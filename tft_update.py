"""Conservative, deterministic reconciliation of a staged TFT snapshot.

This module does not fetch, publish, warm caches, or change its arguments.
A previously bound audit is the trust anchor. New numeric notes must identify
existing fields; all other changes to modeled definitions require review.
"""
from __future__ import annotations

from copy import copy, deepcopy
import json
import math
from pathlib import Path
import re


class ReviewRequired(ValueError):
    """The staged inputs need a human review before publication."""


def _fail(message):
    raise ReviewRequired(message + "; review the staged snapshot and add an explicit audit target before publishing")


def _norm(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _label(value):
    return re.sub(r"(?:buff|nerf)$", "", _norm(value))


def _equal(a, b):
    if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
        return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=0, abs_tol=1e-9)
    return a == b


def _same(a, b):
    return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))


def _encode(values, scale, offset):
    # Source decimals such as 35% should serialize as .35, not an artifact
    # of multiplying the binary approximation of .01.
    return [round(value * scale + offset, 12) for value in values]


_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
_SERIES = re.compile(rf"\s*({_NUMBER}%?(?:\s*/\s*{_NUMBER}%?)*)\s*([^\d]*)\s*\Z")
_UNITS = {"", "%", "ad", "ap", "adas", "adap", "adapas", "hp", "mana", "armor", "mr", "s", "seconds", "g", "gold"}


def _numbers(value, what):
    match = _SERIES.fullmatch(str(value))
    if not match or _norm(match[2].replace("%", "")) not in _UNITS:
        _fail(f"{what}: unsupported numeric expression {value!r} (possibly a mechanics change)")
    return [float(x) for x in re.findall(_NUMBER, match[1])]


def _number_unit(value):
    match = _SERIES.fullmatch(str(value))
    suffix = _norm(match[2].replace('%', '')) if match else ''
    return {'seconds': 's', 'gold': 'g'}.get(suffix, suffix)


def _check_numeric_units(change, templates):
    # When both sides explicitly name a unit, adding/removing % can change
    # the formula even if the number is identical (10 AD versus 10% AD).
    # A side with no unit can simply be Riot's abbreviated repeated suffix.
    written_values = [change['old'], change['new'],
                      *(c.get('patchLine', {}).get('new', '') for c in templates)]
    percent_modes = {'%' in str(value) for value in written_values
                     if _number_unit(value) or '%' in str(value)}
    if len(percent_modes) > 1:
        _fail(f"{change['what']}: numeric percentage/flat units changed, which needs a mechanics review")
    units = {_number_unit(change[key]) for key in ('old', 'new')} - {''}
    units.update(_number_unit(c.get('patchLine', {}).get('new', '')) for c in templates)
    units.discard('')
    if len(units) > 1:
        _fail(f"{change['what']}: numeric units/scaling changed ({sorted(units)}), which needs a mechanics review")


def _positions(target):
    return target.get("stars", target.get("columns", [1]))


def _target_key(target):
    return (target["kind"], target["api"], target.get("form"), target.get("stat"), target.get("row"))


def _entity(snap, target):
    data = {"unit": snap.units, "item": snap.items, "trait": snap.traits}[target["kind"]].get(target["api"])
    if data is None and target["kind"] == "unit":
        # Summons and transformed forms are lookup units too (Snapshot.extras); a
        # reviewed change to one of them needs an address like any other.
        data = getattr(snap, "extras", {}).get(target["api"])
    if data is None:
        _fail(f"audit target {target['api']} no longer exists")
    if target.get("form"):
        data = data.get("forms", {}).get(target["form"])
        if data is None:
            _fail(f"audit form {target['api']} {target['form']} no longer exists")
    return data


def _values(snap, target):
    import tft
    entity = _entity(snap, target)
    if "stat" in target:
        value = (entity.get("stats") or {}).get(target["stat"])
        if value is None:
            _fail(f"missing stat {target}")
        return [value]
    curve = entity.get("curve", {}).get(target["row"])
    if not curve:
        _fail(f"missing curve row {target}")
    return [tft.curve_at(curve, p) for p in _positions(target)]


def _normalized(candidate, overrides):
    import tft
    snap = copy(candidate)
    snap.overrides = deepcopy(overrides)
    for group in ("units", "items", "traits"):
        snap.overrides.setdefault(group, {})
    shop = {u["apiName"] for u in tft.real_units(snap.raw)}
    all_units = {u["apiName"]: snap._unit(u) for u in snap.raw["units"]}
    snap.units = {key: value for key, value in all_units.items() if key in shop}
    snap.extras = {key: value for key, value in all_units.items() if key not in shop}
    snap.items = {x["apiName"]: snap._item(x) for x in snap.raw["items"]}
    snap.traits = {x["apiName"]: snap._trait(x) for x in snap.raw["traits"]}
    return snap


# The wrong-row guard. 18.2's "Nidalee Empowered Attack Damage: 285/425 AP ⇒ 300/450 AP"
# was reviewed onto EmpoweredDamage (170/255, the ordinary javelins) although the
# sibling ThirdAttackEmpoweredDamage (320/480) was the row Riot meant: the published
# javelins dealt 300/450 instead of 170/255. Both signs below were on the page.
_FAR_FROM_NOTE = 0.05    # the mapped row's previous value is further than this from the note's old value
_CLEARLY_CLOSER = 0.5    # ...and a sibling's distance is at most this share of the mapped row's
_ENCODINGS = ((1, 0), (.01, 0), (.01, 1), (-.01, 1))
_NOTE_SERIES = re.compile(rf"{_NUMBER}%?(?:\s*/\s*{_NUMBER}%?)*")


def _note_series(text):
    """Every a/b/c series in a note value, the parts of "15 + 30% AP" included."""
    return [[float(x) for x in re.findall(_NUMBER, part)] for part in _NOTE_SERIES.findall(str(text))]


def _spread(series, positions):
    if len(series) == 1:
        return series * len(positions)
    if any(p < 1 or p > len(series) for p in positions):
        return None
    return [series[p - 1] for p in positions]


def _gap(have, want):
    """The largest relative difference between two series of one length."""
    worst = 0.0
    for a, b in zip(have, want):
        scale = max(abs(a), abs(b))
        if scale:
            worst = max(worst, abs(a - b) / scale)
    return worst


def _better_sibling_rows(mapping, previous, lookups):
    """Rows of the mapped entity/form that fit a numeric note better than the mapped row.

    1. Old value: the mapped row's previous value is far from the note's old value
       (a stale source, by the reviewer's own account) while a sibling is clearly
       closer to it. A stale row with no such sibling passes: Kayle, Azir, Diana and
       Mama Beak in 18.2 were that.
    2. New value: upstream itself moved a sibling to exactly the note's new value
       and left the mapped row where it was.
    Returns {row: why}. `lookups` are the previous and the staged lookup with no
    override applied: sign 2 is about what upstream changed, and an override of ours
    that upstream never followed (Radiant Hand of Justice's AD/AP, 0.35 over a raw
    0.3) is not upstream moving.
    """
    import tft
    target, change = mapping['target'], mapping['change']
    positions, before, expected = _positions(target), mapping['observedBefore'], mapping['expected']
    rows = _entity(previous, target).get('curve', {})

    def at(curve):
        values = [tft.curve_at(curve, p) for p in positions] if curve else []
        if values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            return values
        return None

    found = {}
    stored = mapping.get('numericEncoding')
    encodings = [(stored['scale'], stored['offset'])] if stored else _ENCODINGS
    closest = None
    for series in _note_series(change.get('old', '')):
        spread = _spread(series, positions)
        if spread is None:
            continue
        for scale, offset in encodings:
            wanted = [value * scale + offset for value in spread]
            gap = _gap(before, wanted)
            if closest is None or gap < closest[0]:
                closest = gap, wanted
    if closest and closest[0] > _FAR_FROM_NOTE:
        gap, wanted = closest
        for row, curve in rows.items():
            values = at(curve)
            if row != target['row'] and values is not None and _gap(values, wanted) <= gap * _CLEARLY_CLOSER:
                found[row] = (f"held {values}, within {_gap(values, wanted):.0%} of the note's old {wanted}; "
                              f"{target['row']} held {before}, {gap:.0%} away")
    try:
        earlier, staged = (_entity(lookup, target).get('curve', {}) for lookup in lookups)
    except ReviewRequired:
        earlier = staged = {}
    mapped_now = at(staged.get(target['row']))
    if mapped_now is not None and not _same(mapped_now, expected):
        for row, curve in staged.items():
            now, was = at(curve), at(earlier.get(row))
            if (row != target['row'] and now is not None and was is not None
                    and _same(now, expected) and not _same(was, expected)):
                found.setdefault(row, f"went from {was} to the note's new {expected} in the staged lookup, "
                                      f"while {target['row']} stayed at {mapped_now}")
    return found


