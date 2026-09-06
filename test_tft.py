"""Checks for the TFT build math.

Run: python3 -m unittest test_tft -v

The rule tests pin the set's mechanics to hand-computed values, read off
the compiled engine through its traces and end-of-fight probes; the golden
tests replay data/tft/golden bit for bit; the snapshot tests read the
committed data/tft archive, so they also catch a MetaTFT schema change or
a stale override. Needs the built engine (jobs/build-engine.sh tft).
"""

import itertools
import json
import os
import unittest

import tft

SNAP = tft.load_snapshot()
ENGINE = tft.engine()
ITEM_FX = tft.load_item_effects(SNAP.set_no)
TRAIT_FX = tft.load_trait_effects(SNAP.set_no)
DUMMY = tft.dummies_for(SNAP)


def immortal(spec):
    """The dummy spec with unkillable health, for timing tests."""
    return dict(spec, slots=[dict(s, hp=10 ** 6) for s in spec["slots"]])


def one_hitter(pre=100.0, period=1.0, attackers=1):
    """Dummies that cannot die and hit for exactly `pre` once a period
    (no crit), the first `attackers` of them; the rest never swing."""
    slots = []
    for i, s in enumerate(DUMMY["slots"]):
        s = dict(s, hp=10 ** 6, ad=pre if i < attackers else 0.0, ability=0.0,
                 manaMax=0.0, manaStart=0.0, manaPerAttack=0.0, manaFromDamage=False)
        s["as"] = 1.0 / period
        slots.append(s)
    # Isolated mitigation fixtures have no enemy debuffs; tank benchmark
    # defaults and debuff interactions are covered in test_tft_tanks.
    return dict(DUMMY, slots=slots, critEv=1.0, board=None, enemyDebuffs={})


def item(name):
    return SNAP.item(name)["api"]


def spec_for(unit_name, star=2, items=(), fx=(), geometry="clump", traits=(), duration=None,
             dummy=None, pressure=None, driver=None):
    """A cell spec for one fight of `unit_name`; `fx` adds synthetic items
    carrying raw effects (the keys tft.item_spec resolves)."""
    u = SNAP.unit(unit_name)
    spec = tft.cell_spec(SNAP, u, star, geometry, list(traits), dummy or DUMMY, duration, pressure,
                         ITEM_FX, TRAIT_FX, items=[item(x) for x in items], driver=driver)
    for extra in fx:
        spec["items"].append({"api": "test", "name": "test", "unique": False, "stats": [],
                              "adds": [], **extra})
    return spec


def run(unit_name, **kw):
    """One traced fight: (sheet, result)."""
    return ENGINE.simulate(spec_for(unit_name, **kw), True)


def fx_of(unit_name, **kw):
    return ENGINE.compose_fx(spec_for(unit_name, **kw))


def events(res, kind=None, src=None, target=None):
    return [e for e in res["trace"]
            if (kind is None or e[1] == kind) and (src is None or e[4] == src)
            and (target is None or e[3] == target)]


