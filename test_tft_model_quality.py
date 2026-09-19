"""Frozen examples that exposed unrealistic composition recommendations.

These are behavioral regressions, not calibration to live-game win rates.
Keep opponents independent of the fixture and held-out outcomes out of search.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

import tft
import tft_team
from tft_comp_traits import resolve_board_traits


class TestFrontlineItemization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((Path(tft.TFT_DATA_DIR) / "regressions" / "tank-itemization.json").read_text())
        cls.snap = tft.load_snapshot(18, cls.fixture["patch"])
        cls.members = [{"api": unit["api"], "star": unit["star"]} for unit in cls.fixture["units"]]
        cls.effects = resolve_board_traits(cls.snap, cls.members)["effects"]
        cls.original = {unit["api"]: {"items": tuple(unit["itemApis"]), "alpha": False}
                        for unit in cls.fixture["units"]}
        cls.defensive = deepcopy(cls.original)
        for api, items in cls.fixture["defensiveReplacement"].items():
            cls.defensive[api]["items"] = tuple(items)
        evaluator = tft_team.Evaluator(cls.snap, cls.fixture["geometry"])
        cls.before, cls.after = evaluator.evaluate_many(cls.members, cls.effects,
            [cls.original, cls.defensive], cls.fixture["mainCarry"], cls.fixture["mainTank"], details=True)

    def test_defense_converts_losses_into_wins_with_the_same_board_and_budget(self):
        self.assertEqual(sum(len(option["items"]) for option in self.original.values()), 9)
        self.assertEqual(sum(len(option["items"]) for option in self.defensive.values()), 9)
        changed = {api for api in self.original if self.original[api] != self.defensive[api]}
        self.assertEqual(changed, {"TFT18_Malphite", "TFT18_Sentinel"})
        self.assertTrue(all(self.snap.units[api]["objective"] == "tank" for api in changed))
        # The former twelve-fight suite tied these builds. More survivability
        # now earns actual wins despite the offensive build's higher DPS.
        self.assertLess(tft_team.rank_key(self.after), tft_team.rank_key(self.before))
        self.assertLess(self.after["metrics"]["damageDps"], self.before["metrics"]["damageDps"])
        self.assertGreater(self.after["metrics"]["frontlineTime"], self.before["metrics"]["frontlineTime"])
        self.assertEqual(self.before["poolRevision"], self.after["poolRevision"])
        self.assertEqual(self.before["poolSplit"], "search")

    def test_enemy_position_exposes_a_frontline_failure_hidden_at_the_center(self):
        fights = {fight["key"]: fight for fight in self.before["matchups"]}
        better = {fight["key"]: fight for fight in self.after["matchups"]}
        for initiative in (0, 1):
            center = f"aphelios-rapidfire-p0-i{initiative}"
            flank = f"aphelios-rapidfire-p2-i{initiative}"
            self.assertEqual(fights[center]["outcome"], "win")
            self.assertEqual(fights[flank]["outcome"], "loss")
            self.assertEqual(better[flank]["outcome"], "win")
        self.assertEqual(self.before["opponentCount"], 12)
        self.assertEqual(self.before["metrics"]["benchmarkCount"], 72)


if __name__ == "__main__":
    unittest.main()
