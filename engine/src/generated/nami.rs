//! Nami. A caster who still auto-attacks between casts: Tidal Wave opens the
//! fight, Tidecaller's Blessing is self-cast alongside it to arm empowered
//! hits, Aqua Prison's bubble lands on a delay after its cast, and Ebb and
//! Flow resolves on cast; all four ride Tidecaller's Blessing's bonus on-hit
//! damage whenever a charge is armed.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Aqua Prison's bubble lands (after its cast + travel delay).
const EV_Q_LAND: u8 = 0;
/// Ebb and Flow is cast (and resolves immediately: no travel modeled).
const EV_W_CAST: u8 = 1;
/// Tidecaller's Blessing is cast on herself.
const EV_E_CAST: u8 = 2;
/// Tidal Wave's wave lands (after its cast time).
const EV_R_LAND: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    q_travel_s: f64,
    w_dmg: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    e_duration_s: f64,
    e_hit_count: i64,
    r_dmg: f64,
    r_cast_s: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// When Aqua Prison's pending bubble lands (INF: none pending).
    q_land_at: f64,
    w_ready: f64,
    e_ready: f64,
    /// Tidecaller's Blessing: charges left, and until when they're usable.
    e_charges: i64,
    e_until: f64,
    /// When Tidal Wave's pending wave lands (INF: none pending).
    r_land_at: f64,
}

impl GenDriver {
    /// Spends one Tidecaller's Blessing charge if one is armed, returning
    /// whether its bonus damage should be dealt.
    fn try_consume_e(&mut self, t: f64) -> bool {
        if self.s.e_charges > 0 && t <= self.s.e_until {
            self.s.e_charges -= 1;
            true
        } else {
            false
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            q_land_at: INF,
            w_ready: 0.0,
            e_ready: 0.0,
            e_charges: 0,
            e_until: -1.0,
            r_land_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("nami kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_travel_s: kit.num("gen.Q.travelTotalS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_duration_s: kit.num("gen.E.buffDurationS")?,
            e_hit_count: kit.num("gen.E.hitCount")? as i64,
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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.try_consume_e(t) {
            e.deal(self.e_dmg, DType::Magic, SRC_E, false, false, 1.0);
        }
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
        e.lockout();
        e.prime_spellblade();
        self.s.q_land_at = t + self.q_travel_s;
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack past the cast; the wave lands when it ends
        self.s.r_land_at = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_land_at != INF {
            out[n] = (self.s.q_land_at, Kind::Ev(EV_Q_LAND));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.r_land_at != INF {
            out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
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
                if self.try_consume_e(t) {
                    e.deal(self.e_dmg, DType::Magic, SRC_E, false, false, 1.0);
                }
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.lockout();
                e.prime_spellblade();
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                if self.try_consume_e(t) {
                    e.deal(self.e_dmg, DType::Magic, SRC_E, false, false, 1.0);
                }
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.prime_spellblade();
                self.s.e_charges = self.e_hit_count;
                self.s.e_until = t + self.e_duration_s;
            }
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                if self.try_consume_e(t) {
                    e.deal(self.e_dmg, DType::Magic, SRC_E, false, false, 1.0);
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
