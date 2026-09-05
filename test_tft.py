"""Checks for the TFT build math.

Run: python3 -m unittest test_tft -v

The rule tests pin the set's mechanics to hand-computed values; the
snapshot tests read the committed data/tft archive, so they also catch a
MetaTFT schema change or a stale override.
"""

import itertools
import os
import unittest

import tft
import tft_kits

SNAP = tft.load_snapshot()


def immortal(spec):
    """The dummy spec with unkillable health, for timing tests."""
    return dict(spec, slots=[dict(s, hp=10 ** 6) for s in spec["slots"]])


def fx_with(**kw):
    fx = tft.Fx()
    for k, v in kw.items():
        setattr(fx, k, v)
    return fx


def fight_for(unit_name, star=2, fx=None, geometry="clump", driver=None):
    unit = SNAP.unit(unit_name)
    sheet = tft.Sheet(unit, star, fx or tft.Fx())
    dummies = tft.make_dummies(tft.dummies_for(SNAP))
    return tft.Fight(sheet, dummies, geometry, driver or tft.Driver())


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
    def test_ashe_arrow_is_percent_of_ad(self):
        # ArrowDamage 440 at 1★ means 440% AD: 4.4 × 75
        u = SNAP.unit("Ashe")
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc1", 1, 75, 100, 900, 45, 45, {}), 330.0)

    def test_akali_mixes_ad_percent_and_flat_ap(self):
        # 145% of 40 AD plus 10 per 100 AP
        u = SNAP.unit("Akali")
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc1", 1, 40, 100, 650, 35, 35, {}), 68.0)
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc1", 1, 40, 200, 650, 35, 35, {}), 78.0)

    def test_basic_attack_scaling_is_a_fraction(self):
        # Draven's spinning axe: 1.5 × AD
        u = SNAP.unit("Draven")
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc2", 1, 48, 100, 900, 45, 45, {}), 72.0)

    def test_chained_calc(self):
        # Sivir's bounce is 20% of the first hit (190% AD + 15 AP = 110 at base)
        u = SNAP.unit("Sivir")
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc1", 1, 50, 100, 850, 40, 40, {}), 110.0)
        self.assertAlmostEqual(tft.calc_value(u, "PhysicalDamageCalc2", 1, 50, 100, 850, 40, 40, {}), 22.0)

    def test_star_scaling_of_calcs(self):
        u = SNAP.unit("Ashe")
        two = tft.calc_value(u, "PhysicalDamageCalc1", 2, 112.5, 100, 1620, 45, 45, {})
        self.assertAlmostEqual(two, 6.6 * 112.5)

    def test_calc_type_from_name(self):
        self.assertEqual(tft.calc_type("TFTCalculationAttributes.PhysicalDamageCalc1"), "physical")
        self.assertEqual(tft.calc_type("MagicDamageCalc2"), "magic")
        self.assertEqual(tft.calc_type("TrueDamageCalc1"), "true")


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
        self.assertNotIn("omnivamp", s)

    def test_amp_items(self):
        self.assertAlmostEqual(self.stats("Rabadon's Deathcap")["amp"], 0.15)
        self.assertAlmostEqual(self.stats("Deathblade")["adPct"], 0.55)
        self.assertAlmostEqual(self.stats("Red Buff")["asPct"], 0.45)

    def test_every_pool_item_has_a_stat(self):
        pool = tft.pool_items(SNAP, tft.load_item_effects(SNAP.set_no))
        self.assertEqual(len(pool), 35)
        for api in pool:
            self.assertTrue(tft.parse_stat_line(SNAP.items[api]), api)


