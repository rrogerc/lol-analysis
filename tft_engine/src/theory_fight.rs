//! Persistent allied actors measured under one conserved generic pressure budget.
//!
//! Enemy targets remain immortal per-actor damage probes. Only the incoming
//! budget, target control/resistance state and allied control schedule are shared; there is
//! no opposing champion board or symmetric combat bridge.
use std::cell::RefCell;
use std::collections::{HashMap, HashSet, VecDeque};
use std::rc::Rc;
use std::sync::Arc;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::driver::Driver;
use crate::fight::{Dummy, Fight, ResponseSample, SharedSleep, Sheet, SleepTracker,
                   SleepWake, TeamClock, TeamEffect, FAR};
use crate::fx::{build_fx_for, Fx};
use crate::kit::DType;
use crate::pyget::dict_of;
use crate::spec::CellSpec;
use crate::with_driver;

pub(crate) const PRESSURE_INTERVAL: f64 = 0.5;
// Outgoing area coverage must not change the number of incoming sources.
// A local target represents the correspondingly numbered pressure source
// for control; an absent local target cannot have its source stunned.
pub(crate) const INCOMING_SOURCE_COUNT: usize = 3;
const PREPARED_LIMIT: usize = 4096;
const MAX_WINDOW: f64 = 2047.0;
const EPS: f64 = 1e-9;

fn invalid(message: impl Into<String>) -> PyErr { PyValueError::new_err(message.into()) }

/// One target's timed percentage Sunder and Shred as a `Dummy` holds them,
/// (strength, expiry) each. A reduction is applied by the ally whose hit
/// carries it (Caustic, Last Whisper, Void Staff, a driver's own) to its own
/// copy of the target; `share_reductions` hands the change to every ally
/// before anyone else acts at that instant, so a reduction helps exactly the
/// damage that lands on that target while it lasts. Auras stay standing
/// coverage (`baseline_*`).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
struct Reductions {
    sunder: (f64, f64),
    shred: (f64, f64),
}

impl Reductions {
    fn of(target: &Dummy) -> Self {
        Self { sunder: (target.sunder, target.sunder_until), shred: (target.shred, target.shred_until) }
    }

    /// Fold one ally's view in the way `Fight::sunder` folds a second
    /// application on one target: the strongest strength still running,
    /// lasting to the latest expiry. An expired view adds nothing.
    fn merge(&mut self, view: Reductions, time: f64) {
        for (shared, local) in [(&mut self.sunder, view.sunder), (&mut self.shred, view.shred)] {
            if local.1 > time {
                shared.0 = shared.0.max(local.0);
                shared.1 = shared.1.max(local.1);
            }
        }
    }
}

pub(crate) struct Measurement {
    pub samples: Vec<ResponseSample>,
    pub elapsed: f64,
    pub collapsed: bool,
    pub incoming_budget: f64,
    pub unspent: f64,
    pub target_order: Vec<usize>,
    pub initial_source_targets: [Option<usize>; INCOMING_SOURCE_COUNT],
}

trait Actor {
    fn sleep_tracker(&mut self, tracker: SharedSleep, source: usize);
    fn wake_sleep(&mut self, time: f64, wake: SleepWake);
    fn spellweaver_casts(&mut self) -> usize;
    fn allied_spellweaver_cast(&mut self, time: f64);
    fn next(&self) -> f64;
    fn events(&mut self, time: f64) -> bool;
    fn attack(&mut self, time: f64, due: bool);
    fn holding(&self) -> bool;
    fn targetable(&self, time: f64) -> bool;
    fn focus(&mut self, source_mask: u8);
    fn control(&mut self, time: f64, duration: f64);
    fn receive(&mut self, amount: f64, dtype: DType, source: usize) -> f64;
    fn deny(&mut self, amount: f64);
    fn collect_stuns(&self, until: &mut [f64]);
    fn sync_stuns(&mut self, until: &[f64]);
    fn collect_flat(&self, flat: &mut [(f64, f64)]);
    fn sync_flat(&mut self, flat: &[(f64, f64)]);
    fn changed_flat(&self) -> bool;
    fn collect_reductions(&self, shared: &mut [Reductions], time: f64);
    fn sync_reductions(&mut self, shared: &[Reductions]);
    fn changed_reductions(&self) -> bool;
    fn observe(&mut self, time: f64) -> ResponseSample;
}

