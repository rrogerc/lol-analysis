"""Permanent team Sunder/Shred against carry benchmark targets.

Run after jobs/build-engine.sh tft: python3 -m unittest test_tft_target_debuffs -v
"""

import copy
import unittest

from test_tft import DUMMY, ENGINE, events, immortal, one_hitter, spec_for


BASELINE = {"sunder": 0.3, "shred": 0.3}


def target_spec(unit="Ashe", *, debuffs=None, **kwargs):
    kwargs.setdefault("dummy", immortal(DUMMY))
    spec = copy.deepcopy(spec_for(unit, **kwargs))
    # Set the engine input explicitly, independent of benchmark defaults.
    spec["targetDebuffs"] = dict(BASELINE if debuffs is None else debuffs)
    return spec


def burst_spec(dtype, *, debuffs=None, fx=()):
    """A flat five-target cast, including two slots outside local aura range."""
    spec = target_spec("Hecarim", debuffs=debuffs, duration=0.4,
                       pressure=False, fx=fx)
    spec["dummies"]["slots"] = [
        dict(spec["dummies"]["slots"][0], hp=10 ** 6,
             armor=100.0 + i * 10, mr=200.0 + i * 20,
             nearby=i < 3, kind="tank" if i < 3 else "non-tank")
        for i in range(5)
    ]
    kit = spec["kits"]["base"]
    kit["stats"].update(initialMana=10 ** 6, mana=10 ** 6)
    kit["rows"]["NumEnemies"] = 5.0
    kit["calcs"]["MagicDamageCalc1"] = {
        "dtype": dtype,
        "terms": [{"type": "flat", "value": 170.0, "op": "add"}],
    }
    return spec


