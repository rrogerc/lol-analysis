//! Ahri. A spell-driven caster who still weaves in auto-attacks between
//! casts: Orb of Deception fires an outbound magic hit and a return true
//! hit on cast, Fox-Fire lands its three flames together after the
//! acquisition delay and its cooldown starts there, Charm hits once on
//! cast, and Spirit Rush opens the fight firing all three of its free
//! casts back to back (1 s apart). Casts go one at a time: Orb of Deception
//! and Charm each keep Ahri busy for their 0.25 s cast time; Fox-Fire and
//! Spirit Rush have no cast time but still wait for a cast in progress.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Fox-Fire is cast (instant); its flames land later.
const EV_W_CAST: u8 = 0;
/// Fox-Fire's three flames land together; the cooldown starts here.
const EV_W_FLAMES: u8 = 1;
/// Charm is cast on cooldown.
const EV_E_CAST: u8 = 2;
/// A pending Spirit Rush recast (the 1 s static interval).
const EV_R_RECAST: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_primary_dmg: f64,
    w_subsequent_dmg: f64,
    w_cd: f64,
    w_post_delay: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    r_dmg: f64,
    r_interval: f64,
    r_max_casts: i64,
    src_q_return: SourceId,
    src_w_sub: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    w_ready: f64,
    /// When Fox-Fire's flames land (INF: none pending).
    w_flame_at: f64,
    e_ready: f64,
    /// When the next Spirit Rush recast fires (INF: none pending).
    r_recast_at: f64,
    r_casts_done: i64,
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
            w_ready: 0.0,
            w_flame_at: INF,
            e_ready: 0.0,
            r_recast_at: INF,
            r_casts_done: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_primary_dmg: kit.hit("gen.W.primary", ranks.w, sheet)?,
            w_subsequent_dmg: kit.hit("gen.W.subsequent", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_post_delay: kit.num("gen.W.postEffectDelayS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_interval: kit.num("gen.R.recastIntervalS")?,
            r_max_casts: kit.num("gen.R.maxCasts")? as i64,
            src_q_return: intern("Q return"),
            src_w_sub: intern("W subsequent"),
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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        // outbound magic pass, then the return true pass; both resolve on
        // cast for a stationary target sitting at the return point
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.deal(self.q_dmg, DType::True, self.src_q_return, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_casts_done = 1;
        e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.ult_hatefog();
        if self.r_max_casts > 1 {
            self.s.r_recast_at = e.st.t + self.r_interval;
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_flame_at != INF {
                // a delayed effect, not a cast: it reports its own time
                out[n] = (self.s.w_flame_at, Kind::Ev(EV_W_FLAMES));
            } else {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 && self.s.r_recast_at != INF {
            out[n] = (self.castable_at(e, self.s.r_recast_at), Kind::Ev(EV_R_RECAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                // no cast time: the flames go out now and land later, when
                // the post-effect cooldown starts; it still cannot start
                // inside another cast, which castable_at already enforced
                self.s.w_flame_at = t + self.w_post_delay;
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_FLAMES) => {
                self.s.w_flame_at = INF;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_primary_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.deal(self.w_subsequent_dmg, DType::Magic, self.src_w_sub, false, true, 1.0);
                e.deal(self.w_subsequent_dmg, DType::Magic, self.src_w_sub, false, true, 1.0);
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_R_RECAST) => {
                self.s.r_casts_done += 1;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                if self.s.r_casts_done < self.r_max_casts {
                    self.s.r_recast_at = t + self.r_interval;
                } else {
                    self.s.r_recast_at = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
