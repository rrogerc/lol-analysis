"""Carry audit repairs checked against independent target/time arithmetic.

Archived ability descriptions in data/tft/set18/18.1d supply the mechanics.
Synthetic zero-resist targets isolate target identity, poison lifetime, mana
multipliers and integration. These are regression oracles, not live-game proof
of the retained geometry, animation or projectile approximations.
"""

import unittest

from test_tft import ENGINE, events, spec_for


def fixture(name, *, hp=(10000, 10000, 10000), geometry="clump",
            duration=1.25, form=None, fx=()):
    effects = list(fx)
    if form:
        effects.append({"stats": [["adPct", 0.1]] if form == "AD" else [["ap", 10]]})
    dummy = {"critEv": 1.0, "slots": [
        {"hp": health, "armor": 0, "mr": 0, "ad": 0, "as": 0,
         "ability": 0, "manaMax": 0, "kind": "tank"} for health in hp]}
    spec = spec_for(name, star=2, duration=duration, geometry=geometry,
                    pressure=False, dummy=dummy, fx=effects)
    spec.update(autoPressure=False, enemyDebuffs={}, targetDebuffs={})
    spec["role"] = {"manaRegen": 0, "asPct": 0}
    spec["unit"]["castTime"] = 0.25
    for kit in spec["kits"].values():
        kit.update(baseAd=0, hpStar=10000)
        kit["stats"].update(hp=10000, ad=0, armor=0, mr=0,
                            critChance=0, critMult=1,
                            mana=1000, initialMana=1000)
        kit["stats"]["as"] = 0.01
        for calc in kit["calcs"].values():
            calc["terms"] = [{"type": "flat", "value": 0, "op": "add"}]
    return spec


def flat(spec, name, damage):
    for kit in spec["kits"].values():
        if name in kit["calcs"]:
            kit["calcs"][name]["terms"] = [
                {"type": "flat", "value": damage, "op": "add"}]


def rows(spec, **values):
    for kit in spec["kits"].values():
        kit["rows"].update(values)


def result(spec):
    return ENGINE.simulate(spec, True)[1]


class TestPoisonTargets(unittest.TestCase):
    def test_cassiopeia_second_poison_is_independent_of_clump(self):
        for geometry in ("spread", "clump"):
            with self.subTest(geometry=geometry):
                spec = fixture("Cassiopeia", geometry=geometry, duration=1)
                flat(spec, "MagicDamageCalc1", 150)
                rows(spec, Duration=15)
                res = result(spec)
                # Cast lands at .25: .75 s of 10 DPS on two distinct enemies.
                self.assertEqual(res["left"], [9992.5, 9992.5, 10000])
                self.assertEqual({hit[3] for hit in events(res, "damage", "poison")}, {0, 1})

    def test_cassiopeia_expired_poison_allows_the_same_second_enemy(self):
        for poison_duration in (0.5, 2):
            with self.subTest(poison_duration=poison_duration):
                spec = fixture("Cassiopeia", duration=2.75)
                flat(spec, "MagicDamageCalc1", 100 * poison_duration)
                rows(spec, Duration=poison_duration)
                kit = spec["kits"]["base"]
                kit["stats"].update(mana=7, initialMana=0)
                kit["stats"]["as"] = 0.5
                res = result(spec)
                self.assertEqual(res["castTimes"], [0, 2])
                # Poison lasts through .75 or 2.25; the second cast lands
                # at 2.25. Both the already-expired and exact-expiry cases
                # may poison enemy 1 again. Enemy 2 is never selected.
                expected = 100 * (poison_duration + 0.5)
                self.assertEqual(res["left"], [10000 - expected, 10000 - expected, 10000])

    def test_cassiopeia_skips_an_enemy_whose_poison_is_still_active(self):
        spec = fixture("Cassiopeia", duration=2.75, geometry="spread")
        flat(spec, "MagicDamageCalc1", 1500)
        rows(spec, Duration=15)
        kit = spec["kits"]["base"]
        kit["stats"].update(mana=7, initialMana=0)
        kit["stats"]["as"] = 0.5
        res = result(spec)
        # Two stacked poisons contribute 2.5 + .5 seconds on the primary;
        # the first secondary has 2.5 seconds, the next .5 seconds.
        self.assertEqual(res["left"], [9700, 9750, 9950])


