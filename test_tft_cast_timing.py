"""Per-unit cast timelines, the crit-overflow rate and Blue Buff.

Run: python3 -m unittest test_tft_cast_timing -v

data/tft/set18/cast-timing.json transcribes TFTraits' cast timelines: a third
party ("not 100% accurate", its own disclaimer) that Roger already adopted
for Azir's six-command lock. The engine takes them as a model rule: while a
unit's cast animation (and channel) plays it neither attacks nor gains mana,
a cast made possible by an attack starts at that attack's unlock point, a
fresh attack starts when the window ends. These tests pin that rule as
relations read off the fight trace and compare every unit's first cast with
the one TFTraits publishes; they do not establish live-game timing. A unit
without a timeline keeps the flat rules, bit for bit.
"""

import copy
import os
import tempfile
import unittest
from unittest.mock import patch

import tft
from test_tft import DUMMY, ENGINE, ITEM_FX, SNAP, TRAIT_FX, events, immortal, item, spec_for

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
    0.75 attacks per second" — base attack speed, standing still — so the
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
                    # times every unit, against TFTraits' own figure — wherever
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
    afterwards, so a driver's own window stands — and everything keyed to
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
    driver from the effect's own clock, which starts when the ability LANDS —
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
    """The team, symmetric and theory fights share `Fight`: the same unit
    follows the same timeline whichever scheduler drives it."""

    def test_team_and_symmetric_timelines_equal_the_standalone_one(self):
        from test_tft_team_engine import ally, enemy, fight
        from test_tft_symmetric import match, events as match_events
        _, solo = ENGINE.simulate(timed("Soraka", duration=12.0), True)
        attacks = [e[0] for e in events(solo, "attack")]
        casts = [e[0] for e in events(solo, "cast")]
        self.assertEqual(len(casts), 2)

        def member():
            return {"spec": spec_for("Soraka", pressure=True, timed=True), "frontline": False,
                    "lane": 3, "priority": 0}
        team = fight([member()], [enemy(hp=10 ** 7)], duration=12.0)
        self.assertEqual([e["time"] for e in team["trace"] if e["kind"] == "attack"], attacks)
        self.assertEqual([e["time"] for e in team["trace"] if e["kind"] == "cast"], casts)
        duel = match([member()], [ally(hp=10 ** 7)], duration=12.0)
        self.assertEqual([e["time"] for e in match_events(duel, "attack")], attacks)
        self.assertEqual([e["time"] for e in match_events(duel, "cast")], casts)

    def test_the_theory_wrapper_carries_the_timeline(self):
        from test_tft_theory_pressure import actor, measure
        from tft_unit_profiles import UnitProfiles
        spec = UnitProfiles(SNAP, "clump").spec("TFT18_Soraka", 2, [], [])
        self.assertEqual(spec["unit"]["timing"]["base"]["castAnimation"], 1.98)
        front = actor("front", hp=10 ** 7, targets=3)
        result = measure([front, spec], [True, False], window=12.0)["samples"][1]
        self.assertAlmostEqual(result["firstCast"], 4.53)
        self.assertEqual(result["casts"], 2)
        flat = dict(spec, unit=dict(spec["unit"], timing=None))
        result = measure([front, flat], [True, False], window=12.0)["samples"][1]
        self.assertEqual((result["firstCast"], result["casts"]), (4.0, 3))

    def test_a_stun_over_the_unlock_point_delays_the_cast_and_loses_nothing(self):
        from test_tft_team_engine import ally
        from test_tft_symmetric import caster, match, events as match_events
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
            "lockUntil": 20.110665086482022, "castingUntil": 19.360665086482022, "mana": 9.412120342809779},
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
    sources": the bonus is multiplied, the base is not (an adopted reading)."""

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

    def test_copies_multiply_each_other(self):
        sheet, _ = ENGINE.simulate(spec_for("Ahri", items=["Blue Buff"] * 2 + ["Rabadon's Deathcap"]), False)
        self.assertAlmostEqual(sheet["ap"], 100 + (15 + 15 + 55) * 1.1 * 1.1)

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

    def test_the_item_note_says_it_is_an_adopted_reading(self):
        note = ITEM_FX["items"]["DA_BlueBuff"]["note"]
        self.assertIn("not verified", note)
        self.assertIn("base", note)


if __name__ == "__main__":
    unittest.main()
