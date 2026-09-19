#!/usr/bin/env python3
"""Write champion kit dossiers with a cheap model, one small call per part.

  kit_dossier.py <slug>... | --all  [--workers 4] [--effort low]
                 [--top-effort medium] [--model sonnet] [--rounds 3] [--force]

--all takes every champion of ddragon's list for the patch, the simplest kits
first (by the triage's primitive count, when a triage exists). The run stops
by itself, with exit code 75, when the CLI reports the plan's usage limit (or
is not logged in): run the same command again later and it resumes.

For each champion: fetch the sources and write the skeleton if they are
missing (jobs/kit_sources.py), then for P, Q, W, E, R and top.json call the
model with that part's rules, the numbers sheet, the wiki templates and the
skeleton, write what it returns, run the checker on it and, while errors
remain, send them back (up to --rounds calls a part). A part that already
passes is kept, so an interrupted run resumes; --force rewrites everything.

The model is called through the Claude Code CLI in print mode (`claude -p`)
with no tools and its own system prompt: it uses the login the CLI already
has, so no API key, and it runs from an empty directory, so the project's
CLAUDE.md never reaches it. Every call's seconds, tokens and cost-equivalent
are logged to data/builds/dossiers/<slug>.parts/run.json.

The instructions come from jobs/kit-dossier-prompt.md (its "fight", "Reading
the sources", "An ability part" and "top.json" sections), so the agent
protocol and this runner cannot drift apart. Stdlib only.
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_sources as ks  # noqa: E402

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kit-dossier-prompt.md")
PARTS = ks.SLOTS + ("top",)
_print_lock = threading.Lock()
_stop = threading.Event()  # the plan's usage limit was hit, or the CLI is logged out
_stop_reason = []

# what the CLI answers when the subscription's window is used up / when it has
# no login; anything else that fails is retried with a pause before giving up
LIMIT = re.compile(r"(usage|session|weekly|opus|sonnet|5[- ]hour|hour)\s+limit|limit reached|"
                   r"hit your .{0,40}limit|out of (extra )?usage|resets? (at |in )?\d", re.I)
LOGGED_OUT = re.compile(r"not logged in|/login|invalid api key|authentication", re.I)
TRANSIENT = re.compile(r"rate.?limit|overloaded|timeout|timed out|529|503|502|connection", re.I)


def say(msg):
    with _print_lock:
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


_streak_lock = threading.Lock()
_streak = [0]  # calls that failed in a row, over all workers


def note_call(ok, failure=None, limit=10):
    """A run in which nothing succeeds any more ends itself."""
    with _streak_lock:
        _streak[0] = 0 if ok else _streak[0] + 1
        if _streak[0] >= limit:
            stop(f"{limit} calls in a row failed, the last with: {failure}")


def stop(reason):
    if not _stop.is_set():
        _stop_reason.append(reason)
        _stop.set()
        say(f"STOPPING: {reason}")


def prompt_sections():
    """{heading: text} of the instructions file's ## sections."""
    with open(PROMPT_PATH) as f:
        text = f.read()
    out = {}
    for m in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", text, re.M | re.S):
        out[m.group(1).strip()] = m.group(2).strip()
    return out


def system_prompt(part):
    sec = prompt_sections()
    what = "the `top.json` part" if part == "top" else "ONE ability part"
    rules = sec["top.json"] if part == "top" else sec["An ability part"]
    return (f"You fill in {what} of a League of Legends champion kit dossier: JSON that maps "
            "the champion's numbers between Riot's game file and the wiki, states the mechanics "
            "that matter with verbatim quotes, and lists what the sources leave open. A script "
            "checks everything you write; a human and a stronger model make the rulings "
            "afterwards, so be accurate and surface doubt rather than resolve it by guessing.\n\n"
            "Reply with ONLY the finished JSON object for the part: no prose, no code fence.\n\n"
            f"## The fight the dossier is for\n\n{sec['The fight the dossier is for']}\n\n"
            f"## Reading the sources\n\n{sec['Reading the sources']}\n\n"
            f"## {'top.json' if part == 'top' else 'An ability part'}\n\n{rules}")


