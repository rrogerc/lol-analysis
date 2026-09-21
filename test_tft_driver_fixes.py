"""Driver fixes from the September 2026 leaderboard audit, one regression each.

Every check reads RELATIONS off the fight trace — no cast while an effect is
up, mana that came in between two casts equals what the unlocked attacks paid,
the Nth cast is the moon — never absolute timestamps: those belong to the
engine's cast windows, which are being reworked separately.

The effect-long mana locks are TFTraits' per-unit statements
(https://tftraits.com/champions/<unit>/, retrieved 2026-09-20; third party,
"publicly available game data with gameplay observation"). They are adopted
model rules like Azir's, not verified in game; these tests pin the adopted
rule, they do not establish live timing.

Run after rebuilding the TFT engine: python3 -m unittest test_tft_driver_fixes -v
"""

import collections
import copy
import unittest

import tft
from test_tft import DUMMY, ENGINE, SNAP, events, immortal, spec_for

TICK = tft.TICK_S


def flat(dtype, amount):
    return {"dtype": dtype, "terms": [{"type": "flat", "value": float(amount), "op": "add"}]}


def timing_spec(name, *, star=2, duration=20.0, geometry="clump", regen=0.0, attack_speed=None,
                fx=(), items=(), traits=(), dummy=None):
    """One unpressured fight against dummies that cannot die, with the role's
    regeneration and attack speed replaced, so the mana that comes in between
    two casts is exactly what the attacks (and `regen`) paid."""
    spec = spec_for(name, star=star, items=items, fx=fx, geometry=geometry, traits=traits,
                    duration=duration, pressure=False, dummy=dummy or immortal(DUMMY))
    spec["role"] = {"manaRegen": float(regen), "asPct": 0.0}
    if attack_speed is not None:
        for kit in spec["kits"].values():
            kit["stats"]["as"] = attack_speed
    return spec


def fight(spec):
    return ENGINE.simulate(spec, True)


def casts_of(res):
    """[(cast time, mana on the bar at the cast, when the driver's cast ran)].
    The last is the `land` row that follows the cast, or the cast itself for an
    ability the engine runs at the start of the cast."""
    rows = [(e[0], e[2]) for e in events(res, "cast")]
    lands = [e[0] for e in events(res, "land")]
    out = []
    for i, (t, mana) in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else float("inf")
        landed = [x for x in lands if t <= x < nxt]
        out.append((t, mana, landed[0] if landed else t))
    return out


def came_in(casts, i, mana_max):
    """Mana gained between cast i and cast i+1 (the overflow carries)."""
    return casts[i + 1][1] - min(max(casts[i][1] - mana_max, 0.0), mana_max)


def attack_times(res, after, through):
    return [e[0] for e in events(res, "attack") if after < e[0] <= through]


def hit_times(res, src):
    return [e[0] for e in events(res, "damage", src)]


class TestNidaleeJavelinLock(unittest.TestCase):
    """AP form: no mana from the cast until the third javelin is thrown."""

    def ap(self, **kw):
        return timing_spec("Nidalee", fx=[{"stats": [["ap", 1.0]]}], **kw)

    def test_javelins_grant_no_mana_and_ordinary_attacks_do(self):
        spec = self.ap(duration=30.0)
        sheet, res = fight(spec)
        self.assertEqual(sheet["form"], "AP")
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        javelins = sorted(hit_times(res, "javelins") + hit_times(res, "farthest javelin"))
        per_attack = 10.0                                   # Marksman
        for i in range(len(casts) - 1):
            start, end = casts[i][0], casts[i + 1][0]
            thrown = [t for t in javelins if start < t <= end]
            # every window is thrown out before the bar can fill again
            self.assertEqual(len(thrown), 3)
            ordinary = [t for t in attack_times(res, start, end) if t not in thrown]
            self.assertTrue(all(t > thrown[-1] for t in ordinary))
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), per_attack * len(ordinary))

    def test_regeneration_is_held_with_the_attack_mana(self):
        regen = 4.0
        sheet, res = fight(self.ap(duration=30.0, regen=regen))
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        javelins = sorted(hit_times(res, "javelins") + hit_times(res, "farthest javelin"))
        for i in range(len(casts) - 1):
            start, end = casts[i][0], casts[i + 1][0]
            release = [t for t in javelins if start < t <= end][2]
            ordinary = [t for t in attack_times(res, start, end) if t > release]
            gained = came_in(casts, i, sheet["manaMax"]) - 10.0 * len(ordinary)
            # regen only for the time after the last javelin (paid on the ticks)
            self.assertLessEqual(gained, regen * (end - release) + 1e-9)
            self.assertGreaterEqual(gained, regen * (end - release - TICK) - 1e-9)

    def test_mana_items_cannot_chain_casts_through_the_javelins(self):
        # the published #2 build: Nashor's Tooth refilled the bar on her own javelins
        spec = spec_for("Nidalee", items=("Giant Slayer", "Jeweled Gauntlet", "Nashor's Tooth"),
                        dummy=immortal(DUMMY), pressure=False, duration=30.0)
        sheet, res = fight(spec)
        self.assertEqual(sheet["form"], "AP")
        casts = casts_of(res)
        javelins = sorted(hit_times(res, "javelins") + hit_times(res, "farthest javelin"))
        for (start, _, _), (end, _, _) in zip(casts, casts[1:]):
            self.assertEqual(len([t for t in javelins if start < t <= end]), 3)
            self.assertGreater(len(attack_times(res, start, end)), 3)   # ordinary attacks refill it

    def test_ad_form_keeps_the_ordinary_cast_lock(self):
        spec = timing_spec("Nidalee", fx=[{"stats": [["adPct", 0.1]]}], duration=10.0)
        sheet, res = fight(spec)
        self.assertEqual(sheet["form"], "AD")
        self.assertLess(res["probe"]["lockUntil"], 1e8)
        self.assertGreaterEqual(res["casts"], 3)


