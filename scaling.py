"""Champion scaling analysis: win rates by game length from the ../lol-quant
soloq crawl.

Owns the `stats` table in lol.db and the data/scaling/ JSON archive. The
per-champion-lane rows track every lane above 10% play rate, bucketed into
seven game-length intervals (0-15 ... 40+ minutes).
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from common import (BASE_DIR, DATA_DIR, DB_PATH, db_connect, patch_key,
                    write_csv)

SCALING_DATA_DIR = os.path.join(DATA_DIR, "scaling")

BUCKET_LABELS = {
    1: "0-15 min", 2: "15-20 min", 3: "20-25 min", 4: "25-30 min",
    5: "30-35 min", 6: "35-40 min", 7: "40+ min",
}
EARLY_BUCKETS = (1, 2)   # 0-20 min
LATE_BUCKETS = (6, 7)    # 35+ min


# ---------------------------------------------------------------------------
# stats table access
# ---------------------------------------------------------------------------

def db_patches(con, tier):
    rows = con.execute("SELECT DISTINCT patch FROM stats WHERE tier=?", (tier,)).fetchall()
    return sorted((r[0] for r in rows), key=patch_key)


def default_tier(con):
    """soloq_masters_plus when present (the primary tier — every game);
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
        sys.exit("Database is empty — run `lol.py scaling sync` or `lol.py import-json` first.")
    patches = getattr(args, "patches", None) or db_patches(con, tier)
    if not patches:
        sys.exit(f"No data for tier '{tier}'. Available: run `lol.py status`.")
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
        print(f"\n=== Scaling: late-game WR (35+ min) minus early WR (0-20 min) ===")
        print(f"{'Rank':<5} {'Champion':<25} {'Early WR':<10} {'Late WR':<10} {'Delta':<8} {'Games':<10}")
        print("-" * 70)
        shown = rows[:args.top] + ([] if len(rows) <= args.top else rows[-args.top:])
        for rank, (name, ewr, lwr, d, total) in enumerate(shown, 1):
            marker = rank if rank <= args.top else len(rows) - (len(shown) - rank)
            print(f"{marker:<5} {name:<25} {ewr:>6.2f}%   {lwr:>6.2f}%   {d:>+6.2f}  {total:>9,}")
            if rank == args.top and len(rows) > args.top:
                print(f"{'...':<5} (bottom {args.top} — early-game champions)")
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
            cells.append(f"{w / g * 100:>6.2f}% ({g:>8,})" if g else f"{'—':>18}")
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
# Imports (data/ archive and the lol-quant crawl)
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
# Riot internal names that don't lowercase to the champion's usual slug.
SOLOQ_SLUG_FIXES = {"monkeyking": "wukong"}


