"""Patch-specific public statistics are complete, mapped, and served offline."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tft
import tft_lobby


def fixture():
    units = {"TFT18_Ahri": {"name": "Ahri", "cost": 4},
             "TFT18_Lux_Base": {"name": "Lux", "cost": 5}}
    snap = SimpleNamespace(set_no=18, patch="18.3", units=units,
                           raw={"units": [{"apiName": "TFT18_Ahri", "assetNames": ["DA_18_Ahri"]},
                                          {"apiName": "TFT18_Lux_Base", "assetNames": ["DA_Lux18_Base", "DA_18_Lux_Fae"]}]},
                           dir="/unused/set18/18.3")
    aperture = {"patch": {"_0": 16190}, "rankGroup": 1, "gameType": 0, "queue": 1100}
    distributions = {"DA_18_Ahri": [4, 5, 1, 0], "DA_Lux18_Base": [2, 0, 0, 0], "DA_18_Lux_Fae": [1, 2, 0, 1]}
    overview = {"aperture": deepcopy(aperture),
                "statsData": {"totalEntries": 80, "lastUpdated": 1790333940,
                              "units": {key: {"count": sum(counts)} for key, counts in distributions.items()}}}
    details = {}
    for source_id, counts in distributions.items():
        details[source_id] = {"aperture": deepcopy(aperture), "unitId": source_id, "starLevel": 0,
                              "unitData": {"unitId": source_id, "starLevel": 0,
                                           "base": {"count": sum(counts)},
                                           "starLevelData": [{"id": i + 1, "count": count} for i, count in enumerate(counts) if count]}}
    return snap, overview, details


def document_fixture():
    snap, overview, details = fixture()
    profile = tft_lobby.normalize_profile(snap, "diamond_plus", overview, details, "2026-10-11T20:00:00Z")
    return snap, tft_lobby.make_document(snap, [profile])


class TestTftLobbyStatistics(unittest.TestCase):
    def test_parse_embedded_public_data_without_executing_scripts(self):
        page = {"props": {"pageProps": {"aperture": {"queue": 1100}}}}
        html = f"<script>throw Error('never execute')</script><script type='application/json' id='__NEXT_DATA__'>{json.dumps(page)}</script>"
        self.assertEqual(tft_lobby.parse_next_data(html), page["props"]["pageProps"])
        for invalid in ("<html>no data</html>", '<script id="__NEXT_DATA__">bad json</script>'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                tft_lobby.parse_next_data(invalid)

    def test_explicit_patch_and_rank_are_path_segments(self):
        self.assertEqual(tft_lobby.source_url("18.3", "diamond_plus"), "https://tactics.tools/units/18.3")
        self.assertEqual(tft_lobby.source_url("18.3", "all", "DA_18_Ahri"), "https://tactics.tools/units/da_18_ahri/18.3/all")
        self.assertEqual(tft_lobby.patch_code("18.3"), 16190)
        self.assertEqual(tft_lobby.patch_code("18.3b"), 16191)
        for value in ("latest", "17.3", "18.3garbage"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tft_lobby.patch_code(value)

    def test_variant_counts_and_four_stars_use_standard_merge_equivalents(self):
        snap, document = document_fixture()
        profile = document["profiles"][0]
        rows = {row["api"]: row for row in profile["units"]}
        # Lux source assets are distinct rows in the final-board statistics.
        lux = rows["TFT18_Lux_Base"]
        self.assertEqual(len(lux["sourceUnits"]), 2)
        self.assertEqual(lux["starRates"], [3 / 6, 2 / 6, 0, 1 / 6])
        self.assertAlmostEqual(lux["playRatePerLobby"], 0.6)
        self.assertEqual(lux["meanCopiesWhenPlayed"], 6.0)
        self.assertEqual(lux["copiesPerPlayer"], 0.45)
        self.assertEqual(rows["TFT18_Ahri"]["meanCopiesWhenPlayed"], 2.8)
        self.assertEqual(rows["TFT18_Ahri"]["copiesPerPlayer"], 0.35)
        self.assertEqual(document["copyEquivalentsByStar"], [1, 3, 9, 27])
        self.assertTrue(any("actual pool withdrawals" in value for value in document["limitations"]))
        self.assertIs(tft_lobby.validate_document(snap, document), document)

    def test_no_guessing_unknown_source_ids_or_missing_shop_champions(self):
        snap, overview, details = fixture()
        overview["statsData"]["units"]["DA_18_Guess"] = {"count": 1}
        with self.assertRaisesRegex(ValueError, "unmapped"):
            tft_lobby.normalize_profile(snap, "diamond_plus", overview, details, "2026-10-11T20:00:00Z")
        snap, overview, details = fixture()
        del overview["statsData"]["units"]["DA_18_Ahri"]
        with self.assertRaisesRegex(ValueError, "missing shop champion"):
            tft_lobby.normalize_profile(snap, "diamond_plus", overview, details, "2026-10-11T20:00:00Z")

    def test_reject_silent_latest_rank_queue_and_unit_fallbacks(self):
        for field, value, message in (("patch", {"_0": 16201}, "patch mismatch"),
                                       ("rankGroup", 3, "rank mismatch"),
                                       ("queue", 1160, "normal ranked"),
                                       ("gameType", 1, "normal ranked")):
            snap, overview, details = fixture()
            overview["aperture"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                tft_lobby.normalize_profile(snap, "diamond_plus", overview, details, "2026-10-11T20:00:00Z")
        snap, overview, details = fixture()
        details["DA_18_Ahri"]["unitId"] = "DA_18_Sett"
        with self.assertRaisesRegex(ValueError, "unit mismatch"):
            tft_lobby.normalize_profile(snap, "diamond_plus", overview, details, "2026-10-11T20:00:00Z")

    def test_reject_incomplete_inconsistent_or_unknown_star_distributions(self):
        for case in ("missing", "overview_count", "unknown_star", "duplicate_star"):
            snap, overview, details = fixture()
            detail = details["DA_18_Ahri"]["unitData"]
            if case == "missing":
                detail["starLevelData"].pop()
            elif case == "overview_count":
                detail["base"]["count"] += 1
            elif case == "unknown_star":
                detail["starLevelData"][0]["id"] = 5
            else:
                detail["starLevelData"][0]["id"] = 2
            with self.subTest(case=case), self.assertRaises(ValueError):
                tft_lobby.normalize_profile(snap, "diamond_plus", overview, details, "2026-10-11T20:00:00Z")

    def test_reject_numeric_identity_and_provenance_corruption(self):
        for case in ("nan", "rate", "identity", "source", "star", "missing", "duplicate"):
            snap, document = document_fixture()
            row = document["profiles"][0]["units"][0]
            if case == "nan":
                row["copiesPerPlayer"] = float("nan")
            elif case == "rate":
                row["copiesPerPlayer"] += 0.1
            elif case == "identity":
                row["cost"] = 5
            elif case == "source":
                row["sourceUnits"][0]["id"] = "guess"
            elif case == "star":
                row["sourceUnits"][0]["starCounts"][0] += 1
            elif case == "missing":
                document["profiles"][0]["units"].pop()
            else:
                document["profiles"].append(deepcopy(document["profiles"][0]))
            with self.subTest(case=case), self.assertRaises(ValueError):
                tft_lobby.validate_document(snap, document)

    def test_catalog_is_offline_and_does_not_substitute_a_newer_patch(self):
        snap, document = document_fixture()
        with tempfile.TemporaryDirectory() as directory:
            snap.dir = str(Path(directory) / "18.3")
            newer = deepcopy(document)
            newer["patch"] = "18.4"
            (Path(directory) / "lobby-stats-18.4.json").write_text(json.dumps(newer))
            with patch("urllib.request.urlopen", side_effect=AssertionError("catalog must be offline")):
                empty = tft_lobby.catalog_profiles(snap)
                self.assertEqual(empty["profiles"], [])
                self.assertIsNone(empty["defaultProfile"])
                path = tft_lobby.write_document(snap, document)
                self.assertEqual(path.name, "lobby-stats-18.3.json")
                self.assertEqual(tft_lobby.catalog_profiles(snap), document)
                invalid = deepcopy(document)
                invalid["patch"] = "18.4"
                with self.assertRaisesRegex(ValueError, "patch mismatch"):
                    tft_lobby.write_document(snap, invalid)
                self.assertEqual(tft_lobby.catalog_profiles(snap), document)
                self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_checked_in_profiles_are_exact_and_cover_the_gameplay_roster(self):
        snap = tft.load_snapshot(18, "18.3")
        document = tft_lobby.catalog_profiles(snap)
        self.assertEqual({profile["rank"] for profile in document["profiles"]}, {"diamond_plus", "all"})
        self.assertEqual(document["defaultProfile"], "18.3-diamond_plus")
        for profile in document["profiles"]:
            with self.subTest(rank=profile["rank"]):
                self.assertEqual(profile["patch"], snap.patch)
                self.assertEqual({row["api"] for row in profile["units"]}, set(snap.units))
                self.assertGreater(profile["totalEntries"], 700000)
                self.assertTrue(all(len(row["starRates"]) == 4 for row in profile["units"]))
                lux = next(row for row in profile["units"] if row["name"] == "Lux")
                self.assertEqual(len(lux["sourceUnits"]), 10)


if __name__ == "__main__":
    unittest.main()
