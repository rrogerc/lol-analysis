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

import json
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

def load_kit(slug):
    path = os.path.join(BUILDS_DATA_DIR, f"{slug}.json")
    if not os.path.exists(path):
        sys.exit(f"No kit encoding at data/builds/{slug}.json — only "
                 f"hand-encoded champions can be simulated.")
    with open(path) as f:
        return json.load(f)


def by_level(spec, level):
    """Evaluate a {'from', 'to', 'curve': 'linear', 'levels': [lo, hi]}
    level-scaled value ('20 - 41 (based on level)' on the wiki)."""
    lo, hi = spec["levels"]
    frac = (min(max(level, lo), hi) - lo) / (hi - lo)
    return spec["from"] + (spec["to"] - spec["from"]) * frac


def ability_hit(dmg, rank, sheet):
    """Raw (pre-mitigation) damage of one ability hit at `rank` (1-5)."""
    return (dmg["base"][rank - 1]
            + dmg.get("bonusAdRatio", 0.0) * sheet["ad_bonus"]
            + dmg.get("adRatio", 0.0) * sheet["ad"]
            + dmg.get("apRatio", 0.0) * sheet["ap"])


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


def resolve_stats(champ, level, item_ids, pool, effects=None):
    """Champion base stats at `level` plus `item_ids` -> final stat sheet.

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

    ap = agg["ap_flat"] * ap_mult
    haste = agg["haste"]
    sheet = {
        "champion": champ["slug"], "level": level, "gold": gold,
        "items": names,
        "ad_base": stat_at(dd["attackdamage"], dd["attackdamageperlevel"], level),
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
        "hp": stat_at(dd["hp"], dd["hpperlevel"], level) + agg["hp"],
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
# Approximations (all chosen to preserve build RANKING, not absolute DPS):
# - crit is expected value, no RNG anywhere
# - projectiles land instantly; the target never moves or acts
# - Q/R have a flat 0.25s cast lockout that delays the next auto
# - an E reset lands its empowered attack after one windup (windup/AS)
# - stacking buffs (Zeal, Seething, Terminus) never expire mid-fight
# - range-scaled amps (Hexoptics) assume every attack is made at max range
# ---------------------------------------------------------------------------

ABILITY_LOCKOUT_S = 0.25


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


def simulate(sheet, kit, fx, level, ranks, target_hp, target_armor, target_mr,
             duration, use_ult=True, prestacked=False, target_bonus_hp=0.0,
             _blend=True):
    """One fight vs a stat dummy. Returns totals, DPS, time-to-kill (None if
    the dummy survives), and a per-source damage breakdown."""
    INF = float("inf")
    ranged = level >= kit["passive"]["arisen"]["level"]
    zeal_cfg = kit["passive"]["zealous"]
    zeal_perm = level >= zeal_cfg["permanentAtLevel"]
    aflame = level >= kit["passive"]["aflame"]["level"]
    wave_cfg = kit["passive"]["aflame"]["wave"]
    e_cfg, q_cfg, r_cfg = (kit["abilities"][s] for s in ("E", "Q", "R"))
    q_shred = q_cfg.get("shred", {})
    q_shred_pct = {res: q_shred.get("pct", 0.0)
                   for res in q_shred.get("appliesTo", ())}

    crit_c = sheet["crit_chance"] / 100.0
    crit_ev = 1.0 + crit_c * (sheet["crit_damage"] / 100.0 - 1.0)

    # Attack range for this level: form passives (Kayle's Arisen/Transcendent)
    # override the champion's base range.
    atk_range = sheet["base_attack_range"]
    for form in ("arisen", "transcendent"):
        f = kit["passive"].get(form, {})
        if "attackRange" in f and level >= f["level"]:
            atk_range = f["attackRange"]
    # Hexoptics' Magnification: scales with distance to the target, capped.
    # We assume attacks are made at max range (see approximations above).
    auto_amp = 1.0
    if fx["attackAmp"]:
        aa = fx["attackAmp"]
        auto_amp += (aa["maxPct"] / 100.0
                     * min(1.0, atk_range / aa["maxAtRange"]))

    st = {
        "t": 0.0, "hp": float(target_hp),
        "zeal": zeal_cfg["maxStacks"] if (zeal_perm or prestacked) else 0,
        "seething": 0, "phantom": 0, "kraken": 0, "dark": 0, "attacks": 0,
        "phantom_hits": 0,
        "q_ready": 0.0, "e_ready": 0.0, "e_pending": False,
        "q_shred_until": -1.0, "mal_shred_until": -1.0,
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
    if fx["manaActive"]:  # Actualizer: cast on engage, empowered for 8s
        st["ma_until"] = fx["manaActive"]["durationS"]

    def attack_speed():
        bonus = sheet["bonus_as_pct"] + st["zeal"] * zeal_cfg["asPctPerStack"]
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
        for a in fx["dmgAmps"]:
            amp *= 1.0 + a["pctPerStack"] / 100.0 * min(a["maxStacks"], int(t))
        for a in fx["flatAmps"]:  # Abyssal Mask: always-on, one damage type
            if a["damageType"] in (dtype, "all"):
                amp *= 1.0 + a["pct"] / 100.0
        if fx["giantSlayer"]:  # 1% per 100 target bonus HP, capped
            amp *= 1.0 + min(fx["giantSlayer"]["maxPct"],
                             target_bonus_hp / 100.0) / 100.0
        if fx["hypershot"] and t < st["hz_until"]:
            amp *= 1.0 + fx["hypershot"]["ampPct"] / 100.0
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
        qs_on = t < st["q_shred_until"]
        if dtype == "physical":
            shred = q_shred_pct.get("armor", 0.0) if qs_on else 0.0
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
            shred = q_shred_pct.get("mr", 0.0) if qs_on else 0.0
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

    def cast_q():
        st["q_ready"] = st["t"] + basic_cd(q_cfg["cooldownS"][ranks["Q"] - 1])
        st["q_shred_until"] = st["t"] + q_shred.get("durationS", 0.0)
        deal(ability_hit(q_cfg["damage"], ranks["Q"], sheet), "magic", "Q",
             ability=True)
        ability_cast_proc()
        eclipse_hit()
        prime_spellblade()
        st["next_attack"] = max(st["next_attack"], st["t"]) + ABILITY_LOCKOUT_S

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
        if ranks["E"]:
            deal(ability_hit(e_cfg["onhit"], ranks["E"], sheet), "magic", "E onhit")
        for oh in fx["onhitCurrentHp"]:
            pct = oh["rangedPct"] if ranged else oh["meleePct"]
            deal(pct / 100.0 * max(st["hp"], 0.0), oh["damageType"], "botrk")

    def do_attack():
        t = st["t"]
        st["attacks"] += 1
        if fx["navoriCdr"]:  # on-attack: shave 15% off remaining basic CDs
            for key in ("q_ready", "e_ready"):
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
        if not zeal_perm:
            st["zeal"] = min(st["zeal"] + 1, zeal_cfg["maxStacks"])

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
                for e in fx["energized"]:
                    deal(e["bonus"], e["damageType"],
                         e.get("source", "energized"))
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
        if st["e_pending"]:
            pct = (e_cfg["active"]["missingHpPct"][ranks["E"] - 1]
                   + e_cfg["active"]["missingHpPctPer100Ap"] * sheet["ap"] / 100.0)
            deal(pct / 100.0 * (target_hp - max(st["hp"], 0.0)), "magic",
                 "E active", crit_mod=aflame, ability=True)
            ability_cast_proc()
            eclipse_hit()
            st["e_pending"] = False
        if aflame and st["zeal"] >= zeal_cfg["maxStacks"]:
            deal(by_level(wave_cfg["baseByLevel"], level)
                 + wave_cfg["bonusAdRatio"] * sheet["ad_bonus"]
                 + wave_cfg["apRatio"] * sheet["ap"],
                 "magic", "wave", crit_mod=True, ability=True)
        if phantom_now:
            st["phantom_hits"] += 1
            apply_onhits()

        # E weave: cast right after an auto to use the attack reset
        if ranks["E"] and t >= st["e_ready"] and not st["e_pending"]:
            st["e_pending"] = True
            st["e_ready"] = t + basic_cd(e_cfg["cooldownS"][ranks["E"] - 1])
            prime_spellblade()
            st["next_attack"] = t + kit["attack"]["windupFraction"] / attack_speed()
        else:
            st["next_attack"] = t + 1.0 / attack_speed()

    # opening casts at t=0, before the first auto
    if use_ult and ranks["R"]:
        st["r_impact"] = r_cfg["delayS"]
        prime_spellblade()
        st["next_attack"] += ABILITY_LOCKOUT_S
        if fx["onUltCast"]:  # Hexplate Overdrive starts on cast
            st["hex_until"] = fx["onUltCast"]["durationS"]
        if fx["ultAttackSteroid"]:  # Fiendhunter's next-3-attacks window
            st["post_r_attacks"] = 0
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
        if ranks["Q"]:  # so Q casts the moment it's ready, not at the next event
            cand.append((max(st["q_ready"], st["t"]), "q", 0))
        t_next, kind, idx = min(cand)
        if t_next > duration or st["hp"] <= 0:
            break
        st["t"] = t_next
        if ranks["Q"] and st["t"] >= st["q_ready"]:
            cast_q()
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
# web API: preset scenarios the dashboard can show (and export can pre-bake)
# ---------------------------------------------------------------------------

# targetBonusHp: the item/rune share of the dummy's HP (drives Giant Slayer)
SCENARIOS = {
    "full-squishy": dict(label="Full build vs squishy", level=16, targetHp=2800,
                         armor=110, mr=60, duration=8, targetBonusHp=800),
    "full-bruiser": dict(label="Full build vs bruiser", level=16, targetHp=3800,
                         armor=180, mr=120, duration=12, targetBonusHp=1500),
    "full-tank": dict(label="Full build vs tank", level=16, targetHp=4800,
                      armor=220, mr=160, duration=15, targetBonusHp=1500),
    "mid-squishy": dict(label="Mid-game 7.5k vs squishy", level=11, targetHp=2200,
                        armor=60, mr=45, duration=8, budget=7500,
                        targetBonusHp=600),
    "mid-tank": dict(label="Mid-game 7.5k vs tank", level=11, targetHp=3800,
                     armor=160, mr=110, duration=12, budget=7500,
                     targetBonusHp=1500),
    "first-item": dict(label="First item 4.5k spike", level=9, targetHp=1900,
                       armor=50, mr=40, duration=8, budget=4500,
                       targetBonusHp=400),
}


def kit_champions():
    """Slugs with a hand-encoded kit (data/builds/<slug>.json)."""
    slugs = []
    if os.path.isdir(BUILDS_DATA_DIR):
        for fn in sorted(os.listdir(BUILDS_DATA_DIR)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(BUILDS_DATA_DIR, fn)) as f:
                if "abilities" in json.load(f):
                    slugs.append(fn[:-5])
    return slugs


def api_builds_meta():
    champs = []
    for slug in kit_champions():
        kit = load_kit(slug)
        champs.append({"slug": slug, "kitPatch": kit.get("patch")})
    patch, pool = load_items()

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


_OPTIMIZE_CACHE = {}
SCENARIO_CACHE_DIR = os.path.join(BASE_DIR, ".cache", "builds")


def _scenario_cache_path(slug, key, sc, patch, champ, pool):
    """Disk-cache filename whose hash covers every input the result depends
    on: the scenario, the pool, the loaded snapshot data (champion and item
    stats — content, not just patch labels, since the daily refresh rewrites
    a patch's snapshot in place), the item-bin rules that decide which builds
    are legal, and this module's code."""
    import hashlib
    h = hashlib.sha256()
    paths = [ITEM_EFFECTS_PATH,
             os.path.join(BUILDS_DATA_DIR, f"{slug}.json"),
             os.path.abspath(__file__)]
    gp = groups_path()
    if gp:
        paths.append(gp)
    for p in paths:
        with open(p, "rb") as f:
            h.update(f.read())
    h.update(json.dumps([patch, champ, sc, DEFAULT_POOL, BOOTS, pool],
                        sort_keys=True).encode())
    return os.path.join(SCENARIO_CACHE_DIR,
                        f"{slug}-{key}-{h.hexdigest()[:16]}.json")


# Every cache entry holds this many rows; a request for fewer is sliced on
# the way out. `top` therefore stays out of the cache key — one expensive
# result serves every caller.
CACHED_ROWS = 20


def api_optimize_scenario(slug, key, top=20):
    """Ranked builds for one preset scenario. Cached in-process AND on disk
    (.cache/builds/, keyed by a hash of all inputs) — big pools take minutes
    to simulate, and the disk cache survives serve restarts."""
    out = _optimize_scenario_cached(slug, key)
    if top >= len(out["rows"]):
        return out
    return {**out, "rows": out["rows"][:top]}


def _optimize_scenario_cached(slug, key):
    """The full CACHED_ROWS-row payload, computed at most once per input set."""
    if (slug, key) in _OPTIMIZE_CACHE:
        return _OPTIMIZE_CACHE[(slug, key)]
    if key not in SCENARIOS:
        raise ValueError(f"unknown scenario '{key}'")
    # ValueError (not load_champion's sys.exit) so the web layer can 404
    if slug not in kit_champions():
        raise ValueError(f"unknown champion '{slug}'")
    top = CACHED_ROWS
    sc = SCENARIOS[key]
    champ = load_champion(slug)
    patch, pool = load_items()
    cache_path = _scenario_cache_path(slug, key, sc, patch, champ, pool)
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            out = json.load(f)
        _OPTIMIZE_CACHE[(slug, key)] = out
        return out
    effects = load_item_effects()
    kit = load_kit(slug)
    ranks = skill_ranks(sc["level"])
    results, count = enumerate_builds(
        champ, pool, effects, kit, sc["level"], ranks, sc["targetHp"],
        sc["armor"], sc["mr"], sc["duration"], budget=sc.get("budget"),
        target_bonus_hp=sc.get("targetBonusHp", 0.0))
    rows = []
    for n, (ids, sheet, r) in enumerate(results[:top], 1):
        rows.append({
            "rank": n, "items": [pool[i]["name"] for i in ids],
            "gold": sheet["gold"],
            "ttk": round(r["ttk"], 2) if r["ttk"] is not None else None,
            "ttkExp": round(r["ttk_exp"], 2) if r["ttk_exp"] is not None else None,
            "ttkEff": round(r["ttk_eff"], 3) if r["ttk_eff"] is not None else None,
            "dps": round(r["dps"]), "total": round(r["total"]),
            "attacks": r["attacks"],
            "ap": round(sheet["ap"]), "ad": round(sheet["ad"]),
            "attackSpeed": round(sheet["attack_speed"], 2),
            "breakdown": {k: round(v) for k, v in r["breakdown"].items()},
        })
    out = {"champion": slug, "scenario": {"key": key, **sc},
           "itemsPatch": patch, "championPatch": champ["meta"]["patch"],
           "kitPatch": kit.get("patch"), "buildsEvaluated": count,
           "ranks": ranks, "rows": rows}
    os.makedirs(SCENARIO_CACHE_DIR, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    _OPTIMIZE_CACHE[(slug, key)] = out
    return out


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
    champ = load_champion(norm_name(args.name), args.patch)
    patch, pool = load_items(args.patch)
    idx = item_index(pool)
    ids = [resolve_item(pool, idx, t) for t in args.items]
    s = resolve_stats(champ, args.level, ids, pool)

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
    sheet = resolve_stats(champ, args.level, ids, pool, effects)
    kit = load_kit(slug)
    return champ, patch, pool, ids, effects, sheet, kit


def cmd_sim(args):
    champ, patch, pool, ids, effects, sheet, kit = sim_setup(args)
    ranks = skill_ranks(args.level, tuple(args.max_order.upper().split(",")))
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


def _enum_batch(ctx, boots, size, start, step):
    """Simulate every `step`-th combination of `size` items (offset `start`)
    with `boots`; returns (top-`keep` results, simulated count)."""
    import itertools
    keep = ctx["keep"]
    out, n = [], 0
    groups, caps = ctx["groups"], ctx["caps"]
    for combo in itertools.islice(
            itertools.combinations(ctx["free"], size), start, None, step):
        ids = [boots, *ctx["required"], *combo]
        if not build_is_legal(ids, groups, caps):
            continue
        if ctx["budget"] and sum(ctx["price"][i] for i in ids) > ctx["budget"]:
            continue
        sheet = resolve_stats(ctx["champ"], ctx["level"], ids, ctx["pool"],
                              ctx["effects"])
        r = simulate(sheet, ctx["kit"], merge_effects(ids, ctx["effects"]),
                     ctx["level"], ctx["ranks"], ctx["target_hp"],
                     ctx["armor"], ctx["mr"], ctx["duration"],
                     use_ult=ctx["use_ult"], prestacked=ctx["prestacked"],
                     target_bonus_hp=ctx["target_bonus_hp"])
        n += 1
        # Rank on the EXPECTED kill time (real time, plus the charge-back for
        # an execute that deterministic crit guarantees but real crit would
        # not). The interpolated time breaks ties inside an attack tick,
        # ordering builds that land on the same attack by damage to spare.
        key = ((0, r["ttk_exp"], r["ttk_eff"]) if r["ttk"] is not None
               else (1, float("inf"), -r["total"]))
        out.append((key, ids, sheet, r))
        if len(out) > 4 * keep:  # bound memory: keep only the running best
            out.sort(key=lambda x: x[0])
            del out[keep:]
    return out, n


def _enum_worker(task):
    boots, size, start, step = task
    return _enum_batch(_ENUM_CTX, boots, size, start, step)


def enumerate_builds(champ, pool, effects, kit, level, ranks, target_hp,
                     armor, mr, duration, budget=None, slots=6, required=(),
                     candidates=None, use_ult=True, prestacked=False,
                     target_bonus_hp=0.0, keep=250):
    """Simulate every boots + item combination; returns (results, count):
    the top-`keep` (ids, sheet, result) sorted best-first — by the
    interpolated time-to-kill when the dummy dies, else by damage — plus how
    many builds were simulated. Large pools fan out across CPU cores (fork)."""
    import math
    free = [i for i in (candidates or DEFAULT_POOL) if i not in required]
    n_free = slots - 1 - len(required)  # one slot is always boots
    if n_free < 0:
        sys.exit("more required items than slots allow")
    sizes = list(range(n_free + 1)) if budget else [n_free]
    _groups, _caps = load_exclusive_groups()
    ctx = dict(
        champ=champ, pool=pool, effects=effects, kit=kit, level=level,
        ranks=ranks, target_hp=target_hp, armor=armor, mr=mr,
        duration=duration, budget=budget, required=list(required), free=free,
        use_ult=use_ult, prestacked=prestacked,
        target_bonus_hp=target_bonus_hp, keep=keep,
        groups=_groups, caps=_caps,
        price={i: pool[i]["shop"]["prices"]["total"]
               for i in set(free) | set(required) | set(BOOTS)})
    combos = len(BOOTS) * sum(math.comb(len(free), s) for s in sizes)
    workers = max(1, (os.cpu_count() or 2) - 1)
    results, count = [], 0
    if combos > 50_000 and workers > 1 and hasattr(os, "fork"):
        import multiprocessing as mp
        global _ENUM_CTX
        _ENUM_CTX = ctx
        try:
            with mp.get_context("fork").Pool(workers) as p:
                tasks = [(b, s, w, workers) for b in BOOTS for s in sizes
                         for w in range(workers)]
                for out, n in p.map(_enum_worker, tasks):
                    results += out
                    count += n
        finally:
            _ENUM_CTX = None
    else:
        for b in BOOTS:
            for s in sizes:
                out, n = _enum_batch(ctx, b, s, 0, 1)
                results += out
                count += n
    results.sort(key=lambda x: x[0])
    del results[keep:]
    return [(ids, sheet, r) for _, ids, sheet, r in results], count


def cmd_optimize(args):
    import time
    slug = norm_name(args.name)
    champ = load_champion(slug, args.patch)
    patch, pool = load_items(args.patch)
    idx = item_index(pool)
    effects = load_item_effects()
    kit = load_kit(slug)
    ranks = skill_ranks(args.level, tuple(args.max_order.upper().split(",")))
    candidates = ([resolve_item(pool, idx, t) for t in args.pool]
                  if args.pool else None)
    required = [resolve_item(pool, idx, t) for t in (args.require or [])]

    t0 = time.time()
    results, count = enumerate_builds(
        champ, pool, effects, kit, args.level, ranks, args.target_hp,
        args.armor, args.mr, args.duration, budget=args.budget,
        slots=args.slots, required=required, candidates=candidates,
        use_ult=not args.no_ult, prestacked=args.prestacked,
        target_bonus_hp=args.target_bonus_hp)

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
