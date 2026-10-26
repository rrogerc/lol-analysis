"""Core TFT simulation, cast timing, movement, tank benchmark, core analysis, and cache tests."""

from contextlib import ExitStack
import copy
from copy import deepcopy
import itertools
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from tft import sim as tft
from tft.tests.helpers import (
    DUMMY,
    ENGINE,
    ITEM_FX,
    SNAP,
    TRAIT_FX,
    ally,
    body_spec,
    caster,
    events,
    flat,
    fx_of,
    immortal,
    item,
    match,
    match_events,
    one_hitter,
    plain_spec,
    run,
    sim,
    spec_for,
)
from web import server as webapp


class TestCurves(unittest.TestCase):
    def test_hold_semantics(self):
        row = [[1, 0.3], [3, 0.5], [4, 0.8]]
        self.assertEqual([tft.curve_at(row, s) for s in (1, 2, 3, 4)], [0.3, 0.3, 0.5, 0.8])

    def test_below_first_breakpoint(self):
        self.assertEqual(tft.curve_at([[2, 5], [4, 9]], 1), 5)

    def test_override_merges_partial_lists(self):
        row = [[1, 190], [2, 285], [3, 1000], [4, 2200]]
        out = tft.override_curve(row, [225, 335])
        self.assertEqual([tft.curve_at(out, s) for s in (1, 2, 3, 4)], [225, 335, 1000, 2200])

    def test_override_single_value_is_constant(self):
        out = tft.override_curve([[1, 10], [4, 10]], [8])
        self.assertEqual([tft.curve_at(out, s) for s in (1, 2, 3, 4)], [8, 8, 8, 8])


class TestCalcs(unittest.TestCase):
    def test_ashe_arrow_is_the_damage_at_base_ad(self):
        # ArrowDamage 440 at 1★ is the card's number at her 75 base AD,
        # i.e. 587% AD: double the AD, double the arrow
        u = SNAP.unit("Ashe")
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc1", 1, 75, 100, 900, 45, 45), 440.0)
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc1", 1, 150, 100, 900, 45, 45), 880.0)
        # at 2★ the row is 660 at her 112.5 base AD: the same 587%
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc1", 2, 112.5, 100, 900, 45, 45), 660.0)

    def test_ad_rows_scale_like_base_ad(self):
        # Warwick's bite row 200/300/450 over his 40/60/90 AD is 500% AD at every star
        u = SNAP.unit("Warwick")
        for star in (1, 2, 3):
            ad = 40 * 1.5 ** (star - 1)
            self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc1", star, ad, 100, 750, 45, 45) / ad, 5.0)

    def test_akali_mixes_ad_and_flat_ap(self):
        # 145 at 40 base AD plus 10 per 100 AP
        u = SNAP.unit("Akali")
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc1", 1, 40, 100, 650, 35, 35), 155.0)
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc1", 1, 40, 200, 650, 35, 35), 165.0)
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc1", 1, 80, 100, 650, 35, 35), 300.0)

    def test_basic_attack_scaling_is_a_fraction(self):
        # Draven's spinning axe: 1.5 × AD
        u = SNAP.unit("Draven")
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc2", 1, 48, 100, 900, 45, 45), 72.0)

    def test_chained_calc(self):
        # Sivir's bounce is 20% of the first hit (190 at her 50 base AD + 15 AP = 205)
        u = SNAP.unit("Sivir")
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc1", 1, 50, 100, 850, 40, 40), 205.0)
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc2", 1, 50, 100, 850, 40, 40), 41.0)

    def test_star_scaling_of_calcs(self):
        u = SNAP.unit("Ashe")
        two = tft.calc_value(u, "PhysicalDamageCalc1", 2, 112.5, 100, 1620, 45, 45)
        self.assertAlmostEqual(two, 660.0)   # the 2★ row at 2★ base AD

    def test_calc_type_from_name(self):
        self.assertEqual(tft.calc_type("TFTCalculationAttributes.PhysicalDamageCalc1"), "physical")
        self.assertEqual(tft.calc_type("MagicDamageCalc2"), "magic")
        self.assertEqual(tft.calc_type("TrueDamageCalc1"), "true")

    def test_unknown_calc_is_an_error(self):
        with self.assertRaises(KeyError):
            tft.calc_value(SNAP.unit("Ashe"), "NoSuchCalc9", 1, 75, 100, 900, 45, 45)


class TestStatLines(unittest.TestCase):
    def stats(self, name):
        return tft.parse_stat_line(SNAP.item(name))

    def test_infinity_edge(self):
        self.assertEqual(self.stats("Infinity Edge"), {"adPct": 0.35, "crit": 0.35})

    def test_nashors_tooth_conventions(self):
        # health flat, ability power as percent, attack speed as percentMinusOne
        s = self.stats("Nashor's Tooth")
        self.assertEqual(s["hp"], 150)
        self.assertAlmostEqual(s["ap"], 15)
        self.assertAlmostEqual(s["asPct"], 0.1)
        self.assertAlmostEqual(s["crit"], 0.2)

    def test_bloodthirster_percent_points(self):
        # its rows carry percent points with no format attribute
        s = self.stats("Bloodthirster")
        self.assertAlmostEqual(s["adPct"], 0.15)
        self.assertAlmostEqual(s["ap"], 15)
        self.assertEqual(s["mr"], 20)
        self.assertAlmostEqual(s["omnivamp"], 0.20)

    def test_omnivamp_and_durability_conventions(self):
        self.assertAlmostEqual(self.stats("Hextech Gunblade")["omnivamp"], 0.15)   # format="percent"
        self.assertAlmostEqual(self.stats("Spirit Visage")["durability"], 0.08)    # invertedPercent 0.92

    def test_amp_items(self):
        self.assertAlmostEqual(self.stats("Rabadon's Deathcap")["amp"], 0.15)
        self.assertAlmostEqual(self.stats("Deathblade")["adPct"], 0.55)
        self.assertAlmostEqual(self.stats("Red Buff")["asPct"], 0.45)

    def test_every_pool_item_has_a_stat(self):
        pool = tft.pool_items(SNAP, ITEM_FX)
        self.assertEqual(len(pool), 35)
        for api in pool:
            self.assertTrue(tft.parse_stat_line(SNAP.items[api]), api)


class TestSheet(unittest.TestCase):
    def test_star_multipliers(self):
        s2, _ = run("Ashe", star=2)
        self.assertAlmostEqual(s2["ad"], 112.5)
        self.assertAlmostEqual(s2["hp"], 1620)
        s3, _ = run("Ashe", star=3)
        self.assertAlmostEqual(s3["ad"], 168.75)
        self.assertAlmostEqual(s3["hp"], 900 * 1.8 * 1.8)

    def test_crit_excess_and_double_precision(self):
        # 25% base + 90 = 115%: capped, 80% of the excess is crit damage
        # (Riot's patch 13.18 rate), and a second Precision adds 10%
        s, _ = run("Ashe", fx=[{"stats": [["crit", 0.9]], "precision": 2}])
        self.assertAlmostEqual(s["crit"], 1.0)
        self.assertAlmostEqual(s["critMult"], 1.4 + 0.8 * 0.15 + 0.1)
        self.assertTrue(s["precision"])

    def test_attack_speed_additive_on_base_and_capped(self):
        s, _ = run("Ashe", fx=[{"stats": [["asPct", 0.5]]}])
        self.assertAlmostEqual(s["as"], 0.8 * 1.5)
        s, _ = run("Ashe", fx=[{"stats": [["asPct", 20.0]]}])
        self.assertAlmostEqual(s["as"], tft.AS_CAP)

    def test_role_mana(self):
        self.assertEqual(tft.ROLE_MANA["Caster"], 7)
        self.assertEqual(tft.ROLE_MANA["Marksman"], 10)
        self.assertEqual(tft.ROLE_MANA["Assassin"], 10)
        self.assertEqual(tft.ROLE_MANA["Tank"], 5)

    def test_items_build_fx(self):
        fx = fx_of("Ashe", items=("Infinity Edge", "Spear of Shojin"))
        self.assertEqual(fx["precision"], 1)
        self.assertAlmostEqual(fx["manaPerAttack"], 5)
        self.assertAlmostEqual(fx["adPct"], 0.5)
        self.assertAlmostEqual(fx["manaRegen"], 2 + 1)   # caster role + the spear


class TestDamage(unittest.TestCase):
    """Ashe 2★ with the plain driver: 112.5 attack damage, a 25% chance to
    crit for 1.4 (an expected 1.1), against the first target's 110 armor."""

    def first_auto(self, **kw):
        kw.setdefault("dummy", immortal(DUMMY))
        _, res = run("Ashe", driver="Driver", **kw)
        return events(res, "damage", "auto")[0][2]

    def test_attacks_always_crit_by_expected_value(self):
        self.assertAlmostEqual(self.first_auto(), 112.5 * (1 + 0.25 * 0.4) * 100 / 210)

    def test_physical_through_armor_and_health(self):
        _, res = run("Ashe", driver="Driver", duration=0.1)
        hit = events(res, "damage", "auto")[0][2]
        self.assertAlmostEqual(hit, 112.5 * 1.1 * 100 / 210)
        self.assertAlmostEqual(res["left"][0], 3000 - hit)
        self.assertAlmostEqual(res["total"], hit)

    def test_magic_damage_through_first_target_resist(self):
        # Ahri's 2★ orb deals 640 magic damage before the first target's
        # 110 MR. Its primary hit has no secondary-target falloff.
        _, res = run("Ahri", fx=[{"startingMana": 100}], dummy=immortal(DUMMY), duration=2.0)
        hit = events(res, "damage", "ability", target=0)[0][2]
        self.assertAlmostEqual(hit, 640 * 100 / 210)

    def test_true_damage_ignores_everything(self):
        # a burn ticks true damage: no resists, no amp
        _, res = run("Ashe", driver="Driver", fx=[{"burnOnHit": [0.01, 5.0]}, {"adds": [["amp", 0.5]]}],
                     dummy=immortal(DUMMY), duration=1.0)
        burn = events(res, "damage", "burn")
        self.assertTrue(burn)
        self.assertAlmostEqual(burn[0][2], 0.01 * 10 ** 6 * tft.TICK_S)

    def test_amp_is_post_mitigation_and_additive(self):
        fx = [{"adds": [["amp", 0.1]], "ampVsTank": 0.15}]
        self.assertAlmostEqual(self.first_auto(fx=fx), 112.5 * 1.1 * 100 / 210 * 1.25)
        # the tank-only amp does not apply to the non-tank dummy
        other = DUMMY["slots"][-1]
        first = dict(DUMMY, slots=[other] + DUMMY["slots"][:2])
        self.assertAlmostEqual(self.first_auto(fx=fx, dummy=immortal(first)),
                               112.5 * 1.1 * 100 / (100 + other["armor"]) * 1.1)

    def test_ability_crits_only_with_precision(self):
        u = SNAP.unit("Ashe")
        _, plain = run("Ashe", dummy=immortal(DUMMY))
        arrow = tft.calc_value(u, "PhysicalDamageCalc1", 2, 112.5, 100, 1620, 45, 45)
        self.assertAlmostEqual(events(plain, "damage", "ability")[0][2], arrow * 100 / 210)
        _, prec = run("Ashe", fx=[{"precision": 1}], dummy=immortal(DUMMY))
        self.assertAlmostEqual(events(prec, "damage", "ability")[0][2],
                               arrow * 100 / 210 * (1 + 0.25 * 0.4))

    def test_sunder_on_hit(self):
        _, res = run("Ashe", driver="Driver", fx=[{"sunderOnHit": [0.3, 3.0]}], dummy=immortal(DUMMY))
        autos = events(res, "damage", "auto")
        self.assertAlmostEqual(autos[0][2], 112.5 * 1.1 * 100 / 210)                # sundered after the hit
        self.assertAlmostEqual(autos[1][2], 112.5 * 1.1 * 100 / (100 + 110 * 0.7))  # 1.25 s later, inside 3 s

    def test_burn_ticks_true_damage(self):
        _, res = run("Ashe", driver="Driver", fx=[{"burnOnHit": [0.01, 5.0]}], duration=1.0)
        burn = events(res, "damage", "burn")
        self.assertAlmostEqual(burn[0][0], 0.25)
        self.assertAlmostEqual(burn[0][2], 0.01 * 3000 * 0.25)
        self.assertAlmostEqual(res["breakdown"]["burn"], sum(e[2] for e in burn))

    def test_overkill_moves_to_the_next_dummy(self):
        # a 3★ Aphelios with a full bar one-shots: the total is capped at the
        # dummies' health and the rest is overkill
        _, res = sim("Aphelios", ("Deathblade", "Protector's Vow", "Protector's Vow"), star=3,
                     geometry="spread")
        self.assertAlmostEqual(res["total"], DUMMY["totalHp"], places=3)
        self.assertGreater(res["rawTotal"], res["total"])
        self.assertEqual(res["left"], [0.0] * len(DUMMY["slots"]))

    def test_aoe_geometry(self):
        # Ahri's bomb has a 3-hex radius (HexRadius), so the formation decides
        # what it covers rather than a nearby flag. Clumped the frontline sits
        # at lanes 2/3/4/5 and the backline at lanes 2/4 two rows behind, so
        # the bomb centred on the first frontliner reaches all four of them
        # and the backliner directly behind it. Spread it also reaches the
        # frontliner two hexes away and the near backliner three hexes away.
        for geo, hits in (("clump", 5), ("spread", 3)):
            _, res = run("Ahri", geometry=geo, dummy=immortal(DUMMY))
            land = events(res, "land")[0][0]
            self.assertEqual(len([e for e in events(res, "damage", "ability") if e[0] == land]), hits, geo)


class TestAbilityRadii(unittest.TestCase):
    """Areas with a stated radius measure the board, not the nearby flag.

    Each radius is a kit row: one Riot publishes, or one kits.json
    transcribes from the ability's tooltip because the data has none. The
    coverage below follows from tft.FRONT_LANES / BACK_LANES and the
    |dlane| + |drow| rule, with the epicentre on the first frontliner.
    """

    HAND = tft.load_kits(SNAP.set_no)

    def radius_rows(self, api):
        return ((self.HAND.get("units") or {}).get(api) or {}).get("radii") or {}

    def test_every_declared_radius_resolves(self):
        for api, rows in ((api, self.radius_rows(api)) for api in SNAP.units):
            for row, meta in rows.items():
                unit = SNAP.units[api]
                extra = tft.ability_radius_rows(unit, self.HAND)
                for star in tft.unit_stars(unit):
                    kit = tft.kit_spec(unit, star, None, extra)
                    self.assertIn(row, kit["rows"], f"{api}/{row} at {star}star")
                    self.assertGreater(kit["rows"][row], 0.0, f"{api}/{row} at {star}star")
                    if "byStar" not in meta and meta["source"] == "tooltip":
                        self.assertEqual(kit["rows"][row], float(meta["hexes"]), f"{api}/{row}")

    def test_transcribed_radii_are_not_in_the_data(self):
        # A row Riot starts publishing must take over from the transcription,
        # so a tooltip entry is only legitimate while the data has none.
        for api in SNAP.units:
            for row, meta in self.radius_rows(api).items():
                published = row in (SNAP.units[api]["curve"] or {})
                self.assertEqual(published, meta["source"] == "row", f"{api}/{row}")

    def test_small_radius_no_longer_covers_the_whole_frontline(self):
        # Gromp's 1-hex cloud: the target and the frontliner one lane over
        # when packed, the target alone when the frontline is spread.
        for geo, hits in (("clump", 2), ("spread", 1)):
            _, res = run("Gromp", geometry=geo, dummy=immortal(DUMMY))
            clouds = {e[3] for e in events(res, "damage", "cloud")}
            self.assertEqual(len(clouds), hits, geo)

    def test_radius_two_reaches_the_near_backliner(self):
        # Amumu's passive is 1 hex and his cast 2 (PassiveHexRadius,
        # BigHexRadius): the cast adds the far frontliner and the backliner
        # standing two rows behind the target.
        _, res = run("Amumu", geometry="clump", dummy=immortal(DUMMY))
        self.assertEqual(len({e[3] for e in events(res, "damage", "tantrum")}), 2)
        self.assertEqual(len({e[3] for e in events(res, "damage", "ability")}), 4)

    def test_a_three_slot_board_is_the_frontline(self):
        # What the theoretical composition probes are: a frontline with
        # nothing behind it, in the geometry's own lanes.
        slots = [dict(s, hp=10 ** 6) for s in DUMMY["slots"][:3]]
        placed = tft.board_positions([dict(s) for s in slots], "clump")
        self.assertEqual([(s["lane"], s["row"]) for s in placed],
                         [(2, 0), (3, 0), (4, 0)])
        board = dict(DUMMY, slots=slots, count=3, board=[1, 1, 1])
        _, res = run("Gromp", geometry="clump", dummy=board)
        self.assertEqual(len({e[3] for e in events(res, "damage", "cloud")}), 2)

    def test_a_board_without_positions_keeps_the_old_rule(self):
        # A null lane is the explicit opt-out, and a board that is neither
        # shape gets no positions at all; both keep every selection as it was.
        slots = [dict(s, hp=10 ** 6, lane=None) for s in DUMMY["slots"][:3]]
        self.assertIsNone(tft.board_positions([dict(s) for s in slots], "clump")[0]["lane"])
        board = dict(DUMMY, slots=slots, count=3, board=[1, 1, 1])
        _, res = run("Gromp", geometry="clump", dummy=board)
        self.assertEqual(len({e[3] for e in events(res, "damage", "cloud")}), 3)

        two = [dict(s, hp=10 ** 6) for s in DUMMY["slots"][:2]]
        self.assertNotIn("lane", tft.board_positions([dict(s) for s in two], "clump")[0])


class TestManaCycle(unittest.TestCase):
    """Ashe with nothing: 7 mana per attack at 0.8 attacks/s, 2 regen/s,
    20 of 80 to start; regen lands on the quarter second ticks.

    The public path carries her cast timeline (cast-timing.json: attack
    delay 0.21 s, recovery 0.29 s, cast animation 1.58 s). Attacks land at
    0.21, 1.46, …; after the one at 6.46 s she has 20 + 42 + 12.5 = 74.5,
    the ticks through 7.5 s add 2.5 and the attack at 7.71 s 7: a full bar,
    so the cast starts at that attack's unlock point, 8.0 s, with 84.5 on
    the bar (the tick at 7.75 s still paid). It leaves 4.5. Until 9.58 s she
    neither attacks nor gains mana: the tick at 9.75 s pays for the 0.17 s
    after the window, and a fresh attack lands at 9.79 s. Eight attacks and
    39 ticks later the second bar fills on the tick at 19.5 s.

    Under the flat rules (any unit without a timeline, and this module's
    fixtures) attacks land at 0, 1.25, …: after the attack at 6.25 s she
    has 74.5, the ticks through 7.5 s add 2.5 and the attack there 7, so
    the first cast is at 7.5 s with 84 on the bar. It leaves 4 mana and a
    one-second lock that blocks exactly a second of regen (the tick at
    8.5 s pays nothing: its whole quarter second is inside the lock); the
    second bar fills on the tick at 18.5 s."""

    def test_cast_times(self):
        _, res = sim("Ashe", dummy=immortal(DUMMY), duration=20.0)
        self.assertEqual(res["casts"], 2)
        self.assertAlmostEqual(res["castTimes"][0], 8.0)
        self.assertAlmostEqual(res["castTimes"][1], 19.5)
        self.assertEqual(res["attacks"], 15)

    def test_cast_window_and_overflow(self):
        _, res = run("Ashe", dummy=immortal(DUMMY), duration=9.8, timed=True)
        self.assertEqual(res["casts"], 1)
        self.assertAlmostEqual(events(res, "cast")[0][2], 84.5)       # the bar at the cast
        self.assertAlmostEqual(res["probe"]["castingUntil"], 9.58)
        self.assertAlmostEqual(res["probe"]["lockUntil"], 9.58)       # no mana inside the window
        self.assertAlmostEqual(events(res, "attack")[-1][0], 9.79)    # the fresh attack
        # the overflow, the tick's 0.17 s of regen after the window, that attack
        self.assertAlmostEqual(res["probe"]["mana"], 4.5 + 2 * 0.17 + 7)

    def test_flat_cast_times(self):
        _, res = run("Ashe", dummy=immortal(DUMMY), duration=20.0)
        self.assertEqual(res["casts"], 2)
        self.assertAlmostEqual(res["castTimes"][0], 7.5)
        self.assertAlmostEqual(res["castTimes"][1], 18.5)
        self.assertEqual(res["attacks"], 16)

    def test_mana_lock_and_overflow(self):
        _, res = run("Ashe", dummy=immortal(DUMMY), duration=7.6)
        self.assertEqual(res["casts"], 1)
        self.assertAlmostEqual(events(res, "cast")[0][2], 84.0)       # the bar at the cast
        self.assertAlmostEqual(res["probe"]["mana"], 4.0)             # the overflow carries
        self.assertAlmostEqual(res["probe"]["castingUntil"], 7.75)
        self.assertAlmostEqual(res["probe"]["lockUntil"], 8.5)        # no mana for the second after

    def test_manaless_units_never_cast(self):
        _, res = sim("Kayle")
        self.assertEqual(res["casts"], 0)
        self.assertIn("ascension", res["breakdown"])


class TestTraits(unittest.TestCase):
    def test_contexts_skip_prismatic_tiers(self):
        u = SNAP.unit("Ashe")
        ctx, unmodeled = tft.unit_trait_contexts(SNAP, u, TRAIT_FX)
        by_name = {SNAP.traits[a]["name"]: SNAP.traits[a]["levels"][c - 1] for a, c in ctx["high"]}
        self.assertEqual(by_name, {"Blossom": 9, "Hunter": 5})
        self.assertEqual({SNAP.traits[a]["name"]: SNAP.traits[a]["levels"][c - 1] for a, c in ctx["low"]},
                         {"Blossom": 3, "Hunter": 2})
        self.assertEqual(ctx["bare"], [])
        self.assertEqual(unmodeled, [])

    def test_hunter_ad_and_delayed_amp(self):
        u = SNAP.unit("Ashe")
        ctx, _ = tft.unit_trait_contexts(SNAP, u, TRAIT_FX)
        hunter = [(a, c) for a, c in ctx["low"] if SNAP.traits[a]["name"] == "Hunter"]
        fx = fx_of("Ashe", traits=hunter)
        self.assertAlmostEqual(fx["adPct"], 0.2)
        self.assertEqual(fx["ampAfterSameTarget"], (0.1, 4.0))
        _, res = run("Ashe", driver="Driver", traits=hunter, dummy=immortal(DUMMY))
        autos = events(res, "damage", "auto")
        self.assertAlmostEqual(autos[3][0], 3.75)
        self.assertAlmostEqual(autos[3][2], autos[0][2])            # under four seconds on the target
        self.assertAlmostEqual(autos[4][2], autos[0][2] * 1.1)      # at 5 s

    def test_lunar_doubles_for_its_own(self):
        u = SNAP.unit("Aphelios")
        api = next(a for a in u["traitApis"] if SNAP.traits[a]["name"] == "Lunar")
        fx = fx_of("Aphelios", traits=[(api, 1)])
        self.assertAlmostEqual(fx["asPct"], 0.14)
        self.assertAlmostEqual(fx["ap"], 14)

    def test_executioner_gives_precision(self):
        u = SNAP.unit("Ezreal")
        api = next(a for a in u["traitApis"] if SNAP.traits[a]["name"] == "Executioner")
        fx = fx_of("Ezreal", traits=[(api, 2)])
        self.assertEqual(fx["precision"], 1)
        self.assertAlmostEqual(fx["crit"], 0.15)
        self.assertAlmostEqual(fx["bleedPct"], 0.3)

    def test_every_modeled_trait_resolves(self):
        for api, spec in TRAIT_FX.items():
            if api.startswith("_"):
                continue
            t = SNAP.traits[api]
            u = next(SNAP.units[x] for x in SNAP.units if api in SNAP.units[x]["traitApis"])
            for col in range(1, len(t["levels"]) + 1):
                fx_of(u["name"], traits=[(api, col)])


class TestEffects(unittest.TestCase):
    def test_every_modeled_item_effect_resolves(self):
        for api in tft.pool_items(SNAP, ITEM_FX):
            fx = fx_of("Ashe", items=[api])
            self.assertTrue(any(v for k, v in fx.items() if k not in ("form", "notes")), api)

    def test_guinsoo_stacks_per_second(self):
        _, res = run("Ashe", fx=[{"asPerSecond": [0.07, None]}])
        self.assertGreater(res["probe"]["asStack"], 0.07 * 15)

    def test_stacking_items_change_the_ranking_inputs(self):
        _, plain = sim("Ashe")
        _, kraken = sim("Ashe", ("Kraken's Fury",))
        self.assertGreater(kraken["total"], plain["total"])


class TestDrivers(unittest.TestCase):
    def test_every_driver_runs_everywhere(self):
        trio = ("Infinity Edge", "Jeweled Gauntlet", "Spear of Shojin")
        tanky = ("Warmog's Armor", "Bramble Vest", "Sterak's Gage")
        for u in tft.modeled_units(SNAP):
            ctx, _ = tft.unit_trait_contexts(SNAP, u, TRAIT_FX)
            for items, geo, c, star in itertools.product(((), trio, tanky), ("spread", "clump"),
                                                          ("bare", "high"), tft.unit_stars(u)):
                sheet, res = sim(u["name"], items, star, geo, ctx[c])
                self.assertGreater(res["total"], 0, u["name"])
                self.assertAlmostEqual(sum(res["breakdown"].values()), res["total"], places=6)
                if res["killTime"] is not None:
                    self.assertAlmostEqual(res["total"], DUMMY["totalHp"], places=3)
                self.assertLessEqual(res["aliveTime"], tft.fight_duration(u) + 1e-9)
                pressured = u["objective"] in tft.PRESSURED or (
                    u["api"] == "TFT18_Nidalee" and sheet["form"] == "AD")
                self.assertEqual(sheet["pressure"], pressured)
                if not pressured:
                    self.assertEqual(res["absorbed"], 0.0)

    def test_every_unit_has_a_driver(self):
        self.assertEqual(len(tft.modeled_units(SNAP)), len(SNAP.units))
        self.assertEqual(set(ENGINE.DRIVERS.values()), set(ENGINE.NAMES) - {"Driver"})
        for u in SNAP.units.values():
            self.assertIn(tft.driver_name(u), ENGINE.NAMES, u["name"])

    def test_kayle_waves_need_company(self):
        _, clump = sim("Kayle", star=3, geometry="clump")
        _, spread = sim("Kayle", star=3, geometry="spread")
        self.assertIn("waves", clump["breakdown"])
        self.assertNotIn("waves", spread["breakdown"])

    def test_caitlyn_headshots_every_third_attack(self):
        _, res = sim("Caitlyn", dummy=immortal(DUMMY), duration=20.0)
        self.assertEqual(res["casts"], 0)
        # 0.7 attacks/s over 20 s; the last swing lands on the 20 s mark, so
        # floating point decides whether it is inside the window
        self.assertIn(res["attacks"], (14, 15))
        self.assertIn("headshot", res["breakdown"])
        # a headshot every third attack, so a third of the swings and the
        # bigger share of the damage
        self.assertGreater(res["breakdown"]["headshot"], res["breakdown"]["auto"])

    def test_khazix_isolation(self):
        _, spread = sim("Kha'Zix", geometry="spread")
        self.assertIn("isolated", spread["breakdown"])


class TestRoles(unittest.TestCase):
    """Riot's role label decides the mana rules and what a unit is scored on."""

    def test_shop_units_only(self):
        self.assertEqual(len(SNAP.units), 65)
        self.assertIn("TFT18_Fiddlesticks", SNAP.units)     # health from the override
        self.assertIn("TFT18_Ivern", SNAP.units)
        self.assertNotIn("TFT18_EliseSpider", SNAP.units)   # a transformed form, not a shop unit
        self.assertIn("TFT18_EliseSpider", SNAP.extras)
        self.assertEqual(SNAP.unit("Kog'Maw")["stats"]["ad"], 40)   # override: the file has none

    def test_role_from_the_role_definition_not_the_tag_list(self):
        akali = SNAP.unit("Akali")       # her own tag list lacks Role.Attack
        self.assertTrue(akali["attack"])
        self.assertEqual(akali["kind"], "Fighter")
        self.assertEqual(SNAP.unit("Rengar")["kind"], "Fighter")   # tags say Assassin, the role says Fighter
        self.assertEqual(SNAP.unit("Leona")["roleName"], "Magic Tank")
        self.assertEqual(SNAP.unit("Caitlyn")["resource"], "Ammo")

    def test_objective_by_role(self):
        want = {"Leona": "tank", "Sett": "tank", "Warwick": "fighter", "Kha'Zix": "fighter",
                "Ashe": "carry", "Lux": "carry", "Kayle": "carry",
                "Master Yi": "fighter", "Gnar": "fighter", "Caitlyn": "carry"}   # recommended-items role wins
        for name, obj in want.items():
            self.assertEqual(SNAP.unit(name)["objective"], obj, name)
        self.assertEqual(tft.fight_duration(SNAP.unit("Leona")), tft.TANK_DURATION)
        self.assertEqual(tft.fight_duration(SNAP.unit("Ashe")), tft.FIGHT_DURATION)

    def test_adaptor_forms(self):
        for name in ("Akali", "Gromp", "Kog'Maw", "Master Yi", "Nidalee"):
            u = SNAP.unit(name)   # the file carries the form the unit is not in by default
            self.assertIn("AP" if u["attack"] else "AD", u["forms"], name)
        self.assertEqual(fx_of("Gromp")["form"], "AP")            # tie: his role is Magic
        self.assertEqual(fx_of("Akali")["form"], "AD")
        self.assertEqual(fx_of("Gromp", fx=[{"stats": [["adPct", 0.5]]}])["form"], "AD")
        self.assertEqual(fx_of("Gromp", fx=[{"stats": [["adPct", 0.3], ["ap", 50.0]]}])["form"], "AP")
        self.assertIsNone(fx_of("Ashe", fx=[{"stats": [["adPct", 0.5]]}])["form"])
        self.assertEqual(SNAP.unit("Lux")["forms"], {})   # her Avatar variants are not forms
        # the magic form fights with its own attack damage row
        self.assertAlmostEqual(run("Gromp", star=1)[0]["ad"], 30.0)
        self.assertAlmostEqual(run("Gromp", star=1, fx=[{"stats": [["adPct", 0.5]]}])[0]["ad"], 45.0 * 1.5)
        self.assertEqual(SNAP.unit("Elise")["curve"]["SpiderHealthBuff"][0], [1, 375])   # from curveValues

    def test_dummies_carry_their_groups_offense(self):
        for s in DUMMY["slots"]:
            self.assertGreater(s["ad"], 0)
            self.assertGreater(s["as"], 0)
            self.assertGreater(s["ability"], 0)
            self.assertTrue(0 <= s["physicalShare"] <= 1)
            self.assertEqual(s["manaFromDamage"], s["kind"] == "tank")
        self.assertGreater(DUMMY["pressureDps"], 100)


