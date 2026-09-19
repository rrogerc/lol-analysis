"""Scaling: the Overview rankings payload and the page's min-games options."""
import os
import re
import sqlite3
import unittest

import common
import scaling


def stats_db(rows):
    """An in-memory lol.db holding (champion, lane, bucket, games, wins) rows."""
    con = sqlite3.connect(":memory:")
    con.executescript(common.SCHEMA)
    con.executemany(
        "INSERT INTO stats VALUES ('16.18', 'soloq_otp', ?, ?, ?, ?, ?, NULL, "
        "'2026-09-18T00:00:00Z')", rows)
    return con


def page_min_games():
    with open(os.path.join(common.WEB_DIR, "index.html")) as f:
        select = re.search(r'<select id="mingames".*?</select>', f.read(), re.S)
    return [int(v) for v in re.findall(r'<option value="(\d+)"', select.group(0))]


class TestRows(unittest.TestCase):
    def rows(self, con):
        return scaling.build_rows(con, "soloq_otp", ["16.18"])["rows"]

    def test_500_game_floor(self):
        # 999 games in all, under the old 1,000 floor, is kept: its 40+ bucket
        # has exactly 500 games and a win rate, 35-40 has 499 and none.
        # Garen's 499 games in all are dropped.
        con = stats_db([("ahri", "middle", 6, 499, 250), ("ahri", "middle", 7, 500, 275),
                        ("garen", "top", 3, 499, 250)])
        (row,) = self.rows(con)
        self.assertEqual((row["c"], row["l"], row["total"]), ("ahri", "middle", 999))
        self.assertEqual(row["g"], [0, 0, 0, 0, 0, 499, 500])
        self.assertEqual(row["wr"], [None] * 6 + [55.0])

    def test_every_page_option_can_be_filled(self):
        # The page ranks a bucket only when it has a win rate and the chosen
        # games: an option under the payload's floor would show nothing new.
        options = page_min_games()
        self.assertIn(500, options)
        for n in options:
            (row,) = self.rows(stats_db([("ahri", "middle", 7, n, n // 2)]))
            self.assertIsNotNone(row["wr"][6], f"a {n}-game bucket has no win rate")


if __name__ == "__main__":
    unittest.main()
