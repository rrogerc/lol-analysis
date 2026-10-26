//! Xin Zhao. R opens the fight at t=0 (largest current-health term against a
//! full dummy); W and E are cast as soon as each is off cooldown (W first on
//! ties); Q is cast as soon as it is off cooldown and not already armed, and
//! arms the next three basic attacks, each of which resets the attack timer
//! and shaves 1s off W/E's cooldowns; Q's own cooldown starts when the third
//! empowered attack lands, or when the 5s window expires, whichever first.
//! Every attack and W's first slash/thrust feed Determination, whose third
//! stack procs non-crit proc damage.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const EV_W_CAST: u8 = 0;
const EV_W_SLASH: u8 = 1;
const EV_W_THRUST: u8 = 2;
const EV_E_CAST: u8 = 3;
const EV_R_DMG: u8 = 4;
const EV_Q_EXPIRE: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    p_stack_cap: i64,
    p_ad_coef: f64,
    p_ap_coef: f64,
    src_p: SourceId,

    q_dmg: f64,
    q_cd: f64,
    q_window_s: f64,
    q_cdr_s: f64,

    w_cd: f64,
    w_slash_dmg: f64,
    w_thrust_dmg: f64,
    w_crit_amp: f64,
    w_cast_s: f64,
    w_slash_interval: f64,
    w_slash_remaining: i64,
    src_w_slash: SourceId,
    src_w_thrust: SourceId,

    e_cd: f64,
    e_dmg: f64,
    e_as_bonus_pct: f64,
    e_as_duration_s: f64,

    r_dmg_base: f64,
    r_target_hp_ratio: f64,
    r_cast_s: f64,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,

    q_armed: i64,
    q_window_until: f64,
    q_attack_flag: bool,

    w_ready: f64,
    w_slashes_left: i64,
    w_next_slash_at: f64,
    w_thrust_at: f64,

    e_ready: f64,
    e_as_until: f64,

    r_dmg_at: f64,
}

impl GenDriver {
    fn proc_determination(&mut self, e: &mut Engine) {
        self.s.p_stacks += 1;
        if self.s.p_stacks >= self.p_stack_cap {
            self.s.p_stacks = 0;
            let dmg = self.p_ad_coef * e.p.ad + self.p_ap_coef * e.p.sheet.ap;
            e.deal(dmg, DType::Physical, self.src_p, false, false, 1.0);
        }
    }

