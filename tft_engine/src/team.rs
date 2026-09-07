//! A shared encounter: real champion drivers against declared synthetic
//! enemies. One clock, health pool and targeting assignment per enemy.
//! Positions are fixed lanes/rows, not a reproduction of TFT hex movement.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::driver::Driver;
use crate::fight::{make_dummies, Deal, Dummy, Ev, Fight, FightResult, Sheet, TeamClock, TeamEffect, TICK_S};
use crate::fx::build_fx;
use crate::kit::DType;
use crate::pyget::{dict_of, getf, geti, getlist, gets, reqd, truthy};
use crate::spec::{CellSpec, DummySpec, EnemyDebuffs};

const NEVER: f64 = 1e18;
const EPS: f64 = 1e-9;

#[derive(Clone)]
struct Position { front: bool, lane: i64, priority: i64 }

struct AllySpec { cell: CellSpec, position: Position }

struct EnemySpec {
    slot: DummySpec,
    name: String,
    front: bool,
    lane: i64,
    back: bool,
    healing: f64,
    debuffs: EnemyDebuffs,
    debuff_duration: f64,
}

struct TeamSpec {
    allies: Vec<AllySpec>,
    enemies: Vec<EnemySpec>,
    duration: f64,
    clump: bool,
    crit_ev: f64,
    burn_wound: f64,
}

fn nonnegative(value: f64, name: &str) -> PyResult<f64> {
    if !value.is_finite() || value < 0.0 {
        Err(PyValueError::new_err(format!("{name}: expected a finite nonnegative number")))
    } else { Ok(value) }
}

fn fraction(d: &Bound<'_, PyDict>, name: &str, default: f64) -> PyResult<f64> {
    let value = nonnegative(getf(d, name, default)?, name)?;
    if value > 1.0 { Err(PyValueError::new_err(format!("{name}: expected a fraction from 0 to 1"))) }
    else { Ok(value) }
}

impl TeamSpec {
    fn from_py(d: &Bound<'_, PyDict>) -> PyResult<Self> {
        let duration = nonnegative(getf(d, "duration", 30.0)?, "duration")?;
        if duration <= 0.0 || duration > 120.0 {
            return Err(PyValueError::new_err("team duration must be greater than zero and at most 120 seconds"));
        }
        let geometry = gets(d, "geometry", "clump")?;
        if !matches!(geometry.as_str(), "clump" | "spread") {
            return Err(PyValueError::new_err("unknown team geometry"));
        }
        let mut allies = Vec::new();
        for entry in getlist(d, "allies")? {
            let entry = dict_of(&entry)?;
            let cell = CellSpec::from_py(&reqd(&entry, "spec")?)?;
            let lane = geti(&entry, "lane", 3)?;
            if !(0..=6).contains(&lane) { return Err(PyValueError::new_err("ally lane must be between 0 and 6")); }
            allies.push(AllySpec { cell, position: Position {
                front: truthy(&entry, "frontline")?, lane, priority: geti(&entry, "priority", 0)?,
            }});
        }
        let mut enemies = Vec::new();
        for entry in getlist(d, "enemies")? {
            let entry = dict_of(&entry)?;
            let slot = DummySpec::from_py(&entry)?;
            for (name, value) in [("hp", slot.hp), ("armor", slot.armor), ("mr", slot.mr),
                ("ad", slot.ad), ("as", slot.as_), ("ability", slot.ability)] { nonnegative(value, name)?; }
            if slot.hp <= 0.0 || (slot.ad > 0.0 && slot.as_ <= 0.0) || slot.as_ > 5.0 {
                return Err(PyValueError::new_err("enemies need positive HP and a valid attack speed for damaging attacks"));
            }
            if !(0.0..=1.0).contains(&slot.phys_share) { return Err(PyValueError::new_err("physicalShare must be a fraction")); }
            if slot.ability > 0.0 && slot.cast_interval <= 0.0 {
                return Err(PyValueError::new_err("synthetic enemy spells require a positive castInterval"));
            }
            if slot.streams != 1 { return Err(PyValueError::new_err("each shared enemy must be exactly one entity")); }
            let lane = geti(&entry, "lane", 3)?;
            if !(0..=6).contains(&lane) { return Err(PyValueError::new_err("enemy lane must be between 0 and 6")); }
            let targeting = gets(&entry, "targeting", "front")?;
            if !matches!(targeting.as_str(), "front" | "back") {
                return Err(PyValueError::new_err("enemy targeting must be front or back"));
            }
            enemies.push(EnemySpec {
                name: gets(&entry, "name", "Enemy")?, slot,
                front: truthy(&entry, "frontline")?, lane, back: targeting == "back",
                healing: nonnegative(getf(&entry, "healPerSecond", 0.0)?, "healPerSecond")?,
                debuffs: EnemyDebuffs { wound: fraction(&entry, "wound", 0.0)?,
                    sunder: fraction(&entry, "sunder", 0.0)?, shred: fraction(&entry, "shred", 0.0)? },
                debuff_duration: nonnegative(getf(&entry, "debuffDuration", 4.0)?, "debuffDuration")?,
            });
        }
        if allies.is_empty() || allies.len() > 8 || enemies.is_empty() || enemies.len() > 8 {
            return Err(PyValueError::new_err("shared encounters require one to eight allies and enemies"));
        }
        Ok(Self { allies, enemies, duration, clump: geometry == "clump",
            crit_ev: nonnegative(getf(d, "critEv", 1.1)?, "critEv")?,
            burn_wound: fraction(d, "burnWound", 0.33)? })
    }
}