class TestBody(unittest.TestCase):
    """The unit's own health, for the fights where the dummies hit back."""

    def test_mitigation_order(self):
        # Leona 1★: 40 armor. 100 physical -> 71.43 through armor; 20% durability -> 57.14;
        # a 30-point shield eats 30 of that; the rest reaches health.
        hp = 700.0
        _, res = run("Leona", star=1, driver="Driver", dummy=one_hitter(100.0), duration=1.5,
                     fx=[{"durability": 0.2}, {"shieldAtStart": [30.0 / hp, 5.0]}])
        take = events(res, "take")
        self.assertEqual(len(take), 1)
        post = 100 * 100 / 140 * 0.8
        self.assertAlmostEqual(take[0][2], post, places=6)
        self.assertAlmostEqual(res["shielded"], 30.0)
        self.assertAlmostEqual(take[0][5], hp - (post - 30.0), places=6)   # health after the hit
        self.assertAlmostEqual(res["absorbed"], 100.0)
        self.assertAlmostEqual(res["taken"], post)
        # Bramble's attack reduction applies before durability
        _, res2 = run("Leona", star=1, driver="Driver", dummy=one_hitter(100.0), duration=1.5,
                      fx=[{"durability": 0.2}, {"attackDamageTaken": 0.5}])
        self.assertAlmostEqual(events(res2, "take")[0][2], 100 * 100 / 140 * 0.5 * 0.8)

    def test_durability_stacks_multiplicatively(self):
        fx = fx_of("Leona", fx=[{"durability": 0.2}, {"durability": 0.15}])
        self.assertAlmostEqual(fx["durability"], 0.32)
        # Steadfast Heart: one value above the threshold, the other below
        _, res = run("Leona", driver="Driver", dummy=one_hitter(100.0), duration=1.5,
                     fx=[{"durability": 0.2}, {"durabilityByHealth": [0.05, 0.15, 0.5]}])
        self.assertAlmostEqual(events(res, "take")[0][2], 100 * 100 / 140 * 0.8 * 0.85)   # at full health
        self.assertAlmostEqual(fx_of("Leona", fx=[{"durability": 0.2}, {"durabilityByHealth": [0.05, 0.15, 0.5]}])["durability"],
                               1 - 0.8 * 0.95)   # the sheet quotes the low-health value

    def test_tank_mana_from_damage_is_capped(self):
        # Leona 2★ (40 armor, 40/100 mana, 5 per attack): her attack at 0 s,
        # then a 200 hit at 1 s: 1% of it and 3% of what got through
        _, res = run("Leona", driver="Driver", dummy=one_hitter(200.0), duration=1.5)
        post = 200 * 100 / 140
        self.assertAlmostEqual(res["probe"]["mana"], 40 + 5 + 200 * 0.01 + post * 0.03)
        _, res = run("Leona", driver="Driver", dummy=one_hitter(100000.0), duration=1.5)
        self.assertAlmostEqual(res["probe"]["mana"], 40 + 5 + tft.TANK_MANA_PER_HIT_CAP)
        # a fighter gains nothing from being hit (two attacks of his own by 1.5 s)
        _, res = run("Warwick", driver="Driver", dummy=one_hitter(200.0), duration=1.5)
        self.assertEqual(res["hitsTaken"], 1)
        self.assertAlmostEqual(res["probe"]["mana"], 20.0)

    def test_assassins_take_less_from_the_others(self):
        _, res = run("Kha'Zix", driver="Driver", dummy=one_hitter(100.0, attackers=2), duration=1.5)
        take = events(res, "take")
        self.assertEqual([e[3] for e in take], [0, 1])
        self.assertAlmostEqual(take[0][2], 100 * 100 / 155)
        self.assertAlmostEqual(take[1][2], 100 * (1 - tft.ASSASSIN_OFFTARGET_REDUCTION) * 100 / 155)

    def test_omnivamp_heals_from_damage_dealt(self):
        _, res = run("Warwick", driver="Driver", fx=[{"stats": [["omnivamp", 0.5]]}])
        tr = res["trace"]
        i = next(i for i, e in enumerate(tr) if e[1] == "heal" and e[4] == "omnivamp")
        dmg = next(e for e in reversed(tr[:i]) if e[1] == "damage")
        self.assertGreater(tr[i][2], 0)
        self.assertLessEqual(tr[i][2], dmg[2] * 0.5 + 1e-9)   # half the damage, capped by the missing health
        # a carry never bleeds, so nothing to heal
        _, res = run("Ashe", fx=[{"stats": [["omnivamp", 0.5]]}])
        self.assertEqual(res["healed"], 0.0)

    def test_heal_caps_at_max_health(self):
        _, res = run("Vi", fx=[{"stats": [["omnivamp", 5.0]]}])
        self.assertGreater(res["healed"], 0)
        # health lost is what got past the shields; healing never exceeds it
        self.assertLessEqual(res["healed"], res["taken"] - res["shielded"] + 1e-6)
        self.assertLessEqual(res["hpLeft"], res["probe"]["maxHp"] + 1e-9)

    def test_death_ends_the_fight_and_a_body_holds_on(self):
        _, res = sim("Akali")
        self.assertTrue(res["died"])
        self.assertAlmostEqual(res["aliveTime"], res["diedAt"])
        self.assertLess(res["aliveTime"], tft.FIGHT_DURATION)
        self.assertEqual(res["t"], res["diedAt"])
        # Krug splits into kruglettes that keep the dummies busy after he falls
        _, res2 = sim("Krug")
        self.assertTrue(res2["died"])
        self.assertGreater(res2["aliveTime"], res2["diedAt"])

    def test_stuns_and_untargetability_deny_damage(self):
        # Hecarim is a tank: his own board is the threat preset's, where every
        # slot stands for one attacker. The legacy split puts the fast slots
        # behind the frontline, outside the three his stun selects.
        tank_board = dict(tft.dummies_for(SNAP, threat="mixed"), meleeRepositionSeconds=0.0)
        # Give him enough health to remain alive for the enemy attacks his
        # first stun blocks; the six-source preset kills an unitemized body
        # before those attacks become due.
        _, res = sim("Hecarim", ("Warmog's Armor",), geometry="clump", dummy=tank_board)
        self.assertGreater(res["ccTime"], 0.0)
        self.assertGreater(res["denied"], 0.0)
        _, res = run("Leona", driver="Driver", fx=[{"untargetableAtHp": [0.6, 1.0, 0.2]}])
        self.assertGreater(res["denied"], 0.0)
        self.assertGreater(res["probe"]["untargetableUntil"], 0.0)

    def test_legacy_board_streams_remain_available_for_custom_fights(self):
        self.assertEqual(sum(DUMMY["board"]), tft.BOARD_SIZE)
        self.assertGreater(DUMMY["boardPressureDps"], 2 * DUMMY["pressureDps"])
        # a tank's fight is the whole board's; a fighter faces the four in
        # front of it and the backline stands there as a damage target only
        _, leona = sim("Leona", duration=10.0)
        _, akali = sim("Akali", duration=10.0)
        rate = lambda r: r["hitsTaken"] / r["aliveTime"]
        self.assertGreater(rate(leona), rate(akali) * 2)
        self.assertEqual(len(leona["dummyAttacks"]), tft.FRONTLINERS + tft.BACKLINERS)
        # The legacy eight-attacker allocation need not arm every slot: its
        # three tank-role streams leave the fourth frontliner unarmed.
        self.assertEqual([n > 0 for n in leona["dummyAttacks"]],
                         [streams > 0 for streams in DUMMY["board"]])
        self.assertTrue(all(n > 0 for n in akali["dummyAttacks"][:tft.FRONTLINERS]))
        self.assertEqual(akali["dummyAttacks"][tft.FRONTLINERS:], [0] * tft.BACKLINERS)
        self.assertEqual(akali["dummyCasts"][tft.FRONTLINERS:], [0] * tft.BACKLINERS)

    def test_dummies_cast_on_their_mana(self):
        # the non-tank dummy: 7 mana per attack, 42.5 to fill, first swing one period in
        _, res = run("Leona", fx=[{"stats": [["hp", 10 ** 9]]}])
        self.assertGreaterEqual(res["dummyCasts"][tft.FRONTLINERS], 2)
        self.assertGreater(res["dummyCasts"][0], 0)   # tanks fill from the damage Leona deals them
        self.assertGreater(res["absorbed"], 0.0)

    def test_health_triggers_fire_once(self):
        _, res = run("Leona", driver="Driver",
                     fx=[{"shieldAtHp": [0.5, 0.25, 5.0, 0.0]}, {"manaAtHp": [0.5, 15.0]}])
        low = events(res, "shield", "low health")
        self.assertEqual(len(low), 1)
        self.assertAlmostEqual(low[0][2], 0.25 * res["probe"]["maxHp"])
        self.assertLess(low[0][5], 0.5 * res["probe"]["maxHp"])

    def test_edge_of_night(self):
        _, res = run("Leona", driver="Driver", fx=[{"untargetableAtHp": [0.6, 1.0, 0.2]}])
        heal = events(res, "heal", "edge of night")
        self.assertEqual(len(heal), 1)
        t, _, amount, _, _, hp_after = heal[0]
        missing_before = res["probe"]["maxHp"] - (hp_after - amount)
        self.assertAlmostEqual(amount, missing_before * 0.2 * (1 - tft.tank_debuffs(SNAP)["wound"]))
        self.assertGreater(res["denied"], 0.0)

    def test_gargoyle_counts_attackers(self):
        s, _ = run("Leona", driver="Driver", fx=[{"resistsPerAttacker": [10.0, 5.0]}])
        base, _ = run("Leona", driver="Driver")
        debuffs = tft.tank_debuffs(SNAP)
        self.assertAlmostEqual(s["armor"], base["armor"] + 10.0 * tft.BOARD_SIZE * (1 - debuffs["sunder"]))
        self.assertAlmostEqual(s["mr"], base["mr"] + 5.0 * tft.BOARD_SIZE * (1 - debuffs["shred"]))
        g, _ = run("Ashe", fx=[{"resistsPerAttacker": [10.0, 5.0]}])   # carry: nobody attacks
        self.assertAlmostEqual(g["armor"], 45.0)

    def test_timed_healing(self):
        _, res = run("Leona", driver="Driver", duration=4.0,
                     dummy=one_hitter(),
                     fx=[{"healPerInterval": [0.025, 2.0]}, {"regenMissingPct": 0.02}])
        claw = events(res, "heal", "dragon's claw")
        self.assertEqual([round(e[0], 2) for e in claw], [2.0, 4.0])
        self.assertTrue(events(res, "heal", "regeneration"))

    def test_titans_stacks_from_being_hit(self):
        _, res = run("Leona", driver="Driver", duration=3.0, dummy=one_hitter(pre=10.0, period=0.1),
                     fx=[{"adapPerAttack": [0.02, 25, 0.1], "adapPerHit": True}])
        self.assertEqual(res["probe"]["adapStackN"], 25)
        _, attacks_only = run("Leona", driver="Driver", duration=3.0,
                              dummy=one_hitter(pre=10.0, period=0.1),
                              fx=[{"adapPerAttack": [0.02, 25, 0.1]}])
        self.assertLess(attacks_only["probe"]["adapStackN"], 25)

    def test_hand_of_justice_doubles_by_health(self):
        s, res = run("Warwick", driver="Driver", fx=[{"hoj": [0.15, 15.0, 0.12, 0.5]}])
        self.assertAlmostEqual(s["ad"], 60 * (1 + 0.30))      # doubled above half health
        autos = events(res, "damage", "auto", target=0)
        half = res["probe"]["maxHp"] * 0.5
        low = next(e for e in autos if e[5] < half)
        self.assertAlmostEqual(low[2] / autos[0][2], 1.15 / 1.30)

    def test_ionic_spark_fires_on_dummy_casts(self):
        _, res = sim("Akali", ("Ionic Spark",), star=3, pressure=True)
        self.assertIn("ionic spark", res["breakdown"])

    def test_defensive_items_extend_a_tank(self):
        _, naked = sim("Leona", driver="Driver")
        _, built = sim("Leona", ("Warmog's Armor", "Bramble Vest", "Dragon's Claw"), driver="Driver")
        self.assertTrue(naked["died"])
        self.assertGreater(built["aliveTime"], naked["aliveTime"] * 2)
        self.assertGreater(built["healed"], 0)
        self.assertIn("thorns", built["breakdown"])


class TestObjectives(unittest.TestCase):
    def test_tank_key_prefers_holding_longer(self):
        long = {"killTime": None, "total": 100, "aliveTime": 40.0, "absorbed": 5000, "denied": 0}
        short = {"killTime": None, "total": 9999, "aliveTime": 20.0, "absorbed": 9000, "denied": 0}
        self.assertLess(tft.rank_key(long, "tank"), tft.rank_key(short, "tank"))
        tie = dict(long, total=200)
        self.assertLess(tft.rank_key(tie, "tank"), tft.rank_key(long, "tank"))
        # the carry key ignores survival entirely
        self.assertLess(tft.rank_key(short, "carry"), tft.rank_key(long, "carry"))

    def test_enumeration_sorts_by_the_units_objective(self):
        pool = tft.pool_items(SNAP, ITEM_FX)[:4]
        u = SNAP.unit("Akali")
        out, count = tft.enumerate_builds(SNAP, u, 2, "clump", [], DUMMY, pool, workers=1)
        self.assertEqual(count, 20)
        keys = [tft.rank_key(r, u["objective"]) for _, _, r in out]
        self.assertEqual(keys, sorted(keys))
        self.assertTrue(all(r["absorbed"] > 0 for _, _, r in out))   # she is hit in every build

    def test_carries_are_never_hit(self):
        _, res = sim("Ashe")
        self.assertFalse(res["died"])
        self.assertEqual(res["absorbed"], 0.0)
        self.assertEqual(res["aliveTime"], tft.FIGHT_DURATION)


class TestEnumeration(unittest.TestCase):
    def test_combination_count(self):
        pool = tft.pool_items(SNAP, ITEM_FX)
        n = sum(1 for _ in itertools.combinations_with_replacement(pool, 3))
        self.assertEqual(n, 7770)

    def test_ranking_order(self):
        killer = {"killTime": 5.0, "total": 100}
        faster = {"killTime": 4.0, "total": 50}
        survivor = {"killTime": None, "total": 9999}
        self.assertLess(tft.rank_key(faster), tft.rank_key(killer))
        self.assertLess(tft.rank_key(killer), tft.rank_key(survivor))

    def test_small_pool_enumerates_sorted(self):
        pool = tft.pool_items(SNAP, ITEM_FX)[:5]
        u = SNAP.unit("Ashe")
        out, count = tft.enumerate_builds(SNAP, u, 2, "clump", [], DUMMY, pool, workers=1)
        self.assertEqual(count, 35)
        self.assertEqual(len(out), 35)
        keys = [tft.rank_key(r) for _, _, r in out]
        self.assertEqual(keys, sorted(keys))
        # every core gives the same order as one
        par, _ = tft.enumerate_builds(SNAP, u, 2, "clump", [], DUMMY, pool)
        self.assertEqual([c for c, _, _ in par], [c for c, _, _ in out])

    def test_unique_items_appear_once(self):
        pool = tft.pool_items(SNAP, ITEM_FX)
        unique = [a for a in pool if SNAP.items[a]["unique"]]
        if not unique:
            self.skipTest("no unique item in the pool")
        small = list(dict.fromkeys(unique[:1] + pool[:2]))
        out, count = tft.enumerate_builds(SNAP, SNAP.unit("Ashe"), 2, "clump", [], DUMMY, small, workers=1)
        self.assertTrue(all(c.count(unique[0]) <= 1 for c, _, _ in out))
        self.assertLess(count, 10)


class TestSnapshot(unittest.TestCase):
    def test_benchmark_places_four_frontliners_and_two_backliners(self):
        self.assertEqual((tft.FRONTLINERS, tft.BACKLINERS), (4, 2))
        positions = {
            "clump": [(2, 0), (3, 0), (4, 0), (5, 0), (2, 2), (4, 2)],
            "spread": [(0, 0), (2, 0), (4, 0), (6, 0), (1, 2), (5, 2)],
        }
        for name, threat in (("Ashe", None), ("Leona", "mixed")):
            dummy = tft.dummies_for(SNAP, threat=threat)
            self.assertEqual(dummy["count"], 6)
            for geometry, expected in positions.items():
                with self.subTest(unit=name, geometry=geometry):
                    spec = tft.cell_spec(SNAP, SNAP.unit(name), 2, geometry, [], dummy,
                                         duration=0.1, item_fx=ITEM_FX, trait_fx=TRAIT_FX)
                    slots = spec["dummies"]["slots"]
                    self.assertEqual([(s["lane"], s["row"]) for s in slots], expected)
                    self.assertEqual([s["line"] for s in slots],
                                     ["frontline"] * 4 + ["backline"] * 2)
                    _, result = ENGINE.simulate(spec, False)
                    self.assertEqual(len(result["left"]), 6)
                    other = "spread" if geometry == "clump" else "clump"
                    tft.set_geometry(spec, other)
                    self.assertEqual([(s["lane"], s["row"]) for s in slots], positions[other])

    def test_dummies_from_the_set(self):
        self.assertEqual(DUMMY["count"], tft.FRONTLINERS + tft.BACKLINERS)
        self.assertEqual([s["kind"] for s in DUMMY["slots"]],
                         ["tank"] * tft.FRONTLINERS + ["non-tank"] * tft.BACKLINERS)
        self.assertEqual([s["line"] for s in DUMMY["slots"]],
                         ["frontline"] * tft.FRONTLINERS + ["backline"] * tft.BACKLINERS)
        self.assertGreater(DUMMY["tanks"], 10)
        self.assertGreater(DUMMY["tank"]["hp"], DUMMY["other"]["hp"])
        self.assertGreater(DUMMY["tank"]["armor"], DUMMY["other"]["armor"])

    def test_only_first_dummy_has_fixed_defenses(self):
        before = {api: dict(unit["stats"]) for api, unit in SNAP.units.items()}
        dummy = tft.dummies_for(SNAP)
        defenses = lambda slot: (slot["hp"], slot["armor"], slot["mr"])
        self.assertEqual([defenses(slot) for slot in dummy["slots"]],
                         [(3000, 110, 110), (1800, 45, 45), (1800, 45, 45),
                          (1800, 45, 45),
                          (1440, 40, 40), (1440, 40, 40)])
        self.assertEqual(dummy["totalHp"], 11280)
        self.assertEqual(defenses(dummy["tank"]), (1800, 45, 45))
        self.assertEqual([slot["nearby"] for slot in dummy["slots"]],
                         [True] * 4 + [False] * 2)
        # the backline is a damage target only: it never attacks
        self.assertEqual([slot["streams"] for slot in dummy["slots"]],
                         [1] * 4 + [0] * 2)
        extra = ("nearby", "line", "streams")
        self.assertEqual({k: v for k, v in dummy["slots"][1].items() if k not in extra},
                         dummy["tank"])
        self.assertEqual({k: v for k, v in dummy["slots"][-1].items() if k not in extra},
                         dummy["other"])
        for key in ("kind", "ad", "as", "ability", "physicalShare", "manaMax",
                    "manaStart", "manaPerAttack", "manaFromDamage"):
            self.assertEqual(dummy["slots"][0][key], dummy["tank"][key], key)
        self.assertEqual({api: unit["stats"] for api, unit in SNAP.units.items()}, before)

    def test_enemy_defenses_are_consistent_across_all_scenarios(self):
        # One formation for every objective: the damage board and the tank
        # board differ in who attacks, not in who is standing there.
        formation = [(3000, 110, 110), (1800, 45, 45), (1800, 45, 45),
                     (1800, 45, 45),
                     (1440, 40, 40), (1440, 40, 40)]
        count = 0
        for unit in tft.modeled_units(SNAP):
            contexts, _ = tft.unit_trait_contexts(SNAP, unit, TRAIT_FX)
            for scenario in tft.unit_scenarios(unit).values():
                with self.subTest(unit=unit["name"], scenario=scenario["key"]):
                    dummy = tft.dummies_for(SNAP, threat=scenario["threat"]
                                            if unit["objective"] == "tank" else None)
                    spec = tft.cell_spec(SNAP, unit, scenario["star"], scenario["geometry"],
                                         contexts[scenario["traits"]], dummy,
                                         item_fx=ITEM_FX, trait_fx=TRAIT_FX)
                    self.assertEqual([(slot["hp"], slot["armor"], slot["mr"])
                                      for slot in spec["dummies"]["slots"]],
                                     formation)
                    count += 1
        self.assertEqual(count, 1770)

    def test_overrides_are_current_with_the_patch_notes(self):
        snapshots = {snap.patch: snap for snap in (SNAP, tft.load_snapshot())}
        for patch, snap in snapshots.items():
            with self.subTest(patch=patch):
                findings, unmatched = tft.check_patch_notes(snap)
                stale = [f for f in findings if f["status"] != "current"]
                self.assertEqual(stale, [])
                self.assertGreater(len(findings), 5)

    def test_cast_times_from_bins(self):
        self.assertAlmostEqual(SNAP.unit("Ashe")["castTime"], 0.25)

    def test_meta_shape(self):
        meta = tft.api_meta()
        self.assertEqual(meta["set"], 18)
        self.assertEqual(len(meta["scenarios"]), 54)   # base scenarios × three tank threats
        self.assertEqual(len(meta["items"]), 35)
        self.assertGreaterEqual(len(meta["units"]), 20)
        self.assertEqual(set(meta["objectives"]), {"carry", "fighter", "tank"})
        for u in meta["units"]:
            self.assertIn(u["objective"], meta["objectives"])
            self.assertTrue(u["role"])

    def test_cells_are_hashed_per_unit_and_scenario(self):
        paths = tft.cell_paths(SNAP)
        self.assertEqual(len(paths), sum(len(tft.unit_scenarios(u)) for u in tft.modeled_units(SNAP)))
        self.assertEqual(len(set(paths.values())), len(paths))
        self.assertTrue(all(p.startswith(tft.CACHE_DIR) for p in paths.values()))

    def test_engine_is_current(self):
        # the compiled engine matches tft_engine/ on disk (else: jobs/build-engine.sh tft)
        self.assertFalse(tft.source_stale(), "tft/sim.py or tft/engine/ changed since the build")
        self.assertEqual(tft.engine_source_hash(), ENGINE.SOURCE_HASH)


class TestAuditFixes(unittest.TestCase):
    """Pins from the 2026-09-05 damage-math audit."""

    def test_curve_overrides_reach_the_calcs(self):
        # overrides.json: Soraka DamageAP 190/285 -> 225/335; the calc's own
        # coefficient list used to win over the corrected row
        u = SNAP.unit("Soraka")
        kit = {"name": u["name"], "calcs": u["calcs"], "curve": u["curve"]}
        self.assertAlmostEqual(tft.calc_value(kit, "MagicDamageCalc1", 2, 45.0, 100.0, 0, 0, 0, {}, 45.0), 335.0)
        self.assertAlmostEqual(tft.calc_value(kit, "MagicDamageCalc1", 3, 67.5, 100.0, 0, 0, 0, {}, 67.5), 1000.0)

    def test_trait_curve_override_is_by_breakpoint_column(self):
        row = [[0, 0], [3, 0.06], [4, 0.06]]
        self.assertEqual(tft.override_trait_curve(row, {"3": 0.05}), [[0, 0], [3, 0.05], [4, 0.06]])

    def test_riftbeast_capstone_stats_at_high(self):
        u = SNAP.unit("Murkwolf")
        ctx, _ = tft.unit_trait_contexts(SNAP, u, TRAIT_FX)
        high = fx_of("Murkwolf", traits=ctx["high"])
        self.assertAlmostEqual(high["adPct"], 0.05)     # 6% in the snapshot, 5% per the patch notes
        self.assertAlmostEqual(high["asPct"], 0.05)
        self.assertAlmostEqual(high["hp"], 50.0)
        low = fx_of("Murkwolf", traits=ctx["low"])
        self.assertAlmostEqual(low["asPct"], 0.0)       # a 0 multiplier row is not -100% attack speed

    def test_stat_line_formats_are_case_insensitive(self):
        item = {"statLine": '<TFTCurveTable row="AS" icon="icon.AS" format="PercentMinusOne" type="stat"/>',
                "curve": {"AS": [[1, 1.2]]}}
        self.assertAlmostEqual(tft.parse_stat_line(item)["asPct"], 0.2)

    def test_starting_mana_is_capped_at_the_bar(self):
        # Soraka 0/30 with 60 starting mana: the bar holds 30, the first
        # attack adds 7 and casts at 37, not 67 chain-casting through the lock
        _, res = run("Soraka", fx=[{"startingMana": 60.0}], dummy=immortal(DUMMY))
        casts = events(res, "cast")
        self.assertAlmostEqual(casts[0][0], 0.0)
        self.assertAlmostEqual(casts[0][2], 37.0)
        self.assertGreater(casts[1][0], 1.0)

    def test_a_cast_lands_after_its_animation(self):
        _, res = run("Soraka", dummy=immortal(DUMMY))
        cast = events(res, "cast")[0]
        land = events(res, "land")[0]
        self.assertAlmostEqual(land[0], cast[0] + tft.CAST_TIME_DEFAULT)
        first = events(res, "damage", "ability")[0]
        self.assertAlmostEqual(first[0], land[0])      # nothing before the landing

    def test_a_channel_locks_through_itself_and_the_second_after(self):
        # Aphelios with a full bar casts at 0 s: a two-second onslaught, then the second
        _, res = run("Aphelios", fx=[{"startingMana": 100.0}], dummy=immortal(DUMMY), duration=0.1)
        self.assertEqual(res["casts"], 1)
        self.assertAlmostEqual(res["probe"]["castingUntil"], 2.0)
        self.assertAlmostEqual(res["probe"]["lockUntil"], 3.0)

    def test_aphelios_onslaught_takes_its_two_seconds(self):
        # 3★ with a full bar from two Protector's Vows: the swipes and the
        # blast used to land at t=0 (a kill at 0.0 s and a DPS of 5e12).
        # Keep the targets alive so timing is independent of their health
        # and of whether this build kills them within its first channel.
        _, res = run("Aphelios", items=("Deathblade", "Protector's Vow", "Protector's Vow"),
                     star=3, geometry="spread", dummy=immortal(DUMMY), duration=2.0)
        self.assertAlmostEqual(events(res, "cast")[0][0], 0.0)
        self.assertAlmostEqual(events(res, "damage", "blast")[0][0], 2.0)
        swipe_times = [event[0] for event in events(res, "damage", "swipes")]
        self.assertGreater(min(swipe_times), 0.0)
        self.assertGreater(len(set(swipe_times)), 1)
        self.assertLessEqual(max(swipe_times), 2.0)
        self.assertAlmostEqual(res["dps"], res["total"] / 2.0)

    def test_second_copies_stack(self):
        # Titan's: one stack per attack shared by both copies, each copy's share
        _, res = run("Ashe", driver="Driver", dummy=immortal(DUMMY),
                     fx=[{"adapPerAttack": [0.02, 25, 0.1]}, {"adapPerAttack": [0.02, 25, 0.1]}])
        autos = events(res, "damage", "auto")
        self.assertAlmostEqual(autos[10][2] / autos[0][2], (1 + 0.04 * 11) / (1 + 0.04 * 1))
        self.assertEqual(res["probe"]["adapStackN"], min(res["attacks"], 25))
        # Hand of Justice: both copies count
        s, _ = run("Warwick", driver="Driver", fx=[{"hoj": [0.15, 15.0, 0.12, 0.5]}] * 2)
        self.assertAlmostEqual(s["ad"], 60 * (1 + 0.6))
        # Striker's Flail: both copies apply to the same four-stack cap.
        _, res = run("Ashe", driver="Driver", dummy=immortal(DUMMY),
                     fx=[{"ampPerCrit": [0.05, 5.0, 4]}] * 2 + [{"stats": [["crit", 0.75]]}])
        autos = events(res, "damage", "auto")
        # The first hit has no stacks. Copies fill the shared cap faster,
        # but four stacks still grant only 20% amp.
        self.assertAlmostEqual(autos[4][2] / autos[0][2], 1.2)

    def test_ability_dots_crit_with_precision(self):
        _, with_p = run("Cassiopeia", fx=[{"stats": [["crit", 0.75]], "precision": 1}], duration=6.0)
        _, without = run("Cassiopeia", fx=[{"stats": [["crit", 0.75]]}], duration=6.0)
        self.assertAlmostEqual(with_p["breakdown"]["poison"] / without["breakdown"]["poison"], 1.4, places=6)

    def test_a_dot_pays_for_the_time_elapsed_only(self):
        # at 0.9 attacks/s Cassiopeia's bar fills on the attack at 3.33 s, the
        # poison lands at 3.58 s and the tick at 3.75 s pays for a sixth of a
        # second, the next one for a full quarter
        _, res = run("Cassiopeia", fx=[{"stats": [["asPct", 0.2]]}], dummy=immortal(DUMMY))
        land = events(res, "land")[0][0]
        ticks = events(res, "damage", "poison", target=0)
        span = ticks[0][0] - land
        self.assertLess(span, tft.TICK_S - 1e-9)
        self.assertAlmostEqual(ticks[0][2] / ticks[1][2], span / tft.TICK_S)


class TestCells(unittest.TestCase):
    def test_star_levels_by_cost(self):
        # 1★ and 2★ for everyone, 3★ only for the 1–3 costs
        for u in SNAP.units.values():
            self.assertEqual(tft.unit_stars(u), (1, 2, 3) if u["cost"] <= 3 else (1, 2), u["name"])
        self.assertEqual(len(tft.unit_scenarios(SNAP.unit("Karma"))), 18)
        self.assertEqual(len(tft.unit_scenarios(SNAP.unit("Soraka"))), 12)
        self.assertFalse(any(k.startswith("s3-") for k in tft.unit_scenarios(SNAP.unit("Soraka"))))
        keys = {key for _, key in tft.cells(SNAP)}
        self.assertEqual(keys, set(tft.SCENARIOS))


