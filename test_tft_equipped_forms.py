"""Equipped-form roles, pressure and aura reach across native and cached results."""
from copy import deepcopy
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tft
from tft_unit_profiles import UnitProfiles

SNAP = tft.load_snapshot(18, "18.1d")
ITEM_FX = tft.load_item_effects(18)


class TestEquippedNidalee(unittest.TestCase):
    def spec(self, form, *, auto=True, pressure=False):
        names = ["Spear of Shojin", "Sterak's Gage", "Infinity Edge"] if form == "AD" else [
            "Spear of Shojin", "Rabadon's Deathcap", "Jeweled Gauntlet"]
        unit = SNAP.unit("Nidalee")
        spec = tft.cell_spec(SNAP, unit, 2, "clump", [], tft.dummies_for(SNAP), duration=4,
                             pressure=pressure, items=[SNAP.item(name)["api"] for name in names])
        spec.update(autoPressure=auto, immortal=True)
        return spec

    def test_automatic_pressure_follows_form_and_metadata_uses_effective_range(self):
        for form, kind, objective, pressure in (("AD", "Assassin", "fighter", True),
                                                ("AP", "Marksman", "carry", False)):
            with self.subTest(form=form):
                spec = self.spec(form)
                opening, result = tft.engine().simulate(spec)
                kit = spec["kits"][form]
                expected_range = kit["stats"]["range"] + (kit["rows"].get("AdditionalAttackRange", 0) if form == "AP" else 0)
                self.assertEqual((opening["form"], opening["kind"], opening["objective"], opening["pressure"], opening["range"]),
                                 (form, kind, objective, pressure, expected_range))
                self.assertEqual(result["absorbed"] > 0, pressure)

    def test_explicit_pressure_overrides_are_preserved_in_both_forms(self):
        for form in ("AD", "AP"):
            for pressure in (False, True):
                with self.subTest(form=form, pressure=pressure):
                    spec = self.spec(form, auto=False, pressure=pressure)
                    opening, result = tft.engine().simulate(spec)
                    self.assertEqual(opening["pressure"], pressure)
                    self.assertEqual(result["absorbed"] > 0, pressure)
                    if not pressure:
                        measured, samples = tft.engine().measure_response(spec, [1, 2, 4])
                        self.assertFalse(measured["pressure"])
                        self.assertTrue(all(sample["incomingSpent"] == 0 for sample in samples))

    def test_enumeration_resolves_pressure_independently_for_each_build(self):
        spec = self.spec("AD")
        spec["items"] = []
        spec["pool"] = [tft.item_spec(SNAP, SNAP.item(name)["api"], ITEM_FX, SNAP.unit("Nidalee"))
                        for name in ("Sterak's Gage", "Rabadon's Deathcap", "Spear of Shojin")]
        count, rows = tft.engine().run_cell(spec, 1000, 1)
        self.assertEqual(count, len(rows))
        self.assertEqual({opening["form"] for _, opening, _ in rows}, {"AD", "AP"})
        for _, opening, result in rows:
            self.assertEqual(opening["pressure"], opening["form"] == "AD")
            self.assertEqual(result["absorbed"] > 0, opening["form"] == "AD")

    def test_aura_reach_is_resolved_after_form_selection(self):
        for name, fields in (("Sunfire Cape", ("burnAura",)), ("Evenshroud", ("sunderAura",)),
                              ("Ionic Spark", ("shredAura", "ionicSpark"))):
            for form in ("AD", "AP"):
                with self.subTest(item=name, form=form):
                    spec = self.spec(form, auto=False)
                    spec["items"][0] = tft.item_spec(SNAP, SNAP.item(name)["api"], ITEM_FX, SNAP.unit("Nidalee"))
                    fx = tft.engine().compose_fx(spec)
                    self.assertEqual(fx["form"], form)
                    for field in fields:
                        self.assertEqual(bool(fx[field]), form == "AD", field)
                    opening, _ = tft.engine().simulate(spec)
                    self.assertEqual(opening["range"], fx["range"])