#[derive(Clone)]
struct Status {
    hp: f64,
    max_hp: f64,
    alive: bool,
    holding: bool,
    untargetable: f64,
    sunder_aura: f64,
    shred_aura: f64,
}

trait Actor {
    fn status(&self) -> Status;
    fn sync(&mut self, time: f64, targets: &[Dummy], nearby: &[bool], focus: &[bool], primary: Option<usize>);
    fn targets(&self) -> &[Dummy];
    fn effects(&mut self) -> Vec<TeamEffect>;
    fn drain_trace(&mut self) -> Vec<Ev>;
    fn next_event(&self) -> f64;
    fn advance(&mut self, time: f64);
    fn receive(&mut self, time: f64, amount: f64, dtype: DType, enemy: usize, attack: bool,
               debuffs: EnemyDebuffs, until: f64);
    fn burn_tick(&mut self, time: f64, target: usize, amount: f64);
    fn enemy_cast(&mut self, time: f64, enemy: usize);
    fn heal(&mut self, time: f64, amount: f64) -> f64;
    fn shield(&mut self, time: f64, amount: f64, duration: f64) -> f64;
    fn result(&mut self, time: f64) -> FightResult;
}

struct Champion<'a, D: Driver> {
    fight: Fight<'a, D>,
    clock: TeamClock,
    debuffs: Vec<(EnemyDebuffs, f64)>,
}

impl<'a, D: Driver> Champion<'a, D> {
    fn new(spec: &'a CellSpec, trace: bool) -> Self {
        let items = spec.items.iter().collect::<Vec<_>>();
        let fx = build_fx(spec.role, &items, &spec.traits, spec.unit.has_forms, spec.unit.attack);
        let kit = spec.kit_for(fx.form);
        let sheet = Sheet::new(spec, kit, &fx);
        let driver = D::new(kit, &spec.unit);
        let clock = TeamClock::new(&fx);
        let mut fight = Fight::new(spec, kit, sheet, fx, make_dummies(spec), driver);
        fight.pressure = true;
        fight.enemy_debuffs = EnemyDebuffs::default();
        fight.team_mode = true;
        if trace { fight.trace = Some(Vec::new()); }
        D::init(&mut fight);
        Self { fight, clock, debuffs: Vec::new() }
    }

    fn refresh_debuffs(&mut self, time: f64) {
        self.debuffs.retain(|(_, until)| *until > time);
        let mut active = EnemyDebuffs::default();
        for (effect, _) in &self.debuffs {
            active.wound = active.wound.max(effect.wound);
            active.sunder = active.sunder.max(effect.sunder);
            active.shred = active.shred.max(effect.shred);
        }
        self.fight.enemy_debuffs = active;
    }
}