class TestSheet(unittest.TestCase):
    def test_star_multipliers(self):
        s2 = tft.Sheet(SNAP.unit("Ashe"), 2, tft.Fx())
        self.assertAlmostEqual(s2.base_ad, 112.5)
        self.assertAlmostEqual(s2.max_hp, 1620)
        s3 = tft.Sheet(SNAP.unit("Ashe"), 3, tft.Fx())
        self.assertAlmostEqual(s3.base_ad, 168.75)
        self.assertAlmostEqual(s3.max_hp, 900 * 1.8 * 1.8)

    def test_crit_excess_and_double_precision(self):
        # 25% base + 35 + 35 + 20 = 115%: capped, the excess is crit damage,
        # and a second Precision adds 10%
        fx = fx_with(crit=0.9, precision=2)
        s = tft.Sheet(SNAP.unit("Ashe"), 2, fx)
        self.assertAlmostEqual(s.crit_chance, 1.0)
        self.assertAlmostEqual(s.crit_mult, 1.4 + 0.15 + 0.1)
        self.assertTrue(s.precision)

    def test_attack_speed_additive_on_base_and_capped(self):
        s = tft.Sheet(SNAP.unit("Ashe"), 2, fx_with(asPct=0.5))
        self.assertAlmostEqual(s.attack_speed(), 0.8 * 1.5)
        self.assertAlmostEqual(s.attack_speed(20.0), tft.AS_CAP)

    def test_role_mana(self):
        self.assertEqual(tft.Sheet(SNAP.unit("Ashe"), 2, tft.Fx()).mana_per_attack, 7)
        self.assertEqual(tft.Sheet(SNAP.unit("Draven"), 2, tft.Fx()).mana_per_attack, 10)
        self.assertEqual(tft.Sheet(SNAP.unit("Kha'Zix"), 2, tft.Fx()).mana_per_attack, 10)

    def test_items_build_fx(self):
        item_fx = tft.load_item_effects(SNAP.set_no)
        u = SNAP.unit("Ashe")
        fx = tft.build_fx(SNAP, u, [SNAP.item("Infinity Edge")["api"], SNAP.item("Spear of Shojin")["api"]],
                          [], item_fx, {})
        self.assertEqual(fx.precision, 1)
        self.assertAlmostEqual(fx.manaPerAttack, 5)
        self.assertAlmostEqual(fx.adPct, 0.5)
        self.assertAlmostEqual(fx.manaRegen, 2 + 1)   # caster role + the spear


class TestDamage(unittest.TestCase):
    def test_resist_formula(self):
        self.assertAlmostEqual(tft.resist_mult(45), 100 / 145)
        self.assertAlmostEqual(tft.resist_mult(0), 1.0)

    def test_physical_through_armor(self):
        f = fight_for("Ashe")
        d = f.targets[0]
        got = f.deal(100, "physical", d, "x", ability=True, crit=False)
        self.assertAlmostEqual(got, 100 * 100 / 145)
        self.assertAlmostEqual(d.hp, d.max_hp - got)

    def test_true_damage_ignores_everything(self):
        f = fight_for("Ashe", fx=fx_with(amp=0.5))
        self.assertAlmostEqual(f.deal(100, "true", f.targets[0], "x"), 100)

    def test_amp_is_post_mitigation_and_additive(self):
        f = fight_for("Ashe", fx=fx_with(amp=0.1, ampVsTank=0.15))
        got = f.deal(100, "magic", f.targets[0], "x", crit=False)
        self.assertAlmostEqual(got, 100 * 100 / 145 * 1.25)
        # the tank-only amp does not apply to the non-tank dummy
        other = f.targets[2]
        got = f.deal(100, "magic", other, "x", crit=False)
        self.assertAlmostEqual(got, 100 * 100 / (100 + other.mr) * 1.1)

    def test_ability_crits_only_with_precision(self):
        plain = fight_for("Ashe")
        prec = fight_for("Ashe", fx=fx_with(precision=1))
        a = plain.deal(100, "magic", plain.targets[0], "x")
        b = prec.deal(100, "magic", prec.targets[0], "x")
        self.assertAlmostEqual(a, 100 * 100 / 145)
        self.assertAlmostEqual(b, a * (1 + 0.25 * 0.4))

    def test_attacks_always_crit_by_expected_value(self):
        f = fight_for("Ashe")
        got = f.hit_attack(f.targets[0])
        self.assertAlmostEqual(got, 112.5 * (1 + 0.25 * 0.4) * 100 / 145)

    def test_sunder_on_hit(self):
        f = fight_for("Ashe", fx=fx_with(sunderOnHit=[(0.3, 3.0)]))
        d = f.targets[0]
        f.hit_attack(d)
        self.assertAlmostEqual(d.sunder, 0.3)
        self.assertAlmostEqual(d.sunder_until, 3.0)
        got = f.deal(100, "physical", d, "x", crit=False)
        self.assertAlmostEqual(got, 100 * 100 / (100 + 45 * 0.7))

    def test_burn_ticks_true_damage(self):
        f = fight_for("Ashe", fx=fx_with(burnOnHit=[(0.01, 5.0, False)]))
        d = f.targets[0]
        f.on_hit_effects(d, False)
        hp = d.hp
        f.t = 0.25
        f._tick(0.25, 1.0, [], [])
        self.assertAlmostEqual(hp - d.hp, 0.01 * d.max_hp * 0.25)
        self.assertAlmostEqual(f.breakdown["burn"], 0.01 * d.max_hp * 0.25)

    def test_overkill_moves_to_the_next_dummy(self):
        f = fight_for("Ashe")
        d0 = f.targets[0]
        f.deal(1e6, "true", d0, "x")
        self.assertFalse(d0.alive)
        self.assertIs(f.target(), f.targets[1])
        self.assertAlmostEqual(f.total, d0.max_hp)
        self.assertIsNone(f.kill_time)

    def test_aoe_geometry(self):
        clump = fight_for("Ashe", geometry="clump")
        spread = fight_for("Ashe", geometry="spread")
        self.assertEqual(len(clump.aoe()), 3)
        self.assertEqual(len(clump.aoe(2)), 2)
        self.assertEqual(len(clump.aoe(exclude_primary=True)), 2)
        self.assertEqual(len(spread.aoe()), 1)
        self.assertEqual(len(spread.aoe(exclude_primary=True)), 0)


