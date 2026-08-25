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
from common import DATA_DIR, patch_key

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
        return m["patch"], pool
    sys.exit("No meraki item snapshot — run `lol.py items fetch` first.")


def load_item_effects():
    if not os.path.exists(ITEM_EFFECTS_PATH):
        return {}
    with open(ITEM_EFFECTS_PATH) as f:
        return {int(k): v for k, v in json.load(f)["items"].items()}


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
        ap_mult += fx.get("apMult", 0.0)
        covered = set(fx.get("covers", []))
        uncovered += [f"{p['name']} ({it['name']})" for p in it["passives"]
                      if p.get("name") and p["name"] not in covered]
    agg.update(pct_pen_multi)
    # Riftmaker's Void Infusion: AP from bonus (item) health, before Rabadon
    if item_ids and any(effects.get(i, {}).get("apFromBonusHpPct") for i in item_ids):
        pct = sum(effects.get(i, {}).get("apFromBonusHpPct", 0.0) for i in item_ids)
        agg["ap_flat"] += pct / 100.0 * agg["hp"]

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
        "hp": stat_at(dd["hp"], dd["hpperlevel"], level) + agg["hp"],
        "mana": stat_at(dd["mp"], dd["mpperlevel"], level) + agg["mana"],
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


def merge_effects(item_ids, effects):
    fx = {"onhit": [], "onhitCurrentHp": [], "dmgAmps": [], "spellblade": None,
          "asStacking": None, "phantom": None, "kraken": None, "altPen": None,
          "burn": None, "magicCrit": None, "ultBurn": None}
    for iid in item_ids:
        e = effects.get(iid, {})
        fx["onhit"] += e.get("onhit", [])
        if "onhitCurrentHp" in e:
            fx["onhitCurrentHp"].append(e["onhitCurrentHp"])
        if "dmgAmp" in e:
            fx["dmgAmps"].append(e["dmgAmp"])
        for k in ("spellblade", "asStacking", "phantom", "kraken", "altPen",
                  "burn", "magicCrit", "ultBurn"):
            if k in e:
                fx[k] = e[k]
    return fx


