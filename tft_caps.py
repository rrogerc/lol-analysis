"""Practical level-nine transitions from already selected level-eight boards.

The main carry/tank, surviving stars and surviving item holders are fixed.
Only a sold support's completed items can move, and only onto newly bought
two-star five-costs. Search fights select the cap; held-out fights describe
the selected result afterwards. A cap never feeds back into its parent's
selection or rank.
"""
from collections import Counter
from copy import copy, deepcopy
from itertools import combinations, product

import tft
from tft_board import slots_used, unit_slots
from tft_comp_items import arrangement, identity
from tft_comp_traits import RIFTBEAST, resolve_board_traits


CAP_LEVEL = 9
CAP_FIVE_COST_STAR = 2
PREFERRED_FIVE_COST_SLOTS = 2
ITEM_POLICY = {
    "key": "retain-and-transfer",
    "description": "Retain surviving units' items; transfer only the sold unit's completed items to newly added champions.",
}


def _parent_state(search, row):
    units = row["units"]
    roster = [unit["api"] for unit in units]
    if (row.get("level", 8) != 8 or slots_used(roster) != 8
            or len(set(roster)) != len(roster)
            or any(search.snap.units[api]["cost"] == 5 for api in roster)):
        raise ValueError("a four-cost cap requires a complete level-eight board without five-costs")
    if "rank" not in row:
        raise ValueError("select and rank the level-eight board before planning its cap")
    carry = next((unit["api"] for unit in units if unit["slug"] == row["mainCarry"]), None)
    tank = next((unit["api"] for unit in units if unit["slug"] == row["mainTank"]), None)
    if carry is None or tank is None or carry == tank:
        raise ValueError("the parent board needs distinct main carry and tank units")
    if sum(len(unit["itemApis"]) for unit in units) != row["itemCount"]:
        raise ValueError("the parent board's item count does not match its holders")
    return {unit["api"]: unit for unit in units}, carry, tank


def _cap_profile(profile):
    return dict(profile, level=CAP_LEVEL, boardSlots=CAP_LEVEL, maxFiveCosts=2,
                maxFourCosts=CAP_LEVEL, minSameCost=3)


def _transitions(search, parent, carry, tank, profile):
    """Enumerate the complete, deliberately narrow practical transition policy."""
    import tft_comps

    fives = sorted(unit["api"] for unit in tft.modeled_units(search.snap)
                   if unit["cost"] == 5 and unit["api"] not in parent)
    ordinary = [api for api in fives if unit_slots(api) == 1]
    double = [api for api in fives if unit_slots(api) == 2]
    transitions = []
    for removed in sorted(set(parent) - {carry, tank}):
        transitions.extend((removed, added) for added in combinations(ordinary, 2))
        transitions.extend((removed, (api,)) for api in double)
    transitions.extend((None, (api,)) for api in ordinary)

    def order(transition):
        removed, added = transition
        five_slots = sum(unit_slots(api) for api in added)
        sold_gold = (parent[removed]["cost"] * (1, 3, 9)[parent[removed]["star"] - 1]
                     if removed is not None else 0)
        return (five_slots != PREFERRED_FIVE_COST_SLOTS, sold_gold, removed or "", added)

    return [(removed, added) for removed, added in sorted(transitions, key=order)
            if tft_comps.valid_board(search.snap,
                tuple(sorted((set(parent) - {removed}) | set(added))), carry, tank, profile)]


def _legal_allocation(snap, selected, carry, tank, budget):
    counts = {api: len(option["items"]) for api, option in selected.items()}
    if (sum(counts.values()) != budget or any(count > 3 for count in counts.values())
            or counts[carry] < 2 or counts[tank] < 2):
        return False
    for api, option in selected.items():
        if any(item not in snap.items for item in option["items"]):
            return False
        if any(count > 1 and snap.items[item]["unique"]
               for item, count in Counter(option["items"]).items()):
            return False
        if api not in (carry, tank) and counts[api] >= 2:
            main = tank if snap.units[api]["objective"] == "tank" else carry
            if counts[api] > counts[main]:
                return False
    return arrangement(snap, selected, carry, tank) is not None


