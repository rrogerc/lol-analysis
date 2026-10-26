"""Champion ability driver, mana lock, targeting, and spell-refund tests."""

import collections
from collections import Counter
import copy
from copy import deepcopy
import json
from pathlib import Path
import unittest

from tft import sim as tft
from tft.update import ReviewRequired, reconcile
from tft.tests.helpers import (
    DUMMY,
    ENGINE,
    SNAP,
    ally,
    carry_result,
    enemy,
    events,
    fight,
    fixture,
    flat,
    flat_calc,
    immortal,
    match as champion_match,
    match_events,
    one_hitter,
    rows,
    spec_for,
    trace,
)


def flat_term(dtype, amount):
    return {"dtype": dtype,
            "terms": [{"type": "flat", "value": float(amount), "op": "add"}]}


def opening_cast(name, *, form=None, geometry="spread", targets=4,
                 duration=0.4, immortal=False):
    fx = [{"stats": [["ap", 1.0]]}] if form == "AP" else []
    spec = spec_for(name, fx=fx, geometry=geometry, duration=duration,
                    pressure=False)
    spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
    spec["unit"]["castTime"] = 0.25
    spec["immortal"] = immortal
    spec["dummies"]["slots"] = [
        dict(spec["dummies"]["slots"][0], hp=10000.0, armor=0.0, mr=0.0,
             nearby=i == 0, ad=0.0, ability=0.0)
        for i in range(targets)
    ]
    for kit in spec["kits"].values():
        kit["baseAd"] = 0.0
        kit["stats"].update(ad=0.0, initialMana=1000000.0, mana=1000000.0,
                            critChance=0.0, critMult=1.0)
        kit["stats"]["as"] = 0.01
    return spec


def independent_cast(name, **kwargs):
    spec = opening_cast(name, **kwargs)
    for kit in spec["kits"].values():
        if name == "Alune":
            kit["calcs"]["MagicDamageCalc1"] = flat_term("magic", 75.0)
        elif name == "Teemo":
            kit["calcs"]["MagicDamageCalc1"] = flat_term("magic", 90.0)
            kit["calcs"]["MagicDamageCalc2"] = flat_term("magic", 200.0)
        else:
            kit["calcs"]["PhysicalDamageCalc1"] = flat_term("physical", 250.0)
            kit["calcs"]["PhysicalDamageCalc2"] = flat_term("physical", 350.0)
            kit["calcs"]["MagicDamageCalc1"] = flat_term("magic", 240.0)
            kit["calcs"]["MagicDamageCalc2"] = flat_term("magic", 72.0)
    return spec


class TestIndependentTargeting(unittest.TestCase):
    def test_alune_distributes_all_nine_shards_among_three_spread_enemies(self):
        for geometry in ("spread", "clump"):
            for immortal in (False, True):
                with self.subTest(geometry=geometry, immortal=immortal):
                    _, result = ENGINE.simulate(independent_cast(
                        "Alune", geometry=geometry, immortal=immortal), True)
                    hits = events(result, "damage", "moonshards")
                    self.assertEqual([event[3] for event in hits], [0, 1, 2] * 3)
                    self.assertEqual([event[2] for event in hits], [75.0] * 9)
                    self.assertEqual(result["total"], 675.0)
                    self.assertEqual(result["casts"], 1)

    def test_teemo_clusters_reach_three_spread_enemies_and_giant_hits_primary(self):
        for geometry in ("spread", "clump"):
            with self.subTest(geometry=geometry):
                _, result = ENGINE.simulate(independent_cast("Teemo", geometry=geometry), True)
                clusters = events(result, "damage", "mushrooms")
                giant = events(result, "damage", "giant mushroom")
                self.assertEqual([event[3] for event in clusters], [0, 1, 2] * 2)
                self.assertEqual([event[2] for event in clusters], [90.0] * 6)
                self.assertEqual([(event[3], event[2]) for event in giant], [(0, 200.0)])
                self.assertEqual(result["total"], 2 * 3 * 90.0 + 200.0)

    def test_both_kogmaw_forms_reach_a_second_independent_enemy(self):
        for form, damage in (("AD", 250.0), ("AP", 240.0)):
            for geometry in ("spread", "clump"):
                with self.subTest(form=form, geometry=geometry):
                    sheet, result = ENGINE.simulate(independent_cast(
                        "Kog'Maw", form=form, geometry=geometry), True)
                    hits = events(result, "damage", "ability")
                    self.assertEqual(sheet["form"], form)
                    self.assertEqual([(event[3], event[2]) for event in hits],
                                     [(0, damage), (1, damage)])
                    self.assertEqual(result["total"], 2 * damage)

    def test_ap_kogmaw_dot_pays_its_full_total_to_both_recipients(self):
        _, result = ENGINE.simulate(independent_cast(
            "Kog'Maw", form="AP", duration=3.25), True)
        ticks = events(result, "damage", "acid")
        self.assertEqual(Counter(event[3] for event in ticks), {0: 12, 1: 12})
        self.assertTrue(all(event[2] == 6.0 for event in ticks))
        self.assertEqual(result["total"], 2 * (240.0 + 72.0))
        self.assertEqual(result["casts"], 1)

    def test_independent_counts_are_limited_by_living_population(self):
        for count in (1, 2, 5):
            for name in ("Alune", "Teemo", "Kog'Maw"):
                with self.subTest(champion=name, population=count):
                    _, result = ENGINE.simulate(independent_cast(name, targets=count), True)
                    hits = events(result, "damage")
                    if name == "Alune":
                        expected = 9 * 75.0
                        recipients = min(count, 3)
                    elif name == "Teemo":
                        recipients = min(count, 3)
                        expected = 2 * recipients * 90.0 + 200.0
                    else:
                        recipients = min(count, 2)
                        expected = recipients * 250.0
                    self.assertEqual(result["total"], expected)
                    self.assertEqual({event[3] for event in hits}, set(range(recipients)))

    def test_independent_selection_skips_a_target_killed_before_cast_lands(self):
        for name, source, recipients in (("Alune", "moonshards", [1, 2, 3] * 3),
                                         ("Teemo", "mushrooms", [1, 2, 3] * 2),
                                         ("Kog'Maw", "ability", [1, 2])):
            with self.subTest(champion=name):
                spec = independent_cast(name)
                spec["dummies"]["slots"][0]["hp"] = 50.0
                for kit in spec["kits"].values():
                    kit["baseAd"] = 100.0
                _, result = ENGINE.simulate(spec, True)
                self.assertEqual(events(result, "damage", "auto")[0][2], 50.0)
                self.assertEqual([event[3] for event in events(result, "damage", source)], recipients)

    def test_shared_kogmaw_keeps_current_target_even_when_it_has_higher_index(self):
        for form in ("AD", "AP"):
            with self.subTest(form=form):
                caster = {"spec": independent_cast("Kog'Maw", form=form),
                          "frontline": False, "lane": 6, "priority": 0}
                result = fight([caster], [enemy(lane=0), enemy(lane=2), enemy(lane=6)],
                               duration=0.4, geometry="spread")
                hits = trace(result, "damage", source=0, name="ability")
                self.assertEqual(len(hits), 2)
                self.assertEqual(hits[0]["target"], 2)
                self.assertEqual(len({hit["target"] for hit in hits}), 2)


class TestKayleAttackIdentity(unittest.TestCase):
    def kayle(self, *, auto, ascension, star=3, geometry="clump"):
        spec = opening_cast("Kayle", geometry=geometry, duration=0.1)
        spec["star"] = star
        kit = spec["kits"]["base"]
        kit["baseAd"] = auto
        kit["calcs"]["MagicDamageCalc1"] = flat_term("magic", ascension)
        kit["calcs"]["MagicDamageCalc2"] = flat_term("magic", 40.0)
        for index, target in enumerate(spec["dummies"]["slots"]):
            target.update(hp=50.0 if index == 0 else 10000.0, nearby=index < 3)
        return spec

    def test_waves_keep_both_secondaries_when_either_primary_damage_component_kills(self):
        for auto, ascension in ((100.0, 0.0), (0.0, 100.0)):
            with self.subTest(auto=auto, ascension=ascension):
                _, result = ENGINE.simulate(self.kayle(auto=auto, ascension=ascension), True)
                waves = events(result, "damage", "waves")
                self.assertEqual([(event[3], event[2]) for event in waves],
                                 [(1, 40.0), (2, 40.0)])
                self.assertEqual(result["total"], 50.0 + 2 * 40.0)
                self.assertEqual(result["attacks"], 1)

    def test_primary_death_does_not_add_waves_to_spread_or_lower_star_kayle(self):
        for star, geometry in ((3, "spread"), (2, "clump"), (1, "clump")):
            with self.subTest(star=star, geometry=geometry):
                _, result = ENGINE.simulate(self.kayle(
                    auto=100.0, ascension=100.0, star=star, geometry=geometry), True)
                self.assertFalse(events(result, "damage", "waves"))
                self.assertEqual(result["total"], 50.0)


class TestPoisonTargets(unittest.TestCase):
    def test_cassiopeia_second_poison_is_independent_of_clump(self):
        for geometry in ("spread", "clump"):
            with self.subTest(geometry=geometry):
                spec = fixture("Cassiopeia", geometry=geometry, duration=1)
                flat(spec, "MagicDamageCalc1", 150)
                rows(spec, Duration=15)
                res = carry_result(spec)
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
                res = carry_result(spec)
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
        res = carry_result(spec)
        # Two stacked poisons contribute 2.5 + .5 seconds on the primary;
        # the first secondary has 2.5 seconds, the next .5 seconds.
        self.assertEqual(res["left"], [9700, 9750, 9950])


