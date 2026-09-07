"""Champion-versus-champion regressions, independent of opponent selection."""
from copy import deepcopy
import math
import unittest

from test_tft import ENGINE, SNAP, spec_for
from test_tft_team_engine import ally, flat_calc


def match(allies, enemies, duration=3.0, initiative=0, **options):
    return ENGINE.simulate_match({"allies": allies, "enemies": enemies,
                                  "duration": duration, "geometry": "clump",
                                  "initiative": initiative, **options}, True)


def events(result, kind=None, side="ally", source=None, name=None):
    return [event for event in result["trace"]
            if (kind is None or event["kind"] == kind)
            and event["side"] == side
            and (source is None or event["source"] == source)
            and (name is None or event.get("sourceName") == name)]


def caster(name="Ahri", damage=100, **changes):
    member = ally(name, driver=name, cast=True, **changes)
    flat_calc(member, "MagicDamageCalc1", damage)
    if name == "Ahri":
        member["spec"]["kits"]["base"]["rows"].update(ChannelTime=0.25, HexPercentDamageFalloffTooltip=0.0)
    if name == "Leona":
        flat_calc(member, "GenericCalc1", 0)
        member["spec"]["kits"]["base"]["rows"]["StunDuration"] = 0.0
    return member


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
        # 100 / 2 armor * 0.8 durability = 40 damage,30 on shield and10 HP.
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
        self.assertEqual(result["damage"], 600.0)


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
        self.assertAlmostEqual(hits[1]["amount"], 100.0/1.7)
        self.assertAlmostEqual(result["enemyHpLeft"], 1000.0-50.0-100.0/1.7)

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

    def test_proc_healing_assumption_is_explicit_and_switchable(self):
        holder = ally(ad=1, fx=[{"burnOnHit": [0.01, 4.0], "allyHealPct": 0.2}])
        target = ally(hp=1000, ad=100, lane=5)
        board = [holder, ally(lane=5)]
        yes = match(board, [target], duration=0.3, damageHealingFromProcs=True)
        no = match(board, [target], duration=0.3, damageHealingFromProcs=False)
        self.assertTrue(yes["damageHealingFromProcs"])
        self.assertFalse(no["damageHealingFromProcs"])
        self.assertGreater(yes["allies"][0]["allyHealing"], 0.0)
        self.assertEqual(no["allies"][0]["allyHealing"], 0.0)

    def test_postdeath_ally_healing_assumption_is_explicit(self):
        holder = ally(hp=50, ad=1, lane=1, fx=[{"burnOnHit": [0.01, 4.0], "allyHealPct": 0.2}])
        survivor = ally(lane=5)
        attackers = [ally(ad=100, lane=1), ally(ad=100, lane=5)]
        yes = match([holder, survivor], attackers, duration=0.3, postDeathAllyHealing=True)
        no = match([holder, survivor], attackers, duration=0.3, postDeathAllyHealing=False)
        self.assertFalse(yes["allies"][0]["alive"])
        self.assertGreater(yes["allies"][0]["allyHealing"], 0.0)
        self.assertEqual(no["allies"][0]["allyHealing"], 0.0)


class TestSymmetricCoverage(unittest.TestCase):
    def test_multiple_real_drivers_share_health_and_conserve_damage_credit(self):
        import tft
        units = tft.modeled_units(SNAP)
        for start in range(0, len(units), 8):
            with self.subTest(group=start):
                members = []
                for index, unit in enumerate(units[start:start+8]):
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
            import tft
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


if __name__ == "__main__":
    unittest.main()
