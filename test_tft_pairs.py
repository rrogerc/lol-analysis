"""Actual two-item fight scores for choosing a carry's first two items.

Run after rebuilding the engine: python3 -m unittest test_tft_pairs -v
"""

import copy
import itertools
import unittest

import tft
from test_tft import DUMMY, ENGINE, ITEM_FX, SNAP, TRAIT_FX, immortal, spec_for
from test_tft_tanks import body_spec


def legal_pairs(pool):
    return [pair for pair in itertools.combinations_with_replacement(range(len(pool)), 2)
            if pair[0] != pair[1] or not pool[pair[0]]["unique"]]


def compact_score(pair, result):
    return (list(pair), result["killTime"], result["total"], result["aliveTime"],
            result["survivalCapped"], result["stressAliveTime"], result["stressCapped"])


def direct_pair_scores(spec):
    rows = []
    for pair in legal_pairs(spec["pool"]):
        single = dict(spec, items=[spec["pool"][i] for i in pair])
        _, result = ENGINE.simulate(single)
        rows.append((pair, result))
    rows.sort(key=lambda row: (tft.rank_key(row[1], spec["unit"]["objective"]),
                              tuple(spec["pool"][i]["api"] for i in row[0])))
    return [compact_score(pair, result) for pair, result in rows]


def with_pool(spec, names):
    unit = SNAP.units[spec["unit"]["api"]]
    spec["pool"] = [tft.item_spec(SNAP, SNAP.item(name)["api"], ITEM_FX, unit)
                    for name in names]
    return spec


def synthetic_item(api, unique=False, stats=()):
    return {"api": api, "name": api, "unique": unique, "stats": list(stats), "adds": []}


