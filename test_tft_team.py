"""Symmetric reference evaluation, paired initiatives and outcome accounting."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import tft
import tft_team as team
from tft_comp_traits import resolve_board_traits


class TestTeamBenchmarks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def candidate(self):
        return team.opponent_suite(self.snap, budget=9)[0]

    @staticmethod
    def simulate(spec, trace=False):
        duration = 10.0
        return {"outcome": "win" if spec["initiative"] == 0 else "loss", "duration": duration,
                "damage": len(spec["allies"]) * 100, "damageDps": len(spec["allies"]) * 10,
                "allyHpFraction": .5 if spec["initiative"] == 0 else 0,
                "enemyHpFraction": 0 if spec["initiative"] == 0 else .5, "frontlineTime": 8,
                "allies": [{"api": ally["spec"]["unit"]["api"], "damage": 100, "dps": 10,
                            "aliveTime": 8, "alive": spec["initiative"] == 0} for ally in spec["allies"]]}

    def test_team_specs_keep_actual_roles_and_items_without_free_targets(self):
        evaluator = team.Evaluator(self.snap, "clump", prepared=False)
        member = {"api": self.snap.unit("Teemo")["api"], "star": 2}
        effects = resolve_board_traits(self.snap, [member])["effects"][member["api"]]
        spec = evaluator.spec(member["api"], 2, effects, ["DA_Morellonomicon", "DA_VoidStaff"])
        self.assertEqual(spec["duration"], 30)
        self.assertTrue(spec["pressure"])
        self.assertFalse(spec["immortal"])
        self.assertEqual(spec["targetDebuffs"], {})
        self.assertEqual(spec["enemyDebuffs"], {})
        self.assertEqual(spec["dummies"]["slots"], [])
        self.assertEqual(spec["unit"]["objective"], self.snap.units[member["api"]]["objective"])
        self.assertTrue(spec["items"][0]["burnOnHit"])
        self.assertTrue(spec["items"][1]["shredOnHit"])

    def test_summarized_dps_is_actual_but_hp_time_and_healing_never_rank(self):
        def result(outcome, duration, ally_hp, enemy_hp):
            return {"outcome": outcome, "duration": duration, "damage": 100,
                    "damageDps": 100 / duration, "allyHpFraction": ally_hp, "enemyHpFraction": enemy_hp,
                    "frontlineTime": min(8, duration), "allies": [{"api": "test", "name": "Test",
                        "damage": 100, "aliveTime": min(8, duration), "alive": bool(ally_hp), "healing": 10}]}
        results = [result("win", 10, .5, 0), result("loss", 20, 0, .25)]
        summary = team.summarize(results, [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
        self.assertEqual(summary["metrics"]["benchmarkWins"], 1)
        self.assertEqual(summary["metrics"]["benchmarkCount"], 2)
        self.assertEqual(summary["metrics"]["benchmarkWinRate"], .5)
        self.assertEqual(summary["metrics"]["benchmarkScore"], 50)
        self.assertEqual(summary["metrics"]["hpMargin"], .125)
        self.assertEqual(summary["metrics"]["clearTime"], 10)
        self.assertEqual(summary["metrics"]["damageDps"], 7.5)
        self.assertEqual(summary["units"]["test"]["dps"], 7.5)
        self.assertEqual(summary["units"]["test"]["aliveTime"], 8)
        changed = deepcopy(summary)
        changed["metrics"].update(hpMargin=1, clearTime=1, damageDps=100000)
        changed["units"]["test"]["healing"] = 100000
        self.assertEqual(team.rank_key(changed), team.rank_key(summary))
        lower_wins = deepcopy(changed)
        lower_wins["metrics"]["benchmarkWins"] = 0
        self.assertLess(team.rank_key(summary), team.rank_key(lower_wins))
        equivalent_rate = dict(summary["metrics"], benchmarkWins=3, benchmarkCount=6)
        self.assertEqual(team.rank_key(summary), team.rank_key(equivalent_rate))

    def test_timeouts_are_not_wins_even_with_more_remaining_health(self):
        result = {"outcome": "timeout", "duration": 30, "damage": 100, "damageDps": 100 / 30,
                  "allyHpFraction": 1, "enemyHpFraction": .001, "frontlineTime": 30, "allies": []}
        summary = team.summarize([result], [{"key": "timeout"}])
        self.assertEqual(summary["metrics"]["benchmarkWins"], 0)
        self.assertEqual(summary["metrics"]["benchmarkTimeouts"], 1)
        self.assertEqual(summary["metrics"]["benchmarkScore"], 0)
        self.assertEqual(summary["metrics"]["clearTime"], 30)
        draw = team.summarize([dict(result, outcome="draw", allyHpFraction=0, enemyHpFraction=0)], [{"key": "draw"}])
        self.assertEqual(draw["metrics"]["benchmarkWins"], 0)
        self.assertEqual(draw["metrics"]["benchmarkDraws"], 1)
        self.assertEqual(draw["metrics"]["benchmarkScore"], 0)
        with self.assertRaisesRegex(ValueError, "exactly one label"):
            team.summarize([result], [])

    def test_immediate_clear_keeps_unit_and_team_dps_consistent(self):
        result = {"outcome": "win", "duration": 0, "damage": 150, "damageDps": 600,
                  "allyHpFraction": 1, "enemyHpFraction": 0, "frontlineTime": 0,
                  "allies": [{"api": "a", "damage": 100, "alive": True},
                             {"api": "b", "damage": 50, "alive": True, "dps": 200}]}
        summary = team.summarize([result], [{"key": "a", "label": "A"}])
        self.assertEqual(summary["units"]["a"]["dps"], 400)
        self.assertEqual(sum(unit["dps"] for unit in summary["units"].values()),
                         summary["metrics"]["damageDps"])

    def test_each_reference_uses_identical_positions_for_both_initiatives(self):
        evaluator = team.Evaluator(self.snap, "clump", prepared=False)
        encounters = evaluator.encounters(9)
        self.assertEqual(len(encounters), 12)
        for a, b in zip(encounters[::2], encounters[1::2]):
            self.assertEqual((a["initiative"], b["initiative"]), (0, 1))
            self.assertIs(a["enemies"], b["enemies"])
            self.assertIs(a["label"]["roster"], b["label"]["roster"])
            self.assertEqual(a["label"]["opponentId"], b["label"]["opponentId"])
            self.assertEqual(len(a["enemies"]), 8)
            self.assertEqual(sum(len(actor["spec"]["items"]) for actor in a["enemies"]), 9)
            by_api = {actor["spec"]["unit"]["api"]: actor for actor in a["enemies"]}
            for unit in a["label"]["roster"]:
                self.assertEqual(by_api[unit["api"]]["lane"], unit["lane"])
                self.assertEqual(by_api[unit["api"]]["frontline"], unit["frontline"])

    def test_search_screen_and_validation_are_separate_cache_entries(self):
        evaluator = team.Evaluator(self.snap, "clump", prepared=False)
        board = self.candidate()
        args = (board["members"], board["effects"], board["selected"], board["carry"], board["tank"])
        with patch.object(tft.engine(), "simulate_match", side_effect=self.simulate, create=True) as simulate:
            search = evaluator.evaluate(*args)
            self.assertEqual(simulate.call_count, 12)
            repeated = evaluator.evaluate(*args)
            self.assertIs(search, repeated)
            validation = evaluator.evaluate(*args, split="validation")
            screen = evaluator.evaluate(*args, subset="screen")
        self.assertEqual(simulate.call_count, 24)
        self.assertEqual((search["metrics"]["benchmarkWins"], search["metrics"]["benchmarkCount"]), (6, 12))
        self.assertEqual((validation["metrics"]["benchmarkWins"], validation["metrics"]["benchmarkCount"]), (3, 6))
        self.assertEqual(screen["metrics"]["benchmarkCount"], 6)
        self.assertEqual(validation["poolSplit"], "validation")
        self.assertEqual(search["poolRevision"], validation["poolRevision"])
        self.assertEqual(search["itemBudget"], 9)
        search_ids = {row["opponentId"] for row in search["matchups"]}
        validation_ids = {row["opponentId"] for row in validation["matchups"]}
        self.assertFalse(search_ids & validation_ids)
        self.assertLessEqual({row["opponentId"] for row in screen["matchups"]}, search_ids)

    def test_candidates_cannot_change_opponents_and_result_cache_is_bounded(self):
        evaluator = team.Evaluator(self.snap, "clump", prepared=False)
        board = self.candidate()
        selected = deepcopy(board["selected"])
        changed = deepcopy(selected)
        changed[board["carry"]]["items"] = ("DA_RabadonsDeathcap",) * 3
        args = (board["members"], board["effects"])
        tail = (board["carry"], board["tank"])
        seen = []
        def simulate(spec, trace):
            seen.append((id(spec["enemies"]), spec["initiative"]))
            return self.simulate(spec, trace)
        with patch.object(team, "RESULT_CACHE_LIMIT", 1),                 patch.object(tft.engine(), "simulate_match", side_effect=simulate, create=True):
            a = evaluator.evaluate(*args, selected, *tail)
            b = evaluator.evaluate(*args, changed, *tail)
            evaluator.evaluate(*args, selected, *tail)
        self.assertEqual(len(evaluator.results), 1)
        self.assertEqual(seen[:12], seen[12:24])
        self.assertEqual(seen[:12], seen[24:36])
        self.assertIs(a["matchups"][0]["roster"], b["matchups"][0]["roster"])
        self.assertEqual(a["poolRevision"], b["poolRevision"])

    def test_healing_sensitivity_is_explicit_and_cannot_replace_cached_broad_results(self):
        evaluator = team.Evaluator(self.snap, "clump", prepared=False)
        board = self.candidate()
        args = (board["members"], board["effects"], board["selected"], board["carry"], board["tank"])
        policies = []
        def simulate(spec, trace):
            policies.append((spec["damageHealingFromProcs"], spec["postDeathAllyHealing"]))
            result = self.simulate(spec, trace)
            if not spec["damageHealingFromProcs"]:
                result["outcome"] = "loss"
            return result
        with patch.object(tft.engine(), "simulate_match", side_effect=simulate, create=True) as simulate:
            broad = evaluator.evaluate(*args, split="validation")
            restricted = evaluator.evaluate(*args, split="validation", healing_policy="restricted")
            repeated = evaluator.evaluate(*args, split="validation")
        self.assertIs(broad, repeated)
        self.assertEqual(simulate.call_count, 12)
        self.assertEqual(policies, [(True, True)] * 6 + [(False, False)] * 6)
        self.assertEqual(broad["healingPolicy"], "broad")
        self.assertEqual(restricted["healingPolicy"], "restricted")
        self.assertEqual(broad["poolRevision"], restricted["poolRevision"])
        self.assertEqual(broad["metrics"]["benchmarkWins"], 3)
        self.assertEqual(restricted["metrics"]["benchmarkWins"], 0)
        self.assertEqual(broad["matchups"][0]["roster"], restricted["matchups"][0]["roster"])
        with self.assertRaisesRegex(ValueError, "healing policy"):
            evaluator.evaluate(*args, healing_policy="unknown")


if __name__ == "__main__":
    unittest.main()