impl<D: Driver> Actor for Champion<'_, D> {
    fn status(&self) -> Status {
        let f = &self.fight;
        Status { hp: if f.alive_unit { f.hp } else { f.body.as_ref().map(|b| b.hp).unwrap_or(0.0) },
            max_hp: f.max_hp(), alive: f.alive_unit, holding: f.holding(),
            untargetable: f.untargetable_until, sunder_aura: f.fx.sunder_aura, shred_aura: f.fx.shred_aura }
    }

    fn sync(&mut self, time: f64, targets: &[Dummy], nearby: &[bool], focus: &[bool], primary: Option<usize>) {
        self.refresh_debuffs(time);
        let f = &mut self.fight;
        f.t = time;
        if f.targets.len() != targets.len() { f.targets = targets.to_vec(); }
        for (index, target) in targets.iter().enumerate() {
            // Marks and DoTs belong to their source's ability state. HP,
            // debuffs, burns and CC are shared by all source champions.
            let dots = std::mem::take(&mut f.targets[index].dots);
            let marks = std::mem::take(&mut f.targets[index].mark);
            let mark_times = std::mem::take(&mut f.targets[index].mark_times);
            f.targets[index] = target.clone();
            f.targets[index].dots = dots;
            f.targets[index].mark = marks;
            f.targets[index].mark_times = mark_times;
            f.targets[index].nearby = nearby[index];
            f.targets[index].team_focus(focus[index]);
        }
        if let Some(primary) = primary {
            if primary != f.cur { f.target_since = time; }
            f.cur = primary;
        }
    }

    fn targets(&self) -> &[Dummy] { &self.fight.targets }
    fn effects(&mut self) -> Vec<TeamEffect> { std::mem::take(&mut self.fight.team_effects) }
    fn drain_trace(&mut self) -> Vec<Ev> {
        self.fight.trace.as_mut().map(std::mem::take).unwrap_or_default()
    }
    fn next_event(&self) -> f64 { self.fight.team_next_event(&self.clock) }
    fn advance(&mut self, time: f64) { self.fight.team_advance(&mut self.clock, time); }

    fn receive(&mut self, time: f64, amount: f64, dtype: DType, enemy: usize, attack: bool,
               debuffs: EnemyDebuffs, until: f64) {
        self.fight.t = time;
        if until > time { self.debuffs.push((debuffs, until)); }
        self.refresh_debuffs(time);
        self.fight.take(amount, dtype, Some(enemy), attack);
    }

    fn burn_tick(&mut self, time: f64, target: usize, amount: f64) {
        self.fight.t = time;
        self.fight.deal(amount, DType::True, Some(target), "burn", Deal::PLAIN);
    }

    fn enemy_cast(&mut self, time: f64, enemy: usize) {
        let f = &mut self.fight;
        f.t = time;
        if f.alive_unit && f.targets[enemy].nearby && f.fx.ionic_spark > 0.0 {
            let amount = f.fx.ionic_spark * f.targets[enemy].mana_max;
            f.deal(amount, DType::Magic, Some(enemy), "ionic spark", Deal::PLAIN);
        }
    }

    fn heal(&mut self, time: f64, amount: f64) -> f64 {
        self.fight.t = time;
        self.refresh_debuffs(time);
        self.fight.heal(amount, "ally healing")
    }

    fn shield(&mut self, time: f64, amount: f64, duration: f64) -> f64 {
        self.fight.t = time;
        if self.fight.shield(amount, duration, "ally shield", false).is_some() { amount } else { 0.0 }
    }

    fn result(&mut self, time: f64) -> FightResult {
        self.fight.t = time;
        self.fight.result()
    }
}

fn make_actor(spec: &CellSpec, trace: bool) -> PyResult<Box<dyn Actor + '_>> {
    crate::with_driver!(spec.driver.as_str(), D, {
        Ok(Box::new(Champion::<D>::new(spec, trace)) as Box<dyn Actor + '_>)
    })
}

struct EnemyClock { attack: f64, cast: f64, attacks: usize, casts: usize, focus: Option<usize> }

#[derive(Default)]
struct BurnOwners {
    ordinary: Option<usize>, stacked: Option<usize>, wound_until: f64,
    // Each strength keeps its own lifetime; weaker refreshes cannot prolong
    // a stronger timed effect from another item or champion.
    reductions: Vec<(usize, bool, f64, f64)>,
    burns: Vec<(usize, bool, f64, f64, f64)>, // source, stacked, pct, start, end
}

struct Encounter<'a> {
    specs: &'a TeamSpec,
    actors: Vec<Box<dyn Actor + 'a>>,
    targets: Vec<Dummy>,
    enemies: Vec<EnemyClock>,
    burns: Vec<BurnOwners>,
    primary: Vec<Option<usize>>,
    ally_healing: Vec<f64>,
    ally_shielding: Vec<f64>,
    frontline_time: Option<f64>,
    trace: Vec<TeamEvent>,
    tracing: bool,
    time: f64,
}

