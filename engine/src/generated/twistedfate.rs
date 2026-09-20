//! Twisted Fate. Destiny opens the fight only to register as an ability
//! activation (it deals no damage and is never recast). Wild Cards is cast
//! on cooldown for magic damage. Pick a Card is cast on cooldown, then
//! recast on the Blue Card after an assumed average wait: the recast resets
//! the attack timer and arms the next attack with a 0.25s cast that deals
//! Blue Card's magic damage instead of normal AD damage. Stacked Deck grants
//! flat bonus attack speed and stacks a bonus magic on-hit every 3rd landed
//! attack while Twisted Fate auto-attacks throughout.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Pick a Card's initial cast (arms the recast); its recast (arms the attack).
const EV_W_CAST: u8 = 0;
const EV_W_RECAST: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_blue_dmg: f64,
    w_crit_mult: f64,
    w_wait_s: f64,
    w_cast_s: f64,
    w_cd: f64,
    e_dmg: f64,
    e_as_pct: f64,
    e_need: i64,
    crit_chance_frac: f64,
    src_w: SourceId,
    src_e: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    /// When the pending Blue Card recast fires (INF: none pending).
    w_pending_recast_at: f64,
    /// The next basic attack is armed to become the empowered Blue Card hit.
    w_armed: bool,
    /// The attack currently in flight is the empowered one.
    w_pending_attack: bool,
    e_stacks: i64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            w_ready: 0.0,
            w_pending_recast_at: INF,
            w_armed: false,
            w_pending_attack: false,
            e_stacks: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_blue_dmg: kit.hit("gen.W.blueDamage", ranks.w, sheet)?,
            w_crit_mult: kit.num("gen.W.blueCritMult")?,
            w_wait_s: kit.num("gen.W.blueWaitS")?,
            w_cast_s: kit.num("gen.W.empoweredCastTimeS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_as_pct: kit.at_rank("gen.E.attackSpeedBonus", ranks.e)? * 100.0,
            e_need: kit.num("gen.E.stacksNeeded")? as i64,
            crit_chance_frac: sheet.crit_chance / 100.0,
            src_w: intern("W blue"),
            src_e: intern("E onhit"),
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
        self.e_as_pct
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        if self.s.w_pending_attack {
            0.0
        } else {
            e.p.ad
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {
        if self.s.w_armed {
            self.s.w_armed = false;
            self.s.w_pending_attack = true;
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.ranks.e == 0 {
            return;
        }
        if self.s.e_stacks >= self.e_need {
            self.s.e_stacks = 0;
            e.deal(self.e_dmg, DType::Magic, self.src_e, false, false, 1.0);
        } else {
            self.s.e_stacks += 1;
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.w_pending_attack {
            self.s.w_pending_attack = false;
            let amt = self.w_blue_dmg * (1.0 + self.w_crit_mult * self.crit_chance_frac);
            e.deal(amt, DType::Magic, self.src_w, false, false, 1.0);
        }
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
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // Destiny deals no damage; it only registers as an ability cast.
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_pending_recast_at != INF {
                out[n] = (self.s.w_pending_recast_at, Kind::Ev(EV_W_RECAST));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                // instant cast: only starts the cycle, no damage, counts as
                // an activation; the cooldown is parked until the recast
                self.s.w_pending_recast_at = t + self.w_wait_s;
                self.s.w_ready = INF;
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_RECAST) => {
                // the recast does not count as an activation; it resets the
                // attack timer and arms the next attack, and its cooldown
                // starts here (post-effect)
                self.s.w_pending_recast_at = INF;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_armed = true;
                e.st.next_attack = t + self.w_cast_s;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
