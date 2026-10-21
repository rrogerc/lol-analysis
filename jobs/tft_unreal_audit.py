#!/usr/bin/env python3
"""Decode a hash-pinned, already downloaded Unreal roster without changing the model.

Usage: python3 jobs/tft_unreal_audit.py --manifest audit-manifest.json [--out CACHE]

The manifest contains patch/build/manifestId/engineVersion/paks, hash records for
executable/mapping/reflectionProvenance/decoder, decoder.files for dependencies,
containers (full hashes or explicitly hashed byte ranges), rangePlans hash
records for sparse containers, available packages
({path, size, packageId, optional chunkId/container}), expectedRoster, and
champions ({api, packages, optional expectedCurvesByTable}). Hash-record paths
are relative to the manifest. expectedCurvesByTable maps table -> row -> [[time,
value], ...]; champion.starLevels defaults to [1, 2, 3]. Numeric comparisons use
the source's constant or linear interpolation at those stars, while exact key
differences are separate.
Acquisition and schema generation remain separate steps: this job
does not execute the game, infer missing defaults, edit models, or publish.

Every source input is checked before cache reuse. Package exports, export counts,
strict mode, and all reported serialization boundaries must pass independently.
Missing imports remain visible in the report. Neither a successful decode nor a
matching coefficient proves runtime targeting, timing, or mana behavior.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BOUND_TYPES = [
    "ChronoDef", "ChronoGroup", "ChronoTrack", "EventChronoModule",
    "TFTChronoModuleAnimation", "TFTChronoModuleProjectile", "TFTNiagaraChronoModule",
    "RGIChronoAudioModule", "TFTChronoModuleMaterialParam", "TFTChronoPreviewActorProvider",
    "TFTFixedTimeMovement", "TFTGameplayEffect", "TFTGameplayAbility",
    "TFTChronoEffectComponent", "TFTAbilitiesEffectComponent", "TFTManaEffectComponent",
    "TFTDamageEffectComponent", "TFTHealEffectComponent",
    "TFTRestrictToPhaseGameplayEffectComponent", "TargetTagsGameplayEffectComponent",
    "AssetTagsGameplayEffectComponent",
]
RUNTIME_UNKNOWNS = [
    "Unserialized native constructor defaults require separate native evidence.",
    "Timeline markers require runtime clock, play-rate, and consumer verification.",
    "Targeting, projectile travel, mana acceptance, and team interactions need behavioral review.",
]
CURVE_REL_TOL = 2 ** -23  # One native float32 relative epsilon.
CURVE_ABS_TOL = 1e-12


class AuditError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise AuditError(message)


def read_json(path):
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise AuditError(f"non-finite JSON value: {value}")

    try:
        return json.loads(Path(path).read_text(), object_pairs_hook=pairs, parse_constant=constant)
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"cannot read {path}: {error}") from error


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def resolve(base, path):
    require(isinstance(path, str) and bool(path), "missing artifact path")
    return (base / path).resolve()


def positive_int(value, name, *, zero=False):
    require(type(value) is int and value >= (0 if zero else 1), f"invalid {name}")
    return value


def verify_file(record, base):
    require(isinstance(record, dict), "artifact must be a hash record")
    path = resolve(base, record.get("path"))
    size = positive_int(record.get("bytes"), f"file size: {path}")
    require(path.is_file() and path.stat().st_size == size, f"file size mismatch: {path}")
    regions = record.get("ranges")
    if regions is None:
        regions = [{"offset": 0, "bytes": size, "sha256": record.get("sha256")}]
    require(isinstance(regions, list) and bool(regions), f"empty hash coverage: {path}")
    checked = []
    with path.open("rb") as stream:
        for region in regions:
            require(isinstance(region, dict), f"invalid hash range: {path}")
            offset = positive_int(region.get("offset"), "range offset", zero=True)
            length = positive_int(region.get("bytes"), "range length")
            expected = region.get("sha256")
            require(isinstance(expected, str) and re.fullmatch(r"[a-f0-9]{64}", expected),
                    f"invalid SHA256: {path}")
            require(offset + length <= size, f"hash range outside file: {path}")
            stream.seek(offset)
            hasher = hashlib.sha256()
            remaining = length
            while remaining:
                data = stream.read(min(remaining, 1 << 20))
                require(bool(data), f"short hash read: {path}")
                hasher.update(data)
                remaining -= len(data)
            require(hasher.hexdigest() == expected, f"SHA256 mismatch: {path} at {offset}")
            checked.append({"offset": offset, "bytes": length, "sha256": expected})
    return {"path": str(path), "bytes": size, "ranges": checked}


def package_path(value):
    require(isinstance(value, str) and value.startswith("TFT/") and
            not any(char.isspace() for char in value) and "\\" not in value and
            ".." not in value.split("/") and value.endswith((".uasset", ".umap")) and
            "/ui/" not in value.lower(), f"invalid package path: {value!r}")
    return value


def curve_keys(value, label):
    require(isinstance(value, list), f"invalid curve keys: {label}")
    for key in value:
        require(isinstance(key, list) and len(key) == 2 and
                all(type(number) in (int, float) and math.isfinite(number) for number in key),
                f"invalid curve key: {label}")
    require(all(left[0] < right[0] for left, right in zip(value, value[1:])),
            f"unordered curve keys: {label}")
    return value


def held_value(keys, star):
    """The model's documented constant-breakpoint evaluation, including edges."""
    if not keys:
        return None
    value = keys[0][1]
    for position, candidate in keys:
        if position > star:
            break
        value = candidate
    return value