class TestImpactIdentity(unittest.TestCase):
    def test_draven_return_keeps_the_outbound_line_after_a_lethal_hit(self):
        spec = fixture("Draven", hp=(1, 10000), geometry="spread")
        flat(spec, "PhysicalDamageCalc3", 100)
        flat(spec, "GenericCalc2", 25)
        res = carry_result(spec)
        self.assertEqual(res["left"], [0, 10000])
        self.assertEqual([(hit[3], hit[2]) for hit in events(res, "damage", "giant axes")],
                         [(0, 1)])

    def test_draven_living_recipient_takes_both_passes(self):
        spec = fixture("Draven", hp=(10000, 10000), geometry="spread")
        flat(spec, "PhysicalDamageCalc3", 100)
        flat(spec, "GenericCalc2", 25)
        res = carry_result(spec)
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
                    # Both forms are a stated 1 hex (BubbleHexRadius), so the
                    # explosion covers the target and the frontliner one lane
                    # over when packed, and the target alone when spread.
                    reached = [0, 10000, 10000] if geometry == "spread" else [0, 9950, 10000]
                    self.assertEqual(res["left"], reached)

    def test_leblanc_and_yunara_select_secondaries_before_primary_dies(self):
        for name, main, splash, label, expected in (
            ("LeBlanc", "MagicDamageCalc1", "MagicDamageCalc2", "splash", [1, 2, 3]),
            ("Yunara", "PhysicalDamageCalc1", "PhysicalDamageCalc2", "split", [1, 2]),
        ):
            for primary_hp in (1, 10000):
                with self.subTest(name=name, primary_hp=primary_hp):
                    spec = fixture(name, hp=(primary_hp, 10000, 10000, 10000))
                    # Isolate recipient identity from formation: every
                    # secondary is nearby in this four-target fixture.
                    for target in spec["dummies"]["slots"]:
                        target["lane"] = None
                    flat(spec, main, 100)
                    flat(spec, splash, 25)
                    res = carry_result(spec)
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
                res = carry_result(spec)
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
                res = carry_result(spec)
                burst = events(res, "damage", "burst")
                # The burst is a stated 1 hex (AoEHexRange), so packed it
                # reaches the dead centre's neighbour and nothing further.
                expected = [] if geometry == "spread" else [1]
                self.assertEqual([hit[3] for hit in burst], expected)
                self.assertTrue(all(hit[0] == 1.25 and hit[2] == 25 for hit in burst))

    def test_karma_living_center_receives_tether_and_burst(self):
        spec = fixture("Karma", geometry="spread", duration=1.25)
        flat(spec, "MagicDamageCalc1", 100)
        flat(spec, "MagicDamageCalc2", 25)
        rows(spec, TetherDuration=1)
        res = carry_result(spec)
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
                    res = carry_result(spec)
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
        res = carry_result(spec)
        laser = events(res, "damage", "laser")
        self.assertEqual(res["castTimes"], [0])
        # The channel drains the bar she cast with, the opening attack's 7
        # overflow included (2026-09-20: it used to be zeroed at the cast).
        bar = events(res, "cast")[0][2]
        self.assertEqual(bar, 1007)
        length = bar / (0.35 * 1000)
        self.assertAlmostEqual(sum(hit[2] for hit in laser), 240 * length)
        self.assertAlmostEqual(laser[-1][0], length)
        self.assertAlmostEqual(laser[-1][2], 240 * (length - 2.75))

    def test_pebbles_pays_only_elapsed_time_on_the_first_partial_tick(self):
        spec = fixture("Pebbles", duration=0.76)
        flat(spec, "MagicDamageCalc1", 240)
        rows(spec, PercentManaPerSecond=0.35, MRReduction=0)
        kit = spec["kits"]["base"]
        kit["stats"].update(mana=14, initialMana=0)
        kit["stats"]["as"] = 1 / 0.6
        res = carry_result(spec)
        self.assertEqual(res["castTimes"], [0.6])
        laser = events(res, "damage", "laser")
        self.assertEqual(len(laser), 1)
        self.assertEqual(laser[0][0], 0.75)
        self.assertAlmostEqual(laser[0][2], 240 * 0.15)

    def test_pebbles_tick_aligned_endpoint_is_paid_exactly_once(self):
        spec = fixture("Pebbles", duration=3.1)
        flat(spec, "MagicDamageCalc1", 240)
        rows(spec, PercentManaPerSecond=0.4, MRReduction=0)
        # No attack mana: the bar is exactly 1000 and, now that the overflow
        # drains too, still runs dry on the tick at 2.5 s.
        spec["unit"]["kind"] = "Specialist"
        res = carry_result(spec)
        laser = events(res, "damage", "laser")
        self.assertEqual(sum(hit[2] for hit in laser), 600)
        self.assertEqual([hit[0] for hit in laser], [i / 4 for i in range(1, 11)])


class TestSivirBounces(unittest.TestCase):
    def sivir(self, hp, *, bounces=2):
        spec = fixture("Sivir", hp=hp)
        flat(spec, "PhysicalDamageCalc1", 2)
        flat(spec, "PhysicalDamageCalc2", 10)
        rows(spec, NumBounces=bounces, BonusKillBounces=3)
        return carry_result(spec)

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


def fighter_fixture(name, *, geometry="spread", duration=0.4, opening_cast=False):
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
    spec = fighter_fixture("Gnar", **kwargs)
    kit = spec["kits"]["base"]
    kit["rows"].update(RagePerAttack=1, RagePerSecond=0, TransformRageMax=1,
                       StunDuration=0)
    for calc, dtype, value in (("HealthCalc1", "magic", 0),
                               ("GenericCalc1", "magic", 0),
                               ("PhysicalDamageCalc3", "physical", 0),
                               ("PhysicalDamageCalc1", "physical", 500),
                               ("PhysicalDamageCalc2", "physical", 70)):
        kit["calcs"][calc] = flat_term(dtype, value)
    return spec


class TestFighterImpactIdentity(unittest.TestCase):
    def test_elder_splash_keeps_the_original_primary_when_auto_is_lethal(self):
        for geometry, recipients in (("spread", []), ("clump", [1, 2])):
            with self.subTest(geometry=geometry):
                spec = fighter_fixture("Elder Dragon", geometry=geometry, duration=0.05)
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
        spec["kits"]["base"]["calcs"]["PhysicalDamageCalc3"] = flat_term("physical", 100)
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
                spec = fighter_fixture("Diana", geometry=geometry, opening_cast=True)
                kit = spec["kits"]["base"]
                kit["calcs"]["MagicDamageCalc1"] = flat_term("magic", 100)
                kit["rows"]["NumOrbs"] = 6
                spec["dummies"]["slots"][0]["hp"] = 50
                for target in spec["dummies"]["slots"][1:]:
                    target["nearby"] = False
                _, result = ENGINE.simulate(spec, True)
                self.assertEqual([(e[3], e[2]) for e in events(result, "damage", "orbs")], [(0, 50)])

    def test_diana_orbs_left_over_in_spread_do_not_fly_on_to_the_next_enemy(self):
        # Every dummy is a nearby one here. Spread out, only the target was
        # "within 2 hexes" when the cast landed: once it dies the other five
        # orbs are lost (they used to resolve onto the next dummy in line).
        # In the clump all three were in reach and the orbs move on as before.
        for geometry, expected in (
                ("spread", [(0, 50)]),
                ("clump", [(0, 50), (2, 100), (1, 100), (2, 100), (1, 100), (2, 100)])):
            with self.subTest(geometry=geometry):
                spec = fighter_fixture("Diana", geometry=geometry, duration=2.0, opening_cast=True)
                kit = spec["kits"]["base"]
                kit["calcs"]["MagicDamageCalc1"] = flat_term("magic", 100)
                kit["rows"]["NumOrbs"] = 6
                spec["dummies"]["slots"][0]["hp"] = 50
                _, result = ENGINE.simulate(spec, True)
                self.assertEqual(result["casts"], 1)
                self.assertEqual([(e[3], e[2]) for e in events(result, "damage", "orbs")], expected)


class TestElderFlightWindow(unittest.TestCase):
    def test_protection_is_during_first_cast_and_ends_at_landing(self):
        spec = fighter_fixture("Elder Dragon", duration=1.1, opening_cast=True)
        spec["unit"]["castTime"] = 0.5
        spec["pressure"] = True
        dummy = one_hitter(100, period=0.25, attackers=1)
        spec["dummies"] = deepcopy(dummy)
        kit = spec["kits"]["base"]
        kit["rows"].update(StunDuration=0, Omnivamp=0, IgniteMaxHealthDamage=0)
        kit["calcs"]["PhysicalDamageCalc2"] = flat_term("physical", 0)
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual(result["castTimes"], [0])
        self.assertEqual([e[0] for e in events(result, "land")], [0.5])
        self.assertEqual([(e[0], e[2]) for e in events(result, "take")],
                         [(0.5, 100), (0.75, 100), (1.0, 100)])
        self.assertEqual(result["taken"], 300)


def harvest_spec(*, geometry="spread", near_count=3, target_count=5):
    dummy = one_hitter(0)
    dummy["slots"] = [dict(dummy["slots"][0], hp=10000, armor=0, mr=100,
                           nearby=i < near_count, kind="tank" if i < near_count else "non-tank")
                       for i in range(target_count)]
    dummy.update(board=None, count=target_count, boardSize=target_count)
    spec = copy.deepcopy(spec_for("Fiddlesticks", star=2, geometry=geometry,
                                  dummy=dummy, pressure=False, duration=2.1))
    spec["immortal"] = False
    # One opening channel, no second attack and no recast. Preserve the
    # actual 2-star spell calculation (105 at 100 AP), MR strip and duration.
    kit = spec["kits"]["base"]
    kit["baseAd"] = 0
    kit["stats"].update(ad=0, initialMana=10**6, mana=10**6)
    kit["stats"]["as"] = 0.01
    return spec


class TestFiddlesticksNearest(unittest.TestCase):
    def test_three_nearest_recipients_do_not_require_clumping(self):
        for geometry in ("spread", "clump"):
            with self.subTest(geometry=geometry):
                _, result = ENGINE.simulate(harvest_spec(geometry=geometry), True)
                damage = events(result, "damage", "drain")
                self.assertEqual({hit[3] for hit in damage}, {0, 1, 2})
                self.assertEqual(result["casts"], 1)
                # 105 magic damage against (100 - 10) MR on each victim.
                for target in (0, 1, 2):
                    self.assertAlmostEqual(sum(hit[2] for hit in damage if hit[3] == target),
                                           105 / 1.9)

    def test_nearest_selection_can_reach_beyond_the_nearby_group(self):
        # Only one close target remains. The other two chosen enemies may
        # be protected backliners: nearest N has no nearby-only restriction.
        for geometry in ("spread", "clump"):
            with self.subTest(geometry=geometry):
                _, result = ENGINE.simulate(harvest_spec(geometry=geometry, near_count=1), True)
                self.assertEqual({hit[3] for hit in events(result, "damage", "drain")},
                                 {0, 1, 2})

    def test_target_count_is_a_limit_and_does_not_duplicate_recipients(self):
        _, result = ENGINE.simulate(harvest_spec(target_count=2), True)
        damage = events(result, "damage", "drain")
        self.assertEqual({hit[3] for hit in damage}, {0, 1})
        self.assertAlmostEqual(sum(hit[2] for hit in damage), 2 * 105 / 1.9)

    def test_recipients_are_selected_from_survivors_after_opening_attack(self):
        spec = harvest_spec()
        spec["kits"]["base"]["baseAd"] = 100
        spec["kits"]["base"]["stats"]["ad"] = 100
        spec["dummies"]["slots"][0]["hp"] = 1
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual({hit[3] for hit in events(result, "damage", "drain")}, {1, 2, 3})


