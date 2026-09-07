//! Champion-versus-champion encounters. Damage is resolved through the
//! recipient's real Fight before its donor receives damage/healing credit.
//! RefCell borrows serialize actions; reciprocal procs use a FIFO at the
//! same timestamp rather than recursively damaging a borrowed attacker.

use std::cell::{Cell, RefCell};
use std::collections::VecDeque;
use std::rc::{Rc, Weak};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::driver::Driver;
use crate::fight::{make_dummies, Deal, Dummy, Ev, Fight, Sheet, TeamClock, TeamEffect, TICK_S};
use crate::fx::{build_fx, Fx};
use crate::kit::DType;
use crate::pyget::{dict_of, getf, geti, getlist, gets, reqd, truthy};
use crate::spec::{CellSpec, DummySpec, EnemyDebuffs};

const EPS: f64 = 1e-9;
// Match capacity counts actors. Board-slot costs (for example Elder Dragon's
// two slots) belong to the composition planner, not the combat scheduler.
const MAX_ACTORS: usize = crate::fight::MAX_COMBAT_TARGETS;
type ActorMask = u16;
const _: () = assert!(MAX_ACTORS <= ActorMask::BITS as usize);

#[derive(Clone, Copy, Debug)]
pub(crate) struct CombatState {
    pub generation: u64,
    pub hp: f64, pub max_hp: f64, pub champion_max_hp: f64,
    pub alive: bool, pub holding: bool, pub armor: f64, pub mr: f64,
    pub shield: f64, pub untargetable_until: f64, pub stunned_until: f64,
    pub mana_max: f64, pub tank: bool, pub sunder_aura: f64,
    pub shred_aura: f64, pub ionic_spark: f64,
    pub cc_immune: bool,
}

#[derive(Clone, Debug)]
pub(crate) struct CombatHit {
    pub generation: u64,
    pub target: usize, pub amount: f64, pub dtype: DType,
    pub src: &'static str, pub mode: Deal, pub attack: bool,
    pub execution: bool, pub armor_ignore_pct: f64,
    pub armor_ignore: f64, pub mr_ignore: f64,
}

pub(crate) struct CombatDamage {
    pub amount: f64, pub raw_amount: f64, pub state: CombatState,
    pub champion_killed: bool,
}

pub(crate) trait CombatBridge {
    fn hit(&self, hit: CombatHit) -> Option<CombatDamage>;
    fn effects(&self, effects: Vec<TeamEffect>);
    fn publish(&self, state: CombatState);
    fn primary(&self) -> Option<usize>;
    fn damage_heals(&self, alive: bool, source: &str) -> bool;
    fn record(&self, event: Ev);
    fn deferred_on_hit(&self, target: usize, generation: u64);
}

#[derive(Clone)]
struct Position { front: bool, lane: i64, priority: i64 }
#[derive(Clone)]
struct Entry { prepared: Arc<PreparedData>, position: Position }

/// Parsed, resolved inputs only. Drivers, effects, clocks and sheets are
/// cloned before init, so no combat state is shared across encounters.
struct PreparedData {
    spec: CellSpec, fx: Fx, sheet: Sheet, clock: TeamClock,
    placeholder: Dummy, driver: Box<dyn ActorSeed>,
}

#[pyclass(frozen, module = "lol_tft")]
pub(crate) struct PreparedActor { entry: Entry }

#[pymethods]
impl PreparedActor {
    fn __repr__(&self) -> String {
        format!("PreparedActor({:?}, star={}, items={})", self.entry.prepared.spec.unit.api,
                self.entry.prepared.spec.star, self.entry.prepared.spec.items.len())
    }
}

impl Entry {
    fn parse(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        if let Ok(prepared) = value.extract::<PyRef<'_, PreparedActor>>() {
            return Ok(prepared.entry.clone());
        }
        let entry = dict_of(value)?;
        // Keep CellSpec's standalone target validation. Neither preparing
        // nor matching changes the caller's dictionary or needs fixtures.
        let cell_data = reqd(&entry, "spec")?.copy()?;
        let placeholder = PyDict::new(value.py());
        placeholder.set_item("hp", 1.0)?;
        placeholder.set_item("armor", 0.0)?;
        placeholder.set_item("mr", 0.0)?;
        let dummies = PyDict::new(value.py());
        dummies.set_item("slots", PyList::new(value.py(), [placeholder])?)?;
        cell_data.set_item("dummies", dummies)?;
        let mut spec = CellSpec::from_py(&cell_data)?;
        let lane = geti(&entry, "lane", 3)?;
        if !(0..=6).contains(&lane) { return Err(PyValueError::new_err("match lanes must be between 0 and 6")); }
        spec.pressure = true; spec.immortal = false;
        spec.enemy_debuffs = EnemyDebuffs::default(); spec.target_debuffs = Default::default();
        spec.dummies = vec![DummySpec {
            hp: 1.0, armor: 0.0, mr: 0.0, is_tank: false, nearby: true,
            ad: 0.0, as_: 0.0, ability: 0.0, phys_share: 1.0,
            mana_max: 0.0, mana_start: 0.0, mana_per_attack: 0.0, mana_from_damage: false,
            attack_start: None, cast_interval: 0.0, cast_start: None, streams: 1,
        }];
        let items = spec.items.iter().collect::<Vec<_>>();
        let fx = build_fx(spec.role, &items, &spec.traits, spec.unit.has_forms, spec.unit.attack);
        let kit = spec.kit_for(fx.form);
        let sheet = Sheet::new(&spec, kit, &fx);
        let clock = TeamClock::new(&fx);
        let driver = make_seed(&spec, &fx)?;
        let placeholder = make_dummies(&spec).pop().unwrap();
        Ok(Self { prepared: Arc::new(PreparedData { spec, fx, sheet, clock, placeholder, driver }),
            position: Position { front: truthy(&entry, "frontline")?, lane, priority: geti(&entry, "priority", 0)? } })
    }
}

/// Resolve one complete actor entry for reuse in otherwise independent matches.
#[pyfunction]
pub(crate) fn prepare_actor(entry: &Bound<'_, PyAny>) -> PyResult<PreparedActor> {
    Ok(PreparedActor { entry: Entry::parse(entry)? })
}

