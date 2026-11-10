"""Tests for the kit-dossier and machine-written driver pipeline
(lol/codegen/sources.py, lol/codegen/dossier.py, lol/codegen/driver.py):
numbers sheets from Riot's bin, wiki template parsing, dossier checking,
driver registry, static lints, Jax reference driver,
and cast-time / enumeration checks. Hermetic: no model is called."""

import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from lol import builds
from lol.codegen import dossier, driver as kd, sources as ks

PATCH = "16.18"


# ---------------------------------------------------------------------------
# sources & dossier tests (from test_kit_sources.py)
# ---------------------------------------------------------------------------

def rebuilt_sheet(slug):
    """The sheet as the code builds it now from the archived raw bin (not the
    stored sheet.json, which an older version of the code may have written)."""
    d = os.path.join(ks.SOURCES_DIR, PATCH, slug)
    with open(os.path.join(d, "bin.json")) as f:
        raw = json.load(f)
    with open(os.path.join(d, "sheet.json")) as f:
        dd = json.load(f)["ddragon"]
    return ks.bin_sheet(raw, dd)


def spell(sheet, script):
    return next(sp for sps in sheet["slots"].values() for sp in sps if sp["script"] == script)


class TestNumbers(unittest.TestCase):
    def test_clean_reads_float32_as_typed(self):
        self.assertEqual(ks.clean(0.07000000029802322), 0.07)
        self.assertEqual(ks.clean(0.019999999552965164), 0.02)
        self.assertEqual(ks.clean(9000.0), 9000)
        self.assertEqual(ks.clean(3.3329999446868896), 3.333)

    def test_vectors_collapse_and_combine(self):
        self.assertEqual(ks.num([4.0, 4.0, 4.0], "rank"), 4)
        v = ks.num([1.0, 2.0], "rank")
        self.assertEqual(ks.n_mul(v, 6), {"axis": "rank", "v": [6, 12]})
        self.assertIsNone(ks.n_add(v, ks.num([1.0, 2.0, 3.0], "level")))

    def test_seq_match_knows_percent_and_fraction(self):
        self.assertTrue(ks.seq_match((0.8,), (80.0,)))
        self.assertTrue(ks.seq_match((4.0, 4.5), (0.04, 0.045)))
        self.assertTrue(ks.seq_match((25.0,), (25.0, 25.0, 25.0)))
        self.assertFalse(ks.seq_match((70.0, 90.0, 110.0), (80.0, 95.0, 110.0)))


class TestSheet(unittest.TestCase):
    def test_kassadin_live_and_stale_rows(self):
        r = spell(rebuilt_sheet("kassadin"), "RiftWalk")
        self.assertEqual(r["dataValues"]["RBaseDamage"]["v"], [80, 95, 110])
        self.assertEqual(r["dataValues"]["RiftWalkBaseDamage"]["v"], [70, 90, 110])  # stale
        calc = r["calcs"]["BaseDamage"]
        self.assertEqual(calc["flat"]["v"], [80, 95, 110])
        self.assertEqual([(t["stat"], t["coef"]) for t in calc["terms"]],
                         [("ap", 0.5), ("abilityResource", 0.02)])
        self.assertNotIn("RiftWalkBaseDamage", calc["refs"])
        self.assertEqual(r["cooldown"]["v"], [5, 3.5, 2])  # the rank-0 entry dropped

    def test_by_level_breakpoints(self):
        venom = spell(rebuilt_sheet("twitch"), "TwitchDeadlyVenomMarker")["calcs"]["DamagePerSecond"]
        self.assertEqual(venom["flat"]["v"],
                         [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5])
        # 20 until Aflame's level 11, then +3 a level: Riot's file and the wiki agree
        wave = spell(rebuilt_sheet("kayle"), "KaylePassive")["calcs"]["PassiveWaveDamage"]
        self.assertEqual(wave["flat"]["v"], [20] * 11 + [23, 26, 29, 32, 35, 38, 41])
        self.assertEqual([(t["stat"], t["scope"], t["coef"]) for t in wave["terms"]],
                         [("ap", "total", 0.25), ("ad", "bonus", 0.1)])

    def test_cost_reads_the_live_array(self):
        q = spell(rebuilt_sheet("jax"), "JaxQ")
        self.assertEqual((q["cost"], q["costLegacy"]), (50, 65))
        self.assertEqual(spell(rebuilt_sheet("cassiopeia"), "CassiopeiaE")["cost"], 45)
        self.assertNotIn("costLegacy", spell(rebuilt_sheet("kassadin"), "NullLance"))

    def test_stat_labels(self):
        vlad = spell(rebuilt_sheet("vladimir"), "VladimirE")["calcs"]["MaxDamageTooltip"]
        self.assertIn(("maxHp", "total", 0.06), [(t["stat"], t["scope"], t["coef"])
                                                for t in vlad["terms"]])
        ashe = spell(rebuilt_sheet("ashe"), "AshePassive")["calcs"]["DamageBonus"]
        self.assertIn("critChance", ashe["expr"])
        self.assertIn("bonus critDamage", ashe["expr"])


class TestWiki(unittest.TestCase):
    TEMPLATE = ("<!-- values -->{{#vardefine:b1|80}}<!--\n-->{{#vardefine:b5|220}}<!--\n"
                "-->{{{{{1<noinclude>|Ability data</noinclude>}}}|Smash|{{{2|}}}\n"
                "|skill        = Q\n"
                "|leveling     = {{st|Magic Damage|{{ap|{{#var:b1}} to {{#var:b5}}}} {{as|(+ 70% AP)}}"
                "|Bonus|{{ap|4*5 to 6*5}}%}}\n"
                "|cooldown     = {{ap|8 to 4}}\n|spelleffects = aoe\n"
                "|notes        = * A note with a [[link|pipe]].\n}}<noinclude>docs</noinclude>")

    def test_fields_past_a_vardefine_preamble(self):
        f = ks.template_fields(self.TEMPLATE)
        self.assertEqual(f["cooldown"], "{{ap|8 to 4}}")
        self.assertEqual(f["spelleffects"], "aoe")
        self.assertEqual(f["notes"], "* A note with a [[link|pipe]].")
        self.assertIn("{{ap|80 to 220}}", f["leveling"])

    def test_leveling_entries(self):
        self.assertEqual(ks.st_entries(self.TEMPLATE),
                         [("Magic Damage", "{{ap|80 to 220}} {{as|(+ 70% AP)}}"),
                          ("Bonus", "{{ap|4*5 to 6*5}}%")])

    def test_archived_fields(self):
        wiki = ks.load_sources("kassadin", PATCH)["wiki"]
        q = wiki["abilities"]["Q"][0]
        self.assertEqual(q["name"], "Null Sphere")
        self.assertEqual(q["flags"]["damagetype"], "Magic")
        self.assertEqual(ks.template_fields(q["wikitext"])["cooldown"], "{{ap|9 to 7}}")