def source_value(row, keys, star):
    if not keys:
        return None, "no serialized source keys"
    mode = row.get("InterpMode")
    supported = isinstance(mode, str) and mode.endswith(("::RCIM_Constant", "::RCIM_Linear"))
    if mode is None:
        modes = {key.get("InterpMode") for key in row["Keys"]}
        if len(modes) == 1:
            mode = next(iter(modes))
            supported = isinstance(mode, str) and mode.endswith(("::RCIM_Constant", "::RCIM_Linear"))
    if not supported:
        return None, "source interpolation unverified"
    edge = "PreInfinityExtrap" if star < keys[0][0] else "PostInfinityExtrap" if star > keys[-1][0] else None
    if edge:
        extrap = row.get(edge)
        if not isinstance(extrap, str) or not extrap.endswith("::RCCE_Constant"):
            return None, f"source {edge} constant extrapolation unverified"
    if mode.endswith("::RCIM_Constant"):
        return held_value(keys, star), None
    if star <= keys[0][0]:
        return keys[0][1], None
    for left, right in zip(keys, keys[1:]):
        if star <= right[0]:
            fraction = (star - left[0]) / (right[0] - left[0])
            return left[1] + fraction * (right[1] - left[1]), None
    return keys[-1][1], None


def verify_range_plans(manifest, base, pins, packages):
    """Tie sparse-file hash coverage to the actual selected UTOC package ranges."""
    sparse = {record["path"] for record in manifest["containers"] if "ranges" in record}
    sparse = {str(resolve(base, path)) for path in sparse}
    plans = manifest.get("rangePlans", [])
    require(isinstance(plans, list) and (bool(plans) or not sparse), "missing sparse container rangePlans")
    pins["rangePlans"] = [verify_file(record, base) for record in plans]
    available = {record["path"]: record for record in pins["containers"]}
    chunks, covered_files = {}, set()
    for record in pins["rangePlans"]:
        plan = read_json(record["path"])
        require(isinstance(plan, dict), "invalid range plan")
        utoc = resolve(Path(record["path"]).parent, plan.get("utoc"))
        container = str(utoc.with_suffix(".ucas"))
        require(str(utoc) in available and container in available, "range plan container mismatch")
        spans = sorted((region["offset"], region["offset"] + region["bytes"])
                       for region in available[container]["ranges"])
        merged = []
        for begin, end in spans:
            if merged and begin <= merged[-1][1]:
                merged[-1][1] = max(end, merged[-1][1])
            else:
                merged.append([begin, end])
        requested = plan.get("ranges")
        require(isinstance(requested, list) and bool(requested), "empty source range plan")
        for span in requested:
            require(isinstance(span, list) and len(span) == 2 and
                    all(type(value) is int for value in span) and 0 <= span[0] < span[1] and
                    any(begin <= span[0] and span[1] <= end for begin, end in merged),
                    f"unhashed requested container range: {container}")
        covered_files.add(container)
        source_chunks = plan.get("chunks")
        require(isinstance(source_chunks, list), "missing range-plan chunks")
        for chunk in source_chunks:
            require(isinstance(chunk, dict), "invalid range-plan chunk")
            if chunk.get("type") == "ExportBundleData":
                path = chunk.get("path")
                require(isinstance(path, str), "missing range-plan package path")
                path = path.removeprefix("../../../")
                if path in chunks:
                    require(chunks[path] == chunk, f"conflicting package range plans: {path}")
                chunks[path] = chunk
    require(sparse <= covered_files, "sparse container lacks verified range plan")
    if sparse:
        for package in packages.values():
            chunk = chunks.get(package["path"], {})
            require(chunk.get("chunkId") == package["chunkId"] and
                    str(chunk.get("packageId")) == package["packageId"] and
                    chunk.get("uncompressedSize") == package["size"],
                    f"package missing or mismatched in source range plans: {package['path']}")