def _read_notes(snap):
    try:
        with (Path(snap.dir) / "patchnotes.json").open() as stream:
            return json.load(stream)
    except (OSError, ValueError) as exc:
        _fail(f"cannot read previous patch-note evidence: {exc}")


def _validate_sources(candidate, previous, notes, prior_notes):
    import tft
    if candidate.set_no != previous.set_no:
        _fail("a new TFT set needs drivers and a complete set audit")
    if tft.tft_patch_key(candidate.patch) < tft.tft_patch_key(previous.patch):
        _fail(f"refusing patch rollback from {previous.patch} to {candidate.patch}")
    if notes.get("patch") != candidate.patch:
        _fail(f"patch-note revision {notes.get('patch')!r} does not match {candidate.patch}")
    base = re.sub(r"[a-z]$", "", candidate.patch)
    expected_url = tft.PATCH_NOTES_URL.format(slug=base.replace(".", "-"))
    if notes.get("basePatch") != base or notes.get("url", "").rstrip("/") != expected_url.rstrip("/"):
        _fail("patch notes are not bound to the expected official Riot patch URL")
    if not notes.get("changes") or not isinstance(notes.get("notes"), list) or not notes["notes"]:
        _fail("patch notes lack full balance changes and mechanics bullets")
    if candidate.raw.get("_metadata", {}).get("set") != f"TFTSet{candidate.set_no}":
        _fail("lookup metadata names the wrong TFT set")
    audit = previous.audit or {}
    if not audit.get("checks") or audit.get("patch") != previous.patch:
        _fail("previous snapshot has no usable bound audit")
    for name, data in (("lookupHash", previous.raw), ("binsHash", previous.bins), ("patchNotesHash", prior_notes)):
        if audit.get(name) != tft.json_hash(data):
            _fail(f"previous audit {name} does not match its archived evidence")
    for check in audit["checks"]:
        if not _same(_values(previous, check["target"]), check["expected"]):
            _fail(f"previous audited value is inconsistent: {check['what']}")
    if candidate.bins != previous.bins:
        keys = sorted(k for k in set(candidate.bins) | set(previous.bins) if candidate.bins.get(k) != previous.bins.get(k))
        _fail(f"unverified timing/bin changes for {', '.join(keys[:5])}")


def _change_key(change):
    return (_label(change["what"]), str(change["old"]).strip(), str(change["new"]).strip(),
            _norm(change.get("update", "")), _norm(change.get("section", "")), _norm(change.get("major", "")))


def _entry_key(entry):
    # Mechanics text is evidence: 1.5%, 15%, and -15% must stay distinct.
    return tuple(" ".join(str(entry.get(key, "")).casefold().split())
                 for key in ("update", "major", "section", "parent", "text"))


def _change_category(change):
    section = _norm(change.get('section', ''))
    if any(word in section for word in ('augment', 'wisp')):
        return 'outside'
    if section.startswith(('unit', 'champion')):
        return 'unit'
    if section == 'traits':
        return 'trait'
    if any(word in section for word in ('item', 'artifact', 'radiant', 'emblem')):
        return 'item'
    return None


def _replace_check(checks, check):
    """Supersede only the coordinates actually reviewed, retaining the rest."""
    target = check['target']
    positions = set(_positions(target))
    kept, superseded = [], []
    for old in checks:
        if _target_key(old['target']) != _target_key(target):
            kept.append(old)
            continue
        remaining = [] if 'stat' in target else [
            (p, v) for p, v in zip(_positions(old['target']), old['expected']) if p not in positions]
        if len(remaining) == len(old['expected']):
            kept.append(old)
            continue
        # Keep lineage flat; copying its own lineage at every update would
        # double the audit size on successive patches.
        superseded.extend(deepcopy(old.get('supersededChecks', [])))
        superseded.append({k: deepcopy(v) for k, v in old.items() if k not in {'supersededChecks', 'history'}})
        if remaining:
            rest = deepcopy(old)
            rest['target']['stars' if target['kind'] == 'unit' else 'columns'] = [p for p, _ in remaining]
            rest['expected'] = [v for _, v in remaining]
            kept.append(rest)
    if superseded:
        records = [*check.get('supersededChecks', []), *superseded]
        check['supersededChecks'] = list({json.dumps(record, sort_keys=True): record for record in records}.values())
    checks[:] = [*kept, check]


