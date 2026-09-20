//! Renekton. A Fury-driven melee bruiser: Dominus opens the fight for its
//! full duration of aura ticks, Q/E go out on cooldown, and W arms the next
//! basic attack to strike twice (thrice once Fury pays for the empowered
//! version) instead of the engine's normal auto-attack damage.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Ruthless Predator is cast when its cooldown allows.
const EV_W: u8 = 0;
/// Slice and Dice is cast (and immediately recast) when its cooldown allows.
const EV_E: u8 = 1;
/// A Dominus aura tick.
const EV_R_TICK: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    p_fury_per_attack: f64,
    fury_cost: f64,
    q_dmg: f64,
    q_emp_dmg: f64,
    q_cd: f64,
    q_fury_per_hit: f64,
    w_hit_dmg: f64,
    w_cd: f64,
    w_champ_fury_bonus: f64,
    e_dmg: f64,
    e_cd: f64,
    e_fury_per_hit: f64,
    r_tick_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    r_tick_interval: f64,
    r_duration_s: f64,
    r_total_ticks: i64,
    r_fury_on_cast: f64,
    r_fury_per_tick: f64,
    r_fury_cap: f64,
    src_e_recast: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    fury: f64,
    w_ready: f64,
    w_armed: bool,
    w_strikes: i64,
    w_fury_per_strike: f64,
    e_ready: f64,
    r_next_tick: f64,
    r_ticks_left: i64,
    r_fury_generated: f64,
    r_first_tick_done: bool,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let _ = level;
        let state = State {
            fury: 0.0,
            w_ready: 0.0,
            w_armed: false,
            w_strikes: 0,
            w_fury_per_strike: 0.0,
            e_ready: 0.0,
            r_next_tick: INF,
            r_ticks_left: 0,
            r_fury_generated: 0.0,
            r_first_tick_done: false,
        };
        let r_tick_interval = kit.num("gen.R.tickIntervalS")?;
        let r_duration_s = kit.num("gen.R.durationS")?;
        let r_fury_per_second = kit.num("gen.R.furyPerSecond")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("renekton kit needs attack.windupFraction")?,
            p_fury_per_attack: kit.num("gen.P.furyPerAttack")?,
            fury_cost: kit.num("gen.P.furyCost")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_emp_dmg: kit.hit("gen.Q.empDamage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_fury_per_hit: kit.num("gen.Q.furyPerChampHit")?,
            w_hit_dmg: kit.hit("gen.W.hitDamage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_champ_fury_bonus: kit.num("gen.W.champFuryBonus")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_fury_per_hit: kit.num("gen.E.furyPerChampHit")?,
            r_tick_dmg: kit.hit("gen.R.tickDamage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_tick_interval,
            r_duration_s,
            r_total_ticks: (r_duration_s / r_tick_interval) as i64,
            r_fury_on_cast: kit.num("gen.R.furyOnCast")?,
            r_fury_per_tick: r_fury_per_second * r_tick_interval,
            r_fury_cap: kit.num("gen.R.furyCap")?,
            src_e_recast: intern("E recast"),
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

    fn attack_damage(&self, e: &Engine) -> f64 {
        if self.s.w_armed {
            0.0
        } else {
            e.p.ad
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {
        if !self.s.w_armed {
            self.s.fury += self.p_fury_per_attack;
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.w_armed {
            let strikes = self.s.w_strikes;
            let mut i: i64 = 0;
            while i < strikes {
                let first = i == 0;
                e.deal(self.w_hit_dmg, DType::Physical, SRC_W, first, first, 1.0);
                i += 1;
            }
            if self.s.w_fury_per_strike > 0.0 {
                self.s.fury += self.s.w_fury_per_strike * (strikes as f64);
            }
            self.s.w_armed = false;
            self.s.w_ready = e.st.t + e.basic_cd(self.w_cd);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let empowered = self.s.fury >= self.fury_cost;
        let dmg = if empowered {
            self.s.fury -= self.fury_cost;
            self.q_emp_dmg
        } else {
            self.s.fury += self.q_fury_per_hit;
            self.q_dmg
        };
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.fury += self.r_fury_on_cast;
        self.s.r_next_tick = t + self.r_cast_s + self.r_tick_interval;
        self.s.r_ticks_left = self.r_total_ticks;
        self.s.r_fury_generated = 0.0;
        self.s.r_first_tick_done = false;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 && !self.s.w_armed {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E));
            n += 1;
        }
        if self.s.r_next_tick != INF {
            out[n] = (self.s.r_next_tick, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                let empowered = self.s.fury >= self.fury_cost;
                let (strikes, fury_per_strike): (i64, f64) = if empowered {
                    self.s.fury -= self.fury_cost;
                    (3, 0.0)
                } else {
                    (2, self.p_fury_per_attack + self.w_champ_fury_bonus)
                };
                self.s.w_strikes = strikes;
                self.s.w_fury_per_strike = fury_per_strike;
                self.s.w_armed = true;
                e.prime_spellblade();
                e.ability_cast_proc();
                let b = self.bonus_as(t);
                let reset_at = t + e.attack_windup(b, self.windup_fraction);
                e.st.next_attack = pymin(e.st.next_attack, reset_at);
            }
            Kind::Ev(EV_E) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.lockout();
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.fury += self.e_fury_per_hit;
                // Dice: the free recast, taken immediately, a separate cast instance
                e.deal(self.e_dmg, DType::Physical, self.src_e_recast, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.fury += self.e_fury_per_hit;
            }
            Kind::Ev(EV_R_TICK) => {
                e.deal(self.r_tick_dmg, DType::Magic, SRC_R, false, true, 1.0);
                if !self.s.r_first_tick_done {
                    e.ability_cast_proc();
                    e.eclipse_hit();
                    self.s.r_first_tick_done = true;
                }
                e.ult_hatefog();
                let room = pymax(self.r_fury_cap - self.s.r_fury_generated, 0.0);
                let add = pymin(self.r_fury_per_tick, room);
                self.s.fury += add;
                self.s.r_fury_generated += add;
                self.s.r_ticks_left -= 1;
                if self.s.r_ticks_left > 0 {
                    self.s.r_next_tick = t + self.r_tick_interval;
                } else {
                    self.s.r_next_tick = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
