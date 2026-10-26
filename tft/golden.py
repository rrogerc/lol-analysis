#!/usr/bin/env python3
"""Generate and compare TFT engine golden fixtures (tft/data/golden).

The fixtures pin the engine's output bit for bit (replayed by TestGolden),
so they are regenerated ONLY after a deliberate model change, in the same
commit, from the live engine:

    python3 -m tft.golden gen [--only fights|cells] [--reason TEXT]
    python3 -m tft.golden compare [--unit Ahri ...] [--driver Ahri ...] [--max-report N] [-v]
"""

import argparse
import hashlib
import itertools
import json
import math
import os
import random
import subprocess
import sys
import time

from tft import sim as tft


GOLDEN_DIR = os.path.join(tft.TFT_DATA_DIR, "golden")
GOLDEN_FIGHTS = os.path.join(GOLDEN_DIR, "fights.json")
RANDOM_PER_CELL = 2
TOP_PER_CELL = 2
CELL_ROWS = 20

RESULT_KEYS = ("killTime", "total", "dps", "rawTotal", "attacks", "casts", "castTimes",
               "breakdown", "left", "t", "aliveTime", "died", "diedAt", "hpLeft", "absorbed",
               "taken", "mitigated", "healed", "shielded", "denied", "allyHeal", "allyShield",
               "ccTime", "hitsTaken", "survivalCapped", "stressAliveTime", "stressCapped")
SHEET_KEYS = ("ad", "ap", "as", "crit", "critMult", "precision", "hp", "armor", "mr",
              "durability", "omnivamp", "form", "manaStart", "manaMax", "physicalEhp", "magicEhp")


def enc(o):
    """Encode an engine value for JSON without losing float precision."""
    if isinstance(o, bool) or o is None or isinstance(o, (int, str)):
        return o
    if isinstance(o, float):
        if math.isnan(o):
            return "nan"
        if math.isinf(o):
            return "inf" if o > 0 else "-inf"
        return o
    if isinstance(o, (list, tuple)):
        return [enc(x) for x in o]
    if isinstance(o, dict):
        return {(k if isinstance(k, str) else str(k)): enc(v) for k, v in o.items()}
    raise TypeError("cannot encode %r (%s)" % (o, type(o).__name__))


def dec(v):
    """Undo enc for non-finite float strings."""
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