def lullaby_spec(*, ad=0, attack_speed=0.01, armor=0, mr=0,
                 duration=2.1, targets=1, fx=()):
    dummy = one_hitter(0)
    dummy["slots"] = [dict(dummy["slots"][0], hp=10000, armor=armor, mr=mr,
                           nearby=True) for _ in range(targets)]
    dummy.update(board=None, count=targets, boardSize=targets)
    spec = spec_for("Lillia", star=2, dummy=dummy, pressure=False,
                    duration=duration, fx=fx)
    spec["immortal"] = False
    kit = spec["kits"]["base"]
    kit["baseAd"] = ad
    kit["stats"].update(ad=ad, initialMana=10**6, mana=10**6,
                        critChance=0, critMult=1)
    kit["stats"]["as"] = attack_speed
    return spec


def lullaby_member(**kwargs):
    return {"spec": lullaby_spec(**kwargs), "frontline": True, "lane": 3, "priority": 0}


class TestLilliaSleep(unittest.TestCase):
    def test_small_damage_and_natural_expiry_grant_no_wake_bonus(self):
        _, result = ENGINE.simulate(lullaby_spec(ad=100, attack_speed=2), True)
        self.assertEqual(len(events(result, "damage", "butterflies")), 1)
        self.assertEqual(events(result, "damage", "wake-up"), [])

    def test_threshold_counts_post_sleep_damage_once(self):
        _, result = ENGINE.simulate(lullaby_spec(ad=600, attack_speed=2), True)
        wakes = events(result, "damage", "wake-up")
        self.assertEqual([(hit[0], hit[2]) for hit in wakes], [(1.0, 1000)])
        # The opening attack and butterflies precede sleep. Only attacks at
        # .5 and 1.0 supply its 1000-damage threshold, with 200 excess lost.
        self.assertEqual(len(events(result, "sleep", "lullaby")), 1)

    def test_post_mitigation_damage_cannot_use_raw_attack_amount(self):
        _, result = ENGINE.simulate(lullaby_spec(ad=600, attack_speed=2, armor=100), True)
        # Three post-sleep attacks each deal300 before expiry: 900 < 1000.
        self.assertEqual(events(result, "damage", "wake-up"), [])

    def test_crossing_threshold_after_sleep_expires_does_not_wake(self):
        _, result = ENGINE.simulate(lullaby_spec(ad=600, attack_speed=1, duration=3.1), True)
        self.assertEqual(events(result, "damage", "wake-up"), [])

    def test_item_burn_can_awaken_secondary_victims(self):
        # Synthetic burn strength isolates the damage condition; no guessed
        # sleep delay or special treatment for attacks is introduced.
        _, result = ENGINE.simulate(lullaby_spec(targets=3, fx=[{"burnOnHit": [0.2, 3]}]), True)
        wakes = events(result, "damage", "wake-up")
        self.assertEqual({hit[3] for hit in wakes}, {0, 1, 2})
        self.assertEqual(len(wakes), 3)
        self.assertTrue(all(hit[2] == 1000 for hit in wakes))

    def test_wake_bonus_uses_magic_mitigation(self):
        _, result = ENGINE.simulate(lullaby_spec(ad=600, attack_speed=2, mr=100), True)
        self.assertEqual([hit[2] for hit in events(result, "damage", "wake-up")], [500])

    def test_awakening_releases_a_spell_previously_delayed_by_sleep(self):
        spec = lullaby_spec(ad=600, attack_speed=2, duration=1.2)
        spec["pressure"] = True
        spec["dummies"]["slots"][0].update(ability=10, physicalShare=0,
                                             castStart=0.5, castInterval=10)
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([hit[0] for hit in events(result, "take", "magic")], [1.0])

    def test_refreshed_sleep_does_not_leave_a_pending_spell_at_the_old_expiry(self):
        spec = lullaby_spec(ad=1000, attack_speed=2 / 3, duration=2.1)
        spec["pressure"] = True
        # Exactly two casts land at .25 and 1.5; the second refreshes the
        # expiry from 2.0 to 3.25. The attack at 1.5 wakes the refreshed sleep.
        spec["role"] = {"manaRegen": 400, "asPct": 0}
        spec["kits"]["base"]["stats"].update(mana=100, initialMana=100)
        spec["dummies"]["slots"][0].update(ability=10, physicalShare=0,
                                             castStart=0.5, castInterval=10)
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual(result["castTimes"], [0.0, 1.25])
        self.assertEqual([hit[0] for hit in events(result, "sleep", "lullaby")], [0.25, 1.5])
        self.assertEqual([hit[0] for hit in events(result, "wake", "lullaby")], [1.5])
        # The spell has been ready since .5: it resumes on wake, despite
        # having been parked at the original 2.0 expiry before the refresh.
        self.assertEqual([hit[0] for hit in events(result, "take", "magic")], [1.5])

    def test_wake_does_not_advance_a_future_spell_coincidentally_at_sleep_expiry(self):
        spec = lullaby_spec(ad=600, attack_speed=2, duration=2.1)
        spec["pressure"] = True
        # First Sleep naturally ends at 2.0, but damage wakes it at 1.0.
        # The spell due at 2.0 has never been blocked and must keep its timer.
        spec["dummies"]["slots"][0].update(ability=10, physicalShare=0,
                                             castStart=2.0, castInterval=10)
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([hit[0] for hit in events(result, "wake", "lullaby")], [1.0])
        self.assertEqual([hit[0] for hit in events(result, "take", "magic")], [2.0])


class TestSharedLilliaSleep(unittest.TestCase):
    def test_cc_immunity_rejects_sleep_and_its_conditional_bonus(self):
        result = champion_match([lullaby_member(), ally("Ashe", hp=10000, ad=600, attack_speed=2)],
                                 [ally("Leona", hp=10000, fx=[{"ccImmuneDuration": 5}])], duration=2.1)
        self.assertEqual(match_events(result, "damage", name="wake-up"), [])

    def test_death_body_does_not_inherit_the_original_victims_sleep(self):
        lillia = lullaby_member()
        lillia["spec"]["kits"]["base"]["rows"]["SleepDuration"] = 4
        krug = ally("Krug", driver="Krug", hp=500)
        flat_calc(krug, "HealthCalc1", 3000)
        krug["spec"]["unit"]["extras"]["TFT18_KrugMini"].update(armor=0, mr=0)
        result = champion_match([lillia, ally("Ashe", hp=10000, ad=300, attack_speed=2)],
                                 [krug], duration=2.6)
        self.assertEqual(match_events(result, "damage", name="wake-up"), [])
        self.assertFalse(result["enemies"][0]["alive"])

    def test_shared_scheduled_spell_resumes_when_awakened(self):
        result = fight([lullaby_member(), ally("Ashe", hp=10000, ad=600, attack_speed=2)],
                       [enemy(ability=10, physicalShare=0, castStart=0.5, castInterval=10)], duration=1.2)
        self.assertEqual([hit["time"] for hit in trace(result, "enemySpell")], [1.0])

    def test_refreshed_shared_sleep_resumes_an_already_pending_spell_on_wake(self):
        first = lullaby_member()
        second = lullaby_member()
        first["spec"]["unit"]["castTime"] = 0.25
        second["spec"]["unit"]["castTime"] = 0.75
        result = fight([first, second, ally("Ashe", hp=10000, ad=500, attack_speed=2)],
                       [enemy(ability=10, physicalShare=0, castStart=0.5, castInterval=10)], duration=2.1)
        self.assertEqual([hit["time"] for hit in trace(result, "sleep")], [0.25, 0.75])
        self.assertEqual([(hit["time"], hit["source"]) for hit in trace(result, "wake")], [(1.5, 1)])
        # The enemy became ready at .5 under the first Sleep (expiry 2.0).
        # The refreshed Sleep expires at 2.5 but is broken at 1.5; retaining
        # either expiry would suppress a spell whose pending cast is ready.
        self.assertEqual([hit["time"] for hit in trace(result, "enemySpell")], [1.5])

    def test_shared_wake_preserves_a_future_spell_timer_matching_sleep_expiry(self):
        result = fight([lullaby_member(), ally("Ashe", hp=10000, ad=600, attack_speed=2)],
                       [enemy(ability=10, physicalShare=0, castStart=2.0, castInterval=10)], duration=2.1)
        self.assertEqual([hit["time"] for hit in trace(result, "wake")], [1.0])
        self.assertEqual([hit["time"] for hit in trace(result, "enemySpell")], [2.0])

    def test_shorter_shared_sleep_refresh_updates_pending_spell_without_a_damage_wake(self):
        for extra_stun, expected in ((False, 1.75), (True, 3.25)):
            with self.subTest(independent_stun=extra_stun):
                first = lullaby_member()
                first["spec"]["unit"]["castTime"] = 0.25
                first["spec"]["kits"]["base"]["rows"]["SleepDuration"] = 4.0
                second = lullaby_member()
                second["spec"]["unit"]["castTime"] = 0.75
                second["spec"]["kits"]["base"]["rows"]["SleepDuration"] = 1.0
                members = [first, second]
                if extra_stun:
                    stunner = ally("Leona", driver="Leona", hp=10000, cast=True)
                    flat_calc(stunner, "GenericCalc1", 0)
                    flat_calc(stunner, "MagicDamageCalc1", 0)
                    stunner["spec"]["kits"]["base"]["rows"]["StunDuration"] = 3.0
                    members.append(stunner)
                result = fight(members, [enemy(ability=10, physicalShare=0,
                                              castStart=0.5, castInterval=10)], duration=3.5)
                # Synthetic durations exercise the declared refresh rule:
                # the old 4.25 expiry becomes 1.75. No threshold is crossed,
                # and an independent stun may still delay readiness to 3.25.
                self.assertEqual([hit["time"] for hit in trace(result, "sleep")], [0.25, 0.75])
                self.assertEqual(trace(result, "wake"), [])
                self.assertEqual([hit["time"] for hit in trace(result, "enemySpell")], [expected])

    def test_allied_attacks_awaken_once_and_credit_lillia_in_both_shared_modes(self):
        for mode in ("dummy", "champion"):
            with self.subTest(mode=mode):
                members = [lullaby_member(), ally("Ashe", hp=10000, ad=600, attack_speed=2)]
                if mode == "dummy":
                    result = fight(members, [enemy()], duration=2.1)
                    wakes = trace(result, "damage", name="wake-up")
                else:
                    result = champion_match(members, [ally("Leona", hp=10000)], duration=2.1)
                    wakes = match_events(result, "damage", name="wake-up")
                self.assertEqual([(hit["time"], hit["source"], hit["amount"]) for hit in wakes],
                                 [(1.0, 0, 1000)])
                self.assertAlmostEqual(result["allies"][0]["damage"], 135 + 1000)

    def test_shared_item_burn_damage_counts_toward_threshold(self):
        for mode in ("dummy", "champion"):
            with self.subTest(mode=mode):
                members = [lullaby_member(), ally("Ashe", hp=10000, ad=1,
                                                 fx=[{"burnOnHit": [0.2, 3]}])]
                if mode == "dummy":
                    result = fight(members, [enemy()], duration=2.1)
                    wakes = trace(result, "damage", name="wake-up")
                else:
                    result = champion_match(members, [ally("Leona", hp=10000)], duration=2.1)
                    wakes = match_events(result, "damage", name="wake-up")
                self.assertEqual(len(wakes), 1)
                self.assertEqual((wakes[0]["source"], wakes[0]["amount"]), (0, 1000))

    def test_awakening_keeps_an_independent_longer_stun(self):
        for mode in ("dummy", "champion"):
            with self.subTest(mode=mode):
                stunner = ally("Leona", driver="Leona", hp=10000, cast=True)
                flat_calc(stunner, "GenericCalc1", 0)
                flat_calc(stunner, "MagicDamageCalc1", 0)
                stunner["spec"]["kits"]["base"]["rows"]["StunDuration"] = 3
                members = [lullaby_member(), stunner,
                           ally("Ashe", hp=10000, ad=600, attack_speed=2)]
                if mode == "dummy":
                    result = fight(members, [enemy(ad=10, **{"as": 2}, attackStart=0.5)], duration=3.6)
                    wakes = trace(result, "damage", name="wake-up")
                    attacks = trace(result, "enemyAttack")
                else:
                    result = champion_match(members, [ally("Ornn", hp=10000, ad=10,
                                                          attack_speed=2)], duration=3.6)
                    wakes = match_events(result, "damage", name="wake-up")
                    attacks = [hit for hit in match_events(result, "attack", side="enemy")
                               if hit["time"] > 0]
                self.assertEqual(len(wakes), 1)
                self.assertTrue(attacks)
                self.assertTrue(all(hit["time"] >= 3.25 for hit in attacks))

    def test_sleep_expiry_has_no_bonus_in_either_shared_mode(self):
        for mode in ("dummy", "champion"):
            with self.subTest(mode=mode):
                members = [lullaby_member()]
                result = (fight(members, [enemy()], duration=2.1) if mode == "dummy" else
                          champion_match(members, [ally("Leona", hp=10000)], duration=2.1))
                wakes = (trace(result, "damage", name="wake-up") if mode == "dummy" else
                         match_events(result, "damage", name="wake-up"))
                self.assertEqual(wakes, [])


