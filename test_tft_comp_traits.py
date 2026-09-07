"""Composition trait counts, team effects and unique effect ownership."""

from copy import deepcopy
import json
import unittest

import tft
from tft_comp_traits import resolve_board_traits
from tft_board import slots_used


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
        # The archived team curve omits column2. Preserve the existing
        # hold-previous curve convention rather than inventing interpolation.
        for names, own, team in ((["Rakan", "Yorick", "Karma"], 0.2, 0.04),
                                 (["Rakan", "Yorick", "Sejuani", "Vi", "Karma"], 0.3, 0.04),
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
        self.assertTrue(any("recurring growth" in text for text in board["limitations"]))

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

    def test_solar_unsupported_high_upgrade_effects_are_explicit(self):
        board = self.board("Kayle", "Leona", "Sejuani", "Karma", "Kobuko", "Ornn", "Rakan", "Varus", star=3)
        self.assertTrue(any("true damage" in text for text in board["limitations"]))
        self.assertTrue(any("ascension" in text for text in board["limitations"]))
        self.assertAlmostEqual(self.effect(board, "Karma", "Solar")["bonusMagicPct"], 0.19)

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


if __name__ == "__main__":
    unittest.main()