def provenance(snap, reason=None):
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, check=True,
                                cwd=tft.BASE_DIR).stdout.strip()
    except Exception:
        commit = None
    try:
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                                    capture_output=True, text=True, check=True,
                                    cwd=tft.BASE_DIR).stdout.strip())
    except Exception:
        dirty = None
    with open(__file__, "rb") as f:
        generator_hash = hashlib.sha256(f.read()).hexdigest()
    return {"sourceHash": tft.SOURCE_HASH, "commit": commit, "worktreeDirty": dirty,
            "engine": {"implementation": "Rust/PyO3", "module": "lol_tft",
                       "sourceHash": tft.engine().SOURCE_HASH},
            "generatorHash": generator_hash, "snapshotHash": snap.hash_inputs(),
            "effectsHash": tft.json_hash({"items": tft.load_item_effects(snap.set_no),
                                         "traits": tft.load_trait_effects(snap.set_no)}),
            "set": snap.set_no, "patch": snap.patch,
            "dummy": tft.dummies_for(snap),
            "tankDummies": {key: tft.dummies_for(snap, threat=key) for key in tft.TANK_THREATS},
            "reason": reason,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def with_history(prov, name):
    """Retain the exact previous artifact identity before replacing it."""
    path = os.path.join(GOLDEN_DIR, name)
    if not os.path.exists(path):
        return prov
    with open(path, "rb") as f:
        raw = f.read()
    previous = json.loads(raw)
    return {**prov, "previousFixture": {"kind": previous["kind"],
                                       "sha256": hashlib.sha256(raw).hexdigest(),
                                       "provenance": previous["provenance"]}}


def legal_combos(snap, pool):
    out = []
    for combo in itertools.combinations_with_replacement(pool, 3):
        if any(snap.items[a]["unique"] and combo.count(a) > 1 for a in set(combo)):
            continue
        out.append(combo)
    return out


def gen_fights(snap, cached):
    item_fx = tft.load_item_effects(snap.set_no)
    trait_fx = tft.load_trait_effects(snap.set_no)
    pool = tft.pool_items(snap, item_fx)
    combos = legal_combos(snap, pool)
    cases = []
    for u in tft.modeled_units(snap):
        contexts, _ = tft.unit_trait_contexts(snap, u, trait_fx)
        slug = tft.unit_slug(u)
        for key, sc in tft.unit_scenarios(u).items():
            cell = cached[(slug, key)]
            dummy = cell["scenario"]["dummy"]
            builds = []
            for row in cell["rows"][:TOP_PER_CELL]:
                builds.append(tuple(snap.item(n)["api"] for n in row["items"]))
            rng = random.Random(f"{slug}/{key}")
            for _ in range(RANDOM_PER_CELL):
                builds.append(combos[rng.randrange(len(combos))])
            if sc["traits"] == "bare":
                builds.append(())
            ctx = contexts[sc["traits"]]
            for items in builds:
                sheet, res = tft.simulate(snap, u, sc["star"], list(items), sc["geometry"],
                                          ctx, dummy, None, item_fx, trait_fx)
                cases.append({
                    "unit": u["api"], "scenario": key, "star": sc["star"],
                    "geometry": sc["geometry"], "traits": sc["traits"],
                    "threat": sc["threat"] if u["objective"] == "tank" else None,
                    "ctxTraits": [[a, c] for a, c in ctx],
                    "items": list(items),
                    "sheet": {k: sheet[k] for k in SHEET_KEYS},
                    "result": {k: v for k, v in res.items()
                               if k not in ("dummyCasts", "dummyAttacks", "probe", "trace")},
                })
    return cases


def gen_cells(snap, cached):
    out = {}
    for (slug, key), cell in sorted(cached.items()):
        out[f"{slug}/{key}"] = {
            "unit": cell["unitApi"], "objective": cell["objective"],
            "driver": cell["scenario"]["driver"],
            "buildsEvaluated": cell["buildsEvaluated"],
            "rows": cell["rows"][:CELL_ROWS],
        }
    return out


def cmd_gen(args):
    snap = tft.load_snapshot()
    paths = tft.cell_paths(snap)
    cached = {}
    for (slug, key), p in paths.items():
        if not os.path.exists(p):
            sys.exit(f"cell {slug}/{key} is not cached for the current code and data; "
                     "run `cli.py tft warm` first")
        with open(p) as f:
            cell = json.load(f)
        cell.pop("coreAnalysis", None)
        cell["rows"] = cell["rows"][:max(CELL_ROWS, TOP_PER_CELL)]
        cached[(slug, key)] = cell
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    prov = provenance(snap, args.reason)
    if args.only in (None, "fights"):
        t0 = time.time()
        cases = gen_fights(snap, cached)
        fight_prov = with_history(prov, "fights.json")
        with open(GOLDEN_FIGHTS, "w") as f:
            json.dump({"kind": "tft-fights", "provenance": fight_prov, "cases": enc(cases)},
                      f, separators=(",", ":"))
        print(f"fights.json: {len(cases)} fights in {time.time() - t0:.1f}s")
    if args.only in (None, "cells"):
        cells = gen_cells(snap, cached)
        cell_prov = with_history(prov, "cells.json")
        with open(os.path.join(GOLDEN_DIR, "cells.json"), "w") as f:
            json.dump({"kind": "tft-cells", "provenance": cell_prov, "cells": cells},
                      f, separators=(",", ":"))
        print(f"cells.json: {len(cells)} cells, {CELL_ROWS} rows each")


def cmd_compare(args):
    with open(GOLDEN_FIGHTS) as f:
        fixture = json.load(f)
    prov = fixture["provenance"]
    snap = tft.load_snapshot(prov["set"], prov["patch"])
    item_fx = tft.load_item_effects(snap.set_no)
    trait_fx = tft.load_trait_effects(snap.set_no)
    dummy = prov.get("dummy", tft.dummies_for(snap))
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
        case_dummy = prov.get("tankDummies", {}).get(case.get("threat"), dummy)
        stats = per_unit.setdefault(api, [0, 0])
        try:
            sheet, res = tft.simulate(snap, unit, case["star"], case["items"], case["geometry"],
                                      [tuple(x) for x in case["ctxTraits"]], case_dummy, None,
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("gen", help="regenerate golden fights.json and/or cells.json")
    p_gen.add_argument("--only", choices=("fights", "cells"))
    p_gen.add_argument("--reason", help="deliberate model change recorded in fixture provenance")
    p_gen.set_defaults(fn=cmd_gen)

    p_cmp = sub.add_parser("compare", help="replay golden fights and diff against recorded benchmark")
    p_cmp.add_argument("--unit", nargs="*", help="unit names or api names to check (default: all)")
    p_cmp.add_argument("--driver", nargs="*", help="driver names to check")
    p_cmp.add_argument("--max-report", type=int, default=12)
    p_cmp.add_argument("-v", "--verbose", action="store_true")
    p_cmp.set_defaults(fn=cmd_compare)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
