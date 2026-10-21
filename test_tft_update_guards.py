"""Review guards added after the 2026-09-20 leaderboard audit (no simulations).

- the wrong-row guard: a numeric note mapped onto a row its numbers do not fit, while
  a sibling row fits them, needs a recorded acknowledgement (18.2's Nidalee mapping);
- lookupChanges / definitionDispositions: a regenerated lookup's unannounced changes are
  decided one coordinate at a time, against the exact staged values;
- the base-stat cross-check against the archived CommunityDragon export.
"""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tft
import tft_update
import test_tft_update as fixtures


class Base(unittest.TestCase):
    setUp = fixtures.TestReconcile.setUp
    snapshot = fixtures.TestReconcile.snapshot
    candidate = fixtures.TestReconcile.candidate

    def manifest(self, candidate, notes, **records):
        return {'schema': 1, 'patch': candidate.patch, 'previousPatch': self.previous.patch, 'source': notes['url'],
                'bindings': {'lookupHash': tft.json_hash(candidate.raw), 'binsHash': tft.json_hash(candidate.bins),
                             'patchNotesHash': tft.json_hash(notes), 'previousAuditHash': tft.json_hash(self.previous.audit)},
                'mappings': [], 'dispositions': [], **records}

    def reviewed(self, candidate, notes, review):
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes, review=review)
        checked = self.snapshot('checked', candidate.raw, notes, overrides, audit, candidate.bins)
        findings, _ = tft.check_audit(checked, notes)
        self.assertTrue(all(f['status'] == 'current' for f in findings), findings)
        return checked, audit

    def rebase(self, raw, overrides=None):
        """Make `raw` the previous lookup (the audit is bound to it)."""
        self.raw = raw
        self.overrides = overrides or self.overrides
        self.audit['lookupHash'] = tft.json_hash(raw)
        self.previous = self.snapshot('previous', raw, self.notes, self.overrides, self.audit)


class TestWrongRowGuard(Base):
    """Riot: "Nidalee AP Form: Nidalee Empowered Attack Damage: 285/425 AP ⇒ 300/450 AP"."""
    LINE = fixtures.change('Nidalee AP Form: Nidalee Empowered Attack Damage', '285/425 AP', '300/450 AP')

    def setUp(self):
        super().setUp()
        nidalee = fixtures.unit('Nidalee')
        nidalee['curveTable'] = {'EmpoweredDamage': [[1, 170], [2, 255], [3, 2000], [4, 3000]],
                                 'ThirdAttackEmpoweredDamage': [[1, 320], [2, 480], [3, 3000], [4, 5000]],
                                 'TrapApplyDamage': [[1, 50], [2, 75], [3, 100], [4, 100]]}
        raw = deepcopy(self.raw)
        raw['units'].append(nidalee)
        self.rebase(raw)

    def mapping(self, row, **extra):
        target = {'kind': 'unit', 'api': 'TFT18_Nidalee', 'row': row, 'stars': [1, 2]}
        return {'change': self.LINE, 'target': target, 'expected': [300, 450],
                'observedBefore': tft_update._values(self.previous, target),
                'numericEncoding': {'scale': 1, 'offset': 0}, 'reason': 'Reviewed.', **extra}

    def test_the_18_2_nidalee_mapping_is_refused_without_an_acknowledgement(self):
        candidate, notes = self.candidate([self.LINE])
        review = self.manifest(candidate, notes, mappings=[self.mapping('EmpoweredDamage')])
        with self.assertRaisesRegex(tft_update.ReviewRequired, r'EmpoweredDamage may be the wrong row \(ThirdAttackEmpoweredDamage held \[320, 480\]'):
            tft_update.reconcile(candidate, self.previous, notes, review=review)

    def test_the_corrected_mapping_passes_and_leaves_the_ordinary_javelins_alone(self):
        candidate, notes = self.candidate([self.LINE])
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, mappings=[self.mapping('ThirdAttackEmpoweredDamage')]))
        curve = checked.units['TFT18_Nidalee']['curve']
        self.assertEqual([tft.curve_at(curve['EmpoweredDamage'], s) for s in (1, 2, 3, 4)], [170, 255, 2000, 3000])
        self.assertEqual([tft.curve_at(curve['ThirdAttackEmpoweredDamage'], s) for s in (1, 2, 3, 4)], [300, 450, 3000, 5000])

    def test_an_acknowledgement_names_the_rows_it_rules_out_and_is_kept_in_the_audit(self):
        candidate, notes = self.candidate([self.LINE])
        ack = {'rows': ['ThirdAttackEmpoweredDamage'], 'reason': 'Deliberate: documented elsewhere.'}
        review = self.manifest(candidate, notes, mappings=[self.mapping('EmpoweredDamage', siblingRowAcknowledgement=ack)])
        _, audit = self.reviewed(candidate, notes, review)
        self.assertEqual(audit['reviews'][-1]['mappings'][0]['siblingRowAcknowledgement'], ack)
        for bad in ({'rows': ['TrapApplyDamage'], 'reason': 'Names the wrong sibling.'},
                    {'rows': ['ThirdAttackEmpoweredDamage'], 'reason': ' '}, {'rows': [], 'reason': 'x'}, 'yes'):
            with self.subTest(bad=bad), self.assertRaises(tft_update.ReviewRequired):
                review = self.manifest(candidate, notes, mappings=[self.mapping('EmpoweredDamage', siblingRowAcknowledgement=bad)])
                tft_update.reconcile(candidate, self.previous, notes, review=review)

    def test_an_acknowledgement_nothing_calls_for_is_rejected(self):
        candidate, notes = self.candidate([self.LINE])
        ack = {'rows': ['EmpoweredDamage'], 'reason': 'Not needed.'}
        review = self.manifest(candidate, notes, mappings=[self.mapping('ThirdAttackEmpoweredDamage', siblingRowAcknowledgement=ack)])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'no sibling row fits the note better'):
            tft_update.reconcile(candidate, self.previous, notes, review=review)

    def test_a_stale_source_with_no_better_sibling_needs_no_acknowledgement(self):
        # Soraka's DamageAP holds 225/335 against a stated old 260/390 (14% off); Shield 200/300 is further.
        line = fixtures.change('Soraka Initial Star Damage', '260/390 AP', '280/420 AP')
        candidate, notes = self.candidate([line])
        target = {'kind': 'unit', 'api': 'TFT18_Soraka', 'row': 'DamageAP', 'stars': [1, 2]}
        mapping = {'change': line, 'target': target, 'expected': [280, 420], 'observedBefore': [225, 335],
                   'numericEncoding': {'scale': 1, 'offset': 0}, 'reason': 'Stale source, right row.'}
        self.reviewed(candidate, notes, self.manifest(candidate, notes, mappings=[mapping]))

    def test_upstream_moving_a_sibling_to_the_new_value_is_the_second_sign(self):
        # The mapped row matches the note's old value exactly, so the first sign is silent; but the
        # staged lookup already carries 300/450 on the sibling and left the mapped row alone.
        line = fixtures.change('Nidalee Ability Damage', '170/255 AP', '300/450 AP')
        raw = deepcopy(self.raw)
        raw['units'][-1]['curveTable']['ThirdAttackEmpoweredDamage'] = [[1, 300], [2, 450], [3, 3000], [4, 5000]]
        candidate, notes = self.candidate([line], raw=raw)
        target = {'kind': 'unit', 'api': 'TFT18_Nidalee', 'row': 'EmpoweredDamage', 'stars': [1, 2]}
        mapping = {'change': line, 'target': target, 'expected': [300, 450], 'observedBefore': [170, 255],
                   'numericEncoding': {'scale': 1, 'offset': 0}, 'reason': 'Exact old value.'}
        with self.assertRaisesRegex(tft_update.ReviewRequired, r"ThirdAttackEmpoweredDamage went from \[320, 480\] to the note's new \[300, 450\]"):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, mappings=[mapping]))

    def test_our_own_override_on_a_sibling_is_not_upstream_moving(self):
        # 18.2's Radiant Hand of Justice: AD/AP pinned at 0.35 over an unchanged raw 0.3, and an
        # Omnivamp note whose new value happens to be 0.3.
        raw = deepcopy(self.raw)
        raw['units'][-1]['curveTable'].update(OmnivampBase=[[1, .24]], ADBase=[[1, .3]])
        overrides = deepcopy(self.overrides)
        overrides['units']['TFT18_Nidalee'] = {'curve': {'ADBase': [.35]}}
        self.rebase(raw, overrides)
        line = fixtures.change('Nidalee Base Omnivamp', '24%', '30%')
        candidate, notes = self.candidate([line])
        target = {'kind': 'unit', 'api': 'TFT18_Nidalee', 'row': 'OmnivampBase', 'stars': [1]}
        mapping = {'change': line, 'target': target, 'expected': [.3], 'observedBefore': [.24],
                   'numericEncoding': {'scale': .01, 'offset': 0}, 'reason': 'Exact old value.'}
        self.reviewed(candidate, notes, self.manifest(candidate, notes, mappings=[mapping]))

    def test_an_acknowledgement_belongs_to_a_numeric_note_on_a_curve_row(self):
        line = fixtures.change('Soraka Health', '800', '900')
        candidate, notes = self.candidate([line])
        mapping = {'change': line, 'target': {'kind': 'unit', 'api': 'TFT18_Soraka', 'stat': 'hp'}, 'expected': [900],
                   'observedBefore': [850], 'reason': 'Stale.', 'siblingRowAcknowledgement': {'rows': ['armor'], 'reason': 'x'}}
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'only applies to a numeric note mapped to a curve row'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, mappings=[mapping]))

    def test_attack_in_a_row_name_no_longer_hides_an_ability_amount_from_discovery(self):
        for row, expected in [('ThirdAttackEmpoweredDamage', {'damage'}), ('MiniDamagePerAttack', {'damage'}),
                              ('ThirdAttackHeal', {'heal'}), ('AutoAttackDamage', set()), ('BasicAttackDamage', set()),
                              ('BonusAttackDamage', set()), ('EmpoweredAttackDamage', set()), ('AttackSpeedBuff', set()),
                              ('NumEmpoweredAttacks', set())]:
            with self.subTest(row=row):
                self.assertEqual(tft_update._row_semantics(row), expected)
        # the automatic path now finds the third javelin by its exact old value, and only that row
        line = fixtures.change('Nidalee Ability Damage', '320/480 AP', '300/450 AP')
        raw = deepcopy(self.raw)
        raw['units'][-1]['ability']['attributeCalcs']['MagicDamageCalc2'] = {'terms': [
            {'type': 'scaled', 'op': 'add', 'scaling': 'AbilityPower', 'row': 'ThirdAttackEmpoweredDamage',
             'coefficient': [320, 480, 3000, 5000]}], 'values': [320, 480, 3000, 5000]}
        self.rebase(raw)
        candidate, notes = self.candidate([line])
        overrides, _ = tft_update.reconcile(candidate, self.previous, notes)
        self.assertEqual(overrides['units']['TFT18_Nidalee']['curve'], {'ThirdAttackEmpoweredDamage': [300, 450, 3000, 5000]})