struct Spec {
    sides: [Vec<Entry>; 2], duration: f64, clump: bool, initiative: usize,
    wound: f64, heal_procs: bool, heal_after_death: bool,
}

fn option_bool(d: &Bound<'_, PyDict>, key: &str, default: bool) -> PyResult<bool> {
    Ok(match d.get_item(key)? { Some(value) => value.extract()?, None => default })
}

impl Spec {
    fn parse(d: &Bound<'_, PyDict>) -> PyResult<Self> {
        let duration = getf(d, "duration", 30.0)?;
        if !duration.is_finite() || duration <= 0.0 || duration > 120.0 {
            return Err(PyValueError::new_err("match duration must be positive and at most 120 seconds"));
        }
        let geometry = gets(d, "geometry", "clump")?;
        if !matches!(geometry.as_str(), "clump" | "spread") {
            return Err(PyValueError::new_err("unknown match geometry"));
        }
        let initiative = geti(d, "initiative", 0)?;
        if !matches!(initiative, 0 | 1) { return Err(PyValueError::new_err("initiative must be 0 or 1")); }
        let wound = getf(d, "burnWound", 0.33)?;
        if !wound.is_finite() || !(0.0..=1.0).contains(&wound) {
            return Err(PyValueError::new_err("burnWound must be a fraction"));
        }
        let mut sides = [Vec::new(), Vec::new()];
        for (side, key) in ["allies", "enemies"].iter().enumerate() {
            for entry in getlist(d, key)? {
                sides[side].push(Entry::parse(&entry)?);
            }
            if sides[side].is_empty() || sides[side].len() > MAX_ACTORS {
                return Err(PyValueError::new_err("each match side needs one to nine champions"));
            }
        }
        Ok(Self { sides, duration, clump: geometry == "clump", initiative: initiative as usize, wound,
            // These retain the previous broad damage-healing convention.
            // Exposed switches keep unverified proc/post-death behavior out
            // of hidden assumptions when comparing composition sensitivity.
            heal_procs: option_bool(d, "damageHealingFromProcs", true)?,
            heal_after_death: option_bool(d, "postDeathAllyHealing", true)? })
    }
}

/// Only the shared portion of a target. Rebuilding full Dummy vectors on
/// every action copied each target's empty attack/event buffers as well as
/// its useful fields. The bounded snapshot stays on the stack instead.
#[derive(Clone, Copy, Default)]
struct TargetProjection {
    generation: u64, hp: f64, max_hp: f64, armor: f64, mr: f64, mana_max: f64,
    stunned_until: f64, burn_pct: f64, burn_until: f64,
    burn_stack: f64, burn_stack_until: f64,
    alive: bool, tank: bool, nearby: bool, focused: bool,
}
impl TargetProjection {
    fn apply(&self, target: &mut Dummy) {
        // This is Dummy::new plus the previous context's shared fields.
        // Source-local marks and DoTs survive only on the same entity.
        if target.generation != self.generation {
            target.dots.clear(); target.mark_times.clear(); target.mark = false;
        }
        target.generation = self.generation;
        target.hp = self.hp; target.max_hp = self.max_hp;
        target.armor = self.armor; target.mr = self.mr;
        target.baseline_sunder = 0.0; target.baseline_shred = 0.0;
        target.sunder = 0.0; target.sunder_until = 0.0;
        target.shred = 0.0; target.shred_until = 0.0;
        target.armor_flat = 0.0; target.mr_flat = 0.0;
        target.burn_pct = self.burn_pct; target.burn_until = self.burn_until;
        target.burn_stack = self.burn_stack; target.burn_stack_until = self.burn_stack_until;
        target.alive = self.alive; target.died_at = None;
        target.is_tank = self.tank; target.nearby = self.nearby; target.immortal = false;
        target.ad = 0.0; target.as_ = 0.0; target.crit_ev = 1.0;
        target.next_attacks.fill(0.0); target.n_streams = 0;
        target.targeting_streams = usize::from(self.focused);
        target.cast_interval = 0.0; target.next_cast = crate::fight::FAR;
        target.ability = 0.0; target.phys_share = 1.0;
        target.mana = 0.0; target.mana_max = self.mana_max;
        target.mana_per_attack = 0.0; target.mana_from_damage = false;
        target.lock_until = 0.0; target.stunned_until = self.stunned_until;
        target.attacks = 0; target.casts = 0;
    }
}

struct Context {
    time: f64, targets: [TargetProjection; MAX_ACTORS], primary: Option<usize>,
    debuffs: EnemyDebuffs, stun: f64, armor_flat: f64, mr_flat: f64,
}
#[derive(Clone, Copy, Default)]
struct ScheduleTarget { generation: u64, alive: bool, focused: bool }

struct ActorResult {
    damage: f64, taken: f64, healed: f64, shielded: f64,
    died_at: Option<f64>, attacks: i64, casts: i64,
}

trait Actor<'a> {
    fn bridge(&mut self, bridge: Rc<dyn CombatBridge + 'a>);
    fn sync(&mut self, context: &Context);
    fn schedule(&mut self, time: f64, primary: Option<usize>, debuffs: EnemyDebuffs,
                stun: f64, armor_flat: f64, mr_flat: f64, targets: &[ScheduleTarget]);
    fn state(&self) -> CombatState;
    fn next(&self) -> f64;
    fn advance(&mut self, time: f64);
    fn receive(&mut self, hit: &CombatHit) -> CombatDamage;
    fn credit(&mut self, hit: &CombatHit, damage: CombatDamage);
    fn damage(&mut self, target: usize, amount: f64, dtype: DType, source: &'static str);
    fn heal(&mut self, amount: f64) -> f64;
    fn shield(&mut self, amount: f64, duration: f64) -> f64;
    fn reave(&mut self, amount: f64);
    fn on_hit(&mut self, target: usize);
    fn finish(&mut self) -> (Vec<Ev>, [(u64, f64, f64); MAX_ACTORS]);
    fn result(&mut self, time: f64) -> ActorResult;
}

trait ActorSeed: Send + Sync {
    fn spawn<'a>(&self, prepared: &'a PreparedData, duration: f64, clump: bool,
                 targets: usize, trace: bool) -> Box<dyn Actor<'a> + 'a>;
}

