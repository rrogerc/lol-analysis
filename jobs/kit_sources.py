#!/usr/bin/env python3
"""Sources and checks for champion kit dossiers (the Builds tab's kits).

A pilot of the staged kit pipeline: the numbers come from archived sources,
never from whoever writes the kit, and everything a model claims is checked
by this script.

  fetch  <slug>... [--patch P] [--offline]
                             archive Riot's character bin (spell data values,
                             spell calculations resolved to base + ratios),
                             ddragon's cooldowns/costs and the wiki's ability
                             templates under data/builds/sources/<patch>/<slug>/
  sheet  <slug>              print the numbers sheet a dossier is written from
  init   <slug>              write the parts skeleton (data/builds/dossiers/
                             <slug>.parts/): one stub per wiki leveling entry
                             with its verbatim quote, for a model to fill in
  check  <slug> [--slot Q]   validate data/builds/dossiers/<slug>.json: schema,
                             every quote verbatim in the archived wikitext,
                             every number against the bin rows it names, and
                             every data value of the five slot spells accounted
                             for (exit 1 on errors)
  score  <slug>              recall of a dossier against the hand-encoded kit
                             data/builds/<slug>.json (pilot evaluation only)
  primitives                 print the closed list of rotation primitives
  wiki-all [--patch P]       archive every champion's ability templates, for
                             the triage pass (data/builds/sources/<patch>/_triage/)

Stdlib only; does not import builds.py (no compiled engine needed).
"""

import argparse
import hashlib
import json
import math
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILDS_DATA_DIR = os.path.join(BASE_DIR, "data", "builds")
SOURCES_DIR = os.path.join(BUILDS_DATA_DIR, "sources")
DOSSIERS_DIR = os.path.join(BUILDS_DATA_DIR, "dossiers")
ITEMS_DIR = os.path.join(BASE_DIR, "data", "items")

DDRAGON_VERSIONS = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_INDEX = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
DDRAGON_CHAMP = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion/{cid}.json"
RIOT_CHAMP_BIN = ("https://raw.communitydragon.org/{patch}/game/data/characters/"
                  "{name}/{name}.bin.json")
WIKI_API = "https://wiki.leagueoflegends.com/en-us/api.php"
WIKI_CHAMPION_DATA = "Module:ChampionData/data"
USER_AGENT = "lol-analysis kit_sources (personal research; python urllib)"

SLOTS = ("P", "Q", "W", "E", "R")
LEVELS = 18

# mStat of a spell calculation part. 0 (absent), 2 and 12 are verified against
# the hand-encoded kits (Kassadin's AP ratios, Twitch's bonus AD, Vladimir's
# and Dr. Mundo's health ratios), 8 and 9 against Ashe's Frost Shot (1 + crit
# chance x (1 + bonus crit damage)); the rest are guesses from the enum's
# order and carry a "?" until a kit confirms them.
STAT_NAMES = {
    0: "ap", 1: "armor?", 2: "ad", 3: "attackSpeed?", 4: "attackWindup?", 5: "mr?",
    6: "moveSpeed?", 7: "stat7?", 8: "critChance", 9: "critDamage",
    10: "cooldownReduction?", 11: "abilityHaste?", 12: "maxHp",
}
SCOPES = {0: "total", 1: "base", 2: "bonus"}