def cmd_import_soloq(args):
    """Aggregate lol-quant's Riot-API soloq parquet into stats rows
    (patch/tier/champion/lane/bucket games+wins)."""
    try:
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.dataset as pads
    except ModuleNotFoundError:
        sys.exit("pyarrow is required for import-soloq: .venv/bin/pip install pyarrow")

    if not args.tier:
        args.tier = ("soloq_otp" if args.otp_share else
                     "soloq_mastery" if args.min_champ_games else "soloq_masters_plus")
    part_dir = os.path.join(args.quant_dir, "data", "parquet", "participants")
    if not os.path.isdir(part_dir):
        sys.exit(f"No participants parquet at {part_dir} — check --quant-dir.")

    filt = pads.field("role").isin(list(SOLOQ_ROLE_MAP))
    if args.platforms:
        filt = filt & pads.field("platform").isin(args.platforms)
    columns = ["patch", "champion", "role", "win", "game_duration", "match_id"]
    if args.min_champ_games or args.otp_share:
        columns.append("puuid")
    table = pads.dataset(part_dir, format="parquet").to_table(
        columns=columns, filter=filt)
    print(f"Read {table.num_rows:,} participant rows from {part_dir}")

    if args.otp_share:
        # One-tricks: the champion makes up >= X% of the player's games in the
        # role, with a floor on champion games so tiny samples don't qualify.
        floor = args.min_champ_games or 20
        champ_ct = table.select(["puuid", "role", "champion"]).group_by(
            ["puuid", "role", "champion"]).aggregate([([], "count_all")])
        role_tot = table.select(["puuid", "role"]).group_by(
            ["puuid", "role"]).aggregate([([], "count_all")])
        joined = champ_ct.join(role_tot, keys=["puuid", "role"],
                               join_type="inner", right_suffix="_role")
        share = pc.divide(pc.cast(joined["count_all"], pa.float64()),
                          pc.cast(joined["count_all_role"], pa.float64()))
        mask = pc.and_(pc.greater_equal(share, args.otp_share / 100),
                       pc.greater_equal(joined["count_all"], floor))
        keep = joined.filter(mask).select(["puuid", "role", "champion"])
        before = table.num_rows
        table = table.join(keep, keys=["puuid", "role", "champion"], join_type="inner")
        print(f"One-trick filter (>= {args.otp_share:g}% of role games, "
              f">= {floor} champion games): kept {table.num_rows:,} of {before:,} rows "
              f"({len(keep):,} player-champion-roles)")
    elif args.min_champ_games:
        # Keep only games where the pilot has >= N season games on that champion.
        counts = table.select(["puuid", "champion"]).group_by(
            ["puuid", "champion"]).aggregate([([], "count_all")])
        counts = counts.filter(
            pc.field("count_all") >= args.min_champ_games).drop_columns(["count_all"])
        before = table.num_rows
        table = table.join(counts, keys=["puuid", "champion"], join_type="inner")
        print(f"Mastery filter (>= {args.min_champ_games} games on champion): "
              f"kept {table.num_rows:,} of {before:,} rows")

    # Distinct matches per patch, counted after the tier filters so filtered
    # tiers (mastery/otp) report only matches that contributed rows.
    counted = table.select(["patch", "match_id"]).group_by("patch").aggregate(
        [("match_id", "count_distinct")])
    matches_by_patch = {r["patch"]: r["match_id_count_distinct"]
                        for r in counted.to_pylist()}
    table = table.drop_columns(["match_id"])

    # game_duration is seconds; buckets 1-7 are 0-15, 15-20, ... 40+ minutes.
    # Integer division is load-bearing here — force an integer type rather
    # than trusting the parquet schema (a float column would yield float
    # buckets, which blow up as dict keys downstream).
    dur = table["game_duration"]
    if not pa.types.is_integer(dur.type):
        dur = pc.cast(pc.floor(pc.cast(dur, pa.float64())), pa.int64())
    bucket = pc.min_element_wise(
        pc.max_element_wise(pc.subtract(pc.divide(dur, 300), 1), 1), 7)
    grouped = pa.table({
        "patch": table["patch"], "champion": table["champion"],
        "role": table["role"], "bucket": bucket,
        "win": pc.cast(table["win"], pa.int32()),
    }).group_by(["patch", "champion", "role", "bucket"]).aggregate(
        [("win", "sum"), ("win", "count")])

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
    for tier, min_champ_games, otp_share in presets:
        print(f"=== {tier} ===")
        cmd_import_soloq(argparse.Namespace(
            quant_dir=args.quant_dir, tier=tier, platforms=args.platforms,
            min_lane_rate=10, min_champ_games=min_champ_games,
            otp_share=otp_share))
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
    print("Sync complete — commit and push data/ to update the published site.")


# ---------------------------------------------------------------------------
# Web API payloads (used by webapp.py serve/export and the legacy dashboard)
# ---------------------------------------------------------------------------