class TestObservedLookupMappings(Base):
    """A reviewed official value can override a separately recorded lookup conflict."""
    OLD, EXPECTED, LOOKUP = [68, 102, 155, 165], [73, 108, 165, 175], [73, 108, 165, 280]

    def setUp(self):
        super().setUp()
        self.target = {'kind': 'unit', 'api': 'TFT18_Xayah', 'row': 'DamageAD', 'stars': [1, 2, 3, 4]}
        self.line = fixtures.change('Xayah Ability Damage', '68/102/155/165 AD', '73/108/165/175 AD')
        xayah = fixtures.unit('Xayah', 1)
        xayah['curveTable']['DamageAD'] = [[star, value] for star, value in enumerate(self.OLD, 1)]
        calc = {'resolved': True, 'terms': [{'type': 'scaled', 'op': 'add', 'scaling': 'AttackDamage',
                                            'row': 'DamageAD', 'coefficient': deepcopy(self.OLD)}],
                'values': deepcopy(self.OLD)}
        xayah['ability']['attributeCalcs'] = {'PhysicalDamageCalc1': deepcopy(calc)}
        xayah['attributeCalcs'] = {'PhysicalDamageCalc1': deepcopy(calc)}
        raw = deepcopy(self.raw)
        raw['units'].append(xayah)
        self.rebase(raw)

    def staged_raw(self, fourth=280):
        raw = deepcopy(self.raw)
        xayah = raw['units'][-1]
        values = [*self.LOOKUP[:3], fourth]
        xayah['curveTable']['DamageAD'] = [[star, value] for star, value in enumerate(values, 1)]
        for block in (xayah, xayah['ability']):
            calc = block['attributeCalcs']['PhysicalDamageCalc1']
            calc['terms'][0]['coefficient'] = deepcopy(values)
            calc['values'] = deepcopy(values)
        return raw

    def mapping(self, *, observed=True):
        record = {'change': self.line, 'target': deepcopy(self.target), 'observedBefore': deepcopy(self.OLD),
                  'expected': deepcopy(self.EXPECTED), 'numericEncoding': {'scale': 1, 'offset': 0},
                  'reason': 'Official four-star damage is 175; the staged lookup says 280 and remains a source conflict.'}
        if observed:
            record['observedLookup'] = deepcopy(self.LOOKUP)
        return record

    def test_exact_observed_lookup_acknowledges_the_conflict_but_final_model_uses_expected(self):
        candidate, notes = self.candidate([self.line], raw=self.staged_raw())
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified coefficient'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, mappings=[self.mapping(observed=False)]))
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, mappings=[self.mapping()]))
        self.assertEqual(tft_update._values(checked, self.target), self.EXPECTED)
        self.assertEqual(checked.units['TFT18_Xayah']['calcs']['PhysicalDamageCalc1']['terms'][0]['coefficient'], self.EXPECTED)
        self.assertEqual(checked.raw['units'][-1]['curveTable']['DamageAD'][-1], [4, 280])
        check = next(c for c in audit['checks'] if c['target'] == self.target)
        self.assertEqual((check['expected'], check['observedLookup'], check['fixedScope']),
                         (self.EXPECTED, self.LOOKUP, True))

    def test_observed_lookup_requires_finite_numeric_values_at_every_target_coordinate(self):
        candidate, notes = self.candidate([self.line], raw=self.staged_raw())
        invalid = (None, [], [73, 108, 165], (73, 108, 165, 280), ['73', 108, 165, 280],
                   [True, 108, 165, 280], [float('nan'), 108, 165, 280], [float('inf'), 108, 165, 280])
        for values in invalid:
            with self.subTest(values=values), self.assertRaisesRegex(tft_update.ReviewRequired, 'invalid reviewed observedLookup'):
                record = {**self.mapping(), 'observedLookup': values}
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, mappings=[record]))

    def test_observed_lookup_matches_override_free_source_and_rejects_stale_values(self):
        candidate, notes = self.candidate([self.line], raw=self.staged_raw())
        overrides = deepcopy(self.overrides)
        overrides['units']['TFT18_Xayah'] = {'curve': {'DamageAD': deepcopy(self.EXPECTED)}}
        effective = self.snapshot('effective', candidate.raw, notes, overrides, self.audit)
        self.assertEqual(tft_update._values(effective, self.target), self.EXPECTED)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'staged lookup does not hold.*observedLookup'):
            tft_update.reconcile(effective, self.previous, notes,
                                 review=self.manifest(effective, notes, mappings=[{**self.mapping(), 'observedLookup': self.EXPECTED}]))
        self.reviewed(effective, notes, self.manifest(effective, notes, mappings=[self.mapping()]))
        candidate, notes = self.candidate([self.line], raw=self.staged_raw(fourth=281))
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'staged lookup does not hold.*observedLookup'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, mappings=[self.mapping()]))

    def test_observed_lookup_does_not_cover_an_unrelated_stat_or_unmapped_star(self):
        for case in ('stat', 'star'):
            with self.subTest(case=case):
                raw, record = self.staged_raw(), self.mapping()
                if case == 'stat':
                    raw['units'][-1]['stats']['armor'] += 1
                    message = 'unexplained base-stat change'
                else:
                    record['target']['stars'] = [1, 2, 3]
                    for key in ('expected', 'observedBefore', 'observedLookup'):
                        record[key] = record[key][:3]
                    message = 'unverified coefficient'
                candidate, notes = self.candidate([self.line], raw=raw)
                with self.assertRaisesRegex(tft_update.ReviewRequired, message):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, mappings=[record]))

    def test_observed_lookup_does_not_permit_a_second_coordinate_decision(self):
        candidate, notes = self.candidate([self.line], raw=self.staged_raw())
        lookup = TestLookupChanges.record(self, self.target, self.LOOKUP, decision='keep-reviewed-value')
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'same coordinate twice'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, mappings=[self.mapping()], lookupChanges=[lookup]))

    def test_a_persisted_lookup_conflict_does_not_approve_a_changed_future_feed(self):
        candidate, notes = self.candidate([self.line], raw=self.staged_raw())
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, mappings=[self.mapping()]))
        _, repeated = tft_update.reconcile(checked, checked, notes)
        self.assertEqual(repeated, audit)
        future = self.snapshot('future', self.staged_raw(fourth=281), notes, checked.overrides, audit)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified coefficient'):
            tft_update.reconcile(future, checked, notes)


