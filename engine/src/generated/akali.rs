//! Akali. Q is spammed on its flat 1.5s cooldown, E is cast on cooldown and
//! its recast used immediately to consume the mark (with a forced-attack
//! reset once it lands), and R opens the fight with its initial dash then
//! recasts as soon as its 2.5s static delay allows, its damage scaled by the
//! target's missing health. P's empowered attack never arms because
//! continuous Q casting keeps its ring refreshed (see kit notes); W deals no
//! damage and is never cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Perfect Execution's initial dash lands (after its cast time).
const EV_R1: u8 = 0;
/// Perfect Execution's recast, after the static 2.5s delay.
const EV_R2: u8 = 1;
/// Shuriken Flip's initial cast and immediate recast.
const EV_E: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    e1_dmg: f64,
    e2_dmg: f64,
    e_cd: f64,
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
    e_ready: f64,
    /// When Perfect Execution's initial dash lands (INF: none pending).
    r1_land_at: f64,
    /// When Perfect Execution's recast fires (INF: none pending).
    r2_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            e_ready: 0.0,
            r1_land_at: INF,
            r2_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            e1_dmg: kit.hit("gen.E.e1", ranks.e, sheet)?,
            e2_dmg: kit.hit("gen.E.e2", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
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
        self.s.r1_land_at = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r1_land_at != INF {
            out[n] = (self.s.r1_land_at, Kind::Ev(EV_R1));
            n += 1;
        }
        if self.s.r2_at != INF {
            out[n] = (self.s.r2_at, Kind::Ev(EV_R2));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R1) => {
                self.s.r1_land_at = INF;
                e.deal(self.r1_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.r2_at = t + self.r_recast_delay_s;
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
            }
            Kind::Ev(EV_E) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e1_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
                e.deal(self.e2_dmg, DType::Magic, self.src_e2, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                // the game orders a basic attack right after the recast dash lands
                e.st.next_attack = pymin(e.st.next_attack, t);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