GOLDEN_DIR = os.path.join(tft.TFT_DATA_DIR, "golden")


def same_number(a, b):
    """Bit-identical, with an int and a float of equal value counting as
    the same number and the non-finite strings decoded."""
    dec = {"inf": float("inf"), "-inf": float("-inf")}
    if isinstance(a, str):
        a = dec.get(a, a)
    if isinstance(b, str):
        b = dec.get(b, b)
    if a == "nan" or b == "nan":
        return a == b or (isinstance(a, float) and a != a) or (isinstance(b, float) and b != b)
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b and type(a) is type(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_number(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(same_number(a[k], b[k]) for k in a)
    return a == b


class TestGolden(unittest.TestCase):
    """Replay the deliberately updated compiled-engine benchmark bit for
    bit: every golden fight and a sample of ranked cells (every cell with
    TFT_GOLDEN_ALL=1). The fixtures retain their historical provenance."""

    def test_benchmark_dummy_is_explicit_and_current(self):
        for name in ("fights.json", "cells.json"):
            with self.subTest(fixture=name):
                with open(os.path.join(GOLDEN_DIR, name)) as f:
                    provenance = json.load(f)["provenance"]
                snap = tft.load_snapshot(provenance["set"], provenance["patch"])
                self.assertEqual(provenance["dummy"], tft.dummies_for(snap))
                self.assertEqual(provenance["tankDummies"],
                                 {key: tft.dummies_for(snap, threat=key) for key in tft.TANK_THREATS})

    def test_fights(self):
        with open(os.path.join(GOLDEN_DIR, "fights.json")) as f:
            fixture = json.load(f)
        provenance = fixture["provenance"]
        snap = tft.load_snapshot(provenance["set"], provenance["patch"])
        dummy = provenance.get("dummy", tft.dummies_for(snap))
        cases = fixture["cases"]
        self.assertGreater(len(cases), 4000)
        bad = []
        for case in cases:
            unit = snap.units[case["unit"]]
            case_dummy = provenance.get("tankDummies", {}).get(case.get("threat"), dummy)
            sheet, res = tft.simulate(snap, unit, case["star"], case["items"], case["geometry"],
                                      [tuple(x) for x in case["ctxTraits"]], case_dummy, None,
                                      ITEM_FX, TRAIT_FX)
            for k, v in case["sheet"].items():
                if not same_number(v, sheet[k]):
                    bad.append((unit["name"], case["scenario"], case["items"], k, v, sheet[k]))
                    break
            else:
                for k, v in case["result"].items():
                    if not same_number(v, res[k]):
                        bad.append((unit["name"], case["scenario"], case["items"], k, v, res[k]))
                        break
        self.assertEqual(bad[:5], [], f"{len(bad)} of {len(cases)} golden fights differ")

    def test_cells(self):
        with open(os.path.join(GOLDEN_DIR, "cells.json")) as f:
            fixture = json.load(f)
        provenance = fixture["provenance"]
        snap = tft.load_snapshot(provenance["set"], provenance["patch"])
        dummy = provenance.get("dummy", tft.dummies_for(snap))
        cells = fixture["cells"]
        keys = sorted(cells)
        if not os.environ.get("TFT_GOLDEN_ALL"):
            keys = keys[::23]
        pool = tft.pool_items(snap, ITEM_FX)
        for key in keys:
            _, sc_key = key.split("/")
            unit = snap.units[cells[key]["unit"]]
            sc = tft.SCENARIOS[sc_key]
            case_dummy = provenance["tankDummies"][sc["threat"]] if unit["objective"] == "tank" else dummy
            contexts, _ = tft.unit_trait_contexts(snap, unit, TRAIT_FX)
            out, count = tft.enumerate_builds(snap, unit, sc["star"], sc["geometry"],
                                              contexts[sc["traits"]], case_dummy, pool, item_fx=ITEM_FX,
                                              trait_fx=TRAIT_FX)
            self.assertEqual(count, cells[key]["buildsEvaluated"], key)
            rows = tft.cell_rows(snap, unit, out, len(cells[key]["rows"]))
            self.assertEqual(rows, cells[key]["rows"], key)
            self.assertEqual(tft.driver_name(unit), cells[key]["driver"])


NOTES = """
<h1>Teamfight Tactics patch 18.1</h1>
<h2>Mid-Patch Updates</h2>
<h3>AUGUST 31ST AND SEPTEMBER 1ST</h3><h4>CHAMPIONS</h4>
<ul><li>Amumu Heal Max HP %: <span>2.2%</span> ⇒ <span>2.5%</span></li></ul>
<h3>AUGUST 28TH</h3><h4>BUG FIXES</h4><ul><li>A performance fix.</li></ul>
<h3>AUGUST 27TH</h3><h4>BUG FIXES</h4><ul><li>Another fix.</li></ul>
<h2>LARGE CHANGES</h2><h4>ADJUSTED ARTIFACTS</h4>
<ul><li>Dawncore:<ul><li>Mana Regen: 2 ⇒ 1</li></ul></li>
<li>Example Health Threshold: 30 ⇒ 35. Reward: 3 items ⇒ 2 items</li></ul>
"""


class TestPatchDocuments(unittest.TestCase):
    def test_hotfix_order(self):
        values = ["18.10", "18.1d", "18.2", "18.1", "18.1b", "17.9"]
        self.assertEqual(sorted(values, key=tft.tft_patch_key),
                         ["17.9", "18.1", "18.1b", "18.1d", "18.2", "18.10"])
        with self.assertRaises(ValueError):
            tft.tft_patch_key("../18.1d")

    def test_dates_and_nested_item_labels_survive(self):
        document = tft.patch_notes_document(NOTES, "18.1")
        self.assertEqual(document["patch"], "18.1d")
        self.assertTrue(document["url"].endswith("patch-18-1/"))
        changes = {c["what"]: c for c in document["changes"]}
        self.assertEqual(changes["Amumu Heal Max HP %"]["update"], "AUGUST 31ST AND SEPTEMBER 1ST")
        self.assertEqual(changes["Amumu Heal Max HP %"]["new"], "2.5%")
        self.assertEqual(changes["Dawncore Mana Regen"]["new"], "1")
        self.assertEqual(changes["Example Health Threshold"]["new"], "35")
        self.assertEqual(changes["Reward"]["old"], "3 items")

    def test_explicit_revision_wins_over_section_count(self):
        document = tft.patch_notes_document(NOTES.replace("AUGUST 28TH", "18.1e UPDATE"), "18.1")
        self.assertEqual(document["patch"], "18.1e")

    def test_unreadable_patch_notes_fail(self):
        with self.assertRaisesRegex(ValueError, "no readable balance"):
            tft.patch_notes_document("<h1>Temporarily unavailable</h1>", "18.1")

    def test_nonnumeric_gameplay_changes_invalidate_review(self):
        before = tft.patch_notes_document(NOTES, "18.1")
        changed = NOTES.replace("A performance fix.", "Akali now casts twice against her current target.")
        after = tft.patch_notes_document(changed, "18.1")
        self.assertEqual(before["changes"], after["changes"])
        self.assertNotEqual(tft.json_hash(before), tft.json_hash(after))

    def test_rotating_article_recommendations_do_not_invalidate_review(self):
        before = tft.patch_notes_document(NOTES, "18.1")
        footer = "<h2>Related Articles</h2><ul><li>A different recommended article</li></ul>"
        after = tft.patch_notes_document(NOTES + footer, "18.1")
        self.assertEqual(tft.json_hash(before), tft.json_hash(after))

    def test_ambiguous_communitydragon_set_fails(self):
        with self.assertRaisesRegex(ValueError, "found 0"):
            tft.communitydragon_set({"setData": []}, 18)
        with self.assertRaisesRegex(ValueError, "found 2"):
            tft.communitydragon_set({"setData": [{"number": 18}, {"number": 18}]}, 18)


class TestLivePatchAudit(unittest.TestCase):
    def setUp(self):
        self.snap = copy.deepcopy(tft.load_snapshot(18, "18.1d"))

    def test_live_patch_targets_pass(self):
        findings, _ = tft.check_patch_notes(self.snap)
        self.assertEqual(len(findings), 65)
        self.assertTrue(all(f["status"] == "current" for f in findings))

    def test_amumu_heal_and_yi_ap_resists_reach_engine_inputs(self):
        amumu = self.snap.unit("Amumu")
        self.assertEqual([tft.curve_at(amumu["curve"]["PassiveHealPercent"], s) for s in (1, 2, 3, 4)],
                         [.025, .025, .04, .04])
        yi = self.snap.unit("Master Yi")
        for form in ("AD", "AP"):
            kit = tft.kit_spec(yi, 2, form)
            self.assertEqual(kit["stats"]["armor"], 55)
            self.assertEqual(kit["stats"]["mr"], 55)

    def test_single_percentage_and_form_regressions_are_detected(self):
        self.snap.unit("Amumu")["curve"]["PassiveHealPercent"] = [[1, .022], [3, .04]]
        self.snap.unit("Master Yi")["forms"]["AP"]["stats"]["armor"] = 60
        findings, _ = tft.check_patch_notes(self.snap)
        bad = [f for f in findings if f["status"] != "current"]
        self.assertEqual(len(bad), 2)
        self.assertEqual({f["unit"] for f in bad}, {"Amumu", "Master Yi"})

    def test_item_regression_is_detected(self):
        target = next(c["target"] for c in self.snap.audit["checks"]
                      if c["target"]["kind"] == "item")
        self.snap.items[target["api"]]["curve"][target["row"]] = [[1, -999]]
        findings, _ = tft.check_patch_notes(self.snap)
        self.assertTrue(any(f["api"] == target["api"] and f["status"] != "current" for f in findings))

    def test_changed_lookup_requires_a_new_review(self):
        self.snap.raw["_metadata"]["coreHash"] = "new-upstream-data"
        findings, _ = tft.check_patch_notes(self.snap)
        self.assertTrue(any(f["where"] == "audit lookupHash" for f in findings))

    def test_new_patch_note_cannot_reuse_old_review(self):
        notes = json.loads((Path(self.snap.dir) / "patchnotes.json").read_text())
        notes["changes"][0]["new"] = "99% AD/AP/AS"
        findings, _ = tft.check_audit(self.snap, notes)
        self.assertTrue(any(f["where"] == "audit patchNotesHash" for f in findings))

    def test_failed_review_leaves_active_snapshot_unchanged(self):
        raw = copy.deepcopy(self.snap.raw)
        source = {"url": "https://example.invalid/data", "lastModified": None, "sha256": "test"}
        cdragon = {"setData": [{"number": 18, "champions": [], "traits": [], "items": []}]}
        original = Path(tft.TFT_DATA_DIR) / "set18" / "18.1"
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "set18" / "18.1"
            shutil.copytree(original, active)
            before = (active / "metatft.json").read_bytes()
            with patch.object(tft, "TFT_DATA_DIR", directory), \
                 patch.object(tft, "fetch_bytes", return_value=NOTES.encode()), \
                 patch.object(tft, "fetch_source", side_effect=[(raw, source), (cdragon, source)]), \
                 patch.object(tft, "fetch_json", return_value={}):
                with self.assertRaisesRegex(ValueError, "needs a patch audit"):
                    tft.cmd_fetch(SimpleNamespace(set=18, patch="18.1d", force=True))
                self.assertEqual(tft.patch_dirs(18), ["18.1"])
            self.assertEqual((active / "metatft.json").read_bytes(), before)
            self.assertTrue((Path(directory) / "set18" / ".pending" / "18.1d" / "metatft.json").exists())

    def test_missing_timings_cannot_replace_an_audited_patch(self):
        raw = copy.deepcopy(self.snap.raw)
        source = {"url": "https://example.invalid/data", "lastModified": None, "sha256": "test"}
        cdragon = {"setData": [{"number": 18, "champions": [], "traits": [], "items": []}]}
        notes = json.loads((Path(self.snap.dir) / "patchnotes.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "set18" / "18.1d"
            shutil.copytree(self.snap.dir, active)
            before = (active / "bins.json").read_bytes()
            with patch.object(tft, "TFT_DATA_DIR", directory), \
                 patch.object(tft, "fetch_bytes", return_value=b"notes"), \
                 patch.object(tft, "patch_notes_document", return_value=notes), \
                 patch.object(tft, "fetch_source", side_effect=[(raw, source), (cdragon, source)]), \
                 patch.object(tft, "fetch_json", side_effect=HTTPError("https://example.invalid", 404, "Not Found", {}, None)):
                with self.assertRaisesRegex(ValueError, "needs a patch audit"):
                    tft.cmd_fetch(SimpleNamespace(set=18, patch="18.1d", force=True))
            self.assertEqual((active / "bins.json").read_bytes(), before)


TIMING = tft.load_cast_timing(SNAP.set_no)
TICK = tft.TICK_S
# One regen tick: TFTraits' regen is continuous, the engine's lands on the
# quarter second, so a bar that regen fills is seen up to a tick late.
TOLERANCE = TICK + 0.01


def form_entry(unit, form):
    """The resolved timeline of `unit`'s form (None: a unit without forms)."""
    forms = tft.cast_timing_spec(unit, TIMING) or {}
    return forms.get(form or "base") or forms.get("base") or {}


def timed(name, *, fx=(), duration=30.0, **kw):
    """A production spec (cast timelines on) against dummies that neither
    die nor hit back, so only attacks and regen move the bar."""
    spec = spec_for(name, fx=fx, duration=duration, dummy=immortal(DUMMY), pressure=False,
                    timed=True, **kw)
    return spec


def scale(sheet, unit_spec_base_as):
    return unit_spec_base_as / sheet["as"]


REGEN = {"stats": [["manaRegen", 8.0]]}   # a tick inside a window would show on the bar


class TestTimingFile(unittest.TestCase):
    def test_every_modeled_unit_is_covered(self):
        snap = tft.load_snapshot()
        units = TIMING["units"]
        self.assertEqual(set(units), {u["api"] for u in tft.modeled_units(snap)})
        self.assertEqual(TIMING["retrieved"], "2026-09-20")
        for api, entry in units.items():
            with self.subTest(unit=api):
                self.assertTrue(entry["source"].startswith("https://tftraits.com/champions/"))
                unit = snap.units[api]
                self.assertLessEqual(set(entry) - {"name", "source", "base"}, set(unit["forms"]))

    def test_every_unit_with_a_bar_has_a_cast_window(self):
        # The engine's manaless kits (their drivers zero the bar) are the
        # only units TFTraits gives no cast animation for.
        snap = tft.load_snapshot()
        dummy = immortal(dict(tft.dummies_for(snap), targetDebuffs={}))
        for unit in tft.modeled_units(snap):
            with self.subTest(unit=unit["name"]):
                spec = tft.cell_spec(snap, unit, 2, "clump", [], dummy, 1.0, False, ITEM_FX, TRAIT_FX)
                sheet, _ = ENGINE.simulate(spec, False)
                entry = form_entry(unit, sheet["form"])
                base = TIMING["units"][unit["api"]]["base"]
                if base.get("noMana") or base.get("noCastAnimation"):
                    self.assertEqual(sheet["manaMax"], 0.0)
                    self.assertNotIn("castAnimation", entry)
                elif unit["name"] != "Gnar":   # his bar only appears with Mega Gnar
                    self.assertGreater(sheet["manaMax"], 0.0)
                    self.assertGreater(entry["castAnimation"], 0.0)

    def test_numbers_are_plausible(self):
        snap = tft.load_snapshot()
        for api, entry in TIMING["units"].items():
            for form in ("base", "AD", "AP"):
                values = entry.get(form) or {}
                with self.subTest(unit=api, form=form):
                    for key in ("castAnimation", "channel", "effectAt", "manaLock",
                                "attackDelay", "attackRecovery", "firstCast"):
                        if key in values:
                            self.assertGreaterEqual(values[key], 0.0)
                            self.assertLess(values[key], 60.0)
                    if "attackDelay" in values:
                        # delay and recovery are parts of one attack animation
                        period = 1.0 / (values.get("assumed") or {}).get(
                            "attackSpeed", snap.units[api]["stats"]["as"])
                        self.assertLess(values["attackDelay"] + values["attackRecovery"], period)
                    if values.get("lockRule") in ("castAnimation", "channel"):
                        self.assertAlmostEqual(values["manaLock"],
                                               values["castAnimation"] + values.get("channel", 0.0))

    def test_forms_take_what_they_lack_from_the_base_entry(self):
        # `base` is the form the data lists first (the engine falls back to it
        # for the form without an entry of its own), the other key the
        # Adaptor's second form. TFTraits repeats only Akali's attack split
        # for hers, so it takes the cast window from the base entry.
        akali = tft.cast_timing_spec(SNAP.unit("Akali"), TIMING)
        self.assertEqual(sorted(akali), ["AP", "base"])
        self.assertEqual(akali["AP"], akali["base"])
        self.assertEqual(akali["AP"]["castAnimation"], 0.8)
        nidalee = tft.cast_timing_spec(SNAP.unit("Nidalee"), TIMING)
        self.assertEqual((nidalee["base"]["castAnimation"], nidalee["base"]["attackDelay"]), (1.02, 0.21))
        self.assertEqual((nidalee["AD"]["castAnimation"], nidalee["AD"]["effectAt"],
                          nidalee["AD"]["attackDelay"]), (1.0, 0.37, 0.26))
        self.assertNotIn("effectAt", nidalee["base"])
        self.assertNotIn("manaLock", nidalee["base"])    # three javelins: her driver's lock
        self.assertEqual(nidalee["AD"]["manaLock"], 1.0)
        yi = tft.cast_timing_spec(SNAP.unit("Master Yi"), TIMING)
        self.assertEqual(yi["base"], {"attackDelay": 0.71, "attackRecovery": 0.04})
        self.assertEqual(yi["AP"], {"attackDelay": 0.66, "attackRecovery": 0.04})
        self.assertIsNone(tft.cast_timing_spec(SNAP.unit("Kayle"), TIMING))
        self.assertIsNone(tft.cast_timing_spec(SNAP.unit("Soraka"), {}))

    def test_only_fixed_lock_rules_reach_the_engine(self):
        by_rule = {}
        for unit in tft.modeled_units(SNAP):
            base = TIMING["units"][unit["api"]]["base"]
            spec = (tft.cast_timing_spec(unit, TIMING) or {}).get("base") or {}
            by_rule.setdefault(base.get("lockRule"), set()).add("manaLock" in spec)
        self.assertEqual(by_rule["castAnimation"], {True})
        self.assertEqual(by_rule["channel"], {True})
        self.assertEqual(by_rule["untilEffectEnds"], {True})
        # these follow the fight: the unit's driver holds the bar, not the engine
        for rule in ("effectDuration", "shieldHolds", "empoweredAttacks", "none"):
            self.assertEqual(by_rule[rule], {False}, rule)

    def test_the_file_is_part_of_every_cache_key(self):
        # an edit moves every cell (and, through snapshot_revision, the
        # composition and site revisions): the hash reads the file's bytes
        paths = tft.cell_paths(SNAP)
        with tempfile.TemporaryDirectory() as directory:
            for name in ("item-effects.json", "trait-effects.json", "kits.json", "cast-timing.json"):
                with open(os.path.join(tft.set_dir(SNAP.set_no), name), "rb") as src, \
                        open(os.path.join(directory, name), "wb") as dst:
                    dst.write(src.read())
            with patch.object(tft, "set_dir", return_value=directory):
                same, revision = tft.cell_paths(SNAP), tft.snapshot_revision(SNAP)
                with open(os.path.join(directory, "cast-timing.json"), "ab") as f:
                    f.write(b"\n")
                moved, moved_revision = tft.cell_paths(SNAP), tft.snapshot_revision(SNAP)
        self.assertEqual({os.path.basename(p) for p in same.values()},
                         {os.path.basename(p) for p in paths.values()})
        self.assertTrue(all(os.path.basename(same[key]) != os.path.basename(moved[key]) for key in same))
        self.assertNotEqual(revision, moved_revision)


def tftraits_first_cast(mana, per_attack, regen, attack_speed, delay, recovery, start=0.0):
    """TFTraits' rule with its continuous regen: the attack that fills the
    bar casts at its unlock point; a bar regen fills between attacks casts
    there and then. None: nothing ever fills the bar."""
    if start >= mana:
        return 0.0
    if per_attack <= 0 and regen <= 0:
        return None
    k = 0
    while True:
        landing = delay + k / attack_speed
        before = start + per_attack * k + regen * landing
        if before >= mana:
            return (mana - start - per_attack * k) / regen
        if before + per_attack >= mana:
            return landing + recovery
        k += 1


class TestFirstCast(unittest.TestCase):
    """The acceptance table: an itemless 2-star unit's first cast START
    against the one TFTraits publishes. TFTraits states each figure "with
    0.75 attacks per second", base attack speed, standing still, so the
    fighters' role attack speed and the melee walk are taken out of the spec;
    dummies do not hit back (a tank's bar fills from attacks alone, as in
    TFTraits' figure)."""

    # Units whose first cast is not the plain "fill the bar by attacking".
    EXPLAINED = {
        "TFT18_Gnar": "the engine gives Gnar a Rage bar first and a mana bar only as Mega Gnar; "
                      "TFTraits times a plain 70-mana bar",
    }
    # Units that start at 0 mana and still cannot be held against TFTraits'
    # own figure, because TFTraits (live data) timed another bar than the
    # archived snapshot has: they are held against the rule instead. Anyone
    # else landing here is a finding, not a skip.
    OTHER_BAR = {
        "TFT18_Nidalee": "TFTraits times the live 45-mana bar; the archived snapshots still have 40",
        "TFT18_Akali": "18.1d has a 30-mana bar, 18.2 and TFTraits 25",
    }

    @staticmethod
    def measure(snap, unit):
        dummy = immortal(dict(tft.dummies_for(snap), targetDebuffs={}, meleeRepositionSeconds=0.0))
        spec = tft.cell_spec(snap, unit, 2, "clump", [], dummy, 80.0, False, ITEM_FX, TRAIT_FX)
        spec["role"]["asPct"] = 0.0
        sheet, res = ENGINE.simulate(spec, False)
        kit = spec["kits"][sheet["form"] or "base"]["stats"]
        engine = {"mana": kit["mana"], "manaStart": kit["initialMana"],
                  "manaPerAttack": tft.ROLE_MANA.get(tft.unit_role_kind(unit), 0),
                  "manaRegen": spec["role"]["manaRegen"], "attackSpeed": kit["as"]}
        return sheet, res, engine

    def rows(self, snap):
        out = []
        for unit in tft.modeled_units(snap):
            base = TIMING["units"][unit["api"]]["base"]
            sheet, res, engine = self.measure(snap, unit)
            entry = form_entry(unit, sheet["form"])
            if "castAnimation" not in entry:
                continue
            own = TIMING["units"][unit["api"]].get(sheet["form"]) or {}
            published = own if "firstCast" in own and "castAnimation" in own else base
            first = res["castTimes"][0] if res["castTimes"] else None
            rule = tftraits_first_cast(engine["mana"], engine["manaPerAttack"], engine["manaRegen"],
                                       engine["attackSpeed"], entry.get("attackDelay", 0.0),
                                       entry.get("attackRecovery", 0.0), engine["manaStart"])
            out.append({"unit": unit, "form": sheet["form"], "engine": first, "rule": rule,
                        "tftraits": published.get("firstCast"), "assumed": published.get("assumed"),
                        "stats": engine, "attackSplit": "attackDelay" in entry})
        return out

    def test_the_rule_reproduces_tftraits_own_figures(self):
        # no engine here: the reading of TFTraits' rule against its numbers
        checked = 0
        for api, entry in TIMING["units"].items():
            for form in ("base", "AD", "AP"):
                values = entry.get(form) or {}
                if "assumed" not in values or "attackDelay" not in values:
                    continue
                a = values["assumed"]
                with self.subTest(unit=api, form=form):
                    rule = tftraits_first_cast(a["mana"], a["manaPerAttack"], a["manaRegen"], a["attackSpeed"],
                                               values["attackDelay"], values["attackRecovery"], a["manaStart"])
                    # the page rounds delay, recovery and the result to 0.01 s
                    self.assertAlmostEqual(rule, values["firstCast"], delta=0.035)
                    checked += 1
        self.assertGreater(checked, 55)

    def test_engine_first_cast_matches(self):
        for snap in (SNAP, tft.load_snapshot()):
            rows = self.rows(snap)
            self.assertGreater(len(rows), 55)
            compared, not_compared = 0, set()
            for row in rows:
                unit = row["unit"]
                with self.subTest(patch=snap.patch, unit=unit["name"], form=row["form"]):
                    if unit["api"] in self.EXPLAINED:
                        continue
                    self.assertIsNotNone(row["engine"])
                    # the engine against the rule, at the snapshot's own numbers
                    self.assertAlmostEqual(row["engine"], row["rule"], delta=TOLERANCE)
                    self.assertGreaterEqual(row["engine"], row["rule"] - 0.011)
                    # The acceptance: a unit that starts at 0 mana, as TFTraits
                    # times every unit, against TFTraits' own figure, wherever
                    # it assumed the bar, mana per attack, regen and attack
                    # speed the snapshot has.
                    if row["stats"]["manaStart"] != 0:
                        continue
                    if row["assumed"] == row["stats"] and row["attackSplit"]:
                        self.assertAlmostEqual(row["engine"], row["tftraits"], delta=TOLERANCE)
                        compared += 1
                    else:
                        not_compared.add(unit["api"])
            self.assertLessEqual(not_compared, set(self.OTHER_BAR), snap.patch)
            self.assertGreaterEqual(compared, 27)


class TestCastWindow(unittest.TestCase):
    def window(self, name, **kw):
        unit = SNAP.unit(name)
        spec = timed(name, fx=[REGEN], **kw)
        sheet, res = ENGINE.simulate(spec, True)
        entry = form_entry(unit, sheet["form"])
        busy = entry["castAnimation"] + entry.get("channel", 0.0)
        base_as = spec["kits"][sheet["form"] or "base"]["stats"]["as"]
        return spec, sheet, res, entry, busy, base_as

    def test_no_attack_lands_and_no_mana_arrives_inside_the_window(self):
        for name in ("Soraka", "Alune", "Kennen", "Karma", "Ezreal", "Camille"):
            with self.subTest(unit=name):
                spec, sheet, res, entry, busy, base_as = self.window(name)
                casts = [e for e in events(res, "cast")]
                self.assertGreaterEqual(len(casts), 2)
                attacks = [e[0] for e in events(res, "attack")]
                for start, _, bar, *_ in casts:
                    inside = [t for t in attacks if start < t < start + busy - 1e-9]
                    self.assertEqual(inside, [], f"attack inside the window opened at {start}")
                # the bar just before the first window closes is the cast's
                # overflow: every regen tick in between paid nothing
                start, _, bar, *_ = casts[0]
                cost = spec["kits"][sheet["form"] or "base"]["stats"]["mana"]
                late = copy.deepcopy(spec)
                late["duration"] = start + busy - 0.001
                _, cut = ENGINE.simulate(late, True)
                self.assertEqual(len(events(cut, "cast")), 1)
                self.assertAlmostEqual(cut["probe"]["mana"], bar - cost)
                self.assertAlmostEqual(cut["probe"]["lockUntil"], start + busy)
                self.assertAlmostEqual(cut["probe"]["castingUntil"], start + busy)
                # and the first tick after it pays only for the time outside
                # (plus whatever the fresh attack brought, if it landed first)
                after = copy.deepcopy(spec)
                tick = (int((start + busy) / TICK) + 1) * TICK
                after["duration"] = tick + 0.001
                _, paid = ENGINE.simulate(after, True)
                self.assertEqual(len(events(paid, "cast")), 1)
                landed = sum(1 for e in events(paid, "attack") if e[0] > start + busy)
                self.assertLessEqual(landed, 1)
                regen = 8.0 + spec["role"]["manaRegen"]
                per_attack = tft.ROLE_MANA[tft.unit_role_kind(SNAP.unit(name))]
                self.assertAlmostEqual(paid["probe"]["mana"], bar - cost + regen * (tick - start - busy)
                                       + per_attack * landed)

    def test_karmas_channel_is_part_of_the_window(self):
        _, _, res, entry, busy, _ = self.window("Karma")
        self.assertEqual((entry["castAnimation"], entry["channel"]), (0.6, 1.5))
        start = events(res, "cast")[0][0]
        after = [e[0] for e in events(res, "attack") if e[0] > start]
        self.assertAlmostEqual(after[0], start + 2.1 + entry["attackDelay"])

    def test_the_cast_starts_at_the_attacks_unlock_point(self):
        # Soraka, as TFTraits walks through her: the fourth attack lands at
        # 4.21 s and fills the bar, the cast starts 0.32 s later
        spec = timed("Soraka")
        sheet, res = ENGINE.simulate(spec, True)
        attacks = [e[0] for e in events(res, "attack")]
        self.assertAlmostEqual(attacks[0], 0.21)
        self.assertAlmostEqual(attacks[3], 0.21 + 3 / 0.75)
        self.assertAlmostEqual(events(res, "cast")[0][0], attacks[3] + 0.32)
        self.assertAlmostEqual(res["castTimes"][0], 4.53, places=2)
        # without an effect time the ability lands after the bin's cast time
        self.assertAlmostEqual(events(res, "land")[0][0], res["castTimes"][0] + 0.25)

    def test_attack_delay_and_recovery_scale_with_attack_speed(self):
        spec = timed("Soraka", fx=[{"stats": [["asPct", 1.0]]}])
        sheet, res = ENGINE.simulate(spec, True)
        self.assertAlmostEqual(sheet["as"], 1.5)
        attacks = [e[0] for e in events(res, "attack")]
        self.assertAlmostEqual(attacks[0], 0.21 / 2)
        cast = events(res, "cast")[0][0]
        trigger = max(t for t in attacks if t <= cast)
        self.assertAlmostEqual(cast, trigger + 0.32 / 2)
        # the window does not scale
        nxt = min(t for t in attacks if t > cast)
        self.assertAlmostEqual(nxt, cast + 1.98 + 0.21 / 2)

    def test_a_tick_or_an_arrival_starts_the_cast_at_once(self):
        # Karma's bar fills on a regen tick: no attack, no recovery to wait for
        _, _, res, _, _, _ = self.window("Karma")
        start = events(res, "cast")[0][0]
        self.assertAlmostEqual(start / TICK, round(start / TICK))
        self.assertNotIn(start, [e[0] for e in events(res, "attack")])
        # a melee unit with a full bar casts when its walk ends
        spec = spec_for("Camille", duration=3.0, pressure=False, timed=True,
                        dummy=dict(immortal(DUMMY), meleeRepositionSeconds=0.4))
        spec["kits"]["base"]["stats"]["initialMana"] = spec["kits"]["base"]["stats"]["mana"]
        _, res = ENGINE.simulate(spec, True)
        self.assertEqual(events(res, "cast")[0][0], 0.4)

    def test_the_ability_lands_where_the_timeline_says(self):
        for name, lands in (("Alune", 0.74), ("Camille", 0.29), ("Kennen", 0.0)):
            with self.subTest(unit=name):
                _, _, res, entry, busy, _ = self.window(name)
                self.assertEqual(entry["effectAt"], lands)
                start = events(res, "cast")[0][0]
                if lands:
                    self.assertAlmostEqual(events(res, "land")[0][0], start + lands)
                else:   # an instant effect: the driver runs with the cast, no landing row
                    self.assertEqual([e for e in events(res, "land") if e[0] < start + busy], [])
                    self.assertTrue(any(e[0] == start for e in events(res, "damage")))

    def test_an_effect_never_lands_after_the_window(self):
        # Diana's orbs are stated 0.96 s in, past her 0.66 s animation
        _, _, res, entry, busy, _ = self.window("Diana")
        self.assertGreater(entry["effectAt"], busy)
        start = events(res, "cast")[0][0]
        self.assertAlmostEqual(events(res, "land")[0][0], start + busy)


class TestFreshAttack(unittest.TestCase):
    def test_rengars_short_animation_cuts_the_attack(self):
        spec = timed("Rengar")
        sheet, res = ENGINE.simulate(spec, True)
        entry = form_entry(SNAP.unit("Rengar"), sheet["form"])
        self.assertEqual((entry["castAnimation"], entry["attackDelay"]), (0.35, 0.21))
        k = spec["kits"]["base"]["stats"]["as"] / sheet["as"]   # the fighters' role attack speed
        self.assertLess(k, 1.0)
        attacks = [e[0] for e in events(res, "attack")]
        start = events(res, "cast")[0][0]
        trigger = max(t for t in attacks if t <= start)
        fresh = min(t for t in attacks if t > start)
        self.assertAlmostEqual(start, trigger + 0.33 * k)
        self.assertAlmostEqual(fresh, start + 0.35 + 0.21 * k)
        # earlier than the period would have allowed: an animation cancel
        self.assertLess(fresh, trigger + 1.0 / sheet["as"])
        # and the attacks after it follow the period again
        following = min(t for t in attacks if t > fresh)
        self.assertAlmostEqual(following, fresh + 1.0 / sheet["as"])

    def test_the_first_attack_has_its_delay_and_the_walk_still_applies(self):
        _, res = ENGINE.simulate(timed("Soraka"), True)
        self.assertAlmostEqual(events(res, "attack")[0][0], 0.21)
        # a melee unit needs both: the walk (0.5 s) outlasts Camille's 0.2 s delay
        spec = spec_for("Camille", duration=2.0, pressure=False, timed=True,
                        dummy=dict(immortal(DUMMY), meleeRepositionSeconds=0.5))
        _, res = ENGINE.simulate(spec, True)
        self.assertEqual(events(res, "attack")[0][0], 0.5)
        spec = spec_for("Camille", duration=2.0, pressure=False, timed=True,
                        dummy=dict(immortal(DUMMY), meleeRepositionSeconds=0.05))
        sheet, res = ENGINE.simulate(spec, True)
        self.assertAlmostEqual(events(res, "attack")[0][0],
                               0.2 * spec["kits"]["base"]["stats"]["as"] / sheet["as"])

    def test_units_without_a_cast_still_wind_up_their_first_attack(self):
        for name, delay in (("Caitlyn", 0.11), ("Master Yi", 0.71)):
            with self.subTest(unit=name):
                spec = timed(name, duration=4.0)
                sheet, res = ENGINE.simulate(spec, True)
                k = spec["kits"][sheet["form"] or "base"]["stats"]["as"] / sheet["as"]
                attacks = [e[0] for e in events(res, "attack")]
                self.assertAlmostEqual(attacks[0], delay * k)
                self.assertAlmostEqual(attacks[1], attacks[0] + 1.0 / sheet["as"])
                self.assertEqual(res["casts"], 0)

    def test_a_unit_with_no_attack_split_attacks_when_it_is_free(self):
        # TFTraits has no delay or recovery for Aphelios: the first attack at
        # 0, the cast at the landing, the next attack as the window closes
        spec = timed("Aphelios")
        _, res = ENGINE.simulate(spec, True)
        attacks = [e[0] for e in events(res, "attack")]
        self.assertEqual(attacks[0], 0.0)
        start = events(res, "cast")[0][0]
        self.assertIn(start, attacks)
        self.assertAlmostEqual(min(t for t in attacks if t > start), start + 1.65 + 2.0)


class TestDriversKeepTheirOwnTiming(unittest.TestCase):
    def test_a_declared_channel_keeps_its_length_and_landing(self):
        # Varus winds up for SpellDuration (2 s, TFTraits' animation too)
        spec = timed("Varus")
        _, res = ENGINE.simulate(spec, True)
        start = events(res, "cast")[0][0]
        self.assertAlmostEqual(events(res, "land")[0][0], start + 2.0)
        # Ahri channels for ChannelTime, longer than her 1.0 s animation: no
        # attack before it ends; her lock is TFTraits' "until the effect
        # ends, 2.25 s after the cast starts"
        spec = timed("Ahri", fx=[REGEN])
        channel = spec["kits"]["base"]["rows"]["ChannelTime"]
        self.assertGreater(channel, 1.0)
        _, res = ENGINE.simulate(spec, True)
        start = events(res, "cast")[0][0]
        self.assertAlmostEqual(events(res, "land")[0][0], start + channel)
        nxt = min(e[0] for e in events(res, "attack") if e[0] > start)
        self.assertAlmostEqual(nxt, start + channel + 0.33)
        cut = copy.deepcopy(spec)
        cut["duration"] = start + 2.2
        _, locked = ENGINE.simulate(cut, True)
        self.assertAlmostEqual(locked["probe"]["lockUntil"], start + 2.25)
        self.assertAlmostEqual(locked["probe"]["castingUntil"], start + channel)

    def test_a_channel_that_lands_at_its_start_still_does(self):
        # Aphelios's onslaught starts with the cast; the unit stays busy for
        # TFTraits' 1.65 s animation + 2 s channel
        _, res = ENGINE.simulate(timed("Aphelios"), True)
        start = events(res, "cast")[0][0]
        swipes = [e[0] for e in events(res, "damage") if e[4] not in ("auto",) and e[0] > start]
        self.assertLessEqual(min(swipes), start + 0.25 + 1e-9)
        self.assertEqual(events(res, "land"), [])

    def test_instant_casts_open_their_window_and_run_the_driver_at_once(self):
        # Tristana's charge and Xayah's feathers start with the cast; the
        # driver's own lock is written after the engine's and stands
        for name, window in (("Tristana", 0.7), ("Xayah", 0.68)):
            with self.subTest(unit=name):
                spec = timed(name)
                sheet, res = ENGINE.simulate(spec, True)
                start = events(res, "cast")[0][0]
                cut = copy.deepcopy(spec)
                cut["duration"] = start + 0.01
                _, now = ENGINE.simulate(cut, True)
                self.assertAlmostEqual(now["probe"]["castingUntil"], start + window)
                self.assertGreater(now["probe"]["lockUntil"], start + window)   # the driver's
                nxt = min(e[0] for e in events(res, "attack") if e[0] > start)
                self.assertGreaterEqual(nxt, start + window)

    def test_azirs_command_lock_survives_the_window(self):
        # the driver takes the bar at the landing, inside the engine's
        # window, and releases it with the sixth command: no mana between
        # the cast and that attack, whatever regen he has
        spec = timed("Azir", fx=[REGEN])
        _, res = ENGINE.simulate(spec, True)
        start = events(res, "cast")[0][0]
        commands = [e[0] for e in events(res, "damage", "soldiers")]
        sixth = sorted(set(commands))[5]
        self.assertGreater(sixth, start + 0.8)
        cost = spec["kits"]["base"]["stats"]["mana"]
        cut = copy.deepcopy(spec)
        cut["duration"] = sixth + 0.001
        _, held = ENGINE.simulate(cut, True)
        self.assertAlmostEqual(held["probe"]["mana"], events(res, "cast")[0][2] - cost)
        self.assertAlmostEqual(held["probe"]["lockUntil"], sixth)      # released, no extra second
        self.assertEqual([e[0] for e in events(res, "attack") if start < e[0] < start + 0.8], [])

    def test_scuttlecrabs_burrow_extends_the_busy_window(self):
        spec = timed("Scuttlecrab")
        spec["kits"]["base"]["stats"].update(initialMana=spec["kits"]["base"]["stats"]["mana"])
        _, res = ENGINE.simulate(spec, True)
        start = events(res, "cast")[0][0]
        land = events(res, "land")[0][0]
        self.assertAlmostEqual(land, start + 0.2)
        burrow = spec["kits"]["base"]["rows"]["BurrowDuration"]
        nxt = min(e[0] for e in events(res, "attack") if e[0] > start)
        self.assertAlmostEqual(nxt, land + burrow)

    def test_elder_dragon_is_untargetable_until_the_landing(self):
        spec = timed("Elder Dragon")
        spec["kits"]["base"]["stats"].update(initialMana=spec["kits"]["base"]["stats"]["mana"])
        spec["duration"] = 1.0
        _, res = ENGINE.simulate(spec, True)
        start = events(res, "cast")[0][0]
        self.assertAlmostEqual(res["probe"]["untargetableUntil"], start + 0.45)
        self.assertAlmostEqual(events(res, "land")[0][0], start + 0.45)


def first_cast(res):
    start = events(res, "cast")[0][0]
    landings = [e[0] for e in events(res, "land") if e[0] >= start]
    return start, (landings[0] if landings else start)


def cut_at(spec, time):
    """The same fight stopped at `time`, for what the probe reports there."""
    short = copy.deepcopy(spec)
    short["duration"] = time
    return ENGINE.simulate(short, True)[1]


class TestDriverManagedWindows(unittest.TestCase):
    """Windows a driver takes over from the engine. The engine writes
    `casting_until` and `lock_until` at the start of the cast and never
    afterwards, so a driver's own window stands, and everything keyed to
    the end of the window (above all the fresh attack) follows the driver's,
    not the timeline's. These read the drivers as they are now; they fail
    without the driver work this merges with."""

    DRAIN = 0.35        # Pebbles' PercentManaPerSecond
    MANA = 70.0

    def laser(self, regen, duration, start=None):
        """Pebbles channelling at a fixed 0.5 attacks a second, so the drain
        is the only thing that moves her clock."""
        spec = timed("Pebbles", duration=duration)
        spec["role"] = {"manaRegen": float(regen), "asPct": 0.0}
        stats = spec["kits"]["base"]["stats"]
        stats.update({"mana": self.MANA, "as": 0.5,
                      "initialMana": self.MANA - 1.0 if start is None else start})
        spec["kits"]["base"]["rows"]["PercentManaPerSecond"] = self.DRAIN
        sheet, res = ENGINE.simulate(spec, True)
        return spec, sheet, res

    def test_the_drain_replaces_the_window_and_the_fresh_attack_follows_it(self):
        spec, sheet, res = self.laser(0.0, 20.0)
        entry = form_entry(SNAP.unit("Pebbles"), sheet["form"])
        start, _ = first_cast(res)
        bar = events(res, "cast")[0][2]
        self.assertGreater(bar, self.MANA)                     # the overflow drains too
        # the projection the driver wrote over the engine's window, read the
        # instant after the cast: the bar over the net drain, to the last ulp
        end = cut_at(spec, start + 0.01)["probe"]["castingUntil"]
        self.assertAlmostEqual(end, start + bar / (self.DRAIN * self.MANA), places=9)
        self.assertLess(end, start + entry["castAnimation"])   # her animation is 3.31 s
        # the fresh attack is armed off the drain's end, not the window's
        scale = spec["kits"]["base"]["stats"]["as"] / sheet["as"]
        nxt = min(e[0] for e in events(res, "attack") if e[0] > start)
        self.assertAlmostEqual(nxt, end + entry["attackDelay"] * scale)
        self.assertLess(nxt, start + entry["castAnimation"] + entry["attackDelay"] * scale)
        # and the laser is paid to that instant, off the quarter-second grid
        last = max(e[0] for e in events(res, "damage", "laser"))
        self.assertAlmostEqual(last, end)
        self.assertNotAlmostEqual(last / TICK, round(last / TICK))

    def test_regen_flows_through_the_drain_and_lengthens_it(self):
        spec, sheet, res = self.laser(7.0, 25.0)
        start, _ = first_cast(res)
        bar = events(res, "cast")[0][2]
        at_cast = cut_at(spec, start + 0.01)["probe"]
        self.assertAlmostEqual(at_cast["lockUntil"], start)    # no lock at all
        self.assertAlmostEqual(at_cast["castingUntil"],
                               start + bar / (self.DRAIN * self.MANA - 7.0), places=9)

    def test_a_drain_regen_outpaces_runs_to_the_end_of_the_fight(self):
        for regen in (self.DRAIN * self.MANA, 40.0):
            with self.subTest(regen=regen):
                spec, _, res = self.laser(regen, 15.0)
                start, _ = first_cast(res)
                self.assertEqual(len(events(res, "cast")), 1)
                self.assertEqual([e[0] for e in events(res, "attack") if e[0] > start], [])
                self.assertGreater(res["probe"]["castingUntil"], 1e8)
                self.assertAlmostEqual(res["probe"]["lockUntil"], start)
                # the fight still ends on its own clock, with the laser paid
                # through the last instant of it
                self.assertAlmostEqual(res["aliveTime"], 15.0)
                self.assertAlmostEqual(max(e[0] for e in events(res, "damage", "laser")), 15.0)


class TestEffectLongLocks(unittest.TestCase):
    """A lock that "lasts as long as the effect runs" is written by the unit's
    driver from the effect's own clock, which starts when the ability LANDS , 
    the barrier, the flock and Frenzy all begin there. TFTraits prints the
    same seconds from the START of the cast, so the engine's lock ends
    `effectAt` later than its figure; those seconds are inside the cast
    window, where nothing came in anyway. The alternative (ending the lock
    while the effect is still up) would contradict the rule it comes from.
    These read the drivers as they are now; they fail without the driver
    work this merges with."""

    # unit -> (TFTraits' manaLock, the kit row/calc that carries the duration)
    CASES = {"Mama Beak": (5.0, "the flock"), "Brambleback": (8.0, "Frenzy"),
             "Diana": (2.0, "the barrier")}

    def test_the_lock_runs_from_the_landing_and_holds_the_bar(self):
        for name, (published, effect) in self.CASES.items():
            with self.subTest(unit=name, effect=effect):
                spec = timed(name, fx=[REGEN], duration=30.0)
                sheet, res = ENGINE.simulate(spec, True)
                entry = form_entry(SNAP.unit(name), sheet["form"])
                start, land = first_cast(res)
                # the ability lands where the timeline says, inside the window
                self.assertAlmostEqual(land - start,
                                       min(entry.get("effectAt", tft.CAST_TIME_DEFAULT),
                                           entry["castAnimation"] + entry.get("channel", 0.0)))
                # the lock ends with the effect: the source's seconds from the
                # landing, which is its figure plus the effect time
                until = cut_at(spec, land + 0.01)["probe"]["lockUntil"]
                self.assertAlmostEqual(until, land + published)
                self.assertAlmostEqual(until - (start + published), land - start)
                # and nothing reached the bar in between, attacks included
                bar, cost = events(res, "cast")[0][2], sheet["manaMax"]
                self.assertGreater(len([e for e in events(res, "attack") if start < e[0] < until]), 0)
                self.assertAlmostEqual(cut_at(spec, until - 0.01)["probe"]["mana"], bar - cost)

    def test_a_driver_that_runs_at_the_cast_keeps_the_sources_own_figure(self):
        # Tristana's charge starts with the cast (LANDS_AT_START), so her
        # "the mana lock lasts the 4.00 s the effect runs" is exactly that
        spec = timed("Tristana", fx=[REGEN])
        _, res = ENGINE.simulate(spec, True)
        start, land = first_cast(res)
        self.assertEqual(land, start)
        self.assertAlmostEqual(cut_at(spec, start + 0.01)["probe"]["lockUntil"],
                               start + spec["kits"]["base"]["rows"]["Duration"])
        self.assertEqual(TIMING["units"]["TFT18_Tristana"]["base"]["manaLock"], 4.0)


class TestSharedSchedulers(unittest.TestCase):
    """The symmetric and theory fights share `Fight`: the same unit follows
    the same timeline whichever scheduler drives it."""

    def test_symmetric_timeline_equals_the_standalone_one(self):
        _, solo = ENGINE.simulate(timed("Soraka", duration=12.0), True)
        attacks = [e[0] for e in events(solo, "attack")]
        casts = [e[0] for e in events(solo, "cast")]
        self.assertEqual(len(casts), 2)

        def member():
            return {"spec": spec_for("Soraka", pressure=True, timed=True), "frontline": False,
                    "lane": 3, "priority": 0}
        duel = match([member()], [ally(hp=10 ** 7)], duration=12.0)
        self.assertEqual([e["time"] for e in match_events(duel, "attack")], attacks)
        self.assertEqual([e["time"] for e in match_events(duel, "cast")], casts)

    def test_a_stun_over_the_unlock_point_delays_the_cast_and_loses_nothing(self):
        leona = caster("Leona", damage=0)                   # her stun lands at 0.25 s
        leona["spec"]["kits"]["base"]["rows"]["StunDuration"] = 1.0
        target = ally("Ashe", driver="Driver", ad=100, attack_speed=1, hp=10 ** 6)
        target["spec"]["kits"]["base"]["stats"].update(mana=7.0, initialMana=0.0)   # one attack fills it
        target["spec"]["unit"]["timing"] = {"base": {"castAnimation": 1.0, "attackDelay": 0.1,
                                                     "attackRecovery": 0.4}}
        result = match([leona], [target], duration=4.0)
        # the attack at 0.1 s fills the bar; its unlock point (0.5 s) is inside
        # the stun, so the cast starts as the stun ends, and the fresh attack
        # a window and a delay later; that attack fills the bar again
        self.assertEqual([e["time"] for e in match_events(result, "attack", "enemy")], [0.1, 2.35, 3.85])
        self.assertEqual([e["time"] for e in match_events(result, "cast", "enemy")], [1.25, 2.75])
        self.assertEqual([e["time"] for e in match_events(result, "land", "enemy")], [1.5, 3.0])


class TestNoTimeline(unittest.TestCase):
    """The flat rules, frozen from the engine before the timelines existed
    (a0cc14674fde): what any unit without an entry must keep reproducing."""

    FROZEN = {
        ("Soraka", 2, ("Nashor's Tooth", "Spear of Shojin", "Guinsoo's Rageblade"), "clump"): {
            "killTime": None, "total": 5309.471428571429, "attacks": 28,
            "castTimes": [1.111111111111111, 3.156004753137201, 5.862747185342913, 8.334962141704548,
                          10.0, 11.928306135993694, 13.890631390551615, 15.726756310636706,
                          17.46877419058562, 19.110665086482022],
            "aliveTime": 20.0, "firstAttacks": [0.0, 1.111111111111111, 2.1609798775153104, 3.156004753137201],
            "lockUntil": 20.110665086482022, "castingUntil": 19.360665086482022, "mana": 9.41212034280975},
        ("Rengar", 2, ("Bloodthirster", "Titan's Resolve", "Sterak's Gage"), "spread"): {
            "killTime": None, "total": 3315.4344827586215, "attacks": 21,
            "castTimes": [2.884615384615384, 8.653846153846153, 14.423076923076923], "aliveTime": 20.0,
            "firstAttacks": [0.0, 0.9615384615384615, 1.923076923076923, 2.884615384615384],
            "lockUntil": 15.423076923076923, "castingUntil": 14.673076923076923, "mana": 40.0},
        ("Leona", 2, ("Warmog's Armor", "Protector's Vow", "Adaptive Helm"), "clump"): {
            "killTime": None, "total": 528.6875, "attacks": 7, "castTimes": [2.0, 6.153846153846153],
            "aliveTime": 9.6, "firstAttacks": [0.0, 1.5384615384615383, 3.0769230769230766, 4.615384615384615],
            "lockUntil": 7.153846153846153, "castingUntil": 6.403846153846153, "mana": 72.07406421278253},
        ("Kayle", 3, ("Guinsoo's Rageblade", "Nashor's Tooth", "Giant Slayer"), "clump"): {
            "killTime": None, "total": 6179.9999999999945, "attacks": 30, "castTimes": [], "aliveTime": 20.0,
            "firstAttacks": [0.0, 0.9876543209876542, 1.9753086419753083, 2.914275778125543],
            "lockUntil": 0.0, "castingUntil": 0.0, "mana": 0.0},
    }

    @staticmethod
    def legacy_board():
        """The three-slot damage board and eight-unit split these numbers were
        frozen against, before the formation put two dummies in a backline.
        Pinned here so the fixture keeps testing the cast rules alone."""
        base = tft.dummies_for(SNAP)
        slots = [dict(base["tank"]), dict(base["tank"]), dict(base["other"])]
        slots[0].update(tft.FRONT_TANK_DEFENSES, fixedDefenses=True)
        n_tanks = round(tft.BOARD_SIZE * base["tanks"] / (base["tanks"] + base["others"]))
        board = [0, 0, 0]
        for i in range(n_tanks):
            board[i % 2] += 1
        board[-1] = tft.BOARD_SIZE - n_tanks
        return dict(base, slots=slots, count=len(slots), board=board,
                    totalHp=sum(s["hp"] for s in slots),
                    targetDebuffs={}, meleeRepositionSeconds=0.0)

    @staticmethod
    def fight(case, cast_timing):
        name, star, items, geometry = case
        dummy = TestNoTimeline.legacy_board()
        spec = tft.cell_spec(SNAP, SNAP.unit(name), star, geometry, [], dummy, 20.0, None, ITEM_FX,
                             TRAIT_FX, items=[item(i) for i in items], cast_timing=cast_timing)
        _, res = ENGINE.simulate(spec, True)
        return spec, {"killTime": res["killTime"], "total": res["total"], "attacks": res["attacks"],
                      "castTimes": res["castTimes"], "aliveTime": res["aliveTime"],
                      "firstAttacks": [e[0] for e in events(res, "attack")][:4],
                      "lockUntil": res["probe"]["lockUntil"], "castingUntil": res["probe"]["castingUntil"],
                      "mana": res["probe"]["mana"]}

    # Nashor's crit mana now settles separately after damage; its operation
    # order deliberately updates only the Soraka fixture's final mana bits.
    def test_a_spec_without_timelines_reproduces_the_old_engine_exactly(self):
        for case, frozen in self.FROZEN.items():
            with self.subTest(unit=case[0]):
                spec, got = self.fight(case, {})
                self.assertIsNone(spec["unit"]["timing"])
                self.assertEqual(got, frozen)

    def test_kayle_has_no_timeline_in_production_either(self):
        case = next(c for c in self.FROZEN if c[0] == "Kayle")
        spec, got = self.fight(case, None)
        self.assertIsNone(spec["unit"]["timing"])
        self.assertEqual(got, self.FROZEN[case])

    def test_a_timeline_moves_the_others(self):
        case = next(c for c in self.FROZEN if c[0] == "Soraka")
        spec, got = self.fight(case, None)
        self.assertEqual(spec["unit"]["timing"]["base"]["castAnimation"], 1.98)
        self.assertLess(len(got["castTimes"]), len(self.FROZEN[case]["castTimes"]))

    def test_the_engine_rejects_a_malformed_timeline(self):
        for bad in ({"castAnimation": -1.0}, {"castAnimation": float("nan")}, {"attackDelay": True},
                    {"castAnimation": 1.0, "effectAt": float("inf")}):
            with self.subTest(timing=bad):
                spec = timed("Soraka")
                spec["unit"]["timing"] = {"base": bad}
                with self.assertRaises(ValueError):
                    ENGINE.simulate(spec, False)


class TestCritOverflow(unittest.TestCase):
    def test_three_infinity_edges(self):
        # 25% + 3 x 35% = 130% crit: 80% of the excess 30% becomes crit
        # damage (Riot's patch 13.18 rate), and the two extra sources of
        # Precision add 10% each: 1.4 + 0.8 x 0.30 + 2 x 0.10
        sheet, _ = ENGINE.simulate(spec_for("Ashe", items=["Infinity Edge"] * 3), False)
        self.assertEqual(sheet["crit"], 1.0)
        self.assertAlmostEqual(sheet["critMult"], 1.84)
        self.assertEqual(tft.CRIT_EXCESS_TO_DAMAGE, 0.8)

    def test_no_excess_no_change(self):
        sheet, _ = ENGINE.simulate(spec_for("Ashe", items=["Infinity Edge"] * 2), False)
        self.assertAlmostEqual(sheet["crit"], 0.95)
        self.assertAlmostEqual(sheet["critMult"], 1.4 + 0.10)


class TestBlueBuff(unittest.TestCase):
    """"Gain 10% additional Attack Damage and Ability Power from all
    sources": native modifiers preserve the base and add duplicate bonuses."""

    def test_blue_buff_and_rabadons(self):
        sheet, _ = ENGINE.simulate(spec_for("Ahri", items=["Blue Buff", "Rabadon's Deathcap"]), False)
        self.assertAlmostEqual(sheet["ap"], 100 + (15 + 55) * 1.1)      # 177, not 187
        base, _ = ENGINE.simulate(spec_for("Ahri"), False)
        self.assertAlmostEqual(sheet["ad"], base["ad"] * (1 + 0.15 * 1.1))

    def test_base_attack_damage_is_not_multiplied(self):
        bare, _ = ENGINE.simulate(spec_for("Ashe"), False)
        blue, _ = ENGINE.simulate(spec_for("Ashe", items=["Blue Buff"]), False)
        self.assertAlmostEqual(blue["ad"], bare["ad"] * (1 + 0.15 * 1.1))
        self.assertAlmostEqual(blue["ap"], 100 + 15 * 1.1)
        both, _ = ENGINE.simulate(spec_for("Ashe", items=["Blue Buff", "Deathblade"]), False)
        self.assertAlmostEqual(both["ad"], bare["ad"] * (1 + (0.15 + 0.55) * 1.1))

    def test_copies_add_their_extra_bonus_percentages(self):
        sheet, _ = ENGINE.simulate(spec_for("Ahri", items=["Blue Buff"] * 2 + ["Rabadon's Deathcap"]), False)
        self.assertAlmostEqual(sheet["ap"], 202.0)  # 100 + (15 + 15 + 55) * 1.20

    def test_what_a_fight_adds_is_a_gain_too(self):
        # Archangel's stacks while she fights: every stack is worth 10% more
        def ap_after(items, seconds):
            spec = spec_for("Ahri", items=items, duration=seconds, dummy=immortal(DUMMY))
            spec["kits"]["base"]["calcs"]["MagicDamageCalc1"]["terms"] = [
                {"type": "scaled", "coef": 1.0, "scaling": "AbilityPower", "op": "add"}]
            spec["kits"]["base"]["stats"].update(initialMana=spec["kits"]["base"]["stats"]["mana"])
            tft.set_geometry(spec, "spread")
            _, res = ENGINE.simulate(spec, True)
            return events(res, "damage", "ability")
        staff = SNAP.item("Archangel's Staff")
        period = tft.curve_at(staff["curve"]["Period"], 1)
        stack = tft.curve_at(staff["curve"]["APStack"], 1)
        fx = ENGINE.compose_fx(spec_for("Ahri", items=["Archangel's Staff", "Blue Buff"]))
        self.assertAlmostEqual(fx["adapMult"], 1.1)
        self.assertEqual(fx["apPerInterval"], [(stack, period)])
        # the sheet's opening AP carries the static bonus only
        sheet, _ = ENGINE.simulate(spec_for("Ahri", items=["Archangel's Staff", "Blue Buff"]), False)
        self.assertAlmostEqual(sheet["ap"], 100 + (30 + 15) * 1.1)
        # one Archangel's interval in: the stack is a gain and is multiplied,
        # read through a cast that lands after it and deals 1 x AP
        spec = spec_for("Ahri", items=["Archangel's Staff", "Blue Buff"], duration=period + 3.0,
                        dummy=immortal(DUMMY), geometry="spread")
        spec["kits"]["base"]["calcs"]["MagicDamageCalc1"] = {"dtype": "true", "terms": [
            {"type": "scaled", "coef": 100.0, "scaling": "AbilityPower", "op": "add"}]}
        spec["kits"]["base"]["rows"]["ChannelTime"] = period + 1.0
        spec["kits"]["base"]["stats"].update(initialMana=spec["kits"]["base"]["stats"]["mana"])
        _, res = ENGINE.simulate(spec, True)
        hit = events(res, "damage", "ability")[0]
        self.assertGreater(hit[0], period)
        self.assertAlmostEqual(hit[2], 100 + (30 + 15 + stack) * 1.1)

    def test_without_the_item_nothing_moves(self):
        # the plain expression is kept bit for bit (the golden replay's job;
        # here the two readings are told apart)
        sheet, _ = ENGINE.simulate(spec_for("Ahri", items=["Rabadon's Deathcap", "Deathblade"]), False)
        base, _ = ENGINE.simulate(spec_for("Ahri"), False)
        self.assertEqual(sheet["ap"], 100 + 55.00000000000001)
        self.assertEqual(sheet["ad"], base["ad"] * (1.0 + 0.55))

    def test_the_item_note_documents_bonus_scope_and_additive_copies(self):
        note = ITEM_FX["items"]["DA_BlueBuff"]["note"]
        self.assertIn("Copies add", note)
        self.assertIn("base", note)



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


def chain_fixture(name, *, hp=(1000, 1000, 1000), delay=0.5, form=None,
            damage=0, cast=False, duration=4, geometry="spread", fx=()):
    effects = list(fx) + ([{"stats": [["ap", 1]]}] if form == "AP" else [])
    dummy = {"critEv": 1.0, "slots": [
        {"hp": health, "armor": 0, "mr": 0, "nearby": True,
         "ad": 0, "as": 0, "ability": 0, "manaMax": 0}
        for health in hp]}
    spec = spec_for(name, star=2, geometry=geometry, duration=duration,
                    pressure=False, dummy=dummy, fx=effects)
    spec.update(immortal=False, autoPressure=False, meleeRepositionSeconds=delay,
                enemyDebuffs={}, targetDebuffs={}, traits=[])
    spec["unit"].update(kind="Specialist", castTime=0.25)
    spec["role"] = {"manaRegen": 0, "asPct": 0}
    for kit in spec["kits"].values():
        kit.update(baseAd=damage, hpStar=10000)
        kit["stats"].update(hp=10000, ad=damage, armor=0, mr=0,
                            critChance=0, critMult=1, range=1,
                            mana=1000000, initialMana=1000000 if cast else 0)
        kit["stats"]["as"] = 0.01 if cast else 1
        for calc in kit["calcs"].values():
            calc["terms"] = [{"type": "flat", "value": 0, "op": "add"}]
    return spec


def akali(**kwargs):
    spec = chain_fixture("Akali", form="AP", cast=True, **kwargs)
    flat(spec, "MagicDamageCalc1", 100)
    for kit in spec["kits"].values():
        kit["rows"].update(TankDamageMultiplierAP=1, RecastDamageReduction=0.7)
    return spec


def yi(**kwargs):
    spec = chain_fixture("Master Yi", damage=100, **kwargs)
    flat(spec, "MagicDamageCalc1", 50)
    flat(spec, "AttackSpeedCalc1", 0)  # isolate the third-attack continuation
    return spec


def hits(result, source):
    return [(hit[0], hit[3], hit[2]) for hit in events(result, "damage", source)]


class TestAkaliMovementChain(unittest.TestCase):
    def test_recasts_wait_for_each_arrival_without_an_extra_cast_window(self):
        for delay, times in ((0, [0.25, 0.25, 0.25]), (0.5, [0.75, 1.25, 1.75])):
            with self.subTest(delay=delay):
                sheet, result = ENGINE.simulate(akali(hp=(50, 50, 1000), delay=delay), True)
                self.assertEqual(sheet["form"], "AP")
                damage = hits(result, "ability")
                self.assertEqual([(time, target) for time, target, _ in damage], list(zip(times, [0, 1, 2])))
                for (_, _, amount), expected in zip(damage, [50, 50, 49], strict=True):
                    self.assertAlmostEqual(amount, expected)
                self.assertEqual(result["castTimes"], [delay])
                self.assertEqual(result["casts"], 1)

    def test_delayed_chain_preserves_its_existing_four_volley_limit(self):
        _, result = ENGINE.simulate(akali(hp=(1, 1, 1, 1, 1000), duration=3), True)
        self.assertEqual(hits(result, "ability"),
                         [(0.75, 0, 1), (1.25, 1, 1), (1.75, 2, 1), (2.25, 3, 1)])
        self.assertEqual(result["left"][-1], 1000)
        self.assertEqual(result["casts"], 1)

    def test_next_victim_dying_in_transit_preserves_recast_power_and_waits_again(self):
        spec = akali(hp=(50, 50, 1000), geometry="clump", duration=1.6,
                     fx=[{"thorns": [60, 10]}])
        spec["pressure"] = True
        spec["dummies"]["slots"][2].update(ad=10, **{"as": 1}, attackStart=1.0)
        _, result = ENGINE.simulate(spec, True)
        # First volley kills at .75. Thorns kills the destination at 1.0,
        # replacing the 1.25 arrival with 1.5. It does not consume a volley.
        self.assertEqual(hits(result, "ability"), [(0.75, 0, 50), (1.5, 2, 70)])
        self.assertEqual([(e[0], e[3]) for e in events(result, "kill")], [(0.75, 0), (1.0, 1)])

    def test_dead_akali_does_not_finish_a_queued_recast(self):
        spec = akali(hp=(50, 1000), duration=2)
        spec["pressure"] = True
        for kit in spec["kits"].values():
            kit["hpStar"] = kit["stats"]["hp"] = 50
        spec["dummies"]["slots"][1].update(ad=100, **{"as": 1}, attackStart=1.0)
        _, result = ENGINE.simulate(spec, True)
        self.assertTrue(result["died"])
        self.assertEqual(hits(result, "ability"), [(0.75, 0, 50)])


class TestBramblebackMovementLeap(unittest.TestCase):
    def test_lethal_leaps_chain_at_arrivals_and_not_initial_engagement(self):
        for delay, times in ((0, [0, 0]), (0.5, [1.0, 1.5])):
            with self.subTest(delay=delay):
                spec = chain_fixture("Brambleback", hp=(100, 100, 100), damage=100, delay=delay)
                flat(spec, "PhysicalDamageCalc1", 100)
                _, result = ENGINE.simulate(spec, True)
                self.assertEqual(hits(result, "auto"), [(delay, 0, 100)])
                self.assertEqual(hits(result, "leap"), list(zip(times, [1, 2], [100, 100])))
                self.assertEqual(result["killTime"], times[-1])

    def test_collateral_kill_does_not_trigger_a_leap_or_more_movement(self):
        spec = chain_fixture("Brambleback", hp=(1000, 50, 1000), geometry="clump", duration=1.6,
                       fx=[{"thorns": [100, 10]}])
        flat(spec, "PhysicalDamageCalc1", 100)
        spec["pressure"] = True
        spec["dummies"]["slots"][0].update(ad=10, **{"as": 1}, attackStart=0.75)
        _, result = ENGINE.simulate(spec, True)
        self.assertEqual([(e[0], e[3]) for e in events(result, "kill")], [(0.75, 1)])
        self.assertEqual(hits(result, "leap"), [])
        self.assertEqual([(e[0], e[3]) for e in events(result, "move")], [(0, 0)])
        self.assertEqual([e[0] for e in events(result, "attack")], [0.5, 1.5])


class TestMasterYiMovementDoubleStrike(unittest.TestCase):
    def test_cross_target_second_hit_waits_in_both_forms(self):
        for form in ("AD", "AP"):
            for delay, second_at in ((0, 2), (0.5, 3)):
                with self.subTest(form=form, delay=delay):
                    spec = yi(hp=(300, 1000), form=form, delay=delay, duration=second_at + 0.1)
                    sheet, result = ENGINE.simulate(spec, True)
                    self.assertEqual(sheet["form"], form)
                    self.assertEqual(hits(result, "auto"),
                                     [(delay, 0, 100), (1 + delay, 0, 100), (2 + delay, 0, 100)])
                    expected = [(second_at, 1, 100)] + ([(second_at, 1, 50)] if form == "AP" else [])
                    self.assertEqual(hits(result, "double strike"), expected)
                    self.assertEqual(result["attacks"], 3)

    def test_same_target_second_hit_has_no_extra_delay(self):
        for form in ("AD", "AP"):
            with self.subTest(form=form):
                _, result = ENGINE.simulate(yi(form=form, duration=2.6), True)
                expected = [(2.5, 0, 100)] + ([(2.5, 0, 50)] if form == "AP" else [])
                self.assertEqual(hits(result, "double strike"), expected)
                self.assertEqual([(e[0], e[3]) for e in events(result, "move")], [(0, 0)])

    def test_second_hit_follows_a_replaced_arrival_once(self):
        spec = yi(hp=(300, 50, 1000), form="AP", geometry="clump", duration=3.4,
                  fx=[{"thorns": [60, 10]}])
        spec["pressure"] = True
        spec["dummies"]["slots"][2].update(ad=10, **{"as": 1}, attackStart=2.75)
        _, result = ENGINE.simulate(spec, True)
        # The third ordinary attack kills at 2.5. Thorns kills the next
        # victim during travel, so the continuation arrives at 3.25 once.
        self.assertEqual(hits(result, "double strike"), [(3.25, 2, 100), (3.25, 2, 50)])
        self.assertEqual([(e[0], e[3]) for e in events(result, "kill")], [(2.5, 0), (2.75, 1)])


class CoreFixtures(unittest.TestCase):
    def setUp(self):
        self.pool = list("ABCDEFGHI")
        self.snap = SimpleNamespace(items={api: {"name": "Item " + api}
                                          for api in self.pool})

    def score(self, items, *, kill=10.0, total=1000.0, alive=30.0,
              capped=False, stress=None, stress_capped=False):
        """Scores are supplied best-first, exactly as the engine returns them."""
        indices = tuple(sorted(self.pool.index(api) for api in items))
        return indices, kill, total, alive, capped, stress, stress_capped

    def analyze(self, scores, objective="carry", **kwargs):
        # These fixtures isolate scoring/exclusion math. The production
        # two-choice eligibility rule is covered in test_tft_pair_cores.
        kwargs.setdefault("min_completions", 1)
        return tft.analyze_cores(self.snap, {"objective": objective}, self.pool,
                                 scores, **kwargs)

    def core(self, result, apis):
        return next(c for c in result["candidates"]
                    if sorted(c["itemApis"]) == sorted(apis))

    def exclusion(self, core, api):
        return next(e for e in core["exclusions"] if e["itemApi"] == api)

    def completion(self, core, apis):
        return next(c for c in core["completions"]
                    if sorted(c["itemApis"]) == sorted(apis))

    def assertLoss(self, comparison, expected, metric="killTime"):
        self.assertEqual(comparison["status"], "comparable")
        self.assertEqual(comparison["metric"], metric)
        self.assertAlmostEqual(comparison["lossPct"], expected)


class TestCarryCores(CoreFixtures):
    def test_optimal_full_build_is_independent_of_the_selected_flexible_core(self):
        result = self.analyze([
            self.score("ABC"), self.score("DEF", kill=10.2),
            self.score("DEG", kill=10.3),
        ], min_completions=2)
        self.assertEqual(result["optimal"]["itemApis"], list("ABC"))
        self.assertLoss(result["optimal"]["performance"], 0)
        self.assertEqual(result["candidates"][0]["itemApis"], list("DE"))
        self.assertLoss(result["candidates"][0]["best"]["performance"], 2)

    def test_core_strength_flexible_third_items_and_individual_exclusions(self):
        result = self.analyze([
            self.score("ABB"), self.score("ABC", kill=10.2),
            self.score("ABD", kill=10.4), self.score("ACD", kill=11.2),
            self.score("BCD", kill=11.5), self.score("CCD", kill=15.0),
        ])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coreSize"], 2)
        self.assertEqual(result["metric"], "killTime")
        self.assertEqual(result["buildsEvaluated"], 6)
        self.assertEqual(result["nearBuildCount"], 3)
        self.assertEqual(result["qualifyingCoreCount"], 6)
        self.assertEqual({e["itemApi"] for e in result["essentialItems"]}, {"A", "B"})
        self.assertEqual(result["requiredItems"], [])
        core = self.core(result, "AB")
        self.assertEqual(result["candidates"][0], core)
        self.assertEqual(core["items"], ["Item A", "Item B"])
        self.assertEqual(core["nearCount"], 3)
        self.assertEqual(core["totalCount"], 3)
        self.assertEqual(core["best"]["itemApis"], ["A", "B", "B"])
        self.assertEqual(core["best"]["items"], ["Item A", "Item B", "Item B"])
        for apis, flex, penalty in (("ABB", "B", 0), ("ABC", "C", 2),
                                    ("ABD", "D", 4)):
            completion = self.completion(core, apis)
            self.assertEqual(completion["flexApis"], [flex])
            self.assertEqual(completion["flexItems"], ["Item " + flex])
            self.assertLoss(completion["performance"], penalty)
            self.assertLoss(completion["vsCore"], penalty)
        for api, alternative, loss in (("A", "BCD", 15), ("B", "ACD", 12)):
            exclusion = self.exclusion(core, api)
            self.assertTrue(exclusion["essential"])
            self.assertIsNone(exclusion["requiredFor"])
            self.assertEqual(exclusion["without"]["itemApis"], list(alternative))
            self.assertLoss(exclusion["without"]["performance"], loss)

    def test_equal_alternative_build_allows_every_slot_to_change(self):
        result = self.analyze([
            self.score("ABC"), self.score("DEF"),
            self.score("ABD", kill=10.1), self.score("ABE", kill=10.2),
        ])
        self.assertEqual(result["essentialItems"], [])
        core = self.core(result, "AB")
        self.assertEqual(core["nearCount"], 3)
        for api in "AB":
            exclusion = self.exclusion(core, api)
            self.assertFalse(exclusion["essential"])
            self.assertEqual(exclusion["without"]["itemApis"], list("DEF"))
            self.assertLoss(exclusion["without"]["performance"], 0)

    def test_exclusions_use_all_results_even_with_one_displayed_core(self):
        result = self.analyze([
            self.score("ABC"), self.score("ABD", kill=10.1),
            self.score("ABE", kill=10.2), self.score("CDE", kill=10.3),
        ], limit=1)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["nearBuildCount"], 4)
        self.assertEqual(result["essentialItems"], [])
        core = self.core(result, "AB")
        for api in "AB":
            alternative = self.exclusion(core, api)["without"]
            self.assertEqual(alternative["itemApis"], list("CDE"))
            self.assertLoss(alternative["performance"], 3)

    def test_completion_penalty_uses_core_best_and_qualification_uses_global_best(self):
        result = self.analyze([
            self.score("ABC"), self.score("ADE", kill=10.2),
            self.score("ADF", kill=10.3), self.score("ADG", kill=10.6),
        ])
        core = self.core(result, "AD")
        self.assertEqual(core["nearCount"], 2)
        self.assertEqual(core["totalCount"], 3)
        self.assertEqual(len(core["completions"]), 3)
        self.assertLoss(core["best"]["performance"], 2)
        self.assertLoss(self.completion(core, "ADE")["vsCore"], 0)
        third = self.completion(core, "ADG")
        self.assertLoss(third["performance"], 6)
        self.assertLoss(third["vsCore"], 3.92156862745098)

    def test_five_percent_boundary_uses_full_precision(self):
        result = self.analyze([
            self.score("ABC"), self.score("ABD", kill=10.4999999),
            self.score("ABE", kill=10.5), self.score("ABF", kill=10.5000001),
        ])
        self.assertEqual(result["thresholdPct"], 5.0)
        self.assertEqual(result["nearBuildCount"], 3)
        core = self.core(result, "AB")
        self.assertEqual(core["nearCount"], 3)
        self.assertEqual(core["totalCount"], 4)
        self.assertLoss(self.completion(core, "ABE")["performance"], 5)
        self.assertLoss(self.completion(core, "ABF")["performance"], 5.000001)

    def test_item_exactly_at_threshold_is_replaceable(self):
        result = self.analyze([
            self.score("ABC"), self.score("BCD", kill=10.5),
            self.score("ACD", kill=10.5000001),
        ])
        core = self.core(result, "AB")
        self.assertFalse(self.exclusion(core, "A")["essential"])
        self.assertTrue(self.exclusion(core, "B")["essential"])
        self.assertEqual([e["itemApi"] for e in result["essentialItems"]], ["B"])

    def test_custom_threshold_changes_near_counts_and_essentiality(self):
        scores = [self.score("ABC"), self.score("BCD", kill=10.8)]
        strict = self.analyze(scores)
        flexible = self.analyze(scores, threshold_pct=10.0)
        self.assertEqual(strict["nearBuildCount"], 1)
        self.assertEqual(flexible["nearBuildCount"], 2)
        self.assertEqual([e["itemApi"] for e in strict["essentialItems"]], ["A"])
        self.assertEqual(flexible["essentialItems"], [])
        self.assertEqual(flexible["thresholdPct"], 10.0)

    def test_failing_to_clear_is_not_assigned_a_percentage(self):
        result = self.analyze([
            self.score("ABC", kill=20, total=100),
            self.score("ABD", kill=21, total=99),
            self.score("ACD", kill=None, total=100000),
            self.score("BCD", kill=None, total=90000),
        ])
        self.assertEqual(result["nearBuildCount"], 2)
        self.assertEqual(result["essentialItems"], [])
        self.assertEqual({e["itemApi"] for e in result["requiredItems"]}, {"A", "B"})
        core = self.core(result, "AB")
        for api in "AB":
            exclusion = self.exclusion(core, api)
            self.assertFalse(exclusion["essential"])
            self.assertEqual(exclusion["requiredFor"], "clear")
            comparison = exclusion["without"]["performance"]
            self.assertEqual(comparison["status"], "noClear")
            self.assertIsNone(comparison["lossPct"])
            self.assertIsNone(comparison["killTime"])
        incomplete = self.completion(self.core(result, "AC"), "ACD")
        self.assertEqual(incomplete["vsCore"]["status"], "noClear")

    def test_missing_clear_at_fight_limit_does_not_prove_five_percent_loss(self):
        # A clear just before 20 seconds gives no measured penalty for a build
        # still fighting at 20; its unknown clear could be inside the 5% range.
        result = self.analyze([
            self.score("ABC", kill=19.99, total=10000, alive=20),
            self.score("BCD", kill=None, total=9999, alive=20),
        ])
        exclusion = self.exclusion(self.core(result, "AB"), "A")
        self.assertFalse(exclusion["essential"])
        self.assertEqual(exclusion["requiredFor"], "clear")
        self.assertEqual(exclusion["without"]["performance"]["status"], "noClear")
        self.assertIsNone(exclusion["without"]["performance"]["lossPct"])
        self.assertEqual(result["essentialItems"], [])
        self.assertEqual([e["itemApi"] for e in result["requiredItems"]], ["A"])
        self.assertEqual(result["nearBuildCount"], 1)


class TestDuplicateCores(CoreFixtures):
    def test_duplicate_core_counts_builds_once_and_removes_exactly_two_items(self):
        result = self.analyze([
            self.score("AAA"), self.score("AAB", kill=10.1),
            self.score("ABB", kill=10.3), self.score("BCD", kill=12),
        ])
        core = self.core(result, "AA")
        self.assertEqual(core["nearCount"], 2)
        self.assertEqual(core["totalCount"], 2)
        self.assertEqual(len(core["completions"]), 2)
        self.assertEqual(self.completion(core, "AAA")["flexApis"], ["A"])
        self.assertEqual(self.completion(core, "AAB")["flexApis"], ["B"])
        self.assertEqual(len(core["exclusions"]), 1)
        exclusion = self.exclusion(core, "A")
        self.assertEqual(exclusion["copies"], 2)
        self.assertTrue(exclusion["essential"])
        self.assertEqual(exclusion["without"]["itemApis"], list("BCD"))
        self.assertLoss(exclusion["without"]["performance"], 20)
        self.assertFalse(exclusion["copiesEssential"])
        self.assertEqual(exclusion["fewerCopies"]["itemApis"], list("ABB"))
        self.assertLoss(exclusion["fewerCopies"]["performance"], 3)
        self.assertEqual(self.core(result, "AB")["totalCount"], 2)

    def test_second_copy_can_be_essential_independently_of_first_copy(self):
        result = self.analyze([
            self.score("AAA"), self.score("AAB", kill=10.2),
            self.score("ABB", kill=11), self.score("BCD", kill=12),
        ])
        exclusion = self.exclusion(self.core(result, "AA"), "A")
        self.assertTrue(exclusion["copiesEssential"])
        self.assertIsNone(exclusion["copiesRequiredFor"])
        self.assertLoss(exclusion["fewerCopies"]["performance"], 10)
        self.assertLoss(exclusion["without"]["performance"], 20)

    def test_second_copy_required_to_clear_has_no_measured_percent_penalty(self):
        result = self.analyze([
            self.score("AAB", kill=19.99, total=10000, alive=20),
            self.score("ABB", kill=None, total=9999, alive=20),
            self.score("BCD", kill=None, total=9998, alive=20),
        ])
        exclusion = self.exclusion(self.core(result, "AA"), "A")
        self.assertFalse(exclusion["essential"])
        self.assertEqual(exclusion["requiredFor"], "clear")
        self.assertFalse(exclusion["copiesEssential"])
        self.assertEqual(exclusion["copiesRequiredFor"], "clear")
        self.assertEqual(exclusion["fewerCopies"]["itemApis"], list("ABB"))
        self.assertEqual(exclusion["without"]["itemApis"], list("BCD"))
        self.assertIsNone(exclusion["fewerCopies"]["performance"]["lossPct"])
        self.assertEqual(result["essentialItems"], [])

    def test_no_legal_alternative_is_not_evidence_of_essentiality(self):
        # B is unique: AAA and AAB are the only supplied legal builds.
        self.pool = list("AB")
        self.snap.items["B"]["unique"] = True
        result = self.analyze([self.score("AAA"), self.score("AAB", kill=10.2)])
        exclusion = self.exclusion(self.core(result, "AA"), "A")
        self.assertIsNone(exclusion["without"])
        self.assertIsNone(exclusion["fewerCopies"])
        self.assertFalse(exclusion["essential"])
        self.assertFalse(exclusion["copiesEssential"])
        self.assertIsNone(exclusion["requiredFor"])
        self.assertIsNone(exclusion["copiesRequiredFor"])
        self.assertEqual(result["essentialItems"], [])
        self.assertEqual(result["requiredItems"], [])

    def test_analysis_does_not_invent_illegal_or_excluded_item_builds(self):
        # A is unique; C remains in the snapshot but is excluded from the pool.
        self.pool = list("AB")
        self.snap.items["A"]["unique"] = True
        result = self.analyze([self.score("ABB"), self.score("BBB", kill=11)])
        self.assertEqual(result["buildsEvaluated"], 2)
        self.assertEqual(len(result["coreStats"]), 2)
        self.assertEqual({tuple(c["itemApis"]) for c in result["candidates"]},
                         {("A", "B"), ("B", "B")})
        self.assertEqual(self.core(result, "AB")["totalCount"], 1)
        self.assertEqual(self.core(result, "BB")["totalCount"], 2)


class TestDamageAndEmptyCores(CoreFixtures):
    def test_nonfinishers_and_fighters_compare_damage_and_use_two_item_cores(self):
        for objective in ("carry", "fighter"):
            with self.subTest(objective=objective):
                result = self.analyze([
                    self.score("ABC", kill=None, total=1000, alive=5),
                    self.score("ABD", kill=None, total=950, alive=30),
                    self.score("ACD", kill=None, total=949, alive=30),
                ], objective=objective)
                self.assertEqual(result["metric"], "damage")
                self.assertEqual(result["coreSize"], 2)
                self.assertEqual(result["nearBuildCount"], 2)
                core = self.core(result, "AB")
                self.assertLoss(self.completion(core, "ABD")["performance"], 5, "damage")
                exclusion = self.exclusion(core, "B")
                self.assertTrue(exclusion["essential"])
                self.assertLoss(exclusion["without"]["performance"], 5.1, "damage")

    def test_zero_damage_does_not_produce_essential_items(self):
        for objective in ("carry", "fighter"):
            with self.subTest(objective=objective):
                result = self.analyze([
                    self.score("ABC", kill=None, total=0),
                    self.score("ABD", kill=None, total=0),
                ], objective=objective)
                self.assertEqual(result["status"], "noDamage")
                self.assertEqual(result["buildsEvaluated"], 2)
                self.assertEqual(result["essentialItems"], [])
                self.assertEqual(result["requiredItems"], [])
                self.assertEqual(result["nearBuildCount"], 0)
                self.assertEqual(result["candidates"], [])
                self.assertEqual(len(result["coreStats"]), 5)
                for stat in result["coreStats"].values():
                    self.assertEqual(stat["best"]["status"], "unavailable")
                    self.assertIsNone(stat["best"]["lossPct"])
                    self.assertEqual(stat["nearCount"], 0)

    def test_empty_input_returns_an_explicit_empty_analysis(self):
        for objective, core_size in (("carry", 2), ("fighter", 2), ("tank", 1)):
            with self.subTest(objective=objective):
                result = self.analyze([], objective=objective)
                self.assertEqual(result["status"], "empty")
                self.assertEqual(result["coreSize"], core_size)
                self.assertEqual(result["buildsEvaluated"], 0)
                self.assertEqual(result["nearBuildCount"], 0)
                self.assertEqual(result["qualifyingCoreCount"], 0)
                self.assertEqual(result["essentialItems"], [])
                self.assertEqual(result["requiredItems"], [])
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["coreStats"], {})


class TestTankCores(CoreFixtures):
    def test_tank_single_item_core_uses_survival_and_two_flexible_items(self):
        result = self.analyze([
            self.score("AAA", kill=None, alive=20, total=100),
            self.score("ABC", kill=None, alive=19, total=1000),
            self.score("BCD", kill=None, alive=18.999, total=10000),
            self.score("DEF", kill=None, alive=10, total=100000),
        ], objective="tank")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coreSize"], 1)
        self.assertEqual(result["metric"], "aliveTime")
        self.assertEqual(result["nearBuildCount"], 2)
        core = self.core(result, "A")
        self.assertEqual(core["nearCount"], 2)
        self.assertEqual(core["totalCount"], 2)
        self.assertEqual(self.completion(core, "AAA")["flexApis"], ["A", "A"])
        self.assertEqual(self.completion(core, "ABC")["flexApis"], ["B", "C"])
        self.assertLoss(self.completion(core, "ABC")["performance"], 5, "aliveTime")
        exclusion = self.exclusion(core, "A")
        self.assertTrue(exclusion["essential"])
        self.assertEqual(exclusion["without"]["itemApis"], list("BCD"))
        self.assertLoss(exclusion["without"]["performance"], 5.005, "aliveTime")

    def test_regular_capped_tanks_compare_stress_survival(self):
        result = self.analyze([
            self.score("AAA", kill=None, capped=True, stress=20),
            self.score("ABC", kill=None, capped=True, stress=19),
            self.score("BCD", kill=None, capped=True, stress=18.9),
            self.score("DEF", kill=None, alive=29.9999, total=100000),
        ], objective="tank")
        self.assertEqual(result["metric"], "stressAliveTime")
        self.assertEqual(result["nearBuildCount"], 2)
        core = self.core(result, "A")
        self.assertLoss(self.completion(core, "ABC")["performance"], 5, "stressAliveTime")
        self.assertLoss(self.exclusion(core, "A")["without"]["performance"],
                        5.5, "stressAliveTime")
        completion = self.completion(self.core(result, "B"), "BCD")
        self.assertLoss(completion["vsCore"], 0.5263157894736842, "stressAliveTime")
        self.assertEqual(len(result["coreStats"]), 6)
        short = [s for s in result["coreStats"].values()
                 if s["best"]["status"] == "shortSurvival"]
        self.assertEqual(len(short), 2)  # E and F only occur in the uncapped build.
        self.assertTrue(all(s["best"]["lossPct"] is None for s in short))
        self.assertTrue(all(s["nearCount"] == 0 for s in short))

    def test_failing_regular_cap_is_not_a_small_percentage_stress_loss(self):
        result = self.analyze([
            self.score("ABC", kill=None, capped=True, stress=20),
            self.score("ABD", kill=None, capped=True, stress=19),
            self.score("CDE", kill=None, alive=29.9999),
        ], objective="tank")
        exclusion = self.exclusion(self.core(result, "A"), "A")
        self.assertFalse(exclusion["essential"])
        self.assertEqual(exclusion["requiredFor"], "survivalLimit")
        self.assertEqual(result["essentialItems"], [])
        self.assertEqual([e["itemApi"] for e in result["requiredItems"]], ["A", "B"])
        comparison = exclusion["without"]["performance"]
        self.assertEqual(comparison["status"], "shortSurvival")
        self.assertIsNone(comparison["lossPct"])
        self.assertFalse(comparison["survivalCapped"])

    def test_surviving_both_caps_is_a_tie_and_cap_failures_have_no_percentage(self):
        result = self.analyze([
            self.score("AAA", kill=None, total=0, capped=True, stress=30,
                       stress_capped=True),
            self.score("ABC", kill=None, total=0, capped=True, stress=30,
                       stress_capped=True),
            self.score("BCD", kill=None, total=1000, capped=True, stress=29.999),
            self.score("DEF", kill=None, total=10000, alive=29.999),
        ], objective="tank")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["metric"], "capped")
        self.assertEqual(result["nearBuildCount"], 2)
        core = self.core(result, "A")
        self.assertLoss(self.completion(core, "ABC")["performance"], 0, "capped")
        exclusion = self.exclusion(core, "A")
        self.assertFalse(exclusion["essential"])
        self.assertEqual(exclusion["requiredFor"], "bothSurvivalLimits")
        self.assertEqual(result["essentialItems"], [])
        self.assertEqual([e["itemApi"] for e in result["requiredItems"]], ["A"])
        self.assertEqual(exclusion["without"]["performance"]["status"], "belowCap")
        self.assertIsNone(exclusion["without"]["performance"]["lossPct"])
        below = [s for s in result["coreStats"].values()
                 if s["best"]["status"] == "belowCap"]
        self.assertEqual(len(below), 3)  # D, E, F.
        self.assertTrue(all(s["best"]["lossPct"] is None for s in below))

    def test_missing_sixty_second_stress_cap_does_not_prove_five_percent_loss(self):
        # The baseline's true survival is censored at 60 seconds. A 59.999
        # second alternative fails that outcome without establishing a >5% gap.
        result = self.analyze([
            self.score("ABC", kill=None, alive=60, capped=True, stress=60,
                       stress_capped=True),
            self.score("BCD", kill=None, alive=60, capped=True, stress=59.999),
        ], objective="tank")
        exclusion = self.exclusion(self.core(result, "A"), "A")
        self.assertFalse(exclusion["essential"])
        self.assertEqual(exclusion["requiredFor"], "bothSurvivalLimits")
        self.assertEqual(exclusion["without"]["performance"]["status"], "belowCap")
        self.assertIsNone(exclusion["without"]["performance"]["lossPct"])
        self.assertEqual(result["essentialItems"], [])
        self.assertEqual([e["itemApi"] for e in result["requiredItems"]], ["A"])
        self.assertEqual(result["nearBuildCount"], 1)


class TestCoreOrdering(CoreFixtures):
    def test_more_near_optimal_completions_rank_ahead_of_a_faster_best_build(self):
        result = self.analyze([
            self.score("ABC"), self.score("DEF", kill=10.1),
            self.score("DEG", kill=10.2), self.score("DEH", kill=10.3),
            self.score("ABF", kill=10.4),
        ], limit=2)
        self.assertEqual([c["itemApis"] for c in result["candidates"]],
                         [["D", "E"], ["A", "B"]])
        self.assertEqual([c["nearCount"] for c in result["candidates"]], [3, 2])

    def test_performance_ties_sort_by_stable_item_identity(self):
        self.pool = list("DBAC")
        # Names and input order intentionally disagree with stable API order.
        for api, name in zip("ABCD", ("Zulu", "Yankee", "Xray", "Whiskey")):
            self.snap.items[api]["name"] = name
        result = self.analyze([self.score("ABD"), self.score("ABC")])
        self.assertEqual([sorted(c["itemApis"]) for c in result["candidates"]],
                         [list("AB"), list("AC"), list("AD"), list("BC"), list("BD")])

    def test_candidate_limit_keeps_stats_for_every_core(self):
        scores = [self.score("ABC"), self.score("DEF"), self.score("GHI")]
        result = self.analyze(scores)
        all_candidates = self.analyze(scores, limit=20)
        self.assertEqual(len(result["candidates"]), 6)
        self.assertEqual(result["qualifyingCoreCount"], 9)
        self.assertEqual(len(result["coreStats"]), 9)
        self.assertEqual(result["coreStats"], all_candidates["coreStats"])
        self.assertEqual([c["id"] for c in result["candidates"]],
                         [c["id"] for c in all_candidates["candidates"][:6]])
        for core in all_candidates["candidates"]:
            stat = result["coreStats"][core["id"]]
            self.assertEqual(stat["nearCount"], 1)
            self.assertEqual(stat["totalCount"], 1)
            self.assertEqual(stat["best"], core["best"]["performance"])


class PairCoreFixtures(unittest.TestCase):
    def setUp(self):
        self.pool = list("ABCDEFGHI")
        self.snap = SimpleNamespace(items={api: {"name": "Item " + api}
                                          for api in self.pool})

    def score(self, items, *, kill=10.0, total=10000.0, alive=30.0,
              capped=False, stress=None, stress_capped=False):
        indices = tuple(sorted(self.pool.index(api) for api in items))
        return indices, kill, total, alive, capped, stress, stress_capped

    def pairs(self, scores, overrides=None, *, default_kill=30.0,
              default_total=1000.0):
        """Supply every distinct pair, including pairs of duplicate items.

        Overrides may also add legal pairs absent from the three-item fixture;
        those must still participate in the global two-item comparison.
        """
        required = {tuple(sorted(self.pool[i] for i in pair))
                    for score in scores
                    for pair in itertools.combinations(score[0], 2)}
        overrides = {tuple(sorted(apis)): values
                     for apis, values in (overrides or {}).items()}
        pairs = []
        for apis in sorted(required | overrides.keys()):
            values = dict(kill=default_kill, total=default_total)
            values.update(overrides.get(apis, {}))
            pairs.append(self.score(apis, **values))
        return sorted(pairs, key=lambda score: (
            score[1] is None,
            score[1] if score[1] is not None else -score[2],
            tuple(sorted(self.pool[i] for i in score[0])),
        ))

    def analyze(self, scores, pair_scores, objective="carry", **kwargs):
        # Isolate ranking/grouping from the eligibility threshold; the
        # default two-choice rule has its own cases below.
        kwargs.setdefault("min_completions", 1)
        return tft.analyze_cores(self.snap, {"objective": objective}, self.pool,
                                 scores, pair_scores=pair_scores, **kwargs)

    def identities(self, result):
        return ["".join(sorted(core["itemApis"])) for core in result["candidates"]]

    def core(self, result, apis):
        return next(core for core in result["candidates"]
                    if sorted(core["itemApis"]) == sorted(apis))

    def assertLoss(self, comparison, expected, metric="killTime"):
        self.assertEqual(comparison["status"], "comparable")
        self.assertEqual(comparison["metric"], metric)
        self.assertAlmostEqual(comparison["lossPct"], expected)

    def assertDistinctNearCompletions(self, result):
        seen = set()
        for core in result["candidates"]:
            near = {tuple(sorted(completion["itemApis"]))
                    for completion in core["completions"]
                    if completion["performance"]["status"] == "comparable"
                    and completion["performance"]["lossPct"] <= result["thresholdPct"] + 1e-9}
            self.assertTrue(near)
            self.assertTrue(seen.isdisjoint(near))
            seen.update(near)


class TestMeasuredPairSelection(PairCoreFixtures):
    def test_one_full_build_selects_its_strongest_actual_pair(self):
        scores = [self.score("ABC", kill=10, total=10000)]
        pair_scores = self.pairs(scores, {
            "AB": {"kill": 18, "total": 300},
            "AC": {"kill": 12, "total": 400},
            "BC": {"kill": 15, "total": 350},
        })
        result = self.analyze(scores, pair_scores)
        self.assertEqual(self.identities(result), ["AC"])
        self.assertEqual(result["selectionMode"], "twoItemSpike")
        self.assertEqual(result["pairBuildsEvaluated"], 3)
        self.assertEqual(result["qualifyingCoreCount"], 3)
        self.assertEqual(result["groupedCoreCount"], 1)
        core = self.core(result, "AC")
        self.assertEqual(core["spike"]["killTime"], 12)
        self.assertEqual(core["spike"]["total"], 400)
        self.assertLoss(core["spike"], 0)
        self.assertEqual(core["best"]["performance"]["killTime"], 10)
        self.assertEqual(core["best"]["performance"]["total"], 10000)
        self.assertEqual(core["completions"][0]["flexApis"], ["B"])
        self.assertEqual(set(result["coreStats"]), {"A|B", "A|C", "B|C"})

    def test_spike_can_beat_a_more_flexible_core_with_a_better_full_build(self):
        scores = [self.score("ABC"), self.score("ABF", kill=10.1),
                  self.score("ABG", kill=10.2), self.score("DEH", kill=10.3),
                  self.score("DEI", kill=10.4)]
        pairs = self.pairs(scores, {"AB": {"kill": 20}, "DE": {"kill": 12}})
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["DE", "AB"])
        self.assertEqual(result["nearBuildCount"], 5)
        self.assertEqual(self.core(result, "DE")["nearCount"], 2)
        self.assertEqual(self.core(result, "AB")["nearCount"], 3)
        self.assertLoss(self.core(result, "DE")["best"]["performance"], 3)
        self.assertLoss(self.core(result, "AB")["best"]["performance"], 0)
        self.assertLoss(self.core(result, "AB")["spike"], 100 * (20 - 12) / 12)
        self.assertDistinctNearCompletions(result)

    def test_globally_best_pair_with_bad_completions_is_not_recommended(self):
        scores = [self.score("ABC"), self.score("DEF", kill=15)]
        pairs = self.pairs(scores, {"AB": {"kill": 20}, "DE": {"kill": 5}})
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["AB"])
        self.assertEqual(result["nearBuildCount"], 1)
        self.assertEqual(result["qualifyingCoreCount"], 3)
        self.assertEqual(result["groupedCoreCount"], 1)
        self.assertEqual(result["pairBuildsEvaluated"], 6)
        self.assertEqual(result["coreStats"]["D|E"]["nearCount"], 0)
        self.assertLoss(self.core(result, "AB")["spike"], 300)
        self.assertLoss(self.core(result, "AB")["best"]["performance"], 0)

    def test_pair_baseline_includes_supplied_pairs_absent_from_full_fixture(self):
        scores = [self.score("ABC")]
        pairs = self.pairs(scores, {"AB": {"kill": 20}, "DE": {"kill": 10}})
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["AB"])
        self.assertEqual(result["pairBuildsEvaluated"], 4)
        self.assertNotIn("D|E", result["coreStats"])
        self.assertLoss(self.core(result, "AB")["spike"], 100)

    def test_nonclearing_pairs_use_their_actual_damage_even_when_full_builds_clear(self):
        for objective in ("carry", "fighter"):
            with self.subTest(objective=objective):
                scores = [self.score("ABC", total=10000),
                          self.score("DEF", kill=10.1, total=9900)]
                pairs = self.pairs(scores, {
                    "AB": {"total": 700}, "DE": {"total": 1000},
                }, default_kill=None, default_total=100)
                result = self.analyze(scores, pairs, objective=objective)
                self.assertEqual(self.identities(result), ["DE", "AB"])
                self.assertEqual(result["metric"], "killTime")
                core = self.core(result, "AB")
                self.assertIsNone(core["spike"]["killTime"])
                self.assertEqual(core["spike"]["total"], 700)
                self.assertLoss(core["spike"], 30, "damage")
                self.assertEqual(core["best"]["performance"]["total"], 10000)
                self.assertLoss(core["best"]["performance"], 0)

    def test_clearing_pair_beats_nonclear_and_does_not_invent_a_loss_percent(self):
        scores = [self.score("ABC"), self.score("DEF", kill=10.1)]
        pairs = self.pairs(scores, {
            "AB": {"total": 999999}, "DE": {"kill": 29, "total": 100},
        }, default_kill=None, default_total=1000)
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["DE", "AB"])
        self.assertLoss(self.core(result, "DE")["spike"], 0)
        spike = self.core(result, "AB")["spike"]
        self.assertEqual(spike["status"], "noClear")
        self.assertEqual(spike["metric"], "killTime")
        self.assertIsNone(spike["killTime"])
        self.assertIsNone(spike["lossPct"])
        self.assertEqual(spike["total"], 999999)


