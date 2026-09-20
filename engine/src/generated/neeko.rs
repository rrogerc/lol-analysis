//! Neeko. Blooming Burst blooms twice more automatically against a
//! champion-classed dummy, Tangle-Barbs and Pop Blossom are plain cast
//! abilities on their own cooldowns, and Shapesplitter's passive stacks a
//! bonus magic on-hit every second attack; Pop Blossom's wind-up and cast
//! time fully commit Neeko before it lands.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Pop Blossom's landing damage, after the wind-up + cast time.
const EV_R_LAND: u8 = 0;
/// Tangle-Barbs, cast on cooldown.
const EV_E_CAST: u8 = 1;
/// Blooming Burst's two automatic re-blooms.
const EV_Q_BLOOM1: u8 = 2;
const EV_Q_BLOOM2: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_init_dmg: f64,
    q_bloom_dmg: f64,
    q_repeat_delay: f64,
    q_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    w_dmg: f64,
    w_stack_cap: i64,
    r_dmg: f64,
    r_windup: f64,
    r_cast: f64,
    src_q_bloom: SourceId,
    src_w: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    e_ready: f64,
    w_stacks: i64,
    /// Set once, at Pop Blossom's cast: when its wind-up + cast time ends.
    r_busy_until: f64,
    /// Pending Pop Blossom landing time (INF once it has landed).
    r_land_at: f64,
    q_bloom1_at: f64,
    q_bloom2_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            e_ready: 0.0,
            w_stacks: 0,
            r_busy_until: 0.0,
            r_land_at: INF,
            q_bloom1_at: INF,
            q_bloom2_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_init_dmg: kit.hit("gen.Q.initialDamage", ranks.q, sheet)?,
            q_bloom_dmg: kit.hit("gen.Q.bloomDamage", ranks.q, sheet)?,
            q_repeat_delay: kit.num("gen.Q.repeatDelayS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_stack_cap: kit.num("gen.W.stackCap")? as i64,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_windup: kit.num("gen.R.windupS")?,
            r_cast: kit.num("gen.R.castTimeS")?,
            src_q_bloom: intern("Q bloom"),
            src_w: intern("W onhit"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        true
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self, _t: f64) -> f64 {
        0.0
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Shapesplitter: every attack's on-hit adds a refreshing stack; the
        // attack that reaches the cap consumes them for a bonus strike.
        if self.ranks.w == 0 {
            return;
        }
        self.s.w_stacks += 1;
        if self.s.w_stacks >= self.w_stack_cap {
            self.s.w_stacks = 0;
            e.deal(self.w_dmg, DType::Magic, self.src_w, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        // held until Pop Blossom's wind-up + cast time has elapsed
        pymax(pymax(e.st.q_ready, self.s.r_busy_until), e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_init_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.q_bloom1_at = t + self.q_repeat_delay;
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: wind-up + cast time fully commits Neeko, so
        // attacks, Q and E are held until it lands
        let t = e.st.t;
        self.s.r_busy_until = t + self.r_windup + self.r_cast;
        self.s.r_land_at = self.s.r_busy_until;
        e.st.next_attack = pymax(e.st.next_attack, self.s.r_busy_until);
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_land_at != INF {
            out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (
                pymax(pymax(self.s.e_ready, self.s.r_busy_until), e.st.t),
                Kind::Ev(EV_E_CAST),
            );
            n += 1;
        }
        if self.s.q_bloom1_at != INF {
            out[n] = (self.s.q_bloom1_at, Kind::Ev(EV_Q_BLOOM1));
            n += 1;
        }
        if self.s.q_bloom2_at != INF {
            out[n] = (self.s.q_bloom2_at, Kind::Ev(EV_Q_BLOOM2));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_Q_BLOOM1) => {
                self.s.q_bloom1_at = INF;
                e.deal(self.q_bloom_dmg, DType::Magic, self.src_q_bloom, false, true, 1.0);
                self.s.q_bloom2_at = t + self.q_repeat_delay;
            }
            Kind::Ev(EV_Q_BLOOM2) => {
                self.s.q_bloom2_at = INF;
                e.deal(self.q_bloom_dmg, DType::Magic, self.src_q_bloom, false, true, 1.0);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
