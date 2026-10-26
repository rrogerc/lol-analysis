//! Neeko. Blooming Burst blooms twice more automatically against a
//! champion-classed dummy, Tangle-Barbs and Pop Blossom are plain cast
//! abilities on their own cooldowns, and Shapesplitter's passive stacks a
//! bonus magic on-hit every second attack. Casts go one after another: one
//! `busy_until` timer holds off every other cast and any attack until a cast
//! time (Q, E: 0.25s each) or Pop Blossom's wind-up + cast time (1.85s) ends.

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
    q_cast_s: f64,
    q_cd: f64,
    e_dmg: f64,
    e_cast_s: f64,
    e_cd: f64,
    w_dmg: f64,
    w_stack_cap: i64,
    r_dmg: f64,
    r_busy_s: f64,
    src_q_bloom: SourceId,
    src_w: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    e_ready: f64,
    w_stacks: i64,
    /// Pending Pop Blossom landing time (INF once it has landed).
    r_land_at: f64,
    q_bloom1_at: f64,
    q_bloom2_at: f64,
}

impl GenDriver {
    /// The earliest a cast readied at `ready` can start: not before now, and
    /// not inside another cast.
    fn castable_at(&self, e: &Engine, ready: f64) -> f64 {
        pymax(pymax(ready, e.st.t), self.s.busy_until)
    }

    /// A cast with a cast time just started: no other cast and no attack
    /// until it ends (an attack already due later keeps its time).
    fn busy_for(&mut self, e: &mut Engine, cast_s: f64) {
        self.s.busy_until = e.st.t + cast_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            e_ready: 0.0,
            w_stacks: 0,
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
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_stack_cap: kit.num("gen.W.stackCap")? as i64,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_busy_s: kit.num("gen.R.windupS")? + kit.num("gen.R.castTimeS")?,
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
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_init_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.q_bloom1_at = t + self.q_repeat_delay;
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: wind-up + cast time fully commits Neeko, so
        // attacks, Q and E are held until it lands
        let t = e.st.t;
        self.s.r_land_at = t + self.r_busy_s;
        e.prime_spellblade();
        self.busy_for(e, self.r_busy_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_land_at != INF {
            out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
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
                self.busy_for(e, self.e_cast_s);
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