def _allocations(snap, parent, removed, added, resolved, carry, tank, budget):
    """Every split of the sold items, crossed with every eligible Alpha holder."""
    retained = {api: {"items": tuple(unit["itemApis"]), "count": len(unit["itemApis"]), "alpha": False}
                for api, unit in parent.items() if api != removed}
    freed = tuple(sorted(parent[removed]["itemApis"])) if removed is not None else ()
    active_rift = any(trait["api"] == RIFTBEAST and trait["active"] for trait in resolved["traits"])
    alphas = sorted(api for api in (*retained, *added) if RIFTBEAST in snap.units[api]["traitApis"])
    alphas = alphas if active_rift else [None]
    selected_rows, seen = [], set()
    for destinations in product(range(len(added)), repeat=len(freed)):
        transferred = [[] for _ in added]
        for item, destination in zip(freed, destinations, strict=True):
            transferred[destination].append(item)
        selected = {api: dict(option) for api, option in retained.items()}
        selected.update({api: {"items": tuple(items), "count": len(items), "alpha": False}
                         for api, items in zip(added, transferred, strict=True)})
        if not _legal_allocation(snap, selected, carry, tank, budget):
            continue
        for alpha in alphas:
            trial = {api: dict(option, alpha=api == alpha) for api, option in selected.items()}
            key = identity(trial)
            if key not in seen:
                seen.add(key)
                selected_rows.append(trial)
    return selected_rows


def _search_wins(result, expected_count, parent):
    metrics = result["metrics"]
    wins, count = metrics["benchmarkWins"], metrics["benchmarkCount"]
    if type(wins) is not int or count != expected_count or not 0 <= wins <= count:
        raise ValueError("cap comparisons must use the parent's complete search opponent suite")
    if result.get("poolSplit", "search") != "search":
        raise ValueError("held-out fights cannot select a cap")
    if ("poolRevision" in parent and "poolRevision" in result
            and result["poolRevision"] != parent["poolRevision"]):
        raise ValueError("cap comparisons must use the parent's opponent revision")
    return wins


def _summary(unit):
    return {field: deepcopy(unit[field]) for field in (
        "api", "slug", "name", "cost", "star", "objective", "assignment", "slotCost", "itemApis", "items")
        if field in unit}


def _trait_changes(before, after):
    previous = {trait["api"]: trait for trait in before}
    current = {trait["api"]: trait for trait in after}
    result = []
    for api in sorted(previous.keys() | current.keys()):
        old, new = previous.get(api, {}), current.get(api, {})
        old_state = old.get("count", 0), old.get("breakpoint"), old.get("active", False)
        new_state = new.get("count", 0), new.get("breakpoint"), new.get("active", False)
        if old_state == new_state:
            continue
        result.append({"api": api, "name": new.get("name", old.get("name", api)),
                       "beforeCount": old_state[0], "afterCount": new_state[0],
                       "beforeBreakpoint": old_state[1], "afterBreakpoint": new_state[1],
                       "beforeActive": old_state[2], "afterActive": new_state[2]})
    return result