class _BoundReview:
    """An explicit review of one transition, bound to source and prior audit.

    This is the escape hatch for stale upstream values, compound expressions,
    and documented model limits. It grants no approval to future source edits.
    Raw definition and final numeric validation still run after applying it.

    Record families, every one tied to exact values of the staged inputs:
    `mappings` / `dispositions` answer a patch-note line (a numeric mapping onto a
    curve row also passes the wrong-row guard, or carries
    `siblingRowAcknowledgement {rows, reason}`); `lookupChanges` decide a value the
    regenerated lookup changed with no note behind it (`accept-lookup`, or
    `keep-reviewed-value` when a better source disagrees with it);
    `definitionDispositions` acknowledge one text or structure change by its path
    and both values; `sourceDisagreements` explain where the published base stats
    and the CommunityDragon export still differ.
    """
    def __init__(self, candidate, previous, notes, document=None):
        import tft
        if document is None and previous.patch != candidate.patch:
            path = Path(previous.dir).parent / 'patch-reviews' / f'{candidate.patch}.json'
            if path.exists():
                document = json.loads(path.read_text())
        self.document = deepcopy(document)
        self.changes, self.notes, self.dispositions = {}, {}, {}
        self.lookup_changes, self.definitions, self.disagreements = [], {}, {}
        if document is None:
            return
        if document.get('schema') != 1 or document.get('patch') != candidate.patch or document.get('previousPatch') != previous.patch:
            _fail('review manifest names a different patch transition')
        if document.get('source', '').rstrip('/') != notes['url'].rstrip('/'):
            _fail('review manifest is not bound to the official patch source')
        expected = {'lookupHash': tft.json_hash(candidate.raw), 'binsHash': tft.json_hash(candidate.bins),
                    'patchNotesHash': tft.json_hash(notes), 'previousAuditHash': tft.json_hash(previous.audit)}
        if document.get('bindings') != expected:
            _fail('review manifest hashes do not match the staged sources and previous audit')
        identities = {'change': {tft.json_hash(c) for c in notes['changes']},
                      'note': {tft.json_hash(n) for n in notes['notes']}}
        lookups = []   # [previous, candidate] with no override at all: what upstream itself said and says

        def lookup(index):
            if not lookups:
                lookups.extend((_normalized(previous, {}), _normalized(candidate, {})))
            return lookups[index]

        seen, claimed = set(), {}
        for mapping in document.get('mappings', []):
            kind, evidence = self._evidence(mapping, identities)
            target = mapping['target']
            positions = self._coordinates(mapping, ('expected', 'observedBefore'))
            if not _same(_values(previous, target), mapping['observedBefore']):
                _fail(f'reviewed baseline no longer matches {target}')
            key = (kind, tft.json_hash(evidence), _target_key(target), tuple(positions))
            if key in seen:
                _fail(f'duplicate reviewed mapping for {target}')
            seen.add(key)
            if kind == 'change' and 'row' in target:
                self._row_choice(mapping, previous, (lookup(0), lookup(1)))
            elif 'siblingRowAcknowledgement' in mapping:
                _fail(f'siblingRowAcknowledgement only applies to a numeric note mapped to a curve row: {target}')
            claimed.setdefault(_target_key(target), set()).update(positions)
            bucket = self.changes if kind == 'change' else self.notes
            bucket.setdefault(tft.json_hash(evidence), []).append(deepcopy(mapping))
        for disposition in document.get('dispositions', []):
            kind, evidence = self._evidence(disposition, identities)
            key = kind, tft.json_hash(evidence)
            if not disposition.get('disposition') or key in self.dispositions:
                _fail('missing or duplicate review disposition')
            if kind == 'change' and key[1] in self.changes:
                _fail('review both applies and skips the same numeric change')
            self.dispositions[key] = deepcopy(disposition)
        # Upstream changes that no patch note announces (a regenerated lookup catching
        # up with the live game): each coordinate is a decision with its evidence.
        accepted = {}
        for record in document.get('lookupChanges', []):
            target = record.get('target') or {}
            positions = self._coordinates(record, ('observedBefore', 'lookup', 'expected'), inactive_column=True)
            self._explained(record, 'lookup change', ('evidence',))
            if not _same(_values(previous, target), record['observedBefore']):
                _fail(f'reviewed baseline no longer matches {target}')
            if not _same(_values(lookup(1), target), record['lookup']):
                _fail(f'the staged lookup does not hold the reviewed value at {target}')
            if _same(record['lookup'], record['observedBefore']):
                _fail(f'reviewed lookup change is not a change: {target}')
            wanted = {'accept-lookup': record['lookup'], 'keep-reviewed-value': record['observedBefore']}.get(record.get('decision'))
            if wanted is None or not _same(record['expected'], wanted):
                _fail(f"lookup change needs decision accept-lookup (expected = lookup) or "
                      f"keep-reviewed-value (expected = observedBefore): {target}")
            taken = accepted.setdefault(_target_key(target), set())
            if taken & set(positions) or claimed.get(_target_key(target), set()) & set(positions):
                _fail(f'review decides the same coordinate twice: {target}')
            taken.update(positions)
            self.lookup_changes.append(deepcopy(record))
        for record in document.get('definitionDispositions', []):
            path = record.get('path')
            self._explained(record, 'definition disposition', ('disposition',))
            if not isinstance(path, str) or not path or path in self.definitions or 'before' not in record or 'after' not in record:
                _fail('definition disposition needs a unique path and its exact before/after values')
            self.definitions[path] = deepcopy(record)
        for record in document.get('sourceDisagreements', []):
            self._explained(record, 'source disagreement', ('disposition', 'evidence'))
            key = tuple(record.get(k) for k in ('api', 'asset', 'stat'))
            if not all(isinstance(k, str) and k for k in key) or key in self.disagreements \
                    or 'effective' not in record or 'communitydragon' not in record:
                _fail('source disagreement needs a unique api/asset/stat and both observed values')
            self.disagreements[key] = deepcopy(record)

    @staticmethod
    def _coordinates(record, names, inactive_column=False):
        target = record.get('target') or {}
        positions = [1] if 'stat' in target else _positions(target)
        # A trait row's column 0 is its inactive value. No patch note addresses it,
        # but a regenerated lookup can change it together with the first breakpoint.
        lowest = 0 if inactive_column and target.get('kind') == 'trait' else 1
        if (target.get('kind') not in {'unit', 'item', 'trait'}
                or ('stat' in target) == ('row' in target)
                or not positions or len(set(positions)) != len(positions)
                or any(type(p) is not int or p < lowest or (target['kind'] == 'unit' and p > 4) for p in positions)):
            _fail(f'invalid reviewed target {target}')
        for name in names:
            values = record.get(name)
            if (not isinstance(values, list) or len(values) != len(positions)
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)):
                _fail(f'invalid reviewed {name} for {target}')
        return positions

    @staticmethod
    def _explained(record, what, fields):
        if not isinstance(record, dict) or not str(record.get('reason', '')).strip():
            _fail(f'{what} needs an explanation')
        for field in fields:
            value = record.get(field)
            if not value or (field == 'evidence' and not (isinstance(value, list) and all(str(v).strip() for v in value))):
                _fail(f'{what} needs {field}')

    @staticmethod
    def _row_choice(mapping, previous, lookups):
        target, what = mapping['target'], mapping['change'].get('what', '')
        rivals = _better_sibling_rows(mapping, previous, lookups)
        acknowledged = []
        if 'siblingRowAcknowledgement' in mapping:
            ack = mapping['siblingRowAcknowledgement']
            acknowledged = ack.get('rows') if isinstance(ack, dict) else None
            if (not isinstance(acknowledged, list) or not acknowledged or not all(isinstance(r, str) for r in acknowledged)
                    or not str(ack.get('reason', '')).strip()):
                _fail(f'siblingRowAcknowledgement needs the rows it rules out and a reason: {target}')
            if not rivals:
                _fail(f'{what}: siblingRowAcknowledgement names {acknowledged}, but no sibling row fits the note '
                      f'better than {target["row"]}; remove it')
        unexplained = sorted(set(rivals) - set(acknowledged))
        if unexplained:
            detail = '; '.join(f'{row} {rivals[row]}' for row in unexplained)
            _fail(f'{what}: {target["row"]} may be the wrong row ({detail}). Retarget the mapping, or record '
                  'siblingRowAcknowledgement {rows, reason} saying why those rows are not the one the note means')

    @staticmethod
    def _evidence(record, identities):
        import tft
        kinds = [kind for kind in ('change', 'note') if kind in record]
        if len(kinds) != 1 or not str(record.get('reason', '')).strip():
            _fail('review record needs exact evidence and an explanation')
        kind = kinds[0]
        evidence = record[kind]
        if tft.json_hash(evidence) not in identities[kind]:
            _fail('review record is absent from the exact staged patch notes')
        return kind, evidence

    def mapped(self, kind, evidence):
        import tft
        return (self.changes if kind == 'change' else self.notes).get(tft.json_hash(evidence), [])

    def disposition(self, kind, evidence):
        import tft
        return self.dispositions.get((kind, tft.json_hash(evidence)))


_XP_SECTIONS = {"xpperlevel", "experienceperlevel", "levelingcosts", "xpcosts"}
# The whole label of one row of the player-level XP table: "Level 8 to Level 9"
# (18.2's SYSTEMS / XP PER LEVEL) or "XP From Level 8-9" (its mid-patch SYSTEMS block).
_XP_LEVEL_ROW = re.compile(r"^(?:XP\s+(?:from|for|to)\s+)?Level\s+\d+\s*(?:to|-|–|—)\s*(?:Level\s+)?\d+(?:\s*:|\s*$)", re.I)


def _outside(entry, excluded_items):
    section, major = _norm(entry.get("section", "")), _norm(entry.get("major", ""))
    # Riot files the table under SYSTEMS as the article's major heading, or as a
    # section of a dated mid-patch update. Either heading has to be there AND the
    # line has to be a table row: a champion, trait or item line that mentions XP
    # or a level (Draven's bounty, Teemo's forage) still needs its own review.
    if (major == "systems" and section in _XP_SECTIONS) or section == "systems":
        text = entry.get("text", entry.get("what", ""))
        if _XP_LEVEL_ROW.match(text):
            return "XP purchase costs are outside fixed-level combat evaluation"
    if any(word in section for word in ("augment", "artifact", "radiant")):
        return "category is outside the craftable-item combat model"
    if "cosmetic" in major or section in {"arenas", "booms", "tacticians", "3rdpartyfriends"}:
        return "cosmetic or informational category"
    text = entry.get("text", entry.get("what", ""))
    normalized = _norm(text)
    if any(normalized.startswith(name) for name in excluded_items):
        return "item is excluded from both the previous and candidate build pools"
    cosmetic = re.search(r"\b(icon|icons|localization|sound|sfx|volume|graphics|loading screen|frame rate|memory leak)\b", text, re.I)
    mechanics = re.search(r"\b(damage|heal|heals|mana|attack|attacks|target|targets|stun|stuns|shield|shields|durability|cast|casts|ability)\b", text, re.I)
    if cosmetic and not mechanics:
        return "explicit presentation/performance fix with no combat change"
    return None


def _project(series, target):
    if "stat" in target:
        if target["stat"] in ("initialMana", "mana") and len(series) == 2:
            return [series[0 if target["stat"] == "initialMana" else 1]]
        if len(series) != 1:
            _fail(f"array does not identify one stat: {target}")
        return series
    if len(series) == 1:
        return series * len(_positions(target))
    positions = _positions(target)
    if any(p < 1 or p > len(series) for p in positions):
        _fail(f"array length changed ambiguously for {target}")
    return [series[p - 1] for p in positions]


def _transform(check):
    stored = check.get("numericEncoding")
    if stored:
        return stored["scale"], stored["offset"]
    old_numbers = _numbers(check.get("patchLine", {}).get("new", ""), check["what"])
    projected = _project(old_numbers, check["target"])
    choices = [(scale, offset) for scale, offset in ((1, 0), (.01, 0), (.01, 1), (-.01, 1))
               if _same([x * scale + offset for x in projected], check["expected"])]
    if len(choices) != 1:
        _fail(f"{check['what']}: percentage/storage convention is ambiguous for {check['target']}")
    return choices[0]


_STAT_LABELS = {
    "mana": ("initialMana", "mana"), "startingmana": ("initialMana",),
    "initialmana": ("initialMana",), "maxmana": ("mana",),
    "health": ("hp",), "basehealth": ("hp",), "hp": ("hp",), "basehp": ("hp",),
    "ad": ("ad",), "basead": ("ad",), "attackdamage": ("ad",), "baseattackdamage": ("ad",),
    "attackspeed": ("as",), "baseas": ("as",), "baseattackspeed": ("as",),
    "armor": ("armor",), "magicresist": ("mr",), "resists": ("armor", "mr"),
    "resistances": ("armor", "mr"),
}


