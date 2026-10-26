#!/usr/bin/env python3
"""Champion kit dossiers and rotation-primitive triage.

Consolidates dossier writing (one model call per part, validated against
archived Riot bin + wiki sources) and roster triage (classifying champions by
rotation primitives).

  build <slug>...   write and validate dossiers for the named champions
  build-all         write and validate dossiers for every champion, simplest first
  check <slug>      validate lol/data/builds/dossiers/<slug>.json
  status            report archived sources, passing dossiers, kits, and triage
  triage batches    split wiki-all abilities into per-champion inputs and batches
  triage check      validate one batch's triage outputs (_triage/out/<slug>.json)
  triage merge      validate and merge _triage/out/*.json into triage.json
"""

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from lol.codegen import sources as ks
from web.db import BASE_DIR

CODEGEN_DIR = Path(__file__).resolve().parent
PROMPT_PATH = CODEGEN_DIR / "dossier-prompt.md"
TRIAGE_PROMPT_PATH = CODEGEN_DIR / "triage-prompt.md"
DOSSIERS_DIR = ks.DOSSIERS_DIR
KITS_DIR = ks.KITS_DIR
CACHE_DIR = os.path.join(BASE_DIR, ".cache", "kit-dossier")

PARTS = ks.SLOTS + ("top",)
FREE = {"defensive-only"}  # never needs driver support

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


# ---------------------------------------------------------------------------
# dossier prompts & runner
# ---------------------------------------------------------------------------

def prompt_sections():
    """{heading: text} of dossier-prompt.md's ## sections."""
    text = PROMPT_PATH.read_text()
    out = {}
    for m in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", text, re.M | re.S):
        out[m.group(1).strip()] = m.group(2).strip()
    return out


def triage_prompt():
    """Full text of triage-prompt.md."""
    return TRIAGE_PROMPT_PATH.read_text()


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
        if not errors and not untouched(part, current) and not args.force:
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


def _run_build(args, slugs):
    if not slugs:
        raise SystemExit("name champions, or use `build-all`")
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
    out = os.path.join(CACHE_DIR, "last-run.json")
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


def cmd_build(args):
    slugs = all_slugs() if getattr(args, "all", False) else [s.lower() for s in args.slugs]
    _run_build(args, slugs)


def cmd_build_all(args):
    _run_build(args, all_slugs())


def cmd_check(args):
    ks.cmd_check(args)


def cmd_status(args):
    patch = args.patch or ks.default_patch()
    src_patch = os.path.join(ks.SOURCES_DIR, patch)
    n_sources = sum(1 for d in os.listdir(src_patch)
                    if not d.startswith("_") and os.path.isdir(os.path.join(src_patch, d))) \
        if os.path.isdir(src_patch) else 0
    dossiers = sorted(f[:-5] for f in os.listdir(DOSSIERS_DIR) if f.endswith(".json")) \
        if os.path.isdir(DOSSIERS_DIR) else []
    kits = sorted(f[:-5] for f in os.listdir(KITS_DIR) if f.endswith(".json")) \
        if os.path.isdir(KITS_DIR) else []
    passing, failing = 0, []
    for slug in dossiers:
        try:
            chk = ks.Checker(slug, ks.load_dossier(slug), ks.load_sources(slug, args.patch))
            chk.run()
            if chk.errors:
                failing.append(slug)
            else:
                passing += 1
        except SystemExit:
            failing.append(slug)
    tri_path = os.path.join(ks.SOURCES_DIR, patch, "_triage", "triage.json")
    triage_info = "none"
    if os.path.exists(tri_path):
        with open(tri_path) as f:
            tri = json.load(f)
        triage_info = f"{tri.get('champions', 0)} triaged ({len(tri.get('needsBespoke', []))} bespoke)"
    print(f"patch {patch}: {n_sources} archived sources, "
          f"{passing}/{len(dossiers)} dossiers passing"
          + (f" (failing: {', '.join(failing)})" if failing else "")
          + f", {len(kits)} kits, triage: {triage_info}")


# ---------------------------------------------------------------------------
# triage (consolidated from kit_triage.py)
# ---------------------------------------------------------------------------

def triage_dir(patch=None):
    return os.path.join(ks.SOURCES_DIR, patch or ks.default_patch(), "_triage")


