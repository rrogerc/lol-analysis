"""Small shared encounters with hand-computable health and action timelines.

These exercise the compiled scheduler directly, independently of composition
search heuristics and opponent calibration. Synthetic stats isolate mechanics;
real drivers are used where their ability behavior is the subject of the test.
"""

import unittest

from test_tft import ENGINE, spec_for


def ally(name="Ashe", *, driver="Driver", hp=1000.0, ad=0.0,
         attack_speed=0.01, armor=0.0, mr=0.0, frontline=True, lane=3,
         priority=0, cast=False, fx=()):
    spec = spec_for(name, star=1, driver=driver, pressure=True, fx=fx)
    kit = spec["kits"]["base"]
    kit["hpStar"] = hp
    kit["baseAd"] = ad
    kit["stats"].update(hp=hp, ad=ad, armor=armor, mr=mr,
                        mana=10000.0 if cast else 0.0,
                        initialMana=10000.0 if cast else 0.0,
                        critChance=0.0, critMult=1.0)
    kit["stats"]["as"] = attack_speed
    spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
    spec["unit"]["castTime"] = 0.25
    return {"spec": spec, "frontline": frontline, "lane": lane, "priority": priority}


def enemy(**changes):
    return {"name": "Synthetic enemy", "hp": 10000.0, "armor": 0.0, "mr": 0.0,
            "kind": "tank", "frontline": True, "lane": 3, "streams": 1,
            "ad": 0.0, "as": 0.0, "ability": 0.0, "physicalShare": 1.0,
            "manaMax": 0.0, **changes}


def flat_calc(member, name, amount):
    member["spec"]["kits"]["base"]["calcs"][name]["terms"] = [
        {"type": "flat", "value": float(amount), "op": "add"}]


def fight(allies, enemies, duration=3.0, geometry="clump"):
    return ENGINE.simulate_team({"allies": allies, "enemies": enemies,
                                 "duration": duration, "geometry": geometry,
                                 "critEv": 1.0, "burnWound": 0.33}, True)


def trace(result, kind=None, *, source=None, target=None, name=None):
    return [event for event in result["trace"]
            if (kind is None or event["kind"] == kind)
            and (source is None or event["source"] == source)
            and (target is None or event["target"] == target)
            and (name is None or event.get("sourceName") == name)]


