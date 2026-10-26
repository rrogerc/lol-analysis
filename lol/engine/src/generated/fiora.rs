//! Fiora. Grand Challenge opens the fight to reveal all four Vitals at once,
//! Lunge and Bladework are cast the instant they are off cooldown (Lunge's
//! landed stab halves its own cooldown), Riposte is cast once for its
//! guaranteed shock, and Duelist's Dance procs its bonus true damage on the
//! first qualifying hit after each Vital becomes available.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Bladework becomes castable again; Riposte's cast, then its delayed shock.
const EV_E_CAST: u8 = 0;
const EV_W_CAST: u8 = 1;
const EV_W_SHOCK: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    p_vital_delay: f64,
    p_pct: f64,
    q_dmg: f64,
    q_cd: f64,
    q_refund_mult: f64,
    w_dmg: f64,
    w_cd: f64,
    w_parry_s: f64,
    w_shock_delay_s: f64,
    e_cd: f64,
    e_as_pct: f64,
    e_crit_mult: f64,
    e_buff_s: f64,
    r_vitals_count: i64,
    r_reveal_delay_s: f64,
    r_mark_duration_s: f64,
    src_p: SourceId,
    src_e_crit: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    e_ready: f64,
    e_charges: i64,
    e_buff_until: f64,
    /// Which of the two empowered attacks just landed (0 none, 1 first, 2 second).
    e_current_hit: i64,
    w_ready: f64,
    w_cast_at: f64,
    /// When Riposte's shock lands (INF: none pending).
    w_shock_at: f64,
    /// When the next normally-cycling Vital becomes available.
    p_normal_ready: f64,
    /// When Grand Challenge's four Vitals become available (INF: none pending).
    p_r_ready_at: f64,
    p_r_vitals_left: i64,
}

impl GenDriver {
    /// A qualifying hit landed: spend a Grand Challenge Vital if one is
    /// available, otherwise the normally-cycling Vital if it is ready.
    fn p_check(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.p_r_vitals_left > 0 && t >= self.s.p_r_ready_at {
            self.s.p_r_vitals_left -= 1;
            self.p_proc(e);
        } else if t >= self.s.p_normal_ready {
            self.s.p_normal_ready = t + self.p_vital_delay;
            self.p_proc(e);
        }
    }

    fn p_proc(&mut self, e: &mut Engine) {
        let amt = self.p_pct * e.target_hp;
        e.deal(amt, DType::True, self.src_p, false, false, 1.0);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_vital_delay = kit.num("gen.P.vitalDelayS")?;
        let p_base = kit.num("gen.P.damage.base")?;
        let p_ad_ratio = kit.num("gen.P.damage.adRatio")?;
        let state = State {
            e_ready: 0.0,
            e_charges: 0,
            e_buff_until: 0.0,
            e_current_hit: 0,
            w_ready: 0.0,
            w_cast_at: 0.0,
            w_shock_at: INF,
            p_normal_ready: p_vital_delay,
            p_r_ready_at: INF,
            p_r_vitals_left: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("fiora kit needs attack.windupFraction")?,
            p_vital_delay,
            p_pct: p_base + p_ad_ratio * sheet.ad_bonus,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_refund_mult: 1.0 - kit.num("gen.Q.cdRefundPercent")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_parry_s: kit.num("gen.W.parryDurationS")?,
            w_shock_delay_s: kit.num("gen.W.shockDelayS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_as_pct: kit.at_rank("gen.E.asPercent", ranks.e)? * 100.0,
            e_crit_mult: kit.at_rank("gen.E.critMult", ranks.e)?,
            e_buff_s: kit.num("gen.E.buffDurationS")?,
            r_vitals_count: kit.num("gen.R.vitalsCount")? as i64,
            r_reveal_delay_s: kit.num("gen.R.revealDelayS")?,
            r_mark_duration_s: kit.num("gen.R.markDurationS")?,
            src_p: intern("P vital"),
            src_e_crit: intern("E crit"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        false
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self, t: f64) -> f64 {
        if self.s.e_charges > 0 && t < self.s.e_buff_until {
            self.e_as_pct
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
        let t = e.st.t;
        if self.s.e_charges > 0 {
            if t < self.s.e_buff_until {
                let hit_num = 3 - self.s.e_charges;
                self.s.e_charges -= 1;
                self.s.e_current_hit = hit_num;
                if self.s.e_charges == 0 {
                    self.s.e_ready = t + e.basic_cd(self.e_cd);
                }
                return;
            } else {
                // the buff expired before the remaining empowered attacks landed
                self.s.e_charges = 0;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
        }
        self.s.e_current_hit = 0;
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.e_current_hit == 2 {
            // correct this attack's crit onto Bladework's own crit-damage value
            let normal_mult = 1.0
                + (e.p.sheet.crit_chance / 100.0) * (e.p.sheet.crit_damage / 100.0 - 1.0);
            let bonus = e.p.ad * (self.e_crit_mult - normal_mult);
            e.deal(bonus, DType::Physical, self.src_e_crit, false, false, 1.0);
        }
        self.p_check(e);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd) * self.q_refund_mult;
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.p_check(e);
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.p_r_ready_at = t + self.r_reveal_delay_s;
        self.s.p_r_vitals_left = self.r_vitals_count;
        self.s.p_normal_ready = t + self.r_mark_duration_s + self.p_vital_delay;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_shock_at != INF {
                out[n] = (self.s.w_shock_at, Kind::Ev(EV_W_SHOCK));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_charges = 2;
                self.s.e_buff_until = t + self.e_buff_s;
                self.s.e_ready = INF;
                e.prime_spellblade();
                // Bladework resets Fiora's basic attack timer
                let b = self.bonus_as(t);
                e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_cast_at = t;
                self.s.w_shock_at = t + self.w_shock_delay_s;
                e.st.next_attack = pymax(e.st.next_attack, t + self.w_parry_s);
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_SHOCK) => {
                self.s.w_shock_at = INF;
                self.s.w_ready = self.s.w_cast_at + self.w_parry_s + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.p_check(e);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
