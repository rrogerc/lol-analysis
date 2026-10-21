#!/usr/bin/env python3
"""Cache patch-matched Tactics.tools public-page final-board population stats."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tft
import tft_lobby


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_page(url, cache_dir, refresh=False):
    """Keep small extracted public-page data, not full HTML or combat content."""
    path = cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; lol-analysis statistics cache)"})
            with urllib.request.urlopen(request, timeout=30) as response:
                page = tft_lobby.parse_next_data(response.read().decode("utf-8"))
            break
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    compact = {key: page[key] for key in ("aperture", "unitId", "starLevel") if key in page}
    if "statsData" in page:
        stats = page["statsData"]
        compact["statsData"] = {key: stats[key] for key in ("totalEntries", "lastUpdated", "units")}
    if "unitData" in page:
        detail = page["unitData"]
        compact["unitData"] = {key: detail[key] for key in ("unitId", "starLevel", "base", "starLevelData")}
    compact["fetchedAt"] = now()
    compact["sourceUrl"] = url
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(compact, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
    return compact


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", type=int, default=18)
    parser.add_argument("--patch", required=True, help="Exact archived gameplay patch; latest is deliberately unsupported")
    parser.add_argument("--rank", action="append", choices=tft_lobby.RANKS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path(tempfile.gettempdir()) / "lol-analysis-tft-lobby-pages")
    parser.add_argument("--refresh", action="store_true", help="Refetch pages instead of reusing the patch-specific download cache")
    args = parser.parse_args(argv)
    snap = tft.load_snapshot(args.set, args.patch)
    if snap.set_no != 18:
        parser.error("only Set 18 source routes have been verified")
    tft_lobby.patch_code(snap.patch)
    ranks = list(dict.fromkeys(args.rank or ["diamond_plus", "all"]))
    profiles = []
    for rank in ranks:
        url = tft_lobby.source_url(snap.patch, rank)
        overview = fetch_page(url, args.cache_dir, args.refresh)
        tft_lobby.validate_page(overview, snap.patch, rank)
        stats = overview.get("statsData", {})
        source_units = stats.get("units", {})
        print(f"{snap.patch} {tft_lobby.RANKS[rank][1]}: {len(source_units)} source rows, {stats.get('totalEntries', 0):,} player entries", flush=True)
        details = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = {executor.submit(fetch_page, tft_lobby.source_url(snap.patch, rank, source_id), args.cache_dir, args.refresh): source_id
                       for source_id in source_units}
            for future in as_completed(pending):
                source_id = pending[future]
                details[source_id] = future.result()
                if len(details) % 20 == 0 or len(details) == len(source_units):
                    print(f"  fetched {len(details)}/{len(source_units)} unit distributions", flush=True)
        fetched_at = max([overview["fetchedAt"], *(page["fetchedAt"] for page in details.values())])
        profile = tft_lobby.normalize_profile(snap, rank, overview, details, fetched_at)
        profiles.append(profile)
        print(f"  mapped every shop champion: {len(profile['units'])}; updated {profile['updatedAt']}", flush=True)
    document = tft_lobby.make_document(snap, profiles)
    destination = tft_lobby.write_document(snap, document, args.output)
    print(f"Saved {len(profiles)} validated profiles to {destination}")
    for limitation in document["limitations"]:
        print(f"  Limitation: {limitation}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"Cannot save lobby statistics: {exc}", file=sys.stderr)
        raise SystemExit(1)
