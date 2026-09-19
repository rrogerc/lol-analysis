"""Hand-computed timing checks for the shared melee engagement estimate."""
from copy import deepcopy
import math
import unittest

import tft
from test_tft import ENGINE, SNAP, events, spec_for
from test_tft_unit_profiles import plain_spec


def walker(*, delay=0.5, speed=1.0, range_=1, healths=(100, 100),
           damage=100, hp=10000, incoming=0, duration=6, fx=()):
    spec = plain_spec(hp=hp, damage=damage, incoming_dps=incoming,
                      pressure_interval=0.25, fx=fx)
    spec.update(immortal=False, duration=duration, meleeRepositionSeconds=delay)
    spec["unit"].update(kind="Fighter", range=range_)
    stats = spec["kits"]["base"]["stats"]
    stats.update(range=range_, mana=10**9, initialMana=0)
    stats["as"] = speed
    source = spec["dummies"]["slots"][0]
    spec["dummies"]["slots"] = [dict(source, hp=value) for value in healths]
    return spec


class TestMovementClock(unittest.TestCase):
    def simulate(self, **kwargs):
        return ENGINE.simulate(walker(**kwargs), True)[1]

    def test_first_engagement_and_attack_cooldown_overlap(self):
        result = self.simulate()
        self.assertEqual([row[0] for row in events(result, "attack")], [0.5, 1.5])
        self.assertEqual(result["killTime"], 1.5)
        # One second physically moving is not one second of extra attack delay:
        # the second half-second overlaps the previous attack's cooldown.
        self.assertEqual(result["movementTime"], 1.0)
        self.assertEqual(result["repositions"], 2)
        self.assertEqual(result["dps"], 200 / 1.5)

    def test_fast_attack_cannot_erase_the_target_change_deadline(self):
        result = self.simulate(speed=5, healths=(100, 100, 100))
        self.assertEqual([row[0] for row in events(result, "attack")], [0.5, 1.0, 1.5])
        self.assertEqual(result["killTime"], 1.5)
        self.assertEqual(result["movementTime"], 1.5)
        self.assertEqual(result["repositions"], 3)

    def test_no_recurring_delay_while_fighting_the_same_target(self):
        result = self.simulate(speed=5, healths=(10000,), duration=1.11)
        self.assertEqual([round(row[0], 9) for row in events(result, "attack")],
                         [0.5, 0.7, 0.9, 1.1])
        self.assertEqual(result["repositions"], 1)
        self.assertEqual(result["movementTime"], 0.5)

    def test_ranged_output_is_exactly_unchanged(self):
        moving = self.simulate(range_=4, speed=5)
        stationary = self.simulate(range_=4, speed=5, delay=0)
        self.assertEqual(moving, stationary)
        self.assertNotIn("movementTime", moving)

    def test_both_short_ranges_use_the_shared_estimate(self):
        for range_ in (1, 2):
            with self.subTest(range=range_):
                result = self.simulate(range_=range_)
                self.assertEqual(events(result, "attack")[0][0], 0.5)

    def test_omitted_delay_matches_explicit_zero(self):
        spec = walker(delay=0)
        explicit = ENGINE.simulate(spec, True)
        del spec["meleeRepositionSeconds"]
        self.assertEqual(ENGINE.simulate(spec, True), explicit)

    def test_no_attack_mana_is_earned_during_approach(self):
        result = self.simulate(delay=1, duration=0.75)
        self.assertEqual(result["attacks"], 0)
        self.assertEqual(result["casts"], 0)
        self.assertEqual(result["probe"]["mana"], 0)
        self.assertEqual(result["movementTime"], 0.75)

    def test_pressure_can_kill_the_actor_during_approach(self):
        result = self.simulate(delay=1, hp=100, incoming=400, healths=(10000,))
        self.assertTrue(result["died"])
        self.assertEqual(result["diedAt"], 0.25)
        self.assertEqual(result["taken"], 100)
        self.assertEqual(result["movementTime"], 0.25)
        self.assertEqual(result["attacks"], 0)
        self.assertEqual(result["denied"], 0)

    def test_passive_regeneration_continues_during_approach(self):
        spec = walker(delay=1, duration=0.75)
        spec["role"]["manaRegen"] = 4
        result = ENGINE.simulate(spec, True)[1]
        self.assertEqual(result["attacks"], 0)
        self.assertEqual(result["probe"]["mana"], 3)

    def test_released_damage_over_time_continues_between_targets(self):
        # Give a known trail spell a short-range actor solely to isolate the
        # clock. Its opening arrow kills target0; the released trail keeps
        # damaging the surviving targets during the ensuing movement.
        spec = spec_for("Ashe", duration=2, pressure=False)
        spec["unit"]["range"] = 1
        spec["meleeRepositionSeconds"] = 0.5
        spec["kits"]["base"]["stats"].update(mana=1000, initialMana=1000, range=1)
        for slot, health in zip(spec["dummies"]["slots"], (50, 1000000, 1000000)):
            slot.update(hp=health, armor=0, mr=0)
        result = ENGINE.simulate(spec, True)[1]
        self.assertEqual(events(result, "kill", target=0)[0][0], 0.75)
        self.assertTrue(any(0.75 < row[0] < 1.25
                            for row in events(result, "damage", src="trail")))
        self.assertFalse(any(0.75 <= row[0] < 1.25 for row in events(result, "attack")))

    def test_a_full_bar_can_cast_at_a_non_tick_arrival(self):
        spec = walker(delay=0.37, healths=(10000,))
        spec["kits"]["base"]["stats"].update(mana=100, initialMana=100)
        result = ENGINE.simulate(spec, True)[1]
        self.assertEqual(result["castTimes"][0], 0.37)
        self.assertEqual(events(result, "attack")[0][0], 0.62)

    def test_immortal_measurement_does_not_invent_movement(self):
        spec = walker(healths=(10000,))
        spec["immortal"] = True
        first = ENGINE.simulate(spec, True)
        spec["meleeRepositionSeconds"] = 0
        self.assertEqual(ENGINE.simulate(spec, True), first)

    def test_invalid_delay_fails_before_simulation(self):
        for delay in (-0.1, math.inf, -math.inf, math.nan, True, False):
            with self.subTest(delay=delay), self.assertRaisesRegex(ValueError, "meleeRepositionSeconds"):
                ENGINE.simulate(walker(delay=delay), False)