class TestDistinctPairFamilies(PairCoreFixtures):
    def test_pairs_sharing_one_item_can_be_separate_families(self):
        scores = [self.score("ABC"), self.score("ADE", kill=10.1)]
        pairs = self.pairs(scores, {"AB": {"kill": 12}, "AD": {"kill": 14}})
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["AB", "AD"])
        self.assertEqual(result["groupedCoreCount"], 2)
        self.assertDistinctNearCompletions(result)

    def test_shared_bad_completion_does_not_collapse_competitive_families(self):
        scores = [self.score("ABD"), self.score("ACE", kill=10.1),
                  self.score("ABC", kill=12)]
        pairs = self.pairs(scores, {"AB": {"kill": 12}, "AC": {"kill": 14}})
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["AB", "AC"])
        self.assertEqual(result["nearBuildCount"], 2)
        for apis in ("AB", "AC"):
            core = self.core(result, apis)
            self.assertEqual(core["totalCount"], 2)
            bad = next(c for c in core["completions"]
                       if sorted(c["itemApis"]) == list("ABC"))
            self.assertLoss(bad["performance"], 20)
        self.assertDistinctNearCompletions(result)

    def test_grouping_does_not_merge_through_a_suppressed_pair(self):
        # BC bridges ABC and BCD, but losing BC to AB must not hide CD.
        scores = [self.score("ABC"), self.score("BCD", kill=10.1)]
        pairs = self.pairs(scores, {
            "AB": {"kill": 12}, "BC": {"kill": 13}, "CD": {"kill": 14},
        })
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["AB", "CD"])
        self.assertEqual(result["qualifyingCoreCount"], 5)
        self.assertEqual(result["groupedCoreCount"], 2)
        self.assertEqual(result["coreStats"]["B|C"]["nearCount"], 2)
        self.assertEqual(result["coreStats"]["B|C"]["totalCount"], 2)
        self.assertDistinctNearCompletions(result)

    def test_duplicate_item_build_is_counted_once_and_keeps_the_correct_flex_item(self):
        for winner, flex in (("AA", "B"), ("AB", "A")):
            with self.subTest(winner=winner):
                scores = [self.score("AAB")]
                pairs = self.pairs(scores, {winner: {"kill": 12}})
                result = self.analyze(scores, pairs)
                self.assertEqual(self.identities(result), [winner])
                self.assertEqual(result["pairBuildsEvaluated"], 2)
                self.assertEqual(result["qualifyingCoreCount"], 2)
                self.assertEqual(result["groupedCoreCount"], 1)
                self.assertEqual(set(result["coreStats"]), {"A|A", "A|B"})
                self.assertEqual(result["coreStats"]["A|B"]["totalCount"], 1)
                core = self.core(result, winner)
                self.assertEqual(core["nearCount"], 1)
                self.assertEqual(core["totalCount"], 1)
                self.assertEqual(core["completions"][0]["flexApis"], [flex])
                self.assertEqual(sum(e["copies"] for e in core["exclusions"]), 2)

    def test_display_limit_preserves_group_count_and_all_full_build_statistics(self):
        scores = [self.score("ABC"), self.score("ABD", kill=10.1),
                  self.score("DEF", kill=10.2), self.score("GHI", kill=10.3),
                  self.score("ABE", kill=12)]
        pairs = self.pairs(scores, {
            "AB": {"kill": 12}, "DE": {"kill": 14}, "GH": {"kill": 16},
        })
        limited = self.analyze(scores, pairs, limit=1)
        full = self.analyze(scores, pairs, limit=20)
        legacy = self.analyze(scores, None, limit=20)
        self.assertEqual(self.identities(limited), ["AB"])
        self.assertEqual(self.identities(full), ["AB", "DE", "GH"])
        self.assertEqual(limited["groupedCoreCount"], 3)
        self.assertEqual(full["groupedCoreCount"], 3)
        for key in ("buildsEvaluated", "nearBuildCount", "qualifyingCoreCount",
                    "coreStats", "essentialItems", "requiredItems", "metric"):
            self.assertEqual(limited[key], full[key], key)
            self.assertEqual(limited[key], legacy[key], key)
        selected = self.core(limited, "AB")
        prior = self.core(legacy, "AB")
        for key in ("best", "completions", "exclusions", "nearCount", "totalCount"):
            self.assertEqual(selected[key], prior[key], key)
        self.assertEqual(selected["totalCount"], 3)
        self.assertEqual(selected["nearCount"], 2)
        self.assertDistinctNearCompletions(full)


