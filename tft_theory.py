"""Theoretical composition capacity from EHP buying damage uptime.

No champion opponent boards, wins, or fixed fight deadline enter this score.
A shared pressure budget measures the frontline through an opening-EHP /
pressure workload, ending early on frontline collapse. Spent raw damage plus
remaining EHP values usable self sustain; EHP times measured DPS is still a
first-order capacity estimate. It is not a full team fight.
"""
from collections import Counter, OrderedDict
from itertools import product
import math
from pathlib import Path

import tft
from tft_board import MAX_BOARD_SLOTS, slots_used
from tft_comp_traits import PRIMAL, primal_options, with_primal_effects
from tft_unit_profiles import UnitProfiles

MODEL = "ehp-damage-capacity-v3"
THREATS = {"mixed": "Theoretical pressure range"}
PRESSURES = (1000.0, 2000.0)
PHYSICAL_SHARES = (0.0, 0.5, 1.0)
WOUNDS = (0.0, 0.33)
CONTROL_DURATIONS = (0.0, 1.5)
CONTROL_INTERVAL = 8.0
TARGETING = ("main-first", "secondary-first")
TARGET_COUNT = 3
MAX_MEASUREMENT_TIME = 2047.0  # explicit supported workload range, never a capped score
RESULT_CACHE_LIMIT = 2048

LIMITATIONS = [
    "Scores are theoretical EHP × DPS capacity estimates, not live win rates or outcomes against chosen champion boards.",
    "The planned measurement window is opening frontline EHP divided by raw incoming DPS; measurement ends early if the frontline collapses. There is no fixed 30-second win/loss cutoff.",
    "Three incoming sources keep their assigned frontline target until death or untargetability, including while the source is stunned. Their shared raw budget arrives in staggered half-second pulses, including a proportional final pulse; lethal excess follows the same source to its next target.",
    "Both focus orientations have equal weight: two sources start on the first eligible frontliner and one on the second, or all three on the only eligible frontliner. The main tank leads a fixed priority ordered by unfocused opening mixed EHP; the other orientation swaps its first two entries. Remaining frontliners wait for retargeting. This is a formation sensitivity assumption, not hex positioning or measured enemy target selection.",
    "Each pressure profile is tested both without enemy control and with a 1.5-second frontline stun every 8 seconds. Immunity is honored; these are equally weighted sensitivity endpoints, not measured lobby frequencies. Walking and target access are not simulated.",
    "Adjusted EHP is observed raw pressure spent (excluding overkill), observed denied damage, and remaining health/shield/body EHP. Effective self healing and consumed shields are already included and are not added twice.",
    "If the frontline survives the workload, estimated protection and damage capacity extend measured average throughput using remaining EHP. Temporary defenses and future ramping remain approximate. All protected damage stops at observed frontline collapse.",
    "Ally healing and shielding are reported as potential output and are not credited as team EHP without a recipient-utilization model.",
    "A timed Sunder/Shred (Caustic, Last Whisper, Void Staff, an ability's own) lands on the target its holder hits and lasts its duration there; allies hitting that same generic target share it while it runs. Only an aura (Evenshroud, Ionic Spark) is standing coverage. Only one provider per nonstacking burn channel is credited; burn provider timing and replacement after death remain approximations.",
    "Both layouts contain three immortal generic targets with fixed health and base defenses. Spread/clump changes adjacency coverage, not enemy population; spells selecting independent nearest enemies can reach multiple targets in either layout. Takedown/execute bonuses and hex movement are not modeled.",
    "The displayed pressure, damage mixes, target defenses and antiheal are explicit sensitivity assumptions, not estimated opponent frequencies.",
]


def code_hash():
    return tft.json_hash([Path(__file__).read_text(),
                          Path(__file__).with_name("tft_unit_profiles.py").read_text(),
                          Path(__file__).with_name("tft_comp_traits.py").read_text()])


MODEL_HASH = code_hash()


def revision(snap=None):
    return tft.json_hash([MODEL_HASH, tft.snapshot_revision(snap)])


