"""Theoretical build math: exact stat sheets (and, next, damage simulation)
for champion + item combinations — first principles only, no match data.

The plan for this domain, built up in phases:
  1. stat layer (this file's core) — resolve champion base stats at a level
     plus an item set into a final stat sheet using the real in-game rules
  2. kit encoding — data/builds/<champ>.json, hand-encoded ability formulas
  3. combat engine — deterministic event-based DPS vs a configured target
  4. optimizer — rank builds over a scenario grid (level x target x duration)

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

import glob
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import items
from common import BASE_DIR, DATA_DIR, patch_key

BUILDS_DATA_DIR = os.path.join(DATA_DIR, "builds")
CHAMPIONS_DIR = os.path.join(BUILDS_DATA_DIR, "champions")
ITEM_EFFECTS_PATH = os.path.join(BUILDS_DATA_DIR, "item-effects.json")

DDRAGON_CHAMP_INDEX = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
DDRAGON_CHAMP = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion/{cid}.json"
MERAKI_CHAMP = "https://cdn.merakianalytics.com/riot/lol/resources/latest/en-US/champions/{cid}.json"

AS_CAP = 2.5  # attack speed is hard-capped in game


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


def resolve_stats(champ, level, item_ids, pool, effects=None, kit=None):
    """Champion base stats at `level` plus `item_ids` -> final stat sheet.
    `kit` (the champion's encoding) supplies stat-converting passives —
    Vladimir's Crimson Pact; without it the sheet is items and levels only.

    Returns a flat dict; `uncovered` lists item passives that exist only as
    text and aren't part of the sheet (the combat engine's job, or genuinely
    out of scope) so callers can show what the numbers do NOT include.
    """
    effects = effects if effects is not None else load_item_effects()
    dd = champ["dd"]["stats"]
    mk = (champ["mk"] or {}).get("stats", {})

    agg = {v: 0.0 for v in ITEM_STAT_MAP.values()}
    ap_mult, gold, names, uncovered = 1.0, 0, [], []
    pct_pen_multi = {"armor_pen_pct": 0.0, "magic_pen_pct": 0.0}
    for iid in item_ids:
        it = pool[iid]
        names.append(it["name"])
        gold += it["shop"]["prices"]["total"]
        for stat, fields in it["stats"].items():
            for field, val in fields.items():
                if not val:
                    continue
                key = ITEM_STAT_MAP.get((stat, field))
                if key in pct_pen_multi:
                    pct_pen_multi[key] = stack_pct_pen(pct_pen_multi[key], val)
                elif key:
                    agg[key] += val
                elif (stat, field) not in IGNORED_ITEM_STATS:
                    print(f"Warning: unmapped item stat {stat}.{field} on "
                          f"{it['name']} — update ITEM_STAT_MAP", file=sys.stderr)
        fx = effects.get(iid, {})
        # AP increases (Rabadon, Blackfire) compound multiplicatively in game
        ap_mult *= 1.0 + fx.get("apMult", 0.0)
        # permanent-stack items (Rod of Ages) are assumed fully stacked
        for stat, val in fx.get("stackedStats", {}).items():
            agg[{"abilityPower": "ap_flat", "attackDamage": "ad_bonus",
                 "health": "hp", "mana": "mana"}[stat]] += val
        covered = set(fx.get("covers", []))
        uncovered += [f"{p['name']} ({it['name']})" for p in it["passives"]
                      if p.get("name") and p["name"] not in covered]
    agg.update(pct_pen_multi)
    # Stat-granting passives that need the item totals: Riftmaker (AP from
    # bonus health), Seraph's (AP from bonus mana), Muramana (AD from maximum
    # mana), Yun Tal (permanent crit stacks, assumed fully stacked). All land
    # before the AP multiplier, matching the in-game order.
    base_mana = stat_at(dd["mp"], dd["mpperlevel"], level)
    basic_haste = 0.0
    for iid in item_ids:
        fx = effects.get(iid, {})
        agg["ap_flat"] += fx.get("apFromBonusHpPct", 0.0) / 100.0 * agg["hp"]
        agg["ad_bonus"] += fx.get("adFromBonusHpPct", 0.0) / 100.0 * agg["hp"]
        agg["ap_flat"] += fx.get("apFromBonusManaPct", 0.0) / 100.0 * agg["mana"]
        agg["ad_bonus"] += (fx.get("adFromMaxManaPct", 0.0) / 100.0
                            * (base_mana + agg["mana"]))
        agg["crit_chance"] += fx.get("critChanceStackedPct", 0.0)
        basic_haste += fx.get("basicAbilityHaste", 0.0)
    for iid in item_ids:  # Famine (Endless Hunger): haste from total bonus AD
        fh = effects.get(iid, {}).get("hasteFromBonusAd")
        if fh:
            agg["haste"] += fh["base"] + fh["perBonusAdPct"] / 100.0 * agg["ad_bonus"]

    # Attack speed is the one stat with its own model: growth and item AS are
    # both "bonus AS" percent, converted through the champion's AS ratio
    # (ddragon doesn't ship the ratio; without meraki, ratio = base AS).
    base_as = dd["attackspeed"]
    as_ratio = mk.get("attackSpeedRatio", {}).get("flat", base_as)
    bonus_as = dd["attackspeedperlevel"] * growth(level) + agg["bonus_as_pct"]
    attack_speed = min(base_as + as_ratio * bonus_as / 100.0, AS_CAP)

    # Vladimir's Crimson Pact: 1 AP per 30 bonus health, and 1.6 bonus health
    # per point of AP. "Does not stack with itself" (wiki, Crimson Pact
    # notes): the AP the pact makes from health earns no health back — but
    # Rabadon's enhancement of that AP is credited to Rabadon's and does.
    # Riftmaker's Void Infusion reads bonus health, which the pact's health
    # is, so with both on the sheet the pair feeds back on itself; this is
    # the closed form of that fixed point (the wiki is silent on whether the
    # game resolves it that way — a modeling assumption).
    pact = (kit or {}).get("passive", {}).get("crimsonPact")
    pact_hp = 0.0
    if pact:
        per_hp = pact["apPer30BonusHp"] / 30.0
        per_ap = pact["bonusHpPerAp"]
        rift = sum(effects.get(i, {}).get("apFromBonusHpPct", 0.0)
                   for i in item_ids) / 100.0
        ap_pact = per_hp * agg["hp"]
        ap = (ap_mult * (agg["ap_flat"] + ap_pact * (1.0 - rift * per_ap))
              / (1.0 - ap_mult * rift * per_ap))
        agg["ap_flat"] = ap / ap_mult
        pact_hp = per_ap * (ap - ap_pact)
        for iid in item_ids:  # Overlord's: bonus AD counts that health too
            agg["ad_bonus"] += (effects.get(iid, {}).get("adFromBonusHpPct", 0.0)
                                / 100.0 * pact_hp)
    else:
        ap = agg["ap_flat"] * ap_mult
    haste = agg["haste"]
    # ddragon 16.5.1 onward publishes attackdamageperlevel = 0 for every
    # champion at once (other per-level fields untouched) while Riot's game
    # files keep the real growth (16.17: Vladimir 3, Kayle 2.5) — a Data
    # Dragon regression, not a game change. Meraki's per-level AD is the
    # fallback for exactly that case; meraki lags patches, so re-check any
    # champion against raw.communitydragon.org when the number matters.
    ad_growth = dd["attackdamageperlevel"]
    if ad_growth == 0:
        ad_growth = mk.get("attackDamage", {}).get("perLevel", 0.0) or 0.0
    sheet = {
        "champion": champ["slug"], "level": level, "gold": gold,
        "items": names,
        "ad_base": stat_at(dd["attackdamage"], ad_growth, level),
        "ad_bonus": agg["ad_bonus"],
        "ap": ap, "ap_flat": agg["ap_flat"], "ap_mult": ap_mult,
        "attack_speed": attack_speed, "base_as": base_as, "as_ratio": as_ratio,
        "bonus_as_pct": bonus_as,
        "crit_chance": min(agg["crit_chance"], 100.0),
        # 175% base crit damage; items with criticalStrikeDamage add to it
        "crit_damage": mk.get("criticalStrikeDamage", {}).get("flat", 175.0)
                       + agg["crit_damage_bonus"],
        "haste": haste,
        "cd_mult": 100.0 / (100.0 + haste),
        # Shojin's Dragonforce: extra haste for basic abilities only
        "basic_cd_mult": 100.0 / (100.0 + haste + basic_haste),
        "hp": stat_at(dd["hp"], dd["hpperlevel"], level) + agg["hp"] + pact_hp,
        "hp_bonus": agg["hp"] + pact_hp,
        "mana": stat_at(dd["mp"], dd["mpperlevel"], level) + agg["mana"],
        "mana_bonus": agg["mana"],
        "armor": stat_at(dd["armor"], dd["armorperlevel"], level) + agg["armor"],
        "mr": stat_at(dd["spellblock"], dd["spellblockperlevel"], level) + agg["mr"],
        "lethality": agg["lethality"],
        "armor_pen_pct": agg["armor_pen_pct"],
        "magic_pen_flat": agg["magic_pen_flat"],
        "magic_pen_pct": agg["magic_pen_pct"],
        "lifesteal": agg["lifesteal"], "omnivamp": agg["omnivamp"],
        "tenacity": agg["tenacity"],
        "heal_shield_power": agg["heal_shield_power"],
        "move_speed": (dd["movespeed"] + agg["ms_flat"])
                      * (1.0 + agg["ms_pct"] / 100.0),
        # base (form-independent) range; the kit's passive may override it
        "base_attack_range": dd["attackrange"],
        "uncovered": uncovered,
    }
    sheet["ad"] = sheet["ad_base"] + sheet["ad_bonus"]
    return sheet


# ---------------------------------------------------------------------------
# combat engine: deterministic expected-value damage timeline
#
# The engine owns the clock, the target, the damage pipeline and every item
# proc; a per-champion driver (KIT_DRIVERS) owns the rotation — what the
# champion does with its attacks and abilities. Approximations (all chosen
# to preserve build RANKING, not absolute DPS):
# - crit is expected value, no RNG anywhere
# - projectiles land instantly; the target never moves or acts
# - every ability cast has a flat 0.25s lockout that delays the next auto
# - an attack reset (Kayle E) lands its empowered attack after one windup
# - stacking buffs (Zeal, Seething, Terminus) never expire mid-fight
# - range-scaled amps (Hexoptics) assume every attack is made at max range
# ---------------------------------------------------------------------------

ABILITY_LOCKOUT_S = 0.25
INF = float("inf")
MELEE_MAX_RANGE = 325  # Riot's split: every melee champion attacks from <= 325


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
            # first one wins: same-named unique passives (two Spellblade
            # items) grant only one instance in game
            if k in e and fx[k] is None:
                fx[k] = e[k]
    return fx


# ---------------------------------------------------------------------------
# kit drivers: one class per hand-encoded champion. The engine calls these
# hooks at each point of the fight; `e` is the engine handle — fight state
# `e.st` (drivers keep their own state there too), the stat sheet, merged
# item effects, the target, and the callbacks an ability cast has to fire:
# deal, ability_cast_proc, eclipse_hit, prime_spellblade, basic_cd,
# attack_speed, lockout.
# ---------------------------------------------------------------------------

class KitDriver:
    basic_cd_keys = ("q_ready",)  # the cooldowns Navori's on-attack CDR shaves

    def __init__(self, kit, sheet, level, ranks, prestacked):
        self.kit, self.sheet = kit, sheet
        self.level, self.ranks, self.prestacked = level, ranks, prestacked
        self.attack_range = sheet["base_attack_range"]
        self.ranged = self.attack_range > MELEE_MAX_RANGE

    def init_state(self, st):
        pass

    def bonus_as(self, st):
        """Kit-side bonus attack speed (stacking passives), in percent."""
        return 0.0

    def before_attack(self, e):
        """On-attack, before the hit lands: stack gains."""

    def attack_riders(self, e):
        """Ability on-hit riders — reapplied whenever the item on-hits are
        (Guinsoo phantom hits, Dusk and Dawn)."""

    def after_attack(self, e):
        """Ability procs delivered by the attack that just landed."""

    def schedule_attack(self, e):
        e.st["next_attack"] = e.st["t"] + 1.0 / e.attack_speed()

    def q_at(self, e):
        """Earliest moment Q can be cast; INF when it can't be."""
        if not self.ranks["Q"]:
            return INF
        return max(e.st["q_ready"], e.st["t"])

    def cast_q(self, e):
        raise NotImplementedError

    def cast_r(self, e):
        """Kit-side effects of the R cast at t=0 (the engine schedules the
        delayed damage and the item ult-cast procs itself)."""

    def events(self, e):
        """Extra timed events as (time, kind) pairs the engine folds into
        its timeline; `on_event` handles them when they come up."""
        return ()

    def on_event(self, e, kind):
        raise NotImplementedError(kind)


