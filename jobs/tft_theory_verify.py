#!/usr/bin/env python3
"""Freeze/replay theoretical capacity outputs and time unchanged item searches.

Run from the checkout. Capture records the original/reference evaluator before a
native rewrite; replay checks every numeric and descriptive result field except
the expected source revision change. Benchmark runs complete production ItemSearch
with the same two seeds, item pool, moves, interaction limit and convergence bound.
No profiler or reduced search is used. Cold and repeated-warm passes are separate.

Examples:
  python3 jobs/tft_theory_verify.py capture --out /tmp/theory-corpus.json
  python3 jobs/tft_theory_verify.py replay --corpus /tmp/theory-corpus.json
  python3 jobs/tft_theory_verify.py benchmark --out /tmp/theory-benchmark.json --cpu 2
"""
from copy import deepcopy
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tft
import tft_theory as theory
from tft_comp_items import ItemSearch, arrangement, identity
from tft_comp_traits import resolve_board_traits

# Inputs sampled from the reported board, its actual theoretical refinement,
# previously saved c1–c4 dense boards/cap, and explicit Elder Alpha alternatives.
# Expected outputs are captured separately, before replacing the evaluator.
FIXTURES = [{'name': 'reported',
  'carry': 'TFT18_Sivir',
  'tank': 'TFT18_Malphite',
  'units': [('TFT18_Brambleback', 2, [], False),
            ('TFT18_Diana', 2, [], False),
            ('TFT18_KogMaw', 2, [], False),
            ('TFT18_Malphite', 2, ['DA_LastWhisper', 'DA_RedBuff'], False),
            ('TFT18_Morgana', 2, [], False),
            ('TFT18_Nidalee', 2, ['DA_RabadonsDeathcap', 'DA_SpearOfShojin'], False),
            ('TFT18_Sentinel', 2, ['DA_GiantSlayer', 'DA_RedBuff'], False),
            ('TFT18_Sivir', 2, ['DA_GiantSlayer', 'DA_InfinityEdge', 'DA_LastWhisper'], False)]},
 {'name': 'defensive',
  'carry': 'TFT18_Sivir',
  'tank': 'TFT18_Malphite',
  'units': [('TFT18_Brambleback', 2, [], False),
            ('TFT18_Diana', 2, [], False),
            ('TFT18_KogMaw', 2, [], False),
            ('TFT18_Malphite', 2, ['DA_WarmogsArmor', 'DA_GargoyleStoneplate'], False),
            ('TFT18_Morgana', 2, [], False),
            ('TFT18_Nidalee', 2, ['DA_RabadonsDeathcap', 'DA_SpearOfShojin'], False),
            ('TFT18_Sentinel', 2, ['DA_WarmogsArmor', 'DA_GargoyleStoneplate'], False),
            ('TFT18_Sivir', 2, ['DA_GiantSlayer', 'DA_InfinityEdge', 'DA_LastWhisper'], False)]},
 {'name': 'refined',
  'carry': 'TFT18_Sivir',
  'tank': 'TFT18_Malphite',
  'units': [('TFT18_Brambleback', 2, [], False),
            ('TFT18_Diana', 2, [], False),
            ('TFT18_KogMaw', 2, [], False),
            ('TFT18_Malphite', 2, ['DA_LastWhisper', 'DA_RedBuff'], False),
            ('TFT18_Morgana', 2, [], False),
            ('TFT18_Nidalee', 2, ['DA_GiantSlayer', 'DA_KrakensFury'], False),
            ('TFT18_Sentinel', 2, ['DA_BrambleVest', 'DA_IonicSpark'], False),
            ('TFT18_Sivir', 2, ['DA_GiantSlayer', 'DA_GiantSlayer', 'DA_InfinityEdge'], False)]},
 {'name': 'c1-dense12',
  'carry': 'TFT18_Xayah',
  'tank': 'TFT18_Yorick',
  'units': [('TFT18_Akali', 2, [], False),
            ('TFT18_Amumu', 2, ['DA_GargoyleStoneplate', 'DA_WarmogsArmor', 'DA_WarmogsArmor'], False),
            ('TFT18_Diana', 2, [], False),
            ('TFT18_MamaBeak', 2, ['DA_GiantSlayer', 'DA_InfinityEdge', 'DA_LastWhisper'], False),
            ('TFT18_MasterYi', 2, [], False),
            ('TFT18_Varus', 2, [], False),
            ('TFT18_Xayah', 3, ['DA_GiantSlayer', 'DA_KrakensFury', 'DA_LastWhisper'], False),
            ('TFT18_Yorick', 3, ['DA_GargoyleStoneplate', 'DA_GargoyleStoneplate', 'DA_WarmogsArmor'], False)]},
 {'name': 'c2-dense12',
  'carry': 'TFT18_LeBlanc',
  'tank': 'TFT18_Scuttlecrab',
  'units': [('TFT18_Akali', 2, [], False),
            ('TFT18_Amumu', 2, ['DA_GargoyleStoneplate', 'DA_WarmogsArmor', 'DA_WarmogsArmor'], False),
            ('TFT18_Brambleback', 2, ['DA_GiantSlayer', 'DA_GuinsoosRageblade', 'DA_GuinsoosRageblade'], True),
            ('TFT18_Gromp', 2, [], False),
            ('TFT18_Karma', 2, [], False),
            ('TFT18_LeBlanc', 3, ['DA_JeweledGauntlet', 'DA_NashorsTooth', 'DA_VoidStaff'], False),
            ('TFT18_Scuttlecrab', 3, ['DA_Crownguard', 'DA_GargoyleStoneplate', 'DA_ProtectorsVow'], False),
            ('TFT18_Shen', 2, [], False)]},
 {'name': 'c3-dense12',
  'carry': 'TFT18_Diana',
  'tank': 'TFT18_Hecarim',
  'units': [('TFT18_Alune', 1, [], False),
            ('TFT18_Aphelios', 2, [], False),
            ('TFT18_Brambleback', 2, ['DA_GuinsoosRageblade', 'DA_GuinsoosRageblade', 'DA_LastWhisper'], False),
            ('TFT18_Diana', 3, ['DA_RabadonsDeathcap', 'DA_SpearOfShojin', 'DA_VoidStaff'], False),
            ('TFT18_Fiddlesticks', 2, [], False),
            ('TFT18_Hecarim', 3, ['DA_Crownguard', 'DA_GargoyleStoneplate', 'DA_SunfireCape'], False),
            ('TFT18_MamaBeak', 2, [], False),
            ('TFT18_Rammus', 2, ['DA_Bloodthirster', 'DA_Crownguard', 'DA_Crownguard'], False)]},
 {'name': 'c4-dense12',
  'carry': 'TFT18_Zyra',
  'tank': 'TFT18_Malphite',
  'units': [('TFT18_Aphelios', 2, [], False),
            ('TFT18_Brambleback', 2, ['DA_GuinsoosRageblade'], False),
            ('TFT18_Diana', 2, [], False),
            ('TFT18_Malphite', 2, ['DA_ArchangelsStaff', 'DA_EdgeOfNight', 'DA_GargoyleStoneplate'], False),
            ('TFT18_MamaBeak', 2, ['DA_GiantSlayer', 'DA_GuinsoosRageblade', 'DA_KrakensFury'], True),
            ('TFT18_Morgana', 2, [], False),
            ('TFT18_Sentinel', 2, ['DA_GargoyleStoneplate', 'DA_SunfireCape'], False),
            ('TFT18_Zyra', 2, ['DA_RabadonsDeathcap', 'DA_SpearOfShojin', 'DA_VoidStaff'], False)]},
 {'name': 'nine-actor-cap',
  'carry': 'TFT18_Zyra',
  'tank': 'TFT18_Malphite',
  'units': [('TFT18_Alune', 2, [], False),
            ('TFT18_Aphelios', 2, [], False),
            ('TFT18_Brambleback', 2, ['DA_GuinsoosRageblade'], False),
            ('TFT18_Draven', 2, [], False),
            ('TFT18_Malphite', 2, ['DA_ArchangelsStaff', 'DA_EdgeOfNight', 'DA_GargoyleStoneplate'], False),
            ('TFT18_MamaBeak', 2, ['DA_GiantSlayer', 'DA_GuinsoosRageblade', 'DA_KrakensFury'], True),
            ('TFT18_Morgana', 2, [], False),
            ('TFT18_Sentinel', 2, ['DA_GargoyleStoneplate', 'DA_SunfireCape'], False),
            ('TFT18_Zyra', 2, ['DA_RabadonsDeathcap', 'DA_SpearOfShojin', 'DA_VoidStaff'], False)]},
 {'name': 'elder-alpha-six',
  'carry': 'TFT18_Diana',
  'tank': 'TFT18_Hecarim',
  'units': [('TFT18_Brambleback', 2, [], False),
            ('TFT18_Cassiopeia', 2, [], False),
            ('TFT18_Diana', 3, ['DA_RabadonsDeathcap', 'DA_SpearOfShojin', 'DA_VoidStaff'], False),
            ('TFT18_ElderDragon', 1, [], True),
            ('TFT18_Hecarim', 3, ['DA_GargoyleStoneplate', 'DA_WarmogsArmor', 'DA_SunfireCape'], False),
            ('TFT18_Leona', 2, [], False),
            ('TFT18_MamaBeak', 2, [], False)]},
 {'name': 'bramble-alpha-six',
  'carry': 'TFT18_Diana',
  'tank': 'TFT18_Hecarim',
  'units': [('TFT18_Brambleback', 2, [], True),
            ('TFT18_Cassiopeia', 2, [], False),
            ('TFT18_Diana', 3, ['DA_RabadonsDeathcap', 'DA_SpearOfShojin', 'DA_VoidStaff'], False),
            ('TFT18_ElderDragon', 1, [], False),
            ('TFT18_Hecarim', 3, ['DA_GargoyleStoneplate', 'DA_WarmogsArmor', 'DA_SunfireCape'], False),
            ('TFT18_Leona', 2, [], False),
            ('TFT18_MamaBeak', 2, [], False)]}]


