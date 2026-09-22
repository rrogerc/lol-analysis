//! Cached theoretical composition scoring with conserved frontline pressure.
//! Every champion keeps its own live state on a shared measurement clock;
//! Python resolves loadouts and independently verifies the aggregates.

use std::collections::{HashMap, HashSet, VecDeque};
use std::hash::Hash;
use std::sync::Arc;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyInt, PyList};

use crate::fight::ResponseSample;
use crate::fx::{build_fx_for, Form};
use crate::kit::{Kit, RowId};
use crate::pyget::*;
use crate::spec::{CellSpec, DummySpec, EnemyDebuffs, Kind, Objective, TargetDebuffs};

const MAX_BOARD_SLOTS: usize = 9;
const RESULT_CACHE_LIMIT: usize = 2048;
const DAMAGE: usize = 0;
const SPENT: usize = 1;
const DENIED: usize = 2;
const RESIDUAL: usize = 3;
const HEAL: usize = 4;
const SHIELD: usize = 5;
const ALLY_HEAL: usize = 6;
const ALLY_SHIELD: usize = 7;
const UNIT_ALIVE: usize = 9;
const CASTS: usize = 10;
const RESPONSE_FIELDS: usize = 11;

fn invalid(message: impl Into<String>) -> PyErr {
    PyValueError::new_err(message.into())
}

fn finite_nonnegative(value: f64, field: &str) -> PyResult<f64> {
    if !value.is_finite() || value < 0.0 {
        Err(invalid(format!("{field} must be finite and nonnegative")))
    } else { Ok(value) }
}

fn positive(value: f64, field: &str) -> PyResult<f64> {
    finite_nonnegative(value, field)?;
    if value == 0.0 { Err(invalid(format!("{field} must be positive"))) } else { Ok(value) }
}

fn fraction(value: f64, field: &str) -> PyResult<f64> {
    finite_nonnegative(value, field)?;
    if value > 1.0 { Err(invalid(format!("{field} must be from 0 to 1"))) } else { Ok(value) }
}

/// CPython math.fsum's partials algorithm and final half-even correction.
/// Equivalent to the existing LoL engine's fsum for the finite inputs used
/// here. A plain sum or Neumaier sum can reorder near-tied compositions.
fn accurate_sum(values: impl IntoIterator<Item = f64>) -> f64 {
    let mut partials: Vec<f64> = Vec::with_capacity(16);
    let mut lo = 0.0;
    for mut x in values {
        let mut i = 0;
        for j in 0..partials.len() {
            let mut y = partials[j];
            if x.abs() < y.abs() { std::mem::swap(&mut x, &mut y); }
            let hi = x + y;
            let yr = hi - x;
            lo = y - yr;
            if lo != 0.0 { partials[i] = lo; i += 1; }
            x = hi;
        }
        partials.truncate(i);
        if x != 0.0 { partials.push(x); }
    }
    let mut hi = 0.0;
    let mut n = partials.len();
    if n > 0 {
        n -= 1;
        hi = partials[n];
        while n > 0 {
            let x = hi;
            n -= 1;
            let y = partials[n];
            hi = x + y;
            let yr = hi - x;
            lo = y - yr;
            if lo != 0.0 { break; }
        }
        if n > 0 && ((lo < 0.0 && partials[n - 1] < 0.0)
                      || (lo > 0.0 && partials[n - 1] > 0.0)) {
            let y = lo * 2.0;
            let x = hi + y;
            if x - hi == y { hi = x; }
        }
    }
    hi
}

fn geometric_mean(values: impl IntoIterator<Item = f64>) -> PyResult<f64> {
    let values: Vec<f64> = values.into_iter().collect();
    if values.is_empty() || values.iter().any(|v| !v.is_finite() || *v < 0.0) {
        return Err(invalid("capacity metrics require finite nonnegative values"));
    }
    if values.contains(&0.0) { return Ok(0.0); }
    Ok((accurate_sum(values.iter().map(|v| v.ln())) / values.len() as f64).exp())
}

/// Bounded LRU with an amortized constant-time access log. Stale log
/// entries are compacted before they can grow beyond four cache lengths.
struct Cache<K, V> {
    values: HashMap<K, (Arc<V>, u64)>,
    accesses: VecDeque<(K, u64)>,
    clock: u64,
    limit: usize,
}

impl<K: Clone + Eq + Hash, V> Cache<K, V> {
    fn new(limit: usize) -> Self {
        Self { values: HashMap::new(), accesses: VecDeque::new(), clock: 0, limit }
    }

    fn compact(&mut self) {
        if self.accesses.len() > self.limit.saturating_mul(4) {
            self.accesses.retain(|(key, stamp)| self.values.get(key)
                .is_some_and(|(_, current)| current == stamp));
        }
    }

