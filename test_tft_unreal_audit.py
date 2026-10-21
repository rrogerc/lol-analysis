"""Offline extraction guards: build identity, sparse coverage, and complete exports."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from jobs import tft_unreal_audit as audit


class TestUnrealAudit(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paks = self.root / "paks"
        self.paks.mkdir()
        self.manifest_path = self.root / "manifest.json"
        self.out = self.root / "out"
        executable = self.artifact("game.exe", b"matched executable")
        mapping = self.artifact("schema.usmap", b"matched mapping")
        proof = {"patch": "18.3", "build": "rls-18.3.0.5599856.Set18.Shipping.live",
                 "manifestId": "85BF576176126CA2", "exe": executable, "mapping": mapping}
        reflection = self.artifact("reflection.json", json.dumps(proof).encode())
        decoder = self.artifact("decoder", b"pinned decoder")
        decoder["files"] = [self.artifact("decoder.dll", b"pinned dependency")]
        self.manifest = {"patch": proof["patch"], "build": proof["build"],
                         "manifestId": proof["manifestId"], "engineVersion": "UE5_6", "paks": "paks",
                         "executable": executable, "mapping": mapping, "reflectionProvenance": reflection,
                         "decoder": decoder, "boundedObjectTypes": ["ChronoDef"],
                         "containers": [self.artifact("paks/pakchunk1-WindowsClient.utoc", b"toc")],
                         "packages": [], "champions": [], "expectedRoster": ["TFT18_Soraka"]}
        self.add_package("Soraka", 1)
        self.manifest["champions"] = [{"api": "TFT18_Soraka", "packages": [self.manifest["packages"][0]["path"]],
                                         "expectedCurvesByTable": {"CT_18_Soraka": {"DamageAP": [[1, 225], [2, 335]]}}}]
        self.save()

    def artifact(self, path, data):
        dest = self.root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return {"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def add_package(self, name, ident):
        self.manifest["packages"].append({"path": f"TFT/Set_18/Champions/{name}/CT_18_{name}.uasset",
                                          "packageId": str(ident), "size": 100})

    def save(self):
        self.manifest_path.write_text(json.dumps(self.manifest))

    def decode_document(self, selected, context):
        packages = []
        for path in selected:
            name = Path(path).stem
            packages.append({"path": path, "totalExports": 1, "exportFailures": [],
                             "exports": [{"exportIndex": 0, "data": {"Name": name, "Type": "CurveTable",
                                                                       "Rows": {"DamageAP": {"InterpMode": "ERichCurveInterpMode::RCIM_Constant",
                                                                                              "PreInfinityExtrap": "ERichCurveExtrapolation::RCCE_Constant",
                                                                                              "PostInfinityExtrap": "ERichCurveExtrapolation::RCCE_Constant",
                                                                                              "Keys": [{"Time": 1, "Value": 225},
                                                                                                       {"Time": 2, "Value": 335}]}}},
                                          "warnings": []}]})
        return {"metadata": {"strictObjectReads": True, "directory": context["paks"],
                             "mapping": context["pins"]["mapping"]["path"], "game": "GAME_UE5_6",
                             "fileCount": len(context["packages"]), "boundedObjectTypes": context["bounded"]},
                "packages": packages, "failures": [], "diagnostics": [],
                "boundedCurveReads": [{"exact": True, "endExclusive": 100, "expectedEndExclusive": 100}],
                "boundedObjectReads": []}

    def fake_decode(self, context, *, mutate=lambda document: None, exit_code=0):
        def run(command, *, env, stdout, stderr, timeout, check):
            self.assertEqual(command[1], "export-partial")
            self.assertEqual(env["TFT_STRICT_OBJECT_READS"], "1")
            self.assertNotIn("TFT_NATIVE_CURVES_ONLY", env)
            lines = Path(env["TFT_ASSET_ALLOWLIST"]).read_text().splitlines()
            self.assertEqual(len(lines), len(context["packages"]))
            self.assertTrue(all(len(line.split()) == 6 for line in lines))
            selected = [package["path"] for package in context["packages"] if re.fullmatch(command[5], package["path"])]
            self.assertLessEqual(len(selected), 32)
            document = self.decode_document(selected, context)
            mutate(document)
            Path(command[4]).write_text(json.dumps(document))
            return subprocess.CompletedProcess(command, exit_code)
        return run

    def test_batching_keeps_all_dependencies_available_and_replays_verified_outputs(self):
        for number in range(2, 36):
            self.add_package(f"Dependency{number}", number)
        self.save()
        context = audit.load_manifest(self.manifest_path)
        with patch.object(audit.subprocess, "run", side_effect=self.fake_decode(context)) as run:
            report = audit.audit(context, self.out, log=lambda _: None)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(report["complete"])
        self.assertEqual(report["decodedPackages"], 35)
        self.assertEqual(report["champions"][0]["curveStatus"], "matches")
        self.assertEqual(report["champions"][0]["curveKeysCompared"], 2)
        self.assertFalse(report["runtimeVerified"])
        with patch.object(audit.subprocess, "run", side_effect=AssertionError("verified cache should replay")):
            replay = audit.audit(audit.load_manifest(self.manifest_path), self.out, log=lambda _: None)
        self.assertEqual(replay["reusedBatches"], 2)

    def test_cache_tampering_forces_a_fresh_decode_even_when_json_remains_valid(self):
        context = audit.load_manifest(self.manifest_path)
        with patch.object(audit.subprocess, "run", side_effect=self.fake_decode(context)):
            audit.audit(context, self.out, log=lambda _: None)
        output = self.out / "batch-0001.json"
        data = json.loads(output.read_text())
        data["packages"][0]["exports"][0]["data"]["Rows"]["DamageAP"]["Keys"][1]["Value"] = 999
        output.write_text(json.dumps(data))
        with patch.object(audit.subprocess, "run", side_effect=self.fake_decode(context)) as run:
            report = audit.audit(context, self.out, log=lambda _: None)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(report["champions"][0]["curveStatus"], "matches")
        self.assertEqual(report["reusedBatches"], 0)

    def test_wrong_build_and_changed_source_bytes_are_rejected_before_any_decode(self):
        original = deepcopy(self.manifest)
        for key, value in (("build", "rls-18.4.0.other.Set18.Shipping.live"), ("patch", "18.4"),
                           ("manifestId", "1111111111111111")):
            with self.subTest(key=key):
                self.manifest = deepcopy(original)
                self.manifest[key] = value
                self.save()
                with self.assertRaisesRegex(audit.AuditError, "reflection source .* mismatch"):
                    audit.load_manifest(self.manifest_path)
        self.manifest = original
        self.save()
        (self.root / "schema.usmap").write_bytes(b"changed mapping")
        with self.assertRaisesRegex(audit.AuditError, "SHA256 mismatch"):
            audit.load_manifest(self.manifest_path)

    def test_full_build_identifier_matches_the_numeric_reflection_build(self):
        proof = json.loads((self.root / "reflection.json").read_text())
        proof["build"] = 5599856
        self.manifest["reflectionProvenance"] = self.artifact("reflection.json", json.dumps(proof).encode())
        self.save()
        audit.load_manifest(self.manifest_path)
        self.manifest["build"] = "rls-18.3.0.5599857.Set18.Shipping.live"
        self.save()
        with self.assertRaisesRegex(audit.AuditError, "reflection source build mismatch"):
            audit.load_manifest(self.manifest_path)

    def test_audit_cannot_write_into_production_directories(self):
        context = audit.load_manifest(self.manifest_path)
        with patch.object(audit.subprocess, "run", side_effect=AssertionError("must reject before decoding")):
            with self.assertRaisesRegex(audit.AuditError, "must stay in .cache"):
                audit.audit(context, audit.ROOT / "data/tft", log=lambda _: None)

    def test_missing_roster_champion_or_package_is_an_error(self):
        self.manifest["expectedRoster"].append("TFT18_Sivir")
        self.save()
        with self.assertRaisesRegex(audit.AuditError, "missing roster champions.*Sivir"):
            audit.load_manifest(self.manifest_path)
        self.manifest["expectedRoster"].pop()
        self.manifest["champions"][0]["packages"].append("TFT/Set_18/Missing.uasset")
        self.save()
        with self.assertRaisesRegex(audit.AuditError, "missing champion package"):
            audit.load_manifest(self.manifest_path)

    def test_malformed_json_package_ids_and_curve_keys_are_rejected(self):
        for data in ('{"patch": "18.3", "patch": "18.4"}', '{"value": NaN}', '{'):
            with self.subTest(data=data):
                self.manifest_path.write_text(data)
                with self.assertRaises(audit.AuditError):
                    audit.load_manifest(self.manifest_path)
        original = deepcopy(self.manifest)
        for value in (-1, 1 << 64, "abc", False):
            with self.subTest(packageId=value):
                self.manifest = deepcopy(original)
                self.manifest["packages"][0]["packageId"] = value
                self.save()
                with self.assertRaisesRegex(audit.AuditError, "invalid packageId"):
                    audit.load_manifest(self.manifest_path)
        self.manifest = original
        self.manifest["champions"][0]["expectedCurvesByTable"]["CT_18_Soraka"]["DamageAP"] = [[2, 335], [1, 225]]
        self.save()
        with self.assertRaisesRegex(audit.AuditError, "unordered curve keys"):
            audit.load_manifest(self.manifest_path)

    def test_partial_export_failure_zero_exit_and_wrong_boundary_cannot_be_cached_as_success(self):
        context = audit.load_manifest(self.manifest_path)
        mutations = [lambda document: document["packages"][0]["exportFailures"].append({"error": "bad layout"}),
                     lambda document: document["boundedCurveReads"][0].update(endExclusive=99),
                     lambda document: document["packages"][0]["exports"].clear(),
                     lambda document: document["packages"].clear(),
                     lambda document: document["metadata"].update(strictObjectReads=False)]
        for number, mutate in enumerate(mutations):
            with self.subTest(failure=number):
                out = self.root / f"bad-{number}"
                with patch.object(audit.subprocess, "run", side_effect=self.fake_decode(context, mutate=mutate)):
                    report = audit.audit(context, out, log=lambda _: None)
                self.assertFalse(report["complete"])
                self.assertEqual(len(report["decodeFailures"]), 1)
                self.assertEqual(report["champions"][0]["curveStatus"], "unavailable")
                self.assertFalse((out / "batch-0001.record.json").exists())

    def test_missing_imports_and_curve_mismatches_are_preserved(self):
        context = audit.load_manifest(self.manifest_path)
        def mutate(document):
            document["diagnostics"].append('Missing "package" import 0x1')
            export = document["packages"][0]["exports"][0]
            export["warnings"].append('Missing "package" import 0x1')
            export["data"]["Rows"]["DamageAP"]["Keys"][1]["Value"] = 336
        with patch.object(audit.subprocess, "run", side_effect=self.fake_decode(context, mutate=mutate)):
            report = audit.audit(context, self.out, log=lambda _: None)
        self.assertTrue(report["complete"])
        self.assertEqual(report["curveMismatchChampions"], 1)
        self.assertEqual(report["diagnostics"], ['Missing "package" import 0x1'])
        self.assertEqual(report["champions"][0]["warnings"], report["diagnostics"])
        mismatch = report["champions"][0]["curveMismatches"][0]
        self.assertEqual(mismatch["expected"], [[1, 225], [2, 335]])
        self.assertEqual(mismatch["actual"], [[1, 225], [2, 336]])

    def test_redundant_keys_and_unmodeled_four_star_values_are_structural_differences(self):
        self.manifest["champions"][0]["starLevels"] = [1, 2, 3]
        self.save()
        context = audit.load_manifest(self.manifest_path)
        def mutate(document):
            row = document["packages"][0]["exports"][0]["data"]["Rows"]["DamageAP"]
            row["Keys"] += [{"Time": 3, "Value": 335}, {"Time": 4, "Value": 999}]
        with patch.object(audit.subprocess, "run", side_effect=self.fake_decode(context, mutate=mutate)):
            report = audit.audit(context, self.out, log=lambda _: None)
        champion = report["champions"][0]
        self.assertEqual(champion["curveStatus"], "matches")
        self.assertEqual(report["curveMismatchChampions"], 0)
        self.assertEqual(champion["curveStarValuesCompared"], 3)
        self.assertTrue(champion["curveStructureDifferences"][0]["valuesAgreeAtRequestedStars"])
        self.manifest["champions"][0]["starLevels"] = [1, 2, 3, 4]
        self.save()
        context = audit.load_manifest(self.manifest_path)
        with patch.object(audit.subprocess, "run", side_effect=self.fake_decode(context, mutate=mutate)):
            four_star = audit.audit(context, self.root / "four-star", log=lambda _: None)
        self.assertEqual(four_star["champions"][0]["curveMismatches"][0]["valueDifferences"],
                         [{"star": 4, "expected": 335, "actual": 999}])

    def test_unverified_interpolation_is_unknown_and_native_float_precision_is_tolerated(self):
        context = audit.load_manifest(self.manifest_path)
        def missing_mode(document):
            document["packages"][0]["exports"][0]["data"]["Rows"]["DamageAP"].pop("InterpMode")
        with patch.object(audit.subprocess, "run", side_effect=self.fake_decode(context, mutate=missing_mode)):
            report = audit.audit(context, self.out, log=lambda _: None)
        self.assertEqual(report["champions"][0]["curveStatus"], "unknown")
        self.assertEqual(report["curveUnknownChampions"], 1)
        self.assertEqual(report["curveMismatchChampions"], 0)
        def float_precision(document):
            document["packages"][0]["exports"][0]["data"]["Rows"]["DamageAP"]["Keys"][1]["Value"] += 1e-6
        with patch.object(audit.subprocess, "run", side_effect=self.fake_decode(context, mutate=float_precision)):
            near = audit.audit(context, self.root / "float-precision", log=lambda _: None)
        self.assertEqual(near["curveMismatchChampions"], 0)
        self.assertTrue(near["champions"][0]["curveStructureDifferences"])

    def test_linear_source_values_between_sparse_keys_are_compared(self):
        self.manifest["champions"][0]["starLevels"] = [1, 2, 3]
        self.manifest["champions"][0]["expectedCurvesByTable"]["CT_18_Soraka"]["DamageAP"] = [
            [1, 10], [2, 20], [3, 30]]
        self.save()
        context = audit.load_manifest(self.manifest_path)
        def linear(document):
            row = document["packages"][0]["exports"][0]["data"]["Rows"]["DamageAP"]
            row["InterpMode"] = "ERichCurveInterpMode::RCIM_Linear"
            row["Keys"] = [{"Time": 1, "Value": 10}, {"Time": 3, "Value": 30}]
        with patch.object(audit.subprocess, "run", side_effect=self.fake_decode(context, mutate=linear)):
            report = audit.audit(context, self.out, log=lambda _: None)
        champion = report["champions"][0]
        self.assertEqual(champion["curveStatus"], "matches")
        self.assertEqual(champion["curveStarValuesCompared"], 3)
        self.assertTrue(champion["curveStructureDifferences"][0]["valuesAgreeAtRequestedStars"])

    def test_identical_keys_can_differ_when_model_holds_and_source_interpolates(self):
        self.manifest["champions"][0]["starLevels"] = [1, 2, 3]
        self.manifest["champions"][0]["expectedCurvesByTable"]["CT_18_Soraka"]["DamageAP"] = [
            [1, 10], [3, 30]]
        self.save()
        context = audit.load_manifest(self.manifest_path)
        def linear(document):
            row = document["packages"][0]["exports"][0]["data"]["Rows"]["DamageAP"]
            row["InterpMode"] = "ERichCurveInterpMode::RCIM_Linear"
            row["Keys"] = [{"Time": 1, "Value": 10}, {"Time": 3, "Value": 30}]
        with patch.object(audit.subprocess, "run", side_effect=self.fake_decode(context, mutate=linear)):
            report = audit.audit(context, self.out, log=lambda _: None)
        champion = report["champions"][0]
        self.assertEqual(champion["curveMismatches"][0]["valueDifferences"],
                         [{"star": 2, "expected": 10, "actual": 20}])
        self.assertEqual(champion["curveStructureDifferences"], [])

    def test_linear_extrapolation_and_cubic_tangents_remain_unknown(self):
        keys = [[1, 10], [3, 30]]
        row = {"InterpMode": "ERichCurveInterpMode::RCIM_Linear",
               "PreInfinityExtrap": "ERichCurveExtrapolation::RCCE_Constant",
               "PostInfinityExtrap": "ERichCurveExtrapolation::RCCE_Constant"}
        self.assertEqual(audit.source_value(row, keys, 0), (10, None))
        self.assertEqual(audit.source_value(row, keys, 4), (30, None))
        row["PostInfinityExtrap"] = "ERichCurveExtrapolation::RCCE_Linear"
        self.assertIsNone(audit.source_value(row, keys, 4)[0])
        row["InterpMode"] = "ERichCurveInterpMode::RCIM_Cubic"
        self.assertIsNone(audit.source_value(row, keys, 2)[0])

    def sparse_plan(self):
        record = self.artifact("paks/pakchunk1-WindowsClient.ucas", b"firstsecondthird")
        path = self.root / record["path"]
        record.pop("sha256")
        record["ranges"] = [{"offset": 0, "bytes": path.stat().st_size,
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
        self.manifest["containers"].append(record)
        package = self.manifest["packages"][0]
        plan = {"utoc": str(self.paks / "pakchunk1-WindowsClient.utoc"), "ranges": [[0, 16]],
                "chunks": [{"type": "ExportBundleData", "path": "../../../" + package["path"],
                            "chunkId": int(package["packageId"]).to_bytes(8, "little").hex() + "00000001",
                            "packageId": package["packageId"], "uncompressedSize": package["size"]}]}
        self.manifest["rangePlans"] = [self.artifact("ranges.json", json.dumps(plan).encode())]
        return record, plan

    def test_sparse_ranges_must_cover_selected_chunks_and_all_requested_bytes(self):
        record, plan = self.sparse_plan()
        self.save()
        self.assertEqual(len(audit.load_manifest(self.manifest_path)["pins"]["rangePlans"]), 1)
        record["ranges"] = [{"offset": 0, "bytes": 5, "sha256": hashlib.sha256(b"first").hexdigest()}]
        self.save()
        with self.assertRaisesRegex(audit.AuditError, "unhashed requested container range"):
            audit.load_manifest(self.manifest_path)
        record["ranges"] = [{"offset": 0, "bytes": 16, "sha256": hashlib.sha256(b"firstsecondthird").hexdigest()}]
        plan["chunks"][0]["chunkId"] = "0" * 24
        self.manifest["rangePlans"] = [self.artifact("ranges.json", json.dumps(plan).encode())]
        self.save()
        with self.assertRaisesRegex(audit.AuditError, "package missing or mismatched"):
            audit.load_manifest(self.manifest_path)


if __name__ == "__main__":
    unittest.main()
