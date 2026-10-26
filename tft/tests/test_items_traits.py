"""Item mechanics, board trait resolution, equipped forms, and trait timing tests."""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from tft import sim as tft
from tft.board import RIFTBEAST, resolve_board_traits, slots_used
from tft.tests.helpers import (
    ENGINE,
    ITEM_FX,
    SNAP,
    ally,
    carry_result,
    caster,
    enemy,
    events,
    fight,
    fixture,
    flat,
    flat_calc,
    generic_targets,
    gnar_spec,
    match,
    match_events as combat_events,
    plain_spec,
    rows,
    spec_for,
    trace,
)

champion_match = match
match_events = combat_events
solo_events = events


def controlled_brambleback(*, duration=1.1, incoming=0, interval=1,
                          alpha=False, fx=(), target_armor=0):
    spec = spec_for(
        "Brambleback", star=2, duration=duration, pressure=incoming > 0,
        dummy=generic_targets(incoming_dps=incoming, physical_share=1,
                              pressure_interval=interval, target_count=1,
                              target_armor=target_armor, target_mr=0),
        traits=[("DA_Riftbeast18", 1)] if alpha else [], fx=fx,
    )
    spec["immortal"] = True
    spec["role"] = {"manaRegen": 0, "asPct": 0}
    spec["unit"]["kind"] = "Specialist"  # no attack mana in the timing fixture
    kit = spec["kits"]["base"]
    kit.update(baseAd=100, hpStar=1000)
    kit["stats"].update(hp=1000, ad=100, armor=0, mr=0, mana=10**9,
                        initialMana=0, critChance=0, critMult=1)
    kit["stats"]["as"] = 1
    return spec


class TestBramblebackAlphaAndRageblade(unittest.TestCase):
    def test_pinned_alpha_heal_and_item_rows_support_the_hand_values(self):
        unit = SNAP.unit("Brambleback")
        self.assertIn('row="TraitMaxHealthHeal"', unit["ability"]["desc"])
        self.assertEqual(tft.curve_at(unit["curve"]["TraitMaxHealthHeal"], 2), 0.04)
        self.assertEqual(tft.curve_at(unit["curve"]["FrenzyADPercent"], 2), 0.8)
        self.assertEqual(tft.curve_at(unit["curve"]["Duration"], 2), 8)
        item = tft.item_spec(SNAP, "DA_GuinsoosRageblade",
                             tft.load_item_effects(SNAP.set_no), unit)
        self.assertFalse(item["unique"])
        self.assertAlmostEqual(item["asPerSecond"][0], 0.07)

    def test_alpha_heal_occurs_once_per_attack_after_wound_and_missing_hp_limit(self):
        for incoming, wound, expected in ((400, 0, 40), (400, 0.5, 20),
                                         (400, 1, 0), (10, 0, 10), (0, 0, 0)):
            with self.subTest(incoming=incoming, wound=wound):
                spec = controlled_brambleback(incoming=incoming, interval=0.25, alpha=True)
                spec["enemyDebuffs"]["wound"] = wound
                _, result = ENGINE.simulate(spec, True)
                # Two attacks at 0 and 1 seconds. The opening attack cannot
                # overheal; the next restores min(missing HP, 1000*.04*(1-wound)).
                self.assertEqual(result["attacks"], 2)
                self.assertEqual(result["healed"], expected)
                self.assertEqual(result["hpLeft"], 1000 - incoming + expected)
                self.assertEqual(sum(hit[2] for hit in events(result, "heal", "red buff")), expected)
                unmarked = deepcopy(spec)
                unmarked["traits"] = []
                self.assertEqual(ENGINE.simulate(unmarked, False)[1]["healed"], 0)

    def test_each_real_rageblade_adds_seven_percent_base_speed_per_second(self):
        for copies in (1, 2, 3):
            with self.subTest(copies=copies):
                spec = spec_for("Brambleback", pressure=False, duration=3.1,
                                items=["Guinsoo's Rageblade"] * copies)
                spec["role"] = {"manaRegen": 0, "asPct": 0}
                spec["kits"]["base"]["stats"].update(mana=10**9, initialMana=0)
                opening, result = ENGINE.simulate(spec, True)
                self.assertAlmostEqual(opening["ap"], 100 + 10 * copies)
                self.assertAlmostEqual(opening["as"], 0.55 * (1 + 0.1 * copies))
                self.assertAlmostEqual(result["probe"]["asStack"], 3 * 0.07 * copies)


class TestConservativeBramblebackFrenzy(unittest.TestCase):
    def test_recast_refreshes_one_buff_and_expiry_removes_only_that_buff(self):
        spec = controlled_brambleback(
            duration=12.1, incoming=25, target_armor=100,
            fx=[{"manaAtHp": [0.95, 40]}],
        )
        spec["kits"]["base"]["stats"].update(mana=40, initialMana=40)
        _, result = ENGINE.simulate(spec, True)
        # A single low-health mana trigger supplies the second cast. No
        # further mana is available, so the refreshed expiration is observable.
        # Native threshold equality triggers at950 HP on the second hit.
        self.assertEqual(result["castTimes"], [0, 2])
        self.assertEqual([hit[0] for hit in events(result, "land")], [0.25, 2.25])
        for hit in events(result, "damage", "auto"):
            with self.subTest(time=hit[0]):
                # One +80% AD and 40% personal ignore: 180/(1+60/100).
                # It stays active beyond the first expiration at 8.25s,
                # then ends at 10.25s. Opening/expired damage is 100/2.
                expected = 112.5 if 0.25 <= hit[0] < 10.25 else 50
                self.assertAlmostEqual(hit[2], expected)

    def test_real_three_rageblade_loadout_cannot_multiply_frenzy_ad_with_recasts(self):
        spec = spec_for(
            "Brambleback", star=2, duration=40.1, pressure=False,
            dummy=generic_targets(target_count=1, target_armor=100, target_mr=100),
            items=["Guinsoo's Rageblade"] * 3,
        )
        spec["immortal"] = True
        opening, result = ENGINE.simulate(spec, True)
        self.assertEqual(opening["ad"], 165)
        self.assertEqual(opening["ap"], 130)
        # Frenzy now locks mana for its eight seconds (the adopted TFTraits
        # rule, 2026-09-20), so ordinary mana recasts it only after it has
        # ended: several windows back to back, none overlapping. The refresh
        # of a live Frenzy is exercised above through a proc that ignores locks.
        self.assertGreaterEqual(result["casts"], 3)
        lands = [hit[0] for hit in events(result, "land")]
        self.assertTrue(all(later - earlier >= 8 for earlier, later in zip(lands, lands[1:])))
        # At 130 AP the personal ignore is 10% + 30%*1.3 = 49%.
        # Base crit EV is 1.1; no other trait, AD item or proc raises this bound.
        frenzy_hit = 165 * 1.8 * 1.1 / 1.51
        autos = events(result, "damage", "auto")
        self.assertTrue(any(abs(hit[2] - frenzy_hit) < 1e-8 for hit in autos))
        self.assertLessEqual(max(hit[2] for hit in autos), frenzy_hit + 1e-8)


