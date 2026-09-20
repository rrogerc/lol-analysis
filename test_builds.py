"""Hand-computed checks for the builds stat layer.

Run: python3 -m unittest test_builds -v

The math tests pin the League formulas to values computed by hand from the
wiki; the integration tests read the committed data/items snapshot, so they
also catch a meraki schema change sneaking past ITEM_STAT_MAP.
"""

import copy
import itertools
import json
import math
import os
import re
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

    def test_item_catalog_covers_the_pool(self):
        # Every item a ranked row can carry has an icon and tooltip entry
        # under the row's own name, and the model notes agree with the
        # stat sheet's uncovered-passive report.
        import items as items_mod
        meta = builds.api_builds_meta()
        by_name = {it["name"]: it for it in meta["items"]}
        version = next(m for m in items_mod.snapshots()
                       if m["patch"] == self.patch)["ddragonVersion"]
        effects = builds.load_item_effects()
        every = list(dict.fromkeys(builds.DEFAULT_POOL + builds.TANK_POOL + builds.BOOTS))
        for iid in every:
            it = by_name[self.pool[iid]["name"]]
            self.assertEqual(it["id"], iid)
            self.assertEqual(it["icon"],
                             f"https://ddragon.leagueoflegends.com/cdn/{version}"
                             f"/img/item/{iid}.png")
            self.assertEqual(it["gold"], self.pool[iid]["shop"]["prices"]["total"])
            self.assertTrue(it["stats"], f"{it['name']} has no stat lines")
            for fx in it["effects"]:
                self.assertIn(fx["kind"], ("passive", "active", "text"))
                self.assertTrue(fx["text"], f"{it['name']}: empty {fx['kind']} block")
            self.assertEqual(it["modeled"]["covers"],
                             effects.get(iid, {}).get("covers", []))
            sheet = builds.resolve_stats(fake_champ(), 18, [iid], self.pool)
            self.assertEqual([f"{p} ({it['name']})" for p in it["modeled"]["unmodeled"]],
                             sheet["uncovered"])
        self.assertEqual(len(meta["items"]), len(every))
        json.dumps(meta)  # the whole payload serialises

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


class TestItemCatalog(unittest.TestCase):
    """The dashboard's item tooltips: ddragon's markup parsed into styled
    text runs, hand-checked against the shapes the 16.17 snapshot uses."""

    def test_stats_and_a_named_passive(self):
        out = builds.parse_dd_description(
            "<mainText><stats><attention>130</attention> Ability Power<br>"
            "<attention>10%</attention> Move Speed</stats><br><br>"
            "<passive>Magical Opus</passive><br>Increases your total "
            "<scaleAP>Ability Power by 30%</scaleAP>.</mainText>")
        self.assertEqual(out["stats"], ["130 Ability Power", "10% Move Speed"])
        self.assertEqual(out["effects"], [{
            "kind": "passive", "name": "Magical Opus",
            "text": [["", "Increases your total "],
                     ["scaleAP", "Ability Power by 30%"], ["", "."]]}])

    def test_active_label_and_cooldown_placeholder_are_dropped(self):
        # Gunblade: "<active>ACTIVE</active> (0s)" labels the real name below
        out = builds.parse_dd_description(
            "<mainText><stats><attention>80</attention> Ability Power</stats>"
            "<br><br><br><br><active>ACTIVE</active> (0s)<br>"
            "<active>Lightning Bolt</active><br>Shocks the target.</mainText>")
        self.assertEqual([(e["kind"], e["name"]) for e in out["effects"]],
                         [("active", "Lightning Bolt")])
        self.assertEqual(out["effects"][0]["text"], [["", "Shocks the target."]])

    def test_heading_after_a_sentence_and_reference_mid_sentence(self):
        # Ravenous Hydra's active follows the passive on the same line;
        # Horizon Focus names Hypershot inside a sentence (a reference)
        out = builds.parse_dd_description(
            "<mainText><stats><attention>65</attention> Attack Damage</stats>"
            "<br><br><passive>Cleave</passive><br>Attacks deal "
            "<physicalDamage>physical damage</physicalDamage> nearby."
            "<active>Ravenous Crescent</active><br>Deal damage around you. <br>"
            "Your Life Steal applies.<br><br><passive>Focus</passive><br>When "
            "<passive>Hypershot</passive> is triggered, Reveal them.</mainText>")
        self.assertEqual([(e["kind"], e["name"]) for e in out["effects"]],
                         [("passive", "Cleave"), ("active", "Ravenous Crescent"),
                          ("passive", "Focus")])
        cleave, crescent, focus = out["effects"]
        self.assertEqual(cleave["text"], [["", "Attacks deal "],
                                          ["physicalDamage", "physical damage"],
                                          ["", " nearby."]])
        self.assertEqual(crescent["text"],
                         [["", "Deal damage around you.\nYour Life Steal applies."]])
        self.assertEqual(focus["text"], [["", "When "], ["passive", "Hypershot"],
                                         ["", " is triggered, Reveal them."]])

    def test_bullets_entities_and_unknown_tags(self):
        out = builds.parse_dd_description(
            "<mainText><stats><attention>30</attention> Attack Damage</stats><br><br>"
            "<passive>Juxtaposition</passive><br>Alternate Attacks: "
            "<li><keywordMajor>Light</keywordMajor> grants Armor. "
            "<li>Dark grants <font color='#DD2E2E'>Pen &amp; more</font>.</mainText>")
        self.assertEqual(out["effects"][0]["text"], [
            ["", "Alternate Attacks:\n• "], ["keywordMajor", "Light"],
            ["", " grants Armor.\n• Dark grants "], ["font", "Pen & more"], ["", "."]])
        self.assertEqual(builds.parse_dd_description(""), {"stats": [], "effects": []})
        # a stats-only item (boots) has no effect blocks at all
        out = builds.parse_dd_description(
            "<mainText><stats><attention>45</attention> Move Speed</stats><br><br></mainText>")
        self.assertEqual(out, {"stats": ["45 Move Speed"], "effects": []})


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

    def test_fiendhunter_opening_barrage(self):
        # The three attacks after R crit for at least 80% of the crit bonus
        # (an attack that rolls a crit crits normally) and the crit share
        # adds 15% of the attack's pre-mitigation damage as true damage —
        # expected values over crit chance, so Infinity Edge's crit damage
        # scales both. The old model was a fixed 1.6 floor and no true
        # damage, which gave a 100%-crit build nothing but the attack speed.
        ranks = builds.skill_ranks(16)
        for tokens in (["fiendhunter"], ["fiendhunter", "infinity edge"]):
            ids, sheet = self.resolve(16, tokens)
            fx = builds.merge_effects(ids, self.effects)
            r = builds.simulate(sheet, self.kit, fx, 16, ranks,
                                100_000, 0, 0, 3.0)
            self.assertGreaterEqual(r["attacks"], 4, tokens)  # past the window
            c = sheet["crit_chance"] / 100
            d = sheet["crit_damage"] / 100
            ev = 1 + c * (d - 1)
            window_ev = c * d + (1 - c) * (1 + 0.8 * (d - 1))
            self.assertGreater(window_ev, ev)
            ad = sheet["ad"]
            self.assertAlmostEqual(
                r["breakdown"]["auto"],
                ad * (3 * window_ev + (r["attacks"] - 3) * ev), places=6, msg=tokens)
            self.assertAlmostEqual(r["breakdown"]["barrage"],
                                   3 * c * 0.15 * ad * d, places=6, msg=tokens)
            # no ult, no window: plain crit EV on every attack and no rider
            no_r = builds.simulate(sheet, self.kit, fx, 16, ranks,
                                   100_000, 0, 0, 3.0, use_ult=False)
            self.assertNotIn("barrage", no_r["breakdown"])
            self.assertAlmostEqual(no_r["breakdown"]["auto"],
                                   ad * no_r["attacks"] * ev, places=6, msg=tokens)

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
        self.assertEqual(builds.kit_champions(), ["kassadin", "kayle", "twitch", "vladimir"])
        self.assertEqual(sorted(builds.KIT_DRIVERS), ["kassadin", "kayle", "twitch", "vladimir"])

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



def fake_twitch(riot=True):
    """A Twitch-shaped champion snapshot (patch 16.17 ddragon values), with
    meraki's stale 25.08 AD growth and Riot's own file alongside."""
    dd = {"name": "Twitch", "stats": {
        "hp": 630, "hpperlevel": 98, "mp": 300, "mpperlevel": 40,
        "armor": 27, "armorperlevel": 4,
        "spellblock": 33, "spellblockperlevel": 1.1,
        "attackdamage": 59, "attackdamageperlevel": 0,
        "attackspeed": 0.679, "attackspeedperlevel": 3,
        "movespeed": 330, "attackrange": 550,
    }}
    mk = {"stats": {"attackSpeedRatio": {"flat": 0.679},
                    "criticalStrikeDamage": {"flat": 175.0},
                    "attackDamage": {"flat": 59, "perLevel": 3.1}}}
    return {"slug": "twitch", "dd": dd, "mk": mk, "meta": {"patch": "16.17"},
            "riot": {"damagePerLevel": 3.0} if riot else None}


class TestTwitchKit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kit = builds.load_kit("twitch")

    def test_shape(self):
        for slot, ranks in [("Q", 5), ("W", 5), ("E", 5), ("R", 3)]:
            self.assertEqual(len(self.kit["abilities"][slot]["cooldownS"]), ranks)
        e = self.kit["abilities"]["E"]
        self.assertEqual(e["damage"]["base"], [20, 30, 40, 50, 60])
        self.assertEqual(e["perStack"]["physical"]["base"], [15, 20, 25, 30, 35])
        self.assertEqual(e["perStack"]["physical"]["bonusAdRatio"], 0.35)
        self.assertEqual(e["perStack"]["magic"]["apRatio"], 0.35)
        self.assertEqual(e["maxStacks"], 6)
        self.assertFalse(e["consumesStacks"])
        self.assertEqual(self.kit["abilities"]["R"]["bonusAd"], [30, 45, 60])
        self.assertEqual(self.kit["abilities"]["R"]["durationS"], 6)
        self.assertNotIn("damage", self.kit["abilities"]["R"])
        q = self.kit["abilities"]["Q"]["attackSpeed"]
        self.assertEqual((q["pct"], q["durationS"]), ([40, 45, 50, 55, 60], 6))
        w = self.kit["abilities"]["W"]
        self.assertEqual((w["stacksOnHit"], w["cloud"]["durationS"], w["cloud"]["tickS"]), (1, 3, 1))
        venom = self.kit["passive"]["deadlyVenom"]
        table = venom["perStackPerSecond"]["byLevel"]
        self.assertEqual(len(table), 18)
        # 1/2/3/4/5 at levels 1/5/9/13/17: Riot's breakpoints
        self.assertEqual([table[lv - 1] for lv in (1, 4, 5, 9, 13, 16, 17, 18)],
                         [1, 1, 2, 3, 4, 4, 5, 5])
        self.assertEqual((venom["maxStacks"], venom["durationS"], venom["tickS"]), (6, 6, 1))
        self.assertFalse(self.kit.get("manaless"))
        self.assertFalse(self.kit["attack"].get("never"))
        self.assertEqual(builds.kit_max_order(self.kit), ("E", "Q", "W"))
        self.assertEqual(builds.skill_ranks(16, builds.kit_max_order(self.kit)),
                         {"Q": 5, "W": 3, "E": 5, "R": 3})
        self.assertTrue(self.kit["notes"])


