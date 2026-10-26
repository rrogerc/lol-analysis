"""Champion scaling and Master+ one-tricks from the ../lol-quant soloq crawl.

Owns the `stats`, `match_counts`, `onetricks`, and `onetrick_sync` tables in
lol.db, plus the lol/data/scaling/ JSON archive. Scaling tracks every lane
above 10% play rate across seven game-length intervals (0-15 ... 40+ minutes).
One-tricks counts Master+ ladder players whose ranked solo games in a role are
more than 85% on one champion with at least 20 games this season.
"""

import argparse
import gzip
import json
import os
import sys
import zlib
from datetime import datetime, timezone

from web.db import BASE_DIR, DB_PATH, db_connect, patch_key, write_csv

SCALING_DATA_DIR = os.path.join(BASE_DIR, "lol", "data", "scaling")

BUCKET_LABELS = {
    1: "0-15 min", 2: "15-20 min", 3: "20-25 min", 4: "25-30 min",
    5: "30-35 min", 6: "35-40 min", 7: "40+ min",
}
EARLY_BUCKETS = (1, 2)   # 0-20 min
LATE_BUCKETS = (6, 7)    # 35+ min

MIN_SHARE_PCT = 85  # strictly more than this share of the role's games
MIN_GAMES = 20      # games on the champion in the role, as soloq_otp requires
APEX_TIERS = {"MASTER", "GRANDMASTER", "CHALLENGER"}
LANES = ("top", "jungle", "middle", "bottom", "support")


# ---------------------------------------------------------------------------
# stats table access
# ---------------------------------------------------------------------------

def db_patches(con, tier):
    rows = con.execute("SELECT DISTINCT patch FROM stats WHERE tier=?", (tier,)).fetchall()
    return sorted((r[0] for r in rows), key=patch_key)


def default_tier(con):
    """soloq_masters_plus when present (the primary tier: every game);
    otherwise the most recently imported tier, ties broken by newest patch
    covered, then by data volume."""
    rows = con.execute(
        "SELECT tier, MAX(scraped_at), SUM(games) FROM stats GROUP BY tier"
    ).fetchall()
    if not rows:
        return None
    if any(t == "soloq_masters_plus" for t, _, _ in rows):
        return "soloq_masters_plus"
    newest = {t: max((patch_key(p) for p in db_patches(con, t)), default=(0,))
              for t, _, _ in rows}
    return max(rows, key=lambda r: (r[1], newest[r[0]], r[2]))[0]


def replace_patch_data(con, patch, tier, entries):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        (patch, tier, e["champion"], e["lane"], int(k),
         b["games"], b["wins"], e.get("lane_play_rate"), now)
        for e in entries for k, b in e["buckets"].items()
    ]
    with con:
        con.execute("DELETE FROM stats WHERE patch=? AND tier=?", (patch, tier))
        con.executemany("INSERT INTO stats VALUES (?,?,?,?,?,?,?,?,?)", rows)


def replace_match_count(con, patch, tier, matches):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with con:
        con.execute("DELETE FROM match_counts WHERE patch=? AND tier=?",
                    (patch, tier))
        con.execute("INSERT INTO match_counts VALUES (?,?,?,?)",
                    (patch, tier, matches, now))


def aggregate(con, tier, patches, lane=None):
    """Sum games/wins per (champion, lane, bucket) across patches."""
    ph = ",".join("?" * len(patches))
    q = (f"SELECT champion, lane, bucket, SUM(games), SUM(wins) FROM stats "
         f"WHERE tier=? AND patch IN ({ph})")
    params = [tier, *patches]
    if lane:
        q += " AND lane=?"
        params.append(lane)
    q += " GROUP BY champion, lane, bucket"
    agg = {}
    for champ, ln, bucket, games, wins in con.execute(q, params):
        agg.setdefault((champ, ln), {})[bucket] = (games, wins)
    return agg


def phase_wr(buckets, phase):
    games = sum(buckets.get(b, (0, 0))[0] for b in phase)
    wins = sum(buckets.get(b, (0, 0))[1] for b in phase)
    return (wins / games * 100 if games else None), games


def resolve_slice(con, args):
    tier = getattr(args, "tier", None) or default_tier(con)
    if not tier:
        sys.exit("Database is empty: run `cli.py scaling sync` or `cli.py import-json` first.")
    patches = getattr(args, "patches", None) or db_patches(con, tier)
    if not patches:
        sys.exit(f"No data for tier '{tier}'. Available: run `cli.py status`.")
    return tier, patches