def cmd_triage_batches(args):
    """Deal every champion that has no result yet into N batches. A result
    whose input has changed since it was written is stale: it is removed and
    the champion is dealt again."""
    tdir = triage_dir(args.patch)
    with open(os.path.join(tdir, "abilities.json")) as f:
        champs = json.load(f)
    os.makedirs(os.path.join(tdir, "in"), exist_ok=True)
    os.makedirs(os.path.join(tdir, "out"), exist_ok=True)
    todo = []
    for slug in sorted(champs):
        in_path = os.path.join(tdir, "in", f"{slug}.json")
        out_path = os.path.join(tdir, "out", f"{slug}.json")
        old = None
        if os.path.exists(in_path):
            with open(in_path) as f:
                old = json.load(f)
        if old != {slug: champs[slug]}:
            ks.write_json(in_path, {slug: champs[slug]})
            if os.path.exists(out_path):
                os.remove(out_path)
                print(f"{slug}: input changed, stale result removed")
        if not os.path.exists(out_path):
            todo.append(slug)
    for name in os.listdir(os.path.join(tdir, "in")):
        if name.startswith("batch-"):
            os.remove(os.path.join(tdir, "in", name))
    # deal champions round-robin by size so the batches weigh about the same
    by_size = sorted(todo, key=lambda s: -len(json.dumps(champs[s])))
    batches = [[] for _ in range(args.n)]
    for i, slug in enumerate(by_size):
        batches[i % args.n if (i // args.n) % 2 == 0 else args.n - 1 - i % args.n].append(slug)
    for i, batch in enumerate(batches, 1):
        path = os.path.join(tdir, "in", f"batch-{i}.txt")
        with open(path, "w") as f:
            f.write("\n".join(sorted(batch)) + "\n")
        print(f"{os.path.relpath(path, BASE_DIR)}: {len(batch)} champions")
    print(f"{len(champs) - len(todo)} champions already have a result")


def batch_slugs(tdir, n):
    with open(os.path.join(tdir, "in", f"batch-{n}.txt")) as f:
        return [line.strip() for line in f if line.strip()]


def load_results(tdir, slugs):
    """({slug: result}, errors) from _triage/out/<slug>.json."""
    merged, errors = {}, []
    for slug in slugs:
        path = os.path.join(tdir, "out", f"{slug}.json")
        if not os.path.exists(path):
            errors.append(f"{slug}: no result file out/{slug}.json")
            continue
        try:
            with open(path) as f:
                r = json.load(f)
        except json.JSONDecodeError as e:
            errors.append(f"{slug}: out/{slug}.json is not valid JSON: {e}")
            continue
        merged[slug] = r.get(slug, r) if isinstance(r, dict) else r
    return merged, errors


def validate(result, expected=None):
    errors = []
    if not isinstance(result, dict):
        return ["the result must be a JSON object keyed by champion slug"]
    if expected is not None:
        for slug in sorted(set(expected) - set(result)):
            errors.append(f"{slug}: missing")
        for slug in sorted(set(result) - set(expected)):
            errors.append(f"{slug}: not in this batch")
    for slug, r in result.items():
        if not isinstance(r, dict):
            errors.append(f"{slug}: must be an object")
            continue
        prims = r.get("primitives")
        if not isinstance(prims, list) or not prims:
            errors.append(f"{slug}: primitives must be a non-empty list")
            prims = []
        for p in prims:
            if not isinstance(p, dict) or p.get("id") not in ks.PRIMITIVES:
                errors.append(f"{slug}: unknown primitive {p.get('id') if isinstance(p, dict) else p!r}")
            elif not isinstance(p.get("abilities"), list) or not p["abilities"] \
                    or not set(p["abilities"]) <= set(ks.SLOTS):
                errors.append(f"{slug}: {p['id']}: abilities must be a non-empty list of P/Q/W/E/R")
        if not isinstance(r.get("needsBespoke"), bool):
            errors.append(f"{slug}: needsBespoke must be true or false")
        if not isinstance(r.get("why"), str) or not r.get("why"):
            errors.append(f"{slug}: why must say what decides needsBespoke")
        if r.get("confidence") not in ("high", "medium", "low"):
            errors.append(f"{slug}: confidence must be high, medium or low")
        if r.get("damageFrom") not in ("attacks", "abilities", "mixed"):
            errors.append(f"{slug}: damageFrom must be attacks, abilities or mixed")
    return errors


def cmd_triage_check(args):
    tdir = triage_dir(args.patch)
    slugs = batch_slugs(tdir, args.batch)
    merged, errors = load_results(tdir, slugs)
    errors += validate(merged)
    for e in errors:
        print(f"ERROR   {e}")
    print(f"batch {args.batch}: {len(merged)}/{len(slugs)} champions, {len(errors)} errors - "
          f"{'OK' if not errors else 'FAILED'}")
    sys.exit(1 if errors else 0)


def cmd_triage_merge(args):
    tdir = triage_dir(args.patch)
    with open(os.path.join(tdir, "abilities.json")) as f:
        everyone = sorted(json.load(f))
    merged, errors = load_results(tdir, everyone)
    errors += validate(merged)
    for e in errors:
        print(f"ERROR   {e}")
    needs = {slug: {p["id"] for p in r.get("primitives", []) if isinstance(p, dict)} - FREE
             for slug, r in merged.items()}
    bespoke = sorted(s for s, r in merged.items() if r.get("needsBespoke"))
    fits = {s: n for s, n in needs.items() if s not in bespoke}
    freq = {}
    for n in needs.values():
        for p in n:
            freq[p] = freq.get(p, 0) + 1
    # greedy order: the next primitive is the one that completes the most
    # champions, ties broken by how many champions use it at all
    supported, order = set(), []
    remaining = set(freq)
    while remaining:
        def gain(p):
            s2 = supported | {p}
            return (sum(1 for n in fits.values() if n <= s2), freq[p])
        best = max(sorted(remaining), key=gain)
        supported.add(best)
        remaining.discard(best)
        order.append({"primitive": best, "usedBy": freq[best],
                      "covered": sum(1 for n in fits.values() if n <= supported)})
    out = {"champions": len(merged), "needsBespoke": bespoke, "order": order, "results": merged}
    ks.write_json(os.path.join(tdir, "triage.json"), out)
    print(f"{len(merged)} champions; {len(bespoke)} flagged as needing bespoke logic")
    print(f"{'primitive':28} {'used by':>8} {'champions fully covered':>24}")
    for row in order:
        print(f"{row['primitive']:28} {row['usedBy']:>8} {row['covered']:>24}")
    sys.exit(1 if errors else 0)


# Keep aliases matching legacy kit_triage names
cmd_batches = cmd_triage_batches
cmd_merge = cmd_triage_merge


def _runner_args(sp):
    sp.add_argument("--workers", type=int, default=4, help="champions in parallel")
    sp.add_argument("--model", default="sonnet")
    sp.add_argument("--effort", default="low", help="effort for the ability parts")
    sp.add_argument("--top-effort", default="medium", help="effort for top.json")
    sp.add_argument("--rounds", type=int, default=3, help="calls per part at most")
    sp.add_argument("--timeout", type=int, default=900, help="seconds per call")
    sp.add_argument("--force", action="store_true", help="rewrite parts that already pass")
    sp.add_argument("--claude", default=shutil.which("claude") or "claude", help="the CLI binary")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("build", help="write and check dossiers for named champions")
    sp.add_argument("slugs", nargs="*")
    sp.add_argument("--all", action="store_true", help="every champion, simplest kits first")
    _runner_args(sp)
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("build-all", help="write and check dossiers for every champion")
    _runner_args(sp)
    sp.set_defaults(func=cmd_build_all)

    sp = sub.add_parser("check", help="validate a champion's dossier against archived sources")
    sp.add_argument("slug")
    sp.add_argument("--patch")
    sp.add_argument("--slot", choices=[*ks.SLOTS, *(x.lower() for x in ks.SLOTS)],
                    help="report only this ability's findings")
    sp.add_argument("--dossier", help="path (default lol/data/builds/dossiers/<slug>.json)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("status", help="summarize archived sources, dossiers, kits, and triage")
    sp.add_argument("--patch")
    sp.set_defaults(func=cmd_status)

    tri = sub.add_parser("triage", help="triage champions by rotation primitives")
    tri.add_argument("--patch")
    trisub = tri.add_subparsers(dest="triage_cmd", required=True)

    sp = trisub.add_parser("batches")
    sp.add_argument("--patch")
    sp.add_argument("--n", type=int, default=9)
    sp.set_defaults(func=cmd_triage_batches)

    sp = trisub.add_parser("check")
    sp.add_argument("--patch")
    sp.add_argument("--batch", type=int, required=True)
    sp.set_defaults(func=cmd_triage_check)

    sp = trisub.add_parser("merge")
    sp.add_argument("--patch")
    sp.set_defaults(func=cmd_triage_merge)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
