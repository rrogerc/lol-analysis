//! Braum. Opens with Glacial Fissure, then attacks continuously and weaves
//! Winter's Bite on cooldown; both feed Concussive Blows, whose 4th stack
//! bursts for magic damage and opens an immunity window where Braum's
//! attacks deal bonus on-hit magic damage.

use crate::fight::{Driver, Engine, Events, Kind};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Glacial Fissure's damage lands when its cast ends.
const EV_R_SWING: u8 = 0;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    r_dmg: f64,
    r_cast_s: f64,
    p_stun_dmg: f64,
    p_onhit_bonus: f64,
    p_immune_dur: f64,
    p_stack_dur: f64,
    p_stack_cap: i64,
    src_p_burst: SourceId,
    src_p_onhit: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Concussive Blows: current stacks (0..cap), when they expire, and
    /// when the post-stun immunity window (with its on-hit bonus) ends.
    p_stacks: i64,
    p_stack_until: f64,
    p_immune_until: f64,
    /// Glacial Fissure's pending swing (INF: none pending).
    r_swing_at: f64,
}

impl GenDriver {
    /// Concussive Blows: called for every attack landing and every Q hit.
    /// During the immunity window an attack instead deals the bonus on-hit
    /// magic damage and applies no stack; otherwise a stack is applied
    /// (resetting first if the previous ones expired), and the 4th consumes
    /// them all for the burst and opens the immunity window.
    fn passive_proc(&mut self, e: &mut Engine, is_attack: bool) {
        let t = e.st.t;
        if t < self.s.p_immune_until {
            if is_attack {
                e.deal(self.p_onhit_bonus, DType::Magic, self.src_p_onhit, false, false, 1.0);
            }
            return;
        }
        if t > self.s.p_stack_until {
            self.s.p_stacks = 0;
        }
        self.s.p_stacks += 1;
        self.s.p_stack_until = t + self.p_stack_dur;
        if self.s.p_stacks >= self.p_stack_cap {
            self.s.p_stacks = 0;
            self.s.p_immune_until = t + self.p_immune_dur;
            e.deal(self.p_stun_dmg, DType::Magic, self.src_p_burst, false, false, 1.0);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            p_stack_until: -INF,
            p_immune_until: -INF,
            r_swing_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            p_stun_dmg: kit.at_level("gen.P.stunDamageByLevel", level)?,
            p_onhit_bonus: kit.at_level("gen.P.onhitBonusByLevel", level)?,
            p_immune_dur: kit.at_level("gen.P.immunityDurationByLevel", level)?,
            p_stack_dur: kit.num("gen.P.stackDurationS")?,
            p_stack_cap: kit.num("gen.P.stackCap")? as i64,
            src_p_burst: intern("P burst"),
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

    fn attack_riders(&mut self, e: &mut Engine) {
        // Braum's basic attacks apply (or, during immunity, ride) Concussive Blows
        self.passive_proc(e, true);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        // Winter's Bite also applies a Concussive Blows stack
        self.passive_proc(e, false);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the opener: the engine has primed Spellblade and held the first
        // attack past the standard 0.25s lockout; hold it the rest of the
        // way to the actual 0.5s cast, when the fissure's damage lands
        let t = e.st.t;
        self.s.r_swing_at = t + self.r_cast_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.r_swing_at);
    }

    fn events(&self, _e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_swing_at != INF {
            out[n] = (self.s.r_swing_at, Kind::Ev(EV_R_SWING));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        match kind {
            Kind::Ev(EV_R_SWING) => {
                self.s.r_swing_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
