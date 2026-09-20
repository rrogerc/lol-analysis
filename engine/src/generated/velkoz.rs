//! Vel'Koz. Organic Deconstruction stacks off every ability hit and attack,
//! bursting true damage on the third stack and marking the target
//! Researched; Plasma Fission, Void Rift (2 charges) and Tectonic Disruption
//! all fire on cooldown from t=0; Life Form Disintegration Ray is held until
//! the target is Researched (or a fallback time) so its whole channel deals
//! true damage, and while it channels everything else is locked out.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const EV_E_CAST: u8 = 0;
const EV_E_LAND: u8 = 1;
const EV_W_CAST: u8 = 2;
const EV_W_INITIAL: u8 = 3;
const EV_W_SECONDARY: u8 = 4;
const EV_W_CHARGE: u8 = 5;
const EV_R_START: u8 = 6;
const EV_R_TICK: u8 = 7;
const EV_R_STACK: u8 = 8;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    p_dmg: f64,
    p_duration: f64,
    p_max: i64,
    src_p: SourceId,

    q_dmg: f64,
    q_cd: f64,

    w_initial_dmg: f64,
    w_secondary_dmg: f64,
    w_initial_delay: f64,
    w_secondary_delay: f64,
    w_static_cd: f64,
    w_recharge_s: f64,
    w_max_charges: i64,
    w_start_charges: i64,
    src_w_i: SourceId,
    src_w_s: SourceId,

    e_dmg: f64,
    e_land_delay: f64,
    e_cd: f64,

    r_tick_dmg: f64,
    r_predelay: f64,
    r_tick_interval: f64,
    r_stack_interval: f64,
    r_max_ticks: i64,
    r_max_stacks: i64,
    r_cd: f64,
    r_fallback_t: f64,
    src_r: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    p_expiry: f64,
    research_until: f64,

    e_ready: f64,
    e_land_at: f64,

    w_ready: f64,
    w_charges: i64,
    w_charge_at: f64,
    w_stage: i64,
    w_stage_at: f64,

    r_ready: f64,
    r_channel: bool,
    r_lock_until: f64,
    r_tick_next_at: f64,
    r_ticks_done: i64,
    r_stack_next_at: f64,
    r_stacks_done: i64,
}

impl GenDriver {
    fn apply_p(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t < self.s.research_until {
            self.s.research_until = t + self.p_duration;
        }
        if t > self.s.p_expiry {
            self.s.p_stacks = 0;
        }
        self.s.p_stacks += 1;
        self.s.p_expiry = t + self.p_duration;
        if self.s.p_stacks >= self.p_max {
            self.s.p_stacks = 0;
            self.s.research_until = t + self.p_duration;
            e.deal(self.p_dmg, DType::True, self.src_p, false, false, 1.0);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool) -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            p_expiry: 0.0,
            research_until: 0.0,
            e_ready: 0.0,
            e_land_at: INF,
            w_ready: 0.0,
            w_charges: kit.num("gen.W.startCharges")? as i64,
            w_charge_at: INF,
            w_stage: 0,
            w_stage_at: INF,
            r_ready: 0.0,
            r_channel: false,
            r_lock_until: 0.0,
            r_tick_next_at: INF,
            r_ticks_done: 0,
            r_stack_next_at: INF,
            r_stacks_done: 0,
        };
        let p_duration = kit.num("gen.P.durationS")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("velkoz kit needs attack.windupFraction")?,

            p_dmg: kit.at_level("gen.P.trueDamageByLevel", level)? + kit.num("gen.P.apRatio")? * sheet.ap,
            p_duration,
            p_max: kit.num("gen.P.maxStacks")? as i64,
            src_p: intern("P burst"),

            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,

