"""Regression checks for fair team item search and replacement evidence."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import tft
from tft_comp_items import ItemSearch, identity


class TeamFixture:
    def __init__(self, score, outcomes=None):
        self.score, self.outcomes, self.calls = score, outcomes, []

    def evaluate(self, members, effects, selected, carry, tank, *, split):
        if split != "theory":
            raise AssertionError("non-theory evaluation leaked into item selection")
        self.calls.append(identity(selected))
        wins = self.score(selected)
        outcomes = self.outcomes(selected) if self.outcomes else set(range(int(wins)))
        gunblade = any("DA_HextechGunblade" in o["items"] for o in selected.values())
        return {"metrics": {"theoryScore": wins, "damageDps": 10000 if gunblade else 100},
                "modelRevision": "fixed-theory-model", "units": {},
                "scenarios": [{"key": str(i), "score": 2 if i in outcomes else 1} for i in range(12)]}



class BatchFixture(TeamFixture):
    def __init__(self, score, outcomes=None):
        super().__init__(score, outcomes)
        self.batches = []

    def evaluate_many(self, members, effects, selections, carry, tank, *, split, details):
        if details:
            raise AssertionError("inner item search requested presentation diagnostics")
        self.batches.append([identity(selected) for selected in selections])
        return [self.evaluate(members, effects, selected, carry, tank, split=split) for selected in selections]


class TestTeamItemSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.apis = [cls.snap.unit(name)["api"] for name in
                    ("Akali", "Yorick", "Camille", "Karma", "Cinderling", "Xayah", "Rakan", "Leona")]
        cls.carry, cls.tank = cls.apis[:2]
        cls.members = [{"api": api, "star": 2} for api in cls.apis]

    def starting(self):
        selected = {api: {"items": (), "alpha": False} for api in self.apis}
        selected[self.carry]["items"] = ("DA_Deathblade",) * 2
        selected[self.tank]["items"] = ("DA_WarmogsArmor",) * 2
        return selected

    def optimizer(self, score, anchors=None, outcomes=None):
        return ItemSearch(self.snap, TeamFixture(score, outcomes), self.members, {}, self.carry, self.tank, "single", anchors)

    def test_every_legal_replacement_can_beat_a_protected_shortlist_item(self):
        search = self.optimizer(lambda selected: 8 if "DA_Bloodthirster" in selected[self.carry]["items"] else 6)
        initial = self.starting()
        initial[self.carry]["items"] = ("DA_Deathblade", "DA_HextechGunblade")
        before = deepcopy(initial)
        selected, result, evidence = search.optimize([initial])
        self.assertEqual(initial, before)
        self.assertIn("DA_Bloodthirster", selected[self.carry]["items"])
        self.assertEqual(result["metrics"]["theoryScore"], 8)
        for holder in evidence["holders"]:
            for entry in holder["items"]:
                expected = set()
                for item in search.pool:
                    items = list(selected[holder["api"]]["items"])
                    items[entry["slot"]] = item
                    if item != entry["itemApi"] and search.legal(search.changed(selected, holder["api"], items)):
                        expected.add(item)
                self.assertEqual({row["itemApi"] for row in entry["alternatives"]}, expected)
                self.assertEqual(entry["testedAlternatives"], len(expected))
                self.assertLessEqual(entry["bestScoreDelta"], 0)
        self.assertEqual(evidence["modelRevision"], "fixed-theory-model")
        self.assertEqual(evidence["evaluatedOn"], "theory")

    def test_diagnostics_cannot_promote_an_item_when_capacity_is_equal(self):
        search = self.optimizer(lambda _: 6)
        initial = self.starting()
        selected, _, evidence = search.optimize([initial])
        self.assertEqual(identity(selected), identity(initial))
        self.assertTrue(all(e["equivalentAlternatives"] == e["testedAlternatives"]
                            for h in evidence["holders"] for e in h["items"]))

    def test_selected_pair_can_win_when_neither_single_change_improves(self):
        pair = ("DA_GuinsoosRageblade", "DA_InfinityEdge")
        score = lambda selected: 9 if set(pair) <= set(selected[self.carry]["items"]) else 6
        search = self.optimizer(score, {self.carry: [{"items": pair, "alpha": False}]})
        selected, result, _ = search.optimize([self.starting()])
        self.assertEqual(set(selected[self.carry]["items"]), set(pair))
        self.assertEqual(result["metrics"]["theoryScore"], 9)
        self.assertGreater(search.stats["itemInteractionsCompared"], 0)

    def test_item_can_move_from_support_to_main_without_changing_budget(self):
        initial = self.starting()
        initial[self.tank]["items"] += ("DA_WarmogsArmor",)
        initial[self.apis[2]]["items"] = ("DA_JeweledGauntlet",)
        score = lambda selected: 9 if (len(selected[self.carry]["items"]) == 3
                    and "DA_JeweledGauntlet" in selected[self.carry]["items"]) else 6
        search = self.optimizer(score)
        selected, result, _ = search.optimize([initial])
        self.assertEqual(result["metrics"]["theoryScore"], 9)
        self.assertEqual(sum(len(o["items"]) for o in selected.values()), 6)
        self.assertTrue(search.legal(selected))
        self.assertGreater(search.stats["itemTransfersAndExchangesCompared"], 0)

    def test_complete_tank_loadout_can_escape_a_three_item_plateau(self):
        # Health, resists and healing can work together even when none of
        # the single/two-item substitutions changes the encounter outcome.
        defense = ("DA_WarmogsArmor", "DA_GargoyleStoneplate", "DA_DragonsClaw")
        initial = self.starting()
        initial[self.tank]["items"] = ("DA_Deathblade",) * 3
        search = self.optimizer(lambda selected: 9 if set(defense) <= set(selected[self.tank]["items"]) else 6,
                                {self.tank: [{"items": defense, "alpha": False}]})
        selected, result, _ = search.optimize([initial])
        self.assertEqual(set(selected[self.tank]["items"]), set(defense))
        self.assertEqual(result["metrics"]["theoryScore"], 9)
        self.assertEqual(sum(len(option["items"]) for option in selected.values()), 5)

    def test_multiple_seeds_can_escape_a_three_item_interaction(self):
        goal = ("DA_BlueBuff", "DA_JeweledGauntlet", "DA_RabadonsDeathcap")
        first = self.starting()
        first[self.carry]["items"] = ("DA_Deathblade",) * 3
        first[self.tank]["items"] = ("DA_WarmogsArmor",) * 3
        second = deepcopy(first)
        second[self.carry]["items"] = (*goal[:2], "DA_Deathblade")
        search = self.optimizer(lambda selected: 10 if set(goal) <= set(selected[self.carry]["items"]) else 6)
        selected, result, _ = search.optimize([first, second])
        self.assertEqual(set(selected[self.carry]["items"]), set(goal))
        self.assertEqual(result["metrics"]["theoryScore"], 10)

    def test_equal_scores_can_trade_pressure_profiles_and_evidence_preserves_that(self):
        def outcomes(selected):
            has_red = "DA_RedBuff" in selected[self.carry]["items"]
            return set(range(6, 12) if has_red else range(6))
        search = self.optimizer(lambda _: 6, outcomes=outcomes)
        _, _, evidence = search.optimize([self.starting()])
        holder = next(h for h in evidence["holders"] if h["api"] == self.carry)
        red = next(a for a in holder["items"][0]["alternatives"] if a["itemApi"] == "DA_RedBuff")
        self.assertEqual(red["scoreDelta"], 0)
        self.assertEqual(set(red["degradedScenarios"]), {str(i) for i in range(6)})
        self.assertEqual(set(red["improvedScenarios"]), {str(i) for i in range(6, 12)})

    def test_invalid_allocations_are_rejected(self):
        search = self.optimizer(lambda _: 6)
        initial = self.starting()
        initial[self.carry]["items"] = ("DA_Deathblade",)
        with self.assertRaisesRegex(ValueError, "illegal"):
            search.optimize([initial])

    def test_continuous_score_has_no_perfect_win_ceiling(self):
        search = self.optimizer(lambda selected: 12.001 if "DA_HextechGunblade" in selected[self.carry]["items"] else 12)
        first = self.starting()
        selected, result, evidence = search.optimize([first])
        self.assertIn("DA_HextechGunblade", selected[self.carry]["items"])
        self.assertAlmostEqual(result["metrics"]["theoryScore"], 12.001)
        self.assertTrue(evidence["converged"])
        self.assertGreater(search.stats["itemInteractionsCompared"], 0)

    def test_round_limit_discloses_unfinished_refinement_with_complete_evidence(self):
        search = self.optimizer(lambda selected: 12.001 if "DA_HextechGunblade" in selected[self.carry]["items"] else 12)
        with patch("tft_comp_items.REFINEMENT_ROUND_LIMIT", 0):
            _, _, evidence = search.optimize([self.starting()])
        self.assertFalse(evidence["converged"])
        self.assertTrue(any(item["bestScoreDelta"] > 0 for holder in evidence["holders"] for item in holder["items"]))
        self.assertTrue(all(item["testedAlternatives"] > 0 for holder in evidence["holders"] for item in holder["items"]))

    def test_batch_and_scalar_paths_visit_identical_trials_in_identical_order(self):
        pair = ("DA_GuinsoosRageblade", "DA_InfinityEdge")
        score = lambda selected: 9 if set(pair) <= set(selected[self.carry]["items"]) else 6
        anchors = {self.carry: [{"items": pair, "alpha": False}]}
        scalar = self.optimizer(score, anchors)
        batch = self.optimizer(score, anchors)
        batch.evaluator = BatchFixture(score)
        initial = self.starting()
        self.assertEqual(scalar.optimize([initial]), batch.optimize([initial]))
        self.assertEqual(scalar.evaluator.calls, batch.evaluator.calls)
        self.assertEqual(scalar.stats, batch.stats)
        self.assertTrue(any(len(entries) > 30 for entries in batch.evaluator.batches))

    def test_incomplete_batch_is_rejected_without_caching_partial_answers(self):
        search = self.optimizer(lambda _: 6)
        search.evaluator = BatchFixture(lambda _: 6)
        search.evaluator.evaluate_many = lambda *args, **kwargs: []
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            search.evaluate_many([self.starting()])
        self.assertEqual(search.memo, {})


if __name__ == "__main__":
    unittest.main()
