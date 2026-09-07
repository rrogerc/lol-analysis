"""Full-build score export for TFT core analysis.

Run after rebuilding the engine: python3 -m unittest test_tft_scores -v
"""

import itertools
import unittest

import tft
from test_tft import ENGINE, ITEM_FX, SNAP, spec_for
from test_tft_tanks import body_spec


def compact_row(row):
    combo, _, res = row
    return (combo, res["killTime"], res["total"], res["aliveTime"],
            res["survivalCapped"], res["stressAliveTime"], res["stressCapped"])


def item_pool(spec, names):
    unit = SNAP.units[spec["unit"]["api"]]
    spec["pool"] = [tft.item_spec(SNAP, SNAP.item(name)["api"], ITEM_FX, unit)
                    for name in names]
    return spec


class TestCellScores(unittest.TestCase):
    def test_all_scores_match_full_rich_results_for_each_objective(self):
        items = ["Infinity Edge", "Guinsoo's Rageblade", "Jeweled Gauntlet",
                 "Nashor's Tooth", "Warmog's Armor"]
        for name in ("Ashe", "Warwick", "Leona"):
            with self.subTest(unit=name):
                spec = item_pool(spec_for(name), items)
                count, full = ENGINE.run_cell(spec, 1000, 1)
                analyzed, rows, scores = ENGINE.analyze_cell(spec, 3, 1)
                self.assertEqual(analyzed, count)
                self.assertEqual(rows, full[:3])
                self.assertEqual(scores, [compact_row(row) for row in full])
                self.assertEqual(len(scores), count)
                self.assertGreater(count, len(rows))
                # These values must stay unrounded for small substitution
                # penalties; the dashboard rounds only for display.
                self.assertTrue(any(s[2] != round(s[2], 2) for s in scores))

    def test_capped_and_measured_tanks_keep_the_stress_measurements(self):
        spec = body_spec(hp=400.0, duration=6.5)
        spec["pool"] = [
            {"api": "health", "name": "health", "unique": False,
             "stats": [["hp", 300.0]], "adds": []},
            {"api": "damage", "name": "damage", "unique": False,
             "stats": [["adPct", 10.0]], "adds": []},
        ]
        count, rows, scores = ENGINE.analyze_cell(spec, 4, 1)
        self.assertEqual(count, 4)
        self.assertEqual(scores, [compact_row(row) for row in rows])
        by_combo = {tuple(score[0]): score for score in scores}
        self.assertEqual(by_combo[(0, 0, 0)][3:], (6.5, True, 6.5, True))
        self.assertEqual(by_combo[(0, 0, 1)][3:], (6.5, True, 5.0, False))
        self.assertEqual(by_combo[(1, 1, 1)][3:], (4.0, False, None, False))
        self.assertEqual((count, rows), ENGINE.run_cell(spec, 4, 1))

    def test_duplicates_unique_constraints_and_api_ties_survive_export(self):
        spec = spec_for("Ashe", duration=0.1)
        # Identical effects force the API tuple tie-break. Pool order is
        # deliberately different from API order, and only one item is unique.
        spec["pool"] = [
            {"api": api, "name": api, "unique": unique, "stats": [], "adds": []}
            for api, unique in (("z", False), ("a", True), ("m", False))
        ]
        legal = [c for c in itertools.combinations_with_replacement(range(3), 3)
                 if c.count(1) <= 1]
        expected = sorted(legal, key=lambda c: tuple(spec["pool"][i]["api"] for i in c))
        count, rows, scores = ENGINE.analyze_cell(spec, 2, 1)
        self.assertEqual(count, len(expected))
        self.assertEqual([tuple(score[0]) for score in scores], expected)
        self.assertEqual(rows, ENGINE.run_cell(spec, 2, 1)[1])
        self.assertIn((0, 0, 0), expected)
        self.assertIn((2, 2, 2), expected)
        self.assertNotIn((1, 1, 1), expected)

    def test_scores_and_rows_are_independent_of_worker_count(self):
        spec = spec_for("Ashe")
        unit = SNAP.unit("Ashe")
        spec["pool"] = [tft.item_spec(SNAP, api, ITEM_FX, unit)
                        for api in tft.pool_items(SNAP, ITEM_FX)]
        expected = ENGINE.analyze_cell(spec, 7, 1)
        self.assertGreater(expected[0], 7000)
        for workers in (4, 0):
            with self.subTest(workers=workers):
                self.assertEqual(ENGINE.analyze_cell(spec, 7, workers), expected)

    def test_top_limits_only_rich_rows_including_zero(self):
        spec = item_pool(spec_for("Ashe"), ["Nashor's Tooth", "Jeweled Gauntlet"])
        count, all_rows, all_scores = ENGINE.analyze_cell(spec)
        self.assertEqual(len(all_rows), count)
        for top in (0, 1, count, count + 1):
            with self.subTest(top=top):
                analyzed, rows, scores = ENGINE.analyze_cell(spec, top, 1)
                self.assertEqual(analyzed, count)
                self.assertEqual(rows, all_rows[:top])
                self.assertEqual(scores, all_scores)

    def test_no_legal_builds_return_empty_rows_and_scores(self):
        spec = spec_for("Ashe")
        for pool in ([], [{"api": "only", "name": "only", "unique": True,
                           "stats": [], "adds": []}]):
            with self.subTest(pool=pool):
                spec["pool"] = pool
                self.assertEqual(ENGINE.analyze_cell(spec), (0, [], []))
                self.assertEqual(ENGINE.run_cell(spec), (0, []))


if __name__ == "__main__":
    unittest.main()
