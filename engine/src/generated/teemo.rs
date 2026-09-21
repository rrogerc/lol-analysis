//! Teemo. Q is a plain-cast nuke on cooldown; Toxic Shot (E) rides every
//! basic attack with an on-hit burst and a refreshable poison DoT; Noxious
//! Trap (R) is thrown from a limited charge pool, arms for 1 s and is
//! assumed to detonate immediately under the stationary target, running its
//! own refreshable poison DoT; Element of Surprise (P) grants a one-time,
//! non-refreshing bonus attack speed window opened by the first
//! stealth-breaking action (the opening Q cast). Q and R both have 0.25 s
//! cast times, so each holds `busy_until` forward before anything else can
//! start.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Toxic Shot's poison tick.
const EV_E_TICK: u8 = 0;
/// A thrown Noxious Trap finishes arming and detonates.
const EV_R_ARM: u8 = 1;
/// Noxious Trap's poison tick.
const EV_R_TICK: u8 = 2;
/// The next Noxious Trap is thrown.
const EV_R_CAST: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Element of Surprise: percent bonus attack speed and its duration.
    p_as_pct: f64,
    p_dur: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    e_impact: f64,
    e_tick: f64,
    e_poison_dur: f64,
    e_tick_interval: f64,
    r_tick_dmg: f64,
    r_arm: f64,
    r_poison_dur: f64,
    r_tick_interval: f64,
    r_cast_cd: f64,
    r_cast_s: f64,
    src_e_tick: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    p_triggered: bool,
    p_until: f64,
    e_ticking: bool,
    e_poison_until: f64,
    e_tick_next: f64,
    r_charges: i64,
    r_cast_ready: f64,
    /// When the mushroom currently in flight will detonate (INF: none).
    r_pending_arm_at: f64,
    r_ticking: bool,
    r_poison_until: f64,
    r_tick_next: f64,
}

impl GenDriver {
    /// The earliest a cast readied at `ready` can start: not before now, and
    /// not inside another cast.
    fn castable_at(&self, e: &Engine, ready: f64) -> f64 {
        pymax(pymax(ready, e.st.t), self.s.busy_until)
    }

    /// A cast with a cast time just started: no other cast and no attack
    /// until it ends (an attack already due later keeps its time).
    fn busy_for(&mut self, e: &mut Engine, cast_s: f64) {
        self.s.busy_until = e.st.t + cast_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
    }

    /// Element of Surprise triggers once, on the first stealth-breaking
    /// action; later actions do nothing since stealth never reforms.
    fn trigger_eos(&mut self, t: f64) {
        if !self.s.p_triggered {
            self.s.p_triggered = true;
            self.s.p_until = t + self.p_dur;
        }
    }

    /// Throws a Noxious Trap: consumes a charge, starts its cooldown and
    /// arms the mushroom for detonation `r_arm` seconds from now.
    fn fire_r_cast(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_charges -= 1;
        self.s.r_cast_ready = t + e.ult_cd(self.r_cast_cd);
        self.s.r_pending_arm_at = t + self.r_arm;
        e.prime_spellblade();
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let r_total = kit.hit("gen.R.totalDamage", ranks.r, sheet)?;
        let r_poison_dur = kit.num("gen.R.poisonDurationS")?;
        let r_tick_interval = kit.num("gen.R.tickIntervalS")?;
        let num_ticks = if r_tick_interval > 0.0 { r_poison_dur / r_tick_interval } else { 1.0 };
        let r_start_charges = kit.at_rank("gen.R.maxAmmo", ranks.r)? as i64;

        let state = State {
            busy_until: 0.0,
            p_triggered: false,
            p_until: 0.0,
            e_ticking: false,
            e_poison_until: 0.0,
            e_tick_next: 0.0,
            r_charges: r_start_charges,
            r_cast_ready: 0.0,
            r_pending_arm_at: INF,
            r_ticking: false,
            r_poison_until: 0.0,
            r_tick_next: 0.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("teemo kit needs attack.windupFraction")?,
            p_as_pct: kit.at_level("gen.P.asPctByLevel", level)? * 100.0,
            p_dur: kit.num("gen.P.buffDurationS")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            e_impact: kit.hit("gen.E.impactDamage", ranks.e, sheet)?,
            e_tick: kit.hit("gen.E.tickDamage", ranks.e, sheet)?,
            e_poison_dur: kit.num("gen.E.poisonDurationS")?,
            e_tick_interval: kit.num("gen.E.tickIntervalS")?,
            r_tick_dmg: r_total / num_ticks,
            r_arm: kit.num("gen.R.armTimeS")?,
            r_poison_dur,
            r_tick_interval,
            r_cast_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_e_tick: intern("E poison"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        self.attack_range > MELEE_MAX_RANGE
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self, t: f64) -> f64 {
        if self.s.p_triggered && t < self.s.p_until {
            self.p_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        self.trigger_eos(e.st.t);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        e.deal(self.e_impact, DType::Magic, SRC_E, false, false, 1.0);
        let t = e.st.t;
        if !self.s.e_ticking {
            self.s.e_ticking = true;
            self.s.e_tick_next = t + self.e_tick_interval;
        }
        self.s.e_poison_until = t + self.e_poison_dur;
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        self.trigger_eos(e.st.t);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        self.trigger_eos(e.st.t);
        if self.ranks.r > 0 && self.s.r_charges > 0 {
            self.fire_r_cast(e);
            self.busy_for(e, self.r_cast_s);
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.e_ticking {
            out[n] = (self.s.e_tick_next, Kind::Ev(EV_E_TICK));
            n += 1;
        }
        if self.s.r_pending_arm_at != INF {
            out[n] = (self.s.r_pending_arm_at, Kind::Ev(EV_R_ARM));
            n += 1;
        } else if self.ranks.r > 0 && self.s.r_charges > 0 {
            let ready = if self.s.r_ticking {
                pymax(self.s.r_cast_ready, self.s.r_poison_until - self.r_arm)
            } else {
                self.s.r_cast_ready
            };
            out[n] = (self.castable_at(e, ready), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        if self.s.r_ticking {
            out[n] = (self.s.r_tick_next, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_TICK) => {
                e.deal(self.e_tick, DType::Magic, self.src_e_tick, false, false, 1.0);
                let next = t + self.e_tick_interval;
                if next <= self.s.e_poison_until {
                    self.s.e_tick_next = next;
                } else {
                    self.s.e_ticking = false;
                }
            }
            Kind::Ev(EV_R_CAST) => {
                self.fire_r_cast(e);
                self.busy_for(e, self.r_cast_s);
            }
            Kind::Ev(EV_R_ARM) => {
                self.s.r_pending_arm_at = INF;
                if !self.s.r_ticking {
                    self.s.r_ticking = true;
                    self.s.r_tick_next = t + self.r_tick_interval;
                }
                self.s.r_poison_until = t + self.r_poison_dur;
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_R_TICK) => {
                e.deal(self.r_tick_dmg, DType::Magic, SRC_R, false, true, 1.0);
                let next = t + self.r_tick_interval;
                if next <= self.s.r_poison_until {
                    self.s.r_tick_next = next;
                } else {
                    self.s.r_ticking = false;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