struct TeamEvent {
    time: f64, kind: &'static str, source: usize, target: i64, amount: f64,
    source_name: Option<&'static str>, side: &'static str,
}

impl<'a> Encounter<'a> {
    fn new(specs: &'a TeamSpec, trace: bool) -> PyResult<Self> {
        let actors = specs.allies.iter().map(|ally| make_actor(&ally.cell, trace)).collect::<PyResult<Vec<_>>>()?;
        let targets = specs.enemies.iter().map(|enemy| {
            let slot = &enemy.slot;
            let mut target = Dummy::new(slot.hp, slot.armor, slot.mr, slot.is_tank);
            target.arm(slot, specs.crit_ev, 1);
            target.team_focus(false);
            target
        }).collect::<Vec<_>>();
        let enemies = specs.enemies.iter().map(|enemy| EnemyClock {
            attack: if enemy.slot.ad > 0.0 && enemy.slot.as_ > 0.0 { enemy.slot.attack_start.unwrap_or(0.0) } else { NEVER },
            cast: if enemy.slot.ability > 0.0 && enemy.slot.cast_interval > 0.0 {
                enemy.slot.cast_start.unwrap_or(enemy.slot.cast_interval) } else { NEVER },
            attacks: 0, casts: 0, focus: None,
        }).collect();
        let count = actors.len();
        Ok(Self { specs, actors, targets, enemies,
            burns: (0..specs.enemies.len()).map(|_| BurnOwners::default()).collect(),
            primary: vec![None; count], ally_healing: vec![0.0; count], ally_shielding: vec![0.0; count],
            frontline_time: None, trace: Vec::new(), tracing: trace, time: 0.0 })
    }

    fn record(&mut self, kind: &'static str, source: usize, target: usize, amount: f64) {
        if self.tracing { self.trace.push(TeamEvent { time: self.time, kind, source, target: target as i64, amount,
            source_name: None, side: if kind.starts_with("enemy") { "enemy" } else { "ally" } }); }
    }

    fn capture(&mut self, actor: usize) {
        if self.tracing {
            for event in self.actors[actor].drain_trace() {
                self.trace.push(TeamEvent { time: event.t, kind: event.kind, source: actor,
                    target: event.target, amount: event.amount, source_name: Some(event.src), side: "ally" });
            }
        }
    }

    fn retarget(&mut self) {
        let statuses = self.actors.iter().map(|actor| actor.status()).collect::<Vec<_>>();
        if self.frontline_time.is_none() && !statuses.iter().enumerate().any(|(i, s)| s.holding && self.specs.allies[i].position.front) {
            self.frontline_time = Some(self.time);
        }
        for (i, enemy) in self.enemies.iter_mut().enumerate() {
            if !self.targets[i].alive { enemy.focus = None; continue; }
            if enemy.focus.is_some_and(|target| statuses[target].holding && statuses[target].untargetable <= self.time) {
                continue;
            }
            let source = &self.specs.enemies[i];
            enemy.focus = statuses.iter().enumerate().filter(|(_, s)| s.holding && s.untargetable <= self.time)
                .min_by_key(|(j, _)| {
                    let pos = &self.specs.allies[*j].position;
                    (if source.back { pos.front } else { !pos.front }, (source.lane - pos.lane).abs(), pos.priority, *j)
                }).map(|(j, _)| j);
        }
        for (i, primary) in self.primary.iter_mut().enumerate() {
            if primary.is_some_and(|target| self.targets[target].alive) { continue; }
            let pos = &self.specs.allies[i].position;
            *primary = self.targets.iter().enumerate().filter(|(_, target)| target.alive)
                .min_by_key(|(j, _)| (!self.specs.enemies[*j].front, (pos.lane - self.specs.enemies[*j].lane).abs(), *j))
                .map(|(j, _)| j);
        }
    }

    fn nearby(&self, actor: usize) -> Vec<bool> {
        let front_alive = self.targets.iter().enumerate().any(|(i, target)| target.alive && self.specs.enemies[i].front);
        self.targets.iter().enumerate().map(|(i, _)| {
            if self.specs.clump { self.specs.enemies[i].front || !front_alive }
            else { self.primary[actor] == Some(i) }
        }).collect()
    }

