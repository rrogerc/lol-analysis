#!/usr/bin/env python3
"""Regenerate the TFT engine's golden fixtures (data/tft/golden).

The fixtures pin the engine's output bit for bit — test_tft.TestGolden
replays them — so they are regenerated ONLY after a deliberate model change,
in the same commit, from the live engine:

    python3 jobs/gen_tft_golden.py [--only fights|cells]

fights.json: for every modeled unit and every one of its scenario cells, a
few builds through `tft.simulate` — the cell's two best cached builds, two
drawn with a fixed seed from every legal 3-item multiset, and (in the bare
trait context) the empty build — with the opening sheet numbers and the
unrounded result. cells.json: the top rows of every cached cell as the
dashboard shows them (rounded), so the enumeration's order and the row
formatting are pinned as well. Both files need a fully warm .cache/tft for
the current code and data (the cells come from there).
"""
import argparse
import itertools
import json
import math
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tft  # noqa: E402

GOLDEN_DIR = os.path.join(tft.TFT_DATA_DIR, "golden")
RANDOM_PER_CELL = 2
TOP_PER_CELL = 2
CELL_ROWS = 20


def enc(o):
    """An engine value made JSON-safe without losing bits: floats pass
    through (json writes repr, which round-trips) except inf/nan, which
    become strings; tuples become lists."""
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


def provenance(snap):
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, check=True,
                                cwd=tft.BASE_DIR).stdout.strip()
    except Exception:
        commit = None
    return {"sourceHash": tft.SOURCE_HASH, "commit": commit,
            "set": snap.set_no, "patch": snap.patch,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


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
    dummy = tft.dummies_for(snap)
    pool = tft.pool_items(snap, item_fx)
    combos = legal_combos(snap, pool)
    cases = []
    for u in tft.modeled_units(snap):
        contexts, _ = tft.unit_trait_contexts(snap, u, trait_fx)
        slug = tft.unit_slug(u)
        for key, sc in tft.unit_scenarios(u).items():
            cell = cached[(slug, key)]
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
                o = sheet.opening
                cases.append({
                    "unit": u["api"], "scenario": key, "star": sc["star"],
                    "geometry": sc["geometry"], "traits": sc["traits"],
                    "ctxTraits": [[a, c] for a, c in ctx],
                    "items": list(items),
                    "sheet": {"ad": o["ad"], "ap": o["ap"], "as": o["as"],
                              "crit": sheet.crit_chance, "critMult": sheet.crit_mult,
                              "precision": sheet.precision, "hp": o["hp"],
                              "armor": o["armor"], "mr": o["mr"],
                              "durability": sheet.durability, "omnivamp": sheet.omnivamp,
                              "form": sheet.form, "manaStart": sheet.mana_start,
                              "manaMax": sheet.mana_max},
                    "result": res,
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", choices=("fights", "cells"))
    args = ap.parse_args()
    snap = tft.load_snapshot()
    paths = tft.cell_paths(snap)
    cached = {}
    for (slug, key), p in paths.items():
        if not os.path.exists(p):
            sys.exit(f"cell {slug}/{key} is not cached for the current code and data — "
                     "run `lol.py tft warm` first")
        with open(p) as f:
            cached[(slug, key)] = json.load(f)
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    prov = provenance(snap)
    if args.only in (None, "fights"):
        t0 = time.time()
        cases = gen_fights(snap, cached)
        with open(os.path.join(GOLDEN_DIR, "fights.json"), "w") as f:
            json.dump({"kind": "tft-fights", "provenance": prov, "cases": enc(cases)},
                      f, separators=(",", ":"))
        print(f"fights.json: {len(cases)} fights in {time.time() - t0:.1f}s")
    if args.only in (None, "cells"):
        cells = gen_cells(snap, cached)
        with open(os.path.join(GOLDEN_DIR, "cells.json"), "w") as f:
            json.dump({"kind": "tft-cells", "provenance": prov, "cells": cells},
                      f, separators=(",", ":"))
        print(f"cells.json: {len(cells)} cells, {CELL_ROWS} rows each")


if __name__ == "__main__":
    main()