def wiki_text(src, slots):
    out = []
    for slot in slots:
        for a in src["wiki"]["abilities"][slot]:
            out.append(f"### {slot}: {a['name']} (flags: {json.dumps(a.get('flags', {}))})\n"
                       f"{a.get('wikitext', '(no template on the wiki)')}")
    return "\n\n".join(out)


def user_prompt(slug, part, src, current):
    head = f"Champion: {slug}. Part: {part}.\n\n## Numbers sheet (Riot's bin)\n\n{ks.sheet_text(src)}\n"
    if part == "top":
        parts = {}
        for slot in ks.SLOTS:
            with open(os.path.join(ks.parts_dir(slug), f"{slot}.json")) as f:
                parts[slot] = json.load(f)
        prims = "\n".join(f"- {pid}: {what}" for pid, what in ks.PRIMITIVES.items())
        return (head + f"\n## Wiki templates\n\n{wiki_text(src, ks.SLOTS)}\n\n"
                f"## The five finished ability parts\n\n{json.dumps(parts, ensure_ascii=False)}\n\n"
                f"## The closed list of rotation primitives\n\n{prims}\n\n"
                f"## Skeleton to fill in\n\n{json.dumps(current, indent=1)}")
    return (head + f"\n## Wiki templates of slot {part}\n\n{wiki_text(src, (part,))}\n\n"
            f"## Skeleton to fill in\n\n{json.dumps(current, indent=1, ensure_ascii=False)}")


def call_model(args, system, user, effort, cwd):
    """One print-mode call -> (reply text | None, log entry)."""
    cmd = [args.claude, "-p", "--model", args.model, "--effort", effort, "--tools", "",
           "--system-prompt", system, "--output-format", "json"]
    t0 = time.time()
    for pause in (30, 90, 240, None):
        if _stop.is_set():
            return None, {"seconds": 0, "error": "stopped"}
        o, failure = {}, None
        try:
            r = subprocess.run(cmd, input=user, capture_output=True, text=True, cwd=cwd,
                               timeout=args.timeout)
            try:
                o = json.loads(r.stdout)
            except json.JSONDecodeError:
                failure = f"no JSON from the CLI: {(r.stdout + ' ' + r.stderr).strip()[:300]}"
        except subprocess.TimeoutExpired:
            failure = f"timeout after {args.timeout} s"
        if failure is None and o.get("is_error"):
            failure = str(o.get("result"))[:300]
        if failure is None:
            break
        if LOGGED_OUT.search(failure) or LIMIT.search(failure):
            stop(failure)
            return None, {"seconds": round(time.time() - t0, 1), "error": failure}
        note_call(False, failure)
        if pause is None or not TRANSIENT.search(failure):
            return None, {"seconds": round(time.time() - t0, 1), "error": failure}
        say(f"  transient failure, retrying in {pause} s: {failure[:120]}")
        if _stop.wait(pause):
            return None, {"seconds": round(time.time() - t0, 1), "error": "stopped"}
    note_call(True)
    usage = o.get("usage") or {}
    log = {"seconds": round(time.time() - t0, 1), "cost": o.get("total_cost_usd"),
           "in": sum(usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                               "cache_read_input_tokens")),
           "out": usage.get("output_tokens", 0)}
    return o.get("result") or "", log


def parse_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    if not text.startswith("{"):
        i, j = text.find("{"), text.rfind("}")
        text = text[i:j + 1] if 0 <= i < j else text
    return json.loads(text)


def findings(slug, src, part):
    """(errors, warnings) of the assembled dossier, for one part."""
    path = ks.assemble(slug)
    with open(path) as f:
        chk = ks.Checker(slug, json.load(f), src)
    chk.run()
    if part == "top":
        keep = lambda m: not m.startswith("abilities.")  # noqa: E731
    else:
        keep = lambda m: m.startswith(f"abilities.{part}")  # noqa: E731
    return [e for e in chk.errors if keep(e)], [w for w in chk.warnings if keep(w)]


