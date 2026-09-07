"""The performance verifier must reject any combat or ordering drift."""
from copy import deepcopy
import math
import json
from pathlib import Path
import struct
import tempfile
import unittest

from jobs.tft_exact_verify import (benchmark_comparison, composition_index, differences, freeze,
                                   normalize_artifact, normalize_cell_artifact, thaw)


class TestExactParityVerifier(unittest.TestCase):
    def test_float_bits_include_signed_zero_and_nan_payload(self):
        self.assertTrue(differences(0.0, -0.0))
        self.assertTrue(differences(1.0, math.nextafter(1.0, math.inf)))
        a = struct.unpack(">d", bytes.fromhex("7ff8000000000001"))[0]
        b = struct.unpack(">d", bytes.fromhex("7ff8000000000002"))[0]
        self.assertFalse(differences(a, a))
        self.assertTrue(differences(a, b))

    def test_serialization_retains_exact_native_types_and_bits(self):
        value = ({"trace": [(0.0, 1), (-0.0, True)], "inf": math.inf}, [None, "inf", 1.0])
        self.assertFalse(differences(value, thaw(freeze(value))))
        self.assertTrue(differences(1, 1.0))
        self.assertTrue(differences(1, True))
        self.assertTrue(differences([1], (1,)))

    def test_trace_order_and_missing_result_fields_are_not_normalized(self):
        trace = [{"time": 0.0, "kind": "damage"}, {"time": 0.0, "kind": "death"}]
        self.assertTrue(differences(trace, list(reversed(trace))))
        self.assertTrue(differences({"outcome": "win", "hp": 1.0}, {"outcome": "win"}))
        self.assertFalse(differences({"a": 1, "b": 2}, {"b": 2, "a": 1}))

    def artifact(self):
        return {"revision": "old", "baselineRevision": "old", "computedAt": "then", "computeSeconds": 10.0,
                "search": {"exhaustive": False, "sharedFightsSimulated": 12},
                "opponentPool": {"hash": "authored-input-hash", "version": "same"},
                "methodology": {"fightDuration": 30.0},
                "results": [{"poolRevision": "old", "duration": 20.0, "clearTime": 20.0,
                             "hpMargin": 0.2, "items": ["a", "b"],
                             "validation": {"poolRevision": "old", "benchmarkWins": 4},
                             "alternatives": [{"winDelta": 0, "lostMatchups": ["a", "b"]}]}]}

    def test_only_operational_revision_timing_and_counters_are_ignored(self):
        before, after = self.artifact(), self.artifact()
        after.update(revision="new", baselineRevision="new", computedAt="now", computeSeconds=1.0)
        after["search"]["sharedFightsSimulated"] = 3
        after["results"][0]["poolRevision"] = "new"
        after["results"][0]["validation"]["poolRevision"] = "new"
        self.assertFalse(differences(normalize_artifact(before), normalize_artifact(after)))

    def test_authored_pool_hash_game_timings_and_replacement_order_stay_exact(self):
        before = self.artifact()
        mutations = [
            lambda a: a["opponentPool"].update(hash="changed"),
            lambda a: a["methodology"].update(fightDuration=29.0),
            lambda a: a["search"].update(exhaustive=True),
            lambda a: a["results"][0].update(duration=19.0),
            lambda a: a["results"][0].update(clearTime=19.0),
            lambda a: a["results"][0].update(hpMargin=math.nextafter(0.2, math.inf)),
            lambda a: a["results"][0]["items"].reverse(),
            lambda a: a["results"][0]["alternatives"][0]["lostMatchups"].reverse(),
            lambda a: a["results"][0]["validation"].update(benchmarkWins=3),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                after = deepcopy(before)
                mutation(after)
                self.assertTrue(differences(normalize_artifact(before), normalize_artifact(after)))

    def test_benchmark_comparison_rejects_workload_drift_and_profiled_times(self):
        before = {"inputHash": "fixed", "cpu": 2, "affinity": [2], "fights": 240, "profiled": False,
                  "engineHash": "old", "root": "baseline", "workloads": {
                      "matches": {"medianSeconds": 2.0, "minSeconds": 1.9, "maxSeconds": 2.1, "loopsPerSample": 2}}}
        after = deepcopy(before)
        after["engineHash"] = "new"
        after["workloads"]["matches"].update(medianSeconds=1.0, minSeconds=0.9, maxSeconds=1.1)
        self.assertEqual(benchmark_comparison(before, after)["workloads"]["matches"]["speedup"], 2.0)
        for key, value in (("cpu", 3), ("affinity", [2, 3]), ("inputHash", "changed"), ("profiled", True)):
            with self.subTest(key=key):
                changed = deepcopy(after)
                changed[key] = value
                with self.assertRaises(ValueError):
                    benchmark_comparison(before, changed)

    def test_composition_index_ignores_progress_but_rejects_malformed_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "progress.json").write_text(json.dumps({"revision": "new", "key": "c1-spread-mixed"}))
            name = root / "c1-spread-mixed-abc123.json"
            name.write_text(json.dumps({"revision": "new", "key": "c1-spread-mixed", "results": {}}))
            self.assertEqual(composition_index(root, "new"), {"c1-spread-mixed": name})
            name.write_text(json.dumps({"revision": "new", "key": "c1-spread-mixed"}))
            with self.assertRaisesRegex(RuntimeError, "invalid composition artifact shape"):
                composition_index(root, "new")

    def test_cell_comparison_preserves_core_pairs_and_diagnostic_times(self):
        before = {"computedAt": "before", "computeSeconds": 10.0,
                  "coreAnalysis": {"candidates": [{"items": ["a", "b"], "killTime": 2.0}]}}
        after = deepcopy(before)
        after.update(computedAt="after", computeSeconds=1.0)
        self.assertFalse(differences(normalize_cell_artifact(before), normalize_cell_artifact(after)))
        after["coreAnalysis"]["candidates"][0]["items"].reverse()
        self.assertTrue(differences(normalize_cell_artifact(before), normalize_cell_artifact(after)))
        after = deepcopy(before)
        after["coreAnalysis"]["candidates"][0]["killTime"] = math.nextafter(2.0, math.inf)
        self.assertTrue(differences(normalize_cell_artifact(before), normalize_cell_artifact(after)))


if __name__ == "__main__":
    unittest.main()