def fmt_name(champ, lane):
    return champ if lane == "main" else f"{champ} ({lane})"


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def cmd_report(args):
    con = db_connect()
    tier, patches = resolve_slice(con, args)
    agg = aggregate(con, tier, patches, args.lane)
    print(f"Tier: {tier} | Patches: {', '.join(patches)} | min games: {args.min_games}")

    if args.scaling:
        rows = []
        for (champ, lane), buckets in agg.items():
            early_wr, early_g = phase_wr(buckets, EARLY_BUCKETS)
            late_wr, late_g = phase_wr(buckets, LATE_BUCKETS)
            total = sum(g for g, _ in buckets.values())
            if early_wr is None or late_wr is None:
                continue
            if early_g < args.min_games or late_g < args.min_games:
                continue
            rows.append((fmt_name(champ, lane), early_wr, late_wr, late_wr - early_wr, total))
        rows.sort(key=lambda r: r[3], reverse=True)
        print("\n=== Scaling: late-game WR (35+ min) minus early WR (0-20 min) ===")
        print(f"{'Rank':<5} {'Champion':<25} {'Early WR':<10} {'Late WR':<10} {'Delta':<8} {'Games':<10}")
        print("-" * 70)
        shown = rows[:args.top] + ([] if len(rows) <= args.top else rows[-args.top:])
        for rank, (name, ewr, lwr, d, total) in enumerate(shown, 1):
            marker = rank if rank <= args.top else len(rows) - (len(shown) - rank)
            print(f"{marker:<5} {name:<25} {ewr:>6.2f}%   {lwr:>6.2f}%   {d:>+6.2f}  {total:>9,}")
            if rank == args.top and len(rows) > args.top:
                print(f"{'...':<5} (bottom {args.top}: early-game champions)")
        if args.csv:
            write_csv(args.csv, ["champion", "early_wr", "late_wr", "delta", "games"], rows)
        return

    for bucket in range(1, 8):
        ranking = []
        for (champ, lane), buckets in agg.items():
            games, wins = buckets.get(bucket, (0, 0))
            if games >= args.min_games:
                ranking.append((fmt_name(champ, lane), wins / games * 100, games))
        ranking.sort(key=lambda r: r[1], reverse=True)
        print(f"\n=== {BUCKET_LABELS[bucket]} (Top {args.top}) ===")
        print(f"{'Rank':<5} {'Champion':<25} {'Win Rate':<10} {'Games':<10}")
        print("-" * 55)
        for rank, (name, wr, games) in enumerate(ranking[:args.top], 1):
            print(f"{rank:<5} {name:<25} {wr:>6.2f}%  {games:>9,}")

    if args.csv:
        rows = [(fmt_name(c, l), b, g, w, w / g * 100 if g else 0)
                for (c, l), buckets in agg.items() for b, (g, w) in sorted(buckets.items())]
        write_csv(args.csv, ["champion", "bucket", "games", "wins", "win_rate"], rows)


def cmd_champion(args):
    con = db_connect()
    tier, patches = resolve_slice(con, args)
    ph = ",".join("?" * len(patches))
    rows = con.execute(
        f"SELECT lane, bucket, SUM(games), SUM(wins), COUNT(DISTINCT patch) FROM stats "
        f"WHERE tier=? AND champion=? AND patch IN ({ph}) GROUP BY lane, bucket",
        [tier, args.name, *patches]).fetchall()
    if not rows:
        sys.exit(f"No data for '{args.name}' at {tier}. (Names are lowercase slugs, e.g. 'missfortune'.)")

    lanes = {}
    coverage = {}
    for lane, bucket, games, wins, npatches in rows:
        lanes.setdefault(lane, {})[bucket] = (games, wins)
        coverage[lane] = max(coverage.get(lane, 0), npatches)

    lane_order = sorted(lanes, key=lambda l: -sum(g for g, _ in lanes[l].values()))
    print(f"{args.name} @ {tier} | patches: {', '.join(patches)}")
    print(f"\n{'Interval':<12}" + "".join(f"{l:>22}" for l in lane_order))
    print("-" * (12 + 22 * len(lane_order)))
    for bucket in range(1, 8):
        cells = []
        for lane in lane_order:
            g, w = lanes[lane].get(bucket, (0, 0))
            cells.append(f"{w / g * 100:>6.2f}% ({g:>8,})" if g else f"{'-':>18}")
        print(f"{BUCKET_LABELS[bucket]:<12}" + "".join(f"{c:>22}" for c in cells))
    print("\nPatch coverage: " + ", ".join(
        f"{l}: {coverage[l]}/{len(patches)}" for l in lane_order))