def untouched(part, current):
    """Whether a part is still the skeleton `init` wrote."""
    if current.get("filledBy"):
        return False
    if part == "top":
        return not current.get("primitives")
    return (not current.get("mechanics") and not current.get("binUnused")
            and all(v.get("role") is None for v in current.get("values") or []))


def run_champion(args, slug, cwd):
    t0 = time.time()
    src = ks.load_sources(slug)
    pdir = ks.parts_dir(slug)
    calls = []
    for part in PARTS:
        path = os.path.join(pdir, f"{part}.json")
        with open(path) as f:
            current = json.load(f)
        errors, _ = findings(slug, src, part)
        if not errors and not untouched(part, current):
            continue  # done in an earlier run
        system = system_prompt(part)
        user = user_prompt(slug, part, src, current)
        for rnd in range(1, args.rounds + 1):
            text, log = call_model(args, system, user,
                                   args.top_effort if part == "top" else args.effort, cwd)
            if _stop.is_set():
                break
            log.update(part=part, round=rnd)
            calls.append(log)
            problem = log.get("error")
            if text is not None:
                try:
                    reply = parse_json(text)
                    if part == "top":
                        reply["champion"] = slug
                    reply["filledBy"] = args.model
                    ks.write_json(path, reply)
                    errors, warnings = findings(slug, src, part)
                    log.update(errors=len(errors), warnings=len(warnings),
                               messages=[e[:240] for e in errors[:8]])
                    if not errors:
                        break
                    problem = "The checker reports:\n" + "\n".join(f"ERROR {e}" for e in errors)
                    current = reply
                except (json.JSONDecodeError, AttributeError, TypeError) as e:
                    problem = f"Your reply was not one valid JSON object ({e})."
                    log["error"] = problem
            user = (user_prompt(slug, part, src, current)
                    + f"\n\n## Your previous attempt failed\n\n{problem}\n\n"
                    "Reply with the corrected JSON object for the whole part. Do not change a "
                    "number to agree with the other source: explain disagreements in binNote.")
        if _stop.is_set():
            break
        say(f"  {slug} {part}: {'ok' if not errors else f'{len(errors)} errors left'} "
            f"after {sum(1 for c in calls if c['part'] == part)} call(s)")
    path = ks.assemble(slug)
    with open(path) as f:
        chk = ks.Checker(slug, json.load(f), src)
    chk.run()
    summary = {"slug": slug, "ok": not chk.errors, "stopped": _stop.is_set(),
               "errors": len(chk.errors),
               "warnings": len(chk.warnings), "calls": len(calls),
               "seconds": round(time.time() - t0, 1),
               "tokensIn": sum(c.get("in", 0) for c in calls),
               "tokensOut": sum(c.get("out", 0) for c in calls),
               "costEquivalentUsd": round(sum(c.get("cost") or 0 for c in calls), 4),
               "model": args.model, "effort": [args.effort, args.top_effort]}
    if calls:
        # a resumed run adds to the log of the run it continues
        log_path = os.path.join(pdir, "run.json")
        earlier = []
        if os.path.exists(log_path) and not args.force:
            with open(log_path) as f:
                earlier = json.load(f).get("calls", [])
        ks.write_json(log_path, {"summary": summary, "calls": earlier + calls})
    if summary["stopped"]:
        say(f"{slug}: interrupted after {len(calls)} calls; its finished parts are kept")
        return summary
    say(f"{slug}: {'OK' if summary['ok'] else 'FAILED'} - {summary['errors']} errors, "
        f"{summary['warnings']} warnings; {summary['calls']} calls, {summary['seconds']} s, "
        f"{summary['tokensIn']} in / {summary['tokensOut']} out tokens, "
        f"${summary['costEquivalentUsd']} cost-equivalent")
    return summary