class TestPublished18_2Review(unittest.TestCase):
    """The real 18.1d -> 18.2 review, replayed on the archived inputs."""
    WHAT = 'Nidalee AP Form: Nidalee Empowered Attack Damage'

    @classmethod
    def setUpClass(cls):
        cls.previous = tft.load_snapshot(18, '18.1d')
        active = Path(tft.load_snapshot(18, '18.2').dir)
        cls.manifest = json.loads((active.parent / 'patch-reviews' / '18.2.json').read_text())
        cls.tmp = tempfile.TemporaryDirectory()
        staging = Path(cls.tmp.name) / '18.2'
        staging.mkdir()
        for name in ('metatft.json', 'bins.json', 'patchnotes.json', 'meta.json'):
            shutil.copyfile(active / name, staging / name)
        shutil.copyfile(Path(cls.previous.dir) / 'overrides.json', staging / 'overrides.json')
        cls.candidate = tft.Snapshot(18, '18.2', directory=str(staging))
        cls.notes = json.loads((staging / 'patchnotes.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_published_mapping_passes_the_guard_without_an_acknowledgement(self):
        self.assertFalse([m for m in self.manifest['mappings'] if 'siblingRowAcknowledgement' in m])
        review = tft_update._BoundReview(self.candidate, self.previous, self.notes, self.manifest)
        self.assertEqual(sum(len(v) for v in review.changes.values()) + sum(len(v) for v in review.notes.values()), 98)

    def test_the_original_nidalee_mapping_would_not_have_passed(self):
        wrong = deepcopy(self.manifest)
        mapping = next(m for m in wrong['mappings'] if m.get('change', {}).get('what') == self.WHAT)
        self.assertEqual((mapping['target']['row'], mapping['observedBefore'], mapping['expected']),
                         ('ThirdAttackEmpoweredDamage', [320, 480], [300, 450]))
        mapping['target']['row'], mapping['observedBefore'] = 'EmpoweredDamage', [170, 255]
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'EmpoweredDamage may be the wrong row .ThirdAttackEmpoweredDamage'):
            tft_update._BoundReview(self.candidate, self.previous, self.notes, wrong)

    def test_the_published_snapshot_throws_170_255_javelins_and_a_300_450_third(self):
        snap = tft.load_snapshot(18, '18.2')
        nidalee = snap.unit('Nidalee')
        self.assertEqual([tft.curve_at(nidalee['curve']['EmpoweredDamage'], s) for s in (1, 2, 3, 4)], [170, 255, 2000, 3000])
        self.assertEqual([tft.curve_at(nidalee['curve']['ThirdAttackEmpoweredDamage'], s) for s in (1, 2, 3, 4)], [300, 450, 3000, 5000])
        calcs = {name.split('.')[-1]: calc['terms'][0]['coefficient'] for name, calc in nidalee['calcs'].items()
                 if calc['terms'] and calc['terms'][0].get('row', '').endswith('EmpoweredDamage')}
        self.assertEqual(calcs, {'MagicDamageCalc1': [170, 255, 2000, 3000], 'MagicDamageCalc2': [300, 450, 3000, 5000]})
        self.assertNotIn('EmpoweredDamage', snap.overrides['units']['TFT18_Nidalee']['curve'])
        findings, _ = tft.check_patch_notes(snap)
        self.assertEqual([f for f in findings if f['status'] != 'current'], [])
        self.assertEqual(snap.audit['reviews'], [self.manifest])
        self.assertEqual(snap.audit['automatic']['reviewManifestHash'], tft.json_hash(self.manifest))


class TestPublished18_3Review(unittest.TestCase):
    """The real 18.2b -> 18.3 review, replayed on the archived inputs (patch-reviews/18.3-review.md)."""

    @classmethod
    def setUpClass(cls):
        cls.previous = tft.load_snapshot(18, '18.2b')
        cls.published = tft.load_snapshot(18, '18.3')
        active = Path(cls.published.dir)
        cls.manifest = json.loads((active.parent / 'patch-reviews' / '18.3.json').read_text())
        cls.tmp = tempfile.TemporaryDirectory()
        staging = Path(cls.tmp.name) / '18.3'
        staging.mkdir()
        for name in ('metatft.json', 'bins.json', 'patchnotes.json', 'meta.json', 'communitydragon.json'):
            shutil.copyfile(active / name, staging / name)
        shutil.copyfile(Path(cls.previous.dir) / 'overrides.json', staging / 'overrides.json')
        cls.candidate = tft.Snapshot(18, '18.3', directory=str(staging))
        cls.notes = json.loads((staging / 'patchnotes.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def reconcile(self, manifest):
        with contextlib.redirect_stderr(io.StringIO()):   # Ivern's unmodeled-scaling warning
            return tft_update.reconcile(self.candidate, self.previous, self.notes, review=manifest)

    def test_the_manifest_reproduces_the_published_overrides_and_checks(self):
        overrides, audit = self.reconcile(self.manifest)
        self.assertEqual(overrides, self.published.overrides)
        self.assertEqual(audit['checks'], self.published.audit['checks'])
        self.assertEqual(len(audit['checks']), 147)
        self.assertEqual(len(audit['automatic']['appliedChanges']), 29)
        self.assertEqual(audit['sourceCrossCheck'], self.published.audit['sourceCrossCheck'])

    def test_without_its_records_the_patch_stops_where_the_scheduled_runs_did(self):
        bare = {**self.manifest, 'mappings': [], 'dispositions': [], 'sourceDisagreements': []}
        with self.assertRaisesRegex(tft_update.ReviewRequired,
                                    'Blossom Charms Animate Shop Duration: trait change needs an explicit reviewed field mapping'):
            self.reconcile(bare)

    def test_the_gromp_base_ad_row_needs_its_acknowledgement(self):
        # A single base value spread over four stars sits 70% from the per-star
        # AutoAttackDamage row, and the 30%-slow rows look closer to the guard.
        stripped = deepcopy(self.manifest)
        mapping = next(m for m in stripped['mappings'] if m['target'].get('row') == 'AutoAttackDamage')
        self.assertEqual(mapping['siblingRowAcknowledgement']['rows'], ['SlowAmountAD', 'SlowAmountAd'])
        del mapping['siblingRowAcknowledgement']
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'AutoAttackDamage may be the wrong row'):
            tft_update._BoundReview(self.candidate, self.previous, self.notes, stripped)

    def test_the_published_decisions(self):
        snap = self.published
        at = lambda curve, positions: [tft.curve_at(curve, p) for p in positions]
        # Riot's note (and TFTraits), not the live client's 2/3/5/8
        self.assertEqual(at(snap.traits['DA_18_Invoker']['curve']['InvokerManaBonus'], (1, 2, 3, 4)), [3, 4, 6, 8])
        # 18.2 applied 60% while the row said 40%; 18.3 states the 60%
        nidalee = snap.unit('Nidalee')
        self.assertEqual(at(nidalee['forms']['AD']['curve']['ArmorIgnoreRatio'], (1, 2, 3)), [0.6, 0.6, 0.8])
        # The AD form's attack damage comes from its own row, so the stat alone would not reach a fight
        gromp = snap.unit('Gromp')
        self.assertEqual(tft.kit_spec(gromp, 1, 'AD')['baseAd'], 50)
        self.assertEqual(tft.kit_spec(gromp, 1, 'AD')['stats']['as'], 0.75)
        self.assertEqual(tft.kit_spec(gromp, 1, 'AP')['baseAd'], 30)
        findings, _ = tft.check_patch_notes(snap)
        self.assertEqual([f for f in findings if f['status'] != 'current'], [])
        explained = {(d['api'], d['stat']): (d['effective'], d['communitydragon'], d['disposition'])
                     for d in snap.audit['sourceCrossCheck']['explained']}
        self.assertEqual(explained[('TFT18_MasterYi', 'ad')], (60.0, 62.0, 'unresolved-riot-note-kept'))
        self.assertEqual(len(explained), 7)
        self.assertEqual([r['patch'] for r in snap.audit['reviews']], ['18.2', '18.2b', '18.3'])
        self.assertEqual(snap.audit['reviews'][-1], self.manifest)
        self.assertEqual(snap.audit['automatic']['reviewManifestHash'], tft.json_hash(self.manifest))


class TestLookupChanges(Base):
    def record(self, target, lookup, decision='accept-lookup', **extra):
        before = tft_update._values(self.previous, target)
        return {'target': target, 'decision': decision, 'observedBefore': before, 'lookup': lookup,
                'expected': lookup if decision == 'accept-lookup' else before,
                'evidence': ['CommunityDragon says so too.'], 'reason': 'The regenerated lookup caught up.', **extra}

    def test_an_unannounced_value_is_accepted_only_through_its_own_record(self):
        raw = deepcopy(self.raw)
        raw['units'][2]['stats']['hp'] = 900
        candidate, notes = self.candidate(raw=raw)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'base-stat change'):
            tft_update.reconcile(candidate, self.previous, notes)
        record = self.record({'kind': 'unit', 'api': 'TFT18_Soraka', 'stat': 'hp'}, [900])
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, lookupChanges=[record]))
        self.assertEqual(checked.units['TFT18_Soraka']['stats']['hp'], 900)
        self.assertNotIn('stats', checked.overrides['units']['TFT18_Soraka'])     # the lookup supplies it, unpinned
        self.assertEqual(audit['automatic']['lookupChanges'][0]['decision'], 'accept-lookup')
        self.assertEqual(audit['automatic']['policyVersion'], 2)

    def test_accepting_a_value_on_a_pinned_row_moves_the_pin_and_splits_no_other_star(self):
        raw = deepcopy(self.raw)
        raw['units'][2]['curveTable']['DamageAP'][2][1] = 1100
        calc = raw['units'][2]['ability']['attributeCalcs']['MagicDamageCalc1']
        calc['terms'][0]['coefficient'][2] = calc['values'][2] = 1100
        candidate, notes = self.candidate(raw=raw)
        record = self.record({'kind': 'unit', 'api': 'TFT18_Soraka', 'row': 'DamageAP', 'stars': [3]}, [1100])
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, lookupChanges=[record]))
        self.assertEqual(checked.overrides['units']['TFT18_Soraka']['curve']['DamageAP'], [225, 335, 1100, 2200])
        damage = [c for c in audit['checks'] if c['target'].get('row') == 'DamageAP']
        self.assertEqual([(c['target']['stars'], c['expected']) for c in damage], [([1, 2], [225, 335])])

    def test_a_better_source_can_keep_the_reviewed_value_against_the_lookup(self):
        raw = deepcopy(self.raw)
        raw['units'][0]['curveTable']['PassiveHealPercent'] = [[1, .03], [2, .03], [3, .04], [4, .04]]
        candidate, notes = self.candidate(raw=raw)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unexplained lookup'):
            tft_update.reconcile(candidate, self.previous, notes)
        target = {'kind': 'unit', 'api': 'TFT18_Amumu', 'row': 'PassiveHealPercent', 'stars': [1, 2]}
        record = self.record(target, [.03, .03], decision='keep-reviewed-value')
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, lookupChanges=[record]))
        self.assertEqual(tft_update._values(checked, target), [.025, .025])
        kept = audit['checks'][-1]
        self.assertEqual((kept['expected'], kept['lookupValue'], kept['manualOnly']), ([.025, .025], [.03, .03], True))

    def test_a_record_must_describe_the_exact_staged_change(self):
        raw = deepcopy(self.raw)
        raw['units'][2]['stats']['hp'] = 900
        candidate, notes = self.candidate(raw=raw)
        target = {'kind': 'unit', 'api': 'TFT18_Soraka', 'stat': 'hp'}
        good = self.record(target, [900])
        cases = {'staged lookup does not hold': {'lookup': [950], 'expected': [950]},
                 'baseline no longer matches': {'observedBefore': [800]},
                 'needs decision': {'decision': 'whatever'},
                 'needs decision ': {'expected': [850]},
                 'needs evidence': {'evidence': []},
                 'needs an explanation': {'reason': ''},
                 'invalid reviewed lookup': {'lookup': ['900']}}
        for message, change in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(tft_update.ReviewRequired, message.strip()):
                tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, lookupChanges=[{**good, **change}]))
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'same coordinate twice'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, lookupChanges=[good, good]))
        unchanged = self.record({'kind': 'unit', 'api': 'TFT18_Soraka', 'stat': 'armor'}, [40])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'not a change'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, lookupChanges=[good, unchanged]))

    def test_a_coordinate_cannot_be_both_mapped_from_a_note_and_accepted_from_the_lookup(self):
        line = fixtures.change('Soraka Health', '850', '900')
        raw = deepcopy(self.raw)
        raw['units'][2]['stats']['hp'] = 900
        candidate, notes = self.candidate([line], raw=raw)
        target = {'kind': 'unit', 'api': 'TFT18_Soraka', 'stat': 'hp'}
        mapping = {'change': line, 'target': target, 'expected': [900], 'observedBefore': [850], 'reason': 'Note.'}
        review = self.manifest(candidate, notes, mappings=[mapping], lookupChanges=[self.record(target, [900])])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'same coordinate twice'):
            tft_update.reconcile(candidate, self.previous, notes, review=review)

    def test_a_summon_and_a_traits_inactive_column_have_an_address(self):
        golem = fixtures.unit('Golem')
        golem.update(shopUnit=False, apiName='TFT18_GolemSummon')
        raw = deepcopy(self.raw)
        raw['units'].append(golem)
        raw['traits'].append({'apiName': 'DA_18_Solar', 'name': 'Solar', 'effects': [{'minUnits': 3, 'style': 1}],
                              'curveTable': {'PercentIncreasePer3Star': [[0, .015], [1, .015]]}})
        self.rebase(raw)
        changed = deepcopy(raw)
        changed['units'][-1]['stats']['hp'] = 900
        changed['traits'][-1]['curveTable']['PercentIncreasePer3Star'] = [[0, .01], [1, .01]]
        candidate, notes = self.candidate(raw=changed)
        records = [self.record({'kind': 'unit', 'api': 'TFT18_GolemSummon', 'stat': 'hp'}, [900]),
                   self.record({'kind': 'trait', 'api': 'DA_18_Solar', 'row': 'PercentIncreasePer3Star', 'columns': [0, 1]}, [.01, .01])]
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, lookupChanges=records))
        self.assertEqual(checked.extras['TFT18_GolemSummon']['stats']['hp'], 900)
        # a patch-note mapping still may not address the inactive column
        line = fixtures.change('Solar Bonus', '1.5%', '1%', section='TRAITS')
        candidate, notes = self.candidate([line], raw=raw)
        mapping = {'change': line, 'target': {'kind': 'trait', 'api': 'DA_18_Solar', 'row': 'PercentIncreasePer3Star', 'columns': [0]},
                   'expected': [.01], 'observedBefore': [.015], 'reason': 'x'}
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'invalid reviewed target'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, mappings=[mapping]))

    def test_four_star_attack_damage_that_is_no_longer_base_times_1_5_cubed(self):
        raw = deepcopy(self.raw)
        raw['units'][2]['stats']['damageByStar'] = [40, 60, 90, 135]
        raw['units'][2]['curveTable']['AutoAttackDamage'] = [[1, 40], [2, 60], [3, 90], [4, 135]]
        self.rebase(raw)
        changed = deepcopy(raw)
        changed['units'][2]['stats']['damageByStar'] = [40, 60, 90, 120]
        changed['units'][2]['curveTable']['AutoAttackDamage'][3][1] = 120
        candidate, notes = self.candidate(raw=changed)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unexplained'):
            tft_update.reconcile(candidate, self.previous, notes)
        record = self.record({'kind': 'unit', 'api': 'TFT18_Soraka', 'row': 'AutoAttackDamage', 'stars': [4]}, [120])
        self.reviewed(candidate, notes, self.manifest(candidate, notes, lookupChanges=[record]))
        changed['units'][2]['stats']['damageByStar'] = [40, 60, 90, 121]     # no longer the row's copy
        candidate, notes = self.candidate(raw=changed)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'damageByStar'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, lookupChanges=[record]))

    def test_a_differently_cased_tooltip_copy_of_a_trait_row_follows_the_row(self):
        row = [[0, 1], [3, 1.06], [4, 1.06]]
        raw = deepcopy(self.raw)
        raw['traits'].append({'apiName': 'DA_Riftbeast18', 'name': 'Riftbeast', 'effects': [{'minUnits': 3, 'style': 1}],
                              'curveTable': {'CapstoneASPD': deepcopy(row)}, 'curveValues': {'CapstoneAspd': deepcopy(row)}})
        overrides = deepcopy(self.overrides)
        overrides['traits']['DA_Riftbeast18'] = {'curve': {'CapstoneASPD': {'3': 1.05, '4': 1.05}}}
        self.audit['checks'].append({'what': 'Riftbeast (7) Stats', 'expected': [1.05, 1.05],
                                     'target': {'kind': 'trait', 'api': 'DA_Riftbeast18', 'row': 'CapstoneASPD', 'columns': [3, 4]}})
        self.rebase(raw, overrides)
        caught_up = deepcopy(raw)
        for table, name in (('curveTable', 'CapstoneASPD'), ('curveValues', 'CapstoneAspd')):
            caught_up['traits'][-1][table][name] = [[0, 1], [3, 1.05], [4, 1.05]]
        candidate, notes = self.candidate(raw=caught_up, patch='18.1d')
        tft_update.reconcile(candidate, self.previous, notes)
        caught_up['traits'][-1]['curveValues']['CapstoneAspd'][1][1] = 1.04     # the copy alone says something else
        candidate, notes = self.candidate(raw=caught_up, patch='18.1d')
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unexplained lookup change'):
            tft_update.reconcile(candidate, self.previous, notes)


