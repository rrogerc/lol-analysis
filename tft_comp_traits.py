"""Resolve a composition's traits for the existing unit engine.

This is separate from the champion benchmark's bare/low/high contexts. Counts
come from the selected shop champions; global bonuses reach nonmembers once,
and a Riftbeast's Alpha Mark belongs to one explicitly selected champion.
``modeled`` means some combat effect is supported, not complete coverage.
The returned limitations describe the remaining board-dependent effects.
"""

from copy import deepcopy

import tft
from tft_board import APEX_PREDATOR, MAX_BOARD_SLOTS, slots_used, trait_counts


RIFTBEAST = "DA_Riftbeast18"
SOLAR = "DA_18_Solar"
LUNAR = "DA_18_Lunar"
ECLIPSE = "DA_18_Eclipse"


# Nonmembers receive only the team share. Member trait_spec entries already
# include it, or replace it with their larger amount (Defender/Juggernaut).
_TEAM_EFFECTS = {
    "DA_18_Brawler": {"stats": {"hp": {"row": "BrawlerTeamHealth", "col": 1}}},
    "DA_18_Defender": {"stats": {
        "armor": {"row": "NonDefenderDefenseGain", "col": 1},
        "mr": {"row": "NonDefenderDefenseGain", "col": 1}}},
    "DA_Juggernaut18": {
        "durability": {"row": "TeamDurability", "minusOne": True, "scale": -1}},
    "DA_18_Invoker": {"stats": {"manaRegen": {"row": "TeamManaRegen"}}},
    "DA_18_Rapidfire": {"stats": {
        "asPct": {"row": "TeamAS", "col": 1, "minusOne": True}}},
    "DA_18_Spellweaver": {"stats": {
        "ap": {"row": "TeamwideAP", "col": 1, "scale": 100}}},
}


_UNMODELED_NOTES = {
    "DA_18_Blackthorn": "the sacrifice hex is left empty; no unit is sacrificed and no Blackthorn bonus is included",
    "DA_18_Coven": "prior Essence and earned Ability Power are set to zero; rituals and rewards are not modeled",
    "DA_18_Elderwood": "placeable plants and their combat effects are not modeled",
    "DA_18_Sprykin": "the Big Furry Friend, its Rider bonuses and its combat effects are not modeled",
    "DA_18_Rival": "prior takedown stacks, chosen evolutions and paired ability bonuses are not modeled",
    "DA_18_LuxUniqueTrait": "no Avatar variant is selected; no chosen trait or doubled trait count is inferred",
    "DA_Emerald18": "the paired ally's bonuses are not applied to another champion",
    "DA_18_Maokai_UniqueTrait": "prior permanent Health stacks are set to zero",
}


def _solar_effect(trait, column, three_stars):
    """The archived Solar effect applies to every champion, not just Solars.

    Its curve rows describe additive percentage points for each unique 3-star
    champion. True-damage conversion and in-combat ascension need engine
    support and are reported separately instead of inventing a conversion.
    """
    curve = trait["curve"]
    value = lambda row: tft.curve_at(curve[row], column)
    per_star = value("PercentIncreasePer3Star") * three_stars
    stats = []
    if three_stars >= value("NumThreeStarThreshold1"):
        stats = [["asPct", value("Threshold1AttackSpeed") - 1.0],
                 ["armor", value("Threshold1ArmorMagicResist")],
                 ["mr", value("Threshold1ArmorMagicResist")]]
    return {"api": trait["api"], "name": trait["name"], "stats": stats,
            "bonusMagicPct": value("BonusMagicDamage") + per_star,
            "shieldAtStart": [value("ShieldRatio") + per_star, value("ShieldDuration")]}


