"""Level-nine matches include the last actor in every native combat path."""

from copy import deepcopy
import unittest

from test_tft import ENGINE
from test_tft_prepared import bits, prepared
from test_tft_symmetric import caster, events, match
from test_tft_team_engine import ally, flat_calc


def mirror(allies, enemies, side, **options):
    """Give the same designated board the same initiative on either side."""
    return (match(allies, enemies, **options) if side == "ally" else
            match(enemies, allies, initiative=1, **options))


class TestNineChampionMatches(unittest.TestCase):
    def test_ninth_survivor_keeps_fighting_after_first_eight_die(self):
        for side in ("ally", "enemy"):
            with self.subTest(side=side):
                board = [ally(hp=10, priority=i) for i in range(8)]
                board.append(ally(hp=100, ad=40, attack_speed=1, priority=8))
                result = mirror(board, [ally(hp=10000, ad=10, attack_speed=1)],
                                side, duration=8.1)
                units = result["allies" if side == "ally" else "enemies"]
                incoming = events(result, "damage", "enemy" if side == "ally" else "ally", name="auto")
                self.assertEqual([(hit["time"], hit["target"]) for hit in incoming],
                                 [(float(i), i) for i in range(9)])
                self.assertFalse(any(unit["alive"] for unit in units[:8]))
                self.assertTrue(units[8]["alive"])
                self.assertEqual((units[8]["damage"], units[8]["attacks"]), (360.0, 9))
                self.assertEqual((result["outcome"], result["duration"]), ("timeout", 8.1))

    def test_ninth_death_ends_the_match_and_clears_the_frontline(self):
        for side in ("ally", "enemy"):
            with self.subTest(side=side):
                board = [ally(hp=10, priority=i) for i in range(9)]
                result = mirror(board, [ally(hp=10000, ad=10, attack_speed=1)],
                                side, duration=9.1)
                self.assertEqual(result["outcome"], "loss" if side == "ally" else "win")
                self.assertEqual(result["duration"], 8.0)
                self.assertEqual(result["frontlineTime" if side == "ally" else "enemyFrontlineTime"], 8.0)
                units = result["allies" if side == "ally" else "enemies"]
                self.assertFalse(units[8]["alive"])
                self.assertEqual(units[8]["aliveTime"], 8.0)

    def test_ninth_on_death_body_holds_after_every_champion_dies(self):
        board = [ally(hp=10, priority=i) for i in range(8)]
        yorick = deepcopy(ally("Yorick", driver="Yorick", hp=50, priority=8))
        flat_calc(yorick, "HealthCalc2", 200)
        yorick["spec"]["unit"]["extras"]["TFT18_Yorick_Spirit"].update(armor=0.0, mr=0.0)
        board.append(yorick)
        result = match(board, [ally(hp=10000, ad=100, attack_speed=1)], duration=9.1)
        self.assertFalse(any(unit["alive"] for unit in result["allies"]))
        self.assertEqual(result["allies"][8]["aliveTime"], 8.0)
        self.assertEqual(result["allyHpLeft"], 100.0)
        self.assertEqual((result["outcome"], result["duration"], result["frontlineTime"]),
                         ("timeout", 9.1, 9.1))

    def test_burn_from_ninth_source_ticks_on_ninth_recipient(self):
        allies = [ally(frontline=False) for _ in range(8)]
        allies.append(ally(ad=1, fx=[{"burnOnHit": [0.01, 1.0]}]))
        enemies = [ally(frontline=False) for _ in range(8)] + [ally()]
        result = match(allies, enemies, duration=1.1)
        burns = events(result, "damage", source=8, name="burn")
        self.assertEqual([(hit["time"], hit["target"], hit["amount"]) for hit in burns],
                         [(time, 8, 2.5) for time in (0.25, 0.5, 0.75, 1.0)])
        self.assertEqual(result["allies"][8]["damage"], 11.0)
        self.assertEqual(result["enemies"][8]["damageTaken"], 11.0)

    def test_ninth_frontliner_supplies_both_auras_until_its_death(self):
        for side in ("ally", "enemy"):
            with self.subTest(side=side):
                board = [ally(ad=100, attack_speed=1, frontline=False),
                         caster(damage=100, frontline=False)]
                board.extend(ally(frontline=False) for _ in range(6))
                board.append(ally(hp=15, fx=[{"sunderAura": 0.3, "shredAura": 0.3}]))
                target = [ally(hp=10000, ad=10, attack_speed=1, armor=100, mr=100)]
                # The aura holder takes 10 damage at 0 and dies at 1. The
                # enemy acts first, so the second auto sees no aura.
                result = (match(board, target, duration=1.1, initiative=1) if side == "ally"
                          else match(target, board, duration=1.1))
                autos = events(result, "damage", side, source=0, name="auto")
                spells = events(result, "damage", side, source=1, name="ability")
                self.assertEqual([hit["time"] for hit in autos], [0.0, 1.0])
                self.assertAlmostEqual(autos[0]["amount"], 100 / 1.7)
                self.assertAlmostEqual(autos[1]["amount"], 50.0)
                self.assertEqual(len(spells), 1)
                self.assertAlmostEqual(spells[0]["amount"], 100 / 1.7)
                self.assertEqual(result["frontlineTime" if side == "ally" else "enemyFrontlineTime"], 1.0)

    def test_ninth_actor_can_receive_or_supply_single_recipient_healing(self):
        for recipient in (0, 8):
            donor = 8 - recipient
            with self.subTest(recipient=recipient):
                board = [ally(frontline=False) for _ in range(9)]
                board[recipient] = ally()
                board[donor] = ally(ad=100, frontline=False, fx=[{"allyHealPct": 0.2}])
                result = match(board, [ally(ad=100)], duration=0.1, initiative=1)
                healing = events(result, "allyHeal", source=donor)
                self.assertEqual([(hit["target"], hit["amount"]) for hit in healing], [(recipient, 20.0)])
                self.assertEqual(result["allies"][recipient]["healing"], 20.0)
                self.assertEqual(result["allies"][donor]["allyHealing"], 20.0)

    def test_ninth_attacker_counts_toward_recipient_armor_and_magic_resist(self):
        target = ally(hp=10000, fx=[{"resistsPerAttacker": [10.0, 20.0]}])
        enemies = [ally(ad=100) for _ in range(8)]
        enemies.append(caster(damage=100, ad=100))
        result = match([target], enemies, duration=0.3)
        autos = events(result, "damage", "enemy", name="auto")
        self.assertEqual(len(autos), 9)
        for hit in autos:
            self.assertAlmostEqual(hit["amount"], 100 / 1.9)
        spell = events(result, "damage", "enemy", source=8, name="ability")
        self.assertEqual(len(spell), 1)
        self.assertAlmostEqual(spell[0]["amount"], 100 / 2.8)

    def test_area_selection_and_flat_resistance_changes_include_ninth_target(self):
        gnar = ally("Gnar", driver="Gnar", attack_speed=1)
        for calc, value in (("HealthCalc1", 0), ("GenericCalc1", 20), ("PhysicalDamageCalc3", 0)):
            flat_calc(gnar, calc, value)
        gnar["spec"]["kits"]["base"]["rows"].update(
            RagePerAttack=1.0, TransformRageMax=1.0, RagePerSecond=0.0, StunDuration=0.0)
        # Gnar's transform selects all nine and publishes its flat cuts.
        # A different source attacking later must see target 8's shared cut.
        attacker = ally(ad=100, attack_speed=1, lane=6)
        enemies = [ally(hp=10000, armor=100, mr=100, lane=0) for _ in range(8)]
        enemies.append(ally(hp=10000, armor=100, mr=100, lane=6))
        result = match([gnar, attacker], enemies, duration=0.1)
        hits = events(result, "damage", source=1, name="auto")
        self.assertEqual([hit["target"] for hit in hits], [8])
        self.assertAlmostEqual(hits[0]["amount"], 100 / 1.8)

    def test_nine_vs_nine_initiative_and_all_native_api_shapes(self):
        board = [ally(hp=100, ad=100, attack_speed=1, priority=i) for i in range(9)]
        raw = [{"allies": board, "enemies": deepcopy(board), "duration": 1.1,
                "initiative": initiative, "geometry": geometry}
               for geometry in ("spread", "clump") for initiative in (0, 1)]
        expected = [ENGINE.simulate_match(spec, True) for spec in raw]
        self.assertEqual([result["outcome"] for result in expected], ["win", "loss"] * 2)
        for result in expected:
            winner = "ally" if result["initiative"] == 0 else "enemy"
            self.assertEqual(result["duration"], 0.0)
            self.assertEqual([hit["source"] for hit in events(result, "damage", winner, name="auto")],
                             list(range(9)))
        native = [prepared(spec) for spec in raw]
        for specs in (raw, native):
            for workers in (1, 4):
                self.assertEqual(bits(expected), bits(ENGINE.simulate_matches(specs, trace=True, workers=workers)))
                compact = ENGINE.simulate_matches(specs, detail="compact", workers=workers)
                for complete, small in zip(expected, compact):
                    self.assertEqual(bits({key: complete[key] for key in small}), bits(small))
        # Reusing the exact same prepared handles must not retain ninth-unit
        # deaths or targeting masks from the preceding worlds.
        self.assertEqual(bits(expected[::-1]), bits(ENGINE.simulate_matches(native[::-1], trace=True)))

    def test_ten_actors_are_rejected_on_both_sides_for_raw_and_prepared(self):
        spec = {"allies": [ally()], "enemies": [ally()], "duration": 0.1}
        for source in (spec, prepared(spec)):
            for side in ("allies", "enemies"):
                invalid = {**source, side: source[side] * 10}
                with self.subTest(side=side, prepared=source is not spec):
                    with self.assertRaisesRegex(ValueError, "one to nine"):
                        ENGINE.simulate_match(invalid)
                    with self.assertRaisesRegex(ValueError, "one to nine"):
                        ENGINE.simulate_matches([source, invalid])
        # Expanding champion encounters leaves the standalone dummy contract
        # unchanged; its nine-target input still fails at the existing guard.
        solo = deepcopy(spec["allies"][0]["spec"])
        solo["dummies"] = {"slots": [{"hp": 1000, "armor": 0, "mr": 0}] * 9}
        with self.assertRaises(ValueError):
            ENGINE.simulate(solo)


if __name__ == "__main__":
    unittest.main()
