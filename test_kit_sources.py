"""The kit-dossier pipeline (jobs/kit_sources.py, jobs/kit_triage.py): the
numbers sheet built from Riot's bin, the wiki template parsing and the
dossier checker. Hermetic: reads the archive under data/builds/sources/."""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs"))
import kit_sources as ks  # noqa: E402
import kit_triage  # noqa: E402

PATCH = "16.18"


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

    def errors(self, dossier):
        chk = ks.Checker("kassadin", dossier, self.src)
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
        import kit_dossier
        sec = kit_dossier.prompt_sections()
        for name in ("The fight the dossier is for", "Reading the sources", "An ability part",
                     "top.json"):
            self.assertIn(name, sec)
        self.assertIn("ONE ability part", kit_dossier.system_prompt("Q"))
        self.assertIn("needsBespoke", kit_dossier.system_prompt("top"))
        self.assertNotIn("How to work", kit_dossier.system_prompt("Q"))  # the agent protocol

    def test_reply_parsing_and_skeleton(self):
        import kit_dossier
        self.assertEqual(kit_dossier.parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(kit_dossier.parse_json('Here it is: {"a": [1, 2]} done'), {"a": [1, 2]})
        self.assertTrue(kit_dossier.untouched("Q", {"values": [{"role": None}], "mechanics": [],
                                                    "binUnused": []}))
        self.assertFalse(kit_dossier.untouched("Q", {"values": [], "filledBy": "sonnet"}))


class TestTriage(unittest.TestCase):
    def test_validate(self):
        ok = {"jax": {"primitives": [{"id": "plain-cast", "abilities": ["Q"]}],
                      "needsBespoke": False, "why": "x", "confidence": "high",
                      "damageFrom": "mixed"}}
        self.assertEqual(kit_triage.validate(ok), [])
        bad = copy.deepcopy(ok)
        bad["jax"]["primitives"][0]["id"] = "made-up"
        bad["jax"]["confidence"] = "certain"
        self.assertEqual(len(kit_triage.validate(bad)), 2)


if __name__ == "__main__":
    unittest.main()
