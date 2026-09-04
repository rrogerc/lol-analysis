"""The web app shell: `lol.py serve` (local dashboard) and `lol.py export`
(self-contained static snapshot of the same site).

Both expose the same API shape — serve answers JSON per request, export
pre-bakes the identical paths as files — so web/index.html runs unchanged in
either mode. New analysis domains plug in by adding their endpoints here in
both places. The builds scenarios are precomputed either way (builds.warm):
serve keeps them warm in the background and never simulates on request.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

import builds
import items
import scaling
from common import BASE_DIR, WEB_DIR, db_connect

JOBS_DIR = os.path.join(BASE_DIR, "jobs")

# The systemd units that run this repo, declared in
# dotfiles/nixos/configuration.nix. Listed here rather than discovered, so a
# unit that disappears shows up as "not found" instead of silently dropping
# out of the table. The dashboard is a *system* unit, the timers *user*
# ones (the item refresh pushes over SSH as rogerc, the scaling sync writes
# lol.db as rogerc) — they need different buses.
SERVICES = [
    {"unit": "lol-dashboard.service", "scope": "system", "runs": "continuous",
     "desc": "serves this dashboard on :8321, reachable over Tailscale only"},
    {"unit": "lol-items-refresh.timer", "svc": "lol-items-refresh.service",
     "scope": "user", "runs": "daily 07:23",
     "desc": "triggers jobs/refresh-items.sh — its own outcome is below"},
    {"unit": "lol-scaling-sync.timer", "svc": "lol-scaling-sync.service",
     "scope": "user", "runs": "every 6h at :30",
     "desc": "triggers jobs/sync-scaling.sh — its own outcome is below"},
]


def local_jobs():
    """Scheduled scripts in jobs/ (run by systemd user timers on the home
    server). Each script declares itself in its header comments: `# name:`,
    `# schedule:`, then description lines."""
    jobs = []
    if not os.path.isdir(JOBS_DIR):
        return jobs
    for fn in sorted(os.listdir(JOBS_DIR)):
        if not fn.endswith(".sh"):
            continue
        with open(os.path.join(JOBS_DIR, fn)) as f:
            text = f.read()
        header = []
        for line in text.splitlines():
            if line.startswith("#!"):
                continue
            if not line.startswith("#"):
                break
            header.append(line.lstrip("# ").rstrip())
        fields = {}
        desc = []
        for line in header:
            m = re.match(r"(name|schedule):\s*(.+)", line)
            if m:
                fields[m.group(1)] = m.group(2)
            else:
                desc.append(line)
        job = {"name": fields.get("name", fn), "file": f"jobs/{fn}",
               "runs": fields.get("schedule", "?"), "desc": " ".join(desc)}
        # Heartbeat the script writes on every exit (jobs/.state/, gitignored)
        # — feeds the UI health indicator on the local dashboard.
        state_path = os.path.join(JOBS_DIR, ".state", fn[:-3] + ".json")
        if os.path.exists(state_path):
            with open(state_path) as f:
                state = json.load(f)
            job["lastRun"] = state.get("finishedAt")
            job["lastExit"] = state.get("exit")
        jobs.append(job)
    return jobs


def systemctl_show(unit, props, scope):
    """`systemctl show` as a dict, or {} when systemd can't answer — not this
    box, unit removed, user bus unreachable. Never raises: the Data tab has
    to render with or without a verdict."""
    cmd = ["systemctl", "show", unit, "--timestamp=unix", "-p", ",".join(props)]
    env = dict(os.environ)
    if scope == "user":
        cmd.insert(1, "--user")
        # A system service running as rogerc has no session bus of its own.
        # Lingering keeps /run/user/<uid> alive, which is all systemctl needs.
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5,
                             env=env)
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(l.split("=", 1) for l in out.stdout.splitlines() if "=" in l)


def unix_stamp(v):
    """systemd's '@<epoch>' (from --timestamp=unix) as an int. Unset
    timestamps come back as 0 or 'n/a' — both mean "never", i.e. None."""
    if v and v.startswith("@"):
        try:
            return int(float(v[1:])) or None
        except ValueError:
            return None
    return None


def services():
    """Live state of the units behind this repo, for the Data tab.

    The restart count is the point. A service that can't bind its port
    restarts forever, and no other indicator here would show it: whichever
    process *did* get the port is the one serving this page."""
    rows = []
    for s in SERVICES:
        row = {k: s[k] for k in ("unit", "runs", "desc")}
        p = systemctl_show(s["unit"],
                           ["LoadState", "ActiveState", "NRestarts",
                            "ExecMainStartTimestamp", "NextElapseUSecRealtime",
                            "LastTriggerUSec"], s["scope"])
        row["loaded"] = p.get("LoadState") == "loaded"
        if not row["loaded"]:
            rows.append(row)
            continue
        row["state"] = p.get("ActiveState")
        if s["unit"].endswith(".timer"):
            row["last"] = unix_stamp(p.get("LastTriggerUSec"))
            row["next"] = unix_stamp(p.get("NextElapseUSecRealtime"))
            # A timer is only ever "armed" — whether the run it triggered
            # succeeded is on the service it starts.
            if s.get("svc"):
                r = systemctl_show(s["svc"], ["ActiveState", "Result"], s["scope"])
                row["result"] = r.get("Result")
                row["running"] = r.get("ActiveState") in ("active", "activating")
        else:
            row["since"] = unix_stamp(p.get("ExecMainStartTimestamp"))
            row["restarts"] = int(p.get("NRestarts") or 0)
        rows.append(row)
    return rows


def app_meta(con):
    meta = scaling.api_meta(con)
    meta["jobs"] = local_jobs()
    meta["services"] = services()
    meta["itemsSnapshot"] = items.latest_snapshot()
    return meta


def cmd_export(args):
    """Write the web app + pre-generated API JSON as a static site."""
    import shutil
    con = db_connect()
    meta = app_meta(con)
    # The export is a frozen snapshot: job heartbeats would only age into a
    # false "timer may be dead" wherever it's viewed later, so strip them.
    # Unit state is worse — it describes a machine the reader isn't on — so
    # drop it entirely and let the Services card hide itself.
    meta.pop("services", None)
    for job in meta["jobs"]:
        job.pop("lastRun", None)
        job.pop("lastExit", None)
    if not meta["tiers"]:
        sys.exit("Database is empty — run `lol.py scaling sync` or `lol.py import-json` first.")
    out = args.out
    os.makedirs(os.path.join(out, "api"), exist_ok=True)
    shutil.copy(os.path.join(WEB_DIR, "index.html"), os.path.join(out, "index.html"))

    def dump(rel, obj):
        path = os.path.join(out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f, separators=(",", ":"))

    dump("api/meta.json", meta)
    files = 1
    dump("api/builds/meta.json", builds.api_builds_meta())
    files += 1
    # scenario cells are precomputed: warm whatever is cold, then copy
    if builds.warm() is None:
        sys.exit("Another warm is running — wait for it, then export again.")
    paths = builds.cell_paths()
    for slug, key in paths:
        dump(f"api/builds/{slug}/{key}.json", builds.cached_scenario(slug, key, paths))
        files += 1
    dump("api/builds/status.json",
         {"ready": {f"{slug}/{key}": True for slug, key in paths}, "warmer": "idle"})
    files += 1
    for tier in meta["tiers"]:
        patches = scaling.db_patches(con, tier)
        dump(f"api/rows/{tier}.json", scaling.build_rows(con, tier, patches))
        names = [r[0] for r in con.execute(
            "SELECT DISTINCT champion FROM stats WHERE tier=? ORDER BY champion", (tier,))]
        dump(f"api/champions/{tier}.json", names)
        for name in names:
            dump(f"api/champion/{tier}/{name}.json", scaling.api_champion(con, tier, name))
        files += 2 + len(names)
    print(f"Exported static site to {out}/ ({files} API files, tiers: {', '.join(meta['tiers'])})")


class AutoWarm:
    """Keeps the builds cache warm from inside `serve`: once a minute, if any
    cell is cold, runs `lol.py builds warm` as a subprocess. A subprocess
    rather than a thread because the enumerator forks a worker pool, which is
    only safe from a single-threaded process, and because a crash there must
    not take the dashboard down. The warm lock stops it ever doubling up with
    a manual run. A warm that exits non-zero — it failed, or someone killed
    it — turns auto-warm off until serve restarts, so a broken setup can't
    loop and a deliberate kill sticks."""

    def __init__(self, enabled):
        self.proc = None
        self.failed = False
        if enabled:
            threading.Thread(target=self._loop, daemon=True).start()

    def state(self):
        """One word for the dashboard: running, idle, failed, or stale."""
        if builds.source_stale():
            return "stale"
        if self.failed:
            return "failed"
        if builds.warm_running():
            return "running"
        return "idle"

    def _tick(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                return
            self.failed = self.proc.returncode != 0
            self.proc = None
        if self.state() != "idle" or all(builds.cell_ready().values()):
            return
        os.makedirs(builds.SCENARIO_CACHE_DIR, exist_ok=True)
        with open(os.path.join(builds.SCENARIO_CACHE_DIR, "warm.log"), "a") as log:
            self.proc = subprocess.Popen(
                [sys.executable, os.path.join(BASE_DIR, "lol.py"), "builds", "warm"],
                cwd=BASE_DIR, stdout=log, stderr=subprocess.STDOUT)

    def _loop(self):
        while True:
            try:
                self._tick()
            except BaseException as e:  # builds' loaders report missing data via sys.exit
                print(f"auto-warm: {e}", file=sys.stderr)
            time.sleep(60)

    def stop(self):
        """Serve is exiting: take the warm we started with us. The next serve
        starts a fresh one within a minute, resuming at the next cold cell."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()