def cmd_status(args):
    con = db_connect()
    rows = con.execute(
        "SELECT tier, patch, COUNT(DISTINCT champion || '/' || lane), SUM(games), MAX(scraped_at) "
        "FROM stats GROUP BY tier, patch").fetchall()
    if not rows:
        print("Database is empty.")
        return
    rows.sort(key=lambda r: (r[0], patch_key(r[1])))
    print(f"{'Tier':<15} {'Patch':<8} {'Champ-lanes':<12} {'Games':<14} {'Imported at':<22}")
    print("-" * 72)
    for tier, patch, recs, games, at in rows:
        print(f"{tier:<15} {patch:<8} {recs:<12} {games:<14,} {at:<22}")


# ---------------------------------------------------------------------------
# Imports (lol/data/ archive and the lol-quant crawl)
# ---------------------------------------------------------------------------

def cmd_import_json(args):
    con = db_connect()
    imported = 0
    for root, _dirs, files in os.walk(args.data_dir):
        if "champion_win_rates.json" not in files:
            continue
        parts = os.path.normpath(root).split(os.sep)
        patch, tier = parts[-2], parts[-1]
        with open(os.path.join(root, "champion_win_rates.json")) as f:
            data = json.load(f)
        if not data:
            continue
        entries = [{"champion": e["champion"],
                    "lane": e.get("lane") or "main",
                    "lane_play_rate": e.get("lane_play_rate"),
                    "buckets": {k: {"games": b["games"], "wins": b["wins"]}
                                for k, b in e["buckets"].items()}}
                   for e in data]
        replace_patch_data(con, patch, tier, entries)
        mc_path = os.path.join(root, "match_count.json")
        if os.path.exists(mc_path):
            with open(mc_path) as f:
                replace_match_count(con, patch, tier, json.load(f)["matches"])
        imported += 1
        print(f"Imported {patch}/{tier}: {len(entries)} records")
    print(f"\nImported {imported} files into {DB_PATH}")


SOLOQ_ROLE_MAP = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "middle",
                  "BOTTOM": "bottom", "UTILITY": "support"}
# Riot internal names that do not lowercase to the champion's usual slug.
SOLOQ_SLUG_FIXES = {"monkeyking": "wukong"}


# A chunk of the crawl is at most this much parquet, and this many files
# (each file's footer stays in memory while its chunk is read); the crawl's
# shards run from 20 KB to 900 KB. See crawl_snapshot.
CRAWL_CHUNK_BYTES = 256 << 20
CRAWL_CHUNK_FILES = 1000


def crawl_snapshot(quant_dir):
    """The crawl's participants parquet as it is now: (part_dir, chunks,
    schema), `chunks` being its file list cut into consecutive groups of at
    most CRAWL_CHUNK_BYTES and CRAWL_CHUNK_FILES.

    Read it through crawl_chunks, never as one dataset over every file: a
    dataset keeps the parquet footer of each file it has scanned, and over
    the crawl's ~53k uncompacted files a one-column scan alone outgrew 6 GB
    (2026-10-07). The file list is fixed here, so every pass of a sync reads
    the same rows while the crawler keeps adding files."""
    import pyarrow.dataset as pads

    part_dir = os.path.join(quant_dir, "data", "parquet", "participants")
    if not os.path.isdir(part_dir):
        sys.exit(f"No participants parquet at {part_dir}. Check --quant-dir.")
    dataset = pads.dataset(part_dir, format="parquet")
    if not dataset.files:
        sys.exit(f"No participants parquet files under {part_dir}.")
    chunks, size = [[]], 0
    for path in dataset.files:
        n = os.path.getsize(path)
        if chunks[-1] and (size + n > CRAWL_CHUNK_BYTES
                           or len(chunks[-1]) >= CRAWL_CHUNK_FILES):
            chunks.append([])
            size = 0
        chunks[-1].append(path)
        size += n
    return part_dir, chunks, dataset.schema


def crawl_chunks(snapshot):
    """A dataset over each chunk of a snapshot's files, with the whole
    crawl's schema. A count or sum taken per chunk is the crawl's once added
    up across chunks."""
    import pyarrow.dataset as pads

    _, chunks, schema = snapshot
    for files in chunks:
        yield pads.dataset(files, schema=schema, format="parquet")


class _Codes:
    """Strings (None included) to dense integer codes, in first-seen order."""

    def __init__(self):
        self.code, self.names = {}, []

    def _get(self, name):
        code = self.code.get(name)
        if code is None:
            code = self.code[name] = len(self.names)
            self.names.append(name)
        return code

    def encode(self, column):
        import numpy as np
        import pyarrow.compute as pc

        enc = pc.dictionary_encode(column.combine_chunks(), null_encoding="encode")
        lut = np.fromiter(map(self._get, enc.dictionary.to_pylist()), np.int64,
                          len(enc.dictionary))
        return lut[enc.indices.to_numpy()]


