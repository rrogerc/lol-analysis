"""Core analysis integration: full enumeration, cache and scenario coverage."""

import glob
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import tft


class TestCoreCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def test_analysis_uses_builds_below_the_display_limit(self):
        unit = self.snap.unit("Ashe")
        key = "s2-clump-bare"
        # Three items whose builds stay within a few percent of each other for
        # Ashe, so a flexible core exists (with the cast timelines Red Buff's
        # builds no longer are: Giant Slayer x2 + Red Buff leads by 9%; with
        # the five-slot formation Deathblade's no longer are either).
        pool = [self.snap.item(name)["api"] for name in
                ("Blue Buff", "Giant Slayer", "Infinity Edge")]
        full, count = tft.enumerate_builds(self.snap, unit, 2, "clump", [],
                                          tft.dummies_for(self.snap), pool, top=100, workers=1)
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "CACHED_ROWS", 1), \
                patch.object(tft, "pool_items", return_value=pool):
            path = os.path.join(directory, "cell.json")
            cell = tft.compute_cell(self.snap, unit, key, {("ashe", key): path}, prune=False)
            self.assertEqual(cell["rows"], tft.cell_rows(self.snap, unit, full, 1))
            self.assertEqual(cell["coreAnalysis"]["buildsEvaluated"], count)
            self.assertGreater(count, len(cell["rows"]))
            candidates = cell["coreAnalysis"]["candidates"]
            self.assertTrue(any(len(core["completions"]) > 1 for core in candidates))
            for core in candidates:
                for excluded in core["exclusions"]:
                    best_without = next(combo for combo, _, _ in full
                                        if excluded["itemApi"] not in combo)
                    self.assertEqual(excluded["without"]["itemApis"], list(best_without))
            with open(path) as f:
                self.assertEqual(json.load(f), cell)

    def test_contexts_cover_all_cores_and_only_the_requested_current_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            keys = ("s1-clump-bare", "s2-clump-bare", "s2-spread-bare")
            paths = {("ahri", key): os.path.join(directory, key + ".json") for key in keys}
            paths[("leona", keys[0])] = os.path.join(directory, "other.json")
            stats = {"hidden-from-suggestions": {"nearCount": 2, "totalCount": 35,
                                               "best": {"lossPct": 1.25, "status": "comparable"}}}
            first = {"scenario": tft.SCENARIOS[keys[0]],
                     "coreAnalysis": {"coreStats": stats, "candidates": []}}
            # The second cell is from an old schema, the third is still cold.
            for key, content in ((keys[0], first), (keys[1], {"rows": []})):
                with open(paths[("ahri", key)], "w") as f:
                    json.dump(content, f)
            with patch.object(tft, "snapshot_revision", return_value="current"):
                contexts = tft.cached_core_contexts("ahri", paths, snap=self.snap)
            self.assertEqual(contexts["totalScenarios"], 3)
            self.assertEqual(contexts["revision"], "current")
            self.assertEqual(len(contexts["scenarios"]), 1)
            self.assertEqual(contexts["scenarios"][0]["key"], keys[0])
            self.assertEqual(contexts["scenarios"][0]["coreStats"], stats)
            self.assertFalse(os.path.exists(paths[("ahri", keys[2])]))
            with self.assertRaisesRegex(ValueError, "unknown unit"):
                tft.cached_core_contexts("missing", paths, snap=self.snap)

    def test_truncated_score_export_is_rejected(self):
        unit = self.snap.unit("Ahri")
        with patch.object(tft.engine(), "analyze_cell", return_value=(2, [], [])):
            with self.assertRaisesRegex(ValueError, "every enumerated build"):
                tft.enumerate_builds(self.snap, unit, 2, "clump", [],
                                     tft.dummies_for(self.snap), [], include_scores=True)

    def test_cached_spikes_are_actual_two_item_fights(self):
        unit = self.snap.unit("Ashe")
        pool = [self.snap.item(name)["api"] for name in ("Blue Buff", "Giant Slayer", "Infinity Edge")]
        key = "s2-clump-bare"
        dummy = tft.dummies_for(self.snap)
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "pool_items", return_value=pool):
            path = os.path.join(directory, "cell.json")
            cell = tft.compute_cell(self.snap, unit, key, {("ashe", key): path}, prune=False)
            analysis = cell["coreAnalysis"]
            self.assertEqual(analysis["selectionMode"], "twoItemSpike")
            self.assertEqual(analysis["pairBuildsEvaluated"], 6)
            self.assertEqual(analysis["minCompletions"], 2)
            self.assertTrue(analysis["candidates"])
            used_finishes = set()
            for core in analysis["candidates"]:
                self.assertEqual(len(core["itemApis"]), 2)
                self.assertGreaterEqual(core["nearCount"], 2)
                _, result = tft.simulate(self.snap, unit, 2, core["itemApis"], "clump", [], dummy)
                for field in ("killTime", "total", "aliveTime", "survivalCapped",
                              "stressAliveTime", "stressCapped"):
                    self.assertEqual(core["spike"][field], result[field])
                near = {tuple(sorted(build["itemApis"])) for build in core["completions"]
                        if build["performance"]["status"] == "comparable"
                        and build["performance"]["lossPct"] <= analysis["thresholdPct"] + 1e-9}
                self.assertTrue(used_finishes.isdisjoint(near))
                used_finishes.update(near)
            with open(path) as f:
                self.assertEqual(json.load(f)["coreAnalysis"], analysis)

    def test_incomplete_pair_export_does_not_publish_a_cell(self):
        unit = self.snap.unit("Ashe")
        key = "s2-clump-bare"
        pool = [self.snap.item("Red Buff")["api"]]
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "pool_items", return_value=pool), \
                patch.object(tft.engine(), "score_pairs", return_value=[]):
            path = os.path.join(directory, "cell.json")
            with self.assertRaisesRegex(ValueError, "every legal pair"):
                tft.compute_cell(self.snap, unit, key, {("ashe", key): path}, prune=False)
            self.assertFalse(os.path.exists(path))

    def test_tank_cells_keep_single_item_recommendations_without_pair_fights(self):
        unit = self.snap.unit("Leona")
        key = "s2-clump-bare"
        pool = [self.snap.item("Warmog's Armor")["api"]]
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "pool_items", return_value=pool), \
                patch.object(tft.engine(), "score_pairs", side_effect=AssertionError("tank pair simulation")):
            cell = tft.compute_cell(self.snap, unit, key, {("leona", key): os.path.join(directory, "cell.json")},
                                    prune=False)
            self.assertEqual(cell["coreAnalysis"]["selectionMode"], "completions")
            self.assertEqual(cell["coreAnalysis"]["pairBuildsEvaluated"], 0)
            self.assertTrue(all(len(core["items"]) == 1 and "spike" not in core
                                for core in cell["coreAnalysis"]["candidates"]))

    def test_static_export_pins_one_snapshot_for_cells_and_core_contexts(self):
        import webapp
        import tft_comps

        key = "s2-clump-bare"
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = os.path.join(directory, "cell.json")
            payload = {"scenario": tft.SCENARIOS[key], "rows": [],
                       "coreAnalysis": {"coreStats": {"an-item-pair": {"nearCount": 2}}}}
            with open(path, "w") as f:
                json.dump(payload, f)
            paths = {("ahri", key): path}
            con = SimpleNamespace(execute=lambda *args: [])
            early_payload = {"schema": "tft-early-comps-v1", "set": self.snap.set_no,
                "patch": self.snap.patch, "revision": "early-export-fixture",
                "baselineRevision": tft.snapshot_revision(self.snap), "complete": True,
                "variants": [{"key": "level4", "results": [{"score": 55.0}]}]}
            for owner, name, value in (
                    (webapp, "db_connect", con), (webapp, "app_meta", {"jobs": [], "tiers": ["test"]}),
                    (webapp.builds, "api_builds_meta", {}), (webapp.builds, "warm", 0),
                    (webapp.builds, "cell_paths", {}), (webapp.scaling, "db_patches", []),
                    (webapp.scaling, "build_rows", []), (webapp.onetricks, "api_onetricks", {}),
                    (webapp.builds_leaderboard, "cached_leaderboard", {}),
                    (tft, "cell_paths", paths),
                    (webapp.tft_early, "supports", True),
                    (webapp.tft_early, "revision", early_payload["revision"]),
                    (webapp.tft_early, "cached_catalog", early_payload)):
                stack.enter_context(patch.object(owner, name, return_value=value))
            for name in ("warm", "cached_scenario", "api_meta", "cell_ready", "revision"):
                stack.enter_context(patch.object(tft_comps, name,
                    side_effect=AssertionError("export must not use archived compositions")))
            # The fixture's cell inventory defines its baseline revision.
            early_payload["baselineRevision"] = tft.snapshot_revision(self.snap)
            # A second lookup could return a newly published patch. The entire
            # export must keep using the snapshot its metadata described.
            loaded = stack.enter_context(patch.object(tft, "load_snapshot", side_effect=[self.snap,
                         AssertionError("export reloaded the active snapshot")]))
            warmed = stack.enter_context(patch.object(tft, "warm", return_value=0))
            early_warmed = stack.enter_context(patch.object(webapp.tft_early, "warm", return_value=0))
            out = os.path.join(directory, "export")
            legacy = os.path.join(out, "api", "tft", "compositions")
            os.makedirs(legacy)
            legacy_bytes = b'{"savedComposition":true}\n'
            with open(os.path.join(legacy, "saved.json"), "wb") as f:
                f.write(legacy_bytes)
            webapp.cmd_export(SimpleNamespace(out=out))
            self.assertFalse(os.path.exists(os.path.join(out, "api", "tft", "compositions")))
            archived = glob.glob(os.path.join(out, "archive", "tft-compositions", "*", "saved.json"))
            self.assertEqual(len(archived), 1)
            with open(archived[0], "rb") as f:
                self.assertEqual(f.read(), legacy_bytes)
            loaded.assert_called_once_with()
            warmed.assert_called_once_with(snap=self.snap, prune=False)
            early_warmed.assert_called_once_with(snap=self.snap)
            with open(os.path.join(out, "api", "tft", "traits.json")) as f:
                flex = json.load(f)
            self.assertEqual((flex["set"], flex["patch"]), (self.snap.set_no, self.snap.patch))
            self.assertEqual(flex["schema"], "tft-trait-flex-v1")
            self.assertEqual({unit["api"] for unit in flex["units"]}, set(self.snap.units))
            self.assertNotIn("items", flex)
            with open(os.path.join(out, "api", "tft", "early.json")) as f:
                early = json.load(f)
            self.assertEqual((early["set"], early["patch"]), (self.snap.set_no, self.snap.patch))
            self.assertEqual(early["variants"], early_payload["variants"])
            self.assertEqual(early["baselineRevision"], early_payload["baselineRevision"])
            self.assertIn("teamPlanner", early)
            self.assertNotIn("teamPlanner", early_payload)
            self.assertNotIn("publicationRevision", early_payload)
            records = []
            for name in ("meta.json", "status.json", "ahri/cores.json"):
                with open(os.path.join(out, "api", "tft", name)) as f:
                    records.append(json.load(f))
            self.assertEqual(len({record["revision"] for record in records}), 1)
            self.assertEqual(records[-1]["scenarios"][0]["coreStats"], payload["coreAnalysis"]["coreStats"])
            with open(os.path.join(out, "api", "tft", "ahri", key + ".json")) as f:
                self.assertEqual(json.load(f), payload)
            for board_key in tft.leaderboard_scenarios():
                with open(os.path.join(out, "api", "tft", "leaderboard", board_key + ".json")) as f:
                    board = json.load(f)
                self.assertEqual(board["revision"], records[0]["revision"])
                self.assertEqual(board["selection"]["key"], board_key)
                self.assertFalse(board["complete"])
                self.assertEqual(board["readyCount"], 0)
            publication = records[0]["publicationRevision"]
            self.assertTrue(publication.startswith("g-"))
            self.assertEqual({record["publicationRevision"] for record in
                             (records[0], records[1], flex, early)}, {publication})
            self.assertEqual(flex["teamPlanner"], early["teamPlanner"])


if __name__ == "__main__":
    unittest.main()
