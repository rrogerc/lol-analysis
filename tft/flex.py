"""Units, breakpoints, and cached lobby population profiles for the units-only TFT planner.

The catalog includes every shop unit, including units and traits without a
combat model. It does not choose items, resolve combat effects, or simulate,
and never imports the combat engine.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import sys
import tempfile

from web.db import BASE_DIR


SCHEMA = "tft-trait-flex-v1"
LOBBY_SCHEMA = "tft-lobby-stats-v1"
ELDER_DRAGON = "TFT18_ElderDragon"
RIFTBEAST = "DA_Riftbeast18"
SOURCE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

SOURCE_ROOT = "https://tactics.tools"
RANKS = {"diamond_plus": (1, "Diamond+", ""), "all": (3, "All ranks", "/all")}
STAR_COPIES = (1, 3, 9, 27)
LIMITATIONS = [
    "Finished-game boards approximate a typical lobby; they are not observed stage 4-1 holdings.",
    "Bench units and extra copies held toward upgrades are absent from these statistics.",
    "Star levels use 1/3/9/27 copy equivalents; generated units and magical upgrades can differ from actual pool withdrawals.",
    "This profile pools regions and levels; it does not condition opponents on being level 8.",
]


def unit_slots(api):
    return 2 if api == ELDER_DRAGON else 1


def _json_hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _unit_slug(unit):
    return re.sub(r"[^a-z0-9]", "", unit["name"].lower())


def _curve_at(curve, star):
    val = None
    for s, v in curve:
        if s <= star:
            val = v
        else:
            break
    if val is None:
        val = curve[0][1]
    return val


def _cdragon_image_url(asset):
    if not isinstance(asset, str):
        return None
    path = asset.strip().lower()
    if (not re.fullmatch(r"assets/[a-z0-9_./-]+\.(?:tex|png)", path)
            or any(part in (".", "..") for part in path.split("/"))):
        return None
    return "https://raw.communitydragon.org/latest/game/" + re.sub(r"\.tex$", ".png", path)


def _ui_icons(snap):
    champions = snap.communitydragon.get("champions", [])
    by_api = {u["apiName"]: u for u in champions}
    units = {}
    for api, unit in snap.units.items():
        match = next((by_api[a] for a in [api, *unit["assets"]] if a in by_api), None)
        if match is None:
            named = [u for u in champions if (u.get("name") or "").casefold() == unit["name"].casefold()]
            match = named[0] if len(named) == 1 else {}
        units[api] = next((url for key in ("squareIcon", "tileIcon", "icon")
                           if (url := _cdragon_image_url(match.get(key)))), None)
    traits = {t["apiName"]: _cdragon_image_url(t.get("icon"))
              for t in snap.communitydragon.get("traits", [])}
    return units, traits


def _set_dir(set_no):
    sim = sys.modules.get("tft.sim")
    if sim is not None and hasattr(sim, "set_dir"):
        return Path(sim.set_dir(set_no))
    return Path(BASE_DIR) / "tft" / "data" / f"set{set_no}"


def source_stale():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != SOURCE_HASH


class _NextDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reading = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.reading = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.reading = False

    def handle_data(self, data):
        if self.reading:
            self.parts.append(data)


def parse_next_data(html):
    """Read the public page's server-rendered data without executing scripts."""
    parser = _NextDataParser()
    parser.feed(html)
    if not parser.parts:
        raise ValueError("statistics page has no __NEXT_DATA__")
    try:
        result = json.loads("".join(parser.parts))["props"]["pageProps"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("statistics page has invalid __NEXT_DATA__") from exc
    if not isinstance(result, dict):
        raise ValueError("statistics page has invalid pageProps")
    return result


def patch_code(patch):
    """Tactics.tools' public Set 18 route metadata uses League patch codes."""
    match = re.fullmatch(r"18\.(\d+)([b-d]?)", patch)
    if not match:
        raise ValueError(f"unsupported statistics patch {patch!r}; expected Set 18")
    suffix = {"": 0, "b": 1, "c": 2, "d": 3}[match[2]]
    return 16000 + (int(match[1]) + 16) * 10 + suffix


def source_url(patch, rank, source_id=None):
    if rank not in RANKS:
        raise ValueError(f"unsupported rank group {rank!r}")
    patch_code(patch)
    unit = f"/{source_id.lower()}" if source_id else ""
    return f"{SOURCE_ROOT}/units{unit}/{patch}{RANKS[rank][2]}"


def validate_page(page, patch, rank, source_id=None):
    """Reject silent route fallbacks, other queues, or a wrong unit detail."""
    aperture = page.get("aperture", {})
    actual = aperture.get("patch", {})
    if not isinstance(actual, dict) or actual.get("_0") != patch_code(patch):
        raise ValueError(f"statistics patch mismatch: requested {patch}, received {actual!r}")
    if aperture.get("rankGroup") != RANKS[rank][0]:
        raise ValueError(f"statistics rank mismatch for {rank}")
    if aperture.get("queue") != 1100 or aperture.get("gameType") != 0:
        raise ValueError("statistics are not normal ranked TFT")
    if source_id is not None:
        detail = page.get("unitData", {})
        if page.get("unitId") != source_id or detail.get("unitId") != source_id:
            raise ValueError(f"statistics unit mismatch for {source_id}")
        if page.get("starLevel") != 0 or detail.get("starLevel") != 0:
            raise ValueError("statistics detail unexpectedly filters star level")
    return page


def _integer(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _finite(value, label, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"invalid {label}: {value!r}")
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"out of range {label}: {value!r}")
    return value


def _timestamp(value, label):
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {label}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return value


def _asset_mapping(snap):
    mapping = {}
    for unit in snap.raw["units"]:
        api = unit["apiName"]
        if api not in snap.units:
            continue
        for asset in unit.get("assetNames", []):
            if asset in mapping and mapping[asset] != api:
                raise ValueError(f"ambiguous statistics asset {asset}")
            mapping[asset] = api
    return mapping


def normalize_profile(snap, rank, overview, details, fetched_at):
    """Aggregate source asset variants using the gameplay snapshot's mapping."""
    validate_page(overview, snap.patch, rank)
    if snap.set_no != 18:
        raise ValueError("statistics set does not match Set 18")
    stats = overview.get("statsData", {})
    total = _integer(stats.get("totalEntries"), "player entry count", 1)
    updated = _integer(stats.get("lastUpdated"), "statistics update time", 1)
    source_units = stats.get("units")
    if not isinstance(source_units, dict) or not source_units:
        raise ValueError("statistics overview has no units")
    mapping = _asset_mapping(snap)
    unknown = set(source_units) - mapping.keys()
    if unknown:
        raise ValueError(f"unmapped statistics units: {', '.join(sorted(unknown))}")
    grouped = {api: [] for api in snap.units}
    for source_id, row in sorted(source_units.items()):
        count = _integer(row.get("count"), f"{source_id} count")
        if source_id not in details:
            raise ValueError(f"missing statistics detail for {source_id}")
        page = validate_page(details[source_id], snap.patch, rank, source_id)
        detail = page["unitData"]
        if detail.get("base", {}).get("count") != count:
            raise ValueError(f"overview/detail count mismatch for {source_id}")
        star_counts = [0] * len(STAR_COPIES)
        levels = set()
        for star in detail.get("starLevelData", []):
            level = _integer(star.get("id"), f"{source_id} star level", 1)
            if level > len(STAR_COPIES) or level in levels:
                raise ValueError(f"invalid or repeated star level for {source_id}")
            levels.add(level)
            star_counts[level - 1] = _integer(star.get("count"), f"{source_id} star count")
        if sum(star_counts) != count:
            raise ValueError(f"incomplete star distribution for {source_id}")
        grouped[mapping[source_id]].append({"id": source_id, "count": count,
                                            "starCounts": star_counts,
                                            "sourceUrl": source_url(snap.patch, rank, source_id)})
    units = []
    for api, unit in sorted(snap.units.items(), key=lambda pair: (pair[1]["cost"], pair[1]["name"], pair[0])):
        sources = grouped[api]
        if not sources:
            raise ValueError(f"missing shop champion statistics for {api}")
        count = sum(row["count"] for row in sources)
        star_counts = [sum(row["starCounts"][i] for row in sources) for i in range(len(STAR_COPIES))]
        copy_count = sum(n * copies for n, copies in zip(star_counts, STAR_COPIES))
        units.append({"api": api, "slug": _unit_slug(unit), "name": unit["name"],
                      "cost": unit["cost"], "playRatePerLobby": count / total * 8,
                      "meanCopiesWhenPlayed": copy_count / count if count else 0,
                      "starRates": [n / count if count else 0 for n in star_counts],
                      "copiesPerPlayer": copy_count / total, "sourceUnits": sources})
    return {"id": f"{snap.patch}-{rank}", "patch": snap.patch, "rank": rank,
            "rankLabel": RANKS[rank][1], "fetchedAt": _timestamp(fetched_at, "fetch time"),
            "updatedAt": datetime.fromtimestamp(updated, timezone.utc).isoformat().replace("+00:00", "Z"),
            "sourceUrl": source_url(snap.patch, rank), "totalEntries": total, "units": units}


def make_document(snap, profiles):
    default = f"{snap.patch}-diamond_plus"
    if not any(profile["id"] == default for profile in profiles):
        default = profiles[0]["id"] if profiles else None
    return {"schema": LOBBY_SCHEMA, "set": snap.set_no, "patch": snap.patch,
            "profiles": profiles, "defaultProfile": default,
            "copyEquivalentsByStar": list(STAR_COPIES), "limitations": list(LIMITATIONS)}


def validate_document(snap, document):
    """Validate cached provenance and recompute every published numeric value."""
    if document.get("schema") != LOBBY_SCHEMA:
        raise ValueError("unsupported lobby statistics schema")
    if document.get("set") != snap.set_no or document.get("patch") != snap.patch:
        raise ValueError("lobby statistics set/patch mismatch")
    if document.get("copyEquivalentsByStar") != list(STAR_COPIES):
        raise ValueError("unsupported lobby statistics copy model")
    profiles = document.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("invalid lobby statistics profiles")
    mapping = _asset_mapping(snap)
    profile_ids = set()
    for profile in profiles:
        rank = profile.get("rank")
        if rank not in RANKS or profile.get("rankLabel") != RANKS[rank][1]:
            raise ValueError("invalid lobby statistics rank")
        if profile.get("patch") != snap.patch or profile.get("id") != f"{snap.patch}-{rank}":
            raise ValueError("lobby profile patch/id mismatch")
        if profile["id"] in profile_ids:
            raise ValueError("duplicate lobby profile")
        profile_ids.add(profile["id"])
        _timestamp(profile.get("fetchedAt"), "fetch time")
        _timestamp(profile.get("updatedAt"), "update time")
        if profile.get("sourceUrl") != source_url(snap.patch, rank):
            raise ValueError("lobby profile source URL mismatch")
        total = _integer(profile.get("totalEntries"), "player entry count", 1)
        rows = profile.get("units")
        if not isinstance(rows, list) or len(rows) != len(snap.units) or {row.get("api") for row in rows} != set(snap.units):
            raise ValueError("lobby profile does not cover every shop champion exactly once")
        source_ids = set()
        for row in rows:
            api = row["api"]
            unit = snap.units[api]
            if (row.get("name"), row.get("cost"), row.get("slug")) != (unit["name"], unit["cost"], _unit_slug(unit)):
                raise ValueError(f"lobby unit identity mismatch for {api}")
            sources = row.get("sourceUnits")
            if not isinstance(sources, list) or not sources:
                raise ValueError(f"missing lobby unit provenance for {api}")
            count = 0
            counts = [0] * len(STAR_COPIES)
            for source in sources:
                source_id = source.get("id")
                if mapping.get(source_id) != api or source_id in source_ids:
                    raise ValueError(f"invalid or duplicate source asset for {api}")
                source_ids.add(source_id)
                if source.get("sourceUrl") != source_url(snap.patch, rank, source_id):
                    raise ValueError(f"lobby unit source URL mismatch for {api}")
                n = _integer(source.get("count"), f"{api} source count")
                star_counts = source.get("starCounts")
                if not isinstance(star_counts, list) or len(star_counts) != len(STAR_COPIES):
                    raise ValueError(f"invalid cached star distribution for {api}")
                for i, star_count in enumerate(star_counts):
                    counts[i] += _integer(star_count, f"{api} star count")
                if sum(star_counts) != n:
                    raise ValueError(f"incomplete cached star distribution for {api}")
                count += n
            copy_count = sum(n * copies for n, copies in zip(counts, STAR_COPIES))
            expected = {"playRatePerLobby": count / total * 8,
                        "meanCopiesWhenPlayed": copy_count / count if count else 0,
                        "copiesPerPlayer": copy_count / total}
            for field, value in expected.items():
                if not math.isclose(_finite(row.get(field), f"{api} {field}"), value, rel_tol=1e-12, abs_tol=1e-12):
                    raise ValueError(f"inconsistent {field} for {api}")
            rates = row.get("starRates")
            if not isinstance(rates, list) or len(rates) != len(STAR_COPIES):
                raise ValueError(f"invalid star rates for {api}")
            for rate, n in zip(rates, counts):
                if not math.isclose(_finite(rate, f"{api} star rate", maximum=1), n / count if count else 0, rel_tol=1e-12, abs_tol=1e-12):
                    raise ValueError(f"inconsistent star rate for {api}")
    if document.get("defaultProfile") not in profile_ids and (profiles or document.get("defaultProfile") is not None):
        raise ValueError("invalid default lobby profile")
    return document


def cache_path(snap):
    if getattr(snap, "dir", None):
        return Path(snap.dir).parent / f"lobby-stats-{snap.patch}.json"
    return _set_dir(snap.set_no) / f"lobby-stats-{snap.patch}.json"


def write_document(snap, document, destination=None):
    """Validate before replacing a cached profile file atomically."""
    validate_document(snap, document)
    destination = Path(destination) if destination is not None else cache_path(snap)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         prefix=f".{destination.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        temporary.replace(destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


def catalog_profiles(snap):
    """Return only this gameplay snapshot's locally saved population profiles."""
    path = cache_path(snap)
    if not path.exists():
        return make_document(snap, [])
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    return validate_document(snap, document)


def shop_model(snap):
    """Reviewed shop parameters, with tier populations from this exact roster."""
    try:
        model = json.loads((_set_dir(snap.set_no) / "shop-model.json").read_bytes())
    except FileNotFoundError:
        return None
    odds, bags, slots = (model.get(key) for key in ("costOdds", "bagCopies", "visibleSlots"))
    if (model.get("schema") != "tft-level-eight-shops-v1" or model.get("set") != snap.set_no
            or model.get("level") != 8 or model.get("refreshCost") != 2
            or not isinstance(odds, list) or len(odds) != 5
            or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in odds)
            or not math.isclose(sum(odds), 1, abs_tol=1e-12)
            or not isinstance(bags, list) or len(bags) != 5
            or any(type(value) is not int or value < 3 for value in bags)
            or slots not in ([4, 5], [5])):
        raise ValueError("invalid TFT level-eight shop model")
    populations = Counter(unit["cost"] for unit in snap.units.values())
    return dict(model, championCounts=[populations[cost] for cost in range(1, 6)])


