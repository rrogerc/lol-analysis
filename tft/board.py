"""Board capacity, composition trait resolution, and symmetric team combat helpers.

Elder Dragon consumes two team slots and contributes two Riftbeast in total.
Source: Riot's Enchanted Wilds overview (August 2026), Riftbeast section:
https://teamfighttactics.leagueoflegends.com/en-sg/news/game-updates/enchanted-wilds-overview/
"""

from collections import Counter, OrderedDict
from copy import deepcopy
from fractions import Fraction
import hashlib
from itertools import combinations
import math
from pathlib import Path

from tft import sim as tft


ELDER_DRAGON = "TFT18_ElderDragon"
RIFTBEAST = "DA_Riftbeast18"
APEX_PREDATOR = "DA_18_ApexPredator"
SOLAR = "DA_18_Solar"
LUNAR = "DA_18_Lunar"
ECLIPSE = "DA_18_Eclipse"
PRIMAL = "DA_Primal18"

DEFAULT_BOARD_SLOTS = 8
MAX_BOARD_SLOTS = 9
DURATION = 30.0
PREPARED_ACTOR_CACHE_LIMIT = 4096
INITIATIVES = (0, 1)
LANE_ORDER = (3, 1, 5, 2, 4, 0, 6)

# Keep Tiger first to preserve the former default when measured scores tie.
# Ordering breaks exact ties only; it adds no preference to the score.
PRIMAL_BLESSINGS = ("tiger", "turtle", "bear", "phoenix")

SOURCE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def unit_slots(api):
    return 2 if api == ELDER_DRAGON else 1


def slots_used(roster):
    """Accept champion APIs or resolved member/unit records."""
    return sum(unit_slots(unit["api"] if isinstance(unit, dict) else unit) for unit in roster)


def trait_counts(snap, roster):
    apis = [unit["api"] if isinstance(unit, dict) else unit for unit in roster]
    counts = Counter(trait for api in apis for trait in set(snap.units[api]["traitApis"]))
    if ELDER_DRAGON in apis:
        # Two total, rather than two additional copies of the listed trait.
        counts[RIFTBEAST] += 1
    return counts


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
    """The archived Solar effect applies to every champion, not just Solars."""
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
    """Resolve actual trait counts and per-champion combat effects."""
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


board_trait_effects = resolve_board_traits


def _frontline(spec):
    unit = spec["unit"]
    return unit["objective"] in ("tank", "fighter") or unit["kind"] == "Assassin"


def _reference_positions(roster, roles):
    """Keep authored positions, relocating only units whose form changes row."""
    positions = {}
    occupied = {True: set(), False: set()}
    for unit in roster:
        api = unit["api"]
        front = roles[api]["frontline"]
        if unit["frontline"] == front:
            positions[api] = {**roles[api], "lane": unit["lane"], "priority": unit["priority"]}
            occupied[front].add(unit["lane"])
    for unit in sorted(roster, key=lambda value: (value["priority"], value["api"])):
        api = unit["api"]
        if api in positions:
            continue
        front = roles[api]["frontline"]
        free = [lane for lane in LANE_ORDER if lane not in occupied[front]]
        if not free:
            raise ValueError("reference form changes require an available lane in the effective row")
        lane = min(free, key=lambda value: (abs(value - unit["lane"]), LANE_ORDER.index(value)))
        positions[api] = {**roles[api], "lane": lane, "priority": unit["priority"]}
        occupied[front].add(lane)
    return [dict(unit, **positions[unit["api"]]) for unit in roster]


def rank_key(result):
    """Only win rate ranks results; equal rates remain genuine outcome ties."""
    metrics = result.get("metrics", result)
    return (-Fraction(metrics["benchmarkWins"], metrics["benchmarkCount"]),)


def summarize(results, labels, *, details=True):
    """Summarize actual wins; HP, time and contributions are diagnostics."""
    if not results or len(results) != len(labels):
        raise ValueError("shared results need exactly one label for each encounter")
    if any(result["outcome"] not in ("win", "loss", "draw", "timeout") for result in results):
        raise ValueError("unknown symmetric encounter outcome")
    count = len(results)
    wins = sum(result["outcome"] == "win" for result in results)
    margin = math.fsum(result["allyHpFraction"] - result["enemyHpFraction"] for result in results) / count
    win_times = [result["duration"] for result in results if result["outcome"] == "win"]
    metrics = {"benchmarkWins": wins, "benchmarkCount": count, "benchmarkWinRate": wins / count,
               "benchmarkScore": 100 * wins / count,
               "benchmarkLosses": sum(result["outcome"] == "loss" for result in results),
               "benchmarkDraws": sum(result["outcome"] == "draw" for result in results),
               "benchmarkTimeouts": sum(result["outcome"] == "timeout" for result in results),
               "hpMargin": margin, "clearTime": math.fsum(win_times) / len(win_times) if win_times else DURATION,
               "damageDps": math.fsum(result["damageDps"] for result in results) / count,
               "frontlineTime": math.fsum(result["frontlineTime"] for result in results) / count}
    by_unit = {}
    for result in results if details else ():
        for unit in result["allies"]:
            row = by_unit.setdefault(unit["api"], Counter())
            for field in ("damage", "aliveTime", "damageTaken", "healing", "shielding", "allyHealing", "allyShielding", "attacks", "casts"):
                row[field] += unit.get(field, 0.0) / count
            row["dps"] += unit.get("dps", unit["damage"] / max(result["duration"], tft.TICK_S)) / count
            row["survived"] += bool(unit["alive"]) / count
    matchups = [{**label, **{field: result[field] for field in (
        "outcome", "duration", "enemyHpFraction", "allyHpFraction", "damage", "frontlineTime")}}
                for label, result in zip(labels, results)]
    return {"metrics": metrics, "units": {api: dict(row) for api, row in by_unit.items()}, "matchups": matchups}


