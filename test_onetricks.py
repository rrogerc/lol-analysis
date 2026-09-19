"""One-tricks: the ladder population, the per-role share rule and the payload."""
import argparse
import gzip
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

import common
import onetricks


def write_ledger(ladder_dir, day, records):
    os.makedirs(ladder_dir, exist_ok=True)
    with gzip.open(os.path.join(ladder_dir, f"{day}.jsonl.gz"), "at") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def games(puuid, role, champion, n, patch="16.1"):
    return [(puuid, role, champion, patch)] * n


# Each player isolates one edge of the rule.
PARTICIPANTS = {
    "kr": [
        # 20/23 = 87% of mid: an Ahri one-trick. The Zed games also carry the
        # latest patch, which sorts after 16.2 only numerically.
        *games("mid", "MIDDLE", "Ahri", 20), *games("mid", "MIDDLE", "Zed", 3, "16.10"),
        # Exactly 85% is not over 85%; 35 of 41 (85.4%) is.
        *games("exact", "UTILITY", "Lux", 34), *games("exact", "UTILITY", "Nami", 6),
        *games("over", "UTILITY", "Lux", 35), *games("over", "UTILITY", "Nami", 6),
        # Every top game on Garen, but only 19 of them.
        *games("floor", "TOP", "Garen", 19),
        # Jhin is every bottom game but 25 of 55 overall: the role decides.
        *games("role", "BOTTOM", "Jhin", 25), *games("role", "MIDDLE", "Ahri", 10),
        *games("role", "MIDDLE", "Zed", 10), *games("role", "MIDDLE", "Syndra", 10),
        # Unassigned-role games never count.
        *games("norole", "", "Teemo", 30),
    ],
    "na1": [
        # One champion in two roles: one player, in both role columns.
        *games("two", "TOP", "Pantheon", 20), *games("two", "UTILITY", "Pantheon", 20),
        # Not on any ladder.
        *games("off", "MIDDLE", "Katarina", 30),
        # Last season's games are out; 5 this season is under the floor.
        *games("old", "MIDDLE", "Yasuo", 30, "15.24"), *games("old", "MIDDLE", "Yone", 5, "16.2"),
        # Riot's internal name for Wukong.
        *games("wukong", "JUNGLE", "MonkeyKing", 22),
    ],
}
LADDER = {
    "kr": ["mid", "exact", "over", "floor", "role", "norole", "two"],
    "na1": ["two", "old", "wukong"],
}
LADDER_TS = 1789792374  # 2026-09-19T04:32:54Z


def make_crawl(root):
    for platform, rows in PARTICIPANTS.items():
        out = os.path.join(root, "data", "parquet", "participants", platform)
        os.makedirs(out)
        pq.write_table(pa.table(dict(zip(("puuid", "role", "champion", "patch"),
                                         map(list, zip(*rows))))),
                       os.path.join(out, "participants-1.parquet"))
    for platform, members in LADDER.items():
        write_ledger(os.path.join(root, "data", "ladder", platform), "2026-09-19",
                     [{"ts": LADDER_TS, "t": "f", "e": [[p, "MASTER", 400] for p in members]}])


