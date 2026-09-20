//! Poppy. Iron Ambassador periodically arms an on-hit magic proc on her next
//! attack (checked at windup start); Hammer Shock is cast on cooldown for
//! its two same-instance hits 1s apart; Steadfast Presence and Heroic
//! Charge are cast on cooldown for their instant damage; Keeper's Verdict
//! opens the fight, channels the 0.5s minimum for its charged tier, then
//! releases, holding attacks through the whole channel and release cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Hammer Shock's rupture, 1s after the impact (same cast instance).
const EV_Q_RUPTURE: u8 = 0;
/// Steadfast Presence, cast on cooldown.
const EV_W_CAST: u8 = 1;
/// Heroic Charge, cast on cooldown.
const EV_E_CAST: u8 = 2;
/// Keeper's Verdict: the channel starting, its release, and the damage.
const EV_R_START: u8 = 3;
const EV_R_RELEASE: u8 = 4;
const EV_R_DAMAGE: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Iron Ambassador: cooldown (level-scaled, not haste-reduced) and its
    /// bonus magic damage.
    p_cd: f64,
    p_dmg: f64,
    q_cd: f64,
    q_dmg: f64,
    q_hp_ratio: f64,
    q_delay: f64,
    w_cd: f64,
    w_dmg: f64,
    e_cd: f64,
    e_dmg: f64,
    r_cd: f64,
    r_dmg: f64,
    r_charge_s: f64,
    r_cast_s: f64,
    src_p: SourceId,
    src_q_rupture: SourceId,
    src_w: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_ready: f64,
    p_armed: bool,
    w_ready: f64,
    e_ready: f64,
    q_rupture_at: f64,
    r_ready: f64,
    r_recast_at: f64,
    r_damage_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_ready: 0.0,
            p_armed: false,
            w_ready: 0.0,
            e_ready: 0.0,
            q_rupture_at: INF,
            r_ready: 0.0,
            r_recast_at: INF,
            r_damage_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_cd: kit.at_level("gen.P.cooldownByLevel", level)?,
            p_dmg: kit.at_level("gen.P.damageByLevel", level)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_hp_ratio: kit.at_rank("gen.Q.targetMaxHpRatioPct", ranks.q)? / 100.0,
            q_delay: kit.num("gen.Q.delayBetweenHitsS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_charge_s: kit.num("gen.R.chargeThresholdS")?,
            r_cast_s: kit.num("gen.R.castTimeChargedS")?,
            src_p: intern("P"),
            src_q_rupture: intern("Q rupture"),
            src_w: intern("W"),
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
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        // Iron Ambassador: armed if its (haste-immune) cooldown has elapsed
        // by the time the windup begins.
        if e.st.t >= self.s.p_ready {
            self.s.p_armed = true;
            self.s.p_ready = e.st.t + self.p_cd;
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.s.p_armed {
            self.s.p_armed = false;
            e.deal(self.p_dmg, DType::Magic, self.src_p, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let dmg = self.q_dmg + self.q_hp_ratio * e.target_hp;
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
        self.s.q_rupture_at = e.st.t + self.q_delay;
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: begin the channel, hold attacks through the
        // channel and the eventual release cast
        let t = e.st.t;
        e.prime_spellblade();
        self.s.r_recast_at = t + self.r_charge_s;
        e.st.next_attack = pymax(e.st.next_attack, t + self.r_charge_s + self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_rupture_at != INF {
            out[n] = (self.s.q_rupture_at, Kind::Ev(EV_Q_RUPTURE));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_damage_at != INF {
                out[n] = (self.s.r_damage_at, Kind::Ev(EV_R_DAMAGE));
            } else if self.s.r_recast_at != INF {
                out[n] = (self.s.r_recast_at, Kind::Ev(EV_R_RELEASE));
            } else {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_START));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_RUPTURE) => {
                self.s.q_rupture_at = INF;
                let dmg = self.q_dmg + self.q_hp_ratio * e.target_hp;
                e.deal(dmg, DType::Physical, self.src_q_rupture, false, true, 1.0);
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, self.src_w, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_START) => {
                self.s.r_recast_at = t + self.r_charge_s;
                e.prime_spellblade();
                e.st.next_attack = pymax(e.st.next_attack, t + self.r_charge_s + self.r_cast_s);
            }
            Kind::Ev(EV_R_RELEASE) => {
                self.s.r_recast_at = INF;
                self.s.r_damage_at = t + self.r_cast_s;
            }
            Kind::Ev(EV_R_DAMAGE) => {
                self.s.r_damage_at = INF;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
