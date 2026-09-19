"""Persistent pressure owners, source identity and opening-mask regressions."""
import math
import unittest

from test_tft import ENGINE, spec_for
import test_tft_theory_pressure as pressure_fixtures
from test_tft_theory_pressure import actor
from tft_unit_profiles import generic_targets


def tank(api, *, hp=10000, targets=3, resists=0, fx=()):
    spec = actor(api, hp=hp, targets=targets, items=fx)
    if resists:
        spec["traits"] = [{"api": "test-focus", "name": "test-focus", "stats": [],
                           "resistsPerAttacker": [resists, resists]}]
    return spec


def measure(specs, fronts=None, *, order=None, window=1.5, pressure=1000, share=1,
            control_interval=8, control_duration=0):
    if fronts is None:
        fronts = [True] * len(specs)
    return ENGINE.measure_theory_team(specs, fronts, window, pressure, share,
        control_interval, control_duration, target_order=order)


def conserved(test, result, pressure):
    spent = math.fsum(sample["incomingSpent"] for sample in result["samples"])
    denied = math.fsum(sample["denied"] for sample in result["samples"])
    test.assertAlmostEqual(result["incomingBudget"], pressure * result["elapsed"])
    test.assertAlmostEqual(result["incomingBudget"], spent + denied + result["unspentPressure"])


class TestPersistentOpening(unittest.TestCase):
    def test_single_tank_receives_three_source_stacks_for_any_outgoing_coverage(self):
        for targets in (1, 2, 3, 8):
            with self.subTest(targets=targets):
                result = measure([tank("front", hp=1000, targets=targets, resists=10)],
                                 order=[0], pressure=300)
                sample = result["samples"][0]
                self.assertEqual(result["initialSourceTargets"], [0, 0, 0])
                self.assertEqual((sample["armor"], sample["mr"]), (30, 30))
                self.assertAlmostEqual(sample["hp"], 1000 - 450 / 1.3)
                conserved(self, result, 300)

    def test_two_frontliners_start_two_plus_one_and_mirror_by_priority(self):
        specs = [tank("a", resists=10), tank("b", resists=10)]
        for order, owners, armor in (([0, 1], [0, 1, 0], [20, 10]),
                                     ([1, 0], [1, 0, 1], [10, 20])):
            with self.subTest(order=order):
                result = measure(specs, order=order, window=0)
                self.assertEqual(result["targetOrder"], order)
                self.assertEqual(result["initialSourceTargets"], owners)
                self.assertEqual([sample["armor"] for sample in result["samples"]], armor)

    def test_only_first_two_frontliners_start_with_owners(self):
        result = measure([tank("a", resists=10), tank("b", resists=10), tank("c", resists=10)],
                         order=[0, 1, 2])
        self.assertEqual([sample["armor"] for sample in result["samples"]], [20, 10, 0])
        self.assertEqual([sample["incomingSpent"] for sample in result["samples"]], [1000, 500, 0])
        conserved(self, result, 1000)

    def test_masks_remain_stable_between_every_source_pulse(self):
        specs = [tank("a", resists=10), tank("b", resists=10)]
        for time in (0.25, 0.5, 0.75, 1, 1.25, 1.5, 2):
            with self.subTest(time=time):
                result = measure(specs, order=[0, 1], window=time)
                self.assertEqual([sample["armor"] for sample in result["samples"]], [20, 10])
                conserved(self, result, 1000)

    def test_explicit_opening_masks_and_legacy_default(self):
        spec = tank("front", targets=1, resists=10)
        for mask in (0, 1, 2, 3, 4, 5, 6, 7):
            with self.subTest(mask=mask):
                sample = ENGINE.theory_opening(spec, True, source_mask=mask)
                self.assertEqual(sample["armor"], mask.bit_count() * 10)
                self.assertTrue(sample["targetable"])
        legacy = ENGINE.theory_opening(spec, True)
        explicit = ENGINE.theory_opening(spec, True, source_mask=1)
        self.assertNotIn("targetable", legacy)
        self.assertEqual(legacy, {key: value for key, value in explicit.items() if key != "targetable"})
        self.assertEqual(ENGINE.theory_opening(spec, False, source_mask=7)["armor"], 0)

    def test_mask_applies_before_driver_initialization_and_matches_measurement(self):
        spec = spec_for("Leona", star=1, pressure=True,
                        dummy=generic_targets(target_count=1))
        kit = spec["kits"]["base"]
        kit["stats"].update(armor=0, mr=0)
        # A controlled initializer reading current armor distinguishes
        # before-init masks from merely patching Gargoyle after init.
        kit["calcs"]["GenericCalc1"]["terms"] = [
            {"type": "scaled", "coef": 1, "scaling": "Armor", "preAdd": None, "op": "add"}]
        spec["traits"] = [{"api": "test-focus", "name": "test-focus", "stats": [],
                           "resistsPerAttacker": [10, 10]}]
        opening = ENGINE.theory_opening(spec, True, source_mask=7)
        self.assertEqual((opening["armor"], opening["mr"]), (60, 60))
        result = measure([spec], order=[0], window=0)
        self.assertEqual(result["samples"][0], {key: value for key, value in opening.items() if key != "targetable"})

    def test_each_actor_opening_agrees_with_its_initial_owner_mask(self):
        specs = [tank("z-front", resists=10), tank("a-front", resists=10), tank("back", resists=10)]
        result = measure(specs, [True, True, False], order=[0, 1], window=0)
        for index, spec in enumerate(specs):
            mask = sum(1 << source for source, owner in enumerate(result["initialSourceTargets"]) if owner == index)
            opening = ENGINE.theory_opening(spec, index < 2, source_mask=mask)
            self.assertEqual(result["samples"][index], {key: value for key, value in opening.items() if key != "targetable"})