# the stats a dossier term may name
TERM_STATS = {
    "ap", "ad", "bonusAd", "baseAd", "maxHp", "bonusHp", "missingHp", "armor", "bonusArmor",
    "mr", "bonusMr", "maxMana", "bonusMana", "missingMana", "attackSpeed", "bonusAttackSpeed",
    "moveSpeed", "critChance", "critDamage", "lethality", "level", "targetMaxHp",
    "targetCurrentHp", "targetMissingHp", "stacks", "other",
}
VALUE_ROLES = {
    "damage", "dot", "onhit", "self-buff", "target-debuff", "resource", "heal-shield",
    "duration", "count", "interval", "utility", "other",
}
UNUSED_REASONS = {
    "stale-duplicate", "range-or-geometry", "tooltip-only", "defensive", "utility",
    "not-in-a-dummy-fight", "unknown",
}
# The closed vocabulary of rotation primitives the triage counts. A kit whose
# damage fight needs only these could run on a generic scripted driver.
PRIMITIVES = {
    "plain-cast": "an ability that deals its damage on cast or on a fixed delay, on cooldown",
    "onhit-rider": "bonus damage on every basic attack (passive or toggle)",
    "empowered-attack": "an ability arms the next basic attack(s) with bonus damage",
    "attack-reset": "an ability resets the basic attack timer",
    "stacking-self-buff": "stacks gained by attacking or casting grant stats, with a cap and expiry",
    "timed-self-buff": "an ability grants attack speed, AD, AP or damage amp for a duration",
    "target-debuff": "resist shred or damage-taken amp on the target for a duration",
    "target-stacks": "stacks on the target that abilities or attacks add, read or consume",
    "dot": "damage over time or a ticking zone",
    "nth-hit-or-cast": "every Nth attack or cast is empowered",
    "charge-or-channel": "a charged or channelled ability that blocks other actions while it runs",
    "recast-or-multipart": "an ability with a recast or several separately timed parts",
    "resource-budget": "mana, energy or health costs that really limit casts inside a 15 s fight",
    "ramping-per-cast": "cost or damage that stacks up per cast",
    "cooldown-manipulation": "casts or hits refund or shorten cooldowns",
    "conditional-damage": "damage that depends on the target's state (missing, current or max "
                          "health, poisoned, marked, isolated, controlled)",
    "stat-conversion": "a passive converts one stat into another",
    "crit-interaction": "the kit changes how critical strikes work or lets abilities crit",
    "attack-speed-interaction": "the kit caps, converts or replaces attack speed",
    "level-gated-passive": "passive effects that switch on at champion levels",
    "stealth-or-opener": "a pre-fight state decides how the fight opens",
    "infinite-scaling": "permanent stacks from kills, time or hits: needs an assumed stack count",
    "execute-or-threshold": "an execute or a damage step below a health threshold",
    "form-or-stance": "transforms between forms with separate abilities",
    "weapon-or-ammo-system": "an ammo count, a weapon rotation or a reload governs attacks",
    "pet-or-summon": "a separate entity deals part of the damage",
    "positional-assumption": "damage depends on geometry a dummy fight must assume (returning "
                             "projectiles, walls, multi-hit shapes, distance)",
    "self-damage-or-health-cost": "abilities cost health or hurt the caster",
    "defensive-only": "a part of the kit that a damage-only dummy fight never reads",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def http_get(url, params=None, tries=4, timeout=60):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code in (403, 404):
                break
            time.sleep(1.5 * (attempt + 1))
    raise SystemExit(f"fetch failed: {url}: {last}")


def f32(x):
    return struct.unpack("f", struct.pack("f", x))[0]


def clean(x):
    """A bin float as the number Riot typed: the shortest decimal that is the
    same float32 (0.07000000029802322 -> 0.07)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return x
    if isinstance(x, int) or math.isinf(x) or math.isnan(x):
        return x
    target = f32(x)
    for nd in range(0, 9):
        r = round(x, nd)
        if f32(r) == target:
            return int(r) if nd == 0 or r == int(r) else r
    return x


def patch_key(p):
    return tuple(int(n) if n.isdigit() else 0 for n in p.split("."))


def default_patch():
    patches = sorted((p for p in os.listdir(ITEMS_DIR) if re.fullmatch(r"\d+\.\d+", p)),
                     key=patch_key) if os.path.isdir(ITEMS_DIR) else []
    if not patches:
        raise SystemExit("no data/items/<patch> snapshot to take the patch from; pass --patch")
    return patches[-1]


def source_dir(slug, patch=None):
    """The newest archived source directory of a champion (or the one of `patch`)."""
    if patch:
        return os.path.join(SOURCES_DIR, patch, slug)
    if os.path.isdir(SOURCES_DIR):
        for p in sorted(os.listdir(SOURCES_DIR), key=patch_key, reverse=True):
            d = os.path.join(SOURCES_DIR, p, slug)
            if os.path.isdir(d):
                return d
    raise SystemExit(f"no archived sources for '{slug}' - run `kit_sources.py fetch {slug}`")


def load_sources(slug, patch=None):
    d = source_dir(slug, patch)
    out = {}
    for name in ("meta", "sheet", "wiki"):
        with open(os.path.join(d, f"{name}.json")) as f:
            out[name] = json.load(f)
    return out


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# numbers on two axes: a scalar, a by-rank vector or a by-level vector
# ---------------------------------------------------------------------------

def num(v, axis=None):
    """A number: a plain scalar, or {"axis": "rank"|"level", "v": [...]}.
    A vector whose entries are all equal collapses to the scalar."""
    if isinstance(v, list):
        v = [clean(x) for x in v]
        if all(x == v[0] for x in v):
            return v[0]
        return {"axis": axis, "v": v}
    return clean(v)


def is_num(x):
    return isinstance(x, (int, float)) or (isinstance(x, dict) and "axis" in x)


def _zip(a, b, op):
    """Combine two numbers elementwise; None when their axes differ."""
    if not is_num(a) or not is_num(b):
        return None
    av, bv = isinstance(a, dict), isinstance(b, dict)
    if not av and not bv:
        return clean(op(a, b))
    if av and bv:
        if a["axis"] != b["axis"] or len(a["v"]) != len(b["v"]):
            return None
        return num([op(x, y) for x, y in zip(a["v"], b["v"])], a["axis"])
    vec, other, flip = (a, b, False) if av else (b, a, True)
    return num([op(other, x) if flip else op(x, other) for x in vec["v"]], vec["axis"])


def n_add(a, b):
    return _zip(a, b, lambda x, y: x + y)


def n_mul(a, b):
    return _zip(a, b, lambda x, y: x * y)


def fmt(x):
    if isinstance(x, dict):
        return ("L" if x["axis"] == "level" else "") + "[" + "/".join(f"{v:g}" for v in x["v"]) + "]"
    if isinstance(x, (int, float)):
        return f"{x:g}"
    return str(x)


# ---------------------------------------------------------------------------
# Riot's character bin -> a numbers sheet
# ---------------------------------------------------------------------------

class Spell:
    """One SpellObject's numbers at the ranks of its slot."""

    def __init__(self, key, obj, ranks):
        self.key = key
        self.script = obj.get("mScriptName") or key.rsplit("/", 1)[-1]
        self.sp = obj.get("mSpell") or {}
        self.ranks = ranks  # None: not a ranked slot (a passive or an extra spell)
        self.warnings = []
        self.values = {}
        for dv in self.sp.get("DataValues") or []:
            name, vals = dv.get("name"), dv.get("values")
            if name is None or not isinstance(vals, list):
                continue
            self.values[name] = self._by_rank(vals)

    def _by_rank(self, vals, from_zero=True):
        """Bin arrays carry a rank-0 entry first (DataValues, cooldownTime) —
        `mana` does not. Sliced to the slot's ranks; an unranked spell keeps
        ranks 1-6 (collapsed when constant)."""
        body = vals[1:] if from_zero else vals
        if self.ranks:
            body = body[:self.ranks]
        return num(body, "rank") if body else None

    def part(self, p, refs):
        """One calculation part -> (number | None, [terms], expr)."""
        t = p.get("__type", "?")
        stat = STAT_NAMES.get(p.get("mStat", 0), f"stat{p.get('mStat')}?")
        scope = SCOPES.get(p.get("mStatFormula", 0), f"formula{p.get('mStatFormula')}?")
        label = stat if scope == "total" else f"{scope} {stat}"
        if t == "NamedDataValueCalculationPart":
            name = p.get("mDataValue")
            refs.add(name)
            v = self.values.get(name)
            if v is None:
                self.warnings.append(f"calculation names a missing data value {name}")
            return v, [], f"{name}={fmt(v)}" if is_num(v) else f"{name}"
        if t == "NumberCalculationPart":
            v = clean(p.get("mNumber", 0.0))
            return v, [], fmt(v)
        if t == "EffectValueCalculationPart":
            i = p.get("mEffectIndex")
            eff = self.sp.get("mEffectAmount") or []
            v = None
            if isinstance(i, int) and 0 < i <= len(eff) and isinstance(eff[i - 1].get("value"), list):
                v = self._by_rank(eff[i - 1]["value"])
            return v, [], f"Effect{i}{fmt(v) if is_num(v) else ''}"
        if t == "StatByCoefficientCalculationPart":
            c = clean(p.get("mCoefficient", 0.0))
            return 0, [{"stat": stat, "scope": scope, "coef": c}], f"{fmt(c)}x{label}"
        if t == "StatByNamedDataValueCalculationPart":
            name = p.get("mDataValue")
            refs.add(name)
            c = self.values.get(name)
            return 0, [{"stat": stat, "scope": scope, "coef": c, "dataValue": name}], \
                (f"({name}={fmt(c)})x{label}" if is_num(c) else f"{name}x{label}")
        if t == "StatBySubPartCalculationPart":
            sub, sub_terms, sub_expr = self.part(p.get("mSubpart") or {}, refs)
            if sub_terms or not is_num(sub):
                return None, [], f"({sub_expr})x{label}"
            return 0, [{"stat": stat, "scope": scope, "coef": sub}], f"({sub_expr})x{label}"
        if t == "AbilityResourceByCoefficientCalculationPart":
            c = clean(p.get("mCoefficient", 0.0))
            return 0, [{"stat": "abilityResource", "scope": scope, "coef": c}], \
                f"{fmt(c)}x{scope} abilityResource"
        if t == "ByCharLevelInterpolationCalculationPart":
            a, b = p.get("mStartValue", 0.0), p.get("mEndValue", 0.0)
            v = num([a + (b - a) * (lv - 1) / (LEVELS - 1) for lv in range(1, LEVELS + 1)], "level")
            return v, [], f"byLevel({fmt(clean(a))}..{fmt(clean(b))})"
        if t == "ByCharLevelBreakpointsCalculationPart":
            # level 1 value; each later level adds the running per-level bonus
            # (a breakpoint may replace it from its level on) plus a
            # breakpoint's one-off bonus. Checked against Twitch's venom
            # (1/2/3/4/5 at 1/5/9/13/17) and Kayle's wave (20 + 3 a level from 12).
            per = p.get("mInitialBonusPerLevel", 0.0)
            bps = {b.get("mLevel"): b for b in p.get("mBreakpoints") or []}
            cur, out = p.get("mLevel1Value", 0.0), []
            for lv in range(1, LEVELS + 1):
                if lv > 1:
                    b = bps.get(lv, {})
                    if "mBonusPerLevelAtAndAfter" in b:
                        per = b["mBonusPerLevelAtAndAfter"]
                    cur += per + b.get("mAdditionalBonusAtThisLevel", 0.0)
                out.append(cur)
            v = num(out, "level")
            return v, [], f"byLevel{fmt(v)}"
        if t in ("BuffCounterByCoefficientCalculationPart",
                 "BuffCounterByNamedDataValueCalculationPart"):
            name = p.get("mDataValue")
            if name:
                refs.add(name)
            c = self.values.get(name) if name else clean(p.get("mCoefficient", 0.0))
            return 0, [{"stat": f"buffStacks({p.get('mBuffName')})", "scope": "total", "coef": c,
                        **({"dataValue": name} if name else {})}], \
                (f"({name}={fmt(c)})xbuffStacks" if name and is_num(c) else f"{fmt(c)}xbuffStacks")
        if t == "ProductOfSubPartsCalculationPart":
            a, at, ae = self.part(p.get("mPart1") or {}, refs)
            b, bt, be = self.part(p.get("mPart2") or {}, refs)
            expr = f"({ae})*({be})"
            if not at and not bt:
                return n_mul(a, b), [], expr
            # a number times stat terms: scale the coefficients
            for nside, tside in ((a, bt), (b, at)):
                other_terms = at if tside is bt else bt
                if not other_terms and is_num(nside):
                    scaled = [dict(tm, coef=n_mul(tm["coef"], nside)) for tm in tside]
                    if all(is_num(tm["coef"]) for tm in scaled):
                        return 0, scaled, expr
            return None, [], expr
        if t == "SumOfSubPartsCalculationPart":
            total, terms, exprs = 0, [], []
            for sub in p.get("mSubparts") or []:
                v, tm, ex = self.part(sub, refs)
                total = n_add(total, v) if total is not None and v is not None else None
                terms += tm
                exprs.append(ex)
            return total, terms, " + ".join(exprs)
        self.warnings.append(f"unresolved calculation part {t}")
        return None, [], f"<{t}>"

    def calc(self, name, c, seen=()):
        """One spell calculation -> {"expr", "flat", "terms", "refs", ...}."""
        refs, t = set(), c.get("__type")
        out = {}
        if t == "GameCalculation":
            flat, terms, exprs, ok = 0, [], [], True
            for p in c.get("mFormulaParts") or []:
                v, tm, ex = self.part(p, refs)
                exprs.append(ex)
                terms += tm
                if v is None:
                    ok = False
                elif ok:
                    s = n_add(flat, v)
                    ok, flat = (s is not None), (s if s is not None else flat)
            expr = " + ".join(exprs) or "0"
            if "mMultiplier" in c:
                m, mt, me = self.part(c["mMultiplier"], refs)
                expr = f"({expr}) * {me}"
                if ok and not mt and is_num(m):
                    f2 = n_mul(flat, m)
                    t2 = [dict(tm, coef=n_mul(tm["coef"], m)) for tm in terms]
                    if f2 is not None and all(is_num(tm["coef"]) for tm in t2):
                        flat, terms = f2, t2
                    else:
                        ok = False
                else:
                    ok = False
            out = {"expr": expr, "flat": flat if ok else None, "terms": terms if ok else None}
            if not ok:
                out["unresolved"] = True
        elif t == "GameCalculationModified":
            base = c.get("mModifiedGameCalculation")
            m, mt, me = self.part(c.get("mMultiplier") or {}, refs)
            out = {"expr": f"{base} * {me}", "modifies": base, "flat": None, "terms": None,
                   "unresolved": True}
            calcs = self.sp.get("mSpellCalculations") or {}
            if base in calcs and base not in seen and not mt and is_num(m):
                inner = self.calc(base, calcs[base], seen + (name,))
                refs |= set(inner["refs"])
                if not inner.get("unresolved"):
                    flat = n_mul(inner["flat"], m)
                    terms = [dict(tm, coef=n_mul(tm["coef"], m)) for tm in inner["terms"]]
                    if flat is not None and all(is_num(tm["coef"]) for tm in terms):
                        out.update(flat=flat, terms=terms)
                        del out["unresolved"]
        elif t == "GameCalculationConditional":
            out = {"expr": f"{c.get('mDefaultGameCalculation')} unless "
                           f"{json.dumps(c.get('mConditionalCalculationRequirements'))[:80]} then "
                           f"{c.get('mConditionalGameCalculation')}",
                   "flat": None, "terms": None, "unresolved": True}
        else:
            self.warnings.append(f"unresolved calculation type {t}")
            out = {"expr": f"<{t}>", "flat": None, "terms": None, "unresolved": True}
        if c.get("mDisplayAsPercent"):
            out["displayAsPercent"] = True
        if c.get("tooltipOnly"):
            out["tooltipOnly"] = True
        out["refs"] = sorted(r for r in refs if r)
        return out

    def sheet(self):
        sp = self.sp
        out = {"script": self.script, "key": self.key}
        if isinstance(sp.get("cooldownTime"), list):
            out["cooldown"] = self._by_rank(sp["cooldownTime"])
        # two cost arrays: manaValues is the live one (it agrees with ddragon
        # and the wiki where the older `mana` does not: Jax's Q 50 vs 65,
        # Cassiopeia's E 45 vs 40). Neither carries a rank-0 entry.
        live = (sp.get("manaValues") or {}).get("values")
        legacy = sp.get("mana")
        if isinstance(live, list) or isinstance(legacy, list):
            out["cost"] = self._by_rank(live if isinstance(live, list) else legacy,
                                        from_zero=False)
            if isinstance(live, list) and isinstance(legacy, list):
                old_cost = self._by_rank(legacy, from_zero=False)
                if old_cost != out["cost"]:
                    out["costLegacy"] = old_cost
        for src, dst in (("mCastTime", "castTime"), ("mCantCancelWhileWindingUp", "uncancellableWindup")):
            if src in sp:
                out[dst] = clean(sp[src])
        if self.values:
            out["dataValues"] = self.values
        calcs = sp.get("mSpellCalculations") or {}
        if calcs:
            out["calcs"] = {n: self.calc(n, c) for n, c in calcs.items()}
        if self.warnings:
            out["warnings"] = sorted(set(self.warnings))
        return out


def bin_sheet(bin_json, ddragon):
    """The champion's spells by slot: {"P": [spell...], "Q": [...], ..., "other": [...]}.
    A slot's first spell is its root (the one ddragon and the HUD name)."""
    record = next((v for v in bin_json.values()
                   if isinstance(v, dict) and v.get("__type") == "CharacterRecord"), None)
    if record is None:
        raise SystemExit("the bin has no CharacterRecord")
    spells = {k: v for k, v in bin_json.items()
              if isinstance(v, dict) and v.get("__type") == "SpellObject"}
    by_lower = {k.lower(): k for k in spells}

    def find(name):
        if not name:
            return None
        if name in spells:
            return name
        tail = "/" + name.lower()
        hits = [k for low, k in by_lower.items() if low.endswith("/spells" + tail) or low.endswith(tail)]
        return min(hits, key=len) if hits else None

    ranks = {"P": None}
    for slot, sp in zip("QWER", ddragon["spells"]):
        ranks[slot] = sp.get("maxrank")
    roots = {}
    for slot, name in zip("QWER", record.get("spellNames") or []):
        roots[slot] = find(name)
    roots["P"] = find(record.get("mCharacterPassiveSpell"))
    slot_of = {}
    for slot, root in roots.items():
        if root:
            slot_of[root] = slot
    # an AbilityObject groups a slot's root spell with its child spells
    for v in bin_json.values():
        if isinstance(v, dict) and v.get("__type") == "AbilityObject":
            slot = slot_of.get(find(v.get("mRootSpell")) or "")
            if slot:
                for child in v.get("mChildSpells") or []:
                    k = find(child)
                    if k and k not in slot_of:
                        slot_of[k] = slot
    out = {s: [] for s in SLOTS + ("other",)}
    for k in sorted(spells, key=lambda k: (k not in roots.values(), k)):
        slot = slot_of.get(k, "other")
        sh = Spell(k, spells[k], ranks.get(slot)).sheet()
        if "dataValues" in sh or "calcs" in sh or k in roots.values():
            out[slot].append(sh)
    res = record.get("primaryAbilityResource") or {}
    resource = {"arType": res.get("arType")}
    for k, v in res.items():
        if isinstance(v, dict) and "baseValue" in v:
            resource.setdefault("values", {})[k] = clean(v["baseValue"])
    return {"slots": out, "roots": {s: (roots[s] or "").rsplit("/", 1)[-1] for s in roots},
            "resource": resource,
            "attack": {"attackDelayCastOffsetPercent":
                       clean((record.get("basicAttack") or {}).get("mAttackDelayCastOffsetPercent"))}}


# ---------------------------------------------------------------------------
# the wiki: Module:ChampionData + one template per ability
# ---------------------------------------------------------------------------

def wiki_query(titles):
    """{title: {"revid", "timestamp", "wikitext"}} for up to 50 titles a call."""
    out = {}
    for i in range(0, len(titles), 40):
        chunk = titles[i:i + 40]
        raw = http_get(WIKI_API, {"action": "query", "prop": "revisions", "rvslots": "main",
                                  "rvprop": "content|ids|timestamp", "format": "json",
                                  "redirects": 1, "titles": "|".join(chunk)})
        data = json.loads(raw)["query"]
        # a requested title may be normalized and then redirected ("Kled &
        # Skaarl" keeps its templates under "Kled"): key results by what was asked
        asked = {t: [t] for t in chunk}
        for step in ("normalized", "redirects"):
            for n in data.get(step, []):
                asked.setdefault(n["to"], []).extend(asked.get(n["from"], [n["from"]]))
        for page in data.get("pages", {}).values():
            if "revisions" not in page:
                continue
            rev = page["revisions"][0]
            for title in asked.get(page["title"], [page["title"]]):
                out[title] = {"revid": rev["revid"], "timestamp": rev["timestamp"],
                              "wikitext": rev["slots"]["main"]["*"]}
        time.sleep(0.3)
    return out


def lua_block(text, start):
    """The balanced {...} that opens at or after `start`."""
    i = text.index("{", start)
    depth, j = 0, i
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
        j += 1
    raise ValueError("unbalanced Lua table")


def champion_data(lua):
    """Module:ChampionData/data -> {apiname.lower(): {"wikiName", "skills": {slot: [names]}, ...}}."""
    out = {}
    for m in re.finditer(r'^  \["([^"]+)"\]\s*=\s*\{', lua, re.M):
        block = lua_block(lua, m.start())
        api = re.search(r'\["apiname"\]\s*=\s*"([^"]+)"', block)
        if not api:
            continue
        skills = {}
        for slot, key in zip(SLOTS, ("skill_i", "skill_q", "skill_w", "skill_e", "skill_r")):
            sm = re.search(r'\["%s"\]\s*=\s*(\{[^}]*\})' % key, block)
            skills[slot] = re.findall(r'"([^"]+)"', sm.group(1)) if sm else []
        rec = {"wikiName": m.group(1), "skills": skills}
        for key in ("resource", "rangetype", "changes", "herotype", "alttype", "adaptivetype"):
            km = re.search(r'\["%s"\]\s*=\s*"([^"]*)"' % key, block)
            if km:
                rec[key] = km.group(1)
        sm = re.search(r'\["stats"\]\s*=\s*', block)
        if sm:
            stats = lua_block(block, sm.end() - 1)
            rec["stats"] = {k: clean(float(v)) for k, v in
                            re.findall(r'^\s{6}\["(\w+)"\]\s*=\s*(-?[\d.]+)', stats, re.M)}
        out[api.group(1).lower()] = rec
    return out


WIKI_FLAG_FIELDS = ("damagetype", "spelleffects", "onhiteffects", "spellshield", "projectile",
                    "targeting", "affects", "costtype", "occurrence")


VARDEFINE = re.compile(r"\{\{#vardefine:\s*([^|{}]+?)\s*\|([^{}]*)\}\}")


def expand_vars(wikitext):
    """The wikitext with every {{#var:name}} replaced by its {{#vardefine}}
    value: some templates keep their numbers in variables, and a quote has to
    show the numbers it is quoted for."""
    values = {name: value.strip() for name, value in VARDEFINE.findall(wikitext)}
    return re.sub(r"\{\{#var:\s*([^|{}]+?)\s*\}\}",
                  lambda m: values.get(m.group(1), m.group(0)), wikitext)


def template_fields(wikitext):
    """The top-level |name = value fields of an ability template. Values are
    verbatim slices of the (variable-expanded) wikitext, stripped, so they
    can serve as quotes."""
    wikitext = expand_vars(wikitext)
    start = wikitext.find("{{{{{1")  # the ability template, past any #vardefine preamble
    body, depth, i = wikitext[max(start, 0):], 0, 0
    parts, last, opened = [], 0, False
    while i < len(body):
        two = body[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            opened = True
            i += 2
            continue
        if two in ("}}", "]]"):
            depth -= 1
            if opened and depth == 0:  # the template closes here
                break
            i += 2
            continue
        if body[i] == "|" and depth == 1:
            parts.append(body[last:i])
            last = i + 1
        i += 1
    parts.append(body[last:i])
    fields = {}
    for part in parts[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if re.fullmatch(r"[\w ]+", k):
                fields[k] = v.strip()
    return fields


def fetch_wiki(api_id, cdata=None):
    if cdata is None:
        lua = wiki_query([WIKI_CHAMPION_DATA])[WIKI_CHAMPION_DATA]["wikitext"]
        cdata = champion_data(lua)
    rec = cdata.get(api_id.lower())
    if rec is None:
        raise SystemExit(f"the wiki's ChampionData has no apiname {api_id}")
    titles = {}
    for slot in SLOTS:
        for name in rec["skills"][slot]:
            titles[f"Template:Data {rec['wikiName']}/{name}"] = (slot, name)
    pages = wiki_query(list(titles))
    abilities = {s: [] for s in SLOTS}
    for title, (slot, name) in titles.items():
        page = pages.get(title)
        if page is None:
            abilities[slot].append({"name": name, "title": title, "missing": True})
            continue
        fields = template_fields(page["wikitext"])
        abilities[slot].append({
            "name": name, "title": title, "revid": page["revid"],
            "timestamp": page["timestamp"],
            "flags": {k: fields[k] for k in WIKI_FLAG_FIELDS if fields.get(k)},
            "wikitext": page["wikitext"]})
    return {"champion": rec, "abilities": abilities}


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

_shared = {}  # downloads every champion of a run shares


def shared(key, load):
    if key not in _shared:
        _shared[key] = load()
    return _shared[key]


def ddragon_index(patch):
    """(version, {champion id: entry}) of ddragon's champion list for a patch."""
    versions = shared("versions", lambda: json.loads(http_get(DDRAGON_VERSIONS)))
    version = next((v for v in versions if v.startswith(patch + ".")), None)
    if version is None:
        raise SystemExit(f"ddragon has no version for patch {patch}")
    return version, shared(("index", version), lambda: json.loads(
        http_get(DDRAGON_INDEX.format(version=version)))["data"])


def ddragon_champion(slug, patch):
    version, index = ddragon_index(patch)
    cid = next((c for c in index if c.lower() == slug), None)
    if cid is None:
        raise SystemExit(f"ddragon {version} has no champion '{slug}'")
    full = json.loads(http_get(DDRAGON_CHAMP.format(version=version, cid=cid)))["data"][cid]
    spells = [{"id": s["id"], "name": s["name"], "maxrank": s["maxrank"],
               "cooldown": s["cooldown"], "cost": s["cost"]} for s in full["spells"]]
    return {"version": version, "id": cid, "name": full["name"], "partype": full.get("partype"),
            "passive": full["passive"]["name"], "spells": spells}


def cmd_fetch(args):
    patch = args.patch or default_patch()
    cdata = None
    for slug in args.slugs:
        slug = slug.lower()
        out = os.path.join(SOURCES_DIR, patch, slug)
        url = RIOT_CHAMP_BIN.format(patch=patch, name=slug)
        if args.offline:
            # the script changed, the sources did not: rebuild from the archive
            with open(os.path.join(out, "bin.json"), "rb") as f:
                raw = f.read()
            with open(os.path.join(out, "sheet.json")) as f:
                dd = json.load(f)["ddragon"]
            with open(os.path.join(out, "wiki.json")) as f:
                wiki = json.load(f)
            with open(os.path.join(out, "meta.json")) as f:
                fetched_at = json.load(f)["fetchedAt"]
        else:
            dd = ddragon_champion(slug, patch)
            raw = http_get(url)
            if cdata is None:
                cdata = shared("championdata", lambda: champion_data(
                    wiki_query([WIKI_CHAMPION_DATA])[WIKI_CHAMPION_DATA]["wikitext"]))
            wiki = fetch_wiki(dd["id"], cdata)
            fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        sheet = bin_sheet(json.loads(raw), dd)
        sheet["ddragon"] = dd
        sheet["wikiFlags"] = {s: {a["name"]: a.get("flags", {}) for a in wiki["abilities"][s]}
                              for s in SLOTS}
        meta = {"slug": slug, "patch": patch, "fetchedAt": fetched_at,
                "riotSource": url, "riotSha256": hashlib.sha256(raw).hexdigest(),
                "ddragonVersion": dd["version"], "wikiChanges": wiki["champion"].get("changes"),
                "wikiRevisions": {a["title"]: a.get("revid") for s in SLOTS
                                  for a in wiki["abilities"][s]}}
        write_json(os.path.join(out, "meta.json"), meta)
        write_json(os.path.join(out, "sheet.json"), sheet)
        write_json(os.path.join(out, "wiki.json"), wiki)
        if not args.offline:
            with open(os.path.join(out, "bin.json"), "wb") as f:
                f.write(raw)
        n_vals = sum(len(sp.get("dataValues", {})) for s in sheet["slots"].values() for sp in s)
        n_calcs = sum(len(sp.get("calcs", {})) for s in sheet["slots"].values() for sp in s)
        warn = sorted({w for s in sheet["slots"].values() for sp in s for w in sp.get("warnings", [])})
        print(f"{slug} {patch}: {n_vals} data values, {n_calcs} calculations, "
              f"{sum(len(v) for v in wiki['abilities'].values())} wiki templates "
              f"(wiki last changed {meta['wikiChanges']}) -> {os.path.relpath(out, BASE_DIR)}")
        for w in warn:
            print(f"  warning: {w}")


def cmd_wiki_all(args):
    """Every champion's ability templates, fields only, for the triage pass."""
    patch = args.patch or default_patch()
    lua = wiki_query([WIKI_CHAMPION_DATA])[WIKI_CHAMPION_DATA]["wikitext"]
    cdata = champion_data(lua)
    titles = {}
    for api, rec in cdata.items():
        for slot in SLOTS:
            for name in rec["skills"][slot]:
                titles[f"Template:Data {rec['wikiName']}/{name}"] = (api, slot, name)
    print(f"{len(cdata)} champions, {len(titles)} ability templates")
    pages = wiki_query(list(titles))
    keep = ("description", "leveling", "cooldown", "cost", "costtype", "cast time", "recharge",
            "static", "damagetype", "spelleffects", "onhiteffects")
    out = {}
    for title, (api, slot, name) in titles.items():
        page = pages.get(title)
        champ = out.setdefault(api, {"wikiName": cdata[api]["wikiName"],
                                     "resource": cdata[api].get("resource"),
                                     "rangetype": cdata[api].get("rangetype"),
                                     "abilities": {s: [] for s in SLOTS}})
        if page is None:
            champ["abilities"][slot].append({"name": name, "missing": True})
            continue
        fields = template_fields(page["wikitext"])
        champ["abilities"][slot].append(
            {"name": name, **{k: v for k, v in fields.items()
                              if v and any(k == p or re.fullmatch(p + r"\d*", k) for p in keep)}})
    path = os.path.join(SOURCES_DIR, patch, "_triage", "abilities.json")
    write_json(path, out)
    print(f"-> {os.path.relpath(path, BASE_DIR)} ({os.path.getsize(path) // 1024} KB)")


# ---------------------------------------------------------------------------
# sheet (the human- and model-readable numbers sheet)
# ---------------------------------------------------------------------------

def sheet_text(src):
    """The numbers sheet as the text a dossier is written from."""
    sheet, meta = src["sheet"], src["meta"]
    out = [f"# {meta['slug']} - Riot's bin at {meta['patch']} ({meta['riotSha256'][:12]}), "
           f"ddragon {meta['ddragonVersion']}, wiki last changed {meta['wikiChanges']}"]
    for i, sp in enumerate(sheet["ddragon"]["spells"]):
        out.append(f"ddragon {'QWER'[i]} {sp['name']}: maxrank {sp['maxrank']}, "
                   f"cooldown {sp['cooldown']}, cost {sp['cost']}")
    for slot in SLOTS + ("other",):
        for sp in sheet["slots"][slot]:
            out.append(f"\n## {slot} {sp['script']}")
            for k in ("cooldown", "cost", "costLegacy", "castTime"):
                if k in sp:
                    out.append(f"  {k}: {fmt(sp[k])}")
            for name, v in sp.get("dataValues", {}).items():
                out.append(f"  {sp['script']}.{name} = {fmt(v)}")
            for name, c in sp.get("calcs", {}).items():
                tag = " (tooltip only)" if c.get("tooltipOnly") else ""
                tag += " (percent)" if c.get("displayAsPercent") else ""
                out.append(f"  {sp['script']}#{name}{tag} = {c['expr']}")
            for w in sp.get("warnings", []):
                out.append(f"  ! {w}")
    return "\n".join(out)


def cmd_sheet(args):
    print(sheet_text(load_sources(args.slug.lower(), args.patch)))


# ---------------------------------------------------------------------------
# init / assemble: a dossier is written as parts, one ability at a time
# ---------------------------------------------------------------------------

def parts_dir(slug):
    return os.path.join(DOSSIERS_DIR, f"{slug}.parts")


def balanced(text, start):
    """The end index (exclusive) of the {{...}} that opens at `start`."""
    depth, i = 0, start
    while i < len(text):
        two = text[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
        elif two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return len(text)


def st_entries(wikitext):
    """(label, value) of every {{st|label|value|label2|value2...}} block: the
    wiki's leveling tables. The value is a verbatim slice of the wikitext."""
    wikitext = expand_vars(wikitext)
    out, i = [], 0
    while True:
        i = wikitext.find("{{st|", i)
        if i < 0:
            return out
        end = balanced(wikitext, i)
        body = wikitext[i + 5:end - 2]
        parts, depth, last, j = [], 0, 0, 0
        while j < len(body):
            two = body[j:j + 2]
            if two in ("{{", "[["):
                depth += 1
                j += 2
                continue
            if two in ("}}", "]]"):
                depth -= 1
                j += 2
                continue
            if body[j] == "|" and depth == 0:
                parts.append(body[last:j])
                last = j + 1
            j += 1
        parts.append(body[last:])
        for k in range(0, len(parts) - 1, 2):
            out.append((re.sub(r"[{}']|\[\[|\]\]", "", parts[k]).strip(), parts[k + 1].strip()))
        i = end


def cmd_init(args):
    """Write the parts skeleton: names, the wiki's cooldown/cost/cast-time
    fields and one stub per leveling entry, each with its verbatim quote.
    Everything a stub leaves null the checker reports until it is filled."""
    slug = args.slug.lower()
    src = load_sources(slug, args.patch)
    out = parts_dir(slug)
    if os.path.isdir(out) and os.listdir(out) and not args.force:
        raise SystemExit(f"{os.path.relpath(out, BASE_DIR)} exists; pass --force to overwrite it")
    for slot in SLOTS:
        pages = src["wiki"]["abilities"][slot]
        part = {"name": " / ".join(a["name"] for a in pages) or None,
                "cooldown": None, "cost": None, "castTime": None,
                "values": [], "mechanics": [], "binUnused": []}
        n = 0
        for a in pages:
            fields = template_fields(a.get("wikitext", ""))
            for field, key, inner in (("cooldown", "cooldown", "values"), ("static", "cooldown", "values"),
                                      ("cost", "cost", "values"), ("cast time", "castTime", "value")):
                if fields.get(field) and part[key] is None:
                    part[key] = {inner: None, "quote": fields[field]}
                    if key == "cost":
                        part[key]["resource"] = (fields.get("costtype") or "").lower() or None
            for label, value in st_entries(a.get("wikitext", "")):
                n += 1
                part["values"].append({
                    "id": f"{slot.lower()}_{n}", "meaning": label, "role": None,
                    "damageType": None, "by": None, "base": None, "terms": [],
                    "quote": value, "bin": []})
        roots = src["sheet"]["slots"].get(slot) or []
        if roots:
            part["binUnused"] = [{"ref": f"{roots[0]['script']}.{name}", "reason": None}
                                 for name in roots[0].get("dataValues", {})]
        write_json(os.path.join(out, f"{slot}.json"), part)
    write_json(os.path.join(out, "top.json"), {
        "champion": slug, "primitives": [], "needsBespoke": {"value": None, "why": None},
        "unsettled": [], "rotation": {"proposal": [], "questions": []}})
    print(f"skeleton -> {os.path.relpath(out, BASE_DIR)}/ (P Q W E R top).json")


def assemble(slug):
    """data/builds/dossiers/<slug>.parts/*.json -> the dossier, when parts exist."""
    pdir = parts_dir(slug)
    if not os.path.isdir(pdir):
        return None
    def part(name):
        path = os.path.join(pdir, f"{name}.json")
        try:
            with open(path) as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as e:
            raise SystemExit(f"{os.path.relpath(path, BASE_DIR)} is not valid JSON: {e}")
    top = part("top") or {}
    dossier = {"champion": top.get("champion", slug),
               "abilities": {s: part(s) for s in SLOTS if part(s) is not None}}
    dossier.update({k: v for k, v in top.items() if k != "champion"})
    path = os.path.join(DOSSIERS_DIR, f"{slug}.json")
    write_json(path, dossier)
    return path


# ---------------------------------------------------------------------------
# check: a dossier against the archived sources
# ---------------------------------------------------------------------------

def norm_ws(s):
    return re.sub(r"\s+", " ", s).strip()


def _words(text):
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r" \1 ", text)  # [[target|label]] -> label
    text = re.sub(r"[^0-9A-Za-z%.+*/-]+", " ", text)
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)
    # a lone sign is punctuation, not a word: "(+ 20% AP)" and "20% AP" quote the same thing
    return " ".join(w for w in text.lower().split() if re.search(r"[0-9a-z]", w))


def loose(text):
    """Two readings of a text with its wiki markup dropped, as words and
    numbers: every template argument kept, and only each template's last
    positional argument kept (what the page renders: {{tip|root|roots}} ->
    roots). A quote copied from the rendered page ("roots them for 2
    seconds") still has to find every word, in order, in one of them."""
    every = re.sub(r"\{\{\s*[#\w: ]+\|", " ", text)  # template openers: {{tip|
    shown = text
    for _ in range(12):  # innermost templates first
        reduced = re.sub(
            r"\{\{([^{}]*)\}\}",
            lambda m: " " + next((a for a in reversed(m.group(1).split("|")[1:])
                                  if not re.match(r"\s*[\w ]+=", a)), "") + " ", shown)
        if reduced == shown:
            break
        shown = reduced
    return _words(every), _words(shown)


def seq(x):
    """A dossier or sheet number as a tuple of floats (a scalar is length 1)."""
    if isinstance(x, dict) and "v" in x:
        x = x["v"]
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return (float(x),)
    if isinstance(x, list) and x and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                         for v in x):
        return tuple(float(v) for v in x)
    return None


def seq_match(a, b):
    """Whether two sequences agree up to the percent/fraction convention. A
    constant on one side matches a constant vector on the other."""
    if a is None or b is None:
        return False
    if len(a) != len(b):
        if len(set(a)) == 1 and len(set(b)) == 1:
            a, b = a[:1], b[:1]
        else:
            return False
    for k in (1.0, 100.0, 0.01):
        if all(math.isclose(x * k, y, rel_tol=1e-4, abs_tol=1e-6) for x, y in zip(a, b)):
            return True
    return False


class Checker:
    def __init__(self, slug, dossier, src):
        self.slug, self.d, self.src = slug, dossier, src
        self.errors, self.warnings, self.info = [], [], []
        self.spells = {}  # script name -> sheet spell
        self.slot_of = {}
        for slot, sps in src["sheet"]["slots"].items():
            for sp in sps:
                self.spells[sp["script"]] = sp
                self.slot_of[sp["script"]] = slot
        self.wikitext = {slot: [norm_ws(text) for a in abilities
                                for text in (a.get("wikitext", ""),
                                             expand_vars(a.get("wikitext", "")))]
                         for slot, abilities in src["wiki"]["abilities"].items()}
        self.all_wikitext = [t for ts in self.wikitext.values() for t in ts]
        self.loose_wikitext = [view for t in self.all_wikitext for view in loose(t)]
        self.sheet_words = _words(sheet_text(src)) if "meta" in src else ""
        self.used = set()  # (script, data value) rows some value accounts for

    def err(self, where, msg):
        self.errors.append(f"{where}: {msg}")

    def warn(self, where, msg):
        self.warnings.append(f"{where}: {msg}")

    def quote(self, where, q, slot=None, required=True):
        if q is None or q == "":
            if required:
                self.err(where, "needs a quote")
            return False
        if not isinstance(q, str):
            self.err(where, "quote must be a string")
            return False
        nq = norm_ws(q)
        texts = self.wikitext.get(slot, []) if slot else self.all_wikitext
        if any(nq in t for t in texts):
            return True
        if any(nq in t for t in self.all_wikitext):
            self.warn(where, "quote is from another ability's template")
            return True
        # the same words in the same order, markup aside; "..." may join pieces
        pieces = [loose(piece) for piece in re.split(r"\.{3,}|\u2026", q)]
        pieces = [views for views in pieces if any(views)]
        if pieces and all(any(f" {view} " in f" {t} " for view in views if view
                              for t in self.loose_wikitext) for views in pieces):
            return True
        self.err(where, f"quote is not in the archived wikitext: {q[:90]!r}")
        return False

    def bin_pool(self, where, refs):
        """The number sequences the named bin rows offer: data values as they
        are, a calculation's flat part and each term's coefficient (and the
        data values it reads)."""
        pool = []
        for ref in refs:
            if not isinstance(ref, str) or not re.fullmatch(r"[^.#]+[.#].+", ref):
                self.err(where, f"bin ref {ref!r} is not 'Spell.DataValue' or 'Spell#Calc'")
                continue
            script, sep, name = re.match(r"([^.#]+)([.#])(.+)", ref).groups()
            sp = self.spells.get(script)
            if sp is None:
                self.err(where, f"bin ref {ref!r}: no spell {script!r} in the sheet")
                continue
            if sep == ".":
                if name in ("cooldown", "cost", "castTime") and name in sp:
                    pool.append((ref, seq(sp[name])))
                    continue
                if name not in sp.get("dataValues", {}):
                    self.err(where, f"bin ref {ref!r}: {script} has no data value {name!r}")
                    continue
                self.used.add((script, name))
                pool.append((ref, seq(sp["dataValues"][name])))
            else:
                c = sp.get("calcs", {}).get(name)
                if c is None:
                    self.err(where, f"bin ref {ref!r}: {script} has no calculation {name!r}")
                    continue
                for r in c.get("refs", []):
                    self.used.add((script, r))
                    if r in sp.get("dataValues", {}):
                        pool.append((f"{script}.{r}", seq(sp["dataValues"][r])))
                if c.get("flat") is not None:
                    pool.append((ref + " flat", seq(c["flat"])))
                for tm in c.get("terms") or []:
                    pool.append((ref + f" {tm['stat']}", seq(tm["coef"])))
                for base in ([c["modifies"]] if c.get("modifies") else []):
                    inner = sp.get("calcs", {}).get(base, {})
                    for r in inner.get("refs", []):
                        self.used.add((script, r))
        return [(r, s) for r, s in pool if s is not None]

    def numbers(self, where, value):
        """Every number a value states, as (label, sequence)."""
        out = []
        if value.get("base") is not None:
            s = seq(value["base"])
            if s is None:
                self.err(where, "base must be a number or a list of numbers")
            else:
                out.append(("base", s))
        for i, tm in enumerate(value.get("terms") or []):
            if not isinstance(tm, dict) or tm.get("stat") not in TERM_STATS:
                self.err(where, f"terms[{i}].stat must be one of {sorted(TERM_STATS)}")
                continue
            s = seq(tm.get("coef"))
            if s is None:
                self.err(where, f"terms[{i}].coef must be a number or a list of numbers")
            else:
                out.append((f"terms[{i}] {tm['stat']}", s))
            for j, sc in enumerate(tm.get("coefScaling") or []):
                s2 = seq(sc.get("coef")) if isinstance(sc, dict) else None
                if s2 is None or sc.get("stat") not in TERM_STATS:
                    self.err(where, f"terms[{i}].coefScaling[{j}] needs stat, per and coef")
                else:
                    out.append((f"terms[{i}].coefScaling[{j}]", s2))
        return out

    def in_quote(self, where, label, s, q):
        """The first and last number of a sequence should be readable in the
        quote (as written, or as a percentage)."""
        text = re.sub(r"(?<=\d),(?=\d{3})", "", q or "")
        found = {float(x) for x in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", text)}
        for v in {s[0], s[-1]}:
            if not any(math.isclose(v * k, f, rel_tol=1e-6, abs_tol=1e-9)
                       for k in (1, 100) for f in found):
                self.warn(where, f"{label}: {v:g} is not readable in the quote")

    def timed(self, where, slot, field, obj, bin_key):
        """cooldown / cost / castTime: {"values"|"value", "quote"} against the
        slot's root spell and ddragon."""
        if obj is None:
            return
        if not isinstance(obj, dict):
            self.err(where, f"{field} must be an object or null")
            return
        v = obj.get("values", obj.get("value"))
        if v is None:
            return
        s = seq(v)
        if s is None:
            self.err(where, f"{field} must hold a number or a list of numbers")
            return
        self.quote(f"{where}.{field}", obj.get("quote"), slot)
        roots = self.src["sheet"]["slots"].get(slot) or []
        root = roots[0] if roots else {}
        for name, v in root.get("dataValues", {}).items():
            if re.search(r"cooldown|(^|[a-z])CD|cost|mana|energy", name, re.I if field != "cooldown" else 0) \
                    or (field == "cooldown" and re.search(r"cooldown|CD", name, re.I)):
                if seq_match(s, seq(v)):
                    self.used.add((root["script"], name))
        if bin_key in root and not seq_match(s, seq(root[bin_key])):
            note = obj.get("binNote")
            (self.warn if note else self.err)(
                f"{where}.{field}", f"{list(s)} differs from the bin's {fmt(root[bin_key])}"
                + (f" (noted: {note})" if note else " - fix it or explain it in binNote"))

    def run(self):
        d = self.d
        if not isinstance(d, dict):
            self.err("dossier", "must be a JSON object")
            return
        if d.get("champion") != self.slug:
            self.err("champion", f"must be {self.slug!r}")
        abilities = d.get("abilities")
        if not isinstance(abilities, dict) or set(abilities) != set(SLOTS):
            self.err("abilities", f"must have exactly the slots {list(SLOTS)}")
            abilities = abilities if isinstance(abilities, dict) else {}
        ids = set()
        for slot in SLOTS:
            ab = abilities.get(slot)
            if not isinstance(ab, dict):
                continue
            where = f"abilities.{slot}"
            if not isinstance(ab.get("name"), str):
                self.err(where, "needs a name")
            for field, inner in (("cooldown", "values"), ("cost", "values"), ("castTime", "value")):
                obj = ab.get(field)
                if isinstance(obj, dict) and obj.get(inner) is None \
                        and re.search(r"\d", str(obj.get("quote") or "")):
                    (self.warn if slot == "P" else self.err)(
                        f"{where}.{field}", f"{inner} is still null: fill it from the quote, "
                        "or set the whole field to null if the ability has none")
            self.timed(where, slot, "cooldown", ab.get("cooldown"), "cooldown")
            self.timed(where, slot, "cost", ab.get("cost"), "cost")
            self.timed(where, slot, "castTime", ab.get("castTime"), "castTime")
            for i, v in enumerate(ab.get("values") or []):
                vw = f"{where}.values[{i}]"
                if not isinstance(v, dict):
                    self.err(vw, "must be an object")
                    continue
                vid = v.get("id")
                vw = f"{where}.values[{vid or i}]"
                if not isinstance(vid, str) or vid in ids:
                    self.err(vw, "needs a unique string id")
                ids.add(vid)
                if v.get("role") not in VALUE_ROLES:
                    self.err(vw, f"role must be one of {sorted(VALUE_ROLES)}")
                if v.get("by") not in ("rank", "level", "const"):
                    self.err(vw, "by must be rank, level or const")
                if v.get("by") == "level" and isinstance(v.get("base"), list) \
                        and len(v["base"]) != LEVELS:
                    self.err(vw, f"by level needs exactly {LEVELS} numbers (levels 1-{LEVELS})")
                if v.get("damageType") not in (None, "physical", "magic", "true", "adaptive"):
                    self.err(vw, "damageType must be physical, magic, true, adaptive or null")
                if not isinstance(v.get("meaning"), str):
                    self.err(vw, "needs a meaning")
                quoted = self.quote(vw, v.get("quote"), slot)
                nums = self.numbers(vw, v)
                if not nums:
                    self.err(vw, "states no number (base or terms)")
                if quoted:
                    for label, s in nums:
                        self.in_quote(vw, label, s, v["quote"])
                refs = v.get("bin")
                if refs is None:
                    refs = []
                if not isinstance(refs, list):
                    self.err(vw, "bin must be a list of refs (empty when the bin has no such row)")
                    continue
                note = v.get("binNote")
                if not refs:
                    if not note:
                        self.err(vw, "no bin ref: name the bin rows, or say in binNote why none exist")
                    else:
                        self.warn(vw, f"wiki only (noted: {note})")
                    continue
                pool = self.bin_pool(vw, refs)
                for label, s in nums:
                    if not any(seq_match(s, p) for _, p in pool):
                        offered = "; ".join(f"{r}={list(p)}" for r, p in pool[:6])
                        (self.warn if note else self.err)(
                            vw, f"{label} {list(s)} matches none of its bin rows ({offered})"
                            + (f" (noted: {note})" if note else
                               " - fix it, or explain the disagreement in binNote"))
                # a named row has to earn its place: naming a row only to get
                # it past the coverage rule hides what the row really is
                for ref in refs:
                    if not isinstance(ref, str):
                        continue
                    mine = [p for r, p in pool if r == ref or r.startswith(ref + " ")
                            or ("#" in ref and r.split(".")[0] == ref.split("#")[0])]
                    if mine and not any(seq_match(s, p) for _, s in nums for p in mine):
                        (self.warn if note else self.err)(
                            vw, f"bin row {ref} carries none of this value's numbers"
                            + (f" (noted: {note})" if note else
                               " - state its number here, or account for it in binUnused"))
            for i, m in enumerate(ab.get("mechanics") or []):
                mw = f"{where}.mechanics[{i}]"
                if not isinstance(m, dict) or not isinstance(m.get("claim"), str):
                    self.err(mw, "needs a claim")
                    continue
                if not isinstance(m.get("affectsDamageFight"), bool):
                    self.err(mw, "affectsDamageFight must be true or false")
                self.quote(mw, m.get("quote"), slot)
        # every data value of the five root spells is used or explained
        unused = [(f"binUnused[{i}]", u) for i, u in enumerate(d.get("binUnused") or [])]
        for slot in SLOTS:
            ab = abilities.get(slot)
            if isinstance(ab, dict):
                unused += [(f"abilities.{slot}.binUnused[{i}]", u)
                           for i, u in enumerate(ab.get("binUnused") or [])]
        for uw, u in unused:
            if isinstance(u, dict) and isinstance(u.get("ref"), str):
                u = dict(u, ref=re.sub(r"\s*\(.*?\)\s*$", "", u["ref"]))  # "(percent)" as printed
                script, _, name = u["ref"].replace("#", ".", 1).partition(".")
                sp = self.spells.get(script, {})
                if name in sp.get("calcs", {}) and name not in sp.get("dataValues", {}):
                    continue  # calculations need no accounting; listing one is harmless
                if "#" in u["ref"]:
                    self.err(uw, f"{u['ref']!r} is not a calculation in the sheet")
                    continue
            if not isinstance(u, dict) or not isinstance(u.get("ref"), str) or "." not in u["ref"]:
                self.err(uw, "needs ref 'Spell.DataValue'")
                continue
            script, name = u["ref"].split(".", 1)
            if name not in self.spells.get(script, {}).get("dataValues", {}):
                self.err(uw, f"{u['ref']!r} is not a data value in the sheet")
                continue
            if u.get("reason") is None and (script, name) in self.used:
                continue  # a skeleton entry left behind: a value uses the row
            if u.get("reason") not in UNUSED_REASONS:
                self.err(uw, f"reason must be one of {sorted(UNUSED_REASONS)}")
            self.used.add((script, name))
        for slot in SLOTS:
            roots = self.src["sheet"]["slots"].get(slot) or []
            if not roots:
                continue
            root = roots[0]
            missing = [n for n in root.get("dataValues", {}) if (root["script"], n) not in self.used]
            if missing:
                self.err(f"abilities.{slot}", f"data values of {root['script']} neither used by a "
                         f"value nor listed in binUnused: {', '.join(missing)}")
        prims = d.get("primitives")
        if not isinstance(prims, list) or not prims:
            self.err("primitives", "must be a non-empty list")
        for i, p in enumerate(prims or []):
            if not isinstance(p, dict) or p.get("id") not in PRIMITIVES:
                self.err(f"primitives[{i}]", f"id must be one of {sorted(PRIMITIVES)}")
            elif not isinstance(p.get("abilities"), list) or not set(p["abilities"]) <= set(SLOTS):
                self.err(f"primitives[{i}]", "abilities must be a list of slots")
        nb = d.get("needsBespoke")
        if not isinstance(nb, dict) or not isinstance(nb.get("value"), bool) \
                or not isinstance(nb.get("why"), str):
            self.err("needsBespoke", "must be {value: bool, why: str}")
        for i, u in enumerate(d.get("unsettled") or []):
            uw = f"unsettled[{i}]"
            if not isinstance(u, dict) or not isinstance(u.get("question"), str):
                self.err(uw, "needs a question")
                continue
            if u.get("impact") not in ("high", "medium", "low"):
                self.err(uw, "impact must be high, medium or low")
            if not isinstance(u.get("options"), list) or len(u["options"]) < 2:
                self.err(uw, "needs at least two options")
            for j, q in enumerate(u.get("quotes") or []):
                if isinstance(q, str):
                    # lines of the numbers sheet are evidence too ("..." may join them)
                    pieces = [_words(x) for x in re.split(r"\.{3,}|\u2026", q)]
                    if any(pieces) and all(x in self.sheet_words for x in pieces if x):
                        continue
                self.quote(f"{uw}.quotes[{j}]", q)
        if not isinstance(d.get("unsettled"), list):
            self.err("unsettled", "must be a list (empty only if the sources settle everything)")
        rot = d.get("rotation")
        if not isinstance(rot, dict) or not isinstance(rot.get("proposal"), list) \
                or not isinstance(rot.get("questions"), list):
            self.err("rotation", "must be {proposal: [...], questions: [...]}")

    def report(self):
        n_values = sum(len((self.d.get("abilities", {}).get(s) or {}).get("values") or [])
                       for s in SLOTS) if isinstance(self.d, dict) else 0
        return {"slug": self.slug, "ok": not self.errors, "values": n_values,
                "errors": self.errors, "warnings": self.warnings}


def load_dossier(slug, path=None):
    path = path or os.path.join(DOSSIERS_DIR, f"{slug}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"no dossier at {os.path.relpath(path, BASE_DIR)}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"{os.path.relpath(path, BASE_DIR)} is not valid JSON: {e}")


def cmd_check(args):
    slug = args.slug.lower()
    if not args.dossier:
        assemble(slug)
    chk = Checker(slug, load_dossier(slug, args.dossier), load_sources(slug, args.patch))
    chk.run()
    rep = chk.report()
    if args.slot:
        # one ability at a time: only that slot's findings, so a dossier can
        # be written and checked in steps
        keep = f"abilities.{args.slot.upper()}"
        rep["errors"] = [e for e in rep["errors"] if e.startswith(keep)]
        rep["warnings"] = [w for w in rep["warnings"] if w.startswith(keep)]
        rep["ok"] = not rep["errors"]
    if args.json:
        print(json.dumps(rep, indent=1))
    else:
        for e in rep["errors"]:
            print(f"ERROR   {e}")
        for w in rep["warnings"]:
            print(f"warning {w}")
        print(f"{slug}: {rep['values']} values, {len(rep['errors'])} errors, "
              f"{len(rep['warnings'])} warnings - {'OK' if rep['ok'] else 'FAILED'}")
    sys.exit(0 if rep["ok"] else 1)


# ---------------------------------------------------------------------------
# score: a dossier against the hand-encoded kit (pilot evaluation)
# ---------------------------------------------------------------------------

def kit_numbers(node, path=""):
    """(path, sequence) for every numeric leaf or numeric list in a kit."""
    out = []
    if isinstance(node, dict):
        if set(node) >= {"from", "to", "levels"}:  # a ByLevel: its two ends
            return [(path, (float(node["from"]), float(node["to"])))]
        for k, v in node.items():
            if k in ("note", "name", "levels"):
                continue
            out += kit_numbers(v, f"{path}.{k}" if path else k)
    else:
        s = seq(node)
        if s is not None:
            out.append((path, s))
    return out


def dossier_numbers(ab):
    out = []
    for field in ("cooldown", "cost", "castTime"):
        obj = ab.get(field)
        if isinstance(obj, dict):
            s = seq(obj.get("values", obj.get("value")))
            if s is not None:
                out.append(s)
    for v in ab.get("values") or []:
        if not isinstance(v, dict):
            continue
        for key in ("base",):
            s = seq(v.get(key))
            if s is not None:
                out.append(s)
        for tm in v.get("terms") or []:
            if isinstance(tm, dict):
                s = seq(tm.get("coef"))
                if s is not None:
                    out.append(s)
                for sc in tm.get("coefScaling") or []:
                    s = seq(sc.get("coef")) if isinstance(sc, dict) else None
                    if s is not None:
                        out.append(s)
    return out


def cmd_score(args):
    slug = args.slug.lower()
    with open(os.path.join(BUILDS_DATA_DIR, f"{slug}.json")) as f:
        kit = json.load(f)
    d = load_dossier(slug, args.dossier)
    have = {s: dossier_numbers((d.get("abilities") or {}).get(s) or {}) for s in SLOTS}
    everything = [x for xs in have.values() for x in xs]
    wanted = [("P", p, s) for p, s in kit_numbers(kit.get("passive", {}), "passive")]
    for slot in "QWER":
        wanted += [(slot, p, s) for p, s in kit_numbers(kit.get("abilities", {}).get(slot, {}), slot)]

    def found(s, pool):
        if len(s) == 2 and any(len(p) == LEVELS and seq_match((p[0], p[-1]), s) for p in pool):
            return True  # a ByLevel's two ends against an 18-level list
        return any(seq_match(s, p) or (len(p) > len(s) and seq_match(s, p[:len(s)])) for p in pool)

    rows = {"list": [0, 0], "scalar": [0, 0]}
    misses = []
    for slot, path, s in wanted:
        kind = "list" if len(set(s)) > 1 else "scalar"
        hit = found(s, have[slot]) or found(s, everything)
        rows[kind][0] += hit
        rows[kind][1] += 1
        if not hit:
            misses.append(f"{path} = {list(s)}")
    rep = {"slug": slug, "lists": rows["list"], "scalars": rows["scalar"], "misses": misses}
    if args.json:
        print(json.dumps(rep, indent=1))
    else:
        print(f"{slug}: kit lists found {rows['list'][0]}/{rows['list'][1]}, "
              f"scalars {rows['scalar'][0]}/{rows['scalar'][1]}")
        for m in misses:
            print(f"  missing: {m}")


def cmd_primitives(args):
    for pid, what in PRIMITIVES.items():
        print(f"{pid}: {what}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("fetch")
    sp.add_argument("slugs", nargs="+")
    sp.add_argument("--patch")
    sp.add_argument("--offline", action="store_true",
                    help="rebuild sheet.json from the archived bin.json and wiki.json")
    sp.set_defaults(func=cmd_fetch)
    sp = sub.add_parser("init")
    sp.add_argument("slug")
    sp.add_argument("--patch")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)
    sp = sub.add_parser("primitives")
    sp.set_defaults(func=cmd_primitives)
    sp = sub.add_parser("wiki-all")
    sp.add_argument("--patch")
    sp.set_defaults(func=cmd_wiki_all)
    for name, func in (("sheet", cmd_sheet), ("check", cmd_check), ("score", cmd_score)):
        sp = sub.add_parser(name)
        sp.add_argument("slug")
        sp.add_argument("--patch")
        if name == "check":
            sp.add_argument("--slot", choices=[*SLOTS, *(x.lower() for x in SLOTS)],
                            help="report only this ability's findings")
        if name != "sheet":
            sp.add_argument("--dossier", help="path (default data/builds/dossiers/<slug>.json)")
            sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=func)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