class TestFormDependentItemSpecs(unittest.TestCase):
    def test_form_units_retain_aura_reach_until_items_choose_the_form(self):
        effects = tft.load_item_effects(SNAP.set_no)
        for api, fields in (("DA_Evenshroud", ("sunderAura",)),
                            ("DA_IonicSpark", ("shredAura", "ionicSpark")),
                            ("DA_SunfireCape", ("burnAura",))):
            with self.subTest(item=api):
                spec = tft.item_spec(SNAP, api, effects, SNAP.unit("Nidalee"))
                for field in fields:
                    self.assertNotIn(field, spec)
                    self.assertIn(field + "ByRange", spec)
                for name in ("Ashe", "Brambleback"):
                    static = tft.item_spec(SNAP, api, effects, SNAP.unit(name))
                    self.assertFalse(any(key.endswith("ByRange") for key in static))

    def test_nonowner_burn_suppression_removes_form_dependent_aura_without_mutating_templates(self):
        profiles = UnitProfiles(SNAP, "clump")
        args = (SNAP.unit("Nidalee")["api"], 2, [], ["DA_SunfireCape", "DA_Deathblade", "DA_Deathblade"])
        before = profiles.spec(*args)
        saved = deepcopy(before)
        suppressed = profiles.spec(*args, item_burn=False)
        self.assertTrue(any(item.get("burnAuraByRange") for item in before["items"]))
        self.assertFalse(any(item.get("burnAuraByRange") for item in suppressed["items"]))
        self.assertEqual(before, saved)
        self.assertEqual([item["stats"] for item in before["items"]],
                         [item["stats"] for item in suppressed["items"]])



class TestProfileAndCellForms(unittest.TestCase):
    def test_nidalee_form_metadata_and_explicit_pressure_survive_profile_spec(self):
        profiles = UnitProfiles(SNAP, "clump")
        for item, form, kind, reach in (("DA_Deathblade", "AD", "Assassin", 1),
                                        ("DA_RabadonsDeathcap", "AP", "Marksman", 5)):
            with self.subTest(form=form):
                spec = profiles.spec("TFT18_Nidalee", 2, [], [item] * 3)
                self.assertEqual((spec["unit"]["form"], spec["unit"]["kind"], spec["unit"]["range"]),
                                 (form, kind, reach))
                self.assertFalse(spec["pressure"])
                self.assertFalse(spec["autoPressure"])


    def test_cell_preserves_base_identity_but_reports_pressured_melee_builds(self):
        unit = SNAP.unit("Nidalee")
        key = "s2-clump-bare"
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "pool_items", return_value=["DA_Deathblade"]):
            path = str(Path(directory) / "nidalee.json")
            cell = tft.compute_cell(SNAP, unit, key, {("nidalee", key): path}, prune=False)
        self.assertEqual(cell["objective"], "carry")
        self.assertTrue(cell["pressureByBuild"])
        self.assertIsNone(cell["pressured"])
        row = cell["rows"][0]
        self.assertEqual((row["form"], row["kind"], row["objective"], row["range"]),
                         ("AD", "Assassin", "fighter", 1))
        self.assertTrue(row["pressure"])
        self.assertIn("aliveTime", row)
        self.assertIn("hpLeft", row)
        self.assertTrue(cell["best"]["pressure"])
        self.assertNotIn("damageCurve", cell)
        self.assertNotIn("diagnosticModel", cell)


    def test_malformed_deferred_aura_fields_fail_during_parsing(self):
        unit = SNAP.unit("Nidalee")
        for field, value in (("sunderAuraByRange", [1]), ("shredAuraByRange", [1.1, 2]),
                             ("burnAuraByRange", [.01, 0, 2]), ("ionicSparkByRange", [1, math.nan])):
            spec = tft.cell_spec(SNAP, unit, 2, "clump", [], tft.dummies_for(SNAP), pressure=False)
            spec["items"] = [{"api": "invalid-aura", "name": "invalid-aura", "unique": False,
                              "stats": [], "adds": [], field: value}]
            with self.subTest(field=field), self.assertRaises(ValueError):
                tft.engine().compose_fx(spec)


if __name__ == "__main__":
    unittest.main()