class TestCompositionTraits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def members(self, *names, star=2):
        return [{"api": self.snap.unit(name)["api"], "star": star} for name in names]

    def board(self, *names, star=2, alpha=None):
        return resolve_board_traits(self.snap, self.members(*names, star=star),
                                    self.snap.unit(alpha)["api"] if alpha else None)

    def trait(self, board, name):
        return next(trait for trait in board["traits"] if trait["name"] == name)

    def effect(self, board, unit, trait):
        api = self.snap.unit(unit)["api"]
        found = [effect for effect in board["effects"][api] if effect["name"] == trait]
        self.assertEqual(len(found), 1, (unit, trait, found))
        return found[0]

    def test_partial_boards_do_not_activate_unreached_traits(self):
        empty = resolve_board_traits(self.snap, [])
        self.assertEqual(empty["effects"], {})
        self.assertFalse(any(trait["active"] for trait in empty["traits"]))
        one = self.board("Karma")
        self.assertEqual(self.trait(one, "Spellweaver")["count"], 1)
        self.assertIsNone(self.trait(one, "Spellweaver")["breakpoint"])
        self.assertEqual(one["effects"][self.snap.unit("Karma")["api"]], [])

    def test_elder_contributes_two_riftbeast_in_total(self):
        alone = self.board("Elder Dragon")
        self.assertEqual(self.trait(alone, "Riftbeast")["count"], 2)
        self.assertFalse(self.trait(alone, "Riftbeast")["active"])
        self.assertTrue(self.trait(alone, "Apex Predator")["modeled"])
        paired = self.board("Elder Dragon", "Cinderling", alpha="Elder Dragon")
        self.assertEqual(self.trait(paired, "Riftbeast")["count"], 3)
        self.assertTrue(self.trait(paired, "Riftbeast")["active"])
        self.assertTrue(self.effect(paired, "Elder Dragon", "Riftbeast")["riftbeast"])
        self.assertFalse(self.effect(paired, "Cinderling", "Riftbeast")["riftbeast"])
        self.assertFalse(any("Apex Predator" in note for note in paired["limitations"]))

    def test_nine_slot_capacity_accepts_nine_actors_or_eight_with_elder(self):
        ordinary = self.members("Karma", "Kobuko", "Leona", "Ornn", "Rakan", "Rek'Sai", "Varus", "Veigar", "Xayah")
        self.assertEqual(len(resolve_board_traits(self.snap, ordinary)["effects"]), 9)
        elder = ordinary[:-2] + self.members("Elder Dragon")
        self.assertEqual(slots_used(elder), 9)
        self.assertEqual(len(resolve_board_traits(self.snap, elder)["effects"]), 8)
        with self.assertRaisesRegex(ValueError, "nine board slots"):
            resolve_board_traits(self.snap, ordinary[:-1] + self.members("Elder Dragon"))

    def test_actual_counts_select_highest_source_column(self):
        for names, count, breakpoint, column in [
                (("Karma", "LeBlanc"), 2, 2, 1),
                (("Karma", "LeBlanc", "Cassiopeia"), 3, 2, 1),
                (("Karma", "LeBlanc", "Cassiopeia", "Ahri"), 4, 4, 2)]:
            with self.subTest(names=names):
                board = self.board(*names)
                trait = self.trait(board, "Spellweaver")
                self.assertEqual((trait["count"], trait["breakpoint"], trait["column"]),
                                 (count, breakpoint, column))
                self.assertEqual(dict(self.effect(board, "Karma", "Spellweaver")["stats"])["ap"],
                                 20 if column == 1 else 40)

    def test_unique_style_traits_and_duplicate_breakpoint_columns(self):
        monolith = self.board("Malphite")
        self.assertTrue(self.trait(monolith, "Monolith")["active"])
        self.assertEqual(self.effect(monolith, "Malphite", "Monolith")["resistsPerAttacker"],
                         [10, 10])
        one = self.trait(self.board("Rengar"), "Rival")
        both = self.trait(self.board("Rengar", "Kha'Zix"), "Rival")
        self.assertEqual((one["breakpoint"], one["column"]), (1, 2))
        self.assertEqual((both["breakpoint"], both["column"]), (2, 3))

    def test_global_bonuses_reach_nonmembers_once(self):
        cases = [
            (("Kobuko", "Rek'Sai", "Karma"), "Brawler", "Kobuko", "Karma",
             {"hp": 120, "hpMult": 1.25}, {"hp": 120}),
            (("Leona", "Ornn", "Karma"), "Defender", "Leona", "Karma",
             {"armor": 25, "mr": 25}, {"armor": 12, "mr": 12}),
            (("Pebbles", "Teemo", "Karma"), "Invoker", "Pebbles", "Karma",
             {"manaRegen": 4}, {"manaRegen": 1}),
            (("Aphelios", "Kayle", "Karma"), "Rapidfire", "Aphelios", "Karma",
             {"asPct": 0.1}, {"asPct": 0.1}),
            (("Karma", "Ahri", "Leona"), "Spellweaver", "Karma", "Leona",
             {"ap": 20}, {"ap": 10}),
        ]
        for names, trait, member, nonmember, own_stats, team_stats in cases:
            with self.subTest(trait=trait):
                board = self.board(*names)
                for unit, expected in ((member, own_stats), (nonmember, team_stats)):
                    actual = dict(self.effect(board, unit, trait)["stats"])
                    self.assertEqual(actual.keys(), expected.keys())
                    for stat, value in expected.items():
                        self.assertAlmostEqual(actual[stat], value)
                nonmember_effect = self.effect(board, nonmember, trait)
                self.assertNotIn("asPerAttackStack", nonmember_effect)
                self.assertNotIn("apPerCast", nonmember_effect)

    def test_team_rows_follow_actual_breakpoint(self):
        board = self.board("Pebbles", "Teemo", "Morgana", "Sentinel", "Karma")
        self.assertEqual(self.trait(board, "Invoker")["column"], 3)
        self.assertEqual(dict(self.effect(board, "Karma", "Invoker")["stats"]),
                         {"manaRegen": 2})
        self.assertEqual(dict(self.effect(board, "Pebbles", "Invoker")["stats"]),
                         {"manaRegen": 8})
        # The explicit audited four-unit value fills the sparse lookup's
        # missing second column without changing other curve semantics.
        for names, own, team in ((["Rakan", "Yorick", "Karma"], 0.2, 0.04),
                                 (["Rakan", "Yorick", "Sejuani", "Vi", "Karma"], 0.3, 0.06),
                                 (["Rakan", "Yorick", "Sejuani", "Vi", "Amumu", "Maokai", "Karma"], 0.4, 0.08)):
            result = self.board(*names)
            self.assertAlmostEqual(self.effect(result, "Rakan", "Juggernaut")["durability"], own)
            self.assertAlmostEqual(self.effect(result, "Karma", "Juggernaut")["durability"], team)
            self.assertFalse(any("team's share is not modeled" in text for text in result["limitations"]))

    def test_resolved_team_effects_reach_engine_opening_once(self):
        unit = self.snap.unit("Karma")
        spec = tft.cell_spec(self.snap, unit, 2, "spread", [],
                             tft.dummies_for(self.snap), duration=0.01,
                             pressure=False, driver="Driver")
        baseline, _ = tft.engine().simulate(spec, False)
        for names, key, expected in [
                (("Kobuko", "Rek'Sai", "Karma"), "hp", baseline["hp"] + 120),
                (("Leona", "Ornn", "Karma"), "armor", baseline["armor"] + 12),
                (("Rakan", "Yorick", "Karma"), "durability", 0.04),
                (("Aphelios", "Kayle", "Karma"), "as", baseline["as"] * 1.1),
                (("Ahri", "Karma"), "ap", baseline["ap"] + 20)]:
            with self.subTest(names=names):
                board = self.board(*names)
                spec["traits"] = board["effects"][unit["api"]]
                opening, _ = tft.engine().simulate(spec, False)
                self.assertAlmostEqual(opening[key], expected)

        members = self.members("Kayle", "Leona", "Sejuani", "Karma")
        for member in members[:3]:
            member["star"] = 3
        spec["traits"] = resolve_board_traits(self.snap, members)["effects"][unit["api"]]
        opening, result = tft.engine().simulate(spec, False)
        self.assertAlmostEqual(opening["as"], baseline["as"] * 1.18)
        self.assertEqual(opening["armor"], baseline["armor"] + 15)
        self.assertEqual(result["probe"]["shieldsActive"], 1)

    def test_only_selected_alpha_holder_receives_unique_buff(self):
        names = ("Murkwolf", "Gromp", "Scuttlecrab")
        no_mark = self.board(*names)
        self.assertIsNone(no_mark["alphaHolder"])
        self.assertFalse(any(effect.get("riftbeast") for effects in no_mark["effects"].values()
                             for effect in effects))
        marked = self.board(*names, alpha="Murkwolf")
        for unit in names:
            effect = self.effect(marked, unit, "Riftbeast")
            self.assertEqual(effect["riftbeast"], unit == "Murkwolf")
            self.assertNotIn("note", effect)

    def test_riftbeast_capstone_stats_reach_every_riftbeast(self):
        names = ("Murkwolf", "Gromp", "Scuttlecrab", "Krug", "Mama Beak",
                 "Cinderling", "Pebbles", "Karma")
        board = self.board(*names, alpha="Murkwolf")
        for name in names[:-1]:
            stats = dict(self.effect(board, name, "Riftbeast")["stats"])
            self.assertEqual(stats["hp"], 50)
            self.assertEqual(stats["ap"], 5)
            self.assertAlmostEqual(stats["asPct"], 0.05)
        self.assertFalse(any(effect["name"] == "Riftbeast"
                             for effect in board["effects"][self.snap.unit("Karma")["api"]]))
        for name in names[:-1]:
            growth = self.effect(board, name, "Riftbeast")["timedStats"]
            self.assertEqual((growth[0]["after"], growth[0]["interval"]), (5, 5))
            self.assertEqual(dict(growth[0]["stats"])["hp"], 50)
        self.assertFalse(any("recurring growth" in text for text in board["limitations"]))

    def test_invalid_alpha_holder_is_rejected(self):
        for names, holder in [(("Murkwolf", "Gromp"), "Murkwolf"),
                              (("Murkwolf", "Gromp", "Scuttlecrab"), "Krug"),
                              (("Murkwolf", "Gromp", "Scuttlecrab", "Karma"), "Karma")]:
            with self.subTest(holder=holder, names=names), self.assertRaises(ValueError):
                self.board(*names, alpha=holder)

    def test_member_validation_rejects_illegal_or_duplicate_units(self):
        invalid = [self.members("Karma", "Karma"), self.members("Ahri", star=3),
                   self.members("Ashe", star=3), self.members("Karma", star=0),
                   self.members("Karma", star=4), self.members("Karma", star=True),
                   self.members("Karma", star=2.0), [{"api": "summon", "star": 2}],
                   ["Karma"], [{"api": "TFT18_Karma"}],
                   self.members("Karma", "Kobuko", "Leona", "Ornn", "Rakan",
                                "Rek'Sai", "Varus", "Veigar", "Xayah", "Ahri")]
        for members in invalid:
            with self.subTest(members=members), self.assertRaises(ValueError):
                resolve_board_traits(self.snap, members)

    def test_eclipse_never_activates_from_zero_unit_threshold(self):
        ordinary = self.board("Karma")
        combo = self.board("Kayle", "Leona", "Sejuani", "Diana", "Aphelios", "Alune")
        for board in (ordinary, combo):
            self.assertFalse(self.trait(board, "Eclipse")["active"])
            self.assertFalse(any(effect["api"] == "DA_18_Eclipse"
                                 for effects in board["effects"].values() for effect in effects))
        self.assertTrue(any("Eclipse" in text for text in combo["limitations"]))

    def test_lunar_does_not_invent_adjacent_allies(self):
        board = self.board("Diana", "Aphelios", "Karma")
        self.assertTrue(self.trait(board, "Lunar")["active"])
        self.assertFalse(any(effect["name"] == "Lunar"
                             for effect in board["effects"][self.snap.unit("Karma")["api"]]))
        self.assertTrue(any("adjacent non-Lunar" in text for text in board["limitations"]))

    def test_solar_teamwide_shield_and_bonus_scale_with_unique_three_stars(self):
        for upgrades in range(4):
            members = self.members("Kayle", "Leona", "Sejuani", "Karma")
            for member in members[:upgrades]:
                member["star"] = 3
            board = resolve_board_traits(self.snap, members)
            for name in ("Kayle", "Karma"):
                effect = self.effect(board, name, "Solar")
                self.assertAlmostEqual(effect["bonusMagicPct"], 0.07 + 0.015 * upgrades)
                self.assertAlmostEqual(effect["shieldAtStart"][0], 0.05 + 0.015 * upgrades)
                self.assertEqual(effect["shieldAtStart"][1], 12)
                stats = dict(effect["stats"])
                if upgrades == 3:
                    self.assertAlmostEqual(stats["asPct"], 0.18)
                    self.assertEqual((stats["armor"], stats["mr"]), (15, 15))
                else:
                    self.assertEqual(stats, {})
        # A non-Solar upgrade contributes too, even if the Solars are all 2★.
        members = self.members("Kayle", "Leona", "Sejuani", "Karma")
        members[-1]["star"] = 3
        board = resolve_board_traits(self.snap, members)
        self.assertAlmostEqual(self.effect(board, "Leona", "Solar")["bonusMagicPct"], 0.085)

    def test_solar_high_upgrade_splits_bonus_and_keeps_ascension_explicit(self):
        board = self.board("Kayle", "Leona", "Sejuani", "Karma", "Kobuko", "Ornn", "Rakan", "Varus", star=3)
        self.assertFalse(any("true damage" in text for text in board["limitations"]))
        self.assertTrue(any("ascension" in text for text in board["limitations"]))
        effect = self.effect(board, "Karma", "Solar")
        self.assertAlmostEqual(effect["bonusMagicPct"], 0.095)
        self.assertAlmostEqual(effect["bonusTruePct"], 0.095)

    def test_solar_true_damage_share_is_the_patch_row_not_a_half(self):
        # 18.2b converts 40% of the bonus at five 3★ (Threshold2TrueDamage
        # Conversion); 18.1d's row was 0.5, which the test above pins.
        snap = tft.load_snapshot(18, "18.2b")
        self.assertEqual(tft.curve_at(snap.traits["DA_18_Solar"]["curve"]["Threshold2TrueDamageConversion"], 1), 0.4)
        names = ("Kayle", "Leona", "Sejuani", "Karma", "Kobuko")
        for upgrades in (4, 5):
            members = [{"api": snap.unit(name)["api"], "star": 3 if index < upgrades else 2}
                       for index, name in enumerate(names)]
            board = resolve_board_traits(snap, members)
            effect = next(effect for effect in board["effects"][snap.unit("Karma")["api"]]
                          if effect["name"] == "Solar")
            bonus = 0.08 + 0.01 * upgrades
            if upgrades < 5:
                self.assertAlmostEqual(effect["bonusMagicPct"], bonus)
                self.assertNotIn("bonusTruePct", effect)
            else:
                self.assertAlmostEqual(effect["bonusMagicPct"], bonus * 0.6)
                self.assertAlmostEqual(effect["bonusTruePct"], bonus * 0.4)

    def test_lux_does_not_infer_an_avatar_variant_or_duplicate_traits(self):
        board = self.board("Lux", "Kayle", "Leona")
        self.assertEqual(self.trait(board, "Solar")["count"], 2)
        self.assertFalse(self.trait(board, "Solar")["active"])
        self.assertTrue(any("no Avatar variant" in text for text in board["limitations"]))

    def test_unmodeled_board_traits_have_specific_limitations(self):
        board = self.board("Rek'Sai", "Azir", "Caitlyn", "Camille", "Elise", "Kobuko", "Veigar", "Teemo")
        text = " ".join(board["limitations"])
        self.assertIn("sacrifice hex is left empty", text)
        self.assertIn("Essence", text)
        self.assertIn("Big Furry Friend", text)
        self.assertFalse(self.trait(board, "Blackthorn")["modeled"])
        self.assertFalse(any(effect["name"] in ("Blackthorn", "Coven", "Sprykin")
                             for effects in board["effects"].values() for effect in effects))

    def test_coverage_distinguishes_engine_hooks_from_scored_trait_effects(self):
        board = self.board("Soraka", "Fiddlesticks", "Karma", "Ahri", "Zyra", "Kobuko", "Rek'Sai", "Ivern")
        self.assertEqual(len(board["traits"]), len(self.snap.traits))
        self.assertTrue(all(isinstance(trait["coverageNotes"], list) for trait in board["traits"]))
        self.assertEqual(self.trait(board, "Brawler")["coverage"], "supported")
        self.assertEqual(self.trait(board, "Spellweaver")["coverage"], "partial")
        self.assertTrue(any("opening member AP" in note for note in self.trait(board, "Spellweaver")["coverageNotes"]))
        self.assertGreater(self.effect(board, "Karma", "Spellweaver")["apPerCast"], 0)
        for name, omission in (("Flora Fatalis", "contribute zero"),
                               ("Thornmaiden", "six plants")):
            trait = self.trait(board, name)
            self.assertTrue(trait["modeled"])
            self.assertEqual(trait["coverage"], "partial")
            self.assertTrue(any(omission in note for note in trait["coverageNotes"]))
        self.assertEqual(self.trait(board, "Greenfather")["coverage"], "unmodeled")
        self.assertTrue(any("cultivated hexes" in note for note in self.trait(board, "Greenfather")["coverageNotes"]))
        specials = self.board("Elder Dragon", "Draven")
        self.assertEqual(self.trait(specials, "Apex Predator")["coverage"], "structural")
        self.assertEqual(self.trait(specials, "Bounty Seeker")["coverage"], "economy")

    def test_results_are_deterministic_json_safe_and_do_not_mutate_sources(self):
        members = self.members("Kayle", "Leona", "Sejuani", "Karma", "Ahri")
        before_units, before_traits = deepcopy(self.snap.units), deepcopy(self.snap.traits)
        first = resolve_board_traits(self.snap, members)
        again = resolve_board_traits(self.snap, list(reversed(members)))
        self.assertEqual(first, again)
        self.assertEqual(json.loads(json.dumps(first)), first)
        first["effects"][members[0]["api"]][0]["stats"].append(["hp", 9999])
        self.assertEqual(resolve_board_traits(self.snap, members), again)
        self.assertEqual((self.snap.units, self.snap.traits), (before_units, before_traits))


class TestEquippedNidalee(unittest.TestCase):
    def spec(self, form, *, auto=True, pressure=False):
        names = ["Spear of Shojin", "Sterak's Gage", "Infinity Edge"] if form == "AD" else [
            "Spear of Shojin", "Rabadon's Deathcap", "Jeweled Gauntlet"]
        unit = SNAP.unit("Nidalee")
        spec = tft.cell_spec(SNAP, unit, 2, "clump", [], tft.dummies_for(SNAP), duration=4,
                             pressure=pressure, items=[SNAP.item(name)["api"] for name in names])
        spec.update(autoPressure=auto, immortal=True)
        return spec

    def test_automatic_pressure_follows_form_and_metadata_uses_effective_range(self):
        for form, kind, objective, pressure in (("AD", "Assassin", "fighter", True),
                                                ("AP", "Marksman", "carry", False)):
            with self.subTest(form=form):
                spec = self.spec(form)
                opening, result = tft.engine().simulate(spec)
                kit = spec["kits"][form]
                expected_range = kit["stats"]["range"] + (kit["rows"].get("AdditionalAttackRange", 0) if form == "AP" else 0)
                self.assertEqual((opening["form"], opening["kind"], opening["objective"], opening["pressure"], opening["range"]),
                                 (form, kind, objective, pressure, expected_range))
                self.assertEqual(result["absorbed"] > 0, pressure)

    def test_explicit_pressure_overrides_are_preserved_in_both_forms(self):
        for form in ("AD", "AP"):
            for pressure in (False, True):
                with self.subTest(form=form, pressure=pressure):
                    spec = self.spec(form, auto=False, pressure=pressure)
                    opening, result = tft.engine().simulate(spec)
                    self.assertEqual(opening["pressure"], pressure)
                    self.assertEqual(result["absorbed"] > 0, pressure)

    def test_enumeration_resolves_pressure_independently_for_each_build(self):
        spec = self.spec("AD")
        spec["items"] = []
        spec["pool"] = [tft.item_spec(SNAP, SNAP.item(name)["api"], ITEM_FX, SNAP.unit("Nidalee"))
                        for name in ("Sterak's Gage", "Rabadon's Deathcap", "Spear of Shojin")]
        count, rows = tft.engine().run_cell(spec, 1000, 1)
        self.assertEqual(count, len(rows))
        self.assertEqual({opening["form"] for _, opening, _ in rows}, {"AD", "AP"})
        for _, opening, result in rows:
            self.assertEqual(opening["pressure"], opening["form"] == "AD")
            self.assertEqual(result["absorbed"] > 0, opening["form"] == "AD")

    def test_aura_reach_is_resolved_after_form_selection(self):
        for name, fields in (("Sunfire Cape", ("sunfireAuras",)), ("Evenshroud", ("sunderAura",)),
                              ("Ionic Spark", ("shredAura", "ionicSpark"))):
            for form in ("AD", "AP"):
                with self.subTest(item=name, form=form):
                    spec = self.spec(form, auto=False)
                    spec["items"][0] = tft.item_spec(SNAP, SNAP.item(name)["api"], ITEM_FX, SNAP.unit("Nidalee"))
                    fx = tft.engine().compose_fx(spec)
                    self.assertEqual(fx["form"], form)
                    for field in fields:
                        self.assertEqual(bool(fx[field]), form == "AD", field)
                    opening, _ = tft.engine().simulate(spec)
                    self.assertEqual(opening["range"], fx["range"])



class TestFormDependentItemSpecs(unittest.TestCase):
    def test_form_units_retain_aura_reach_until_items_choose_the_form(self):
        effects = tft.load_item_effects(SNAP.set_no)
        for api, fields in (("DA_Evenshroud", ("sunderAura",)),
                            ("DA_IonicSpark", ("shredAura", "ionicSpark"))):
            with self.subTest(item=api):
                spec = tft.item_spec(SNAP, api, effects, SNAP.unit("Nidalee"))
                for field in fields:
                    self.assertNotIn(field, spec)
                    self.assertIn(field + "ByRange", spec)
                for name in ("Ashe", "Brambleback"):
                    static = tft.item_spec(SNAP, api, effects, SNAP.unit(name))
                    self.assertFalse(any(key.endswith("ByRange") for key in static))

    def test_periodic_sunfire_retains_copy_cadence_and_form_dependent_reach(self):
        effects = tft.load_item_effects(SNAP.set_no)
        for name in ("Nidalee", "Brambleback"):
            with self.subTest(unit=name):
                spec = tft.item_spec(SNAP, "DA_SunfireCape", effects, SNAP.unit(name))
                self.assertEqual(spec["sunfireAura"], [0.01, 10.0, 2.0, 2.0])
                self.assertNotIn("burnAura", spec)
                self.assertNotIn("burnAuraByRange", spec)
        ranged = tft.item_spec(SNAP, "DA_SunfireCape", effects, SNAP.unit("Ashe"))
        self.assertNotIn("sunfireAura", ranged)



class TestProfileAndCellForms(unittest.TestCase):
    def test_cell_preserves_base_identity_but_reports_pressured_melee_builds(self):
        unit = SNAP.unit("Nidalee")
        key = "s2-clump-bare"
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "pool_items", return_value=["DA_Deathblade"]):
            path = str(Path(directory) / "nidalee.json")
            cell = tft.compute_cell(SNAP, unit, key, {("nidalee", key): path}, prune=False)
        self.assertEqual(cell["objective"], "carry")
        self.assertTrue(cell["pressureByBuild"])
        self.assertIsNone(cell["pressured"])
        row = cell["rows"][0]
        self.assertEqual((row["form"], row["kind"], row["objective"], row["range"]),
                         ("AD", "Assassin", "fighter", 1))
        self.assertTrue(row["pressure"])
        self.assertIn("aliveTime", row)
        self.assertIn("hpLeft", row)
        self.assertTrue(cell["best"]["pressure"])
        self.assertNotIn("damageCurve", cell)
        self.assertNotIn("diagnosticModel", cell)


    def test_malformed_deferred_aura_fields_fail_during_parsing(self):
        unit = SNAP.unit("Nidalee")
        for field, value in (("sunderAuraByRange", [1]), ("shredAuraByRange", [1.1, 2]),
                             ("burnAuraByRange", [.01, 0, 2]), ("ionicSparkByRange", [1, math.nan])):
            spec = tft.cell_spec(SNAP, unit, 2, "clump", [], tft.dummies_for(SNAP), pressure=False)
            spec["items"] = [{"api": "invalid-aura", "name": "invalid-aura", "unique": False,
                              "stats": [], "adds": [], field: value}]
            with self.subTest(field=field), self.assertRaises(ValueError):
                tft.engine().compose_fx(spec)


