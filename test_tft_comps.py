"""Composition legality, shared resource allocation, search and publication."""
from copy import deepcopy
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
import json
import math
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from unittest.mock import Mock

import tft
import tft_comps as comps
from tft_board import BOARD_PLAN_MODEL, ELDER_DRAGON, slots_used


class _PipelineFixtureSearch(comps.Search):
    """Tiny deterministic fights; retain the real phase and board assembly code."""
    def prepare(self):
        self.screened = 3
        self.states_screened = 7

    def candidates(self):
        roster = [self.snap.unit(name)["api"] for name in
                  ("Akali", "Yorick", "Camille", "Karma", "Varus", "Xayah", "Rakan", "Leona")]
        return [(tuple(sorted(roster[:-1] + [self.snap.unit(last)["api"]])), roster[0], roster[1])
                for last in ("Leona", "Pebbles", "Teemo")]

    def refine(self, candidate):
        roster, carry, tank = candidate
        members = comps.board_members(self.snap, roster, carry, tank, self.profile)
        selected = {api: {"items": (), "count": 0, "dps": 1.0, "frontline": 0.0,
                          "alpha": False, "itemBurn": False, "infernoBurn": False} for api in roster}
        selected[carry] = dict(selected[carry], items=("DA_Deathblade",) * 3, count=3)
        selected[tank] = dict(selected[tank], items=("DA_WarmogsArmor",) * 3, count=3)
        index = self.candidates().index(candidate)
        wins = 8 if index < 2 else 7
        allocation = {"selected": selected, "metrics": {"benchmarkWins": wins, "benchmarkCount": 12,
            "benchmarkWinRate": wins / 12, "damageDps": 8.0, "frontlineTime": 10.0, "hpMargin": index / 10},
            "units": {api: {"damage": 10.0, "dps": 1.0, "aliveTime": 10.0} for api in roster},
            "matchups": [], "screening": {}}
        result = {"members": members, "traits": comps.resolve_board_traits(self.snap, members),
                  "allocations": {"6": {"single": [allocation]}}}
        self.refined[candidate] = result
        return result

    def swaps(self):
        return []

    def item_anchors(self, candidate, refined, budget, structure):
        return {api: [option] for api, option in refined["allocations"][str(budget)][structure][0]["selected"].items()}

    def final_items(self, candidate, refined, budget, structure, *, anchors=None):
        if anchors is not None:
            assert anchors == self.item_anchors(candidate, refined, budget, structure)
        # Preparation deliberately sends only seed inputs. Reconstruct the
        # fixture fight as a real item worker would obtain full diagnostics.
        allocation = self.refine(candidate)["allocations"][str(budget)][structure][0]
        return deepcopy(allocation)

    def validate_boards(self, rows):
        rows = list(rows)
        assert all("rank" in row for row in rows), "validation started before final ranks were fixed"
        assert [row["rank"] for row in rows] == [1, 1, 3]
        def evaluate(members, effects, selected, carry, tank, *, split, healing_policy="broad"):
            assert split == "validation"
            wins = 0 if self.snap.unit("Leona")["api"] in selected else 6
            return {"metrics": {"benchmarkWins": wins, "benchmarkCount": 6}, "matchups": [],
                    "poolRevision": "fixture-pool", "poolSplit": split, "opponentCount": 3,
                    "itemBudget": 6, "healingPolicy": healing_policy}
        with patch.object(self.team, "evaluate", side_effect=evaluate):
            super().validate_boards(rows)

    def level9_upgrade(self, row):
        assert "rank" in row and "validation" in row, "cap started before the parent was finalized"
        # Make the weakest parent's cap strongest. Parent ordering must stay
        # based on the actual level-eight result in both execution paths.
        wins = 12 if row["metrics"]["benchmarkWins"] == 7 else 0
        return {"parentId": row["id"], "board": {"id": "cap-" + row["id"],
                "level": 9, "metrics": {"benchmarkWins": wins, "benchmarkCount": 12}}}


_REAL_WORKER_TASK = comps._worker_task


def _fixture_pipeline_task(task):
    """Runs the actual task dispatcher under spawn with bounded fake fights."""
    if comps._WORKER_PROGRESS is not None:
        comps._WORKER_PROGRESS.put((task["key"], "fixture:" + json.dumps(
            {"phase": task["phase"], "event": "start", "ordinal": task.get("ordinal"), "pid": os.getpid()})))
    if task["phase"] == "items":
        time.sleep(.3 if task["ordinal"] == 0 else .005)
    with patch.object(comps, "Search", _PipelineFixtureSearch), \
            patch.object(comps, "ITEM_BUDGETS", (6,)), \
            patch.object(comps, "STRUCTURES", ({"key": "single"},)):
        result = _REAL_WORKER_TASK(task)
    if comps._WORKER_PROGRESS is not None:
        comps._WORKER_PROGRESS.put((task["key"], "fixture:" + json.dumps(
            {"phase": task["phase"], "event": "done", "ordinal": task.get("ordinal"), "pid": os.getpid()})))
    return result


def _fixture_failed_pipeline_task(task):
    result = _fixture_pipeline_task(task)
    if task["key"] == "c1-clump-mixed" and task["phase"] == "items":
        raise RuntimeError("fixture item worker failed")
    if task["key"] == "c2-clump-mixed" and task["phase"] == "prepare":
        result["revision"] = "changed-after-prepare"
    return result


def _fixture_wrong_order_task(task):
    result = _fixture_pipeline_task(task)
    if task["phase"] == "validate":
        result["result"]["rows"].reverse()
    return result


def _fixture_wrong_item_task(task):
    result = _fixture_pipeline_task(task)
    if task["phase"] == "items":
        result["result"]["ordinal"] += 1
    return result


def _fixture_wrong_cap_task(task):
    result = _fixture_pipeline_task(task)
    if task["phase"] == "cap":
        result["result"]["parentId"] = "wrong-parent"
    return result