            w_initial_dmg: kit.hit("gen.W.initialDamage", ranks.w, sheet)?,
            w_secondary_dmg: kit.hit("gen.W.secondaryDamage", ranks.w, sheet)?,
            w_initial_delay: kit.num("gen.W.initialDelayS")?,
            w_secondary_delay: kit.num("gen.W.secondaryDelayS")?,
            w_static_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_recharge_s: kit.at_rank("gen.W.rechargeS", ranks.w)?,
            w_max_charges: kit.num("gen.W.maxCharges")? as i64,
            w_start_charges: kit.num("gen.W.startCharges")? as i64,
            src_w_i: intern("W initial"),
            src_w_s: intern("W secondary"),

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_land_delay: kit.num("gen.E.landDelayS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,

            r_tick_dmg: kit.hit("gen.R.tickDamage", ranks.r, sheet)?,
            r_predelay: kit.num("gen.R.preDelayS")?,
            r_tick_interval: kit.num("gen.R.tickIntervalS")?,
            r_stack_interval: kit.num("gen.R.stackIntervalS")?,
            r_max_ticks: kit.num("gen.R.tickCount")? as i64,
            r_max_stacks: kit.num("gen.R.stackApplications")? as i64,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_fallback_t: p_duration,
            src_r: intern("R"),

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
        shave(&mut self.s.w_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        if !self.s.r_channel {
            self.apply_p(e);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        let mut next = t + e.attack_period(b);
        if self.s.r_channel && next < self.s.r_lock_until {
            next = self.s.r_lock_until;
        }
        e.st.next_attack = next;
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.r_channel {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        self.apply_p(e);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, _e: &mut Engine) {
        // R's actual start time is decided by our own events() below (it
        // waits for the target to be Researched, or a fallback time).
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        let t = e.st.t;

        if self.ranks.e > 0 {
            if self.s.e_land_at != INF {
                out[n] = (self.s.e_land_at, Kind::Ev(EV_E_LAND));
                n += 1;
            } else {
                let mut ready = pymax(self.s.e_ready, t);
                if self.s.r_channel {
                    ready = pymax(ready, self.s.r_lock_until);
                }
                out[n] = (ready, Kind::Ev(EV_E_CAST));
                n += 1;
            }
        }

        if self.ranks.w > 0 {
            if self.s.w_stage != 0 {
                let kind = if self.s.w_stage == 1 { EV_W_INITIAL } else { EV_W_SECONDARY };
                out[n] = (self.s.w_stage_at, Kind::Ev(kind));
                n += 1;
            } else if self.s.w_charges > 0 {
                let mut ready = pymax(self.s.w_ready, t);
                if self.s.r_channel {
                    ready = pymax(ready, self.s.r_lock_until);
                }
                out[n] = (ready, Kind::Ev(EV_W_CAST));
                n += 1;
            }
            if self.s.w_charge_at != INF {
                out[n] = (self.s.w_charge_at, Kind::Ev(EV_W_CHARGE));
                n += 1;
            }
        }

        if self.ranks.r > 0 {
            if self.s.r_channel {
                if self.s.r_tick_next_at != INF {
                    out[n] = (self.s.r_tick_next_at, Kind::Ev(EV_R_TICK));
                    n += 1;
                }
                if self.s.r_stack_next_at != INF {
                    out[n] = (self.s.r_stack_next_at, Kind::Ev(EV_R_STACK));
                    n += 1;
                }
            } else {
                let mut target = pymax(self.s.r_ready, t);
                if self.s.research_until <= 0.0 {
                    target = pymax(target, self.r_fallback_t);
                }
                out[n] = (target, Kind::Ev(EV_R_START));
                n += 1;
            }
        }

        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_land_at = t + self.e_land_delay;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_LAND) => {
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.apply_p(e);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.e_land_at = INF;
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_charges -= 1;
                self.s.w_ready = t + e.basic_cd(self.w_static_cd);
                if self.s.w_charge_at == INF {
                    self.s.w_charge_at = t + e.basic_cd(self.w_recharge_s);
                }
                self.s.w_stage = 1;
                self.s.w_stage_at = t + self.w_initial_delay;
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_INITIAL) => {
                e.deal(self.w_initial_dmg, DType::Magic, self.src_w_i, false, true, 1.0);
                self.apply_p(e);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.w_stage = 2;
                self.s.w_stage_at = t + self.w_secondary_delay;
            }
            Kind::Ev(EV_W_SECONDARY) => {
                e.deal(self.w_secondary_dmg, DType::Magic, self.src_w_s, false, true, 1.0);
                self.apply_p(e);
                self.s.w_stage = 0;
                self.s.w_stage_at = INF;
            }
            Kind::Ev(EV_W_CHARGE) => {
                self.s.w_charges = imin(self.s.w_charges + 1, self.w_max_charges);
                if self.s.w_charges < self.w_max_charges {
                    self.s.w_charge_at = t + e.basic_cd(self.w_recharge_s);
                } else {
                    self.s.w_charge_at = INF;
                }
            }
            Kind::Ev(EV_R_START) => {
                self.s.r_channel = true;
                let channel_start = t + self.r_predelay;
                self.s.r_lock_until = channel_start + self.r_tick_interval * (self.r_max_ticks as f64);
                self.s.r_tick_next_at = channel_start;
                self.s.r_ticks_done = 0;
                self.s.r_stack_next_at = channel_start;
                self.s.r_stacks_done = 0;
                e.prime_spellblade();
                e.st.next_attack = pymax(e.st.next_attack, self.s.r_lock_until);
            }
            Kind::Ev(EV_R_TICK) => {
                let researched = t < self.s.research_until;
                let dtype = if researched { DType::True } else { DType::Magic };
                e.deal(self.r_tick_dmg, dtype, self.src_r, false, true, 1.0);
                if self.s.r_ticks_done == 0 {
                    e.ability_cast_proc();
                    e.eclipse_hit();
                }
                e.ult_hatefog();
                self.s.r_ticks_done += 1;
                if self.s.r_ticks_done >= self.r_max_ticks {
                    self.s.r_tick_next_at = INF;
                    self.s.r_stack_next_at = INF;
                    self.s.r_channel = false;
                    self.s.r_ready = t + e.ult_cd(self.r_cd);
                } else {
                    self.s.r_tick_next_at = t + self.r_tick_interval;
                }
            }
            Kind::Ev(EV_R_STACK) => {
                self.apply_p(e);
                self.s.r_stacks_done += 1;
                if self.s.r_stacks_done >= self.r_max_stacks {
                    self.s.r_stack_next_at = INF;
                } else {
                    self.s.r_stack_next_at = t + self.r_stack_interval;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