    fn get(&mut self, key: &K) -> Option<Arc<V>> {
        let (value, stamp) = self.values.get_mut(key)?;
        self.clock += 1;
        *stamp = self.clock;
        let result = value.clone();
        self.accesses.push_back((key.clone(), self.clock));
        self.compact();
        Some(result)
    }

    fn insert(&mut self, key: K, value: V) -> Arc<V> {
        self.clock += 1;
        let value = Arc::new(value);
        self.values.insert(key.clone(), (value.clone(), self.clock));
        self.accesses.push_back((key, self.clock));
        while self.values.len() > self.limit {
            let (key, stamp) = self.accesses.pop_front().expect("cache access log");
            if self.values.get(&key).is_some_and(|(_, current)| *current == stamp) {
                self.values.remove(&key);
            }
        }
        self.compact();
        value
    }
}

struct Scenario {
    source: Py<PyDict>,
    pressure: f64,
    physical_share: f64,
    target_hp: f64,
    armor: f64,
    mr: f64,
    wound: f64,
    target_count: usize,
    control_interval: f64,
    control_duration: f64,
    secondary_first: bool,
    prepared_index: usize,
}

impl Scenario {
    fn parse(source: &Bound<'_, PyDict>) -> PyResult<Self> {
        let count = get(source, "targetCount")?.ok_or_else(|| invalid("scenario requires targetCount"))?;
        if count.is_instance_of::<PyBool>() || !count.is_instance_of::<PyInt>() {
            return Err(invalid("targetCount must be an integer from 1 to 8"));
        }
        let target_count: usize = count.extract()?;
        if !(1..=crate::fight::MAX_TARGETS).contains(&target_count) {
            return Err(invalid("targetCount must be an integer from 1 to 8"));
        }
        if geti(source, "incomingSourceCount", 3)? != 3 {
            return Err(invalid("theoretical pressure uses three incoming source channels"));
        }
        if gets(source, "pressureAllocation", "")? != "persistent-source-targets" {
            return Err(invalid("theoretical pressure requires persistent source targets"));
        }
        let secondary_first = match gets(source, "targeting", "")?.as_str() {
            "main-first" => false,
            "secondary-first" => true,
            _ => return Err(invalid("targeting must be main-first or secondary-first")),
        };
        Ok(Self {
            source: source.copy()?.unbind(),
            pressure: positive(reqf(source, "incomingDps")?, "incomingDps")?,
            physical_share: fraction(reqf(source, "physicalShare")?, "physicalShare")?,
            target_hp: positive(reqf(source, "targetHp")?, "targetHp")?,
            armor: finite_nonnegative(reqf(source, "armor")?, "armor")?,
            mr: finite_nonnegative(reqf(source, "mr")?, "mr")?,
            wound: fraction(reqf(source, "wound")?, "wound")?,
            target_count,
            control_interval: positive(getf(source, "controlInterval", 8.0)?, "controlInterval")?,
            control_duration: finite_nonnegative(getf(source, "controlDuration", 0.0)?, "controlDuration")?,
            secondary_first,
            prepared_index: 0,
        })
    }
}

#[derive(Clone, Copy, Default)]
struct Providers {
    regular: f64,
    inferno: f64,
    sunder: f64,
    shred: f64,
}

struct Registered {
    spec: CellSpec,
    providers: Providers,
    form: Option<Form>,
    frontline: bool,
}