struct Seed<D: Driver>(D);
impl<D: Driver + Sync + 'static> ActorSeed for Seed<D> {
    fn spawn<'a>(&self, prepared: &'a PreparedData, duration: f64, clump: bool,
                 targets: usize, trace: bool) -> Box<dyn Actor<'a> + 'a> {
        Box::new(Champion::<D>::new(prepared, duration, clump, targets, trace, self.0.clone()))
    }
}

fn make_seed(spec: &CellSpec, fx: &Fx) -> PyResult<Box<dyn ActorSeed>> {
    crate::with_driver!(spec.driver.as_str(), D, {
        Ok(Box::new(Seed(D::new(spec.kit_for(fx.form), &spec.unit))) as Box<dyn ActorSeed>)
    })
}

struct Champion<'a, D: Driver> { fight: Fight<'a, D>, clock: TeamClock, received: f64 }

impl<'a, D: Driver> Champion<'a, D> {
    fn new(prepared: &'a PreparedData, duration: f64, clump: bool, targets: usize,
           trace: bool, driver: D) -> Self {
        let spec = &prepared.spec;
        let kit = spec.kit_for(prepared.fx.form);
        let mut fight = Fight::new(spec, kit, prepared.sheet.clone(), prepared.fx.clone(),
                                   vec![prepared.placeholder.clone(); targets], driver);
        // Match-only geometry, horizon and target count belong to each
        // world. The prepared input can be reused across all combinations.
        fight.duration = duration; fight.clump = clump;
        fight.team_mode = true;
        if trace { fight.trace = Some(Vec::new()); }
        D::init(&mut fight);
        Self { fight, clock: prepared.clock.clone(), received: 0.0 }
    }
}

impl<'a, D: Driver> Actor<'a> for Champion<'a, D> {
    fn bridge(&mut self, bridge: Rc<dyn CombatBridge + 'a>) { self.fight.combat_bridge = Some(bridge); }
    fn sync(&mut self, context: &Context) {
        let f = &mut self.fight;
        f.t = context.time; f.enemy_debuffs = context.debuffs;
        f.combat_stunned_until = if f.combat_cc_immune() { 0.0 } else { context.stun };
        f.combat_armor_flat = context.armor_flat; f.combat_mr_flat = context.mr_flat;
        for (local, shared) in f.targets.iter_mut().zip(&context.targets) {
            shared.apply(local);
        }
        let primary = context.primary.unwrap_or(usize::MAX);
        if f.cur != primary { f.target_since = context.time; }
        f.cur = primary;
    }
    fn schedule(&mut self, time: f64, primary: Option<usize>, debuffs: EnemyDebuffs,
                stun: f64, armor_flat: f64, mr_flat: f64, targets: &[ScheduleTarget]) {
        let f = &mut self.fight;
        f.t = time; f.enemy_debuffs = debuffs;
        f.combat_stunned_until = if f.combat_cc_immune() { 0.0 } else { stun };
        f.combat_armor_flat = armor_flat; f.combat_mr_flat = mr_flat;
        for (index, (target, state)) in f.targets.iter_mut().zip(targets).enumerate() {
            if target.generation != state.generation {
                target.dots.clear(); target.mark_times.clear(); target.mark = false;
                target.generation = state.generation;
                if f.cur == index { f.target_since = time; }
            }
            target.alive = state.alive;
            target.team_focus(state.focused);
        }
        let primary = primary.unwrap_or(usize::MAX);
        if f.cur != primary { f.target_since = time; }
        f.cur = primary;
    }
    fn state(&self) -> CombatState { self.fight.combat_state() }
    fn next(&self) -> f64 { self.fight.team_next_event(&self.clock) }
    fn advance(&mut self, time: f64) { self.fight.team_advance(&mut self.clock, time); }
    fn receive(&mut self, hit: &CombatHit) -> CombatDamage {
        let before = self.fight.combat_state();
        let raw = if hit.execution { self.fight.combat_execute(hit.target) } else {
            self.fight.take_with_ignore(hit.amount, hit.dtype, Some(hit.target), hit.attack,
                hit.armor_ignore + before.armor.max(0.0)*hit.armor_ignore_pct, hit.mr_ignore)
        };
        let state = self.fight.combat_state();
        let amount = raw.min((before.hp.max(0.0) + before.shield).max(0.0));
        self.received += amount;
        CombatDamage { amount,
                       raw_amount: raw, champion_killed: before.alive && !state.alive, state }
    }
    fn credit(&mut self, hit: &CombatHit, damage: CombatDamage) { self.fight.combat_credit(hit, damage); }
    fn damage(&mut self, target: usize, amount: f64, dtype: DType, source: &'static str) {
        self.fight.deal(amount, dtype, Some(target), source, Deal::PLAIN);
    }
    fn heal(&mut self, amount: f64) -> f64 { self.fight.heal(amount, "ally healing") }
    fn shield(&mut self, amount: f64, duration: f64) -> f64 {
        if self.fight.shield(amount, duration, "ally shield", false).is_some() { amount } else { 0.0 }
    }
    fn reave(&mut self, amount: f64) { self.fight.mana = (self.fight.mana - amount).max(0.0); }
    fn on_hit(&mut self, target: usize) { self.fight.on_hit_effects(Some(target), true); }
    fn finish(&mut self) -> (Vec<Ev>, [(u64, f64, f64); MAX_ACTORS]) {
        self.fight.combat_flush();
        let mut flat = [(0, 0.0, 0.0); MAX_ACTORS];
        for (target, change) in self.fight.targets.iter_mut().zip(&mut flat) {
            *change = (target.generation, target.armor_flat, target.mr_flat);
            target.armor_flat = 0.0; target.mr_flat = 0.0;
        }
        (self.fight.trace.as_mut().map(std::mem::take).unwrap_or_default(), flat)
    }
    fn result(&mut self, time: f64) -> ActorResult {
        self.fight.t = time;
        // Match contributions report the same capped health/shield damage
        // credited to donors; internal mitigation/mana calculations retain
        // the complete incoming hit and the standalone convention. Avoid
        // cloning standalone breakdowns, probes and traces just to discard
        // them: these are the exact fields the previous report read.
        ActorResult { damage: self.fight.total, taken: self.received,
            healed: self.fight.healed, shielded: self.fight.shield_used,
            died_at: self.fight.died_at, attacks: self.fight.attacks, casts: self.fight.casts }
    }
}