    fn sync(&mut self, actor: usize) {
        self.retarget();
        self.refresh_statuses();
        // Positional auras cease when their source dies; timed on-hit
        // reductions remain until their own expiration on the shared target.
        for target in &mut self.targets { target.baseline_sunder = 0.0; target.baseline_shred = 0.0; }
        for i in 0..self.actors.len() {
            let status = self.actors[i].status();
            if !status.alive || !self.specs.allies[i].position.front { continue; }
            let nearby = self.nearby(i);
            for (j, target) in self.targets.iter_mut().enumerate() {
                if nearby[j] {
                    target.baseline_sunder = target.baseline_sunder.max(status.sunder_aura);
                    target.baseline_shred = target.baseline_shred.max(status.shred_aura);
                }
            }
        }
        let nearby = self.nearby(actor);
        let focus = self.enemies.iter().map(|enemy| enemy.focus == Some(actor)).collect::<Vec<_>>();
        self.actors[actor].sync(self.time, &self.targets, &nearby, &focus, self.primary[actor]);
    }

    fn commit(&mut self, actor: usize) {
        for (shared, changed) in self.targets.iter_mut().zip(self.actors[actor].targets()) {
            shared.hp = changed.hp;
            shared.max_hp = changed.max_hp;
            shared.alive = changed.alive;
            shared.died_at = changed.died_at;
            shared.armor_flat = changed.armor_flat;
            shared.mr_flat = changed.mr_flat;
            shared.stunned_until = changed.stunned_until;
        }
        self.capture(actor);
        let mut shielded = Vec::new();
        for effect in self.actors[actor].effects() {
            match effect {
                TeamEffect::Heal(amount) => {
                    // Gunblade heals one lowest-percent-health ally. Any
                    // overhealing is lost; it cannot spill to another unit.
                    let target = (0..self.actors.len()).filter(|&i| i != actor && self.actors[i].status().alive)
                        .min_by(|&a, &b| {
                            let sa = self.actors[a].status(); let sb = self.actors[b].status();
                            (sa.hp / sa.max_hp).total_cmp(&(sb.hp / sb.max_hp)).then(a.cmp(&b))
                        });
                    if let Some(ally) = target {
                        let healed = self.actors[ally].heal(self.time, amount);
                        self.capture(ally);
                        self.ally_healing[actor] += healed;
                        if healed > 0.0 { self.record("allyHeal", actor, ally, healed); }
                    }
                }
                TeamEffect::Stun(_, _) | TeamEffect::Cast(_) | TeamEffect::ManaReave(_, _) => {}
                TeamEffect::HealAllies(amount, count) => {
                    let mut eligible = (0..self.actors.len()).filter(|&i| i != actor && self.actors[i].status().alive).collect::<Vec<_>>();
                    eligible.sort_by(|&a, &b| {
                        let sa = self.actors[a].status(); let sb = self.actors[b].status();
                        (sa.hp / sa.max_hp).total_cmp(&(sb.hp / sb.max_hp)).then(a.cmp(&b))
                    });
                    for ally in eligible.into_iter().take(count) {
                        let healed = self.actors[ally].heal(self.time, amount);
                        self.capture(ally);
                        self.ally_healing[actor] += healed;
                        if healed > 0.0 { self.record("allyHeal", actor, ally, healed); }
                    }
                }
                TeamEffect::Shield(amount, duration) => {
                    let target = (0..self.actors.len()).filter(|&i| i != actor && !shielded.contains(&i) && self.actors[i].status().alive)
                        .min_by(|&a, &b| {
                            let sa = self.actors[a].status(); let sb = self.actors[b].status();
                            (sa.hp / sa.max_hp).total_cmp(&(sb.hp / sb.max_hp)).then(a.cmp(&b))
                        });
                    if let Some(target) = target {
                        shielded.push(target);
                        let amount = self.actors[target].shield(self.time, amount, duration);
                        self.capture(target);
                        self.ally_shielding[actor] += amount;
                        self.record("allyShield", actor, target, amount);
                    }
                }
                TeamEffect::Burn(target, pct, duration, stacked) => {
                    if pct <= 0.0 || duration <= 0.0 { continue; }
                    let owners = &mut self.burns[target];
                    // Inferno is a separate refreshing burn channel, additive
                    // with ordinary burn. Repeated hits/providers do not add
                    // more stacks inside either channel (same as Fight::burn).
                    if let Some(entry) = owners.burns.iter_mut().find(|entry|
                        entry.0 == actor && entry.1 == stacked && entry.2 == pct && entry.4 >= self.time) {
                        entry.4 = entry.4.max(self.time + duration);
                    } else { owners.burns.push((actor, stacked, pct, self.time, self.time + duration)); }
                    owners.wound_until = owners.wound_until.max(self.time + duration);
                }
                TeamEffect::Reduction(target, pct, duration, shred) => {
                    if pct <= 0.0 || duration <= 0.0 { continue; }
                    let entries = &mut self.burns[target].reductions;
                    if let Some(entry) = entries.iter_mut().find(|entry| entry.0 == actor && entry.1 == shred && entry.2 == pct) {
                        entry.3 = entry.3.max(self.time + duration);
                    } else { entries.push((actor, shred, pct, self.time + duration)); }
                }
            }
        }
        self.refresh_statuses();
        self.retarget();
    }