class TestMovementInputs(unittest.TestCase):
    def test_finite_benchmark_declares_half_second_and_tank_presets_do_not(self):
        dummy = tft.dummies_for(SNAP)
        self.assertEqual(dummy["meleeRepositionSeconds"], 0.5)
        spec = tft.cell_spec(SNAP, SNAP.unit("Camille"), 2, "clump", [], dummy)
        self.assertEqual(spec["meleeRepositionSeconds"], 0.5)
        for threat in tft.TANK_THREATS:
            self.assertNotIn("meleeRepositionSeconds", tft.dummies_for(SNAP, threat=threat))

    def test_nidalee_uses_equipped_range_for_the_delay(self):
        for items, form, range_ in ((["Deathblade"] * 3, "AD", 1),
                                    (["Rabadon's Deathcap"] * 3, "AP", 5)):
            spec = spec_for("Nidalee", items=items, duration=4, pressure=False)
            spec["meleeRepositionSeconds"] = 0.5
            opening, result = ENGINE.simulate(spec, True)
            self.assertEqual((opening["form"], opening["range"]), (form, range_))
            control = deepcopy(spec)
            control["meleeRepositionSeconds"] = 0
            stationary = ENGINE.simulate(control, True)[1]
            if form == "AP":
                self.assertEqual(result, stationary)
            else:
                self.assertEqual(events(result, "attack")[0][0], 0.5)
                self.assertGreater(result["movementTime"], 0)


if __name__ == "__main__":
    unittest.main()
