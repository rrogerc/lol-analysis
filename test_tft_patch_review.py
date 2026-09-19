"""A reviewed transition must never become approval for unrelated changes."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

import tft
import tft_update
import test_tft_update as fixtures


class TestBoundReview(unittest.TestCase):
    setUp = fixtures.TestReconcile.setUp
    snapshot = fixtures.TestReconcile.snapshot
    candidate = fixtures.TestReconcile.candidate

    def manifest(self, candidate, notes, mappings=(), dispositions=()):
        return {'schema': 1, 'patch': candidate.patch, 'previousPatch': self.previous.patch,
                'source': notes['url'], 'bindings': {
                    'lookupHash': tft.json_hash(candidate.raw), 'binsHash': tft.json_hash(candidate.bins),
                    'patchNotesHash': tft.json_hash(notes), 'previousAuditHash': tft.json_hash(self.previous.audit)},
                'mappings': list(mappings), 'dispositions': list(dispositions)}

    def mapping(self, line, stars=(1, 2), expected=(250, 375)):
        target = {'kind': 'unit', 'api': 'TFT18_Soraka', 'row': 'DamageAP', 'stars': list(stars)}
        return {'change': line, 'target': target, 'expected': list(expected),
                'observedBefore': tft_update._values(self.previous, target),
                'numericEncoding': {'scale': 1, 'offset': 0},
                'reason': 'Reviewed exact current AP row despite stale previous source values.'}

    def reviewed(self, candidate, notes, review):
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes, review=review)
        checked = self.snapshot('checked', candidate.raw, notes, overrides, audit, candidate.bins)
        findings, _ = tft.check_audit(checked, notes)
        self.assertTrue(all(f['status'] == 'current' for f in findings), findings)
        return checked, audit

    def test_stale_source_requires_bound_review_and_preserves_unmentioned_stars(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'continuity gap'):
            tft_update.reconcile(candidate, self.previous, notes)
        review = self.manifest(candidate, notes, [self.mapping(line)])
        untouched = deepcopy((candidate.__dict__, self.previous.__dict__, notes, review))
        checked, audit = self.reviewed(candidate, notes, review)
        self.assertEqual(tft_update._values(checked, {**self.mapping(line)['target'], 'stars': [1, 2, 3, 4]}),
                         [250, 375, 1000, 2200])
        self.assertEqual(audit['reviews'], [review])
        self.assertEqual(untouched, (candidate.__dict__, self.previous.__dict__, notes, review))

    def test_every_source_binding_is_required(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        for binding in ('lookupHash', 'binsHash', 'patchNotesHash', 'previousAuditHash'):
            with self.subTest(binding=binding):
                review = self.manifest(candidate, notes, [self.mapping(line)])
                review['bindings'][binding] = 'wrong'
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'manifest hashes'):
                    self.reviewed(candidate, notes, review)

    def test_wrong_transition_or_source_is_rejected(self):
        candidate, notes = self.candidate()
        for key, value in [('patch', '18.3'), ('previousPatch', '18.1c'), ('source', 'https://example.com')]:
            with self.subTest(key=key):
                review = self.manifest(candidate, notes)
                review[key] = value
                with self.assertRaises(tft_update.ReviewRequired):
                    self.reviewed(candidate, notes, review)

    def test_baseline_values_cannot_be_silently_replaced(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        mapping = self.mapping(line)
        mapping['observedBefore'] = [230, 345]
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'baseline no longer matches'):
            self.reviewed(candidate, notes, self.manifest(candidate, notes, [mapping]))

    def test_exact_evidence_including_major_and_punctuation_is_required(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        for field, value in [('major', 'Small Changes'), ('new', '250/375AP'), ('what', 'Soraka Initial Star Damage!')]:
            with self.subTest(field=field):
                mapping = deepcopy(self.mapping(line))
                mapping['change'][field] = value
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'absent from the exact'):
                    self.reviewed(candidate, notes, self.manifest(candidate, notes, [mapping]))

    def test_review_does_not_hide_unrelated_raw_change(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        raw = deepcopy(self.raw)
        raw['units'][0]['stats']['hp'] = 999
        candidate, notes = self.candidate([line], raw=raw)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'base-stat change'):
            self.reviewed(candidate, notes, self.manifest(candidate, notes, [self.mapping(line)]))

    def test_review_does_not_hide_unknown_mechanics(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line], bullets=[{'text': 'Soraka now burns all enemies.', 'section': 'BUG FIXES'}])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'mechanics bullet'):
            self.reviewed(candidate, notes, self.manifest(candidate, notes, [self.mapping(line)]))

    def test_exact_mechanics_disposition_is_recorded(self):
        note = {'text': 'Soraka now selects the farthest ally.', 'section': 'BUG FIXES'}
        candidate, notes = self.candidate(bullets=[note])
        disposition = {'note': note, 'disposition': 'retained-combat-approximation',
                       'reason': 'The single-actor model counts ally shielding but does not place allies.'}
        _, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, dispositions=[disposition]))
        self.assertIn(disposition, audit['automatic']['outOfScope'])

    def test_note_disposition_does_not_skip_an_unreviewed_numeric_change(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        disposition = {'note': fixtures.entry(line), 'disposition': 'reviewed', 'reason': 'A note is not a numeric mapping.'}
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'continuity gap'):
            self.reviewed(candidate, notes, self.manifest(candidate, notes, dispositions=[disposition]))

    def test_tooltip_correction_uses_exact_nonnumeric_note(self):
        note = {'text': 'Soraka tooltip now correctly states 250/375 damage; functionality unchanged.', 'section': 'BUG FIXES'}
        candidate, notes = self.candidate(bullets=[note])
        mapping = self.mapping({})
        del mapping['change']
        mapping['note'] = note
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, [mapping]))
        self.assertEqual(tft_update._values(checked, mapping['target']), [250, 375])
        self.assertTrue(audit['checks'][-1]['manualOnly'])

    def test_overlapping_check_preserves_unaffected_coordinates_and_history(self):
        line = fixtures.change('Soraka Two Star Damage', '345 AP', '375 AP')
        candidate, notes = self.candidate([line])
        mapping = self.mapping(line, stars=[2], expected=[375])
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, [mapping]))
        damage = [c for c in audit['checks'] if c['target'].get('row') == 'DamageAP']
        self.assertEqual([c['target']['stars'] for c in damage], [[1], [2]])
        self.assertEqual([c['expected'] for c in damage], [[225], [375]])
        self.assertEqual(damage[-1]['supersededChecks'][0]['expected'], [225, 335])

    def test_simple_review_mapping_is_reusable_for_future_numeric_update(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, [self.mapping(line)]))
        future_line = fixtures.change(line['what'], '250/375 AP', '260/390 AP', update='SEPTEMBER 4TH')
        future_notes = deepcopy(notes)
        future_notes['changes'].insert(0, future_line)
        future_notes['notes'].insert(0, fixtures.entry(future_line))
        future_notes['updates'].insert(0, 'SEPTEMBER 4TH')
        future = self.snapshot('future', candidate.raw, future_notes, checked.overrides, checked.audit)
        overrides, audit = tft_update.reconcile(future, checked, future_notes)
        self.assertEqual(overrides['units']['TFT18_Soraka']['curve']['DamageAP'], [260, 390, 1000, 2200])

    def test_compound_review_never_becomes_a_guessed_automatic_mapping(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP + 10% HP', '250/375 AP + 15% HP')
        candidate, notes = self.candidate([line])
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, [self.mapping(line)]))
        self.assertTrue(audit['checks'][-1]['manualOnly'])
        future_line = fixtures.change(line['what'], line['new'], '270/405 AP + 15% HP', update='SEPTEMBER 4TH')
        future_notes = deepcopy(notes)
        future_notes['changes'].insert(0, future_line)
        future_notes['notes'].insert(0, fixtures.entry(future_line))
        future_notes['updates'].insert(0, 'SEPTEMBER 4TH')
        future = self.snapshot('future', candidate.raw, future_notes, checked.overrides, checked.audit)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'decomposed expression'):
            tft_update.reconcile(future, checked, future_notes)

    def test_future_array_extension_cannot_leave_newly_listed_stars_stale(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, [self.mapping(line)]))
        future_line = fixtures.change(line['what'], '250/375/1000 AP', '260/390/1100 AP', update='SEPTEMBER 4TH')
        future_notes = deepcopy(notes)
        future_notes['changes'].insert(0, future_line)
        future_notes['notes'].insert(0, fixtures.entry(future_line))
        future_notes['updates'].insert(0, 'SEPTEMBER 4TH')
        future = self.snapshot('future', candidate.raw, future_notes, checked.overrides, checked.audit)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'outside the reviewed scope'):
            tft_update.reconcile(future, checked, future_notes)

    def test_augment_note_cannot_reuse_a_champion_mapping_with_the_same_label(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, [self.mapping(line)]))
        future_line = fixtures.change(line['what'], '250/375 AP', '260/390 AP', update='SEPTEMBER 4TH', section='AUGMENTS')
        future_notes = deepcopy(notes)
        future_notes['changes'].insert(0, future_line)
        future_notes['notes'].insert(0, fixtures.entry(future_line))
        future_notes['updates'].insert(0, 'SEPTEMBER 4TH')
        future = self.snapshot('future', candidate.raw, future_notes, checked.overrides, checked.audit)
        overrides, audit = tft_update.reconcile(future, checked, future_notes)
        self.assertEqual(overrides['units']['TFT18_Soraka']['curve']['DamageAP'], [250, 375, 1000, 2200])
        self.assertIn('outside', audit['automatic']['outOfScope'][0]['reason'])

    def test_successive_checks_do_not_recursively_duplicate_history(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        check = {'what': line['what'], 'target': self.mapping(line)['target'], 'expected': [250, 375]}
        checks = [check]
        for step in range(8):
            updated = deepcopy(checks[-1])
            updated['expected'] = [251 + step, 376 + step]
            tft_update._replace_check(checks, updated)
        history = checks[-1]['supersededChecks']
        self.assertEqual(len(history), 8)
        self.assertTrue(all('supersededChecks' not in old for old in history))

    def test_excluded_stage_review_retires_only_changed_field_checks(self):
        api = 'DA_Artifact_Reviewed'
        self.raw['items'].append({'apiName': api, 'name': 'Reviewed Artifact',
                                 'curveTable': {'Damage': [[1, 30], [3, 50]], 'Health': [[1, 300]]}})
        for row, column, expected in [('Damage', 1, 30), ('Damage', 3, 50), ('Health', 1, 300)]:
            self.audit['checks'].append({'what': f'Reviewed Artifact {row}',
                'target': {'kind': 'item', 'api': api, 'row': row, 'columns': [column]}, 'expected': [expected]})
        self.audit['lookupHash'] = tft.json_hash(self.raw)
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        line = fixtures.change('Reviewed Artifact Damage', '30/50 (stages 2–4)', '25/45', section='ARTIFACTS')
        candidate, notes = self.candidate([line])
        disposition = {'change': line, 'disposition': 'excluded-item-stage-array',
                       'reason': 'The stage encoding is unresolved, and this artifact is excluded from both pools.',
                       'retireTargets': [{'kind': 'item', 'api': api, 'row': 'Damage'}]}
        _, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, dispositions=[disposition]))
        self.assertEqual([c['target']['row'] for c in audit['checks'] if c['target']['api'] == api], ['Health'])
        retired = [c for c in audit['outOfScopeChecks'] if c['target']['api'] == api]
        self.assertEqual([c['expected'] for c in retired], [[30], [50]])
        self.assertTrue(all(c['retiredAtPatch'] == candidate.patch for c in retired))

    def test_review_cannot_retire_modeled_values_without_replacements(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        disposition = {'change': line, 'disposition': 'unsupported', 'reason': 'Retirement is not approval to skip modeled changes.',
                       'retireTargets': [self.mapping(line)['target']]}
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'only an excluded item field'):
            self.reviewed(candidate, notes, self.manifest(candidate, notes, dispositions=[disposition]))

    def test_flat_and_percentage_units_cannot_reuse_the_same_numeric_value(self):
        self.audit['checks'].append({'what': 'Soraka Health',
            'target': {'kind': 'unit', 'api': 'TFT18_Soraka', 'stat': 'hp'}, 'expected': [850],
            'numericEncoding': {'scale': 1, 'offset': 0},
            'patchLine': {'what': 'Soraka Health', 'old': '800 HP', 'new': '850 HP'}})
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        for old, new in [('850 HP', '850% HP'), ('850% HP', '850 HP'),
                         ('850 HP', '850%'), ('850', '850%')]:
            line = fixtures.change('Soraka Health', old, new)
            candidate, notes = self.candidate([line])
            with self.subTest(old=old), self.assertRaisesRegex(tft_update.ReviewRequired, 'percentage/flat units changed'):
                tft_update.reconcile(candidate, self.previous, notes)

    def test_omitted_repeated_unit_suffix_remains_supported(self):
        line = fixtures.change('Soraka Health', '850 HP', '900')
        candidate, notes = self.candidate([line])
        overrides, _ = tft_update.reconcile(candidate, self.previous, notes)
        self.assertEqual(overrides['units']['TFT18_Soraka']['stats']['hp'], 900)

    def test_malformed_target_values_and_duplicate_review_are_rejected(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        for value in ([250], [True, 375], [float('inf'), 375]):
            mapping = self.mapping(line)
            mapping['expected'] = value
            with self.subTest(value=value), self.assertRaisesRegex(tft_update.ReviewRequired, 'invalid reviewed expected'):
                self.reviewed(candidate, notes, self.manifest(candidate, notes, [mapping]))
        mapping = self.mapping(line)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'duplicate reviewed mapping'):
            self.reviewed(candidate, notes, self.manifest(candidate, notes, [mapping, mapping]))

    def test_manifest_file_is_loaded_only_for_a_new_transition(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line], patch='18.2', next_minor=True)
        review = self.manifest(candidate, notes, [self.mapping(line)])
        directory = Path(self.previous.dir).parent / 'patch-reviews'
        directory.mkdir()
        (directory / '18.2.json').write_text(json.dumps(review))
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes)
        checked = self.snapshot('checked', candidate.raw, notes, overrides, audit)
        # An unchanged scheduled run must not reapply the old transition review.
        _, again = tft_update.reconcile(checked, checked, notes)
        self.assertEqual(again['reviews'], [review])

    def test_xp_purchase_cost_is_outside_fixed_level_combat_but_other_systems_are_not(self):
        line = {'what': 'Level 7 to Level 8', 'old': '60', 'new': '56', 'update': '',
                'section': 'XP PER LEVEL', 'major': 'SYSTEMS'}
        candidate, notes = self.candidate([line], patch='18.2', next_minor=True)
        _, audit = tft_update.reconcile(candidate, self.previous, notes)
        self.assertIn('XP purchase costs', audit['automatic']['outOfScope'][0]['reason'])
        line['what'] = 'All units attack speed'
        candidate, notes = self.candidate([line], patch='18.2', next_minor=True)
        with self.assertRaises(tft_update.ReviewRequired):
            tft_update.reconcile(candidate, self.previous, notes)
