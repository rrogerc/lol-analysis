"""Machine-written drivers (jobs/kit_driver.py, engine/src/generated/): the
registry, the static checks, the engine's generic driver socket (through the
hand-written reference driver, Jax) and the fight checks. No model is called.
Needs the built engine."""

import json
import os
import sys
import unittest
import unittest.mock

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "jobs"))
import builds  # noqa: E402
import kit_driver as kd  # noqa: E402


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
        with open(os.path.join(BASE, "data", "builds", "jax.json")) as f:
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

    def sim(self, duration, level=16, items=(), hp=100_000, **kw):
        return self.f.run(level, list(items), hp, 0, 0, duration, **kw)

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
        with open(os.path.join(kd.ks.DOSSIERS_DIR, "jax.json")) as f:
            dossier = json.load(f)
        errors, facts = kd.fight_checks(builds.load_kit("jax"), "jax", dossier)
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
        dossier = {"abilities": {"Q": {"castTime": None}, "W": {}, "E": {}, "R": {}}}
        riot = {"Q": 0.3}
        with unittest.mock.patch.object(kd, "sheet_cast_times", lambda slug: riot):
            self.assertEqual(kd.cast_floors({}, dossier, "x"), {"Q": 0.3})
            # but an explicit "none" from the wiki wins: the ability is instant
            dossier["abilities"]["Q"]["castTime"] = {"value": None, "quote": "none"}
            self.assertEqual(kd.cast_floors({}, dossier, "x"), {})
        self.assertEqual(kd.cast_floors({}, dossier), {})  # no slug: no fallback

    def test_every_stated_cast_time_must_be_carried_and_read(self):
        dossier = {"abilities": {"Q": {"castTime": {"value": 0.25, "quote": "{{fd|0.25}}"}},
                                 "W": {"castTime": None}, "E": {}, "R": {}}}
        rs = 'let q = kit.num("gen.Q.castTimeS")?;'
        self.assertEqual(kd.lint_cast_times({"gen": {"Q": {"castTimeS": 0.25}}}, dossier, rs, None), [])
        # in the kit but never read: a dead number
        errs = kd.lint_cast_times({"gen": {"Q": {"castTimeS": 0.25}}}, dossier, "", None)
        self.assertIn("the driver never reads it", errs[0])
        # not in the kit at all: cast for free (Diana's Q, 2026-09-20)
        errs = kd.lint_cast_times({"gen": {}}, dossier, rs, None)
        self.assertIn("0.25 s cast time and the kit does not carry it", errs[0])
        self.assertIn('"unused"', errs[0])
        # an ability the driver never casts is waived by `unused`
        self.assertEqual(kd.lint_cast_times({"gen": {}, "unused": {"Q": "never cast"}}, dossier,
                                            rs, None), [])
        # an ability the wiki calls instant is never asked for
        self.assertEqual(kd.lint_cast_times({"gen": {}}, {"abilities": {"W": {"castTime": None}}},
                                            rs, None), [])

    def test_the_admitted_drivers_carry_their_cast_times(self):
        for name in ("jax", "annie", "chogath"):
            with open(os.path.join(kd.ks.DOSSIERS_DIR, f"{name}.json")) as f:
                dossier = json.load(f)
            with open(os.path.join(kd.GENERATED_DIR, f"{name}.rs")) as f:
                rs = f.read()
            self.assertEqual(kd.lint_cast_times(builds.load_kit(name), dossier, rs, name), [], name)

    def test_cast_floors_and_the_assumed_waiver(self):
        dossier = {"abilities": {"Q": {"castTime": {"value": 0.25, "quote": "{{fd|0.25}}"}},
                                 "W": {"castTime": {"value": 0.5, "quote": "{{fd|0.5}}"}},
                                 "E": {"castTime": None}, "R": {"castTime": {"value": None}}}}
        self.assertEqual(kd.cast_floors({}, dossier), {"Q": 0.25, "W": 0.5})
        kit = {"gen": {"W": {"castTimeS": 0}}, "assumed": [{"path": "gen.W.castTimeS", "why": "cougar form"}]}
        self.assertEqual(kd.cast_floors(kit, dossier), {"Q": 0.25})
        kit["assumed"][0].pop("why")  # a waiver needs its reason
        self.assertEqual(kd.cast_floors(kit, dossier), {"Q": 0.25, "W": 0.5})

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
        from unittest import mock
        cast = {"castTime": {"value": 0.25, "quote": "{{fd|0.25}}"},
                "values": [{"role": "damage", "damageType": "magic"}]}
        dossier = {"abilities": {"Q": cast, "W": cast, "E": {"castTime": None, "values": []}, "R": cast}}
        kit = {"unused": {}}
        with mock.patch.object(kd, "Fights", self.fake_fights({"Q": 300.0, "W": 300.0, "R": 300.0})):
            errors, facts = kd.fight_checks(kit, "jax", dossier)
        self.assertEqual(facts["castTimes"], {"Q": 0.25, "W": 0.25, "R": 0.25})
        self.assertEqual([e[:26] for e in errors], ["cast times are not respect"])
        # the same opening, big enough to kill: the zero-time kill is reported too
        with mock.patch.object(kd, "Fights", self.fake_fights({"Q": 900.0, "W": 900.0, "R": 900.0})):
            errors, _ = kd.fight_checks(kit, "jax", dossier)
        self.assertEqual(len(errors), 2)
        self.assertIn("dead at t = 0.00 s", errors[1])
        self.assertIn("AP burst", errors[1])
        # one cast in the first instant is the convention, not a fault
        with mock.patch.object(kd, "Fights", self.fake_fights({"R": 300.0})):
            errors, _ = kd.fight_checks(dict(kit, unused={"Q": "x", "W": "x"}), "jax", dossier)
        self.assertEqual(errors, [])

    def test_the_hand_written_kits_pass(self):
        # the rule is theirs: their blind copies' dossiers give the cast times
        for slug in ("kassadin", "vladimir", "twitch", "kayle"):
            with open(os.path.join(kd.ks.DOSSIERS_DIR, f"{slug}.json")) as f:
                dossier = json.load(f)
            kit = builds.load_kit(slug)
            floors = kd.cast_floors(kit, dossier)
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
        import shutil
        import tempfile
        from unittest import mock
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        tiny = [3089, 3135, 4645, 6653, 4633, 3115, 3042]
        with mock.patch.object(builds, "SCENARIO_CACHE_DIR", tmp), \
                mock.patch.object(builds, "DEFAULT_POOL", tiny):
            champs = builds.damage_champions()
            self.assertEqual(champs[:4], builds.kit_champions())
            self.assertIn("jax", champs)
            self.assertFalse([c for c in champs if c.endswith("_blind")])
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
