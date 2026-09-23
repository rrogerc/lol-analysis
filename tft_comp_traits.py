"""Resolve a composition's traits for the existing unit engine.

This is separate from the champion benchmark's bare/low/high contexts. Counts
come from the selected shop champions; global bonuses reach nonmembers once,
and a Riftbeast's Alpha Mark belongs to one explicitly selected champion.
``modeled`` is retained for compatibility and means some engine support.
``coverage`` and ``coverageNotes`` describe what the theoretical score can
actually credit. Supported does not certify accuracy against live games.
"""

from copy import deepcopy
from itertools import combinations

import tft
from tft_board import APEX_PREDATOR, MAX_BOARD_SLOTS, slots_used, trait_counts


RIFTBEAST = "DA_Riftbeast18"
SOLAR = "DA_18_Solar"
LUNAR = "DA_18_Lunar"
ECLIPSE = "DA_18_Eclipse"
PRIMAL = "DA_Primal18"

# Keep Tiger first to preserve the former default when measured scores tie.
# Ordering breaks exact ties only; it adds no preference to the score.
PRIMAL_BLESSINGS = ("tiger", "turtle", "bear", "phoenix")


def primal_catalog(snap):
    curve = snap.traits[PRIMAL]["curve"]
    value = lambda row: tft.curve_at(curve[row], 1)
    return [
        {"key": "tiger", "name": "Tiger",
         "description": f"After {value('TigerDelay'):g}s, Primals gain {(value('TigerAttackSpeedPrimal') - 1) * 100:g}% attack speed and other allies gain {(value('TigerAttackSpeedTeam') - 1) * 100:g}%.",
         "scoreLimitation": "The current interpretation treats the Primal amount as its total; whether it also receives the team share remains unverified."},
        {"key": "turtle", "name": "Turtle",
         "description": f"Each living ally heals {value('TurtleMaxHealthRatio') * 100:g}% of its maximum health every {value('TurtlePeriod'):g}s.",
         "scoreLimitation": "Only effective healing is credited, after Wound and missing-health limits."},
        {"key": "bear", "name": "Bear",
         "description": f"Primal damage executes enemies below {value('BearExecuteThreshold') * 100:g}% health.",
         "scoreLimitation": "Executes work in finite combat, but cannot trigger on the immortal targets used by composition scoring; their value is unmeasured here."},
        {"key": "phoenix", "name": "Phoenix",
         "description": f"Every {value('PhoenixTakedownsPerComponent'):g} Primal takedowns grants a component, up to four.",
         "scoreLimitation": "Persistent takedowns and acquired components are outside this fixed-item-budget comparison; economy value is unmeasured here."},
    ]


def primal_options(snap, members, required=None):
    if (required is not None and (not isinstance(required, (list, tuple)) or len(required) > 2
            or any(key not in PRIMAL_BLESSINGS for key in required) or len(set(required)) != len(required))):
        raise ValueError("retained Primal blessings must be distinct known choices")
    if PRIMAL not in snap.traits:
        return [()]
    count = trait_counts(snap, members)[PRIMAL]
    choices = sum(count >= threshold for threshold in snap.traits[PRIMAL]["levels"] if threshold > 0)
    options = list(combinations(PRIMAL_BLESSINGS, choices)) if choices else [()]
    retained = set((required or ())[:choices])
    return [option for option in options if retained <= set(option)]


def primal_selection(snap, members, selected=None):
    options = primal_options(snap, members)
    if selected is None:
        return options[0]
    if (not isinstance(selected, (list, tuple)) or any(not isinstance(key, str) for key in selected)
            or len(set(selected)) != len(selected) or any(key not in PRIMAL_BLESSINGS for key in selected)):
        raise ValueError("Primal blessings must be distinct known choices")
    selected = tuple(key for key in PRIMAL_BLESSINGS if key in selected)
    if selected not in options:
        raise ValueError("Primal blessing count must match its active breakpoint")
    return selected


