"""Precise runner-up backfills preserve the immutable published rankings."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jobs import tft_bis_gaps
import test_tft_site as site_tests
import tft


def performance(time):
    return {"killTime": time, "total": 1000.0, "dps": 1000 / time,
            "aliveTime": 30.0, "survivalCapped": False,
            "stressAliveTime": None, "stressCapped": False}


class TestBisPublication(unittest.TestCase):
    def setUp(self):
        self.fixture = site_tests.TestTftSite()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.meta["set"] = self.fixture.snap.set_no
        for (slug, key), path in self.fixture.paths.items():
            payload = self.fixture.payloads[f"/api/tft/{slug}/{key}.json"]
            payload["rows"] = [{"items": ["A", "B", "C"]}, {"items": ["A", "B", "D"]}]
            payload["best"] = {"itemApis": ["A", "B", "C"], "performance": performance(10.004)}
            payload["runnerUp"] = {"itemApis": ["A", "B", "D"], "performance": performance(10.007)}
            Path(path).write_text(json.dumps(payload))
        self.fixture.leaderboard.side_effect = self.board

    def board(self, key, paths, *, snap):
        rows = [{"unit": slug, "scenario": scenario, "rank": rank,
                 **json.loads(Path(path).read_bytes())["best"]}
                for rank, ((slug, scenario), path) in enumerate(paths.items(), 1)]
        return {"revision": "baseline", "complete": True, "selection": {"key": key},
                "readyCount": len(paths), "expectedCount": len(paths), "pending": [],
                "damage": rows, "tanks": []}

    def test_upgrade_preserves_ranks_original_bytes_and_unchanged_links(self):
        _, before = self.fixture.prepare()
        raw = {url: before.asset(url)["path"].read_bytes() for url in before.entries}
        with patch.object(tft, "engine", side_effect=AssertionError("exact saved scores must not replay")):
            descriptor = tft_bis_gaps.prepare(before, self.fixture.snap, log=lambda _: None)
        after = tft_bis_gaps.tft_site.load(descriptor)
        self.assertEqual(after.manifest["baselineRevision"], before.manifest["baselineRevision"])
        self.assertEqual(set(after.entries), set(before.entries))
        for url in before.entries:
            self.assertEqual(before.asset(url)["path"].read_bytes(), raw[url])
            if url.startswith("/api/tft/leaderboard/"):
                original, enriched = before.json(url), after.json(url)
                for row in enriched["damage"]:
                    self.assertAlmostEqual(row["secondBest"]["performance"]["lossPct"],
                                           100 * (10.007 - 10.004) / 10.004)
                    row.pop("secondBest")
                self.assertEqual(enriched, original)
        for slug, key in self.fixture.paths:
            url = f"/api/tft/{slug}/{key}.json"
            self.assertEqual(after.entries[url], before.entries[url])
            self.assertEqual(after.asset(url)["path"].stat().st_ino,
                             before.asset(url)["path"].stat().st_ino)
        for url in ("/api/tft/meta.json", "/api/tft/status.json"):
            self.assertEqual(after.json(url)["publicationRevision"], descriptor["siteGeneration"])
        with patch.object(tft, "leaderboard_runner_up", side_effect=AssertionError("repeat backfill")):
            self.assertEqual(tft_bis_gaps.prepare(after, self.fixture.snap), descriptor)

    def test_invalid_runner_does_not_publish_or_change_the_original(self):
        slug, key = next(iter(self.fixture.paths))
        path = Path(self.fixture.paths[slug, key])
        payload = json.loads(path.read_bytes())
        payload["runnerUp"]["performance"] = performance(1.0)
        path.write_text(json.dumps(payload))
        _, before = self.fixture.prepare()
        directories = list(self.fixture.site.glob("g-*"))
        original = {url: before.asset(url)["path"].read_bytes() for url in before.entries}
        with self.assertRaisesRegex(ValueError, "ranks ahead"):
            tft_bis_gaps.prepare(before, self.fixture.snap, log=lambda _: None)
        self.assertEqual(list(self.fixture.site.glob("g-*")), directories)
        self.assertEqual(list(self.fixture.site.glob(".stage-*")), [])
        self.assertEqual({url: before.asset(url)["path"].read_bytes() for url in before.entries}, original)


class TestBisReplay(unittest.TestCase):
    def test_missing_runner_replays_exact_recorded_builds_and_checks_optimal_fight(self):
        snap = tft.load_snapshot(18, "18.1d")
        unit = snap.unit("Azir")
        key = "s2-clump-low"
        pool = [snap.item(name)["api"] for name in ("Blue Buff", "Spear of Shojin")]
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "pool_items", return_value=pool):
            path = str(Path(directory) / "cell.json")
            original = tft.compute_cell(snap, unit, key, {("azir", key): path}, prune=False)
        expected = tft.leaderboard_runner_up(original)
        legacy = deepcopy(original)
        legacy.pop("runnerUp")
        legacy["coreAnalysis"] = {}
        first = deepcopy(legacy["best"])
        second, replayed = tft_bis_gaps.runner_up(snap, legacy)
        self.assertTrue(replayed)
        self.assertEqual(second, expected)
        self.assertEqual(legacy["best"], first)
        invalid = deepcopy(original)
        invalid.pop("runnerUp")
        invalid["coreAnalysis"] = {}
        invalid["best"]["performance"]["killTime"] = 123.0
        with self.assertRaisesRegex(ValueError, "no longer reproduces"):
            tft_bis_gaps.runner_up(snap, invalid)


if __name__ == "__main__":
    unittest.main()
