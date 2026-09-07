"""Hand-computed regressions for real-target damage and effect ordering."""

import unittest

from test_tft import ENGINE, events as solo_events
from test_tft_symmetric import events, match
from test_tft_team_engine import ally, flat_calc


def brambleback():
    member = ally("Brambleback", driver="Brambleback", ad=100,
                  attack_speed=1, cast=True)
    flat_calc(member, "GenericCalc1", 0.5)
    member["spec"]["kits"]["base"]["rows"].update(
        Duration=1.0, FrenzyADPercent=0.0)
    return member


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
                result = match([ally(lane=1), ally(ad=100, attack_speed=1,
                                                lane=5, mr=100)],
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


if __name__ == "__main__":
    unittest.main()
