//! Darius. Auto-attacker whose passive Hemorrhage (a stacking bleed) is fed
//! by every attack and ability, Decimate is a delayed-swing AoE cast on
//! cooldown, Crippling Strike is woven in for its attack reset like an
//! Empower, and Noxian Guillotine's single cast is held until Hemorrhage
//! hits its 5-stack cap for the full true-damage multiplier.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Decimate's swing lands (after its 0.75s wind-up).
const EV_Q_SWING: u8 = 0;
/// Hemorrhage's bleed ticks.
const EV_BLEED_TICK: u8 = 1;
/// Noxian Guillotine's (delayed) real cast.
const EV_R_CAST: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    p_bleed_base: f64,
    p_bleed_ad_coef: f64,
    p_tick_interval: f64,
    p_bleed_duration: f64,
    p_stack_cap: i64,
    p_might_ad: f64,
    p_might_duration: f64,
    ticks_per_app: f64,
    q_blade_base: f64,
    q_blade_ad_coef: f64,
    q_windup: f64,
    q_cd: f64,
    w_bonus_ad_ratio: f64,
    w_cd: f64,
    r_base: f64,
    r_bonusad_coef: f64,
    r_per_stack_frac: f64,
    r_cd: f64,
    src_p_bleed: SourceId,
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// When Decimate's damage lands (INF: none pending).
    q_swing_at: f64,
    w_ready: f64,
    w_armed: bool,
    r_ready: f64,
    hemo_stacks: i64,
    hemo_expire: f64,
    /// When the bleed's next tick lands (INF: no bleed active).
    hemo_next_tick: f64,
    /// Noxian Might's bonus AD buff expiry.
    might_until: f64,
    /// Whether it has already fired for the current uninterrupted run at cap.
    might_triggered: bool,
}

impl GenDriver {
    fn bonus_ad_now(&self, e: &Engine, t: f64) -> f64 {
        let mut b = e.p.sheet.ad_bonus;
        if t < self.s.might_until {
            b += self.p_might_ad;
        }
        b
    }

    fn total_ad_now(&self, e: &Engine, t: f64) -> f64 {
        e.p.sheet.ad_base + self.bonus_ad_now(e, t)
    }

    /// Applies/refreshes a Hemorrhage stack, and edge-triggers Noxian Might.
    fn apply_hemo(&mut self, t: f64) {
        if self.s.hemo_stacks == 0 {
            self.s.hemo_next_tick = t + self.p_tick_interval;
        }
        self.s.hemo_stacks = imin(self.s.hemo_stacks + 1, self.p_stack_cap);
        self.s.hemo_expire = t + self.p_bleed_duration;
        if self.s.hemo_stacks >= self.p_stack_cap && !self.s.might_triggered {
            self.s.might_triggered = true;
            self.s.might_until = t + self.p_might_duration;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_bleed_duration = kit.num("gen.P.bleedDurationS")?;
        let p_tick_interval = kit.num("gen.P.tickIntervalS")?;
        let state = State {
            q_swing_at: INF,
            w_ready: 0.0,
            w_armed: false,
            r_ready: 0.0,
            hemo_stacks: 0,
            hemo_expire: 0.0,
            hemo_next_tick: INF,
            might_until: 0.0,
            might_triggered: false,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("darius kit needs attack.windupFraction")?,
            p_bleed_base: kit.at_level("gen.P.bleedBaseByLevel", level)?,
            p_bleed_ad_coef: kit.num("gen.P.bleedBonusAdRatio")?,
            p_tick_interval,
            p_bleed_duration,
            p_stack_cap: kit.num("gen.P.maxStacks")? as i64,
            p_might_ad: kit.at_level("gen.P.noxianMightAdByLevel", level)?,
            p_might_duration: kit.num("gen.P.noxianMightDurationS")?,
            ticks_per_app: p_bleed_duration / p_tick_interval,
            q_blade_base: kit.at_rank("gen.Q.bladeBaseByRank", ranks.q)?,
            q_blade_ad_coef: kit.at_rank("gen.Q.bladeAdRatioByRank", ranks.q)?,
            q_windup: kit.num("gen.Q.windupS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_bonus_ad_ratio: kit.at_rank("gen.W.bonusAdRatioByRank", ranks.w)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            r_base: kit.at_rank("gen.R.baseByRank", ranks.r)?,
            r_bonusad_coef: kit.num("gen.R.bonusAdRatio")?,
            r_per_stack_frac: kit.num("gen.R.perStackFrac")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            src_p_bleed: intern("P bleed"),
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

    fn attack_damage(&self, e: &Engine) -> f64 {
        e.p.ad + if e.st.t < self.s.might_until { self.p_might_ad } else { 0.0 }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.apply_hemo(t);
        if self.s.w_armed {
            self.s.w_armed = false;
            self.s.w_ready = t + e.basic_cd(self.w_cd);
            let bonus_ad = self.bonus_ad_now(e, t);
            let dmg = self.w_bonus_ad_ratio * bonus_ad;
            e.deal(dmg, DType::Physical, SRC_W, true, true, 1.0);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.ranks.w > 0 && !self.s.w_armed && t >= self.s.w_ready {
            self.s.w_armed = true;
            e.prime_spellblade();
            e.ability_cast_proc();
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
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
        let t = e.st.t;
        e.st.q_ready = INF;
        self.s.q_swing_at = t + self.q_windup;
        e.st.next_attack = pymax(e.st.next_attack, t + self.q_windup);
        e.prime_spellblade();
    }

    fn cast_r(&mut self, _e: &mut Engine) {
        // deliberately does nothing: the real cast is delayed until
        // Hemorrhage hits its stack cap, handled entirely via events/on_event
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_swing_at != INF {
            out[n] = (self.s.q_swing_at, Kind::Ev(EV_Q_SWING));
            n += 1;
        }
        if self.s.hemo_next_tick != INF {
            out[n] = (self.s.hemo_next_tick, Kind::Ev(EV_BLEED_TICK));
            n += 1;
        }
        if self.ranks.r > 0 && self.s.r_ready <= e.st.t && self.s.hemo_stacks >= self.p_stack_cap {
            out[n] = (e.st.t, Kind::Ev(EV_R_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_SWING) => {
                self.s.q_swing_at = INF;
                let total_ad = self.total_ad_now(e, t);
                let dmg = self.q_blade_base + self.q_blade_ad_coef * total_ad;
                e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.apply_hemo(t);
                e.st.q_ready = t + e.basic_cd(self.q_cd);
            }
            Kind::Ev(EV_BLEED_TICK) => {
                if self.s.hemo_stacks > 0 && t <= self.s.hemo_expire {
                    let bonus_ad = self.bonus_ad_now(e, t);
                    let total_per_stack = self.p_bleed_base + self.p_bleed_ad_coef * bonus_ad;
                    let per_tick = total_per_stack / self.ticks_per_app;
                    let dmg = per_tick * self.s.hemo_stacks as f64;
                    e.deal(dmg, DType::Physical, self.src_p_bleed, false, false, 1.0);
                    self.s.hemo_next_tick = t + self.p_tick_interval;
                } else {
                    self.s.hemo_stacks = 0;
                    self.s.hemo_next_tick = INF;
                    self.s.might_triggered = false;
                }
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                let bonus_ad = self.bonus_ad_now(e, t);
                let mult = 1.0 + self.r_per_stack_frac * self.s.hemo_stacks as f64;
                let dmg = (self.r_base + self.r_bonusad_coef * bonus_ad) * mult;
                e.deal(dmg, DType::True, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                self.apply_hemo(t);
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