def scenarios(geometry=None):
    if geometry is not None and geometry not in tft.GEOMETRIES:
        raise ValueError("unknown theoretical target geometry")
    return [{"key": f"p{int(pressure)}-physical{int(share * 100)}-wound{int(wound * 100)}-control{control:g}-{targeting}",
             "label": f"{int(pressure):,} raw DPS · {int(share * 100)}% physical · {int(wound * 100)}% antiheal · "
                      + (f"{control:g}s frontline stun every {CONTROL_INTERVAL:g}s" if control else "no enemy control")
                      + (" · main tank focus" if targeting == "main-first" else " · secondary frontline focus"),
             "incomingDps": pressure, "physicalShare": share, "armor": 100.0, "mr": 100.0,
             "targetHp": 3000.0, "wound": wound,
             "controlInterval": CONTROL_INTERVAL, "controlDuration": control,
             "pressureInterval": 0.5, "pressureAllocation": "persistent-source-targets", "incomingSourceCount": 3,
             "targeting": targeting,
             **({"targetCount": TARGET_COUNT} if geometry else {})}
            for pressure, share, wound, control, targeting in product(
                PRESSURES, PHYSICAL_SHARES, WOUNDS, CONTROL_DURATIONS, TARGETING)]


def metadata(snap=None, geometry=None):
    stamp = revision(snap)
    assumptions = scenarios(geometry)
    return {"evaluationModel": MODEL, "modelRevision": stamp, "scenarios": assumptions,
            "methodology": {
                "evaluationModel": MODEL, "modelRevision": stamp,
                "ranking": "Geometric mean of frontline EHP × team DPS across the declared pressure assumptions.",
                "aggregation": "Geometric mean, with any zero input giving zero; each declared profile has equal weight.",
                "theoryScore": "frontlineEhp × damageDps; geometric mean across profiles",
                "damageCapacity": "Estimated protected damage = frontlineEhp × measured DPS / raw incoming DPS.",
                "protectionTime": "Estimated frontline protection = adjusted frontline EHP / raw incoming DPS.",
                "measurementWindow": "Opening frontline EHP / raw incoming DPS, ending early on frontline collapse; champion state advances on one shared event clock.",
                "targeting": "Persistent source targets in two equally weighted focus orientations. Opening EHP and live per-attacker defenses use the same assigned source counts; the expanded profiles identify initial targets and retargeting priority.",
                "frontlineEhp": "Raw incoming pressure actually spent, excluding lethal overkill, plus observed denial and remaining effective health/shields/bodies.",
                "damageDps": "Damage per observed protection second. Dead units stop acting and all protected output stops when the frontline collapses.",
                "unitDps": "Contributions to aggregate DPS, allocated by each unit's mean share of measured DPS across profiles.",
                "scenarios": assumptions, "limitations": list(LIMITATIONS)}}


def geometric_mean(values):
    values = list(values)
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("capacity metrics require finite nonnegative values")
    if any(value == 0 for value in values):
        return 0.0
    return math.exp(math.fsum(math.log(value) for value in values) / len(values))


def rank_key(result):
    score = result.get("metrics", result)["theoryScore"]
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0:
        raise ValueError("theoretical capacity score must be finite and nonnegative")
    return (-score,)


def capacity_metrics(frontline_ehp, damage_dps, pressure):
    if (not all(math.isfinite(value) and value >= 0 for value in (frontline_ehp, damage_dps))
            or not math.isfinite(pressure) or pressure <= 0):
        raise ValueError("capacity needs nonnegative EHP/DPS and positive raw pressure")
    score = frontline_ehp * damage_dps
    if not math.isfinite(score):
        raise ValueError("capacity score overflowed")
    return {"frontlineEhp": frontline_ehp, "damageDps": damage_dps,
            "protectionTime": frontline_ehp / pressure, "damageCapacity": score / pressure,
            "theoryScore": score}


def summarize(rows):
    if not rows:
        raise ValueError("capacity evaluation needs pressure profiles")
    metrics = {field: geometric_mean(row[field] for row in rows) for field in
               ("frontlineEhp", "damageDps", "damageCapacity", "protectionTime")}
    # Same mathematical mean as geometric_mean(row['score']), with the
    # factorization preserved in the serialized numbers.
    metrics["theoryScore"] = metrics["frontlineEhp"] * metrics["damageDps"]
    return metrics