class KayleDriver(KitDriver):
    """An auto-attacker: Zealous stacks attack speed per attack, Arisen makes
    her ranged at 6, Aflame (11) rides a wave on every attack at full stacks,
    E is an always-on on-hit plus an attack-reset active, Q shreds resists."""

    basic_cd_keys = ("q_ready", "e_ready")

    def __init__(self, kit, sheet, level, ranks, prestacked):
        super().__init__(kit, sheet, level, ranks, prestacked)
        p = kit["passive"]
        self.zeal = p["zealous"]
        self.zeal_perm = level >= self.zeal["permanentAtLevel"]
        self.ranged = level >= p["arisen"]["level"]
        self.aflame = level >= p["aflame"]["level"]
        self.wave = p["aflame"]["wave"]
        self.q_cfg, self.e_cfg = kit["abilities"]["Q"], kit["abilities"]["E"]
        # form passives override the champion's base attack range
        for form in ("arisen", "transcendent"):
            f = p.get(form, {})
            if "attackRange" in f and level >= f["level"]:
                self.attack_range = f["attackRange"]
        # per-fight constants: the Q's damage, the E on-hit, the wave's damage
        self.q_dmg = (ability_hit(self.q_cfg["damage"], ranks["Q"], sheet)
                      if ranks["Q"] else 0.0)
        self.e_onhit = (ability_hit(self.e_cfg["onhit"], ranks["E"], sheet)
                        if ranks["E"] else 0.0)
        self.wave_dmg = (by_level(self.wave["baseByLevel"], level)
                         + self.wave["bonusAdRatio"] * sheet["ad_bonus"]
                         + self.wave["apRatio"] * sheet["ap"])

    def init_state(self, st):
        st["zeal"] = (self.zeal["maxStacks"]
                      if (self.zeal_perm or self.prestacked) else 0)
        st["e_ready"], st["e_pending"] = 0.0, False

    def bonus_as(self, st):
        return st["zeal"] * self.zeal["asPctPerStack"]

    def before_attack(self, e):
        if not self.zeal_perm:
            e.st["zeal"] = min(e.st["zeal"] + 1, self.zeal["maxStacks"])

    def attack_riders(self, e):
        if self.ranks["E"]:
            e.deal(self.e_onhit, "magic", "E onhit")

    def after_attack(self, e):
        st = e.st
        if st["e_pending"]:
            act = self.e_cfg["active"]
            pct = (act["missingHpPct"][self.ranks["E"] - 1]
                   + act["missingHpPctPer100Ap"] * self.sheet["ap"] / 100.0)
            e.deal(pct / 100.0 * (e.target_hp - max(st["hp"], 0.0)), "magic",
                   "E active", crit_mod=self.aflame, ability=True)
            e.ability_cast_proc()
            e.eclipse_hit()
            st["e_pending"] = False
        if self.aflame and st["zeal"] >= self.zeal["maxStacks"]:
            e.deal(self.wave_dmg, "magic", "wave", crit_mod=True, ability=True)

    def schedule_attack(self, e):
        # E weave: cast right after an auto to use the attack reset
        st, t = e.st, e.st["t"]
        if self.ranks["E"] and t >= st["e_ready"] and not st["e_pending"]:
            st["e_pending"] = True
            st["e_ready"] = t + e.basic_cd(self.e_cfg["cooldownS"][self.ranks["E"] - 1])
            e.prime_spellblade()
            st["next_attack"] = t + self.kit["attack"]["windupFraction"] / e.attack_speed()
        else:
            st["next_attack"] = t + 1.0 / e.attack_speed()

    def cast_q(self, e):
        st, q = e.st, self.q_cfg
        st["q_ready"] = st["t"] + e.basic_cd(q["cooldownS"][self.ranks["Q"] - 1])
        st["shred_until"] = st["t"] + q.get("shred", {}).get("durationS", 0.0)
        e.deal(self.q_dmg, "magic", "Q", ability=True)
        e.ability_cast_proc()
        e.eclipse_hit()
        e.prime_spellblade()
        e.lockout()


