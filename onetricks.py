"""One-tricks per champion, counted from the ../lol-quant soloq crawl.

A player one-tricks a champion in a role when that champion is more than 85%
of the player's ranked solo games in that role this season, with at least 20
games on it. The share is taken within the role, not over all of the
player's games as onetricks.gg does, so a one-trick who also plays other
roles still counts. The players are everyone on each crawled platform's
latest Master+ ladder observation: the crawl backfills their whole season.

Owns the `onetricks` and `onetrick_sync` tables in lol.db.
"""

import gzip
import json
import os
import sys
import zlib
from datetime import datetime, timezone

from common import db_connect, patch_key
from scaling import SOLOQ_ROLE_MAP, SOLOQ_SLUG_FIXES

MIN_SHARE_PCT = 85  # strictly more than this share of the role's games
MIN_GAMES = 20      # games on the champion in the role, as soloq_otp requires
APEX_TIERS = {"MASTER", "GRANDMASTER", "CHALLENGER"}
LANES = ("top", "jungle", "middle", "bottom", "support")


# ---------------------------------------------------------------------------
# Who: the latest Master+ ladder, from the crawl's ladder ledger
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
        sys.exit(f"No ladder ledger at {root} — check --quant-dir.")
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


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

def count(quant_dir, players):
    """One-trick counts per champion among `players` this season.

    Streams the participants parquet through a grouped count, so the tens of
    millions of matching rows never sit in memory at once. The season is the
    crawl's patches of the newest major version: the crawl keeps earlier
    seasons' games, which would otherwise mix in after a rollover."""
    try:
        import pyarrow as pa
        import pyarrow.acero as ac
        import pyarrow.compute as pc
        import pyarrow.dataset as pads
    except ModuleNotFoundError:
        sys.exit("pyarrow is required for onetricks sync: .venv/bin/pip install pyarrow")

    part_dir = os.path.join(quant_dir, "data", "parquet", "participants")
    if not os.path.isdir(part_dir):
        sys.exit(f"No participants parquet at {part_dir} — check --quant-dir.")
    dataset = pads.dataset(part_dir, format="parquet")

    def grouped(keys, filt=None):
        nodes = [ac.Declaration("scan", ac.ScanNodeOptions(dataset, columns=keys, filter=filt))]
        if filt is not None:  # the scan only uses its filter for pushdown
            nodes.append(ac.Declaration("filter", ac.FilterNodeOptions(filt)))
        nodes.append(ac.Declaration("aggregate", ac.AggregateNodeOptions(
            [([], "hash_count_all", None, "games")], keys=keys)))
        return ac.Declaration.from_sequence(nodes).to_table()

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


# ---------------------------------------------------------------------------
# lol.db
# ---------------------------------------------------------------------------

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


def cmd_sync(args):
    players, platforms, ladder_at = ladder_players(args.quant_dir, args.platforms)
    if ladder_at is None:
        sys.exit("The ladder ledger has no observations yet.")
    print(f"{len(players):,} players on the latest Master+ ladders of {', '.join(platforms)}")
    result = count(args.quant_dir, players)
    store(db_connect(), result, platforms, len(players), ladder_at)
    patches = result["patches"]
    span = f"{patches[0]}–{patches[-1]}" if patches else "no patches"
    print(f"{span}: {result['games']:,} player-games, {result['onetrick_players']:,} players "
          f"one-trick a champion in a role (> {MIN_SHARE_PCT}% of the role's games, "
          f">= {MIN_GAMES} games on it)")
    top = sorted(result["champions"].items(), key=lambda kv: (-kv[1]["players"], kv[0]))
    for champ, v in top[:10]:
        print(f"  {champ:<14} {v['players']:>5}")