def simulate(sheet, kit, fx, level, ranks, target_hp, target_armor, target_mr,
             duration, use_ult=True, prestacked=False):
    """One fight vs a stat dummy. Returns totals, DPS, time-to-kill (None if
    the dummy survives), and a per-source damage breakdown."""
    INF = float("inf")
    ranged = level >= kit["passive"]["arisen"]["level"]
    zeal_cfg = kit["passive"]["zealous"]
    zeal_perm = level >= zeal_cfg["permanentAtLevel"]
    aflame = level >= kit["passive"]["aflame"]["level"]
    wave_cfg = kit["passive"]["aflame"]["wave"]
    e_cfg, q_cfg, r_cfg = (kit["abilities"][s] for s in ("E", "Q", "R"))

    crit_c = sheet["crit_chance"] / 100.0
    crit_ev = 1.0 + crit_c * (sheet["crit_damage"] / 100.0 - 1.0)

    st = {
        "t": 0.0, "hp": float(target_hp),
        "zeal": zeal_cfg["maxStacks"] if (zeal_perm or prestacked) else 0,
        "seething": 0, "phantom": 0, "kraken": 0, "dark": 0, "attacks": 0,
        "phantom_hits": 0,
        "q_ready": 0.0, "e_ready": 0.0, "e_pending": False,
        "q_shred_until": -1.0, "mal_shred_until": -1.0,
        "burn_until": -1.0, "next_burn": INF,
        "mal_until": -1.0, "next_mal": INF, "mal_tick": 0.0,
        "sb_primed": False, "sb_icd_until": -1.0,
        "r_impact": INF,
        "next_attack": 0.0,
        "breakdown": {}, "total": 0.0, "ttk": None,
    }

    def attack_speed():
        bonus = sheet["bonus_as_pct"] + st["zeal"] * zeal_cfg["asPctPerStack"]
        if fx["asStacking"]:
            bonus += st["seething"] * fx["asStacking"]["pctPerStack"]
        return min(sheet["base_as"] + sheet["as_ratio"] * bonus / 100.0, AS_CAP)

    def deal(amount, dtype, source, crit_mod=False, ability=False):
        t = st["t"]
        amp = 1.0
        for a in fx["dmgAmps"]:
            amp *= 1.0 + a["pctPerStack"] / 100.0 * min(a["maxStacks"], int(t))
        dark_pen = 0.0
        if fx["altPen"]:
            dark_pen = min(st["dark"], fx["altPen"]["maxStacks"]) \
                       * fx["altPen"]["pctPerStack"]
        qs = 15.0 if t < st["q_shred_until"] else 0.0
        if dtype == "physical":
            r = eff_resist(target_armor, 0.0, qs,
                           stack_pct_pen(sheet["armor_pen_pct"], dark_pen),
                           sheet["lethality"])
        else:
            mal = (fx["ultBurn"]["mrReduction"]
                   if fx["ultBurn"] and t < st["mal_shred_until"] else 0.0)
            r = eff_resist(target_mr, mal, qs,
                           stack_pct_pen(sheet["magic_pen_pct"], dark_pen),
                           sheet["magic_pen_flat"])
        ev = 1.0
        if crit_mod:
            ev = crit_ev
        elif (dtype == "magic" and fx["magicCrit"]
              and st["hp"] / target_hp * 100.0
                  < fx["magicCrit"]["belowTargetHpPct"]):
            ev = 1.0 + crit_c * (fx["magicCrit"]["critDmgPct"] / 100.0)
        dmg = amount * amp * resist_mult(r) * ev
        st["hp"] -= dmg
        st["total"] += dmg
        st["breakdown"][source] = st["breakdown"].get(source, 0.0) + dmg
        if ability and fx["burn"]:
            if st["next_burn"] == INF:
                st["next_burn"] = t + fx["burn"]["tickS"]
            st["burn_until"] = t + fx["burn"]["durationS"]
        if st["hp"] <= 0 and st["ttk"] is None:
            st["ttk"] = t

    def prime_spellblade():
        if fx["spellblade"] and st["t"] >= st["sb_icd_until"]:
            st["sb_primed"] = True

    def cast_q():
        st["q_ready"] = st["t"] + q_cfg["cooldownS"][ranks["Q"] - 1] * sheet["cd_mult"]
        st["q_shred_until"] = st["t"] + q_cfg["shred"]["durationS"]
        deal(ability_hit(q_cfg["damage"], ranks["Q"], sheet), "magic", "Q",
             ability=True)
        prime_spellblade()
        st["next_attack"] = max(st["next_attack"], st["t"]) + ABILITY_LOCKOUT_S

    def apply_onhits():
        """Everything riding a basic attack hit (reapplied by a phantom hit)."""
        for oh in fx["onhit"]:
            deal(oh["base"] + oh.get("apRatio", 0.0) * sheet["ap"]
                 + oh.get("bonusAdRatio", 0.0) * sheet["ad_bonus"],
                 oh["damageType"], "onhit")
        if ranks["E"]:
            deal(ability_hit(e_cfg["onhit"], ranks["E"], sheet), "magic", "E onhit")
        for oh in fx["onhitCurrentHp"]:
            pct = oh["rangedPct"] if ranged else oh["meleePct"]
            deal(pct / 100.0 * max(st["hp"], 0.0), oh["damageType"], "botrk")

    def do_attack():
        t = st["t"]
        st["attacks"] += 1
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
            if fx["phantom"] and st["seething"] == fx["asStacking"]["maxStacks"]:
                st["phantom"] = min(st["phantom"] + 1, fx["phantom"]["stacksNeeded"])
        if fx["altPen"]:
            if st["attacks"] % 2 == 0:  # every other hit is a Dark hit
                st["dark"] = min(st["dark"] + 1, fx["altPen"]["maxStacks"])
        if not zeal_perm:
            st["zeal"] = min(st["zeal"] + 1, zeal_cfg["maxStacks"])

        deal(sheet["ad"], "physical", "auto", crit_mod=True)
        apply_onhits()
        if st["sb_primed"]:
            sb = fx["spellblade"]
            deal(sb["baseAdRatio"] * sheet["ad_base"]
                 + sb["apRatio"] * sheet["ap"], sb["damageType"], "spellblade")
            st["sb_primed"] = False
            st["sb_icd_until"] = t + sb["icdS"]
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
            st["e_ready"] = t + e_cfg["cooldownS"][ranks["E"] - 1] * sheet["cd_mult"]
            prime_spellblade()
            st["next_attack"] = t + kit["attack"]["windupFraction"] / attack_speed()
        else:
            st["next_attack"] = t + 1.0 / attack_speed()

    # opening casts at t=0, before the first auto
    if use_ult and ranks["R"]:
        st["r_impact"] = r_cfg["delayS"]
        prime_spellblade()
        st["next_attack"] += ABILITY_LOCKOUT_S

    while True:
        cand = [(st["next_attack"], "attack"), (st["next_burn"], "burn"),
                (st["next_mal"], "mal")]
        if st["r_impact"] != INF:
            cand.append((st["r_impact"], "r"))
        if ranks["Q"]:  # so Q casts the moment it's ready, not at the next event
            cand.append((max(st["q_ready"], st["t"]), "q"))
        t_next, kind = min(cand)
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
            deal(fx["burn"]["maxHpPctTotal"] / (fx["burn"]["durationS"]
                 / fx["burn"]["tickS"]) / 100.0 * target_hp,
                 fx["burn"]["damageType"], "burn")
            st["next_burn"] = (st["t"] + fx["burn"]["tickS"]
                               if st["t"] + fx["burn"]["tickS"] <= st["burn_until"]
                               else INF)
        elif kind == "mal":
            deal(st["mal_tick"], "magic", "malignance")
            st["next_mal"] = (st["t"] + 0.25
                              if st["t"] + 0.25 <= st["mal_until"] else INF)
        elif kind == "r":
            st["r_impact"] = INF
            deal(ability_hit(r_cfg["damage"], ranks["R"], sheet), "magic", "R",
                 ability=True)
            if fx["ultBurn"]:
                ub = fx["ultBurn"]
                ticks = ub["durationS"] / 0.25
                st["mal_tick"] = (ub["totalBase"]
                                  + ub["totalApRatio"] * sheet["ap"]) / ticks
                st["mal_until"] = st["t"] + ub["durationS"]
                st["mal_shred_until"] = st["t"] + ub["durationS"]
                st["next_mal"] = st["t"] + 0.25

    fight = min(duration, st["ttk"]) if st["ttk"] is not None else duration
    return {"total": st["total"], "dps": st["total"] / fight if fight else 0.0,
            "ttk": st["ttk"], "attacks": st["attacks"],
            "phantom_hits": st["phantom_hits"], "hp_left": max(st["hp"], 0.0),
            "breakdown": dict(sorted(st["breakdown"].items(),
                                     key=lambda kv: -kv[1]))}