class TestPersistentRetargeting(unittest.TestCase):
    def test_lethal_excess_keeps_source_and_updates_all_lost_owner_masks(self):
        result = measure([tank("a", hp=100, resists=10), tank("b", hp=1000, resists=10)],
                         order=[0, 1], window=0.5)
        first, second = result["samples"]
        # A holds sources0/2:100HP at20armor consumes120raw. Both now
        # retargetB, which receives the380 remainder at30armor.
        self.assertAlmostEqual(first["incomingSpent"], 120)
        self.assertAlmostEqual(second["incomingSpent"], 380)
        self.assertEqual(second["armor"], 30)
        self.assertAlmostEqual(second["hp"], 1000 - 380 / 1.3)
        conserved(self, result, 1000)

    def test_retargeting_prefers_first_eligible_without_balancing_other_fronts(self):
        result = measure([tank("a", hp=750), tank("b"), tank("c")],
                         order=[0, 1, 2], window=2)
        self.assertEqual([sample["incomingSpent"] for sample in result["samples"]], [750, 1250, 0])
        self.assertEqual(result["samples"][2]["hp"], 10000)
        conserved(self, result, 1000)

    def test_physical_remainder_and_magic_part_use_new_owner_with_original_source(self):
        first, second = tank("a", hp=100), tank("b", hp=1000)
        first["kits"]["base"]["stats"]["armor"] = 100
        second["kits"]["base"]["stats"]["mr"] = 100
        result = measure([first, second], order=[0, 1], window=0.5, share=0.5)
        self.assertAlmostEqual(result["samples"][0]["incomingSpent"], 200)
        self.assertAlmostEqual(result["samples"][1]["incomingSpent"], 300)
        self.assertAlmostEqual(result["samples"][1]["hp"], 825)
        conserved(self, result, 1000)

    def test_untargetability_retargets_and_does_not_respec_when_owner_returns(self):
        first = tank("a", hp=1000, fx=[{"untargetableAtHp": [0.9, 1, 0]}])
        result = measure([first, tank("b")], order=[0, 1], window=2)
        self.assertEqual([sample["incomingSpent"] for sample in result["samples"]], [500, 1500])
        self.assertEqual(result["samples"][0]["hp"], 500)
        self.assertEqual(result["samples"][1]["hp"], 8500)
        conserved(self, result, 1000)

    def test_physical_hit_that_triggers_untargetability_redirects_magic_part(self):
        first = tank("a", hp=1000, fx=[{"untargetableAtHp": [0.9, 1, 0]}])
        second = tank("b", hp=1000)
        second["unit"]["kind"] = "Assassin"
        result = measure([first, second], order=[0, 1], window=0.5, share=0.5)
        # This is still source0, so the second actor's primary-target
        # comparison receives no off-target reduction merely for beingB.
        self.assertEqual([sample["hp"] for sample in result["samples"]], [750, 750])
        conserved(self, result, 1000)

    def test_untargetable_fronts_deny_then_reacquire_without_losing_budget(self):
        spec = tank("a", hp=1000, fx=[{"untargetableAtHp": [0.9, 2, 0]}])
        during = measure([spec], order=[0], window=2)
        self.assertEqual(during["samples"][0]["incomingSpent"], 500)
        self.assertEqual(during["samples"][0]["denied"], 1500)
        after = measure([spec], order=[0], window=2.5)
        self.assertTrue(after["collapsed"])
        self.assertEqual(after["samples"][0]["incomingSpent"], 1000)
        self.assertEqual(after["samples"][0]["denied"], 1500)
        for result in (during, after):
            conserved(self, result, 1000)

    def test_death_bodies_hold_owner_and_same_source_excess_until_exhausted(self):
        krug = spec_for("Krug", star=1, pressure=True, dummy=generic_targets(target_count=1))
        kit = krug["kits"]["base"]
        kit.update(hpStar=100, baseAd=0)
        kit["stats"].update(hp=100, ad=0, armor=0, mr=0, mana=10**9, initialMana=0)
        kit["calcs"]["HealthCalc1"] = {"dtype": "magic", "terms": [{"type": "flat", "value": 100, "op": "add"}]}
        krug["unit"]["extras"]["TFT18_KrugMini"].update(armor=100, mr=0)
        krug["enemyDebuffs"] = {}
        other = tank("other", hp=1000, targets=1)
        early = measure([krug, other], order=[0, 1], pressure=600, window=0.5)
        self.assertFalse(early["samples"][0]["alive"])
        self.assertTrue(early["samples"][0]["holding"])
        self.assertEqual([sample["incomingSpent"] for sample in early["samples"]], [300, 0])
        later = measure([krug, other], order=[0, 1], pressure=600, window=1.5)
        self.assertFalse(later["samples"][0]["holding"])
        self.assertEqual([sample["incomingSpent"] for sample in later["samples"]], [500, 400])
        for result in (early, later):
            conserved(self, result, 600)

    def test_last_front_death_returns_lethal_excess_as_unspent(self):
        result = measure([tank("a", hp=100)], order=[0], window=5)
        self.assertTrue(result["collapsed"])
        self.assertEqual((result["elapsed"], result["unspentPressure"]), (0.5, 400))
        self.assertEqual(result["samples"][0]["incomingSpent"], 100)
        conserved(self, result, 1000)