class TestGnarRemoval(unittest.TestCase):
    def test_last_immortal_target_only_credits_the_real_transform_damage(self):
        spec = gnar_spec()
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual(result["castTimes"], [1, 2, 3, 4])
        self.assertEqual([hit[0] for hit in events(result, "land")], [1.25, 2.25, 3.25, 4.25])
        self.assertEqual(result["total"], 100)
        self.assertEqual(result["left"], [3000])
        self.assertIsNone(result["killTime"])
        self.assertEqual(events(result, "damage", "thrown off"), [])
        self.assertEqual(events(result, "kill"), [])

    def test_finite_last_target_is_still_removed_at_the_first_cast_land(self):
        spec = gnar_spec(immortal=False)
        # measure_response deliberately makes its targets immortal, so
        # finite boundary checks use the separate simulate API.
        totals = [ENGINE.simulate(dict(spec, duration=time), False)[1]["total"]
                  for time in (1, 1.25, 4.5)]
        self.assertEqual(totals, [100, 3000, 3000])
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual(result["total"], 3000)
        self.assertEqual(result["castTimes"], [1])
        self.assertEqual(result["killTime"], 1.25)
        self.assertEqual(result["left"], [0])
        self.assertEqual([(hit[0], hit[2]) for hit in events(result, "damage", "thrown off")],
                         [(1.25, 2900)])
        self.assertEqual(len(events(result, "kill")), 1)

    def test_three_targets_keep_the_normal_throw_and_pass_through_damage(self):
        for immortal in (False, True):
            with self.subTest(immortal=immortal):
                _, result = ENGINE.simulate(gnar_spec(immortal=immortal, targets=3), True)
                # Three 100-damage transforms, then four casts each hitting
                # the target for 200 and two other enemies for 50 each.
                self.assertEqual(result["total"], 3 * 100 + 4 * (200 + 2 * 50))
                self.assertEqual(result["castTimes"], [1, 2, 3, 4])
                self.assertEqual(sum(hit[2] for hit in events(result, "damage", "throw")), 800)
                self.assertEqual(sum(hit[2] for hit in events(result, "damage", "passed through")), 400)
                self.assertEqual(events(result, "damage", "thrown off"), [])
                self.assertIsNone(result["killTime"])


    def test_ordinary_true_damage_burn_is_not_suppressed_with_execution(self):
        spec = gnar_spec(transform=0, fx=[{"burnOnHit": [0.01, 4]}])
        for kit in spec["kits"].values():
            kit["baseAd"] = 1
            kit["stats"]["ad"] = 1
        _, result = ENGINE.simulate(spec, True)
        # Five positive attacks keep the 30 DPS burn active for 4.5 seconds.
        self.assertEqual(result["total"], 5 + 3000 * 0.01 * 4.5)
        self.assertEqual(sum(hit[2] for hit in events(result, "damage", "burn")), 135)
        self.assertEqual(events(result, "damage", "thrown off"), [])


    def test_unmodified_gnar_is_health_independent_on_immortal_probes(self):
        times = [10, 20, 60]
        totals = []
        for target_hp in (3000, 30000):
            dummy = generic_targets(target_count=1, target_hp=target_hp,
                                    target_armor=100, target_mr=100)
            spec = spec_for("Gnar", star=2, geometry="spread", duration=60,
                            pressure=False, dummy=dummy)
            spec.update(immortal=True, autoPressure=False, enemyDebuffs={}, targetDebuffs={})
            totals.append([ENGINE.simulate(dict(spec, duration=t), False)[1]["total"]
                           for t in times])
        self.assertEqual(*totals)
        self.assertGreater(totals[0][-1], 0)
        _, finite = ENGINE.simulate(dict(spec, immortal=False))
        self.assertIsNotNone(finite["killTime"])
        self.assertGreater(finite["breakdown"].get("thrown off", 0), 0)
        self.assertAlmostEqual(finite["total"], 30000, places=8)
        self.assertEqual(finite["left"], [0])


FLAIL = {"api": "DA_StrikersFlail", "ampPerCrit": [0.05, 5.0, 4]}


