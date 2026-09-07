"""Murkwolf's leap applies its AD and AP contributions once each.

The hand values follow the archived ability footer, independently of the
lookup's erroneous nested AP formula and its precomputed damage array.
"""
import copy
import json
from pathlib import Path
import unittest

import tft
from tft_update import ReviewRequired, reconcile
from test_tft import DUMMY, ENGINE, SNAP, events, immortal, spec_for


API = "TFT18_Murkwolf"
LEAP = "TFTCalculationAttributes.PhysicalDamageCalc1"


class TestMurkwolfLeap(unittest.TestCase):
    def test_base_leap_matches_independent_ad_plus_ap_at_each_star(self):
        unit = SNAP.unit("Murkwolf")
        for star, expected in ((1, 120), (2, 180), (3, 270), (4, 455)):
            with self.subTest(star=star):
                ad = 50 * 1.5 ** (star - 1)
                self.assertEqual(tft.calc_value(unit, LEAP, star, ad, 100, 700, 40, 40), expected)
                self.assertEqual(tft.unit_primary_damage(unit, star), expected)

    def test_ad_and_ap_scale_independently_once(self):
        unit = SNAP.unit("Murkwolf")
        # At two stars the unitemized AD is 75, the AD term is 150,
        # and every 100 AP contributes exactly 30 more physical damage.
        for ad, ap, expected in ((75, 0, 150), (0, 100, 30), (75, 200, 210),
                                 (150, 100, 330), (108.75, 145, 261)):
            with self.subTest(ad=ad, ap=ap):
                self.assertEqual(tft.calc_value(unit, LEAP, 2, ad, ap, 700, 40, 40), expected)

    def test_actual_cast_and_empowered_attacks_use_distinct_damage(self):
        dummy = immortal(DUMMY)
        dummy["slots"] = [dict(slot, armor=0, mr=0) for slot in dummy["slots"]]
        spec = spec_for("Murkwolf", pressure=False, duration=2, dummy=dummy)
        spec["kits"]["base"]["stats"]["initialMana"] = 40
        _, result = ENGINE.simulate(spec, True)
        leaps = events(result, "damage", "leap")
        empowered = events(result, "damage", "empowered")
        self.assertEqual(len(leaps), 1)
        self.assertEqual(leaps[0][2], 180)
        self.assertEqual(len(empowered), 2)
        self.assertTrue(all(hit[2] == 90 for hit in empowered))

    def test_source_evidence_is_preserved_and_other_calcs_are_unchanged(self):
        raw = next(unit for unit in SNAP.raw["units"] if unit["apiName"] == API)
        before = copy.deepcopy(raw)
        unit = SNAP._unit(raw)
        # These erroneous values remain archived as evidence, not used as
        # the expected outcome of the hand-calculation regressions above.
        self.assertEqual(raw["ability"]["attributeCalcs"][LEAP]["values"],
                         [500, 1050, 2250, 6005])
        self.assertIn('row="LeapDamageAD"', raw["ability"]["footer"][1]["desc"])
        self.assertIn('row="LeapDamageAP"', raw["ability"]["footer"][1]["desc"])
        for name, calc in raw["ability"]["attributeCalcs"].items():
            if name != LEAP:
                self.assertEqual(unit["calcs"][name], calc)
        self.assertEqual(raw, before)

    def test_a_numeric_row_override_updates_both_damage_and_card(self):
        snap = copy.deepcopy(SNAP)
        snap.overrides["units"][API]["curve"] = {"LeapDamageAP": [25, 35, 50]}
        raw = next(unit for unit in snap.raw["units"] if unit["apiName"] == API)
        unit = snap._unit(raw)
        for star, expected in ((1, 125), (2, 185), (3, 275), (4, 455)):
            with self.subTest(star=star):
                ad = 50 * 1.5 ** (star - 1)
                self.assertEqual(tft.calc_value(unit, LEAP, star, ad, 100, 700, 40, 40), expected)
                self.assertEqual(tft.unit_primary_damage(unit, star), expected)

    def test_invalid_formula_overrides_fail_explicitly(self):
        invalid = (
            {"missing": [{"row": "LeapDamageAD", "scaling": "AttackDamage"}]},
            {LEAP: []},
            {LEAP: [{"row": "missing", "scaling": "AttackDamage"}]},
            {LEAP: [{"row": "LeapDamageAD", "scaling": "HealthMax"}]},
        )
        raw = next(unit for unit in SNAP.raw["units"] if unit["apiName"] == API)
        for formula in invalid:
            with self.subTest(formula=formula):
                snap = copy.deepcopy(SNAP)
                snap.overrides["units"][API]["damageFormulas"] = formula
                with self.assertRaisesRegex(ValueError, "damage formula override"):
                    snap._unit(raw)

    def test_automatic_refresh_preserves_the_reviewed_formula(self):
        notes = json.loads((Path(SNAP.dir) / "patchnotes.json").read_text())
        before = copy.deepcopy(SNAP.overrides)
        overrides, _ = reconcile(SNAP, SNAP, notes)
        self.assertEqual(overrides["units"][API], before["units"][API])
        self.assertEqual(SNAP.overrides, before)

    def test_formula_override_cannot_conceal_an_upstream_mechanics_change(self):
        candidate = copy.deepcopy(SNAP)
        raw = next(unit for unit in candidate.raw["units"] if unit["apiName"] == API)
        raw["ability"]["attributeCalcs"][LEAP]["terms"][1]["op"] = "multiply"
        notes = json.loads((Path(SNAP.dir) / "patchnotes.json").read_text())
        with self.assertRaisesRegex(ReviewRequired, "formula"):
            reconcile(candidate, SNAP, notes)


if __name__ == "__main__":
    unittest.main()
