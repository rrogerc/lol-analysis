//! Shyvana. Auto-attacks while throwing Emberstrike (Q) chains on cooldown
//! (only the final cast per chain consumes the real cooldown, earlier casts
//! are free resets), Molten Burst (E) and Inferno Aegis (W) on cooldown, and
//! Dragon's Descent (R) whenever Dragon Fury is full: Fury drains while in
//! Dragon Form and regenerates in Human Form, topped up by every hit landed.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Molten Burst: cast, its damage landing, and its Dragon Form DoT ticks.
const EV_E_CAST: u8 = 0;
const EV_E_LAND: u8 = 1;
const EV_E_DOT: u8 = 2;
/// Inferno Aegis: cast, then its damage recast.
const EV_W_CAST: u8 = 3;
const EV_W_RECAST: u8 = 4;
/// Dragon's Descent: a mid-fight recast's damage landing.
const EV_R_LAND: u8 = 5;
/// The Dragon Fury pool crossing empty (exit Dragon Form) or full (recast R).
const EV_FURY: u8 = 6;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_dmg: f64,
    q_true_dmg: f64,
    q_cd: f64,
    q_onhit_ratio: f64,
    q_cdr_on_hit_s: f64,
    q_lockout_s: f64,
    q_max_human: i64,
    q_max_dragon: i64,

    w_dmg: f64,
    w_cd: f64,
    w_recast_delay_s: f64,

    e_flat: f64,
    e_hp_ratio: f64,
    e_dragon_mult: f64,
    e_cd: f64,
    e_cast_human_s: f64,
    e_cast_dragon_s: f64,
    e_dot_per_tick: f64,
    e_dot_tick_s: f64,
    e_dot_total_ticks: i64,

    r_dmg: f64,
    r_cast_time_s: f64,
    fury_regen_per_s: f64,
    fury_drain_per_s: f64,
    fury_gain_per_hit: f64,
    fury_dragon_mult: f64,
    fury_full: f64,

    src_q_true: SourceId,
    src_q_onhit: SourceId,
    src_e_dot: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    fury: f64,
    fury_t: f64,
    in_dragon: bool,

    q_armed: bool,
    q_step: i64,
    q_chain_active: bool,
    q_chain_dragon: bool,
    q_recast_ready: f64,

    e_ready: f64,
    e_land_at: f64,
    e_land_dragon: bool,
    e_dot_next: f64,
    e_dot_ticks_left: i64,

    w_ready: f64,
    w_recast_at: f64,

    r_land_at: f64,
}

impl GenDriver {
    /// Dragon Fury's current value at time `t`, evolved linearly from the
    /// last update point at the current form's constant rate.
    fn fury_now(&self, t: f64) -> f64 {
        let rate = if self.s.in_dragon { -self.fury_drain_per_s } else { self.fury_regen_per_s };
        self.s.fury + rate * (t - self.s.fury_t)
    }

    /// The next time Fury crosses empty (Dragon Form) or full (Human Form).
    fn fury_cross_time(&self, t: f64) -> f64 {
        if self.ranks.r == 0 {
            return INF;
        }
        let f = self.fury_now(t);
        if self.s.in_dragon {
            if self.fury_drain_per_s <= 0.0 {
                return INF;
            }
            pymax(t, t + f / self.fury_drain_per_s)
        } else {
            if self.fury_regen_per_s <= 0.0 {
                return INF;
            }
            pymax(t, t + (self.fury_full - f) / self.fury_regen_per_s)
        }
    }