class TestMamaBeakFlockLock(unittest.TestCase):
    def test_no_mana_while_the_tiny_beaks_are_out(self):
        for items in ((), ("Rabadon's Deathcap",) * 3):
            with self.subTest(items=items):
                spec = timing_spec("Mama Beak", items=items, duration=90.0)
                sheet, res = fight(spec)
                casts = casts_of(res)
                self.assertGreaterEqual(len(casts), 3)
                beaks = hit_times(res, "tiny beaks")
                for i in range(len(casts) - 1):
                    start, end = casts[i][0], casts[i + 1][0]
                    with_flock = [t for t in attack_times(res, start, end) if t in beaks]
                    alone = [t for t in attack_times(res, start, end) if t not in beaks]
                    self.assertTrue(with_flock)
                    # the flock's whole life lies between two casts, and only
                    # the attacks made without it paid mana
                    self.assertTrue(all(t > with_flock[-1] for t in alone))
                    self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), 10.0 * len(alone))

    def test_the_lock_lasts_as_long_as_ability_power_keeps_the_flock(self):
        windows = []
        for items in ((), ("Rabadon's Deathcap",) * 3):
            _, res = fight(timing_spec("Mama Beak", items=items, duration=40.0))
            (start, _, landed), (end, _, _) = casts_of(res)[:2]
            beaks = [t for t in hit_times(res, "tiny beaks") if start < t <= end]
            windows.append((beaks[-1] - landed, end - landed))
        (plain_flock, plain_gap), (ap_flock, ap_gap) = windows
        self.assertGreater(ap_flock, plain_flock)
        self.assertGreater(ap_gap, plain_gap)


class TestMurkwolf(unittest.TestCase):
    def test_empowered_attacks_grant_no_mana(self):
        sheet, res = fight(timing_spec("Murkwolf", duration=30.0))
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        empowered = hit_times(res, "empowered")
        for i in range(len(casts) - 1):
            start, end = casts[i][0], casts[i + 1][0]
            claws = [t for t in empowered if start < t <= end]
            self.assertEqual(len(claws), 2)
            ordinary = [t for t in attack_times(res, start, end) if t not in claws]
            self.assertTrue(all(t > claws[-1] for t in ordinary))
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), 10.0 * len(ordinary))

    def test_regeneration_resumes_at_the_second_empowered_attack(self):
        regen = 4.0
        sheet, res = fight(timing_spec("Murkwolf", duration=30.0, regen=regen))
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        empowered = hit_times(res, "empowered")
        for i in range(len(casts) - 1):
            start, end = casts[i][0], casts[i + 1][0]
            release = [t for t in empowered if start < t <= end][1]
            ordinary = [t for t in attack_times(res, start, end) if t > release]
            gained = came_in(casts, i, sheet["manaMax"]) - 10.0 * len(ordinary)
            self.assertLessEqual(gained, regen * (end - release) + 1e-9)
            self.assertGreaterEqual(gained, regen * (end - release - TICK) - 1e-9)

    def test_a_kill_grants_no_empowered_attacks(self):
        # NumEmpoweredAttacksGainedOnKill is in the data and in no tooltip: a
        # dormant row. The leap kills the weak dummy; two empowered attacks follow.
        dummy = immortal(DUMMY)
        dummy["slots"] = [dict(slot, hp=1.0 if i == 1 else slot["hp"])
                          for i, slot in enumerate(dummy["slots"])]
        spec = timing_spec("Murkwolf", duration=12.0, dummy=dummy)
        rows = spec["kits"]["base"]["rows"]
        self.assertEqual(rows["NumEmpoweredAttacks"], 2)
        self.assertEqual(rows["NumEmpoweredAttacksGainedOnKill"], 2)
        _, res = fight(spec)
        casts = casts_of(res)
        kills = events(res, "kill")
        self.assertEqual([(e[3], e[4]) for e in kills], [(1, "leap")])
        end = casts[1][0] if len(casts) > 1 else float("inf")
        self.assertLess(kills[0][0], end)
        self.assertEqual(len([t for t in hit_times(res, "empowered") if casts[0][0] < t <= end]), 2)


