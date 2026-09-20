//! Irelia. Opens with Vanguard's Edge (R) at t=0, then loops Bladesurge (Q)
//! on cooldown (consuming the Unsteady mark left by E/R for a near-instant
//! recooldown), Defiant Dance (W) fully charged before its recast swipe, and
//! Flawless Duet (E) recast at its minimum delay, with basic attacks filling
//! every gap and keeping Ionian Fervor (P) stacked.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const EV_W_CAST: u8 = 0;
const EV_W_CHARGE_DONE: u8 = 1;
const EV_W_SWING: u8 = 2;
const EV_E_CAST: u8 = 3;
const EV_E_RECAST: u8 = 4;
const EV_E_CONVERGE: u8 = 5;
const EV_R_CAST: u8 = 6;
const EV_R_LANDS: u8 = 7;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,

    // Ionian Fervor
    p_duration: f64,
    p_max: i64,
    p_as_pct: f64,
    p_onhit_base: f64,
    p_onhit_adcoef: f64,

    // Bladesurge
    q_dmg: f64,
    q_cd: f64,
    q_mark_cd: f64,

    // Defiant Dance
    w_dmg: f64,
    w_cd: f64,
    w_charge_s: f64,
    w_recast_cast_s: f64,

    // Flawless Duet
    e_dmg: f64,
    e_cd: f64,
    e_recast_delay_s: f64,
    e_converge_s: f64,
    e_mark_duration: f64,

    // Vanguard's Edge
    r_missile_dmg: f64,
    r_zone_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    r_mark_duration: f64,

    src_w: SourceId,
    src_e: SourceId,
    src_r_zone: SourceId,
    src_p_onhit: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    p_until: f64,
    mark_until: f64,

    w_ready: f64,
    w_charge_done_at: f64,
    w_swing_at: f64,

    e_ready: f64,
    e_recast_at: f64,
    e_converge_at: f64,

    r_ready: f64,
    r_lands_at: f64,
}

impl GenDriver {
    fn p_stacks_at(&self, t: f64) -> i64 {
        if t < self.s.p_until {
            self.s.p_stacks
        } else {
            0
        }
    }

    fn p_hit(&mut self, t: f64) {
        let cur = self.p_stacks_at(t);
        self.s.p_stacks = imin(cur + 1, self.p_max);
        self.s.p_until = t + self.p_duration;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            p_until: 0.0,
            mark_until: 0.0,
            w_ready: 0.0,
            w_charge_done_at: INF,
            w_swing_at: INF,
            e_ready: 0.0,
            e_recast_at: INF,
            e_converge_at: INF,
            r_ready: INF,
            r_lands_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,

            p_duration: kit.num("gen.P.stackDurationS")?,
            p_max: kit.num("gen.P.maxStacks")? as i64,
            p_as_pct: kit.at_level("gen.P.asPerStackByLevel", level)?,
            p_onhit_base: kit.at_level("gen.P.onhitBaseByLevel", level)?,
            p_onhit_adcoef: kit.num("gen.P.onhitBonusAdCoef")?,

            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_mark_cd: kit.num("gen.Q.markConsumeCdS")?,

            w_dmg: kit.hit("gen.W.maxDamage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_charge_s: kit.num("gen.W.chargeTimeForMaxS")?,
            w_recast_cast_s: kit.num("gen.W.recastCastTimeS")?,

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_recast_delay_s: kit.num("gen.E.minRecastDelayS")?,
            e_converge_s: kit.num("gen.E.convergeTravelS")?,
            e_mark_duration: kit.num("gen.E.markDurationS")?,

            r_missile_dmg: kit.hit("gen.R.missileDamage", ranks.r, sheet)?,
            r_zone_dmg: kit.hit("gen.R.zoneDamage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_mark_duration: kit.num("gen.R.markDurationS")?,

            src_w: intern("W"),
            src_e: intern("E"),
            src_r_zone: intern("R zone"),
            src_p_onhit: intern("P onhit"),

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
        self.p_as_pct * (self.p_stacks_at(t) as f64)
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        // basic attacks refresh Ionian Fervor's duration but do not add stacks
        let t = e.st.t;
        let cur = self.p_stacks_at(t);
        self.s.p_stacks = cur;
        self.s.p_until = t + self.p_duration;
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.p_stacks_at(t) >= self.p_max {
            let amt = self.p_onhit_base + self.p_onhit_adcoef * e.p.sheet.ad_bonus;
            e.deal(amt, DType::Magic, self.src_p_onhit, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t < self.s.mark_until {
            // consumes the Unsteady mark: cooldown drops to a flat 0.2s
            e.st.q_ready = t + self.q_mark_cd;
            self.s.mark_until = t - 1.0;
        } else {
            e.st.q_ready = t + e.basic_cd(self.q_cd);
        }
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.p_hit(t);
        // the dash and forced follow-up attack are approximated by the
        // standard cast lockout, as Leap Strike is
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // opening cast: engine already primed Spellblade and applied the
        // lockout; the barrage lands when the cast time ends
        self.s.r_lands_at = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_swing_at != INF {
                out[n] = (self.s.w_swing_at, Kind::Ev(EV_W_SWING));
                n += 1;
            } else if self.s.w_charge_done_at != INF {
                out[n] = (self.s.w_charge_done_at, Kind::Ev(EV_W_CHARGE_DONE));
                n += 1;
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
                n += 1;
            }
        }
        if self.ranks.e > 0 {
            if self.s.e_converge_at != INF {
                out[n] = (self.s.e_converge_at, Kind::Ev(EV_E_CONVERGE));
                n += 1;
            } else if self.s.e_recast_at != INF {
                out[n] = (self.s.e_recast_at, Kind::Ev(EV_E_RECAST));
                n += 1;
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
                n += 1;
            }
        }
        if self.ranks.r > 0 {
            if self.s.r_lands_at != INF {
                out[n] = (self.s.r_lands_at, Kind::Ev(EV_R_LANDS));
                n += 1;
            } else {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                // start the channel; hold attacks through channel + recast cast
                self.s.w_charge_done_at = t + self.w_charge_s;
                e.prime_spellblade();
                e.st.next_attack =
                    pymax(e.st.next_attack, t + self.w_charge_s + self.w_recast_cast_s);
            }
            Kind::Ev(EV_W_CHARGE_DONE) => {
                self.s.w_charge_done_at = INF;
                e.lockout();
                e.prime_spellblade();
                self.s.w_swing_at = t + self.w_recast_cast_s;
            }
            Kind::Ev(EV_W_SWING) => {
                self.s.w_swing_at = INF;
                e.deal(self.w_dmg, DType::Physical, self.src_w, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.p_hit(t);
                self.s.w_ready = t + e.basic_cd(self.w_cd);
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_recast_at = t + self.e_recast_delay_s;
                self.s.e_ready = INF;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_RECAST) => {
                self.s.e_recast_at = INF;
                self.s.e_converge_at = t + self.e_converge_s;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_CONVERGE) => {
                self.s.e_converge_at = INF;
                e.deal(self.e_dmg, DType::Magic, self.src_e, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.p_hit(t);
                self.s.mark_until = t + self.e_mark_duration;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_ready = INF;
                e.lockout();
                e.prime_spellblade();
                self.s.r_lands_at = t + self.r_cast_s;
            }
            Kind::Ev(EV_R_LANDS) => {
                self.s.r_lands_at = INF;
                e.deal(self.r_missile_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.deal(self.r_zone_dmg, DType::Magic, self.src_r_zone, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.p_hit(t);
                self.s.mark_until = t + self.r_mark_duration;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