class TestAzirProtectorsVow(unittest.TestCase):
    def spec(self, *, duration=None, copies=1):
        contexts, _ = tft.unit_trait_contexts(
            SNAP, SNAP.unit("Azir"), tft.load_trait_effects(SNAP.set_no))
        return spec_for(
            "Azir", star=2, geometry="spread", traits=contexts["low"],
            items=["Protector's Vow"] * copies
                  + ["Rabadon's Deathcap"] * (3 - copies),
            duration=duration, dummy=tft.dummies_for(SNAP))

    def test_vow_grants_twenty_starting_mana_once(self):
        spec = self.spec(duration=0.01)
        sheet, res = ENGINE.simulate(spec, True)
        self.assertEqual(sheet["manaStart"], 20.0)
        self.assertEqual(sheet["manaMax"], 35.0)
        self.assertEqual(res["casts"], 0)
        self.assertEqual(res["attacks"], 1)
        # One opening attack grants ten: neither the 20 starting mana nor
        # the low-health 15-mana proc has been counted a second time.
        self.assertEqual(res["probe"]["mana"], 30.0)

    def test_two_vows_cap_opening_mana_at_azirs_bar(self):
        _, res = ENGINE.simulate(self.spec(duration=0.01, copies=2), True)
        casts = events(res, "cast")
        self.assertEqual(len(casts), 1)
        self.assertEqual(casts[0][0], 0.0)
        # The engine's opening attack runs before its cast check. Starting
        # mana is capped at 35, then that attack adds ten (45, not 50).
        self.assertEqual(res["attacks"], 1)
        self.assertEqual(casts[0][2], 45.0)
        self.assertEqual(res["probe"]["mana"], 10.0)

    def test_defenses_and_health_procs_do_not_improve_unattacked_carry(self):
        spec = self.spec()
        self.assertFalse(spec["pressure"])
        _, expected = ENGINE.simulate(spec, True)
        without_defense = copy.deepcopy(spec)
        vow = without_defense["items"][0]
        self.assertEqual(vow.pop("manaAtHp"), [0.4, 15.0])
        self.assertEqual(vow.pop("shieldAtHp")[:2], [0.4, 0.2])
        vow["stats"] = [(key, value) for key, value in vow["stats"]
                        if key not in ("armor", "mr")]
        _, actual = ENGINE.simulate(without_defense, True)
        self.assertEqual(actual, expected)
        self.assertEqual(expected["taken"], 0.0)
        self.assertEqual(expected["shielded"], 0.0)
        self.assertEqual(expected["probe"]["shieldsActive"], 0)

    def test_starting_mana_advances_first_cast_by_two_attacks(self):
        spec = self.spec(duration=4.01)
        _, with_vow = ENGINE.simulate(spec, True)
        without_start = copy.deepcopy(spec)
        self.assertEqual(without_start["items"][0].pop("startingMana"), 20.0)
        _, without_vow_start = ENGINE.simulate(without_start, True)
        opening_with = events(with_vow, "cast")[0][0]
        opening_without = events(without_vow_start, "cast")[0][0]
        # Both retain Vow's regeneration and every other stat. With 0.75
        # base AS, the second and fourth ordinary attacks start the casts.
        self.assertAlmostEqual(opening_with, 1.0 / 0.75)
        self.assertAlmostEqual(opening_without, 3.0 / 0.75)
        for result, first_cast, expected_count in (
                (with_vow, opening_with, 2),
                (without_vow_start, opening_without, 4)):
            self.assertEqual(len([e for e in events(result, "attack")
                                  if e[0] <= first_cast]), expected_count)


class TestAzirManaLock(unittest.TestCase):
    def spec(self, *, duration, attack_speed=.8, regen=0.0, items=(), traits=()):
        spec = spec_for("Azir", items=items, traits=traits, duration=duration,
                        pressure=False, dummy=immortal(DUMMY))
        spec["kits"]["base"]["stats"].update(initialMana=35.0)
        spec["kits"]["base"]["stats"]["as"] = attack_speed
        if regen:
            spec["role"]["manaRegen"] = regen
        return spec

    def test_all_six_commands_block_attack_mana(self):
        # Opening attack fills a 35-mana bar and leaves 10 overflow.
        # Base AS .8 gives the first command at 1.25s, then five more
        # every .5s. The sixth command at 3.75s must grant no mana either.
        for duration, count in ((3.51, 5), (3.76, 6)):
            with self.subTest(duration=duration):
                _, result = ENGINE.simulate(self.spec(duration=duration), True)
                self.assertEqual(result["casts"], 1)
                self.assertEqual(len(events(result, "damage", "soldiers")), count)
                self.assertEqual(result["probe"]["mana"], 10.0)
        # The first ordinary attack after the window grants mana again.
        _, result = ENGINE.simulate(self.spec(duration=5.01), True)
        self.assertEqual(len(events(result, "damage", "soldiers")), 6)
        self.assertEqual(result["probe"]["mana"], 20.0)

    def test_regeneration_is_blocked_and_resumes_after_sixth_command(self):
        _, locked = ENGINE.simulate(self.spec(duration=3.51, regen=4.0), True)
        self.assertEqual(locked["casts"], 1)
        self.assertEqual(locked["probe"]["mana"], 10.0)
        _, released = ENGINE.simulate(self.spec(duration=4.01, regen=4.0), True)
        self.assertEqual(len(events(released, "damage", "soldiers")), 6)
        self.assertEqual(released["probe"]["lockUntil"], 3.75)
        # Only .25s is outside the lock. There is no extra second added
        # after the sixth command and no regeneration for time inside it.
        self.assertEqual(released["probe"]["mana"], 11.0)

    def test_partial_tick_only_regenerates_after_unlock(self):
        _, result = ENGINE.simulate(
            self.spec(duration=3.51, attack_speed=.9, regen=4.0), True)
        commands = events(result, "damage", "soldiers")
        self.assertEqual(len(commands), 6)
        end = commands[-1][0]
        self.assertAlmostEqual(end, 10 / 3)
        self.assertAlmostEqual(result["probe"]["lockUntil"], end)
        self.assertAlmostEqual(result["probe"]["mana"], 10 + 4 * (3.5 - end))

    def test_mana_items_cannot_refresh_an_unfinished_command_window(self):
        contexts, _ = tft.unit_trait_contexts(
            SNAP, SNAP.unit("Azir"), tft.load_trait_effects(SNAP.set_no))
        builds = (
            ("Blue Buff",) * 3,
            ("Spear of Shojin",) * 3,
            ("Nashor's Tooth",) * 3,
            ("Adaptive Helm", "Blue Buff", "Nashor's Tooth"),
            ("Protector's Vow", "Rabadon's Deathcap", "Rabadon's Deathcap"),
        )
        for items in builds:
            for traits in ([], contexts["high"]):
                with self.subTest(items=items, traits=traits):
                    _, result = ENGINE.simulate(
                        self.spec(duration=20, items=items, traits=traits), True)
                    casts = result["castTimes"]
                    self.assertGreaterEqual(len(casts), 2)
                    commands = events(result, "damage", "soldiers")
                    for start, following in zip(casts, casts[1:]):
                        window = [event for event in commands if start < event[0] <= following]
                        self.assertEqual(len(window), 6)

    def test_item_attack_mana_is_blocked_with_the_base_attack_mana(self):
        spec = self.spec(duration=3.0,
                         items=("Spear of Shojin", "Spear of Shojin", "Nashor's Tooth"))
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual(result["casts"], 1)
        self.assertGreaterEqual(len(events(result, "damage", "soldiers")), 4)
        overflow = events(result, "cast")[0][2] - 35
        self.assertAlmostEqual(result["probe"]["mana"], overflow)

    def test_target_death_does_not_end_the_command_lock(self):
        spec = self.spec(duration=3.76, regen=4.0)
        spec["dummies"]["slots"][0]["hp"] = 50.0
        _, result = ENGINE.simulate(spec, True)
        commands = events(result, "damage", "soldiers")
        self.assertEqual(len(commands), 6)
        self.assertEqual(commands[0][3], 0)
        self.assertEqual([event[3] for event in commands[1:]], [1] * 5)
        self.assertEqual(result["casts"], 1)
        self.assertEqual(result["probe"]["mana"], 10.0)


