"""Theoretical build math: exact stat sheets (and, next, damage simulation)
for champion + item combinations — first principles only, no match data.

The plan for this domain, built up in phases:
  1. stat layer — resolve champion base stats at a level plus an item set
     into a final stat sheet using the real in-game rules
  2. kit encoding — data/builds/<champ>.json, hand-encoded ability formulas
  3. combat engine — deterministic event-based DPS vs a configured target
  4. optimizer — rank builds over a scenario grid (level x target x duration)

The numbers (the sheet, the fight, the enumeration's inner loop) are computed
by the compiled engine in engine/ (Rust, imported as lol_engine — build it
with jobs/build-engine.sh). This module keeps the data, the parent side of
the enumeration, the cache and the CLI, and wraps the engine's entry points
with the Python-facing contracts (resolve_stats, simulate, enumerate_builds).
The fixtures under data/builds/golden pin the engine's output bit for bit.

Data sources — the rule everywhere is: ddragon (Riot, patch-versioned) is
canonical wherever it has the number; meraki fills the structural gaps but
can lag patches behind:
- Item stats: numeric stats are parsed from the ddragon snapshot's <stats>
  description block (data/items/<patch>/ddragon.json) — meraki's structured
  stats proved stale in 16.16 (Berserker's 25% AS vs the real 30%) and only
  supply the fallback, plus passives text, prices and nicknames. Passive
  effects that exist only as text (Rabadon's 30%, on-hit riders) live in the
  hand-curated overlay data/builds/item-effects.json — re-check each patch.
- Champion base stats: data/builds/champions/<patch>/<slug>/ddragon.json
  (`builds fetch-champion` snapshots it). Meraki's champion file rides along
  for what ddragon lacks — attack-speed ratio and windup (meta.json records
  how far behind it is).
- Kit encodings were verified against Riot's actual game files
  (raw.communitydragon.org/<patch>/game/data/characters/<champ>/) — spell
  base damages, ratios, cooldowns and mana as of 16.16.

Stat formulas follow the League wiki ("Champion statistic", "Armor
penetration"); each is cited at its implementation.
"""

import ctypes
import glob
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

import items
from common import BASE_DIR, DATA_DIR, patch_key

try:
    import lol_engine
except ImportError as e:  # the compiled engine: engine/, built by jobs/build-engine.sh
    raise ImportError("builds needs the compiled engine (lol_engine.abi3.so at the repo "
                      "root) — run jobs/build-engine.sh; it uses cargo, or fetches one "
                      "through nix-shell") from e

ENGINE_DIR = os.path.join(BASE_DIR, "engine")

BUILDS_DATA_DIR = os.path.join(DATA_DIR, "builds")
CHAMPIONS_DIR = os.path.join(BUILDS_DATA_DIR, "champions")
ITEM_EFFECTS_PATH = os.path.join(BUILDS_DATA_DIR, "item-effects.json")

DDRAGON_CHAMP_INDEX = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
DDRAGON_CHAMP = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion/{cid}.json"
MERAKI_CHAMP = "https://cdn.merakianalytics.com/riot/lol/resources/latest/en-US/champions/{cid}.json"

AS_CAP = 2.5  # attack speed is hard-capped in game (the engine's num.rs agrees)
INF = float("inf")


# ---------------------------------------------------------------------------
# League math primitives
# ---------------------------------------------------------------------------

def growth(level):
    """Riot's per-level stat growth multiplier: stats don't grow linearly,
    early levels grant less. stat(n) = base + g * growth(n), and growth(18)
    is exactly 17 so the level-18 value matches base + 17 * g."""
    return (level - 1) * (0.7025 + 0.0175 * (level - 1))


def stat_at(base, per_level, level):
    return base + per_level * growth(level)


def resist_mult(resist):
    """Post-mitigation damage multiplier for armor/MR, including the
    negative-resist rule (reductions can push resists below zero)."""
    if resist >= 0:
        return 100.0 / (100.0 + resist)
    return 2.0 - 100.0 / (100.0 - resist)


def penetrate(resist, pct_pen=0.0, flat_pen=0.0):
    """Attacker's view of a target resist: percent pen applies before flat
    pen (lethality / flat magic pen), and penetration never takes a resist
    below zero — only reductions (Black Cleaver et al, not modeled yet) can."""
    return max(0.0, resist * (1.0 - pct_pen / 100.0) - flat_pen)


def stack_pct_pen(a, b):
    """Percent penetration from different sources stacks multiplicatively:
    40% + 30% -> 58%, not 70%."""
    return 100.0 * (1.0 - (1.0 - a / 100.0) * (1.0 - b / 100.0))


def eff_resist(base, flat_reduction=0.0, pct_reduction=0.0,
               pct_pen=0.0, flat_pen=0.0):
    """Wiki order of operations: flat reduction -> % reduction -> % pen ->
    flat pen. Reductions may push a resist negative; penetration applies
    only to what's still positive and stops at zero."""
    r = (base - flat_reduction) * (1.0 - pct_reduction / 100.0)
    if r > 0:
        r = max(0.0, r * (1.0 - pct_pen / 100.0) - flat_pen)
    return r


# ---------------------------------------------------------------------------
# kit encodings (hand-curated ability formulas, data/builds/<slug>.json)
# ---------------------------------------------------------------------------

def load_kit(slug, required=True):
    path = os.path.join(BUILDS_DATA_DIR, f"{slug}.json")
    if not os.path.exists(path):
        if not required:
            return None
        sys.exit(f"No kit encoding at data/builds/{slug}.json — only "
                 f"hand-encoded champions can be simulated.")
    with open(path) as f:
        return json.load(f)


def kit_max_order(kit, override=None):
    """Ability max order for skill_ranks: a CLI override ("Q,E,W"), else the
    kit's declared order, else the Q > E > W default."""
    if override:
        return tuple(override.upper().split(","))
    return tuple(kit.get("maxOrder", ("Q", "E", "W")))


def by_level(spec, level):
    """Evaluate a {'from', 'to', 'curve': 'linear', 'levels': [lo, hi]}
    level-scaled value ('20 - 41 (based on level)' on the wiki)."""
    lo, hi = spec["levels"]
    frac = (min(max(level, lo), hi) - lo) / (hi - lo)
    return spec["from"] + (spec["to"] - spec["from"]) * frac


def ability_hit(dmg, rank, sheet):
    """Raw (pre-mitigation) damage of one ability hit at `rank` (1-5):
    base plus AD/AP ratios, plus ratios on the caster's own health
    (Vladimir's E and W) where the encoding has them."""
    amt = (dmg["base"][rank - 1]
           + dmg.get("bonusAdRatio", 0.0) * sheet["ad_bonus"]
           + dmg.get("adRatio", 0.0) * sheet["ad"]
           + dmg.get("apRatio", 0.0) * sheet["ap"])
    if "maxHpRatio" in dmg:
        amt += dmg["maxHpRatio"] / 100.0 * sheet["hp"]
    if "bonusHpRatio" in dmg:
        amt += dmg["bonusHpRatio"] / 100.0 * sheet["hp_bonus"]
    return amt


# ---------------------------------------------------------------------------
# data loading: champion snapshots, item pool, effects overlay
# ---------------------------------------------------------------------------

def champion_patches():
    if not os.path.isdir(CHAMPIONS_DIR):
        return []
    return sorted(os.listdir(CHAMPIONS_DIR), key=patch_key)


def load_champion(slug, patch=None):
    patches = [patch] if patch else reversed(champion_patches())
    for p in patches:
        cdir = os.path.join(CHAMPIONS_DIR, p, slug)
        if not os.path.isdir(cdir):
            continue
        with open(os.path.join(cdir, "ddragon.json")) as f:
            dd = json.load(f)
        mk = None
        mk_path = os.path.join(cdir, "meraki.json")
        if os.path.exists(mk_path):
            with open(mk_path) as f:
                mk = json.load(f)
        with open(os.path.join(cdir, "meta.json")) as f:
            meta = json.load(f)
        return {"slug": slug, "dd": dd, "mk": mk, "meta": meta}
    sys.exit(f"No snapshot for '{slug}'"
             + (f" at patch {patch}" if patch else "")
             + f" — run `lol.py builds fetch-champion {slug}` first.")


# ddragon's <stats> description block, "80 Ability Power" style, mapped onto
# meraki's schema. ddragon is canonical per patch — meraki item stats can lag
# (16.16: Berserker's 30% AS vs meraki's stale 25%) — so parsed ddragon
# values override meraki's, which then only supplies passives and nicknames.
DD_STAT_NAMES = {
    ("Ability Power", False): ("abilityPower", "flat"),
    ("Attack Damage", False): ("attackDamage", "flat"),
    ("Attack Speed", True): ("attackSpeed", "flat"),  # meraki keeps %AS in .flat
    ("Ability Haste", False): ("abilityHaste", "flat"),
    ("Magic Penetration", False): ("magicPenetration", "flat"),
    ("Magic Penetration", True): ("magicPenetration", "percent"),
    ("Armor Penetration", True): ("armorPenetration", "percent"),
    ("Lethality", False): ("lethality", "flat"),
    ("Health", False): ("health", "flat"),
    ("Mana", False): ("mana", "flat"),
    ("Armor", False): ("armor", "flat"),
    ("Magic Resist", False): ("magicResistance", "flat"),
    ("Move Speed", False): ("movespeed", "flat"),
    ("Move Speed", True): ("movespeed", "percent"),
    ("Critical Strike Chance", True): ("criticalStrikeChance", "percent"),
    ("Critical Strike Damage", True): ("criticalStrikeDamage", "percent"),
    ("Life Steal", True): ("lifesteal", "percent"),
    ("Omnivamp", True): ("omnivamp", "percent"),
    ("Tenacity", True): ("tenacity", "percent"),
    ("Heal and Shield Power", False): ("healAndShieldPower", "flat"),
    ("Heal and Shield Power", True): ("healAndShieldPower", "flat"),
}
DD_STAT_IGNORED = ("Base Health Regen", "Base Mana Regen", "Gold Per 10")