class TestBramblebackFrenzyLock(unittest.TestCase):
    def test_no_mana_for_the_eight_seconds_of_frenzy(self):
        for items in ((), ("Blue Buff", "Spear of Shojin", "Guinsoo's Rageblade")):
            with self.subTest(items=items):
                spec = timing_spec("Brambleback", items=items, duration=60.0)
                frenzy = spec["kits"]["base"]["rows"]["Duration"]
                self.assertEqual(frenzy, 8)
                sheet, res = fight(spec)
                casts = casts_of(res)
                self.assertGreaterEqual(len(casts), 3)
                for (_, _, landed), (start, _, _) in zip(casts, casts[1:]):
                    self.assertGreaterEqual(start, landed + frenzy)     # never refreshed from mana
                if not items:
                    for i in range(len(casts) - 1):
                        (start, _, landed), end = casts[i], casts[i + 1][0]
                        locked = [t for t in attack_times(res, start, end) if t < landed + frenzy]
                        free = [t for t in attack_times(res, start, end) if t >= landed + frenzy]
                        self.assertGreaterEqual(len(locked), 3)
                        self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), 10.0 * len(free))


class TestDianaBarrier(unittest.TestCase):
    def test_orbs_stay_with_the_enemies_in_reach_when_the_cast_landed(self):
        # Spread out, only the target is "within 2 hexes": when it dies the
        # orbs left over are lost, they do not fly on to the next dummy.
        dummy = copy.deepcopy(DUMMY)
        dummy["slots"][0]["hp"] = 60.0
        for geometry, recipients in (("spread", {0}), ("clump", {0, 1, 2})):
            with self.subTest(geometry=geometry):
                spec = timing_spec("Diana", geometry=geometry, duration=20.0, dummy=dummy)
                spec["kits"]["base"]["stats"].update(initialMana=spec["kits"]["base"]["stats"]["mana"])
                for kit in spec["kits"].values():
                    kit["baseAd"] = 0.0                       # the orbs, not an attack, kill it
                _, res = fight(spec)
                second = casts_of(res)[1][0]
                orbs = [e for e in events(res, "damage", "orbs") if e[0] < second]
                self.assertEqual({e[3] for e in orbs}, recipients)
                kill = events(res, "kill")[0]
                self.assertEqual((kill[3], kill[4]), (0, "orbs"))
                if geometry == "spread":
                    self.assertLess(len(orbs), spec["kits"]["base"]["rows"]["NumOrbs"])
                else:
                    self.assertEqual(len(orbs), spec["kits"]["base"]["rows"]["NumOrbs"])

    def test_mana_is_locked_while_the_barrier_stands_unbroken(self):
        spec = timing_spec("Diana", star=3, duration=30.0, attack_speed=2.0)
        hold = spec["kits"]["base"]["rows"]["ShieldDuration"]
        sheet, res = fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        for i in range(len(casts) - 1):
            (start, _, landed), end = casts[i], casts[i + 1][0]
            locked = [t for t in attack_times(res, start, end) if t < landed + hold]
            free = [t for t in attack_times(res, start, end) if t >= landed + hold]
            self.assertGreaterEqual(len(locked), 2)
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), 10.0 * len(free))

    def test_a_broken_barrier_releases_the_mana_early(self):
        calm = timing_spec("Diana", star=3, duration=30.0, attack_speed=4.0)
        hold = calm["kits"]["base"]["rows"]["ShieldDuration"]
        _, res = fight(calm)
        landed = casts_of(res)[0][2]
        # one blow, well after any cast animation and off her attack times, that
        # spends the whole barrier
        strike = landed + 0.8 * hold
        spec = copy.deepcopy(calm)
        spec["pressure"] = True
        for i, slot in enumerate(spec["dummies"]["slots"]):
            slot.update(ad=10000.0 if i == 0 else 0.0, ability=0.0, manaMax=0.0, manaStart=0.0,
                        manaPerAttack=0.0, manaFromDamage=False, attackStart=strike, streams=1)
            slot["as"] = 0.01
        spec["dummies"]["critEv"] = 1.0
        for kit in spec["kits"].values():
            kit["hpStar"] = 10.0 ** 6                       # she survives the blow
        sheet, res = fight(spec)
        casts = casts_of(res)
        self.assertEqual(casts[0][2], landed)
        blows = [e for e in events(res, "take") if e[0] <= landed + hold]
        self.assertEqual([e[0] for e in blows], [strike])
        start, end = casts[0][0], casts[1][0]
        locked = [t for t in attack_times(res, start, end) if t < strike]
        free = [t for t in attack_times(res, start, end) if t >= strike]
        early = [t for t in free if t < landed + hold]
        self.assertTrue(locked)
        self.assertTrue(early)                                # these would still be locked unbroken
        self.assertAlmostEqual(came_in(casts, 0, sheet["manaMax"]), 10.0 * len(free))


