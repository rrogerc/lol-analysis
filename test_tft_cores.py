"""Core-item analysis with hand-computed, full-three-item fight results.

Run: python3 -m unittest test_tft_cores -v
These fixtures need neither the compiled engine nor a downloaded snapshot.
"""

from types import SimpleNamespace
import unittest

import tft


class CoreFixtures(unittest.TestCase):
    def setUp(self):
        self.pool = list("ABCDEFGHI")
        self.snap = SimpleNamespace(items={api: {"name": "Item " + api}
                                          for api in self.pool})

    def score(self, items, *, kill=10.0, total=1000.0, alive=30.0,
              capped=False, stress=None, stress_capped=False):
        """Scores are supplied best-first, exactly as the engine returns them."""
        indices = tuple(sorted(self.pool.index(api) for api in items))
        return indices, kill, total, alive, capped, stress, stress_capped

    def analyze(self, scores, objective="carry", **kwargs):
        # These fixtures isolate scoring/exclusion math. The production
        # two-choice eligibility rule is covered in test_tft_pair_cores.
        kwargs.setdefault("min_completions", 1)
        return tft.analyze_cores(self.snap, {"objective": objective}, self.pool,
                                 scores, **kwargs)

    def core(self, result, apis):
        return next(c for c in result["candidates"]
                    if sorted(c["itemApis"]) == sorted(apis))

    def exclusion(self, core, api):
        return next(e for e in core["exclusions"] if e["itemApi"] == api)

    def completion(self, core, apis):
        return next(c for c in core["completions"]
                    if sorted(c["itemApis"]) == sorted(apis))

    def assertLoss(self, comparison, expected, metric="killTime"):
        self.assertEqual(comparison["status"], "comparable")
        self.assertEqual(comparison["metric"], metric)
        self.assertAlmostEqual(comparison["lossPct"], expected)


