//! One build's fight against the dummies: the port of tft.Sheet, tft.Dummy
//! and tft.Fight. Every method mirrors its Python original operation for
//! operation (the golden fixtures pin the bits), with the driver reached
//! through the `Driver` hooks and the dummies addressed by index.
//!
//! Driver-facing API (what tft_kits' docstring described, in Rust):
//!
//! Targets: `f.target()` is the dummy being attacked (`Option<usize>`);
//! `f.alive()` all that stand; `f.aoe(count, exclude_primary)` who an area
//! ability reaches (every standing dummy, up to `count`, in the clump; only
//! the current target spread out); `f.adjacent(attacker)` the same for
//! melee-range effects; `f.d(i)` / `f.dm(i)` the dummy itself.
//!
//! Damage: `f.hit_ability(calc, target, src, mult)` (and `_typed` /
//! `_rt` variants for an explicit damage type or runtime values) resolves
//! the calc at the unit's current stats; `f.hit_attack(target, mult, src)`
//! is a basic attack with on-hits; `f.dot_ability(calc, target, duration,
//! src, mult)` spreads it over time; `f.deal(amount, dtype, target, src,
//! Deal::…)` is raw. `f.calc(id)` / `f.row(id)` read the unit's numbers.
//! `f.stun(&targets, seconds)`, `f.sunder(d, pct, dur)`, `f.shred`, `f.burn`.
//!
//! The unit's body: `f.hp`, `f.max_hp()`, `f.hp_frac()`, `f.alive_unit`;
//! `f.heal(amount, src)`, `f.shield(amount, duration, src, decays)`,
//! `f.buff_resists`, `f.buff_durability`, `f.untargetable`, `f.gain_max_hp`,
//! `f.armor_extra` / `f.mr_extra`, `f.add_body`. Ally effects are counted:
//! `f.heal_ally`, `f.shield_ally`.
//!
//! Stats: `f.ad()`, `f.ap()`, `f.attack_speed()`; `f.buff_as(pct, duration)`
//! or `f.as_extra` + `f.as_extra_until`; `f.ad_extra`, `f.ap_extra`,
//! `f.amp_extra`. Mana: `f.mana`, `f.sheet.mana_max`, `f.lock_until`.
//! `f.after(delay, tag)` queues `Driver::event(f, tag)`.

use std::collections::HashMap;

use crate::driver::Driver;
use crate::fx::{combined_durability, Fx};
use crate::kit::{CalcId, DType, Kit, RowId, Runtime};
use crate::pyf::{pymax, pymin};
use crate::spec::{CellSpec, DummySpec, Kind};

pub const AS_CAP: f64 = 5.0;
pub const CRIT_EXCESS_TO_DAMAGE: f64 = 1.0;
pub const PRECISION_EXTRA_CRIT_DAMAGE: f64 = 0.10;
pub const MANA_LOCK_S: f64 = 1.0;
pub const CAST_TIME_DEFAULT: f64 = 0.25;
pub const TICK_S: f64 = 0.25;
pub const TANK_MANA_PER_PREMIT: f64 = 0.01;
pub const TANK_MANA_PER_POSTMIT: f64 = 0.03;
pub const TANK_MANA_PER_HIT_CAP: f64 = 42.5;
pub const ASSASSIN_OFFTARGET_REDUCTION: f64 = 0.15;
pub const FIGHT_DURATION: f64 = 20.0;
pub const TANK_DURATION: f64 = 60.0;
pub const MAX_TARGETS: usize = 4;
pub const MAX_STREAMS: usize = 8;
const FAR: f64 = 1e18;

// ---------------------------------------------------------------------------
// the sheet
// ---------------------------------------------------------------------------

/// tft.Sheet: a unit's numbers for one fight, before dynamic stacks.
#[derive(Clone, Debug)]
pub struct Sheet {
    pub form: Option<crate::fx::Form>,
    pub kind: Kind,
    pub star: i64,
    pub base_ad: f64,
    pub ad_pct: f64,
    pub ap_flat: f64,
    pub adap_mult: f64,
    pub base_as: f64,
    pub as_pct: f64,
    pub max_hp: f64,
    pub armor: f64,
    pub mr: f64,
    pub crit_chance: f64,
    pub crit_mult: f64,
    pub precision: bool,
    pub mana_max: f64,
    pub mana_start: f64,
    pub mana_per_attack: f64,
    pub range: f64,
    pub omnivamp: f64,
    pub durability: f64,
}

impl Sheet {
    pub fn new(spec: &CellSpec, kit: &Kit, fx: &Fx) -> Sheet {
        let s = &kit.stats;
        let crit = s.crit_chance + fx.crit;
        let excess = pymax(0.0, crit - 1.0);
        let extra_precision = if fx.precision - 1 > 0 { fx.precision - 1 } else { 0 };
        Sheet {
            form: fx.form,
            kind: spec.unit.kind,
            star: spec.star,
            base_ad: kit.base_ad,
            ad_pct: fx.ad_pct,
            ap_flat: crate::kit::BASE_AP + fx.ap,
            adap_mult: fx.adap_mult,
            base_as: s.as_,
            as_pct: fx.as_pct,
            max_hp: (kit.hp_star + fx.hp) * fx.hp_mult,
            armor: s.armor + fx.armor,
            mr: s.mr + fx.mr,
            crit_chance: pymin(crit, 1.0),
            crit_mult: s.crit_mult + fx.crit_dmg + excess * CRIT_EXCESS_TO_DAMAGE
                + (extra_precision as f64) * PRECISION_EXTRA_CRIT_DAMAGE,
            precision: fx.precision > 0,
            mana_max: s.mana,
            mana_start: s.initial_mana + fx.starting_mana,
            mana_per_attack: spec.unit.kind.mana_per_attack(),
            range: s.range,
            omnivamp: fx.omnivamp,
            durability: fx.durability(),
        }
    }

    #[inline]
    pub fn ad(&self, ad_pct_extra: f64) -> f64 {
        self.base_ad * (1.0 + self.ad_pct + ad_pct_extra) * self.adap_mult
    }

    #[inline]
    pub fn ap(&self, ap_extra: f64) -> f64 {
        (self.ap_flat + ap_extra) * self.adap_mult
    }

    #[inline]
    pub fn attack_speed(&self, as_extra: f64) -> f64 {
        pymin(self.base_as * (1.0 + self.as_pct + as_extra), AS_CAP)
    }

    #[inline]
    pub fn crit_ev(&self) -> f64 {
        1.0 + self.crit_chance * (self.crit_mult - 1.0)
    }
}

// ---------------------------------------------------------------------------
// the dummies
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct Dot {
    pub dps: f64,
    pub until: f64,
    pub dtype: DType,
    pub src: &'static str,
    pub start: f64,
    pub ability: bool,
}

/// tft.Dummy: a health pool with resists — and, when the fight has the
/// dummies hitting back, its group's median attacks and ability.
#[derive(Clone, Debug)]
pub struct Dummy {
    pub hp: f64,
    pub max_hp: f64,
    pub armor: f64,
    pub mr: f64,
    pub sunder: f64,
    pub sunder_until: f64,
    pub shred: f64,
    pub shred_until: f64,
    pub armor_flat: f64,
    pub mr_flat: f64,
    pub burn_pct: f64,
    pub burn_until: f64,
    pub burn_stack: f64,
    /// `marks["burn_stack_until"]` in Python.
    pub burn_stack_until: f64,
    pub dots: Vec<Dot>,
    pub alive: bool,
    pub died_at: Option<f64>,
    pub is_tank: bool,
    pub immortal: bool,
    pub ad: f64,
    pub as_: f64,
    pub crit_ev: f64,
    pub next_attacks: [f64; MAX_STREAMS],
    pub n_streams: usize,
    pub ability: f64,
    pub phys_share: f64,
    pub mana: f64,
    pub mana_max: f64,
    pub mana_per_attack: f64,
    pub mana_from_damage: bool,
    pub lock_until: f64,
    pub stunned_until: f64,
    pub attacks: i64,
    pub casts: i64,
    /// Driver scratch (Python's `d.marks`): a flag, a number, a list of times.
    pub mark: bool,
    pub mark_n: f64,
    pub mark_times: Vec<f64>,
}