class TestPebblesDrain(unittest.TestCase):
    """Azure Laser drains the bar; nothing locks mana out while it does.
    Expectations are derived from the bar the `cast` row reports and from the
    laser's own payments, so they hold whatever instant the engine starts the
    cast at."""

    DPS = 240.0
    MANA = 70.0
    RATE = 0.35

    def laser(self, *, mana=MANA, start=MANA - 1.0, regen=0.0, duration=12.0,
              attack_speed=0.5, traits=(), kind="Caster"):
        spec = timing_spec("Pebbles", duration=duration, regen=regen, attack_speed=attack_speed,
                           traits=traits)
        spec["unit"]["kind"] = kind
        spec["targetDebuffs"] = {}
        for slot in spec["dummies"]["slots"]:
            slot.update(armor=0.0, mr=0.0)
        kit = spec["kits"]["base"]
        kit["stats"].update(mana=mana, initialMana=start, critChance=0.0, critMult=1.0)
        kit["baseAd"] = 0.0
        kit["calcs"]["MagicDamageCalc1"] = flat("magic", self.DPS)
        kit["rows"].update(PercentManaPerSecond=self.RATE, MRReduction=0.0)
        return spec

    def channel(self, res, index=0):
        """(channel start, bar at the cast, laser damage, last laser hit, first
        attack after the cast) of one cast. The start is read off the laser
        itself: its first payment is DPS x the time since the channel began."""
        casts = casts_of(res)
        cast, bar, _ = casts[index]
        end = casts[index + 1][0] if index + 1 < len(casts) else float("inf")
        hits = [e for e in events(res, "damage", "laser") if cast < e[0] <= end]
        start = hits[0][0] - hits[0][2] / self.DPS
        self.assertGreaterEqual(start, cast - 1e-9)
        after = attack_times(res, cast, float("inf"))
        return start, bar, sum(e[2] for e in hits), hits[-1][0], (after[0] if after else None)

    def test_the_whole_bar_drains_overflow_included(self):
        res = fight(self.laser())[1]
        start, bar, damage, last, resumed = self.channel(res)
        self.assertGreater(bar, self.MANA)                    # 69 and an attack's 7: overflow
        length = bar / (self.RATE * self.MANA)
        self.assertAlmostEqual(damage, self.DPS * length)
        # the laser stops the instant the bar is empty, not on the next tick,
        # and she does not attack before that
        self.assertAlmostEqual(last - start, length)
        self.assertNotAlmostEqual(last / TICK, round(last / TICK))
        self.assertGreaterEqual(resumed, last - 1e-9)

    def test_regen_keeps_flowing_and_lengthens_the_laser(self):
        for regen in (2.0, 7.0, 12.0):
            with self.subTest(regen=regen):
                res = fight(self.laser(regen=regen, duration=20.0))[1]
                start, bar, damage, last, resumed = self.channel(res)
                length = bar / (self.RATE * self.MANA - regen)
                self.assertGreater(length, bar / (self.RATE * self.MANA) + 0.25)
                self.assertAlmostEqual(damage, self.DPS * length, places=6)
                self.assertAlmostEqual(last - start, length, places=9)
                self.assertGreaterEqual(resumed, last - 1e-9)

    def test_mana_mult_scales_the_regen_that_feeds_the_channel(self):
        spec = self.laser(regen=4.0, duration=20.0)
        spec["items"].append({"api": "test", "name": "test", "unique": False, "stats": [],
                              "adds": [], "manaMult": 1.5})
        _, bar, damage, _, _ = self.channel(fight(spec)[1])
        self.assertAlmostEqual(damage, self.DPS * bar / (self.RATE * self.MANA - 4.0 * 1.5), places=6)

    def test_the_bar_refills_from_the_end_of_the_channel_without_a_lock(self):
        regen = 5.0
        res = fight(self.laser(start=self.MANA, regen=regen, duration=40.0, kind="Specialist"))[1]
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 2)
        _, _, _, last, _ = self.channel(res)
        # a Specialist gains nothing per attack: the second bar is regen alone,
        # counted from the instant the first channel ended
        refill = casts[1][0] - last
        self.assertLessEqual(casts[1][1], regen * refill + 1e-9)
        self.assertGreaterEqual(casts[1][1], regen * (refill - TICK) - 1e-9)
        self.assertAlmostEqual(refill, self.MANA / regen, delta=TICK + 1e-9)

    def test_a_channel_that_regen_outpaces_runs_to_the_end_of_the_fight(self):
        for regen in (self.RATE * self.MANA, 40.0):
            with self.subTest(regen=regen):
                duration = 15.0
                res = fight(self.laser(regen=regen, duration=duration))[1]
                casts = casts_of(res)
                self.assertEqual(len(casts), 1)
                self.assertFalse(attack_times(res, casts[0][0], duration))
                start, _, damage, last, _ = self.channel(res)
                self.assertAlmostEqual(last, duration)
                self.assertAlmostEqual(damage, self.DPS * (duration - start))

    def teal(self, regen, **kw):
        spec = self.laser(start=self.MANA, regen=regen, duration=60.0, kind="Specialist",
                          traits=[("DA_Riftbeast18", 1)], **kw)
        rows = spec["kits"]["base"]["rows"]
        self.assertEqual((rows["TraitChannelSecondsTooltip"], rows["TraitManaRegenTooltip"]), (4, 2))
        res = fight(spec)[1]
        start, bar, _, last, _ = self.channel(res)
        return start, bar, len(casts_of(res)) == 1 and abs(last - 60.0) < 1e-9

    def catch_up(self, regen):
        """Mana the drain takes before the Teal Buff's +2 regen per 4 s channeled overtakes it."""
        need, net = 0.0, self.RATE * self.MANA - regen
        while net > 0.0:
            need, net = need + 4.0 * net, net - 2.0
        return need

    def test_teal_buff_carries_the_channel_forever_once_the_bar_outlasts_the_catch_up(self):
        plain = fight(self.laser(start=self.MANA, regen=18.5, duration=60.0, kind="Specialist"))[1]
        self.assertGreaterEqual(len(casts_of(plain)), 2)      # without the buff the laser ends
        for regen in (14.0, 16.0, 18.5, 21.0):
            with self.subTest(regen=regen):
                _, bar, endless = self.teal(regen)
                self.assertGreater(abs(bar - self.catch_up(regen)), 3.0)
                self.assertEqual(endless, bar > self.catch_up(regen))

    def test_tftflow_go_infinite_figure(self):
        # tftflow's 18.2b guide: "~17.1 Mana Regen + Alpha Mark to go infinite ...
        # 70 Max Mana and 35% Mana Drain". With a bar of exactly 70 cast on a tick
        # the catch-up costs 16 * (24.5 - regen) - 48 mana: 70 at regen 17.125.
        self.assertAlmostEqual(self.catch_up(17.125), 70.0)
        start, bar, _ = self.teal(17.0)
        if bar != self.MANA or abs(start / TICK - round(start / TICK)) > 1e-9:
            self.skipTest("the fixture's bar is no longer exactly 70 on a tick")
        self.assertFalse(self.teal(17.12)[2])
        self.assertTrue(self.teal(17.13)[2])