class TestCarryCores(CoreFixtures):
    def test_optimal_full_build_is_independent_of_the_selected_flexible_core(self):
        result = self.analyze([
            self.score("ABC"), self.score("DEF", kill=10.2),
            self.score("DEG", kill=10.3),
        ], min_completions=2)
        self.assertEqual(result["optimal"]["itemApis"], list("ABC"))
        self.assertLoss(result["optimal"]["performance"], 0)
        self.assertEqual(result["candidates"][0]["itemApis"], list("DE"))
        self.assertLoss(result["candidates"][0]["best"]["performance"], 2)

    def test_core_strength_flexible_third_items_and_individual_exclusions(self):
        result = self.analyze([
            self.score("ABB"), self.score("ABC", kill=10.2),
            self.score("ABD", kill=10.4), self.score("ACD", kill=11.2),
            self.score("BCD", kill=11.5), self.score("CCD", kill=15.0),
        ])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coreSize"], 2)
        self.assertEqual(result["metric"], "killTime")
        self.assertEqual(result["buildsEvaluated"], 6)
        self.assertEqual(result["nearBuildCount"], 3)
        self.assertEqual(result["qualifyingCoreCount"], 6)
        self.assertEqual({e["itemApi"] for e in result["essentialItems"]}, {"A", "B"})
        self.assertEqual(result["requiredItems"], [])
        core = self.core(result, "AB")
        self.assertEqual(result["candidates"][0], core)
        self.assertEqual(core["items"], ["Item A", "Item B"])
        self.assertEqual(core["nearCount"], 3)
        self.assertEqual(core["totalCount"], 3)
        self.assertEqual(core["best"]["itemApis"], ["A", "B", "B"])
        self.assertEqual(core["best"]["items"], ["Item A", "Item B", "Item B"])
        for apis, flex, penalty in (("ABB", "B", 0), ("ABC", "C", 2),
                                    ("ABD", "D", 4)):
            completion = self.completion(core, apis)
            self.assertEqual(completion["flexApis"], [flex])
            self.assertEqual(completion["flexItems"], ["Item " + flex])
            self.assertLoss(completion["performance"], penalty)
            self.assertLoss(completion["vsCore"], penalty)
        for api, alternative, loss in (("A", "BCD", 15), ("B", "ACD", 12)):
            exclusion = self.exclusion(core, api)
            self.assertTrue(exclusion["essential"])
            self.assertIsNone(exclusion["requiredFor"])
            self.assertEqual(exclusion["without"]["itemApis"], list(alternative))
            self.assertLoss(exclusion["without"]["performance"], loss)

    def test_equal_alternative_build_allows_every_slot_to_change(self):
        result = self.analyze([
            self.score("ABC"), self.score("DEF"),
            self.score("ABD", kill=10.1), self.score("ABE", kill=10.2),
        ])
        self.assertEqual(result["essentialItems"], [])
        core = self.core(result, "AB")
        self.assertEqual(core["nearCount"], 3)
        for api in "AB":
            exclusion = self.exclusion(core, api)
            self.assertFalse(exclusion["essential"])
            self.assertEqual(exclusion["without"]["itemApis"], list("DEF"))
            self.assertLoss(exclusion["without"]["performance"], 0)

    def test_exclusions_use_all_results_even_with_one_displayed_core(self):
        result = self.analyze([
            self.score("ABC"), self.score("ABD", kill=10.1),
            self.score("ABE", kill=10.2), self.score("CDE", kill=10.3),
        ], limit=1)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["nearBuildCount"], 4)
        self.assertEqual(result["essentialItems"], [])
        core = self.core(result, "AB")
        for api in "AB":
            alternative = self.exclusion(core, api)["without"]
            self.assertEqual(alternative["itemApis"], list("CDE"))
            self.assertLoss(alternative["performance"], 3)

    def test_completion_penalty_uses_core_best_and_qualification_uses_global_best(self):
        result = self.analyze([
            self.score("ABC"), self.score("ADE", kill=10.2),
            self.score("ADF", kill=10.3), self.score("ADG", kill=10.6),
        ])
        core = self.core(result, "AD")
        self.assertEqual(core["nearCount"], 2)
        self.assertEqual(core["totalCount"], 3)
        self.assertEqual(len(core["completions"]), 3)
        self.assertLoss(core["best"]["performance"], 2)
        self.assertLoss(self.completion(core, "ADE")["vsCore"], 0)
        third = self.completion(core, "ADG")
        self.assertLoss(third["performance"], 6)
        self.assertLoss(third["vsCore"], 3.92156862745098)

    def test_five_percent_boundary_uses_full_precision(self):
        result = self.analyze([
            self.score("ABC"), self.score("ABD", kill=10.4999999),
            self.score("ABE", kill=10.5), self.score("ABF", kill=10.5000001),
        ])
        self.assertEqual(result["thresholdPct"], 5.0)
        self.assertEqual(result["nearBuildCount"], 3)
        core = self.core(result, "AB")
        self.assertEqual(core["nearCount"], 3)
        self.assertEqual(core["totalCount"], 4)
        self.assertLoss(self.completion(core, "ABE")["performance"], 5)
        self.assertLoss(self.completion(core, "ABF")["performance"], 5.000001)

    def test_item_exactly_at_threshold_is_replaceable(self):
        result = self.analyze([
            self.score("ABC"), self.score("BCD", kill=10.5),
            self.score("ACD", kill=10.5000001),
        ])
        core = self.core(result, "AB")
        self.assertFalse(self.exclusion(core, "A")["essential"])
        self.assertTrue(self.exclusion(core, "B")["essential"])
        self.assertEqual([e["itemApi"] for e in result["essentialItems"]], ["B"])

    def test_custom_threshold_changes_near_counts_and_essentiality(self):
        scores = [self.score("ABC"), self.score("BCD", kill=10.8)]
        strict = self.analyze(scores)
        flexible = self.analyze(scores, threshold_pct=10.0)
        self.assertEqual(strict["nearBuildCount"], 1)
        self.assertEqual(flexible["nearBuildCount"], 2)
        self.assertEqual([e["itemApi"] for e in strict["essentialItems"]], ["A"])
        self.assertEqual(flexible["essentialItems"], [])
        self.assertEqual(flexible["thresholdPct"], 10.0)

    def test_failing_to_clear_is_not_assigned_a_percentage(self):
        result = self.analyze([
            self.score("ABC", kill=20, total=100),
            self.score("ABD", kill=21, total=99),
            self.score("ACD", kill=None, total=100000),
            self.score("BCD", kill=None, total=90000),
        ])
        self.assertEqual(result["nearBuildCount"], 2)
        self.assertEqual(result["essentialItems"], [])
        self.assertEqual({e["itemApi"] for e in result["requiredItems"]}, {"A", "B"})
        core = self.core(result, "AB")
        for api in "AB":
            exclusion = self.exclusion(core, api)
            self.assertFalse(exclusion["essential"])
            self.assertEqual(exclusion["requiredFor"], "clear")
            comparison = exclusion["without"]["performance"]
            self.assertEqual(comparison["status"], "noClear")
            self.assertIsNone(comparison["lossPct"])
            self.assertIsNone(comparison["killTime"])
        incomplete = self.completion(self.core(result, "AC"), "ACD")
        self.assertEqual(incomplete["vsCore"]["status"], "noClear")

    def test_missing_clear_at_fight_limit_does_not_prove_five_percent_loss(self):
        # A clear just before 20 seconds gives no measured penalty for a build
        # still fighting at 20; its unknown clear could be inside the 5% range.
        result = self.analyze([
            self.score("ABC", kill=19.99, total=10000, alive=20),
            self.score("BCD", kill=None, total=9999, alive=20),
        ])
        exclusion = self.exclusion(self.core(result, "AB"), "A")
        self.assertFalse(exclusion["essential"])
        self.assertEqual(exclusion["requiredFor"], "clear")
        self.assertEqual(exclusion["without"]["performance"]["status"], "noClear")
        self.assertIsNone(exclusion["without"]["performance"]["lossPct"])
        self.assertEqual(result["essentialItems"], [])
        self.assertEqual([e["itemApi"] for e in result["requiredItems"]], ["A"])
        self.assertEqual(result["nearBuildCount"], 1)