    fn refresh_statuses(&mut self) {
        for (target, effects) in self.targets.iter_mut().zip(&mut self.burns) {
            effects.reductions.retain(|entry| entry.3 > self.time);
            target.sunder = 0.0; target.shred = 0.0;
            target.sunder_until = self.time; target.shred_until = self.time;
            for &(_, shred, pct, until) in &effects.reductions {
                let (value, expires) = if shred { (&mut target.shred, &mut target.shred_until) }
                    else { (&mut target.sunder, &mut target.sunder_until) };
                if pct > *value { *value = pct; *expires = until; }
                else if pct == *value { *expires = expires.max(until); }
            }
            target.burn_pct = 0.0; target.burn_stack = 0.0;
            target.burn_until = self.time; target.burn_stack_until = self.time;
            effects.ordinary = None; effects.stacked = None;
            for &(source, stacked, pct, start, until) in &effects.burns {
                if start > self.time || until <= self.time { continue; }
                if stacked {
                    if pct > target.burn_stack {
                        target.burn_stack = pct; target.burn_stack_until = until; effects.stacked = Some(source);
                    }
                } else if pct > target.burn_pct {
                    target.burn_pct = pct; target.burn_until = until; effects.ordinary = Some(source);
                }
            }
        }
    }

    fn burn_and_heal(&mut self) {
        for target in 0..self.targets.len() {
            if !self.targets[target].alive { continue; }
            // Integrate the strongest ordinary burn over the actual part of
            // this tick it existed. Stack-changing hits also partition time.
            let start = self.time - TICK_S;
            let entries = self.burns[target].burns.clone();
            let mut boundaries = vec![start, self.time];
            for &(_, _, _, from, until) in &entries {
                if from > start && from < self.time { boundaries.push(from); }
                if until > start && until < self.time { boundaries.push(until); }
            }
            boundaries.sort_by(f64::total_cmp);
            boundaries.dedup();
            let mut amounts = vec![0.0; self.actors.len()];
            for span in boundaries.windows(2) {
                let mid = (span[0] + span[1]) / 2.0;
                let mut ordinary: Option<(usize, f64)> = None;
                let mut inferno: Option<(usize, f64)> = None;
                for &(source, stacked, pct, from, until) in &entries {
                    if from > mid || until <= mid { continue; }
                    let channel = if stacked { &mut inferno } else { &mut ordinary };
                    if channel.is_none_or(|(_, value)| pct > value) { *channel = Some((source, pct)); }
                }
                if let Some((source, pct)) = ordinary { amounts[source] += pct * (span[1] - span[0]); }
                if let Some((source, pct)) = inferno { amounts[source] += pct * (span[1] - span[0]); }
            }
            for (actor, fraction) in amounts.into_iter().enumerate() {
                if fraction > 0.0 && self.targets[target].alive {
                    self.sync(actor);
                    self.actors[actor].burn_tick(self.time, target, fraction * self.targets[target].max_hp);
                    self.commit(actor);
                }
            }
            self.burns[target].burns.retain(|entry| entry.4 > self.time);
            if self.targets[target].alive {
                let wound = if self.burns[target].wound_until > self.time { self.specs.burn_wound } else { 0.0 };
                let enemy = &mut self.targets[target];
                let healed = (self.specs.enemies[target].healing * TICK_S * (1.0 - wound))
                    .min((enemy.max_hp - enemy.hp).max(0.0));
                enemy.hp += healed;
                if healed > 0.0 { self.record("enemyHeal", target, target, healed); }
            }
        }
    }

