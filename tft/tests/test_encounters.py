"""Symmetric match, nine-unit, and prepared-actor encounter tests."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import gc
import json
import math
from pathlib import Path
import struct
import unittest

from tft import board as team
from tft import sim as tft
from tft.board import resolve_board_traits
from tft.tests.helpers import (
    ENGINE,
    SNAP,
    ally,
    caster,
    events as solo_events,
    flat_calc,
    generic_targets,
    match,
    match_events as events,
    spec_for,
)

THORNMAIDEN = "DA_18_ZyraUniqueTrait"


def brambleback():
    member = ally("Brambleback", driver="Brambleback", ad=100,
                  attack_speed=1, cast=True)
    flat_calc(member, "GenericCalc1", 0.5)
    member["spec"]["kits"]["base"]["rows"].update(
        Duration=1.0, FrenzyADPercent=0.0)
    return member


def bits(value):
    """Compare floating-point bits, list order, dictionary order and types."""
    if isinstance(value, float):
        return (float, struct.pack("!d", value))
    if isinstance(value, dict):
        return (dict, [(key, bits(item)) for key, item in value.items()])
    if isinstance(value, list):
        return (list, [bits(item) for item in value])
    return (type(value), value)


def prepared(spec):
    return {**spec, **{side: [ENGINE.prepare_actor(entry) for entry in spec[side]]
                     for side in ("allies", "enemies")}}


def mirror(allies, enemies, side, **options):
    """Give the same designated board the same initiative on either side."""
    return (match(allies, enemies, **options) if side == "ally" else
            match(enemies, allies, initiative=1, **options))


class TestSymmetricDamage(unittest.TestCase):
    def test_identical_duel_obeys_equal_time_initiative(self):
        board = [ally(hp=100, ad=100, attack_speed=1)]
        first = match(board, board, initiative=0)
        second = match(board, board, initiative=1)
        self.assertEqual((first["outcome"], second["outcome"]), ("win", "loss"))
        self.assertEqual(first["damage"], second["enemyDamage"])
        self.assertEqual(first["allyHpLeft"], second["enemyHpLeft"])
        self.assertEqual(first["enemies"][0]["attacks"], 0)

    def test_swap_sides_and_initiative_preserves_fight(self):
        left = [ally(hp=500, ad=90, attack_speed=1, armor=35)]
        right = [ally(hp=700, ad=50, attack_speed=1.5, armor=70)]
        first = match(left, right, duration=12, initiative=0)
        reverse = match(right, left, duration=12, initiative=1)
        self.assertEqual(first["duration"], reverse["duration"])
        self.assertEqual(first["allies"], reverse["enemies"])
        self.assertEqual(first["enemies"], reverse["allies"])
        self.assertEqual(first["allyHpFraction"], reverse["enemyHpFraction"])

    def test_empty_dummy_inputs_are_replaced_without_mutating_callers(self):
        member = ally(ad=100)
        member["spec"]["dummies"] = {"slots": []}
        before = deepcopy(member)
        result = match([member], [ally()], duration=0.1)
        self.assertEqual(result["damage"], 100)
        self.assertEqual(member, before)
        with self.assertRaises(ValueError):
            ENGINE.simulate(member["spec"], False)

    def test_armor_durability_and_shield_apply_exactly_once(self):
        tank = ally(armor=100, fx=[{"durability": 0.2}, {"shieldAtStart": [0.03, 10.0]}])
        result = match([ally(ad=100)], [tank], duration=0.1)
        # 100 / 2 armor * 0.8 durability = 40 damage, 30 on shield and 10 HP.
        self.assertAlmostEqual(result["damage"], 40.0)
        self.assertAlmostEqual(result["enemyHpLeft"], 990.0)
        self.assertAlmostEqual(result["enemies"][0]["shielding"], 30.0)
        self.assertAlmostEqual(result["enemies"][0]["damageTaken"], 40.0)

    def test_real_enemy_spell_uses_magic_resist_and_durability(self):
        tank = ally(mr=100, fx=[{"durability": 0.2}])
        result = match([tank], [caster()], duration=0.3)
        self.assertEqual(result["enemies"][0]["casts"], 1)
        self.assertEqual([event["time"] for event in events(result, "land", "enemy")], [0.25])
        self.assertAlmostEqual(result["enemyDamage"], 40.0)
        self.assertAlmostEqual(result["allyHpLeft"], 960.0)

    def test_overkill_donor_credit_includes_spent_shield_but_no_extra_health(self):
        donor = ally(ad=1000, frontline=False, fx=[{"allyHealPct": 0.2}])
        target = ally(hp=50, ad=100, fx=[{"shieldAtStart": [0.6, 10.0]}])
        result = match([ally(), donor], [target], duration=0.1, initiative=1)
        self.assertEqual(result["damage"], 80.0)
        self.assertEqual(result["enemies"][0]["damageTaken"], 80.0)
        self.assertEqual(result["allies"][1]["allyHealing"], 16.0)
        self.assertEqual(result["enemyHpLeft"], 0.0)

    def test_enemy_death_removes_attacks_and_exposes_next_target(self):
        result = match([ally(ad=100, attack_speed=1)], [
            ally(hp=50, ad=500), ally(hp=1000, ad=10, frontline=False),
        ], duration=1.1)
        self.assertEqual(result["enemies"][0]["attacks"], 0)
        self.assertEqual(result["enemies"][1]["attacks"], 1)
        self.assertEqual([(e["time"], e["target"]) for e in events(result, "damage", name="auto")],
                         [(0.0, 0), (1.0, 1)])
        self.assertEqual(result["allies"][0]["damageTaken"], 10.0)

    def test_last_target_untargetability_waits_without_rewinding_clock(self):
        target = ally(fx=[{"untargetableAtHp": [0.9, 2.0, 0.0]}])
        result = match([ally(ad=200, attack_speed=2)], [target], duration=2.6)
        self.assertEqual([e["time"] for e in events(result, "attack")], [0.0, 2.0, 2.5])
        self.assertEqual(result["duration"], 2.6)
        # The Edge-style 90% floor prevents 100 of the opening 200 hit.
        # Both later hits receive full credit once the effect is spent.
        self.assertEqual(result["damage"], 500.0)


class TestSymmetricAbilities(unittest.TestCase):
    def test_incoming_damage_mana_enables_enemy_tank_cast(self):
        tank = caster("Leona")
        tank["spec"]["kits"]["base"]["stats"].update(mana=10.0, initialMana=0.0)
        with_damage = match([ally(ad=100, attack_speed=1)], [tank], duration=1.3)
        without_damage = match([ally()], [tank], duration=1.3)
        self.assertEqual([e["time"] for e in events(with_damage, "cast", "enemy")], [1.0])
        self.assertEqual(with_damage["enemies"][0]["casts"], 1)
        self.assertEqual(without_damage["enemies"][0]["casts"], 0)

    def test_enemy_mana_item_advances_its_actual_spell(self):
        normal = caster(attack_speed=1)
        normal["spec"]["kits"]["base"]["stats"].update(mana=20.0, initialMana=0.0)
        faster = deepcopy(normal)
        faster["spec"]["items"].append({"api": "test", "name": "test", "unique": False,
                                          "stats": [], "adds": [], "manaPerAttack": 5.0})
        base = match([ally()], [normal], duration=2.3)
        item = match([ally()], [faster], duration=2.3)
        self.assertEqual(events(base, "cast", "enemy")[0]["time"], 2.0)
        self.assertEqual(events(item, "cast", "enemy")[0]["time"], 1.0)

    def test_real_enemy_heal_repairs_damage_in_shared_pool(self):
        healer = ally("Alistar", driver="Alistar", cast=True)
        flat_calc(healer, "HealthCalc1", 100)
        flat_calc(healer, "HealthCalc2", 0)
        flat_calc(healer, "MagicDamageCalc1", 0)
        result = match([ally(ad=200)], [healer], duration=0.3)
        self.assertEqual(result["damage"], 200.0)
        self.assertEqual(result["enemyHpLeft"], 900.0)
        self.assertEqual(result["enemies"][0]["healing"], 100.0)

    def test_real_enemy_shield_absorbs_later_attacks(self):
        shen = ally("Shen", driver="Shen", cast=True)
        flat_calc(shen, "ShieldCalc1", 150)
        flat_calc(shen, "ShieldCalc2", 0)
        result = match([ally(ad=100, attack_speed=1)], [shen], duration=1.1)
        self.assertEqual(result["enemies"][0]["casts"], 1)
        self.assertEqual(result["enemies"][0]["shielding"], 100.0)
        self.assertEqual(result["enemyHpLeft"], 900.0)
        self.assertEqual(result["damage"], 200.0)

    def test_stun_blocks_enemy_attacks_until_expiry(self):
        leona = caster("Leona", damage=0)
        leona["spec"]["kits"]["base"]["rows"]["StunDuration"] = 1.0
        result = match([leona], [ally(ad=100, attack_speed=2)], duration=1.3)
        self.assertEqual([e["time"] for e in events(result, "attack", "enemy")], [0.0, 1.25])
        self.assertEqual(result["enemyDamage"], 200.0)

    def test_quicksilver_duration_blocks_new_cc_and_expires_exactly(self):
        for duration, land, expected in ((0.3, 0.25, [0.0, 0.5, 1.0]),
                                         (0.5, 0.5, [0.0, 1.5])):
            with self.subTest(immune_duration=duration):
                leona = caster("Leona", damage=0)
                leona["spec"]["unit"]["castTime"] = land
                leona["spec"]["kits"]["base"]["rows"]["StunDuration"] = 1.0
                target = ally(ad=100, attack_speed=2, fx=[{"ccImmuneDuration": duration}])
                result = match([leona], [target], duration=1.1 if land == 0.25 else 1.6)
                self.assertEqual([e["time"] for e in events(result, "attack", "enemy")], expected)

    def test_titans_immunity_activates_on_the_hit_reaching_max_stacks(self):
        leona = caster("Leona", damage=100)
        leona["spec"]["kits"]["base"]["rows"]["StunDuration"] = 1.0
        for immune, expected in ((False, [0.0, 1.25]), (True, [0.0, 0.5, 1.0])):
            with self.subTest(unstoppable=immune):
                # Opening attack supplies one stack; Leona's ability hit
                # supplies the second, before its stun is applied.
                target = ally(ad=100, attack_speed=2, fx=[{
                    "adapPerAttack": [0.01, 2.0, 0.0], "adapPerHit": True,
                    "unstoppableAtMaxStacks": immune,
                }])
                result = match([leona], [target], duration=1.3 if not immune else 1.1)
                self.assertEqual([e["time"] for e in events(result, "attack", "enemy")], expected)

    def test_vi_is_unstoppable_during_her_resolved_ability_duration(self):
        vi = ally("Vi", driver="Vi", ad=100, attack_speed=2, cast=True)
        vi["spec"]["kits"]["base"]["rows"].update(SpellDuration=1.0, SpellAS=1.0)
        leona = caster("Leona", damage=0)
        leona["spec"]["unit"]["castTime"] = 0.5
        leona["spec"]["kits"]["base"]["rows"]["StunDuration"] = 1.0
        result = match([leona], [vi], duration=1.1)
        self.assertEqual([e["time"] for e in events(result, "attack", "enemy")], [0.0, 0.5, 1.0])

    def test_sentinel_reaves_actual_enemy_mana_and_delays_first_cast(self):
        sentinel = ally("Sentinel", driver="Sentinel", cast=True)
        flat_calc(sentinel, "MagicDamageCalc1", 0)
        flat_calc(sentinel, "ShieldCalc1", 0)
        rows = sentinel["spec"]["kits"]["base"]["rows"]
        rows.update(KnockupDuration=0.0, ManaReaveFlat=7.0)
        target = caster(attack_speed=1)
        target["spec"]["kits"]["base"]["stats"].update(mana=20.0, initialMana=0.0)
        reaved = match([sentinel], [target], duration=3.3)
        rows["ManaReaveFlat"] = 0.0
        normal = match([sentinel], [target], duration=3.3)
        self.assertEqual(events(normal, "cast", "enemy")[0]["time"], 2.0)
        self.assertEqual(events(reaved, "cast", "enemy")[0]["time"], 3.0)

    def test_on_hit_sunder_changes_actual_enemy_mitigation_once(self):
        attacker = ally(ad=100, attack_speed=1, fx=[{"sunderOnHit": [0.3, 4.0]}])
        result = match([attacker], [ally(armor=100)], duration=1.1)
        hits = events(result, "damage", name="auto")
        self.assertAlmostEqual(hits[0]["amount"], 50.0)
        self.assertAlmostEqual(hits[1]["amount"], 100.0 / 1.7)
        self.assertAlmostEqual(result["enemyHpLeft"], 1000.0 - 50.0 - 100.0 / 1.7)

    def test_wound_reduces_real_enemy_healing(self):
        attacker = ally(ad=200, fx=[{"burnOnHit": [0.01, 4.0]}])
        healer = ally("Alistar", driver="Alistar", cast=True)
        flat_calc(healer, "HealthCalc1", 100)
        flat_calc(healer, "HealthCalc2", 0)
        flat_calc(healer, "MagicDamageCalc1", 0)
        result = match([attacker], [healer], duration=0.3)
        self.assertAlmostEqual(result["enemies"][0]["healing"], 67.0)

    def test_reciprocal_thorns_settles_once_without_recursion(self):
        thorns = {"thorns": [20.0, 1.0]}
        result = match([ally(ad=100, fx=[thorns])], [ally(ad=100, fx=[thorns])], duration=0.1)
        self.assertEqual(result["damage"], 120.0)
        self.assertEqual(result["enemyDamage"], 120.0)
        self.assertEqual(len(events(result, "damage", name="thorns")), 1)
        self.assertEqual(len(events(result, "damage", "enemy", name="thorns")), 1)

    def test_ionic_spark_reacts_to_real_enemy_cast_once(self):
        holder = ally(fx=[{"ionicSpark": 1.6}])
        spell = caster()
        spell["spec"]["kits"]["base"]["stats"].update(mana=100.0, initialMana=100.0)
        result = match([holder], [spell], duration=0.3)
        zaps = events(result, "damage", name="ionic spark")
        self.assertEqual(len(zaps), 1)
        self.assertEqual(zaps[0]["amount"], 160.0)
        self.assertEqual(result["enemyDamage"], 100.0)

    def test_scuttle_burrow_prevents_attacks_on_both_sides(self):
        scuttle = ally("Scuttlecrab", driver="Scuttlecrab", hp=10000, ad=50,
                       attack_speed=2, cast=True)
        result = match([scuttle], [scuttle], duration=3.3)
        for side in ("ally", "enemy"):
            self.assertEqual([e["time"] for e in events(result, "attack", side)], [0.0, 3.25])

    def test_body_holds_after_champion_dies_without_casting(self):
        yorick = ally("Yorick", driver="Yorick", hp=50, cast=True)
        flat_calc(yorick, "HealthCalc2", 100)
        result = match([ally(ad=100, attack_speed=1)], [yorick], duration=1.1, initiative=1)
        self.assertFalse(result["enemies"][0]["alive"])
        self.assertEqual(result["enemies"][0]["casts"], 1)
        self.assertFalse(events(result, "land", "enemy"))
        self.assertGreater(result["enemyHpLeft"], 0.0)
        self.assertEqual(result["enemies"][0]["attacks"], 1)

    def test_killing_hit_does_not_put_its_on_hit_burn_on_fresh_body(self):
        yorick = ally("Yorick", driver="Yorick", hp=50)
        flat_calc(yorick, "HealthCalc2", 200)
        attacker = ally(ad=100, fx=[{"burnOnHit": [0.01, 4.0], "sunderOnHit": [0.3, 4.0]}])
        result = match([attacker], [yorick], duration=0.3)
        self.assertFalse(result["enemies"][0]["alive"])
        self.assertEqual(result["enemyHpLeft"], 200.0)
        self.assertEqual(result["damage"], 50.0)
        self.assertFalse(events(result, "damage", name="burn"))

    def test_fresh_body_is_targetable_at_next_attack_without_extra_tick_delay(self):
        # Unit extras originate in the shared archive snapshot. Isolate
        # this synthetic spirit armor so later fights keep their real stats.
        yorick = deepcopy(ally("Yorick", driver="Yorick", hp=50))
        flat_calc(yorick, "HealthCalc2", 200)
        yorick["spec"]["unit"]["extras"]["TFT18_Yorick_Spirit"].update(armor=0.0, mr=0.0)
        result = match([ally(ad=100, attack_speed=5)], [yorick], duration=0.21)
        self.assertEqual([e["time"] for e in events(result, "attack")], [0.0, 0.2])
        self.assertEqual(result["damage"], 150.0)
        self.assertEqual(result["enemyHpLeft"], 100.0)

    def test_applied_poison_and_burn_do_not_transfer_to_fresh_body(self):
        draven = ally("Draven", driver="Draven", fx=[{"burnOnHit": [0.01, 4.0]}])
        flat_calc(draven, "PhysicalDamageCalc1", 100)
        flat_calc(draven, "GenericCalc1", 0)
        draven["spec"]["kits"]["base"]["rows"]["BleedDuration"] = 2.0
        yorick = ally("Yorick", driver="Yorick", hp=100)
        flat_calc(yorick, "HealthCalc2", 200)
        result = match([draven, ally(ad=200)], [yorick], duration=0.6)
        self.assertEqual(result["enemyHpLeft"], 200.0)
        self.assertEqual(result["allies"][0]["damage"], 0.0)
        self.assertEqual(result["allies"][1]["damage"], 100.0)

    def test_lethal_poison_tick_stops_at_original_body_generation(self):
        draven = ally("Draven", driver="Draven")
        flat_calc(draven, "PhysicalDamageCalc1", 200)
        flat_calc(draven, "GenericCalc1", 0)
        draven["spec"]["kits"]["base"]["rows"]["BleedDuration"] = 1.0
        yorick = ally("Yorick", driver="Yorick", hp=30)
        flat_calc(yorick, "HealthCalc2", 200)
        result = match([draven], [yorick], duration=1.1)
        self.assertEqual(result["enemyHpLeft"], 200.0)
        self.assertEqual(result["damage"], 30.0)


class TestSymmetricGunblade(unittest.TestCase):
    def test_one_hit_heals_only_lowest_percent_ally_and_discards_overheal(self):
        donor = ally(ad=100, attack_speed=1, frontline=False, fx=[{"allyHealPct": 0.2}])
        result = match([ally(hp=100, lane=1), ally(hp=100, lane=5), donor], [
            ally(ad=5, lane=1), ally(ad=6, lane=5),
        ], duration=1.1, initiative=0)
        heals = events(result, "allyHeal", source=2)
        self.assertEqual([(e["time"], e["target"], e["amount"]) for e in heals], [(1.0, 1, 6.0)])

    def test_self_omnivamp_is_distinct_from_ally_healing(self):
        donor = ally(ad=100, attack_speed=1, lane=5,
                     fx=[{"stats": [["omnivamp", 0.15]], "allyHealPct": 0.2}])
        result = match([ally(lane=1), donor], [ally(ad=100, lane=1), ally(ad=100, lane=5)], duration=1.1)
        self.assertEqual(result["allies"][1]["healing"], 15.0)
        self.assertEqual(result["allies"][1]["allyHealing"], 20.0)

    def test_proc_healing_option_preserves_gunblade_source_gate(self):
        holder = ally(ad=1, fx=[{"burnOnHit": [0.01, 4.0], "allyHealPct": 0.2,
                               "stats": [["omnivamp", 0.2]]}])
        target = ally(hp=1000, ad=100, lane=3)
        board = [holder, ally(lane=5)]
        yes = match(board, [target], duration=0.3, damageHealingFromProcs=True)
        no = match(board, [target], duration=0.3, damageHealingFromProcs=False)
        self.assertTrue(yes["damageHealingFromProcs"])
        self.assertFalse(no["damageHealingFromProcs"])
        # The item burn can self-heal under the option, but cannot feed Gunblade.
        self.assertAlmostEqual(yes["allies"][0]["healing"], 0.5)
        self.assertEqual(no["allies"][0]["healing"], 0.0)
        self.assertEqual(yes["allies"][0]["allyHealing"], 0.0)
        self.assertEqual(no["allies"][0]["allyHealing"], 0.0)

    def test_postdeath_ally_healing_assumption_is_explicit(self):
        holder = ally("Cassiopeia", driver="Cassiopeia", cast=True, hp=50,
                      ad=0, lane=1, fx=[{"allyHealPct": 0.2}])
        flat_calc(holder, "MagicDamageCalc1", 100)
        holder["spec"]["kits"]["base"]["rows"]["Duration"] = 1.0
        survivor = ally(lane=5)
        attackers = [ally(ad=100, lane=1), ally(ad=100, lane=5)]
        for attacker in attackers:
            attacker["spec"]["unit"]["timing"] = {"base": {"attackDelay": 0.5}}
        # Unit-sourced poison remains eligible after its owner dies at 0.5.
        yes = match([holder, survivor], attackers, duration=0.81, postDeathAllyHealing=True)
        no = match([holder, survivor], attackers, duration=0.81, postDeathAllyHealing=False)
        self.assertFalse(yes["allies"][0]["alive"])
        self.assertAlmostEqual(yes["allies"][0]["allyHealing"], 10.0)
        self.assertEqual(no["allies"][0]["allyHealing"], 0.0)


class TestSymmetricCoverage(unittest.TestCase):
    def test_multiple_real_drivers_share_health_and_conserve_damage_credit(self):
        units = tft.modeled_units(SNAP)
        for start in range(0, len(units), 8):
            with self.subTest(group=start):
                members = []
                for index, unit in enumerate(units[start:start + 8]):
                    spec = spec_for(unit["name"], star=2, pressure=True,
                                    items=("Hextech Gunblade", "Bloodthirster", "Bramble Vest"))
                    members.append({"spec": spec, "frontline": index < 4, "lane": index % 7,
                                    "priority": index})
                result = match(members, members, duration=6.0)
                self.assertAlmostEqual(result["damage"], sum(u["damageTaken"] for u in result["enemies"]))
                self.assertAlmostEqual(result["enemyDamage"], sum(u["damageTaken"] for u in result["allies"]))

    def test_every_current_driver_can_fight_on_either_side(self):
        units = [unit for unit in SNAP.units.values() if unit.get("shop") and unit["api"] in ENGINE.DRIVERS]
        if not units:
            units = tft.modeled_units(SNAP)
        self.assertEqual(len(units), 65)
        for unit in units:
            with self.subTest(champion=unit["name"]):
                spec = spec_for(unit["name"], star=2, pressure=True)
                member = {"spec": spec, "frontline": True, "lane": 3, "priority": 0}
                result = match([member], [member], duration=6.0)
                self.assertTrue(math.isfinite(result["damage"]))
                self.assertTrue(math.isfinite(result["enemyDamage"]))
                self.assertGreaterEqual(result["allyHpLeft"], 0.0)
                self.assertGreaterEqual(result["enemyHpLeft"], 0.0)


class TestPersonalArmorIgnore(unittest.TestCase):
    def test_brambleback_ignore_composes_with_ally_sunder_and_stays_private(self):
        for side in ("ally", "enemy"):
            with self.subTest(side=side):
                board = [ally(ad=100, attack_speed=1,
                              fx=[{"sunderOnHit": [0.3, 4.0]}]), brambleback()]
                target = [ally(hp=10000, armor=100)]
                result = (match(board, target, duration=2.1) if side == "ally"
                          else match(target, board, duration=2.1, initiative=1))
                partner = events(result, "damage", side, source=0, name="auto")
                holder = events(result, "damage", side, source=1, name="auto")
                self.assertEqual([hit["time"] for hit in partner], [0.0, 1.0, 2.0])
                self.assertEqual([hit["time"] for hit in holder], [0.0, 1.0, 2.0])
                # The opening partner hit applies 30% Sunder. Personal
                # ignore is active only from .25 to 1.25 and leaves
                # 100 * .7 * .5 = 35 armor for Brambleback alone.
                for hit, expected in zip(partner, (50.0, 100 / 1.7, 100 / 1.7)):
                    self.assertAlmostEqual(hit["amount"], expected)
                for hit, expected in zip(holder, (100 / 1.7, 100 / 1.35, 100 / 1.7)):
                    self.assertAlmostEqual(hit["amount"], expected)

    def test_standalone_ignore_composes_with_baseline_sunder_and_expires(self):
        spec = brambleback()["spec"]
        spec.update(duration=2.1, pressure=False, enemyDebuffs={},
                    targetDebuffs={"sunder": 0.3},
                    dummies={"slots": [{"hp": 10000.0, "armor": 100.0, "mr": 0.0}]})
        _, result = ENGINE.simulate(spec, True)
        hits = solo_events(result, "damage", "auto")
        self.assertEqual([hit[0] for hit in hits], [0.0, 1.0, 2.0])
        # 50% personal ignore follows 30% Sunder; taking their maximum
        # would leave 50 armor instead of the required 35.
        for hit, expected in zip(hits, (100 / 1.7, 100 / 1.35, 100 / 1.7)):
            self.assertAlmostEqual(hit[2], expected)


class TestExecutionBypassesDefenses(unittest.TestCase):
    def test_gnar_removes_the_last_enemy_through_shields_and_durability(self):
        for durability, shield in ((0.0, 0.0), (0.2, 0.0), (0.0, 0.3), (0.2, 0.3)):
            with self.subTest(durability=durability, shield=shield):
                gnar = ally("Gnar", driver="Gnar", cast=True)
                for calc in ("HealthCalc1", "GenericCalc1", "PhysicalDamageCalc3"):
                    flat_calc(gnar, calc, 0.0)
                gnar["spec"]["kits"]["base"]["rows"].update(
                    RagePerAttack=1.0, TransformRageMax=1.0,
                    RagePerSecond=0.0, StunDuration=0.0)
                target = ally(hp=1000, fx=[{"durability": durability},
                                         {"shieldAtStart": [shield, 10.0]}])
                result = match([gnar], [target], duration=0.3)
                removal = events(result, "damage", name="thrown off")
                self.assertEqual([(hit["time"], hit["amount"]) for hit in removal],
                                 [(0.25, 1000.0)])
                self.assertEqual(result["outcome"], "win")
                self.assertEqual(result["duration"], 0.25)
                self.assertEqual(result["enemyHpLeft"], 0.0)
                self.assertFalse(result["enemies"][0]["alive"])
                self.assertEqual(len(events(result, "kill")), 1)

    def test_elder_executes_below_threshold_through_a_new_low_health_shield(self):
        for durability, shield in ((0.0, 0.0), (0.2, 0.0), (0.0, 0.3), (0.2, 0.3)):
            with self.subTest(durability=durability, shield=shield):
                elder = ally("Elder Dragon", driver="ElderDragon",
                             ad=600.0 / (1.0 - durability), attack_speed=1)
                elder["spec"]["traits"].append({"name": "test", "riftbeast": True})
                elder["spec"]["kits"]["base"]["rows"]["TraitExecuteThreshold"] = 0.5
                target = ally(hp=1000, fx=[{"durability": durability},
                                         {"shieldAtHp": [0.5, shield, 10.0, 0.0]}])
                result = match([elder], [target], duration=0.1)
                # The auto removes 600 HP and triggers the shield. The
                # remaining 400 HP must be executed despite that shield.
                auto = events(result, "damage", name="auto")
                execution = events(result, "damage", name="execute")
                self.assertEqual([hit["amount"] for hit in auto], [600.0])
                self.assertEqual([(hit["time"], hit["amount"]) for hit in execution],
                                 [(0.0, 400.0)])
                if shield:
                    self.assertEqual([hit["amount"] for hit in events(
                        result, "shield", "enemy", name="low health")], [300.0])
                self.assertEqual(result["outcome"], "win")
                self.assertEqual(result["duration"], 0.0)
                self.assertEqual(result["enemyHpLeft"], 0.0)
                self.assertFalse(result["enemies"][0]["alive"])
                self.assertEqual(len(events(result, "kill")), 1)


class TestDeferredDamageOrdering(unittest.TestCase):
    def test_shield_break_damage_lands_before_its_own_on_hit_shred(self):
        for shred in (False, True):
            with self.subTest(shred=shred):
                malphite = ally("Malphite", driver="Malphite", cast=True, lane=1,
                                fx=[{"shredOnHit": [0.3, 4.0]}] if shred else [])
                flat_calc(malphite, "ShieldCalc1", 50.0)
                flat_calc(malphite, "MagicDamageCalc1", 100.0)
                malphite["spec"]["kits"]["base"]["rows"]["ShieldDuration"] = 5.0
                # Both allies stand inside the wave: it is a stated 2 hexes
                # (AoEHexRange), so the second one has to be within two lanes
                # of Malphite's own for this to be about damage ordering.
                result = match([ally(lane=1), ally(ad=100, attack_speed=1,
                                                lane=3, mr=100)],
                               [malphite], duration=1.1)
                wave = events(result, "damage", "enemy", name="shield break")
                self.assertEqual([(hit["time"], hit["target"]) for hit in wave],
                                 [(1.0, 0), (1.0, 1)])
                # Malphite targets ally 0; ally 1 has not been hit or
                # shredded before it breaks the shield. Its reciprocal
                # wave hit is deferred because its attack is still running.
                self.assertEqual([hit["amount"] for hit in wave], [100.0, 50.0])
                received = events(result, "take", source=1)
                self.assertEqual([(hit["time"], hit["amount"]) for hit in received],
                                 [(1.0, 50.0)])
                self.assertEqual(result["allies"][1]["damageTaken"], 50.0)


class TestAppliedDamageAfterDeath(unittest.TestCase):
    def test_draven_bleed_finishes_after_death_without_new_attacks_or_casts(self):
        draven = ally("Draven", driver="Draven", hp=50, ad=0,
                      attack_speed=2, cast=True)
        flat_calc(draven, "PhysicalDamageCalc1", 100.0)
        flat_calc(draven, "GenericCalc1", 0.0)
        draven["spec"]["kits"]["base"]["rows"]["BleedDuration"] = 2.0
        result = match([draven, ally(hp=10000, frontline=False)],
                       [ally(hp=10000, ad=100, attack_speed=2)], duration=3.1)
        donor = result["allies"][0]
        self.assertFalse(donor["alive"])
        self.assertEqual(donor["aliveTime"], 0.0)
        self.assertTrue(result["allies"][1]["alive"])
        self.assertEqual((donor["attacks"], donor["casts"]), (1, 1))
        for kind in ("attack", "cast"):
            self.assertEqual([event["time"] for event in events(result, kind, source=0)],
                             [0.0])
        self.assertFalse(events(result, "land", source=0))
        bleed = events(result, "damage", source=0, name="bleed axes")
        self.assertEqual([hit["time"] for hit in bleed],
                         [0.25 * tick for tick in range(1, 9)])
        self.assertEqual([hit["amount"] for hit in bleed], [12.5] * 8)
        self.assertEqual(donor["damage"], 100.0)
        self.assertEqual(result["enemyHpLeft"], 9900.0)
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["duration"], 3.1)


class TestTeamBenchmarks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def test_team_specs_keep_actual_roles_and_items_without_free_targets(self):
        evaluator = team.Evaluator(self.snap, "clump")
        member = {"api": self.snap.unit("Teemo")["api"], "star": 2}
        effects = resolve_board_traits(self.snap, [member])["effects"][member["api"]]
        spec = evaluator.spec(member["api"], 2, effects, ["DA_Morellonomicon", "DA_VoidStaff"])
        self.assertEqual(spec["duration"], 30)
        self.assertTrue(spec["pressure"])
        self.assertFalse(spec["immortal"])
        self.assertEqual(spec["targetDebuffs"], {})
        self.assertEqual(spec["enemyDebuffs"], {})
        self.assertEqual(spec["dummies"]["slots"], [])
        self.assertEqual(spec["unit"]["objective"], self.snap.units[member["api"]]["objective"])
        self.assertTrue(spec["items"][0]["burnOnHit"])
        self.assertTrue(spec["items"][1]["shredOnHit"])

    def test_summarized_dps_and_benchmark_metrics(self):
        def result(outcome, duration, ally_hp, enemy_hp):
            return {"outcome": outcome, "duration": duration, "damage": 100,
                    "damageDps": 100 / duration, "allyHpFraction": ally_hp, "enemyHpFraction": enemy_hp,
                    "frontlineTime": min(8, duration), "allies": [{"api": "test", "name": "Test",
                        "damage": 100, "aliveTime": min(8, duration), "alive": bool(ally_hp), "healing": 10}]}
        results = [result("win", 10, .5, 0), result("loss", 20, 0, .25)]
        summary = team.summarize(results, [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
        self.assertEqual(summary["metrics"]["benchmarkWins"], 1)
        self.assertEqual(summary["metrics"]["benchmarkCount"], 2)
        self.assertEqual(summary["metrics"]["benchmarkWinRate"], .5)
        self.assertEqual(summary["metrics"]["benchmarkScore"], 50)
        self.assertEqual(summary["metrics"]["hpMargin"], .125)
        self.assertEqual(summary["metrics"]["clearTime"], 10)
        self.assertEqual(summary["metrics"]["damageDps"], 7.5)
        self.assertEqual(summary["units"]["test"]["dps"], 7.5)
        self.assertEqual(summary["units"]["test"]["aliveTime"], 8)

    def test_timeouts_are_not_wins_even_with_more_remaining_health(self):
        result = {"outcome": "timeout", "duration": 30, "damage": 100, "damageDps": 100 / 30,
                  "allyHpFraction": 1, "enemyHpFraction": .001, "frontlineTime": 30, "allies": []}
        summary = team.summarize([result], [{"key": "timeout"}])
        self.assertEqual(summary["metrics"]["benchmarkWins"], 0)
        self.assertEqual(summary["metrics"]["benchmarkTimeouts"], 1)
        self.assertEqual(summary["metrics"]["benchmarkScore"], 0)
        self.assertEqual(summary["metrics"]["clearTime"], 30)
        draw = team.summarize([dict(result, outcome="draw", allyHpFraction=0, enemyHpFraction=0)], [{"key": "draw"}])
        self.assertEqual(draw["metrics"]["benchmarkWins"], 0)
        self.assertEqual(draw["metrics"]["benchmarkDraws"], 1)
        self.assertEqual(draw["metrics"]["benchmarkScore"], 0)
        with self.assertRaisesRegex(ValueError, "exactly one label"):
            team.summarize([result], [])

    def test_immediate_clear_keeps_unit_and_team_dps_consistent(self):
        result = {"outcome": "win", "duration": 0, "damage": 150, "damageDps": 600,
                  "allyHpFraction": 1, "enemyHpFraction": 0, "frontlineTime": 0,
                  "allies": [{"api": "a", "damage": 100, "alive": True},
                             {"api": "b", "damage": 50, "alive": True, "dps": 200}]}
        summary = team.summarize([result], [{"key": "a", "label": "A"}])
        self.assertEqual(summary["units"]["a"]["dps"], 400)
        self.assertEqual(sum(unit["dps"] for unit in summary["units"].values()),
                         summary["metrics"]["damageDps"])


class TestTraitTeamResolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def test_thornmaiden_base_durability_reaches_every_member_once(self):
        members = [{"api": api, "star": 2} for api in
                   ("TFT18_Zyra", "TFT18_Leona", "TFT18_Sivir")]
        resolved = resolve_board_traits(self.snap, members)
        for member in members:
            effects = [effect for effect in resolved["effects"][member["api"]]
                       if effect["api"] == THORNMAIDEN]
            with self.subTest(unit=member["api"]):
                self.assertEqual(len(effects), 1)
                self.assertAlmostEqual(effects[0]["durability"], .05)
        self.assertTrue(any("six plants" in note for note in resolved["limitations"]))

    def test_thornmaiden_is_absent_without_zyra_and_never_assumes_six_plants(self):
        resolved = resolve_board_traits(self.snap, [{"api": "TFT18_Leona", "star": 2}])
        self.assertFalse(any(effect["api"] == THORNMAIDEN
                             for effects in resolved["effects"].values() for effect in effects))
        for star in (1, 2):
            resolved = resolve_board_traits(self.snap, [{"api": "TFT18_Zyra", "star": star},
                                                        {"api": "TFT18_Leona", "star": 2}])
            self.assertAlmostEqual(next(effect["durability"] for effect in resolved["effects"]["TFT18_Leona"]
                                        if effect["api"] == THORNMAIDEN), .05)


class TestSummonerDocumentedEffects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.live = tft.load_snapshot(18, "18.2b")
        cls.hand = tft.load_trait_effects(18)

    def test_mapping_uses_only_rows_referenced_by_active_trait_definition(self):
        mapping = self.hand["DA_18_Summoner"]["summoner"]
        required = {value["row"] for value in mapping.values() if not value.get("optional")}
        # 18.2b added AzirDamageMult; the optional entry covers both patches.
        for patch in ("18.1d", "18.2b"):
            with self.subTest(patch=patch):
                raw = json.loads((Path(tft.TFT_DATA_DIR) / f"set18/{patch}/metatft.json").read_text())
                source = next(trait for trait in raw["traits"] if trait["apiName"] == "DA_18_Summoner")
                rows = set(source["curveValues"])
                self.assertEqual({value["row"] for value in mapping.values() if value["row"] in rows}, rows)
                self.assertLessEqual(required, rows)
                self.assertIn("Improve each effect by 50%", source["effects"][1]["desc"])
        self.assertNotIn("extraSummons", mapping)
        self.assertNotIn("summonPower", mapping)

    def one_cast(self, name, column, snap=None):
        snap = snap or self.snap
        unit = snap.unit(name)
        traits = [tft.trait_spec(snap, "DA_18_Summoner", column, self.hand)] if column else []
        dummy = generic_targets(target_armor=0, target_mr=0, target_count=1)
        spec = tft.cell_spec(snap, unit, 2, "spread", [], dummy, pressure=False)
        spec["traits"] = traits
        spec.update(immortal=True, autoPressure=False,
                    enemyDebuffs=dict(dummy["enemyDebuffs"]),
                    targetDebuffs=dict(dummy["targetDebuffs"]))
        # One opening cast and no second cast within the observation window.
        # The synthetic mana bar isolates summon count and damage/shot bonuses.
        spec["kits"]["base"]["stats"].update(mana=1e6, initialMana=1e6)
        spec.update(duration=18.0, pool=[])
        _, result = tft.engine().simulate(spec, True)
        self.assertEqual(result["casts"], 1)
        return result

    def test_azir_keeps_two_soldiers_with_only_documented_damage_multipliers(self):
        damage = []
        for column in (0, 1, 2):
            result = self.one_cast("Azir", column)
            hits = [event for event in result["trace"] if event[1] == "damage" and event[4] == "soldiers"]
            self.assertEqual(len(hits), 6)
            damage.append(sum(event[2] for event in hits))
        self.assertAlmostEqual(damage[0], 6 * 2 * 69)
        self.assertAlmostEqual(damage[1] / damage[0], 1.45)
        self.assertAlmostEqual(damage[2] / damage[0], 1.675)

    def test_azir_reads_his_own_row_from_18_2b_while_mama_beak_keeps_the_shared_one(self):
        # 18.2b's text: "Azir: +AzirDamageMult Damage", "Mama Beak:
        # +DamageMult Damage". 18.1d sent both through DamageMult (above).
        curve = self.live.traits["DA_18_Summoner"]["curve"]
        self.assertEqual([tft.curve_at(curve["AzirDamageMult"], column) for column in (1, 2)], [1.2, 1.3])
        for name, source, row in (("Azir", "soldiers", "AzirDamageMult"),
                                  ("Mama Beak", "tiny beaks", "DamageMult")):
            with self.subTest(unit=name):
                damage = []
                for column in (0, 1, 2):
                    result = self.one_cast(name, column, self.live)
                    damage.append(sum(event[2] for event in result["trace"]
                                      if event[1] == "damage" and event[4] == source))
                self.assertGreater(damage[0], 0)
                for column in (1, 2):
                    self.assertAlmostEqual(damage[column] / damage[0], tft.curve_at(curve[row], column))

    def test_zyra_keeps_two_plants_with_only_documented_extra_attacks(self):
        for column, attacks_per_plant in ((0, 10), (1, 14), (2, 16)):
            with self.subTest(column=column):
                result = self.one_cast("Zyra", column)
                hits = [event for event in result["trace"] if event[1] == "damage" and event[4] == "plants"]
                self.assertEqual(len(hits), 2 * attacks_per_plant)
                self.assertAlmostEqual(sum(event[2] for event in hits), 2 * attacks_per_plant * 55)


class TestAdaptorCombatPositions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.nidalee = cls.snap.unit("Nidalee")["api"]
        cls.carry = cls.snap.unit("Sivir")["api"]
        cls.tank = cls.snap.unit("Malphite")["api"]
        cls.members = [{"api": api, "star": 2} for api in (cls.nidalee, cls.carry, cls.tank)]
        cls.effects = resolve_board_traits(cls.snap, cls.members)["effects"]

    def selection(self, form):
        return {
            self.nidalee: {"items": ("DA_SpearOfShojin", "DA_SteraksGage" if form == "AD"
                                     else "DA_RabadonsDeathcap"), "alpha": False},
            self.carry: {"items": ("DA_InfinityEdge", "DA_LastWhisper"), "alpha": False},
            self.tank: {"items": ("DA_GargoyleStoneplate", "DA_SpiritVisage"), "alpha": False},
        }

    def actors(self, evaluator, form, *, prepared=False):
        return evaluator.allies(self.members, self.effects, self.selection(form),
                                self.carry, self.tank, prepared=prepared)

    def test_native_form_resolves_role_and_distinct_positions_without_mutating_base(self):
        evaluator = team.Evaluator(self.snap, "clump")
        before = deepcopy(self.snap.units[self.nidalee])
        self.assertIn("Melee cougar Assassin", before["ability"]["desc"])
        for form, kind, front in (("AD", "Assassin", True),
                                  ("AP", "Marksman", False),
                                  ("AD", "Assassin", True)):
            with self.subTest(form=form):
                actors = self.actors(evaluator, form)
                actor = next(row for row in actors if row["spec"]["unit"]["api"] == self.nidalee)
                spec = actor["spec"]
                probe = dict(spec, dummies={"slots": [{"hp": 1, "armor": 0, "mr": 0}]})
                self.assertEqual(tft.engine().compose_fx(probe)["form"], form)
                self.assertEqual((spec["unit"]["form"], spec["unit"]["kind"], actor["frontline"]),
                                 (form, kind, front))
                self.assertEqual(len({(row["frontline"], row["lane"]) for row in actors}), len(actors))
        self.assertEqual(self.snap.units[self.nidalee], before)
        self.assertTrue(all(template["unit"]["kind"] == "Marksman"
                            for key, template in evaluator.templates.items() if key[0] == self.nidalee))

    def test_melee_form_takes_frontline_pressure_while_ranged_form_is_protected(self):
        evaluator = team.Evaluator(self.snap, "clump")
        pressure = [ally(hp=10000, ad=100, attack_speed=1, lane=1)]
        taken = {}
        for form in ("AD", "AP"):
            result = match(self.actors(evaluator, form), pressure, duration=0.1)
            taken[form] = next(unit["damageTaken"] for unit in result["allies"]
                               if unit["api"] == self.nidalee)
        self.assertGreater(taken["AD"], 0)
        self.assertEqual(taken["AP"], 0)

    def test_cougar_receives_assassin_reduction_only_from_other_attackers(self):
        evaluator = team.Evaluator(self.snap, "clump")
        for form, expected in (("AD", [100.0, 85.0]), ("AP", [100.0, 100.0])):
            with self.subTest(form=form):
                actor = deepcopy(next(row for row in self.actors(evaluator, form)
                                      if row["spec"]["unit"]["api"] == self.nidalee))
                # Isolate the resolved role from stats, shields and trait/item
                # defenses while preserving native AD/AP form selection.
                actor["spec"]["traits"] = []
                actor["spec"]["items"] = [{"api": "test", "name": "test", "unique": False,
                                             "stats": [["adPct", .2]] if form == "AD" else [["ap", 20]],
                                             "adds": []}]
                for kit in actor["spec"]["kits"].values():
                    kit.update(hpStar=10000, baseAd=0)
                    kit["stats"].update(hp=10000, ad=0, armor=0, mr=0, mana=0, initialMana=0)
                actor.update(frontline=True, lane=3)
                result = match([actor], [ally(ad=100, lane=1), ally(ad=100, lane=5)], duration=0.1)
                self.assertEqual([hit["amount"] for hit in events(result, "damage", "enemy", name="auto")],
                                 expected)


class TestNineChampionMatches(unittest.TestCase):
    def test_ninth_survivor_keeps_fighting_after_first_eight_die(self):
        for side in ("ally", "enemy"):
            with self.subTest(side=side):
                board = [ally(hp=10, priority=i) for i in range(8)]
                board.append(ally(hp=100, ad=40, attack_speed=1, priority=8))
                result = mirror(board, [ally(hp=10000, ad=10, attack_speed=1)],
                                side, duration=8.1)
                units = result["allies" if side == "ally" else "enemies"]
                incoming = events(result, "damage", "enemy" if side == "ally" else "ally", name="auto")
                self.assertEqual([(hit["time"], hit["target"]) for hit in incoming],
                                 [(float(i), i) for i in range(9)])
                self.assertFalse(any(unit["alive"] for unit in units[:8]))
                self.assertTrue(units[8]["alive"])
                self.assertEqual((units[8]["damage"], units[8]["attacks"]), (360.0, 9))
                self.assertEqual((result["outcome"], result["duration"]), ("timeout", 8.1))

    def test_ninth_death_ends_the_match_and_clears_the_frontline(self):
        for side in ("ally", "enemy"):
            with self.subTest(side=side):
                board = [ally(hp=10, priority=i) for i in range(9)]
                result = mirror(board, [ally(hp=10000, ad=10, attack_speed=1)],
                                side, duration=9.1)
                self.assertEqual(result["outcome"], "loss" if side == "ally" else "win")
                self.assertEqual(result["duration"], 8.0)
                self.assertEqual(result["frontlineTime" if side == "ally" else "enemyFrontlineTime"], 8.0)
                units = result["allies" if side == "ally" else "enemies"]
                self.assertFalse(units[8]["alive"])
                self.assertEqual(units[8]["aliveTime"], 8.0)

    def test_ninth_on_death_body_holds_after_every_champion_dies(self):
        board = [ally(hp=10, priority=i) for i in range(8)]
        yorick = deepcopy(ally("Yorick", driver="Yorick", hp=50, priority=8))
        flat_calc(yorick, "HealthCalc2", 200)
        yorick["spec"]["unit"]["extras"]["TFT18_Yorick_Spirit"].update(armor=0.0, mr=0.0)
        board.append(yorick)
        result = match(board, [ally(hp=10000, ad=100, attack_speed=1)], duration=9.1)
        self.assertFalse(any(unit["alive"] for unit in result["allies"]))
        self.assertEqual(result["allies"][8]["aliveTime"], 8.0)
        self.assertEqual(result["allyHpLeft"], 100.0)
        self.assertEqual((result["outcome"], result["duration"], result["frontlineTime"]),
                         ("timeout", 9.1, 9.1))

    def test_burn_from_ninth_source_ticks_on_ninth_recipient(self):
        allies = [ally(frontline=False) for _ in range(8)]
        allies.append(ally(ad=1, fx=[{"burnOnHit": [0.01, 1.0]}]))
        enemies = [ally(frontline=False) for _ in range(8)] + [ally()]
        result = match(allies, enemies, duration=1.1)
        burns = events(result, "damage", source=8, name="burn")
        self.assertEqual([(hit["time"], hit["target"], hit["amount"]) for hit in burns],
                         [(time, 8, 2.5) for time in (0.25, 0.5, 0.75, 1.0)])
        self.assertEqual(result["allies"][8]["damage"], 11.0)
        self.assertEqual(result["enemies"][8]["damageTaken"], 11.0)

    def test_ninth_frontliner_supplies_both_auras_until_its_death(self):
        for side in ("ally", "enemy"):
            with self.subTest(side=side):
                board = [ally(ad=100, attack_speed=1, frontline=False),
                         caster(damage=100, frontline=False)]
                board.extend(ally(frontline=False) for _ in range(6))
                board.append(ally(hp=15, fx=[{"sunderAura": 0.3, "shredAura": 0.3}]))
                target = [ally(hp=10000, ad=10, attack_speed=1, armor=100, mr=100)]
                # The aura holder takes 10 damage at 0 and dies at 1. The
                # enemy acts first, so the second auto sees no aura.
                result = (match(board, target, duration=1.1, initiative=1) if side == "ally"
                          else match(target, board, duration=1.1))
                autos = events(result, "damage", side, source=0, name="auto")
                spells = events(result, "damage", side, source=1, name="ability")
                self.assertEqual([hit["time"] for hit in autos], [0.0, 1.0])
                self.assertAlmostEqual(autos[0]["amount"], 100 / 1.7)
                self.assertAlmostEqual(autos[1]["amount"], 50.0)
                self.assertEqual(len(spells), 1)
                self.assertAlmostEqual(spells[0]["amount"], 100 / 1.7)
                self.assertEqual(result["frontlineTime" if side == "ally" else "enemyFrontlineTime"], 1.0)

    def test_ninth_actor_can_receive_or_supply_single_recipient_healing(self):
        for recipient in (0, 8):
            donor = 8 - recipient
            with self.subTest(recipient=recipient):
                board = [ally(frontline=False) for _ in range(9)]
                board[recipient] = ally()
                board[donor] = ally(ad=100, frontline=False, fx=[{"allyHealPct": 0.2}])
                result = match(board, [ally(ad=100)], duration=0.1, initiative=1)
                healing = events(result, "allyHeal", source=donor)
                self.assertEqual([(hit["target"], hit["amount"]) for hit in healing], [(recipient, 20.0)])
                self.assertEqual(result["allies"][recipient]["healing"], 20.0)
                self.assertEqual(result["allies"][donor]["allyHealing"], 20.0)

    def test_ninth_attacker_counts_toward_recipient_armor_and_magic_resist(self):
        target = ally(hp=10000, fx=[{"resistsPerAttacker": [10.0, 20.0]}])
        enemies = [ally(ad=100) for _ in range(8)]
        enemies.append(caster(damage=100, ad=100))
        result = match([target], enemies, duration=0.3)
        autos = events(result, "damage", "enemy", name="auto")
        self.assertEqual(len(autos), 9)
        for hit in autos:
            self.assertAlmostEqual(hit["amount"], 100 / 1.9)
        spell = events(result, "damage", "enemy", source=8, name="ability")
        self.assertEqual(len(spell), 1)
        self.assertAlmostEqual(spell[0]["amount"], 100 / 2.8)

    def test_area_selection_and_flat_resistance_changes_include_ninth_target(self):
        gnar = ally("Gnar", driver="Gnar", attack_speed=1)
        for calc, value in (("HealthCalc1", 0), ("GenericCalc1", 20), ("PhysicalDamageCalc3", 0)):
            flat_calc(gnar, calc, value)
        gnar["spec"]["kits"]["base"]["rows"].update(
            RagePerAttack=1.0, TransformRageMax=1.0, RagePerSecond=0.0, StunDuration=0.0)
        # Gnar's transform selects all nine and publishes its flat cuts.
        # A different source attacking later must see target 8's shared cut.
        attacker = ally(ad=100, attack_speed=1, lane=6)
        # Gnar's transform is a stated 2 hexes (TransformHexRadius), so the
        # ninth enemy has to stand inside it for this to be about the shared
        # cut rather than about the radius.
        enemies = [ally(hp=10000, armor=100, mr=100, lane=0) for _ in range(8)]
        enemies.append(ally(hp=10000, armor=100, mr=100, lane=2))
        result = match([gnar, attacker], enemies, duration=0.1)
        hits = events(result, "damage", source=1, name="auto")
        self.assertEqual([hit["target"] for hit in hits], [8])
        self.assertAlmostEqual(hits[0]["amount"], 100 / 1.8)

    def test_nine_vs_nine_initiative_and_all_native_api_shapes(self):
        board = [ally(hp=100, ad=100, attack_speed=1, priority=i) for i in range(9)]
        raw = [{"allies": board, "enemies": deepcopy(board), "duration": 1.1,
                "initiative": initiative, "geometry": geometry}
               for geometry in ("spread", "clump") for initiative in (0, 1)]
        expected = [ENGINE.simulate_match(spec, True) for spec in raw]
        self.assertEqual([result["outcome"] for result in expected], ["win", "loss"] * 2)
        for result in expected:
            winner = "ally" if result["initiative"] == 0 else "enemy"
            self.assertEqual(result["duration"], 0.0)
            self.assertEqual([hit["source"] for hit in events(result, "damage", winner, name="auto")],
                             list(range(9)))
        native = [prepared(spec) for spec in raw]
        for specs in (raw, native):
            for workers in (1, 4):
                self.assertEqual(bits(expected), bits(ENGINE.simulate_matches(specs, trace=True, workers=workers)))
                compact = ENGINE.simulate_matches(specs, detail="compact", workers=workers)
                for complete, small in zip(expected, compact):
                    self.assertEqual(bits({key: complete[key] for key in small}), bits(small))
        # Reusing the exact same prepared handles must not retain ninth-unit
        # deaths or targeting masks from the preceding worlds.
        self.assertEqual(bits(expected[::-1]), bits(ENGINE.simulate_matches(native[::-1], trace=True)))

    def test_ten_actors_are_rejected_on_both_sides_for_raw_and_prepared(self):
        spec = {"allies": [ally()], "enemies": [ally()], "duration": 0.1}
        for source in (spec, prepared(spec)):
            for side in ("allies", "enemies"):
                invalid = {**source, side: source[side] * 10}
                with self.subTest(side=side, prepared=source is not spec):
                    with self.assertRaisesRegex(ValueError, "one to nine"):
                        ENGINE.simulate_match(invalid)
                    with self.assertRaisesRegex(ValueError, "one to nine"):
                        ENGINE.simulate_matches([source, invalid])
        # Expanding champion encounters leaves the standalone dummy contract
        # unchanged; its nine-target input still fails at the existing guard.
        solo = deepcopy(spec["allies"][0]["spec"])
        solo["dummies"] = {"slots": [{"hp": 1000, "armor": 0, "mr": 0}] * 9}
        with self.assertRaises(ValueError):
            ENGINE.simulate(solo)


class TestPreparedMatches(unittest.TestCase):
    def assertExact(self, expected, actual):
        self.assertEqual(bits(expected), bits(actual))

    def test_raw_prepared_and_mixed_entries_have_identical_full_traces(self):
        spec = {"allies": [caster(), ally(hp=150, ad=50, lane=5)],
                "enemies": [caster("Leona"), ally(ad=75, attack_speed=2, lane=5)],
                "duration": 5.0, "geometry": "spread", "initiative": 1}
        expected = ENGINE.simulate_match(spec, True)
        native = prepared(spec)
        self.assertExact(expected, ENGINE.simulate_match(native, True))
        native["enemies"][0] = spec["enemies"][0]
        self.assertExact(expected, ENGINE.simulate_matches([native], trace=True)[0])
        self.assertExact(expected, ENGINE.simulate_matches([spec], trace=True)[0])

    def test_preparation_is_an_immutable_owned_snapshot(self):
        member = caster(damage=91)
        member["spec"]["dummies"] = {"slots": []}
        before = deepcopy(member)
        handle = ENGINE.prepare_actor(member)
        self.assertEqual(before, member)
        fixture = {"allies": [handle], "enemies": [ally(hp=1000)], "duration": 1.0}
        expected = ENGINE.simulate_match(fixture, True)
        member["spec"]["kits"]["base"]["baseAd"] = 99999.0
        member["spec"]["kits"]["base"]["calcs"].clear()
        member["lane"] = 6
        del member
        gc.collect()
        self.assertExact(expected, ENGINE.simulate_match(fixture, True))
        with self.assertRaises(AttributeError):
            handle.lane = 0
        with self.assertRaises(ValueError):
            ENGINE.simulate(before["spec"], False)

    def test_shared_handle_gets_fresh_driver_effect_and_clock_state(self):
        scuttle = {"spec": spec_for("Scuttlecrab", items=("Protector's Vow", "Warmog's Armor")),
                   "frontline": True, "lane": 1}
        sentinel = {"spec": spec_for("Sentinel", items=("Guinsoo's Rageblade",)),
                    "frontline": False, "lane": 5}
        enemy_actor = {"spec": spec_for("Karma", items=("Hextech Gunblade", "Red Buff")),
                       "frontline": True, "lane": 3}
        spec = {"allies": [scuttle, sentinel], "enemies": [enemy_actor, enemy_actor], "duration": 9.0}
        expected = ENGINE.simulate_match(spec, True)
        native = prepared(spec)
        # Deliberately share the same seed both within a world and across
        # worlds; casts mutate driver state and Sentinel mutates its Fx.
        native["enemies"][1] = native["enemies"][0]
        matches = [native] * 12
        for workers in (1, 4):
            for actual in ENGINE.simulate_matches(matches, trace=True, workers=workers):
                self.assertExact(expected, actual)

    def test_target_count_geometry_and_duration_belong_to_each_match(self):
        actor = caster(damage=73, attack_speed=1.3)
        actor["spec"]["dummies"] = {"slots": []}
        actor["spec"]["duration"] = 0.1
        enemy_actor = caster("Leona", damage=19)
        actor_handle, enemy_handle = ENGINE.prepare_actor(actor), ENGINE.prepare_actor(enemy_actor)
        raw, native = [], []
        for count in range(1, 10):
            for geometry in ("spread", "clump"):
                for duration in (0.125, 2.125):
                    spec = {"allies": [actor] * count, "enemies": [enemy_actor] * (10 - count),
                            "geometry": geometry, "duration": duration, "initiative": count % 2}
                    raw.append(spec)
                    native.append({**spec, "allies": [actor_handle] * count,
                                   "enemies": [enemy_handle] * (10 - count)})
        expected = [ENGINE.simulate_match(spec, True) for spec in raw]
        self.assertExact(expected, ENGINE.simulate_matches(native, trace=True, workers=4))
        # Run in reverse as well so a preceding larger/longer world cannot
        # leave an extra target, event or stack in a prepared input.
        self.assertExact(expected[::-1], ENGINE.simulate_matches(native[::-1], trace=True))

    def test_compact_values_are_exact_full_values_and_keep_input_order(self):
        holder = ally(hp=60, ad=10, lane=1, fx=[
            {"burnOnHit": [0.01, 1.0], "allyHealPct": 0.2}])
        survivor = ally(hp=10000, ad=90, lane=5, frontline=False)
        matches = []
        for initiative in (0, 1):
            for proc_heal in (True, False):
                for post_death in (True, False):
                    matches.append(prepared({"allies": [holder, survivor],
                        "enemies": [ally(ad=100, attack_speed=1, lane=1), ally(ad=45, lane=5)],
                        "duration": 3.1, "initiative": initiative,
                        "damageHealingFromProcs": proc_heal, "postDeathAllyHealing": post_death}))
        full = ENGINE.simulate_matches(matches)
        compact = ENGINE.simulate_matches(matches, detail="compact", workers=4)
        self.assertEqual(len(full), len(compact))
        for complete, small in zip(full, compact):
            self.assertNotIn("allies", small)
            self.assertNotIn("modelAssumptions", small)
            self.assertNotIn("trace", small)
            self.assertExact({key: complete[key] for key in small}, small)
        self.assertNotEqual(full[0]["damage"], full[-1]["damage"])

    def test_python_threads_can_share_prepared_inputs_without_state_leaks(self):
        spec = prepared({"allies": [caster(attack_speed=1.7), ally(ad=35)],
                         "enemies": [caster("Leona"), ally(ad=70, attack_speed=1.1)],
                         "duration": 5.0})
        expected = ENGINE.simulate_match(spec, True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: ENGINE.simulate_match(spec, True), range(16)))
        for result in results:
            self.assertExact(expected, result)

    def test_batch_validation_and_empty_input(self):
        self.assertEqual(ENGINE.simulate_matches([]), [])
        spec = prepared({"allies": [ally(ad=100)], "enemies": [ally()], "duration": 0.1})
        for options in ({"detail": "unknown"}, {"detail": "compact", "trace": True},
                        {"workers": 0}, {"workers": 65}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ENGINE.simulate_matches([spec], **options)
        for change in ({"allies": []}, {"enemies": spec["enemies"] * 10},
                       {"geometry": "bad"}, {"duration": 0}, {"initiative": 2}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ENGINE.simulate_matches([spec, {**spec, **change}])
        result = ENGINE.simulate_matches([spec])[0]
        self.assertEqual(result["damage"], 100.0)


if __name__ == "__main__":
    unittest.main()
