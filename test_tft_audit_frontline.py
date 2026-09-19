"""Source-based regressions for confirmed frontline mechanics corrections.

The pinned Fiddlesticks text explicitly selects NumTargets nearest enemies.
These tests assert recipients and independently compute the drain after his
flat MR strip; passing a historical output fixture alone would not do that.
"""

import copy
import math
import unittest

from test_tft import ENGINE, events, one_hitter, spec_for
from test_tft_team_engine import ally, enemy, fight, flat_calc, trace
from test_tft_symmetric import match as champion_match, events as match_events
from test_tft_theory_pressure import measure


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


class TestTheoryTargetConditions(unittest.TestCase):
    def conserved(self, result, pressure):
        self.assertAlmostEqual(result["incomingBudget"], pressure * result["elapsed"])
        self.assertAlmostEqual(result["incomingBudget"],
            math.fsum(sample["incomingSpent"] + sample["denied"] for sample in result["samples"])
            + result["unspentPressure"])

    def test_allied_theory_damage_wakes_once_with_source_credit_and_canonical_order(self):
        lillia = lullaby_spec()
        carry = copy.deepcopy(lillia)
        carry["unit"].update(api="TFT18_Ashe", name="Ashe")
        carry["driver"] = "Driver"
        carry["kits"]["base"]["baseAd"] = 600
        carry["kits"]["base"]["stats"].update(ad=600, mana=0, initialMana=0)
        carry["kits"]["base"]["stats"]["as"] = 2
        result = measure([lillia, carry], [True, False], pressure=100, window=2.1)
        self.assertAlmostEqual(result["samples"][0]["damage"], 135 + 1000)
        reverse = measure([carry, lillia], [False, True], pressure=100, window=2.1)
        self.assertEqual(result, dict(reverse, samples=list(reversed(reverse["samples"])),
            targetOrder=[1 - index for index in reverse["targetOrder"]],
            initialSourceTargets=[None if index is None else 1 - index
                                  for index in reverse["initialSourceTargets"]]))
        self.conserved(result, 100)

    def test_theory_ally_burn_counts_and_expiry_alone_does_not(self):
        lillia = lullaby_spec()
        carry = copy.deepcopy(lillia)
        carry["unit"].update(api="TFT18_Ashe", name="Ashe")
        carry["driver"] = "Driver"
        carry["kits"]["base"]["stats"].update(mana=0, initialMana=0)
        carry["items"] = [{"api": "test", "name": "test", "unique": False,
                           "stats": [], "burnOnHit": [0.2, 3]}]
        result = measure([lillia, carry], [True, False], pressure=100, window=2.1)
        self.assertAlmostEqual(result["samples"][0]["damage"], 1135)
        carry["items"] = []
        expired = measure([lillia, carry], [True, False], pressure=100, window=2.1)
        self.assertAlmostEqual(expired["samples"][0]["damage"], 135)
        self.conserved(result, 100)
        self.conserved(expired, 100)

    def test_theory_flat_mr_strip_is_shared_without_reapplying_synchronized_totals(self):
        fiddle = harvest_spec(target_count=1)
        carry = copy.deepcopy(fiddle)
        carry["unit"].update(api="TFT18_KogMaw", name="KogMaw")
        carry["driver"] = "Driver"
        carry["kits"]["base"]["baseAd"] = 100
        carry["kits"]["base"]["stats"].update(ad=100, mana=0, initialMana=0, critChance=0, critMult=1)
        carry["kits"]["base"]["stats"]["as"] = 1
        carry["traits"] = [{"api": "test", "name": "test", "stats": [], "bonusMagicPct": 1}]
        before = measure([fiddle, carry], [True, False], pressure=100, window=2.1)
        # Fiddlesticks strips10 at0, and no extra strip occurs on later
        # synchronization. Three100 physical autos each add100/(1+.9) magic.
        self.assertAlmostEqual(before["samples"][1]["damage"], 3 * (100 + 100 / 1.9))
        self.assertAlmostEqual(before["samples"][0]["damage"], 105 / 1.9)
        after = measure([carry, fiddle], [False, True], pressure=100, window=2.1)
        self.assertEqual(before, dict(after, samples=list(reversed(after["samples"])),
            targetOrder=[1 - index for index in after["targetOrder"]],
            initialSourceTargets=[None if index is None else 1 - index
                                  for index in after["initialSourceTargets"]]))
        self.conserved(before, 100)

    def test_theory_distinct_flat_reductions_add_once_for_armor_and_mr(self):
        from test_tft_execute import gnar_spec
        fiddle = harvest_spec(target_count=1)
        gnar = gnar_spec(transform=0)
        gnar["kits"]["base"]["calcs"]["GenericCalc1"]["terms"] = [
            {"type": "flat", "value": 20, "op": "add"}]
        carry = copy.deepcopy(fiddle)
        carry["unit"].update(api="TFT18_KogMaw", name="KogMaw")
        carry["driver"] = "Driver"
        carry["kits"]["base"]["baseAd"] = 100
        carry["kits"]["base"]["stats"].update(ad=100, mana=0, initialMana=0, critChance=0, critMult=1)
        carry["kits"]["base"]["stats"]["as"] = 1
        carry["traits"] = [{"api": "test", "name": "test", "stats": [], "bonusMagicPct": 1}]
        for spec in (fiddle, gnar, carry):
            spec["dummies"]["slots"] = [dict(fiddle["dummies"]["slots"][0], armor=100)]
        result = measure([fiddle, gnar, carry], [True, True, False], pressure=100, window=2.1)
        # Gnar adds20 armor/MR strip and Fiddle adds10 MR strip. The shared
        # values are20/30, not max(20,10) or multiples of those totals.
        self.assertAlmostEqual(result["samples"][2]["damage"], 3 * (100 / 1.8 + 100 / 1.8 / 1.7))
        self.assertAlmostEqual(result["samples"][0]["damage"], 105 / 1.7)
        self.conserved(result, 100)


if __name__ == "__main__":
    unittest.main()