#[derive(Clone)]
struct Burn { source: usize, inferno: bool, pct: f64, start: f64, until: f64 }
#[derive(Default)]
struct Ledger {
    burns: Vec<Burn>, reductions: Vec<(bool, f64, f64)>,
    wound_until: f64, stun_until: f64, armor_flat: f64, mr_flat: f64,
}
enum Deferred {
    Damage { side: usize, source: usize, hit: CombatHit },
    Support { side: usize, source: usize, target: usize, amount: f64, duration: Option<f64> },
    Proc { side: usize, source: usize, target: usize, amount: f64, dtype: DType, name: &'static str },
    Reave { side: usize, target: usize, amount: f64 },
    OnHit { side: usize, source: usize, target: usize, generation: u64 },
}
struct Trace { side: usize, source: usize, event: Ev }
type ActorCell<'a> = RefCell<Box<dyn Actor<'a> + 'a>>;

struct World<'a> {
    spec: &'a Spec, actors: [Vec<ActorCell<'a>>; 2], states: [Vec<Cell<CombatState>>; 2],
    // Exact projections of the last published states, updated alongside
    // them. Repeated eligibility scans need no fresh target copies.
    alive: [Cell<ActorMask>; 2], holding: [Cell<ActorMask>; 2], fronts: [ActorMask; 2],
    primary: [Vec<Cell<Option<usize>>>; 2], ledgers: [Vec<RefCell<Ledger>>; 2],
    frontline: [Cell<Option<f64>>; 2], healing: [Vec<Cell<f64>>; 2], shielding: [Vec<Cell<f64>>; 2],
    time: Cell<f64>, pending: RefCell<VecDeque<Deferred>>, traces: RefCell<Vec<Trace>>, trace: bool,
    targeting_dirty: Cell<bool>, targeting_expires: Cell<f64>,
}

struct Link<'a> { world: Weak<World<'a>>, side: usize, source: usize }
impl CombatBridge for Link<'_> {
    fn hit(&self, hit: CombatHit) -> Option<CombatDamage> {
        self.world.upgrade().unwrap().resolve(self.side, self.source, hit)
    }
    fn effects(&self, effects: Vec<TeamEffect>) { self.world.upgrade().unwrap().effects(self.side, self.source, effects); }
    fn publish(&self, state: CombatState) {
        let world = self.world.upgrade().unwrap();
        world.publish(self.side, self.source, state);
    }
    fn primary(&self) -> Option<usize> {
        self.world.upgrade().unwrap().primary[self.side][self.source].get()
    }
    fn damage_heals(&self, alive: bool, source: &str) -> bool {
        let world = self.world.upgrade().unwrap();
        (alive || world.spec.heal_after_death) && (world.spec.heal_procs ||
            !matches!(source, "burn" | "bleed" | "solar" | "thorns" | "ionic spark" | "execute" | "thrown off"))
    }
    fn record(&self, event: Ev) {
        let world = self.world.upgrade().unwrap();
        if world.trace { world.traces.borrow_mut().push(Trace { side: self.side, source: self.source, event }); }
    }
    fn deferred_on_hit(&self, target: usize, generation: u64) {
        self.world.upgrade().unwrap().pending.borrow_mut().push_back(
            Deferred::OnHit { side: self.side, source: self.source, target, generation });
    }
}