class TestPairScores(unittest.TestCase):
    def test_match_individual_fights_across_stars_geometry_traits_and_forms(self):
        names = ["Nashor's Tooth", "Jeweled Gauntlet", "Last Whisper",
                 "Void Staff", "Warmog's Armor"]
        cleared = uncleared = False
        for name in ("Ashe", "Ahri", "Warwick", "Akali"):
            unit = SNAP.unit(name)
            contexts, _ = tft.unit_trait_contexts(SNAP, unit, TRAIT_FX)
            for star, geometry, context in itertools.product((1, 2), ("spread", "clump"),
                                                              ("bare", "low", "high")):
                with self.subTest(unit=name, star=star, geometry=geometry, traits=context):
                    spec = tft.cell_spec(SNAP, unit, star, geometry, contexts[context],
                                         tft.dummies_for(SNAP), item_fx=ITEM_FX, trait_fx=TRAIT_FX)
                    with_pool(spec, names)
                    self.assertEqual(spec["targetDebuffs"], {"sunder": 0.3, "shred": 0.3})
                    expected = direct_pair_scores(spec)
                    scores = ENGINE.score_pairs(spec, workers=1)
                    self.assertEqual(scores, expected)
                    self.assertEqual(len(scores), len(legal_pairs(spec["pool"])))
                    self.assertTrue(all(len(score[0]) == 2 for score in scores))
                    cleared |= any(score[1] is not None for score in scores)
                    uncleared |= any(score[1] is None for score in scores)
        self.assertTrue(cleared)
        self.assertTrue(uncleared)

    def test_team_reduction_remains_redundant_with_pair_item_passives(self):
        for unit, item, effect in (("Ashe", "Last Whisper", "sunderOnHit"),
                                    ("Ahri", "Void Staff", "shredOnHit")):
            with self.subTest(unit=unit):
                spec = with_pool(spec_for(unit, duration=12.0, dummy=immortal(DUMMY)),
                                 [item, "Nashor's Tooth"])
                spec["targetDebuffs"] = {"sunder": 0.3, "shred": 0.3}
                without_passive = copy.deepcopy(spec)
                without_passive["pool"][0].pop(effect)
                expected = ENGINE.score_pairs(spec, 1)
                self.assertEqual(expected, direct_pair_scores(spec))
                self.assertEqual(ENGINE.score_pairs(without_passive, 1), expected)
                spec["targetDebuffs"] = {}
                without_passive["targetDebuffs"] = {}
                self.assertNotEqual(ENGINE.score_pairs(spec, 1),
                                    ENGINE.score_pairs(without_passive, 1))

    def test_pair_has_no_implicit_third_item_or_items_from_single_fight_spec(self):
        spec = spec_for("Ashe", driver="Driver", duration=0.1)
        ad = synthetic_item("ad", stats=[["adPct", 0.5]])
        spec["pool"] = [ad]
        # Single-fight items are a separate input and cannot fill the empty
        # third slot in a pool enumeration.
        spec["items"] = [synthetic_item("extra", stats=[["adPct", 10.0]])]
        pair = ENGINE.score_pairs(spec, 1)
        self.assertEqual(pair, direct_pair_scores(spec))
        self.assertEqual(pair[0][0], [0, 0])
        _, bare = ENGINE.simulate(dict(spec, items=[]))
        _, third = ENGINE.simulate(dict(spec, items=[ad, ad, ad]))
        self.assertAlmostEqual(pair[0][2], 2 * bare["total"])
        self.assertGreater(third["total"], pair[0][2])
        spec["duration"] = 1.5
        longer = ENGINE.score_pairs(spec, 1)
        self.assertEqual(longer, direct_pair_scores(spec))
        self.assertGreater(longer[0][2], pair[0][2])

    def test_unique_constraints_duplicates_and_api_ties(self):
        spec = spec_for("Ashe", duration=0.1)
        spec["pool"] = [synthetic_item("z"), synthetic_item("a", True), synthetic_item("m")]
        expected = sorted(legal_pairs(spec["pool"]),
                          key=lambda pair: tuple(spec["pool"][i]["api"] for i in pair))
        scores = ENGINE.score_pairs(spec)
        self.assertEqual([tuple(score[0]) for score in scores], expected)
        self.assertEqual(scores, direct_pair_scores(spec))
        self.assertIn((0, 0), expected)
        self.assertIn((2, 2), expected)
        self.assertNotIn((1, 1), expected)
        self.assertEqual(len(scores), len(set(tuple(score[0]) for score in scores)))

    def test_complete_pool_is_independent_of_worker_count(self):
        unit = SNAP.unit("Ashe")
        spec = spec_for("Ashe")
        spec["pool"] = [tft.item_spec(SNAP, api, ITEM_FX, unit)
                        for api in tft.pool_items(SNAP, ITEM_FX)]
        expected = ENGINE.score_pairs(spec, 1)
        self.assertGreater(len(expected), 500)
        self.assertEqual({tuple(score[0]) for score in expected}, set(legal_pairs(spec["pool"])))
        for workers in (4, 0):
            with self.subTest(workers=workers):
                self.assertEqual(ENGINE.score_pairs(spec, workers), expected)

    def test_empty_and_small_legal_pools(self):
        spec = spec_for("Ashe", duration=0.1)
        for pool, expected in (([], []),
                               ([synthetic_item("only", True)], []),
                               ([synthetic_item("only")], [[0, 0]]),
                               ([synthetic_item("a", True), synthetic_item("b", True)], [[0, 1]])):
            with self.subTest(pool=pool):
                spec["pool"] = pool
                # A huge request is bounded by the number of nonempty batches.
                scores = ENGINE.score_pairs(spec, workers=1000000)
                self.assertEqual([score[0] for score in scores], expected)
                self.assertEqual(scores, direct_pair_scores(spec))

    def test_tank_score_fields_and_stress_runs_keep_the_same_meaning(self):
        spec = body_spec(hp=400.0, duration=6.5)
        spec["pool"] = [synthetic_item("health", stats=[["hp", 300.0]]),
                        synthetic_item("damage", stats=[["adPct", 10.0]])]
        scores = ENGINE.score_pairs(spec, 1)
        self.assertEqual(scores, direct_pair_scores(spec))
        by_combo = {tuple(score[0]): score for score in scores}
        self.assertEqual(by_combo[(0, 0)][3:], (6.5, True, 5.0, False))
        self.assertEqual(by_combo[(0, 1)][3:], (6.5, True, 4.0, False))
        self.assertEqual(by_combo[(1, 1)][3:], (4.0, False, None, False))


if __name__ == "__main__":
    unittest.main()
