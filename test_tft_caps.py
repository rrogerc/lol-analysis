"""Level-nine transitions preserve the selected level-eight plan and items."""
from collections import Counter
from copy import deepcopy
import unittest
from unittest.mock import patch

import tft
import tft_caps as caps
import tft_comps as comps
import tft_theory
from tft_board import ELDER_DRAGON
from tft_comp_traits import RIFTBEAST, resolve_board_traits


class _Team:
    def __init__(self, snap, score=None):
        self.snap = snap
        self.score = score or (lambda members, selected, split, policy: 10.0)
        self.calls = []
        self.stats = Counter()

    def result(self, members, selected, split="theory", policy="broad", *, tank="TFT18_Amumu"):
        score = self.score(members, selected, split, policy)
        extra = score if isinstance(score, dict) else {"theoryScore": score}
        score, dps = extra["theoryScore"], extra.get("damageDps", 9.0)
        rows = []
        for scenario in tft_theory.scenarios("clump"):
            metrics = tft_theory.capacity_metrics(score / dps, dps, scenario["incomingDps"])
            order = sorted((m["api"] for m in members if comps._frontliner(self.snap.units[m["api"]])),
                           key=lambda api: (api != tank, api))
            if scenario["targeting"] == "secondary-first" and len(order) > 1:
                order[0], order[1] = order[1], order[0]
            targets = [order[0], order[min(1, len(order) - 1)], order[0]] if order else [None] * 3
            rows.append({**scenario, **metrics, "score": score,
                         "pressureTargetOrder": order, "initialPressureTargets": targets,
                         "plannedMeasurementWindow": metrics["protectionTime"],
                         "measurementWindow": metrics["protectionTime"], "frontlineCollapsed": True,
                         "incomingBudget": metrics["frontlineEhp"],
                         "spentPressure": metrics["frontlineEhp"],
                         "deniedPressure": 0.0, "unspentPressure": 0.0})
        return {"metrics": {**tft_theory.summarize(rows), "theoryScore": score, "damageDps": dps},
                "units": {member["api"]: {"damage": 10.0, "dps": dps / len(members), "aliveTime": 7.0}
                          for member in members},
                "evaluationModel": tft_theory.MODEL, "modelRevision": tft_theory.revision(self.snap),
                "scenarios": rows, "profileCount": len(rows),
                "itemBudget": sum(len(o["items"]) for o in selected.values())}

    def evaluate_many(self, members, effects, allocations, carry, tank, *, split, details):
        assert split == "theory" and details is False
        self.calls.append({"kind": "compact", "split": split, "members": deepcopy(members),
                           "allocations": deepcopy(allocations)})
        return [self.result(members, selected, split, tank=tank) for selected in allocations]

    def evaluate(self, members, effects, selected, carry, tank, *, split):
        assert split == "theory"
        self.calls.append({"kind": "full", "split": split,
                           "members": deepcopy(members), "selected": deepcopy(selected)})
        return self.result(members, selected, split, tank=tank)


class _Loadouts:
    def __init__(self):
        self.calls = []

    def loadout(self, api, star, effects, items, alpha=False):
        self.calls.append((api, star, tuple(items), alpha))
        return {"items": tuple(sorted(items)), "count": len(items), "dps": 12345.0,
                "frontline": 98765.0, "alpha": bool(alpha), "itemBurn": False, "infernoBurn": False}


