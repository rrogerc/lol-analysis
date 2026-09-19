"""Symmetric champion fights against a fixed, independently authored pool.

Search and held-out validation rosters are disjoint. Both initiatives are
evaluated; only wins determine rank, never surviving HP or healing volume.
"""
from collections import Counter, OrderedDict
from copy import deepcopy
from fractions import Fraction
from functools import lru_cache
import json
import math
from pathlib import Path

import tft
import tft_board
import tft_match_cache
from tft_comp_traits import RIFTBEAST, resolve_board_traits

MODEL = "symmetric-reference-pool-v1"
DURATION = 30.0
POOL_FILENAME = "composition-opponents.json"
ITEM_BUDGETS = tuple(range(6, 13))
RESULT_CACHE_LIMIT = 2048
PREPARED_ACTOR_CACHE_LIMIT = 4096
ALLOCATION_BATCH_SIZE = 32
SCORE_MODEL_HASH = tft.json_hash([Path(__file__).read_text(), tft_match_cache.SOURCE_HASH,
                                 tft_board.SOURCE_HASH])
THREATS = {"mixed": "Reference boards"}
INITIATIVES = (0, 1)
LANE_ORDER = (3, 1, 5, 2, 4, 0, 6)


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


def pool_path(snap_or_set):
    set_no = snap_or_set.set_no if hasattr(snap_or_set, "set_no") else snap_or_set
    return Path(tft.set_dir(set_no), POOL_FILENAME)


@lru_cache(maxsize=4)
def _parse_pool(text):
    return json.loads(text)


def load_pool(snap):
    """Read authored inputs, never composition-search output files."""
    data = _parse_pool(pool_path(snap).read_text())
    validate_pool(snap, data)
    return data