class TestAluneMoonCycle(unittest.TestCase):
    def test_the_five_phases_are_in_the_data(self):
        # The driver spells out the five (no kit row carries the count): the
        # pinned snapshot and the newest archive must both still list them.
        for snap in (SNAP, tft.load_snapshot(SNAP.set_no, None)):
            with self.subTest(patch=snap.patch):
                phases = [t for t in snap.raw["extras"]["traitTooltips"]
                          if t["apiName"].startswith("DA_AluneUniqueTrait18_Tooltip_Phase")]
                self.assertEqual([t["desc"].split(" <")[0] for t in phases],
                                 ["New Moon", "Waxing Crescent", "Half Moon", "Waxing Gibbous", "Full Moon"])
                attuned = next(t for t in snap.raw["traits"] if t["apiName"] == "DA_AluneUniqueTrait18")
                self.assertIn("cycles to a new phase of the moon after each cast", attuned["desc"])

    def test_the_moon_crashes_on_every_fifth_cast(self):
        spec = timing_spec("Alune", duration=60.0, attack_speed=2.0, regen=10.0)
        _, res = fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 11)
        moons, shards = hit_times(res, "full moon"), hit_times(res, "moonshards")
        for index, (start, _, landed) in enumerate(casts, 1):
            end = casts[index][0] if index < len(casts) else float("inf")
            if landed > spec["duration"] - 1e-9:
                continue                                       # the fight ended mid-cast
            crashed = any(start <= t < end for t in moons)
            rained = any(start <= t < end for t in shards)
            self.assertEqual(crashed, index % 5 == 0, index)
            self.assertEqual(rained, index % 5 != 0, index)


class TestApheliosSwipeCount(unittest.TestCase):
    def swipes(self, bonus=0.0, items=()):
        spec = timing_spec("Aphelios", duration=30.0, items=items,
                           fx=[{"startingMana": 1000.0}, {"stats": [["asPct", bonus]]}])
        rows = spec["kits"]["base"]["rows"]
        self.assertEqual((rows["NumAttacksBase"], rows["AS_NeededForExtraSwipe"]), (5, 0.2))
        sheet, res = fight(spec)
        blast = hit_times(res, "blast")[0]
        return sheet, len([t for t in hit_times(res, "swipes") if t <= blast])

    def test_an_exact_multiple_of_bonus_attack_speed_keeps_its_swipe(self):
        # a whole number of 20% steps must not come out a hair under it:
        # 0.6 / 0.2 is 2.9999999999999996 in floating point
        for bonus, expected in ((0.0, 5), (0.2, 6), (0.4, 7), (0.6, 8), (0.8, 9), (1.0, 10),
                                (1.2, 11), (1.4, 12)):
            with self.subTest(bonus=bonus):
                self.assertEqual(self.swipes(bonus)[1], expected)

    def test_giant_slayer_and_red_buff_buy_three_swipes(self):
        # +15% and +45% on 0.8: the sheet reads 1.28, the bonus 0.5999999999999999
        sheet, swipes = self.swipes(items=("Giant Slayer", "Red Buff", "Deathblade"))
        self.assertAlmostEqual(sheet["as"], 0.8 * 1.6)
        self.assertEqual(swipes, 8)

    def test_a_partial_step_still_buys_nothing(self):
        for bonus, expected in ((0.19, 5), (0.59, 7), (0.61, 8), (0.79, 8)):
            with self.subTest(bonus=bonus):
                self.assertEqual(self.swipes(bonus)[1], expected)


