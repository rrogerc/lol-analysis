"""Primal choice search preserves one legal, auditable choice for a whole board."""
from copy import deepcopy
import math
import unittest

import tft
import tft_theory as theory
from tft_comp_traits import (PRIMAL, PRIMAL_BLESSINGS, primal_options, primal_selection,
                             resolve_board_traits, with_primal_effects)
from jobs.tft_theory_verify import assert_close


class _ChoiceOracle(theory.ReferenceEvaluator):
    """Two contrasting pressure outcomes expose per-profile cherry-picking."""
    def __init__(self, snap, geometry, *, tied=False):
        super().__init__(snap, geometry, optimize_primal=True)
        self.calls = []
        self.tied = tied

    def _evaluate_resolved_many(self, members, effects, allocations, carry, tank, **kwargs):
        trait = next(effect for effect in effects[carry] if effect["api"] == PRIMAL)
        self.calls.append(deepcopy(trait))
        tiger, turtle = bool(trait.get("timedStats")), bool(trait.get("healPerInterval"))
        scores = [100, 100] if self.tied else [160, 40] if tiger else [60, 160] if turtle else [25, 25]
        return [{"metrics": {"theoryScore": math.sqrt(math.prod(scores))},
                 "scenarios": [{"score": score} for score in scores]} for _ in allocations]


