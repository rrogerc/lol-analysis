//! Ekko. Attacks and Q/E/R damage all feed Z-Drive Resonance (every third
//! hit on a target consumes its stacks for bonus magic damage); Phase Dive
//! (E) is woven in after an attack for its reset, arming the following
//! attack's empowered, fixed-cast-time bonus damage; Parallel Convergence
//! (W) is never actively cast, only its missing-health on-hit passive
//! fires, riding every attack; Chronobreak (R) opens the fight and is
//! recast on cooldown for its explosion.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Timewinder's return hit, scheduled after the outgoing hit.
const EV_Q_RETURN: u8 = 0;
/// Chronobreak's explosion, then its next cast when off cooldown.
const EV_R_EXPLODE: u8 = 1;
const EV_R_CAST: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Z-Drive Resonance: the third-stack bonus, its cap, refresh window and
    /// per-target lockout after it consumes.
    p_dmg: f64,
    p_max_stacks: i64,
    p_stack_window: f64,
    p_lockout_s: f64,
    q_out_dmg: f64,
    q_return_dmg: f64,
    q_cd: f64,
    q_expand_s: f64,
    /// Parallel Convergence's passive: fraction of the target's missing
    /// health, and the max-health fraction it requires to trigger.
    w_frac: f64,
    w_threshold: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    r_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    src_p: SourceId,
    src_q_return: SourceId,
    src_e: SourceId,
    src_w: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    p_last_hit: f64,
    p_lockout_until: f64,
    e_armed: bool,
    e_ready: f64,
    /// When Timewinder's return hit lands (INF: none pending).
    q_return_at: f64,
    /// When Chronobreak's explosion lands (INF: none pending), and when it
    /// is next ready to be (re)cast (INF: not yet scheduled).
    r_explode_at: f64,
    r_ready: f64,
}

impl GenDriver {
    /// Applies a Resonance stack from a hit and, on the third, consumes
    /// them all for the bonus magic damage.
    fn resonance_hit(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t < self.s.p_lockout_until {
            return;
        }
        if t - self.s.p_last_hit > self.p_stack_window {
            self.s.p_stacks = 0;
        }
        self.s.p_last_hit = t;
        self.s.p_stacks += 1;
        if self.s.p_stacks >= self.p_max_stacks {
            self.s.p_stacks = 0;
            self.s.p_lockout_until = t + self.p_lockout_s;
            e.deal(self.p_dmg, DType::Magic, self.src_p, false, false, 1.0);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            p_last_hit: -INF,
            p_lockout_until: 0.0,
            e_armed: false,
            e_ready: 0.0,
            q_return_at: INF,
            r_explode_at: INF,
            r_ready: INF,
        };
        let p_base = kit.at_level("gen.P.threeHitDamage.baseByLevel", level)?;
        let p_ap_ratio = kit.num("gen.P.threeHitDamage.apRatio")?;
        let w_base_pct = kit.num("gen.W.baseOnHitPct")?;
        let w_ap_coef_pct = kit.num("gen.W.apCoefPct")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("ekko kit needs attack.windupFraction")?,
            p_dmg: p_base + p_ap_ratio * sheet.ap,
            p_max_stacks: kit.num("gen.P.maxStacks")? as i64,
            p_stack_window: kit.num("gen.P.stackDurationS")?,
            p_lockout_s: kit.num("gen.P.lockoutS")?,
            q_out_dmg: kit.hit("gen.Q.outgoingDamage", ranks.q, sheet)?,
            q_return_dmg: kit.hit("gen.Q.returnDamage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_expand_s: kit.num("gen.Q.expandDelayS")?,
            w_frac: (w_base_pct + w_ap_coef_pct * sheet.ap) / 100.0,
            w_threshold: kit.num("gen.W.belowHealthThresholdPct")? / 100.0,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_p: intern("P"),
            src_q_return: intern("Q return"),
            src_e: intern("E"),
            src_w: intern("W onhit"),
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
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Resonance stacks on-hit; W's missing-health bonus is also an
        // on-hit rider, gated below the 30% max-health threshold.
        self.resonance_hit(e);
        let cur_hp = pymax(e.st.hp, 0.0);
        if self.ranks.w > 0 && cur_hp < self.w_threshold * e.target_hp {
            let missing = e.target_hp - cur_hp;
            e.deal(self.w_frac * missing, DType::Magic, self.src_w, false, false, 1.0);
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.e_armed {
            let t = e.st.t;
            self.s.e_armed = false;
            self.s.e_ready = t + e.basic_cd(self.e_cd);
            e.deal(self.e_dmg, DType::Magic, self.src_e, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.ranks.e > 0 && !self.s.e_armed && t >= self.s.e_ready {
            // Phase Dive, woven in right after an attack: it resets the
            // attack timer, so the next attack is the empowered one with a
            // fixed cast time instead of the usual windup.
            self.s.e_armed = true;
            e.prime_spellblade();
            e.st.next_attack = t + self.e_cast_s;
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_out_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        self.resonance_hit(e);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.q_return_at = e.st.t + self.q_expand_s;
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_explode_at = t + self.r_cast_s;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        e.lockout();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_return_at != INF {
            out[n] = (self.s.q_return_at, Kind::Ev(EV_Q_RETURN));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_explode_at != INF {
                out[n] = (self.s.r_explode_at, Kind::Ev(EV_R_EXPLODE));
                n += 1;
            } else if self.s.r_ready != INF {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_RETURN) => {
                self.s.q_return_at = INF;
                e.deal(self.q_return_dmg, DType::Magic, self.src_q_return, false, true, 1.0);
                self.resonance_hit(e);
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_explode_at = t + self.r_cast_s;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.lockout();
            }
            Kind::Ev(EV_R_EXPLODE) => {
                self.s.r_explode_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                self.resonance_hit(e);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
