"""Shared infrastructure for the web app and database: SQLite access, patch
ordering, paths, and small helpers.

Every SQLite-backed domain stores its tables in lol.db (defined in SCHEMA so a
fresh DB always has the full shape) and keeps its committed JSON archive under
its game directory (lol/data/ or tft/data/).
"""

import csv
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "lol.db")
STATIC_DIR = os.path.join(BASE_DIR, "web", "static")
OPS_DIR = os.path.join(BASE_DIR, "web", "ops")
OPS_STATE_DIR = os.path.join(BASE_DIR, ".cache", "ops")

# Full SQLite schema across domains (`stats` and `match_counts` for scaling,
# `onetricks` and `onetrick_sync` for one-trick tracking).
SCHEMA = """
CREATE TABLE IF NOT EXISTS stats (
  patch TEXT NOT NULL,
  tier TEXT NOT NULL,
  champion TEXT NOT NULL,
  lane TEXT NOT NULL,
  bucket INTEGER NOT NULL,
  games INTEGER NOT NULL,
  wins INTEGER NOT NULL,
  lane_play_rate REAL,
  scraped_at TEXT NOT NULL,
  PRIMARY KEY (patch, tier, champion, lane, bucket)
);
CREATE INDEX IF NOT EXISTS idx_stats_tier_patch ON stats (tier, patch);
CREATE TABLE IF NOT EXISTS match_counts (
  patch TEXT NOT NULL,
  tier TEXT NOT NULL,
  matches INTEGER NOT NULL,
  scraped_at TEXT NOT NULL,
  PRIMARY KEY (patch, tier)
);
CREATE TABLE IF NOT EXISTS onetricks (
  champion TEXT PRIMARY KEY,
  players INTEGER NOT NULL,
  top INTEGER NOT NULL,
  jungle INTEGER NOT NULL,
  middle INTEGER NOT NULL,
  bottom INTEGER NOT NULL,
  support INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS onetrick_sync (
  synced_at TEXT NOT NULL,
  ladder_at TEXT,
  ladder_players INTEGER NOT NULL,
  onetrick_players INTEGER NOT NULL,
  games INTEGER NOT NULL,
  first_patch TEXT,
  last_patch TEXT,
  min_share_pct INTEGER NOT NULL,
  min_games INTEGER NOT NULL,
  platforms TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matchups_snapshot (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  synced_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
"""


def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def patch_key(patch):
    try:
        return tuple(int(x) for x in patch.split("."))
    except ValueError:
        return (0,)


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {path}")