def parse_dd_stats(description):
    """The <stats> block of a ddragon item description -> meraki-schema
    stats, or None if any line fails to parse (caller falls back whole)."""
    m = re.search(r"<stats>(.*?)</stats>", description, re.S)
    if not m:
        return None
    out = {}
    for line in re.sub(r"<[^>]+>", " ", m.group(1).replace("<br>", "\n")).splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        lm = re.fullmatch(r"\+?([\d.]+)(%?)\s+(.+)", line)
        if not lm:
            return None
        val, pct, name = float(lm.group(1)), lm.group(2) == "%", lm.group(3)
        if any(name.startswith(i) for i in DD_STAT_IGNORED):
            continue
        key = DD_STAT_NAMES.get((name, pct))
        if not key:
            return None
        out.setdefault(key[0], {})[key[1]] = out.get(key[0], {}).get(key[1], 0.0) + val
    return out


def load_items(patch=None):
    """The item pool as {int id: meraki item}, with each item's `stats`
    replaced by the parsed ddragon <stats> block wherever it parses."""
    metas = items.snapshots()
    if patch:
        metas = [m for m in metas if m["patch"] == patch]
    for m in reversed(metas):
        mk_path = os.path.join(items.ITEMS_DATA_DIR, m["patch"], "meraki.json")
        if not os.path.exists(mk_path):
            continue
        with open(mk_path) as f:
            pool = {int(k): v for k, v in json.load(f).items()}
        dd_path = os.path.join(items.ITEMS_DATA_DIR, m["patch"], "ddragon.json")
        if os.path.exists(dd_path):
            with open(dd_path) as f:
                dd = json.load(f)
            for iid, it in pool.items():
                parsed = parse_dd_stats(dd.get(str(iid), {}).get("description", ""))
                if parsed is not None:
                    it["stats"] = parsed
            # ddragon is canonical: synthesize entries for items meraki
            # hasn't (re)published — e.g. Stormrazor came back as 3095 in
            # 16.17 and meraki still only knew the retired 3097.
            for sid, d in dd.items():
                if int(sid) in pool:
                    continue
                desc = d.get("description", "")
                pool[int(sid)] = {
                    "id": int(sid),
                    "name": d["name"],
                    "stats": parse_dd_stats(desc) or {},
                    "passives": [{"unique": True, "name": n} for n in
                                 re.findall(r"<passive>(.*?)</passive>", desc)],
                    "nicknames": [n for n in d.get("colloq", "").split(";") if n],
                    "shop": {"prices": {"total": d.get("gold", {}).get("total", 0)},
                             "purchasable": d.get("gold", {}).get("purchasable", False)},
                }
        return m["patch"], pool
    sys.exit("No meraki item snapshot — run `lol.py items fetch` first.")


def load_item_effects():
    if not os.path.exists(ITEM_EFFECTS_PATH):
        return {}
    with open(ITEM_EFFECTS_PATH) as f:
        return {int(k): v for k, v in json.load(f)["items"].items()}


def groups_path(patch=None):
    """Newest snapshot carrying the distilled item-bin rules, or None."""
    metas = items.snapshots()
    if patch:
        metas = [m for m in metas if m["patch"] == patch]
    for m in reversed(metas):
        path = os.path.join(items.ITEMS_DATA_DIR, m["patch"], "groups.json")
        if os.path.exists(path):
            return path
    return None


def load_item_rules(patch=None):
    """Riot's own item rules, captured by `lol.py items fetch`."""
    path = groups_path(patch)
    if path is None:
        print("Warning: no groups.json in any item snapshot — build "
              "exclusivity is NOT enforced. Run `lol.py items fetch --force`.",
              file=sys.stderr)
        return {}, {}, {}, set()
    with open(path) as f:
        g = json.load(f)
    return ({int(k): v for k, v in g["items"].items()},
            g["maxOwnable"],
            {int(k): v for k, v in g.get("currencyGated", {}).items()},
            {int(i) for i in g.get("retired", [])})


def load_exclusive_groups(patch=None):
    """The in-game ownership limits. Returns ({item id: [group, ...]},
    {group: max ownable}) — an item can sit in several groups at once
    (Terminus is both a Last Whisper and a Void Pen item)."""
    groups, caps, _, _ = load_item_rules(patch)
    return groups, caps


def load_gated_items(patch=None):
    """{item id: currency} for items gold alone cannot buy — the Feats of
    Strength boots and the support-quest line. They must never enter a pool
    of freely-buyable items."""
    return load_item_rules(patch)[2]


def load_retired_items(patch=None):
    """Ids pulled from the shop whose data ddragon still carries."""
    return load_item_rules(patch)[3]


def group_name(group):
    """'Items/ItemGroups/LastWhisper' -> 'Last Whisper'; unresolved hashes
    stay as-is (they still identify a group, just without a readable name)."""
    tail = group.rsplit("/", 1)[-1]
    if tail.startswith("{"):
        return tail
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tail)


def build_is_legal(ids, groups, caps):
    """False when a build owns more of a group than the game allows."""
    counts = {}
    for i in ids:
        for g in groups.get(i, ()):
            counts[g] = counts.get(g, 0) + 1
            if counts[g] > caps.get(g, 99):
                return False
    return True


def norm_name(s):
    return "".join(ch for ch in s.lower() if ch.isalnum())


def item_index(pool):
    """Lookup by id, meraki nickname, or normalized name. Purchasable items
    win name collisions with system entries."""
    idx = {}
    for iid, it in sorted(pool.items(),
                          key=lambda kv: kv[1]["shop"]["purchasable"]):
        idx[str(iid)] = iid
        idx[norm_name(it["name"])] = iid
        for nick in it.get("nicknames", []):
            idx.setdefault(norm_name(nick), iid)
    return idx


def resolve_item(pool, idx, token):
    iid = idx.get(norm_name(token)) or idx.get(token)
    if iid is None:
        # unique substring match is good enough: "rabadons" -> Rabadon's Deathcap
        near = {i for k, i in idx.items() if norm_name(token) in k}
        if len(near) == 1:
            return near.pop()
        names = sorted(pool[i]["name"] for i in near)[:8]
        sys.exit(f"Unknown item '{token}'."
                 + (f" Matches several: {', '.join(names)}" if names else ""))
    return iid


# ---------------------------------------------------------------------------
# stat resolution
# ---------------------------------------------------------------------------

# meraki (stat, field) -> how it lands in the sheet. Percent-natured stats
# arrive as percent points (attackSpeed.flat 50.0 == +50% AS). Income and
# regen stats are irrelevant to damage math and deliberately dropped.
ITEM_STAT_MAP = {
    ("abilityHaste", "flat"): "haste",
    ("abilityPower", "flat"): "ap_flat",
    ("armor", "flat"): "armor",
    ("armorPenetration", "percent"): "armor_pen_pct",
    ("attackDamage", "flat"): "ad_bonus",
    ("attackSpeed", "flat"): "bonus_as_pct",
    ("criticalStrikeChance", "percent"): "crit_chance",
    ("criticalStrikeDamage", "percent"): "crit_damage_bonus",
    ("healAndShieldPower", "flat"): "heal_shield_power",
    ("health", "flat"): "hp",
    ("lethality", "flat"): "lethality",
    ("lifesteal", "percent"): "lifesteal",
    ("magicPenetration", "flat"): "magic_pen_flat",
    ("magicPenetration", "percent"): "magic_pen_pct",
    ("magicResistance", "flat"): "mr",
    ("mana", "flat"): "mana",
    ("movespeed", "flat"): "ms_flat",
    ("movespeed", "percent"): "ms_pct",
    ("omnivamp", "percent"): "omnivamp",
    ("tenacity", "percent"): "tenacity",
}
IGNORED_ITEM_STATS = {("goldPer10", "flat"), ("healthRegen", "flat"),
                      ("healthRegen", "percent"), ("manaRegen", "percent")}


def champ_base(champ):
    """The champion base stats the sheet needs, as one flat dict for the
    engine: ddragon's numbers with meraki's attack-speed ratio and crit damage
    riding along, and the AD-growth fallback resolved — ddragon 16.5.1 onward
    publishes attackdamageperlevel = 0 for every champion at once (a Data
    Dragon regression, not a game change; Riot's files keep the real growth,
    16.17: Vladimir 3, Kayle 2.5), so meraki's per-level AD stands in for
    exactly that case. Meraki lags patches: re-check a champion against
    raw.communitydragon.org when the number matters."""
    dd = champ["dd"]["stats"]
    mk = (champ["mk"] or {}).get("stats", {})
    ad_growth = dd["attackdamageperlevel"]
    if ad_growth == 0:
        ad_growth = mk.get("attackDamage", {}).get("perLevel", 0.0) or 0.0
    return dict(
        hp=dd["hp"], hp_per=dd["hpperlevel"], mp=dd["mp"], mp_per=dd["mpperlevel"],
        armor=dd["armor"], armor_per=dd["armorperlevel"],
        mr=dd["spellblock"], mr_per=dd["spellblockperlevel"],
        ad=dd["attackdamage"], ad_per=ad_growth,
        base_as=dd["attackspeed"], as_per=dd["attackspeedperlevel"],
        # ddragon doesn't ship the ratio; without meraki, ratio = base AS
        as_ratio=mk.get("attackSpeedRatio", {}).get("flat", dd["attackspeed"]),
        # 175% base crit damage; items with criticalStrikeDamage add to it
        crit_damage_base=mk.get("criticalStrikeDamage", {}).get("flat", 175.0),
        move_speed=dd["movespeed"], attack_range=dd["attackrange"])


