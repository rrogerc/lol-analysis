"""Composition scoring that credits removing an enemy, not raw output.

Why this exists. The published composition model
(`tft_theory`, `ehp-damage-capacity-v3`) scores a board as frontline EHP x
team DPS against three IMMORTAL probes, with the incoming pressure supplied
as a fixed external budget. Two things follow from that, and both of them
overvalue damage sprayed across a board:

* nothing dies, so damage that chips three enemies scores exactly as much as
  damage that removes one, and removal is the only thing that reduces what is
  coming back at you;
* there is no overkill and no target priority, so the last point of damage on
  an enemy already dead counts the same as the first.

Gromp is the clearest case: his whole ability is a cloud, and on the clumped
2-cost board he was the main carry of 163 of 208 published boards while being
a poor unit to actually play. `data/tft/README.md` records the measurement.

What this module does instead. It fights the candidate board against a small
suite of SYNTHETIC opponents through the existing two-sided engine
(`tft_engine/src/symmetric.rs`, driven by `tft_team.Evaluator`), where the
enemies have finite health, fight back, and stop doing either when they die.
The objective is how fast the board clears them.

The opponents are not authored — an authored suite is what demoted the
symmetric path to a diagnostic in the first place. They are built from the
set's own data the way `tft.dummies_for` builds the damage board's dummies:
the units closest to their group's median stats, holding the best three-item
build the champion leaderboard already computed for them. A composition
refresh warms every champion cell before it warms compositions, so those
builds are available by the time this runs.

What is deliberately NOT claimed. The opponent suite is a small, explicit
stress set, not a distribution over what a lobby actually fields; hex
movement, positioning choices and item inventories are still outside the
model, and the suite's own boards are as approximate as any other board this
repo builds. What changes is only that damage now has to kill something.
"""

import math
import statistics

import tft
import tft_board
import tft_team
from tft_comp_traits import resolve_board_traits

MODEL = "removal-clear-time-v1"

# How many of the opponent's eight slots stand in front, and what its backline
# deals. Both axes come from the data: the frontline count brackets the board
# shapes the set's own role split produces (tft.BOARD_SIZE split by the tank
# share rounds to three), and the damage type picks the median attacker of
# that type. Two of each gives four opponents, fought from both initiatives.
OPPONENT_FRONTLINES = (3, 4)
OPPONENT_DAMAGE = ("physical", "magic")

# The leaderboard scenario the opponent's members take their build from: the
# same star the damage board's dummies use, its own geometry, no traits.
OPPONENT_STAR = tft.DUMMY_STAR
OPPONENT_TRAITS = "bare"
OPPONENT_THREAT = "mixed"


def _profile(unit, star):
    """The numbers a slot is chosen on: durability for a frontliner, output
    for a backliner. Star-scaled exactly as the dummies are."""
    stats = unit["stats"]
    if unit["kind"] == "Tank":
        return (stats["hp"] * tft.HP_PER_STAR ** (star - 1), stats["armor"], stats["mr"])
    return (tft.unit_primary_damage(unit, star),
            stats["ad"] * tft.AD_PER_STAR ** (star - 1), stats["as"])


def _closest(units, star, count):
    """The `count` units whose profile sits closest to the group's median.

    Distance is the sum of |log| ratios to the median, so each number counts
    the same however it is scaled, and a unit missing one of them is skipped
    rather than given a substitute value.
    """
    rows = [(unit, _profile(unit, star)) for unit in units]
    rows = [(unit, profile) for unit, profile in rows if all(value > 0 for value in profile)]
    if len(rows) < count:
        raise ValueError("the snapshot has too few units to build a reference opponent")
    median = [statistics.median(values) for values in zip(*(profile for _, profile in rows))]
    def distance(row):
        return (math.fsum(abs(math.log(value / mid)) for value, mid in zip(row[1], median)),
                row[0]["api"])
    return [unit for unit, _ in sorted(rows, key=distance)[:count]]


def leaderboard_builds(snap, geometry, star=OPPONENT_STAR, paths=None):
    """Each unit's best three-item build under the matching damage/tank board."""
    key = f"s{star}-{geometry}-{OPPONENT_TRAITS}-{OPPONENT_THREAT}"
    board = tft.cached_leaderboard(key, paths, snap=snap)
    return {row["unitApi"]: tuple(row["itemApis"])
            for group in ("damage", "tanks") for row in board[group]}