class TestSharedHealthAndPressure(unittest.TestCase):
    def test_champions_deplete_one_shared_enemy_health_pool(self):
        result = fight([ally(ad=100), ally(ad=100)], [enemy(hp=150)])
        self.assertEqual(result["outcome"], "win")
        self.assertEqual(result["enemyHpLeft"], 0.0)
        self.assertEqual(result["damage"], 150.0)
        self.assertEqual([unit["damage"] for unit in result["allies"]], [100.0, 50.0])
        self.assertEqual(result["enemies"][0]["attacks"], 0)

    def test_dead_enemy_stops_its_future_attacks_and_spells(self):
        result = fight([ally(hp=10000, ad=100, attack_speed=1)], [
            enemy(hp=150, ad=10, **{"as": 1}, attackStart=0.25,
                  ability=20, physicalShare=0, castInterval=1, castStart=0.5),
            enemy(frontline=False, lane=5),
        ], duration=3.1)
        # The first source attacks/casts once, then dies at 1s. The second
        # enemy keeps the encounter running beyond its next scheduled events.
        first = result["enemies"][0]
        self.assertFalse(first["alive"])
        self.assertEqual((first["attacks"], first["casts"]), (1, 1))
        self.assertEqual(result["allies"][0]["damageTaken"], 30.0)
        self.assertEqual([e["time"] for e in trace(result, "enemyAttack", source=0)], [0.25])
        self.assertEqual([e["time"] for e in trace(result, "enemySpell", source=0)], [0.5])

    def test_two_tanks_share_then_survivor_inherits_pressure(self):
        result = fight([ally(hp=100, lane=1), ally(hp=500, lane=5)], [
            enemy(lane=1, ad=60, **{"as": 1}, attackStart=0.5),
            enemy(lane=5, ad=60, **{"as": 1}, attackStart=0.5),
        ], duration=2.6)
        hits = trace(result, "enemyAttack")
        self.assertEqual([(e["time"], e["source"], e["target"]) for e in hits], [
            (0.5, 0, 0), (0.5, 1, 1), (1.5, 0, 0), (1.5, 1, 1),
            (2.5, 0, 1), (2.5, 1, 1),
        ])
        self.assertFalse(result["allies"][0]["alive"])
        self.assertTrue(result["allies"][1]["alive"])
        self.assertEqual(result["allies"][1]["damageTaken"], 240.0)

    def test_carry_becomes_exposed_after_frontline_death(self):
        result = fight([ally(hp=50), ally(hp=200, frontline=False)], [
            enemy(ad=60, **{"as": 1}, attackStart=0.5),
        ], duration=1.6)
        self.assertEqual([(e["time"], e["target"]) for e in trace(result, "enemyAttack")],
                         [(0.5, 0), (1.5, 1)])
        self.assertEqual(result["frontlineTime"], 0.5)
        self.assertEqual(result["allies"][1]["damageTaken"], 60.0)

    def test_defensive_frontline_can_enable_more_total_team_damage(self):
        incoming = [enemy(ad=60, **{"as": 1}, attackStart=0.5)]
        carry = ally(hp=100, ad=100, attack_speed=1, frontline=False)
        offense = fight([ally(hp=100, ad=50, attack_speed=1), carry], incoming, duration=10)
        defense = fight([ally(hp=100, armor=100), carry], incoming, duration=10)
        # Offensive frontliner dies at1.5s; armored one at3.5s. The carry
        # gets four versus six attacks before it is exposed and then dies.
        self.assertEqual(offense["allies"][0]["aliveTime"], 1.5)
        self.assertEqual(defense["allies"][0]["aliveTime"], 3.5)
        self.assertEqual(offense["allies"][1]["damage"], 400.0)
        self.assertEqual(defense["allies"][1]["damage"], 600.0)
        self.assertEqual(offense["damage"], 500.0)
        self.assertEqual(defense["damage"], 600.0)
        self.assertGreater(defense["damage"], offense["damage"])

    def test_scuttle_cannot_attack_during_shared_fight_burrow(self):
        scuttle = ally("Scuttlecrab", driver="Scuttlecrab", ad=50,
                       attack_speed=2, cast=True)
        result = fight([scuttle], [enemy()], duration=3.26)
        self.assertEqual([e["time"] for e in trace(result, "land", source=0)], [0.25])
        self.assertEqual([e["time"] for e in trace(result, "attack", source=0)], [0.0, 3.25])
        self.assertEqual([e["time"] for e in trace(result, "damage", source=0, name="dance")],
                         [0.0, 3.25])

    def test_one_mixed_spell_keeps_its_target_when_first_portion_kills(self):
        result = fight([ally(hp=40), ally(hp=200, frontline=False)], [
            enemy(ability=100, physicalShare=0.5, castStart=0.5, castInterval=10),
        ], duration=0.6)
        self.assertFalse(result["allies"][0]["alive"])
        self.assertEqual(result["allies"][1]["damageTaken"], 0.0)
        self.assertEqual(result["allyHpLeft"], 200.0)
        self.assertEqual(result["enemies"][0]["casts"], 1)
        self.assertFalse(trace(result, "enemySpell", target=1))

    def test_a_dead_champion_cannot_finish_cast_while_summon_holds(self):
        for name in ("Krug", "Yorick"):
            with self.subTest(champion=name):
                champion = ally(name, driver=name, hp=40, cast=True)
                result = fight([champion], [
                    enemy(ad=100, **{"as": 0.01}, attackStart=0.1),
                ], duration=1)
                self.assertFalse(result["allies"][0]["alive"])
                self.assertEqual(result["outcome"], "timeout")  # summon remains
                self.assertEqual(result["allies"][0]["casts"], 1)
                self.assertFalse(trace(result, "land", source=0))
                self.assertFalse([e for e in trace(result, "damage", source=0)
                                  if e["time"] > 0.1])