def build_rows(con, tier, patches, min_games=1000, min_bucket_games=1000):
    """Aggregated per-champion-lane payload used by the dashboard and the web API."""
    agg = aggregate(con, tier, patches)
    rows = []
    for (champ, lane), buckets in sorted(agg.items()):
        total = sum(g for g, _ in buckets.values())
        if total < min_games:
            continue
        early_wr, early_g = phase_wr(buckets, EARLY_BUCKETS)
        late_wr, late_g = phase_wr(buckets, LATE_BUCKETS)
        wr, games = [], []
        for b in range(1, 8):
            g, w = buckets.get(b, (0, 0))
            games.append(g)
            wr.append(round(w / g * 100, 2) if g >= min_bucket_games else None)
        rows.append({
            "c": champ, "l": lane, "wr": wr, "g": games, "total": total,
            "early": round(early_wr, 2) if early_wr is not None else None,
            "late": round(late_wr, 2) if late_wr is not None else None,
            "delta": round(late_wr - early_wr, 2)
                     if early_wr is not None and late_wr is not None else None,
        })
    ph = ",".join("?" * len(patches))
    matches = con.execute(
        f"SELECT SUM(matches) FROM match_counts WHERE tier=? AND patch IN ({ph})",
        [tier, *patches]).fetchone()[0]
    return {
        "tier": tier,
        "patches": patches,
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


def api_champion(con, tier, name):
    patches = db_patches(con, tier)
    ph = ",".join("?" * len(patches))
    lanes = {}
    for lane, bucket, games, wins in con.execute(
            f"SELECT lane, bucket, SUM(games), SUM(wins) FROM stats "
            f"WHERE tier=? AND champion=? AND patch IN ({ph}) GROUP BY lane, bucket",
            [tier, name, *patches]):
        d = lanes.setdefault(lane, {"wr": [None] * 7, "g": [0] * 7})
        d["g"][bucket - 1] = games
        # 1000 games ~= +/-3pp at 95%; below that a chart point is mostly noise.
        d["wr"][bucket - 1] = round(wins / games * 100, 2) if games >= 1000 else None
    per_patch = {}
    for lane, patch, games, wins in con.execute(
            f"SELECT lane, patch, SUM(games), SUM(wins) FROM stats "
            f"WHERE tier=? AND champion=? AND patch IN ({ph}) GROUP BY lane, patch",
            [tier, name, *patches]):
        per_patch.setdefault(lane, {})[patch] = {
            "wr": round(wins / games * 100, 2) if games else None, "g": games}
    return {"name": name, "tier": tier, "patches": patches,
            "lanes": lanes, "perPatch": per_patch}


def cmd_dashboard(args):
    con = db_connect()
    tier, patches = resolve_slice(con, args)
    payload = build_rows(con, tier, patches, args.min_games, args.min_bucket_games)
    html = DASHBOARD_TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    with open(args.out, "w") as f:
        f.write(html)
    print(f"Wrote {args.out} ({len(payload['rows'])} champion-roles, tier {tier}, "
          f"patches {patches[0]}-{patches[-1]}). Open it in a browser.")


DASHBOARD_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LoL Scaling Dashboard</title>
<style>
  :root {
    --surface-1: #fcfcfb; --page: #f9f9f7;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
    --grid: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
    --s1:#2a78d6; --s2:#1baf7a; --s3:#eda100; --s4:#008300;
    --s5:#4a3aa7; --s6:#e34948; --s7:#e87ba4; --s8:#eb6834;
    --up: #006300; --down: #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --surface-1: #1a1a19; --page: #0d0d0d;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
      --s1:#3987e5; --s2:#199e70; --s3:#c98500; --s4:#008300;
      --s5:#9085e9; --s6:#e66767; --s7:#d55181; --s8:#d95926;
      --up: #0ca30c; --down: #d03b3b;
    }
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--page); color: var(--text-primary);
    padding: 24px; max-width: 1200px; margin: 0 auto;
  }
  h1 { font-size: 20px; font-weight: 600; }
  .sub { color: var(--text-secondary); font-size: 13px; margin-top: 4px; }
  .kpis { display: flex; gap: 12px; margin: 20px 0; flex-wrap: wrap; }
  .tile {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
    padding: 12px 16px; min-width: 150px; flex: 1;
  }
  .tile .label { font-size: 12px; color: var(--text-secondary); }
  .tile .value { font-size: 26px; font-weight: 600; margin-top: 2px; }
  .controls { display: flex; gap: 10px; margin: 0 0 16px; flex-wrap: wrap; align-items: center; }
  .controls input, .controls select {
    font: inherit; font-size: 13px; color: var(--text-primary);
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 10px;
  }
  .controls input { width: 200px; }
  .card {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
    padding: 16px; margin-bottom: 16px;
  }
  .card h2 { font-size: 14px; font-weight: 600; margin-bottom: 2px; }
  .card .hint { font-size: 12px; color: var(--muted); margin-bottom: 10px; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; margin: 10px 2px 0; font-size: 12px; color: var(--text-secondary); }
  .legend .key { display: inline-flex; align-items: center; gap: 6px; }
  .legend .swatch { width: 14px; height: 2px; border-radius: 1px; }
  #chartwrap { position: relative; }
  #tooltip {
    position: absolute; pointer-events: none; display: none; z-index: 5;
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.12); padding: 8px 10px; font-size: 12px;
    min-width: 170px;
  }
  #tooltip .tt-title { color: var(--muted); margin-bottom: 6px; }
  #tooltip .tt-row { display: flex; align-items: center; gap: 6px; margin-top: 3px; }
  #tooltip .tt-key { width: 12px; height: 2px; border-radius: 1px; flex: none; }
  #tooltip .tt-val { font-weight: 600; font-variant-numeric: tabular-nums; }
  #tooltip .tt-name { color: var(--text-secondary); }
  #tooltip .tt-games { color: var(--muted); margin-left: auto; font-variant-numeric: tabular-nums; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--grid); }
  th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
  td { font-variant-numeric: tabular-nums; }
  th {
    color: var(--text-secondary); font-weight: 500; cursor: pointer; user-select: none;
    white-space: nowrap; position: sticky; top: 0; background: var(--surface-1);
  }
  th .arrow { color: var(--muted); font-size: 10px; }
  tbody tr { cursor: pointer; }
  tbody tr:hover { background: color-mix(in srgb, var(--text-primary) 4%, transparent); }
  tbody tr.sel td:first-child { font-weight: 600; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 8px; vertical-align: baseline; }
  .dot.off { background: transparent; border: 1px solid var(--baseline); width: 6px; height: 6px; }
  .pos { color: var(--up); } .neg { color: var(--down); }
  .tablewrap { max-height: 480px; overflow-y: auto; overflow-x: auto; }
  .lane-tag { color: var(--text-secondary); }
  svg text { font-family: inherit; }
