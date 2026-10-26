"""Item-free stage-two board search, tested against fixed early reference boards.

This uses finite champion fights, independently of the late-game theoretical
composition model and its expensive calculation cache. HTTP only serves the
finished artifact; preparation and the CLI run the bounded native search.
"""

from collections import Counter, OrderedDict
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

from web.db import BASE_DIR
from tft import board as tft_board, sim as tft


SCHEMA = "tft-early-comps-v1"
MODEL = "stage-two-reference-fights-v1"
CACHE_DIR = Path(BASE_DIR) / ".cache" / "tft-early"
STAGE = 2
BEAM_WIDTH = 6
FINALISTS_PER_CARRY = 6
RESULTS_PER_CARRY = 4
WORKERS = min(8, max(1, (os.cpu_count() or 1) // 2))
STAR_ASSUMPTION = "All units at 1★"
VARIANTS = (
    {"key": "level4", "label": "4 units · 1–2 costs", "level": 4, "boardSlots": 4, "maxThreeCosts": 0},
    {"key": "level5", "label": "5 units · at most one 3-cost", "level": 5, "boardSlots": 5, "maxThreeCosts": 1},
)
LIMITATIONS = [
    "Benchmark wins are outcomes against a fixed, equally weighted early reference pool, not a live stage 2 win probability.",
    "Every champion is assumed 1-star, including all supports and reference opponents. All boards and opponents have zero items, augments and prior permanent stacks.",
    "Stage 2 Fighter role attack speed is 5%. Fights last up to 30 seconds; unresolved fights are timeouts and count as zero wins. Overtime bonuses are not modeled.",
    "Front/back placement uses fixed lanes; each reference rotates through three lane offsets and both initiative orders. This does not search optimal hex positioning.",
    "A bounded combat beam search explores supports for each eligible damage champion, then checks single-unit replacements. The listed candidates are not every legal roster or a proven global optimum.",
    "Known engine and trait omissions still apply, including Elderwood plants, Sprykin riders and prior Coven/Blackthorn rewards. Missing effects can change actual board strength; each board exposes the resolver's coverage notes.",
    "Purchase gold buys one copy of each champion. It excludes rolling, leveling and contesting.",
]


def _code_hash():
    return tft.json_hash([Path(__file__).read_text(),
                          Path(tft_board.__file__).read_text()])


SOURCE_HASH = _code_hash()


def source_stale():
    return tft.source_stale() or _code_hash() != SOURCE_HASH


def pool_path(snap):
    return Path(tft.set_dir(snap.set_no)) / "early-opponents.json"


def supports(snap):
    """Do not opt tiny publication fixtures or other sets into this model."""
    return (getattr(snap, "set_no", None) == 18
            and bool(getattr(snap, "units", None)) and len(snap.units) >= 40
            and hasattr(snap, "traits") and pool_path(snap).is_file())


def revision(snap=None):
    snap = snap or tft.load_snapshot()
    return tft.json_hash([SOURCE_HASH, tft.snapshot_revision(snap),
                          hashlib.sha256(pool_path(snap).read_bytes()).hexdigest()])


def star_for(unit):
    return 1


def legal_roster(snap, roster, level, max_three=None, *, complete=True):
    if level not in (4, 5) or max_three not in (None, 0, 1):
        return False
    roster = tuple(roster)
    if (not roster or any(not isinstance(api, str) for api in roster)
            or len(set(roster)) != len(roster) or len(roster) > level
            or any(api not in snap.units for api in roster)):
        return False
    costs = Counter(snap.units[api]["cost"] for api in roster)
    allowed_three = int(level == 5) if max_three is None else min(max_three, int(level == 5))
    return (all(cost in (1, 2, 3) for cost in costs) and costs[3] <= allowed_three
            and (not complete or len(roster) == level)
            and any(snap.units[api]["objective"] in ("tank", "fighter") for api in roster))


def members_for(snap, roster):
    return [{"api": api, "star": star_for(snap.units[api])} for api in sorted(roster)]


def finite_traits(snap, members, alpha=None, *, primal_blessings=None):
    """Keep shared effects while replacing notes specific to immortal probes."""
    resolved = tft_board.resolve_board_traits(snap, members, alpha, primal_blessings=primal_blessings)
    probe_only = {"DA_18_Hunter", "DA_18_Inferno", "DA_18_Slayer", "DA_FloraFatalis18", tft_board.RIFTBEAST}
    if not any(member["api"] == "TFT18_Zyra" for member in members):
        probe_only.add("DA_18_Summoner")
    removed = {f"{snap.traits[api]['name']}: {note}" for api in probe_only
               for note in tft_board._THEORY_COVERAGE_NOTES.get(api, ())}
    removed.update(note for note in resolved["limitations"]
                   if note.startswith("Primal: Executes work in finite combat"))
    resolved["limitations"] = [note for note in resolved["limitations"] if note not in removed]
    for trait in resolved["traits"]:
        trait["coverageNotes"] = [note for note in trait["coverageNotes"]
                                  if f"{trait['name']}: {note}" not in removed]
        if trait["api"] == tft_board.RIFTBEAST and alpha == "TFT18_Scuttlecrab":
            note = "Scuttlecrab's Alpha team healing is not modeled"
            trait["coverageNotes"].append(note)
            resolved["limitations"].append(f"{trait['name']}: {note}")
        if trait["coverage"] == "partial" and not trait["coverageNotes"]:
            trait["coverage"] = "supported"
    return resolved


def load_pool(snap):
    data = json.loads(pool_path(snap).read_bytes())
    if (data.get("schema") != "tft-early-reference-v1"
            or data.get("laneOffsets") != [0, 2, 4]
            or not isinstance(data.get("boards"), list) or len(data["boards"]) < 6):
        raise ValueError("invalid early composition reference pool")
    by_name = {unit["name"]: api for api, unit in snap.units.items()}
    seen, boards = set(), []
    for row in data["boards"]:
        try:
            roster = tuple(sorted(by_name[name] for name in row["units"]))
            fifth = by_name[row["fifth"]]
            carry, tank = by_name[row["carry"]], by_name[row["tank"]]
            alpha = by_name[row["alphaHolder"]] if row.get("alphaHolder") else None
        except (KeyError, TypeError) as error:
            raise ValueError("unknown unit in early reference pool") from error
        if (not isinstance(row.get("id"), str) or row["id"] in seen
                or not isinstance(row.get("label"), str) or type(row.get("screen")) is not bool
                or not legal_roster(snap, roster, 4) or not legal_roster(snap, (*roster, fifth), 5)
                or carry == tank or carry not in roster or tank not in roster
                or snap.units[carry]["objective"] == "tank"
                or snap.units[tank]["objective"] not in ("tank", "fighter")):
            raise ValueError("illegal early reference board")
        for size in (4, 5):
            tft_board.resolve_board_traits(snap, members_for(snap, roster if size == 4 else (*roster, fifth)), alpha)
        seen.add(row["id"])
        boards.append(dict(row, roster=roster, fifthApi=fifth, carryApi=carry, tankApi=tank, alphaApi=alpha))
    if not 2 <= sum(row["screen"] for row in boards) < len(boards):
        raise ValueError("early screening needs a smaller declared reference subset")
    return dict(data, boards=boards)


class Combat(tft_board.Evaluator):
    """Reuse native actor construction, with local stage-two role inputs."""

    def __init__(self, snap, level, *, workers=WORKERS):
        if level not in (4, 5):
            raise ValueError("early board size must be four or five")
        if type(workers) is not int or not 1 <= workers <= 8:
            raise ValueError("early match workers must be between one and eight")
        self.snap, self.geometry, self.prepared, self.workers = snap, "clump", True, workers
        self.item_fx, self.templates, self.items = {}, {}, {}
        self._forms, self._prepared_actors = OrderedDict(), OrderedDict()
        self.stats = Counter()
        self.level = level
        self.references = load_pool(snap)
        self._early_encounters, self._scores, self._resolved = {}, {}, {}
        self._reference_boards = []
        for row in self.references["boards"]:
            roster = row["roster"] if level == 4 else tuple(sorted((*row["roster"], row["fifthApi"])))
            members = members_for(snap, roster)
            resolved = finite_traits(snap, members, row["alphaApi"])
            selected = {api: {"items": (), "alpha": api == row["alphaApi"]} for api in roster}
            actors = self.allies(members, resolved["effects"], selected, row["carryApi"], row["tankApi"])
            public = {"id": row["id"], "label": row["label"], "screen": row["screen"],
                      "limitations": resolved["limitations"], "alphaHolder": row["alphaApi"],
                      "traits": [trait for trait in resolved["traits"] if trait["active"]],
                      "units": [self.unit_row(member, actor) for member, actor in
                                ((member, next(actor for actor in actors if actor["spec"]["unit"]["api"] == member["api"]))
                                 for member in members)]}
            self._reference_boards.append(public)
            for offset in self.references["laneOffsets"]:
                shifted = [dict(actor, lane=(actor["lane"] + offset) % 7) for actor in actors]
                prepared = [tft.engine().prepare_actor(actor) for actor in shifted]
                for initiative in tft_board.INITIATIVES:
                    label = {"key": f"{row['id']}-p{offset}-i{initiative}", "opponentId": row["id"],
                             "label": row["label"], "initiative": initiative, "laneOffset": offset,
                             "roster": [dict(unit, lane=(unit["lane"] + offset) % 7) for unit in public["units"]]}
                    encounter = {"enemies": prepared, "initiative": initiative, "label": label}
                    self._early_encounters.setdefault("full", []).append(encounter)
                    if row["screen"] and offset == 0:
                        self._early_encounters.setdefault("screen", []).append(encounter)

    def spec(self, api, star, effects, items, alpha=False):
        if type(star) is not int or star != 1:
            raise ValueError("early comparisons require every champion at one star")
        if items:
            raise ValueError("early comparisons are item-free")
        result = super().spec(api, star, effects, items, alpha)
        result["role"] = dict(result["role"], asPct=tft.FIGHTER_AS_BY_STAGE[STAGE]
                              if result["unit"]["kind"] == "Fighter" else 0.0)
        return result

    def unit_row(self, member, actor=None):
        unit = self.snap.units[member["api"]]
        row = {"api": member["api"], "slug": tft.unit_slug(unit), "name": unit["name"],
               "cost": unit["cost"], "star": member["star"], "role": unit["roleName"],
               "objective": unit["objective"], "kind": unit["kind"], "traitApis": list(unit["traitApis"])}
        if actor is not None:
            row.update({field: actor[field] for field in ("frontline", "lane", "priority")})
            row.update(kind=actor["spec"]["unit"]["kind"], form=actor["spec"]["unit"].get("form"))
        return row

    def pool_metadata(self):
        return {"opponentCount": len(self._reference_boards), "encounterCount": len(self._early_encounters["full"]),
                "laneOffsets": list(self.references["laneOffsets"]), "initiatives": list(tft_board.INITIATIVES),
                "description": self.references["description"], "boards": self._reference_boards}

    def configs(self, roster, carry, tank):
        key = tuple(sorted(roster)), carry, tank
        if key not in self._resolved:
            members = members_for(self.snap, roster)
            counts = Counter(trait for api in roster for trait in self.snap.units[api]["traitApis"])
            alpha_options = [None]
            if counts[tft_board.RIFTBEAST] >= min(self.snap.traits[tft_board.RIFTBEAST]["levels"]):
                alpha_options = [api for api in sorted(roster) if tft_board.RIFTBEAST in self.snap.units[api]["traitApis"]]
            choices = []
            for alpha in alpha_options:
                for blessings in tft_board.primal_options(self.snap, members):
                    resolved = finite_traits(self.snap, members, alpha, primal_blessings=blessings)
                    selected = {api: {"items": (), "alpha": api == alpha} for api in roster}
                    actors = self.allies(members, resolved["effects"], selected, carry, tank, prepared=True)
                    choices.append({"members": members, "resolved": resolved, "selected": selected,
                                    "actors": actors, "alpha": alpha, "blessings": list(blessings)})
            self._resolved[key] = choices
        return self._resolved[key]

    def score_many(self, candidates, *, subset="screen", details=False):
        if subset not in self._early_encounters:
            raise ValueError("unknown early reference subset")
        encounters = self._early_encounters[subset]
        candidates = list(candidates)
        answers, todo = {}, []
        for roster, carry, tank in candidates:
            if (not legal_roster(self.snap, roster, self.level, complete=False)
                    or carry == tank or carry not in roster or tank not in roster
                    or self.snap.units[carry]["objective"] == "tank"
                    or self.snap.units[tank]["objective"] not in ("tank", "fighter")):
                raise ValueError("illegal early combat candidate or anchors")
            key = tuple(sorted(roster)), carry, tank, subset, details
            if key in answers:
                continue
            if key in self._scores:
                answers[key] = self._scores[key]
            else:
                todo.append(key)
        for start in range(0, len(todo), 64):
            requests, parts = [], []
            for key in todo[start:start + 64]:
                roster, carry, tank = key[:3]
                for config in self.configs(roster, carry, tank):
                    parts.append((key, config, len(requests)))
                    requests.extend({"duration": tft_board.DURATION, "geometry": self.geometry,
                                     "allies": config["actors"], "enemies": encounter["enemies"],
                                     "initiative": encounter["initiative"], "burnWound": 0.33,
                                     "damageHealingFromProcs": True, "postDeathAllyHealing": True}
                                    for encounter in encounters)
            results = tft.engine().simulate_matches(requests, detail="full" if details else "compact",
                                                     trace=False, workers=self.workers)
            if len(results) != len(requests):
                raise RuntimeError("incomplete early fight batch")
            self.stats["fightsSimulated"] += len(results)
            for key, config, offset in parts:
                summary = tft_board.summarize(results[offset:offset + len(encounters)],
                                              [encounter["label"] for encounter in encounters], details=details)
                row = dict(summary, config=config)
                if key not in answers or search_key(row, key[:3]) < search_key(answers[key], key[:3]):
                    answers[key] = row
            for key in todo[start:start + 64]:
                self._scores[key] = answers[key]
        return [answers[(tuple(sorted(roster)), carry, tank, subset, details)] for roster, carry, tank in candidates]


def search_key(result, candidate):
    metrics = result["metrics"]
    return (-metrics["benchmarkWinRate"], -metrics["hpMargin"], -metrics["damageDps"], candidate)


def board_payload(combat, candidate, result):
    """Publish one full native result with its actual units and trait choices."""
    snap, level = combat.snap, combat.level
    roster, carry, tank = candidate
    config = result["config"]
    actors = combat.allies(config["members"], config["resolved"]["effects"], config["selected"], carry, tank)
    by_api = {actor["spec"]["unit"]["api"]: actor for actor in actors}
    unit_icons, trait_icons = tft.ui_icons(snap)
    units = [dict(combat.unit_row(member, by_api[member["api"]]), icon=unit_icons.get(member["api"]),
                  **result["units"][member["api"]]) for member in config["members"]]
    damage_dealer = min((unit for unit in units if unit["objective"] != "tank"),
                        key=lambda unit: (-unit["dps"], unit["api"] != carry, unit["api"]))
    traits = [dict(trait, icon=trait_icons.get(trait["api"]), levels=list(snap.traits[trait["api"]]["levels"]))
              for trait in config["resolved"]["traits"]]
    return {"id": f"early-{level}-" + tft.json_hash([roster, carry, tank, config["alpha"], config["blessings"]])[:16],
            "level": level, "boardSlots": level, "unitCount": level, "slotsUsed": level,
            "mainCarry": damage_dealer["slug"], "mainTank": tft.unit_slug(snap.units[tank]),
            "damageAnchor": tft.unit_slug(snap.units[carry]),
            "purchaseGold": sum(snap.units[api]["cost"] for api in roster),
            "itemCount": 0, "threeCostCount": sum(snap.units[api]["cost"] == 3 for api in roster),
            "activeTraitCount": sum(trait["active"] for trait in traits), "units": units, "traits": traits,
            "metrics": result["metrics"], "matchups": result["matchups"],
            "alphaHolder": tft.unit_slug(snap.units[config["alpha"]]) if config["alpha"] else None,
            "primalBlessings": config["blessings"], "limitations": config["resolved"]["limitations"]}


def search(snap, level, *, workers=WORKERS, log=print):
    combat = Combat(snap, level, workers=workers)
    pool = sorted(api for api, unit in snap.units.items() if unit["cost"] <= (3 if level == 5 else 2))
    carries = [api for api in pool if snap.units[api]["objective"] != "tank"]
    fronts = [api for api in pool if snap.units[api]["objective"] in ("tank", "fighter")]
    finalists, scopes = set(), {}
    for max_three in ((0, 1) if level == 5 else (0,)):
        for ordinal, carry in enumerate(carries, 1):
            seeds = [(tuple(sorted((carry, tank))), carry, tank) for tank in fronts if tank != carry
                     and legal_roster(snap, (carry, tank), level, max_three, complete=False)]
            if not seeds:
                continue
            scores = combat.score_many(seeds)
            beam = [candidate for _, candidate in sorted(zip(scores, seeds), key=lambda pair: search_key(*pair))[:BEAM_WIDTH]]
            while len(beam[0][0]) < level:
                expanded = set()
                for roster, _, tank in beam:
                    for api in pool:
                        if api not in roster and legal_roster(snap, (*roster, api), level, max_three, complete=False):
                            expanded.add((tuple(sorted((*roster, api))), carry, tank))
                choices = sorted(expanded)
                scores = combat.score_many(choices)
                beam = [candidate for _, candidate in sorted(zip(scores, choices), key=lambda pair: search_key(*pair))[:BEAM_WIDTH]]
            finalists.update(beam[:FINALISTS_PER_CARRY])
            scopes[carry, max_three] = beam[:FINALISTS_PER_CARRY]
            if log and (ordinal % 4 == 0 or ordinal == len(carries)):
                log(f"Early level {level}, ≤{max_three} 3-cost: {ordinal}/{len(carries)} damage anchors screened")

    candidates = sorted(finalists)
    results = combat.score_many(candidates, subset="full")
    full = dict(zip(candidates, results))
    replacements = set()
    for (carry, max_three), scope in scopes.items():
        winner = min(scope, key=lambda candidate: search_key(full[candidate], candidate))
        roster, _, tank = winner
        for outgoing in roster:
            if outgoing in (carry, tank):
                continue
            for incoming in pool:
                changed = tuple(sorted((*[api for api in roster if api != outgoing], incoming)))
                if incoming not in roster and legal_roster(snap, changed, level, max_three):
                    replacements.add((changed, carry, tank))
    changed = sorted(replacements - finalists)
    full.update(zip(changed, combat.score_many(changed, subset="full")))
    candidates = sorted(full)
    chosen = set()
    for carry, max_three in scopes:
        eligible = [candidate for candidate in candidates if candidate[1] == carry
                    and legal_roster(snap, candidate[0], level, max_three)]
        chosen.update(sorted(eligible, key=lambda candidate: search_key(full[candidate], candidate))[:RESULTS_PER_CARRY])
    by_roster = {}
    for candidate in sorted(chosen):
        roster = candidate[0]
        if roster not in by_roster or search_key(full[candidate], candidate) < search_key(full[by_roster[roster]], by_roster[roster]):
            by_roster[roster] = candidate
    selected = sorted(by_roster.values(), key=lambda candidate: search_key(full[candidate], candidate))
    results = combat.score_many(selected, subset="full", details=True)
    boards = [board_payload(combat, candidate, result) for candidate, result in zip(selected, results)]
    boards.sort(key=lambda board: (-board["metrics"]["benchmarkWinRate"], board["purchaseGold"], board["id"]))
    previous, rank = None, 0
    for index, board in enumerate(boards, 1):
        rate = board["metrics"]["benchmarkWinRate"]
        if rate != previous:
            rank = index
        board["rank"] = rank
        previous = rate
    return {**next(variant for variant in VARIANTS if variant["level"] == level), "boards": boards,
            "searchStats": {"candidateConfigurations": len(combat._scores), "fullPoolCandidates": len(full),
                            "rostersShown": len(boards), "fightsSimulated": combat.stats["fightsSimulated"]}}, combat.pool_metadata()


def cache_path(snap):
    return Path(CACHE_DIR) / f"early-{snap.set_no}-{snap.patch}-{revision(snap)[:20]}.json"


def validate_catalog(snap, result):
    if (not isinstance(result, dict) or result.get("schema") != SCHEMA or result.get("complete") is not True
            or result.get("revision") != revision(snap) or result.get("baselineRevision") != tft.snapshot_revision(snap)
            or result.get("set") != snap.set_no or result.get("patch") != snap.patch
            or result.get("stage") != 2 or result.get("starAssumption") != STAR_ASSUMPTION
            or result.get("evaluationModel") != MODEL
            or result.get("itemCount") != 0 or not isinstance(result.get("variants"), list)
            or [variant.get("key") for variant in result["variants"]] != [variant["key"] for variant in VARIANTS]):
        raise ValueError("early composition cache is incomplete or does not match this snapshot")
    authored = load_pool(snap)["boards"]
    for variant, expected in zip(result["variants"], VARIANTS):
        if any(variant.get(field) != value for field, value in expected.items()):
            raise ValueError("invalid early composition variant")
        pool = result.get("referencePools", {}).get(variant["key"], {})
        boards = variant.get("boards")
        if (not isinstance(boards, list) or not boards or not isinstance(pool.get("boards"), list)
                or len(pool["boards"]) != pool.get("opponentCount")
                or pool.get("laneOffsets") != [0, 2, 4] or pool.get("initiatives") != [0, 1]
                or pool.get("encounterCount") != pool["opponentCount"] * 6):
            raise ValueError("early composition cache is missing reference evidence")
        if [row.get("id") for row in pool["boards"]] != [row["id"] for row in authored]:
            raise ValueError("early reference pool differs from its declared rosters")
        reference_units = {}
        for reference, declared in zip(pool["boards"], authored):
            wanted = declared["roster"] if variant["level"] == 4 else (*declared["roster"], declared["fifthApi"])
            units = reference.get("units", [])
            if (not isinstance(units, list) or sorted(unit.get("api", "") for unit in units) != sorted(wanted)
                    or any(unit.get("star") != star_for(snap.units[unit["api"]])
                           or unit.get("cost") != snap.units[unit["api"]]["cost"] for unit in units)):
                raise ValueError("early reference unit costs or upgrades differ from the declared pool")
            reference_units[reference["id"]] = {unit["api"]: unit for unit in units}
        encounter_keys = {f"{row['id']}-p{offset}-i{initiative}"
                          for row in authored for offset in pool["laneOffsets"] for initiative in pool["initiatives"]}
        signatures, ids = set(), set()
        previous_rate, expected_rank = None, 0
        for board in boards:
            units, metrics, matches = board.get("units", []), board.get("metrics", {}), board.get("matchups", [])
            roster = tuple(unit.get("api") for unit in units)
            if not legal_roster(snap, roster, variant["level"]):
                raise ValueError("illegal early composition roster")
            signature = tuple(sorted(roster))
            if (signature in signatures
                    or board.get("id") in ids or not isinstance(board.get("id"), str)
                    or any(type(unit.get("star")) is not int or unit["star"] != star_for(snap.units[unit["api"]])
                           or type(unit.get("cost")) is not int or unit["cost"] != snap.units[unit["api"]]["cost"] for unit in units)
                    or any(board.get(field) != variant["level"] for field in ("level", "boardSlots", "unitCount", "slotsUsed"))
                    or board.get("itemCount") != 0
                    or board.get("threeCostCount") != sum(snap.units[api]["cost"] == 3 for api in roster)
                    or board.get("purchaseGold") != sum(unit["cost"] for unit in units)
                    or board.get("mainCarry") not in {unit.get("slug") for unit in units}
                    or board.get("mainTank") not in {unit.get("slug") for unit in units}
                    or not isinstance(matches, list) or len(matches) != pool["encounterCount"]
                    or metrics.get("benchmarkCount") != len(matches)):
                raise ValueError("illegal or incomplete early composition board")
            counts = Counter(match.get("outcome") for match in matches)
            if (any(outcome not in ("win", "loss", "draw", "timeout") for outcome in counts)
                    or metrics.get("benchmarkWins") != counts["win"]
                    or metrics.get("benchmarkLosses") != counts["loss"]
                    or metrics.get("benchmarkDraws") != counts["draw"]
                    or metrics.get("benchmarkTimeouts") != counts["timeout"]
                    or metrics.get("benchmarkWinRate") != counts["win"] / len(matches)
                    or metrics.get("benchmarkScore") != 100 * counts["win"] / len(matches)
                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in metrics.values())):
                raise ValueError("early benchmark metrics disagree with their fight evidence")
            if ({match.get("key") for match in matches} != encounter_keys
                    or len({match.get("key") for match in matches}) != len(matches)):
                raise ValueError("early fight evidence duplicates or omits a declared encounter")
            for match in matches:
                opponent, offset, initiative = match.get("opponentId"), match.get("laneOffset"), match.get("initiative")
                if (opponent not in reference_units or offset not in pool["laneOffsets"] or initiative not in pool["initiatives"]
                        or match["key"] != f"{opponent}-p{offset}-i{initiative}"
                        or not isinstance(match.get("roster"), list)
                        or {unit.get("api") for unit in match["roster"]} != set(reference_units[opponent])
                        or len(match["roster"]) != variant["level"]):
                    raise ValueError("early fight evidence does not match its declared opponent")
                for unit in match["roster"]:
                    reference = reference_units[opponent][unit["api"]]
                    if (unit.get("star") != reference["star"] or unit.get("frontline") != reference["frontline"]
                            or unit.get("lane") != (reference["lane"] + offset) % 7):
                        raise ValueError("early fight evidence changes the declared opponent's upgrade or position")
            rate = metrics["benchmarkWinRate"]
            if previous_rate is not None and rate > previous_rate:
                raise ValueError("early composition ranking is not ordered by benchmark wins")
            if rate != previous_rate:
                expected_rank = len(ids) + 1
            if board.get("rank") != expected_rank:
                raise ValueError("equal early benchmark win rates must share a rank")
            previous_rate = rate
            signatures.add(signature)
            ids.add(board["id"])
    return result


def cached_catalog(snap=None):
    snap = snap or tft.load_snapshot()
    try:
        result = json.loads(cache_path(snap).read_bytes())
    except FileNotFoundError as error:
        raise ValueError("early compositions are not prepared for this snapshot") from error
    return validate_catalog(snap, result)


def warm(*, snap=None, log=print, workers=WORKERS):
    """Complete a separate early artifact, or validate and reuse its cache."""
    snap = snap or tft.load_snapshot()
    if not supports(snap):
        raise ValueError("no early composition reference pool for this snapshot")
    if source_stale():
        raise RuntimeError("early composition source changed; restart preparation")
    try:
        return cached_catalog(snap)
    except ValueError:
        pass
    started = time.monotonic()
    stamp = revision(snap)
    variants, pools = [], {}
    for variant in VARIANTS:
        row, pool = search(snap, variant["level"], workers=workers, log=log)
        variants.append(row)
        pools[variant["key"]] = pool
    result = {"schema": SCHEMA, "revision": stamp, "baselineRevision": tft.snapshot_revision(snap),
              "complete": True, "set": snap.set_no, "patch": snap.patch, "stage": STAGE,
              "evaluationModel": MODEL, "starAssumption": STAR_ASSUMPTION, "itemCount": 0,
              "variants": variants, "referencePools": pools, "limitations": list(LIMITATIONS),
              "search": {"description": "Combat beam search for each damage anchor, followed by single support replacements; all found finalists are browsable.",
                         "beamWidth": BEAM_WIDTH, "resultsPerCarry": RESULTS_PER_CARRY,
                         "exhaustive": False, "elapsedSeconds": round(time.monotonic() - started, 3)}}
    if source_stale() or revision(snap) != stamp:
        raise RuntimeError("early composition inputs changed during calculation")
    validate_catalog(snap, result)
    path = cache_path(snap)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, separators=(",", ":"), allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", type=int, default=18)
    parser.add_argument("--patch")
    parser.add_argument("--workers", type=int, default=WORKERS, choices=range(1, 9))
    args = parser.parse_args()
    snapshot = tft.load_snapshot(args.set, args.patch)
    catalog = warm(snap=snapshot, workers=args.workers)
    print(json.dumps({"patch": catalog["patch"], "boards": {row["key"]: len(row["boards"]) for row in catalog["variants"]},
                      "cache": str(cache_path(snapshot))}))
