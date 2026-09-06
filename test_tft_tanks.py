"""Tank benchmark regressions, with explicit pressure for hand-computed fights.

Run after rebuilding the engine: python3 -m unittest test_tft_tanks -v
"""

import copy
import unittest

import tft
from test_tft import DUMMY, ENGINE, SNAP, events, one_hitter, spec_for


def body_spec(*, hp=1000.0, armor=0.0, mr=0.0, pre=100.0, duration=1.5,
              debuffs=None, fx=(), dummy=None, unit="Leona", driver="Driver"):
    """A known health pool, with no ability unless a test requests a driver."""
    spec = copy.deepcopy(spec_for(unit, star=1, driver=driver, duration=duration,
                                  dummy=dummy or one_hitter(pre), fx=fx))
    kit = spec["kits"]["base"]
    kit["hpStar"] = hp
    kit["stats"].update(hp=hp, armor=armor, mr=mr)
    spec["enemyDebuffs"] = debuffs or {}
    return spec


def scheduled_source(*, ability=100.0, physical_share=0.0, start=0.25, interval=1.0):
    """One spell-only enemy, with a full mana bar to catch accidental mana casts."""
    dummy = one_hitter(0.0)
    dummy["slots"][0].update(ability=ability, physicalShare=physical_share,
                              castStart=start, castInterval=interval,
                              manaMax=1.0, manaStart=1.0, manaPerAttack=1.0,
                              manaFromDamage=True)
    return dummy


def formation_sources(*, pre=100.0, ability=100.0, start=0.5, interval=1.0):
    """Three nearby tanks followed by two protected damage sources."""
    dummy = one_hitter(0.0)
    slots = [dict(dummy["slots"][0], kind="tank" if i < 3 else "non-tank",
                  nearby=i < 3, ad=pre, ability=ability, physicalShare=0.0,
                  attackStart=start, castStart=start, castInterval=interval)
             for i in range(5)]
    for slot in slots:
        slot["as"] = 1.0 / interval
    return dict(dummy, slots=slots, count=5, board=[1] * 5, boardSize=5)


def opening_cast(spec):
    """A full, large mana bar permits exactly one opening cast in short fights."""
    spec["kits"]["base"]["stats"].update(initialMana=10 ** 6, mana=10 ** 6)
    return spec