</style>
</head>
<body>
<h1>LoL Scaling Dashboard</h1>
<div class="sub" id="subtitle"></div>

<div class="kpis">
  <div class="tile"><div class="label">Champion-roles</div><div class="value" id="kpi-roles"></div></div>
  <div class="tile"><div class="label">Games analyzed</div><div class="value" id="kpi-games"></div></div>
  <div class="tile"><div class="label">Patches</div><div class="value" id="kpi-patches"></div></div>
  <div class="tile"><div class="label">Biggest scaler</div><div class="value" id="kpi-scaler" style="font-size:18px"></div></div>
</div>

<div class="controls">
  <input id="search" type="search" placeholder="Search champion..." aria-label="Search champion">
  <select id="lane" aria-label="Lane filter">
    <option value="">All lanes</option>
    <option>top</option><option>jungle</option><option>middle</option>
    <option>bottom</option><option>support</option>
  </select>
  <select id="mingames" aria-label="Minimum games">
    <option value="1000">≥ 1,000 games</option>
    <option value="5000" selected>≥ 5,000 games</option>
    <option value="20000">≥ 20,000 games</option>
    <option value="50000">≥ 50,000 games</option>
  </select>
</div>

<div class="card">
  <h2>Win rate by game length</h2>
  <div class="hint">Click table rows to add or remove champions (up to 8).</div>
  <div id="chartwrap">
    <svg id="chart" width="100%" height="340" role="img" aria-label="Win rate by game length line chart"></svg>
    <div id="tooltip"></div>
  </div>
  <div class="legend" id="legend"></div>
