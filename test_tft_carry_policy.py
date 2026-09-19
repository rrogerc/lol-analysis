"""Composition search permits melee carries while retaining real equipped forms.

Bounded fixture scores deliberately prefer AD Nidalee beside Brambleback, so
unrestricted legality is exercised through allocation, refinement and validation.
"""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

import tft
import tft_comps as comps
from tft_comp_items import ItemSearch, identity
from tft_comp_traits import resolve_board_traits
from tft_unit_profiles import UnitProfiles
from test_tft_comps import _theory_fixture
from test_tft_loadouts import direct_loadouts
from test_tft_pairs import synthetic_item


AD_ITEMS = ("DA_Deathblade", "DA_WarmogsArmor")
AP_ITEMS = ("DA_RabadonsDeathcap", "DA_WarmogsArmor")


class _ItemScores:
    """Prefer actual melee Nidalee without running combat simulations."""

    def __init__(self, snap, members, effects, nidalee):
        self.profiles = UnitProfiles(snap, "clump")
        self.members = {member["api"]: member["star"] for member in members}
        self.effects, self.nidalee, self.calls = effects, nidalee, []

    def role(self, api, option):
        return self.profiles.spec(api, self.members[api], self.effects.get(api, []),
                                  option["items"], alpha=option.get("alpha", False))["unit"]

    def evaluate_many(self, members, effects, selections, carry, tank, *, split, details):
        if split != "theory" or details:
            raise AssertionError("item comparisons must use compact theoretical scores")
        results = []
        for selected in selections:
            self.calls.append(deepcopy(selected))
            role = self.role(self.nidalee, selected[self.nidalee])
            score = 100.0 if role["form"] == "AD" else 1.0
            results.append({"metrics": {"theoryScore": score}, "units": {},
                            "modelRevision": "carry-planning-fixture",
                            "scenarios": [{"key": str(i), "score": score} for i in range(12)]})
        return results


