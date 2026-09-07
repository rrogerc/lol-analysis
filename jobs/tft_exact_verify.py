#!/usr/bin/env python3
"""Independent, exact TFT optimization verification; never updates game fixtures.

Capture uses an explicitly selected source tree/extension and writes a separate
corpus. Replay compares complete native results, float bits and ordered traces.
Benchmark times identical frozen inputs on one pinned CPU, without a profiler.

Examples (run before and after in separate processes):
  python3 jobs/tft_exact_verify.py capture --root /tmp/frozen --out /tmp/corpus
  python3 jobs/tft_exact_verify.py replay --root . --corpus /tmp/corpus --api prepared
  python3 jobs/tft_exact_verify.py benchmark --root /tmp/frozen --corpus /tmp/corpus --out /tmp/before.json
  python3 jobs/tft_exact_verify.py artifacts --before /tmp/frozen/.cache/tft-comps --after .cache/tft-comps --revision REV --out /tmp/artifacts.json

The corpus includes all current drivers, paired formations/initiatives, existing
mechanic regressions, published boards and legal replacements, all golden fight
inputs, and native top-20 enumeration results for every golden cell. It is not a
new game-math baseline: the original golden files remain untouched.
"""
import argparse
from collections import Counter
from copy import deepcopy
import gzip
import hashlib
import importlib
import importlib.util
import io
from itertools import islice
import json
import os
from pathlib import Path
import re
import statistics
import struct
import sys
import time
import unittest


VERSION = "tft-exact-parity-v1"
COMPACT_OMITTED = {"allies", "enemies", "damageHealingFromProcs", "postDeathAllyHealing",
                   "combatModel", "modelAssumptions", "trace"}
MECHANIC_MODULES = (
    "test_tft_symmetric", "test_tft_symmetric_regressions", "test_tft_team_engine",
    "test_tft_scuttlecrab", "test_tft_azir", "test_tft_murkwolf",
)