class TestCaps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def api(self, name):
        return self.snap.unit(name)["api"]

    def fixture(self, *, score=None, extra_items=(), rift=0, main_count=3, fourth_unit="Lillia"):
        names = ["Ahri", "Amumu", "Aphelios", fourth_unit, "Leona", "Karma", "Varus", "Rakan"]
        if rift:
            names[2] = "Brambleback"
        if rift > 1:
            names[3] = "Sentinel"
        roster = tuple(sorted(self.api(name) for name in names))
        carry, tank = self.api("Ahri"), self.api("Amumu")
        search = object.__new__(comps.Search)
        search.snap = self.snap
        search.profile = dict(comps.PROFILES["c4"], level=8, boardSlots=8, maxFiveCosts=0)
        search.progress = lambda message: None
        search.geometry = "clump"
        search.units = {api: unit for api, unit in self.snap.units.items() if unit["cost"] < 5}
        search.team = _Team(self.snap, score)
        search.evaluator = _Loadouts()
        members = [{"api": api, "star": 2} for api in roster]
        selected = {api: search.evaluator.loadout(api, 2, [], ()) for api in roster}
        selected[carry] = search.evaluator.loadout(carry, 2, [],
            ("DA_RedBuff",) + ("DA_Deathblade",) * (main_count - 1))
        selected[tank] = search.evaluator.loadout(tank, 2, [], ("DA_WarmogsArmor",) * main_count)
        selected[self.api("Karma")] = search.evaluator.loadout(self.api("Karma"), 2, [], extra_items)
        resolved = resolve_board_traits(self.snap, members)
        budget = sum(option["count"] for option in selected.values())
        result = _Team(self.snap).result(members, selected)
        row = search.compose((roster, carry, tank), {"members": members, "traits": resolved},
                             budget, caps.arrangement(self.snap, selected, carry, tank),
                             search.allocation(selected, result))
        row.update(rank=4, itemAnalysis={"parentOnly": [1, 2, 3]})
        search.validate_boards([row])
        search.evaluator.calls.clear()
        return search, row

    def five_pool(self, *names):
        allowed = {self.api(name) for name in names}
        transitions = caps._transitions

        def limited(*args):
            return [(removed, added) for removed, added in transitions(*args) if set(added) <= allowed]

        # Narrow the fixture's transitions while retaining real legality checks.
        return patch.object(caps, "_transitions", side_effect=limited)

    def test_cap_preserves_primal_history_and_passes_it_to_both_score_paths(self):
        search, parent = self.fixture()
        # The trait is currently inactive on this roster, but a previously
        # chosen blessing must not disappear from the upgrade plan's history.
        parent["primalHistory"] = ["turtle"]
        original = deepcopy(parent)

        class RetainedTeam(_Team):
            def evaluate_many(self, *args, required_primal=None, **kwargs):
                self.required_compact = required_primal
                return super().evaluate_many(*args, **kwargs)

            def evaluate(self, *args, required_primal=None, **kwargs):
                self.required_full = required_primal
                return super().evaluate(*args, **kwargs)

        search.team = RetainedTeam(self.snap)
        with self.five_pool("Taric"):
            result = caps.build_upgrade(search, parent)
        self.assertEqual(search.team.required_compact, ["turtle"])
        self.assertEqual(search.team.required_full, ["turtle"])
        self.assertEqual(result["board"]["primalHistory"], ["turtle"])
        self.assertNotIn("primal", result["board"], "inactive trait grants no combat blessing")
        self.assertEqual(parent, original)

    @staticmethod
    def items(row):
        return Counter(item for unit in row["units"] for item in unit["itemApis"])

    def test_nine_slot_cap_keeps_parent_mains_items_stars_and_rank(self):
        search, parent = self.fixture()
        before = deepcopy(parent)
        old_profile, old_units = deepcopy(search.profile), dict(search.units)
        with self.five_pool("Alune", "Taric"):
            upgrade = caps.build_upgrade(search, parent)
        board = upgrade["board"]
        self.assertEqual(parent, before)
        self.assertEqual(search.profile, old_profile)
        self.assertEqual(search.units, old_units)
        self.assertEqual(upgrade["parentId"], parent["id"])
        self.assertEqual((board["level"], board["boardSlots"], board["slotsUsed"], board["unitCount"]), (9, 9, 9, 9))
        self.assertEqual((board["mainCarry"], board["mainTank"]), (parent["mainCarry"], parent["mainTank"]))
        old = {unit["api"]: unit for unit in parent["units"]}
        added = {unit["api"] for unit in upgrade["transition"]["added"]}
        for unit in board["units"]:
            if unit["api"] in added:
                self.assertEqual((unit["cost"], unit["star"]), (5, 2))
            else:
                self.assertEqual((unit["star"], unit["itemApis"]), (old[unit["api"]]["star"], old[unit["api"]]["itemApis"]))
        self.assertEqual(self.items(board), self.items(parent))
        self.assertEqual(len(added), 2)
        self.assertEqual(upgrade["selection"]["fiveCostStar"], 2)
        sold_gold = sum(unit["cost"] * (1, 3, 9)[unit["star"] - 1] for unit in upgrade["transition"]["removed"])
        self.assertEqual(board["purchaseGold"], parent["purchaseGold"] - sold_gold + 15 * len(added))
        for call in search.team.calls:
            self.assertTrue(all(member["star"] == 2 for member in call["members"]
                                if self.snap.units[member["api"]]["cost"] == 5),
                            "Every capacity comparison and final measurement uses two-star five-costs")
        self.assertTrue(all(star == 2 for api, star, _, _ in search.evaluator.calls if api in added))
        self.assertNotIn("rank", board)
        self.assertNotIn("level9Upgrade", board)
        self.assertNotIn("itemAnalysis", board)
        self.assertEqual(board["itemPolicy"], caps.ITEM_POLICY)
        self.assertEqual(board["metrics"]["damageDps"], 9.0)
        self.assertGreater(board["screening"]["damageDps"], 10000)
        self.assertEqual(len(search.evaluator.calls), 9)
        self.assertTrue(all(call["kind"] == "compact" for call in search.team.calls[:-1]))
        self.assertEqual(search.team.calls[-1]["split"], "theory")
        self.assertEqual(board["consistency"]["status"], "passed")
        self.assertNotIn("validation", board)
        self.assertNotIn("assumptionCheck", board)

    def test_elder_is_one_champion_using_two_slots_and_two_riftbeasts(self):
        def score(members, selected, split, policy):
            if split != "theory":
                return 5
            return 12 if selected.get(ELDER_DRAGON, {}).get("alpha") else 9
        search, parent = self.fixture(score=score, rift=1)
        with self.five_pool("Elder Dragon"):
            upgrade = caps.build_upgrade(search, parent)
        board, transition = upgrade["board"], upgrade["transition"]
        self.assertEqual((len(board["units"]), board["slotsUsed"]), (8, 9))
        self.assertEqual(len(transition["removed"]), 1)
        self.assertEqual([unit["api"] for unit in transition["added"]], [ELDER_DRAGON])
        self.assertEqual(transition["added"][0]["slotCost"], 2)
        self.assertEqual(transition["added"][0]["star"], 2)
        rift = next(trait for trait in board["traits"] if trait["api"] == RIFTBEAST)
        self.assertEqual((rift["count"], rift["breakpoint"], rift["active"]), (3, 3, True))
        change = next(trait for trait in transition["traitChanges"] if trait["api"] == RIFTBEAST)
        self.assertEqual((change["beforeCount"], change["afterCount"], change["beforeActive"], change["afterActive"]),
                         (1, 3, False, True))
        self.assertEqual(board["alphaHolder"], "elderdragon")
        self.assertEqual(upgrade["selection"]["fiveCostSlots"], 2)

    def test_all_splits_of_sold_items_are_compared_and_retained_holders_are_fixed(self):
        freed = ("DA_JeweledGauntlet", "DA_NashorsTooth", "DA_SpearOfShojin")
        karma, alune, taric = (self.api(name) for name in ("Karma", "Alune", "Taric"))
        def score(members, selected, split, policy):
            if split != "theory":
                return 2
            return 11 if (karma not in selected and selected.get(taric, {}).get("items") == ("DA_NashorsTooth",)) else 6
        search, parent = self.fixture(score=score, extra_items=freed)
        with self.five_pool("Alune", "Taric"):
            upgrade = caps.build_upgrade(search, parent)
        batch = next(call for call in search.team.calls if call["kind"] == "compact"
                     and karma not in call["allocations"][0] and alune in call["allocations"][0] and taric in call["allocations"][0])
        self.assertEqual(len(batch["allocations"]), 8)
        self.assertEqual(len({caps.identity(trial) for trial in batch["allocations"]}), 8)
        original = {unit["api"]: unit for unit in parent["units"]}
        for trial in batch["allocations"]:
            self.assertEqual(Counter(item for api in (alune, taric) for item in trial[api]["items"]), Counter(freed))
            for api in set(original) - {karma}:
                self.assertEqual(tuple(original[api]["itemApis"]), trial[api]["items"])
        self.assertEqual(upgrade["transition"]["removed"][0]["api"], karma)
        self.assertEqual(Counter(t["itemApi"] for t in upgrade["transition"]["itemTransfers"]), Counter(freed))
        self.assertTrue(all(t["fromApi"] == karma and t["toApi"] in (alune, taric)
                            for t in upgrade["transition"]["itemTransfers"]))
        self.assertEqual(self.items(upgrade["board"]), self.items(parent))

    def test_sold_items_can_change_the_secondary_role_structure(self):
        karma, taric = self.api("Karma"), self.api("Taric")
        def score(members, selected, split, policy):
            if split != "theory":
                return 3
            return 11 if karma not in selected and len(selected.get(taric, {}).get("items", ())) == 3 else 6
        search, parent = self.fixture(score=score, extra_items=("DA_JeweledGauntlet", "DA_NashorsTooth", "DA_SpearOfShojin"))
        with self.five_pool("Alune", "Taric"):
            upgrade = caps.build_upgrade(search, parent)
        self.assertEqual(parent["structure"], "duoCarry")
        self.assertEqual(upgrade["board"]["structure"], "duoTank")
        self.assertEqual(upgrade["board"]["mainTank"], parent["mainTank"])

    def test_legal_allocations_retain_main_priority_and_at_most_one_secondary_per_role(self):
        carry, tank, alune, ashe = (self.api(name) for name in ("Ahri", "Amumu", "Alune", "Ashe"))
        selected = {carry: {"items": ("DA_Deathblade",) * 2}, tank: {"items": ("DA_WarmogsArmor",) * 2},
                    alune: {"items": ("DA_NashorsTooth",) * 2}}
        legal = lambda trial: caps._legal_allocation(self.snap, trial, carry, tank,
                                                    sum(len(o["items"]) for o in trial.values()))
        self.assertTrue(legal(selected))
        self.assertFalse(legal(dict(selected, **{alune: {"items": ("DA_NashorsTooth",) * 3}})))
        self.assertFalse(legal(dict(selected, **{carry: {"items": ("DA_Deathblade",)}})))
        self.assertFalse(legal(dict(selected, **{ashe: {"items": ("DA_NashorsTooth",) * 2}})))
        self.assertFalse(legal(dict(selected, **{alune: {"items": ("DA_NashorsTooth",) * 4}})))

    def test_duplicate_freed_items_do_not_duplicate_equivalent_allocations(self):
        search, row = self.fixture(extra_items=("DA_NashorsTooth",) * 3)
        parent, carry, tank = caps._parent_state(search, row)
        removed, added = self.api("Karma"), (self.api("Alune"), self.api("Taric"))
        members = [{"api": api, "star": 2} for api in (set(parent) - {removed}) | set(added)]
        resolved = resolve_board_traits(self.snap, members)
        choices = caps._allocations(self.snap, parent, removed, added, resolved, carry, tank, row["itemCount"])
        self.assertEqual(len(choices), 4)
        self.assertEqual({len(trial[added[0]]["items"]) for trial in choices}, {0, 1, 2, 3})

    def test_greater_capacity_overrides_two_legendary_slot_preference(self):
        def score(members, selected, split, policy):
            if split != "theory":
                return 1
            fives = sum(self.snap.units[m["api"]]["cost"] == 5 for m in members)
            return {"theoryScore": 11 if fives == 1 else 10, "damageDps": 1.0 if fives == 1 else 100.0}
        search, parent = self.fixture(score=score)
        with self.five_pool("Alune", "Taric"):
            upgrade = caps.build_upgrade(search, parent)
        self.assertEqual(upgrade["selection"]["fiveCostSlots"], 1)
        self.assertEqual(upgrade["transition"]["removed"], [])
        self.assertEqual(len(upgrade["transition"]["added"]), 1)
        self.assertEqual(upgrade["transition"]["added"][0]["star"], 2)
        self.assertEqual(upgrade["transition"]["itemTransfers"], [])
        self.assertEqual(set(upgrade["transition"]["retained"]), {u["slug"] for u in parent["units"]})
        self.assertEqual(upgrade["theoryScoreDelta"], 1)

    def test_equal_capacity_prefers_two_slots_then_cheaper_sale_and_deterministic_order(self):
        karma = self.api("Karma")
        def score(members, selected, split, policy):
            return {"theoryScore": 10,
                    "damageDps": 1.0 if karma not in selected else 999999.0}
        search, parent = self.fixture(score=score)
        with self.five_pool("Taric", "Alune"):
            upgrade = caps.build_upgrade(search, parent)
        self.assertEqual(upgrade["selection"]["fiveCostSlots"], 2)
        self.assertEqual(upgrade["transition"]["removed"][0]["api"], karma)
        self.assertEqual(upgrade["board"]["metrics"]["damageDps"], 1.0)
        self.assertNotIn("perfectScoreBoundReached", upgrade["selection"])
        self.assertEqual(upgrade["selection"]["rostersCompared"], upgrade["selection"]["candidatesAvailable"])

    def test_continuous_scores_have_no_perfect_score_early_stop(self):
        karma = self.api("Karma")
        def score(members, selected, split, policy):
            # The first preferred transition scores above the old win ceiling;
            # a later roster still improves it by a small continuous amount.
            return 100.0 if karma not in selected else 100.001
        search, parent = self.fixture(score=score)
        with self.five_pool("Alune", "Taric", "Ashe"):
            upgrade = caps.build_upgrade(search, parent)
        self.assertEqual(upgrade["board"]["metrics"]["theoryScore"], 100.001)
        self.assertEqual(upgrade["selection"]["rostersCompared"], upgrade["selection"]["candidatesAvailable"])
        self.assertGreater(upgrade["selection"]["rostersCompared"], 1)
        self.assertNotIn("perfectScoreBoundReached", upgrade["selection"])
        self.assertEqual(upgrade["selection"]["evaluatedOn"], "theory")
        self.assertEqual(upgrade["selection"]["parentRanking"], "level8")
        self.assertEqual(upgrade["theoryScoreDelta"], 90.001)
        self.assertEqual(parent["rank"], 4)

    def test_fallback_is_compared_after_all_preferred_transitions(self):
        def score(members, selected, split, policy):
            return 1000.1 if sum(self.snap.units[m["api"]]["cost"] == 5 for m in members) == 1 else 1000.0
        search, parent = self.fixture(score=score)
        with self.five_pool("Alune", "Taric"):
            upgrade = caps.build_upgrade(search, parent)
        self.assertEqual(upgrade["selection"]["fiveCostSlots"], 1)
        self.assertEqual(upgrade["selection"]["rostersCompared"], upgrade["selection"]["candidatesAvailable"])
        self.assertTrue(all(call["split"] == "theory" for call in search.team.calls))

    def test_alpha_states_include_all_eligible_holders_only_when_active(self):
        for five_names, active in ((("Alune", "Taric"), False), (("Elder Dragon",), True)):
            with self.subTest(active=active):
                search, parent = self.fixture(rift=2)
                self.assertIsNone(parent["alphaHolder"])
                with self.five_pool(*five_names):
                    upgrade = caps.build_upgrade(search, parent)
                first = next(call for call in search.team.calls if call["kind"] == "compact")
                marked = [tuple(api for api, option in trial.items() if option["alpha"])
                          for trial in first["allocations"]]
                if active:
                    self.assertEqual(set(marked), {(self.api(name),) for name in ("Brambleback", "Sentinel", "Elder Dragon")})
                    self.assertEqual(upgrade["selection"]["fiveCostSlots"], 2)
                else:
                    self.assertEqual(marked, [()])
                    self.assertIsNone(upgrade["board"]["alphaHolder"])

    def test_removing_one_four_cost_is_allowed_but_mains_never_change(self):
        aphelios = self.api("Aphelios")
        def score(members, selected, split, policy):
            return (11 if aphelios not in selected else 10) if split == "theory" else 4
        search, parent = self.fixture(score=score)
        with self.five_pool("Alune", "Taric"):
            upgrade = caps.build_upgrade(search, parent)
        self.assertEqual(upgrade["transition"]["removed"][0]["api"], aphelios)
        self.assertEqual(upgrade["board"]["sameCostCount"], 3)
        self.assertEqual(upgrade["board"]["mainCarry"], parent["mainCarry"])
        self.assertEqual(upgrade["board"]["mainTank"], parent["mainTank"])

    def test_level_nine_upgrade_retains_three_target_cost_units(self):
        aphelios = self.api("Aphelios")
        def score(members, selected, split, policy):
            return 100 if aphelios not in selected else 10
        search, parent = self.fixture(score=score, fourth_unit="Alistar")
        self.assertEqual(parent["sameCostCount"], 3)
        with self.five_pool("Alune", "Taric"):
            upgrade = caps.build_upgrade(search, parent)
        self.assertIsNotNone(upgrade)
        self.assertEqual(upgrade["board"]["sameCostCount"], 3)
        self.assertIn(aphelios, {unit["api"] for unit in upgrade["board"]["units"]})
        for call in search.team.calls:
            self.assertIn(aphelios, {member["api"] for member in call["members"]})

    def test_empty_five_cost_pool_and_other_cost_plans_do_no_work(self):
        search, parent = self.fixture()
        with self.five_pool():
            self.assertIsNone(caps.build_upgrade(search, parent))
        self.assertEqual(search.team.calls, [])
        self.assertEqual(search.evaluator.calls, [])
        search.profile["cost"] = 3
        self.assertIsNone(caps.build_upgrade(search, {}))

    def test_invalid_parent_and_incomplete_or_wrong_suite_results_fail(self):
        search, parent = self.fixture()
        no_rank = deepcopy(parent)
        del no_rank["rank"]
        for invalid in (no_rank, dict(parent, level=9), dict(parent, itemCount=parent["itemCount"] + 1)):
            with self.subTest(invalid=invalid.get("level")), self.assertRaises(ValueError):
                caps.build_upgrade(search, invalid)
        with self.five_pool("Alune", "Taric"), patch.object(search.team, "evaluate_many", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                caps.build_upgrade(search, parent)
        for bad in (dict(parent, profileCount=6), dict(parent, metrics={"theoryScore": -1}),
                    dict(parent, metrics={"theoryScore": float("nan")}),
                    dict(parent, evaluationModel="symmetric-reference-pool-v1"),
                    dict(parent, modelRevision="different-model")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                caps._theory_score(bad, parent["profileCount"], parent)
        for field, value in {"targetCount": 1, "incomingSourceCount": 1,
                             "controlInterval": 4.0, "controlDuration": 1.5,
                             "pressureInterval": 1.0,
                             "pressureAllocation": "independent-frontline", "targeting": "invalid-focus"}.items():
            for omitted in (False, True):
                changed = deepcopy(parent)
                if omitted:
                    del changed["scenarios"][0][field]
                else:
                    changed["scenarios"][0][field] = value
                with self.subTest(field=field, omitted=omitted), self.assertRaisesRegex(ValueError, "scenario inputs"):
                    caps._theory_score(changed, parent["profileCount"], parent)

    def test_native_nine_actor_cap_uses_the_same_pressure_inputs_and_item_budget(self):
        search, parent = self.fixture()
        search.team = tft_theory.Evaluator(self.snap, "clump")
        search.evaluator = comps.Evaluator(self.snap, "clump", "mixed")
        members = [{"api": unit["api"], "star": unit["star"]} for unit in parent["units"]]
        effects = resolve_board_traits(self.snap, members)["effects"]
        selected = {unit["api"]: {"items": tuple(unit["itemApis"]), "alpha": False} for unit in parent["units"]}
        result = search.team.evaluate(members, effects, selected, self.api("Ahri"), self.api("Amumu"))
        parent.update({field: result[field] for field in
                       ("metrics", "modelRevision", "evaluationModel", "scenarios", "profileCount")})
        with patch.object(caps, "_transitions", return_value=[(None, (self.api("Alune"),))]):
            upgrade = caps.build_upgrade(search, parent)
        board = upgrade["board"]
        self.assertEqual(len(board["units"]), 9)
        alune = next(unit for unit in board["units"] if unit["api"] == self.api("Alune"))
        self.assertEqual(alune["star"], 2)
        self.assertEqual(board["modelRevision"], parent["modelRevision"])
        self.assertEqual(board["itemCount"], parent["itemCount"])
        self.assertEqual([row["key"] for row in board["scenarios"]], [row["key"] for row in parent["scenarios"]])
        self.assertEqual(board["profileCount"], len(tft_theory.scenarios("clump")))
        self.assertEqual(board["consistency"]["status"], "passed")
        self.assertEqual(upgrade["selection"]["profilesPerAllocation"], parent["profileCount"])
        self.assertNotIn("matchups", board)


if __name__ == "__main__":
    unittest.main()