def sim(unit_name, items=(), star=2, geometry="clump", traits=(), duration=None, dummy=None,
        pressure=None, driver=None):
    """One fight through the public path, with the unit's own driver."""
    u = SNAP.unit(unit_name)
    return tft.simulate(SNAP, u, star, [item(x) for x in items], geometry, list(traits),
                        dummy or DUMMY, duration, ITEM_FX, TRAIT_FX, driver, pressure)


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
        # 25% base + 90 = 115%: capped, the excess is crit damage, and a
        # second Precision adds 10%
        s, _ = run("Ashe", fx=[{"stats": [["crit", 0.9]], "precision": 2}])
        self.assertAlmostEqual(s["crit"], 1.0)
        self.assertAlmostEqual(s["critMult"], 1.4 + 0.15 + 0.1)
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
    crit for 1.4 (an expected 1.1), against the first target's 70 armor."""

    def first_auto(self, **kw):
        kw.setdefault("dummy", immortal(DUMMY))
        _, res = run("Ashe", driver="Driver", **kw)
        return events(res, "damage", "auto")[0][2]

    def test_attacks_always_crit_by_expected_value(self):
        self.assertAlmostEqual(self.first_auto(), 112.5 * (1 + 0.25 * 0.4) * 100 / 170)

    def test_physical_through_armor_and_health(self):
        _, res = run("Ashe", driver="Driver", duration=0.1)
        hit = events(res, "damage", "auto")[0][2]
        self.assertAlmostEqual(hit, 112.5 * 1.1 * 100 / 170)
        self.assertAlmostEqual(res["left"][0], 3000 - hit)
        self.assertAlmostEqual(res["total"], hit)

    def test_magic_damage_through_first_target_resist(self):
        # Ahri's 2★ orb deals 640 magic damage before the first target's
        # 70 MR. Its primary hit has no secondary-target falloff.
        _, res = run("Ahri", fx=[{"startingMana": 100}], dummy=immortal(DUMMY), duration=2.0)
        hit = events(res, "damage", "ability", target=0)[0][2]
        self.assertAlmostEqual(hit, 640 * 100 / 170)

    def test_true_damage_ignores_everything(self):
        # a burn ticks true damage: no resists, no amp
        _, res = run("Ashe", driver="Driver", fx=[{"burnOnHit": [0.01, 5.0]}, {"adds": [["amp", 0.5]]}],
                     dummy=immortal(DUMMY), duration=1.0)
        burn = events(res, "damage", "burn")
        self.assertTrue(burn)
        self.assertAlmostEqual(burn[0][2], 0.01 * 10 ** 6 * tft.TICK_S)

    def test_amp_is_post_mitigation_and_additive(self):
        fx = [{"adds": [["amp", 0.1]], "ampVsTank": 0.15}]
        self.assertAlmostEqual(self.first_auto(fx=fx), 112.5 * 1.1 * 100 / 170 * 1.25)
        # the tank-only amp does not apply to the non-tank dummy
        other = DUMMY["slots"][2]
        first = dict(DUMMY, slots=[other] + DUMMY["slots"][:2])
        self.assertAlmostEqual(self.first_auto(fx=fx, dummy=immortal(first)),
                               112.5 * 1.1 * 100 / (100 + other["armor"]) * 1.1)

    def test_ability_crits_only_with_precision(self):
        u = SNAP.unit("Ashe")
        _, plain = run("Ashe", dummy=immortal(DUMMY))
        arrow = tft.calc_value(u, "PhysicalDamageCalc1", 2, 112.5, 100, 1620, 45, 45)
        self.assertAlmostEqual(events(plain, "damage", "ability")[0][2], arrow * 100 / 170)
        _, prec = run("Ashe", fx=[{"precision": 1}], dummy=immortal(DUMMY))
        self.assertAlmostEqual(events(prec, "damage", "ability")[0][2],
                               arrow * 100 / 170 * (1 + 0.25 * 0.4))

    def test_sunder_on_hit(self):
        _, res = run("Ashe", driver="Driver", fx=[{"sunderOnHit": [0.3, 3.0]}], dummy=immortal(DUMMY))
        autos = events(res, "damage", "auto")
        self.assertAlmostEqual(autos[0][2], 112.5 * 1.1 * 100 / 170)               # sundered after the hit
        self.assertAlmostEqual(autos[1][2], 112.5 * 1.1 * 100 / (100 + 70 * 0.7))  # 1.25 s later, inside 3 s

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
        self.assertEqual(res["left"], [0.0, 0.0, 0.0])

    def test_aoe_geometry(self):
        for geo, hits in (("clump", 3), ("spread", 1)):
            _, res = run("Ahri", geometry=geo, dummy=immortal(DUMMY))
            land = events(res, "land")[0][0]
            self.assertEqual(len([e for e in events(res, "damage", "ability") if e[0] == land]), hits, geo)