def stat_pairs(item):
    """An item's mapped, nonzero stats as (sheet key, value), in the order
    the sheet folds them. Percent-natured stats arrive as percent points
    (attackSpeed.flat 50.0 == +50% AS). Warns about a stat ITEM_STAT_MAP
    doesn't know, so a meraki schema change can't slip past silently."""
    out = []
    for stat, fields in item["stats"].items():
        for field, val in fields.items():
            if not val:
                continue
            key = ITEM_STAT_MAP.get((stat, field))
            if key:
                out.append((key, val))
            elif (stat, field) not in IGNORED_ITEM_STATS:
                print(f"Warning: unmapped item stat {stat}.{field} on "
                      f"{item['name']} — update ITEM_STAT_MAP", file=sys.stderr)
    return out


def resolve_stats(champ, level, item_ids, pool, effects=None, kit=None):
    """Champion base stats at `level` plus `item_ids` -> final stat sheet.
    `kit` (the champion's encoding) supplies stat-converting passives —
    Vladimir's Crimson Pact; without it the sheet is items and levels only.

    The numbers come from the engine (engine/src/sheet.rs: the in-game
    rules, the AP multipliers, the stat-granting passives that need the item
    totals, the Crimson Pact / Riftmaker closed form). Returns a flat dict;
    `uncovered` lists item passives that exist only as text and aren't part
    of the sheet (the combat engine's job, or genuinely out of scope) so
    callers can show what the numbers do NOT include.
    """
    effects = effects if effects is not None else load_item_effects()
    entries, names, gold, uncovered = [], [], 0, []
    for iid in item_ids:
        it = pool[iid]
        names.append(it["name"])
        gold += it["shop"]["prices"]["total"]
        fx = effects.get(iid, {})
        entries.append((stat_pairs(it), fx))
        covered = set(fx.get("covers", []))
        uncovered += [f"{p['name']} ({it['name']})" for p in it["passives"]
                      if p.get("name") and p["name"] not in covered]
    pact = (kit or {}).get("passive", {}).get("crimsonPact")
    sheet = lol_engine.resolve_sheet(champ_base(champ), level, entries, pact)
    # base (form-independent) range; the kit's passive may override it
    sheet["base_attack_range"] = champ["dd"]["stats"]["attackrange"]
    sheet.update(champion=champ["slug"], level=level, gold=gold, items=names,
                 uncovered=uncovered)
    return sheet


# ---------------------------------------------------------------------------
# combat engine: deterministic expected-value damage timeline
#
# The fight itself is engine/src/fight.rs; the rotation of each hand-encoded
# champion is a driver in engine/src/drivers.rs. Approximations (all chosen
# to preserve build RANKING, not absolute DPS):
# - crit is expected value, no RNG anywhere
# - projectiles land instantly; the target never moves or acts
# - every ability cast has a flat 0.25s lockout that delays the next auto
# - an attack reset (Kayle E) lands its empowered attack after one windup
# - stacking buffs (Zeal, Seething, Terminus) never expire mid-fight
# - range-scaled amps (Hexoptics) assume every attack is made at max range
# ---------------------------------------------------------------------------