class TestTankThreats(unittest.TestCase):
    def test_default_debuffs_and_explicit_override(self):
        expected = {"wound": 0.33, "sunder": 0.30, "shred": 0.30}
        self.assertEqual(tft.tank_debuffs(SNAP), expected)
        self.assertEqual(spec_for("Leona")["enemyDebuffs"], expected)
        clean = spec_for("Leona", dummy=dict(DUMMY, enemyDebuffs={}))
        self.assertEqual(clean["enemyDebuffs"], {})
        custom = {"wound": 0.5, "shred": 0.1}
        self.assertEqual(spec_for("Leona", dummy=dict(DUMMY, enemyDebuffs=custom))
                         ["enemyDebuffs"], custom)
        for unit in ("Ashe", "Warwick"):
            with self.subTest(unit=unit):
                self.assertFalse(spec_for(unit).get("enemyDebuffs"))

    def test_presets_share_five_sources_and_a_calibrated_damage_budget(self):
        legacy_dps = tft.dummies_for(SNAP)["boardPressureDps"]
        profiles = {p["key"]: p for p in tft.tank_threats(SNAP)}
        self.assertEqual(set(profiles), {"mixed", "physical", "magic"})
        baseline_dps = profiles["mixed"]["dps"]
        self.assertGreater(baseline_dps, legacy_dps)
        shares = {"mixed": (0.5, 0.0), "physical": (0.85, 1.0), "magic": (0.15, 0.0)}
        frontline = None
        for key, (attack_share, spell_physical_share) in shares.items():
            with self.subTest(threat=key):
                dummy = tft.dummies_for(SNAP, threat=key)
                slots = dummy["slots"]
                self.assertEqual(len(slots), 5)
                self.assertEqual([s["nearby"] for s in slots], [True] * 3 + [False] * 2)
                self.assertEqual([s["kind"] for s in slots], ["tank"] * 3 + ["non-tank"] * 2)
                self.assertEqual(dummy["board"], [1] * 5)
                profile = profiles[key]
                front_dps = sum(s["ad"] * dummy["critEv"] * s["as"]
                                + s["ability"] / s["castInterval"] for s in slots[:3])
                attack_dps = sum(s["ad"] * dummy["critEv"] * s["as"] for s in slots[3:])
                spell_dps = sum(s["ability"] / s["castInterval"] for s in slots[3:])
                self.assertAlmostEqual(front_dps + attack_dps + spell_dps, baseline_dps)
                self.assertAlmostEqual(front_dps, profile["frontlineDps"])
                self.assertAlmostEqual(attack_dps + spell_dps, profile["backlineDps"])
                self.assertAlmostEqual(attack_dps / profile["backlineDps"], attack_share)
                self.assertTrue(all(s["physicalShare"] == spell_physical_share for s in slots[3:]))
                self.assertEqual(profile["dps"], baseline_dps)
                self.assertEqual(profile["attackers"], 5)
                # Presets change the carry damage mix, keeping the same frontline.
                if frontline is None:
                    frontline = slots[:3]
                self.assertEqual(slots[:3], frontline)
                spec = spec_for("Leona", dummy=dummy)
                self.assertEqual([s["streams"] for s in spec["dummies"]["slots"]], [1] * 5)

    def test_reference_carries_reproduce_the_backline_budget(self):
        profile = tft.tank_threats(SNAP)[0]
        refs = profile["referenceCarries"]
        self.assertEqual({r["name"] for r in refs}, {"Aphelios", "Ahri"})
        expected_items = {
            "Aphelios": ["Guinsoo's Rageblade", "Kraken's Fury", "Infinity Edge"],
            "Ahri": ["Spear of Shojin", "Jeweled Gauntlet", "Archangel's Staff"],
        }
        for ref in refs:
            with self.subTest(carry=ref["name"]):
                self.assertEqual(ref["star"], 2)
                self.assertEqual(ref["duration"], 20.0)
                self.assertEqual(ref["api"], SNAP.unit(ref["name"])["api"])
                self.assertEqual(ref["items"], expected_items[ref["name"]])
                self.assertEqual(ref["itemApis"], [SNAP.item(n)["api"] for n in ref["items"]])
                target = one_hitter(0.0)
                target["slots"] = [dict(target["slots"][0], hp=3000.0, armor=0.0, mr=0.0)]
                target["board"] = None
                spec = spec_for(ref["name"], star=2, items=ref["items"],
                                duration=20.0, geometry="spread", dummy=target, pressure=False)
                spec["immortal"] = True
                _, res = ENGINE.simulate(spec, False)
                self.assertAlmostEqual(ref["dps"], res["total"] / 20.0)
        self.assertAlmostEqual(sum(r["dps"] for r in refs), profile["backlineDps"])

    def test_magic_backline_bursts_together_while_mixed_spells_are_staggered(self):
        for key, expected in (("magic", [4.0, 4.0]), ("mixed", [2.0, 4.0])):
            with self.subTest(threat=key):
                spec = body_spec(hp=10 ** 9, duration=4.1,
                                 dummy=tft.dummies_for(SNAP, threat=key))
                _, res = ENGINE.simulate(spec, True)
                casts = [e for e in events(res, "take", "magic") if e[3] >= 3]
                self.assertEqual(len(casts), 2)
                for event, time in zip(casts, expected):
                    self.assertAlmostEqual(event[0], time)

    def test_only_tanks_get_additional_threat_scenarios(self):
        tank = tft.unit_scenarios(SNAP.unit("Leona"))
        bases = {key for key in tank
                 if not key.endswith(("-physical", "-magic"))}
        self.assertEqual(len(tank), 3 * len(bases))
        for key in bases:
            self.assertIn(key + "-physical", tank)
            self.assertIn(key + "-magic", tank)
        for name in ("Ashe", "Warwick"):
            with self.subTest(unit=name):
                self.assertFalse(any(key.endswith(("-physical", "-magic"))
                                     for key in tft.unit_scenarios(SNAP.unit(name))))


