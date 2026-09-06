"""Refresh orchestration: staged warming, activation and failure reporting."""
import copy
import fcntl
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tft


class TestRefreshPublication(unittest.TestCase):
    def setUp(self):
        original = tft.load_snapshot(18, "18.1d")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.active = root / "data" / "set18" / "18.1d"
        self.cache = root / "cache"
        self.cache.mkdir()
        shutil.copytree(original.dir, self.active)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(tft, "TFT_DATA_DIR", str(root / "data")))
        self.stack.enter_context(patch.object(tft, "CACHE_DIR", str(self.cache)))
        self.stack.enter_context(patch.object(tft, "REFRESH_STATE_FILE", str(root / "refresh.json")))
        self.stack.enter_context(patch.object(tft, "_SNAP", {}))
        self.snap = tft.load_snapshot(18, "18.1d")
        self.args = SimpleNamespace(set=18, patch="18.1d", force=True)
        self.marker = self.cache / ".dashboard-ready"

    def mock_downloads(self, notes=None):
        notes = notes or json.loads((self.active / "patchnotes.json").read_text())
        source = {"url": "https://example.invalid", "lastModified": None, "sha256": "test"}
        cd = self.snap.communitydragon
        exported = {"setData": [{"number": 18, "champions": cd["champions"],
                                "traits": cd["traits"], "items": [x["apiName"] for x in cd["items"]]}],
                    "items": cd["items"]}
        bins = {k.lower(): v for k, v in self.snap.bins.items()}
        self.stack.enter_context(patch.object(tft, "fetch_bytes", return_value=b"notes"))
        self.stack.enter_context(patch.object(tft, "latest_patch_slug", return_value="18-1"))
        self.stack.enter_context(patch.object(tft, "patch_notes_document", return_value=notes))
        self.stack.enter_context(patch.object(tft, "fetch_source", side_effect=[(self.snap.raw, source), (exported, source)]))
        self.stack.enter_context(patch.object(tft, "fetch_json", side_effect=lambda url: bins[url.rsplit("/", 1)[-1].removesuffix(".cdtb.bin.json")]))
        self.stack.enter_context(patch.object(tft, "distill_bin", side_effect=lambda data: data))

    def test_warming_failure_keeps_active_snapshot(self):
        self.mock_downloads()
        before = {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()}

        def prepare(candidate):
            self.assertNotEqual(candidate.dir, str(self.active))
            self.assertEqual(before["meta.json"], (self.active / "meta.json").read_bytes())
            raise RuntimeError("simulation failed")

        with self.assertRaisesRegex(RuntimeError, "simulation failed"):
            tft.cmd_fetch(self.args, prepare=prepare)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()})
        self.assertFalse(self.marker.exists())

    def test_publication_happens_after_prepare(self):
        self.mock_downloads()
        before = (self.active / "meta.json").read_bytes()
        (self.active / "review.txt").write_text("Keep these review notes.")
        calls = []

        def prepare(candidate):
            self.assertEqual((self.active / "meta.json").read_bytes(), before)
            calls.append(candidate.patch)

        result = tft.cmd_fetch(self.args, prepare=prepare)
        self.assertEqual(calls, ["18.1d"])
        self.assertEqual(result.dir, str(self.active))
        self.assertNotEqual((self.active / "meta.json").read_bytes(), before)
        self.assertEqual((self.active / "review.txt").read_text(), "Keep these review notes.")

    def test_publication_failure_leaves_every_active_file_unchanged(self):
        self.mock_downloads()
        before = {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()}
        with patch.object(tft, "_exchange_directories", side_effect=OSError("publication interrupted")):
            with self.assertRaisesRegex(OSError, "publication interrupted"):
                tft.cmd_fetch(self.args, prepare=lambda candidate: None)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()})
        self.assertTrue(all(f["status"] == "current" for f in tft.check_patch_notes(tft.Snapshot(18, "18.1d", str(self.active)))[0]))

    def test_patch_only_change_has_distinct_cache_keys(self):
        newer = copy.deepcopy(self.snap)
        newer.patch = "18.1e"
        self.assertTrue(set(tft.cell_paths(self.snap).values()).isdisjoint(tft.cell_paths(newer).values()))
        self.assertNotEqual(tft.snapshot_revision(self.snap), tft.snapshot_revision(newer))

    def test_loaded_snapshot_keeps_its_cache_when_same_patch_files_change(self):
        old_hash = self.snap.hash_inputs()
        old_paths = tft.cell_paths(self.snap)
        old_revision = tft.snapshot_revision(self.snap)
        old_mana = self.snap.unit("Soraka")["stats"]["mana"]
        overrides = copy.deepcopy(self.snap.overrides)
        overrides["units"].setdefault(self.snap.unit("Soraka")["api"], {}).setdefault("stats", {})["mana"] = old_mana + 10
        (self.active / "overrides.json").write_text(json.dumps(overrides))
        newer = tft.Snapshot(18, "18.1d", str(self.active))
        self.assertEqual(newer.unit("Soraka")["stats"]["mana"], old_mana + 10)
        self.assertEqual(self.snap.unit("Soraka")["stats"]["mana"], old_mana)
        self.assertEqual(self.snap.hash_inputs(), old_hash)
        self.assertEqual(tft.cell_paths(self.snap), old_paths)
        self.assertEqual(tft.snapshot_revision(self.snap), old_revision)
        self.assertTrue(set(old_paths.values()).isdisjoint(tft.cell_paths(newer).values()))

    def test_stale_index_cannot_activate_an_older_audited_patch(self):
        from tft_update import ReviewRequired
        newer_dir = self.active.with_name("18.1e")
        shutil.copytree(self.active, newer_dir)
        self.mock_downloads()
        self.assertEqual(tft.load_snapshot().patch, "18.1e")
        before = (self.active / "meta.json").read_bytes()
        with patch.object(tft, "fetch_source") as fetch, patch.object(tft, "warm") as warm:
            with self.assertRaisesRegex(ReviewRequired, "older patch 18.1d"):
                tft.cmd_fetch(SimpleNamespace(set=18, patch=None, force=True), automatic=True)
        fetch.assert_not_called()
        warm.assert_not_called()
        self.assertEqual(tft.load_snapshot().patch, "18.1e")
        self.assertEqual((self.active / "meta.json").read_bytes(), before)

    def test_known_hotfix_is_reconciled_before_publication(self):
        notes = json.loads((self.active / "patchnotes.json").read_text())
        update = "SEPTEMBER 5TH"
        change = {"what": "Amumu Heal Max HP %", "old": "2.5%", "new": "3%",
                  "section": "CHAMPIONS", "update": update, "major": "Mid-Patch Updates"}
        notes["patch"] = "18.1e"
        notes["updates"].insert(0, update)
        notes["changes"].insert(0, change)
        notes["notes"].insert(0, {"text": "Amumu Heal Max HP %: 2.5% ⇒ 3%", "parent": "",
                                  "section": "CHAMPIONS", "update": update, "major": "Mid-Patch Updates"})
        self.mock_downloads(notes)
        before = (self.active / "overrides.json").read_bytes()
        prepared = []

        def prepare(candidate):
            self.assertEqual(tft.load_snapshot().patch, "18.1d")
            self.assertEqual(tft.curve_at(candidate.unit("Amumu")["curve"]["PassiveHealPercent"], 2), .03)
            prepared.append(candidate.patch)

        result = tft.cmd_fetch(SimpleNamespace(set=18, patch=None, force=True), automatic=True, prepare=prepare)
        self.assertEqual(prepared, ["18.1e"])
        self.assertEqual(result.patch, "18.1e")
        self.assertEqual((self.active / "overrides.json").read_bytes(), before)
        self.assertEqual(tft.load_snapshot().patch, "18.1e")
        self.assertTrue(all(f["status"] == "current" for f in tft.check_patch_notes(result)[0]))

    def test_staged_cell_keeps_previous_build_cache(self):
        unit = self.snap.unit("Akali")
        key = "s2-clump-bare"
        old = self.cache / f"akali-{key}-{'0' * 16}.json"
        old.write_text("previous build")
        path = self.cache / f"akali-{key}-{'1' * 16}.json"
        with patch.object(tft, "enumerate_builds", return_value=([], 0)):
            tft.compute_cell(self.snap, unit, key, {("akali", key): str(path)}, prune=False)
        self.assertEqual(old.read_text(), "previous build")
        self.assertTrue(path.exists())

    def fake_fetch(self, args, *, automatic, prepare, progress):
        self.assertTrue(automatic)
        progress(phase="fetching", targetPatch="18.1d")
        prepare(self.snap)
        return self.snap

    def test_success_records_state_and_unchanged_run_does_not_reload(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", return_value=0) as warm, \
             patch.object(tft, "cell_ready", return_value={"akali/s2-clump-bare": True}):
            tft.cmd_refresh(self.args)
            stamp = self.marker.stat().st_mtime_ns
            tft.cmd_refresh(self.args)
        self.assertEqual(self.marker.stat().st_mtime_ns, stamp)
        self.assertFalse(warm.call_args.kwargs["prune"])
        state = tft.refresh_state()
        self.assertEqual((state["status"], state["activePatch"], state["exit"]), ("ok", "18.1d", 0))
        self.assertEqual(state["computedCells"], 0)

    def test_malformed_state_does_not_prevent_the_next_refresh(self):
        Path(tft.REFRESH_STATE_FILE).write_text("[]")
        self.marker.write_text("[]")
        self.assertEqual(tft.refresh_state(), {})
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"akali/s2-clump-bare": True}):
            tft.cmd_refresh(self.args)
        self.assertEqual(tft.refresh_state()["status"], "ok")
        self.assertEqual(json.loads(self.marker.read_text())["patch"], "18.1d")

    def test_warm_failure_records_error_without_activation(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", side_effect=RuntimeError("simulation failed")):
            with self.assertRaises(SystemExit) as error:
                tft.cmd_refresh(self.args)
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(tft.refresh_state()["status"], "failed")
        self.assertEqual(tft.refresh_state()["activePatch"], "18.1d")
        self.assertFalse(self.marker.exists())

    def test_unknown_change_records_review_status(self):
        from tft_update import ReviewRequired
        with patch.object(tft, "cmd_fetch", side_effect=ReviewRequired("New targeting behavior needs review.")), \
             patch.object(tft, "warm") as warm:
            with self.assertRaises(SystemExit) as error:
                tft.cmd_refresh(self.args)
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(tft.refresh_state()["status"], "needs-review")
        warm.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_concurrent_refresh_does_not_overwrite_running_state(self):
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, {"status": "running", "phase": "warming"})
        with open(self.cache / "refresh.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(tft, "cmd_fetch") as fetch:
                tft.cmd_refresh(self.args)
        fetch.assert_not_called()
        self.assertEqual(tft.refresh_state(), {"status": "running", "phase": "warming"})


if __name__ == "__main__":
    unittest.main()
