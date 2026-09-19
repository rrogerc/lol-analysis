"""Delayed Primal and recurring Riftbeast grants use the native combat clock."""

from copy import deepcopy
import unittest

import tft
from tft_comp_traits import RIFTBEAST, resolve_board_traits
from test_tft import ENGINE, SNAP, events, spec_for
from test_tft_unit_profiles import plain_spec
from test_tft_team_engine import ally
from test_tft_symmetric import match


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
        opening, samples = ENGINE.measure_response(spec, [5.75, 6, 6.75])
        self.assertEqual(opening["as"], 1.0)
        self.assertEqual([row["damage"] for row in samples], [600, 700, 800])
        spec["duration"] = 15.1
        _, result = ENGINE.simulate(spec, True)
        grants = events(result, "trait stats")
        self.assertEqual([event[0] for event in grants], [6])
        self.assertAlmostEqual(grants[0][2], 0.35)

    def test_rift_growth_repeats_all_stats_and_does_not_retroactively_grant_mana(self):
        spec = plain_spec()
        spec["unit"]["kind"] = "Specialist"  # isolate regeneration from attack mana
        spec["traits"] = [trait(RIFTBEAST, 3)]
        opening, samples = ENGINE.measure_response(spec, [4.75, 5, 9.75, 10])
        self.assertEqual((opening["hp"], opening["armor"], opening["mr"], opening["ad"], opening["ap"]),
                         (1050, 5, 5, 105, 105))
        self.assertEqual([row["hp"] for row in samples], [1050, 1100, 1100, 1150])
        self.assertEqual([row["armor"] for row in samples], [5, 10, 10, 15])
        self.assertEqual([row["mr"] for row in samples], [5, 10, 10, 15])
        self.assertEqual([row["selfHeal"] for row in samples], [0, 0, 0, 0])
        spec["duration"] = 10.1
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([event[0] for event in events(result, "trait stats")], [5, 10])
        self.assertAlmostEqual(result["probe"]["mana"], 15)
        damage = events(result, "damage", "auto")
        for event in damage:
            self.assertAlmostEqual(event[2], 105 if event[0] < 5 else 110 if event[0] < 10 else 115)

    def test_rift_flat_health_growth_uses_existing_health_multipliers(self):
        spec = plain_spec(fx=[{"hpMult": 1.2}])
        spec["traits"] = [trait(RIFTBEAST, 3)]
        opening, samples = ENGINE.measure_response(spec, [5, 10])
        self.assertEqual(opening["hp"], 1260)
        self.assertEqual([row["hp"] for row in samples], [1320, 1380])

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

    def test_timed_trait_grants_stop_after_death(self):
        spec = plain_spec(hp=100, incoming_dps=1000)
        spec["traits"] = [trait(RIFTBEAST, 3)]
        _, samples = ENGINE.measure_response(spec, [1, 5, 10])
        self.assertTrue(all(not row["alive"] for row in samples))
        self.assertEqual([row["residualPhysicalEhp"] for row in samples], [0, 0, 0])
        self.assertEqual([row["hp"] for row in samples], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