class TestEnemyDebuffs(unittest.TestCase):
    def test_wound_reduces_healing_before_the_missing_health_cap(self):
        # A 200 heal becomes 134. It still fully repairs 100 missing HP;
        # applying Wound after the missing-health cap would only restore 67.
        for damage, expected in ((100.0, 100.0), (400.0, 134.0)):
            with self.subTest(damage=damage):
                dummy = one_hitter(damage, period=10.0)
                dummy["slots"][0]["attackStart"] = 0.25
                spec = body_spec(dummy=dummy, duration=1.1, debuffs={"wound": 0.33},
                                 fx=[{"healPerInterval": [0.2, 1.0]}])
                _, res = ENGINE.simulate(spec, True)
                heals = events(res, "heal", "dragon's claw")
                self.assertEqual(len(heals), 1)
                self.assertAlmostEqual(heals[0][2], expected)
                self.assertAlmostEqual(res["hpLeft"], 1000 - damage + expected)

    def test_wound_does_not_reduce_shields(self):
        spec = body_spec(pre=300.0, debuffs={"wound": 1.0},
                         fx=[{"shieldAtStart": [0.2, 5.0]}])
        sheet, res = ENGINE.simulate(spec, True)
        self.assertEqual(res["shielded"], 200.0)
        self.assertEqual(res["taken"], 300.0)
        self.assertEqual(res["hpLeft"], 900.0)
        self.assertEqual(sheet["physicalEhp"], 1000.0)
        self.assertEqual(sheet["magicEhp"], 1000.0)

    def test_wound_does_not_reduce_max_health_grants(self):
        dummy = one_hitter(100.0, period=10.0)
        dummy["slots"][0]["attackStart"] = 0.1
        spec = body_spec(unit="Krug", driver="Krug", dummy=dummy, duration=0.4,
                         debuffs={"wound": 1.0})
        kit = spec["kits"]["base"]
        kit["stats"].update(initialMana=100.0, mana=100.0)
        kit["calcs"]["HealthCalc2"]["terms"] = [{"type": "flat", "value": 200.0, "op": "add"}]
        _, res = ENGINE.simulate(spec, True)
        self.assertEqual(res["casts"], 1)
        self.assertEqual(res["probe"]["maxHp"], 1200.0)
        self.assertEqual(res["hpLeft"], 1100.0)
        self.assertEqual(res["healed"], 0.0)

    def test_immortal_targets_preserve_omnivamp_and_wound(self):
        spec = body_spec(pre=200.0, duration=3.1, debuffs={"wound": 0.33},
                         fx=[{"stats": [["omnivamp", 0.5]], "allyHealPct": 0.2}])
        _, immortal = ENGINE.simulate(spec, True)
        spec["immortal"] = False
        _, mortal = ENGINE.simulate(spec, True)
        heals = events(immortal, "heal", "omnivamp")
        self.assertGreater(len(heals), 0)
        self.assertEqual(heals, events(mortal, "heal", "omnivamp"))
        self.assertAlmostEqual(immortal["healed"], mortal["healed"])
        self.assertAlmostEqual(immortal["allyHeal"], mortal["allyHeal"])
        self.assertAlmostEqual(immortal["allyHeal"], immortal["total"] * 0.2)
        for i, event in enumerate(immortal["trace"]):
            if event[1] == "heal" and event[4] == "omnivamp":
                damage = next(e for e in reversed(immortal["trace"][:i])
                              if e[1] == "damage")
                self.assertAlmostEqual(event[2], damage[2] * 0.5 * 0.67)

    def test_sunder_and_shred_apply_to_the_corresponding_damage_type(self):
        for physical, expected in ((True, 100.0 / 1.7 * 0.8),
                                    (False, 100.0 / 2.5 * 0.8)):
            with self.subTest(physical=physical):
                dummy = one_hitter(100.0) if physical else scheduled_source(interval=10.0)
                spec = body_spec(armor=100.0, mr=200.0, dummy=dummy,
                                 debuffs={"sunder": 0.3, "shred": 0.25},
                                 fx=[{"durability": 0.2}])
                sheet, res = ENGINE.simulate(spec, True)
                hits = events(res, "take")
                self.assertEqual(len(hits), 1)
                self.assertAlmostEqual(hits[0][2], expected)
                self.assertAlmostEqual(sheet["physicalEhp"], 2125.0)
                self.assertAlmostEqual(sheet["magicEhp"], 3125.0)

    def test_resistance_reduction_includes_temporary_and_gargoyle_resists(self):
        dummy = one_hitter(100.0, attackers=3)
        for slot in dummy["slots"]:
            slot["attackStart"] = 0.5
        spec = body_spec(armor=100.0, mr=200.0, dummy=dummy, duration=1.6,
                         debuffs={"sunder": 0.3, "shred": 0.3},
                         fx=[{"resistsAtStart": [50.0, 80.0, 1.0]},
                             {"resistsPerAttacker": [10.0, 5.0]}])
        sheet, res = ENGINE.simulate(spec, True)
        self.assertAlmostEqual(sheet["armor"], (100 + 50 + 3 * 10) * 0.7)
        self.assertAlmostEqual(sheet["mr"], (200 + 80 + 3 * 5) * 0.7)
        hits = events(res, "take")
        self.assertEqual(len(hits), 6)
        for hit in hits[:3]:
            self.assertAlmostEqual(hit[2], 100.0 / 2.26)
        for hit in hits[3:]:
            self.assertAlmostEqual(hit[2], 100.0 / 1.91)

    def test_debuffs_are_inert_without_incoming_pressure(self):
        clean = body_spec(armor=100.0, mr=200.0)
        clean["pressure"] = False
        debuffed = copy.deepcopy(clean)
        debuffed["enemyDebuffs"] = {"wound": 1.0, "sunder": 1.0, "shred": 1.0}
        self.assertEqual(ENGINE.simulate(clean, True), ENGINE.simulate(debuffed, True))

    def test_debuff_fractions_reject_invalid_values(self):
        for key in ("wound", "sunder", "shred"):
            for value in (-0.01, 1.01, float("nan"), float("inf")):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        ENGINE.simulate(body_spec(debuffs={key: value}), False)


