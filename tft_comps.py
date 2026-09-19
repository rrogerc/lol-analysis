"""Level-eight composition cores with optional, separately chosen level-nine caps.

Roster screening and item allocation use damage capacity with resolved board
traits across declared theoretical pressures. Individual damage/survival tests
only provide initial item alternatives.
"""
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from copy import deepcopy
import itertools
import json
import math
import multiprocessing
import os
from pathlib import Path
from queue import Empty
import signal
import sys
import tempfile
import time

import tft
import tft_theory
from tft_caps import CAP_FIVE_COST_STAR
from tft_comp_items import ItemSearch, identity as allocation_identity
from tft_comp_utility import AntihealPolicy
from tft_comp_traits import RIFTBEAST, primal_catalog, primal_options, resolve_board_traits, with_primal_effects
from tft_board import BOARD_PLAN_MODEL, DEFAULT_BOARD_SLOTS, ELDER_DRAGON, MAX_BOARD_SLOTS, slots_used, unit_slots


CACHE_DIR = os.path.join(tft.BASE_DIR, ".cache", "tft-comps")
SCENARIO_CACHE_DIR = CACHE_DIR
BOARD_SIZE = DEFAULT_BOARD_SLOTS
ITEM_BUDGETS = tuple(range(6, 13))
DEFAULT_ITEM_BUDGET = 9
DAMAGE_WINDOW = 20.0
FRONT_WINDOW = 60.0
BEAM_WIDTH = 4
REFINE_BOARDS = 16
SWAP_BOARDS = 4
LOADOUT_TOP = 4
ALLOCATION_FRONTIER = 4
ALLOCATION_CANDIDATES = 8
REFINEMENT_SEEDS = 2
MAX_WARM_WORKERS = 8
DEFAULT_WARM_WORKERS = min(MAX_WARM_WORKERS, max(1, (os.cpu_count() or 1) // 2))
_WORKER_SNAPSHOT = None
_WORKER_REVISION = None
_WORKER_PROGRESS = None
RESULT_LIMIT = 8
STRUCTURES = (
    {"key": "single", "label": "One carry + one tank", "minItems": 4},
    {"key": "duoCarry", "label": "Two carries", "minItems": 6},
    {"key": "duoTank", "label": "Two tanks", "minItems": 6},
    {"key": "both", "label": "Two carries + two tanks", "minItems": 8},
)
PROFILES = {
    "c1": {"key": "c1", "cost": 1, "label": "1-cost reroll", "mainStar": 3,
           "supportStar": 2, "maxFiveCosts": 0, "maxFourCosts": 1, "minSameCost": 3,
           "level": 8, "boardSlots": 8},
    "c2": {"key": "c2", "cost": 2, "label": "2-cost reroll", "mainStar": 3,
           "supportStar": 2, "maxFiveCosts": 0, "maxFourCosts": 2, "minSameCost": 3,
           "level": 8, "boardSlots": 8},
    "c3": {"key": "c3", "cost": 3, "label": "3-cost reroll", "mainStar": 3,
           "supportStar": 2, "maxFiveCosts": 1, "maxFourCosts": 3, "minSameCost": 3,
           "level": 8, "boardSlots": 8},
    "c4": {"key": "c4", "cost": 4, "label": "4-cost board", "mainStar": 2,
           "supportStar": 2, "maxFiveCosts": 0, "maxFourCosts": 8, "minSameCost": 3,
           "level": 8, "boardSlots": 8,
           "level9FiveCostStar": CAP_FIVE_COST_STAR,
           "level9Plan": f"Keep the level-8 carry and tank; optionally replace one support and add two {CAP_FIVE_COST_STAR}-star legendaries, or replace one support with {CAP_FIVE_COST_STAR}-star Elder Dragon."},
}
LIMITATIONS = [
    "Theoretical capacity compares damage delivered under declared generic pressure assumptions. It is not a win probability or a prediction of a specific fight.",
    "Pressure, damage mix, target defenses, area coverage and antiheal are explicit sensitivity inputs, not sampled player boards.",
    "Ability timing and effective self sustain use native unit response curves. Team protection and damage uptime use an abstract frontline model without movement or pathfinding.",
    "Unitemized rosters are screened with their actual trait breakpoints and the same native EHP × DPS model used for final boards. Legal Alpha holders are compared. Trait counts and unit costs add no score of their own; item allocation follows screening.",
    "In level-8 reroll plans, the main carry and tank are the only assumed 3-star units. Supports are 2-star, except 5-cost supports at 1-star.",
    "Main units receive at least two items; a secondary cannot receive more items than its same-role main. Main refers to the upgrade target, not a guarantee of the largest damage contribution.",
    "Every core and level-9 upgrade requires an active antiheal source: Inferno, a usable Wound item, or a champion ability that explicitly reduces healing. Burn alone does not qualify. This requirement adds no damage bonus against targets that never heal.",
    "Cost limits are planning rules, not shop probabilities. Purchase gold excludes rerolls, XP, contesting and the cost of reaching this board.",
    "Items are ideal craftable items under one shared count limit. Component demand is shown, but drops and available components are not constrained.",
    "Nonstacking damage and utility effects are credited under the theoretical model's declared provider assumptions; see the methodology for unsupported interactions.",
    "A deterministic beam search screens rosters and initial allocations. Finalists compare every legal single-item replacement, transfers, item exchanges, complete screened loadouts and selected paired changes. This is not an exhaustive global optimum.",
    "Ranks compare continuous theoretical score. Equal scores share a rank; damage and durability remain visible as separate metrics.",
    "Board limits count team slots: Elder Dragon occupies two slots and contributes two Riftbeast in total. A level-8 board with Elder Dragon has seven champions.",
    "Four-cost plans are ranked only on their level-8 core, without 5-cost units. Level-9 upgrades are chosen afterwards and cannot improve a core's ranking.",
    f"Level-9 upgrades assume {CAP_FIVE_COST_STAR}-star legendaries, including Elder Dragon, with the same item budget and surviving holders' items. Only items freed by the one sold support can move to newly added legendaries; traits are recalculated.",
    "Core and upgrade use the same theoretical pressure scenarios to isolate the transition. The optional upgrade is not a guarantee of finding legendaries.",
]


def _code_hash():
    return tft.json_hash([Path(__file__).read_text(),
                          Path(__file__).with_name("tft_comp_traits.py").read_text(),
                          Path(__file__).with_name("tft_theory.py").read_text(),
                          Path(__file__).with_name("tft_unit_profiles.py").read_text(),
                          Path(__file__).with_name("tft_comp_items.py").read_text(),
                          Path(__file__).with_name("tft_comp_utility.py").read_text(),
                          Path(__file__).with_name("tft_board.py").read_text(),
                          Path(__file__).with_name("tft_caps.py").read_text(),
                          tft.engine_source_hash()])


MODEL_HASH = _code_hash()


def source_stale():
    return tft.source_stale() or _code_hash() != MODEL_HASH


def revision(snap=None):
    snap = snap or tft.load_snapshot()
    return tft.json_hash([MODEL_HASH, tft.snapshot_revision(snap)])


def scenarios():
    return {f"{profile}-{geometry}-{threat}": {
        "key": f"{profile}-{geometry}-{threat}", "profile": profile,
        "geometry": geometry, "threat": threat}
        for profile, geometry, threat in itertools.product(PROFILES, tft.GEOMETRIES, ("mixed",))}


def cell_paths(snap=None):
    stamp = revision(snap)[:20]
    return {key: os.path.join(CACHE_DIR, f"{key}-{stamp}.json") for key in scenarios()}


def cell_ready(snap=None):
    return {key: os.path.exists(path) for key, path in cell_paths(snap).items()}


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Several scenario workers may discover the same cold unit benchmark.
    # Independent temporary files keep their otherwise identical writes safe.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name + ".",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, separators=(",", ":"), allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def cached_scenario(key, *, snap=None):
    if key not in scenarios():
        raise ValueError(f"unknown composition scenario {key}")
    try:
        return json.loads(Path(cell_paths(snap)[key]).read_text())
    except FileNotFoundError:
        return None


def api_meta(snap=None):
    snap = snap or tft.load_snapshot()
    profiles = deepcopy(list(PROFILES.values()))
    for profile in profiles:
        five = profile["maxFiveCosts"]
        profile["description"] = (
            f"Level 8: same-cost main carry and tank; at least {profile['minSameCost']} {profile['cost']}-cost champions. "
            + "Antiheal required. "
            + ("No 5-cost units. " if not five else f"Up to {five} 1-star 5-cost support{'s' if five != 1 else ''}. ")
            + (f"At most {profile['maxFourCosts']} 4-cost support{'s' if profile['maxFourCosts'] != 1 else ''}."
               if profile["cost"] < 4 else "Optional level-9 upgrades are evaluated after the level-8 core is selected."))
    icons = {item.get("name"): tft.cdragon_image_url(item.get("icon"))
             for item in snap.communitydragon.get("items", [])}
    theory = tft_theory.metadata(snap)
    return {"revision": revision(snap), "baselineRevision": tft.snapshot_revision(snap),
            "modelRevision": theory["modelRevision"], "evaluationModel": theory["evaluationModel"],
            "set": snap.set_no, "patch": snap.patch, "boardSize": BOARD_SIZE, "boardPlanModel": BOARD_PLAN_MODEL,
            "itemBudgets": list(ITEM_BUDGETS), "defaultItemBudget": DEFAULT_ITEM_BUDGET,
            "profiles": profiles, "geometries": tft.GEOMETRIES,
            "threats": [{"key": key, "label": label} for key, label in tft_theory.THREATS.items()],
            "structures": list(STRUCTURES), "limitations": LIMITATIONS,
            "scenarios": theory["scenarios"], "primalBlessings": primal_catalog(snap),
            "items": [{"api": api, "name": item["name"], "icon": icons.get(item["name"])}
                      for api, item in snap.items.items()],
            "methodology": {**theory["methodology"],
                            "screening": {"model": "unitemized-trait-resolved-capacity",
                                "approximation": True, "evaluationModel": tft_theory.MODEL,
                                "resolvedBoardTraits": True, "alphaSelection": "all-eligible-holders",
                                "primalSelection": "all-legal-blessings-and-pairs",
                                "description": "Actual board trait effects enter native EHP × DPS screening at every search step. Legal Alpha holders and Primal choices are compared; no points are awarded for trait counts or unit costs. Finalists then receive item allocation."},
                            "search": "Beam search and diverse seeds, followed by legal item replacements, transfers/exchanges and screened loadout changes. Level-9 caps are chosen after level-8 ranking. Results are not a global optimum."}}


def _frontliner(unit):
    return unit["objective"] in ("fighter", "tank")


def board_members(snap, roster, carry, tank, profile):
    five_star = CAP_FIVE_COST_STAR if profile.get("level", BOARD_SIZE) == 9 else 1
    return [{"api": api, "star": profile["mainStar"] if api in (carry, tank)
             else five_star if snap.units[api]["cost"] == 5 else profile["supportStar"]}
            for api in sorted(roster)]


def valid_board(snap, roster, carry, tank, profile, *, complete=True):
    capacity = profile.get("boardSlots", BOARD_SIZE)
    if type(capacity) is not int or not 1 <= capacity <= MAX_BOARD_SLOTS:
        return False
    if len(set(roster)) != len(roster) or slots_used(roster) > capacity:
        return False
    if any(api not in snap.units for api in roster) or carry == tank or carry not in roster or tank not in roster:
        return False
    if (snap.units[carry]["cost"] != profile["cost"] or snap.units[tank]["cost"] != profile["cost"]
            or snap.units[carry]["objective"] == "tank" or snap.units[tank]["objective"] != "tank"):
        return False
    costs = Counter(snap.units[api]["cost"] for api in roster)
    if costs[5] > profile["maxFiveCosts"] or costs[4] > profile["maxFourCosts"]:
        return False
    fronts = sum(_frontliner(snap.units[api]) for api in roster)
    damage = sum(snap.units[api]["objective"] != "tank" for api in roster)
    left = capacity - slots_used(roster)
    if costs[profile["cost"]] + left < profile["minSameCost"] or fronts > capacity - 3 or fronts + left < 3:
        return False
    return not complete or (not left and costs[profile["cost"]] >= profile["minSameCost"] and fronts >= 3 and damage >= 2)


def _burn_adjusted(spec, item_burn, inferno_burn):
    spec = deepcopy(spec)
    if not item_burn:
        for item in spec["pool"] + spec["items"]:
            item.pop("burnOnHit", None)
            item.pop("burnAura", None)
        # These native effects call the same non-stacking burn path as
        # items. Preserve their attacks, other Alpha bonuses and healing.
        native = {"TFT18_Cinderling": "BurnDuration", "TFT18_Brambleback": "TraitBurnDuration"}
        duration = native.get(spec.get("unit", {}).get("api"))
        if duration:
            for kit in spec["kits"].values():
                kit["rows"]["BurnAmount"] = 0.0
                kit["rows"][duration] = 0.0
    if not inferno_burn:
        for trait in spec["traits"]:
            if trait["api"] == "DA_18_Inferno":
                trait.pop("burnOnHit", None)
    return spec


class Evaluator:
    """Memoized single-unit screening; these scores never rank final boards."""
    def __init__(self, snap, geometry, threat):
        self.snap, self.geometry, self.threat = snap, geometry, threat
        self.pool = tft.pool_items(snap, tft.load_item_effects(snap.set_no))
        self.item_fx = tft.load_item_effects(snap.set_no)
        self.dummy = tft.dummies_for(snap)
        self.tank_dummy = tft.dummies_for(snap, threat=threat)
        self.specs, self.libraries, self.simulations, self.optima = {}, {}, {}, {}
        self.stats = Counter()

    def spec(self, api, star, effects, defense=False):
        key = (api, star, tft.json_hash(effects), defense)
        if key not in self.specs:
            unit = dict(self.snap.units[api], objective="tank" if defense else "carry")
            spec = tft.cell_spec(self.snap, unit, star, self.geometry, [],
                                self.tank_dummy if defense else self.dummy,
                                FRONT_WINDOW if defense else DAMAGE_WINDOW, defense,
                                item_fx=self.item_fx, pool=self.pool)
            spec["immortal"] = True
            # Components with utility must remain competitive during screening.
            # Shared fights likewise require an actual source for these effects.
            spec["targetDebuffs"] = {}
            spec["traits"] = deepcopy(effects)
            self.specs[key] = spec
        return self.specs[key]

    @staticmethod
    def compact(result):
        return {field: result[field] for field in (
            "total", "aliveTime", "survivalCapped", "stressAliveTime", "stressCapped")}

    def optimal(self, spec):
        signature = tft.json_hash([tft.engine().SOURCE_HASH, spec, LOADOUT_TOP])
        if signature in self.optima:
            return self.optima[signature]
        filename = Path(CACHE_DIR) / "bench" / (signature + ".json")
        try:
            result = json.loads(filename.read_text())
            self.stats["benchmarksReused"] += 1
        except FileNotFoundError:
            count, grouped = tft.engine().optimize_loadouts(
                spec, top=LOADOUT_TOP, workers=1 if _WORKER_SNAPSHOT is not None else 0,
                preserve_forms=True)
            result = {"count": count, "groups": [[{
                "items": [self.pool[i] for i in indices], "result": self.compact(fight)}
                for indices, _, fight in rows] for rows in grouped]}
            _atomic(filename, result)
            self.stats["loadoutsSimulated"] += count
            self.stats["unitEvaluations"] += 1
        self.optima[signature] = result
        for rows in result["groups"]:
            for row in rows:
                self.simulations[(signature, tuple(row["items"]))] = row["result"]
        return result

    def fight(self, spec, items):
        signature = tft.json_hash([tft.engine().SOURCE_HASH, spec, LOADOUT_TOP])
        key = signature, tuple(items)
        if key not in self.simulations:
            trial = dict(spec, pool=[], items=[spec["pool"][self.pool.index(api)] for api in items])
            self.simulations[key] = self.compact(tft.engine().simulate(trial, False)[1])
            self.stats["loadoutsSimulated"] += 1
        return self.simulations[key]

    def baseline(self, api, star):
        traits = resolve_board_traits(self.snap, [{"api": api, "star": star}])["effects"][api]
        damage = self.fight(self.spec(api, star, traits), ())['total'] / DAMAGE_WINDOW
        frontline = 0.0
        if _frontliner(self.snap.units[api]):
            frontline = 100 * self.fight(self.spec(api, star, traits, True), ())['aliveTime'] / FRONT_WINDOW
        return damage, frontline

    def loadout(self, api, star, effects, items, alpha=False):
        effects = deepcopy(effects)
        for trait in effects:
            if trait["api"] == RIFTBEAST:
                trait["riftbeast"] = alpha
        damage_spec = self.spec(api, star, effects)
        dmg = self.fight(damage_spec, tuple(sorted(items)))
        defense = self.fight(self.spec(api, star, effects, True), tuple(sorted(items))) if _frontliner(self.snap.units[api]) else None
        specs = [damage_spec["pool"][self.pool.index(item)] for item in items]
        inferno = any(trait["api"] == "DA_18_Inferno" and trait.get("burnOnHit") for trait in effects)
        native = api == "TFT18_Cinderling" or (api == "TFT18_Brambleback" and alpha)
        burn = native or any(item.get("burnOnHit") or item.get("burnAura") for item in specs)
        return {"items": tuple(sorted(items)), "count": len(items), "dps": dmg["total"] / DAMAGE_WINDOW,
                "frontline": 100 * defense["aliveTime"] / FRONT_WINDOW if defense else 0.0,
                "stress": (defense["stressAliveTime"] or 0) if defense else 0.0,
                "capped": defense["survivalCapped"] if defense else False, "alpha": bool(alpha),
                "itemBurn": bool(burn), "infernoBurn": bool(inferno),
                "utility": _utility_mask(specs) | (4 if burn or inferno else 0),
                "allyHeal": any(item.get("allyHealPct") for item in specs)}

    def options(self, api, star, effects, alpha=False):
        effects = deepcopy(effects)
        for trait in effects:
            if trait["api"] == RIFTBEAST:
                trait["riftbeast"] = alpha
        key = api, star, tft.json_hash(effects), alpha
        if key in self.libraries:
            return self.libraries[key]
        unit = self.snap.units[api]
        front = _frontliner(unit)
        inferno = any(t["api"] == "DA_18_Inferno" and t.get("burnOnHit") for t in effects)
        options = []
        damage_spec = self.spec(api, star, effects)
        defense_spec = self.spec(api, star, effects, True) if front else None
        damage_opt = self.optimal(damage_spec)
        defense_opt = self.optimal(defense_spec) if front else None
        item_specs = {item["api"]: item for item in damage_spec["pool"]}
        utility_items = [item for item, spec in item_specs.items() if _utility_mask([spec]) or spec.get("allyHealPct")]
        native_burn = api == "TFT18_Cinderling" or (api == "TFT18_Brambleback" and alpha)
        for count in range(4):
            damage_rows = damage_opt["groups"][count]
            defense_rows = defense_opt["groups"][count] if front else []
            candidates = {tuple(row["items"]) for row in damage_rows + defense_rows}
            if count == 1:
                candidates.update((item,) for item in self.pool)
            anchors = [rows[0]["items"] for rows in (damage_rows, defense_rows) if rows]
            if count:
                # Carry utility can be worth more to teammates than to its
                # holder. Keep explicit one-item substitutions before pruning.
                additions = set(utility_items)
                additions.update(item for anchor in anchors for item in anchor)
                for anchor in anchors:
                    for index in range(count):
                        for item in additions:
                            items = list(anchor)
                            items[index] = item
                            candidates.add(tuple(sorted(items)))
            for items in sorted(candidates):
                if any(number > 1 and self.snap.items[item]["unique"] for item, number in Counter(items).items()):
                    continue
                dmg = self.fight(damage_spec, items)
                defense = self.fight(defense_spec, items) if front else None
                specs = [item_specs[item] for item in items]
                has_burn = native_burn or any(item.get("burnOnHit") or item.get("burnAura") for item in specs)
                options.append({"items": items, "count": count,
                                "dps": dmg["total"] / DAMAGE_WINDOW,
                                "frontline": 100 * defense["aliveTime"] / FRONT_WINDOW if defense else 0.0,
                                "capped": defense["survivalCapped"] if defense else False,
                                "stress": defense["stressAliveTime"] or 0 if defense else 0.0,
                                "itemBurn": bool(has_burn), "infernoBurn": inferno, "alpha": alpha,
                                "utility": _utility_mask(specs) | (4 if has_burn or inferno else 0),
                                "allyHeal": any(item.get("allyHealPct") for item in specs)})
        # Preserve all one-item candidates. Final team refinement also tests
        # every legal replacement for larger builds, regardless of this
        # initial shortlist. Ally-healing items receive no separate category.
        groups = defaultdict(list)
        antiheal_policy = AntihealPolicy(self.snap, [{"api": api, "star": star}], {api: effects})
        for option in options:
            # Both equipped forms can offer different team tradeoffs even
            # when one has lower isolated damage. Resolve form in Rust.
            form = None
            if unit.get("forms"):
                equipped = dict(damage_spec, pool=[], items=[item_specs[item] for item in option["items"]])
                form = tft.engine().compose_fx(equipped)["form"]
            groups[(option["count"], option["utility"], alpha,
                    form, antiheal_policy.has_source(api, option))].append(option)
        result = []
        for values in groups.values():
            result.extend(values if values[0]["count"] == 1 else
                          _pareto(values, "dps", "frontline", limit=LOADOUT_TOP, tie=lambda o: (-o["stress"], o["items"])))
        self.libraries[key] = result
        return result


def _utility_mask(specs):
    return (int(any(spec.get("sunderOnHit") or spec.get("sunderAura") for spec in specs))
            | 2 * int(any(spec.get("shredOnHit") or spec.get("shredAura") for spec in specs))
            | 4 * int(any(spec.get("burnOnHit") or spec.get("burnAura") for spec in specs)))


def _pareto(rows, damage_key, front_key, *, limit, tie):
    ordered = sorted(rows, key=lambda row: (-row[damage_key], -row[front_key], tie(row)))
    frontier, best_front = [], -1.0
    for row in ordered:
        if row[front_key] > best_front:
            frontier.append(row)
            best_front = row[front_key]
    if len(frontier) <= limit:
        return frontier
    # Preserve both extremes and the strongest balanced interior outcomes.
    keep = [frontier[0], frontier[-1]]
    keep += sorted(frontier[1:-1], key=lambda row: (-row[damage_key] * row[front_key], tie(row)))[:max(0, limit - 2)]
    return keep[:limit]


def allocate(members, libraries, carry, tank, snap, *, require_alpha=False, antiheal_policy=None):
    """Generate diverse allocation seeds under one board-wide item budget.

    These isolated values screen candidates only. Preserve utility coverage
    and both offensive/defensive extremes for final theoretical evaluation.
    """
    order = [carry, tank] + sorted(member["api"] for member in members if member["api"] not in (carry, tank))
    if antiheal_policy is None:
        effects = resolve_board_traits(snap, members)["effects"]
        antiheal_policy = AntihealPolicy(snap, members, effects)
    antiheal = {api: [antiheal_policy.has_source(api, option) for option in libraries[api]] for api in order}
    # Shared resources plus the main units' item counts: a secondary must
    # not receive more items than the same-role main unit being chased.
    states = {(0, 0, 0, 0, 0, 0, 0, False): [{"damage": 0.0, "front": 0.0, "stress": 0.0, "choices": ()}]}
    for position, api in enumerate(order):
        following = defaultdict(list)
        for state, partials in states.items():
            for index, option in enumerate(libraries[api]):
                count = option["count"]
                if api in (carry, tank) and count < 2:
                    continue
                extra_carry = api not in (carry, tank) and count >= 2 and snap.units[api]["objective"] != "tank"
                extra_tank = api not in (carry, tank) and count >= 2 and snap.units[api]["objective"] == "tank"
                if (extra_carry and count > state[4]) or (extra_tank and count > state[5]):
                    continue
                nxt = (state[0] + count, state[1] + extra_carry, state[2] + extra_tank,
                       state[3] + option["alpha"], count if api == carry else state[4],
                       count if api == tank else state[5], state[6] | option.get("utility", 0),
                       state[7] or antiheal[api][index])
                minimum_remaining = 2 if position == 0 else 0
                if nxt[0] + minimum_remaining > max(ITEM_BUDGETS) or any(value > 1 for value in nxt[1:4]):
                    continue
                for partial in partials:
                    following[nxt].append({"damage": partial["damage"] + option["dps"],
                                           "front": partial["front"] + option["frontline"],
                                           "stress": partial["stress"] + option["stress"],
                                           "choices": partial["choices"] + (index,)})
        states = {key: _pareto(values, "damage", "front", limit=ALLOCATION_FRONTIER,
                              tie=lambda row: (-row["stress"], row["choices"]))
                  for key, values in following.items()}
    out = {str(budget): {s["key"]: [] for s in STRUCTURES} for budget in ITEM_BUDGETS}
    for state, values in states.items():
        spent, second_carry, second_tank, alpha, _, _, utility, has_antiheal = state
        if spent not in ITEM_BUDGETS or (require_alpha and alpha != 1) or not has_antiheal:
            continue
        structure = "both" if second_carry and second_tank else "duoCarry" if second_carry else "duoTank" if second_tank else "single"
        for row in values:
            out[str(spent)][structure].append({**row, "balance": math.sqrt(row["damage"] * row["front"]), "utility": utility,
                "selected": {api: libraries[api][index] for api, index in zip(order, row["choices"])}})
    for groups in out.values():
        for key, rows in groups.items():
            groups[key] = _allocation_seeds(rows)
    return out


def _allocation_seeds(rows):
    if not rows:
        return []
    balanced = lambda row: (-row["balance"], -row["stress"], row["choices"])
    candidates = [min(rows, key=balanced),
                  min(rows, key=lambda row: (-row["front"], -row["stress"], -row["damage"], row["choices"])),
                  min(rows, key=lambda row: (-row["damage"], -row["front"], row["choices"]))]
    by_utility = defaultdict(list)
    for row in rows:
        by_utility[row["utility"]].append(row)
    candidates += [min(by_utility[mask], key=balanced)
                   for mask in sorted(by_utility, key=lambda mask: (-mask.bit_count(), mask))]
    candidates += sorted(rows, key=balanced)
    kept, seen = [], set()
    for row in candidates:
        identity = tuple((api, option["items"], option["alpha"]) for api, option in sorted(row["selected"].items()))
        if identity not in seen:
            kept.append(row)
            seen.add(identity)
            if len(kept) == ALLOCATION_CANDIDATES:
                break
    return kept


class Search:
    def __init__(self, snap, profile, geometry, threat, progress=lambda message: None):
        self.snap, self.profile, self.progress = snap, profile, progress
        self.evaluator = Evaluator(snap, geometry, threat)
        self.team = tft_theory.Evaluator(snap, geometry, threat,
                                        score_cache_dir=Path(CACHE_DIR) / "theory-scores", optimize_primal=True)
        self.geometry = geometry
        self.units = {unit["api"]: unit for unit in tft.modeled_units(snap)
                      if unit["cost"] < 5 or profile["maxFiveCosts"]}
        self.screened = 0
        self.states_screened = 0
        self.guides = {}
        self.refined = {}

    def prepare(self):
        self.progress("Preparing native roster scoring with actual board traits")

    def guide(self, roster, carry, tank):
        key = tuple(sorted(roster)), carry, tank
        if key in self.guides:
            return self.guides[key]
        members = board_members(self.snap, roster, carry, tank, self.profile)
        resolved = resolve_board_traits(self.snap, members)
        rift = next(trait for trait in resolved["traits"] if trait["api"] == RIFTBEAST)
        holders = ([member["api"] for member in members
                    if RIFTBEAST in self.units[member["api"]]["traitApis"]]
                   if rift["active"] else [None])
        allocations = [{member["api"]: {"items": (), "alpha": member["api"] == holder}
                        for member in members} for holder in holders]
        # The same native objective evaluates partial rosters and full boards.
        # A cheap standalone-unit sum discarded support units before their
        # shared bonuses could affect selection. Every legal Alpha option is
        # batched; registered specs and immutable actor preparation stay cached in Rust.
        results = self.team.evaluate_many(members, resolved["effects"], allocations,
                                          carry, tank, details=False)
        value = max(result["metrics"]["theoryScore"] for result in results)
        self.guides[key] = value
        self.states_screened += 1
        self.screened += slots_used(roster) == self.profile.get("boardSlots", BOARD_SIZE)
        return value

    def candidates(self):
        carries = [api for api, u in self.units.items() if u["cost"] == self.profile["cost"] and u["objective"] != "tank"]
        tanks = [api for api, u in self.units.items() if u["cost"] == self.profile["cost"] and u["objective"] == "tank"]
        completed = {}
        for carry, tank in itertools.product(sorted(carries), sorted(tanks)):
            initial = tuple(sorted((carry, tank)))
            capacity = self.profile.get("boardSlots", BOARD_SIZE)
            layers = {slots_used(initial): {initial}}
            for used in range(slots_used(initial), capacity + 1):
                options = layers.pop(used, set())
                beam = ([initial] if used == slots_used(initial) else
                        sorted(options, key=lambda roster: (-self.guide(roster, carry, tank), roster))[:BEAM_WIDTH])
                if used == capacity:
                    break
                for roster in beam:
                    for api in self.units:
                        if api in roster:
                            continue
                        candidate = tuple(sorted((*roster, api)))
                        next_used = slots_used(candidate)
                        if valid_board(self.snap, candidate, carry, tank, self.profile, complete=next_used == capacity):
                            layers.setdefault(next_used, set()).add(candidate)
            for roster in beam:
                completed[(roster, carry, tank)] = self.guide(roster, carry, tank)
        ranked = sorted(completed, key=lambda key: (-completed[key], key))
        # Include the strongest screened candidate for different main units,
        # then fill remaining places. Final combat scores do not use guide().
        chosen, seen_carries, seen_tanks = [], set(), set()
        for key in ranked:
            if key[1] not in seen_carries or key[2] not in seen_tanks:
                chosen.append(key)
                seen_carries.add(key[1]); seen_tanks.add(key[2])
                if len(chosen) == REFINE_BOARDS:
                    break
        chosen += [key for key in ranked if key not in chosen][:max(0, REFINE_BOARDS - len(chosen))]
        return chosen

    def refine(self, candidate):
        if candidate in self.refined:
            return self.refined[candidate]
        roster, carry, tank = candidate
        if not valid_board(self.snap, roster, carry, tank, self.profile):
            raise ValueError("cannot score an invalid composition")
        members = board_members(self.snap, roster, carry, tank, self.profile)
        resolved = resolve_board_traits(self.snap, members)
        rift = next((trait for trait in resolved["traits"] if trait["api"] == RIFTBEAST), {})
        seeds = _empty_results()
        libraries = {}
        # Seed each blessing independently: mixing one blessing's damage
        # proxy with another's healing would prune coherent allocations.
        for blessing in primal_options(self.snap, members):
            variant = with_primal_effects(self.snap, members, resolved["effects"], blessing)
            libraries = {}
            for member in members:
                api = member["api"]
                options = list(self.evaluator.options(api, member["star"], variant[api]))
                if rift.get("active") and RIFTBEAST in self.units[api]["traitApis"]:
                    options += self.evaluator.options(api, member["star"], variant[api], True)
                libraries[api] = options
            antiheal_policy = AntihealPolicy(self.snap, members, variant,
                                            profiles=getattr(self.team, "profiles", None))
            trial = allocate(members, libraries, carry, tank, self.snap,
                             require_alpha=rift.get("active", False), antiheal_policy=antiheal_policy)
            for budget, groups in trial.items():
                for structure, rows in groups.items():
                    existing = {allocation_identity(row["selected"]) for row in seeds[budget][structure]}
                    for row in rows:
                        identity = allocation_identity(row["selected"])
                        if identity not in existing:
                            seeds[budget][structure].append(row)
                            existing.add(identity)
        allocated = {budget: {structure: self.compare_allocations(members, resolved["effects"], rows,
                         libraries, carry, tank) for structure, rows in groups.items()}
                     for budget, groups in seeds.items()}
        result = {"members": members, "traits": resolved, "allocations": allocated}
        self.refined[candidate] = result
        return result

    def compare_allocations(self, members, effects, seeds, libraries, carry, tank):
        """Compare diverse starting allocations on the pressure scenarios.

        Every finalist subsequently receives complete single-item comparisons.
        Keeping two starting allocations gives that search different starting
        points for the continuous damage-capacity objective.
        """
        if not seeds:
            return []
        results = self.team.evaluate_many(members, effects, [seed["selected"] for seed in seeds],
                                          carry, tank, split="theory", details=False)
        compared = [self.allocation(seed["selected"], result) for seed, result in zip(seeds, results, strict=True)]
        compared.sort(key=tft_theory.rank_key)
        return compared[:REFINEMENT_SEEDS]

    @staticmethod
    def allocation(selected, result):
        return {"selected": selected, **result,
                "screening": {"approximation": True,
                              "damageDps": sum(option["dps"] for option in selected.values()),
                              "frontlineIndex": sum(option["frontline"] for option in selected.values())}}

    def item_anchors(self, candidate, refined, budget, structure):
        members, effects = refined["members"], refined["traits"]["effects"]
        allocations = refined["allocations"][str(budget)][structure]
        anchors = {}
        variants = [with_primal_effects(self.snap, members, effects, blessing)
                    for blessing in primal_options(self.snap, members)]
        for member in members:
            api = member["api"]
            alpha = allocations[0]["selected"][api].get("alpha", False)
            candidates = {}
            for variant in variants:
                for option in self.evaluator.options(api, member["star"], variant[api], alpha):
                    candidates.setdefault(tuple(option["items"]), option)
            anchors[api] = list(candidates.values())
        return anchors

    def final_items(self, candidate, refined, budget, structure, *, anchors=None):
        members, effects = refined["members"], refined["traits"]["effects"]
        _, carry, tank = candidate
        allocations = refined["allocations"][str(budget)][structure]
        if anchors is None:
            anchors = self.item_anchors(candidate, refined, budget, structure)
        optimizer = ItemSearch(self.snap, self.team, members, effects, carry, tank, structure, anchors,
                               level=self.profile["level"])
        selected, _, evidence = optimizer.optimize([row["selected"] for row in allocations])
        self.team.stats.update(optimizer.stats)
        # Batched item comparisons retain only scores. Published boards
        # always receive the complete contributions from this final allocation.
        result = self.team.evaluate(members, effects, selected, carry, tank, split="theory")
        if "primal" in result:
            effects = with_primal_effects(self.snap, members, effects, result["primal"]["selected"])
        # Recompute the cheap diagnostics/flags for changed loadouts only.
        # They never feed back into theoretical selection.
        selected = {member["api"]: self.evaluator.loadout(member["api"], member["star"], effects[member["api"]],
                    selected[member["api"]]["items"], selected[member["api"]].get("alpha", False)) for member in members}
        output = self.allocation(selected, result)
        output["itemAnalysis"] = evidence
        return output

    def swaps(self):
        def best(result):
            return min((tft_theory.rank_key(row) for rows in result["allocations"][str(DEFAULT_ITEM_BUDGET)].values() for row in rows), default=(math.inf,))
        leaders = sorted(self.refined, key=lambda key: (best(self.refined[key]), key))[:3]
        candidates = set()
        for roster, carry, tank in leaders:
            for remove in set(roster) - {carry, tank}:
                for add in self.units.keys() - set(roster):
                    changed = tuple(sorted(set(roster) - {remove} | {add}))
                    key = changed, carry, tank
                    if key not in self.refined and valid_board(self.snap, changed, carry, tank, self.profile):
                        candidates.add(key)
            # A two-slot champion trades with two ordinary support champions.
            supports = sorted(set(roster) - {carry, tank})
            if ELDER_DRAGON in self.units and ELDER_DRAGON not in roster:
                for removed in itertools.combinations(supports, 2):
                    changed = tuple(sorted(set(roster) - set(removed) | {ELDER_DRAGON}))
                    key = changed, carry, tank
                    if key not in self.refined and valid_board(self.snap, changed, carry, tank, self.profile):
                        candidates.add(key)
            elif ELDER_DRAGON in supports:
                additions = sorted(api for api in self.units.keys() - set(roster) if unit_slots(api) == 1)
                for added in itertools.combinations(additions, 2):
                    changed = tuple(sorted(set(roster) - {ELDER_DRAGON} | set(added)))
                    key = changed, carry, tank
                    if key not in self.refined and valid_board(self.snap, changed, carry, tank, self.profile):
                        candidates.add(key)
        return sorted(candidates, key=lambda key: (-self.guide(*key), key))[:SWAP_BOARDS]

    def compose(self, candidate, refined, budget, structure, allocation):
        roster, carry, tank = candidate
        members, selected = refined["members"], allocation["selected"]
        if "primal" in allocation:
            refined = dict(refined, traits=resolve_board_traits(self.snap, members,
                           primal_blessings=allocation["primal"]["selected"]))
        units, recipes = [], Counter()
        alpha, item_burn, inferno = None, None, None
        for member in members:
            api, star = member["api"], member["star"]
            unit, option = self.units[api], selected[api]
            slug = tft.unit_slug(unit)
            if option["alpha"]: alpha = slug
            if option["itemBurn"]: item_burn = slug
            if option["infernoBurn"]: inferno = slug
            assignment = "mainCarry" if api == carry else "mainTank" if api == tank else (
                "tank" if unit["objective"] == "tank" else "carry") if option["count"] >= 2 else "support"
            units.append({"api": api, "slug": slug, "name": unit["name"], "cost": unit["cost"],
                          "slotCost": unit_slots(api),
                          "star": star, "objective": unit["objective"], "assignment": assignment,
                          "items": [self.snap.items[item]["name"] for item in option["items"]],
                          "itemApis": list(option["items"]), "frontline": _frontliner(unit),
                          **allocation["units"][api]})
            for item in option["items"]:
                recipes.update(self.snap.items[item]["composition"])
        if "sharedUtility" in allocation:
            utility = allocation["sharedUtility"]
            item_api, inferno_api = utility.get("itemBurnHolder"), utility.get("infernoBurnHolder")
            item_burn = tft.unit_slug(self.units[item_api]) if item_api else None
            inferno = tft.unit_slug(self.units[inferno_api]) if inferno_api else None
        priority = {"mainCarry": 0, "mainTank": 1, "carry": 2, "tank": 3, "support": 4}
        units.sort(key=lambda unit: (priority[unit["assignment"]], unit["name"]))
        capacity = self.profile.get("boardSlots", BOARD_SIZE)
        assert slots_used(units) == capacity and sum(len(unit["items"]) for unit in units) == budget
        same = sum(self.units[api]["cost"] == self.profile["cost"] for api in roster)
        active = [trait for trait in refined["traits"]["traits"] if trait["active"]]
        connected = sorted((trait for trait in active if trait["count"] > 1 and trait["modeled"]),
                           key=lambda trait: (-trait["count"], trait["name"]))
        explanations = [f"{same} of {len(units)} champions share the {self.profile['cost']}-cost plan.",
                        f"{self.units[carry]['name']} and {self.units[tank]['name']} are the matched-cost main carry and tank."]
        if connected:
            explanations.append("Active connections: " + ", ".join(f"{t['name']} {t['count']}" for t in connected[:4]) + ".")
        if alpha:
            explanations.append(f"The Alpha Mark is assigned only to {next(unit['name'] for unit in units if unit['slug'] == alpha)}.")
        identity = [capacity, roster, carry, tank, budget, structure,
                    [(unit["api"], unit["itemApis"]) for unit in units], alpha, item_burn, inferno]
        if "primal" in allocation:
            identity.append(allocation["primal"]["selected"])
        antiheal = AntihealPolicy(self.snap, members, refined["traits"]["effects"],
                                  profiles=getattr(self.team, "profiles", None)).sources(selected)
        return {"id": tft.json_hash(identity)[:20], "structure": structure,
                "level": self.profile.get("level", capacity), "boardSlots": capacity,
                "antihealSources": antiheal,
                "slotsUsed": slots_used(units), "unitCount": len(units),
                "mainCarry": tft.unit_slug(self.units[carry]), "mainTank": tft.unit_slug(self.units[tank]),
                "alphaHolder": alpha, "burnHolder": item_burn, "itemBurnHolder": item_burn, "infernoBurnHolder": inferno,
                "units": units, "traits": [trait for trait in refined["traits"]["traits"] if trait["count"] > 0],
                "sameCostCount": same, "purchaseGold": sum(unit["cost"] * (1, 3, 9)[unit["star"] - 1] for unit in units),
                "itemCount": budget, "components": [{"api": api, "name": self.snap.items[api]["name"], "count": count}
                                                     for api, count in sorted(recipes.items())],
                "metrics": allocation["metrics"],
                **{field: allocation[field] for field in ("evaluationModel", "modelRevision", "profileCount", "scenarios", "sharedUtility", "sensitivity", "itemAnalysis", "primal") if field in allocation},
                "screening": allocation["screening"],
                "explanations": explanations, "limitations": refined["traits"]["limitations"]}

    def prepare_finalists(self):
        """Finish the dependent roster/seed decisions before splitting jobs."""
        self.prepare()
        self.progress("Searching legal level-8 boards and trait connections")
        candidates = self.candidates()
        for index, candidate in enumerate(candidates, 1):
            self.progress(f"Comparing damage capacity and item alternatives for board {index}/{len(candidates)}")
            self.refine(candidate)
        for index, candidate in enumerate(self.swaps(), 1):
            self.progress(f"Checking support swap {index}/{SWAP_BOARDS}")
            self.refine(candidate)

    def finalists(self):
        """Yield finalists in the original budget, structure and candidate order."""
        for budget in ITEM_BUDGETS:
            for structure in (s["key"] for s in STRUCTURES):
                contenders = [(candidate, refined, rows[0]) for candidate, refined in self.refined.items()
                              if (rows := refined["allocations"][str(budget)][structure])]
                contenders.sort(key=lambda entry: (tft_theory.rank_key(entry[2]), entry[0]))
                finalists, rosters = [], set()
                for candidate, refined, _ in contenders:
                    if candidate[0] in rosters:
                        continue
                    finalists.append((candidate, refined)); rosters.add(candidate[0])
                    if len(finalists) == RESULT_LIMIT:
                        break
                for index, (candidate, refined) in enumerate(finalists, 1):
                    yield budget, structure, index, len(finalists), candidate, refined

    def item_job(self, budget, structure, index, count, candidate, refined):
        """Only serializable board inputs cross the process boundary.

        Scores already selected the same starting seeds as the serial path.
        Native response curves, memoized scores and screening search states
        stay local; anchors preserve their original order for paired changes.
        """
        allocations = refined["allocations"][str(budget)][structure]
        return {"budget": budget, "structure": structure, "index": index, "count": count,
                "candidate": candidate, "refined": {"members": refined["members"], "traits": refined["traits"],
                    "allocations": {str(budget): {structure: [{"selected": row["selected"]} for row in allocations]}}},
                "anchors": self.item_anchors(candidate, refined, budget, structure)}

    def validate_boards(self, rows):
        # Scores and ranks are already fixed. Check the declared inputs and
        # aggregate arithmetic before publication, without another objective.
        self.progress("Checking theoretical scenarios and score arithmetic")
        declared = tft_theory.scenarios(self.geometry)
        expected_revision = tft_theory.revision(self.snap)
        for row in rows:
            if (row.get("evaluationModel") != tft_theory.MODEL
                    or row.get("modelRevision") != expected_revision):
                raise ValueError("finalized board has a different theoretical model revision")
            pressures = row.get("scenarios", [])
            if row.get("profileCount") != len(declared) or len(pressures) != len(declared):
                raise ValueError("finalized board lacks the complete theoretical pressure range")
            for measured, expected in zip(pressures, declared, strict=True):
                if any(measured.get(key) != value for key, value in expected.items()):
                    raise ValueError("finalized theoretical scenario differs from its declared inputs")
                if (not _finite_nonnegative(measured.get("score"))
                        or not _finite_nonnegative(measured.get("measurementWindow"))):
                    raise ValueError("theoretical scenario has invalid score or measurement window")
                for field in ("plannedMeasurementWindow", "incomingBudget", "spentPressure",
                              "deniedPressure", "unspentPressure"):
                    if not _finite_nonnegative(measured.get(field)):
                        raise ValueError(f"theoretical scenario has invalid {field}")
                if (type(measured.get("frontlineCollapsed")) is not bool
                        or measured["measurementWindow"] > measured["plannedMeasurementWindow"] + 1e-9):
                    raise ValueError("theoretical scenario has inconsistent protection timing")
                budget = measured["incomingBudget"]
                accounted = math.fsum(measured[field] for field in ("spentPressure", "deniedPressure", "unspentPressure"))
                if (not math.isclose(budget, measured["incomingDps"] * measured["measurementWindow"], rel_tol=1e-9, abs_tol=1e-7)
                        or not math.isclose(budget, accounted, rel_tol=1e-9, abs_tol=1e-7)):
                    raise ValueError("theoretical scenario does not conserve incoming pressure")
                for field in ("frontlineEhp", "damageDps", "damageCapacity", "protectionTime"):
                    if not _finite_nonnegative(measured.get(field)):
                        raise ValueError(f"theoretical scenario has invalid {field}")
                expected_metrics = tft_theory.capacity_metrics(measured["frontlineEhp"], measured["damageDps"],
                                                               measured["incomingDps"])
                if (not math.isclose(measured["score"], expected_metrics["theoryScore"], rel_tol=1e-9, abs_tol=1e-9)
                        or any(not math.isclose(measured[field], expected_metrics[field], rel_tol=1e-9, abs_tol=1e-9)
                               for field in ("damageCapacity", "protectionTime"))):
                    raise ValueError("theoretical scenario capacity arithmetic is inconsistent")
            for field in ("theoryScore", "frontlineEhp", "damageDps", "damageCapacity", "protectionTime"):
                if not _finite_nonnegative(row["metrics"].get(field)):
                    raise ValueError(f"theoretical metric {field} must be finite and nonnegative")
            aggregate = tft_theory.summarize(pressures)
            if any(not math.isclose(row["metrics"][field], value, rel_tol=1e-9, abs_tol=1e-9)
                   for field, value in aggregate.items()):
                raise ValueError("theoretical aggregate metrics disagree with the scenario measurements")
            units = row.get("units", [])
            level = row.get("level")
            if not units or type(level) is not int or level != self.profile["level"]:
                raise ValueError("finalized board lacks its roster or declared planning level")
            carry = next((unit["api"] for unit in units if unit["slug"] == row.get("mainCarry")), None)
            tank = next((unit["api"] for unit in units if unit["slug"] == row.get("mainTank")), None)
            if carry is None or tank is None or carry == tank:
                raise ValueError("finalized board lacks distinct main carry and tank")
            if any(type(unit.get("frontline")) is not bool for unit in units):
                raise ValueError("finalized board lacks its resolved frontline roles")
            tft_theory.validate_pressure_targets(pressures,
                (unit["api"] for unit in units if unit["frontline"]), tank)
            members = [{"api": unit["api"], "star": unit["star"]} for unit in units]
            selected = {unit["api"]: {"items": unit["itemApis"],
                        "alpha": unit["slug"] == row.get("alphaHolder")} for unit in units}
            primal = row.get("primal")
            resolved = resolve_board_traits(self.snap, members,
                       primal_blessings=primal["selected"] if primal is not None else None)
            if primal is not None:
                choices = primal_options(self.snap, members, primal.get("required"))
                if choices == [()]:
                    raise ValueError("finalized board selects Primal without its active trait")
                alternatives = primal.get("alternatives", [])
                if (not isinstance(alternatives, list) or len(alternatives) != len(choices)
                        or any(not isinstance(option, dict) or not isinstance(option.get("blessings"), list)
                               or not _finite_nonnegative(option.get("score")) for option in alternatives)):
                    raise ValueError("finalized Primal comparison lacks valid complete alternatives")
                scores = {tuple(option["blessings"]): option["score"] for option in alternatives}
                if set(scores) != set(choices):
                    raise ValueError("finalized Primal alternatives differ from its legal choices")
                chosen_score = scores[tuple(resolved["primalBlessings"])]
                if (chosen_score != max(scores.values())
                        or not math.isclose(chosen_score, row["metrics"]["theoryScore"], rel_tol=1e-12, abs_tol=1e-12)):
                    raise ValueError("finalized Primal selection does not match its best aggregate score")
            antiheal = AntihealPolicy(self.snap, members, resolved["effects"],
                                      profiles=getattr(self.team, "profiles", None))
            if not antiheal.legal(selected):
                raise ValueError("finalized board lacks a usable antiheal source")
            row["consistency"] = {"status": "passed", "modelRevision": expected_revision,
                                  "profileCount": len(pressures)}

    def level9_upgrade(self, row):
        if self.profile["cost"] != 4:
            return None
        from tft_caps import build_upgrade
        upgrade = build_upgrade(self, row)
        if upgrade is not None:
            selection = upgrade["selection"]
            self.team.stats["level9CapsComputed"] += 1
            self.team.stats["level9RostersCompared"] += selection["rostersCompared"]
            self.team.stats["level9AllocationsCompared"] += selection["allocationsCompared"]
        return upgrade

    def run(self):
        self.prepare_finalists()
        results = _empty_results()
        for budget, structure, index, count, candidate, refined in self.finalists():
            self.progress(f"Comparing all legal item replacements: {budget} items, {structure}, board {index}/{count}")
            allocation = self.final_items(candidate, refined, budget, structure)
            results[str(budget)][structure].append(self.compose(candidate, refined, budget, structure, allocation))
        _rank_results(results)
        self.validate_boards(_result_boards(results))
        if self.profile["cost"] == 4:
            rows = _result_boards(results)
            for index, row in enumerate(rows, 1):
                self.progress(f"Comparing optional level-9 upgrade {index}/{len(rows)}")
                row["level9Upgrade"] = self.level9_upgrade(row)
        return results


def _empty_results():
    return {str(budget): {structure["key"]: [] for structure in STRUCTURES} for budget in ITEM_BUDGETS}


def _finite_nonnegative(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def _result_boards(results):
    return [row for groups in results.values() for rows in groups.values() for row in rows]


def _rank_results(results):
    for groups in results.values():
        for rows in groups.values():
            rows.sort(key=lambda row: (tft_theory.rank_key(row), row["id"]))
            previous, rank = None, 0
            for index, row in enumerate(rows, 1):
                score = tft_theory.rank_key(row)
                if score != previous:
                    rank = index
                row["rank"] = rank
                previous = score


def warm_lock():
    import fcntl
    os.makedirs(CACHE_DIR, exist_ok=True)
    stream = open(os.path.join(CACHE_DIR, "lock"), "w")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        return None
    return stream


def warm_running():
    lock = warm_lock()
    if lock is None:
        return True
    lock.close()
    return False


def progress_state():
    try:
        return json.loads(Path(CACHE_DIR, "progress.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _scenario_payload(key, snap, progress=lambda message: None):
    sc = scenarios()[key]
    started = time.monotonic()
    search = Search(snap, PROFILES[sc["profile"]], sc["geometry"], sc["threat"], progress)
    results = search.run()
    return _assemble_payload(key, snap, results, {"boardsScreened": search.screened,
        "statesScreened": search.states_screened, "boardsRefined": len(search.refined),
        "exhaustive": False, **search.evaluator.stats, **search.team.stats}, started)


def _assemble_payload(key, snap, results, statistics, started):
    theory = tft_theory.metadata(snap, scenarios()[key]["geometry"])
    return {"revision": revision(snap), "baselineRevision": tft.snapshot_revision(snap),
            "modelRevision": theory["modelRevision"], "evaluationModel": theory["evaluationModel"],
            "set": snap.set_no, "patch": snap.patch, "boardSize": BOARD_SIZE,
            "boardPlanModel": BOARD_PLAN_MODEL, **scenarios()[key],
            "methodology": {**api_meta(snap)["methodology"], **theory["methodology"]},
            "scenarios": theory["scenarios"],
            "results": results, "search": {**statistics, "exhaustive": False},
            "computeSeconds": time.monotonic() - started,
            "computedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _worker_init(snap, cache_dir, expected_revision, progress):
    """Spawned workers use the supplied loaded generation, including staging."""
    global CACHE_DIR, _WORKER_SNAPSHOT, _WORKER_REVISION, _WORKER_PROGRESS
    CACHE_DIR = cache_dir
    _WORKER_SNAPSHOT, _WORKER_REVISION, _WORKER_PROGRESS = snap, expected_revision, progress
    if source_stale() or revision(snap) != expected_revision:
        raise RuntimeError("composition worker source/data revision differs from its parent")


def _worker_task(task):
    """One dependency phase or one finalist; native work stays single-threaded."""
    if _WORKER_SNAPSHOT is None:
        raise RuntimeError("composition worker has no supplied snapshot")
    key, phase = task["key"], task["phase"]
    def progress(message):
        if _WORKER_PROGRESS is not None:
            _WORKER_PROGRESS.put((key, message))
    if source_stale() or revision(_WORKER_SNAPSHOT) != _WORKER_REVISION:
        raise RuntimeError("composition worker inputs changed before calculation")
    sc = scenarios()[key]
    search = Search(_WORKER_SNAPSHOT, PROFILES[sc["profile"]], sc["geometry"], sc["threat"], progress)
    if phase == "prepare":
        search.prepare_finalists()
        output = {"jobs": [search.item_job(*entry) for entry in search.finalists()],
                  "search": {"boardsScreened": search.screened, "statesScreened": search.states_screened,
                             "boardsRefined": len(search.refined), "exhaustive": False}}
    elif phase == "items":
        job = task["job"]
        budget, structure, candidate, refined = (job[field] for field in ("budget", "structure", "candidate", "refined"))
        progress(f"Comparing all legal item replacements: {budget} items, {structure}, board {job['index']}/{job['count']}")
        allocation = search.final_items(candidate, refined, budget, structure, anchors=job["anchors"])
        output = {"ordinal": task["ordinal"],
                  "row": search.compose(candidate, refined, budget, structure, allocation)}
    elif phase == "validate":
        rows = task["rows"]
        search.validate_boards(rows)
        output = {"rows": [{field: row[field] for field in ("id", "consistency")} for row in rows]}
    elif phase == "cap":
        parent = task["row"]
        progress(f"Comparing optional level-9 upgrade {task['ordinal'] + 1}/{task['count']}")
        output = {"ordinal": task["ordinal"], "parentId": parent["id"],
                  "upgrade": search.level9_upgrade(parent)}
    else:
        raise ValueError(f"unknown composition worker phase {phase}")
    output.setdefault("search", {}).update({**search.evaluator.stats, **search.team.stats})
    if source_stale() or revision(_WORKER_SNAPSHOT) != _WORKER_REVISION:
        raise RuntimeError("composition worker inputs changed during calculation")
    return {"key": key, "phase": phase, "revision": _WORKER_REVISION,
            "baselineRevision": tft.snapshot_revision(_WORKER_SNAPSHOT), "result": output}


def _validation_inputs(results):
    # Rankings are fixed. Carry legality also needs equipped roster inputs;
    # combat contributions and replacement evidence stay with item workers.
    return [{**{field: row[field] for field in ("id", "rank", "evaluationModel", "modelRevision",
                                             "profileCount", "scenarios", "metrics", "level",
                                             "mainCarry", "mainTank", "alphaHolder")},
             **({"primal": row["primal"]} if "primal" in row else {}),
             "units": [{field: unit[field] for field in ("api", "slug", "star", "itemApis", "frontline")}
                       for unit in row["units"]]}
            for row in _result_boards(results)]


def _cap_input(row):
    # Caps keep the actual selected board/items. Replacement evidence is
    # already finalized and does not need to cross another process boundary.
    return {key: value for key, value in row.items() if key not in ("itemAnalysis", "level9Upgrade")}


def _parallel_scenarios(keys, snap, workers, expected_revision, progress, publish):
    """Dynamically share finalists from every context across one bounded pool.

    Only preparation depends on prior roster results. Item searches use the
    exact selected seeds independently; their completion order never selects
    a board or breaks a tie. A context can enter validation only after every
    item job has returned and its final ordering and ranks are fixed.

    At most ``workers`` futures are submitted. Completed context state is
    released after the lock-owning parent validates and atomically publishes
    it; failed contexts cannot schedule further phases or publish a fragment.
    """
    context = multiprocessing.get_context("spawn")
    updates = context.Queue()
    executor = ProcessPoolExecutor(max_workers=workers, mp_context=context,
        initializer=_worker_init, initargs=(snap, CACHE_DIR, expected_revision, updates))
    errors = {}
    states = {key: {"started": None, "jobs": deque(), "targets": [], "rows": {},
                    "search": Counter(), "results": None, "capJobs": deque(),
                    "capsRemaining": set()} for key in keys}
    unprepared, ready, validations, caps = deque(keys), deque(), deque(), deque()
    baseline_revision = tft.snapshot_revision(snap)

    def failed(task, error):
        key = task["key"]
        message = str(error) or type(error).__name__
        label = task["phase"] + (f" #{task['ordinal']}" if "ordinal" in task else "")
        errors[key] = "; ".join(filter(None, (errors.get(key), f"{label}: {message}")))
        states[key]["jobs"].clear()
        states[key]["rows"].clear()
        states[key]["capJobs"].clear()
        states[key]["capsRemaining"].clear()
        states[key]["results"] = None
        progress(key, "Calculation failed: " + errors[key])

    def next_task():
        # Preparing all requested contexts first makes all finalists available
        # for load balancing; the common eight-worker run starts them together.
        while validations:
            key = validations.popleft()
            if key not in errors:
                return {"key": key, "phase": "validate", "rows": _validation_inputs(states[key]["results"])}
        while unprepared:
            key = unprepared.popleft()
            if key not in errors:
                states[key]["started"] = time.monotonic()
                return {"key": key, "phase": "prepare"}
        while ready:
            key = ready.popleft()
            state = states[key]
            if key in errors or not state["jobs"]:
                continue
            ordinal, job = state["jobs"].popleft()
            if state["jobs"]:
                ready.append(key)
            return {"key": key, "phase": "items", "ordinal": ordinal, "job": job}
        # Base-board calculations retain priority. Optional caps can use
        # otherwise free workers after their own parents are fully selected.
        while caps:
            key = caps.popleft()
            state = states[key]
            if key in errors or not state["capJobs"]:
                continue
            ordinal, row = state["capJobs"].popleft()
            if state["capJobs"]:
                caps.append(key)
            return {"key": key, "phase": "cap", "ordinal": ordinal,
                    "row": _cap_input(row), "count": len(_result_boards(state["results"]))}
        return None

    def publish_complete(key):
        state = states[key]
        publish(key, _assemble_payload(key, snap, state["results"], dict(state["search"]), state["started"]))
        del states[key]

    def finish_items(key):
        state = states[key]
        results = _empty_results()
        for ordinal, (budget, structure) in enumerate(state["targets"]):
            results[str(budget)][structure].append(state["rows"].pop(ordinal))
        _rank_results(results)
        state["results"] = results
        validations.append(key)

    def completed(task, envelope):
        key, phase = task["key"], task["phase"]
        if (envelope.get("key") != key or envelope.get("phase") != phase
                or envelope.get("revision") != expected_revision
                or envelope.get("baselineRevision") != baseline_revision
                or source_stale() or revision(snap) != expected_revision):
            raise RuntimeError("source/data revision or worker phase changed; result was not accepted")
        if key in errors:
            return
        state, result = states[key], envelope["result"]
        state["search"].update(result.get("search", {}))
        if phase == "prepare":
            jobs = result["jobs"]
            state["targets"] = [(job["budget"], job["structure"]) for job in jobs]
            state["jobs"].extend(enumerate(jobs))
            if jobs:
                ready.append(key)
                progress(key, f"Sharing {len(jobs)} independent item searches across {workers} workers")
            else:
                finish_items(key)
        elif phase == "items":
            row, ordinal = result["row"], task["ordinal"]
            budget, structure = state["targets"][ordinal]
            if (result.get("ordinal") != ordinal or row.get("structure") != structure
                    or row.get("itemCount") != budget or ordinal in state["rows"]):
                raise RuntimeError("item result does not match its scheduled finalist")
            state["rows"][ordinal] = row
            if len(state["rows"]) == len(state["targets"]):
                finish_items(key)
        elif phase == "validate":
            rows = _result_boards(state["results"])
            additions = result["rows"]
            if [row["id"] for row in rows] != [row["id"] for row in additions]:
                raise RuntimeError("consistency results do not match the finalized board order")
            for row, addition in zip(rows, additions, strict=True):
                row["consistency"] = addition["consistency"]
            if PROFILES[scenarios()[key]["profile"]]["cost"] == 4 and rows:
                state["capJobs"].extend(enumerate(rows))
                state["capsRemaining"] = set(range(len(rows)))
                caps.append(key)
            else:
                publish_complete(key)
        elif phase == "cap":
            ordinal = task["ordinal"]
            parent = _result_boards(state["results"])[ordinal]
            upgrade = result["upgrade"]
            if (result.get("ordinal") != ordinal or result.get("parentId") != parent["id"]
                    or ordinal not in state["capsRemaining"]
                    or (upgrade is not None and upgrade.get("parentId") != parent["id"])):
                raise RuntimeError("level-9 result does not match its finalized level-8 parent")
            parent["level9Upgrade"] = upgrade
            state["capsRemaining"].remove(ordinal)
            if not state["capsRemaining"]:
                publish_complete(key)
        else:
            raise RuntimeError("unexpected composition worker phase")

    try:
        pending = {}
        while True:
            while len(pending) < workers and (task := next_task()) is not None:
                try:
                    pending[executor.submit(_worker_task, task)] = task
                except Exception as error:
                    failed(task, error)
            if not pending:
                break
            done, _ = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            while True:
                try:
                    key, message = updates.get_nowait()
                except Empty:
                    break
                progress(key, message)
            for future in done:
                task = pending.pop(future)
                try:
                    # Worker SystemExit/PyO3 failures can derive directly from
                    # BaseException; inspect them without treating them as a
                    # parent interrupt and abandoning other worker results.
                    failure = future.exception()
                    if failure is not None:
                        failed(task, failure)
                    else:
                        completed(task, future.result())
                except Exception as error:
                    failed(task, error)
        executor.shutdown(wait=True)
        # Progress uses a separate multiprocessing feeder. Drain after the
        # workers exit as well so the final phase updates are not dropped.
        while True:
            try:
                key, message = updates.get_nowait()
            except Empty:
                break
            progress(key, message)
    except BaseException:
        # Python 3.13 has no public terminate_workers(). Stop running children
        # on an interrupt before releasing the parent-held warm lock.
        processes = list((getattr(executor, "_processes", None) or {}).values())
        executor.shutdown(wait=False, cancel_futures=True)
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        raise
    finally:
        updates.close()
        updates.join_thread()
    return errors


def warm(log=print, only=None, *, snap=None, prune=False, workers=1):
    """Calculate cold scenarios; direct callers remain serial by default."""
    snap = snap or tft.load_snapshot()
    if only is not None and only not in scenarios():
        raise ValueError(f"unknown composition scenario {only}")
    if type(workers) is not int or not 1 <= workers <= MAX_WARM_WORKERS:
        raise ValueError(f"composition workers must be between 1 and {MAX_WARM_WORKERS}")
    lock = warm_lock()
    if lock is None:
        return None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    paths = cell_paths(snap)
    ordered = sorted(scenarios(), key=lambda key: (not key.endswith("clump-mixed"), key))
    requested = [key for key in ordered if only is None or key == only]
    cold = [key for key in requested if not os.path.exists(paths[key])]
    # One cold context still has many independent finalist jobs. Explicit
    # --only keeps its established serial behavior for targeted calculations.
    workers = workers if cold and only is None else 1
    expected_revision, baseline_revision = revision(snap), tft.snapshot_revision(snap)
    completed = 0
    current = None
    errors = {}
    def progress(key, message):
        nonlocal current
        current = key
        state = {"revision": expected_revision, "key": key, "message": message,
                 "status": "running", "completed": completed, "total": len(requested), "workers": workers}
        _atomic(Path(CACHE_DIR, "progress.json"), state)
        log(f"{key}: {message}")

    def publish(key, payload):
        nonlocal completed
        if (payload.get("key") != key or payload.get("revision") != expected_revision
                or payload.get("baselineRevision") != baseline_revision
                or source_stale() or revision(snap) != expected_revision):
            raise RuntimeError(f"{key}: source/data revision changed; result was not published")
        _atomic(paths[key], payload)
        completed += 1
        progress(key, f"Ready in {payload['computeSeconds']:.1f}s")

    try:
        if workers > 1:
            progress(cold[0], f"Comparing {len(cold)} scenarios with {workers} workers")
            errors = _parallel_scenarios(cold, snap, workers, expected_revision, progress, publish)
        else:
            for key in cold:
                current = key
                payload = _scenario_payload(key, snap, lambda message: progress(key, message))
                publish(key, payload)
        missing = [key for key in requested if not os.path.exists(paths[key])]
        if errors or missing:
            raise RuntimeError("Composition scenarios failed or remain missing: " + "; ".join(
                f"{key}: {errors.get(key, 'no ready artifact')}" for key in sorted(set(errors) | set(missing))))
        _atomic(Path(CACHE_DIR, "progress.json"), {"revision": expected_revision, "status": "complete",
            "message": "Composition results are ready", "completed": completed,
            "ready": len(requested), "total": len(requested), "workers": workers})
        return completed
    except BaseException as error:
        _atomic(Path(CACHE_DIR, "progress.json"), {"revision": expected_revision, "key": current,
            "status": "failed", "message": f"Composition calculation stopped: {error}",
            "completed": completed, "total": len(requested), "errors": errors,
            "missing": [key for key in requested if not os.path.exists(paths[key])]})
        raise
    finally:
        lock.close()


def cmd_warm(args):
    count = warm(only=getattr(args, "only", None), workers=getattr(args, "workers", DEFAULT_WARM_WORKERS))
    if count is None:
        print("Another composition calculation is already running.")
    else:
        print(f"{count} composition scenarios calculated.")