def opponent_boards(snap, geometry, *, star=OPPONENT_STAR, paths=None, builds=None):
    """The synthetic reference opponents for one geometry.

    Each board fills `tft_board.DEFAULT_BOARD_SLOTS` with the set's most
    median units: tanks in front, attackers of the declared damage type
    behind. Its main carry and main tank hold the best three-item build the
    champion leaderboard found for them under the matching scenario, so the
    opponent's items come from the same calculation the rest of the dashboard
    publishes rather than from anyone's opinion.

    Raises if those cells are cold: an opponent assembled from whichever
    builds happened to be cached would make a composition score depend on
    cache state. `builds` supplies them directly instead — how the tests
    stand up an opponent without warming an archived snapshot.
    """
    if geometry not in tft.GEOMETRIES:
        raise ValueError(f"unknown target geometry {geometry!r}")
    if builds is None:
        builds = leaderboard_builds(snap, geometry, star, paths)
    tanks = [unit for unit in snap.units.values() if unit["kind"] == "Tank"]
    slots = tft_board.DEFAULT_BOARD_SLOTS
    out = []
    for frontline in OPPONENT_FRONTLINES:
        for damage in OPPONENT_DAMAGE:
            attackers = [unit for unit in snap.units.values()
                         if unit["kind"] != "Tank"
                         and ("physical" if unit["attack"] else "magic") == damage]
            front = _closest(tanks, star, frontline)
            back = _closest(attackers, star, slots - frontline)
            # A 4- or 5-cost has no legal 3-star, so a stronger reference
            # opponent raises only the units that can actually be raised.
            members = [{"api": unit["api"], "star": min(star, max(tft.unit_stars(unit)))}
                       for unit in front + back]
            if tft_board.slots_used(members) > slots:       # Elder Dragon takes two
                members = members[:-1]
            carry, tank = back[0]["api"], front[0]["api"]
            try:
                held = {api: list(builds[api]) for api in (carry, tank)}
            except KeyError as missing:
                raise ValueError(f"reference opponent {damage}/{frontline} has no build for "
                                 f"{missing.args[0]}; warm the champion leaderboard first") from None
            selected = {member["api"]: {"items": [], "alpha": False} for member in members}
            selected[carry]["items"], selected[tank]["items"] = held[carry], held[tank]
            resolved = resolve_board_traits(snap, members, None)
            out.append({
                "opponentId": f"median-{damage}-f{frontline}",
                "key": f"median-{damage}-f{frontline}",
                "label": (f"Median board, {frontline} in front, {damage} backline "
                          f"({snap.units[carry]['name']} and {snap.units[tank]['name']})"),
                "model": MODEL, "geometry": geometry,
                "level": slots, "boardSlots": slots, "unitCount": len(members),
                "slotsUsed": tft_board.slots_used(members), "archetype": [damage, "median"],
                "opponentVersion": MODEL, "costPlan": 0,
                "members": members, "selected": selected, "carry": carry, "tank": tank,
                "effects": resolved["effects"],
                "traits": [trait for trait in resolved["traits"] if trait["count"]],
                "limitations": resolved["limitations"],
                "roster": [{"api": member["api"], "star": member["star"],
                            "name": snap.units[member["api"]]["name"],
                            "items": list(selected[member["api"]]["items"])}
                           for member in members],
                "star": star,
                "purchaseGold": sum(snap.units[member["api"]]["cost"] * (1, 3, 9)[member["star"] - 1]
                                    for member in members),
            })
    return out


def effective_clear_time(result, window=tft_team.DURATION):
    """How long this board needs to clear the enemy in front of it.

    A fight that clears them scores its own duration. One that does not is
    extrapolated the way the survival tier extrapolates a survivor: the whole
    window plus the enemy health still standing over the damage the board
    actually dealt per second. A board that dealt nothing scores an explicit
    infinity rather than a number, so it can never win a geometric mean.
    """
    if result["outcome"] == "win":
        return max(result["duration"], tft.TICK_S)
    left, dps = result["enemyHpLeft"], result["damageDps"]
    if dps <= 0.0:
        return math.inf
    return window + left / dps


def summarize(results, labels, *, details=True):
    """`tft_team.summarize` plus the removal metrics this model ranks on.

    `clearTime` is the geometric mean of the effective clear times, so one
    opponent a board cannot handle is not averaged away by three it can.
    `score` is its reciprocal, keeping the convention that a bigger number is
    a better board. `survivors` is how many enemies are left standing on
    average — what TFT actually charges you for.
    """
    summary = tft_team.summarize(results, labels, details=details)
    times = [effective_clear_time(result) for result in results]
    survivors = [sum(1 for enemy in result["enemies"] if enemy["alive"]) for result in results]
    if any(not math.isfinite(time) for time in times):
        clear, score = math.inf, 0.0
    else:
        clear = math.exp(math.fsum(math.log(time) for time in times) / len(times))
        score = 1.0 / clear
    summary["metrics"].update(
        evaluationModel=MODEL, removalClearTime=clear, removalScore=score,
        boardsCleared=sum(result["outcome"] == "win" for result in results) / len(results),
        survivors=math.fsum(survivors) / len(survivors),
        enemyHpLeft=math.fsum(result["enemyHpFraction"] for result in results) / len(results))
    for matchup, time, left in zip(summary["matchups"], times, survivors, strict=True):
        matchup.update(effectiveClearTime=time, survivors=left)
    return summary


