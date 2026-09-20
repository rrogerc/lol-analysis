//! Soraka. A caster whose damage is entirely two abilities: Starcall (Q) is
//! cast on cooldown, its magic damage landing after a travel delay assumed
//! at max range; Equinox (E) is cast on cooldown, dealing its magic damage
//! instantly and again 1.5 s later when the zone erupts. Astral Infusion (W)
//! and Wish (R) deal no damage and are never cast. Basic attacks fill the
//! gaps via the engine's default attack loop.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Starcall's damage lands (after its assumed travel delay).
const EV_Q_LAND: u8 = 0;
/// Equinox is cast (its on-cast damage instance).
const EV_E_CAST: u8 = 1;
/// Equinox's zone erupts (its second damage instance).
const EV_E_ERUPT: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_delay_s: f64,
    e_dmg: f64,
    e_cd: f64,
    e_erupt_delay_s: f64,
    src_e_erupt: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// When Starcall's already-cast damage lands (INF: none pending).
    q_land_at: f64,
    e_ready: f64,
    /// When Equinox's zone erupts for its pending cast (INF: none pending).
    e_erupt_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            q_land_at: INF,
            e_ready: 0.0,
            e_erupt_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_delay_s: kit.num("gen.Q.travelDelayS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_erupt_delay_s: kit.num("gen.E.eruptDelayS")?,
            src_e_erupt: intern("E eruption"),
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
        shave(&mut self.s.e_ready, t, factor);
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
        self.s.q_land_at = t + self.q_delay_s;
        e.prime_spellblade();
        e.lockout();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_land_at != INF {
            out[n] = (self.s.q_land_at, Kind::Ev(EV_Q_LAND));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_erupt_at != INF {
                out[n] = (self.s.e_erupt_at, Kind::Ev(EV_E_ERUPT));
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
            Kind::Ev(EV_Q_LAND) => {
                self.s.q_land_at = INF;
                e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_erupt_at = t + self.e_erupt_delay_s;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_ERUPT) => {
                self.s.e_erupt_at = INF;
                e.deal(self.e_dmg, DType::Magic, self.src_e_erupt, false, true, 1.0);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
