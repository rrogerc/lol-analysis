"""Aggregate capacity math and the frozen tank-itemization regression.

Analytic profiles below provide independently specified health budgets and
constant damage rates. Native cases exercise actual pressure accounting and
the reported board without calibrating assumptions to particular items.
"""

from copy import deepcopy
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch

import tft
import tft_theory as theory
from tft_comp_traits import resolve_board_traits
from tft_unit_profiles import UnitProfiles
from test_tft_unit_profiles import plain_spec


SNAP = tft.load_snapshot(18, "18.1d")
TANK = SNAP.unit("Leona")["api"]
SECOND_TANK = SNAP.unit("Malphite")["api"]
CARRY = SNAP.unit("Ashe")["api"]
CINDERLING = SNAP.unit("Cinderling")["api"]
THIRD_TANK = SNAP.unit("Ornn")["api"]


def roster(*apis):
    members = [{"api": api, "star": 1} for api in apis]
    effects = {api: [] for api in apis}
    selected = {api: {"items": (), "alpha": False} for api in apis}
    return members, effects, selected


def scenario(pressure=100.0, *, physical_share=1.0, targeting="main-first"):
    return {"key": f"pressure-{pressure}-{physical_share}-{targeting}", "label": "Analytic pressure",
            "incomingDps": pressure, "physicalShare": physical_share,
            "armor": 0.0, "mr": 0.0, "targetHp": 3000.0, "targetCount": 1, "wound": 0.0,
            "targeting": targeting, "pressureAllocation": "persistent-source-targets"}


def compact_result(result):
    return dict(result, units={}, scenarios=[
        {key: value for key, value in row.items()
         if key not in ("pressureTargetOrder", "initialPressureTargets")}
        for row in result["scenarios"]])