def _sum_by_key(keys, counts):
    """Sorted unique `keys` and the summed `counts` of each."""
    import numpy as np

    if not len(keys):
        return keys, counts
    order = np.argsort(keys, kind="stable")  # merges the already-sorted runs
    keys, counts = keys[order], counts[order]
    del order
    starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
    return keys[starts], np.add.reduceat(counts, starts)


def _player_games(snapshot, filt):
    """Games per (puuid, role, champion) over a snapshot's rows: the counts a
    group_by over every row would give, taken a chunk at a time on integer
    codes, since the crawl's tens of millions of distinct player keys do not
    fit in memory as 78-character puuid strings.

    Returns {"keys": sorted puuid << 24 | role << 16 | champion codes,
    "games": their counts, "names": {column: the strings the codes index},
    "rows": rows read}. The mastery and one-trick tiers both filter on it."""
    import numpy as np
    import pyarrow as pa

    puuids, roles, champs = _Codes(), _Codes(), _Codes()
    keys, counts = np.empty(0, np.int64), np.empty(0, np.int32)
    pending, rows = [], 0

    def merge():
        nonlocal keys, counts, pending
        merged = (np.concatenate([keys, *(k for k, _ in pending)]),
                  np.concatenate([counts, *(n for _, n in pending)]))
        keys = counts = pending = None  # free the inputs before sorting
        keys, counts = _sum_by_key(*merged)
        pending = []
        _release_memory()

    for chunk in crawl_chunks(snapshot):
        table = chunk.to_table(columns=["puuid", "role", "champion"], filter=filt)
        rows += table.num_rows
        # key = puuid << 24 | role << 16 | champion
        key = (puuids.encode(table["puuid"]) << 24 | roles.encode(table["role"]) << 16
               | champs.encode(table["champion"]))
        if len(roles.names) > 0xFF or len(champs.names) > 0xFFFF:
            sys.exit("Too many distinct roles or champions to pack into a key.")
        unique, n = np.unique(key, return_counts=True)
        pending.append((unique, n.astype(np.int32)))
        if sum(len(k) for k, _ in pending) > 16_000_000:
            merge()
    merge()
    return {"keys": keys, "games": counts, "rows": rows,
            "names": {"puuid": pa.array(puuids.names, pa.string()),
                      "role": pa.array(roles.names, pa.string()),
                      "champion": pa.array(champs.names, pa.string())}}


def _player_filter(players, args):
    """The keys a mastery or one-trick tier keeps, (puuid, champion) or
    (puuid, role, champion), as a table to inner-join the rows with, from
    the counts of _player_games."""
    import numpy as np
    import pyarrow as pa

    keys, games = players["keys"], players["games"]
    if args.otp_share:
        # One-tricks: the champion makes up >= X% of the player's games in the
        # role, with a floor on champion games so tiny samples do not qualify.
        floor = args.min_champ_games or 20
        # keys are sorted, so each (puuid, role) is one run of them
        player_role = keys >> 16
        starts = np.flatnonzero(np.r_[True, player_role[1:] != player_role[:-1]])
        del player_role
        role_games = (np.repeat(np.add.reduceat(games, starts), np.diff(np.r_[starts, len(keys)]))
                      if len(keys) else games)
        share = games.astype(np.float64) / role_games.astype(np.float64)
        kept = keys[(share >= args.otp_share / 100) & (games >= floor)]
        del starts, role_games, share
        cols = {"puuid": kept >> 24, "role": kept >> 16 & 0xFF, "champion": kept & 0xFFFF}
    else:
        # Keep only games where the pilot has >= N season games on that champion.
        pairs, pair_games = _sum_by_key(keys >> 24 << 16 | keys & 0xFFFF, games)
        kept = pairs[pair_games >= args.min_champ_games]
        del pairs, pair_games
        cols = {"puuid": kept >> 16, "champion": kept & 0xFFFF}
    return pa.table({name: players["names"][name].take(pa.array(c))
                     for name, c in cols.items()})


def _release_memory():
    """Hand memory freed so far back to the OS. Arrow's allocator and glibc's
    heap otherwise keep what one step of a sync freed, and the next step's
    peak lands on top of it; called after each merge of the player counts
    and after each tier."""
    import ctypes

    import pyarrow as pa

    pa.default_memory_pool().release_unused()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass  # not glibc


