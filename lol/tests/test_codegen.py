"""Tests for the kit-dossier and machine-written driver pipeline
(lol/codegen/sources.py, lol/codegen/dossier.py, lol/codegen/driver.py):
numbers sheets from Riot's bin, wiki template parsing, dossier checking,
triage validation, driver registry, static lints, Jax reference driver,
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


class TestTriage(unittest.TestCase):
    def test_validate(self):
        ok = {"jax": {"primitives": [{"id": "plain-cast", "abilities": ["Q"]}],
                      "needsBespoke": False, "why": "x", "confidence": "high",
                      "damageFrom": "mixed"}}
        self.assertEqual(dossier.validate(ok), [])
        bad = copy.deepcopy(ok)
        bad["jax"]["primitives"][0]["id"] = "made-up"
        bad["jax"]["confidence"] = "certain"
        self.assertEqual(len(dossier.validate(bad)), 2)


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


if __name__ == "__main__":
    unittest.main()