    /// A hit landed on the dummy: bank the Fury it generates.
    fn add_fury_hit(&mut self, e: &Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        let f = self.fury_now(t);
        let mult = if self.s.in_dragon { self.fury_dragon_mult } else { 1.0 };
        self.s.fury = f + self.fury_gain_per_hit * mult;
        self.s.fury_t = t;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let q_dmg = kit.hit("gen.Q.damage", ranks.q, sheet)?;
        let q_true_dmg = kit.hit("gen.Q.trueDamage", ranks.q, sheet)?;
        let q_cd = kit.at_rank("abilities.Q.cooldownS", ranks.q)?;
        let q_onhit_base = kit.num("gen.Q.onhit.maxHpBase")?;
        let q_onhit_per_bonus_ad = kit.num("gen.Q.onhit.maxHpPerBonusAd")?;
        let q_onhit_ratio = q_onhit_base + q_onhit_per_bonus_ad * sheet.ad_bonus;
        let q_cdr_on_hit_s = kit.num("gen.Q.cdrOnHitS")?;
        let q_lockout_s = kit.num("gen.Q.lockoutDurationS")?;
        let q_max_human = kit.num("gen.Q.maxCastsHuman")? as i64;
        let q_max_dragon = kit.num("gen.Q.maxCastsDragon")? as i64;

        let w_dmg = kit.hit("gen.W.damage", ranks.w, sheet)?;
        let w_cd = kit.at_rank("abilities.W.cooldownS", ranks.w)?;
        let w_recast_delay_s = kit.num("gen.W.recastDelayS")?;

        let e_base = kit.at_rank("gen.E.damage.baseByRank", ranks.e)?;
        let e_ap_ratio = kit.at_rank("gen.E.damage.apRatioByRank", ranks.e)?;
        let e_flat = e_base + e_ap_ratio * sheet.ap;
        let e_hp_ratio = kit.num("gen.E.damage.targetMaxHpRatio")?;
        let e_dragon_mult = kit.num("gen.E.damage.dragonMult")?;
        let e_cd = kit.at_rank("abilities.E.cooldownS", ranks.e)?;
        let e_cast_human_s = kit.num("gen.E.castTimeHumanS")?;
        let e_cast_dragon_s = kit.num("gen.E.castTimeDragonS")?;
        let e_dot_per_sec_base = kit.at_level("gen.E.dot.perSecondByLevel", level)?;
        let e_dot_ap_ratio = kit.num("gen.E.dot.apRatio")?;
        let e_dot_tick_s = kit.num("gen.E.dot.tickIntervalS")?;
        let e_dot_duration_s = kit.num("gen.E.dot.durationS")?;
        let e_dot_per_tick = (e_dot_per_sec_base + e_dot_ap_ratio * sheet.ap) * e_dot_tick_s;
        let e_dot_total_ticks = (e_dot_duration_s / e_dot_tick_s) as i64;

        let r_dmg = kit.hit("gen.R.damage", ranks.r, sheet)?;
        let r_cast_time_s = kit.num("gen.R.castTimeS")?;
        let fury_regen_per_s = kit.at_rank("gen.R.fury.regenPerSecondByRank", ranks.r)?;
        let fury_drain_per_s = kit.num("gen.R.fury.drainPerSecondS")?;
        let fury_gain_per_hit = kit.num("gen.R.fury.gainPerHit")?;
        let fury_dragon_mult = kit.num("gen.R.fury.dragonMult")?;
        let fury_full = kit.num("gen.R.fury.fullFury")?;
        let starting_fury = kit.num("gen.R.fury.startingFury")?;

        let state = State {
            fury: starting_fury,
            fury_t: 0.0,
            in_dragon: false,
            q_armed: false,
            q_step: 0,
            q_chain_active: false,
            q_chain_dragon: false,
            q_recast_ready: INF,
            e_ready: 0.0,
            e_land_at: INF,
            e_land_dragon: false,
            e_dot_next: INF,
            e_dot_ticks_left: 0,
            w_ready: 0.0,
            w_recast_at: INF,
            r_land_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("shyvana kit needs attack.windupFraction")?,
            q_dmg,
            q_true_dmg,
            q_cd,
            q_onhit_ratio,
            q_cdr_on_hit_s,
            q_lockout_s,
            q_max_human,
            q_max_dragon,
            w_dmg,
            w_cd,
            w_recast_delay_s,
            e_flat,
            e_hp_ratio,
            e_dragon_mult,
            e_cd,
            e_cast_human_s,
            e_cast_dragon_s,
            e_dot_per_tick,
            e_dot_tick_s,
            e_dot_total_ticks,
            r_dmg,
            r_cast_time_s,
            fury_regen_per_s,
            fury_drain_per_s,
            fury_gain_per_hit,
            fury_dragon_mult,
            fury_full,
            src_q_true: intern("Q true"),
            src_q_onhit: intern("Q onhit"),
            src_e_dot: intern("E dot"),
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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.add_fury_hit(e);

        if self.ranks.q == 0 {
            return;
        }

        // Emberstrike's passive on-hit magic damage and current-cooldown CDR
        // apply on every basic attack, empowered or not.
        let onhit_dmg = self.q_onhit_ratio * e.target_hp;
        e.deal(onhit_dmg, DType::Magic, self.src_q_onhit, false, false, 1.0);
        if e.st.q_ready > t {
            e.st.q_ready = pymax(t, e.st.q_ready - self.q_cdr_on_hit_s);
        }

        if !self.s.q_armed {
            return;
        }
        let max_step = if self.s.q_chain_dragon { self.q_max_dragon } else { self.q_max_human };
        if self.s.q_step < max_step {
            e.deal(self.q_dmg, DType::Physical, SRC_Q, true, true, 1.0);
        } else {
            e.deal(self.q_true_dmg, DType::True, self.src_q_true, false, true, 1.0);
        }
        e.ability_cast_proc();
        e.eclipse_hit();
        self.s.q_armed = false;
        if self.s.q_step >= max_step {
            self.s.q_chain_active = false;
            self.s.q_step = 0;
            e.st.q_ready = t + e.basic_cd(self.q_cd);
            self.s.q_recast_ready = INF;
        } else {
            self.s.q_recast_ready = t + self.q_lockout_s;
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.q_armed {
            return INF;
        }
        if self.s.q_chain_active {
            return pymax(self.s.q_recast_ready, e.st.t);
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if !self.s.q_chain_active {
            self.s.q_chain_active = true;
            self.s.q_step = 0;
            self.s.q_chain_dragon = self.s.in_dragon;
        }
        self.s.q_step += 1;
        self.s.q_armed = true;
        e.prime_spellblade();
        let b = self.bonus_as(t);
        let windup_at = t + e.attack_windup(b, self.windup_fraction);
        e.st.next_attack = pymin(e.st.next_attack, windup_at);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack; Dragon Form starts immediately
        self.s.in_dragon = true;
        self.s.fury = self.fury_full;
        self.s.fury_t = e.st.t;
        self.s.r_land_at = e.st.t + self.r_cast_time_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            if self.s.e_land_at != INF {
                out[n] = (self.s.e_land_at, Kind::Ev(EV_E_LAND));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
            if self.s.e_dot_ticks_left > 0 {
                out[n] = (self.s.e_dot_next, Kind::Ev(EV_E_DOT));
                n += 1;
            }
        }
        if self.ranks.w > 0 {
            if self.s.w_recast_at != INF {
                out[n] = (self.s.w_recast_at, Kind::Ev(EV_W_RECAST));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_land_at != INF {
                out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
                n += 1;
            }
            out[n] = (self.fury_cross_time(e.st.t), Kind::Ev(EV_FURY));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                let cast_s = if self.s.in_dragon { self.e_cast_dragon_s } else { self.e_cast_human_s };
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_land_at = t + cast_s;
                self.s.e_land_dragon = self.s.in_dragon;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_LAND) => {
                self.s.e_land_at = INF;
                let mut dmg = self.e_flat + self.e_hp_ratio * e.target_hp;
                if self.s.e_land_dragon {
                    dmg *= self.e_dragon_mult;
                }
                e.deal(dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.add_fury_hit(e);
                if self.s.e_land_dragon {
                    self.s.e_dot_ticks_left = self.e_dot_total_ticks;
                    self.s.e_dot_next = t + self.e_dot_tick_s;
                }
            }
            Kind::Ev(EV_E_DOT) => {
                e.deal(self.e_dot_per_tick, DType::Magic, self.src_e_dot, false, true, 1.0);
                self.s.e_dot_ticks_left -= 1;
                if self.s.e_dot_ticks_left > 0 {
                    self.s.e_dot_next = t + self.e_dot_tick_s;
                } else {
                    self.s.e_dot_next = INF;
                }
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_recast_at = t + self.w_recast_delay_s;
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_RECAST) => {
                self.s.w_recast_at = INF;
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.add_fury_hit(e);
            }
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.add_fury_hit(e);
                e.ult_hatefog();
            }
            Kind::Ev(EV_FURY) => {
                self.s.fury_t = t;
                if self.s.in_dragon {
                    self.s.fury = 0.0;
                    self.s.in_dragon = false;
                } else {
                    self.s.fury = self.fury_full;
                    self.s.in_dragon = true;
                    e.lockout();
                    e.prime_spellblade();
                    self.s.r_land_at = t + self.r_cast_time_s;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