def all_slugs():
    """Every champion of ddragon's list for the patch; the simplest kits first
    when a triage exists (fewest primitives, champion-specific logic last), so
    an interrupted run has finished as many as it could."""
    patch = ks.default_patch()
    _, index = ks.ddragon_index(patch)
    slugs = sorted(cid.lower() for cid in index)
    tri_path = os.path.join(ks.SOURCES_DIR, patch, "_triage", "triage.json")
    if os.path.exists(tri_path):
        with open(tri_path) as f:
            results = json.load(f)["results"]
        def weight(slug):
            r = results.get(slug)
            return (1, 99, slug) if r is None else \
                (int(bool(r.get("needsBespoke"))), len(r.get("primitives") or []), slug)
        slugs.sort(key=weight)
    return slugs


def guarded(args, slug, cwd):
    try:
        return run_champion(args, slug, cwd)
    except BaseException as e:  # one champion must not end the run (SystemExit included)
        say(f"{slug}: crashed: {type(e).__name__}: {e}")
        return {"slug": slug, "ok": False, "stopped": False, "crashed": str(e)[:300], "calls": 0,
                "costEquivalentUsd": 0}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("slugs", nargs="*")
    ap.add_argument("--all", action="store_true", help="every champion, the simplest kits first")
    ap.add_argument("--workers", type=int, default=4, help="champions in parallel")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="low", help="effort for the ability parts")
    ap.add_argument("--top-effort", default="medium", help="effort for top.json")
    ap.add_argument("--rounds", type=int, default=3, help="calls per part at most")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per call")
    ap.add_argument("--force", action="store_true", help="rewrite parts that already pass")
    ap.add_argument("--claude", default=shutil.which("claude") or "claude", help="the CLI binary")
    args = ap.parse_args()
    slugs = all_slugs() if args.all else [s.lower() for s in args.slugs]
    if not slugs:
        ap.error("name champions, or pass --all")
    t0 = time.time()
    cwd = tempfile.mkdtemp(prefix="kit-dossier-")  # no CLAUDE.md anywhere above it
    futures, skipped = [], []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            # sources and skeletons are made here, one champion ahead of the
            # workers: fetching prints, and the wiki deserves one client at a time
            for slug in slugs:
                if _stop.is_set():
                    break
                try:
                    try:
                        ks.source_dir(slug)
                    except SystemExit:
                        ks.cmd_fetch(argparse.Namespace(slugs=[slug], patch=None, offline=False))
                    if args.force or not os.path.isdir(ks.parts_dir(slug)):
                        ks.cmd_init(argparse.Namespace(slug=slug, patch=None, force=True))
                except (SystemExit, Exception) as e:
                    say(f"{slug}: skipped, no sources: {e}")
                    skipped.append(slug)
                    continue
                futures.append(pool.submit(guarded, args, slug, cwd))
                while not _stop.is_set() and sum(not f.done() for f in futures) >= args.workers * 2:
                    time.sleep(1)  # stay a little ahead of the workers, not the whole roster
            results = [f.result() for f in futures]
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    done = [r for r in results if r["ok"] and not r.get("stopped")]
    bad = [r["slug"] for r in results if not r["ok"] and not r.get("stopped")]
    left = [s for s in slugs if s not in {r["slug"] for r in done} and s not in bad
            and s not in skipped]
    summary = {"finishedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": round(time.time() - t0),
               "passing": [r["slug"] for r in done], "failing": bad, "skipped": skipped,
               "notDone": left, "stoppedBecause": _stop_reason[0] if _stop_reason else None,
               "calls": sum(r.get("calls", 0) for r in results),
               "costEquivalentUsd": round(sum(r.get("costEquivalentUsd", 0) for r in results), 2)}
    out = os.path.join(ks.BASE_DIR, ".cache", "kit-dossier", "last-run.json")
    ks.write_json(out, summary)
    print(f"{len(done)} dossiers pass the checker"
          + (f"; failing: {', '.join(bad)}" if bad else "")
          + (f"; skipped: {', '.join(skipped)}" if skipped else "")
          + (f"; {len(left)} not done" if left else "")
          + f"; {summary['calls']} calls, ${summary['costEquivalentUsd']} cost-equivalent, "
          f"{summary['seconds']} s")
    if _stop.is_set():
        print(f"stopped: {_stop_reason[0]}\nrun the same command again to resume")
        sys.exit(75)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