TICK = tft.TICK_S


def timing_spec(name, *, star=2, duration=20.0, geometry="clump", regen=0.0, attack_speed=None,
                fx=(), items=(), traits=(), dummy=None):
    """One unpressured fight against dummies that cannot die, with the role's
    regeneration and attack speed replaced, so the mana that comes in between
    two casts is exactly what the attacks (and `regen`) paid."""
    spec = spec_for(name, star=star, items=items, fx=fx, geometry=geometry, traits=traits,
                    duration=duration, pressure=False, dummy=dummy or immortal(DUMMY))
    spec["role"] = {"manaRegen": float(regen), "asPct": 0.0}
    if attack_speed is not None:
        for kit in spec["kits"].values():
            kit["stats"]["as"] = attack_speed
    return spec


def sim_fight(spec):
    return ENGINE.simulate(spec, True)


def casts_of(res):
    """[(cast time, mana on the bar at the cast, when the driver's cast ran)].
    The last is the `land` row that follows the cast, or the cast itself for an
    ability the engine runs at the start of the cast."""
    rows = [(e[0], e[2]) for e in events(res, "cast")]
    lands = [e[0] for e in events(res, "land")]
    out = []
    for i, (t, mana) in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else float("inf")
        landed = [x for x in lands if t <= x < nxt]
        out.append((t, mana, landed[0] if landed else t))
    return out


def came_in(casts, i, mana_max):
    """Mana gained between cast i and cast i+1 (the overflow carries)."""
    return casts[i + 1][1] - min(max(casts[i][1] - mana_max, 0.0), mana_max)


def attack_times(res, after, through):
    return [e[0] for e in events(res, "attack") if after < e[0] <= through]


def hit_times(res, src):
    return [e[0] for e in events(res, "damage", src)]


def five_target_dummy(geometry):
    """Pin these area-rule fixtures to three frontliners and two backliners.

    Explicit positions keep their recipient oracles independent of changes
    to the leaderboard's default enemy formation.
    """
    positions = {
        "clump": ((2, 0), (3, 0), (4, 0), (2, 2), (4, 2)),
        "spread": ((0, 0), (3, 0), (6, 0), (1, 2), (5, 2)),
    }
    slots = copy.deepcopy(DUMMY["slots"][:3] + DUMMY["slots"][-2:])
    for index, (slot, (lane, row)) in enumerate(zip(slots, positions[geometry])):
        slot.update(lane=lane, row=row, nearby=index < 3)
    return dict(DUMMY, slots=slots, count=5, board=None,
                totalHp=sum(slot["hp"] for slot in slots))


class TestNidaleeJavelinLock(unittest.TestCase):
    """AP form: no mana from the cast until the third javelin is thrown."""

    def ap(self, **kw):
        return timing_spec("Nidalee", fx=[{"stats": [["ap", 1.0]]}], **kw)

    def test_javelins_grant_no_mana_and_ordinary_attacks_do(self):
        spec = self.ap(duration=30.0)
        sheet, res = sim_fight(spec)
        self.assertEqual(sheet["form"], "AP")
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        javelins = sorted(hit_times(res, "javelins") + hit_times(res, "farthest javelin"))
        per_attack = 10.0                                   # Marksman
        for i in range(len(casts) - 1):
            start, end = casts[i][0], casts[i + 1][0]
            thrown = [t for t in javelins if start < t <= end]
            # every window is thrown out before the bar can fill again
            self.assertEqual(len(thrown), 3)
            ordinary = [t for t in attack_times(res, start, end) if t not in thrown]
            self.assertTrue(all(t > thrown[-1] for t in ordinary))
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), per_attack * len(ordinary))

    def test_regeneration_is_held_with_the_attack_mana(self):
        regen = 4.0
        sheet, res = sim_fight(self.ap(duration=30.0, regen=regen))
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        javelins = sorted(hit_times(res, "javelins") + hit_times(res, "farthest javelin"))
        for i in range(len(casts) - 1):
            start, end = casts[i][0], casts[i + 1][0]
            release = [t for t in javelins if start < t <= end][2]
            ordinary = [t for t in attack_times(res, start, end) if t > release]
            gained = came_in(casts, i, sheet["manaMax"]) - 10.0 * len(ordinary)
            # regen only for the time after the last javelin (paid on the ticks)
            self.assertLessEqual(gained, regen * (end - release) + 1e-9)
            self.assertGreaterEqual(gained, regen * (end - release - TICK) - 1e-9)

    def test_mana_items_cannot_chain_casts_through_the_javelins(self):
        # the published #2 build: Nashor's Tooth refilled the bar on her own javelins
        spec = spec_for("Nidalee", items=("Giant Slayer", "Jeweled Gauntlet", "Nashor's Tooth"),
                        dummy=immortal(DUMMY), pressure=False, duration=30.0)
        sheet, res = sim_fight(spec)
        self.assertEqual(sheet["form"], "AP")
        casts = casts_of(res)
        javelins = sorted(hit_times(res, "javelins") + hit_times(res, "farthest javelin"))
        for (start, _, _), (end, _, _) in zip(casts, casts[1:]):
            self.assertEqual(len([t for t in javelins if start < t <= end]), 3)
            self.assertGreater(len(attack_times(res, start, end)), 3)   # ordinary attacks refill it

    def test_ad_form_keeps_the_ordinary_cast_lock(self):
        spec = timing_spec("Nidalee", fx=[{"stats": [["adPct", 0.1]]}], duration=10.0)
        sheet, res = sim_fight(spec)
        self.assertEqual(sheet["form"], "AD")
        self.assertLess(res["probe"]["lockUntil"], 1e8)
        self.assertGreaterEqual(res["casts"], 3)


class TestMamaBeakFlockLock(unittest.TestCase):
    def test_no_mana_while_the_tiny_beaks_are_out(self):
        for items in ((), ("Rabadon's Deathcap",) * 3):
            with self.subTest(items=items):
                spec = timing_spec("Mama Beak", items=items, duration=90.0)
                sheet, res = sim_fight(spec)
                casts = casts_of(res)
                self.assertGreaterEqual(len(casts), 3)
                beaks = hit_times(res, "tiny beaks")
                for i in range(len(casts) - 1):
                    start, end = casts[i][0], casts[i + 1][0]
                    with_flock = [t for t in attack_times(res, start, end) if t in beaks]
                    alone = [t for t in attack_times(res, start, end) if t not in beaks]
                    self.assertTrue(with_flock)
                    # the flock's whole life lies between two casts, and only
                    # the attacks made without it paid mana
                    self.assertTrue(all(t > with_flock[-1] for t in alone))
                    self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), 10.0 * len(alone))

    def test_the_lock_lasts_as_long_as_ability_power_keeps_the_flock(self):
        windows = []
        for items in ((), ("Rabadon's Deathcap",) * 3):
            _, res = sim_fight(timing_spec("Mama Beak", items=items, duration=40.0))
            (start, _, landed), (end, _, _) = casts_of(res)[:2]
            beaks = [t for t in hit_times(res, "tiny beaks") if start < t <= end]
            windows.append((beaks[-1] - landed, end - landed))
        (plain_flock, plain_gap), (ap_flock, ap_gap) = windows
        self.assertGreater(ap_flock, plain_flock)
        self.assertGreater(ap_gap, plain_gap)


class TestMurkwolf(unittest.TestCase):
    def test_empowered_attacks_grant_no_mana(self):
        sheet, res = sim_fight(timing_spec("Murkwolf", duration=30.0))
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        empowered = hit_times(res, "empowered")
        for i in range(len(casts) - 1):
            start, end = casts[i][0], casts[i + 1][0]
            claws = [t for t in empowered if start < t <= end]
            self.assertEqual(len(claws), 2)
            ordinary = [t for t in attack_times(res, start, end) if t not in claws]
            self.assertTrue(all(t > claws[-1] for t in ordinary))
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), 10.0 * len(ordinary))

    def test_regeneration_resumes_at_the_second_empowered_attack(self):
        regen = 4.0
        sheet, res = sim_fight(timing_spec("Murkwolf", duration=30.0, regen=regen))
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        empowered = hit_times(res, "empowered")
        for i in range(len(casts) - 1):
            start, end = casts[i][0], casts[i + 1][0]
            release = [t for t in empowered if start < t <= end][1]
            ordinary = [t for t in attack_times(res, start, end) if t > release]
            gained = came_in(casts, i, sheet["manaMax"]) - 10.0 * len(ordinary)
            self.assertLessEqual(gained, regen * (end - release) + 1e-9)
            self.assertGreaterEqual(gained, regen * (end - release - TICK) - 1e-9)

    def test_a_kill_grants_no_empowered_attacks(self):
        # NumEmpoweredAttacksGainedOnKill is in the data and in no tooltip: a
        # dormant row. The leap kills the weak dummy; two empowered attacks follow.
        dummy = immortal(DUMMY)
        dummy["slots"] = [dict(slot, hp=1.0 if i == 1 else slot["hp"])
                          for i, slot in enumerate(dummy["slots"])]
        spec = timing_spec("Murkwolf", duration=12.0, dummy=dummy)
        rows = spec["kits"]["base"]["rows"]
        self.assertEqual(rows["NumEmpoweredAttacks"], 2)
        self.assertEqual(rows["NumEmpoweredAttacksGainedOnKill"], 2)
        _, res = sim_fight(spec)
        casts = casts_of(res)
        kills = events(res, "kill")
        self.assertEqual([(e[3], e[4]) for e in kills], [(1, "leap")])
        end = casts[1][0] if len(casts) > 1 else float("inf")
        self.assertLess(kills[0][0], end)
        self.assertEqual(len([t for t in hit_times(res, "empowered") if casts[0][0] < t <= end]), 2)