def catalog(snap):
    """Extract only unit identity, base roles, slot usage and trait thresholds."""
    unit_icons, trait_icons = _ui_icons(snap)
    traits = []
    for api, trait in sorted(snap.traits.items(), key=lambda pair: (pair[1]["name"], pair[0])):
        levels = sorted({int(level) for level in trait["levels"] if level > 0})
        traits.append({"api": api, "name": trait["name"], "icon": trait_icons.get(api),
                       "levels": levels, "sharedLevels": [level for level in levels if level >= 2]})

    units = []
    for api, unit in sorted(snap.units.items(), key=lambda pair: (pair[1]["cost"], pair[1]["name"], pair[0])):
        trait_apis = list(dict.fromkeys(unit["traitApis"]))
        if any(trait not in snap.traits for trait in trait_apis):
            raise ValueError(f"unknown trait on {api} in TFT trait catalog")
        contributions = {trait: 1 for trait in trait_apis}
        if api == ELDER_DRAGON and RIFTBEAST in contributions:
            contributions[RIFTBEAST] = 2
        attack_range = unit["stats"]["range"]
        extra_range = unit.get("curve", {}).get("AdditionalAttackRange")
        if extra_range:
            attack_range += _curve_at(extra_range, 1)
        objective = unit["objective"]
        units.append({"api": api, "slug": _unit_slug(unit), "name": unit["name"],
                      "cost": unit["cost"], "icon": unit_icons.get(api),
                      "role": unit["roleName"], "kind": unit["kind"], "objective": objective,
                      "range": attack_range,
                      "frontline": objective in ("tank", "fighter") and attack_range <= 2,
                      "backline": objective == "carry" and attack_range > 2,
                      "slotCost": unit_slots(api), "traitApis": trait_apis,
                      "traitContributions": contributions})

    result = {"schema": SCHEMA, "set": snap.set_no, "patch": snap.patch,
              "level": 8, "boardSlots": 8, "units": units, "traits": traits}
    shop = shop_model(snap)
    if shop is not None:
        result["shop"] = shop
        try:
            result["lobbyStats"] = catalog_profiles(snap)
        except ValueError as error:
            result["lobbyStats"] = {"schema": LOBBY_SCHEMA, "profiles": [],
                                    "defaultProfile": None, "error": str(error)}
    return dict(result, revision=_json_hash([SOURCE_HASH, result]))


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_page(url, cache_dir, refresh=False):
    """Keep small extracted public-page data, not full HTML or combat content."""
    from tft import update as tft_http

    path = cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    response = tft_http.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; lol-analysis statistics cache)"},
        max_attempts=3,
    )
    page = parse_next_data(response.body.decode("utf-8"))
    compact = {key: page[key] for key in ("aperture", "unitId", "starLevel") if key in page}
    if "statsData" in page:
        stats = page["statsData"]
        compact["statsData"] = {key: stats[key] for key in ("totalEntries", "lastUpdated", "units")}
    if "unitData" in page:
        detail = page["unitData"]
        compact["unitData"] = {key: detail[key] for key in ("unitId", "starLevel", "base", "starLevelData")}
    compact["fetchedAt"] = _now()
    compact["sourceUrl"] = url
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(compact, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
    return compact


def add_fetch_lobby_stats_args(parser):
    parser.add_argument("--set", type=int, default=18)
    parser.add_argument("--patch", required=True,
                        help="Exact archived gameplay patch; latest is deliberately unsupported")
    parser.add_argument("--rank", action="append", choices=RANKS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache-dir", type=Path,
                        default=Path(tempfile.gettempdir()) / "lol-analysis-tft-lobby-pages")
    parser.add_argument("--refresh", action="store_true",
                        help="Refetch pages instead of reusing the patch-specific download cache")
    return parser


def cmd_fetch_lobby_stats(args):
    from tft import sim as tft

    snap = tft.load_snapshot(args.set, args.patch)
    if snap.set_no != 18:
        raise ValueError("only Set 18 source routes have been verified")
    patch_code(snap.patch)
    ranks = list(dict.fromkeys(args.rank or ["diamond_plus", "all"]))
    profiles = []
    for rank in ranks:
        url = source_url(snap.patch, rank)
        overview = fetch_page(url, args.cache_dir, args.refresh)
        validate_page(overview, snap.patch, rank)
        stats = overview.get("statsData", {})
        source_units = stats.get("units", {})
        print(f"{snap.patch} {RANKS[rank][1]}: {len(source_units)} source rows, {stats.get('totalEntries', 0):,} player entries", flush=True)
        details = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = {executor.submit(fetch_page, source_url(snap.patch, rank, source_id), args.cache_dir, args.refresh): source_id
                       for source_id in source_units}
            for future in as_completed(pending):
                source_id = pending[future]
                details[source_id] = future.result()
                if len(details) % 20 == 0 or len(details) == len(source_units):
                    print(f"  fetched {len(details)}/{len(source_units)} unit distributions", flush=True)
        fetched_at = max([overview["fetchedAt"], *(page["fetchedAt"] for page in details.values())])
        profile = normalize_profile(snap, rank, overview, details, fetched_at)
        profiles.append(profile)
        print(f"  mapped every shop champion: {len(profile['units'])}; updated {profile['updatedAt']}", flush=True)
    document = make_document(snap, profiles)
    destination = write_document(snap, document, args.output)
    print(f"Saved {len(profiles)} validated profiles to {destination}")
    for limitation in document["limitations"]:
        print(f"  Limitation: {limitation}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cache patch-matched Tactics.tools public-page final-board population stats.")
    add_fetch_lobby_stats_args(parser)
    args = parser.parse_args(argv)
    return cmd_fetch_lobby_stats(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"Cannot save lobby statistics: {exc}", file=sys.stderr)
        raise SystemExit(1)