class TestOpeningEhp(unittest.TestCase):
    def test_opening_includes_defensive_driver_initialization(self):
        # Leona starts with 60 extra armor/MR at 100 AP, then those decay.
        spec = body_spec(armor=100.0, mr=200.0, driver="Leona", duration=0.1,
                         debuffs={"sunder": 0.3, "shred": 0.3},
                         fx=[{"resistsAtStart": [20.0, 30.0, 5.0]},
                             {"resistsPerAttacker": [10.0, 5.0]}])
        sheet, _ = ENGINE.simulate(spec, False)
        self.assertAlmostEqual(sheet["armor"], (100 + 60 + 20 + 10) * 0.7)
        self.assertAlmostEqual(sheet["mr"], (200 + 60 + 30 + 5) * 0.7)
        self.assertAlmostEqual(sheet["physicalEhp"], 2330.0)
        self.assertAlmostEqual(sheet["magicEhp"], 3065.0)

    def test_opening_uses_full_health_durability_and_excludes_attack_only_reduction(self):
        spec = body_spec(armor=100.0, mr=100.0,
                         fx=[{"durability": 0.2}, {"durabilityByHealth": [0.05, 0.15, 0.5]},
                             {"attackDamageTaken": 0.5}, {"healPerInterval": [0.1, 1.0]}])
        sheet, _ = ENGINE.simulate(spec, False)
        self.assertAlmostEqual(sheet["durability"], 1 - 0.8 * 0.85)
        self.assertAlmostEqual(sheet["physicalEhp"], 2000.0 / (0.8 * 0.85))
        self.assertAlmostEqual(sheet["magicEhp"], 2000.0 / (0.8 * 0.85))


