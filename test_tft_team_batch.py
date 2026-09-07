"""Exact native batch integration, compact reporting and cache isolation."""
from copy import deepcopy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import tft
import tft_board
import tft_team as team
import tft_match_cache
from tft_comp_traits import resolve_board_traits


class TestPreparedTeamEvaluation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.board = team.opponent_suite(cls.snap, budget=9)[0]

    def allocation(self, item):
        selected = deepcopy(self.board["selected"])
        selected[self.board["carry"]]["items"] = (item,) * 3
        return selected

    def evaluate_many(self, evaluator, selections, **kwargs):
        b = self.board
        return evaluator.evaluate_many(b["members"], b["effects"], selections, b["carry"], b["tank"], **kwargs)

    def test_native_batch_matches_scalar_full_results_and_preserves_duplicates(self):
        selections = [self.board["selected"], self.allocation("DA_HextechGunblade"),
                      self.allocation("DA_RabadonsDeathcap"), self.board["selected"]]
        for geometry in ("clump", "spread"):
            with self.subTest(geometry=geometry):
                scalar = team.Evaluator(self.snap, geometry, prepared=False)
                native = team.Evaluator(self.snap, geometry)
                expected = self.evaluate_many(scalar, selections, details=True)
                actual = self.evaluate_many(native, selections, details=True)
                self.assertEqual(actual, expected)
                self.assertIs(actual[0], actual[3])
                self.assertEqual(native.stats["sharedFightsSimulated"], 36)
                self.assertEqual(native.stats["nativeMatchBatches"], 1)
                self.assertGreater(native.stats["preparedActorsReused"], 0)

    def test_compact_results_preserve_all_fight_metrics_and_full_requests_stay_full(self):
        evaluator = team.Evaluator(self.snap, "clump")
        selections = [self.allocation("DA_HextechGunblade")]
        compact = self.evaluate_many(evaluator, selections)[0]
        full = self.evaluate_many(evaluator, selections, details=True)[0]
        self.assertEqual(compact, dict(full, units={}))
        self.assertEqual(len(full["units"]), 8)
        self.assertIs(self.evaluate_many(evaluator, selections)[0], compact)
        self.assertIs(self.evaluate_many(evaluator, selections, details=True)[0], full)
        self.assertEqual(evaluator.stats["sharedFightsSimulated"], 24)

    def test_result_eviction_during_a_batch_preserves_all_input_ordered_answers(self):
        selections = [self.board["selected"], self.allocation("DA_HextechGunblade"),
                      self.allocation("DA_RabadonsDeathcap"), self.board["selected"]]
        expected = self.evaluate_many(team.Evaluator(self.snap, "spread", prepared=False), selections)
        evaluator = team.Evaluator(self.snap, "spread")
        with patch.object(team, "RESULT_CACHE_LIMIT", 1), patch.object(team, "ALLOCATION_BATCH_SIZE", 1):
            actual = self.evaluate_many(evaluator, selections)
        self.assertEqual(actual, expected)
        self.assertEqual(len(evaluator.results), 1)
        self.assertEqual(evaluator.stats["sharedFightsSimulated"], 36)

    def test_prepared_handles_are_immutable_across_policies_and_item_changes(self):
        native = team.Evaluator(self.snap, "clump")
        scalar = team.Evaluator(self.snap, "clump", prepared=False)
        selections = [self.board["selected"], self.allocation("DA_HextechGunblade")]
        before = deepcopy(selections)
        for split, subset, policy in (("search", None, "broad"), ("search", "screen", "broad"),
                                      ("validation", None, "restricted"), ("validation", None, "broad")):
            with self.subTest(split=split, subset=subset, policy=policy):
                options = dict(split=split, subset=subset, healing_policy=policy, details=True)
                self.assertEqual(self.evaluate_many(native, selections, **options),
                                 self.evaluate_many(scalar, selections, **options))
        self.assertEqual(selections, before)

    def test_prepared_actor_cache_is_bounded_and_eviction_does_not_change_results(self):
        selections = [self.board["selected"], self.allocation("DA_HextechGunblade")]
        expected = self.evaluate_many(team.Evaluator(self.snap, "clump", prepared=False), selections)
        evaluator = team.Evaluator(self.snap, "clump")
        with patch.object(team, "PREPARED_ACTOR_CACHE_LIMIT", 1):
            actual = self.evaluate_many(evaluator, selections)
        self.assertEqual(actual, expected)
        self.assertEqual(len(evaluator._prepared_actors), 1)

    def test_incomplete_native_batch_cannot_publish_partial_cached_scores(self):
        evaluator = team.Evaluator(self.snap, "clump", prepared=False)
        with patch.object(evaluator, "_simulate_matches", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                self.evaluate_many(evaluator, [self.board["selected"]])
        self.assertEqual(evaluator.results, {})

    def test_persisted_scores_replay_exactly_and_never_replace_full_diagnostics(self):
        selections = [self.board["selected"], self.allocation("DA_HextechGunblade")]
        with TemporaryDirectory() as directory:
            first = team.Evaluator(self.snap, "clump", prepared=False, score_cache_dir=directory)
            expected = self.evaluate_many(first, selections)
            self.assertEqual(first.stats["sharedFightsSimulated"], 24)
            first._score_cache.close()
            second = team.Evaluator(self.snap, "clump", prepared=False, score_cache_dir=directory)
            with patch.object(second, "_simulate_matches", side_effect=AssertionError("cached fights were rerun")):
                self.assertEqual(self.evaluate_many(second, selections), expected)
            self.assertEqual(second.stats["sharedFightsSimulated"], 0)
            self.assertEqual(second.stats["cachedFightsReused"], 24)
            full = self.evaluate_many(second, selections, details=True)
            self.assertEqual(second.stats["sharedFightsSimulated"], 24)
            self.assertEqual([dict(row, units={}) for row in full], expected)
            self.assertTrue(all(len(row["units"]) == 8 for row in full))
            second._score_cache.close()

    def test_persistent_key_separates_geometry_policies_splits_and_positions(self):
        selection = self.allocation("DA_HextechGunblade")
        with TemporaryDirectory() as directory:
            first = team.Evaluator(self.snap, "clump", prepared=False, score_cache_dir=directory)
            self.evaluate_many(first, [selection])
            first._score_cache.close()
            changed = team.Evaluator(self.snap, "spread", prepared=False, score_cache_dir=directory)
            self.evaluate_many(changed, [selection])
            self.assertEqual(changed.stats["cachedFightsReused"], 0)
            changed._score_cache.close()
            second = team.Evaluator(self.snap, "clump", prepared=False, score_cache_dir=directory)
            self.evaluate_many(second, [selection], healing_policy="restricted")
            self.evaluate_many(second, [selection], split="validation")
            self.evaluate_many(second, [selection], subset="screen")
            b = self.board
            other_tank = next(m["api"] for m in b["members"] if m["api"] not in (b["carry"], b["tank"]))
            second.evaluate_many(b["members"], b["effects"], [selection], b["carry"], other_tank)
            self.assertEqual(second.stats["cachedFightsReused"], 0)
            self.assertEqual(second.stats["sharedFightsSimulated"], 36)
            second._score_cache.close()

    def test_cached_wrong_fight_count_is_rejected_and_recomputed(self):
        with TemporaryDirectory() as directory:
            first = team.Evaluator(self.snap, "clump", prepared=False, score_cache_dir=directory)
            expected = self.evaluate_many(first, [self.board["selected"]])
            connection = first._score_cache.connection
            key, blob = connection.execute("SELECT signature,result FROM scores").fetchone()
            short = tft_match_cache.encode(key, tft_match_cache.decode(key, blob)[:6])
            with connection:
                connection.execute("UPDATE scores SET result=? WHERE signature=?", (short, key))
            first._score_cache.close()
            second = team.Evaluator(self.snap, "clump", prepared=False, score_cache_dir=directory)
            self.assertEqual(self.evaluate_many(second, [self.board["selected"]]), expected)
            self.assertEqual(second.stats["cachedFightsReused"], 0)
            self.assertEqual(second.stats["scoreCacheCorruptRows"], 1)
            self.assertEqual(second.stats["sharedFightsSimulated"], 12)
            key, blob = second._score_cache.connection.execute("SELECT signature,result FROM scores").fetchone()
            self.assertEqual(len(tft_match_cache.decode(key, blob)), 12)
            second._score_cache.close()

    def test_persistent_inputs_include_resolved_stats_even_with_same_archive_stamp(self):
        with TemporaryDirectory() as directory:
            first = team.Evaluator(self.snap, "clump", prepared=False, score_cache_dir=directory)
            self.evaluate_many(first, [self.board["selected"]])
            first._score_cache.close()
            changed = deepcopy(self.snap)
            changed.units[self.board["carry"]]["stats"]["ad"] += 100
            self.assertEqual(changed.hash_inputs(), self.snap.hash_inputs())
            second = team.Evaluator(changed, "clump", prepared=False, score_cache_dir=directory)
            self.evaluate_many(second, [self.board["selected"]])
            self.assertEqual(second.stats["cachedFightsReused"], 0)
            self.assertEqual(second.stats["sharedFightsSimulated"], 12)
            second._score_cache.close()

    def capped_candidate(self, added="TFT18_Gnar"):
        board = self.board
        members = deepcopy(board["members"]) + [{"api": added, "star": 1}]
        effects = resolve_board_traits(self.snap, members)["effects"]
        selected = deepcopy(board["selected"])
        selected[added] = {"items": (), "count": 0, "alpha": False}
        return members, effects, selected

    def test_nine_actor_cap_evaluates_against_explicit_eight_slot_references(self):
        members, effects, selected = self.capped_candidate()
        board = self.board
        for geometry in ("spread", "clump"):
            with self.subTest(geometry=geometry):
                raw = team.Evaluator(self.snap, geometry, prepared=False)
                native = team.Evaluator(self.snap, geometry)
                expected = raw.evaluate(members, effects, selected, board["carry"], board["tank"])
                actual = native.evaluate(members, effects, selected, board["carry"], board["tank"])
                self.assertEqual(actual, expected)
                self.assertEqual(len(actual["units"]), 9)
                self.assertEqual(actual["metrics"]["benchmarkCount"], 12)
                actors = raw.allies(members, effects, selected, board["carry"], board["tank"])
                self.assertEqual(len({(actor["frontline"], actor["lane"]) for actor in actors}), 9)
                for encounter in actual["matchups"]:
                    self.assertEqual((encounter["level"], encounter["boardSlots"], encounter["slotsUsed"], encounter["unitCount"]),
                                     (8, 8, 8, 8))
                    self.assertEqual(sum(unit["slotCost"] for unit in encounter["roster"]), 8)

    def test_ninth_actor_changes_persistent_identity_and_reordered_members_reuse_it(self):
        members, effects, selected = self.capped_candidate()
        board = self.board
        with TemporaryDirectory() as directory:
            initial = team.Evaluator(self.snap, "clump", score_cache_dir=directory)
            self.evaluate_many(initial, [board["selected"]])
            initial._score_cache.close()
            cap = team.Evaluator(self.snap, "clump", score_cache_dir=directory)
            expected = cap.evaluate_many(members, effects, [selected], board["carry"], board["tank"])
            self.assertEqual(cap.stats["cachedFightsReused"], 0)
            self.assertEqual(cap.stats["sharedFightsSimulated"], 12)
            cap._score_cache.close()
            replay = team.Evaluator(self.snap, "clump", score_cache_dir=directory)
            with patch.object(replay, "_simulate_matches", side_effect=AssertionError("cached capped board reran")):
                actual = replay.evaluate_many(members[::-1], effects, [selected], board["carry"], board["tank"])
            self.assertEqual(actual, expected)
            self.assertEqual(replay.stats["cachedFightsReused"], 12)
            replay._score_cache.close()

    def test_candidate_capacity_uses_slots_and_accepts_eight_actors_with_elder_at_nine(self):
        board = self.board
        evaluator = team.Evaluator(self.snap, "clump")
        members = deepcopy(board["members"])
        removed = next(member["api"] for member in members
                       if not board["selected"][member["api"]]["items"])
        members = [member for member in members if member["api"] != removed]
        members.append({"api": tft_board.ELDER_DRAGON, "star": 1})
        effects = resolve_board_traits(self.snap, members)["effects"]
        selected = {api: option for api, option in deepcopy(board["selected"]).items() if api != removed}
        selected[tft_board.ELDER_DRAGON] = {"items": (), "count": 0, "alpha": False}
        self.assertEqual(tft_board.slots_used(members), 9)
        self.assertEqual(len(evaluator.allies(members, effects, selected, board["carry"], board["tank"])), 8)
        members.append({"api": removed, "star": 2})
        for evaluate in (False, True):
            with self.subTest(evaluate=evaluate), self.assertRaisesRegex(ValueError, "at most nine slots"):
                if evaluate:
                    evaluator.evaluate_many(members, effects, [selected], board["carry"], board["tank"])
                else:
                    evaluator.allies(members, effects, selected, board["carry"], board["tank"])

    def test_position_sequence_is_bounded_through_ninth_actor_for_either_row(self):
        evaluator = team.Evaluator(self.snap, "clump")
        for front in (False, True):
            with self.subTest(front=front):
                units = [unit for unit in self.snap.units.values() if unit["api"] != tft_board.ELDER_DRAGON
                         and (unit["objective"] in ("tank", "fighter")) == front][:9]
                members = [{"api": unit["api"], "star": 2} for unit in units]
                effects = {member["api"]: [] for member in members}
                selected = {member["api"]: {"items": (), "alpha": False} for member in members}
                actors = evaluator.allies(members, effects, selected, units[0]["api"], units[1]["api"])
                self.assertEqual([actor["lane"] for actor in actors], [3, 1, 5, 2, 4, 0, 6, 3, 1])
                self.assertTrue(all(actor["frontline"] == front for actor in actors))
                invalid = members + [{"api": self.snap.unit("Elder Dragon")["api"], "star": 1}]
                with self.assertRaisesRegex(ValueError, "at most nine slots"):
                    evaluator.allies(invalid, effects, selected, units[0]["api"], units[1]["api"])


if __name__ == "__main__":
    unittest.main()