class TestDuplicateCores(CoreFixtures):
    def test_duplicate_core_counts_builds_once_and_removes_exactly_two_items(self):
        result = self.analyze([
            self.score("AAA"), self.score("AAB", kill=10.1),
            self.score("ABB", kill=10.3), self.score("BCD", kill=12),
        ])
        core = self.core(result, "AA")
        self.assertEqual(core["nearCount"], 2)
        self.assertEqual(core["totalCount"], 2)
        self.assertEqual(len(core["completions"]), 2)
        self.assertEqual(self.completion(core, "AAA")["flexApis"], ["A"])
        self.assertEqual(self.completion(core, "AAB")["flexApis"], ["B"])
        self.assertEqual(len(core["exclusions"]), 1)
        exclusion = self.exclusion(core, "A")
        self.assertEqual(exclusion["copies"], 2)
        self.assertTrue(exclusion["essential"])
        self.assertEqual(exclusion["without"]["itemApis"], list("BCD"))
        self.assertLoss(exclusion["without"]["performance"], 20)
        self.assertFalse(exclusion["copiesEssential"])
        self.assertEqual(exclusion["fewerCopies"]["itemApis"], list("ABB"))
        self.assertLoss(exclusion["fewerCopies"]["performance"], 3)
        self.assertEqual(self.core(result, "AB")["totalCount"], 2)

    def test_second_copy_can_be_essential_independently_of_first_copy(self):
        result = self.analyze([
            self.score("AAA"), self.score("AAB", kill=10.2),
            self.score("ABB", kill=11), self.score("BCD", kill=12),
        ])
        exclusion = self.exclusion(self.core(result, "AA"), "A")
        self.assertTrue(exclusion["copiesEssential"])
        self.assertIsNone(exclusion["copiesRequiredFor"])
        self.assertLoss(exclusion["fewerCopies"]["performance"], 10)
        self.assertLoss(exclusion["without"]["performance"], 20)

    def test_second_copy_required_to_clear_has_no_measured_percent_penalty(self):
        result = self.analyze([
            self.score("AAB", kill=19.99, total=10000, alive=20),
            self.score("ABB", kill=None, total=9999, alive=20),
            self.score("BCD", kill=None, total=9998, alive=20),
        ])
        exclusion = self.exclusion(self.core(result, "AA"), "A")
        self.assertFalse(exclusion["essential"])
        self.assertEqual(exclusion["requiredFor"], "clear")
        self.assertFalse(exclusion["copiesEssential"])
        self.assertEqual(exclusion["copiesRequiredFor"], "clear")
        self.assertEqual(exclusion["fewerCopies"]["itemApis"], list("ABB"))
        self.assertEqual(exclusion["without"]["itemApis"], list("BCD"))
        self.assertIsNone(exclusion["fewerCopies"]["performance"]["lossPct"])
        self.assertEqual(result["essentialItems"], [])

    def test_no_legal_alternative_is_not_evidence_of_essentiality(self):
        # B is unique: AAA and AAB are the only supplied legal builds.
        self.pool = list("AB")
        self.snap.items["B"]["unique"] = True
        result = self.analyze([self.score("AAA"), self.score("AAB", kill=10.2)])
        exclusion = self.exclusion(self.core(result, "AA"), "A")
        self.assertIsNone(exclusion["without"])
        self.assertIsNone(exclusion["fewerCopies"])
        self.assertFalse(exclusion["essential"])
        self.assertFalse(exclusion["copiesEssential"])
        self.assertIsNone(exclusion["requiredFor"])
        self.assertIsNone(exclusion["copiesRequiredFor"])
        self.assertEqual(result["essentialItems"], [])
        self.assertEqual(result["requiredItems"], [])

    def test_analysis_does_not_invent_illegal_or_excluded_item_builds(self):
        # A is unique; C remains in the snapshot but is excluded from the pool.
        self.pool = list("AB")
        self.snap.items["A"]["unique"] = True
        result = self.analyze([self.score("ABB"), self.score("BBB", kill=11)])
        self.assertEqual(result["buildsEvaluated"], 2)
        self.assertEqual(len(result["coreStats"]), 2)
        self.assertEqual({tuple(c["itemApis"]) for c in result["candidates"]},
                         {("A", "B"), ("B", "B")})
        self.assertEqual(self.core(result, "AB")["totalCount"], 1)
        self.assertEqual(self.core(result, "BB")["totalCount"], 2)