class TestTwitchEngine(unittest.TestCase):
    """Twitch's rotation, hand-computed. Level 16 naked: attack speed 0.679 x
    (1 + 43.4% growth + 60% Ambush) = 1.381, a 0.724 s period; AD 59 + 3 x
    growth(16) = 102.425; the venom deals 4 true damage per stack per
    second; Contaminate (rank 5) is 60 + 6 x (35 + 35% bonus AD) at six
    stacks, its magic half 6 x 35% AP."""

    @classmethod
    def setUpClass(cls):
        cls.kit = builds.load_kit("twitch")
        cls.patch, cls.pool = builds.load_items()
        cls.idx = builds.item_index(cls.pool)
        cls.effects = builds.load_item_effects()
        cls.order = builds.kit_max_order(cls.kit)
        cls.ad16 = 59 + 3 * builds.growth(16)

    def resolve(self, level, tokens, effects=None):
        ids = [builds.resolve_item(self.pool, self.idx, t) for t in tokens]
        return ids, builds.resolve_stats(fake_twitch(), level, ids, self.pool,
                                         effects or self.effects, kit=self.kit)

    def sim(self, level, tokens, hp=100_000, armor=0, mr=0, duration=3.0,
            use_ult=False, kit=None, effects=None, **kw):
        fx = effects or self.effects
        ids, sheet = self.resolve(level, tokens, fx)
        return builds.simulate(sheet, kit or self.kit,
                               builds.merge_effects(ids, fx), level,
                               builds.skill_ranks(level, self.order), hp, armor,
                               mr, duration, use_ult=use_ult, **kw)

    def test_ad_growth_prefers_riot_file_over_stale_meraki(self):
        # ddragon says 0 (its 16.5.1 regression), meraki's 25.08 entry 3.1,
        # Riot's 16.17 file 3: the file wins; meraki stands in only for a
        # snapshot without one; a real ddragon number beats both
        g = builds.growth(16)
        s = builds.resolve_stats(fake_twitch(), 16, [], {}, effects={}, kit=self.kit)
        self.assertAlmostEqual(s["ad"], 59 + 3 * g)
        s = builds.resolve_stats(fake_twitch(riot=False), 16, [], {}, effects={},
                                 kit=self.kit)
        self.assertAlmostEqual(s["ad"], 59 + 3.1 * g)
        champ = fake_twitch()
        champ["dd"]["stats"]["attackdamageperlevel"] = 2
        s = builds.resolve_stats(champ, 16, [], {}, effects={}, kit=self.kit)
        self.assertAlmostEqual(s["ad"], 59 + 2 * g)
        # the archived snapshot carries the file, which agrees with ddragon
        # on everything ddragon publishes
        champ = builds.load_champion("twitch")
        self.assertEqual(champ["riot"]["damagePerLevel"], 3.0)
        self.assertAlmostEqual(builds.champ_base(champ)["ad_per"], 3.0)
        dd = champ["dd"]["stats"]
        for dk, rk in (("hp", "baseHP"), ("hpperlevel", "hpPerLevel"),
                       ("armor", "baseArmor"), ("armorperlevel", "armorPerLevel"),
                       ("spellblock", "baseMR"), ("spellblockperlevel", "mrPerLevel"),
                       ("attackdamage", "baseDamage"), ("attackspeed", "attackSpeed"),
                       ("attackspeedperlevel", "attackSpeedPerLevel"),
                       ("attackrange", "attackRange"), ("movespeed", "baseMoveSpeed")):
            self.assertAlmostEqual(dd[dk], champ["riot"][rk], msg=dk)

    def test_level1_hand_computed(self):
        # Level 1 is one point in Contaminate, which never sees six stacks:
        # autos at 0 and 1/0.679 = 1.473 for 59 each, the one venom stack
        # ticking 1 true damage at 1.0. No Ambush rank, so no buff.
        r = self.sim(1, [], duration=1.5)
        self.assertEqual(r["attacks"], 2)
        self.assertAlmostEqual(r["breakdown"]["auto"], 118.0)
        self.assertAlmostEqual(r["breakdown"]["venom"], 1.0)
        self.assertAlmostEqual(r["total"], 119.0)
        self.assertNotIn("E", r["breakdown"])

    def test_rotation_hand_computed(self):
        # Level 16 naked, no ult, 0 resists. The auto at 0 breaks the
        # camouflage (stack 1); the cask at 0 (stack 2; cloud stacks at 1, 2,
        # 3 s) delays the next auto to 0.974 (stack 3); the cloud's 1.0 tick
        # (stack 4) lands before the venom's first: 4 x 4 = 16. An auto at
        # 1.698 (5), the cloud at 2.0 (6): Contaminate at 2.0 for 60 + 6 x 35
        # = 270 (no bonus AD), its lockout pushing the auto due at 2.422 to
        # 2.672; venom ticks of 24 at 2.0 and 3.0.
        r = self.sim(16, [], duration=1.0)
        self.assertEqual(r["attacks"], 2)
        self.assertAlmostEqual(r["breakdown"]["auto"], 2 * self.ad16)
        self.assertAlmostEqual(r["breakdown"]["venom"], 16.0)
        self.assertNotIn("W", r["breakdown"])  # the cask deals nothing itself
        r = self.sim(16, [], duration=3.0)
        self.assertEqual(r["attacks"], 4)
        self.assertAlmostEqual(r["breakdown"]["E"], 270.0)
        self.assertNotIn("E magic", r["breakdown"])  # no AP: no magic half
        self.assertAlmostEqual(r["breakdown"]["venom"], 64.0)
        self.assertAlmostEqual(r["breakdown"]["auto"], 4 * self.ad16)
        self.assertAlmostEqual(sum(r["breakdown"].values()), r["total"], places=6)
        # over 12 s: the buff ends at 6 (the auto scheduled at 5.568 still
        # lands at 6.292, then 1.027 s apart), Contaminate again at 10.0
        # (8 s cooldown, no haste) with the stacks still at six, the cask
        # never again (its 11 s cooldown is up with six stacks on the
        # target): 14 autos, 540 from E, venom 16 + 11 x 24
        r = self.sim(16, [], duration=12.0)
        self.assertEqual(r["attacks"], 14)
        self.assertAlmostEqual(r["breakdown"]["E"], 540.0)
        self.assertAlmostEqual(r["breakdown"]["venom"], 280.0)

    def test_ambush_attack_speed_runs_six_seconds(self):
        # eight autos in 5.6 s with the buff (0, 0.974, then every 0.724 s
        # around Contaminate's lockout); with the buff zeroed the period is
        # 1.027 s and five land
        self.assertEqual(self.sim(16, [], duration=5.6)["attacks"], 8)
        calm = copy.deepcopy(self.kit)
        calm["abilities"]["Q"]["attackSpeed"]["pct"] = [0, 0, 0, 0, 0]
        self.assertEqual(self.sim(16, [], duration=5.6, kit=calm)["attacks"], 5)

    def test_spray_and_pray_adds_bonus_ad_for_six_seconds(self):
        # R at 0 from camouflage (the standard 0.25 s lockout), the cask at 0
        # (another): the first auto at 0.5 deals base AD + 60. Contaminate at
        # 2.0 reads the bonus AD too: 60 + 6 x (35 + 0.35 x 60) = 396; its
        # lockout moves the auto due at 2.672 to 2.922.
        r = self.sim(16, [], duration=0.5, use_ult=True)
        self.assertEqual(r["attacks"], 1)
        self.assertAlmostEqual(r["breakdown"]["auto"], self.ad16 + 60)
        self.assertNotIn("R", r["breakdown"])  # no damage of its own
        r = self.sim(16, [], duration=3.0, use_ult=True)
        self.assertEqual(r["attacks"], 4)
        self.assertAlmostEqual(r["breakdown"]["E"], 396.0)
        self.assertAlmostEqual(r["breakdown"]["auto"], 4 * (self.ad16 + 60))
        # past 6 s the autos are back to the sheet's AD (the one at 6.542
        # already is) and the second Contaminate at 10.0 reads no bonus
        r = self.sim(16, [], duration=12.0, use_ult=True)
        self.assertEqual(r["attacks"], 14)
        self.assertAlmostEqual(r["breakdown"]["auto"], 8 * (self.ad16 + 60) + 6 * self.ad16)
        self.assertAlmostEqual(r["breakdown"]["E"], 396.0 + 270.0)

    def test_venom_is_true_damage(self):
        # 300 armor and MR leave the venom untouched while the autos and
        # Contaminate shrink; the stack timeline is the same, so it is the
        # same number
        a = self.sim(16, [], duration=3.0)
        b = self.sim(16, [], duration=3.0, armor=300, mr=300)
        self.assertAlmostEqual(a["breakdown"]["venom"], b["breakdown"]["venom"])
        self.assertLess(b["breakdown"]["auto"], a["breakdown"]["auto"])
        self.assertLess(b["breakdown"]["E"], a["breakdown"]["E"])

    def test_contaminate_magic_half_scales_with_ap(self):
        # Rabadon's: the physical half is unchanged, "E magic" is 6 x 35% AP,
        # and the venom is 4 + 3% AP per stack over the same 16 stack-ticks
        ids, sheet = self.resolve(16, ["rabadons"])
        r = self.sim(16, ["rabadons"], duration=3.0)
        self.assertAlmostEqual(r["breakdown"]["E"], 270.0)
        self.assertAlmostEqual(r["breakdown"]["E magic"], 6 * 0.35 * sheet["ap"])
        self.assertAlmostEqual(r["breakdown"]["venom"], 16 * (4 + 0.03 * sheet["ap"]))

    def test_phantom_hits_apply_stacks(self):
        # Guinsoo's phantom hits are on-hits, so each adds a stack: with the
        # cap lifted (a kit copy stacking without limit, Contaminate never
        # due) the venom counts every one, and Wrath's phantom switched off
        # leaves the attack speed stacks alone but loses those stacks
        loose = copy.deepcopy(self.kit)
        loose["passive"]["deadlyVenom"]["maxStacks"] = 1000
        loose["abilities"]["E"]["maxStacks"] = 1000
        r = self.sim(16, ["guinsoo"], duration=6.0, kit=loose)
        self.assertGreater(r["phantom_hits"], 0)
        self.assertNotIn("E", r["breakdown"])
        calm = copy.deepcopy(self.effects)
        del calm[3124]["phantom"]
        off = self.sim(16, ["guinsoo"], duration=6.0, kit=loose, effects=calm)
        self.assertEqual(off["phantom_hits"], 0)
        self.assertEqual(off["attacks"], r["attacks"])
        self.assertGreater(r["breakdown"]["venom"], off["breakdown"]["venom"])

    def test_ability_items_ride_contaminate_not_the_venom(self):
        # Liandry's burn and Luden's ride Contaminate (ability damage); the
        # venom, the cask and the ult are not ability damage, so with
        # Contaminate never due nothing of theirs fires
        r = self.sim(16, ["liandry", "ludens echo"], duration=3.0, use_ult=True)
        for src in ("burn", "ludens"):
            self.assertIn(src, r["breakdown"])
        quiet = copy.deepcopy(self.kit)
        quiet["abilities"]["E"]["maxStacks"] = 1000
        r = self.sim(16, ["liandry", "ludens echo"], duration=3.0, use_ult=True,
                     kit=quiet)
        for src in ("E", "burn", "ludens"):
            self.assertNotIn(src, r["breakdown"])
        self.assertIn("venom", r["breakdown"])

    def test_item_actives_read_the_ult_ad(self):
        # Profane Hydra's Cleave at 80% AD fires on engage, after Spray and
        # Pray: 0.8 x (AD + 60) with the ult, 0.8 x AD without (Kayle's and
        # Vladimir's actives keep the sheet's AD: the golden fixtures pin it)
        ids, sheet = self.resolve(16, ["profane hydra"])
        r = self.sim(16, ["profane hydra"], duration=0.1, use_ult=True)
        self.assertAlmostEqual(r["breakdown"]["active"], 0.8 * (sheet["ad"] + 60))
        r = self.sim(16, ["profane hydra"], duration=0.1, use_ult=False)
        self.assertAlmostEqual(r["breakdown"]["active"], 0.8 * sheet["ad"])

    def test_full_pool_and_dashboard_notes(self):
        self.assertEqual(builds.champion_pool(self.kit, self.effects), builds.DEFAULT_POOL)
        meta = builds.api_builds_meta()
        by_slug = {c["slug"]: c for c in meta["champions"]}
        self.assertEqual(by_slug["twitch"]["name"], "Twitch")
        self.assertEqual(by_slug["twitch"]["excluded"], [])
        self.assertEqual(by_slug["twitch"]["notes"], self.kit["notes"])
        self.assertTrue(any("Ambush" in n for n in by_slug["twitch"]["notes"]))

    def test_ranking_prefers_kill_time(self):
        cands = [3031, 6672, 3153, 3124, 3036, 3032, 3046, 6675]
        results, _ = enum_one(
            fake_twitch(), self.pool, self.effects, self.kit, 16,
            builds.skill_ranks(16, self.order), 2800, 110, 60, 8, candidates=cands)
        killers = [r for _, _, r in results if r["ttk"] is not None]
        self.assertTrue(killers)
        exp = [r["ttk_exp"] for r in killers]
        self.assertEqual(exp, sorted(exp))