def validate_pressure_targets(rows, frontline, tank):
    """Check published assignment structure; native opening checks eligibility."""
    frontline = set(frontline)
    priority = None
    for row in rows:
        order, targets = row.get("pressureTargetOrder"), row.get("initialPressureTargets")
        if (not isinstance(order, list) or not order
                or any(not isinstance(api, str) for api in order)
                or len(order) != len(frontline) or set(order) != frontline):
            raise ValueError("pressure target order must contain every actual frontliner exactly once")
        if (not isinstance(targets, list) or len(targets) != 3
                or any(api is not None and (not isinstance(api, str) or api not in frontline) for api in targets)):
            raise ValueError("initial pressure targets must identify three valid source owners")
        if any(api is None for api in targets):
            if not all(api is None for api in targets):
                raise ValueError("initial pressure sources cannot be partially unassigned")
        elif (targets[0] != targets[2]
                or targets[0] != targets[1] and order.index(targets[0]) >= order.index(targets[1])):
            raise ValueError("initial pressure sources must follow the declared two-to-one priority")
        neutral = list(order)
        if row.get("targeting") == "secondary-first" and len(neutral) > 1:
            neutral[0], neutral[1] = neutral[1], neutral[0]
        elif row.get("targeting") not in TARGETING:
            raise ValueError("unknown theoretical targeting orientation")
        if tank in frontline and neutral[0] != tank:
            raise ValueError("main tank must lead the neutral pressure priority")
        if priority is not None and neutral != priority:
            raise ValueError("pressure profiles must share one neutral formation priority")
        priority = neutral