# ---------------------------------------------------------------------------
# web API: preset scenarios the dashboard can show (and export can pre-bake)
# ---------------------------------------------------------------------------

SCENARIOS = {
    "full-squishy": dict(label="Full build vs squishy", level=16, targetHp=2800,
                         armor=80, mr=60, duration=8),
    "full-bruiser": dict(label="Full build vs bruiser", level=16, targetHp=3800,
                         armor=150, mr=100, duration=12),
    "full-tank": dict(label="Full build vs tank", level=16, targetHp=4800,
                      armor=220, mr=160, duration=15),
    "mid-squishy": dict(label="Mid-game 7.5k vs squishy", level=11, targetHp=2200,
                        armor=60, mr=45, duration=8, budget=7500),
    "mid-tank": dict(label="Mid-game 7.5k vs tank", level=11, targetHp=3800,
                     armor=160, mr=110, duration=12, budget=7500),
    "first-item": dict(label="First item 4.5k spike", level=9, targetHp=1900,
                       armor=50, mr=40, duration=8, budget=4500),
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
    return {
        "champions": champs,
        "scenarios": [{"key": k, **v} for k, v in SCENARIOS.items()],
        "note": "Theoretical damage model — deterministic sim, expected-value "
                "crit, damage only. Runes are not modeled yet.",
    }


_OPTIMIZE_CACHE = {}


def api_optimize_scenario(slug, key, top=20):
    """Ranked builds for one preset scenario; cached per serve process (the
    inputs only change when snapshots or encodings do — restart to refresh)."""
    if (slug, key) in _OPTIMIZE_CACHE:
        return _OPTIMIZE_CACHE[(slug, key)]
    if key not in SCENARIOS:
        raise ValueError(f"unknown scenario '{key}'")
    sc = SCENARIOS[key]
    champ = load_champion(slug)
    patch, pool = load_items()
    effects = load_item_effects()
    kit = load_kit(slug)
    ranks = skill_ranks(sc["level"])
    results = enumerate_builds(
        champ, pool, effects, kit, sc["level"], ranks, sc["targetHp"],
        sc["armor"], sc["mr"], sc["duration"], budget=sc.get("budget"))
    rows = []
    for n, (ids, sheet, r) in enumerate(results[:top], 1):
        rows.append({
            "rank": n, "items": [pool[i]["name"] for i in ids],
            "gold": sheet["gold"],
            "ttk": round(r["ttk"], 2) if r["ttk"] is not None else None,
            "dps": round(r["dps"]), "total": round(r["total"]),
            "attacks": r["attacks"],
            "ap": round(sheet["ap"]), "ad": round(sheet["ad"]),
            "attackSpeed": round(sheet["attack_speed"], 2),
            "breakdown": {k: round(v) for k, v in r["breakdown"].items()},
        })
    out = {"champion": slug, "scenario": {"key": key, **sc},
           "itemsPatch": patch, "championPatch": champ["meta"]["patch"],
           "kitPatch": kit.get("patch"), "buildsEvaluated": len(results),
           "ranks": ranks, "rows": rows}
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
                 use_ult=not args.no_ult, prestacked=args.prestacked)

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
# (or that is pure stats). Items with UNMODELED damage passives (Horizon
# Focus, Stormsurge) are deliberately absent — including them would rank
# them on stats alone and misrank them downward.
DEFAULT_POOL = [
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
    3124,  # Guinsoo's Rageblade
    3091,  # Wit's End
    3302,  # Terminus
    6672,  # Kraken Slayer
    3153,  # Blade of the Ruined King
]
BOOTS = [3006, 3020]  # Berserker's Greaves, Sorcerer's Shoes


