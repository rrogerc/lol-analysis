"""An immortal damage probe cannot be removed for repeatable health credit.

The real Gnar driver transforms on its first zero-damage attack. Controlled
rows give it one attack per second, a 10-mana bar and no regeneration, so
casts start at 1, 2, 3, 4 seconds and land a quarter-second later.
"""

import unittest

from test_tft import ENGINE, events, spec_for
from tft_unit_profiles import generic_targets


def gnar_spec(*, immortal=True, targets=1, target_hp=3000, incoming=0,
              transform=100, fx=()):
    dummy = generic_targets(incoming_dps=incoming, physical_share=1,
                            target_hp=target_hp, target_armor=0, target_mr=0,
                            target_count=targets)
    spec = spec_for("Gnar", star=2, geometry="spread" if targets == 1 else "clump",
                    duration=4.5, pressure=incoming > 0, dummy=dummy, fx=fx)
    spec.update(immortal=immortal, autoPressure=False, enemyDebuffs={}, targetDebuffs={})
    spec["role"] = {"manaRegen": 0, "asPct": 0}
    spec["unit"]["castTime"] = 0.25
    kit = spec["kits"]["base"]
    kit.update(baseAd=0, hpStar=1000)
    kit["stats"].update(hp=1000, armor=0, mr=0, ad=0, critChance=0, critMult=1,
                        mana=10, initialMana=0)
    kit["stats"]["as"] = 1
    kit["rows"].update(RagePerAttack=1, TransformRageMax=1, RagePerSecond=0, StunDuration=0)
    for name, damage in (("HealthCalc1", 0), ("GenericCalc1", 0),
                         ("PhysicalDamageCalc3", transform),
                         ("PhysicalDamageCalc1", 200), ("PhysicalDamageCalc2", 50)):
        kit["calcs"][name] = {"dtype": "physical", "terms": [
            {"type": "flat", "value": damage, "op": "add"}]}
    return spec


