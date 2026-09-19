"""Trait corrections checked against archived sources and native health budgets."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

import tft
from tft_comp_traits import resolve_board_traits
from tft_unit_profiles import UnitProfiles
from test_tft_unit_profiles import plain_spec


class TestAuditedClassTraits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.hand = tft.load_trait_effects(18)

    def test_vanguard_capstone_mapping_preserves_its_shield_condition(self):
        api = "DA_18_Vanguard"
        raw = next(trait for trait in self.snap.raw["traits"] if trait["apiName"] == api)
        self.assertIn("while Shielded", raw["effects"][2]["desc"])
        for column, shield, durability in ((1, .18, 0), (2, .32, 0), (3, .42, .05)):
            effect = tft.trait_spec(self.snap, api, column, self.hand, self.snap.unit("Rakan"))
            self.assertNotIn("durability", effect)
            self.assertAlmostEqual(effect["durabilityWhileShielded"], durability)
            self.assertEqual(effect["shieldAtStart"], [shield, 10])
            self.assertEqual(effect["shieldAtHp"], [.5, shield, 10])

    def test_juggernaut_four_team_share_matches_archived_explicit_source(self):
        api = "DA_Juggernaut18"
        raw = next(trait for trait in self.snap.raw["traits"] if trait["apiName"] == api)
        source = next(trait for trait in self.snap.communitydragon["traits"] if trait["apiName"] == api)
        source_four = next(effect for effect in source["effects"] if effect["minUnits"] == 4)
        # The sparse lookup omits column 2; correct only this known conflict,
        # rather than changing hold-previous semantics across every curve.
        self.assertNotIn(2, [column for column, _ in raw["curveTable"]["TeamDurability"]])
        self.assertAlmostEqual(source_four["variables"]["{f8c73243}"], .06, places=6)
        normalized = self.snap.traits[api]["curve"]["TeamDurability"]
        self.assertEqual([tft.curve_at(normalized, column) for column in (0, 1, 2, 3)], [1, .96, .94, .92])
        members = [{"api": self.snap.unit(name)["api"], "star": 2}
                   for name in ("Rakan", "Yorick", "Sejuani", "Vi", "Karma")]
        resolved = resolve_board_traits(self.snap, members)
        for name, expected in (("Rakan", .3), ("Karma", .06)):
            effect = next(effect for effect in resolved["effects"][self.snap.unit(name)["api"]] if effect["api"] == api)
            self.assertAlmostEqual(effect["durability"], expected)
        check = next(check for check in self.snap.audit["checks"]
                     if check["id"] == "trait:DA_Juggernaut18:TeamDurability:columns-2")
        self.assertEqual(check["expected"], [.94])
        archived = Path(self.snap.dir, "communitydragon.json")
        self.assertEqual(check["source"]["sha256"], hashlib.sha256(archived.read_bytes()).hexdigest())
        notes = json.loads(Path(self.snap.dir, "patchnotes.json").read_text())
        findings, _ = tft.check_audit(self.snap, notes)
        self.assertEqual(next(row["status"] for row in findings if row["what"] == check["what"]), "current")


class TestVanguardShieldCondition(unittest.TestCase):
    @staticmethod
    def spec(*, incoming=100, interval=1, items=(), low_health=False):
        spec = plain_spec(hp=1000, damage=0, incoming_dps=incoming, pressure_interval=interval, fx=items)
        trait = {"api": "test-vanguard", "name": "Vanguard", "stats": [], "durabilityWhileShielded": .05}
        if low_health:
            trait["shieldAtHp"] = [.5, .2, 10]
        spec["traits"] = [trait]
        return spec

    def test_no_shield_grants_no_durability(self):
        _, samples = tft.engine().measure_response(self.spec(), [0, 1, 2])
        self.assertEqual([sample["durability"] for sample in samples], [0, 0, 0])
        self.assertEqual([sample["hp"] for sample in samples], [1000, 900, 800])

    def test_any_shield_grants_durability_until_exact_expiry(self):
        spec = self.spec(interval=.25, items=[{"shieldAtStart": [.2, .5]}])
        _, samples = tft.engine().measure_response(spec, [0, .25, .5, .75])
        for sample, expected in zip(samples, (.05, .05, 0, 0), strict=True):
            self.assertAlmostEqual(sample["durability"], expected)
        self.assertAlmostEqual(samples[1]["shieldHp"], 176.25)
        self.assertEqual(samples[2]["shieldHp"], 0)
        self.assertEqual([sample["hp"] for sample in samples], [1000, 1000, 975, 950])

    def test_exhausted_shield_removes_durability_before_next_hit(self):
        spec = self.spec(items=[{"shieldAtStart": [.01, 10]}])
        _, samples = tft.engine().measure_response(spec, [0, 1, 2])
        self.assertAlmostEqual(samples[0]["durability"], .05)
        self.assertEqual(samples[1]["shieldHp"], 0)
        self.assertEqual(samples[1]["durability"], 0)
        self.assertEqual([sample["hp"] for sample in samples], [1000, 915, 815])

    def test_low_health_shield_turns_conditional_durability_back_on(self):
        _, samples = tft.engine().measure_response(self.spec(incoming=300, low_health=True), [1, 2, 3])
        self.assertEqual(samples[0]["durability"], 0)
        self.assertAlmostEqual(samples[1]["durability"], .05)
        self.assertEqual(samples[1]["shieldHp"], 200)
        self.assertEqual(samples[2]["durability"], 0)
        self.assertEqual([sample["hp"] for sample in samples], [700, 400, 315])

    def test_conditional_and_unconditional_durability_stack_multiplicatively(self):
        spec = self.spec(interval=.25, items=[{"durability": .2, "shieldAtStart": [.2, .5]}])
        _, samples = tft.engine().measure_response(spec, [0, .5])
        self.assertAlmostEqual(samples[0]["durability"], 1 - .8 * .95)
        self.assertAlmostEqual(samples[1]["durability"], .2)


class TestHunterDamageAmp(unittest.TestCase):
    def test_stable_primary_grants_amp_to_secondary_ability_damage_after_four_seconds(self):
        snap = tft.load_snapshot(18, "18.1d")
        unit = snap.unit("Sivir")
        effect = tft.trait_spec(snap, "DA_18_Hunter", 1, tft.load_trait_effects(18), unit)
        spec = UnitProfiles(snap, "clump").spec(unit["api"], 2, [effect], [], target_count=3)
        spec["duration"] = 12
        stats = spec["kits"]["base"]["stats"]
        stats["initialMana"] = stats["mana"]
        baseline = deepcopy(spec)
        baseline["traits"][0].pop("ampAfterSameTarget")
        damage = lambda result: [event for event in result["trace"] if event[1] == "damage"]
        before = damage(tft.engine().simulate(baseline, True)[1])
        after = damage(tft.engine().simulate(spec, True)[1])
        self.assertEqual([(event[0], event[3], event[4]) for event in before],
                         [(event[0], event[3], event[4]) for event in after])
        secondary_early = secondary_late = 0
        for normal, amplified in zip(before, after, strict=True):
            multiplier = 1.1 if normal[0] >= 4 else 1
            self.assertAlmostEqual(amplified[2], normal[2] * multiplier)
            if normal[3] != 0 and normal[4] == "bounces":
                secondary_early += normal[0] < 4
                secondary_late += normal[0] >= 4
        self.assertGreater(secondary_early, 0)
        self.assertGreater(secondary_late, 0)

    def test_primary_target_death_restarts_the_four_second_timer(self):
        spec = plain_spec(damage=100)
        first = dict(spec["dummies"]["slots"][0], hp=600)
        second = dict(first, hp=10000)
        spec.update(immortal=False, duration=12,
                    dummies={"critEv": 1, "slots": [first, second]},
                    traits=[{"api": "test-hunter", "name": "Hunter", "stats": [],
                             "ampAfterSameTarget": [.1, 4]}])
        result = tft.engine().simulate(spec, True)[1]
        attacks = {event[0]: event for event in result["trace"]
                   if event[1] == "damage" and event[4] == "auto"}
        self.assertAlmostEqual(attacks[4][2], 110)
        self.assertAlmostEqual(attacks[5][2], 90)
        self.assertEqual(attacks[5][3], 0)
        for time in (6, 7, 8):
            self.assertEqual((attacks[time][2], attacks[time][3]), (100, 1))
        self.assertAlmostEqual(attacks[9][2], 110)


if __name__ == "__main__":
    unittest.main()