class TestBramblebackFrenzyLock(unittest.TestCase):
    def test_no_mana_for_the_eight_seconds_of_frenzy(self):
        for items in ((), ("Blue Buff", "Spear of Shojin", "Guinsoo's Rageblade")):
            with self.subTest(items=items):
                spec = timing_spec("Brambleback", items=items, duration=60.0)
                frenzy = spec["kits"]["base"]["rows"]["Duration"]
                self.assertEqual(frenzy, 8)
                sheet, res = sim_fight(spec)
                casts = casts_of(res)
                self.assertGreaterEqual(len(casts), 3)
                for (_, _, landed), (start, _, _) in zip(casts, casts[1:]):
                    self.assertGreaterEqual(start, landed + frenzy)     # never refreshed from mana
                if not items:
                    for i in range(len(casts) - 1):
                        (start, _, landed), end = casts[i], casts[i + 1][0]
                        locked = [t for t in attack_times(res, start, end) if t < landed + frenzy]
                        free = [t for t in attack_times(res, start, end) if t >= landed + frenzy]
                        self.assertGreaterEqual(len(locked), 3)
                        self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), 10.0 * len(free))


class TestDianaBarrier(unittest.TestCase):
    def test_orbs_stay_with_the_enemies_in_reach_when_the_cast_landed(self):
        # "within 2 hexes" is measured against the board: packed, that is the
        # whole frontline plus the backliner two rows behind the target;
        # spread, only the target, and when it dies the orbs left over are
        # lost rather than flying on to the next dummy.
        for geometry, recipients in (("spread", {0}), ("clump", {0, 1, 2, 3})):
            with self.subTest(geometry=geometry):
                dummy = five_target_dummy(geometry)
                dummy["slots"][0]["hp"] = 60.0
                spec = timing_spec("Diana", geometry=geometry, duration=20.0, dummy=dummy)
                spec["kits"]["base"]["stats"].update(initialMana=spec["kits"]["base"]["stats"]["mana"])
                for kit in spec["kits"].values():
                    kit["baseAd"] = 0.0                       # the orbs, not an attack, kill it
                _, res = sim_fight(spec)
                second = casts_of(res)[1][0]
                orbs = [e for e in events(res, "damage", "orbs") if e[0] < second]
                self.assertEqual({e[3] for e in orbs}, recipients)
                kill = events(res, "kill")[0]
                self.assertEqual((kill[3], kill[4]), (0, "orbs"))
                if geometry == "spread":
                    self.assertLess(len(orbs), spec["kits"]["base"]["rows"]["NumOrbs"])
                else:
                    self.assertEqual(len(orbs), spec["kits"]["base"]["rows"]["NumOrbs"])

    def test_mana_is_locked_while_the_barrier_stands_unbroken(self):
        spec = timing_spec("Diana", star=3, duration=30.0, attack_speed=2.0)
        hold = spec["kits"]["base"]["rows"]["ShieldDuration"]
        sheet, res = sim_fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        for i in range(len(casts) - 1):
            (start, _, landed), end = casts[i], casts[i + 1][0]
            locked = [t for t in attack_times(res, start, end) if t < landed + hold]
            free = [t for t in attack_times(res, start, end) if t >= landed + hold]
            self.assertGreaterEqual(len(locked), 2)
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), 10.0 * len(free))

    def test_a_broken_barrier_releases_the_mana_early(self):
        calm = timing_spec("Diana", star=3, duration=30.0, attack_speed=4.0)
        hold = calm["kits"]["base"]["rows"]["ShieldDuration"]
        _, res = sim_fight(calm)
        landed = casts_of(res)[0][2]
        # one blow, well after any cast animation and off her attack times, that
        # spends the whole barrier
        strike = landed + 0.8 * hold
        spec = copy.deepcopy(calm)
        spec["pressure"] = True
        for i, slot in enumerate(spec["dummies"]["slots"]):
            slot.update(ad=10000.0 if i == 0 else 0.0, ability=0.0, manaMax=0.0, manaStart=0.0,
                        manaPerAttack=0.0, manaFromDamage=False, attackStart=strike, streams=1)
            slot["as"] = 0.01
        spec["dummies"]["critEv"] = 1.0
        for kit in spec["kits"].values():
            kit["hpStar"] = 10.0 ** 6                       # she survives the blow
        sheet, res = sim_fight(spec)
        casts = casts_of(res)
        self.assertEqual(casts[0][2], landed)
        blows = [e for e in events(res, "take") if e[0] <= landed + hold]
        self.assertEqual([e[0] for e in blows], [strike])
        start, end = casts[0][0], casts[1][0]
        locked = [t for t in attack_times(res, start, end) if t < strike]
        free = [t for t in attack_times(res, start, end) if t >= strike]
        early = [t for t in free if t < landed + hold]
        self.assertTrue(locked)
        self.assertTrue(early)                                # these would still be locked unbroken
        self.assertAlmostEqual(came_in(casts, 0, sheet["manaMax"]), 10.0 * len(free))


class TestPebblesDrain(unittest.TestCase):
    """Azure Laser drains the bar; nothing locks mana out while it does.
    Expectations are derived from the bar the `cast` row reports and from the
    laser's own payments, so they hold whatever instant the engine starts the
    cast at."""

    DPS = 240.0
    MANA = 70.0
    RATE = 0.35

    def laser(self, *, mana=MANA, start=MANA - 1.0, regen=0.0, duration=12.0,
              attack_speed=0.5, traits=(), kind="Caster"):
        spec = timing_spec("Pebbles", duration=duration, regen=regen, attack_speed=attack_speed,
                           traits=traits)
        spec["unit"]["kind"] = kind
        spec["targetDebuffs"] = {}
        for slot in spec["dummies"]["slots"]:
            slot.update(armor=0.0, mr=0.0)
        kit = spec["kits"]["base"]
        kit["stats"].update(mana=mana, initialMana=start, critChance=0.0, critMult=1.0)
        kit["baseAd"] = 0.0
        kit["calcs"]["MagicDamageCalc1"] = flat_term("magic", self.DPS)
        kit["rows"].update(PercentManaPerSecond=self.RATE, MRReduction=0.0)
        return spec

    def channel(self, res, index=0):
        """(channel start, bar at the cast, laser damage, last laser hit, first
        attack after the cast) of one cast. The start is read off the laser
        itself: its first payment is DPS x the time since the channel began."""
        casts = casts_of(res)
        cast, bar, _ = casts[index]
        end = casts[index + 1][0] if index + 1 < len(casts) else float("inf")
        hits = [e for e in events(res, "damage", "laser") if cast < e[0] <= end]
        start = hits[0][0] - hits[0][2] / self.DPS
        self.assertGreaterEqual(start, cast - 1e-9)
        after = attack_times(res, cast, float("inf"))
        return start, bar, sum(e[2] for e in hits), hits[-1][0], (after[0] if after else None)

    def test_the_whole_bar_drains_overflow_included(self):
        res = sim_fight(self.laser())[1]
        start, bar, damage, last, resumed = self.channel(res)
        self.assertGreater(bar, self.MANA)                    # 69 and an attack's 7: overflow
        length = bar / (self.RATE * self.MANA)
        self.assertAlmostEqual(damage, self.DPS * length)
        # the laser stops the instant the bar is empty, not on the next tick,
        # and she does not attack before that
        self.assertAlmostEqual(last - start, length)
        self.assertNotAlmostEqual(last / TICK, round(last / TICK))
        self.assertGreaterEqual(resumed, last - 1e-9)

    def test_regen_keeps_flowing_and_lengthens_the_laser(self):
        for regen in (2.0, 7.0, 12.0):
            with self.subTest(regen=regen):
                res = sim_fight(self.laser(regen=regen, duration=20.0))[1]
                start, bar, damage, last, resumed = self.channel(res)
                length = bar / (self.RATE * self.MANA - regen)
                self.assertGreater(length, bar / (self.RATE * self.MANA) + 0.25)
                self.assertAlmostEqual(damage, self.DPS * length, places=6)
                self.assertAlmostEqual(last - start, length, places=9)
                self.assertGreaterEqual(resumed, last - 1e-9)

    def test_mana_mult_scales_the_regen_that_feeds_the_channel(self):
        spec = self.laser(regen=4.0, duration=20.0)
        spec["items"].append({"api": "test", "name": "test", "unique": False, "stats": [],
                              "adds": [], "manaMult": 1.5})
        _, bar, damage, _, _ = self.channel(sim_fight(spec)[1])
        self.assertAlmostEqual(damage, self.DPS * bar / (self.RATE * self.MANA - 4.0 * 1.5), places=6)

    def test_the_bar_refills_from_the_end_of_the_channel_without_a_lock(self):
        regen = 5.0
        res = sim_fight(self.laser(start=self.MANA, regen=regen, duration=40.0, kind="Specialist"))[1]
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 2)
        _, _, _, last, _ = self.channel(res)
        # a Specialist gains nothing per attack: the second bar is regen alone,
        # counted from the instant the first channel ended
        refill = casts[1][0] - last
        self.assertLessEqual(casts[1][1], regen * refill + 1e-9)
        self.assertGreaterEqual(casts[1][1], regen * (refill - TICK) - 1e-9)
        self.assertAlmostEqual(refill, self.MANA / regen, delta=TICK + 1e-9)

    def test_a_channel_that_regen_outpaces_runs_to_the_end_of_the_fight(self):
        for regen in (self.RATE * self.MANA, 40.0):
            with self.subTest(regen=regen):
                duration = 15.0
                res = sim_fight(self.laser(regen=regen, duration=duration))[1]
                casts = casts_of(res)
                self.assertEqual(len(casts), 1)
                self.assertFalse(attack_times(res, casts[0][0], duration))
                start, _, damage, last, _ = self.channel(res)
                self.assertAlmostEqual(last, duration)
                self.assertAlmostEqual(damage, self.DPS * (duration - start))

    def teal(self, regen, **kw):
        spec = self.laser(start=self.MANA, regen=regen, duration=60.0, kind="Specialist",
                          traits=[("DA_Riftbeast18", 1)], **kw)
        rows = spec["kits"]["base"]["rows"]
        self.assertEqual((rows["TraitChannelSecondsTooltip"], rows["TraitManaRegenTooltip"]), (4, 2))
        res = sim_fight(spec)[1]
        start, bar, _, last, _ = self.channel(res)
        return start, bar, len(casts_of(res)) == 1 and abs(last - 60.0) < 1e-9

    def catch_up(self, regen):
        """Mana the drain takes before the Teal Buff's +2 regen per 4 s channeled overtakes it."""
        need, net = 0.0, self.RATE * self.MANA - regen
        while net > 0.0:
            need, net = need + 4.0 * net, net - 2.0
        return need

    def test_teal_buff_carries_the_channel_forever_once_the_bar_outlasts_the_catch_up(self):
        plain = sim_fight(self.laser(start=self.MANA, regen=18.5, duration=60.0, kind="Specialist"))[1]
        self.assertGreaterEqual(len(casts_of(plain)), 2)      # without the buff the laser ends
        for regen in (14.0, 16.0, 18.5, 21.0):
            with self.subTest(regen=regen):
                _, bar, endless = self.teal(regen)
                self.assertGreater(abs(bar - self.catch_up(regen)), 3.0)
                self.assertEqual(endless, bar > self.catch_up(regen))

    def test_tftflow_go_infinite_figure(self):
        # tftflow's 18.2b guide: "~17.1 Mana Regen + Alpha Mark to go infinite ...
        # 70 Max Mana and 35% Mana Drain". With a bar of exactly 70 cast on a tick
        # the catch-up costs 16 * (24.5 - regen) - 48 mana: 70 at regen 17.125.
        self.assertAlmostEqual(self.catch_up(17.125), 70.0)
        start, bar, _ = self.teal(17.0)
        if bar != self.MANA or abs(start / TICK - round(start / TICK)) > 1e-9:
            self.skipTest("the fixture's bar is no longer exactly 70 on a tick")
        self.assertFalse(self.teal(17.12)[2])
        self.assertTrue(self.teal(17.13)[2])


