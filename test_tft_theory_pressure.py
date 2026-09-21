"""Shared generic pressure conserves damage and ends protected output at collapse."""
from copy import deepcopy
import math
import unittest

import tft
from tft_unit_profiles import UnitProfiles
from test_tft_unit_profiles import plain_spec


def actor(api, *, hp=1000, damage=0, attack_speed=1, items=(), targets=1):
    spec = plain_spec(hp=hp, damage=damage, fx=items)
    spec["unit"] = dict(spec["unit"], api=api, name=api, kind="Specialist", objective="tank")
    spec["kits"]["base"]["stats"]["as"] = attack_speed
    spec["dummies"]["slots"] = [deepcopy(spec["dummies"]["slots"][0]) for _ in range(targets)]
    return spec


def measure(specs, fronts, *, window=2, pressure=1000, share=1, control_interval=8, control_duration=0):
    return tft.engine().measure_theory_team(specs, fronts, window, pressure, share,
                                           control_interval, control_duration)


class TestConservedTheoryPressure(unittest.TestCase):
    def conserved(self, result, pressure):
        spent = math.fsum(sample["incomingSpent"] for sample in result["samples"])
        denied = math.fsum(sample["denied"] for sample in result["samples"])
        self.assertAlmostEqual(result["incomingBudget"], pressure * result["elapsed"])
        self.assertAlmostEqual(result["incomingBudget"], spent + denied + result["unspentPressure"])

    def test_weak_front_death_redirects_its_entire_share_and_lethal_excess(self):
        specs = [actor("weak", hp=150), actor("strong", hp=1000),
                 actor("carry", damage=100, attack_speed=2)]
        result = measure(specs, [True, True, False], window=5)
        self.assertTrue(result["collapsed"])
        self.assertEqual(result["elapsed"], 1.5)
        self.assertEqual(result["samples"][0]["incomingSpent"], 150)
        self.assertEqual(result["samples"][1]["incomingSpent"], 1000)
        self.assertEqual(result["unspentPressure"], 350)
        self.assertEqual(result["samples"][2]["incomingSpent"], 0)
        # The carry attacks at0,.5,1; collapse at1.5 precedes its next attack.
        self.assertEqual(result["samples"][2]["damage"], 300)
        self.conserved(result, 1000)

    def test_dividing_static_health_into_multiple_fronts_does_not_delete_pressure(self):
        carry = actor("carry", damage=100, attack_speed=2)
        one = measure([actor("tank", hp=1150), carry], [True, False], window=5)
        two = measure([actor("weak", hp=150), actor("strong", hp=1000), carry], [True, True, False], window=5)
        self.assertEqual(one["elapsed"], two["elapsed"])
        self.assertEqual(one["unspentPressure"], two["unspentPressure"])
        self.assertEqual(one["samples"][-1]["damage"], two["samples"][-1]["damage"])
        self.conserved(one, 1000)
        self.conserved(two, 1000)

    def test_fractional_final_pulse_spends_exact_nominal_budget(self):
        result = measure([actor("tank", hp=1000)], [True], window=1.25, pressure=100)
        self.assertFalse(result["collapsed"])
        self.assertEqual(result["elapsed"], 1.25)
        self.assertEqual(result["samples"][0]["hp"], 875)
        self.assertEqual(result["unspentPressure"], 0)
        self.conserved(result, 100)

    def test_physical_and_magic_shares_are_conserved_after_resists(self):
        spec = actor("tank", hp=10000)
        spec["kits"]["base"]["stats"].update(armor=100, mr=0)
        for share, expected_loss in ((1, 500), (.5, 750), (0, 1000)):
            with self.subTest(share=share):
                result = measure([spec], [True], window=1, share=share)
                self.assertAlmostEqual(result["samples"][0]["hp"], 10000 - expected_loss)
                self.conserved(result, 1000)

    def test_input_permutation_preserves_all_unit_results(self):
        specs = [actor("z-weak", hp=150), actor("b-strong", hp=1000), actor("a-carry", damage=100)]
        before = measure(specs, [True, True, False])
        after = measure(list(reversed(specs)), [False, True, True])
        self.assertEqual(before, dict(after, samples=list(reversed(after["samples"])),
            targetOrder=[len(specs) - 1 - index for index in after["targetOrder"]],
            initialSourceTargets=[None if index is None else len(specs) - 1 - index
                                  for index in after["initialSourceTargets"]]))

    def test_all_actors_are_observed_at_the_actual_endpoint(self):
        result = measure([actor("weak", hp=1), actor("carry", damage=100)], [True, False], window=40)
        self.assertEqual(result["elapsed"], .5)
        self.assertTrue(all(sample["time"] == .5 for sample in result["samples"]))
        self.assertEqual(result["samples"][1]["damage"], 100)
        self.conserved(result, 1000)

    def test_opening_focus_is_independent_of_outgoing_target_count(self):
        for targets in (1, 3):
            spec = actor("tank", targets=targets)
            spec["traits"] = [{"api": "monolith", "name": "Monolith", "stats": [], "resistsPerAttacker": [10, 10]}]
            front = tft.engine().theory_opening(spec, True)
            back = tft.engine().theory_opening(spec, False)
            self.assertEqual((front["armor"], front["mr"]), (10, 10))
            self.assertEqual((back["armor"], back["mr"]), (0, 0))
            self.assertEqual(front["damage"], 0)

    def test_outgoing_target_count_does_not_change_uncontrolled_pressure(self):
        one = measure([actor("tank", hp=10000, targets=1)], [True], window=3)
        three = measure([actor("tank", hp=10000, targets=3)], [True], window=3)
        self.assertEqual(one, three)

    def test_assassin_off_target_reduction_uses_incoming_source_identity(self):
        for share in (0, .5, 1):
            outcomes = []
            for targets in (1, 3):
                spec = actor("assassin", hp=10000, targets=targets)
                spec["unit"]["kind"] = "Assassin"
                result = measure([spec], [True], window=3, share=share)
                with self.subTest(share=share, targets=targets):
                    # Of six500-point pulses, two come from the primary
                    # target; four receive the Assassin's15% reduction.
                    self.assertEqual(result["samples"][0]["hp"], 7300)
                    self.assertEqual(result["samples"][0]["incomingSpent"], 3000)
                    self.conserved(result, 1000)
                outcomes.append(result)
            self.assertEqual(*outcomes)

    def test_shared_frontline_preserves_wound_when_cached_spec_pressure_is_false(self):
        spec = actor("tank", items=[{"healPerInterval": [.2, 1]}])
        spec["enemyDebuffs"] = {"wound": .33, "sunder": 0, "shred": 0}
        outcomes = []
        for pressure_flag in (False, True):
            spec["pressure"] = pressure_flag
            result = measure([spec], [True], window=1.5, pressure=600)
            with self.subTest(pressure_flag=pressure_flag):
                self.assertAlmostEqual(result["samples"][0]["selfHeal"], 134)
                self.conserved(result, 600)
            outcomes.append(result)
        self.assertEqual(*outcomes)
        spec["enemyDebuffs"]["wound"] = 0
        clean = measure([spec], [True], window=1.5, pressure=600)
        self.assertEqual(clean["samples"][0]["selfHeal"], 200)
        self.assertGreater(clean["samples"][0]["hp"], outcomes[0]["samples"][0]["hp"])