class TestAsheTrailCrit(unittest.TestCase):
    def test_both_halves_of_the_trail_crit_with_precision(self):
        crit = {"stats": [["crit", 0.75]]}                    # 25% + 75%: every hit crits for 1.4
        runs = {}
        for label, fx in (("plain", [crit]), ("precise", [dict(crit, precision=1)])):
            spec = timing_spec("Ashe", duration=20.0, fx=fx + [{"startingMana": 1000.0}])
            spec["targetDebuffs"] = {}
            _, runs[label] = fight(spec)
        plain = events(runs["plain"], "damage", "trail")
        precise = events(runs["precise"], "damage", "trail")
        self.assertGreater(len(plain), 10)
        self.assertEqual([(e[0], e[3]) for e in plain], [(e[0], e[3]) for e in precise])
        # two shares tick on every dummy in the trail: the flat one and the max-Health one
        per_tick = collections.Counter((e[0], e[3]) for e in plain)
        self.assertEqual(set(per_tick.values()), {2})
        for before, after in zip(plain, precise):
            self.assertAlmostEqual(after[2] / before[2], 1.4)


class TestDravenCashOut(unittest.TestCase):
    def test_the_cashed_bleed_crits_exactly_as_its_ticks_would(self):
        crit = {"stats": [["crit", 0.75]]}
        runs = {}
        for label, fx in (("plain", [crit]), ("precise", [dict(crit, precision=1)])):
            spec = timing_spec("Draven", geometry="spread", duration=25.0, fx=fx)
            _, runs[label] = fight(spec)
        for res in runs.values():
            self.assertGreaterEqual(res["casts"], 1)
        plain, precise = runs["plain"], runs["precise"]
        landed = casts_of(plain)[0][2]
        self.assertEqual(landed, casts_of(precise)[0][2])
        ticks = [[e for e in events(res, "damage", "bleed axes") if e[0] < landed] for res in (plain, precise)]
        self.assertGreater(len(ticks[0]), 10)
        for before, after in zip(*ticks):
            self.assertAlmostEqual(after[2] / before[2], 1.4)
        # out, the cashed bleed, back: three hits on the one dummy in the line
        axes = [[e for e in events(res, "damage", "giant axes") if e[0] == landed] for res in (plain, precise)]
        self.assertEqual(len(axes[0]), 3)
        for before, after in zip(*axes):
            self.assertAlmostEqual(after[2] / before[2], 1.4)
        # and nothing is left to tick until her next attack bleeds the dummy again
        for res in (plain, precise):
            again = min(t for t in attack_times(res, landed, float("inf")))
            self.assertFalse([e for e in events(res, "damage", "bleed axes") if landed < e[0] < again])


class TestMorganaBlastTargets(unittest.TestCase):
    def test_the_blast_picks_three_enemies_whatever_the_geometry(self):
        for geometry in ("spread", "clump"):
            with self.subTest(geometry=geometry):
                spec = timing_spec("Morgana", geometry=geometry, duration=20.0)
                count = spec["kits"]["base"]["rows"]["NumEnemiesCursed"]
                self.assertEqual(count, 3)
                _, res = fight(spec)
                casts = casts_of(res)
                self.assertGreaterEqual(len(casts), 2)
                for index, (start, _, landed) in enumerate(casts[:-1]):
                    end = casts[index + 1][0]
                    blast = [e[3] for e in events(res, "damage", "blast") if start <= e[0] < end]
                    self.assertEqual(blast, [0, 1, 2])
                    zone = {e[3] for e in events(res, "damage", "withering zone") if start <= e[0] < end}
                    self.assertEqual(zone, {0} if geometry == "spread" else {0, 1, 2})


TANK_PER_ATTACK = 5.0                                   # Kind::Tank

# The tanks whose shield holds their mana, with the row that gives TFTraits'
# "at most 4.00 s" and the highest star the unit is simulated at.
SHIELD_TANKS = (("Ornn", 3, "ShieldDuration"), ("Rakan", 3, "ShieldDuration"),
                ("Sejuani", 3, "ShieldDuration"), ("Rammus", 3, "Duration"),
                ("Malphite", 2, "ShieldDuration"), ("Sentinel", 2, "ShieldDuration"))


def one_blow(spec, at, ad):
    """The fight's pressure reduced to a single blow from the first dummy at
    `at` seconds; nothing else ever swings, casts or gains mana."""
    spec = copy.deepcopy(spec)
    spec["pressure"] = True
    for i, slot in enumerate(spec["dummies"]["slots"]):
        slot.update(ad=ad if i == 0 else 0.0, ability=0.0, manaMax=0.0, manaStart=0.0,
                    manaPerAttack=0.0, manaFromDamage=False, attackStart=at, streams=1)
        slot["as"] = 0.01
    spec["dummies"]["critEv"] = 1.0
    return spec


