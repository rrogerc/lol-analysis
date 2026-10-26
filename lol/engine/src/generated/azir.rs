//! Azir. His basic attacks are routed through a Sand Soldier's magic-damage
//! stab (Arise!, kept up by recasting on cooldown/charge availability)
//! instead of his own auto-attack while a soldier is active; Conquering
//! Sands and Shifting Sands both require that soldier and go out on
//! cooldown; Emperor's Divide opens the fight as a single long-cooldown
//! nuke whose damage lands after its cast time. Casts go one at a time: a
//! shared `busy_until` (set by Q, W and R, each of which has a real cast
//! time) keeps every other cast and attack waiting until it ends.

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
    q_cast_time: f64,
    w_stab_dmg: f64,
    w_cast_cd: f64,
    w_cast_time: f64,
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
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
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
        let w_max_charges = kit.num("gen.W.maxCharges")? as i64;
        let state = State {
            busy_until: 0.0,
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
            q_cast_time: kit.num("gen.Q.castTimeS")?,
            w_stab_dmg: w_base + w_level_bonus,
            w_cast_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_time: kit.num("gen.W.castTimeS")?,
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
            self.castable_at(e, e.st.q_ready)
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
        self.busy_for(e, self.q_cast_time);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: its 0.5s cast time keeps Azir busy, and the
        // damage lands via a scheduled event once it ends
        self.s.r_swing_at = e.st.t + self.r_cast_time;
        self.busy_for(e, self.r_cast_time);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 && self.s.charges > 0 {
            let ready = pymax(self.s.w_ready, self.s.soldier_until);
            out[n] = (self.castable_at(e, ready), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.w > 0 && self.s.charges < self.w_max_charges {
            out[n] = (self.s.charge_regen_at, Kind::Ev(EV_CHARGE_REGEN));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
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
                self.busy_for(e, self.w_cast_time);
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
                    // E has no cast time, but it still cannot start inside
                    // another cast (castable_at above already ensures that);
                    // it does interrupt an attack in progress, like a dash
                    e.lockout();
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