trait Seed {
    fn spawn<'a>(&self, prepared: &'a Prepared, front: bool, window: f64, source_mask: u8) -> Box<dyn Actor + 'a>;
}

fn apply_focus<D: Driver>(fight: &mut Fight<D>, source_mask: u8) {
    for target in &mut fight.targets { target.targeting_streams = 0; }
    let count = fight.targets.len();
    for source in 0..INCOMING_SOURCE_COUNT {
        if source_mask & (1 << source) != 0 {
            // A spread outgoing probe can represent multiple incoming
            // owners. Keep their global IDs and add their stream counts.
            fight.targets[source % count].targeting_streams += 1;
        }
    }
}

struct TypedSeed<D: Driver>(D);
impl<D: Driver + 'static> Seed for TypedSeed<D> {
    fn spawn<'a>(&self, prepared: &'a Prepared, front: bool, window: f64, source_mask: u8) -> Box<dyn Actor + 'a> {
        let spec = &prepared.spec;
        let mut fight = Fight::new(spec, spec.kit_for(prepared.fx.form), prepared.sheet.clone(),
                                   prepared.fx.clone(), prepared.targets.clone(), self.0.clone());
        fight.duration = window;
        fight.shared_casts = prepared.fx.spellweaver_ap_per_cast > 0.0;
        fight.pressure = front;
        // Pressure is assigned by this scheduler, independently of the
        // reusable spec's dummy timers. Preserve the declared antiheal on
        // exposed actors even when that immutable spec has pressure=false.
        fight.enemy_debuffs = if front { spec.enemy_debuffs } else { Default::default() };
        // Local burns and damage accounting must remain active. The generic
        // incoming packets are supplied externally, never by dummy timers.
        fight.team_mode = false;
        let source_mask = if front { source_mask } else { 0 };
        apply_focus(&mut fight, source_mask);
        D::init(&mut fight);
        let clock = TeamClock::new(&fight.fx);
        let flat_baseline = vec![(0.0, 0.0); fight.targets.len()];
        let reduction_baseline = vec![Reductions::default(); fight.targets.len()];
        Box::new(Champion { fight, clock, flat_baseline, reduction_baseline, source_mask })
    }
}