class VladimirDriver(KitDriver):
    """A caster who pays health, not mana. The rotation, from the wiki's
    channel rules: R opens the fight (its amp holds through its own burst);
    E is charged only as long as its damage grows (1s of its 1.5s) and
    released — attacks and Q wait, since either would end the charge; W is
    cast as the charge begins (the E-W combo: the pool blocks attacks and
    casts for 2s, but a charging E may still release inside it); Q is cast
    the moment it's castable, and every third cast is Crimson Rush-empowered
    (a cooldown-cast Q always lands inside the 2.5s surge window)."""

    basic_cd_keys = ("q_ready", "e_ready", "w_ready")

    def __init__(self, kit, sheet, level, ranks, prestacked):
        super().__init__(kit, sheet, level, ranks, prestacked)
        ab = kit["abilities"]
        self.q_cfg, self.w_cfg, self.e_cfg, self.r_cfg = (
            ab[s] for s in ("Q", "W", "E", "R"))
        self.rush = self.q_cfg["crimsonRush"]
        # per-fight constants: each ability's damage and cooldown at its rank
        q, w, e = ranks["Q"], ranks["W"], ranks["E"]
        self.q_dmg = ability_hit(self.q_cfg["damage"], q, sheet) if q else 0.0
        self.q_cd = self.q_cfg["cooldownS"][q - 1] if q else INF
        self.e_dmg = ability_hit(self.e_cfg["damage"]["max"], e, sheet) if e else 0.0
        self.e_cd = self.e_cfg["cooldownS"][e - 1] if e else INF
        self.w_tick = (ability_hit(self.w_cfg["damage"], w, sheet)
                       / self.w_cfg["damage"]["ticks"]) if w else 0.0
        self.w_cd = self.w_cfg["cooldownS"][w - 1] if w else INF

    def init_state(self, st):
        st.update(e_ready=0.0, w_ready=0.0, q_casts=0,
                  busy_until=0.0,      # a cast animation: the next cast waits
                  charge_until=INF,    # E release time while charging
                  pool_until=-1.0,     # W: no attacks or casts until then
                  w_ticks_left=0, w_next=INF)

    def _castable_at(self, e, ready):
        st = e.st
        t = max(ready, st["t"], st["busy_until"], st["pool_until"])
        if st["charge_until"] != INF:  # a cast would cut the charge short
            t = max(t, st["charge_until"])
        return t

    def _cast_done(self, e):
        st = e.st
        st["busy_until"] = st["t"] + ABILITY_LOCKOUT_S
        st["next_attack"] = max(st["next_attack"], st["t"] + ABILITY_LOCKOUT_S)

    def q_at(self, e):
        return self._castable_at(e, e.st["q_ready"]) if self.ranks["Q"] else INF

    def cast_q(self, e):
        st = e.st
        st["q_ready"] = st["t"] + e.basic_cd(self.q_cd)
        st["q_casts"] += 1
        amt = self.q_dmg
        empowered = st["q_casts"] % self.rush["everyNthCast"] == 0
        if empowered:
            amt *= 1.0 + self.rush["bonusDamagePct"] / 100.0
        e.deal(amt, "magic", "Q empowered" if empowered else "Q", ability=True)
        e.ability_cast_proc()
        e.eclipse_hit()
        e.prime_spellblade()
        self._cast_done(e)

    def cast_r(self, e):
        st = e.st
        st["kit_amp_pct"] = self.r_cfg["ampPct"]
        st["kit_amp_until"] = st["t"] + self.r_cfg["delayS"]
        st["busy_until"] = st["t"] + ABILITY_LOCKOUT_S

    def events(self, e):
        st, ev = e.st, []
        if self.ranks["E"]:
            if st["charge_until"] != INF:
                ev.append((st["charge_until"], "e_release"))
            else:
                ev.append((self._castable_at(e, st["e_ready"]), "e_charge"))
        if st["w_ticks_left"]:
            ev.append((st["w_next"], "w_tick"))
        return ev

    def on_event(self, e, kind):
        st = e.st
        if kind == "e_charge":
            if self._castable_at(e, st["e_ready"]) > st["t"]:
                return  # a cast at this instant took priority; try again
            st["charge_until"] = st["t"] + self.e_cfg["chargeFullS"]
            st["next_attack"] = max(st["next_attack"], st["charge_until"])
            if self.ranks["W"] and st["t"] >= st["w_ready"]:
                self.cast_w(e)
        elif kind == "e_release":
            st["charge_until"] = INF
            st["e_ready"] = st["t"] + e.basic_cd(self.e_cd)
            e.deal(self.e_dmg, "magic", "E", ability=True)
            e.ability_cast_proc()
            e.eclipse_hit()
            e.prime_spellblade()
            self._cast_done(e)
        elif kind == "w_tick":
            e.deal(self.w_tick, "magic", "W", ability=True)
            st["w_ticks_left"] -= 1
            st["w_next"] = (st["t"] + self.w_cfg["damage"]["tickS"]
                            if st["w_ticks_left"] else INF)
        else:
            raise NotImplementedError(kind)

    def cast_w(self, e):
        st, w = e.st, self.w_cfg
        st["w_ready"] = st["t"] + e.basic_cd(self.w_cd)
        st["pool_until"] = st["t"] + w["durationS"]
        st["next_attack"] = max(st["next_attack"], st["pool_until"])
        st["w_ticks_left"], st["w_next"] = w["damage"]["ticks"], st["t"]
        e.prime_spellblade()


KIT_DRIVERS = {"kayle": KayleDriver, "vladimir": VladimirDriver}


