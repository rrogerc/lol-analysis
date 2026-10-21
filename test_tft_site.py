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
import tft_early
import tft_flex
import tft_lobby
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
        self.comp_payloads = {}
        for key in ("c1-clump-mixed", "c4-spread-mixed"):
            path = self.directory / f"{key}.json"
            payload = {"key": key, "revision": "composition", "baselineRevision": "baseline",
                       "methodology": {"evaluationModel": "symmetric-reference-pool-v1"},
                       "results": [{"score": 100 / 3, "items": ["C", "B", "A"]}]}
            path.write_text(json.dumps(payload) + "\n")
            self.comp_paths[key] = str(path)
            self.comp_payloads[f"/api/tft/compositions/{key}.json"] = payload
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
                (tft_early, "source_stale", False)):
            self.stack.enter_context(patch.object(owner, name, return_value=value))
        self.comp_models = {}
        for name in ("source_stale", "revision", "cell_paths", "api_meta", "warm", "cached_scenario"):
            self.comp_models[name] = self.stack.enter_context(patch.object(tft_comps, name,
                side_effect=AssertionError("archived composition model used: " + name)))
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
        self.assertEqual(descriptor["compositionRevision"], tft_site.ARCHIVED_COMPOSITION_REVISION)
        self.assertIs(bundle.manifest["inventory"]["compositionArchived"], True)
        self.assertEqual(bundle.manifest["inventory"]["compositionContexts"], [])
        expected = set(self.payloads) | {
            "/api/tft/meta.json", "/api/tft/status.json", "/api/tft/ahri/cores.json",
            "/api/tft/leona/cores.json", *(
                f"/api/tft/leaderboard/{key}.json" for key in self.leaderboards)}
        self.assertEqual(set(bundle.entries), expected)
        self.assertFalse(any(url.startswith("/api/tft/compositions/") for url in bundle.entries))
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
        self.assertNotIn("publicationRevision", self.comp_meta)
        self.metadata.assert_called_once_with(self.snap)
        for model in self.comp_models.values():
            model.assert_not_called()
        self.assertEqual(self.leaderboard.call_count, len(self.leaderboards))
        self.assertEqual(list(self.site.glob(".stage-*")), [])
        self.early_warm.assert_not_called()
        self.early_read.assert_not_called()

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
                for url in ("/api/tft/status.json",):
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

    def add_trait_catalog(self):
        snapshot = tft.Snapshot(18, "18.3")
        self.snap.patch = snapshot.patch
        self.meta["patch"] = snapshot.patch
        self.snap.units = deepcopy(snapshot.units)
        self.snap.traits = deepcopy(snapshot.traits)
        self.snap.raw = deepcopy(snapshot.raw)
        self.snap.dir = self.directory / self.snap.patch
        tft_lobby.write_document(self.snap, deepcopy(tft_lobby.catalog_profiles(snapshot)))

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
        for key in ("baselineRevision", "compositionRevision"):
            self.assertEqual(current[key], descriptor[key])
        for source in self.payloads:
            self.assertEqual(changed.entries[source], bundle.entries[source])
        self.assertEqual(bundle.json(url), result)

    def test_trait_inventory_requires_its_complete_saved_endpoint(self):
        self.add_trait_catalog()
        descriptor, bundle = self.prepare()
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
        # Even corrected evidence with the same model revision gets its own
        # immutable response; serving the previous generation remains valid.
        self.early["variants"][0]["results"][0]["score"] = 57.5
        current, after = self.prepare()
        self.assertNotEqual(descriptor["siteGeneration"], current["siteGeneration"])
        for key in ("baselineRevision", "compositionRevision"):
            self.assertEqual(descriptor[key], current[key])
        for source in self.payloads:
            self.assertEqual(before.entries[source], after.entries[source])
        self.assertEqual(before.json("/api/tft/early.json"), saved)
        self.assertEqual(after.json("/api/tft/early.json")["variants"], self.early["variants"])
        self.early["revision"] = self.early_revision.return_value = "early-b"
        updated, _ = self.prepare()
        self.assertNotEqual(current["siteGeneration"], updated["siteGeneration"])

    def test_early_inventory_requires_complete_endpoint_and_accepts_older_flags(self):
        legacy = {"baselineCells": [["ahri", "s1-clump-bare"]],
                  "compositionContexts": ["c1-clump-mixed"], "leaderboardSelections": ["best-clump-bare"]}
        archived = legacy | {"compositionContexts": [], "compositionArchived": True}
        for inventory in (legacy, archived):
            for flags in ({}, {"traitFlex": True}, {"earlyComps": True}, {"traitFlex": True, "earlyComps": True}):
                with self.subTest(inventory=inventory, flags=flags):
                    urls = tft_site._endpoints(inventory | flags)
                    self.assertEqual("/api/tft/traits.json" in urls, bool(flags.get("traitFlex")))
                    self.assertEqual("/api/tft/early.json" in urls, bool(flags.get("earlyComps")))
                    self.assertEqual("/api/tft/compositions/status.json" in urls, inventory is legacy)
        for flags in ({"earlyComps": False}, {"earlyComps": 1}, {"other": True}):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, "invalid TFT site inventory"):
                tft_site._endpoints(legacy | flags)
        self.add_early_catalog()
        descriptor, _ = self.prepare()
        bad = self.rewrite_manifest(descriptor, lambda manifest: manifest["entries"].pop("/api/tft/early.json"))
        with self.assertRaisesRegex(ValueError, "every expected endpoint"):
            tft_site.load(bad)

    def test_archived_inventory_rejects_inconsistent_or_nonboolean_flags(self):
        inventory = {"baselineCells": [["ahri", "s1-clump-bare"]], "compositionContexts": [],
                     "compositionArchived": True, "leaderboardSelections": ["best-clump-bare"]}
        self.assertFalse(any(url.startswith("/api/tft/compositions/")
                             for url in tft_site._endpoints(inventory)))
        invalid = (
            inventory | {"compositionArchived": False},
            inventory | {"compositionArchived": 1},
            inventory | {"compositionArchived": "true"},
            inventory | {"compositionContexts": ["c1-clump-mixed"]},
            {key: value for key, value in inventory.items() if key != "compositionArchived"},
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
            # Import-ID corrections are presentation inputs, not combat math.
            catalog["unitCodes"]["TFT18_Ahri"] = "3e8"
            path.write_text(json.dumps(catalog))
            current, bundle = self.prepare()
            self.assertNotEqual(current["siteGeneration"], previous["siteGeneration"])
            for key in ("baselineRevision", "compositionRevision"):
                self.assertEqual(current[key], previous[key])
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
    def test_v1_publication_without_presentation_hash_remains_readable(self):
        descriptor, bundle = self.prepare()
        manifest = deepcopy(bundle.manifest)
        manifest.pop("presentationHash")
        manifest["compositionRevision"] = "composition"
        manifest["inventory"].pop("compositionArchived")
        manifest["inventory"]["compositionContexts"] = sorted(self.comp_paths)
        manifest["siteGeneration"] = tft_site._generation(
            manifest["baselineRevision"], manifest["compositionRevision"], manifest["publisherHash"])
        for url in ("/api/tft/meta.json", "/api/tft/status.json"):
            payload = bundle.json(url)
            payload.pop("publicationRevision")
            tft_site._write(bundle.directory, url, payload)
            manifest["entries"][url] = tft_site._compress(bundle.directory, url)
        legacy_payloads = self.comp_payloads | {
            "/api/tft/compositions/meta.json": self.comp_meta,
            "/api/tft/compositions/status.json": {
                "revision": "composition", "ready": {key: True for key in self.comp_paths},
                "warmer": "idle", "progress": {}},
        }
        for url, payload in legacy_payloads.items():
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
        self.assertNotIn("compositionArchived", loaded.manifest["inventory"])
        for url, payload in legacy_payloads.items():
            self.assertEqual(loaded.json(url), payload)
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

    def test_missing_invalid_or_stale_archived_compositions_do_not_invalidate_results(self):
        descriptor, _ = self.prepare()
        for model, value in (("source_stale", True), ("revision", "changed-composition"),
                             ("cell_paths", {})):
            self.comp_models[model].side_effect = None
            self.comp_models[model].return_value = value
        for path in self.comp_paths.values():
            Path(path).write_text("invalid archived result")
        self.assertEqual(tft_site.prepare(self.snap), descriptor)
        for path in self.comp_paths.values():
            Path(path).unlink()
        self.assertEqual(tft_site.prepare(self.snap), descriptor)
        self.snap.meta["sources"]["metatft"]["url"] = "https://example.test/revised-source"
        self.meta["sources"] = deepcopy(self.snap.meta["sources"])
        updated, bundle = self.prepare()
        self.assertNotEqual(updated["siteGeneration"], descriptor["siteGeneration"])
        self.assertFalse(any(url.startswith("/api/tft/compositions/") for url in bundle.entries))
        for model in self.comp_models.values():
            model.assert_not_called()

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


class TestTeamPlannerCatalog(unittest.TestCase):
    def test_all_current_champions_match_the_archived_riot_planner_records(self):
        snap = tft.load_snapshot(18, "18.1d")
        catalog = json.loads((Path(tft.set_dir(18)) / "team-planner.json").read_bytes())
        planner = tft_site._team_planner(snap)
        self.assertEqual(set(planner["unitCodes"]), set(snap.units))
        self.assertEqual(len(set(planner["unitCodes"].values())), len(snap.units))
        raw = {unit["apiName"]: unit for unit in snap.raw["units"]}
        for api, code in planner["unitCodes"].items():
            source = catalog["sourceRecords"][api]
            with self.subTest(champion=api):
                self.assertIn(source["matchedAsset"], raw[api]["assetNames"])
                self.assertEqual(source["displayName"], snap.units[api]["name"])
                self.assertEqual(source["tier"], snap.units[api]["cost"])
                self.assertEqual(int(code, 16), source["teamPlannerCode"])
        # Riot renumbered these two in the 16.18 client (live 18.2). The 16.17 IDs the
        # catalog first pinned (405, 413) now import Lux for Ivern and nobody for Lux.
        self.assertEqual(planner["unitCodes"]["TFT18_Ivern"], "404")
        self.assertEqual(planner["unitCodes"]["TFT18_Lux_Base"], "405")
        self.assertNotIn("413", planner["unitCodes"].values())


if __name__ == "__main__":
    unittest.main()