class TestPairOrderingAndCoverage(PairCoreFixtures):
    def test_equal_pair_clear_times_prefer_more_near_completions_before_damage(self):
        scores = [self.score("ABC"), self.score("DEF", kill=10.1),
                  self.score("DEG", kill=10.2)]
        pairs = self.pairs(scores, {
            "AB": {"kill": 20, "total": 100000},
            "DE": {"kill": 20, "total": 100},
        })
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["DE", "AB"])

    def test_equal_spike_and_flexibility_prefer_the_better_full_build(self):
        scores = [self.score("ABC"), self.score("DEF", kill=10.2)]
        pairs = self.pairs(scores, {
            "AB": {"kill": 20, "total": 100},
            "DE": {"kill": 20, "total": 100000},
        })
        result = self.analyze(scores, pairs)
        self.assertEqual(self.identities(result), ["AB", "DE"])

    def test_primary_ties_use_stable_item_apis_despite_pool_and_name_order(self):
        for api, name in zip("ABC", ("Zulu", "Yankee", "Alpha")):
            self.snap.items[api]["name"] = name
        for pool in ("ABC", "CBA", "BAC"):
            with self.subTest(pool=pool):
                self.pool = list(pool)
                scores = [self.score("ABC")]
                pairs = self.pairs(scores, default_kill=20)
                result = self.analyze(scores, pairs)
                self.assertEqual(self.identities(result), ["AB"])
                self.assertEqual(result["candidates"][0]["id"], "A|B")

    def test_explicit_missing_or_partial_pair_data_raises(self):
        scores = [self.score("ABC"), self.score("DEF", kill=15)]
        complete = self.pairs(scores)
        missing_nonqualifying = [score for score in complete
                                 if sorted(self.pool[i] for i in score[0]) != list("EF")]
        for pair_scores in ([], [self.score("AB", kill=20)], missing_nonqualifying):
            with self.subTest(pair_count=len(pair_scores)):
                with self.assertRaises(ValueError):
                    self.analyze(scores, pair_scores)

    def test_none_retains_full_build_based_compatibility_without_measured_spikes(self):
        scores = [self.score("ABC"), self.score("ABD", kill=10.1)]
        result = self.analyze(scores, None)
        self.assertEqual(self.identities(result)[0], "AB")
        self.assertEqual(len(result["candidates"]), 5)
        self.assertNotEqual(result.get("selectionMode"), "twoItemSpike")
        self.assertTrue(all(core.get("spike") is None for core in result["candidates"]))

    def test_tank_single_item_recommendations_do_not_require_or_infer_pair_spikes(self):
        scores = [self.score("AAA", kill=None, alive=20),
                  self.score("ABC", kill=None, alive=19.5),
                  self.score("BCD", kill=None, alive=18)]
        analyses = [self.analyze(scores, pair_scores, objective="tank")
                    for pair_scores in (None, [], [self.score("BC", kill=1)])]
        baseline = analyses[0]
        self.assertEqual(baseline["coreSize"], 1)
        self.assertEqual(baseline["metric"], "aliveTime")
        self.assertEqual(self.identities(baseline), ["A", "B", "C"])
        for result in analyses:
            self.assertNotEqual(result.get("selectionMode"), "twoItemSpike")
            self.assertEqual(result["candidates"], baseline["candidates"])
            self.assertEqual(result["coreStats"], baseline["coreStats"])
            self.assertTrue(all(core.get("spike") is None for core in result["candidates"]))