def fake_kassadin():
    """A Kassadin-shaped champion snapshot (patch 16.18 ddragon values), with
    Riot's own file for the AD growth ddragon publishes as 0."""
    dd = {"name": "Kassadin", "stats": {
        "hp": 646, "hpperlevel": 113, "mp": 400, "mpperlevel": 87,
        "armor": 21, "armorperlevel": 4,
        "spellblock": 30, "spellblockperlevel": 1.3,
        "attackdamage": 59, "attackdamageperlevel": 0,
        "attackspeed": 0.64, "attackspeedperlevel": 3.7,
        "movespeed": 335, "attackrange": 150,
    }}
    mk = {"stats": {"attackSpeedRatio": {"flat": 0.64},
                    "criticalStrikeDamage": {"flat": 175.0}}}
    return {"slug": "kassadin", "dd": dd, "mk": mk, "meta": {"patch": "16.18"},
            "riot": {"damagePerLevel": 3.9}}


class TestKassadinKit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kit = builds.load_kit("kassadin")

    def test_shape(self):
        # Riot's 16.18 character bin (ranks 1-5; its rank-0 entries dropped)
        ab = self.kit["abilities"]
        for slot, ranks in [("Q", 5), ("W", 5), ("E", 5), ("R", 3)]:
            self.assertEqual(len(ab[slot]["cooldownS"]), ranks)
            self.assertEqual(len(ab[slot]["mana"]), ranks)
        self.assertEqual(ab["Q"]["cooldownS"], [9, 8.5, 8, 7.5, 7])
        self.assertEqual(ab["Q"]["damage"]["base"], [65, 95, 125, 155, 185])
        self.assertEqual(ab["Q"]["damage"]["apRatio"], 0.8)  # V26.18: 70% -> 80%
        self.assertEqual(ab["W"]["onhit"]["base"], [25] * 5)  # V26.11: 20 -> 25
        self.assertEqual(ab["W"]["onhit"]["apRatio"], 0.1)
        em = ab["W"]["empowered"]
        self.assertEqual(em["damage"]["base"], [50, 75, 100, 125, 150])
        self.assertEqual(em["damage"]["apRatio"], 0.8)
        self.assertEqual((em["windowS"], em["championMult"]), (5, 5))
        self.assertEqual(em["missingManaPct"], [4, 4.5, 5, 5.5, 6])
        self.assertEqual(ab["W"]["cooldownS"], [7] * 5)
        self.assertEqual(ab["E"]["cooldownS"], [21, 20, 19, 18, 17])
        self.assertEqual(ab["E"]["damage"]["base"], [70, 100, 130, 160, 190])
        self.assertEqual(ab["E"]["damage"]["apRatio"], 0.7)
        self.assertEqual(ab["E"]["cooldownReductionPerCastS"], 0.75)
        self.assertEqual(ab["R"]["cooldownS"], [5, 3.5, 2])
        self.assertEqual(ab["R"]["mana"], [40, 40, 40])
        rw = ab["R"]["riftwalk"]
        self.assertEqual(rw["base"]["base"], [80, 95, 110])  # V26.18: 70-110 -> 80-110
        self.assertEqual((rw["base"]["apRatio"], rw["base"]["maxManaRatio"]), (0.5, 2))
        self.assertEqual(rw["perStack"]["base"], [35, 45, 55])
        self.assertEqual((rw["perStack"]["apRatio"], rw["perStack"]["maxManaRatio"]),
                         (0.07, 1))
        self.assertEqual((rw["maxStacks"], rw["stackDurationS"], rw["manaCostMult"]),
                         (4, 15, 2))
        # the engine's single scheduled ult impact must stay out of it
        self.assertNotIn("damage", ab["R"])
        self.assertEqual(self.kit["attack"]["windupFraction"], 0.15)
        self.assertFalse(self.kit.get("manaless"))
        self.assertFalse(self.kit["attack"].get("never"))
        # Riot's recommendation and 16.18's most played order
        self.assertEqual(builds.kit_max_order(self.kit), ("E", "W", "Q"))
        self.assertEqual(builds.skill_ranks(16, builds.kit_max_order(self.kit)),
                         {"Q": 3, "W": 5, "E": 5, "R": 3})
        self.assertTrue(self.kit["notes"])

    def test_snapshot_agrees_with_riots_file(self):
        # the archived 16.18 snapshot: AD growth from Riot's file (ddragon
        # says 0), everything ddragon does publish matching the file
        champ = builds.load_champion("kassadin")
        self.assertEqual(champ["meta"]["patch"], "16.18")
        self.assertEqual(champ["riot"]["damagePerLevel"], 3.9)
        self.assertAlmostEqual(builds.champ_base(champ)["ad_per"], 3.9)
        dd = champ["dd"]["stats"]
        for dk, rk in (("hp", "baseHP"), ("hpperlevel", "hpPerLevel"),
                       ("armor", "baseArmor"), ("spellblock", "baseMR"),
                       ("spellblockperlevel", "mrPerLevel"),
                       ("attackdamage", "baseDamage"), ("attackspeed", "attackSpeed"),
                       ("attackspeedperlevel", "attackSpeedPerLevel"),
                       ("attackrange", "attackRange"), ("movespeed", "baseMoveSpeed")):
            self.assertAlmostEqual(dd[dk], champ["riot"][rk], msg=dk)
        # the windup is 30% + Riot's attack cast offset
        self.assertAlmostEqual(0.3 + champ["riot"]["attackDelayCastOffsetPercent"],
                               self.kit["attack"]["windupFraction"])


class TestKassadinEngine(unittest.TestCase):
    """Kassadin's rotation, hand-computed at level 16 naked against 0
    resists: AD 59 + 3.9 x growth(16) = 115.4525, mana 400 + 87 x growth(16)
    = 1659.325, attack speed 0.64 x 1.535575 = 0.98277 (a 1.0175 s period,
    a 0.1526 s windup). E > W > Q gives Null Sphere rank 3 (125, 8 s, 70
    mana), Force Pulse rank 5 (190, 17 s, 80 mana), Nether Blade rank 5 (150
    on the empowered attack, which refunds 30% of the missing mana, plus 25
    on every hit); Riftwalk rank 3 is 110 + 2% mana = 143.1865 plus 55 + 1%
    mana = 71.59325 a stack. The opening: R at 0 (its 0.25 s cast holds
    everything), Q at 0.25, the auto at 0.5 then W (the reset) and E at 0.5,
    whose cast puts the empowered attack at 0.75."""

    @classmethod
    def setUpClass(cls):
        cls.kit = builds.load_kit("kassadin")
        cls.patch, cls.pool = builds.load_items()
        cls.idx = builds.item_index(cls.pool)
        cls.effects = builds.load_item_effects()
        cls.order = builds.kit_max_order(cls.kit)
        g = builds.growth(16)
        cls.ad16 = 59 + 3.9 * g
        cls.mana16 = 400 + 87 * g
        cls.r0 = 110 + 2 / 100 * cls.mana16
        cls.rs = 55 + 1 / 100 * cls.mana16

    def resolve(self, level, tokens, effects=None):
        ids = [builds.resolve_item(self.pool, self.idx, t) for t in tokens]
        return ids, builds.resolve_stats(fake_kassadin(), level, ids, self.pool,
                                         effects or self.effects, kit=self.kit)

    def sim(self, level, tokens, hp=100_000, armor=0, mr=0, duration=3.0,
            use_ult=True, kit=None, effects=None, **kw):
        fx = effects or self.effects
        ids, sheet = self.resolve(level, tokens, fx)
        return builds.simulate(sheet, kit or self.kit,
                               builds.merge_effects(ids, fx), level,
                               builds.skill_ranks(level, self.order), hp, armor,
                               mr, duration, use_ult=use_ult, **kw)

    def riftwalks(self, n):
        """n Riftwalks in a row: the stacks standing before each cast, capped
        at four."""
        return sum(self.r0 + min(k, 4) * self.rs for k in range(n))

    def test_opening_hand_computed(self):
        r = self.sim(16, [], duration=0.5)
        self.assertEqual(r["attacks"], 1)
        bd = r["breakdown"]
        self.assertAlmostEqual(bd["R"], self.r0)
        self.assertAlmostEqual(bd["Q"], 125.0)
        self.assertAlmostEqual(bd["E"], 190.0)
        self.assertAlmostEqual(bd["auto"], self.ad16)
        self.assertAlmostEqual(bd["W onhit"], 25.0)
        self.assertNotIn("W", bd)  # armed at 0.5, not landed yet
        # the empowered attack at 0.75: the reset pulled it to 0.5 + windup,
        # Force Pulse's cast animation to 0.75; it carries both halves
        r = self.sim(16, [], duration=0.75)
        self.assertEqual(r["attacks"], 2)
        self.assertAlmostEqual(r["breakdown"]["W"], 150.0)
        self.assertAlmostEqual(r["breakdown"]["W onhit"], 50.0)
        self.assertAlmostEqual(sum(r["breakdown"].values()), r["total"], places=6)

    def test_riftwalk_until_the_mana_runs_out(self):
        # R every 2 s, each counting the stacks before it and costing 40 x
        # 2^stacks: casts at 0/2/4/6/8 cost 40+80+160+320+640. Between them
        # Null Sphere, Force Pulse and two Nether Blades, whose refunds (30%
        # of the missing mana) leave 533 after the fifth: the sixth (640)
        # waits, off cooldown since 10 s, for the third refund at 14.995 s
        r = self.sim(16, [], duration=8.0)
        self.assertAlmostEqual(r["breakdown"]["R"], self.riftwalks(5))
        self.assertAlmostEqual(r["breakdown"]["Q"], 125.0)  # the next at 8.25
        self.assertAlmostEqual(r["breakdown"]["W"], 2 * 150.0)
        self.assertEqual(r["attacks"], 9)
        self.assertAlmostEqual(r["breakdown"]["auto"], 9 * self.ad16)
        self.assertAlmostEqual(r["breakdown"]["W onhit"], 9 * 25.0)
        r = self.sim(16, [], duration=14.9)
        self.assertAlmostEqual(r["breakdown"]["R"], self.riftwalks(5))
        self.assertAlmostEqual(r["breakdown"]["Q"], 2 * 125.0)
        r = self.sim(16, [], duration=15.0)
        self.assertAlmostEqual(r["breakdown"]["W"], 3 * 150.0)
        self.assertAlmostEqual(r["breakdown"]["R"], self.riftwalks(6))
        # the squishy preset: 60 MR, all five by its 8 s
        r = self.sim(16, [], hp=2800, armor=110, mr=60, duration=8.0)
        self.assertAlmostEqual(r["breakdown"]["R"], self.riftwalks(5) * 100 / 160)

    def test_force_pulse_cooldown_shaved_by_casts(self):
        # cast at 0.5, back at 17.5 on its own; the R casts at 2, 4, 6 and 8,
        # Nether Blade at 7.75 and Null Sphere at 8.25 take 0.75 s each
        self.assertAlmostEqual(self.sim(16, [], duration=12.99)["breakdown"]["E"], 190.0)
        self.assertAlmostEqual(self.sim(16, [], duration=13.0)["breakdown"]["E"], 380.0)

    def test_nether_blade_refund_pays_for_riftwalk(self):
        # R made to cost 520 (1040 at a stack): after R, Q, W and E the pool
        # holds 988.3 — short of the second Riftwalk at 2.0 — until the
        # empowered attack at 0.75 refunds 30% of the 671 missing
        dear = copy.deepcopy(self.kit)
        dear["abilities"]["R"]["mana"] = [520, 520, 520]
        r = self.sim(16, [], duration=2.0, kit=dear)
        self.assertAlmostEqual(r["breakdown"]["R"], self.riftwalks(2))
        dry = copy.deepcopy(dear)
        dry["abilities"]["W"]["empowered"]["missingManaPct"] = [0] * 5
        r = self.sim(16, [], duration=2.0, kit=dry)
        self.assertAlmostEqual(r["breakdown"]["R"], self.riftwalks(1))

    def test_no_ult_no_riftwalk(self):
        # Null Sphere opens at 0 and is back at 8.0
        r = self.sim(16, [], duration=8.1, use_ult=False)
        self.assertNotIn("R", r["breakdown"])
        self.assertAlmostEqual(r["breakdown"]["Q"], 2 * 125.0)
        self.assertIn("W", r["breakdown"])
        # below level 6 there is no rank to cast either way; level 1 is one
        # point in Force Pulse (70), cast right after the auto at 0
        r = self.sim(1, [], duration=0.25)
        self.assertEqual(r["attacks"], 1)
        self.assertAlmostEqual(r["breakdown"]["auto"], 59.0)
        self.assertAlmostEqual(r["breakdown"]["E"], 70.0)
        self.assertNotIn("Q", r["breakdown"])
        self.assertNotIn("W onhit", r["breakdown"])  # no rank in W yet

    def test_ultimate_haste_speeds_riftwalk(self):
        # Hexplate's 30 ultimate haste: the sheet keeps it apart from ability
        # haste, and the second Riftwalk comes at 2 x 100/130 = 1.538 s
        ids, sheet = self.resolve(16, ["experimental hexplate"])
        self.assertAlmostEqual(sheet["cd_mult"], 1.0)
        self.assertAlmostEqual(sheet["ult_cd_mult"], 100 / 130)
        self.assertAlmostEqual(
            self.sim(16, ["experimental hexplate"], duration=1.6)["breakdown"]["R"],
            self.riftwalks(2))
        calm = copy.deepcopy(self.effects)
        del calm[3073]["ultimateAbilityHaste"]
        self.assertAlmostEqual(
            self.sim(16, ["experimental hexplate"], duration=1.6, effects=calm)
            ["breakdown"]["R"], self.riftwalks(1))
        # Malignance: 15 ability haste for everything, 20 more for the ult
        ids, sheet = self.resolve(16, ["malignance"])
        self.assertAlmostEqual(sheet["cd_mult"], 100 / 115)
        self.assertAlmostEqual(sheet["ult_cd_mult"], 100 / 135)

    def test_hatefog_refreshes_and_keeps_its_cadence(self):
        # Malignance: Riftwalks at 0, 1.481 and 2.963. The first opens the
        # zone after its own hit (60 MR), the later ones land inside it (50
        # MR) and only push its end back, so it ticks every 0.25 s from 0.25:
        # twelve ticks of (180 + 15% AP) / 12 = 16.125 by 3.0
        ids, sheet = self.resolve(16, ["malignance"])
        mana, ap = sheet["mana"], sheet["ap"]
        r0 = 110 + 0.5 * ap + 2 / 100 * mana
        rs = 55 + 0.07 * ap + 1 / 100 * mana
        r = self.sim(16, ["malignance"], mr=60, duration=3.0)
        self.assertAlmostEqual(r["breakdown"]["malignance"], 12 * 16.125 * 100 / 150)
        self.assertAlmostEqual(r["breakdown"]["R"],
                               r0 * 100 / 160 + (2 * r0 + 3 * rs) * 100 / 150)

    def test_actualizer_doubles_what_casts_cost(self):
        # 8 s of doubled costs empty the pool sooner: fewer Riftwalks than
        # the same build with the cost increase taken out
        free = copy.deepcopy(self.effects)
        free[2522]["manaActive"]["costIncreasePct"] = 0
        paid = self.sim(16, ["actualizer"], duration=15.0)
        unpaid = self.sim(16, ["actualizer"], duration=15.0, effects=free)
        self.assertLess(paid["breakdown"]["R"], unpaid["breakdown"]["R"])

    def test_recasts_do_not_reopen_the_on_ult_windows(self):
        # Opening Barrage has a 45 s cooldown: the opening Riftwalk empowers
        # the next three attacks (all in by 3 s) and the four more casts by
        # 8 s (every 1.54 s with the item's 30 ultimate haste) add nothing
        a = self.sim(16, ["fiendhunter bolts"], duration=3.0)
        b = self.sim(16, ["fiendhunter bolts"], duration=8.0)
        self.assertGreaterEqual(a["attacks"], 3)
        self.assertGreater(b["breakdown"]["R"], self.riftwalks(4))
        self.assertGreater(a["breakdown"]["barrage"], 0)
        self.assertEqual(a["breakdown"]["barrage"], b["breakdown"]["barrage"])

    def test_full_pool_and_dashboard_notes(self):
        self.assertEqual(builds.champion_pool(self.kit, self.effects), builds.DEFAULT_POOL)
        meta = builds.api_builds_meta()
        by_slug = {c["slug"]: c for c in meta["champions"]}
        self.assertEqual(by_slug["kassadin"]["name"], "Kassadin")
        self.assertEqual(by_slug["kassadin"]["excluded"], [])
        self.assertEqual(by_slug["kassadin"]["notes"], self.kit["notes"])
        self.assertTrue(any("Riftwalk" in n for n in by_slug["kassadin"]["notes"]))


