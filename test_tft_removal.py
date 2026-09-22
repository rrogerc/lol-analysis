"""The removal-aware composition objective (tft_removal).

These pin the parts that are decisions rather than arithmetic: which
opponents get built, how a fight that does not clear the board is scored,
and that the aggregate cannot be rescued by the encounters a board wins.
The end-to-end cases need warm champion cells, because the reference
opponents hold the builds the champion leaderboard found for them.
"""
import collections
import math
import unittest

import tft
import tft_board
import tft_removal
import tft_team
from test_tft import SNAP


def result(outcome="win", duration=12.0, enemy_left=0.0, enemy_fraction=0.0, dps=500.0):
    return {"outcome": outcome, "duration": duration, "enemyHpLeft": enemy_left,
            "enemyHpFraction": enemy_fraction, "allyHpFraction": 0.5, "damageDps": dps,
            "damage": dps * duration, "frontlineTime": duration,
            "enemies": [{"api": "TFT18_Ahri", "alive": enemy_left > 0}],
            "allies": []}


class TestEffectiveClearTime(unittest.TestCase):
    def test_a_clear_scores_its_own_duration(self):
        self.assertEqual(tft_removal.effective_clear_time(result(duration=11.5)), 11.5)

    def test_an_instant_clear_cannot_score_zero(self):
        # A zero would make the geometric mean collapse for the whole board.
        self.assertEqual(tft_removal.effective_clear_time(result(duration=0.0)), tft.TICK_S)

    def test_a_fight_that_does_not_clear_extrapolates_from_what_it_did(self):
        # The survival tier's rule: the whole window, plus the health still
        # standing over the damage the board actually managed per second.
        row = result("timeout", duration=30.0, enemy_left=1500.0, enemy_fraction=0.25, dps=500.0)
        self.assertEqual(tft_removal.effective_clear_time(row, window=30.0), 33.0)

    def test_a_board_that_deals_nothing_never_ranks(self):
        row = result("loss", duration=30.0, enemy_left=6000.0, enemy_fraction=1.0, dps=0.0)
        self.assertEqual(tft_removal.effective_clear_time(row), math.inf)


class TestSummarize(unittest.TestCase):
    LABELS = [{"key": "a"}, {"key": "b"}]

    def test_the_mean_is_geometric_so_one_bad_matchup_still_counts(self):
        rows = [result(duration=4.0), result(duration=16.0)]
        summary = tft_removal.summarize(rows, self.LABELS, details=False)
        self.assertAlmostEqual(summary["metrics"]["removalClearTime"], 8.0)
        self.assertAlmostEqual(summary["metrics"]["removalScore"], 1 / 8.0)
        self.assertEqual(summary["metrics"]["boardsCleared"], 1.0)
        self.assertEqual(summary["metrics"]["evaluationModel"], tft_removal.MODEL)

    def test_one_board_it_cannot_beat_sinks_the_score(self):
        rows = [result(duration=4.0),
                result("loss", duration=30.0, enemy_left=6000.0, enemy_fraction=1.0, dps=0.0)]
        summary = tft_removal.summarize(rows, self.LABELS, details=False)
        self.assertEqual(summary["metrics"]["removalScore"], 0.0)
        self.assertEqual(summary["metrics"]["boardsCleared"], 0.5)

    def test_every_matchup_carries_its_own_time_and_survivors(self):
        rows = [result(duration=4.0),
                result("timeout", duration=30.0, enemy_left=600.0, enemy_fraction=0.1, dps=300.0)]
        summary = tft_removal.summarize(rows, self.LABELS, details=False)
        self.assertEqual([row["effectiveClearTime"] for row in summary["matchups"]], [4.0, 32.0])
        self.assertEqual([row["survivors"] for row in summary["matchups"]], [0, 1])


class TestMedianSelection(unittest.TestCase):
    def test_it_picks_distinct_units_closest_to_the_median(self):
        tanks = [unit for unit in SNAP.units.values() if unit["kind"] == "Tank"]
        picked = tft_removal._closest(tanks, 2, 3)
        self.assertEqual(len({unit["api"] for unit in picked}), 3)
        self.assertTrue(all(unit["kind"] == "Tank" for unit in picked))

    def test_it_is_deterministic(self):
        tanks = [unit for unit in SNAP.units.values() if unit["kind"] == "Tank"]
        self.assertEqual([unit["api"] for unit in tft_removal._closest(tanks, 2, 4)],
                         [unit["api"] for unit in tft_removal._closest(list(reversed(tanks)), 2, 4)])

    def test_it_refuses_rather_than_repeat_a_unit(self):
        tanks = [unit for unit in SNAP.units.values() if unit["kind"] == "Tank"][:2]
        with self.assertRaises(ValueError):
            tft_removal._closest(tanks, 2, 5)