def skill_ranks(level, max_order=("Q", "E", "W")):
    """Standard leveling: R at 6/11/16, otherwise the next point goes to the
    first ability in max_order below rank 5 and the level's rank ceiling."""
    ranks = {"Q": 0, "W": 0, "E": 0, "R": 0}
    for lv in range(1, level + 1):
        if lv in (6, 11, 16):
            ranks["R"] += 1
            continue
        for ab in max_order:
            if ranks[ab] < min(5, (lv + 1) // 2):
                ranks[ab] += 1
                break
    return ranks


SINGLETON_FX = (
    "spellblade", "asStacking", "phantom", "kraken", "altPen", "magicCrit",
    "ultBurn", "navoriCdr", "flurry", "executePct", "giantSlayer",
    "stormsurge", "abilityManaProc", "abilityProcOnce", "armorShred",
    "mrShred", "abilityAmpStacking", "manaActive", "onUltCast",
    "ultAttackSteroid", "hitPairProc", "nthHitProc", "hypershot",
    "openerLethality", "firstAttackBonus", "firstAttackCritFloorEv",
    "attackAmp")


def merge_effects(item_ids, effects):
    """A build's item effects as one dict (the engine's fx.rs merges the same
    way for the enumeration): list-valued effects concatenate in item order;
    same-named unique passives (two Spellblade items) grant only one instance
    in game, so the first one wins."""
    fx = {"onhit": [], "onhitCurrentHp": [], "dmgAmps": [], "flatAmps": [],
          "burns": [], "activesOnce": [], "energized": []}
    fx.update({k: None for k in SINGLETON_FX})
    for iid in item_ids:
        e = effects.get(iid, {})
        fx["onhit"] += e.get("onhit", [])
        for src, dst in (("onhitCurrentHp", "onhitCurrentHp"),
                         ("dmgAmp", "dmgAmps"), ("flatAmp", "flatAmps"),
                         ("burn", "burns"), ("activeOnce", "activesOnce"),
                         ("energized", "energized")):
            if src in e:
                fx[dst].append(e[src])
        for k in SINGLETON_FX:
            if k in e and fx[k] is None:
                fx[k] = e[k]
    return fx


# the champions the engine has a driver for (engine/src/drivers.rs)
KIT_DRIVERS = tuple(lol_engine.DRIVERS)


def simulate(sheet, kit, fx, level, ranks, target_hp, target_armor, target_mr,
             duration, use_ult=True, prestacked=False, target_bonus_hp=0.0,
             stop_after=INF, breakdown=True, _blend=True):
    """One fight vs a stat dummy. Returns totals, DPS, time-to-kill (None if
    the dummy survives), and a per-source damage breakdown (empty unless
    `breakdown`: the enumerator skips it and fills it in for the rows that
    place) — or None if the clock passed `stop_after` with the dummy still
    standing (the enumerator stops fights whose outcome can no longer
    matter). `ttk_exp` blends an execute's deterministic-crit timeline with
    the one real crit would miss; `_blend=False` skips that second pass."""
    return lol_engine.simulate(sheet, kit, fx, level, ranks, target_hp,
                               target_armor, target_mr, duration, use_ult,
                               prestacked, target_bonus_hp, stop_after,
                               breakdown, _blend)


# ---------------------------------------------------------------------------
# web API: the preset scenarios the dashboard shows
# ---------------------------------------------------------------------------

# A tier is one level/budget preset (a budget caps the build's gold and lets
# it be smaller than six items). Its targets are simulated together: one
# enumeration pass scores every build against each of them, which fills the
# per-target cells and — at no extra cost — the tier's "overall" cell, which
# ranks every build on all of the targets at once (see overall_key). Only the
# full-build tier ships; the mid-game and first-item presets were dropped.
# targetBonusHp: the item/rune share of the dummy's HP (drives Giant Slayer)
SCENARIOS = {
    "full-squishy": dict(label="Full build vs squishy", tier="full",
                         target="squishy", level=16, targetHp=2800, armor=110,
                         mr=60, duration=8, targetBonusHp=800),
    "full-bruiser": dict(label="Full build vs bruiser", tier="full",
                         target="bruiser", level=16, targetHp=3800, armor=180,
                         mr=120, duration=12, targetBonusHp=1500),
    "full-tank": dict(label="Full build vs tank", tier="full", target="tank",
                      level=16, targetHp=4800, armor=220, mr=160, duration=15,
                      targetBonusHp=1500),
    "full-overall": dict(label="Full build — overall", tier="full",
                         overall=True, level=16),
}


def tier_scenarios(tier):
    """A tier's scenario keys in SCENARIOS order: its targets, then overall."""
    return [k for k, sc in SCENARIOS.items() if sc["tier"] == tier]


def tier_targets(tier):
    """The scenario keys a tier's builds are simulated against."""
    return [k for k in tier_scenarios(tier) if not SCENARIOS[k].get("overall")]


def tiers():
    """Tier keys cheapest first: budget presets before full builds (they
    reject nearly every combination before simulating it), shorter total
    fight length first."""
    def cost(tier):
        ts = tier_targets(tier)
        return (SCENARIOS[ts[0]].get("budget") is None,
                sum(SCENARIOS[k]["duration"] for k in ts))
    return sorted(dict.fromkeys(sc["tier"] for sc in SCENARIOS.values()),
                  key=cost)


def kit_champions():
    """Slugs with a hand-encoded kit (data/builds/<slug>.json) and the
    rotation logic that drives it (KIT_DRIVERS)."""
    slugs = []
    if os.path.isdir(BUILDS_DATA_DIR):
        for fn in sorted(os.listdir(BUILDS_DATA_DIR)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(BUILDS_DATA_DIR, fn)) as f:
                if "abilities" not in json.load(f):
                    continue
            if fn[:-5] not in KIT_DRIVERS:
                print(f"Warning: data/builds/{fn} has no engine driver — add "
                      f"one in engine/src/drivers.rs to simulate it", file=sys.stderr)
                continue
            slugs.append(fn[:-5])
    return slugs


def champion_pool(kit, effects):
    """The enumeration pool for one champion: DEFAULT_POOL minus the items
    the kit can't use — mana-scaling items on a manaless champion (Tear
    stacks by spending mana, which Vladimir never does)."""
    return [i for i in DEFAULT_POOL
            if not (kit.get("manaless") and effects.get(i, {}).get("needsMana"))]


def api_builds_meta():
    patch, pool = load_items()
    effects = load_item_effects()
    champs = []
    for slug in kit_champions():
        kit = load_kit(slug)
        ids = champion_pool(kit, effects)
        name = kit.get("name", slug)
        dropped = [pool[i]["name"] for i in DEFAULT_POOL
                   if i not in ids and i in pool]
        notes = []
        if kit.get("attack", {}).get("never"):
            notes.append(f"{name} never auto-attacks in this model, as played: "
                         "on-hit, crit, energized and spellblade passives never "
                         "fire, so those items rank on their raw stats alone.")
        champs.append({
            "slug": slug, "name": name, "kitPatch": kit.get("patch"),
            "pool": ids,
            "excluded": [f"{', '.join(dropped)} — need mana to stack or "
                         f"scale; {name} has none"] if dropped else [],
            "notes": notes,
        })

    def entry(iid):
        return {"id": iid, "name": pool[iid]["name"],
                "gold": pool[iid]["shop"]["prices"]["total"]}

    excluded = []
    if os.path.exists(ITEM_EFFECTS_PATH):
        with open(ITEM_EFFECTS_PATH) as f:
            excluded = sorted((json.load(f).get("excluded") or {}).values())
    # items gold alone can't buy (Feats of Strength boots, support quest
    # line) are excluded by the data itself, not by hand
    gated = load_gated_items()
    by_currency = {}
    for iid, cur in gated.items():
        if iid in pool:
            by_currency.setdefault(cur, []).append(pool[iid]["name"])
    for cur, names in sorted(by_currency.items()):
        excluded.append(f"{', '.join(sorted(names))} — needs {cur}, "
                        f"not buyable with gold alone")
    retired = sorted(pool[i]["name"] for i in load_retired_items() if i in pool)
    if retired:
        excluded.append(f"{', '.join(retired)} — pulled from the shop; "
                        f"ddragon still carries the data")
    return {
        "champions": champs,
        "scenarios": [{"key": k, **v} for k, v in SCENARIOS.items()],
        "pool": sorted((entry(i) for i in DEFAULT_POOL),
                       key=lambda e: e["name"]),
        "boots": [entry(i) for i in BOOTS],
        "excluded": excluded,
        "itemsPatch": patch,
        "note": "Theoretical damage model — deterministic sim, expected-value "
                "crit, damage only. Runes are not modeled yet.",
    }


# ---------------------------------------------------------------------------
# scenario results: precomputed, never simulated on request
#
# A cell is one (champion, scenario) pair. Its result lives in .cache/builds/
# under a name that hashes every input it depends on, so a change to code or
# data simply makes the cell cold — nothing is ever served stale. The
# dashboard only READS cells (cached_scenario); warm() computes the cold ones,
# one at a time, cheapest first, and `lol.py serve` runs it in the background
# whenever something is cold.
# ---------------------------------------------------------------------------

SCENARIO_CACHE_DIR = os.path.join(BASE_DIR, ".cache", "builds")
CACHED_ROWS = 500  # rows kept per cell (all the enumerator keeps)

# The code is part of every cache key: a result is only valid for the code
# that produced it — this module's source and the engine's (lol_engine bakes
# a hash of engine/src at build time). Hashed once at import, so a serve that
# keeps running after an edit still recognises the results matching the code
# it runs — and can report itself stale (source_stale) instead of chasing
# files it will never see.
def _code_hash(py_bytes, engine_hash):
    return hashlib.sha256(py_bytes + engine_hash.encode()).hexdigest()


with open(os.path.abspath(__file__), "rb") as _f:
    SOURCE_HASH = _code_hash(_f.read(), lol_engine.SOURCE_HASH)


def engine_source_hash():
    """The hash engine/build.rs stamps into the module, recomputed from the
    sources on disk: Cargo.toml and every file under src/, sorted, each as
    its path, a NUL, its length and its bytes."""
    root = ENGINE_DIR
    files = [os.path.join(root, "Cargo.toml")]
    for d, _, names in os.walk(os.path.join(root, "src")):
        files += [os.path.join(d, n) for n in names]
    h = hashlib.sha256()
    for path in sorted(files):
        with open(path, "rb") as f:
            data = f.read()
        h.update(os.path.relpath(path, root).encode())
        h.update(b"\0")
        h.update(len(data).to_bytes(8, "little"))
        h.update(data)
    return h.hexdigest()


def source_stale():
    """Whether the code on disk differs from what this process runs: builds.py
    edited, or engine/ edited without a rebuild (jobs/build-engine.sh)."""
    with open(os.path.abspath(__file__), "rb") as f:
        return _code_hash(f.read(), engine_source_hash()) != SOURCE_HASH


def cells():
    """Every (champion slug, scenario key) the dashboard shows, in warm
    order: tier by tier, cheapest first (a budget preset would reject nearly
    every combination before simulating it). A tier's cells are computed
    together — its targets share one enumeration pass — so one champion's
    cells of a tier sit side by side. The full-build tier ranks ~130M
    builds against each of its targets (three to five fights per item
    combination and target: boots that fight alike share one, and most
    fights stop early once the build can't place)."""
    champs = kit_champions()
    return [(slug, key) for tier in tiers() for slug in champs
            for key in tier_scenarios(tier)]


def cell_paths():
    """{(slug, key): cache path} for every cell. The path's hash covers every
    input a result depends on: this module's code, the whole tier (its cells
    come from one pass, and the overall cell depends on every target), the
    pool constants, the loaded item and champion data (content, not just
    patch labels, since the daily refresh rewrites a patch's snapshot in
    place), the kit encoding, the hand-curated item passives, and the
    item-bin rules that decide which builds are legal."""
    patch, pool = load_items()
    base = hashlib.sha256(SOURCE_HASH.encode())
    for path in (ITEM_EFFECTS_PATH, groups_path()):
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                base.update(f.read())
    base.update(json.dumps([patch, DEFAULT_POOL, BOOTS, pool],
                           sort_keys=True).encode())
    paths = {}
    for slug in kit_champions():
        h_champ = base.copy()
        with open(os.path.join(BUILDS_DATA_DIR, f"{slug}.json"), "rb") as f:
            h_champ.update(f.read())
        h_champ.update(json.dumps(load_champion(slug), sort_keys=True).encode())
        for tier in tiers():
            keys = tier_scenarios(tier)
            h = h_champ.copy()
            h.update(json.dumps([[k, SCENARIOS[k]] for k in keys],
                                sort_keys=True).encode())
            for key in keys:
                paths[(slug, key)] = os.path.join(
                    SCENARIO_CACHE_DIR,
                    f"{slug}-{key}-{h.hexdigest()[:16]}.json")
    return paths


def cell_ready():
    """{"slug/key": computed?} for every cell — the dashboard's status."""
    return {f"{slug}/{key}": os.path.exists(path)
            for (slug, key), path in cell_paths().items()}


def cached_scenario(slug, key, paths=None):
    """A cell's precomputed payload, or None if it isn't computed for the
    current code and data. Never computes — that is warm()'s job."""
    paths = paths if paths is not None else cell_paths()
    if (slug, key) not in paths:
        raise ValueError(f"unknown champion or scenario '{slug}/{key}'")
    path = paths[(slug, key)]
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def kill_time(r, duration):
    """The expected kill time of one fight, extended past its end for a dummy
    that survived: the time the fight's average DPS would have needed. None
    if the build dealt no damage at all."""
    if r["ttk"] is not None:
        return r["ttk_exp"]
    return duration + r["hp_left"] / r["dps"] if r["dps"] > 0 else None


def rank_key(r):
    """Sort key of one fight, best first. Rank on the EXPECTED kill time
    (real time, plus the charge-back for an execute that deterministic crit
    guarantees but real crit would not). The interpolated time breaks ties
    inside an attack tick, ordering builds that land on the same attack by
    damage to spare. Builds that never kill come last, most damage first."""
    return ((0, r["ttk_exp"], r["ttk_eff"]) if r["ttk"] is not None
            else (1, INF, -r["total"]))


def geo_mean(xs):
    """exp(mean(log x)), floored at 1e-9 — the engine's, so the workers'
    ranking keys and the parent's are the same bits (its fsum is a port of
    math.fsum)."""
    return lol_engine.geo_mean(list(xs))


def overall_key(rs, targets):
    """Sort key across every target of a tier, best first: builds that kill
    all of them come before builds that leave one standing; within that, the
    geometric mean of the kill times (kill_time, so a survived fight counts
    as the time it would have needed). The geometric mean weights each
    target equally in percentage terms — 10% slower on the tank and 10%
    faster on the squishy is level pegging — and ranking by it is the same
    as ranking by the mean regret against each target's own best build, so
    no per-target optimum is needed to compute it. The interpolated times
    break ties the same way, so builds landing on the same attack tick
    everywhere are ordered by damage to spare."""
    unkilled = sum(rs[k]["ttk"] is None for k in targets)
    times = [kill_time(rs[k], targets[k]["duration"]) for k in targets]
    if None in times:
        return (unkilled, INF, INF)
    effs = [rs[k]["ttk_eff"] if rs[k]["ttk"] is not None else t
            for k, t in zip(targets, times)]
    return (unkilled, geo_mean(times), geo_mean(effs))


def _fight_row(r, duration, best):
    """One fight's numbers for a row's `vs` map. `loss` is the kill time as a
    multiple of the target's best build's (1.0 = as good as it gets)."""
    kt = kill_time(r, duration)
    return {
        "ttk": round(r["ttk"], 2) if r["ttk"] is not None else None,
        "ttkExp": round(r["ttk_exp"], 2) if r["ttk_exp"] is not None else None,
        "killTime": round(kt, 2) if kt is not None else None,
        "loss": round(kt / best, 4) if kt is not None and best else None,
        "dps": round(r["dps"]), "total": round(r["total"]),
        "attacks": r["attacks"],
        "breakdown": {k: round(v) for k, v in r["breakdown"].items()},
    }


def cached_builds(slug, pool):
    """Every build on the champion's cached cells, whatever code and data
    they were computed for, as id lists — the seeds of the next pass:
    yesterday's winners are still good builds, and scoring them first
    gives the enumerator tight pruning bounds from the outset."""
    by_name = {it["name"]: i for i, it in pool.items()}
    seen, out = set(), []
    for path in sorted(glob.glob(os.path.join(SCENARIO_CACHE_DIR,
                                              f"{slug}-*.json"))):
        try:
            with open(path) as f:
                rows = json.load(f).get("rows", [])
        except (OSError, ValueError):
            continue
        for row in rows:
            ids = [by_name.get(name) for name in row.get("items", [])]
            if None in ids or tuple(ids) in seen:
                continue
            seen.add(tuple(ids))
            out.append(ids)
    return out


def compute_tier(slug, tier, paths, log=None):
    """Simulate one champion's tier — every build against each of its
    targets, in one pass — and write every cell of the tier to its path in
    `paths` (cell_paths()): through a temp file and rename, so a killed run
    can never leave a truncated file, then retiring the cell's older
    generations. Returns {key: payload}. Every row carries the build's fight
    against every target of the tier under `vs`; per-target cells also keep
    their own fight's numbers flat, and the overall cell adds `mean` (the
    geometric mean it is ranked by) and `kills`."""
    import time
    t0 = time.time()
    keys = tier_scenarios(tier)
    targets = {k: SCENARIOS[k] for k in tier_targets(tier)}
    overall = next((k for k in keys if SCENARIOS[k].get("overall")), None)
    level, budget = SCENARIOS[keys[0]]["level"], SCENARIOS[keys[0]].get("budget")
    champ = load_champion(slug)
    patch, pool = load_items()
    effects = load_item_effects()
    kit = load_kit(slug)
    ranks = skill_ranks(level, kit_max_order(kit))
    lists, count = enumerate_builds(
        champ, pool, effects, kit, level, ranks, targets, budget=budget,
        candidates=champion_pool(kit, effects), overall=overall,
        seeds=cached_builds(slug, pool), log=log)
    secs = round(time.time() - t0, 1)
    # each target's best kill time, for the loss column: the top of the
    # target's own list is its fastest kill whenever anything kills at all
    best = {}
    for k, t in targets.items():
        times = [kill_time(rs[k], t["duration"])
                 for lst in lists.values() for _, _, rs in lst]
        times = [x for x in times if x is not None]
        best[k] = min(times) if times else None
    target_meta = [{"key": k, **t} for k, t in targets.items()]
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    outs = {}
    for key in keys:
        sc = SCENARIOS[key]
        rows = []
        for n, (ids, sheet, rs) in enumerate(lists[key][:CACHED_ROWS], 1):
            row = {"rank": n, "items": [pool[i]["name"] for i in ids],
                   "gold": sheet["gold"],
                   "ap": round(sheet["ap"]), "ad": round(sheet["ad"]),
                   "attackSpeed": round(sheet["attack_speed"], 2),
                   "vs": {t["target"]: _fight_row(rs[k], t["duration"], best[k])
                          for k, t in targets.items()}}
            if sc.get("overall"):
                times = [kill_time(rs[k], t["duration"])
                         for k, t in targets.items()]
                row["kills"] = sum(rs[k]["ttk"] is not None for k in targets)
                row["mean"] = (round(geo_mean(times), 2)
                               if None not in times else None)
            else:
                r = rs[key]
                row.update({
                    "ttk": round(r["ttk"], 2) if r["ttk"] is not None else None,
                    "ttkExp": (round(r["ttk_exp"], 2)
                               if r["ttk_exp"] is not None else None),
                    "ttkEff": (round(r["ttk_eff"], 3)
                               if r["ttk_eff"] is not None else None),
                    "dps": round(r["dps"]), "total": round(r["total"]),
                    "attacks": r["attacks"],
                    "breakdown": {s: round(v) for s, v in r["breakdown"].items()},
                })
            rows.append(row)
        outs[key] = {
            "champion": slug, "championName": kit.get("name", slug),
            "scenario": {"key": key, **sc, "targets": target_meta},
            "itemsPatch": patch, "championPatch": champ["meta"]["patch"],
            "kitPatch": kit.get("patch"), "buildsEvaluated": count,
            "ranks": ranks, "rows": rows,
            "computedAt": when, "computeSeconds": secs}
    os.makedirs(SCENARIO_CACHE_DIR, exist_ok=True)
    for key, out in outs.items():
        path = paths[(slug, key)]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f, separators=(",", ":"))
        os.replace(tmp, path)
        for old in glob.glob(os.path.join(SCENARIO_CACHE_DIR,
                                          f"{slug}-{key}-*.json")):
            if old != path:
                os.remove(old)
    return outs