def kit_driver(kit, sheet, level, ranks, prestacked):
    cls = KIT_DRIVERS.get(kit["champion"])
    if cls is None:
        raise ValueError(f"no engine driver for '{kit['champion']}' — a kit "
                         f"encoding needs matching rotation logic in builds.py")
    return cls(kit, sheet, level, ranks, prestacked)


def simulate(sheet, kit, fx, level, ranks, target_hp, target_armor, target_mr,
             duration, use_ult=True, prestacked=False, target_bonus_hp=0.0,
             stop_after=INF, breakdown=True, _blend=True):
    """One fight vs a stat dummy. Returns totals, DPS, time-to-kill (None if
    the dummy survives), and a per-source damage breakdown (empty unless
    `breakdown`: the enumerator skips it and fills it in for the rows that
    place) — or None if the clock passed `stop_after` with the dummy still
    standing (the enumerator stops fights whose outcome can no longer
    matter)."""
    drv = kit_driver(kit, sheet, level, ranks, prestacked)
    ranged, atk_range = drv.ranged, drv.attack_range
    r_cfg = kit["abilities"]["R"]
    # kit resist shreds (Kayle Q): reductions that hold while the debuff is up
    shred_cfg = kit["abilities"]["Q"].get("shred", {})
    shred_pct = {res: shred_cfg.get("pct", 0.0)
                 for res in shred_cfg.get("appliesTo", ())}

    crit_c = sheet["crit_chance"] / 100.0
    crit_ev = 1.0 + crit_c * (sheet["crit_damage"] / 100.0 - 1.0)

    # Hexoptics' Magnification: scales with distance to the target, capped.
    # We assume attacks are made at max range (see approximations above).
    auto_amp = 1.0
    if fx["attackAmp"]:
        aa = fx["attackAmp"]
        auto_amp += (aa["maxPct"] / 100.0
                     * min(1.0, atk_range / aa["maxAtRange"]))

    st = {
        "t": 0.0, "hp": float(target_hp),
        "seething": 0, "phantom": 0, "kraken": 0, "dark": 0, "attacks": 0,
        "phantom_hits": 0,
        "q_ready": 0.0,
        "shred_until": -1.0, "mal_shred_until": -1.0,
        # kit-side damage amp (Vladimir's Hemoplague): pct while t <= until
        "kit_amp_pct": 0.0, "kit_amp_until": -1.0,
        "combat_t0": None,  # when the first damage landed: "in combat" since
        "burns": [{"until": -1.0, "next": INF} for _ in fx["burns"]],
        "mal_until": -1.0, "next_mal": INF, "mal_tick": 0.0,
        "sb_primed": False, "sb_icd_until": -1.0,
        "flurry_until": -1.0, "flurry_ready": 0.0,
        "dmg_log": [], "ss_at": INF, "ss_done": False, "once_done": False,
        "energize": 0.0, "en_last": 0.0,
        "cleaver": 0, "blood": 0, "shojin": 0, "eclipse": 0, "nth": 0,
        "hz_until": -1.0, "hz_first": False, "ma_until": -1.0,
        "hex_until": -1.0, "post_r_attacks": 10 ** 9, "sundered_used": False,
        "r_impact": INF,
        "next_attack": 0.0,
        # per-timestamp damage batch, for the interpolated kill time
        "ev_t": -1.0, "ev_hp0": float(target_hp), "ev_dmg": 0.0,
        "prev_ev_t": 0.0,
        "breakdown": {}, "total": 0.0, "ttk": None, "ttk_eff": None,
        "exec_p": None,
    }
    drv.init_state(st)
    if kit.get("attack", {}).get("never"):
        # a kit played without autos: nothing that rides an attack ever fires
        st["next_attack"] = INF
    if fx["manaActive"]:  # Actualizer: cast on engage, empowered for 8s
        st["ma_until"] = fx["manaActive"]["durationS"]

    as_stacking, flurry, on_ult, ult_steroid = (
        fx["asStacking"], fx["flurry"], fx["onUltCast"], fx["ultAttackSteroid"])
    sheet_bonus_as, base_as, as_ratio = (sheet["bonus_as_pct"], sheet["base_as"],
                                         sheet["as_ratio"])

    def attack_speed():
        bonus = sheet_bonus_as + drv.bonus_as(st)
        if as_stacking:
            bonus += st["seething"] * as_stacking["pctPerStack"]
        if flurry and st["t"] < st["flurry_until"]:
            bonus += flurry["asPct"]
        if on_ult and st["t"] < st["hex_until"]:
            bonus += on_ult["asPct"]
        if ult_steroid and st["post_r_attacks"] < ult_steroid["attacks"]:
            bonus += ult_steroid["asPct"]
        return min(base_as + as_ratio * bonus / 100.0, AS_CAP)

    # Everything deal() needs that the build and target fix, resolved once:
    # the effect fields (most are None for most builds), the sheet's numbers,
    # the amp a damage type carries after n seconds in combat, and — cached
    # on the little state that changes it — the resist multiplier.
    dmg_amps, flat_amps = fx["dmgAmps"], fx["flatAmps"]
    giant, hyper, shojin_fx, mana_fx = (fx["giantSlayer"], fx["hypershot"],
                                       fx["abilityAmpStacking"], fx["manaActive"])
    alt_pen, armor_shred, mr_shred = fx["altPen"], fx["armorShred"], fx["mrShred"]
    opener, magic_crit, exec_pct = (fx["openerLethality"], fx["magicCrit"],
                                    fx["executePct"])
    storm, burn_fx, once_fx, ult_burn = (fx["stormsurge"], fx["burns"],
                                         fx["abilityProcOnce"], fx["ultBurn"])
    lethality, armor_pen_pct = sheet["lethality"], sheet["armor_pen_pct"]
    magic_pen_pct, magic_pen_flat = sheet["magic_pen_pct"], sheet["magic_pen_flat"]
    crit_damage = sheet["crit_damage"]
    shred_armor, shred_mr = shred_pct.get("armor", 0.0), shred_pct.get("mr", 0.0)
    hyper_amp = 1.0 + hyper["ampPct"] / 100.0 if hyper else 1.0
    mana_amp = (1.0 + (mana_fx["ampBasePct"] + mana_fx["ampPer100BonusMana"]
                       * sheet["mana_bonus"] / 100.0) / 100.0) if mana_fx else 1.0
    shojin_per = shojin_fx["pctPerStack"] / 100.0 if shojin_fx else 0.0
    shojin_max = shojin_fx["maxStacks"] if shojin_fx else 0
    crit_below = magic_crit["belowTargetHpPct"] if magic_crit else 0.0
    crit_below_pct = magic_crit["critDmgPct"] if magic_crit else 0.0
    crit_below_ev = magic_crit["critDmgPct"] / 100.0 if magic_crit else 0.0
    exec_hp = exec_pct / 100.0 * target_hp if exec_pct else 0.0
    opener_until = opener["durationS"] if opener else -1.0
    opener_leth = (opener["ranged"] if ranged else opener["melee"]) if opener else 0.0
    alt_max = alt_pen["maxStacks"] if alt_pen else 0
    alt_per = alt_pen["pctPerStack"] if alt_pen else 0.0
    cleaver_per = armor_shred["pctPerStack"] if armor_shred else 0.0
    cleaver_max = armor_shred["maxStacks"] if armor_shred else 0
    blood_per = mr_shred["pctPerStack"] if mr_shred else 0.0
    blood_max = mr_shred["maxStacks"] if mr_shred else 0
    mal_reduction = ult_burn["mrReduction"] if ult_burn else 0.0
    # the resist multiplier depends on more than the Q shred for some builds
    phys_dyn = bool(armor_shred or alt_pen or opener)
    magic_dyn = bool(mr_shred or alt_pen or ult_burn)
    # a build's base amp by damage type — and, with a ramp (Liandry's,
    # Riftmaker) on the build, by whole seconds in combat
    def base_amp(dt, secs):
        amp = 1.0
        for a in dmg_amps:
            amp *= 1.0 + a["pctPerStack"] / 100.0 * min(a["maxStacks"], secs)
        for a in flat_amps:  # Abyssal Mask: always-on, one damage type
            if a["damageType"] in (dt, "all"):
                amp *= 1.0 + a["pct"] / 100.0
        if giant:  # 1% per 100 target bonus HP, capped
            amp *= 1.0 + min(giant["maxPct"], target_bonus_hp / 100.0) / 100.0
        return amp
    amp_base = {}
    if dmg_amps:
        n_secs = max(1, int(duration) + 2) if duration < INF else 64
        for dt in ("physical", "magic", "true"):
            amp_base[dt] = [base_amp(dt, secs) for secs in range(n_secs)]
    else:
        for dt in ("physical", "magic", "true"):
            amp_base[dt] = [base_amp(dt, 0)]
    resist_phys, resist_magic = {}, {}
    bd = st["breakdown"]

    def deal(amount, dtype, source, crit_mod=False, ability=False,
             ev_floor=1.0):
        t = st["t"]
        if st["combat_t0"] is None:  # the first damage dealt opens combat
            st["combat_t0"] = t
        # Liandry's Suffering, Riftmaker's Void Corruption: a stack per whole
        # second in combat. The hair of tolerance keeps a cast that lands on
        # a second boundary on the right side of it — 4.6 / 1.15 is
        # 3.9999999999999996 to the machine.
        amp = amp_base[dtype][int(t - st["combat_t0"] + 1e-9) if dmg_amps else 0]
        if hyper and t < st["hz_until"]:
            amp *= hyper_amp
        if t <= st["kit_amp_until"] and dtype != "true":
            amp *= 1.0 + st["kit_amp_pct"] / 100.0
        # "increased basic damage" is the attack itself — not the on-hit
        # effects it triggers, and not abilities
        if source == "auto":
            amp *= auto_amp
        if ability:
            if shojin_fx:  # Shojin's Focused Will
                amp *= 1.0 + shojin_per * st["shojin"]
            if mana_fx and t < st["ma_until"]:
                amp *= mana_amp
        qs_on = t < st["shred_until"]
        if dtype == "physical":
            key = ((qs_on, st["cleaver"], st["dark"], t < opener_until)
                   if phys_dyn else qs_on)
            mult = resist_phys.get(key)
            if mult is None:
                dark_pen = min(st["dark"], alt_max) * alt_per if alt_pen else 0.0
                shred = shred_armor if qs_on else 0.0
                if armor_shred:  # Black Cleaver: % armor reduction stacks
                    shred += st["cleaver"] * cleaver_per
                leth = lethality
                if opener and t < opener_until:
                    leth += opener_leth
                mult = resist_phys[key] = resist_mult(eff_resist(
                    target_armor, 0.0, shred,
                    stack_pct_pen(armor_pen_pct, dark_pen), leth))
        elif dtype == "true":
            mult = 1.0
        else:
            key = ((qs_on, st["blood"], st["dark"], t < st["mal_shred_until"])
                   if magic_dyn else qs_on)
            mult = resist_magic.get(key)
            if mult is None:
                dark_pen = min(st["dark"], alt_max) * alt_per if alt_pen else 0.0
                mal = mal_reduction if ult_burn and t < st["mal_shred_until"] else 0.0
                shred = shred_mr if qs_on else 0.0
                if mr_shred:  # Bloodletter's Curse: % MR reduction stacks
                    shred += st["blood"] * blood_per
                mult = resist_magic[key] = resist_mult(eff_resist(
                    target_mr, mal, shred,
                    stack_pct_pen(magic_pen_pct, dark_pen), magic_pen_flat))
        # Cinderbloom is a deterministic crit: magic damage below the HP
        # threshold always deals critDmgPct (120%). A wave that can already
        # crit rolls real crit first; the non-crit share cinderblooms.
        hp = st["hp"]
        below = (magic_crit is not None and dtype == "magic"
                 and hp / target_hp * 100.0 < crit_below)
        ev = 1.0
        if crit_mod:
            ev = crit_ev
            if below:
                ev = (crit_c * crit_damage / 100.0
                      + (1.0 - crit_c) * crit_below_pct / 100.0)
            if ev < ev_floor:  # guaranteed-crit attacks (Sundered Sky)
                ev = ev_floor
        elif below:
            ev = crit_below_ev
        dmg = amount * amp * mult * ev
        # Damage arrives in batches at discrete times (an attack and all its
        # on-hits share one timestamp). Track each batch so the killing blow
        # can be credited only for the share of it that was actually needed.
        if t != st["ev_t"]:
            st["prev_ev_t"] = st["ev_t"] if st["ev_t"] > 0.0 else 0.0
            st["ev_t"], st["ev_hp0"], st["ev_dmg"] = t, hp, 0.0
        st["ev_dmg"] += dmg
        hp -= dmg
        st["hp"] = hp
        st["total"] += dmg
        if breakdown:
            bd[source] = bd.get(source, 0.0) + dmg
        # stacking shreds/amps build off the damage just dealt
        if dtype == "physical" and armor_shred:
            c = st["cleaver"] + 1
            st["cleaver"] = c if c < cleaver_max else cleaver_max
        if ability:
            if mr_shred and dtype == "magic":
                b = st["blood"] + 1
                st["blood"] = b if b < blood_max else blood_max
            if shojin_fx:
                s = st["shojin"] + 1
                st["shojin"] = s if s < shojin_max else shojin_max
            if hyper and not st["hz_first"]:
                # the opening cast is the one made from 600+ range
                st["hz_first"] = True
                st["hz_until"] = t + hyper["durationS"]
            for i, b in enumerate(burn_fx):
                sb = st["burns"][i]
                if sb["next"] == INF:
                    sb["next"] = t + b["tickS"]
                sb["until"] = t + b["durationS"]
            if once_fx and not st["once_done"]:
                st["once_done"] = True
                deal(once_fx["base"] + once_fx["apRatio"] * sheet["ap"],
                     once_fx["damageType"], once_fx["source"])
        hp = st["hp"]
        if storm and not st["ss_done"] and st["ss_at"] == INF:
            st["dmg_log"].append((t, dmg))
            recent = math.fsum(d for tt, d in st["dmg_log"]
                               if tt >= t - storm["windowS"])
            if recent >= storm["thresholdPct"] / 100.0 * target_hp:
                st["ss_at"] = t + storm["delayS"]
        exec_amt = 0.0
        if exec_pct and st["ttk"] is None and 0.0 < hp <= exec_hp:
            exec_amt = hp
            if breakdown:
                bd["execute"] = bd.get("execute", 0.0) + exec_amt
            st["total"] += exec_amt
            st["ev_dmg"] += exec_amt
            st["hp"] = hp = 0.0
        if hp <= 0 and st["ttk"] is None:
            st["ttk"] = t
            # Effective (ranking) kill time. The real kill lands on this
            # batch, but a blow that overkills 4x shouldn't earn the same
            # credit as one that barely finishes the job: interpolate back
            # over the gap by the fraction of the batch actually needed.
            # Without this, whole-attack rounding hands threshold effects
            # (Collector's execute) a full attack cycle for a few % of HP.
            frac = min(1.0, st["ev_hp0"] / st["ev_dmg"]) if st["ev_dmg"] > 0 else 1.0
            st["ttk_eff"] = st["prev_ev_t"] + frac * (t - st["prev_ev_t"])
            # With expected-value crit the health curve is fixed, so an
            # execute window ALWAYS catches the target. With real (random)
            # crits the health left before the killing attack is spread over
            # that attack's damage, so a window of W catches it only about
            # W/D of the time. Record that probability; the caller blends the
            # executing and non-executing timelines with it.
            if exec_amt > 0.0:
                batch = st["ev_dmg"] - exec_amt
                st["exec_p"] = min(1.0, exec_hp / batch) if batch > 0 else 1.0

    def prime_spellblade():
        if fx["spellblade"] and st["t"] >= st["sb_icd_until"]:
            st["sb_primed"] = True

    def ability_cast_proc():
        """Muramana's Shock: bonus physical damage per damaging ability cast."""
        if fx["abilityManaProc"]:
            mp = fx["abilityManaProc"]
            deal(by_level(mp["pctByLevel"], level) / 100.0 * sheet["mana"],
                 mp["damageType"], "muramana")

    def eclipse_hit():
        """Eclipse: attacks and damaging casts each grant one stack; every
        2nd stack procs (% max HP). No internal cooldown in 16.16."""
        if not fx["hitPairProc"]:
            return
        st["eclipse"] += 1
        if st["eclipse"] >= 2:
            st["eclipse"] = 0
            hp_cfg = fx["hitPairProc"]
            pct = hp_cfg["maxHpPctRanged"] if ranged else hp_cfg["maxHpPctMelee"]
            deal(pct / 100.0 * target_hp, hp_cfg["damageType"], "eclipse")

    def basic_cd(base_cd):
        """Basic-ability cooldown after haste (Shojin) and Actualizer's
        30%-faster window."""
        cd = base_cd * sheet["basic_cd_mult"]
        if fx["manaActive"] and st["t"] < st["ma_until"]:
            cd /= 1.0 + fx["manaActive"]["basicCdFasterPct"] / 100.0
        return cd

    def lockout():
        """A cast just happened: the next auto waits out its animation."""
        st["next_attack"] = max(st["next_attack"], st["t"]) + ABILITY_LOCKOUT_S

    e = SimpleNamespace(
        st=st, sheet=sheet, fx=fx, level=level, ranks=ranks,
        target_hp=target_hp, deal=deal, ability_cast_proc=ability_cast_proc,
        eclipse_hit=eclipse_hit, prime_spellblade=prime_spellblade,
        basic_cd=basic_cd, attack_speed=attack_speed, lockout=lockout)

    # on-hit damage is fixed by the sheet: work each entry out once
    onhits = []
    for oh in fx["onhit"]:
        amt = (oh["base"] + oh.get("apRatio", 0.0) * sheet["ap"]
               + oh.get("bonusAdRatio", 0.0) * sheet["ad_bonus"]
               + oh.get("maxManaPct", 0.0) / 100.0 * sheet["mana"])
        if "selfMaxHpPct" in oh:  # Titanic Hydra: % of OWN max health
            pct = oh["selfMaxHpPct"]["ranged" if ranged else "melee"]
            amt += pct / 100.0 * sheet["hp"]
        onhits.append((amt, oh["damageType"], oh.get("source", "onhit")))
    onhits_current = [((oh["rangedPct"] if ranged else oh["meleePct"]) / 100.0,
                       oh["damageType"]) for oh in fx["onhitCurrentHp"]]

    def apply_onhits():
        """Everything riding a basic attack hit (reapplied by a phantom hit)."""
        for amt, dtype, source in onhits:
            deal(amt, dtype, source)
        drv.attack_riders(e)
        for pct, dtype in onhits_current:
            deal(pct * max(st["hp"], 0.0), dtype, "botrk")

    navori, phantom, kraken, alt_pen_fx = (fx["navoriCdr"], fx["phantom"],
                                           fx["kraken"], fx["altPen"])
    sundered, nth_hit, first_bonus, spellblade, energized = (
        fx["firstAttackCritFloorEv"], fx["nthHitProc"], fx["firstAttackBonus"],
        fx["spellblade"], fx["energized"])
    ad, move_speed = sheet["ad"], sheet["move_speed"]
    # Energize: 6 stacks per attack (+ item bonuses) plus 1 per 24 units
    # moved — assumed kiting at full move speed between attacks
    energize_per_attack = 6.0 + sum(en.get("extraStacksPerAttack", 0.0)
                                    for en in energized)
    if kraken:
        kraken_base = by_level(kraken["baseByLevel"], level)
        if ranged:
            kraken_base *= kraken["rangedMult"]
        kraken_amp = kraken["missingHpAmpMaxPct"]["ranged" if ranged else "melee"]
    if nth_hit:  # Hullbreaker's Skipper
        nth_need = (nth_hit["stacksNeededRanged"] if ranged
                    else nth_hit["stacksNeededMelee"])
        nth_dmg = ((nth_hit["baseAdRatioRanged"] if ranged
                    else nth_hit["baseAdRatioMelee"]) * sheet["ad_base"]
                   + (nth_hit["selfMaxHpPctRanged"] if ranged
                      else nth_hit["selfMaxHpPctMelee"]) / 100.0 * sheet["hp"])
    if spellblade:
        spellblade_dmg = (spellblade["baseAdRatio"] * sheet["ad_base"]
                          + spellblade["apRatio"] * sheet["ap"]
                          + spellblade.get("perCritChancePct", 0.0)
                          * sheet["crit_chance"])

    def do_attack():
        t = st["t"]
        st["attacks"] += 1
        if navori:  # on-attack: shave 15% off remaining basic CDs
            for key in drv.basic_cd_keys:
                if st[key] > t:
                    st[key] = t + (st[key] - t) * (1.0 - navori / 100.0)
        if flurry:
            if t >= st["flurry_ready"]:
                st["flurry_until"] = t + flurry["durationS"]
                st["flurry_ready"] = t + flurry["cooldownS"]
            # on-hit refund (1s, 2s on crit -> EV blend) pulls the next window in
            st["flurry_ready"] -= (flurry["refundOnHitS"]
                                   + crit_c * flurry["refundCritExtraS"])
        # on-attack stack machinery first (pre-hit state decides the procs)
        phantom_now = False
        if phantom and st["phantom"] >= phantom["stacksNeeded"]:
            phantom_now, st["phantom"] = True, 0
        kraken_now = False
        if kraken and st["kraken"] >= 2:
            kraken_now, st["kraken"] = True, 0
        elif kraken:
            st["kraken"] += 1
        if as_stacking:
            st["seething"] = min(st["seething"] + 1, as_stacking["maxStacks"])
            # the consuming attack grants no Phantom stack: Riot's tooltip is
            # "every third Attack" at full stacks, not every other
            if (phantom and not phantom_now
                    and st["seething"] == as_stacking["maxStacks"]):
                st["phantom"] = min(st["phantom"] + 1, phantom["stacksNeeded"])
        if alt_pen_fx:
            if st["attacks"] % 2 == 0:  # every other hit is a Dark hit
                st["dark"] = min(st["dark"] + 1, alt_pen_fx["maxStacks"])
        drv.before_attack(e)

        floor = 1.0
        if sundered and not st["sundered_used"]:
            st["sundered_used"] = True  # Sundered Sky: once per target
            floor = sundered
        if ult_steroid and st["post_r_attacks"] < ult_steroid["attacks"]:
            floor = max(floor, ult_steroid.get("critFloorEv", 1.0))
        deal(ad, "physical", "auto", crit_mod=True, ev_floor=floor)
        apply_onhits()
        if energized:
            st["energize"] += (t - st["en_last"]) * move_speed / 24.0
            st["en_last"] = t
            st["energize"] += energize_per_attack
            if st["energize"] >= 100.0:
                st["energize"] -= 100.0
                for en in energized:
                    deal(en["bonus"], en["damageType"],
                         en.get("source", "energized"))
        eclipse_hit()
        if nth_hit:
            if st["nth"] >= nth_need:
                st["nth"] = 0
                deal(nth_dmg, nth_hit["damageType"], "hullbreaker")
            else:
                st["nth"] += 1
        if first_bonus and st["attacks"] == 1:  # Umbral: opens from unseen
            deal(first_bonus["base"] + first_bonus["perLethality"] * lethality,
                 first_bonus["damageType"], first_bonus.get("source", "opener"))
        st["post_r_attacks"] += 1
        if st["sb_primed"]:
            deal(spellblade_dmg, spellblade["damageType"], "spellblade")
            st["sb_primed"] = False
            st["sb_icd_until"] = t + spellblade["icdS"]
            if spellblade.get("reapplyOnhit"):  # Dusk and Dawn: on-hits land twice
                apply_onhits()
        if kraken_now:
            missing = 1.0 - max(st["hp"], 0.0) / target_hp
            deal(kraken_base * (1.0 + kraken_amp / 100.0 * missing),
                 kraken["damageType"], "kraken")
        drv.after_attack(e)
        if phantom_now:
            st["phantom_hits"] += 1
            apply_onhits()
        drv.schedule_attack(e)

    # opening casts at t=0, before the first auto
    if use_ult and ranks["R"]:
        st["r_impact"] = r_cfg["delayS"]
        prime_spellblade()
        st["next_attack"] += ABILITY_LOCKOUT_S
        if fx["onUltCast"]:  # Hexplate Overdrive starts on cast
            st["hex_until"] = fx["onUltCast"]["durationS"]
        if fx["ultAttackSteroid"]:  # Fiendhunter's next-3-attacks window
            st["post_r_attacks"] = 0
        drv.cast_r(e)
    # item actives (Rocketbelt, Gunblade, hydra actives) fire on engage;
    # they're item casts, not abilities — no spellblade/burn interaction
    for a in fx["activesOnce"]:
        amt = (a.get("base", 0.0)
               + (by_level(a["byLevel"], level) if "byLevel" in a else 0.0)
               + a.get("adRatio", 0.0) * sheet["ad"]
               + a.get("apRatio", 0.0) * sheet["ap"])
        deal(amt, a["damageType"], a.get("source", "active"))

    burns = st["burns"]
    while True:
        # the next event: the earliest of everything scheduled; at the same
        # instant, the kind that sorts first (attack, burn, e_charge,
        # e_release, mal, q, r, ss, w_tick), then the earlier burn
        t_next, kind, idx = st["next_attack"], "attack", 0
        x = st["next_mal"]
        if x < t_next or (x == t_next and "mal" < kind):
            t_next, kind = x, "mal"
        for i, b in enumerate(burns):
            x = b["next"]
            if x < t_next or (x == t_next and ("burn", i) < (kind, idx)):
                t_next, kind, idx = x, "burn", i
        x = st["ss_at"]
        if x < t_next or (x == t_next and "ss" < kind):
            t_next, kind, idx = x, "ss", 0
        x = st["r_impact"]
        if x < t_next or (x == t_next and "r" < kind):
            t_next, kind, idx = x, "r", 0
        q_at = drv.q_at(e)  # so Q casts the moment it's ready, not at the next event
        if q_at < t_next or (q_at == t_next and "q" < kind):
            t_next, kind, idx = q_at, "q", 0
        for x, k in drv.events(e):
            if x < t_next or (x == t_next and k < kind):
                t_next, kind, idx = x, k, 0
        if t_next > duration or st["hp"] <= 0:
            break
        if t_next > stop_after:
            return None
        st["t"] = t_next
        # castable now? q_at only grows with the clock, so the answer is
        # whether the moment it was castable is already here
        if q_at <= t_next:
            drv.cast_q(e)
            if kind == "attack" and st["next_attack"] > st["t"]:
                continue  # the lockout pushed this auto; re-pick the next event
        if kind == "attack":
            do_attack()
        elif kind == "burn":
            b = fx["burns"][idx]
            ticks = b["durationS"] / b["tickS"]
            if "maxHpPctTotal" in b:  # Liandry's: % of target max HP
                tick = b["maxHpPctTotal"] / ticks / 100.0 * target_hp
            else:  # Blackfire Torch: flat + AP ratio, total over the duration
                tick = (b["totalBase"] + b["totalApRatio"] * sheet["ap"]) / ticks
            deal(tick, b["damageType"], b.get("source", "burn"))
            bs = st["burns"][idx]
            bs["next"] = (st["t"] + b["tickS"]
                          if st["t"] + b["tickS"] <= bs["until"] else INF)
        elif kind == "ss":
            st["ss_at"], st["ss_done"] = INF, True
            ss = fx["stormsurge"]
            deal(ss["base"] + ss["apRatio"] * sheet["ap"], ss["damageType"],
                 "stormsurge")
        elif kind == "mal":
            deal(st["mal_tick"], "magic", "malignance")
            st["next_mal"] = (st["t"] + 0.25
                              if st["t"] + 0.25 <= st["mal_until"] else INF)
        elif kind == "r":
            st["r_impact"] = INF
            deal(ability_hit(r_cfg["damage"], ranks["R"], sheet), "magic", "R",
                 ability=True)
            ability_cast_proc()
            eclipse_hit()
            if fx["hypershot"]:  # R is always a 600+ range cast
                st["hz_until"] = max(st["hz_until"],
                                     st["t"] + fx["hypershot"]["durationS"])
            if fx["ultBurn"]:
                ub = fx["ultBurn"]
                ticks = ub["durationS"] / 0.25
                st["mal_tick"] = (ub["totalBase"]
                                  + ub["totalApRatio"] * sheet["ap"]) / ticks
                st["mal_until"] = st["t"] + ub["durationS"]
                st["mal_shred_until"] = st["t"] + ub["durationS"]
                st["next_mal"] = st["t"] + 0.25
        elif kind != "q":
            drv.on_event(e, kind)

    # Expected kill time: blend the executing timeline with the one where the
    # window was missed, weighted by how often real crit would land in it.
    # Only execute builds pay for the second pass. If the non-executing run
    # never kills, the fight length stands in for "no kill this window".
    ttk_exp = st["ttk"]
    if _blend and st["ttk"] is not None and (st["exec_p"] or 1.0) < 1.0:
        alt = simulate(sheet, kit, dict(fx, executePct=None), level, ranks,
                       target_hp, target_armor, target_mr, duration,
                       use_ult=use_ult, prestacked=prestacked,
                       target_bonus_hp=target_bonus_hp, breakdown=False,
                       _blend=False)
        p = st["exec_p"]
        ttk_exp = (p * st["ttk"]
                   + (1.0 - p) * (alt["ttk"] if alt["ttk"] is not None
                                  else duration))
    fight = min(duration, st["ttk"]) if st["ttk"] is not None else duration
    return {"total": st["total"], "dps": st["total"] / fight if fight else 0.0,
            "ttk": st["ttk"], "ttk_eff": st["ttk_eff"],
            "ttk_exp": ttk_exp, "attacks": st["attacks"],
            "phantom_hits": st["phantom_hits"], "hp_left": max(st["hp"], 0.0),
            "breakdown": dict(sorted(st["breakdown"].items(),
                                     key=lambda kv: -kv[1]))}


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
                      f"one to KIT_DRIVERS to simulate it", file=sys.stderr)
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

