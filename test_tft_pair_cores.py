"""Measured two-item spikes choose distinct, competitive carry foundations.

Run: python3 -m unittest test_tft_pair_cores -v
Hand-computed scores need neither the compiled engine nor a snapshot download.
"""

import itertools
from types import SimpleNamespace
import unittest

import tft


class PairCoreFixtures(unittest.TestCase):
    def setUp(self):
        self.pool = list("ABCDEFGHI")
        self.snap = SimpleNamespace(items={api: {"name": "Item " + api}
                                          for api in self.pool})

    def score(self, items, *, kill=10.0, total=10000.0, alive=30.0,
              capped=False, stress=None, stress_capped=False):
        indices = tuple(sorted(self.pool.index(api) for api in items))
        return indices, kill, total, alive, capped, stress, stress_capped

    def pairs(self, scores, overrides=None, *, default_kill=30.0,
              default_total=1000.0):
        """Supply every distinct pair, including pairs of duplicate items.

        Overrides may also add legal pairs absent from the three-item fixture;
        those must still participate in the global two-item comparison.
        """
        required = {tuple(sorted(self.pool[i] for i in pair))
                    for score in scores
                    for pair in itertools.combinations(score[0], 2)}
        overrides = {tuple(sorted(apis)): values
                     for apis, values in (overrides or {}).items()}
        pairs = []
        for apis in sorted(required | overrides.keys()):
            values = dict(kill=default_kill, total=default_total)
            values.update(overrides.get(apis, {}))
            pairs.append(self.score(apis, **values))
        return sorted(pairs, key=lambda score: (
            score[1] is None,
            score[1] if score[1] is not None else -score[2],
            tuple(sorted(self.pool[i] for i in score[0])),
        ))

    def analyze(self, scores, pair_scores, objective="carry", **kwargs):
        # Isolate ranking/grouping from the eligibility threshold; the
        # default two-choice rule has its own cases below.
        kwargs.setdefault("min_completions", 1)
        return tft.analyze_cores(self.snap, {"objective": objective}, self.pool,
                                 scores, pair_scores=pair_scores, **kwargs)

    def identities(self, result):
        return ["".join(sorted(core["itemApis"])) for core in result["candidates"]]

    def core(self, result, apis):
        return next(core for core in result["candidates"]
                    if sorted(core["itemApis"]) == sorted(apis))

    def assertLoss(self, comparison, expected, metric="killTime"):
        self.assertEqual(comparison["status"], "comparable")
        self.assertEqual(comparison["metric"], metric)
        self.assertAlmostEqual(comparison["lossPct"], expected)

    def assertDistinctNearCompletions(self, result):
        seen = set()
        for core in result["candidates"]:
            near = {tuple(sorted(completion["itemApis"]))
                    for completion in core["completions"]
                    if completion["performance"]["status"] == "comparable"
                    and completion["performance"]["lossPct"] <= result["thresholdPct"] + 1e-9}
            self.assertTrue(near)
            self.assertTrue(seen.isdisjoint(near))
            seen.update(near)