class TestLatestLadder(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def test_last_frame_plus_later_deltas(self):
        write_ledger(self.dir, "2026-09-01", [
            {"ts": 100, "t": "f", "e": [["a", "MASTER", 1], ["gone", "MASTER", 1]]},
            {"ts": 110, "t": "d", "l": ["gone"]},
            {"ts": 200, "t": "f", "e": [["a", "MASTER", 1], ["b", "GRANDMASTER", 900],
                                         ["c", "CHALLENGER", 1500], ["d", "MASTER", 5]]},
        ])
        # The newest day has no frame yet: it continues the previous day's
        # last frame. A demotion to Diamond I (--ledger-diamond1) is a leave.
        write_ledger(self.dir, "2026-09-02", [
            {"ts": 300, "t": "d", "j": [["e", "MASTER", 10], ["a", "DIAMOND", 90]], "l": ["b"]},
            {"ts": 310, "t": "d"},
        ])
        self.assertEqual(onetricks.latest_ladder(self.dir), ({"c", "d", "e"}, 310))

    def test_torn_tail_keeps_the_readable_prefix(self):
        write_ledger(self.dir, "2026-09-02", [
            {"ts": 300, "t": "f", "e": [["a", "MASTER", 1]]},
            {"ts": 310, "t": "d", "j": [["b", "MASTER", 1]]},
        ])
        # The crawler dies mid-append: half a gzip member, cutting its record.
        leaves = ["a", *(f"x{i}" for i in range(200))]
        member = gzip.compress(json.dumps({"ts": 320, "t": "d", "l": leaves}).encode() + b"\n")
        with open(os.path.join(self.dir, "2026-09-02.jsonl.gz"), "ab") as f:
            f.write(member[:len(member) // 2])
        self.assertEqual(onetricks.latest_ladder(self.dir), ({"a", "b"}, 310))

    def test_no_observations(self):
        self.assertEqual(onetricks.latest_ladder(self.dir), (set(), None))
        write_ledger(self.dir, "2026-09-02", [{"ts": 310, "t": "d", "l": ["a"]}])
        self.assertEqual(onetricks.latest_ladder(self.dir), (set(), None))


class TestCount(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        make_crawl(self.root)

    def result(self):
        players, platforms, ladder_at = onetricks.ladder_players(self.root)
        self.assertEqual((platforms, ladder_at), (["kr", "na1"], LADDER_TS))
        self.assertEqual(len(players), 9, "a player on two ladders is one player")
        return onetricks.count(self.root, players)

    def test_role_share_rule(self):
        r = self.result()
        zero = dict.fromkeys(("players", *onetricks.LANES), 0)
        self.assertEqual(r["champions"], {
            "ahri": {**zero, "players": 1, "middle": 1},
            "lux": {**zero, "players": 1, "support": 1},
            "jhin": {**zero, "players": 1, "bottom": 1},
            "pantheon": {**zero, "players": 1, "top": 1, "support": 1},
            "wukong": {**zero, "players": 1, "jungle": 1},
            # Played by the ladder this season, one-tricked by no one.
            "zed": zero, "nami": zero, "garen": zero, "syndra": zero, "yone": zero,
        })  # No katarina (off-ladder), yasuo (last season), teemo (no role).
        self.assertEqual(r["onetrick_players"], 5)
        self.assertEqual(r["games"], 23 + 40 + 41 + 19 + 55 + 40 + 5 + 22)
        self.assertEqual(r["patches"], ["16.1", "16.2", "16.10"])

    def test_store_and_payload(self):
        con = sqlite3.connect(":memory:")
        con.executescript(common.SCHEMA)
        self.assertEqual(onetricks.api_onetricks(con), {"synced": None, "rows": []})
        onetricks.store(con, self.result(), ["kr", "na1"], 9, LADDER_TS)
        # A second sync replaces the first rather than adding to it.
        onetricks.store(con, self.result(), ["kr", "na1"], 9, LADDER_TS)
        p = onetricks.api_onetricks(con)
        self.assertEqual([r["c"] for r in p["rows"]],
                         ["ahri", "jhin", "lux", "pantheon", "wukong",
                          "garen", "nami", "syndra", "yone", "zed"])
        self.assertEqual(p["rows"][3], {"c": "pantheon", "n": 1, "top": 1, "jungle": 0,
                                        "middle": 0, "bottom": 0, "support": 1})
        self.assertRegex(p.pop("synced"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        p.pop("rows")
        self.assertEqual(p, {
            "ladderAt": "2026-09-19T04:32:54Z", "ladderPlayers": 9, "onetrickPlayers": 5,
            "games": 245, "firstPatch": "16.1", "lastPatch": "16.10",
            "minShare": 85, "minGames": 20, "platforms": ["kr", "na1"]})

    def test_sync_command(self):
        con = sqlite3.connect(":memory:")
        con.executescript(common.SCHEMA)
        with patch.object(onetricks, "db_connect", return_value=con), \
                redirect_stdout(io.StringIO()) as out:
            onetricks.cmd_sync(argparse.Namespace(quant_dir=self.root, platforms=["na1"]))
        self.assertIn("3 players on the latest Master+ ladders of na1", out.getvalue())
        p = onetricks.api_onetricks(con)
        self.assertEqual((p["platforms"], p["ladderPlayers"], p["onetrickPlayers"]),
                         (["na1"], 3, 2))
        self.assertEqual({r["c"]: r["n"] for r in p["rows"]},
                         {"pantheon": 1, "wukong": 1, "yone": 0})
        with self.assertRaisesRegex(SystemExit, "No ladder ledger for euw"):
            onetricks.cmd_sync(argparse.Namespace(quant_dir=self.root, platforms=["euw"]))


if __name__ == "__main__":
    unittest.main()
