//! Nasus. Siphoning Strike (Q) is cast the instant it is off cooldown and
//! unarmed: it arms the next basic attack with bonus physical damage
//! (base, crit-affected, plus a flat non-crit amount from assumed permanent
//! stacks) and resets the attack timer; its cooldown starts once that
//! attack lands, halved while Fury of the Sands is active. Spirit Fire (E)
//! is recast on cooldown for its delayed initial hit, its 10-tick lingering
//! DoT and its armor shred. Fury of the Sands (R) is cast at t=0 and ticks
//! a max-health-percent aura on the dummy every 0.5s for its duration.
//! Wither (W) is never cast: it only affects a target that moves or
//! attacks back.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Spirit Fire: cast, its delayed initial hit, and its lingering ticks.
const EV_E_CAST: u8 = 0;
const EV_E_INITIAL: u8 = 1;
const EV_E_TICK: u8 = 2;
/// Fury of the Sands' aura tick.
const EV_R_TICK: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_base_dmg: f64,
    q_stacks: f64,
    q_cd: f64,
    r_q_cdr_mult: f64,
    e_initial_dmg: f64,
    e_tick_dmg: f64,
    e_tick_interval: f64,
    e_num_ticks: i64,
    e_cast_delay: f64,
    e_cd: f64,
    e_shred_dur: f64,
    r_tick_frac: f64,
    r_tick_cap: f64,
    r_tick_interval: f64,
    r_num_ticks: i64,
    r_duration: f64,
    r_cast_delay: f64,
    src_e_initial: SourceId,
    src_e_tick: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_armed: bool,
    e_ready: f64,
    e_initial_at: f64,
    e_tick_at: f64,
    e_ticks_left: i64,
    r_until: f64,
    r_tick_at: f64,
    r_ticks_left: i64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let r_tick_base = kit.at_rank("gen.R.tick.base", ranks.r)?;
        let r_ap_coef = kit.num("gen.R.apCoefPer100")?;
        let r_cap_per_second = kit.num("gen.R.capPerSecond")?;
        let r_tick_interval = kit.num("gen.R.tickIntervalS")?;
        let r_duration = kit.num("gen.R.durationS")?;
        let e_tick_interval = kit.num("gen.E.tickIntervalS")?;

        let state = State {
            q_armed: false,
            e_ready: 0.0,
            e_initial_at: INF,
            e_tick_at: INF,
            e_ticks_left: 0,
            r_until: -1.0,
            r_tick_at: INF,
            r_ticks_left: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("nasus kit needs attack.windupFraction")?,
            q_base_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_stacks: kit.num_or("gen.Q.assumedStacks", 0.0),
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            r_q_cdr_mult: kit.num("gen.R.qCdrMult")?,
            e_initial_dmg: kit.hit("gen.E.initial", ranks.e, sheet)?,
            e_tick_dmg: kit.hit("gen.E.tick", ranks.e, sheet)?,
            e_tick_interval,
            e_num_ticks: kit.num("gen.E.numTicks")? as i64,
            e_cast_delay: kit.num("gen.E.castTimeS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_shred_dur: kit.num("abilities.Q.shred.durationS")?,
            r_tick_frac: r_tick_base + (sheet.ap / 100.0) * r_ap_coef,
            r_tick_cap: r_cap_per_second * r_tick_interval,
            r_tick_interval,
            r_num_ticks: (r_duration / r_tick_interval) as i64,
            r_duration,
            r_cast_delay: kit.num("gen.R.castTimeS")?,
            src_e_initial: intern("E initial"),
            src_e_tick: intern("E tick"),
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

    fn bonus_as(&self, _t: f64) -> f64 {
        0.0
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.q_armed {
            self.s.q_armed = false;
            let t = e.st.t;
            let cd = if self.ranks.r > 0 && t < self.s.r_until {
                self.q_cd * self.r_q_cdr_mult
            } else {
                self.q_cd
            };
            e.st.q_ready = t + e.basic_cd(cd);
            e.deal(self.q_base_dmg, DType::Physical, SRC_Q, true, true, 1.0);
            e.deal(self.q_stacks, DType::Physical, SRC_Q, false, true, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.q_armed {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        self.s.q_armed = true;
        e.prime_spellblade();
        let b = self.bonus_as(e.st.t);
        e.st.next_attack = e.st.t + e.attack_windup(b, self.windup_fraction);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        e.lockout();
        let start = t + self.r_cast_delay;
        self.s.r_until = start + self.r_duration;
        self.s.r_tick_at = start + self.r_tick_interval;
        self.s.r_ticks_left = self.r_num_ticks;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            if self.s.e_initial_at != INF {
                out[n] = (self.s.e_initial_at, Kind::Ev(EV_E_INITIAL));
                n += 1;
            } else if self.s.e_tick_at != INF {
                out[n] = (self.s.e_tick_at, Kind::Ev(EV_E_TICK));
                n += 1;
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
                n += 1;
            }
        }
        if self.ranks.r > 0 && self.s.r_tick_at != INF {
            out[n] = (self.s.r_tick_at, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.lockout();
                e.prime_spellblade();
                self.s.e_initial_at = t + self.e_cast_delay;
            }
            Kind::Ev(EV_E_INITIAL) => {
                self.s.e_initial_at = INF;
                e.deal(self.e_initial_dmg, DType::Magic, self.src_e_initial, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.st.shred_until = t + self.e_shred_dur;
                self.s.e_tick_at = t + self.e_tick_interval;
                self.s.e_ticks_left = self.e_num_ticks;
            }
            Kind::Ev(EV_E_TICK) => {
                e.deal(self.e_tick_dmg, DType::Magic, self.src_e_tick, false, true, 1.0);
                self.s.e_ticks_left -= 1;
                if self.s.e_ticks_left > 0 {
                    self.s.e_tick_at = t + self.e_tick_interval;
                } else {
                    self.s.e_tick_at = INF;
                }
            }
            Kind::Ev(EV_R_TICK) => {
                let amt = pymin(self.r_tick_frac * e.target_hp, self.r_tick_cap);
                e.deal(amt, DType::Magic, SRC_R, false, true, 1.0);
                e.ult_hatefog();
                self.s.r_ticks_left -= 1;
                if self.s.r_ticks_left > 0 {
                    self.s.r_tick_at = t + self.r_tick_interval;
                } else {
                    self.s.r_tick_at = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