class TestSharedHealingAndShields(unittest.TestCase):
    def test_gunblade_heals_one_recipient_and_discards_overheal(self):
        donor = ally(ad=100, attack_speed=1, frontline=False, fx=[{"allyHealPct": 0.2}])
        result = fight([ally(hp=100, lane=1), ally(hp=100, lane=5), donor], [
            enemy(lane=1, ad=5, **{"as": 0.01}, attackStart=0.1),
            enemy(lane=5, ad=6, **{"as": 0.01}, attackStart=0.1),
        ], duration=1.1)
        heals = trace(result, "allyHeal", source=2)
        self.assertEqual([(e["target"], e["amount"]) for e in heals], [(1, 6.0)])
        self.assertEqual(result["allies"][2]["allyHealing"], 6.0)

    def ivern(self, duration=1.0):
        member = ally("Ivern", driver="Ivern", frontline=False, cast=True)
        member["spec"]["kits"]["base"]["rows"].update(
            NumAlliesToShield=2.0, ShieldAmount=100.0, ShieldDuration=duration)
        return member

    def test_ivern_shields_distinct_allies(self):
        result = fight([ally(lane=1), ally(lane=5), self.ivern()], [
            enemy(lane=lane, ad=50, **{"as": 0.01}, attackStart=0.1,
                  ability=60, physicalShare=0, castStart=0.5, castInterval=10)
            for lane in (1, 5)
        ], duration=0.6)
        shields = trace(result, "allyShield", source=2)
        self.assertEqual(sorted((e["target"], e["amount"]) for e in shields),
                         [(0, 100.0), (1, 100.0)])
        self.assertEqual(result["allies"][2]["allyShielding"], 200.0)
        self.assertEqual([u["shielding"] for u in result["allies"][:2]], [60.0, 60.0])
        self.assertEqual(result["allyHpLeft"], 2900.0)

    def test_ally_shield_expires_at_its_ability_duration(self):
        result = fight([ally(lane=1), ally(lane=5), self.ivern(duration=0.5)], [
            enemy(lane=lane, ability=100, physicalShare=0, castStart=1, castInterval=10)
            for lane in (1, 5)
        ], duration=1.1)
        self.assertEqual([u["shielding"] for u in result["allies"][:2]], [0.0, 0.0])
        self.assertEqual(result["allyHpLeft"], 2800.0)

    def test_alistar_preserves_per_recipient_healing_amount(self):
        alistar = ally("Alistar", driver="Alistar", frontline=False, cast=True)
        flat_calc(alistar, "HealthCalc1", 0)
        flat_calc(alistar, "HealthCalc2", 100)
        flat_calc(alistar, "MagicDamageCalc1", 0)
        result = fight([ally(lane=1), ally(lane=5), alistar], [
            enemy(lane=lane, ad=400, **{"as": 0.01}, attackStart=0.1)
            for lane in (1, 5)
        ], duration=0.3)
        heals = trace(result, "allyHeal", source=2)
        self.assertEqual(sorted((e["target"], e["amount"]) for e in heals),
                         [(0, 100.0), (1, 100.0)])
        self.assertEqual(result["allies"][2]["allyHealing"], 200.0)
        self.assertEqual(result["allyHpLeft"], 2400.0)

    def test_shared_healing_applies_wound_before_missing_health_cap(self):
        healer = ally(ad=200, attack_speed=1, frontline=False, fx=[{"allyHealPct": 1.0}])
        result = fight([ally(), healer], [
            enemy(ad=100, **{"as": 0.01}, attackStart=0.1, wound=0.33, debuffDuration=2),
        ], duration=1.1)
        # At1s the 200 raw heal becomes134 and repairs all100 missing HP;
        # capping raw healing at100 first would incorrectly restore only67.
        heals = trace(result, "allyHeal", source=1, target=0)
        self.assertEqual([(e["time"], e["amount"]) for e in heals], [(1.0, 100.0)])
        self.assertEqual(result["allies"][1]["allyHealing"], 100.0)
        self.assertEqual(result["allyHpLeft"], 2000.0)


