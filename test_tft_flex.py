"""Trait search inputs are complete and independent of items and combat."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tft
from tft_board import ELDER_DRAGON, RIFTBEAST
import tft_flex
import tft_lobby


class TestTraitFlexCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.3")

    def test_catalog_contains_every_shop_unit_without_using_combat_or_item_models(self):
        with patch.object(tft, "engine", side_effect=AssertionError("combat")), \
                patch.object(tft, "has_driver", side_effect=AssertionError("combat support")), \
                patch.object(tft, "load_item_effects", side_effect=AssertionError("items")), \
                patch.object(tft, "load_trait_effects", side_effect=AssertionError("trait effects")):
            result = tft_flex.catalog(self.snap)
        self.assertEqual(result["schema"], "tft-trait-flex-v1")
        self.assertEqual((result["level"], result["boardSlots"]), (8, 8))
        self.assertEqual({unit["api"] for unit in result["units"]}, set(self.snap.units))
        self.assertEqual({trait["api"] for trait in result["traits"]}, set(self.snap.traits))
        self.assertNotIn("items", result)
        self.assertTrue(all("items" not in unit for unit in result["units"]))
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_anchor_roles_use_base_line_instead_of_damage_objective_alone(self):
        result = tft_flex.catalog(self.snap)
        units = {unit["name"]: unit for unit in result["units"]}
        for name in ("Amumu", "Lillia", "Malphite", "Sentinel", "Sett", "Brambleback", "Morgana"):
            with self.subTest(name=name):
                self.assertEqual(units[name]["cost"], 4)
                self.assertTrue(units[name]["frontline"])
                self.assertFalse(units[name]["backline"])
        for name in ("Ahri", "Aphelios", "Ezreal", "Nidalee", "Sivir", "Soraka", "Zyra"):
            with self.subTest(name=name):
                self.assertEqual(units[name]["cost"], 4)
                self.assertTrue(units[name]["backline"])
                self.assertFalse(units[name]["frontline"])
        # Her listed Marksman kit's range bonus is intrinsic, without items.
        self.assertEqual(units["Nidalee"]["range"], 5)

    def test_activation_thresholds_preserve_solo_and_shared_tiers(self):
        result = tft_flex.catalog(self.snap)
        traits = {trait["name"]: trait for trait in result["traits"]}
        self.assertEqual(traits["Flora Fatalis"]["levels"], [1, 2])
        self.assertEqual(traits["Flora Fatalis"]["sharedLevels"], [2])
        self.assertEqual(traits["Rival"]["levels"], [1, 2])
        self.assertEqual(traits["Rival"]["sharedLevels"], [2])
        self.assertEqual(traits["Thornmaiden"]["levels"], [1])
        self.assertEqual(traits["Thornmaiden"]["sharedLevels"], [])
        self.assertEqual(traits["Monolith"]["levels"], [1])
        self.assertEqual(traits["Monolith"]["sharedLevels"], [])
        self.assertEqual(traits["Eclipse"]["levels"], [])
        self.assertEqual(traits["Eclipse"]["sharedLevels"], [])
        self.assertEqual(traits["Blossom"]["sharedLevels"], [3, 5, 7, 9, 11])

    def test_slot_and_trait_contributions_preserve_multi_slot_units(self):
        result = tft_flex.catalog(self.snap)
        for unit in result["units"]:
            with self.subTest(unit=unit["name"]):
                self.assertEqual(unit["slotCost"], 2 if unit["api"] == ELDER_DRAGON else 1)
                self.assertEqual(set(unit["traitContributions"]), set(unit["traitApis"]))
                for trait, count in unit["traitContributions"].items():
                    self.assertEqual(count, 2 if unit["api"] == ELDER_DRAGON and trait == RIFTBEAST else 1)

    def test_revision_ignores_items_and_stats_outside_the_unit_search(self):
        before = tft_flex.catalog(self.snap)
        snap = deepcopy(self.snap)
        snap.items.clear()
        snap.units["TFT18_Ahri"]["stats"]["hp"] += 999
        snap.units["TFT18_Ahri"]["stats"]["ad"] += 999
        self.assertEqual(tft_flex.catalog(snap), before)
        snap.traits["DA_18_Spellweaver"]["levels"] = [2, 5, 6]
        self.assertNotEqual(tft_flex.catalog(snap)["revision"], before["revision"])

    def test_duplicate_trait_membership_is_not_double_counted(self):
        snap = deepcopy(self.snap)
        snap.units["TFT18_Ahri"]["traitApis"] *= 2
        self.assertEqual(tft_flex.catalog(snap), tft_flex.catalog(self.snap))
        snap.units["TFT18_Ahri"]["traitApis"].append("missing-trait")
        with self.assertRaisesRegex(ValueError, "unknown trait"):
            tft_flex.catalog(snap)

    def test_shop_model_has_reviewed_odds_bags_and_actual_roster_populations(self):
        shop = tft_flex.catalog(self.snap)["shop"]
        self.assertEqual(shop["costOdds"], [.15, .20, .32, .30, .03])
        self.assertEqual(shop["bagCopies"], [30, 25, 18, 10, 9])
        self.assertEqual(shop["championCounts"], [14, 13, 14, 14, 10])
        self.assertEqual(shop["visibleSlots"], [4, 5])
        self.assertEqual(shop["refreshCost"], 2)
        self.assertTrue(all(source["url"].startswith("https://teamfighttactics.leagueoflegends.com/")
                            for source in shop["sources"]))
        snap = deepcopy(self.snap)
        del snap.units["TFT18_Ahri"]
        self.assertEqual(tft_flex.catalog(snap)["shop"]["championCounts"], [14, 13, 14, 13, 10])

    def test_shop_parameter_changes_revise_the_catalog_and_invalid_models_fail(self):
        model = tft_flex.shop_model(self.snap)
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "set_dir", return_value=directory):
            self.assertNotIn("shop", tft_flex.catalog(self.snap))
            path = Path(directory) / "shop-model.json"
            path.write_text(json.dumps(model))
            original = tft_flex.catalog(self.snap)
            model["visibleSlots"] = [5]
            path.write_text(json.dumps(model))
            self.assertNotEqual(tft_flex.catalog(self.snap)["revision"], original["revision"])
            for key, invalid in (("costOdds", [.15, .20, .32, .30, .50]), ("bagCopies", [30, 25, 18, 2, 9]),
                                 ("visibleSlots", [3]), ("costOdds", [True, 0, 0, 0, 0])):
                with self.subTest(key=key):
                    path.write_text(json.dumps(dict(model, **{key: invalid})))
                    with self.assertRaisesRegex(ValueError, "shop model"):
                        tft_flex.catalog(self.snap)

    def test_lobby_statistics_revise_only_the_trait_catalog(self):
        before = tft_flex.catalog(self.snap)
        baseline_revision = tft.snapshot_revision(self.snap)
        profiles = deepcopy(before["lobbyStats"])
        profiles["profiles"][0]["units"][0]["copiesPerPlayer"] += 0.01
        with patch.object(tft_lobby, "catalog_profiles", return_value=profiles):
            after = tft_flex.catalog(self.snap)
        self.assertNotEqual(before["revision"], after["revision"])
        self.assertEqual(after["shop"], before["shop"])
        self.assertEqual(tft.snapshot_revision(self.snap), baseline_revision)

    def test_bad_lobby_statistics_leave_the_unit_search_and_baseline_available(self):
        with patch.object(tft_lobby, "catalog_profiles", side_effect=ValueError("statistics patch mismatch")):
            result = tft_flex.catalog(self.snap)
        self.assertEqual(len(result["units"]), len(self.snap.units))
        self.assertEqual(result["shop"]["championCounts"], [14, 13, 14, 14, 10])
        self.assertEqual(result["lobbyStats"]["profiles"], [])
        self.assertIn("statistics patch mismatch", result["lobbyStats"]["error"])


if __name__ == "__main__":
    unittest.main()
