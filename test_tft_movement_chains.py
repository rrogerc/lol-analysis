"""Cross-target chains honor the explicitly supplied average movement delay.

These timings test the chosen benchmark assumption, not measured champion
animations. Zero-delay cases preserve the stationary benchmark's behavior.
"""

import unittest

from test_tft import ENGINE, events, spec_for


def fixture(name, *, hp=(1000, 1000, 1000), delay=0.5, form=None,
            damage=0, cast=False, duration=4, geometry="spread", fx=()):
    effects = list(fx) + ([{"stats": [["ap", 1]]}] if form == "AP" else [])
    dummy = {"critEv": 1.0, "slots": [
        {"hp": health, "armor": 0, "mr": 0, "nearby": True,
         "ad": 0, "as": 0, "ability": 0, "manaMax": 0}
        for health in hp]}
    spec = spec_for(name, star=2, geometry=geometry, duration=duration,
                    pressure=False, dummy=dummy, fx=effects)
    spec.update(immortal=False, autoPressure=False, meleeRepositionSeconds=delay,
                enemyDebuffs={}, targetDebuffs={}, traits=[])
    spec["unit"].update(kind="Specialist", castTime=0.25)
    spec["role"] = {"manaRegen": 0, "asPct": 0}
    for kit in spec["kits"].values():
        kit.update(baseAd=damage, hpStar=10000)
        kit["stats"].update(hp=10000, ad=damage, armor=0, mr=0,
                            critChance=0, critMult=1, range=1,
                            mana=1000000, initialMana=1000000 if cast else 0)
        kit["stats"]["as"] = 0.01 if cast else 1
        for calc in kit["calcs"].values():
            calc["terms"] = [{"type": "flat", "value": 0, "op": "add"}]
    return spec


def flat(spec, name, damage):
    for kit in spec["kits"].values():
        if name in kit["calcs"]:
            kit["calcs"][name]["terms"] = [{"type": "flat", "value": damage, "op": "add"}]


def akali(**kwargs):
    spec = fixture("Akali", form="AP", cast=True, **kwargs)
    flat(spec, "MagicDamageCalc1", 100)
    for kit in spec["kits"].values():
        kit["rows"].update(TankDamageMultiplierAP=1, RecastDamageReduction=0.7)
    return spec


def yi(**kwargs):
    spec = fixture("Master Yi", damage=100, **kwargs)
    flat(spec, "MagicDamageCalc1", 50)
    flat(spec, "AttackSpeedCalc1", 0)  # isolate the third-attack continuation
    return spec


def hits(result, source):
    return [(hit[0], hit[3], hit[2]) for hit in events(result, "damage", source)]


class TestAkaliMovementChain(unittest.TestCase):
    def test_recasts_wait_for_each_arrival_without_an_extra_cast_window(self):
        for delay, times in ((0, [0.25, 0.25, 0.25]), (0.5, [0.75, 1.25, 1.75])):
            with self.subTest(delay=delay):
                sheet, result = ENGINE.simulate(akali(hp=(50, 50, 1000), delay=delay), True)
                self.assertEqual(sheet["form"], "AP")
                damage = hits(result, "ability")
                self.assertEqual([(time, target) for time, target, _ in damage], list(zip(times, [0, 1, 2])))
                for (_, _, amount), expected in zip(damage, [50, 50, 49], strict=True):
                    self.assertAlmostEqual(amount, expected)
                self.assertEqual(result["castTimes"], [delay])
                self.assertEqual(result["casts"], 1)

    def test_delayed_chain_preserves_its_existing_four_volley_limit(self):
        _, result = ENGINE.simulate(akali(hp=(1, 1, 1, 1, 1000), duration=3), True)
        self.assertEqual(hits(result, "ability"),
                         [(0.75, 0, 1), (1.25, 1, 1), (1.75, 2, 1), (2.25, 3, 1)])
        self.assertEqual(result["left"][-1], 1000)
        self.assertEqual(result["casts"], 1)

    def test_next_victim_dying_in_transit_preserves_recast_power_and_waits_again(self):
        spec = akali(hp=(50, 50, 1000), geometry="clump", duration=1.6,
                     fx=[{"thorns": [60, 10]}])
        spec["pressure"] = True
        spec["dummies"]["slots"][2].update(ad=10, **{"as": 1}, attackStart=1.0)
        _, result = ENGINE.simulate(spec, True)
        # First volley kills at .75. Thorns kills the destination at 1.0,
        # replacing the 1.25 arrival with 1.5. It does not consume a volley.
        self.assertEqual(hits(result, "ability"), [(0.75, 0, 50), (1.5, 2, 70)])
        self.assertEqual([(e[0], e[3]) for e in events(result, "kill")], [(0.75, 0), (1.0, 1)])

    def test_dead_akali_does_not_finish_a_queued_recast(self):
        spec = akali(hp=(50, 1000), duration=2)
        spec["pressure"] = True
        for kit in spec["kits"].values():
            kit["hpStar"] = kit["stats"]["hp"] = 50
        spec["dummies"]["slots"][1].update(ad=100, **{"as": 1}, attackStart=1.0)
        _, result = ENGINE.simulate(spec, True)
        self.assertTrue(result["died"])
        self.assertEqual(hits(result, "ability"), [(0.75, 0, 50)])


