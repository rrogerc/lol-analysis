"""The Rust scorer matches independent aggregate math and search decisions.

The Python reference aggregates raw native persistent-target observations;
hand-computed scheduler tests independently check pressure and timing. Frozen
capture/replay comparisons apply within one model revision, not across deliberate
model changes. Full-board benchmarks retain the production item search.
"""
from copy import deepcopy
import math
import unittest
from unittest.mock import patch

import tft
import tft_theory as theory
from tft_comp_traits import resolve_board_traits
from tft_unit_profiles import UnitProfiles
from jobs.tft_theory_verify import (assert_close, cases, check_optimizer,
                                   evaluate, run_optimizer, score_order)


class TestNativeTheoryParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.inputs = cases(cls.snap)
        cls.reference = {g: theory.ReferenceEvaluator(cls.snap, g) for g in ("spread", "clump")}
        cls.native = {g: theory.Evaluator(cls.snap, g) for g in ("spread", "clump")}
        cls.expected, cls.actual = {}, {}
        for case in cls.inputs:
            cls.expected[case["name"]] = evaluate(cls.reference[case["geometry"]], case)
            cls.actual[case["name"]] = evaluate(cls.native[case["geometry"]], case)

    def test_every_scenario_aggregate_contribution_and_role_matches_reference(self):
        self.assertEqual(len(self.inputs), 80)
        self.assertGreaterEqual(len({member["api"] for case in self.inputs for member in case["members"]}), 30)
        self.assertEqual({member["star"] for case in self.inputs for member in case["members"]}, {1, 2, 3})
        for name in self.expected:
            with self.subTest(case=name):
                assert_close(self.expected[name], self.actual[name], name)

    def test_complete_capacity_ranking_is_unchanged(self):
        self.assertEqual(score_order(self.expected), score_order(self.actual))
        for geometry in ("spread", "clump"):
            before, after = (self.actual[name + "-" + geometry] for name in ("reported", "defensive"))
            self.assertGreater(after["metrics"]["theoryScore"], before["metrics"]["theoryScore"])
            self.assertLess(after["metrics"]["damageDps"], before["metrics"]["damageDps"])

    def test_compact_results_omit_unit_contributions_and_target_diagnostics(self):
        for case in self.inputs:
            with self.subTest(case=case["name"]):
                actual = evaluate(self.native[case["geometry"]], case, details=False)
                expected = deepcopy(self.expected[case["name"]])
                expected["units"] = {}
                for row in expected["scenarios"]:
                    del row["pressureTargetOrder"], row["initialPressureTargets"]
                assert_close(expected, actual)

    def test_mixed_allocation_batch_preserves_order_and_cache_results(self):
        lookup = {case["name"]: case for case in self.inputs}
        for geometry in ("spread", "clump"):
            names = [name + "-" + geometry for name in ("refined", "reported", "defensive", "reported")]
            case = lookup[names[0]]
            result = self.native[geometry].evaluate_many(case["members"], case["effects"],
                [lookup[name]["selected"] for name in names], case["carry"], case["tank"], details=True)
            for name, actual in zip(names, result, strict=True):
                assert_close(self.expected[name], actual)

    def test_member_order_preserves_roles_providers_and_numeric_results(self):
        for case in self.inputs[:20]:
            reordered = dict(case, members=list(reversed(case["members"])))
            with self.subTest(case=case["name"]):
                actual = evaluate(self.native[case["geometry"]], reordered)
                assert_close(self.expected[case["name"]], actual)

    def test_production_scoring_never_calls_python_response_curves(self):
        case = self.inputs[0]
        native = theory.Evaluator(self.snap, case["geometry"])
        with patch.object(UnitProfiles, "curve", side_effect=AssertionError("Python curve scoring was used")):
            actual = evaluate(native, case)
        assert_close(self.expected[case["name"]], actual)
        self.assertGreater(native.stats["unitLoadoutsPrepared"], 0)

    def test_full_item_search_on_two_real_unit_seeds_preserves_winner_and_evidence(self):
        # Complete item pool and search limits on a smaller real roster keep
        # routine tests bounded. The helper benchmarks full eight-unit boards.
        carry, tank = "TFT18_Sivir", "TFT18_Malphite"
        members = [{"api": api, "star": 2} for api in (carry, tank)]
        effects = resolve_board_traits(self.snap, members)["effects"]
        original = {carry: {"items": ["DA_GiantSlayer", "DA_InfinityEdge", "DA_KrakensFury"], "alpha": False},
                    tank: {"items": ["DA_LastWhisper", "DA_RedBuff", "DA_ArchangelsStaff"], "alpha": False}}
        defensive = deepcopy(original)
        defensive[tank]["items"] = ["DA_WarmogsArmor", "DA_GargoyleStoneplate", "DA_SunfireCape"]
        case = {"members": members, "effects": effects, "selected": original,
                "carry": carry, "tank": tank, "geometry": "clump"}
        workload = {"case": case, "seeds": [original, defensive],
                    "anchors": {api: [original[api], defensive[api]] for api in (carry, tank)}}
        expected = run_optimizer(self.snap, theory.ReferenceEvaluator(self.snap, "clump"), workload)
        actual = run_optimizer(self.snap, theory.Evaluator(self.snap, "clump"), workload)
        self.assertGreater(expected["stats"]["singleItemComparisons"], 100)
        check_optimizer(expected, actual)


class TestNativeTheoryAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.scenarios = theory.scenarios("clump")

    def scorer(self, **kwargs):
        return tft.engine().TheoryScorer(self.scenarios, **kwargs)

    def spec(self, name, *, star=2, items=()):
        api = self.snap.unit(name)["api"]
        spec = UnitProfiles(self.snap, "clump").spec(api, star, [], items)
        return dict(spec, pool=[])

    def test_invalid_pressure_profiles_fail_at_construction(self):
        factory = tft.engine().TheoryScorer
        with self.assertRaises(ValueError):
            factory([])
        for field, values in (("incomingDps", (0, -1, math.inf, math.nan)),
                              ("physicalShare", (-.1, 1.1, math.nan)),
                              ("wound", (-.1, 1.1, math.inf)),
                              ("armor", (-1, math.inf)), ("mr", (-1, math.nan)),
                              ("targetHp", (0, -1, math.inf)),
                              ("controlInterval", (0, -1, math.inf, math.nan)),
                              ("controlDuration", (-1, math.inf, math.nan)),
                              ("incomingSourceCount", (0, 1, 2, 4)),
                              ("pressureAllocation", ("shared-live-frontline", "", "independent-frontline")),
                              ("targeting", ("", "random", "main")),
                              ("targetCount", (0, 9, 1.5, True))):
            for value in values:
                scenario = dict(self.scenarios[0], **{field: value})
                with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError, OverflowError)):
                    factory([scenario])

    def test_invalid_workload_and_cache_limits_fail_at_construction(self):
        for kwargs in ({"max_window": 0}, {"max_window": math.nan}, {"max_window": 3000},
                       {"cache_limit": 0}, {"cache_limit": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, TypeError, OverflowError)):
                self.scorer(**kwargs)

    def test_registration_rejects_malformed_unit_and_item_inputs(self):
        scorer = self.scorer()
        base = self.spec("Leona", items=("DA_WarmogsArmor",))
        mutations = [lambda spec: spec.update(star=0), lambda spec: spec.update(star=4),
                     lambda spec: spec["unit"].update(api=""),
                     lambda spec: spec.update(driver="unknown-driver"),
                     lambda spec: spec.update(items=spec["items"] * 4)]
        for mutate in mutations:
            spec = deepcopy(base)
            mutate(spec)
            with self.subTest(spec=spec["unit"]["api"]), self.assertRaises((ValueError, TypeError)):
                scorer.register(spec)
        unique = deepcopy(base)
        unique["items"][0]["unique"] = True
        unique["items"] *= 2
        with self.assertRaises(ValueError):
            scorer.register(unique)

    def test_registered_inputs_are_owned_and_batched_order_is_preserved(self):
        scorer = self.scorer()
        tank_spec = self.spec("Leona", items=("DA_WarmogsArmor",))
        expected_spec = deepcopy(tank_spec)
        tank = scorer.register(tank_spec)
        carry = scorer.register(self.spec("Sivir", items=("DA_InfinityEdge",)))
        tank_spec["kits"]["base"]["stats"]["hp"] *= 100
        # Registration copied the template, before any response was measured.
        other = self.scorer()
        expected_tank = other.register(expected_spec)
        expected_carry = other.register(self.spec("Sivir", items=("DA_InfinityEdge",)))
        actual = scorer.evaluate_many([[carry, tank], [tank, carry]], 0, 1, details=True)
        expected = other.evaluate_many([[expected_carry, expected_tank], [expected_tank, expected_carry]],
                                      0, 1, details=True)
        assert_close(expected, actual)
        self.assertEqual(scorer.evaluate_many([], 0, 1), [])
        before = scorer.stats()
        repeated = scorer.evaluate_many([[carry, tank]], 0, 1, details=True)
        assert_close([actual[0]], repeated)
        after = scorer.stats()
        self.assertEqual(after["teamAllocationsReused"], before["teamAllocationsReused"] + 1)
        for field in ("teamAllocationsSimulated", "unitOpeningsMeasured", "sharedMeasurements"):
            self.assertEqual(after[field], before[field])

    def test_cache_eviction_changes_only_reuse_not_results(self):
        for limit in (1, 4096):
            scorer = self.scorer(cache_limit=limit)
            identifiers = [scorer.register(self.spec(name, items=items)) for name, items in
                           (("Leona", ()), ("Leona", ("DA_WarmogsArmor",)), ("Sivir", ("DA_InfinityEdge",)))]
            allocations = [[identifiers[2], identifiers[0]], [identifiers[2], identifiers[1]],
                           [identifiers[2], identifiers[0]]]
            actual = scorer.evaluate_many(allocations, 0, 1, details=True)
            assert_close(actual[0], actual[2])
            if limit == 1:
                expected = actual
                self.assertEqual(scorer.stats()["teamAllocationsSimulated"], 3)
            else:
                assert_close(expected, actual)
                self.assertEqual(scorer.stats()["teamAllocationsSimulated"], 2)

    def test_invalid_allocations_and_main_indices_raise_python_errors(self):
        scorer = self.scorer()
        tank = scorer.register(self.spec("Leona"))
        alternate_tank = scorer.register(self.spec("Leona", items=("DA_WarmogsArmor",)))
        carry = scorer.register(self.spec("Sivir"))
        for boards, carry_index, tank_index in (([[]], 0, 1), ([[tank]], 0, 1),
                                                ([[tank, tank]], 0, 1), ([[tank, alternate_tank]], 0, 1),
                                                ([[tank, carry]], 0, 0), ([[tank, carry]], 0, 2),
                                                ([[tank, carry]], -1, 1), ([[tank, 999999]], 0, 1),
                                                ([[tank, -1]], 0, 1)):
            with self.subTest(boards=boards, carry=carry_index, tank=tank_index), \
                    self.assertRaises((ValueError, TypeError, OverflowError)):
                scorer.evaluate_many(boards, carry_index, tank_index)

    def test_no_frontline_and_too_many_team_slots_are_rejected(self):
        scorer = self.scorer()
        carries = [scorer.register(self.spec(name)) for name in ("Sivir", "Ashe")]
        with self.assertRaises(ValueError):
            scorer.evaluate_many([carries], 0, 1)
        crowded = [scorer.register(self.spec(name)) for name in
                   ("Leona", "Sivir", "Ashe", "Ahri", "Amumu", "Yorick", "Karma", "Diana", "Elder Dragon")]
        with self.assertRaises(ValueError):
            scorer.evaluate_many([crowded], 1, 0)

    def test_unsupported_derived_window_is_an_error_not_a_cutoff_score(self):
        scorer = self.scorer(max_window=1)
        tank = scorer.register(self.spec("Leona"))
        carry = scorer.register(self.spec("Sivir"))
        with self.assertRaisesRegex(ValueError, "window|range|cutoff"):
            scorer.evaluate_many([[carry, tank]], 0, 1)


if __name__ == "__main__":
    unittest.main()
