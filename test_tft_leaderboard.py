"""Optimal champion comparisons: precision, eligibility and cache-only reads."""
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


if __name__ == "__main__":
    unittest.main()
