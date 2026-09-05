"""Teamfight Tactics build math: optimal item combinations per unit, from
first principles — no match data.

Numbers come from data, mechanics from code:

- data/tft/set<N>/<patch>/metatft.json — MetaTFT's public lookup file for
  the set: every unit's base stats, per-star curve tables and ability
  formulas (Riot's own calculation terms), every item's stat line and
  curve table, every trait's per-breakpoint values, and Riot's role tags.
  Community Dragon stopped carrying the ability numbers when Set 18 moved
  them into curve tables, so this is the one machine-readable source.
- data/tft/set<N>/<patch>/bins.json — per-unit timings distilled from
  Community Dragon's character bins: ability cast time, attack windup.
- data/tft/set<N>/<patch>/patchnotes.json — every "old ⇒ new" line of the
  patch's notes, so `lol.py tft check` can flag a snapshot value the notes
  say has changed (MetaTFT's file has lagged the live patch before).
- data/tft/set<N>/<patch>/overrides.json — hand corrections with
  provenance, applied on top of that patch's snapshot (typically: patch-
  note values the snapshot doesn't have yet). Per patch, so a new patch
  starts clean instead of inheriting pins the notes have since changed.
- data/tft/set<N>/item-effects.json, trait-effects.json — which item
  passives and trait bonuses the engine models, as references to the
  data's own rows (so a number change in the data flows through).
- tft_kits.py — one short driver per unit: the ability's shape (how many
  targets, over how long, what repeats), reading its numbers from the
  unit's calcs and curve rows.

The fight is a unit's mana cycle against three stat dummies derived from
the set's own units: attacks at the unit's attack speed grant role-based
mana, the ability casts when the bar fills, damage goes through the
armor/MR formula with sunder, shred, crit, Precision and post-mitigation
damage amp. "spread" puts the dummies out of each other's reach (area
abilities hit one), "clump" puts them together (area abilities hit all).

What a unit is scored on follows Riot's role label for it (the second word
of "Attack Caster", "Magic Tank", …):

- Marksman, Caster, Specialist — a carry: the dummies never hit back and
  the build is ranked by the time to kill all three, then by damage.
- Fighter, Assassin — a frontliner: the dummies hit back with the set's
  median attacks and abilities, the unit can die, and the build is ranked
  by kill time, then by damage dealt before dying.
- Tank — the dummies hit back with a whole board's worth of attackers
  (tanks are "more likely to be targeted") and cannot die (holding is the
  job); the build is ranked by how long the unit (and any on-death body
  it leaves) holds them, then by the damage its stuns deny, then damage.

Every 3-item combination is simulated and ranked that way.
"""

import glob
import hashlib
import itertools
import json
import multiprocessing as mp
import os
import re
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from html import unescape

from common import BASE_DIR, patch_key

TFT_DATA_DIR = os.path.join(BASE_DIR, "data", "tft")
CACHE_DIR = os.path.join(BASE_DIR, ".cache", "tft")
SCENARIO_CACHE_DIR = CACHE_DIR   # the name webapp's warmer expects
DEFAULT_SET = 18

METATFT_URL = "https://data.metatft.com/lookups/TFTSet{set}_latest_en_us.json"
CDRAGON_BIN = ("https://raw.communitydragon.org/latest/game/characters/"
               "{name}.cdtb.bin.json")
PATCH_NOTES_INDEX = "https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/"
PATCH_NOTES_URL = PATCH_NOTES_INDEX + "teamfight-tactics-patch-{slug}/"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) lol-analysis/0.1 (+github.com/rrogerc/lol-analysis)"

# --- the rules of the set that are not in the data ------------------------
AD_PER_STAR = 1.5      # attack damage multiplies by this per star above one
HP_PER_STAR = 1.8      # max health likewise
BASE_AP = 100.0        # every unit's ability power before items
AS_CAP = 5.0           # attacks per second, soft cap
CRIT_EXCESS_TO_DAMAGE = 1.0   # 1% crit chance over 100% -> 1% crit damage
PRECISION_EXTRA_CRIT_DAMAGE = 0.10  # a second source of Precision
MANA_LOCK_S = 1.0      # no mana for a second after a cast starts
CAST_TIME_DEFAULT = 0.25  # when the character bin has no cast time
TICK_S = 0.25          # granularity of regen, burns and timed stacks
# fighters gain attack speed by stage: "5-30% (based on Stage)"; the
# per-stage curve is patch 15.4's "Stage 2-6: 5/10/20/30/30%"
FIGHTER_AS_BY_STAGE = {1: 0.05, 2: 0.05, 3: 0.10, 4: 0.20, 5: 0.30, 6: 0.30}
STAGE = 4              # fighters' role bonus scales with the stage
# per-role mana per attack (Riot's role descriptions); casters also regenerate
ROLE_MANA = {"Assassin": 10, "Fighter": 10, "Marksman": 10, "Caster": 7,
             "Tank": 5}
CASTER_MANA_REGEN = 2.0
# tanks also "gain Mana from taking damage": the long-standing community
# formula, 1% of pre-mitigation plus 3% of post-mitigation damage, capped
# per hit (Riot publishes no numbers for it)
TANK_MANA_PER_PREMIT = 0.01
TANK_MANA_PER_POSTMIT = 0.03
TANK_MANA_PER_HIT_CAP = 42.5
# assassins "take 15% less damage from all enemies except the current target"
ASSASSIN_OFFTARGET_REDUCTION = 0.15
TANK_ROLE_TAG = "Role.Tank"
PRISMATIC_STYLE = 5    # trait breakpoint styles at or above this are chase tiers

FIGHT_DURATION = 20.0  # carries and frontliners
TANK_DURATION = 60.0   # tanks are scored on how long they last, so longer
N_DUMMIES = 3
DUMMY_STAR = 2
BOARD_SIZE = 8         # the enemy board at STAGE: what a tank ("more likely
                       # to be targeted") is hit by; a fighter fights the
                       # three dummies in front of it
CACHED_ROWS = 250

# What a unit is scored on, from the team-role half of Riot's label
# ("Attack Fighter" -> Fighter). recommendedItems can point a Specialist
# at another role's table; that wins (Master Yi and Gnar itemize as Fighters,
# Caitlyn as a Marksman).
OBJECTIVE_BY_KIND = {"Tank": "tank", "Fighter": "fighter", "Assassin": "fighter",
                     "Marksman": "carry", "Caster": "carry", "Specialist": "carry"}
OBJECTIVES = {
    "carry": "the dummies never hit back; ranked by time to kill all three, then damage dealt",
    "fighter": "the dummies hit back and the unit can die; ranked by kill time, then damage dealt before dying",
    "tank": "the dummies hit back with a whole board's worth of attackers and cannot die; ranked by how long the unit holds them (on-death bodies included), then by the damage its stuns deny, then damage dealt",
}
PRESSURED = ("fighter", "tank")   # objectives whose fights have the dummies attacking

# Scenario axes. A cell is one (star, geometry, trait context).
STARS = (2, 3)
GEOMETRIES = {
    "spread": "targets out of each other's reach: area abilities hit one",
    "clump": "targets together: area abilities hit every one still standing",
}
TRAIT_CONTEXTS = {
    "bare": "no traits active",
    "low": "the unit's own modeled traits at their first breakpoint",
    "high": "the unit's own modeled traits at their highest breakpoint",
}


def scenarios():
    """{key: scenario} in warm order: two stars, both geometries, three
    trait contexts — the same twelve cells for every unit."""
    out = {}
    for star in STARS:
        for geo in GEOMETRIES:
            for ctx in TRAIT_CONTEXTS:
                key = f"s{star}-{geo}-{ctx}"
                out[key] = {"key": key, "star": star, "geometry": geo,
                            "traits": ctx,
                            "label": f"{star}★ · {geo} · traits {ctx}"}
    return out


def fight_duration(unit):
    return TANK_DURATION if unit["objective"] == "tank" else FIGHT_DURATION


SCENARIOS = scenarios()


# ---------------------------------------------------------------------------
# snapshots: fetch and load
# ---------------------------------------------------------------------------

def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def fetch_json(url):
    return json.loads(fetch_bytes(url))


def set_dir(set_no):
    return os.path.join(TFT_DATA_DIR, f"set{set_no}")


def patch_dirs(set_no):
    """Archived patches of a set, oldest first."""
    d = set_dir(set_no)
    if not os.path.isdir(d):
        return []
    return sorted((p for p in os.listdir(d)
                   if re.fullmatch(r"\d+\.\d+", p)
                   and os.path.exists(os.path.join(d, p, "metatft.json"))),
                  key=patch_key)


def latest_patch_slug(set_no):
    """The newest `teamfight-tactics-patch-<set>-<n>` on the news index."""
    html = fetch_bytes(PATCH_NOTES_INDEX).decode("utf-8", "ignore")
    slugs = set(re.findall(rf"teamfight-tactics-patch-({set_no}-\d+)", html))
    if not slugs:
        return None
    return max(slugs, key=lambda s: int(s.split("-")[1]))