class TestDamageAndEmptyCores(CoreFixtures):
    def test_nonfinishers_and_fighters_compare_damage_and_use_two_item_cores(self):
        for objective in ("carry", "fighter"):
            with self.subTest(objective=objective):
                result = self.analyze([
                    self.score("ABC", kill=None, total=1000, alive=5),
                    self.score("ABD", kill=None, total=950, alive=30),
                    self.score("ACD", kill=None, total=949, alive=30),
                ], objective=objective)
                self.assertEqual(result["metric"], "damage")
                self.assertEqual(result["coreSize"], 2)
                self.assertEqual(result["nearBuildCount"], 2)
                core = self.core(result, "AB")
                self.assertLoss(self.completion(core, "ABD")["performance"], 5, "damage")
                exclusion = self.exclusion(core, "B")
                self.assertTrue(exclusion["essential"])
                self.assertLoss(exclusion["without"]["performance"], 5.1, "damage")

    def test_zero_damage_does_not_produce_essential_items(self):
        for objective in ("carry", "fighter"):
            with self.subTest(objective=objective):
                result = self.analyze([
                    self.score("ABC", kill=None, total=0),
                    self.score("ABD", kill=None, total=0),
                ], objective=objective)
                self.assertEqual(result["status"], "noDamage")
                self.assertEqual(result["buildsEvaluated"], 2)
                self.assertEqual(result["essentialItems"], [])
                self.assertEqual(result["requiredItems"], [])
                self.assertEqual(result["nearBuildCount"], 0)
                self.assertEqual(result["candidates"], [])
                self.assertEqual(len(result["coreStats"]), 5)
                for stat in result["coreStats"].values():
                    self.assertEqual(stat["best"]["status"], "unavailable")
                    self.assertIsNone(stat["best"]["lossPct"])
                    self.assertEqual(stat["nearCount"], 0)

    def test_empty_input_returns_an_explicit_empty_analysis(self):
        for objective, core_size in (("carry", 2), ("fighter", 2), ("tank", 1)):
            with self.subTest(objective=objective):
                result = self.analyze([], objective=objective)
                self.assertEqual(result["status"], "empty")
                self.assertEqual(result["coreSize"], core_size)
                self.assertEqual(result["buildsEvaluated"], 0)
                self.assertEqual(result["nearBuildCount"], 0)
                self.assertEqual(result["qualifyingCoreCount"], 0)
                self.assertEqual(result["essentialItems"], [])
                self.assertEqual(result["requiredItems"], [])
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["coreStats"], {})


