#!/usr/bin/env python3
"""Replay the golden fights (data/tft/golden/fights.json) on the compiled
TFT engine and diff every number bit for bit against the recorded benchmark
(test_tft.TestGolden does the same; this prints per-unit detail). Fixture
provenance identifies the generating engine, patch and dummy inputs.

    python3 jobs/tft_compare.py [--unit Ahri ...] [--driver Ahri ...] [--max-report N] [-v]

Exit 0 when every fight matches, 1 otherwise. A mismatch prints the case
(unit, scenario, items) and the first differing fields; a driver that is
not ported yet is reported and skipped. An int and a float of equal value
count as the same number.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tft  # noqa: E402

GOLDEN = os.path.join(tft.TFT_DATA_DIR, "golden", "fights.json")
RESULT_KEYS = ("killTime", "total", "dps", "rawTotal", "attacks", "casts", "castTimes",
               "breakdown", "left", "t", "aliveTime", "died", "diedAt", "hpLeft", "absorbed",
               "taken", "mitigated", "healed", "shielded", "denied", "allyHeal", "allyShield",
               "ccTime", "hitsTaken")
SHEET_KEYS = ("ad", "ap", "as", "crit", "critMult", "precision", "hp", "armor", "mr",
              "durability", "omnivamp", "form", "manaStart", "manaMax")


def dec(v):
    """Undo gen_tft_golden.enc for the non-finite strings."""
    if v == "inf":
        return float("inf")
    if v == "-inf":
        return float("-inf")
    if v == "nan":
        return float("nan")
    return v


def same(a, b):
    a, b = dec(a), dec(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b and type(a) is type(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a != a and b != b:
            return True
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(same(a[k], b[k]) for k in a)
    return a == b


def diffs(want, got, keys):
    out = []
    for k in keys:
        if k not in want:
            continue
        if k not in got:
            out.append((k, want[k], "<missing>"))
        elif not same(want[k], got[k]):
            out.append((k, want[k], got[k]))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--unit", nargs="*", help="unit names or api names to check (default: all)")
    ap.add_argument("--driver", nargs="*", help="driver names to check")
    ap.add_argument("--max-report", type=int, default=12)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    with open(GOLDEN) as f:
        fixture = json.load(f)
    provenance = fixture["provenance"]
    snap = tft.load_snapshot(provenance["set"], provenance["patch"])
    item_fx = tft.load_item_effects(snap.set_no)
    trait_fx = tft.load_trait_effects(snap.set_no)
    dummy = provenance.get("dummy", tft.dummies_for(snap))
    cases = fixture["cases"]
    want_units = None
    if args.unit:
        want_units = {snap.unit(x)["api"] for x in args.unit}
    if args.driver:
        drivers = tft.engine().DRIVERS
        picked = {api for api, name in drivers.items() if name in set(args.driver)}
        want_units = picked if want_units is None else want_units | picked
    per_unit = {}
    skipped = set()
    reported = 0
    for case in cases:
        api = case["unit"]
        if want_units is not None and api not in want_units:
            continue
        unit = snap.units[api]
        stats = per_unit.setdefault(api, [0, 0])
        try:
            sheet, res = tft.simulate(snap, unit, case["star"], case["items"], case["geometry"],
                                         [tuple(x) for x in case["ctxTraits"]], dummy, None,
                                         item_fx, trait_fx)
        except ValueError as e:
            if "not ported" in str(e):
                skipped.add(unit["name"])
                continue
            raise
        d = diffs(case["sheet"], sheet, SHEET_KEYS) + diffs(case["result"], res, RESULT_KEYS)
        stats[0] += 1
        if d:
            stats[1] += 1
            if reported < args.max_report:
                reported += 1
                names = [snap.items[a]["name"] for a in case["items"]]
                print(f"MISMATCH {unit['name']} {case['scenario']} [{', '.join(names) or 'no items'}]")
                for k, w, g in d[:6]:
                    print(f"    {k}: expected {w!r}\n    {' ' * len(k)}  actual   {g!r}")
        elif args.verbose:
            print(f"ok {unit['name']} {case['scenario']} {case['items']}")
    bad = 0
    for api, (n, m) in sorted(per_unit.items(), key=lambda kv: snap.units[kv[0]]["name"]):
        if n == 0:
            continue
        mark = "ok  " if m == 0 else "FAIL"
        print(f"{mark} {snap.units[api]['name']:<14} {n - m}/{n} fights identical")
        bad += m
    if skipped:
        print(f"not ported yet: {', '.join(sorted(skipped))}")
    total = sum(n for n, _ in per_unit.values())
    print(f"{total - bad}/{total} fights identical")
    sys.exit(0 if bad == 0 else 1)


if __name__ == "__main__":
    main()