class Evaluator:
    """Resolve champion specs, combat layouts, and prepared actors for symmetric fights."""

    def __init__(self, snap, geometry="clump", threat="mixed", *, prepared=True, workers=1):
        if geometry not in tft.GEOMETRIES or threat not in ("mixed", "physical", "magic"):
            raise ValueError("unknown symmetric reference context")
        if type(workers) is not int or not 1 <= workers <= 8:
            raise ValueError("match workers must be between 1 and 8")
        self.snap, self.geometry, self.threat = snap, geometry, threat
        self.prepared, self.workers = prepared, workers
        self.item_fx = tft.load_item_effects(snap.set_no)
        self.templates, self.items = {}, {}
        self._forms = OrderedDict()
        self._prepared_actors = OrderedDict()
        self.stats = Counter()

    def spec(self, api, star, effects, items, alpha=False):
        items = tuple(sorted(items))
        signature = api, star, tft.json_hash(effects), alpha
        if signature not in self.templates:
            dummy = {"slots": [], "targetDebuffs": {}, "enemyDebuffs": {}}
            result = tft.cell_spec(self.snap, self.snap.units[api], star, self.geometry, [], dummy,
                                   DURATION, True, item_fx=self.item_fx)
            result.update(immortal=False, pressure=True, targetDebuffs={}, enemyDebuffs={}, traits=deepcopy(effects))
            for effect in result["traits"]:
                if effect["api"] == RIFTBEAST:
                    effect["riftbeast"] = bool(alpha)
            self.templates[signature] = result
        for item in items:
            if (api, item) not in self.items:
                self.items[api, item] = tft.item_spec(self.snap, item, self.item_fx, self.snap.units[api])
        result = dict(self.templates[signature], items=[self.items[api, item] for item in items])
        form = None
        if result["unit"]["hasForms"]:
            key = signature, items
            if key not in self._forms:
                probe = dict(result, dummies={"slots": [{"hp": 1.0, "armor": 0.0, "mr": 0.0}]})
                self._forms[key] = tft.engine().compose_fx(probe)["form"]
                while len(self._forms) > PREPARED_ACTOR_CACHE_LIMIT:
                    self._forms.popitem(last=False)
            else:
                self._forms.move_to_end(key)
            form = self._forms[key]
        result["unit"] = dict(result["unit"], form=form)
        if api == "TFT18_Nidalee" and form == "AD":
            result["unit"].update(kind="Assassin", range=result["kits"]["AD"]["stats"]["range"])
        return result

    def _placements(self, members, carry, tank, specs):
        def order(member):
            api = member["api"]
            unit = self.snap.units[api]
            return (0 if api == tank else 1 if unit["objective"] == "tank" else 2 if api == carry else 3, api)
        ordered = sorted(members, key=order)
        positions = {True: iter((*LANE_ORDER, 3, 1)), False: iter((*LANE_ORDER, 3, 1))}
        placements = []
        for priority, member in enumerate(ordered):
            front = _frontline(specs[member["api"]])
            lane = next(positions[front])
            placements.append((member, front, lane, priority))
        return placements

    def _combat_specs(self, members, effects, selected):
        if (not 1 <= len(members) <= MAX_BOARD_SLOTS
                or len({member["api"] for member in members}) != len(members)
                or slots_used(members) > MAX_BOARD_SLOTS):
            raise ValueError("candidate boards require one to nine distinct champions using at most nine slots")
        return {member["api"]: self.spec(member["api"], member["star"], effects[member["api"]],
                                         selected[member["api"]]["items"],
                                         bool(selected[member["api"]].get("alpha")))
                for member in members}

    def combat_layout(self, members, effects, selected, carry, tank):
        """Resolved form, role and position for each member's actual loadout."""
        specs = self._combat_specs(members, effects, selected)

        def ability(api):
            unit = self.snap.units[api]
            form = specs[api]["unit"]["form"]
            return (unit.get("forms") or {}).get(form, {}).get("name") or unit["ability"]["name"]

        return {member["api"]: {"form": specs[member["api"]]["unit"]["form"],
                                "kind": specs[member["api"]]["unit"]["kind"],
                                "abilityName": ability(member["api"]),
                                "frontline": front, "lane": lane, "priority": priority}
                for member, front, lane, priority in self._placements(members, carry, tank, specs)}

    def allies(self, members, effects, selected, carry, tank, *, prepared=False):
        allies = []
        specs = self._combat_specs(members, effects, selected)
        for member, front, lane, priority in self._placements(members, carry, tank, specs):
            api = member["api"]
            option = selected[api]
            alpha = bool(option.get("alpha", False))
            if prepared:
                key = (api, member["star"], tft.json_hash(effects[api]), tuple(sorted(option["items"])),
                       alpha, specs[api]["unit"]["form"], specs[api]["unit"]["kind"], front, lane, priority)
                if key not in self._prepared_actors:
                    entry = {"spec": specs[api],
                             "frontline": front, "lane": lane, "priority": priority}
                    self._prepared_actors[key] = tft.engine().prepare_actor(entry)
                    self.stats["actorsPrepared"] += 1
                    while len(self._prepared_actors) > PREPARED_ACTOR_CACHE_LIMIT:
                        self._prepared_actors.popitem(last=False)
                else:
                    self._prepared_actors.move_to_end(key)
                    self.stats["preparedActorsReused"] += 1
                allies.append(self._prepared_actors[key])
            else:
                allies.append({"spec": specs[api],
                               "frontline": front, "lane": lane, "priority": priority})
        return allies