impl<'a> World<'a> {
    fn new(spec: &'a Spec, trace: bool) -> Rc<Self> {
        let mut actors: [Vec<ActorCell<'a>>; 2] = [Vec::new(), Vec::new()];
        for side in 0..2 {
            for entry in &spec.sides[side] {
                actors[side].push(RefCell::new(entry.prepared.driver.spawn(
                    &entry.prepared, spec.duration, spec.clump, spec.sides[1-side].len(), trace)));
            }
        }
        let states: [Vec<Cell<CombatState>>; 2] = std::array::from_fn(|side|
            actors[side].iter().map(|actor| Cell::new(actor.borrow().state())).collect());
        let alive = std::array::from_fn(|side| Cell::new(states[side].iter().enumerate().fold(0,
            |mask, (i, state)| mask | (ActorMask::from(state.get().alive) << i))));
        let holding = std::array::from_fn(|side| Cell::new(states[side].iter().enumerate().fold(0,
            |mask, (i, state)| mask | (ActorMask::from(state.get().holding) << i))));
        let fronts = std::array::from_fn(|side| spec.sides[side].iter().enumerate().fold(0,
            |mask, (i, entry)| mask | (ActorMask::from(entry.position.front) << i)));
        let primary = std::array::from_fn(|side| actors[side].iter().map(|_| Cell::new(None)).collect());
        let ledgers = std::array::from_fn(|side| actors[side].iter().map(|_| RefCell::new(Ledger::default())).collect());
        let healing = std::array::from_fn(|side| actors[side].iter().map(|_| Cell::new(0.0)).collect());
        let shielding = std::array::from_fn(|side| actors[side].iter().map(|_| Cell::new(0.0)).collect());
        let world = Rc::new(Self { spec, actors, states, alive, holding, fronts, primary, ledgers, healing, shielding,
            frontline: [Cell::new(None), Cell::new(None)], time: Cell::new(0.0),
            pending: RefCell::new(VecDeque::new()), traces: RefCell::new(Vec::new()), trace,
            targeting_dirty: Cell::new(true), targeting_expires: Cell::new(f64::INFINITY) });
        for side in 0..2 { for source in 0..world.actors[side].len() {
            world.actors[side][source].borrow_mut().bridge(Rc::new(Link { world: Rc::downgrade(&world), side, source }));
        }}
        world.retarget();
        world
    }

    fn state(&self, side: usize, index: usize) -> CombatState { self.states[side][index].get() }
    fn publish(&self, side: usize, index: usize, state: CombatState) {
        {
            let previous = self.states[side][index].get();
            if previous.generation != state.generation {
                // A fresh on-death body occupies the position, not the
                // victim's poison, wounds, resistance cuts or crowd control.
                *self.ledgers[side][index].borrow_mut() = Ledger::default();
            }
            if previous.alive != state.alive || previous.holding != state.holding
                || previous.untargetable_until != state.untargetable_until {
                self.targeting_dirty.set(true);
            }
            if previous.alive != state.alive {
                let bit = 1 << index;
                self.alive[side].set(if state.alive { self.alive[side].get() | bit }
                                    else { self.alive[side].get() & !bit });
            }
            if previous.holding != state.holding {
                let bit = 1 << index;
                self.holding[side].set(if state.holding { self.holding[side].get() | bit }
                                      else { self.holding[side].get() & !bit });
            }
            self.states[side][index].set(state);
        }
        self.retarget();
    }
    fn retarget(&self) {
        if !self.targeting_dirty.get() && self.time.get() < self.targeting_expires.get() { return; }
        self.targeting_dirty.set(false);
        let mut expires = f64::INFINITY;
        for side in 0..2 {
            if self.frontline[side].get().is_none() && self.fronts[side] & self.holding[side].get() == 0 {
                self.frontline[side].set(Some(self.time.get()));
            }
            for index in 0..self.actors[side].len() {
                let until = self.state(side,index).untargetable_until;
                if until > self.time.get() { expires = expires.min(until); }
                if let Some(target) = self.primary[side][index].get() {
                    let state = self.state(1-side, target);
                    if state.holding && state.untargetable_until <= self.time.get() { continue; }
                }
                let position = &self.spec.sides[side][index].position;
                let target = (0..self.actors[1-side].len()).filter(|&i| {
                    let state = self.state(1-side, i);
                    state.holding && state.untargetable_until <= self.time.get()
                }).min_by_key(|&i| {
                    let other = &self.spec.sides[1-side][i].position;
                    (!other.front, (position.lane-other.lane).abs(), other.priority, i)
                });
                self.primary[side][index].set(target);
            }
        }
        self.targeting_expires.set(expires);
    }
    fn nearby(&self, side: usize, source: usize, target: usize) -> bool {
        if !self.spec.clump { return self.primary[side][source].get() == Some(target); }
        self.spec.sides[1-side][target].position.front || self.fronts[1-side] & self.holding[1-side].get() == 0
    }
    fn defenses(&self, side: usize, source: usize) -> (EnemyDebuffs, f64, f64, f64) {
        let time = self.time.get();
        let ledger = self.ledgers[side][source].borrow();
        let mut debuffs = EnemyDebuffs::default();
        debuffs.wound = if ledger.wound_until > time { self.spec.wound } else { 0.0 };
        for &(shred, pct, until) in &ledger.reductions {
            if until > time {
                if shred { debuffs.shred = debuffs.shred.max(pct); }
                else { debuffs.sunder = debuffs.sunder.max(pct); }
            }
        }
        let mut providers = self.fronts[1-side] & self.alive[1-side].get();
        while providers != 0 {
            let enemy = providers.trailing_zeros() as usize;
            providers &= providers - 1;
            let state = self.state(1-side, enemy);
            if self.nearby(1-side, enemy, source) {
                debuffs.shred = debuffs.shred.max(state.shred_aura);
                debuffs.sunder = debuffs.sunder.max(state.sunder_aura);
            }
        }
        (debuffs, ledger.stun_until, ledger.armor_flat, ledger.mr_flat)
    }
    fn schedule(&self, side: usize, source: usize, actor: &mut dyn Actor<'a>) {
        self.retarget();
        let (debuffs, stun, armor, mr) = self.defenses(side,source);
        let mut targets = [ScheduleTarget::default(); MAX_ACTORS];
        for (index, target) in targets.iter_mut().enumerate().take(self.actors[1-side].len()) {
            let state = self.state(1-side,index);
            *target = ScheduleTarget { generation: state.generation, alive: state.holding,
                focused: state.alive && self.primary[1-side][index].get() == Some(source) };
        }
        actor.schedule(self.time.get(), self.primary[side][source].get(), debuffs, stun, armor, mr,
                       &targets[..self.actors[1-side].len()]);
        self.publish(side,source,actor.state());
    }
    fn context(&self, side: usize, source: usize) -> Context {
        self.retarget();
        let time = self.time.get();
        let (debuffs, stun, armor_flat, mr_flat) = self.defenses(side,source);
        let mut targets = [TargetProjection::default(); MAX_ACTORS];
        for (index, target) in targets.iter_mut().enumerate().take(self.actors[1-side].len()) {
            let state = self.state(1-side, index);
            *target = TargetProjection { generation: state.generation,
                hp: state.hp, max_hp: state.max_hp, armor: state.armor, mr: state.mr,
                tank: state.tank, alive: state.holding, mana_max: state.mana_max,
                nearby: self.nearby(side, source, index), stunned_until: state.stunned_until,
                focused: state.alive && self.primary[1-side][index].get() == Some(source),
                ..TargetProjection::default() };
            for burn in &self.ledgers[1-side][index].borrow().burns {
                if burn.start <= time && burn.until > time {
                    if burn.inferno { target.burn_stack = target.burn_stack.max(burn.pct); target.burn_stack_until = target.burn_stack_until.max(burn.until); }
                    else { target.burn_pct = target.burn_pct.max(burn.pct); target.burn_until = target.burn_until.max(burn.until); }
                }
            }
        }
        Context { time, targets, primary: self.primary[side][source].get(), debuffs,
            stun, armor_flat, mr_flat }
    }
    fn finish(&self, side: usize, index: usize, actor: &mut dyn Actor<'a>) {
        let (events, flat) = actor.finish();
        self.publish(side,index,actor.state());
        for (target, (generation, armor, mr)) in flat.into_iter().enumerate().take(self.actors[1-side].len()) {
            if generation == self.state(1-side,target).generation && (armor != 0.0 || mr != 0.0) {
                let mut ledger = self.ledgers[1-side][target].borrow_mut();
                ledger.armor_flat += armor; ledger.mr_flat += mr;
            }
        }
        if self.trace { self.traces.borrow_mut().extend(events.into_iter().map(|event| Trace { side, source: index, event })); }
        self.retarget();
    }
    fn resolve(&self, side: usize, source: usize, hit: CombatHit) -> Option<CombatDamage> {
        let target = hit.target;
        let state = self.state(1-side, target);
        if state.generation != hit.generation || !state.holding
            || (state.untargetable_until > self.time.get() && hit.src != "burn") {
            return Some(CombatDamage { amount: 0.0, raw_amount: 0.0, state, champion_killed: false });
        }
        let Ok(mut recipient) = self.actors[1-side][target].try_borrow_mut() else {
            self.pending.borrow_mut().push_back(Deferred::Damage { side, source, hit });
            return None;
        };
        recipient.sync(&self.context(1-side, target));
        // The recipient's opposing slot is the donor, not its own index.
        let mut incoming = hit.clone(); incoming.target = source;
        let damage = recipient.receive(&incoming);
        self.finish(1-side, target, recipient.as_mut());
        Some(damage)
    }
    fn settle(&self) {
        loop {
            let pending = self.pending.borrow_mut().pop_front();
            let Some(pending) = pending else { break; };
            match pending {
                Deferred::Damage { side, source, hit } => {
                    if let Some(damage) = self.resolve(side, source, hit.clone()) {
                        let mut actor = self.actors[side][source].borrow_mut();
                        actor.sync(&self.context(side, source));
                        actor.credit(&hit, damage);
                        self.finish(side, source, actor.as_mut());
                    }
                }
                Deferred::Support { side, source, target, amount, duration } => self.support(side, source, target, amount, duration),
                Deferred::Reave { side, target, amount } => self.actors[side][target].borrow_mut().reave(amount),
                Deferred::OnHit { side, source, target, generation } => {
                    if self.state(1-side,target).generation != generation { continue; }
                    let mut actor = self.actors[side][source].borrow_mut();
                    actor.sync(&self.context(side,source));
                    actor.on_hit(target);
                    self.finish(side,source,actor.as_mut());
                }
                Deferred::Proc { side, source, target, amount, dtype, name } => {
                    let mut actor = self.actors[side][source].borrow_mut();
                    actor.sync(&self.context(side, source));
                    actor.damage(target, amount, dtype, name);
                    self.finish(side, source, actor.as_mut());
                }
            }
        }
    }
    fn recipients(&self, side: usize, source: usize) -> Vec<usize> {
        let mut recipients = (0..self.actors[side].len()).filter(|&i| i != source && self.state(side, i).alive).collect::<Vec<_>>();
        recipients.sort_by(|&a, &b| {
            let astate = self.state(side, a); let bstate = self.state(side, b);
            (astate.hp/astate.max_hp).total_cmp(&(bstate.hp/bstate.max_hp)).then(a.cmp(&b))
        });
        recipients
    }
    fn support(&self, side: usize, source: usize, target: usize, amount: f64, duration: Option<f64>) {
        if !self.state(side, target).alive { return; }
        let Ok(mut actor) = self.actors[side][target].try_borrow_mut() else {
            self.pending.borrow_mut().push_back(Deferred::Support { side, source, target, amount, duration });
            return;
        };
        actor.sync(&self.context(side, target));
        let actual = if let Some(duration) = duration { actor.shield(amount, duration) } else { actor.heal(amount) };
        self.finish(side, target, actor.as_mut());
        let counter = if duration.is_some() { &self.shielding[side][source] } else { &self.healing[side][source] };
        counter.set(counter.get() + actual);
        if self.trace && actual > 0.0 {
            self.traces.borrow_mut().push(Trace { side, source, event: Ev {
                t: self.time.get(), kind: if duration.is_some() { "allyShield" } else { "allyHeal" },
                amount: actual, target: target as i64, src: "", hp: self.state(side, source).hp,
            }});
        }
    }
    fn effects(&self, side: usize, source: usize, effects: Vec<TeamEffect>) {
        let time = self.time.get();
        let mut shielded = Vec::new();
        for effect in effects {
            match effect {
                TeamEffect::Heal(amount) => {
                    if let Some(target) = self.recipients(side, source).first() { self.support(side, source, *target, amount, None); }
                }
                TeamEffect::HealAllies(amount, count) => {
                    for target in self.recipients(side, source).into_iter().take(count) { self.support(side, source, target, amount, None); }
                }
                TeamEffect::Shield(amount, duration) => {
                    if let Some(target) = self.recipients(side, source).into_iter().find(|target| !shielded.contains(target)) {
                        shielded.push(target); self.support(side, source, target, amount, Some(duration));
                    }
                }
                TeamEffect::Reduction(target, pct, duration, shred) => {
                    if pct > 0.0 && duration > 0.0 { self.ledgers[1-side][target].borrow_mut().reductions.push((shred, pct, time+duration)); }
                }
                TeamEffect::Stun(target, duration) => {
                    // Immunity can expire at this exact timestamp even if
                    // the target has not yet taken its own turn.
                    if let Ok(mut actor) = self.actors[1-side][target].try_borrow_mut() {
                        actor.sync(&self.context(1-side, target));
                        self.publish(1-side,target,actor.state());
                    }
                    if self.state(1-side, target).cc_immune { continue; }
                    let mut ledger = self.ledgers[1-side][target].borrow_mut();
                    ledger.stun_until = ledger.stun_until.max(time+duration);
                }
                TeamEffect::Burn(target, pct, duration, inferno) => {
                    if pct <= 0.0 || duration <= 0.0 { continue; }
                    let mut ledger = self.ledgers[1-side][target].borrow_mut();
                    if let Some(burn) = ledger.burns.iter_mut().find(|burn|
                        burn.source == source && burn.inferno == inferno && burn.pct == pct && burn.until >= time) {
                        burn.until = burn.until.max(time+duration);
                    } else { ledger.burns.push(Burn { source, inferno, pct, start: time, until: time+duration }); }
                    ledger.wound_until = ledger.wound_until.max(time+duration);
                }
                TeamEffect::Cast(mana) => {
                    for enemy in 0..self.actors[1-side].len() {
                        let state = self.state(1-side, enemy);
                        if !state.alive || state.ionic_spark <= 0.0 || !self.nearby(1-side, enemy, source) { continue; }
                        let Ok(mut actor) = self.actors[1-side][enemy].try_borrow_mut() else {
                            self.pending.borrow_mut().push_back(Deferred::Proc { side: 1-side, source: enemy,
                                target: source, amount: mana*state.ionic_spark, dtype: DType::Magic, name: "ionic spark" });
                            continue;
                        };
                        actor.sync(&self.context(1-side, enemy));
                        actor.damage(source, mana * state.ionic_spark, DType::Magic, "ionic spark");
                        self.finish(1-side, enemy, actor.as_mut());
                    }
                }
                TeamEffect::ManaReave(target, amount) => {
                    if let Ok(mut actor) = self.actors[1-side][target].try_borrow_mut() {
                        actor.reave(amount);
                    } else {
                        self.pending.borrow_mut().push_back(Deferred::Reave { side: 1-side, target, amount });
                    }
                }
            }
        }
    }
    fn burn_tick(&self) {
        let time = self.time.get();
        for side in [self.spec.initiative, 1-self.spec.initiative] {
            for target in 0..self.actors[side].len() {
                if !self.state(side, target).holding { continue; }
                let generation = self.state(side,target).generation;
                let entries = self.ledgers[side][target].borrow().burns.clone();
                let mut boundaries = vec![time-TICK_S, time];
                for burn in &entries {
                    for point in [burn.start, burn.until] {
                        if point > time-TICK_S && point < time { boundaries.push(point); }
                    }
                }
                boundaries.sort_by(f64::total_cmp); boundaries.dedup();
                let mut amounts = vec![0.0; self.actors[1-side].len()];
                for span in boundaries.windows(2) {
                    let middle = (span[0]+span[1])/2.0;
                    for inferno in [false, true] {
                        let strongest = entries.iter().filter(|burn|
                            burn.inferno == inferno && burn.start <= middle && burn.until > middle)
                            .max_by(|a,b| a.pct.total_cmp(&b.pct).then(b.source.cmp(&a.source)));
                        if let Some(burn) = strongest { amounts[burn.source] += burn.pct*(span[1]-span[0]); }
                    }
                }
                for (source, fraction) in amounts.into_iter().enumerate() {
                    if self.state(side,target).generation != generation { break; }
                    if fraction <= 0.0 || !self.state(side, target).holding { continue; }
                    let amount = fraction * self.state(side, target).max_hp;
                    let mut actor = self.actors[1-side][source].borrow_mut();
                    actor.sync(&self.context(1-side, source));
                    actor.damage(target, amount, DType::True, "burn");
                    self.finish(1-side, source, actor.as_mut());
                    drop(actor); self.settle();
                }
                let mut ledger = self.ledgers[side][target].borrow_mut();
                ledger.burns.retain(|burn| burn.until > time);
                ledger.reductions.retain(|effect| effect.2 > time);
            }
        }
    }
    fn run(&self) -> &'static str {
        let mut tick = TICK_S;
        loop {
            let alive = self.holding.each_ref().map(|mask| mask.get() != 0);
            if !alive[0] && !alive[1] { return "draw"; }
            if !alive[1] { return "win"; }
            if !alive[0] { return "loss"; }
            if self.time.get() >= self.spec.duration { return "timeout"; }
            let mut next = tick;
            // Refresh actual target counts and CC before scheduling either side.
            for side in 0..2 { for index in 0..self.actors[side].len() {
                let mut actor = self.actors[side][index].borrow_mut();
                self.schedule(side,index,actor.as_mut());
                next = next.min(actor.next());
            }}
            if next > self.spec.duration { self.time.set(self.spec.duration); return "timeout"; }
            self.time.set(next);
            if tick <= next+EPS { self.burn_tick(); tick += TICK_S; }
            for side in [self.spec.initiative, 1-self.spec.initiative] {
                for index in 0..self.actors[side].len() {
                    if self.holding[1-side].get() == 0 { continue; }
                    let mut actor = self.actors[side][index].borrow_mut();
                    self.schedule(side,index,actor.as_mut());
                    if actor.next() <= next+EPS {
                        actor.sync(&self.context(side,index));
                        actor.advance(next);
                        self.finish(side,index,actor.as_mut());
                    }
                    drop(actor); self.settle();
                }
            }
            self.retarget();
        }
    }
}