def build_upgrade(search, parent_row):
    """Return an independently chosen level-nine variant, leaving the parent intact.

    The bounded roster policy is exhaustive unless a preferred two-legendary-
    slot board wins every search fight. That is an exact upper bound: an unseen
    board cannot improve either the primary score or the legendary-slot tie
    preference. Ending health, damage and held-out outcomes never break ties.
    """
    if search.profile["cost"] != 4:
        return None
    parent, carry, tank = _parent_state(search, parent_row)
    profile = _cap_profile(search.profile)
    transitions = _transitions(search, parent, carry, tank, profile)
    budget = parent_row["itemCount"]
    expected_count = parent_row["metrics"]["benchmarkCount"]
    if type(expected_count) is not int or expected_count < 1:
        raise ValueError("a cap requires the parent's complete search benchmark")
    selection = {"evaluatedOn": "search", "parentRanking": "level8",
                 "fiveCostStar": CAP_FIVE_COST_STAR,
                 "preferredFiveCostSlots": PREFERRED_FIVE_COST_SLOTS,
                 "candidatesAvailable": len(transitions), "rostersCompared": 0,
                 "allocationsCompared": 0, "alphaAssignmentsCompared": 0,
                 "perfectScoreBoundReached": False, "testFightsPerAllocation": expected_count}
    best, best_key = None, None
    for removed, added in transitions:
        members = [{"api": api, "star": unit["star"]} for api, unit in parent.items() if api != removed]
        members.extend({"api": api, "star": CAP_FIVE_COST_STAR} for api in added)
        members.sort(key=lambda member: member["api"])
        resolved = resolve_board_traits(search.snap, members)
        trials = _allocations(search.snap, parent, removed, added, resolved, carry, tank, budget)
        if not trials:
            continue
        results = search.team.evaluate_many(members, resolved["effects"], trials,
                                            carry, tank, split="search", details=False)
        if len(results) != len(trials):
            raise RuntimeError("cap comparison batch returned an incomplete result")
        selection["rostersCompared"] += 1
        selection["allocationsCompared"] += len(trials)
        selection["alphaAssignmentsCompared"] += len({next((api for api, option in trial.items()
            if option["alpha"]), None) for trial in trials})
        five_slots = sum(unit_slots(api) for api in added)
        for selected, result in zip(trials, results, strict=True):
            key = (_search_wins(result, expected_count, parent_row),
                   five_slots == PREFERRED_FIVE_COST_SLOTS)
            if best_key is None or key > best_key:
                best_key = key
                best = {"members": members, "traits": resolved, "selected": selected,
                        "removed": removed, "added": added, "fiveCostSlots": five_slots}
        if best_key == (expected_count, True):
            selection["perfectScoreBoundReached"] = True
            break
    if best is None:
        return None

    members, resolved, selected = best["members"], best["traits"], best["selected"]
    result = search.team.evaluate(members, resolved["effects"], selected, carry, tank, split="search")
    if _search_wins(result, expected_count, parent_row) != best_key[0]:
        raise RuntimeError("full cap diagnostics disagree with the selected search result")
    # The expensive search is complete. Cheap standalone diagnostics now fill
    # the standard board payload, without being allowed to change the winner.
    detailed = {member["api"]: search.evaluator.loadout(member["api"], member["star"],
                resolved["effects"][member["api"]], selected[member["api"]]["items"],
                selected[member["api"]]["alpha"]) for member in members}
    cap_search = copy(search)
    cap_search.profile = profile
    cap_search.units = {**search.units, **{member["api"]: search.snap.units[member["api"]] for member in members}}
    structure = arrangement(search.snap, detailed, carry, tank)
    candidate = tuple(member["api"] for member in members), carry, tank
    board = cap_search.compose(candidate, {"members": members, "traits": resolved}, budget,
                               structure, cap_search.allocation(detailed, result))
    board["itemPolicy"] = deepcopy(ITEM_POLICY)
    board.pop("itemAnalysis", None)
    validation = search.team.evaluate(members, resolved["effects"], selected, carry, tank, split="validation")
    board["validation"] = {field: validation[field] for field in
        ("metrics", "matchups", "poolRevision", "poolSplit", "opponentCount", "itemBudget")}
    restricted = search.team.evaluate(members, resolved["effects"], selected, carry, tank,
                                    split="validation", healing_policy="restricted")
    board["assumptionCheck"] = {field: restricted[field] for field in
        ("metrics", "matchups", "poolRevision", "poolSplit", "opponentCount", "itemBudget", "healingPolicy")}
    board["assumptionCheck"]["winDelta"] = (restricted["metrics"]["benchmarkWins"]
                                          - validation["metrics"]["benchmarkWins"])
    by_api = {unit["api"]: unit for unit in board["units"]}
    removed, added = best["removed"], best["added"]
    transfers = [{"fromApi": removed, "fromSlug": parent[removed]["slug"],
                  "toApi": api, "toSlug": by_api[api]["slug"], "itemApi": item,
                  "item": search.snap.items[item]["name"]}
                 for api in added for item in selected[api]["items"]] if removed is not None else []
    selection.update(fiveCostSlots=best["fiveCostSlots"],
                     poolRevision=result["poolRevision"], itemBudget=budget)
    search.team.stats.update(capRostersCompared=selection["rostersCompared"],
                             capAllocationsCompared=selection["allocationsCompared"],
                             capPerfectScoreBounds=int(selection["perfectScoreBoundReached"]))
    return {"parentId": parent_row["id"], "board": board,
            "transition": {"removed": [_summary(parent[removed])] if removed is not None else [],
                           "added": [_summary(by_api[api]) for api in added],
                           "retained": [parent[api]["slug"] for api in sorted(parent) if api != removed],
                           "itemTransfers": transfers,
                           "traitChanges": _trait_changes(parent_row["traits"], board["traits"])},
            "selection": selection,
            "benchmarkWinDelta": result["metrics"]["benchmarkWins"] - parent_row["metrics"]["benchmarkWins"]}
