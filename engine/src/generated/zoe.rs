//! Zoe. An ability-driven attacker: Sleepy Trouble Bubble and Paddle Star!
//! are cast on cooldown, Spell Thief is cast continuously for its orbiting
//! bolts, More Sparkles! rides whichever lands first (an attack or a bolt),
//! and Zoe auto-attacks between casts. Portal Jump is never cast (see notes).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Sleepy Trouble Bubble is cast; its drowsy expires into sleep; a Spell
/// Thief bolt fires (and a new set is cast in if the previous one is spent).
const EV_E_CAST: u8 = 0;
const EV_E_SLEEP_START: u8 = 1;
const EV_BOLT: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// More Sparkles!: bonus magic damage on the next attack or bolt, and
    /// its arming window.
    p_dmg: f64,
    p_arm_dur: f64,
    /// Paddle Star!'s minimum (0% distance bonus) magic damage.
    q_dmg: f64,
    q_cd: f64,
    /// Spell Thief's per-bolt magic damage and the assumed bolt cadence.
    w_bolt_dmg: f64,
    w_bolt_interval: f64,
    /// Sleepy Trouble Bubble's damage, cooldown, on-champion-hit refund
    /// fraction, and its drowsy/sleep/wake-linger timings.
    e_dmg: f64,
    e_cd: f64,
    e_refund: f64,
    e_drowsy: f64,
    e_sleep: f64,
    e_linger: f64,
    src_p: SourceId,
    src_w_bolt: SourceId,
    src_e_wake: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_armed: bool,
    p_until: f64,
    e_ready: f64,
    /// When the pending sleep begins (INF: none pending).
    e_sleep_start_at: f64,
    /// The wake-up bonus true damage is armed until this time.
    e_wake_pending: bool,
    e_wake_until: f64,
    /// Which bolt (0..2) of the current Spell Thief set fires next; 0 also
    /// means "cast a fresh set now".
    bolt_idx: i64,
    bolt_next_at: f64,
}

impl GenDriver {
    fn try_consume_p(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.p_armed && t <= self.s.p_until {
            e.deal(self.p_dmg, DType::Magic, self.src_p, false, false, 1.0);
            self.s.p_armed = false;
        }
    }

    fn try_wake(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.e_wake_pending && t <= self.s.e_wake_until {
            e.deal(self.e_dmg, DType::True, self.src_e_wake, false, false, 1.0);
            self.s.e_wake_pending = false;
        }
    }

    fn arm_p(&mut self, t: f64) {
        self.s.p_armed = true;
        self.s.p_until = t + self.p_arm_dur;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_armed: false,
            p_until: -INF,
            e_ready: 0.0,
            e_sleep_start_at: INF,
            e_wake_pending: false,
            e_wake_until: -INF,
            bolt_idx: 0,
            bolt_next_at: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_dmg: kit.at_level("gen.P.damageBase", level)? + kit.num("gen.P.apRatio")? * sheet.ap,
            p_arm_dur: kit.num("gen.P.armDurationS")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)? + kit.at_level("gen.Q.perLevelDamage", level)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_bolt_dmg: kit.hit("gen.W.boltDamage", ranks.w, sheet)?,
            w_bolt_interval: kit.num("gen.W.boltIntervalS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_refund: kit.at_rank("gen.E.refundFrac", ranks.e)?,
            e_drowsy: kit.num("gen.E.drowsyDurationS")?,
            e_sleep: kit.num("gen.E.sleepDurationS")?,
            e_linger: kit.num("gen.E.wakeLingerS")?,
            src_p: intern("P"),
            src_w_bolt: intern("W bolt"),
            src_e_wake: intern("E wake"),
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

    fn after_attack(&mut self, e: &mut Engine) {
        self.try_consume_p(e);
        self.try_wake(e);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.arm_p(t);
        self.try_wake(e);
        e.lockout();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.e_sleep_start_at != INF {
            out[n] = (self.s.e_sleep_start_at, Kind::Ev(EV_E_SLEEP_START));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (self.s.bolt_next_at, Kind::Ev(EV_BOLT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_SLEEP_START) => {
                self.s.e_sleep_start_at = INF;
                e.st.shred_until = t + self.e_sleep;
                self.s.e_wake_pending = true;
                self.s.e_wake_until = t + self.e_sleep + self.e_linger;
            }
            Kind::Ev(EV_E_CAST) => {
                e.lockout();
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.arm_p(t);
                self.s.e_sleep_start_at = t + self.e_drowsy;
                let cd = e.basic_cd(self.e_cd) * (1.0 - self.e_refund);
                self.s.e_ready = t + cd;
            }
            Kind::Ev(EV_BOLT) => {
                if self.s.bolt_idx == 0 {
                    e.prime_spellblade();
                    self.arm_p(t);
                }
                e.deal(self.w_bolt_dmg, DType::Magic, self.src_w_bolt, false, true, 1.0);
                self.try_consume_p(e);
                self.try_wake(e);
                self.s.bolt_idx = (self.s.bolt_idx + 1) % 2 + self.s.bolt_idx / 2 * 0; // placeholder never used
                self.s.bolt_idx = (self.s.bolt_idx + 1) % 3;
                self.s.bolt_next_at = t + self.w_bolt_interval;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