def _worker_probe():
    """Read the spawn initializer's captured generation without simulating."""
    snap = comps._WORKER_SNAPSHOT
    return {"pid": os.getpid(), "inputHash": snap.hash_inputs(), "directory": snap.dir,
            "cache": comps.CACHE_DIR, "revision": comps.revision(snap)}


class TestCompositions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.enterContext(patch.object(comps, "CACHE_DIR", self.tmp.name))
        self.roster = self.apis("Akali", "Yorick", "Camille", "Karma", "Varus", "Xayah", "Rakan", "Leona")
        self.carry, self.tank = self.roster[:2]
        self.profile = comps.PROFILES["c1"]

    def apis(self, *names):
        return [self.snap.unit(name)["api"] for name in names]

    def members(self):
        return comps.board_members(self.snap, self.roster, self.carry, self.tank, self.profile)

    def options(self, api):
        tank = self.snap.units[api]["objective"] == "tank"
        return [{"items": ("test-item",) * count, "count": count,
                 "dps": 10 + count * (5 if tank else 25),
                 "frontline": 5 + count * 20 if tank else 0,
                 "stress": 0, "capped": False, "itemBurn": False,
                 "infernoBurn": False, "alpha": False} for count in range(4)]

    def test_eight_distinct_units_and_same_cost_role_anchors(self):
        self.assertTrue(comps.valid_board(self.snap, self.roster, self.carry, self.tank, self.profile))
        for roster, carry, tank in ((self.roster[:-1], self.carry, self.tank),
                                    (self.roster[:-1] + [self.roster[0]], self.carry, self.tank),
                                    (self.roster, self.carry, self.carry),
                                    (self.roster, self.tank, self.carry)):
            with self.subTest(roster=roster, carry=carry, tank=tank):
                self.assertFalse(comps.valid_board(self.snap, roster, carry, tank, self.profile))
        self.assertTrue(comps.valid_board(self.snap, [self.carry, self.tank], self.carry, self.tank,
                                         self.profile, complete=False))

    def test_reroll_restrictions_prevent_expensive_support_creep(self):
        with_five = self.roster[:-1] + self.apis("Ashe")
        self.assertFalse(comps.valid_board(self.snap, with_five, self.carry, self.tank, self.profile))
        with_two_fours = self.roster[:-2] + self.apis("Ahri", "Amumu")
        self.assertFalse(comps.valid_board(self.snap, with_two_fours, self.carry, self.tank, self.profile))
        with_one_four = self.roster[:-1] + self.apis("Amumu")
        self.assertTrue(comps.valid_board(self.snap, with_one_four, self.carry, self.tank, self.profile))

    def test_four_cost_level_eight_never_requires_five_costs(self):
        roster = self.apis("Ahri", "Sett", "Aphelios", "Amumu", "Ashe", "Taric", "Leona", "Karma")
        profile = comps.PROFILES["c4"]
        self.assertFalse(comps.valid_board(self.snap, roster, roster[0], roster[1], profile))
        legal = self.apis("Ahri", "Sett", "Aphelios", "Amumu", "Leona", "Karma", "Soraka", "Morgana")
        self.assertTrue(comps.valid_board(self.snap, legal, legal[0], legal[1], profile))
        self.assertEqual(profile["maxFiveCosts"], 0)
        search = comps.Search(self.snap, profile, "spread", "mixed")
        self.assertTrue(all(unit["cost"] <= 4 for unit in search.units.values()))

    def test_level_nine_cap_capacity_and_legendary_stars(self):
        roster = self.apis("Ahri", "Sett", "Aphelios", "Amumu", "Ashe", "Taric", "Leona", "Karma", "Soraka")
        profile = dict(comps.PROFILES["c4"], level=9, boardSlots=9, maxFiveCosts=2, minSameCost=3)
        self.assertTrue(comps.valid_board(self.snap, roster, roster[0], roster[1], profile))
        members = comps.board_members(self.snap, roster, roster[0], roster[1], profile)
        for member in members:
            self.assertEqual(member["star"], 2)
        third_five = roster[:-1] + self.apis("Alune")
        self.assertFalse(comps.valid_board(self.snap, third_five, roster[0], roster[1], profile))
        elder = roster[:4] + self.apis("Leona", "Karma", "Soraka", "Elder Dragon")
        self.assertEqual(len(elder), 8)
        self.assertEqual(slots_used(elder), 9)
        self.assertTrue(comps.valid_board(self.snap, elder, elder[0], elder[1], profile))
        self.assertFalse(comps.valid_board(self.snap, elder, elder[0], elder[1], comps.PROFILES["c4"]))

    def test_elder_occupies_two_level_eight_slots(self):
        roster = self.apis("Diana", "Hecarim", "Cassiopeia", "Mama Beak", "Leona", "Karma", "Elder Dragon")
        profile = comps.PROFILES["c3"]
        self.assertEqual(slots_used(roster), 8)
        self.assertTrue(comps.valid_board(self.snap, roster, roster[0], roster[1], profile))
        members = comps.board_members(self.snap, roster, roster[0], roster[1], profile)
        self.assertEqual(next(member["star"] for member in members if member["api"] == ELDER_DRAGON), 1)
        self.assertFalse(comps.valid_board(self.snap, roster + self.apis("Varus"), roster[0], roster[1], profile))
        self.assertFalse(comps.valid_board(self.snap, roster[:-1], roster[0], roster[1], profile))
        self.assertTrue(comps.valid_board(self.snap, roster[:-1], roster[0], roster[1], profile, complete=False))

    def test_beam_can_complete_a_seven_champion_elder_board(self):
        roster = self.apis("Diana", "Hecarim", "Cassiopeia", "Mama Beak", "Leona", "Karma", "Elder Dragon")
        search = comps.Search(self.snap, comps.PROFILES["c3"], "spread", "mixed")
        search.units = {api: self.snap.units[api] for api in roster}
        with patch.object(search, "guide", return_value=1.0):
            candidates = search.candidates()
        self.assertTrue(candidates)
        self.assertTrue(all(len(row[0]) == 7 and slots_used(row[0]) == 8 for row in candidates))
        self.assertTrue(all(ELDER_DRAGON in row[0] for row in candidates))

    def test_only_the_reroll_main_pair_is_automatically_three_star(self):
        members = self.members()
        self.assertEqual(sum(member["star"] == 3 for member in members), 2)
        self.assertTrue(all(member["star"] in tft.unit_stars(self.snap.units[member["api"]]) for member in members))

    def test_every_allocation_spends_exact_shared_budget_and_honors_structure(self):
        libraries = {api: self.options(api) for api in self.roster}
        results = comps.allocate(self.members(), libraries, self.carry, self.tank, self.snap)
        for budget in comps.ITEM_BUDGETS:
            for structure, rows in results[str(budget)].items():
                with self.subTest(budget=budget, structure=structure):
                    if structure == "both" and budget < 8:
                        self.assertEqual(rows, [])
                        continue
                    self.assertTrue(rows)
                    choices = rows[0]["selected"]
                    self.assertEqual(sum(option["count"] for option in choices.values()), budget)
                    self.assertTrue(all(0 <= option["count"] <= 3 for option in choices.values()))
                    self.assertGreaterEqual(choices[self.carry]["count"], 2)
                    self.assertGreaterEqual(choices[self.tank]["count"], 2)
                    second_carries, second_tanks = 0, 0
                    for api, option in choices.items():
                        if api in (self.carry, self.tank) or option["count"] < 2:
                            continue
                        tank = self.snap.units[api]["objective"] == "tank"
                        second_tanks += tank
                        second_carries += not tank
                        self.assertLessEqual(option["count"], choices[self.tank if tank else self.carry]["count"])
                    self.assertEqual((second_carries, second_tanks), {
                        "single": (0, 0), "duoCarry": (1, 0), "duoTank": (0, 1), "both": (1, 1)}[structure])
                    self.assertEqual(rows[0]["damage"], sum(option["dps"] for option in choices.values()))
                    self.assertEqual(rows[0]["front"], sum(option["frontline"] for option in choices.values()))
                    self.assertEqual(rows[0]["balance"], math.sqrt(rows[0]["damage"] * rows[0]["front"]))

    def test_shared_engine_allows_multiple_burn_sources_but_only_one_alpha(self):
        libraries = {api: self.options(api) for api in self.roster}
        for api in self.roster[:2]:
            libraries[api] += [dict(option, dps=option["dps"] + 1000, itemBurn=True,
                                    infernoBurn=True, alpha=True) for option in libraries[api] if option["count"]]
        libraries[self.roster[2]] = [dict(option, itemBurn=True, infernoBurn=True) for option in libraries[self.roster[2]]]
        results = comps.allocate(self.members(), libraries, self.carry, self.tank, self.snap, require_alpha=True)
        for groups in results.values():
            for rows in groups.values():
                for row in rows:
                    choices = row["selected"].values()
                    self.assertEqual(sum(option["alpha"] for option in choices), 1)
        # Burn sources are not suppressed to fake team-wide non-stacking:
        # actual shared enemy debuff state resolves repeated applications.
        found = [row for groups in results.values() for rows in groups.values() for row in rows]
        self.assertTrue(any(sum(option["itemBurn"] for option in row["selected"].values()) > 1 for row in found))

    def test_burn_suppression_keeps_item_stats_and_independent_trait_effects(self):
        original = {"pool": [{"api": "RedBuff", "stats": [["asPct", .45]], "burnOnHit": [.01, 5]}],
                    "items": [], "traits": [{"api": "DA_18_Inferno", "stats": [], "burnOnHit": [.01, 10]}]}
        before = deepcopy(original)
        item_off = comps._burn_adjusted(original, False, True)
        self.assertNotIn("burnOnHit", item_off["pool"][0])
        self.assertEqual(item_off["pool"][0]["stats"], original["pool"][0]["stats"])
        self.assertIn("burnOnHit", item_off["traits"][0])
        trait_off = comps._burn_adjusted(original, True, False)
        self.assertIn("burnOnHit", trait_off["pool"][0])
        self.assertNotIn("burnOnHit", trait_off["traits"][0])
        self.assertEqual(original, before)

    def test_damage_window_is_fixed_and_frontline_is_a_separate_benchmark(self):
        evaluator = comps.Evaluator(self.snap, "clump", "magic")
        damage = evaluator.spec(self.carry, 3, [])
        defense = evaluator.spec(self.tank, 3, [], True)
        self.assertEqual(damage["duration"], 20)
        self.assertTrue(damage["immortal"])
        self.assertFalse(damage["pressure"])
        self.assertEqual(damage["targetDebuffs"], {})
        self.assertEqual(defense["duration"], 60)
        self.assertTrue(defense["immortal"] and defense["pressure"])
        self.assertEqual(len(defense["dummies"]["slots"]), 5)

    def test_cinderling_native_burn_is_retained_for_shared_resolution(self):
        evaluator = comps.Evaluator(self.snap, "clump", "mixed")
        spec = evaluator.spec(self.snap.unit("Cinderling")["api"], 3, [])
        disabled = comps._burn_adjusted(spec, False, False)
        enabled = comps._burn_adjusted(spec, True, False)
        off = tft.engine().simulate(disabled)[1]
        on = tft.engine().simulate(enabled)[1]
        self.assertEqual(off["breakdown"].get("burn", 0), 0)
        self.assertGreater(on["breakdown"]["burn"], 0)
        self.assertEqual(off["breakdown"]["ability"], on["breakdown"]["ability"])
        self.assertEqual(off["breakdown"]["auto"], on["breakdown"]["auto"])
        options = evaluator.options(self.snap.unit("Cinderling")["api"], 3, [])
        bare = [option for option in options if not option["count"]]
        self.assertEqual({option["itemBurn"] for option in bare}, {True})
        self.assertTrue(all(option["utility"] & 4 for option in bare))

    def test_search_is_deterministic_and_all_candidates_obey_the_plan(self):
        def baseline(_self, api, star):
            unit = self.snap.units[api]
            return unit["stats"]["ad"] * star, unit["stats"]["hp"] / 100 if comps._frontliner(unit) else 0
        with patch.object(comps.Evaluator, "baseline", baseline):
            searches = [comps.Search(self.snap, self.profile, "clump", "mixed") for _ in range(2)]
            found = []
            for search in searches:
                search.prepare()
                found.append(search.candidates())
        self.assertEqual(found[0], found[1])
        self.assertEqual(len(found[0]), comps.REFINE_BOARDS)
        self.assertTrue(all(comps.valid_board(self.snap, roster, carry, tank, self.profile)
                            for roster, carry, tank in found[0]))

    def test_support_swaps_recompute_trait_contexts(self):
        roster = tuple(self.apis("Cinderling", "Yorick", "Pebbles", "Scuttlecrab", "Karma", "Varus", "Rakan", "Leona"))
        carry, tank = roster[:2]
        changed = tuple(self.apis("Cinderling", "Yorick", "Pebbles", "Scuttlecrab", "Karma", "Teemo", "Rakan", "Leona"))
        search = comps.Search(self.snap, self.profile, "clump", "mixed")
        def options(api, star, effects, alpha=False):
            return [dict(option, alpha=alpha) for option in self.options(api)]
        with patch.object(search.evaluator, "options", side_effect=options), \
                patch.object(search, "compare_allocations", return_value=[]):
            a = search.refine((roster, carry, tank))
            b = search.refine((changed, carry, tank))
        invoker = lambda result: next(trait for trait in result["traits"]["traits"] if trait["name"] == "Invoker")
        self.assertFalse(invoker(a)["active"])
        self.assertTrue(invoker(b)["active"])
        self.assertEqual(invoker(b)["count"], 2)
        self.assertEqual(len(search.refined), 2)

    def test_allocation_shortlist_keeps_offense_defense_and_utility(self):
        libraries = {api: self.options(api) for api in self.roster}
        base = next(option for option in libraries[self.tank] if option["count"] == 3)
        libraries[self.tank] += [dict(base, items=(name,) * 3, dps=damage, frontline=front, utility=utility)
                                 for name, damage, front, utility in (("offense", 10000, 1, 0),
                                                                     ("defense", 1, 10000, 0),
                                                                     ("utility", 1, 1, 7))]
        results = comps.allocate(self.members(), libraries, self.carry, self.tank, self.snap)
        options = [row["selected"][self.tank] for row in results["9"]["single"]]
        self.assertTrue(any(option["items"] == ("offense",) * 3 for option in options))
        self.assertTrue(any(option["items"] == ("defense",) * 3 for option in options))
        self.assertTrue(any(option.get("utility") == 7 for option in options))

    def test_actual_team_outcome_selects_seed_over_protected_dps(self):
        search = comps.Search(self.snap, self.profile, "clump", "mixed")
        selected = {api: self.options(api)[0] for api in self.roster}
        offense = dict(self.options(self.tank)[3], items=("offense",) * 3, dps=10000, frontline=1)
        defense = dict(offense, items=("defense",) * 3, dps=1, frontline=100)
        selected[self.tank] = offense
        libraries = {api: [option] for api, option in selected.items()}
        libraries[self.tank] = [offense, defense]
        calls = []
        def evaluate(_members, _effects, choices, _carry, _tank, *, split, details):
            self.assertEqual(split, "search")
            self.assertFalse(details)
            results = []
            for choice in choices:
                tank_items = choice[self.tank]["items"]
                calls.append(tank_items)
                results.append({"metrics": {"benchmarkWins": 4 if tank_items == defense["items"] else 0,
                    "benchmarkCount": 4, "benchmarkWinRate": 1.0 if tank_items == defense["items"] else 0},
                    "matchups": []})
            return results
        with patch.object(search.team, "evaluate_many", side_effect=evaluate):
            defensive = dict(selected, **{self.tank: defense})
            rows = search.compare_allocations(self.members(), {}, [{"selected": selected}, {"selected": defensive}], libraries, self.carry, self.tank)
        self.assertEqual(rows[0]["selected"][self.tank]["items"], defense["items"])
        self.assertIn(offense["items"], calls)
        self.assertIn(defense["items"], calls)
        self.assertEqual(rows[0]["metrics"]["benchmarkWins"], 4)

    def test_published_unit_contributions_come_from_the_shared_fights(self):
        search = comps.Search(self.snap, self.profile, "clump", "mixed")
        selected = {api: dict(self.options(api)[0], items=()) for api in self.roster}
        selected[self.carry] = dict(self.options(self.carry)[3], items=("DA_Deathblade",) * 3)
        selected[self.tank] = dict(self.options(self.tank)[3], items=("DA_WarmogsArmor",) * 3)
        members = self.members()
        traits = comps.resolve_board_traits(self.snap, members)
        shared = {api: {"damage": 60.0, "dps": 6.0, "aliveTime": 8.0, "damageTaken": 20.0,
                        "healing": 0.0, "shielding": 0.0, "allyHealing": 0.0, "allyShielding": 0.0}
                  for api in self.roster}
        allocation = {"selected": selected, "metrics": {"benchmarkWins": 1, "benchmarkCount": 4,
                      "benchmarkScore": 25.0, "damageDps": 48.0, "frontlineTime": 8.0,
                      "hpMargin": -.5, "clearTime": 10.0}, "units": shared,
                      "matchups": [{"key": "fixture"}], "screening": {"damageDps": 9999.0, "frontlineIndex": 200.0}}
        row = search.compose((tuple(self.roster), self.carry, self.tank),
                             {"members": members, "traits": traits}, 6, "single", allocation)
        self.assertEqual(row["metrics"]["damageDps"], 48)
        self.assertEqual(sum(unit["dps"] for unit in row["units"]), 48)
        self.assertTrue(all(unit["aliveTime"] == 8 for unit in row["units"]))
        self.assertTrue(all(type(unit["frontline"]) is bool for unit in row["units"]))
        self.assertEqual(row["matchups"], [{"key": "fixture"}])
        self.assertNotIn("balanceScore", row["metrics"])

    def test_compact_item_winner_gets_full_diagnostics_before_publication(self):
        search = comps.Search(self.snap, self.profile, "clump", "mixed")
        selected = {api: dict(self.options(api)[0], items=()) for api in self.roster}
        selected[self.carry] = dict(selected[self.carry], items=("DA_Deathblade",) * 3, count=3)
        selected[self.tank] = dict(selected[self.tank], items=("DA_WarmogsArmor",) * 3, count=3)
        refined = {"members": self.members(), "traits": {"effects": {api: [] for api in self.roster}},
                   "allocations": {"6": {"single": [{"selected": selected}]}}}
        compact = {"metrics": {"benchmarkWins": 8, "benchmarkCount": 12}, "matchups": []}
        full = {**compact, "units": {api: {"damage": 12.0, "dps": 1.2} for api in self.roster}}
        optimizer = Mock(stats={"singleItemComparisons": 204})
        optimizer.optimize.return_value = selected, compact, {"evidence": "all legal replacements"}
        with patch.object(comps, "ItemSearch", return_value=optimizer), \
                patch.object(search.team, "evaluate", return_value=full) as evaluate, \
                patch.object(search.evaluator, "loadout", side_effect=lambda api, *args: selected[api]):
            result = search.final_items((tuple(self.roster), self.carry, self.tank), refined, 6, "single", anchors={})
        evaluate.assert_called_once_with(refined["members"], refined["traits"]["effects"], selected,
                                         self.carry, self.tank, split="search")
        self.assertIs(result["units"], full["units"])
        self.assertEqual(result["itemAnalysis"], {"evidence": "all legal replacements"})
        self.assertEqual(search.team.stats["singleItemComparisons"], 204)

    def test_held_out_results_are_added_after_selection_and_never_change_ranks(self):
        search = comps.Search(self.snap, self.profile, "clump", "mixed")
        other = self.roster[:-1] + self.apis("Pebbles")
        candidates = [(tuple(sorted(roster)), self.carry, self.tank) for roster in (self.roster, other)]
        evaluations = []
        def refine(candidate):
            roster, carry, tank = candidate
            members = comps.board_members(self.snap, roster, carry, tank, self.profile)
            selected = {api: dict(self.options(api)[0], items=()) for api in roster}
            selected[carry] = dict(selected[carry], items=("DA_Deathblade",) * 3, count=3)
            selected[tank] = dict(selected[tank], items=("DA_WarmogsArmor",) * 3, count=3)
            wins = 8 if candidate == candidates[0] else 7
            allocation = {"selected": selected, "metrics": {"benchmarkWins": wins, "benchmarkCount": 12,
                          "benchmarkWinRate": wins / 12, "damageDps": 0, "frontlineTime": 0},
                          "units": {api: {} for api in roster}, "matchups": [], "screening": {}}
            result = {"members": members, "traits": comps.resolve_board_traits(self.snap, members),
                      "allocations": {"6": {"single": [allocation]}}}
            search.refined[candidate] = result
            return result
        def validate(members, effects, selected, carry, tank, *, split, healing_policy="broad"):
            self.assertEqual(split, "validation")
            evaluations.append(tuple(sorted(m["api"] for m in members)))
            # The weaker selected board gets a perfect held-out result;
            # this must not improve its rank or trigger another item search.
            wins = 0 if self.apis("Leona")[0] in selected else 6
            return {"metrics": {"benchmarkWins": wins, "benchmarkCount": 6}, "matchups": [],
                    "poolRevision": "pool", "poolSplit": "validation", "opponentCount": 3, "itemBudget": 6,
                    "healingPolicy": healing_policy}
        with patch.object(comps, "ITEM_BUDGETS", (6,)), \
                patch.object(comps, "STRUCTURES", ({"key": "single"},)), \
                patch.object(search, "prepare"), patch.object(search, "candidates", return_value=candidates), \
                patch.object(search, "refine", side_effect=refine), patch.object(search, "swaps", return_value=[]), \
                patch.object(search, "final_items", side_effect=lambda c, r, b, s: r["allocations"][str(b)][s][0]) as final_items, \
                patch.object(search.team, "evaluate", side_effect=validate):
            rows = search.run()["6"]["single"]
        self.assertEqual([row["metrics"]["benchmarkWins"] for row in rows], [8, 7])
        self.assertEqual([row["validation"]["metrics"]["benchmarkWins"] for row in rows], [0, 6])
        self.assertEqual([row["rank"] for row in rows], [1, 2])
        self.assertEqual(len(evaluations), 4)
        self.assertEqual(final_items.call_count, 2)

    def test_single_item_screening_retains_competing_self_sustain(self):
        evaluator = comps.Evaluator(self.snap, "clump", "mixed")
        unit = self.snap.unit("Mama Beak")
        options = evaluator.options(unit["api"], 2, [])
        singles = {option["items"][0] for option in options if option["count"] == 1}
        self.assertEqual(singles, set(evaluator.pool))
        self.assertIn("DA_Bloodthirster", singles)
        self.assertIn("DA_HextechGunblade", singles)

    def test_missing_and_old_generations_are_not_fabricated_results(self):
        with patch.object(tft, "snapshot_revision", return_value="old"):
            old = comps.cell_paths(self.snap)["c1-clump-mixed"]
            comps._atomic(old, {"sentinel": "old"})
            self.assertEqual(comps.cached_scenario("c1-clump-mixed", snap=self.snap), {"sentinel": "old"})
        with patch.object(tft, "snapshot_revision", return_value="new"):
            self.assertIsNone(comps.cached_scenario("c1-clump-mixed", snap=self.snap))
        with self.assertRaisesRegex(ValueError, "unknown composition"):
            comps.cached_scenario("c5-clump-mixed", snap=self.snap)

    def test_failed_search_does_not_publish_a_ready_artifact(self):
        with patch.object(comps.Search, "run", side_effect=RuntimeError("broken calculation")), \
                patch.object(comps.signal, "signal"):
            with self.assertRaisesRegex(RuntimeError, "broken calculation"):
                comps.warm(log=lambda _: None, only="c1-clump-mixed", snap=self.snap)
        self.assertIsNone(comps.cached_scenario("c1-clump-mixed", snap=self.snap))
        self.assertEqual(comps.progress_state()["status"], "failed")
        self.assertFalse(comps.warm_running())

    def test_ready_artifact_carries_shared_model_marker_and_opponent_budget(self):
        with patch.object(comps.Search, "run", return_value={}), patch.object(comps.signal, "signal"):
            self.assertEqual(comps.warm(log=lambda _: None, only="c1-clump-mixed", snap=self.snap), 1)
        ready = comps.cached_scenario("c1-clump-mixed", snap=self.snap)
        self.assertEqual(ready["methodology"]["evaluationModel"], "symmetric-reference-pool-v1")
        self.assertEqual(ready["baselineRevision"], tft.snapshot_revision(self.snap))
        self.assertEqual(ready["opponentPool"]["searchBoards"], 6)
        self.assertEqual(ready["opponentPool"]["validationBoards"], 3)

    def test_atomic_cache_writes_are_concurrent_safe_and_clean_up_failures(self):
        target = Path(self.tmp.name, "bench", "shared.json")
        values = [{"writer": i, "payload": str(i) * 20000} for i in range(32)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda value: comps._atomic(target, value), values))
        final = json.loads(target.read_text())
        self.assertIn(final, values)
        self.assertEqual(list(target.parent.glob("*.tmp")), [])
        with self.assertRaises(ValueError):
            comps._atomic(target, {"invalid": math.nan})
        self.assertEqual(json.loads(target.read_text()), final)
        self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_spawn_worker_uses_supplied_snapshot_and_cache_directory(self):
        snapshot = deepcopy(self.snap)
        snapshot._input_hash = "pinned-staged-inputs"
        snapshot.dir = str(Path(self.tmp.name, "staged-snapshot"))
        expected = comps.revision(snapshot)
        with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                initializer=comps._worker_init, initargs=(snapshot, self.tmp.name, expected, None)) as pool:
            seen = pool.submit(_worker_probe).result(timeout=30)
        self.assertNotEqual(seen["pid"], os.getpid())
        self.assertEqual(seen["inputHash"], "pinned-staged-inputs")
        self.assertEqual(seen["directory"], snapshot.dir)
        self.assertEqual(seen["cache"], self.tmp.name)
        self.assertEqual(seen["revision"], expected)

    def test_parallel_workers_do_not_start_nested_loadout_threads(self):
        evaluator = comps.Evaluator(self.snap, "clump", "mixed")
        spec = {"pool": []}
        with patch.object(comps, "_WORKER_SNAPSHOT", self.snap), \
                patch.object(tft.engine(), "optimize_loadouts", return_value=(0, [[], [], [], []])) as optimize:
            evaluator.optimal(spec)
        optimize.assert_called_once_with(spec, top=comps.LOADOUT_TOP, workers=1)

    def test_parallel_runner_collects_every_failure_and_success(self):
        keys = ["c1-clump-mixed", "c2-clump-mixed", "c3-clump-mixed"]
        revision = comps.revision(self.snap)
        envelope = {"key": keys[2], "revision": revision, "baselineRevision": tft.snapshot_revision(self.snap)}
        futures = []
        for value in (RuntimeError("first failure"), SystemExit("second failure"),
                      dict(envelope, phase="prepare", result={"jobs": []}),
                      dict(envelope, phase="validate", result={"rows": []})):
            future = Future()
            if isinstance(value, BaseException):
                future.set_exception(value)
            else:
                future.set_result(value)
            futures.append(future)
        executor = Mock()
        executor.submit.side_effect = futures
        publish = Mock()
        with patch.object(comps, "ProcessPoolExecutor", return_value=executor) as construct:
            errors = comps._parallel_scenarios(keys, self.snap, 2, revision, lambda *_: None, publish)
        self.assertEqual(errors, {keys[0]: "prepare: first failure", keys[1]: "prepare: second failure"})
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[0], keys[2])
        self.assertEqual(publish.call_args.args[1]["results"], comps._empty_results())
        self.assertEqual(construct.call_args.kwargs["max_workers"], 2)
        self.assertEqual(construct.call_args.kwargs["mp_context"].get_start_method(), "spawn")
        self.assertIs(construct.call_args.kwargs["initargs"][0], self.snap)
        executor.shutdown.assert_called_once_with(wait=True)

    def test_spawn_pipeline_matches_serial_and_keeps_validation_after_shared_tie_ranks(self):
        keys = ["c1-clump-mixed", "c1-spread-mixed"]
        published, events = {}, []
        with patch.object(comps, "Search", _PipelineFixtureSearch), \
                patch.object(comps, "ITEM_BUDGETS", (6,)), \
                patch.object(comps, "STRUCTURES", ({"key": "single"},)), \
                patch.object(comps, "_worker_task", _fixture_pipeline_task):
            serial = {key: comps._scenario_payload(key, self.snap) for key in keys}
            errors = comps._parallel_scenarios(keys, self.snap, 2, comps.revision(self.snap),
                lambda key, message: events.append((key, message)), lambda key, payload: published.update({key: payload}))
        self.assertEqual(errors, {})
        self.assertEqual(set(published), set(keys))
        completion_orders = []
        for key in keys:
            # The entire artifact is identical apart from observed time/work
            # statistics; input and output revisions must match as well.
            without_runtime = lambda payload: {field: value for field, value in payload.items()
                if field not in ("search", "computeSeconds", "computedAt")}
            self.assertEqual(without_runtime(published[key]), without_runtime(serial[key]))
            rows = published[key]["results"]["6"]["single"]
            self.assertEqual([row["rank"] for row in rows], [1, 1, 3])
            self.assertEqual([row["metrics"]["benchmarkWins"] for row in rows], [8, 8, 7])
            self.assertIs(published[key]["search"]["exhaustive"], False)
            recorded = [json.loads(message.removeprefix("fixture:")) for context, message in events
                        if context == key and message.startswith("fixture:")]
            validation_start = next(index for index, event in enumerate(recorded)
                                    if event["phase"] == "validate" and event["event"] == "start")
            completed_items = [event["ordinal"] for event in recorded[:validation_start]
                               if event["phase"] == "items" and event["event"] == "done"]
            self.assertEqual(set(completed_items), {0, 1, 2})
            completion_orders.append(completed_items)
            self.assertTrue(all(event["pid"] != os.getpid() for event in recorded))
        self.assertTrue(any(order != [0, 1, 2] for order in completion_orders))

    def test_real_spawn_item_failure_and_changed_preparation_revision_block_only_their_contexts(self):
        keys = ["c1-clump-mixed", "c2-clump-mixed", "c3-clump-mixed"]
        published, events = {}, []
        with patch.object(comps, "ITEM_BUDGETS", (6,)), \
                patch.object(comps, "STRUCTURES", ({"key": "single"},)), \
                patch.object(comps, "_worker_task", _fixture_failed_pipeline_task):
            errors = comps._parallel_scenarios(keys, self.snap, 2, comps.revision(self.snap),
                lambda key, message: events.append((key, message)), lambda key, payload: published.update({key: payload}))
        self.assertEqual(set(errors), set(keys[:2]))
        self.assertIn("item worker failed", errors[keys[0]])
        self.assertIn("revision or worker phase changed", errors[keys[1]])
        self.assertEqual(set(published), {keys[2]})
        recorded = [json.loads(message.removeprefix("fixture:")) for key, message in events
                    if key == keys[1] and message.startswith("fixture:")]
        self.assertEqual({event["phase"] for event in recorded}, {"prepare"})

    def test_caps_follow_fixed_level_eight_ranks_in_serial_and_spawn(self):
        key = "c4-clump-mixed"
        published, events = {}, []
        with patch.object(comps, "Search", _PipelineFixtureSearch), \
                patch.object(comps, "ITEM_BUDGETS", (6,)), \
                patch.object(comps, "STRUCTURES", ({"key": "single"},)), \
                patch.object(comps, "_worker_task", _fixture_pipeline_task):
            serial = comps._scenario_payload(key, self.snap)
            errors = comps._parallel_scenarios([key], self.snap, 2, comps.revision(self.snap),
                lambda key, message: events.append(message), lambda key, payload: published.update({key: payload}))
        self.assertEqual(errors, {})
        self.assertEqual(published[key]["results"], serial["results"])
        rows = published[key]["results"]["6"]["single"]
        self.assertEqual([row["rank"] for row in rows], [1, 1, 3])
        self.assertEqual([row["metrics"]["benchmarkWins"] for row in rows], [8, 8, 7])
        self.assertEqual([row["level9Upgrade"]["board"]["metrics"]["benchmarkWins"] for row in rows], [0, 0, 12])
        self.assertTrue(all(row["level9Upgrade"]["parentId"] == row["id"] for row in rows))
        recorded = [json.loads(message.removeprefix("fixture:")) for message in events if message.startswith("fixture:")]
        validation_done = next(i for i, event in enumerate(recorded)
                               if event["phase"] == "validate" and event["event"] == "done")
        self.assertTrue(all(i > validation_done for i, event in enumerate(recorded) if event["phase"] == "cap"))

    def test_cap_for_wrong_parent_blocks_publication(self):
        publish = Mock()
        with patch.object(comps, "ITEM_BUDGETS", (6,)), \
                patch.object(comps, "STRUCTURES", ({"key": "single"},)), \
                patch.object(comps, "_worker_task", _fixture_wrong_cap_task):
            errors = comps._parallel_scenarios(["c4-clump-mixed"], self.snap, 2, comps.revision(self.snap),
                                              lambda *_: None, publish)
        self.assertIn("level-9 result does not match", errors["c4-clump-mixed"])
        publish.assert_not_called()

    def test_real_spawn_cannot_attach_validation_to_a_different_final_board_order(self):
        publish = Mock()
        with patch.object(comps, "ITEM_BUDGETS", (6,)), \
                patch.object(comps, "STRUCTURES", ({"key": "single"},)), \
                patch.object(comps, "_worker_task", _fixture_wrong_order_task):
            errors = comps._parallel_scenarios(["c1-clump-mixed"], self.snap, 2, comps.revision(self.snap),
                                              lambda *_: None, publish)
        self.assertIn("held-out results do not match", errors["c1-clump-mixed"])
        publish.assert_not_called()

    def test_real_spawn_rejects_a_result_for_the_wrong_item_job(self):
        publish = Mock()
        with patch.object(comps, "ITEM_BUDGETS", (6,)), \
                patch.object(comps, "STRUCTURES", ({"key": "single"},)), \
                patch.object(comps, "_worker_task", _fixture_wrong_item_task):
            errors = comps._parallel_scenarios(["c1-clump-mixed"], self.snap, 2, comps.revision(self.snap),
                                              lambda *_: None, publish)
        self.assertIn("item result does not match", errors["c1-clump-mixed"])
        publish.assert_not_called()

    def test_parallel_warm_parent_publishes_only_matching_completed_results(self):
        scenarios = {key: comps.scenarios()[key] for key in ("c1-clump-mixed", "c2-clump-mixed")}
        def run(keys, snap, workers, expected, progress, publish):
            self.assertTrue(comps.warm_running())
            self.assertIs(snap, self.snap)
            self.assertEqual(workers, 4)
            for key in reversed(keys):
                publish(key, comps._scenario_payload(key, snap))
            return {}
        with patch.object(comps, "scenarios", return_value=scenarios), \
                patch.object(comps, "_parallel_scenarios", side_effect=run), \
                patch.object(comps.Search, "run", return_value={}), patch.object(comps.signal, "signal"):
            self.assertEqual(comps.warm(log=lambda _: None, snap=self.snap, workers=4), 2)
            self.assertTrue(all(comps.cell_ready(self.snap).values()))
        progress = comps.progress_state()
        self.assertEqual(progress["status"], "complete")
        self.assertEqual(progress["ready"], 2)
        self.assertFalse(comps.warm_running())

    def test_one_remaining_cold_context_can_share_finalist_jobs(self):
        cold = "c1-spread-mixed"
        for key, path in comps.cell_paths(self.snap).items():
            if key != cold:
                comps._atomic(path, {"readyFixture": True})
        def run(keys, snap, workers, expected, progress, publish):
            self.assertEqual(keys, [cold])
            self.assertEqual(workers, 3)
            self.assertTrue(comps.warm_running())
            publish(cold, comps._scenario_payload(cold, snap))
            return {}
        with patch.object(comps, "_parallel_scenarios", side_effect=run) as parallel, \
                patch.object(comps.Search, "run", return_value={}), patch.object(comps.signal, "signal"):
            self.assertEqual(comps.warm(log=lambda _: None, snap=self.snap, workers=3), 1)
        parallel.assert_called_once()
        self.assertEqual(comps.progress_state()["workers"], 3)
        self.assertEqual(comps.progress_state()["ready"], 8)

    def test_parallel_failure_or_missing_artifact_never_marks_warm_complete(self):
        scenarios = {key: comps.scenarios()[key] for key in ("c1-clump-mixed", "c2-clump-mixed")}
        for errors in ({"c2-clump-mixed": "failed worker"}, {}):
            with self.subTest(errors=errors):
                for path in comps.cell_paths(self.snap).values():
                    Path(path).unlink(missing_ok=True)
                with patch.object(comps, "scenarios", return_value=scenarios), \
                        patch.object(comps, "_parallel_scenarios", return_value=errors), \
                        patch.object(comps.signal, "signal"):
                    with self.assertRaisesRegex(RuntimeError, "failed or remain missing"):
                        comps.warm(log=lambda _: None, snap=self.snap, workers=2)
                progress = comps.progress_state()
                self.assertEqual(progress["status"], "failed")
                self.assertEqual(set(progress["missing"]), set(scenarios))
                self.assertEqual(progress["errors"], errors)
                self.assertFalse(comps.warm_running())

    def test_only_and_all_ready_use_no_process_pool(self):
        with patch.object(comps.Search, "run", return_value={}), \
                patch.object(comps, "_parallel_scenarios") as parallel, patch.object(comps.signal, "signal"):
            self.assertEqual(comps.warm(log=lambda _: None, only="c1-clump-mixed", snap=self.snap, workers=4), 1)
            parallel.assert_not_called()
            for path in comps.cell_paths(self.snap).values():
                comps._atomic(path, {"readyFixture": True})
            self.assertEqual(comps.warm(log=lambda _: None, snap=self.snap, workers=4), 0)
            parallel.assert_not_called()
        self.assertEqual(comps.progress_state()["ready"], 8)

    def test_invalid_workers_or_changed_revision_cannot_publish(self):
        for workers in (0, 9, -1, True, 1.5):
            with self.subTest(workers=workers), self.assertRaisesRegex(ValueError, "workers must be"):
                comps.warm(log=lambda _: None, snap=self.snap, workers=workers)
        payload = {"key": "c1-clump-mixed", "revision": "wrong", "baselineRevision": tft.snapshot_revision(self.snap)}
        with patch.object(comps, "_scenario_payload", return_value=payload), patch.object(comps.signal, "signal"):
            with self.assertRaisesRegex(RuntimeError, "revision changed"):
                comps.warm(log=lambda _: None, only="c1-clump-mixed", snap=self.snap)
        self.assertIsNone(comps.cached_scenario("c1-clump-mixed", snap=self.snap))
        self.assertEqual(comps.progress_state()["status"], "failed")

    def test_metadata_exposes_planning_assumptions_and_the_shared_budget(self):
        meta = comps.api_meta(self.snap)
        self.assertEqual(meta["boardSize"], 8)
        self.assertEqual(meta["itemBudgets"], list(range(6, 13)))
        self.assertEqual(meta["defaultItemBudget"], 9)
        self.assertEqual(next(profile["level9FiveCostStar"] for profile in meta["profiles"] if profile["key"] == "c4"), 2)
        self.assertEqual(meta["baselineRevision"], tft.snapshot_revision(self.snap))
        self.assertEqual(len(comps.scenarios()), 8)
        self.assertTrue(any("not win probability" in limitation for limitation in meta["limitations"]))
        self.assertEqual(meta["methodology"]["evaluationModel"], "symmetric-reference-pool-v1")
        self.assertNotIn("balanceScore", meta["methodology"])


if __name__ == "__main__":
    unittest.main()