def enumerate_builds(champ, pool, effects, kit, level, ranks, target_hp,
                     armor, mr, duration, budget=None, slots=6, required=(),
                     candidates=None, use_ult=True, prestacked=False):
    """Simulate every boots + item combination; return (ids, sheet, result)
    sorted best-first — by time-to-kill when the dummy dies, else by damage."""
    import itertools
    free = [i for i in (candidates or DEFAULT_POOL) if i not in required]
    n_free = slots - 1 - len(required)  # one slot is always boots
    if n_free < 0:
        sys.exit("more required items than slots allow")
    sizes = range(n_free + 1) if budget else [n_free]
    results = []
    for boots in BOOTS:
        for size in sizes:
            for combo in itertools.combinations(free, size):
                ids = [boots, *required, *combo]
                sheet = resolve_stats(champ, level, ids, pool, effects)
                if budget and sheet["gold"] > budget:
                    continue
                r = simulate(sheet, kit, merge_effects(ids, effects),
                             level, ranks, target_hp, armor, mr, duration,
                             use_ult=use_ult, prestacked=prestacked)
                key = ((0, r["ttk"]) if r["ttk"] is not None
                       else (1, -r["total"]))
                results.append((key, ids, sheet, r))
    results.sort(key=lambda x: x[0])
    return [(ids, sheet, r) for _, ids, sheet, r in results]


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
    results = enumerate_builds(
        champ, pool, effects, kit, args.level, ranks, args.target_hp,
        args.armor, args.mr, args.duration, budget=args.budget,
        slots=args.slots, required=required, candidates=candidates,
        use_ult=not args.no_ult, prestacked=args.prestacked)

    print(f"{champ['dd']['name']} lvl {args.level} — {len(results)} builds "
          f"in {time.time() - t0:.1f}s, {args.duration:g}s vs "
          f"{args.target_hp}hp {args.armor}armor {args.mr}mr"
          + (f", budget {args.budget}g" if args.budget else "") + "\n")
    print(f"  {'#':>3} {'ttk':>6} {'dps':>6} {'total':>7} {'gold':>6}  items")
    for n, (ids, sheet, r) in enumerate(results[:args.top], 1):
        names = ", ".join(pool[i]["name"].split()[0] for i in ids)
        ttk = f"{r['ttk']:.2f}" if r["ttk"] is not None else "-"
        print(f"  {n:>3} {ttk:>6} {r['dps']:>6.0f} {r['total']:>7.0f} "
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
