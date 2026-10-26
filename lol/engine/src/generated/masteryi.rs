//! Master Yi. An auto-attacker whose kit rides attacks (Double Strike, Wuju
//! Style's on-hit true damage) plus a self-vanishing multi-hit ability
//! (Alpha Strike) and an opening attack-speed cooldown (Highlander). R and E
//! are cast at t=0; Q goes out on cooldown, sped up by attacking; E is
//! recast on cooldown; W (Meditate) is never cast since it only interrupts
//! attacking against a dummy that never attacks back.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Alpha Strike's three instantly-detonating marks, then its primary
/// detonation on reappear; then Wuju Style's recast.
const EV_Q_SUB1: u8 = 0;
const EV_Q_SUB2: u8 = 1;
const EV_Q_SUB3: u8 = 2;
const EV_Q_PRIMARY: u8 = 3;
const EV_E_CAST: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Double Strike: stack cap, stack duration, second strike's AD ratio.
    p_max_stacks: i64,
    p_stack_dur: f64,
    p_ad_ratio: f64,
    /// Alpha Strike: primary and per-mark reduced damage, cooldown, on-hit
    /// effectiveness on the primary and the instant marks, the interval
    /// between marks, the total vanish duration, and the on-hit CDR.
    q_dmg: f64,
    q_dmg_sub: f64,
    q_cd: f64,
    q_onhit_primary: f64,
    q_onhit_subsequent: f64,
    q_mark_interval: f64,
    q_total_duration: f64,
    q_cdr_onhit_base_s: f64,
    /// Wuju Style: on-hit true damage, cooldown, duration.
    e_dmg: f64,
    e_cd: f64,
    e_dur: f64,
    /// Highlander: bonus attack speed (percent) and duration.
    r_as_pct: f64,
    r_dur: f64,
    src_p: SourceId,
    src_e: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    p_stack_until: f64,
    p_armed: bool,
    e_until: f64,
    e_ready: f64,
    r_until: f64,
    q_sub1_at: f64,
    q_sub2_at: f64,
    q_sub3_at: f64,
    q_primary_at: f64,
}

impl GenDriver {
    /// Wuju Style's true damage on-hit (if active) and Alpha Strike's
    /// on-hit cooldown reduction; called once for a real attack, and again
    /// for Double Strike's second strike (which also applies on-hit).
    fn apply_basic_onhit(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t < self.s.e_until {
            e.deal(self.e_dmg, DType::True, self.src_e, false, false, 1.0);
        }
        if self.ranks.q > 0 {
            let reduction = e.basic_cd(self.q_cdr_onhit_base_s);
            e.st.q_ready = pymax(t, e.st.q_ready - reduction);
        }
    }

    /// Wuju Style's true damage on-hit at Alpha Strike's reduced
    /// effectiveness (it does not reduce Alpha Strike's own cooldown).
    fn apply_q_onhit(&mut self, e: &mut Engine, effectiveness: f64) {
        let t = e.st.t;
        if t < self.s.e_until {
            e.deal(self.e_dmg * effectiveness, DType::True, self.src_e, false, false, 1.0);
        }
    }