def reference_type():
    return getattr(theory, "ReferenceEvaluator", theory.Evaluator)


def cases(snap, *, replacements=True):
    """Portable complete inputs; reading prior caches is never required."""
    result = []
    for fixture in FIXTURES:
        members = [{"api": api, "star": star} for api, star, _, _ in fixture["units"]]
        selected = {api: {"items": list(items), "alpha": alpha} for api, _, items, alpha in fixture["units"]}
        effects = resolve_board_traits(snap, members)["effects"]
        for geometry in ("spread", "clump"):
            result.append({"name": fixture["name"] + "-" + geometry,
                           "geometry": geometry, "members": deepcopy(members), "effects": deepcopy(effects),
                           "selected": deepcopy(selected), "carry": fixture["carry"], "tank": fixture["tank"]})
    if replacements:
        for case in list(result):
            for holder, item in ((case["tank"], "DA_DragonsClaw"), (case["tank"], "DA_IonicSpark"),
                                 (case["carry"], "DA_Morellonomicon")):
                selected = deepcopy(case["selected"])
                if not selected[holder]["items"]:
                    continue
                selected[holder]["items"][0] = item
                if any(snap.items[api]["unique"] and selected[holder]["items"].count(api) > 1
                       for api in selected[holder]["items"]):
                    continue
                result.append({**case, "name": case["name"] + "-" + holder + "-" + item, "selected": selected})
    return result