class TestTraitCalculationCaches(Base):
    OLD, NEW = [.07, .10, .14, .18], [.07, .12, .16, .20]

    def setUp(self):
        super().setUp()
        self.api = 'DA_18_Lunar'
        self.target = {'kind': 'trait', 'api': self.api, 'row': 'AbilityPower', 'columns': [1, 2, 3, 4]}
        self.line = fixtures.change('Lunar Ability Power', '7/10/14/18%', '7/12/16/20%', section='TRAITS')
        calc = {'resolved': True, 'terms': [
            {'type': 'flat', 'op': 'add', 'row': 'AbilityPower', 'values': deepcopy(self.OLD)},
            {'type': 'flat', 'op': 'add', 'row': 'LunarMultiplier', 'values': [2, 2, 2, 2]}],
            'values': [value + 2 for value in self.OLD]}
        trait = {'apiName': self.api, 'name': 'Lunar', 'effects': [{'minUnits': 2, 'style': 1}],
                 'curveTable': {'AbilityPower': [[0, .07], *[[column, value] for column, value in enumerate(self.OLD, 1)]],
                                'LunarMultiplier': [[column, 2] for column in range(5)],
                                'UnrelatedValue': [[0, .3], [1, .3]]},
                 'attributeCalcs': {'GenericCalc1': calc},
                 'attributeValues': {'GenericCalc1': deepcopy(calc['values'])}}
        raw = deepcopy(self.raw)
        raw['traits'].append(trait)
        self.rebase(raw)

    def changed_raw(self):
        raw = deepcopy(self.raw)
        trait = raw['traits'][-1]
        trait['curveTable']['AbilityPower'] = [[0, .07], *[[column, value] for column, value in enumerate(self.NEW, 1)]]
        calc = trait['attributeCalcs']['GenericCalc1']
        term = calc['terms'][0]
        term['coefficient' if term['type'] == 'scaled' else 'values'] = deepcopy(self.NEW)
        calc['values'] = [value + 2 for value in self.NEW]
        trait['attributeValues']['GenericCalc1'] = deepcopy(calc['values'])
        return raw

    def mapping(self):
        return {'change': self.line, 'target': self.target, 'observedBefore': self.OLD, 'expected': self.NEW,
                'numericEncoding': {'scale': .01, 'offset': 0}, 'reason': 'The official AP row names these breakpoint columns.'}

    def test_flat_trait_calculation_caches_follow_their_mapped_rows_without_a_cache_disposition(self):
        candidate, notes = self.candidate([self.line], raw=self.changed_raw())
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, mappings=[self.mapping()]))
        self.assertEqual(tft_update._values(checked, self.target), self.NEW)
        self.assertEqual(checked.raw['traits'][-1]['attributeValues']['GenericCalc1'], [2.07, 2.12, 2.16, 2.20])

    def test_flat_trait_caches_reject_corrupt_old_or_new_calculation_and_attribute_values(self):
        baseline = deepcopy(self.raw)
        for when, key in (('old', 'attributeCalcs'), ('new', 'attributeCalcs'),
                          ('old', 'attributeValues'), ('new', 'attributeValues')):
            with self.subTest(when=when, key=key):
                raw = deepcopy(baseline)
                if when == 'old':
                    values = raw['traits'][-1][key]['GenericCalc1']
                    (values['values'] if key == 'attributeCalcs' else values)[1] += 1
                self.rebase(raw)
                changed = self.changed_raw()
                if when == 'new':
                    values = changed['traits'][-1][key]['GenericCalc1']
                    (values['values'] if key == 'attributeCalcs' else values)[1] += 1
                candidate, notes = self.candidate([self.line], raw=changed)
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified calculation cache'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, mappings=[self.mapping()]))

    def test_trait_caches_never_assume_stats_for_scaled_runtime_or_unknown_terms(self):
        baseline = deepcopy(self.raw)
        for kind in ('scaled', 'runtime', 'unknown'):
            with self.subTest(kind=kind):
                raw = deepcopy(baseline)
                term = raw['traits'][-1]['attributeCalcs']['GenericCalc1']['terms'][0]
                term['type'] = kind
                if kind == 'scaled':
                    term['scaling'] = 'AbilityPower'
                    term['coefficient'] = term.pop('values')
                self.rebase(raw)
                candidate, notes = self.candidate([self.line], raw=self.changed_raw())
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'trait calculation contains runtime or scaled terms'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, mappings=[self.mapping()]))

    def test_trait_cache_arithmetic_does_not_approve_other_rows_or_formula_changes(self):
        for case, message in (('row', 'unexplained lookup change'), ('formula', 'unverified formula')):
            with self.subTest(case=case):
                changed = self.changed_raw()
                if case == 'row':
                    changed['traits'][-1]['curveTable']['UnrelatedValue'][1][1] = .5
                else:
                    changed['traits'][-1]['attributeCalcs']['GenericCalc1']['terms'][1]['op'] = 'multiply'
                candidate, notes = self.candidate([self.line], raw=changed)
                with self.assertRaisesRegex(tft_update.ReviewRequired, message):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, mappings=[self.mapping()]))