class TestMinimumCompletions(PairCoreFixtures):
    def test_one_choice_pair_cannot_suppress_a_flexible_pair(self):
        scores = [self.score("ABC"), self.score("ACD", kill=10.2)]
        pairs = self.pairs(scores, {"AB": {"kill": 11}, "AC": {"kill": 15}})
        for objective in ("carry", "fighter"):
            with self.subTest(objective=objective):
                result = tft.analyze_cores(self.snap, {"objective": objective}, self.pool,
                                           scores, pair_scores=pairs)
                self.assertEqual(result["minCompletions"], 2)
                self.assertEqual(self.identities(result), ["AC"])
                self.assertEqual(result["qualifyingCoreCount"], 1)
                self.assertEqual(result["groupedCoreCount"], 1)
                self.assertEqual(result["coreStats"]["A|B"]["nearCount"], 1)
                self.assertEqual(self.core(result, "AC")["nearCount"], 2)

    def test_no_flexible_pair_does_not_fall_back_to_single_completion_cores(self):
        scores = [self.score("ABC")]
        for pairs in (None, self.pairs(scores)):
            with self.subTest(pair_scores=pairs):
                result = tft.analyze_cores(self.snap, {"objective": "carry"}, self.pool,
                                           scores, pair_scores=pairs)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["optimal"]["itemApis"], list("ABC"))
                self.assertEqual(result["optimal"]["performance"]["lossPct"], 0)
                self.assertEqual(result["qualifyingCoreCount"], 0)
                self.assertEqual(result["nearBuildCount"], 1)
                self.assertEqual(len(result["coreStats"]), 3)

    def test_two_distinct_finishers_qualify_at_the_exact_five_percent_boundary(self):
        scores = [self.score("AAB"), self.score("AAC", kill=10.5),
                  self.score("AAD", kill=10.5000001)]
        result = tft.analyze_cores(self.snap, {"objective": "carry"}, self.pool,
                                   scores, pair_scores=self.pairs(scores))
        self.assertEqual(self.identities(result), ["AA"])
        core = self.core(result, "AA")
        self.assertEqual(core["nearCount"], 2)
        self.assertEqual(core["totalCount"], 3)
        self.assertEqual([c["flexApis"] for c in core["completions"]], [["B"], ["C"], ["D"]])
        # The repeated A in AAB does not create two choices for the AB core.
        self.assertEqual(result["coreStats"]["A|B"]["nearCount"], 1)

    def test_tanks_still_allow_one_completion(self):
        scores = [self.score("ABC", kill=None, alive=30)]
        result = tft.analyze_cores(self.snap, {"objective": "tank"}, self.pool, scores)
        self.assertEqual(result["minCompletions"], 1)
        self.assertEqual(self.identities(result), ["A", "B", "C"])
        self.assertTrue(all(c["nearCount"] == 1 for c in result["candidates"]))


def legal_pairs(pool):
    return [pair for pair in itertools.combinations_with_replacement(range(len(pool)), 2)
            if pair[0] != pair[1] or not pool[pair[0]]["unique"]]


def compact_score(pair, result):
    return (list(pair), result["killTime"], result["total"], result["aliveTime"],
            result["survivalCapped"], result["stressAliveTime"], result["stressCapped"])


def direct_pair_scores(spec):
    rows = []
    for pair in legal_pairs(spec["pool"]):
        single = dict(spec, items=[spec["pool"][i] for i in pair])
        _, result = ENGINE.simulate(single)
        rows.append((pair, result))
    rows.sort(key=lambda row: (tft.rank_key(row[1], spec["unit"]["objective"]),
                              tuple(spec["pool"][i]["api"] for i in row[0])))
    return [compact_score(pair, result) for pair, result in rows]


def with_pool(spec, names):
    unit = SNAP.units[spec["unit"]["api"]]
    spec["pool"] = [tft.item_spec(SNAP, SNAP.item(name)["api"], ITEM_FX, unit)
                    for name in names]
    return spec


def synthetic_item(api, unique=False, stats=()):
    return {"api": api, "name": api, "unique": unique, "stats": list(stats), "adds": []}


class TestPairScores(unittest.TestCase):
    def test_match_individual_fights_across_stars_geometry_traits_and_forms(self):
        names = ["Nashor's Tooth", "Jeweled Gauntlet", "Last Whisper",
                 "Void Staff", "Warmog's Armor"]
        cleared = uncleared = False
        for name in ("Ashe", "Ahri", "Warwick", "Akali"):
            unit = SNAP.unit(name)
            contexts, _ = tft.unit_trait_contexts(SNAP, unit, TRAIT_FX)
            for star, geometry, context in itertools.product((1, 2), ("spread", "clump"),
                                                              ("bare", "low", "high")):
                with self.subTest(unit=name, star=star, geometry=geometry, traits=context):
                    spec = tft.cell_spec(SNAP, unit, star, geometry, contexts[context],
                                         tft.dummies_for(SNAP), item_fx=ITEM_FX, trait_fx=TRAIT_FX)
                    with_pool(spec, names)
                    self.assertEqual(spec["targetDebuffs"], {"sunder": 0.3, "shred": 0.3})
                    expected = direct_pair_scores(spec)
                    scores = ENGINE.score_pairs(spec, workers=1)
                    self.assertEqual(scores, expected)
                    self.assertEqual(len(scores), len(legal_pairs(spec["pool"])))
                    self.assertTrue(all(len(score[0]) == 2 for score in scores))
                    cleared |= any(score[1] is not None for score in scores)
                    uncleared |= any(score[1] is None for score in scores)
        self.assertTrue(cleared)
        self.assertTrue(uncleared)

    def test_team_reduction_remains_redundant_with_pair_item_passives(self):
        for unit, item, effect in (("Ashe", "Last Whisper", "sunderOnHit"),
                                    ("Ahri", "Void Staff", "shredOnHit")):
            with self.subTest(unit=unit):
                spec = with_pool(spec_for(unit, duration=12.0, dummy=immortal(DUMMY)),
                                 [item, "Nashor's Tooth"])
                spec["targetDebuffs"] = {"sunder": 0.3, "shred": 0.3}
                without_passive = copy.deepcopy(spec)
                without_passive["pool"][0].pop(effect)
                expected = ENGINE.score_pairs(spec, 1)
                self.assertEqual(expected, direct_pair_scores(spec))
                self.assertEqual(ENGINE.score_pairs(without_passive, 1), expected)
                spec["targetDebuffs"] = {}
                without_passive["targetDebuffs"] = {}
                self.assertNotEqual(ENGINE.score_pairs(spec, 1),
                                    ENGINE.score_pairs(without_passive, 1))

    def test_pair_has_no_implicit_third_item_or_items_from_single_fight_spec(self):
        spec = spec_for("Ashe", driver="Driver", duration=0.1)
        ad = synthetic_item("ad", stats=[["adPct", 0.5]])
        spec["pool"] = [ad]
        # Single-fight items are a separate input and cannot fill the empty
        # third slot in a pool enumeration.
        spec["items"] = [synthetic_item("extra", stats=[["adPct", 10.0]])]
        pair = ENGINE.score_pairs(spec, 1)
        self.assertEqual(pair, direct_pair_scores(spec))
        self.assertEqual(pair[0][0], [0, 0])
        _, bare = ENGINE.simulate(dict(spec, items=[]))
        _, third = ENGINE.simulate(dict(spec, items=[ad, ad, ad]))
        self.assertAlmostEqual(pair[0][2], 2 * bare["total"])
        self.assertGreater(third["total"], pair[0][2])
        spec["duration"] = 1.5
        longer = ENGINE.score_pairs(spec, 1)
        self.assertEqual(longer, direct_pair_scores(spec))
        self.assertGreater(longer[0][2], pair[0][2])

    def test_unique_constraints_duplicates_and_api_ties(self):
        spec = spec_for("Ashe", duration=0.1)
        spec["pool"] = [synthetic_item("z"), synthetic_item("a", True), synthetic_item("m")]
        expected = sorted(legal_pairs(spec["pool"]),
                          key=lambda pair: tuple(spec["pool"][i]["api"] for i in pair))
        scores = ENGINE.score_pairs(spec)
        self.assertEqual([tuple(score[0]) for score in scores], expected)
        self.assertEqual(scores, direct_pair_scores(spec))
        self.assertIn((0, 0), expected)
        self.assertIn((2, 2), expected)
        self.assertNotIn((1, 1), expected)
        self.assertEqual(len(scores), len(set(tuple(score[0]) for score in scores)))

    def test_complete_pool_is_independent_of_worker_count(self):
        unit = SNAP.unit("Ashe")
        spec = spec_for("Ashe")
        spec["pool"] = [tft.item_spec(SNAP, api, ITEM_FX, unit)
                        for api in tft.pool_items(SNAP, ITEM_FX)]
        expected = ENGINE.score_pairs(spec, 1)
        self.assertGreater(len(expected), 500)
        self.assertEqual({tuple(score[0]) for score in expected}, set(legal_pairs(spec["pool"])))
        for workers in (4, 0):
            with self.subTest(workers=workers):
                self.assertEqual(ENGINE.score_pairs(spec, workers), expected)

    def test_empty_and_small_legal_pools(self):
        spec = spec_for("Ashe", duration=0.1)
        for pool, expected in (([], []),
                               ([synthetic_item("only", True)], []),
                               ([synthetic_item("only")], [[0, 0]]),
                               ([synthetic_item("a", True), synthetic_item("b", True)], [[0, 1]])):
            with self.subTest(pool=pool):
                spec["pool"] = pool
                # A huge request is bounded by the number of nonempty batches.
                scores = ENGINE.score_pairs(spec, workers=1000000)
                self.assertEqual([score[0] for score in scores], expected)
                self.assertEqual(scores, direct_pair_scores(spec))

    def test_tank_score_fields_and_stress_runs_keep_the_same_meaning(self):
        spec = body_spec(hp=400.0, duration=6.5)
        spec["pool"] = [synthetic_item("health", stats=[["hp", 300.0]]),
                        synthetic_item("damage", stats=[["adPct", 10.0]])]
        scores = ENGINE.score_pairs(spec, 1)
        self.assertEqual(scores, direct_pair_scores(spec))
        by_combo = {tuple(score[0]): score for score in scores}
        self.assertEqual(by_combo[(0, 0)][3:], (6.5, True, 5.0, False))
        self.assertEqual(by_combo[(0, 1)][3:], (6.5, True, 4.0, False))
        self.assertEqual(by_combo[(1, 1)][3:], (4.0, False, None, False))


class TestCoreCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def test_analysis_uses_builds_below_the_display_limit(self):
        unit = self.snap.unit("Ashe")
        key = "s2-clump-bare"
        # Three items whose builds stay within a few percent of each other for
        # Ashe, so a flexible core exists (with the cast timelines Red Buff's
        # builds no longer are: Giant Slayer x2 + Red Buff leads by 9%; with
        # the five-slot formation Deathblade's no longer are either).
        pool = [self.snap.item(name)["api"] for name in
                ("Blue Buff", "Giant Slayer", "Infinity Edge")]
        full, count = tft.enumerate_builds(self.snap, unit, 2, "clump", [],
                                          tft.dummies_for(self.snap), pool, top=100, workers=1)
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "CACHED_ROWS", 1), \
                patch.object(tft, "pool_items", return_value=pool):
            path = os.path.join(directory, "cell.json")
            cell = tft.compute_cell(self.snap, unit, key, {("ashe", key): path}, prune=False)
            self.assertEqual(cell["rows"], tft.cell_rows(self.snap, unit, full, 1))
            self.assertEqual(cell["coreAnalysis"]["buildsEvaluated"], count)
            self.assertGreater(count, len(cell["rows"]))
            candidates = cell["coreAnalysis"]["candidates"]
            self.assertTrue(any(len(core["completions"]) > 1 for core in candidates))
            for core in candidates:
                for excluded in core["exclusions"]:
                    best_without = next(combo for combo, _, _ in full
                                        if excluded["itemApi"] not in combo)
                    self.assertEqual(excluded["without"]["itemApis"], list(best_without))
            with open(path) as f:
                self.assertEqual(json.load(f), cell)

    def test_contexts_cover_all_cores_and_only_the_requested_current_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            keys = ("s1-clump-bare", "s2-clump-bare", "s2-spread-bare")
            paths = {("ahri", key): os.path.join(directory, key + ".json") for key in keys}
            paths[("leona", keys[0])] = os.path.join(directory, "other.json")
            stats = {"hidden-from-suggestions": {"nearCount": 2, "totalCount": 35,
                                               "best": {"lossPct": 1.25, "status": "comparable"}}}
            first = {"scenario": tft.SCENARIOS[keys[0]],
                     "coreAnalysis": {"coreStats": stats, "candidates": []}}
            # The second cell is from an old schema, the third is still cold.
            for key, content in ((keys[0], first), (keys[1], {"rows": []})):
                with open(paths[("ahri", key)], "w") as f:
                    json.dump(content, f)
            with patch.object(tft, "snapshot_revision", return_value="current"):
                contexts = tft.cached_core_contexts("ahri", paths, snap=self.snap)
            self.assertEqual(contexts["totalScenarios"], 3)
            self.assertEqual(contexts["revision"], "current")
            self.assertEqual(len(contexts["scenarios"]), 1)
            self.assertEqual(contexts["scenarios"][0]["key"], keys[0])
            self.assertEqual(contexts["scenarios"][0]["coreStats"], stats)
            self.assertFalse(os.path.exists(paths[("ahri", keys[2])]))
            with self.assertRaisesRegex(ValueError, "unknown unit"):
                tft.cached_core_contexts("missing", paths, snap=self.snap)

    def test_truncated_score_export_is_rejected(self):
        unit = self.snap.unit("Ahri")
        with patch.object(tft.engine(), "analyze_cell", return_value=(2, [], [])):
            with self.assertRaisesRegex(ValueError, "every enumerated build"):
                tft.enumerate_builds(self.snap, unit, 2, "clump", [],
                                     tft.dummies_for(self.snap), [], include_scores=True)

    def test_cached_spikes_are_actual_two_item_fights(self):
        unit = self.snap.unit("Ashe")
        pool = [self.snap.item(name)["api"] for name in ("Blue Buff", "Giant Slayer", "Infinity Edge")]
        key = "s2-clump-bare"
        dummy = tft.dummies_for(self.snap)
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "pool_items", return_value=pool):
            path = os.path.join(directory, "cell.json")
            cell = tft.compute_cell(self.snap, unit, key, {("ashe", key): path}, prune=False)
            analysis = cell["coreAnalysis"]
            self.assertEqual(analysis["selectionMode"], "twoItemSpike")
            self.assertEqual(analysis["pairBuildsEvaluated"], 6)
            self.assertEqual(analysis["minCompletions"], 2)
            self.assertTrue(analysis["candidates"])
            used_finishes = set()
            for core in analysis["candidates"]:
                self.assertEqual(len(core["itemApis"]), 2)
                self.assertGreaterEqual(core["nearCount"], 2)
                _, result = tft.simulate(self.snap, unit, 2, core["itemApis"], "clump", [], dummy)
                for field in ("killTime", "total", "aliveTime", "survivalCapped",
                              "stressAliveTime", "stressCapped"):
                    self.assertEqual(core["spike"][field], result[field])
                near = {tuple(sorted(build["itemApis"])) for build in core["completions"]
                        if build["performance"]["status"] == "comparable"
                        and build["performance"]["lossPct"] <= analysis["thresholdPct"] + 1e-9}
                self.assertTrue(used_finishes.isdisjoint(near))
                used_finishes.update(near)
            with open(path) as f:
                self.assertEqual(json.load(f)["coreAnalysis"], analysis)

    def test_incomplete_pair_export_does_not_publish_a_cell(self):
        unit = self.snap.unit("Ashe")
        key = "s2-clump-bare"
        pool = [self.snap.item("Red Buff")["api"]]
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "pool_items", return_value=pool), \
                patch.object(tft.engine(), "score_pairs", return_value=[]):
            path = os.path.join(directory, "cell.json")
            with self.assertRaisesRegex(ValueError, "every legal pair"):
                tft.compute_cell(self.snap, unit, key, {("ashe", key): path}, prune=False)
            self.assertFalse(os.path.exists(path))

    def test_tank_cells_keep_single_item_recommendations_without_pair_fights(self):
        unit = self.snap.unit("Leona")
        key = "s2-clump-bare"
        pool = [self.snap.item("Warmog's Armor")["api"]]
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "pool_items", return_value=pool), \
                patch.object(tft.engine(), "score_pairs", side_effect=AssertionError("tank pair simulation")):
            cell = tft.compute_cell(self.snap, unit, key, {("leona", key): os.path.join(directory, "cell.json")},
                                    prune=False)
            self.assertEqual(cell["coreAnalysis"]["selectionMode"], "completions")
            self.assertEqual(cell["coreAnalysis"]["pairBuildsEvaluated"], 0)
            self.assertTrue(all(len(core["items"]) == 1 and "spike" not in core
                                for core in cell["coreAnalysis"]["candidates"]))


    def test_static_export_pins_one_snapshot_for_cells_and_core_contexts(self):
        key = "s2-clump-bare"
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = os.path.join(directory, "cell.json")
            payload = {"scenario": tft.SCENARIOS[key], "rows": [],
                       "coreAnalysis": {"coreStats": {"an-item-pair": {"nearCount": 2}}}}
            with open(path, "w") as f:
                json.dump(payload, f)
            paths = {("ahri", key): path}
            con = SimpleNamespace(execute=lambda *args: [])
            early_payload = {"schema": "tft-early-comps-v1", "set": self.snap.set_no,
                "patch": self.snap.patch, "revision": "early-export-fixture",
                "baselineRevision": tft.snapshot_revision(self.snap), "complete": True,
                "variants": [{"key": "level4", "results": [{"score": 55.0}]}]}
            for owner, name, value in (
                    (webapp, "db_connect", con), (webapp, "app_meta", {"jobs": [], "tiers": ["test"]}),
                    (webapp.builds, "api_builds_meta", {}), (webapp.builds, "warm", 0),
                    (webapp.builds, "cell_paths", {}), (webapp.ladder, "db_patches", []),
                    (webapp.ladder, "build_rows", []), (webapp.ladder, "api_onetricks", {}),
                    (webapp.builds_leaderboard, "cached_leaderboard", {}),
                    (tft, "cell_paths", paths),
                    (webapp.tft_early, "supports", True),
                    (webapp.tft_early, "revision", early_payload["revision"]),
                    (webapp.tft_early, "cached_catalog", early_payload)):
                stack.enter_context(patch.object(owner, name, return_value=value))
            early_payload["baselineRevision"] = tft.snapshot_revision(self.snap)
            # A second lookup could return a newly published patch. The entire
            # export must keep using the snapshot its metadata described.
            loaded = stack.enter_context(patch.object(tft, "load_snapshot", side_effect=[self.snap,
                         AssertionError("export reloaded the active snapshot")]))
            warmed = stack.enter_context(patch.object(tft, "warm", return_value=0))
            early_warmed = stack.enter_context(patch.object(webapp.tft_early, "warm", return_value=0))
            out = os.path.join(directory, "export")
            webapp.cmd_export(SimpleNamespace(out=out))
            loaded.assert_called_once_with()
            warmed.assert_called_once_with(snap=self.snap, prune=False)
            early_warmed.assert_called_once_with(snap=self.snap)
            with open(os.path.join(out, "api", "tft", "traits.json")) as f:
                flex = json.load(f)
            self.assertEqual((flex["set"], flex["patch"]), (self.snap.set_no, self.snap.patch))
            self.assertEqual(flex["schema"], "tft-trait-flex-v1")
            self.assertEqual({unit["api"] for unit in flex["units"]}, set(self.snap.units))
            self.assertNotIn("items", flex)
            with open(os.path.join(out, "api", "tft", "early.json")) as f:
                early = json.load(f)
            self.assertEqual((early["set"], early["patch"]), (self.snap.set_no, self.snap.patch))
            self.assertEqual(early["variants"], early_payload["variants"])
            self.assertEqual(early["baselineRevision"], early_payload["baselineRevision"])
            self.assertIn("teamPlanner", early)
            self.assertNotIn("teamPlanner", early_payload)
            self.assertNotIn("publicationRevision", early_payload)
            records = []
            for name in ("meta.json", "status.json", "ahri/cores.json"):
                with open(os.path.join(out, "api", "tft", name)) as f:
                    records.append(json.load(f))
            self.assertEqual(len({record["revision"] for record in records}), 1)
            self.assertEqual(records[-1]["scenarios"][0]["coreStats"], payload["coreAnalysis"]["coreStats"])
            with open(os.path.join(out, "api", "tft", "ahri", key + ".json")) as f:
                self.assertEqual(json.load(f), payload)
            for board_key in tft.leaderboard_scenarios():
                with open(os.path.join(out, "api", "tft", "leaderboard", board_key + ".json")) as f:
                    board = json.load(f)
                self.assertEqual(board["revision"], records[0]["revision"])
                self.assertEqual(board["selection"]["key"], board_key)
                self.assertFalse(board["complete"])
                self.assertEqual(board["readyCount"], 0)
            publication = records[0]["publicationRevision"]
            self.assertTrue(publication.startswith("g-"))
            self.assertEqual({record["publicationRevision"] for record in
                             (records[0], records[1], flex, early)}, {publication})
            self.assertEqual(flex["teamPlanner"], early["teamPlanner"])


class TestLeaderboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {}
        self.units = [self.snap.unit(name) for name in (
            "Akali", "Murkwolf", "Azir", "Ahri", "Ashe",
            "Leona", "Sejuani", "Amumu", "Taric")]
        self.enterContext(patch.object(tft, "modeled_units", return_value=self.units))
        self.enterContext(patch.object(tft, "snapshot_revision", return_value="test-revision"))
        self.items = [self.snap.item(name)["api"] for name in (
            "Deathblade", "Spear of Shojin", "Spear of Shojin")]

    def write(self, name, star=2, geometry="clump", traits="bare", threat="mixed", **metrics):
        unit = self.snap.unit(name)
        slug = tft.unit_slug(unit)
        key = f"s{star}-{geometry}-{traits}" + (f"-{threat}" if threat != "mixed" else "")
        performance = {"killTime": None, "total": 100, "dps": 5, "aliveTime": 20,
                       "survivalCapped": False, "stressAliveTime": None, "stressCapped": False,
                       **metrics}
        filename = str(Path(self.tmp.name) / f"{slug}-{key}.json")
        # A deliberately different rounded row catches using display data
        # or highest displayed DPS instead of the recorded optimal result.
        payload = {"rows": [{"killTime": 5.0, "dps": 999}],
                   "best": {"itemApis": self.items, "performance": performance},
                   "buildsEvaluated": 7770, "computedAt": "2026-09-06T00:00:00+00:00"}
        Path(filename).write_text(json.dumps(payload))
        self.paths[(slug, key)] = filename
        return payload

    def board(self, key="s2-clump-bare-mixed"):
        with patch.object(tft, "engine", side_effect=AssertionError("request-time simulation")):
            return tft.cached_leaderboard(key, self.paths, snap=self.snap)

    def ranked_payload(self, name="Azir", **metrics):
        """Two distinct builds whose display outcomes deliberately round equal."""
        payload = self.write(name, **metrics)
        payload["objective"] = self.snap.unit(name)["objective"]
        payload["rows"] = [{"items": self.item_names(self.items), "killTime": 5.0, "total": 100},
                           {"items": self.item_names(self.second_items()), "killTime": 5.0, "total": 100}]
        return payload

    def item_names(self, apis):
        return [self.snap.items[api]["name"] for api in apis]

    def second_items(self):
        return [self.items[0], self.items[0], self.items[1]]

    def exact_build(self, payload, apis=None, **metrics):
        apis = self.second_items() if apis is None else apis
        return {"items": self.item_names(apis), "itemApis": list(apis),
                "performance": {**payload["best"]["performance"], **metrics}}

    def legacy_runner(self, payload, **metrics):
        optimal = self.exact_build(payload, self.items)
        runner = self.exact_build(payload, **metrics)
        # Exact completions can be arbitrarily deep inside an old core record.
        payload["coreAnalysis"] = {"optimal": optimal,
                                   "candidates": [{"completions": [runner]}]}
        return runner

    def save_payload(self, payload, name="Azir"):
        path = self.paths[(tft.unit_slug(self.snap.unit(name)), "s2-clump-bare")]
        Path(path).write_text(json.dumps(payload))

    def test_highest_allowed_star_gives_one_winner_per_champion(self):
        for unit in self.units:
            self.write(unit["name"], max(tft.unit_stars(unit)))
        board = self.board("best-clump-bare-mixed")
        rows = board["damage"] + board["tanks"]
        self.assertTrue(board["complete"])
        self.assertEqual(board["readyCount"], len(self.units))
        self.assertEqual(len({row["unit"] for row in rows}), len(self.units))
        for row in rows:
            self.assertEqual(row["star"], 3 if row["cost"] <= 3 else 2)
            self.assertEqual(row["itemApis"], self.items)
            self.assertEqual(row["items"], [self.snap.items[a]["name"] for a in self.items])
            self.assertEqual(row["buildsEvaluated"], 7770)

    def test_three_star_four_and_five_costs_are_excluded_even_if_a_file_exists(self):
        for unit in self.units:
            self.write(unit["name"], 3)
        board = self.board("s3-clump-bare-mixed")
        rows = board["damage"] + board["tanks"]
        self.assertEqual(board["expectedCount"], 5)
        self.assertTrue(board["complete"])
        self.assertTrue(all(row["cost"] <= 3 and row["star"] == 3 for row in rows))

    def test_fixed_star_compares_only_that_star(self):
        self.write("Azir", 1, killTime=19)
        self.write("Azir", 2, killTime=10)
        self.write("Azir", 3, killTime=5)
        board = self.board("s1-clump-bare-mixed")
        self.assertEqual(board["damage"][0]["performance"]["killTime"], 19)
        self.assertEqual(board["damage"][0]["scenario"], "s1-clump-bare")

    def test_damage_uses_exact_clear_times_and_then_damage_for_nonclears(self):
        self.write("Azir", killTime=5.004, total=6240, dps=1247)
        self.write("Ahri", killTime=5.003, total=6240, dps=1247)
        self.write("Ashe", total=6100, dps=9999)
        self.write("Murkwolf", total=6200)
        rows = self.board()["damage"]
        self.assertEqual([row["unitName"] for row in rows], ["Ahri", "Azir", "Murkwolf", "Ashe"])
        self.assertEqual([row["rank"] for row in rows], [1, 2, 3, 4])
        self.assertEqual(rows[0]["performance"]["killTime"], 5.003)
        self.assertEqual(rows[0]["performance"]["dps"], 1247)

    def test_equal_damage_outcomes_share_competition_rank(self):
        self.write("Azir", killTime=5, total=6240)
        self.write("Ahri", killTime=5, total=6240)
        self.write("Ashe", killTime=6, total=6240)
        rows = self.board()["damage"]
        self.assertEqual([row["rank"] for row in rows], [1, 1, 3])
        self.assertEqual([row["unitName"] for row in rows[:2]], ["Ahri", "Azir"])

    def test_finite_winner_reports_its_equipped_form_and_pressure(self):
        self.units.append(self.snap.unit("Nidalee"))
        payload = self.write("Nidalee", killTime=5.5)
        payload["best"].update(form="AD", kind="Assassin", objective="fighter", range=1, pressure=True)
        Path(self.paths[("nidalee", "s2-clump-bare")]).write_text(json.dumps(payload))
        row = next(row for row in self.board()["damage"] if row["unit"] == "nidalee")
        self.assertEqual((row["form"], row["kind"], row["objective"], row["role"], row["range"], row["pressure"]),
                         ("AD", "Assassin", "fighter", "Attack Assassin", 1, True))
        self.assertEqual(row["performance"]["killTime"], 5.5)
        self.assertEqual(row["score"], [0, 5.5])

    def test_old_curve_payload_does_not_replace_the_finite_winner(self):
        payload = self.write("Azir", killTime=10, total=6240)
        payload["damageCurve"] = {"builds": [{"samples": [99999999]}]}
        Path(self.paths[("azir", "s2-clump-bare")]).write_text(json.dumps(payload))
        board = self.board()
        row = board["damage"][0]
        self.assertEqual(row["performance"]["killTime"], 10)
        self.assertEqual(row["performance"]["total"], 6240)
        self.assertEqual(row["score"], [0, 10])
        for key in ("damageCurve", "finiteDiagnostic", "evaluationModel"):
            self.assertNotIn(key, row)
        self.assertNotIn("damageEvaluationModel", board)
        self.assertNotIn("defaultDamageTime", board)

    def test_movement_assumption_and_exact_time_come_from_the_saved_cell(self):
        payload = self.write("Akali", killTime=8, movementTime=1.25, repositions=3)
        payload["scenario"] = {"dummy": {"meleeRepositionSeconds": 0.75}}
        path = Path(self.paths[("akali", "s2-clump-bare")])
        path.write_text(json.dumps(payload))
        row = self.board()["damage"][0]
        self.assertEqual(row["meleeRepositionSeconds"], 0.75)
        self.assertEqual(row["performance"]["movementTime"], 1.25)
        self.assertEqual(row["performance"]["repositions"], 3)
        del payload["scenario"]
        path.write_text(json.dumps(payload))
        self.assertNotIn("meleeRepositionSeconds", self.board()["damage"][0])

    def test_tanks_use_hold_time_and_double_pressure_with_double_caps_tied(self):
        self.write("Leona", aliveTime=60, survivalCapped=True, stressAliveTime=60,
                   stressCapped=True, total=200)
        self.write("Amumu", aliveTime=60, survivalCapped=True, stressAliveTime=60,
                   stressCapped=True, total=10000)
        self.write("Taric", aliveTime=60, survivalCapped=True, stressAliveTime=35)
        self.write("Sejuani", aliveTime=59.999)
        rows = self.board()["tanks"]
        self.assertEqual([row["unitName"] for row in rows], ["Amumu", "Leona", "Taric", "Sejuani"])
        self.assertEqual([row["rank"] for row in rows], [1, 1, 3, 4])
        self.assertTrue(all(row["objective"] == "tank" for row in rows))
        self.assertEqual(self.board()["damage"], [])

    def test_matching_formation_traits_and_tank_threat_are_selected(self):
        self.write("Azir", geometry="spread", traits="low", killTime=7)
        self.write("Azir", geometry="clump", traits="high", killTime=1)
        self.write("Leona", geometry="spread", traits="low", threat="magic", aliveTime=25)
        self.write("Leona", geometry="spread", traits="low", threat="physical", aliveTime=30)
        board = self.board("s2-spread-low-magic")
        self.assertEqual(board["damage"][0]["performance"]["killTime"], 7)
        self.assertEqual(board["damage"][0]["scenario"], "s2-spread-low")
        self.assertEqual(board["tanks"][0]["performance"]["aliveTime"], 25)
        self.assertEqual(board["tanks"][0]["scenario"], "s2-spread-low-magic")

    def test_missing_cells_are_pending_without_fallback_or_fake_zeroes(self):
        self.write("Azir", star=1)
        board = self.board()
        self.assertFalse(board["complete"])
        self.assertEqual(board["readyCount"], 0)
        self.assertEqual(board["expectedCount"], len(self.units))
        self.assertEqual(board["damage"], [])
        self.assertEqual(board["tanks"], [])
        self.assertEqual(len(board["pending"]), len(self.units))
        self.write("Azir", star=2, killTime=10)
        after = self.board()
        self.assertEqual(after["readyCount"], 1)
        self.assertEqual(len(after["pending"]), len(self.units) - 1)
        self.assertEqual(after["revision"], "test-revision")

    def test_cache_replacement_is_visible_and_returned_rows_are_independent(self):
        self.write("Azir", killTime=10)
        board = self.board()
        board["damage"][0]["items"].clear()
        board["damage"][0]["performance"]["killTime"] = -1
        self.assertEqual(self.board()["damage"][0]["performance"]["killTime"], 10)
        self.write("Azir", killTime=9.12345)
        self.assertEqual(self.board()["damage"][0]["performance"]["killTime"], 9.12345)

    def test_second_build_gap_uses_full_precision_and_prefers_new_exact_record(self):
        payload = self.ranked_payload(killTime=5.003, total=6240)
        self.legacy_runner(payload, killTime=5.9)
        payload["runnerUp"] = self.exact_build(payload, killTime=5.004)
        # Newly computed records need APIs and metrics, not redundant names.
        del payload["runnerUp"]["items"]
        result = tft.leaderboard_runner_up(payload)
        self.assertEqual(result["itemApis"], self.second_items())
        self.assertEqual(result["items"], self.item_names(self.second_items()))
        self.assertEqual(result["performance"]["killTime"], 5.004)
        self.assertEqual(result["performance"]["metric"], "killTime")
        self.assertEqual(result["performance"]["status"], "comparable")
        self.assertAlmostEqual(result["performance"]["lossPct"], 100 * (5.004 - 5.003) / 5.003)
        self.assertGreater(result["performance"]["lossPct"], 0)

    def test_legacy_second_build_preserves_duplicates_and_ignores_item_order(self):
        payload = self.ranked_payload(killTime=5.003, total=6240)
        runner = self.legacy_runner(payload, killTime=5.004)
        runner["items"].reverse()
        runner["itemApis"].reverse()
        # Same set of names, wrong multiplicity: it must not become the runner-up.
        decoy = self.exact_build(payload, self.items, killTime=10)
        payload["coreAnalysis"]["requiredItems"] = [{"without": decoy}]
        payload["coreAnalysis"]["optimal"]["itemApis"].reverse()
        result = tft.leaderboard_runner_up(payload)
        self.assertEqual(sorted(result["itemApis"]), sorted(self.second_items()))
        self.assertEqual(result["performance"]["killTime"], 5.004)
        self.assertAlmostEqual(result["performance"]["lossPct"], 100 * (5.004 - 5.003) / 5.003)

    def test_second_ranked_build_keeps_primary_ties_instead_of_skipping_to_a_lower_outcome(self):
        payload = self.ranked_payload(killTime=5, total=6240, rawTotal=9000)
        self.legacy_runner(payload, killTime=5, rawTotal=7000)
        third_apis = [self.snap.item("Blue Buff")["api"], self.items[1], self.items[1]]
        third = self.exact_build(payload, third_apis, killTime=6, rawTotal=6300)
        payload["rows"].append({"items": third["items"], "killTime": 6.0})
        payload["coreAnalysis"]["candidates"][0]["completions"].append(third)
        result = tft.leaderboard_runner_up(payload)
        self.assertEqual(result["itemApis"], self.second_items())
        self.assertEqual(result["performance"]["killTime"], 5)
        self.assertEqual(result["performance"]["lossPct"], 0)

    def test_nonclear_gap_uses_exact_damage_and_overkill_does_not_create_a_gap(self):
        payload = self.ranked_payload(total=100.004, rawTotal=1000)
        payload["runnerUp"] = self.exact_build(payload, total=100.003, rawTotal=500)
        result = tft.leaderboard_runner_up(payload)["performance"]
        self.assertEqual((result["metric"], result["status"], result["value"]),
                         ("damage", "comparable", 100.003))
        self.assertAlmostEqual(result["lossPct"], 100 * (100.004 - 100.003) / 100.004)
        payload["runnerUp"]["performance"]["total"] = 100.004
        self.assertEqual(tft.leaderboard_runner_up(payload)["performance"]["lossPct"], 0)

    def test_second_build_that_does_not_clear_has_a_censored_gap(self):
        payload = self.ranked_payload(killTime=5, total=6240)
        payload["runnerUp"] = self.exact_build(payload, killTime=None, total=6000)
        performance = tft.leaderboard_runner_up(payload)["performance"]
        self.assertEqual((performance["metric"], performance["status"]), ("killTime", "noClear"))
        self.assertIsNone(performance["lossPct"])
        self.assertIsNone(performance["value"])

    def test_tank_gaps_use_hold_time_stress_time_and_censored_survival_limits(self):
        cases = [
            ({"aliveTime": 30.005}, {"aliveTime": 29.995},
             "aliveTime", "comparable", 100 * (30.005 - 29.995) / 30.005),
            ({"aliveTime": 60, "survivalCapped": True, "stressAliveTime": 40.004},
             {"stressAliveTime": 39.996}, "stressAliveTime", "comparable",
             100 * (40.004 - 39.996) / 40.004),
            ({"aliveTime": 60, "survivalCapped": True, "stressAliveTime": 40},
             {"aliveTime": 59.999, "survivalCapped": False, "stressAliveTime": None},
             "stressAliveTime", "shortSurvival", None),
            ({"aliveTime": 60, "survivalCapped": True, "stressAliveTime": 60, "stressCapped": True},
             {"total": 1}, "capped", "comparable", 0),
            ({"aliveTime": 60, "survivalCapped": True, "stressAliveTime": 60, "stressCapped": True},
             {"stressAliveTime": 59.999, "stressCapped": False}, "capped", "belowCap", None),
        ]
        for first, second, metric, status, expected_gap in cases:
            with self.subTest(metric=metric, status=status):
                payload = self.ranked_payload("Leona", **first)
                payload["runnerUp"] = self.exact_build(payload, **second)
                performance = tft.leaderboard_runner_up(payload)["performance"]
                self.assertEqual((performance["metric"], performance["status"]), (metric, status))
                if expected_gap is None:
                    self.assertIsNone(performance["lossPct"])
                else:
                    self.assertAlmostEqual(performance["lossPct"], expected_gap)

    def test_old_cells_without_an_exact_second_score_do_not_use_rounded_rows(self):
        payload = self.ranked_payload(killTime=5.003, total=6240)
        self.assertIsNone(tft.leaderboard_runner_up(payload))
        payload["coreAnalysis"] = {"optimal": self.exact_build(payload, self.items)}
        self.assertIsNone(tft.leaderboard_runner_up(payload))
        payload["coreAnalysis"]["candidates"] = [{"completions": [{
            "items": self.item_names(self.second_items()), "itemApis": self.second_items()}]}]
        self.assertIsNone(tft.leaderboard_runner_up(payload))
        self.save_payload(payload)
        self.assertNotIn("secondBest", self.board()["damage"][0])
        single_row = self.write("Azir", killTime=5.003)
        self.assertIsNone(tft.leaderboard_runner_up(single_row))
        self.assertNotIn("secondBest", self.board()["damage"][0])

    def test_mismatched_optimal_and_runner_identities_are_rejected(self):
        payload = self.ranked_payload(killTime=5, total=6240)
        self.legacy_runner(payload, killTime=6)
        payload["coreAnalysis"]["optimal"]["itemApis"] = self.second_items()
        with self.assertRaisesRegex(ValueError, "optimal build"):
            tft.leaderboard_runner_up(payload)

        payload = self.ranked_payload(killTime=5, total=6240)
        for change in ({"itemApis": self.items}, {"items": self.item_names(self.items)}):
            with self.subTest(change=change):
                payload["runnerUp"] = {**self.exact_build(payload, killTime=6), **change}
                with self.assertRaisesRegex(ValueError, "second-ranked build"):
                    tft.leaderboard_runner_up(payload)
        payload["runnerUp"] = self.exact_build(payload, killTime=4)
        with self.assertRaisesRegex(ValueError, "ahead of its optimal"):
            tft.leaderboard_runner_up(payload)
        payload["rows"][1]["items"] = list(reversed(payload["rows"][0]["items"]))
        with self.assertRaisesRegex(ValueError, "repeats the optimal"):
            tft.leaderboard_runner_up(payload)

    def test_cached_runner_metrics_are_exact_and_returned_rows_are_independent(self):
        payload = self.ranked_payload(killTime=5.003, total=6240)
        payload["runnerUp"] = self.exact_build(payload, killTime=5.004)
        self.save_payload(payload)
        row = self.board()["damage"][0]
        self.assertEqual(row["secondBest"]["performance"]["killTime"], 5.004)
        self.assertAlmostEqual(row["secondBest"]["performance"]["lossPct"], 100 * (5.004 - 5.003) / 5.003)
        row["secondBest"]["items"].clear()
        row["secondBest"]["itemApis"].clear()
        row["secondBest"]["performance"]["lossPct"] = -1
        fresh = self.board()["damage"][0]["secondBest"]
        self.assertEqual(fresh["items"], self.item_names(self.second_items()))
        self.assertEqual(fresh["itemApis"], self.second_items())
        self.assertGreater(fresh["performance"]["lossPct"], 0)
        payload["runnerUp"]["performance"]["killTime"] = 5.01
        self.save_payload(payload)
        self.assertEqual(self.board()["damage"][0]["secondBest"]["performance"]["killTime"], 5.01)

    def test_old_cache_cannot_silently_fall_back_to_rounded_results(self):
        payload = self.write("Azir")
        del payload["best"]
        Path(self.paths[("azir", "s2-clump-bare")]).write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "exact leaderboard result"):
            self.board()

    def test_invalid_scenarios_fail_before_any_cache_read(self):
        for key in ("s4-clump-bare-mixed", "best-clump-any-mixed", "s2-spread-low-other", "../meta"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "unknown leaderboard"):
                self.board(key)
        self.assertEqual(len(tft.leaderboard_scenarios()), 72)

    def test_computed_winner_preserves_the_actual_unrounded_result(self):
        # Small pool, real enumeration: verify the extra cache record is
        # the same winning build and fight, without changing display rows.
        unit = self.snap.unit("Azir")
        key = "s2-clump-bare"
        filename = str(Path(self.tmp.name) / "actual.json")
        pool = [self.snap.item(name)["api"] for name in ("Blue Buff", "Spear of Shojin")]
        with patch.object(tft, "pool_items", return_value=pool):
            result = tft.compute_cell(self.snap, unit, key, {("azir", key): filename}, prune=False)
        best = result["best"]
        _, fight = tft.simulate(self.snap, unit, 2, best["itemApis"], "clump", [], tft.dummies_for(self.snap))
        self.assertEqual(best["performance"], {field: fight[field] for field in best["performance"]})
        self.assertEqual(result["rows"][0]["items"], [self.snap.items[a]["name"] for a in best["itemApis"]])
        kill_time = best["performance"]["killTime"]
        self.assertEqual(result["rows"][0]["killTime"], None if kill_time is None else round(kill_time, 2))
        self.assertEqual(result["rows"][0]["total"], round(best["performance"]["total"]))

        # Every newly computed cell retains the real second ranked build even
        # if it would not survive the older core suggestions' display limits.
        runner = result["runnerUp"]
        self.assertEqual(json.loads(Path(filename).read_text())["runnerUp"], runner)
        self.assertNotEqual(sorted(runner["itemApis"]), sorted(best["itemApis"]))
        self.assertEqual(result["rows"][1]["items"], self.item_names(runner["itemApis"]))
        _, second_fight = tft.simulate(self.snap, unit, 2, runner["itemApis"], "clump", [],
                                       tft.dummies_for(self.snap))
        self.assertEqual(runner["performance"], {field: second_fight[field] for field in runner["performance"]})
        comparison = tft.leaderboard_runner_up(result)
        self.assertEqual(comparison["itemApis"], runner["itemApis"])
        self.assertEqual(comparison["performance"]["killTime"], second_fight["killTime"])
        without_cores = copy.deepcopy(result)
        del without_cores["coreAnalysis"]
        self.assertEqual(tft.leaderboard_runner_up(without_cores), comparison)


def compact_row(row):
    combo, _, res = row
    return (combo, res["killTime"], res["total"], res["aliveTime"],
            res["survivalCapped"], res["stressAliveTime"], res["stressCapped"])


def item_pool(spec, names):
    unit = SNAP.units[spec["unit"]["api"]]
    spec["pool"] = [tft.item_spec(SNAP, SNAP.item(name)["api"], ITEM_FX, unit)
                    for name in names]
    return spec