def compute_scenario(slug, key, paths):
    """One cell's payload — computing its whole tier, since a tier's cells
    come from one pass; the siblings are written too. See compute_tier."""
    return compute_tier(slug, SCENARIOS[key]["tier"], paths)[key]


def warm_lock():
    """Take the cache-wide warm lock, or return None if another process holds
    it. One warmer at a time: a single 15-worker pool already saturates the
    machine, and two would do the same work twice for the same files."""
    import fcntl
    os.makedirs(SCENARIO_CACHE_DIR, exist_ok=True)
    f = open(os.path.join(SCENARIO_CACHE_DIR, "lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def warm_running():
    """Whether some process currently holds the warm lock."""
    lock = warm_lock()
    if lock is None:
        return True
    lock.close()
    return False


def _say(line):
    print(line, flush=True)


def warm(log=_say):
    """Compute every cold cell, cheapest first — a tier at a time, since a
    tier's cells come from one pass. Returns how many cells were computed,
    or None if another warmer holds the lock. Results are keyed to the code
    this process runs (SOURCE_HASH), so an edit to builds.py mid-run doesn't
    disturb it: a serve running that same code keeps a complete matrix, and
    the next serve recomputes under the new hash."""
    import signal
    lock = warm_lock()
    if lock is None:
        return None
    # SIGTERM (pkill, a serve shutting down) would otherwise end the process
    # without unwinding the `with Pool` block, orphaning the workers mid-run.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        for tmp in glob.glob(os.path.join(SCENARIO_CACHE_DIR, "*.tmp")):
            os.remove(tmp)
        paths = cell_paths()
        # cells of a scenario or champion that no longer exists would linger
        # forever (nothing recomputes them); older generations of live cells
        # are left to compute_tier, so a serve on older code keeps serving
        # them until their replacement lands
        for path in glob.glob(os.path.join(SCENARIO_CACHE_DIR, "*.json")):
            m = re.fullmatch(r"([a-z0-9]+)-(.+)-[0-9a-f]{16}\.json",
                             os.path.basename(path))
            if m and (m.group(1), m.group(2)) not in paths:
                os.remove(path)
        champs = kit_champions()
        cold = [(slug, tier) for tier in tiers() for slug in champs
                if any(not os.path.exists(paths[(slug, k)])
                       for k in tier_scenarios(tier))]
        done = 0
        for n, (slug, tier) in enumerate(cold, 1):
            keys = tier_scenarios(tier)
            log(f"[{n}/{len(cold)}] {slug}/{tier} ({', '.join(keys)}) …")
            outs = compute_tier(slug, tier, paths, log=log)
            first = outs[keys[0]]
            log(f"  {first['buildsEvaluated']:,} builds in "
                f"{first['computeSeconds']}s")
            done += len(outs)
        return done
    finally:
        lock.close()


def cmd_warm(args):
    n = warm()
    if n is None:
        print("Another warm is already running (it holds .cache/builds/lock).")
    elif n == 0:
        print("Nothing to do — every scenario is computed for the current "
              "code and data.")
    else:
        print(f"Computed {n} scenario(s).")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_fetch_champion(args):
    versions = items.fetch_json(items.DDRAGON_VERSIONS)
    version = (items.resolve_version(versions, args.version)
               if args.version else versions[0])
    patch = items.short_patch(version)
    slug = norm_name(args.name)

    out_dir = os.path.join(CHAMPIONS_DIR, patch, slug)
    meta_path = os.path.join(out_dir, "meta.json")
    if os.path.exists(meta_path) and not args.force:
        with open(meta_path) as f:
            meta = json.load(f)
        print(f"data/builds/champions/{patch}/{slug} already fetched "
              f"({meta['fetchedAt']}). Use --force to refresh.")
        return

    index = items.fetch_json(DDRAGON_CHAMP_INDEX.format(version=version))["data"]
    by_key = {}
    for cid, c in index.items():
        by_key[cid.lower()] = cid          # MonkeyKing
        by_key[norm_name(c["name"])] = cid  # wukong
    cid = by_key.get(slug)
    if not cid:
        sys.exit(f"No champion matches '{args.name}' in ddragon {version}.")

    dd = items.fetch_json(DDRAGON_CHAMP.format(version=version, cid=cid))["data"][cid]
    mk, mk_note = None, None
    try:
        mk = items.fetch_json(MERAKI_CHAMP.format(cid=cid))
    except Exception as e:
        mk_note = f"meraki fetch failed: {e} (stat sheets fall back to base AS as ratio)"
        print(f"Warning: {mk_note}")

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "ddragon.json"), "w") as f:
        json.dump(dd, f, separators=(",", ":"))
    if mk:
        with open(os.path.join(out_dir, "meraki.json"), "w") as f:
            json.dump(mk, f, separators=(",", ":"))
    meta = {
        "slug": slug, "championId": cid, "patch": patch,
        "ddragonVersion": version,
        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "merakiPatchLastChanged": mk.get("patchLastChanged") if mk else None,
    }
    if mk_note:
        meta["note"] = mk_note
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"data/builds/champions/{patch}/{slug}: ddragon {version}"
          + (f", meraki (last changed {meta['merakiPatchLastChanged']})" if mk else ""))
    if mk and mk.get("patchLastChanged") != patch:
        print(f"  Note: meraki's champion data is from patch "
              f"{mk['patchLastChanged']}; ddragon is used for base stats, "
              f"meraki only for AS ratio / windup / ability text.")
    print("Commit data/builds/ to archive this snapshot.")


