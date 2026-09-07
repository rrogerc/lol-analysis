"""Team-outcome item refinement and auditable one-item alternatives.

Every legal single replacement is evaluated on the search opponents. Transfers,
item exchanges, and a bounded set of paired replacements explore interactions.
No item name, healing category, ending HP, or held-out result receives a bonus.
"""
from collections import Counter
from itertools import combinations, product, zip_longest

import tft
import tft_team

PAIR_TRIAL_LIMIT = 64


def identity(selected):
    return tuple((api, tuple(sorted(option["items"])), bool(option.get("alpha")))
                 for api, option in sorted(selected.items()))


def arrangement(snap, selected, carry, tank):
    extra_carries = sum(api not in (carry, tank) and len(option["items"]) >= 2
                        and snap.units[api]["objective"] != "tank" for api, option in selected.items())
    extra_tanks = sum(api not in (carry, tank) and len(option["items"]) >= 2
                      and snap.units[api]["objective"] == "tank" for api, option in selected.items())
    if extra_carries > 1 or extra_tanks > 1:
        return None
    return "both" if extra_carries and extra_tanks else "duoCarry" if extra_carries else "duoTank" if extra_tanks else "single"


class ItemSearch:
    def __init__(self, snap, evaluator, members, effects, carry, tank, structure, anchors=None):
        self.snap, self.evaluator = snap, evaluator
        self.members, self.effects, self.carry, self.tank = members, effects, carry, tank
        self.structure = structure
        self.pool = tuple(tft.pool_items(snap, tft.load_item_effects(snap.set_no)))
        self.anchors = anchors or {}
        self.budget = None
        self.alpha_count = None
        self.memo = {}
        self.stats = Counter()

    def legal(self, selected):
        if set(selected) != {m["api"] for m in self.members}:
            return False
        counts = {api: len(option["items"]) for api, option in selected.items()}
        if sum(counts.values()) != self.budget or any(count > 3 for count in counts.values()):
            return False
        if counts[self.carry] < 2 or counts[self.tank] < 2:
            return False
        if sum(bool(option.get("alpha")) for option in selected.values()) != self.alpha_count:
            return False
        for api, option in selected.items():
            if any(item not in self.pool for item in option["items"]):
                return False
            if any(count > 1 and self.snap.items[item]["unique"] for item, count in Counter(option["items"]).items()):
                return False
            if api not in (self.carry, self.tank) and counts[api] >= 2:
                main = self.tank if self.snap.units[api]["objective"] == "tank" else self.carry
                if counts[api] > counts[main]:
                    return False
        return arrangement(self.snap, selected, self.carry, self.tank) == self.structure

    @staticmethod
    def changed(selected, api, items):
        return dict(selected, **{api: dict(selected[api], items=tuple(sorted(items)), count=len(items))})

    def evaluate(self, selected):
        return self.evaluate_many([selected])[0]

    def evaluate_many(self, selections):
        """Batch independent trials without changing their order or coverage."""
        keys, missing = [], {}
        for selected in selections:
            key = identity(selected)
            keys.append(key)
            if key not in self.memo and key not in missing:
                missing[key] = selected
        if missing:
            if callable(getattr(self.evaluator, "evaluate_many", None)):
                results = self.evaluator.evaluate_many(self.members, self.effects, list(missing.values()),
                    self.carry, self.tank, split="search", details=False)
            else:
                results = [self.evaluator.evaluate(self.members, self.effects, selected, self.carry,
                                                   self.tank, split="search") for selected in missing.values()]
            if len(results) != len(missing):
                raise RuntimeError("item comparison batch returned an incomplete result")
            self.memo.update(zip(missing, results))
            self.stats["itemAllocationsCompared"] += len(missing)
        return [self.memo[key] for key in keys]

    def singles(self, selected):
        trials = []
        for api, option in sorted(selected.items()):
            for old in sorted(set(option["items"])):
                slot = option["items"].index(old)
                for item in self.pool:
                    if item == old:
                        continue
                    items = list(option["items"])
                    items[slot] = item
                    trial = self.changed(selected, api, items)
                    if self.legal(trial):
                        trials.append((api, old, item, trial))
        self.stats["singleItemComparisons"] += len(trials)
        results = self.evaluate_many([trial[3] for trial in trials])
        return [(*trial, result) for trial, result in zip(trials, results)]

    def moves(self, selected):
        """Move a completed item, or exchange two holders' existing items."""
        seen = {identity(selected)}
        for donor, receiver in product(sorted(selected), repeat=2):
            if donor == receiver or len(selected[receiver]["items"]) == 3:
                continue
            for item in sorted(set(selected[donor]["items"])):
                left = list(selected[donor]["items"])
                left.remove(item)
                trial = self.changed(selected, donor, left)
                trial = self.changed(trial, receiver, (*trial[receiver]["items"], item))
                key = identity(trial)
                if key not in seen and self.legal(trial):
                    seen.add(key)
                    yield trial
        for left, right in combinations(sorted(selected), 2):
            for a, b in product(sorted(set(selected[left]["items"])), sorted(set(selected[right]["items"]))):
                if a == b:
                    continue
                left_items, right_items = list(selected[left]["items"]), list(selected[right]["items"])
                left_items.remove(a); left_items.append(b)
                right_items.remove(b); right_items.append(a)
                trial = self.changed(self.changed(selected, left, left_items), right, right_items)
                key = identity(trial)
                if key not in seen and self.legal(trial):
                    seen.add(key)
                    yield trial

    def pairs(self, selected, singles):
        """Selected two-item interactions; this is deliberately not exhaustive."""
        seen = {identity(selected)}
        groups = []
        # Complete damage/defense/utility loadouts can uncover a pair whose
        # two individual substitutions are both unhelpful on their own.
        for api in sorted(selected):
            current = Counter(selected[api]["items"])
            options = []
            for option in self.anchors.get(api, ()):
                if len(option["items"]) != len(selected[api]["items"]):
                    continue
                if bool(option.get("alpha")) != bool(selected[api].get("alpha")):
                    continue
                if sum((current - Counter(option["items"])).values()) != 2:
                    continue
                options.append(self.changed(selected, api, option["items"]))
            if options:
                groups.append(options)
        per_holder = {}
        for api in sorted(selected):
            candidates = [row for row in singles if row[0] == api]
            candidates.sort(key=lambda row: (tft_team.rank_key(row[4]), row[1], row[2]))
            per_holder[api] = candidates[:2]
        for left, right in combinations(sorted(selected), 2):
            options = []
            for a, b in product(per_holder[left], per_holder[right]):
                options.append(self.changed(a[3], right, b[3][right]["items"]))
            if options:
                groups.append(options)
        count = 0
        # Round-robin holders and holder pairs so one early champion's
        # larger seed library cannot consume the entire interaction budget.
        for trial in (entry for layer in zip_longest(*groups) for entry in layer if entry is not None):
            key = identity(trial)
            if key not in seen and self.legal(trial):
                seen.add(key)
                yield trial
                count += 1
                if count == PAIR_TRIAL_LIMIT:
                    return

    def optimize(self, seeds):
        if not seeds:
            raise ValueError("item refinement requires an initial allocation")
        seeds = [{api: dict(option, items=tuple(sorted(option["items"]))) for api, option in seed.items()} for seed in seeds]
        self.budget = sum(len(o["items"]) for o in seeds[0].values())
        self.alpha_count = sum(bool(o.get("alpha")) for o in seeds[0].values())
        if any(not self.legal(seed) for seed in seeds):
            raise ValueError("item refinement received an illegal starting allocation")
        winners = []
        for seed in seeds:
            selected = seed
            result = self.evaluate(selected)
            while True:
                singles = self.singles(selected)
                if result["metrics"]["benchmarkWins"] == result["metrics"]["benchmarkCount"]:
                    # No transfer, paired change, or other seed can exceed
                    # winning every search fight. Singles are still fully
                    # evaluated for the published replacement evidence.
                    self.stats["perfectScoreRefinements"] += 1
                    return selected, result, self.evidence(selected, result, singles)
                candidates = [(selected, result)] + [(row[3], row[4]) for row in singles]
                moves = list(self.moves(selected))
                pairs = list(self.pairs(selected, singles))
                self.stats["itemTransfersAndExchangesCompared"] += len(moves)
                self.stats["pairedItemChangesCompared"] += len(pairs)
                trials = moves + pairs
                candidates.extend(zip(trials, self.evaluate_many(trials)))
                improved, score = min(candidates, key=lambda pair: tft_team.rank_key(pair[1]))
                if tft_team.rank_key(score) >= tft_team.rank_key(result):
                    winners.append((selected, result, singles))
                    break
                # Strictly increasing wins on one fixed suite bound this
                # loop. Equal HP/time differences cannot prolong the search.
                selected, result = improved, score
                self.stats["acceptedItemImprovements"] += 1
        selected, result, singles = min(winners, key=lambda row: tft_team.rank_key(row[1]))
        return selected, result, self.evidence(selected, result, singles)

    def evidence(self, selected, baseline, singles):
        base_wins = baseline["metrics"]["benchmarkWins"]
        before = {m["key"] for m in baseline["matchups"] if m["outcome"] == "win"}
        holders = []
        for api, option in sorted(selected.items()):
            unit = self.snap.units[api]
            entries = []
            for slot, old in enumerate(option["items"]):
                alternatives = []
                for holder, original, replacement, _, result in singles:
                    if holder != api or original != old:
                        continue
                    after = {m["key"] for m in result["matchups"] if m["outcome"] == "win"}
                    wins = result["metrics"]["benchmarkWins"]
                    alternatives.append({"itemApi": replacement, "item": self.snap.items[replacement]["name"],
                                         "wins": wins, "winDelta": wins - base_wins,
                                         "lostMatchups": sorted(before - after), "gainedMatchups": sorted(after - before)})
                alternatives.sort(key=lambda row: (-row["wins"], row["itemApi"]))
                entries.append({"slot": slot, "itemApi": old, "item": self.snap.items[old]["name"],
                                "testedAlternatives": len(alternatives),
                                "equivalentAlternatives": sum(a["winDelta"] == 0 for a in alternatives),
                                "bestWinDelta": max((a["winDelta"] for a in alternatives), default=0),
                                "alternatives": alternatives})
            if entries:
                holders.append({"api": api, "slug": tft.unit_slug(unit), "name": unit["name"], "items": entries})
        return {"model": "team-item-replacements-v1", "evaluatedOn": "search",
                "poolRevision": baseline["poolRevision"], "matches": baseline["metrics"]["benchmarkCount"],
                "holders": holders}
