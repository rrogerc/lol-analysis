"""Azir/Protector's Vow regressions for the isolated carry benchmark.

These check item accounting and the adopted six-command mana lock. They
validate that model; they do not independently establish live-game timing.
"""

import copy
import unittest

import tft
from test_tft import DUMMY, ENGINE, SNAP, events, immortal, spec_for


class TestAzirProtectorsVow(unittest.TestCase):
    def spec(self, *, duration=None, copies=1):
        contexts, _ = tft.unit_trait_contexts(
            SNAP, SNAP.unit("Azir"), tft.load_trait_effects(SNAP.set_no))
        return spec_for(
            "Azir", star=2, geometry="spread", traits=contexts["low"],
            items=["Protector's Vow"] * copies
                  + ["Rabadon's Deathcap"] * (3 - copies),
            duration=duration, dummy=tft.dummies_for(SNAP))

    def test_vow_grants_twenty_starting_mana_once(self):
        spec = self.spec(duration=0.01)
        sheet, res = ENGINE.simulate(spec, True)
        self.assertEqual(sheet["manaStart"], 20.0)
        self.assertEqual(sheet["manaMax"], 35.0)
        self.assertEqual(res["casts"], 0)
        self.assertEqual(res["attacks"], 1)
        # One opening attack grants ten: neither the 20 starting mana nor
        # the low-health 15-mana proc has been counted a second time.
        self.assertEqual(res["probe"]["mana"], 30.0)

    def test_two_vows_cap_opening_mana_at_azirs_bar(self):
        _, res = ENGINE.simulate(self.spec(duration=0.01, copies=2), True)
        casts = events(res, "cast")
        self.assertEqual(len(casts), 1)
        self.assertEqual(casts[0][0], 0.0)
        # The engine's opening attack runs before its cast check. Starting
        # mana is capped at 35, then that attack adds ten (45, not 50).
        self.assertEqual(res["attacks"], 1)
        self.assertEqual(casts[0][2], 45.0)
        self.assertEqual(res["probe"]["mana"], 10.0)

    def test_defenses_and_health_procs_do_not_improve_unattacked_carry(self):
        spec = self.spec()
        self.assertFalse(spec["pressure"])
        _, expected = ENGINE.simulate(spec, True)
        without_defense = copy.deepcopy(spec)
        vow = without_defense["items"][0]
        self.assertEqual(vow.pop("manaAtHp"), [0.4, 15.0])
        self.assertEqual(vow.pop("shieldAtHp")[:2], [0.4, 0.2])
        vow["stats"] = [(key, value) for key, value in vow["stats"]
                        if key not in ("armor", "mr")]
        _, actual = ENGINE.simulate(without_defense, True)
        self.assertEqual(actual, expected)
        self.assertEqual(expected["taken"], 0.0)
        self.assertEqual(expected["shielded"], 0.0)
        self.assertEqual(expected["probe"]["shieldsActive"], 0)

    def test_starting_mana_advances_first_cast_by_two_attacks(self):
        spec = self.spec(duration=4.01)
        _, with_vow = ENGINE.simulate(spec, True)
        without_start = copy.deepcopy(spec)
        self.assertEqual(without_start["items"][0].pop("startingMana"), 20.0)
        _, without_vow_start = ENGINE.simulate(without_start, True)
        opening_with = events(with_vow, "cast")[0][0]
        opening_without = events(without_vow_start, "cast")[0][0]
        # Both retain Vow's regeneration and every other stat. With 0.75
        # base AS, the second and fourth ordinary attacks start the casts.
        self.assertAlmostEqual(opening_with, 1.0 / 0.75)
        self.assertAlmostEqual(opening_without, 3.0 / 0.75)
        for result, first_cast, expected_count in (
                (with_vow, opening_with, 2),
                (without_vow_start, opening_without, 4)):
            self.assertEqual(len([e for e in events(result, "attack")
                                  if e[0] <= first_cast]), expected_count)