impl Registered {
    fn parse(input: &Bound<'_, PyDict>) -> PyResult<Self> {
        let spec = CellSpec::from_py(input)?;
        if spec.unit.api.is_empty() || !(1..=3).contains(&spec.star) || spec.items.len() > 3 {
            return Err(invalid("loadout requires a champion API, star 1 to 3 and at most three items"));
        }
        if !crate::drivers::NAMES.contains(&spec.driver.as_str()) {
            return Err(invalid(format!("no driver named {:?}", spec.driver)));
        }
        for item in &spec.items {
            if item.unique && spec.items.iter().filter(|other| other.api == item.api).count() > 1 {
                return Err(invalid("a unique item cannot be repeated on one unit"));
            }
        }
        let mut providers = Providers::default();
        for value in getlist(input, "items")? {
            let effect = dict_of(&value)?;
            for name in ["burnOnHit", "burnAura"] {
                if let Some(values) = getvecf(&effect, name)? {
                    if let Some(&rate) = values.first() { providers.regular = providers.regular.max(rate); }
                }
            }
        }
        let alpha = match get(input, "theoryAlpha")? {
            Some(value) => value.extract::<bool>()?,
            None => spec.traits.iter().any(|t| t.api == "DA_Riftbeast18" && t.riftbeast),
        };
        if spec.unit.api == "TFT18_Cinderling" || spec.unit.api == "TFT18_Brambleback" && alpha {
            let row = spec.kit_base.row("BurnAmount");
            if row != RowId::MISSING {
                providers.regular = providers.regular.max(spec.kit_base.row_value(row) / 100.0);
            }
        }
        for value in getlist(input, "traits")? {
            let effect = dict_of(&value)?;
            if gets(&effect, "api", "")? == "DA_18_Inferno" {
                if let Some(values) = getvecf(&effect, "burnOnHit")? {
                    if let Some(&rate) = values.first() { providers.inferno = providers.inferno.max(rate); }
                }
            }
        }
        for value in getlist(input, "items")?.into_iter().chain(getlist(input, "traits")?) {
            let effect = dict_of(&value)?;
            // Caustic reduces the shared target's armor and MR, just like
            // item-provided on-hit reductions. Its local driver still keeps
            // the hit timing; board coverage uses the same opening-uptime
            // approximation as the other shared reduction providers.
            if let Some(values) = getvecf(&effect, "caustic")? {
                if let Some(&rate) = values.first() {
                    providers.sunder = providers.sunder.max(rate);
                    providers.shred = providers.shred.max(rate);
                }
            }
            for (name, target) in [("sunder", &mut providers.sunder), ("shred", &mut providers.shred)] {
                *target = target.max(getf(&effect, &format!("{name}Aura"), 0.0)?);
                if let Some(values) = getvecf(&effect, &format!("{name}OnHit"))? {
                    if let Some(&rate) = values.first() { *target = target.max(rate); }
                }
            }
        }
        let items = spec.items.iter().collect::<Vec<_>>();
        let fx = build_fx_for(&spec, &items);
        providers.sunder = providers.sunder.max(fx.sunder_aura);
        providers.shred = providers.shred.max(fx.shred_aura);
        if let Some((rate, _)) = fx.burn_aura { providers.regular = providers.regular.max(rate); }
        fraction(providers.sunder, "shared Sunder")?;
        fraction(providers.shred, "shared Shred")?;
        let form = fx.form;
        let frontline = matches!(spec.objective_for(form), Objective::Tank | Objective::Fighter)
            || spec.kind_for(form) == Kind::Assassin;
        Ok(Self { spec, providers, form, frontline })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct ProfileKey {
    unit: usize,
    scenario: usize,
    incoming: u64,
    sunder: u64,
    shred: u64,
    item_burn: bool,
    inferno_burn: bool,
    source_mask: u8,
}

struct Opening {
    response: ResponseSample,
    targetable: bool,
}

#[derive(Clone, PartialEq, Eq, Hash)]
struct ResultKey {
    units: Vec<usize>,
    carry: usize,
    tank: usize,
    details: bool,
}

#[derive(Clone, Copy, Default)]
struct Response([f64; RESPONSE_FIELDS]);

fn mixed_residual(sample: &ResponseSample, share: f64) -> f64 {
    let mut total = 0.0;
    for &(physical, magic) in &sample.residual_pools {
        if share == 1.0 { total += physical; }
        else if share == 0.0 { total += magic; }
        else if physical > 0.0 && magic > 0.0 {
            total += 1.0 / (share / physical + (1.0 - share) / magic);
        }
    }
    total
}

impl Response {
    fn from_sample(sample: &ResponseSample, share: f64) -> Self {
        Self([sample.damage, sample.incoming_spent, sample.denied, mixed_residual(sample, share),
              sample.self_heal, sample.self_shield, sample.ally_heal_potential,
              sample.ally_shield_potential, sample.alive_time, sample.unit_alive_time,
              sample.casts as f64])
    }

}

#[derive(Clone, Copy, Default)]
struct Metrics {
    ehp: f64,
    dps: f64,
    protection: f64,
    capacity: f64,
    score: f64,
}

impl Metrics {
    fn capacity(ehp: f64, dps: f64, pressure: f64) -> PyResult<Self> {
        finite_nonnegative(ehp, "frontline EHP")?;
        finite_nonnegative(dps, "team DPS")?;
        positive(pressure, "incoming pressure")?;
        let score = ehp * dps;
        if !score.is_finite() { return Err(invalid("capacity score overflowed")); }
        Ok(Self { ehp, dps, protection: ehp / pressure, capacity: score / pressure, score })
    }

    fn summarize(rows: &[ProfileResult]) -> PyResult<Self> {
        let ehp = geometric_mean(rows.iter().map(|row| row.metrics.ehp))?;
        let dps = geometric_mean(rows.iter().map(|row| row.metrics.dps))?;
        Ok(Self { ehp, dps,
                  capacity: geometric_mean(rows.iter().map(|row| row.metrics.capacity))?,
                  protection: geometric_mean(rows.iter().map(|row| row.metrics.protection))?,
                  score: ehp * dps })
    }

    fn put(&self, row: &Bound<'_, PyDict>) -> PyResult<()> {
        row.set_item("frontlineEhp", self.ehp)?;
        row.set_item("damageDps", self.dps)?;
        row.set_item("protectionTime", self.protection)?;
        row.set_item("damageCapacity", self.capacity)?;
        row.set_item("theoryScore", self.score)?;
        Ok(())
    }
}

struct ProfileResult {
    metrics: Metrics,
    opening_ehp: f64,
    window: f64,
    planned_window: f64,
    collapsed: bool,
    incoming_budget: f64,
    unspent: f64,
    spent: f64,
    denied: f64,
    target_order: Vec<usize>,
    initial_source_targets: [Option<usize>; 3],
}

#[derive(Clone, Copy, Default)]
struct Contribution {
    damage: f64,
    share: f64,
    measured_dps: f64,
    alive: f64,
    taken: f64,
    healing: f64,
    shielding: f64,
    ally_healing: f64,
    ally_shielding: f64,
    casts: f64,
}

impl Contribution {
    fn add(&mut self, response: Response, window: f64, dps: f64, count: f64) {
        self.damage += response.0[DAMAGE] / count;
        self.share += if dps != 0.0 { response.0[DAMAGE] / window / dps / count } else { 0.0 };
        self.measured_dps += response.0[DAMAGE] / window / count;
        self.alive += response.0[UNIT_ALIVE] / count;
        self.taken += response.0[SPENT] / count;
        self.healing += response.0[HEAL] / count;
        self.shielding += response.0[SHIELD] / count;
        self.ally_healing += response.0[ALLY_HEAL] / count;
        self.ally_shielding += response.0[ALLY_SHIELD] / count;
        self.casts += response.0[CASTS] / count;
    }
}

struct ScoredResult {
    units: Vec<usize>,
    rows: Vec<ProfileResult>,
    metrics: Metrics,
    contributions: Option<Vec<Contribution>>,
    item_budget: usize,
    sunder: f64,
    shred: f64,
    item_owner: Option<usize>,
    inferno_owner: Option<usize>,
}

#[derive(Default)]
struct Counters {
    registered: u64,
    opening_measured: u64,
    opening_reused: u64,
    allocations_measured: u64,
    allocations_reused: u64,
    profiles_evaluated: u64,
}

#[pyclass(module = "lol_tft")]
pub(crate) struct TheoryScorer {
    scenarios: Vec<Scenario>,
    max_window: f64,
    registered: Vec<Registered>,
    openings: Cache<ProfileKey, Opening>,
    conditioned: Cache<ProfileKey, CellSpec>,
    results: Cache<ResultKey, ScoredResult>,
    counters: Counters,
}

#[pymethods]
impl TheoryScorer {
    #[new]
    #[pyo3(signature = (scenarios, max_window=2047.0, cache_limit=4096))]
    fn new(scenarios: &Bound<'_, PyAny>, max_window: f64, cache_limit: usize) -> PyResult<Self> {
        positive(max_window, "max_window")?;
        if cache_limit == 0 || cache_limit > 1_000_000 {
            return Err(invalid("cache_limit must be from 1 to 1000000"));
        }
        if max_window > 2047.0 {
            return Err(invalid("maximum supported measurement window is 2047 seconds"));
        }
        let mut scenarios = scenarios.try_iter()?.map(|value| Scenario::parse(&dict_of(&value?)?))
            .collect::<PyResult<Vec<_>>>()?;
        if scenarios.is_empty() { return Err(invalid("capacity evaluation needs pressure profiles")); }
        // Incoming amounts, their mix and control are scheduler inputs,
        // not immutable actor stats. Reuse one prepared seed across them.
        for index in 0..scenarios.len() {
            let row = &scenarios[index];
            scenarios[index].prepared_index = scenarios.iter().position(|other|
                other.target_count == row.target_count && other.target_hp == row.target_hp
                && other.armor == row.armor && other.mr == row.mr && other.wound == row.wound).unwrap();
        }
        Ok(Self { scenarios, max_window,
                  registered: Vec::new(), openings: Cache::new(cache_limit),
                  conditioned: Cache::new(cache_limit),
                  results: Cache::new(cache_limit.min(RESULT_CACHE_LIMIT)),
                  counters: Counters::default() })
    }

    fn register(&mut self, spec: &Bound<'_, PyDict>) -> PyResult<usize> {
        let registered = Registered::parse(spec)?;
        let id = self.registered.len();
        self.registered.push(registered);
        self.counters.registered += 1;
        Ok(id)
    }

    #[pyo3(signature = (allocations, carry_index, tank_index, details=false))]
    fn evaluate_many<'py>(&mut self, py: Python<'py>, allocations: Vec<Vec<usize>>,
                         carry_index: usize, tank_index: usize, details: bool) -> PyResult<Bound<'py, PyList>> {
        for allocation in &allocations { self.validate(allocation, carry_index, tank_index)?; }
        let results = py.detach(|| allocations.iter()
            .map(|allocation| self.evaluate(allocation, carry_index, tank_index, details))
            .collect::<PyResult<Vec<_>>>())?;
        let out = PyList::empty(py);
        let mut converted: HashMap<usize, Bound<'py, PyDict>> = HashMap::new();
        for result in results {
            let pointer = Arc::as_ptr(&result) as usize;
            if let Some(row) = converted.get(&pointer) { out.append(row)?; }
            else {
                let row = self.to_py(py, &result)?;
                out.append(&row)?;
                converted.insert(pointer, row);
            }
        }
        Ok(out)
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        for (name, value) in [
            ("registeredSpecs", self.counters.registered),
            ("unitOpeningsMeasured", self.counters.opening_measured),
            ("unitOpeningsReused", self.counters.opening_reused),
            ("sharedMeasurements", self.counters.profiles_evaluated),
            ("teamAllocationsSimulated", self.counters.allocations_measured),
            ("teamAllocationsReused", self.counters.allocations_reused),
            ("theoryProfilesEvaluated", self.counters.profiles_evaluated),
        ] { out.set_item(name, value)?; }
        Ok(out)
    }
}

