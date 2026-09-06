"""TFT snapshot/hotfix regressions: python3 -m unittest test_tft_data."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

import tft


NOTES = """
<h1>Teamfight Tactics patch 18.1</h1>
<h2>Mid-Patch Updates</h2>
<h3>AUGUST 31ST AND SEPTEMBER 1ST</h3><h4>CHAMPIONS</h4>
<ul><li>Amumu Heal Max HP %: <span>2.2%</span> ⇒ <span>2.5%</span></li></ul>
<h3>AUGUST 28TH</h3><h4>BUG FIXES</h4><ul><li>A performance fix.</li></ul>
<h3>AUGUST 27TH</h3><h4>BUG FIXES</h4><ul><li>Another fix.</li></ul>
<h2>LARGE CHANGES</h2><h4>ADJUSTED ARTIFACTS</h4>
<ul><li>Dawncore:<ul><li>Mana Regen: 2 ⇒ 1</li></ul></li>
<li>Example Health Threshold: 30 ⇒ 35. Reward: 3 items ⇒ 2 items</li></ul>
"""


class TestPatchDocuments(unittest.TestCase):
    def test_hotfix_order(self):
        values = ["18.10", "18.1d", "18.2", "18.1", "18.1b", "17.9"]
        self.assertEqual(sorted(values, key=tft.tft_patch_key),
                         ["17.9", "18.1", "18.1b", "18.1d", "18.2", "18.10"])
        with self.assertRaises(ValueError):
            tft.tft_patch_key("../18.1d")

    def test_dates_and_nested_item_labels_survive(self):
        document = tft.patch_notes_document(NOTES, "18.1")
        self.assertEqual(document["patch"], "18.1d")
        self.assertTrue(document["url"].endswith("patch-18-1/"))
        changes = {c["what"]: c for c in document["changes"]}
        self.assertEqual(changes["Amumu Heal Max HP %"]["update"], "AUGUST 31ST AND SEPTEMBER 1ST")
        self.assertEqual(changes["Amumu Heal Max HP %"]["new"], "2.5%")
        self.assertEqual(changes["Dawncore Mana Regen"]["new"], "1")
        self.assertEqual(changes["Example Health Threshold"]["new"], "35")
        self.assertEqual(changes["Reward"]["old"], "3 items")

    def test_explicit_revision_wins_over_section_count(self):
        document = tft.patch_notes_document(NOTES.replace("AUGUST 28TH", "18.1e UPDATE"), "18.1")
        self.assertEqual(document["patch"], "18.1e")

    def test_unreadable_patch_notes_fail(self):
        with self.assertRaisesRegex(ValueError, "no readable balance"):
            tft.patch_notes_document("<h1>Temporarily unavailable</h1>", "18.1")

    def test_nonnumeric_gameplay_changes_invalidate_review(self):
        before = tft.patch_notes_document(NOTES, "18.1")
        changed = NOTES.replace("A performance fix.", "Akali now casts twice against her current target.")
        after = tft.patch_notes_document(changed, "18.1")
        self.assertEqual(before["changes"], after["changes"])
        self.assertNotEqual(tft.json_hash(before), tft.json_hash(after))

    def test_rotating_article_recommendations_do_not_invalidate_review(self):
        before = tft.patch_notes_document(NOTES, "18.1")
        footer = "<h2>Related Articles</h2><ul><li>A different recommended article</li></ul>"
        after = tft.patch_notes_document(NOTES + footer, "18.1")
        self.assertEqual(tft.json_hash(before), tft.json_hash(after))

    def test_ambiguous_communitydragon_set_fails(self):
        with self.assertRaisesRegex(ValueError, "found 0"):
            tft.communitydragon_set({"setData": []}, 18)
        with self.assertRaisesRegex(ValueError, "found 2"):
            tft.communitydragon_set({"setData": [{"number": 18}, {"number": 18}]}, 18)


class TestLivePatchAudit(unittest.TestCase):
    def setUp(self):
        self.snap = copy.deepcopy(tft.load_snapshot(18, "18.1d"))

    def test_live_patch_targets_pass(self):
        findings, _ = tft.check_patch_notes(self.snap)
        self.assertEqual(len(findings), 64)
        self.assertTrue(all(f["status"] == "current" for f in findings))

    def test_amumu_heal_and_yi_ap_resists_reach_engine_inputs(self):
        amumu = self.snap.unit("Amumu")
        self.assertEqual([tft.curve_at(amumu["curve"]["PassiveHealPercent"], s) for s in (1, 2, 3, 4)],
                         [.025, .025, .04, .04])
        yi = self.snap.unit("Master Yi")
        for form in ("AD", "AP"):
            kit = tft.kit_spec(yi, 2, form)
            self.assertEqual(kit["stats"]["armor"], 55)
            self.assertEqual(kit["stats"]["mr"], 55)

    def test_single_percentage_and_form_regressions_are_detected(self):
        self.snap.unit("Amumu")["curve"]["PassiveHealPercent"] = [[1, .022], [3, .04]]
        self.snap.unit("Master Yi")["forms"]["AP"]["stats"]["armor"] = 60
        findings, _ = tft.check_patch_notes(self.snap)
        bad = [f for f in findings if f["status"] != "current"]
        self.assertEqual(len(bad), 2)
        self.assertEqual({f["unit"] for f in bad}, {"Amumu", "Master Yi"})

    def test_item_regression_is_detected(self):
        target = next(c["target"] for c in self.snap.audit["checks"]
                      if c["target"]["kind"] == "item")
        self.snap.items[target["api"]]["curve"][target["row"]] = [[1, -999]]
        findings, _ = tft.check_patch_notes(self.snap)
        self.assertTrue(any(f["api"] == target["api"] and f["status"] != "current" for f in findings))

    def test_changed_lookup_requires_a_new_review(self):
        self.snap.raw["_metadata"]["coreHash"] = "new-upstream-data"
        findings, _ = tft.check_patch_notes(self.snap)
        self.assertTrue(any(f["where"] == "audit lookupHash" for f in findings))

    def test_new_patch_note_cannot_reuse_old_review(self):
        notes = json.loads((Path(self.snap.dir) / "patchnotes.json").read_text())
        notes["changes"][0]["new"] = "99% AD/AP/AS"
        findings, _ = tft.check_audit(self.snap, notes)
        self.assertTrue(any(f["where"] == "audit patchNotesHash" for f in findings))

    def test_failed_review_leaves_active_snapshot_unchanged(self):
        raw = copy.deepcopy(self.snap.raw)
        source = {"url": "https://example.invalid/data", "lastModified": None, "sha256": "test"}
        cdragon = {"setData": [{"number": 18, "champions": [], "traits": [], "items": []}]}
        original = Path(tft.TFT_DATA_DIR) / "set18" / "18.1"
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "set18" / "18.1"
            shutil.copytree(original, active)
            before = (active / "metatft.json").read_bytes()
            with patch.object(tft, "TFT_DATA_DIR", directory), \
                 patch.object(tft, "fetch_bytes", return_value=NOTES.encode()), \
                 patch.object(tft, "fetch_source", side_effect=[(raw, source), (cdragon, source)]), \
                 patch.object(tft, "fetch_json", return_value={}):
                with self.assertRaisesRegex(ValueError, "needs a patch audit"):
                    tft.cmd_fetch(SimpleNamespace(set=18, patch="18.1d", force=True))
                self.assertEqual(tft.patch_dirs(18), ["18.1"])
            self.assertEqual((active / "metatft.json").read_bytes(), before)
            self.assertTrue((Path(directory) / "set18" / ".pending" / "18.1d" / "metatft.json").exists())

    def test_missing_timings_cannot_replace_an_audited_patch(self):
        raw = copy.deepcopy(self.snap.raw)
        source = {"url": "https://example.invalid/data", "lastModified": None, "sha256": "test"}
        cdragon = {"setData": [{"number": 18, "champions": [], "traits": [], "items": []}]}
        notes = json.loads((Path(self.snap.dir) / "patchnotes.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "set18" / "18.1d"
            shutil.copytree(self.snap.dir, active)
            before = (active / "bins.json").read_bytes()
            with patch.object(tft, "TFT_DATA_DIR", directory), \
                 patch.object(tft, "fetch_bytes", return_value=b"notes"), \
                 patch.object(tft, "patch_notes_document", return_value=notes), \
                 patch.object(tft, "fetch_source", side_effect=[(raw, source), (cdragon, source)]), \
                 patch.object(tft, "fetch_json", side_effect=HTTPError("https://example.invalid", 404, "Not Found", {}, None)):
                with self.assertRaisesRegex(ValueError, "needs a patch audit"):
                    tft.cmd_fetch(SimpleNamespace(set=18, patch="18.1d", force=True))
            self.assertEqual((active / "bins.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
