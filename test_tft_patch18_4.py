"""18.4 spell miss refunds under the retained standalone target geometry."""

import unittest

from test_tft import events
from test_tft_audit_carries import fixture, flat, result, rows


class TestSpellMissRefunds(unittest.TestCase):
    def spec(self, name, ratio, *, nearby, geometry="clump", multiplier=1):
        spec = fixture(name, duration=0.4, geometry=geometry,
                       fx=[{"manaMult": multiplier}])
        # Resolve the spell at .25 s, but keep its animation busy past the
        # observation horizon so even a full refunded bar has only one cast.
        spec["unit"]["timing"] = {"base": {"castAnimation": 1.0, "effectAt": 0.25}}
        if ratio is None:
            for kit in spec["kits"].values():
                kit["rows"].pop("ManaRefund", None)
        else:
            rows(spec, ManaRefund=ratio)
        for slot in spec["dummies"]["slots"]:
            slot["nearby"] = nearby
        flat(spec, "PhysicalDamageCalc1" if name == "Ashe" else "PhysicalDamageCalc3", 100)
        return spec

    def test_living_rear_targets_trigger_refund_during_own_cast_lock(self):
        for name, ratio in (("Ashe", 0.7), ("Draven", 0.5)):
            for geometry in ("spread", "clump"):
                for multiplier in (1, 1.15, 2):
                    with self.subTest(name=name, geometry=geometry, multiplier=multiplier):
                        res = result(self.spec(name, ratio, nearby=False,
                                               geometry=geometry, multiplier=multiplier))
                        self.assertEqual(res["castTimes"], [0])
                        self.assertTrue(all(health > 0 for health in res["left"]))
                        self.assertEqual(res["total"], 0)
                        # The opening Caster/Marksman attack adds 7/10 mana.
                        # Only the refund bypasses the still-active cast lock.
                        opening_mana = 7 if name == "Ashe" else 10
                        self.assertAlmostEqual(res["probe"]["mana"],
                                               (opening_mana + 1000 * ratio) * multiplier)
                        self.assertGreater(res["probe"]["lockUntil"], res["t"])

    def test_a_hit_does_not_refund_even_when_its_damage_is_zero(self):
        for name, ratio in (("Ashe", 0.7), ("Draven", 0.5)):
            for damage in (0, 100):
                for geometry in ("spread", "clump"):
                    with self.subTest(name=name, damage=damage, geometry=geometry):
                        spec = self.spec(name, ratio, nearby=True, geometry=geometry)
                        flat(spec, "PhysicalDamageCalc1" if name == "Ashe" else "PhysicalDamageCalc3", damage)
                        res = result(spec)
                        self.assertEqual(res["castTimes"], [0])
                        self.assertAlmostEqual(res["probe"]["mana"], 7 if name == "Ashe" else 10)
                        ability = events(res, "damage", "ability" if name == "Ashe" else "giant axes")
                        self.assertEqual(bool(ability), damage > 0)

    def test_old_snapshots_without_refund_row_keep_old_mana(self):
        for name in ("Ashe", "Draven"):
            for multiplier in (1, 1.15):
                with self.subTest(name=name, multiplier=multiplier):
                    res = result(self.spec(name, None, nearby=False, multiplier=multiplier))
                    self.assertEqual(res["castTimes"], [0])
                    self.assertEqual(res["total"], 0)
                    opening_mana = 7 if name == "Ashe" else 10
                    self.assertAlmostEqual(res["probe"]["mana"], opening_mana * multiplier)

    def test_a_full_refunded_bar_can_start_another_cast_after_animation(self):
        for name, ratio, opening_mana in (("Ashe", 0.7, 7), ("Draven", 0.5, 10)):
            with self.subTest(name=name):
                spec = self.spec(name, ratio, nearby=False, multiplier=2)
                spec["unit"]["timing"] = None
                res = result(spec)
                self.assertEqual(res["castTimes"], [0, 0.25])
                # The first refund fills the bar; the second cast spends it
                # at .25 s and will refund when it lands, beyond this horizon.
                self.assertAlmostEqual(res["probe"]["mana"],
                                       (opening_mana + 1000 * ratio) * 2 - 1000)


if __name__ == "__main__":
    unittest.main()