impl TheoryScorer {
    fn validate(&self, allocation: &[usize], carry: usize, tank: usize) -> PyResult<()> {
        if allocation.is_empty() || carry >= allocation.len() || tank >= allocation.len() || carry == tank {
            return Err(invalid("capacity needs a roster and distinct valid carry/tank indexes"));
        }
        let mut apis = HashSet::new();
        let mut slots = 0;
        for &id in allocation {
            let unit = self.registered.get(id).ok_or_else(|| invalid("allocation contains an unregistered unit ID"))?;
            if !apis.insert(&unit.spec.unit.api) { return Err(invalid("capacity requires distinct champion APIs")); }
            slots += if unit.spec.unit.api == "TFT18_ElderDragon" { 2 } else { 1 };
        }
        if slots > MAX_BOARD_SLOTS { return Err(invalid("capacity roster exceeds nine team slots")); }
        Ok(())
    }

    fn provider_context(&self, allocation: &[usize], carry: usize, tank: usize)
        -> (f64, f64, [Option<usize>; 2]) {
        let mut sunder = 0.0f64;
        let mut shred = 0.0f64;
        let mut owners: [Option<usize>; 2] = [None, None];
        for &id in allocation {
            let unit = &self.registered[id];
            sunder = sunder.max(unit.providers.sunder);
            shred = shred.max(unit.providers.shred);
            for (channel, rate) in [unit.providers.regular, unit.providers.inferno].into_iter().enumerate() {
                if rate <= 0.0 { continue; }
                let better = match owners[channel] {
                    None => true,
                    Some(previous) => {
                        let other = &self.registered[previous];
                        let previous_rate = if channel == 0 { other.providers.regular } else { other.providers.inferno };
                        (-rate, id != carry, id != tank, &unit.spec.unit.api)
                            < (-previous_rate, previous != carry, previous != tank, &other.spec.unit.api)
                    }
                };
                if better { owners[channel] = Some(id); }
            }
        }
        (sunder, shred, owners)
    }