class TestTankSurvivalCap(unittest.TestCase):
    def test_capped_build_gets_a_double_pressure_stress_fight(self):
        spec = body_spec(duration=6.5)
        _, res = ENGINE.simulate(spec, True)
        self.assertTrue(res["survivalCapped"])
        self.assertEqual(res["aliveTime"], 6.5)
        self.assertFalse(res["died"])
        self.assertEqual(res["hpLeft"], 400.0)
        self.assertEqual(res["stressAliveTime"], 5.0)
        self.assertFalse(res["stressCapped"])
        # The displayed trace and damage totals still describe normal pressure.
        self.assertEqual(len(events(res, "take")), 6)
        self.assertEqual(res["taken"], 600.0)

    def test_stress_pressure_doubles_scheduled_spells_too(self):
        spec = body_spec(duration=6.5,
                         dummy=scheduled_source(start=1.0, interval=1.0))
        _, res = ENGINE.simulate(spec, False)
        self.assertTrue(res["survivalCapped"])
        self.assertEqual(res["hpLeft"], 400.0)
        self.assertEqual(res["stressAliveTime"], 5.0)
        self.assertFalse(res["stressCapped"])

    def test_a_death_at_the_limit_is_measured_not_capped(self):
        _, res = ENGINE.simulate(body_spec(pre=200.0, duration=5.0), False)
        self.assertTrue(res["died"])
        self.assertEqual(res["aliveTime"], 5.0)
        self.assertFalse(res["survivalCapped"])
        self.assertIsNone(res["stressAliveTime"])

    def test_a_fighter_clearing_early_is_not_survival_capped(self):
        spec = body_spec(unit="Warwick", duration=6.5)
        for slot in spec["dummies"]["slots"]:
            slot["hp"] = 1.0
        _, res = ENGINE.simulate(spec, False)
        self.assertIsNotNone(res["killTime"])
        self.assertLess(res["t"], 6.5)
        self.assertFalse(res["survivalCapped"])
        self.assertIsNone(res["stressAliveTime"])

    def test_surviving_death_body_counts_as_capped_and_inherits_resist_debuffs(self):
        for debuff, dummy in (("sunder", one_hitter(100.0)),
                               ("shred", scheduled_source(start=1.0))):
            with self.subTest(debuff=debuff):
                spec = body_spec(unit="Krug", driver="Krug", hp=100.0, duration=2.5,
                                 dummy=dummy, debuffs={debuff: 0.3})
                kit = spec["kits"]["base"]
                kit["stats"].update(initialMana=0.0, mana=0.0)
                kit["calcs"]["HealthCalc1"]["terms"] = [
                    {"type": "flat", "value": 1000.0, "op": "add"}]
                spec["unit"]["extras"]["TFT18_KrugMini"].update(armor=100.0, mr=100.0)
                _, res = ENGINE.simulate(spec, True)
                self.assertTrue(res["died"])
                self.assertEqual(res["diedAt"], 1.0)
                self.assertEqual(res["aliveTime"], 2.5)
                self.assertTrue(res["survivalCapped"])
                self.assertTrue(res["stressCapped"])
                body_hits = events(res, "take", "kruglette")
                self.assertEqual(len(body_hits), 1)
                self.assertAlmostEqual(body_hits[0][2], 100.0 / 1.7)

    def test_rank_uses_stress_survival_before_damage_and_leaves_double_caps_tied(self):
        base = {"aliveTime": 60.0, "survivalCapped": True,
                "stressAliveTime": 20.0, "stressCapped": False,
                "denied": 10000.0, "total": 100000.0}
        longer = dict(base, stressAliveTime=30.0, denied=0.0, total=0.0)
        self.assertLess(tft.rank_key(longer, "tank"), tft.rank_key(base, "tank"))
        capped = dict(base, stressAliveTime=60.0, stressCapped=True)
        other = dict(capped, denied=0.0, total=0.0)
        self.assertEqual(tft.rank_key(capped, "tank"), tft.rank_key(other, "tank"))
        measured = dict(other, survivalCapped=False, stressAliveTime=None, stressCapped=False)
        self.assertLess(tft.rank_key(other, "tank"), tft.rank_key(measured, "tank"))

    def test_engine_enumeration_and_python_rank_agree_on_stress_survival(self):
        spec = body_spec(duration=6.5)
        spec["pool"] = [
            {"api": "health", "name": "health", "unique": False,
             "stats": [["hp", 100.0]], "adds": []},
            {"api": "damage", "name": "damage", "unique": False,
             "stats": [["adPct", 10.0]], "adds": []},
        ]
        count, rows = ENGINE.run_cell(spec, 10, 1)
        self.assertEqual(count, 4)
        self.assertEqual(tuple(rows[0][0]), (0, 0, 0))
        self.assertTrue(rows[0][2]["stressCapped"])
        keys = [tft.rank_key(res, "tank") for _, _, res in rows]
        self.assertEqual(keys, sorted(keys))


