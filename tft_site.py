"""Build immutable TFT HTTP responses after the scheduled calculations finish.

Only ``prepare`` consults TFT models or calculation caches. ``load`` and
``Bundle`` use the published manifest and files, so serving an existing
generation never runs simulations, assembles leaderboards, or loads a snapshot.
"""

from dataclasses import dataclass
import errno
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile


CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "tft-site"
SCHEMA = "tft-http-bundle-v1"
SOURCE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_CHUNK = 1024 * 1024
_SLUG = re.compile(r"[a-z0-9]+")
_KEY = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_HASH = re.compile(r"[0-9a-f]{64}")


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _generation(baseline, composition, publisher, presentation=None):
    # The optional fourth input keeps already published v1 manifests readable.
    inputs = [baseline, composition, publisher]
    if presentation is not None:
        inputs.append(presentation)
    return "g-" + hashlib.sha256(_json_bytes(inputs)).hexdigest()


def _presentation_hash(snap):
    """Inputs to saved metadata that do not affect the calculation revisions.

    Fetch/verification timestamps describe a particular check, not changed
    source content. The UI receives current check times through the dynamic
    refresh status; unchanged content keeps its existing bundle and marker.
    Only unresolved audit limitations are exposed by the metadata API, so
    audit bookkeeping (including checkedAt) cannot invalidate a publication.
    """
    meta = {key: value for key, value in snap.meta.items()
            if key not in ("fetchedAt", "verifiedAt", "checkedAt")}
    return hashlib.sha256(_json_bytes([
        meta, (snap.audit or {}).get("unresolved", []), snap.communitydragon,
    ])).hexdigest()


def _endpoints(inventory):
    """Validate the inventory and derive its complete API surface."""
    if not isinstance(inventory, dict) or set(inventory) != {
            "baselineCells", "compositionContexts", "leaderboardSelections"}:
        raise ValueError("invalid TFT site inventory")
    cells = inventory["baselineCells"]
    contexts = inventory["compositionContexts"]
    leaderboards = inventory["leaderboardSelections"]
    if not all(isinstance(values, list) and values for values in (cells, contexts, leaderboards)):
        raise ValueError("TFT site inventory is empty")
    for pair in cells:
        if (not isinstance(pair, list) or len(pair) != 2
                or not isinstance(pair[0], str) or not _SLUG.fullmatch(pair[0])
                or pair[0] in ("compositions", "leaderboard")
                or not isinstance(pair[1], str) or not _KEY.fullmatch(pair[1])
                or pair[1] == "cores"):
            raise ValueError("invalid TFT champion endpoint")
    if len({tuple(pair) for pair in cells}) != len(cells):
        raise ValueError("duplicate TFT champion endpoint")
    for values in (contexts, leaderboards):
        if (any(not isinstance(key, str) or not _KEY.fullmatch(key)
                or key in ("meta", "status") for key in values)
                or len(set(values)) != len(values)):
            raise ValueError("invalid TFT selection endpoint")
    return {
        "/api/tft/meta.json", "/api/tft/status.json",
        "/api/tft/compositions/meta.json", "/api/tft/compositions/status.json",
        *(f"/api/tft/{slug}/{key}.json" for slug, key in cells),
        *(f"/api/tft/{slug}/cores.json" for slug, _ in cells),
        *(f"/api/tft/leaderboard/{key}.json" for key in leaderboards),
        *(f"/api/tft/compositions/{key}.json" for key in contexts),
    }


@dataclass(frozen=True)
class Bundle:
    directory: Path
    manifest: dict
    descriptor: dict

    @property
    def entries(self):
        return self.manifest["entries"]

    def asset(self, url, *, compressed=False):
        entry = self.entries.get(url)
        if entry is None:
            return None
        representation = entry["gzip" if compressed else "identity"]
        return {"path": self.directory / representation["path"],
                "size": representation["size"], "etag": representation["etag"],
                "encoding": "gzip" if compressed else None}

    def json(self, url):
        """Read a prebuilt payload, principally the two startup statuses."""
        asset = self.asset(url)
        if asset is None:
            raise KeyError(url)
        return json.loads(asset["path"].read_bytes())


def load(descriptor):
    """Validate a pinned publication without consulting current TFT inputs.

    The descriptor pins the manifest bytes. Every expected URL and both file
    representations must exist with their recorded lengths. Hashes were
    computed before the immutable generation was published; HTTP can use its
    ETags directly without reading or parsing unrelated response files.
    """
    return _load(descriptor)