class TestMeasuredPairSelection(PairCoreFixtures):
    def test_one_full_build_selects_its_strongest_actual_pair(self):
        scores = [self.score("ABC", kill=10, total=10000)]
        pair_scores = self.pairs(scores, {
            "AB": {"kill": 18, "total": 300},
            "AC": {"kill": 12, "total": 400},
            "BC": {"kill": 15, "total": 350},
        })
        result = self.analyze(scores, pair_scores)
        self.assertEqual(self.identities(result), ["AC"])
        self.assertEqual(result["selectionMode"], "twoItemSpike")
        self.assertEqual(result["pairBuildsEvaluated"], 3)
        self.assertEqual(result["qualifyingCoreCount"], 3)
        self.assertEqual(result["groupedCoreCount"], 1)
        core = self.core(result, "AC")
        self.assertEqual(core["spike"]["killTime"], 12)
        self.assertEqual(core["spike"]["total"], 400)
        self.assertLoss(core["spike"], 0)
        self.assertEqual(core["best"]["performance"]["killTime"], 10)
        self.assertEqual(core["best"]["performance"]["total"], 10000)
        self.assertEqual(core["completions"][0]["flexApis"], ["B"])
        self.assertEqual(set(result["coreStats"]), {"A|B", "A|C", "B|C"})

    def test_spike_can_beat_a_more_flexible_core_with_a_better_full_build(self):
        scores = [self.score("ABC"), self.score("ABF", kill=10.1),
                  self.score("ABG", kill=10.2), self.score("DEH", kill=10.3),
                  self.score("DEI", kill=10.4)]
        pairs = self.pairs(scores, {"AB": {"kill": 20}, "DE": {"kill": 12}})
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["DE", "AB"])
        self.assertEqual(result["nearBuildCount"], 5)
        self.assertEqual(self.core(result, "DE")["nearCount"], 2)
        self.assertEqual(self.core(result, "AB")["nearCount"], 3)
        self.assertLoss(self.core(result, "DE")["best"]["performance"], 3)
        self.assertLoss(self.core(result, "AB")["best"]["performance"], 0)
        self.assertLoss(self.core(result, "AB")["spike"], 100 * (20 - 12) / 12)
        self.assertDistinctNearCompletions(result)

    def test_globally_best_pair_with_bad_completions_is_not_recommended(self):
        scores = [self.score("ABC"), self.score("DEF", kill=15)]
        pairs = self.pairs(scores, {"AB": {"kill": 20}, "DE": {"kill": 5}})
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["AB"])
        self.assertEqual(result["nearBuildCount"], 1)
        self.assertEqual(result["qualifyingCoreCount"], 3)
        self.assertEqual(result["groupedCoreCount"], 1)
        self.assertEqual(result["pairBuildsEvaluated"], 6)
        self.assertEqual(result["coreStats"]["D|E"]["nearCount"], 0)
        self.assertLoss(self.core(result, "AB")["spike"], 300)
        self.assertLoss(self.core(result, "AB")["best"]["performance"], 0)

    def test_pair_baseline_includes_supplied_pairs_absent_from_full_fixture(self):
        scores = [self.score("ABC")]
        pairs = self.pairs(scores, {"AB": {"kill": 20}, "DE": {"kill": 10}})
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["AB"])
        self.assertEqual(result["pairBuildsEvaluated"], 4)
        self.assertNotIn("D|E", result["coreStats"])
        self.assertLoss(self.core(result, "AB")["spike"], 100)

    def test_nonclearing_pairs_use_their_actual_damage_even_when_full_builds_clear(self):
        for objective in ("carry", "fighter"):
            with self.subTest(objective=objective):
                scores = [self.score("ABC", total=10000),
                          self.score("DEF", kill=10.1, total=9900)]
                pairs = self.pairs(scores, {
                    "AB": {"total": 700}, "DE": {"total": 1000},
                }, default_kill=None, default_total=100)
                result = self.analyze(scores, pairs, objective=objective)
                self.assertEqual(self.identities(result), ["DE", "AB"])
                self.assertEqual(result["metric"], "killTime")
                core = self.core(result, "AB")
                self.assertIsNone(core["spike"]["killTime"])
                self.assertEqual(core["spike"]["total"], 700)
                self.assertLoss(core["spike"], 30, "damage")
                self.assertEqual(core["best"]["performance"]["total"], 10000)
                self.assertLoss(core["best"]["performance"], 0)

    def test_clearing_pair_beats_nonclear_and_does_not_invent_a_loss_percent(self):
        scores = [self.score("ABC"), self.score("DEF", kill=10.1)]
        pairs = self.pairs(scores, {
            "AB": {"total": 999999}, "DE": {"kill": 29, "total": 100},
        }, default_kill=None, default_total=1000)
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["DE", "AB"])
        self.assertLoss(self.core(result, "DE")["spike"], 0)
        spike = self.core(result, "AB")["spike"]
        self.assertEqual(spike["status"], "noClear")
        self.assertEqual(spike["metric"], "killTime")
        self.assertIsNone(spike["killTime"])
        self.assertIsNone(spike["lossPct"])
        self.assertEqual(spike["total"], 999999)