    fn enemy_hit(&mut self, enemy: usize, amount: f64, dtype: DType, attack: bool, fixed_target: Option<usize>) {
        self.retarget();
        let Some(target) = fixed_target.or(self.enemies[enemy].focus) else { return; };
        let status = self.actors[target].status();
        if !self.targets[enemy].alive || !status.holding || status.untargetable > self.time { return; }
        self.sync(target);
        let debuffs = self.specs.enemies[enemy].debuffs;
        let until = self.time + self.specs.enemies[enemy].debuff_duration;
        let before = self.actors[target].status().hp;
        self.actors[target].receive(self.time, amount, dtype, enemy, attack, debuffs, until);
        let after = self.actors[target].status().hp;
        self.record(if attack { "enemyAttack" } else { "enemySpell" }, enemy, target, (before - after).max(0.0));
        self.commit(target);
    }

    fn run(&mut self) -> &str {
        self.retarget();
        for i in 0..self.actors.len() {
            self.sync(i);
            self.commit(i); // Any driver initialization ally effects.
        }
        let mut world_tick = TICK_S;
        loop {
            if !self.targets.iter().any(|target| target.alive) { return "win"; }
            if !self.actors.iter().any(|actor| actor.status().holding) { return "loss"; }
            if self.time >= self.specs.duration { return "timeout"; }
            let mut next = world_tick;
            for actor in &self.actors { next = next.min(actor.next_event()); }
            for (i, enemy) in self.enemies.iter().enumerate() {
                if self.targets[i].alive { next = next.min(enemy.attack).min(enemy.cast); }
            }
            if next > self.specs.duration { self.time = self.specs.duration; return "timeout"; }
            self.time = next;
            self.retarget();
            if world_tick <= self.time + EPS {
                self.burn_and_heal();
                world_tick += TICK_S;
            }
            for i in 0..self.actors.len() {
                if self.actors[i].next_event() <= self.time + EPS && self.targets.iter().any(|target| target.alive) {
                    self.sync(i);
                    self.actors[i].advance(self.time);
                    self.commit(i);
                }
            }
            for enemy in 0..self.enemies.len() {
                if !self.targets[enemy].alive { continue; }
                if self.enemies[enemy].attack <= self.time + EPS {
                    self.enemies[enemy].attack += 1.0 / self.specs.enemies[enemy].slot.as_;
                    if self.targets[enemy].stunned_until <= self.time {
                        self.enemies[enemy].attacks += 1;
                        self.enemy_hit(enemy, self.specs.enemies[enemy].slot.ad * self.specs.crit_ev, DType::Physical, true, None);
                    }
                }
                if self.targets[enemy].alive && self.enemies[enemy].cast <= self.time + EPS {
                    if self.targets[enemy].stunned_until > self.time {
                        self.enemies[enemy].cast = self.targets[enemy].stunned_until;
                    } else {
                        self.enemies[enemy].cast = self.time + self.specs.enemies[enemy].slot.cast_interval;
                        self.enemies[enemy].casts += 1;
                        self.retarget();
                        let target = self.enemies[enemy].focus;
                        let original_alive = target.map(|target| self.actors[target].status().alive);
                        // Spell-triggered item damage occurs once per enemy
                        // cast, including Spark holders who are not its target.
                        for actor in 0..self.actors.len() {
                            if !self.actors[actor].status().alive || !self.targets[enemy].alive { continue; }
                            self.sync(actor);
                            self.actors[actor].enemy_cast(self.time, enemy);
                            self.commit(actor);
                        }
                        let amount = self.specs.enemies[enemy].slot.ability;
                        let physical = self.specs.enemies[enemy].slot.phys_share;
                        if let Some(target) = target {
                            if physical > 0.0 { self.enemy_hit(enemy, amount * physical, DType::Physical, false, Some(target)); }
                            if physical < 1.0 && Some(self.actors[target].status().alive) == original_alive {
                                self.enemy_hit(enemy, amount * (1.0 - physical), DType::Magic, false, Some(target));
                            }
                        }
                    }
                }
            }
            self.retarget();
        }
    }
}

