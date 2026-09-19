#!/usr/bin/env python3
"""Triage every champion by the rotation primitives its damage fight needs.

  batches [--n N]   split data/builds/sources/<patch>/_triage/abilities.json
                    (written by `kit_sources.py wiki-all`) into one input file
                    per champion (_triage/in/<slug>.json) and N batch lists
                    (_triage/in/batch-N.txt)
  check --batch N   validate the results of one batch (_triage/out/<slug>.json)
  merge             validate and merge _triage/out/*.json, then print how many
                    champions a generic driver would cover as primitives are
                    added in the most useful order

The primitive vocabulary is kit_sources.PRIMITIVES. Stdlib only.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_sources as ks  # noqa: E402

FREE = {"defensive-only"}  # never needs driver support


def triage_dir(patch=None):
    return os.path.join(ks.SOURCES_DIR, patch or ks.default_patch(), "_triage")


def cmd_batches(args):
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
        print(f"{os.path.relpath(path, ks.BASE_DIR)}: {len(batch)} champions")
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


def cmd_check(args):
    tdir = triage_dir(args.patch)
    slugs = batch_slugs(tdir, args.batch)
    merged, errors = load_results(tdir, slugs)
    errors += validate(merged)
    for e in errors:
        print(f"ERROR   {e}")
    print(f"batch {args.batch}: {len(merged)}/{len(slugs)} champions, {len(errors)} errors - "
          f"{'OK' if not errors else 'FAILED'}")
    sys.exit(1 if errors else 0)


def cmd_merge(args):
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--patch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("batches")
    sp.add_argument("--n", type=int, default=9)
    sp.set_defaults(func=cmd_batches)
    sp = sub.add_parser("check")
    sp.add_argument("--batch", type=int, required=True)
    sp.set_defaults(func=cmd_check)
    sp = sub.add_parser("merge")
    sp.set_defaults(func=cmd_merge)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