class TestShieldTankLocks(unittest.TestCase):
    """Ornn, Rakan, Sejuani, Rammus, Malphite and Sentinel hold every source
    of mana — attacks, the regen ticks and the share a tank takes off damage
    — for as long as their shield stands, its own duration row at most."""

    def spec(self, name, star, hp=None):
        spec = timing_spec(name, star=star, duration=60.0, attack_speed=3.0)
        self.assertEqual(spec["unit"]["kind"], "Tank")
        for kit in spec["kits"].values():
            if hp is not None:
                kit["hpStar"] = hp                      # he survives the blow below
        return spec

    def probe(self, name, star, hp):
        """The same fight with nothing hitting back: (spec, when the first
        cast landed, that shield's size, when the second cast starts)."""
        spec = self.spec(name, star, hp=hp)
        _, res = fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 2)
        return spec, casts[0][2], events(res, "shield")[0][2], casts[1][0]

    def test_no_mana_while_the_shield_stands(self):
        for name, star, row in SHIELD_TANKS:
            with self.subTest(unit=name):
                spec = self.spec(name, star)
                hold = spec["kits"]["base"]["rows"][row]
                self.assertEqual(hold, 4)               # TFTraits' 4.00 s, from the kit
                sheet, res = fight(spec)
                casts = casts_of(res)
                self.assertGreaterEqual(len(casts), 2)
                self.assertFalse(events(res, "take"))   # nothing hits back in this fixture
                for i in range(len(casts) - 1):
                    (start, _, landed), end = casts[i], casts[i + 1][0]
                    locked = [t for t in attack_times(res, start, end) if t < landed + hold]
                    free = [t for t in attack_times(res, start, end) if t >= landed + hold]
                    self.assertGreaterEqual(len(locked), 3)
                    self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]),
                                           TANK_PER_ATTACK * len(free))

    def test_a_broken_shield_releases_the_mana_early(self):
        for name, star, row in SHIELD_TANKS:
            with self.subTest(unit=name):
                calm, landed, barrier, _ = self.probe(name, star, 10.0 ** 6)
                hold = calm["kits"]["base"]["rows"][row]
                # one blow, clear of the cast animation and far bigger than
                # the shield, so it is spent in full well before it expires
                strike = landed + 0.5 * hold
                sheet, res = fight(one_blow(calm, strike, 5.0 * barrier))
                casts = casts_of(res)
                self.assertEqual(casts[0][2], landed)
                self.assertEqual([e[0] for e in events(res, "take")], [strike])
                self.assertGreater(res["hpLeft"], 0.0)
                start, end = casts[0][0], casts[1][0]
                free = [t for t in attack_times(res, start, end) if t >= strike]
                # the blow itself is processed before the driver's hit hook,
                # so it buys nothing; the attacks after it do, and the first
                # of them would still be locked with the shield unbroken
                self.assertTrue([t for t in free if t < landed + hold])
                self.assertAlmostEqual(came_in(casts, 0, sheet["manaMax"]),
                                       TANK_PER_ATTACK * len(free))

    def test_damage_taken_inside_the_lock_buys_no_mana(self):
        # The audit's mechanism: a tank gains 1% pre + 3% post-mitigation mana
        # off every hit, which is what let Rammus and Malphite re-shield every
        # ~2.75 s while the previous shield still stood.
        for name, star, row in SHIELD_TANKS:
            with self.subTest(unit=name):
                calm, landed, barrier, second = self.probe(name, star, 10.0 ** 6)
                hold = calm["kits"]["base"]["rows"][row]
                swallowed = 0.5 * barrier               # never breaks the shield
                inside = fight(one_blow(calm, landed + 0.5 * hold, swallowed))[1]
                self.assertEqual([e[0] for e in events(inside, "take")],
                                 [landed + 0.5 * hold])
                self.assertEqual(casts_of(inside)[1][0], second)
                # the same blow once the shield has gone does pay for itself
                after = fight(one_blow(calm, landed + hold + 0.5, swallowed))[1]
                self.assertLess(casts_of(after)[1][0], second)