def cmd_serve(args):
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    warmer = AutoWarm(enabled=not args.no_warm)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if u.path == "/api/builds/meta.json":
                    self._json(builds.api_builds_meta())
                    return
                if u.path == "/api/builds/status.json":
                    self._json({"ready": builds.cell_ready(), "warmer": warmer.state()})
                    return
                if m := re.fullmatch(r"/api/builds/([a-z0-9]+)/([a-z0-9-]+)\.json", u.path):
                    try:
                        out = builds.cached_scenario(m.group(1), m.group(2))
                    except ValueError as e:
                        self._json({"error": str(e)}, 404)
                        return
                    # 202: "not computed yet" is an answer, not an error — the tab polls
                    if out is None:
                        self._json({"pending": True}, 202)
                    else:
                        self._json(out)
                    return
                con = db_connect()
                if u.path == "/api/meta.json":
                    self._json(app_meta(con))
                elif m := re.fullmatch(r"/api/rows/([a-z0-9_]+)\.json", u.path):
                    tier = m.group(1)
                    patches = scaling.db_patches(con, tier)
                    if not patches:
                        self._json({"error": f"no data for tier {tier}"}, 404)
                        return
                    self._json(scaling.build_rows(con, tier, patches))
                elif m := re.fullmatch(r"/api/champions/([a-z0-9_]+)\.json", u.path):
                    names = [r[0] for r in con.execute(
                        "SELECT DISTINCT champion FROM stats WHERE tier=? ORDER BY champion",
                        (m.group(1),))]
                    self._json(names)
                elif m := re.fullmatch(r"/api/champion/([a-z0-9_]+)/([a-z0-9]+)\.json", u.path):
                    self._json(scaling.api_champion(con, m.group(1), m.group(2)))
                else:
                    self._json({"error": "not found"}, 404)
            except BrokenPipeError:
                pass
            except (Exception, SystemExit) as e:  # loaders sys.exit on missing data
                self._json({"error": str(e)}, 500)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Serving dashboard at {url}  (Ctrl-C to stop)")
    if args.no_warm:
        print("Builds auto-warm is off; run `lol.py builds warm` by hand.")
    else:
        print("Cold builds scenarios warm in the background "
              "(log: .cache/builds/warm.log)")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    # a restart's SIGTERM must unwind normally so the warm we spawned goes too
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        warmer.stop()