class TestDistinctPairFamilies(PairCoreFixtures):
    def test_pairs_sharing_one_item_can_be_separate_families(self):
        scores = [self.score("ABC"), self.score("ADE", kill=10.1)]
        pairs = self.pairs(scores, {"AB": {"kill": 12}, "AD": {"kill": 14}})
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["AB", "AD"])
        self.assertEqual(result["groupedCoreCount"], 2)
        self.assertDistinctNearCompletions(result)

    def test_shared_bad_completion_does_not_collapse_competitive_families(self):
        scores = [self.score("ABD"), self.score("ACE", kill=10.1),
                  self.score("ABC", kill=12)]
        pairs = self.pairs(scores, {"AB": {"kill": 12}, "AC": {"kill": 14}})
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["AB", "AC"])
        self.assertEqual(result["nearBuildCount"], 2)
        for apis in ("AB", "AC"):
            core = self.core(result, apis)
            self.assertEqual(core["totalCount"], 2)
            bad = next(c for c in core["completions"]
                       if sorted(c["itemApis"]) == list("ABC"))
            self.assertLoss(bad["performance"], 20)
        self.assertDistinctNearCompletions(result)

    def test_grouping_does_not_merge_through_a_suppressed_pair(self):
        # BC bridges ABC and BCD, but losing BC to AB must not hide CD.
        scores = [self.score("ABC"), self.score("BCD", kill=10.1)]
        pairs = self.pairs(scores, {
            "AB": {"kill": 12}, "BC": {"kill": 13}, "CD": {"kill": 14},
        })
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["AB", "CD"])
        self.assertEqual(result["qualifyingCoreCount"], 5)
        self.assertEqual(result["groupedCoreCount"], 2)
        self.assertEqual(result["coreStats"]["B|C"]["nearCount"], 2)
        self.assertEqual(result["coreStats"]["B|C"]["totalCount"], 2)
        self.assertDistinctNearCompletions(result)

    def test_duplicate_item_build_is_counted_once_and_keeps_the_correct_flex_item(self):
        for winner, flex in (("AA", "B"), ("AB", "A")):
            with self.subTest(winner=winner):
                scores = [self.score("AAB")]
                pairs = self.pairs(scores, {winner: {"kill": 12}})
                result = self.analyze(scores, pairs)
                self.assertEqual(self.identities(result), [winner])
                self.assertEqual(result["pairBuildsEvaluated"], 2)
                self.assertEqual(result["qualifyingCoreCount"], 2)
                self.assertEqual(result["groupedCoreCount"], 1)
                self.assertEqual(set(result["coreStats"]), {"A|A", "A|B"})
                self.assertEqual(result["coreStats"]["A|B"]["totalCount"], 1)
                core = self.core(result, winner)
                self.assertEqual(core["nearCount"], 1)
                self.assertEqual(core["totalCount"], 1)
                self.assertEqual(core["completions"][0]["flexApis"], [flex])
                self.assertEqual(sum(e["copies"] for e in core["exclusions"]), 2)

    def test_display_limit_preserves_group_count_and_all_full_build_statistics(self):
        scores = [self.score("ABC"), self.score("ABD", kill=10.1),
                  self.score("DEF", kill=10.2), self.score("GHI", kill=10.3),
                  self.score("ABE", kill=12)]
        pairs = self.pairs(scores, {
            "AB": {"kill": 12}, "DE": {"kill": 14}, "GH": {"kill": 16},
        })
        limited = self.analyze(scores, pairs, limit=1)
        full = self.analyze(scores, pairs, limit=20)
        legacy = self.analyze(scores, None, limit=20)
        self.assertEqual(self.identities(limited), ["AB"])
        self.assertEqual(self.identities(full), ["AB", "DE", "GH"])
        self.assertEqual(limited["groupedCoreCount"], 3)
        self.assertEqual(full["groupedCoreCount"], 3)
        for key in ("buildsEvaluated", "nearBuildCount", "qualifyingCoreCount",
                    "coreStats", "essentialItems", "requiredItems", "metric"):
            self.assertEqual(limited[key], full[key], key)
            self.assertEqual(limited[key], legacy[key], key)
        selected = self.core(limited, "AB")
        prior = self.core(legacy, "AB")
        for key in ("best", "completions", "exclusions", "nearCount", "totalCount"):
            self.assertEqual(selected[key], prior[key], key)
        self.assertEqual(selected["totalCount"], 3)
        self.assertEqual(selected["nearCount"], 2)
        self.assertDistinctNearCompletions(full)