class TestScenarioCache(unittest.TestCase):
    """The precomputed-cell layer: tiers, warm order, cache paths, read-only
    access, compute, and the warm lock — on a tiny item pool in a temp cache
    dir, so every cell is instant and the real .cache/builds/ is never
    touched."""
    # Rabadon, Void Staff, Shadowflame, Liandry, Riftmaker, Nashor, Muramana
    TINY_POOL = [3089, 3135, 4645, 6653, 4633, 3115, 3042]
    # Warmog's, Randuin's, Spirit Visage, Jak'Sho, Force of Nature, Guardian
    # Angel, Protoplasm Harness: the Survival tier's pool, as small
    TINY_TANK_POOL = [3083, 3143, 3065, 6665, 4401, 3026, 2525]
    # a budget tier that doesn't ship, to exercise the multi-tier paths:
    # warm order (cheap before full) and per-tier cache invalidation
    PROBE = {"probe-squishy": dict(label="Probe vs squishy", tier="probe",
                                   target="squishy", level=9, targetHp=1900,
                                   armor=50, mr=40, duration=8, budget=4500,
                                   targetBonusHp=400)}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        # the hand-encoded champions only: the machine-written roster has its
        # own enumeration test (test_kit_driver) and would make every warm
        # here a hundred and seventy champions long
        for name, value in (("SCENARIO_CACHE_DIR", self.tmp),
                            ("DEFAULT_POOL", self.TINY_POOL),
                            ("TANK_POOL", self.TINY_TANK_POOL),
                            ("SHOW_GENERATED", False)):
            patcher = mock.patch.object(builds, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_scenarios_form_tiers(self):
        # every scenario belongs to a tier; a tier's targets share the level
        # and budget its overall cell carries (one pass = one stat sheet per
        # build), and an overall cell only exists where there is something
        # to average — two or more targets — and is listed last
        # (a Survival tier's "targets" are its attackers)
        for key, sc in builds.SCENARIOS.items():
            self.assertIn("tier", sc, key)
            self.assertEqual("target" in sc or "attacker" in sc,
                             not sc.get("overall"), key)
        self.assertEqual(list(builds.SCENARIOS),
                         ["full-squishy", "full-bruiser", "full-tank",
                          "full-overall", "survive-kayle", "survive-kassadin",
                          "survive-overall"])
        # damage tiers first: a survival tier fights their winners
        self.assertEqual(builds.tiers(), ["full", "survive"])
        self.assertEqual([builds.tier_objective(t) for t in builds.tiers()],
                         ["damage", "survival"])
        self.assertEqual(builds.tier_champions("survive"), ["drmundo"])
        self.assertNotIn("drmundo", builds.tier_champions("full"))
        for k in builds.tier_targets("survive"):
            self.assertIn(builds.SCENARIOS[k]["attacker"],
                          builds.tier_champions("full"))
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
        per_tier = lambda: sum(len(builds.tier_champions(t)) * len(builds.tier_scenarios(t))
                               for t in builds.tiers())
        self.assertEqual(len(cs), per_tier())
        self.assertEqual(len(set(cs)), len(cs))
        self.assertEqual(cs[0], (champs[0], "full-squishy"))
        # the tanks' cells come after every damage cell
        kinds = [builds.tier_objective(builds.SCENARIOS[k]["tier"]) for _, k in cs]
        self.assertEqual(kinds, sorted(kinds))
        # with a budget tier in the mix: tier by tier, budget presets before
        # full builds (seconds, not half an hour), shorter total fights
        # first; every champion gets a tier's cells before anyone gets a
        # costlier tier's; and one champion's cells of a tier are adjacent,
        # since they come from one pass
        with mock.patch.dict(builds.SCENARIOS, self.PROBE):
            self.assertEqual(builds.tiers(), ["probe", "full", "survive"])
            cs = builds.cells()
            self.assertEqual(len(cs), per_tier())
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
                    [s for s, t in dict.fromkeys(groups) if t == tier],
                    builds.tier_champions(tier))
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
        # the cells key on the engine's core (and this module), not on the
        # machine-written drivers: a change to it reaches every cell ...
        with mock.patch.object(builds, "CORE_HASH", "0" * 64):
            other = builds.cell_paths()
        self.assertTrue(set(other.values()).isdisjoint(paths.values()))
        # ... and a change to one machine-written driver only its champion's
        if builds.GENERATED_HASHES:
            slug = sorted(s for s in builds.GENERATED_HASHES if not s.endswith("_blind"))[0]
            with mock.patch.object(builds, "SHOW_GENERATED", True):
                roster = builds.cell_paths()
                self.assertEqual({c: roster[c] for c in paths}, paths)  # the others' cells stay
                with mock.patch.dict(builds.GENERATED_HASHES, {slug: "0" * 64}):
                    other = builds.cell_paths()
            self.assertEqual({c[0] for c in roster if other[c] != roster[c]}, {slug})
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
            # ... and the Survival tier's too: its attackers' builds are the
            # full tier's overall winners
            for cell in both:
                same = builds.SCENARIOS[cell[1]]["tier"] not in ("full", "survive")
                self.assertEqual(changed[cell] == both[cell], same, cell)
            # an attacker's own scenario reaches only the Survival tier
            with mock.patch.dict(builds.SCENARIOS["survive-kayle"],
                                 {"duration": 31}):
                changed = builds.cell_paths()
            for cell in both:
                same = builds.SCENARIOS[cell[1]]["tier"] != "survive"
                self.assertEqual(changed[cell] == both[cell], same, cell)

    def test_read_only_then_compute(self):
        cell = ("kayle", "full-squishy")
        paths = builds.cell_paths()
        with mock.patch.object(builds, "enumerate_builds",
                               side_effect=AssertionError("simulated on read")):
            self.assertIsNone(builds.cached_scenario(*cell))
        self.assertRaises(ValueError, builds.cached_scenario, "kayle", "nope")
        self.assertRaises(ValueError, builds.cached_scenario, "notachampion", "full-squishy")
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

    def test_top_rows_carry_buy_orders(self):
        outs = builds.compute_tier("kayle", "full", builds.cell_paths())
        champ = builds.load_champion("kayle")
        _, pool = builds.load_items()
        effects = builds.load_item_effects()
        kit = builds.load_kit("kayle")
        targets = {k: builds.SCENARIOS[k] for k in builds.tier_targets("full")}
        ids_of = {pool[i]["name"]: i for i in [*self.TINY_POOL, *builds.BOOTS]}
        cost = lambda i: pool[i]["shop"]["prices"]["total"]
        for key, d in outs.items():
            rows = d["rows"]
            self.assertGreater(len(rows), builds.BUY_ORDER_ROWS)
            for r in rows[builds.BUY_ORDER_ROWS:]:
                self.assertNotIn("buyOrder", r)
            for r in rows[:builds.BUY_ORDER_ROWS]:
                # the items after the boots, reordered; the boots stay first
                # and `items` keeps the enumeration's order (seeds read it)
                self.assertCountEqual(r["buyOrder"], r["items"][1:])
                self.assertNotEqual(r["buyOrder"][0], "Muramana")  # Tear first
                # no allowed order does better along the way: each stage's
                # kill time (the cell's own target, or the geometric mean
                # over all three for overall) times the next item's gold
                ids = [ids_of[n] for n in r["items"]]
                stages = builds.stage_times(champ, kit, pool, effects, ids, targets)
                self.assertEqual(len(stages), 2 ** 5 - 1)  # every partial build
                for ts in stages.values():  # each stage fights its own enemies
                    self.assertLess(ts["full-squishy"], ts["full-tank"])
                if d["scenario"].get("overall"):
                    own = lambda ts: math.prod(ts.values()) ** (1 / len(ts))
                else:
                    own = lambda ts: ts[key]

                def score(order):
                    return sum(own(stages[frozenset(order[:k])]) * cost(i)
                               for k, i in enumerate(order))
                allowed = [o for o in itertools.permutations(ids[1:])
                           if o[0] not in builds.BUY_NOT_FIRST]
                chosen = score([ids_of[n] for n in r["buyOrder"]])
                self.assertLessEqual(chosen, min(map(score, allowed)) * (1 + 1e-9))

    def test_survival_cells(self):
        paths = builds.cell_paths()
        # a survival cell fights its attackers' damage winners: not before
        with self.assertRaises(RuntimeError):
            builds.compute_tier("drmundo", "survive", paths)
        full = {slug: builds.compute_tier(slug, "full", paths)
                for slug in ("kayle", "kassadin")}
        outs = builds.compute_tier("drmundo", "survive", paths)
        self.assertEqual(set(outs), {"survive-kayle", "survive-kassadin",
                                     "survive-overall"})
        for key, d in outs.items():
            self.assertEqual(builds.cached_scenario("drmundo", key), d)
            self.assertEqual(d["objective"], "survival")
            self.assertEqual(d["scenario"]["objective"], "survival")
            self.assertEqual(d["championName"], "Dr. Mundo")
            # every boots x five of the seven tank items; Mundo has no mana
            # item to lose
            self.assertEqual(d["buildsEvaluated"], len(builds.BOOTS) * math.comb(7, 5))
            # the attackers are the damage cells' overall winners
            for m in d["scenario"]["attackers"]:
                top = full[m["champion"]]["full-overall"]["rows"][0]
                self.assertEqual(m["items"], top["items"])
            rows = d["rows"]
            self.assertEqual(len(rows), len(builds.BOOTS) * math.comb(7, 5))
            self.assertEqual([r["rank"] for r in rows], list(range(1, len(rows) + 1)))
            for r in rows:
                self.assertEqual(list(r["vs"]), ["kayle", "kassadin"])
                for v in r["vs"].values():
                    self.assertLessEqual(v["share"], 1.0)
                    self.assertGreater(v["ttd"], 0)
                    self.assertIn("rAt", v["defense"])
            if d["scenario"].get("overall"):
                order = [(-r["survived"], -r["mean"]) for r in rows]
                self.assertEqual(order, sorted(order))
                geo = lambda xs: math.prod(xs) ** (1 / len(xs))
                for r in rows:
                    self.assertAlmostEqual(
                        r["mean"], geo([v["ttd"] for v in r["vs"].values()]), delta=0.02)
            else:
                me = builds.SCENARIOS[key]["attacker"]
                for r in rows:
                    self.assertEqual(r["ttd"], r["vs"][me]["ttd"])
                # the longest-lived first, and it is the yardstick
                self.assertEqual(rows[0]["vs"][me]["share"], 1.0)
                alive = [r["ttd"] for r in rows if not r["died"]]
                dead = [r["ttd"] for r in rows if r["died"]]
                self.assertEqual(alive + dead, [r["ttd"] for r in rows])
                self.assertEqual(dead, sorted(dead, reverse=True))
        json.dumps(builds.api_builds_meta())

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
        self.assertEqual(len(heads), sum(len(builds.tier_champions(t))
                                         for t in builds.tiers()))
        self.assertTrue(any("full-overall" in h for h in heads))
        self.assertTrue(heads[-1].endswith("drmundo/survive (survive-kayle, "
                                           "survive-kassadin, survive-overall) …"))
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