def freeze(value):
    """JSON-safe values with exact binary64 and tuple/list distinctions."""
    if type(value) is float:
        return {"$float64": struct.pack(">d", value).hex()}
    if type(value) is tuple:
        return {"$tuple": [freeze(v) for v in value]}
    if type(value) is list:
        return [freeze(v) for v in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("native result dictionaries must use string keys")
        return {key: freeze(v) for key, v in value.items()}
    if value is None or type(value) in (bool, int, str):
        return value
    raise TypeError(f"unsupported native value {type(value).__name__}")


def thaw(value):
    if type(value) is dict:
        if set(value) == {"$float64"}:
            return struct.unpack(">d", bytes.fromhex(value["$float64"]))[0]
        if set(value) == {"$tuple"}:
            return tuple(thaw(v) for v in value["$tuple"])
        return {key: thaw(v) for key, v in value.items()}
    if type(value) is list:
        return [thaw(v) for v in value]
    return value


def canonical(value):
    return json.dumps(freeze(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def differences(before, after, path="$", limit=12):
    """Report exact typed differences, retaining sequence/event order."""
    found = []

    def walk(a, b, here):
        if len(found) >= limit:
            return
        if type(a) is not type(b):
            found.append({"path": here, "before": repr(a), "after": repr(b), "reason": "type"})
        elif type(a) is float:
            ab, bb = struct.pack(">d", a).hex(), struct.pack(">d", b).hex()
            if ab != bb:
                found.append({"path": here, "before": repr(a), "after": repr(b),
                              "beforeBits": ab, "afterBits": bb, "reason": "float64"})
        elif type(a) is dict:
            for key in sorted(set(a) | set(b)):
                if key not in a or key not in b:
                    found.append({"path": f"{here}.{key}", "reason": "missing key",
                                  "before": key in a, "after": key in b})
                else:
                    walk(a[key], b[key], f"{here}.{key}")
                if len(found) >= limit:
                    break
        elif type(a) in (list, tuple):
            if len(a) != len(b):
                found.append({"path": here, "before": len(a), "after": len(b), "reason": "length"})
            for index, (left, right) in enumerate(zip(a, b)):
                walk(left, right, f"{here}[{index}]")
                if len(found) >= limit:
                    break
        elif a != b:
            found.append({"path": here, "before": repr(a), "after": repr(b), "reason": "value"})

    walk(before, after, path)
    return found


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def runtime(root, extension=None):
    """Import only the requested tree; do not install or replace an extension."""
    root = Path(root).resolve()
    if "tft" in sys.modules or "lol_tft" in sys.modules:
        raise RuntimeError("each engine/tree must be checked in a fresh process")
    sys.path.insert(0, str(root))
    if extension:
        spec = importlib.util.spec_from_file_location("lol_tft", Path(extension).resolve())
        module = importlib.util.module_from_spec(spec)
        sys.modules["lol_tft"] = module
        spec.loader.exec_module(module)
    tft = importlib.import_module("tft")
    if Path(tft.__file__).resolve().parent != root:
        raise RuntimeError("wrong TFT source tree loaded")
    engine = tft.engine()
    return tft, engine


def pin_cpu(cpu):
    pin_cpus({cpu})


def pin_cpus(cpus):
    cpus = set(cpus)
    allowed = os.sched_getaffinity(0)
    if not cpus or not cpus <= allowed:
        raise RuntimeError(f"CPUs {sorted(cpus)} unavailable; allowed CPUs: {sorted(allowed)}")
    os.sched_setaffinity(0, cpus)
    if os.sched_getaffinity(0) != cpus:
        raise RuntimeError("CPU affinity did not stick")


class CorpusWriter:
    def __init__(self, directory, engine):
        self.directory, self.engine = Path(directory), engine
        self.directory.mkdir(parents=True, exist_ok=True)
        self.files, self.counts, self.seen = {}, Counter(), set()
        self.events, self.drivers = Counter(), set()
        self.functions = {api: getattr(engine, api) for api in ("simulate", "simulate_team", "simulate_match", "run_cell")}

    def add(self, group, identity, api, spec, *, trace=True, tags=(), output=None, top=20):
        # Normalize input sequences through JSON before BOTH baseline and replay.
        spec = json.loads(json.dumps(spec, allow_nan=False))
        key = digest((api, spec, trace, top))
        if group == "mechanics" and key in self.seen:
            return
        self.seen.add(key)
        if output is None:
            output = self.functions[api](spec, top, 1) if api == "run_cell" else self.functions[api](spec, trace)
        if group not in self.files:
            self.files[group] = gzip.open(self.directory / f"{group}.jsonl.gz", "wt", compresslevel=1)
        row = {"id": identity, "api": api, "spec": spec, "trace": trace,
               "top": top, "tags": list(tags), "expected": freeze(output)}
        self.files[group].write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")
        self.counts[group] += 1
        result = output[1] if api == "simulate" else output
        if isinstance(result, dict):
            for event in result.get("trace", []):
                self.events[event["kind"] if isinstance(event, dict) else event[1]] += 1
        for side in ("allies", "enemies"):
            for actor in spec.get(side, []):
                if "spec" in actor:
                    self.drivers.add(actor["spec"]["driver"])

    def close(self):
        for handle in self.files.values():
            handle.close()
        return {name: {"count": self.counts[name], "sha256": file_hash(self.directory / f"{name}.jsonl.gz")}
                for name in sorted(self.files)}


def capture_mechanics(writer):
    originals = {api: getattr(writer.engine, api) for api in ("simulate", "simulate_team", "simulate_match")}
    captured = Counter()

    def wrapped(api):
        def call(spec, trace=False):
            result = originals[api](spec, trace)
            captured[api] += 1
            # Always retain trace as well as the complete result for successful calls.
            writer.add("mechanics", f"mechanic/{api}/{captured[api]}", api, spec,
                       tags=("existing-regression",), trace=True)
            return result
        return call

    try:
        for api in originals:
            setattr(writer.engine, api, wrapped(api))
        suite = unittest.defaultTestLoader.loadTestsFromNames(MECHANIC_MODULES)
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, verbosity=1).run(suite)
        if not result.wasSuccessful():
            raise RuntimeError("frozen mechanism tests failed:\n" + output.getvalue())
        return {"tests": result.testsRun, "skipped": len(result.skipped), "calls": dict(captured)}
    finally:
        for api, original in originals.items():
            setattr(writer.engine, api, original)


def capture_drivers(writer, tft, snap):
    units = tft.modeled_units(snap)
    for unit in units:
        for geometry in ("spread", "clump"):
            spec = tft.cell_spec(snap, unit, 2, geometry, [], {"slots": [], "targetDebuffs": {}, "enemyDebuffs": {}},
                                 18.0, True)
            # Synthetic endurance/full opening mana exercise each real ability;
            # these are coverage fixtures, not altered live game inputs.
            for kit in spec["kits"].values():
                kit["stats"]["hp"] *= 10.0
                kit["hpStar"] *= 10.0
                kit["stats"]["initialMana"] = kit["stats"]["mana"]
            actor = {"spec": spec, "frontline": True, "lane": 3, "priority": 0}
            for initiative in (0, 1):
                writer.add("drivers", f"driver/{unit['api']}/{geometry}/i{initiative}", "simulate_match",
                           {"duration": 18.0, "geometry": geometry, "initiative": initiative,
                            "allies": [actor], "enemies": [actor]},
                           tags=("all-drivers", "synthetic-endurance", geometry, f"initiative-{initiative}"))
    return len(units)


def board_inputs(tft, snap, row):
    traits = importlib.import_module("tft_comp_traits")
    members = [{"api": unit["api"], "star": unit["star"]} for unit in row["units"]]
    by_slug = {unit["slug"]: unit["api"] for unit in row["units"]}
    carry, tank = by_slug[row["mainCarry"]], by_slug[row["mainTank"]]
    alpha = row.get("alphaHolder")
    alpha = by_slug.get(alpha, alpha)
    selected = {unit["api"]: {"items": tuple(unit["itemApis"]), "count": len(unit["itemApis"]),
                              "alpha": unit["api"] == alpha} for unit in row["units"]}
    resolved = traits.resolve_board_traits(snap, members, alpha)
    return members, resolved["effects"], selected, carry, tank


def published_rows(directory):
    """Fixed coverage independent of future optimized ranking or timings."""
    for path in sorted(Path(directory).glob("*.json")):
        artifact = json.loads(path.read_text())
        picked = []
        for budget, structure in ((6, "single"), (9, "duoCarry"), (9, "duoTank"), (12, "both")):
            rows = artifact["results"][str(budget)][structure]
            if rows:
                picked.append(rows[0])
        scuttle = next((row for groups in artifact["results"].values() for rows in groups.values() for row in rows
                        if any(unit["name"] == "Scuttlecrab" for unit in row["units"])), None)
        if scuttle and all(row["id"] != scuttle["id"] for row in picked):
            picked.append(scuttle)
        for row in picked:
            yield artifact, row


def capture_published(writer, tft, snap, root):
    team = importlib.import_module("tft_team")
    item_module = importlib.import_module("tft_comp_items")
    boards = []
    for artifact, row in published_rows(Path(root) / ".cache/tft-comps"):
        members, effects, selected, carry, tank = board_inputs(tft, snap, row)
        evaluator = team.Evaluator(snap, artifact["geometry"])
        board_id = f"{artifact['key']}/{row['itemCount']}/{row['structure']}/{row['id']}"
        boards.append({"id": board_id, "geometry": artifact["geometry"], "row": row,
                       "members": members, "effects": effects, "selected": selected, "carry": carry, "tank": tank})
        search = item_module.ItemSearch(snap, evaluator, members, effects, carry, tank, row["structure"])
        search.budget = row["itemCount"]
        search.alpha_count = sum(bool(option["alpha"]) for option in selected.values())
        swaps = []
        # Deterministic legal replacements for both main holders, preferably a
        # utility item so the trace exercises changed targeting/procs as well.
        for api, preferred in ((carry, "DA_RedBuff"), (tank, "DA_IonicSpark")):
            for replacement in (preferred, *search.pool):
                if replacement not in search.pool or replacement in selected[api]["items"]:
                    continue
                changed = search.changed(selected, api, (replacement, *selected[api]["items"][1:]))
                if search.legal(changed):
                    swaps.append((f"swap-{api}-{replacement}", changed))
                    break
        for allocation_name, allocation in [("published", selected), *swaps]:
            for split, policy in (("search", "broad"), ("validation", "broad"), ("validation", "restricted")):
                # Swaps include the entire search pass. Held-out policy coverage
                # belongs to the unchanged published board only.
                if allocation_name != "published" and split != "search":
                    continue
                allies = evaluator.allies(members, effects, allocation, carry, tank)
                for encounter in evaluator.encounters(row["itemCount"], split):
                    spec = {"duration": team.DURATION, "geometry": artifact["geometry"],
                            "allies": allies, "enemies": encounter["enemies"], "initiative": encounter["initiative"],
                            "damageHealingFromProcs": policy == "broad", "postDeathAllyHealing": policy == "broad",
                            "burnWound": tft.tank_debuffs(snap)["wound"]}
                    writer.add("published", f"{board_id}/{allocation_name}/{split}/{policy}/{encounter['label']['key']}",
                               "simulate_match", spec, tags=(allocation_name, split, policy, artifact["key"]))
    save_json(writer.directory / "replacement-boards.json", boards)
    return len(boards)


def capture_goldens(writer, tft, snap):
    item_fx, trait_fx = tft.load_item_effects(snap.set_no), tft.load_trait_effects(snap.set_no)
    directory = Path(tft.TFT_DATA_DIR) / "golden"
    fixture = json.loads((directory / "fights.json").read_text())
    for index, case in enumerate(fixture["cases"]):
        dummy = fixture["provenance"].get("tankDummies", {}).get(case.get("threat"), fixture["provenance"]["dummy"])
        spec = tft.cell_spec(snap, snap.units[case["unit"]], case["star"], case["geometry"],
                             [tuple(v) for v in case["ctxTraits"]], dummy, item_fx=item_fx, trait_fx=trait_fx,
                             items=case["items"])
        writer.add("golden-fights", f"golden-fight/{index}/{case['unit']}/{case['scenario']}", "simulate", spec,
                   trace=False, tags=("golden-fight",))
    fixture = json.loads((directory / "cells.json").read_text())
    pool = tft.pool_items(snap, item_fx)
    for index, (key, cell) in enumerate(sorted(fixture["cells"].items())):
        unit = snap.units[cell["unit"]]
        scenario = tft.SCENARIOS[key.split("/")[1]]
        dummy = (fixture["provenance"]["tankDummies"][scenario["threat"]]
                 if unit["objective"] == "tank" else fixture["provenance"]["dummy"])
        contexts, _ = tft.unit_trait_contexts(snap, unit, trait_fx)
        spec = tft.cell_spec(snap, unit, scenario["star"], scenario["geometry"], contexts[scenario["traits"]],
                             dummy, item_fx=item_fx, trait_fx=trait_fx, pool=pool)
        result = writer.functions["run_cell"](spec, len(cell["rows"]), 1)
        rows = [(tuple(pool[i] for i in indices), sheet, fight) for indices, sheet, fight in result[1]]
        if result[0] != cell["buildsEvaluated"] or tft.cell_rows(snap, unit, rows, len(cell["rows"])) != cell["rows"]:
            raise RuntimeError(f"frozen engine does not match original golden cell {key}")
        writer.add("golden-cells", f"golden-cell/{key}", "run_cell", spec, trace=False,
                   tags=("golden-cell",), output=result, top=len(cell["rows"]))
        if (index + 1) % 100 == 0:
            print(f"captured {index + 1}/{len(fixture['cells'])} golden cells", flush=True)
    return {"fights": len(json.loads((directory / "fights.json").read_text())["cases"]), "cells": len(fixture["cells"])}


def capture(args):
    pin_cpu(args.cpu)
    tft, engine = runtime(args.root, args.engine)
    if Path(args.out, "manifest.json").exists():
        raise RuntimeError("refusing to overwrite an existing exact corpus")
    writer = CorpusWriter(args.out, engine)
    started = time.perf_counter()
    snap = tft.load_snapshot()
    try:
        mechanics = capture_mechanics(writer)
        print(f"captured mechanic regressions: {mechanics}", flush=True)
        # Some existing regression helpers deliberately mutate nested extras
        # referenced by their module-level SNAP. Never let those synthetic stats
        # leak into the real published or golden workloads captured afterward.
        snap = tft.Snapshot(snap.set_no, snap.patch)
        driver_count = capture_drivers(writer, tft, snap)
        boards = capture_published(writer, tft, snap, args.root)
        for group in ("mechanics", "drivers", "published"):
            writer.files[group].close()
        print(f"captured {driver_count} drivers and {boards} published boards with swaps", flush=True)
        goldens = capture_goldens(writer, tft, snap)
    finally:
        files = writer.close()
    # Freeze game inputs independently of executable/source revision hashes.
    data = {str(path.relative_to(args.root)): file_hash(path)
            for path in sorted(Path(args.root, "data/tft").rglob("*.json"))}
    manifest = {"version": VERSION, "root": str(Path(args.root).resolve()), "engineHash": engine.SOURCE_HASH,
                "binaryHash": file_hash(engine.__file__), "cpu": args.cpu, "files": files,
                "mechanics": mechanics, "driverCount": driver_count, "drivers": sorted(writer.drivers),
                "events": dict(writer.events), "publishedBoards": boards, "goldens": goldens,
                "replacementBoardsHash": file_hash(Path(args.out, "replacement-boards.json")),
                "gameInputHashes": data, "captureSeconds": time.perf_counter() - started}
    save_json(Path(args.out, "manifest.json"), manifest)
    print(json.dumps({key: manifest[key] for key in ("engineHash", "files", "driverCount", "publishedBoards", "goldens", "captureSeconds")}, indent=2))


def corpus_rows(directory, groups=None):
    manifest = json.loads(Path(directory, "manifest.json").read_text())
    if manifest["version"] != VERSION:
        raise RuntimeError("unsupported exact corpus version")
    if groups and not set(groups) <= set(manifest["files"]):
        raise ValueError(f"unknown corpus groups: {sorted(set(groups) - set(manifest['files']))}")
    for group, info in manifest["files"].items():
        if groups and group not in groups:
            continue
        path = Path(directory, f"{group}.jsonl.gz")
        if file_hash(path) != info["sha256"]:
            raise RuntimeError(f"corpus integrity failure: {path}")
        count = 0
        with gzip.open(path, "rt") as handle:
            for line in handle:
                count += 1
                row = json.loads(line)
                row["expected"] = thaw(row["expected"])
                yield group, row
        if count != info["count"]:
            raise RuntimeError(f"corpus record count changed: {path}")


def check_game_inputs(directory, root):
    manifest = json.loads(Path(directory, "manifest.json").read_text())
    actual = {str(path.relative_to(root)): file_hash(path)
              for path in sorted(Path(root, "data/tft").rglob("*.json"))}
    delta = differences(manifest["gameInputHashes"], actual)
    if delta:
        raise RuntimeError("game inputs changed during an exact-only optimization: " + json.dumps(delta))


def prepared_specs(engine, specs):
    actors = {}
    result = []
    for spec in specs:
        changed = dict(spec)
        for side in ("allies", "enemies"):
            changed[side] = []
            for actor in spec[side]:
                key = digest(actor)
                if key not in actors:
                    actors[key] = engine.prepare_actor(actor)
                changed[side].append(actors[key])
        result.append(changed)
    return result


def replay(args):
    pin_cpus(args.cpus or [args.cpu])
    if args.workers > len(os.sched_getaffinity(0)):
        raise ValueError("use --cpus to make the requested replay workers available")
    _, engine = runtime(args.root, args.engine)
    check_game_inputs(args.corpus, args.root)
    manifest = json.loads(Path(args.corpus, "manifest.json").read_text())
    expected_counts = {group: info["count"] for group, info in manifest["files"].items()
                       if not args.groups or group in args.groups}
    counts, failures = Counter(), []
    started = time.perf_counter()
    pending = []

    def compare(group, row, actual):
        expected = row["expected"]
        if args.api == "compact" and row["api"] == "simulate_match":
            expected = {key: value for key, value in expected.items() if key not in COMPACT_OMITTED}
        diff = differences(expected, actual)
        counts[group] += 1
        if diff:
            failures.append({"id": row["id"], "differences": diff})
        if counts[group] % 1000 == 0:
            print(f"replayed {group}: {counts[group]}", flush=True)

    def flush():
        if not pending:
            return
        specs = [row["spec"] for _, row in pending]
        if args.api in ("prepared", "compact"):
            specs = prepared_specs(engine, specs)
        compact = args.api == "compact"
        actual = engine.simulate_matches(specs, detail="compact" if compact else "full",
                                         trace=False if compact else pending[0][1]["trace"], workers=args.workers)
        if len(actual) != len(pending):
            raise RuntimeError("batch output count differs from input count")
        for (group, row), result in zip(pending, actual):
            compare(group, row, result)
        pending.clear()

    for group, row in corpus_rows(args.corpus, args.groups):
        api, spec = row["api"], row["spec"]
        if api == "simulate_match" and args.api != "raw":
            if pending and row["trace"] != pending[0][1]["trace"]:
                flush()
            pending.append((group, row))
            if len(pending) >= 32:
                flush()
        else:
            flush()
            actual = engine.run_cell(spec, row["top"], 1) if api == "run_cell" else getattr(engine, api)(spec, row["trace"])
            compare(group, row, actual)
        if len(failures) >= args.max_failures:
            break
    flush()
    complete = dict(counts) == expected_counts
    report = {"version": VERSION, "engineHash": engine.SOURCE_HASH, "binaryHash": file_hash(engine.__file__),
              "api": args.api, "affinity": sorted(os.sched_getaffinity(0)), "workers": args.workers,
              "counts": dict(counts), "expectedCounts": expected_counts, "failures": failures,
              "complete": complete, "passed": not failures and complete, "seconds": time.perf_counter() - started}
    save_json(args.out or Path(args.corpus, f"replay-{args.api}-{engine.SOURCE_HASH[:12]}.json"), report)
    print(json.dumps({key: report[key] for key in ("engineHash", "api", "counts", "passed", "seconds")}, indent=2))
    return int(not report["passed"])


def timed_samples(operation, warmups, samples, loops):
    for _ in range(warmups):
        operation()
    times = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        for _ in range(loops):
            operation()
        times.append((time.perf_counter_ns() - started) / 1e9 / loops)
        print(f"timing sample {len(times)}/{samples}: {times[-1]:.6f}s", flush=True)
    return {"samplesSeconds": times, "medianSeconds": statistics.median(times), "minSeconds": min(times),
            "maxSeconds": max(times), "samples": samples, "loopsPerSample": loops, "warmups": warmups}


def benchmark(args):
    pin_cpu(args.cpu)
    tft, engine = runtime(args.root, args.engine)
    check_game_inputs(args.corpus, args.root)
    rows = list(islice((row for _, row in corpus_rows(args.corpus, {"published"})
                       if "published" in row["tags"] and "search" in row["tags"]), args.fights))
    specs = [row["spec"] for row in rows]
    if not specs:
        raise RuntimeError("no benchmark matches selected")
    report = {"version": VERSION, "engineHash": engine.SOURCE_HASH, "binaryHash": file_hash(engine.__file__),
              "root": str(Path(args.root).resolve()), "cpu": args.cpu, "affinity": sorted(os.sched_getaffinity(0)),
              "profiled": False, "inputHash": digest(specs), "fights": len(specs), "api": args.api,
              "pythonVersion": sys.version, "pythonSourceHash": tft.SOURCE_HASH, "workloads": {},
              "replacementEntryPoint": "ItemSearch.singles from the selected --root; --api controls only native match timing"}
    topology = Path(f"/sys/devices/system/cpu/cpu{args.cpu}/topology/thread_siblings_list")
    report["cpuThreadSiblings"] = topology.read_text().strip() if topology.exists() else None
    if args.api == "raw":
        operation = lambda: [engine.simulate_match(spec, False) for spec in specs]
    elif args.api == "batch":
        operation = lambda: engine.simulate_matches(specs, detail="full", trace=False, workers=1)
    else:
        prepared = prepared_specs(engine, specs)
        detail = "compact" if args.api == "compact" else "full"
        operation = lambda: engine.simulate_matches(prepared, detail=detail, trace=False, workers=1)
        report["workloads"]["prepareAndDeduplicateActors"] = timed_samples(
            lambda: prepared_specs(engine, specs), args.warmups, args.samples, 1)
    report["workloads"]["matches"] = timed_samples(operation, args.warmups, args.samples, args.loops)
    if args.replacements:
        team = importlib.import_module("tft_team")
        items = importlib.import_module("tft_comp_items")
        boards = json.loads(Path(args.corpus, "replacement-boards.json").read_text())
        # One board per formation, nine items, same frozen allocation each time.
        chosen = []
        for geometry in ("spread", "clump"):
            chosen.append(next(board for board in boards if board["geometry"] == geometry and board["row"]["itemCount"] == 9))
        snap = tft.load_snapshot()
        for board in chosen:
            counts = []

            def replacement_pass():
                evaluator = team.Evaluator(snap, board["geometry"])
                search = items.ItemSearch(snap, evaluator, board["members"], board["effects"],
                                          board["carry"], board["tank"], board["row"]["structure"])
                search.budget = board["row"]["itemCount"]
                search.alpha_count = sum(bool(option.get("alpha")) for option in board["selected"].values())
                result = search.singles(board["selected"])
                counts.append(len(result))
                return result

            timing = timed_samples(replacement_pass, args.warmups, args.samples, 1)
            if len(set(counts)) != 1:
                raise RuntimeError("replacement pass changed workload between repetitions")
            timing.update(board=board["id"], boardHash=digest(board), replacements=counts[0])
            report["workloads"][f"replacements-{board['geometry']}"] = timing
    if args.refinement:
        team = importlib.import_module("tft_team")
        items = importlib.import_module("tft_comp_items")
        workload = thaw(json.loads(args.refinement.read_text()))
        job = workload["job"]
        _, carry, tank = job["candidate"]
        refined = job["refined"]
        seeds = [row["selected"] for row in refined["allocations"][str(job["budget"])][job["structure"]]]
        snap, last = tft.load_snapshot(), {}
        input_hash = digest(workload)

        def refinement_pass():
            evaluator = team.Evaluator(snap, workload["geometry"])
            optimizer = items.ItemSearch(snap, evaluator, refined["members"], refined["traits"]["effects"],
                                         carry, tank, job["structure"], job["anchors"])
            selected, _, evidence = optimizer.optimize(seeds)
            result = evaluator.evaluate(refined["members"], refined["traits"]["effects"], selected, carry, tank)
            last.update(result={"selected": selected, "result": result, "itemAnalysis": evidence},
                        stats=dict(optimizer.stats))

        timing = timed_samples(refinement_pass, args.warmups, args.samples, 1)
        if digest(workload) != input_hash:
            raise RuntimeError("refinement mutated its frozen workload")
        timing.update(boardHash=input_hash, resultHash=digest(normalize_artifact(last["result"])),
                      stats=last["stats"])
        result_path = args.out.with_suffix(".refinement.json.gz")
        with gzip.open(result_path, "wt", compresslevel=1) as handle:
            json.dump(freeze(last["result"]), handle, separators=(",", ":"), allow_nan=False)
        timing["resultFile"] = str(result_path)
        report["workloads"]["full-refinement"] = timing
    save_json(args.out, report)
    print(json.dumps(report, indent=2))


def benchmark_comparison(before, after):
    for key in ("inputHash", "cpu", "affinity", "fights"):
        if before[key] != after[key]:
            raise ValueError(f"benchmark {key} differs; timings are not the same workload/environment")
    if before["profiled"] or after["profiled"]:
        raise ValueError("performance comparison requires unprofiled runs")
    comparisons = {}
    for key in sorted(set(before["workloads"]) & set(after["workloads"])):
        left, right = before["workloads"][key], after["workloads"][key]
        for field in ("boardHash", "replacements", "resultHash", "loopsPerSample"):
            if left.get(field) != right.get(field):
                raise ValueError(f"benchmark {key}.{field} workload differs")
        comparisons[key] = {"beforeSeconds": left["medianSeconds"], "afterSeconds": right["medianSeconds"],
                            "speedup": left["medianSeconds"] / right["medianSeconds"],
                            "runtimeReductionPercent": 100.0 * (1.0 - right["medianSeconds"] / left["medianSeconds"]),
                            "beforeRange": [left["minSeconds"], left["maxSeconds"]],
                            "afterRange": [right["minSeconds"], right["maxSeconds"]]}
    return {"version": VERSION, "beforeEngine": before["engineHash"], "afterEngine": after["engineHash"],
            "beforeRoot": before["root"], "afterRoot": after["root"], "cpu": before["cpu"],
            "inputHash": before["inputHash"], "workloads": comparisons}


def benchmarks(args):
    report = benchmark_comparison(json.loads(args.before.read_text()), json.loads(args.after.read_text()))
    save_json(args.out, report)
    print(json.dumps(report, indent=2))


def normalize_artifact(value, path=()):
    """Only operational provenance/counters; combat timings and pool hash stay."""
    if type(value) is dict:
        result = {}
        for key, item in value.items():
            if not path and key in {"revision", "baselineRevision", "computedAt", "computeSeconds"}:
                continue
            if key == "poolRevision":
                continue
            if not path and key == "search":
                result[key] = {"exhaustive": item["exhaustive"]} if "exhaustive" in item else {}
            else:
                result[key] = normalize_artifact(item, (*path, key))
        return result
    if type(value) is list:
        return [normalize_artifact(item, (*path, index)) for index, item in enumerate(value)]
    return value


def artifact_counts(artifact):
    boards = [row for groups in artifact["results"].values() for rows in groups.values() for row in rows]
    records = sum(len(slot["alternatives"]) for row in boards for holder in row["itemAnalysis"]["holders"]
                  for slot in holder["items"])
    return len(boards), records


def composition_index(directory, revision=None):
    entries = {}
    for path in sorted(Path(directory).glob("c[1-4]-*-mixed-*.json")):
        if not re.fullmatch(r"c[1-4]-(spread|clump)-mixed-[0-9a-f]+\.json", path.name):
            continue
        artifact = json.loads(path.read_text())
        if revision and artifact.get("revision") != revision:
            continue
        if "key" not in artifact or not isinstance(artifact.get("results"), dict):
            raise RuntimeError(f"invalid composition artifact shape: {path}")
        key = artifact["key"]
        if not path.name.startswith(key + "-"):
            raise RuntimeError(f"composition artifact key does not match its filename: {path}")
        if key in entries:
            raise RuntimeError(f"multiple artifacts for {key}; select a revision")
        entries[key] = path
    if not entries:
        raise RuntimeError(f"no selected artifacts in {directory}")
    return entries


def artifacts(args):
    before, after = composition_index(args.before), composition_index(args.after, args.revision)
    failures, checked = [], []
    total_boards = total_replacements = 0
    for key in sorted(set(before) | set(after)):
        if key not in before or key not in after:
            failures.append({"key": key, "reason": "missing context", "before": key in before, "after": key in after})
            continue
        old, new = json.loads(before[key].read_text()), json.loads(after[key].read_text())
        boards, replacements = artifact_counts(old)
        total_boards += boards
        total_replacements += replacements
        diff = differences(normalize_artifact(old), normalize_artifact(new))
        record = {"key": key, "boards": boards, "replacements": replacements,
                  "beforeHash": file_hash(before[key]), "afterHash": file_hash(after[key]), "passed": not diff}
        checked.append(record)
        if diff:
            failures.append({"key": key, "differences": diff})
    report = {"version": VERSION, "passed": not failures, "contexts": checked, "boards": total_boards,
              "replacementRecords": total_replacements, "failures": failures,
              "exclusions": ["root revision/baselineRevision", "root computedAt/computeSeconds", "poolRevision",
                             "root search counters, preserving exhaustive"],
              "retained": ["opponentPool.hash", "all methodology", "combat timings", "float bits", "all sequence order"]}
    save_json(args.out, report)
    print(json.dumps(report, indent=2))
    return int(bool(failures))


def cell_index(args):
    tft, engine = runtime(args.root, args.engine)
    snap = tft.load_snapshot()
    paths = tft.cell_paths(snap)
    files = {f"{slug}/{key}": str((args.cache_dir / Path(path).name).resolve() if args.cache_dir else Path(path).resolve())
             for (slug, key), path in sorted(paths.items())}
    report = {"version": VERSION, "engineHash": engine.SOURCE_HASH, "sourceHash": tft.SOURCE_HASH,
              "snapshotRevision": tft.snapshot_revision(snap), "files": files}
    save_json(args.out, report)
    print(json.dumps({"revision": report["snapshotRevision"], "cells": len(files),
                      "present": sum(Path(path).exists() for path in files.values())}))


def normalize_cell_artifact(value):
    # Current cell payloads contain no native/code revision field; filenames
    # carry that provenance. Do not discard diagnostic times inside the cell.
    return {key: item for key, item in value.items() if key not in {"computedAt", "computeSeconds"}}


def cell_artifacts(args):
    before, after = json.loads(args.before.read_text()), json.loads(args.after.read_text())
    failures, checked = [], []
    rows = cores = core_stats = 0
    for key in sorted(set(before["files"]) | set(after["files"])):
        if key not in before["files"] or key not in after["files"]:
            failures.append({"cell": key, "reason": "missing indexed cell"})
            continue
        left, right = Path(before["files"][key]), Path(after["files"][key])
        if not left.exists() or not right.exists():
            failures.append({"cell": key, "reason": "missing artifact", "before": left.exists(), "after": right.exists()})
            continue
        old, new = json.loads(left.read_text()), json.loads(right.read_text())
        delta = differences(normalize_cell_artifact(old), normalize_cell_artifact(new))
        rows += len(old["rows"])
        cores += len(old["coreAnalysis"].get("candidates", []))
        core_stats += len(old["coreAnalysis"].get("coreStats", []))
        checked.append({"cell": key, "beforeHash": file_hash(left), "afterHash": file_hash(right), "passed": not delta})
        if delta:
            failures.append({"cell": key, "differences": delta})
        if len(checked) % 250 == 0:
            print(f"compared {len(checked)}/{len(before['files'])} complete baseline artifacts", flush=True)
    report = {"version": VERSION, "passed": bool(checked) and not failures, "cells": len(checked),
              "rows": rows, "coreCandidates": cores, "coreStats": core_stats, "failures": failures,
              "beforeRevision": before["snapshotRevision"], "afterRevision": after["snapshotRevision"],
              "exclusions": ["root computedAt", "root computeSeconds"], "artifacts": checked}
    save_json(args.out, report)
    print(json.dumps({key: value for key, value in report.items() if key != "artifacts"}, indent=2))
    return int(not report["passed"])


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "replay", "benchmark"):
        sub = commands.add_parser(name)
        sub.add_argument("--root", required=True, type=Path)
        sub.add_argument("--engine", type=Path, help="explicit extension file; never installed")
        sub.add_argument("--cpu", type=int, default=2)
        if name == "capture":
            sub.add_argument("--out", required=True, type=Path)
        else:
            sub.add_argument("--corpus", required=True, type=Path)
            sub.add_argument("--out", type=Path, required=name == "benchmark")
            sub.add_argument("--api", choices=("raw", "batch", "prepared", "compact"), default="raw")
        if name == "replay":
            sub.add_argument("--groups", nargs="*", help="default: every corpus group")
            sub.add_argument("--max-failures", type=int, default=12)
            sub.add_argument("--workers", type=int, default=1, help="batch worker count")
            sub.add_argument("--cpus", type=int, nargs="+", help="explicit replay CPU set for multiple workers")
        if name == "benchmark":
            sub.add_argument("--fights", type=int, default=240)
            sub.add_argument("--samples", type=int, default=5)
            sub.add_argument("--warmups", type=int, default=1)
            sub.add_argument("--loops", type=int, default=2)
            sub.add_argument("--replacements", action="store_true")
            sub.add_argument("--refinement", type=Path, help="frozen tagged-JSON full item-refinement workload")
        sub.set_defaults(run=globals()[name])
    sub = commands.add_parser("artifacts")
    sub.add_argument("--before", type=Path, required=True)
    sub.add_argument("--after", type=Path, required=True)
    sub.add_argument("--revision")
    sub.add_argument("--out", type=Path, required=True)
    sub.set_defaults(run=artifacts)
    sub = commands.add_parser("benchmarks")
    sub.add_argument("--before", type=Path, required=True)
    sub.add_argument("--after", type=Path, required=True)
    sub.add_argument("--out", type=Path, required=True)
    sub.set_defaults(run=benchmarks)
    sub = commands.add_parser("cell-index")
    sub.add_argument("--root", type=Path, required=True)
    sub.add_argument("--engine", type=Path)
    sub.add_argument("--cache-dir", type=Path, help="map expected basenames into a preserved cache directory")
    sub.add_argument("--out", type=Path, required=True)
    sub.set_defaults(run=cell_index)
    sub = commands.add_parser("cell-artifacts")
    sub.add_argument("--before", type=Path, required=True, help="frozen cell-index JSON")
    sub.add_argument("--after", type=Path, required=True, help="candidate cell-index JSON")
    sub.add_argument("--out", type=Path, required=True)
    sub.set_defaults(run=cell_artifacts)
    args = parser.parse_args()
    return args.run(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
