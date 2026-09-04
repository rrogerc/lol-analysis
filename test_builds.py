"""Hand-computed checks for the builds stat layer.

Run: python3 -m unittest test_builds -v

The math tests pin the League formulas to values computed by hand from the
wiki; the integration tests read the committed data/items snapshot, so they
also catch a meraki schema change sneaking past ITEM_STAT_MAP.
"""

import copy
import math
import os
import shutil
import tempfile
import unittest
from unittest import mock

import builds


def fake_champ(**dd_overrides):
    """A Kayle-shaped champion snapshot (patch 16.16 values)."""
    dd = {"name": "Kayle", "stats": {
        "hp": 670, "hpperlevel": 92, "mp": 330, "mpperlevel": 50,
        "armor": 26, "armorperlevel": 4.2,
        "spellblock": 22, "spellblockperlevel": 1.3,
        "attackdamage": 50, "attackdamageperlevel": 0,
        "attackspeed": 0.625, "attackspeedperlevel": 1.5,
        "movespeed": 335, "attackrange": 175,
    }}
    dd["stats"].update(dd_overrides)
    mk = {"stats": {"attackSpeedRatio": {"flat": 0.667},
                    "criticalStrikeDamage": {"flat": 175.0}}}
    return {"slug": "kayle", "dd": dd, "mk": mk, "meta": {"patch": "16.16"}}


def enum_one(champ, pool, effects, kit, level, ranks, hp, armor, mr, duration,
             bonus_hp=0.0, **kw):
    """enumerate_builds against a single target, in the one-target shape:
    ([(ids, sheet, fight result)] best-first, count)."""
    target = dict(targetHp=hp, armor=armor, mr=mr, duration=duration,
                  targetBonusHp=bonus_hp)
    lists, count = builds.enumerate_builds(
        champ, pool, effects, kit, level, ranks, {"t": target}, **kw)
    return [(ids, sheet, rs["t"]) for ids, sheet, rs in lists["t"]], count


class TestGrowth(unittest.TestCase):
    def test_endpoints(self):
        # growth(1)=0 and growth(18)=17 exactly: level 18 = base + 17 * g
        self.assertEqual(builds.growth(1), 0.0)
        self.assertAlmostEqual(builds.growth(18), 17.0)

    def test_hp_at_18(self):
        self.assertAlmostEqual(builds.stat_at(670, 92, 18), 670 + 92 * 17)

    def test_early_levels_grow_slower(self):
        # level 2 grants 0.72 of a linear level's growth
        self.assertAlmostEqual(builds.growth(2), 0.72)


class TestResists(unittest.TestCase):
    def test_multiplier(self):
        self.assertEqual(builds.resist_mult(0), 1.0)
        self.assertEqual(builds.resist_mult(100), 0.5)
        # negative resists amplify: -20 -> 2 - 100/120
        self.assertAlmostEqual(builds.resist_mult(-20), 2 - 100 / 120)

    def test_pen_order_pct_before_flat(self):
        # 100 armor, 30% pen then 10 flat: 100*0.7 - 10 = 60
        self.assertAlmostEqual(builds.penetrate(100, 30, 10), 60.0)

    def test_pen_never_negative(self):
        self.assertEqual(builds.penetrate(20, 0, 40), 0.0)

    def test_pct_pen_stacks_multiplicatively(self):
        self.assertAlmostEqual(builds.stack_pct_pen(40, 30), 58.0)


class TestStatSheet(unittest.TestCase):
    def test_naked_kayle_level_11_attack_speed(self):
        # bonus AS from growth: 1.5 * growth(11) = 1.5 * 8.775 = 13.1625%
        # AS = 0.625 + 0.667 * 0.131625 = 0.71279...
        s = builds.resolve_stats(fake_champ(), 11, [], {}, effects={})
        self.assertAlmostEqual(s["attack_speed"], 0.625 + 0.667 * 0.131625, places=4)
        self.assertAlmostEqual(s["ad"], 50.0)  # 16.16: no AD growth
        self.assertAlmostEqual(s["hp"], builds.stat_at(670, 92, 11))

    def test_as_cap(self):
        pool = {1: _item("Turbo", attackSpeed=("flat", 400))}
        s = builds.resolve_stats(fake_champ(), 18, [1], pool, effects={})
        self.assertEqual(s["attack_speed"], builds.AS_CAP)

    def test_haste_to_cooldown(self):
        pool = {1: _item("Clock", abilityHaste=("flat", 25))}
        s = builds.resolve_stats(fake_champ(), 18, [1], pool, effects={})
        self.assertAlmostEqual(s["cd_mult"], 0.8)


def _item(name, gold=1000, **stats):
    it = {"name": name, "shop": {"prices": {"total": gold}, "purchasable": True},
          "stats": {}, "passives": [], "nicknames": []}
    for stat, (field, val) in stats.items():
        it["stats"][stat] = {field: val}
    return it


class TestRealSnapshot(unittest.TestCase):
    """Against the committed data/items/<latest>/meraki.json."""

    @classmethod
    def setUpClass(cls):
        cls.patch, cls.pool = builds.load_items()
        cls.idx = builds.item_index(cls.pool)

    def resolve(self, *tokens, level=18):
        ids = [builds.resolve_item(self.pool, self.idx, t) for t in tokens]
        return builds.resolve_stats(fake_champ(), level, ids, self.pool)

    def test_item_lookup(self):
        self.assertEqual(builds.resolve_item(self.pool, self.idx, "nashors"),
                         3115)  # nickname
        self.assertEqual(
            builds.resolve_item(self.pool, self.idx, "Rabadon's Deathcap"),
            3089)  # name
        self.assertEqual(builds.resolve_item(self.pool, self.idx, "3135"),
                         3135)  # id

    def test_ddragon_overrides_stale_meraki(self):
        # 16.16 ddragon: Berserker's is 30% AS; meraki still says 25%.
        # The pool must carry the ddragon value.
        self.assertEqual(self.pool[3006]["stats"]["attackSpeed"]["flat"], 30.0)
        s = self.resolve("berserkers", level=1)
        self.assertAlmostEqual(s["bonus_as_pct"], 30.0)

    def test_modeled_pool_parses_from_ddragon(self):
        # Every item the optimizer enumerates must get ddragon-canonical
        # stats, not a silent meraki fallback.
        import json, os, items as items_mod
        with open(os.path.join(items_mod.ITEMS_DATA_DIR, self.patch,
                               "ddragon.json")) as f:
            dd = json.load(f)
        for iid in builds.DEFAULT_POOL + builds.BOOTS:
            parsed = builds.parse_dd_stats(dd[str(iid)]["description"])
            self.assertIsNotNone(parsed, f"item {iid} fell back to meraki")

    def test_rabadon_multiplier(self):
        # Nashor's 80 AP + Rabadon 130 AP, x1.30 from the overlay
        s = self.resolve("nashors", "rabadons deathcap")
        self.assertAlmostEqual(s["ap"], (80 + 130) * 1.30)
        self.assertEqual(s["gold"], 2900 + 3500)

    def test_magic_pen_split(self):
        # Void Staff 40% + Shadowflame 15 flat land in separate channels
        s = self.resolve("void staff", "shadowflame")
        self.assertAlmostEqual(s["magic_pen_pct"], 40.0)
        self.assertAlmostEqual(s["magic_pen_flat"], 15.0)

    def test_no_unmapped_stats_in_whole_pool(self):
        # Every stat.field in the snapshot is either mapped or ignored;
        # resolve the entire pool at once and rely on the stderr warning
        # path being exercised as a mapping check.
        known = set(builds.ITEM_STAT_MAP) | builds.IGNORED_ITEM_STATS
        unmapped = {(stat, field)
                    for it in self.pool.values()
                    for stat, fields in it["stats"].items()
                    for field, v in fields.items() if v} - known
        self.assertEqual(unmapped, set())