def load_manifest(path):
    path = Path(path).resolve()
    manifest, base = read_json(path), path.parent
    require(isinstance(manifest, dict), "manifest must be an object")
    for key in ("patch", "manifestId", "engineVersion"):
        require(isinstance(manifest.get(key), str) and bool(manifest[key]), f"missing {key}")
    build = manifest.get("build")
    require((type(build) is int and build > 0) or (isinstance(build, str) and bool(build)), "missing build")
    require(re.fullmatch(r"[0-9A-Fa-f]{16}", manifest["manifestId"]), "invalid manifestId")
    require(re.fullmatch(r"UE5_\d+", manifest["engineVersion"]), "invalid engineVersion")
    for key in ("executable", "mapping", "reflectionProvenance", "decoder"):
        require(isinstance(manifest.get(key), dict) and "ranges" not in manifest[key],
                f"{key} requires a complete file hash")
    pins = {key: verify_file(manifest.get(key), base)
            for key in ("executable", "mapping", "reflectionProvenance", "decoder")}
    proof = read_json(pins["reflectionProvenance"]["path"])
    require(isinstance(proof, dict), "invalid reflection provenance")
    for key in ("patch", "manifestId"):
        require(proof.get(key) == manifest[key], f"reflection source {key} mismatch")
    proof_build = proof.get("build")
    build_match = proof_build == build
    if type(proof_build) is int and isinstance(build, str):
        match = re.fullmatch(r"rls-" + re.escape(manifest["patch"]) + r"\.\d+\.(\d+)\.Set\d+\.Shipping\.[\w-]+", build)
        build_match = bool(match) and int(match[1]) == proof_build
    require(build_match, "reflection source build mismatch")
    for key, proof_key in (("executable", "exe"), ("mapping", "mapping")):
        require(verify_file(proof.get(proof_key), Path(pins["reflectionProvenance"]["path"]).parent)
                == pins[key], f"reflection {key} mismatch")
    dependencies = manifest["decoder"].get("files", [])
    require(isinstance(dependencies, list) and all(isinstance(record, dict) and "ranges" not in record
                                                 for record in dependencies), "invalid decoder dependencies")
    pins["decoderFiles"] = [verify_file(record, base) for record in dependencies]
    containers = manifest.get("containers")
    require(isinstance(containers, list) and bool(containers), "missing containers")
    pins["containers"] = [verify_file(record, base) for record in containers]
    paks = resolve(base, manifest.get("paks"))
    require(paks.is_dir(), "missing paks directory")
    actual_containers = {str(file.resolve()) for file in paks.rglob("*")
                         if file.suffix in (".utoc", ".ucas")}
    require(actual_containers == {record["path"] for record in pins["containers"]},
            "unverified or missing container in paks directory")
    packages, by_path = manifest.get("packages"), {}
    require(isinstance(packages, list) and bool(packages), "missing packages")
    for package in packages:
        require(isinstance(package, dict), "invalid package record")
        name = package_path(package.get("path"))
        require(name.lower() not in by_path, f"duplicate package: {name}")
        size = positive_int(package.get("size"), f"package size: {name}")
        ident = str(package.get("packageId", ""))
        require(re.fullmatch(r"\d+", ident) and 0 <= int(ident) < 1 << 64, f"invalid packageId: {name}")
        chunk = int(ident).to_bytes(8, "little").hex() + "00000001"
        provided_chunk = package.get("chunkId", chunk)
        require(isinstance(provided_chunk, str) and provided_chunk.lower() == chunk,
                f"chunkId mismatch: {name}")
        container = package.get("container", "pakchunk1-WindowsClient")
        require(isinstance(container, str) and re.fullmatch(r"[\w.-]+", container), "invalid container name")
        by_path[name.lower()] = {"path": name, "size": size, "packageId": ident,
                                "chunkId": chunk, "container": container}
    verify_range_plans(manifest, base, pins, by_path)
    roster = manifest.get("expectedRoster")
    champions = manifest.get("champions")
    require(isinstance(roster, list) and bool(roster) and
            all(isinstance(api, str) and bool(api) for api in roster) and len(set(roster)) == len(roster),
            "missing or duplicate expectedRoster")
    require(isinstance(champions, list) and bool(champions), "missing champions")
    seen = set()
    for champion in champions:
        require(isinstance(champion, dict), "invalid champion record")
        api = champion.get("api")
        require(api in roster and api not in seen, f"unexpected or duplicate champion: {api}")
        seen.add(api)
        selected = champion.get("packages")
        require(isinstance(selected, list) and bool(selected) and
                all(isinstance(name, str) for name in selected) and len(set(selected)) == len(selected),
                f"missing or duplicate champion packages: {api}")
        require(all(package_path(name).lower() in by_path for name in selected),
                f"missing champion package: {api}")
        stars = champion.get("starLevels", [1, 2, 3])
        require(isinstance(stars, list) and bool(stars) and
                all(type(star) is int and 1 <= star <= 4 for star in stars) and stars == sorted(set(stars)),
                f"invalid starLevels: {api}")
        champion["starLevels"] = stars
        expected = champion.get("expectedCurvesByTable", {})
        require(isinstance(expected, dict), f"invalid expected curves: {api}")
        if "expectedCurves" in champion:
            table = champion.get("curveTable")
            require(isinstance(table, str) and bool(table) and table not in expected,
                    f"missing or duplicate curveTable: {api}")
            expected = {**expected, table: champion["expectedCurves"]}
        for table, rows in expected.items():
            require(isinstance(table, str) and bool(table) and isinstance(rows, dict) and bool(rows),
                    f"invalid expected table: {api}")
            for row, keys in rows.items():
                require(isinstance(row, str) and bool(row), f"invalid curve row: {api}")
                curve_keys(keys, f"{api}/{table}/{row}")
        champion["expectedCurvesByTable"] = expected
    require(seen == set(roster), f"missing roster champions: {sorted(set(roster) - seen)}")
    bounded = manifest.get("boundedObjectTypes", BOUND_TYPES)
    require(isinstance(bounded, list) and bool(bounded) and
            all(isinstance(name, str) and re.fullmatch(r"\w+", name) for name in bounded) and
            len(set(bounded)) == len(bounded),
            "invalid boundedObjectTypes")
    return {"manifest": manifest, "pins": pins, "paks": str(paks), "bounded": bounded,
            "packages": sorted(by_path.values(), key=lambda package: package["path"]),
            "fingerprint": digest({"manifest": manifest, "pins": pins, "auditVersion": 1})}