class TestTargetDebuffs(unittest.TestCase):
    def test_first_attack_uses_permanent_sunder(self):
        spec = target_spec(driver="Driver", duration=0.1)
        sheet, res = ENGINE.simulate(spec, True)
        hit = events(res, "damage", "auto")[0]
        self.assertEqual(hit[0], 0.0)
        self.assertAlmostEqual(hit[2], 112.5 * 1.1 * 100 / (100 + 110 * 0.7))
        self.assertEqual(sheet["armor"], 45.0)
        self.assertEqual(sheet["mr"], 45.0)

    def test_each_damage_type_uses_its_own_debuff_on_every_target(self):
        # Different armor/MR and fractions catch crossed fields, even on the
        # distant non-tanks that local item auras cannot reach.
        for dtype, resist, debuff in (("physical", "armor", "sunder"),
                                      ("magic", "mr", "shred")):
            for debuffs in ({}, {"sunder": 0.3}, {"shred": 0.25},
                            {"sunder": 0.3, "shred": 0.25},
                            {"sunder": 1.0, "shred": 1.0}):
                with self.subTest(dtype=dtype, debuffs=debuffs):
                    spec = burst_spec(dtype, debuffs=debuffs)
                    _, res = ENGINE.simulate(spec, True)
                    hits = events(res, "damage", "riders")
                    self.assertEqual([e[3] for e in hits], list(range(5)))
                    for hit, slot in zip(hits, spec["dummies"]["slots"]):
                        expected = 170 * 100 / (100 + slot[resist]
                                               * (1 - debuffs.get(debuff, 0)))
                        self.assertAlmostEqual(hit[2], expected)

    def test_true_damage_ignores_both_debuffs(self):
        for debuffs in ({}, BASELINE, {"sunder": 1.0, "shred": 1.0}):
            with self.subTest(debuffs=debuffs):
                _, res = ENGINE.simulate(burst_spec("true", debuffs=debuffs), True)
                self.assertEqual([e[2] for e in events(res, "damage", "riders")],
                                 [170.0] * 5)

    def test_equal_or_weaker_on_hit_and_aura_effects_do_not_stack(self):
        for unit, effect in (("Ashe", "sunder"), ("Ahri", "shred")):
            plain = target_spec(unit, duration=12.0)
            expected = ENGINE.simulate(plain, True)
            for pct in (0.15, 0.3):
                variants = [{effect + "OnHit": [pct, 3.0]},
                            {effect + "Aura": pct},
                            {effect + "OnHit": [pct, 3.0], effect + "Aura": pct}]
                for extra in variants:
                    with self.subTest(unit=unit, extra=extra):
                        spec = target_spec(unit, duration=12.0, fx=[extra])
                        self.assertEqual(ENGINE.simulate(spec, True), expected)

    def test_stronger_aura_overrides_baseline_only_within_its_range(self):
        for dtype, resist, aura in (("physical", "armor", "sunderAura"),
                                    ("magic", "mr", "shredAura")):
            with self.subTest(dtype=dtype):
                spec = burst_spec(dtype, fx=[{aura: 0.5}])
                _, res = ENGINE.simulate(spec, True)
                hits = events(res, "damage", "riders")
                self.assertEqual(len(hits), 5)
                for hit, slot in zip(hits, spec["dummies"]["slots"]):
                    pct = 0.5 if slot["nearby"] else 0.3
                    self.assertAlmostEqual(hit[2], 170 * 100 / (100 + slot[resist] * (1 - pct)))

    def test_stronger_timed_effect_expires_back_to_baseline(self):
        for dtype, resist, effect in (("physical", "armor", "sunderOnHit"),
                                      ("magic", "mr", "shredOnHit")):
            with self.subTest(dtype=dtype):
                spec = target_spec(duration=2.0, geometry="spread",
                                   fx=[{effect: [0.6, 0.75]}])
                kit = spec["kits"]["base"]
                # One opening cast applies the short debuff; the trail keeps
                # dealing damage after expiry without reapplying on-hit effects.
                kit["stats"].update(initialMana=10 ** 6, mana=10 ** 6)
                kit["stats"]["as"] = 0.01
                kit["rows"].update(RiftDuration=3.0, MaxHealthDamagePerSecond=0.0)
                kit["calcs"]["PhysicalDamageCalc2"] = {
                    "dtype": dtype,
                    "terms": [{"type": "flat", "value": 170.0, "op": "add"}],
                }
                expected = ENGINE.simulate(spec, True)
                _, res = expected
                self.assertEqual(res["attacks"], 1)
                self.assertEqual(res["casts"], 1)
                hits = events(res, "damage", "trail", target=0)
                self.assertEqual([e[0] for e in hits], [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0])
                resistance = spec["dummies"]["slots"][0][resist]
                for hit in hits:
                    pct = 0.6 if hit[0] < 1.0 else 0.3
                    self.assertAlmostEqual(hit[2], 170 * 0.25 * 100
                                           / (100 + resistance * (1 - pct)))
                # A redundant item must not donate its longer duration (or
                # the aura's unlimited duration) to the stronger effect.
                for pct in (0.15, 0.3):
                    for extra in ({effect: [pct, 5.0]},
                                  {effect.replace("OnHit", "Aura"): pct},
                                  {effect: [pct, 5.0], effect.replace("OnHit", "Aura"): pct}):
                        with self.subTest(dtype=dtype, extra=extra):
                            redundant = copy.deepcopy(spec)
                            redundant["items"].append({"api": "test", "name": "test", **extra})
                            self.assertEqual(ENGINE.simulate(redundant, True), expected)

    def test_last_whisper_and_void_staff_keep_stats_without_extra_reduction(self):
        for unit, name, effect, stat in (
                ("Ashe", "Last Whisper", "sunderOnHit", "ad"),
                ("Ahri", "Void Staff", "shredOnHit", "ap")):
            for count in (1, 2):
                with self.subTest(item=name, count=count):
                    spec = target_spec(unit, items=[name] * count, duration=12.0)
                    full = ENGINE.simulate(spec, True)
                    without_passive = copy.deepcopy(spec)
                    for item in without_passive["items"]:
                        self.assertEqual(item.pop(effect)[0], 0.3)
                    self.assertEqual(ENGINE.simulate(without_passive, True), full)
                    bare = target_spec(unit, duration=12.0)
                    bare_sheet, bare_res = ENGINE.simulate(bare, True)
                    self.assertGreater(full[0][stat], bare_sheet[stat])
                    self.assertGreater(full[1]["total"], bare_res["total"])
                    # With no team-applied baseline their real passive still
                    # contributes, so equality above cannot pass with inert FX.
                    spec["targetDebuffs"] = {}
                    without_passive["targetDebuffs"] = {}
                    self.assertGreater(ENGINE.simulate(spec, False)[1]["total"],
                                       ENGINE.simulate(without_passive, False)[1]["total"])

    def test_kayles_native_shred_is_redundant_and_does_not_extend_stronger_shred(self):
        for level in (20.0, 30.0):
            for fx in ([], [{"shredOnHit": [0.6, 0.25]}]):
                with self.subTest(level=level, fx=fx):
                    spec = target_spec("Kayle", duration=3.0, fx=fx)
                    spec["kits"]["base"]["rows"]["ShredLevel"] = level
                    # Solar damage from the next auto lands before its on-hit
                    # reapplies stronger Shred, exposing any extended duration.
                    spec["traits"].append({"name": "test", "bonusMagicPct": 0.2})
                    suppressed = copy.deepcopy(spec)
                    suppressed["kits"]["base"]["rows"].update(ShredLevel=0.0,
                                                               ShredDuration=0.0)
                    expected = ENGINE.simulate(suppressed, True)
                    self.assertGreater(expected[1]["attacks"], 1)
                    self.assertEqual(ENGINE.simulate(spec, True), expected)

    def test_target_debuffs_do_not_reduce_the_simulated_unit(self):
        spec = target_spec("Warwick", driver="Driver", duration=1.1,
                           dummy=one_hitter(100.0), pressure=True)
        spec["enemyDebuffs"] = {"sunder": 0.2, "shred": 0.1}
        spec["kits"]["base"]["stats"].update(armor=100.0, mr=200.0)
        sheet, res = ENGINE.simulate(spec, True)
        spec["targetDebuffs"] = {}
        clean_sheet, clean_res = ENGINE.simulate(spec, True)
        self.assertEqual(sheet, clean_sheet)
        self.assertEqual(events(res, "take"), events(clean_res, "take"))
        self.assertAlmostEqual(res["taken"], 100 * 100 / (100 + 100 * 0.8))
        self.assertGreater(res["total"], clean_res["total"])

    def test_absent_target_debuffs_preserve_legacy_on_hit_timing(self):
        spec = target_spec(debuffs={}, driver="Driver", duration=1.5,
                           fx=[{"sunderOnHit": [0.3, 3.0]}])
        expected = ENGINE.simulate(spec, True)
        spec.pop("targetDebuffs")
        self.assertEqual(ENGINE.simulate(spec, True), expected)
        hits = events(expected[1], "damage", "auto")
        self.assertAlmostEqual(hits[0][2], 112.5 * 1.1 * 100 / 210)
        self.assertAlmostEqual(hits[1][2], 112.5 * 1.1 * 100 / (100 + 110 * 0.7))

    def test_debuff_fractions_reject_invalid_values(self):
        for key in ("sunder", "shred"):
            for value in (-0.01, 1.01, float("nan"), float("inf"), float("-inf")):
                with self.subTest(key=key, value=value):
                    spec = target_spec(debuffs={key: value}, duration=0.1)
                    with self.assertRaisesRegex(ValueError, "targetDebuffs\\." + key):
                        ENGINE.simulate(spec, False)


if __name__ == "__main__":
    unittest.main()