# This module's own source is part of every cache key: a result is only valid
# for the code that produced it. Hashed once at import, so a serve that keeps
# running after an edit still recognises the results matching the code it
# runs — and can report itself stale (source_stale) instead of chasing files
# it will never see.
with open(os.path.abspath(__file__), "rb") as _f:
    SOURCE_HASH = hashlib.sha256(_f.read()).hexdigest()


def source_stale():
    """Whether builds.py on disk differs from the code this process runs."""
    with open(os.path.abspath(__file__), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest() != SOURCE_HASH


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
    # fsum: the same bits from every Python (CPython's sum() compensates
    # float rounding since 3.12; PyPy's doesn't)
    return math.exp(math.fsum(math.log(max(x, 1e-9)) for x in xs) / len(xs))


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
# unpruned pass ranks them. A stale read of the table is only ever looser.
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


# slack on the kill-time cuts: a stopped fight must belong to a build that is
# worse than the keep-th best by more than rounding could account for
_PRUNE_SLACK = 1.0 + 1e-9
# the guess behind the overall list's bound: no build kills a target faster
# than this share of the fastest kill seen so far (checked at the end; a
# wrong guess costs a second pass, never a wrong result)
MIN_KILL_GUESS = 0.75
# pools with more builds than this fan out across CPU cores
FORK_ABOVE = 50_000


def _enum_task(ctx, task):
    """Simulate one task's builds and return ({key: rows}, ranked count) —
    the rows that could still place, given the bounds when they were
    scored. A task is ("block", size, prefix): every combination of `size`
    free items whose first indices are `prefix` (the rest enumerate in C);
    or ("builds", [ids, ...]): explicit builds, the seeds. Each boots class
    fights every target once and every member of the class is ranked with
    that fight (boots_partitions)."""
    import itertools
    keep, targets, overall = ctx["keep"], ctx["targets"], ctx["overall"]
    tkeys = list(targets)
    keys = tkeys + ([overall] if overall else [])
    n_t = len(tkeys)
    bounds = _Bounds(keys, overall, ctx["shared"])
    out = {k: [] for k in keys}
    n = 0
    groups, caps, price, budget = (ctx["groups"], ctx["caps"], ctx["price"],
                                   ctx["budget"])
    champ, level, pool, effects, kit = (ctx["champ"], ctx["level"], ctx["pool"],
                                        ctx["effects"], ctx["kit"])
    ranks, use_ult, prestacked = ctx["ranks"], ctx["use_ult"], ctx["prestacked"]
    free, partitions, energized = ctx["free"], ctx["classes"], ctx["energized"]
    order = ctx["order"]
    place = lambda ids: _place(order, ids)
    # boots share no ownership group with anything else in the game today,
    # so a build's legality is its items' — but a boots in a group is checked
    grouped_boots = {b for b in BOOTS if groups.get(b)}
    if task[0] == "block":
        _, size, prefix = task
        stem = [*ctx["required"], *(free[i] for i in prefix)]
        tail = free[prefix[-1] + 1:] if prefix else free
        work = ((stem + list(c), None) for c in
                itertools.combinations(tail, size - len(prefix)))
    else:
        work = ((list(ids[1:]), [[ids[0]]]) for ids in task[1])
    tv = [(t["targetHp"], t["armor"], t["mr"], t["duration"],
           t.get("targetBonusHp", 0.0)) for t in targets.values()]
    for rest, classes in work:
        if not build_is_legal(rest, groups, caps):
            continue
        if classes is None:
            classes = partitions[energized.isdisjoint(rest)]
        # the bounds, once per item combination
        tb = [bounds.target(i) for i in range(n_t)]
        if overall:
            o_max, o_g, o_ids = bounds.overall_bound(n_t)
            o_ids = place(o_ids)
        for members in classes:
            legal = [[b, *rest] for b in members
                     if b not in grouped_boots
                     or build_is_legal([b, *rest], groups, caps)]
            if budget:
                legal = [ids for ids in legal
                         if sum(price[i] for i in ids) <= budget]
            if not legal:
                continue
            n += len(legal)
            sheet = resolve_stats(champ, level, legal[0], pool, effects, kit=kit)
            fx = merge_effects(legal[0], effects)
            rs, unkilled, prod = {}, 0, 1.0
            # once the build can no longer make the overall list, each fight
            # only has its own target's list to make
            out_of_overall = not overall
            for i, k in enumerate(tkeys):
                hp, armor, mr, duration, bonus_hp = tv[i]
                stop = INF
                if not out_of_overall:
                    # a survivor too many, or the same survivors as the
                    # keep-th row and ids that sort after it (rows with a
                    # survivor tie on everything but ids)
                    if unkilled > o_max or (unkilled == o_max and o_max > 0
                                            and min(map(place, legal)) > o_ids):
                        out_of_overall = True
                if out_of_overall:
                    stop = tb[i][0] * _PRUNE_SLACK
                elif o_max == 0:
                    # every target has to die: the geometric mean of the
                    # kill times bounds this fight — the fights already
                    # fought by their times, those to come by the fastest
                    # any build kills them
                    rem = prod
                    for j in range(i + 1, n_t):
                        rem *= tb[j][2]
                    if rem > 0.0:
                        stop = max(tb[i][0], o_g ** n_t / rem) * _PRUNE_SLACK
                r = simulate(sheet, kit, fx, level, ranks, hp, armor, mr,
                             duration, use_ult=use_ult, prestacked=prestacked,
                             target_bonus_hp=bonus_hp, stop_after=stop,
                             breakdown=False)
                rs[k] = r
                if r is None:  # cut: off this target's list and the overall
                    out_of_overall = True
                    continue
                if r["ttk"] is None:
                    unkilled += 1
                prod *= kill_time(r, duration) or 0.0
            for i, k in enumerate(tkeys):
                r = rs[k]
                if r is None:
                    continue
                t_max, tot_min, _ = tb[i]
                if r["ttk"] is not None:
                    if r["ttk_exp"] > t_max:
                        continue
                elif r["total"] < tot_min:
                    continue
                key = rank_key(r)
                for ids in legal:
                    out[k].append((key, ids, rs))
                _keep_best(out[k], keep, order)
            if overall and not out_of_overall:
                key = overall_key(rs, targets)
                lead = (key[0], key[1])
                if lead < (o_max, o_g):
                    take = legal
                elif lead == (o_max, o_g):
                    take = legal if o_max == 0 else [ids for ids in legal
                                                     if place(ids) <= o_ids]
                else:
                    take = []
                for ids in take:
                    out[overall].append((key, ids, rs))
                _keep_best(out[overall], keep, order)
    for k in keys:
        _cut(out[k], keep, order)
    return out, n


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
        price={i: pool[i]["shop"]["prices"]["total"]
               for i in set(free) | set(required) | set(BOOTS)},
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

    if combos > FORK_ABOVE and workers > 1 and hasattr(os, "fork"):
        import multiprocessing as mp
        global _ENUM_CTX
        ctx["shared"] = mp.RawArray("d", _Bounds.WIDTH * len(keys))
        bounds = _Bounds(keys, overall, ctx["shared"])
        bounds.reset()
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
        ctx["shared"] = [0.0] * (_Bounds.WIDTH * len(keys))
        bounds = _Bounds(keys, overall, ctx["shared"])
        bounds.reset()
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