class TestPrimalChoices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def members(self, *names):
        return [{"api": self.snap.unit(name)["api"], "star": 2} for name in names]

    def fixture(self, snap=None, members=None):
        snap = snap or self.snap
        members = members or self.members("Sivir", "Vi", "Leona")
        resolved = resolve_board_traits(snap, members)
        selected = {member["api"]: {"items": [], "alpha": False} for member in members}
        return members, resolved["effects"], [selected], members[0]["api"], members[1]["api"]

    def test_all_four_single_choices_and_no_free_blessing_below_two(self):
        self.assertEqual(primal_options(self.snap, self.members("Sivir", "Leona")), [()])
        self.assertEqual(primal_options(self.snap, self.members("Sivir", "Vi")),
                         [(key,) for key in PRIMAL_BLESSINGS])
        self.assertEqual(primal_options(self.snap, self.members("Sivir", "Vi", "Nidalee", "Lux")),
                         [(key,) for key in PRIMAL_BLESSINGS], "generic Lux grants no free origin")

    def test_all_six_distinct_pairs_at_an_explicit_four_primal_breakpoint(self):
        # A supplied trait assignment exercises tier4 without pretending the
        # production roster search already supports an emblem or Lux variant.
        snap = deepcopy(self.snap)
        extra = snap.unit("Cinderling")["api"]
        snap.units[extra]["traitApis"].append(PRIMAL)
        members = self.members("Sivir", "Vi", "Nidalee", "Cinderling")
        options = primal_options(snap, members)
        self.assertEqual(len(options), 6)
        self.assertTrue(all(len(set(option)) == 2 for option in options))
        for selected in options:
            resolved = resolve_board_traits(snap, members, primal_blessings=selected)
            self.assertEqual(resolved["primalBlessings"], list(selected))
        with self.assertRaises(ValueError):
            resolve_board_traits(snap, members, primal_blessings=["tiger"])

    def test_invalid_duplicates_unknown_names_and_wrong_counts_fail(self):
        members = self.members("Sivir", "Vi")
        for selected in ("tiger", ["bogus"], ["tiger", "tiger"], ["tiger", "turtle"], [], [None]):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                primal_selection(self.snap, members, selected)
        with self.assertRaises(ValueError):
            primal_selection(self.snap, self.members("Sivir", "Leona"), ["turtle"])

    def test_choice_replaces_tiger_without_changing_other_traits_or_inputs(self):
        members, effects, *_ = self.fixture()
        original = deepcopy(effects)
        turtle = with_primal_effects(self.snap, members, effects, ["turtle"])
        for api in effects:
            self.assertEqual([e for e in turtle[api] if e["api"] != PRIMAL],
                             [e for e in effects[api] if e["api"] != PRIMAL])
            chosen = next(e for e in turtle[api] if e["api"] == PRIMAL)
            self.assertEqual(chosen["healPerInterval"], [0.04, 4])
            self.assertNotIn("timedStats", chosen)
        self.assertEqual(effects, original)

    def test_bear_is_member_only_and_phoenix_does_not_invent_combat_stats(self):
        members = self.members("Sivir", "Vi", "Leona")
        bear = resolve_board_traits(self.snap, members, primal_blessings=["bear"])
        phoenix = resolve_board_traits(self.snap, members, primal_blessings=["phoenix"])
        for member in members:
            api = member["api"]
            effect = next(e for e in bear["effects"][api] if e["api"] == PRIMAL)
            self.assertEqual(effect.get("executeBelowHp", 0), 0 if api == members[-1]["api"] else 0.12)
            blank = next(e for e in phoenix["effects"][api] if e["api"] == PRIMAL)
            self.assertEqual(blank, {"api": PRIMAL, "name": "Primal", "stats": []})
        self.assertTrue(any("unmeasured" in note for note in bear["limitations"]))
        self.assertTrue(any("unmeasured" in note for note in phoenix["limitations"]))

    def test_aggregate_choice_never_combines_different_profile_winners(self):
        inputs = self.fixture()
        original = deepcopy(inputs)
        scorer = _ChoiceOracle(self.snap, "clump")
        result = scorer.evaluate_many(*inputs, details=True)[0]
        self.assertEqual(result["primal"]["selected"], ["turtle"])
        self.assertEqual([s["score"] for s in result["scenarios"]], [60, 160])
        self.assertAlmostEqual(result["metrics"]["theoryScore"], math.sqrt(60 * 160))
        self.assertEqual(len(result["primal"]["alternatives"]), 4)
        self.assertEqual(len(scorer.calls), 3, "Bear/Phoenix share identical immortal-target physics")
        self.assertEqual(inputs, original)

    def test_exact_ties_keep_the_previous_tiger_default_without_score_bonus(self):
        result = _ChoiceOracle(self.snap, "clump", tied=True).evaluate_many(*self.fixture())[0]
        self.assertEqual(result["primal"]["selected"], ["tiger"])
        self.assertEqual({a["score"] for a in result["primal"]["alternatives"]}, {100})

    def test_choice_evidence_is_independent_between_calls(self):
        scorer = _ChoiceOracle(self.snap, "clump")
        first = scorer.evaluate_many(*self.fixture())[0]
        first["primal"]["selected"].append("bear")
        first["primal"]["alternatives"][0]["score"] = -1
        second = scorer.evaluate_many(*self.fixture())[0]
        self.assertEqual(second["primal"]["selected"], ["turtle"])
        self.assertTrue(all(a["score"] >= 0 for a in second["primal"]["alternatives"]))

    def test_upgrade_retains_its_prior_choice_instead_of_freely_respecing(self):
        scorer = _ChoiceOracle(self.snap, "clump")
        inputs = self.fixture()
        unrestricted = scorer.evaluate_many(*inputs)[0]
        constrained = scorer.evaluate_many(*inputs, required_primal=["tiger"])[0]
        self.assertEqual(unrestricted["primal"]["selected"], ["turtle"])
        self.assertEqual(constrained["primal"]["selected"], ["tiger"])
        self.assertEqual(constrained["primal"]["required"], ["tiger"])
        self.assertEqual(len(constrained["primal"]["alternatives"]), 1)
        self.assertLess(constrained["metrics"]["theoryScore"], unrestricted["metrics"]["theoryScore"])

    def test_reaching_four_adds_one_blessing_and_inactive_trait_retains_no_effect(self):
        snap = deepcopy(self.snap)
        snap.unit("Cinderling")["traitApis"].append(PRIMAL)
        members = self.members("Sivir", "Vi", "Nidalee", "Cinderling")
        options = primal_options(snap, members, required=["turtle"])
        self.assertEqual(len(options), 3)
        self.assertTrue(all("turtle" in choice and len(choice) == 2 for choice in options))
        inactive = self.members("Sivir", "Leona")
        self.assertEqual(primal_options(snap, inactive, required=["turtle"]), [()])
        scorer = theory.Evaluator(snap, "clump", optimize_primal=True)
        result = scorer.evaluate_many(*self.fixture(snap, inactive), required_primal=["turtle"])[0]
        self.assertNotIn("primal", result)

    def test_native_choices_match_explicit_independent_reference_results(self):
        for geometry in ("spread", "clump"):
            with self.subTest(geometry=geometry):
                members, effects, allocations, carry, tank = self.fixture()
                native = theory.Evaluator(self.snap, geometry, optimize_primal=True)
                reference = theory.ReferenceEvaluator(self.snap, geometry)
                expected = []
                for option in primal_options(self.snap, members):
                    variant = with_primal_effects(self.snap, members, effects, option)
                    result = reference.evaluate_many(members, variant, allocations, carry, tank, details=True)[0]
                    expected.append((option, result))
                actual = native.evaluate_many(members, effects, allocations, carry, tank, details=True)[0]
                best, winner = min(expected, key=lambda pair: theory.rank_key(pair[1]))
                self.assertEqual(actual["primal"]["selected"], list(best))
                assert_close(winner, {k: v for k, v in actual.items() if k != "primal"})
                for evidence, (option, result) in zip(actual["primal"]["alternatives"], expected, strict=True):
                    self.assertEqual(evidence["blessings"], list(option))
                    self.assertAlmostEqual(evidence["score"], result["metrics"]["theoryScore"], delta=1e-7)

    def test_pair_search_compares_six_choices_with_four_distinct_physics_batches(self):
        snap = deepcopy(self.snap)
        snap.unit("Cinderling")["traitApis"].append(PRIMAL)
        inputs = self.fixture(snap, self.members("Sivir", "Vi", "Nidalee", "Cinderling"))
        scorer = theory.Evaluator(snap, "clump", optimize_primal=True)
        result = scorer.evaluate_many(*inputs)[0]
        self.assertEqual(len(result["primal"]["selected"]), 2)
        self.assertEqual(len(result["primal"]["alternatives"]), 6)
        self.assertEqual(scorer.stats["primalDistinctEffectsCompared"], 4)

    def test_explicit_and_inactive_contexts_keep_the_existing_output_contract(self):
        for members in (self.members("Sivir", "Vi", "Leona"), self.members("Sivir", "Leona")):
            inputs = self.fixture(members=members)
            fixed = theory.Evaluator(self.snap, "clump").evaluate_many(*inputs)[0]
            self.assertNotIn("primal", fixed)
        inactive = theory.Evaluator(self.snap, "clump", optimize_primal=True).evaluate_many(*inputs)[0]
        self.assertNotIn("primal", inactive)
        assert_close(fixed, inactive)


if __name__ == "__main__":
    unittest.main()
