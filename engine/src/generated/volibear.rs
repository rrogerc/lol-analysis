//! Volibear. A melee auto-attacker: The Relentless Storm stacks attack speed
//! per hit and grants a single Lightning Claws on-hit proc on the hit that
//! reaches 5 stacks, Thundering Smash arms the next basic attack (bonus
//! damage plus a free extra attack), Frenzied Maul is cast on cooldown and
//! reads/re-applies its own Wounded mark, and Sky Splitter and Stormbringer
//! are one-shot casts whose delayed impacts are scheduled as events.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Frenzied Maul: the cast completing, then its delayed strike landing.
const EV_W_CAST: u8 = 0;
const EV_W_HIT: u8 = 1;
/// Sky Splitter: the cast, then its bolt landing 2s later.
const EV_E_CAST: u8 = 2;
const EV_E_HIT: u8 = 3;
/// Stormbringer's impact, 1s after the opening cast.
const EV_R_HIT: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// The Relentless Storm: percent bonus AS per stack, the cap, and its
    /// refresh duration.
    p_as_pct_per_stack: f64,
    p_max: i64,
    p_stack_duration: f64,
    /// Lightning Claws' bonus magic on-hit damage.
    p_lc_dmg: f64,
    q_dmg: f64,
    q_cd: f64,
    w_total_dmg: f64,
    w_bite_dmg: f64,
    w_cd: f64,
    w_cast_s: f64,
    w_mark_dur: f64,
    e_dmg_base: f64,
    e_target_hp_ratio: f64,
    e_cd: f64,
    e_delay_s: f64,
    r_dmg: f64,
    r_delay_s: f64,
    src_w: SourceId,
    src_e: SourceId,
    src_p_lc: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    p_last_hit_t: f64,
    /// Thundering Smash armed on Volibear, waiting for the next attack.
    q_armed: bool,
    /// The attack that just consumed Q gets a windup-only reset next.
    q_reset_pending: bool,
    w_ready: f64,
    /// When the pending Frenzied Maul strike lands (INF: none pending).
    w_hit_at: f64,
    w_wounded_until: f64,
    e_ready: f64,
    /// When the pending Sky Splitter bolt lands (INF: none pending).
    e_hit_at: f64,
    /// When Stormbringer's impact lands (INF: none pending).
    r_hit_at: f64,
}

impl GenDriver {
    /// Registers a damaging hit (basic attack or ability) against The
    /// Relentless Storm: refreshes/advances the stack count, and the hit
    /// that reaches the 5th stack is empowered by Lightning Claws.
    fn p_register_hit(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let cur = if t <= self.s.p_last_hit_t + self.p_stack_duration {
            self.s.p_stacks
        } else {
            0
        };
        if cur == self.p_max - 1 {
            e.deal(self.p_lc_dmg, DType::Magic, self.src_p_lc, false, false, 1.0);
            self.s.p_stacks = self.p_max;
        } else {
            self.s.p_stacks = imin(cur + 1, self.p_max);
        }
        self.s.p_last_hit_t = t;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let w_total_dmg = kit.hit("gen.W.damage", ranks.w, sheet)?;
        let w2_mult_base = kit.num("gen.W.w2MultiplierBase")?;
        let w2_bonusad_coef = kit.num("gen.W.w2BonusAdCoefPerPoint")?;
        let w_bite_dmg = w_total_dmg * (w2_mult_base + w2_bonusad_coef * sheet.ad_bonus);

        let state = State {
            p_stacks: 0,
            p_last_hit_t: -100.0,
            q_armed: false,
            q_reset_pending: false,
            w_ready: 0.0,
            w_hit_at: INF,
            w_wounded_until: -100.0,
            e_ready: 0.0,
            e_hit_at: INF,
            r_hit_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("volibear kit needs attack.windupFraction")?,
            p_as_pct_per_stack: (kit.num("gen.P.asBase")? + kit.num("gen.P.apCoefPerAp")? * sheet.ap) * 100.0,
            p_max: kit.num("gen.P.maxStacks")? as i64,
            p_stack_duration: kit.num("gen.P.stackDurationS")?,
            p_lc_dmg: kit.at_level("gen.P.lightningClaws.base", level)?
                + kit.num("gen.P.lightningClaws.apRatio")? * sheet.ap,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_total_dmg,
            w_bite_dmg,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            w_mark_dur: kit.num("gen.W.markDurationS")?,
            e_dmg_base: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_target_hp_ratio: kit.at_rank("gen.E.targetMaxHpRatio", ranks.e)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_delay_s: kit.num("gen.E.impactDelayS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_delay_s: kit.num("gen.R.impactDelayS")?,
            src_w: intern("W"),
            src_e: intern("E"),
            src_p_lc: intern("P Lightning Claws"),
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
        let stacks = if t <= self.s.p_last_hit_t + self.p_stack_duration {
            self.s.p_stacks
        } else {
            0
        };
        stacks as f64 * self.p_as_pct_per_stack
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // any damaging hit builds The Relentless Storm, including the
        // Thundering Smash bonus dealt on this same attack below
        self.p_register_hit(e);
        if self.s.q_armed {
            self.s.q_armed = false;
            e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
            self.s.q_reset_pending = true;
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.s.q_reset_pending {
            // Thundering Smash's attack does not go on cooldown: the next
            // attack fires after just a windup, a free extra attack
            self.s.q_reset_pending = false;
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.q_armed {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // no damage on cast: Thundering Smash arms the next basic attack
        self.s.q_armed = true;
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        e.prime_spellblade();
        self.s.r_hit_at = e.st.t + self.r_delay_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_hit_at != INF {
                out[n] = (self.s.w_hit_at, Kind::Ev(EV_W_HIT));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_hit_at != INF {
                out[n] = (self.s.e_hit_at, Kind::Ev(EV_E_HIT));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.s.r_hit_at != INF {
            out[n] = (self.s.r_hit_at, Kind::Ev(EV_R_HIT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                e.prime_spellblade();
                e.lockout();
                self.s.w_hit_at = t + self.w_cast_s;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
            }
            Kind::Ev(EV_W_HIT) => {
                self.s.w_hit_at = INF;
                let wounded = t < self.s.w_wounded_until;
                let dmg = if wounded { self.w_bite_dmg } else { self.w_total_dmg };
                e.deal(dmg, DType::Physical, self.src_w, false, true, 1.0);
                self.s.w_wounded_until = t + self.w_mark_dur;
                e.ability_cast_proc();
                e.eclipse_hit();
                self.p_register_hit(e);
            }
            Kind::Ev(EV_E_CAST) => {
                e.prime_spellblade();
                self.s.e_hit_at = t + self.e_delay_s;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
            Kind::Ev(EV_E_HIT) => {
                self.s.e_hit_at = INF;
                let dmg = self.e_dmg_base + self.e_target_hp_ratio * e.target_hp;
                e.deal(dmg, DType::Magic, self.src_e, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.p_register_hit(e);
            }
            Kind::Ev(EV_R_HIT) => {
                self.s.r_hit_at = INF;
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.p_register_hit(e);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