class TestAluneMoonCycle(unittest.TestCase):
    def test_the_five_phases_are_in_the_data(self):
        # The driver spells out the five (no kit row carries the count): the
        # pinned snapshot and the newest archive must both still list them.
        for snap in (SNAP, tft.load_snapshot(SNAP.set_no, None)):
            with self.subTest(patch=snap.patch):
                phases = [t for t in snap.raw["extras"]["traitTooltips"]
                          if t["apiName"].startswith("DA_AluneUniqueTrait18_Tooltip_Phase")]
                self.assertEqual([t["desc"].split(" <")[0] for t in phases],
                                 ["New Moon", "Waxing Crescent", "Half Moon", "Waxing Gibbous", "Full Moon"])
                attuned = next(t for t in snap.raw["traits"] if t["apiName"] == "DA_AluneUniqueTrait18")
                self.assertIn("cycles to a new phase of the moon after each cast", attuned["desc"])

    def test_the_moon_crashes_on_every_fifth_cast(self):
        spec = timing_spec("Alune", duration=60.0, attack_speed=2.0, regen=10.0)
        _, res = sim_fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 11)
        moons, shards = hit_times(res, "full moon"), hit_times(res, "moonshards")
        for index, (start, _, landed) in enumerate(casts, 1):
            end = casts[index][0] if index < len(casts) else float("inf")
            if landed > spec["duration"] - 1e-9:
                continue                                       # the fight ended mid-cast
            crashed = any(start <= t < end for t in moons)
            rained = any(start <= t < end for t in shards)
            self.assertEqual(crashed, index % 5 == 0, index)
            self.assertEqual(rained, index % 5 != 0, index)


class TestApheliosSwipeCount(unittest.TestCase):
    def swipes(self, bonus=0.0, items=()):
        spec = timing_spec("Aphelios", duration=30.0, items=items,
                           fx=[{"startingMana": 1000.0}, {"stats": [["asPct", bonus]]}])
        rows = spec["kits"]["base"]["rows"]
        self.assertEqual((rows["NumAttacksBase"], rows["AS_NeededForExtraSwipe"]), (5, 0.2))
        sheet, res = sim_fight(spec)
        blast = hit_times(res, "blast")[0]
        return sheet, len([t for t in hit_times(res, "swipes") if t <= blast])

    def test_an_exact_multiple_of_bonus_attack_speed_keeps_its_swipe(self):
        # a whole number of 20% steps must not come out a hair under it:
        # 0.6 / 0.2 is 2.9999999999999996 in floating point
        for bonus, expected in ((0.0, 5), (0.2, 6), (0.4, 7), (0.6, 8), (0.8, 9), (1.0, 10),
                                (1.2, 11), (1.4, 12)):
            with self.subTest(bonus=bonus):
                self.assertEqual(self.swipes(bonus)[1], expected)

    def test_giant_slayer_and_red_buff_buy_three_swipes(self):
        # +15% and +45% on 0.8: the sheet reads 1.28, the bonus 0.5999999999999999
        sheet, swipes = self.swipes(items=("Giant Slayer", "Red Buff", "Deathblade"))
        self.assertAlmostEqual(sheet["as"], 0.8 * 1.6)
        self.assertEqual(swipes, 8)

    def test_a_partial_step_still_buys_nothing(self):
        for bonus, expected in ((0.19, 5), (0.59, 7), (0.61, 8), (0.79, 8)):
            with self.subTest(bonus=bonus):
                self.assertEqual(self.swipes(bonus)[1], expected)


class TestAsheTrailCrit(unittest.TestCase):
    def test_both_halves_of_the_trail_crit_with_precision(self):
        crit = {"stats": [["crit", 0.75]]}                    # 25% + 75%: every hit crits for 1.4
        runs = {}
        for label, fx in (("plain", [crit]), ("precise", [dict(crit, precision=1)])):
            spec = timing_spec("Ashe", duration=20.0, fx=fx + [{"startingMana": 1000.0}])
            spec["targetDebuffs"] = {}
            _, runs[label] = sim_fight(spec)
        plain = events(runs["plain"], "damage", "trail")
        precise = events(runs["precise"], "damage", "trail")
        self.assertGreater(len(plain), 10)
        self.assertEqual([(e[0], e[3]) for e in plain], [(e[0], e[3]) for e in precise])
        # The flat and max-Health shares form one native trail hit per target.
        per_tick = collections.Counter((e[0], e[3]) for e in plain)
        self.assertEqual(set(per_tick.values()), {1})
        for before, after in zip(plain, precise):
            self.assertAlmostEqual(after[2] / before[2], 1.4)


class TestDravenCashOut(unittest.TestCase):
    def test_the_cashed_bleed_crits_exactly_as_its_ticks_would(self):
        crit = {"stats": [["crit", 0.75]]}
        runs = {}
        for label, fx in (("plain", [crit]), ("precise", [dict(crit, precision=1)])):
            spec = timing_spec("Draven", geometry="spread", duration=25.0, fx=fx)
            _, runs[label] = sim_fight(spec)
        for res in runs.values():
            self.assertGreaterEqual(res["casts"], 1)
        plain, precise = runs["plain"], runs["precise"]
        landed = casts_of(plain)[0][2]
        self.assertEqual(landed, casts_of(precise)[0][2])
        ticks = [[e for e in events(res, "damage", "bleed axes") if e[0] < landed] for res in (plain, precise)]
        self.assertGreater(len(ticks[0]), 10)
        for before, after in zip(*ticks):
            self.assertAlmostEqual(after[2] / before[2], 1.4)
        # out, the cashed bleed, back: three hits on the one dummy in the line
        axes = [[e for e in events(res, "damage", "giant axes") if e[0] == landed] for res in (plain, precise)]
        self.assertEqual(len(axes[0]), 3)
        for before, after in zip(*axes):
            self.assertAlmostEqual(after[2] / before[2], 1.4)
        # and nothing is left to tick until her next attack bleeds the dummy again
        for res in (plain, precise):
            again = min(t for t in attack_times(res, landed, float("inf")))
            self.assertFalse([e for e in events(res, "damage", "bleed axes") if landed < e[0] < again])


class TestMorganaBlastTargets(unittest.TestCase):
    def test_the_blast_picks_three_enemies_whatever_the_geometry(self):
        for geometry in ("spread", "clump"):
            with self.subTest(geometry=geometry):
                spec = timing_spec("Morgana", geometry=geometry, duration=20.0,
                                   dummy=immortal(five_target_dummy(geometry)))
                count = spec["kits"]["base"]["rows"]["NumEnemiesCursed"]
                self.assertEqual(count, 3)
                _, res = sim_fight(spec)
                casts = casts_of(res)
                self.assertGreaterEqual(len(casts), 2)
                for index, (start, _, landed) in enumerate(casts[:-1]):
                    end = casts[index + 1][0]
                    blast = [e[3] for e in events(res, "damage", "blast") if start <= e[0] < end]
                    self.assertEqual(blast, [0, 1, 2])
                    # The blast still picks three separate enemies whatever the
                    # geometry; the zone is a stated 2 hexes (HexRangeAOE), so
                    # packed it also covers the backliner behind the target.
                    zone = {e[3] for e in events(res, "damage", "withering zone") if start <= e[0] < end}
                    self.assertEqual(zone, {0} if geometry == "spread" else {0, 1, 2, 3})


TANK_PER_ATTACK = 5.0                                   # Kind::Tank

# The tanks whose shield holds their mana, with the row that gives TFTraits'
# "at most 4.00 s" and the highest star the unit is simulated at.
SHIELD_TANKS = (("Ornn", 3, "ShieldDuration"), ("Rakan", 3, "ShieldDuration"),
                ("Sejuani", 3, "ShieldDuration"), ("Rammus", 3, "Duration"),
                ("Malphite", 2, "ShieldDuration"), ("Sentinel", 2, "ShieldDuration"))


def one_blow(spec, at, ad):
    """The fight's pressure reduced to a single blow from the first dummy at
    `at` seconds; nothing else ever swings, casts or gains mana."""
    spec = copy.deepcopy(spec)
    spec["pressure"] = True
    for i, slot in enumerate(spec["dummies"]["slots"]):
        slot.update(ad=ad if i == 0 else 0.0, ability=0.0, manaMax=0.0, manaStart=0.0,
                    manaPerAttack=0.0, manaFromDamage=False, attackStart=at, streams=1)
        slot["as"] = 0.01
    spec["dummies"]["critEv"] = 1.0
    return spec