    /// Ends the current Q arming window (whether by the third attack landing
    /// or the window lapsing) and starts Q's post-effect cooldown.
    fn end_q_window(&mut self, e: &mut Engine, t: f64) {
        self.s.q_armed = 0;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let e_as_frac = kit.at_rank("gen.E.asBase", ranks.e)?
            + kit.num("gen.E.apToAsRatio")? * sheet.ap
            + kit.num("gen.E.permanentAsToAsRatio")? * (sheet.bonus_as_pct / 100.0);

        let state = State {
            p_stacks: 0,
            q_armed: 0,
            q_window_until: 0.0,
            q_attack_flag: false,
            w_ready: 0.0,
            w_slashes_left: 0,
            w_next_slash_at: INF,
            w_thrust_at: INF,
            e_ready: 0.0,
            e_as_until: 0.0,
            r_dmg_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("xinzhao kit needs attack.windupFraction")?,

            p_stack_cap: kit.num("gen.P.stackCap")? as i64,
            p_ad_coef: kit.at_level("gen.P.dmgAdCoefByLevel", level)?,
            p_ap_coef: kit.at_level("gen.P.dmgApCoefByLevel", level)?,
            src_p: intern("P proc"),

            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_window_s: kit.num("gen.Q.windowS")?,
            q_cdr_s: kit.num("gen.Q.otherCdReductionS")?,

            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_slash_dmg: kit.hit("gen.W.slashDamage", ranks.w, sheet)?,
            w_thrust_dmg: kit.hit("gen.W.thrustDamage", ranks.w, sheet)?,
            w_crit_amp: kit.num("gen.W.critChanceAmp")?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            w_slash_interval: kit.num("gen.W.slashIntervalS")?,
            w_slash_remaining: kit.num("gen.W.slashCount")? as i64 - 1,
            src_w_slash: intern("W slash"),
            src_w_thrust: intern("W thrust"),

            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_as_bonus_pct: e_as_frac * 100.0,
            e_as_duration_s: kit.num("gen.E.asDurationS")?,

            r_dmg_base: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_target_hp_ratio: kit.num("gen.R.targetCurrentHpRatio")?,
            r_cast_s: kit.num("gen.R.castTimeS")?,

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
        if t < self.s.e_as_until {
            self.e_as_bonus_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        if self.ranks.q > 0 && self.s.q_armed > 0 && e.st.t <= self.s.q_window_until {
            self.s.q_armed -= 1;
            self.s.q_attack_flag = true;
            e.deal(self.q_dmg, DType::Physical, SRC_Q, true, true, 1.0);
            if self.s.q_armed == 0 {
                self.end_q_window(e, e.st.t);
            }
            self.s.w_ready = pymax(self.s.w_ready - self.q_cdr_s, e.st.t);
            self.s.e_ready = pymax(self.s.e_ready - self.q_cdr_s, e.st.t);
        } else {
            self.s.q_attack_flag = false;
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        self.proc_determination(e);
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.s.q_attack_flag {
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.q_armed > 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        self.s.q_armed = 2 + 1;
        self.s.q_window_until = e.st.t + self.q_window_s;
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        self.s.r_dmg_at = e.st.t + self.r_cast_s;
        e.lockout();
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_slashes_left > 0 {
                out[n] = (self.s.w_next_slash_at, Kind::Ev(EV_W_SLASH));
                n += 1;
            } else if self.s.w_thrust_at != INF {
                out[n] = (self.s.w_thrust_at, Kind::Ev(EV_W_THRUST));
                n += 1;
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
                n += 1;
            }
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.r_dmg_at != INF {
            out[n] = (self.s.r_dmg_at, Kind::Ev(EV_R_DMG));
            n += 1;
        }
        if self.ranks.q > 0 && self.s.q_armed > 0 {
            out[n] = (self.s.q_window_until, Kind::Ev(EV_Q_EXPIRE));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.lockout();
                e.prime_spellblade();
                e.deal(self.w_slash_dmg, DType::Physical, self.src_w_slash, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.proc_determination(e);
                self.s.w_slashes_left = self.w_slash_remaining;
                self.s.w_next_slash_at = t + self.w_slash_interval;
                self.s.w_thrust_at = t + self.w_cast_s;
            }
            Kind::Ev(EV_W_SLASH) => {
                e.deal(self.w_slash_dmg, DType::Physical, self.src_w_slash, false, true, 1.0);
                self.s.w_slashes_left -= 1;
                if self.s.w_slashes_left > 0 {
                    self.s.w_next_slash_at = t + self.w_slash_interval;
                } else {
                    self.s.w_next_slash_at = INF;
                }
            }
            Kind::Ev(EV_W_THRUST) => {
                self.s.w_thrust_at = INF;
                let crit_frac = e.p.sheet.crit_chance / 100.0;
                let amp = 1.0 + self.w_crit_amp * crit_frac;
                let dmg = self.w_thrust_dmg * amp;
                e.deal(dmg, DType::Physical, self.src_w_thrust, false, true, 1.0);
                self.proc_determination(e);
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.e_as_until = t + self.e_as_duration_s;
            }
            Kind::Ev(EV_R_DMG) => {
                self.s.r_dmg_at = INF;
                let dmg = self.r_dmg_base + self.r_target_hp_ratio * pymax(e.st.hp, 0.0);
                e.deal(dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_Q_EXPIRE) => {
                // the 5s window lapsed before the third empowered attack
                // landed: end it here so the cooldown still advances
                if self.s.q_armed > 0 {
                    self.end_q_window(e, t);
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
