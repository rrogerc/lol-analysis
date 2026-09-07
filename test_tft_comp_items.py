"""Regression checks for fair team item search and replacement evidence."""
from copy import deepcopy
import unittest

import tft
from tft_comp_items import ItemSearch, identity


class TeamFixture:
    def __init__(self, score, outcomes=None):
        self.score, self.outcomes, self.calls = score, outcomes, []

    def evaluate(self, members, effects, selected, carry, tank, *, split):
        if split != "search":
            raise AssertionError("held-out fights leaked into item selection")
        self.calls.append(identity(selected))
        wins = self.score(selected)
        outcomes = self.outcomes(selected) if self.outcomes else set(range(wins))
        gunblade = any("DA_HextechGunblade" in o["items"] for o in selected.values())
        return {"metrics": {"benchmarkWins": wins, "benchmarkCount": 12, "benchmarkWinRate": wins / 12,
                            "hpMargin": 1.0 if gunblade else 0.1, "clearTime": 1 if gunblade else 20},
                "poolRevision": "fixed-search-pool", "poolSplit": "search", "units": {},
                "matchups": [{"key": str(i), "outcome": "win" if i in outcomes else "loss"} for i in range(12)]}


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
                    ("Akali", "Yorick", "Camille", "Karma", "Varus", "Xayah", "Rakan", "Leona")]
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
        self.assertEqual(result["metrics"]["benchmarkWins"], 8)
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
                self.assertLessEqual(entry["bestWinDelta"], 0)
        self.assertEqual(evidence["poolRevision"], "fixed-search-pool")
        self.assertEqual(evidence["evaluatedOn"], "search")

    def test_health_and_speed_cannot_promote_gunblade_when_wins_are_equal(self):
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
        self.assertEqual(result["metrics"]["benchmarkWins"], 9)
        self.assertGreater(search.stats["pairedItemChangesCompared"], 0)

    def test_item_can_move_from_support_to_main_without_changing_budget(self):
        initial = self.starting()
        initial[self.tank]["items"] += ("DA_WarmogsArmor",)
        initial[self.apis[2]]["items"] = ("DA_JeweledGauntlet",)
        score = lambda selected: 9 if (len(selected[self.carry]["items"]) == 3
                    and "DA_JeweledGauntlet" in selected[self.carry]["items"]) else 6
        search = self.optimizer(score)
        selected, result, _ = search.optimize([initial])
        self.assertEqual(result["metrics"]["benchmarkWins"], 9)
        self.assertEqual(sum(len(o["items"]) for o in selected.values()), 6)
        self.assertTrue(search.legal(selected))
        self.assertGreater(search.stats["itemTransfersAndExchangesCompared"], 0)

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
        self.assertEqual(result["metrics"]["benchmarkWins"], 10)

    def test_equal_scores_can_exchange_matchups_and_evidence_preserves_that(self):
        def outcomes(selected):
            has_red = "DA_RedBuff" in selected[self.carry]["items"]
            return set(range(6, 12) if has_red else range(6))
        search = self.optimizer(lambda _: 6, outcomes=outcomes)
        _, _, evidence = search.optimize([self.starting()])
        holder = next(h for h in evidence["holders"] if h["api"] == self.carry)
        red = next(a for a in holder["items"][0]["alternatives"] if a["itemApi"] == "DA_RedBuff")
        self.assertEqual(red["winDelta"], 0)
        self.assertEqual(set(red["lostMatchups"]), {str(i) for i in range(6)})
        self.assertEqual(set(red["gainedMatchups"]), {str(i) for i in range(6, 12)})

    def test_invalid_allocations_are_rejected(self):
        search = self.optimizer(lambda _: 6)
        initial = self.starting()
        initial[self.carry]["items"] = ("DA_Deathblade",)
        with self.assertRaisesRegex(ValueError, "illegal"):
            search.optimize([initial])

    def test_perfect_score_still_checks_every_single_but_skips_unbeatable_branches(self):
        search = self.optimizer(lambda _: 12)
        first = self.starting()
        second = deepcopy(first)
        second[self.carry]["items"] = ("DA_HextechGunblade",) * 2
        selected, _, evidence = search.optimize([first, second])
        self.assertEqual(identity(selected), identity(first))
        self.assertTrue(all(entry["testedAlternatives"] == len(search.pool) - 1
                            for holder in evidence["holders"] for entry in holder["items"]))
        self.assertEqual(search.stats["pairedItemChangesCompared"], 0)
        self.assertEqual(search.stats["itemTransfersAndExchangesCompared"], 0)

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
