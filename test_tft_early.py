"""Early-board legality, stage inputs, finite evidence and cache isolation."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tft
import tft_comps
import tft_early as early
import tft_team


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
        with patch.object(tft_team, "pool_metadata", side_effect=AssertionError("late-game opponents loaded")):
            self.assertEqual(early.Combat(self.snap, 4, workers=1).pool_metadata()["opponentCount"], 18)

    def test_full_native_results_match_independent_unprepared_fights(self):
        combat, candidate, measured = self.combats[4], self.candidates[4], self.results[4]
        config = measured["config"]
        allies = combat.allies(config["members"], config["resolved"]["effects"], config["selected"], candidate[1], candidate[2])
        # Recreate the first authored opponent through public unit definitions,
        # rather than reusing prepared handles from the measured batch.
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

    def test_early_revision_changes_preserve_baseline_and_composition_revisions(self):
        baseline, composition = tft.snapshot_revision(self.snap), tft_comps.revision(self.snap)
        before = early.revision(self.snap)
        with patch.object(early, "SOURCE_HASH", "changed early model"):
            self.assertNotEqual(before, early.revision(self.snap))
            self.assertEqual(tft.snapshot_revision(self.snap), baseline)
            self.assertEqual(tft_comps.revision(self.snap), composition)

    def test_tiny_or_other_set_snapshots_do_not_enable_the_model(self):
        self.assertFalse(early.supports(SimpleNamespace(set_no=18, units={"tiny": {}}, traits={})))
        self.assertFalse(early.supports(SimpleNamespace(set_no=19, units=self.snap.units, traits=self.snap.traits)))


if __name__ == "__main__":
    unittest.main()
