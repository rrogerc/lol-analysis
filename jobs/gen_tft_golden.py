#!/usr/bin/env python3
"""Regenerate the TFT engine's golden fixtures (data/tft/golden).

The fixtures pin the engine's output bit for bit — test_tft.TestGolden
replays them — so they are regenerated ONLY after a deliberate model change,
in the same commit, from the live engine:

    python3 jobs/gen_tft_golden.py [--only fights|cells] [--reason TEXT]

fights.json: for every modeled unit and every one of its scenario cells, a
few builds through `tft.simulate` — the cell's two best cached builds, two
drawn with a fixed seed from every legal 3-item multiset, and (in the bare
trait context) the empty build — with the opening sheet numbers and the
unrounded result. cells.json: the top rows of every cached cell as the
dashboard shows them (rounded), so the enumeration's order and the row
formatting are pinned as well. Both files need a fully warm .cache/tft for
the current code and data (the cells come from there). Each generation
records the compiled engine, exact dummy specification and input hashes,
plus the previous fixture's hash/provenance. The initial generation pinned
the pre-Rust Python engine; deliberate model changes establish new compiled
engine benchmarks without claiming they are still the historical output.
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
                    "sheet": {k: sheet[k] for k in ("ad", "ap", "as", "crit", "critMult", "precision",
                                                    "hp", "armor", "mr", "durability", "omnivamp",
                                                    "form", "manaStart", "manaMax", "physicalEhp", "magicEhp")},
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", choices=("fights", "cells"))
    ap.add_argument("--reason", help="deliberate model change recorded in fixture provenance")
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
    prov = provenance(snap, args.reason)
    if args.only in (None, "fights"):
        t0 = time.time()
        cases = gen_fights(snap, cached)
        fight_prov = with_history(prov, "fights.json")
        with open(os.path.join(GOLDEN_DIR, "fights.json"), "w") as f:
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


if __name__ == "__main__":
    main()
