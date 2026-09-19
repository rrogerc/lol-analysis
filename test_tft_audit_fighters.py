"""Hand-computed impact identity and existing flight-window regressions.

These fixtures do not claim a sourced flight duration or a complete movement
model. They verify the timing and recipients for the explicitly supplied
window and the existing spread/clump layout.
"""
from copy import deepcopy
import unittest

from test_tft import ENGINE, events, one_hitter, spec_for


def flat(dtype, amount):
    return {"dtype": dtype, "terms": [{"type": "flat", "value": amount, "op": "add"}]}


def fixture(name, *, geometry="spread", duration=0.4, opening_cast=False):
    spec = spec_for(name, geometry=geometry, duration=duration, pressure=False)
    spec["unit"].update(kind="Specialist", castTime=0.25)
    spec["role"] = {"manaRegen": 0, "asPct": 0}
    spec["traits"] = []
    spec["items"] = []
    spec["dummies"] = {"slots": [
        {"hp": 10000, "armor": 0, "mr": 0, "nearby": True}
        for _ in range(3)
    ], "critEv": 1}
    for kit in spec["kits"].values():
        kit.update(baseAd=0, hpStar=10000)
        kit["stats"].update(ad=0, hp=10000, armor=0, mr=0,
                            initialMana=1000000 if opening_cast else 0,
                            mana=1000000, critChance=0, critMult=1)
        kit["stats"]["as"] = 0.01
    return spec


def gnar_fixture(**kwargs):
    spec = fixture("Gnar", **kwargs)
    kit = spec["kits"]["base"]
    kit["rows"].update(RagePerAttack=1, RagePerSecond=0, TransformRageMax=1,
                       StunDuration=0)
    for calc, dtype, value in (("HealthCalc1", "magic", 0),
                               ("GenericCalc1", "magic", 0),
                               ("PhysicalDamageCalc3", "physical", 0),
                               ("PhysicalDamageCalc1", "physical", 500),
                               ("PhysicalDamageCalc2", "physical", 70)):
        kit["calcs"][calc] = flat(dtype, value)
    return spec


class TestFighterImpactIdentity(unittest.TestCase):
    def test_elder_splash_keeps_the_original_primary_when_auto_is_lethal(self):
        for geometry, recipients in (("spread", []), ("clump", [1, 2])):
            with self.subTest(geometry=geometry):
                spec = fixture("Elder Dragon", geometry=geometry, duration=0.05)
                kit = spec["kits"]["base"]
                kit["baseAd"] = kit["stats"]["ad"] = 100
                kit["rows"]["AttackAoERatio"] = 0.75
                spec["dummies"]["slots"][0]["hp"] = 50
                _, result = ENGINE.simulate(spec, True)
                self.assertEqual([(e[3], e[2]) for e in events(result, "damage", "auto")], [(0, 50)])
                self.assertEqual([(e[3], e[2]) for e in events(result, "damage", "splash")],
                                 [(i, 75) for i in recipients])

    def test_gnar_lethal_throw_preserves_both_original_secondary_targets(self):
        for geometry, recipients in (("spread", []), ("clump", [1, 2])):
            with self.subTest(geometry=geometry):
                spec = gnar_fixture(geometry=geometry, opening_cast=True)
                spec["dummies"]["slots"][0]["hp"] = 50
                _, result = ENGINE.simulate(spec, True)
                self.assertEqual([(e[3], e[2]) for e in events(result, "damage", "throw")], [(0, 50)])
                self.assertEqual([(e[3], e[2]) for e in events(result, "damage", "passed through")],
                                 [(i, 70) for i in recipients])

    def test_gnar_transform_does_not_stun_an_unhit_isolated_survivor(self):
        spec = gnar_fixture(duration=0.6)
        spec["pressure"] = True
        spec["kits"]["base"]["calcs"]["PhysicalDamageCalc3"] = flat("physical", 100)
        spec["kits"]["base"]["rows"]["StunDuration"] = 1
        first = {"hp": 50, "armor": 0, "mr": 0, "nearby": True}
        second = {"hp": 10000, "armor": 0, "mr": 0, "nearby": True,
                  "ad": 100, "as": 2, "streams": 1, "attackStart": 0.5}
        spec["dummies"]["slots"] = [first, second]
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([(e[3], e[2]) for e in events(result, "damage", "transform")], [(0, 50)])
        self.assertEqual([(e[0], e[2]) for e in events(result, "take")], [(0.5, 100)])

    def test_diana_orbs_cannot_jump_from_exhausted_nearby_targets_to_far_enemies(self):
        for geometry in ("spread", "clump"):
            with self.subTest(geometry=geometry):
                spec = fixture("Diana", geometry=geometry, opening_cast=True)
                kit = spec["kits"]["base"]
                kit["calcs"]["MagicDamageCalc1"] = flat("magic", 100)
                kit["rows"]["NumOrbs"] = 6
                spec["dummies"]["slots"][0]["hp"] = 50
                for target in spec["dummies"]["slots"][1:]:
                    target["nearby"] = False
                _, result = ENGINE.simulate(spec, True)
                self.assertEqual([(e[3], e[2]) for e in events(result, "damage", "orbs")], [(0, 50)])


class TestElderFlightWindow(unittest.TestCase):
    def test_protection_is_during_first_cast_and_ends_at_landing(self):
        spec = fixture("Elder Dragon", duration=1.1, opening_cast=True)
        spec["unit"]["castTime"] = 0.5
        spec["pressure"] = True
        dummy = one_hitter(100, period=0.25, attackers=1)
        spec["dummies"] = deepcopy(dummy)
        kit = spec["kits"]["base"]
        kit["rows"].update(StunDuration=0, Omnivamp=0, IgniteMaxHealthDamage=0)
        kit["calcs"]["PhysicalDamageCalc2"] = flat("physical", 0)
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual(result["castTimes"], [0])
        self.assertEqual([e[0] for e in events(result, "land")], [0.5])
        self.assertEqual([(e[0], e[2]) for e in events(result, "take")],
                         [(0.5, 100), (0.75, 100), (1.0, 100)])
        self.assertEqual(result["taken"], 300)


if __name__ == "__main__":
    unittest.main()
