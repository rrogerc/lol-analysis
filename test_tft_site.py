"""Publication is complete, immutable and usable without request-time math."""

from contextlib import ExitStack
from copy import deepcopy
import errno
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tft
import tft_comps
import tft_site


class TestTftSite(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.site = self.directory / "site"
        self.stack.enter_context(patch.object(tft_site, "CACHE_DIR", self.site))
        self.snap = SimpleNamespace(set_no=18, patch="test-patch",
            meta={"patch": "test-patch", "fetchedAt": "first-fetch", "verifiedAt": "first-check",
                  "verification": "numeric checks passed", "sources": {
                      "metatft": {"url": "https://example.test/source-a", "sha256": "source-a"}}},
            audit={"checkedAt": "first-check", "unresolved": [{"id": "fixture", "detail": "original"}]},
            communitydragon={"items": [{"apiName": "item", "icon": "assets/item-a.png"}]})
        self.paths = {}
        self.payloads = {}
        for slug, key in (("ahri", "s1-clump-bare"), ("ahri", "s2-clump-bare"),
                          ("leona", "s2-clump-bare")):
            scenario = {"key": key, "label": key, "star": int(key[1]), "geometry": "clump",
                        "traits": "bare", "threat": "mixed"}
            payload = {"unit": slug, "unitApi": "TFT18_" + slug.title(), "scenario": scenario,
                       "rows": [{"items": ["A", "B", "C"], "damage": 1.2345678901234567}],
                       "coreAnalysis": {"coreStats": {"pair": {"nearCount": 2, "zero": -0.0}}},
                       "best": {"itemApis": ["A", "B", "C"], "damage": 7.25}}
            path = self.directory / f"{slug}-{key}.json"
            path.write_text(json.dumps(payload, indent=1) + "\n")
            self.paths[slug, key] = str(path)
            self.payloads[f"/api/tft/{slug}/{key}.json"] = payload
        self.comp_paths = {}
        for key in ("c1-clump-mixed", "c4-spread-mixed"):
            path = self.directory / f"{key}.json"
            payload = {"key": key, "revision": "composition", "baselineRevision": "baseline",
                       "methodology": {"evaluationModel": "symmetric-reference-pool-v1"},
                       "results": [{"score": 100 / 3, "items": ["C", "B", "A"]}]}
            path.write_text(json.dumps(payload) + "\n")
            self.comp_paths[key] = str(path)
            self.payloads[f"/api/tft/compositions/{key}.json"] = payload
        self.leaderboards = {"best-clump-bare-mixed": {}, "s2-clump-bare-mixed": {}}
        self.meta = {"revision": "baseline", "patch": self.snap.patch, "units": [],
                     "tankDummies": {"mixed": [{"hp": 3000, "armor": 110}]},
                     "sources": deepcopy(self.snap.meta["sources"]),
                     "sourceLimitations": deepcopy(self.snap.audit["unresolved"])}
        self.comp_meta = {"revision": "composition", "baselineRevision": "baseline",
                          "methodology": {"evaluationModel": "symmetric-reference-pool-v1"}}
        for owner, name, value in (
                (tft, "source_stale", False), (tft, "snapshot_revision", "baseline"),
                (tft, "cell_paths", self.paths), (tft, "leaderboard_scenarios", self.leaderboards),
                (tft_comps, "source_stale", False), (tft_comps, "revision", "composition"),
                (tft_comps, "cell_paths", self.comp_paths)):
            self.stack.enter_context(patch.object(owner, name, return_value=value))
        self.metadata = self.stack.enter_context(patch.object(tft, "api_meta", return_value=self.meta))
        self.comp_metadata = self.stack.enter_context(
            patch.object(tft_comps, "api_meta", return_value=self.comp_meta))
        self.leaderboard = self.stack.enter_context(
            patch.object(tft, "cached_leaderboard", side_effect=self.board))
        self.stack.enter_context(patch.object(tft, "load_snapshot", side_effect=AssertionError("snapshot reloaded")))
        self.stack.enter_context(patch.object(tft, "warm", side_effect=AssertionError("baseline warm")))
        self.stack.enter_context(patch.object(tft_comps, "warm", side_effect=AssertionError("composition warm")))

    def board(self, key, paths, *, snap):
        self.assertIs(snap, self.snap)
        # Read the provided links, preserving the same API's input semantics.
        self.assertTrue(all(self.site in Path(path).parents for path in paths.values()))
        return {"revision": "baseline", "complete": True, "selection": {"key": key},
                "readyCount": len(paths), "expectedCount": len(paths), "pending": [],
                "damage": [json.loads(Path(path).read_bytes())["best"] for path in paths.values()],
                "tanks": []}

    def prepare(self):
        descriptor = tft_site.prepare(self.snap)
        return descriptor, tft_site.load(descriptor)

    def rewrite_manifest(self, descriptor, edit):
        descriptor = dict(descriptor)
        path = self.site / descriptor["siteGeneration"] / "manifest.json"
        manifest = json.loads(path.read_bytes())
        edit(manifest)
        raw = tft_site._json_bytes(manifest)
        path.write_bytes(raw)
        descriptor["manifestHash"] = hashlib.sha256(raw).hexdigest()
        return descriptor

    def assert_no_publication(self):
        self.assertEqual(list(self.site.glob("g-*")), [])
        self.assertEqual(list(self.site.glob(".stage-*")), [])

    def test_complete_bundle_preserves_cells_and_prepares_every_response(self):
        descriptor, bundle = self.prepare()
        self.assertEqual(set(descriptor), {"siteGeneration", "baselineRevision",
                                          "compositionRevision", "manifestHash"})
        expected = set(self.payloads) | {
            "/api/tft/meta.json", "/api/tft/status.json", "/api/tft/ahri/cores.json",
            "/api/tft/leona/cores.json", "/api/tft/compositions/meta.json",
            "/api/tft/compositions/status.json", *(
                f"/api/tft/leaderboard/{key}.json" for key in self.leaderboards)}
        self.assertEqual(set(bundle.entries), expected)
        for url in expected:
            identity, compressed = bundle.asset(url), bundle.asset(url, compressed=True)
            raw, zipped = identity["path"].read_bytes(), compressed["path"].read_bytes()
            self.assertEqual(gzip.decompress(zipped), raw)
            for asset, data in ((identity, raw), (compressed, zipped)):
                self.assertEqual(asset["size"], len(data))
                self.assertEqual(asset["etag"], '"' + hashlib.sha256(data).hexdigest() + '"')
            self.assertIsNone(identity["encoding"])
            self.assertEqual(compressed["encoding"], "gzip")
        for url, payload in self.payloads.items():
            self.assertEqual(bundle.json(url), payload)
        for (slug, key), path in self.paths.items():
            linked = bundle.asset(f"/api/tft/{slug}/{key}.json")["path"]
            self.assertEqual(linked.read_bytes(), Path(path).read_bytes())
            self.assertEqual(linked.stat().st_ino, Path(path).stat().st_ino)
        self.assertEqual(bundle.json("/api/tft/meta.json"), dict(self.meta, publicationRevision=descriptor["siteGeneration"]))
        self.assertEqual(bundle.json("/api/tft/compositions/meta.json"),
                         dict(self.comp_meta, publicationRevision=descriptor["siteGeneration"]))
        status = bundle.json("/api/tft/status.json")
        self.assertEqual(status, {"warmer": "idle", "patch": self.snap.patch,
                         "revision": "baseline", "refresh": {},
                         "publicationRevision": descriptor["siteGeneration"],
                         "ready": {f"{slug}/{key}": True for slug, key in self.paths}})
        self.assertEqual(bundle.json("/api/tft/compositions/status.json"), {
            "revision": "composition", "ready": {key: True for key in self.comp_paths},
            "publicationRevision": descriptor["siteGeneration"],
            "warmer": "idle", "progress": {}})
        self.assertNotIn("publicationRevision", self.meta)
        self.assertNotIn("publicationRevision", self.comp_meta)
        self.metadata.assert_called_once_with(self.snap)
        self.comp_metadata.assert_called_once_with(self.snap)
        self.assertEqual(self.leaderboard.call_count, len(self.leaderboards))
        self.assertEqual(list(self.site.glob(".stage-*")), [])

    def test_loaded_publication_never_uses_models_or_current_publisher_hash(self):
        descriptor, _ = self.prepare()
        with ExitStack() as stack:
            for owner, names in ((tft, ("source_stale", "snapshot_revision", "cell_paths", "api_meta",
                                       "cached_scenario", "cached_core_contexts", "cached_leaderboard")),
                                 (tft_comps, ("source_stale", "revision", "cell_paths", "api_meta"))):
                for name in names:
                    stack.enter_context(patch.object(owner, name, side_effect=AssertionError(name)))
            stack.enter_context(patch.object(tft_site, "SOURCE_HASH", "f" * 64))
            bundle = tft_site.load(descriptor)
            self.assertEqual(bundle.json("/api/tft/meta.json"),
                             dict(self.meta, publicationRevision=descriptor["siteGeneration"]))
            self.assertIsNone(bundle.asset("/api/tft/missing.json"))
            with self.assertRaises(KeyError):
                bundle.json("/api/tft/missing.json")

    def test_same_generation_reuse_does_not_rebuild_or_recompress(self):
        descriptor, bundle = self.prepare()
        before = {path: path.stat().st_mtime_ns for path in bundle.directory.rglob("*") if path.is_file()}
        with patch.object(tft, "api_meta", side_effect=AssertionError("metadata rebuilt")), \
                patch.object(tft, "cached_core_contexts", side_effect=AssertionError("cores rebuilt")), \
                patch.object(tft_site, "_compress", side_effect=AssertionError("recompressed")):
            self.assertEqual(tft_site.prepare(self.snap), descriptor)
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in before})

    def test_presentation_changes_republish_without_changing_calculation_artifacts(self):
        previous, old_bundle = self.prepare()
        paths_before = dict(self.paths), dict(self.comp_paths)
        artifacts = {path: Path(path).read_bytes() for path in (*self.paths.values(), *self.comp_paths.values())}
        old_metadata = old_bundle.json("/api/tft/meta.json")

        def provenance():
            self.snap.meta["sources"]["metatft"]["url"] = "https://example.test/source-b"
            self.meta["sources"] = deepcopy(self.snap.meta["sources"])

        def assets():
            self.snap.communitydragon["items"][0]["icon"] = "assets/item-b.png"
            self.comp_meta["items"] = deepcopy(self.snap.communitydragon["items"])

        def limitations():
            self.snap.audit["unresolved"][0]["detail"] = "review updated the limitation"
            self.meta["sourceLimitations"] = deepcopy(self.snap.audit["unresolved"])

        for change in (provenance, assets, limitations):
            with self.subTest(change=change.__name__):
                change()
                descriptor, bundle = self.prepare()
                self.assertNotEqual(descriptor["siteGeneration"], previous["siteGeneration"])
                self.assertEqual(descriptor["baselineRevision"], previous["baselineRevision"])
                self.assertEqual(descriptor["compositionRevision"], previous["compositionRevision"])
                self.assertNotEqual(bundle.manifest["presentationHash"],
                                    tft_site.load(previous).manifest["presentationHash"])
                self.assertEqual((self.paths, self.comp_paths), paths_before)
                self.assertEqual({path: Path(path).read_bytes() for path in artifacts}, artifacts)
                self.assertEqual(bundle.json("/api/tft/meta.json"),
                                 dict(self.meta, publicationRevision=descriptor["siteGeneration"]))
                self.assertEqual(bundle.json("/api/tft/compositions/meta.json"),
                                 dict(self.comp_meta, publicationRevision=descriptor["siteGeneration"]))
                for url in ("/api/tft/status.json", "/api/tft/compositions/status.json"):
                    self.assertEqual(bundle.json(url)["publicationRevision"], descriptor["siteGeneration"])
                    self.assertNotEqual(bundle.json(url)["publicationRevision"],
                                        tft_site.load(previous).json(url)["publicationRevision"])
                for url in self.payloads:
                    self.assertEqual(bundle.entries[url], old_bundle.entries[url])
                previous = descriptor
        self.assertEqual(old_bundle.json("/api/tft/meta.json"), old_metadata)

    def test_check_timestamps_alone_reuse_saved_responses(self):
        descriptor, bundle = self.prepare()
        saved_meta = bundle.json("/api/tft/meta.json")
        original_hash = bundle.manifest["presentationHash"]
        self.snap.meta.update(fetchedAt="next-fetch", verifiedAt="next-check", checkedAt="next-check")
        self.snap.audit.update(checkedAt="next-check", baselineReviewedAt="next-check")
        # The static metadata describes the retained publication. Fresh
        # operational times are already provided by TftResponses.refresh.
        with patch.object(tft, "api_meta", side_effect=AssertionError("timestamp-only metadata rebuild")), \
                patch.object(tft_site, "_compress", side_effect=AssertionError("timestamp-only compression")):
            self.assertEqual(tft_site.prepare(self.snap), descriptor)
        self.assertEqual(tft_site._presentation_hash(self.snap), original_hash)
        self.assertEqual(bundle.json("/api/tft/meta.json"), saved_meta)
        self.assertEqual(len(list(self.site.glob("g-*"))), 1)

    def test_v1_publication_without_presentation_hash_remains_readable(self):
        descriptor, bundle = self.prepare()
        manifest = deepcopy(bundle.manifest)
        manifest.pop("presentationHash")
        manifest["siteGeneration"] = tft_site._generation(
            manifest["baselineRevision"], manifest["compositionRevision"], manifest["publisherHash"])
        for url in ("/api/tft/meta.json", "/api/tft/compositions/meta.json",
                    "/api/tft/status.json", "/api/tft/compositions/status.json"):
            payload = bundle.json(url)
            payload.pop("publicationRevision")
            tft_site._write(bundle.directory, url, payload)
            manifest["entries"][url] = tft_site._compress(bundle.directory, url)
        raw = tft_site._json_bytes(manifest)
        (bundle.directory / "manifest.json").write_bytes(raw)
        legacy = tft_site._descriptor(manifest, raw)
        os.rename(bundle.directory, self.site / legacy["siteGeneration"])
        loaded = tft_site.load(legacy)
        self.assertNotIn("presentationHash", loaded.manifest)
        self.assertEqual(loaded.json("/api/tft/meta.json"), self.meta)
        self.assertNotIn("publicationRevision", loaded.json("/api/tft/status.json"))
        next_descriptor, _ = self.prepare()
        self.assertNotEqual(next_descriptor["siteGeneration"], legacy["siteGeneration"])
        self.assertEqual(tft_site.load(legacy).json("/api/tft/meta.json"), self.meta)

    def test_presentation_hash_is_validated_and_bound_to_generation(self):
        descriptor, bundle = self.prepare()
        original = (bundle.directory / "manifest.json").read_bytes()
        for value in (None, "bad-hash", "f" * 64):
            with self.subTest(value=value):
                altered = self.rewrite_manifest(descriptor, lambda manifest: manifest.update(presentationHash=value))
                with self.assertRaisesRegex(ValueError, "presentation hash|generation does not match"):
                    tft_site.load(altered)
                (bundle.directory / "manifest.json").write_bytes(original)

    def test_presentation_change_during_preparation_prevents_publication(self):
        compress = tft_site._compress

        def changed(directory, url):
            result = compress(directory, url)
            self.snap.audit["unresolved"][0]["detail"] = "changed during preparation"
            return result

        with patch.object(tft_site, "_compress", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "inputs changed"):
                tft_site.prepare(self.snap)
        self.assert_no_publication()

    def test_missing_champion_or_composition_blocks_preparation(self):
        for path in (next(iter(self.paths.values())), next(iter(self.comp_paths.values()))):
            with self.subTest(path=path):
                source = Path(path)
                raw = source.read_bytes()
                source.unlink()
                with self.assertRaisesRegex(ValueError, "calculations must finish"):
                    tft_site.prepare(self.snap)
                source.write_bytes(raw)
                self.assert_no_publication()

    def test_incomplete_cores_leaderboard_and_wrong_composition_are_rejected(self):
        with patch.object(tft, "cached_core_contexts", return_value={}):
            with self.assertRaisesRegex(ValueError, "core comparisons are incomplete"):
                tft_site.prepare(self.snap)
        self.assert_no_publication()
        with patch.object(tft, "cached_leaderboard", return_value={"complete": False}):
            with self.assertRaisesRegex(ValueError, "leaderboard is incomplete"):
                tft_site.prepare(self.snap)
        self.assert_no_publication()
        path = Path(next(iter(self.comp_paths.values())))
        payload = json.loads(path.read_bytes())
        payload["revision"] = "wrong"
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "composition does not match"):
            tft_site.prepare(self.snap)
        self.assert_no_publication()

    def test_source_or_revision_change_during_build_preserves_previous_generation(self):
        descriptor, old = self.prepare()
        previous = {path: path.read_bytes() for path in old.directory.rglob("*") if path.is_file()}
        with patch.object(tft, "snapshot_revision", return_value="next-baseline"), \
                patch.object(tft, "api_meta", return_value=dict(self.meta, revision="next-baseline")), \
                patch.object(tft_comps, "api_meta", return_value=dict(self.comp_meta, baselineRevision="next-baseline")), \
                patch.object(tft_site, "_link", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                tft_site.prepare(self.snap)
        self.assertEqual(previous, {path: path.read_bytes() for path in previous})
        self.assertEqual(tft_site.load(descriptor).descriptor, descriptor)
        self.assertEqual(list(self.site.glob(".stage-*")), [])
        with patch.object(tft, "source_stale", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                tft_site.prepare(self.snap)

    def test_revision_change_at_final_check_does_not_publish(self):
        compress = tft_site._compress

        def changed(directory, url):
            result = compress(directory, url)
            tft.snapshot_revision.return_value = "changed-baseline"
            return result

        with patch.object(tft_site, "_compress", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "inputs changed"):
                tft_site.prepare(self.snap)
        self.assert_no_publication()

    def test_atomic_source_replacement_during_build_is_rejected(self):
        compress = tft_site._compress
        source = Path(next(iter(self.paths.values())))
        replacement = source.with_suffix(".new")
        replacement.write_bytes(source.read_bytes())

        def changed(directory, url):
            result = compress(directory, url)
            if replacement.exists():
                os.replace(replacement, source)
            return result

        with patch.object(tft_site, "_compress", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "inputs changed"):
                tft_site.prepare(self.snap)
        self.assert_no_publication()

    def test_hardlink_falls_back_to_copy_and_pruning_does_not_remove_published_files(self):
        with patch.object(tft_site.os, "link", side_effect=OSError(errno.EXDEV, "cross-device")):
            descriptor, bundle = self.prepare()
        source = Path(next(iter(self.paths.values())))
        url = next(iter(self.payloads))
        self.assertNotEqual(source.stat().st_ino, bundle.asset(url)["path"].stat().st_ino)
        for path in (*self.paths.values(), *self.comp_paths.values()):
            Path(path).unlink()
        self.assertEqual(tft_site.load(descriptor).json(url), self.payloads[url])

    def test_gzip_is_deterministic_across_fresh_publications(self):
        _, before = self.prepare()
        second = self.directory / "another-site"
        with patch.object(tft_site, "CACHE_DIR", second):
            self.site = second  # The mock leaderboard checks its provided staging paths.
            _, after = self.prepare()
        for url in before.entries:
            self.assertEqual(before.asset(url, compressed=True)["path"].read_bytes(),
                             after.asset(url, compressed=True)["path"].read_bytes())
        self.assertEqual(before.descriptor, after.descriptor)

    def test_manifest_digest_missing_representation_and_unsafe_paths_are_rejected(self):
        descriptor, bundle = self.prepare()
        wrong = dict(descriptor, manifestHash="0" * 64)
        with self.assertRaisesRegex(ValueError, "manifest hash"):
            tft_site.load(wrong)
        with self.assertRaisesRegex(ValueError, "generation"):
            tft_site.load(dict(descriptor, siteGeneration="../outside"))
        original = (bundle.directory / "manifest.json").read_bytes()
        edits = (
            lambda manifest: manifest["entries"].pop("/api/tft/meta.json"),
            lambda manifest: manifest["entries"]["/api/tft/meta.json"].pop("gzip"),
            lambda manifest: manifest["entries"]["/api/tft/meta.json"]["identity"].update(path="../outside"),
            lambda manifest: manifest["inventory"]["baselineCells"].append(["..", "outside"]),
            lambda manifest: manifest["entries"]["/api/tft/meta.json"]["identity"].update(size=True),
        )
        for edit in edits:
            with self.subTest(edit=edit):
                altered = self.rewrite_manifest(descriptor, edit)
                with self.assertRaises(ValueError):
                    tft_site.load(altered)
                (bundle.directory / "manifest.json").write_bytes(original)

    def test_missing_truncated_or_symlinked_file_is_rejected_even_on_reuse(self):
        descriptor, bundle = self.prepare()
        asset = bundle.asset("/api/tft/meta.json", compressed=True)["path"]
        raw = asset.read_bytes()
        for damage in (lambda: asset.unlink(), lambda: asset.write_bytes(raw[:-1]),
                       lambda: (asset.unlink(), asset.symlink_to(self.directory / "outside.gz"))):
            with self.subTest(damage=damage):
                damage()
                with self.assertRaises(ValueError):
                    tft_site.load(descriptor)
                with self.assertRaises(ValueError):
                    tft_site.prepare(self.snap)
                asset.unlink(missing_ok=True)
                asset.write_bytes(raw)


if __name__ == "__main__":
    unittest.main()