def cmd_import_soloq(args, crawl=None):
    """Aggregate lol-quant's Riot-API soloq parquet into stats rows
    (patch/tier/champion/lane/bucket games+wins).

    The ~130M participant rows do not fit in memory, so the crawl is read a
    chunk of files at a time (crawl_chunks) and every count is added up
    across chunks: memory follows the distinct keys, not the rows. `crawl`,
    a dict, lets cmd_sync's tiers share one snapshot and one count of the
    players' games."""
    try:
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.dataset as pads
    except ModuleNotFoundError:
        sys.exit("pyarrow is required for import-soloq: pip install pyarrow")

    if not args.tier:
        args.tier = ("soloq_otp" if args.otp_share else
                     "soloq_mastery" if args.min_champ_games else "soloq_masters_plus")
    crawl = {} if crawl is None else crawl
    if "snapshot" not in crawl:
        crawl["snapshot"] = crawl_snapshot(args.quant_dir)
    snapshot = crawl["snapshot"]
    part_dir = snapshot[0]

    filt = pads.field("role").isin(list(SOLOQ_ROLE_MAP))
    if args.platforms:
        filt = filt & pads.field("platform").isin(args.platforms)
    columns = ["patch", "champion", "role", "win", "game_duration", "match_id"]
    keep = None
    if args.min_champ_games or args.otp_share:
        columns.append("puuid")
        if "players" not in crawl:
            crawl["players"] = _player_games(snapshot, filt)
            _release_memory()
        keep = _player_filter(crawl["players"], args)
        print(f"Read {crawl['players']['rows']:,} participant rows from {part_dir}")

    stat_keys = ["patch", "champion", "role", "bucket"]
    stats, matches, read, kept = [], [], 0, 0
    for chunk in crawl_chunks(snapshot):
        table = chunk.to_table(columns=columns, filter=filt)
        read += table.num_rows
        if keep is not None:
            table = table.join(keep, keys=keep.column_names, join_type="inner")
        kept += table.num_rows

        # Distinct matches per patch, counted after the tier filters so filtered
        # tiers (mastery/otp) report only matches that contributed rows. A
        # match can recur across chunks, so only the pairs are kept here.
        matches.append(table.group_by(["patch", "match_id"]).aggregate([]))

        # game_duration is seconds; buckets 1-7 are 0-15, 15-20, ... 40+ minutes.
        # Integer division is load-bearing here: force an integer type rather
        # than trusting the parquet schema (a float column would yield float
        # buckets, which blow up as dict keys downstream).
        dur = table["game_duration"]
        if not pa.types.is_integer(dur.type):
            dur = pc.cast(pc.floor(pc.cast(dur, pa.float64())), pa.int64())
        bucket = pc.min_element_wise(
            pc.max_element_wise(pc.subtract(pc.divide(dur, 300), 1), 1), 7)
        stats.append(pa.table({
            "patch": table["patch"], "champion": table["champion"],
            "role": table["role"], "bucket": bucket,
            "win": pc.cast(table["win"], pa.int32()),
        }).group_by(stat_keys).aggregate([("win", "sum"), ("win", "count")]))

    if args.otp_share:
        print(f"One-trick filter (>= {args.otp_share:g}% of role games, "
              f">= {args.min_champ_games or 20} champion games): kept {kept:,} of "
              f"{read:,} rows ({keep.num_rows:,} player-champion-roles)")
    elif args.min_champ_games:
        print(f"Mastery filter (>= {args.min_champ_games} games on champion): "
              f"kept {kept:,} of {read:,} rows")
    else:
        print(f"Read {read:,} participant rows from {part_dir}")

    # One thread: a hash table per thread over millions of match ids is what
    # the distinct count would otherwise cost.
    counted = pa.concat_tables(matches).group_by("patch", use_threads=False).aggregate(
        [("match_id", "count_distinct")])
    matches_by_patch = {r["patch"]: r["match_id_count_distinct"]
                        for r in counted.to_pylist()}
    grouped = pa.concat_tables(stats).group_by(stat_keys).aggregate(
        [("win_sum", "sum"), ("win_count", "sum")]).rename_columns(
        {"win_sum_sum": "win_sum", "win_count_sum": "win_count"})

    per_patch = {}
    for row in grouped.to_pylist():
        slug = row["champion"].lower()
        slug = SOLOQ_SLUG_FIXES.get(slug, slug)
        lane = SOLOQ_ROLE_MAP[row["role"]]
        buckets = per_patch.setdefault(row["patch"], {}).setdefault((slug, lane), {})
        buckets[row["bucket"]] = (row["win_count"], row["win_sum"])

    con = db_connect()
    for patch in sorted(per_patch, key=patch_key):
        champ_lanes = per_patch[patch]
        champ_games = {}
        for (slug, lane), buckets in champ_lanes.items():
            champ_games[slug] = champ_games.get(slug, 0) + sum(g for g, _ in buckets.values())
        # Keep the most-played lane plus any lane above the play-rate threshold.
        main_lane = {}
        for (slug, lane), buckets in champ_lanes.items():
            games = sum(g for g, _ in buckets.values())
            if games > main_lane.get(slug, (0, None))[0]:
                main_lane[slug] = (games, lane)
        entries = []
        for (slug, lane), buckets in sorted(champ_lanes.items()):
            rate = sum(g for g, _ in buckets.values()) / champ_games[slug] * 100
            if rate < args.min_lane_rate and lane != main_lane[slug][1]:
                continue
            entries.append({"champion": slug, "lane": lane,
                            "lane_play_rate": round(rate, 2),
                            "buckets": {str(b): {"games": g, "wins": w}
                                        for b, (g, w) in sorted(buckets.items())}})
        replace_patch_data(con, patch, args.tier, entries)
        matches = matches_by_patch.get(patch, 0)
        replace_match_count(con, patch, args.tier, matches)
        if not getattr(args, "db_only", False):
            out_dir = os.path.join(SCALING_DATA_DIR, patch, args.tier)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "champion_win_rates.json"), "w") as f:
                json.dump(entries, f, indent=2)
            with open(os.path.join(out_dir, "match_count.json"), "w") as f:
                json.dump({"matches": matches}, f)
        print(f"{patch}/{args.tier}: {len(entries)} champion-lane records "
              f"({matches:,} matches)")
    print("Done.")


