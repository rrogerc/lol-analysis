"""Composition combat must use the role and row of the equipped Adaptor form."""

from copy import deepcopy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import tft
import tft_team as team
from test_tft_symmetric import events, match
from test_tft_team_engine import ally
from tft_comp_traits import resolve_board_traits


class TestAdaptorCombatPositions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.nidalee = cls.snap.unit("Nidalee")["api"]
        cls.carry = cls.snap.unit("Sivir")["api"]
        cls.tank = cls.snap.unit("Malphite")["api"]
        cls.members = [{"api": api, "star": 2} for api in (cls.nidalee, cls.carry, cls.tank)]
        cls.effects = resolve_board_traits(cls.snap, cls.members)["effects"]

    def selection(self, form):
        return {
            self.nidalee: {"items": ("DA_SpearOfShojin", "DA_SteraksGage" if form == "AD"
                                     else "DA_RabadonsDeathcap"), "alpha": False},
            self.carry: {"items": ("DA_InfinityEdge", "DA_LastWhisper"), "alpha": False},
            self.tank: {"items": ("DA_GargoyleStoneplate", "DA_SpiritVisage"), "alpha": False},
        }

    def actors(self, evaluator, form, *, prepared=False):
        return evaluator.allies(self.members, self.effects, self.selection(form),
                                self.carry, self.tank, prepared=prepared)

    def test_native_form_resolves_role_and_distinct_positions_without_mutating_base(self):
        evaluator = team.Evaluator(self.snap, "clump")
        before = deepcopy(self.snap.units[self.nidalee])
        self.assertIn("Melee cougar Assassin", before["ability"]["desc"])
        for form, kind, front, ability in (("AD", "Assassin", True, "Prowler's Pounce"),
                                           ("AP", "Marksman", False, "Javelin Toss"),
                                           ("AD", "Assassin", True, "Prowler's Pounce")):
            with self.subTest(form=form):
                actors = self.actors(evaluator, form)
                actor = next(row for row in actors if row["spec"]["unit"]["api"] == self.nidalee)
                spec = actor["spec"]
                probe = dict(spec, dummies={"slots": [{"hp": 1, "armor": 0, "mr": 0}]})
                self.assertEqual(tft.engine().compose_fx(probe)["form"], form)
                self.assertEqual((spec["unit"]["form"], spec["unit"]["kind"], actor["frontline"]),
                                 (form, kind, front))
                self.assertEqual(len({(row["frontline"], row["lane"]) for row in actors}), len(actors))
                layout = evaluator.combat_layout(self.members, self.effects, self.selection(form),
                                                 self.carry, self.tank)
                self.assertEqual(layout[self.nidalee]["abilityName"], ability)
                self.assertEqual(layout[self.nidalee]["lane"], actor["lane"])
        self.assertEqual(self.snap.units[self.nidalee], before)
        self.assertTrue(all(template["unit"]["kind"] == "Marksman"
                            for key, template in evaluator.templates.items() if key[0] == self.nidalee))

    def test_melee_form_takes_frontline_pressure_while_ranged_form_is_protected(self):
        evaluator = team.Evaluator(self.snap, "clump")
        pressure = [ally(hp=10000, ad=100, attack_speed=1, lane=1)]
        taken = {}
        for form in ("AD", "AP"):
            result = match(self.actors(evaluator, form), pressure, duration=0.1)
            taken[form] = next(unit["damageTaken"] for unit in result["allies"]
                               if unit["api"] == self.nidalee)
        self.assertGreater(taken["AD"], 0)
        self.assertEqual(taken["AP"], 0)

    def test_cougar_receives_assassin_reduction_only_from_other_attackers(self):
        evaluator = team.Evaluator(self.snap, "clump")
        for form, expected in (("AD", [100.0, 85.0]), ("AP", [100.0, 100.0])):
            with self.subTest(form=form):
                actor = deepcopy(next(row for row in self.actors(evaluator, form)
                                      if row["spec"]["unit"]["api"] == self.nidalee))
                # Isolate the resolved role from stats, shields and trait/item
                # defenses while preserving native AD/AP form selection.
                actor["spec"]["traits"] = []
                actor["spec"]["items"] = [{"api": "test", "name": "test", "unique": False,
                                             "stats": [["adPct", .2]] if form == "AD" else [["ap", 20]],
                                             "adds": []}]
                for kit in actor["spec"]["kits"].values():
                    kit.update(hpStar=10000, baseAd=0)
                    kit["stats"].update(hp=10000, ad=0, armor=0, mr=0, mana=0, initialMana=0)
                actor.update(frontline=True, lane=3)
                result = match([actor], [ally(ad=100, lane=1), ally(ad=100, lane=5)], duration=0.1)
                self.assertEqual([hit["amount"] for hit in events(result, "damage", "enemy", name="auto")],
                                 expected)

    def test_prepared_results_and_compact_results_share_the_actual_layout(self):
        scalar = team.Evaluator(self.snap, "clump", prepared=False)
        native = team.Evaluator(self.snap, "clump")
        allocations = [self.selection(form) for form in ("AD", "AP", "AD")]
        args = (self.members, self.effects, allocations, self.carry, self.tank)
        expected = scalar.evaluate_many(*args, subset="screen", details=True)
        actual = native.evaluate_many(*args, subset="screen", details=True)
        self.assertEqual(actual, expected)
        self.assertIs(actual[0], actual[2])
        self.assertEqual([(row["units"][self.nidalee]["form"], row["units"][self.nidalee]["frontline"])
                          for row in actual], [("AD", True), ("AP", False), ("AD", True)])
        compact = native.evaluate_many(*args, subset="screen", details=False)
        self.assertEqual(compact, [dict(row, units={}) for row in actual])

    def test_persistent_scores_distinguish_form_changes_and_reuse_matching_layouts(self):
        allocations = [self.selection("AD"), self.selection("AP")]
        args = (self.members, self.effects, allocations, self.carry, self.tank)
        with TemporaryDirectory() as directory:
            first = team.Evaluator(self.snap, "clump", score_cache_dir=directory)
            expected = first.evaluate_many(*args, subset="screen")
            self.assertEqual(first.stats["teamAllocationsSimulated"], 2)
            first._score_cache.close()
            replay = team.Evaluator(self.snap, "clump", score_cache_dir=directory)
            with patch.object(replay, "_simulate_matches", side_effect=AssertionError("cached layouts reran")):
                actual = replay.evaluate_many(*args, subset="screen")
            self.assertEqual(actual, expected)
            self.assertEqual(replay.stats["cachedFightsReused"], 2 * replay.pool["screenEncounters"])
            replay._score_cache.close()

    def test_reference_form_change_relocates_without_overwriting_authored_positions(self):
        data = deepcopy(team.load_pool(self.snap))
        board = next(row for row in data["boards"] if row["id"] == "nidalee-javelin")
        replacements = iter(("DA_SpearOfShojin", "DA_SteraksGage", "DA_LastWhisper"))
        for entry in board["itemPriority"]:
            if entry[0] == self.nidalee:
                entry[1] = next(replacements)
        before = deepcopy(data)
        authored = {unit["api"]: unit for unit in board["units"]}
        with patch.object(team, "load_pool", return_value=data):
            reference = next(row for row in team.opponent_suite(self.snap) if row["opponentId"] == board["id"])
            cougar = next(unit for unit in reference["roster"] if unit["api"] == self.nidalee)
            self.assertEqual((cougar["form"], cougar["kind"], cougar["frontline"], cougar["lane"]),
                             ("AD", "Assassin", True, 4))
            self.assertEqual(cougar["abilityName"], "Prowler's Pounce")
            self.assertEqual(len({(unit["frontline"], unit["lane"]) for unit in reference["roster"]}), 8)
            for unit in reference["roster"]:
                if unit["api"] != self.nidalee:
                    self.assertEqual((unit["frontline"], unit["lane"]),
                                     (authored[unit["api"]]["frontline"], authored[unit["api"]]["lane"]))
            encounters = team.Evaluator(self.snap, "clump").encounters(9)
            variants = [row for row in encounters if row["label"]["opponentId"] == board["id"]]
            self.assertEqual(len(variants), 6)
            for encounter in variants:
                actors = {row["spec"]["unit"]["api"]: row for row in encounter["enemies"]}
                for unit in encounter["label"]["roster"]:
                    actor = actors[unit["api"]]
                    self.assertEqual((actor["frontline"], actor["lane"], actor["spec"]["unit"]["kind"]),
                                     (unit["frontline"], unit["lane"], unit["kind"]))
                self.assertEqual(len({(row["frontline"], row["lane"]) for row in actors.values()}), 8)
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
