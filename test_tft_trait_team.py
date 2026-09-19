"""Shared trait recipients and resistance reduction affect the whole board."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

import tft
import tft_theory as theory
from tft_comp_traits import resolve_board_traits


CAUSTIC = "DA_18_Caustic"
THORNMAIDEN = "DA_18_ZyraUniqueTrait"


class TestTraitTeamResolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def test_thornmaiden_base_durability_reaches_every_member_once(self):
        members = [{"api": api, "star": 2} for api in
                   ("TFT18_Zyra", "TFT18_Leona", "TFT18_Sivir")]
        resolved = resolve_board_traits(self.snap, members)
        for member in members:
            effects = [effect for effect in resolved["effects"][member["api"]]
                       if effect["api"] == THORNMAIDEN]
            with self.subTest(unit=member["api"]):
                self.assertEqual(len(effects), 1)
                self.assertAlmostEqual(effects[0]["durability"], .05)
        self.assertTrue(any("six plants" in note for note in resolved["limitations"]))

    def test_thornmaiden_is_absent_without_zyra_and_never_assumes_six_plants(self):
        resolved = resolve_board_traits(self.snap, [{"api": "TFT18_Leona", "star": 2}])
        self.assertFalse(any(effect["api"] == THORNMAIDEN
                             for effects in resolved["effects"].values() for effect in effects))
        for star in (1, 2):
            resolved = resolve_board_traits(self.snap, [{"api": "TFT18_Zyra", "star": star},
                                                        {"api": "TFT18_Leona", "star": 2}])
            self.assertAlmostEqual(next(effect["durability"] for effect in resolved["effects"]["TFT18_Leona"]
                                        if effect["api"] == THORNMAIDEN), .05)


class TestCausticProviderReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def test_caustic_joins_both_shared_reductions_using_maximum_not_sum(self):
        class ProviderSpecs:
            def spec(self, api, star, effects, items, **kwargs):
                return {"items": [{"sunderAura": .4, "shredOnHit": [.5, 5]}] if items else [],
                        "traits": deepcopy(effects)}

        members = [{"api": api, "star": 2} for api in ("TFT18_KogMaw", "TFT18_Leona")]
        effects = {"TFT18_KogMaw": [{"api": CAUSTIC, "caustic": [.3, 4]}], "TFT18_Leona": []}
        selected = {member["api"]: {"items": []} for member in members}
        evaluator = theory.ReferenceEvaluator(self.snap, "clump", profiles=ProviderSpecs())
        shared, owners = evaluator._providers(members, effects, selected, "TFT18_KogMaw", "TFT18_Leona")
        self.assertEqual(shared, {"target_sunder": .3, "target_shred": .3})
        self.assertEqual(owners, [None, None])
        selected["TFT18_Leona"]["items"] = ["synthetic-stronger-provider"]
        shared, _ = evaluator._providers(members, effects, selected, "TFT18_KogMaw", "TFT18_Leona")
        self.assertEqual(shared, {"target_sunder": .4, "target_shred": .5})


class TestNativeTraitTeamScoring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def test_caustic_improves_another_carry_and_matches_reference(self):
        members = [{"api": api, "star": 2} for api in
                   ("TFT18_KogMaw", "TFT18_Sivir", "TFT18_Leona")]
        effects = resolve_board_traits(self.snap, members)["effects"]
        selected = {member["api"]: {"items": []} for member in members}
        without = deepcopy(effects)
        without["TFT18_KogMaw"] = [effect for effect in without["TFT18_KogMaw"] if effect["api"] != CAUSTIC]
        native = theory.Evaluator(self.snap, "clump")
        args = (selected, "TFT18_Sivir", "TFT18_Leona")
        before = native.evaluate(members, without, *args)
        after = native.evaluate(members, effects, *args)
        expected = theory.ReferenceEvaluator(self.snap, "clump").evaluate(members, effects, *args)
        self.assertEqual(after["sharedUtility"]["sunder"], .3)
        self.assertEqual(after["sharedUtility"]["shred"], .3)
        # 100 armor becomes 70. The untouched physical carry's damage rises
        # by (100 + 100) / (100 + 70), under the declared opening coverage.
        self.assertAlmostEqual(after["units"]["TFT18_Sivir"]["measuredDps"] /
                               before["units"]["TFT18_Sivir"]["measuredDps"], 20 / 17)
        self.assertAlmostEqual(after["metrics"]["theoryScore"], expected["metrics"]["theoryScore"], places=6)
        self.assertEqual(after["sharedUtility"], expected["sharedUtility"])

    def test_thornmaiden_increases_frontline_capacity_for_a_different_champion(self):
        members = [{"api": api, "star": 2} for api in ("TFT18_Zyra", "TFT18_Leona")]
        effects = resolve_board_traits(self.snap, members)["effects"]
        without = deepcopy(effects)
        without["TFT18_Leona"] = [effect for effect in without["TFT18_Leona"] if effect["api"] != THORNMAIDEN]
        selected = {member["api"]: {"items": []} for member in members}
        evaluator = theory.Evaluator(self.snap, "clump")
        args = (selected, "TFT18_Zyra", "TFT18_Leona")
        before = evaluator.evaluate(members, without, *args)
        after = evaluator.evaluate(members, effects, *args)
        for prior, current in zip(before["scenarios"], after["scenarios"], strict=True):
            with self.subTest(scenario=current["key"]):
                self.assertAlmostEqual(current["openingFrontlineEhp"] / prior["openingFrontlineEhp"], 1 / .95)
        self.assertGreater(after["metrics"]["frontlineEhp"], before["metrics"]["frontlineEhp"])


class TestSummonerDocumentedEffects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.hand = tft.load_trait_effects(18)

    def test_mapping_uses_only_rows_referenced_by_active_trait_definition(self):
        raw = json.loads((Path(tft.__file__).parent / "data/tft/set18/18.1d/metatft.json").read_text())
        source = next(trait for trait in raw["traits"] if trait["apiName"] == "DA_18_Summoner")
        mapping = self.hand["DA_18_Summoner"]["summoner"]
        self.assertEqual({value["row"] for value in mapping.values()}, set(source["curveValues"]))
        self.assertIn("Improve each effect by 50%", source["effects"][1]["desc"])
        self.assertNotIn("extraSummons", mapping)
        self.assertNotIn("summonPower", mapping)

    def one_cast(self, name, column):
        from tft_unit_profiles import UnitProfiles
        unit = self.snap.unit(name)
        traits = [tft.trait_spec(self.snap, "DA_18_Summoner", column, self.hand, unit)] if column else []
        spec = UnitProfiles(self.snap, "spread").spec(unit["api"], 2, traits, [],
                                                        target_armor=0, target_mr=0, target_count=1)
        # One opening cast and no second cast within the observation window.
        # The synthetic mana bar isolates summon count and damage/shot bonuses.
        spec["kits"]["base"]["stats"].update(mana=1e6, initialMana=1e6)
        spec.update(duration=18.0, pool=[])
        _, result = tft.engine().simulate(spec, True)
        self.assertEqual(result["casts"], 1)
        return result

    def test_azir_keeps_two_soldiers_with_only_documented_damage_multipliers(self):
        damage = []
        for column in (0, 1, 2):
            result = self.one_cast("Azir", column)
            hits = [event for event in result["trace"] if event[1] == "damage" and event[4] == "soldiers"]
            self.assertEqual(len(hits), 6)
            damage.append(sum(event[2] for event in hits))
        self.assertAlmostEqual(damage[0], 6 * 2 * 69)
        self.assertAlmostEqual(damage[1] / damage[0], 1.45)
        self.assertAlmostEqual(damage[2] / damage[0], 1.675)

    def test_zyra_keeps_two_plants_with_only_documented_extra_attacks(self):
        for column, attacks_per_plant in ((0, 10), (1, 14), (2, 16)):
            with self.subTest(column=column):
                result = self.one_cast("Zyra", column)
                hits = [event for event in result["trace"] if event[1] == "damage" and event[4] == "plants"]
                self.assertEqual(len(hits), 2 * attacks_per_plant)
                self.assertAlmostEqual(sum(event[2] for event in hits), 2 * attacks_per_plant * 55)


if __name__ == "__main__":
    unittest.main()