/// Real champion action timelines against shared, killable enemy sources.
#[pyfunction]
#[pyo3(signature = (spec, trace=false))]
pub(crate) fn simulate_team<'py>(py: Python<'py>, spec: &Bound<'py, PyDict>, trace: bool)
    -> PyResult<Bound<'py, PyDict>> {
    let mut spec = TeamSpec::from_py(spec)?;
    // Team targets and pressure belong exclusively to the encounter.
    // Never inherit the independent benchmark's immortal or free-debuff flags.
    let dummies = spec.enemies.iter().map(|enemy| enemy.slot.clone()).collect::<Vec<_>>();
    for ally in &mut spec.allies {
        ally.cell.dummies = dummies.clone();
        ally.cell.immortal = false;
        ally.cell.pressure = true;
        ally.cell.duration = spec.duration;
        ally.cell.clump = spec.clump;
        ally.cell.enemy_debuffs = EnemyDebuffs::default();
        ally.cell.target_debuffs = Default::default();
    }
    let mut encounter = Encounter::new(&spec, trace)?;
    let outcome = encounter.run().to_owned();
    let out = PyDict::new(py);
    let elapsed = encounter.time;
    let statuses = encounter.actors.iter().map(|actor| actor.status()).collect::<Vec<_>>();
    let ally_hp = statuses.iter().map(|s| s.hp.max(0.0)).sum::<f64>();
    let ally_max = statuses.iter().map(|s| s.max_hp).sum::<f64>();
    let enemy_hp = encounter.targets.iter().map(|enemy| enemy.hp.max(0.0)).sum::<f64>();
    let enemy_max = encounter.targets.iter().map(|enemy| enemy.max_hp).sum::<f64>();
    out.set_item("outcome", outcome)?;
    out.set_item("duration", elapsed)?;
    out.set_item("allyHpLeft", ally_hp)?;
    out.set_item("allyHpFraction", (ally_hp / ally_max.max(1.0)).clamp(0.0, 1.0))?;
    out.set_item("enemyHpLeft", enemy_hp)?;
    out.set_item("enemyHpFraction", (enemy_hp / enemy_max.max(1.0)).clamp(0.0, 1.0))?;
    out.set_item("frontlineTime", encounter.frontline_time.unwrap_or(elapsed))?;
    let mut damage = 0.0;
    let allies = PyList::empty(py);
    let timelines = PyList::empty(py);
    for (i, actor) in encounter.actors.iter_mut().enumerate() {
        let result = actor.result(elapsed);
        damage += result.total;
        let unit = PyDict::new(py);
        unit.set_item("api", &spec.allies[i].cell.unit.api)?;
        unit.set_item("name", &spec.allies[i].cell.unit.name)?;
        unit.set_item("damage", result.total)?;
        unit.set_item("dps", result.total / elapsed.max(TICK_S))?;
        unit.set_item("aliveTime", result.alive_time.min(elapsed))?;
        unit.set_item("alive", statuses[i].alive)?;
        unit.set_item("damageTaken", result.taken)?;
        unit.set_item("healing", result.healed)?;
        unit.set_item("shielding", result.shielded)?;
        unit.set_item("allyHealing", encounter.ally_healing[i])?;
        unit.set_item("allyShielding", encounter.ally_shielding[i])?;
        unit.set_item("attacks", result.attacks)?;
        unit.set_item("casts", result.casts)?;
        allies.append(unit)?;
    }
    out.set_item("damage", damage)?;
    out.set_item("damageDps", damage / elapsed.max(TICK_S))?;
    out.set_item("allies", allies)?;
    let enemies = PyList::empty(py);
    for (i, enemy) in encounter.targets.iter().enumerate() {
        let row = PyDict::new(py);
        row.set_item("name", &spec.enemies[i].name)?;
        row.set_item("hp", enemy.hp.max(0.0))?; row.set_item("maxHp", enemy.max_hp)?;
        row.set_item("alive", enemy.alive)?;
        row.set_item("attacks", encounter.enemies[i].attacks)?;
        row.set_item("casts", encounter.enemies[i].casts)?;
        enemies.append(row)?;
    }
    out.set_item("enemies", enemies)?;
    if trace {
        for event in &encounter.trace {
            let e = PyDict::new(py);
            e.set_item("time", event.time)?; e.set_item("kind", event.kind)?;
            e.set_item("source", event.source)?; e.set_item("target", event.target)?;
            e.set_item("amount", event.amount)?;
            e.set_item("side", event.side)?;
            if let Some(name) = event.source_name { e.set_item("sourceName", name)?; }
            timelines.append(e)?;
        }
        out.set_item("trace", timelines)?;
    }
    Ok(out)
}
