//! Vex. A pure ability-caster who weaves basic attacks between casts:
//! Shadow Surge opens the fight with its initial hit and an immediate,
//! cast-time-free recast for the mark-consume damage; Mistral Bolt,
//! Personal Space and Looming Darkness are cast on cooldown throughout.
//! Doom 'n Gloom never procs against a stationary target and is not modeled.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Personal Space is cast on cooldown.
const EV_W: u8 = 0;
/// Looming Darkness is cast on cooldown.
const EV_E: u8 = 1;
/// Shadow Surge's initial hit lands (its cast time after the opening cast).
const EV_R: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    r_initial_dmg: f64,
    r_recast_dmg: f64,
    r_cast_s: f64,
    r_cd: f64,
    src_r_recast: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    e_ready: f64,
    /// When Shadow Surge's initial hit lands (INF: none pending).
    r_fire_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            w_ready: 0.0,
            e_ready: 0.0,
            r_fire_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_initial_dmg: kit.hit("gen.R.initial", ranks.r, sheet)?,
            r_recast_dmg: kit.hit("gen.R.recast", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            src_r_recast: intern("R recast"),
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
        shave(&mut self.s.w_ready, t, factor);
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
        // the opening cast: the engine has primed Spellblade and held the
        // first attack past the cast; the initial hit lands when the cast
        // ends
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_fire_at = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E));
            n += 1;
        }
        if self.s.r_fire_at != INF {
            out[n] = (self.s.r_fire_at, Kind::Ev(EV_R));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_R) => {
                self.s.r_fire_at = INF;
                // initial hit: Spellblade was already primed at the opening cast
                e.deal(self.r_initial_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                // immediate, cast-time-free recast consuming the mark
                e.deal(self.r_recast_dmg, DType::Magic, self.src_r_recast, false, true, 1.0);
                e.prime_spellblade();
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