def primal_effect(snap, unit_api, selected):
    curve = snap.traits[PRIMAL]["curve"]
    value = lambda row: tft.curve_at(curve[row], 1)
    member = PRIMAL in snap.units[unit_api]["traitApis"]
    effect = {"api": PRIMAL, "name": snap.traits[PRIMAL]["name"], "stats": []}
    if "tiger" in selected:
        row = "TigerAttackSpeedPrimal" if member else "TigerAttackSpeedTeam"
        effect["timedStats"] = [{"after": value("TigerDelay"), "interval": 0,
                                 "stats": [["asPct", value(row) - 1.0]]}]
    if "turtle" in selected:
        effect["healPerInterval"] = [value("TurtleMaxHealthRatio"), value("TurtlePeriod")]
    if "bear" in selected and member:
        effect["executeBelowHp"] = value("BearExecuteThreshold")
    return effect


def with_primal_effects(snap, members, effects, selected):
    selected = primal_selection(snap, members, selected)
    updated = deepcopy(effects)
    for member in members:
        api = member["api"]
        updated[api] = [effect for effect in updated[api] if effect.get("api") != PRIMAL]
        if selected:
            updated[api].append(primal_effect(snap, api, selected))
    return updated


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
    "DA_18_ZyraUniqueTrait": {
        "durability": {"row": "BaseDurability", "col": 1, "minusOne": True, "scale": -1}},
}


_UNMODELED_NOTES = {
    "DA_18_Blackthorn": "the sacrifice hex is left empty; no unit is sacrificed and no Blackthorn bonus is included",
    "DA_18_Coven": "prior Essence, ritual progress and cashout rewards are not supplied or modeled",
    "DA_18_Elderwood": "placeable plants and their combat effects are not modeled",
    "DA_18_Sprykin": "the Big Furry Friend, its Rider bonuses and its combat effects are not modeled",
    "DA_18_Rival": "prior takedown stacks, chosen evolutions and paired ability bonuses are not modeled",
    "DA_18_LuxUniqueTrait": "no Avatar variant is selected; no chosen trait or doubled trait count is inferred",
    "DA_Emerald18": "the paired ally's bonuses are not applied to another champion",
    "DA_18_Maokai_UniqueTrait": "prior permanent Health stacks are set to zero",
    "DA_18_Greenfather": "no biome, cultivated hexes, occupants or seed history are supplied; their combat bonuses are not included",
    "DA_AluneUniqueTrait18": "Alune's cast-driven moon phases do not grant the team's alternating durability and damage amplification",
    "DA_DravenUniqueTrait18": "bounty progress and rewards affect economy and are outside the fixed-board combat score",
}

# An implemented single-unit hook is not proof that the composition objective
# values the whole trait. Keep these concrete scoring gaps beside the resolver
# and expose them on the affected trait instead of relying on a boolean badge.
_THEORY_COVERAGE_NOTES = {
    "DA_18_Adaptor": ["form is fixed from equipped bonus AD/AP before other traits; live tie rules and trait-driven form changes are not verified"],
    "DA_18_Battlemage": ["attacker count follows assigned generic pressure sources independently of outgoing area coverage; actual enemy targeting is not represented"],
    "DA_18_Executioner": ["bleed excludes true/raw damage and stops being measured after the holder dies; exact live proc eligibility is not verified"],
    "DA_18_Hunter": ["immortal targets usually keep the primary target fixed, so actual retargeting and damage-amp uptime are not represented"],
    "DA_18_Inferno": ["Wound has no value against generic targets that never heal; the nonstacking trait burn uses one provider"],
    "DA_18_Invoker": ["Invoker mana regeneration is interpreted as the team amount plus InvokerManaBonus; the archived wording does not unambiguously establish the member total"],
    "DA_18_Slayer": ["generic targets stay at full Health, so the stronger damage amplification below half Health is not credited"],
    "DA_18_Spellweaver": ["opening member AP is interpreted as TeamwideAP plus SpellweaverAP; the archived wording does not unambiguously establish whether the member row is additional or total"],
    "DA_18_Summoner": ["Zyra plant attacks use an assumed one-second cadence; live plant attack timing is not verified"],
    "DA_FloraFatalis18": ["generic targets never die, so takedown mana and healing contribute zero to this score"],
    RIFTBEAST: ["Alpha team effects remain incomplete: Sentinel cast mana regeneration and Scuttlecrab healing are absent; Krug ally shields are uncredited and Elder executes cannot trigger on immortal targets. Applied Mama Beak flat armor reductions are shared."],
}