def _load(descriptor, *, directory=None):
    if not isinstance(descriptor, dict):
        raise ValueError("invalid TFT site descriptor")
    required = ("siteGeneration", "baselineRevision", "compositionRevision", "manifestHash")
    if any(not isinstance(descriptor.get(key), str) for key in required):
        raise ValueError("incomplete TFT site descriptor")
    if not re.fullmatch(r"g-[0-9a-f]{64}", descriptor["siteGeneration"]):
        raise ValueError("invalid TFT site generation")
    if not _HASH.fullmatch(descriptor["manifestHash"]):
        raise ValueError("invalid TFT site manifest hash")
    directory = directory if directory is not None else Path(CACHE_DIR) / descriptor["siteGeneration"]
    if directory.is_symlink():
        raise ValueError("TFT site generation cannot be a symlink")
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("TFT site manifest cannot be a symlink")
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != descriptor["manifestHash"]:
        raise ValueError("TFT site manifest hash does not match its publication")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA
            or manifest.get("siteGeneration") != descriptor["siteGeneration"]
            or any(manifest.get(key) != descriptor[key]
                   for key in ("baselineRevision", "compositionRevision"))
            or not isinstance(manifest.get("publisherHash"), str)
            or not _HASH.fullmatch(manifest["publisherHash"])):
        raise ValueError("TFT site manifest identity does not match its publication")
    presentation = manifest.get("presentationHash")
    if "presentationHash" in manifest and (
            not isinstance(presentation, str) or not _HASH.fullmatch(presentation)):
        raise ValueError("invalid TFT site presentation hash")
    if _generation(manifest["baselineRevision"], manifest["compositionRevision"],
                   manifest["publisherHash"], presentation) != descriptor["siteGeneration"]:
        raise ValueError("TFT site generation does not match its inputs")
    expected = _endpoints(manifest.get("inventory"))
    entries = manifest.get("entries")
    if not isinstance(entries, dict) or set(entries) != expected:
        raise ValueError("TFT site manifest does not contain every expected endpoint")
    for url, entry in entries.items():
        if not isinstance(entry, dict) or set(entry) != {"identity", "gzip"}:
            raise ValueError(f"TFT site representation is missing: {url}")
        for encoding, suffix in (("identity", ""), ("gzip", ".gz")):
            record = entry[encoding]
            if (not isinstance(record, dict) or set(record) != {"path", "size", "etag"}
                    or record.get("path") != url.lstrip("/") + suffix
                    or type(record.get("size")) is not int or record["size"] <= 0
                    or not isinstance(record.get("etag"), str)
                    or not re.fullmatch(r'"[0-9a-f]{64}"', record["etag"])):
                raise ValueError(f"invalid TFT site representation: {url}")
            path = directory / record["path"]
            if (path.is_symlink() or not path.is_file()
                    or path.resolve().parent != (directory / url.lstrip("/")).resolve().parent
                    or not path.resolve().is_relative_to(directory.resolve())
                    or path.stat().st_size != record["size"]):
                raise ValueError(f"TFT site file is missing or changed: {url} ({encoding})")
    return Bundle(directory, manifest, dict(descriptor))


def _descriptor(manifest, raw):
    return {key: manifest[key] for key in
            ("siteGeneration", "baselineRevision", "compositionRevision")} | {
                "manifestHash": hashlib.sha256(raw).hexdigest()}


def _stat(path):
    info = Path(path).stat()
    if not Path(path).is_file() or info.st_size == 0:
        raise ValueError(f"TFT calculation cache is not ready: {path}")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _link(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError as error:
        if error.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP):
            raise
        shutil.copyfile(source, destination)


def _write(directory, url, payload):
    destination = directory / url.lstrip("/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(_json_bytes(payload))


def _compress(directory, url):
    path = directory / url.lstrip("/")
    zipped = Path(str(path) + ".gz")
    identity_hash = hashlib.sha256()
    with path.open("rb") as source, zipped.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output,
                           compresslevel=6, mtime=0) as compressor:
            while chunk := source.read(_CHUNK):
                identity_hash.update(chunk)
                compressor.write(chunk)
    gzip_hash = hashlib.sha256()
    with zipped.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            gzip_hash.update(chunk)
    return {encoding: {"path": str(file.relative_to(directory)), "size": file.stat().st_size,
                       "etag": '"' + digest.hexdigest() + '"'}
            for encoding, file, digest in (("identity", path, identity_hash),
                                          ("gzip", zipped, gzip_hash))}


