"""Primal blessings use their actual timing/threshold, with no uptime bonuses.

Source: saved18.1d metatft.json extras.payloads DA_Primal18_*Blessing.
The board resolver supplies effects only to their declared recipients.
"""

from copy import deepcopy
import math
import unittest

from test_tft import ENGINE, events, spec_for
from test_tft_unit_profiles import plain_spec
from test_tft_team_engine import ally, enemy, fight, trace
from test_tft_symmetric import match as champion_match, events as match_events
from test_tft_theory_pressure import measure
from tft_unit_profiles import generic_targets


def blessing(*names, primal=True):
    effect = {"api": "DA_Primal18", "name": "Primal", "stats": [],
              "selectedBlessings": list(names)}
    if "tiger" in names:
        effect["timedStats"] = [{"after": 6, "interval": 0,
                                 "stats": [["asPct", 0.35 if primal else 0.15]]}]
    if "turtle" in names:
        effect["healPerInterval"] = [0.04, 4]
    if "bear" in names and primal:
        effect["executeBelowHp"] = 0.12
    return effect


def finite_attacker(damage, *, armor=0, duration=0.1, effects=(), fx=()):
    spec = plain_spec(damage=damage, target_armor=armor, fx=fx)
    spec.update(immortal=False, duration=duration)
    spec["dummies"]["slots"][0]["hp"] = 100
    spec["traits"] = list(effects)
    return spec


class TestPrimalBear(unittest.TestCase):
    def test_executes_only_after_positive_damage_leaves_health_strictly_below_threshold(self):
        for damage, expected, execute in ((87, 87, []), (88, 88, []), (89, 100, [11])):
            with self.subTest(damage=damage):
                _, result = ENGINE.simulate(finite_attacker(damage, effects=[blessing("bear")]), True)
                self.assertEqual(result["total"], expected)
                self.assertEqual([hit[2] for hit in events(result, "damage", "primal bear")], execute)

    def test_threshold_uses_remaining_health_after_mitigation(self):
        for damage, expected in ((176, 88), (178, 100)):
            with self.subTest(damage=damage):
                _, result = ENGINE.simulate(finite_attacker(damage, armor=100,
                                                           effects=[blessing("bear")]), True)
                self.assertEqual(result["total"], expected)

    def test_owned_item_burn_can_cross_the_execute_threshold(self):
        _, result = ENGINE.simulate(finite_attacker(0, duration=1.1,
            effects=[blessing("bear")], fx=[{"burnOnHit": [0.9, 2]}]), True)
        self.assertEqual(result["total"], 100)
        self.assertEqual([(hit[0], hit[2]) for hit in events(result, "damage", "primal bear")], [(1, 10)])

    def test_nonprimal_ally_cannot_trigger_a_primal_members_execute_without_their_damage(self):
        for primal_damage, expected in ((0, []), (1, [10])):
            with self.subTest(primal_damage=primal_damage):
                member = ally("Vi", ad=primal_damage)
                member["spec"]["traits"] = [blessing("bear")]
                result = fight([ally("Ashe", ad=89), member], [enemy(hp=100)], duration=0.1)
                self.assertEqual([hit["amount"] for hit in trace(result, "damage", name="primal bear")], expected)
                self.assertTrue(all(hit["source"] == 1 for hit in trace(result, "damage", name="primal bear")))

    def test_execute_bypasses_new_shield_without_crediting_unused_shield_health(self):
        member = ally("Vi", ad=89)
        member["spec"]["traits"] = [blessing("bear")]
        target = ally("Leona", hp=100, fx=[{"shieldAtHp": [0.15, 1, 10, False]}])
        result = champion_match([member], [target], duration=0.1)
        self.assertEqual(result["damage"], 100)
        self.assertFalse(result["enemies"][0]["alive"])
        self.assertEqual([hit["amount"] for hit in match_events(result, "damage", name="primal bear")], [11])

    def test_immortal_response_probes_do_not_generate_execute_damage_or_sustain(self):
        spec = plain_spec(damage=89, incoming_dps=10,
                          fx=[{"stats": [["omnivamp", 0.5]]}])
        before = ENGINE.measure_response(spec, [1, 2, 5])
        spec["traits"] = [blessing("bear")]
        self.assertEqual(ENGINE.measure_response(spec, [1, 2, 5]), before)