def cmd_stats(args):
    slug = norm_name(args.name)
    champ = load_champion(slug, args.patch)
    patch, pool = load_items(args.patch)
    idx = item_index(pool)
    ids = [resolve_item(pool, idx, t) for t in args.items]
    s = resolve_stats(champ, args.level, ids, pool, kit=load_kit(slug, required=False))

    print(f"{champ['dd']['name']} — level {s['level']}, items patch {patch} "
          f"(champion: {champ['meta']['patch']})")
    if s["items"]:
        print(f"Items ({s['gold']}g): {', '.join(s['items'])}")
    print()
    rows = [
        ("Attack damage", f"{s['ad']:.1f}",
         f"{s['ad_base']:.1f} base + {s['ad_bonus']:.0f} item"),
        ("Ability power", f"{s['ap']:.1f}",
         f"{s['ap_flat']:.0f} flat x {s['ap_mult']:.2f}" if s["ap_mult"] > 1 else ""),
        ("Attack speed", f"{s['attack_speed']:.3f}",
         f"{s['base_as']:.3f} + {s['as_ratio']:.3f} ratio x "
         f"{s['bonus_as_pct']:.1f}% bonus (cap {AS_CAP})"),
        ("Crit", f"{s['crit_chance']:.0f}%", f"{s['crit_damage']:.0f}% damage"),
        ("Ability haste", f"{s['haste']:.0f}",
         f"cooldowns x {s['cd_mult']:.3f}"),
        ("Magic pen", f"{s['magic_pen_flat']:.0f} | {s['magic_pen_pct']:.0f}%",
         "flat | percent"),
        ("Armor pen", f"{s['lethality']:.0f} | {s['armor_pen_pct']:.0f}%",
         "lethality | percent"),
        ("Health", f"{s['hp']:.0f}", ""),
        ("Mana", f"{s['mana']:.0f}", ""),
        ("Armor / MR", f"{s['armor']:.1f} / {s['mr']:.1f}", ""),
        ("Move speed", f"{s['move_speed']:.0f}", ""),
        ("Vamp", f"{s['lifesteal']:.0f}% LS / {s['omnivamp']:.0f}% omni", ""),
    ]
    for label, val, note in rows:
        print(f"  {label:<14} {val:>16}  {note}")
    if s["uncovered"]:
        print("\nNot in these numbers (combat-engine territory):")
        for u in s["uncovered"]:
            print(f"  - {u}")


def sim_setup(args):
    """Shared by sim and (soon) optimize: champion, pool, sheet, kit, fx."""
    slug = norm_name(args.name)
    champ = load_champion(slug, args.patch)
    patch, pool = load_items(args.patch)
    idx = item_index(pool)
    ids = [resolve_item(pool, idx, t) for t in args.items]
    groups, caps = load_exclusive_groups(args.patch)
    by_group = {}
    for i in ids:
        for g in groups.get(i, ()):
            by_group.setdefault(g, []).append(pool[i]["name"])
    for g, members in by_group.items():
        cap = caps.get(g, 99)
        if len(members) > cap:
            print(f"Warning: {' + '.join(members)} — the game limits you to "
                  f"{cap} {group_name(g)} item(s); this build is not buyable.",
                  file=sys.stderr)
    effects = load_item_effects()
    kit = load_kit(slug)
    sheet = resolve_stats(champ, args.level, ids, pool, effects, kit=kit)
    return champ, patch, pool, ids, effects, sheet, kit


def cmd_sim(args):
    champ, patch, pool, ids, effects, sheet, kit = sim_setup(args)
    ranks = skill_ranks(args.level, kit_max_order(kit, args.max_order))
    fx = merge_effects(ids, effects)
    r = simulate(sheet, kit, fx, args.level, ranks,
                 args.target_hp, args.armor, args.mr, args.duration,
                 use_ult=not args.no_ult, prestacked=args.prestacked,
                 target_bonus_hp=args.target_bonus_hp)

    print(f"{champ['dd']['name']} lvl {args.level} "
          f"(Q{ranks['Q']} W{ranks['W']} E{ranks['E']} R{ranks['R']}) — "
          f"{args.duration:g}s vs {args.target_hp}hp {args.armor}armor {args.mr}mr")
    if sheet["items"]:
        print(f"Items ({sheet['gold']}g): {', '.join(sheet['items'])}")
    print()
    dead = f"target dies at {r['ttk']:.2f}s" if r["ttk"] is not None else \
           f"target survives with {r['hp_left']:.0f} hp"
    print(f"  {r['total']:.0f} damage, {r['dps']:.0f} DPS — {dead}")
    print(f"  {r['attacks']} attacks"
          + (f", {r['phantom_hits']} phantom hits" if r["phantom_hits"] else ""))
    print()
    for src, dmg in r["breakdown"].items():
        bar = "#" * int(round(dmg / r["total"] * 40))
        print(f"  {src:<12} {dmg:>8.0f}  {dmg / r['total'] * 100:>5.1f}%  {bar}")
    if sheet["uncovered"]:
        print("\nNot modeled: " + "; ".join(sheet["uncovered"]))


# The enumeration pool: every damage item whose effects the engine models
# (or that is pure stats). Items with UNMODELED damage passives are
# deliberately absent — including them would rank them on stats alone and
# misrank them downward. Each exclusion and its reason is documented in
# data/builds/item-effects.json under "excluded".
DEFAULT_POOL = [
    # --- mage ---
    3115,  # Nashor's Tooth
    3089,  # Rabadon's Deathcap
    3135,  # Void Staff
    4645,  # Shadowflame
    3100,  # Lich Bane
    4633,  # Riftmaker
    6653,  # Liandry's Torment
    3118,  # Malignance
    4629,  # Cosmic Drive
    3116,  # Rylai's Crystal Scepter (slow not modeled; damage-irrelevant)
    2503,  # Blackfire Torch
    4646,  # Stormsurge
    2510,  # Dusk and Dawn
    6655,  # Luden's Echo
    3040,  # Seraph's Embrace (fully-stacked Archangel's)
    3157,  # Zhonya's Hourglass
    3102,  # Banshee's Veil
    3165,  # Morellonomicon
    3137,  # Cryptbloom
    4628,  # Horizon Focus
    6657,  # Rod of Ages (assumed fully stacked)
    3041,  # Mejai's Soulstealer (assumed zero stacks)
    8010,  # Bloodletter's Curse
    3152,  # Hextech Rocketbelt
    3146,  # Hextech Gunblade
    2522,  # Actualizer
    8020,  # Abyssal Mask (12% magic amp outweighs its tank statline sometimes)
    # --- on-hit / marksman ---
    3124,  # Guinsoo's Rageblade
    3091,  # Wit's End
    3302,  # Terminus
    6672,  # Kraken Slayer
    3153,  # Blade of the Ruined King
    3031,  # Infinity Edge
    3046,  # Phantom Dancer
    6675,  # Navori Flickerblade
    3032,  # Yun Tal Wildarrows
    6676,  # The Collector
    3036,  # Lord Dominik's Regards
    3085,  # Runaan's Hurricane (single-target: stats only)
    3033,  # Mortal Reminder
    3094,  # Rapid Firecannon
    2523,  # Hexoptics C44 (max-range assumption; see item-effects note)
    3095,  # Stormrazor (was 3097 until 16.16; Riot reissued the id in 16.17)
    3087,  # Statikk Shiv
    3072,  # Bloodthirster
    3508,  # Essence Reaver
    6673,  # Immortal Shieldbow
    3139,  # Mercurial Scimitar
    2512,  # Fiendhunter Bolts
    # --- assassin ---
    3142,  # Youmuu's Ghostblade
    6697,  # Hubris (assumed zero stacks)
    6698,  # Profane Hydra
    6699,  # Voltaic Cyclosword
    6696,  # Axiom Arc
    3814,  # Edge of Night
    6695,  # Serpent's Fang
    3179,  # Umbral Glaive
    6692,  # Eclipse
    6694,  # Serylda's Grudge
    # --- bruiser ---
    3042,  # Muramana (fully-stacked Manamune)
    3071,  # Black Cleaver
    3161,  # Spear of Shojin
    6610,  # Sundered Sky
    6631,  # Stridebreaker
    3074,  # Ravenous Hydra
    3748,  # Titanic Hydra
    6333,  # Death's Dance
    3156,  # Maw of Malmortius
    3073,  # Experimental Hexplate
    2501,  # Overlord's Bloodmail
    3078,  # Trinity Force
    6662,  # Iceborn Gauntlet
    3181,  # Hullbreaker
    6609,  # Chempunk Chainsword
    2517,  # Endless Hunger
    3026,  # Guardian Angel
]
# Every tier-2 boots gold can buy: Berserker's Greaves, Sorcerer's Shoes,
# Ionian Boots of Lucidity, Boots of Swiftness, Mercury's Treads, Plated
# Steelcaps, Gluttonous Greaves. Tier-3 upgrades (Spellslinger's et al) are
# Feats-of-Strength-gated, so they're out by assumption — see "excluded" in
# item-effects.json. Boots that differ only in stats the engine never reads
# share one simulation (boots_classes), so the defensive pairs cost little.
BOOTS = [3006, 3020, 3158, 3009, 3111, 3047, 3008]


_ENUM_CTX = None  # set before forking workers; children inherit via fork


# Stats the combat engine never reads from the attacker's own sheet: the
# fight is the same whatever they are. Move speed is NOT one of them —
# Energized items charge with movement.
ENGINE_IGNORES = frozenset({"armor", "mr", "tenacity", "lifesteal", "omnivamp",
                            "heal_shield_power"})
# item-effects.json fields that describe an entry rather than model anything
EFFECT_META = ("name", "covers", "note")


def boots_classes(pool, effects, boots=None, ignores=ENGINE_IGNORES):
    """Group boots whose damage-relevant stats and modeled effects are
    identical — a list of member lists, in BOOTS order. Stat resolution sums
    each item's contributions, so two such boots give the same sheet, and
    the same fight, alongside any other five items: the enumerator simulates
    each class once per item combination and ranks every member with the
    result (Mercury's Treads, Plated Steelcaps and Gluttonous Greaves are
    one class; Swiftness moves faster, Lucidity casts faster). `ignores`
    names the stats that don't tell boots apart (see boots_partitions)."""
    classes = {}
    for b in (BOOTS if boots is None else boots):
        stats = tuple(sorted(
            (key, val) for stat, fields in pool[b]["stats"].items()
            for field, val in fields.items() if val
            for key in [ITEM_STAT_MAP.get((stat, field))]
            if key and key not in ignores))
        modeled = {k: v for k, v in effects.get(b, {}).items()
                   if k not in EFFECT_META}
        sig = (stats, json.dumps(modeled, sort_keys=True))
        classes.setdefault(sig, []).append(b)
    return list(classes.values())


