//! Zyra. A ranged mage whose damage in the dummy fight is her three
//! direct-damage casts (Deadly Spines, Grasping Roots, Stranglethorns) plus
//! basic attacks between them; the Seed/plant economy (P, W and the pets Q/E
//! can spawn) is out of scope, as explained in the kit's notes.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Deadly Spines' damage, delayed after the cast.
const EV_Q_DMG: u8 = 0;
/// Grasping Roots: the press (sets cooldown, schedules the delayed damage)
/// and the damage landing at cast-time end.
const EV_E_CAST: u8 = 1;
const EV_E_DMG: u8 = 2;
/// Stranglethorns' damage, delayed after the opening cast.
const EV_R_DMG: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    q_delay: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    r_dmg: f64,
    r_cast_s: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Grasping Roots: when it may next be cast.
    e_ready: f64,
    /// Grasping Roots: when its pending damage lands (INF: none pending).
    e_pending: f64,
    /// Deadly Spines: when its pending damage lands (INF: none pending).
    q_pending: f64,
    /// Stranglethorns: when its pending damage lands (INF: none pending).
    r_pending: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            e_ready: 0.0,
            e_pending: INF,
            q_pending: INF,
            r_pending: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("zyra kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_delay: kit.num("gen.Q.effectDelayS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
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
        shave(&mut self.s.e_ready, t, factor);
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
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.q_pending = t + self.q_delay;
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack past the cast; the thicket's damage lands
        // when the cast time ends
        self.s.r_pending = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_pending != INF {
            out[n] = (self.s.q_pending, Kind::Ev(EV_Q_DMG));
            n += 1;
        }
        if self.s.r_pending != INF {
            out[n] = (self.s.r_pending, Kind::Ev(EV_R_DMG));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_pending != INF {
                out[n] = (self.s.e_pending, Kind::Ev(EV_E_DMG));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_DMG) => {
                self.s.q_pending = INF;
                e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_R_DMG) => {
                self.s.r_pending = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_E_CAST) => {
                // the press: cooldown starts now, the damage lands at
                // cast-time end
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_pending = t + self.e_cast_s;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_DMG) => {
                self.s.e_pending = INF;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