def validate_decode(document, selected, context):
    require(isinstance(document, dict), "decoded output must be an object")
    metadata = document.get("metadata", {})
    require(isinstance(metadata, dict), "invalid decoder metadata")
    require(metadata.get("strictObjectReads") is True, "decoder did not use strict object reads")
    require(metadata.get("directory") == context["paks"] and
            metadata.get("mapping") == context["pins"]["mapping"]["path"] and
            metadata.get("game") == "GAME_" + context["manifest"]["engineVersion"],
            "decoder source metadata mismatch")
    require(metadata.get("fileCount") == len(context["packages"]), "decoder allowlist is incomplete")
    require(metadata.get("boundedObjectTypes") == context["bounded"], "decoder boundary types mismatch")
    require(document.get("failures") == [], "decoder package failures")
    packages = document.get("packages")
    require(isinstance(packages, list), "missing decoded packages")
    names = [package.get("path") for package in packages if isinstance(package, dict)]
    require(len(names) == len(packages) and all(isinstance(name, str) for name in names) and
            len(set(names)) == len(names) and set(names) == set(selected),
            "missing or unexpected decoded package")
    for package in packages:
        require(package.get("exportFailures") == [], f"decoder export failures: {package['path']}")
        count = positive_int(package.get("totalExports"), "export count")
        exports = package.get("exports")
        require(isinstance(exports, list) and len(exports) == count and
                all(isinstance(export, dict) and type(export.get("exportIndex")) is int and
                    isinstance(export.get("data"), dict) and isinstance(export.get("warnings"), list)
                    for export in exports) and {export["exportIndex"] for export in exports} == set(range(count)),
                f"incomplete decoded exports: {package['path']}")
    for key in ("boundedCurveReads", "boundedObjectReads"):
        require(isinstance(document.get(key), list), f"missing {key}")
        for boundary in document[key]:
            require(isinstance(boundary, dict) and boundary.get("exact") is True and
                    type(boundary.get("endExclusive")) is int and
                    boundary.get("endExclusive") == boundary.get("expectedEndExclusive"),
                    f"serialization boundary mismatch: {key}")
    require(isinstance(document.get("diagnostics"), list), "missing decoder diagnostics")
    return document


