//! Hecarim. Warpath's bonus AD tracks bonus movement speed continuously;
//! Rampage (Q) is wired into the engine and stacks itself; Spirit of Dread
//! (W) is a self-cast DoT/aura on a flat on-cast cooldown; Devastating
//! Charge (E) arms the next attack for bonus damage (assumed 0 distance
//! traveled) and resets the attack timer; Onslaught of Shadows (R) is a
//! single opening cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Spirit of Dread comes off cooldown; then its ticks; Devastating Charge
/// comes off cooldown.
const EV_W_CAST: u8 = 0;
const EV_W_TICK: u8 = 1;
const EV_E_CAST: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    base_move_speed: f64,
    p_coef: f64,
    q_base: f64,
    q_ad_ratio: f64,
    q_stack_pct: f64,
    q_max_stacks: i64,
    q_stack_duration: f64,
    q_cd_reduction: f64,
    q_cd: f64,
    w_tick_dmg: f64,
    w_num_ticks: i64,
    w_tick_interval: f64,
    w_cd: f64,
    e_min_base: f64,
    e_ad_ratio: f64,
    e_ms_min: f64,
    e_ms_max: f64,
    e_ms_time_to_max: f64,
    e_ms_duration: f64,
    e_cd: f64,
    r_dmg: f64,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_stacks: i64,
    q_stack_expire: f64,
    w_ready: f64,
    w_ticks_remaining: i64,
    w_next_tick: f64,
    e_ready: f64,
    e_armed: bool,
    /// When Devastating Charge's speed ramp started (INF: not active).
    e_active_since: f64,
    /// Set after the empowered attack lands, so schedule_attack resets it.
    e_just_fired: bool,
}

impl GenDriver {
    /// Hecarim's current bonus movement speed above his base, including
    /// Devastating Charge's temporary ramp while it is still armed.
    fn bonus_ms(&self, e: &Engine, t: f64) -> f64 {
        let base_total = e.p.sheet.move_speed;
        let total = if self.s.e_active_since < INF {
            let elapsed = t - self.s.e_active_since;
            if elapsed <= self.e_ms_duration {
                let frac = pymin(elapsed / self.e_ms_time_to_max, 1.0);
                let pct = self.e_ms_min + (self.e_ms_max - self.e_ms_min) * frac;
                base_total * (1.0 + pct)
            } else {
                base_total
            }
        } else {
            base_total
        };
        pymax(total - self.base_move_speed, 0.0)
    }

    /// Warpath's bonus AD right now.
    fn warpath_ad(&self, e: &Engine, t: f64) -> f64 {
        self.p_coef * self.bonus_ms(e, t)
    }

    /// Total bonus AD (items/level plus Warpath) right now.
    fn bonus_ad_total(&self, e: &Engine, t: f64) -> f64 {
        e.p.sheet.ad_bonus + self.warpath_ad(e, t)
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            q_stacks: 0,
            q_stack_expire: -INF,
            w_ready: 0.0,
            w_ticks_remaining: 0,
            w_next_tick: INF,
            e_ready: 0.0,
            e_armed: false,
            e_active_since: INF,
            e_just_fired: false,
        };
        let w_duration = kit.num("gen.W.durationS")?;
        let w_tick_interval = kit.num("gen.W.tickIntervalS")?;
        let w_num_ticks = (w_duration / w_tick_interval) as i64;
        let w_total = kit.hit("gen.W.damage", ranks.w, sheet)?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("hecarim kit needs attack.windupFraction")?,
            base_move_speed: kit.num("gen.P.baseMoveSpeed")?,
            p_coef: kit.at_level("gen.P.adPerBonusMsByLevel", level)?,
            q_base: kit.at_rank("gen.Q.damage.base", ranks.q)?,
            q_ad_ratio: kit.num("gen.Q.damage.bonusAdRatio")?,
            q_stack_pct: kit.num("gen.Q.stackPct")?,
            q_max_stacks: kit.num("gen.Q.maxStacks")? as i64,
            q_stack_duration: kit.num("gen.Q.stackDurationS")?,
            q_cd_reduction: kit.num("gen.Q.cdReductionPerStackS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_tick_dmg: w_total / (w_num_ticks as f64),
            w_num_ticks,
            w_tick_interval,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_min_base: kit.at_rank("gen.E.minDamage.base", ranks.e)?,
            e_ad_ratio: kit.num("gen.E.minDamage.bonusAdRatio")?,
            e_ms_min: kit.num("gen.E.msMin")?,
            e_ms_max: kit.num("gen.E.msMax")?,
            e_ms_time_to_max: kit.num("gen.E.msTimeToMaxS")?,
            e_ms_duration: kit.num("gen.E.msDurationS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
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

    fn attack_damage(&self, e: &Engine) -> f64 {
        e.p.ad + self.warpath_ad(e, e.st.t)
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.e_armed {
            // Devastating Charge's empowered attack: its bonus damage lands
            // and its ghosting/speed buff ends immediately (the target
            // stayed nearby)
            self.s.e_armed = false;
            self.s.e_active_since = INF;
            let bonus_ad = self.bonus_ad_total(e, e.st.t);
            let dmg = self.e_min_base + self.e_ad_ratio * bonus_ad;
            e.deal(dmg, DType::Physical, SRC_E, true, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            self.s.e_just_fired = true;
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.s.e_just_fired {
            // Devastating Charge resets Hecarim's basic attack timer
            self.s.e_just_fired = false;
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
        let bonus_ad = self.bonus_ad_total(e, t);
        let pre_stacks = self.s.q_stacks;
        let mult = 1.0 + (pre_stacks as f64) * self.q_stack_pct * (1.0 + bonus_ad / 100.0);
        let dmg = (self.q_base + self.q_ad_ratio * bonus_ad) * mult;
        e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        if t <= self.s.q_stack_expire {
            self.s.q_stacks = imin(self.s.q_stacks + 1, self.q_max_stacks);
        } else {
            self.s.q_stacks = 1;
        }
        self.s.q_stack_expire = t + self.q_stack_duration;
        let eff_base_cd = pymax(self.q_cd - (self.s.q_stacks as f64) * self.q_cd_reduction, 0.0);
        e.st.q_ready = t + e.basic_cd(eff_base_cd);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.ult_hatefog();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_ticks_remaining > 0 {
                out[n] = (self.s.w_next_tick, Kind::Ev(EV_W_TICK));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_ticks_remaining = self.w_num_ticks;
                self.s.w_next_tick = t + self.w_tick_interval;
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_TICK) => {
                e.deal(self.w_tick_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.w_ticks_remaining -= 1;
                self.s.w_next_tick = t + self.w_tick_interval;
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_active_since = t;
                self.s.e_armed = true;
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
