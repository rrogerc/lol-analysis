"""Exact zero-to-three-item options for a composition's shared budget."""

import itertools
import random
import unittest

import tft
from test_tft import ENGINE, ITEM_FX, SNAP, TRAIT_FX, spec_for
from test_tft_pairs import synthetic_item, with_pool
from test_tft_tanks import body_spec


def direct_loadouts(spec):
    groups = []
    for size in range(4):
        rows = []
        for combo in itertools.combinations_with_replacement(range(len(spec["pool"])), size):
            if any(spec["pool"][i]["unique"] and combo.count(i) > 1 for i in combo):
                continue
            opening, result = ENGINE.simulate(dict(spec, items=[spec["pool"][i] for i in combo]))
            rows.append((list(combo), opening, result))
        rows.sort(key=lambda row: (tft.rank_key(row[2], spec["unit"]["objective"]),
                                  tuple(spec["pool"][i]["api"] for i in row[0])))
        groups.append(rows)
    return sum(map(len, groups)), groups


class TestLoadoutOptimization(unittest.TestCase):
    def test_each_item_count_matches_direct_fights_across_roles_and_forms(self):
        names = ["Jeweled Gauntlet", "Nashor's Tooth", "Deathblade", "Warmog's Armor"]
        for name, star, geometry, context in (
                ("Ahri", 2, "spread", "low"), ("Azir", 3, "clump", "high"),
                ("Murkwolf", 2, "clump", "low"), ("Warwick", 1, "spread", "high"),
                ("Akali", 2, "clump", "high"), ("Gromp", 3, "spread", "low")):
            with self.subTest(unit=name, star=star, geometry=geometry, traits=context):
                unit = SNAP.unit(name)
                contexts, _ = tft.unit_trait_contexts(SNAP, unit, TRAIT_FX)
                spec = tft.cell_spec(SNAP, unit, star, geometry, contexts[context],
                                     tft.dummies_for(SNAP), item_fx=ITEM_FX, trait_fx=TRAIT_FX)
                with_pool(spec, names)
                expected_count, expected = direct_loadouts(spec)
                count, rows = ENGINE.optimize_loadouts(spec, top=4, workers=1)
                self.assertEqual(count, expected_count)
                self.assertEqual(rows, [group[:4] for group in expected])
                all_count, all_rows = ENGINE.optimize_loadouts(spec, top=1000, workers=3)
                self.assertEqual((all_count, all_rows), (expected_count, expected))

    def test_no_placeholder_or_single_fight_items_fill_empty_slots(self):
        spec = spec_for("Ashe", driver="Driver", duration=0.1)
        item = synthetic_item("ad", stats=[["adPct", 0.5]])
        spec["pool"] = [item]
        spec["items"] = [synthetic_item("unrelated", stats=[["adPct", 10.0]])]
        count, rows = ENGINE.optimize_loadouts(spec)
        self.assertEqual(count, 4)
        self.assertEqual([group[0][0] for group in rows], [[], [0], [0, 0], [0, 0, 0]])
        bare_damage = rows[0][0][2]["total"]
        for size, group in enumerate(rows):
            self.assertAlmostEqual(group[0][2]["total"], bare_damage * (1 + 0.5 * size))
        self.assertEqual((count, rows), direct_loadouts(spec))

    def test_unique_constraints_and_api_ties_apply_at_each_count(self):
        spec = spec_for("Ashe", duration=0.1)
        spec["pool"] = [synthetic_item("z"), synthetic_item("a", True), synthetic_item("m")]
        expected = direct_loadouts(spec)
        count, rows = ENGINE.optimize_loadouts(spec, top=1000)
        self.assertEqual((count, rows), expected)
        for size, group in enumerate(rows):
            self.assertTrue(all(len(row[0]) == size for row in group))
            self.assertTrue(all(row[0].count(1) <= 1 for row in group))
        self.assertIn([0, 0, 0], [row[0] for row in rows[3]])
        self.assertNotIn([1, 1], [row[0] for row in rows[2]])

    def test_empty_and_small_pools_are_bounded_and_return_four_groups(self):
        spec = spec_for("Ashe", duration=0.1)
        for pool in ([], [synthetic_item("unique", True)], [synthetic_item("ordinary")],
                     [synthetic_item("one", True), synthetic_item("two", True)]):
            with self.subTest(pool=pool):
                spec["pool"] = pool
                expected = direct_loadouts(spec)
                self.assertEqual(ENGINE.optimize_loadouts(spec, top=1000, workers=1000000), expected)
                count, rows = ENGINE.optimize_loadouts(spec, top=0, workers=1)
                self.assertEqual(count, expected[0])
                self.assertEqual(rows, [[], [], [], []])

    def test_tank_survival_and_double_pressure_match_direct_fights(self):
        spec = body_spec(hp=400, duration=6.5)
        spec["pool"] = [synthetic_item("health", stats=[["hp", 300]]),
                        synthetic_item("damage", stats=[["adPct", 10]])]
        actual = ENGINE.optimize_loadouts(spec, top=1000, workers=1)
        self.assertEqual(actual, direct_loadouts(spec))
        by_combo = {tuple(row[0]): row[2] for group in actual[1] for row in group}
        self.assertEqual(by_combo[()]["aliveTime"], 4)
        self.assertFalse(by_combo[()]["survivalCapped"])
        self.assertEqual(by_combo[(0, 0)]["aliveTime"], 6.5)
        self.assertTrue(by_combo[(0, 0)]["survivalCapped"])
        self.assertEqual(by_combo[(0, 0)]["stressAliveTime"], 5)
        self.assertTrue(by_combo[(0, 0, 0)]["stressCapped"])

    def test_complete_pool_count_determinism_and_existing_full_build_results(self):
        unit = SNAP.unit("Ahri")
        spec = spec_for("Ahri")
        spec["pool"] = [tft.item_spec(SNAP, api, ITEM_FX, unit)
                        for api in tft.pool_items(SNAP, ITEM_FX)]
        expected_count, groups = ENGINE.optimize_loadouts(spec, top=4, workers=1)
        self.assertEqual(len(spec["pool"]), 35)
        self.assertEqual(expected_count, 8436)
        self.assertEqual([len(group) for group in groups], [1, 4, 4, 4])
        full_count, full_rows = ENGINE.run_cell(spec, top=4, workers=1)
        self.assertEqual(full_count, 7770)
        self.assertEqual(groups[3], full_rows)
        for workers in (4, 0):
            with self.subTest(workers=workers):
                self.assertEqual(ENGINE.optimize_loadouts(spec, top=4, workers=workers),
                                 (expected_count, groups))
        random_rows = random.Random(118).sample(
            [row for group in ENGINE.optimize_loadouts(spec, top=10000, workers=1)[1]
             for row in group], 20)
        for combo, opening, result in random_rows:
            with self.subTest(combo=combo):
                actual = ENGINE.simulate(dict(spec, items=[spec["pool"][i] for i in combo]))
                self.assertEqual(actual, (opening, result))


if __name__ == "__main__":
    unittest.main()
