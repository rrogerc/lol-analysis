"""Early-board legality, trait-flex catalog, lobby statistics, and Team Planner checks."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tft import board as tft_board, early, flex as tft_flex, sim as tft, site as tft_site
from tft.board import ELDER_DRAGON, RIFTBEAST


def _lobby_fixture():
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


def _lobby_document_fixture():
    snap, overview, details = _lobby_fixture()
    profile = tft_flex.normalize_profile(snap, "diamond_plus", overview, details, "2026-10-11T20:00:00Z")
    return snap, tft_flex.make_document(snap, [profile])


class TestEarlyComps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.3")
        cls.catalog = {"schema": early.SCHEMA, "complete": True, "revision": early.revision(cls.snap),
                       "baselineRevision": tft.snapshot_revision(cls.snap), "set": 18, "patch": "18.3",
                       "stage": 2, "evaluationModel": early.MODEL, "starAssumption": early.STAR_ASSUMPTION,
                       "itemCount": 0, "variants": [], "referencePools": {}}
        cls.combats, cls.candidates, cls.results = {}, {}, {}
        for variant in early.VARIANTS:
            level = variant["level"]
            combat = early.Combat(cls.snap, level, workers=2)
            names = ["Karma", "Yorick", "Leona", "Ornn"] + (["Fiddlesticks"] if level == 5 else [])
            roster = tuple(sorted(cls.snap.unit(name)["api"] for name in names))
            candidate = roster, cls.snap.unit("Karma")["api"], cls.snap.unit("Yorick")["api"]
            result = combat.score_many([candidate], subset="full", details=True)[0]
            board = dict(early.board_payload(combat, candidate, result), rank=1)
            cls.catalog["variants"].append(dict(variant, boards=[board]))
            cls.catalog["referencePools"][variant["key"]] = combat.pool_metadata()
            cls.combats[level], cls.candidates[level], cls.results[level] = combat, candidate, result

    def apis(self, *names):
        return tuple(self.snap.unit(name)["api"] for name in names)

    def test_four_units_exclude_three_costs_and_duplicates(self):
        self.assertTrue(early.legal_roster(self.snap, self.apis("Akali", "Camille", "Leona", "Shen"), 4))
        self.assertFalse(early.legal_roster(self.snap, self.apis("Akali", "Camille", "Leona", "Diana"), 4))
        self.assertFalse(early.legal_roster(self.snap, self.apis("Akali", "Akali", "Leona", "Shen"), 4))
        self.assertFalse(early.legal_roster(self.snap, self.apis("Akali", "Camille", "Leona"), 4))
        self.assertTrue(early.legal_roster(self.snap, self.apis("Akali", "Leona"), 4, complete=False))
        self.assertFalse(early.legal_roster(self.snap, (None, "missing"), 4, complete=False))

    def test_five_units_allow_zero_or_one_three_cost(self):
        base = self.apis("Karma", "Yorick", "Leona", "Ornn")
        self.assertTrue(early.legal_roster(self.snap, (*base, self.apis("Fiddlesticks")[0]), 5))
        self.assertTrue(early.legal_roster(self.snap, (*base, self.apis("Shen")[0]), 5, 0))
        self.assertFalse(early.legal_roster(self.snap, (*base, self.apis("Fiddlesticks")[0]), 5, 0))
        self.assertFalse(early.legal_roster(self.snap, (*base[:3], *self.apis("Fiddlesticks", "Diana")), 5))
        self.assertFalse(early.legal_roster(self.snap, (*base, self.apis("Ahri")[0]), 5))

    def test_every_member_and_reference_opponent_is_one_star(self):
        roster = self.apis("Akali", "Leona", "Shen", "Caitlyn", "Fiddlesticks")
        self.assertEqual({self.snap.units[m["api"]]["name"]: m["star"] for m in early.members_for(self.snap, roster)},
                         {"Akali": 1, "Leona": 1, "Shen": 1, "Caitlyn": 1, "Fiddlesticks": 1})
        for combat in self.combats.values():
            self.assertTrue(all(unit["star"] == 1 for board in combat.pool_metadata()["boards"] for unit in board["units"]))
        self.assertTrue(all(unit["star"] == 1 for variant in self.catalog["variants"]
                            for board in variant["boards"] for match in board["matchups"] for unit in match["roster"]))

    def test_fighter_stage_is_local_and_items_are_rejected(self):
        combat = self.combats[4]
        api = self.apis("Akali")[0]
        early_spec = combat.spec(api, 1, [], ())
        self.assertEqual(early_spec["role"]["asPct"], 0.05)
        baseline_spec = tft.cell_spec(self.snap, self.snap.units[api], 2, "clump", [], {"slots": []})
        self.assertEqual(baseline_spec["role"]["asPct"], tft.FIGHTER_AS_BY_STAGE[tft.STAGE])
        self.assertEqual(tft.STAGE, 4)
        self.assertEqual(early_spec["items"], [])
        self.assertFalse(early_spec["immortal"])
        with self.assertRaises(ValueError):
            combat.spec(api, 1, [], ("DA_Deathblade",))
        with self.assertRaisesRegex(ValueError, "one star"):
            combat.spec(api, 2, [], ())

    def test_early_construction_does_not_require_late_game_reference_pool(self):
        self.assertFalse(hasattr(tft_board, "pool_metadata"))
        self.assertEqual(early.Combat(self.snap, 4, workers=1).pool_metadata()["opponentCount"], 18)

    def test_full_native_results_match_independent_unprepared_fights(self):
        combat, candidate, measured = self.combats[4], self.candidates[4], self.results[4]
        config = measured["config"]
        allies = combat.allies(config["members"], config["resolved"]["effects"], config["selected"], candidate[1], candidate[2])
        reference = early.load_pool(self.snap)["boards"][0]
        members = early.members_for(self.snap, reference["roster"])
        resolved = early.finite_traits(self.snap, members, reference["alphaApi"])
        selected = {m["api"]: {"items": (), "alpha": m["api"] == reference["alphaApi"]} for m in members}
        enemies = combat.allies(members, resolved["effects"], selected, reference["carryApi"], reference["tankApi"])
        for initiative in (0, 1):
            actual = tft.engine().simulate_match({"duration": 30.0, "geometry": "clump", "allies": allies,
                "enemies": enemies, "initiative": initiative, "burnWound": 0.33,
                "damageHealingFromProcs": True, "postDeathAllyHealing": True}, False)
            evidence = measured["matchups"][initiative]
            self.assertEqual(actual["outcome"], evidence["outcome"])
            self.assertAlmostEqual(actual["duration"], evidence["duration"])
            self.assertAlmostEqual(actual["damage"], evidence["damage"])

    def test_screen_and_full_results_use_distinct_evidence_and_caches(self):
        combat, candidate = self.combats[4], self.candidates[4]
        screen = combat.score_many([candidate])[0]
        full = combat.score_many([candidate], subset="full", details=True)[0]
        self.assertLess(screen["metrics"]["benchmarkCount"], full["metrics"]["benchmarkCount"])
        self.assertEqual(full["metrics"]["benchmarkCount"], 18 * 3 * 2)
        self.assertIs(full, self.results[4])
        self.assertEqual(len({m["key"] for m in full["matchups"]}), 108)
        self.assertEqual(sum(full["metrics"][field] for field in
                             ("benchmarkWins", "benchmarkLosses", "benchmarkDraws", "benchmarkTimeouts")), 108)

    def test_mortal_fights_remove_immortal_trait_claims_and_retain_real_omissions(self):
        rosters = [self.apis("Akali", "Shen", "Varus", "Leona"),
                   self.apis("Caitlyn", "Cinderling", "Camille", "Elise"),
                   self.apis("Fiddlesticks", "Karma", "Veigar", "Yorick", "Ornn")]
        for roster in rosters:
            resolved = early.finite_traits(self.snap, early.members_for(self.snap, roster))
            notes = " ".join(resolved["limitations"])
            self.assertNotIn("generic targets", notes)
            self.assertNotIn("immortal targets", notes)
        roster = self.apis("Gromp", "Scuttlecrab", "Pebbles", "Cinderling")
        resolved = early.finite_traits(self.snap, early.members_for(self.snap, roster), self.apis("Scuttlecrab")[0])
        self.assertIn("Riftbeast: Scuttlecrab's Alpha team healing is not modeled", resolved["limitations"])
        self.assertNotIn("Krug ally shields are uncredited", " ".join(resolved["limitations"]))
        elder = early.finite_traits(self.snap, early.members_for(self.snap, self.apis("Xayah", "Ornn", "Alistar", "LeBlanc")))
        self.assertTrue(any("plants" in note for note in elder["limitations"]))

    def test_actual_fixture_validates_and_purchase_gold_is_copies_only(self):
        self.assertIs(early.validate_catalog(self.snap, self.catalog), self.catalog)
        self.assertEqual(self.catalog["variants"][0]["boards"][0]["purchaseGold"], 4)
        self.assertEqual(self.catalog["variants"][1]["boards"][0]["purchaseGold"], 7)
        for variant in self.catalog["variants"]:
            board = variant["boards"][0]
            self.assertAlmostEqual(sum(unit["dps"] for unit in board["units"]), board["metrics"]["damageDps"])

    def test_highlighted_carry_is_measured_damage_dealer_not_just_search_anchor(self):
        combat = self.combats[4]
        candidate = tuple(sorted(self.apis("LeBlanc", "Akali", "Camille", "Elise"))), self.apis("Akali")[0], self.apis("Elise")[0]
        result = combat.score_many([candidate], subset="full", details=True)[0]
        board = early.board_payload(combat, candidate, result)
        highest = max(unit["dps"] for unit in board["units"] if unit["objective"] != "tank")
        self.assertEqual(next(unit["dps"] for unit in board["units"] if unit["slug"] == board["mainCarry"]), highest)
        self.assertEqual(board["damageAnchor"], "akali")
        self.assertNotEqual(board["mainCarry"], board["damageAnchor"])

    def test_invalid_upgrades_costs_and_partial_results_are_rejected(self):
        mutations = [lambda c: c.update(complete=False), lambda c: c.update(patch="18.4"),
                     lambda c: c["variants"][0]["boards"][0]["units"][0].update(star=2),
                     lambda c: c["variants"][1]["boards"][0]["units"][0].update(cost=4),
                     lambda c: c["variants"][0]["boards"][0].update(purchaseGold=0),
                     lambda c: c["variants"][0]["boards"][0]["matchups"].pop(),
                     lambda c: c["variants"][0]["boards"][0]["metrics"].update(benchmarkWins=108),
                     lambda c: c["variants"][0]["boards"][0]["metrics"].update(damageDps=float("nan"))]
        for mutate in mutations:
            changed = deepcopy(self.catalog)
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                early.validate_catalog(self.snap, changed)

    def test_evidence_rejects_duplicate_fights_or_changed_opponent(self):
        changed = deepcopy(self.catalog)
        matches = changed["variants"][0]["boards"][0]["matchups"]
        matches[0]["key"] = matches[1]["key"]
        with self.assertRaisesRegex(ValueError, "duplicates"):
            early.validate_catalog(self.snap, changed)
        for field, value in (("star", 3), ("lane", 6)):
            changed = deepcopy(self.catalog)
            changed["variants"][0]["boards"][0]["matchups"][0]["roster"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "opponent"):
                early.validate_catalog(self.snap, changed)

    def test_reference_upgrade_corruption_is_rejected(self):
        changed = deepcopy(self.catalog)
        changed["referencePools"]["level4"]["boards"][0]["units"][0]["star"] = 3
        with self.assertRaisesRegex(ValueError, "reference unit"):
            early.validate_catalog(self.snap, changed)

    def test_valid_cache_is_reused_without_running_fights(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(early, "CACHE_DIR", Path(directory)):
            early.cache_path(self.snap).write_text(json.dumps(self.catalog))
            with patch.object(early, "search", side_effect=AssertionError("cache unexpectedly searched")):
                self.assertEqual(early.warm(snap=self.snap, log=None), self.catalog)

    def test_early_revision_changes_preserve_baseline_revision(self):
        baseline = tft.snapshot_revision(self.snap)
        before = early.revision(self.snap)
        with patch.object(early, "SOURCE_HASH", "changed early model"):
            self.assertNotEqual(before, early.revision(self.snap))
            self.assertEqual(tft.snapshot_revision(self.snap), baseline)

    def test_tiny_or_other_set_snapshots_do_not_enable_the_model(self):
        self.assertFalse(early.supports(SimpleNamespace(set_no=18, units={"tiny": {}}, traits={})))
        self.assertFalse(early.supports(SimpleNamespace(set_no=19, units=self.snap.units, traits=self.snap.traits)))


class TestTraitFlexCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.3")

    def test_catalog_contains_every_shop_unit_without_using_combat_or_item_models(self):
        with patch.object(tft, "engine", side_effect=AssertionError("combat")), \
                patch.object(tft, "has_driver", side_effect=AssertionError("combat support")), \
                patch.object(tft, "load_item_effects", side_effect=AssertionError("items")), \
                patch.object(tft, "load_trait_effects", side_effect=AssertionError("trait effects")):
            result = tft_flex.catalog(self.snap)
        self.assertEqual(result["schema"], "tft-trait-flex-v1")
        self.assertEqual((result["level"], result["boardSlots"]), (8, 8))
        self.assertEqual({unit["api"] for unit in result["units"]}, set(self.snap.units))
        self.assertEqual({trait["api"] for trait in result["traits"]}, set(self.snap.traits))
        self.assertNotIn("items", result)
        self.assertTrue(all("items" not in unit for unit in result["units"]))
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_anchor_roles_use_base_line_instead_of_damage_objective_alone(self):
        result = tft_flex.catalog(self.snap)
        units = {unit["name"]: unit for unit in result["units"]}
        for name in ("Amumu", "Lillia", "Malphite", "Sentinel", "Sett", "Brambleback", "Morgana"):
            with self.subTest(name=name):
                self.assertEqual(units[name]["cost"], 4)
                self.assertTrue(units[name]["frontline"])
                self.assertFalse(units[name]["backline"])
        for name in ("Ahri", "Aphelios", "Ezreal", "Nidalee", "Sivir", "Soraka", "Zyra"):
            with self.subTest(name=name):
                self.assertEqual(units[name]["cost"], 4)
                self.assertTrue(units[name]["backline"])
                self.assertFalse(units[name]["frontline"])
        self.assertEqual(units["Nidalee"]["range"], 5)

    def test_activation_thresholds_preserve_solo_and_shared_tiers(self):
        result = tft_flex.catalog(self.snap)
        traits = {trait["name"]: trait for trait in result["traits"]}
        self.assertEqual(traits["Flora Fatalis"]["levels"], [1, 2])
        self.assertEqual(traits["Flora Fatalis"]["sharedLevels"], [2])
        self.assertEqual(traits["Rival"]["levels"], [1, 2])
        self.assertEqual(traits["Rival"]["sharedLevels"], [2])
        self.assertEqual(traits["Thornmaiden"]["levels"], [1])
        self.assertEqual(traits["Thornmaiden"]["sharedLevels"], [])
        self.assertEqual(traits["Monolith"]["levels"], [1])
        self.assertEqual(traits["Monolith"]["sharedLevels"], [])
        self.assertEqual(traits["Eclipse"]["levels"], [])
        self.assertEqual(traits["Eclipse"]["sharedLevels"], [])
        self.assertEqual(traits["Blossom"]["sharedLevels"], [3, 5, 7, 9, 11])

    def test_slot_and_trait_contributions_preserve_multi_slot_units(self):
        result = tft_flex.catalog(self.snap)
        for unit in result["units"]:
            with self.subTest(unit=unit["name"]):
                self.assertEqual(unit["slotCost"], 2 if unit["api"] == ELDER_DRAGON else 1)
                self.assertEqual(set(unit["traitContributions"]), set(unit["traitApis"]))
                for trait, count in unit["traitContributions"].items():
                    self.assertEqual(count, 2 if unit["api"] == ELDER_DRAGON and trait == RIFTBEAST else 1)

    def test_revision_ignores_items_and_stats_outside_the_unit_search(self):
        before = tft_flex.catalog(self.snap)
        snap = deepcopy(self.snap)
        snap.items.clear()
        snap.units["TFT18_Ahri"]["stats"]["hp"] += 999
        snap.units["TFT18_Ahri"]["stats"]["ad"] += 999
        self.assertEqual(tft_flex.catalog(snap), before)
        snap.traits["DA_18_Spellweaver"]["levels"] = [2, 5, 6]
        self.assertNotEqual(tft_flex.catalog(snap)["revision"], before["revision"])

    def test_duplicate_trait_membership_is_not_double_counted(self):
        snap = deepcopy(self.snap)
        snap.units["TFT18_Ahri"]["traitApis"] *= 2
        self.assertEqual(tft_flex.catalog(snap), tft_flex.catalog(self.snap))
        snap.units["TFT18_Ahri"]["traitApis"].append("missing-trait")
        with self.assertRaisesRegex(ValueError, "unknown trait"):
            tft_flex.catalog(snap)

    def test_shop_model_has_reviewed_odds_bags_and_actual_roster_populations(self):
        shop = tft_flex.catalog(self.snap)["shop"]
        self.assertEqual(shop["costOdds"], [.15, .20, .32, .30, .03])
        self.assertEqual(shop["bagCopies"], [30, 25, 18, 10, 9])
        self.assertEqual(shop["championCounts"], [14, 13, 14, 14, 10])
        self.assertEqual(shop["visibleSlots"], [4, 5])
        self.assertEqual(shop["refreshCost"], 2)
        self.assertTrue(all(source["url"].startswith("https://teamfighttactics.leagueoflegends.com/")
                            for source in shop["sources"]))
        snap = deepcopy(self.snap)
        del snap.units["TFT18_Ahri"]
        self.assertEqual(tft_flex.catalog(snap)["shop"]["championCounts"], [14, 13, 14, 13, 10])

    def test_shop_parameter_changes_revise_the_catalog_and_invalid_models_fail(self):
        model = tft_flex.shop_model(self.snap)
        with tempfile.TemporaryDirectory() as directory, patch.object(tft, "set_dir", return_value=directory):
            self.assertNotIn("shop", tft_flex.catalog(self.snap))
            path = Path(directory) / "shop-model.json"
            path.write_text(json.dumps(model))
            original = tft_flex.catalog(self.snap)
            model["visibleSlots"] = [5]
            path.write_text(json.dumps(model))
            self.assertNotEqual(tft_flex.catalog(self.snap)["revision"], original["revision"])
            for key, invalid in (("costOdds", [.15, .20, .32, .30, .50]), ("bagCopies", [30, 25, 18, 2, 9]),
                                 ("visibleSlots", [3]), ("costOdds", [True, 0, 0, 0, 0])):
                with self.subTest(key=key):
                    path.write_text(json.dumps(dict(model, **{key: invalid})))
                    with self.assertRaisesRegex(ValueError, "shop model"):
                        tft_flex.catalog(self.snap)

    def test_lobby_statistics_revise_only_the_trait_catalog(self):
        before = tft_flex.catalog(self.snap)
        baseline_revision = tft.snapshot_revision(self.snap)
        profiles = deepcopy(before["lobbyStats"])
        profiles["profiles"][0]["units"][0]["copiesPerPlayer"] += 0.01
        with patch.object(tft_flex, "catalog_profiles", return_value=profiles):
            after = tft_flex.catalog(self.snap)
        self.assertNotEqual(before["revision"], after["revision"])
        self.assertEqual(after["shop"], before["shop"])
        self.assertEqual(tft.snapshot_revision(self.snap), baseline_revision)

    def test_bad_lobby_statistics_leave_the_unit_search_and_baseline_available(self):
        with patch.object(tft_flex, "catalog_profiles", side_effect=ValueError("statistics patch mismatch")):
            result = tft_flex.catalog(self.snap)
        self.assertEqual(len(result["units"]), len(self.snap.units))
        self.assertEqual(result["shop"]["championCounts"], [14, 13, 14, 14, 10])
        self.assertEqual(result["lobbyStats"]["profiles"], [])
        self.assertIn("statistics patch mismatch", result["lobbyStats"]["error"])


class TestTftLobbyStatistics(unittest.TestCase):
    def test_parse_embedded_public_data_without_executing_scripts(self):
        page = {"props": {"pageProps": {"aperture": {"queue": 1100}}}}
        html = f"<script>throw Error('never execute')</script><script type='application/json' id='__NEXT_DATA__'>{json.dumps(page)}</script>"
        self.assertEqual(tft_flex.parse_next_data(html), page["props"]["pageProps"])
        for invalid in ("<html>no data</html>", '<script id="__NEXT_DATA__">bad json</script>'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                tft_flex.parse_next_data(invalid)

    def test_explicit_patch_and_rank_are_path_segments(self):
        self.assertEqual(tft_flex.source_url("18.3", "diamond_plus"), "https://tactics.tools/units/18.3")
        self.assertEqual(tft_flex.source_url("18.3", "all", "DA_18_Ahri"), "https://tactics.tools/units/da_18_ahri/18.3/all")
        self.assertEqual(tft_flex.patch_code("18.3"), 16190)
        self.assertEqual(tft_flex.patch_code("18.3b"), 16191)
        for value in ("latest", "17.3", "18.3garbage"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tft_flex.patch_code(value)

    def test_variant_counts_and_four_stars_use_standard_merge_equivalents(self):
        snap, document = _lobby_document_fixture()
        profile = document["profiles"][0]
        rows = {row["api"]: row for row in profile["units"]}
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
        self.assertIs(tft_flex.validate_document(snap, document), document)

    def test_no_guessing_unknown_source_ids_or_missing_shop_champions(self):
        snap, overview, details = _lobby_fixture()
        overview["statsData"]["units"]["DA_18_Guess"] = {"count": 1}
        with self.assertRaisesRegex(ValueError, "unmapped"):
            tft_flex.normalize_profile(snap, "diamond_plus", overview, details, "2026-10-11T20:00:00Z")
        snap, overview, details = _lobby_fixture()
        del overview["statsData"]["units"]["DA_18_Ahri"]
        with self.assertRaisesRegex(ValueError, "missing shop champion"):
            tft_flex.normalize_profile(snap, "diamond_plus", overview, details, "2026-10-11T20:00:00Z")

    def test_reject_silent_latest_rank_queue_and_unit_fallbacks(self):
        for field, value, message in (("patch", {"_0": 16201}, "patch mismatch"),
                                       ("rankGroup", 3, "rank mismatch"),
                                       ("queue", 1160, "normal ranked"),
                                       ("gameType", 1, "normal ranked")):
            snap, overview, details = _lobby_fixture()
            overview["aperture"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                tft_flex.normalize_profile(snap, "diamond_plus", overview, details, "2026-10-11T20:00:00Z")
        snap, overview, details = _lobby_fixture()
        details["DA_18_Ahri"]["unitId"] = "DA_18_Sett"
        with self.assertRaisesRegex(ValueError, "unit mismatch"):
            tft_flex.normalize_profile(snap, "diamond_plus", overview, details, "2026-10-11T20:00:00Z")

    def test_reject_incomplete_inconsistent_or_unknown_star_distributions(self):
        for case in ("missing", "overview_count", "unknown_star", "duplicate_star"):
            snap, overview, details = _lobby_fixture()
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
                tft_flex.normalize_profile(snap, "diamond_plus", overview, details, "2026-10-11T20:00:00Z")

    def test_reject_numeric_identity_and_provenance_corruption(self):
        for case in ("nan", "rate", "identity", "source", "star", "missing", "duplicate"):
            snap, document = _lobby_document_fixture()
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
                tft_flex.validate_document(snap, document)

    def test_catalog_is_offline_and_does_not_substitute_a_newer_patch(self):
        snap, document = _lobby_document_fixture()
        with tempfile.TemporaryDirectory() as directory:
            snap.dir = str(Path(directory) / "18.3")
            newer = deepcopy(document)
            newer["patch"] = "18.4"
            (Path(directory) / "lobby-stats-18.4.json").write_text(json.dumps(newer))
            with patch("urllib.request.urlopen", side_effect=AssertionError("catalog must be offline")):
                empty = tft_flex.catalog_profiles(snap)
                self.assertEqual(empty["profiles"], [])
                self.assertIsNone(empty["defaultProfile"])
                path = tft_flex.write_document(snap, document)
                self.assertEqual(path.name, "lobby-stats-18.3.json")
                self.assertEqual(tft_flex.catalog_profiles(snap), document)
                invalid = deepcopy(document)
                invalid["patch"] = "18.4"
                with self.assertRaisesRegex(ValueError, "patch mismatch"):
                    tft_flex.write_document(snap, invalid)
                self.assertEqual(tft_flex.catalog_profiles(snap), document)
                self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_checked_in_profiles_are_exact_and_cover_the_gameplay_roster(self):
        snap = tft.load_snapshot(18, "18.3")
        document = tft_flex.catalog_profiles(snap)
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


class TestTeamPlannerCatalog(unittest.TestCase):
    def test_all_current_champions_match_the_archived_riot_planner_records(self):
        snap = tft.load_snapshot(18, "18.1d")
        catalog = json.loads((Path(tft.set_dir(18)) / "team-planner.json").read_bytes())
        planner = tft_site._team_planner(snap)
        self.assertEqual(set(planner["unitCodes"]), set(snap.units))
        self.assertEqual(len(set(planner["unitCodes"].values())), len(snap.units))
        raw = {unit["apiName"]: unit for unit in snap.raw["units"]}
        for api, code in planner["unitCodes"].items():
            source = catalog["sourceRecords"][api]
            with self.subTest(champion=api):
                self.assertIn(source["matchedAsset"], raw[api]["assetNames"])
                self.assertEqual(source["displayName"], snap.units[api]["name"])
                self.assertEqual(source["tier"], snap.units[api]["cost"])
                self.assertEqual(int(code, 16), source["teamPlannerCode"])
        self.assertEqual(planner["unitCodes"]["TFT18_Ivern"], "404")
        self.assertEqual(planner["unitCodes"]["TFT18_Lux_Base"], "405")
        self.assertNotIn("413", planner["unitCodes"].values())


if __name__ == "__main__":
    unittest.main()