</div>

<div class="card">
  <h2>Champions</h2>
  <div class="hint">Scaling Δ = win rate in 35+ min games minus win rate in games under 20 min. Click a column to sort.</div>
  <div class="tablewrap">
    <table id="tbl">
      <thead><tr>
        <th data-k="c">Champion <span class="arrow"></span></th>
        <th data-k="l">Lane <span class="arrow"></span></th>
        <th data-k="early">Early WR <span class="arrow"></span></th>
        <th data-k="late">Late WR <span class="arrow"></span></th>
        <th data-k="wr40">40+ WR <span class="arrow"></span></th>
        <th data-k="delta">Scaling Δ <span class="arrow"></span></th>
        <th data-k="total">Games <span class="arrow"></span></th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<script>
const DATA = __DATA__;
const BUCKETS = ["0–15", "15–20", "20–25", "25–30", "30–35", "35–40", "40+"];
const SLOTS = ["--s1","--s2","--s3","--s4","--s5","--s6","--s7","--s8"];
const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const fmtInt = n => n.toLocaleString("en-US");
const fmtCompact = n => n >= 1e6 ? (n/1e6).toFixed(1) + "M" : n >= 1e3 ? (n/1e3).toFixed(0) + "K" : "" + n;
const rid = r => r.c + "/" + r.l;

document.getElementById("subtitle").textContent =
  `Tier: ${DATA.tier} · Patches ${DATA.patches[0]}–${DATA.patches[DATA.patches.length-1]} · generated ${DATA.generated}`;
document.getElementById("kpi-roles").textContent = fmtInt(DATA.rows.length);
document.getElementById("kpi-games").textContent = fmtCompact(DATA.totalGames);
document.getElementById("kpi-patches").textContent = DATA.patches.length;
const best = DATA.rows.filter(r => r.delta != null && r.total > 20000)
                      .reduce((a, b) => (a && a.delta > b.delta ? a : b), null);
document.getElementById("kpi-scaler").textContent = best ? `${best.c} (${best.l}) +${best.delta.toFixed(1)}` : "—";

// ----- selection state: color follows the entity while selected -----
const selected = new Map();   // rid -> slot index
function freeSlot() { const used = new Set(selected.values()); return SLOTS.findIndex((_, i) => !used.has(i)); }
function toggle(r) {
  const id = rid(r);
  if (selected.has(id)) selected.delete(id);
  else { const s = freeSlot(); if (s === -1) return; selected.set(id, s); }
  render();
}