class TestBuyOrder(unittest.TestCase):
    """The suggested buy order on the top rows of a cell: builds.buy_order
    picks among orders, builds.stage_target sizes each stage's enemy."""

    def test_each_stage_weighs_by_the_gold_to_the_next_item(self):
        # b alone kills faster, but a is cheap: 10 s for 1,000 gold, then
        # 6 s for 3,000 (28,000) beats 10 s for 3,000, then 4 s for 1,000
        # (34,000)
        times = {frozenset(): 10.0, frozenset({"a"}): 6.0, frozenset({"b"}): 4.0}
        self.assertEqual(builds.buy_order(["a", "b"], times, {"a": 1000, "b": 3000}),
                         ("a", "b"))
        # at one price the stronger item goes first: 42,000 against 48,000
        self.assertEqual(builds.buy_order(["a", "b"], times, {"a": 3000, "b": 3000}),
                         ("b", "a"))

    def test_rules_ties_and_stages_without_damage(self):
        times = {frozenset(s): 5.0 for n in range(3)
                 for s in itertools.combinations("abc", n)}
        cost = dict.fromkeys("abc", 1000)
        self.assertEqual(builds.buy_order(list("abc"), times, cost), ("a", "b", "c"),
                         "ties keep pool order")
        self.assertEqual(builds.buy_order(list("abc"), times, cost, first={"c"}),
                         ("c", "a", "b"))
        self.assertEqual(builds.buy_order(list("abc"), times, cost, not_first={"a"}),
                         ("b", "a", "c"))
        self.assertEqual(builds.buy_order(["a"], {frozenset(): 5.0}, cost,
                                          not_first={"a"}), ("a",),
                         "a lone not-first item is still bought")
        times[frozenset({"a"})] = None  # owning just a deals nothing
        self.assertEqual(builds.buy_order(list("abc"), times, cost)[0], "b")

    def test_stage_targets_grow_into_the_full_dummies(self):
        tank = builds.SCENARIOS["full-tank"]
        full = builds.stage_target(tank, tank["level"])
        for k in ("targetHp", "armor", "mr", "targetBonusHp", "duration"):
            self.assertAlmostEqual(full[k], tank[k], msg=k)
        early = builds.stage_target(tank, 9)
        self.assertAlmostEqual(early["armor"], 220 * 50 / 110)  # first-item share
        self.assertEqual(builds.stage_target(tank, 7), early, "no lower before 9")
        # the health it lacks is item health first: 1,543 lacking, 1,500 of it items
        self.assertEqual(early["targetBonusHp"], 0.0)
        squishy = builds.stage_target(builds.SCENARIOS["full-squishy"], 11)
        self.assertEqual([round(squishy[k]) for k in ("targetHp", "armor", "mr",
                                                      "targetBonusHp")],
                         [2157, 67, 46, 157])
        # a tier at or below the early level has nothing to grow into
        self.assertAlmostEqual(builds.stage_target(dict(tank, level=9), 7)["armor"], 220)


class TestBootsClasses(unittest.TestCase):
    """Every tier-2 boots is enumerated; boots that differ only in stats the
    engine never reads share one simulation."""

    def setUp(self):
        _, self.pool = builds.load_items()
        self.effects = builds.load_item_effects()
        self.kit = builds.load_kit("kayle")

    def test_shipped_boots_are_every_tier_two_pair_gold_can_buy(self):
        self.assertEqual(
            [self.pool[b]["name"] for b in builds.BOOTS],
            ["Berserker's Greaves", "Sorcerer's Shoes",
             "Ionian Boots of Lucidity", "Boots of Swiftness",
             "Mercury's Treads", "Plated Steelcaps", "Gluttonous Greaves"])
        gated = builds.load_gated_items()
        for b in builds.BOOTS:
            self.assertTrue(self.pool[b]["shop"]["purchasable"], b)
            self.assertNotIn(b, gated)

    def test_classes_merge_only_engine_ignored_stats(self):
        # Mercury's (MR, tenacity), Steelcaps (armor) and Gluttonous
        # (omnivamp) are one class: 45 move speed and nothing the engine
        # reads. Swiftness stays apart — Energized items charge with move
        # speed — as do the three with an offensive stat.
        classes = builds.boots_classes(self.pool, self.effects)
        self.assertEqual(
            [[self.pool[b]["name"] for b in c] for c in classes],
            [["Berserker's Greaves"], ["Sorcerer's Shoes"],
             ["Ionian Boots of Lucidity"], ["Boots of Swiftness"],
             ["Mercury's Treads", "Plated Steelcaps", "Gluttonous Greaves"]])
        # any modeled effect on a boots would split it off
        effects = {**self.effects, 3047: {**self.effects.get(3047, {}),
                                          "apMult": 0.1}}
        self.assertEqual(len(builds.boots_classes(self.pool, effects)), 6)

    def test_engine_never_reads_an_ignored_stat(self):
        # the merge is only sound while the engine (and every kit driver)
        # leaves these sheet fields alone; move speed it does read. The
        # Survival tier's defender (defense.rs, survive.rs) reads the TANK's
        # sheet — armor, resists, regeneration — which no damage fight has
        read = set()
        for name in ("fight.rs", "drivers.rs", "num.rs"):
            with open(os.path.join(builds.ENGINE_DIR, "src", name)) as f:
                read |= set(re.findall(r"sheet\.(\w+)", f.read()))
        with open(builds.__file__) as f:
            read |= set(re.findall(r'sheet\["(\w+)"\]', f.read()))
        self.assertFalse(read & builds.ENGINE_IGNORES, read & builds.ENGINE_IGNORES)
        self.assertIn("move_speed", read)

    def test_class_members_fight_identically(self):
        # with an Energized item in the build, so move speed matters: the
        # three 45-speed defensive boots fight to the same decimal; their
        # sheets differ only in the ignored stats, gold and names
        champ = fake_champ()
        idx = builds.item_index(self.pool)
        items = [builds.resolve_item(self.pool, idx, n)
                 for n in ("Stormrazor", "Infinity Edge", "Kraken Slayer")]
        ranks = builds.skill_ranks(16)

        def fight(boots):
            ids = [boots, *items]
            sheet = builds.resolve_stats(champ, 16, ids, self.pool,
                                         self.effects, kit=self.kit)
            fx = builds.merge_effects(ids, self.effects)
            return sheet, builds.simulate(sheet, self.kit, fx, 16, ranks,
                                          4800, 220, 160, 15)
        (sa, a), (sb, b), (sc, c) = fight(3111), fight(3047), fight(3008)
        for x in (b, c):
            self.assertEqual((a["total"], a["ttk"], a["ttk_exp"], a["breakdown"]),
                             (x["total"], x["ttk"], x["ttk_exp"], x["breakdown"]))
        skip = builds.ENGINE_IGNORES | {"gold", "items", "uncovered"}
        for x in (sb, sc):
            self.assertEqual({k: v for k, v in sa.items() if k not in skip},
                             {k: v for k, v in x.items() if k not in skip})
        self.assertNotEqual(sa["mr"], sb["mr"])
        self.assertNotEqual(sa["armor"], sb["armor"])

    def test_enumerator_ranks_every_member_with_the_shared_fight(self):
        tiny = TestScenarioCache.TINY_POOL
        champ = builds.load_champion("kayle")
        target = dict(targetHp=2800, armor=110, mr=60, duration=8)
        lists, count = builds.enumerate_builds(
            champ, self.pool, self.effects, self.kit, 16,
            builds.skill_ranks(16), {"t": target}, candidates=tiny,
            keep=10_000)
        rows = lists["t"]
        self.assertEqual(count, len(builds.BOOTS) * math.comb(len(tiny), 5))
        self.assertEqual(len(rows), count)
        cls = {b: i for i, ms in enumerate(builds.boots_classes(self.pool, self.effects))
               for b in ms}
        by_rest = {}
        for ids, sheet, rs in rows:
            by_rest.setdefault(tuple(ids[1:]), {})[ids[0]] = (
                sheet["gold"], rs["t"]["total"], rs["t"]["ttk_exp"],
                rs["t"]["breakdown"])
        for rest, per_boots in by_rest.items():
            self.assertEqual(set(per_boots), set(builds.BOOTS), rest)
            for a in per_boots:
                for b in per_boots:
                    if cls[a] == cls[b]:
                        self.assertEqual(per_boots[a][1:], per_boots[b][1:])
            # each member keeps its own sheet: Mercury's costs 50 more
            self.assertEqual(per_boots[3111][0] - per_boots[3047][0], 50)