def validate_pool(snap, data):
    """Reject illegal fixtures before optimization or publication starts."""
    if (not data.get("version") or data.get("boardSize") != tft_board.DEFAULT_BOARD_SLOTS
            or data.get("itemBudgets") != list(ITEM_BUDGETS)):
        raise ValueError("reference pool needs a version, eight board slots and item budgets 6 through 12")
    lane_offsets = data.get("laneOffsets")
    if (not isinstance(lane_offsets, list) or not lane_offsets or lane_offsets[0] != 0
            or any(type(offset) is not int or not 0 <= offset <= 6 for offset in lane_offsets)
            or len(set(lane_offsets)) != len(lane_offsets)):
        raise ValueError("reference lane offsets must start at zero and use distinct lanes from zero through six")
    ids, rosters = set(), set()
    split_counts, screen_count = Counter(), 0
    search_carries = set()
    legal_items = set(tft.pool_items(snap, tft.load_item_effects(snap.set_no)))
    modeled_units = {unit["api"] for unit in tft.modeled_units(snap)}
    for board in data.get("boards", []):
        identity = board.get("id")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ValueError("reference board IDs must be distinct nonempty strings")
        ids.add(identity)
        split = board.get("split")
        if split not in ("search", "validation"):
            raise ValueError(f"{identity}: reference split must be search or validation")
        split_counts[split] += 1
        if split == "search":
            search_carries.add(board.get("mainCarry"))
        if board.get("screen"):
            if split != "search":
                raise ValueError(f"{identity}: held-out boards cannot enter screening")
            screen_count += 1
        members = board.get("units", [])
        if not members or len({member.get("api") for member in members}) != len(members):
            raise ValueError(f"{identity}: expected distinct champions filling eight board slots")
        if any(member.get("api") not in modeled_units for member in members):
            raise ValueError(f"{identity}: unknown or unmodeled reference champion")
        if tft_board.slots_used(members) != tft_board.DEFAULT_BOARD_SLOTS:
            raise ValueError(f"{identity}: reference champions must fill exactly eight board slots")
        roster = tuple(sorted(member["api"] for member in members))
        if roster in rosters:
            raise ValueError("reference rosters must be distinct, including across search and validation")
        rosters.add(roster)
        cost = board.get("costPlan")
        carry, tank = board.get("mainCarry"), board.get("mainTank")
        if (cost not in (1, 2, 3, 4) or carry not in roster or tank not in roster or carry == tank
                or snap.units[carry]["cost"] != cost or snap.units[tank]["cost"] != cost
                or snap.units[carry]["objective"] == "tank" or snap.units[tank]["objective"] != "tank"):
            raise ValueError(f"{identity}: invalid same-cost carry/tank plan")
        costs = Counter(snap.units[api]["cost"] for api in roster)
        if (costs[cost] < 4 or costs[5] > (0, 0, 1, 0)[cost - 1]
                or costs[4] > (1, 2, 3, 8)[cost - 1]):
            raise ValueError(f"{identity}: reference cost distribution exceeds its declared plan")
        positions = set()
        for member in members:
            unit = snap.units[member["api"]]
            expected_star = 3 if member["api"] in (carry, tank) and cost <= 3 else 1 if unit["cost"] == 5 else 2
            if type(member.get("star")) is not int or member["star"] != expected_star:
                raise ValueError(f"{identity}: invalid reference star level")
            front = unit["objective"] in ("tank", "fighter")
            lane = member.get("lane")
            if (type(member.get("frontline")) is not bool or member["frontline"] != front
                    or type(lane) is not int or not 0 <= lane <= 6
                    or type(member.get("priority")) is not int):
                raise ValueError(f"{identity}: invalid reference position")
            if (front, lane) in positions:
                raise ValueError(f"{identity}: duplicate reference position")
            positions.add((front, lane))
        if not 3 <= sum(member["frontline"] for member in members) <= 5:
            raise ValueError(f"{identity}: expected three to five frontline champions")
        priorities = board.get("itemPriority", [])
        if len(priorities) != max(ITEM_BUDGETS):
            raise ValueError(f"{identity}: declare one item priority for every budget slot")
        equipped, unique = Counter(), Counter()
        for index, entry in enumerate(priorities, 1):
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError(f"{identity}: invalid item priority entry")
            api, item = entry
            if api not in roster or item not in legal_items:
                raise ValueError(f"{identity}: invalid item holder or non-craftable item")
            equipped[api] += 1
            unique[api, item] += 1
            if equipped[api] > 3 or (snap.items[item].get("unique") and unique[api, item] > 1):
                raise ValueError(f"{identity}: illegal completed-item loadout")
            if index == min(ITEM_BUDGETS) and (equipped[carry] != 3 or equipped[tank] != 3):
                raise ValueError(f"{identity}: the first six items must equip the main carry and tank")
        resolved = resolve_board_traits(snap, members, board.get("alphaHolder"))
        rift_active = any(trait["api"] == RIFTBEAST and trait["active"] for trait in resolved["traits"])
        if rift_active != bool(board.get("alphaHolder")):
            raise ValueError(f"{identity}: active Riftbeast needs exactly one declared Alpha holder")
    if not split_counts["search"] or not split_counts["validation"] or not screen_count:
        raise ValueError("reference pool needs search, held-out and search-screening boards")
    required_carries = {api for api in modeled_units
                        if snap.units[api]["cost"] == 4 and snap.units[api]["objective"] != "tank"}
    if missing := required_carries - search_carries:
        raise ValueError("reference search pool is missing supported four-cost carries: " + ", ".join(sorted(missing)))