class TestImpactIdentity(unittest.TestCase):
    def test_draven_return_keeps_the_outbound_line_after_a_lethal_hit(self):
        spec = fixture("Draven", hp=(1, 10000), geometry="spread")
        flat(spec, "PhysicalDamageCalc3", 100)
        flat(spec, "GenericCalc2", 25)
        res = result(spec)
        self.assertEqual(res["left"], [0, 10000])
        self.assertEqual([(hit[3], hit[2]) for hit in events(res, "damage", "giant axes")],
                         [(0, 1)])

    def test_draven_living_recipient_takes_both_passes(self):
        spec = fixture("Draven", hp=(10000, 10000), geometry="spread")
        flat(spec, "PhysicalDamageCalc3", 100)
        flat(spec, "GenericCalc2", 25)
        res = result(spec)
        self.assertEqual(res["left"], [9875, 10000])
        self.assertEqual([(hit[3], hit[2]) for hit in events(res, "damage", "giant axes")],
                         [(0, 100), (0, 25)])

    def test_gromp_both_forms_keep_the_explosion_center(self):
        for form in ("AD", "AP"):
            for geometry in ("spread", "clump"):
                with self.subTest(form=form, geometry=geometry):
                    spec = fixture("Gromp", hp=(1, 10000, 10000),
                                   geometry=geometry, duration=1.25, form=form)
                    flat(spec, "PhysicalDamageCalc1", 100)
                    flat(spec, "PhysicalDamageCalc2", 50)
                    flat(spec, "MagicDamageCalc1", 100)
                    flat(spec, "MagicDamageCalc2", 50)
                    rows(spec, PoisonDurationAP=1)
                    sheet, res = ENGINE.simulate(spec, True)
                    self.assertEqual(sheet["form"], form)
                    secondary = 10000 if geometry == "spread" else 9950
                    self.assertEqual(res["left"], [0, secondary, secondary])

    def test_leblanc_and_yunara_select_secondaries_before_primary_dies(self):
        for name, main, splash, label, expected in (
            ("LeBlanc", "MagicDamageCalc1", "MagicDamageCalc2", "splash", [1, 2, 3]),
            ("Yunara", "PhysicalDamageCalc1", "PhysicalDamageCalc2", "split", [1, 2]),
        ):
            for primary_hp in (1, 10000):
                with self.subTest(name=name, primary_hp=primary_hp):
                    spec = fixture(name, hp=(primary_hp, 10000, 10000, 10000))
                    flat(spec, main, 100)
                    flat(spec, splash, 25)
                    res = result(spec)
                    self.assertEqual([hit[3] for hit in events(res, "damage", label)], expected)
                    self.assertTrue(all(hit[2] == 25 for hit in events(res, "damage", label)))

    def test_nearby_splashes_still_do_not_reach_isolated_enemies(self):
        for name, main, splash, label in (
            ("LeBlanc", "MagicDamageCalc1", "MagicDamageCalc2", "splash"),
            ("Yunara", "PhysicalDamageCalc1", "PhysicalDamageCalc2", "split"),
        ):
            with self.subTest(name=name):
                spec = fixture(name, hp=(1, 10000), geometry="spread")
                flat(spec, main, 100)
                flat(spec, splash, 25)
                res = result(spec)
                self.assertEqual(events(res, "damage", label), [])
                self.assertEqual(res["left"], [0, 10000])

    def test_karma_burst_stays_at_the_tether_center_after_early_death(self):
        # This checks that a delayed burst cannot migrate to a newly
        # selected attack target. Retaining the delayed clump burst after
        # early death is the existing assumption, not a live-game oracle
        # for whether early victim death should cancel it entirely.
        for geometry in ("spread", "clump"):
            with self.subTest(geometry=geometry):
                spec = fixture("Karma", hp=(1, 10000, 10000),
                               geometry=geometry, duration=1.25)
                flat(spec, "MagicDamageCalc1", 100)
                flat(spec, "MagicDamageCalc2", 25)
                rows(spec, TetherDuration=1)
                res = result(spec)
                burst = events(res, "damage", "burst")
                expected = [] if geometry == "spread" else [1, 2]
                self.assertEqual([hit[3] for hit in burst], expected)
                self.assertTrue(all(hit[0] == 1.25 and hit[2] == 25 for hit in burst))

    def test_karma_living_center_receives_tether_and_burst(self):
        spec = fixture("Karma", geometry="spread", duration=1.25)
        flat(spec, "MagicDamageCalc1", 100)
        flat(spec, "MagicDamageCalc2", 25)
        rows(spec, TetherDuration=1)
        res = result(spec)
        self.assertEqual(res["left"], [9875, 10000, 10000])
        self.assertEqual([(hit[0], hit[2], hit[3]) for hit in events(res, "damage", "burst")],
                         [(1.25, 25, 0)])