class TestChecker(unittest.TestCase):
    """A minimal valid dossier for Kassadin's Q and R, then one defect at a time."""

    @classmethod
    def setUpClass(cls):
        cls.src = ks.load_sources("kassadin", PATCH)
        empty = {"name": "x", "cooldown": None, "cost": None, "castTime": None, "values": [],
                 "mechanics": []}
        unused = {"P": ["VoidStone.DamageReductionPercent"],
                  "Q": ["NullLance.ShieldAmount", "NullLance.ShieldDuration"],
                  "W": ["NetherBlade.ActiveBaseDamage", "NetherBlade.PassiveBaseDamage",
                        "NetherBlade.MissingManaRatio", "NetherBlade.ChampionMissingManaRatio"],
                  "E": ["ForcePulse.ReductionPerSpellCast", "ForcePulse.BaseDamage",
                        "ForcePulse.APRatio", "ForcePulse.SlowAmount", "ForcePulse.SlowDuration"],
                  "R": ["RiftWalk.BaseCD", "RiftWalk.RBaseCost", "RiftWalk.StackDamage",
                        "RiftWalk.APStackRatio", "RiftWalk.RStackDuration", "RiftWalk.MaxStacks",
                        "RiftWalk.RStackManaRatio", "RiftWalk.RiftWalkBaseDamage",
                        "RiftWalk.CastRange"]}
        abilities = {s: dict(copy.deepcopy(empty),
                             binUnused=[{"ref": r, "reason": "unknown"} for r in unused[s]])
                     for s in ks.SLOTS}
        abilities["Q"]["cooldown"] = {"values": [9, 8.5, 8, 7.5, 7], "quote": "{{ap|9 to 7}}"}
        abilities["Q"]["values"] = [{
            "id": "q_1", "meaning": "damage", "role": "damage", "damageType": "magic",
            "by": "rank", "base": [65, 95, 125, 155, 185], "terms": [{"stat": "ap", "coef": 0.8}],
            "quote": "{{ap|65 to 185}} {{as|(+ 80% AP)}}", "bin": ["NullLance#TotalDamage"]}]
        abilities["R"]["values"] = [{
            "id": "r_1", "meaning": "damage", "role": "damage", "damageType": "magic",
            "by": "rank", "base": [80, 95, 110],
            "terms": [{"stat": "ap", "coef": 0.5}, {"stat": "maxMana", "coef": 0.02}],
            "quote": "{{ap|80 to 110}} {{as|(+ 50% AP)}} {{as|(+ 2% '''maximum''' mana)}}",
            "bin": ["RiftWalk#BaseDamage", "RiftWalk.RManaRatio"]}]
        cls.good = {"champion": "kassadin", "abilities": abilities,
                    "primitives": [{"id": "plain-cast", "abilities": ["Q"]}],
                    "needsBespoke": {"value": False, "why": "x"}, "unsettled": [],
                    "rotation": {"proposal": [], "questions": []}}

    def errors(self, d):
        chk = ks.Checker("kassadin", d, self.src)
        chk.run()
        return chk.errors

    def test_valid(self):
        self.assertEqual(self.errors(self.good), [])

    def test_mistyped_number(self):
        d = copy.deepcopy(self.good)
        d["abilities"]["Q"]["values"][0]["base"][4] = 190
        self.assertTrue(any("matches none of its bin rows" in e for e in self.errors(d)))

    def test_stale_row_numbers_do_not_pass(self):
        d = copy.deepcopy(self.good)
        d["abilities"]["R"]["values"][0]["base"] = [70, 90, 110]
        self.assertTrue(any("abilities.R.values[r_1]: base" in e for e in self.errors(d)))

    def test_invented_quote(self):
        d = copy.deepcopy(self.good)
        d["abilities"]["Q"]["values"][0]["quote"] = "deals 42 bonus damage"
        self.assertTrue(any("not in the archived wikitext" in e for e in self.errors(d)))

    def test_cooldown_against_the_bin(self):
        d = copy.deepcopy(self.good)
        d["abilities"]["Q"]["cooldown"]["values"] = [10, 9, 8, 7, 6]
        self.assertTrue(any("differs from the bin's" in e for e in self.errors(d)))
        d["abilities"]["Q"]["cooldown"]["binNote"] = "the wiki says otherwise"
        self.assertEqual(self.errors(d), [])  # explained: a warning for the reviewer

    def test_every_data_value_is_accounted_for(self):
        d = copy.deepcopy(self.good)
        d["abilities"]["R"]["binUnused"].pop()  # CastRange
        self.assertTrue(any("neither used by a value nor listed" in e and "CastRange" in e
                            for e in self.errors(d)))

    def test_a_named_row_has_to_carry_a_number(self):
        d = copy.deepcopy(self.good)
        d["abilities"]["R"]["values"][0]["bin"].append("RiftWalk.MaxStacks")
        d["abilities"]["R"]["binUnused"] = [u for u in d["abilities"]["R"]["binUnused"]
                                            if u["ref"] != "RiftWalk.MaxStacks"]
        self.assertTrue(any("carries none of this value's numbers" in e for e in self.errors(d)))

    def test_quotes_may_drop_wiki_markup_but_not_words(self):
        d = copy.deepcopy(self.good)
        mech = {"claim": "x", "affectsDamageFight": False}
        # the wikitext reads: deals {{as|magic damage}} and {{tip|disrupt|disrupts}} their ongoing
        d["abilities"]["Q"]["mechanics"] = [
            dict(mech, quote="deals magic damage and disrupts their ongoing channels"),
            dict(mech, quote="fires an orb of void energy ... absorbs magic damage")]
        self.assertEqual(self.errors(d), [])
        d["abilities"]["Q"]["mechanics"] = [dict(mech, quote="silences them for 2 seconds")]
        self.assertTrue(any("not in the archived wikitext" in e for e in self.errors(d)))

    def test_no_cast_time_and_leftover_skeleton_entries(self):
        d = copy.deepcopy(self.good)
        d["abilities"]["W"]["castTime"] = {"value": None, "quote": "none"}
        d["abilities"]["Q"]["binUnused"] += [
            {"ref": "NullLance.BaseDamage", "reason": None},  # used by q_1: as good as deleted
            {"ref": "NullLance#TotalShield", "reason": "defensive"}]  # a calculation: harmless
        self.assertEqual(self.errors(d), [])
        d["abilities"]["Q"]["binUnused"][0]["reason"] = None  # ShieldAmount: nothing uses it
        self.assertTrue(any("reason must be one of" in e for e in self.errors(d)))
        d = copy.deepcopy(self.good)
        d["abilities"]["Q"]["castTime"] = {"value": None, "quote": "{{fd|0.25}}"}
        self.assertTrue(any("still null" in e for e in self.errors(d)))

    def test_refs_and_quotes_as_a_model_writes_them(self):
        d = copy.deepcopy(self.good)
        mech = {"claim": "x", "affectsDamageFight": False}
        # a quote that ends where the sentence does
        d["abilities"]["Q"]["mechanics"] = [dict(mech, quote="disrupts their ongoing channels")]
        # a calculation listed with a dot and the sheet's "(percent)" tag
        d["abilities"]["Q"]["binUnused"].append({"ref": "NullLance.TotalShield (percent)",
                                                 "reason": "defensive"})
        # RiftWalk.BaseCD carries the cooldown the ability states: accounted for
        d["abilities"]["R"]["cooldown"] = {"values": [5, 3.5, 2], "quote": "{{ap|5 to 2}}"}
        d["abilities"]["R"]["binUnused"] = [u for u in d["abilities"]["R"]["binUnused"]
                                            if u["ref"] != "RiftWalk.BaseCD"]
        self.assertEqual(self.errors(d), [])

    def test_pilot_dossiers_still_pass(self):
        for slug in ("kassadin", "kayle", "vladimir", "twitch", "jax"):
            path = os.path.join(ks.DOSSIERS_DIR, f"{slug}.json")
            if not os.path.exists(path):
                self.skipTest("pilot dossiers not present")
            with open(path) as f:
                chk = ks.Checker(slug, json.load(f), ks.load_sources(slug, PATCH))
            chk.run()
            self.assertEqual(chk.errors, [], slug)


class TestRunner(unittest.TestCase):
    def test_prompt_sections_the_runner_sends(self):
        sec = dossier.prompt_sections()
        for name in ("The fight the dossier is for", "Reading the sources", "An ability part",
                     "top.json"):
            self.assertIn(name, sec)
        self.assertIn("ONE ability part", dossier.system_prompt("Q"))
        self.assertIn("needsBespoke", dossier.system_prompt("top"))
        self.assertNotIn("How to work", dossier.system_prompt("Q"))  # the agent protocol

    def test_reply_parsing_and_skeleton(self):
        self.assertEqual(dossier.parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(dossier.parse_json('Here it is: {"a": [1, 2]} done'), {"a": [1, 2]})
        self.assertTrue(dossier.untouched("Q", {"values": [{"role": None}], "mechanics": [],
                                                "binUnused": []}))
        self.assertFalse(dossier.untouched("Q", {"values": [], "filledBy": "sonnet"}))


# ---------------------------------------------------------------------------
# driver tests (from test_kit_driver.py)
# ---------------------------------------------------------------------------

class TestRegistry(unittest.TestCase):
    def test_text(self):
        text = kd.registry_text(["ashe", "jax"])
        self.assertIn("pub mod ashe;\npub mod jax;\n", text)
        self.assertIn('pub const NAMES: [&str; 2] = ["ashe", "jax"];', text)
        self.assertIn('"jax" => jax::GenDriver::new(kit, sheet, level, ranks, prestacked)', text)

    def test_tree_is_in_step(self):
        with open(os.path.join(kd.GENERATED_DIR, "mod.rs")) as f:
            self.assertEqual(f.read(), kd.registry_text(kd.driver_names()))
        # what the engine was built with; the hand-written four stay their own list
        self.assertEqual(sorted(builds.GENERATED_DRIVERS), kd.driver_names())
        self.assertEqual(sorted(builds.KIT_DRIVERS), ["kassadin", "kayle", "twitch", "vladimir"])
        self.assertNotIn("jax", builds.kit_champions())  # unreviewed: not in the dashboard's tiers
        self.assertIn("jax", builds.kit_champions(include_generated=True))


class TestLints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(kd.GENERATED_DIR, "jax.rs")) as f:
            cls.jax_rs = f.read()
        with open(os.path.join(builds.KITS_DIR, "jax.json")) as f:
            kit = json.load(f)
        cls.jax_kit = {k: v for k, v in kit.items() if k not in ("_provenance", "attack")}
        cls.known = kd.source_numbers("jax")

    def test_reference_is_clean(self):
        self.assertEqual(kd.lint_driver(self.jax_rs), [])
        self.assertEqual(kd.lint_kit(self.jax_kit, "jax", self.known), [])

    def test_driver_rules(self):
        def errs(code):
            return " ".join(kd.lint_driver(self.jax_rs + code))
        self.assertIn("game numbers", errs("\nfn f() -> f64 { 0.35 * 185.0 }\n"))
        self.assertNotIn("game numbers", errs("\n// 0.35 of 185 in a comment\n"))
        self.assertIn("pymax", errs("\nfn f(a: f64) -> f64 { a.max(1.0) }\n"))
        self.assertIn("std::", errs("\nfn f() { std::process::exit(1) }\n"))
        self.assertIn("unsafe", errs("\nfn f() { unsafe { } }\n"))
        self.assertIn("import not allowed", errs("\nuse crate::enumerate::Ctx;\n"))
        self.assertIn("reset", " ".join(kd.lint_driver(self.jax_rs.replace("self.s = self.s0;", ""))))

    def test_kit_rules(self):
        def errs(**change):
            return " ".join(kd.lint_kit(dict(self.jax_kit, **change), "jax", self.known))
        self.assertIn('"champion"', errs(champion="jaxx"))
        self.assertIn("maxOrder", errs(maxOrder=["Q", "W"]))
        self.assertIn('"attack" may only be', errs(attack={"windupFraction": 0.2}))
        self.assertEqual(errs(attack={"never": True}), "")
        gen = json.loads(json.dumps(self.jax_kit["gen"]))
        gen["Q"]["damage"]["base"][4] = 231  # not a number any source states
        self.assertIn("gen.Q.damage.base = 231", errs(gen=gen))
        self.assertEqual(errs(gen=gen, assumed=[{"path": "gen.Q.damage.base", "why": "x"}]), "")
        ab = json.loads(json.dumps(self.jax_kit["abilities"]))
        ab["R"]["damage"] = {"base": [1]}
        self.assertIn("abilities.R may hold only", errs(abilities=ab))