// ----- table -----
let sortKey = "delta", sortDir = -1;
const tbody = document.querySelector("#tbl tbody");
function visibleRows() {
  const q = document.getElementById("search").value.trim().toLowerCase();
  const lane = document.getElementById("lane").value;
  const min = +document.getElementById("mingames").value;
  return DATA.rows.filter(r =>
    r.total >= min && (!lane || r.l === lane) && (!q || r.c.includes(q)));
}
function renderTable() {
  const val = r => sortKey === "wr40" ? r.wr[6] : r[sortKey];
  const rows = visibleRows().slice().sort((a, b) => {
    const va = val(a), vb = val(b);
    if (va == null) return 1; if (vb == null) return -1;
    return (va < vb ? -1 : va > vb ? 1 : 0) * sortDir;
  });
  tbody.replaceChildren(...rows.map(r => {
    const tr = document.createElement("tr");
    const id = rid(r);
    if (selected.has(id)) tr.className = "sel";
    const dot = document.createElement("span");
    dot.className = "dot" + (selected.has(id) ? "" : " off");
    if (selected.has(id)) dot.style.background = css(SLOTS[selected.get(id)]);
    const tdName = document.createElement("td");
    tdName.append(dot, document.createTextNode(r.c));
    const tds = [
      tdName,
      cell(r.l, "lane-tag"),
      cell(r.early == null ? "—" : r.early.toFixed(2) + "%"),
      cell(r.late == null ? "—" : r.late.toFixed(2) + "%"),
      cell(r.wr[6] == null ? "—" : r.wr[6].toFixed(2) + "%"),
      cell(r.delta == null ? "—" : (r.delta > 0 ? "+" : "") + r.delta.toFixed(2),
           r.delta > 0 ? "pos" : "neg"),
      cell(fmtInt(r.total)),
    ];
    tr.append(...tds);
    tr.addEventListener("click", () => toggle(r));
    return tr;
  }));
  document.querySelectorAll("#tbl th").forEach(th => {
    th.querySelector(".arrow").textContent =
      th.dataset.k === sortKey ? (sortDir === 1 ? "▲" : "▼") : "";
  });
}
function cell(text, cls) {
  const td = document.createElement("td");
  td.textContent = text;
  if (cls) td.className = cls;
  return td;
}
document.querySelectorAll("#tbl th").forEach(th =>
  th.addEventListener("click", () => {
    const k = th.dataset.k;
    if (sortKey === k) sortDir = -sortDir;
    else { sortKey = k; sortDir = k === "c" || k === "l" ? 1 : -1; }
    renderTable();
  }));
["search", "lane", "mingames"].forEach(id =>
  document.getElementById(id).addEventListener("input", renderTable));

// ----- chart -----
const svg = document.getElementById("chart");
const wrap = document.getElementById("chartwrap");
const tooltip = document.getElementById("tooltip");
const M = { top: 16, right: 24, bottom: 28, left: 44 };

function chartData() {
  return [...selected.entries()].map(([id, slot]) => {
    const r = DATA.rows.find(x => rid(x) === id);
    return r ? { r, slot } : null;
  }).filter(Boolean);
}

function render() { renderTable(); renderChart(); }

