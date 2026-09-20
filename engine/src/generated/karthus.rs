//! Karthus. A caster who still attacks between casts: Wall of Pain shreds
//! magic resistance once at the start (and on cooldown after), Defile toggles
//! on immediately and ticks every 0.25s for as long as mana allows, Requiem
//! opens the fight with a 3s channel that blocks Lay Waste, and Lay Waste is
//! spammed on its ~1s cooldown as a delayed, always-doubled single-target
//! nuke.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Defile's recurring tick, Requiem's channel completion, Lay Waste's
/// delayed detonation, and Wall of Pain's cast/recast.
const EV_E_TICK: u8 = 0;
const EV_R_LAND: u8 = 1;
const EV_Q_LAND: u8 = 2;
const EV_W_CAST: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cost: f64,
    q_delay: f64,
    w_cd: f64,
    w_cost: f64,
    w_shred_dur: f64,
    e_dps: f64,
    e_cost_per_s: f64,
    e_tick_s: f64,
    r_dmg: f64,
    r_cost: f64,
    r_channel_s: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    mana: f64,
    w_ready: f64,
    defile_on: bool,
    e_next_tick: f64,
    /// Requiem: the channel blocks Lay Waste until this time; landed tracks
    /// whether the pending damage still needs to be dealt.
    r_channel_until: f64,
    r_landed: bool,
    /// Lay Waste's pending detonation (INF: none in flight).
    q_land_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            mana: sheet.mana,
            w_ready: if ranks.w > 0 { 0.0 } else { INF },
            defile_on: ranks.e > 0,
            e_next_tick: kit.num("gen.E.tickIntervalS")?,
            r_channel_until: 0.0,
            r_landed: true,
            q_land_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cost: kit.at_rank("gen.Q.costMana", ranks.q)?,
            q_delay: kit.num("gen.Q.detonationDelayS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cost: kit.at_rank("gen.W.costMana", ranks.w)?,
            w_shred_dur: kit.num("abilities.Q.shred.durationS")?,
            e_dps: kit.hit("gen.E.dps", ranks.e, sheet)?,
            e_cost_per_s: kit.at_rank("gen.E.costManaPerSecond", ranks.e)?,
            e_tick_s: kit.num("gen.E.tickIntervalS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cost: kit.at_rank("gen.R.costMana", ranks.r)?,
            r_channel_s: kit.num("gen.R.channelS")?,
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
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if e.st.t < self.s.r_channel_until {
            return INF;
        }
        if self.s.mana < self.q_cost {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        self.s.mana -= self.q_cost;
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
        self.s.q_land_at = e.st.t + self.q_delay;
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack past the cast; only the channel timers need
        // setting here, the damage (and its procs) land when the channel ends
        if self.s.mana < self.r_cost {
            return;
        }
        self.s.mana -= self.r_cost;
        let t = e.st.t;
        self.s.r_channel_until = t + self.r_channel_s;
        self.s.r_landed = false;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 && self.s.defile_on {
            out[n] = (self.s.e_next_tick, Kind::Ev(EV_E_TICK));
            n += 1;
        }
        if !self.s.r_landed {
            out[n] = (self.s.r_channel_until, Kind::Ev(EV_R_LAND));
            n += 1;
        }
        if self.s.q_land_at != INF {
            out[n] = (self.s.q_land_at, Kind::Ev(EV_Q_LAND));
            n += 1;
        }
        if self.ranks.w > 0 && self.s.w_ready != INF {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_TICK) => {
                // Defile: pay this tick's mana or switch off for good; a
                // toggle does not proc on-cast effects
                let cost = self.e_cost_per_s * self.e_tick_s;
                if self.s.mana >= cost {
                    self.s.mana -= cost;
                    let dmg = self.e_dps * self.e_tick_s;
                    e.deal(dmg, DType::Magic, SRC_E, false, true, 1.0);
                    self.s.e_next_tick = t + self.e_tick_s;
                } else {
                    self.s.defile_on = false;
                }
            }
            Kind::Ev(EV_R_LAND) => {
                self.s.r_landed = true;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_Q_LAND) => {
                self.s.q_land_at = INF;
                e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
            }
            Kind::Ev(EV_W_CAST) => {
                if self.s.mana >= self.w_cost {
                    self.s.mana -= self.w_cost;
                    e.st.shred_until = t + self.w_shred_dur;
                    e.prime_spellblade();
                    e.lockout();
                    self.s.w_ready = t + e.basic_cd(self.w_cd);
                } else {
                    self.s.w_ready = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
