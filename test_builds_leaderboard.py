"""The Builds tab's leaderboard: every champion's best build under one scenario.

Run: python3 -m unittest test_builds_leaderboard -v

Needs the built engine (importing builds does). The ranking tests write their
own cells into a temp dir; the end-to-end test computes real ones there on a
tiny item pool. The real .cache/builds/ is never touched.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import builds
import builds_leaderboard


def fight(ttk, total=3000, duration=8):
    """One fight of a cell row's `vs` map, as builds._fight_row writes it:
    a kill at `ttk`, or (None) a target that outlived the fight."""
    dps = total / (ttk or duration)
    kill_time = ttk if ttk is not None else round(duration + (5000 - total) / dps, 2)
    return {"ttk": ttk, "ttkExp": ttk, "killTime": kill_time, "loss": 1.0,
            "dps": round(dps), "total": total, "attacks": 3, "breakdown": {"Q": total}}


def target_row(ttk, total=3000, items=("Boots", "Item")):
    """The top row of a per-target cell: its own fight flat, every target's
    under `vs`."""
    own = fight(ttk, total)
    return {"rank": 1, "items": list(items), "gold": 3000, "ap": 0, "ad": 100,
            "attackSpeed": 1.0, "vs": {"squishy": own, "bruiser": fight(9.0),
                                       "tank": fight(None, 4000)},
            "ttk": own["ttk"], "ttkExp": own["ttkExp"], "ttkEff": own["ttk"],
            "dps": own["dps"], "total": own["total"], "attacks": 3,
            "breakdown": own["breakdown"], "buyOrder": list(items[1:])}


def overall_row(times, mean):
    """The top row of an overall cell: a fight per target, `kills`, `mean`."""
    return {"rank": 1, "items": ["Boots", "Item"], "gold": 3000, "ap": 0, "ad": 100,
            "attackSpeed": 1.0, "kills": sum(t is not None for t in times),
            "mean": mean, "vs": dict(zip(("squishy", "bruiser", "tank"),
                                         (fight(t) for t in times)))}


class TestLeaderboard(unittest.TestCase):
    """Ranking over cells written by hand: one champion a kit file."""
    KITS = {"ahri": {"name": "Ahri", "generated": True, "reviewed": False},
            "kayle": {"name": "Kayle"},
            "garen": {"name": "Garen", "generated": True, "reviewed": True},
            "zed": {"name": "Zed", "generated": True},
            "annie": {"name": "Annie", "generated": True}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.paths = {(slug, key): os.path.join(self.tmp, f"{slug}-{key}-{'0' * 16}.json")
                      for slug in self.KITS for key in builds.SCENARIOS}
        for name, value in (("tier_champions", lambda tier: list(self.KITS)),
                            ("load_kit", lambda slug: self.KITS[slug]),
                            # nothing here may simulate
                            ("compute_tier", mock.Mock(side_effect=AssertionError)),
                            ("enumerate_builds", mock.Mock(side_effect=AssertionError))):
            patcher = mock.patch.object(builds, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, slug, key, rows, **extra):
        with open(self.paths[(slug, key)], "w") as f:
            json.dump({"champion": slug, "championName": self.KITS[slug]["name"],
                       "kitPatch": "16.18", "buildsEvaluated": 1234,
                       "computedAt": "2026-09-20T00:00:00+00:00", "rows": rows,
                       **extra}, f)

    def board(self, key):
        return builds_leaderboard.cached_leaderboard(key, self.paths)

    def test_scenarios(self):
        # the damage tiers' scenarios; one tank ranks nothing
        self.assertEqual(builds_leaderboard.leaderboard_scenarios(),
                         ["full-squishy", "full-bruiser", "full-tank", "full-overall"])
        for key in ("survive-kayle", "survive-overall", "nope"):
            with self.assertRaises(ValueError):
                self.board(key)

    def test_target_board(self):
        key = "full-squishy"
        self.write("kayle", key, [target_row(1.07, items=("Boots", "IE", "LDR")),
                                  target_row(0.5)])  # only the top row counts
        self.write("ahri", key, [target_row(0.0)])
        self.write("zed", key, [target_row(1.07)])  # as fast as Kayle
        self.write("garen", key, [target_row(None, total=2500)])  # never kills
        self.write("annie", key, [target_row(None, total=2700)])
        d = self.board(key)
        self.assertEqual([(r["rank"], r["champion"]) for r in d["rows"]],
                         [(1, "ahri"), (2, "kayle"), (2, "zed"),  # a tie, by name
                          (4, "annie"), (5, "garen")])  # most damage first
        self.assertTrue(d["complete"])
        self.assertEqual((d["readyCount"], d["expectedCount"]), (5, 5))
        self.assertEqual((d["pending"], d["failed"]), ([], []))
        kayle = d["rows"][1]
        # the cell's top row, whole, with who it is and whether to trust it
        self.assertEqual(kayle["items"], ["Boots", "IE", "LDR"])
        self.assertEqual(kayle["buyOrder"], ["IE", "LDR"])
        self.assertEqual(kayle["vs"]["bruiser"]["ttkExp"], 9.0)
        self.assertEqual((kayle["championName"], kayle["generated"], kayle["reviewed"]),
                         ("Kayle", False, True))
        self.assertEqual((kayle["kitPatch"], kayle["buildsEvaluated"]), ("16.18", 1234))
        flags = {r["champion"]: (r["generated"], r["reviewed"]) for r in d["rows"]}
        self.assertEqual(flags["ahri"], (True, False))
        self.assertEqual(flags["zed"], (True, False))  # unreviewed unless it says so
        self.assertEqual(flags["garen"], (True, True))
        # the scenario is a cell's: the page reads its targets the same way
        self.assertEqual(d["scenario"]["key"], key)
        self.assertEqual([t["target"] for t in d["scenario"]["targets"]],
                         ["squishy", "bruiser", "tank"])
        json.dumps(d)  # serve sends it as is

    def test_overall_board(self):
        key = "full-overall"
        self.write("kayle", key, [overall_row((1.2, 2.5, 4.2), 2.33)])
        self.write("ahri", key, [overall_row((0.0, 4.5, 7.7), 0.0)])
        # a faster mean that leaves the tank standing ranks below every full kill
        self.write("zed", key, [overall_row((0.5, 1.0, None), 1.9)])
        self.write("garen", key, [overall_row((3.0, 5.0, 7.0), 4.72)])
        # no damage at all on a target: no time to average
        self.write("annie", key, [overall_row((None, None, None), None)])
        d = self.board(key)
        self.assertEqual([(r["rank"], r["champion"]) for r in d["rows"]],
                         [(1, "ahri"), (2, "kayle"), (3, "garen"), (4, "zed"), (5, "annie")])
        self.assertTrue(d["scenario"]["overall"])

    def test_pending_and_failed(self):
        key = "full-tank"
        self.write("kayle", key, [target_row(4.2)])
        self.write("zed", key, [target_row(3.1)])
        # a machine-written driver that failed in the enumeration (builds.warm)
        self.write("ahri", key, [], error="PanicException: index out of bounds")
        self.write("annie", key, [])
        d = self.board(key)  # garen's cell is cold
        self.assertEqual([r["champion"] for r in d["rows"]], ["zed", "kayle"])
        self.assertFalse(d["complete"])
        self.assertEqual((d["readyCount"], d["expectedCount"]), (4, 5))
        self.assertEqual(d["pending"], [{"champion": "garen", "championName": "Garen",
                                         "generated": True, "reviewed": True}])
        self.assertEqual([(f["champion"], f["error"]) for f in d["failed"]],
                         [("ahri", "PanicException: index out of bounds"),
                          ("annie", "no build was ranked")])

    def test_a_recomputed_cell_is_read_again(self):
        key = "full-squishy"
        self.write("kayle", key, [target_row(2.0)])
        self.write("zed", key, [target_row(1.0)])
        first = self.board(key)
        self.assertEqual([r["champion"] for r in first["rows"]], ["zed", "kayle"])
        self.assertEqual(first["rows"][0]["rank"], 1)
        # the memo hands the same rows out again, unchanged by the ranking
        self.assertEqual(self.board(key), first)
        self.write("kayle", key, [target_row(0.75, items=("Boots", "A", "B", "C"))])
        again = self.board(key)
        self.assertEqual([(r["champion"], r["ttkExp"]) for r in again["rows"]],
                         [("kayle", 0.75), ("zed", 1.0)])


class TestRealCells(unittest.TestCase):
    """End to end on cells builds.compute_tier writes: the hand-encoded
    champions on a tiny pool, in a temp cache."""
    TINY_POOL = [3089, 3135, 4645, 6653, 4633, 3115, 3042]  # test_builds' pool

    def test_board_of_computed_cells(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for name, value in (("SCENARIO_CACHE_DIR", tmp), ("DEFAULT_POOL", self.TINY_POOL),
                            ("SHOW_GENERATED", False)):
            patcher = mock.patch.object(builds, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        paths = builds.cell_paths()
        champions = builds.tier_champions("full")
        self.assertGreaterEqual(len(champions), 4)
        cells = {}
        for slug in champions[1:]:
            cells.update({(slug, k): out for k, out in
                          builds.compute_tier(slug, "full", paths).items()})
        for key in builds_leaderboard.leaderboard_scenarios():
            sc = builds.SCENARIOS[key]
            d = builds_leaderboard.cached_leaderboard(key)  # paths: the current cells'
            self.assertFalse(d["complete"])
            self.assertEqual([p["champion"] for p in d["pending"]], champions[:1])
            self.assertCountEqual([r["champion"] for r in d["rows"]], champions[1:])
            for row in d["rows"]:
                top = cells[(row["champion"], key)]["rows"][0]
                self.assertEqual(top["rank"], 1)
                for field in ("items", "buyOrder", "gold", "vs"):
                    self.assertEqual(row[field], top[field], (key, row["champion"], field))
                self.assertEqual((row["generated"], row["reviewed"]), (False, True))
            # best first on the scenario's own metric, as the cells rank builds
            if sc.get("overall"):
                keys = [(len(r["vs"]) - r["kills"], r["mean"]) for r in d["rows"]]
            else:
                self.assertTrue(all(r["ttk"] is not None for r in d["rows"]))
                keys = [r["ttkExp"] for r in d["rows"]]
            self.assertEqual(keys, sorted(keys))
            self.assertEqual([r["rank"] for r in d["rows"]][0], 1)
        # and once every cell is in, the board is complete
        builds.compute_tier(champions[0], "full", paths)
        d = builds_leaderboard.cached_leaderboard("full-overall")
        self.assertTrue(d["complete"])
        self.assertEqual(len(d["rows"]), len(champions))


if __name__ == "__main__":
    unittest.main()
