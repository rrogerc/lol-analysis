//! Aurora. Between Worlds opens the fight for its shockwave (no cast time,
//! but the dash locks out the next attack); Twofold Hex is cast on cooldown
//! and manually recast 0.1 s later every time; The Weirding is cast on
//! cooldown; basic attacks fill the gaps. Every damaging attack and ability
//! applies a stack of Spirit Abjuration, which caps at 3 and consumes itself
//! for bonus max-health magic damage. Casts go one at a time: Q and E each
//! keep Aurora busy for their cast time before any other cast or attack.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// The Weirding is cast on cooldown; Twofold Hex's recast follows the
/// outbound cast by its fixed 0.1 s delay.
const EV_E_CAST: u8 = 0;
const EV_Q_RECAST: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Spirit Abjuration: fraction of target max HP per stack-3 consume,
    /// its AP coefficient, the stack timer, and the stack cap.
    p_base_frac: f64,
    p_ap_coef: f64,
    p_stack_dur: f64,
    p_stack_cap: i64,
    q_dmg: f64,
    q_recast_dmg: f64,
    q_missing_coef: f64,
    q_recast_delay: f64,
    q_cast_s: f64,
    q_cd: f64,
    e_dmg: f64,
    e_cast_s: f64,
    e_cd: f64,
    r_dmg: f64,
    src_p: SourceId,
    src_q_recast: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    p_stacks: i64,
    /// When the current stacks lapse if nothing refreshes them.
    p_expire: f64,
    e_ready: f64,
    /// When the pending Twofold Hex recast fires (INF: none pending).
    q_recast_at: f64,
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

    /// A damaging attack or ability applies a Spirit Abjuration stack; the
    /// 3rd consumes all three for the bonus max-health magic damage.
    fn add_p_stack(&mut self, e: &mut Engine, t: f64) {
        if t > self.s.p_expire {
            self.s.p_stacks = 0;
        }
        self.s.p_stacks += 1;
        if self.s.p_stacks >= self.p_stack_cap {
            self.s.p_stacks = 0;
            let ratio = self.p_base_frac + self.p_ap_coef * e.p.sheet.ap;
            let dmg = ratio * e.target_hp;
            e.deal(dmg, DType::Magic, self.src_p, false, false, 1.0);
        } else {
            self.s.p_expire = t + self.p_stack_dur;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            p_stacks: 0,
            p_expire: -INF,
            e_ready: 0.0,
            q_recast_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("aurora kit needs attack.windupFraction")?,
            p_base_frac: kit.num("gen.P.procBaseFrac")?,
            p_ap_coef: kit.num("gen.P.procApCoef")?,
            p_stack_dur: kit.num("gen.P.stackDurationS")?,
            p_stack_cap: kit.num("gen.P.stackCap")? as i64,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_recast_dmg: kit.hit("gen.Q.recastDamage", ranks.q, sheet)?,
            q_missing_coef: kit.num("gen.Q.missingHpCoef")?,
            q_recast_delay: kit.num("gen.Q.recastDelayS")?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            src_p: intern("P proc"),
            src_q_recast: intern("Q recast"),
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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.add_p_stack(e, t);
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
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        self.add_p_stack(e, t);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
        self.s.q_recast_at = t + self.q_recast_delay;
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
        self.add_p_stack(e, t);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.ult_hatefog();
        // no cast time, but the opening dash still holds the next attack back
        e.lockout();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.q_recast_at != INF {
            out[n] = (self.castable_at(e, self.s.q_recast_at), Kind::Ev(EV_Q_RECAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.add_p_stack(e, t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_Q_RECAST) => {
                self.s.q_recast_at = INF;
                let missing = pymax(e.target_hp - pymax(e.st.hp, 0.0), 0.0) / e.target_hp;
                let dmg = self.q_recast_dmg * (1.0 + self.q_missing_coef * missing);
                e.deal(dmg, DType::Magic, self.src_q_recast, false, true, 1.0);
                self.add_p_stack(e, t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                // the recast has no cast time of its own, but it did just
                // consume this instant: nothing else should overlap it either
                self.s.busy_until = pymax(self.s.busy_until, t);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