def evaluate(evaluator, case, *, details=True):
    return evaluator.evaluate_many(case["members"], case["effects"], [case["selected"]],
                                   case["carry"], case["tank"], details=details)[0]


def comparable(result):
    return {key: value for key, value in result.items() if key != "modelRevision"}


def assert_close(expected, actual, path="$", *, rel_tol=1e-11, abs_tol=1e-8):
    """All schemas/order/labels exact, floating arithmetic within tight error."""
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        if type(expected) is not type(actual) or expected != actual:
            raise AssertionError(f"{path}: {expected!r} != {actual!r}")
    elif isinstance(expected, int):
        if type(actual) is not int or expected != actual:
            raise AssertionError(f"{path}: integer {expected!r} != {actual!r}")
    elif isinstance(expected, float):
        if (isinstance(actual, bool) or not isinstance(actual, (int, float))
                or not math.isclose(expected, actual, rel_tol=rel_tol, abs_tol=abs_tol)):
            raise AssertionError(f"{path}: {expected!r} != {actual!r}")
    elif isinstance(expected, dict):
        if not isinstance(actual, dict) or expected.keys() != actual.keys():
            raise AssertionError(f"{path}: dictionary keys differ: {set(expected) ^ set(actual)}")
        for key in expected:
            assert_close(expected[key], actual[key], f"{path}.{key}", rel_tol=rel_tol, abs_tol=abs_tol)
    elif isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)) or len(expected) != len(actual):
            raise AssertionError(f"{path}: sequence lengths differ")
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            assert_close(left, right, f"{path}[{index}]", rel_tol=rel_tol, abs_tol=abs_tol)
    else:
        raise TypeError(f"unsupported comparison at {path}: {type(expected).__name__}")


