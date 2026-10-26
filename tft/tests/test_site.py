"""TFT publication preparation, manifest validation, and HTTP serving checks."""

from contextlib import ExitStack
from copy import deepcopy
import errno
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from tft import early as tft_early, flex as tft_flex, sim as tft, site as tft_site
from web import server as webapp


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
        self.leaderboards = {"best-clump-bare-mixed": {}, "s2-clump-bare-mixed": {}}
        self.meta = {"revision": "baseline", "patch": self.snap.patch, "units": [],
                     "tankDummies": {"mixed": [{"hp": 3000, "armor": 110}]},
                     "sources": deepcopy(self.snap.meta["sources"]),
                     "sourceLimitations": deepcopy(self.snap.audit["unresolved"])}
        for owner, name, value in (
                (tft, "source_stale", False), (tft, "snapshot_revision", "baseline"),
                (tft, "cell_paths", self.paths), (tft, "leaderboard_scenarios", self.leaderboards),
                (tft_early, "source_stale", False)):
            self.stack.enter_context(patch.object(owner, name, return_value=value))
        self.early_supported = self.stack.enter_context(patch.object(tft_early, "supports", return_value=False))
        self.early_warm = self.stack.enter_context(patch.object(tft_early, "warm",
            side_effect=AssertionError("unsupported fixture must not warm early boards")))
        self.early_read = self.stack.enter_context(patch.object(tft_early, "cached_catalog",
            side_effect=AssertionError("unsupported fixture must not read early boards")))
        self.early_revision = self.stack.enter_context(patch.object(tft_early, "revision",
            side_effect=AssertionError("unsupported fixture has no early revision")))
        self.metadata = self.stack.enter_context(patch.object(tft, "api_meta", return_value=self.meta))
        self.leaderboard = self.stack.enter_context(
            patch.object(tft, "cached_leaderboard", side_effect=self.board))
        self.stack.enter_context(patch.object(tft, "load_snapshot", side_effect=AssertionError("snapshot reloaded")))
        self.stack.enter_context(patch.object(tft, "warm", side_effect=AssertionError("baseline warm")))

    def board(self, key, paths, *, snap):
        self.assertIs(snap, self.snap)
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
        self.assertEqual(set(descriptor), {"siteGeneration", "baselineRevision", "manifestHash"})
        self.assertEqual(set(bundle.manifest["inventory"]), {"baselineCells", "leaderboardSelections"})
        expected = set(self.payloads) | {
            "/api/tft/meta.json", "/api/tft/status.json", "/api/tft/ahri/cores.json",
            "/api/tft/leona/cores.json", *(
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
        status = bundle.json("/api/tft/status.json")
        self.assertEqual(status, {"warmer": "idle", "patch": self.snap.patch,
                         "revision": "baseline", "refresh": {},
                         "publicationRevision": descriptor["siteGeneration"],
                         "ready": {f"{slug}/{key}": True for slug, key in self.paths}})
        self.assertNotIn("publicationRevision", self.meta)
        self.metadata.assert_called_once_with(self.snap)
        self.assertEqual(self.leaderboard.call_count, len(self.leaderboards))
        self.assertEqual(list(self.site.glob(".stage-*")), [])
        self.early_warm.assert_not_called()
        self.early_read.assert_not_called()

    def test_loaded_publication_never_uses_models_or_current_publisher_hash(self):
        descriptor, _ = self.prepare()
        with ExitStack() as stack:
            for name in ("source_stale", "snapshot_revision", "cell_paths", "api_meta",
                         "cached_scenario", "cached_core_contexts", "cached_leaderboard"):
                stack.enter_context(patch.object(tft, name, side_effect=AssertionError(name)))
            stack.enter_context(patch.object(tft_site, "SOURCE_HASH", "f" * 64))
            bundle = tft_site.load(descriptor)
            self.assertEqual(bundle.json("/api/tft/meta.json"),
                             dict(self.meta, publicationRevision=descriptor["siteGeneration"]))
            self.assertIsNone(bundle.asset("/api/tft/missing.json"))
            with self.assertRaises(KeyError):
                bundle.json("/api/tft/missing.json")

    def test_metadata_formations_match_combat_positions_without_mutating_source(self):
        self.meta["dummy"] = {"slots": [{"hp": 3000, "nearby": True},
            *({"hp": 1800, "nearby": True} for _ in range(3)),
            *({"hp": 1440, "nearby": False} for _ in range(2))]}
        original = deepcopy(self.meta)
        with patch.object(tft, "engine", side_effect=AssertionError("formation catalog simulated")):
            descriptor, bundle = self.prepare()
        formations = bundle.json("/api/tft/meta.json")["formations"]
        self.assertEqual(formations, {
            "clump": [{"lane": 2, "row": 0}, {"lane": 3, "row": 0},
                      {"lane": 4, "row": 0}, {"lane": 5, "row": 0},
                      {"lane": 2, "row": 2}, {"lane": 4, "row": 2}],
            "spread": [{"lane": 0, "row": 0}, {"lane": 2, "row": 0},
                       {"lane": 4, "row": 0}, {"lane": 6, "row": 0},
                       {"lane": 1, "row": 2}, {"lane": 5, "row": 2}],
        })
        self.assertEqual(self.meta, original)
        self.assertEqual(bundle.json("/api/tft/meta.json")["dummy"], original["dummy"])
        with patch.object(tft, "board_positions", side_effect=AssertionError("request-time formation")):
            self.assertEqual(tft_site.load(descriptor).json("/api/tft/meta.json")["formations"], formations)

    def test_missing_or_unpositioned_dummies_have_no_invented_formation(self):
        for dummy in (None, {}, {"slots": []}, {"slots": [{"hp": 3000}, {"hp": 1800}]}):
            with self.subTest(dummy=dummy):
                meta = dict(self.meta, dummy=dummy)
                original = deepcopy(meta)
                self.assertIsNone(tft_site._formations(meta))
                self.assertEqual(meta, original)
        descriptor, bundle = self.prepare()
        self.assertNotIn("formations", bundle.json("/api/tft/meta.json"))

    def test_formation_catalog_respects_explicit_source_positions(self):
        meta = dict(self.meta, dummy={"slots": [{"lane": 6, "row": 1, "hp": 3000},
                                               {"lane": 1, "row": 3, "hp": 1440}]})
        original = deepcopy(meta)
        expected = [{"lane": 6, "row": 1}, {"lane": 1, "row": 3}]
        self.assertEqual(tft_site._formations(meta), {geometry: expected for geometry in tft.GEOMETRIES})
        self.assertEqual(meta, original)

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
        paths_before = dict(self.paths)
        artifacts = {path: Path(path).read_bytes() for path in self.paths.values()}
        old_metadata = old_bundle.json("/api/tft/meta.json")

        def provenance():
            self.snap.meta["sources"]["metatft"]["url"] = "https://example.test/source-b"
            self.meta["sources"] = deepcopy(self.snap.meta["sources"])

        def assets():
            self.snap.communitydragon["items"][0]["icon"] = "assets/item-b.png"

        def limitations():
            self.snap.audit["unresolved"][0]["detail"] = "review updated the limitation"
            self.meta["sourceLimitations"] = deepcopy(self.snap.audit["unresolved"])

        for change in (provenance, assets, limitations):
            with self.subTest(change=change.__name__):
                change()
                descriptor, bundle = self.prepare()
                self.assertNotEqual(descriptor["siteGeneration"], previous["siteGeneration"])
                self.assertEqual(descriptor["baselineRevision"], previous["baselineRevision"])
                self.assertNotEqual(bundle.manifest["presentationHash"],
                                    tft_site.load(previous).manifest["presentationHash"])
                self.assertEqual(self.paths, paths_before)
                self.assertEqual({path: Path(path).read_bytes() for path in artifacts}, artifacts)
                self.assertEqual(bundle.json("/api/tft/meta.json"),
                                 dict(self.meta, publicationRevision=descriptor["siteGeneration"]))
                self.assertEqual(bundle.json("/api/tft/status.json")["publicationRevision"], descriptor["siteGeneration"])
                self.assertNotEqual(bundle.json("/api/tft/status.json")["publicationRevision"],
                                    tft_site.load(previous).json("/api/tft/status.json")["publicationRevision"])
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
        with patch.object(tft, "api_meta", side_effect=AssertionError("timestamp-only metadata rebuild")), \
                patch.object(tft_site, "_compress", side_effect=AssertionError("timestamp-only compression")):
            self.assertEqual(tft_site.prepare(self.snap), descriptor)
        self.assertEqual(tft_site._presentation_hash(self.snap), original_hash)
        self.assertEqual(bundle.json("/api/tft/meta.json"), saved_meta)
        self.assertEqual(len(list(self.site.glob("g-*"))), 1)

    def add_trait_catalog(self):
        snapshot = tft.Snapshot(18, "18.3")
        self.snap.patch = snapshot.patch
        self.meta["patch"] = snapshot.patch
        self.snap.units = deepcopy(snapshot.units)
        self.snap.traits = deepcopy(snapshot.traits)
        self.snap.raw = deepcopy(snapshot.raw)
        self.snap.dir = self.directory / self.snap.patch
        tft_flex.write_document(self.snap, deepcopy(tft_flex.catalog_profiles(snapshot)))

    def test_trait_catalog_is_prepared_and_pinned_without_combat(self):
        self.add_trait_catalog()
        with patch.object(tft, "engine", side_effect=AssertionError("trait catalog simulated")):
            descriptor, bundle = self.prepare()
        url = "/api/tft/traits.json"
        self.assertTrue(bundle.manifest["inventory"]["traitFlex"])
        self.assertIn(url, bundle.entries)
        result = bundle.json(url)
        self.assertEqual(result["publicationRevision"], descriptor["siteGeneration"])
        self.assertEqual({unit["api"] for unit in result["units"]}, set(self.snap.units))
        self.assertEqual(result["revision"], tft_flex.catalog(self.snap)["revision"])
        self.assertIn("teamPlanner", result)
        self.assertEqual(result["lobbyStats"]["schema"], "tft-lobby-stats-v1")
        self.assertEqual(len(result["lobbyStats"]["profiles"]), 2)
        identity = bundle.asset(url)["path"].read_bytes()
        self.assertEqual(gzip.decompress(bundle.asset(url, compressed=True)["path"].read_bytes()), identity)
        with patch.object(tft_flex, "catalog", side_effect=AssertionError("request-time catalog")):
            self.assertEqual(tft_site.load(descriptor).json(url), result)
        self.snap.traits["DA_18_Spellweaver"]["levels"] = [2, 5, 6]
        current, changed = self.prepare()
        self.assertNotEqual(current["siteGeneration"], descriptor["siteGeneration"])
        self.assertNotEqual(changed.json(url)["revision"], result["revision"])
        self.assertEqual(current["baselineRevision"], descriptor["baselineRevision"])
        for source in self.payloads:
            self.assertEqual(changed.entries[source], bundle.entries[source])
        self.assertEqual(bundle.json(url), result)

    def test_trait_inventory_requires_its_complete_saved_endpoint(self):
        self.add_trait_catalog()
        descriptor, _ = self.prepare()
        bad = self.rewrite_manifest(descriptor, lambda manifest: manifest["entries"].pop("/api/tft/traits.json"))
        with self.assertRaisesRegex(ValueError, "every expected endpoint"):
            tft_site.load(bad)

    def test_stale_trait_catalog_source_prevents_publication(self):
        with patch.object(tft_flex, "source_stale", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                self.prepare()
        self.assert_no_publication()

    def add_early_catalog(self):
        self.add_trait_catalog()
        self.early = {"schema": "tft-early-comps-v1", "set": self.snap.set_no,
                      "patch": self.snap.patch, "revision": "early-a", "baselineRevision": "baseline",
                      "complete": True, "variants": [{"key": "level4", "results": [{"score": 51.25}]}]}
        self.early_supported.return_value = True
        self.early_warm.side_effect = None
        self.early_warm.return_value = 0
        self.early_read.side_effect = None
        self.early_read.return_value = self.early
        self.early_revision.side_effect = None
        self.early_revision.return_value = self.early["revision"]

    def test_early_catalog_is_warmed_saved_and_read_without_live_calculations(self):
        self.add_early_catalog()
        descriptor, bundle = self.prepare()
        self.early_warm.assert_called_once_with(snap=self.snap)
        url = "/api/tft/early.json"
        self.assertTrue(bundle.manifest["inventory"]["earlyComps"])
        result = bundle.json(url)
        self.assertEqual(result["publicationRevision"], descriptor["siteGeneration"])
        self.assertEqual(result["revision"], self.early["revision"])
        self.assertEqual(result["baselineRevision"], descriptor["baselineRevision"])
        self.assertIn("teamPlanner", result)
        self.assertNotIn("publicationRevision", self.early)
        self.assertNotIn("teamPlanner", self.early)
        self.assertEqual(gzip.decompress(bundle.asset(url, compressed=True)["path"].read_bytes()),
                         bundle.asset(url)["path"].read_bytes())
        with ExitStack() as stack:
            for name in ("supports", "revision", "cached_catalog", "warm", "source_stale"):
                stack.enter_context(patch.object(tft_early, name, side_effect=AssertionError(name)))
            self.assertEqual(tft_site.load(descriptor).json(url), result)

    def test_early_evidence_changes_republish_without_recomputing_other_artifacts(self):
        self.add_early_catalog()
        descriptor, before = self.prepare()
        saved = before.json("/api/tft/early.json")
        self.early["variants"][0]["results"][0]["score"] = 57.5
        current, after = self.prepare()
        self.assertNotEqual(descriptor["siteGeneration"], current["siteGeneration"])
        self.assertEqual(descriptor["baselineRevision"], current["baselineRevision"])
        for source in self.payloads:
            self.assertEqual(before.entries[source], after.entries[source])
        self.assertEqual(before.json("/api/tft/early.json"), saved)
        self.assertEqual(after.json("/api/tft/early.json")["variants"], self.early["variants"])
        self.early["revision"] = self.early_revision.return_value = "early-b"
        updated, _ = self.prepare()
        self.assertNotEqual(current["siteGeneration"], updated["siteGeneration"])

    def test_early_inventory_requires_complete_endpoint_and_optional_flags(self):
        clean = {"baselineCells": [["ahri", "s1-clump-bare"]],
                 "leaderboardSelections": ["best-clump-bare"]}
        for flags in ({}, {"traitFlex": True}, {"earlyComps": True}, {"traitFlex": True, "earlyComps": True}):
            with self.subTest(flags=flags):
                urls = tft_site._endpoints(clean | flags)
                self.assertEqual("/api/tft/traits.json" in urls, bool(flags.get("traitFlex")))
                self.assertEqual("/api/tft/early.json" in urls, bool(flags.get("earlyComps")))
                self.assertFalse(any(url.startswith("/api/tft/compositions/") for url in urls))
        for flags in ({"earlyComps": False}, {"earlyComps": 1}, {"other": True}):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, "invalid TFT site inventory"):
                tft_site._endpoints(clean | flags)
        self.add_early_catalog()
        descriptor, _ = self.prepare()
        bad = self.rewrite_manifest(descriptor, lambda manifest: manifest["entries"].pop("/api/tft/early.json"))
        with self.assertRaisesRegex(ValueError, "every expected endpoint"):
            tft_site.load(bad)

    def test_inventory_rejects_inconsistent_or_nonboolean_flags(self):
        inventory = {"baselineCells": [["ahri", "s1-clump-bare"]],
                     "leaderboardSelections": ["best-clump-bare"]}
        invalid = (
            inventory | {"compositionArchived": False},
            inventory | {"compositionArchived": 1},
            inventory | {"compositionArchived": "true"},
            inventory | {"compositionContexts": ["c1-clump-mixed"]},
            inventory | {"baselineCells": []},
            inventory | {"leaderboardSelections": []},
        )
        for value in invalid:
            with self.subTest(inventory=value), self.assertRaises(ValueError):
                tft_site._endpoints(value)

    def test_unready_stale_or_mismatched_early_results_prevent_publication(self):
        self.add_early_catalog()
        self.early_warm.return_value = None
        with self.assertRaisesRegex(RuntimeError, "warm is running"):
            self.prepare()
        self.early_warm.return_value = 0
        self.early_read.side_effect = ValueError("early cache incomplete")
        with self.assertRaisesRegex(ValueError, "early cache incomplete"):
            self.prepare()
        self.early_read.side_effect = None
        for key, value in (("complete", False), ("revision", "wrong"), ("baselineRevision", "wrong"),
                           ("patch", "wrong"), ("set", 17)):
            with self.subTest(key=key):
                original = self.early[key]
                self.early[key] = value
                with self.assertRaisesRegex(ValueError, "early compositions do not match"):
                    self.prepare()
                self.early[key] = original
        with patch.object(tft_early, "source_stale", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                self.prepare()
        self.assert_no_publication()

    def test_changing_early_evidence_during_preparation_does_not_publish(self):
        self.add_early_catalog()
        link = tft_site._link

        def changed(source, destination):
            link(source, destination)
            self.early["variants"][0]["results"][0]["score"] += 1

        with patch.object(tft_site, "_link", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "inputs changed"):
                self.prepare()
        self.assert_no_publication()

    def test_early_payload_is_hashed_from_the_same_read_that_will_be_published(self):
        self.add_early_catalog()
        changed = deepcopy(self.early)
        changed["variants"][0]["results"][0]["score"] += 1
        self.early_read.side_effect = [self.early, changed]
        with self.assertRaisesRegex(RuntimeError, "inputs changed"):
            self.prepare()
        self.assert_no_publication()

    def test_planner_catalog_republishes_without_recalculating_boards(self):
        self.add_early_catalog()
        catalog = {"schemaVersion": 1, "set": 18, "format": "tft-team-planner-v2", "slots": 10,
                   "unitCodes": {"TFT18_Ahri": "3e9", "TFT18_Leona": "411"}}
        path = self.directory / "team-planner.json"
        path.write_text(json.dumps(catalog))
        with patch.object(tft, "set_dir", return_value=str(self.directory)):
            previous, old = self.prepare()
            exposed = old.json("/api/tft/early.json")["teamPlanner"]
            self.assertEqual(exposed["unitCodes"], catalog["unitCodes"])
            self.assertEqual((exposed["set"], exposed["slots"], exposed["format"]),
                             (18, 10, "tft-team-planner-v2"))
            catalog["unitCodes"]["TFT18_Ahri"] = "3e8"
            path.write_text(json.dumps(catalog))
            current, bundle = self.prepare()
            self.assertNotEqual(current["siteGeneration"], previous["siteGeneration"])
            self.assertEqual(current["baselineRevision"], previous["baselineRevision"])
            for url in self.payloads:
                self.assertEqual(bundle.entries[url], old.entries[url])
            self.assertEqual(bundle.json("/api/tft/early.json")["teamPlanner"]["unitCodes"],
                             catalog["unitCodes"])
            self.assertEqual(old.json("/api/tft/early.json")["teamPlanner"], exposed)

    def test_invalid_or_ambiguous_planner_ids_cannot_be_published(self):
        self.snap.units = {"TFT18_Ahri": {}, "TFT18_Leona": {}}
        catalog = {"schemaVersion": 1, "set": 18, "format": "tft-team-planner-v2", "slots": 10,
                   "unitCodes": {"TFT18_Ahri": "3e9", "TFT18_Leona": "411"}}
        with patch.object(tft, "set_dir", return_value=str(self.directory)):
            self.assertIsNone(tft_site._team_planner(self.snap))
            for bad in ("000", "not-code", "1234", "411", 1001, None):
                value = deepcopy(catalog)
                value["unitCodes"]["TFT18_Ahri"] = bad
                (self.directory / "team-planner.json").write_text(json.dumps(value))
                with self.subTest(code=bad), self.assertRaisesRegex(ValueError, "Team Planner catalog"):
                    self.prepare()

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

    def test_missing_champion_blocks_preparation(self):
        for path in self.paths.values():
            with self.subTest(path=path):
                source = Path(path)
                raw = source.read_bytes()
                source.unlink()
                with self.assertRaisesRegex(ValueError, "calculations must finish"):
                    tft_site.prepare(self.snap)
                source.write_bytes(raw)
                self.assert_no_publication()

    def test_incomplete_cores_and_leaderboard_are_rejected(self):
        with patch.object(tft, "cached_core_contexts", return_value={}):
            with self.assertRaisesRegex(ValueError, "core comparisons are incomplete"):
                tft_site.prepare(self.snap)
        self.assert_no_publication()
        with patch.object(tft, "cached_leaderboard", return_value={"complete": False}):
            with self.assertRaisesRegex(ValueError, "leaderboard is incomplete"):
                tft_site.prepare(self.snap)
        self.assert_no_publication()
        with patch.object(tft, "api_meta", return_value=dict(self.meta, revision="wrong")), \
                self.assertRaisesRegex(ValueError, "metadata does not match"):
            tft_site.prepare(self.snap)
        self.assert_no_publication()

    def test_source_or_revision_change_during_build_preserves_previous_generation(self):
        descriptor, old = self.prepare()
        previous = {path: path.read_bytes() for path in old.directory.rglob("*") if path.is_file()}
        with patch.object(tft, "snapshot_revision", return_value="next-baseline"), \
                patch.object(tft, "api_meta", return_value=dict(self.meta, revision="next-baseline")), \
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
        for path in self.paths.values():
            Path(path).unlink()
        self.assertEqual(tft_site.load(descriptor).json(url), self.payloads[url])

    def test_gzip_is_deterministic_across_fresh_publications(self):
        _, before = self.prepare()
        second = self.directory / "another-site"
        with patch.object(tft_site, "CACHE_DIR", second):
            self.site = second
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


class Handler:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.sent_headers = {}
        self.wfile = io.BytesIO()
        self.code = None

    def send_response(self, code):
        self.code = code

    def send_header(self, name, value):
        self.sent_headers[name] = value

    def end_headers(self):
        pass

    def _json(self, value, code=200):
        self.code = code
        self.sent_headers["Cache-Control"] = "no-store"
        self.wfile.write(json.dumps(value).encode())


class TestTftSavedResponses(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(tft, "CACHE_DIR", str(self.directory)))
        self.stack.enter_context(patch.object(tft, "REFRESH_STATE_FILE", str(self.directory / "refresh.json")))
        for module, names in ((tft, ("load_snapshot", "engine", "api_meta", "warm", "cached_scenario")),
                              (tft_early, ("warm", "revision", "cached_catalog", "supports")),
                              (tft_flex, ("catalog",))):
            for name in names:
                self.stack.enter_context(patch.object(module, name, side_effect=AssertionError("HTTP must not calculate: " + name)))
        self.marker = self.directory / ".dashboard-ready"
        self.marker.write_text(json.dumps({"revision": "base-a", "site": {"siteGeneration": "site-a"}}))
        self.statuses = {
            "/api/tft/status.json": {"revision": "base-a", "ready": {"champion/cell": True}, "warmer": "idle"},
        }
        self.payload = b'{"revision":"base-a","results":[1,2,3],"value":-0.0}'
        self.identity = self.directory / "response.json"
        self.identity.write_bytes(self.payload)
        self.compressed = self.directory / "response.json.gz"
        self.compressed.write_bytes(gzip.compress(self.payload, mtime=0))

        def asset(url, *, compressed=False):
            if url not in ("/api/tft/meta.json", "/api/tft/traits.json", "/api/tft/early.json"):
                return None
            path = self.compressed if compressed else self.identity
            return {"path": path, "size": path.stat().st_size,
                    "etag": '"' + hashlib.sha256(path.read_bytes()).hexdigest() + '"',
                    "encoding": "gzip" if compressed else None}

        self.bundle = SimpleNamespace(manifest={"baselineRevision": "base-a"},
                                      json=Mock(side_effect=lambda url: self.statuses[url]), asset=asset)
        self.load = self.stack.enter_context(patch.object(tft_site, "load", return_value=self.bundle))

    def test_startup_reads_only_active_status_from_a_pinned_publication(self):
        server = webapp.TftResponses()
        self.assertIs(server.bundle, self.bundle)
        self.bundle.json.assert_called_once_with("/api/tft/status.json")

    def test_identity_and_gzip_serve_prebuilt_bytes_without_calculating(self):
        server = webapp.TftResponses()
        for accept in ("", "gzip", "br, gzip;q=0.8"):
            with self.subTest(accept=accept):
                handler = Handler({"Accept-Encoding": accept})
                server.send(handler, "/api/tft/meta.json")
                self.assertEqual(handler.code, 200)
                body = handler.wfile.getvalue()
                if accept:
                    self.assertEqual(handler.sent_headers["Content-Encoding"], "gzip")
                    body = gzip.decompress(body)
                self.assertEqual(body, self.payload)
                self.assertEqual(handler.sent_headers["Vary"], "Accept-Encoding")
                self.assertEqual(handler.sent_headers["Cache-Control"], "no-cache")

    def test_conditional_requests_use_the_selected_representation_etag(self):
        server = webapp.TftResponses()
        raw = Handler()
        server.send(raw, "/api/tft/meta.json")
        tag = raw.sent_headers["ETag"]
        conditional = Handler({"If-None-Match": '"different", W/' + tag})
        server.send(conditional, "/api/tft/meta.json")
        self.assertEqual(conditional.code, 304)
        self.assertEqual(conditional.wfile.getvalue(), b"")
        gzip_request = Handler({"Accept-Encoding": "gzip", "If-None-Match": tag})
        server.send(gzip_request, "/api/tft/meta.json")
        self.assertEqual(gzip_request.code, 200)
        self.assertNotEqual(tag, gzip_request.sent_headers["ETag"])

    def test_trait_endpoint_reads_saved_bytes_and_revalidates_without_catalog_work(self):
        server = webapp.TftResponses()
        handler = Handler({"Accept-Encoding": "gzip"})
        server.send(handler, "/api/tft/traits.json")
        self.assertEqual(handler.code, 200)
        self.assertEqual(gzip.decompress(handler.wfile.getvalue()), self.payload)
        conditional = Handler({"Accept-Encoding": "gzip", "If-None-Match": handler.sent_headers["ETag"]})
        server.send(conditional, "/api/tft/traits.json")
        self.assertEqual(conditional.code, 304)
        self.assertEqual(conditional.wfile.getvalue(), b"")

    def test_early_endpoint_reads_only_saved_bytes_and_uses_their_etag(self):
        server = webapp.TftResponses()
        for accept in ("", "gzip"):
            with self.subTest(accept=accept):
                handler = Handler({"Accept-Encoding": accept})
                server.send(handler, "/api/tft/early.json")
                self.assertEqual(handler.code, 200)
                actual = handler.wfile.getvalue()
                self.assertEqual(gzip.decompress(actual) if accept else actual, self.payload)
                conditional = Handler({"Accept-Encoding": accept,
                                       "If-None-Match": handler.sent_headers["ETag"]})
                server.send(conditional, "/api/tft/early.json")
                self.assertEqual(conditional.code, 304)
                self.assertEqual(conditional.wfile.getvalue(), b"")

    def test_refresh_status_does_not_change_published_revision_or_readiness(self):
        server = webapp.TftResponses()
        Path(tft.REFRESH_STATE_FILE).write_text(json.dumps({"status": "running", "phase": "warming-builds"}))
        for url, original in self.statuses.items():
            handler = Handler()
            server.send(handler, url)
            value = json.loads(handler.wfile.getvalue())
            self.assertEqual(value["revision"], original["revision"])
            self.assertEqual(value["ready"], original["ready"])
            self.assertEqual(value["warmer"], "idle")
            self.assertEqual(value["refresh"]["status"], "running")
        self.assertNotIn("refresh", self.statuses["/api/tft/status.json"])

    def test_new_marker_does_not_mix_files_into_running_publication(self):
        server = webapp.TftResponses()
        self.marker.write_text(json.dumps({"revision": "base-b", "site": {"siteGeneration": "site-b"}}))
        handler = Handler()
        server.send(handler, "/api/tft/meta.json")
        self.assertEqual(handler.wfile.getvalue(), self.payload)
        self.load.assert_called_once_with({"siteGeneration": "site-a"})

    def test_missing_publication_returns_pending_without_fallback_calculation(self):
        self.marker.unlink()
        server = webapp.TftResponses()
        handler = Handler()
        server.send(handler, "/api/tft/meta.json")
        self.assertEqual(handler.code, 202)
        self.assertTrue(json.loads(handler.wfile.getvalue())["pending"])
        self.load.assert_not_called()

    def test_mismatched_marker_cannot_serve_a_mixed_generation(self):
        self.marker.write_text(json.dumps({"revision": "base-b", "site": {"siteGeneration": "site-a"}}))
        server = webapp.TftResponses()
        handler = Handler()
        server.send(handler, "/api/tft/meta.json")
        self.assertEqual(handler.code, 202)

    def test_unknown_path_is_not_resolved_as_a_file(self):
        server = webapp.TftResponses()
        for url in ("/api/tft/../../secret.json", "/api/tft/compositions/status.json"):
            with self.subTest(url=url):
                handler = Handler()
                server.send(handler, url)
                self.assertEqual(handler.code, 404)

    def test_missing_file_does_not_send_success_headers(self):
        server = webapp.TftResponses()
        self.identity.unlink()
        handler = Handler()
        with self.assertRaises(FileNotFoundError):
            server.send(handler, "/api/tft/meta.json")
        self.assertIsNone(handler.code)

    def test_serve_starts_only_the_existing_lol_warmer(self):
        args = SimpleNamespace(no_warm=False, no_open=True, host="127.0.0.1", port=0)
        with patch.object(webapp, "AutoWarm") as warmer, \
             patch("http.server.ThreadingHTTPServer") as http_server, \
             patch.object(webapp.signal, "signal"):
            webapp.cmd_serve(args)
        self.assertEqual(warmer.call_args_list, [call(enabled=True)])
        warmer.return_value.stop.assert_called_once()
        http_server.return_value.serve_forever.assert_called_once()


class TestEncodingNegotiation(unittest.TestCase):
    def test_gzip_quality_and_explicit_refusal(self):
        for value in ("gzip", "br, gzip ; q=0.3", "*;q=1", "GZIP;q=1"):
            self.assertTrue(webapp._accepts_gzip(value), value)
        for value in ("", "br", "gzip;q=0, *;q=1", "gzip;q=broken", "gzip;q=NaN", "gzip;q=2"):
            self.assertFalse(webapp._accepts_gzip(value), value)


if __name__ == "__main__":
    unittest.main()
