"""Hand-computed checks for the builds stat layer.

Run: python3 -m unittest test_builds -v

The math tests pin the League formulas to values computed by hand from the
wiki; the integration tests read the committed data/items snapshot, so they
also catch a meraki schema change sneaking past ITEM_STAT_MAP.
"""

import unittest

import builds


def fake_champ(**dd_overrides):
    """A Kayle-shaped champion snapshot (patch 16.16 values)."""
    dd = {"name": "Kayle", "stats": {
        "hp": 670, "hpperlevel": 92, "mp": 330, "mpperlevel": 50,
        "armor": 26, "armorperlevel": 4.2,
        "spellblock": 22, "spellblockperlevel": 1.3,
        "attackdamage": 50, "attackdamageperlevel": 0,
        "attackspeed": 0.625, "attackspeedperlevel": 1.5,
        "movespeed": 335,
    }}
    dd["stats"].update(dd_overrides)
    mk = {"stats": {"attackSpeedRatio": {"flat": 0.667},
                    "criticalStrikeDamage": {"flat": 175.0}}}
    return {"slug": "kayle", "dd": dd, "mk": mk, "meta": {"patch": "16.16"}}


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
        # 4 seething stacks to reach max, 2 more attacks to bank 2 phantom
        # stacks -> first phantom on attack 7, then every other attack.
        r = self.sim(16, ["rageblade"], hp=100_000, duration=10)
        self.assertGreater(r["phantom_hits"], 0)
        expected = max(0, (r["attacks"] - 6 + 1) // 2)
        self.assertAlmostEqual(r["phantom_hits"], expected, delta=1)

    def test_api_scenario_shape(self):
        # cheapest preset; exercises the full web-API path end to end
        builds._OPTIMIZE_CACHE.clear()
        d = builds.api_optimize_scenario("kayle", "first-item", top=5)
        self.assertEqual(d["champion"], "kayle")
        self.assertEqual([r["rank"] for r in d["rows"]], [1, 2, 3, 4, 5])
        for r in d["rows"]:
            self.assertLessEqual(r["gold"], 4500)
            self.assertAlmostEqual(sum(r["breakdown"].values()), r["total"],
                                   delta=len(r["breakdown"]))  # rounding
        ttks = [r["ttk"] for r in d["rows"] if r["ttk"] is not None]
        self.assertEqual(ttks, sorted(ttks))
        self.assertIn("first-item", [s["key"] for s in
                                     builds.api_builds_meta()["scenarios"]])

    def test_liandry_burns(self):
        r = self.sim(16, ["liandry"], hp=10_000, duration=6)
        self.assertIn("burn", r["breakdown"])
        # each tick is 1% max hp pre-mitigation; with 60 mr and no pen the
        # burn can't exceed 1%/tick * ticks
        self.assertLess(r["breakdown"]["burn"], 0.01 * 10_000 * 13)


if __name__ == "__main__":
    unittest.main()
