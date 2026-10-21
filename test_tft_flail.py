"""Flail crit events, post-hit buffs and native periodic opportunity counts.

Fixed damage, guaranteed crits and zero-resist targets give independent
arithmetic oracles. Fractional crit expectations remain a declared model
approximation; these checks do not establish live animation timing.
"""

import json
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

from test_tft import ENGINE, events, spec_for
from test_tft_audit_carries import fixture, flat, result, rows
from test_tft_symmetric import match, events as combat_events
from test_tft_team_engine import ally, flat_calc


FLAIL = {"api": "DA_StrikersFlail", "ampPerCrit": [0.05, 5.0, 4]}


class TestFlail(unittest.TestCase):
    def spell(self, name="Lux", *, precision=True, copies=1, duration=3.1,
              hp=(1000000,), geometry="spread", regeneration=2000, extra_fx=()):
        effects = [FLAIL] * copies + list(extra_fx)
        if precision:
            effects.append({"precision": 1})
        spec = fixture(name, hp=hp, duration=duration, geometry=geometry, fx=effects)
        for kit in spec["kits"].values():
            kit["stats"].update(critChance=1.0, critMult=1.4, **{"as": 1.0})
        # Native attacks cannot occur during this observation; repeated casts
        # use the initially full bar and the specified mana regeneration.
        spec["unit"]["timing"] = {
            "base": {"castAnimation": 0.25, "effectAt": 0.25, "attackDelay": 100.0}}
        spec["role"] = {"manaRegen": regeneration, "asPct": 0.0}
        flat(spec, "MagicDamageCalc1", 100.0)
        return spec

    def basic(self, *, chance=1.0, copies=1, duration=4.01):
        spec = self.spell(precision=False, copies=copies, duration=duration)
        spec["driver"] = "Driver"
        spec["unit"]["timing"] = None
        spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
        for kit in spec["kits"].values():
            kit.update(baseAd=100.0)
            kit["stats"].update(ad=100.0, critChance=chance,
                                mana=1000000000, initialMana=0.0)
        return spec

    def assert_amounts(self, hits, expected):
        self.assertEqual(len(hits), len(expected))
        for hit, amount in zip(hits, expected):
            self.assertAlmostEqual(hit[2], amount)

    def test_basic_crit_grants_amp_after_the_hit_that_earned_it(self):
        res = result(self.basic())
        # A guaranteed crit deals 100 x 1.4. Only earlier crits contribute
        # 5/10/15/20% amp; the first hit cannot use its own new buff.
        self.assert_amounts(events(res, "damage", "auto"), [140, 147, 154, 161, 168])

    def test_fractional_crit_expectation_does_not_buff_the_current_hit(self):
        res = result(self.basic(chance=0.5, duration=2.01))
        # Independent 50% crits give 120 expected base damage and 2.5%
        # expected amp from each previous hit, below the stack cap.
        self.assert_amounts(events(res, "damage", "auto"), [120, 123, 126])

    def test_separate_ability_crits_stack_without_any_attacks(self):
        res = result(self.spell())
        self.assertEqual(res["attacks"], 0)
        self.assert_amounts(events(res, "damage", "ability"), [140, 147, 154, 161])

    def test_abilities_without_precision_cannot_generate_flail_stacks(self):
        res = result(self.spell(precision=False))
        self.assertEqual(res["attacks"], 0)
        self.assert_amounts(events(res, "damage", "ability"), [100] * 4)

    def test_noncritting_item_and_trait_damage_do_not_add_crit_stacks(self):
        for kind, extra, source in (("item", {"burnOnHit": [0.01, 5.0]}, "burn"),
                                    ("trait", {"bonusMagicPct": 0.5}, "solar"),
                                    ("trait", {"bleed": [0.5, 2.0]}, "bleed")):
            with self.subTest(source=source):
                spec = self.spell(extra_fx=[extra] if kind == "item" else [])
                if kind == "trait":
                    spec["traits"].append({"api": "test", "name": "test",
                                           "stats": [], **extra})
                res = result(spec)
                self.assertTrue(events(res, "damage", source))
                # The four eligible spell crits still produce exactly the
                # same ramp despite additional item/trait damage events.
                self.assert_amounts(events(res, "damage", "ability"), [140, 147, 154, 161])

    def test_native_noncritting_burn_does_not_buff_noncritical_spells(self):
        spec = self.spell("Cinderling", precision=False)
        flat(spec, "PhysicalDamageCalc1", 100.0)
        rows(spec, BurnAmount=1.0, BurnDuration=5.0)
        res = result(spec)
        self.assertTrue(events(res, "damage", "burn"))
        self.assert_amounts(events(res, "damage", "ability"), [100] * 4)

    def test_each_accepted_target_hit_grants_once_after_its_damage(self):
        spec = self.spell(hp=(1000000,) * 3, geometry="clump", duration=1.1)
        rows(spec, DamageReductionPerUnit=0.0, MinimumFalloffDamageRatio=1.0)
        res = result(spec)
        self.assertEqual(res["attacks"], 0)
        self.assert_amounts(events(res, "damage", "ability"),
                            [140, 147, 154, 161, 168, 168])

    def test_empowered_swipes_do_not_double_proc_from_simulated_attack(self):
        for precision in (False, True):
            with self.subTest(precision=precision):
                spec = self.spell("Aphelios", precision=precision)
                rows(spec, Duration=0.25, NumAttacksBase=1.0,
                     AS_NeededForExtraSwipe=1000.0, NumSwipesTriggerSimulatedAutos=1.0)
                flat(spec, "PhysicalDamageCalc1", 100.0)
                flat(spec, "PhysicalDamageCalc2", 0.0)
                res = result(spec)
                self.assertEqual(res["attacks"], 0)
                self.assert_amounts(events(res, "damage", "swipes"),
                                    [140, 147, 154, 161] if precision else [100] * 4)

    def test_duplicate_flails_share_four_stack_limit_but_each_listens(self):
        res = result(self.basic(copies=2))
        # Each copy applies a 5% stack after a crit; both use the same buff
        # definition, so two crits fill the shared four-stack 20% cap.
        self.assert_amounts(events(res, "damage", "auto"), [140, 154, 168, 168, 168])

    def test_at_cap_refreshes_only_the_oldest_instance(self):
        spec = self.spell(duration=6.0)
        spec["unit"].update(timing=None, castTime=3.75)
        spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
        for kit in spec["kits"].values():
            kit.update(baseAd=100.0)
            kit["stats"].update(ad=100.0, mana=35.0, initialMana=0.0, **{"as": 2.0})
        res = result(spec)
        self.assertEqual(res["castTimes"], [2.0])
        # Crits at 0/.5/1/1.5 fill the cap. The crit at 2 refreshes only
        # the oldest stack to expiry 7; the .5 stack still expires at 5.5.
        # A spell at 5.75 therefore sees three stacks, not two or four.
        hits = events(res, "damage", "ability")
        self.assertEqual([hit[0] for hit in hits], [5.75])
        self.assert_amounts(hits, [161])

    def poison(self, *, period):
        spec = self.spell("Cassiopeia", duration=2.6, regeneration=0.0)
        rows(spec, Duration=2.0)
        flat(spec, "MagicDamageCalc1", 200.0)
        for kit in spec["kits"].values():
            kit["dotCritPeriods"] = {} if period is None else {"poison": period}
        return spec

    def test_poison_procs_follow_half_second_period_not_integration_slices(self):
        res = result(self.poison(period=0.5))
        hits = events(res, "damage", "poison")
        self.assertEqual([hit[0] for hit in hits],
                         [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5])
        # 200 over 2 seconds gives 35 damage per .25s at 100% crit. The
        # first completed native .5s period grants its buff AFTER time 1.
        self.assert_amounts(hits, [35, 35, 36.75, 36.75, 38.5, 38.5, 40.25, 40.25])

    def test_unknown_dot_period_keeps_crit_damage_without_inventing_procs(self):
        res = result(self.poison(period=None))
        self.assert_amounts(events(res, "damage", "poison"), [35] * 8)

    def test_dot_period_must_be_finite_and_positive(self):
        for period in (0.0, -0.5, float("nan"), float("inf")):
            with self.subTest(period=period), self.assertRaises(ValueError):
                ENGINE.simulate(self.poison(period=period), False)

    def test_source_periods_reach_all_equipped_kits(self):
        expected = {
            "Ashe": {"trail": 0.5},
            "Cassiopeia": {"poison": 0.5},
            "Draven": {"bleed axes": 0.5},
            "Fiddlesticks": {"drain": 0.25},
            "Gromp": {"cloud": 1.0},
            "Karma": {"tether": 0.25},
            "Kog'Maw": {"acid": 0.5},
            "Morgana": {"withering zone": 0.25},
        }
        # The three row-derived values resolve native damage timer rows;
        # fixed periods come from the source effects/manual timer. Forms
        # must retain the metadata even when only their AP ability uses it.
        for name, periods in expected.items():
            with self.subTest(name=name):
                spec = spec_for(name)
                for kit in spec["kits"].values():
                    self.assertEqual(kit["dotCritPeriods"], periods)
        for kit in spec_for("Kennen")["kits"].values():
            self.assertNotIn("firestorm", kit.get("dotCritPeriods", {}))

    def test_ashe_trail_terms_share_one_periodic_crit_opportunity(self):
        spec = self.spell("Ashe", hp=(10000,), duration=2.6, regeneration=0.0)
        flat(spec, "PhysicalDamageCalc1", 0.0)
        flat(spec, "PhysicalDamageCalc2", 100.0)
        rows(spec, RiftDuration=2.0, MaxHealthDamagePerSecond=0.01)
        for kit in spec["kits"].values():
            kit["dotCritPeriods"] = {"trail": 0.5}
        res = result(spec)
        totals = {}
        for hit in events(res, "damage", "trail"):
            totals[hit[0]] = totals.get(hit[0], 0.0) + hit[2]
        # Flat 100/sec and 1% of 10,000 HP/sec are one trail hit. Both
        # halves must settle before its one .5s proc can amplify later hits.
        self.assertEqual(list(totals), [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5])
        for amount, expected in zip(totals.values(),
                                    [70, 70, 73.5, 73.5, 77, 77, 80.5, 80.5]):
            self.assertAlmostEqual(amount, expected)

    def test_lethal_compound_trail_hit_still_earns_one_periodic_stack(self):
        spec = self.spell("Ashe", hp=(60.0, 10000.0), geometry="clump",
                          duration=1.26, regeneration=0.0)
        flat(spec, "PhysicalDamageCalc1", 0.0)
        flat(spec, "PhysicalDamageCalc2", 100.0)
        rows(spec, RiftDuration=2.0, MaxHealthDamagePerSecond=0.01)
        for kit in spec["kits"].values():
            kit["dotCritPeriods"] = {"trail": 0.5}
        res = result(spec)
        first = events(res, "damage", "trail", target=0)
        self.assertAlmostEqual(sum(hit[2] for hit in first), 60.0)
        self.assertEqual(res["left"][0], 0.0)
        self.assertGreater(res["left"][1], 0.0)
        # At .75 the first victim loses (100 + .01*60)*1.4*.25 = 35.21.
        # At the due time 1.0 its remaining 24.79 HP is below the flat
        # term's 35 damage. The accepted compound crit must still earn one
        # stack, even though evaluating a later separate term would fail.
        totals = {}
        for hit in events(res, "damage", "trail", target=1):
            totals[hit[0]] = totals.get(hit[0], 0.0) + hit[2]
        self.assertEqual(list(totals), [0.75, 1.0, 1.25])
        # The sibling sees that one earned stack at 1.0; its own periodic
        # crit then earns the second stack for the subsequent slice.
        for amount, expected in zip(totals.values(), [70.0, 73.5, 77.0]):
            self.assertAlmostEqual(amount, expected)

    def test_fractional_overflow_finishes_with_many_concurrent_crit_hits(self):
        script = textwrap.dedent("""
            import json
            from test_tft_flail import TestFlail, events, flat, result, rows

            fixture = TestFlail()
            observations = []
            for chance in (0.6, 0.8, 0.95):
                for copies in (1, 3):
                    spec = fixture.spell('Alune', copies=copies, duration=12.1,
                                         hp=(1000000.0,) * 3, geometry='clump')
                    for kit in spec['kits'].values():
                        kit['stats']['critChance'] = chance
                    rows(spec, NumEnemies=3.0, NumMoonshards=64.0)
                    flat(spec, 'MagicDamageCalc1', 100.0)
                    flat(spec, 'MagicDamageCalc2', 0.0)
                    measured = result(spec)
                    hits = events(measured, 'damage', 'moonshards')
                    baseline = 100.0 * (1.0 + 0.4 * chance)
                    assert len(hits) > 256, (chance, copies, len(hits))
                    assert hits[-1][0] > 6.0
                    assert abs(hits[0][2] - baseline) < 1e-8
                    # Shared four-stack 5% buffs can amplify damage by at
                    # most 20%, including with three listener copies.
                    assert all(baseline - 1e-8 <= hit[2] <= baseline * 1.2 + 1e-8
                               for hit in hits), (chance, copies)
                    observations.append({'chance': chance, 'copies': copies,
                                         'hits': len(hits), 'last': hits[-1][0]})
            print(json.dumps(observations))
        """)
        # The simulator's overflow loop once reselected a refreshed tiny fragment
        # without reducing its floating-point budget. A subprocess bounds
        # that failure and prevents a regression from hanging the test run.
        try:
            completed = subprocess.run([sys.executable, "-c", script],
                                       cwd=Path(__file__).resolve().parent,
                                       capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("Fractional Flail overflow did not finish within 10 seconds")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(len(json.loads(completed.stdout)), 6)

    def test_reciprocal_deferred_crit_is_credited_once_after_settlement(self):
        malphite = ally("Malphite", driver="Malphite", hp=10000.0,
                        ad=100.0, attack_speed=0.5, cast=True, lane=1,
                        fx=[FLAIL, {"precision": 1}])
        stats = malphite["spec"]["kits"]["base"]["stats"]
        stats.update(critChance=1.0, critMult=1.4)
        flat_calc(malphite, "ShieldCalc1", 50.0)
        flat_calc(malphite, "MagicDamageCalc1", 100.0)
        malphite["spec"]["kits"]["base"]["rows"]["ShieldDuration"] = 5.0
        res = match([ally(hp=10000.0, lane=1),
                     ally(hp=10000.0, ad=100.0, attack_speed=1.0, lane=3)],
                    [malphite], duration=2.1)
        wave = combat_events(res, "damage", "enemy", name="shield break")
        self.assertEqual([(hit["time"], hit["target"]) for hit in wave], [(1.0, 0), (1.0, 1)])
        self.assertEqual([hit["amount"] for hit in wave], [147.0, 154.0])
        # Initial auto earned one stack. Both wave recipients earn one;
        # the second wave is deferred because that attacker is borrowed.
        # The next auto must see all three, with no queue-time double grant.
        autos = combat_events(res, "damage", "enemy", name="auto")
        self.assertEqual([hit["time"] for hit in autos], [0.0, 2.0])
        self.assertEqual([hit["amount"] for hit in autos], [140.0, 161.0])


if __name__ == "__main__":
    unittest.main()