class TestCarryManaAndChannels(unittest.TestCase):
    def test_khazix_isolation_refund_uses_mana_multiplier_during_own_lock(self):
        for multiplier in (1, 1.15, 2):
            for geometry, expected in (("spread", 20), ("clump", 10)):
                with self.subTest(multiplier=multiplier, geometry=geometry):
                    spec = fixture("KhaZix", duration=0.4, geometry=geometry,
                                   fx=[{"manaMult": multiplier}])
                    rows(spec, IsolateManaGrant=10)
                    res = result(spec)
                    # Opening attack grants 10 mana (overflow after the
                    # initial full bar). Isolation adds another 10 at .25,
                    # before the spell's 1s mana lock ends.
                    self.assertEqual(res["castTimes"], [0])
                    self.assertAlmostEqual(res["probe"]["mana"], expected * multiplier)
                    self.assertGreater(res["probe"]["lockUntil"], res["t"])

    def test_pebbles_integrates_the_complete_non_tick_aligned_channel(self):
        spec = fixture("Pebbles", duration=3.1)
        flat(spec, "MagicDamageCalc1", 240)
        rows(spec, PercentManaPerSecond=0.35, MRReduction=0)
        res = result(spec)
        laser = events(res, "damage", "laser")
        self.assertEqual(res["castTimes"], [0])
        self.assertAlmostEqual(sum(hit[2] for hit in laser), 240 / 0.35)
        self.assertAlmostEqual(laser[-1][0], 1 / 0.35)
        self.assertAlmostEqual(laser[-1][2], 240 * (1 / 0.35 - 2.75))

    def test_pebbles_pays_only_elapsed_time_on_the_first_partial_tick(self):
        spec = fixture("Pebbles", duration=0.76)
        flat(spec, "MagicDamageCalc1", 240)
        rows(spec, PercentManaPerSecond=0.35, MRReduction=0)
        kit = spec["kits"]["base"]
        kit["stats"].update(mana=14, initialMana=0)
        kit["stats"]["as"] = 1 / 0.6
        res = result(spec)
        self.assertEqual(res["castTimes"], [0.6])
        laser = events(res, "damage", "laser")
        self.assertEqual(len(laser), 1)
        self.assertEqual(laser[0][0], 0.75)
        self.assertAlmostEqual(laser[0][2], 240 * 0.15)

    def test_pebbles_tick_aligned_endpoint_is_paid_exactly_once(self):
        spec = fixture("Pebbles", duration=3.1)
        flat(spec, "MagicDamageCalc1", 240)
        rows(spec, PercentManaPerSecond=0.4, MRReduction=0)
        res = result(spec)
        laser = events(res, "damage", "laser")
        self.assertEqual(sum(hit[2] for hit in laser), 600)
        self.assertEqual([hit[0] for hit in laser], [i / 4 for i in range(1, 11)])


class TestSivirBounces(unittest.TestCase):
    def sivir(self, hp, *, bounces=2):
        spec = fixture("Sivir", hp=hp)
        flat(spec, "PhysicalDamageCalc1", 2)
        flat(spec, "PhysicalDamageCalc2", 10)
        rows(spec, NumBounces=bounces, BonusKillBounces=3)
        return result(spec)

    def test_initial_blade_kill_extends_the_chain(self):
        res = self.sivir((1, 10000, 10000))
        hits = events(res, "damage", "bounces")
        self.assertEqual([hit[3] for hit in hits], [1, 2, 1, 2, 1])
        self.assertEqual(sum(hit[2] for hit in hits), 50)

    def test_surviving_primary_keeps_the_declared_rotation(self):
        res = self.sivir((10000, 10000, 10000), bounces=5)
        self.assertEqual([hit[3] for hit in events(res, "damage", "bounces")], [1, 2, 0, 1, 2])

    def test_projectile_can_leave_a_dead_target_for_the_last_survivor(self):
        res = self.sivir((1, 10000))
        self.assertEqual([(hit[3], hit[2]) for hit in events(res, "damage", "bounces")], [(1, 10)])
        self.assertEqual(res["left"], [0, 9990])

    def test_bounce_kill_extends_chain_without_rehitting_previous_enemy(self):
        res = self.sivir((10000, 1, 10000))
        self.assertEqual([hit[3] for hit in events(res, "damage", "bounces")], [1, 2, 0, 2, 0])


if __name__ == "__main__":
    unittest.main()