class TestShieldTankLocks(unittest.TestCase):
    """Ornn, Rakan, Sejuani, Rammus, Malphite and Sentinel hold every source
    of mana: attacks, the regen ticks and the share a tank takes off damage
   : for as long as their shield stands, its own duration row at most."""

    def spec(self, name, star, hp=None):
        spec = timing_spec(name, star=star, duration=60.0, attack_speed=3.0)
        self.assertEqual(spec["unit"]["kind"], "Tank")
        for kit in spec["kits"].values():
            if hp is not None:
                kit["hpStar"] = hp                      # he survives the blow below
        return spec

    def probe(self, name, star, hp):
        """The same fight with nothing hitting back: (spec, when the first
        cast landed, that shield's size, when the second cast starts)."""
        spec = self.spec(name, star, hp=hp)
        _, res = sim_fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 2)
        return spec, casts[0][2], events(res, "shield")[0][2], casts[1][0]

    def test_no_mana_while_the_shield_stands(self):
        for name, star, row in SHIELD_TANKS:
            with self.subTest(unit=name):
                spec = self.spec(name, star)
                hold = spec["kits"]["base"]["rows"][row]
                self.assertEqual(hold, 4)               # TFTraits' 4.00 s, from the kit
                sheet, res = sim_fight(spec)
                casts = casts_of(res)
                self.assertGreaterEqual(len(casts), 2)
                self.assertFalse(events(res, "take"))   # nothing hits back in this fixture
                for i in range(len(casts) - 1):
                    (start, _, landed), end = casts[i], casts[i + 1][0]
                    locked = [t for t in attack_times(res, start, end) if t < landed + hold]
                    free = [t for t in attack_times(res, start, end) if t >= landed + hold]
                    self.assertGreaterEqual(len(locked), 3)
                    self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]),
                                           TANK_PER_ATTACK * len(free))

    def test_a_broken_shield_releases_the_mana_early(self):
        for name, star, row in SHIELD_TANKS:
            with self.subTest(unit=name):
                calm, landed, barrier, _ = self.probe(name, star, 10.0 ** 6)
                hold = calm["kits"]["base"]["rows"][row]
                # one blow, clear of the cast animation and far bigger than
                # the shield, so it is spent in full well before it expires
                strike = landed + 0.5 * hold
                sheet, res = sim_fight(one_blow(calm, strike, 5.0 * barrier))
                casts = casts_of(res)
                self.assertEqual(casts[0][2], landed)
                self.assertEqual([e[0] for e in events(res, "take")], [strike])
                self.assertGreater(res["hpLeft"], 0.0)
                start, end = casts[0][0], casts[1][0]
                free = [t for t in attack_times(res, start, end) if t >= strike]
                # the blow itself is processed before the driver's hit hook,
                # so it buys nothing; the attacks after it do, and the first
                # of them would still be locked with the shield unbroken
                self.assertTrue([t for t in free if t < landed + hold])
                self.assertAlmostEqual(came_in(casts, 0, sheet["manaMax"]),
                                       TANK_PER_ATTACK * len(free))

    def test_damage_taken_inside_the_lock_buys_no_mana(self):
        # The audit's mechanism: a tank gains 1% pre + 3% post-mitigation mana
        # off every hit, which is what let Rammus and Malphite re-shield every
        # ~2.75 s while the previous shield still stood.
        for name, star, row in SHIELD_TANKS:
            with self.subTest(unit=name):
                calm, landed, barrier, second = self.probe(name, star, 10.0 ** 6)
                hold = calm["kits"]["base"]["rows"][row]
                swallowed = 0.5 * barrier               # never breaks the shield
                inside = sim_fight(one_blow(calm, landed + 0.5 * hold, swallowed))[1]
                self.assertEqual([e[0] for e in events(inside, "take")],
                                 [landed + 0.5 * hold])
                self.assertEqual(casts_of(inside)[1][0], second)
                # the same blow once the shield has gone does pay for itself
                after = sim_fight(one_blow(calm, landed + hold + 0.5, swallowed))[1]
                self.assertLess(casts_of(after)[1][0], second)


class TestShenKiStrikeLock(unittest.TestCase):
    """TFTraits: "Attack speed shortens this lock: it lasts 3 empowered
    attacks", and it blocks "mana from attacks, ticks or damage taken"."""

    def spec(self, attack_speed=2.0):
        spec = timing_spec("Shen", star=3, duration=60.0, attack_speed=attack_speed)
        self.assertEqual(spec["unit"]["kind"], "Tank")
        self.assertEqual(spec["kits"]["base"]["rows"]["NumAttacksBuff"], 3)
        return spec

    def test_the_ki_strikes_grant_no_mana(self):
        spec = self.spec()
        sheet, res = sim_fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        ki = hit_times(res, "ki strike")
        for i in range(len(casts) - 1):
            start, end = casts[i][0], casts[i + 1][0]
            struck = [t for t in ki if start < t <= end]
            self.assertEqual(len(struck), 3)
            ordinary = [t for t in attack_times(res, start, end) if t not in struck]
            self.assertTrue(all(t > struck[-1] for t in ordinary))
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]),
                                   TANK_PER_ATTACK * len(ordinary))

    def test_attack_speed_shortens_the_lock(self):
        # the window is three of his own attacks, so tripling his attack
        # speed thirds the time they take
        windows = []
        for attack_speed in (1.0, 3.0):
            _, res = sim_fight(self.spec(attack_speed))
            ki = hit_times(res, "ki strike")
            windows.append(ki[2] - ki[0])
        self.assertAlmostEqual(windows[0], 3.0 * windows[1], delta=1e-9)

    def test_damage_taken_before_the_third_strike_buys_no_mana(self):
        calm = self.spec()
        for kit in calm["kits"].values():
            kit["hpStar"] = 10.0 ** 6
        _, res = sim_fight(calm)
        casts = casts_of(res)
        release = hit_times(res, "ki strike")[2]
        hit = 2000.0                        # worth the whole per-hit mana cap
        # inside: between the cast landing and the third ki strike
        inside = sim_fight(one_blow(calm, 0.5 * (casts[0][2] + release), hit))[1]
        self.assertEqual(len(events(inside, "take")), 1)
        self.assertEqual(casts_of(inside)[1][0], casts[1][0])
        after = sim_fight(one_blow(calm, release + 0.5, hit))[1]
        self.assertLess(casts_of(after)[1][0], casts[1][0])


class TestEffectDurationLocks(unittest.TestCase):
    """Elise and Vi: "The mana lock lasts the N s the effect runs. Attacks
    continue.": N is the kit's own row, and no second is added after it."""

    def locked(self, name, row, seconds, star=3):
        spec = timing_spec(name, star=star, duration=60.0, attack_speed=3.0)
        self.assertEqual(spec["unit"]["kind"], "Tank")
        hold = spec["kits"]["base"]["rows"][row]
        self.assertEqual(hold, seconds)                 # TFTraits' seconds, from the kit
        sheet, res = sim_fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        seen_early = False
        for i in range(len(casts) - 1):
            (start, _, landed), end = casts[i], casts[i + 1][0]
            locked = [t for t in attack_times(res, start, end) if t < landed + hold]
            free = [t for t in attack_times(res, start, end) if t >= landed + hold]
            self.assertGreaterEqual(len(locked), 3)
            seen_early |= any(t < landed + hold + 1.0 for t in free)
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]),
                                   TANK_PER_ATTACK * len(free))
        self.assertTrue(seen_early)     # an attack inside the old extra second paid mana
        return casts, res

    def test_vi_is_locked_for_the_roar_and_no_longer(self):
        self.locked("Vi", "SpellDuration", 3)

    def test_elise_is_locked_for_the_decaying_attack_speed(self):
        casts, res = self.locked("Elise", "ASBuffDuration", 4)
        # The first cast is the transform, which grants no attack speed;
        # TFTraits publishes one lock for the spell, so it is held for the
        # same four seconds, which the loop above already checked.
        fangs = hit_times(res, "spider fangs")
        self.assertTrue(fangs)
        self.assertGreater(fangs[0], casts[0][2])


class TestChargeAndFeatherLocks(unittest.TestCase):
    def test_tristana_is_locked_for_the_charge_and_not_a_second_longer(self):
        spec = timing_spec("Tristana", duration=40.0, attack_speed=1.5)
        charge = spec["kits"]["base"]["rows"]["Duration"]
        sheet, res = sim_fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        seen_early = False
        for i in range(len(casts) - 1):
            (start, _, landed), end = casts[i], casts[i + 1][0]
            locked = [t for t in attack_times(res, start, end) if t < landed + charge]
            free = [t for t in attack_times(res, start, end) if t >= landed + charge]
            self.assertTrue(locked)
            seen_early |= any(t < landed + charge + 1.0 for t in free)
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), 10.0 * len(free))
        self.assertTrue(seen_early)       # an attack inside the old extra second paid mana

    def test_xayah_gains_mana_again_right_after_the_fifth_feather(self):
        spec = timing_spec("Xayah", star=3, duration=40.0, attack_speed=2.0)
        self.assertEqual(spec["kits"]["base"]["rows"]["NumAttacks"], 5)
        sheet, res = sim_fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        feathers = hit_times(res, "feathers")
        seen_early = False
        for i in range(len(casts) - 1):
            start, end = casts[i][0], casts[i + 1][0]
            thrown = [t for t in feathers if start <= t <= end]
            self.assertEqual(len(thrown), 5)
            ordinary = [t for t in attack_times(res, start, end) if t not in thrown]
            self.assertTrue(all(t > thrown[-1] for t in ordinary))
            seen_early |= any(t < thrown[-1] + 1.0 for t in ordinary)
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), 10.0 * len(ordinary))
        self.assertTrue(seen_early)


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
                        res = carry_result(self.spec(name, ratio, nearby=False,
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
                        res = carry_result(spec)
                        self.assertEqual(res["castTimes"], [0])
                        self.assertAlmostEqual(res["probe"]["mana"], 7 if name == "Ashe" else 10)
                        ability = events(res, "damage", "ability" if name == "Ashe" else "giant axes")
                        self.assertEqual(bool(ability), damage > 0)

    def test_old_snapshots_without_refund_row_keep_old_mana(self):
        for name in ("Ashe", "Draven"):
            for multiplier in (1, 1.15):
                with self.subTest(name=name, multiplier=multiplier):
                    res = carry_result(self.spec(name, None, nearby=False, multiplier=multiplier))
                    self.assertEqual(res["castTimes"], [0])
                    self.assertEqual(res["total"], 0)
                    opening_mana = 7 if name == "Ashe" else 10
                    self.assertAlmostEqual(res["probe"]["mana"], opening_mana * multiplier)

    def test_a_full_refunded_bar_can_start_another_cast_after_animation(self):
        for name, ratio, opening_mana in (("Ashe", 0.7, 7), ("Draven", 0.5, 10)):
            with self.subTest(name=name):
                spec = self.spec(name, ratio, nearby=False, multiplier=2)
                spec["unit"]["timing"] = None
                res = carry_result(spec)
                self.assertEqual(res["castTimes"], [0, 0.25])
                # The first refund fills the bar; the second cast spends it
                # at .25 s and will refund when it lands, beyond this horizon.
                self.assertAlmostEqual(res["probe"]["mana"],
                                       (opening_mana + 1000 * ratio) * 2 - 1000)


if __name__ == "__main__":
    unittest.main()
