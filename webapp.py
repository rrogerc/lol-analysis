"""The web app shell: `lol.py serve` (local dashboard) and `lol.py export`
(self-contained static snapshot of the same site).

Both expose the same API shape — serve computes JSON per request, export
pre-bakes the identical paths as files — so web/index.html runs unchanged in
either mode. New analysis domains plug in by adding their endpoints here in
both places.
"""

import json
import os
import re
import sys

import builds
import items
import scaling
from common import BASE_DIR, WEB_DIR, db_connect

WORKFLOWS_DIR = os.path.join(BASE_DIR, ".github", "workflows")
JOBS_DIR = os.path.join(BASE_DIR, "jobs")


def humanize_cron(expr):
    m, h, dom, mon, dow = (expr.split() + ["*"] * 5)[:5]
    if dom == mon == dow == "*" and m.isdigit() and h.isdigit():
        return f"daily {int(h):02d}:{int(m):02d} UTC"
    return f"cron {expr}"


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


def workflow_jobs():
    """The repo's GitHub Actions workflows, for the UI's automation panel:
    name, when they run, and the file's leading comment block as description.
    Parsed straight from .github/workflows/ so new jobs show up on their own."""
    jobs = []
    if not os.path.isdir(WORKFLOWS_DIR):
        return jobs
    for fn in sorted(os.listdir(WORKFLOWS_DIR)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(WORKFLOWS_DIR, fn)) as f:
            text = f.read()
        name = re.search(r"^name:\s*(.+)$", text, re.M)
        header = text.split("\non:", 1)[0]
        desc = " ".join(l.lstrip("# ").rstrip() for l in header.splitlines()
                        if l.startswith("#"))
        runs = [humanize_cron(c) for c in
                re.findall(r"""cron:\s*["']([^"']+)["']""", text)]
        if re.search(r"^\s{2,}push:", text, re.M):
            runs.append("on push to main")
        if re.search(r"^\s{2,}workflow_dispatch:", text, re.M):
            runs.append("manual")
        jobs.append({"name": name.group(1).strip() if name else fn,
                     "file": f".github/workflows/{fn}",
                     "runs": ", ".join(runs) or "?", "desc": desc})
    return jobs


def app_meta(con):
    meta = scaling.api_meta(con)
    meta["jobs"] = local_jobs() + workflow_jobs()
    meta["itemsSnapshot"] = items.latest_snapshot()
    return meta


def cmd_export(args):
    """Write the web app + pre-generated API JSON as a static site."""
    import shutil
    con = db_connect()
    meta = app_meta(con)
    # The export is a frozen snapshot: job heartbeats would only age into a
    # false "timer may be dead" wherever it's viewed later, so strip them.
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
    builds_meta = builds.api_builds_meta()
    dump("api/builds/meta.json", builds_meta)
    files += 1
    for champ in builds_meta["champions"]:
        for sc in builds_meta["scenarios"]:
            dump(f"api/builds/{champ['slug']}/{sc['key']}.json",
                 builds.api_optimize_scenario(champ["slug"], sc["key"]))
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


def cmd_serve(args):
    import threading
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

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
                if m := re.fullmatch(r"/api/builds/([a-z0-9]+)/([a-z0-9-]+)\.json", u.path):
                    try:
                        self._json(builds.api_optimize_scenario(m.group(1), m.group(2)))
                    except ValueError as e:
                        self._json({"error": str(e)}, 404)
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
            except Exception as e:
                self._json({"error": str(e)}, 500)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Serving dashboard at {url}  (Ctrl-C to stop)")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
