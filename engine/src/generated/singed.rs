//! Singed. Poison Trail is toggled on before the fight and ticks continuously
//! on the stationary, always-in-range dummy; Insanity Potion is cast at the
//! opening for its AP buff (which lasts the whole fight and boosts both Q and
//! E); Fling goes out on cooldown for its burst; Mega Adhesive is never cast
//! since it deals no damage.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Fling is cast on cooldown.
const EV_E: u8 = 0;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Poison Trail: per-tick base damage (already includes the caster's
    /// base AP via the sheet), the raw AP ratio (to apply Insanity Potion's
    /// bonus AP separately), and the tick interval.
    q_tick_base: f64,
    q_tick_ap_ratio: f64,
    q_tick_interval: f64,
    /// Fling: base damage (with base AP already folded in), the raw AP
    /// ratio, the target-max-health fraction, and its cooldown.
    e_dmg_base: f64,
    e_ap_ratio: f64,
    e_target_hp_ratio: f64,
    e_cd: f64,
    /// Insanity Potion: bonus AP while active, and its duration.
    r_bonus_ap: f64,
    r_duration: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Whether the one-time extra poison instance (on the very first
    /// application of the fight) has already been dealt.
    q_first_tick_done: bool,
    e_ready: f64,
    /// When Insanity Potion's buff ends (never, until cast).
    r_until: f64,
}

impl GenDriver {
    /// Insanity Potion's bonus AP, if its buff is currently active.
    fn bonus_ap(&self, t: f64) -> f64 {
        if t < self.s.r_until {
            self.r_bonus_ap
        } else {
            0.0
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            q_first_tick_done: false,
            e_ready: 0.0,
            r_until: -INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("singed kit needs attack.windupFraction")?,
            q_tick_base: kit.hit("gen.Q.tickDamage", ranks.q, sheet)?,
            q_tick_ap_ratio: kit.num("gen.Q.tickDamage.apRatio")?,
            q_tick_interval: kit.num("gen.Q.tickIntervalS")?,
            e_dmg_base: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_ap_ratio: kit.num("gen.E.damage.apRatio")?,
            e_target_hp_ratio: kit.at_rank("gen.E.targetMaxHpRatio", ranks.e)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_bonus_ap: kit.at_rank("gen.R.bonusAP", ranks.r)?,
            r_duration: kit.num("gen.R.durationS")?,
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

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + self.q_tick_interval;
        let bonus_ap = self.bonus_ap(t);
        let dmg = self.q_tick_base + self.q_tick_ap_ratio * bonus_ap;
        e.deal(dmg, DType::Magic, SRC_Q, false, true, 1.0);
        if !self.s.q_first_tick_done {
            self.s.q_first_tick_done = true;
            // the extra instance on first application, at the same moment
            e.deal(dmg, DType::Magic, SRC_Q, false, true, 1.0);
        }
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_until = e.st.t + self.r_duration;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                let bonus_ap = self.bonus_ap(t);
                let amt = self.e_dmg_base
                    + self.e_ap_ratio * bonus_ap
                    + self.e_target_hp_ratio * e.target_hp;
                e.deal(amt, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