class AnalyticProfiles:
    """Finite raw-health budgets and constant DPS with no combat simulator."""

    def __init__(self, units, items=None):
        self.units = units
        self.items = items or {}
        self.calls = []
        self.opening_calls = []
        self.shared_calls = []

    def spec(self, api, star, effects, items, *, alpha=False, **conditions):
        return {"items": [deepcopy(self.items[item]) for item in items], "traits": deepcopy(effects),
                "kits": {"base": {"rows": {"BurnAmount": self.units[api].get("nativeBurnPercent", 0.0)}}},
                "analyticApi": api, "conditions": conditions}

    @staticmethod
    def capacity(unit, physical_share):
        if "physicalEhp" not in unit:
            return unit["ehp"]
        physical, magic = unit["physicalEhp"], unit["magicEhp"]
        return 1 / (physical_share / physical + (1 - physical_share) / magic)

    def theory_opening(self, api, star, effects, items, *, alpha=False, source_mask=None, **conditions):
        self.opening_calls.append((api, source_mask, dict(conditions)))
        result = self.curve(api, star, effects, items, (0.0,), alpha=alpha, **conditions)
        if source_mask is not None:
            unit = self.units[api]
            result["opening"]["targetable"] = unit.get("targetable", True) and unit.get("untargetableUntil", 0) <= 0
        return result

    def measure_team(self, specs, fronts, *, window, pressure, physical_share,
                     control_interval=8.0, control_duration=0.0, target_order=None):
        """Three continuous streams keep their victims until unavailable.

        Health exhaustion is solved analytically, independently of the native
        half-second packets. These fixtures deliberately exclude enemy CC.
        """
        assert not control_duration
        assert sorted(target_order) == [i for i, front in enumerate(fronts) if front]
        self.shared_calls.append((deepcopy(specs), fronts[:], window, pressure))
        units = [self.units[spec["analyticApi"]] for spec in specs]
        health = [self.capacity(unit, physical_share) for unit in units]
        samples = [{"damage": 0.0, "incomingSpent": 0.0, "denied": 0.0,
                    "selfHeal": 0.0, "selfShield": 0.0, "allyHealPotential": 0.0,
                    "allyShieldPotential": 0.0, "unitAliveTime": 0.0, "casts": 0.0}
                   for _ in specs]
        def eligible(index, time):
            return (fronts[index] and health[index] > 1e-8
                    and units[index].get("targetable", True)
                    and time >= units[index].get("untargetableUntil", 0))

        initial = [i for i in target_order if eligible(i, 0)][:2]
        owners = [initial[source % len(initial)] if initial else None for source in range(3)]
        initial_owners = owners[:]
        time, granted, unspent = 0.0, False, 0.0
        while time < window:
            holding = [i for i, front in enumerate(fronts) if front and health[i] > 1e-8]
            if not holding:
                break
            exposed = [i for i in target_order if eligible(i, time)]
            owners = [owner if owner is not None and eligible(owner, time)
                      else exposed[0] if exposed else None for owner in owners]
            rates = [owners.count(i) * pressure / 3 for i in range(len(units))]
            death = min((time + health[i] / rates[i] for i in holding if rates[i] > 0), default=math.inf)
            next_targetable = min((unit["untargetableUntil"] for unit in units
                                   if unit.get("untargetableUntil", 0) > time), default=math.inf)
            end = min(window, death, next_targetable, 1.0 if not granted else math.inf)
            delta = end - time
            for i, (unit, spec, sample) in enumerate(zip(units, specs, samples)):
                if health[i] <= 1e-8:
                    continue
                dps = unit["dps"]
                if spec["conditions"].get("item_burn"):
                    dps += unit.get("itemBurnDps", 0.0)
                if spec["conditions"].get("inferno_burn"):
                    dps += unit.get("infernoBurnDps", 0.0)
                sample["damage"] += dps * delta
                sample["unitAliveTime"] += delta
                sample["allyHealPotential"] += unit.get("allyHeal", 0.0) * delta
                sample["allyShieldPotential"] += unit.get("allyShield", 0.0) * delta
                if rates[i]:
                    health[i] = max(0.0, health[i] - rates[i] * delta)
                    sample["incomingSpent"] += rates[i] * delta
            unspent += pressure * delta if not exposed else 0.0
            time = end
            if not granted and time >= 1.0:
                granted = True
                for i in exposed:
                    if health[i] > 1e-8:
                        sample = samples[i]
                        sample["selfHeal"] = units[i].get("heal", 0.0)
                        sample["selfShield"] = units[i].get("shield", 0.0)
                        health[i] += sample["selfHeal"] + sample["selfShield"]
        for i, sample in enumerate(samples):
            sample["residualMixedEhp"] = health[i]
        return {"elapsed": time, "collapsed": not any(front and hp > 1e-8 for front, hp in zip(fronts, health)),
                "incomingBudget": pressure * time, "unspentPressure": unspent, "samples": samples,
                "targetOrder": list(target_order), "initialSourceTargets": initial_owners}

    def curve(self, api, star, effects, items, times, *, alpha=False, **conditions):
        self.calls.append((api, tuple(times), dict(conditions)))
        unit = self.units[api]
        incoming = conditions.get("incoming_dps", 0.0)
        base_health = self.capacity(unit, conditions.get("physical_share", 0.5))
        heal, shield = unit.get("heal", 0.0), unit.get("shield", 0.0)
        # The synthetic sustain is granted once at one second. It has no
        # effect on the independently declared opening health budget.
        total_health = base_health + heal + shield if incoming else base_health
        death_time = total_health / incoming if incoming else math.inf
        damage_rate = unit["dps"]
        if conditions.get("item_burn"):
            damage_rate += unit.get("itemBurnDps", 0.0)
        if conditions.get("inferno_burn"):
            damage_rate += unit.get("infernoBurnDps", 0.0)
        samples = []
        for time in times:
            sustain_ready = incoming > 0 and time >= 1
            health_available = base_health + (heal + shield if sustain_ready else 0.0)
            spent = min(incoming * time, health_available)
            samples.append({
                "time": time, "damage": damage_rate * min(time, death_time),
                "incomingSpent": spent, "denied": 0.0,
                "residualMixedEhp": max(0.0, health_available - spent),
                "selfHeal": heal if sustain_ready else 0.0,
                "selfShield": shield if sustain_ready else 0.0,
                "allyHealPotential": unit.get("allyHeal", 0.0) * time,
                "allyShieldPotential": unit.get("allyShield", 0.0) * time,
                "aliveTime": min(time, death_time), "unitAliveTime": min(time, death_time), "casts": 0,
            })
        return {"opening": {"residualMixedEhp": base_health},
                "kind": tft.unit_role_kind(SNAP.units[api]), "form": None, "samples": samples}


