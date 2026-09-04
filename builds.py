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
            e.deal(ability_hit(self.e_cfg["onhit"], self.ranks["E"], self.sheet),
                   "magic", "E onhit")

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
            e.deal(by_level(self.wave["baseByLevel"], self.level)
                   + self.wave["bonusAdRatio"] * self.sheet["ad_bonus"]
                   + self.wave["apRatio"] * self.sheet["ap"],
                   "magic", "wave", crit_mod=True, ability=True)

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
        e.deal(ability_hit(q["damage"], self.ranks["Q"], self.sheet), "magic", "Q",
               ability=True)
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
        st, q, rank = e.st, self.q_cfg, self.ranks["Q"]
        st["q_ready"] = st["t"] + e.basic_cd(q["cooldownS"][rank - 1])
        st["q_casts"] += 1
        amt = ability_hit(q["damage"], rank, self.sheet)
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
            rank = self.ranks["E"]
            st["e_ready"] = st["t"] + e.basic_cd(self.e_cfg["cooldownS"][rank - 1])
            e.deal(ability_hit(self.e_cfg["damage"]["max"], rank, self.sheet),
                   "magic", "E", ability=True)
            e.ability_cast_proc()
            e.eclipse_hit()
            e.prime_spellblade()
            self._cast_done(e)
        elif kind == "w_tick":
            w, rank = self.w_cfg, self.ranks["W"]
            e.deal(ability_hit(w["damage"], rank, self.sheet) / w["damage"]["ticks"],
                   "magic", "W", ability=True)
            st["w_ticks_left"] -= 1
            st["w_next"] = st["t"] + w["damage"]["tickS"] if st["w_ticks_left"] else INF
        else:
            raise NotImplementedError(kind)

    def cast_w(self, e):
        st, w, rank = e.st, self.w_cfg, self.ranks["W"]
        st["w_ready"] = st["t"] + e.basic_cd(w["cooldownS"][rank - 1])
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
             _blend=True):
    """One fight vs a stat dummy. Returns totals, DPS, time-to-kill (None if
    the dummy survives), and a per-source damage breakdown."""
    from types import SimpleNamespace
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

    def attack_speed():
        bonus = sheet["bonus_as_pct"] + drv.bonus_as(st)
        if fx["asStacking"]:
            bonus += st["seething"] * fx["asStacking"]["pctPerStack"]
        if fx["flurry"] and st["t"] < st["flurry_until"]:
            bonus += fx["flurry"]["asPct"]
        if fx["onUltCast"] and st["t"] < st["hex_until"]:
            bonus += fx["onUltCast"]["asPct"]
        if (fx["ultAttackSteroid"]
                and st["post_r_attacks"] < fx["ultAttackSteroid"]["attacks"]):
            bonus += fx["ultAttackSteroid"]["asPct"]
        return min(sheet["base_as"] + sheet["as_ratio"] * bonus / 100.0, AS_CAP)

    def deal(amount, dtype, source, crit_mod=False, ability=False,
             ev_floor=1.0):
        t = st["t"]
        amp = 1.0
        if st["combat_t0"] is None:  # the first damage dealt opens combat
            st["combat_t0"] = t
        # Liandry's Suffering, Riftmaker's Void Corruption: a stack per whole
        # second in combat. The hair of tolerance keeps a cast that lands on
        # a second boundary on the right side of it — 4.6 / 1.15 is
        # 3.9999999999999996 to the machine.
        secs_in_combat = int(t - st["combat_t0"] + 1e-9)
        for a in fx["dmgAmps"]:
            amp *= 1.0 + a["pctPerStack"] / 100.0 * min(a["maxStacks"], secs_in_combat)
        for a in fx["flatAmps"]:  # Abyssal Mask: always-on, one damage type
            if a["damageType"] in (dtype, "all"):
                amp *= 1.0 + a["pct"] / 100.0
        if fx["giantSlayer"]:  # 1% per 100 target bonus HP, capped
            amp *= 1.0 + min(fx["giantSlayer"]["maxPct"],
                             target_bonus_hp / 100.0) / 100.0
        if fx["hypershot"] and t < st["hz_until"]:
            amp *= 1.0 + fx["hypershot"]["ampPct"] / 100.0
        if t <= st["kit_amp_until"] and dtype != "true":
            amp *= 1.0 + st["kit_amp_pct"] / 100.0
        # "increased basic damage" is the attack itself — not the on-hit
        # effects it triggers, and not abilities
        if source == "auto":
            amp *= auto_amp
        if ability and fx["abilityAmpStacking"]:  # Shojin's Focused Will
            aa = fx["abilityAmpStacking"]
            amp *= 1.0 + aa["pctPerStack"] / 100.0 * st["shojin"]
        if ability and fx["manaActive"] and t < st["ma_until"]:
            ma = fx["manaActive"]
            amp *= 1.0 + (ma["ampBasePct"] + ma["ampPer100BonusMana"]
                          * sheet["mana_bonus"] / 100.0) / 100.0
        dark_pen = 0.0
        if fx["altPen"]:
            dark_pen = min(st["dark"], fx["altPen"]["maxStacks"]) \
                       * fx["altPen"]["pctPerStack"]
        qs_on = t < st["shred_until"]
        if dtype == "physical":
            shred = shred_pct.get("armor", 0.0) if qs_on else 0.0
            if fx["armorShred"]:  # Black Cleaver: % armor reduction stacks
                shred += st["cleaver"] * fx["armorShred"]["pctPerStack"]
            leth = sheet["lethality"]
            if fx["openerLethality"] and t < fx["openerLethality"]["durationS"]:
                ol = fx["openerLethality"]
                leth += ol["ranged"] if ranged else ol["melee"]
            r = eff_resist(target_armor, 0.0, shred,
                           stack_pct_pen(sheet["armor_pen_pct"], dark_pen),
                           leth)
        elif dtype == "true":
            r = 0.0
        else:
            mal = (fx["ultBurn"]["mrReduction"]
                   if fx["ultBurn"] and t < st["mal_shred_until"] else 0.0)
            shred = shred_pct.get("mr", 0.0) if qs_on else 0.0
            if fx["mrShred"]:  # Bloodletter's Curse: % MR reduction stacks
                shred += st["blood"] * fx["mrShred"]["pctPerStack"]
            r = eff_resist(target_mr, mal, shred,
                           stack_pct_pen(sheet["magic_pen_pct"], dark_pen),
                           sheet["magic_pen_flat"])
        # Cinderbloom is a deterministic crit: magic damage below the HP
        # threshold always deals critDmgPct (120%). A wave that can already
        # crit rolls real crit first; the non-crit share cinderblooms.
        below = (fx["magicCrit"] is not None and dtype == "magic"
                 and st["hp"] / target_hp * 100.0
                     < fx["magicCrit"]["belowTargetHpPct"])
        ev = 1.0
        if crit_mod:
            ev = crit_ev
            if below:
                ev = (crit_c * sheet["crit_damage"] / 100.0
                      + (1.0 - crit_c) * fx["magicCrit"]["critDmgPct"] / 100.0)
            ev = max(ev, ev_floor)  # guaranteed-crit attacks (Sundered Sky)
        elif below:
            ev = fx["magicCrit"]["critDmgPct"] / 100.0
        dmg = amount * amp * resist_mult(r) * ev
        # Damage arrives in batches at discrete times (an attack and all its
        # on-hits share one timestamp). Track each batch so the killing blow
        # can be credited only for the share of it that was actually needed.
        if t != st["ev_t"]:
            st["prev_ev_t"] = max(st["ev_t"], 0.0)
            st["ev_t"], st["ev_hp0"], st["ev_dmg"] = t, st["hp"], 0.0
        st["ev_dmg"] += dmg
        st["hp"] -= dmg
        st["total"] += dmg
        st["breakdown"][source] = st["breakdown"].get(source, 0.0) + dmg
        # stacking shreds/amps build off the damage just dealt
        if dtype == "physical" and fx["armorShred"]:
            st["cleaver"] = min(st["cleaver"] + 1, fx["armorShred"]["maxStacks"])
        if ability:
            if fx["mrShred"] and dtype == "magic":
                st["blood"] = min(st["blood"] + 1, fx["mrShred"]["maxStacks"])
            if fx["abilityAmpStacking"]:
                st["shojin"] = min(st["shojin"] + 1,
                                   fx["abilityAmpStacking"]["maxStacks"])
            if fx["hypershot"] and not st["hz_first"]:
                # the opening cast is the one made from 600+ range
                st["hz_first"] = True
                st["hz_until"] = t + fx["hypershot"]["durationS"]
        if ability:
            for i, b in enumerate(fx["burns"]):
                if st["burns"][i]["next"] == INF:
                    st["burns"][i]["next"] = t + b["tickS"]
                st["burns"][i]["until"] = t + b["durationS"]
            if fx["abilityProcOnce"] and not st["once_done"]:
                st["once_done"] = True
                po = fx["abilityProcOnce"]
                deal(po["base"] + po["apRatio"] * sheet["ap"],
                     po["damageType"], po["source"])
        if fx["stormsurge"] and not st["ss_done"] and st["ss_at"] == INF:
            ss = fx["stormsurge"]
            st["dmg_log"].append((t, dmg))
            recent = sum(d for tt, d in st["dmg_log"]
                         if tt >= t - ss["windowS"])
            if recent >= ss["thresholdPct"] / 100.0 * target_hp:
                st["ss_at"] = t + ss["delayS"]
        exec_amt = 0.0
        if (fx["executePct"] and st["ttk"] is None
                and 0.0 < st["hp"] <= fx["executePct"] / 100.0 * target_hp):
            exec_amt = st["hp"]
            st["breakdown"]["execute"] = (st["breakdown"].get("execute", 0.0)
                                          + exec_amt)
            st["total"] += exec_amt
            st["ev_dmg"] += exec_amt
            st["hp"] = 0.0
        if st["hp"] <= 0 and st["ttk"] is None:
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
                window = fx["executePct"] / 100.0 * target_hp
                batch = st["ev_dmg"] - exec_amt
                st["exec_p"] = min(1.0, window / batch) if batch > 0 else 1.0

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

    def apply_onhits():
        """Everything riding a basic attack hit (reapplied by a phantom hit)."""
        for oh in fx["onhit"]:
            amt = (oh["base"] + oh.get("apRatio", 0.0) * sheet["ap"]
                   + oh.get("bonusAdRatio", 0.0) * sheet["ad_bonus"]
                   + oh.get("maxManaPct", 0.0) / 100.0 * sheet["mana"])
            if "selfMaxHpPct" in oh:  # Titanic Hydra: % of OWN max health
                pct = oh["selfMaxHpPct"]["ranged" if ranged else "melee"]
                amt += pct / 100.0 * sheet["hp"]
            deal(amt, oh["damageType"], oh.get("source", "onhit"))
        drv.attack_riders(e)
        for oh in fx["onhitCurrentHp"]:
            pct = oh["rangedPct"] if ranged else oh["meleePct"]
            deal(pct / 100.0 * max(st["hp"], 0.0), oh["damageType"], "botrk")

    def do_attack():
        t = st["t"]
        st["attacks"] += 1
        if fx["navoriCdr"]:  # on-attack: shave 15% off remaining basic CDs
            for key in drv.basic_cd_keys:
                if st[key] > t:
                    st[key] = t + (st[key] - t) * (1.0 - fx["navoriCdr"] / 100.0)
        if fx["flurry"]:
            f = fx["flurry"]
            if t >= st["flurry_ready"]:
                st["flurry_until"] = t + f["durationS"]
                st["flurry_ready"] = t + f["cooldownS"]
            # on-hit refund (1s, 2s on crit -> EV blend) pulls the next window in
            st["flurry_ready"] -= (f["refundOnHitS"]
                                   + crit_c * f["refundCritExtraS"])
        # on-attack stack machinery first (pre-hit state decides the procs)
        phantom_now = False
        if fx["phantom"] and st["phantom"] >= fx["phantom"]["stacksNeeded"]:
            phantom_now, st["phantom"] = True, 0
        kraken_now = False
        if fx["kraken"] and st["kraken"] >= 2:
            kraken_now, st["kraken"] = True, 0
        elif fx["kraken"]:
            st["kraken"] += 1
        if fx["asStacking"]:
            st["seething"] = min(st["seething"] + 1, fx["asStacking"]["maxStacks"])
            # the consuming attack grants no Phantom stack: Riot's tooltip is
            # "every third Attack" at full stacks, not every other
            if (fx["phantom"] and not phantom_now
                    and st["seething"] == fx["asStacking"]["maxStacks"]):
                st["phantom"] = min(st["phantom"] + 1, fx["phantom"]["stacksNeeded"])
        if fx["altPen"]:
            if st["attacks"] % 2 == 0:  # every other hit is a Dark hit
                st["dark"] = min(st["dark"] + 1, fx["altPen"]["maxStacks"])
        drv.before_attack(e)

        floor = 1.0
        if fx["firstAttackCritFloorEv"] and not st["sundered_used"]:
            st["sundered_used"] = True  # Sundered Sky: once per target
            floor = fx["firstAttackCritFloorEv"]
        if (fx["ultAttackSteroid"]
                and st["post_r_attacks"] < fx["ultAttackSteroid"]["attacks"]):
            floor = max(floor, fx["ultAttackSteroid"].get("critFloorEv", 1.0))
        deal(sheet["ad"], "physical", "auto", crit_mod=True, ev_floor=floor)
        apply_onhits()
        if fx["energized"]:
            # Energize: 6 stacks per attack (+ item bonuses) plus 1 per 24
            # units moved — assumed kiting at full move speed between attacks
            st["energize"] += (t - st["en_last"]) * sheet["move_speed"] / 24.0
            st["en_last"] = t
            st["energize"] += 6.0 + sum(e.get("extraStacksPerAttack", 0.0)
                                        for e in fx["energized"])
            if st["energize"] >= 100.0:
                st["energize"] -= 100.0
                for en in fx["energized"]:
                    deal(en["bonus"], en["damageType"],
                         en.get("source", "energized"))
        eclipse_hit()
        if fx["nthHitProc"]:  # Hullbreaker's Skipper
            nh = fx["nthHitProc"]
            need = nh["stacksNeededRanged"] if ranged else nh["stacksNeededMelee"]
            if st["nth"] >= need:
                st["nth"] = 0
                ratio = nh["baseAdRatioRanged"] if ranged else nh["baseAdRatioMelee"]
                hp_pct = nh["selfMaxHpPctRanged"] if ranged else nh["selfMaxHpPctMelee"]
                deal(ratio * sheet["ad_base"] + hp_pct / 100.0 * sheet["hp"],
                     nh["damageType"], "hullbreaker")
            else:
                st["nth"] += 1
        if fx["firstAttackBonus"] and st["attacks"] == 1:
            fb = fx["firstAttackBonus"]  # Umbral: opens from unseen
            deal(fb["base"] + fb["perLethality"] * sheet["lethality"],
                 fb["damageType"], fb.get("source", "opener"))
        st["post_r_attacks"] += 1
        if st["sb_primed"]:
            sb = fx["spellblade"]
            deal(sb["baseAdRatio"] * sheet["ad_base"]
                 + sb["apRatio"] * sheet["ap"]
                 + sb.get("perCritChancePct", 0.0) * sheet["crit_chance"],
                 sb["damageType"], "spellblade")
            st["sb_primed"] = False
            st["sb_icd_until"] = t + sb["icdS"]
            if sb.get("reapplyOnhit"):  # Dusk and Dawn: on-hits land twice
                apply_onhits()
        if kraken_now:
            k = fx["kraken"]
            base = by_level(k["baseByLevel"], level)
            if ranged:
                base *= k["rangedMult"]
            amp_max = k["missingHpAmpMaxPct"]["ranged" if ranged else "melee"]
            missing = 1.0 - max(st["hp"], 0.0) / target_hp
            deal(base * (1.0 + amp_max / 100.0 * missing), k["damageType"], "kraken")
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

    while True:
        cand = [(st["next_attack"], "attack", 0), (st["next_mal"], "mal", 0)]
        for i, b in enumerate(st["burns"]):
            cand.append((b["next"], "burn", i))
        if st["ss_at"] != INF:
            cand.append((st["ss_at"], "ss", 0))
        if st["r_impact"] != INF:
            cand.append((st["r_impact"], "r", 0))
        q_at = drv.q_at(e)
        if q_at != INF:  # so Q casts the moment it's ready, not at the next event
            cand.append((q_at, "q", 0))
        for t_ev, kind in drv.events(e):
            cand.append((t_ev, kind, 0))
        t_next, kind, idx = min(cand)
        if t_next > duration or st["hp"] <= 0:
            break
        st["t"] = t_next
        if drv.q_at(e) <= st["t"]:
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
                       target_bonus_hp=target_bonus_hp, _blend=False)
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
CACHED_ROWS = 250  # rows kept per cell (all the enumerator keeps)

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
    cells of a tier sit side by side. The full-build tier simulates ~30M
    builds against each of its targets and scales with the fight lengths:
    about half an hour per champion on 16 cores."""
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
    return math.exp(sum(math.log(max(x, 1e-9)) for x in xs) / len(xs))


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


def compute_tier(slug, tier, paths):
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
        candidates=champion_pool(kit, effects), overall=overall)
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
            outs = compute_tier(slug, tier, paths)
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
# Berserker's Greaves, Sorcerer's Shoes. Tier-3 upgrades (Spellslinger's et
# al) are Feats-of-Strength-gated, so they're out by assumption — see
# "excluded" in item-effects.json.
BOOTS = [3006, 3020]


_ENUM_CTX = None  # set before forking workers; children inherit via fork


def _keep_best(lst, keep):
    """Bound a running result list: sort and cut it to `keep` once it has
    grown past 4x that, so memory stays flat over millions of builds."""
    if len(lst) > 4 * keep:
        lst.sort(key=lambda x: x[0])
        del lst[keep:]


def _enum_batch(ctx, boots, size, start, step):
    """Simulate every `step`-th combination of `size` items (offset `start`)
    with `boots` against each of ctx["targets"]; returns ({key: top-`keep`
    results}, simulated count) — one list per target, ranked by rank_key,
    and, if ctx["overall"] names one, a list under that key ranking every
    build on all targets at once (overall_key)."""
    import itertools
    keep, targets, overall = ctx["keep"], ctx["targets"], ctx["overall"]
    out = {k: [] for k in targets}
    if overall:
        out[overall] = []
    n = 0
    groups, caps = ctx["groups"], ctx["caps"]
    for combo in itertools.islice(
            itertools.combinations(ctx["free"], size), start, None, step):
        ids = [boots, *ctx["required"], *combo]
        if not build_is_legal(ids, groups, caps):
            continue
        if ctx["budget"] and sum(ctx["price"][i] for i in ids) > ctx["budget"]:
            continue
        sheet = resolve_stats(ctx["champ"], ctx["level"], ids, ctx["pool"],
                              ctx["effects"], kit=ctx["kit"])
        fx = merge_effects(ids, ctx["effects"])
        rs = {k: simulate(sheet, ctx["kit"], fx, ctx["level"], ctx["ranks"],
                          t["targetHp"], t["armor"], t["mr"], t["duration"],
                          use_ult=ctx["use_ult"], prestacked=ctx["prestacked"],
                          target_bonus_hp=t.get("targetBonusHp", 0.0))
              for k, t in targets.items()}
        n += 1
        for k in targets:
            out[k].append((rank_key(rs[k]), ids, sheet, rs))
            _keep_best(out[k], keep)
        if overall:
            out[overall].append((overall_key(rs, targets), ids, sheet, rs))
            _keep_best(out[overall], keep)
    return out, n


def _enum_worker(task):
    boots, size, start, step = task
    return _enum_batch(_ENUM_CTX, boots, size, start, step)


def enumerate_builds(champ, pool, effects, kit, level, ranks, targets,
                     budget=None, slots=6, required=(), candidates=None,
                     use_ult=True, prestacked=False, keep=250, overall=None):
    """Simulate every boots + item combination against each target in one
    pass — `targets` is {key: scenario-shaped dict: targetHp, armor, mr,
    duration, targetBonusHp}. Returns ({key: results}, count): per target
    the top-`keep` (ids, sheet, {target key: fight result}) best-first by
    rank_key, plus, with `overall` set, a list under that key ranked by
    overall_key across every target; and how many builds were simulated.
    Large pools fan out across CPU cores (fork)."""
    free = [i for i in (candidates or DEFAULT_POOL) if i not in required]
    n_free = slots - 1 - len(required)  # one slot is always boots
    if n_free < 0:
        sys.exit("more required items than slots allow")
    sizes = list(range(n_free + 1)) if budget else [n_free]
    _groups, _caps = load_exclusive_groups()
    ctx = dict(
        champ=champ, pool=pool, effects=effects, kit=kit, level=level,
        ranks=ranks, targets=dict(targets), overall=overall, budget=budget,
        required=list(required), free=free, use_ult=use_ult,
        prestacked=prestacked, keep=keep, groups=_groups, caps=_caps,
        price={i: pool[i]["shop"]["prices"]["total"]
               for i in set(free) | set(required) | set(BOOTS)})
    combos = len(BOOTS) * sum(math.comb(len(free), s) for s in sizes)
    workers = max(1, (os.cpu_count() or 2) - 1)
    keys = list(targets) + ([overall] if overall else [])
    lists, count = {k: [] for k in keys}, 0
    if combos > 50_000 and workers > 1 and hasattr(os, "fork"):
        import multiprocessing as mp
        global _ENUM_CTX
        _ENUM_CTX = ctx
        try:
            with mp.get_context("fork").Pool(workers) as p:
                tasks = [(b, s, w, workers) for b in BOOTS for s in sizes
                         for w in range(workers)]
                batches = p.map(_enum_worker, tasks)
        finally:
            _ENUM_CTX = None
    else:
        batches = [_enum_batch(ctx, b, s, 0, 1) for b in BOOTS for s in sizes]
    for out, n in batches:
        for k in keys:
            lists[k] += out[k]
        count += n
    for k in keys:
        lists[k].sort(key=lambda x: x[0])
        del lists[k][keep:]
        lists[k] = [(ids, sheet, rs) for _, ids, sheet, rs in lists[k]]
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