def prepare(snap):
    """Prepare every TFT response from complete caches for one supplied snapshot.

    This does not warm caches, activate a snapshot, write the dashboard marker,
    or remove any previous generation. Call it before publishing the snapshot;
    pass its descriptor to the existing dashboard publication path afterwards.
    """
    import tft
    import tft_comps

    def revisions():
        if (hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != SOURCE_HASH
                or tft.source_stale() or tft_comps.source_stale()):
            raise RuntimeError("TFT site source changed; restart preparation with current code")
        return tft.snapshot_revision(snap), tft_comps.revision(snap)

    baseline, composition = revisions()
    presentation = _presentation_hash(snap)
    paths, comp_paths = tft.cell_paths(snap), tft_comps.cell_paths(snap)
    inventory = {"baselineCells": [list(key) for key in sorted(paths)],
                 "compositionContexts": sorted(comp_paths),
                 "leaderboardSelections": sorted(tft.leaderboard_scenarios())}
    expected = _endpoints(inventory)
    sources = {f"/api/tft/{slug}/{key}.json": Path(path)
               for (slug, key), path in paths.items()}
    sources.update({f"/api/tft/compositions/{key}.json": Path(path)
                    for key, path in comp_paths.items()})
    try:
        original = {url: _stat(path) for url, path in sources.items()}
    except FileNotFoundError as error:
        raise ValueError("all TFT champion and composition calculations must finish before publication") from error

    def unchanged():
        if (revisions() != (baseline, composition) or _presentation_hash(snap) != presentation
                or tft.cell_paths(snap) != paths or tft_comps.cell_paths(snap) != comp_paths
                or any(_stat(path) != original[url] for url, path in sources.items())):
            raise RuntimeError("TFT calculation inputs changed during site preparation")

    generation = _generation(baseline, composition, SOURCE_HASH, presentation)
    root = Path(CACHE_DIR)
    directory = root / generation
    if directory.exists():
        raw = (directory / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        descriptor = _descriptor(manifest, raw)
        bundle = load(descriptor)
        if (bundle.directory != directory or bundle.manifest["inventory"] != inventory
                or set(bundle.entries) != expected):
            raise ValueError("TFT site generation does not match the current endpoint set")
        unchanged()
        return descriptor

    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=root))
    try:
        # Source artifacts are atomically replaced by the warmers. A hardlink
        # retains these exact bytes even when a later warm prunes its old cache.
        for url, source in sources.items():
            _link(source, stage / url.lstrip("/"))
        unchanged()
        meta, comp_meta = tft.api_meta(snap), tft_comps.api_meta(snap)
        if (meta.get("revision") != baseline or comp_meta.get("revision") != composition
                or comp_meta.get("baselineRevision") != baseline):
            raise ValueError("TFT metadata does not match its calculation generation")
        _write(stage, "/api/tft/meta.json", dict(meta, publicationRevision=generation))
        _write(stage, "/api/tft/compositions/meta.json", dict(comp_meta, publicationRevision=generation))
        # Read through linked files so all derived responses use the exact
        # champion cells that this generation will serve.
        linked_paths = {key: str(stage / f"api/tft/{key[0]}/{key[1]}.json") for key in paths}
        for slug in sorted({slug for slug, _ in paths}):
            result = tft.cached_core_contexts(slug, linked_paths, snap=snap)
            count = sum(unit == slug for unit, _ in paths)
            if (result.get("unit") != slug or result.get("revision") != baseline
                    or result.get("totalScenarios") != count
                    or len(result.get("scenarios", [])) != count
                    or {scenario.get("key") for scenario in result.get("scenarios", [])}
                       != {key for unit, key in paths if unit == slug}):
                raise ValueError(f"TFT core comparisons are incomplete: {slug}")
            _write(stage, f"/api/tft/{slug}/cores.json", result)
        for key in inventory["leaderboardSelections"]:
            result = tft.cached_leaderboard(key, linked_paths, snap=snap)
            if (result.get("revision") != baseline or not result.get("complete")
                    or result.get("pending") or result.get("readyCount") != result.get("expectedCount")
                    or result.get("selection", {}).get("key") != key):
                raise ValueError(f"TFT leaderboard is incomplete: {key}")
            _write(stage, f"/api/tft/leaderboard/{key}.json", result)
        for key in inventory["compositionContexts"]:
            result = json.loads((stage / f"api/tft/compositions/{key}.json").read_bytes())
            if (result.get("revision") != composition or result.get("baselineRevision") != baseline
                    or result.get("key") != key):
                raise ValueError(f"TFT composition does not match its calculation generation: {key}")
        _write(stage, "/api/tft/status.json", {
            "ready": {f"{slug}/{key}": True for slug, key in paths}, "warmer": "idle",
            "patch": snap.patch, "revision": baseline, "publicationRevision": generation, "refresh": {}})
        _write(stage, "/api/tft/compositions/status.json", {
            "revision": composition, "ready": {key: True for key in comp_paths},
            "publicationRevision": generation, "warmer": "idle", "progress": {}})
        entries = {url: _compress(stage, url) for url in sorted(expected)}
        manifest = {"schema": SCHEMA, "siteGeneration": generation,
                    "baselineRevision": baseline, "compositionRevision": composition,
                    "publisherHash": SOURCE_HASH, "presentationHash": presentation,
                    "inventory": inventory, "entries": entries}
        raw = _json_bytes(manifest)
        (stage / "manifest.json").write_bytes(raw)
        descriptor = _descriptor(manifest, raw)
        _load(descriptor, directory=stage)
        unchanged()
        try:
            os.rename(stage, directory)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            # Another complete preparation won the same generation. Its
            # manifest may differ only in equivalent source-file serialization.
            existing_raw = (directory / "manifest.json").read_bytes()
            descriptor = _descriptor(json.loads(existing_raw), existing_raw)
        bundle = load(descriptor)
        if bundle.directory != directory or bundle.manifest["inventory"] != inventory:
            raise ValueError("concurrent TFT site publication used a different endpoint set")
        return descriptor
    finally:
        if stage.exists():
            shutil.rmtree(stage)