    fn conditioned_spec(&self, key: ProfileKey) -> CellSpec {
        let mut spec = self.registered[key.unit].spec.clone();
        let scenario = &self.scenarios[key.scenario];
        let incoming = f64::from_bits(key.incoming);
        let pulse = incoming * 1.0 / scenario.target_count as f64;
        spec.pressure = incoming > 0.0;
        spec.immortal = true;
        spec.crit_ev = 1.0;
        spec.enemy_debuffs = EnemyDebuffs { wound: scenario.wound, sunder: 0.0, shred: 0.0 };
        spec.target_debuffs = TargetDebuffs { sunder: f64::from_bits(key.sunder), shred: f64::from_bits(key.shred) };
        // The probes keep the positions the cell spec gave them, so an
        // ability with a stated radius measures this board too instead of
        // covering every target that counts toward the score.
        let positions: Vec<Option<(i64, i64)>> =
            spec.dummies.iter().map(|dummy| dummy.position).collect();
        spec.dummies = (0..scenario.target_count).map(|index| {
            let start = 1.0 * (index + 1) as f64 / scenario.target_count as f64;
            DummySpec { hp: scenario.target_hp, armor: scenario.armor, mr: scenario.mr,
                is_tank: true, nearby: true,
                position: positions.get(index).copied().flatten(), ad: pulse * scenario.physical_share, as_: 1.0,
                ability: pulse * (1.0 - scenario.physical_share), phys_share: 0.0,
                mana_max: 0.0, mana_start: 0.0, mana_per_attack: 0.0, mana_from_damage: false,
                attack_start: Some(start), cast_start: Some(start), streams: 1,
                cast_interval: if scenario.physical_share < 1.0 && pulse > 0.0 { 1.0 } else { 0.0 } }
        }).collect();
        if !key.item_burn {
            for item in spec.items.iter_mut().chain(spec.pool.iter_mut()) {
                item.burn_on_hit = None;
                item.burn_aura = None;
                item.burn_aura_by_range = None;
            }
            let duration = match spec.unit.api.as_str() {
                "TFT18_Cinderling" => Some("BurnDuration"),
                "TFT18_Brambleback" => Some("TraitBurnDuration"),
                _ => None,
            };
            if let Some(duration) = duration {
                let suppress = |kit: &mut Kit| {
                    kit.set_existing_row("BurnAmount", 0.0);
                    kit.set_existing_row(duration, 0.0);
                };
                suppress(&mut spec.kit_base);
                if let Some(kit) = &mut spec.kit_ad { suppress(kit); }
                if let Some(kit) = &mut spec.kit_ap { suppress(kit); }
            }
        }
        if !key.inferno_burn {
            for effect in &mut spec.traits {
                if effect.api == "DA_18_Inferno" { effect.burn_on_hit = None; }
            }
        }
        spec
    }