_ABILITY_LABELS = {
    'damage': 'damage', 'abilitydamage': 'damage', 'spelldamage': 'damage',
    'heal': 'heal', 'healing': 'heal', 'abilityheal': 'heal', 'abilityhealing': 'heal',
    'spellheal': 'heal', 'spellhealing': 'heal',
    'shield': 'shield', 'shielding': 'shield', 'abilityshield': 'shield',
    'abilityshielding': 'shield', 'spellshield': 'shield', 'spellshielding': 'shield',
}
_NON_AMOUNT_TOKENS = {
    'auto', 'basic', 'duration', 'count', 'counts', 'num',
    'number', 'radius', 'range', 'time', 'timing', 'delay', 'tick', 'ticks',
    'rate', 'frequency', 'interval', 'cooldown', 'windup', 'speed', 'targets',
    'chance', 'reduction', 'falloff', 'amplification', 'amp',
}


def _row_semantics(row):
    spaced = re.sub(r'([a-z])([A-Z])', r'\1 \2', row)
    ordered = re.findall(r'[a-z]+', spaced.lower())
    tokens = set(ordered)
    if tokens & _NON_AMOUNT_TOKENS:
        return set()
    # "Attack" disqualifies a row only as part of the basic attack (Auto/Basic, above)
    # or of the attack-damage stat itself (BonusAttackDamage is a buff, not damage
    # dealt). ThirdAttackEmpoweredDamage and MiniDamagePerAttack are ability amounts:
    # dropping every row with "attack" in its name hid Nidalee's third javelin from
    # the candidates of "Empowered Attack Damage".
    if any(a in ('attack', 'attacks') and b == 'damage' for a, b in zip(ordered, ordered[1:])):
        return set()
    names = {'damage': {'damage', 'dmg'}, 'heal': {'heal', 'heals', 'healing', 'healed'},
             'shield': {'shield', 'shields', 'shielding'}}
    return {name for name, words in names.items() if tokens & words}


def _discover(change, previous, current):
    import tft
    label = _label(change["what"])
    units = [u for u in previous.units.values() if label.startswith(_norm(u["name"]))]
    if len(units) != 1:
        for kind, entities in (("trait", previous.traits), ("item", previous.items)):
            found = [entity for entity in entities.values() if label.startswith(_norm(entity["name"]))]
            if found:
                _fail(f"{change['what']}: {kind} change needs an explicit reviewed field mapping")
        _fail(f"{change['what']}: no unique existing champion or reviewed target mapping")
    unit = units[0]
    suffix = label[len(_norm(unit["name"])):]
    old, new = _numbers(change["old"], change["what"]), _numbers(change["new"], change["what"])
    if len(old) != len(new):
        _fail(f"{change['what']}: old/new array lengths differ")
    stats = _STAT_LABELS.get(suffix)
    if stats:
        if suffix == "mana" and len(new) == 1:
            stats = ("mana",)
        if "%" in str(change["new"]):
            _fail(f"{change['what']}: a percentage base-stat change needs an explicit mapping")
        checks = []
        for form in [None, *unit.get("forms", {})]:
            data = unit if form is None else unit["forms"][form]
            if not data.get("stats"):
                continue
            for stat in stats:
                target = {"kind": "unit", "api": unit["api"], "stat": stat}
                if form:
                    target["form"] = form
                expected = _project(old, target)
                have = _values(previous, target)
                if not (_same(have, expected) or _same(have, _project(new, target))):
                    _fail(f"{change['what']}: {form or 'base'} {stat} does not match the documented old/current value")
                checks.append({"what": change["what"], "target": target, "expected": have,
                               "numericEncoding": {"scale": 1, "offset": 0}})
        return checks
    if re.search(r"timing|windup|casttime|channeltime|missilespeed|tickrate|duration", suffix):
        _fail(f"{change['what']}: unverified ability timing change")
    semantic = _ABILITY_LABELS.get(suffix)
    if semantic is None:
        _fail(f"{change['what']}: qualified or unknown ability label requires an explicit audit mapping")
    requested = {semantic}
    matches = []
    for form in [None, *unit.get("forms", {})]:
        data = unit if form is None else unit["forms"][form]
        for row, curve in data.get("curve", {}).items():
            if _row_semantics(row) != requested:
                continue
            unit_type = _number_unit(change['old']) or _number_unit(change['new'])
            if unit_type in ('ap', 'ad'):
                scalings = {term.get('scaling') for calc in data.get('calcs', {}).values()
                            for term in calc.get('terms', []) if term.get('row') == row}
                required = {'AbilityPower'} if unit_type == 'ap' else {'AttackDamage', 'BasicAttackDamage'}
                if not scalings & required:
                    continue
            for scale, offset in ((1, 0), (.01, 0), (.01, 1), (-.01, 1)):
                if "%" not in str(change["old"]) and (scale, offset) != (1, 0):
                    continue
                expected = [x * scale + offset for x in old]
                have = [tft.curve_at(curve, p) for p in range(1, len(old) + 1)]
                if not _same(have, expected):
                    continue
                positions = list(range(1, len(old) + 1))
                if len(old) == 1:
                    positions = [p for p in range(1, 5) if _equal(tft.curve_at(curve, p), expected[0])]
                target = {"kind": "unit", "api": unit["api"], "row": row, "stars": positions}
                if form:
                    target["form"] = form
                matches.append({"what": change["what"], "target": target,
                                "expected": _values(previous, target), "numericEncoding": {"scale": scale, "offset": offset}})
    if not matches or len({(m['target']['row'], tuple(m['numericEncoding'].values())) for m in matches}) != 1:
        _fail(f"{change['what']}: ability numbers match zero or multiple curve rows")
    return matches


def _write_override(overrides, snap, target, values):
    entity = overrides.setdefault(target["kind"] + "s", {}).setdefault(target["api"], {})
    if target.get("form"):
        entity = entity.setdefault("forms", {}).setdefault(target["form"], {})
    if "stat" in target:
        entity.setdefault("stats", {})[target["stat"]] = values[0]
    elif target["kind"] == "unit":
        full = dict(target, stars=[1, 2, 3, 4])
        updated = _values(snap, full)
        for position, value in zip(_positions(target), values):
            if position not in (1, 2, 3, 4):
                _fail(f"unsupported unit star position {position}")
            updated[position - 1] = value
        entity.setdefault("curve", {})[target["row"]] = updated
    elif target["kind"] == "trait":
        row = entity.setdefault("curve", {}).setdefault(target["row"], {})
        row.update({str(p): v for p, v in zip(_positions(target), values)})
    else:
        if not all(_equal(v, values[0]) for v in values) or _positions(target) != [1]:
            _fail(f"{target['api']} {target['row']}: staged item arrays need a reviewed override representation")
        entity.setdefault("curve", {})[target["row"]] = values[0]


def _check_dependencies(overrides, target, matching, before, expected):
    if _same(before, expected):
        return
    if target['kind'] == 'trait':
        existing = overrides.get('traits', {}).get(target['api'], {}).get('curve', {}).get(target['row'], {})
        covered = {p for c in matching if _target_key(c['target']) == _target_key(target)
                   for p in _positions(c['target'])}
        dependent = set(map(int, existing)) - covered
        if dependent:
            _fail(f"{target['api']} {target['row']}: corrected dependent columns {sorted(dependent)} "
                  "are outside the verified patch targets; document the breakpoint dependency first")


def _raw_context(snap, context):
    _, api, form = context
    unit = next(u for u in snap.raw['units'] if u['apiName'] == api)
    extra = next((f for f in unit.get('extraAbilities', {}).values() if f.get('variant') == form), {}) if form else {}
    stats = {**unit.get('stats', {}), **(extra.get('stats') or {})}
    calcs = {**unit.get('attributeCalcs', {}), **unit.get('ability', {}).get('attributeCalcs', {}),
             **extra.get('attributeCalcs', {}), **extra.get('ability', {}).get('attributeCalcs', {})}
    return stats, calcs