impl Dummy {
    pub fn new(hp: f64, armor: f64, mr: f64, is_tank: bool) -> Dummy {
        Dummy {
            hp, max_hp: hp, armor, mr, is_tank,
            sunder: 0.0, sunder_until: 0.0, shred: 0.0, shred_until: 0.0,
            armor_flat: 0.0, mr_flat: 0.0, burn_pct: 0.0, burn_until: 0.0, burn_stack: 0.0,
            burn_stack_until: 0.0, dots: Vec::new(), alive: true, died_at: None,
            immortal: false, ad: 0.0, as_: 0.0, crit_ev: 1.0,
            next_attacks: [0.0; MAX_STREAMS], n_streams: 0, ability: 0.0, phys_share: 1.0,
            mana: 0.0, mana_max: 0.0, mana_per_attack: 0.0, mana_from_damage: false,
            lock_until: 0.0, stunned_until: 0.0, attacks: 0, casts: 0,
            mark: false, mark_n: 0.0, mark_times: Vec::new(),
        }
    }

    /// Dummy.arm: the group's offense, attack timers staggered per stream.
    pub fn arm(&mut self, s: &DummySpec, crit_ev: f64, streams: i64) {
        self.ad = s.ad;
        self.as_ = s.as_;
        self.crit_ev = crit_ev;
        if self.as_ > 0.0 && self.ad > 0.0 {
            let period = 1.0 / self.as_;
            let n = streams as usize;
            assert!(n <= MAX_STREAMS, "too many attack streams");
            for i in 0..n {
                self.next_attacks[i] = period * ((i + 1) as f64) / (streams as f64);
            }
            self.n_streams = n;
        } else {
            self.n_streams = 0;
        }
        self.ability = s.ability;
        self.phys_share = s.phys_share;
        self.mana_max = s.mana_max;
        self.mana = s.mana_start;
        self.mana_per_attack = s.mana_per_attack;
        self.mana_from_damage = s.mana_from_damage;
    }

    #[inline]
    pub fn gain_mana(&mut self, amount: f64, t: f64) {
        if t >= self.lock_until && self.mana_max > 0.0 {
            self.mana += amount;
        }
    }

    #[inline]
    pub fn streams(&self) -> usize {
        self.n_streams
    }

    #[inline]
    fn next_event(&self) -> f64 {
        let mut t = FAR;
        // Python: min(self.next_attacks) — the first minimal value
        for i in 0..self.n_streams {
            let x = self.next_attacks[i];
            if i == 0 || x < t {
                t = x;
            }
        }
        t
    }

    /// tft.Fight._is_burning as the drivers spell it: an item burn or a
    /// trait's stacking burn still running.
    #[inline]
    pub fn burning(&self, t: f64) -> bool {
        (t <= self.burn_until && self.burn_pct > 0.0)
            || (self.burn_stack > 0.0 && t <= self.burn_stack_until)
    }
}

/// tft.make_dummies.
pub fn make_dummies(spec: &CellSpec) -> Vec<Dummy> {
    let mut out = Vec::with_capacity(spec.dummies.len());
    for s in &spec.dummies {
        let mut d = Dummy::new(s.hp, s.armor, s.mr, s.is_tank);
        d.immortal = spec.immortal;
        if spec.pressure {
            d.arm(s, spec.crit_ev, s.streams);
        }
        out.push(d);
    }
    out
}

#[inline]
pub fn resist_mult(r: f64) -> f64 {
    if r >= 0.0 { 100.0 / (100.0 + r) } else { 2.0 - 100.0 / (100.0 - r) }
}

// ---------------------------------------------------------------------------
// target selections
// ---------------------------------------------------------------------------

/// A few dummy indices, in order (what Python's lists of dummies were).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Sel {
    n: usize,
    ids: [usize; MAX_TARGETS],
}

impl Sel {
    pub fn push(&mut self, i: usize) {
        self.ids[self.n] = i;
        self.n += 1;
    }

    #[inline]
    pub fn len(&self) -> usize {
        self.n
    }

    #[inline]
    pub fn is_empty(&self) -> bool {
        self.n == 0
    }

    #[inline]
    pub fn get(&self, i: usize) -> usize {
        self.ids[i]
    }

    pub fn first(&self) -> Option<usize> {
        if self.n > 0 { Some(self.ids[0]) } else { None }
    }

    pub fn last(&self) -> Option<usize> {
        if self.n > 0 { Some(self.ids[self.n - 1]) } else { None }
    }

    pub fn iter(&self) -> impl Iterator<Item = usize> + '_ {
        self.ids[..self.n].iter().copied()
    }

    pub fn contains(&self, i: usize) -> bool {
        self.iter().any(|x| x == i)
    }

    /// Python `al[1:]`.
    pub fn tail(&self) -> Sel {
        let mut s = Sel::default();
        for i in self.iter().skip(1) {
            s.push(i);
        }
        s
    }

    /// Python `al[:n]`.
    pub fn take(&self, n: usize) -> Sel {
        let mut s = Sel::default();
        for i in self.iter().take(n) {
            s.push(i);
        }
        s
    }

    pub fn one(i: usize) -> Sel {
        let mut s = Sel::default();
        s.push(i);
        s
    }
}

// ---------------------------------------------------------------------------
// the fight
// ---------------------------------------------------------------------------

/// How `deal` treats the damage: ability damage crits only with Precision;
/// `raw` skips amp, Solar and bleeds (Python's `ability=`, `crit=`, `raw=`).
#[derive(Clone, Copy, Debug)]
pub struct Deal {
    pub ability: bool,
    pub crit: bool,
    pub raw: bool,
}

impl Deal {
    /// Python's defaults: ability damage that may crit (with Precision).
    pub const ABILITY: Deal = Deal { ability: true, crit: true, raw: false };
    /// `ability=True, crit=False`.
    pub const ABILITY_NOCRIT: Deal = Deal { ability: true, crit: false, raw: false };
    /// `ability=False, crit=False`: burns, thorns, executes.
    pub const PLAIN: Deal = Deal { ability: false, crit: false, raw: false };
    /// `ability=False, crit=False, raw=True`: Solar's bonus.
    pub const RAW: Deal = Deal { ability: false, crit: false, raw: true };
}

#[derive(Clone, Copy, Debug)]
pub enum Event {
    /// The engine's own: the ability lands when its cast time is up.
    Cast,
    /// A driver's, dispatched to `Driver::event`.
    Driver(u32),
}

#[derive(Clone, Debug)]
pub struct Shield {
    pub amount: f64,
    pub until: f64,
    pub decay: f64,
    /// Dropped by the tick (spent or expired): Python removed it from the
    /// list; it stays here so a driver watching it still reads its end.
    pub dead: bool,
}

#[derive(Clone, Debug)]
pub struct Body {
    pub hp: f64,
    pub armor: f64,
    pub mr: f64,
    pub name: &'static str,
}

/// A recorded fight event, for tests and `lol.py tft sim --trace`.
#[derive(Clone, Debug)]
pub struct Ev {
    pub t: f64,
    pub kind: &'static str,
    pub amount: f64,
    pub target: i64,
    pub src: &'static str,
}

