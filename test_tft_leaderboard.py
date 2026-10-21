"""Optimal champion comparisons: precision, runner-up gaps and cache-only reads."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tft


class TestLeaderboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {}
        self.units = [self.snap.unit(name) for name in (
            "Akali", "Murkwolf", "Azir", "Ahri", "Ashe",
            "Leona", "Sejuani", "Amumu", "Taric")]
        self.enterContext(patch.object(tft, "modeled_units", return_value=self.units))
        self.enterContext(patch.object(tft, "snapshot_revision", return_value="test-revision"))
        self.items = [self.snap.item(name)["api"] for name in (
            "Deathblade", "Spear of Shojin", "Spear of Shojin")]

    def write(self, name, star=2, geometry="clump", traits="bare", threat="mixed", **metrics):
        unit = self.snap.unit(name)
        slug = tft.unit_slug(unit)
        key = f"s{star}-{geometry}-{traits}" + (f"-{threat}" if threat != "mixed" else "")
        performance = {"killTime": None, "total": 100, "dps": 5, "aliveTime": 20,
                       "survivalCapped": False, "stressAliveTime": None, "stressCapped": False,
                       **metrics}
        filename = str(Path(self.tmp.name) / f"{slug}-{key}.json")
        # A deliberately different rounded row catches using display data
        # or highest displayed DPS instead of the recorded optimal result.
        payload = {"rows": [{"killTime": 5.0, "dps": 999}],
                   "best": {"itemApis": self.items, "performance": performance},
                   "buildsEvaluated": 7770, "computedAt": "2026-09-06T00:00:00+00:00"}
        Path(filename).write_text(json.dumps(payload))
        self.paths[(slug, key)] = filename
        return payload

    def board(self, key="s2-clump-bare-mixed"):
        with patch.object(tft, "engine", side_effect=AssertionError("request-time simulation")):
            return tft.cached_leaderboard(key, self.paths, snap=self.snap)

    def ranked_payload(self, name="Azir", **metrics):
        """Two distinct builds whose display outcomes deliberately round equal."""
        payload = self.write(name, **metrics)
        payload["objective"] = self.snap.unit(name)["objective"]
        payload["rows"] = [{"items": self.item_names(self.items), "killTime": 5.0, "total": 100},
                           {"items": self.item_names(self.second_items()), "killTime": 5.0, "total": 100}]
        return payload

    def item_names(self, apis):
        return [self.snap.items[api]["name"] for api in apis]

    def second_items(self):
        return [self.items[0], self.items[0], self.items[1]]

    def exact_build(self, payload, apis=None, **metrics):
        apis = self.second_items() if apis is None else apis
        return {"items": self.item_names(apis), "itemApis": list(apis),
                "performance": {**payload["best"]["performance"], **metrics}}

    def legacy_runner(self, payload, **metrics):
        optimal = self.exact_build(payload, self.items)
        runner = self.exact_build(payload, **metrics)
        # Exact completions can be arbitrarily deep inside an old core record.
        payload["coreAnalysis"] = {"optimal": optimal,
                                   "candidates": [{"completions": [runner]}]}
        return runner

    def save_payload(self, payload, name="Azir"):
        path = self.paths[(tft.unit_slug(self.snap.unit(name)), "s2-clump-bare")]
        Path(path).write_text(json.dumps(payload))

    def test_highest_allowed_star_gives_one_winner_per_champion(self):
        for unit in self.units:
            self.write(unit["name"], max(tft.unit_stars(unit)))
        board = self.board("best-clump-bare-mixed")
        rows = board["damage"] + board["tanks"]
        self.assertTrue(board["complete"])
        self.assertEqual(board["readyCount"], len(self.units))
        self.assertEqual(len({row["unit"] for row in rows}), len(self.units))
        for row in rows:
            self.assertEqual(row["star"], 3 if row["cost"] <= 3 else 2)
            self.assertEqual(row["itemApis"], self.items)
            self.assertEqual(row["items"], [self.snap.items[a]["name"] for a in self.items])
            self.assertEqual(row["buildsEvaluated"], 7770)

    def test_three_star_four_and_five_costs_are_excluded_even_if_a_file_exists(self):
        for unit in self.units:
            self.write(unit["name"], 3)
        board = self.board("s3-clump-bare-mixed")
        rows = board["damage"] + board["tanks"]
        self.assertEqual(board["expectedCount"], 5)
        self.assertTrue(board["complete"])
        self.assertTrue(all(row["cost"] <= 3 and row["star"] == 3 for row in rows))

    def test_fixed_star_compares_only_that_star(self):
        self.write("Azir", 1, killTime=19)
        self.write("Azir", 2, killTime=10)
        self.write("Azir", 3, killTime=5)
        board = self.board("s1-clump-bare-mixed")
        self.assertEqual(board["damage"][0]["performance"]["killTime"], 19)
        self.assertEqual(board["damage"][0]["scenario"], "s1-clump-bare")

    def test_damage_uses_exact_clear_times_and_then_damage_for_nonclears(self):
        self.write("Azir", killTime=5.004, total=6240, dps=1247)
        self.write("Ahri", killTime=5.003, total=6240, dps=1247)
        self.write("Ashe", total=6100, dps=9999)
        self.write("Murkwolf", total=6200)
        rows = self.board()["damage"]
        self.assertEqual([row["unitName"] for row in rows], ["Ahri", "Azir", "Murkwolf", "Ashe"])
        self.assertEqual([row["rank"] for row in rows], [1, 2, 3, 4])
        self.assertEqual(rows[0]["performance"]["killTime"], 5.003)
        self.assertEqual(rows[0]["performance"]["dps"], 1247)

    def test_equal_damage_outcomes_share_competition_rank(self):
        self.write("Azir", killTime=5, total=6240)
        self.write("Ahri", killTime=5, total=6240)
        self.write("Ashe", killTime=6, total=6240)
        rows = self.board()["damage"]
        self.assertEqual([row["rank"] for row in rows], [1, 1, 3])
        self.assertEqual([row["unitName"] for row in rows[:2]], ["Ahri", "Azir"])

    def test_finite_winner_reports_its_equipped_form_and_pressure(self):
        self.units.append(self.snap.unit("Nidalee"))
        payload = self.write("Nidalee", killTime=5.5)
        payload["best"].update(form="AD", kind="Assassin", objective="fighter", range=1, pressure=True)
        Path(self.paths[("nidalee", "s2-clump-bare")]).write_text(json.dumps(payload))
        row = next(row for row in self.board()["damage"] if row["unit"] == "nidalee")
        self.assertEqual((row["form"], row["kind"], row["objective"], row["role"], row["range"], row["pressure"]),
                         ("AD", "Assassin", "fighter", "Attack Assassin", 1, True))
        self.assertEqual(row["performance"]["killTime"], 5.5)
        self.assertEqual(row["score"], [0, 5.5])

    def test_old_curve_payload_does_not_replace_the_finite_winner(self):
        payload = self.write("Azir", killTime=10, total=6240)
        payload["damageCurve"] = {"builds": [{"samples": [99999999]}]}
        Path(self.paths[("azir", "s2-clump-bare")]).write_text(json.dumps(payload))
        board = self.board()
        row = board["damage"][0]
        self.assertEqual(row["performance"]["killTime"], 10)
        self.assertEqual(row["performance"]["total"], 6240)
        self.assertEqual(row["score"], [0, 10])
        for key in ("damageCurve", "finiteDiagnostic", "evaluationModel"):
            self.assertNotIn(key, row)
        self.assertNotIn("damageEvaluationModel", board)
        self.assertNotIn("defaultDamageTime", board)

    def test_movement_assumption_and_exact_time_come_from_the_saved_cell(self):
        payload = self.write("Akali", killTime=8, movementTime=1.25, repositions=3)
        payload["scenario"] = {"dummy": {"meleeRepositionSeconds": 0.75}}
        path = Path(self.paths[("akali", "s2-clump-bare")])
        path.write_text(json.dumps(payload))
        row = self.board()["damage"][0]
        self.assertEqual(row["meleeRepositionSeconds"], 0.75)
        self.assertEqual(row["performance"]["movementTime"], 1.25)
        self.assertEqual(row["performance"]["repositions"], 3)
        del payload["scenario"]
        path.write_text(json.dumps(payload))
        self.assertNotIn("meleeRepositionSeconds", self.board()["damage"][0])

    def test_tanks_use_hold_time_and_double_pressure_with_double_caps_tied(self):
        self.write("Leona", aliveTime=60, survivalCapped=True, stressAliveTime=60,
                   stressCapped=True, total=200)
        self.write("Amumu", aliveTime=60, survivalCapped=True, stressAliveTime=60,
                   stressCapped=True, total=10000)
        self.write("Taric", aliveTime=60, survivalCapped=True, stressAliveTime=35)
        self.write("Sejuani", aliveTime=59.999)
        rows = self.board()["tanks"]
        self.assertEqual([row["unitName"] for row in rows], ["Amumu", "Leona", "Taric", "Sejuani"])
        self.assertEqual([row["rank"] for row in rows], [1, 1, 3, 4])
        self.assertTrue(all(row["objective"] == "tank" for row in rows))
        self.assertEqual(self.board()["damage"], [])

    def test_matching_formation_traits_and_tank_threat_are_selected(self):
        self.write("Azir", geometry="spread", traits="low", killTime=7)
        self.write("Azir", geometry="clump", traits="high", killTime=1)
        self.write("Leona", geometry="spread", traits="low", threat="magic", aliveTime=25)
        self.write("Leona", geometry="spread", traits="low", threat="physical", aliveTime=30)
        board = self.board("s2-spread-low-magic")
        self.assertEqual(board["damage"][0]["performance"]["killTime"], 7)
        self.assertEqual(board["damage"][0]["scenario"], "s2-spread-low")
        self.assertEqual(board["tanks"][0]["performance"]["aliveTime"], 25)
        self.assertEqual(board["tanks"][0]["scenario"], "s2-spread-low-magic")

    def test_missing_cells_are_pending_without_fallback_or_fake_zeroes(self):
        self.write("Azir", star=1)
        board = self.board()
        self.assertFalse(board["complete"])
        self.assertEqual(board["readyCount"], 0)
        self.assertEqual(board["expectedCount"], len(self.units))
        self.assertEqual(board["damage"], [])
        self.assertEqual(board["tanks"], [])
        self.assertEqual(len(board["pending"]), len(self.units))
        self.write("Azir", star=2, killTime=10)
        after = self.board()
        self.assertEqual(after["readyCount"], 1)
        self.assertEqual(len(after["pending"]), len(self.units) - 1)
        self.assertEqual(after["revision"], "test-revision")

    def test_cache_replacement_is_visible_and_returned_rows_are_independent(self):
        self.write("Azir", killTime=10)
        board = self.board()
        board["damage"][0]["items"].clear()
        board["damage"][0]["performance"]["killTime"] = -1
        self.assertEqual(self.board()["damage"][0]["performance"]["killTime"], 10)
        self.write("Azir", killTime=9.12345)
        self.assertEqual(self.board()["damage"][0]["performance"]["killTime"], 9.12345)

    def test_second_build_gap_uses_full_precision_and_prefers_new_exact_record(self):
        payload = self.ranked_payload(killTime=5.003, total=6240)
        self.legacy_runner(payload, killTime=5.9)
        payload["runnerUp"] = self.exact_build(payload, killTime=5.004)
        # Newly computed records need APIs and metrics, not redundant names.
        del payload["runnerUp"]["items"]
        result = tft.leaderboard_runner_up(payload)
        self.assertEqual(result["itemApis"], self.second_items())
        self.assertEqual(result["items"], self.item_names(self.second_items()))
        self.assertEqual(result["performance"]["killTime"], 5.004)
        self.assertEqual(result["performance"]["metric"], "killTime")
        self.assertEqual(result["performance"]["status"], "comparable")
        self.assertAlmostEqual(result["performance"]["lossPct"], 100 * (5.004 - 5.003) / 5.003)
        self.assertGreater(result["performance"]["lossPct"], 0)

    def test_legacy_second_build_preserves_duplicates_and_ignores_item_order(self):
        payload = self.ranked_payload(killTime=5.003, total=6240)
        runner = self.legacy_runner(payload, killTime=5.004)
        runner["items"].reverse()
        runner["itemApis"].reverse()
        # Same set of names, wrong multiplicity: it must not become the runner-up.
        decoy = self.exact_build(payload, self.items, killTime=10)
        payload["coreAnalysis"]["requiredItems"] = [{"without": decoy}]
        payload["coreAnalysis"]["optimal"]["itemApis"].reverse()
        result = tft.leaderboard_runner_up(payload)
        self.assertEqual(sorted(result["itemApis"]), sorted(self.second_items()))
        self.assertEqual(result["performance"]["killTime"], 5.004)
        self.assertAlmostEqual(result["performance"]["lossPct"], 100 * (5.004 - 5.003) / 5.003)

    def test_second_ranked_build_keeps_primary_ties_instead_of_skipping_to_a_lower_outcome(self):
        payload = self.ranked_payload(killTime=5, total=6240, rawTotal=9000)
        self.legacy_runner(payload, killTime=5, rawTotal=7000)
        third_apis = [self.snap.item("Blue Buff")["api"], self.items[1], self.items[1]]
        third = self.exact_build(payload, third_apis, killTime=6, rawTotal=6300)
        payload["rows"].append({"items": third["items"], "killTime": 6.0})
        payload["coreAnalysis"]["candidates"][0]["completions"].append(third)
        result = tft.leaderboard_runner_up(payload)
        self.assertEqual(result["itemApis"], self.second_items())
        self.assertEqual(result["performance"]["killTime"], 5)
        self.assertEqual(result["performance"]["lossPct"], 0)

    def test_nonclear_gap_uses_exact_damage_and_overkill_does_not_create_a_gap(self):
        payload = self.ranked_payload(total=100.004, rawTotal=1000)
        payload["runnerUp"] = self.exact_build(payload, total=100.003, rawTotal=500)
        result = tft.leaderboard_runner_up(payload)["performance"]
        self.assertEqual((result["metric"], result["status"], result["value"]),
                         ("damage", "comparable", 100.003))
        self.assertAlmostEqual(result["lossPct"], 100 * (100.004 - 100.003) / 100.004)
        payload["runnerUp"]["performance"]["total"] = 100.004
        self.assertEqual(tft.leaderboard_runner_up(payload)["performance"]["lossPct"], 0)

    def test_second_build_that_does_not_clear_has_a_censored_gap(self):
        payload = self.ranked_payload(killTime=5, total=6240)
        payload["runnerUp"] = self.exact_build(payload, killTime=None, total=6000)
        performance = tft.leaderboard_runner_up(payload)["performance"]
        self.assertEqual((performance["metric"], performance["status"]), ("killTime", "noClear"))
        self.assertIsNone(performance["lossPct"])
        self.assertIsNone(performance["value"])

    def test_tank_gaps_use_hold_time_stress_time_and_censored_survival_limits(self):
        cases = [
            ({"aliveTime": 30.005}, {"aliveTime": 29.995},
             "aliveTime", "comparable", 100 * (30.005 - 29.995) / 30.005),
            ({"aliveTime": 60, "survivalCapped": True, "stressAliveTime": 40.004},
             {"stressAliveTime": 39.996}, "stressAliveTime", "comparable",
             100 * (40.004 - 39.996) / 40.004),
            ({"aliveTime": 60, "survivalCapped": True, "stressAliveTime": 40},
             {"aliveTime": 59.999, "survivalCapped": False, "stressAliveTime": None},
             "stressAliveTime", "shortSurvival", None),
            ({"aliveTime": 60, "survivalCapped": True, "stressAliveTime": 60, "stressCapped": True},
             {"total": 1}, "capped", "comparable", 0),
            ({"aliveTime": 60, "survivalCapped": True, "stressAliveTime": 60, "stressCapped": True},
             {"stressAliveTime": 59.999, "stressCapped": False}, "capped", "belowCap", None),
        ]
        for first, second, metric, status, expected_gap in cases:
            with self.subTest(metric=metric, status=status):
                payload = self.ranked_payload("Leona", **first)
                payload["runnerUp"] = self.exact_build(payload, **second)
                performance = tft.leaderboard_runner_up(payload)["performance"]
                self.assertEqual((performance["metric"], performance["status"]), (metric, status))
                if expected_gap is None:
                    self.assertIsNone(performance["lossPct"])
                else:
                    self.assertAlmostEqual(performance["lossPct"], expected_gap)

    def test_old_cells_without_an_exact_second_score_do_not_use_rounded_rows(self):
        payload = self.ranked_payload(killTime=5.003, total=6240)
        self.assertIsNone(tft.leaderboard_runner_up(payload))
        payload["coreAnalysis"] = {"optimal": self.exact_build(payload, self.items)}
        self.assertIsNone(tft.leaderboard_runner_up(payload))
        payload["coreAnalysis"]["candidates"] = [{"completions": [{
            "items": self.item_names(self.second_items()), "itemApis": self.second_items()}]}]
        self.assertIsNone(tft.leaderboard_runner_up(payload))
        self.save_payload(payload)
        self.assertNotIn("secondBest", self.board()["damage"][0])
        single_row = self.write("Azir", killTime=5.003)
        self.assertIsNone(tft.leaderboard_runner_up(single_row))
        self.assertNotIn("secondBest", self.board()["damage"][0])

    def test_mismatched_optimal_and_runner_identities_are_rejected(self):
        payload = self.ranked_payload(killTime=5, total=6240)
        self.legacy_runner(payload, killTime=6)
        payload["coreAnalysis"]["optimal"]["itemApis"] = self.second_items()
        with self.assertRaisesRegex(ValueError, "optimal build"):
            tft.leaderboard_runner_up(payload)

        payload = self.ranked_payload(killTime=5, total=6240)
        for change in ({"itemApis": self.items}, {"items": self.item_names(self.items)}):
            with self.subTest(change=change):
                payload["runnerUp"] = {**self.exact_build(payload, killTime=6), **change}
                with self.assertRaisesRegex(ValueError, "second-ranked build"):
                    tft.leaderboard_runner_up(payload)
        payload["runnerUp"] = self.exact_build(payload, killTime=4)
        with self.assertRaisesRegex(ValueError, "ahead of its optimal"):
            tft.leaderboard_runner_up(payload)
        payload["rows"][1]["items"] = list(reversed(payload["rows"][0]["items"]))
        with self.assertRaisesRegex(ValueError, "repeats the optimal"):
            tft.leaderboard_runner_up(payload)

    def test_cached_runner_metrics_are_exact_and_returned_rows_are_independent(self):
        payload = self.ranked_payload(killTime=5.003, total=6240)
        payload["runnerUp"] = self.exact_build(payload, killTime=5.004)
        self.save_payload(payload)
        row = self.board()["damage"][0]
        self.assertEqual(row["secondBest"]["performance"]["killTime"], 5.004)
        self.assertAlmostEqual(row["secondBest"]["performance"]["lossPct"], 100 * (5.004 - 5.003) / 5.003)
        row["secondBest"]["items"].clear()
        row["secondBest"]["itemApis"].clear()
        row["secondBest"]["performance"]["lossPct"] = -1
        fresh = self.board()["damage"][0]["secondBest"]
        self.assertEqual(fresh["items"], self.item_names(self.second_items()))
        self.assertEqual(fresh["itemApis"], self.second_items())
        self.assertGreater(fresh["performance"]["lossPct"], 0)
        payload["runnerUp"]["performance"]["killTime"] = 5.01
        self.save_payload(payload)
        self.assertEqual(self.board()["damage"][0]["secondBest"]["performance"]["killTime"], 5.01)

    def test_old_cache_cannot_silently_fall_back_to_rounded_results(self):
        payload = self.write("Azir")
        del payload["best"]
        Path(self.paths[("azir", "s2-clump-bare")]).write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "exact leaderboard result"):
            self.board()

    def test_invalid_scenarios_fail_before_any_cache_read(self):
        for key in ("s4-clump-bare-mixed", "best-clump-any-mixed", "s2-spread-low-other", "../meta"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "unknown leaderboard"):
                self.board(key)
        self.assertEqual(len(tft.leaderboard_scenarios()), 72)

    def test_computed_winner_preserves_the_actual_unrounded_result(self):
        # Small pool, real enumeration: verify the extra cache record is
        # the same winning build and fight, without changing display rows.
        unit = self.snap.unit("Azir")
        key = "s2-clump-bare"
        filename = str(Path(self.tmp.name) / "actual.json")
        pool = [self.snap.item(name)["api"] for name in ("Blue Buff", "Spear of Shojin")]
        with patch.object(tft, "pool_items", return_value=pool):
            result = tft.compute_cell(self.snap, unit, key, {("azir", key): filename}, prune=False)
        best = result["best"]
        _, fight = tft.simulate(self.snap, unit, 2, best["itemApis"], "clump", [], tft.dummies_for(self.snap))
        self.assertEqual(best["performance"], {field: fight[field] for field in best["performance"]})
        self.assertEqual(result["rows"][0]["items"], [self.snap.items[a]["name"] for a in best["itemApis"]])
        kill_time = best["performance"]["killTime"]
        self.assertEqual(result["rows"][0]["killTime"], None if kill_time is None else round(kill_time, 2))
        self.assertEqual(result["rows"][0]["total"], round(best["performance"]["total"]))

        # Every newly computed cell retains the real second ranked build even
        # if it would not survive the older core suggestions' display limits.
        runner = result["runnerUp"]
        self.assertEqual(json.loads(Path(filename).read_text())["runnerUp"], runner)
        self.assertNotEqual(sorted(runner["itemApis"]), sorted(best["itemApis"]))
        self.assertEqual(result["rows"][1]["items"], self.item_names(runner["itemApis"]))
        _, second_fight = tft.simulate(self.snap, unit, 2, runner["itemApis"], "clump", [],
                                       tft.dummies_for(self.snap))
        self.assertEqual(runner["performance"], {field: second_fight[field] for field in runner["performance"]})
        comparison = tft.leaderboard_runner_up(result)
        self.assertEqual(comparison["itemApis"], runner["itemApis"])
        self.assertEqual(comparison["performance"]["killTime"], second_fight["killTime"])
        without_cores = copy.deepcopy(result)
        del without_cores["coreAnalysis"]
        self.assertEqual(tft.leaderboard_runner_up(without_cores), comparison)


if __name__ == "__main__":
    unittest.main()