def cached_batch(output, record, selected, context):
    try:
        saved = read_json(record)
        require(saved["inputSha256"] == context["fingerprint"] and saved["selection"] == selected,
                "cached selection or inputs changed")
        verify_file(saved["output"], record.parent)
        require(resolve(record.parent, saved["output"]["path"]) == output, "cached output path mismatch")
        return validate_decode(read_json(output), selected, context)
    except (AuditError, KeyError, TypeError):
        return None


def summarize(context, documents, failures):
    packages = {package["path"]: package for document in documents for package in document["packages"]}
    champions = []
    for champion in context["manifest"]["champions"]:
        selected = [packages[path] for path in champion["packages"] if path in packages]
        missing = sorted(set(champion["packages"]) - packages.keys())
        exports = [export for package in selected for export in package["exports"]]
        tables, native_rows = {}, {}
        for export in exports:
            data = export["data"]
            if data.get("Type") == "CurveTable":
                require(data.get("Name") not in tables, f"ambiguous curve table: {champion['api']}")
                tables[data["Name"]] = {row: curve_keys([[key["Time"], key["Value"]] for key in curve["Keys"]],
                                                      f"{data['Name']}/{row}")
                                        for row, curve in data["Rows"].items()}
                native_rows[data["Name"]] = data["Rows"]
        expected = champion["expectedCurvesByTable"]
        mismatches, structures, unknowns, rows_compared, keys_compared, values_compared = [], [], [], 0, 0, 0
        for table, rows in expected.items():
            for row, keys in rows.items():
                actual = tables.get(table, {}).get(row)
                rows_compared += 1
                keys_compared += len(keys)
                differences = []
                if actual is None:
                    mismatches.append({"table": table, "row": row, "expected": keys, "actual": actual,
                                       "reason": "missing source row"})
                else:
                    for star in champion["starLevels"]:
                        source, unknown = source_value(native_rows[table][row], actual, star)
                        model = held_value(keys, star)
                        if unknown or model is None:
                            unknowns.append({"table": table, "row": row, "star": star,
                                             "reason": unknown or "no model keys"})
                            continue
                        values_compared += 1
                        if not math.isclose(source, model, rel_tol=CURVE_REL_TOL, abs_tol=CURVE_ABS_TOL):
                            differences.append({"star": star, "expected": model, "actual": source})
                    if differences:
                        mismatches.append({"table": table, "row": row, "expected": keys, "actual": actual,
                                           "reason": "values differ at modeled stars", "valueDifferences": differences})
                if actual != keys:
                    structures.append({"table": table, "row": row, "expected": keys, "actual": actual,
                                       "valuesAgreeAtRequestedStars": actual is not None and not differences and
                                       not any(unknown["table"] == table and unknown["row"] == row for unknown in unknowns)})
        warnings = [warning for export in exports for warning in export["warnings"]]
        champions.append({"api": champion["api"], "packageCount": len(champion["packages"]),
                          "decodedPackageCount": len(selected), "missingPackages": missing,
                          "decodedExports": len(exports), "exportTypes": dict(Counter(export["data"].get("Type", "unknown")
                                                                                    for export in exports)),
                          "curveStatus": "unavailable" if missing else "not-compared" if not expected
                          else "mismatch" if mismatches else "unknown" if unknowns else "matches",
                          "starLevels": champion["starLevels"],
                          "curveRowsCompared": rows_compared, "curveKeysCompared": keys_compared,
                          "curveStarValuesCompared": values_compared, "curveUnknowns": unknowns,
                          "curveStructureDifferences": structures,
                          "curveMismatches": mismatches, "sourceCurves": tables,
                          "rowsWithoutSerializedKeys": {table: sorted(row for row, keys in rows.items() if not keys)
                                                       for table, rows in tables.items()
                                                       if any(not keys for keys in rows.values())},
                          "uncomparedRows": {table: sorted(set(rows) - set(expected.get(table, {})))
                                             for table, rows in tables.items()
                                             if set(rows) - set(expected.get(table, {}))},
                          "warnings": warnings, "runtimeVerified": False})
    return {"auditVersion": 1, "patch": context["manifest"]["patch"],
            "build": context["manifest"]["build"], "manifestId": context["manifest"]["manifestId"],
            "inputSha256": context["fingerprint"], "verifiedInputs": context["pins"],
            "expectedChampions": len(context["manifest"]["expectedRoster"]), "champions": champions,
            "decodedPackages": len(packages), "expectedPackages": len(context["packages"]),
            "decodeFailures": failures, "complete": not failures and len(packages) == len(context["packages"]),
            "curveMismatchChampions": sum(bool(champion["curveMismatches"]) for champion in champions),
            "curveUnknownChampions": sum(bool(champion["curveUnknowns"]) for champion in champions),
            "curveStructureDifferenceChampions": sum(bool(champion["curveStructureDifferences"]) for champion in champions),
            "curveValueTolerance": {"relative": CURVE_REL_TOL, "absolute": CURVE_ABS_TOL},
            "boundaryReads": {key: sum(len(document[key]) for document in documents)
                              for key in ("boundedCurveReads", "boundedObjectReads")},
            "diagnostics": [message for document in documents for message in document["diagnostics"]],
            "runtimeVerified": False, "runtimeUnknowns": RUNTIME_UNKNOWNS,
            "futureManualWork": ["Acquire and verify the exact new build and its required byte ranges.",
                                 "Regenerate reflection mappings from that build's executable and validate layouts.",
                                 "Review new engine/container formats, unresolved imports, and changed spell consumers.",
                                 "Validate runtime behavior before changing or reranking the production model."]}