pub struct Fight<'a, D: Driver> {
    pub kit: &'a Kit,
    pub unit_cast_time: Option<f64>,
    pub sheet: Sheet,
    pub fx: Fx,
    pub targets: Vec<Dummy>,
    pub clump: bool,
    pub duration: f64,
    pub pressure: bool,
    pub t: f64,
    pub mana: f64,
    pub lock_until: f64,
    pub casting_until: f64,
    pub next_attack: f64,
    pub attacks: i64,
    pub casts: i64,
    pub cast_times: Vec<f64>,
    pub total: f64,
    pub raw_total: f64,
    pub breakdown: Vec<(&'static str, f64)>,
    pub kill_time: Option<f64>,
    pub cur: usize,
    pub target_since: f64,
    pub as_stack: f64,
    pub as_attack_stack: f64,
    pub ad_stack: f64,
    pub ad_stack_n: i64,
    pub adap_stack_n: i64,
    pub rapidfire_n: i64,
    pub ap_stack: f64,
    pub amp_stacks: Vec<(f64, f64, usize)>,
    pending: Vec<(f64, u64, Event)>,
    seq: u64,
    pub amp_extra: f64,
    pub as_extra_until: f64,
    pub as_extra: f64,
    pub ad_extra: f64,
    pub ap_extra: f64,
    pub max_hp_extra: f64,
    pub hp: f64,
    pub alive_unit: bool,
    pub died_at: Option<f64>,
    pub hold_until: Option<f64>,
    pub body: Option<Body>,
    pub bodies: Vec<Body>,
    pub shields: Vec<Shield>,
    pub absorbed: f64,
    pub taken: f64,
    pub mitigated: f64,
    pub healed: f64,
    pub shield_used: f64,
    pub denied: f64,
    pub ally_heal: f64,
    pub ally_shield: f64,
    pub cc_time: f64,
    pub hits_taken: i64,
    pub untargetable_until: f64,
    pub resist_buffs: Vec<(f64, f64, f64)>,
    pub dur_buffs: Vec<(f64, f64)>,
    pub armor_extra: f64,
    pub mr_extra: f64,
    fired_shield: u64,
    fired_mana: u64,
    fired_untargetable: u64,
    fired_fae: bool,
    thorns_ready: Vec<f64>,
    pub drv: D,
    pub trace: Option<Vec<Ev>>,
}