class TestBramblebackMovementLeap(unittest.TestCase):
    def test_lethal_leaps_chain_at_arrivals_and_not_initial_engagement(self):
        for delay, times in ((0, [0, 0]), (0.5, [1.0, 1.5])):
            with self.subTest(delay=delay):
                spec = fixture("Brambleback", hp=(100, 100, 100), damage=100, delay=delay)
                flat(spec, "PhysicalDamageCalc1", 100)
                _, result = ENGINE.simulate(spec, True)
                self.assertEqual(hits(result, "auto"), [(delay, 0, 100)])
                self.assertEqual(hits(result, "leap"), list(zip(times, [1, 2], [100, 100])))
                self.assertEqual(result["killTime"], times[-1])

    def test_collateral_kill_does_not_trigger_a_leap_or_more_movement(self):
        spec = fixture("Brambleback", hp=(1000, 50, 1000), geometry="clump", duration=1.6,
                       fx=[{"thorns": [100, 10]}])
        flat(spec, "PhysicalDamageCalc1", 100)
        spec["pressure"] = True
        spec["dummies"]["slots"][0].update(ad=10, **{"as": 1}, attackStart=0.75)
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([(e[0], e[3]) for e in events(result, "kill")], [(0.75, 1)])
        self.assertEqual(hits(result, "leap"), [])
        self.assertEqual([(e[0], e[3]) for e in events(result, "move")], [(0, 0)])
        self.assertEqual([e[0] for e in events(result, "attack")], [0.5, 1.5])


class TestMasterYiMovementDoubleStrike(unittest.TestCase):
    def test_cross_target_second_hit_waits_in_both_forms(self):
        for form in ("AD", "AP"):
            for delay, second_at in ((0, 2), (0.5, 3)):
                with self.subTest(form=form, delay=delay):
                    spec = yi(hp=(300, 1000), form=form, delay=delay, duration=second_at + 0.1)
                    sheet, result = ENGINE.simulate(spec, True)
                    self.assertEqual(sheet["form"], form)
                    self.assertEqual(hits(result, "auto"),
                                     [(delay, 0, 100), (1 + delay, 0, 100), (2 + delay, 0, 100)])
                    expected = [(second_at, 1, 100)] + ([(second_at, 1, 50)] if form == "AP" else [])
                    self.assertEqual(hits(result, "double strike"), expected)
                    self.assertEqual(result["attacks"], 3)

    def test_same_target_second_hit_has_no_extra_delay(self):
        for form in ("AD", "AP"):
            with self.subTest(form=form):
                _, result = ENGINE.simulate(yi(form=form, duration=2.6), True)
                expected = [(2.5, 0, 100)] + ([(2.5, 0, 50)] if form == "AP" else [])
                self.assertEqual(hits(result, "double strike"), expected)
                self.assertEqual([(e[0], e[3]) for e in events(result, "move")], [(0, 0)])

    def test_second_hit_follows_a_replaced_arrival_once(self):
        spec = yi(hp=(300, 50, 1000), form="AP", geometry="clump", duration=3.4,
                  fx=[{"thorns": [60, 10]}])
        spec["pressure"] = True
        spec["dummies"]["slots"][2].update(ad=10, **{"as": 1}, attackStart=2.75)
        _, result = ENGINE.simulate(spec, True)
        # The third ordinary attack kills at 2.5. Thorns kills the next
        # victim during travel, so the continuation arrives at 3.25 once.
        self.assertEqual(hits(result, "double strike"), [(3.25, 2, 100), (3.25, 2, 50)])
        self.assertEqual([(e[0], e[3]) for e in events(result, "kill")], [(2.5, 0), (2.75, 1)])


if __name__ == "__main__":
    unittest.main()