def resolve_board_traits(snap, members, alpha_holder=None):
    """Resolve actual trait counts and per-champion combat effects.

    ``members`` contains ``{"api": ..., "star": ...}`` entries within nine slots;
    partial boards are useful during search. Every champion must be distinct
    and use an allowed star level. ``alpha_holder`` is either absent or an
    eligible Riftbeast on a board with the trait active; absent means no Mark.

    All snapshot traits are returned in API order. ``breakpoint`` and the
    original one-based curve ``column`` are null when inactive. Effects and
    limitations are independent JSON-compatible values on every call.
    """
    members = list(members)
    if len(members) > MAX_BOARD_SLOTS:
        raise ValueError("a composition can use at most nine board slots")
    selected = {}
    stars = {}
    for member in members:
        if not isinstance(member, dict):
            raise ValueError("each composition member needs a champion api and star")
        api, star = member.get("api"), member.get("star")
        if not isinstance(api, str) or api not in snap.units:
            raise ValueError(f"unknown shop champion {api!r}")
        if api in selected:
            raise ValueError(f"duplicate composition champion {api}")
        unit = snap.units[api]
        if type(star) is not int or star not in tft.unit_stars(unit):
            raise ValueError(f"invalid star level {star!r} for {unit['name']}")
        selected[api] = unit
        stars[api] = star

    if slots_used(selected) > MAX_BOARD_SLOTS:
        raise ValueError("a composition can use at most nine board slots")
    counts = trait_counts(snap, selected)
    hand = tft.load_trait_effects(snap.set_no)
    traits = []
    for api, trait in sorted(snap.traits.items()):
        columns = [column for column, threshold in enumerate(trait["levels"], 1)
                   if threshold > 0 and counts[api] >= threshold]
        column = max(columns, key=lambda c: (trait["levels"][c - 1], c)) if columns else None
        traits.append({"api": api, "name": trait["name"], "count": counts[api],
                       "breakpoint": trait["levels"][column - 1] if column else None,
                       "column": column, "active": column is not None,
                       "modeled": api in hand or api in (SOLAR, APEX_PREDATOR)})
    by_trait = {trait["api"]: trait for trait in traits}

    if alpha_holder is not None:
        if (not isinstance(alpha_holder, str) or alpha_holder not in selected
                or RIFTBEAST not in selected[alpha_holder]["traitApis"]):
            raise ValueError("the Alpha Mark holder must be a Riftbeast on this board")
        if not by_trait.get(RIFTBEAST, {}).get("active"):
            raise ValueError("the Alpha Mark requires an active Riftbeast breakpoint")

    effects = {api: [] for api in sorted(selected)}
    limitations = []
    three_stars = sum(star == 3 for star in stars.values())
    for entry in traits:
        if not entry["active"]:
            continue
        api, column = entry["api"], entry["column"]
        trait = snap.traits[api]
        if api == APEX_PREDATOR:
            # Slot occupancy and the total two-Riftbeast contribution are
            # resolved structurally; the champion driver handles its combat.
            continue
        if api == SOLAR:
            solar = _solar_effect(trait, column, three_stars)
            for unit_api in effects:
                effects[unit_api].append(deepcopy(solar))
            if three_stars >= tft.curve_at(trait["curve"]["NumThreeStarThreshold2"], column):
                limitations.append("Solar: the bonus damage remains magic; its conversion to true damage at five 3-star champions is not modeled")
            if three_stars >= tft.curve_at(trait["curve"]["NumThreeStarThreshold3"], column):
                limitations.append("Solar: in-combat ascension to four stars is not modeled")
            continue

        if api not in hand:
            note = _UNMODELED_NOTES.get(api, "this trait's combat or economy effects are not modeled")
            limitations.append(f"{trait['name']}: {note}")
            continue

        for unit_api, unit in sorted(selected.items()):
            if api in unit["traitApis"]:
                effect = tft.trait_spec(snap, api, column, hand, unit)
                if api == RIFTBEAST:
                    effect["riftbeast"] = unit_api == alpha_holder
                    # The benchmark's note assumes the Mark on every isolated
                    # unit and describes obsolete timed bonuses; use this
                    # board's explicit ownership and growth limitation below.
                    effect.pop("note", None)
                effects[unit_api].append(deepcopy(effect))
            elif api in _TEAM_EFFECTS:
                effects[unit_api].append(tft.trait_spec(
                    snap, api, column, _TEAM_EFFECTS, unit))

        if api == RIFTBEAST:
            if entry["breakpoint"] >= 7:
                limitations.append("Riftbeast: capstone stats apply at combat start; recurring growth every five seconds is not modeled")
        elif api == "DA_18_Spellweaver":
            limitations.append("Spellweaver: each champion gains AP from its own casts; other Spellweavers' casts do not grant additional stacks")
        elif api == "DA_Juggernaut18":
            # This module supplies the team share omitted by the individual
            # benchmark; do not carry its now-inapplicable note into the UI.
            for unit_effects in effects.values():
                for effect in unit_effects:
                    if effect["api"] == api:
                        effect.pop("note", None)
        elif api == "DA_Primal18":
            limitations.append("Primal: the existing Tiger model grants Primal attack speed from combat start; its six-second delay and team attack speed are not modeled")
        elif hand[api].get("note"):
            limitations.append(f"{trait['name']}: {hand[api]['note']}")
        if api == LUNAR:
            limitations.append("Lunar: adjacent non-Lunar allies receive no bonus because hex positions are not modeled")

    if counts[SOLAR] >= 3 and counts[LUNAR] >= 3:
        limitations.append("Eclipse: the combined Solar/Lunar execute is not modeled")

    return {"traits": traits, "effects": effects,
            "limitations": sorted(set(limitations)), "alphaHolder": alpha_holder}