function renderChart() {
  const series = chartData();
  const W = svg.clientWidth, H = 340;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const iw = W - M.left - M.right, ih = H - M.top - M.bottom;
  const xs = i => M.left + iw * i / 6;

  let lo = 46, hi = 56;
  const vals = series.flatMap(s => s.r.wr.filter(v => v != null));
  if (vals.length) {
    lo = Math.floor(Math.min(...vals, 49) - 1);
    hi = Math.ceil(Math.max(...vals, 51) + 1);
  }
  const ys = v => M.top + ih * (1 - (v - lo) / (hi - lo));

  const ns = "http://www.w3.org/2000/svg";
  const el = (tag, attrs) => {
    const e = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  };
  svg.replaceChildren();

  // gridlines + y ticks (clean integers)
  const step = (hi - lo) <= 8 ? 2 : (hi - lo) <= 16 ? 4 : 5;
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    svg.append(el("line", { x1: M.left, x2: W - M.right, y1: ys(v), y2: ys(v),
                            stroke: css("--grid"), "stroke-width": 1 }));
    const t = el("text", { x: M.left - 8, y: ys(v) + 4, "text-anchor": "end",
                           fill: css("--muted"), "font-size": 11 });
    t.textContent = v + "%";
    svg.append(t);
  }
  // 50% baseline slightly stronger
  if (lo <= 50 && 50 <= hi)
    svg.append(el("line", { x1: M.left, x2: W - M.right, y1: ys(50), y2: ys(50),
                            stroke: css("--baseline"), "stroke-width": 1 }));
  // x labels
  BUCKETS.forEach((b, i) => {
    const t = el("text", { x: xs(i), y: H - 8, "text-anchor": "middle",
                           fill: css("--muted"), "font-size": 11 });
    t.textContent = b;
    svg.append(t);
  });

  for (const { r, slot } of series) {
    const color = css(SLOTS[slot]);
    const pts = r.wr.map((v, i) => v == null ? null : [xs(i), ys(v)]);
    let d = "", started = false;
    pts.forEach(p => {
      if (!p) { started = false; return; }
      d += (started ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1);
      started = true;
    });
    svg.append(el("path", { d, fill: "none", stroke: color, "stroke-width": 2,
                            "stroke-linecap": "round", "stroke-linejoin": "round" }));
    pts.forEach(p => {
      if (!p) return;
      svg.append(el("circle", { cx: p[0], cy: p[1], r: 5.5, fill: css("--surface-1") }));
      svg.append(el("circle", { cx: p[0], cy: p[1], r: 4, fill: color }));
    });
  }

  // legend (always present for >=2 series; also fine for 1)
  const legend = document.getElementById("legend");
  legend.replaceChildren(...series.map(({ r, slot }) => {
    const k = document.createElement("span");
    k.className = "key";
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = css(SLOTS[slot]);
    k.append(sw, document.createTextNode(`${r.c} (${r.l})`));
    return k;
  }));
  if (!series.length) {
    const t = el("text", { x: W / 2, y: H / 2, "text-anchor": "middle",
                           fill: css("--muted"), "font-size": 13 });
    t.textContent = "Select champions from the table below";
    svg.append(t);
  }

  // crosshair + unified tooltip
  svg.onpointermove = ev => {
    const series = chartData();
    if (!series.length) return;
    const rect = svg.getBoundingClientRect();
    const x = ev.clientX - rect.left;
    const i = Math.max(0, Math.min(6, Math.round((x - M.left) / (iw / 6))));
    svg.querySelector(".xhair")?.remove();
    svg.append(el("line", { class: "xhair", x1: xs(i), x2: xs(i), y1: M.top, y2: H - M.bottom,
                            stroke: css("--baseline"), "stroke-width": 1 }));
    tooltip.replaceChildren();
    const title = document.createElement("div");
    title.className = "tt-title";
    title.textContent = BUCKETS[i] + " min";
    tooltip.append(title);
    series.slice().sort((a, b) => (b.r.wr[i] ?? -1) - (a.r.wr[i] ?? -1)).forEach(({ r, slot }) => {
      const row = document.createElement("div");
      row.className = "tt-row";
      const key = document.createElement("span");
      key.className = "tt-key";
      key.style.background = css(SLOTS[slot]);
      const val = document.createElement("span");
      val.className = "tt-val";
      val.textContent = r.wr[i] == null ? "—" : r.wr[i].toFixed(1) + "%";
      const name = document.createElement("span");
      name.className = "tt-name";
      name.textContent = r.c;
      const games = document.createElement("span");
      games.className = "tt-games";
      games.textContent = fmtCompact(r.g[i]);
      row.append(key, val, name, games);
      tooltip.append(row);
    });
    tooltip.style.display = "block";
    const tw = tooltip.offsetWidth;
    tooltip.style.left = Math.min(xs(i) + 12, W - tw - 8) + "px";
    tooltip.style.top = M.top + 8 + "px";
  };
  svg.onpointerleave = () => {
    tooltip.style.display = "none";
    svg.querySelector(".xhair")?.remove();
  };
}

// preselect top 5 scalers with meaningful volume
DATA.rows.filter(r => r.delta != null && r.total > 20000)
  .sort((a, b) => b.delta - a.delta).slice(0, 5).forEach(toggle);
window.addEventListener("resize", renderChart);
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
render();
</script>
</body>
</html>
"""
