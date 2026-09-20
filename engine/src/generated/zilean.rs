//! Zilean. A pure-caster whose only damage is Time Bomb: it lands on the
//! target and detonates after a 3 s fuse, or instantly if a second bomb
//! attaches to the same target first. Rewind is woven in on cooldown to
//! zero out Time Bomb's remaining cooldown, driving the real cadence.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// A bomb's own fuse detonates (if not popped early by a recast first).
const EV_BOMB: u8 = 0;
/// Rewind is cast, cutting Time Bomb's remaining cooldown.
const EV_W: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    fuse_s: f64,
    w_cd: f64,
    w_cdr: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    /// When the currently-attached bomb will self-detonate (INF: none attached).
    bomb_detonate_at: f64,
    /// Whether Time Bomb has been cast at least once (gates Rewind).
    q_has_been_cast: bool,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            w_ready: 0.0,
            bomb_detonate_at: INF,
            q_has_been_cast: false,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            fuse_s: kit.num("gen.Q.fuseDurationS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cdr: kit.num("gen.W.cdrS")?,
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
        e.p.ad
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {}

    fn attack_riders(&mut self, _e: &mut Engine) {}

    fn after_attack(&mut self, _e: &mut Engine) {}

    fn schedule_attack(&mut self, e: &mut Engine) {
        let b = self.bonus_as(e.st.t);
        e.st.next_attack = e.st.t + e.attack_period(b);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.bomb_detonate_at != INF {
            // a bomb is already attached to the dummy: the new bomb pops it
            // immediately instead of waiting out its own fuse
            e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
        }
        self.s.bomb_detonate_at = t + self.fuse_s;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.q_has_been_cast = true;
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, _e: &mut Engine) {
        // Chronoshift only heals/revives an ally; it never affects the dummy
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.bomb_detonate_at != INF {
            out[n] = (self.s.bomb_detonate_at, Kind::Ev(EV_BOMB));
            n += 1;
        }
        if self.ranks.w > 0 && self.s.q_has_been_cast {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_BOMB) => {
                // the fuse ran out with no follow-up bomb: it detonates on its own
                self.s.bomb_detonate_at = INF;
                e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_W) => {
                // Rewind: cut Time Bomb's remaining cooldown by the flat amount
                e.st.q_ready = pymax(t, e.st.q_ready - self.w_cdr);
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
