"""Shared infrastructure for all analysis domains: SQLite access, patch
ordering, and small helpers.

Every domain stores its tables in the one lol.db (add new tables to SCHEMA
below so a fresh DB always has the full shape) and keeps its committed JSON
archive under data/<domain>/.
"""

import csv
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "lol.db")
DATA_DIR = os.path.join(BASE_DIR, "data")
WEB_DIR = os.path.join(BASE_DIR, "web")

# The whole database, all domains. `stats` belongs to scaling.py.
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