class TestTankCores(CoreFixtures):
    def test_tank_single_item_core_uses_survival_and_two_flexible_items(self):
        result = self.analyze([
            self.score("AAA", kill=None, alive=20, total=100),
            self.score("ABC", kill=None, alive=19, total=1000),
            self.score("BCD", kill=None, alive=18.999, total=10000),
            self.score("DEF", kill=None, alive=10, total=100000),
        ], objective="tank")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coreSize"], 1)
        self.assertEqual(result["metric"], "aliveTime")
        self.assertEqual(result["nearBuildCount"], 2)
        core = self.core(result, "A")
        self.assertEqual(core["nearCount"], 2)
        self.assertEqual(core["totalCount"], 2)
        self.assertEqual(self.completion(core, "AAA")["flexApis"], ["A", "A"])
        self.assertEqual(self.completion(core, "ABC")["flexApis"], ["B", "C"])
        self.assertLoss(self.completion(core, "ABC")["performance"], 5, "aliveTime")
        exclusion = self.exclusion(core, "A")
        self.assertTrue(exclusion["essential"])
        self.assertEqual(exclusion["without"]["itemApis"], list("BCD"))
        self.assertLoss(exclusion["without"]["performance"], 5.005, "aliveTime")

    def test_regular_capped_tanks_compare_stress_survival(self):
        result = self.analyze([
            self.score("AAA", kill=None, capped=True, stress=20),
            self.score("ABC", kill=None, capped=True, stress=19),
            self.score("BCD", kill=None, capped=True, stress=18.9),
            self.score("DEF", kill=None, alive=29.9999, total=100000),
        ], objective="tank")
        self.assertEqual(result["metric"], "stressAliveTime")
        self.assertEqual(result["nearBuildCount"], 2)
        core = self.core(result, "A")
        self.assertLoss(self.completion(core, "ABC")["performance"], 5, "stressAliveTime")
        self.assertLoss(self.exclusion(core, "A")["without"]["performance"],
                        5.5, "stressAliveTime")
        completion = self.completion(self.core(result, "B"), "BCD")
        self.assertLoss(completion["vsCore"], 0.5263157894736842, "stressAliveTime")
        self.assertEqual(len(result["coreStats"]), 6)
        short = [s for s in result["coreStats"].values()
                 if s["best"]["status"] == "shortSurvival"]
        self.assertEqual(len(short), 2)  # E and F only occur in the uncapped build.
        self.assertTrue(all(s["best"]["lossPct"] is None for s in short))
        self.assertTrue(all(s["nearCount"] == 0 for s in short))

    def test_failing_regular_cap_is_not_a_small_percentage_stress_loss(self):
        result = self.analyze([
            self.score("ABC", kill=None, capped=True, stress=20),
            self.score("ABD", kill=None, capped=True, stress=19),
            self.score("CDE", kill=None, alive=29.9999),
        ], objective="tank")
        exclusion = self.exclusion(self.core(result, "A"), "A")
        self.assertFalse(exclusion["essential"])
        self.assertEqual(exclusion["requiredFor"], "survivalLimit")
        self.assertEqual(result["essentialItems"], [])
        self.assertEqual([e["itemApi"] for e in result["requiredItems"]], ["A", "B"])
        comparison = exclusion["without"]["performance"]
        self.assertEqual(comparison["status"], "shortSurvival")
        self.assertIsNone(comparison["lossPct"])
        self.assertFalse(comparison["survivalCapped"])

    def test_surviving_both_caps_is_a_tie_and_cap_failures_have_no_percentage(self):
        result = self.analyze([
            self.score("AAA", kill=None, total=0, capped=True, stress=30,
                       stress_capped=True),
            self.score("ABC", kill=None, total=0, capped=True, stress=30,
                       stress_capped=True),
            self.score("BCD", kill=None, total=1000, capped=True, stress=29.999),
            self.score("DEF", kill=None, total=10000, alive=29.999),
        ], objective="tank")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["metric"], "capped")
        self.assertEqual(result["nearBuildCount"], 2)
        core = self.core(result, "A")
        self.assertLoss(self.completion(core, "ABC")["performance"], 0, "capped")
        exclusion = self.exclusion(core, "A")
        self.assertFalse(exclusion["essential"])
        self.assertEqual(exclusion["requiredFor"], "bothSurvivalLimits")
        self.assertEqual(result["essentialItems"], [])
        self.assertEqual([e["itemApi"] for e in result["requiredItems"]], ["A"])
        self.assertEqual(exclusion["without"]["performance"]["status"], "belowCap")
        self.assertIsNone(exclusion["without"]["performance"]["lossPct"])
        below = [s for s in result["coreStats"].values()
                 if s["best"]["status"] == "belowCap"]
        self.assertEqual(len(below), 3)  # D, E, F.
        self.assertTrue(all(s["best"]["lossPct"] is None for s in below))

    def test_missing_sixty_second_stress_cap_does_not_prove_five_percent_loss(self):
        # The baseline's true survival is censored at 60 seconds. A 59.999
        # second alternative fails that outcome without establishing a >5% gap.
        result = self.analyze([
            self.score("ABC", kill=None, alive=60, capped=True, stress=60,
                       stress_capped=True),
            self.score("BCD", kill=None, alive=60, capped=True, stress=59.999),
        ], objective="tank")
        exclusion = self.exclusion(self.core(result, "A"), "A")
        self.assertFalse(exclusion["essential"])
        self.assertEqual(exclusion["requiredFor"], "bothSurvivalLimits")
        self.assertEqual(exclusion["without"]["performance"]["status"], "belowCap")
        self.assertIsNone(exclusion["without"]["performance"]["lossPct"])
        self.assertEqual(result["essentialItems"], [])
        self.assertEqual([e["itemApi"] for e in result["requiredItems"]], ["A"])
        self.assertEqual(result["nearBuildCount"], 1)