def _solar_effect(trait, column, three_stars):
    """The archived Solar effect applies to every champion, not just Solars.

    Its curve rows describe additive percentage points for each unique 3-star
    champion. At five, the Threshold2TrueDamageConversion share of that same
    bonus becomes true damage (half through 18.2, 40% from 18.2b). In-combat
    four-star ascension remains outside the supported actor state.
    """
    curve = trait["curve"]
    value = lambda row: tft.curve_at(curve[row], column)
    per_star = value("PercentIncreasePer3Star") * three_stars
    stats = []
    if three_stars >= value("NumThreeStarThreshold1"):
        stats = [["asPct", value("Threshold1AttackSpeed") - 1.0],
                 ["armor", value("Threshold1ArmorMagicResist")],
                 ["mr", value("Threshold1ArmorMagicResist")]]
    bonus = value("BonusMagicDamage") + per_star
    effect = {"api": trait["api"], "name": trait["name"], "stats": stats,
              "bonusMagicPct": bonus,
              "shieldAtStart": [value("ShieldRatio") + per_star, value("ShieldDuration")]}
    if three_stars >= value("NumThreeStarThreshold2"):
        converted = value("Threshold2TrueDamageConversion")
        effect.update(bonusMagicPct=bonus * (1.0 - converted), bonusTruePct=bonus * converted)
    return effect


def resolve_board_traits(snap, members, alpha_holder=None, *, primal_blessings=None):
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
    blessings = primal_selection(snap, members, primal_blessings)
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
            if three_stars >= tft.curve_at(trait["curve"]["NumThreeStarThreshold3"], column):
                limitations.append("Solar: in-combat ascension to four stars is not modeled")
            continue

        if api == PRIMAL:
            for unit_api in sorted(selected):
                effects[unit_api].append(primal_effect(snap, unit_api, blessings))
            entry["blessings"] = list(blessings)
            limitations.extend("Primal: " + option["scoreLimitation"] for option in primal_catalog(snap)
                               if option["key"] in blessings)
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
                    # The benchmark assumes the Mark on every isolated unit;
                    # compositions use this board's explicit ownership.
                    effect.pop("note", None)
                effects[unit_api].append(deepcopy(effect))
            elif api in _TEAM_EFFECTS:
                effects[unit_api].append(tft.trait_spec(
                    snap, api, column, _TEAM_EFFECTS, unit))

        if api == RIFTBEAST:
            if entry["breakpoint"] >= 5:
                limitations.append("Riftbeast: overrun shops are not modeled")
            if entry["breakpoint"] >= 10:
                limitations.append("Riftbeast: bonus maximum team size is not modeled")
        elif api == "DA_Juggernaut18":
            # This module supplies the team share omitted by the individual
            # benchmark; do not carry its now-inapplicable note into the UI.
            for unit_effects in effects.values():
                for effect in unit_effects:
                    if effect["api"] == api:
                        effect.pop("note", None)
        elif hand[api].get("note"):
            limitations.append(f"{trait['name']}: {hand[api]['note']}")
        if api == LUNAR:
            limitations.append("Lunar: adjacent non-Lunar allies receive no bonus because hex positions are not modeled")

    if counts[SOLAR] >= 3 and counts[LUNAR] >= 3:
        limitations.append("Eclipse: the combined Solar/Lunar execute is not modeled")

    for entry in traits:
        prefix = entry["name"] + ": "
        if entry["active"]:
            limitations.extend(prefix + note for note in _THEORY_COVERAGE_NOTES.get(entry["api"], ()))
        notes = sorted({note[len(prefix):] for note in limitations if note.startswith(prefix)})
        entry["coverageNotes"] = notes
        entry["coverage"] = ("structural" if entry["api"] == APEX_PREDATOR else
                             "economy" if entry["api"] == "DA_DravenUniqueTrait18" else
                             "unmodeled" if not entry["modeled"] else
                             "partial" if notes else "supported")

    return {"traits": traits, "effects": effects,
            "limitations": sorted(set(limitations)), "alphaHolder": alpha_holder,
            "primalBlessings": list(blessings)}