class TestPersistentSourceAndOrder(unittest.TestCase):
    def test_source_cc_denies_its_owner_packet_without_erasing_ownership(self):
        specs = [tank("a", resists=10), tank("b", resists=10),
                 pressure_fixtures.TestTheoryControl.stun_actor("stunner")]
        result = measure(specs, [True, True, False], order=[0, 1])
        self.assertEqual([sample["denied"] for sample in result["samples"]], [500, 0, 0])
        self.assertEqual([sample["incomingSpent"] for sample in result["samples"]], [500, 500, 0])
        self.assertEqual([sample["armor"] for sample in result["samples"][:2]], [20, 10])
        conserved(self, result, 1000)

    def test_global_source_identity_survives_single_local_probe_projection(self):
        first, second = tank("a", targets=1), tank("b", hp=1000, targets=1)
        second["unit"]["kind"] = "Assassin"
        result = measure([first, second], order=[0, 1], window=1)
        self.assertEqual(result["samples"][1]["hp"], 575)
        self.assertEqual(result["samples"][1]["incomingSpent"], 500)
        conserved(self, result, 1000)

    def test_fractional_endpoint_keeps_global_source_cycle_and_total_dps(self):
        result = measure([tank("a"), tank("b")], order=[0, 1], window=1.25)
        self.assertEqual([sample["incomingSpent"] for sample in result["samples"]], [750, 500])
        self.assertEqual(result["incomingBudget"], 1250)
        conserved(self, result, 1000)

    def test_explicit_caller_priority_is_independent_of_api_action_order(self):
        specs = [tank("z", resists=10), tank("a", resists=10), tank("back")]
        explicit = measure(specs, [True, True, False], order=[0, 1], window=0)
        default = measure(specs, [True, True, False], window=0)
        self.assertEqual(explicit["initialSourceTargets"], [0, 1, 0])
        self.assertEqual(default["targetOrder"], [1, 0])
        self.assertEqual(default["initialSourceTargets"], [1, 0, 1])

    def test_caller_permutation_preserves_api_associations_and_canonical_default(self):
        specs = [tank("z", resists=10), tank("a", resists=10), tank("back")]
        first = measure(specs, [True, True, False])
        second = measure(list(reversed(specs)), [False, True, True])
        self.assertEqual(first["samples"], list(reversed(second["samples"])))
        for key in ("incomingBudget", "elapsed", "collapsed", "unspentPressure"):
            self.assertEqual(first[key], second[key])
        for board, result in ((specs, first), (list(reversed(specs)), second)):
            self.assertEqual([board[i]["unit"]["api"] for i in result["targetOrder"]], ["a", "z"])
            self.assertEqual([board[i]["unit"]["api"] for i in result["initialSourceTargets"]], ["a", "z", "a"])

    def test_invalid_priority_and_masks_are_rejected(self):
        specs = [tank("a"), tank("b"), tank("back")]
        for order in ([], [0], [0, 0], [0, 2], [0, 3], [0, 1, 2], [-1, 1]):
            with self.subTest(order=order), self.assertRaises((ValueError, OverflowError)):
                measure(specs, [True, True, False], order=order)
        for mask in (-1, 8, 255):
            with self.subTest(mask=mask), self.assertRaises(ValueError):
                ENGINE.theory_opening(specs[0], True, source_mask=mask)


if __name__ == "__main__":
    unittest.main()