struct Champion<'a, D: Driver> {
    fight: Fight<'a, D>, clock: TeamClock, flat_baseline: Vec<(f64, f64)>,
    reduction_baseline: Vec<Reductions>, source_mask: u8,
}
impl<D: Driver> Actor for Champion<'_, D> {
    fn sleep_tracker(&mut self, tracker: SharedSleep, source: usize) {
        self.fight.sleeps = Some(tracker);
        self.fight.sleep_source = source;
        self.fight.external_sleep = true;
    }
    fn wake_sleep(&mut self, time: f64, wake: SleepWake) {
        self.fight.t = time;
        self.fight.wake_sleep(wake);
    }
    fn spellweaver_casts(&mut self) -> usize {
        self.fight.team_effects.drain(..)
            .filter(|effect| matches!(effect, TeamEffect::Cast(_, true))).count()
    }
    fn allied_spellweaver_cast(&mut self, time: f64) {
        self.fight.t = time;
        self.fight.allied_spellweaver_cast();
    }
    fn next(&self) -> f64 { self.fight.team_next_event(&self.clock) }
    fn events(&mut self, time: f64) -> bool { self.fight.theory_events(&mut self.clock, time) }
    fn attack(&mut self, time: f64, due: bool) { self.fight.theory_attack(time, due); }
    fn holding(&self) -> bool { self.fight.holding() }
    fn targetable(&self, time: f64) -> bool {
        self.fight.holding() && (!self.fight.alive_unit || time >= self.fight.untargetable_until)
    }
    fn focus(&mut self, source_mask: u8) {
        if self.source_mask == source_mask { return; }
        apply_focus(&mut self.fight, source_mask);
        self.source_mask = source_mask;
    }
    fn control(&mut self, time: f64, duration: f64) {
        self.fight.t = time;
        if self.fight.alive_unit && !self.fight.combat_cc_immune() {
            self.fight.combat_stunned_until = self.fight.combat_stunned_until.max(time + duration);
        }
    }
    fn receive(&mut self, amount: f64, dtype: DType, source: usize) -> f64 {
        self.fight.theory_receive(amount, dtype, source)
    }
    fn deny(&mut self, amount: f64) { self.fight.denied += amount; }
    fn collect_stuns(&self, until: &mut [f64]) {
        for (shared, target) in until.iter_mut().zip(&self.fight.targets) {
            *shared = shared.max(target.stunned_until);
        }
    }
    fn sync_stuns(&mut self, until: &[f64]) {
        for (target, shared) in self.fight.targets.iter_mut().zip(until) {
            target.stunned_until = target.stunned_until.max(*shared);
        }
    }
    fn collect_flat(&self, flat: &mut [(f64, f64)]) {
        for ((shared, target), baseline) in flat.iter_mut().zip(&self.fight.targets).zip(&self.flat_baseline) {
            // Driver writes are increments relative to its last synchronized
            // view. Summing complete views would reapply old reductions once
            // per ally/tick; max would lose distinct providers' increments.
            shared.0 += target.armor_flat - baseline.0;
            shared.1 += target.mr_flat - baseline.1;
        }
    }
    fn changed_flat(&self) -> bool {
        self.fight.targets.iter().zip(&self.flat_baseline).any(|(target, baseline)|
            target.armor_flat != baseline.0 || target.mr_flat != baseline.1)
    }
    fn changed_reductions(&self) -> bool {
        // The flag is what runs; comparing every target is the slow truth it
        // stands for (a driver writing a reduction without it would show here).
        debug_assert!(self.fight.reductions_changed
            || self.fight.targets.iter().zip(&self.reduction_baseline).all(|(target, baseline)|
                Reductions::of(target) == *baseline),
            "a timed Sunder/Shred changed without setting Fight::reductions_changed");
        self.fight.reductions_changed
    }
    fn sync_flat(&mut self, flat: &[(f64, f64)]) {
        for ((target, baseline), shared) in self.fight.targets.iter_mut().zip(&mut self.flat_baseline).zip(flat) {
            target.armor_flat = shared.0;
            target.mr_flat = shared.1;
            *baseline = *shared;
        }
    }
    fn collect_reductions(&self, shared: &mut [Reductions], time: f64) {
        for (shared, target) in shared.iter_mut().zip(&self.fight.targets) {
            shared.merge(Reductions::of(target), time);
        }
    }
    fn sync_reductions(&mut self, shared: &[Reductions]) {
        for ((target, baseline), shared) in self.fight.targets.iter_mut().zip(&mut self.reduction_baseline).zip(shared) {
            (target.sunder, target.sunder_until) = shared.sunder;
            (target.shred, target.shred_until) = shared.shred;
            *baseline = *shared;
        }
        self.fight.reductions_changed = false;
    }
    fn observe(&mut self, time: f64) -> ResponseSample { self.fight.response_sample(time) }
}

struct Prepared {
    // Owning the Arc also prevents address reuse while this seed is cached.
    spec: Arc<CellSpec>,
    fx: Fx,
    sheet: Sheet,
    targets: Vec<Dummy>,
    seed: Box<dyn Seed>,
}

impl Prepared {
    fn new(spec: Arc<CellSpec>) -> PyResult<Self> {
        if spec.unit.api.is_empty() || !(1..=3).contains(&spec.star)
            || spec.items.len() > 3 || spec.dummies.is_empty() || spec.dummies.len() > 8 {
            return Err(invalid("theoretical actor needs a champion, star1..3, up to3items and1..8targets"));
        }
        if !crate::drivers::NAMES.contains(&spec.driver.as_str()) {
            return Err(invalid(format!("unknown theoretical driver {:?}", spec.driver)));
        }
        if spec.dummies.iter().any(|target| !target.hp.is_finite() || target.hp <= 0.0
            || !target.armor.is_finite() || target.armor < 0.0
            || !target.mr.is_finite() || target.mr < 0.0) {
            return Err(invalid("theoretical targets need positive finite health and nonnegative finite resists"));
        }
        let items = spec.items.iter().collect::<Vec<_>>();
        let fx = build_fx_for(&spec, &items);
        let kit = spec.kit_for(fx.form);
        let sheet = Sheet::new(&spec, kit, &fx);
        if !sheet.max_hp.is_finite() || sheet.max_hp <= 0.0
            || !sheet.base_as.is_finite() || sheet.base_as <= 0.0
            || !sheet.attack_speed(0.0).is_finite() || sheet.attack_speed(0.0) <= 0.0
            || !sheet.base_ad.is_finite() || sheet.base_ad < 0.0
            || !sheet.armor.is_finite() || !sheet.mr.is_finite()
            || !sheet.mana_max.is_finite() || sheet.mana_max < 0.0
            || !sheet.mana_start.is_finite() || sheet.mana_start < 0.0
            || spec.unit.cast_time.is_some_and(|time| !time.is_finite() || time < 0.0) {
            return Err(invalid("theoretical actor needs finite combat stats, positive health and attack speed"));
        }
        let seed = with_driver!(spec.driver.as_str(), D, {
            Ok::<Box<dyn Seed>, PyErr>(Box::new(TypedSeed(D::new(kit, &spec.unit))))
        })?;
        let targets = spec.dummies.iter().map(|source| {
            let mut target = Dummy::new(source.hp, source.armor, source.mr, source.is_tank);
            target.nearby = source.nearby;
            // Without it a stated radius fell back to the `nearby` rule here,
            // so the per-ability radii never reached the composition score.
            target.position = source.position;
            target.immortal = true;
            target.baseline_sunder = spec.target_debuffs.sunder;
            target.baseline_shred = spec.target_debuffs.shred;
            target
        }).collect();
        Ok(Self { spec, fx, sheet, targets, seed })
    }
}