def _retire_caught_up_stats(overrides, previous, candidate):
    """Do not carry an unnecessary old stat pin onto a changed source.

    Unit curve overrides also repair coefficient arrays, so those remain
    until their explicit audit mapping is revalidated.
    """
    old_raw = _normalized(previous, {})
    new_raw = _normalized(candidate, {})
    retired = []
    for api, correction in overrides.get('units', {}).items():
        for form, values in [(None, correction), *list(correction.get('forms', {}).items())]:
            for stat, expected in list(values.get('stats', {}).items()):
                target = {'kind': 'unit', 'api': api, 'stat': stat}
                if form:
                    target['form'] = form
                try:
                    a = (_entity(old_raw, target).get('stats') or {}).get(stat)
                    b = (_entity(new_raw, target).get('stats') or {}).get(stat)
                except ReviewRequired:
                    continue
                if a != b and _equal(b, expected):
                    del values['stats'][stat]
                    retired.append({'target': target, 'value': expected,
                                    'reason': 'changed raw source now supplies the approved value directly'})
    return retired


def _cached_calc(snap, context, name, star, visiting=()):
    """Validate changed cached outputs only where the old cache confirms
    these lookup conventions. Unsupported runtime/formulas require review.
    This does not run the combat engine or reinterpret calculation terms.
    """
    stats, calcs = _raw_context(snap, context)
    names = [key for key in calcs if key.split('.')[-1] == name.split('.')[-1]]
    if len(names) != 1 or names[0] in visiting:
        raise ValueError('missing, duplicate, or recursive calculation')
    name = names[0]
    terms = calcs[name].get('terms')
    if not terms:
        raise ValueError('calculation has no terms')
    def at(values):
        if not isinstance(values, list) or not values:
            raise ValueError('missing coefficient array')
        value = values[min(star, len(values)) - 1]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError('non-numeric calculation term')
        return value
    acc = 0.0
    for term in terms:
        if term.get('type') == 'flat':
            value = at(term.get('values', term.get('coefficient')))
        elif term.get('type') == 'scaled':
            scaling = term.get('scaling')
            if scaling in (None, 'AbilityPower', 'AttackDamage'):
                factor = 1.0
            elif scaling in ('Health', 'HealthMax', 'Armor', 'MagicResist', 'BasicAttackDamage'):
                key = {'Health': 'hp', 'HealthMax': 'hp', 'Armor': 'armor', 'MagicResist': 'magicResist', 'BasicAttackDamage': 'damage'}[scaling]
                factor = stats[key]
            elif isinstance(scaling, str) and re.search(r'Calc\d+$', scaling):
                factor = _cached_calc(snap, context, scaling, star, (*visiting, name))
            else:
                raise ValueError(f'unsupported cached scaling {scaling!r}')
            value = at(term.get('coefficient')) * (factor + (at(term['preAdd']) if term.get('preAdd') is not None else 0))
        else:
            raise ValueError('runtime or unknown calculation term')
        op = term.get('op')
        if op == 'add': acc += value
        elif op == 'override': acc = value
        elif op == 'multiply': acc *= value
        elif op == 'divide' and value: acc /= value
        else: raise ValueError(f'unsupported cached operation {op!r}')
    return acc


_COSMETIC_KEYS = {"icon", "squareIcon", "tileIcon", "skin", "image", "iconUrl", "texture", "overlays"}
_NONMODELED_ROOTS = {"_metadata", "augments", "augmentTiers", "charms", "encounters", "armory_items", "extras"}
_RAW_STATS = {"damage": "ad", "attackSpeed": "as", "magicResist": "mr", "critMultiplier": "critMult",
              "hp": "hp", "armor": "armor", "mana": "mana", "initialMana": "initialMana", "range": "range", "critChance": "critChance"}


def _raw_entities(raw, kind):
    records = raw.get(kind + "s")
    if not isinstance(records, list):
        _fail(f"lookup has no valid {kind} definitions")
    result = {r.get("apiName"): r for r in records}
    if None in result or len(result) != len(records):
        _fail(f"lookup has missing or duplicate {kind} IDs")
    return result


def _without_icons(value):
    if isinstance(value, dict):
        return {key: _without_icons(data) for key, data in value.items() if key not in _COSMETIC_KEYS}
    if isinstance(value, list):
        return [_without_icons(data) for data in value]
    return value


def _raw_block(snap, context):
    """The raw unit, or the raw alternate-form block, a unit context names."""
    _, api, form = context
    unit = next(u for u in snap.raw['units'] if u['apiName'] == api)
    if not form:
        return unit
    return next((f for f in unit.get('extraAbilities', {}).values() if f.get('variant') == form), {})


def _form_stats(snap, api, form):
    """What a form fights with: tft.kit_spec overlays its own stats on the unit's."""
    unit = snap.units.get(api) or snap.extras.get(api)
    own = (unit['forms'][form].get('stats') or {}) if unit and form in unit.get('forms', {}) else None
    if own is None:
        return None
    return {**unit['stats'], **{key: value for key, value in own.items() if value is not None}}