class TestKayleKit(unittest.TestCase):
    """Shape and spot checks on the hand-encoded kit."""

    @classmethod
    def setUpClass(cls):
        cls.kit = builds.load_kit("kayle")

    def test_shape(self):
        for slot, ranks in [("Q", 5), ("E", 5), ("R", 3)]:
            self.assertEqual(len(self.kit["abilities"][slot]["cooldownS"]), ranks)
        self.assertEqual(len(self.kit["abilities"]["Q"]["damage"]["base"]), 5)
        self.assertEqual(len(self.kit["abilities"]["R"]["damage"]["base"]), 3)

    def test_wave_by_level(self):
        wave = self.kit["passive"]["aflame"]["wave"]["baseByLevel"]
        self.assertAlmostEqual(builds.by_level(wave, 1), 20.0)
        self.assertAlmostEqual(builds.by_level(wave, 18), 41.0)
        # level 11 (first level Aflame exists): 20 + 21 * 10/17
        self.assertAlmostEqual(builds.by_level(wave, 11), 20 + 21 * 10 / 17)

    def test_ability_hit(self):
        # Q rank 5 with 100 bonus AD and 400 AP: 180 + 60 + 200 = 440
        sheet = {"ad": 150.0, "ad_bonus": 100.0, "ap": 400.0}
        q = self.kit["abilities"]["Q"]["damage"]
        self.assertAlmostEqual(builds.ability_hit(q, 5, sheet), 440.0)


class TestEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kit = builds.load_kit("kayle")
        cls.patch, cls.pool = builds.load_items()
        cls.idx = builds.item_index(cls.pool)
        cls.effects = builds.load_item_effects()

    def sim(self, level, tokens, hp=2800, armor=80, mr=60, duration=8.0,
            use_ult=True, **kw):
        ids = [builds.resolve_item(self.pool, self.idx, t) for t in tokens]
        sheet = builds.resolve_stats(fake_champ(), level, ids, self.pool,
                                     self.effects)
        return builds.simulate(sheet, self.kit, builds.merge_effects(ids, self.effects),
                               level, builds.skill_ranks(level), hp, armor, mr,
                               duration, use_ult=use_ult, **kw)

    def test_skill_ranks(self):
        self.assertEqual(builds.skill_ranks(16),
                         {"Q": 5, "W": 3, "E": 5, "R": 3})
        self.assertEqual(builds.skill_ranks(1), {"Q": 1, "W": 0, "E": 0, "R": 0})

    def test_eff_resist_order(self):
        # 100 armor: -0 flat red, 15% red -> 85, 40% pen -> 51, 10 flat -> 41
        self.assertAlmostEqual(builds.eff_resist(100, 0, 15, 40, 10), 41.0)
        # reductions can go negative, pen then does nothing
        self.assertAlmostEqual(builds.eff_resist(5, 20, 0, 50, 10), -15.0)

    def test_level1_hand_computed(self):
        # No items, no ult, 0 resists, 1s: Q at t=0 (60 dmg, lockout), one
        # auto at 0.25 (50 dmg); zeal makes the next auto land past 1s.
        r = self.sim(1, [], hp=10_000, armor=0, mr=0, duration=1.0,
                     use_ult=False)
        self.assertEqual(r["attacks"], 1)
        self.assertAlmostEqual(r["breakdown"]["auto"], 50.0)
        self.assertAlmostEqual(r["breakdown"]["Q"], 60.0)
        self.assertAlmostEqual(r["total"], 110.0)

    def test_breakdown_sums_to_total(self):
        r = self.sim(16, ["nashors", "rabadons", "void staff", "lich bane"])
        self.assertAlmostEqual(sum(r["breakdown"].values()), r["total"], places=6)

    def test_armor_only_reduces_physical(self):
        soft = self.sim(16, ["nashors"], hp=10_000, armor=0, duration=4)
        hard = self.sim(16, ["nashors"], hp=10_000, armor=200, duration=4)
        self.assertLess(hard["breakdown"]["auto"], soft["breakdown"]["auto"])
        self.assertAlmostEqual(hard["breakdown"]["E onhit"],
                               soft["breakdown"]["E onhit"], places=4)

    def test_waves_need_aflame_and_stacks(self):
        # level 10: no Aflame -> no wave damage even prestacked
        r10 = self.sim(10, [], hp=10_000, duration=6, prestacked=True)
        self.assertNotIn("wave", r10["breakdown"])
        # level 11 fresh: waves only after 5 autos stack Zeal
        r11 = self.sim(11, [], hp=10_000, duration=6)
        r11p = self.sim(11, [], hp=10_000, duration=6, prestacked=True)
        self.assertIn("wave", r11["breakdown"])
        self.assertGreater(r11p["breakdown"]["wave"], r11["breakdown"]["wave"])

    def test_guinsoo_phantom_cadence(self):
        # Attack 4 reaches max Seething and banks the first Phantom stack,
        # attack 5 the second, attack 6 consumes them without banking a new
        # one -> phantom hits on attacks 6, 9, 12, ... (Riot: "every third
        # Attack" while fully stacked).
        r = self.sim(16, ["rageblade"], hp=100_000, duration=10)
        self.assertGreater(r["phantom_hits"], 0)
        self.assertEqual(r["phantom_hits"], (r["attacks"] - 6) // 3 + 1)

    def test_liandry_burns(self):
        r = self.sim(16, ["liandry"], hp=10_000, duration=6)
        self.assertIn("burn", r["breakdown"])
        # each tick is 1% max hp pre-mitigation; with 60 mr and no pen the
        # burn can't exceed 1%/tick * ticks
        self.assertLess(r["breakdown"]["burn"], 0.01 * 10_000 * 13)

    def resolve(self, level, tokens):
        ids = [builds.resolve_item(self.pool, self.idx, t) for t in tokens]
        return ids, builds.resolve_stats(fake_champ(), level, ids, self.pool,
                                         self.effects)

    def test_cinderbloom_is_deterministic(self):
        # The audit's bug: Cinderbloom was scaled by crit chance, so a 0-crit
        # AP build got nothing. It's an unconditional 1.2x on magic damage
        # below 40% target HP.
        ids, sheet = self.resolve(16, ["shadowflame"])
        self.assertEqual(sheet["crit_chance"], 0.0)
        fx = builds.merge_effects(ids, self.effects)
        ranks = builds.skill_ranks(16)
        off = builds.simulate(sheet, self.kit, dict(fx, magicCrit=None), 16,
                              ranks, 2000, 0, 0, 8.0)
        on = builds.simulate(sheet, self.kit, fx, 16, ranks, 2000, 0, 0, 8.0)
        self.assertLess(on["ttk"], off["ttk"])

    def test_ap_multipliers_compound(self):
        # Rabadon x1.30 and Blackfire's x1.04 are multiplicative in game
        _, sheet = self.resolve(16, ["rabadons", "blackfire"])
        self.assertAlmostEqual(sheet["ap_mult"], 1.30 * 1.04)
        self.assertAlmostEqual(sheet["ap"], (130 + 80) * 1.30 * 1.04)

    def test_blackfire_burn(self):
        r = self.sim(16, ["blackfire"], hp=10_000, mr=0, duration=4)
        self.assertIn("blackfire", r["breakdown"])
        # 60 + 6% of 83.2 AP over each 3s refresh window, pre-mitigation
        self.assertGreater(r["breakdown"]["blackfire"], 60)

    def test_seraphs_awe(self):
        # 70 AP + 2% of its 1000 bonus mana = 90 AP
        _, sheet = self.resolve(16, ["seraphs embrace"])
        self.assertAlmostEqual(sheet["ap"], 90.0)

    def test_muramana(self):
        ids, sheet = self.resolve(16, ["muramana"])
        fc = fake_champ()
        base_mana = builds.stat_at(fc["dd"]["stats"]["mp"],
                                   fc["dd"]["stats"]["mpperlevel"], 16)
        self.assertAlmostEqual(sheet["ad_bonus"],
                               35 + 0.02 * (base_mana + 1000))
        r = self.sim(16, ["muramana"], hp=10_000, armor=0, duration=4)
        self.assertIn("muramana", r["breakdown"])  # Shock on-hit + per cast

    def test_yun_tal_stacked_crit(self):
        _, sheet = self.resolve(16, ["yun tal"])
        self.assertAlmostEqual(sheet["crit_chance"], 25.0)

    def test_yun_tal_flurry_speeds_attacks(self):
        ids, sheet = self.resolve(16, ["yun tal"])
        fx = builds.merge_effects(ids, self.effects)
        ranks = builds.skill_ranks(16)
        with_f = builds.simulate(sheet, self.kit, fx, 16, ranks,
                                 100_000, 80, 60, 8.0)
        without = builds.simulate(sheet, self.kit, dict(fx, flurry=None), 16,
                                  ranks, 100_000, 80, 60, 8.0)
        self.assertGreater(with_f["attacks"], without["attacks"])

    def test_collector_execute(self):
        # resists keep the hits small enough that one lands inside the 5%
        # window (a chunk that jumps clean past 0 correctly never executes)
        r = self.sim(16, ["collector"], hp=2200, armor=80, mr=80, duration=12)
        self.assertIn("execute", r["breakdown"])
        self.assertLessEqual(r["breakdown"]["execute"], 0.05 * 2200)
        self.assertIsNotNone(r["ttk"])

    def test_effective_ttk_discounts_overkill(self):
        # a blow that overkills lands the real kill on its own tick, but the
        # effective time interpolates back toward the previous batch
        r = self.sim(16, ["infinity edge", "collector"], hp=2200,
                     armor=80, mr=80, duration=12)
        self.assertIsNotNone(r["ttk"])
        self.assertLessEqual(r["ttk_eff"], r["ttk"])
        self.assertGreater(r["ttk_eff"], 0.0)

    def test_ranking_is_ordered_by_expected_kill_time(self):
        cands = [3031, 6676, 3036, 3072, 3115, 3089, 3135, 4645]
        results, _ = enum_one(
            fake_champ(), self.pool, self.effects, self.kit, 16,
            builds.skill_ranks(16), 2800, 110, 60, 8, candidates=cands)
        killers = [r for _, _, r in results if r["ttk"] is not None]
        exp = [r["ttk_exp"] for r in killers]
        self.assertEqual(exp, sorted(exp))
        # among builds with no execute, expected == real, so the real kill
        # times must still be ordered — no bogus overkill leapfrogging
        plain = [r["ttk"] for r in killers
                 if r["breakdown"].get("execute") is None]
        self.assertEqual(plain, sorted(plain))

    def test_execute_charged_back_to_expected_value(self):
        # deterministic crit always lands in the 5% window; real crit would
        # only ~W/D of the time, so expected sits between the two outcomes
        ids, sheet = self.resolve(16, ["berserkers", "infinity edge", "yun tal",
                                       "collector", "lord dominik"])
        fx = builds.merge_effects(ids, self.effects)
        ranks = builds.skill_ranks(16)
        a = (sheet, self.kit, fx, 16, ranks, 2800, 110, 60, 8.0)
        on = builds.simulate(*a)
        off = builds.simulate(*a[:2], dict(fx, executePct=None), *a[3:])
        self.assertGreater(on["ttk_exp"], on["ttk"])
        self.assertLess(on["ttk_exp"], off["ttk"])

    def test_no_execute_leaves_expected_equal_to_real(self):
        r = self.sim(16, ["infinity edge", "lord dominik"], hp=2200,
                     armor=80, mr=60, duration=12)
        self.assertIsNotNone(r["ttk"])
        self.assertAlmostEqual(r["ttk_exp"], r["ttk"], places=9)

    def test_effective_ttk_shrinks_execute_advantage(self):
        # Collector's execute must not buy a whole attack cycle: its worth
        # under the effective metric is well under its worth under raw ttk
        ids, sheet = self.resolve(16, ["berserkers", "infinity edge", "yun tal",
                                       "collector", "lord dominik"])
        fx = builds.merge_effects(ids, self.effects)
        ranks = builds.skill_ranks(16)
        a = (sheet, self.kit, fx, 16, ranks, 2800, 110, 60, 8.0)
        on = builds.simulate(*a)
        off = builds.simulate(*a[:2], dict(fx, executePct=None), *a[3:])
        raw = off["ttk"] - on["ttk"]
        eff = off["ttk_eff"] - on["ttk_eff"]
        self.assertGreater(raw, 0.0)
        self.assertLess(eff, raw)

    def test_navori_accelerates_q(self):
        # vs Phantom Dancer (more AS, no CDR): Navori must land more Q casts
        nav = self.sim(16, ["navori"], hp=100_000, duration=12)
        pd = self.sim(16, ["phantom dancer"], hp=100_000, duration=12)
        self.assertGreater(nav["breakdown"]["Q"], pd["breakdown"]["Q"])

    def test_giant_slayer_amps_by_bonus_hp(self):
        base = self.sim(16, ["lord dominik"], hp=10_000, duration=6)
        amped = self.sim(16, ["lord dominik"], hp=10_000, duration=6,
                         target_bonus_hp=1500)
        # every source amps 15% (E-active missing-HP feedback pushes it a bit
        # above; the auto attack line is exactly 1.15x)
        self.assertAlmostEqual(amped["breakdown"]["auto"],
                               base["breakdown"]["auto"] * 1.15, places=6)

    def test_hexoptics_amps_the_attack_only(self):
        # Magnification: +10% at/beyond 500 range. Kayle is 625 at 16, so the
        # amp is capped — and it must not touch on-hits or abilities.
        ids, sheet = self.resolve(16, ["hexoptics"])
        fx = builds.merge_effects(ids, self.effects)
        ranks = builds.skill_ranks(16)
        args = (sheet, self.kit, None, 16, ranks, 100_000, 80, 60, 6.0)
        hexo = builds.simulate(*args[:2], fx, *args[3:])
        off = builds.simulate(*args[:2], dict(fx, attackAmp=None), *args[3:])
        self.assertEqual(hexo["attacks"], off["attacks"])
        self.assertAlmostEqual(hexo["breakdown"]["auto"],
                               off["breakdown"]["auto"] * 1.10, places=6)
        for src in ("E onhit", "Q", "R", "wave"):
            self.assertAlmostEqual(hexo["breakdown"][src],
                                   off["breakdown"][src], places=6)

    def test_hexoptics_scales_down_for_short_range(self):
        # a melee-form level (pre-Arisen, 175 range) gets 175/500 of the 10%
        ids, sheet = self.resolve(5, ["hexoptics"])
        fx = builds.merge_effects(ids, self.effects)
        ranks = builds.skill_ranks(5)
        r = builds.simulate(sheet, self.kit, fx, 5, ranks, 100_000, 0, 0, 6.0)
        flat = builds.simulate(sheet, self.kit, dict(fx, attackAmp=None), 5,
                               ranks, 100_000, 0, 0, 6.0)
        self.assertAlmostEqual(r["breakdown"]["auto"],
                               flat["breakdown"]["auto"] * (1 + 0.10 * 175 / 500),
                               places=6)

    def test_abyssal_amps_magic_only(self):
        plain = self.sim(16, [], hp=100_000, duration=4)
        aby = self.sim(16, ["abyssal"], hp=100_000, duration=4)
        self.assertEqual(plain["attacks"], aby["attacks"])
        self.assertAlmostEqual(aby["breakdown"]["E onhit"],
                               plain["breakdown"]["E onhit"] * 1.12, places=6)
        self.assertAlmostEqual(aby["breakdown"]["auto"],
                               plain["breakdown"]["auto"], places=6)

    def test_ludens_procs_once(self):
        r = self.sim(16, ["ludens echo"], hp=100_000, mr=0, duration=8)
        # 150 + 10% of 100 AP, exactly once (recharge undocumented in 16.16)
        self.assertAlmostEqual(r["breakdown"]["ludens"], 160.0, places=6)

    def test_stormsurge_procs(self):
        r = self.sim(16, ["stormsurge", "rabadons", "shadowflame"],
                     hp=2800, duration=8)
        self.assertIn("stormsurge", r["breakdown"])

    def test_spellblade_unique_keeps_first(self):
        ids = [builds.resolve_item(self.pool, self.idx, t)
               for t in ("lich bane", "dusk and dawn")]
        fx = builds.merge_effects(ids, self.effects)
        self.assertAlmostEqual(fx["spellblade"]["apRatio"], 0.40)

    def test_kraken_level_window(self):
        k = self.effects[6672]["kraken"]["baseByLevel"]
        self.assertAlmostEqual(builds.by_level(k, 8), 150.0)
        self.assertAlmostEqual(builds.by_level(k, 9), 155.0)
        self.assertAlmostEqual(builds.by_level(k, 18), 200.0)

    def test_rod_of_ages_stacked(self):
        # 45 AP + 30 stacked; 350 HP + 100; 500 mana + 300
        _, sheet = self.resolve(16, ["rod of ages"])
        self.assertAlmostEqual(sheet["ap"], 75.0)
        self.assertAlmostEqual(sheet["mana_bonus"], 800.0)

    def test_overlords_ad_from_hp(self):
        # 30 AD + 2.5% of 550 bonus HP = 43.75 bonus AD
        _, sheet = self.resolve(16, ["overlord"])
        self.assertAlmostEqual(sheet["ad_bonus"], 30 + 0.025 * 550)

    def test_endless_hunger_famine_haste(self):
        # 5 + 10% of 65 bonus AD = 11.5 haste
        _, sheet = self.resolve(16, ["endless hunger"])
        self.assertAlmostEqual(sheet["haste"], 5 + 0.10 * 65)

    def test_shojin_basic_haste_and_amp(self):
        _, sheet = self.resolve(16, ["spear of shojin"])
        self.assertLess(sheet["basic_cd_mult"], sheet["cd_mult"])
        # vs a similar-AD stats item, Shojin's basic haste + Focused Will
        # must produce more Q damage over a long fight
        sho = self.sim(16, ["spear of shojin"], hp=100_000, duration=12)
        gaq = self.sim(16, ["guardian angel"], hp=100_000, duration=12)
        self.assertGreater(sho["breakdown"]["Q"], gaq["breakdown"]["Q"])

    def test_trinity_and_essence_reaver_spellblades(self):
        tri = self.sim(16, ["trinity"], hp=100_000, duration=6)
        self.assertIn("spellblade", tri["breakdown"])
        ids, sheet = self.resolve(16, ["essence reaver"])
        fx = builds.merge_effects(ids, self.effects)
        # 125% base AD + 0.5 per 1% crit (25% from the item itself)
        self.assertAlmostEqual(fx["spellblade"]["perCritChancePct"], 0.5)
        self.assertAlmostEqual(sheet["crit_chance"], 25.0)

    def test_titanic_onhit_scales_with_own_hp(self):
        r = self.sim(16, ["titanic"], hp=100_000, armor=0, duration=4)
        self.assertIn("titanic", r["breakdown"])

    def test_hullbreaker_cadence(self):
        # ranged: 4 stacks then the 5th attack procs
        r = self.sim(16, ["hullbreaker"], hp=100_000, armor=0, duration=10)
        self.assertIn("hullbreaker", r["breakdown"])

    def test_eclipse_procs_every_second_hit(self):
        r = self.sim(16, ["eclipse"], hp=100_000, armor=0, duration=6)
        self.assertIn("eclipse", r["breakdown"])
        # ranged 4% max HP per proc, pre-mitigation with 0 armor
        procs = r["breakdown"]["eclipse"] / (0.04 * 100_000)
        self.assertGreater(procs, 2)

    def test_energized_statikk(self):
        r = self.sim(16, ["statikk"], hp=100_000, mr=0, duration=10)
        self.assertIn("shiv", r["breakdown"])
        # each proc is exactly 60 pre-mitigation at 0 MR (amp-free build)
        procs = r["breakdown"]["shiv"] / 60.0
        self.assertGreaterEqual(procs, 2)

    def test_black_cleaver_shreds(self):
        # same-ish AD stats item vs Cleaver against heavy armor: Cleaver's
        # stacking 30% reduction must give more auto damage over the fight
        bc = self.sim(16, ["black cleaver"], hp=100_000, armor=250, duration=10)
        ga = self.sim(16, ["guardian angel"], hp=100_000, armor=250, duration=10)
        self.assertGreater(bc["breakdown"]["auto"], ga["breakdown"]["auto"] * 1.1)

    def test_hexplate_ult_steroid(self):
        ids, sheet = self.resolve(16, ["hexplate"])
        fx = builds.merge_effects(ids, self.effects)
        ranks = builds.skill_ranks(16)
        with_r = builds.simulate(sheet, self.kit, fx, 16, ranks,
                                 100_000, 80, 60, 8.0)
        no_r = builds.simulate(sheet, self.kit, fx, 16, ranks,
                               100_000, 80, 60, 8.0, use_ult=False)
        self.assertGreater(with_r["attacks"], no_r["attacks"])

    def test_horizon_focus_amps_after_opener(self):
        ids, sheet = self.resolve(16, ["horizon"])
        fx = builds.merge_effects(ids, self.effects)
        ranks = builds.skill_ranks(16)
        on = builds.simulate(sheet, self.kit, fx, 16, ranks, 100_000, 80, 60, 6.0)
        off = builds.simulate(sheet, self.kit, dict(fx, hypershot=None), 16,
                              ranks, 100_000, 80, 60, 6.0)
        self.assertGreater(on["total"], off["total"] * 1.05)

    def test_item_actives_fire_once(self):
        r = self.sim(16, ["gunblade"], hp=100_000, mr=0, duration=8)
        # 175->253 by level: level 16 = 175 + 78*15/17, +30% of 80 AP
        expected = 175 + 78 * 15 / 17 + 0.30 * 80
        self.assertAlmostEqual(r["breakdown"]["active"], expected, places=4)

    def test_umbral_true_damage_opener(self):
        r = self.sim(16, ["umbral"], hp=100_000, armor=300, mr=300, duration=4)
        # true damage ignores the 300 resists: exactly 50 + 1.5 * 18 lethality
        self.assertAlmostEqual(r["breakdown"]["umbral"], 50 + 1.5 * 18, places=4)

    def test_exclusive_groups_come_from_the_game_bin(self):
        # the groups are Riot's own mItemGroups, so assert the memberships
        # that actually bite — including the two that hand-curation missed
        groups, caps = builds.load_exclusive_groups()
        self.assertTrue(groups, "no item groups loaded")

        def named(label):
            return {i for i, gs in groups.items()
                    if any(builds.group_name(g) == label for g in gs)}
        lw = named("Last Whisper")
        for iid in (3036, 3033, 6694, 3302, 3071):  # incl. Terminus, Cleaver
            self.assertIn(iid, lw, f"{iid} should be a Last Whisper item")
        # Terminus sits in two groups at once — the old one-group-per-item
        # model could not express this
        self.assertIn(3302, named("Void Pen"))
        self.assertIn(3040, named("Lifeline Items"))  # Seraph's, also missed
        for g in groups.get(3036, ()):
            self.assertEqual(caps[g], 1)

    def test_enumerator_respects_exclusive_groups(self):
        # candidates stacked with Last Whisper items, including Terminus and
        # Black Cleaver: no result may hold two of any capped group
        champ = fake_champ()
        cands = [3036, 3033, 6694, 3302, 3071, 3031, 3032, 6676, 3115]
        results, count = enum_one(
            champ, self.pool, self.effects, self.kit, 16,
            builds.skill_ranks(16), 2800, 110, 60, 8, candidates=cands)
        groups, caps = builds.load_exclusive_groups()
        self.assertTrue(results)
        for ids, _, _ in results:
            self.assertTrue(builds.build_is_legal(ids, groups, caps),
                            f"unbuyable build survived: {ids}")

    def test_pool_is_buyable_with_gold_alone(self):
        # Feats of Strength boots (Gunmetal Greaves, Spellslinger's, ...) and
        # the support-quest line still read as purchasable map-11 items in
        # ddragon; only the item bin's currency flag keeps them out
        gated = builds.load_gated_items()
        self.assertTrue(gated, "no currency-gated items loaded")
        offenders = [(i, self.pool[i]["name"], gated[i])
                     for i in builds.DEFAULT_POOL + builds.BOOTS if i in gated]
        self.assertEqual(offenders, [])
        self.assertIn(3175, gated)  # Spellslinger's Shoes, a known T3 boot

    def test_pool_has_no_retired_items(self):
        # an item ddragon marks unpurchasable while it still has a recipe was
        # pulled from the shop (Opportunity); meraki keeps calling it buyable
        retired = builds.load_retired_items()
        self.assertIn(6701, retired)
        offenders = [(i, self.pool[i]["name"])
                     for i in builds.DEFAULT_POOL + builds.BOOTS
                     if i in retired]
        self.assertEqual(offenders, [])
        # transformations are unpurchasable but legitimate — must NOT be swept up
        for iid in (3040, 3042):  # Seraph's Embrace, Muramana
            self.assertNotIn(iid, retired)

    def test_terminus_and_lord_dominik_never_pair(self):
        groups, caps = builds.load_exclusive_groups()
        self.assertFalse(builds.build_is_legal([3302, 3036], groups, caps))
        self.assertTrue(builds.build_is_legal([3302, 3031], groups, caps))

    def test_pool_has_no_unmapped_stats_or_uncovered_passives(self):
        # every pool item must resolve without stat warnings and leave no
        # unexplained passive in `uncovered`
        for iid in builds.DEFAULT_POOL + builds.BOOTS:
            _, sheet = self.resolve(16, [str(iid)])
            self.assertEqual(sheet["uncovered"], [],
                             f"{self.pool[iid]['name']}: {sheet['uncovered']}")


def fake_vlad():
    """A Vladimir-shaped champion snapshot (patch 16.17 ddragon values)."""
    dd = {"name": "Vladimir", "stats": {
        "hp": 600, "hpperlevel": 110, "mp": 2, "mpperlevel": 0,
        "armor": 24, "armorperlevel": 4.5,
        "spellblock": 30, "spellblockperlevel": 1.3,
        "attackdamage": 55, "attackdamageperlevel": 0,
        "attackspeed": 0.658, "attackspeedperlevel": 2,
        "movespeed": 330, "attackrange": 450,
    }}
    mk = {"stats": {"attackSpeedRatio": {"flat": 0.658},
                    "criticalStrikeDamage": {"flat": 175.0}}}
    return {"slug": "vladimir", "dd": dd, "mk": mk, "meta": {"patch": "16.17"}}


class TestVladimirKit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kit = builds.load_kit("vladimir")

    def test_shape(self):
        for slot, ranks in [("Q", 5), ("W", 5), ("E", 5), ("R", 3)]:
            self.assertEqual(len(self.kit["abilities"][slot]["cooldownS"]), ranks)
        e = self.kit["abilities"]["E"]["damage"]
        self.assertEqual(len(e["min"]["base"]), 5)
        self.assertEqual(len(e["max"]["base"]), 5)
        self.assertEqual(self.kit["abilities"]["Q"]["crimsonRush"]["everyNthCast"], 3)
        self.assertTrue(self.kit["manaless"])
        self.assertEqual(builds.kit_max_order(self.kit), ("Q", "E", "W"))
        self.assertEqual(builds.kit_max_order(self.kit, "e,q,w"), ("E", "Q", "W"))

    def test_registered(self):
        self.assertEqual(builds.kit_champions(), ["kayle", "vladimir"])

    def test_own_health_ratios(self):
        # E at full charge, rank 5: 180 + 80% AP + 6% of OWN max health
        sheet = {"ad": 55.0, "ad_bonus": 0.0, "ap": 100.0, "hp": 3000.0,
                 "hp_bonus": 800.0}
        e = self.kit["abilities"]["E"]["damage"]["max"]
        self.assertAlmostEqual(builds.ability_hit(e, 5, sheet), 180 + 80 + 180)
        # W rank 5 over the pool: 300 + 15% bonus health
        w = self.kit["abilities"]["W"]["damage"]
        self.assertAlmostEqual(builds.ability_hit(w, 5, sheet), 300 + 120)


class TestVladimirEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kit = builds.load_kit("vladimir")  # as played: never attacks
        # the same kit with autos on, to pin the driver's channel rules
        # (charge and pool vs attacks) that other kits rely on
        cls.kit_autos = copy.deepcopy(cls.kit)
        del cls.kit_autos["attack"]["never"]
        cls.patch, cls.pool = builds.load_items()
        cls.idx = builds.item_index(cls.pool)
        cls.effects = builds.load_item_effects()

    def resolve(self, level, tokens, effects=None):
        ids = [builds.resolve_item(self.pool, self.idx, t) for t in tokens]
        return ids, builds.resolve_stats(fake_vlad(), level, ids, self.pool,
                                         effects or self.effects, kit=self.kit)

    def sim(self, level, tokens, hp=2800, armor=80, mr=60, duration=8.0,
            use_ult=True, kit=None, effects=None, **kw):
        fx = effects or self.effects
        ids, sheet = self.resolve(level, tokens, fx)
        return builds.simulate(sheet, kit or self.kit,
                               builds.merge_effects(ids, fx), level,
                               builds.skill_ranks(level), hp, armor, mr,
                               duration, use_ult=use_ult, **kw)

    def test_ramp_amps_by_seconds_in_combat(self):
        # Liandry's Suffering: +2% per whole second in combat, capped at 6%.
        # Against the same build with the ramp zeroed, each source's ratio is
        # the average multiplier over its casts — Q at 0 and 4.6 -> 1.03, E at
        # 1.25 and 6.25 -> 1.04, the Crimson Rush Q at 9.2 -> 1.06, the W
        # ticks at 0.25/0.75/1.25/1.75 -> 1.01 (timings as pinned above)
        flat = copy.deepcopy(self.effects)
        flat[6653]["dmgAmp"] = {"pctPerStack": 0, "maxStacks": 3}
        flat[4633]["dmgAmp"] = {"pctPerStack": 0, "maxStacks": 4}
        args = dict(hp=100_000, armor=0, mr=0, duration=10.0, use_ult=False)
        on = self.sim(16, ["liandry"], **args)
        off = self.sim(16, ["liandry"], effects=flat, **args)
        ratio = lambda src: on["breakdown"][src] / off["breakdown"][src]
        self.assertAlmostEqual(ratio("Q"), 1.03)
        self.assertAlmostEqual(ratio("E"), 1.04)
        self.assertAlmostEqual(ratio("Q empowered"), 1.06)
        self.assertAlmostEqual(ratio("W"), 1.01)
        # A cast landing on a second boundary gets that second's stack even
        # when the machine computes the moment a hair short: Riftmaker's 15
        # haste puts Q at 0, 4.6 / 1.15 = 3.9999999999999996 and 8.0 — four
        # stacks at 4.0 (1.00 and 1.08 average 1.04), not three
        on = self.sim(16, ["riftmaker"], **args)
        off = self.sim(16, ["riftmaker"], effects=flat, **args)
        self.assertAlmostEqual(ratio("Q"), 1.04)
        self.assertAlmostEqual(ratio("Q empowered"), 1.08)
        # The clock starts at the first damage dealt, not at t=0: with the ult
        # opening the fight the first hit is Q at 0.25 (R's cast lockout), so
        # Hemoplague's burst at 4.0 has been in combat 3.75s — three stacks
        on = self.sim(16, ["riftmaker"], hp=100_000, armor=0, mr=0, duration=4.0)
        off = self.sim(16, ["riftmaker"], hp=100_000, armor=0, mr=0, duration=4.0,
                       effects=flat)
        self.assertAlmostEqual(ratio("R"), 1.06)

    def test_never_attacks(self):
        # a crit/on-hit/spellblade build gets nothing from its passives —
        # only the rotation and the raw stats count
        r = self.sim(16, ["infinity edge", "kraken slayer", "lich bane"],
                     hp=100_000, duration=8.0)
        self.assertEqual(r["attacks"], 0)
        for src in ("auto", "onhit", "kraken", "spellblade"):
            self.assertNotIn(src, r["breakdown"])
        for src in ("Q", "E", "W", "R"):
            self.assertIn(src, r["breakdown"])
        # and the rotation itself is exactly what it was with autos on
        on = self.sim(16, [], hp=100_000, armor=0, mr=0, duration=10.0,
                      use_ult=False, kit=self.kit_autos)
        off = self.sim(16, [], hp=100_000, armor=0, mr=0, duration=10.0,
                       use_ult=False)
        self.assertIn("auto", on["breakdown"])
        self.assertNotIn("auto", off["breakdown"])
        for src in ("Q", "Q empowered", "E", "W"):
            self.assertAlmostEqual(off["breakdown"][src], on["breakdown"][src])
        meta = builds.api_builds_meta()
        by_slug = {c["slug"]: c for c in meta["champions"]}
        self.assertTrue(any("never auto-attacks" in n
                            for n in by_slug["vladimir"]["notes"]))
        self.assertEqual(by_slug["kayle"]["notes"], [])

    def item_stat(self, iid, stat):
        return self.pool[iid]["stats"].get(stat, {}).get("flat", 0.0)

    def test_crimson_pact(self):
        # Rylai's: its AP plus 1 AP per 30 of its health; then 1.6 health per
        # point of AP that did NOT come from the pact itself
        ids, sheet = self.resolve(16, ["rylai"])
        ap_i, hp_i = (self.item_stat(ids[0], k) for k in ("abilityPower", "health"))
        self.assertGreater(hp_i, 0)
        self.assertAlmostEqual(sheet["ap"], ap_i + hp_i / 30)
        base_hp = builds.stat_at(600, 110, 16)
        self.assertAlmostEqual(sheet["hp"], base_hp + hp_i + 1.6 * ap_i)
        self.assertAlmostEqual(sheet["hp_bonus"], hp_i + 1.6 * ap_i)

    def test_crimson_pact_rabadon(self):
        # The wiki's Rabadon's figures: bonus AP = 30% AP + 4.333% bonus
        # health, bonus health = 208% AP + 1.6% bonus health — Rabadon's 30%
        # of the pact's AP is credited to Rabadon's, so it does earn health
        ids, sheet = self.resolve(16, ["rabadons", "rylai"])
        ap_i = sum(self.item_stat(i, "abilityPower") for i in ids)
        hp_i = sum(self.item_stat(i, "health") for i in ids)
        self.assertAlmostEqual(sheet["ap"], 1.30 * ap_i + 1.30 / 30 * hp_i)
        self.assertAlmostEqual(sheet["hp_bonus"],
                               hp_i + 2.08 * ap_i + 0.016 * hp_i)

    def test_crimson_pact_riftmaker_fixed_point(self):
        # Riftmaker's 2% of bonus health counts the pact's health, whose AP
        # grows the health again: the closed form must survive one more
        # pass of the loop unchanged
        ids, sheet = self.resolve(16, ["rabadons", "riftmaker"])
        ap_i = sum(self.item_stat(i, "abilityPower") for i in ids)
        hp_i = sum(self.item_stat(i, "health") for i in ids)
        ap, hp_pact = sheet["ap"], sheet["hp_bonus"] - hp_i
        again = 1.30 * (ap_i + 0.02 * (hp_i + hp_pact) + hp_i / 30)
        self.assertAlmostEqual(ap, again, places=6)
        self.assertAlmostEqual(hp_pact, 1.6 * (ap - hp_i / 30), places=6)
        self.assertAlmostEqual(sheet["ap"], sheet["ap_flat"] * sheet["ap_mult"])

    def test_ad_growth_falls_back_to_meraki(self):
        # ddragon 16.5+ zeroes attackdamageperlevel for everyone; meraki's
        # 3/level stands in (Riot's files agree) — 55 + 3 * growth(16)
        champ = fake_vlad()
        champ["mk"]["stats"]["attackDamage"] = {"flat": 55, "perLevel": 3}
        s = builds.resolve_stats(champ, 16, [], {}, effects={}, kit=self.kit)
        self.assertAlmostEqual(s["ad"], 55 + 3 * builds.growth(16))
        # a genuinely flat champion (no meraki growth either) stays flat
        s = builds.resolve_stats(fake_vlad(), 16, [], {}, effects={}, kit=self.kit)
        self.assertAlmostEqual(s["ad"], 55.0)

    def test_level1_hand_computed(self):
        # Q at t=0 (80 magic, 0.25s lockout), one 55-AD auto at 0.25; the
        # next auto (1/0.658 later) is past 1s
        r = self.sim(1, [], hp=10_000, armor=0, mr=0, duration=1.0,
                     use_ult=False, kit=self.kit_autos)
        self.assertEqual(r["attacks"], 1)
        self.assertAlmostEqual(r["breakdown"]["Q"], 80.0)
        self.assertAlmostEqual(r["breakdown"]["auto"], 55.0)
        self.assertAlmostEqual(r["total"], 135.0)

    def test_rotation_hand_computed(self):
        # Level 16 naked, no ult, 0 MR, 10s. Q (4.6s cd) at 0, 4.6, 9.2 —
        # the third is Crimson Rush: 160 x 1.85 = 296. E charges at 0.25
        # (Q's cast time) for 1s and lands 180 + 6% of 2192.25 max health;
        # again at 6.25 (5s cd from the release). W rides the first charge:
        # 190 over four ticks. No items, so no amps and no bonus health.
        r = self.sim(16, [], hp=100_000, armor=0, mr=0, duration=10.0,
                     use_ult=False)
        self.assertAlmostEqual(r["breakdown"]["Q"], 320.0)
        self.assertAlmostEqual(r["breakdown"]["Q empowered"], 296.0)
        e_hit = 180 + 0.06 * builds.stat_at(600, 110, 16)
        self.assertAlmostEqual(r["breakdown"]["E"], 2 * e_hit)
        self.assertAlmostEqual(r["breakdown"]["W"], 190.0)
        self.assertAlmostEqual(sum(r["breakdown"].values()), r["total"], places=6)

    def test_e_charge_pauses_attacks(self):
        # Level 1 (Q only): autos at 0.25 and 1.77. Level 2 (Q, E): the auto
        # at 0.25 weaves in ahead of the charge, which then holds attacks
        # until its release at 1.25 plus the cast lockout — the next auto is
        # at 1.5, so a 1.4s window sees one attack instead of two.
        q_only = self.sim(1, [], hp=100_000, armor=0, mr=0, duration=1.8,
                          use_ult=False, kit=self.kit_autos)
        with_e = self.sim(2, [], hp=100_000, armor=0, mr=0, duration=1.4,
                          use_ult=False, kit=self.kit_autos)
        self.assertEqual(q_only["attacks"], 2)
        self.assertEqual(with_e["attacks"], 1)
        self.assertIn("E", with_e["breakdown"])

    def test_hemoplague_amps_everything_including_itself(self):
        # 4s fight at 0 resists: R's 10% holds through its own burst at 4.0s
        # (350 x 1.1 = 385) and every auto inside the window is 55 x 1.1
        r = self.sim(16, [], hp=100_000, armor=0, mr=0, duration=4.0,
                     kit=self.kit_autos)
        self.assertAlmostEqual(r["breakdown"]["R"], 385.0)
        self.assertAlmostEqual(r["breakdown"]["auto"], r["attacks"] * 55 * 1.1)
        off = self.sim(16, [], hp=100_000, armor=0, mr=0, duration=4.0,
                       use_ult=False)
        self.assertNotIn("R", off["breakdown"])

    def test_true_damage_escapes_hemoplague(self):
        # Umbral's opener is true damage: exactly 50 + 1.5 x 18 lethality,
        # untouched by the 10% amp it lands inside of
        r = self.sim(16, ["umbral"], hp=100_000, armor=300, mr=300, duration=4.0,
                     kit=self.kit_autos)
        self.assertAlmostEqual(r["breakdown"]["umbral"], 50 + 1.5 * 18, places=4)

    def test_pool_blocks_casts_but_not_the_charged_release(self):
        # Q at 0, one auto weaves in at 0.25 as the charge and the pool start
        # together; the release still lands inside the pool at 1.25, and
        # nothing else attacks or casts before the pool ends at 2.25
        r = self.sim(16, [], hp=100_000, armor=0, mr=0, duration=2.24,
                     use_ult=False, kit=self.kit_autos)
        self.assertEqual(r["attacks"], 1)
        self.assertIn("E", r["breakdown"])
        self.assertAlmostEqual(r["breakdown"]["W"], 190.0)
        self.assertAlmostEqual(r["breakdown"]["Q"], 160.0)
        longer = self.sim(16, [], hp=100_000, armor=0, mr=0, duration=2.26,
                          use_ult=False, kit=self.kit_autos)
        self.assertEqual(longer["attacks"], 2)

    def test_ability_items_ride_the_casts(self):
        # burns and Luden's ride the casts; spellblade needs the attack the
        # kit never makes (it does fire with autos on)
        r = self.sim(16, ["lich bane", "liandry", "ludens echo"],
                     hp=100_000, duration=8.0)
        for src in ("burn", "ludens"):
            self.assertIn(src, r["breakdown"])
        self.assertNotIn("spellblade", r["breakdown"])
        self.assertAlmostEqual(sum(r["breakdown"].values()), r["total"], places=6)
        on = self.sim(16, ["lich bane"], hp=100_000, duration=8.0,
                      kit=self.kit_autos)
        self.assertIn("spellblade", on["breakdown"])

    def test_manaless_pool(self):
        vlad = builds.champion_pool(self.kit, self.effects)
        kayle = builds.champion_pool(builds.load_kit("kayle"), self.effects)
        self.assertEqual(kayle, builds.DEFAULT_POOL)
        for iid in (3040, 3042, 2522):  # Seraph's, Muramana, Actualizer
            self.assertIn(iid, kayle)
            self.assertNotIn(iid, vlad)
        self.assertEqual(len(vlad), len(builds.DEFAULT_POOL) - 3)
        meta = builds.api_builds_meta()
        by_slug = {c["slug"]: c for c in meta["champions"]}
        self.assertEqual(by_slug["vladimir"]["pool"], vlad)
        self.assertEqual(by_slug["vladimir"]["name"], "Vladimir")
        self.assertTrue(any("mana" in x for x in by_slug["vladimir"]["excluded"]))
        self.assertEqual(by_slug["kayle"]["excluded"], [])

    def test_ranking_prefers_kill_time(self):
        cands = [3089, 3135, 4645, 6653, 4633, 3100, 3115, 3031]
        results, _ = enum_one(
            fake_vlad(), self.pool, self.effects, self.kit, 16,
            builds.skill_ranks(16), 2800, 110, 60, 8, candidates=cands)
        killers = [r for _, _, r in results if r["ttk"] is not None]
        self.assertTrue(killers)
        exp = [r["ttk_exp"] for r in killers]
        self.assertEqual(exp, sorted(exp))



class TestScenarioCache(unittest.TestCase):
    """The precomputed-cell layer: tiers, warm order, cache paths, read-only
    access, compute, and the warm lock — on a tiny item pool in a temp cache
    dir, so every cell is instant and the real .cache/builds/ is never
    touched."""
    # Rabadon, Void Staff, Shadowflame, Liandry, Riftmaker, Nashor, Muramana
    TINY_POOL = [3089, 3135, 4645, 6653, 4633, 3115, 3042]
    # a budget tier that doesn't ship, to exercise the multi-tier paths:
    # warm order (cheap before full) and per-tier cache invalidation
    PROBE = {"probe-squishy": dict(label="Probe vs squishy", tier="probe",
                                   target="squishy", level=9, targetHp=1900,
                                   armor=50, mr=40, duration=8, budget=4500,
                                   targetBonusHp=400)}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        for name, value in (("SCENARIO_CACHE_DIR", self.tmp),
                            ("DEFAULT_POOL", self.TINY_POOL)):
            patcher = mock.patch.object(builds, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_scenarios_form_tiers(self):
        # every scenario belongs to a tier; a tier's targets share the level
        # and budget its overall cell carries (one pass = one stat sheet per
        # build), and an overall cell only exists where there is something
        # to average — two or more targets — and is listed last
        for key, sc in builds.SCENARIOS.items():
            self.assertIn("tier", sc, key)
            self.assertEqual("target" in sc, not sc.get("overall"), key)
        self.assertEqual(list(builds.SCENARIOS),
                         ["full-squishy", "full-bruiser", "full-tank",
                          "full-overall"])
        self.assertEqual(builds.tiers(), ["full"])
        for tier in builds.tiers():
            keys = builds.tier_scenarios(tier)
            targets = builds.tier_targets(tier)
            self.assertTrue(targets)
            overall = [k for k in keys if k not in targets]
            self.assertLessEqual(len(overall), 1)
            if overall:
                self.assertEqual(keys[-1], overall[0])
                self.assertGreaterEqual(len(targets), 2)
            for k in keys:
                self.assertEqual(builds.SCENARIOS[k]["level"],
                                 builds.SCENARIOS[keys[0]]["level"], k)
                self.assertEqual(builds.SCENARIOS[k].get("budget"),
                                 builds.SCENARIOS[keys[0]].get("budget"), k)
        self.assertEqual(builds.tier_targets("full"),
                         ["full-squishy", "full-bruiser", "full-tank"])
        self.assertEqual(builds.tier_scenarios("full")[-1], "full-overall")

    def test_cells_cheapest_first(self):
        champs = builds.kit_champions()
        cs = builds.cells()
        self.assertEqual(len(cs), len(champs) * len(builds.SCENARIOS))
        self.assertEqual(len(set(cs)), len(cs))
        self.assertEqual(cs[0], (champs[0], "full-squishy"))
        # with a budget tier in the mix: tier by tier, budget presets before
        # full builds (seconds, not half an hour), shorter total fights
        # first; every champion gets a tier's cells before anyone gets a
        # costlier tier's; and one champion's cells of a tier are adjacent,
        # since they come from one pass
        with mock.patch.dict(builds.SCENARIOS, self.PROBE):
            self.assertEqual(builds.tiers(), ["probe", "full"])
            cs = builds.cells()
            self.assertEqual(len(cs), len(champs) * len(builds.SCENARIOS))
            def cost(key):
                ts = builds.tier_targets(builds.SCENARIOS[key]["tier"])
                return (builds.SCENARIOS[ts[0]].get("budget") is None,
                        sum(builds.SCENARIOS[k]["duration"] for k in ts))
            costs = [cost(k) for _, k in cs]
            self.assertEqual(costs, sorted(costs))
            groups = [(slug, builds.SCENARIOS[k]["tier"]) for slug, k in cs]
            runs = 1 + sum(a != b for a, b in zip(groups, groups[1:]))
            self.assertEqual(runs, len(set(groups)))
            for tier in builds.tiers():
                self.assertEqual(
                    [s for s, t in dict.fromkeys(groups) if t == tier], champs)
            self.assertEqual(cs[:len(champs)],
                             [(s, "probe-squishy") for s in champs])

    def test_cell_paths_cover_code_and_inputs(self):
        paths = builds.cell_paths()
        self.assertEqual(set(paths), set(builds.cells()))
        self.assertTrue(all(p.startswith(self.tmp) for p in paths.values()))
        self.assertEqual(len(set(paths.values())), len(paths))
        self.assertFalse(builds.source_stale())
        with mock.patch.object(builds, "SOURCE_HASH", "0" * 64):
            self.assertTrue(builds.source_stale())
            other = builds.cell_paths()
        self.assertTrue(set(other.values()).isdisjoint(paths.values()))
        # a target's change reaches every cell of its tier — they come from
        # the same pass, and the overall cell depends on all of them — and
        # no cell of any other tier; and the shipped cells don't care
        # whether another tier exists
        with mock.patch.dict(builds.SCENARIOS, self.PROBE):
            both = builds.cell_paths()
            self.assertEqual({c: both[c] for c in paths}, paths)
            with mock.patch.dict(builds.SCENARIOS["probe-squishy"],
                                 {"armor": 51}):
                changed = builds.cell_paths()
            for cell in both:
                same = builds.SCENARIOS[cell[1]]["tier"] != "probe"
                self.assertEqual(changed[cell] == both[cell], same, cell)
            with mock.patch.dict(builds.SCENARIOS["full-squishy"],
                                 {"armor": 111}):
                changed = builds.cell_paths()
            for cell in both:
                same = builds.SCENARIOS[cell[1]]["tier"] != "full"
                self.assertEqual(changed[cell] == both[cell], same, cell)

    def test_read_only_then_compute(self):
        cell = ("kayle", "full-squishy")
        paths = builds.cell_paths()
        with mock.patch.object(builds, "enumerate_builds",
                               side_effect=AssertionError("simulated on read")):
            self.assertIsNone(builds.cached_scenario(*cell))
        self.assertRaises(ValueError, builds.cached_scenario, "kayle", "nope")
        self.assertRaises(ValueError, builds.cached_scenario, "teemo", "full-squishy")
        self.assertRaises(ValueError, builds.cached_scenario, "kayle", "first-item")
        stale = os.path.join(self.tmp, "kayle-full-squishy-0000000000000000.json")
        open(stale, "w").close()
        d = builds.compute_scenario(*cell, paths)
        self.assertTrue(os.path.exists(paths[cell]))
        self.assertFalse(os.path.exists(stale))  # older generation retired
        self.assertFalse(os.path.exists(paths[cell] + ".tmp"))
        self.assertEqual(builds.cached_scenario(*cell), d)
        self.assertEqual(d["champion"], "kayle")
        self.assertEqual(d["scenario"]["key"], "full-squishy")
        self.assertEqual(d["scenario"]["tier"], "full")
        self.assertEqual([t["key"] for t in d["scenario"]["targets"]],
                         ["full-squishy", "full-bruiser", "full-tank"])
        self.assertEqual([r["rank"] for r in d["rows"]],
                         list(range(1, len(d["rows"]) + 1)))
        self.assertTrue(d["rows"])
        for r in d["rows"]:
            self.assertEqual(len(r["items"]), 6)
            self.assertAlmostEqual(sum(r["breakdown"].values()), r["total"],
                                   delta=len(r["breakdown"]))  # rounding
            # every fight of the tier is under vs, keyed by the target name;
            # the row's own is the squishy one
            self.assertEqual(list(r["vs"]), ["squishy", "bruiser", "tank"])
            v = r["vs"]["squishy"]
            self.assertEqual((v["ttk"], v["ttkExp"], v["dps"], v["total"]),
                             (r["ttk"], r["ttkExp"], r["dps"], r["total"]))
            self.assertEqual(v["breakdown"], r["breakdown"])
            self.assertGreaterEqual(v["loss"], 1.0)
        ttks = [r["ttk"] for r in d["rows"] if r["ttk"] is not None]
        self.assertEqual(ttks, sorted(ttks))
        if ttks:  # rank 1 is the fastest kill: loss 1.0 by definition
            self.assertEqual(d["rows"][0]["vs"]["squishy"]["loss"], 1.0)
        self.assertIn("computedAt", d)
        self.assertGreaterEqual(d["computeSeconds"], 0)

    def test_vladimir_cell_drops_mana_items(self):
        cell = ("vladimir", "full-tank")
        d = builds.compute_scenario(*cell, builds.cell_paths())
        self.assertEqual(d["championName"], "Vladimir")
        kit = builds.load_kit("vladimir")
        self.assertEqual(d["ranks"],
                         builds.skill_ranks(16, builds.kit_max_order(kit)))
        self.assertTrue(d["rows"])
        for r in d["rows"]:
            self.assertNotIn("Muramana", r["items"])

    def test_warm_computes_cold_cells_once_and_respects_lock(self):
        # a cell of a scenario that no longer ships is swept, not kept forever
        stray = os.path.join(self.tmp, "kayle-mid-squishy-0123456789abcdef.json")
        open(stray, "w").close()
        log = []
        self.assertEqual(builds.warm(log=log.append), len(builds.cells()))
        self.assertFalse(os.path.exists(stray))
        self.assertTrue(all(builds.cell_ready().values()))
        # one pass per (champion, tier), announced with the cells it fills
        heads = [l for l in log if l.startswith("[")]
        self.assertEqual(len(heads),
                         len(builds.tiers()) * len(builds.kit_champions()))
        self.assertTrue(any("full-overall" in h for h in heads))
        self.assertEqual(builds.warm(log=log.append), 0)  # nothing cold now
        lock = builds.warm_lock()
        try:
            self.assertTrue(builds.warm_running())
            self.assertIsNone(builds.warm(log=log.append))
        finally:
            lock.close()
        self.assertFalse(builds.warm_running())


class TestOverallRanking(unittest.TestCase):
    """The cross-target 'overall' cell: its sort key, and the tier pass that
    fills it alongside the per-target cells it must agree with."""

    def test_kill_time_extends_past_a_survived_fight(self):
        dead = {"ttk": 3.0, "ttk_exp": 3.4, "ttk_eff": 2.9, "hp_left": 0.0,
                "dps": 500.0, "total": 1500.0}
        alive = {"ttk": None, "ttk_exp": None, "ttk_eff": None,
                 "hp_left": 500.0, "dps": 100.0, "total": 800.0}
        self.assertEqual(builds.kill_time(dead, 8), 3.4)
        # an 8s fight with 500 hp left at 100 DPS: five more seconds
        self.assertAlmostEqual(builds.kill_time(alive, 8), 13.0)
        self.assertIsNone(builds.kill_time(dict(alive, dps=0.0), 8))

    @staticmethod
    def fight(ttk, eff=None, hp_left=0.0, dps=100.0):
        return {"ttk": ttk, "ttk_exp": ttk,
                "ttk_eff": eff if eff is not None else ttk,
                "hp_left": hp_left, "dps": dps, "total": 1000.0}

    def test_overall_key_kills_first_then_geometric_mean(self):
        T = {"a": {"duration": 8}, "b": {"duration": 12}}
        f = self.fight
        both = {"a": f(2.0), "b": f(8.0)}   # geometric mean 4.0
        slow = {"a": f(4.0), "b": f(9.0)}   # 6.0
        # fastest on a, but leaves b standing: 12s + 100hp / 100dps = 13s
        fails = {"a": f(1.0), "b": f(None, hp_left=100.0)}
        k_both, k_slow, k_fails = (builds.overall_key(rs, T)
                                   for rs in (both, slow, fails))
        self.assertEqual(k_both[0], 0)
        self.assertAlmostEqual(k_both[1], 4.0)
        self.assertEqual(k_fails[0], 1)
        self.assertAlmostEqual(k_fails[1], math.sqrt(13.0))
        self.assertEqual(sorted([k_fails, k_slow, k_both]),
                         [k_both, k_slow, k_fails])
        # percentage-symmetric: 10% slower on one target and 10% faster on
        # the other is level pegging
        level = {"a": f(2.0 * 1.1), "b": f(8.0 / 1.1)}
        self.assertAlmostEqual(builds.overall_key(level, T)[1], 4.0)
        # equal expected times: damage to spare (the interpolated time) wins
        spare = {"a": f(2.0, eff=1.5), "b": f(8.0, eff=7.0)}
        self.assertLess(builds.overall_key(spare, T), k_both)
        # a build that never damages a target can't be placed: last
        nothing = {"a": f(2.0), "b": f(None, hp_left=3800.0, dps=0.0)}
        self.assertGreater(builds.overall_key(nothing, T), k_fails)

    def test_tier_pass_agrees_with_single_target_passes(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with mock.patch.object(builds, "SCENARIO_CACHE_DIR", tmp), \
             mock.patch.object(builds, "DEFAULT_POOL",
                               TestScenarioCache.TINY_POOL):
            paths = builds.cell_paths()
            outs = builds.compute_tier("kayle", "full", paths)
            self.assertEqual(set(outs), {"full-squishy", "full-bruiser",
                                         "full-tank", "full-overall"})
            for key in outs:
                self.assertTrue(os.path.exists(paths[("kayle", key)]))
            champ = builds.load_champion("kayle")
            _, pool = builds.load_items()
            effects = builds.load_item_effects()
            kit = builds.load_kit("kayle")
            cands = builds.champion_pool(kit, effects)
            # each per-target cell is exactly what a pass over that target
            # alone ranks — the shared pass changes nothing but the cost
            targets = builds.tier_targets("full")
            names = [builds.SCENARIOS[k]["target"] for k in targets]
            for key in targets:
                sc = builds.SCENARIOS[key]
                single, count = enum_one(
                    champ, pool, effects, kit, sc["level"],
                    builds.skill_ranks(sc["level"], builds.kit_max_order(kit)),
                    sc["targetHp"], sc["armor"], sc["mr"], sc["duration"],
                    bonus_hp=sc["targetBonusHp"], budget=sc.get("budget"),
                    candidates=cands)
                d = outs[key]
                self.assertEqual(d["buildsEvaluated"], count)
                single = single[:builds.CACHED_ROWS]
                self.assertEqual([r["items"] for r in d["rows"]],
                                 [[pool[i]["name"] for i in ids]
                                  for ids, _, _ in single])
                self.assertEqual(
                    [r["ttkExp"] for r in d["rows"]],
                    [round(r["ttk_exp"], 2) if r["ttk_exp"] is not None
                     else None for _, _, r in single])
                top = d["rows"][0]
                if top["ttk"] is not None:
                    self.assertEqual(top["vs"][sc["target"]]["loss"], 1.0)
        ov = outs["full-overall"]
        self.assertTrue(ov["scenario"]["overall"])
        self.assertEqual([t["key"] for t in ov["scenario"]["targets"]], targets)
        rows = ov["rows"]
        self.assertTrue(rows)
        # the tiny pool fits in one cell, so every list holds every build
        self.assertEqual(len(rows), len(outs[targets[0]]["rows"]))
        geo = lambda xs: math.prod(xs) ** (1 / len(xs))
        order = [(-r["kills"], r["mean"]) for r in rows]
        self.assertEqual(order, sorted(order))
        for r in rows:
            times = [r["vs"][t]["killTime"] for t in names]
            self.assertAlmostEqual(r["mean"], geo(times), delta=0.02)  # rounded
            for t in names:
                self.assertGreaterEqual(r["vs"][t]["loss"], 1.0)
            self.assertEqual(r["kills"],
                             sum(r["vs"][t]["ttk"] is not None for t in names))
        # the winner's fights are the same ones its per-target rows report
        best = rows[0]
        for key, t in zip(targets, names):
            twin = next(r for r in outs[key]["rows"]
                        if r["items"] == best["items"])
            self.assertEqual(twin["vs"], best["vs"])
            self.assertEqual(twin["ttkExp"], best["vs"][t]["ttkExp"])
        # no per-target row beats the overall winner on the mean: the winner
        # is the minimum of the mean over the whole (shared) universe
        for key in targets:
            for r in outs[key]["rows"]:
                kills = sum(r["vs"][t]["ttk"] is not None for t in names)
                times = [r["vs"][t]["killTime"] for t in names]
                self.assertLessEqual((-best["kills"], best["mean"]),
                                     (-kills, round(geo(times), 2) + 0.02))


if __name__ == "__main__":
    unittest.main()
