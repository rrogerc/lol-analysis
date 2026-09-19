"""Hand-computed response measurements, independent of opponent rosters."""

from copy import deepcopy
import math
import unittest
from unittest.mock import patch

import tft
from tft_unit_profiles import UnitProfiles, generic_targets, residual_mixed_ehp
from test_tft import ENGINE, SNAP, spec_for


def plain_spec(*, hp=1000, armor=0, mr=0, damage=100, target_armor=0,
               incoming_dps=0, physical_share=1, pressure_interval=1, fx=()):
    """One known attack per second, no casts, crits or other role bonuses."""
    dummy = generic_targets(incoming_dps=incoming_dps, physical_share=physical_share,
                            target_hp=150, target_armor=target_armor, target_mr=0,
                            target_count=1, pressure_interval=pressure_interval)
    spec = deepcopy(spec_for("Ashe", star=1, dummy=dummy, driver="Driver",
                             pressure=incoming_dps > 0, fx=fx))
    spec.update(immortal=True, enemyDebuffs=dummy["enemyDebuffs"], targetDebuffs={})
    kit = spec["kits"]["base"]
    kit.update(baseAd=damage, hpStar=hp)
    kit["stats"].update(hp=hp, ad=damage, armor=armor, mr=mr, mana=10**9,
                         initialMana=0, critChance=0, critMult=1)
    kit["stats"]["as"] = 1.0
    spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
    return spec