class TestCarryPlanning(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.apis = {name: cls.snap.unit(name)["api"] for name in
                    ("Nidalee", "Amumu", "Brambleback", "Camille", "Leona", "Sivir", "Cinderling", "Gromp", "Ahri")}
        cls.nidalee, cls.tank, cls.bramble = (cls.apis[name] for name in ("Nidalee", "Amumu", "Brambleback"))
        cls.members = [{"api": api, "star": 2} for name, api in cls.apis.items() if name != "Ahri"]
        cls.effects = resolve_board_traits(cls.snap, cls.members)["effects"]

    def selected(self, nidalee_items=AP_ITEMS):
        selected = {member["api"]: {"items": (), "alpha": False} for member in self.members}
        selected[self.nidalee].update(items=nidalee_items)
        selected[self.tank].update(items=("DA_WarmogsArmor",) * 2)
        selected[self.bramble].update(items=("DA_Deathblade",) * 2, alpha=True)
        return selected

    def search(self, *, level=8, carry=None):
        members = self.members if level == 8 else [*self.members, {"api": self.apis["Ahri"], "star": 2}]
        effects = resolve_board_traits(self.snap, members)["effects"]
        evaluator = _ItemScores(self.snap, members, effects, self.nidalee)
        search = ItemSearch(self.snap, evaluator, members, effects,
                            carry or self.bramble, self.tank, "duoCarry",
                            {self.nidalee: [{"items": ("DA_Deathblade",) * 2, "alpha": False}]},
                            level=level)
        search.pool = ("DA_Deathblade", "DA_RabadonsDeathcap", "DA_WarmogsArmor")
        return search, evaluator

    def test_only_declared_planning_levels_are_accepted(self):
        for level in (8, 9):
            self.search(level=level)
        for level in (7, 10, True, 8.0, "8", None):
            with self.subTest(level=level), self.assertRaises(ValueError):
                self.search(level=level)

    def test_actual_nidalee_items_keep_ad_melee_and_ap_backline_roles(self):
        _, evaluator = self.search()
        for items, stale, form, kind, objective, reach in (
            (AD_ITEMS, "AP", "AD", "Assassin", "fighter", 1),
            (AP_ITEMS, "AD", "AP", "Marksman", "carry", 5),
        ):
            with self.subTest(form=form):
                role = evaluator.role(self.nidalee, {"items": items, "form": stale})
                self.assertEqual((role["form"], role["kind"], role["objective"], role["range"]),
                                 (form, kind, objective, reach))

    def test_two_melee_carries_are_allowed_at_both_levels_with_either_main(self):
        for level in (8, 9):
            for carry in (self.nidalee, self.bramble):
                with self.subTest(level=level, carry=carry):
                    selected = self.selected(AD_ITEMS)
                    if level == 9:
                        selected[self.apis["Ahri"]] = {"items": (), "alpha": False}
                    search, evaluator = self.search(level=level, carry=carry)
                    search.budget, search.alpha_count = 6, 1
                    self.assertEqual(evaluator.role(self.nidalee, selected[self.nidalee])["kind"], "Assassin")
                    self.assertEqual(evaluator.role(self.bramble, selected[self.bramble])["objective"], "fighter")
                    self.assertTrue(search.legal(selected))

    def test_supported_two_carry_structure_and_item_budget_still_apply(self):
        for level in (8, 9):
            with self.subTest(level=level):
                search, _ = self.search(level=level)
                selected = self.selected(AD_ITEMS)
                if level == 9:
                    selected[self.apis["Ahri"]] = {"items": (), "alpha": False}
                search.budget, search.alpha_count = 6, 1
                self.assertTrue(search.legal(selected))
                selected[self.apis["Camille"]]["items"] = ("DA_Deathblade",) * 2
                self.assertFalse(search.legal(selected), "shared budget remains enforced")
                search.budget = 8
                self.assertFalse(search.legal(selected), "three itemized carries exceed supported arrangements")

    def test_seed_pruning_can_choose_the_higher_scoring_second_melee(self):
        def option(items=(), *, dps=1.0, frontline=0.0, alpha=False):
            return {"items": items, "count": len(items), "dps": dps,
                    "frontline": frontline, "stress": frontline, "alpha": alpha}
        libraries = {member["api"]: [option()] for member in self.members}
        libraries[self.nidalee] = [option(AD_ITEMS, dps=10000), option(AP_ITEMS)]
        libraries[self.tank] = [option(("DA_WarmogsArmor",) * 2, frontline=100)]
        libraries[self.bramble] = [option(("DA_Deathblade",) * 2, dps=100, alpha=True)]
        before = deepcopy(libraries)
        seeds = comps.allocate(self.members, libraries, self.nidalee, self.tank,
                               self.snap, require_alpha=True)["6"]["duoCarry"]
        self.assertTrue(seeds)
        self.assertTrue(any(row["selected"][self.nidalee]["items"] == AD_ITEMS for row in seeds))
        self.assertEqual(libraries, before)

    def test_single_unit_shortlist_preserves_both_equipped_forms(self):
        for name in ("Nidalee", "Gromp"):
            with self.subTest(name=name):
                api = self.apis[name]
                evaluator = comps.Evaluator(self.snap, "clump", "mixed")
                evaluator.pool = ["DA_Deathblade", "DA_RabadonsDeathcap", "DA_WarmogsArmor"]
                groups = [[{"items": []}]] + [[{"items": [item] * count} for item in evaluator.pool[:2]]
                                                for count in range(1, 4)]
                evaluator.optimal = Mock(return_value={"groups": groups})
                evaluator.fight = Mock(side_effect=lambda spec, items: {
                    "total": 10000 if "DA_Deathblade" in items else 1,
                    "aliveTime": 20, "survivalCapped": False, "stressAliveTime": 20})
                with patch.object(tft.engine(), "simulate", side_effect=AssertionError("shortlist fixture must not simulate")):
                    options = evaluator.options(api, 2, self.effects[api])
                profiles = UnitProfiles(self.snap, "clump")
                for count in (1, 2, 3):
                    forms = {profiles.spec(api, 2, self.effects[api], option["items"])["unit"]["form"]
                             for option in options if option["count"] == count}
                    self.assertEqual(forms, {"AD", "AP"})

    def native_spec(self, name="Nidalee"):
        api = self.apis[name]
        spec = UnitProfiles(self.snap, "clump").spec(api, 2, self.effects.get(api, []), ())
        spec["duration"] = 3.5
        # Eighty-four legal loadouts span three native work chunks. Every
        # score and equipped form below is independently replayed by simulate.
        spec["pool"] = [tft.item_spec(self.snap, item, tft.load_item_effects(self.snap.set_no), self.snap.units[api])
                        for item in ("DA_Deathblade", "DA_RabadonsDeathcap", "DA_WarmogsArmor",
                                     "DA_GuinsoosRageblade", "DA_InfinityEdge", "DA_JeweledGauntlet")]
        return spec

    @staticmethod
    def per_form_top(groups, top):
        expected = []
        for group in groups:
            counts, retained = {}, []
            for row in group:
                form = row[1]["form"]
                if counts.get(form, 0) < top:
                    retained.append(row)
                    counts[form] = counts.get(form, 0) + 1
            expected.append(retained)
        return expected

    def test_native_top_one_retains_real_nidalee_ad_and_ap_before_parallel_truncation(self):
        spec = self.native_spec()
        count, exhaustive = direct_loadouts(spec)
        self.assertEqual(count, 84)
        expected = self.per_form_top(exhaustive, 1)
        self.assertEqual([len(group) for group in expected], [1, 2, 2, 2])
        for workers in (1, 3, 0):
            with self.subTest(workers=workers):
                actual = tft.engine().optimize_loadouts(spec, top=1, workers=workers, preserve_forms=True)
                self.assertEqual(actual, (count, expected))
                for group in actual[1][1:]:
                    self.assertEqual({row[1]["form"] for row in group}, {"AD", "AP"})
                    self.assertEqual({row[1]["kind"] for row in group}, {"Assassin", "Marksman"})

    def test_native_default_ranking_and_non_adaptors_are_unchanged(self):
        for name in ("Nidalee", "Ahri"):
            with self.subTest(unit=name):
                spec = self.native_spec(name)
                count, exhaustive = direct_loadouts(spec)
                expected = (count, [group[:2] for group in exhaustive])
                self.assertEqual(tft.engine().optimize_loadouts(spec, top=2, workers=1), expected)
                self.assertEqual(tft.engine().optimize_loadouts(spec, top=2, workers=3, preserve_forms=False), expected)
                if name == "Ahri":
                    self.assertEqual(tft.engine().optimize_loadouts(spec, top=2, preserve_forms=True), expected)
                self.assertEqual(tft.engine().optimize_loadouts(spec, top=0, preserve_forms=True),
                                 (count, [[], [], [], []]))

    def test_native_form_preservation_keeps_unique_constraints_and_api_ties(self):
        spec = self.native_spec()
        spec["duration"] = 0.1
        spec["pool"] = [synthetic_item("z_ad", stats=[["adPct", 0.5]]),
                        synthetic_item("a_ad", True, stats=[["adPct", 0.5]]),
                        synthetic_item("z_ap", stats=[["ap", 50]]),
                        synthetic_item("a_ap", True, stats=[["ap", 50]])]
        count, exhaustive = direct_loadouts(spec)
        expected = self.per_form_top(exhaustive, 2)
        for workers in (1, 4):
            with self.subTest(workers=workers):
                actual = tft.engine().optimize_loadouts(spec, top=2, workers=workers, preserve_forms=True)
                self.assertEqual(actual, (count, expected))
                for group in actual[1]:
                    self.assertTrue(all(row[0].count(index) <= 1 for row in group for index in (1, 3)))

    def test_item_optimizer_accepts_ad_replacement_exchange_and_anchor_at_eight(self):
        initial = self.selected()
        initial[self.nidalee].update(form="AP", objective="carry", kind="Marksman", range=5)
        before = deepcopy(initial)
        search, evaluator = self.search()
        selected, result, evidence = search.optimize([initial])
        self.assertEqual(initial, before)
        self.assertNotEqual(identity(selected), identity(initial))
        self.assertEqual(result["metrics"]["theoryScore"], 100.0)
        self.assertTrue(evidence["converged"])
        self.assertEqual(evaluator.role(self.nidalee, selected[self.nidalee])["form"], "AD")
        self.assertTrue(all(search.legal(trial) for trial in evaluator.calls))
        replacement = search.changed(initial, self.nidalee, AD_ITEMS)
        self.assertEqual(replacement[self.nidalee]["form"], "AP", "exercise the stale presentation label")
        self.assertTrue(search.legal(replacement))
        exchange = search.changed(replacement, self.bramble, ("DA_Deathblade", "DA_RabadonsDeathcap"))
        self.assertTrue(search.legal(exchange))
        self.assertIn(identity(exchange), [identity(trial) for trial in search.moves(initial)])

    def test_optimizer_accepts_a_second_melee_seed_at_nine(self):
        search, evaluator = self.search(level=9)
        initial = self.selected(AD_ITEMS)
        initial[self.apis["Ahri"]] = {"items": (), "alpha": False}
        selected, result, _ = search.optimize([initial])
        self.assertEqual(result["metrics"]["theoryScore"], 100.0)
        self.assertEqual(evaluator.role(self.nidalee, selected[self.nidalee])["form"], "AD")
        self.assertTrue(search.legal(selected))

    def test_double_melee_still_requires_antiheal_before_any_score_is_requested(self):
        members = [member for member in self.members if member["api"] != self.apis["Cinderling"]]
        members.append({"api": self.apis["Ahri"], "star": 2})
        effects = resolve_board_traits(self.snap, members)["effects"]
        evaluator = _ItemScores(self.snap, members, effects, self.nidalee)
        search = ItemSearch(self.snap, evaluator, members, effects, self.bramble, self.tank, "duoCarry", level=8)
        search.pool = ("DA_Deathblade", "DA_WarmogsArmor", "DA_SunfireCape")
        selected = self.selected(AD_ITEMS)
        del selected[self.apis["Cinderling"]]
        selected[self.apis["Ahri"]] = {"items": (), "alpha": False}
        with self.assertRaisesRegex(ValueError, "illegal"):
            search.optimize([selected])
        self.assertEqual(evaluator.calls, [])
        selected[self.tank]["items"] = ("DA_WarmogsArmor", "DA_SunfireCape")
        self.assertTrue(search.legal(selected), "a usable tank Sunfire supplies the missing antiheal")

    def publication_row(self, items=AP_ITEMS, *, level=8):
        selected = self.selected(items)
        if level == 9:
            selected[self.apis["Ahri"]] = {"items": (), "alpha": False}
        profiles = UnitProfiles(self.snap, "clump")
        roles = {api: profiles.spec(api, 2, self.effects.get(api, []), option["items"],
                                   alpha=option["alpha"])["unit"] for api, option in selected.items()}
        fronts = {api for api, role in roles.items() if role["objective"] in ("tank", "fighter")
                  or role["kind"] == "Assassin"}
        return {"id": "carry-planning-publication", "rank": 1, "level": level,
                "mainCarry": tft.unit_slug(self.snap.units[self.nidalee]),
                "mainTank": tft.unit_slug(self.snap.units[self.tank]),
                "alphaHolder": tft.unit_slug(self.snap.units[self.bramble]),
                "units": [{"api": api, "slug": tft.unit_slug(self.snap.units[api]), "star": 2,
                           "itemApis": list(option["items"]), "form": "AP", "kind": "Marksman",
                           "objective": "carry", "frontline": api in fronts}
                          for api, option in selected.items()],
                **_theory_fixture(self.snap, 12.0, frontline=fronts, tank=self.tank)}

    def test_publication_allows_double_melee_and_ignores_old_limit_metadata(self):
        search = comps.Search(self.snap, comps.PROFILES["c4"], "clump", "mixed")
        for items in (AP_ITEMS, AD_ITEMS):
            with self.subTest(items=items):
                row = self.publication_row(items)
                row["maxMeleeCarries"] = 1
                search.validate_boards([row])
                self.assertEqual(row["consistency"]["status"], "passed")

    def test_publication_allows_two_at_nine_but_rejects_mislabeled_level_eight(self):
        row = self.publication_row(AD_ITEMS, level=9)
        profile = dict(comps.PROFILES["c4"], level=9, boardSlots=9, maxFiveCosts=2)
        comps.Search(self.snap, profile, "clump", "mixed").validate_boards([row])
        self.assertEqual(row["consistency"]["status"], "passed")
        with self.assertRaisesRegex(ValueError, "planning level"):
            comps.Search(self.snap, comps.PROFILES["c4"], "clump", "mixed").validate_boards([row])

    def test_new_metadata_has_no_melee_limit_and_keeps_antiheal_requirement(self):
        metadata = comps.api_meta(self.snap)
        for profile in metadata["profiles"]:
            self.assertNotIn("maxMeleeCarries", profile)
            self.assertNotIn("level9MaxMeleeCarries", profile)
            self.assertNotIn("melee", profile["description"])
            self.assertIn("Antiheal required", profile["description"])
        self.assertFalse(any("melee carry" in note for note in metadata["limitations"]))


if __name__ == "__main__":
    unittest.main()