class TestAzirManaLock(unittest.TestCase):
    def spec(self, *, duration, attack_speed=.8, regen=0.0, items=(), traits=()):
        spec = spec_for("Azir", items=items, traits=traits, duration=duration,
                        pressure=False, dummy=immortal(DUMMY))
        spec["kits"]["base"]["stats"].update(initialMana=35.0)
        spec["kits"]["base"]["stats"]["as"] = attack_speed
        if regen:
            spec["role"]["manaRegen"] = regen
        return spec

    def test_all_six_commands_block_attack_mana(self):
        # Opening attack fills a 35-mana bar and leaves 10 overflow.
        # Base AS .8 gives the first command at 1.25s, then five more
        # every .5s. The sixth command at 3.75s must grant no mana either.
        for duration, count in ((3.51, 5), (3.76, 6)):
            with self.subTest(duration=duration):
                _, result = ENGINE.simulate(self.spec(duration=duration), True)
                self.assertEqual(result["casts"], 1)
                self.assertEqual(len(events(result, "damage", "soldiers")), count)
                self.assertEqual(result["probe"]["mana"], 10.0)
        # The first ordinary attack after the window grants mana again.
        _, result = ENGINE.simulate(self.spec(duration=5.01), True)
        self.assertEqual(len(events(result, "damage", "soldiers")), 6)
        self.assertEqual(result["probe"]["mana"], 20.0)

    def test_regeneration_is_blocked_and_resumes_after_sixth_command(self):
        _, locked = ENGINE.simulate(self.spec(duration=3.51, regen=4.0), True)
        self.assertEqual(locked["casts"], 1)
        self.assertEqual(locked["probe"]["mana"], 10.0)
        _, released = ENGINE.simulate(self.spec(duration=4.01, regen=4.0), True)
        self.assertEqual(len(events(released, "damage", "soldiers")), 6)
        self.assertEqual(released["probe"]["lockUntil"], 3.75)
        # Only .25s is outside the lock. There is no extra second added
        # after the sixth command and no regeneration for time inside it.
        self.assertEqual(released["probe"]["mana"], 11.0)

    def test_partial_tick_only_regenerates_after_unlock(self):
        _, result = ENGINE.simulate(
            self.spec(duration=3.51, attack_speed=.9, regen=4.0), True)
        commands = events(result, "damage", "soldiers")
        self.assertEqual(len(commands), 6)
        end = commands[-1][0]
        self.assertAlmostEqual(end, 10 / 3)
        self.assertAlmostEqual(result["probe"]["lockUntil"], end)
        self.assertAlmostEqual(result["probe"]["mana"], 10 + 4 * (3.5 - end))

    def test_mana_items_cannot_refresh_an_unfinished_command_window(self):
        contexts, _ = tft.unit_trait_contexts(
            SNAP, SNAP.unit("Azir"), tft.load_trait_effects(SNAP.set_no))
        builds = (
            ("Blue Buff",) * 3,
            ("Spear of Shojin",) * 3,
            ("Nashor's Tooth",) * 3,
            ("Adaptive Helm", "Blue Buff", "Nashor's Tooth"),
            ("Protector's Vow", "Rabadon's Deathcap", "Rabadon's Deathcap"),
        )
        for items in builds:
            for traits in ([], contexts["high"]):
                with self.subTest(items=items, traits=traits):
                    _, result = ENGINE.simulate(
                        self.spec(duration=20, items=items, traits=traits), True)
                    casts = result["castTimes"]
                    self.assertGreaterEqual(len(casts), 2)
                    commands = events(result, "damage", "soldiers")
                    for start, following in zip(casts, casts[1:]):
                        window = [event for event in commands if start < event[0] <= following]
                        self.assertEqual(len(window), 6)

    def test_item_attack_mana_is_blocked_with_the_base_attack_mana(self):
        spec = self.spec(duration=3.0,
                         items=("Spear of Shojin", "Spear of Shojin", "Nashor's Tooth"))
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual(result["casts"], 1)
        self.assertGreaterEqual(len(events(result, "damage", "soldiers")), 4)
        overflow = events(result, "cast")[0][2] - 35
        self.assertAlmostEqual(result["probe"]["mana"], overflow)

    def test_target_death_does_not_end_the_command_lock(self):
        spec = self.spec(duration=3.76, regen=4.0)
        spec["dummies"]["slots"][0]["hp"] = 50.0
        _, result = ENGINE.simulate(spec, True)
        commands = events(result, "damage", "soldiers")
        self.assertEqual(len(commands), 6)
        self.assertEqual(commands[0][3], 0)
        self.assertEqual([event[3] for event in commands[1:]], [1] * 5)
        self.assertEqual(result["casts"], 1)
        self.assertEqual(result["probe"]["mana"], 10.0)


if __name__ == "__main__":
    unittest.main()