def cmd_sync(args):
    """Refresh all three soloq tiers from the lol-quant crawl in one go.

    The tier definitions live here so every refresh uses the same thresholds:
    soloq_masters_plus is every game, soloq_mastery requires season games on
    the champion, soloq_otp requires the champion to dominate the player's
    role games.
    """
    presets = [
        ("soloq_masters_plus", 0, 0),
        ("soloq_mastery", args.min_champ_games, 0),
        ("soloq_otp", 0, args.otp_share),
    ]
    crawl = {}  # one snapshot, and one count of players' games, for every tier
    for tier, min_champ_games, otp_share in presets:
        print(f"=== {tier} ===")
        cmd_import_soloq(argparse.Namespace(
            quant_dir=args.quant_dir, tier=tier, platforms=args.platforms,
            min_lane_rate=10, min_champ_games=min_champ_games,
            otp_share=otp_share, db_only=args.db_only), crawl)
        _release_memory()
        print()
    # Record which regions the crawl covers; the UI shows this next to the
    # tier description. The platform partitions are directories under the
    # participants parquet root.
    part_dir = os.path.join(args.quant_dir, "data", "parquet", "participants")
    platforms = args.platforms or sorted(
        d for d in os.listdir(part_dir) if os.path.isdir(os.path.join(part_dir, d)))
    os.makedirs(SCALING_DATA_DIR, exist_ok=True)
    with open(os.path.join(SCALING_DATA_DIR, "platforms.json"), "w") as f:
        json.dump(platforms, f)
    if args.db_only:
        # The scheduled sync (web/ops/sync-scaling.sh) runs this way: lol.db
        # follows the crawl, while the committed lol/data/scaling archive is left
        # to deliberate hand-run syncs. An 8 MB rewrite four times a day
        # would only bloat the repo.
        print("Sync complete: lol.db refreshed, lol/data/scaling archive left as is.")
    else:
        print("Sync complete: commit lol/data/scaling to checkpoint the archive.")


# ---------------------------------------------------------------------------
# Web API payloads (used by web/server.py serve/export)
# ---------------------------------------------------------------------------

def build_rows(con, tier, patches, min_games=500, min_bucket_games=500):
    """Aggregated per-champion-lane payload used by the web API.

    The defaults are the web page's lowest min-games option (#mingames in
    web/static/index.html). The page filters on the ranked bucket's games,
    so the payload must carry every row and win rate that option can show."""
    agg = aggregate(con, tier, patches)
    rows = []
    for (champ, lane), buckets in sorted(agg.items()):
        total = sum(g for g, _ in buckets.values())
        if total < min_games:
            continue
        wr, games = [], []
        for b in range(1, 8):
            g, w = buckets.get(b, (0, 0))
            games.append(g)
            wr.append(round(w / g * 100, 2) if g >= min_bucket_games else None)
        rows.append({"c": champ, "l": lane, "wr": wr, "g": games, "total": total})
    ph = ",".join("?" * len(patches))
    matches = con.execute(
        f"SELECT SUM(matches) FROM match_counts WHERE tier=? AND patch IN ({ph})",
        [tier, *patches]).fetchone()[0]
    # When lol.db last pulled these patches from the crawl. The page shows
    # this, not the request date: it is the one that can go stale.
    synced = con.execute(
        f"SELECT MAX(scraped_at) FROM stats WHERE tier=? AND patch IN ({ph})",
        [tier, *patches]).fetchone()[0]
    return {
        "tier": tier,
        "patches": patches,
        "synced": synced,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "totalGames": sum(r["total"] for r in rows),
        "totalMatches": matches,
        "rows": rows,
    }