#[derive(Default)]
struct PreparedCache {
    entries: HashMap<usize, (Arc<Prepared>, u64)>,
    access: VecDeque<(usize, u64)>,
    clock: u64,
}
impl PreparedCache {
    fn get(&mut self, spec: &Arc<CellSpec>) -> PyResult<Arc<Prepared>> {
        let key = Arc::as_ptr(spec) as usize;
        self.clock += 1;
        let stamp = self.clock;
        let result = if let Some((prepared, used)) = self.entries.get_mut(&key) {
            *used = stamp;
            Arc::clone(prepared)
        } else {
            let prepared = Arc::new(Prepared::new(Arc::clone(spec))?);
            self.entries.insert(key, (Arc::clone(&prepared), stamp));
            prepared
        };
        self.access.push_back((key, stamp));
        while self.entries.len() > PREPARED_LIMIT {
            if let Some((old, used)) = self.access.pop_front() {
                if self.entries.get(&old).is_some_and(|(_, current)| *current == used) {
                    self.entries.remove(&old);
                }
            }
        }
        if self.access.len() > PREPARED_LIMIT * 4 {
            self.access.retain(|(key, used)| self.entries.get(key).is_some_and(|(_, current)| current == used));
        }
        Ok(result)
    }
}
thread_local! {
    static PREPARED: RefCell<PreparedCache> = RefCell::new(PreparedCache::default());
}
fn prepare(spec: &Arc<CellSpec>) -> PyResult<Arc<Prepared>> {
    PREPARED.with(|cache| cache.borrow_mut().get(spec))
}

pub(crate) fn opening(spec: &Arc<CellSpec>, front: bool) -> PyResult<ResponseSample> {
    opening_targeted_info(spec, front, u8::from(front)).map(|(sample, _)| sample)
}

pub(crate) fn opening_targeted_info(spec: &Arc<CellSpec>, front: bool, source_mask: u8)
    -> PyResult<(ResponseSample, bool)> {
    if source_mask > 0b111 { return Err(invalid("source_mask must be between 0 and 7")); }
    let prepared = prepare(spec)?;
    let mut actor = prepared.seed.spawn(&prepared, front, 0.0, source_mask);
    let targetable = actor.targetable(0.0);
    if !targetable { actor.focus(0); }
    Ok((actor.observe(0.0), targetable))
}