class TestManaCycle(unittest.TestCase):
    """Ashe with nothing: 7 mana per attack at 0.8 attacks/s, 2 regen/s,
    20 of 80 to start. Attacks at 0, 1.25, …; regen lands on the quarter
    second ticks. After the attack at 6.25 s she has 20 + 42 + 12.5 =
    74.5; the tick at 7.5 s adds 0.5 and the attack there 7, so the first
    cast is at 7.5 s. It leaves 4 mana and a one-second lock; the second
    bar fills on the tick at 18.25 s."""

    def test_cast_times(self):
        unit = SNAP.unit("Ashe")
        dummy = immortal(tft.dummies_for(SNAP))   # nothing dies
        sheet, res = tft.simulate(SNAP, unit, 2, [], "clump", [], dummy, 20.0,
                                  {}, {}, tft_kits.driver_for(unit))
        self.assertEqual(res["casts"], 2)
        self.assertAlmostEqual(res["castTimes"][0], 7.5)
        self.assertAlmostEqual(res["castTimes"][1], 18.25)
        self.assertEqual(res["attacks"], 16)

    def test_mana_lock_after_cast(self):
        f = fight_for("Ashe")
        f.mana = f.sheet.mana_max
        f._cast()
        self.assertEqual(f.casts, 1)
        self.assertAlmostEqual(f.lock_until, 1.0)
        f.gain_mana(50)
        self.assertAlmostEqual(f.mana, 0.0)

    def test_overflow_carries(self):
        f = fight_for("Ashe")
        f.mana = f.sheet.mana_max + 5
        f._cast()
        self.assertAlmostEqual(f.mana, 5.0)

    def test_manaless_units_never_cast(self):
        unit = SNAP.unit("Kayle")
        sheet, res = tft.simulate(SNAP, unit, 2, [], "clump", [], tft.dummies_for(SNAP), 20.0,
                                  {}, {}, tft_kits.driver_for(unit))
        self.assertEqual(res["casts"], 0)
        self.assertIn("ascension", res["breakdown"])