class TestFlail(unittest.TestCase):
    def spell(self, name="Lux", *, precision=True, copies=1, duration=3.1,
              hp=(1000000,), geometry="spread", regeneration=2000, extra_fx=()):
        effects = [FLAIL] * copies + list(extra_fx)
        if precision:
            effects.append({"precision": 1})
        spec = fixture(name, hp=hp, duration=duration, geometry=geometry, fx=effects)
        for kit in spec["kits"].values():
            kit["stats"].update(critChance=1.0, critMult=1.4, **{"as": 1.0})
        # Native attacks cannot occur during this observation; repeated casts
        # use the initially full bar and the specified mana regeneration.
        spec["unit"]["timing"] = {
            "base": {"castAnimation": 0.25, "effectAt": 0.25, "attackDelay": 100.0}}
        spec["role"] = {"manaRegen": regeneration, "asPct": 0.0}
        flat(spec, "MagicDamageCalc1", 100.0)
        return spec

    def basic(self, *, chance=1.0, copies=1, duration=4.01):
        spec = self.spell(precision=False, copies=copies, duration=duration)
        spec["driver"] = "Driver"
        spec["unit"]["timing"] = None
        spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
        for kit in spec["kits"].values():
            kit.update(baseAd=100.0)
            kit["stats"].update(ad=100.0, critChance=chance,
                                mana=1000000000, initialMana=0.0)
        return spec

    def assert_amounts(self, hits, expected):
        self.assertEqual(len(hits), len(expected))
        for hit, amount in zip(hits, expected):
            self.assertAlmostEqual(hit[2], amount)

    def test_basic_crit_grants_amp_after_the_hit_that_earned_it(self):
        res = carry_result(self.basic())
        # A guaranteed crit deals 100 x 1.4. Only earlier crits contribute
        # 5/10/15/20% amp; the first hit cannot use its own new buff.
        self.assert_amounts(events(res, "damage", "auto"), [140, 147, 154, 161, 168])

    def test_fractional_crit_expectation_does_not_buff_the_current_hit(self):
        res = carry_result(self.basic(chance=0.5, duration=2.01))
        # Independent 50% crits give 120 expected base damage and 2.5%
        # expected amp from each previous hit, below the stack cap.
        self.assert_amounts(events(res, "damage", "auto"), [120, 123, 126])

    def test_separate_ability_crits_stack_without_any_attacks(self):
        res = carry_result(self.spell())
        self.assertEqual(res["attacks"], 0)
        self.assert_amounts(events(res, "damage", "ability"), [140, 147, 154, 161])

    def test_abilities_without_precision_cannot_generate_flail_stacks(self):
        res = carry_result(self.spell(precision=False))
        self.assertEqual(res["attacks"], 0)
        self.assert_amounts(events(res, "damage", "ability"), [100] * 4)

    def test_noncritting_item_and_trait_damage_do_not_add_crit_stacks(self):
        for kind, extra, source in (("item", {"burnOnHit": [0.01, 5.0]}, "burn"),
                                    ("trait", {"bonusMagicPct": 0.5}, "solar"),
                                    ("trait", {"bleed": [0.5, 2.0]}, "bleed")):
            with self.subTest(source=source):
                spec = self.spell(extra_fx=[extra] if kind == "item" else [])
                if kind == "trait":
                    spec["traits"].append({"api": "test", "name": "test",
                                           "stats": [], **extra})
                res = carry_result(spec)
                self.assertTrue(events(res, "damage", source))
                # The four eligible spell crits still produce exactly the
                # same ramp despite additional item/trait damage events.
                self.assert_amounts(events(res, "damage", "ability"), [140, 147, 154, 161])

    def test_native_noncritting_burn_does_not_buff_noncritical_spells(self):
        spec = self.spell("Cinderling", precision=False)
        flat(spec, "PhysicalDamageCalc1", 100.0)
        rows(spec, BurnAmount=1.0, BurnDuration=5.0)
        res = carry_result(spec)
        self.assertTrue(events(res, "damage", "burn"))
        self.assert_amounts(events(res, "damage", "ability"), [100] * 4)

    def test_each_accepted_target_hit_grants_once_after_its_damage(self):
        spec = self.spell(hp=(1000000,) * 3, geometry="clump", duration=1.1)
        rows(spec, DamageReductionPerUnit=0.0, MinimumFalloffDamageRatio=1.0)
        res = carry_result(spec)
        self.assertEqual(res["attacks"], 0)
        self.assert_amounts(events(res, "damage", "ability"),
                            [140, 147, 154, 161, 168, 168])

    def test_empowered_swipes_do_not_double_proc_from_simulated_attack(self):
        for precision in (False, True):
            with self.subTest(precision=precision):
                spec = self.spell("Aphelios", precision=precision)
                rows(spec, Duration=0.25, NumAttacksBase=1.0,
                     AS_NeededForExtraSwipe=1000.0, NumSwipesTriggerSimulatedAutos=1.0)
                flat(spec, "PhysicalDamageCalc1", 100.0)
                flat(spec, "PhysicalDamageCalc2", 0.0)
                res = carry_result(spec)
                self.assertEqual(res["attacks"], 0)
                self.assert_amounts(events(res, "damage", "swipes"),
                                    [140, 147, 154, 161] if precision else [100] * 4)

    def test_duplicate_flails_share_four_stack_limit_but_each_listens(self):
        res = carry_result(self.basic(copies=2))
        # Each copy applies a 5% stack after a crit; both use the same buff
        # definition, so two crits fill the shared four-stack 20% cap.
        self.assert_amounts(events(res, "damage", "auto"), [140, 154, 168, 168, 168])

    def test_at_cap_refreshes_only_the_oldest_instance(self):
        spec = self.spell(duration=6.0)
        spec["unit"].update(timing=None, castTime=3.75)
        spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
        for kit in spec["kits"].values():
            kit.update(baseAd=100.0)
            kit["stats"].update(ad=100.0, mana=35.0, initialMana=0.0, **{"as": 2.0})
        res = carry_result(spec)
        self.assertEqual(res["castTimes"], [2.0])
        # Crits at 0/.5/1/1.5 fill the cap. The crit at 2 refreshes only
        # the oldest stack to expiry 7; the .5 stack still expires at 5.5.
        # A spell at 5.75 therefore sees three stacks, not two or four.
        hits = events(res, "damage", "ability")
        self.assertEqual([hit[0] for hit in hits], [5.75])
        self.assert_amounts(hits, [161])

    def poison(self, *, period):
        spec = self.spell("Cassiopeia", duration=2.6, regeneration=0.0)
        rows(spec, Duration=2.0)
        flat(spec, "MagicDamageCalc1", 200.0)
        for kit in spec["kits"].values():
            kit["dotCritPeriods"] = {} if period is None else {"poison": period}
        return spec

    def test_poison_procs_follow_half_second_period_not_integration_slices(self):
        res = carry_result(self.poison(period=0.5))
        hits = events(res, "damage", "poison")
        self.assertEqual([hit[0] for hit in hits],
                         [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5])
        # 200 over 2 seconds gives 35 damage per .25s at 100% crit. The
        # first completed native .5s period grants its buff AFTER time 1.
        self.assert_amounts(hits, [35, 35, 36.75, 36.75, 38.5, 38.5, 40.25, 40.25])

    def test_unknown_dot_period_keeps_crit_damage_without_inventing_procs(self):
        res = carry_result(self.poison(period=None))
        self.assert_amounts(events(res, "damage", "poison"), [35] * 8)

    def test_dot_period_must_be_finite_and_positive(self):
        for period in (0.0, -0.5, float("nan"), float("inf")):
            with self.subTest(period=period), self.assertRaises(ValueError):
                ENGINE.simulate(self.poison(period=period), False)

    def test_source_periods_reach_all_equipped_kits(self):
        expected = {
            "Ashe": {"trail": 0.5},
            "Cassiopeia": {"poison": 0.5},
            "Draven": {"bleed axes": 0.5},
            "Fiddlesticks": {"drain": 0.25},
            "Gromp": {"cloud": 1.0},
            "Karma": {"tether": 0.25},
            "Kog'Maw": {"acid": 0.5},
            "Morgana": {"withering zone": 0.25},
        }
        # The three row-derived values resolve native damage timer rows;
        # fixed periods come from the source effects/manual timer. Forms
        # must retain the metadata even when only their AP ability uses it.
        for name, periods in expected.items():
            with self.subTest(name=name):
                spec = spec_for(name)
                for kit in spec["kits"].values():
                    self.assertEqual(kit["dotCritPeriods"], periods)
        for kit in spec_for("Kennen")["kits"].values():
            self.assertNotIn("firestorm", kit.get("dotCritPeriods", {}))

    def test_ashe_trail_terms_share_one_periodic_crit_opportunity(self):
        spec = self.spell("Ashe", hp=(10000,), duration=2.6, regeneration=0.0)
        flat(spec, "PhysicalDamageCalc1", 0.0)
        flat(spec, "PhysicalDamageCalc2", 100.0)
        rows(spec, RiftDuration=2.0, MaxHealthDamagePerSecond=0.01)
        for kit in spec["kits"].values():
            kit["dotCritPeriods"] = {"trail": 0.5}
        res = carry_result(spec)
        totals = {}
        for hit in events(res, "damage", "trail"):
            totals[hit[0]] = totals.get(hit[0], 0.0) + hit[2]
        # Flat 100/sec and 1% of 10,000 HP/sec are one trail hit. Both
        # halves must settle before its one .5s proc can amplify later hits.
        self.assertEqual(list(totals), [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5])
        for amount, expected in zip(totals.values(),
                                    [70, 70, 73.5, 73.5, 77, 77, 80.5, 80.5]):
            self.assertAlmostEqual(amount, expected)

    def test_lethal_compound_trail_hit_still_earns_one_periodic_stack(self):
        spec = self.spell("Ashe", hp=(60.0, 10000.0), geometry="clump",
                          duration=1.26, regeneration=0.0)
        flat(spec, "PhysicalDamageCalc1", 0.0)
        flat(spec, "PhysicalDamageCalc2", 100.0)
        rows(spec, RiftDuration=2.0, MaxHealthDamagePerSecond=0.01)
        for kit in spec["kits"].values():
            kit["dotCritPeriods"] = {"trail": 0.5}
        res = carry_result(spec)
        first = events(res, "damage", "trail", target=0)
        self.assertAlmostEqual(sum(hit[2] for hit in first), 60.0)
        self.assertEqual(res["left"][0], 0.0)
        self.assertGreater(res["left"][1], 0.0)
        # At .75 the first victim loses (100 + .01*60)*1.4*.25 = 35.21.
        # At the due time 1.0 its remaining 24.79 HP is below the flat
        # term's 35 damage. The accepted compound crit must still earn one
        # stack, even though evaluating a later separate term would fail.
        totals = {}
        for hit in events(res, "damage", "trail", target=1):
            totals[hit[0]] = totals.get(hit[0], 0.0) + hit[2]
        self.assertEqual(list(totals), [0.75, 1.0, 1.25])
        # The sibling sees that one earned stack at 1.0; its own periodic
        # crit then earns the second stack for the subsequent slice.
        for amount, expected in zip(totals.values(), [70.0, 73.5, 77.0]):
            self.assertAlmostEqual(amount, expected)

    def test_fractional_overflow_finishes_with_many_concurrent_crit_hits(self):
        script = textwrap.dedent("""
            import json
            from tft.tests.test_items_traits import TestFlail, carry_result as result, events, flat, rows

            fixture = TestFlail()
            observations = []
            for chance in (0.6, 0.8, 0.95):
                for copies in (1, 3):
                    spec = fixture.spell('Alune', copies=copies, duration=12.1,
                                         hp=(1000000.0,) * 3, geometry='clump')
                    for kit in spec['kits'].values():
                        kit['stats']['critChance'] = chance
                    rows(spec, NumEnemies=3.0, NumMoonshards=64.0)
                    flat(spec, 'MagicDamageCalc1', 100.0)
                    flat(spec, 'MagicDamageCalc2', 0.0)
                    measured = result(spec)
                    hits = events(measured, 'damage', 'moonshards')
                    baseline = 100.0 * (1.0 + 0.4 * chance)
                    assert len(hits) > 256, (chance, copies, len(hits))
                    assert hits[-1][0] > 6.0
                    assert abs(hits[0][2] - baseline) < 1e-8
                    # Shared four-stack 5% buffs can amplify damage by at
                    # most 20%, including with three listener copies.
                    assert all(baseline - 1e-8 <= hit[2] <= baseline * 1.2 + 1e-8
                               for hit in hits), (chance, copies)
                    observations.append({'chance': chance, 'copies': copies,
                                         'hits': len(hits), 'last': hits[-1][0]})
            print(json.dumps(observations))
        """)
        # The simulator's overflow loop once reselected a refreshed tiny fragment
        # without reducing its floating-point budget. A subprocess bounds
        # that failure and prevents a regression from hanging the test run.
        try:
            completed = subprocess.run([sys.executable, "-c", script],
                                       cwd=Path(__file__).resolve().parents[2],
                                       capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("Fractional Flail overflow did not finish within 10 seconds")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(len(json.loads(completed.stdout)), 6)

    def test_reciprocal_deferred_crit_is_credited_once_after_settlement(self):
        malphite = ally("Malphite", driver="Malphite", hp=10000.0,
                        ad=100.0, attack_speed=0.5, cast=True, lane=1,
                        fx=[FLAIL, {"precision": 1}])
        stats = malphite["spec"]["kits"]["base"]["stats"]
        stats.update(critChance=1.0, critMult=1.4)
        flat_calc(malphite, "ShieldCalc1", 50.0)
        flat_calc(malphite, "MagicDamageCalc1", 100.0)
        malphite["spec"]["kits"]["base"]["rows"]["ShieldDuration"] = 5.0
        res = match([ally(hp=10000.0, lane=1),
                     ally(hp=10000.0, ad=100.0, attack_speed=1.0, lane=3)],
                    [malphite], duration=2.1)
        wave = combat_events(res, "damage", "enemy", name="shield break")
        self.assertEqual([(hit["time"], hit["target"]) for hit in wave], [(1.0, 0), (1.0, 1)])
        self.assertEqual([hit["amount"] for hit in wave], [147.0, 154.0])
        # Initial auto earned one stack. Both wave recipients earn one;
        # the second wave is deferred because that attacker is borrowed.
        # The next auto must see all three, with no queue-time double grant.
        autos = combat_events(res, "damage", "enemy", name="auto")
        self.assertEqual([hit["time"] for hit in autos], [0.0, 2.0])
        self.assertEqual([hit["amount"] for hit in autos], [140.0, 161.0])


class TestNativeHealthStacking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.engine = tft.engine()
        cls.item_fx = tft.load_item_effects(cls.snap.set_no)

    def spec(self, name="Sett", *, star=2, items=(), effects=()):
        spec = tft.cell_spec(
            self.snap, self.snap.unit(name), star, "spread", [], generic_targets(),
            duration=0.01, pressure=False, item_fx=self.item_fx,
            items=[self.snap.item(item)["api"] for item in items], driver="Driver")
        spec["traits"] = deepcopy(list(effects))
        return spec

    def assert_opening_hp(self, spec, expected):
        # The standalone leaderboard and composition opening calculation
        # must both expose the same HP, before any attacks or timed grants.
        sheet, _ = self.engine.simulate(spec, False)
        self.assertAlmostEqual(sheet["hp"], expected, places=7)

    def test_zero_through_three_warmogs_add_their_percentages(self):
        # Sett: 1200 base HP × 1.8 at two stars. Each Warmog adds 500
        # flat HP and 18 percentage points to the same HP multiplier.
        for count, expected in ((0, 2160.0), (1, 3138.8),
                                (2, 4297.6), (3, 5636.4)):
            with self.subTest(warmogs=count):
                spec = self.spec(items=("Warmog's Armor",) * count)
                self.assert_opening_hp(spec, expected)

    def test_bramble_compounds_after_the_additive_item_percentage_pool(self):
        cases = (
            # (2160 + 500) × (1 + .18 + .06) × 1.06.
            (("Warmog's Armor", "Bramble Vest", "Dragon's Claw"), 3496.304),
            # Sunfire adds 150 flat HP and 8%; Bramble compounds by 1.06.
            # (2160 + 500 + 150) × (1 + .18 + .08) × 1.06.
            (("Warmog's Armor", "Sunfire Cape", "Bramble Vest"), 3753.036),
        )
        for items, expected in cases:
            for ordered in (items, tuple(reversed(items))):
                with self.subTest(items=ordered):
                    self.assert_opening_hp(self.spec(items=ordered), expected)

    def test_sett_brawler_and_blossom_share_the_item_health_pool(self):
        members = [{"api": self.snap.unit(name)["api"], "star": 2}
                   for name in ("Sett", "Kobuko", "Ahri", "Ashe")]
        resolved = resolve_board_traits(self.snap, members)
        effects = resolved["effects"][self.snap.unit("Sett")["api"]]
        self.assertEqual({trait["name"]: trait["breakpoint"]
                          for trait in resolved["traits"] if trait["active"]},
                         {"Brawler": 2, "Blossom": 3})
        # Brawler adds 120 flat HP and 25%; Blossom adds 10%.
        # With three Warmogs: (2160 + 120 + 1500) × 1.89 = 7144.2.
        for count, expected in ((0, 3078.0), (1, 4253.4), (3, 7144.2)):
            with self.subTest(warmogs=count):
                spec = self.spec(items=("Warmog's Armor",) * count, effects=effects)
                self.assert_opening_hp(spec, expected)

    def test_star_scaling_precedes_flat_item_health_and_percentage_health(self):
        # Kobuko is a supported three-star unit with 700 base HP. Only
        # champion base HP receives the 1 / 1.8 / 3.24 star multiplier;
        # Warmog's 500 flat HP is added afterwards, then amplified by 18%.
        for star, bare_hp, item_hp in ((1, 700.0, 1416.0),
                                      (2, 1260.0, 2076.8),
                                      (3, 2268.0, 3266.24)):
            with self.subTest(star=star):
                self.assert_opening_hp(self.spec("Kobuko", star=star), bare_hp)
                self.assert_opening_hp(
                    self.spec("Kobuko", star=star, items=("Warmog's Armor",)), item_hp)


NASHOR = {"api": "DA_NashorsTooth", "manaPerAttack": 2.0, "manaPerCrit": 2.0}
TITAN = {"api": "DA_TitansResolve", "adapPerAttack": [0.02, 25.0, 0.1],
         "adapPerHit": True}


def basic(name="Ashe", *, hp=(1000000,), duration=2.01, fx=(), ad=100.0,
          chance=0.0, attack_speed=1.0):
    spec = fixture(name, hp=hp, duration=duration, fx=fx)
    spec["driver"] = "Driver"
    spec["unit"]["timing"] = None
    for kit in spec["kits"].values():
        kit.update(baseAd=ad)
        kit["stats"].update(ad=ad, critChance=chance, critMult=1.4,
                            mana=1000000000, initialMana=0.0,
                            **{"as": attack_speed})
    return spec


def spell(name="Lux", *, hp=(1000000,), duration=1.1, fx=(), regen=0.0,
          precision=False):
    spec = fixture(name, hp=hp, duration=duration,
                   fx=[*fx, *([{"precision": 1}] if precision else [])])
    spec["unit"]["timing"] = {"base": {
        "castAnimation": 0.25, "effectAt": 0.25, "attackDelay": 100.0}}
    spec["role"] = {"manaRegen": regen, "asPct": 0.0}
    for kit in spec["kits"].values():
        kit["stats"].update(critChance=1.0, critMult=1.4, **{"as": 1.0})
    flat(spec, "MagicDamageCalc1", 100.0)
    return spec


def retaliation(*, fx=(), damage=30.0, duration=0.51):
    spec = basic("Warwick", hp=(10000,), ad=0.0, attack_speed=0.001,
                 duration=duration, fx=[{"thorns": [damage, 2.0]}, *fx])
    spec["pressure"] = True
    spec["dummies"]["slots"][0].update(ad=10.0, attackStart=0.0, **{"as": 0.001})
    return spec


class TestAcceptedCriticalMana(unittest.TestCase):
    def test_each_critical_attack_target_grants_nashor_mana_after_damage(self):
        for copies, expected in ((1, 18.0), (2, 26.0)):
            with self.subTest(copies=copies):
                spec = basic("Elder Dragon", hp=(1000000,) * 3, duration=0.01,
                             chance=1.0, fx=[NASHOR] * copies)
                spec["driver"] = "ElderDragon"
                rows(spec, AttackAoERatio=0.5, AttackAoEHexRadius=5.0)
                res = carry_result(spec)
                self.assertEqual(res["attacks"], 1)
                self.assertEqual(len(events(res, "damage")), 3)
                # Fighter10 + base2/copy + three eligible crits2/copy.
                self.assertAlmostEqual(res["probe"]["mana"], expected)

    def test_nashor_plain_attack_expected_mana_and_copies(self):
        for chance in (0.0, 0.5, 1.0):
            for copies in (1, 2):
                with self.subTest(chance=chance, copies=copies):
                    res = carry_result(basic(chance=chance, fx=[NASHOR] * copies))
                    self.assertEqual(res["attacks"], 3)
                    self.assertAlmostEqual(res["probe"]["mana"],
                                           3 * (7 + copies * (2 + 2 * chance)))

    def test_positive_lethal_hit_still_grants_its_critical_mana(self):
        res = carry_result(basic(hp=(1,), duration=0.01, chance=1.0, fx=[NASHOR]))
        self.assertEqual(res["left"], [0.0])
        self.assertEqual(len(events(res, "damage")), 1)
        self.assertAlmostEqual(res["probe"]["mana"], 7 + 2 + 2)

    def test_rejected_zero_damage_does_not_grant_critical_mana(self):
        res = carry_result(basic(ad=0.0, duration=0.01, chance=1.0, fx=[NASHOR]))
        self.assertFalse(events(res, "damage"))
        self.assertAlmostEqual(res["probe"]["mana"], 7 + 2)

    def test_spell_crit_without_attack_applicability_does_not_grant_mana(self):
        for precision in (False, True):
            with self.subTest(precision=precision):
                res = carry_result(spell(duration=0.6, fx=[NASHOR], precision=precision))
                self.assertEqual(res["attacks"], 0)
                self.assertEqual(len(events(res, "damage", "ability")), 1)
                self.assertAlmostEqual(res["probe"]["mana"], 0.0)

    def test_scuttle_attack_applicability_includes_each_precision_crit(self):
        spec = basic("Scuttlecrab", hp=(1000000,) * 3, duration=0.01,
                     chance=1.0, fx=[NASHOR, {"precision": 1}])
        spec["driver"] = "Scuttlecrab"
        flat(spec, "PhysicalDamageCalc1", 100.0)
        rows(spec, DanceHexRadius=5.0)
        res = carry_result(spec)
        self.assertEqual(res["attacks"], 1)
        self.assertEqual([h[2] for h in events(res, "damage", "dance")], [140.0] * 3)
        self.assertAlmostEqual(res["probe"]["mana"], 5 + 2 + 3 * 2)
        # Native PassInCritResult copies launch tags. Its producer is still
        # unresolved, so this does not impose a new no-Precision crit rule.


class TestDamageSourceGates(unittest.TestCase):
    def test_item_spark_is_excluded_from_giant_slayer_and_gunblade(self):
        for extra in ([], [{"ampVsTank": 0.3}, {"allyHealPct": 0.2}]):
            with self.subTest(extra=extra):
                spec = basic(ad=0.0, duration=0.3, fx=[{"ionicSpark": 0.5}, *extra])
                spec["pressure"] = True
                spec["dummies"]["slots"][0].update(manaMax=100.0, manaStart=100.0)
                res = carry_result(spec)
                self.assertEqual([h[2] for h in events(res, "damage", "ionic spark")], [50.0])
                self.assertEqual(res["allyHeal"], 0.0)

    def test_native_unit_sourced_bramble_keeps_damage_amp_and_healing(self):
        res = carry_result(retaliation(fx=[{"ampVsTank": 0.3}, {"allyHealPct": 0.2}]))
        self.assertEqual(len(events(res, "damage", "thorns")), 1)
        self.assertAlmostEqual(events(res, "damage", "thorns")[0][2], 30 * 1.3)
        self.assertAlmostEqual(res["allyHeal"], 30 * 1.3 * 0.2)

    def test_bramble_triggers_morello_but_red_requires_attack_or_spell_tag(self):
        for requires_tag, expected in ((False, 50.0), (True, 0.0)):
            with self.subTest(requires_tag=requires_tag):
                effect = {"burnOnHit": [0.01, 1.0],
                          "burnRequiresAttackOrSpell": requires_tag}
                res = carry_result(retaliation(fx=[effect]))
                self.assertEqual(len(events(res, "damage", "thorns")), 1)
                self.assertAlmostEqual(res["breakdown"].get("burn", 0.0), expected)

    def test_untagged_thorns_do_not_grant_titan_incoming_stacks(self):
        holder = ally("Warwick", ad=100.0, attack_speed=1.0, hp=10000.0, fx=[TITAN])
        thorns = ally(ad=0.0, attack_speed=0.01, hp=10000.0, fx=[{"thorns": [30.0, 2.0]}])
        res = match([holder], [thorns], duration=2.01)
        self.assertEqual([e["amount"] for e in combat_events(res, "damage", name="auto")],
                         [102.0, 104.0, 106.0])

    def test_tagged_unit_attack_and_spell_damage_grant_titan_stacks(self):
        for spell_hit in (False, True):
            with self.subTest(spell=spell_hit):
                holder = ally("Warwick", ad=100.0, attack_speed=1.0, hp=10000.0, fx=[TITAN])
                donor = ally("Lux", driver="Lux" if spell_hit else "Driver", ad=0.0 if spell_hit else 10.0,
                             attack_speed=0.01, hp=10000.0, cast=spell_hit)
                if spell_hit:
                    flat_calc(donor, "MagicDamageCalc1", 100.0)
                res = match([holder], [donor], duration=1.01)
                hits = combat_events(res, "damage", name="auto")
                self.assertEqual([e["amount"] for e in hits], [102.0, 106.0])


class TestPeriodicAndAreaListeners(unittest.TestCase):
    def poison(self, *, fx=(), duration=3.01, attack_delay=100.0):
        spec = spell("Cassiopeia", duration=duration, fx=fx)
        rows(spec, Duration=2.0)
        flat(spec, "MagicDamageCalc1", 200.0)
        spec["unit"]["timing"]["base"]["attackDelay"] = attack_delay
        for kit in spec["kits"].values():
            kit["stats"].update(critChance=0.0)
            kit["dotCritPeriods"] = {"poison": 0.5}
        return spec

    def test_area_damage_applies_last_whisper_to_every_accepted_target(self):
        spec = basic("Scuttlecrab", hp=(1000000,) * 3, duration=1.01,
                     fx=[{"sunderOnHit": [0.3, 2.0]}])
        spec["driver"] = "Scuttlecrab"
        flat(spec, "PhysicalDamageCalc1", 100.0)
        rows(spec, DanceHexRadius=5.0)
        for target in spec["dummies"]["slots"]:
            target["armor"] = 100.0
        hits = events(carry_result(spec), "damage", "dance")
        self.assertEqual([hit[3] for hit in hits], [0, 1, 2, 0, 1, 2])
        for hit, expected in zip(hits, [50.0] * 3 + [100 / 1.7] * 3):
            self.assertAlmostEqual(hit[2], expected)

    def test_periodic_morello_refreshes_without_flail_or_attacks(self):
        res = carry_result(self.poison(fx=[{"burnOnHit": [0.01, 0.6]}]))
        self.assertEqual(res["attacks"], 0)
        # Poison applies at.5; logical hits at1/1.5/2/2.5 refresh a .6s
        # burn. Eight .25s integrations at1.25..3 deal 20,000 damage to
        # this million-HP target (10,000 per second).
        self.assertAlmostEqual(res["breakdown"]["burn"], 20000.0)

    def test_periodic_void_refreshes_at_native_opportunities_only(self):
        spec = self.poison(fx=[{"shredOnHit": [0.3, 0.3]}], duration=2.6)
        for target in spec["dummies"]["slots"]:
            target["mr"] = 100.0
        hits = events(carry_result(spec), "damage", "poison")
        self.assertEqual([h[0] for h in hits], [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5])
        # Each .5s proc expires before the next proc; the intervening
        # quarter benefits. Integration alone cannot refresh the shred.
        for hit, expected in zip(hits, [12.5, 12.5, 25 / 1.7, 12.5,
                                        25 / 1.7, 12.5, 25 / 1.7, 12.5]):
            self.assertAlmostEqual(hit[2], expected)

    def test_periodic_magic_damage_also_refreshes_last_whisper(self):
        spec = self.poison(fx=[{"sunderOnHit": [0.3, 0.4]}],
                           duration=2.8, attack_delay=2.25)
        for kit in spec["kits"].values():
            kit.update(baseAd=100.0)
            kit["stats"]["ad"] = 100.0
        for target in spec["dummies"]["slots"]:
            target["armor"] = 100.0
        hits = events(carry_result(spec), "damage", "auto")
        self.assertEqual([h[0] for h in hits], [2.75])
        self.assertAlmostEqual(hits[0][2], 100 / 1.7)

    def test_titan_receives_one_stack_per_native_poison_hit(self):
        for known, expected in ((True, [102.0, 106.0, 112.0, 116.0]),
                                (False, [102.0, 104.0, 106.0, 108.0])):
            with self.subTest(known_period=known):
                holder = ally("Warwick", ad=100.0, attack_speed=1.0, hp=10000.0, fx=[TITAN])
                donor = ally("Cassiopeia", driver="Cassiopeia", ad=0.0,
                             attack_speed=0.01, hp=10000.0, cast=True)
                flat_calc(donor, "MagicDamageCalc1", 200.0)
                for kit in donor["spec"]["kits"].values():
                    kit["rows"]["Duration"] = 2.0
                    kit["dotCritPeriods"] = {"poison": 0.5} if known else {}
                res = match([holder], [donor], duration=3.01)
                hits = combat_events(res, "damage", name="auto")
                self.assertEqual(len(hits), len(expected))
                for hit, amount in zip(hits, expected):
                    self.assertAlmostEqual(hit["amount"], amount)

    def test_compound_trail_terms_do_not_double_titan_periodic_stacks(self):
        holder = ally("Warwick", ad=100.0, attack_speed=1.0, hp=10000.0, fx=[TITAN])
        donor = ally("Ashe", driver="Ashe", ad=0.0, attack_speed=0.01, hp=10000.0, cast=True)
        flat_calc(donor, "PhysicalDamageCalc1", 0.0)
        flat_calc(donor, "PhysicalDamageCalc2", 100.0)
        for kit in donor["spec"]["kits"].values():
            kit["rows"].update(RiftDuration=2.0, MaxHealthDamagePerSecond=0.01)
            kit["dotCritPeriods"] = {"trail": 0.5}
        res = match([holder], [donor], duration=3.01)
        hits = combat_events(res, "damage", name="auto")
        self.assertEqual(len(hits), 4)
        for hit, amount in zip(hits, [102.0, 106.0, 112.0, 116.0]):
            self.assertAlmostEqual(hit["amount"], amount)

    def test_elder_finite_ignite_has_bound_half_second_period(self):
        for kit in spec_for("Elder Dragon")["kits"].values():
            self.assertEqual(kit["dotCritPeriods"]["ignite"], 0.5)

    def test_elder_ignite_uses_spell_crit_eligibility(self):
        for precision, expected in ((False, 25.0), (True, 35.0)):
            with self.subTest(precision=precision):
                spec = spell("Elder Dragon", hp=(10000,), duration=1.26,
                             precision=precision)
                rows(spec, IgniteDuration=2.0, IgniteMaxHealthDamage=0.01,
                     StunDuration=0.0, Omnivamp=0.0)
                hits = events(carry_result(spec), "damage", "ignite")
                self.assertEqual([h[0] for h in hits], [0.75, 1.0, 1.25])
                for hit in hits:
                    # 1% of10,000 HP/sec gives25 per integration slice;
                    # the native Unit spell damage crits with Precision.
                    self.assertAlmostEqual(hit[2], expected)


class TestSunfireSelection(unittest.TestCase):
    def test_start_and_two_second_pulses_prefer_unburned_nearby_targets(self):
        spec = basic("Warwick", hp=(1000,) * 4, ad=0.0, attack_speed=0.001,
                     duration=4.01, fx=[{"sunfireAura": [0.01, 5.0, 2.0, 2.0]}])
        spec["dummies"]["slots"][0]["nearby"] = False
        res = carry_result(spec)
        hits = events(res, "damage", "burn")
        first = {target: min(h[0] for h in hits if h[3] == target)
                 for target in {h[3] for h in hits}}
        self.assertEqual(first, {1: 0.25, 2: 2.25})
        self.assertEqual(res["left"], [1000.0, 960.0, 980.0, 1000.0])
        # The pulse at4 applies target3, which takes no damage until4.25.

    def test_sunfire_copies_each_apply_to_an_unburned_target_at_start(self):
        effect = {"sunfireAura": [0.01, 5.0, 2.0, 2.0]}
        res = carry_result(basic("Warwick", hp=(1000,) * 3, ad=0.0, attack_speed=0.001,
                           duration=0.51, fx=[effect, effect]))
        self.assertEqual(res["left"], [995.0, 995.0, 1000.0])


class TestItemDefenseFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.4b")
        cls.effects = tft.load_item_effects(18)
        cls.engine = tft.engine()

    def spec(self, *, items=(), raw=0.0, period=1.0, duration=1.1,
             fx=(), mana=0.0):
        dummy = {
            "critEv": 1.0, "enemyDebuffs": {}, "targetDebuffs": {},
            "meleeRepositionSeconds": 0.0,
            "slots": [{
                "hp": 10**9, "armor": 0.0, "mr": 0.0, "kind": "tank",
                "ad": raw, "as": 1.0 / period, "ability": 0.0,
                "physicalShare": 1.0, "manaMax": 0.0, "manaStart": 0.0,
                "manaPerAttack": 0.0, "manaFromDamage": False,
                "nearby": True, "streams": 1, "lane": 3, "row": 0,
            }],
        }
        spec = tft.cell_spec(
            self.snap, self.snap.unit("Leona"), 1, "clump", [], dummy,
            duration=duration, pressure=True, item_fx=self.effects, trait_fx={},
            items=list(items), driver="Driver", cast_timing={})
        spec["unit"].update(kind="Fighter", objective="fighter", castTime=0.0,
                            timing=None)
        spec.update(immortal=False, enemyDebuffs={}, targetDebuffs={})
        spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
        kit = spec["kits"]["base"]
        kit.update(baseAd=0.0, hpStar=1000.0)
        kit["stats"].update(hp=1000.0, ad=0.0, armor=0.0, mr=0.0,
                            mana=mana, initialMana=0.0, critChance=0.0,
                            critMult=1.0)
        kit["stats"]["as"] = 1.0
        for extra in fx:
            spec["items"].append({"api": "test", "name": "test", "stats": [],
                                  "adds": [], **extra})
        return spec

    def run_spec(self, **options):
        return self.engine.simulate(self.spec(**options), True)

    @staticmethod
    def evs(result, kind, source=None):
        return [e for e in result["trace"]
                if e[1] == kind and (source is None or e[4] == source)]

    def test_small_real_hits_keep_exact_damage_without_health_cancellation(self):
        donor = ally(ad=0.3, attack_speed=1.0)
        victim = ally(ad=0.0, hp=1000000000.0)
        result = match([donor], [victim], duration=2.01)
        # Subtracting HP near1e9 loses part of a .3 hit. Resolved damage is
        # already known precisely and must be credited without that subtraction.
        hits = combat_events(result, "damage", name="auto")
        self.assertEqual([hit["amount"] for hit in hits], [0.3] * 3)
        self.assertEqual(result["damage"], sum([0.3] * 3))

    def test_lifelines_absorb_an_otherwise_lethal_first_hit(self):
        # Item armor applies first. Native PreApply then adds the shield
        # before the same hit consumes it; all numbers use max HP.
        cases = (
            ("DA_Bloodthirster", 1200.0, 1000.0, 0.0, 300.0, 100.0),
            ("DA_ProtectorsVow", 1375.0, 1000.0, 25.0, 200.0, 100.0),
            ("DA_SteraksGage", 1500.0, 1300.0, 0.0, 520.0, 320.0),
        )
        for item, raw, max_hp, armor, shield, hp in cases:
            with self.subTest(item=item):
                sheet, result = self.run_spec(items=(item,), raw=raw)
                self.assertEqual(sheet["hp"], max_hp)
                self.assertEqual(sheet["armor"], armor)
                self.assertIsNone(result["diedAt"])
                self.assertAlmostEqual(result["hpLeft"], hp)
                self.assertAlmostEqual(result["shielded"], shield)
                self.assertEqual(result["healed"], 0.0)
                kinds = [e[1] for e in result["trace"]]
                self.assertLess(kinds.index("shield"), kinds.index("take"))

    def test_lifeline_shield_does_not_prevent_unabsorbable_damage(self):
        _, plain = self.run_spec(raw=1200.0)
        _, too_large = self.run_spec(items=("DA_Bloodthirster",), raw=2000.0)
        self.assertEqual(plain["diedAt"], 1.0)
        self.assertEqual(plain["shielded"], 0.0)
        self.assertEqual(too_large["diedAt"], 1.0)
        self.assertEqual(too_large["shielded"], 300.0)
        self.assertEqual(too_large["hpLeft"], 0.0)

    def test_existing_shield_participates_in_lethal_fallback(self):
        _, result = self.run_spec(
            items=("DA_Bloodthirster",), raw=1400.0,
            fx=({"shieldAtStart": [0.20, 10.0]},))
        # 200 opening shield plus300 lifeline: 900 damage reaches HP.
        self.assertAlmostEqual(result["shielded"], 500.0)
        self.assertAlmostEqual(result["hpLeft"], 100.0)
        self.assertIsNone(result["diedAt"])

    def test_duplicate_lifelines_recheck_damage_after_each_fallback(self):
        _, result = self.run_spec(items=("DA_Bloodthirster",) * 2, raw=1250.0)
        # The first300 shield makes this hit nonlethal. The second copy
        # fires after the hit at50 HP, so it cannot absorb this hit too.
        self.assertEqual(result["shielded"], 300.0)
        self.assertEqual(result["hpLeft"], 50.0)
        relevant = [e[1] for e in result["trace"] if e[1] in ("shield", "take")]
        self.assertEqual(relevant, ["shield", "take", "shield"])

    def test_lifeline_fallback_is_spent_once(self):
        _, result = self.run_spec(items=("DA_Bloodthirster",), raw=1200.0,
                                  duration=2.1)
        self.assertEqual(result["diedAt"], 2.0)
        self.assertEqual(result["shielded"], 300.0)
        self.assertEqual(len(self.evs(result, "shield", "low health")), 1)

    def test_vow_fallback_grants_mana_once(self):
        _, result = self.run_spec(items=("DA_ProtectorsVow",), raw=1375.0,
                                  mana=1000.0)
        # Start20, two Fighter attacks10 each, one second regeneration1,
        # and native shield/mana fallback15. No casts spend this pool.
        self.assertEqual(result["attacks"], 2)
        self.assertEqual(result["casts"], 0)
        self.assertAlmostEqual(result["probe"]["mana"], 56.0)

    def test_threshold_equality_triggers_normal_lifeline(self):
        for item, raw, hp in (("DA_Bloodthirster", 500.0, 500.0),
                              ("DA_ProtectorsVow", 750.0, 400.0),
                              ("DA_SteraksGage", 520.0, 780.0)):
            with self.subTest(item=item):
                _, result = self.run_spec(items=(item,), raw=raw)
                self.assertAlmostEqual(result["hpLeft"], hp)
                self.assertEqual(result["shielded"], 0.0)
                self.assertEqual(len(self.evs(result, "shield", "low health")), 1)

    def test_edge_prevents_its_triggering_hit_from_crossing_health_floor(self):
        for raw in (720.0, 1800.0):
            with self.subTest(raw=raw):
                sheet, result = self.run_spec(items=("DA_EdgeOfNight",), raw=raw)
                self.assertEqual(sheet["armor"], 20.0)
                # 600 or1500 postmit damage clamps HP to400. Edge then
                # heals15% of the missing600, rather than overkill HP.
                self.assertEqual(result["hpLeft"], 490.0)
                self.assertEqual(result["healed"], 90.0)
                self.assertIsNone(result["diedAt"])
                self.assertEqual(result["probe"]["untargetableUntil"], 2.0)
                self.assertEqual(self.evs(result, "take")[0][5], 400.0)

    def test_edge_health_floor_is_spent_once(self):
        _, result = self.run_spec(items=("DA_EdgeOfNight",), raw=1800.0,
                                  period=2.0, duration=4.1)
        self.assertEqual(result["diedAt"], 4.0)
        self.assertEqual(len(self.evs(result, "heal", "edge of night")), 1)
        self.assertEqual(result["healed"], 90.0)

    def test_edge_preserves_permanent_standalone_benchmark_debuffs(self):
        spec = self.spec(items=("DA_EdgeOfNight",), raw=2400.0)
        spec["enemyDebuffs"] = {"wound": 0.33, "sunder": 0.30, "shred": 0.30}
        _, result = self.engine.simulate(spec, True)
        # Benchmark reductions describe continuing enemy coverage. Unlike
        # real encounter effects, they have no temporary status to remove.
        self.assertAlmostEqual(result["healed"], 90.0 * 0.67)
        self.assertAlmostEqual(result["hpLeft"], 400.0 + 90.0 * 0.67)
        self.assertIsNone(result["diedAt"])


    def test_spirit_heals_missing_health_on_one_second_actions(self):
        spec = self.spec(items=("DA_SpiritVisage",), raw=200.0, period=0.5,
                         duration=2.01)
        _, result = self.engine.simulate(spec, True)
        self.assertEqual([e[0] for e in self.evs(result, "heal", "regeneration")], [1.0, 2.0])

    def member(self, **options):
        return {"spec": self.spec(**options), "frontline": True, "lane": 3}

    def match(self, target, sources, duration):
        return self.engine.simulate_match({
            "allies": [target], "enemies": sources, "duration": duration,
            "geometry": "clump", "initiative": 1,
        }, True)

    def attacker(self, ad, attack_speed=0.01):
        member = self.member()
        kit = member["spec"]["kits"]["base"]
        kit["baseAd"] = ad
        kit["stats"].update(ad=ad)
        kit["stats"]["as"] = attack_speed
        return member

    def test_real_encounter_lifeline_credit_includes_newly_spent_shield(self):
        for item, raw, hp, damage in (("DA_Bloodthirster", 1200.0, 100.0, 1200.0),
                                      ("DA_ProtectorsVow", 1375.0, 100.0, 1100.0),
                                      ("DA_SteraksGage", 1500.0, 320.0, 1500.0)):
            with self.subTest(item=item):
                result = self.match(self.member(items=(item,)), [self.attacker(raw)], 0.1)
                self.assertEqual(result["allyHpLeft"], hp)
                self.assertAlmostEqual(result["enemyDamage"], damage)
                self.assertTrue(result["allies"][0]["alive"])

    def test_real_encounter_edge_does_not_credit_prevented_overkill(self):
        result = self.match(self.member(items=("DA_EdgeOfNight",)),
                            [self.attacker(1800.0)], 0.1)
        self.assertEqual(result["allyHpLeft"], 490.0)
        self.assertEqual(result["allies"][0]["healing"], 90.0)
        # 1000->400 loss then90 healing: settled damaging hit is600.
        self.assertEqual(result["enemyDamage"], 600.0)

    def test_edge_cleanses_existing_stun_in_real_encounter(self):
        leona = self.member(mana=10**6)
        spec = leona["spec"]
        spec["driver"] = "Leona"
        spec["unit"]["castTime"] = 0.25
        kit = spec["kits"]["base"]
        kit["stats"]["initialMana"] = 10**6
        for calc, amount in (("MagicDamageCalc1", 100.0), ("GenericCalc1", 0.0)):
            kit["calcs"][calc]["terms"] = [{"type": "flat", "value": amount, "op": "add"}]
        kit["rows"]["StunDuration"] = 3.0
        target = self.member(items=("DA_EdgeOfNight",))
        result = self.match(target, [self.attacker(300.0, 1.0), leona], 1.3)
        attacks = [e["time"] for e in result["trace"]
                   if e["kind"] == "attack" and e["side"] == "ally"]
        # Hits at0 and1 cause250 physical each; Leona's100 at.25
        # stuns until3.25. Edge's1s proc removes that existing stun.
        self.assertEqual(attacks, [0.0, 1.0])
        self.assertEqual(result["allies"][0]["healing"], 90.0)
        self.assertEqual(result["allyHpLeft"], 490.0)

    def test_edge_cleanses_existing_burn_and_wound_before_healing(self):
        burn_source = self.attacker(12.0)
        burn_source["spec"]["items"].append({
            "api": "test-burn", "name": "test-burn", "stats": [],
            "burnOnHit": [0.01, 4.0],
        })
        result = self.match(self.member(items=("DA_EdgeOfNight",)),
                            [burn_source, self.attacker(360.0, 1.0)], 1.6)
        target = result["allies"][0]
        # Two300 attacks, an opening10 hit and four2.5 burn ticks cross
        # 40% at1s. The triggering attacker has no burn application.
        # Edge removes the active wound before its90 heal. It also removes
        # the ongoing burn, including ticks during its untargetability.
        self.assertEqual(target["healing"], 90.0)
        self.assertEqual(result["allyHpLeft"], 490.0)
        burns = [e["time"] for e in result["trace"] if e["kind"] == "damage"
                 and e["side"] == "enemy" and e.get("sourceName") == "burn"]
        self.assertEqual(burns, [0.25, 0.5, 0.75, 1.0])


class TestItemStatsFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.4b")
        cls.engine = tft.engine()
        cls.effects = tft.load_item_effects(18)

    def spec(self, name="Sivir", *, star=1, items=(), duration=0.01):
        # An inert high-HP target keeps these checks independent of tank
        # pressure, formation changes, ability drivers and clear timings.
        dummy = {"slots": [{"hp": 1000000.0, "armor": 0.0, "mr": 0.0,
                             "kind": "tank", "nearby": True, "streams": 0}],
                 "targetDebuffs": {}, "meleeRepositionSeconds": 0.0}
        spec = tft.cell_spec(
            self.snap, self.snap.unit(name), star, "clump", [], dummy,
            duration=duration, pressure=False, item_fx=self.effects,
            items=list(items), driver="Driver", cast_timing={})
        spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
        for kit in spec["kits"].values():
            kit["stats"].update(mana=1000000.0, initialMana=0.0)
        return spec

    def opening(self, spec):
        return self.engine.simulate(spec, False)[0]

    def test_blue_buff_duplicates_add_bonus_percentages(self):
        # Sivir starts at 50 AD and 100 AP. Each Blue gives 15% AD/15 AP;
        # the bonus multiplier is 1 + .10 * copies, preserving the base.
        for count, ad, ap in ((0, 50.0, 100.0), (1, 58.25, 116.5),
                              (2, 68.0, 136.0), (3, 79.25, 158.5)):
            with self.subTest(copies=count):
                spec = self.spec(items=("DA_BlueBuff",) * count)
                sheet = self.opening(spec)
                fx = self.engine.compose_fx(spec)
                self.assertAlmostEqual(fx["adapMult"], 1.0 + 0.10 * count)
                self.assertAlmostEqual(sheet["ad"], ad)
                self.assertAlmostEqual(sheet["ap"], ap)

    def test_blue_buff_scales_other_item_bonuses_without_scaling_base(self):
        # Deathcap gives 55 AP. One Blue scales (15 + 55) by 1.10;
        # two scale (30 + 55) by 1.20, rather than 1.21.
        for items, expected in (
                (("DA_BlueBuff", "DA_RabadonsDeathcap"), 177.0),
                (("DA_BlueBuff", "DA_BlueBuff", "DA_RabadonsDeathcap"), 202.0)):
            for ordered in (items, tuple(reversed(items))):
                with self.subTest(items=ordered):
                    self.assertAlmostEqual(self.opening(self.spec(items=ordered))["ap"], expected)
        spec = self.spec(items=("DA_BlueBuff", "DA_BlueBuff"))
        spec["traits"] = [{"api": "bonus-stats", "name": "Bonus stats",
                           "stats": [["adPct", 0.20], ["ap", 20.0]]}]
        sheet = self.opening(spec)
        # Total gained AD/AP is .50 / 50; the base stays 50 / 100.
        self.assertAlmostEqual(sheet["ad"], 80.0)
        self.assertAlmostEqual(sheet["ap"], 160.0)

    def test_adaptive_helm_duplicates_add_extra_mana(self):
        for count, multiplier in ((0, 1.0), (1, 1.15), (2, 1.30), (3, 1.45)):
            with self.subTest(copies=count):
                spec = self.spec(items=("DA_AdaptiveHelm",) * count)
                fx = self.engine.compose_fx(spec)
                self.assertAlmostEqual(fx["manaMult"], multiplier)
                self.assertAlmostEqual(fx["manaRegen"], 3.0 * count)
                # Sivir's caster role receives the other-role bonus per copy.
                sheet = self.opening(spec)
                self.assertAlmostEqual(sheet["ad"], 50.0 * (1.0 + 0.10 * count))
                self.assertAlmostEqual(sheet["ap"], 100.0 + 10.0 * count)

    def test_adaptive_helm_applies_combined_multiplier_to_attacks_and_regeneration(self):
        # One attack at t=0 gives the caster's 7 mana. During one second,
        # the role regenerates 2 plus 3 per Helm. All are multiplied once.
        for count, mana in ((0, 9.0), (1, 13.8), (2, 19.5), (3, 26.1)):
            with self.subTest(copies=count):
                spec = self.spec(items=("DA_AdaptiveHelm",) * count, duration=1.01)
                spec["role"]["manaRegen"] = 2.0
                _, result = self.engine.simulate(spec, False)
                self.assertEqual(result["attacks"], 1)
                self.assertAlmostEqual(result["probe"]["mana"], mana)

    def test_bramble_vest_health_compounds_per_copy(self):
        # Sett has 1200 base HP, or 2160 at two stars. Bramble's native
        # MultiplyCompound modifier applies 1.06 once per copy.
        for count, hp in ((0, 2160.0), (1, 2289.6),
                          (2, 2426.976), (3, 2572.59456)):
            with self.subTest(copies=count):
                spec = self.spec("Sett", star=2, items=("DA_BrambleVest",) * count)
                fx = self.engine.compose_fx(spec)
                self.assertAlmostEqual(fx["hpCompoundMult"], 1.06 ** count)
                self.assertAlmostEqual(fx["hpMult"], 1.06 ** count)
                self.assertAlmostEqual(self.opening(spec)["hp"], hp)

    def test_additive_health_pool_precedes_bramble_compound_multiplier(self):
        cases = (
            # (2160 + 500) * (1 + .18 + .06) * 1.06.
            (("DA_WarmogsArmor", "DA_DragonsClaw", "DA_BrambleVest"), 3496.304),
            # (2160 + 500 + 150) * (1 + .18 + .08) * 1.06.
            (("DA_WarmogsArmor", "DA_SunfireCape", "DA_BrambleVest"), 3753.036),
        )
        for items, hp in cases:
            for ordered in (items, tuple(reversed(items))):
                with self.subTest(items=ordered):
                    spec = self.spec("Sett", star=2, items=ordered)
                    self.assertAlmostEqual(self.opening(spec)["hp"], hp)
            # Additive traits remain in the shared pool before compounding.
        spec = self.spec("Sett", star=2, items=("DA_WarmogsArmor", "DA_BrambleVest"))
        spec["traits"] = [{"api": "health-trait", "name": "Health trait",
                           "stats": [["hp", 120.0], ["hpMult", 1.25]]}]
        # (2160 + 500 + 120) * (1 + .18 + .25) * 1.06.
        self.assertAlmostEqual(self.opening(spec)["hp"], 4213.924)

    def test_warmog_and_dragon_health_percentages_remain_additive(self):
        for items, hp in (
                (("DA_WarmogsArmor",) * 2, 4297.6),
                (("DA_DragonsClaw",) * 2, 2419.2)):
            with self.subTest(items=items):
                spec = self.spec("Sett", star=2, items=items)
                self.assertAlmostEqual(self.engine.compose_fx(spec)["hpCompoundMult"], 1.0)
                self.assertAlmostEqual(self.opening(spec)["hp"], hp)


    def test_quicksilver_attack_speed_continues_after_immunity_expires(self):
        spec = self.spec(items=("DA_Quicksilver",), duration=20.01)
        fx = self.engine.compose_fx(spec)
        self.assertEqual(fx["ccImmuneDuration"], 18.0)
        self.assertEqual(fx["asPerSecond"][0][1], None)
        _, result = self.engine.simulate(spec, False)
        # Twenty one-second stacks, rather than stopping after eighteen.
        self.assertAlmostEqual(result["probe"]["asStack"], 0.60)

    def test_sunfire_keeps_source_cadence_and_each_equipped_copy(self):
        for count in (1, 2, 3):
            with self.subTest(copies=count):
                spec = self.spec("Sett", items=("DA_SunfireCape",) * count)
                for item in spec["items"]:
                    self.assertEqual(item["sunfireAura"], [0.01, 10.0, 2.0, 2.0])
                fx = self.engine.compose_fx(spec)
                self.assertEqual(fx["sunfireAuras"], [(0.01, 10.0, 2.0, 2.0)] * count)
                self.assertIsNone(fx["burnAura"])

    def test_sunfire_range_uses_equipped_adaptor_form(self):
        for item, form, enabled in (("DA_Deathblade", "AD", True),
                                     ("DA_RabadonsDeathcap", "AP", False)):
            with self.subTest(form=form):
                spec = self.spec("Nidalee", items=("DA_SunfireCape", item, item))
                fx = self.engine.compose_fx(spec)
                self.assertEqual(fx["form"], form)
                self.assertEqual(bool(fx["sunfireAuras"]), enabled)
        spec = self.spec(items=("DA_SunfireCape",))
        self.assertNotIn("sunfireAura", spec["items"][0])
        self.assertEqual(self.engine.compose_fx(spec)["sunfireAuras"], [])

    def test_sunfire_rejects_invalid_application_intervals_and_ranges(self):
        for value in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(interval=value), self.assertRaises(ValueError):
                spec = self.spec("Sett", items=("DA_SunfireCape",))
                spec["items"][0]["sunfireAura"][2] = value
                self.engine.compose_fx(spec)
        for values in ([0.01, 10.0, 2.0], [0.01, 0.0, 2.0, 2.0],
                       [1.1, 10.0, 2.0, 2.0], [0.01, 10.0, 2.0, float("nan")]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                spec = self.spec("Sett", items=("DA_SunfireCape",))
                spec["items"][0]["sunfireAura"] = deepcopy(values)
                self.engine.compose_fx(spec)

    def test_legacy_synthetic_burn_aura_remains_supported(self):
        spec = self.spec("Sett")
        spec["items"].append({"api": "synthetic-aura", "name": "Synthetic aura",
                              "stats": [], "burnAura": [0.01, 3.0]})
        fx = self.engine.compose_fx(spec)
        self.assertEqual(fx["burnAura"], (0.01, 3.0))
        self.assertEqual(fx["sunfireAuras"], [])

    def test_red_buff_requires_attack_or_spell_while_morello_accepts_unit_damage(self):
        for api, requires_tags in (("DA_RedBuff", True), ("DA_Morellonomicon", False)):
            with self.subTest(item=api):
                spec = self.spec(items=(api,))
                self.assertEqual(spec["items"][0].get("burnRequiresAttackOrSpell", False), requires_tags)
                fx = self.engine.compose_fx(spec)
                self.assertEqual(fx["burnRequiresTags"], [requires_tags])
                # Existing callers still receive rate/duration/stacks tuples.
                self.assertEqual(len(fx["burnOnHit"][0]), 3)
                self.assertFalse(fx["burnOnHit"][0][2])
        spec = self.spec()
        spec["traits"] = [{"api": "trait-burn", "name": "Trait burn",
                           "stats": [], "burnOnHit": [0.01, 3.0]}]
        fx = self.engine.compose_fx(spec)
        self.assertEqual(fx["burnOnHit"], [(0.01, 3.0, True)])
        self.assertEqual(fx["burnRequiresTags"], [True])


def sentinel(amount=10, *, land=0.25, repeat=False, spark=False):
    member = ally("Sentinel", driver="Sentinel", hp=10000, cast=True,
                  attack_speed=1 if repeat else 0.01,
                  fx=[{"ionicSpark": 1.0}] if spark else [])
    flat_calc(member, "MagicDamageCalc1", 0)
    flat_calc(member, "ShieldCalc1", 0)
    member["spec"]["kits"]["base"]["rows"].update(
        KnockupDuration=0.0, ManaReaveFlat=float(amount))
    member["spec"]["unit"]["castTime"] = land
    if repeat:
        member["spec"]["kits"]["base"]["stats"].update(mana=5, initialMana=0)
    return member


def target(initial=0):
    member = caster(damage=0, hp=10000, attack_speed=1)
    member["spec"]["kits"]["base"]["stats"].update(mana=20, initialMana=initial)
    return member


def dummy_fight(reaver, *, initial=0, first_attack=0, duration=7.1, periodic=False):
    spec = deepcopy(reaver["spec"])
    spec.update(duration=duration, dummies={"critEv": 1.0, "slots": [{
        "hp": 10000, "armor": 0, "mr": 0, "ad": 1, "as": 1,
        "ability": 100, "physicalShare": 0, "manaMax": 20,
        "manaStart": initial, "manaPerAttack": 7, "manaFromDamage": False,
        "attackStart": first_attack,
        **({"castInterval": 2, "castStart": 0.5} if periodic else {}),
    }]})
    return ENGINE.simulate(spec, True)[1]


class TestChampionManaReave(unittest.TestCase):
    def test_archived_tooltip_defines_next_cast_cost_increase(self):
        self.assertIn("Increase the Mana cost of the next Ability cast",
                      SNAP.unit("Sentinel")["ability"]["desc"])

    def test_low_mana_keeps_the_entire_cost_increase_until_one_cast(self):
        for initiative in (0, 1):
            with self.subTest(initiative=initiative):
                result = match([sentinel()], [target()], duration=7.1,
                               initiative=initiative)
                casts = combat_events(result, "cast", "enemy")
                # At .25s, the target has only 7 mana. Adding 10 to the
                # next cast's cost requires five attacks for 35 mana at 4s.
                # The following cast has the normal 20 cost and 5 overflow.
                self.assertEqual([hit["time"] for hit in casts], [4.0, 7.0])
                self.assertEqual([hit["amount"] for hit in casts], [35.0, 26.0])

    def test_empty_bar_reave_delays_cast_from_mana_regeneration(self):
        for initiative in (0, 1):
            with self.subTest(initiative=initiative):
                receiver = target()
                receiver["spec"]["unit"]["kind"] = "Specialist"
                receiver["spec"]["role"]["manaRegen"] = 10
                result = match([sentinel()], [receiver], duration=3.1,
                               initiative=initiative)
                self.assertEqual([(hit["time"], hit["amount"]) for hit in
                                  combat_events(result, "cast", "enemy")], [(3.0, 30.0)])

    def test_full_bar_is_preserved_and_ionic_uses_the_cost_of_each_cast(self):
        result = match([sentinel(land=0, spark=True)], [target(initial=20)],
                       duration=4.1)
        casts = combat_events(result, "cast", "enemy")
        self.assertEqual([hit["time"] for hit in casts], [1.0, 4.0])
        self.assertEqual([hit["amount"] for hit in casts], [34.0, 25.0])
        self.assertEqual([hit["amount"] for hit in combat_events(
            result, "damage", name="ionic spark")], [30.0, 20.0])

    def test_reave_during_a_cast_is_reserved_for_the_next_cast(self):
        result = match([sentinel()], [target(initial=20)], duration=4.3)
        self.assertEqual([(hit["time"], hit["amount"]) for hit in
                          combat_events(result, "cast", "enemy")], [(0.0, 27.0), (4.0, 35.0)])
        self.assertEqual([hit["time"] for hit in combat_events(result, "land", "enemy")],
                         [0.25, 4.25])

    def test_repeated_reaves_keep_the_strongest_pending_debuff(self):
        for first, second, cast_time, mana in ((10, 10, 4.0, 35.0),
                                              (10, 20, 5.0, 42.0),
                                              (20, 10, 5.0, 42.0)):
            with self.subTest(first=first, second=second):
                result = match([sentinel(first), sentinel(second, land=1.25)],
                               [target()], duration=5.1)
                self.assertEqual([(hit["time"], hit["amount"]) for hit in
                                  combat_events(result, "cast", "enemy")], [(cast_time, mana)])

    def test_new_reave_after_a_cast_applies_to_the_following_cast(self):
        result = match([sentinel(), sentinel(land=4.25)], [target()], duration=8.1)
        self.assertEqual([(hit["time"], hit["amount"]) for hit in
                          combat_events(result, "cast", "enemy")], [(4.0, 35.0), (8.0, 33.0)])

    def test_manaless_champion_does_not_gain_a_mana_bar(self):
        receiver = ally(ad=10, attack_speed=1)
        result = match([sentinel()], [receiver], duration=2.1)
        self.assertEqual(result["enemies"][0]["attacks"], 3)
        self.assertEqual(result["enemies"][0]["casts"], 0)


class TestDummyManaReave(unittest.TestCase):
    def test_low_mana_dummy_pays_the_full_increase_then_returns_to_base_cost(self):
        result = dummy_fight(sentinel())
        self.assertEqual([hit[0] for hit in solo_events(result, "take", "magic")],
                         [4.0, 7.0])

    def test_empty_dummy_bar_keeps_reave(self):
        result = dummy_fight(sentinel(), first_attack=1, duration=5.1)
        self.assertEqual([hit[0] for hit in solo_events(result, "take", "magic")], [5.0])

    def test_full_dummy_bar_and_ionic_use_next_cost_once(self):
        result = dummy_fight(sentinel(land=0, spark=True), initial=20,
                             first_attack=0.5, duration=4.6)
        self.assertEqual([hit[0] for hit in solo_events(result, "take", "magic")],
                         [1.5, 4.5])
        self.assertEqual([(hit[0], hit[2]) for hit in
                          solo_events(result, "damage", "ionic spark")],
                         [(1.5, 30.0), (4.5, 20.0)])

    def test_repeated_dummy_reaves_do_not_accumulate_cost(self):
        result = dummy_fight(sentinel(repeat=True), duration=4.1)
        self.assertEqual([hit[0] for hit in solo_events(result, "take", "magic")], [4.0])
        self.assertEqual([hit[0] for hit in solo_events(result, "cast")],
                         [0.0, 1.0, 2.0, 3.0, 4.0])

    def test_fixed_interval_fixtures_keep_their_declared_spell_times(self):
        result = dummy_fight(sentinel(repeat=True), periodic=True, duration=4.6)
        self.assertEqual([hit[0] for hit in solo_events(result, "take", "magic")],
                         [0.5, 2.5, 4.5])
        shared = fight([sentinel(repeat=True)], [
            enemy(ability=100, physicalShare=0, manaMax=20,
                  castInterval=2, castStart=0.5)], duration=4.6)
        self.assertEqual([hit["time"] for hit in trace(shared, "enemySpell")],
                         [0.5, 2.5, 4.5])


def blessing(*names, primal=True):
    effect = {"api": "DA_Primal18", "name": "Primal", "stats": [],
              "selectedBlessings": list(names)}
    if "tiger" in names:
        effect["timedStats"] = [{"after": 6, "interval": 0,
                                 "stats": [["asPct", 0.35 if primal else 0.15]]}]
    if "turtle" in names:
        effect["healPerInterval"] = [0.04, 4]
    if "bear" in names and primal:
        effect["executeBelowHp"] = 0.12
    return effect


def finite_attacker(damage, *, armor=0, duration=0.1, effects=(), fx=()):
    spec = plain_spec(damage=damage, target_armor=armor, fx=fx)
    spec.update(immortal=False, duration=duration)
    spec["dummies"]["slots"][0]["hp"] = 100
    spec["traits"] = list(effects)
    return spec


class TestPrimalBear(unittest.TestCase):
    def test_executes_only_after_positive_damage_leaves_health_strictly_below_threshold(self):
        for damage, expected, execute in ((87, 87, []), (88, 88, []), (89, 100, [11])):
            with self.subTest(damage=damage):
                _, result = ENGINE.simulate(finite_attacker(damage, effects=[blessing("bear")]), True)
                self.assertEqual(result["total"], expected)
                self.assertEqual([hit[2] for hit in events(result, "damage", "primal bear")], execute)

    def test_threshold_uses_remaining_health_after_mitigation(self):
        for damage, expected in ((176, 88), (178, 100)):
            with self.subTest(damage=damage):
                _, result = ENGINE.simulate(finite_attacker(damage, armor=100,
                                                           effects=[blessing("bear")]), True)
                self.assertEqual(result["total"], expected)

    def test_owned_item_burn_can_cross_the_execute_threshold(self):
        # A positive attack applies the burn; its ticks then cross the threshold.
        _, result = ENGINE.simulate(finite_attacker(1, duration=1.1,
            effects=[blessing("bear")], fx=[{"burnOnHit": [0.9, 2]}]), True)
        self.assertEqual(result["total"], 100)
        self.assertEqual([(hit[0], hit[2]) for hit in events(result, "damage", "primal bear")], [(1, 9)])

    def test_nonprimal_ally_cannot_trigger_a_primal_members_execute_without_their_damage(self):
        for primal_damage, expected in ((0, []), (1, [10])):
            with self.subTest(primal_damage=primal_damage):
                member = ally("Vi", ad=primal_damage)
                member["spec"]["traits"] = [blessing("bear")]
                result = fight([ally("Ashe", ad=89), member], [enemy(hp=100)], duration=0.1)
                self.assertEqual([hit["amount"] for hit in trace(result, "damage", name="primal bear")], expected)
                self.assertTrue(all(hit["source"] == 1 for hit in trace(result, "damage", name="primal bear")))

    def test_execute_bypasses_new_shield_without_crediting_unused_shield_health(self):
        member = ally("Vi", ad=89)
        member["spec"]["traits"] = [blessing("bear")]
        target = ally("Leona", hp=100, fx=[{"shieldAtHp": [0.15, 1, 10, False]}])
        result = champion_match([member], [target], duration=0.1)
        self.assertEqual(result["damage"], 100)
        self.assertFalse(result["enemies"][0]["alive"])
        self.assertEqual([hit["amount"] for hit in match_events(result, "damage", name="primal bear")], [11])



class TestPrimalTurtle(unittest.TestCase):
    def test_first_heal_is_at_four_seconds_and_repeats_at_eight(self):
        spec = plain_spec(damage=0, incoming_dps=100)
        spec["traits"] = [blessing("turtle")]
        spec["duration"] = 8.1
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([(hit[0], hit[2]) for hit in events(result, "heal", "primal turtle")], [(4, 40), (8, 40)])

    def test_heals_apply_wound_before_capping_at_missing_health(self):
        for dps, expected in ((1, 3), (100, 26.8)):
            with self.subTest(dps=dps):
                spec = plain_spec(damage=0, incoming_dps=dps)
                spec["traits"] = [blessing("turtle")]
                spec["enemyDebuffs"]["wound"] = 0.33
                spec["duration"] = 4.1
                _, result = ENGINE.simulate(spec, True)
                heals = events(result, "heal", "primal turtle")
                self.assertEqual(len(heals), 1)
                self.assertAlmostEqual(heals[0][2], expected)

    def test_max_health_growth_changes_the_heal_at_its_actual_tick(self):
        spec = plain_spec(damage=0, incoming_dps=100)
        spec["traits"] = [blessing("turtle"), {"api": "growth", "name": "growth", "stats": [],
            "timedStats": [{"after": 2, "interval": 0, "stats": [["hp", 1000]]}]}]
        spec["duration"] = 4.1
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([hit[2] for hit in events(result, "heal", "primal turtle")], [80])

    def test_item_healing_and_turtle_keep_independent_clocks_and_sources(self):
        spec = plain_spec(damage=0, incoming_dps=100, fx=[{"healPerInterval": [0.02, 2]}])
        spec["traits"] = [blessing("turtle")]
        spec["duration"] = 4.1
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([(hit[0], hit[2]) for hit in events(result, "heal", "dragon's claw")], [(2, 20), (4, 20)])
        self.assertEqual([(hit[0], hit[2]) for hit in events(result, "heal", "primal turtle")], [(4, 40)])

    def test_dead_champion_cannot_heal_even_while_a_death_body_holds(self):
        spec = spec_for("Krug", star=1, dummy=generic_targets(incoming_dps=300,
            physical_share=1, target_count=1), pressure=True, duration=4.1)
        spec["enemyDebuffs"] = {}
        kit = spec["kits"]["base"]
        kit.update(hpStar=250, baseAd=0)
        kit["stats"].update(hp=250, ad=0, armor=0, mr=0, mana=10**6, initialMana=0)
        kit["calcs"]["HealthCalc1"] = {"dtype": "magic", "terms": [{"type": "flat", "value": 1000, "op": "add"}]}
        spec["traits"] = [blessing("turtle", primal=False)]
        _, result = ENGINE.simulate(spec, True)
        self.assertIsNotNone(result["diedAt"])
        self.assertEqual(events(result, "heal", "primal turtle"), [])



class TestPrimalOtherBlessings(unittest.TestCase):
    def test_tiger_preserves_delayed_member_and_team_amounts(self):
        for primal, bonus in ((True, 0.35), (False, 0.15)):
            with self.subTest(primal=primal):
                spec = plain_spec()
                spec["traits"] = [blessing("tiger", primal=primal)]
                spec["duration"] = 6.1
                _, result = ENGINE.simulate(spec, True)
                grants = events(result, "trait stats")
                self.assertEqual([(hit[0], hit[2]) for hit in grants], [(6, bonus)])

    def test_phoenix_adds_no_damage_healing_or_items_to_a_fixed_fight(self):
        spec = plain_spec(incoming_dps=10)
        spec["duration"] = 8.1
        before = ENGINE.simulate(spec, True)
        spec["traits"] = [blessing("phoenix")]
        self.assertEqual(ENGINE.simulate(spec, True), before)

    def test_tiger_and_turtle_pair_retains_both_distinct_effects(self):
        spec = plain_spec(damage=0, incoming_dps=100)
        spec["traits"] = [blessing("tiger", "turtle")]
        spec["duration"] = 8.1
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([hit[0] for hit in events(result, "trait stats")], [6])
        self.assertEqual([(hit[0], hit[2]) for hit in events(result, "heal", "primal turtle")], [(4, 40), (8, 40)])

    def test_invalid_execute_or_heal_parameters_fail_before_simulation(self):
        for field, value in (("executeBelowHp", -0.1), ("executeBelowHp", 1.1),
                             ("executeBelowHp", float("nan")), ("healPerInterval", [0.04, 0]),
                             ("healPerInterval", [0.04, float("inf")]), ("healPerInterval", [1.1, 4])):
            with self.subTest(field=field, value=value):
                spec = plain_spec()
                spec["traits"] = [{"api": "DA_Primal18", "name": "Primal", "stats": [], field: value}]
                with self.assertRaises(ValueError):
                    ENGINE.simulate(spec, False)


class TestAuditedClassTraits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.hand = tft.load_trait_effects(18)

    def test_vanguard_capstone_mapping_preserves_its_shield_condition(self):
        api = "DA_18_Vanguard"
        raw = next(trait for trait in self.snap.raw["traits"] if trait["apiName"] == api)
        self.assertIn("while Shielded", raw["effects"][2]["desc"])
        for column, shield, durability in ((1, .18, 0), (2, .32, 0), (3, .42, .05)):
            effect = tft.trait_spec(self.snap, api, column, self.hand, self.snap.unit("Rakan"))
            self.assertNotIn("durability", effect)
            self.assertAlmostEqual(effect["durabilityWhileShielded"], durability)
            self.assertEqual(effect["shieldAtStart"], [shield, 10])
            self.assertEqual(effect["shieldAtHp"], [.5, shield, 10])

    def test_juggernaut_four_team_share_matches_archived_explicit_source(self):
        api = "DA_Juggernaut18"
        raw = next(trait for trait in self.snap.raw["traits"] if trait["apiName"] == api)
        source = next(trait for trait in self.snap.communitydragon["traits"] if trait["apiName"] == api)
        source_four = next(effect for effect in source["effects"] if effect["minUnits"] == 4)
        # The sparse lookup omits column 2; correct only this known conflict,
        # rather than changing hold-previous semantics across every curve.
        self.assertNotIn(2, [column for column, _ in raw["curveTable"]["TeamDurability"]])
        self.assertAlmostEqual(source_four["variables"]["{f8c73243}"], .06, places=6)
        normalized = self.snap.traits[api]["curve"]["TeamDurability"]
        self.assertEqual([tft.curve_at(normalized, column) for column in (0, 1, 2, 3)], [1, .96, .94, .92])
        members = [{"api": self.snap.unit(name)["api"], "star": 2}
                   for name in ("Rakan", "Yorick", "Sejuani", "Vi", "Karma")]
        resolved = resolve_board_traits(self.snap, members)
        for name, expected in (("Rakan", .3), ("Karma", .06)):
            effect = next(effect for effect in resolved["effects"][self.snap.unit(name)["api"]] if effect["api"] == api)
            self.assertAlmostEqual(effect["durability"], expected)
        check = next(check for check in self.snap.audit["checks"]
                     if check["id"] == "trait:DA_Juggernaut18:TeamDurability:columns-2")
        self.assertEqual(check["expected"], [.94])
        archived = Path(self.snap.dir, "communitydragon.json")
        self.assertEqual(check["source"]["sha256"], hashlib.sha256(archived.read_bytes()).hexdigest())
        notes = json.loads(Path(self.snap.dir, "patchnotes.json").read_text())
        findings, _ = tft.check_audit(self.snap, notes)
        self.assertEqual(next(row["status"] for row in findings if row["what"] == check["what"]), "current")



class TestHunterDamageAmp(unittest.TestCase):
    def test_stable_primary_grants_amp_to_secondary_ability_damage_after_four_seconds(self):
        snap = tft.load_snapshot(18, "18.1d")
        unit = snap.unit("Sivir")
        effect = tft.trait_spec(snap, "DA_18_Hunter", 1, tft.load_trait_effects(18), unit)
        dummy = generic_targets(target_count=3)
        spec = tft.cell_spec(snap, unit, 2, "clump", [], dummy, pressure=False)
        spec["traits"] = [effect]
        spec.update(immortal=True, autoPressure=False,
                    enemyDebuffs=dict(dummy["enemyDebuffs"]),
                    targetDebuffs=dict(dummy["targetDebuffs"]))
        spec["duration"] = 12
        stats = spec["kits"]["base"]["stats"]
        stats["initialMana"] = stats["mana"]
        baseline = deepcopy(spec)
        baseline["traits"][0].pop("ampAfterSameTarget")
        damage = lambda result: [event for event in result["trace"] if event[1] == "damage"]
        before = damage(tft.engine().simulate(baseline, True)[1])
        after = damage(tft.engine().simulate(spec, True)[1])
        self.assertEqual([(event[0], event[3], event[4]) for event in before],
                         [(event[0], event[3], event[4]) for event in after])
        secondary_early = secondary_late = 0
        for normal, amplified in zip(before, after, strict=True):
            multiplier = 1.1 if normal[0] >= 4 else 1
            self.assertAlmostEqual(amplified[2], normal[2] * multiplier)
            if normal[3] != 0 and normal[4] == "bounces":
                secondary_early += normal[0] < 4
                secondary_late += normal[0] >= 4
        self.assertGreater(secondary_early, 0)
        self.assertGreater(secondary_late, 0)

    def test_primary_target_death_restarts_the_four_second_timer(self):
        spec = plain_spec(damage=100)
        first = dict(spec["dummies"]["slots"][0], hp=600)
        second = dict(first, hp=10000)
        spec.update(immortal=False, duration=12,
                    dummies={"critEv": 1, "slots": [first, second]},
                    traits=[{"api": "test-hunter", "name": "Hunter", "stats": [],
                             "ampAfterSameTarget": [.1, 4]}])
        result = tft.engine().simulate(spec, True)[1]
        attacks = {event[0]: event for event in result["trace"]
                   if event[1] == "damage" and event[4] == "auto"}
        self.assertAlmostEqual(attacks[4][2], 110)
        self.assertAlmostEqual(attacks[5][2], 90)
        self.assertEqual(attacks[5][3], 0)
        for time in (6, 7, 8):
            self.assertEqual((attacks[time][2], attacks[time][3]), (100, 1))
        self.assertAlmostEqual(attacks[9][2], 110)


def spellweaver_member(api, *, points=1.0, tagged=True, fast=False):
    member = ally("Ahri", driver="Ahri", hp=10000, cast=True)
    spec = member["spec"]
    spec["unit"].update(api=api, name=api, objective="carry")
    spec["traits"] = [{"api": "DA_18_Spellweaver" if tagged else "generic-own-cast",
                        "name": "Spellweaver" if tagged else "Own-cast AP",
                        "stats": [], "apPerCast": points}]
    spec["dummies"] = {"slots": [{"hp": 100000, "armor": 0, "mr": 0,
                                   "nearby": True}], "critEv": 1}
    kit = spec["kits"]["base"]
    kit["rows"].update(ChannelTime=0.25, HexPercentDamageFalloffTooltip=0.0)
    kit["calcs"]["MagicDamageCalc1"] = {"dtype": "magic", "terms": [
        {"type": "scaled", "coef": 100.0, "scaling": "AbilityPower", "op": "add"}]}
    if fast:
        spec["role"]["manaRegen"] = 400
        kit["stats"].update(mana=100, initialMana=100)
    return member


class TestSharedSpellweaver(unittest.TestCase):
    def test_actual_casts_grant_self_once_and_every_other_member_once(self):
        for points in (1.0, 2.0):
            for mode in ("standalone", "team", "symmetric"):
                with self.subTest(points=points, mode=mode):
                    members = [spellweaver_member("TFT18_Ahri", points=points),
                               spellweaver_member("TFT18_Karma", points=points)]
                    if mode == "standalone":
                        spec = members[0]["spec"]
                        spec["duration"] = 0.4
                        _, result = ENGINE.simulate(spec, True)
                        self.assertEqual(result["total"], 100 + points)
                        continue
                    if mode == "team":
                        result = fight(members, [enemy(hp=100000)], duration=0.4)
                        amounts = [row["damage"] for row in result["allies"]]
                    else:
                        result = champion_match(members, [ally(hp=100000)], duration=0.4)
                        amounts = [row["damage"] for row in result["allies"]]
                    self.assertEqual(amounts, [100 + 2 * points] * 2)

    def test_generic_own_cast_ap_is_neither_a_spellweaver_emitter_nor_recipient(self):
        members = [spellweaver_member("TFT18_Ahri"), spellweaver_member("TFT18_Karma"),
                   spellweaver_member("TFT18_Leona", tagged=False)]
        for mode in ("team", "symmetric"):
            with self.subTest(mode=mode):
                if mode == "team":
                    result = fight(members, [enemy(hp=100000)], duration=0.4)
                    amounts = [row["damage"] for row in result["allies"]]
                else:
                    result = champion_match(members, [ally(hp=100000)], duration=0.4)
                    amounts = [row["damage"] for row in result["allies"]]
                self.assertEqual(amounts, [102, 102, 101])

    def test_unequal_cast_cadences_share_actual_events_without_a_time_estimate(self):
        members = [spellweaver_member("TFT18_Ahri", fast=True),
                   spellweaver_member("TFT18_Karma")]
        for mode in ("team", "symmetric"):
            with self.subTest(mode=mode):
                if mode == "team":
                    result = fight(members, [enemy(hp=100000)], duration=1.6)
                    casts = trace(result, "cast", source=0)
                    self.assertEqual([row["time"] for row in casts], [0, 1.25])
                    amounts = [row["damage"] for row in result["allies"]]
                else:
                    result = champion_match(members, [ally(hp=100000)], duration=1.6)
                    casts = match_events(result, "cast", source=0)
                    self.assertEqual([row["time"] for row in casts], [0, 1.25])
                    amounts = [row["damage"] for row in result["allies"]]
                self.assertEqual(amounts, [102 + 103, 102])

    def test_dead_spellweaver_receives_no_later_cast_bonus(self):
        members = [spellweaver_member("TFT18_Ahri", fast=True),
                   spellweaver_member("TFT18_Karma")]
        recipient = members[1]
        recipient["lane"] = 0
        recipient["spec"]["kits"]["base"]["hpStar"] = 1
        result = fight(members, [enemy(ad=10, lane=0, **{"as": 1}, attackStart=0.1)], duration=1.6)
        self.assertFalse(result["allies"][1]["alive"])
        self.assertEqual([row["time"] for row in trace(result, "traitAP", source=1)], [0])
        self.assertEqual([row["time"] for row in trace(result, "cast", source=0)], [0, 1.25])


def solar_member(*, mr=100, magic=0.2, true=0.2, amp=0.0, hp=100000):
    member = ally(ad=100, attack_speed=0.01)
    spec = member["spec"]
    spec["traits"] = [{"api": "DA_18_Solar", "name": "Solar", "stats": [["amp", amp]],
                        "bonusMagicPct": magic, "bonusTruePct": true}]
    spec["dummies"] = {"slots": [{"hp": hp, "armor": 0, "mr": mr,
                                   "nearby": True}], "critEv": 1}
    spec["duration"] = 0.1
    return member


class TestSolarTrueConversion(unittest.TestCase):
    def test_magic_and_true_bonus_use_the_same_base_and_do_not_recurse(self):
        for mr in (0, 100):
            for amp in (0.0, 0.5):
                expected = 100 * (1 + amp) * (1 + 0.2 * 100 / (100 + mr) + 0.2)
                for mode in ("standalone", "team", "symmetric"):
                    with self.subTest(mr=mr, amp=amp, mode=mode):
                        member = solar_member(mr=mr, amp=amp)
                        if mode == "standalone":
                            _, result = ENGINE.simulate(member["spec"], True)
                            total = result["total"]
                            self.assertEqual(set(result["breakdown"]), {"auto", "solar", "solar true"})
                        elif mode == "team":
                            result = fight([member], [enemy(mr=mr, hp=100000)], duration=0.1)
                            total = result["damage"]
                        else:
                            result = champion_match([member], [ally(mr=mr, hp=100000)], duration=0.1)
                            total = result["damage"]
                        self.assertAlmostEqual(total, expected)

    def test_conversion_preserves_bonus_budget_when_resistance_is_zero(self):
        for magic, true in ((0.4, 0.0), (0.2, 0.2), (0.0, 0.4)):
            with self.subTest(magic=magic, true=true):
                _, result = ENGINE.simulate(solar_member(mr=0, magic=magic, true=true)["spec"], True)
                self.assertEqual(result["total"], 140)

    def test_lethal_bonus_keeps_original_target_and_does_not_spill_to_next_enemy(self):
        for hp, mr, expected_true in ((105, 0, 0), (120, 100, 10)):
            for mode in ("standalone", "symmetric"):
                with self.subTest(hp=hp, mr=mr, mode=mode):
                    member = solar_member(mr=mr, hp=hp)
                    if mode == "standalone":
                        member["spec"]["dummies"]["slots"].append({"hp": 100000, "armor": 0, "mr": 0})
                        _, result = ENGINE.simulate(member["spec"], True)
                        self.assertEqual(result["total"], hp)
                        self.assertFalse(events(result, "damage", target=1))
                        true_total = sum(row[2] for row in events(result, "damage", "solar true"))
                    else:
                        result = champion_match([member], [ally(hp=hp, mr=mr), ally(hp=100000)], duration=0.1)
                        self.assertEqual(result["damage"], hp)
                        self.assertFalse([row for row in match_events(result, "damage") if row["target"] == 1])
                        true_total = sum(row["amount"] for row in match_events(result, "damage", name="solar true"))
                    self.assertEqual(true_total, expected_true)

    def test_absent_and_zero_true_fraction_preserve_existing_standalone_output(self):
        spec = solar_member(true=0)["spec"]
        expected = ENGINE.simulate(spec, True)
        spec["traits"][0].pop("bonusTruePct")
        self.assertEqual(ENGINE.simulate(spec, True), expected)

    def test_invalid_true_fraction_fails_instead_of_poisoning_scores(self):
        for invalid in (-0.01, math.nan, math.inf, -math.inf):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(ValueError, "bonusTruePct"):
                    ENGINE.simulate(solar_member(true=invalid)["spec"], False)


PRIMAL = "DA_Primal18"


def trait(api, column, name="Cinderling"):
    return tft.trait_spec(SNAP, api, column, tft.load_trait_effects(SNAP.set_no), SNAP.unit(name))


class TestTraitTimingResolution(unittest.TestCase):
    def test_primal_member_and_team_speed_are_delayed_and_not_opening_stats(self):
        members = [{"api": SNAP.unit(name)["api"], "star": 2} for name in ("Sivir", "Vi", "Leona")]
        board = resolve_board_traits(SNAP, members)
        for member in members:
            effect = next(row for row in board["effects"][member["api"]] if row["api"] == PRIMAL)
            self.assertEqual(effect["stats"], [])
            self.assertEqual(effect["timedStats"][0]["after"], 6)
            self.assertEqual(effect["timedStats"][0]["interval"], 0)
            expected = 0.15 if member["api"] == SNAP.unit("Leona")["api"] else 0.35
            self.assertAlmostEqual(dict(effect["timedStats"][0]["stats"])["asPct"], expected)

    def test_rift_growth_only_begins_at_the_capstone(self):
        for column in (1, 2):
            effect = trait(RIFTBEAST, column)
            self.assertEqual(effect["timedStats"], [])
            self.assertTrue(all(value == 0 for _, value in effect["stats"]))
        growth = trait(RIFTBEAST, 3)["timedStats"][0]
        self.assertEqual((growth["after"], growth["interval"]), (5, 5))
        values = dict(growth["stats"])
        self.assertEqual((values["ap"], values["armor"], values["mr"], values["hp"], values["manaRegen"]),
                         (5, 5, 5, 50, 1))
        self.assertAlmostEqual(values["adPct"], 0.05)
        self.assertAlmostEqual(values["asPct"], 0.05)


class TestNativeTraitTiming(unittest.TestCase):
    def test_sentinel_alpha_has_no_unsupported_opening_self_regeneration(self):
        unit = SNAP.unit("Sentinel")
        self.assertIn("Each time Sentinel casts, allies gain", unit["ability"]["desc"])
        self.assertIn('row="TraitAlliesManaRegen"', unit["ability"]["desc"])
        self.assertEqual(tft.curve_at(unit["curve"]["TraitAlliesManaRegen"], 1), 2)
        spec = spec_for("Sentinel", star=2, duration=4.1, pressure=False)
        spec["kits"]["base"]["stats"].update(mana=10**9, initialMana=0)
        ordinary = ENGINE.simulate(spec, False)
        marked = deepcopy(spec)
        marked["traits"] = [trait(RIFTBEAST, 1, "Sentinel")]
        self.assertTrue(marked["traits"][0]["riftbeast"])
        self.assertEqual(ENGINE.simulate(marked, False), ordinary)


    def test_tiger_speed_has_no_early_uptime_and_triggers_once_at_six_seconds(self):
        spec = plain_spec()
        spec["traits"] = [trait(PRIMAL, 1, "Sivir")]
        spec["duration"] = 15.1
        opening, result = ENGINE.simulate(spec, True)
        self.assertEqual(opening["as"], 1.0)
        grants = events(result, "trait stats")
        self.assertEqual([event[0] for event in grants], [6])
        self.assertAlmostEqual(grants[0][2], 0.35)

    def test_rift_growth_repeats_all_stats_and_does_not_retroactively_grant_mana(self):
        spec = plain_spec()
        spec["unit"]["kind"] = "Specialist"  # isolate regeneration from attack mana
        spec["traits"] = [trait(RIFTBEAST, 3)]
        spec["duration"] = 10.1
        opening, result = ENGINE.simulate(spec, True)
        self.assertEqual((opening["hp"], opening["armor"], opening["mr"], opening["ad"], opening["ap"]),
                         (1050, 5, 5, 105, 105))
        self.assertEqual([event[0] for event in events(result, "trait stats")], [5, 10])
        self.assertAlmostEqual(result["probe"]["mana"], 15)
        damage = events(result, "damage", "auto")
        for event in damage:
            self.assertAlmostEqual(event[2], 105 if event[0] < 5 else 110 if event[0] < 10 else 115)

    def test_shared_match_uses_the_same_delayed_trait_clock(self):
        member = ally(ad=100, attack_speed=1)
        member["spec"]["traits"] = [trait(PRIMAL, 1, "Sivir")]
        before = deepcopy(member)
        result = match([member], [ally(hp=10000)], duration=6.8)
        attacks = [event for event in result["trace"] if event["side"] == "ally" and event["kind"] == "damage"
                   and event.get("sourceName") == "auto"]
        self.assertEqual(len(attacks), 8)
        self.assertLess(attacks[-1]["time"], 6.8)
        self.assertGreater(attacks[-1]["time"], 6)
        self.assertEqual(member, before)


if __name__ == "__main__":
    unittest.main()