struct UnitResult {
    actor: ActorResult, alive: bool, ally_healing: f64, ally_shielding: f64,
}
struct SideResult {
    hp: f64, hp_fraction: f64, frontline: f64, damage: f64, units: Vec<UnitResult>,
}
struct MatchResult {
    outcome: &'static str, duration: f64, sides: [SideResult; 2], traces: Option<Vec<Trace>>,
}

impl MatchResult {
    fn run(spec: &Spec, full: bool, trace: bool) -> Self {
        let world = World::new(spec, trace);
        let outcome = world.run();
        let duration = world.time.get();
        let sides = std::array::from_fn(|side| {
            let hp: f64 = (0..world.actors[side].len()).map(|i| world.state(side,i).hp.max(0.0)).sum();
            let max_hp: f64 = (0..world.actors[side].len()).map(|i| world.state(side,i).champion_max_hp).sum();
            let mut damage = 0.0;
            let mut units = if full { Vec::with_capacity(world.actors[side].len()) } else { Vec::new() };
            for (index, actor) in world.actors[side].iter().enumerate() {
                let actor = actor.borrow_mut().result(duration);
                damage += actor.damage;
                if full { units.push(UnitResult { actor, alive: world.state(side,index).alive,
                    ally_healing: world.healing[side][index].get(), ally_shielding: world.shielding[side][index].get() }); }
            }
            SideResult { hp, hp_fraction: (hp/max_hp.max(1.0)).clamp(0.0,1.0),
                frontline: world.frontline[side].get().unwrap_or(duration), damage, units }
        });
        let traces = if trace {
            let mut events = std::mem::take(&mut *world.traces.borrow_mut());
            events.sort_by(|a,b| a.event.t.total_cmp(&b.event.t));
            Some(events)
        } else { None };
        Self { outcome, duration, sides, traces }
    }