    /// Each Alpha Strike detonation refreshes Wuju Style's and Highlander's
    /// current durations, while they are active.
    fn refresh_e_r(&mut self, e: &Engine) {
        let t = e.st.t;
        if t < self.s.e_until {
            self.s.e_until = t + self.e_dur;
        }
        if t < self.s.r_until {
            self.s.r_until = t + self.r_dur;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            p_stack_until: 0.0,
            p_armed: false,
            e_until: 0.0,
            e_ready: 0.0,
            r_until: -1.0,
            q_sub1_at: INF,
            q_sub2_at: INF,
            q_sub3_at: INF,
            q_primary_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_max_stacks: kit.num("gen.P.maxStacks")? as i64,
            p_stack_dur: kit.num("gen.P.stackDurationS")?,
            p_ad_ratio: kit.num("gen.P.secondStrikeAdRatio")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_dmg_sub: kit.hit("gen.Q.reducedDamage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_onhit_primary: kit.num("gen.Q.onHitPrimary")?,
            q_onhit_subsequent: kit.num("gen.Q.onHitSubsequent")?,
            q_mark_interval: kit.num("gen.Q.markIntervalS")?,
            q_total_duration: kit.num("gen.Q.totalCastDurationS")?,
            q_cdr_onhit_base_s: kit.num("gen.Q.cdrOnHitS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_dur: kit.num("gen.E.durationS")?,
            r_as_pct: kit.at_rank("gen.R.asBonus", ranks.r)? * 100.0,
            r_dur: kit.num("gen.R.durationS")?,
            src_p: intern("P second strike"),
            src_e: intern("E onhit"),
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
        if t < self.s.r_until {
            self.r_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // the real attack's on-hit: Wuju Style true damage, Alpha Strike CDR
        self.apply_basic_onhit(e);
        if self.s.p_armed {
            // Double Strike: consume the stacks for the second strike, which
            // separately rolls a crit and itself applies on-hit effects
            self.s.p_armed = false;
            self.s.p_stacks = 0;
            let t = e.st.t;
            let dmg = self.p_ad_ratio * e.p.ad;
            e.deal(dmg, DType::Physical, self.src_p, true, false, 1.0);
            self.apply_basic_onhit(e);
            // the second strike's own on-hit is able to add a fresh stack
            self.s.p_stacks = imin(1, self.p_max_stacks);
            self.s.p_stack_until = t + self.p_stack_dur;
        } else {
            let t = e.st.t;
            if t > self.s.p_stack_until {
                self.s.p_stacks = 0;
            }
            self.s.p_stacks = imin(self.s.p_stacks + 1, self.p_max_stacks);
            self.s.p_stack_until = t + self.p_stack_dur;
            if self.s.p_stacks >= self.p_max_stacks {
                self.s.p_armed = true;
            }
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // the vanish: cooldown starts on-cast, marks resolve as timed events,
        // and Master Yi cannot attack for the full vanish duration
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.q_sub1_at = t + self.q_mark_interval * 2.0;
        self.s.q_sub2_at = t + self.q_mark_interval * 3.0;
        self.s.q_sub3_at = t + self.q_mark_interval * 4.0;
        self.s.q_primary_at = t + self.q_total_duration;
        e.st.next_attack = pymax(e.st.next_attack, t + self.q_total_duration);
        e.prime_spellblade();
        e.ability_cast_proc();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_until = t + self.r_dur;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_sub1_at != INF {
            out[n] = (self.s.q_sub1_at, Kind::Ev(EV_Q_SUB1));
            n += 1;
        }
        if self.s.q_sub2_at != INF {
            out[n] = (self.s.q_sub2_at, Kind::Ev(EV_Q_SUB2));
            n += 1;
        }
        if self.s.q_sub3_at != INF {
            out[n] = (self.s.q_sub3_at, Kind::Ev(EV_Q_SUB3));
            n += 1;
        }
        if self.s.q_primary_at != INF {
            out[n] = (self.s.q_primary_at, Kind::Ev(EV_Q_PRIMARY));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        match kind {
            Kind::Ev(EV_Q_SUB1) => {
                self.s.q_sub1_at = INF;
                e.deal(self.q_dmg_sub, DType::Physical, SRC_Q, true, true, 1.0);
                // the first damage instance of this cast: the one that
                // counts for effects like Electrocute and Eclipse
                e.eclipse_hit();
                self.apply_q_onhit(e, self.q_onhit_subsequent);
                self.refresh_e_r(e);
            }
            Kind::Ev(EV_Q_SUB2) => {
                self.s.q_sub2_at = INF;
                e.deal(self.q_dmg_sub, DType::Physical, SRC_Q, true, true, 1.0);
                self.apply_q_onhit(e, self.q_onhit_subsequent);
                self.refresh_e_r(e);
            }
            Kind::Ev(EV_Q_SUB3) => {
                self.s.q_sub3_at = INF;
                e.deal(self.q_dmg_sub, DType::Physical, SRC_Q, true, true, 1.0);
                self.apply_q_onhit(e, self.q_onhit_subsequent);
                self.refresh_e_r(e);
            }
            Kind::Ev(EV_Q_PRIMARY) => {
                self.s.q_primary_at = INF;
                e.deal(self.q_dmg, DType::Physical, SRC_Q, true, true, 1.0);
                self.apply_q_onhit(e, self.q_onhit_primary);
                self.refresh_e_r(e);
            }
            Kind::Ev(EV_E_CAST) => {
                let t = e.st.t;
                self.s.e_until = t + self.e_dur;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