class TestTheoryControl(unittest.TestCase):
    def test_control_affects_frontline_actions_and_respects_immunity(self):
        front = actor("front", hp=100000, damage=100)
        back = actor("back", damage=100)
        clean = measure([front, back], [True, False], window=4, pressure=100)
        controlled = measure([front, back], [True, False], window=4, pressure=100,
                             control_interval=1, control_duration=.5)
        immune = deepcopy(front)
        immune["items"] = [{"api": "test-immunity", "name": "test-immunity", "unique": False,
                            "stats": [], "adds": [], "ccImmuneDuration": 10}]
        protected = measure([immune, back], [True, False], window=4, pressure=100,
                            control_interval=1, control_duration=.5)
        self.assertLess(controlled["samples"][0]["damage"], clean["samples"][0]["damage"])
        self.assertEqual(controlled["samples"][1]["damage"], clean["samples"][1]["damage"])
        self.assertEqual(protected["samples"][0]["damage"], clean["samples"][0]["damage"])
        self.assertEqual(protected["incomingBudget"], controlled["incomingBudget"])

    @staticmethod
    def stun_actor(api, *, name="Leona", targets=3):
        snap = tft.load_snapshot(18, "18.1d")
        unit = snap.unit(name)
        spec = UnitProfiles(snap, "clump" if targets == 3 else "spread").spec(
            unit["api"], 2, [], [], target_count=targets)
        spec["unit"]["api"] = api
        # an instant cast: no bin cast time and no cast timeline of the unit's
        spec["unit"]["castTime"] = 0
        spec["unit"]["timing"] = None
        spec["kits"]["base"]["stats"].update(initialMana=1000000, mana=1000000)
        spec["kits"]["base"]["rows"]["StunDuration"] = 2
        if name == "Hecarim":
            spec["kits"]["base"]["rows"]["NumEnemies"] = 3
        return spec

    def test_one_target_stun_denies_one_of_three_sources_in_both_geometries(self):
        for targets in (1, 3):
            with self.subTest(targets=targets):
                result = measure([actor("front", hp=10000, targets=targets),
                                  self.stun_actor("stunner", targets=targets)],
                                 [True, False], window=1.5)
                self.assertEqual(result["incomingBudget"], 1500)
                self.assertEqual(result["samples"][0]["denied"], 500)
                self.assertEqual(result["samples"][0]["incomingSpent"], 1000)

    def test_three_target_stun_only_covers_the_targets_in_reach(self):
        for targets, denied in ((1, 500), (3, 1500)):
            with self.subTest(targets=targets):
                result = measure([actor("front", hp=10000, targets=targets),
                                  self.stun_actor("stunner", name="Hecarim", targets=targets)],
                                 [True, False], window=1.5)
                self.assertEqual(result["samples"][0]["denied"], denied)
                self.assertEqual(result["samples"][0]["incomingSpent"], 1500 - denied)

    def test_overlapping_allied_stuns_deny_a_shared_packet_once(self):
        tank = actor("front", hp=10000, targets=3)
        first, second = self.stun_actor("stun-a"), self.stun_actor("stun-b")
        one = measure([tank, first], [True, False], window=1.5)
        two = measure([tank, first, second], [True, False, False], window=1.5)
        denied_one = math.fsum(sample["denied"] for sample in one["samples"])
        denied_two = math.fsum(sample["denied"] for sample in two["samples"])
        self.assertGreater(denied_one, 0)
        self.assertEqual(denied_one, denied_two)
        self.assertLessEqual(denied_two, two["incomingBudget"])
        self.assertAlmostEqual(math.fsum(sample["incomingSpent"] + sample["denied"] for sample in two["samples"])
                               + two["unspentPressure"], two["incomingBudget"])