class _DefinitionReview:
    """Validate raw changes without letting old overrides conceal them."""
    def __init__(self, previous, candidate, final, allowed, excluded, dispositions=None):
        self.previous, self.candidate, self.final = previous, candidate, final
        self.allowed, self.excluded = allowed, excluded
        self.dispositions = dispositions or {}
        self.used = set()
        self.changes = []
        self.excluded_changed = set()

    def reviewed(self, path, before, after):
        """A text or structure change is accepted only through a review record
        naming this exact path and both exact values (None = absent)."""
        record = self.dispositions.get(path)
        if record is None or record['before'] != before or record['after'] != after:
            return False
        self.used.add(path)
        return True

    def keys(self, before, after, context, path):
        """Common keys of two definition dicts; each added or removed key needs its
        own review record. Icons are never compared."""
        old_keys, new_keys = set(before) - _COSMETIC_KEYS, set(after) - _COSMETIC_KEYS
        for key in sorted(old_keys ^ new_keys):
            if not self.reviewed(f"{path}.{key}", before.get(key), after.get(key)):
                _fail(f"definition structure changed at {path}: {sorted(old_keys ^ new_keys)}")
            if key == 'stats' and context[0] == 'unit' and context[2]:
                # kit_spec falls back to the unit's stats when a form has no block of its
                # own, so dropping (or adding) one is harmless only if nothing it fights
                # with moves. This is checked, not taken from the review's word.
                was, now = _form_stats(self.previous, context[1], context[2]), _form_stats(self.final, context[1], context[2])
                if was is None or now is None or set(was) != set(now) or any(not _equal(was[k], now[k]) for k in was):
                    _fail(f"{path}.stats: the {context[2]} form's effective stats change with its stats block; "
                          "each stat needs an explicit mapping")
        return sorted(old_keys & new_keys)

    def permit(self, context, field, position, new):
        kind, api, form = context
        values = self.allowed.get((kind, api, form, field, position), [])
        if any(_equal(new, v) for v in values):
            return True
        # An unchanged reviewed correction can be retired when upstream
        # supplies its exact effective value. It cannot hide a new value.
        target = {"kind": kind, "api": api}
        if form:
            target["form"] = form
        target["stat" if position is None else "row"] = field
        if position is not None:
            target["stars" if kind == "unit" else "columns"] = [position]
        try:
            return _same([new], _values(self.previous, target))
        except ReviewRequired:
            return False

    def curve(self, before, after, context, row, path):
        import tft
        if not before or not after:
            _fail(f"curve structure changed at {path}")
        try:
            upper = max([int(p) for p, _ in before + after] + ([4] if context[0] == "unit" else []))
            lower = min(int(p) for p, _ in before + after)
            if upper > 100 or lower < 0:
                _fail(f"unsupported curve coordinates at {path}")
            for p in range(lower, upper + 1):
                old, new = tft.curve_at(before, p), tft.curve_at(after, p)
                inherited_ad = False
                if context[0] == 'unit' and row in ('AutoAttackDamage', 'BasicAttackDamage'):
                    old_stats, _ = _raw_context(self.previous, context)
                    new_stats, _ = _raw_context(self.candidate, context)
                    old_ad, new_ad = old_stats.get('damage'), new_stats.get('damage')
                    inherited_ad = (isinstance(old_ad, (int, float)) and old_ad != 0
                                    and isinstance(new_ad, (int, float)) and self.permit(context, 'ad', None, new_ad)
                                    and _equal(new, old * new_ad / old_ad))
                if not _equal(old, new) and not self.permit(context, row, p, new) and not inherited_ad:
                    _fail(f"unexplained lookup change at {path}, column {p}: {old!r} to {new!r}")
        except (TypeError, ValueError, IndexError) as exc:
            if isinstance(exc, ReviewRequired):
                raise
            _fail(f"malformed curve at {path}")

    def cached(self, before, after, context, name, path):
        if before == after:
            return
        if context[0] != 'unit' or not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after):
            _fail(f'unverified calculation cache change at {path}')
        try:
            for star, (old, new) in enumerate(zip(before, after), 1):
                if not (_equal(old, _cached_calc(self.previous, context, name, star))
                        and _equal(new, _cached_calc(self.candidate, context, name, star))):
                    raise ValueError('cached value does not follow unchanged calculation terms')
        except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
            _fail(f'unverified calculation cache at {path}: {exc}')

    def compare(self, before, after, context, path, row=None):
        if before == after:
            return
        if isinstance(before, dict) and isinstance(after, dict):
            for key in self.keys(before, after, context, path):
                a, b = before[key], after[key]
                if a == b:
                    continue
                location = f"{path}.{key}"
                if key == "stats":
                    if set(a or {}) != set(b or {}):
                        _fail(f"base-stat schema changed at {location}")
                    for stat in a or {}:
                        if a[stat] == b[stat]:
                            continue
                        normalized = _RAW_STATS.get(stat)
                        if normalized and self.permit(context, normalized, None, b[stat]):
                            continue
                        if stat == "damageByStar" and (self._ad_series(a, b, context) or self._ad_rows(a, b, context)):
                            continue
                        _fail(f"unexplained base-stat change at {location}.{stat}: {a[stat]!r} to {b[stat]!r}")
                elif key in ("curveTable", "curveValues"):
                    a, b = a or {}, b or {}
                    for name in sorted(set(a) ^ set(b)):
                        if not self.reviewed(f"{location}.{name}", a.get(name), b.get(name)):
                            _fail(f"new or missing curve rows at {location}: {sorted(set(a) ^ set(b))}")
                    for name in sorted(set(a) & set(b)):
                        if a[name] != b[name]:
                            row_name = self._table_row(before, after, name) if key == "curveValues" else name
                            self.curve(a[name], b[name], context, row_name, f"{location}.{name}")
                elif key == "extraAbilities":
                    if set(a or {}) != set(b or {}):
                        _fail(f"new or missing alternate forms at {location}")
                    for name in a or {}:
                        form = a[name].get("variant")
                        next_context = (context[0], context[1], form if form in ("AD", "AP") else context[2])
                        self.compare(a[name], b[name], next_context, f"{location}.{name}")
                elif key in ('attributeCalcs', 'attributeValues'):
                    if set(a or {}) != set(b or {}):
                        _fail(f'new or missing calculations at {location}')
                    for name in a or {}:
                        if key == 'attributeValues':
                            self.cached(a[name], b[name], context, name, f'{location}.{name}')
                        else:
                            self.compare({k: v for k, v in a[name].items() if k != 'values'},
                                         {k: v for k, v in b[name].items() if k != 'values'}, context, f'{location}.{name}')
                            if ('values' in a[name]) != ('values' in b[name]):
                                _fail(f'calculation cache structure changed at {location}.{name}')
                            self.cached(a[name].get('values'), b[name].get('values'), context, name, f'{location}.{name}.values')
                elif key in ("coefficient", "values") and before.get("row") and isinstance(a, list) and isinstance(b, list):
                    name = before["row"]
                    if a and isinstance(a[0], list):
                        self.curve(a, b, context, name, location)
                    else:
                        if len(a) != len(b):
                            _fail(f"coefficient-array shape changed at {location}")
                        for star, (old, new) in enumerate(zip(a, b), 1):
                            if not _equal(old, new) and not self.permit(context, name, star, new):
                                _fail(f"unverified coefficient change at {location}, star {star}")
                else:
                    self.compare(a, b, context, location, before.get("row", row))
        elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
            for i, (a, b) in enumerate(zip(before, after)):
                self.compare(a, b, context, f"{path}[{i}]", row)
        elif not self.reviewed(path, before, after):
            _fail(f"unverified formula, mechanic, or value at {path}: {str(before)[:80]!r} to {str(after)[:80]!r}")

    def _ad_series(self, before, after, context):
        old, new = before.get("damage"), after.get("damage")
        a, b = before.get("damageByStar"), after.get("damageByStar")
        return (isinstance(old, (int, float)) and old != 0 and isinstance(new, (int, float))
                and self.permit(context, "ad", None, new) and isinstance(a, list) and isinstance(b, list)
                and len(a) == len(b) and _same([v * new / old for v in a], b))

    def _ad_rows(self, before, after, context):
        """`damageByStar` that stopped being damage x 1.5^n (the 2026-09-11 lookup lists
        4-star attack damage at 3x): accepted where it is the staged AutoAttackDamage
        row verbatim, so each star is decided under that row's coordinate."""
        import tft
        a, b = before.get("damageByStar"), after.get("damageByStar")
        curve = (_raw_block(self.candidate, context).get("curveTable") or {}).get("AutoAttackDamage")
        if context[0] != "unit" or not curve or not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
            return False
        return all(_equal(new, tft.curve_at(curve, star))
                   and (_equal(old, new) or self.permit(context, "AutoAttackDamage", star, new))
                   for star, (old, new) in enumerate(zip(a, b), 1))

    @staticmethod
    def _table_row(before, after, name):
        """A `curveValues` row that is a differently cased copy of one `curveTable` row
        (Riftbeast's CapstoneAspd / CapstoneASPD) is decided under the table row's name:
        Snapshot reads a trait's curveTable only, so the copy has no address of its own."""
        old_table, new_table = before.get("curveTable") or {}, after.get("curveTable") or {}
        if name in old_table:
            return name
        twins = [row for row in old_table if row.lower() == name.lower()]
        if (len(twins) == 1 and old_table[twins[0]] == before["curveValues"][name]
                and new_table.get(twins[0]) == after["curveValues"][name]):
            return twins[0]
        return name

    def run(self):
        a, b = self.previous.raw, self.candidate.raw
        ignored = _NONMODELED_ROOTS | {"units", "items", "traits"}
        if {k: v for k, v in a.items() if k not in ignored} != {k: v for k, v in b.items() if k not in ignored}:
            _fail("unverified role definitions or lookup structure changed")
        for key in sorted(_NONMODELED_ROOTS - {'_metadata'}):
            if a.get(key) != b.get(key):
                self.changes.append({'kind': 'outside-model', 'key': key,
                                     'reason': 'this lookup category is not consumed by Snapshot combat definitions'})
        for kind in ("unit", "item", "trait"):
            old, new = _raw_entities(a, kind), _raw_entities(b, kind)
            added_removed = set(old) ^ set(new)
            if added_removed and (kind != 'item' or not added_removed <= self.excluded):
                _fail(f"new or removed {kind} definitions: {sorted(added_removed)[:8]}")
            for api in sorted(added_removed):
                self.excluded_changed.add(api)
                self.changes.append({'kind': kind, 'api': api, 'reason': 'added/removed item is outside both build pools'})
            for api in sorted(set(old) & set(new)):
                if old[api] == new[api]:
                    continue
                if _without_icons(old[api]) == _without_icons(new[api]):
                    self.changes.append({'kind': kind, 'api': api, 'reason': 'icon/appearance metadata only'})
                    continue
                if kind == "item" and api in self.excluded:
                    self.excluded_changed.add(api)
                    self.changes.append({"kind": kind, "api": api, "reason": "changed item is outside both build pools"})
                    continue
                self.compare(old[api], new[api], (kind, api, None), api)
                self.changes.append({"kind": kind, "api": api, "reason": "only mapped numeric values, approved correction catch-up, or icons changed"})
        unused = sorted(set(self.dispositions) - self.used)
        if unused:
            _fail(f"review describes definition changes the staged lookup does not contain: {unused[:5]}")
        for path in sorted(self.used):
            record = self.dispositions[path]
            self.changes.append({'kind': 'reviewed-definition', 'path': path,
                                 'disposition': record['disposition'], 'reason': record['reason']})
        return self.changes