def score_order(results):
    return sorted(results, key=lambda name: (theory.rank_key(results[name]), name))


def capture(snap, output):
    engines = {geometry: reference_type()(snap, geometry) for geometry in ("spread", "clump")}
    inputs = cases(snap)
    for case in inputs:
        case["expected"] = evaluate(engines[case["geometry"]], case)
    payload = {"metadata": {"model": theory.MODEL, "referenceSourceHash": theory.MODEL_HASH,
                            "engineHash": tft.engine().SOURCE_HASH, "snapshotInputs": snap.hash_inputs(),
                            "patch": snap.patch}, "cases": inputs}
    save(output, payload)
    return {"cases": len(inputs), "champions": len({m["api"] for c in inputs for m in c["members"]})}


def replay(snap, corpus):
    payload = json.loads(Path(corpus).read_text())
    if payload["metadata"]["snapshotInputs"] != snap.hash_inputs():
        raise ValueError("frozen corpus uses different source data")
    engines = {geometry: theory.Evaluator(snap, geometry) for geometry in ("spread", "clump")}
    expected, actual = {}, {}
    for case in payload["cases"]:
        result = evaluate(engines[case["geometry"]], case)
        assert_close(comparable(case["expected"]), comparable(result), case["name"])
        expected[case["name"]], actual[case["name"]] = case["expected"], result
    if score_order(expected) != score_order(actual):
        raise AssertionError("frozen capacity ranking changed")
    return {"cases": len(actual), "rankingsEqual": True, "fieldsEqualWithinTolerance": True}


def optimizer_workloads(snap):
    import tft_comps
    from tft_comp_traits import primal_options, with_primal_effects

    inputs = {case["name"]: case for case in cases(snap, replacements=False)}
    reported = inputs["reported-clump"]
    dense = inputs["c4-dense12-clump"]
    defensive = deepcopy(dense["selected"])
    for api, option in defensive.items():
        if snap.units[api]["objective"] == "tank":
            option["items"] = ["DA_SunfireCape", "DA_WarmogsArmor", "DA_GargoyleStoneplate"][:len(option["items"])]
    reported_defensive = deepcopy(inputs["defensive-clump"]["selected"])
    reported_defensive[reported["tank"]]["items"][0] = "DA_SunfireCape"
    # Keep both optimization seeds legal under the required antiheal policy;
    # the archived capacity-replay fixtures above remain unchanged.
    for name, case, second in (("reported9", reported, reported_defensive),
                               ("dense12", dense, defensive)):
        prepared_at = time.perf_counter()
        screening = tft_comps.Evaluator(snap, case["geometry"], "mixed")
        variants = [with_primal_effects(snap, case["members"], case["effects"], blessing)
                    for blessing in primal_options(snap, case["members"])]
        anchors = {}
        for member in case["members"]:
            api, star = member["api"], member["star"]
            options = {}
            for effects in variants:
                for option in screening.options(api, star, effects[api], case["selected"][api].get("alpha", False)):
                    options.setdefault(tuple(option["items"]), option)
            anchors[api] = list(options.values())
        yield {"name": name, "case": case, "seeds": [case["selected"], second], "anchors": anchors,
               "anchorPreparationSeconds": time.perf_counter() - prepared_at}