class TestPrimalTurtle(unittest.TestCase):
    def test_first_heal_is_at_four_seconds_and_repeats_at_eight(self):
        spec = plain_spec(damage=0, incoming_dps=100)
        spec["traits"] = [blessing("turtle")]
        spec["duration"] = 8.1
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([(hit[0], hit[2]) for hit in events(result, "heal", "primal turtle")], [(4, 40), (8, 40)])

    def test_heals_apply_wound_before_capping_at_missing_health(self):
        for dps, expected in ((1, 3), (100, 26.8)):
            with self.subTest(dps=dps):
                spec = plain_spec(damage=0, incoming_dps=dps)
                spec["traits"] = [blessing("turtle")]
                spec["enemyDebuffs"]["wound"] = 0.33
                spec["duration"] = 4.1
                _, result = ENGINE.simulate(spec, True)
                heals = events(result, "heal", "primal turtle")
                self.assertEqual(len(heals), 1)
                self.assertAlmostEqual(heals[0][2], expected)

    def test_max_health_growth_changes_the_heal_at_its_actual_tick(self):
        spec = plain_spec(damage=0, incoming_dps=100)
        spec["traits"] = [blessing("turtle"), {"api": "growth", "name": "growth", "stats": [],
            "timedStats": [{"after": 2, "interval": 0, "stats": [["hp", 1000]]}]}]
        spec["duration"] = 4.1
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([hit[2] for hit in events(result, "heal", "primal turtle")], [80])

    def test_item_healing_and_turtle_keep_independent_clocks_and_sources(self):
        spec = plain_spec(damage=0, incoming_dps=100, fx=[{"healPerInterval": [0.02, 2]}])
        spec["traits"] = [blessing("turtle")]
        spec["duration"] = 4.1
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([(hit[0], hit[2]) for hit in events(result, "heal", "dragon's claw")], [(2, 20), (4, 20)])
        self.assertEqual([(hit[0], hit[2]) for hit in events(result, "heal", "primal turtle")], [(4, 40)])

    def test_dead_champion_cannot_heal_even_while_a_death_body_holds(self):
        spec = spec_for("Krug", star=1, dummy=generic_targets(incoming_dps=300,
            physical_share=1, target_count=1), pressure=True, duration=4.1)
        spec["enemyDebuffs"] = {}
        kit = spec["kits"]["base"]
        kit.update(hpStar=250, baseAd=0)
        kit["stats"].update(hp=250, ad=0, armor=0, mr=0, mana=10**6, initialMana=0)
        kit["calcs"]["HealthCalc1"] = {"dtype": "magic", "terms": [{"type": "flat", "value": 1000, "op": "add"}]}
        spec["traits"] = [blessing("turtle", primal=False)]
        _, result = ENGINE.simulate(spec, True)
        self.assertIsNotNone(result["diedAt"])
        self.assertEqual(events(result, "heal", "primal turtle"), [])

    def test_theory_each_living_frontline_ally_heals_itself_and_conserves_pressure(self):
        first = plain_spec(hp=1000, damage=0)
        second = plain_spec(hp=2000, damage=0)
        first["unit"].update(api="a", name="a")
        second["unit"].update(api="b", name="b")
        for spec in (first, second):
            spec["traits"] = [blessing("turtle", primal=False)]
        result = measure([first, second], [True, True], window=4.1, pressure=100)
        self.assertEqual([sample["selfHeal"] for sample in result["samples"]], [40, 80])
        self.assertEqual([sample["allyHealPotential"] for sample in result["samples"]], [0, 0])
        spent = math.fsum(sample["incomingSpent"] + sample["denied"] for sample in result["samples"])
        self.assertAlmostEqual(spent + result["unspentPressure"], result["incomingBudget"])


class TestPrimalOtherBlessings(unittest.TestCase):
    def test_tiger_preserves_delayed_member_and_team_amounts(self):
        for primal, bonus in ((True, 0.35), (False, 0.15)):
            with self.subTest(primal=primal):
                spec = plain_spec()
                spec["traits"] = [blessing("tiger", primal=primal)]
                spec["duration"] = 6.1
                _, result = ENGINE.simulate(spec, True)
                grants = events(result, "trait stats")
                self.assertEqual([(hit[0], hit[2]) for hit in grants], [(6, bonus)])

    def test_phoenix_adds_no_damage_healing_or_items_to_a_fixed_fight(self):
        spec = plain_spec(incoming_dps=10)
        spec["duration"] = 8.1
        before = ENGINE.simulate(spec, True)
        spec["traits"] = [blessing("phoenix")]
        self.assertEqual(ENGINE.simulate(spec, True), before)

    def test_tiger_and_turtle_pair_retains_both_distinct_effects(self):
        spec = plain_spec(damage=0, incoming_dps=100)
        spec["traits"] = [blessing("tiger", "turtle")]
        spec["duration"] = 8.1
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([hit[0] for hit in events(result, "trait stats")], [6])
        self.assertEqual([(hit[0], hit[2]) for hit in events(result, "heal", "primal turtle")], [(4, 40), (8, 40)])

    def test_invalid_execute_or_heal_parameters_fail_before_simulation(self):
        for field, value in (("executeBelowHp", -0.1), ("executeBelowHp", 1.1),
                             ("executeBelowHp", float("nan")), ("healPerInterval", [0.04, 0]),
                             ("healPerInterval", [0.04, float("inf")]), ("healPerInterval", [1.1, 4])):
            with self.subTest(field=field, value=value):
                spec = plain_spec()
                spec["traits"] = [{"api": "DA_Primal18", "name": "Primal", "stats": [], field: value}]
                with self.assertRaises(ValueError):
                    ENGINE.simulate(spec, False)


if __name__ == "__main__":
    unittest.main()
