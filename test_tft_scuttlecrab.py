"""Scuttlecrab cannot attack during its burrow.

The attack lock is an adopted rule from Roger's in-game correction. These
regressions preserve the existing mana convention; they do not establish a
separate live-game mana lock. Healing and durability begin at ability land.
"""

import unittest

from test_tft import DUMMY, ENGINE, events, immortal, one_hitter, spec_for


class TestScuttlecrabBurrow(unittest.TestCase):
    def spec(self, *, duration, pressure=False, regen=0.0, dummy=None):
        spec = spec_for("Scuttlecrab", duration=duration, pressure=pressure,
                        dummy=dummy or immortal(DUMMY))
        # Force the opening attack to cast. Two attacks per second would
        # otherwise produce six extra dances during a three-second burrow.
        spec["kits"]["base"]["stats"].update(initialMana=100.0)
        spec["kits"]["base"]["stats"]["as"] = 2.0
        spec["role"]["manaRegen"] = regen
        return spec

    def pressure_spec(self):
        spec = self.spec(duration=3.26, pressure=True,
                         dummy=one_hitter(250.0, period=0.125))
        kit = spec["kits"]["base"]
        # Plenty of missing health for every heal, but enough maximum HP
        # to survive the test and a bar that damage cannot refill in it.
        kit["hpStar"] = 10000.0
        kit["stats"].update(mana=1000.0, initialMana=1000.0)
        return spec

    def test_attacks_stop_for_burrow_and_resume_at_its_end(self):
        for duration, expected in ((3.24, [0.0]), (3.26, [0.0, 3.25])):
            with self.subTest(duration=duration):
                _, result = ENGINE.simulate(self.spec(duration=duration), True)
                self.assertEqual([e[0] for e in events(result, "land")], [0.25])
                self.assertEqual([e[0] for e in events(result, "attack")], expected)
                self.assertEqual([e[0] for e in events(result, "damage", "dance", 0)],
                                 expected)
                self.assertEqual(result["probe"]["castingUntil"], 3.25)

    def test_resolved_duration_starts_after_one_animation(self):
        spec = self.spec(duration=1.76)
        spec["unit"]["castTime"] = 0.125
        spec["kits"]["base"]["rows"]["BurrowDuration"] = 1.5
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([e[0] for e in events(result, "land")], [0.125])
        # Exactly one animation plus the resolved row, with no extra
        # attack period or second animation added when the burrow ends.
        self.assertEqual([e[0] for e in events(result, "attack")], [0.0, 1.625])

    def test_healing_starts_on_land_and_pays_out_during_burrow(self):
        _, result = ENGINE.simulate(self.pressure_spec(), True)
        heals = events(result, "heal", "burrow")
        # 2-star unitemized heal = 400, with 30% initially and the other
        # 70% over three seconds. Incoming hits keep these uncapped.
        self.assertEqual(heals[0][0], 0.25)
        self.assertAlmostEqual(heals[0][2], 120.0)
        self.assertEqual([e[0] for e in heals[1:]],
                         [0.5 + 0.25 * i for i in range(12)])
        for heal in heals[1:]:
            self.assertAlmostEqual(heal[2], 280.0 / 12)
        self.assertAlmostEqual(sum(e[2] for e in heals), 400.0)

    def test_durability_is_active_only_during_the_burrow(self):
        _, result = ENGINE.simulate(self.pressure_spec(), True)
        hits = {e[0]: e[2] for e in events(result, "take", target=0)}
        # 45 armor, 15% burrow durability; no enemy Sunder or item effects.
        ordinary = 250.0 * 100.0 / 145.0
        self.assertAlmostEqual(hits[0.125], ordinary)
        self.assertAlmostEqual(hits[0.25], ordinary * 0.85)
        self.assertAlmostEqual(hits[3.125], ordinary * 0.85)
        self.assertAlmostEqual(hits[3.25], ordinary)
        self.assertEqual([e[0] for e in events(result, "attack")], [0.0, 3.25])

    def test_attack_lock_preserves_existing_mana_lock(self):
        _, result = ENGINE.simulate(self.spec(duration=2.01, regen=10.0), True)
        self.assertEqual(result["casts"], 1)
        self.assertEqual(result["attacks"], 1)
        self.assertEqual(result["probe"]["lockUntil"], 1.0)
        # Five mana from the opening attack overflows; regeneration pays
        # for the second after the original mana lock, while still burrowed.
        self.assertAlmostEqual(result["probe"]["mana"], 5.0 + 10.0)

    def test_full_mana_cannot_recast_before_the_burrow_ends(self):
        _, result = ENGINE.simulate(self.spec(duration=3.6, regen=200.0), True)
        self.assertEqual(result["castTimes"], [0.0, 3.25])
        self.assertEqual([e[0] for e in events(result, "land")], [0.25, 3.5])
        self.assertEqual([e[0] for e in events(result, "attack")], [0.0])
        self.assertEqual(result["probe"]["castingUntil"], 6.5)


if __name__ == "__main__":
    unittest.main()