class ReferenceEvaluator:
    """Readable Python oracle for parity checks and analytic test profiles.

    Production searches use the native Evaluator below. Keep this reference
    independent of native aggregation so that comparisons catch port errors.
    """
    def __init__(self, snap, geometry, threat="mixed", *, score_cache_dir=None, profiles=None,
                 optimize_primal=False):
        if geometry not in tft.GEOMETRIES or threat != "mixed":
            raise ValueError("unknown theoretical composition context")
        self.snap, self.geometry = snap, geometry
        self.model_revision = revision(snap)
        self.scenarios = scenarios(geometry)
        self.profiles = profiles if profiles is not None else UnitProfiles(snap, geometry)
        self.results, self.openings, self.provider_inputs = OrderedDict(), {}, {}
        self.stats = Counter()
        if type(optimize_primal) is not bool:
            raise ValueError("Primal optimization must be explicitly enabled or disabled")
        self.optimize_primal = optimize_primal
        self.primal_variants = OrderedDict()

    def _identity(self, member, effects, selected):
        api = member["api"]
        return api, member["star"], tft.json_hash(effects[api]), tuple(sorted(selected[api]["items"])), bool(selected[api].get("alpha"))

    def _providers(self, members, effects, selected, carry, tank):
        providers, sunder, shred = {}, 0.0, 0.0
        for member in members:
            api, star, _, items, alpha = key = self._identity(member, effects, selected)
            if key not in self.provider_inputs:
                spec = self.profiles.spec(api, star, effects[api], items, alpha=alpha)
                # Form-capable items keep their aura reach until the equipped
                # form is known. UnitProfiles supplies its resolved range;
                # independently gate each provider before assigning team credit.
                item_effects = []
                for original in spec["items"]:
                    effect = original
                    for field in ("sunderAura", "shredAura", "burnAura"):
                        conditional = original.get(field + "ByRange")
                        if conditional and spec["unit"]["range"] <= conditional[-1]:
                            if effect is original:
                                effect = dict(original)
                            effect[field] = conditional[:2] if field == "burnAura" else conditional[0]
                    item_effects.append(effect)
                all_effects = item_effects + spec["traits"]
                regular = max([0.0] + [effect[field][0] for effect in item_effects
                              for field in ("burnOnHit", "burnAura") if effect.get(field)])
                if api == "TFT18_Cinderling" or api == "TFT18_Brambleback" and alpha:
                    regular = max(regular, spec["kits"]["base"]["rows"].get("BurnAmount", 0.0) / 100.0)
                inferno = max([0.0] + [effect["burnOnHit"][0] for effect in spec["traits"]
                                      if effect.get("api") == "DA_18_Inferno" and effect.get("burnOnHit")])
                # Only an aura is standing coverage. Timed on-hit reductions
                # (Caustic, Last Whisper, Void Staff) are applied per target
                # inside the shared native measurement, as in theory.rs.
                reductions = {name: max([0.0] + [effect.get(name + "Aura", 0.0) for effect in all_effects])
                              for name in ("sunder", "shred")}
                self.provider_inputs[key] = regular, inferno, reductions
            regular, inferno, reductions = self.provider_inputs[key]
            providers[api] = regular, inferno
            sunder, shred = max(sunder, reductions["sunder"]), max(shred, reductions["shred"])
        owners = []
        for channel in (0, 1):
            eligible = [api for api, values in providers.items() if values[channel] > 0]
            owners.append(min(eligible, key=lambda api: (-providers[api][channel], api != carry, api != tank, api)) if eligible else None)
        return {"target_sunder": sunder, "target_shred": shred}, owners

    def _conditions(self, scenario, incoming, shared, owners, api):
        return {"incoming_dps": incoming, "physical_share": scenario["physicalShare"],
                "target_hp": scenario["targetHp"], "target_armor": scenario["armor"],
                "target_mr": scenario["mr"], "target_count": scenario["targetCount"],
                "wound": scenario["wound"], "pressure_interval": 1.0,
                "item_burn": api == owners[0], "inferno_burn": api == owners[1], **shared}

    def _opening(self, member, effects, selected, conditions, *, source_mask=None):
        identity = self._identity(member, effects, selected)
        key = identity, source_mask, tft.json_hash(conditions)
        if key not in self.openings:
            api, star, _, items, alpha = identity
            self.openings[key] = self.profiles.theory_opening(api, star, effects[api], items,
                alpha=alpha, source_mask=source_mask, **conditions)
            self.stats["unitOpeningsMeasured"] += 1
        return self.openings[key]

    def evaluate(self, members, effects, selected, carry, tank, *, split="theory", **kwargs):
        return self.evaluate_many(members, effects, [selected], carry, tank, split=split, details=True, **kwargs)[0]

    def evaluate_many(self, members, effects, allocations, carry, tank, *, split="theory", details=False,
                      required_primal=None):
        if not self.optimize_primal and required_primal is None:
            return self._evaluate_resolved_many(members, effects, allocations, carry, tank,
                                                split=split, details=details)
        members, allocations = list(members), list(allocations)
        options = primal_options(self.snap, members, required_primal)
        if options == [()] or not allocations:
            return self._evaluate_resolved_many(members, effects, allocations, carry, tank,
                                                split=split, details=details)
        signature = (tuple((member["api"], member["star"]) for member in members),
                     tft.json_hash(effects), tuple(required_primal or ()))
        if signature not in self.primal_variants:
            groups, indices = {}, []
            for selected in options:
                resolved = with_primal_effects(self.snap, members, effects, selected)
                # Bear's positive-hit execute cannot trigger on this model's
                # immortal/full-health targets. Phoenix has no fixed-budget
                # combat effect. Identical remaining effects share one exact
                # calculation, while all legal choices remain in the evidence.
                numerical = {api: [{key: value for key, value in effect.items()
                                    if not (effect.get("api") == PRIMAL and key == "executeBelowHp")}
                                   for effect in rows] for api, rows in resolved.items()}
                key = tft.json_hash(numerical)
                groups.setdefault(key, resolved)
                indices.append(key)
            self.primal_variants[signature] = groups, indices
            while len(self.primal_variants) > 512:
                self.primal_variants.popitem(last=False)
        else:
            self.primal_variants.move_to_end(signature)
        groups, indices = self.primal_variants[signature]
        measured = {key: self._evaluate_resolved_many(members, resolved, allocations, carry, tank,
                                                     split=split, details=details)
                    for key, resolved in groups.items()}
        if any(len(rows) != len(allocations) for rows in measured.values()):
            raise RuntimeError("Primal comparison returned an incomplete allocation batch")
        results = []
        for index in range(len(allocations)):
            alternatives = [{"blessings": list(option),
                             "score": measured[key][index]["metrics"]["theoryScore"]}
                            for option, key in zip(options, indices, strict=True)]
            # Select a single blessing for the aggregate of all profiles,
            # never a different winner for each pressure assumption.
            best = min(range(len(options)), key=lambda choice: rank_key(measured[indices[choice]][index]))
            result = dict(measured[indices[best]][index])
            result["primal"] = {"selected": list(options[best]), "alternatives": alternatives}
            if required_primal:
                result["primal"]["required"] = list(required_primal)
            results.append(result)
        self.stats["primalChoicesCompared"] += len(options) * len(allocations)
        self.stats["primalDistinctEffectsCompared"] += len(groups) * len(allocations)
        return results

    def _evaluate_resolved_many(self, members, effects, allocations, carry, tank, *, split="theory", details=False):
        if split != "theory":
            raise ValueError("theoretical capacity has no search/held-out opponent splits")
        if (not members or len({member["api"] for member in members}) != len(members)
                or slots_used(members) > MAX_BOARD_SLOTS or carry == tank
                or not {carry, tank} <= {member["api"] for member in members}):
            raise ValueError("capacity needs a legal distinct roster and main carry/tank")
        results = []
        for selected in allocations:
            if set(selected) != {member["api"] for member in members}:
                raise ValueError("capacity allocation must specify every roster member")
            key = tuple(self._identity(member, effects, selected) for member in sorted(members, key=lambda m: m["api"])), carry, tank, details
            if key not in self.results:
                self.results[key] = self._evaluate(members, effects, selected, carry, tank, details)
                self.stats["teamAllocationsSimulated"] += 1
                while len(self.results) > RESULT_CACHE_LIMIT:
                    self.results.popitem(last=False)
            else:
                self.stats["teamAllocationsReused"] += 1
                self.results.move_to_end(key)
            results.append(self.results[key])
        return results

    def _evaluate(self, members, effects, selected, carry, tank, details):
        shared, owners = self._providers(members, effects, selected, carry, tank)
        roles, neutral_ehp = {}, {}
        # Formation is fixed for this allocation. Incoming mix, pressure and
        # Wound sensitivity must not silently rearrange the same board.
        neutral = dict(self.scenarios[0], physicalShare=0.5)
        for member in members:
            api = member["api"]
            opening = self._opening(member, effects, selected,
                self._conditions(neutral, 0.0, shared, owners, api), source_mask=0)
            neutral_ehp[api] = opening["opening"]["residualMixedEhp"]
            unit = self.snap.units[api]
            roles[api] = {"kind": opening["kind"], "form": opening["form"],
                          "frontline": unit["objective"] in ("tank", "fighter") or opening["kind"] == "Assassin",
                          "abilityName": unit.get("forms", {}).get(opening["form"], {}).get("name") or unit["ability"]["name"]}
        fronts = [member for member in members if roles[member["api"]]["frontline"]]
        if not fronts:
            raise ValueError("theoretical capacity requires a frontline")
        priority = sorted((m["api"] for m in fronts),
                          key=lambda api: (api != tank, -neutral_ehp[api], api))
        indices = {member["api"]: index for index, member in enumerate(members)}
        rows, contributions = [], {member["api"]: Counter() for member in members}
        for scenario in self.scenarios:
            pressure = scenario["incomingDps"]
            conditions = {m["api"]: self._conditions(scenario, pressure if roles[m["api"]]["frontline"] else 0.0,
                                                     shared, owners, m["api"]) for m in members}
            target_order = priority[:]
            if scenario.get("targeting", "main-first") == "secondary-first" and len(target_order) > 1:
                target_order[0], target_order[1] = target_order[1], target_order[0]
            elif scenario.get("targeting", "main-first") not in ("main-first", "secondary-first"):
                raise ValueError("unknown pressure targeting formation")
            targetable = {}
            for member in fronts:
                api = member["api"]
                state = self._opening(member, effects, selected, conditions[api], source_mask=0)["opening"]
                if type(state.get("targetable")) is not bool:
                    raise ValueError("targeted opening must report boolean targetable state")
                targetable[api] = state["targetable"]
            eligible = [api for api in target_order if targetable[api]][:2]
            initial_targets = [eligible[source % len(eligible)] if eligible else None for source in range(3)]
            masks = {api: sum(1 << source for source, owner in enumerate(initial_targets) if owner == api)
                     for api in target_order}
            opening_values = []
            for member in fronts:
                api = member["api"]
                state = self._opening(member, effects, selected, conditions[api],
                                      source_mask=masks[api])["opening"]
                if state.get("targetable") is not targetable[api]:
                    raise ValueError("opening targetability changed with its assigned sources")
                opening_values.append(state["residualMixedEhp"])
            opening_ehp = math.fsum(opening_values)
            planned_window = opening_ehp / pressure
            if not math.isfinite(planned_window) or planned_window <= 0 or planned_window > MAX_MEASUREMENT_TIME:
                raise ValueError("derived exposure window exceeds supported measurement range; no cutoff score was substituted")
            specs = [self.profiles.spec(m["api"], m["star"], effects[m["api"]],
                         selected[m["api"]]["items"], alpha=bool(selected[m["api"]].get("alpha")),
                         **conditions[m["api"]]) for m in members]
            observed = self.profiles.measure_team(specs, [roles[m["api"]]["frontline"] for m in members],
                window=planned_window, pressure=pressure, physical_share=scenario["physicalShare"],
                control_interval=scenario.get("controlInterval", CONTROL_INTERVAL),
                control_duration=scenario.get("controlDuration", 0.0),
                target_order=[indices[api] for api in target_order])
            if (observed.get("targetOrder") != [indices[api] for api in target_order]
                    or observed.get("initialSourceTargets") != [indices[api] if api is not None else None for api in initial_targets]):
                raise ValueError("shared measurement targeting disagrees with targeted opening")
            window = observed["elapsed"]
            if not math.isfinite(window) or window <= 0:
                raise ValueError("observed protection window must be finite and positive")
            measured = {m["api"]: response for m, response in zip(members, observed["samples"], strict=True)}
            ehp = math.fsum(measured[m["api"]]["incomingSpent"] + measured[m["api"]]["denied"]
                            + measured[m["api"]]["residualMixedEhp"] for m in fronts)
            dps = math.fsum(response["damage"] for response in measured.values()) / window
            metric = capacity_metrics(ehp, dps, pressure)
            rows.append({**scenario, **metric, "score": metric["theoryScore"],
                         "openingFrontlineEhp": opening_ehp, "measurementWindow": window,
                         "plannedMeasurementWindow": planned_window, "frontlineCollapsed": observed["collapsed"],
                         "incomingBudget": observed["incomingBudget"], "unspentPressure": observed["unspentPressure"],
                         "spentPressure": math.fsum(measured[m["api"]]["incomingSpent"] for m in fronts),
                         "deniedPressure": math.fsum(measured[m["api"]]["denied"] for m in fronts)})
            if details:
                rows[-1].update(pressureTargetOrder=target_order,
                                initialPressureTargets=initial_targets)
                for api, response in measured.items():
                    out = contributions[api]
                    out["damage"] += response["damage"] / len(self.scenarios)
                    out["share"] += (response["damage"] / window / dps if dps else 0.0) / len(self.scenarios)
                    out["measuredDps"] += response["damage"] / window / len(self.scenarios)
                    for target, source in (("aliveTime", "unitAliveTime"), ("damageTaken", "incomingSpent"),
                                           ("healing", "selfHeal"), ("shielding", "selfShield"),
                                           ("allyHealing", "allyHealPotential"), ("allyShielding", "allyShieldPotential"),
                                           ("casts", "casts")):
                        out[target] += response[source] / len(self.scenarios)
        metrics = summarize(rows)
        units = {}
        if details:
            for api, contribution in contributions.items():
                unit = dict(contribution)
                unit["dps"] = unit.pop("share") * metrics["damageDps"]
                units[api] = {**unit, **roles[api]}
        self.stats["theoryProfilesEvaluated"] += len(rows)
        return {"evaluationModel": MODEL, "modelRevision": self.model_revision,
                "profileCount": len(rows), "metrics": metrics, "scenarios": rows, "units": units,
                "itemBudget": sum(len(option["items"]) for option in selected.values()),
                "sharedUtility": {"sunder": shared["target_sunder"], "shred": shared["target_shred"],
                                  "itemBurnHolder": owners[0], "infernoBurnHolder": owners[1]},
                "sensitivity": {"lowestScore": min(row["score"] for row in rows),
                                "highestScore": max(row["score"] for row in rows)}}