class TestScheduledTankPressure(unittest.TestCase):
    def test_spell_only_sources_cast_on_schedule_and_count_for_gargoyle(self):
        spec = body_spec(armor=100.0, mr=200.0, duration=2.4,
                         dummy=scheduled_source(),
                         fx=[{"resistsPerAttacker": [10.0, 20.0]}])
        sheet, res = ENGINE.simulate(spec, True)
        self.assertEqual(sheet["armor"], 110.0)
        self.assertEqual(sheet["mr"], 220.0)
        self.assertEqual(res["dummyAttacks"], [0, 0, 0])
        self.assertEqual(res["dummyCasts"], [3, 0, 0])
        hits = events(res, "take", "magic")
        self.assertEqual([e[0] for e in hits], [0.25, 1.25, 2.25])
        for hit in hits:
            self.assertAlmostEqual(hit[2], 100.0 / 3.2)

    def test_stunned_scheduled_cast_waits_then_restarts_its_interval(self):
        # Riders land at 0.25 and stun through 1.75. Even if several spell
        # intervals pass, release one cast at 1.75, then start a fresh cadence.
        for start, interval, expected in (
                (0.5, 0.5, [1.75, 2.25, 2.75, 3.25]),
                (1.75, 1.0, [1.75, 2.75]),
                (2.0, 1.0, [2.0, 3.0])):
            with self.subTest(start=start, interval=interval):
                spec = opening_cast(body_spec(
                    unit="Hecarim", driver="Hecarim", hp=10 ** 6, duration=3.4,
                    dummy=scheduled_source(start=start, interval=interval)))
                spec["kits"]["base"]["rows"]["Resists"] = 0.0
                _, res = ENGINE.simulate(spec, True)
                self.assertEqual(res["casts"], 1)
                self.assertEqual([e[0] for e in events(res, "take", "magic")], expected)
                self.assertEqual(res["dummyCasts"], [len(expected), 0, 0])
                self.assertEqual(res["denied"], 0.0)
                self.assertEqual(res["taken"], 100.0 * len(expected))