def api_meta(con):
    inv = [{"tier": t, "patch": p, "champLanes": r, "games": g, "scrapedAt": at}
           for t, p, r, g, at in con.execute(
               "SELECT tier, patch, COUNT(DISTINCT champion || '/' || lane), "
               "SUM(games), MAX(scraped_at) FROM stats GROUP BY tier, patch")]
    inv.sort(key=lambda r: (r["tier"], patch_key(r["patch"])))
    tiers = sorted({r["tier"] for r in inv})
    platforms = []
    plat_path = os.path.join(SCALING_DATA_DIR, "platforms.json")
    if os.path.exists(plat_path):
        with open(plat_path) as f:
            platforms = json.load(f)
    return {"inventory": inv, "tiers": tiers, "defaultTier": default_tier(con),
            "platforms": platforms}


# ---------------------------------------------------------------------------
# One-tricks: Master+ ladder population and per-role share counting
# ---------------------------------------------------------------------------

def _records(path):
    """A ledger day file's records. A torn tail (the crawler appends to
    today's file) costs only itself."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (EOFError, OSError, zlib.error):
        return


def latest_ladder(ladder_dir):
    """(apex puuids, unix time) at a platform's last ladder observation.

    lol-quant's ledger (sources/riot/ledger.py) is one gzip JSONL file per
    UTC day holding full frames {"t": "f", "e": [[puuid, tier, lp], ...]},
    written at least hourly, and deltas {"t": "d", "j": joins and tier
    changes, "l": leaves} in between: the state is the last frame plus the
    deltas after it. Diamond entries (--ledger-diamond1) are not members."""
    files = sorted(f for f in os.listdir(ladder_dir) if f.endswith(".jsonl.gz"))
    tail = []
    for name in reversed(files):
        records = list(_records(os.path.join(ladder_dir, name)))
        frames = [i for i, r in enumerate(records) if r.get("t") == "f"]
        if frames:
            tail = records[frames[-1]:] + tail
            break
        tail = records + tail
    else:
        return set(), None
    members = {e[0]: e[1] for e in tail[0]["e"]}
    for rec in tail[1:]:
        for entry in rec.get("j", []):
            members[entry[0]] = entry[1]
        for puuid in rec.get("l", []):
            members.pop(puuid, None)
    return {p for p, tier in members.items() if tier in APEX_TIERS}, tail[-1]["ts"]


def ladder_players(quant_dir, platforms=None):
    """Everyone on the latest ladder of each platform, the platforms read,
    and the oldest of their last observations (the ladder is as of then)."""
    root = os.path.join(quant_dir, "data", "ladder")
    if not os.path.isdir(root):
        sys.exit(f"No ladder ledger at {root}. Check --quant-dir.")
    names = platforms or sorted(
        d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    missing = [n for n in names if not os.path.isdir(os.path.join(root, n))]
    if missing:
        sys.exit(f"No ladder ledger for {', '.join(missing)} under {root}.")
    players, observed = set(), []
    for name in names:
        members, ts = latest_ladder(os.path.join(root, name))
        players |= members
        if ts is not None:
            observed.append(ts)
    return players, names, min(observed) if observed else None


def count(quant_dir, players):
    """One-trick counts per champion among `players` this season.

    Streams the participants parquet through a grouped count a chunk of
    files at a time (crawl_chunks), so neither the tens of millions
    of matching rows nor every file's parquet footer sit in memory at once.
    The season is the crawl's patches of the newest major version: the crawl
    keeps earlier seasons' games, which would otherwise mix in after a
    rollover."""
    try:
        import pyarrow as pa
        import pyarrow.acero as ac
        import pyarrow.compute as pc
    except ModuleNotFoundError:
        sys.exit("pyarrow is required for onetricks sync: pip install pyarrow")

    snapshot = crawl_snapshot(quant_dir)

    def grouped(keys, filt=None):
        total = None
        for dataset in crawl_chunks(snapshot):
            nodes = [ac.Declaration("scan", ac.ScanNodeOptions(dataset, columns=keys, filter=filt))]
            if filt is not None:  # the scan only uses its filter for pushdown
                nodes.append(ac.Declaration("filter", ac.FilterNodeOptions(filt)))
            nodes.append(ac.Declaration("aggregate", ac.AggregateNodeOptions(
                [([], "hash_count_all", None, "games")], keys=keys)))
            part = ac.Declaration.from_sequence(nodes).to_table()
            # A key can recur across chunks: add its counts up as we go.
            if total is not None:
                part = pa.concat_tables([total, part.select(total.column_names)]).group_by(
                    keys).aggregate([("games", "sum")]).rename_columns({"games_sum": "games"})
            total = part
        return total

    patches = [p for p in grouped(["patch"])["patch"].to_pylist() if p]
    season_major = max((patch_key(p)[0] for p in patches), default=None)
    season = sorted((p for p in patches if patch_key(p)[0] == season_major), key=patch_key)
    games = grouped(["puuid", "role", "champion"], (
        pc.field("puuid").isin(pa.array(sorted(players), pa.string()))
        & pc.field("role").isin(list(SOLOQ_ROLE_MAP))
        & pc.field("patch").isin(pa.array(season, pa.string()))))

    role_games = games.group_by(["puuid", "role"]).aggregate([("games", "sum")])
    joined = games.join(role_games, keys=["puuid", "role"])
    tricks = joined.filter(pc.and_(
        pc.greater(pc.multiply(joined["games"], 100),
                   pc.multiply(joined["games_sum"], MIN_SHARE_PCT)),
        pc.greater_equal(joined["games"], MIN_GAMES)))

    def slug(name):
        s = name.lower()
        return SOLOQ_SLUG_FIXES.get(s, s)

    # Every champion the ladder played this season gets a row, zeros too.
    per = {slug(c): {"players": set(), **dict.fromkeys(LANES, 0)}
           for c in pc.unique(games["champion"]).to_pylist()}
    for row in tricks.select(["puuid", "role", "champion"]).to_pylist():
        champ = per[slug(row["champion"])]
        champ["players"].add(row["puuid"])
        # Over 85% of a role's games: a player-role has at most one champion.
        champ[SOLOQ_ROLE_MAP[row["role"]]] += 1
    return {
        "champions": {c: {"players": len(v["players"]), **{l: v[l] for l in LANES}}
                      for c, v in per.items()},
        "onetrick_players": len(set(tricks["puuid"].to_pylist())),
        "games": pc.sum(games["games"]).as_py() or 0,
        "patches": season,
    }


def store(con, result, platforms, ladder_players_n, ladder_at):
    """Replace the stored counts with `result` in one transaction."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = (datetime.fromtimestamp(ladder_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
             if ladder_at is not None else None)
    patches = result["patches"]
    with con:
        con.execute("DELETE FROM onetricks")
        con.executemany(
            "INSERT INTO onetricks VALUES (?,?,?,?,?,?,?)",
            [(c, v["players"], *(v[l] for l in LANES))
             for c, v in sorted(result["champions"].items())])
        con.execute("DELETE FROM onetrick_sync")
        con.execute(
            "INSERT INTO onetrick_sync VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now, stamp, ladder_players_n, result["onetrick_players"], result["games"],
             patches[0] if patches else None, patches[-1] if patches else None,
             MIN_SHARE_PCT, MIN_GAMES, json.dumps(platforms)))