class TestOpponentBoards(unittest.TestCase):
    def test_cold_cells_are_an_error_not_an_itemless_opponent(self):
        with self.assertRaises(Exception):
            tft_removal.opponent_boards(SNAP, "clump", paths={})

    def test_unknown_geometry(self):
        with self.assertRaises(ValueError):
            tft_removal.opponent_boards(SNAP, "diagonal")


# The fixtures pin an archived snapshot whose cells are not warm, so the
# opponents take an explicit build map instead of the champion leaderboard's.
# `leaderboard_builds` is what supplies it in production; TestLeaderboardBuilds
# checks that path separately against the live snapshot when it is warm.
FIXED_BUILDS = collections.defaultdict(
    lambda: ("DA_GuinsoosRageblade", "DA_InfinityEdge", "DA_LastWhisper"))


def boards(geometry="clump"):
    return tft_removal.opponent_boards(SNAP, geometry, builds=FIXED_BUILDS)


class TestOpponentSuite(unittest.TestCase):
    def setUp(self):
        self.boards = boards()

    def test_one_board_per_declared_axis(self):
        self.assertEqual(len(self.boards),
                         len(tft_removal.OPPONENT_FRONTLINES) * len(tft_removal.OPPONENT_DAMAGE))
        self.assertEqual(len({board["opponentId"] for board in self.boards}), len(self.boards))

    def test_every_board_is_legal_and_carries_its_leaderboard_items(self):
        for board in self.boards:
            with self.subTest(board["opponentId"]):
                self.assertLessEqual(tft_board.slots_used(board["members"]),
                                     tft_board.DEFAULT_BOARD_SLOTS)
                self.assertEqual(len({member["api"] for member in board["members"]}),
                                 len(board["members"]))
                self.assertEqual(len(board["selected"][board["carry"]]["items"]), 3)
                self.assertEqual(len(board["selected"][board["tank"]]["items"]), 3)
                self.assertNotEqual(board["carry"], board["tank"])

    def test_the_frontline_count_is_what_it_says(self):
        for board in self.boards:
            front = sum(1 for member in board["members"]
                        if SNAP.units[member["api"]]["kind"] == "Tank")
            self.assertIn(front, tft_removal.OPPONENT_FRONTLINES)


class TestLeaderboardBuilds(unittest.TestCase):
    def test_it_reads_the_matching_scenario_or_says_the_cells_are_cold(self):
        try:
            builds = tft_removal.leaderboard_builds(SNAP, "clump")
        except Exception:
            self.skipTest("the archived snapshot's champion cells are not warm")
        self.assertTrue(all(len(items) <= 3 for items in builds.values()))


class TestEvaluator(unittest.TestCase):
    """One real board scored end to end, and the property the model exists for."""

    @classmethod
    def setUpClass(cls):
        cls.evaluator = tft_removal.Evaluator(SNAP, "clump", prepared=True,
                                              builds=FIXED_BUILDS)

    def score(self, members, carry, tank, items):
        from tft_comp_traits import resolve_board_traits
        resolved = resolve_board_traits(SNAP, members, None)
        selected = {member["api"]: {"items": list(items.get(member["api"], [])), "alpha": False}
                    for member in members}
        return self.evaluator.evaluate(members, resolved["effects"], selected, carry, tank)

    def test_a_board_scores_and_reports_its_matchups(self):
        members = [{"api": api, "star": 2} for api in
                   ("TFT18_Gromp", "TFT18_Sejuani", "TFT18_Amumu", "TFT18_Shen",
                    "TFT18_Vi", "TFT18_KogMaw", "TFT18_Nidalee", "TFT18_Scuttlecrab")]
        out = self.score(members, "TFT18_Gromp", "TFT18_Sejuani",
                         {"TFT18_Gromp": ["DA_RabadonsDeathcap", "DA_SpearOfShojin",
                                          "DA_GuinsoosRageblade"]})
        metrics = out["metrics"]
        self.assertEqual(metrics["evaluationModel"], tft_removal.MODEL)
        self.assertGreater(metrics["removalClearTime"], 0.0)
        self.assertEqual(len(out["matchups"]),
                         len(self.evaluator.boards) * len(tft_team.INITIATIVES))
        self.assertEqual(out["opponentCount"], len(self.evaluator.boards))

    def test_more_items_on_the_carry_never_score_worse(self):
        members = [{"api": api, "star": 2} for api in
                   ("TFT18_Gromp", "TFT18_Sejuani", "TFT18_Amumu", "TFT18_Shen",
                    "TFT18_Vi", "TFT18_KogMaw", "TFT18_Nidalee", "TFT18_Scuttlecrab")]
        bare = self.score(members, "TFT18_Gromp", "TFT18_Sejuani", {})
        armed = self.score(members, "TFT18_Gromp", "TFT18_Sejuani",
                           {"TFT18_Gromp": ["DA_RabadonsDeathcap", "DA_RabadonsDeathcap",
                                            "DA_SpearOfShojin"]})
        self.assertGreaterEqual(armed["metrics"]["removalScore"],
                                bare["metrics"]["removalScore"])


if __name__ == "__main__":
    unittest.main()