class TestSharedUtility(unittest.TestCase):
    def test_weaker_timed_reduction_does_not_extend_stronger_one(self):
        for key, source_name in (("sunderOnHit", "auto"), ("shredOnHit", "ascension")):
            with self.subTest(effect=key):
                strong = ally(ad=1, fx=[{key: [0.4, 1.0]}])
                weak = ally(ad=1, fx=[{key: [0.3, 5.0]}])
                if key == "sunderOnHit":
                    probe = ally(ad=100, attack_speed=1)
                else:
                    # 1-star Kayle adds magic damage to each attack but
                    # does not yet apply her own native Shred.
                    probe = ally("Kayle", driver="Kayle", attack_speed=1)
                    flat_calc(probe, "MagicDamageCalc1", 100)
                result = fight([strong, weak, probe], [enemy(armor=100, mr=100)], duration=5.1)
                damage = {e["time"]: e["amount"]
                          for e in trace(result, "damage", source=2, name=source_name)}
                self.assertAlmostEqual(damage[0.0], 100.0 / 1.6)
                self.assertAlmostEqual(damage[1.0], 100.0 / 1.7)
                self.assertAlmostEqual(damage[5.0], 50.0)

    def test_ionic_spark_fires_once_per_enemy_cast(self):
        holder = ally("Leona", hp=1000, fx=[{"ionicSpark": 1.6}])
        result = fight([holder], [
            enemy(manaMax=100, ability=20, physicalShare=0.5,
                  castInterval=10, castStart=0.5),
        ], duration=0.6)
        zaps = trace(result, "damage", source=0, name="ionic spark")
        self.assertEqual([(e["time"], e["amount"]) for e in zaps], [(0.5, 160.0)])
        self.assertEqual(result["damage"], 160.0)
        self.assertEqual(result["enemies"][0]["casts"], 1)
        self.assertEqual(result["allies"][0]["damageTaken"], 20.0)

    def test_ordinary_burn_and_wound_do_not_multiply_by_provider_count(self):
        burn = {"burnOnHit": [0.01, 1.0]}
        result = fight([ally(ad=100, fx=[burn]), ally(ad=100, fx=[burn])], [
            enemy(hp=1000, healPerSecond=100),
        ], duration=1.3)
        self.assertAlmostEqual(sum(e["amount"] for e in trace(result, "damage", name="burn")), 10.0)
        heals = {e["time"]: e["amount"] for e in trace(result, "enemyHeal")}
        self.assertAlmostEqual(heals[0.25], 25.0 * 0.67)
        self.assertAlmostEqual(heals[0.5], 25.0 * 0.67)
        self.assertAlmostEqual(heals[1.25], 25.0)

    def test_inferno_refreshes_one_channel_and_adds_to_ordinary_burn(self):
        for providers in (1, 2):
            for ordinary in (False, True):
                with self.subTest(providers=providers, ordinary=ordinary):
                    members = []
                    for index in range(providers):
                        fx = [{"burnOnHit": [0.01, 1.0]}] if ordinary and index == 0 else []
                        member = ally(ad=100, attack_speed=2, fx=fx)
                        # Trait burns use the separate Inferno channel;
                        # repeated hits refresh it without adding stacks.
                        member["spec"]["traits"] = [{
                            "api": "DA_18_Inferno", "name": "Inferno", "stats": [],
                            "burnOnHit": [0.01, 1.0],
                        }]
                        members.append(member)
                    result = fight(members, [enemy(hp=1000, healPerSecond=100)], duration=1.01)
                    self.assertTrue(all(unit["attacks"] == 3 for unit in result["allies"]))
                    # One second at 1% HP/s, or 2% with an ordinary burn,
                    # regardless of the number of Inferno hits/providers.
                    burned = sum(e["amount"] for e in trace(result, "damage", name="burn"))
                    self.assertAlmostEqual(burned, 20.0 if ordinary else 10.0)
                    heals = trace(result, "enemyHeal")
                    self.assertEqual(len(heals), 4)
                    for heal in heals:
                        self.assertAlmostEqual(heal["amount"], 25.0 * 0.67)

    def test_burn_pays_only_elapsed_parts_of_first_and_last_tick(self):
        cinderling = ally("Cinderling", driver="Cinderling", cast=True)
        cinderling["spec"]["unit"]["castTime"] = 0.125
        cinderling["spec"]["kits"]["base"]["rows"].update(BurnAmount=1.0, BurnDuration=0.5)
        flat_calc(cinderling, "PhysicalDamageCalc1", 0)
        result = fight([cinderling], [enemy(hp=1000)], duration=0.8)
        self.assertEqual([e["time"] for e in trace(result, "land")], [0.125])
        burns = trace(result, "damage", name="burn")
        self.assertEqual([e["time"] for e in burns], [0.25, 0.5, 0.75])
        # Burn starts halfway through the first tick and expires halfway
        # through the last: 0.125 + 0.25 + 0.125 seconds, at 10 damage/second.
        for event, amount in zip(burns, (1.25, 2.5, 1.25)):
            self.assertAlmostEqual(event["amount"], amount)
        self.assertAlmostEqual(result["damage"], 5.0)


if __name__ == "__main__":
    unittest.main()
