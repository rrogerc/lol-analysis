"""Refresh orchestration: staged warming, activation and failure reporting."""
import copy
import fcntl
import json
import shutil
import tempfile
import unittest
import socket
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import tft
import tft_comps
import tft_site
import tft_http


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
        self.comp_ready = {key: True for key in tft_comps.scenarios()}
        self.composition_revision = "composition-a"
        self.comp_warm = self.stack.enter_context(patch.object(tft_comps, "warm", return_value=0))
        self.stack.enter_context(patch.object(tft_comps, "cell_ready", return_value=self.comp_ready))
        self.stack.enter_context(patch.object(tft_comps, "revision", side_effect=lambda snap=None: self.composition_revision))
        self.stack.enter_context(patch.object(tft_comps, "source_stale", return_value=False))
        self.site_prepare = self.stack.enter_context(patch.object(tft_site, "prepare", side_effect=self.fake_site))
        self.site_load = self.stack.enter_context(patch.object(tft_site, "load"))

    def fake_site(self, snap):
        return {"siteGeneration": "prepared-" + self.composition_revision,
                "baselineRevision": tft.snapshot_revision(snap),
                "compositionRevision": self.composition_revision, "manifestHash": "test"}

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
            self.assertEqual(candidate.meta["verifiedAt"], candidate.meta["fetchedAt"])
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

    def test_communitydragon_only_change_reaches_the_reconciler(self):
        # The lookup, the bins and the notes stand still; only the freshly downloaded export moves.
        # check_audit binds none of it, so the fetch itself has to notice.
        from tft_update import ReviewRequired
        audit = json.loads((self.active / "audit.json").read_text())
        known = tft.source_disagreements(self.snap)["disagreements"]
        self.assertTrue(known)      # the archived 18.1d snapshot already differs from its export somewhere
        audit["sourceCrossCheck"] = {"explained": [dict(d, disposition="fixture", reason="Known.", evidence=["fixture"])
                                                   for d in known], "inherited": []}
        (self.active / "audit.json").write_text(json.dumps(audit))
        tft._SNAP.clear()
        self.snap = tft.load_snapshot(18, "18.1d")
        args = SimpleNamespace(set=18, patch="18.1d", force=True)
        calls = []
        self.mock_downloads()
        tft.cmd_fetch(args, automatic=True, prepare=lambda candidate: calls.append(candidate.patch))
        self.assertEqual(calls, ["18.1d"])          # everything explained: published without a review

        tft._SNAP.clear()
        self.snap = tft.load_snapshot(18, "18.1d")
        moved = copy.deepcopy(self.snap.communitydragon)
        soraka = next(c for c in moved["champions"] if c["apiName"] == "DA_18_Soraka")
        soraka["stats"]["hp"] += 50
        # Only the download moves. mock_downloads copies the export it is given, so the
        # archived one is put back: the reconciler reads it as the previous snapshot's,
        # and a disagreement that snapshot already had is inherited rather than raised.
        archived, self.snap.communitydragon = self.snap.communitydragon, moved
        self.mock_downloads()
        self.snap.communitydragon = archived
        before = (self.active / "audit.json").read_bytes()
        with self.assertRaisesRegex(ReviewRequired, "unexplained lookup/CommunityDragon disagreement: Soraka hp"):
            tft.cmd_fetch(args, automatic=True, prepare=lambda candidate: calls.append("published"))
        self.assertEqual(calls, ["18.1d"])
        self.assertEqual((self.active / "audit.json").read_bytes(), before)

    def test_staged_cell_keeps_previous_build_cache(self):
        unit = self.snap.unit("Akali")
        key = "s2-clump-bare"
        old = self.cache / f"akali-{key}-{'0' * 16}.json"
        old.write_text("previous build")
        path = self.cache / f"akali-{key}-{'1' * 16}.json"
        with patch.object(tft, "enumerate_builds", return_value=([], 0, [])):
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
        self.assertEqual(state["computedCompositionScenarios"], 0)
        self.assertEqual(state["compositionScenariosReady"], 8)
        self.assertIs(self.comp_warm.call_args.kwargs["snap"], self.snap)
        self.assertFalse(self.comp_warm.call_args.kwargs["prune"])
        self.assertEqual(self.comp_warm.call_args.kwargs["workers"], tft_comps.DEFAULT_WARM_WORKERS)

    def test_prepares_both_analyses_and_responses_before_activation(self):
        events = []
        def warm_builds(**kwargs):
            self.assertIs(kwargs["snap"], self.snap)
            self.assertFalse(self.marker.exists())
            events.append("champions")
            return 3
        def warm_comps(**kwargs):
            self.assertIs(kwargs["snap"], self.snap)
            self.assertFalse(self.marker.exists())
            events.append("compositions")
            return 2
        def prepare_site(snap):
            self.assertFalse(self.marker.exists())
            events.append("responses")
            return self.fake_site(snap)
        def fetch(args, **kwargs):
            kwargs["prepare"](self.snap)
            self.assertEqual(events, ["champions", "compositions", "responses"])
            events.append("activate")
            return self.snap
        self.comp_warm.side_effect = warm_comps
        self.site_prepare.side_effect = prepare_site
        with patch.object(tft, "cmd_fetch", side_effect=fetch), \
             patch.object(tft, "warm", side_effect=warm_builds), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            tft.cmd_refresh(self.args)
        self.assertEqual(events, ["champions", "compositions", "responses", "activate"])
        state = tft.refresh_state()
        self.assertEqual((state["computedCells"], state["computedCompositionScenarios"]), (3, 2))
        self.assertEqual(json.loads(self.marker.read_text())["site"], self.fake_site(self.snap))

    def test_composition_failure_preserves_active_archive_and_ready_marker(self):
        self.mock_downloads()
        before = {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()}
        self.marker.write_text('{"previous":true}')
        self.comp_warm.side_effect = RuntimeError("composition worker failed")
        with patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            with self.assertRaises(SystemExit) as error:
                tft.cmd_refresh(self.args)
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()})
        self.assertEqual(json.loads(self.marker.read_text()), {"previous": True})
        self.site_prepare.assert_not_called()

    def test_incomplete_compositions_cannot_publish(self):
        self.comp_ready["c1-clump-mixed"] = False
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            with self.assertRaises(SystemExit):
                tft.cmd_refresh(self.args)
        self.assertFalse(self.marker.exists())
        self.site_prepare.assert_not_called()
        self.assertIn("Some compositions did not finish", tft.refresh_state()["message"])

    def test_response_failure_preserves_active_archive(self):
        self.mock_downloads()
        before = {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()}
        self.site_prepare.side_effect = RuntimeError("compression failed")
        with patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            with self.assertRaises(SystemExit):
                tft.cmd_refresh(self.args)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()})
        self.assertFalse(self.marker.exists())

    def test_composition_only_change_signals_a_new_publication(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            tft.cmd_refresh(self.args)
            before = json.loads(self.marker.read_text())
            self.composition_revision = "composition-b"
            tft.cmd_refresh(self.args)
        after = json.loads(self.marker.read_text())
        self.assertEqual(before["revision"], after["revision"])
        self.assertNotEqual(before["compositionRevision"], after["compositionRevision"])
        self.assertNotEqual(before["site"], after["site"])

    def test_waits_for_an_existing_composition_calculation(self):
        self.comp_warm.side_effect = [None, 0]
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"cell": True}), \
             patch.object(tft.time, "sleep") as sleep:
            tft.cmd_refresh(self.args)
        self.assertEqual(self.comp_warm.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_dashboard_ready_rejects_wrong_prepared_revision(self):
        with patch.object(tft, "cell_ready", return_value={"cell": True}):
            with self.assertRaisesRegex(RuntimeError, "do not match"):
                tft.dashboard_ready(self.snap, prepared_site={**self.fake_site(self.snap), "compositionRevision": "wrong"})
        self.assertFalse(self.marker.exists())

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

    def failing_download(self, args, *, automatic, prepare, progress):
        progress(phase="fetching", targetPatch="18.2")
        tft.fetch_bytes("https://user:password@example.invalid/source?token=secret#private")

    def test_exhausted_transient_download_exits_75_with_safe_transport_context(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.failing_download), \
             patch.object(tft.urllib.request, "urlopen", side_effect=URLError(
                 socket.gaierror(socket.EAI_NONAME, "DNS failure with token=secret"))) as opener, \
             patch.object(tft_http.time, "sleep"), patch.object(tft, "warm") as warm:
            with self.assertRaises(SystemExit) as caught:
                tft.cmd_refresh(self.args)
        self.assertEqual(caught.exception.code, 75)
        self.assertEqual(opener.call_count, 4)
        warm.assert_not_called()
        state = tft.refresh_state()
        self.assertEqual((state["status"], state["phase"], state["failedPhase"]), ("failed", "stopped", "fetching"))
        self.assertEqual(state["transport"]["attempts"], 4)
        self.assertTrue(state["transport"]["automaticRetry"])
        self.assertEqual(state["transport"]["url"], "https://example.invalid/source")
        self.assertEqual(state["history"][-1]["exit"], 75)
        for secret in ("password", "token=secret", "#private"):
            self.assertNotIn(secret, json.dumps(state))
        self.assertIsNone(tft._FETCH_PROGRESS.get())

    def test_permanent_http_error_does_not_trigger_service_retry(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.failing_download), \
             patch.object(tft.urllib.request, "urlopen", side_effect=HTTPError(
                 "https://example.invalid/source?token=secret", 403, "secret", {}, None)) as opener:
            with self.assertRaises(SystemExit) as caught:
                tft.cmd_refresh(self.args)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(opener.call_count, 1)
        self.assertFalse(tft.refresh_state()["transport"]["automaticRetry"])
        self.assertEqual(tft.refresh_state()["transport"]["httpStatus"], 403)

    def test_non_fetch_timeouts_and_arbitrary_exit_75_are_permanent_failures(self):
        for failure, expected in ((TimeoutError("warm timeout"), 1), (SystemExit(75), 1), (SystemExit(143), 143)):
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(tft, "cmd_fetch", side_effect=failure):
                with self.assertRaises(SystemExit) as caught:
                    tft.cmd_refresh(self.args)
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(tft.refresh_state()["failedPhase"], "checking")

    def test_review_blocker_and_terminal_history_survive_later_network_failure(self):
        from tft_update import ReviewRequired

        def review(args, **kwargs):
            kwargs["progress"](phase="validating", targetPatch="18.2")
            raise ReviewRequired("A new mechanic needs review.")

        with patch.object(tft, "cmd_fetch", side_effect=review):
            with self.assertRaises(SystemExit) as caught:
                tft.cmd_refresh(self.args)
        self.assertEqual(caught.exception.code, 2)
        blocker = tft.refresh_state()["reviewBlocker"]
        self.assertEqual((blocker["targetPatch"], blocker["failedPhase"]), ("18.2", "validating"))
        with patch.object(tft, "cmd_fetch", side_effect=self.failing_download), \
             patch.object(tft.urllib.request, "urlopen", side_effect=URLError(TimeoutError("network"))), \
             patch.object(tft_http.time, "sleep"):
            with self.assertRaises(SystemExit):
                tft.cmd_refresh(self.args)
        state = tft.refresh_state()
        self.assertEqual(state["reviewBlocker"], blocker)
        self.assertEqual([entry["exit"] for entry in state["history"]], [2, 75])
        self.assertEqual(state["history"][0]["message"], "A new mechanic needs review.")

    def test_legacy_review_state_migrates_to_blocker_and_history(self):
        old = {"status": "needs-review", "phase": "stopped", "message": "Review existing blocker.",
               "targetPatch": "18.2", "activePatch": "18.1d", "finishedAt": "2026-09-10T10:00:00+00:00", "exit": 2}
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, old)
        with patch.object(tft, "cmd_fetch", side_effect=RuntimeError("later failure")):
            with self.assertRaises(SystemExit):
                tft.cmd_refresh(self.args)
        state = tft.refresh_state()
        self.assertEqual(state["reviewBlocker"]["message"], old["message"])
        self.assertEqual(state["history"][0]["message"], old["message"])
        self.assertEqual(state["reviewBlocker"]["detectedAt"], old["finishedAt"])

    def test_validated_preparation_clears_old_blocker_even_if_warming_fails(self):
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, {"status": "needs-review", "message": "Old review.", "exit": 2})
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", side_effect=RuntimeError("new warm failure")):
            with self.assertRaises(SystemExit):
                tft.cmd_refresh(self.args)
        state = tft.refresh_state()
        self.assertIsNone(state["reviewBlocker"])
        self.assertEqual(state["failedPhase"], "warming-builds")
        self.assertEqual(state["history"][0]["message"], "Old review.")

    def test_success_clears_blocker_and_preserves_history(self):
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, {"status": "needs-review", "message": "Old review.", "exit": 2})
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            tft.cmd_refresh(self.args)
        state = tft.refresh_state()
        self.assertIsNone(state["reviewBlocker"])
        self.assertEqual([entry["status"] for entry in state["history"]], ["needs-review", "ok"])

    def test_terminal_history_is_bounded_and_does_not_duplicate_previous_run(self):
        history = [{"status": "failed", "message": f"old {i}", "exit": 1} for i in range(15)]
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, {**history[-1], "history": history})
        with patch.object(tft, "cmd_fetch", side_effect=RuntimeError("new failure")):
            with self.assertRaises(SystemExit):
                tft.cmd_refresh(self.args)
        history = tft.refresh_state()["history"]
        self.assertEqual(len(history), tft.REFRESH_HISTORY_LIMIT)
        self.assertEqual([entry["message"] for entry in history], [f"old {i}" for i in range(6, 15)] + ["new failure"])

    def test_long_retry_after_persists_guard_without_immediate_service_retry(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.failing_download), \
             patch.object(tft.urllib.request, "urlopen", side_effect=HTTPError(
                 "https://example.invalid/source", 429, "limited", {"Retry-After": "600"}, None)) as opener, \
             patch.object(tft_http.time, "sleep") as sleep:
            with self.assertRaises(SystemExit) as caught:
                tft.cmd_refresh(self.args)
        state = tft.refresh_state()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()
        self.assertFalse(state["transport"]["automaticRetry"])
        self.assertEqual(state["transport"]["retryAfterSeconds"], 600)
        self.assertGreater(datetime.fromisoformat(state["retryNotBefore"]), datetime.now(timezone.utc) + timedelta(seconds=590))

    def test_retry_not_before_skips_fetch_and_expired_guard_allows_recovery(self):
        deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        blocker = {"message": "Still needs review.", "targetPatch": "18.2"}
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, {"status": "failed", "exit": 1,
            "activePatch": "18.1d", "lastSuccessAt": "old success", "retryNotBefore": deadline,
            "reviewBlocker": blocker, "transport": {"url": "https://example.invalid/source"}})
        with patch.object(tft, "cmd_fetch") as fetch:
            with self.assertRaises(SystemExit) as caught:
                tft.cmd_refresh(self.args)
        fetch.assert_not_called()
        state = tft.refresh_state()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual((state["status"], state["phase"]), ("waiting-not-before", "waiting-not-before"))
        self.assertEqual(state["lastSuccessAt"], "old success")
        self.assertEqual(state["reviewBlocker"], blocker)
        state["retryNotBefore"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, state)
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch) as fetch, \
             patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            tft.cmd_refresh(self.args)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(tft.refresh_state()["status"], "ok")
        self.assertIsNone(tft.refresh_state()["retryNotBefore"])
        self.assertIsNone(tft.refresh_state()["reviewBlocker"])

    def test_optional_bin_404_preserves_historical_evidence_literal(self):
        missing = next(api for api, value in self.snap.bins.items() if "error" in value)
        self.mock_downloads()
        original = tft.fetch_json

        def fetched(url):
            if url.rsplit("/", 1)[-1].removesuffix(".cdtb.bin.json") == missing.lower():
                raise HTTPError(url, 404, "Not Found; GET context; attempt 1/4", {}, None)
            return original(url)

        with patch.object(tft, "fetch_json", side_effect=fetched):
            result = tft.cmd_fetch(self.args)
        self.assertEqual(result.bins[missing], {"error": "HTTP Error 404: Not Found"})


if __name__ == "__main__":
    unittest.main()
