"""Behavioral tests for staged automatic TFT reconciliation (no simulations)."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import tft
from tft_update import ReviewRequired, reconcile


def change(what, old, new, update="SEPTEMBER 3RD", section="CHAMPIONS"):
    return {"what": what, "old": old, "new": new, "update": update,
            "section": section, "major": "Mid-Patch Updates"}


def entry(c):
    return {"text": f"{c['what']}: {c['old']} ⇒ {c['new']}", "parent": "",
            **{k: c[k] for k in ("update", "section", "major")}}


def unit(name, cost=4):
    return {"apiName": "TFT18_" + name.replace(" ", ""), "name": name,
            "cost": cost, "shopUnit": True, "role": "caster", "traits": [],
            "traitApiNames": [], "assetNames": [], "curveTable": {},
            "stats": {"hp": 850, "damage": 40, "attackSpeed": .75, "armor": 40,
                      "magicResist": 40, "initialMana": 0, "mana": 30, "range": 4},
            "ability": {"name": "Spell", "desc": "A spell.", "attributeCalcs": {}}}


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        amumu, yi, soraka = unit("Amumu"), unit("Master Yi", 3), unit("Soraka")
        amumu["stats"].update(initialMana=30, mana=140)
        amumu["curveTable"]["PassiveHealPercent"] = [[1, .022], [2, .022], [3, .04], [4, .04]]
        yi["stats"].update(armor=60, magicResist=60)
        yi["extraAbilities"] = {"AP": {"variant": "AP", "stats": deepcopy(yi["stats"]),
                                        "curveTable": {}, "attributeCalcs": {}}}
        soraka["curveTable"] = {"DamageAP": [[1, 190], [2, 285], [3, 1000], [4, 2200]],
                                "Shield": [[1, 200], [2, 300], [3, 450], [4, 800]]}
        soraka["ability"]["attributeCalcs"]["MagicDamageCalc1"] = {
            "resolved": True, "terms": [{"type": "scaled", "op": "add", "scaling": "AbilityPower",
                                            "row": "DamageAP", "coefficient": [190, 285, 1000, 2200]}],
            "values": [190, 285, 1000, 2200]}
        self.raw = {"_metadata": {"set": "TFTSet18", "patch": "pbe", "generated": "old"},
                    "units": [amumu, yi, soraka], "traits": [], "items": [],
                    "roles": {"caster": "Magic Caster"},
                    "roleData": {"caster": {"name": "Magic Caster", "roleTags": ["Role.Magic", "Role.Caster"]}}}
        self.overrides = {"units": {
            "TFT18_Amumu": {"stats": {"mana": 125}, "curve": {"PassiveHealPercent": [.025, .025]}},
            "TFT18_MasterYi": {"stats": {"armor": 55, "mr": 55}, "forms": {"AP": {"stats": {"armor": 55, "mr": 55}}}},
            "TFT18_Soraka": {"curve": {"DamageAP": [225, 335]}}}, "items": {}, "traits": {}}
        old_changes = [change("Amumu Heal Max HP %", "2.2%", "2.5%", "AUGUST 31ST"),
                       change("Master Yi Resists", "60", "55", "AUGUST 31ST"),
                       change("Soraka Initial Star Damage", "190/285 AP", "225/335 AP", "AUGUST 31ST")]
        self.notes = {"patch": "18.1d", "basePatch": "18.1", "url": tft.PATCH_NOTES_URL.format(slug="18-1"),
                      "updates": ["AUGUST 31ST"], "changes": old_changes,
                      "notes": [entry(c) for c in old_changes] + [{"text": "Previously reviewed combat mechanics.", "section": "BUG FIXES"}]}
        checks = []
        for target, expected, c in [
            ({"kind": "unit", "api": "TFT18_Amumu", "row": "PassiveHealPercent", "stars": [1, 2]}, [.025, .025], old_changes[0]),
            ({"kind": "unit", "api": "TFT18_Soraka", "row": "DamageAP", "stars": [1, 2]}, [225, 335], old_changes[2]),
        ]:
            checks.append({"id": str(len(checks)), "what": c["what"], "target": target, "expected": expected, "patchLine": c,
                           "source": {"url": self.notes["url"]}})
        for form in [None, "AP"]:
            for stat in ["armor", "mr"]:
                target = {"kind": "unit", "api": "TFT18_MasterYi", "stat": stat}
                if form:
                    target["form"] = form
                checks.append({"id": str(len(checks)), "what": "Master Yi Resists", "target": target,
                               "expected": [55], "patchLine": old_changes[1], "source": {"url": self.notes["url"]}})
        self.audit = {"patch": "18.1d", "checks": checks, "unresolved": [{"id": "existing-limitation"}],
                      "lookupHash": tft.json_hash(self.raw), "binsHash": tft.json_hash({}), "patchNotesHash": tft.json_hash(self.notes)}
        self.previous = self.snapshot("previous", self.raw, self.notes, self.overrides, self.audit)

    def snapshot(self, name, raw, notes, overrides, audit, bins=None):
        directory = Path(self.tmp.name) / name
        directory.mkdir(exist_ok=True)
        values = {"metatft.json": raw, "meta.json": {"set": 18, "patch": notes["patch"]},
                  "patchnotes.json": notes, "overrides.json": overrides, "audit.json": audit,
                  "bins.json": bins or {}}
        for filename, data in values.items():
            (directory / filename).write_text(json.dumps(data))
        return tft.Snapshot(18, notes["patch"], directory=directory)

    def candidate(self, changes=(), raw=None, patch="18.1e", bullets=(), bins=None, next_minor=False):
        notes = deepcopy(self.notes)
        notes["patch"] = patch
        notes["basePatch"] = patch.rstrip("abcdefghijklmnopqrstuvwxyz")
        notes["url"] = tft.PATCH_NOTES_URL.format(slug=notes["basePatch"].replace(".", "-"))
        notes["changes"] = list(changes) + ([] if next_minor else notes["changes"])
        notes["notes"] = [entry(c) for c in changes] + list(bullets) + ([] if next_minor else notes["notes"])
        notes["updates"] = list(dict.fromkeys([c["update"] for c in changes] + notes["updates"]))
        result = self.snapshot("candidate", raw or self.raw, notes, self.overrides, self.audit, bins)
        return result, notes

    def publishable(self, candidate, notes):
        overrides, audit = reconcile(candidate, self.previous, notes)
        checked = self.snapshot("checked", candidate.raw, notes, overrides, audit, candidate.bins)
        findings, _ = tft.check_audit(checked, notes)
        self.assertTrue(all(f["status"] == "current" for f in findings), findings)
        return checked, audit

    def test_unchanged_feeds_and_input_objects_are_preserved(self):
        before = deepcopy(self.previous.__dict__)
        a, b = reconcile(self.previous, self.previous, self.notes)
        self.assertEqual(a, self.overrides)
        self.assertEqual(b["checks"], self.audit["checks"])
        self.assertEqual(b["unresolved"], self.audit["unresolved"])
        self.assertEqual(before, self.previous.__dict__)
        self.assertEqual((a, b), reconcile(self.previous, self.previous, self.notes))

    def test_metadata_only_changes_rebind_exact_hash(self):
        raw = deepcopy(self.raw)
        raw["_metadata"].update(generated="fresh", coreHash="new")
        candidate, notes = self.candidate(raw=raw, patch="18.1d")
        _, audit = self.publishable(candidate, notes)
        self.assertEqual(audit["lookupHash"], tft.json_hash(raw))
        self.assertNotEqual(audit["lookupHash"], self.audit["lookupHash"])

    def test_known_heal_percentage_and_all_master_yi_forms(self):
        candidate, notes = self.candidate([change("Amumu Heal Max HP %", "2.5%", "3%"), change("Master Yi Resists", "55", "50")])
        snap, audit = self.publishable(candidate, notes)
        self.assertEqual([tft.curve_at(snap.units["TFT18_Amumu"]["curve"]["PassiveHealPercent"], s) for s in (1, 2, 3, 4)], [.03, .03, .04, .04])
        yi = snap.units["TFT18_MasterYi"]
        self.assertEqual([yi["stats"]["armor"], yi["stats"]["mr"], yi["forms"]["AP"]["stats"]["armor"], yi["forms"]["AP"]["stats"]["mr"]], [50] * 4)
        self.assertEqual(len(audit["automatic"]["appliedChanges"]), 2)

    def test_same_patch_revision_and_next_minor_same_set(self):
        for patch in ["18.1d", "18.2"]:
            with self.subTest(patch=patch):
                candidate, notes = self.candidate([change("Amumu Heal Max HP %", "2.5%", "2.75%")], patch=patch, next_minor=patch == "18.2")
                snap, audit = self.publishable(candidate, notes)
                self.assertAlmostEqual(tft.curve_at(snap.units["TFT18_Amumu"]["curve"]["PassiveHealPercent"], 1), .0275)
                self.assertEqual(audit["patchNotesHash"], tft.json_hash(notes))

    def test_cumulative_hotfixes_are_applied_oldest_first(self):
        newest = change("Amumu Heal Max HP %", "3%", "3.5%", "SEPTEMBER 4TH")
        older = change("Amumu Heal Max HP %", "2.5%", "3%", "SEPTEMBER 3RD")
        candidate, notes = self.candidate([newest, older])
        snap, _ = self.publishable(candidate, notes)
        self.assertAlmostEqual(tft.curve_at(snap.units["TFT18_Amumu"]["curve"]["PassiveHealPercent"], 1), .035)

    def test_mana_pair_and_extended_star_array(self):
        candidate, notes = self.candidate([change("Amumu Mana", "30/125", "20/110"), change("Soraka Initial Star Damage", "225/335/1000 AP", "250/375/1100 AP")])
        snap, _ = self.publishable(candidate, notes)
        self.assertEqual((snap.units["TFT18_Amumu"]["stats"]["initialMana"], snap.units["TFT18_Amumu"]["stats"]["mana"]), (20, 110))
        self.assertEqual([tft.curve_at(snap.units["TFT18_Soraka"]["curve"]["DamageAP"], s) for s in range(1, 5)], [250, 375, 1100, 2200])

    def test_unambiguous_new_base_stat_and_ability_row(self):
        candidate, notes = self.candidate([change("Soraka Health", "850", "900"), change("Soraka Ability Shield", "200/300/450", "240/360/540")])
        snap, _ = self.publishable(candidate, notes)
        self.assertEqual(snap.units["TFT18_Soraka"]["stats"]["hp"], 900)
        self.assertEqual(tft.curve_at(snap.units["TFT18_Soraka"]["curve"]["Shield"], 2), 360)

    def test_source_catching_up_with_verified_correction(self):
        raw = deepcopy(self.raw)
        raw["units"][0]["curveTable"]["PassiveHealPercent"] = [[1, .025], [2, .025], [3, .04], [4, .04]]
        candidate, notes = self.candidate(raw=raw, patch="18.1d")
        self.publishable(candidate, notes)

    def test_bad_lookup_value_cannot_hide_behind_override(self):
        raw = deepcopy(self.raw)
        raw["units"][0]["curveTable"]["PassiveHealPercent"][0][1] = .9
        candidate, notes = self.candidate(raw=raw)
        with self.assertRaisesRegex(ReviewRequired, "unexplained lookup"):
            reconcile(candidate, self.previous, notes)

    def test_unrelated_numeric_change_is_not_covered_by_hotfix(self):
        raw = deepcopy(self.raw)
        raw["units"][2]["stats"]["hp"] = 999
        candidate, notes = self.candidate([change("Amumu Heal Max HP %", "2.5%", "3%")], raw=raw)
        with self.assertRaisesRegex(ReviewRequired, "base-stat change"):
            reconcile(candidate, self.previous, notes)

    def test_unknown_mechanics_bullet_blocks_even_when_numeric_checks_pass(self):
        candidate, notes = self.candidate(bullets=[{"text": "Soraka now applies a stacking burn on every cast.", "section": "BUG FIXES"}])
        with self.assertRaisesRegex(ReviewRequired, "mechanics bullet"):
            reconcile(candidate, self.previous, notes)

    def test_numeric_line_with_new_mechanics_is_not_treated_as_just_damage(self):
        candidate, notes = self.candidate([change("Soraka Initial Star Damage", "225/335 AP", "250/375 AP and now burns enemies")])
        with self.assertRaisesRegex(ReviewRequired, "unsupported numeric"):
            reconcile(candidate, self.previous, notes)

    def test_scaling_type_cannot_change_under_a_known_numeric_label(self):
        candidate, notes = self.candidate([change('Soraka Initial Star Damage', '225/335 AP', '250/375 AD')])
        with self.assertRaisesRegex(ReviewRequired, 'units/scaling changed'):
            reconcile(candidate, self.previous, notes)

    def test_caught_up_base_stats_retire_redundant_corrections(self):
        raw = deepcopy(self.raw)
        yi = raw['units'][1]
        yi['stats'].update(armor=55, magicResist=55)
        yi['extraAbilities']['AP']['stats'].update(armor=55, magicResist=55)
        candidate, notes = self.candidate(raw=raw, patch='18.1d')
        snap, audit = self.publishable(candidate, notes)
        self.assertEqual(snap.overrides['units']['TFT18_MasterYi']['stats'], {})
        self.assertEqual(snap.overrides['units']['TFT18_MasterYi']['forms']['AP']['stats'], {})
        self.assertEqual(len(audit['automatic']['retiredStatCorrections']), 4)

    def test_ambiguous_ability_rows_require_review(self):
        raw = deepcopy(self.raw)
        raw["units"][2]["curveTable"]["OtherShield"] = deepcopy(raw["units"][2]["curveTable"]["Shield"])
        self.audit["lookupHash"] = tft.json_hash(raw)
        self.previous = self.snapshot("previous", raw, self.notes, self.overrides, self.audit)
        candidate, notes = self.candidate([change("Soraka Ability Shield", "200/300/450", "220/330/500")], raw=raw)
        with self.assertRaisesRegex(ReviewRequired, "multiple curve rows"):
            reconcile(candidate, self.previous, notes)

    def test_unique_numbers_cannot_bind_healing_to_a_shield_row(self):
        candidate, notes = self.candidate([change('Soraka Ability Heal', '200/300/450', '240/360/540')])
        with self.assertRaisesRegex(ReviewRequired, 'zero or multiple curve rows'):
            reconcile(candidate, self.previous, notes)

    def test_novel_ability_label_cannot_add_arbitrary_mechanics_qualifiers(self):
        candidate, notes = self.candidate([change('Soraka Ability Shield On Every Attack', '200/300/450', '240/360/540')])
        with self.assertRaisesRegex(ReviewRequired, 'explicit audit mapping'):
            reconcile(candidate, self.previous, notes)

    def test_unique_auto_attack_and_control_rows_are_not_ability_amounts(self):
        for row, label in [('AutoAttackDamage', 'Ability Damage'), ('BasicAttackDamage', 'Ability Damage'),
                           ('ShieldDuration', 'Ability Shield'), ('ShieldCount', 'Ability Shield'),
                           ('DamageRadius', 'Ability Damage'), ('HealTickRate', 'Ability Heal')]:
            with self.subTest(row=row):
                raw = deepcopy(self.raw)
                raw['units'][2]['curveTable'][row] = [[1, 11], [2, 22], [3, 33], [4, 44]]
                self.audit['lookupHash'] = tft.json_hash(raw)
                self.previous = self.snapshot('previous', raw, self.notes, self.overrides, self.audit)
                candidate, notes = self.candidate([change('Soraka ' + label, '11/22/33', '12/24/36')], raw=raw)
                with self.assertRaisesRegex(ReviewRequired, 'zero or multiple curve rows'):
                    reconcile(candidate, self.previous, notes)

    def test_changed_mechanics_decimal_and_negative_sign_are_not_normalized_away(self):
        reviewed = {'text': 'Champions now heal 1.5% of their Health.', 'section': 'BUG FIXES'}
        self.notes['notes'].append(reviewed)
        self.audit['patchNotesHash'] = tft.json_hash(self.notes)
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        for text in ['Champions now heal 15% of their Health.', 'Champions now heal -1.5% of their Health.']:
            with self.subTest(text=text):
                candidate, notes = self.candidate(bullets=[{**reviewed, 'text': text}])
                with self.assertRaisesRegex(ReviewRequired, 'mechanics bullet'):
                    reconcile(candidate, self.previous, notes)

    def test_successive_reviews_keep_provenance_without_growing_on_unchanged_feeds(self):
        candidate, notes = self.candidate([change('Amumu Heal Max HP %', '2.5%', '3%')])
        first, first_audit = self.publishable(candidate, notes)
        overrides, audit = reconcile(first, first, notes)
        self.assertEqual(audit, first_audit)
        second_notes = deepcopy(notes)
        second_notes['patch'] = '18.1f'
        second_notes['updates'].insert(0, 'SEPTEMBER 4TH')
        c = change('Amumu Heal Max HP %', '3%', '3.5%', 'SEPTEMBER 4TH')
        second_notes['changes'].insert(0, c)
        second_notes['notes'].insert(0, entry(c))
        second = self.snapshot('second', first.raw, second_notes, overrides, audit)
        _, next_audit = reconcile(second, first, second_notes)
        self.assertEqual(next_audit['automaticHistory'][-1], first_audit['automatic'])

    def test_formula_changes_require_review(self):
        raw = deepcopy(self.raw)
        raw["units"][2]["ability"]["attributeCalcs"]["MagicDamageCalc1"]["terms"][0]["op"] = "multiply"
        candidate, notes = self.candidate(raw=raw)
        with self.assertRaisesRegex(ReviewRequired, "formula"):
            reconcile(candidate, self.previous, notes)

    def test_timing_changes_require_review(self):
        candidate, notes = self.candidate(bins={"unit": {"castTime": .5}})
        with self.assertRaisesRegex(ReviewRequired, "timing/bin"):
            reconcile(candidate, self.previous, notes)

    def test_new_champions_require_review(self):
        raw = deepcopy(self.raw)
        raw["units"].append(unit("Brand"))
        candidate, notes = self.candidate(raw=raw)
        with self.assertRaisesRegex(ReviewRequired, "new or removed unit"):
            reconcile(candidate, self.previous, notes)

    def test_wrong_source_and_incomplete_document_require_review(self):
        candidate, notes = self.candidate()
        for bad in [{**notes, "url": "https://example.com/patch"}, {**notes, "notes": []}]:
            with self.subTest(bad=bad["url"]):
                with self.assertRaises(ReviewRequired):
                    reconcile(candidate, self.previous, bad)
        self.previous.audit["lookupHash"] = "unbound"
        with self.assertRaisesRegex(ReviewRequired, "previous audit lookupHash"):
            reconcile(candidate, self.previous, notes)

    def test_cosmetic_bullet_is_recorded_outside_scope(self):
        candidate, notes = self.candidate(bullets=[{"text": "Fixed champion icons on the loading screen.", "section": "BUG FIXES"}])
        _, audit = self.publishable(candidate, notes)
        self.assertTrue(audit["automatic"]["outOfScope"])

    def test_raw_numeric_update_with_consistent_calculation_cache(self):
        raw = deepcopy(self.raw)
        row = raw['units'][2]['curveTable']['DamageAP']
        row[0][1], row[1][1] = 250, 375
        calc = raw['units'][2]['ability']['attributeCalcs']['MagicDamageCalc1']
        calc['terms'][0]['coefficient'][:2] = [250, 375]
        calc['values'][:2] = [250, 375]
        candidate, notes = self.candidate([change('Soraka Initial Star Damage', '225/335 AP', '250/375 AP')], raw=raw)
        self.publishable(candidate, notes)

    def test_wrong_cached_output_cannot_hide_behind_valid_row_update(self):
        raw = deepcopy(self.raw)
        raw['units'][2]['curveTable']['DamageAP'][:2] = [[1, 250], [2, 375]]
        calc = raw['units'][2]['ability']['attributeCalcs']['MagicDamageCalc1']
        calc['terms'][0]['coefficient'][:2] = [250, 375]
        calc['values'][:2] = [250, 999]
        candidate, notes = self.candidate([change('Soraka Initial Star Damage', '225/335 AP', '250/375 AP')], raw=raw)
        with self.assertRaisesRegex(ReviewRequired, 'calculation cache'):
            reconcile(candidate, self.previous, notes)

    def test_repeated_historical_change_in_a_new_hotfix_is_not_skipped(self):
        old = change('Amumu Heal Max HP %', '2.5%', '3%', 'AUGUST 29TH')
        revert = change('Amumu Heal Max HP %', '3%', '2.5%', 'AUGUST 30TH')
        self.notes['changes'].extend([revert, old])
        self.notes['notes'].extend([entry(revert), entry(old)])
        self.notes['updates'].extend(['AUGUST 30TH', 'AUGUST 29TH'])
        self.audit['patchNotesHash'] = tft.json_hash(self.notes)
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        candidate, notes = self.candidate([change('Amumu Heal Max HP %', '2.5%', '3%', 'SEPTEMBER 3RD')])
        snap, _ = self.publishable(candidate, notes)
        self.assertEqual(tft.curve_at(snap.units['TFT18_Amumu']['curve']['PassiveHealPercent'], 1), .03)

    def test_riftbeast_corrected_dependent_breakpoint_requires_review(self):
        line = change('Riftbeast (7) Stats', '6% AD/AP/AS', '5% AD/AP/AS', 'AUGUST 31ST', 'TRAITS')
        self.raw['traits'].append({'apiName': 'DA_Riftbeast18', 'name': 'Riftbeast',
                                  'effects': [{'minUnits': n, 'style': style} for n, style in [(3, 1), (5, 3), (7, 4), (10, 4)]],
                                  'curveTable': {r: [[0, 0], [3, v], [4, v]] for r, v in [('CapstoneAD', .06), ('CapstoneAP', .06), ('CapstoneASPD', 1.06)]}})
        self.overrides['traits']['DA_Riftbeast18'] = {'curve': {r: {'3': v, '4': v} for r, v in [('CapstoneAD', .05), ('CapstoneAP', .05), ('CapstoneASPD', 1.05)]}}
        for row, value in [('CapstoneAD', .05), ('CapstoneAP', .05), ('CapstoneASPD', 1.05)]:
            self.audit['checks'].append({'what': line['what'], 'target': {'kind': 'trait', 'api': 'DA_Riftbeast18', 'row': row, 'columns': [3]},
                                         'expected': [value], 'patchLine': line})
        self.notes['changes'].append(line)
        self.notes['notes'].append(entry(line))
        self.audit.update(lookupHash=tft.json_hash(self.raw), patchNotesHash=tft.json_hash(self.notes))
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        candidate, notes = self.candidate([change('Riftbeast (7) Stats', '5% AD/AP/AS', '4% AD/AP/AS', section='TRAITS')])
        with self.assertRaisesRegex(ReviewRequired, 'corrected dependent columns'):
            reconcile(candidate, self.previous, notes)

    def test_wrong_set_and_malformed_lookup_fail_closed(self):
        candidate, notes = self.candidate()
        candidate.raw['_metadata']['set'] = 'TFTSet19'
        with self.assertRaisesRegex(ReviewRequired, 'wrong TFT set'):
            reconcile(candidate, self.previous, notes)
        candidate.raw['_metadata']['set'] = 'TFTSet18'
        candidate.raw['units'] = None
        with self.assertRaisesRegex(ReviewRequired, 'malformed'):
            reconcile(candidate, self.previous, notes)

    def test_excluded_item_definition_change_retires_old_checks(self):
        api = 'DA_Artifact_Test'
        self.raw['items'].append({'apiName': api, 'name': 'Test Artifact', 'curveTable': {'Health': [[1, 300]]}})
        self.overrides['items'][api] = {'curve': {'Health': 400}}
        self.audit['checks'].append({'what': 'Test Artifact Health', 'target': {'kind': 'item', 'api': api, 'row': 'Health', 'columns': [1]},
                                     'expected': [400], 'patchLine': {'what': 'Test Artifact Health', 'old': '300', 'new': '400'}})
        self.audit['lookupHash'] = tft.json_hash(self.raw)
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        raw = deepcopy(self.raw)
        raw['items'][0]['curveTable']['Health'][0][1] = 450
        candidate, notes = self.candidate(raw=raw)
        snap, audit = self.publishable(candidate, notes)
        self.assertNotIn(api, snap.overrides['items'])
        self.assertTrue(any(c['target']['api'] == api for c in audit['outOfScopeChecks']))

    def test_excluded_stage_array_is_recorded_without_constant_override(self):
        api = 'DA_Artifact_Test'
        self.raw['items'].append({'apiName': api, 'name': 'Test Artifact', 'curveTable': {'Damage': [[1, 30], [3, 60]]}})
        line = change('Test Artifact Damage', '20/20/40', '30/30/60', 'AUGUST 31ST', 'ARTIFACTS')
        for column, value in enumerate([30, 30, 60], 1):
            self.audit['checks'].append({'what': line['what'], 'target': {'kind': 'item', 'api': api, 'row': 'Damage', 'columns': [column]},
                                         'expected': [value], 'patchLine': line})
        self.audit['lookupHash'] = tft.json_hash(self.raw)
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        candidate, notes = self.candidate([change('Test Artifact Damage', '30/30/60', '40/40/70', section='ARTIFACTS')])
        snap, audit = self.publishable(candidate, notes)
        self.assertNotIn(api, snap.overrides['items'])
        self.assertEqual(len([c for c in audit['outOfScopeChecks'] if c['target']['api'] == api]), 3)


if __name__ == "__main__":
    unittest.main()