def parse_patch_notes(html):
    """Every 'Something: old ⇒ new' change line of a patch-notes page. The
    page renders each change as separate spans, so the arrow sits on a
    line of its own between the label+old and the new value."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    text = unescape(re.sub(r"<[^>]+>", "\n", text))
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out = []
    for i, line in enumerate(lines):
        if line != "⇒" or i == 0 or i + 1 >= len(lines):
            continue
        m = re.match(r"(.+?):\s*(.+)$", lines[i - 1])
        if not m:
            continue
        out.append({"what": m.group(1).strip(), "old": m.group(2).strip(),
                    "new": lines[i + 1]})
    return out


def distill_bin(b):
    """The few timings we need from a character bin."""
    out = {"castTime": None, "attackWindup": None, "missileSpeed": None}
    for key, v in b.items():
        if not isinstance(v, dict) or v.get("__type") != "SpellObject":
            continue
        sp = v.get("mSpell") or {}
        leaf = key.split("/")[-1]
        if leaf.endswith("Spell") or "Ability" in leaf:
            if sp.get("mCastTime") is not None:
                out["castTime"] = sp["mCastTime"]
        elif leaf.endswith("BasicAttack"):
            out["attackWindup"] = sp.get("spellCastTime")
            out["missileSpeed"] = sp.get("missileSpeed")
    return out


def cmd_fetch(args):
    set_no = args.set
    patch = args.patch
    if not patch:
        slug = latest_patch_slug(set_no)
        if not slug:
            sys.exit(f"No patch notes found for set {set_no} on the news "
                     "index — pass --patch explicitly (e.g. 18.1).")
        patch = slug.replace("-", ".")
    out_dir = os.path.join(set_dir(set_no), patch)
    if os.path.exists(os.path.join(out_dir, "metatft.json")) and not args.force:
        print(f"Set {set_no} patch {patch} is already archived at "
              f"{os.path.relpath(out_dir)} (use --force to refetch).")
        return
    os.makedirs(out_dir, exist_ok=True)
    print(f"Fetching MetaTFT lookup for set {set_no} …")
    raw = fetch_bytes(METATFT_URL.format(set=set_no))
    data = json.loads(raw)
    with open(os.path.join(out_dir, "metatft.json"), "w") as f:
        json.dump(data, f, separators=(",", ":"), sort_keys=True)
    units = real_units(data)
    print(f"  {len(units)} units, {len(data['items'])} items, "
          f"{len(data['traits'])} traits; MetaTFT stamp: "
          f"{data.get('_metadata', {}).get('patch')} generated "
          f"{data.get('_metadata', {}).get('generated')}")
    print("Fetching character bins from Community Dragon …")
    bins = {}
    for u in units:
        for asset in u.get("assetNames") or []:
            name = asset.lower()
            try:
                bins[asset] = distill_bin(fetch_json(CDRAGON_BIN.format(name=name)))
            except Exception as e:  # a missing bin is data, not a failure
                bins[asset] = {"error": str(e)[:80]}
    ok = sum(1 for b in bins.values() if b.get("castTime") is not None)
    print(f"  {len(bins)} bins, {ok} with a cast time")
    with open(os.path.join(out_dir, "bins.json"), "w") as f:
        json.dump(bins, f, indent=1, sort_keys=True)
    slug = patch.replace(".", "-")
    notes = []
    try:
        notes = parse_patch_notes(fetch_bytes(PATCH_NOTES_URL.format(slug=slug))
                                  .decode("utf-8", "ignore"))
        print(f"  patch notes {patch}: {len(notes)} change lines")
    except Exception as e:
        print(f"  patch notes {patch}: not fetched ({e})")
    with open(os.path.join(out_dir, "patchnotes.json"), "w") as f:
        json.dump({"patch": patch, "url": PATCH_NOTES_URL.format(slug=slug),
                   "changes": notes}, f, indent=1)
    meta = {"set": set_no, "patch": patch,
            "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metatft": data.get("_metadata"),
            "sources": {"metatft": METATFT_URL.format(set=set_no),
                        "bins": CDRAGON_BIN, "patchNotes": PATCH_NOTES_URL.format(slug=slug)}}
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    print(f"Archived under {os.path.relpath(out_dir)}/")


def real_units(data):
    """The set's shop champions. The file flags them (`shopUnit`); it also
    lists summons, transformed forms, props and monsters, some of which
    carry a cost and traits, so the flag is the only reliable test. An
    older file without the flag falls back to costed-with-traits."""
    units = data["units"]
    if any("shopUnit" in u for u in units):
        return [u for u in units if u.get("shopUnit")]
    return [u for u in units
            if u.get("traits") and u.get("cost") in (1, 2, 3, 4, 5)
            and "hp" in (u.get("stats") or {})]


def role_kind(tags):
    """The team-role half of a role: Assassin, Fighter, Marksman, Caster,
    Tank or Specialist."""
    for kind in ("Assassin", "Fighter", "Marksman", "Caster", "Tank", "Specialist"):
        if f"Role.{kind}" in tags:
            return kind
    return "Specialist"


def curve_at(curve, star):
    """A curve table row's value at a star level. Rows list breakpoints as
    [star, value]; a missing star holds the previous breakpoint's value
    (the resolved per-star coefficients in the data show it: a row
    [[1, .3], [3, .5]] resolves to .3, .3, .5). Below the first breakpoint
    the first value applies."""
    val = None
    for s, v in curve:
        if s <= star:
            val = v
        else:
            break
    if val is None:
        val = curve[0][1]
    return val


def override_curve(row, vals):
    """A curve row with hand values applied: one value makes the row that
    constant at every star; several replace the first stars in order and
    leave the higher ones as they were."""
    if len(vals) == 1:
        return [[1, vals[0]], [4, vals[0]]]
    have = [curve_at(row, s) for s in range(1, 5)] if row else [vals[0]] * 4
    for i, v in enumerate(vals[:4]):
        have[i] = v
    return [[s, v] for s, v in zip(range(1, 5), have)]


class Snapshot:
    """One archived patch of a set, with hand overrides applied."""

    def __init__(self, set_no, patch):
        self.set_no, self.patch = set_no, patch
        self.dir = os.path.join(set_dir(set_no), patch)
        with open(os.path.join(self.dir, "metatft.json")) as f:
            self.raw = json.load(f)
        with open(os.path.join(self.dir, "meta.json")) as f:
            self.meta = json.load(f)
        bins_path = os.path.join(self.dir, "bins.json")
        self.bins = {}
        if os.path.exists(bins_path):
            with open(bins_path) as f:
                self.bins = json.load(f)
        self.overrides = {"units": {}, "items": {}, "traits": {}}
        opath = os.path.join(self.dir, "overrides.json")
        if os.path.exists(opath):
            with open(opath) as f:
                o = json.load(f)
            for k in self.overrides:
                self.overrides[k] = o.get(k) or {}
        self.roles = self.raw.get("roleData") or {}
        self.role_names = self.raw.get("roles") or {}
        self.units = {}
        shop = real_units(self.raw)
        for u in shop:
            unit = self._unit(u)
            if unit["stats"]["hp"] is None:
                print(f"Warning: {u['apiName']} has no health in the data or in "
                      f"overrides.json (\"stats\": {{\"hp\": …}}); skipped", file=sys.stderr)
                continue
            self.units[u["apiName"]] = unit
        # the rest of the file: summons, transformed forms, monsters — kept
        # for drivers that need a summon's numbers
        shop_apis = {u["apiName"] for u in shop}
        self.extras = {u["apiName"]: self._unit(u) for u in self.raw["units"]
                       if u["apiName"] not in shop_apis}
        for api in self.overrides["units"]:
            if api not in self.units and api not in self.extras:
                print(f"Warning: overrides.json names unknown unit {api}", file=sys.stderr)
        self.units_by_name = {u["name"]: u for u in self.units.values()}
        self.items = {i["apiName"]: self._item(i) for i in self.raw["items"]}
        self.traits = {t["apiName"]: self._trait(t) for t in self.raw["traits"]}
        self.traits_by_name = {t["name"]: t for t in self.traits.values()}

    def _stats(self, s, curve, ov):
        def stat(key, default):
            v = s.get(key)
            return default if v is None else v
        ad = s.get("damage")
        if ad is None:  # a few units only carry their attack damage as a curve row
            for row in ("AutoAttackDamage", "BasicAttackDamage", "AutoAttackDamageSmall"):
                if row in curve:
                    ad = curve_at(curve[row], 1)
                    break
        stats = {"hp": s.get("hp"), "ad": 0.0 if ad is None else ad,
                 "as": stat("attackSpeed", 0.7),
                 "armor": stat("armor", 0.0), "mr": stat("magicResist", 0.0),
                 "mana": stat("mana", 0.0), "initialMana": stat("initialMana", 0.0),
                 "range": stat("range", 1), "critChance": stat("critChance", 0.25),
                 "critMult": stat("critMultiplier", 1.4)}
        stats.update(ov.get("stats") or {})
        return stats

    def _curve(self, u, ov):
        # curveValues carries rows the tooltip references that curveTable
        # lacks (Elise's spider rows, Kog'Maw's threshold); curveTable wins
        curve = dict(u.get("curveValues") or {})
        curve.update(u.get("curveTable") or {})
        for row, vals in (ov.get("curve") or {}).items():
            curve[row] = override_curve(curve.get(row), vals)
        return curve

    def _unit(self, u):
        s = u.get("stats") or {}
        ov = self.overrides["units"].get(u["apiName"]) or {}
        curve = self._curve(u, ov)
        calcs = dict((u.get("ability") or {}).get("attributeCalcs") or {})
        stats = self._stats(s, curve, ov)
        assets = u.get("assetNames") or []
        b = next((self.bins[a] for a in assets if a in self.bins
                  and self.bins[a].get("castTime") is not None), {})
        # Riot's role: the unit's own tag list is inconsistent (Akali lacks
        # Role.Attack, Rengar's tags say Assassin while his role says
        # Fighter), so the role's own definition is the source of truth
        role = u.get("role")
        rd = self.roles.get(role) or {}
        tags = list(rd.get("roleTags") or u.get("roleTags") or [])
        kind = role_kind(tags)
        rec = u.get("recommendedItems") or ""
        rec_kind = None
        m = re.match(r"DA_RecommendedItemsTable_(?:Attack|Magic|Hybrid)(\w+)$", rec)
        if m and m.group(1) in OBJECTIVE_BY_KIND:
            rec_kind = m.group(1)
        # Adaptor forms: the AD and AP kits (Lux's extra abilities are her
        # Avatar trait variants, not forms)
        forms = {}
        for key, v in (u.get("extraAbilities") or {}).items():
            if v.get("variant") not in ("AD", "AP"):
                continue
            fov = (ov.get("forms") or {}).get(v["variant"]) or {}
            fcurve = self._curve(v, fov)
            forms[v["variant"]] = {
                "name": v.get("name"), "desc": v.get("desc"),
                "calcs": dict(v.get("attributeCalcs") or {}), "curve": fcurve,
                "stats": self._stats(v["stats"], fcurve, fov) if v.get("stats") else None,
            }
        return {
            "api": u["apiName"], "name": u["name"], "cost": u["cost"],
            "role": role, "roleName": rd.get("name") or self.role_names.get(role) or role,
            "roleTags": tags, "kind": kind, "attack": "Role.Attack" in tags,
            "resource": (rd.get("resourceType") or "Mana").split(".")[-1],
            "itemKind": rec_kind or kind,
            "objective": OBJECTIVE_BY_KIND[rec_kind or kind],
            "traits": list(u.get("traits") or []),
            "traitApis": list(u.get("traitApiNames") or []),
            "stats": stats, "curve": curve, "calcs": calcs, "forms": forms,
            "ability": {"name": (u.get("ability") or {}).get("name"),
                        "desc": (u.get("ability") or {}).get("desc")},
            "castTime": ov.get("castTime", b.get("castTime")),
            "assets": assets,
        }

    def _item(self, i):
        curve = dict(i.get("curveTable") or {})
        ov = self.overrides["items"].get(i["apiName"]) or {}
        for row, v in (ov.get("curve") or {}).items():
            curve[row] = [[1, v]]
        return {"api": i["apiName"], "name": i["name"],
                "composition": i.get("composition") or [],
                "unique": bool(i.get("unique")), "curve": curve,
                "statLine": i.get("statLine") or "", "desc": i.get("desc") or "",
                "tags": i.get("tags") or []}

    def _trait(self, t):
        curve = dict(t.get("curveTable") or {})
        ov = self.overrides["traits"].get(t["apiName"]) or {}
        for row, vals in (ov.get("curve") or {}).items():
            curve[row] = [[i, v] for i, v in enumerate(vals)]
        levels = [e["minUnits"] for e in t.get("effects") or []]
        styles = [e.get("style", 0) for e in t.get("effects") or []]
        return {"api": t["apiName"], "name": t["name"], "type": t.get("type"),
                "desc": t.get("desc") or "", "curve": curve, "levels": levels,
                "styles": styles,
                "units": [x["unit"] for x in t.get("units") or []]}

    def unit(self, key):
        """A unit by api name or display name (case-insensitive)."""
        if key in self.units:
            return self.units[key]
        low = key.lower()
        for u in self.units.values():
            if u["name"].lower() == low or u["api"].lower() == low \
                    or u["api"].lower().split("_", 1)[-1] == low:
                return u
        raise KeyError(f"no unit {key!r} in set {self.set_no} patch {self.patch}")

    def item(self, key):
        if key in self.items:
            return self.items[key]
        low = key.lower().replace("'", "").replace(" ", "")
        for it in self.items.values():
            if it["name"].lower().replace("'", "").replace(" ", "") == low \
                    or it["api"].lower() == low:
                return it
        raise KeyError(f"no item {key!r}")

    def hash_inputs(self):
        """Bytes of everything a result depends on in this snapshot."""
        h = hashlib.sha256()
        for fn in ("metatft.json", "bins.json", "overrides.json"):
            p = os.path.join(self.dir, fn)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    h.update(f.read())
        return h.hexdigest()


_SNAP = {}


def load_snapshot(set_no=None, patch=None):
    set_no = set_no or DEFAULT_SET
    patches = patch_dirs(set_no)
    if not patches:
        sys.exit(f"No TFT snapshot for set {set_no} — run `lol.py tft fetch` first.")
    if patch is None:
        patch = patches[-1]
    elif patch not in patches:
        sys.exit(f"No snapshot for set {set_no} patch {patch}; have {', '.join(patches)}")
    key = (set_no, patch)
    if key not in _SNAP:
        _SNAP[key] = Snapshot(set_no, patch)
    return _SNAP[key]


# ---------------------------------------------------------------------------
# hand-curated effect tables (numbers stay in the data, referenced by row)
# ---------------------------------------------------------------------------

def _hand_file(set_no, name):
    path = os.path.join(set_dir(set_no), name)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def load_item_effects(set_no):
    return _hand_file(set_no, "item-effects.json")


def load_trait_effects(set_no):
    return _hand_file(set_no, "trait-effects.json")


def load_kits(set_no):
    return _hand_file(set_no, "kits.json")


def row_value(curve, spec, star=1):
    """Resolve a hand-file reference: a number, or {"row": name, "scale":
    k, "minusOne": bool, "mult": m, "col": breakpoint index}."""
    if isinstance(spec, (int, float)):
        return float(spec)
    row = curve.get(spec["row"])
    if row is None:
        raise KeyError(f"row {spec['row']!r} missing from curve table")
    v = curve_at(row, spec.get("col", star))
    if spec.get("minusOne"):
        v -= 1.0
    v *= spec.get("scale", 1.0)
    v *= spec.get("mult", 1.0)
    return v


# stat line icons -> engine stat keys, with the value convention per format
_ICON_STAT = {"ad": "adPct", "ap": "ap", "as": "asPct", "critchance": "crit",
              "damageamp": "amp", "health": "hp", "armor": "armor",
              "mr": "mr", "manaregen": "manaRegen", "omnivamp": "omnivamp",
              "dura": "durability"}


def parse_stat_line(item):
    """An item's stat line, as {stat: value} in engine units: adPct and
    crit and amp as fractions, ap as flat ability power, asPct as a
    fraction of base attack speed, hp/armor/mr/manaRegen flat. The data
    writes each row in whichever convention its tooltip formatter expects,
    so the format attribute decides how to read it."""
    stats = {}
    for m in re.finditer(r"<TFT(?:CurveTable|Attribute)\s+([^>]*)/>", item["statLine"]):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        if attrs.get("type") != "stat":
            continue
        row = attrs.get("row") or attrs.get("fallbackRow")
        icon = (attrs.get("icon") or "").split(".")[-1].lower()
        key = _ICON_STAT.get(icon)
        if not key or row not in item["curve"]:
            continue
        v = curve_at(item["curve"][row], 1)
        fmt = attrs.get("format", "")
        if key == "adPct":
            v = v if fmt == "percent" else v / 100.0
        elif key == "ap":
            v = v * 100.0 if fmt == "percent" else v
        elif key == "asPct":
            v = v - 1.0 if fmt == "percentMinusOne" else (v if fmt == "percent" else v / 100.0)
        elif key in ("crit", "amp", "omnivamp"):
            v = v if fmt == "percent" else v / 100.0
        elif key == "durability":
            # "invertedPercent" rows hold the damage multiplier (0.92 = 8%)
            v = 1.0 - v if fmt == "invertedPercent" else (v if fmt == "percent" else v / 100.0)
        stats[key] = stats.get(key, 0.0) + v
    return stats


# ---------------------------------------------------------------------------
# the fight: sheet + effects + engine
# ---------------------------------------------------------------------------

def unit_role_kind(unit):
    return unit.get("kind") or role_kind(unit.get("roleTags") or [])


def adaptor_form(unit, fx):
    """Which form an Adaptor (or any unit with an AD and an AP kit) fights
    in: "Adaptor abilities change depending on if their Attack Damage or
    Ability Power is higher" — read as the bonus attack damage (as a
    fraction) against the bonus ability power (per 100), the role's own
    damage type breaking the tie."""
    if not unit.get("forms"):
        return None
    ad, ap = fx.adPct, fx.ap / 100.0
    if ad > ap + 1e-9:
        return "AD"
    if ap > ad + 1e-9:
        return "AP"
    return "AD" if unit["attack"] else "AP"


class Fx:
    """Everything items, traits and the role add to a unit for one fight:
    flat stats, and the dynamic effects the engine knows how to apply."""

    def __init__(self):
        self.adPct = 0.0; self.ap = 0.0; self.asPct = 0.0; self.crit = 0.0
        self.critDmg = 0.0; self.amp = 0.0; self.hp = 0.0; self.hpMult = 1.0
        self.armor = 0.0; self.mr = 0.0; self.manaRegen = 0.0
        self.manaPerAttack = 0.0; self.manaPerCrit = 0.0; self.manaMult = 1.0
        self.adapMult = 1.0; self.startingMana = 0.0; self.precision = 0
        self.ampVsTank = 0.0
        self.asPerSecond = []      # [(pct per second, until_s or None)]
        self.adPerAttack = []      # [(pct, max stacks, as bonus at max)]
        self.adapPerAttack = []    # [(pct, max stacks, amp at max)]
        self.apPerInterval = []    # [(ap, interval s)]
        self.apAfter = []          # [(ap, at s)]
        self.ampPerCrit = []       # [(amp, duration, max stacks)]
        self.asPerAttackStack = [] # [(pct, max stacks)]  (Rapidfire)
        self.apPerCast = 0.0
        self.sunderOnHit = []      # [(pct, duration)]
        self.shredOnHit = []
        self.burnOnHit = []        # [(pct max hp per s, duration, stacks?)]
        self.sunderAura = 0.0; self.shredAura = 0.0
        self.burnAura = None       # (pct, duration)
        self.ampAfterSameTarget = None  # (amp, seconds)
        self.bleedPct = 0.0; self.bleedDur = 0.0   # Executioner
        self.bonusMagicPct = 0.0   # Solar
        self.ravager = None        # (amp, hp threshold, multiplier below)
        self.riftbeast = False
        self.form = None           # Adaptor form chosen for this build
        # --- taking damage (fighters and tanks: the dummies hit back) ---
        self.omnivamp = 0.0
        self.durabilities = []         # fractions of post-mitigation damage prevented; they stack multiplicatively
        self.durabilityAbove = None    # (extra fraction, health fraction) Steadfast Heart
        self.attackDamageTaken = 1.0   # multiplier on damage from attacks (Bramble Vest)
        self.thorns = None             # (magic damage, cooldown s) to adjacent attackers
        self.resistsPerAttacker = [0.0, 0.0]  # armor, mr per enemy attacking (Gargoyle, Monolith)
        self.healPerInterval = []      # [(fraction of max hp, every s)] Dragon's Claw
        self.regenMissingPct = 0.0     # fraction of missing health per second
        self.shieldAtHp = []           # [(health fraction, shield as fraction of max hp, duration, decays)]
        self.shieldAtStart = []        # [(fraction of max hp, duration)]
        self.resistsAtStart = None     # (armor, mr, duration) Evenshroud
        self.untargetableAtHp = None   # (health fraction, duration, missing-health heal) Edge of Night
        self.manaAtHp = []             # [(health fraction, mana)] Protector's Vow
        self.adapPerHit = False        # Titan's stacks from being hit too
        self.ionicSpark = 0.0          # magic damage per mana an attacker spends casting
        self.allyHealPct = 0.0         # Gunblade: share of damage dealt healed on an ally
        self.hoj = None                # (ad, ap, omnivamp, health fraction) Hand of Justice
        self.healOnTakedown = 0.0      # fraction of max hp (Flora Fatalis)
        self.manaOnTakedown = 0.0
        self.faeHeal = None            # (health fraction, heal)  once, below the threshold
        self.summoner = {}             # Summoner trait rows for the unit's summons
        self.caustic = None            # (fraction, duration): shred and sunder on damage
        self.notes = []            # what was left unmodeled
        self.applied = []          # what was modeled, for the UI

    def add_stats(self, stats, mult=1.0):
        for k, v in stats.items():
            if k == "hpMult":
                self.hpMult *= v
            elif k == "durability":
                self.durabilities.append(v * mult)
            elif hasattr(self, k):
                setattr(self, k, getattr(self, k) + v * mult)

    @property
    def durability(self):
        return combined_durability(self.durabilities)


def combined_durability(fractions):
    """Durability sources stack multiplicatively (the wiki: "unique scaling
    that decreases as it approaches 100%"), so 20% and 15% make 32%."""
    out = 1.0
    for d in fractions:
        out *= 1.0 - max(0.0, d)
    return 1.0 - out


def apply_item(fx, item, spec, unit):
    """One item's stats (from its stat line) and modeled passive (from
    item-effects.json) onto fx."""
    fx.add_stats(parse_stat_line(item))
    c = item["curve"]

    def rv(s):
        if isinstance(s, list):
            return sum(rv(x) for x in s)
        return row_value(c, s)
    if spec.get("precision"):
        fx.precision += int(spec["precision"])
    if "ampVsTank" in spec:
        fx.ampVsTank += rv(spec["ampVsTank"])
    if "asPerSecond" in spec:
        until = rv(spec["asStackDuration"]) if "asStackDuration" in spec else None
        fx.asPerSecond.append((rv(spec["asPerSecond"]), until))
    if "adPerAttack" in spec:
        fx.adPerAttack.append((rv(spec["adPerAttack"]), rv(spec["maxStacks"]),
                               rv(spec["asAtMax"]) if "asAtMax" in spec else 0.0))
    if "adapPerAttack" in spec:
        fx.adapPerAttack.append((rv(spec["adapPerAttack"]), rv(spec["maxStacks"]),
                                 rv(spec["ampAtMax"]) if "ampAtMax" in spec else 0.0))
    if "apPerInterval" in spec:
        fx.apPerInterval.append((rv(spec["apPerInterval"]), rv(spec["interval"])))
    if "apAfter" in spec:
        fx.apAfter.append((rv(spec["apAfter"]), rv(spec["at"])))
    if "ampPerCrit" in spec:
        fx.ampPerCrit.append((rv(spec["ampPerCrit"]), rv(spec["ampDuration"]),
                              rv(spec["maxStacks"])))
    if "manaPerAttack" in spec:
        fx.manaPerAttack += rv(spec["manaPerAttack"])
    if "manaPerCrit" in spec:
        fx.manaPerCrit += rv(spec["manaPerCrit"])
    if "manaMult" in spec:
        fx.manaMult *= rv(spec["manaMult"])
    if "adapMult" in spec:
        fx.adapMult *= rv(spec["adapMult"])
    if "startingMana" in spec:
        fx.startingMana += rv(spec["startingMana"])
    for k in ("adPct", "ap", "asPct", "crit", "amp", "armor", "mr", "manaRegen"):
        if k in spec:
            setattr(fx, k, getattr(fx, k) + rv(spec[k]))
    if "byRole" in spec:
        kind = unit_role_kind(unit)
        branch = spec["byRole"]["tankOrFighter" if kind in ("Tank", "Fighter") else "other"]
        for k, s in branch.items():
            setattr(fx, k, getattr(fx, k) + rv(s))
    if "sunderOnHit" in spec:
        fx.sunderOnHit.append((rv(spec["sunderOnHit"]["pct"]), rv(spec["sunderOnHit"]["duration"])))
    if "shredOnHit" in spec:
        fx.shredOnHit.append((rv(spec["shredOnHit"]["pct"]), rv(spec["shredOnHit"]["duration"])))
    if "burnOnHit" in spec:
        fx.burnOnHit.append((rv(spec["burnOnHit"]["pct"]), rv(spec["burnOnHit"]["duration"]), False))
    if "sunderAura" in spec and unit["stats"]["range"] <= rv(spec["sunderAura"]["hexes"]):
        fx.sunderAura = max(fx.sunderAura, rv(spec["sunderAura"]["pct"]))
    if "shredAura" in spec and unit["stats"]["range"] <= rv(spec["shredAura"]["hexes"]):
        fx.shredAura = max(fx.shredAura, rv(spec["shredAura"]["pct"]))
    if "burnAura" in spec:
        fx.burnAura = (rv(spec["burnAura"]["pct"]), rv(spec["burnAura"]["duration"]))
    # --- defensive passives ---
    if "hpMult" in spec:
        fx.hpMult *= rv(spec["hpMult"])
    if "durability" in spec:
        fx.durabilities.append(rv(spec["durability"]))
    if "durabilityAbove" in spec:
        fx.durabilityAbove = (rv(spec["durabilityAbove"]["extra"]), rv(spec["durabilityAbove"]["threshold"]))
    if "attackDamageTaken" in spec:
        fx.attackDamageTaken *= rv(spec["attackDamageTaken"])
    if "thorns" in spec:
        fx.thorns = (rv(spec["thorns"]["damage"]), rv(spec["thorns"]["cooldown"]))
    if "resistsPerAttacker" in spec:
        fx.resistsPerAttacker[0] += rv(spec["resistsPerAttacker"]["armor"])
        fx.resistsPerAttacker[1] += rv(spec["resistsPerAttacker"]["mr"])
    if "healPerInterval" in spec:
        fx.healPerInterval.append((rv(spec["healPerInterval"]["pct"]), rv(spec["healPerInterval"]["interval"])))
    if "regenMissingPct" in spec:
        fx.regenMissingPct += rv(spec["regenMissingPct"])
    if "shieldAtHp" in spec:
        sh = spec["shieldAtHp"]
        fx.shieldAtHp.append((rv(sh["threshold"]), rv(sh["pct"]),
                              rv(sh["duration"]) if "duration" in sh else 1e9, bool(sh.get("decays"))))
    if "shieldAtStart" in spec:
        fx.shieldAtStart.append((rv(spec["shieldAtStart"]["pct"]), rv(spec["shieldAtStart"]["duration"])))
    if "resistsAtStart" in spec:
        rs = spec["resistsAtStart"]
        fx.resistsAtStart = (rv(rs["armor"]), rv(rs["mr"]), rv(rs["duration"]))
    if "untargetableAtHp" in spec:
        un = spec["untargetableAtHp"]
        fx.untargetableAtHp = (rv(un["threshold"]), rv(un["duration"]), rv(un["healMissing"]))
    if "manaAtHp" in spec:
        fx.manaAtHp.append((rv(spec["manaAtHp"]["threshold"]), rv(spec["manaAtHp"]["mana"])))
    if spec.get("adapPerHit"):
        fx.adapPerHit = True
    if "ionicSpark" in spec and unit["stats"]["range"] <= rv(spec["ionicSpark"]["hexes"]):
        fx.ionicSpark += rv(spec["ionicSpark"]["pct"])
    if "allyHealPct" in spec:
        fx.allyHealPct += rv(spec["allyHealPct"])
    if "hoj" in spec:
        h = spec["hoj"]
        fx.hoj = (rv(h["adPct"]), rv(h["ap"]), rv(h["omnivamp"]), rv(h["threshold"]))
    if spec.get("note"):
        fx.notes.append(f"{item['name']}: {spec['note']}")


def trait_level_index(trait, count):
    """Breakpoint column for a unit count: 1 for the first breakpoint met,
    2 for the second, …; 0 when none is met."""
    idx = 0
    for i, lvl in enumerate(trait["levels"], 1):
        if count >= lvl:
            idx = i
    return idx


def apply_trait(fx, trait, spec, col, unit):
    """One trait at breakpoint column `col` onto fx, per trait-effects.json."""
    c = trait["curve"]

    def rv(s):
        if isinstance(s, list):
            return sum(rv(x) for x in s)
        if isinstance(s, dict):
            return row_value(c, dict(s, col=s.get("col", col)))
        return float(s)
    own_mult = rv(spec["ownMultiplier"]) if "ownMultiplier" in spec else 1.0
    for k, s in (spec.get("stats") or {}).items():
        v = rv(s) * own_mult
        if k == "adap":
            fx.adPct += v; fx.ap += v * 100.0
        elif k == "adOrAp":   # Adaptor: whichever side the build leans to
            form = fx.form or ("AD" if unit["attack"] else "AP")
            if form == "AD":
                fx.adPct += v
            else:
                fx.ap += v * 100.0
        elif k == "hpMult":
            fx.hpMult *= v
        else:
            setattr(fx, k, getattr(fx, k) + v)
    if spec.get("precision"):
        fx.precision += 1
    if "asPerAttackStack" in spec:
        fx.asPerAttackStack.append((rv(spec["asPerAttackStack"]), rv(spec["maxStacks"])))
    if "apPerCast" in spec:
        fx.apPerCast += rv(spec["apPerCast"]) * 100.0
    if "ampAfterSameTarget" in spec:
        fx.ampAfterSameTarget = (rv(spec["ampAfterSameTarget"]["amp"]),
                                 rv(spec["ampAfterSameTarget"]["seconds"]))
    if "bleed" in spec:
        fx.bleedPct = max(fx.bleedPct, rv(spec["bleed"]["pct"]))
        fx.bleedDur = rv(spec["bleed"]["duration"])
    if "burnOnHit" in spec:
        fx.burnOnHit.append((rv(spec["burnOnHit"]["pct"]), rv(spec["burnOnHit"]["duration"]), True))
    if "bonusMagicPct" in spec:
        fx.bonusMagicPct += rv(spec["bonusMagicPct"])
    if "ravager" in spec:
        fx.ravager = (rv(spec["ravager"]["amp"]), rv(spec["ravager"]["threshold"]),
                      rv(spec["ravager"]["multiplier"]))
    if "pixies" in spec:  # Fae: per-pixie AD/AP at an assumed pixie count
        n = spec["pixies"]["count"][col - 1] if col - 1 < len(spec["pixies"]["count"]) else spec["pixies"]["count"][-1]
        v = rv(spec["pixies"]["adapPerPixie"]) * n
        fx.adPct += v; fx.ap += v * 100.0
    if spec.get("riftbeast"):
        fx.riftbeast = True
    # --- defensive and support trait bonuses ---
    if "durability" in spec:
        fx.durabilities.append(rv(spec["durability"]))
    if "shieldAtStart" in spec:
        fx.shieldAtStart.append((rv(spec["shieldAtStart"]["pct"]), rv(spec["shieldAtStart"]["duration"])))
    if "shieldAtHp" in spec:
        sh = spec["shieldAtHp"]
        fx.shieldAtHp.append((rv(sh["threshold"]), rv(sh["pct"]), rv(sh["duration"]), False))
    if "resistsPerAttacker" in spec:
        fx.resistsPerAttacker[0] += rv(spec["resistsPerAttacker"]["armor"])
        fx.resistsPerAttacker[1] += rv(spec["resistsPerAttacker"]["mr"])
    if "omnivamp" in spec:
        fx.omnivamp += rv(spec["omnivamp"])
    if "takedown" in spec:
        fx.healOnTakedown += rv(spec["takedown"].get("healPct", 0.0))
        fx.manaOnTakedown += rv(spec["takedown"].get("mana", 0.0))
    if "faeHeal" in spec:
        n = spec["faeHeal"]["count"][col - 1] if col - 1 < len(spec["faeHeal"]["count"]) else spec["faeHeal"]["count"][-1]
        fx.faeHeal = (rv(spec["faeHeal"]["threshold"]), rv(spec["faeHeal"]["healPerPixie"]) * n)
    if "summoner" in spec:
        fx.summoner = {k: rv(s) for k, s in spec["summoner"].items()}
    if "caustic" in spec:
        fx.caustic = (rv(spec["caustic"]["pct"]), rv(spec["caustic"]["duration"]))
    if spec.get("note"):
        fx.notes.append(f"{trait['name']}: {spec['note']}")


def unit_trait_contexts(snap, unit, trait_fx):
    """{context: [(trait api, breakpoint column)]} for the unit's own
    traits that have a modeled effect. Unmodeled traits are reported."""
    modeled, unmodeled = [], []
    for api in unit["traitApis"]:
        t = snap.traits.get(api)
        if t is None:
            continue
        if api in trait_fx and t["levels"]:
            modeled.append(api)
        else:
            unmodeled.append(t["name"])
    out = {"bare": [], "low": [], "high": []}
    for api in modeled:
        t = snap.traits[api]
        # "high" stops short of the prismatic chase tiers (style 5+), which
        # need emblems or a whole board of the trait
        reachable = [i for i, st in enumerate(t["styles"], 1) if st < PRISMATIC_STYLE]
        out["low"].append((api, 1))
        out["high"].append((api, max(reachable) if reachable else len(t["levels"])))
    return out, unmodeled


def build_fx(snap, unit, item_apis, ctx_traits, item_fx, trait_fx):
    """The fx of one build: role, items, traits."""
    fx = Fx()
    kind = unit_role_kind(unit)
    if kind == "Caster":
        fx.manaRegen += CASTER_MANA_REGEN
    if kind == "Fighter":
        fx.asPct += FIGHTER_AS_BY_STAGE[STAGE]
    for api in item_apis:
        item = snap.items[api]
        spec = (item_fx.get("items") or {}).get(api) or {}
        apply_item(fx, item, spec, unit)
    # the items decide an Adaptor's form; its trait bonus then follows it
    fx.form = adaptor_form(unit, fx)
    for api, col in ctx_traits:
        apply_trait(fx, snap.traits[api], trait_fx[api], col, unit)
    return fx


class Sheet:
    """A unit's numbers for one fight, before dynamic stacks. An Adaptor
    fights in the form its items lean to: that form's stats, calcs and
    curve rows replace the base ones."""

    def __init__(self, unit, star, fx):
        self.unit, self.star, self.fx = unit, star, fx
        self.form = fx.form if unit.get("forms") else None
        self.calcs, self.curve = unit["calcs"], unit["curve"]
        s = unit["stats"]
        if self.form is not None and self.form in unit["forms"]:
            form = unit["forms"][self.form]
            self.calcs = {**unit["calcs"], **form["calcs"]}
            self.curve = {**unit["curve"], **form["curve"]}
            if form["stats"]:
                s = {**s, **{k: v for k, v in form["stats"].items() if v is not None}}
        self.stats = s
        self.kit = {"name": unit["name"], "calcs": self.calcs, "curve": self.curve}
        self.kind = unit_role_kind(unit)
        self.objective = unit.get("objective", "carry")
        base_ad = s["ad"]
        # an Adaptor's forms have their own attack damage rows (Community
        # Dragon lists Gromp's magic form at the AutoAttackDamageAP value)
        row = {"AD": "AutoAttackDamage", "AP": "AutoAttackDamageAP"}.get(self.form)
        if row and row in self.curve:
            base_ad = curve_at(self.curve[row], 1)
        self.base_ad = base_ad * AD_PER_STAR ** (star - 1)
        self.ad_pct = fx.adPct
        self.ap_flat = BASE_AP + fx.ap
        self.adap_mult = fx.adapMult
        self.base_as = s["as"]
        self.as_pct = fx.asPct
        self.max_hp = (s["hp"] * HP_PER_STAR ** (star - 1) + fx.hp) * fx.hpMult
        self.armor = s["armor"] + fx.armor
        self.mr = s["mr"] + fx.mr
        crit = s["critChance"] + fx.crit
        excess = max(0.0, crit - 1.0)
        self.crit_chance = min(crit, 1.0)
        self.crit_mult = s["critMult"] + fx.critDmg + excess * CRIT_EXCESS_TO_DAMAGE \
            + max(0, fx.precision - 1) * PRECISION_EXTRA_CRIT_DAMAGE
        self.precision = fx.precision > 0
        self.mana_max = s["mana"]
        self.mana_start = s["initialMana"] + fx.startingMana
        kind = self.kind
        self.mana_per_attack = ROLE_MANA.get(kind, 0)
        self.role_kind = kind
        self.range = s["range"]
        self.omnivamp = fx.omnivamp
        self.durability = fx.durability

    def ad(self, ad_pct_extra=0.0):
        return self.base_ad * (1.0 + self.ad_pct + ad_pct_extra) * self.adap_mult

    def ap(self, ap_extra=0.0):
        return (self.ap_flat + ap_extra) * self.adap_mult

    def attack_speed(self, as_extra=0.0):
        return min(self.base_as * (1.0 + self.as_pct + as_extra), AS_CAP)

    @property
    def crit_ev(self):
        return 1.0 + self.crit_chance * (self.crit_mult - 1.0)


class Dummy:
    """A stat dummy: a health pool with resists — and, when the fight has
    the dummies hitting back, its group's median attacks and ability."""
    __slots__ = ("hp", "max_hp", "armor", "mr", "sunder", "sunder_until",
                 "shred", "shred_until", "armor_flat", "mr_flat", "burn_pct",
                 "burn_until", "burn_stack", "dots", "alive", "marks",
                 "died_at", "is_tank", "immortal", "ad", "as_", "crit_ev", "next_attacks",
                 "ability", "phys_share", "mana", "mana_max", "mana_per_attack",
                 "mana_from_damage", "lock_until", "stunned_until", "attacks", "casts")

    def __init__(self, hp, armor, mr, is_tank=True):
        self.hp = self.max_hp = hp
        self.armor, self.mr = armor, mr
        self.is_tank = is_tank
        self.sunder = 0.0; self.sunder_until = 0.0
        self.shred = 0.0; self.shred_until = 0.0
        self.armor_flat = 0.0; self.mr_flat = 0.0
        self.burn_pct = 0.0; self.burn_until = 0.0; self.burn_stack = 0.0
        self.dots = []      # [dps, until, dtype, src]
        self.alive = True
        self.marks = {}     # driver scratch (Soraka stars, poison stacks…)
        self.died_at = None
        self.immortal = False   # a tank fight's dummies take damage but never fall
        # offense (only armed in pressured fights)
        self.ad = 0.0; self.as_ = 0.0; self.crit_ev = 1.0
        self.next_attacks = []  # one attack timer per unit this dummy stands for
        self.ability = 0.0; self.phys_share = 1.0
        self.mana = 0.0; self.mana_max = 0.0; self.mana_per_attack = 0.0
        self.mana_from_damage = False; self.lock_until = 0.0
        self.stunned_until = 0.0
        self.attacks = 0; self.casts = 0

    def arm(self, spec, crit_ev, streams=1):
        """Give the dummy its group's offense (see dummies_for): attacks at
        its attack speed from one period in, and the same mana rules as a
        unit — mana per attack, a full bar casts the ability (split
        physical/magic by the group's damage types), a second's lock; a
        tank dummy also gains mana from the damage it takes. `streams` is
        how many units it stands for: that many attack timers, staggered."""
        self.ad = float(spec.get("ad") or 0.0)
        self.as_ = float(spec.get("as") or 0.0)
        self.crit_ev = crit_ev
        if self.as_ > 0 and self.ad > 0:
            period = 1.0 / self.as_
            self.next_attacks = [period * (i + 1) / streams for i in range(int(streams))]
        else:
            self.next_attacks = []
        self.ability = float(spec.get("ability") or 0.0)
        self.phys_share = float(spec.get("physicalShare", 1.0))
        self.mana_max = float(spec.get("manaMax") or 0.0)
        self.mana = float(spec.get("manaStart") or 0.0)
        self.mana_per_attack = float(spec.get("manaPerAttack") or 0.0)
        self.mana_from_damage = bool(spec.get("manaFromDamage"))

    def gain_mana(self, amount, t):
        if t >= self.lock_until and self.mana_max > 0:
            self.mana += amount

    def streams(self):
        return len(self.next_attacks)

    def next_event(self):
        return min(self.next_attacks) if self.next_attacks else 1e18


def resist_mult(r):
    return 100.0 / (100.0 + r) if r >= 0 else 2.0 - 100.0 / (100.0 - r)


class Fight:
    """One build's fight against the dummies. Drivers (tft_kits) plug in
    through `driver` hooks and the helper methods below. With `pressure`
    the dummies hit back: the unit has a health pool, shields, healing,
    durability, and can die (an on-death body can hold the dummies on)."""

    def __init__(self, sheet, dummies, geometry, driver, duration=FIGHT_DURATION,
                 aura_sunder=0.0, aura_shred=0.0, pressure=False):
        self.sheet = sheet
        self.fx = sheet.fx
        self.targets = dummies
        self.clump = geometry == "clump"
        self.driver = driver
        self.duration = duration
        self.pressure = pressure
        self.t = 0.0
        self.mana = sheet.mana_start
        self.lock_until = 0.0
        self.casting_until = 0.0
        self.next_attack = 0.0
        self.attacks = 0
        self.casts = 0
        self.cast_times = []
        self.total = 0.0
        self.raw_total = 0.0            # before the cap at each dummy's health
        self.breakdown = {}
        self.kill_time = None
        self.cur = 0                    # index of the current target
        self.target_since = 0.0
        # dynamic stacks
        self.as_stack = 0.0             # from per-second stacking items
        self.as_attack_stack = 0.0      # Rapidfire
        self.ad_stack = 0.0             # Kraken's
        self.ad_stack_n = 0             # attacks so far, for Kraken's cap
        self.adap_stack_n = 0.0         # Titan's stacks
        self.ap_stack = 0.0             # Archangel's, Crownguard, Spellweaver
        self.amp_stacks = []            # [(amp, until)]
        self.amp_extra = 0.0            # driver-set amp (e.g. Titan's full)
        self.as_extra_until = 0.0; self.as_extra = 0.0   # driver buffs
        self.ad_extra = 0.0             # driver-set bonus attack damage (fraction)
        self.ap_extra = 0.0             # driver-set bonus ability power
        self.range_extra = 0
        self.state = {}                 # driver scratch
        self.aura_sunder = aura_sunder
        self.aura_shred = aura_shred
        # --- the unit's own body ---
        self.max_hp_extra = 0.0
        self.hp = sheet.max_hp
        self.alive_unit = True
        self.died_at = None
        self.hold_until = None          # when the last body fell (None while holding)
        self.body = None                # [hp, armor, mr, name]: an on-death body holding the dummies
        self.bodies = []                # queued on-death bodies
        self.shields = []               # [amount, until, decay per second]
        self.absorbed = 0.0             # pre-mitigation damage taken (bodies included)
        self.taken = 0.0                # after resists, durability and shields
        self.mitigated = 0.0
        self.healed = 0.0
        self.shield_used = 0.0
        self.denied = 0.0               # damage the dummies could not land (stunned, untargetable)
        self.ally_heal = 0.0
        self.ally_shield = 0.0
        self.cc_time = 0.0              # stun seconds × dummies
        self.hits_taken = 0
        self.untargetable_until = 0.0
        self.resist_buffs = []          # [(armor, mr, until)]
        self.dur_buffs = []             # [(fraction, until)]
        self.armor_extra = 0.0; self.mr_extra = 0.0   # permanent, driver-set
        self.fired = set()              # one-shot health triggers already used
        self.thorns_ready = 0.0
        for d in self.targets:
            if self.fx.sunderAura:
                d.sunder = max(d.sunder, self.fx.sunderAura); d.sunder_until = 1e9
            if self.fx.shredAura:
                d.shred = max(d.shred, self.fx.shredAura); d.shred_until = 1e9
        for pct, dur in self.fx.shieldAtStart:
            self.shield(pct * self.max_hp(), dur, "combat start")
        if self.fx.resistsAtStart:
            a, m, dur = self.fx.resistsAtStart
            self.resist_buffs.append((a, m, dur))

    # ---- targets ---------------------------------------------------------
    def alive(self):
        return [d for d in self.targets if d.alive]

    def target(self):
        for d in self.targets:
            if d.alive:
                return d
        return None

    def aoe(self, count=None, exclude_primary=False):
        """Targets an area ability hits: in the clump, up to `count` of the
        standing dummies (all of them if None); spread out, only the one
        being attacked."""
        al = self.alive()
        if not al:
            return []
        if not self.clump:
            return [] if exclude_primary else al[:1]
        if exclude_primary:
            al = al[1:]
        return al if count is None else al[:int(count)]

    def adjacent(self, attacker=None):
        """Dummies adjacent to the unit: everyone standing in the clump,
        only the current target (or the given attacker) spread out."""
        if self.clump:
            return self.alive()
        d = attacker if attacker is not None and attacker.alive else self.target()
        return [d] if d is not None else []

    # ---- stats now --------------------------------------------------------
    def _hoj(self):
        """Hand of Justice: its attack damage and ability power double above
        the health threshold, its omnivamp below."""
        h = self.fx.hoj
        if h is None:
            return 0.0, 0.0, 0.0
        above = self.hp_frac() >= h[3]
        return (h[0] * (2 if above else 1), h[1] * (2 if above else 1),
                h[2] * (1 if above else 2))

    def ad(self):
        return self.sheet.ad(self.ad_stack + self.ad_extra + self.adap_stack_n * self._adap_per()
                             + self._hoj()[0])

    def ap(self):
        return self.sheet.ap(self.ap_stack + self.ap_extra + self.adap_stack_n * self._adap_per() * 100.0
                             + self._hoj()[1])

    def _adap_per(self):
        return sum(p for p, _, _ in self.fx.adapPerAttack)

    def omnivamp(self):
        return self.sheet.omnivamp + self._hoj()[2]

    def attack_speed(self):
        extra = self.as_stack + self.as_attack_stack
        if self.t < self.as_extra_until:
            extra += self.as_extra
        for pct, mx, as_at_max in self.fx.adPerAttack:
            if self.ad_stack_n >= mx:
                extra += as_at_max
        return self.sheet.attack_speed(extra)

    def amp(self, target):
        a = self.fx.amp + self.amp_extra
        if target.is_tank:
            a += self.fx.ampVsTank
        for amp, until in self.amp_stacks:
            if self.t < until:
                a += amp
        if self.fx.ampAfterSameTarget and target is self.targets[self.cur] \
                and self.t - self.target_since >= self.fx.ampAfterSameTarget[1]:
            a += self.fx.ampAfterSameTarget[0]
        for pct, mx, amp_at_max in self.fx.adapPerAttack:
            if self.adap_stack_n >= mx:
                a += amp_at_max
        if self.fx.ravager:
            amp, thr, mult = self.fx.ravager
            a += amp * (mult if target.hp < thr * target.max_hp else 1.0)
        return 1.0 + a

    # ---- the unit's body ----------------------------------------------------
    def max_hp(self):
        return self.sheet.max_hp + self.max_hp_extra

    def hp_frac(self):
        m = self.max_hp()
        return self.hp / m if m > 0 else 0.0

    def attackers(self):
        """How many units are attacking the unit (Gargoyle, Monolith)."""
        if not self.pressure:
            return 0
        return sum(d.streams() for d in self.targets if d.alive)

    def armor_now(self):
        a = self.sheet.armor + self.armor_extra
        for ar, _, until in self.resist_buffs:
            if self.t < until:
                a += ar
        return a + self.fx.resistsPerAttacker[0] * self.attackers()

    def mr_now(self):
        m = self.sheet.mr + self.mr_extra
        for _, mr, until in self.resist_buffs:
            if self.t < until:
                m += mr
        return m + self.fx.resistsPerAttacker[1] * self.attackers()

    def durability_now(self):
        ds = list(self.fx.durabilities)
        if self.fx.durabilityAbove and self.hp_frac() >= self.fx.durabilityAbove[1]:
            ds.append(self.fx.durabilityAbove[0])
        for pct, until in self.dur_buffs:
            if self.t < until:
                ds.append(pct)
        return min(combined_durability(ds), 0.99)

    def holding(self):
        """Is anything of ours still keeping the dummies busy?"""
        return self.alive_unit or self.body is not None

    def take(self, amount, dtype, attacker=None, attack=False):
        """Incoming damage of type physical/magic/true on the unit (or on
        the body holding after its death): resists, then Bramble's attack
        reduction, then durability, then shields, then health. Returns the
        post-mitigation damage."""
        if amount <= 0 or not self.holding():
            return 0.0
        pre = amount
        if not self.alive_unit:
            b = self.body
            r = b[1] if dtype == "physical" else b[2] if dtype == "magic" else None
            if r is not None:
                amount *= resist_mult(max(r, 0.0))
            self.absorbed += pre; self.taken += amount; self.mitigated += pre - amount
            b[0] -= amount
            if b[0] <= 0:
                self._next_body()
            return amount
        s = self.sheet
        if s.kind == "Assassin" and attacker is not None and attacker is not self.targets[self.cur]:
            amount *= 1.0 - ASSASSIN_OFFTARGET_REDUCTION
        if dtype == "physical":
            amount *= resist_mult(max(self.armor_now(), 0.0))
        elif dtype == "magic":
            amount *= resist_mult(max(self.mr_now(), 0.0))
        if attack:
            amount *= self.fx.attackDamageTaken
        amount *= 1.0 - self.durability_now()
        post = amount
        left = post
        for sh in self.shields:
            if left <= 0:
                break
            if sh[1] <= self.t or sh[0] <= 0:
                continue
            use = min(sh[0], left)
            sh[0] -= use; left -= use; self.shield_used += use
        self.hp -= left
        self.absorbed += pre; self.taken += post; self.mitigated += pre - post
        self.hits_taken += 1
        if s.kind == "Tank" and s.mana_max > 0:
            self.gain_mana(min(TANK_MANA_PER_HIT_CAP,
                               pre * TANK_MANA_PER_PREMIT + post * TANK_MANA_PER_POSTMIT))
        if self.fx.adapPerHit:
            for _, mx, _ in self.fx.adapPerAttack:
                if self.adap_stack_n < mx:
                    self.adap_stack_n += 1
        if attack and self.fx.thorns and self.t >= self.thorns_ready:
            dmg, cd = self.fx.thorns
            self.thorns_ready = self.t + cd
            for d in self.adjacent(attacker):
                self.deal(dmg, "magic", d, "thorns", ability=False, crit=False)
        self.driver.hit(self, attacker, post)
        self._health_triggers()
        if self.hp <= 0 and self.alive_unit:
            self._die()
        return post

    def _health_triggers(self):
        fx = self.fx
        frac = self.hp_frac()
        for i, (thr, pct, dur, decays) in enumerate(fx.shieldAtHp):
            if frac < thr and ("shield", i) not in self.fired:
                self.fired.add(("shield", i))
                self.shield(pct * self.max_hp(), dur, "low health", decays)
        for i, (thr, mana) in enumerate(fx.manaAtHp):
            if frac < thr and ("mana", i) not in self.fired:
                self.fired.add(("mana", i))
                self.mana += mana
        if fx.untargetableAtHp and frac < fx.untargetableAtHp[0] and "untargetable" not in self.fired:
            self.fired.add("untargetable")
            self.untargetable(fx.untargetableAtHp[1])
            self.heal((self.max_hp() - self.hp) * fx.untargetableAtHp[2], "edge of night")
        if fx.faeHeal and frac < fx.faeHeal[0] and "fae" not in self.fired:
            self.fired.add("fae")
            self.heal(fx.faeHeal[1] * self.max_hp(), "pixies")

    def _die(self):
        self.alive_unit = False
        self.died_at = self.t
        self.hp = 0.0
        self.shields = []
        self.driver.died(self)
        self._next_body()

    def _next_body(self):
        if self.bodies:
            self.body = self.bodies.pop(0)
        else:
            self.body = None
            self.hold_until = self.t

    def add_body(self, hp, armor, mr, name="body"):
        """An on-death body that taunts and keeps the dummies on it."""
        self.bodies.append([float(hp), float(armor), float(mr), name])

    def heal(self, amount, src="heal"):
        """Heal the unit; returns the effective amount."""
        if amount <= 0 or not self.alive_unit:
            return 0.0
        eff = min(amount, self.max_hp() - self.hp)
        if eff > 0:
            self.hp += eff
            self.healed += eff
        return eff

    def shield(self, amount, duration, src="shield", decays=False):
        if amount <= 0 or not self.alive_unit or duration <= 0:
            return
        self.shields.append([amount, self.t + duration, amount / duration if decays else 0.0])

    def heal_ally(self, amount):
        """Healing given to allies — counted, not simulated."""
        if amount > 0:
            self.ally_heal += amount

    def shield_ally(self, amount):
        if amount > 0:
            self.ally_shield += amount

    def stun(self, targets, duration):
        """Stun (sleep, knock up) dummies: their attacks and casts inside
        the window are denied."""
        for d in targets:
            if d.alive and duration > 0:
                d.stunned_until = max(d.stunned_until, self.t + duration)
                self.cc_time += duration

    def buff_resists(self, armor, mr, duration):
        self.resist_buffs.append((armor, mr, self.t + duration))

    def buff_durability(self, pct, duration):
        self.dur_buffs.append((pct, self.t + duration))

    def untargetable(self, duration):
        self.untargetable_until = max(self.untargetable_until, self.t + duration)

    def gain_max_hp(self, amount):
        """Bonus max health, which also fills by that much."""
        if amount > 0:
            self.max_hp_extra += amount
            self.hp += amount

    # ---- damage ------------------------------------------------------------
    def calc(self, name, runtime=None):
        """An ability calculation's value now: Riot's terms folded over the
        unit's current stats. `runtime` supplies values for runtime terms
        (e.g. an attack count) and the Stack scaling."""
        return calc_value(self.sheet.kit, name, self.sheet.star, self.ad(),
                          self.ap(), self.max_hp(), self.armor_now(),
                          self.mr_now(), runtime or {}, self.sheet.base_ad)

    def row(self, name):
        return curve_at(self.sheet.curve[name], self.sheet.star)

    def deal(self, amount, dtype, target, src, ability=True, crit=True, raw=False):
        """Deal `amount` pre-mitigation damage of type physical/magic/true
        to `target`; returns the post-mitigation damage. Crit is an
        expected value; abilities crit only with Precision."""
        if amount <= 0 or target is None or not target.alive:
            return 0.0
        s = self.sheet
        if crit and (not ability or s.precision):
            amount *= s.crit_ev
        if dtype == "physical":
            r = target.armor * (1.0 - target.sunder if self.t < target.sunder_until else 1.0) - target.armor_flat
            amount *= resist_mult(max(r, 0.0))
        elif dtype == "magic":
            r = target.mr * (1.0 - target.shred if self.t < target.shred_until else 1.0) - target.mr_flat
            amount *= resist_mult(max(r, 0.0))
        pre = amount
        if dtype != "true" and not raw:
            amount *= self.amp(target)
        self._apply(amount, target, src, pre)
        if not raw and dtype != "true":
            if self.fx.bonusMagicPct:
                self.deal(amount * self.fx.bonusMagicPct, "magic", target, "solar",
                          ability=False, crit=False, raw=True)
            if self.fx.bleedPct and target.alive:
                self.dot(amount * self.fx.bleedPct, self.fx.bleedDur, "true", target, "bleed")
        return amount

    def _apply(self, amount, target, src, pre=None):
        self.raw_total += amount
        if target.immortal:   # counted in full, never lands on the health bar
            self.total += amount
            self.breakdown[src] = self.breakdown.get(src, 0.0) + amount
            if target.mana_from_damage:
                target.gain_mana(min(TANK_MANA_PER_HIT_CAP,
                                     (pre if pre is not None else amount) * TANK_MANA_PER_PREMIT
                                     + amount * TANK_MANA_PER_POSTMIT), self.t)
            return
        if amount > target.hp:
            amount = target.hp
        target.hp -= amount
        self.total += amount
        self.breakdown[src] = self.breakdown.get(src, 0.0) + amount
        if target.mana_from_damage and target.alive:
            target.gain_mana(min(TANK_MANA_PER_HIT_CAP,
                                 (pre if pre is not None else amount) * TANK_MANA_PER_PREMIT
                                 + amount * TANK_MANA_PER_POSTMIT), self.t)
        if self.pressure and self.alive_unit:
            omni = self.omnivamp()
            if omni:
                self.heal(amount * omni, "omnivamp")
        if self.fx.allyHealPct:
            self.ally_heal += amount * self.fx.allyHealPct
        if target.hp <= 0 and target.alive:
            target.alive = False
            target.died_at = self.t
            if not any(d.alive for d in self.targets):
                self.kill_time = self.t
            elif target is self.targets[self.cur]:
                self.cur = self.targets.index(self.target())
                self.target_since = self.t
            if self.fx.healOnTakedown:
                self.heal(self.fx.healOnTakedown * self.max_hp(), "takedown")
            if self.fx.manaOnTakedown:
                self.gain_mana(self.fx.manaOnTakedown)
            self.driver.kill(self, target)

    def on_hit_effects(self, target, ability):
        """Sunder, shred and burns that attacks and ability damage apply."""
        if target is None or not target.alive:
            return
        for pct, dur in self.fx.sunderOnHit:
            self.sunder(target, pct, dur)
        for pct, dur in self.fx.shredOnHit:
            self.shred(target, pct, dur)
        if self.fx.caustic:
            self.sunder(target, self.fx.caustic[0], self.fx.caustic[1])
            self.shred(target, self.fx.caustic[0], self.fx.caustic[1])
        for pct, dur, stacks in self.fx.burnOnHit:
            self.burn(target, pct, dur, stacks)

    def sunder(self, target, pct, dur):
        if pct >= target.sunder or self.t >= target.sunder_until:
            target.sunder = max(pct, target.sunder if self.t < target.sunder_until else 0.0)
        target.sunder_until = max(target.sunder_until, self.t + dur)

    def shred(self, target, pct, dur):
        if pct >= target.shred or self.t >= target.shred_until:
            target.shred = max(pct, target.shred if self.t < target.shred_until else 0.0)
        target.shred_until = max(target.shred_until, self.t + dur)

    def burn(self, target, pct, dur, stacks=False):
        if stacks:
            target.burn_stack = max(target.burn_stack, pct)
            target.marks["burn_stack_until"] = self.t + dur
        else:
            if pct >= target.burn_pct or self.t >= target.burn_until:
                target.burn_pct = pct
                target.burn_until = self.t + dur

    def dot(self, total, duration, dtype, target, src):
        """Damage over time: `total` pre-mitigation over `duration` s."""
        if target is None or not target.alive:
            return
        if duration <= 0:
            self.deal(total, dtype, target, src, crit=False)
            return
        target.dots.append([total / duration, self.t + duration, dtype, src])

    def hit_attack(self, target, mult=1.0, src="auto"):
        """One basic attack's damage on `target`."""
        dmg = self.deal(self.ad() * mult, "physical", target, src, ability=False)
        self.on_hit_effects(target, False)
        return dmg

    def hit_ability(self, calc, target, src="ability", mult=1.0, dtype=None, runtime=None):
        """Ability damage from calc `calc` on `target` (type from the calc's
        name unless given)."""
        dtype = dtype or calc_type(calc)
        dmg = self.deal(self.calc(calc, runtime) * mult, dtype, target, src)
        self.on_hit_effects(target, True)
        return dmg

    def dot_ability(self, calc, target, duration, src="ability", mult=1.0, dtype=None, runtime=None):
        dtype = dtype or calc_type(calc)
        self.on_hit_effects(target, True)
        self.dot(self.calc(calc, runtime) * mult, duration, dtype, target, src)

    def buff_as(self, pct, duration):
        self.as_extra = pct
        self.as_extra_until = self.t + duration

    # ---- the loop ---------------------------------------------------------
    def run(self):
        drv = self.driver
        drv.init(self)
        next_tick = TICK_S
        next_second = 1.0
        interval_next = [(interval, ap) for ap, interval in self.fx.apPerInterval]
        heal_next = [(interval, pct) for pct, interval in self.fx.healPerInterval]
        ap_after = sorted(self.fx.apAfter, key=lambda x: x[1])
        self.next_attack = 0.0
        while self.t < self.duration and self.kill_time is None and self.holding():
            t_attack = max(self.next_attack, self.casting_until) if self.alive_unit else 1e18
            t_in = self._next_incoming() if self.pressure else 1e18
            t_next = min(t_attack, next_tick, t_in)
            self.t = t_next
            if t_next == next_tick:
                self._tick(next_tick, next_second, interval_next, ap_after, heal_next)
                if next_tick >= next_second:
                    next_second += 1.0
                next_tick += TICK_S
            if self.pressure and t_in <= self.t:
                self._incoming()
            if self.kill_time is not None or self.t >= self.duration or not self.alive_unit:
                continue
            if t_attack <= self.t:
                self._attack()
        return self.result()

    def _next_incoming(self):
        t = 1e18
        for d in self.targets:
            if d.alive:
                e = d.next_event()
                if e < t:
                    t = e
        return t

    def _incoming(self):
        """Every dummy attack that is due (and the cast it fills the bar
        for). A stunned dummy (or one facing an untargetable unit) loses
        the swing: that damage counts as denied."""
        for d in self.targets:
            for i in range(len(d.next_attacks)):
                while d.alive and self.holding() and d.next_attacks[i] <= self.t + 1e-9:
                    d.next_attacks[i] += 1.0 / d.as_
                    d.attacks += 1
                    amount = d.ad * d.crit_ev
                    if self.t < d.stunned_until or self.t < self.untargetable_until:
                        self.denied += amount
                    else:
                        self.take(amount, "physical", d, attack=True)
                        d.gain_mana(d.mana_per_attack, self.t)
                        self._dummy_cast(d)

    def _dummy_cast(self, d):
        """A dummy with a full bar casts: its ability's damage, split by
        its group's damage types, then the mana lock."""
        if not (d.alive and self.holding() and d.mana_max > 0 and d.mana >= d.mana_max
                and self.t >= d.lock_until):
            return
        d.mana = min(d.mana - d.mana_max, d.mana_max)
        d.lock_until = self.t + MANA_LOCK_S
        d.casts += 1
        if self.t < d.stunned_until or self.t < self.untargetable_until:
            self.denied += d.ability
            return
        if self.fx.ionicSpark:
            self.deal(self.fx.ionicSpark * d.mana_max, "magic", d, "ionic spark",
                      ability=False, crit=False)
        if d.phys_share > 0:
            self.take(d.ability * d.phys_share, "physical", d)
        if d.phys_share < 1 and self.holding():
            self.take(d.ability * (1.0 - d.phys_share), "magic", d)

    def _tick(self, now, next_second, interval_next, ap_after, heal_next):
        fx = self.fx
        # mana regen (blocked in the lock)
        if fx.manaRegen and now >= self.lock_until and self.alive_unit:
            self.gain_mana(fx.manaRegen * TICK_S)
        # burns: % max hp per second as true damage, applied per tick
        for d in self.targets:
            if not d.alive:
                continue
            pct = d.burn_pct if now <= d.burn_until else 0.0
            if d.burn_stack and now <= d.marks.get("burn_stack_until", 0.0):
                pct += d.burn_stack
            if pct:
                self.deal(pct * d.max_hp * TICK_S, "true", d, "burn", ability=False, crit=False)
            if d.dots:
                keep = []
                for dot in d.dots:
                    dps, until, dtype, src = dot
                    span = min(TICK_S, max(0.0, until - (now - TICK_S)))
                    if span > 0:
                        self.deal(dps * span, dtype, d, src, ability=False, crit=False, raw=(src == "bleed"))
                    if until > now:
                        keep.append(dot)
                d.dots = keep
            if not d.alive:
                continue
        if fx.burnAura and self.alive_unit:
            tgt = self.target()
            if tgt is not None:
                self.burn(tgt, fx.burnAura[0], fx.burnAura[1])
        # per-second stacking attack speed (Guinsoo, Quicksilver)
        if now >= next_second - 1e-9:
            for pct, until in fx.asPerSecond:
                if until is None or now <= until:
                    self.as_stack += pct
        for i, (interval, ap) in enumerate(interval_next):
            if now >= interval - 1e-9:
                self.ap_stack += ap
                interval_next[i] = (interval + fx.apPerInterval[i][1], ap)
        while ap_after and now >= ap_after[0][1] - 1e-9:
            self.ap_stack += ap_after.pop(0)[0]
        self.amp_stacks = [x for x in self.amp_stacks if x[1] > now]
        # the body: shields decay and expire, timed healing
        if self.alive_unit:
            if self.shields:
                for sh in self.shields:
                    if sh[2]:
                        sh[0] -= sh[2] * TICK_S
                self.shields = [sh for sh in self.shields if sh[0] > 0 and sh[1] > now]
            for i, (at, pct) in enumerate(heal_next):
                if now >= at - 1e-9:
                    self.heal(pct * self.max_hp(), "dragon's claw")
                    heal_next[i] = (at + fx.healPerInterval[i][1], pct)
            if fx.regenMissingPct and self.hp < self.max_hp():
                self.heal(fx.regenMissingPct * (self.max_hp() - self.hp) * TICK_S, "regeneration")
        if self.pressure:
            for d in self.targets:
                if d.alive and d.mana_max > 0 and d.mana >= d.mana_max:
                    self._dummy_cast(d)
        self.driver.tick(self)
        if self.alive_unit and self.mana >= self.sheet.mana_max and self.sheet.mana_max > 0 \
                and self.t >= self.casting_until and self.kill_time is None:
            self._cast()

    def gain_mana(self, amount):
        if self.t < self.lock_until or self.sheet.mana_max <= 0:
            return
        self.mana += amount * self.fx.manaMult

    def _attack(self):
        s, fx = self.sheet, self.fx
        tgt = self.target()
        if tgt is None:
            return
        self.attacks += 1
        self.ad_stack_n += 1
        for pct, mx, _ in fx.adPerAttack:
            if self.ad_stack_n <= mx:
                self.ad_stack += pct
        for pct, mx, _ in fx.adapPerAttack:
            if self.adap_stack_n < mx:
                self.adap_stack_n += 1
        for pct, mx in fx.asPerAttackStack:
            n = self.state.get("rapidfire", 0)
            if n < mx:
                self.state["rapidfire"] = n + 1
                self.as_attack_stack += pct
        for amp, dur, mx in fx.ampPerCrit:
            # expected-value crit: each attack adds crit-chance worth of a stack
            live = sum(a for a, u in self.amp_stacks if u > self.t)
            add = min(amp * s.crit_chance, max(0.0, amp * mx - live))
            if add > 0:
                self.amp_stacks.append((add, self.t + dur))
        # mana on attack (role + items), before the driver so an attack
        # that fills the bar casts right after
        self.gain_mana(s.mana_per_attack + fx.manaPerAttack
                       + fx.manaPerCrit * s.crit_chance)
        self.driver.attack(self, tgt)
        period = 1.0 / self.attack_speed()
        self.next_attack = self.t + period
        if self.mana >= s.mana_max and s.mana_max > 0 and self.alive_unit:
            self._cast()

    def _cast(self):
        s = self.sheet
        if self.target() is None or not self.alive_unit:
            return
        self.casts += 1
        self.cast_times.append(self.t)
        overflow = max(0.0, self.mana - s.mana_max)
        self.mana = min(overflow, s.mana_max)  # overflow carries up to one cast
        cast_time = self.driver.cast_time(self)
        self.lock_until = self.t + max(MANA_LOCK_S, cast_time)
        self.casting_until = self.t + cast_time
        if self.fx.apPerCast:
            self.ap_stack += self.fx.apPerCast
        self.driver.cast(self)

    def result(self):
        dps = self.total / max(self.t, 1e-9) if self.kill_time is None else self.total / max(self.kill_time, 1e-9)
        # how long the unit (or its on-death body) kept the dummies busy: the
        # whole fight unless everything of ours fell first
        alive_time = self.hold_until if self.hold_until is not None else self.duration
        return {"killTime": self.kill_time, "total": self.total, "dps": dps,
                "rawTotal": self.raw_total,
                "attacks": self.attacks, "casts": self.casts,
                "castTimes": list(self.cast_times),
                "breakdown": dict(self.breakdown),
                "left": [max(0.0, d.hp) for d in self.targets],
                "t": self.t,
                "aliveTime": alive_time, "died": not self.alive_unit,
                "diedAt": self.died_at, "hpLeft": max(0.0, self.hp),
                "absorbed": self.absorbed, "taken": self.taken, "mitigated": self.mitigated,
                "healed": self.healed, "shielded": self.shield_used, "denied": self.denied,
                "allyHeal": self.ally_heal, "allyShield": self.ally_shield,
                "ccTime": self.cc_time, "hitsTaken": self.hits_taken}


def calc_type(name):
    n = name.split(".")[-1]
    if n.startswith("Physical"):
        return "physical"
    if n.startswith("Magic"):
        return "magic"
    if n.startswith("True"):
        return "true"
    return "magic"


_SCALE_KEYS = {"AttackDamage": "ad_pct", "AbilityPower": "ap",
               "HealthMax": "hp", "Armor": "armor", "MagicResist": "mr",
               "BasicAttackDamage": "ad"}


def calc_value(unit, name, star, ad, ap, max_hp, armor, mr, runtime, base_ad=None):
    """Fold one of the unit's ability calculations at the given stats.
    Term conventions (from the display values the data resolves them to):
    an AttackDamage coefficient is the damage at the unit's base attack
    damage for that star and scales with the attack damage it has (the
    rows grow ×1.5 per star exactly like base AD, so Warwick's 200/300/450
    bite is 500% AD throughout, and Scuttlecrab's row is literally her
    AutoAttackDamage); AbilityPower ones are a flat amount per 100 AP;
    HealthMax/Armor/MagicResist/BasicAttackDamage and calc-to-calc
    references are fractions."""
    full = name if name.startswith("TFTCalculationAttributes.") else "TFTCalculationAttributes." + name
    calc = unit["calcs"].get(full)
    if calc is None:
        raise KeyError(f"{unit['name']} has no calc {name}")
    if base_ad is None:
        base_ad = unit["stats"]["ad"] * AD_PER_STAR ** (star - 1)
    acc = 0.0
    for term in calc["terms"]:
        ttype = term.get("type")
        if ttype == "runtime":
            v = float(runtime.get(term.get("row") or "runtime", runtime.get("runtime", 0.0)))
        elif ttype == "flat":
            row = term.get("row")
            v = curve_at(unit["curve"][row], star) if row in unit["curve"] else float((term.get("coefficient") or [0])[min(star, 4) - 1] or 0)
        else:
            coefs = term.get("coefficient")
            if coefs is None and term.get("row") in unit["curve"]:
                coef = curve_at(unit["curve"][term["row"]], star)
            elif coefs is None:
                coef = 0.0
            else:
                coef = coefs[min(star, len(coefs)) - 1]
                if coef is None:
                    coef = 0.0
            scaling = term.get("scaling")
            if scaling == "AttackDamage":
                v = coef * ad / base_ad if base_ad else coef
            elif scaling == "AbilityPower":
                v = coef * ap / 100.0
            elif scaling == "HealthMax":
                v = coef * max_hp
            elif scaling == "Armor":
                v = coef * armor
            elif scaling == "MagicResist":
                v = coef * mr
            elif scaling == "BasicAttackDamage":
                v = coef * ad
            elif scaling == "Stack":
                v = coef * float(runtime.get("Stack", 0.0))
            elif scaling and scaling.endswith(("Calc1", "Calc2", "Calc3", "Calc4")):
                v = coef * calc_value(unit, scaling, star, ad, ap, max_hp, armor, mr, runtime, base_ad)
            elif scaling is None:
                v = coef
            else:
                v = 0.0
        op = term.get("op")
        if op == "add":
            acc += v
        elif op == "override":
            acc = v
        elif op == "multiply":
            acc *= v
        elif op == "divide":
            acc = acc / v if v else 0.0
    return acc


class Driver:
    """Base rotation: attacks are plain, the cast does nothing. Kits
    override what their ability does; see tft_kits. Hooks: `hit` (the unit
    took damage), `kill` (a dummy died to the unit), `died` (the unit fell:
    on-death bodies and effects go here)."""
    manaless = False

    def init(self, f):
        pass

    def cast_time(self, f):
        ct = f.sheet.unit.get("castTime")
        return CAST_TIME_DEFAULT if ct is None else float(ct)

    def attack(self, f, target):
        f.hit_attack(target)

    def cast(self, f):
        pass

    def tick(self, f):
        pass

    def hit(self, f, attacker, damage):
        pass

    def kill(self, f, target):
        pass

    def died(self, f):
        pass


def unit_primary_damage(unit, star):
    """The biggest damage number on the unit's ability card at `star`, as
    the data resolves it (100 ability power, first-star base stats)."""
    best = 0.0
    for name, calc in unit["calcs"].items():
        if "Damage" not in name.split(".")[-1]:
            continue
        vals = calc.get("values") or []
        if len(vals) >= star and vals[star - 1] is not None:
            best = max(best, float(vals[star - 1]))
    return best


def dummies_for(snap, n=N_DUMMIES, star=DUMMY_STAR):
    """Stat dummies from the set's own units at `star`: a frontline of
    median tank-role units (health, armor, magic resist), and a median
    non-tank behind them — so tank-only effects (Giant Slayer's amp) apply
    to the frontline, not to everything. Each carries its group's median
    offense too, for the fights where the dummies hit back: attack damage
    and speed, the ability's biggest number on its mana cadence, split
    physical/magic by the group's share of Attack-type roles."""
    tanks = [u for u in snap.units.values() if u["kind"] == "Tank"]
    others = [u for u in snap.units.values() if u["kind"] != "Tank"]
    if not tanks or not others:
        raise SystemExit("the snapshot has no tank-role or non-tank units")

    def median_of(units, kind):
        med = lambda xs: statistics.median(list(xs))
        casters = [u for u in units if u["stats"]["mana"] > 0 and ROLE_MANA.get(u["kind"], 0) > 0]
        return {"kind": kind,
                "hp": round(med(u["stats"]["hp"] * HP_PER_STAR ** (star - 1) for u in units)),
                "armor": med(u["stats"]["armor"] for u in units),
                "mr": med(u["stats"]["mr"] for u in units),
                "ad": round(med(u["stats"]["ad"] * AD_PER_STAR ** (star - 1) for u in units), 1),
                "as": med(u["stats"]["as"] for u in units),
                "ability": round(med(unit_primary_damage(u, star) for u in units)),
                "physicalShare": round(sum(1 for u in units if u["attack"]) / len(units), 2),
                "manaMax": med(u["stats"]["mana"] for u in casters),
                "manaStart": med(u["stats"]["initialMana"] for u in casters),
                "manaPerAttack": med(ROLE_MANA[u["kind"]] for u in casters),
                "manaFromDamage": kind == "tank"}
    tank, other = median_of(tanks, "tank"), median_of(others, "non-tank")
    slots = [tank] * (n - 1) + [other]
    crit_ev = 1.0 + 0.25 * 0.4    # every unit's base crit
    # a tank is hit by the whole enemy board: BOARD_SIZE units split by the
    # set's tank share, the tanks over the tank slots, the rest behind
    n_tanks = round(BOARD_SIZE * len(tanks) / (len(tanks) + len(others)))
    board = [0] * n
    for i in range(n_tanks):
        board[i % (n - 1)] += 1
    board[-1] = BOARD_SIZE - n_tanks

    def dps(streams):   # pre-mitigation damage per second, casting on attacks alone
        out = 0.0
        for s, k in zip(slots, streams):
            rate = s["manaPerAttack"] * s["as"]
            out += k * (s["ad"] * crit_ev * s["as"]
                        + (s["ability"] / (s["manaMax"] / rate + MANA_LOCK_S) if rate else 0.0))
        return round(out)
    return {"count": n, "star": star, "slots": slots, "tank": tank, "other": other,
            "tanks": len(tanks), "others": len(others),
            "totalHp": sum(s["hp"] for s in slots),
            "critEv": crit_ev, "pressureDps": dps([1] * n),
            "board": board, "boardSize": BOARD_SIZE, "boardPressureDps": dps(board)}


def make_dummies(spec, pressure=False, immortal=False, board=False):
    """The dummies; `pressure` arms them, `board` makes them stand for the
    whole enemy board's attackers (tank fights)."""
    out = []
    streams = spec.get("board") if board else None
    for i, s in enumerate(spec["slots"]):
        d = Dummy(float(s["hp"]), float(s["armor"]), float(s["mr"]))
        d.is_tank = s["kind"] == "tank"
        d.immortal = immortal
        if pressure:
            d.arm(s, spec.get("critEv", 1.1), streams[i] if streams else 1)
        out.append(d)
    return out


def simulate(snap, unit, star, item_apis, geometry, ctx_traits, dummy_spec,
             duration=None, item_fx=None, trait_fx=None, driver=None, pressure=None):
    """One build's fight. Returns (sheet numbers, result). The duration and
    whether the dummies hit back follow the unit's objective unless given."""
    import tft_kits
    item_fx = item_fx if item_fx is not None else load_item_effects(snap.set_no)
    trait_fx = trait_fx if trait_fx is not None else load_trait_effects(snap.set_no)
    fx = build_fx(snap, unit, item_apis, ctx_traits, item_fx, trait_fx)
    sheet = Sheet(unit, star, fx)
    driver = driver or tft_kits.driver_for(unit)
    objective = unit.get("objective", "carry")
    if pressure is None:
        pressure = objective in PRESSURED
    if duration is None:
        duration = fight_duration(unit)
    fight = Fight(sheet, make_dummies(dummy_spec, pressure, immortal=objective == "tank",
                                      board=objective == "tank"),
                  geometry, driver, duration, pressure=pressure)
    sheet.opening = {"ad": fight.ad(), "ap": fight.ap(), "as": fight.attack_speed(),
                     "hp": fight.max_hp(), "armor": fight.armor_now(), "mr": fight.mr_now()}
    res = fight.run()
    return sheet, res


# ---------------------------------------------------------------------------
# enumeration + ranking
# ---------------------------------------------------------------------------

def pool_items(snap, item_fx):
    """The craftable completed items a build may hold: every standard
    completed item (two components) that isn't excluded by hand."""
    excluded = (item_fx.get("excluded") or {})
    out = []
    for api, it in snap.items.items():
        if not api.startswith("DA_") or api in excluded:
            continue
        if any(x in api for x in ("Artifact", "Radiant", "Emblem", "Component", "Spatula")):
            continue
        if len(it["composition"]) != 2:
            continue
        out.append(api)
    return sorted(out, key=lambda a: snap.items[a]["name"])


def rank_key(res, objective="carry"):
    """Carries and fighters: killers by kill time, ties broken by the
    damage they had to spare (the uncapped total, i.e. overkill); the rest
    after them by damage dealt (a fighter's stops when it dies). Tanks: by
    how long they held the dummies, then by the damage their stuns denied,
    then damage dealt."""
    if objective == "tank":
        return (-res.get("aliveTime", 0.0), -res.get("denied", 0.0), -res["total"])
    if res["killTime"] is not None:
        return (0, res["killTime"], -res.get("rawTotal", res["total"]))
    return (1, 0.0, -res["total"], -res.get("rawTotal", res["total"]))


_CTX = {}


def _sim_task(combo):
    c = _CTX
    sheet, res = simulate(c["snap"], c["unit"], c["star"], combo, c["geometry"],
                          c["ctx_traits"], c["dummy"], c["duration"],
                          c["item_fx"], c["trait_fx"], c["driver"])
    o = sheet.opening
    return combo, {"ad": o["ad"], "ap": o["ap"], "as": o["as"],
                   "crit": sheet.crit_chance, "critMult": sheet.crit_mult,
                   "precision": sheet.precision, "hp": o["hp"], "armor": o["armor"],
                   "mr": o["mr"], "durability": sheet.durability,
                   "omnivamp": sheet.omnivamp, "form": sheet.form}, res


def enumerate_builds(snap, unit, star, geometry, ctx_traits, dummy_spec, pool,
                     duration=None, slots=3, workers=None, log=None):
    """Every multiset of `slots` pool items (unique items at most once),
    simulated and sorted best first. Returns (rows, count)."""
    import tft_kits
    item_fx = load_item_effects(snap.set_no)
    trait_fx = load_trait_effects(snap.set_no)
    combos = []
    for combo in itertools.combinations_with_replacement(pool, slots):
        ok = True
        for api in set(combo):
            if snap.items[api]["unique"] and combo.count(api) > 1:
                ok = False
                break
        if ok:
            combos.append(combo)
    global _CTX
    _CTX = {"snap": snap, "unit": unit, "star": star, "geometry": geometry,
            "ctx_traits": ctx_traits, "dummy": dummy_spec, "duration": duration,
            "item_fx": item_fx, "trait_fx": trait_fx,
            "driver": tft_kits.driver_for(unit)}
    workers = workers or os.cpu_count() or 1
    if workers > 1 and len(combos) > 200:
        with mp.get_context("fork").Pool(workers) as p:
            out = p.map(_sim_task, combos, chunksize=64)
    else:
        out = [_sim_task(c) for c in combos]
    objective = unit.get("objective", "carry")
    out.sort(key=lambda x: (rank_key(x[2], objective), x[0]))
    return out, len(combos)


# ---------------------------------------------------------------------------
# cache + warm (mirrors builds.py: content-addressed cells, one warmer)
# ---------------------------------------------------------------------------

def _source_hash():
    h = hashlib.sha256()
    for fn in ("tft.py", "tft_kits.py"):
        with open(os.path.join(BASE_DIR, fn), "rb") as f:
            h.update(f.read())
    return h.hexdigest()


SOURCE_HASH = _source_hash()


def source_stale():
    return _source_hash() != SOURCE_HASH


def modeled_units(snap):
    """Units with a driver, in cost then name order."""
    import tft_kits
    out = [u for u in snap.units.values() if tft_kits.has_driver(u)]
    return sorted(out, key=lambda u: (u["cost"], u["name"]))


def unit_slug(unit):
    return re.sub(r"[^a-z0-9]", "", unit["name"].lower())


def cells(snap=None):
    snap = snap or load_snapshot()
    return [(unit_slug(u), key) for u in modeled_units(snap) for key in SCENARIOS]


def cell_paths(snap=None):
    snap = snap or load_snapshot()
    base = hashlib.sha256(SOURCE_HASH.encode())
    base.update(snap.hash_inputs().encode())
    for fn in ("item-effects.json", "trait-effects.json", "kits.json"):
        p = os.path.join(set_dir(snap.set_no), fn)
        if os.path.exists(p):
            with open(p, "rb") as f:
                base.update(f.read())
    base.update(json.dumps([FIGHT_DURATION, TANK_DURATION, N_DUMMIES, DUMMY_STAR, STAGE,
                            CACHED_ROWS, sorted(SCENARIOS)]).encode())
    paths = {}
    for u in modeled_units(snap):
        slug = unit_slug(u)
        for key, sc in SCENARIOS.items():
            h = base.copy()
            h.update(json.dumps([u["api"], key, sc], sort_keys=True).encode())
            paths[(slug, key)] = os.path.join(
                CACHE_DIR, f"{slug}-{key}-{h.hexdigest()[:16]}.json")
    return paths


def cell_ready(snap=None):
    return {f"{slug}/{key}": os.path.exists(p)
            for (slug, key), p in cell_paths(snap).items()}


def cached_scenario(slug, key, paths=None):
    paths = paths or cell_paths()
    if (slug, key) not in paths:
        raise ValueError(f"unknown cell {slug}/{key}")
    p = paths[(slug, key)]
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def compute_cell(snap, unit, key, paths, log=None):
    import tft_kits
    sc = SCENARIOS[key]
    t0 = time.time()
    item_fx = load_item_effects(snap.set_no)
    trait_fx = load_trait_effects(snap.set_no)
    contexts, unmodeled = unit_trait_contexts(snap, unit, trait_fx)
    ctx_traits = contexts[sc["traits"]]
    dummy = dummies_for(snap)
    pool = pool_items(snap, item_fx)
    duration = fight_duration(unit)
    out, count = enumerate_builds(snap, unit, sc["star"], sc["geometry"],
                                  ctx_traits, dummy, pool, duration, log=log)
    secs = round(time.time() - t0, 1)
    pressured = unit["objective"] in PRESSURED
    rows = []
    for n, (combo, sheet, res) in enumerate(out[:CACHED_ROWS], 1):
        row = {
            "rank": n, "items": [snap.items[a]["name"] for a in combo],
            "ad": round(sheet["ad"], 1), "ap": round(sheet["ap"]),
            "attackSpeed": round(sheet["as"], 2),
            "crit": round(sheet["crit"] * 100), "precision": sheet["precision"],
            "killTime": round(res["killTime"], 2) if res["killTime"] is not None else None,
            "dps": round(res["dps"]), "total": round(res["total"]),
            "overkill": round(res["rawTotal"] - res["total"]),
            "attacks": res["attacks"], "casts": res["casts"],
            "breakdown": {k: round(v) for k, v in sorted(res["breakdown"].items(),
                                                          key=lambda kv: -kv[1])},
            "left": [round(x) for x in res["left"]],
        }
        if sheet["form"]:
            row["form"] = sheet["form"]
        if pressured:
            row.update({
                "hp": round(sheet["hp"]), "armor": round(sheet["armor"]), "mr": round(sheet["mr"]),
                "durability": round(sheet["durability"] * 100), "omnivamp": round(sheet["omnivamp"] * 100),
                "aliveTime": round(res["aliveTime"], 2), "died": res["died"],
                "diedAt": round(res["diedAt"], 2) if res["diedAt"] is not None else None,
                "hpLeft": round(res["hpLeft"]),
                "absorbed": round(res["absorbed"]), "taken": round(res["taken"]),
                "healed": round(res["healed"]), "shielded": round(res["shielded"]),
                "denied": round(res["denied"]), "ccTime": round(res["ccTime"], 1),
                "allyHeal": round(res["allyHeal"]), "allyShield": round(res["allyShield"]),
            })
        rows.append(row)
    fx_notes = build_fx(snap, unit, [], ctx_traits, item_fx, trait_fx).notes
    traits_active = [{"trait": snap.traits[api]["name"], "breakpoint": snap.traits[api]["levels"][col - 1]}
                     for api, col in ctx_traits]
    payload = {
        "unit": unit_slug(unit), "unitName": unit["name"], "unitApi": unit["api"],
        "cost": unit["cost"], "role": unit["roleName"], "kind": unit["kind"],
        "objective": unit["objective"], "pressured": pressured,
        "scenario": {**sc, "duration": duration, "dummy": dummy, "traitsActive": traits_active,
                     "traitsUnmodeled": unmodeled, "notes": fx_notes,
                     "driver": tft_kits.driver_for(unit).__class__.__name__},
        "set": snap.set_no, "patch": snap.patch, "buildsEvaluated": count,
        "rows": rows,
        "computedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "computeSeconds": secs,
    }
    os.makedirs(CACHE_DIR, exist_ok=True)
    slug = unit_slug(unit)
    path = paths[(slug, key)]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, path)
    for old in glob.glob(os.path.join(CACHE_DIR, f"{slug}-{key}-*.json")):
        if old != path:
            os.remove(old)
    return payload


