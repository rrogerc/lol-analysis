"""Brambleback's confirmed numbers and explicit conservative Frenzy policy.

The pinned 18.1d lookup supplies 80% AD for eight seconds, 4% maximum-HP
Alpha healing per attack, and 7% AS per second for each Rageblade. Riot
documents the item's per-second rework and multiple-copy behavior here:
https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/patch-14-5-items-update/

Neither the tooltip nor the exported compatibility SpellObject establishes
Frenzy stacking or a duration-long mana lock:
https://raw.communitydragon.org/latest/game/characters/da_brambleback18.cdtb.bin.json
The single-active-buff tests below verify our conservative refresh policy,
not an independently verified claim about live recast behavior.

Since 2026-09-20 the driver adopts TFTraits' third-party statement that the
mana lock lasts the eight seconds Frenzy runs (test_tft_driver_fixes pins
it), so a live Frenzy is only ever refreshed by mana that ignores the lock.
"""

from copy import deepcopy
import unittest

import tft
from test_tft import ENGINE, SNAP, events, spec_for
from tft_unit_profiles import generic_targets


def controlled_brambleback(*, duration=1.1, incoming=0, interval=1,
                          alpha=False, fx=(), target_armor=0):
    spec = spec_for(
        "Brambleback", star=2, duration=duration, pressure=incoming > 0,
        dummy=generic_targets(incoming_dps=incoming, physical_share=1,
                              pressure_interval=interval, target_count=1,
                              target_armor=target_armor, target_mr=0),
        traits=[("DA_Riftbeast18", 1)] if alpha else [], fx=fx,
    )
    spec["immortal"] = True
    spec["role"] = {"manaRegen": 0, "asPct": 0}
    spec["unit"]["kind"] = "Specialist"  # no attack mana in the timing fixture
    kit = spec["kits"]["base"]
    kit.update(baseAd=100, hpStar=1000)
    kit["stats"].update(hp=1000, ad=100, armor=0, mr=0, mana=10**9,
                        initialMana=0, critChance=0, critMult=1)
    kit["stats"]["as"] = 1
    return spec


class TestBramblebackAlphaAndRageblade(unittest.TestCase):
    def test_pinned_alpha_heal_and_item_rows_support_the_hand_values(self):
        unit = SNAP.unit("Brambleback")
        self.assertIn('row="TraitMaxHealthHeal"', unit["ability"]["desc"])
        self.assertEqual(tft.curve_at(unit["curve"]["TraitMaxHealthHeal"], 2), 0.04)
        self.assertEqual(tft.curve_at(unit["curve"]["FrenzyADPercent"], 2), 0.8)
        self.assertEqual(tft.curve_at(unit["curve"]["Duration"], 2), 8)
        item = tft.item_spec(SNAP, "DA_GuinsoosRageblade",
                             tft.load_item_effects(SNAP.set_no), unit)
        self.assertFalse(item["unique"])
        self.assertAlmostEqual(item["asPerSecond"][0], 0.07)

    def test_alpha_heal_occurs_once_per_attack_after_wound_and_missing_hp_limit(self):
        for incoming, wound, expected in ((400, 0, 40), (400, 0.5, 20),
                                         (400, 1, 0), (10, 0, 10), (0, 0, 0)):
            with self.subTest(incoming=incoming, wound=wound):
                spec = controlled_brambleback(incoming=incoming, interval=0.25, alpha=True)
                spec["enemyDebuffs"]["wound"] = wound
                _, result = ENGINE.simulate(spec, True)
                # Two attacks at 0 and 1 seconds. The opening attack cannot
                # overheal; the next restores min(missing HP, 1000*.04*(1-wound)).
                self.assertEqual(result["attacks"], 2)
                self.assertEqual(result["healed"], expected)
                self.assertEqual(result["hpLeft"], 1000 - incoming + expected)
                self.assertEqual(sum(hit[2] for hit in events(result, "heal", "red buff")), expected)
                unmarked = deepcopy(spec)
                unmarked["traits"] = []
                self.assertEqual(ENGINE.simulate(unmarked, False)[1]["healed"], 0)

    def test_each_real_rageblade_adds_seven_percent_base_speed_per_second(self):
        for copies in (1, 2, 3):
            with self.subTest(copies=copies):
                spec = spec_for("Brambleback", pressure=False, duration=3.1,
                                items=["Guinsoo's Rageblade"] * copies)
                spec["role"] = {"manaRegen": 0, "asPct": 0}
                spec["kits"]["base"]["stats"].update(mana=10**9, initialMana=0)
                opening, result = ENGINE.simulate(spec, True)
                self.assertAlmostEqual(opening["ap"], 100 + 10 * copies)
                self.assertAlmostEqual(opening["as"], 0.55 * (1 + 0.1 * copies))
                self.assertAlmostEqual(result["probe"]["asStack"], 3 * 0.07 * copies)


class TestConservativeBramblebackFrenzy(unittest.TestCase):
    def test_recast_refreshes_one_buff_and_expiry_removes_only_that_buff(self):
        spec = controlled_brambleback(
            duration=12.1, incoming=25, target_armor=100,
            fx=[{"manaAtHp": [0.95, 40]}],
        )
        spec["kits"]["base"]["stats"].update(mana=40, initialMana=40)
        _, result = ENGINE.simulate(spec, True)
        # A single low-health mana trigger supplies the second cast. No
        # further mana is available, so the refreshed expiration is observable.
        self.assertEqual(result["castTimes"], [0, 3])
        self.assertEqual([hit[0] for hit in events(result, "land")], [0.25, 3.25])
        for hit in events(result, "damage", "auto"):
            with self.subTest(time=hit[0]):
                # One +80% AD and 40% personal ignore: 180/(1+60/100).
                # It stays active beyond the first expiration at 8.25s,
                # then ends at 11.25s. Opening/expired damage is 100/2.
                expected = 112.5 if 0.25 <= hit[0] < 11.25 else 50
                self.assertAlmostEqual(hit[2], expected)

    def test_real_three_rageblade_loadout_cannot_multiply_frenzy_ad_with_recasts(self):
        spec = spec_for(
            "Brambleback", star=2, duration=40.1, pressure=False,
            dummy=generic_targets(target_count=1, target_armor=100, target_mr=100),
            items=["Guinsoo's Rageblade"] * 3,
        )
        spec["immortal"] = True
        opening, result = ENGINE.simulate(spec, True)
        self.assertEqual(opening["ad"], 165)
        self.assertEqual(opening["ap"], 130)
        # Frenzy now locks mana for its eight seconds (the adopted TFTraits
        # rule, 2026-09-20), so ordinary mana recasts it only after it has
        # ended: several windows back to back, none overlapping. The refresh
        # of a live Frenzy is exercised above through a proc that ignores locks.
        self.assertGreaterEqual(result["casts"], 3)
        lands = [hit[0] for hit in events(result, "land")]
        self.assertTrue(all(later - earlier >= 8 for earlier, later in zip(lands, lands[1:])))
        # At 130 AP the personal ignore is 10% + 30%*1.3 = 49%.
        # Base crit EV is 1.1; no other trait, AD item or proc raises this bound.
        frenzy_hit = 165 * 1.8 * 1.1 / 1.51
        autos = events(result, "damage", "auto")
        self.assertTrue(any(abs(hit[2] - frenzy_hit) < 1e-8 for hit in autos))
        self.assertLessEqual(max(hit[2] for hit in autos), frenzy_hit + 1e-8)


if __name__ == "__main__":
    unittest.main()
