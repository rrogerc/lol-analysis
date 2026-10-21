"""Current item curves checked against independent native-stat arithmetic.

The 18.4b input archive pins the item values. Native modifiers distinguish
additive duplicate multipliers from Bramble's compound HP, and Sunfire
application clocks from the legacy synthetic continuous burn aura.
"""

from copy import deepcopy
import unittest

import tft
from test_tft_unit_profiles import plain_spec


class TestItemStatsFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.4b")
        cls.engine = tft.engine()
        cls.effects = tft.load_item_effects(18)

    def spec(self, name="Sivir", *, star=1, items=(), duration=0.01):
        # An inert high-HP target keeps these checks independent of tank
        # pressure, formation changes, ability drivers and clear timings.
        dummy = {"slots": [{"hp": 1000000.0, "armor": 0.0, "mr": 0.0,
                             "kind": "tank", "nearby": True, "streams": 0}],
                 "targetDebuffs": {}, "meleeRepositionSeconds": 0.0}
        spec = tft.cell_spec(
            self.snap, self.snap.unit(name), star, "clump", [], dummy,
            duration=duration, pressure=False, item_fx=self.effects,
            items=list(items), driver="Driver", cast_timing={})
        spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
        for kit in spec["kits"].values():
            kit["stats"].update(mana=1000000.0, initialMana=0.0)
        return spec

    def opening(self, spec):
        return self.engine.simulate(spec, False)[0]

    def test_blue_buff_duplicates_add_bonus_percentages(self):
        # Sivir starts at 50 AD and 100 AP. Each Blue gives 15% AD/15 AP;
        # the bonus multiplier is 1 + .10 * copies, preserving the base.
        for count, ad, ap in ((0, 50.0, 100.0), (1, 58.25, 116.5),
                              (2, 68.0, 136.0), (3, 79.25, 158.5)):
            with self.subTest(copies=count):
                spec = self.spec(items=("DA_BlueBuff",) * count)
                sheet = self.opening(spec)
                fx = self.engine.compose_fx(spec)
                self.assertAlmostEqual(fx["adapMult"], 1.0 + 0.10 * count)
                self.assertAlmostEqual(sheet["ad"], ad)
                self.assertAlmostEqual(sheet["ap"], ap)

    def test_blue_buff_scales_other_item_bonuses_without_scaling_base(self):
        # Deathcap gives 55 AP. One Blue scales (15 + 55) by 1.10;
        # two scale (30 + 55) by 1.20, rather than 1.21.
        for items, expected in (
                (("DA_BlueBuff", "DA_RabadonsDeathcap"), 177.0),
                (("DA_BlueBuff", "DA_BlueBuff", "DA_RabadonsDeathcap"), 202.0)):
            for ordered in (items, tuple(reversed(items))):
                with self.subTest(items=ordered):
                    self.assertAlmostEqual(self.opening(self.spec(items=ordered))["ap"], expected)
        spec = self.spec(items=("DA_BlueBuff", "DA_BlueBuff"))
        spec["traits"] = [{"api": "bonus-stats", "name": "Bonus stats",
                           "stats": [["adPct", 0.20], ["ap", 20.0]]}]
        sheet = self.opening(spec)
        # Total gained AD/AP is .50 / 50; the base stays 50 / 100.
        self.assertAlmostEqual(sheet["ad"], 80.0)
        self.assertAlmostEqual(sheet["ap"], 160.0)

    def test_adaptive_helm_duplicates_add_extra_mana(self):
        for count, multiplier in ((0, 1.0), (1, 1.15), (2, 1.30), (3, 1.45)):
            with self.subTest(copies=count):
                spec = self.spec(items=("DA_AdaptiveHelm",) * count)
                fx = self.engine.compose_fx(spec)
                self.assertAlmostEqual(fx["manaMult"], multiplier)
                self.assertAlmostEqual(fx["manaRegen"], 3.0 * count)
                # Sivir's caster role receives the other-role bonus per copy.
                sheet = self.opening(spec)
                self.assertAlmostEqual(sheet["ad"], 50.0 * (1.0 + 0.10 * count))
                self.assertAlmostEqual(sheet["ap"], 100.0 + 10.0 * count)

    def test_adaptive_helm_applies_combined_multiplier_to_attacks_and_regeneration(self):
        # One attack at t=0 gives the caster's 7 mana. During one second,
        # the role regenerates 2 plus 3 per Helm. All are multiplied once.
        for count, mana in ((0, 9.0), (1, 13.8), (2, 19.5), (3, 26.1)):
            with self.subTest(copies=count):
                spec = self.spec(items=("DA_AdaptiveHelm",) * count, duration=1.01)
                spec["role"]["manaRegen"] = 2.0
                _, result = self.engine.simulate(spec, False)
                self.assertEqual(result["attacks"], 1)
                self.assertAlmostEqual(result["probe"]["mana"], mana)

    def test_bramble_vest_health_compounds_per_copy(self):
        # Sett has 1200 base HP, or 2160 at two stars. Bramble's native
        # MultiplyCompound modifier applies 1.06 once per copy.
        for count, hp in ((0, 2160.0), (1, 2289.6),
                          (2, 2426.976), (3, 2572.59456)):
            with self.subTest(copies=count):
                spec = self.spec("Sett", star=2, items=("DA_BrambleVest",) * count)
                fx = self.engine.compose_fx(spec)
                self.assertAlmostEqual(fx["hpCompoundMult"], 1.06 ** count)
                self.assertAlmostEqual(fx["hpMult"], 1.06 ** count)
                self.assertAlmostEqual(self.opening(spec)["hp"], hp)
                self.assertAlmostEqual(self.engine.theory_opening(spec, False)["hp"], hp)

    def test_additive_health_pool_precedes_bramble_compound_multiplier(self):
        cases = (
            # (2160 + 500) * (1 + .18 + .06) * 1.06.
            (("DA_WarmogsArmor", "DA_DragonsClaw", "DA_BrambleVest"), 3496.304),
            # (2160 + 500 + 150) * (1 + .18 + .08) * 1.06.
            (("DA_WarmogsArmor", "DA_SunfireCape", "DA_BrambleVest"), 3753.036),
        )
        for items, hp in cases:
            for ordered in (items, tuple(reversed(items))):
                with self.subTest(items=ordered):
                    spec = self.spec("Sett", star=2, items=ordered)
                    self.assertAlmostEqual(self.opening(spec)["hp"], hp)
                    self.assertAlmostEqual(self.engine.theory_opening(spec, False)["hp"], hp)
        # Additive traits remain in the shared pool before compounding.
        spec = self.spec("Sett", star=2, items=("DA_WarmogsArmor", "DA_BrambleVest"))
        spec["traits"] = [{"api": "health-trait", "name": "Health trait",
                           "stats": [["hp", 120.0], ["hpMult", 1.25]]}]
        # (2160 + 500 + 120) * (1 + .18 + .25) * 1.06.
        self.assertAlmostEqual(self.opening(spec)["hp"], 4213.924)

    def test_warmog_and_dragon_health_percentages_remain_additive(self):
        for items, hp in (
                (("DA_WarmogsArmor",) * 2, 4297.6),
                (("DA_DragonsClaw",) * 2, 2419.2)):
            with self.subTest(items=items):
                spec = self.spec("Sett", star=2, items=items)
                self.assertAlmostEqual(self.engine.compose_fx(spec)["hpCompoundMult"], 1.0)
                self.assertAlmostEqual(self.opening(spec)["hp"], hp)

    def test_timed_flat_health_uses_compound_factor_in_both_schedulers(self):
        # Initial 1000 HP * 1.20 * 1.06 = 1272. A timed +100 HP grant
        # receives the same 1.272 multiplier, then 100 raw pressure lands.
        spec = plain_spec(incoming_dps=100.0, pressure_interval=0.5,
                          fx=[{"hpCompoundMult": 1.06}])
        spec["traits"] = [{"api": "health-grant", "name": "Health grant",
                           "stats": [["hpMult", 1.20]],
                           "timedStats": [{"after": 1.0, "interval": 0.0,
                                           "stats": [["hp", 100.0]]}]}]
        spec["enemyDebuffs"] = {"wound": 1.0}
        for mode in ("standalone", "shared theory"):
            with self.subTest(mode=mode):
                if mode == "standalone":
                    opening, samples = self.engine.measure_response(spec, [1.0])
                else:
                    opening = self.engine.theory_opening(spec, True)
                    result = self.engine.measure_theory_team(
                        [spec], [True], 1.0, 100.0, 1.0, 8.0, 0.0)
                    samples = result["samples"]
                self.assertAlmostEqual(opening["hp"], 1272.0)
                self.assertAlmostEqual(samples[0]["hp"], 1299.2)
                self.assertEqual(samples[0]["incomingSpent"], 100.0)

    def test_quicksilver_attack_speed_continues_after_immunity_expires(self):
        spec = self.spec(items=("DA_Quicksilver",), duration=20.01)
        fx = self.engine.compose_fx(spec)
        self.assertEqual(fx["ccImmuneDuration"], 18.0)
        self.assertEqual(fx["asPerSecond"][0][1], None)
        _, result = self.engine.simulate(spec, False)
        # Twenty one-second stacks, rather than stopping after eighteen.
        self.assertAlmostEqual(result["probe"]["asStack"], 0.60)

    def test_sunfire_keeps_source_cadence_and_each_equipped_copy(self):
        for count in (1, 2, 3):
            with self.subTest(copies=count):
                spec = self.spec("Sett", items=("DA_SunfireCape",) * count)
                for item in spec["items"]:
                    self.assertEqual(item["sunfireAura"], [0.01, 10.0, 2.0, 2.0])
                fx = self.engine.compose_fx(spec)
                self.assertEqual(fx["sunfireAuras"], [(0.01, 10.0, 2.0, 2.0)] * count)
                self.assertIsNone(fx["burnAura"])

    def test_sunfire_range_uses_equipped_adaptor_form(self):
        for item, form, enabled in (("DA_Deathblade", "AD", True),
                                     ("DA_RabadonsDeathcap", "AP", False)):
            with self.subTest(form=form):
                spec = self.spec("Nidalee", items=("DA_SunfireCape", item, item))
                fx = self.engine.compose_fx(spec)
                self.assertEqual(fx["form"], form)
                self.assertEqual(bool(fx["sunfireAuras"]), enabled)
        spec = self.spec(items=("DA_SunfireCape",))
        self.assertNotIn("sunfireAura", spec["items"][0])
        self.assertEqual(self.engine.compose_fx(spec)["sunfireAuras"], [])

    def test_sunfire_rejects_invalid_application_intervals_and_ranges(self):
        for value in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(interval=value), self.assertRaises(ValueError):
                spec = self.spec("Sett", items=("DA_SunfireCape",))
                spec["items"][0]["sunfireAura"][2] = value
                self.engine.compose_fx(spec)
        for values in ([0.01, 10.0, 2.0], [0.01, 0.0, 2.0, 2.0],
                       [1.1, 10.0, 2.0, 2.0], [0.01, 10.0, 2.0, float("nan")]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                spec = self.spec("Sett", items=("DA_SunfireCape",))
                spec["items"][0]["sunfireAura"] = deepcopy(values)
                self.engine.compose_fx(spec)

    def test_legacy_synthetic_burn_aura_remains_supported(self):
        spec = self.spec("Sett")
        spec["items"].append({"api": "synthetic-aura", "name": "Synthetic aura",
                              "stats": [], "burnAura": [0.01, 3.0]})
        fx = self.engine.compose_fx(spec)
        self.assertEqual(fx["burnAura"], (0.01, 3.0))
        self.assertEqual(fx["sunfireAuras"], [])

    def test_red_buff_requires_attack_or_spell_while_morello_accepts_unit_damage(self):
        for api, requires_tags in (("DA_RedBuff", True), ("DA_Morellonomicon", False)):
            with self.subTest(item=api):
                spec = self.spec(items=(api,))
                self.assertEqual(spec["items"][0].get("burnRequiresAttackOrSpell", False), requires_tags)
                fx = self.engine.compose_fx(spec)
                self.assertEqual(fx["burnRequiresTags"], [requires_tags])
                # Existing callers still receive rate/duration/stacks tuples.
                self.assertEqual(len(fx["burnOnHit"][0]), 3)
                self.assertFalse(fx["burnOnHit"][0][2])
        spec = self.spec()
        spec["traits"] = [{"api": "trait-burn", "name": "Trait burn",
                           "stats": [], "burnOnHit": [0.01, 3.0]}]
        fx = self.engine.compose_fx(spec)
        self.assertEqual(fx["burnOnHit"], [(0.01, 3.0, True)])
        self.assertEqual(fx["burnRequiresTags"], [True])


if __name__ == "__main__":
    unittest.main()
