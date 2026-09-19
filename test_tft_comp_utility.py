"""Confirmed antiheal must survive item edits and resulting roster changes."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import tft
from tft_comp_traits import resolve_board_traits
from tft_comp_utility import AntihealPolicy, CINDERLING, INFERNO
from tft_unit_profiles import UnitProfiles


SNAP = tft.load_snapshot(18, "18.1d")


def board(names, items=None, *, alpha=None, profiles=None):
    items = items or {}
    members = [{"api": SNAP.unit(name)["api"], "star": 2} for name in names]
    alpha_api = SNAP.unit(alpha)["api"] if alpha else None
    effects = resolve_board_traits(SNAP, members, alpha_holder=alpha_api)["effects"]
    selected = {member["api"]: {"items": tuple(items.get(SNAP.units[member["api"]]["name"], ())),
                                "alpha": member["api"] == alpha_api} for member in members}
    return AntihealPolicy(SNAP, members, effects, profiles=profiles), selected


class TestAntihealPolicy(unittest.TestCase):
    def setUp(self):
        for name in ("simulate", "measure_response", "measure_theory_team"):
            self.enterContext(patch.object(tft.engine(), name, side_effect=AssertionError("policy must not simulate")))

    def test_a_board_without_a_confirmed_source_is_illegal(self):
        profiles = SimpleNamespace(spec=Mock(side_effect=AssertionError("no potential source needs a spec")))
        policy, selected = board(["Ahri", "Malphite"], {"Ahri": ["DA_GuinsoosRageblade"]}, profiles=profiles)
        self.assertFalse(policy.legal(selected))
        self.assertEqual(policy.sources(selected), [])
        self.assertFalse(policy.legal({}))
        self.assertFalse(policy.has_source("unknown", {"items": ("DA_RedBuff",)}))

    def test_inferno_requires_an_active_breakpoint_in_resolved_effects(self):
        one, selected_one = board(["Amumu", "Ahri"])
        two, selected_two = board(["Amumu", "Shen"])
        self.assertFalse(one.legal(selected_one))
        self.assertTrue(two.legal(selected_two))
        self.assertTrue(all(source["type"] == "trait" and source["api"] == INFERNO
                            and source["wound"] == .33 for source in two.sources(selected_two)))
        # An active trait is not inferred from the roster if the resolved
        # effects actually supplied to the evaluator do not include it.
        absent = AntihealPolicy(SNAP, [{"api": api, "star": 2} for api in selected_two], {})
        self.assertFalse(absent.legal(selected_two))

    def test_cinderling_base_ability_is_twenty_percent_wound_without_alpha(self):
        profiles = SimpleNamespace(spec=Mock(side_effect=AssertionError("base ability needs no loadout resolution")))
        policy, selected = board(["Cinderling", "Ahri"], profiles=profiles)
        self.assertFalse(selected[CINDERLING]["alpha"])
        self.assertTrue(policy.has_source(CINDERLING, selected[CINDERLING]))
        self.assertTrue(policy.legal(selected))
        sources = policy.sources(selected)
        self.assertEqual(len(sources), 1)
        self.assertEqual((sources[0]["type"], sources[0]["api"], sources[0]["unitApi"],
                          sources[0]["wound"], sources[0]["duration"]),
                         ("ability", CINDERLING, CINDERLING, .2, 4))

    def test_alpha_brambleback_and_elder_ignite_are_not_assumed_to_wound(self):
        policy, selected = board(["Brambleback", "Elder Dragon", "Ahri"], alpha="Brambleback")
        self.assertTrue(selected[SNAP.unit("Brambleback")["api"]]["alpha"])
        self.assertFalse(policy.legal(selected))
        self.assertEqual(policy.sources(selected), [])
        for api, option in selected.items():
            self.assertFalse(policy.has_source(api, option))

    def test_only_named_confirmed_burn_sources_are_accepted(self):
        policy, selected = board(["Ahri"])
        api = SNAP.unit("Ahri")["api"]
        policy = AntihealPolicy(SNAP, [{"api": api, "star": 2}],
                                 {api: [{"api": "unknown-burn", "burnOnHit": [.01, 4]}]})
        self.assertFalse(policy.legal(selected))
        # The Wound word in an immunity tooltip is not a Wound source.
        self.assertFalse(policy.has_source(api, {"items": ("DA_Artifact_Mittens",)}))
        self.assertFalse(policy.has_source(api, {"items": ("DA_RedBuffRadiant",)}))

    def test_standard_on_hit_items_use_their_pinned_wound_rows(self):
        for item, duration in (("DA_Morellonomicon", 10), ("DA_RedBuff", 5)):
            with self.subTest(item=item):
                policy, selected = board(["Ahri", "Malphite"], {"Ahri": [item]})
                self.assertTrue(policy.legal(selected))
                sources = policy.sources(selected)
                self.assertEqual(len(sources), 1)
                self.assertEqual((sources[0]["type"], sources[0]["api"], sources[0]["unitApi"],
                                  sources[0]["wound"], sources[0]["duration"]),
                                 ("item", item, SNAP.unit("Ahri")["api"], .33, duration))

    def test_sunfire_requires_modeled_aura_reach(self):
        ranged, ranged_items = board(["Ahri", "Malphite"], {"Ahri": ["DA_SunfireCape"]})
        melee, melee_items = board(["Ahri", "Malphite"], {"Malphite": ["DA_SunfireCape"]})
        self.assertFalse(ranged.legal(ranged_items))
        self.assertTrue(melee.legal(melee_items))

    def test_nidalee_sunfire_follows_equipped_form_not_base_range(self):
        for power, expected in (("DA_Deathblade", True), ("DA_RabadonsDeathcap", False)):
            with self.subTest(power=power):
                policy, selected = board(["Nidalee", "Malphite"],
                                         {"Nidalee": ["DA_SunfireCape", power, power]})
                self.assertEqual(policy.legal(selected), expected)
                self.assertEqual(bool(policy.sources(selected)), expected)

    def test_an_item_name_without_a_modeled_application_is_not_enough(self):
        real = UnitProfiles(SNAP, "clump")
        def without_burn(*args, **kwargs):
            spec = deepcopy(real.spec(*args, **kwargs))
            for item in spec["items"]:
                item.pop("burnOnHit", None)
            return spec
        profiles = SimpleNamespace(spec=Mock(side_effect=without_burn))
        policy, selected = board(["Ahri", "Malphite"], {"Ahri": ["DA_Morellonomicon"]}, profiles=profiles)
        self.assertFalse(policy.legal(selected))

    def test_repeated_and_reordered_loadouts_reuse_native_resolution(self):
        profiles = Mock(wraps=UnitProfiles(SNAP, "clump"))
        policy, selected = board(["Ahri", "Malphite"],
                                 {"Ahri": ["DA_RedBuff", "DA_RabadonsDeathcap"]}, profiles=profiles)
        api = SNAP.unit("Ahri")["api"]
        compose = tft.engine().compose_fx
        with patch.object(tft.engine(), "compose_fx", wraps=compose) as resolve:
            for _ in range(3):
                self.assertTrue(policy.legal(selected))
                policy.sources(selected)
                self.assertTrue(policy.has_source(api, dict(selected[api], items=tuple(reversed(selected[api]["items"])))))
            self.assertEqual(profiles.spec.call_count, 1)
            self.assertEqual(resolve.call_count, 1)

    def test_item_changes_cannot_remove_the_last_source(self):
        policy, selected = board(["Ahri", "Malphite"], {"Ahri": ["DA_Morellonomicon", "DA_SpearOfShojin"]})
        api = SNAP.unit("Ahri")["api"]
        removed = dict(selected, **{api: dict(selected[api], items=("DA_SpearOfShojin",))})
        replaced = dict(selected, **{api: dict(selected[api], items=("DA_RabadonsDeathcap", "DA_SpearOfShojin"))})
        alternate = dict(selected, **{api: dict(selected[api], items=("DA_RedBuff", "DA_SpearOfShojin"))})
        self.assertTrue(policy.legal(selected))
        self.assertFalse(policy.legal(removed))
        self.assertFalse(policy.legal(replaced))
        self.assertTrue(policy.legal(alternate))

    def test_support_sale_rechecks_inferno_and_native_sources(self):
        inferno, before = board(["Amumu", "Shen", "Ahri"])
        broken, after = board(["Amumu", "Ahri"])
        self.assertTrue(inferno.legal(before))
        self.assertFalse(broken.legal(after))
        native, before = board(["Cinderling", "Ahri", "Malphite"])
        removed, after = board(["Ahri", "Malphite"])
        self.assertTrue(native.legal(before))
        self.assertFalse(removed.legal(after))

    def test_level_eight_and_nine_both_require_a_source(self):
        names = ["Ahri", "Sivir", "Malphite", "Nidalee", "Warwick", "Brambleback", "Taric", "Ashe", "Ornn"]
        for count in (8, 9):
            with self.subTest(count=count):
                missing, empty = board(names[:count])
                present, equipped = board(names[:count], {"Sivir": ["DA_RedBuff"]})
                self.assertFalse(missing.legal(empty))
                self.assertTrue(present.legal(equipped))

    def test_source_reports_are_auditable_independent_values(self):
        policy, selected = board(["Ahri", "Malphite"], {"Ahri": ["DA_RedBuff", "DA_RedBuff"]})
        first = policy.sources(selected)
        self.assertEqual(len(first), 1)
        self.assertTrue(all(key in first[0] for key in ("type", "api", "unitApi", "name", "wound", "duration")))
        first[0]["wound"] = 0
        self.assertEqual(policy.sources(selected)[0]["wound"], .33)
        self.assertTrue(policy.legal(selected))


class TestAntihealSearchIntegration(unittest.TestCase):
    def test_seed_pruning_keeps_a_lower_scoring_antiheal_loadout(self):
        import tft_comps
        policy, selected = board(["Ahri", "Malphite", "Leona", "Camille", "Gromp", "Xayah", "Nidalee", "Zyra"])
        carry, tank = SNAP.unit("Ahri")["api"], SNAP.unit("Malphite")["api"]
        members = [{"api": api, "star": 2} for api in selected]
        def option(items=(), damage=1, front=1):
            return {"items": items, "count": len(items), "alpha": False,
                    "dps": damage, "frontline": front, "stress": 0, "utility": 0}
        libraries = {api: [option()] for api in selected}
        libraries[carry] = [option(("DA_RabadonsDeathcap",) * 3, damage=10000),
                            option(("DA_Morellonomicon", "DA_RabadonsDeathcap", "DA_SpearOfShojin"))]
        libraries[tank] = [option(("DA_WarmogsArmor",) * 3, front=100)]
        rows = tft_comps.allocate(members, libraries, carry, tank, SNAP)["6"]["single"]
        self.assertTrue(rows)
        self.assertTrue(all(policy.legal(row["selected"]) for row in rows))
        self.assertTrue(all("DA_Morellonomicon" in row["selected"][carry]["items"] for row in rows))

    def test_item_search_cannot_trade_its_only_antiheal_for_a_higher_score(self):
        from test_tft_comp_items import TeamFixture
        from tft_comp_items import ItemSearch
        policy, selected = board(["Ahri", "Malphite"],
            {"Ahri": ("DA_RedBuff", "DA_RabadonsDeathcap"), "Malphite": ("DA_WarmogsArmor",) * 2})
        carry, tank = SNAP.unit("Ahri")["api"], SNAP.unit("Malphite")["api"]
        members = [{"api": api, "star": 2} for api in selected]
        evaluator = TeamFixture(lambda trial: 1 if policy.legal(trial) else 1000)
        search = ItemSearch(SNAP, evaluator, members, policy.effects, carry, tank, "single")
        search.pool = ("DA_RedBuff", "DA_RabadonsDeathcap", "DA_WarmogsArmor")
        winner, result, _ = search.optimize([selected])
        self.assertEqual(result["metrics"]["theoryScore"], 1)
        self.assertTrue(policy.legal(winner))
        removed = search.changed(selected, carry, ("DA_RabadonsDeathcap",) * 2)
        self.assertFalse(search.legal(removed))

    def test_level_nine_allocations_cannot_sell_the_only_inferno_partner(self):
        from tft_caps import _allocations
        names = ["Ahri", "Amumu", "Aphelios", "Lillia", "Leona", "Karma", "Varus", "Rakan"]
        _, selected = board(names, {"Ahri": ("DA_RabadonsDeathcap",) * 3,
                                   "Amumu": ("DA_WarmogsArmor",) * 3})
        parent = {api: {"star": 2, "itemApis": list(option["items"])} for api, option in selected.items()}
        carry, tank = SNAP.unit("Ahri")["api"], SNAP.unit("Amumu")["api"]
        added = tuple(SNAP.unit(name)["api"] for name in ("Ashe", "Ivern"))
        for sold, allowed in (("Varus", False), ("Karma", True)):
            removed = SNAP.unit(sold)["api"]
            members = [{"api": api, "star": 2} for api in (*sorted(set(parent) - {removed}), *added)]
            resolved = resolve_board_traits(SNAP, members)
            trials = _allocations(SNAP, parent, removed, added, resolved, carry, tank, 6)
            with self.subTest(sold=sold):
                self.assertEqual(bool(trials), allowed)
                policy = AntihealPolicy(SNAP, members, resolved["effects"])
                self.assertTrue(all(policy.legal(trial) for trial in trials))


if __name__ == "__main__":
    unittest.main()