class TestReferenceDriver(unittest.TestCase):
    """Jax, hand-computed at level 16 with no items against 0 resists: the
    socket's kit paths, events and hooks all have to work for these to hold."""

    @classmethod
    def setUpClass(cls):
        cls.f = kd.Fights(builds.load_kit("jax"), "jax")

    def sim(self, duration, level=16, items_list=(), hp=100_000, **kw):
        return self.f.run(level, list(items_list), hp, 0, 0, duration, **kw)

    def test_opening(self):
        # R's swing lands with the opening cast: 250 at rank 3, no AP, and nothing else yet
        self.assertEqual(self.sim(0.0)["breakdown"], {"R": 250.0})
        # its 0.25 s cast time keeps Jax busy: Leap Strike (no cast time of its own) waits
        # for it. W > Q > E at level 16 is W 5, Q 5, E 3, so 225, no bonus AD
        self.assertNotIn("Q", self.sim(0.24)["breakdown"])
        self.assertAlmostEqual(self.sim(0.25)["breakdown"]["Q"], 225.0)
        # without the ult nothing holds it
        self.assertAlmostEqual(self.sim(0.0, use_ult=False)["breakdown"]["Q"], 225.0)

    def test_counter_strike_recast(self):
        # cast once the ult's cast time is over (0.25), recast a second later: rank 3's
        # 100 + 4% of the dummy's 100,000
        self.assertNotIn("E", self.sim(1.24)["breakdown"])
        self.assertAlmostEqual(self.sim(1.25)["breakdown"]["E"], 100.0 + 4000.0)
        # its 13 s cooldown starts at the recast: the next one is cast at 14.25, lands at 15.25
        self.assertAlmostEqual(self.sim(15.24)["breakdown"]["E"], 4100.0)
        self.assertAlmostEqual(self.sim(15.25)["breakdown"]["E"], 2 * 4100.0)
        # without the ult: cast at 0, recast at 1
        self.assertNotIn("E", self.sim(0.99, use_ult=False)["breakdown"])
        self.assertAlmostEqual(self.sim(1.0, use_ult=False)["breakdown"]["E"], 4100.0)

    def test_grandmaster_onhit_cadence(self):
        # while the ult runs (8 s) every second attack carries the on-hit, 185 at rank 3
        r = self.sim(3.0)
        self.assertAlmostEqual(r["breakdown"]["R onhit"], 185.0 * (r["attacks"] // 2))
        # without the ult: every third
        r = self.sim(3.0, use_ult=False)
        self.assertAlmostEqual(r["breakdown"]["R onhit"], 185.0 * (r["attacks"] // 3))

    def test_empower_rides_attacks_and_needs_its_rank(self):
        r = self.sim(15.0, use_ult=False)
        self.assertGreater(r["breakdown"]["W"], 0)
        self.assertAlmostEqual(r["breakdown"]["W"] % 190.0, 0.0)  # whole Empowers, rank 5
        r = self.sim(15.0, level=1)  # level 1 learns W only
        self.assertEqual(sorted(r["breakdown"]), ["W", "auto"])
        self.assertAlmostEqual(sum(r["breakdown"].values()), r["total"])

    def test_fight_checks_pass(self):
        with open(os.path.join(ks.DOSSIERS_DIR, "jax.json")) as f:
            d = json.load(f)
        errors, facts = kd.fight_checks(builds.load_kit("jax"), "jax", d)
        self.assertEqual(errors, [])
        self.assertGreater(facts["nakedL16"]["dps"], 100)


class TestCastTimes(unittest.TestCase):
    """The cast-time checks: what the dossier's castTime binds a driver to, the
    opening read off fights of growing length, and the zero-time kill (ten
    machine-written champions killed the squishy at 0.00 s on 2026-09-20: every
    ability resolved in the first instant)."""

    def test_cast_floor_reads_the_dossier_shapes(self):
        floor = kd.cast_floor
        self.assertEqual(floor({"value": 0.25, "quote": "{{fd|0.25}}"}), 0.25)
        for none in (None, {"value": None, "quote": "none"}, {"value": 0, "quote": "None"}):
            self.assertEqual(floor(none), 0.0)
        # a recast's cast time does not bind the first cast; a first form without one is instant
        self.assertEqual(floor({"value": 0.25, "quote":
                                "{{dv|{{tt|{{fd|0.25}}|Initial cast}}|{{tt|None|Recasts}}}}"}), 0.25)
        self.assertEqual(floor({"value": 0.25, "quote":
                                "{{dv|{{tt|None|Active}}|{{tt|{{fd|0.25}}|Recast}}}}"}), 0.0)
        self.assertEqual(floor({"value": 0.5833, "quote":
                                "{{dv|None|{{tt|{{fd|0.5833}}|Starts after the spell is cast}}}}"}), 0.0)
        # one that shrinks with attack speed binds at its shortest; ranges of other things do not count
        self.assertEqual(floor({"value": 0.6, "quote": "{{pp|0.6 to 0.4 for 11|0 to 250|key1=%}}"}), 0.4)
        self.assertEqual(floor({"value": 0.35, "quote": "0.35;0.33;0.297;0.264;0.231;0.198;0.175"}), 0.175)

    def test_cast_floors_fall_back_to_riots_file(self):
        # the wiki says nothing at all for this one (not "none"): Riot's file fills in
        d = {"abilities": {"Q": {"castTime": None}, "W": {}, "E": {}, "R": {}}}
        riot = {"Q": 0.3}
        with mock.patch.object(kd, "sheet_cast_times", lambda slug: riot):
            self.assertEqual(kd.cast_floors({}, d, "x"), {"Q": 0.3})
            # but an explicit "none" from the wiki wins: the ability is instant
            d["abilities"]["Q"]["castTime"] = {"value": None, "quote": "none"}
            self.assertEqual(kd.cast_floors({}, d, "x"), {})
        self.assertEqual(kd.cast_floors({}, d), {})  # no slug: no fallback

    def test_every_stated_cast_time_must_be_carried_and_read(self):
        d = {"abilities": {"Q": {"castTime": {"value": 0.25, "quote": "{{fd|0.25}}"}},
                           "W": {"castTime": None}, "E": {}, "R": {}}}
        rs = 'let q = kit.num("gen.Q.castTimeS")?;'
        self.assertEqual(kd.lint_cast_times({"gen": {"Q": {"castTimeS": 0.25}}}, d, rs, None), [])
        # in the kit but never read: a dead number
        errs = kd.lint_cast_times({"gen": {"Q": {"castTimeS": 0.25}}}, d, "", None)
        self.assertIn("the driver never reads it", errs[0])
        # not in the kit at all: cast for free (Diana's Q, 2026-09-20)
        errs = kd.lint_cast_times({"gen": {}}, d, rs, None)
        self.assertIn("0.25 s cast time and the kit does not carry it", errs[0])
        self.assertIn('"unused"', errs[0])
        # an ability the driver never casts is waived by `unused`
        self.assertEqual(kd.lint_cast_times({"gen": {}, "unused": {"Q": "never cast"}}, d,
                                            rs, None), [])
        # an ability the wiki calls instant is never asked for
        self.assertEqual(kd.lint_cast_times({"gen": {}}, {"abilities": {"W": {"castTime": None}}},
                                            rs, None), [])

    def test_the_admitted_drivers_carry_their_cast_times(self):
        for name in ("jax", "annie", "chogath"):
            with open(os.path.join(ks.DOSSIERS_DIR, f"{name}.json")) as f:
                d = json.load(f)
            with open(os.path.join(kd.GENERATED_DIR, f"{name}.rs")) as f:
                rs = f.read()
            self.assertEqual(kd.lint_cast_times(builds.load_kit(name), d, rs, name), [], name)

    def test_cast_floors_and_the_assumed_waiver(self):
        d = {"abilities": {"Q": {"castTime": {"value": 0.25, "quote": "{{fd|0.25}}"}},
                           "W": {"castTime": {"value": 0.5, "quote": "{{fd|0.5}}"}},
                           "E": {"castTime": None}, "R": {"castTime": {"value": None}}}}
        self.assertEqual(kd.cast_floors({}, d), {"Q": 0.25, "W": 0.5})
        kit = {"gen": {"W": {"castTimeS": 0}}, "assumed": [{"path": "gen.W.castTimeS", "why": "cougar form"}]}
        self.assertEqual(kd.cast_floors(kit, d), {"Q": 0.25})
        kit["assumed"][0].pop("why")  # a waiver needs its reason
        self.assertEqual(kd.cast_floors(kit, d), {"Q": 0.25, "W": 0.5})

    def test_first_landings(self):
        lands = {"R": 0.0, "Q": 0.25, "W mark": 0.5, "E": 0.3, "auto": 0.0, "eclipse": 0.0}
        run = lambda t: {"breakdown": {k: 10.0 for k, at in lands.items() if at <= t}}  # noqa: E731
        # E has no cast time here: not tracked; item and attack sources never count as a slot
        self.assertEqual(kd.first_landings(run, {"Q": 0.25, "W": 0.25, "R": 0.25}),
                         {"R": 0.0, "Q": 0.25, "W": 0.5})

    def test_casts_go_one_after_another(self):
        floors = {"Q": 0.25, "W": 0.25, "R": 0.25}
        errs = kd.cast_time_errors({"Q": 0.0, "W": 0.0, "R": 0.0}, floors, "opening")
        self.assertEqual(len(errs), 1)
        self.assertIn("0.00 s into the fight Q, R, W have all dealt damage", errs[0])
        self.assertIn("take 0.5 s", errs[0])
        self.assertIn("busy_until", errs[0])
        # the hand-written kits' opening: a cast lands as it starts, the next one a cast time later
        self.assertEqual(kd.cast_time_errors({"R": 0.0, "Q": 0.25, "W": 0.5}, floors, ""), [])
        # landing at the end of the cast instead is fine too, and so is a single cast at t=0
        self.assertEqual(kd.cast_time_errors({"R": 0.25, "Q": 0.5, "W": 0.75}, floors, ""), [])
        self.assertEqual(kd.cast_time_errors({"Q": 0.0}, floors, ""), [])
        self.assertEqual(kd.cast_time_errors({}, floors, ""), [])
        # two at once is one too many, however short the cast
        self.assertTrue(kd.cast_time_errors({"Q": 0.1, "W": 0.1}, {"Q": 0.175, "W": 0.5}, ""))
        # damage that lands after a delay says nothing of when it was cast: W cast at 0
        # (0.25 s), Q at 0.25 (0.5 s), W's hit landing at 0.4, inside Q's cast, is legal
        self.assertEqual(kd.cast_time_errors({"Q": 0.25, "W": 0.4}, {"Q": 0.5, "W": 0.25}, ""), [])

    def fake_fights(self, burst):
        """A stand-in engine: `burst` lands in the first instant, 100 damage a
        second of attacks after it."""
        class Fake:
            def __init__(self, kit, slug):
                pass

            def run(self, level, tokens, hp, armor, mr, duration, use_ult=True):
                bd = dict(burst, auto=100.0 * duration + 1.0)
                if tokens:  # items add to everything
                    bd = {k: v * 1.5 for k, v in bd.items()}
                total = sum(bd.values())
                return {"total": total, "dps": total / max(duration, 1.0), "attacks": int(duration),
                        "breakdown": bd, "ttk": 0.0 if sum(burst.values()) * 1.5 >= hp and tokens else None,
                        "hp_left": max(hp - total, 0.0)}
        return Fake

    def test_fight_checks_catch_an_instant_opening(self):
        cast = {"castTime": {"value": 0.25, "quote": "{{fd|0.25}}"},
                "values": [{"role": "damage", "damageType": "magic"}]}
        d = {"abilities": {"Q": cast, "W": cast, "E": {"castTime": None, "values": []}, "R": cast}}
        kit = {"unused": {}}
        with mock.patch.object(kd, "Fights", self.fake_fights({"Q": 300.0, "W": 300.0, "R": 300.0})):
            errors, facts = kd.fight_checks(kit, "jax", d)
        self.assertEqual(facts["castTimes"], {"Q": 0.25, "W": 0.25, "R": 0.25})
        self.assertEqual([e[:26] for e in errors], ["cast times are not respect"])
        # the same opening, big enough to kill: the zero-time kill is reported too
        with mock.patch.object(kd, "Fights", self.fake_fights({"Q": 900.0, "W": 900.0, "R": 900.0})):
            errors, _ = kd.fight_checks(kit, "jax", d)
        self.assertEqual(len(errors), 2)
        self.assertIn("dead at t = 0.00 s", errors[1])
        self.assertIn("AP burst", errors[1])
        # one cast in the first instant is the convention, not a fault
        with mock.patch.object(kd, "Fights", self.fake_fights({"R": 300.0})):
            errors, _ = kd.fight_checks(dict(kit, unused={"Q": "x", "W": "x"}), "jax", d)
        self.assertEqual(errors, [])

    def test_the_hand_written_kits_pass(self):
        # the rule is theirs: their dossiers give the cast times
        for slug in ("kassadin", "vladimir", "twitch", "kayle"):
            with open(os.path.join(ks.DOSSIERS_DIR, f"{slug}.json")) as f:
                d = json.load(f)
            kit = builds.load_kit(slug)
            floors = kd.cast_floors(kit, d)
            f = kd.Fights(kit, slug)
            for ult in (True, False):
                landed = kd.first_landings(
                    lambda t: f.run(16, [], 30_000, 100, 100, t, use_ult=ult), floors)  # noqa: B023
                self.assertEqual(kd.cast_time_errors(landed, floors, slug), [], slug)


class TestRepair(unittest.TestCase):
    def test_as_written_strips_what_the_script_added(self):
        on_disk = {"champion": "x", "notes": ["a", "b"], "manaless": True, "_provenance": ["p"],
                   "attack": {"windupFraction": 0.2, "ranged": True}}
        self.assertEqual(kd.as_written(on_disk), {"champion": "x", "notes": ["a", "b"]})
        on_disk["attack"]["never"] = True
        self.assertEqual(kd.as_written(on_disk)["attack"], {"never": True})

    def test_retry_prompt_carries_the_attempt_and_the_history(self):
        text = kd.retry_prompt("jax", "jax", {"champion": "jax"}, "// driver\n", "PROBLEMS",
                               ["as admitted: old", "round 1: newer"])
        self.assertIn('"champion": "jax"', text)
        self.assertIn("// driver", text)
        self.assertIn("## What is wrong with it\n\nPROBLEMS", text)
        self.assertIn("as admitted: old", text)
        self.assertNotIn("round 1: newer", text)  # the last round is the problems themselves


class TestRosterEnumerates(unittest.TestCase):
    """A machine-written driver under the enumerator (one Sim per boots class,
    reset between fights), on a tiny item pool in a temp cache dir: the cells
    land, and the dashboard's champion list has the hand-encoded ones first."""

    def test_compute_tier(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        tiny = [3089, 3135, 4645, 6653, 4633, 3115, 3042]
        with mock.patch.object(builds, "SCENARIO_CACHE_DIR", tmp), \
                mock.patch.object(builds, "DEFAULT_POOL", tiny):
            champs = builds.damage_champions()
            self.assertEqual(champs[:4], builds.kit_champions())
            self.assertIn("jax", champs)
            paths = builds.cell_paths()
            tier = builds.SCENARIOS["full-squishy"]["tier"]
            outs = builds.compute_tier("jax", tier, paths)
            cell = outs["full-squishy"]
            self.assertGreater(cell["buildsEvaluated"], 0)
            self.assertTrue(cell["rows"])
            self.assertTrue(os.path.exists(paths[("jax", "full-squishy")]))
        meta = {c["slug"]: c for c in builds.api_builds_meta()["champions"]}
        self.assertTrue(meta["jax"]["generated"])
        self.assertFalse(meta["jax"]["reviewed"])
        self.assertFalse(meta["kayle"]["generated"])
        self.assertTrue(meta["kayle"]["reviewed"])


class TestVarusDriver(unittest.TestCase):
    """Patch 16.21 Varus kit, dossier, Blight refunds, and adaptive Q charge."""

    @classmethod
    def setUpClass(cls):
        cls.champ = builds.load_champion("varus")
        cls.kit = builds.load_kit("varus")
        _, cls.pool = builds.load_items()
        cls.idx = builds.item_index(cls.pool)
        cls.effects = builds.load_item_effects()
        cls.ranks16 = builds.skill_ranks(16, builds.kit_max_order(cls.kit))

    def sim(self, tokens, hp, armor, mr, duration, level=16, kit=None, use_ult=True, bonus_hp=0.0):
        kit = kit or self.kit
        ids = [builds.resolve_item(self.pool, self.idx, t) for t in tokens]
        ranks = builds.skill_ranks(level, builds.kit_max_order(kit))
        sh = builds.resolve_stats(self.champ, level, ids, self.pool, self.effects, kit=kit)
        fx = builds.merge_effects(ids, self.effects)
        return builds.simulate(sh, kit, fx, level, ranks, hp, armor, mr, duration,
                               use_ult=use_ult, target_bonus_hp=bonus_hp)

    def test_sources_lints_and_fight_checks(self):
        with open(os.path.join(ks.DOSSIERS_DIR, "varus.json")) as f:
            d = json.load(f)
        chk = ks.Checker("varus", d, ks.load_sources("varus", "16.21"))
        chk.run()
        self.assertEqual(chk.errors, [])
        with open(os.path.join(kd.GENERATED_DIR, "varus.rs")) as f:
            rs = f.read()
        self.assertTrue(self.kit["reviewed"])
        meta = {c["slug"]: c for c in builds.api_builds_meta()["champions"]}
        self.assertTrue(meta["varus"]["reviewed"])
        self.assertEqual(kd.lint_kit(kd.as_written(self.kit), "varus", kd.source_numbers("varus"),
                                     allow_reviewed=True), [])
        self.assertEqual(kd.lint_driver(rs), [])
        self.assertEqual(kd.lint_cast_times(self.kit, d, rs, "varus"), [])
        errors, _ = kd.fight_checks(self.kit, "varus", d)
        self.assertEqual(errors, [])

    def test_max_order_and_unranked_w_at_level_1(self):
        self.assertEqual(builds.kit_max_order(self.kit), ("Q", "W", "E"))
        self.assertEqual(self.ranks16, {"Q": 5, "W": 5, "E": 3, "R": 3})
        r1 = self.sim([], 3000, 50, 50, 10.0, level=1, use_ult=False)
        self.assertNotIn("W onhit", r1["breakdown"])
        self.assertNotIn("W deton Q", r1["breakdown"])
        self.assertGreater(r1["breakdown"].get("Q", 0.0), 0.0)

    def test_blight_refunds_q_and_w_active_cooldowns(self):
        # Naked L16 has 12s Q CD (0 haste): Q #1 has not yet released at 5.0s,
        # so landing 2 full-charge Qs (425 dmg) by 14.0s (<9s span) proves Q self-refunds.
        self.assertEqual(self.sim([], 30_000, 100, 100, 5.0, use_ult=False)["breakdown"].get("Q", 0.0), 0.0)
        naked_14s = self.sim([], 30_000, 100, 100, 14.0, use_ult=False)
        self.assertAlmostEqual(naked_14s["breakdown"]["Q"], 425.0, places=3)
        # W active has a 40s base CD (29.6s at 35 haste): dealing more W-active
        # damage by 15s than by 6s proves Blight refunded W active for a 2nd cast.
        haste_items = ["ionian boots of lucidity", "spear of shojin"]
        w_6s = self.sim(haste_items, 10_000, 100, 100, 6.0)["breakdown"]["W active"]
        w_15s = self.sim(haste_items, 10_000, 100, 100, 15.0)["breakdown"]["W active"]
        self.assertGreater(w_6s, 0.0)
        self.assertGreater(w_15s, w_6s * 1.5)

    def test_adaptive_q_taps_on_high_as_and_charges_on_lethality(self):
        tap_kit = copy.deepcopy(self.kit)
        tap_kit["gen"]["Q"]["chargeDurationS"] = 0.0

        # High-AS on-hit vs squishy: adaptive Q taps (0s hold) and matches pure tap.
        onhit = ["berserkers", "blade of the ruined king", "guinsoos rageblade",
                 "terminus", "wits end", "kraken slayer"]
        r_onhit = self.sim(onhit, 2800, 110, 60, 8.0, bonus_hp=200)
        r_onhit_tap = self.sim(onhit, 2800, 110, 60, 8.0, kit=tap_kit, bonus_hp=200)
        self.assertAlmostEqual(r_onhit["ttk_eff"], r_onhit_tap["ttk_eff"], places=6)

        # Low-AS lethality vs bruiser: adaptive Q charges for 1.7x and beats tap-only by >1s.
        leth = ["ionian boots of lucidity", "youmuus ghostblade", "opportunity",
                "seryldas grudge", "edge of night", "hubris"]
        r_leth = self.sim(leth, 3800, 180, 120, 12.0, bonus_hp=900)
        r_leth_tap = self.sim(leth, 3800, 180, 120, 12.0, kit=tap_kit, bonus_hp=900)
        self.assertLess(r_leth["ttk_eff"] + 1.0, r_leth_tap["ttk_eff"])

        # Early lethal release vs squishy: lethality releases after 1 step (0.125s)
        # instead of overcharging to 1.25s, matching a k<=1 capped kit.
        cap1_kit = copy.deepcopy(self.kit)
        cap1_kit["gen"]["Q"]["chargeDurationS"] = 0.125
        r_leth_sq = self.sim(leth, 2800, 110, 60, 8.0, bonus_hp=200)
        r_leth_sq_c1 = self.sim(leth, 2800, 110, 60, 8.0, kit=cap1_kit, bonus_hp=200)
        self.assertAlmostEqual(r_leth_sq["ttk_eff"], r_leth_sq_c1["ttk_eff"], places=6)


class TestKaisaDriver(unittest.TestCase):
    """Patch 16.21 Kai'Sa kit, dossier, Living Weapon evolutions, Plasma, and adaptive E."""

    @classmethod
    def setUpClass(cls):
        cls.champ = builds.load_champion("kaisa")
        cls.kit = builds.load_kit("kaisa")
        _, cls.pool = builds.load_items()
        cls.idx = builds.item_index(cls.pool)
        cls.effects = builds.load_item_effects()
        cls.ranks16 = builds.skill_ranks(16, builds.kit_max_order(cls.kit))

    def sim(self, tokens, hp, armor, mr, duration, level=16, kit=None, use_ult=True,
            bonus_hp=0.0, ranks=None):
        kit = kit or self.kit
        ids = [builds.resolve_item(self.pool, self.idx, t) for t in tokens]
        ranks = ranks or builds.skill_ranks(level, builds.kit_max_order(kit))
        sh = builds.resolve_stats(self.champ, level, ids, self.pool, self.effects, kit=kit)
        fx = builds.merge_effects(ids, self.effects)
        return builds.simulate(sh, kit, fx, level, ranks, hp, armor, mr, duration,
                               use_ult=use_ult, target_bonus_hp=bonus_hp)

    def test_sources_lints_and_fight_checks(self):
        with open(os.path.join(ks.DOSSIERS_DIR, "kaisa.json")) as f:
            d = json.load(f)
        chk = ks.Checker("kaisa", d, ks.load_sources("kaisa", "16.21"))
        chk.run()
        self.assertEqual(chk.errors, [])
        with open(os.path.join(kd.GENERATED_DIR, "kaisa.rs")) as f:
            rs = f.read()
        self.assertTrue(self.kit["reviewed"])
        meta = {c["slug"]: c for c in builds.api_builds_meta()["champions"]}
        self.assertTrue(meta["kaisa"]["reviewed"])
        self.assertEqual(kd.lint_kit(kd.as_written(self.kit), "kaisa", kd.source_numbers("kaisa"),
                                     allow_reviewed=True), [])
        self.assertEqual(kd.lint_driver(rs), [])
        self.assertEqual(kd.lint_cast_times(self.kit, d, rs, "kaisa"), [])
        errors, _ = kd.fight_checks(self.kit, "kaisa", d)
        self.assertEqual(errors, [])

    def test_max_order_and_level_1_skills(self):
        self.assertEqual(builds.kit_max_order(self.kit), ("Q", "E", "W"))
        self.assertEqual(self.ranks16, {"Q": 5, "E": 5, "W": 3, "R": 3})
        r1 = self.sim([], 3000, 50, 50, 5.0, level=1, use_ult=False)
        self.assertNotIn("W", r1["breakdown"])
        self.assertGreater(r1["breakdown"].get("Q", 0.0), 0.0)
        self.assertGreater(r1["breakdown"].get("P Caustic Wounds", 0.0), 0.0)

    def test_living_weapon_evolutions_and_discrete_q_missiles(self):
        # Naked L16 has 37.635 AD from growth (< 100): Q fires 6 missiles across [0.4s, 1.4s]
        # for 100 + 5 * 25 = 225 physical damage on a 0-armor dummy.
        self.assertEqual(self.sim([], 30_000, 0, 0, 0.39, use_ult=False)["breakdown"].get("Q", 0.0), 0.0)
        self.assertAlmostEqual(self.sim([], 30_000, 0, 0, 0.41, use_ult=False)["breakdown"]["Q"],
                               100.0, places=4)
        self.assertAlmostEqual(self.sim([], 30_000, 0, 0, 1.39, use_ult=False)["breakdown"]["Q"],
                               200.0, places=4)
        self.assertAlmostEqual(self.sim([], 30_000, 0, 0, 1.41, use_ult=False)["breakdown"]["Q"],
                               225.0, places=4)

        # Muramana grants 73.48 bonus AD at L16 -> 73.48 + 37.635 = 111.115 >= 100:
        # Q evolves dynamically to 12 missiles (3.75x single-missile damage).
        mura_id = [builds.resolve_item(self.pool, self.idx, "muramana")]
        sh_mura = builds.resolve_stats(self.champ, 16, mura_id, self.pool, self.effects, kit=self.kit)
        expected_q12 = (100.0 + 0.55 * sh_mura["ad_bonus"]) * 3.75
        self.assertAlmostEqual(
            self.sim(["muramana"], 30_000, 0, 0, 1.5, use_ult=False)["breakdown"]["Q"],
            expected_q12, places=4)

        # Blackfire Torch has 80 flat AP (83.2 in-combat AP < 100 permanent AP): W stays
        # unevolved (1 cast in 6s), whereas Shadowflame (110 AP >= 100) evolves W and
        # refunds 75% cooldown on champion hit to land a 2nd W within 6s.
        w_bft = self.sim(["blackfire torch"], 30_000, 0, 0, 6.0, use_ult=False)["breakdown"]["W"]
        w_sf_1 = self.sim(["shadowflame"], 30_000, 0, 0, 2.0, use_ult=False)["breakdown"]["W"]
        w_sf_2 = self.sim(["shadowflame"], 30_000, 0, 0, 6.0, use_ult=False)["breakdown"]["W"]
        self.assertLess(w_bft, 300.0)
        self.assertAlmostEqual(w_sf_2, w_sf_1 * 2.0, places=4)

    def test_plasma_pre_attack_detonation_and_defended_spell_shield(self):
        # At L1 (AD=59, P base=4, perStack=1, exec=15%) with Q zeroed, 5 autos deal
        # 4+5+6+7+8 = 30 Caustic Wounds and detonate 15% of 266 = 39.9 missing HP
        # BEFORE Auto #5's 59 physical damage (rather than 48.75 post-auto).
        zero_q = copy.deepcopy(self.kit)
        zero_q["gen"]["Q"]["missileDamage"] = {"base": [0, 0, 0, 0, 0], "bonusAd": 0, "ap": 0}
        r5 = self.sim([], 1000, 0, 0, 6.3, level=1, kit=zero_q, use_ult=False)
        self.assertEqual(r5["attacks"], 5)
        self.assertAlmostEqual(r5["breakdown"]["auto"], 295.0, places=4)
        self.assertAlmostEqual(r5["breakdown"]["P Caustic Wounds"], 30.0, places=4)
        self.assertAlmostEqual(r5["breakdown"]["P Detonation"], 39.9, places=4)

        # Against a Banshee's Veil spell shield with Q unranked (Q=0, W=5, E=0, R=0),
        # Evolved W's first bolt is completely blocked: no W damage, no Plasma stacks
        # from W, and no 75% cooldown refund (so W cannot recast by 6.0s).
        mundo = builds.load_champion("drmundo")
        mundo_kit = builds.load_kit("drmundo")
        mundo_ranks = builds.skill_ranks(16, builds.kit_max_order(mundo_kit))
        sf_ids = [builds.resolve_item(self.pool, self.idx, "shadowflame")]
        att_w_only = dict(
            sheet=builds.resolve_stats(self.champ, 16, sf_ids, self.pool, self.effects, kit=self.kit),
            kit=self.kit, fx=builds.merge_effects(sf_ids, self.effects), level=16,
            ranks={"Q": 0, "W": 5, "E": 0, "R": 0}, duration=6.0)
        bv_ids = [builds.resolve_item(self.pool, self.idx, "banshees veil")]
        rep_bv = builds.survive(mundo, mundo_kit, 16, mundo_ranks, bv_ids, self.pool,
                                self.effects, att_w_only)
        self.assertNotIn("W", rep_bv["breakdown"])
        self.assertGreater(rep_bv["defense"]["blocked"], 0.0)

    def test_killer_instinct_attack_reset_and_adaptive_supercharge(self):
        # Killer Instinct resets the basic attack timer after Plasma lands, adding an
        # extra attack within 2.0s without delaying Auto #1 at t = 0.
        items = ["kraken slayer", "guinsoos rageblade"]
        self.assertEqual(self.sim(items, 30_000, 100, 100, 0.05, use_ult=True)["attacks"], 1)
        r_no_r = self.sim(items, 30_000, 100, 100, 2.0, use_ult=False)
        r_with_r = self.sim(items, 30_000, 100, 100, 2.0, use_ult=True)
        self.assertGreater(r_with_r["attacks"], r_no_r["attacks"])
        self.assertGreater(r_with_r["total"], r_no_r["total"])

        # Supercharge skips charging when already AS-capped or on 1-swing lethal targets,
        # while charging on sustained sub-cap fights to gain attacks and total damage.
        capped = ["berserkers greaves", "guinsoos rageblade", "nashors tooth",
                  "kraken slayer", "wits end", "phantom dancer"]
        no_e_ranks = {"Q": 5, "W": 3, "E": 0, "R": 3}
        self.assertAlmostEqual(
            self.sim(capped, 10_000, 100, 100, 8.0)["total"],
            self.sim(capped, 10_000, 100, 100, 8.0, ranks=no_e_ranks)["total"],
            places=6)
        self.assertAlmostEqual(
            self.sim(["kraken slayer"], 400, 50, 50, 8.0)["ttk_eff"],
            self.sim(["kraken slayer"], 400, 50, 50, 8.0, ranks=no_e_ranks)["ttk_eff"],
            places=6)
        r_sust = self.sim(["kraken slayer", "infinity edge"], 8000, 100, 100, 10.0)
        r_sust_no_e = self.sim(["kraken slayer", "infinity edge"], 8000, 100, 100, 10.0,
                               ranks=no_e_ranks)
        self.assertGreater(r_sust["attacks"], r_sust_no_e["attacks"])
        self.assertGreater(r_sust["total"], r_sust_no_e["total"])

    def test_spellblade_r_lookahead_with_ready_w(self):
        sq = builds.SCENARIOS["full-squishy"]
        br = builds.SCENARIOS["full-bruiser"]

        # Nashor's + Dusk and Dawn fires R at t = 0 on squishy because Auto #2 + ready W
        # executes before Sheen's 1.5s ICD, while still holding R on bruiser for a 2nd proc.
        nash_dusk = ["sorcerers shoes", "nashors tooth", "rabadons deathcap",
                     "void staff", "shadowflame", "dusk and dawn"]
        r_sq = self.sim(nash_dusk, sq["targetHp"], sq["armor"], sq["mr"], sq["duration"],
                        bonus_hp=sq["targetBonusHp"])
        r_sq_no_r = self.sim(nash_dusk, sq["targetHp"], sq["armor"], sq["mr"], sq["duration"],
                             use_ult=False, bonus_hp=sq["targetBonusHp"])
        self.assertLess(r_sq["ttk"], 0.55)
        self.assertLess(r_sq["ttk"], r_sq_no_r["ttk"] - 0.35)

        r_br = self.sim(nash_dusk, br["targetHp"], br["armor"], br["mr"], br["duration"],
                        bonus_hp=br["targetBonusHp"])
        self.assertLess(r_br["ttk"], 2.20)

        # Lich Bane also fires R at t = 0 on squishy so Auto #2 + Evolved W detonates 5th-stack Plasma.
        nash_lich = ["sorcerers shoes", "nashors tooth", "rabadons deathcap",
                     "void staff", "shadowflame", "lich bane"]
        r_lich_sq = self.sim(nash_lich, sq["targetHp"], sq["armor"], sq["mr"], sq["duration"],
                             bonus_hp=sq["targetBonusHp"])
        self.assertLess(r_lich_sq["ttk"], 0.56)

        # Gunblade + Dusk and Dawn executes at 0.40s via Q + W without delaying W for a reset swing.
        gun_dusk = ["sorcerers shoes", "rabadons deathcap", "void staff",
                    "shadowflame", "dusk and dawn", "hextech gunblade"]
        r_gun_sq = self.sim(gun_dusk, sq["targetHp"], sq["armor"], sq["mr"], sq["duration"],
                            bonus_hp=sq["targetBonusHp"])
        self.assertAlmostEqual(r_gun_sq["ttk"], 0.40, places=4)

    def test_audit_edge_cases_e_buff_r_stasis_and_item_lookahead(self):
        # 1. Supercharge (E) never overwrites an active 4.0s Supercharge buff even when
        # cooldown reduction brings E off cooldown mid-window.
        short_e_kit = copy.deepcopy(self.kit)
        short_e_kit["abilities"]["E"]["cooldownS"] = [2, 2, 2, 2, 2]
        e_only = {"Q": 0, "W": 0, "E": 5, "R": 0}
        r_short = self.sim(["berserkers greaves"], 20_000, 100, 100, 4.6,
                           kit=short_e_kit, use_ult=False, ranks=e_only)
        r_norm = self.sim(["berserkers greaves"], 20_000, 100, 100, 4.6,
                          use_ult=False, ranks=e_only)
        self.assertEqual(r_short["attacks"], r_norm["attacks"])
        self.assertAlmostEqual(r_short["total"], r_norm["total"], places=6)

        # 2. Killer Instinct (R) does not fire into defender stasis when an attack
        # triggers Zhonya's Hourglass at t = 0.0 (verified via Fiendhunter barrage == 0
        # on the post-stasis finishing auto at t = 2.5s).
        mundo = copy.deepcopy(builds.load_champion("drmundo"))
        mundo["dd"]["stats"]["hp"] = 360
        mundo["dd"]["stats"]["hpperlevel"] = 0
        mundo_kit = copy.deepcopy(builds.load_kit("drmundo"))
        mundo_kit["abilities"]["R"]["cooldownS"] = [999, 999, 999]
        att_ids = [builds.resolve_item(self.pool, self.idx, n) for n in [
            "infinity edge", "lord dominiks regards", "yun tal wildarrows",
            "fiendhunter bolts", "experimental hexplate",
        ]]
        att = dict(
            sheet=builds.resolve_stats(self.champ, 16, att_ids, self.pool, self.effects, kit=self.kit),
            kit=self.kit, fx=builds.merge_effects(att_ids, self.effects),
            level=16, ranks={"Q": 0, "W": 0, "E": 0, "R": 3}, duration=8.0,
        )
        zh_ids = [builds.resolve_item(self.pool, self.idx, "zhonyas hourglass")]
        rep_zh = builds.survive(mundo, mundo_kit, 1, {"Q": 5, "W": 0, "E": 5, "R": 0},
                                zh_ids, self.pool, self.effects, att, thresholds=[])
        self.assertAlmostEqual(rep_zh["defense"]["zhonyaAt"], 0.0, places=6)
        self.assertAlmostEqual(rep_zh["ttk"], 2.5, places=6)
        self.assertEqual(rep_zh["breakdown"].get("barrage", 0.0), 0.0)

        # 3. Lookahead counts opening Q volleys plus Muramana, Luden's, and Horizon Focus
        # so Supercharge skips its charge disarm when the combo already kills by 1.40s.
        combo = ["muramana", "ludens echo", "horizon focus"]
        r_no_e = self.sim(combo, 1718, 60, 60, 8.0, use_ult=False,
                          ranks={"Q": 5, "W": 3, "E": 0, "R": 0})
        r_with_e = self.sim(combo, 1718, 60, 60, 8.0, use_ult=False,
                            ranks={"Q": 5, "W": 3, "E": 5, "R": 0})
        self.assertAlmostEqual(r_no_e["ttk"], 1.40, places=4)
        self.assertAlmostEqual(r_with_e["ttk"], 1.40, places=4)


class TestApheliosDriver(unittest.TestCase):
    """Patch 16.22 Aphelios kit: the five weapons and their Moonlight, Phase,
    Weapon Master's statistics, and the woven rotation. Numbers are worked out
    by hand for a level 16 Aphelios with no items against a dummy with no
    resists unless a test says otherwise."""

    CALIBRUM, SEVERUM, GRAVITUM, INFERNUM, CRESCENDUM = range(5)
    # 55 + 2.3 x growth(16), plus Weapon Master's 6 ranks of attack damage
    AD = 55 + 2.3 * 14.475 + 24
    # 2.1% x growth(16) from levels, plus 4 ranks of attack speed
    BONUS_AS = 2.1 * 14.475 + 36
    PERIOD = 1 / (0.665 + 0.658 * BONUS_AS / 100)

    @classmethod
    def setUpClass(cls):
        cls.champ = builds.load_champion("aphelios")
        cls.kit = builds.load_kit("aphelios")
        _, cls.pool = builds.load_items()
        cls.idx = builds.item_index(cls.pool)
        cls.effects = builds.load_item_effects()

    def variant(self, main=None, off=None, **paths):
        """The kit with another starting loadout and/or other numbers
        (`gen_P_moonlight_capacity=14` sets gen.P.moonlight.capacity)."""
        kit = copy.deepcopy(self.kit)
        if main is not None:
            kit["gen"]["P"]["loadout"] = {"main": main, "off": off,
                                          "queue": [w for w in range(5) if w not in (main, off)]}
        for path, value in paths.items():
            node, *keys = kit, *path.split("_")
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = value
        return kit

    def sim(self, tokens, duration, hp=100_000, armor=0, mr=0, level=16, kit=None, use_ult=True,
            extra_lethality=0.0, bonus_hp=0.0):
        kit = kit or self.kit
        ids = [builds.resolve_item(self.pool, self.idx, t) for t in tokens]
        ranks = builds.skill_ranks(level, builds.kit_max_order(kit))
        sh = builds.resolve_stats(self.champ, level, ids, self.pool, self.effects, kit=kit)
        sh["lethality"] += extra_lethality
        fx = builds.merge_effects(ids, self.effects)
        return builds.simulate(sh, kit, fx, level, ranks, hp, armor, mr, duration,
                               use_ult=use_ult, target_bonus_hp=bonus_hp)

    def test_sources_lints_and_fight_checks(self):
        with open(os.path.join(ks.DOSSIERS_DIR, "aphelios.json")) as f:
            d = json.load(f)
        chk = ks.Checker("aphelios", d, ks.load_sources("aphelios", "16.22"))
        chk.run()
        self.assertEqual(chk.errors, [])
        with open(os.path.join(kd.GENERATED_DIR, "aphelios.rs")) as f:
            rs = f.read()
        self.assertTrue(self.kit["reviewed"])
        meta = {c["slug"]: c for c in builds.api_builds_meta()["champions"]}
        self.assertTrue(meta["aphelios"]["reviewed"])
        self.assertEqual(kd.lint_kit(kd.as_written(self.kit), "aphelios", kd.source_numbers("aphelios"),
                                     allow_reviewed=True), [])
        self.assertEqual(kd.lint_driver(rs), [])
        self.assertEqual(kd.lint_cast_times(self.kit, d, rs, "aphelios"), [])
        errors, _ = kd.fight_checks(self.kit, "aphelios", d)
        self.assertEqual(errors, [])

    def test_weapon_master_ranks_follow_the_max_order(self):
        # Q, W and E rank attack damage, attack speed and lethality; a rank
        # opens at levels 1, 2, 3, 5, 7, 9 and every level grants one point
        self.assertEqual(builds.kit_max_order(self.kit), ("Q", "E", "W"))
        table = {"Q": "adRanksByLevel", "W": "asRanksByLevel", "E": "lethalityRanksByLevel"}
        gate, ranks, by_level = [1, 2, 3, 5, 7, 9], dict.fromkeys("QWE", 0), {s: [] for s in "QWE"}
        for level in range(1, 19):
            for slot in self.kit["maxOrder"]:
                if ranks[slot] < 6 and level >= gate[ranks[slot]]:
                    ranks[slot] += 1
                    break
            for slot in "QWE":
                by_level[slot].append(ranks[slot])
        wm = self.kit["gen"]["P"]["weaponMaster"]
        for slot, key in table.items():
            self.assertEqual(wm[key], by_level[slot], key)
        self.assertEqual([wm[key][15] for key in table.values()], [6, 4, 6])  # level 16: AD, AS, lethality

        # the attack damage is in the attack, the attack speed in its cadence
        self.assertAlmostEqual(self.sim([], 0.0, use_ult=False)["breakdown"]["auto"], self.AD, places=6)
        self.assertEqual(self.sim([], self.PERIOD - 0.01, use_ult=False)["attacks"], 1)
        self.assertEqual(self.sim([], self.PERIOD + 0.01, use_ult=False)["attacks"], 2)
        # 27 lethality: 27 armor is no armor, 127 armor is 100
        auto = lambda **kw: self.sim([], 0.0, use_ult=False, **kw)["breakdown"]["auto"]  # noqa: E731
        self.assertAlmostEqual(auto(armor=27), self.AD, places=6)
        self.assertAlmostEqual(auto(armor=127), self.AD / 2, places=6)
        # level 9: attack damage maxed, 3 ranks of lethality (13.5), no attack speed yet
        ad9 = 55 + 2.3 * builds.growth(9) + 24
        self.assertAlmostEqual(auto(level=9, armor=13.5), ad9, places=6)
        self.assertAlmostEqual(auto(level=9, armor=113.5), ad9 / 2, places=6)
        period9 = 1 / (0.665 + 0.658 * 2.1 * builds.growth(9) / 100)
        self.assertEqual(self.sim([], period9 - 0.01, level=9, use_ult=False)["attacks"], 1)
        self.assertEqual(self.sim([], period9 + 0.01, level=9, use_ult=False)["attacks"], 2)

    def test_lethality_reaches_every_physical_source_like_sheet_lethality(self):
        # The passive's lethality is applied by the driver. The reference is the
        # engine's own chain: the same 27 lethality on the stat sheet, none in the kit.
        on_sheet = self.variant(gen_P_weaponMaster_lethalityPerRank=0)
        crit = ["berserkers greaves", "infinity edge", "yun tal wildarrows", "lord dominiks regards",
                "kraken slayer", "blade of the ruined king"]
        opener = ["ionian boots of lucidity", "youmuus ghostblade", "profane hydra", "seryldas grudge",
                  "umbral glaive", "edge of night"]
        for items in (crit, opener, []):
            for armor in (20, 110, 220):
                mine = self.sim(items, 10.0, hp=30_000, armor=armor, mr=60)
                ref = self.sim(items, 10.0, hp=30_000, armor=armor, mr=60, kit=on_sheet,
                               extra_lethality=27.0)
                self.assertEqual(mine["attacks"], ref["attacks"])
                for source, dmg in ref["breakdown"].items():
                    self.assertAlmostEqual(mine["breakdown"][source], dmg, delta=1e-6 * max(dmg, 1.0),
                                           msg=f"{source} at {armor} armor")
        # Black Cleaver and Terminus move the armor inside an attack: within a tenth of a percent
        shred = ["berserkers greaves", "black cleaver", "terminus", "kraken slayer",
                 "blade of the ruined king", "eclipse"]
        mine = self.sim(shred, 10.0, hp=30_000, armor=110, mr=60)
        ref = self.sim(shred, 10.0, hp=30_000, armor=110, mr=60, kit=on_sheet, extra_lethality=27.0)
        self.assertAlmostEqual(mine["total"], ref["total"], delta=1e-3 * ref["total"])

    def test_opening_weaves_moonlight_vigil_and_moonshot_after_attacks(self):
        r_blast, moonshot, mark = 225 + 0.2 * 24, 170 + 0.6 * 24, 15 + 0.15 * 24
        # the first attack lands at once, and Moonlight Vigil is cast in its shadow
        opening = self.sim([], 0.0)["breakdown"]
        self.assertEqual(sorted(opening), ["R", "auto"])
        self.assertAlmostEqual(opening["auto"], self.AD, places=6)
        self.assertAlmostEqual(opening["R"], r_blast, places=6)
        # its locked-on attack lands 0.3 s later: 100% AD, no critical strike chance yet
        self.assertNotIn("R attack", self.sim([], 0.29)["breakdown"])
        self.assertAlmostEqual(self.sim([], 0.30)["breakdown"]["R attack"], self.AD, places=6)
        # the 0.6 s cast ends before the second attack is due, which consumes the
        # empowered mark (15 + 15% bonus AD, plus rank 3's 110); Moonshot follows it
        before, after = self.sim([], self.PERIOD - 0.01), self.sim([], self.PERIOD + 0.01)
        self.assertEqual((before["attacks"], after["attacks"]), (1, 2))
        self.assertNotIn("Q Moonshot", before["breakdown"])
        self.assertAlmostEqual(after["breakdown"]["P Calibrum mark"], mark + 110, places=6)
        self.assertAlmostEqual(after["breakdown"]["Q Moonshot"], moonshot, places=6)
        # Moonshot's own mark is an ordinary one, consumed by the third attack
        third = self.sim([], 2 * self.PERIOD + 0.01)
        self.assertAlmostEqual(third["breakdown"]["P Calibrum mark"], 2 * mark + 110, places=6)
        # without the ult Moonshot goes out after the first attack instead
        no_ult = self.sim([], 0.0, use_ult=False)["breakdown"]
        self.assertEqual(sorted(no_ult), ["Q Moonshot", "auto"])
        # the locked-on attack never rolls a crit: 1 + 0.3 x chance x (crit damage - 1)
        ie = self.sim(["infinity edge"], 0.30)
        crit_damage = builds.resolve_stats(self.champ, 16, [builds.resolve_item(self.pool, self.idx,
                                                                              "infinity edge")],
                                           self.pool, self.effects, kit=self.kit)["crit_damage"] / 100
        self.assertAlmostEqual(ie["breakdown"]["R attack"],
                               (self.AD + 75) * (1 + 0.3 * 0.25 * (crit_damage - 1)), places=6)
        # level 1 has neither a Q nor an ult, level 2 has the Q
        self.assertEqual(sorted(self.sim([], 5.0, level=1)["breakdown"]), ["auto"])
        self.assertIn("Q Moonshot", self.sim([], 5.0, level=2)["breakdown"])

    def test_onslaught_shots_and_quarter_onhits(self):
        shot = 0.41 * self.AD
        # Phase brings Severum up after Moonshot; Onslaught follows the third attack:
        # floor(6 x (1 + 0.33334 x 66.4%)) = 7 shots over 1.75 s, the first at the cast
        cast = 2 * self.PERIOD
        self.assertAlmostEqual(self.sim([], cast + 0.01)["breakdown"]["Q Onslaught"], shot, places=6)
        done = self.sim([], cast + 1.74)
        self.assertAlmostEqual(done["breakdown"]["Q Onslaught"], 7 * shot, places=6)
        # no attack during it; the next one lands as it ends, and takes the mark
        # its Calibrum shots left
        self.assertEqual(done["attacks"], 3)
        after = self.sim([], cast + 1.76)
        self.assertEqual(after["attacks"], 4)
        self.assertGreater(after["breakdown"]["P Calibrum mark"], done["breakdown"]["P Calibrum mark"])
        # Wit's End: 50% more attack speed makes it 8 shots, each with a quarter
        # of the 45 on-hit; three attacks and the locked-on attack apply it whole
        period = 1 / (0.665 + 0.658 * (self.BONUS_AS + 50) / 100)
        wits = self.sim(["wits end"], 2 * period + 1.74)
        self.assertEqual(wits["attacks"], 3)
        self.assertAlmostEqual(wits["breakdown"]["Q Onslaught"], 8 * shot, places=6)
        self.assertAlmostEqual(wits["breakdown"]["onhit"], 4 * 45 + 8 * 0.25 * 45, places=6)

    def test_onslaught_is_skipped_when_attacks_are_worth_more(self):
        tank = builds.SCENARIOS["full-tank"]
        fight = dict(hp=tank["targetHp"], armor=tank["armor"], mr=tank["mr"],
                     bonus_hp=tank["targetBonusHp"])
        # a crit build: nine shots of 41% AD beat the three attacks they replace
        crit = ["berserkers greaves", "infinity edge", "yun tal wildarrows", "lord dominiks regards",
                "the collector", "bloodthirster"]
        with_q = self.sim(crit, 15.0, **fight)
        self.assertGreater(with_q["breakdown"]["Q Onslaught"], 0.0)
        never = self.variant(gen_Q_onslaught_adRatio=[0.0] * 18, gen_Q_onslaught_onHitEffectiveness=0.0)
        without_q = self.sim(crit, 15.0, kit=never, **fight)
        self.assertNotIn("Q Onslaught", without_q["breakdown"])
        self.assertLess(with_q["ttk"], without_q["ttk"])
        # an on-hit build at the attack speed cap: a quarter of its on-hits per
        # shot is a loss, so Severum only attacks
        onhit = ["berserkers greaves", "blade of the ruined king", "guinsoos rageblade", "kraken slayer",
                 "nashors tooth", "statikk shiv"]
        self.assertNotIn("Q Onslaught", self.sim(onhit, 15.0, hp=30_000, armor=220, mr=160)["breakdown"])

    def test_moonlight_vigil_takes_its_best_weapon(self):
        blast = 225 + 0.2 * 24
        # Infernum main: its bonus blast (rank 3: 150 + 25% bonus AD) and a 110% AD attack
        inf = self.sim([], 0.31, kit=self.variant(self.INFERNUM, self.CALIBRUM))["breakdown"]
        self.assertAlmostEqual(inf["auto"], 1.1 * self.AD, places=6)
        self.assertAlmostEqual(inf["R"], blast + 150 + 0.25 * 24, places=6)
        self.assertAlmostEqual(inf["R attack"], 1.1 * self.AD, places=6)
        # Calibrum in the off-hand of a weapon whose ult deals nothing extra: Phase
        # first (0.25 s), then the cast where it fits before the next attack
        sev = self.variant(self.SEVERUM, self.CALIBRUM)
        self.assertNotIn("R", self.sim([], 0.24, kit=sev)["breakdown"])
        self.assertAlmostEqual(self.sim([], 0.25, kit=sev)["breakdown"]["R"], blast, places=6)
        marked = self.sim([], self.PERIOD + 0.01, kit=sev)["breakdown"]
        self.assertAlmostEqual(marked["P Calibrum mark"], 15 + 0.15 * 24 + 110, places=6)
        # Crescendum beats Infernum: six Chakrams fly back and are then worth
        # 16 + 14.5 + 13 + 11.5 + 10 + 8.5 = 73.5% AD on a Crescendum attack
        cres = self.variant(self.INFERNUM, self.CRESCENDUM)
        opening = self.sim([], 0.26, kit=cres)["breakdown"]
        self.assertAlmostEqual(opening["auto"], 1.1 * self.AD, places=6)   # the first attack is Infernum's
        self.assertAlmostEqual(opening["R"], blast, places=6)              # no Infernum bonus
        first = None
        for step in range(100, 600):
            first = self.sim([], step / 100, kit=cres)["breakdown"].get("P Chakrams")
            if first is not None:
                break
        self.assertAlmostEqual(first, 0.735 * self.AD, places=6)

    def test_the_other_weapons_qs(self):
        # Duskwave: the wave at the cast, the off-hand volley 0.4 + 0.25 s later
        # (100% AD), and no attack until then
        dusk = self.variant(self.INFERNUM, self.SEVERUM)
        cast = self.sim([], 0.0, kit=dusk, use_ult=False)["breakdown"]
        self.assertAlmostEqual(cast["Q Duskwave"], 110 + 0.21 * 24, places=6)
        self.assertNotIn("Q Duskwave volley", self.sim([], 0.64, kit=dusk, use_ult=False)["breakdown"])
        volley = self.sim([], 0.66, kit=dusk, use_ult=False)
        self.assertAlmostEqual(volley["breakdown"]["Q Duskwave volley"], self.AD, places=6)
        self.assertEqual(volley["attacks"], 1)
        # Binding Eclipse needs Gravitum's slow: the first attack lands it
        grav = self.variant(self.GRAVITUM, self.SEVERUM)
        eclipse = self.sim([], 0.0, kit=grav, use_ult=False)["breakdown"]
        self.assertAlmostEqual(eclipse["Q Binding Eclipse"], 140 + 0.5 * 24, places=6)
        # it is magic damage: magic resistance cuts it, armor does not
        self.assertAlmostEqual(self.sim([], 0.0, kit=grav, use_ult=False, mr=100, armor=27)
                               ["breakdown"]["Q Binding Eclipse"], (140 + 0.5 * 24) / 2, places=6)
        # Sentry: armed 0.35 s after the cast, then a hit every 1 / (0.64 x (1 + bonus AS))
        # for 4 s, each 125 + 52% bonus AD
        sentry = self.variant(self.CRESCENDUM, self.SEVERUM)
        hit, gap = 125 + 0.52 * 24, 1 / (0.64 * (1 + self.BONUS_AS / 100))
        self.assertNotIn("Q Sentry", self.sim([], 0.34, kit=sentry, use_ult=False)["breakdown"])
        self.assertAlmostEqual(self.sim([], 0.36, kit=sentry, use_ult=False)["breakdown"]["Q Sentry"],
                               hit, places=6)
        self.assertAlmostEqual(self.sim([], 0.35 + gap + 0.01, kit=sentry, use_ult=False)
                               ["breakdown"]["Q Sentry"], 2 * hit, places=6)
        hits = int(4 / gap) + 1
        self.assertAlmostEqual(self.sim([], 5.9, kit=sentry, use_ult=False)["breakdown"]["Q Sentry"],
                               hits * hit, places=6)

    def test_crescendum_attacks_at_the_pace_of_its_flight(self):
        # 550 units out at 5000 units a second slowing by 6000 a second each second,
        # 0.2 s there, back at 600 + 750 per 100% bonus attack speed, and a windup
        # of 0.16 / 1.5 of the attack time (0.1 / 1.5 on the crit share)
        kit = self.variant(self.CRESCENDUM, self.SEVERUM)
        items = ["phantom dancer", "berserkers greaves"]
        sh = builds.resolve_stats(self.champ, 16, [builds.resolve_item(self.pool, self.idx, t)
                                                    for t in items], self.pool, self.effects, kit=kit)
        bonus_as = sh["bonus_as_pct"] + 36
        attack_time = 1 / (0.665 + 0.658 * bonus_as / 100)
        out = (5000 - (5000 ** 2 - 2 * 6000 * 550) ** 0.5) / 6000
        back = 550 / (600 + 750 * bonus_as / 100)
        crit = sh["crit_chance"] / 100
        cycle = 0.2 + back + ((1 - crit) * 0.16 + crit * 0.1) / 1.5 * attack_time + out
        self.assertGreater(cycle, attack_time)  # slower than another weapon at this attack speed
        # the throw at t = 0 sets when the next attack may land, whatever is cast meanwhile
        self.assertEqual(self.sim(items, cycle - 0.01, kit=kit, use_ult=False)["attacks"], 1)
        self.assertEqual(self.sim(items, cycle + 0.01, kit=kit, use_ult=False)["attacks"], 2)

    def test_a_spent_weapon_is_replaced_from_the_queue(self):
        # 14 Moonlight a weapon: Calibrum spends 1 on its first attack and 10 on
        # Moonshot (the marked target's attacks are free), Severum 10 on Onslaught
        # and 1 an attack until it is empty
        kit = self.variant(gen_P_moonlight_capacity=14)
        full = self.sim([], 14.0, kit=kit)
        self.assertNotIn("Q Binding Eclipse", self.sim([], 14.0)["breakdown"])
        self.assertIn("Q Binding Eclipse", full["breakdown"])  # Gravitum came up from the queue
        # the attack that empties Severum is followed by a 1 s assembly with no attack
        times = []
        prev = 0
        for step in range(0, 1400):
            n = self.sim([], step / 100, kit=kit)["attacks"]
            if n != prev:
                times.append(step / 100)
                prev = n
        gaps = [round(b - a, 2) for a, b in zip(times, times[1:])]
        self.assertIn(1.0, gaps)
        spent_at = times[gaps.index(1.0)]
        # Gravitum's Q is held 1.5 s from the start of the assembly
        self.assertNotIn("Q Binding Eclipse", self.sim([], spent_at + 1.49, kit=kit)["breakdown"])


if __name__ == "__main__":
    unittest.main()