def pool_metadata(snap):
    data = load_pool(snap)
    counts = Counter(board["split"] for board in data["boards"])
    screen_count = sum(bool(board["screen"]) for board in data["boards"])
    position_count = len(data["laneOffsets"])
    encounters_per_board = position_count * len(INITIATIVES)
    return {"version": data["version"], "hash": tft.json_hash(data),
            "level": tft_board.DEFAULT_BOARD_SLOTS, "boardSlots": tft_board.DEFAULT_BOARD_SLOTS,
            "authoredForPatch": data["authoredForPatch"], "searchBoards": counts["search"],
            "validationBoards": counts["validation"], "screenBoards": screen_count,
            "initiatives": list(INITIATIVES), "laneOffsets": list(data["laneOffsets"]),
            "positionVariants": position_count,
            "searchEncounters": counts["search"] * encounters_per_board,
            "validationEncounters": counts["validation"] * encounters_per_board,
            "screenEncounters": screen_count * encounters_per_board,
            "budgetRule": data["construction"], "description": data["description"],
            "validation": data["validation"], "formation": data["formation"],
            "sourceLimitations": [
                "Reference boards use the existing champion drivers and archived numbers; unresolved trait choices and prior stacks retain their declared model defaults.",
                "Fixed lanes approximate positioning and local effects; full hex movement is not simulated.",
                "Default broad healing allows burns and other modeled proc damage to trigger healing, and persistent effects can heal allies after their source dies.",
                "Proc and post-death healing eligibility remains unverified. Restricted-policy validation disables both as a sensitivity experiment, not a claim about correct game mechanics.",
                "Hand-authored fixtures are not claims about live meta frequency or optimal opponent items."],
            "boards": [{**{field: board[field] for field in (
                "id", "label", "split", "screen", "archetype", "costPlan", "mainCarry", "mainTank")},
                        "level": tft_board.DEFAULT_BOARD_SLOTS, "boardSlots": tft_board.DEFAULT_BOARD_SLOTS,
                        "slotsUsed": tft_board.slots_used(board["units"]), "unitCount": len(board["units"])}
                       for board in data["boards"]]}


def pool_revision(snap, budget):
    if type(budget) is not int or budget not in ITEM_BUDGETS:
        raise ValueError("reference opponents require an item budget from 6 through 12")
    return tft.json_hash([load_pool(snap), budget, tft.snapshot_revision(snap)])


def rank_key(result):
    """Only win rate ranks results; equal rates remain genuine outcome ties."""
    metrics = result.get("metrics", result)
    return (-Fraction(metrics["benchmarkWins"], metrics["benchmarkCount"]),)