class TestTankFormation(unittest.TestCase):
    def test_hecarim_stuns_three_fronts_while_backline_attacks_and_casts(self):
        for geometry in ("clump", "spread"):
            with self.subTest(geometry=geometry):
                spec = opening_cast(body_spec(unit="Hecarim", driver="Hecarim",
                                              hp=10 ** 6, duration=1.6,
                                              dummy=formation_sources()))
                spec["geometry"] = geometry
                _, res = ENGINE.simulate(spec, True)
                self.assertEqual(res["casts"], 1)
                self.assertEqual([e[3] for e in events(res, "damage", "riders")], [0, 1, 2])
                self.assertEqual(res["ccTime"], 4.5)
                self.assertEqual(res["dummyCasts"], [0, 0, 0, 2, 2])
                self.assertEqual(res["denied"], 600.0)  # only six frontline attacks
                self.assertEqual({e[3] for e in events(res, "take")}, {3, 4})
                for source in (3, 4):
                    for damage_type in ("physical", "magic"):
                        hits = events(res, "take", damage_type, target=source)
                        self.assertEqual([e[0] for e in hits], [0.5, 1.5])
                        self.assertTrue(all(e[2] > 0.0 for e in hits))

    def test_a_dead_frontline_exposes_the_next_nearest_backliner_to_hecarim(self):
        for geometry in ("clump", "spread"):
            with self.subTest(geometry=geometry):
                spec = opening_cast(body_spec(unit="Hecarim", driver="Hecarim",
                                              hp=10 ** 6, duration=1.0,
                                              dummy=formation_sources()))
                spec["geometry"] = geometry
                spec["immortal"] = False
                # The opening auto removes the first tank before riders select
                # targets; the fourth slot is now one of the nearest three.
                spec["dummies"]["slots"][0]["hp"] = 1.0
                _, res = ENGINE.simulate(spec, True)
                self.assertEqual([e[3] for e in events(res, "kill")], [0])
                self.assertEqual([e[3] for e in events(res, "damage", "riders")], [1, 2, 3])
                self.assertEqual(res["dummyCasts"], [0, 0, 0, 0, 1])
                self.assertEqual({e[3] for e in events(res, "take")}, {4})

    def test_local_aoe_and_adjacency_leave_protected_backliners_in_range_to_fire(self):
        for unit, driver, source in (("Amumu", "Amumu", "ability"),
                                      ("RekSai", "RekSai", "uproot")):
            for geometry, targets in (("clump", [0, 1, 2]), ("spread", [0])):
                with self.subTest(unit=unit, geometry=geometry):
                    spec = opening_cast(body_spec(unit=unit, driver=driver, hp=10 ** 6,
                                                  duration=1.1,
                                                  dummy=formation_sources(start=0.75)))
                    spec["geometry"] = geometry
                    _, res = ENGINE.simulate(spec, True)
                    self.assertEqual([e[3] for e in events(res, "damage", source)], targets)
                    self.assertEqual(res["dummyCasts"], [0 if i in targets else 1
                                                         for i in range(5)])
                    for backliner in (3, 4):
                        self.assertEqual(len(events(res, "take", "physical", backliner)), 1)
                        self.assertEqual(len(events(res, "take", "magic", backliner)), 1)

    def test_global_stuns_can_still_reach_protected_backliners(self):
        spec = opening_cast(body_spec(unit="Elder Dragon", driver="ElderDragon",
                                      hp=10 ** 6, duration=1.6,
                                      dummy=formation_sources(pre=0.0, start=0.75)))
        _, res = ENGINE.simulate(spec, True)
        # The landing stuns all five through 1.5, despite only the first three
        # being nearby. Every queued spell resumes when that stun ends.
        self.assertEqual(res["ccTime"], 6.25)
        self.assertEqual(res["dummyCasts"], [1] * 5)
        hits = events(res, "take", "magic")
        self.assertEqual([e[3] for e in hits], list(range(5)))
        self.assertEqual([e[0] for e in hits], [1.5] * 5)
        self.assertEqual(res["denied"], 0.0)

    def test_farthest_targeting_can_still_reach_a_protected_backliner(self):
        spec = opening_cast(body_spec(unit="KhaZix", driver="KhaZix", hp=10 ** 6,
                                      duration=0.4, dummy=formation_sources()))
        _, res = ENGINE.simulate(spec, True)
        self.assertEqual([e[3] for e in events(res, "damage", "ability")], [4])

    def test_all_five_targeting_sources_count_for_gargoyle(self):
        for attacks, spells in ((100.0, 0.0), (0.0, 100.0)):
            with self.subTest(attacks=attacks, spells=spells):
                spec = body_spec(armor=100.0, mr=200.0, duration=0.1,
                                 dummy=formation_sources(pre=attacks, ability=spells),
                                 fx=[{"resistsPerAttacker": [10.0, 20.0]}])
                sheet, _ = ENGINE.simulate(spec, False)
                self.assertEqual(sheet["armor"], 150.0)
                self.assertEqual(sheet["mr"], 300.0)

    def test_starting_resistance_auras_only_modify_nearby_targets(self):
        for aura, damage_type in (("sunderAura", "physical"), ("shredAura", "magic")):
            with self.subTest(aura=aura):
                spec = opening_cast(body_spec(unit="Hecarim", driver="Hecarim",
                                              hp=10 ** 6, duration=0.4,
                                              dummy=formation_sources(), fx=[{aura: 0.3}]))
                for slot in spec["dummies"]["slots"]:
                    slot.update(armor=100.0, mr=100.0)
                kit = spec["kits"]["base"]
                # A synthetic five-target, 170-damage cast exercises the
                # resistance aura independently of the driver's damage type.
                kit["rows"]["NumEnemies"] = 5.0
                kit["calcs"]["MagicDamageCalc1"] = {
                    "dtype": damage_type,
                    "terms": [{"type": "flat", "value": 170.0, "op": "add"}]}
                _, res = ENGINE.simulate(spec, True)
                hits = events(res, "damage", "riders")
                self.assertEqual([e[3] for e in hits], list(range(5)))
                for hit, expected in zip(hits, [100.0] * 3 + [85.0] * 2):
                    self.assertAlmostEqual(hit[2], expected)

    def test_ionic_spark_only_reacts_to_nearby_casts(self):
        for geometry in ("clump", "spread"):
            with self.subTest(geometry=geometry):
                spec = body_spec(hp=10 ** 6, duration=0.6,
                                 dummy=formation_sources(pre=0.0), fx=[{"ionicSpark": 2.0}])
                spec["geometry"] = geometry
                for slot in spec["dummies"]["slots"]:
                    slot.update(manaMax=50.0, mr=0.0)
                _, res = ENGINE.simulate(spec, True)
                self.assertEqual(res["dummyCasts"], [1] * 5)
                sparks = events(res, "damage", "ionic spark")
                self.assertEqual([e[3] for e in sparks], [0, 1, 2])
                self.assertEqual([e[2] for e in sparks], [100.0] * 3)
                self.assertEqual({e[3] for e in events(res, "take", "magic")}, set(range(5)))

    def test_local_burn_aura_does_not_follow_a_remote_primary_target(self):
        for nearby in (True, False):
            with self.subTest(nearby=nearby):
                spec = body_spec(hp=10 ** 6, duration=1.1,
                                 dummy=formation_sources(pre=0.0, ability=0.0),
                                 fx=[{"burnAura": [0.01, 3.0]}])
                for slot in spec["dummies"]["slots"]:
                    slot["nearby"] = nearby
                _, res = ENGINE.simulate(spec, True)
                burns = events(res, "damage", "burn")
                if nearby:
                    self.assertTrue(burns)
                    self.assertEqual({e[3] for e in burns}, {0})
                else:
                    self.assertEqual(burns, [])

    def test_optional_nearby_defaults_to_legacy_all_nearby(self):
        spec = opening_cast(body_spec(unit="Amumu", driver="Amumu", hp=10 ** 6,
                                      duration=1.1, dummy=formation_sources(start=0.75)))
        for slot in spec["dummies"]["slots"]:
            slot["nearby"] = True
        expected = ENGINE.simulate(spec, True)
        self.assertEqual([e[3] for e in events(expected[1], "damage", "ability")], list(range(5)))
        for unset in ("missing", "none"):
            with self.subTest(nearby=unset):
                legacy = copy.deepcopy(spec)
                for slot in legacy["dummies"]["slots"]:
                    if unset == "missing":
                        del slot["nearby"]
                    else:
                        slot["nearby"] = None
                self.assertEqual(ENGINE.simulate(legacy, True), expected)

    def test_nearby_rejects_non_boolean_values(self):
        for value in (0, 1, "true", "false", [], {}):
            with self.subTest(nearby=value):
                spec = body_spec(duration=0.1)
                spec["dummies"]["slots"][0]["nearby"] = value
                with self.assertRaises((TypeError, ValueError)):
                    ENGINE.simulate(spec, False)


if __name__ == "__main__":
    unittest.main()