def audit(context, out, *, batch_size=32, timeout=300, log=lambda message: print(message, file=sys.stderr)):
    require(type(batch_size) is int and 1 <= batch_size <= 32, "batch size must be 1 through 32")
    out = Path(out).resolve()
    require(not out.is_relative_to(ROOT) or out.is_relative_to(ROOT / ".cache"),
            "audit output inside the checkout must stay in .cache")
    out.mkdir(parents=True, exist_ok=True)
    allowlist = out / "available-assets.txt"
    allowlist.write_text("".join(f"{package['container']} {package['chunkId']} {package['packageId']} "
                                 f"ExportBundleData {package['size']} ../../../{package['path']}\n"
                                 for package in context["packages"]))
    documents, failures, reused = [], [], 0
    for start in range(0, len(context["packages"]), batch_size):
        selected = [package["path"] for package in context["packages"][start:start + batch_size]]
        output = out / f"batch-{start // batch_size + 1:04d}.json"
        record = output.with_suffix(".record.json")
        document = cached_batch(output, record, selected, context)
        if document is not None:
            reused += 1
            log(f"Reused {output.name}: {len(selected)} verified packages")
            documents.append(document)
            continue
        temporary = output.with_suffix(".decode.json")
        env = {**os.environ, "TFT_ASSET_ALLOWLIST": str(allowlist), "TFT_STRICT_OBJECT_READS": "1",
               "TFT_BOUNDED_OBJECT_TYPES": ",".join(context["bounded"])}
        env.pop("TFT_NATIVE_CURVES_ONLY", None)
        pattern = "^(?:" + "|".join(re.escape(path) for path in selected) + ")$"
        command = [context["pins"]["decoder"]["path"], "export-partial", context["paks"],
                   context["manifest"]["engineVersion"], str(temporary), pattern,
                   context["pins"]["mapping"]["path"]]
        log(f"Decoding {output.name}: {len(selected)} packages")
        try:
            with output.with_suffix(".log").open("w") as stream:
                result = subprocess.run(command, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                        timeout=timeout, check=False)
            require(result.returncode == 0, f"decoder exit {result.returncode}; see {output.with_suffix('.log')}")
            document = validate_decode(read_json(temporary), selected, context)
            temporary.replace(output)
            with output.open("rb") as stream:
                output_sha = hashlib.file_digest(stream, "sha256").hexdigest()
            write_json(record, {"inputSha256": context["fingerprint"], "selection": selected,
                                "output": {"path": output.name, "bytes": output.stat().st_size,
                                           "sha256": output_sha}})
            documents.append(document)
        except (AuditError, OSError, subprocess.TimeoutExpired) as error:
            failures.append({"packages": selected, "error": str(error), "output": str(temporary),
                             "log": str(output.with_suffix(".log"))})
            log(f"Failed {output.name}: {error}")
    report = {**summarize(context, documents, failures), "reusedBatches": reused}
    write_json(out / "review.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    try:
        require(math.isfinite(args.timeout) and args.timeout > 0, "timeout must be positive and finite")
        context = load_manifest(args.manifest)
        out = args.out or ROOT / ".cache/tft/unreal-audit" / context["fingerprint"]
        report = audit(context, out, batch_size=args.batch_size, timeout=args.timeout)
        print(json.dumps({"review": str(out / "review.json"), "complete": report["complete"],
                          "decodedPackages": report["decodedPackages"],
                          "curveMismatchChampions": report["curveMismatchChampions"]}))
        return 4 if not report["complete"] else 3 if report["curveMismatchChampions"] or report["curveUnknownChampions"] else 0
    except (AuditError, OSError, KeyError, TypeError) as error:
        print(f"Unreal audit: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