class Evaluator(tft_team.Evaluator):
    """`tft_team.Evaluator` against the synthetic opponents, ranked on removal.

    Everything expensive is inherited: resolved combat specs, automatic
    placement, prepared actors, batched native matches and the allocation
    memo. Only which opponents are fought and what the result is ranked on
    change. One 8v8 fight measured 1.3 ms pinned to a P-core, so the four
    opponents from both initiatives cost about the same as one allocation of
    the model this replaces.
    """

    def __init__(self, snap, geometry, threat="mixed", *, prepared=True, workers=1,
                 score_cache_dir=None, paths=None, builds=None):
        super().__init__(snap, geometry, threat, prepared=prepared, workers=workers,
                         score_cache_dir=score_cache_dir)
        self.paths, self.builds = paths, builds
        self._boards = None
        # The inherited score key mixes in the authored reference pool's
        # revision. This model never reads that pool, so name itself instead;
        # the synthetic enemies are already hashed into the key separately.
        revision = tft.json_hash([MODEL, geometry, tft.snapshot_revision(snap)])
        self._revisions = {budget: revision
                           for budget in range(3 * tft_board.MAX_BOARD_SLOTS + 1)}

    @property
    def boards(self):
        if self._boards is None:
            self._boards = opponent_boards(self.snap, self.geometry, paths=self.paths,
                                           builds=self.builds)
        return self._boards

    def evaluate_many(self, members, effects, allocations, carry, tank, *, split="theory",
                      subset=None, healing_policy="broad", details=False):
        """The composition search's own call, which names its split "theory".

        This model has no search/held-out opponent split to make: the
        reference boards are derived, not authored, so there is nothing to
        hold out from. The name still keys the caches, so a caller that asks
        for a different one gets its own entries.
        """
        if split == "theory":
            split = "search"
        return super().evaluate_many(members, effects, allocations, carry, tank, split=split,
                                     subset=subset, healing_policy=healing_policy, details=details)

    def evaluate(self, members, effects, selected, carry, tank, *, split="theory", **kwargs):
        return self.evaluate_many(members, effects, [selected], carry, tank, split=split,
                                  details=True, **kwargs)[0]

    def encounters(self, budget, split="search", subset=None):
        """The synthetic opponents, each fought from both initiatives.

        `budget` and `split` are the inherited interface: the reference
        boards' own items come from the champion leaderboard, not from a
        shared budget, so neither changes what is fought. They still key the
        cache, so a caller that varies them gets its own entries.
        """
        key = budget, split, subset
        if key not in self._encounters:
            encounters = []
            for board in self.boards:
                enemy_key = board["opponentId"]
                if enemy_key not in self._opponents:
                    self._opponents[enemy_key] = self.allies(
                        board["members"], board["effects"], board["selected"],
                        board["carry"], board["tank"])
                for initiative in tft_team.INITIATIVES:
                    label = {field: board[field] for field in (
                        "label", "opponentId", "opponentVersion", "archetype", "traits",
                        "limitations", "purchaseGold", "level", "boardSlots", "slotsUsed",
                        "unitCount", "roster")}
                    label.update(key=f"{board['opponentId']}-i{initiative}",
                                 initiative=initiative, laneOffset=0,
                                 formation=self.geometry, poolSplit=split)
                    encounters.append({"label": label, "enemies": self._opponents[enemy_key],
                                       "initiative": initiative})
            self._encounters[key] = encounters
        return self._encounters[key]

    def _summary(self, results, encounters, budget, split, subset, healing_policy, details, layout):
        summary = summarize(results, [encounter["label"] for encounter in encounters],
                            details=details)
        for api, contribution in summary["units"].items():
            contribution.update(layout[api])
        summary.update(poolRevision=self._revisions.get(budget, MODEL), poolSplit=split,
                       itemBudget=budget, evaluationModel=MODEL,
                       opponentCount=len({encounter["label"]["opponentId"]
                                          for encounter in encounters}),
                       laneOffsets=[0], subset=subset, healingPolicy=healing_policy)
        return summary