    fn opening(&mut self, mut key: ProfileKey, share: f64) -> PyResult<(f64, bool)> {
        // Opening state depends on defenses and assigned attackers, not on
        // the later pressure cadence, orientation or physical/magic mix.
        key.scenario = self.scenarios[key.scenario].prepared_index;
        key.incoming = 0.0_f64.to_bits();
        let opening = if let Some(opening) = self.openings.get(&key) {
            self.counters.opening_reused += 1;
            opening
        } else {
            let spec = self.prepared_spec(key);
            let (response, targetable) = crate::theory_fight::opening_targeted_info(
                &spec, self.registered[key.unit].frontline, key.source_mask)?;
            self.counters.opening_measured += 1;
            self.openings.insert(key, Opening { response, targetable })
        };
        let ehp = mixed_residual(&opening.response, share);
        finite_nonnegative(ehp, "opening frontline EHP")?;
        Ok((ehp, opening.targetable))
    }

    fn prepared_spec(&mut self, mut key: ProfileKey) -> Arc<CellSpec> {
        key.scenario = self.scenarios[key.scenario].prepared_index;
        key.incoming = 0.0_f64.to_bits();
        key.source_mask = 0;
        if let Some(spec) = self.conditioned.get(&key) { return spec; }
        let spec = self.conditioned_spec(key);
        self.conditioned.insert(key, spec)
    }