class TestDefinitionDispositions(Base):
    def disposition(self, path, before, after):
        return {'path': path, 'before': before, 'after': after, 'disposition': 'presentation-only', 'reason': 'Renamed, same numbers.'}

    def root_leaf_fixture(self):
        raw = deepcopy(self.raw)
        role = 'DA_Role_MagicSpecialist_Manaless'
        raw['roleData'][role] = {'name': 'Magic Specialist', 'roleTags': ['Role.Magic', 'Role.Specialist'],
                                 'resourceType': 'Mana.ResourceType.Mana'}
        raw['units'][2]['role'] = role
        raw['units'][2]['stats'].update(mana=0, initialMana=0)
        raw['unitAssetNames'] = {'DA_18_GnarBig': 'TFT18_Gnar'}
        self.rebase(raw)
        changed = deepcopy(raw)
        changed['roleData'][role]['resourceType'] = 'Mana.ResourceType.None'
        changed['unitAssetNames']['DA_18_GnarBig'] = 'TFT18_GnarBig'
        records = [self.disposition(f'roleData.{role}.resourceType', 'Mana.ResourceType.Mana', 'Mana.ResourceType.None'),
                   self.disposition('unitAssetNames.DA_18_GnarBig', 'TFT18_Gnar', 'TFT18_GnarBig')]
        return changed, records

    def test_known_root_leaves_need_separate_exact_records_and_keep_the_audit(self):
        raw, records = self.root_leaf_fixture()
        candidate, notes = self.candidate(raw=raw)
        for absent in ([], records[:1], records[1:]):
            with self.subTest(records=absent), self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified role definitions or lookup structure'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=absent))
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, definitionDispositions=records))
        self.assertEqual(checked.units['TFT18_Soraka']['resource'], 'None')
        for field in ('stats', 'kind', 'roleTags'):
            self.assertEqual(checked.units['TFT18_Soraka'][field], self.previous.units['TFT18_Soraka'][field])
        for record in records:
            self.assertIn({'kind': 'reviewed-definition', 'path': record['path'],
                           'disposition': record['disposition'], 'reason': record['reason']},
                          audit['automatic']['definitionChanges'])

    def test_known_root_leaf_records_reject_wrong_values_stale_records_and_broad_parents(self):
        raw, records = self.root_leaf_fixture()
        candidate, notes = self.candidate(raw=raw)
        role = 'DA_Role_MagicSpecialist_Manaless'
        bad_records = [
            {**records[0], 'before': 'Mana.ResourceType.Energy'},
            {**records[0], 'after': 'Mana.ResourceType.Rage'},
            self.disposition('roleData', self.previous.raw['roleData'], raw['roleData']),
            self.disposition(f'roleData.{role}', self.previous.raw['roleData'][role], raw['roleData'][role]),
        ]
        for bad in bad_records:
            with self.subTest(record=bad), self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified role definitions or lookup structure'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=[bad, records[1]]))
        candidate, notes = self.candidate()
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'does not contain'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=records))

    def test_known_root_leaf_approval_cannot_cover_other_tags_aliases_or_roots(self):
        base_raw, records = self.root_leaf_fixture()
        role = 'DA_Role_MagicSpecialist_Manaless'
        for case in ('role-tag', 'unit-alias', 'other-root', 'new-root', 'new-role-key'):
            with self.subTest(case=case):
                raw = deepcopy(base_raw)
                if case == 'role-tag':
                    raw['roleData'][role]['roleTags'][0] = 'Role.Attack'
                elif case == 'unit-alias':
                    raw['unitAssetNames']['DA_18_GnarBig'] = 'TFT18_Soraka'
                elif case == 'other-root':
                    raw['roles']['caster'] = 'Attack Caster'
                elif case == 'new-root':
                    raw['combatSettings'] = {'multiplier': 2}
                elif case == 'new-role-key':
                    raw['roleData'][role]['newManaMechanic'] = True
                candidate, notes = self.candidate(raw=raw)
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified role definitions or lookup structure'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, definitionDispositions=records))

    def test_a_known_root_cannot_be_added_even_when_its_value_is_null(self):
        raw = deepcopy(self.raw)
        self.assertNotIn('unitAssetNames', raw)
        raw['unitAssetNames'] = None
        candidate, notes = self.candidate(raw=raw)
        for records in ([], [self.disposition('unitAssetNames', None, None)]):
            with self.subTest(records=records), self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified role definitions or lookup structure'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=records))

    def calculation_member_fixture(self):
        raw = deepcopy(self.raw)
        unit = raw['units'][2]
        unit['attributeCalcs'] = deepcopy(unit['ability']['attributeCalcs'])
        unit['attributeValues'] = {name: deepcopy(calc['values']) for name, calc in unit['attributeCalcs'].items()}
        unit['ability']['attributeValues'] = deepcopy(unit['attributeValues'])
        self.rebase(raw)
        added = deepcopy(raw)
        calc = {'resolved': True, 'terms': [{'type': 'flat', 'op': 'add', 'values': [1, 1, 1, 1]}],
                'values': [1, 1, 1, 1]}
        records = []
        for block, path in ((added['units'][2], 'TFT18_Soraka'),
                            (added['units'][2]['ability'], 'TFT18_Soraka.ability')):
            for key, value in (('attributeCalcs', calc), ('attributeValues', calc['values'])):
                block[key]['GenericCalc3'] = deepcopy(value)
                records.append(self.disposition(f'{path}.{key}.GenericCalc3', None, deepcopy(value)))
        return added, records

    def test_each_added_calculation_and_value_member_requires_its_exact_record(self):
        raw, records = self.calculation_member_fixture()
        candidate, notes = self.candidate(raw=raw)
        for missing in (records, *[[record] for record in records]):
            supplied = [record for record in records if record not in missing]
            with self.subTest(missing=[r['path'] for r in missing]), \
                    self.assertRaisesRegex(tft_update.ReviewRequired, 'definition structure changed'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=supplied))
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, definitionDispositions=records))
        self.assertEqual(checked.units['TFT18_Soraka']['calcs']['MagicDamageCalc1'],
                         self.previous.units['TFT18_Soraka']['calcs']['MagicDamageCalc1'])
        self.assertEqual(checked.units['TFT18_Soraka']['calcs']['GenericCalc3']['values'], [1, 1, 1, 1])
        for record in records:
            self.assertIn({'kind': 'reviewed-definition', 'path': record['path'],
                           'disposition': record['disposition'], 'reason': record['reason']},
                          audit['automatic']['definitionChanges'])

    def test_added_calculation_records_reject_wrong_or_stale_full_member_values(self):
        raw, records = self.calculation_member_fixture()
        candidate, notes = self.candidate(raw=raw)
        for bad in ({**records[0], 'before': {}}, {**records[0], 'after': {'resolved': True}},
                    {**records[0], 'after': {**records[0]['after'], 'values': [2, 2, 2, 2]}},
                    {**records[1], 'after': [2, 2, 2, 2]}):
            supplied = [bad if record['path'] == bad['path'] else record for record in records]
            with self.subTest(record=bad), self.assertRaisesRegex(tft_update.ReviewRequired, 'definition structure changed'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=supplied))
        candidate, notes = self.candidate()
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'does not contain'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=records))

    def test_calculation_parent_approval_and_other_formula_edits_do_not_cover_added_members(self):
        raw, records = self.calculation_member_fixture()
        candidate, notes = self.candidate(raw=raw)
        parent_records = []
        for before, after, path in ((self.previous.raw['units'][2], raw['units'][2], 'TFT18_Soraka'),
                                    (self.previous.raw['units'][2]['ability'], raw['units'][2]['ability'], 'TFT18_Soraka.ability')):
            for key in ('attributeCalcs', 'attributeValues'):
                parent_records.append(self.disposition(f'{path}.{key}', before[key], after[key]))
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'definition structure changed'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=parent_records))
        for case, message in (('formula', 'unverified formula'), ('cache', 'unverified calculation cache')):
            with self.subTest(case=case):
                changed = deepcopy(raw)
                calc = changed['units'][2]['ability']['attributeCalcs']['MagicDamageCalc1']
                if case == 'formula':
                    calc['terms'][0]['op'] = 'multiply'
                else:
                    calc['values'][0] += 1
                candidate, notes = self.candidate(raw=changed)
                with self.assertRaisesRegex(tft_update.ReviewRequired, message):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, definitionDispositions=records))

    def orphan_row_fixture(self, *, shop=False, addressable=False, array='values'):
        old, new = [.035, .0455, .056, .056], [.045, .0059, .072, .072]
        row, name = 'HealthPercentHeal', 'TFTCalculationAttributes.HealthCalc1'
        extra = fixtures.unit('SprykinSummonMelee')
        extra['shopUnit'] = shop
        term = {'type': 'flat' if array == 'values' else 'scaled', 'op': 'add', 'row': row, array: old}
        if array == 'coefficient':
            term['scaling'] = 'AbilityPower'
        calc = {'resolved': True, 'terms': [term], 'values': old}
        extra['attributeCalcs'] = {name: deepcopy(calc)}
        extra['attributeValues'] = {name: deepcopy(old)}
        extra['ability']['attributeCalcs'] = {name: deepcopy(calc)}
        extra['ability']['attributeValues'] = {name: deepcopy(old)}
        extra['ability']['footer'] = [{'row': row, 'values': [[star, value] for star, value in enumerate(old[:3], 1)]}]
        if addressable:
            extra['curveTable'][row] = [[star, value] for star, value in enumerate(old, 1)]
        raw = deepcopy(self.raw)
        raw['units'] = [unit for unit in raw['units'] if unit['apiName'] != extra['apiName']]
        raw['units'].append(extra)
        self.rebase(raw)
        changed = deepcopy(raw)
        unit = changed['units'][-1]
        records = []
        for block, path in ((unit, extra['apiName']), (unit['ability'], extra['apiName'] + '.ability')):
            block['attributeCalcs'][name]['terms'][0][array] = deepcopy(new)
            block['attributeCalcs'][name]['values'] = deepcopy(new)
            block['attributeValues'][name] = deepcopy(new)
            records.append(self.disposition(f'{path}.attributeCalcs.{name}.terms[0].{array}', old, new))
        footer = [[1, .045], [2, .00585], [3, .072]]  # the source footer has different two-star precision
        unit['ability']['footer'][0]['values'] = footer
        records.append(self.disposition(extra['apiName'] + '.ability.footer[0].values',
                                        extra['ability']['footer'][0]['values'], footer))
        return changed, records

    def test_non_shop_orphan_arrays_are_accepted_only_by_their_exact_array_records(self):
        for array in ('values', 'coefficient'):
            with self.subTest(array=array):
                raw, records = self.orphan_row_fixture(array=array)
                candidate, notes = self.candidate(raw=raw)
                for missing in (records, *[[record] for record in records]):
                    supplied = [record for record in records if record not in missing]
                    with self.subTest(missing=[r['path'] for r in missing]), self.assertRaises(tft_update.ReviewRequired):
                        tft_update.reconcile(candidate, self.previous, notes,
                                             review=self.manifest(candidate, notes, definitionDispositions=supplied))
                checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, definitionDispositions=records))
                self.assertEqual(checked.extras['TFT18_SprykinSummonMelee']['curve'], {})
                self.assertEqual(checked.extras['TFT18_SprykinSummonMelee']['stats'],
                                 self.previous.extras['TFT18_SprykinSummonMelee']['stats'])
                for record in records:
                    self.assertIn({'kind': 'reviewed-definition', 'path': record['path'],
                                   'disposition': record['disposition'], 'reason': record['reason']},
                                  audit['automatic']['definitionChanges'])

    def test_orphan_array_records_reject_wrong_stale_parent_and_corrupt_cache_values(self):
        raw, records = self.orphan_row_fixture()
        candidate, notes = self.candidate(raw=raw)
        for bad in ({**records[0], 'before': [.01] * 4}, {**records[0], 'after': [.09] * 4}):
            supplied = [bad, *records[1:]]
            with self.subTest(record=bad), self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified coefficient'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=supplied))
        parent = 'TFT18_SprykinSummonMelee.ability.footer[0]'
        broad = self.disposition(parent, self.previous.raw['units'][-1]['ability']['footer'][0], raw['units'][-1]['ability']['footer'][0])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unexplained lookup change'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=[*records[:2], broad]))
        corrupted = deepcopy(raw)
        corrupted['units'][-1]['ability']['attributeCalcs']['TFTCalculationAttributes.HealthCalc1']['values'][0] += .01
        candidate, notes = self.candidate(raw=corrupted)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified calculation cache'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=records))
        candidate, notes = self.candidate()
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'does not contain'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=records))

    def test_orphan_array_dispositions_cannot_approve_shop_units_or_addressable_rows(self):
        for case in ('shop', 'previous-shop', 'candidate-shop', 'addressable', 'previous-row', 'candidate-row'):
            with self.subTest(case=case):
                raw, records = self.orphan_row_fixture(shop=case in ('shop', 'previous-shop'),
                                                       addressable=case in ('addressable', 'previous-row'))
                unit = raw['units'][-1]
                if case in ('previous-shop', 'candidate-shop'):
                    unit['shopUnit'] = case == 'candidate-shop'
                    records.append(self.disposition(unit['apiName'] + '.shopUnit', not unit['shopUnit'], unit['shopUnit']))
                elif case == 'previous-row':
                    old = unit['curveTable'].pop('HealthPercentHeal')
                    records.append(self.disposition(unit['apiName'] + '.curveTable.HealthPercentHeal', old, None))
                elif case == 'candidate-row':
                    curve = [[star, value] for star, value in enumerate([.045, .0059, .072, .072], 1)]
                    unit['curveTable']['HealthPercentHeal'] = curve
                    records.append(self.disposition(unit['apiName'] + '.curveTable.HealthPercentHeal', None, curve))
                candidate, notes = self.candidate(raw=raw)
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified coefficient'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, definitionDispositions=records))
        # Addressable extra rows still work through the ordinary numeric lookup review.
        raw, _ = self.orphan_row_fixture(addressable=True)
        values = [.045, .0059, .072, .072]
        raw['units'][-1]['curveTable']['HealthPercentHeal'] = [[star, value] for star, value in enumerate(values, 1)]
        raw['units'][-1]['ability']['footer'][0]['values'][1][1] = values[1]
        candidate, notes = self.candidate(raw=raw)
        target = {'kind': 'unit', 'api': 'TFT18_SprykinSummonMelee', 'row': 'HealthPercentHeal', 'stars': [1, 2, 3, 4]}
        record = TestLookupChanges.record(self, target, values)
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, lookupChanges=[record]))
        self.assertEqual([tft.curve_at(checked.extras[target['api']]['curve'][target['row']], star) for star in target['stars']], values)

    def test_a_text_change_needs_its_exact_record(self):
        raw = deepcopy(self.raw)
        raw['units'][2]['ability']['desc'] = 'A renamed spell.'
        candidate, notes = self.candidate(raw=raw)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified formula, mechanic, or value at TFT18_Soraka.ability.desc'):
            tft_update.reconcile(candidate, self.previous, notes)
        good = self.disposition('TFT18_Soraka.ability.desc', 'A spell.', 'A renamed spell.')
        _, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, definitionDispositions=[good]))
        self.assertIn({'kind': 'reviewed-definition', 'path': good['path'], 'disposition': 'presentation-only', 'reason': good['reason']},
                      audit['automatic']['definitionChanges'])
        for bad in ({**good, 'after': 'A renamed spell that also burns.'}, {**good, 'before': 'Another spell.'}):
            with self.subTest(bad=bad), self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified formula'):
                tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, definitionDispositions=[bad]))

    def test_a_record_for_a_change_that_did_not_happen_is_refused(self):
        candidate, notes = self.candidate()
        stale = self.disposition('TFT18_Soraka.ability.desc', 'A spell.', 'A renamed spell.')
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'does not contain'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, definitionDispositions=[stale]))
        for bad in ({**stale, 'path': ''}, {k: v for k, v in stale.items() if k != 'after'}, {**stale, 'disposition': ''}, {**stale, 'reason': ''}):
            with self.subTest(bad=bad), self.assertRaises(tft_update.ReviewRequired):
                tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, definitionDispositions=[bad]))

    def test_added_non_shop_unit_and_unused_trait_require_exact_root_records(self):
        extra = fixtures.unit('GnarBig')
        extra['shopUnit'] = False
        trait = {'apiName': 'TFT18_Prowler', 'name': 'Prowler', 'units': [], 'effects': [], 'curveTable': {}}
        raw = deepcopy(self.raw)
        raw['units'].append(extra)
        raw['traits'].append(trait)
        candidate, notes = self.candidate(raw=raw)
        records = [self.disposition(value['apiName'], None, value) for value in (extra, trait)]
        for absent in ([], records[:1], records[1:]):
            with self.subTest(records=absent), self.assertRaisesRegex(tft_update.ReviewRequired, 'new or removed'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=absent))
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, definitionDispositions=records))
        self.assertEqual(set(checked.units), set(self.previous.units))
        self.assertIn(extra['apiName'], checked.extras)
        self.assertIn(trait['apiName'], checked.traits)
        for record in records:
            self.assertIn({'kind': 'reviewed-definition', 'path': record['path'],
                           'disposition': record['disposition'], 'reason': record['reason']},
                          audit['automatic']['definitionChanges'])

    def test_added_definition_record_must_match_the_full_entity_and_absent_baseline(self):
        extra = fixtures.unit('GnarBig')
        extra['shopUnit'] = False
        raw = deepcopy(self.raw)
        raw['units'].append(extra)
        candidate, notes = self.candidate(raw=raw)
        good = self.disposition(extra['apiName'], None, extra)
        incomplete = {key: value for key, value in extra.items() if key != 'ability'}
        changed = deepcopy(extra)
        changed['stats']['hp'] += 1
        for bad in ({**good, 'before': {}}, {**good, 'after': incomplete}, {**good, 'after': changed},
                    {**good, 'path': extra['apiName'] + '.stats'}):
            with self.subTest(record=bad), self.assertRaisesRegex(tft_update.ReviewRequired, 'new or removed unit'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=[bad]))
        candidate, notes = self.candidate()  # the reviewed addition is no longer present
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'does not contain'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=[good]))

    def test_new_shop_champion_is_refused_even_with_an_exact_definition_record(self):
        base_raw = deepcopy(self.raw)
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                raw = deepcopy(base_raw)
                extra = fixtures.unit('NewChampion')
                if legacy:
                    for unit in [*raw['units'], extra]:
                        unit.pop('shopUnit')
                        unit['traits'] = ['Existing']
                self.rebase(raw)
                raw['units'].append(extra)
                candidate, notes = self.candidate(raw=raw)
                record = self.disposition(extra['apiName'], None, extra)
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'new or removed unit'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, definitionDispositions=[record]))

    def test_legacy_shop_classification_cannot_be_changed_by_an_extra_unit(self):
        raw = deepcopy(self.raw)
        for unit in raw['units']:
            unit.pop('shopUnit')
            unit['traits'] = ['Existing']
        self.rebase(raw)
        extra = fixtures.unit('GnarBig')
        extra['shopUnit'] = False
        raw['units'].append(extra)  # the first flag would demote every legacy champion
        candidate, notes = self.candidate(raw=raw)
        record = self.disposition(extra['apiName'], None, extra)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'new or removed unit'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=[record]))

    def test_added_trait_with_champion_members_or_modeled_effects_is_refused(self):
        trait = {'apiName': 'TFT18_Prowler', 'name': 'Prowler', 'units': [], 'effects': [], 'curveTable': {}}
        base_raw = deepcopy(self.raw)
        for case in ('declared-member', 'current-reference', 'previous-reference', 'modeled-effect'):
            with self.subTest(case=case):
                previous_raw = deepcopy(base_raw)
                if case == 'previous-reference':
                    previous_raw['units'][2]['traitApiNames'] = [trait['apiName']]
                self.rebase(previous_raw)
                raw = deepcopy(previous_raw)
                added = deepcopy(trait)
                if case == 'declared-member':
                    added['units'] = [{'unit': 'TFT18_Soraka'}]
                elif case == 'current-reference':
                    raw['units'][2]['traitApiNames'] = [trait['apiName']]
                elif case == 'previous-reference':
                    raw['units'][2]['traitApiNames'] = []
                raw['traits'].append(added)
                candidate, notes = self.candidate(raw=raw)
                records = [self.disposition(added['apiName'], None, added)]
                if case in ('current-reference', 'previous-reference'):
                    records.append(self.disposition('TFT18_Soraka.traitApiNames',
                                                    previous_raw['units'][2]['traitApiNames'], raw['units'][2]['traitApiNames']))
                effects = {trait['apiName']: {}} if case == 'modeled-effect' else {}
                with patch.object(tft, 'load_trait_effects', return_value=effects), \
                        self.assertRaisesRegex(tft_update.ReviewRequired, 'new or removed trait'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, definitionDispositions=records))

    def test_unit_and_trait_removals_remain_refused_with_exact_records(self):
        extra = fixtures.unit('GnarBig')
        extra['shopUnit'] = False
        trait = {'apiName': 'TFT18_Prowler', 'name': 'Prowler', 'units': [], 'effects': [], 'curveTable': {}}
        previous_raw = deepcopy(self.raw)
        previous_raw['units'].append(extra)
        previous_raw['traits'].append(trait)
        self.rebase(previous_raw)
        for kind, value in (('unit', extra), ('trait', trait)):
            with self.subTest(kind=kind):
                raw = deepcopy(previous_raw)
                raw[kind + 's'] = [entity for entity in raw[kind + 's'] if entity['apiName'] != value['apiName']]
                candidate, notes = self.candidate(raw=raw)
                record = self.disposition(value['apiName'], value, None)
                with self.assertRaisesRegex(tft_update.ReviewRequired, f'new or removed {kind}'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, definitionDispositions=[record]))

    def test_a_new_trait_row_is_acknowledged_by_name_and_value(self):
        raw = deepcopy(self.raw)
        raw['traits'].append({'apiName': 'DA_18_Summoner', 'name': 'Summoner', 'effects': [{'minUnits': 2, 'style': 1}],
                              'curveTable': {'DamageMult': [[0, 1], [1, 1.45]]}})
        self.rebase(raw)
        added = deepcopy(raw)
        added['traits'][-1]['curveTable']['AzirDamageMult'] = [[0, 1], [1, 1.2]]
        candidate, notes = self.candidate(raw=added)
        with self.assertRaisesRegex(tft_update.ReviewRequired, r"new or missing curve rows at DA_18_Summoner.curveTable: \['AzirDamageMult'\]"):
            tft_update.reconcile(candidate, self.previous, notes)
        record = self.disposition('DA_18_Summoner.curveTable.AzirDamageMult', None, [[0, 1], [1, 1.2]])
        self.reviewed(candidate, notes, self.manifest(candidate, notes, definitionDispositions=[record]))

    def test_dropping_a_forms_stats_block_is_checked_not_believed(self):
        def with_form(mana):
            raw = deepcopy(self.raw)
            raw['units'][2]['extraAbilities'] = {'DA_18_Soraka_AP': {
                'variant': 'AP', 'curveTable': {}, 'attributeCalcs': {}, 'stats': {**raw['units'][2]['stats'], 'mana': mana}}}
            return raw

        for mana, message in ((30, None), (25, "the AP form's effective stats change")):
            with self.subTest(mana=mana):
                raw = with_form(mana)
                self.rebase(raw)
                dropped = deepcopy(raw)
                block = dropped['units'][2]['extraAbilities']['DA_18_Soraka_AP'].pop('stats')
                candidate, notes = self.candidate(raw=dropped)
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'definition structure changed'):
                    tft_update.reconcile(candidate, self.previous, notes)
                record = self.disposition('TFT18_Soraka.extraAbilities.DA_18_Soraka_AP.stats', block, None)
                review = self.manifest(candidate, notes, definitionDispositions=[record])
                if message:
                    with self.assertRaisesRegex(tft_update.ReviewRequired, message):
                        tft_update.reconcile(candidate, self.previous, notes, review=review)
                else:
                    self.reviewed(candidate, notes, review)