class TestManaCycle(unittest.TestCase):
    """Ashe with nothing: 7 mana per attack at 0.8 attacks/s, 2 regen/s,
    20 of 80 to start. Attacks at 0, 1.25, …; regen lands on the quarter
    second ticks. After the attack at 6.25 s she has 20 + 42 + 12.5 =
    74.5; the ticks through 7.5 s add 2.5 and the attack there 7, so the
    first cast is at 7.5 s with 84 on the bar. It leaves 4 mana and a
    one-second lock that blocks exactly a second of regen (the tick at
    8.5 s pays nothing: its whole quarter second is inside the lock); the
    second bar fills on the tick at 18.5 s."""

    def test_cast_times(self):
        _, res = sim("Ashe", dummy=immortal(DUMMY))
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
                if u["objective"] not in tft.PRESSURED:
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
        _, res = sim("Caitlyn", dummy=immortal(DUMMY))
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
        _, res = sim("Hecarim", geometry="clump")
        self.assertGreater(res["ccTime"], 0.0)
        self.assertGreater(res["denied"], 0.0)
        _, res = run("Leona", driver="Driver", fx=[{"untargetableAtHp": [0.6, 1.0, 0.2]}])
        self.assertGreater(res["denied"], 0.0)
        self.assertGreater(res["probe"]["untargetableUntil"], 0.0)

    def test_legacy_board_streams_remain_available_for_custom_fights(self):
        self.assertEqual(sum(DUMMY["board"]), tft.BOARD_SIZE)
        self.assertGreater(DUMMY["boardPressureDps"], 2 * DUMMY["pressureDps"])
        # a tank's fight is the board's; a fighter faces the three in front
        _, leona = sim("Leona", duration=10.0)
        _, akali = sim("Akali", duration=10.0)
        rate = lambda r: r["hitsTaken"] / r["aliveTime"]
        self.assertGreater(rate(leona), rate(akali) * 2)
        self.assertEqual(len(leona["dummyAttacks"]), 3)

    def test_dummies_cast_on_their_mana(self):
        # the non-tank dummy: 7 mana per attack, 42.5 to fill, first swing one period in
        _, res = run("Leona", fx=[{"stats": [["hp", 10 ** 9]]}])
        self.assertGreaterEqual(res["dummyCasts"][2], 2)
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
    def test_dummies_from_the_set(self):
        self.assertEqual(DUMMY["count"], 3)
        self.assertEqual([s["kind"] for s in DUMMY["slots"]], ["tank", "tank", "non-tank"])
        self.assertGreater(DUMMY["tanks"], 10)
        self.assertGreater(DUMMY["tank"]["hp"], DUMMY["other"]["hp"])
        self.assertGreater(DUMMY["tank"]["armor"], DUMMY["other"]["armor"])

    def test_only_first_dummy_has_fixed_defenses(self):
        before = {api: dict(unit["stats"]) for api, unit in SNAP.units.items()}
        dummy = tft.dummies_for(SNAP)
        defenses = lambda slot: (slot["hp"], slot["armor"], slot["mr"])
        self.assertEqual([defenses(slot) for slot in dummy["slots"]],
                         [(3000, 70, 70), (1800, 45, 45), (1440, 40, 40)])
        self.assertEqual(dummy["totalHp"], 6240)
        self.assertEqual(defenses(dummy["tank"]), (1800, 45, 45))
        self.assertEqual(dummy["slots"][1], dummy["tank"])
        self.assertEqual(dummy["slots"][2], dummy["other"])
        for key in ("kind", "ad", "as", "ability", "physicalShare", "manaMax",
                    "manaStart", "manaPerAttack", "manaFromDamage"):
            self.assertEqual(dummy["slots"][0][key], dummy["tank"][key], key)
        self.assertEqual({api: unit["stats"] for api, unit in SNAP.units.items()}, before)

    def test_enemy_defenses_are_consistent_across_all_scenarios(self):
        legacy = [(3000, 70, 70), (1800, 45, 45), (1440, 40, 40)]
        frontline = [(3000, 70, 70), (1800, 45, 45), (1800, 45, 45),
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
                                     frontline if unit["objective"] == "tank" else legacy)
                    count += 1
        self.assertEqual(count, 1770)

    def test_overrides_are_current_with_the_patch_notes(self):
        findings, unmatched = tft.check_patch_notes(SNAP)
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
        self.assertFalse(tft.source_stale(), "tft.py or tft_engine/ changed since the build")
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
        # attack adds 7 and casts at 37 — not 67 chain-casting through the lock
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
        # Striker's Flail: each copy fills its own cap (crit 100%: a stack per attack)
        _, res = run("Ashe", driver="Driver", dummy=immortal(DUMMY),
                     fx=[{"ampPerCrit": [0.05, 5.0, 4]}] * 2 + [{"stats": [["crit", 0.75]]}])
        autos = events(res, "damage", "auto")
        self.assertAlmostEqual(autos[4][2] / autos[0][2], 1.4 / 1.1)

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


if __name__ == "__main__":
    unittest.main()