class Evaluator(ReferenceEvaluator):
    """Prepare each distinct loadout once and score allocation batches in Rust.

    No response samples cross into Python during production scoring. Explicit
    injected profiles retain the Python path for independently specified math
    tests; a missing native scorer is an error, never a silent slow fallback.
    """

    def __init__(self, snap, geometry, threat="mixed", *, score_cache_dir=None, profiles=None,
                 optimize_primal=False):
        super().__init__(snap, geometry, threat, score_cache_dir=score_cache_dir, profiles=profiles,
                         optimize_primal=optimize_primal)
        self._native = None
        self._registered = {}
        self._ability_names = {}
        if profiles is None:
            native = getattr(tft.engine(), "TheoryScorer", None)
            if native is None:
                raise RuntimeError("Native theoretical scorer is unavailable; rebuild the TFT engine")
            self._native = native(self.scenarios, max_window=MAX_MEASUREMENT_TIME)

    def _evaluate_resolved_many(self, members, effects, allocations, carry, tank, *, split="theory", details=False):
        if self._native is None:
            return super()._evaluate_resolved_many(members, effects, allocations, carry, tank,
                                                  split=split, details=details)
        if split != "theory":
            raise ValueError("theoretical capacity has no search/held-out opponent splits")
        apis = [member["api"] for member in members]
        roster = set(apis)
        if (not members or len(roster) != len(members) or slots_used(members) > MAX_BOARD_SLOTS
                or carry == tank or not {carry, tank} <= roster):
            raise ValueError("capacity needs a legal distinct roster and main carry/tank")
        # Trait signatures are invariant throughout this allocation batch.
        # The reference path used to serialize them at every sample lookup.
        effect_keys = {api: tft.json_hash(effects[api]) for api in apis}
        prepared = []
        for selected in allocations:
            if set(selected) != roster:
                raise ValueError("capacity allocation must specify every roster member")
            identifiers = []
            for member in members:
                api, star = member["api"], member["star"]
                items = tuple(sorted(selected[api]["items"]))
                alpha = bool(selected[api].get("alpha"))
                key = api, star, effect_keys[api], items, alpha
                if key not in self._registered:
                    # The scorer measures equipped items; the standalone
                    # enumeration pool is not part of this calculation.
                    spec = dict(self.profiles.spec(api, star, effects[api], items, alpha=alpha),
                                pool=[], theoryAlpha=alpha)
                    identifier = self._native.register(spec)
                    unit = self.snap.units[api]
                    form = spec["unit"].get("form")
                    self._registered[key] = identifier
                    self._ability_names[identifier] = (unit.get("forms", {}).get(form, {}).get("name")
                                                       or unit["ability"]["name"])
                identifiers.append(self._registered[key])
            prepared.append(identifiers)
        results = self._native.evaluate_many(prepared, apis.index(carry), apis.index(tank), details=details)
        if len(results) != len(prepared):
            raise RuntimeError("native theoretical batch returned an incomplete result")
        for identifiers, result in zip(prepared, results, strict=True):
            result.update(evaluationModel=MODEL, modelRevision=self.model_revision)
            if details:
                for api, identifier in zip(apis, identifiers, strict=True):
                    result["units"][api]["abilityName"] = self._ability_names[identifier]
        for key, value in self._native.stats().items():
            self.stats[key] = value
        self.stats["unitLoadoutsPrepared"] = len(self._registered)
        return results