CDRAGON_STATS = {'hp': 850.0, 'damage': 40.0, 'attackSpeed': 0.75, 'armor': 40.0, 'magicResist': 40.0,
                 'initialMana': 0, 'mana': 30.0, 'range': 4.0}


class TestSourceCrossCheck(Base):
    """A small snapshot with a CommunityDragon export next to it."""
    def setUp(self):
        super().setUp()
        raw = deepcopy(self.raw)
        amumu, yi, soraka = raw['units']
        amumu['assetNames'], soraka['assetNames'] = ['DA_Amumu18'], ['DA_18_Soraka', 'DA_18_Soraka_Blossom']
        yi['assetNames'] = ['DA_18_MasterYi_AD', 'DA_18_MasterYi_AP']
        yi['curveTable']['AutoAttackDamageAP'] = [[1, 15], [2, 22.5], [3, 33.75], [4, 50.625]]
        self.cdragon = {'champions': [
            {'apiName': 'DA_Amumu18', 'stats': {**CDRAGON_STATS, 'initialMana': 30.0, 'mana': 125.0}},
            {'apiName': 'DA_18_Soraka', 'stats': {**CDRAGON_STATS, 'attackSpeed': 0.75000001192092896}},
            {'apiName': 'DA_18_Soraka_Blossom', 'stats': {**CDRAGON_STATS, 'armor': 45.0}},
            {'apiName': 'DA_18_MasterYi_AD', 'stats': {**CDRAGON_STATS, 'armor': 55.0, 'magicResist': 55.0}},
            {'apiName': 'DA_18_MasterYi_AP', 'stats': {**CDRAGON_STATS, 'damage': 15.0, 'armor': 55.0, 'magicResist': 55.0}},
        ], 'traits': [], 'items': []}
        self.rebase(raw)
        self.previous = self.with_cdragon(self.previous, self.cdragon)

    def with_cdragon(self, snap, cdragon):
        (Path(snap.dir) / 'communitydragon.json').write_text(json.dumps(cdragon))
        return tft.Snapshot(18, snap.patch, directory=snap.dir)

    def staged(self, cdragon, **kwargs):
        candidate, notes = self.candidate(**kwargs)
        return self.with_cdragon(candidate, cdragon), notes

    def disagreeing(self, **stats):
        cdragon = deepcopy(self.cdragon)
        cdragon['champions'][1]['stats'].update(stats)
        return cdragon

    def test_forms_variants_float_noise_and_missing_values(self):
        result = tft.source_disagreements(self.previous)
        self.assertEqual((result['compared'], result['unmodeled'], result['disagreements']), (4, ['DA_18_Soraka_Blossom'], []))
        cdragon = deepcopy(self.cdragon)
        cdragon['champions'][1]['stats'].update(hp=950.0, damage=None)
        cdragon['champions'][4]['stats']['damage'] = 60.0       # the AP form attacks with AutoAttackDamageAP (15), not stats.damage
        found = tft.source_disagreements(self.with_cdragon(self.previous, cdragon))['disagreements']
        self.assertEqual([(d['api'], d['asset'], d['form'], d['stat'], d['effective'], d['communitydragon']) for d in found], [
            ('TFT18_MasterYi', 'DA_18_MasterYi_AP', 'AP', 'ad', 15.0, 60.0),
            ('TFT18_Soraka', 'DA_18_Soraka', None, 'hp', 850, 950.0),
            ('TFT18_Soraka', 'DA_18_Soraka', None, 'ad', 40.0, None)])

    def record(self, **values):
        return {'api': 'TFT18_Soraka', 'asset': 'DA_18_Soraka', 'stat': 'hp', 'effective': 850, 'communitydragon': 950.0,
                'disposition': 'communitydragon-lags', 'evidence': ['Riot: Soraka Health 950 ⇒ 850.'], 'reason': 'CommunityDragon is behind.', **values}

    def test_a_new_disagreement_stops_an_unattended_run_and_a_review_explains_it(self):
        candidate, notes = self.staged(self.disagreeing(hp=950.0))
        with self.assertRaisesRegex(tft_update.ReviewRequired, r'unexplained lookup/CommunityDragon disagreement: Soraka hp \(DA_18_Soraka\): snapshot 850, CommunityDragon 950'):
            tft_update.reconcile(candidate, self.previous, notes)
        _, audit = tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, sourceDisagreements=[self.record()]))
        explained = audit['sourceCrossCheck']['explained']
        self.assertEqual([(e['stat'], e['disposition'], e['evidence']) for e in explained], [('hp', 'communitydragon-lags', ['Riot: Soraka Health 950 ⇒ 850.'])])
        self.assertEqual((audit['sourceCrossCheck']['comparedRecords'], audit['sourceCrossCheck']['unmodeledRecords']), (4, ['DA_18_Soraka_Blossom']))

    def test_a_review_must_describe_the_disagreement_that_exists(self):
        candidate, notes = self.staged(self.disagreeing(hp=950.0))
        for message, records in (('no longer matches the staged values', [self.record(communitydragon=900.0)]),
                                 ('the staged data does not have', [self.record(), self.record(stat='armor', effective=40, communitydragon=45.0)]),
                                 ('needs a unique api/asset/stat', [self.record(), self.record()]),
                                 ('needs evidence', [self.record(evidence=[])]), ('needs disposition', [self.record(disposition='')]),
                                 ('unexplained lookup/CommunityDragon disagreement', [])):
            with self.subTest(message=message), self.assertRaisesRegex(tft_update.ReviewRequired, message):
                tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, sourceDisagreements=records))

    def test_what_the_published_snapshot_already_held_does_not_block_an_unrelated_hotfix(self):
        self.previous = self.with_cdragon(self.previous, self.disagreeing(hp=950.0))
        candidate, notes = self.staged(self.disagreeing(hp=950.0), changes=[fixtures.change('Amumu Heal Max HP %', '2.5%', '3%')])
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes)
        self.assertEqual(audit['sourceCrossCheck']['explained'], [])
        self.assertEqual([(i['stat'], i['since']) for i in audit['sourceCrossCheck']['inherited']], [('hp', '18.1d')])
        # ...but a person reviewing the transition has to deal with it
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'the review needs a sourceDisagreements record'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes))
        # a value that moved is a new disagreement even on an unattended run
        candidate, notes = self.staged(self.disagreeing(hp=975.0), changes=[fixtures.change('Amumu Heal Max HP %', '2.5%', '3%')])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'CommunityDragon 975'):
            tft_update.reconcile(candidate, self.previous, notes)

    def test_an_explanation_is_carried_while_both_values_stand_and_the_audit_stays_stable(self):
        candidate, notes = self.staged(self.disagreeing(hp=950.0))
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, sourceDisagreements=[self.record()]))
        published = self.with_cdragon(self.snapshot('published', candidate.raw, notes, overrides, audit), self.disagreeing(hp=950.0))
        _, again = tft_update.reconcile(published, published, notes)
        self.assertEqual(again, audit)
        self.assertEqual([d['status'] for d in tft.check_sources(published)['disagreements']], ['explained'])
        gone = self.with_cdragon(self.snapshot('caught-up', candidate.raw, notes, overrides, audit), self.cdragon)
        _, later = tft_update.reconcile(gone, published, notes)
        self.assertEqual(later['sourceCrossCheck']['explained'], [])

    def check(self, snap):
        out = io.StringIO()
        with patch.object(tft, 'load_snapshot', return_value=snap), contextlib.redirect_stdout(out):
            try:
                tft.cmd_check(SimpleNamespace(set=18, patch=None))
                return 0, out.getvalue()
            except SystemExit as stop:
                return stop.code, out.getvalue()

    def test_tft_check_reports_every_disagreement_and_fails_on_one_a_reconciled_audit_does_not_know(self):
        older = self.with_cdragon(self.previous, self.disagreeing(hp=950.0))
        code, text = self.check(older)          # an audit from before the cross-check: reported, not failed
        self.assertEqual(code, 0)
        self.assertIn('SRC   Soraka         hp           snapshot 850 ≠ CommunityDragon 950', text)
        self.assertIn('This audit predates the source cross-check', text)
        candidate, notes = self.staged(self.disagreeing(hp=950.0))
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, sourceDisagreements=[self.record()]))
        published = self.with_cdragon(self.snapshot('published', candidate.raw, notes, overrides, audit), self.disagreeing(hp=950.0))
        code, text = self.check(published)
        self.assertEqual(code, 0)
        self.assertIn('ok    Soraka         hp           snapshot 850 ≠ CommunityDragon 950 (DA_18_Soraka)  communitydragon-lags', text)
        drifted = self.with_cdragon(published, self.disagreeing(hp=950.0, armor=45.0))
        code, text = self.check(drifted)
        self.assertEqual(code, 2)
        self.assertIn('SRC   Soraka         armor', text)


if __name__ == '__main__':
    unittest.main()
