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
                 for key in ("update", "section", "parent", "text"))


def _outside(entry, excluded_items):
    section, major = _norm(entry.get("section", "")), _norm(entry.get("major", ""))
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
    'auto', 'basic', 'attack', 'attacks', 'duration', 'count', 'counts', 'num',
    'number', 'radius', 'range', 'time', 'timing', 'delay', 'tick', 'ticks',
    'rate', 'frequency', 'interval', 'cooldown', 'windup', 'speed', 'targets',
    'chance', 'reduction', 'falloff', 'amplification', 'amp',
}


def _row_semantics(row):
    spaced = re.sub(r'([a-z])([A-Z])', r'\1 \2', row)
    tokens = set(re.findall(r'[a-z]+', spaced.lower()))
    if tokens & _NON_AMOUNT_TOKENS:
        return set()
    names = {'damage': {'damage', 'dmg'}, 'heal': {'heal', 'heals', 'healing', 'healed'},
             'shield': {'shield', 'shields', 'shielding'}}
    return {name for name, words in names.items() if tokens & words}


def _discover(change, previous, current):
    import tft
    label = _label(change["what"])
    units = [u for u in previous.units.values() if label.startswith(_norm(u["name"]))]
    if len(units) != 1:
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


class _DefinitionReview:
    """Validate raw changes without letting old overrides conceal them."""
    def __init__(self, previous, candidate, final, allowed, excluded):
        self.previous, self.candidate, self.final = previous, candidate, final
        self.allowed, self.excluded = allowed, excluded
        self.changes = []
        self.excluded_changed = set()

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
            old_keys, new_keys = set(before) - _COSMETIC_KEYS, set(after) - _COSMETIC_KEYS
            if old_keys != new_keys:
                _fail(f"definition structure changed at {path}: {sorted(old_keys ^ new_keys)}")
            for key in sorted(old_keys):
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
                        if stat == "damageByStar" and self._ad_series(a, b, context):
                            continue
                        _fail(f"unexplained base-stat change at {location}.{stat}: {a[stat]!r} to {b[stat]!r}")
                elif key in ("curveTable", "curveValues"):
                    if set(a or {}) != set(b or {}):
                        _fail(f"new or missing curve rows at {location}")
                    for name in a or {}:
                        if a[name] != b[name]:
                            self.curve(a[name], b[name], context, name, f"{location}.{name}")
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
        else:
            _fail(f"unverified formula, mechanic, or value at {path}: {str(before)[:80]!r} to {str(after)[:80]!r}")

    def _ad_series(self, before, after, context):
        old, new = before.get("damage"), after.get("damage")
        a, b = before.get("damageByStar"), after.get("damageByStar")
        return (isinstance(old, (int, float)) and old != 0 and isinstance(new, (int, float))
                and self.permit(context, "ad", None, new) and isinstance(a, list) and isinstance(b, list)
                and len(a) == len(b) and _same([v * new / old for v in a], b))

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
        return self.changes


def _reconcile(candidate, previous, notes):
    """Return publishable overrides/audit, or raise :class:`ReviewRequired`.

    The candidate is staged. Its copied audit/overrides are never treated as
    approval for new content. Hashes are rebound only after both note and raw
    definition comparisons succeed. Input objects and files remain unchanged.
    """
    import tft
    prior_notes = _read_notes(previous)
    _validate_sources(candidate, previous, notes, prior_notes)
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
    handled = set()
    for change in changes:
        matching = [c for c in checks if _label(change["what"]) in {_label(c["what"]), _label(c.get("patchLine", {}).get("what", c["what"]))}]
        reason = _outside(change, excluded_names)
        if not matching and reason:
            ignored.append({"change": deepcopy(change), "reason": reason})
            handled.add(_change_key(change))
            continue
        if not matching:
            matching = _discover(change, previous, working)
        old_numbers = _numbers(change["old"], change["what"])
        new_numbers = _numbers(change["new"], change["what"])
        if len(old_numbers) != len(new_numbers):
            _fail(f"{change['what']}: old/new array lengths differ")
        _check_numeric_units(change, matching)
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
            if target["kind"] == "unit" and "row" in target and len(new_numbers) > 1:
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
            if template in checks:
                checks[checks.index(template)] = check
            else:
                checks.append(check)
            working = _normalized(candidate, overrides)
        applied.append(deepcopy(change))
        handled.add(_change_key(change))

    old_entries = {_entry_key(entry) for entry in prior_notes.get("notes", [])}
    for entry in notes["notes"]:
        if _entry_key(entry) in old_entries:
            continue
        reason = _outside(entry, excluded_names)
        if reason:
            ignored.append({"note": deepcopy(entry), "reason": reason})
            continue
        text = entry.get("text", "")
        if "⇒" in text:
            # The parser must have retained the bullet's numeric statements.
            entry_label = _label((entry.get("parent", "") + " " + text.split(":", 1)[0]).strip())
            relevant = [c for c in notes['changes'] if _label(c['what']) == entry_label
                        and c.get('update', '') == entry.get('update', '')]
            if relevant and all(_change_key(c) in handled or _change_key(c) in old_changes for c in relevant):
                continue
        _fail(f"new unreviewed mechanics bullet [{entry.get('section', '')}]: {text[:180]}")

    review = _DefinitionReview(previous, candidate, working, allowed, excluded)
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
    audit.update({"patch": candidate.patch, "lookupHash": tft.json_hash(candidate.raw),
                  "binsHash": tft.json_hash(candidate.bins), "patchNotesHash": tft.json_hash(notes)})
    record = {"policyVersion": 1, "previousPatch": previous.patch,
              "previousLookupHash": tft.json_hash(previous.raw),
              "previousPatchNotesHash": tft.json_hash(prior_notes),
              "appliedChanges": applied, "definitionChanges": definition_changes,
              "outOfScope": ignored, "retiredStatCorrections": caught_up,
              "rationale": "No unreviewed modeled definition, timing, or mechanics change was accepted."}
    unchanged = all(audit.get(key) == previous.audit.get(key)
                    for key in ('patch', 'lookupHash', 'binsHash', 'patchNotesHash'))
    if not (unchanged and audit.get('automatic')):
        if audit.get('automatic'):
            audit.setdefault('automaticHistory', []).append(deepcopy(audit['automatic']))
        audit['automatic'] = record
    return overrides, audit


def reconcile(candidate: 'tft.Snapshot', previous: 'tft.Snapshot', notes: dict) -> tuple[dict, dict]:
    """Reconcile staged inputs without mutation; fail closed on incomplete
    evidence, ambiguous targets, or unexplained modeled definition changes.
    """
    try:
        return _reconcile(candidate, previous, notes)
    except ReviewRequired:
        raise
    except (KeyError, TypeError, IndexError, ValueError, AttributeError) as exc:
        _fail(f'malformed or unsupported staged TFT evidence: {exc}')