def opponent_suite(snap, threat="mixed", *, budget=9, split="search", subset=None):
    """Resolve fixed reference rosters and item priorities for one budget.

    Legacy pressure keys are aliases: they do not scale, replace or weight the
    opponents. Screening can only use search boards.
    """
    if threat not in ("mixed", "physical", "magic"):
        raise ValueError(f"unknown composition context {threat!r}")
    if type(budget) is not int or budget not in ITEM_BUDGETS:
        raise ValueError("reference opponents require an item budget from 6 through 12")
    if split not in ("search", "validation") or subset not in (None, "screen"):
        raise ValueError("unknown opponent split or subset")
    if split == "validation" and subset is not None:
        raise ValueError("held-out opponents cannot be used for screening")
    data = load_pool(snap)
    suite = []
    resolver = Evaluator(snap, "clump")
    for board in data["boards"]:
        if board["split"] != split or (subset == "screen" and not board["screen"]):
            continue
        members = [{"api": member["api"], "star": member["star"]} for member in board["units"]]
        resolved = resolve_board_traits(snap, members, board["alphaHolder"])
        selected = {member["api"]: {"items": [], "alpha": member["api"] == board["alphaHolder"]} for member in members}
        for api, item in board["itemPriority"][:budget]:
            selected[api]["items"].append(item)
        roster = []
        for member in board["units"]:
            unit, option = snap.units[member["api"]], selected[member["api"]]
            option["items"] = tuple(option["items"])
            option["count"] = len(option["items"])
            roster.append({**member, "name": unit["name"], "slug": tft.unit_slug(unit), "cost": unit["cost"],
                           "slotCost": tft_board.unit_slots(member["api"]),
                           "itemApis": list(option["items"]), "items": [snap.items[item]["name"] for item in option["items"]],
                           "alpha": option["alpha"]})
        roles = resolver.combat_layout(members, resolved["effects"], selected,
                                       board["mainCarry"], board["mainTank"])
        roster = _reference_positions(roster, roles)
        suite.append({"key": board["id"], "label": board["label"], "opponentId": board["id"],
                      "level": tft_board.DEFAULT_BOARD_SLOTS, "boardSlots": tft_board.DEFAULT_BOARD_SLOTS,
                      "slotsUsed": tft_board.slots_used(members), "unitCount": len(members),
                      "opponentVersion": data["version"], "archetype": board["archetype"],
                      "costPlan": board["costPlan"], "members": members, "selected": selected,
                      "carry": board["mainCarry"], "tank": board["mainTank"], "roster": roster,
                      "effects": resolved["effects"], "traits": [trait for trait in resolved["traits"] if trait["count"]],
                      "limitations": resolved["limitations"],
                      "purchaseGold": sum(unit["cost"] * (1, 3, 9)[unit["star"] - 1] for unit in roster)})
    return suite


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
    """One fixed pool per context/budget, with bounded allocation memoization."""
    def __init__(self, snap, geometry, threat="mixed", *, prepared=True, workers=1, score_cache_dir=None):
        if geometry not in tft.GEOMETRIES or threat not in ("mixed", "physical", "magic"):
            raise ValueError("unknown symmetric reference context")
        if type(workers) is not int or not 1 <= workers <= 8:
            raise ValueError("match workers must be between 1 and 8")
        self.snap, self.geometry, self.threat = snap, geometry, threat
        self.prepared, self.workers = prepared, workers
        self.pool = pool_metadata(snap)
        self.item_fx = tft.load_item_effects(snap.set_no)
        self.templates, self.items = {}, {}
        self._forms = OrderedDict()
        self.results = OrderedDict()
        self._opponents, self._encounters, self._revisions = {}, {}, {}
        self._prepared_actors, self._prepared_opponents = OrderedDict(), {}
        self._base_input_hashes, self._item_input_hashes, self._opponent_input_hashes = {}, {}, {}
        self.stats = Counter()
        self._score_cache = None
        if score_cache_dir is not None:
            namespace = tft.json_hash(["tft-exact-match-scores-v1", tft.snapshot_revision(snap), geometry,
                                       SCORE_MODEL_HASH])
            self._score_cache = tft_match_cache.ScoreCache(score_cache_dir, namespace)

    def spec(self, api, star, effects, items, alpha=False):
        items = tuple(sorted(items))
        signature = api, star, tft.json_hash(effects), alpha
        if signature not in self.templates:
            # simulate_match derives actual opposing target stats first.
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
                # compose_fx uses the same native form selection as combat.
                # Its standalone parser requires a target, which is irrelevant
                # when resolving only opening effects.
                probe = dict(result, dummies={"slots": [{"hp": 1.0, "armor": 0.0, "mr": 0.0}]})
                self._forms[key] = tft.engine().compose_fx(probe)["form"]
                while len(self._forms) > PREPARED_ACTOR_CACHE_LIMIT:
                    self._forms.popitem(last=False)
            else:
                self._forms.move_to_end(key)
            form = self._forms[key]
        result["unit"] = dict(result["unit"], form=form)
        if api == "TFT18_Nidalee" and form == "AD":
            # The archived tooltip calls the cougar a melee Assassin. Preserve
            # the base attack/AP tie-breaker so native form selection is stable.
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
        if (not 1 <= len(members) <= tft_board.MAX_BOARD_SLOTS
                or len({member["api"] for member in members}) != len(members)
                or tft_board.slots_used(members) > tft_board.MAX_BOARD_SLOTS):
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

    def encounters(self, budget, split="search", subset=None):
        key = budget, split, subset
        if key not in self._encounters:
            opponents = opponent_suite(self.snap, self.threat, budget=budget, split=split, subset=subset)
            encounters = []
            for opponent in opponents:
                for lane_offset in self.pool["laneOffsets"]:
                    enemy_key = budget, opponent["opponentId"], lane_offset
                    roster = [dict(unit, lane=(unit["lane"] + lane_offset) % 7)
                              for unit in opponent["roster"]]
                    if enemy_key not in self._opponents:
                        actors = self.allies(opponent["members"], opponent["effects"], opponent["selected"], opponent["carry"], opponent["tank"])
                        positions = {unit["api"]: unit for unit in roster}
                        for actor in actors:
                            position = positions[actor["spec"]["unit"]["api"]]
                            actor.update({field: position[field] for field in ("frontline", "lane", "priority")})
                        self._opponents[enemy_key] = actors
                    for initiative in INITIATIVES:
                        label = {field: opponent[field] for field in (
                            "label", "opponentId", "opponentVersion", "archetype", "traits", "limitations", "purchaseGold",
                            "level", "boardSlots", "slotsUsed", "unitCount")}
                        label.update(key=f"{opponent['opponentId']}-p{lane_offset}-i{initiative}",
                                     initiative=initiative, laneOffset=lane_offset, roster=roster,
                                     formation=self.geometry, poolSplit=split)
                        encounters.append({"label": label, "enemies": self._opponents[enemy_key], "initiative": initiative})
            self._encounters[key] = encounters
        return self._encounters[key]

    def _simulate_matches(self, specs, details):
        if self.prepared:
            return tft.engine().simulate_matches(specs, detail="full" if details else "compact",
                                                 trace=False, workers=self.workers)
        return [tft.engine().simulate_match(spec, False) for spec in specs]

    def evaluate(self, members, effects, selected, carry, tank, *, split="search", subset=None, healing_policy="broad"):
        """Full diagnostics for displayed boards and standalone comparisons."""
        return self.evaluate_many(members, effects, [selected], carry, tank, split=split,
                                  subset=subset, healing_policy=healing_policy, details=True)[0]

    def _summary(self, results, encounters, budget, split, subset, healing_policy, details, layout):
        summary = summarize(results, [encounter["label"] for encounter in encounters], details=details)
        for api, contribution in summary["units"].items():
            contribution.update(layout[api])
        summary.update(poolRevision=self._revisions[budget], poolSplit=split, itemBudget=budget,
                       opponentCount=len({encounter["label"]["opponentId"] for encounter in encounters}),
                       laneOffsets=list(self.pool["laneOffsets"]), subset=subset,
                       healingPolicy=healing_policy)
        return summary

    def _remember(self, identity, summary):
        self.results[identity] = summary
        while len(self.results) > RESULT_CACHE_LIMIT:
            self.results.popitem(last=False)

    def _score_key(self, identity, members, effects, selected, wound):
        """Hash the actual resolved combat definitions, not merely item IDs.

        Snapshot revisions are deliberately conservative, but callers can
        also supply an in-memory snapshot. These fingerprints distinguish
        changed resolved stats/kits even when its archive stamp is unchanged.
        Immutable definitions are hashed once and reused across item trials.
        """
        actors = []
        for member in sorted(members, key=lambda value: value["api"]):
            api, star = member["api"], member["star"]
            option = selected[api]
            alpha = bool(option.get("alpha"))
            base = api, star, tft.json_hash(effects[api]), alpha
            if base not in self._base_input_hashes:
                self._base_input_hashes[base] = tft.json_hash(self.spec(api, star, effects[api], (), alpha))
            item_hashes = []
            for item in sorted(option["items"]):
                key = api, item
                if key not in self.items:
                    self.items[key] = tft.item_spec(self.snap, item, self.item_fx, self.snap.units[api])
                if key not in self._item_input_hashes:
                    self._item_input_hashes[key] = tft.json_hash(self.items[key])
                item_hashes.append(self._item_input_hashes[key])
            actors.append((api, self._base_input_hashes[base], item_hashes))
        split, subset = identity[0][:2]
        budget = identity[1]
        opposing = budget, split, subset
        if opposing not in self._opponent_input_hashes:
            encounters = self.encounters(budget, split, subset)
            self._opponent_input_hashes[opposing] = tft.json_hash([
                (encounter["enemies"], encounter["initiative"]) for encounter in encounters])
        return bytes.fromhex(tft.json_hash([self._revisions[budget], identity, self.geometry, DURATION,
                                           wound, actors, self._opponent_input_hashes[opposing]]))

    def evaluate_many(self, members, effects, allocations, carry, tank, *, split="search", subset=None,
                      healing_policy="broad", details=False):
        """Evaluate every requested allocation, preserving input and fight order.

        Compact mode changes reporting only: every fight still resolves in
        full. Finalists request full diagnostics separately. Prepared handles
        reuse immutable inputs; native worlds always own fresh combat state.
        """
        if healing_policy not in ("broad", "restricted"):
            raise ValueError("healing policy must be broad or restricted")
        if split not in ("search", "validation") or subset not in (None, "screen"):
            raise ValueError("unknown opponent split or subset")
        if split == "validation" and subset is not None:
            raise ValueError("held-out opponents cannot be used for screening")
        keys, answers, missing, layouts = [], {}, {}, {}
        effects_hash = tft.json_hash(effects)
        for selected in allocations:
            layout = self.combat_layout(members, effects, selected, carry, tank)
            context = (split, subset, healing_policy,
                       tuple((member["api"], member["star"],
                              *(layout[member["api"]][field] for field in
                                ("frontline", "lane", "priority", "form", "kind")))
                             for member in sorted(members, key=lambda value: value["api"])),
                       effects_hash, carry, tank)
            budget = sum(len(option["items"]) for option in selected.values())
            if budget not in self._revisions:
                self._revisions[budget] = pool_revision(self.snap, budget)
            identity = (context, budget,
                        tuple((api, tuple(sorted(option["items"])), bool(option.get("alpha")))
                              for api, option in sorted(selected.items())), details)
            keys.append(identity)
            layouts[identity] = layout
            if identity in answers or identity in missing:
                self.stats["teamAllocationsReused"] += 1
            elif identity in self.results:
                self.results.move_to_end(identity)
                self.stats["teamAllocationsReused"] += 1
                answers[identity] = self.results[identity]
            else:
                missing[identity] = selected
        cache = self._score_cache if not details else None
        cache_keys = {}
        wound = tft.tank_debuffs(self.snap)["wound"]
        if cache is not None and missing:
            before = cache.stats.copy()
            cache_keys = {identity: self._score_key(identity, members, effects, selected, wound)
                          for identity, selected in missing.items()}
            found = cache.get_many(cache_keys.values())
            for identity in list(missing):
                if (results := found.get(cache_keys[identity])) is None:
                    continue
                budget = identity[1]
                encounters = self.encounters(budget, split, subset)
                if len(results) != len(encounters):
                    cache.discard(cache_keys[identity])
                    cache.stats["scoreCacheHits"] -= 1
                    cache.stats["scoreCacheMisses"] += 1
                    continue
                summary = self._summary(results, encounters, budget, split, subset, healing_policy, False, layouts[identity])
                answers[identity] = summary
                self._remember(identity, summary)
                del missing[identity]
                self.stats["cachedFightsReused"] += len(results)
            self.stats.update(cache.stats - before)
        todo = list(missing.items())
        for start in range(0, len(todo), ALLOCATION_BATCH_SIZE):
            specs, parts = [], []
            for identity, selected in todo[start:start + ALLOCATION_BATCH_SIZE]:
                budget = identity[1]
                encounters = self.encounters(budget, split, subset)
                allies = self.allies(members, effects, selected, carry, tank, prepared=self.prepared)
                parts.append((identity, budget, encounters, len(specs)))
                for encounter in encounters:
                    enemies = encounter["enemies"]
                    if self.prepared:
                        opponent = budget, encounter["label"]["opponentId"], encounter["label"]["laneOffset"]
                        if opponent not in self._prepared_opponents:
                            self._prepared_opponents[opponent] = [tft.engine().prepare_actor(entry) for entry in enemies]
                            self.stats["actorsPrepared"] += len(enemies)
                        enemies = self._prepared_opponents[opponent]
                    specs.append({"duration": DURATION, "geometry": self.geometry, "allies": allies,
                                  "enemies": enemies, "initiative": encounter["initiative"],
                                  "damageHealingFromProcs": healing_policy == "broad",
                                  "postDeathAllyHealing": healing_policy == "broad", "burnWound": wound})
            results = self._simulate_matches(specs, details)
            if len(results) != len(specs):
                raise RuntimeError("native match batch returned an incomplete result")
            self.stats["nativeMatchBatches"] += int(self.prepared)
            self.stats["sharedFightsSimulated"] += len(results)
            self.stats[f"{split}FightsSimulated"] += len(results)
            cache_entries = []
            for identity, budget, encounters, offset in parts:
                fights = results[offset:offset + len(encounters)]
                summary = self._summary(fights, encounters, budget, split, subset, healing_policy, details, layouts[identity])
                answers[identity] = summary
                self._remember(identity, summary)
                self.stats["teamAllocationsSimulated"] += 1
                if cache is not None:
                    cache_entries.append((cache_keys[identity], fights))
            if cache_entries:
                before = cache.stats.copy()
                cache.put_many(cache_entries)
                self.stats.update(cache.stats - before)
        return [answers[key] for key in keys]
