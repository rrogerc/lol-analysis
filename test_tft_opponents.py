"""Legality, provenance and utility of independently authored reference boards."""
from collections import Counter
from copy import deepcopy
import unittest
from unittest.mock import patch

import tft
import tft_board
import tft_team as team
from tft_comp_traits import RIFTBEAST


class TestReferenceOpponents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def test_every_budget_fills_eight_legal_slots_and_exact_item_priorities(self):
        for budget in team.ITEM_BUDGETS:
            for split in ("search", "validation"):
                for board in team.opponent_suite(self.snap, budget=budget, split=split):
                    with self.subTest(budget=budget, board=board["key"]):
                        roster = board["roster"]
                        self.assertEqual(len({u["api"] for u in roster}), len(roster))
                        self.assertEqual(tft_board.slots_used(roster), 8)
                        self.assertEqual((board["level"], board["boardSlots"], board["slotsUsed"], board["unitCount"]),
                                         (8, 8, 8, len(roster)))
                        self.assertTrue(all(u["slotCost"] == tft_board.unit_slots(u["api"]) for u in roster))
                        self.assertEqual(sum(len(u["items"]) for u in roster), budget)
                        self.assertTrue(all(len(u["items"]) <= 3 for u in roster))
                        self.assertTrue(all(u["star"] <= 2 for u in roster if u["cost"] == 4))
                        self.assertTrue(all(u["star"] == 1 for u in roster if u["cost"] == 5))
                        if board["costPlan"] in (1, 2, 4):
                            self.assertFalse(any(u["cost"] == 5 for u in roster))
                        self.assertGreaterEqual(sum(u["cost"] == board["costPlan"] for u in roster), 4)

    def test_distinct_held_out_boards_never_enter_the_screening_subset(self):
        search = team.opponent_suite(self.snap)
        validation = team.opponent_suite(self.snap, split="validation")
        screen = team.opponent_suite(self.snap, subset="screen")
        identities = lambda rows: {row["opponentId"] for row in rows}
        self.assertEqual((len(search), len(validation), len(screen)), (6, 3, 3))
        self.assertFalse(identities(search) & identities(validation))
        self.assertLessEqual(identities(screen), identities(search))
        rosters = {tuple(sorted(u["api"] for u in row["roster"])) for row in search + validation}
        self.assertEqual(len(rosters), 9)
        with self.assertRaisesRegex(ValueError, "held-out"):
            team.opponent_suite(self.snap, split="validation", subset="screen")

    def test_all_contexts_use_the_same_fixed_unscaled_pool(self):
        baseline = team.opponent_suite(self.snap)
        self.assertEqual(baseline, team.opponent_suite(self.snap, "physical"))
        self.assertEqual(baseline, team.opponent_suite(self.snap, "magic"))
        self.assertEqual(team.THREATS, {"mixed": "Reference boards"})
        self.assertTrue(all("pressureDps" not in board for board in baseline))

    def test_actual_native_or_item_sources_supply_antiheal_at_six_items(self):
        evaluator = team.Evaluator(self.snap, "clump")
        for split in ("search", "validation"):
            for board in team.opponent_suite(self.snap, budget=6, split=split):
                with self.subTest(board=board["key"]):
                    actors = evaluator.allies(board["members"], board["effects"], board["selected"], board["carry"], board["tank"])
                    providers = []
                    alphas = []
                    for actor in actors:
                        spec = actor["spec"]
                        effects = spec["items"] + spec["traits"]
                        if spec["unit"]["api"] == "TFT18_Cinderling" or any(e.get("burnOnHit") or e.get("burnAura") for e in effects):
                            providers.append(spec["unit"]["api"])
                        if any(e["api"] == RIFTBEAST and e.get("riftbeast") for e in spec["traits"]):
                            alphas.append(spec["unit"]["api"])
                        self.assertEqual(spec["targetDebuffs"], {})
                        self.assertEqual(spec["enemyDebuffs"], {})
                    self.assertTrue(providers)
                    self.assertLessEqual(len(alphas), 1)
                    self.assertEqual(set(alphas), {api for api, option in board["selected"].items() if option["alpha"]})

    def test_pool_hash_tracks_authored_inputs_and_item_budget(self):
        meta = team.pool_metadata(self.snap)
        self.assertEqual(meta["version"], "18-reference-v2-level8")
        self.assertEqual(len(meta["hash"]), 64)
        self.assertEqual(meta["initiatives"], [0, 1])
        self.assertEqual(meta["authoredForPatch"], "18.1d")
        self.assertEqual((meta["level"], meta["boardSlots"]), (8, 8))
        for board in meta["boards"]:
            self.assertEqual((board["level"], board["boardSlots"], board["slotsUsed"], board["unitCount"]),
                             (8, 8, 8, 8))
        self.assertEqual(len({team.pool_revision(self.snap, budget) for budget in team.ITEM_BUDGETS}), 7)
        changed = deepcopy(self.snap)
        changed._input_hash = "different-resolved-snapshot"
        self.assertNotEqual(team.pool_revision(changed, 9), team.pool_revision(self.snap, 9))

    def test_reference_validation_rejects_illegal_units_items_and_leakage(self):
        data = team.load_pool(self.snap)
        def four_star_cost(board):
            next(u for u in board["units"] if self.snap.units[u["api"]]["cost"] == 4)["star"] = 3
        def too_many_items(board):
            board["itemPriority"][6] = board["itemPriority"][0]
        def duplicate_unit(board):
            board["units"][-1] = deepcopy(board["units"][0])
        def illegal_item(board):
            board["itemPriority"][0][1] = "DA_ThiefsGloves"
        def invented_alpha(board):
            board["alphaHolder"] = board["mainCarry"]
        for mutate in (four_star_cost, too_many_items, duplicate_unit, illegal_item, invented_alpha):
            broken = deepcopy(data)
            mutate(broken["boards"][0])
            with self.subTest(mutate=mutate.__name__), self.assertRaises(ValueError):
                team.validate_pool(self.snap, broken)
        leaked = deepcopy(data)
        held_out = next(board for board in leaked["boards"] if board["split"] == "validation")
        held_out["screen"] = True
        with self.assertRaisesRegex(ValueError, "held-out"):
            team.validate_pool(self.snap, leaked)
        duplicated = deepcopy(data)
        duplicated["boards"][6] = dict(deepcopy(duplicated["boards"][0]), id="held-out-copy", split="validation", screen=False)
        with self.assertRaisesRegex(ValueError, "rosters must be distinct"):
            team.validate_pool(self.snap, duplicated)

    def test_four_cost_reference_board_uses_the_level_eight_support(self):
        data = team.load_pool(self.snap)
        board = next(board for board in data["boards"] if board["id"] == "ezreal-executioner")
        support = next(unit for unit in board["units"] if unit["api"] == "TFT18_Kobuko")
        self.assertEqual(support, {"api": "TFT18_Kobuko", "star": 2,
                                   "frontline": True, "lane": 4, "priority": 5})
        self.assertNotIn("TFT18_Kobuko", {api for api, _ in board["itemPriority"]})
        broken = deepcopy(data)
        old = next(board for board in broken["boards"] if board["id"] == "ezreal-executioner")
        next(unit for unit in old["units"] if unit["api"] == "TFT18_Kobuko").update(api="TFT18_Gnar", star=1)
        with self.assertRaisesRegex(ValueError, "cost distribution"):
            team.validate_pool(self.snap, broken)

    def test_elder_uses_two_of_eight_reference_slots_and_reports_seven_actors(self):
        data = deepcopy(team.load_pool(self.snap))
        board = next(board for board in data["boards"] if board["id"] == "azir-summoner")
        removed = {"TFT18_LeBlanc", "TFT18_Xayah"}
        board["units"] = [unit for unit in board["units"] if unit["api"] not in removed]
        board["units"].append({"api": tft_board.ELDER_DRAGON, "star": 1,
                               "frontline": True, "lane": 2, "priority": 5})
        team.validate_pool(self.snap, data)
        with patch.object(team, "load_pool", return_value=data):
            metadata = next(row for row in team.pool_metadata(self.snap)["boards"] if row["id"] == board["id"])
            self.assertEqual((metadata["level"], metadata["boardSlots"], metadata["slotsUsed"], metadata["unitCount"]),
                             (8, 8, 8, 7))
            evaluator = team.Evaluator(self.snap, "clump")
            encounters = [row for row in evaluator.encounters(9, "validation")
                          if row["label"]["opponentId"] == board["id"]]
        self.assertEqual(len(encounters), 2)
        for encounter in encounters:
            label = encounter["label"]
            self.assertEqual(len(encounter["enemies"]), 7)
            self.assertEqual((label["level"], label["boardSlots"], label["slotsUsed"], label["unitCount"]),
                             (8, 8, 8, 7))
            elder = next(unit for unit in label["roster"] if unit["api"] == tft_board.ELDER_DRAGON)
            self.assertEqual(elder["slotCost"], 2)
        board["units"].append({"api": "TFT18_Xayah", "star": 2,
                               "frontline": False, "lane": 4, "priority": 7})
        with self.assertRaisesRegex(ValueError, "eight board slots"):
            team.validate_pool(self.snap, data)


if __name__ == "__main__":
    unittest.main()