# Stats the engine reads only on a basic attack: attack speed, and move
# speed — Energized items charge with movement, and are spent by an attack.
ATTACK_ONLY_STATS = frozenset({"bonus_as_pct", "ms_flat", "ms_pct"})
MOVE_STATS = frozenset({"ms_flat", "ms_pct"})


def boots_partitions(pool, effects, kit):
    """{no Energized item in the build: boots classes} for one kit. Move
    speed is read only to charge Energized items, so in a build without one
    Swiftness fights exactly like the 45-speed boots; a kit that never
    attacks (Vladimir) never charges them and never reads attack speed
    either, so Berserker's joins them too, whatever the build."""
    never = bool(kit.get("attack", {}).get("never"))
    busy = ENGINE_IGNORES | (ATTACK_ONLY_STATS if never else frozenset())
    calm = ENGINE_IGNORES | (ATTACK_ONLY_STATS if never else MOVE_STATS)
    return {False: boots_classes(pool, effects, ignores=busy),
            True: boots_classes(pool, effects, ignores=calm)}


# ---------------------------------------------------------------------------
# enumeration: every build against every target of a tier, pruned exactly
#
# A result list keeps the best `keep` rows, so a build only matters while it
# can still beat the keep-th best. The parent publishes every list's current
# keep-th key in a small table of doubles (_Bounds) that forked workers read
# through shared memory; before each item combination a worker reads it and
# (a) drops results that cannot place, (b) stops a fight the moment its clock
# passes the point past which the build can place neither on that target's
# list nor on the overall list — a survivor ranks below every killer, and a
# kill still to come lands later than the clock. Both cuts only ever discard
# builds that lose to one already found, so the lists come out exactly as an
# unpruned pass ranks them. A stale read of a list's bound is only ever
# looser; the fastest-kill column can be stale-tighter, which the checked
# guess below (second_pass) repairs.
#
# The loop itself — the combinations of a block, each boots class's sheet,
# effects and fights, the cuts, the rows that could still place — is the
# engine's (engine/src/enumerate.rs, Ctx.run_block). This side splits the
# work into blocks, hands them to forked workers, merges what comes back and
# publishes the bounds.
# ---------------------------------------------------------------------------

# A result row is (sort key, ids, {target key: fight}). Rows are ordered by
# key, then by the build's place in the enumeration (items in pool order,
# then boots in BOOTS order) — a total order, so ties (boots of one class
# share a fight; on a fast kill every boots may) come out the same however
# the work is split into tasks and whichever order those finish in.
def _enum_order(free, required=()):
    """{item id: its place in the enumeration}, for ordering ties."""
    order = {b: i for i, b in enumerate(BOOTS)}
    for i, item in enumerate([*required, *free]):
        order.setdefault(item, len(BOOTS) + i)
    return order


def _place(order, ids):
    """A build's place in the enumeration: its items' places, then its
    boots'."""
    return ([order.get(i, i) for i in ids[1:]],
            order.get(ids[0], ids[0]) if ids else -1)


def _row_key(order):
    return lambda row: (row[0], _place(order, row[1]))


def _cut(lst, keep, order):
    """Sort a result list best-first and cut it to `keep`."""
    lst.sort(key=_row_key(order))
    del lst[keep:]


def _keep_best(lst, keep, order):
    """Bound a running result list: sort and cut it to `keep` once it has
    grown past 4x that, so memory stays flat over millions of builds."""
    if len(lst) > 4 * keep:
        _cut(lst, keep, order)


class _Bounds:
    """Every result list's keep-th best row, flattened into a table of
    doubles (a list, or a multiprocessing.RawArray the workers inherit).
    Per target key: [the latest expected kill time a killer may have (INF:
    any), the least total a survivor needs (-INF: any; INF: none), a lower
    bound on the fastest kill of that target by any build (0: unknown)].
    For the overall key: [the most survivors allowed, the largest geometric
    mean at that count, the keep-th row's ids] — builds that leave the same
    targets standing all tie on the overall key, so the ids that break ties
    travel too. A list that isn't full yet bounds nothing."""
    WIDTH = 8  # two values and a bound, or two values and up to six ids

    def __init__(self, keys, overall, table):
        self.keys, self.overall, self.table = list(keys), overall, table

    def reset(self):
        for i, k in enumerate(self.keys):
            base = i * self.WIDTH
            self.table[base] = INF
            self.table[base + 1] = INF if k == self.overall else -INF
            for j in range(2, self.WIDTH):
                self.table[base + j] = 0.0

    def update(self, lists, keep, min_kill):
        """Refresh from the parent's lists (each already cut to `keep`) and
        its {target key: lower bound on the fastest kill}. Workers read
        without a lock, so every value has to stay sound on its own and
        across a half-done update: each one only ever tightens, and the
        overall row's ids land before the values they break ties for (an
        older, looser pair of values with newer ids is still right)."""
        for i, k in enumerate(self.keys):
            lst, base = lists[k], i * self.WIDTH
            if k != self.overall:
                self.table[base + 2] = min_kill.get(k, 0.0)
            if len(lst) < keep:
                continue
            thr, ids = lst[keep - 1][0], lst[keep - 1][1]
            if k == self.overall:
                for j in range(6):
                    self.table[base + 2 + j] = float(ids[j]) if j < len(ids) else 0.0
                self.table[base], self.table[base + 1] = thr[0], thr[1]
            elif thr[0] == 0:  # a killer: no survivor can place
                self.table[base], self.table[base + 1] = thr[1], INF
            else:  # a survivor: every killer places, survivors need its total
                self.table[base], self.table[base + 1] = INF, -thr[2]

    def target(self, i):
        base = i * self.WIDTH
        return self.table[base], self.table[base + 1], self.table[base + 2]

    def overall_bound(self, i):
        base = i * self.WIDTH
        ids = [int(self.table[base + 2 + j]) for j in range(6)]
        return self.table[base], self.table[base + 1], [x for x in ids if x]


# (the slack on the kill-time cuts — a stopped fight must belong to a build
# worse than the keep-th best by more than rounding could account for — is
# PRUNE_SLACK in engine/src/enumerate.rs)
# the guess behind the overall list's bound: no build kills a target faster
# than this share of the fastest kill seen so far (checked at the end; a
# wrong guess costs a second pass, never a wrong result)
MIN_KILL_GUESS = 0.75
# pools with more builds than this fan out across CPU cores
FORK_ABOVE = 50_000


def _engine_ctx(ctx):
    """The engine's view of an enumeration: every input of the inner loop —
    the pool's stats, effects and prices, the kit, the targets, Riot's
    ownership groups, the boots classes, the enumeration order — parsed once
    into one lol_engine.Ctx that forked workers inherit."""
    pool, effects = ctx["pool"], ctx["effects"]
    ids = set(ctx["free"]) | set(ctx["required"]) | set(BOOTS)
    items = {i: (stat_pairs(pool[i]), effects.get(i, {}),
                 pool[i]["shop"]["prices"]["total"]) for i in ids}
    targets = [(k, t["targetHp"], t["armor"], t["mr"], t["duration"],
                t.get("targetBonusHp", 0.0)) for k, t in ctx["targets"].items()]
    return lol_engine.Ctx(
        champ_base(ctx["champ"]), ctx["level"], ctx["ranks"], ctx["kit"], items,
        {i: list(ctx["groups"].get(i, ())) for i in ids}, dict(ctx["caps"]),
        targets, ctx["overall"], ctx["keep"], ctx["budget"],
        list(ctx["required"]), list(ctx["free"]), list(BOOTS),
        (ctx["classes"][False], ctx["classes"][True]), sorted(ctx["energized"]),
        dict(ctx["order"]), ctx["use_ult"], ctx["prestacked"])


def _enum_task(ctx, task):
    """Simulate one task's builds and return ({key: rows}, ranked count) —
    the rows that could still place, given the bounds when they were
    scored. A task is ("block", size, prefix): every combination of `size`
    free items whose first indices are `prefix` (the rest enumerate in the
    engine); or ("builds", [ids, ...]): explicit builds, the seeds. Each
    boots class fights every target once and every member of the class is
    ranked with that fight (boots_partitions). The engine reads the bounds
    table live, through its address."""
    return ctx["engine"].run_block(task, ctypes.addressof(ctx["shared"]))


def _enum_worker(task):
    i, spec = task
    out, n = _enum_task(_ENUM_CTX, spec)
    return i, out, n


def _enum_init():
    if hasattr(os, "nice"):
        os.nice(5)


def _enum_blocks(n_free, sizes):
    """The enumeration in blocks, biggest first (the tail is then made of
    small ones, so the workers all finish at about the same time): the
    combinations of each size, split by their first two indices. Returns
    [(combinations in the block, ("block", size, prefix))]."""
    import itertools
    blocks = []
    for size in sizes:
        p = min(2, size)
        for prefix in itertools.combinations(range(n_free), p):
            tail = n_free - (prefix[-1] + 1 if prefix else 0)
            count = math.comb(tail, size - p)
            if count:
                blocks.append((count, ("block", size, prefix)))
    blocks.sort(key=lambda b: -b[0])
    return blocks


