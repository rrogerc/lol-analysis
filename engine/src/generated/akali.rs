//! Akali. Casts go one at a time behind a single busy_until: Perfect
//! Execution (R) opens the fight with its initial dash damage and a 0.25s
//! cast time, then Five Point Strike (Q) is cast on cooldown, its own cast
//! time scaling with level, and Shuriken Flip (E) is cast on cooldown with
//! its recast queued to fire the instant its initial cast's busy window
//! ends. R's recast fires 2.5s after the opening cast (a static timer,
//! unaffected by haste) as an instant dash scaled by the target's missing
//! health. P's empowered attack never arms because continuous Q casting
//! keeps its ring refreshed (see kit notes); W deals no damage and is never
//! cast (see the kit's top-level `unused` entry).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Shuriken Flip's initial cast, and its recast once that cast's busy window ends.
const EV_E1: u8 = 0;
const EV_E2: u8 = 1;
/// Perfect Execution's recast, after its static 2.5s delay from the opening cast.
const EV_R2: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    e1_dmg: f64,
    e2_dmg: f64,
    e_cd: f64,
    e1_cast_s: f64,
    e2_cast_s: f64,
    r1_dmg: f64,
    r2min_dmg: f64,
    r_cast_s: f64,
    r_recast_delay_s: f64,
    r_missing_cap: f64,
    r_bonus_mult_cap: f64,
    src_e2: SourceId,
    src_r2: SourceId,
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
    /// When Shuriken Flip's recast fires (INF: none pending).
    e2_at: f64,
    /// When Perfect Execution's recast fires (INF: none pending).
    r2_at: f64,
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
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            e_ready: 0.0,
            e2_at: INF,
            r2_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.at_level("gen.Q.castTimeS", level)?,
            e1_dmg: kit.hit("gen.E.e1", ranks.e, sheet)?,
            e2_dmg: kit.hit("gen.E.e2", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e1_cast_s: kit.num("gen.E.castTimeS")?,
            e2_cast_s: kit.num("gen.E.recastCastTimeS")?,
            r1_dmg: kit.hit("gen.R.r1", ranks.r, sheet)?,
            r2min_dmg: kit.hit("gen.R.r2min", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_recast_delay_s: kit.num("gen.R.recastDelayS")?,
            r_missing_cap: kit.num("gen.R.missingHealthCap")?,
            r_bonus_mult_cap: kit.num("gen.R.bonusMultCap")?,
            src_e2: intern("E recast"),
            src_r2: intern("R recast"),
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
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the initial dash's damage lands as the cast starts
        e.deal(self.r1_dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        // the static recast window is counted from this activation, not from
        // when the cast time ends
        self.s.r2_at = e.st.t + self.r_recast_delay_s;
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.r > 0 && self.s.r2_at != INF {
            out[n] = (self.castable_at(e, self.s.r2_at), Kind::Ev(EV_R2));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e2_at != INF {
                out[n] = (self.castable_at(e, self.s.e2_at), Kind::Ev(EV_E2));
            } else {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E1));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E1) => {
                // damage lands as the cast starts; cooldown starts on-cast
                e.deal(self.e1_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                // the recast is queued to fire the instant this cast's busy window ends
                self.s.e2_at = t + self.e1_cast_s;
                self.busy_for(e, self.e1_cast_s);
            }
            Kind::Ev(EV_E2) => {
                self.s.e2_at = INF;
                e.deal(self.e2_dmg, DType::Magic, self.src_e2, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e2_cast_s);
                // the game orders a basic attack right after the recast dash
                // lands: an attack reset to the moment this cast ends
                e.st.next_attack = self.s.busy_until;
            }
            Kind::Ev(EV_R2) => {
                self.s.r2_at = INF;
                let missing = pymax(e.target_hp - pymax(e.st.hp, 0.0), 0.0);
                let frac = missing / e.target_hp;
                let ratio = pymin(frac / self.r_missing_cap, 1.0);
                let mult = 1.0 + self.r_bonus_mult_cap * ratio;
                let amt = self.r2min_dmg * mult;
                e.deal(amt, DType::Magic, self.src_r2, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                // no cast time, but a dash still interrupts attacking
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