class TestShenKiStrikeLock(unittest.TestCase):
    """TFTraits: "Attack speed shortens this lock: it lasts 3 empowered
    attacks", and it blocks "mana from attacks, ticks or damage taken"."""

    def spec(self, attack_speed=2.0):
        spec = timing_spec("Shen", star=3, duration=60.0, attack_speed=attack_speed)
        self.assertEqual(spec["unit"]["kind"], "Tank")
        self.assertEqual(spec["kits"]["base"]["rows"]["NumAttacksBuff"], 3)
        return spec

    def test_the_ki_strikes_grant_no_mana(self):
        spec = self.spec()
        sheet, res = fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        ki = hit_times(res, "ki strike")
        for i in range(len(casts) - 1):
            start, end = casts[i][0], casts[i + 1][0]
            struck = [t for t in ki if start < t <= end]
            self.assertEqual(len(struck), 3)
            ordinary = [t for t in attack_times(res, start, end) if t not in struck]
            self.assertTrue(all(t > struck[-1] for t in ordinary))
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]),
                                   TANK_PER_ATTACK * len(ordinary))

    def test_attack_speed_shortens_the_lock(self):
        # the window is three of his own attacks, so tripling his attack
        # speed thirds the time they take
        windows = []
        for attack_speed in (1.0, 3.0):
            _, res = fight(self.spec(attack_speed))
            ki = hit_times(res, "ki strike")
            windows.append(ki[2] - ki[0])
        self.assertAlmostEqual(windows[0], 3.0 * windows[1], delta=1e-9)

    def test_damage_taken_before_the_third_strike_buys_no_mana(self):
        calm = self.spec()
        for kit in calm["kits"].values():
            kit["hpStar"] = 10.0 ** 6
        _, res = fight(calm)
        casts = casts_of(res)
        release = hit_times(res, "ki strike")[2]
        hit = 2000.0                        # worth the whole per-hit mana cap
        # inside: between the cast landing and the third ki strike
        inside = fight(one_blow(calm, 0.5 * (casts[0][2] + release), hit))[1]
        self.assertEqual(len(events(inside, "take")), 1)
        self.assertEqual(casts_of(inside)[1][0], casts[1][0])
        after = fight(one_blow(calm, release + 0.5, hit))[1]
        self.assertLess(casts_of(after)[1][0], casts[1][0])


class TestEffectDurationLocks(unittest.TestCase):
    """Elise and Vi: "The mana lock lasts the N s the effect runs. Attacks
    continue." — N is the kit's own row, and no second is added after it."""

    def locked(self, name, row, seconds, star=3):
        spec = timing_spec(name, star=star, duration=60.0, attack_speed=3.0)
        self.assertEqual(spec["unit"]["kind"], "Tank")
        hold = spec["kits"]["base"]["rows"][row]
        self.assertEqual(hold, seconds)                 # TFTraits' seconds, from the kit
        sheet, res = fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        seen_early = False
        for i in range(len(casts) - 1):
            (start, _, landed), end = casts[i], casts[i + 1][0]
            locked = [t for t in attack_times(res, start, end) if t < landed + hold]
            free = [t for t in attack_times(res, start, end) if t >= landed + hold]
            self.assertGreaterEqual(len(locked), 3)
            seen_early |= any(t < landed + hold + 1.0 for t in free)
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]),
                                   TANK_PER_ATTACK * len(free))
        self.assertTrue(seen_early)     # an attack inside the old extra second paid mana
        return casts, res

    def test_vi_is_locked_for_the_roar_and_no_longer(self):
        self.locked("Vi", "SpellDuration", 3)

    def test_elise_is_locked_for_the_decaying_attack_speed(self):
        casts, res = self.locked("Elise", "ASBuffDuration", 4)
        # The first cast is the transform, which grants no attack speed;
        # TFTraits publishes one lock for the spell, so it is held for the
        # same four seconds, which the loop above already checked.
        fangs = hit_times(res, "spider fangs")
        self.assertTrue(fangs)
        self.assertGreater(fangs[0], casts[0][2])


class TestChargeAndFeatherLocks(unittest.TestCase):
    def test_tristana_is_locked_for_the_charge_and_not_a_second_longer(self):
        spec = timing_spec("Tristana", duration=40.0, attack_speed=1.5)
        charge = spec["kits"]["base"]["rows"]["Duration"]
        sheet, res = fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        seen_early = False
        for i in range(len(casts) - 1):
            (start, _, landed), end = casts[i], casts[i + 1][0]
            locked = [t for t in attack_times(res, start, end) if t < landed + charge]
            free = [t for t in attack_times(res, start, end) if t >= landed + charge]
            self.assertTrue(locked)
            seen_early |= any(t < landed + charge + 1.0 for t in free)
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), 10.0 * len(free))
        self.assertTrue(seen_early)       # an attack inside the old extra second paid mana

    def test_xayah_gains_mana_again_right_after_the_fifth_feather(self):
        spec = timing_spec("Xayah", star=3, duration=40.0, attack_speed=2.0)
        self.assertEqual(spec["kits"]["base"]["rows"]["NumAttacks"], 5)
        sheet, res = fight(spec)
        casts = casts_of(res)
        self.assertGreaterEqual(len(casts), 3)
        feathers = hit_times(res, "feathers")
        seen_early = False
        for i in range(len(casts) - 1):
            start, end = casts[i][0], casts[i + 1][0]
            thrown = [t for t in feathers if start <= t <= end]
            self.assertEqual(len(thrown), 5)
            ordinary = [t for t in attack_times(res, start, end) if t not in thrown]
            self.assertTrue(all(t > thrown[-1] for t in ordinary))
            seen_early |= any(t < thrown[-1] + 1.0 for t in ordinary)
            self.assertAlmostEqual(came_in(casts, i, sheet["manaMax"]), 10.0 * len(ordinary))
        self.assertTrue(seen_early)


if __name__ == "__main__":
    unittest.main()