impl<'a, D: Driver> Fight<'a, D> {
    pub fn new(spec: &CellSpec, kit: &'a Kit, sheet: Sheet, fx: Fx, dummies: Vec<Dummy>,
               drv: D) -> Fight<'a, D> {
        let mana = if sheet.mana_max > 0.0 { pymin(sheet.mana_start, sheet.mana_max) }
                   else { sheet.mana_start };
        let hp = sheet.max_hp;
        let thorns_ready = vec![0.0; fx.thorns.len()];
        let mut f = Fight {
            kit,
            unit_cast_time: spec.unit.cast_time,
            sheet,
            fx,
            targets: dummies,
            clump: spec.clump,
            duration: spec.duration,
            pressure: spec.pressure,
            t: 0.0,
            mana,
            lock_until: 0.0,
            casting_until: 0.0,
            next_attack: 0.0,
            attacks: 0,
            casts: 0,
            cast_times: Vec::new(),
            total: 0.0,
            raw_total: 0.0,
            breakdown: Vec::new(),
            kill_time: None,
            cur: 0,
            target_since: 0.0,
            as_stack: 0.0,
            as_attack_stack: 0.0,
            ad_stack: 0.0,
            ad_stack_n: 0,
            adap_stack_n: 0,
            rapidfire_n: 0,
            ap_stack: 0.0,
            amp_stacks: Vec::new(),
            pending: Vec::new(),
            seq: 0,
            amp_extra: 0.0,
            as_extra_until: 0.0,
            as_extra: 0.0,
            ad_extra: 0.0,
            ap_extra: 0.0,
            max_hp_extra: 0.0,
            hp,
            alive_unit: true,
            died_at: None,
            hold_until: None,
            body: None,
            bodies: Vec::new(),
            shields: Vec::new(),
            absorbed: 0.0,
            taken: 0.0,
            mitigated: 0.0,
            healed: 0.0,
            shield_used: 0.0,
            denied: 0.0,
            ally_heal: 0.0,
            ally_shield: 0.0,
            cc_time: 0.0,
            hits_taken: 0,
            untargetable_until: 0.0,
            resist_buffs: Vec::new(),
            dur_buffs: Vec::new(),
            armor_extra: 0.0,
            mr_extra: 0.0,
            fired_shield: 0,
            fired_mana: 0,
            fired_untargetable: 0,
            fired_fae: false,
            thorns_ready,
            drv,
            trace: None,
        };
        let sunder_aura = f.fx.sunder_aura;
        let shred_aura = f.fx.shred_aura;
        for d in f.targets.iter_mut() {
            if sunder_aura != 0.0 {
                d.sunder = pymax(d.sunder, sunder_aura);
                d.sunder_until = 1e9;
            }
            if shred_aura != 0.0 {
                d.shred = pymax(d.shred, shred_aura);
                d.shred_until = 1e9;
            }
        }
        let starts: Vec<(f64, f64)> = f.fx.shield_at_start.clone();
        for (pct, dur) in starts {
            let amount = pct * f.max_hp();
            f.shield(amount, dur, "combat start", false);
        }
        let resists: Vec<(f64, f64, f64)> = f.fx.resists_at_start.clone();
        for (a, m, dur) in resists {
            f.resist_buffs.push((a, m, dur));
        }
        f
    }

    #[inline]
    fn record(&mut self, kind: &'static str, amount: f64, target: Option<usize>, src: &'static str) {
        if let Some(tr) = &mut self.trace {
            tr.push(Ev { t: self.t, kind, amount,
                         target: target.map(|i| i as i64).unwrap_or(-1), src });
        }
    }

    pub fn default_cast_time(&self) -> f64 {
        match self.unit_cast_time {
            Some(ct) => ct,
            None => CAST_TIME_DEFAULT,
        }
    }

    // ---- targets ---------------------------------------------------------

    #[inline]
    pub fn d(&self, i: usize) -> &Dummy {
        &self.targets[i]
    }

    #[inline]
    pub fn dm(&mut self, i: usize) -> &mut Dummy {
        &mut self.targets[i]
    }

    pub fn alive(&self) -> Sel {
        let mut s = Sel::default();
        for (i, d) in self.targets.iter().enumerate() {
            if d.alive {
                s.push(i);
            }
        }
        s
    }

    /// The members of `sel` still standing.
    pub fn alive_of(&self, sel: &Sel) -> Sel {
        let mut s = Sel::default();
        for i in sel.iter() {
            if self.targets[i].alive {
                s.push(i);
            }
        }
        s
    }

    pub fn target(&self) -> Option<usize> {
        self.targets.iter().position(|d| d.alive)
    }

    /// Fight.aoe: in the clump, up to `count` of the standing dummies (all
    /// if None); spread out, only the one being attacked.
    pub fn aoe(&self, count: Option<f64>, exclude_primary: bool) -> Sel {
        let al = self.alive();
        if al.is_empty() {
            return al;
        }
        if !self.clump {
            return if exclude_primary { Sel::default() } else { al.take(1) };
        }
        let al = if exclude_primary { al.tail() } else { al };
        match count {
            None => al,
            Some(c) => al.take(crate::pyf::pyint(c).max(0) as usize),
        }
    }

    pub fn aoe_all(&self) -> Sel {
        self.aoe(None, false)
    }

    /// Fight.adjacent: everyone standing in the clump, only the current
    /// target (or the given attacker) spread out.
    pub fn adjacent(&self, attacker: Option<usize>) -> Sel {
        if self.clump {
            return self.alive();
        }
        let d = match attacker {
            Some(a) if self.targets[a].alive => Some(a),
            _ => self.target(),
        };
        match d {
            Some(i) => Sel::one(i),
            None => Sel::default(),
        }
    }

    /// Python `min(sel, key=...)`: the first minimal.
    pub fn min_by(&self, sel: &Sel, key: impl Fn(&Dummy) -> f64) -> Option<usize> {
        let mut best: Option<(usize, f64)> = None;
        for i in sel.iter() {
            let k = key(&self.targets[i]);
            match best {
                Some((_, bk)) if !(k < bk) => {}
                _ => best = Some((i, k)),
            }
        }
        best.map(|(i, _)| i)
    }

    // ---- stats now --------------------------------------------------------

    fn hoj(&self) -> (f64, f64, f64) {
        let (mut ad, mut ap, mut omni) = (0.0, 0.0, 0.0);
        let frac = self.hp_frac();
        for h in &self.fx.hojs {
            let above = frac >= h.3;
            ad += h.0 * if above { 2.0 } else { 1.0 };
            ap += h.1 * if above { 2.0 } else { 1.0 };
            omni += h.2 * if above { 1.0 } else { 2.0 };
        }
        (ad, ap, omni)
    }

    pub fn ad(&self) -> f64 {
        self.sheet.ad(self.ad_stack + self.ad_extra + self.adap_bonus() + self.hoj().0)
    }

    pub fn ap(&self) -> f64 {
        self.sheet.ap(self.ap_stack + self.ap_extra + self.adap_bonus() * 100.0 + self.hoj().1)
    }

    /// Titan's: each copy's share per stack, each up to its own cap.
    pub fn adap_bonus(&self) -> f64 {
        let mut s = 0.0;
        for &(p, mx, _) in &self.fx.adap_per_attack {
            s += p * pymin(self.adap_stack_n as f64, mx);
        }
        s
    }

    fn adap_stack(&mut self) {
        if !self.fx.adap_per_attack.is_empty() {
            let mut cap = self.fx.adap_per_attack[0].1;
            for &(_, mx, _) in &self.fx.adap_per_attack[1..] {
                cap = pymax(cap, mx);
            }
            let cap = crate::pyf::pyint(cap);
            self.adap_stack_n = (self.adap_stack_n + 1).min(cap);
        }
    }

    pub fn omnivamp(&self) -> f64 {
        self.sheet.omnivamp + self.hoj().2
    }

    pub fn attack_speed(&self) -> f64 {
        let mut extra = self.as_stack + self.as_attack_stack;
        if self.t < self.as_extra_until {
            extra += self.as_extra;
        }
        for &(_, mx, as_at_max) in &self.fx.ad_per_attack {
            if (self.ad_stack_n as f64) >= mx {
                extra += as_at_max;
            }
        }
        self.sheet.attack_speed(extra)
    }

    pub fn amp(&self, target: usize) -> f64 {
        let tg = &self.targets[target];
        let mut a = self.fx.amp + self.amp_extra;
        if tg.is_tank {
            a += self.fx.amp_vs_tank;
        }
        for &(amp, until, _) in &self.amp_stacks {
            if self.t < until {
                a += amp;
            }
        }
        if let Some((amp, secs)) = self.fx.amp_after_same_target {
            if target == self.cur && self.t - self.target_since >= secs {
                a += amp;
            }
        }
        for &(_, mx, amp_at_max) in &self.fx.adap_per_attack {
            if (self.adap_stack_n as f64) >= mx {
                a += amp_at_max;
            }
        }
        if let Some((amp, thr, mult)) = self.fx.ravager {
            a += amp * if tg.hp < thr * tg.max_hp { mult } else { 1.0 };
        }
        1.0 + a
    }

    // ---- the unit's body ----------------------------------------------------

    #[inline]
    pub fn max_hp(&self) -> f64 {
        self.sheet.max_hp + self.max_hp_extra
    }

    pub fn hp_frac(&self) -> f64 {
        let m = self.max_hp();
        if m > 0.0 { self.hp / m } else { 0.0 }
    }

    pub fn attackers(&self) -> i64 {
        if !self.pressure {
            return 0;
        }
        self.targets.iter().filter(|d| d.alive).map(|d| d.streams() as i64).sum()
    }

    pub fn armor_now(&self) -> f64 {
        let mut a = self.sheet.armor + self.armor_extra;
        for &(ar, _, until) in &self.resist_buffs {
            if self.t < until {
                a += ar;
            }
        }
        a + self.fx.resists_per_attacker[0] * (self.attackers() as f64)
    }

    pub fn mr_now(&self) -> f64 {
        let mut m = self.sheet.mr + self.mr_extra;
        for &(_, mr, until) in &self.resist_buffs {
            if self.t < until {
                m += mr;
            }
        }
        m + self.fx.resists_per_attacker[1] * (self.attackers() as f64)
    }

    pub fn durability_now(&self) -> f64 {
        let frac = self.hp_frac();
        let it = self.fx.durabilities.iter().copied()
            .chain(self.fx.durability_by_health.iter()
                   .map(|&(below, above, thr)| if frac >= thr { above } else { below }))
            .chain(self.dur_buffs.iter().filter(|&&(_, until)| self.t < until).map(|&(p, _)| p));
        pymin(combined_durability(it), 0.99)
    }

    #[inline]
    pub fn holding(&self) -> bool {
        self.alive_unit || self.body.is_some()
    }

    /// Fight.take: incoming damage on the unit (or the body holding after
    /// its death): resists, Bramble's attack reduction, durability,
    /// shields, health. Returns the post-mitigation damage.
    pub fn take(&mut self, amount: f64, dtype: DType, attacker: Option<usize>, attack: bool) -> f64 {
        if amount <= 0.0 || !self.holding() {
            return 0.0;
        }
        let pre = amount;
        let mut amount = amount;
        if !self.alive_unit {
            let b = self.body.as_mut().expect("holding");
            let r = match dtype {
                DType::Physical => Some(b.armor),
                DType::Magic => Some(b.mr),
                DType::True => None,
            };
            if let Some(r) = r {
                amount *= resist_mult(pymax(r, 0.0));
            }
            self.absorbed += pre;
            self.taken += amount;
            self.mitigated += pre - amount;
            b.hp -= amount;
            if b.hp <= 0.0 {
                self.next_body();
            }
            self.record("take", amount, attacker, "body");
            return amount;
        }
        if self.sheet.kind == Kind::Assassin {
            if let Some(a) = attacker {
                if a != self.cur {
                    amount *= 1.0 - ASSASSIN_OFFTARGET_REDUCTION;
                }
            }
        }
        match dtype {
            DType::Physical => amount *= resist_mult(pymax(self.armor_now(), 0.0)),
            DType::Magic => amount *= resist_mult(pymax(self.mr_now(), 0.0)),
            DType::True => {}
        }
        if attack {
            amount *= self.fx.attack_damage_taken;
        }
        amount *= 1.0 - self.durability_now();
        let post = amount;
        let mut left = post;
        let t = self.t;
        for sh in self.shields.iter_mut() {
            if left <= 0.0 {
                break;
            }
            if sh.dead || sh.until <= t || sh.amount <= 0.0 {
                continue;
            }
            let use_ = pymin(sh.amount, left);
            sh.amount -= use_;
            left -= use_;
            self.shield_used += use_;
        }
        self.hp -= left;
        self.absorbed += pre;
        self.taken += post;
        self.mitigated += pre - post;
        self.hits_taken += 1;
        self.record("take", post, attacker, dtype.name());
        if self.sheet.kind == Kind::Tank && self.sheet.mana_max > 0.0 {
            let m = pymin(TANK_MANA_PER_HIT_CAP,
                          pre * TANK_MANA_PER_PREMIT + post * TANK_MANA_PER_POSTMIT);
            self.gain_mana(m);
        }
        if self.fx.adap_per_hit {
            self.adap_stack();
        }
        if attack {
            for i in 0..self.fx.thorns.len() {
                let (dmg, cd) = self.fx.thorns[i];
                if self.t >= self.thorns_ready[i] {
                    self.thorns_ready[i] = self.t + cd;
                    let adj = self.adjacent(attacker);
                    for d in adj.iter() {
                        self.deal(dmg, DType::Magic, Some(d), "thorns", Deal::PLAIN);
                    }
                }
            }
        }
        D::hit(self, attacker, post);
        self.health_triggers();
        if self.hp <= 0.0 && self.alive_unit {
            self.die();
        }
        post
    }

    fn health_triggers(&mut self) {
        let frac = self.hp_frac();
        for i in 0..self.fx.shield_at_hp.len() {
            let (thr, pct, dur, decays) = self.fx.shield_at_hp[i];
            if frac < thr && self.fired_shield & (1 << i) == 0 {
                self.fired_shield |= 1 << i;
                let amount = pct * self.max_hp();
                self.shield(amount, dur, "low health", decays);
            }
        }
        for i in 0..self.fx.mana_at_hp.len() {
            let (thr, mana) = self.fx.mana_at_hp[i];
            if frac < thr && self.fired_mana & (1 << i) == 0 {
                self.fired_mana |= 1 << i;
                self.gain_mana_opt(mana, false);
            }
        }
        for i in 0..self.fx.untargetable_at_hp.len() {
            let (thr, dur, heal_missing) = self.fx.untargetable_at_hp[i];
            if frac < thr && self.fired_untargetable & (1 << i) == 0 {
                self.fired_untargetable |= 1 << i;
                self.untargetable(dur);
                let amount = (self.max_hp() - self.hp) * heal_missing;
                self.heal(amount, "edge of night");
            }
        }
        if let Some((thr, heal)) = self.fx.fae_heal {
            if frac < thr && !self.fired_fae {
                self.fired_fae = true;
                let amount = heal * self.max_hp();
                self.heal(amount, "pixies");
            }
        }
    }

    fn die(&mut self) {
        self.alive_unit = false;
        self.died_at = Some(self.t);
        self.hp = 0.0;
        self.shields.clear();
        self.record("died", 0.0, None, "");
        D::died(self);
        self.next_body();
    }

    fn next_body(&mut self) {
        if !self.bodies.is_empty() {
            self.body = Some(self.bodies.remove(0));
        } else {
            self.body = None;
            self.hold_until = Some(self.t);
        }
    }

    /// An on-death body that taunts and keeps the dummies on it.
    pub fn add_body(&mut self, hp: f64, armor: f64, mr: f64, name: &'static str) {
        self.bodies.push(Body { hp, armor, mr, name });
    }

    /// Heal the unit; returns the effective amount.
    pub fn heal(&mut self, amount: f64, src: &'static str) -> f64 {
        if amount <= 0.0 || !self.alive_unit {
            return 0.0;
        }
        let eff = pymin(amount, self.max_hp() - self.hp);
        if eff > 0.0 {
            self.hp += eff;
            self.healed += eff;
            self.record("heal", eff, None, src);
        }
        eff
    }

    /// Returns the index of the new shield (None when nothing was added), so
    /// a driver can watch it.
    pub fn shield(&mut self, amount: f64, duration: f64, src: &'static str, decays: bool)
        -> Option<usize> {
        if amount <= 0.0 || !self.alive_unit || duration <= 0.0 {
            return None;
        }
        self.record("shield", amount, None, src);
        self.shields.push(Shield { amount, until: self.t + duration,
                                   decay: if decays { amount / duration } else { 0.0 },
                                   dead: false });
        Some(self.shields.len() - 1)
    }

    pub fn shields_active(&self) -> usize {
        self.shields.iter().filter(|s| !s.dead).count()
    }

    pub fn heal_ally(&mut self, amount: f64) {
        if amount > 0.0 {
            self.ally_heal += amount;
        }
    }

    pub fn shield_ally(&mut self, amount: f64) {
        if amount > 0.0 {
            self.ally_shield += amount;
        }
    }

    /// Stun (sleep, knock up) dummies: their attacks and casts inside the
    /// window are denied.
    pub fn stun(&mut self, targets: &Sel, duration: f64) {
        for i in targets.iter() {
            let t = self.t;
            let d = &mut self.targets[i];
            if d.alive && duration > 0.0 {
                d.stunned_until = pymax(d.stunned_until, t + duration);
                self.cc_time += duration;
            }
        }
    }

    pub fn buff_resists(&mut self, armor: f64, mr: f64, duration: f64) {
        self.resist_buffs.push((armor, mr, self.t + duration));
    }

    pub fn buff_durability(&mut self, pct: f64, duration: f64) {
        self.dur_buffs.push((pct, self.t + duration));
    }

    pub fn untargetable(&mut self, duration: f64) {
        self.untargetable_until = pymax(self.untargetable_until, self.t + duration);
    }

    /// Bonus max health, which also fills by that much.
    pub fn gain_max_hp(&mut self, amount: f64) {
        if amount > 0.0 {
            self.max_hp_extra += amount;
            self.hp += amount;
        }
    }

    // ---- damage ------------------------------------------------------------

    /// An ability calculation's value now.
    #[inline]
    pub fn calc(&self, id: CalcId) -> f64 {
        self.calc_rt(id, Runtime::NONE)
    }

    pub fn calc_rt(&self, id: CalcId, runtime: Runtime<'_>) -> f64 {
        self.kit.calc_value(id, self.ad(), self.ap(), self.max_hp(), self.armor_now(),
                            self.mr_now(), self.sheet.base_ad, runtime)
    }

    #[inline]
    pub fn row(&self, id: RowId) -> f64 {
        self.kit.row_value(id)
    }

    /// Fight.deal: `amount` pre-mitigation damage of `dtype` to `target`;
    /// returns the post-mitigation damage. Crit is an expected value;
    /// abilities crit only with Precision.
    pub fn deal(&mut self, amount: f64, dtype: DType, target: Option<usize>, src: &'static str,
                mode: Deal) -> f64 {
        let target = match target {
            Some(i) if amount > 0.0 && self.targets[i].alive => i,
            _ => return 0.0,
        };
        let mut amount = amount;
        if mode.crit && (!mode.ability || self.sheet.precision) {
            amount *= self.sheet.crit_ev();
        }
        let pre = amount;
        {
            let t = self.t;
            let tg = &self.targets[target];
            match dtype {
                DType::Physical => {
                    let r = tg.armor * (if t < tg.sunder_until { 1.0 - tg.sunder } else { 1.0 })
                        - tg.armor_flat;
                    amount *= resist_mult(pymax(r, 0.0));
                }
                DType::Magic => {
                    let r = tg.mr * (if t < tg.shred_until { 1.0 - tg.shred } else { 1.0 })
                        - tg.mr_flat;
                    amount *= resist_mult(pymax(r, 0.0));
                }
                DType::True => {}
            }
        }
        if dtype != DType::True && !mode.raw {
            amount *= self.amp(target);
        }
        self.apply(amount, target, src, pre);
        if !mode.raw && dtype != DType::True {
            if self.fx.bonus_magic_pct != 0.0 {
                let bonus = amount * self.fx.bonus_magic_pct;
                self.deal(bonus, DType::Magic, Some(target), "solar", Deal::RAW);
            }
            if self.fx.bleed_pct != 0.0 && self.targets[target].alive {
                let (pct, dur) = (self.fx.bleed_pct, self.fx.bleed_dur);
                self.dot(amount * pct, dur, DType::True, Some(target), "bleed", false);
            }
        }
        amount
    }

    fn breakdown_add(&mut self, src: &'static str, amount: f64) {
        for entry in self.breakdown.iter_mut() {
            if entry.0 == src {
                entry.1 += amount;
                return;
            }
        }
        self.breakdown.push((src, amount));
    }

    fn apply(&mut self, amount: f64, target: usize, src: &'static str, pre: f64) {
        self.raw_total += amount;
        let t = self.t;
        if self.targets[target].immortal {
            self.total += amount;
            self.breakdown_add(src, amount);
            self.record("damage", amount, Some(target), src);
            let d = &mut self.targets[target];
            if d.mana_from_damage {
                d.gain_mana(pymin(TANK_MANA_PER_HIT_CAP,
                                  pre * TANK_MANA_PER_PREMIT + amount * TANK_MANA_PER_POSTMIT), t);
            }
            return;
        }
        let mut amount = amount;
        if amount > self.targets[target].hp {
            amount = self.targets[target].hp;
        }
        self.targets[target].hp -= amount;
        self.total += amount;
        self.breakdown_add(src, amount);
        self.record("damage", amount, Some(target), src);
        {
            let d = &mut self.targets[target];
            if d.mana_from_damage && d.alive {
                d.gain_mana(pymin(TANK_MANA_PER_HIT_CAP,
                                  pre * TANK_MANA_PER_PREMIT + amount * TANK_MANA_PER_POSTMIT), t);
            }
        }
        if self.pressure && self.alive_unit {
            let omni = self.omnivamp();
            if omni != 0.0 {
                self.heal(amount * omni, "omnivamp");
            }
        }
        if self.fx.ally_heal_pct != 0.0 {
            self.ally_heal += amount * self.fx.ally_heal_pct;
        }
        if self.targets[target].hp <= 0.0 && self.targets[target].alive {
            self.targets[target].alive = false;
            self.targets[target].died_at = Some(t);
            if !self.targets.iter().any(|d| d.alive) {
                self.kill_time = Some(t);
            } else if target == self.cur {
                self.cur = self.target().expect("someone alive");
                self.target_since = t;
            }
            if self.fx.heal_on_takedown != 0.0 {
                let amount = self.fx.heal_on_takedown * self.max_hp();
                self.heal(amount, "takedown");
            }
            if self.fx.mana_on_takedown != 0.0 {
                self.gain_mana(self.fx.mana_on_takedown);
            }
            self.record("kill", 0.0, Some(target), src);
            D::kill(self, target);
        }
    }

    /// Sunder, shred and burns that attacks and ability damage apply.
    pub fn on_hit_effects(&mut self, target: Option<usize>, _ability: bool) {
        let target = match target {
            Some(i) if self.targets[i].alive => i,
            _ => return,
        };
        for k in 0..self.fx.sunder_on_hit.len() {
            let (pct, dur) = self.fx.sunder_on_hit[k];
            self.sunder(target, pct, dur);
        }
        for k in 0..self.fx.shred_on_hit.len() {
            let (pct, dur) = self.fx.shred_on_hit[k];
            self.shred(target, pct, dur);
        }
        if let Some((pct, dur)) = self.fx.caustic {
            self.sunder(target, pct, dur);
            self.shred(target, pct, dur);
        }
        for k in 0..self.fx.burn_on_hit.len() {
            let (pct, dur, stacks) = self.fx.burn_on_hit[k];
            self.burn(target, pct, dur, stacks);
        }
    }

    pub fn sunder(&mut self, target: usize, pct: f64, dur: f64) {
        let t = self.t;
        let d = &mut self.targets[target];
        if pct >= d.sunder || t >= d.sunder_until {
            d.sunder = pymax(pct, if t < d.sunder_until { d.sunder } else { 0.0 });
        }
        d.sunder_until = pymax(d.sunder_until, t + dur);
    }

    pub fn shred(&mut self, target: usize, pct: f64, dur: f64) {
        let t = self.t;
        let d = &mut self.targets[target];
        if pct >= d.shred || t >= d.shred_until {
            d.shred = pymax(pct, if t < d.shred_until { d.shred } else { 0.0 });
        }
        d.shred_until = pymax(d.shred_until, t + dur);
    }

    pub fn burn(&mut self, target: usize, pct: f64, dur: f64, stacks: bool) {
        let t = self.t;
        let d = &mut self.targets[target];
        if stacks {
            d.burn_stack = pymax(d.burn_stack, pct);
            d.burn_stack_until = t + dur;
        } else if pct >= d.burn_pct || t >= d.burn_until {
            d.burn_pct = pct;
            d.burn_until = t + dur;
        }
    }

    /// Damage over time: `total` pre-mitigation over `duration` s, paid out
    /// on the ticks for the time elapsed. An ability's DoT crits with
    /// Precision; an item's or a trait's never does.
    pub fn dot(&mut self, total: f64, duration: f64, dtype: DType, target: Option<usize>,
               src: &'static str, ability: bool) {
        let target = match target {
            Some(i) if self.targets[i].alive => i,
            _ => return,
        };
        if duration <= 0.0 {
            self.deal(total, dtype, Some(target), src,
                      Deal { ability, crit: ability, raw: false });
            return;
        }
        let t = self.t;
        self.targets[target].dots.push(Dot { dps: total / duration, until: t + duration, dtype,
                                             src, start: t, ability });
    }

    /// One basic attack's damage on `target`.
    pub fn hit_attack(&mut self, target: usize, mult: f64, src: &'static str) -> f64 {
        let dmg = self.deal(self.ad() * mult, DType::Physical, Some(target), src,
                            Deal { ability: false, crit: true, raw: false });
        self.on_hit_effects(Some(target), false);
        dmg
    }

    /// Ability damage from calc `calc` on `target` (type from the calc's name).
    pub fn hit_ability(&mut self, calc: CalcId, target: Option<usize>, src: &'static str,
                       mult: f64) -> f64 {
        let dtype = self.kit.calc_dtype(calc);
        self.hit_ability_typed(calc, target, src, mult, dtype)
    }

    pub fn hit_ability_typed(&mut self, calc: CalcId, target: Option<usize>, src: &'static str,
                             mult: f64, dtype: DType) -> f64 {
        let dmg = self.deal(self.calc(calc) * mult, dtype, target, src, Deal::ABILITY);
        self.on_hit_effects(target, true);
        dmg
    }

    pub fn hit_ability_rt(&mut self, calc: CalcId, target: Option<usize>, src: &'static str,
                          mult: f64, runtime: Runtime<'_>) -> f64 {
        let dtype = self.kit.calc_dtype(calc);
        let dmg = self.deal(self.calc_rt(calc, runtime) * mult, dtype, target, src, Deal::ABILITY);
        self.on_hit_effects(target, true);
        dmg
    }

    pub fn dot_ability(&mut self, calc: CalcId, target: Option<usize>, duration: f64,
                       src: &'static str, mult: f64) {
        let dtype = self.kit.calc_dtype(calc);
        self.on_hit_effects(target, true);
        self.dot(self.calc(calc) * mult, duration, dtype, target, src, true);
    }

    pub fn buff_as(&mut self, pct: f64, duration: f64) {
        self.as_extra = pct;
        self.as_extra_until = self.t + duration;
    }

    // ---- the loop ---------------------------------------------------------

    pub fn run(&mut self) -> FightResult {
        D::init(self);
        let mut next_tick = TICK_S;
        let mut next_second = 1.0;
        let mut interval_next: Vec<(f64, f64)> =
            self.fx.ap_per_interval.iter().map(|&(ap, interval)| (interval, ap)).collect();
        let mut heal_next: Vec<(f64, f64)> =
            self.fx.heal_per_interval.iter().map(|&(pct, interval)| (interval, pct)).collect();
        let mut ap_after: Vec<(f64, f64)> = self.fx.ap_after.clone();
        ap_after.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap());   // stable, like Python's
        self.next_attack = 0.0;
        while self.t < self.duration && self.kill_time.is_none() && self.holding() {
            let t_attack = if self.alive_unit { pymax(self.next_attack, self.casting_until) }
                           else { FAR };
            let t_in = if self.pressure { self.next_incoming() } else { FAR };
            let t_fx = if !self.pending.is_empty() { self.pending[0].0 } else { FAR };
            let t_next = pymin(pymin(pymin(t_attack, next_tick), t_in), t_fx);
            self.t = t_next;
            // effects landing now (a channel's damage) come first
            while !self.pending.is_empty() && self.pending[0].0 <= self.t + 1e-9 {
                let (_, _, ev) = self.pending.remove(0);
                match ev {
                    Event::Cast => {
                        self.record("land", 0.0, None, "");
                        D::cast(self);
                    }
                    Event::Driver(tag) => D::event(self, tag),
                }
            }
            if self.kill_time.is_some() {
                break;
            }
            if t_next == next_tick {
                self.tick(next_tick, next_second, &mut interval_next, &mut ap_after, &mut heal_next);
                if next_tick >= next_second {
                    next_second += 1.0;
                }
                next_tick += TICK_S;
            }
            if self.pressure && t_in <= self.t {
                self.incoming();
            }
            if self.kill_time.is_some() || self.t >= self.duration || !self.alive_unit {
                continue;
            }
            // (a cast that started on this tick holds the attack that was due)
            if t_attack <= self.t && self.t >= self.casting_until {
                self.attack();
            }
        }
        self.result()
    }

    /// Run something at t + delay: `Driver::event(f, tag)`. Due events run
    /// in order before anything else at that instant.
    pub fn after(&mut self, delay: f64, tag: u32) {
        self.push_event(delay, Event::Driver(tag));
    }

    fn push_event(&mut self, delay: f64, ev: Event) {
        self.seq += 1;
        self.pending.push((self.t + pymax(0.0, delay), self.seq, ev));
        self.pending.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap().then(a.1.cmp(&b.1)));
    }

    /// When the next queued effect lands (tests).
    pub fn next_pending(&self) -> Option<f64> {
        self.pending.first().map(|p| p.0)
    }

    fn next_incoming(&self) -> f64 {
        let mut t = FAR;
        for d in &self.targets {
            if d.alive {
                let e = d.next_event();
                if e < t {
                    t = e;
                }
            }
        }
        t
    }

    /// Every dummy attack that is due (and the cast it fills the bar for).
    fn incoming(&mut self) {
        for di in 0..self.targets.len() {
            let n = self.targets[di].n_streams;
            for i in 0..n {
                loop {
                    let t = self.t;
                    let d = &mut self.targets[di];
                    if !(d.alive && (self.alive_unit || self.body.is_some())
                         && d.next_attacks[i] <= t + 1e-9) {
                        break;
                    }
                    d.next_attacks[i] += 1.0 / d.as_;
                    d.attacks += 1;
                    let amount = d.ad * d.crit_ev;
                    let stunned = t < d.stunned_until;
                    if stunned || t < self.untargetable_until {
                        self.denied += amount;
                    } else {
                        self.take(amount, DType::Physical, Some(di), true);
                        let d = &mut self.targets[di];
                        d.gain_mana(d.mana_per_attack, t);
                        self.dummy_cast(di);
                    }
                }
            }
        }
    }

    /// A dummy with a full bar casts: its ability's damage, split by its
    /// group's damage types, then the mana lock.
    fn dummy_cast(&mut self, di: usize) {
        let t = self.t;
        let holding = self.holding();
        let d = &mut self.targets[di];
        if !(d.alive && holding && d.mana_max > 0.0 && d.mana >= d.mana_max && t >= d.lock_until) {
            return;
        }
        d.mana = pymin(d.mana - d.mana_max, d.mana_max);
        d.lock_until = t + MANA_LOCK_S;
        d.casts += 1;
        if t < d.stunned_until || t < self.untargetable_until {
            self.denied += d.ability;
            return;
        }
        let (ability, phys_share, mana_max) = (d.ability, d.phys_share, d.mana_max);
        if self.fx.ionic_spark != 0.0 {
            let dmg = self.fx.ionic_spark * mana_max;
            self.deal(dmg, DType::Magic, Some(di), "ionic spark", Deal::PLAIN);
        }
        if phys_share > 0.0 {
            self.take(ability * phys_share, DType::Physical, Some(di), false);
        }
        if phys_share < 1.0 && self.holding() {
            self.take(ability * (1.0 - phys_share), DType::Magic, Some(di), false);
        }
    }

    fn tick(&mut self, now: f64, next_second: f64, interval_next: &mut Vec<(f64, f64)>,
            ap_after: &mut Vec<(f64, f64)>, heal_next: &mut Vec<(f64, f64)>) {
        // mana regen for the part of the tick outside the lock
        if self.fx.mana_regen != 0.0 && self.alive_unit {
            let free = now - pymax(now - TICK_S, self.lock_until);
            if free > 0.0 {
                self.gain_mana(self.fx.mana_regen * free);
            }
        }
        // burns: % max hp per second as true damage, applied per tick
        for di in 0..self.targets.len() {
            if !self.targets[di].alive {
                continue;
            }
            let (mut pct, burn_stack, bsu, max_hp) = {
                let d = &self.targets[di];
                (if now <= d.burn_until { d.burn_pct } else { 0.0 }, d.burn_stack,
                 d.burn_stack_until, d.max_hp)
            };
            if burn_stack != 0.0 && now <= bsu {
                pct += burn_stack;
            }
            if pct != 0.0 {
                self.deal(pct * max_hp * TICK_S, DType::True, Some(di), "burn", Deal::PLAIN);
            }
            if !self.targets[di].dots.is_empty() {
                let dots = std::mem::take(&mut self.targets[di].dots);
                let mut keep = Vec::with_capacity(dots.len());
                for dot in dots {
                    let span = pymin(now, dot.until) - pymax(now - TICK_S, dot.start);
                    if span > 0.0 {
                        self.deal(dot.dps * span, dot.dtype, Some(di), dot.src,
                                  Deal { ability: dot.ability, crit: dot.ability,
                                         raw: dot.src == "bleed" });
                    }
                    if dot.until > now {
                        keep.push(dot);
                    }
                }
                // a driver may have queued dots meanwhile (none do); keep Python's replace
                self.targets[di].dots = keep;
            }
        }
        if let Some((pct, dur)) = self.fx.burn_aura {
            if self.alive_unit {
                if let Some(tgt) = self.target() {
                    self.burn(tgt, pct, dur, false);
                }
            }
        }
        // per-second stacking attack speed (Guinsoo, Quicksilver)
        if now >= next_second - 1e-9 {
            for &(pct, until) in &self.fx.as_per_second {
                if until.is_none() || now <= until.unwrap() {
                    self.as_stack += pct;
                }
            }
        }
        for i in 0..interval_next.len() {
            let (interval, ap) = interval_next[i];
            if now >= interval - 1e-9 {
                self.ap_stack += ap;
                interval_next[i] = (interval + self.fx.ap_per_interval[i].1, ap);
            }
        }
        while !ap_after.is_empty() && now >= ap_after[0].1 - 1e-9 {
            let (ap, _) = ap_after.remove(0);
            self.ap_stack += ap;
        }
        self.amp_stacks.retain(|x| x.1 > now);
        // the body: shields decay and expire, timed healing
        if self.alive_unit {
            if !self.shields.is_empty() {
                for sh in self.shields.iter_mut() {
                    if sh.dead {
                        continue;
                    }
                    if sh.decay != 0.0 {
                        sh.amount -= sh.decay * TICK_S;
                    }
                }
                for sh in self.shields.iter_mut() {
                    if !sh.dead && !(sh.amount > 0.0 && sh.until > now) {
                        sh.dead = true;
                    }
                }
            }
            for i in 0..heal_next.len() {
                let (at, pct) = heal_next[i];
                if now >= at - 1e-9 {
                    let amount = pct * self.max_hp();
                    self.heal(amount, "dragon's claw");
                    heal_next[i] = (at + self.fx.heal_per_interval[i].1, pct);
                }
            }
            if self.fx.regen_missing_pct != 0.0 && self.hp < self.max_hp() {
                let amount = self.fx.regen_missing_pct * (self.max_hp() - self.hp) * TICK_S;
                self.heal(amount, "regeneration");
            }
        }
        if self.pressure {
            for di in 0..self.targets.len() {
                let d = &self.targets[di];
                if d.alive && d.mana_max > 0.0 && d.mana >= d.mana_max {
                    self.dummy_cast(di);
                }
            }
        }
        D::tick(self);
        if self.alive_unit && self.mana >= self.sheet.mana_max && self.sheet.mana_max > 0.0
            && self.t >= self.casting_until && self.kill_time.is_none() {
            self.cast();
        }
    }

    /// Mana from any source: none during the lock, scaled by Adaptive Helm.
    pub fn gain_mana(&mut self, amount: f64) {
        self.gain_mana_opt(amount, true);
    }

    /// `lock=False`: a proc that fires regardless of the lock.
    pub fn gain_mana_opt(&mut self, amount: f64, lock: bool) {
        if (lock && self.t < self.lock_until) || self.sheet.mana_max <= 0.0 {
            return;
        }
        self.mana += amount * self.fx.mana_mult;
    }

    /// What every attack does to the per-attack stacks: Kraken's, Titan's,
    /// Rapidfire, Striker's Flail.
    pub fn on_attack_stacks(&mut self) {
        self.ad_stack_n += 1;
        for k in 0..self.fx.ad_per_attack.len() {
            let (pct, mx, _) = self.fx.ad_per_attack[k];
            if (self.ad_stack_n as f64) <= mx {
                self.ad_stack += pct;
            }
        }
        self.adap_stack();
        for k in 0..self.fx.as_per_attack_stack.len() {
            let (pct, mx) = self.fx.as_per_attack_stack[k];
            let n = self.rapidfire_n;
            if (n as f64) < mx {
                self.rapidfire_n = n + 1;
                self.as_attack_stack += pct;
            }
        }
        for i in 0..self.fx.amp_per_crit.len() {
            let (amp, dur, mx) = self.fx.amp_per_crit[i];
            let mut live = 0.0;
            for &(a, u, j) in &self.amp_stacks {
                if u > self.t && j == i {
                    live += a;
                }
            }
            let add = pymin(amp * self.sheet.crit_chance, pymax(0.0, amp * mx - live));
            if add > 0.0 {
                self.amp_stacks.push((add, self.t + dur, i));
            }
        }
    }

    /// An ability hit that counts as an attack for on-hit purposes: the
    /// per-attack stacks and the on-hit effects, no damage of its own and no
    /// mana (the unit is casting).
    pub fn simulated_attack(&mut self, target: usize) {
        self.on_attack_stacks();
        self.on_hit_effects(Some(target), false);
    }

    fn attack(&mut self) {
        let tgt = match self.target() {
            Some(i) => i,
            None => return,
        };
        self.attacks += 1;
        self.record("attack", 0.0, Some(tgt), "");
        self.on_attack_stacks();
        // mana on attack (role + items), before the driver so an attack
        // that fills the bar casts right after
        let mana = self.sheet.mana_per_attack + self.fx.mana_per_attack
            + self.fx.mana_per_crit * self.sheet.crit_chance;
        self.gain_mana(mana);
        D::attack(self, tgt);
        if self.mana >= self.sheet.mana_max && self.sheet.mana_max > 0.0 && self.alive_unit {
            self.cast();
        }
        // the next attack a period later, at the attack speed the unit has
        // now (a cast's attack-speed buff counts from this attack on)
        self.next_attack = self.t + 1.0 / self.attack_speed();
    }

    /// Fight._cast; public so the tests can start a cast by hand.
    pub fn cast(&mut self) {
        if self.target().is_none() || !self.alive_unit {
            return;
        }
        self.casts += 1;
        self.cast_times.push(self.t);
        self.record("cast", self.mana, None, "");
        let overflow = pymax(0.0, self.mana - self.sheet.mana_max);
        self.mana = pymin(overflow, self.sheet.mana_max);   // overflow carries up to one cast
        let cast_time = D::cast_time(self);
        self.casting_until = self.t + cast_time;
        // no mana while casting nor for the second after: a channel the
        // driver declares locks through its length plus that second; the
        // data's default animation is inside the second
        self.lock_until = self.t + MANA_LOCK_S
            + if cast_time > CAST_TIME_DEFAULT { cast_time } else { 0.0 };
        if self.fx.ap_per_cast != 0.0 {
            self.ap_stack += self.fx.ap_per_cast;
        }
        if cast_time > 0.0 && !D::LANDS_AT_START {
            self.push_event(cast_time, Event::Cast);
        } else {
            D::cast(self);
        }
    }

    pub fn result(&self) -> FightResult {
        let dps = match self.kill_time {
            None => self.total / pymax(self.t, TICK_S),
            Some(kt) => self.total / pymax(kt, TICK_S),
        };
        let alive_time = match self.hold_until {
            Some(h) => h,
            None => self.duration,
        };
        FightResult {
            kill_time: self.kill_time,
            total: self.total,
            dps,
            raw_total: self.raw_total,
            attacks: self.attacks,
            casts: self.casts,
            cast_times: self.cast_times.clone(),
            breakdown: self.breakdown.clone(),
            left: self.targets.iter().map(|d| pymax(0.0, d.hp)).collect(),
            t: self.t,
            alive_time,
            died: !self.alive_unit,
            died_at: self.died_at,
            hp_left: pymax(0.0, self.hp),
            absorbed: self.absorbed,
            taken: self.taken,
            mitigated: self.mitigated,
            healed: self.healed,
            shielded: self.shield_used,
            denied: self.denied,
            ally_heal: self.ally_heal,
            ally_shield: self.ally_shield,
            cc_time: self.cc_time,
            hits_taken: self.hits_taken,
            dummy_casts: self.targets.iter().map(|d| d.casts).collect(),
            dummy_attacks: self.targets.iter().map(|d| d.attacks).collect(),
            probe: Probe {
                mana: self.mana,
                lock_until: self.lock_until,
                casting_until: self.casting_until,
                as_stack: self.as_stack,
                adap_stack_n: self.adap_stack_n,
                untargetable_until: self.untargetable_until,
                shields_active: self.shields_active(),
                max_hp: self.max_hp(),
            },
            trace: self.trace.clone(),
        }
    }
}