class TestCoreOrdering(CoreFixtures):
    def test_more_near_optimal_completions_rank_ahead_of_a_faster_best_build(self):
        result = self.analyze([
            self.score("ABC"), self.score("DEF", kill=10.1),
            self.score("DEG", kill=10.2), self.score("DEH", kill=10.3),
            self.score("ABF", kill=10.4),
        ], limit=2)
        self.assertEqual([c["itemApis"] for c in result["candidates"]],
                         [["D", "E"], ["A", "B"]])
        self.assertEqual([c["nearCount"] for c in result["candidates"]], [3, 2])

    def test_performance_ties_sort_by_stable_item_identity(self):
        self.pool = list("DBAC")
        # Names and input order intentionally disagree with stable API order.
        for api, name in zip("ABCD", ("Zulu", "Yankee", "Xray", "Whiskey")):
            self.snap.items[api]["name"] = name
        result = self.analyze([self.score("ABD"), self.score("ABC")])
        self.assertEqual([sorted(c["itemApis"]) for c in result["candidates"]],
                         [list("AB"), list("AC"), list("AD"), list("BC"), list("BD")])

    def test_candidate_limit_keeps_stats_for_every_core(self):
        scores = [self.score("ABC"), self.score("DEF"), self.score("GHI")]
        result = self.analyze(scores)
        all_candidates = self.analyze(scores, limit=20)
        self.assertEqual(len(result["candidates"]), 6)
        self.assertEqual(result["qualifyingCoreCount"], 9)
        self.assertEqual(len(result["coreStats"]), 9)
        self.assertEqual(result["coreStats"], all_candidates["coreStats"])
        self.assertEqual([c["id"] for c in result["candidates"]],
                         [c["id"] for c in all_candidates["candidates"][:6]])
        for core in all_candidates["candidates"]:
            stat = result["coreStats"][core["id"]]
            self.assertEqual(stat["nearCount"], 1)
            self.assertEqual(stat["totalCount"], 1)
            self.assertEqual(stat["best"], core["best"]["performance"])


if __name__ == "__main__":
    unittest.main()