    fn to_py<'py>(&self, py: Python<'py>, spec: &Spec, full: bool) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        out.set_item("outcome", self.outcome)?; out.set_item("duration", self.duration)?;
        for (side, result) in self.sides.iter().enumerate() {
            let prefix = if side == 0 { "ally" } else { "enemy" };
            out.set_item(format!("{prefix}HpLeft"), result.hp)?;
            out.set_item(format!("{prefix}HpFraction"), result.hp_fraction)?;
            out.set_item(if side == 0 { "frontlineTime" } else { "enemyFrontlineTime" }, result.frontline)?;
            if full {
                let units = PyList::empty(py);
                for (index, result) in result.units.iter().enumerate() {
                    let unit = PyDict::new(py);
                    let actor = &result.actor;
                    let input = &spec.sides[side][index].prepared.spec.unit;
                    unit.set_item("api", &input.api)?; unit.set_item("name", &input.name)?;
                    unit.set_item("damage", actor.damage)?; unit.set_item("dps", actor.damage/self.duration.max(TICK_S))?;
                    unit.set_item("alive", result.alive)?;
                    unit.set_item("aliveTime", actor.died_at.unwrap_or(self.duration).min(self.duration))?;
                    unit.set_item("damageTaken", actor.taken)?; unit.set_item("healing", actor.healed)?;
                    unit.set_item("shielding", actor.shielded)?;
                    unit.set_item("allyHealing", result.ally_healing)?; unit.set_item("allyShielding", result.ally_shielding)?;
                    unit.set_item("attacks", actor.attacks)?; unit.set_item("casts", actor.casts)?;
                    units.append(unit)?;
                }
                out.set_item(if side == 0 { "allies" } else { "enemies" }, units)?;
            }
        }
        out.set_item("damage", self.sides[0].damage)?; out.set_item("damageDps", self.sides[0].damage/self.duration.max(TICK_S))?;
        out.set_item("enemyDamage", self.sides[1].damage)?; out.set_item("enemyDamageDps", self.sides[1].damage/self.duration.max(TICK_S))?;
        out.set_item("initiative", spec.initiative)?;
        if full {
            out.set_item("damageHealingFromProcs", spec.heal_procs)?;
            out.set_item("postDeathAllyHealing", spec.heal_after_death)?;
            out.set_item("combatModel", "symmetric-champion-drivers-v1")?;
            out.set_item("modelAssumptions", [
                "Fixed front/back rows and lanes approximate targeting; hex movement is not simulated.",
                "CC delays attacks and unresolved cast landings; already-running damage/heal effects retain their schedules.",
                "Reciprocal damage and support procs settle at the same timestamp after the initiating action.",
                "On-death summons use the existing sequential-body model without independent attacks.",
                "Fresh bodies discard the previous entity's target debuffs and damage-over-time effects; applied effects on other enemies can persist after their donor dies.",
                "Shield damage counts toward damage healing; proc and post-death eligibility are explicit sensitivity switches, not independently verified runtime rules.",
            ])?;
        }
        if let Some(events) = &self.traces {
            let rows = PyList::empty(py);
            for event in events {
                let row = PyDict::new(py);
                row.set_item("time", event.event.t)?; row.set_item("kind", event.event.kind)?;
                row.set_item("source", event.source)?; row.set_item("target", event.event.target)?;
                row.set_item("amount", event.event.amount)?; row.set_item("sourceName", event.event.src)?;
                row.set_item("side", if event.side == 0 { "ally" } else { "enemy" })?;
                rows.append(row)?;
            }
            out.set_item("trace", rows)?;
        }
        Ok(out)
    }
}