    fn evaluate(&mut self, allocation: &[usize], carry_index: usize, tank_index: usize, details: bool)
        -> PyResult<Arc<ScoredResult>> {
        let carry = allocation[carry_index];
        let tank = allocation[tank_index];
        let mut sorted = allocation.to_vec();
        sorted.sort_by(|&a, &b| self.registered[a].spec.unit.api.cmp(&self.registered[b].spec.unit.api));
        let key = ResultKey { units: sorted, carry, tank, details };
        if let Some(result) = self.results.get(&key) {
            self.counters.allocations_reused += 1;
            return Ok(result);
        }
        let (sunder, shred, owners) = self.provider_context(allocation, carry, tank);
        let fronts = allocation.iter().map(|&id| self.registered[id].frontline).collect::<Vec<_>>();
        let front_count = fronts.iter().filter(|&&front| front).count();
        if front_count == 0 { return Err(invalid("theoretical capacity requires a frontline")); }
        let base_profiles = allocation.iter().map(|&id| ProfileKey {
            unit: id, scenario: 0, incoming: 0.0_f64.to_bits(),
            sunder: sunder.to_bits(), shred: shred.to_bits(),
            item_burn: owners[0] == Some(id), inferno_burn: owners[1] == Some(id), source_mask: 0,
        }).collect::<Vec<_>>();
        // Choose the formation once per allocation. Unfocused 50/50 opening
        // EHP avoids changing placement to exploit a particular damage mix
        // or counting Gargoyle's assignment bonus in the assignment itself.
        let mut neutral = Vec::with_capacity(front_count);
        for (index, &profile) in base_profiles.iter().enumerate() {
            if fronts[index] { neutral.push((index, self.opening(profile, 0.5)?.0)); }
        }
        neutral.sort_by(|a, b| (a.0 != tank_index).cmp(&(b.0 != tank_index))
            .then_with(|| b.1.total_cmp(&a.1))
            .then_with(|| self.registered[allocation[a.0]].spec.unit.api
                .cmp(&self.registered[allocation[b.0]].spec.unit.api)));
        let priority = neutral.iter().map(|&(index, _)| index).collect::<Vec<_>>();
        let mut rows = Vec::with_capacity(self.scenarios.len());
        let mut contributions = vec![Contribution::default(); allocation.len()];
        for scenario_index in 0..self.scenarios.len() {
            let pressure = self.scenarios[scenario_index].pressure;
            let share = self.scenarios[scenario_index].physical_share;
            let mut profiles = base_profiles.iter().enumerate().map(|(index, &profile)| ProfileKey {
                scenario: scenario_index,
                incoming: (if fronts[index] { pressure } else { 0.0 }).to_bits(), ..profile
            }).collect::<Vec<_>>();
            let mut target_order = priority.clone();
            if self.scenarios[scenario_index].secondary_first && target_order.len() > 1 {
                target_order.swap(0, 1);
            }
            let mut targetable = vec![false; allocation.len()];
            for (index, &profile) in profiles.iter().enumerate() {
                if fronts[index] { targetable[index] = self.opening(profile, share)?.1; }
            }
            let initial_source_targets = crate::theory_fight::initial_targets(&target_order, &targetable);
            for (source, target) in initial_source_targets.iter().enumerate() {
                if let Some(index) = target { profiles[*index].source_mask |= 1 << source; }
            }
            let mut opening_values = Vec::with_capacity(front_count);
            for (index, &profile) in profiles.iter().enumerate() {
                if fronts[index] {
                    let (ehp, focused_targetable) = self.opening(profile, share)?;
                    if focused_targetable != targetable[index] {
                        return Err(invalid("opening targetability changed with its assigned sources"));
                    }
                    opening_values.push(ehp);
                }
            }
            let opening_ehp = accurate_sum(opening_values);
            let planned_window = opening_ehp / pressure;
            if !planned_window.is_finite() || planned_window <= 0.0 || planned_window > self.max_window {
                return Err(invalid("derived exposure window exceeds supported measurement range; no cutoff score was substituted"));
            }
            let specs = profiles.iter().map(|&profile| self.prepared_spec(profile)).collect::<Vec<_>>();
            let scenario = &self.scenarios[scenario_index];
            let measured_team = crate::theory_fight::measure(&specs, &fronts, planned_window,
                pressure, scenario.physical_share, scenario.control_interval, scenario.control_duration, &target_order)?;
            if measured_team.initial_source_targets != initial_source_targets {
                return Err(invalid("measurement opening targets disagree with the planned EHP targets"));
            }
            let window = measured_team.elapsed;
            positive(window, "observed protection window")?;
            let measured = measured_team.samples.iter().map(|sample|
                Response::from_sample(sample, scenario.physical_share)).collect::<Vec<_>>();
            for response in &measured {
                if response.0.iter().any(|value| !value.is_finite() || *value < 0.0) {
                    return Err(invalid("unit response requires finite nonnegative metrics"));
                }
            }
            let ehp = accurate_sum(measured.iter().zip(allocation).filter_map(|(response, &id)| {
                self.registered[id].frontline.then_some(response.0[SPENT] + response.0[DENIED] + response.0[RESIDUAL])
            }));
            let spent = accurate_sum(measured.iter().zip(&fronts).filter_map(|(response, &front)|
                front.then_some(response.0[SPENT])));
            let denied = accurate_sum(measured.iter().zip(&fronts).filter_map(|(response, &front)|
                front.then_some(response.0[DENIED])));
            let dps = accurate_sum(measured.iter().map(|response| response.0[DAMAGE])) / window;
            let metrics = Metrics::capacity(ehp, dps, pressure)?;
            rows.push(ProfileResult { metrics, opening_ehp, window, planned_window,
                collapsed: measured_team.collapsed, incoming_budget: measured_team.incoming_budget,
                unspent: measured_team.unspent, spent, denied,
                target_order: measured_team.target_order,
                initial_source_targets: measured_team.initial_source_targets });
            if details {
                for (contribution, response) in contributions.iter_mut().zip(measured) {
                    contribution.add(response, window, dps, self.scenarios.len() as f64);
                }
            }
        }
        let metrics = Metrics::summarize(&rows)?;
        self.counters.allocations_measured += 1;
        self.counters.profiles_evaluated += rows.len() as u64;
        let result = ScoredResult { units: allocation.to_vec(), rows, metrics,
            contributions: details.then_some(contributions),
            item_budget: allocation.iter().map(|&id| self.registered[id].spec.items.len()).sum(),
            sunder, shred, item_owner: owners[0], inferno_owner: owners[1] };
        Ok(self.results.insert(key, result))
    }

