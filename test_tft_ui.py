"""Display metadata: archived icons and corrected, readable trait details."""
import copy
import json
import os
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import tft


class TestTftUiMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        with patch.object(tft, "load_snapshot", return_value=cls.snap):
            cls.meta = tft.api_meta()

    def test_complete_icon_and_trait_mapping_for_current_roster(self):
        self.assertEqual(len(self.meta["units"]), 65)
        self.assertEqual(len(self.meta["traits"]), 36)
        traits = {t["api"]: t for t in self.meta["traits"]}
        for record in self.meta["units"] + self.meta["traits"]:
            self.assertTrue(record["icon"].startswith("https://raw.communitydragon.org/latest/game/assets/"))
            self.assertTrue(record["icon"].endswith(".png"))
            self.assertEqual(record["icon"], record["icon"].lower())
        for unit in self.meta["units"]:
            self.assertEqual(unit["traitApis"], self.snap.units[unit["api"]]["traitApis"])
            self.assertEqual([traits[api]["name"] for api in unit["traitApis"]], unit["traits"])
            self.assertEqual(unit["traitBonuses"]["bare"], [])
            for context in ("low", "high"):
                for bonus in unit["traitBonuses"][context]:
                    self.assertIn(bonus["api"], unit["traitApis"])
                    self.assertIn(bonus["breakpoint"], traits[bonus["api"]]["levels"])
                    self.assertTrue(traits[bonus["api"]]["modeled"])

    def test_tank_threats_and_debuffs_match_the_simulation_inputs(self):
        self.assertEqual(self.meta["tankDebuffs"], {"wound": .33, "sunder": .3, "shred": .3})
        for threat in self.meta["tankThreats"]:
            dummy = tft.dummies_for(self.snap, threat=threat["key"])
            self.assertEqual(dummy["threat"], threat)
            self.assertEqual(dummy["enemyDebuffs"], self.meta["tankDebuffs"])
            self.assertEqual(self.meta["tankDummies"][threat["key"]], dummy)
            self.assertEqual([s["line"] for s in dummy["slots"]],
                             ["frontline"] * 3 + ["backline"] * 2)
        tank_keys = set(tft.unit_scenarios(self.snap.unit("Leona")))
        self.assertIn("s2-clump-bare", tank_keys)
        self.assertIn("s2-clump-bare-physical", tank_keys)
        self.assertIn("s2-clump-bare-magic", tank_keys)

    def test_recomputing_mixed_preserves_other_threat_caches(self):
        unit = self.snap.unit("Leona")
        key = "s2-clump-bare"
        pool = tft.pool_items(self.snap, tft.load_item_effects(self.snap.set_no))[:2]
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "CACHE_DIR", directory), \
                patch.object(tft, "pool_items", return_value=pool):
            paths = tft.cell_paths(self.snap)
            variant = paths[("leona", key + "-magic")]
            old = os.path.join(directory, f"leona-{key}-{'0' * 16}.json")
            for path in (variant, old):
                with open(path, "w") as f:
                    json.dump({"sentinel": True}, f)
            cell = tft.compute_cell(self.snap, unit, key, paths)
            self.assertEqual(cell["scenario"]["dummy"]["threat"]["key"], "mixed")
            self.assertTrue(os.path.exists(paths[("leona", key)]))
            self.assertFalse(os.path.exists(old))
            with open(variant) as f:
                self.assertEqual(json.load(f), {"sentinel": True})

    def test_exact_asset_mapping_wins_and_ambiguous_names_do_not_guess(self):
        first = {"apiName": "wrong", "name": "Same", "squareIcon": "assets/wrong.tex"}
        second = {"apiName": "DA_Right", "name": "Same", "squareIcon": "None", "tileIcon": "assets/right.tex"}
        snap = SimpleNamespace(units={"TFT18_Right": {"name": "Same", "assets": ["DA_Right"]}},
                               communitydragon={"champions": [first, second]})
        self.assertTrue(tft.ui_icons(snap)[0]["TFT18_Right"].endswith("/assets/right.png"))
        snap.units["TFT18_Right"]["assets"] = []
        self.assertIsNone(tft.ui_icons(snap)[0]["TFT18_Right"])
        snap.communitydragon["champions"] = [second]
        self.assertTrue(tft.ui_icons(snap)[0]["TFT18_Right"].endswith("/assets/right.png"))

    def test_missing_archive_and_invalid_asset_paths_have_graceful_fallbacks(self):
        snap = SimpleNamespace(units={"unit": {"name": "Champion", "assets": []}}, communitydragon={})
        self.assertEqual(tft.ui_icons(snap), ({"unit": None}, {}))
        for asset in (None, "None", "T_18_Akali_Square", "https://example.com/image.png", "assets/../image.tex"):
            self.assertIsNone(tft.cdragon_image_url(asset))
        self.assertEqual(tft.cdragon_image_url("ASSETS/ICONS/Test.TEX"),
                         "https://raw.communitydragon.org/latest/game/assets/icons/test.png")

    def test_descriptions_are_plain_text_and_use_corrected_curves(self):
        for trait in self.meta["traits"]:
            if trait["description"]:
                self.assertNotRegex(trait["description"], r"[<>{}]|TFTCurveTable|TFTAttribute|@|%i:")
                self.assertFalse(trait["description"].endswith(":"))
        hunter = copy.deepcopy(self.snap.traits["DA_18_Hunter"])
        hunter["curve"]["DamageAmp"] = [[0, 0], [1, .075], [4, .075]]
        self.assertIn("7.5% Damage Amp", tft.trait_description(hunter))
        attuned = next(t for t in self.meta["traits"] if t["name"] == "Attuned")
        self.assertIn("7%", attuned["description"])

    def test_tooltip_series_and_unsupported_templates(self):
        trait = {"levels": [2, 4], "curve": {"Power": [[1, .2], [2, .35]]},
                 "desc": 'Gain <TFTCurveTable row="Power" format="percent"/> power.'}
        self.assertEqual(tft.trait_description(trait), "Gain 20/35% power.")
        for desc in ('Gain <TFTCurveTable row="Missing"/> power.',
                     'Gain <TFTCurveTable row="Power" format="unknown"/> power.',
                     'Current: <TFTAttribute attributeID="Stack"/>', 'Gain {Unknown.Localization}.'):
            self.assertIsNone(tft.trait_description(dict(trait, desc=desc)))

    def test_selected_bonuses_include_corrected_values_and_existing_limits(self):
        gromp = next(u for u in self.meta["units"] if u["name"] == "Gromp")
        rift = next(b for b in gromp["traitBonuses"]["high"] if b["api"] == "DA_Riftbeast18")
        self.assertIn("+5% Attack Damage", rift["notes"][0])
        self.assertIn("+5% Attack Speed", rift["notes"][0])
        self.assertTrue(any("Alpha Mark is assumed" in note for note in rift["notes"]))
        akali = next(u for u in self.meta["units"] if u["name"] == "Akali")
        low = next(b for b in akali["traitBonuses"]["low"] if b["api"] == "DA_18_Adaptor")
        high = next(b for b in akali["traitBonuses"]["high"] if b["api"] == "DA_18_Adaptor")
        self.assertIn("+25% Attack Damage", low["notes"][0])
        self.assertIn("+50% Attack Damage", high["notes"][0])

    def test_unmodeled_traits_are_explicit_and_have_no_active_bonuses(self):
        traits = {t["api"]: t for t in self.meta["traits"]}
        self.assertFalse(traits["DA_18_Rival"]["modeled"])
        self.assertIn("not modeled", traits["DA_18_Rival"]["note"])
        for unit in self.meta["units"]:
            for bonuses in unit["traitBonuses"].values():
                self.assertNotIn("DA_18_Rival", [bonus["api"] for bonus in bonuses])


if __name__ == "__main__":
    unittest.main()
