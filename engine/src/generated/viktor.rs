//! Viktor. A mixed attacker/caster: Siphon Power is cast on cooldown for its
//! missile damage and arms Discharge, which converts the next basic attack
//! into a separate modified-magic-damage hit instead of a normal attack;
//! Hextech Ray is an instant plain-cast laser on cooldown; Arcane Storm opens
//! the fight with an initial burst followed by a once-per-second DoT storm.
//! Gravity Field deals no damage and is never cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Discharge's arming window lapsing unused.
const EV_DISCHARGE_EXPIRE: u8 = 0;
/// Hextech Ray, cast (and recast) on cooldown.
const EV_E_CAST: u8 = 1;
/// Arcane Storm's initial burst landing (after the opening cast time), and
/// each subsequent tick of the storm.
const EV_R_BURST: u8 = 2;
const EV_R_TICK: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_cd: f64,
    q_missile_dmg: f64,
    q_onhit_dmg: f64,
    discharge_dur: f64,
    e_cd: f64,
    e_dmg: f64,
    r_cast_s: f64,
    r_burst_dmg: f64,
    r_tick_dmg: f64,
    r_tick_cadence: f64,
    r_max_ticks: i64,
    src_q_onhit: SourceId,
    src_r_tick: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    discharge_armed: bool,
    discharge_expire: f64,
    e_ready: f64,
    /// Arcane Storm's pending initial burst, and its ticking DoT (INF: none pending).
    r_burst_at: f64,
    r_tick_next: f64,
    r_ticks_done: i64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            discharge_armed: false,
            discharge_expire: 0.0,
            e_ready: 0.0,
            r_burst_at: INF,
            r_tick_next: INF,
            r_ticks_done: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("viktor kit needs attack.windupFraction")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_missile_dmg: kit.hit("gen.Q.missile.damage", ranks.q, sheet)?,
            q_onhit_dmg: kit.hit("gen.Q.onhit.damage", ranks.q, sheet)?,
            discharge_dur: kit.num("gen.Q.dischargeDurationS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_burst_dmg: kit.hit("gen.R.initial.damage", ranks.r, sheet)?,
            r_tick_dmg: kit.hit("gen.R.tick.damage", ranks.r, sheet)?,
            r_tick_cadence: kit.num("gen.R.tickCadenceS")?,
            r_max_ticks: kit.num("gen.R.maxTicks")? as i64,
            src_q_onhit: intern("Q empowered"),
            src_r_tick: intern("R tick"),
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
        // Discharge replaces the normal attack damage entirely with its own
        // modified magic damage, dealt separately in after_attack.
        if self.s.discharge_armed {
            0.0
        } else {
            e.p.ad
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.discharge_armed {
            self.s.discharge_armed = false;
            e.deal(self.q_onhit_dmg, DType::Magic, self.src_q_onhit, false, false, 1.0);
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
        e.deal(self.q_missile_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.discharge_armed = true;
        self.s.discharge_expire = e.st.t + self.discharge_dur;
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack past the cast; the burst lands when it ends
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_burst_at = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.discharge_armed {
            out[n] = (self.s.discharge_expire, Kind::Ev(EV_DISCHARGE_EXPIRE));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.r_burst_at != INF {
            out[n] = (self.s.r_burst_at, Kind::Ev(EV_R_BURST));
            n += 1;
        }
        if self.s.r_tick_next != INF {
            out[n] = (self.s.r_tick_next, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_DISCHARGE_EXPIRE) => {
                // unused within its window: simply lapses
                self.s.discharge_armed = false;
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_BURST) => {
                self.s.r_burst_at = INF;
                e.deal(self.r_burst_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.s.r_tick_next = t + self.r_tick_cadence;
                self.s.r_ticks_done = 0;
            }
            Kind::Ev(EV_R_TICK) => {
                e.deal(self.r_tick_dmg, DType::Magic, self.src_r_tick, false, true, 1.0);
                self.s.r_ticks_done += 1;
                if self.s.r_ticks_done < self.r_max_ticks {
                    self.s.r_tick_next = t + self.r_tick_cadence;
                } else {
                    self.s.r_tick_next = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