def run_optimizer(snap, evaluator, workload):
    case = workload["case"]
    structure = arrangement(snap, case["selected"], case["carry"], case["tank"])
    optimizer = ItemSearch(snap, evaluator, case["members"], case["effects"], case["carry"], case["tank"],
                           structure, workload["anchors"])
    selected, result, evidence = optimizer.optimize(deepcopy(workload["seeds"]))
    return {"selected": selected, "result": result, "evidence": evidence, "stats": dict(optimizer.stats)}


def check_optimizer(reference, actual):
    if identity(reference["selected"]) != identity(actual["selected"]):
        raise AssertionError("item refinement selected different holders/items/Alpha")
    assert_close(comparable(reference["result"]), comparable(actual["result"]), "optimizer.result")
    assert_close(comparable(reference["evidence"]), comparable(actual["evidence"]), "optimizer.evidence")
    if reference["stats"] != actual["stats"]:
        raise AssertionError(f"optimizer comparison coverage changed: {reference['stats']} != {actual['stats']}")


def benchmark(snap, output, repeats):
    report = {"engineHash": tft.engine().SOURCE_HASH, "snapshotInputs": snap.hash_inputs(),
              "cpus": sorted(os.sched_getaffinity(0)), "profiled": False, "optimizePrimal": True, "workloads": []}
    for workload in optimizer_workloads(snap):
        row = {"name": workload["name"], "itemBudget": sum(len(o["items"]) for o in workload["seeds"][0].values()),
               "seeds": 2, "anchorPreparationSeconds": workload["anchorPreparationSeconds"], "timings": {}}
        reference = None
        for implementation, cls in (("reference", reference_type()), ("native", theory.Evaluator)):
            evaluator = cls(snap, workload["case"]["geometry"], optimize_primal=True)
            times, passes = [], []
            for index in range(repeats + 1):
                started = time.perf_counter()
                result = run_optimizer(snap, evaluator, workload)
                times.append(time.perf_counter() - started)
                passes.append({"kind": "cold" if index == 0 else "warm", "seconds": times[-1],
                               "scorerStats": dict(evaluator.stats),
                               "profileStats": dict(evaluator.profiles.stats)})
                if reference is None:
                    reference = result
                check_optimizer(reference, result)
                print(json.dumps({"workload": workload["name"], "implementation": implementation,
                                  "pass": "cold" if index == 0 else "warm", "seconds": times[-1],
                                  "allocationsCompared": result["stats"].get("itemAllocationsCompared"),
                                  "converged": result["evidence"]["converged"]}), flush=True)
            row["timings"][implementation] = {"coldSeconds": times[0], "warmSeconds": times[1:],
                                               "warmMedianSeconds": statistics.median(times[1:]), "passes": passes}
        row["reference"] = reference
        row["winnerEvidenceAndCoverageEqual"] = True
        row["coldSpeedup"] = row["timings"]["reference"]["coldSeconds"] / row["timings"]["native"]["coldSeconds"]
        row["warmSpeedup"] = row["timings"]["reference"]["warmMedianSeconds"] / row["timings"]["native"]["warmMedianSeconds"]
        report["workloads"].append(row)
        save(output, report)
    return {"workloads": [{key: row[key] for key in ("name", "itemBudget", "coldSpeedup", "warmSpeedup",
                                                     "winnerEvidenceAndCoverageEqual")} for row in report["workloads"]]}


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser("capture")
    capture_parser.add_argument("--out", required=True)
    replay_parser = commands.add_parser("replay")
    replay_parser.add_argument("--corpus", required=True)
    bench_parser = commands.add_parser("benchmark")
    bench_parser.add_argument("--out", required=True)
    bench_parser.add_argument("--cpu", type=int, default=2)
    bench_parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    snap = tft.load_snapshot(18, "18.1d")
    if args.command == "capture":
        result = capture(snap, args.out)
    elif args.command == "replay":
        result = replay(snap, args.corpus)
    else:
        if args.repeats < 1:
            parser.error("repeats must be positive")
        if args.cpu not in os.sched_getaffinity(0):
            parser.error("requested CPU is outside the allowed affinity")
        os.sched_setaffinity(0, {args.cpu})
        result = benchmark(snap, args.out, args.repeats)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