fn any_front(actors: &[Box<dyn Actor + '_>], fronts: &[bool]) -> bool {
    actors.iter().zip(fronts).any(|(actor, front)| *front && actor.holding())
}
/// Initial pressure is concentrated on the first two eligible frontliners.
/// The priority entries and returned owners use the same index space.
pub(crate) fn initial_targets(order: &[usize], targetable: &[bool]) -> [Option<usize>; INCOMING_SOURCE_COUNT] {
    let mut eligible = order.iter().copied().filter(|&index| targetable.get(index) == Some(&true));
    let first = eligible.next();
    let second = eligible.next().or(first);
    [first, second, first]
}

struct Targeting {
    order: Vec<usize>,
    owners: [Option<usize>; INCOMING_SOURCE_COUNT],
}
impl Targeting {
    fn mask(&self, actor: usize) -> u8 {
        self.owners.iter().enumerate().fold(0, |mask, (source, owner)|
            mask | if *owner == Some(actor) { 1 << source } else { 0 })
    }

    fn refresh(&mut self, actors: &mut [Box<dyn Actor + '_>], time: f64) {
        let mut changed = false;
        for owner in &mut self.owners {
            if owner.is_some_and(|index| actors[index].targetable(time)) { continue; }
            let next = self.order.iter().copied().find(|&index| actors[index].targetable(time));
            changed |= *owner != next;
            *owner = next;
        }
        if changed {
            for (index, actor) in actors.iter_mut().enumerate() { actor.focus(self.mask(index)); }
        }
    }
}
struct SharedTargets {
    stuns: [f64; INCOMING_SOURCE_COUNT],
    flat: Vec<(f64, f64)>,
    reductions: Vec<Reductions>,
    sleeps: Option<SharedSleep>,
}
impl SharedTargets {
    fn control_until(&self, target: usize, time: f64) -> f64 {
        self.stuns[target].max(self.sleeps.as_ref().map_or(0.0, |sleeps|
            sleeps.borrow().until(target, 0, time)))
    }
}
fn synchronize(actors: &mut [Box<dyn Actor + '_>], shared: &mut SharedTargets, targeting: &mut Targeting, time: f64) {
    for actor in actors.iter() {
        actor.collect_stuns(&mut shared.stuns);
        actor.collect_flat(&mut shared.flat);
    }
    for actor in actors.iter_mut() {
        actor.sync_stuns(&shared.stuns);
        actor.sync_flat(&shared.flat);
    }
    targeting.refresh(actors, time);
}

/// Hand every target's live timed Sunder/Shred to all allies. Each ally
/// leaves holding the same merged view, so the copies stay identical (and
/// run out together) until one of them changes its own. Only an actor's own
/// hits do that (its events, attacks and reactions to incoming damage; an
/// allied Spellweaver cast adds AP and a wake-up is a plain `deal`), and the
/// scheduler shares each change right after that action, before anyone else
/// acts at the same instant. Scanning every ally at every synchronization
/// instead cost a fifth of the whole measurement.
fn share_reductions(actors: &mut [Box<dyn Actor + '_>], shared: &mut SharedTargets, time: f64) {
    shared.reductions.fill(Reductions::default());
    for actor in actors.iter() { actor.collect_reductions(&mut shared.reductions, time); }
    for actor in actors.iter_mut() { actor.sync_reductions(&shared.reductions); }
}

fn settle_wakes(actors: &mut [Box<dyn Actor + '_>], shared: &mut SharedTargets,
                targeting: &mut Targeting, time: f64) {
    let Some(sleeps) = shared.sleeps.clone() else { return; };
    loop {
        let wake = sleeps.borrow_mut().wakes.pop_front();
        let Some(wake) = wake else { break; };
        synchronize(actors, shared, targeting, time);
        actors[wake.source].wake_sleep(time, wake);
    }
}

fn share_casts(actors: &mut [Box<dyn Actor + '_>], source: usize, time: f64) {
    for _ in 0..actors[source].spellweaver_casts() {
        for (index, actor) in actors.iter_mut().enumerate() {
            if index != source { actor.allied_spellweaver_cast(time); }
        }
    }
}
fn deny(actors: &mut [Box<dyn Actor + '_>], fronts: &[bool], amount: f64) {
    let holding: Vec<usize> = actors.iter().zip(fronts).enumerate()
        .filter_map(|(i, (actor, front))| (*front && actor.holding()).then_some(i)).collect();
    if !holding.is_empty() {
        let each = amount / holding.len() as f64;
        for index in holding { actors[index].deny(each); }
    }
}

fn deny_source(actors: &mut [Box<dyn Actor + '_>], fronts: &[bool], targeting: &Targeting,
               source: usize, amount: f64) {
    if let Some(owner) = targeting.owners[source] { actors[owner].deny(amount); }
    else { deny(actors, fronts, amount); }
}

/// Keep each packet and its lethal remainder attached to the same source.
/// An on-death body keeps its owner's slot; only losing that slot or becoming
/// untargetable changes the owner. Surviving owners are never rebalanced.
fn distribute(actors: &mut [Box<dyn Actor + '_>], fronts: &[bool], time: f64,
              amount: f64, dtype: DType, source: usize, shared: &mut SharedTargets,
              targeting: &mut Targeting) -> PyResult<f64> {
    let mut remaining = amount;
    for _ in 0..128 {
        if remaining <= 0.0 { return Ok(0.0); }
        targeting.refresh(actors, time);
        let Some(owner) = targeting.owners[source] else {
            if any_front(actors, fronts) { deny(actors, fronts, remaining); return Ok(0.0); }
            return Ok(remaining);
        };
        remaining = actors[owner].receive(remaining, dtype, source);
        share_casts(actors, owner, time);
        settle_wakes(actors, shared, targeting, time);
        if actors[owner].changed_flat() { synchronize(actors, shared, targeting, time); }
        if actors[owner].changed_reductions() { share_reductions(actors, shared, time); }
        targeting.refresh(actors, time);
    }
    Err(invalid("theoretical pressure could not make progress through finite health pools"))
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn measure(specs: &[Arc<CellSpec>], fronts: &[bool], window: f64, pressure: f64,
                      physical_share: f64, control_interval: f64, control_duration: f64,
                      target_order: &[usize]) -> PyResult<Measurement> {
    if specs.is_empty() || specs.len() != fronts.len() || !fronts.iter().any(|front| *front)
        || specs.iter().map(|spec| if spec.unit.api == "TFT18_ElderDragon" {2} else {1}).sum::<usize>() > 9 {
        return Err(invalid("theoretical team needs matching actors/fronts within9slots and a frontline"));
    }
    let mut priority_seen = HashSet::new();
    if target_order.len() != fronts.iter().filter(|front| **front).count()
        || target_order.iter().any(|&index| index >= fronts.len() || !fronts[index] || !priority_seen.insert(index)) {
        return Err(invalid("target_order must contain every frontline caller index exactly once"));
    }
    if !window.is_finite() || !(0.0..=MAX_WINDOW).contains(&window)
        || !pressure.is_finite() || pressure <= 0.0
        || !physical_share.is_finite() || !(0.0..=1.0).contains(&physical_share)
        || !control_duration.is_finite() || control_duration < 0.0
        || !control_interval.is_finite() || control_interval <= 0.0 {
        return Err(invalid("invalid theoretical window, pressure, damage mix or control schedule"));
    }
    let mut seen = HashSet::new();
    if specs.iter().any(|spec| !seen.insert(&spec.unit.api)) {
        return Err(invalid("theoretical team requires distinct champion APIs"));
    }
    let count = specs[0].dummies.len();
    if specs.iter().any(|spec| spec.dummies.len() != count) {
        return Err(invalid("theoretical actors must share generic target coverage"));
    }
    // Native state and same-time ordering are canonical; output order stays
    // exactly the caller's order for per-unit contribution association.
    let mut order: Vec<usize> = (0..specs.len()).collect();
    order.sort_by(|&a, &b| specs[a].unit.api.cmp(&specs[b].unit.api));
    let prepared = order.iter().map(|&i| prepare(&specs[i])).collect::<PyResult<Vec<_>>>()?;
    let front_order: Vec<bool> = order.iter().map(|&i| fronts[i]).collect();
    let mut canonical_index = vec![0; order.len()];
    for (internal, &caller) in order.iter().enumerate() { canonical_index[caller] = internal; }
    let priority: Vec<usize> = target_order.iter().map(|&caller| canonical_index[caller]).collect();
    let mut targeting = Targeting { owners: initial_targets(&priority, &front_order), order: priority };
    let mut initial_states = HashSet::new();
    let mut actors = loop {
        if !initial_states.insert(targeting.owners) {
            return Err(invalid("initial targetability does not converge under source masks"));
        }
        let actors = prepared.iter().zip(&front_order).enumerate()
            .map(|(index, (prepared, front))| prepared.seed.spawn(prepared, *front, window, targeting.mask(index)))
            .collect::<Vec<_>>();
        let targetable: Vec<bool> = actors.iter().map(|actor| actor.targetable(0.0)).collect();
        let owners = initial_targets(&targeting.order, &targetable);
        if owners == targeting.owners { break actors; }
        // Initial defensive calculations must see the final masks, so a
        // unit that starts untargetable causes a fresh initialization with
        // its sources assigned to the next eligible priority entries.
        targeting.owners = owners;
    };
    let initial_source_targets = targeting.owners.map(|owner| owner.map(|internal| order[internal]));
    let sleeps = specs.iter().any(|spec| spec.driver == "Lillia")
        .then(|| Rc::new(RefCell::new(SleepTracker::new(count))));
    if let Some(tracker) = &sleeps {
        for (source, actor) in actors.iter_mut().enumerate() { actor.sleep_tracker(Rc::clone(tracker), source); }
    }
    let mut shared = SharedTargets { stuns: [0.0; INCOMING_SOURCE_COUNT],
                                    flat: vec![(0.0, 0.0); count],
                                    reductions: vec![Reductions::default(); count], sleeps };
    let mut source = 0;
    let mut elapsed = 0.0;
    let mut last_pressure = 0.0;
    let mut unspent = 0.0;
    let mut pulse = 1usize;
    let mut control = 1usize;
    synchronize(&mut actors, &mut shared, &mut targeting, 0.0);
    if actors.iter().any(|actor| actor.changed_reductions()) {
        share_reductions(&mut actors, &mut shared, 0.0);
    }
    while elapsed < window && any_front(&actors, &front_order) {
        let next_pressure = (pulse as f64 * PRESSURE_INTERVAL).min(window);
        let next_control = if control_duration > 0.0 { control as f64 * control_interval } else { FAR };
        let next_actor = actors.iter().map(|actor| actor.next()).fold(FAR, f64::min);
        let time = next_actor.min(next_pressure).min(next_control).min(window);
        if time + EPS < elapsed || !time.is_finite() {
            return Err(invalid("theoretical actor clock moved backwards or became nonfinite"));
        }
        elapsed = time.max(elapsed);
        targeting.refresh(&mut actors, elapsed);
        if next_control <= elapsed {
            for (actor, front) in actors.iter_mut().zip(&front_order) {
                if *front { actor.control(elapsed, control_duration); }
            }
            control += 1;
        }
        let mut due = vec![false; actors.len()];
        for index in 0..actors.len() {
            if actors[index].holding() { due[index] = actors[index].events(elapsed); }
            share_casts(&mut actors, index, elapsed);
            settle_wakes(&mut actors, &mut shared, &mut targeting, elapsed);
            if actors[index].changed_flat() {
                synchronize(&mut actors, &mut shared, &mut targeting, elapsed);
            }
            if actors[index].changed_reductions() {
                share_reductions(&mut actors, &mut shared, elapsed);
            }
            targeting.refresh(&mut actors, elapsed);
            if !any_front(&actors, &front_order) { break; }
        }
        synchronize(&mut actors, &mut shared, &mut targeting, elapsed);
        if !any_front(&actors, &front_order) { break; }
        if next_pressure <= elapsed {
            let budget = pressure * (elapsed - last_pressure);
            last_pressure = elapsed;
            if shared.control_until(source, elapsed) > elapsed {
                deny_source(&mut actors, &front_order, &targeting, source, budget);
            } else {
                unspent += distribute(&mut actors, &front_order, elapsed, budget * physical_share,
                    DType::Physical, source, &mut shared, &mut targeting)?;
                unspent += distribute(&mut actors, &front_order, elapsed, budget * (1.0-physical_share),
                    DType::Magic, source, &mut shared, &mut targeting)?;
            }
            pulse += 1;
            source = (source + 1) % INCOMING_SOURCE_COUNT;
            synchronize(&mut actors, &mut shared, &mut targeting, elapsed);
            if !any_front(&actors, &front_order) { break; }
        }
        for index in 0..actors.len() {
            if actors[index].holding() { actors[index].attack(elapsed, due[index]); }
            share_casts(&mut actors, index, elapsed);
            settle_wakes(&mut actors, &mut shared, &mut targeting, elapsed);
            if actors[index].changed_flat() {
                synchronize(&mut actors, &mut shared, &mut targeting, elapsed);
            }
            if actors[index].changed_reductions() {
                share_reductions(&mut actors, &mut shared, elapsed);
            }
            targeting.refresh(&mut actors, elapsed);
            if !any_front(&actors, &front_order) { break; }
        }
        synchronize(&mut actors, &mut shared, &mut targeting, elapsed);
    }
    // A self-death between scheduled incoming pulses ends the measurement;
    // its partial outstanding budget cannot be silently lost or credited.
    if elapsed > last_pressure { unspent += pressure * (elapsed - last_pressure); }
    let collapsed = !any_front(&actors, &front_order);
    let mut samples: Vec<Option<ResponseSample>> = vec![None; actors.len()];
    for ((actor, &original), _) in actors.iter_mut().zip(&order).zip(&front_order) {
        samples[original] = Some(actor.observe(elapsed));
    }
    Ok(Measurement { samples: samples.into_iter().map(Option::unwrap).collect(),
                     elapsed, collapsed, incoming_budget: pressure * elapsed, unspent,
                     target_order: target_order.to_vec(), initial_source_targets })
}

#[pyfunction]
#[pyo3(signature = (spec, front, source_mask=None))]
pub(crate) fn theory_opening<'py>(py: Python<'py>, spec: &Bound<'py, PyDict>, front: bool,
                                 source_mask: Option<i64>) -> PyResult<Bound<'py, PyDict>> {
    let spec = Arc::new(CellSpec::from_py(spec)?);
    if let Some(mask) = source_mask {
        if !(0..=7).contains(&mask) { return Err(invalid("source_mask must be between 0 and 7")); }
        let (sample, targetable) = py.detach(|| opening_targeted_info(&spec, front, mask as u8))?;
        let out = crate::response_to_py(py, &sample)?;
        out.set_item("targetable", targetable)?;
        return Ok(out);
    }
    let sample = py.detach(|| opening(&spec, front))?;
    crate::response_to_py(py, &sample)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_reductions_keep_the_strongest_running_view_and_drop_expired_ones() {
        let mut shared = Reductions::default();
        // One ally's Caustic runs to 5 s; another's Void Staff Shred to 7 s.
        shared.merge(Reductions { sunder: (0.3, 5.0), shred: (0.3, 5.0) }, 2.0);
        shared.merge(Reductions { sunder: (0.0, 0.0), shred: (0.3, 7.0) }, 2.0);
        assert_eq!(shared, Reductions { sunder: (0.3, 5.0), shred: (0.3, 7.0) });
        // A view that has run out adds nothing, however strong it was.
        shared.merge(Reductions { sunder: (0.9, 2.0), shred: (0.9, 1.0) }, 2.0);
        assert_eq!(shared, Reductions { sunder: (0.3, 5.0), shred: (0.3, 7.0) });
        // Once no ally's view is running, nothing carries forward.
        let mut later = Reductions::default();
        later.merge(Reductions { sunder: (0.3, 5.0), shred: (0.3, 7.0) }, 7.0);
        assert_eq!(later, Reductions::default());
    }
}

#[pyfunction]
#[pyo3(signature = (specs, fronts, window, pressure, physical_share, control_interval=8.0, control_duration=0.0, target_order=None))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn measure_theory_team<'py>(py: Python<'py>, specs: &Bound<'py, PyAny>, fronts: Vec<bool>,
                                     window: f64, pressure: f64, physical_share: f64,
                                     control_interval: f64, control_duration: f64,
                                     target_order: Option<Vec<usize>>) -> PyResult<Bound<'py, PyDict>> {
    let specs = specs.try_iter()?.map(|spec| Ok(Arc::new(CellSpec::from_py(&dict_of(&spec?)?)?)))
        .collect::<PyResult<Vec<_>>>()?;
    let target_order = target_order.unwrap_or_else(|| {
        let mut order: Vec<usize> = (0..specs.len()).filter(|&index| fronts.get(index) == Some(&true)).collect();
        order.sort_by(|&a, &b| specs[a].unit.api.cmp(&specs[b].unit.api));
        order
    });
    let measurement = py.detach(|| measure(&specs, &fronts, window, pressure, physical_share,
                                           control_interval, control_duration, &target_order))?;
    let out = PyDict::new(py);
    out.set_item("elapsed", measurement.elapsed)?;
    out.set_item("collapsed", measurement.collapsed)?;
    out.set_item("incomingBudget", measurement.incoming_budget)?;
    out.set_item("unspentPressure", measurement.unspent)?;
    out.set_item("targetOrder", measurement.target_order)?;
    out.set_item("initialSourceTargets", measurement.initial_source_targets)?;
    let samples = PyList::empty(py);
    for sample in &measurement.samples { samples.append(crate::response_to_py(py, sample)?)?; }
    out.set_item("samples", samples)?;
    Ok(out)
}