class TestCellScores(unittest.TestCase):
    def test_all_scores_match_full_rich_results_for_each_objective(self):
        items = ["Infinity Edge", "Guinsoo's Rageblade", "Jeweled Gauntlet",
                 "Nashor's Tooth", "Warmog's Armor"]
        for name in ("Ashe", "Warwick", "Leona"):
            with self.subTest(unit=name):
                spec = item_pool(spec_for(name), items)
                count, full = ENGINE.run_cell(spec, 1000, 1)
                analyzed, rows, scores = ENGINE.analyze_cell(spec, 3, 1)
                self.assertEqual(analyzed, count)
                self.assertEqual(rows, full[:3])
                self.assertEqual(scores, [compact_row(row) for row in full])
                self.assertEqual(len(scores), count)
                self.assertGreater(count, len(rows))
                # These values must stay unrounded for small substitution
                # penalties; the dashboard rounds only for display.
                self.assertTrue(any(s[2] != round(s[2], 2) for s in scores))

    def test_capped_and_measured_tanks_keep_the_stress_measurements(self):
        spec = body_spec(hp=400.0, duration=6.5)
        spec["pool"] = [
            {"api": "health", "name": "health", "unique": False,
             "stats": [["hp", 300.0]], "adds": []},
            {"api": "damage", "name": "damage", "unique": False,
             "stats": [["adPct", 10.0]], "adds": []},
        ]
        count, rows, scores = ENGINE.analyze_cell(spec, 4, 1)
        self.assertEqual(count, 4)
        self.assertEqual(scores, [compact_row(row) for row in rows])
        by_combo = {tuple(score[0]): score for score in scores}
        self.assertEqual(by_combo[(0, 0, 0)][3:], (6.5, True, 6.5, True))
        self.assertEqual(by_combo[(0, 0, 1)][3:], (6.5, True, 5.0, False))
        self.assertEqual(by_combo[(1, 1, 1)][3:], (4.0, False, None, False))
        self.assertEqual((count, rows), ENGINE.run_cell(spec, 4, 1))

    def test_duplicates_unique_constraints_and_api_ties_survive_export(self):
        spec = spec_for("Ashe", duration=0.1)
        # Identical effects force the API tuple tie-break. Pool order is
        # deliberately different from API order, and only one item is unique.
        spec["pool"] = [
            {"api": api, "name": api, "unique": unique, "stats": [], "adds": []}
            for api, unique in (("z", False), ("a", True), ("m", False))
        ]
        legal = [c for c in itertools.combinations_with_replacement(range(3), 3)
                 if c.count(1) <= 1]
        expected = sorted(legal, key=lambda c: tuple(spec["pool"][i]["api"] for i in c))
        count, rows, scores = ENGINE.analyze_cell(spec, 2, 1)
        self.assertEqual(count, len(expected))
        self.assertEqual([tuple(score[0]) for score in scores], expected)
        self.assertEqual(rows, ENGINE.run_cell(spec, 2, 1)[1])
        self.assertIn((0, 0, 0), expected)
        self.assertIn((2, 2, 2), expected)
        self.assertNotIn((1, 1, 1), expected)

    def test_scores_and_rows_are_independent_of_worker_count(self):
        spec = spec_for("Ashe")
        unit = SNAP.unit("Ashe")
        spec["pool"] = [tft.item_spec(SNAP, api, ITEM_FX, unit)
                        for api in tft.pool_items(SNAP, ITEM_FX)]
        expected = ENGINE.analyze_cell(spec, 7, 1)
        self.assertGreater(expected[0], 7000)
        for workers in (4, 0):
            with self.subTest(workers=workers):
                self.assertEqual(ENGINE.analyze_cell(spec, 7, workers), expected)

    def test_top_limits_only_rich_rows_including_zero(self):
        spec = item_pool(spec_for("Ashe"), ["Nashor's Tooth", "Jeweled Gauntlet"])
        count, all_rows, all_scores = ENGINE.analyze_cell(spec)
        self.assertEqual(len(all_rows), count)
        for top in (0, 1, count, count + 1):
            with self.subTest(top=top):
                analyzed, rows, scores = ENGINE.analyze_cell(spec, top, 1)
                self.assertEqual(analyzed, count)
                self.assertEqual(rows, all_rows[:top])
                self.assertEqual(scores, all_scores)

    def test_no_legal_builds_return_empty_rows_and_scores(self):
        spec = spec_for("Ashe")
        for pool in ([], [{"api": "only", "name": "only", "unique": True,
                           "stats": [], "adds": []}]):
            with self.subTest(pool=pool):
                spec["pool"] = pool
                self.assertEqual(ENGINE.analyze_cell(spec), (0, [], []))
                self.assertEqual(ENGINE.run_cell(spec), (0, []))


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

    def test_presets_share_six_sources_and_a_calibrated_damage_budget(self):
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
                self.assertEqual(len(slots), 6)
                self.assertEqual([s["nearby"] for s in slots], [True] * 4 + [False] * 2)
                self.assertEqual([s["kind"] for s in slots], ["tank"] * 4 + ["non-tank"] * 2)
                self.assertEqual(dummy["board"], [1] * 6)
                profile = profiles[key]
                front_dps = sum(s["ad"] * dummy["critEv"] * s["as"]
                                + s["ability"] / s["castInterval"] for s in slots[:4])
                attack_dps = sum(s["ad"] * dummy["critEv"] * s["as"] for s in slots[4:])
                spell_dps = sum(s["ability"] / s["castInterval"] for s in slots[4:])
                self.assertAlmostEqual(front_dps + attack_dps + spell_dps, baseline_dps)
                self.assertAlmostEqual(front_dps, profile["frontlineDps"])
                self.assertAlmostEqual(attack_dps + spell_dps, profile["backlineDps"])
                self.assertAlmostEqual(attack_dps / profile["backlineDps"], attack_share)
                self.assertTrue(all(s["physicalShare"] == spell_physical_share for s in slots[4:]))
                self.assertEqual(profile["dps"], baseline_dps)
                self.assertEqual(profile["attackers"], 6)
                self.assertEqual(profile["frontlineAttackers"], 4)
                self.assertEqual(profile["backlineAttackers"], 2)
                # Presets change the carry damage mix, keeping the same frontline.
                if frontline is None:
                    frontline = slots[:4]
                self.assertEqual(slots[:4], frontline)
                spec = spec_for("Leona", dummy=dummy)
                self.assertEqual([s["streams"] for s in spec["dummies"]["slots"]], [1] * 6)

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
                # the calibration is a production fight: cast timelines included
                spec = spec_for(ref["name"], star=2, items=ref["items"], timed=True,
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
                casts = [e for e in events(res, "take", "magic") if e[3] >= 4]
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
        rest = [0] * (tft.FRONTLINERS + tft.BACKLINERS - 1)
        self.assertEqual(res["dummyAttacks"], [0] + rest)
        self.assertEqual(res["dummyCasts"], [3] + rest)
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
                self.assertEqual(res["dummyCasts"], [len(expected)]
                                 + [0] * (tft.FRONTLINERS + tft.BACKLINERS - 1))
                self.assertEqual(res["denied"], 0.0)
                self.assertEqual(res["taken"], 100.0 * len(expected))


class TestTankFormation(unittest.TestCase):
    def test_hecarim_stuns_three_fronts_while_backline_attacks_and_casts(self):
        for geometry in ("clump", "spread"):
            with self.subTest(geometry=geometry):
                spec = opening_cast(body_spec(unit="Hecarim", driver="Hecarim",
                                              hp=10 ** 6, duration=1.6,
                                              dummy=formation_sources()))
                tft.set_geometry(spec, geometry)
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
                tft.set_geometry(spec, geometry)
                spec["immortal"] = False
                # The opening auto removes the first tank before riders select
                # targets; the fourth slot is now one of the nearest three.
                spec["dummies"]["slots"][0]["hp"] = 1.0
                _, res = ENGINE.simulate(spec, True)
                self.assertEqual([e[3] for e in events(res, "kill")], [0])
                self.assertEqual([e[3] for e in events(res, "damage", "riders")], [1, 2, 3])
                self.assertEqual(res["dummyCasts"], [0, 0, 0, 0, 1])
                self.assertEqual({e[3] for e in events(res, "take")}, {4})

    def test_an_area_reaches_what_its_own_radius_covers(self):
        """The formation, not a flag, decides what a local area reaches.

        Rek'Sai's lunge is one hex (LungeHexRadius) and Amumu's cast two
        (BigHexRadius). Clumped the frontline stands at lanes 2/3/4 and the
        backline at lanes 2/4 two rows behind, so one hex covers the target
        and its neighbour while two also reach the far frontliner and the
        backliner directly behind the target. Spread the frontline is three
        hexes apart and neither radius leaves the target. Every source the
        area does not reach keeps firing.
        """
        for unit, driver, source, reach in (("Amumu", "Amumu", "ability",
                                             {"clump": [0, 1, 2, 3], "spread": [0]}),
                                            ("RekSai", "RekSai", "uproot",
                                             {"clump": [0, 1], "spread": [0]})):
            for geometry, targets in reach.items():
                with self.subTest(unit=unit, geometry=geometry):
                    spec = opening_cast(body_spec(unit=unit, driver=driver, hp=10 ** 6,
                                                  duration=1.1,
                                                  dummy=formation_sources(start=0.75)))
                    tft.set_geometry(spec, geometry)
                    _, res = ENGINE.simulate(spec, True)
                    self.assertEqual([e[3] for e in events(res, "damage", source)], targets)
                    self.assertEqual(res["dummyCasts"], [0 if i in targets else 1
                                                         for i in range(5)])
                    for backliner in (3, 4):
                        if backliner in targets:
                            continue
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
                tft.set_geometry(spec, geometry)
                for slot in spec["dummies"]["slots"]:
                    slot.update(manaMax=50.0, mr=0.0)
                _, res = ENGINE.simulate(spec, True)
                self.assertEqual(res["dummyCasts"], [1] * 5)
                sparks = events(res, "damage", "ionic spark")
                self.assertEqual([e[3] for e in sparks], [0, 1, 2])
                self.assertEqual([e[2] for e in sparks], [100.0] * 3)
                self.assertEqual({e[3] for e in events(res, "take", "magic")}, set(range(5)))

    def test_optional_nearby_defaults_to_legacy_all_nearby(self):
        # A null lane opts the board out of the formation, so this exercises
        # the nearby flag alone: without positions a local area still covers
        # every nearby slot, exactly as before.
        spec = opening_cast(body_spec(unit="Amumu", driver="Amumu", hp=10 ** 6,
                                      duration=1.1, dummy=formation_sources(start=0.75)))
        for slot in spec["dummies"]["slots"]:
            slot["nearby"] = True
            slot["lane"] = None
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


BASELINE = {"sunder": 0.3, "shred": 0.3}


def target_spec(unit="Ashe", *, debuffs=None, **kwargs):
    kwargs.setdefault("dummy", immortal(DUMMY))
    spec = copy.deepcopy(spec_for(unit, **kwargs))
    # Set the engine input explicitly, independent of benchmark defaults.
    spec["targetDebuffs"] = dict(BASELINE if debuffs is None else debuffs)
    return spec


def burst_spec(dtype, *, debuffs=None, fx=()):
    """A flat five-target cast, including two slots outside local aura range."""
    spec = target_spec("Hecarim", debuffs=debuffs, duration=0.4,
                       pressure=False, fx=fx)
    spec["dummies"]["slots"] = [
        dict(spec["dummies"]["slots"][0], hp=10 ** 6,
             armor=100.0 + i * 10, mr=200.0 + i * 20,
             nearby=i < 3, kind="tank" if i < 3 else "non-tank")
        for i in range(5)
    ]
    kit = spec["kits"]["base"]
    kit["stats"].update(initialMana=10 ** 6, mana=10 ** 6)
    kit["rows"]["NumEnemies"] = 5.0
    kit["calcs"]["MagicDamageCalc1"] = {
        "dtype": dtype,
        "terms": [{"type": "flat", "value": 170.0, "op": "add"}],
    }
    return spec


class TestTargetDebuffs(unittest.TestCase):
    def test_first_attack_uses_permanent_sunder(self):
        spec = target_spec(driver="Driver", duration=0.1)
        sheet, res = ENGINE.simulate(spec, True)
        hit = events(res, "damage", "auto")[0]
        self.assertEqual(hit[0], 0.0)
        self.assertAlmostEqual(hit[2], 112.5 * 1.1 * 100 / (100 + 110 * 0.7))
        self.assertEqual(sheet["armor"], 45.0)
        self.assertEqual(sheet["mr"], 45.0)

    def test_each_damage_type_uses_its_own_debuff_on_every_target(self):
        # Different armor/MR and fractions catch crossed fields, even on the
        # distant non-tanks that local item auras cannot reach.
        for dtype, resist, debuff in (("physical", "armor", "sunder"),
                                      ("magic", "mr", "shred")):
            for debuffs in ({}, {"sunder": 0.3}, {"shred": 0.25},
                            {"sunder": 0.3, "shred": 0.25},
                            {"sunder": 1.0, "shred": 1.0}):
                with self.subTest(dtype=dtype, debuffs=debuffs):
                    spec = burst_spec(dtype, debuffs=debuffs)
                    _, res = ENGINE.simulate(spec, True)
                    hits = events(res, "damage", "riders")
                    self.assertEqual([e[3] for e in hits], list(range(5)))
                    for hit, slot in zip(hits, spec["dummies"]["slots"]):
                        expected = 170 * 100 / (100 + slot[resist]
                                               * (1 - debuffs.get(debuff, 0)))
                        self.assertAlmostEqual(hit[2], expected)

    def test_true_damage_ignores_both_debuffs(self):
        for debuffs in ({}, BASELINE, {"sunder": 1.0, "shred": 1.0}):
            with self.subTest(debuffs=debuffs):
                _, res = ENGINE.simulate(burst_spec("true", debuffs=debuffs), True)
                self.assertEqual([e[2] for e in events(res, "damage", "riders")],
                                 [170.0] * 5)

    def test_equal_or_weaker_on_hit_and_aura_effects_do_not_stack(self):
        for unit, effect in (("Ashe", "sunder"), ("Ahri", "shred")):
            plain = target_spec(unit, duration=12.0)
            expected = ENGINE.simulate(plain, True)
            for pct in (0.15, 0.3):
                variants = [{effect + "OnHit": [pct, 3.0]},
                            {effect + "Aura": pct},
                            {effect + "OnHit": [pct, 3.0], effect + "Aura": pct}]
                for extra in variants:
                    with self.subTest(unit=unit, extra=extra):
                        spec = target_spec(unit, duration=12.0, fx=[extra])
                        self.assertEqual(ENGINE.simulate(spec, True), expected)

    def test_stronger_aura_overrides_baseline_only_within_its_range(self):
        for dtype, resist, aura in (("physical", "armor", "sunderAura"),
                                    ("magic", "mr", "shredAura")):
            with self.subTest(dtype=dtype):
                spec = burst_spec(dtype, fx=[{aura: 0.5}])
                _, res = ENGINE.simulate(spec, True)
                hits = events(res, "damage", "riders")
                self.assertEqual(len(hits), 5)
                for hit, slot in zip(hits, spec["dummies"]["slots"]):
                    pct = 0.5 if slot["nearby"] else 0.3
                    self.assertAlmostEqual(hit[2], 170 * 100 / (100 + slot[resist] * (1 - pct)))

    def test_stronger_timed_effect_refreshes_only_at_known_native_periods(self):
        for dtype, resist, effect in (("physical", "armor", "sunderOnHit"),
                                      ("magic", "mr", "shredOnHit")):
            for known_period in (False, True):
                with self.subTest(dtype=dtype, known_period=known_period):
                    spec = target_spec(duration=2.0, geometry="spread",
                                       fx=[{effect: [0.6, 0.75]}])
                    kit = spec["kits"]["base"]
                    # Integration alone cannot refresh a debuff. A known
                    # half-second trail period can keep the opening one alive.
                    kit["dotCritPeriods"] = {"trail": 0.5} if known_period else {}
                    kit["stats"].update(initialMana=10 ** 6, mana=10 ** 6)
                    kit["stats"]["as"] = 0.01
                    kit["rows"].update(RiftDuration=3.0, MaxHealthDamagePerSecond=0.0)
                    kit["calcs"]["PhysicalDamageCalc2"] = {
                        "dtype": dtype,
                        "terms": [{"type": "flat", "value": 170.0, "op": "add"}],
                    }
                    expected = ENGINE.simulate(spec, True)
                    _, res = expected
                    self.assertEqual(res["attacks"], 1)
                    self.assertEqual(res["casts"], 1)
                    hits = events(res, "damage", "trail", target=0)
                    self.assertEqual([e[0] for e in hits], [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0])
                    resistance = spec["dummies"]["slots"][0][resist]
                    for hit in hits:
                        pct = 0.6 if known_period or hit[0] < 1.0 else 0.3
                        self.assertAlmostEqual(hit[2], 170 * 0.25 * 100
                                               / (100 + resistance * (1 - pct)))
                    # A redundant item cannot donate its longer duration or
                    # the aura's unlimited duration to the stronger effect.
                    for pct in (0.15, 0.3):
                        for extra in ({effect: [pct, 5.0]},
                                      {effect.replace("OnHit", "Aura"): pct},
                                      {effect: [pct, 5.0], effect.replace("OnHit", "Aura"): pct}):
                            with self.subTest(dtype=dtype, known_period=known_period, extra=extra):
                                redundant = copy.deepcopy(spec)
                                redundant["items"].append({"api": "test", "name": "test", **extra})
                                self.assertEqual(ENGINE.simulate(redundant, True), expected)

    def test_last_whisper_and_void_staff_keep_stats_without_extra_reduction(self):
        for unit, name, effect, stat in (
                ("Ashe", "Last Whisper", "sunderOnHit", "ad"),
                ("Ahri", "Void Staff", "shredOnHit", "ap")):
            for count in (1, 2):
                with self.subTest(item=name, count=count):
                    spec = target_spec(unit, items=[name] * count, duration=12.0)
                    full = ENGINE.simulate(spec, True)
                    without_passive = copy.deepcopy(spec)
                    for item in without_passive["items"]:
                        self.assertEqual(item.pop(effect)[0], 0.3)
                    self.assertEqual(ENGINE.simulate(without_passive, True), full)
                    bare = target_spec(unit, duration=12.0)
                    bare_sheet, bare_res = ENGINE.simulate(bare, True)
                    self.assertGreater(full[0][stat], bare_sheet[stat])
                    self.assertGreater(full[1]["total"], bare_res["total"])
                    # With no team-applied baseline their real passive still
                    # contributes, so equality above cannot pass with inert FX.
                    spec["targetDebuffs"] = {}
                    without_passive["targetDebuffs"] = {}
                    self.assertGreater(ENGINE.simulate(spec, False)[1]["total"],
                                       ENGINE.simulate(without_passive, False)[1]["total"])

    def test_kayles_native_shred_is_redundant_and_does_not_extend_stronger_shred(self):
        for level in (20.0, 30.0):
            for fx in ([], [{"shredOnHit": [0.6, 0.25]}]):
                with self.subTest(level=level, fx=fx):
                    spec = target_spec("Kayle", duration=3.0, fx=fx)
                    spec["kits"]["base"]["rows"]["ShredLevel"] = level
                    # Solar damage from the next auto lands before its on-hit
                    # reapplies stronger Shred, exposing any extended duration.
                    spec["traits"].append({"name": "test", "bonusMagicPct": 0.2})
                    suppressed = copy.deepcopy(spec)
                    suppressed["kits"]["base"]["rows"].update(ShredLevel=0.0,
                                                               ShredDuration=0.0)
                    expected = ENGINE.simulate(suppressed, True)
                    self.assertGreater(expected[1]["attacks"], 1)
                    self.assertEqual(ENGINE.simulate(spec, True), expected)

    def test_target_debuffs_do_not_reduce_the_simulated_unit(self):
        spec = target_spec("Warwick", driver="Driver", duration=1.1,
                           dummy=one_hitter(100.0), pressure=True)
        spec["enemyDebuffs"] = {"sunder": 0.2, "shred": 0.1}
        spec["kits"]["base"]["stats"].update(armor=100.0, mr=200.0)
        sheet, res = ENGINE.simulate(spec, True)
        spec["targetDebuffs"] = {}
        clean_sheet, clean_res = ENGINE.simulate(spec, True)
        self.assertEqual(sheet, clean_sheet)
        self.assertEqual(events(res, "take"), events(clean_res, "take"))
        self.assertAlmostEqual(res["taken"], 100 * 100 / (100 + 100 * 0.8))
        self.assertGreater(res["total"], clean_res["total"])

    def test_absent_target_debuffs_preserve_legacy_on_hit_timing(self):
        spec = target_spec(debuffs={}, driver="Driver", duration=1.5,
                           fx=[{"sunderOnHit": [0.3, 3.0]}])
        expected = ENGINE.simulate(spec, True)
        spec.pop("targetDebuffs")
        self.assertEqual(ENGINE.simulate(spec, True), expected)
        hits = events(expected[1], "damage", "auto")
        self.assertAlmostEqual(hits[0][2], 112.5 * 1.1 * 100 / 210)
        self.assertAlmostEqual(hits[1][2], 112.5 * 1.1 * 100 / (100 + 110 * 0.7))

    def test_debuff_fractions_reject_invalid_values(self):
        for key in ("sunder", "shred"):
            for value in (-0.01, 1.01, float("nan"), float("inf"), float("-inf")):
                with self.subTest(key=key, value=value):
                    spec = target_spec(debuffs={key: value}, duration=0.1)
                    with self.assertRaisesRegex(ValueError, "targetDebuffs\\." + key):
                        ENGINE.simulate(spec, False)


class TestTftUiMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        with patch.object(tft, "load_snapshot", return_value=cls.snap):
            cls.meta = tft.api_meta()

    def test_complete_icon_and_trait_mapping_for_current_roster(self):
        self.assertEqual(len(self.meta["units"]), 65)
        self.assertEqual(len(self.meta["traits"]), 36)
        traits = {t["api"]: t for t in self.meta["traits"]}
        for record in self.meta["units"] + self.meta["traits"]:
            self.assertTrue(record["icon"].startswith("https://raw.communitydragon.org/latest/game/assets/"))
            self.assertTrue(record["icon"].endswith(".png"))
            self.assertEqual(record["icon"], record["icon"].lower())
        for unit in self.meta["units"]:
            self.assertEqual(unit["traitApis"], self.snap.units[unit["api"]]["traitApis"])
            self.assertEqual([traits[api]["name"] for api in unit["traitApis"]], unit["traits"])
            self.assertEqual(unit["traitBonuses"]["bare"], [])
            for context in ("low", "high"):
                for bonus in unit["traitBonuses"][context]:
                    self.assertIn(bonus["api"], unit["traitApis"])
                    self.assertIn(bonus["breakpoint"], traits[bonus["api"]]["levels"])
                    self.assertTrue(traits[bonus["api"]]["modeled"])

    def test_item_recipes_preserve_component_identity_and_duplicate_ingredients(self):
        for item in self.meta["items"]:
            with self.subTest(item=item["name"]):
                recipe = self.snap.items[item["api"]]["composition"]
                self.assertEqual([c["api"] for c in item["components"]], recipe)
                self.assertEqual([c["name"] for c in item["components"]],
                                 [self.snap.items[api]["name"] for api in recipe])
                self.assertEqual(len(recipe), 2)
        blue = next(item for item in self.meta["items"] if item["name"] == "Blue Buff")
        self.assertEqual(blue["components"][0], blue["components"][1])
        self.assertIn("Tear", blue["components"][0]["name"])

    def test_tank_threats_and_debuffs_match_the_simulation_inputs(self):
        self.assertEqual(self.meta["tankDebuffs"], {"wound": .33, "sunder": .3, "shred": .3})
        for threat in self.meta["tankThreats"]:
            dummy = tft.dummies_for(self.snap, threat=threat["key"])
            self.assertEqual(dummy["threat"], threat)
            self.assertEqual(dummy["enemyDebuffs"], self.meta["tankDebuffs"])
            self.assertEqual(self.meta["tankDummies"][threat["key"]], dummy)
            self.assertEqual([s["line"] for s in dummy["slots"]],
                             ["frontline"] * 4 + ["backline"] * 2)
        tank_keys = set(tft.unit_scenarios(self.snap.unit("Leona")))
        self.assertIn("s2-clump-bare", tank_keys)
        self.assertIn("s2-clump-bare-physical", tank_keys)
        self.assertIn("s2-clump-bare-magic", tank_keys)

    def test_offensive_targets_have_team_resistance_reduction_from_corrected_rows(self):
        expected = {"sunder": .3, "shred": .3}
        self.assertEqual(tft.target_debuffs(self.snap), expected)
        self.assertEqual(self.meta["targetDebuffs"], expected)
        self.assertEqual(self.meta["dummy"]["targetDebuffs"], expected)
        for name in ("Ahri", "Ashe", "Warwick"):
            with self.subTest(unit=name):
                spec = tft.cell_spec(self.snap, self.snap.unit(name), 2, "clump", [], self.meta["dummy"])
                self.assertEqual(spec["targetDebuffs"], expected)
                # All slots retain base defenses; the engine applies the
                # shared status once, including the rear non-tank target.
                self.assertEqual([s["armor"] for s in spec["dummies"]["slots"]],
                                 [110, 45, 45, 45, 40, 40])
                self.assertEqual([s["mr"] for s in spec["dummies"]["slots"]],
                                 [110, 45, 45, 45, 40, 40])
        for threat, dummy in self.meta["tankDummies"].items():
            with self.subTest(threat=threat):
                self.assertEqual(dummy["targetDebuffs"], {})
                spec = tft.cell_spec(self.snap, self.snap.unit("Leona"), 2, "clump", [], dummy)
                self.assertEqual(spec["targetDebuffs"], {})
                self.assertEqual(spec["enemyDebuffs"], self.meta["tankDebuffs"])

    def test_custom_offensive_fights_default_to_debuffs_but_can_isolate_mechanics(self):
        unit = self.snap.unit("Ahri")
        custom = {"slots": [{"hp": 1000, "armor": 100, "mr": 100, "kind": "tank"}]}
        spec = tft.cell_spec(self.snap, unit, 2, "spread", [], custom)
        self.assertEqual(spec["targetDebuffs"], {"sunder": .3, "shred": .3})
        for override in ({}, {"sunder": .2, "shred": .4}):
            spec = tft.cell_spec(self.snap, unit, 2, "spread", [], dict(custom, targetDebuffs=override))
            self.assertEqual(spec["targetDebuffs"], override)

    def test_recomputing_mixed_preserves_other_threat_caches(self):
        unit = self.snap.unit("Leona")
        key = "s2-clump-bare"
        pool = tft.pool_items(self.snap, tft.load_item_effects(self.snap.set_no))[:2]
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "CACHE_DIR", directory), \
                patch.object(tft, "pool_items", return_value=pool):
            paths = tft.cell_paths(self.snap)
            variant = paths[("leona", key + "-magic")]
            old = os.path.join(directory, f"leona-{key}-{'0' * 16}.json")
            for path in (variant, old):
                with open(path, "w") as f:
                    json.dump({"sentinel": True}, f)
            cell = tft.compute_cell(self.snap, unit, key, paths)
            self.assertEqual(cell["scenario"]["dummy"]["threat"]["key"], "mixed")
            self.assertTrue(os.path.exists(paths[("leona", key)]))
            self.assertFalse(os.path.exists(old))
            with open(variant) as f:
                self.assertEqual(json.load(f), {"sentinel": True})

    def test_exact_asset_mapping_wins_and_ambiguous_names_do_not_guess(self):
        first = {"apiName": "wrong", "name": "Same", "squareIcon": "assets/wrong.tex"}
        second = {"apiName": "DA_Right", "name": "Same", "squareIcon": "None", "tileIcon": "assets/right.tex"}
        snap = SimpleNamespace(units={"TFT18_Right": {"name": "Same", "assets": ["DA_Right"]}},
                               communitydragon={"champions": [first, second]})
        self.assertTrue(tft.ui_icons(snap)[0]["TFT18_Right"].endswith("/assets/right.png"))
        snap.units["TFT18_Right"]["assets"] = []
        self.assertIsNone(tft.ui_icons(snap)[0]["TFT18_Right"])
        snap.communitydragon["champions"] = [second]
        self.assertTrue(tft.ui_icons(snap)[0]["TFT18_Right"].endswith("/assets/right.png"))

    def test_missing_archive_and_invalid_asset_paths_have_graceful_fallbacks(self):
        snap = SimpleNamespace(units={"unit": {"name": "Champion", "assets": []}}, communitydragon={})
        self.assertEqual(tft.ui_icons(snap), ({"unit": None}, {}))
        for asset in (None, "None", "T_18_Akali_Square", "https://example.com/image.png", "assets/../image.tex"):
            self.assertIsNone(tft.cdragon_image_url(asset))
        self.assertEqual(tft.cdragon_image_url("ASSETS/ICONS/Test.TEX"),
                         "https://raw.communitydragon.org/latest/game/assets/icons/test.png")

    def test_descriptions_are_plain_text_and_use_corrected_curves(self):
        for trait in self.meta["traits"]:
            if trait["description"]:
                self.assertNotRegex(trait["description"], r"[<>{}]|TFTCurveTable|TFTAttribute|@|%i:")
                self.assertFalse(trait["description"].endswith(":"))
        hunter = copy.deepcopy(self.snap.traits["DA_18_Hunter"])
        hunter["curve"]["DamageAmp"] = [[0, 0], [1, .075], [4, .075]]
        self.assertIn("7.5% Damage Amp", tft.trait_description(hunter))
        attuned = next(t for t in self.meta["traits"] if t["name"] == "Attuned")
        self.assertIn("7%", attuned["description"])

    def test_tooltip_series_and_unsupported_templates(self):
        trait = {"levels": [2, 4], "curve": {"Power": [[1, .2], [2, .35]]},
                 "desc": 'Gain <TFTCurveTable row="Power" format="percent"/> power.'}
        self.assertEqual(tft.trait_description(trait), "Gain 20/35% power.")
        for desc in ('Gain <TFTCurveTable row="Missing"/> power.',
                     'Gain <TFTCurveTable row="Power" format="unknown"/> power.',
                     'Current: <TFTAttribute attributeID="Stack"/>', 'Gain {Unknown.Localization}.'):
            self.assertIsNone(tft.trait_description(dict(trait, desc=desc)))

    def test_selected_bonuses_include_corrected_values_and_existing_limits(self):
        gromp = next(u for u in self.meta["units"] if u["name"] == "Gromp")
        rift = next(b for b in gromp["traitBonuses"]["high"] if b["api"] == "DA_Riftbeast18")
        self.assertIn("+5% Attack Damage", rift["notes"][0])
        self.assertIn("+5% Attack Speed", rift["notes"][0])
        self.assertTrue(any("Alpha Mark is assumed" in note for note in rift["notes"]))
        akali = next(u for u in self.meta["units"] if u["name"] == "Akali")
        low = next(b for b in akali["traitBonuses"]["low"] if b["api"] == "DA_18_Adaptor")
        high = next(b for b in akali["traitBonuses"]["high"] if b["api"] == "DA_18_Adaptor")
        self.assertIn("+25% Attack Damage", low["notes"][0])
        self.assertIn("+50% Attack Damage", high["notes"][0])

    def test_unmodeled_traits_are_explicit_and_have_no_active_bonuses(self):
        traits = {t["api"]: t for t in self.meta["traits"]}
        self.assertFalse(traits["DA_18_Rival"]["modeled"])
        self.assertIn("not modeled", traits["DA_18_Rival"]["note"])
        for unit in self.meta["units"]:
            for bonuses in unit["traitBonuses"].values():
                self.assertNotIn("DA_18_Rival", [bonus["api"] for bonus in bonuses])


if __name__ == "__main__":
    unittest.main()