class TestTraits(unittest.TestCase):
    def setUp(self):
        self.trait_fx = tft.load_trait_effects(SNAP.set_no)

    def test_contexts_skip_prismatic_tiers(self):
        u = SNAP.unit("Ashe")
        ctx, unmodeled = tft.unit_trait_contexts(SNAP, u, self.trait_fx)
        by_name = {SNAP.traits[a]["name"]: SNAP.traits[a]["levels"][c - 1] for a, c in ctx["high"]}
        self.assertEqual(by_name, {"Blossom": 9, "Hunter": 5})
        self.assertEqual({SNAP.traits[a]["name"]: SNAP.traits[a]["levels"][c - 1] for a, c in ctx["low"]},
                         {"Blossom": 3, "Hunter": 2})
        self.assertEqual(ctx["bare"], [])
        self.assertEqual(unmodeled, [])

    def test_hunter_ad_and_delayed_amp(self):
        u = SNAP.unit("Ashe")
        ctx, _ = tft.unit_trait_contexts(SNAP, u, self.trait_fx)
        hunter = [(a, c) for a, c in ctx["low"] if SNAP.traits[a]["name"] == "Hunter"]
        fx = tft.build_fx(SNAP, u, [], hunter, {}, self.trait_fx)
        self.assertAlmostEqual(fx.adPct, 0.2)
        self.assertEqual(fx.ampAfterSameTarget, (0.1, 4.0))
        f = tft.Fight(tft.Sheet(u, 2, fx), tft.make_dummies(tft.dummies_for(SNAP)), "clump", tft.Driver())
        self.assertAlmostEqual(f.amp(f.targets[0]), 1.0)
        f.t = 4.0
        self.assertAlmostEqual(f.amp(f.targets[0]), 1.1)

    def test_lunar_doubles_for_its_own(self):
        u = SNAP.unit("Aphelios")
        api = next(a for a in u["traitApis"] if SNAP.traits[a]["name"] == "Lunar")
        fx = tft.build_fx(SNAP, u, [], [(api, 1)], {}, self.trait_fx)
        self.assertAlmostEqual(fx.asPct, 0.14)
        self.assertAlmostEqual(fx.ap, 14)

    def test_executioner_gives_precision(self):
        u = SNAP.unit("Ezreal")
        api = next(a for a in u["traitApis"] if SNAP.traits[a]["name"] == "Executioner")
        fx = tft.build_fx(SNAP, u, [], [(api, 2)], {}, self.trait_fx)
        self.assertEqual(fx.precision, 1)
        self.assertAlmostEqual(fx.crit, 0.15)
        self.assertAlmostEqual(fx.bleedPct, 0.3)

    def test_every_modeled_trait_resolves(self):
        for api, spec in self.trait_fx.items():
            if api.startswith("_"):
                continue
            t = SNAP.traits[api]
            u = next(SNAP.units[x] for x in SNAP.units if api in SNAP.units[x]["traitApis"])
            for col in range(1, len(t["levels"]) + 1):
                tft.build_fx(SNAP, u, [], [(api, col)], {}, self.trait_fx)


class TestEffects(unittest.TestCase):
    def test_every_modeled_item_effect_resolves(self):
        item_fx = tft.load_item_effects(SNAP.set_no)
        u = SNAP.unit("Ashe")
        for api in tft.pool_items(SNAP, item_fx):
            tft.build_fx(SNAP, u, [api], [], item_fx, {})

    def test_guinsoo_stacks_per_second(self):
        f = fight_for("Ashe", fx=fx_with(asPerSecond=[(0.07, None)]))
        f.run()
        self.assertGreater(f.as_stack, 0.07 * 15)

    def test_stacking_items_change_the_ranking_inputs(self):
        u = SNAP.unit("Ashe")
        dummy = tft.dummies_for(SNAP)
        item_fx = tft.load_item_effects(SNAP.set_no)
        _, plain = tft.simulate(SNAP, u, 2, [], "clump", [], dummy, 20.0, item_fx, {},
                                tft_kits.driver_for(u))
        _, kraken = tft.simulate(SNAP, u, 2, [SNAP.item("Kraken's Fury")["api"]], "clump", [],
                                 dummy, 20.0, item_fx, {}, tft_kits.driver_for(u))
        self.assertGreater(kraken["total"], plain["total"])


