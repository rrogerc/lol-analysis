//! Azir. His basic attacks are routed through a Sand Soldier's magic-damage
//! stab (Arise!, kept up by recasting on cooldown/charge availability)
//! instead of his own auto-attack while a soldier is active; Conquering
//! Sands and Shifting Sands both require that soldier and go out on
//! cooldown; Emperor's Divide opens the fight as a single long-cooldown
//! nuke whose damage lands after its cast time.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Arise! is attempted (a soldier expires or a charge frees up).
const EV_W_CAST: u8 = 0;
/// A stocked Arise! charge regenerates.
const EV_CHARGE_REGEN: u8 = 1;
/// Shifting Sands is attempted on cooldown.
const EV_E_CAST: u8 = 2;
/// Emperor's Divide's damage lands after its cast time.
const EV_R_SWING: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    w_stab_dmg: f64,
    w_cast_cd: f64,
    w_recharge: f64,
    w_max_charges: i64,
    w_soldier_dur: f64,
    e_dmg: f64,
    e_cd: f64,
    r_dmg: f64,
    r_cast_time: f64,
    src_w_stab: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    charges: i64,
    /// When the next stocked charge regenerates (INF while already full).
    charge_regen_at: f64,
    /// Earliest time an Arise! recast may be issued (its own 1.5s min cd).
    w_ready: f64,
    /// The current Sand Soldier is active while `t < soldier_until`.
    soldier_until: f64,
    e_ready: f64,
    /// Emperor's Divide's pending swing (INF: none pending).
    r_swing_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let w_max_charges = kit.num("gen.W.maxCharges")? as i64;
        let state = State {
            charges: w_max_charges,
            charge_regen_at: INF,
            w_ready: 0.0,
            soldier_until: 0.0,
            e_ready: 0.0,
            r_swing_at: INF,
        };
        let w_base = kit.hit("gen.W.damage", ranks.w, sheet)?;
        let w_level_bonus = kit.at_level("gen.W.levelBonusByLevel", level)?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_stab_dmg: w_base + w_level_bonus,
            w_cast_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_recharge: kit.at_rank("gen.W.rechargeS", ranks.w)?,
            w_max_charges,
            w_soldier_dur: kit.num("gen.W.soldierDurationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_time: kit.num("gen.R.castTimeS")?,
            src_w_stab: intern("W onhit"),
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

    fn attack_damage(&self, e: &Engine) -> f64 {
        // Azir's own auto-attack is fully replaced by the soldier's stab
        // while one is active; the stab damage itself is dealt as on-hit.
        if e.st.t < self.s.soldier_until {
            0.0
        } else {
            e.p.ad
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if e.st.t < self.s.soldier_until {
            e.deal(self.w_stab_dmg, DType::Magic, self.src_w_stab, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if e.st.t < self.s.soldier_until {
            pymax(e.st.q_ready, e.st.t)
        } else {
            INF
        }
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
        // the opening cast: damage lands after the 0.5s cast time
        self.s.r_swing_at = e.st.t + self.r_cast_time;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 && self.s.charges > 0 {
            let t_w = pymax(pymax(self.s.w_ready, e.st.t), self.s.soldier_until);
            out[n] = (t_w, Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.w > 0 && self.s.charges < self.w_max_charges {
            out[n] = (self.s.charge_regen_at, Kind::Ev(EV_CHARGE_REGEN));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.r_swing_at != INF {
            out[n] = (self.s.r_swing_at, Kind::Ev(EV_R_SWING));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                let was_max = self.s.charges == self.w_max_charges;
                self.s.charges -= 1;
                if was_max {
                    self.s.charge_regen_at = t + self.w_recharge;
                }
                self.s.soldier_until = t + self.w_soldier_dur;
                self.s.w_ready = t + e.basic_cd(self.w_cast_cd);
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_CHARGE_REGEN) => {
                self.s.charges = imin(self.s.charges + 1, self.w_max_charges);
                self.s.charge_regen_at = if self.s.charges < self.w_max_charges {
                    t + self.w_recharge
                } else {
                    INF
                };
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                if t < self.s.soldier_until {
                    e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                    e.ability_cast_proc();
                    e.eclipse_hit();
                    e.prime_spellblade();
                    // the dummy counts as an enemy champion: dashing into it
                    // refunds an Arise! charge
                    if self.s.charges < self.w_max_charges {
                        self.s.charges += 1;
                        if self.s.charges == self.w_max_charges {
                            self.s.charge_regen_at = INF;
                        }
                    }
                }
            }
            Kind::Ev(EV_R_SWING) => {
                self.s.r_swing_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