def enumerate_builds(champ, pool, effects, kit, level, ranks, targets,
                     budget=None, slots=6, required=(), candidates=None,
                     use_ult=True, prestacked=False, keep=500, overall=None,
                     seeds=(), log=None, workers=None):
    """Rank every boots + item combination against each target in one pass
    — `targets` is {key: scenario-shaped dict: targetHp, armor, mr,
    duration, targetBonusHp}. Returns ({key: results}, count): per target
    the top-`keep` (ids, sheet, {target key: fight result}) best-first by
    rank_key, plus, with `overall` set, a list under that key ranked by
    overall_key across every target; and how many builds were ranked (boots
    of one class share a simulation, see boots_classes). Large pools fan
    out across CPU cores (fork) in blocks handed out as workers free up,
    pruned exactly as the lists fill (see _Bounds). `seeds` are builds to
    score before the rest — an earlier result's rows — so the pruning bounds
    are tight from the start. `log` gets a progress line now and then.

    The overall list's bound leans on a guess: that no build kills a target
    faster than three quarters of the fastest kill seen so far. A faster
    kill exposes the guess the moment it lands (a cut fight was never the
    fastest, so the fastest seen is the fastest there is); the blocks that
    could have used the stale guess are then scored again at the end with
    the true minimum — exact either way, only slower."""
    import time
    free = [i for i in (candidates or DEFAULT_POOL) if i not in required]
    n_free = slots - 1 - len(required)  # one slot is always boots
    if n_free < 0:
        sys.exit("more required items than slots allow")
    sizes = list(range(n_free + 1)) if budget else [n_free]
    _groups, _caps = load_exclusive_groups()
    keys = list(targets) + ([overall] if overall else [])
    ctx = dict(
        champ=champ, pool=pool, effects=effects, kit=kit, level=level,
        ranks=ranks, targets=dict(targets), overall=overall, budget=budget,
        required=list(required), free=free, use_ult=use_ult,
        prestacked=prestacked, keep=keep, groups=_groups, caps=_caps,
        classes=boots_partitions(pool, effects, kit),
        order=_enum_order(free, required),
        energized=frozenset(i for i in pool if "energized" in effects.get(i, {})),
        shared=None)
    blocks = _enum_blocks(len(free), sizes)
    total = sum(count for count, _ in blocks)
    combos = len(BOOTS) * total
    pool_ids = set(free) | set(required)
    seeds = list(dict.fromkeys(
        tuple(s) for s in seeds
        if len(set(s)) == len(s) and s[0] in BOOTS
        and set(required) <= set(s[1:]) <= pool_ids
        and len(s) - 1 - len(required) in sizes))
    seeds = [list(s) for s in seeds]
    specs = ([("builds", seeds)] if seeds else []) + [b for _, b in blocks]
    tasks = list(enumerate(specs))
    workers = workers or max(1, os.cpu_count() or 2)
    lists, count = {k: [] for k in keys}, 0
    have = {k: set() for k in keys}  # ids on each list: seeds come round twice
    done, t0, t_log = 0, time.time(), time.time()
    min_kill = {}  # {target key: lower bound on the fastest kill}
    used = {}  # the largest bound each fight was ever cut with
    guessing = True  # a second pass runs with the true minima instead
    finished = 0  # tasks merged so far
    suspect = 0  # tasks (in order) that may have used a wrong guess

    def merge(i, out, n):
        nonlocal count, done, t_log, finished, suspect
        spec = specs[i]
        for k in keys:
            lst, seen, new = lists[k], have[k], []
            for row in out[k]:
                ids = tuple(row[1])
                if ids not in seen:
                    seen.add(ids)
                    new.append(row)
            if new:
                lst += new
                _cut(lst, keep, ctx["order"])
                have[k] = {tuple(row[1]) for row in lst}
        finished += 1
        if guessing:
            for k in targets:
                if lists[k] and lists[k][0][0][0] == 0:
                    best = lists[k][0][0][1]
                    if best < used.get(k, 0.0):
                        # a kill faster than the guess allowed: every task
                        # dispatched so far — those done, and up to one per
                        # worker still running — may have cut a build that
                        # belongs on the overall list
                        suspect = max(suspect, finished + workers)
                        used[k] = 0.0
                    min_kill[k] = MIN_KILL_GUESS * best
                    used[k] = max(used.get(k, 0.0), min_kill[k])
        bounds.update(lists, keep, min_kill)
        if spec[0] == "block" and guessing:
            count += n
            _, size, prefix = spec
            tail = len(free) - (prefix[-1] + 1 if prefix else 0)
            done += math.comb(tail, size - len(prefix))
        if log and (time.time() - t_log >= 30 or done == total):
            t_log = time.time()
            secs = t_log - t0
            eta = secs / done * (total - done) if done else 0.0
            log(f"  {done / total * 100:5.1f}%  {done * len(BOOTS):,} of "
                f"{combos:,} builds, {secs:,.0f}s in, ~{eta:,.0f}s to go")

    def second_pass():
        """The tasks a wrong guess may have spoiled, with the true minima."""
        nonlocal guessing
        if not suspect:
            return []
        guessing = False
        min_kill.clear()
        min_kill.update({k: lists[k][0][0][1] for k in targets
                         if lists[k] and lists[k][0][0][0] == 0})
        bounds.update(lists, keep, min_kill)
        again = tasks[:suspect]
        if log:
            log(f"  a build killed faster than assumed: scoring "
                f"{len(again)} of {len(tasks)} blocks again with the exact bound")
        return again

    import multiprocessing as mp
    # the bounds table is shared memory either way: the engine reads it
    # through its address, so a forked worker sees every tightening live
    ctx["shared"] = mp.RawArray("d", _Bounds.WIDTH * len(keys))
    bounds = _Bounds(keys, overall, ctx["shared"])
    bounds.reset()
    ctx["engine"] = _engine_ctx(ctx)
    if combos > FORK_ABOVE and workers > 1 and hasattr(os, "fork"):
        global _ENUM_CTX
        _ENUM_CTX = ctx
        try:
            # workers on every core, a little nicer than a serve on the same
            # box; the parent only merges
            with mp.get_context("fork").Pool(workers, _enum_init) as p:
                for i, out, n in p.imap_unordered(_enum_worker, tasks):
                    merge(i, out, n)
                for i, out, n in p.imap_unordered(_enum_worker, second_pass()):
                    merge(i, out, n)
        finally:
            _ENUM_CTX = None
    else:
        for i, spec in tasks:
            merge(i, *_enum_task(ctx, spec))
        for i, spec in second_pass():
            merge(i, *_enum_task(ctx, spec))
    # The rows that made it get their sheets and their fights in full: the
    # workers skip the damage breakdown, and a build placing on one target's
    # list may have been cut short on another. Boots of one class still
    # share a fight.
    sheets, fought = {}, {}
    for k in keys:
        rows = []
        for _, ids, rs in lists[k]:
            t_ids = tuple(ids)
            if t_ids not in sheets:
                sheets[t_ids] = resolve_stats(champ, level, ids, pool, effects,
                                              kit=kit)
            if id(rs) not in fought:
                fx = merge_effects(ids, effects)
                fought[id(rs)] = {
                    t: simulate(sheets[t_ids], kit, fx, level, ranks,
                                sc["targetHp"], sc["armor"], sc["mr"],
                                sc["duration"], use_ult=use_ult,
                                prestacked=prestacked,
                                target_bonus_hp=sc.get("targetBonusHp", 0.0))
                    for t, sc in targets.items()}
            rows.append((ids, sheets[t_ids], fought[id(rs)]))
        lists[k] = rows
    return lists, count


def cmd_optimize(args):
    import time
    slug = norm_name(args.name)
    champ = load_champion(slug, args.patch)
    patch, pool = load_items(args.patch)
    idx = item_index(pool)
    effects = load_item_effects()
    kit = load_kit(slug)
    ranks = skill_ranks(args.level, kit_max_order(kit, args.max_order))
    candidates = ([resolve_item(pool, idx, t) for t in args.pool]
                  if args.pool else champion_pool(kit, effects))
    required = [resolve_item(pool, idx, t) for t in (args.require or [])]
    target = dict(targetHp=args.target_hp, armor=args.armor, mr=args.mr,
                  duration=args.duration, targetBonusHp=args.target_bonus_hp)

    t0 = time.time()
    lists, count = enumerate_builds(
        champ, pool, effects, kit, args.level, ranks, {"target": target},
        budget=args.budget, slots=args.slots, required=required,
        candidates=candidates, use_ult=not args.no_ult,
        prestacked=args.prestacked)
    results = [(ids, sheet, rs["target"]) for ids, sheet, rs in lists["target"]]

    print(f"{champ['dd']['name']} lvl {args.level} — {count} builds "
          f"in {time.time() - t0:.1f}s, {args.duration:g}s vs "
          f"{args.target_hp}hp {args.armor}armor {args.mr}mr"
          + (f", budget {args.budget}g" if args.budget else "") + "\n")
    print(f"  {'#':>3} {'ttk':>6} {'exp':>6} {'dps':>6} {'total':>7} "
          f"{'gold':>6}  items")
    for n, (ids, sheet, r) in enumerate(results[:args.top], 1):
        names = ", ".join(pool[i]["name"].split()[0] for i in ids)
        ttk = f"{r['ttk']:.2f}" if r["ttk"] is not None else "-"
        eff = f"{r['ttk_exp']:.2f}" if r["ttk_exp"] is not None else "-"
        print(f"  {n:>3} {ttk:>6} {eff:>6} {r['dps']:>6.0f} {r['total']:>7.0f} "
              f"{sheet['gold']:>6}  {names}")

    ids, sheet, r = results[0]
    print(f"\nBest: {', '.join(pool[i]['name'] for i in ids)}")
    for src, dmg in r["breakdown"].items():
        print(f"  {src:<12} {dmg:>8.0f}  {dmg / r['total'] * 100:>5.1f}%")


def cmd_items(args):
    patch, pool = load_items(None)
    q = norm_name(args.query)
    hits = [it for it in pool.values()
            if it["shop"]["purchasable"] and it["shop"]["prices"]["total"] > 0
            and (not q or q in norm_name(it["name"])
                 or any(q in norm_name(n) for n in it.get("nicknames", [])))]
    print(f"{len(hits)} purchasable items, patch {patch}:\n")
    for it in sorted(hits, key=lambda x: -x["shop"]["prices"]["total"]):
        stats = []
        for stat, fields in it["stats"].items():
            for field, val in fields.items():
                if val:
                    stats.append(f"{stat} {val:g}{'%' if field == 'percent' else ''}")
        print(f"  {it['id']:<6} {it['name']:<28} {it['shop']['prices']['total']:>5}g  "
              + ", ".join(stats))