class TestGnarRemoval(unittest.TestCase):
    def test_last_immortal_target_only_credits_the_real_transform_damage(self):
        spec = gnar_spec()
        _, samples = ENGINE.measure_response(spec, [0, 1, 1.25, 4.5])
        self.assertEqual([sample["damage"] for sample in samples], [100] * 4)
        self.assertEqual([sample["rawDamage"] for sample in samples], [100] * 4)
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual(result["castTimes"], [1, 2, 3, 4])
        self.assertEqual([hit[0] for hit in events(result, "land")], [1.25, 2.25, 3.25, 4.25])
        self.assertEqual(result["total"], 100)
        self.assertEqual(result["left"], [3000])
        self.assertIsNone(result["killTime"])
        self.assertEqual(events(result, "damage", "thrown off"), [])
        self.assertEqual(events(result, "kill"), [])

    def test_finite_last_target_is_still_removed_at_the_first_cast_land(self):
        spec = gnar_spec(immortal=False)
        # measure_response deliberately makes its targets immortal, so
        # finite boundary checks use the separate simulate API.
        totals = [ENGINE.simulate(dict(spec, duration=time), False)[1]["total"]
                  for time in (1, 1.25, 4.5)]
        self.assertEqual(totals, [100, 3000, 3000])
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual(result["total"], 3000)
        self.assertEqual(result["castTimes"], [1])
        self.assertEqual(result["killTime"], 1.25)
        self.assertEqual(result["left"], [0])
        self.assertEqual([(hit[0], hit[2]) for hit in events(result, "damage", "thrown off")],
                         [(1.25, 2900)])
        self.assertEqual(len(events(result, "kill")), 1)

    def test_three_targets_keep_the_normal_throw_and_pass_through_damage(self):
        for immortal in (False, True):
            with self.subTest(immortal=immortal):
                _, result = ENGINE.simulate(gnar_spec(immortal=immortal, targets=3), True)
                # Three 100-damage transforms, then four casts each hitting
                # the target for 200 and two other enemies for 50 each.
                self.assertEqual(result["total"], 3 * 100 + 4 * (200 + 2 * 50))
                self.assertEqual(result["castTimes"], [1, 2, 3, 4])
                self.assertEqual(sum(hit[2] for hit in events(result, "damage", "throw")), 800)
                self.assertEqual(sum(hit[2] for hit in events(result, "damage", "passed through")), 400)
                self.assertEqual(events(result, "damage", "thrown off"), [])
                self.assertIsNone(result["killTime"])

    def test_health_independent_damage_and_prefixes_do_not_gain_from_probe_health_or_extent(self):
        times = [0, 1, 1.25, 4.5]
        for target_hp in (3000, 30000):
            with self.subTest(target_hp=target_hp):
                spec = gnar_spec(target_hp=target_hp)
                _, short = ENGINE.measure_response(spec, times)
                _, long = ENGINE.measure_response(spec, times + [20, 60])
                self.assertEqual(short, long[:len(times)])
                self.assertEqual([sample["damage"] for sample in long], [100] * len(long))
                self.assertEqual([sample["rawDamage"] for sample in long], [100] * len(long))
                self.assertTrue(all(sample["selfHeal"] == 0 for sample in long))

    def test_suppressed_removal_cannot_feed_omnivamp_or_ally_healing(self):
        # No ordinary damage. Incoming attacks remove 100 HP each second;
        # the former fake 3,000-damage removals would heal those losses and
        # also fabricate 1,500 ally-healing potential on every cast.
        spec = gnar_spec(incoming=100, transform=0,
                         fx=[{"stats": [["omnivamp", 0.5]], "allyHealPct": 0.5}])
        _, samples = ENGINE.measure_response(spec, [1.25, 4.5])
        self.assertEqual([sample["damage"] for sample in samples], [0, 0])
        self.assertEqual([sample["hp"] for sample in samples], [900, 600])
        self.assertEqual([sample["incomingSpent"] for sample in samples], [100, 400])
        self.assertEqual([sample["selfHeal"] for sample in samples], [0, 0])
        self.assertEqual([sample["allyHealPotential"] for sample in samples], [0, 0])

    def test_ordinary_true_damage_burn_is_not_suppressed_with_execution(self):
        spec = gnar_spec(transform=0, fx=[{"burnOnHit": [0.01, 4]}])
        _, result = ENGINE.simulate(spec, True)
        # Attacks keep 1%-maximum-HP burn active: 30 DPS for 4.5 seconds.
        self.assertEqual(result["total"], 3000 * 0.01 * 4.5)
        self.assertEqual(sum(hit[2] for hit in events(result, "damage", "burn")), 135)
        self.assertEqual(events(result, "damage", "thrown off"), [])

    def test_unmodified_gnar_is_health_independent_on_immortal_probes(self):
        times = [10, 20, 60]
        totals = []
        for target_hp in (3000, 30000):
            dummy = generic_targets(target_count=1, target_hp=target_hp,
                                    target_armor=100, target_mr=100)
            spec = spec_for("Gnar", star=2, geometry="spread", duration=60,
                            pressure=False, dummy=dummy)
            spec.update(immortal=True, autoPressure=False, enemyDebuffs={}, targetDebuffs={})
            _, samples = ENGINE.measure_response(spec, times)
            totals.append([sample["damage"] for sample in samples])
        self.assertEqual(*totals)
        self.assertGreater(totals[0][-1], 0)
        _, finite = ENGINE.simulate(dict(spec, immortal=False))
        self.assertIsNotNone(finite["killTime"])
        self.assertGreater(finite["breakdown"].get("thrown off", 0), 0)
        self.assertAlmostEqual(finite["total"], 30000, places=8)
        self.assertEqual(finite["left"], [0])


if __name__ == "__main__":
    unittest.main()