class TestEnumeratorPruning(unittest.TestCase):
    """The exact pruning in enumerate_builds: bounds from the lists as they
    fill, fights stopped once they can't matter, per-build boots classes,
    seeds, the checked guess behind the overall bound — every one of them
    has to leave the result exactly what an unpruned pass ranks. On a
    9-item pool (126 combinations), so it runs in a second."""

    @classmethod
    def setUpClass(cls):
        cls.champ = builds.load_champion("kayle")
        _, cls.pool = builds.load_items()
        cls.effects = builds.load_item_effects()
        cls.kit = builds.load_kit("kayle")
        cls.ranks = builds.skill_ranks(16, builds.kit_max_order(cls.kit))
        full = builds.champion_pool(cls.kit, cls.effects)
        charged = [i for i in full if "energized" in cls.effects.get(i, {})]
        cls.energized = charged[0]
        cls.tiny = [i for i in full if i not in charged][:8] + [cls.energized]
        cls.keys = builds.tier_scenarios("full")
        cls.targets = {k: builds.SCENARIOS[k] for k in builds.tier_targets("full")}
        cls.overall = "full-overall"

    def enum(self, **kw):
        kw.setdefault("candidates", self.tiny)
        kw.setdefault("overall", self.overall)
        kw.setdefault("keep", 20)
        return builds.enumerate_builds(self.champ, self.pool, self.effects,
                                       self.kit, 16, self.ranks, self.targets,
                                       **kw)

    @classmethod
    def reference(cls):
        """The unpruned ranking: keep more than there are builds, so no
        bound ever forms, then cut to 20."""
        if not hasattr(cls, "_ref"):
            lists, count = builds.enumerate_builds(
                cls.champ, cls.pool, cls.effects, cls.kit, 16, cls.ranks,
                cls.targets, candidates=cls.tiny, overall=cls.overall,
                keep=10_000)
            assert count == 7 * math.comb(len(cls.tiny), 5)
            assert all(len(lst) == count for lst in lists.values())
            cls._ref = ({k: lst[:20] for k, lst in lists.items()}, count)
        return cls._ref

    def assertSameRanking(self, lists, count):
        ref, ref_count = self.reference()
        self.assertEqual(count, ref_count)
        for k in self.keys:
            self.assertEqual([ids for ids, _, _ in lists[k]],
                             [ids for ids, _, _ in ref[k]], k)
            for (_, sheet, rs), (_, ref_sheet, ref_rs) in zip(lists[k], ref[k]):
                self.assertEqual(sheet, ref_sheet)
                self.assertEqual(rs, ref_rs)

    def test_pruned_pass_ranks_like_an_unpruned_one(self):
        lists, count = self.enum()
        self.assertSameRanking(lists, count)
        # and the rows carry every fight in full, breakdown included
        for k in self.keys:
            for _, _, rs in lists[k]:
                for t in self.targets:
                    self.assertTrue(rs[t]["breakdown"], (k, t))

    def test_forked_pass_ranks_like_a_sequential_one(self):
        with mock.patch.object(builds, "FORK_ABOVE", 0):
            lists, count = self.enum(workers=3)
        self.assertSameRanking(lists, count)

    def test_a_wrong_guess_is_caught_and_the_pass_redone(self):
        # a bound above the true fastest kill would cut builds that belong on
        # the overall list; the end-of-pass check must notice and rerun
        log = []
        with mock.patch.object(builds, "MIN_KILL_GUESS", 2.0):
            lists, count = self.enum(log=log.append)
        self.assertTrue(any("faster than assumed" in line for line in log), log)
        self.assertSameRanking(lists, count)

    def test_seeds_only_change_the_order_of_work(self):
        ref, _ = self.reference()
        seeds = [ids for k in self.keys for ids, _, _ in ref[k][:3]]
        seeds += [[3006, 9999999, *self.tiny[:4]],  # not in the pool
                  [self.tiny[0], *self.tiny[1:6]],  # no boots
                  [3006, *self.tiny[:4]],           # too short
                  [3006, self.tiny[0], self.tiny[0], *self.tiny[1:4]]]  # a dupe
        lists, count = self.enum(seeds=seeds)
        self.assertSameRanking(lists, count)

    def test_progress_is_logged(self):
        log = []
        self.enum(log=log.append)
        self.assertTrue(log and "100.0%" in log[-1], log)
        self.assertIn(f"{7 * math.comb(len(self.tiny), 5):,} builds", log[-1])

    def test_stop_after_cuts_a_fight_that_cannot_matter(self):
        ids = [3006, *self.tiny[:5]]
        sheet = builds.resolve_stats(self.champ, 16, ids, self.pool,
                                     self.effects, kit=self.kit)
        fx = builds.merge_effects(ids, self.effects)
        sc = self.targets["full-tank"]
        args = (sheet, self.kit, fx, 16, self.ranks, sc["targetHp"],
                sc["armor"], sc["mr"], sc["duration"])
        full = builds.simulate(*args, target_bonus_hp=sc["targetBonusHp"])
        self.assertIsNotNone(full["ttk"])
        # the clock passes the cut before the kill: nothing to report
        self.assertIsNone(builds.simulate(*args, target_bonus_hp=sc["targetBonusHp"],
                                          stop_after=full["ttk"] / 2))
        # the kill comes first: the fight is the full one
        self.assertEqual(builds.simulate(*args, target_bonus_hp=sc["targetBonusHp"],
                                         stop_after=full["ttk"]), full)
        # no breakdown wanted: the numbers don't change
        lean = builds.simulate(*args, target_bonus_hp=sc["targetBonusHp"],
                               breakdown=False)
        self.assertEqual(lean["breakdown"], {})
        self.assertEqual({k: v for k, v in lean.items() if k != "breakdown"},
                         {k: v for k, v in full.items() if k != "breakdown"})

    def test_cached_builds_seed_from_any_cell_file(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        names = lambda ids: [self.pool[i]["name"] for i in ids]
        a, b = [3006, *self.tiny[:5]], [3020, *self.tiny[1:6]]
        with mock.patch.object(builds, "SCENARIO_CACHE_DIR", tmp):
            with open(os.path.join(tmp, "kayle-full-tank-0000000000000000.json"), "w") as f:
                json.dump({"rows": [{"items": names(a)}, {"items": names(b)},
                                    {"items": names(a)},
                                    {"items": ["No Such Item", *names(a)[1:]]}]}, f)
            with open(os.path.join(tmp, "kayle-full-squishy-1111111111111111.json"), "w") as f:
                f.write("not json")
            with open(os.path.join(tmp, "vladimir-full-tank-0000000000000000.json"), "w") as f:
                json.dump({"rows": [{"items": names(b)}]}, f)
            self.assertEqual(builds.cached_builds("kayle", self.pool), [a, b])
            self.assertEqual(builds.cached_builds("vladimir", self.pool), [b])
            self.assertEqual(builds.cached_builds("teemo", self.pool), [])


class TestGolden(unittest.TestCase):
    """The engine's output pinned bit for bit. data/builds/golden holds every
    fight of a few hundred builds (each item at least twice, every effect
    key, five targets, the flag variants) and three enumeration passes, as
    the pre-Rust Python engine computed them at commit d2922e6. A refactor
    has to reproduce them exactly; a deliberate model change regenerates
    them in the same commit (see the README there)."""

    GOLDEN = os.path.join(builds.BUILDS_DATA_DIR, "golden")

    @classmethod
    def setUpClass(cls):
        cls.patch, cls.pool = builds.load_items()
        cls.effects = builds.load_item_effects()
        cls.champs = {}
        cls.sheets = {}

    @staticmethod
    def first_diff(exp, got, path=""):
        """The path of the first difference, or None. Exact float equality;
        an int and a float of the same value are the same number (the old
        engine let a kit's integer delay leak through the clock as an int);
        tuples count as lists; dict order doesn't count."""
        if isinstance(exp, bool) or isinstance(got, bool):
            return None if exp is got else f"{path}: {exp!r} != {got!r}"
        if isinstance(exp, (int, float)) and isinstance(got, (int, float)):
            return None if exp == got else f"{path}: {exp!r} != {got!r}"
        if isinstance(exp, list) and isinstance(got, (list, tuple)):
            if len(exp) != len(got):
                return f"{path}: {len(exp)} items != {len(got)}"
            for n, (a, b) in enumerate(zip(exp, got)):
                d = TestGolden.first_diff(a, b, f"{path}[{n}]")
                if d:
                    return d
            return None
        if isinstance(exp, dict) and isinstance(got, dict):
            if set(exp) != set(got):
                return f"{path}: keys {sorted(set(exp) ^ set(got))}"
            for k in exp:
                d = TestGolden.first_diff(exp[k], got[k], f"{path}.{k}")
                if d:
                    return d
            return None
        return None if exp == got else f"{path}: {exp!r} != {got!r}"

    def champ(self, slug):
        if slug not in self.champs:
            kit = builds.load_kit(slug)
            self.champs[slug] = (kit, builds.load_champion(slug),
                                 builds.kit_max_order(kit))
        return self.champs[slug]

    def sheet(self, slug, level, ids):
        key = (slug, level, tuple(ids))
        if key not in self.sheets:
            kit, champ, _ = self.champ(slug)
            self.sheets[key] = builds.resolve_stats(champ, level, list(ids),
                                                    self.pool, self.effects,
                                                    kit=kit)
        return self.sheets[key]

    def test_fights(self):
        with open(os.path.join(self.GOLDEN, "engine-fights.json")) as f:
            doc = json.load(f)
        self.assertEqual(doc["patch"], self.patch)
        for case in doc["cases"]:
            slug, level, ids = case["champion"], case["level"], case["ids"]
            kit, _, order = self.champ(slug)
            sheet = self.sheet(slug, level, ids)
            fx = builds.merge_effects(list(ids), self.effects)
            ranks = builds.skill_ranks(level, order)
            slim = {k: v for k, v in sheet.items() if k != "uncovered"}
            for what, exp, got in (("sheet", case["sheet"], slim),
                                   ("fx", case["fx"], fx),
                                   ("ranks", case["ranks"], ranks)):
                self.assertIsNone(self.first_diff(exp, got, what),
                                  (case["id"], case["items"]))
            stop = (math.inf if case["stop_after"] is None
                    else case["stop_after"])
            got = builds.simulate(
                sheet, kit, fx, level, ranks, case["targetHp"], case["armor"],
                case["mr"], case["duration"], use_ult=case["use_ult"],
                prestacked=case["prestacked"],
                target_bonus_hp=case["targetBonusHp"], stop_after=stop,
                breakdown=case["breakdown"], _blend=case["blend"])
            self.assertIsNone(self.first_diff(case["result"], got, "result"),
                              (case["id"], case["items"], case["target"]))

    def test_enumerate(self):
        with open(os.path.join(self.GOLDEN, "enumerate.json")) as f:
            doc = json.load(f)
        for run in doc["runs"]:
            kit, champ, order = self.champ(run["champion"])
            lists, count = builds.enumerate_builds(
                champ, self.pool, self.effects, kit, run["level"],
                builds.skill_ranks(run["level"], order), doc["targets"],
                candidates=list(run["pool"]), overall=run["overall"],
                keep=run["keep"], workers=run["workers"])
            self.assertEqual(count, run["count"], run["name"])
            self.assertEqual(set(lists), set(run["lists"]), run["name"])
            for key, rows in run["lists"].items():
                got = lists[key]
                self.assertEqual([r["ids"] for r in rows],
                                 [list(ids) for ids, _, _ in got],
                                 (run["name"], key))
                for n, (r, (_, sheet, fights)) in enumerate(zip(rows, got)):
                    slim = {k: sheet[k] for k in run["sheet_keys"]}
                    self.assertIsNone(
                        self.first_diff(r["sheet"], slim, "sheet"),
                        (run["name"], key, n))
                    self.assertIsNone(
                        self.first_diff(r["fights"], fights, "fights"),
                        (run["name"], key, n, r["ids"]))


class TestSurvivalEngine(unittest.TestCase):
    """The Survival tier's defender, mechanic by mechanic, against a clean
    attacker — level-1 Kassadin with every rank at 0: nothing but 59-damage
    physical autos every 1.5625 s from t=0 — and a bare test tank (no kit
    mechanics, 590 health, no resists or regeneration unless given), so
    each number here is worked out by hand."""

    @classmethod
    def setUpClass(cls):
        _, cls.pool = builds.load_items()
        cls.effects = builds.load_item_effects()
        cls.idx = builds.item_index(cls.pool)

    def item(self, name):
        return builds.resolve_item(self.pool, self.idx, name)

    def attacker(self, names=(), level=1, duration=30.0, slug="kassadin", ranks=None):
        kit, champ = builds.load_kit(slug), builds.load_champion(slug)
        ids = [self.item(n) for n in names]
        sheet = builds.resolve_stats(champ, level, ids, self.pool, self.effects, kit=kit)
        return dict(sheet=sheet, kit=kit, fx=builds.merge_effects(ids, self.effects),
                    level=level, duration=duration,
                    ranks=ranks or {"Q": 0, "W": 0, "E": 0, "R": 0})

    @staticmethod
    def base(hp=590.0, armor=0.0, mr=0.0, regen=0.0):
        return dict(hp=hp, hp_per=0, mp=0, mp_per=0, armor=armor, armor_per=0, mr=mr,
                    mr_per=0, ad=50, ad_per=0, base_as=0.625, as_per=0, as_ratio=0.625,
                    crit_damage_base=175, move_speed=340, attack_range=125,
                    hp_regen=regen, hp_regen_per=0)

    TANK = {"champion": "testtank", "name": "Test Tank", "role": "tank", "abilities": {}}

    def fight(self, att, names=(), base=None, kit=None, level=1, ranks=None,
              thresholds=()):
        ids = [self.item(n) for n in names]
        defender = dict(base=base or self.base(), level=level, kit=kit or self.TANK,
                        ranks=ranks or {"Q": 0, "W": 0, "E": 0, "R": 0})
        return builds.lol_engine.survive(att, defender,
                                  [(builds.stat_pairs(self.pool[i]),
                                    self.effects.get(i, {})) for i in ids],
                                  list(thresholds))

    P = 1 / 0.64  # the clean attacker's attack period

    def test_bare_tank_dies_to_the_tenth_auto(self):
        r = self.fight(self.attacker())
        self.assertEqual(r["attacks"], 10)
        self.assertEqual(r["ttk"], 9 * self.P)          # 590 / 59
        self.assertEqual(r["time_to_die"], r["ttk"])
        self.assertEqual(r["total"], 590.0)
        self.assertEqual(r["defense"]["hpLost"], 590.0)

    def test_steelcaps_take_a_tenth_off_attacks(self):
        r = self.fight(self.attacker(), ["Plated Steelcaps"])
        per = 59 * 100 / 125 * 0.9                      # 25 armor, then Plating
        self.assertEqual(r["attacks"], math.ceil(590 / per))
        self.assertEqual(r["ttk"], (math.ceil(590 / per) - 1) * self.P)
        self.assertAlmostEqual(r["defense"]["reducedAttack"],
                               r["attacks"] * 59 * 100 / 125 * 0.1)

    def test_randuins_cuts_the_crit_share(self):
        # Infinity Edge: 134 AD, 25% crit at 205% -> expected 1.2625 a hit;
        # Randuin's leaves the normal 75% alone and takes 30% off the crits
        att = self.attacker(["Infinity Edge"])
        self.assertEqual((att["sheet"]["ad"], att["sheet"]["crit_chance"],
                          att["sheet"]["crit_damage"]), (134.0, 25.0, 205.0))
        r = self.fight(att, ["Randuin's Omen"], base=self.base(hp=1000))
        ev = 0.75 + 0.25 * 2.05 * 0.7
        per = 134 * 100 / 175 * ev
        n = math.ceil(1350 / per)
        self.assertEqual(r["attacks"], n)
        self.assertAlmostEqual(r["ttk"], (n - 1) * self.P)
        self.assertAlmostEqual(r["defense"]["reducedCrit"],
                               n * 134 * 100 / 175 * 0.25 * 2.05 * 0.3)

    def test_jaksho_raises_bonus_resists_after_five_seconds(self):
        r = self.fight(self.attacker(duration=10.0), ["Jak'Sho, The Protean"],
                       base=self.base(hp=100000))
        self.assertEqual(r["defense"]["voidbornAt"], 5.0)
        # autos at 0, 1.56, 3.13, 4.69 against 45 armor; 6.25, 7.81, 9.38
        # against 45 x 1.3
        self.assertAlmostEqual(r["total"], 4 * 59 * 100 / 145 + 3 * 59 * 100 / 158.5)
        self.assertIsNone(r["ttk"])
        self.assertAlmostEqual(r["hp_left"], 100350 - r["total"])

    def test_force_of_nature_stacks_on_magic_hits(self):
        # Nashor's: 27 magic on-hit (15 + 15% of 80 AP) and 50% attack speed
        att = self.attacker(["Nashor's Tooth"])
        period = 1 / att["sheet"]["attack_speed"]
        r = self.fight(att, ["Force of Nature"], base=self.base(hp=100000))
        # a stack an on-hit (they are 1.04 s apart, past the 1 s limit):
        # the eighth auto's on-hit completes them, and 70 MR joins the 55
        self.assertAlmostEqual(r["defense"]["steadfastAt"], 7 * period)
        n = r["attacks"]
        self.assertAlmostEqual(r["breakdown"]["onhit"],
                               8 * 27 * 100 / 155 + (n - 8) * 27 * 100 / 225)

    def test_kaenic_shield_takes_magic_only(self):
        att = self.attacker(["Nashor's Tooth"])
        r = self.fight(att, ["Kaenic Rookern"], base=self.base(hp=100000))
        # 15 magic an on-hit through 80 MR, all of it into a 15,060 shield
        # that outlasts the fight; the autos (physical) go straight to health
        self.assertAlmostEqual(r["breakdown"]["onhit"], r["attacks"] * 27 * 100 / 180)
        self.assertAlmostEqual(r["defense"]["shieldMagic"], r["breakdown"]["onhit"])
        self.assertAlmostEqual(r["defense"]["hpLost"], r["breakdown"]["auto"])
        short = self.fight(att, ["Kaenic Rookern"], base=self.base(hp=1000))
        self.assertAlmostEqual(short["defense"]["shieldMagic"], 0.15 * 1400)
        self.assertAlmostEqual(short["defense"]["hpLost"], 1400)  # to the last point

    def test_sterak_shield_on_the_threshold_blow_then_decays(self):
        r = self.fight(self.attacker(), ["Sterak's Gage"], base=self.base(hp=1000))
        d = r["defense"]
        # 1,400 health: the 17th auto (at 25 s) would leave 397 < 420 (30%);
        # a 240 shield (60% of 400 bonus) takes it whole and holds 0.75 s,
        # then decays 64 a second: 129 left for the 18th, gone by the 19th
        self.assertEqual(d["lifelineAt"], 16 * self.P)
        self.assertAlmostEqual(d["shieldLifeline"], 2 * 59)
        self.assertIsNone(r["ttk"])  # the 25th auto would be at 37.5 s

    def test_guardian_angel_revives_after_four_seconds(self):
        r = self.fight(self.attacker(duration=40.0), ["Guardian Angel"])
        per = 59 * 100 / 145
        first = (math.ceil(590 / per) - 1) * self.P
        self.assertEqual(r["defense"]["reviveAt"], first)
        self.assertEqual(r["defense"]["reviveHealth"], 295.0)  # half of base
        # the attacker waits out the stasis, then needs 295 more
        self.assertAlmostEqual(r["ttk"], first + 4 + (math.ceil(295 / per) - 1) * self.P)

    def test_zhonyas_takes_the_killing_blow(self):
        r = self.fight(self.attacker(duration=40.0), ["Zhonya's Hourglass"],
                       base=self.base(hp=600))
        per = 59 * 100 / 150
        lethal = math.ceil(600 / per) - 1          # 0-based: the 16th auto
        self.assertEqual(r["defense"]["zhonyaAt"], lethal * self.P)
        self.assertAlmostEqual(r["defense"]["negated"], per)
        # the next auto waits for the stasis to end, and kills
        self.assertEqual(r["ttk"], lethal * self.P + 2.5)

    def test_frozen_heart_slows_the_attacker(self):
        r = self.fight(self.attacker(duration=40.0), ["Frozen Heart"])
        per = 59 * 100 / 175
        self.assertEqual(r["ttk"], (math.ceil(590 / per) - 1) / (0.64 * 0.8))

    def test_death_dance_defers_a_share_as_a_bleed(self):
        r = self.fight(self.attacker(duration=40.0), ["Death's Dance"],
                       base=self.base(hp=600))
        per = 59 * 100 / 150
        # a reference: 70% at each auto, 30% bled evenly over the next 3 s
        hits = [k * self.P for k in range(40)]
        edges = sorted(set(hits) | {h + 3 for h in hits})
        hp, t, death = 600.0, 0.0, None
        for e in edges:
            if e > t:
                rate = sum(0.3 * per / 3 for h in hits if h <= t < h + 3)
                if rate and hp - rate * (e - t) <= 0:
                    death = t + hp / rate
                    break
                hp -= rate * (e - t)
                t = e
            if e in hits:
                hp -= 0.7 * per
                if hp <= 0:
                    death = e
                    break
        self.assertIsNotNone(death)
        self.assertAlmostEqual(r["ttk"], death, places=9)
        self.assertAlmostEqual(r["defense"]["deferred"], 0.3 * r["total"])

    def test_spirit_visage_regeneration(self):
        # 50 health per 5 s, +100% (the item) +25% (Boundless Vitality): 25
        # a second, never capped — every auto opens a bigger deficit
        r = self.fight(self.attacker(duration=10.0), ["Spirit Visage"],
                       base=self.base(hp=100000, regen=50))
        self.assertAlmostEqual(r["defense"]["regen"], 25 * 10)
        self.assertAlmostEqual(r["hp_left"], 100400 - 7 * 59 + 250)

    def test_unending_despair_drains_every_four_seconds(self):
        r = self.fight(self.attacker(duration=10.0), ["Unending Despair"],
                       base=self.base(hp=100000))
        # 3% of 400 bonus health through the attacker's 30 MR, healed 250%
        per = 2.5 * 0.03 * 400 * 100 / 130
        self.assertAlmostEqual(r["defense"]["healedDrain"], 2 * per)  # at 4 and 8

    def test_warmogs_vitality_counts_item_health(self):
        r = self.fight(self.attacker(), ["Warmog's Armor"])
        self.assertAlmostEqual(r["sheet"]["hp"], 590 + 1000 * 1.12)

    def test_mundo_heart_zapper_and_maximum_dosage(self):
        # level-16 Mundo, no items, against the clean attacker at 16
        att = self.attacker(level=16)
        kit, champ = builds.load_kit("drmundo"), builds.load_champion("drmundo")
        ranks = builds.skill_ranks(16, builds.kit_max_order(kit))
        self.assertEqual(ranks, {"Q": 5, "W": 3, "E": 5, "R": 3})
        defender = dict(base=builds.champ_base(champ), level=16, ranks=ranks, kit=kit)
        survive = builds.lol_engine.survive
        r = survive(att, defender, [], [1.0])
        d, sheet = r["defense"], r["sheet"]
        hp = sheet["hp"]
        self.assertAlmostEqual(hp, 640 + 103 * builds.growth(16))
        per = att["sheet"]["ad"] * 100 / (100 + sheet["armor"])
        period = 1 / att["sheet"]["attack_speed"]
        # Heart Zapper at 0 and again 16 s later (rank 3, no haste)
        self.assertEqual(d["wCasts"], 2)
        # Maximum Dosage right after the first hit (threshold 1.0): 30% of
        # the missing health (25% + 5% for one champion near) as base
        # health — the zapper's 8% and the hit
        self.assertEqual(d["rAt"], 0.0)
        self.assertEqual(d["rThreshold"], 1.0)
        self.assertAlmostEqual(d["rBaseHealth"], 0.30 * (0.08 * hp + per))
        # the first three seconds alone, Maximum Dosage never cast: autos
        # at 0 (stored at 80-95% by level, 93.24% at 16), 1.02 and 2.04
        # (25%), all of it back at 3 s — Kassadin stands inside 325
        self.assertLess(2 * period, 3.0)
        self.assertGreater(3 * period, 3.0)
        first = survive(dict(att, duration=3.5), defender, [], [])
        f = first["defense"]
        self.assertIsNone(f["rAt"])
        self.assertAlmostEqual(f["hpSpent"], 0.08 * hp)
        frac = (80 + 15 * 15 / 17) / 100
        self.assertAlmostEqual(f["healedW"], per * (frac + 0.25 + 0.25))
        # and regeneration: 1.9% of maximum health every 5 s at 16, on top
        # of the base 7 (+0.5 a level) — never capped here
        regen = (0.019 * hp + 7 + 0.5 * builds.growth(16)) / 5
        self.assertAlmostEqual(f["regen"], regen * 3.5)


class TestSurvivalSearch(unittest.TestCase):
    """Maximum Dosage's timing: the grid search is exactly its best cell,
    though it fights only the thresholds whose casts differ."""

    @classmethod
    def setUpClass(cls):
        _, cls.pool = builds.load_items()
        cls.effects = builds.load_item_effects()
        idx = builds.item_index(cls.pool)
        cls.item = lambda self, n: builds.resolve_item(self.pool, idx, n)
        cls.kit, cls.champ = builds.load_kit("drmundo"), builds.load_champion("drmundo")
        cls.ranks = builds.skill_ranks(16, builds.kit_max_order(cls.kit))
        cls.att = {}
        for slug, names in (("kayle", ["Berserker's Greaves", "Infinity Edge",
                                       "Yun Tal Wildarrows", "Lord Dominik's Regards",
                                       "Hexoptics C44", "Umbral Glaive"]),
                            ("kassadin", ["Ionian Boots of Lucidity", "Malignance",
                                          "Seraph's Embrace", "Cryptbloom", "Actualizer",
                                          "Muramana"])):
            kit, champ = builds.load_kit(slug), builds.load_champion(slug)
            ids = [builds.resolve_item(cls.pool, idx, n) for n in names]
            cls.att[slug] = dict(
                sheet=builds.resolve_stats(champ, 16, ids, cls.pool, cls.effects, kit=kit),
                kit=kit, fx=builds.merge_effects(ids, cls.effects), level=16,
                ranks=builds.skill_ranks(16, builds.kit_max_order(kit)), duration=30.0)

    BUILDS = [
        ["Mercury's Treads"],
        ["Plated Steelcaps", "Warmog's Armor", "Heartsteel", "Jak'Sho, The Protean",
         "Randuin's Omen", "Force of Nature"],
        ["Mercury's Treads", "Warmog's Armor", "Spirit Visage", "Jak'Sho, The Protean",
         "Force of Nature", "Kaenic Rookern"],
        ["Plated Steelcaps", "Warmog's Armor", "Heartsteel", "Guardian Angel",
         "Randuin's Omen", "Sterak's Gage"],
        ["Plated Steelcaps", "Spirit Visage", "Protoplasm Harness", "Death's Dance",
         "Zhonya's Hourglass", "Unending Despair"],
    ]

    def survive(self, slug, names, thresholds):
        ids = [self.item(n) for n in names]
        return builds.survive(self.champ, self.kit, 16, self.ranks, ids, self.pool,
                              self.effects, self.att[slug], thresholds=thresholds)

    def test_probe_search_is_the_grid_best(self):
        grid = builds.R_THRESHOLDS
        for slug in self.att:
            for names in self.BUILDS:
                full = self.survive(slug, names, grid)
                # each single-threshold call fights that threshold and the
                # lethal-only cast: together, every point of the grid
                singles = [self.survive(slug, names, [x]) for x in grid]
                best = max(s["time_to_die"] for s in singles)
                self.assertEqual(full["time_to_die"], best, (slug, names))
                # and the threshold it reports reproduces it
                x = full["defense"]["rThreshold"]
                again = self.survive(slug, names, [x] if x else [])
                self.assertEqual(again["time_to_die"], full["time_to_die"])

    def test_survival_never_beats_its_components(self):
        # sanity on the model's direction: a tank item never shortens a life
        for slug in self.att:
            bare = self.survive(slug, ["Mercury's Treads"], builds.R_THRESHOLDS)
            for extra in ("Warmog's Armor", "Jak'Sho, The Protean", "Randuin's Omen",
                          "Force of Nature", "Spirit Visage", "Guardian Angel"):
                more = self.survive(slug, ["Mercury's Treads", extra], builds.R_THRESHOLDS)
                self.assertGreater(more["time_to_die"], bare["time_to_die"], (slug, extra))


class TestSurvivalEnumeration(unittest.TestCase):
    """The Survival tier's pass ranks exactly what fighting every build of
    the pool one by one ranks."""

    def test_pass_ranks_like_one_fight_per_build(self):
        _, pool = builds.load_items()
        effects = builds.load_item_effects()
        kit, champ = builds.load_kit("drmundo"), builds.load_champion("drmundo")
        ranks = builds.skill_ranks(16, builds.kit_max_order(kit))
        att = TestSurvivalSearch.att if hasattr(TestSurvivalSearch, "att") else None
        if att is None:
            TestSurvivalSearch.setUpClass()
            att = TestSurvivalSearch.att
        attackers = {"survive-kayle": att["kayle"], "survive-kassadin": att["kassadin"]}
        cands = [3083, 3143, 3065, 6665, 4401, 3026, 2525]   # seven tank items
        lists, count = builds.enumerate_survival(
            champ, pool, effects, kit, 16, ranks, attackers, cands, "overall",
            keep=1000, workers=3)
        # every boots x five of seven, minus what Riot's groups forbid
        self.assertEqual(count, len(builds.BOOTS) * math.comb(7, 5))
        order = builds._enum_order(cands)
        rows = []
        for combo in itertools.combinations(cands, 5):
            for b in builds.BOOTS:
                ids = [b, *combo]
                fs = {k: builds.survive(champ, kit, 16, ranks, ids, pool, effects, a)
                      for k, a in attackers.items()}
                rows.append((ids, fs))
        place = lambda ids: ([order[i] for i in ids[1:]], order[ids[0]])
        for key in attackers:
            def k(row):
                f = row[1][key]
                return ((0, -f["time_to_die"], -f["hp_left"]) if f["ttk"] is None
                        else (1, -f["ttk_exp"], -f["ttk_eff"]), place(row[0]))
            want = [ids for ids, _ in sorted(rows, key=k)]
            self.assertEqual([ids for _, ids, _ in lists[key]], want, key)
            for (_, ids, fs), (wids, wfs) in zip(lists[key], sorted(rows, key=k)):
                self.assertEqual(fs[key]["time_to_die"], wfs[key]["time_to_die"])
        def ok(row):
            fs = [row[1][k] for k in attackers]
            killed = sum(f["ttk"] is not None for f in fs)
            effs = [f["ttk_eff"] if f["ttk"] is not None else f["time_to_die"] for f in fs]
            return ((killed, -builds.geo_mean([f["time_to_die"] for f in fs]),
                     -builds.geo_mean(effs)), place(row[0]))
        self.assertEqual([ids for _, ids, _ in lists["overall"]],
                         [ids for ids, _ in sorted(rows, key=ok)])


class TestSurvivalGolden(unittest.TestCase):
    """The Survival tier's fights pinned bit for bit (data/builds/golden/
    survival.json): ~150 Dr. Mundo builds against pinned Kayle and Kassadin
    builds, every number of the fight and the defender's report, and one
    enumeration pass. A deliberate model change regenerates it with
    `jobs/gen_golden.py --only survival` in the same commit."""

    def test_survival(self):
        with open(os.path.join(TestGolden.GOLDEN, "survival.json")) as f:
            doc = json.load(f)
        _, pool = builds.load_items()
        effects = builds.load_item_effects()
        self.assertEqual(doc["patch"], builds.load_items()[0])
        self.assertEqual(doc["thresholds"], builds.R_THRESHOLDS)
        idx = builds.item_index(pool)
        kit, champ = builds.load_kit("drmundo"), builds.load_champion("drmundo")
        ranks = builds.skill_ranks(16, builds.kit_max_order(kit))
        attackers = {}
        for key, a in doc["attackers"].items():
            akit, achamp = builds.load_kit(a["champion"]), builds.load_champion(a["champion"])
            aids = [builds.resolve_item(pool, idx, n) for n in a["items"]]
            attackers[key] = dict(
                sheet=builds.resolve_stats(achamp, 16, aids, pool, effects, kit=akit),
                kit=akit, fx=builds.merge_effects(aids, effects), level=16,
                ranks=builds.skill_ranks(16, builds.kit_max_order(akit)),
                duration=builds.SCENARIOS[key]["duration"])
        for case in doc["cases"]:
            got = builds.survive(champ, kit, 16, ranks, case["ids"], pool, effects,
                                 attackers[case["attacker"]])
            self.assertIsNone(TestGolden.first_diff(case["result"], got, "result"),
                              (case["id"], case["items"], case["attacker"]))
        run = doc["enumerate"]
        lists, count = builds.enumerate_survival(
            champ, pool, effects, kit, 16, ranks, attackers, run["pool"],
            run["overall"], keep=run["keep"], workers=2)
        self.assertEqual(count, run["count"])
        for key, rows in run["lists"].items():
            self.assertEqual([r["ids"] for r in rows], [list(ids) for _, ids, _ in lists[key]], key)
            for r, (sk, _, fights) in zip(rows, lists[key]):
                self.assertIsNone(TestGolden.first_diff(r["key"], list(sk), "key"))
                self.assertIsNone(TestGolden.first_diff(
                    r["ttd"], {a: f["time_to_die"] for a, f in fights.items()}, "ttd"))


class TestBootsPartitions(unittest.TestCase):
    """Boots classes per build: move speed only charges Energized items, so
    without one Swiftness fights like the 45-speed boots; a kit that never
    attacks reads neither move speed nor attack speed, so Berserker's joins
    them too."""

    @classmethod
    def setUpClass(cls):
        _, cls.pool = builds.load_items()
        cls.effects = builds.load_item_effects()
        cls.kayle, cls.vlad = builds.load_kit("kayle"), builds.load_kit("vladimir")
        cls.name = lambda self, b: self.pool[b]["name"]

    def classes(self, kit, calm):
        return [[self.pool[b]["name"] for b in c]
                for c in builds.boots_partitions(self.pool, self.effects, kit)[calm]]

    def test_kayle_without_an_energized_item_merges_swiftness(self):
        self.assertEqual(self.classes(self.kayle, True), [
            ["Berserker's Greaves"], ["Sorcerer's Shoes"],
            ["Ionian Boots of Lucidity"],
            ["Boots of Swiftness", "Mercury's Treads", "Plated Steelcaps",
             "Gluttonous Greaves"]])
        self.assertEqual(self.classes(self.kayle, False),
                         [[self.pool[b]["name"] for b in c]
                          for c in builds.boots_classes(self.pool, self.effects)])

    def test_twitch_attacks_so_his_boots_partition_like_kayles(self):
        twitch = builds.load_kit("twitch")
        for calm in (True, False):
            self.assertEqual(self.classes(twitch, calm), self.classes(self.kayle, calm))

    def test_vladimir_never_attacks_so_only_pen_and_haste_tell_boots_apart(self):
        for calm in (True, False):
            self.assertEqual(self.classes(self.vlad, calm), [
                ["Berserker's Greaves", "Boots of Swiftness", "Mercury's Treads",
                 "Plated Steelcaps", "Gluttonous Greaves"],
                ["Sorcerer's Shoes"], ["Ionian Boots of Lucidity"]])

    def fight(self, slug, kit, ids):
        champ = builds.load_champion(slug)
        ranks = builds.skill_ranks(16, builds.kit_max_order(kit))
        sheet = builds.resolve_stats(champ, 16, ids, self.pool, self.effects, kit=kit)
        r = builds.simulate(sheet, kit, builds.merge_effects(ids, self.effects), 16,
                            ranks, 4800, 220, 160, 15, target_bonus_hp=1500)
        return (r["total"], r["ttk"], r["ttk_exp"], r["breakdown"])

    def test_the_merged_boots_really_fight_alike(self):
        idx = builds.item_index(self.pool)
        item = lambda n: builds.resolve_item(self.pool, idx, n)
        calm = [item(n) for n in ("Infinity Edge", "Kraken Slayer",
                                  "Lord Dominik's Regards", "Nashor's Tooth")]
        charged = [item("Rapid Firecannon"), *calm]
        # Kayle, no Energized item: Swiftness = Steelcaps
        self.assertEqual(self.fight("kayle", self.kayle, [3009, *calm]),
                         self.fight("kayle", self.kayle, [3047, *calm]))
        # with one, the 10 extra move speed charges it sooner
        self.assertNotEqual(self.fight("kayle", self.kayle, [3009, *charged]),
                            self.fight("kayle", self.kayle, [3047, *charged]))
        # Vladimir: Berserker's = Steelcaps, even with an Energized item
        vlad = [item(n) for n in ("Rabadon's Deathcap", "Void Staff", "Liandry's Torment")]
        self.assertEqual(self.fight("vladimir", self.vlad, [3006, *vlad]),
                         self.fight("vladimir", self.vlad, [3047, *vlad]))
        self.assertEqual(self.fight("vladimir", self.vlad, [3006, item("Stormrazor"), *vlad[:2]]),
                         self.fight("vladimir", self.vlad, [3009, item("Stormrazor"), *vlad[:2]]))


if __name__ == "__main__":
    unittest.main()