class TestPairOrderingAndCoverage(PairCoreFixtures):
    def test_equal_pair_clear_times_prefer_more_near_completions_before_damage(self):
        scores = [self.score("ABC"), self.score("DEF", kill=10.1),
                  self.score("DEG", kill=10.2)]
        pairs = self.pairs(scores, {
            "AB": {"kill": 20, "total": 100000},
            "DE": {"kill": 20, "total": 100},
        })
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["DE", "AB"])

    def test_equal_spike_and_flexibility_prefer_the_better_full_build(self):
        scores = [self.score("ABC"), self.score("DEF", kill=10.2)]
        pairs = self.pairs(scores, {
            "AB": {"kill": 20, "total": 100},
            "DE": {"kill": 20, "total": 100000},
        })
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["AB", "DE"])

    def test_primary_ties_use_stable_item_apis_despite_pool_and_name_order(self):
        for api, name in zip("ABC", ("Zulu", "Yankee", "Alpha")):
            self.snap.items[api]["name"] = name
        for pool in ("ABC", "CBA", "BAC"):
            with self.subTest(pool=pool):
                self.pool = list(pool)
                scores = [self.score("ABC")]
                pairs = self.pairs(scores, default_kill=20)
                result = self.analyze(scores, pairs)
                self.assertEqual(self.identities(result), ["AB"])
                self.assertEqual(result["candidates"][0]["id"], "A|B")

    def test_explicit_missing_or_partial_pair_data_raises(self):
        scores = [self.score("ABC"), self.score("DEF", kill=15)]
        complete = self.pairs(scores)
        missing_nonqualifying = [score for score in complete
                                 if sorted(self.pool[i] for i in score[0]) != list("EF")]
        for pair_scores in ([], [self.score("AB", kill=20)], missing_nonqualifying):
            with self.subTest(pair_count=len(pair_scores)):
                with self.assertRaises(ValueError):
                    self.analyze(scores, pair_scores)

    def test_none_retains_full_build_based_compatibility_without_measured_spikes(self):
        scores = [self.score("ABC"), self.score("ABD", kill=10.1)]
        result = self.analyze(scores, None)
        self.assertEqual(self.identities(result)[0], "AB")
        self.assertEqual(len(result["candidates"]), 5)
        self.assertNotEqual(result.get("selectionMode"), "twoItemSpike")
        self.assertTrue(all(core.get("spike") is None for core in result["candidates"]))

    def test_tank_single_item_recommendations_do_not_require_or_infer_pair_spikes(self):
        scores = [self.score("AAA", kill=None, alive=20),
                  self.score("ABC", kill=None, alive=19.5),
                  self.score("BCD", kill=None, alive=18)]
        analyses = [self.analyze(scores, pair_scores, objective="tank")
                    for pair_scores in (None, [], [self.score("BC", kill=1)])]
        baseline = analyses[0]
        self.assertEqual(baseline["coreSize"], 1)
        self.assertEqual(baseline["metric"], "aliveTime")
        self.assertEqual(self.identities(baseline), ["A", "B", "C"])
        for result in analyses:
            self.assertNotEqual(result.get("selectionMode"), "twoItemSpike")
            self.assertEqual(result["candidates"], baseline["candidates"])
            self.assertEqual(result["coreStats"], baseline["coreStats"])
            self.assertTrue(all(core.get("spike") is None for core in result["candidates"]))


class TestMinimumCompletions(PairCoreFixtures):
    def test_one_choice_pair_cannot_suppress_a_flexible_pair(self):
        scores = [self.score("ABC"), self.score("ACD", kill=10.2)]
        pairs = self.pairs(scores, {"AB": {"kill": 11}, "AC": {"kill": 15}})
        for objective in ("carry", "fighter"):
            with self.subTest(objective=objective):
                result = tft.analyze_cores(self.snap, {"objective": objective}, self.pool,
                                           scores, pair_scores=pairs)
                self.assertEqual(result["minCompletions"], 2)
                self.assertEqual(self.identities(result), ["AC"])
                self.assertEqual(result["qualifyingCoreCount"], 1)
                self.assertEqual(result["groupedCoreCount"], 1)
                self.assertEqual(result["coreStats"]["A|B"]["nearCount"], 1)
                self.assertEqual(self.core(result, "AC")["nearCount"], 2)

    def test_no_flexible_pair_does_not_fall_back_to_single_completion_cores(self):
        scores = [self.score("ABC")]
        for pairs in (None, self.pairs(scores)):
            with self.subTest(pair_scores=pairs):
                result = tft.analyze_cores(self.snap, {"objective": "carry"}, self.pool,
                                           scores, pair_scores=pairs)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["optimal"]["itemApis"], list("ABC"))
                self.assertEqual(result["optimal"]["performance"]["lossPct"], 0)
                self.assertEqual(result["qualifyingCoreCount"], 0)
                self.assertEqual(result["nearBuildCount"], 1)
                self.assertEqual(len(result["coreStats"]), 3)

    def test_two_distinct_finishers_qualify_at_the_exact_five_percent_boundary(self):
        scores = [self.score("AAB"), self.score("AAC", kill=10.5),
                  self.score("AAD", kill=10.5000001)]
        result = tft.analyze_cores(self.snap, {"objective": "carry"}, self.pool,
                                   scores, pair_scores=self.pairs(scores))
        self.assertEqual(self.identities(result), ["AA"])
        core = self.core(result, "AA")
        self.assertEqual(core["nearCount"], 2)
        self.assertEqual(core["totalCount"], 3)
        self.assertEqual([c["flexApis"] for c in core["completions"]], [["B"], ["C"], ["D"]])
        # The repeated A in AAB does not create two choices for the AB core.
        self.assertEqual(result["coreStats"]["A|B"]["nearCount"], 1)

    def test_tanks_still_allow_one_completion(self):
        scores = [self.score("ABC", kill=None, alive=30)]
        result = tft.analyze_cores(self.snap, {"objective": "tank"}, self.pool, scores)
        self.assertEqual(result["minCompletions"], 1)
        self.assertEqual(self.identities(result), ["A", "B", "C"])
        self.assertTrue(all(c["nearCount"] == 1 for c in result["candidates"]))


if __name__ == "__main__":
    unittest.main()