def warm_lock():
    import fcntl
    os.makedirs(CACHE_DIR, exist_ok=True)
    f = open(os.path.join(CACHE_DIR, "lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def warm_running():
    lock = warm_lock()
    if lock is None:
        return True
    lock.close()
    return False


def _say(line):
    print(line, flush=True)


def warm(log=_say, only=None):
    """Compute every cold cell. Returns the count, or None if another
    warmer holds the lock."""
    import signal
    lock = warm_lock()
    if lock is None:
        return None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        for tmp in glob.glob(os.path.join(CACHE_DIR, "*.tmp")):
            os.remove(tmp)
        snap = load_snapshot()
        paths = cell_paths(snap)
        for path in glob.glob(os.path.join(CACHE_DIR, "*.json")):
            m = re.fullmatch(r"([a-z0-9]+)-(s\d-[a-z]+-[a-z]+)-[0-9a-f]{16}\.json",
                             os.path.basename(path))
            if m and (m.group(1), m.group(2)) not in paths:
                os.remove(path)
        units = modeled_units(snap)
        cold = [(u, key) for u in units for key in SCENARIOS
                if not os.path.exists(paths[(unit_slug(u), key)])
                and (only is None or unit_slug(u) == only)]
        done = 0
        for n, (u, key) in enumerate(cold, 1):
            log(f"[{n}/{len(cold)}] {u['name']} {key} …")
            out = compute_cell(snap, u, key, paths, log=log)
            best = out["rows"][0] if out["rows"] else None
            score = "" if not best else (
                f" (held {best['aliveTime']}s)" if out["objective"] == "tank"
                else f" ({best['killTime']}s)" if best["killTime"] is not None
                else f" ({best['total']} dmg)")
            log(f"  {out['buildsEvaluated']:,} builds in {out['computeSeconds']}s"
                + (f" — best {', '.join(best['items'])}{score}" if best else ""))
            done += 1
        return done
    finally:
        lock.close()


# ---------------------------------------------------------------------------
# web API
# ---------------------------------------------------------------------------

def api_meta():
    snap = load_snapshot()
    item_fx = load_item_effects(snap.set_no)
    trait_fx = load_trait_effects(snap.set_no)
    kits = load_kits(snap.set_no)
    units = []
    for u in modeled_units(snap):
        contexts, unmodeled = unit_trait_contexts(snap, u, trait_fx)
        units.append({
            "slug": unit_slug(u), "name": u["name"], "api": u["api"],
            "cost": u["cost"], "role": u["roleName"], "kind": u["kind"],
            "attack": u["attack"], "objective": u["objective"],
            "duration": fight_duration(u),
            "traits": u["traits"], "ability": u["ability"]["name"],
            "forms": sorted(u["forms"]) if u.get("forms") else [],
            "traitsModeled": [snap.traits[a]["name"] for a, _ in contexts["high"]],
            "traitsUnmodeled": unmodeled,
            "note": (kits.get("units", {}).get(u["api"]) or {}).get("note"),
        })
    pool = pool_items(snap, item_fx)
    items = []
    for api in pool:
        it = snap.items[api]
        spec = (item_fx.get("items") or {}).get(api) or {}
        items.append({"api": api, "name": it["name"],
                      "stats": parse_stat_line(it),
                      "modeled": [k for k in spec if k not in ("note", "name", "covers")],
                      "note": spec.get("note")})
    return {
        "set": snap.set_no, "patch": snap.patch,
        "metatft": snap.meta.get("metatft"), "fetchedAt": snap.meta.get("fetchedAt"),
        "units": units, "scenarios": list(SCENARIOS.values()),
        "stars": list(STARS), "geometries": GEOMETRIES, "traitContexts": TRAIT_CONTEXTS,
        "dummy": dummies_for(snap), "items": items,
        "excluded": item_fx.get("excluded") or {},
        "objectives": OBJECTIVES,
        "rules": {"adPerStar": AD_PER_STAR, "hpPerStar": HP_PER_STAR,
                  "critExcess": CRIT_EXCESS_TO_DAMAGE, "manaLock": MANA_LOCK_S,
                  "asCap": AS_CAP, "stage": STAGE, "duration": FIGHT_DURATION,
                  "tankDuration": TANK_DURATION,
                  "tankMana": [TANK_MANA_PER_PREMIT, TANK_MANA_PER_POSTMIT, TANK_MANA_PER_HIT_CAP],
                  "assassinReduction": ASSASSIN_OFFTARGET_REDUCTION},
        "note": item_fx.get("_note") or "",
    }


# ---------------------------------------------------------------------------
# patch-note reconciliation
# ---------------------------------------------------------------------------

def _nums(s):
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", s)]


def check_patch_notes(snap):
    """Compare the snapshot's numbers with the patch notes' 'new' values.
    Returns (findings, unmatched): a finding says whether the snapshot
    already carries the new value, still has the old one, or neither."""
    path = os.path.join(snap.dir, "patchnotes.json")
    if not os.path.exists(path):
        return [], []
    with open(path) as f:
        changes = json.load(f).get("changes") or []
    findings, unmatched = [], []
    for ch in changes:
        what = ch["what"]
        unit = next((u for u in snap.units.values()
                     if what.lower().startswith(u["name"].lower())), None)
        if unit is None:
            unmatched.append(ch)
            continue
        old, new = _nums(ch["old"]), _nums(ch["new"])
        label = what[len(unit["name"]):].strip().lower()
        stat_key = None
        for k, words in (("mana", ("mana",)), ("as", ("attack speed", "base as")),
                         ("hp", ("health",)), ("ad", ("attack damage", "base ad")),
                         ("armor", ("resists", "armor")), ("mr", ("magic resist",))):
            if any(w in label for w in words) and "ability" not in label:
                stat_key = k
                break
        found = None
        if stat_key == "mana" and len(new) == 2:
            have = [unit["stats"]["initialMana"], unit["stats"]["mana"]]
            found = ("stats initialMana/mana", have, new)
        elif stat_key == "armor" and "resists" in label and len(new) == 1:
            have = [unit["stats"]["armor"], unit["stats"]["mr"]]
            found = ("stats armor/mr", have, new + new)
        elif stat_key in ("as", "hp", "ad", "armor", "mr") and len(new) == 1:
            have = [unit["stats"][stat_key]]
            found = (f"stats {stat_key}", have, new)
        else:
            # an ability number: the curve row whose per-star values match the old or new values
            for row, curve in unit["curve"].items():
                vals = [curve_at(curve, s) for s in range(1, 5)]
                for cand in (new, old):
                    n = len(cand)
                    if n >= 2 and vals[:n] == cand:
                        found = (f"curve {row}", vals[:n], new)
                        break
                if found:
                    break
        if found is None:
            unmatched.append(ch)
            continue
        where, have, want = found
        status = "current" if have[:len(want)] == want else \
            ("stale" if have[:len(old)] == old else "differs")
        findings.append({"unit": unit["name"], "api": unit["api"], "what": what,
                         "where": where, "have": have, "old": old, "new": want,
                         "status": status})
    return findings, unmatched


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_status(args):
    for set_no in sorted(int(d[3:]) for d in os.listdir(TFT_DATA_DIR)
                         if d.startswith("set") and d[3:].isdigit()) if os.path.isdir(TFT_DATA_DIR) else []:
        for patch in patch_dirs(set_no):
            snap = Snapshot(set_no, patch)
            print(f"set {set_no} patch {patch}: {len(snap.units)} units, "
                  f"{len(snap.items)} items, {len(snap.traits)} traits, "
                  f"fetched {snap.meta.get('fetchedAt')}, MetaTFT stamp "
                  f"{(snap.meta.get('metatft') or {}).get('patch')}")


def cmd_check(args):
    snap = load_snapshot(args.set, args.patch)
    findings, unmatched = check_patch_notes(snap)
    stale = [f for f in findings if f["status"] != "current"]
    for f in findings:
        mark = {"current": "ok   ", "stale": "STALE", "differs": "DIFF "}[f["status"]]
        print(f"{mark} {f['unit']:<14} {f['what']:<40} {f['where']:<28} "
              f"have {f['have']} → notes say {f['new']}")
    print(f"\n{len(findings)} matched, {len(stale)} need attention, "
          f"{len(unmatched)} lines not matched to a unit number:")
    for ch in unmatched[:40]:
        print(f"   ? {ch['what']}: {ch['old']} ⇒ {ch['new']}")
    if stale:
        print(f"\nOverride snippet ({os.path.relpath(snap.dir)}/overrides.json):")
        snippet = {"units": {}}
        for f in stale:
            u = snippet["units"].setdefault(f["api"], {})
            if f["where"].startswith("curve "):
                u.setdefault("curve", {})[f["where"][6:]] = f["new"]
            elif f["where"] == "stats initialMana/mana":
                u.setdefault("stats", {}).update({"initialMana": f["new"][0], "mana": f["new"][1]})
            elif f["where"] == "stats armor/mr":
                u.setdefault("stats", {}).update({"armor": f["new"][0], "mr": f["new"][1]})
            else:
                u.setdefault("stats", {})[f["where"].split()[1]] = f["new"][0]
        print(json.dumps(snippet, indent=1))
        # a non-zero exit lets the refresh job's heartbeat flag it
        sys.exit(2)


def cmd_units(args):
    snap = load_snapshot(args.set, args.patch)
    import tft_kits
    for u in sorted(snap.units.values(), key=lambda u: (u["cost"], u["name"])):
        drv = "driver" if tft_kits.has_driver(u) else "-"
        s = u["stats"]
        print(f"{u['cost']}  {u['name']:<14} {drv:<7} {u['roleName']:<18} {u['objective']:<8} "
              f"{'/'.join(u['traits']):<32} hp {s['hp']:.0f} ad {s['ad']:.0f} as {s['as']:.2f} "
              f"mana {s['initialMana']:.0f}/{s['mana']:.0f} range {s['range']:.0f}"
              f"  cast {u.get('castTime') if u.get('castTime') is not None else '-'}")


def cmd_sim(args):
    snap = load_snapshot(args.set, args.patch)
    unit = snap.unit(args.name)
    items = [snap.item(x)["api"] for x in args.items]
    trait_fx = load_trait_effects(snap.set_no)
    contexts, unmodeled = unit_trait_contexts(snap, unit, trait_fx)
    ctx = contexts[args.traits]
    dummy = dummies_for(snap)
    sheet, res = simulate(snap, unit, args.star, items, args.geometry, ctx, dummy,
                          args.duration)
    print(f"{unit['name']} {args.star}★ with {', '.join(snap.items[a]['name'] for a in items) or 'no items'}"
          f" · {args.geometry} · traits {args.traits}"
          + (f" ({', '.join(snap.traits[a]['name'] + ' ' + str(snap.traits[a]['levels'][c-1]) for a, c in ctx)})" if ctx else ""))
    o = sheet.opening
    print(f"  {unit['roleName']} → {unit['objective']} fight ({OBJECTIVES[unit['objective']]})"
          + (f" · form {sheet.form}" if sheet.form else ""))
    print(f"  AD {o['ad']:.1f}  AP {o['ap']:.0f}  AS {o['as']:.2f}  "
          f"crit {sheet.crit_chance*100:.0f}% ×{sheet.crit_mult:.2f}  precision {sheet.precision}  "
          f"mana {sheet.mana_start:.0f}/{sheet.mana_max:.0f} +{sheet.mana_per_attack + sheet.fx.manaPerAttack:.0f}/attack +{sheet.fx.manaRegen:.1f}/s"
          f"  HP {o['hp']:.0f} armor {o['armor']:.0f} MR {o['mr']:.0f} durability {sheet.durability*100:.0f}% omnivamp {sheet.omnivamp*100:.0f}%")
    print("  dummies: " + "; ".join(f"{s['hp']} HP / {s['armor']} armor / {s['mr']} MR ({s['kind']})"
                                    for s in dummy["slots"])
          + f" — median {dummy['star']}★ of {dummy['tanks']} tanks and {dummy['others']} others")
    if unit["objective"] in PRESSURED:
        board = unit["objective"] == "tank"
        streams = dummy["board"] if board else [1] * dummy["count"]
        print("  they hit back: " + "; ".join(
            f"{k}× {s['ad']} AD at {s['as']}/s, {s['ability']} per cast at {s['manaStart']:.0f}/{s['manaMax']:.0f} mana "
            f"+{s['manaPerAttack']:.0f}/attack{' + damage taken' if s['manaFromDamage'] else ''} ({s['physicalShare']*100:.0f}% physical)"
            for s, k in zip(dummy["slots"], streams))
            + f" — about {dummy['boardPressureDps'] if board else dummy['pressureDps']} pre-mitigation DPS"
            + (f" (a tank is hit by the whole {dummy['boardSize']}-unit board)" if board else ""))
    kt = f"all dead at {res['killTime']:.2f}s" if res["killTime"] is not None else f"survive ({[round(x) for x in res['left']]} HP left)"
    print(f"  {res['total']:.0f} damage, {res['dps']:.0f} DPS, {res['attacks']} attacks, {res['casts']} casts — {kt}")
    for src, v in sorted(res["breakdown"].items(), key=lambda kv: -kv[1]):
        print(f"    {src:<12} {v:8.0f}  {v / max(res['total'], 1e-9) * 100:5.1f}%")
    if unit["objective"] in PRESSURED:
        fate = f"died at {res['diedAt']:.2f}s" if res["died"] else f"alive with {res['hpLeft']:.0f} HP"
        print(f"  {fate}; held the dummies {res['aliveTime']:.2f}s; took {res['absorbed']:.0f} pre-mitigation "
              f"({res['taken']:.0f} after resists/durability, {res['shielded']:.0f} on shields), healed {res['healed']:.0f}, "
              f"denied {res['denied']:.0f} by CC/untargetability ({res['ccTime']:.1f} stun-seconds)"
              + (f", allies healed {res['allyHeal']:.0f}" if res["allyHeal"] else "")
              + (f", allies shielded {res['allyShield']:.0f}" if res["allyShield"] else ""))
    if unmodeled or sheet.fx.notes:
        print("  not modeled: " + "; ".join(unmodeled + sheet.fx.notes))


def cmd_top(args):
    snap = load_snapshot(args.set, args.patch)
    unit = snap.unit(args.name)
    item_fx = load_item_effects(snap.set_no)
    trait_fx = load_trait_effects(snap.set_no)
    contexts, unmodeled = unit_trait_contexts(snap, unit, trait_fx)
    pool = pool_items(snap, item_fx)
    dummy = dummies_for(snap)
    t0 = time.time()
    out, count = enumerate_builds(snap, unit, args.star, args.geometry,
                                  contexts[args.traits], dummy, pool, args.duration)
    print(f"{unit['name']} {args.star}★ · {args.geometry} · traits {args.traits} · "
          f"{unit['roleName']} → {unit['objective']} fight: {count:,} builds in {time.time() - t0:.1f}s")
    for n, (combo, sheet, res) in enumerate(out[:args.top], 1):
        kt = f"{res['killTime']:.2f}s" if res["killTime"] is not None else "survive"
        held = f"  held {res['aliveTime']:5.1f}s" if unit["objective"] in PRESSURED else ""
        print(f"{n:3} {', '.join(snap.items[a]['name'] for a in combo):<60} {kt:>8}  "
              f"{res['total']:7.0f} dmg  {res['dps']:5.0f} dps  {res['casts']} casts{held}")


def cmd_warm(args):
    n = warm(only=args.unit)
    if n is None:
        print("Another warm is already running (it holds .cache/tft/lock).")
    elif n == 0:
        print("Nothing to do — every cell is computed for the current code and data.")
    else:
        print(f"Computed {n} cell(s).")