class TestAbstractInputs(unittest.TestCase):
    def test_generic_sources_exactly_conserve_the_requested_raw_dps(self):
        for physical_share in (0, 0.35, 1):
            for count in (1, 3, 8):
                with self.subTest(share=physical_share, count=count):
                    dummy = generic_targets(incoming_dps=1320, physical_share=physical_share,
                                            target_count=count, pressure_interval=0.8,
                                            target_hp=2345, target_armor=123, target_mr=87)
                    physical = sum(slot["ad"] * slot["as"] for slot in dummy["slots"])
                    magic = sum(slot["ability"] / slot["castInterval"]
                                for slot in dummy["slots"] if slot["castInterval"])
                    self.assertAlmostEqual(physical, 1320 * physical_share)
                    self.assertAlmostEqual(magic, 1320 * (1 - physical_share))
                    self.assertEqual(dummy["critEv"], 1.0)
                    self.assertTrue(all((slot["hp"], slot["armor"], slot["mr"])
                                        == (2345, 123, 87) for slot in dummy["slots"]))

    def test_invalid_abstract_inputs_fail_before_simulation(self):
        for values in ({"incoming_dps": -1}, {"incoming_dps": math.inf},
                       {"target_hp": 0}, {"target_count": 9}, {"target_count": True},
                       {"target_armor": math.nan}, {"physical_share": 1.1},
                       {"wound": -0.1}, {"pressure_interval": 0}, {"target_sunder": 1.1},
                       {"target_shred": -1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                generic_targets(**values)


class TestNativeResponse(unittest.TestCase):
    def test_opening_ehp_matches_resistance_and_durability_formula(self):
        spec = plain_spec(hp=1000, armor=100, mr=50, fx=[{"durability": 0.2}])
        opening, _ = ENGINE.measure_response(spec, [1.0])
        self.assertEqual(opening["physicalEhp"], 2500)
        self.assertEqual(opening["magicEhp"], 1875)

    def test_damage_samples_include_exact_time_attacks_and_do_not_kill_targets(self):
        # 100 raw AD / (1 + 100 armor / 100) = 50 damage per attack.
        # The target only has 150 HP, yet damage keeps accumulating past it.
        _, samples = ENGINE.measure_response(plain_spec(target_armor=100), [0, 0.5, 1, 2, 4])
        self.assertEqual([sample["damage"] for sample in samples], [50, 50, 100, 150, 250])
        self.assertTrue(all(sample["alive"] for sample in samples))

    def test_measurement_horizon_does_not_change_earlier_samples(self):
        spec = plain_spec(incoming_dps=100, fx=[{"healPerInterval": [0.2, 1.0]}])
        _, short = ENGINE.measure_response(spec, [0, 0.5, 1, 2])
        _, long = ENGINE.measure_response(spec, [0, 0.5, 1, 2, 8])
        self.assertEqual(short, long[:4])

    def test_damage_and_effective_sustain_stop_when_the_unit_dies(self):
        _, samples = ENGINE.measure_response(plain_spec(hp=150, incoming_dps=100), [0, 1, 2, 10])
        self.assertEqual([sample["damage"] for sample in samples], [100, 200, 200, 200])
        self.assertEqual([sample["alive"] for sample in samples], [True, True, False, False])
        self.assertEqual(samples[-1]["unitAliveTime"], 2)
        self.assertEqual(samples[-1]["aliveTime"], 2)
        self.assertEqual(samples[-1]["selfHeal"], 0)
        self.assertEqual(samples[-1]["selfShield"], 0)

    def test_only_consumed_shield_counts_and_unused_shield_expires(self):
        spec = plain_spec(incoming_dps=100, pressure_interval=0.25,
                          fx=[{"shieldAtStart": [0.2, 0.5]}])
        _, samples = ENGINE.measure_response(spec, [0, 0.25, 0.5, 0.75])
        self.assertEqual([sample["selfShield"] for sample in samples], [0, 25, 25, 25])
        self.assertEqual([sample["shieldHp"] for sample in samples], [200, 175, 0, 0])
        self.assertEqual(samples[-1]["hp"], 950)

    def test_healing_is_limited_by_missing_health_and_wound_before_that_limit(self):
        for wound, expected in ((0, 150), (0.5, 100), (1, 0)):
            with self.subTest(wound=wound):
                # 400 raw DPS through 100 armor: 50 HP per 0.25 s.
                # At 1 second the heal occurs before that tick's hit, so
                # 150 HP are missing when the nominal 200 heal is applied.
                spec = plain_spec(incoming_dps=400, armor=100, pressure_interval=0.25,
                                  fx=[{"healPerInterval": [0.2, 1.0]}])
                spec["enemyDebuffs"] = {"wound": wound}
                _, samples = ENGINE.measure_response(spec, [1])
                self.assertEqual(samples[0]["selfHeal"], expected)
                self.assertEqual(samples[0]["hp"], 1000 - 200 + expected)
        _, samples = ENGINE.measure_response(
            plain_spec(fx=[{"healPerInterval": [0.2, 1.0]}]), [5])
        self.assertEqual(samples[0]["selfHeal"], 0)

    def test_damage_mix_changes_survival_by_the_corresponding_resistance(self):
        for share, expected in ((1, 10), (0, 5), (0.5, 7)):
            with self.subTest(share=share):
                spec = plain_spec(hp=500, armor=100, mr=0, incoming_dps=100, physical_share=share)
                _, samples = ENGINE.measure_response(spec, [20])
                self.assertEqual(samples[0]["aliveTime"], expected)

    def test_ally_output_is_reported_as_potential_without_crediting_self_sustain(self):
        for name, field in (("Alistar", "allyHealPotential"), ("Ivern", "allyShieldPotential")):
            with self.subTest(unit=name):
                spec = spec_for(name, star=2, dummy=generic_targets(), pressure=False)
                spec["kits"]["base"]["stats"].update(initialMana=10**6, mana=10**6)
                _, samples = ENGINE.measure_response(spec, [5])
                self.assertGreater(samples[0][field], 0)
                self.assertEqual(samples[0]["selfHeal"], 0)
                self.assertEqual(samples[0]["selfShield"], 0)

    def test_invalid_native_times_are_rejected(self):
        spec = plain_spec()
        for times in ([], [-1], [math.nan], [math.inf], [1, 1], [2, 1]):
            with self.subTest(times=times), self.assertRaises(ValueError):
                ENGINE.measure_response(spec, times)

    def test_residual_ehp_includes_active_shields_and_physical_attack_reduction(self):
        spec = plain_spec(hp=1000, armor=100, mr=50, incoming_dps=400,
                          fx=[{"durability": 0.2}, {"attackDamageTaken": 0.5},
                              {"shieldAtStart": [0.2, 5]}])
        opening, samples = ENGINE.measure_response(spec, [0, 1])
        # 1,200 HP+shield / (.5 armor * .8 durability * .5 attack reduction).
        self.assertEqual(opening["attackDamageTaken"], 0.5)
        self.assertEqual(opening["residualPhysicalEhp"], 6000)
        self.assertEqual(opening["residualMagicEhp"], 2250)
        self.assertEqual(samples[0]["residualPhysicalEhp"], 6000)
        self.assertEqual(samples[1]["residualPhysicalEhp"], 5600)
        self.assertEqual(samples[1]["incomingSpent"], 400)
        self.assertEqual(samples[1]["incomingSpent"] + samples[1]["residualPhysicalEhp"], 6000)

    def test_raw_lethal_overkill_is_not_credited_as_usable_ehp(self):
        spec = plain_spec(hp=150, armor=100, incoming_dps=1000)
        _, samples = ENGINE.measure_response(spec, [1, 8])
        for sample in samples:
            self.assertEqual(sample["incoming"], 1000)
            self.assertEqual(sample["incomingSpent"], 300)
            self.assertEqual(sample["residualPhysicalEhp"], 0)
            self.assertEqual(sample["residualMagicEhp"], 0)
            self.assertEqual(sample["residualPools"], [])

    def test_spawned_bodies_keep_their_own_defenses_and_each_discards_overkill(self):
        dummy = generic_targets(incoming_dps=300, physical_share=1, target_count=1,
                                sunder=0.5)
        spec = spec_for("Krug", star=1, dummy=dummy, pressure=True,
                        fx=[{"attackDamageTaken": 0.5}])
        kit = spec["kits"]["base"]
        kit.update(hpStar=100, baseAd=0)
        kit["stats"].update(hp=100, ad=0, armor=0, mr=0, mana=10**9, initialMana=0)
        kit["calcs"]["HealthCalc1"] = {
            "dtype": "magic", "terms": [{"type": "flat", "value": 100, "op": "add"}]}
        spec["unit"]["extras"]["TFT18_KrugMini"].update(armor=100, mr=0)
        _, samples = ENGINE.measure_response(spec, [1, 2, 3])
        # Krug's own 100 HP / .5 attack reduction consumes 200 raw damage.
        # Two minis each have 100 HP and 50 armor after Sunder: 150 EHP.
        # Neither mini inherits Krug's attack reduction.
        self.assertEqual([row["incoming"] for row in samples], [300, 600, 900])
        self.assertEqual([row["incomingSpent"] for row in samples], [200, 350, 500])
        self.assertEqual([row["residualPhysicalEhp"] for row in samples], [300, 150, 0])
        self.assertEqual([row["residualMagicEhp"] for row in samples], [200, 100, 0])
        self.assertEqual([len(row["residualPools"]) for row in samples], [2, 1, 0])
        self.assertTrue(samples[0]["holding"])
        self.assertFalse(samples[0]["alive"])
        self.assertTrue(all(row["incomingSpent"] + row["residualPhysicalEhp"] == 500
                            for row in samples))

    def test_heterogeneous_bodies_are_mixed_before_their_ehp_is_added(self):
        pools = [{"physicalEhp": 400, "magicEhp": 100},
                 {"physicalEhp": 100, "magicEhp": 400}]
        # Each 50/50 pool has 160 mixed EHP. Mixing aggregate pure-type
        # totals would falsely report 500 instead of the correct 320.
        self.assertEqual(residual_mixed_ehp(pools, 0.5), 320)
        self.assertEqual(residual_mixed_ehp(pools, 1), 500)
        self.assertEqual(residual_mixed_ehp(pools, 0), 500)


class TestUnitProfiles(unittest.TestCase):
    def test_explicit_opening_mask_is_forwarded_without_entering_target_conditions(self):
        profiles = UnitProfiles(SNAP, "clump")
        api = SNAP.unit("Malphite")["api"]
        spec = {"unit": {"kind": "Tank", "form": None}}
        sample = {"targetable": True, "residualPools": [{"physicalEhp": 200, "magicEhp": 100}]}
        with patch.object(profiles, "spec", return_value=spec) as build, patch("tft.engine") as engine:
            engine.return_value.theory_opening.return_value = sample
            result = profiles.theory_opening(api, 2, [], [], source_mask=5,
                                              physical_share=0.5, target_count=3)
        build.assert_called_once_with(api, 2, [], [], alpha=False, physical_share=0.5, target_count=3)
        engine.return_value.theory_opening.assert_called_once_with(spec, True, source_mask=5)
        self.assertTrue(result["opening"]["targetable"])
        self.assertAlmostEqual(result["opening"]["residualMixedEhp"], 400 / 3)

    def test_default_theory_opening_preserves_native_default_call(self):
        profiles = UnitProfiles(SNAP, "clump")
        api = SNAP.unit("Malphite")["api"]
        spec = {"unit": {"kind": "Tank", "form": None}}
        with patch.object(profiles, "spec", return_value=spec), patch("tft.engine") as engine:
            engine.return_value.theory_opening.return_value = {"residualPools": []}
            result = profiles.theory_opening(api, 2, [], [])
        engine.return_value.theory_opening.assert_called_once_with(spec, True)
        self.assertNotIn("targetable", result["opening"])

    def test_team_priority_and_exported_source_diagnostics_survive_sample_translation(self):
        profiles = UnitProfiles(SNAP, "spread")
        specs, fronts, order = [{"test": "first"}, {"test": "second"}], [True, True], [1, 0]
        raw = {"targetOrder": order[:], "initialSourceTargets": [1, 0, 1],
               "samples": [{"residualPools": [{"physicalEhp": 200, "magicEhp": 100}]},
                           {"residualPools": []}], "elapsed": 1}
        with patch("tft.engine") as engine:
            engine.return_value.measure_theory_team.return_value = raw
            result = profiles.measure_team(specs, fronts, window=1, pressure=100,
                                            physical_share=0.5, target_order=order)
        engine.return_value.measure_theory_team.assert_called_once_with(
            specs, fronts, 1, 100, 0.5, 8.0, 0.0, target_order=order)
        self.assertEqual(result["targetOrder"], [1, 0])
        self.assertEqual(result["initialSourceTargets"], [1, 0, 1])
        self.assertAlmostEqual(result["samples"][0]["residualMixedEhp"], 400 / 3)
        self.assertEqual(result["samples"][1]["residualMixedEhp"], 0)

    def test_cache_separates_pressure_items_and_trait_effects(self):
        profiles = UnitProfiles(SNAP, "clump")
        api = SNAP.unit("Ashe")["api"]
        base = profiles.curve(api, 2, [], [], [2, 8])
        self.assertIs(base, profiles.curve(api, 2, [], [], [2, 8]))
        pressure = profiles.curve(api, 2, [], [], [2, 8], incoming_dps=1000)
        items = profiles.curve(api, 2, [], ["DA_InfinityEdge"], [2, 8])
        traits = profiles.curve(api, 2, [{"api": "test", "stats": [["asPct", 0.3]]}], [], [2, 8])
        self.assertLess(pressure["samples"][-1]["damage"], base["samples"][-1]["damage"])
        self.assertGreater(items["samples"][-1]["damage"], base["samples"][-1]["damage"])
        self.assertGreater(traits["samples"][-1]["damage"], base["samples"][-1]["damage"])
        self.assertEqual(profiles.stats["measurements"], 4)
        self.assertEqual(profiles.stats["cacheHits"], 1)

    def test_nidalee_form_changes_role_without_mutating_the_snapshot(self):
        profiles = UnitProfiles(SNAP, "clump")
        api = SNAP.unit("Nidalee")["api"]
        before = deepcopy(SNAP.units[api])
        for item, form, kind in (("DA_SteraksGage", "AD", "Assassin"),
                                 ("DA_RabadonsDeathcap", "AP", "Marksman")):
            with self.subTest(form=form):
                curve = profiles.curve(api, 2, [], ["DA_SpearOfShojin", item], [5])
                self.assertEqual((curve["form"], curve["kind"]), (form, kind))
        self.assertEqual(SNAP.units[api], before)

    def test_every_champion_has_finite_nonnegative_observations(self):
        profiles = UnitProfiles(SNAP, "clump")
        for api in SNAP.units:
            for pressure in (0, 1200):
                with self.subTest(unit=api, pressure=pressure):
                    curve = profiles.curve(api, 2, [], [], [2, 8, 32], incoming_dps=pressure)
                    for field in ("physicalEhp", "magicEhp"):
                        self.assertGreater(curve["opening"][field], 0)
                        self.assertTrue(math.isfinite(curve["opening"][field]))
                    for sample in curve["samples"]:
                        for field in ("damage", "selfHeal", "selfShield", "aliveTime", "unitAliveTime",
                                      "incomingSpent", "residualPhysicalEhp", "residualMagicEhp",
                                      "residualMixedEhp"):
                            self.assertTrue(math.isfinite(sample[field]))
                            self.assertGreaterEqual(sample[field], 0)

    def test_shared_target_debuff_does_not_stack_with_the_same_local_effect(self):
        profiles = UnitProfiles(SNAP, "clump")
        api = SNAP.unit("Ashe")["api"]
        spec = profiles.spec(api, 2, [], ["DA_LastWhisper"], target_sunder=0.3, target_shred=0.3)
        self.assertEqual(spec["targetDebuffs"], {"sunder": 0.3, "shred": 0.3})
        self.assertEqual(spec["enemyDebuffs"], {"wound": 0.0, "sunder": 0.0, "shred": 0.0})
        stripped = deepcopy(spec)
        for item in stripped["items"]:
            item.pop("sunderOnHit", None)
        self.assertEqual(ENGINE.measure_response(spec, [2, 8]),
                         ENGINE.measure_response(stripped, [2, 8]))

    def test_burn_channel_suppression_keeps_stats_and_independent_channels(self):
        profiles = UnitProfiles(SNAP, "clump")
        api = SNAP.unit("Ashe")["api"]
        effects = [{"api": "DA_18_Inferno", "stats": [], "burnOnHit": [0.01, 10]}]
        items = ["DA_RedBuff"]
        baseline = profiles.spec(api, 2, effects, items)
        item_off = profiles.spec(api, 2, effects, items, item_burn=False)
        inferno_off = profiles.spec(api, 2, effects, items, inferno_burn=False)
        self.assertIn("burnOnHit", baseline["items"][0])
        self.assertNotIn("burnOnHit", item_off["items"][0])
        self.assertEqual(item_off["items"][0]["stats"], baseline["items"][0]["stats"])
        self.assertIn("burnOnHit", item_off["traits"][0])
        self.assertIn("burnOnHit", inferno_off["items"][0])
        self.assertNotIn("burnOnHit", inferno_off["traits"][0])
        self.assertEqual(baseline, profiles.spec(api, 2, effects, items))

    def test_native_burn_suppression_preserves_other_ability_output(self):
        profiles = UnitProfiles(SNAP, "clump")
        for name in ("Cinderling", "Brambleback"):
            with self.subTest(unit=name):
                api = SNAP.unit(name)["api"]
                effects = [{"api": "DA_Riftbeast18", "riftbeast": True}]
                kwargs = {"alpha": True}
                enabled = profiles.curve(api, 2, effects, [], [20], **kwargs)
                disabled = profiles.curve(api, 2, effects, [], [20], item_burn=False, **kwargs)
                self.assertGreater(enabled["samples"][0]["damage"], disabled["samples"][0]["damage"])
                self.assertGreater(disabled["samples"][0]["damage"], 0)
                self.assertEqual(enabled["opening"], disabled["opening"])


if __name__ == "__main__":
    unittest.main()