    fn to_py<'py>(&self, py: Python<'py>, result: &ScoredResult) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        let metrics = PyDict::new(py);
        result.metrics.put(&metrics)?;
        out.set_item("metrics", metrics)?;
        let rows = PyList::empty(py);
        for (scenario, profile) in self.scenarios.iter().zip(&result.rows) {
            let row = scenario.source.bind(py).copy()?;
            profile.metrics.put(&row)?;
            row.set_item("score", profile.metrics.score)?;
            row.set_item("openingFrontlineEhp", profile.opening_ehp)?;
            row.set_item("measurementWindow", profile.window)?;
            row.set_item("plannedMeasurementWindow", profile.planned_window)?;
            row.set_item("frontlineCollapsed", profile.collapsed)?;
            row.set_item("incomingBudget", profile.incoming_budget)?;
            row.set_item("unspentPressure", profile.unspent)?;
            row.set_item("spentPressure", profile.spent)?;
            row.set_item("deniedPressure", profile.denied)?;
            if result.contributions.is_some() {
                row.set_item("pressureTargetOrder", profile.target_order.iter().map(|&index|
                    self.registered[result.units[index]].spec.unit.api.as_str()).collect::<Vec<_>>())?;
                row.set_item("initialPressureTargets", profile.initial_source_targets.map(|index|
                    index.map(|index| self.registered[result.units[index]].spec.unit.api.as_str())))?;
            }
            rows.append(row)?;
        }
        out.set_item("scenarios", rows)?;
        out.set_item("profileCount", result.rows.len())?;
        out.set_item("itemBudget", result.item_budget)?;
        let shared = PyDict::new(py);
        shared.set_item("sunder", result.sunder)?;
        shared.set_item("shred", result.shred)?;
        shared.set_item("itemBurnHolder", result.item_owner.map(|id| self.registered[id].spec.unit.api.as_str()))?;
        shared.set_item("infernoBurnHolder", result.inferno_owner.map(|id| self.registered[id].spec.unit.api.as_str()))?;
        out.set_item("sharedUtility", shared)?;
        let sensitivity = PyDict::new(py);
        sensitivity.set_item("lowestScore", result.rows.iter().map(|row| row.metrics.score).fold(f64::INFINITY, f64::min))?;
        sensitivity.set_item("highestScore", result.rows.iter().map(|row| row.metrics.score).fold(f64::NEG_INFINITY, f64::max))?;
        out.set_item("sensitivity", sensitivity)?;
        let units = PyDict::new(py);
        if let Some(contributions) = &result.contributions {
            for (&id, contribution) in result.units.iter().zip(contributions) {
                let registered = &self.registered[id];
                let unit = PyDict::new(py);
                unit.set_item("damage", contribution.damage)?;
                unit.set_item("measuredDps", contribution.measured_dps)?;
                unit.set_item("aliveTime", contribution.alive)?;
                unit.set_item("damageTaken", contribution.taken)?;
                unit.set_item("healing", contribution.healing)?;
                unit.set_item("shielding", contribution.shielding)?;
                unit.set_item("allyHealing", contribution.ally_healing)?;
                unit.set_item("allyShielding", contribution.ally_shielding)?;
                unit.set_item("casts", contribution.casts)?;
                unit.set_item("dps", contribution.share * result.metrics.dps)?;
                unit.set_item("kind", match registered.spec.unit.kind {
                    Kind::Assassin => "Assassin", Kind::Fighter => "Fighter", Kind::Marksman => "Marksman",
                    Kind::Caster => "Caster", Kind::Tank => "Tank", Kind::Specialist => "Specialist",
                })?;
                unit.set_item("form", registered.form.map(|form| form.name()))?;
                unit.set_item("frontline", registered.frontline)?;
                units.set_item(&registered.spec.unit.api, unit)?;
            }
        }
        out.set_item("units", units)?;
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accurate_sum_preserves_low_order_terms_and_half_even_rounding() {
        assert_eq!(accurate_sum([1e16, 1.0, -1e16]), 1.0);
        assert_eq!(accurate_sum([1e16, 1.0, 1e-16]), 10000000000000002.0);
        assert_eq!(accurate_sum([]), 0.0);
    }

    #[test]
    fn cache_eviction_keeps_recent_hits_and_bounds_the_access_log() {
        let mut cache = Cache::new(2);
        cache.insert(1, 10);
        cache.insert(2, 20);
        for _ in 0..100 { assert_eq!(*cache.get(&1).unwrap(), 10); }
        assert!(cache.accesses.len() <= 8);
        cache.insert(3, 30);
        assert!(cache.get(&2).is_none());
        assert_eq!(*cache.get(&1).unwrap(), 10);
        assert_eq!(*cache.get(&3).unwrap(), 30);
    }
}