/// Fight.result: what one fight reports.
#[derive(Clone, Debug)]
pub struct FightResult {
    pub kill_time: Option<f64>,
    pub total: f64,
    pub dps: f64,
    pub raw_total: f64,
    pub attacks: i64,
    pub casts: i64,
    pub cast_times: Vec<f64>,
    pub breakdown: Vec<(&'static str, f64)>,
    pub left: Vec<f64>,
    pub t: f64,
    pub alive_time: f64,
    pub died: bool,
    pub died_at: Option<f64>,
    pub hp_left: f64,
    pub absorbed: f64,
    pub taken: f64,
    pub mitigated: f64,
    pub healed: f64,
    pub shielded: f64,
    pub denied: f64,
    pub ally_heal: f64,
    pub ally_shield: f64,
    pub cc_time: f64,
    pub hits_taken: i64,
    pub dummy_casts: Vec<i64>,
    pub dummy_attacks: Vec<i64>,
    pub probe: Probe,
    pub trace: Option<Vec<Ev>>,
}

/// End-of-fight internals the tests look at (not part of the ranking).
#[derive(Clone, Debug)]
pub struct Probe {
    pub mana: f64,
    pub lock_until: f64,
    pub casting_until: f64,
    pub as_stack: f64,
    pub adap_stack_n: i64,
    pub untargetable_until: f64,
    pub shields_active: usize,
    pub max_hp: f64,
}

/// The opening numbers of one build (tft.Sheet.opening plus the sheet's
/// own): what the cell rows report.
#[derive(Clone, Debug)]
pub struct Opening {
    pub ad: f64,
    pub ap: f64,
    pub as_: f64,
    pub crit: f64,
    pub crit_mult: f64,
    pub precision: bool,
    pub hp: f64,
    pub armor: f64,
    pub mr: f64,
    pub durability: f64,
    pub omnivamp: f64,
    pub form: Option<crate::fx::Form>,
    pub mana_start: f64,
    pub mana_max: f64,
}

impl<'a, D: Driver> Fight<'a, D> {
    pub fn opening(&self) -> Opening {
        Opening {
            ad: self.ad(),
            ap: self.ap(),
            as_: self.attack_speed(),
            crit: self.sheet.crit_chance,
            crit_mult: self.sheet.crit_mult,
            precision: self.sheet.precision,
            hp: self.max_hp(),
            armor: self.armor_now(),
            mr: self.mr_now(),
            durability: self.sheet.durability,
            omnivamp: self.sheet.omnivamp,
            form: self.sheet.form,
            mana_start: self.sheet.mana_start,
            mana_max: self.sheet.mana_max,
        }
    }
}

/// Unused import guard: HashMap is handy for callers building kits.
#[allow(dead_code)]
type _Map = HashMap<String, f64>;