def _cross_check(final, previous, bound_review):
    """The audit's record of tft.source_disagreements for the snapshot about to be
    published, or ReviewRequired. None when there is no CommunityDragon record to compare.

    A bound review has a person in the loop, so it must explain every disagreement
    that a previous audit has not. An unattended run may not introduce one: what the
    previous snapshot already held, value for value, is carried forward as
    `inherited` (listed by `tft check`, reviewed by nobody) instead of blocking a
    hotfix that has nothing to do with it.
    """
    import tft
    found = tft.source_disagreements(final)
    reviewed = bound_review.disagreements
    if not found["compared"]:
        if reviewed:
            _fail("review explains source disagreements, but the candidate has no CommunityDragon record to compare")
        return None

    def same(a, b):
        return all((a[k] is None and b[k] is None) or (a[k] is not None and b[k] is not None and _equal(a[k], b[k]))
                   for k in ("effective", "communitydragon"))

    identity = lambda item: (item["api"], item["asset"], item["stat"])
    earlier = (previous.audit or {}).get("sourceCrossCheck") or {}
    carried = {identity(item): (group, item) for group in ("explained", "inherited") for item in earlier.get(group, [])}
    held = {identity(item): item for item in tft.source_disagreements(previous)["disagreements"]}
    explained, inherited, used = [], [], set()
    for item in found["disagreements"]:
        key = identity(item)
        label = f"{item['name']} {item['stat']} ({item['asset']}): snapshot {item['effective']}, CommunityDragon {item['communitydragon']}"
        group, old = carried.get(key, (None, None))
        if key in reviewed:
            if not same(reviewed[key], item):
                _fail(f"reviewed source disagreement no longer matches the staged values: {label}")
            used.add(key)
            explained.append({**item, **{k: deepcopy(reviewed[key][k]) for k in ("disposition", "reason", "evidence")}})
        elif group == "explained" and same(old, item):
            explained.append(deepcopy(old))
        elif bound_review.document is not None:
            _fail(f"unexplained lookup/CommunityDragon disagreement: {label}; the review needs a sourceDisagreements record")
        elif group == "inherited" and same(old, item):
            inherited.append(deepcopy(old))
        elif key in held and same(held[key], item):
            inherited.append({**item, "since": previous.patch})
        else:
            _fail(f"unexplained lookup/CommunityDragon disagreement: {label}")
    stale = sorted(set(reviewed) - used)
    if stale:
        _fail(f"review explains source disagreements the staged data does not have: {stale[:5]}")
    return {"source": "effective base stats against the archived CommunityDragon export (tft.source_disagreements)",
            "comparedRecords": found["compared"], "unmodeledRecords": found["unmodeled"],
            "explained": explained, "inherited": inherited}