class TestTheoryPressureInputs(unittest.TestCase):
    def test_invalid_team_and_scenario_inputs_raise(self):
        good = actor("tank")
        for specs, fronts in (([], []), ([good], []), ([good], [False]), ([good, good], [True, True])):
            with self.subTest(fronts=fronts), self.assertRaises(ValueError):
                measure(specs, fronts)
        for values in ({"window": -1}, {"window": math.nan}, {"window": 2048}, {"pressure": 0},
                       {"pressure": math.inf}, {"share": 1.1}, {"control_interval": 0}, {"control_duration": -1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                measure([good], [True], **values)

    def test_invalid_target_health_and_resists_raise_before_actor_clocks_start(self):
        for field, value in (("hp", 0), ("hp", math.nan), ("hp", math.inf),
                             ("armor", -1), ("armor", math.nan), ("mr", math.inf)):
            spec = actor("tank")
            spec["dummies"]["slots"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                measure([spec], [True])
            with self.subTest(opening=field, value=value), self.assertRaises(ValueError):
                tft.engine().theory_opening(spec, True)

    def test_invalid_actor_stats_raise_before_actor_clocks_start(self):
        for field, value in (("as", 0), ("as", -1), ("as", math.nan),
                             ("as", math.inf), ("mana", math.nan), ("initialMana", math.inf)):
            spec = actor("tank")
            spec["kits"]["base"]["stats"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                measure([spec], [True])
        for hp in (0, -1, math.nan, math.inf):
            with self.subTest(hp=hp), self.assertRaises(ValueError):
                measure([actor("tank", hp=hp)], [True])
        spec = actor("tank")
        spec["unit"]["castTime"] = -1
        with self.assertRaises(ValueError):
            measure([spec], [True])


if __name__ == "__main__":
    unittest.main()