def api_onetricks(con):
    """The One-tricks tab's payload: every champion, most one-tricks first."""
    sync = con.execute(
        "SELECT synced_at, ladder_at, ladder_players, onetrick_players, games, "
        "first_patch, last_patch, min_share_pct, min_games, platforms "
        "FROM onetrick_sync").fetchone()
    if sync is None:
        return {"synced": None, "rows": []}
    rows = [{"c": c, "n": n, **dict(zip(LANES, lanes))}
            for c, n, *lanes in con.execute(
                f"SELECT champion, players, {', '.join(LANES)} FROM onetricks "
                "ORDER BY players DESC, champion")]
    keys = ("synced", "ladderAt", "ladderPlayers", "onetrickPlayers", "games",
            "firstPatch", "lastPatch", "minShare", "minGames")
    return {**dict(zip(keys, sync)), "platforms": json.loads(sync[9]), "rows": rows}


def cmd_onetricks_sync(args):
    players, platforms, ladder_at = ladder_players(args.quant_dir, args.platforms)
    if ladder_at is None:
        sys.exit("The ladder ledger has no observations yet.")
    print(f"{len(players):,} players on the latest Master+ ladders of {', '.join(platforms)}")
    result = count(args.quant_dir, players)
    store(db_connect(), result, platforms, len(players), ladder_at)
    patches = result["patches"]
    span = f"{patches[0]}-{patches[-1]}" if patches else "no patches"
    print(f"{span}: {result['games']:,} player-games, {result['onetrick_players']:,} players "
          f"one-trick a champion in a role (> {MIN_SHARE_PCT}% of the role's games, "
          f">= {MIN_GAMES} games on it)")
    top = sorted(result["champions"].items(), key=lambda kv: (-kv[1]["players"], kv[0]))
    for champ, v in top[:10]:
        print(f"  {champ:<14} {v['players']:>5}")