def _reconcile(candidate, previous, notes, review_document=None):
    """Return publishable overrides/audit, or raise :class:`ReviewRequired`.

    The candidate is staged. Its copied audit/overrides are never treated as
    approval for new content. Hashes are rebound only after both note and raw
    definition comparisons succeed. Input objects and files remain unchanged.
    """
    import tft
    prior_notes = _read_notes(previous)
    _validate_sources(candidate, previous, notes, prior_notes)
    bound_review = _BoundReview(candidate, previous, notes, review_document)
    overrides = deepcopy(previous.overrides)
    audit = deepcopy(previous.audit)
    checks = audit["checks"]
    working = _normalized(candidate, overrides)
    item_fx = tft.load_item_effects(previous.set_no)
    previous_pool, candidate_pool = set(tft.pool_items(previous, item_fx)), set(tft.pool_items(working, item_fx))
    if previous_pool != candidate_pool:
        _fail("the craftable build-item pool changed")
    excluded = (set(previous.items) | set(working.items)) - previous_pool
    excluded_names = {_norm(item['name']) for api, item in {**previous.items, **working.items}.items() if api in excluded}
    old_changes = {_change_key(c) for c in prior_notes["changes"]}
    changes = [c for c in notes["changes"] if _change_key(c) not in old_changes]
    updates = list(reversed(notes.get("updates", [])))
    order = {label: i + 1 for i, label in enumerate(updates)}
    if any(c.get('update') and c['update'] not in order for c in changes):
        _fail('patch notes contain an update absent from their chronological heading list')
    changes.sort(key=lambda c: order.get(c.get("update", ""), 0))
    allowed, applied, ignored = {}, [], []

    def allow(target, values):
        prefix = _target_key(target)
        field = target.get("stat", target.get("row"))
        for pos, value in zip([None] if "stat" in target else _positions(target), values):
            allowed.setdefault((prefix[0], prefix[1], prefix[2], field, pos), []).append(value)

    # Existing verified targets allow the feed to catch up with an audited
    # value, not to introduce any other value behind an old override.
    for check in checks:
        allow(check["target"], check["expected"])

    def apply_review(mapping):
        nonlocal working
        target, expected = mapping['target'], mapping['expected']
        evidence = mapping.get('change', mapping.get('note'))
        check = {'what': evidence.get('what', evidence.get('text')), 'target': deepcopy(target),
                 'expected': deepcopy(expected), 'source': {'url': notes['url']},
                 'reviewRationale': mapping['reason'], 'observedBefore': mapping['observedBefore'],
                 'fixedScope': True, 'manualOnly': True}
        if 'change' in mapping:
            check['patchLine'] = deepcopy(evidence)
            # Only simple, verified encodings become reusable automatic
            # mappings. A decomposed formula must be reviewed again if changed.
            encoding = mapping.get('numericEncoding')
            if encoding:
                try:
                    projected = _project(_numbers(evidence['new'], evidence['what']), target)
                    if _same(_encode(projected, encoding['scale'], encoding['offset']), expected):
                        check['numericEncoding'] = deepcopy(encoding)
                        check['manualOnly'] = False
                except ReviewRequired:
                    pass
        else:
            check['reviewedNote'] = deepcopy(evidence)
        allow(target, mapping['observedBefore'])
        allow(target, expected)
        _write_override(overrides, working, target, expected)
        _replace_check(checks, check)
        working = _normalized(candidate, overrides)

    handled = set()

    def record_disposition(disposition):
        nonlocal working
        for target in disposition.get('retireTargets', []):
            # Excluded stage arrays may have no verified current encoding.
            # Keep their previous checks as history, not claims of currency.
            if target.get('kind') != 'item' or target.get('api') not in excluded or not target.get('row'):
                _fail('only an excluded item field can retire checks without a replacement')
            _values(previous, target)
            retired = [c for c in checks if _target_key(c['target']) == _target_key(target)]
            if not retired:
                _fail(f'review retirement has no previous checked field: {target}')
            checks[:] = [c for c in checks if c not in retired]
            audit.setdefault('outOfScopeChecks', []).extend(
                {**deepcopy(c), 'retiredAtPatch': candidate.patch, 'retirementReason': disposition['reason']}
                for c in retired)
            overrides.get('items', {}).get(target['api'], {}).get('curve', {}).pop(target['row'], None)
            working = _normalized(candidate, overrides)
        ignored.append(disposition)

    def pinned(target):
        """Does an override of ours decide this field, whatever the lookup says?"""
        entity = overrides.get(target['kind'] + 's', {}).get(target['api'], {})
        if target.get('form'):
            entity = entity.get('forms', {}).get(target['form'], {})
        return target.get('stat', target.get('row')) in entity.get('stats' if 'stat' in target else 'curve', {})

    accepted_lookup = []
    # The lookup's own state comes first; dated notes are then applied on top of it.
    for record in bound_review.lookup_changes:
        target, expected = record['target'], record['expected']
        positions = set([1] if 'stat' in target else _positions(target))
        covered = [c for c in checks if _target_key(c['target']) == _target_key(target)
                   and positions & set([1] if 'stat' in c['target'] else _positions(c['target']))]
        allow(target, record['lookup'])
        allow(target, expected)
        keep = record['decision'] == 'keep-reviewed-value'
        if keep or covered or pinned(target):
            # An override or a check already speaks for this coordinate: it has to say
            # the decided value. Elsewhere the lookup simply supplies it, unpinned.
            _write_override(overrides, working, target, expected)
        if keep or covered:
            _replace_check(checks, {
                'what': f"{target['api']} {target.get('form') or 'base'} {target.get('stat', target.get('row'))} "
                        f"({'kept against' if keep else 'accepted from'} the lookup generated "
                        f"{candidate.raw.get('_metadata', {}).get('generated')})",
                'target': deepcopy(target), 'expected': deepcopy(expected), 'source': {'evidence': deepcopy(record['evidence'])},
                'reviewRationale': record['reason'], 'observedBefore': deepcopy(record['observedBefore']),
                'lookupValue': deepcopy(record['lookup']), 'fixedScope': True, 'manualOnly': True})
        working = _normalized(candidate, overrides)
        if not _same(_values(working, target), expected):
            _fail(f"lookup change did not take effect at {target}: have {_values(working, target)}, decided {expected}")
        accepted_lookup.append({'target': deepcopy(target), 'decision': record['decision'],
                                'observedBefore': record['observedBefore'], 'lookup': record['lookup']})

    for change in changes:
        mappings = bound_review.mapped('change', change)
        if mappings:
            for mapping in mappings:
                apply_review(mapping)
            applied.append(deepcopy(change))
            handled.add(_change_key(change))
            continue
        disposition = bound_review.disposition('change', change)
        if disposition:
            record_disposition(disposition)
            handled.add(_change_key(change))
            continue
        category = _change_category(change)
        matching = [c for c in checks if (category is None or category == c['target']['kind'])
                    and _label(change["what"]) in {_label(c["what"]), _label(c.get("patchLine", {}).get("what", c["what"]))}]
        reason = _outside(change, excluded_names)
        if not matching and reason:
            ignored.append({"change": deepcopy(change), "reason": reason})
            handled.add(_change_key(change))
            continue
        if not matching:
            if category == 'outside':
                _fail(f"{change['what']}: this category needs a scoped review disposition")
            matching = _discover(change, previous, working)
            if category and any(c['target']['kind'] != category for c in matching):
                _fail(f"{change['what']}: note category does not match the inferred target")
        if any(c.get('manualOnly') for c in matching):
            _fail(f"{change['what']}: previously decomposed expression needs a new explicit review")
        old_numbers = _numbers(change["old"], change["what"])
        new_numbers = _numbers(change["new"], change["what"])
        if len(old_numbers) != len(new_numbers):
            _fail(f"{change['what']}: old/new array lengths differ")
        _check_numeric_units(change, matching)
        if len(new_numbers) > 1:
            # A source array claims every listed coordinate. Fixed-scope
            # reviews must not mark newly listed stars as handled while
            # projecting only the previously reviewed subset.
            for template in matching:
                target = template['target']
                if not template.get('fixedScope') or 'row' not in target:
                    continue
                covered = {p for c in matching if _target_key(c['target']) == _target_key(target)
                           for p in _positions(c['target'])}
                if not set(range(1, len(new_numbers) + 1)) <= covered:
                    _fail(f"{change['what']}: array adds coordinates outside the reviewed scope")
        item_arrays = {c['target']['api'] for c in matching if c['target']['kind'] == 'item' and len(new_numbers) > 1}
        if item_arrays:
            if not item_arrays <= excluded:
                _fail(f"{change['what']}: item arrays need a reviewed override representation")
            retired = [c for c in checks if c['target']['kind'] == 'item' and c['target']['api'] in item_arrays]
            audit.setdefault('outOfScopeChecks', []).extend(retired)
            checks[:] = [c for c in checks if c not in retired]
            for api in item_arrays:
                overrides.get('items', {}).pop(api, None)
            ignored.append({'change': deepcopy(change), 'reason': 'excluded item stage arrays are retained as historical checks only'})
            working = _normalized(candidate, overrides)
            handled.add(_change_key(change))
            continue
        for template in list(matching):
            check = deepcopy(template)
            target = check["target"]
            scale, offset = _transform(check)
            # A previously mapped unit row can extend to explicitly listed
            # stars. A singleton note retains the reviewed star scope.
            if target["kind"] == "unit" and "row" in target and len(new_numbers) > 1 and not check.get('fixedScope'):
                if len(new_numbers) > 4:
                    _fail(f"{change['what']}: unsupported star array")
                target["stars"] = list(range(1, len(new_numbers) + 1))
            before = _encode(_project(old_numbers, target), scale, offset)
            expected = _encode(_project(new_numbers, target), scale, offset)
            have = _values(working, target)
            # Normalized candidate values can already contain upstream's
            # update, or retain the previous verified correction.
            if not (_same(have, before) or _same(have, expected)):
                _fail(f"{change['what']}: continuity gap; field has {have}, notes start at {before}")
            _check_dependencies(overrides, target, matching, before, expected)
            if target.get('stat') == 'ad' and working.units[target['api']].get('forms'):
                _fail(f"{change['what']}: Adaptor attack-damage curve dependencies need explicit mappings")
            if target["kind"] == "item" and (_positions(target) != [1] or len(set(expected)) > 1):
                if target["api"] in excluded:
                    ignored.append({"change": deepcopy(change), "api": target["api"], "reason": "excluded staged item curve needs no simulation override"})
                    checks[:] = [c for c in checks if c['target']['api'] != target['api']]
                    audit.setdefault("outOfScopeChecks", []).append(deepcopy(template))
                    overrides.get("items", {}).pop(target["api"], None)
                    continue
                _fail(f"{change['what']}: staged item curve is unsupported")
            allow(target, before)
            allow(target, expected)
            _write_override(overrides, working, target, expected)
            source = {"url": notes["url"], "update": change.get("update", ""),
                      "section": change.get("section", ""), "major": change.get("major", "")}
            history = {k: deepcopy(template[k]) for k in ("expected", "source", "patchLine") if k in template}
            if history:
                check.setdefault("history", []).append(history)
            check.update({"what": change["what"], "expected": expected, "patchLine": deepcopy(change),
                          "source": source, "numericEncoding": {"scale": scale, "offset": offset},
                          "automaticRationale": "existing reviewed mapping or unique old-value match; numeric continuity verified"})
            check.setdefault("id", "auto:" + ":".join(str(x) for x in _target_key(target)))
            _replace_check(checks, check)
            working = _normalized(candidate, overrides)
        applied.append(deepcopy(change))
        handled.add(_change_key(change))

    old_entries = {_entry_key(entry) for entry in prior_notes.get("notes", [])}
    for entry in notes["notes"]:
        if _entry_key(entry) in old_entries:
            continue
        mappings = bound_review.mapped('note', entry)
        for mapping in mappings:
            apply_review(mapping)
        disposition = bound_review.disposition('note', entry)
        if not tft.patch_entry_changes(entry) and (mappings or disposition):
            if disposition:
                record_disposition(disposition)
            continue
        reason = _outside(entry, excluded_names)
        if reason:
            ignored.append({"note": deepcopy(entry), "reason": reason})
            continue
        text = entry.get("text", "")
        if "⇒" in text:
            # The parser must have retained the bullet's numeric statements.
            extracted = {_change_key(c) for c in tft.patch_entry_changes(entry)}
            relevant = [c for c in notes['changes'] if _change_key(c) in extracted]
            if relevant and all(_change_key(c) in handled or _change_key(c) in old_changes for c in relevant):
                continue
        _fail(f"new unreviewed mechanics bullet [{entry.get('section', '')}]: {text[:180]}")

    review = _DefinitionReview(previous, candidate, working, allowed, excluded, bound_review.definitions)
    definition_changes = review.run()
    # Changed excluded definitions cannot keep a claim of numeric currency.
    if review.excluded_changed:
        retired = [c for c in checks if c['target']['kind'] == 'item' and c['target']['api'] in review.excluded_changed]
        audit.setdefault("outOfScopeChecks", []).extend(retired)
        checks[:] = [c for c in checks if c not in retired]
        for api in review.excluded_changed:
            overrides.get("items", {}).pop(api, None)
        working = _normalized(candidate, overrides)
    for check in checks:
        if not _same(_values(working, check["target"]), check["expected"]):
            _fail(f"result does not satisfy audited target {check['what']}")
    caught_up = _retire_caught_up_stats(overrides, previous, candidate)
    cross_check = _cross_check(_normalized(candidate, overrides), previous, bound_review)
    if cross_check is None:
        audit.pop("sourceCrossCheck", None)
    else:
        audit["sourceCrossCheck"] = cross_check
    audit.update({"patch": candidate.patch, "lookupHash": tft.json_hash(candidate.raw),
                  "binsHash": tft.json_hash(candidate.bins), "patchNotesHash": tft.json_hash(notes)})
    record = {"policyVersion": 2, "previousPatch": previous.patch,
              "previousLookupHash": tft.json_hash(previous.raw),
              "previousPatchNotesHash": tft.json_hash(prior_notes),
              "appliedChanges": applied, "definitionChanges": definition_changes,
              "outOfScope": ignored, "retiredStatCorrections": caught_up,
              "rationale": "No unreviewed modeled definition, timing, or mechanics change was accepted."}
    if accepted_lookup:
        record["lookupChanges"] = accepted_lookup
    if bound_review.document is not None:
        record['reviewManifestHash'] = tft.json_hash(bound_review.document)
        audit.setdefault('reviews', []).append(bound_review.document)
    unchanged = all(audit.get(key) == previous.audit.get(key)
                    for key in ('patch', 'lookupHash', 'binsHash', 'patchNotesHash'))
    if not (unchanged and audit.get('automatic')):
        if audit.get('automatic'):
            audit.setdefault('automaticHistory', []).append(deepcopy(audit['automatic']))
        audit['automatic'] = record
    return overrides, audit


def reconcile(candidate: 'tft.Snapshot', previous: 'tft.Snapshot', notes: dict,
              *, review: dict | None = None) -> tuple[dict, dict]:
    """Reconcile staged inputs without mutation; fail closed on incomplete
    evidence, ambiguous targets, or unexplained modeled definition changes.
    """
    try:
        return _reconcile(candidate, previous, notes, review)
    except ReviewRequired:
        raise
    except (KeyError, TypeError, IndexError, ValueError, AttributeError) as exc:
        _fail(f'malformed or unsupported staged TFT evidence: {exc}')