#[pyfunction]
#[pyo3(signature = (spec, trace=false))]
pub(crate) fn simulate_match<'py>(py: Python<'py>, spec: &Bound<'py, PyDict>, trace: bool) -> PyResult<Bound<'py, PyDict>> {
    let spec = Spec::parse(spec)?;
    let result = py.detach(|| MatchResult::run(&spec, true, trace));
    result.to_py(py, &spec, true)
}

fn run_matches(specs: &[Spec], full: bool, trace: bool, workers: usize) -> Vec<MatchResult> {
    let workers = workers.min(specs.len()).min(std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1));
    if workers <= 1 { return specs.iter().map(|spec| MatchResult::run(spec, full, trace)).collect(); }
    let next = AtomicUsize::new(0);
    let chunks = std::thread::scope(|scope| {
        let mut threads = Vec::with_capacity(workers);
        for _ in 0..workers {
            let next = &next;
            threads.push(scope.spawn(move || {
                let mut results = Vec::new();
                loop {
                    let index = next.fetch_add(1, Ordering::Relaxed);
                    if index >= specs.len() { break; }
                    results.push((index, MatchResult::run(&specs[index], full, trace)));
                }
                results
            }));
        }
        threads.into_iter().map(|thread| thread.join().unwrap()).collect::<Vec<_>>()
    });
    let mut results: Vec<Option<MatchResult>> = (0..specs.len()).map(|_| None).collect();
    for chunk in chunks { for (index, result) in chunk { results[index] = Some(result); } }
    results.into_iter().map(Option::unwrap).collect()
}

/// Run independent matches in input order, with a fresh world for every
/// row. Compact mode omits reports only; every combat counter still runs.
/// Native work is outside the GIL. Workers are bounded by the requested
/// count, available CPUs and batch length (and never exceed 64).
#[pyfunction]
#[pyo3(signature = (matches, detail="full", trace=false, workers=1))]
pub(crate) fn simulate_matches<'py>(py: Python<'py>, matches: &Bound<'py, PyAny>,
                                   detail: &str, trace: bool, workers: usize) -> PyResult<Bound<'py, PyList>> {
    let full = match detail {
        "full" => true,
        "compact" => false,
        _ => return Err(PyValueError::new_err("match detail must be 'full' or 'compact'")),
    };
    if trace && !full { return Err(PyValueError::new_err("trace requires detail='full'")); }
    if !(1..=64).contains(&workers) { return Err(PyValueError::new_err("match workers must be between 1 and 64")); }
    let specs = matches.try_iter()?.map(|value| Spec::parse(&dict_of(&value?)?)).collect::<PyResult<Vec<_>>>()?;
    let results = py.detach(|| run_matches(&specs, full, trace, workers));
    let rows = PyList::empty(py);
    for (spec, result) in specs.iter().zip(results) { rows.append(result.to_py(py, spec, full)?)?; }
    Ok(rows)
}