class PlainNativeProfiles(UnitProfiles):
    """Native constant attacks/health pools with all other effects removed."""

    def __init__(self, units):
        super().__init__(SNAP, "spread")
        self.units = units

    def spec(self, api, star, effects, items, *, alpha=False, **conditions):
        values = self.units[api]
        spec = plain_spec(hp=values["hp"], damage=values["damage"],
                          incoming_dps=conditions.get("incoming_dps", 0),
                          physical_share=conditions.get("physical_share", 1),
                          fx=values.get("fx", ()))
        spec["unit"] = dict(spec["unit"], api=api, kind=tft.unit_role_kind(SNAP.units[api]))
        return spec


def analytic_evaluation(units, pressure=100.0, *, selected=None, effects=None, items=None):
    members, defaults, default_selection = roster(*units)
    profiles = AnalyticProfiles(units, items)
    evaluator = theory.Evaluator(SNAP, "spread", profiles=profiles)
    evaluator.scenarios = [scenario(pressure)]
    result = evaluator.evaluate(members, effects or defaults, selected or default_selection, CARRY, TANK)
    return result, profiles


class TestCapacityMath(unittest.TestCase):
    def test_capacity_has_the_expected_physical_units(self):
        self.assertEqual(theory.capacity_metrics(2400, 300, 600), {
            "frontlineEhp": 2400, "damageDps": 300, "theoryScore": 720000,
            "protectionTime": 4, "damageCapacity": 1200})
        self.assertEqual(theory.capacity_metrics(2400, 0, 600)["theoryScore"], 0)
        for args in ((-1, 100, 100), (100, -1, 100), (100, 100, 0),
                     (math.inf, 100, 100), (100, math.nan, 100), (1e300, 1e300, 1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                theory.capacity_metrics(*args)

    def test_geometric_mean_is_scale_stable_and_zero_is_not_discarded(self):
        self.assertAlmostEqual(theory.geometric_mean([100, 400]), 200)
        self.assertAlmostEqual(theory.geometric_mean([1e-200, 1e200]), 1)
        self.assertEqual(theory.geometric_mean([100, 0, 400]), 0)
        for values in ([], [-1, 10], [math.nan, 1], [math.inf, 1]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                theory.geometric_mean(values)

    def test_summary_matches_geometric_mean_of_capacity_across_different_pressures(self):
        rows = [theory.capacity_metrics(1000, 100, 100), theory.capacity_metrics(4000, 400, 400)]
        summary = theory.summarize(rows)
        for field, expected in (("frontlineEhp", 2000), ("damageDps", 200),
                                ("theoryScore", 400000), ("protectionTime", 10),
                                ("damageCapacity", 2000)):
            with self.subTest(field=field):
                self.assertAlmostEqual(summary[field], expected)
        self.assertAlmostEqual(summary["theoryScore"], theory.geometric_mean(row["theoryScore"] for row in rows))
        self.assertLess(theory.rank_key({"metrics": summary}), theory.rank_key(rows[0]))


class TestAggregateResponses(unittest.TestCase):
    def test_static_frontline_capacity_equals_ehp_times_team_dps(self):
        result, profiles = analytic_evaluation({
            TANK: {"ehp": 1000, "dps": 10}, SECOND_TANK: {"ehp": 1000, "dps": 10},
            CARRY: {"ehp": 1e9, "dps": 80}})
        row = result["scenarios"][0]
        self.assertEqual(row["measurementWindow"], 20)
        self.assertEqual(row["frontlineEhp"], 2000)
        # The main tank takes two streams and dies at15s; the other takes
        # its streams thereafter and survives until20s. Only its DPS remains.
        self.assertEqual(row["damageDps"], 97.5)
        self.assertEqual(row["protectionTime"], 20)
        self.assertEqual(row["damageCapacity"], 1950)
        self.assertEqual(row["theoryScore"], 195000)
        measured_pressure = {spec["analyticApi"]: spec["conditions"]["incoming_dps"]
                            for spec in profiles.shared_calls[0][0]}
        self.assertEqual(measured_pressure, {TANK: 100, SECOND_TANK: 100, CARRY: 0})
        self.assertEqual(row["initialPressureTargets"], [TANK, SECOND_TANK, TANK])
        self.assertEqual(row["incomingBudget"], 2000)
        self.assertTrue(row["frontlineCollapsed"])

    def test_backline_health_does_not_count_as_frontline_protection(self):
        results = [analytic_evaluation({TANK: {"ehp": 1000, "dps": 0},
                                        CARRY: {"ehp": hp, "dps": 100}})[0]
                   for hp in (1, 1e12)]
        self.assertEqual(results[0]["metrics"], results[1]["metrics"])
        self.assertEqual(results[1]["scenarios"][0]["openingFrontlineEhp"], 1000)
        self.assertFalse(results[1]["units"][CARRY]["frontline"])

    def test_dead_frontliner_pressure_is_conserved_in_independent_budget_oracle(self):
        result, _ = analytic_evaluation({TANK: {"ehp": 100, "dps": 10},
            SECOND_TANK: {"ehp": 900, "dps": 10}, CARRY: {"ehp": 1, "dps": 80}})
        row = result["scenarios"][0]
        self.assertEqual(result["units"][TANK]["aliveTime"], 1.5)
        self.assertEqual(result["units"][SECOND_TANK]["aliveTime"], 10)
        self.assertEqual(row["spentPressure"], 1000)
        self.assertEqual(row["incomingBudget"], row["spentPressure"])
        self.assertTrue(row["frontlineCollapsed"])
        self.assertEqual(row["damageDps"], 91.5)

    def test_derived_windows_continue_across_thirty_seconds_without_a_score_cliff(self):
        for health in (2975, 3025, 6100):
            with self.subTest(health=health):
                result, profiles = analytic_evaluation({TANK: {"ehp": health, "dps": 0},
                                                        CARRY: {"ehp": 1, "dps": 100}})
                row = result["scenarios"][0]
                self.assertEqual(row["measurementWindow"], health / 100)
                self.assertEqual(row["protectionTime"], health / 100)
                self.assertEqual(row["theoryScore"], health * 100)
                self.assertEqual(row["damageCapacity"], health)
                self.assertEqual(profiles.shared_calls[0][2], health / 100)

    def test_unsupported_horizons_raise_instead_of_becoming_losses_or_capped_scores(self):
        with self.assertRaisesRegex(ValueError, "no cutoff score"):
            analytic_evaluation({TANK: {"ehp": 100 * (theory.MAX_MEASUREMENT_TIME + 1), "dps": 0},
                                 CARRY: {"ehp": 1, "dps": 100}})

    def test_effective_self_sustain_is_already_in_the_spent_and_remaining_budget(self):
        result, _ = analytic_evaluation({TANK: {"ehp": 1000, "dps": 0, "heal": 200, "shield": 300,
                                              "allyHeal": 10000, "allyShield": 10000},
                                        CARRY: {"ehp": 1, "dps": 100}})
        row = result["scenarios"][0]
        self.assertEqual(row["measurementWindow"], 10)
        self.assertEqual(row["frontlineEhp"], 1500)
        self.assertEqual(row["theoryScore"], 150000)
        self.assertEqual(result["units"][TANK]["healing"], 200)
        self.assertEqual(result["units"][TANK]["shielding"], 300)
        self.assertEqual(result["units"][TANK]["allyHealing"], 100000)
        self.assertEqual(result["units"][TANK]["allyShielding"], 100000)

    def test_native_lethal_overkill_on_a_weak_frontliner_does_not_inflate_team_ehp(self):
        members, effects, selected = roster(TANK, SECOND_TANK, CARRY)
        profiles = PlainNativeProfiles({TANK: {"hp": 150, "damage": 0},
                                        SECOND_TANK: {"hp": 2000, "damage": 0},
                                        CARRY: {"hp": 1e9, "damage": 100}})
        evaluator = theory.Evaluator(SNAP, "spread", profiles=profiles)
        evaluator.scenarios = [scenario(1000)]
        result = evaluator.evaluate(members, effects, selected, CARRY, TANK)
        row = result["scenarios"][0]
        # The half-second source packet offers500 to its one target. The
        # weak tank spends150; its excess transfers to the next priority.
        self.assertEqual(row["openingFrontlineEhp"], 2150)
        self.assertEqual(row["frontlineEhp"], 2150)
        self.assertEqual(result["units"][TANK]["damageTaken"], 150)
        self.assertEqual(result["units"][TANK]["aliveTime"], .5)
        self.assertAlmostEqual(row["incomingBudget"], 2150)
        self.assertAlmostEqual(result["units"][SECOND_TANK]["damageTaken"], 2000)

    def test_compact_and_full_results_share_identical_ranking_and_attribution_totals(self):
        members, effects, selected = roster(TANK, CARRY)
        profiles = AnalyticProfiles({TANK: {"ehp": 1000, "dps": 10}, CARRY: {"ehp": 1, "dps": 90}})
        evaluator = theory.Evaluator(SNAP, "spread", profiles=profiles)
        evaluator.scenarios = [scenario(100), scenario(200)]
        compact = evaluator.evaluate_many(members, effects, [selected, selected], CARRY, TANK)
        full = evaluator.evaluate(members, effects, selected, CARRY, TANK)
        self.assertIs(compact[0], compact[1])
        self.assertEqual(compact[0], compact_result(full))
        self.assertEqual(theory.rank_key(compact[0]), theory.rank_key(full))
        self.assertAlmostEqual(sum(unit["dps"] for unit in full["units"].values()), full["metrics"]["damageDps"])

    def test_shared_debuffs_and_each_burn_channel_have_one_correctly_scaled_provider(self):
        members, effects, selected = roster(TANK, CARRY, CINDERLING)
        selected[CARRY]["items"] = ("strong-burn", "sunder")
        selected[TANK]["items"] = ("weak-burn", "shred")
        effects[TANK] = [{"api": "DA_18_Inferno", "burnOnHit": [0.02, 10]}]
        effects[CARRY] = [{"api": "DA_18_Inferno", "burnOnHit": [0.01, 10]}]
        profiles = AnalyticProfiles({
            TANK: {"ehp": 1000, "dps": 10, "itemBurnDps": 10, "infernoBurnDps": 20},
            CARRY: {"ehp": 1, "dps": 100, "itemBurnDps": 20, "infernoBurnDps": 10},
            CINDERLING: {"ehp": 1, "dps": 50, "itemBurnDps": 10, "nativeBurnPercent": 1.0}},
            items={"strong-burn": {"burnOnHit": [0.02, 5]}, "weak-burn": {"burnOnHit": [0.01, 5]},
                   "sunder": {"sunderOnHit": [0.3, 3]}, "shred": {"shredAura": 0.4}})
        evaluator = theory.Evaluator(SNAP, "spread", profiles=profiles)
        evaluator.scenarios = [scenario()]
        result = evaluator.evaluate(members, effects, selected, CARRY, TANK)
        # The on-hit Sunder is timed, applied per target inside the shared
        # measurement; only the Shred aura is standing coverage.
        self.assertEqual(result["sharedUtility"], {"sunder": 0.0, "shred": 0.4,
                                                   "itemBurnHolder": CARRY, "infernoBurnHolder": TANK})
        self.assertEqual(result["scenarios"][0]["damageDps"], 200)
        for api, _, conditions in profiles.calls:
            self.assertEqual(conditions["target_sunder"], 0.0)
            self.assertEqual(conditions["target_shred"], 0.4)
            self.assertEqual(conditions["item_burn"], api == CARRY)
            self.assertEqual(conditions["inferno_burn"], api == TANK)

    def test_priority_uses_neutral_defenses_and_is_fixed_across_pressure_and_damage_mix(self):
        members, effects, selected = roster(SECOND_TANK, CARRY, THIRD_TANK, TANK)
        profiles = AnalyticProfiles({
            TANK: {"ehp": 1000, "dps": 0},
            SECOND_TANK: {"physicalEhp": 900, "magicEhp": 100, "dps": 0},
            THIRD_TANK: {"ehp": 400, "dps": 0}, CARRY: {"ehp": 1, "dps": 100}})
        evaluator = theory.ReferenceEvaluator(SNAP, "spread", profiles=profiles)
        evaluator.scenarios = [scenario(pressure, physical_share=share, targeting=targeting)
                              for pressure in (100, 200) for share in (0, 1)
                              for targeting in ("main-first", "secondary-first")]
        result = evaluator.evaluate(members, effects, selected, CARRY, TANK)
        for row in result["scenarios"]:
            with self.subTest(scenario=row["key"]):
                # Neutral EHP is180 for the physical specialist and400 for
                # Ornn. Pure physical pressure must not move the specialist.
                order = ([TANK, THIRD_TANK, SECOND_TANK] if row["targeting"] == "main-first"
                         else [THIRD_TANK, TANK, SECOND_TANK])
                self.assertEqual(row["pressureTargetOrder"], order)
                self.assertEqual(row["initialPressureTargets"], [order[0], order[1], order[0]])
        neutral = [conditions for _, mask, conditions in profiles.opening_calls
                   if mask == 0 and conditions["incoming_dps"] == 0]
        self.assertTrue(neutral)
        self.assertTrue(all(conditions["physical_share"] == 0.5 for conditions in neutral))

    def test_initial_untargetability_assigns_all_sources_to_the_only_eligible_front(self):
        members, effects, selected = roster(TANK, SECOND_TANK, CARRY)
        profiles = AnalyticProfiles({TANK: {"ehp": 1000, "dps": 0, "targetable": False},
                                    SECOND_TANK: {"ehp": 500, "dps": 0}, CARRY: {"ehp": 1, "dps": 100}})
        evaluator = theory.ReferenceEvaluator(SNAP, "spread", profiles=profiles)
        evaluator.scenarios = [scenario()]
        result = evaluator.evaluate(members, effects, selected, CARRY, TANK)
        row = result["scenarios"][0]
        self.assertEqual(row["pressureTargetOrder"], [TANK, SECOND_TANK])
        self.assertEqual(row["initialPressureTargets"], [SECOND_TANK] * 3)
        self.assertEqual(row["spentPressure"], 500)
        self.assertEqual(row["unspentPressure"], 1000)
        self.assertTrue(any(api == SECOND_TANK and mask == 7 for api, mask, _ in profiles.opening_calls))
        self.assertTrue(all(mask == 0 for api, mask, _ in profiles.opening_calls if api == TANK))

    def test_a_returning_frontliner_does_not_take_sources_from_a_valid_owner(self):
        profiles = AnalyticProfiles({TANK: {"ehp": 1000, "dps": 0, "untargetableUntil": 1},
                                    SECOND_TANK: {"ehp": 1000, "dps": 0}})
        specs = [profiles.spec(api, 1, [], []) for api in (TANK, SECOND_TANK)]
        measured = profiles.measure_team(specs, [True, True], window=2, pressure=100,
                                         physical_share=1, target_order=[0, 1])
        self.assertEqual(measured["initialSourceTargets"], [1, 1, 1])
        self.assertEqual([row["incomingSpent"] for row in measured["samples"]], [0, 200])

    def test_opening_cache_separates_source_masks(self):
        members, effects, selected = roster(TANK, CARRY)
        profiles = AnalyticProfiles({TANK: {"ehp": 1000, "dps": 0}, CARRY: {"ehp": 1, "dps": 100}})
        evaluator = theory.ReferenceEvaluator(SNAP, "spread", profiles=profiles)
        conditions = evaluator._conditions(scenario(), 100, {}, [None, None], TANK)
        openings = [evaluator._opening(members[0], effects, selected, conditions, source_mask=mask)
                    for mask in (0, 5, 2, 7, 5)]
        self.assertIs(openings[1], openings[-1])
        self.assertEqual([mask for _, mask, _ in profiles.opening_calls], [0, 5, 2, 7])
        self.assertEqual(evaluator.stats["unitOpeningsMeasured"], 4)

    def test_focused_opening_cannot_silently_change_the_targetability_used_for_assignment(self):
        class InconsistentProfiles(AnalyticProfiles):
            def theory_opening(self, *args, source_mask=None, **kwargs):
                result = super().theory_opening(*args, source_mask=source_mask, **kwargs)
                if source_mask:
                    result["opening"]["targetable"] = False
                return result

        members, effects, selected = roster(TANK, CARRY)
        profiles = InconsistentProfiles({TANK: {"ehp": 1000, "dps": 0}, CARRY: {"ehp": 1, "dps": 100}})
        evaluator = theory.ReferenceEvaluator(SNAP, "spread", profiles=profiles)
        evaluator.scenarios = [scenario()]
        with self.assertRaisesRegex(ValueError, "targetability changed"):
            evaluator.evaluate(members, effects, selected, CARRY, TANK)
        self.assertEqual(profiles.shared_calls, [])


class TestFrozenTankItemization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((Path(tft.TFT_DATA_DIR) / "regressions" / "tank-itemization.json").read_text())
        cls.snap = tft.load_snapshot(18, cls.fixture["patch"])
        cls.members = [{"api": unit["api"], "star": unit["star"]} for unit in cls.fixture["units"]]
        cls.effects = resolve_board_traits(cls.snap, cls.members)["effects"]
        cls.original = {unit["api"]: {"items": tuple(unit["itemApis"]), "alpha": False}
                        for unit in cls.fixture["units"]}
        cls.defensive = deepcopy(cls.original)
        for api, items in cls.fixture["defensiveReplacement"].items():
            cls.defensive[api]["items"] = tuple(items)
        cls.evaluator = theory.Evaluator(cls.snap, cls.fixture["geometry"])
        cls.before, cls.after = cls.evaluator.evaluate_many(cls.members, cls.effects,
            [cls.original, cls.defensive], cls.fixture["mainCarry"], cls.fixture["mainTank"], details=True)

    def test_defensive_substitution_improves_capacity_despite_lower_measured_dps(self):
        changed = {api for api in self.original if self.original[api] != self.defensive[api]}
        self.assertEqual(changed, {"TFT18_Malphite", "TFT18_Sentinel"})
        self.assertEqual(self.before["itemBudget"], 9)
        self.assertEqual(self.after["itemBudget"], 9)
        self.assertLess(self.after["metrics"]["damageDps"], self.before["metrics"]["damageDps"])
        self.assertGreater(self.after["metrics"]["frontlineEhp"], self.before["metrics"]["frontlineEhp"])
        self.assertGreater(self.after["metrics"]["theoryScore"], self.before["metrics"]["theoryScore"])
        self.assertLess(theory.rank_key(self.after), theory.rank_key(self.before))
        self.assertEqual(self.before["modelRevision"], self.after["modelRevision"])
        for result in (self.before, self.after):
            for value in result["metrics"].values():
                self.assertTrue(math.isfinite(value))
                self.assertGreater(value, 0)

    def test_native_compact_and_detailed_results_match(self):
        compact = self.evaluator.evaluate_many(self.members, self.effects, [self.original, self.defensive],
                                               self.fixture["mainCarry"], self.fixture["mainTank"])
        for actual, full in zip(compact, (self.before, self.after)):
            self.assertEqual(actual, compact_result(full))
            self.assertAlmostEqual(sum(unit["dps"] for unit in full["units"].values()), full["metrics"]["damageDps"])

    def test_native_evaluation_needs_no_authored_pool_or_calibrated_opponent_dummies(self):
        with patch("tft_team.load_pool", side_effect=AssertionError("authored opponent pool was read")), \
             patch("tft.dummies_for", side_effect=AssertionError("named carry calibration was used")):
            evaluator = theory.Evaluator(self.snap, self.fixture["geometry"])
            actual = evaluator.evaluate(self.members, self.effects, self.original,
                                         self.fixture["mainCarry"], self.fixture["mainTank"])
        self.assertEqual(actual, self.before)
        self.assertNotIn("wins", actual["metrics"])
        self.assertNotIn("matchups", actual)
        self.assertEqual(actual["evaluationModel"], theory.MODEL)


class TestReportedBramblebackBoard(unittest.TestCase):
    def test_reported_board_conserves_pressure_and_stops_at_actual_collapse(self):
        fixture = json.loads((Path(tft.TFT_DATA_DIR) / "regressions" / "brambleback-ramp.json").read_text())
        snap = tft.load_snapshot(18, fixture["patch"])
        members = [{"api": unit["api"], "star": unit["star"]} for unit in fixture["units"]]
        effects = resolve_board_traits(snap, members)["effects"]
        selected = {unit["api"]: {"items": unit["items"], "alpha": unit["alpha"]} for unit in fixture["units"]}
        result = theory.Evaluator(snap, fixture["geometry"]).evaluate(
            members, effects, selected, fixture["mainCarry"], fixture["mainTank"])
        self.assertEqual(result["profileCount"], 48)
        self.assertEqual({row["controlDuration"] for row in result["scenarios"]}, {0.0, 1.5})
        for row in result["scenarios"]:
            with self.subTest(scenario=row["key"]):
                self.assertLessEqual(row["measurementWindow"], row["plannedMeasurementWindow"])
                self.assertAlmostEqual(row["incomingBudget"], row["incomingDps"] * row["measurementWindow"], places=7)
                self.assertAlmostEqual(row["incomingBudget"], row["spentPressure"]
                    + row["deniedPressure"] + row["unspentPressure"], places=7)
        # Record the reported input, but never encode an item-category or
        # duplicate-item preference as an expected winner in a regression.
        self.assertEqual(selected["TFT18_Brambleback"]["items"], ["DA_GuinsoosRageblade"] * 3)


if __name__ == "__main__":
    unittest.main()
