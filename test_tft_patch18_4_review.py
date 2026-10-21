"""Replay the real 18.3 -> 18.4b transition without changing published data.

The numeric expectations below are independent of the review manifest, from
Riot's 18.4 notes including the October 7 update:
https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/teamfight-tactics-patch-18-4/
"""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import tft
import tft_update


OFFICIAL_SOURCE = (
    "https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/"
    "teamfight-tactics-patch-18-4/"
)


class TestPatch18_4Review(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repository = Path(__file__).resolve().parent
        set_dir = repository / "data" / "tft" / "set18"
        cls.previous = tft.Snapshot(18, "18.3", directory=str(set_dir / "18.3"))
        source = set_dir / "18.4b"
        if not source.is_dir():
            source = set_dir / ".pending" / "18.4b"
        cls.manifest = json.loads(
            (set_dir / "patch-reviews" / "18.4b.json").read_text()
        )
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.staging = Path(cls.temporary.name) / "candidate"
        cls.staging.mkdir()
        cls.input_names = (
            "metatft.json", "meta.json", "bins.json",
            "patchnotes.json", "communitydragon.json",
        )
        for name in cls.input_names:
            shutil.copyfile(source / name, cls.staging / name)
        # A publication's reviewed overrides must not be mistaken for candidate
        # approval: replay using only the previous generation's corrections.
        shutil.copyfile(
            Path(cls.previous.dir) / "overrides.json",
            cls.staging / "overrides.json",
        )
        cls.candidate = tft.Snapshot(18, "18.4b", directory=str(cls.staging))
        cls.notes = json.loads((cls.staging / "patchnotes.json").read_text())
        cls._verified = None

    def reconcile(self, manifest):
        # The retained Ivern approximation can warn during source cross-checks;
        # it is explicitly reviewed and is not relevant to these number oracles.
        with contextlib.redirect_stderr(io.StringIO()):
            return tft_update.reconcile(
                self.candidate, self.previous, self.notes, review=manifest
            )

    def verified(self):
        cls = type(self)
        if cls._verified is None:
            overrides, audit = self.reconcile(self.manifest)
            checked_dir = Path(self.temporary.name) / "checked"
            checked_dir.mkdir()
            for name in self.input_names:
                shutil.copyfile(self.staging / name, checked_dir / name)
            (checked_dir / "overrides.json").write_text(json.dumps(overrides))
            (checked_dir / "audit.json").write_text(json.dumps(audit))
            cls._verified = tft.Snapshot(18, "18.4b", directory=str(checked_dir))
        return cls._verified

    @staticmethod
    def values(entity, row, positions):
        return [tft.curve_at(entity["curve"][row], p) for p in positions]

    def test_review_is_bound_to_the_official_b_update(self):
        self.assertEqual(self.manifest["source"], OFFICIAL_SOURCE)
        self.assertEqual(self.notes["url"], OFFICIAL_SOURCE)
        self.assertEqual(self.notes["patch"], "18.4b")
        self.assertIn("OCTOBER 7TH", self.notes["updates"])
        checked = self.verified()
        self.assertEqual(
            checked.audit["automatic"]["reviewManifestHash"],
            tft.json_hash(self.manifest),
        )
        self.assertEqual(checked.audit["reviews"][-1], self.manifest)

    def test_alistar_damage_reaches_the_ability_calculation(self):
        unit = self.verified().unit("Alistar")
        self.assertEqual(self.values(unit, "Damage", (1, 2, 3)), [180, 270, 420])
        self.assertEqual(self.values(unit, "HealBaseValue", (1, 2, 3)), [230, 300, 400])
        for star, damage in ((1, 180), (2, 270), (3, 420)):
            with self.subTest(star=star):
                terms = tft.kit_spec(unit, star)["calcs"]["MagicDamageCalc1"]["terms"]
                self.assertEqual(len(terms), 1)
                self.assertEqual(terms[0]["scaling"], "AbilityPower")
                self.assertEqual(terms[0]["coef"], damage)

    def test_gromp_b_update_wins_without_overwriting_the_fourth_star(self):
        unit = self.verified().unit("Gromp")
        self.assertEqual(
            self.values(unit, "SecondaryDamageAP", (1, 2, 3, 4)),
            [180, 270, 435, 625],
        )
        for star, damage in ((1, 180), (2, 270), (3, 435)):
            with self.subTest(star=star):
                kit = tft.kit_spec(unit, star, "AP")
                self.assertEqual(kit["calcs"]["MagicDamageCalc2"]["terms"][0]["coef"], damage)
        self.assertEqual(
            self.values(unit["forms"]["AD"], "AutoAttackDamage", (1, 2, 3, 4)),
            [50, 75, 112.5, 168.75],
        )
        for star, ad in ((1, 50), (2, 75), (3, 112.5), (4, 168.75)):
            with self.subTest(ad_star=star):
                kit = tft.kit_spec(unit, star, "AD")
                self.assertEqual(kit["baseAd"], ad)
                self.assertEqual(kit["stats"]["ad"], 50)
                self.assertEqual(kit["stats"]["as"], 0.75)
                self.assertEqual((kit["stats"]["initialMana"], kit["stats"]["mana"]), (0, 70))
        # The AD buff must not turn the AP form's ordinary attacks into AD attacks.
        ap = tft.kit_spec(unit, 1, "AP")
        self.assertEqual(ap["baseAd"], 30)
        self.assertEqual((ap["stats"]["initialMana"], ap["stats"]["mana"]), (0, 45))

    def test_mama_beak_base_attack_and_summon_damage_are_distinct(self):
        unit = self.verified().unit("Mama Beak")
        kit = tft.kit_spec(unit, 2)
        self.assertEqual(unit["stats"]["ad"], 60)
        self.assertEqual(kit["baseAd"], 90)
        self.assertEqual(kit["rows"]["AutoAttackDamage"], 90)
        self.assertEqual(kit["rows"]["MiniDamagePerAttack"], 30)
        self.assertEqual(kit["calcs"]["PhysicalDamageCalc1"]["terms"][0]["coef"], 30)

    def test_nidalee_maps_small_spears_and_swipes_to_their_own_forms(self):
        unit = self.verified().unit("Nidalee")
        self.assertEqual(self.values(unit, "EmpoweredDamage", (1, 2)), [185, 275])
        self.assertEqual(self.values(unit["forms"]["AD"], "SwipeDamage", (1, 2)), [200, 300])
        for star, spear, swipe in ((1, 185, 200), (2, 275, 300)):
            with self.subTest(star=star):
                ap = tft.kit_spec(unit, star, "AP")
                ad = tft.kit_spec(unit, star, "AD")
                self.assertEqual(ap["calcs"]["MagicDamageCalc1"]["terms"][0]["coef"], spear)
                self.assertEqual(ad["calcs"]["PhysicalDamageCalc1"]["terms"][0]["coef"], swipe)
        self.assertEqual(self.values(unit, "ThirdAttackEmpoweredDamage", (1, 2)), [330, 500])
        self.assertEqual(self.values(unit, "EmpoweredDamage", (3, 4)), [2000, 3000])
        self.assertEqual(self.values(unit["forms"]["AD"], "SwipeDamage", (3, 4)), [3000, 5000])

    def test_trait_numeric_encodings_follow_the_official_values(self):
        checked = self.verified()
        self.assertEqual(self.values(checked.traits["DA_18_Fae"], "ADAP", (1, 2)), [4.5, 7])
        self.assertEqual(
            self.values(checked.traits["DA_18_Executioner"], "BonusBleedPercent", (1, 2, 3)),
            [0, 0.25, 0.35],
        )

    def test_unannounced_higher_star_source_changes_do_not_get_accepted(self):
        checked = self.verified()
        self.assertEqual(self.values(checked.unit("Rengar"), "StabDamage", (3, 4)), [650, 1050])
        self.assertEqual(self.values(checked.unit("Tristana"), "AOEDamageBase", (3, 4)), [420, 680])
        self.assertEqual(self.values(checked.unit("Brambleback"), "FrenzyADPercent", (1, 2, 3, 4)), [1, 1, 3, 4])

    def test_all_final_audit_checks_and_source_disagreements_are_reviewed(self):
        checked = self.verified()
        findings, _ = tft.check_audit(checked, self.notes)
        self.assertTrue(findings, "A real archived transition must produce audit checks.")
        self.assertEqual([f for f in findings if f["status"] != "current"], [])
        sources = tft.check_sources(checked)
        self.assertTrue(sources["audited"])
        self.assertEqual(
            [d for d in sources["disagreements"] if d["status"] != "explained"], []
        )
        for disagreement in sources["disagreements"]:
            with self.subTest(api=disagreement["api"], stat=disagreement["stat"]):
                self.assertTrue(disagreement["reason"])
                self.assertTrue(disagreement["disposition"])

    def test_removing_only_alistar_damage_mapping_restores_the_review_blocker(self):
        incomplete = deepcopy(self.manifest)
        removed = [
            mapping for mapping in incomplete["mappings"]
            if mapping.get("change", {}).get("what") == "Alistar Spell Damage"
        ]
        self.assertEqual(len(removed), 1)
        incomplete["mappings"] = [
            mapping for mapping in incomplete["mappings"] if mapping not in removed
        ]
        with self.assertRaisesRegex(
            tft_update.ReviewRequired,
            r"Alistar Spell Damage: ability numbers match zero or multiple curve rows",
        ):
            self.reconcile(incomplete)

    def test_changed_source_binding_is_rejected_before_audit_outputs(self):
        wrong = deepcopy(self.manifest)
        wrong["bindings"]["lookupHash"] = "0" * 64
        with self.assertRaisesRegex(tft_update.ReviewRequired, "manifest hashes do not match"):
            self.reconcile(wrong)
        # Reconciliation returns data for the publication workflow; these
        # negative cases must not write an approved candidate audit on disk.
        self.assertFalse((self.staging / "audit.json").exists())


if __name__ == "__main__":
    unittest.main()
