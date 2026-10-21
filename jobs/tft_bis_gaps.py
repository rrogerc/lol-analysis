#!/usr/bin/env python3
"""Add exact runner-up comparisons to the current saved TFT publication.

Normal refreshes save both leading builds. This upgrades an older publication
without rebuilding its rankings or fetching a different patch. Missing exact
runner-ups are replayed only after its optimal fight reproduces the saved result.
"""

from copy import deepcopy
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tft
import tft_site


def runner_up(snap, payload):
    """Reuse exact saved scores, or replay the two recorded builds."""
    second = tft.leaderboard_runner_up(payload)
    if second is not None or len(payload.get("rows", [])) < 2:
        return second, False
    unit = snap.units[payload["unitApi"]]
    scenario = payload["scenario"]
    contexts, _ = tft.unit_trait_contexts(snap, unit, tft.load_trait_effects(snap.set_no))

    def fight(apis):
        return tft.simulate(snap, unit, scenario["star"], apis, scenario["geometry"],
                            contexts[scenario["traits"]], scenario["dummy"], scenario["duration"])

    _, reproduced = fight(payload["best"]["itemApis"])
    for field, expected in payload["best"]["performance"].items():
        actual = reproduced[field]
        matches = (math.isclose(expected, actual, rel_tol=1e-12, abs_tol=1e-9)
                   if isinstance(expected, (int, float)) and not isinstance(expected, bool)
                   and isinstance(actual, (int, float)) and not isinstance(actual, bool)
                   else expected == actual)
        if not matches:
            raise ValueError(f"Saved optimal fight no longer reproduces: {payload['unit']}/{scenario['key']} ({field})")
    apis = [snap.item(name)["api"] for name in payload["rows"][1]["items"]]
    sheet, result = fight(apis)
    payload["runnerUp"] = {"items": list(payload["rows"][1]["items"]), "itemApis": apis,
                           **tft.equipped_metadata(unit, sheet),
                           "performance": {field: result[field] for field in (
                               "killTime", "total", "dps", "aliveTime", "survivalCapped",
                               "stressAliveTime", "stressCapped")}}
    return tft.leaderboard_runner_up(payload), True


def prepare(bundle, snap, *, log=print):
    """Build an immutable enriched generation, retaining all original ranks."""
    revision = tft.json_hash(["tft-bis-gaps-v1", tft.SOURCE_HASH,
                             hashlib.sha256(Path(__file__).read_bytes()).hexdigest()])
    if bundle.manifest.get("bisGapRevision") == revision:
        return bundle.descriptor
    meta = bundle.json("/api/tft/meta.json")
    if (meta["set"], meta["patch"]) != (snap.set_no, snap.patch):
        raise ValueError("TFT BIS gaps require the published snapshot")
    inventory = deepcopy(bundle.manifest["inventory"])
    inventory.update(compositionContexts=[], compositionArchived=True)
    expected = tft_site._endpoints(inventory)
    baseline = bundle.manifest["baselineRevision"]
    composition = tft_site.ARCHIVED_COMPOSITION_REVISION
    presentation = tft.json_hash([bundle.manifest.get("presentationHash"), revision])
    generation = tft_site._generation(baseline, composition, tft_site.SOURCE_HASH, presentation)
    root = Path(tft_site.CACHE_DIR)
    destination = root / generation
    if destination.exists():
        raw = (destination / "manifest.json").read_bytes()
        return tft_site.load(tft_site._descriptor(json.loads(raw), raw)).descriptor
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=root))
    entries, seconds = {}, {}
    replayed = 0
    try:
        # Keep unchanged representations as hardlinks, including compressed
        # files. Only enriched cells, leaderboards and publication metadata
        # need serialization/compression again.
        for url in sorted(expected):
            old = bundle.entries[url]
            for representation in old.values():
                path = representation["path"]
                tft_site._link(bundle.directory / path, stage / path)
            entries[url] = deepcopy(old)

        def write(url, payload):
            # Replacing a link preserves the immutable source generation.
            path = stage / url.lstrip("/")
            path.unlink()
            (stage / (url.lstrip("/") + ".gz")).unlink()
            tft_site._write(stage, url, payload)
            entries[url] = tft_site._compress(stage, url)

        for index, (slug, key) in enumerate(inventory["baselineCells"], 1):
            url = f"/api/tft/{slug}/{key}.json"
            payload = bundle.json(url)
            if payload.get("unit") != slug or payload.get("scenario", {}).get("key") != key:
                raise ValueError("TFT BIS cell does not match its inventory")
            second, calculated = runner_up(snap, payload)
            seconds[slug, key] = second
            if calculated:
                replayed += 1
                write(url, payload)
            if index % 500 == 0:
                log(f"Read exact BIS comparisons: {index}/{len(inventory['baselineCells'])}")

        for key in inventory["leaderboardSelections"]:
            url = f"/api/tft/leaderboard/{key}.json"
            payload = bundle.json(url)
            if payload.get("revision") != baseline or payload.get("selection", {}).get("key") != key:
                raise ValueError("TFT BIS leaderboard does not match its publication")
            for row in payload["damage"] + payload["tanks"]:
                second = seconds[row["unit"], row["scenario"]]
                if second is not None:
                    finite = row.get("finiteDiagnostic", row)
                    finite["secondBest"] = deepcopy(second)
            write(url, payload)

        for url in ("/api/tft/meta.json", "/api/tft/status.json", "/api/tft/traits.json", "/api/tft/early.json"):
            if url in expected:
                payload = bundle.json(url)
                payload["publicationRevision"] = generation
                write(url, payload)
        manifest = {"schema": tft_site.SCHEMA, "siteGeneration": generation,
                    "baselineRevision": baseline, "compositionRevision": composition,
                    "publisherHash": tft_site.SOURCE_HASH, "presentationHash": presentation,
                    "bisGapRevision": revision, "inventory": inventory, "entries": entries}
        raw = tft_site._json_bytes(manifest)
        (stage / "manifest.json").write_bytes(raw)
        descriptor = tft_site._descriptor(manifest, raw)
        tft_site._load(descriptor, directory=stage)
        try:
            os.rename(stage, destination)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            raw = (destination / "manifest.json").read_bytes()
            descriptor = tft_site._descriptor(json.loads(raw), raw)
        tft_site.load(descriptor)
        log(f"Prepared BIS gaps for {len(seconds)} cells; replayed {replayed} missing runner-ups.")
        return descriptor
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    with open(Path(tft.CACHE_DIR) / "refresh.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker_path = Path(tft.CACHE_DIR) / ".dashboard-ready"
        original = marker_path.read_bytes()
        marker = json.loads(original)
        bundle = tft_site.load(marker["site"])
        meta = bundle.json("/api/tft/meta.json")
        snap = tft.load_snapshot(meta["set"], meta["patch"])
        descriptor = prepare(bundle, snap)
        if marker_path.read_bytes() != original:
            raise RuntimeError("TFT publication changed during BIS gap preparation")
        marker.update(site=descriptor, compositionRevision=descriptor["compositionRevision"])
        if marker != json.loads(original):
            tft.write_json_atomic(str(marker_path), marker)
        print("The saved TFT leaderboard includes exact BIS runner-up comparisons.")


if __name__ == "__main__":
    main()