class TestDrivers(unittest.TestCase):
    def test_every_driver_runs_everywhere(self):
        trait_fx = tft.load_trait_effects(SNAP.set_no)
        item_fx = tft.load_item_effects(SNAP.set_no)
        dummy = tft.dummies_for(SNAP)
        trio = [SNAP.item(x)["api"] for x in ("Infinity Edge", "Jeweled Gauntlet", "Spear of Shojin")]
        for u in tft.modeled_units(SNAP):
            ctx, _ = tft.unit_trait_contexts(SNAP, u, trait_fx)
            for items, geo, c, star in itertools.product(([], trio), ("spread", "clump"),
                                                          ("bare", "high"), (2, 3)):
                sheet, res = tft.simulate(SNAP, u, star, items, geo, ctx[c], dummy, 20.0,
                                          item_fx, trait_fx, tft_kits.driver_for(u))
                self.assertGreater(res["total"], 0, u["name"])
                self.assertAlmostEqual(sum(res["breakdown"].values()), res["total"], places=6)
                if res["killTime"] is not None:
                    self.assertAlmostEqual(res["total"], dummy["totalHp"], places=3)

    def test_kayle_waves_need_company(self):
        u = SNAP.unit("Kayle")
        d = tft.dummies_for(SNAP)
        _, clump = tft.simulate(SNAP, u, 3, [], "clump", [], d, 20.0, {}, {}, tft_kits.driver_for(u))
        _, spread = tft.simulate(SNAP, u, 3, [], "spread", [], d, 20.0, {}, {}, tft_kits.driver_for(u))
        self.assertIn("waves", clump["breakdown"])
        self.assertNotIn("waves", spread["breakdown"])

    def test_caitlyn_headshots_every_third_attack(self):
        u = SNAP.unit("Caitlyn")
        dummy = immortal(tft.dummies_for(SNAP))
        _, res = tft.simulate(SNAP, u, 2, [], "clump", [], dummy, 20.0, {}, {}, tft_kits.driver_for(u))
        self.assertEqual(res["casts"], 0)
        # 0.7 attacks/s over 20 s; the last swing lands on the 20 s mark, so
        # floating point decides whether it is inside the window
        self.assertIn(res["attacks"], (14, 15))
        self.assertIn("headshot", res["breakdown"])
        # a headshot every third attack, so a third of the swings and the
        # bigger share of the damage
        self.assertGreater(res["breakdown"]["headshot"], res["breakdown"]["auto"])

    def test_khazix_isolation(self):
        u = SNAP.unit("Kha'Zix")
        d = tft.dummies_for(SNAP)
        _, spread = tft.simulate(SNAP, u, 2, [], "spread", [], d, 20.0, {}, {}, tft_kits.driver_for(u))
        self.assertIn("isolated", spread["breakdown"])


class TestEnumeration(unittest.TestCase):
    def test_combination_count(self):
        pool = tft.pool_items(SNAP, tft.load_item_effects(SNAP.set_no))
        n = sum(1 for _ in itertools.combinations_with_replacement(pool, 3))
        self.assertEqual(n, 7770)

    def test_ranking_order(self):
        killer = {"killTime": 5.0, "total": 100}
        faster = {"killTime": 4.0, "total": 50}
        survivor = {"killTime": None, "total": 9999}
        self.assertLess(tft.rank_key(faster), tft.rank_key(killer))
        self.assertLess(tft.rank_key(killer), tft.rank_key(survivor))

    def test_small_pool_enumerates_sorted(self):
        item_fx = tft.load_item_effects(SNAP.set_no)
        pool = tft.pool_items(SNAP, item_fx)[:5]
        u = SNAP.unit("Ashe")
        out, count = tft.enumerate_builds(SNAP, u, 2, "clump", [], tft.dummies_for(SNAP), pool,
                                          workers=1)
        self.assertEqual(count, 35)
        keys = [tft.rank_key(r) for _, _, r in out]
        self.assertEqual(keys, sorted(keys))


class TestSnapshot(unittest.TestCase):
    def test_dummies_from_the_set(self):
        d = tft.dummies_for(SNAP)
        self.assertEqual(d["count"], 3)
        self.assertEqual([s["kind"] for s in d["slots"]], ["tank", "tank", "non-tank"])
        self.assertGreater(d["tanks"], 10)
        self.assertGreater(d["tank"]["hp"], d["other"]["hp"])
        self.assertGreater(d["tank"]["armor"], d["other"]["armor"])

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
        self.assertEqual(len(meta["scenarios"]), 12)
        self.assertEqual(len(meta["items"]), 35)
        self.assertGreaterEqual(len(meta["units"]), 20)

    def test_cells_are_hashed_per_unit_and_scenario(self):
        paths = tft.cell_paths(SNAP)
        self.assertEqual(len(paths), len(tft.modeled_units(SNAP)) * 12)
        self.assertEqual(len(set(paths.values())), len(paths))
        self.assertTrue(all(p.startswith(tft.CACHE_DIR) for p in paths.values()))


if __name__ == "__main__":
    unittest.main()
