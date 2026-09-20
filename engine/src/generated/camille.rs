//! Camille. A mixed-damage bruiser: Precision Protocol arms an attack with
//! bonus physical damage, delayed-recast for doubled damage plus a true
//! damage conversion, Tactical Sweep and Hookshot/Wall Dive go out on
//! cooldown, and The Hextech Ultimatum opens the fight and rides every
//! basic attack against the target with bonus magic damage for its zone
//! duration.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Precision Protocol's delayed recast, timed 1.5 s after the first
/// empowered attack lands.
const EV_Q_RECAST: u8 = 0;
/// Tactical Sweep is cast (starts its 1.1 s animation).
const EV_W_CAST: u8 = 1;
/// Tactical Sweep's animation ends and its damage lands.
const EV_W_LAND: u8 = 2;
/// Hookshot/Wall Dive is cast; its damage lands on the same instant.
const EV_E_CAST: u8 = 3;
/// The Hextech Ultimatum's dash ends and the zone opens.
const EV_R_LAND: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_dmg: f64,
    q_amp: f64,
    q_true_frac: f64,
    q_ramp_up_s: f64,
    q_cd: f64,

    w_dmg: f64,
    w_outer_ratio: f64,
    w_channel_s: f64,
    w_cd: f64,

    e_dmg: f64,
    e_as_pct: f64,
    e_as_dur_s: f64,
    e_cd: f64,

    r_dash_s: f64,
    r_zone_s: f64,
    r_onhit_ratio: f64,

    src_q_recast: SourceId,
    src_q_true: SourceId,
    src_w_outer: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The real Q cooldown, which only starts once the second (recast)
    /// empowered attack lands.
    q_ready: f64,
    /// Whether the next basic attack is currently empowered.
    q_armed: bool,
    /// Whether the armed attack is the doubled/true-damage recast.
    q_is_second: bool,
    /// Whether the first empowered attack has landed and a recast is due.
    q_awaiting_recast: bool,
    /// When the first empowered attack landed (INF: none pending).
    q_first_landed_at: f64,

    w_ready: f64,
    /// When Tactical Sweep's animation ends (INF: not currently sweeping).
    w_channel_until: f64,

    e_ready: f64,
    /// Wall Dive's bonus attack speed lasts until this time.
    e_as_until: f64,

    /// When The Hextech Ultimatum's dash ends and the zone opens (INF: none pending).
    r_land_at: f64,
    /// The zone's bonus on-hit magic damage applies until this time.
    r_active_until: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            q_ready: 0.0,
            q_armed: false,
            q_is_second: false,
            q_awaiting_recast: false,
            q_first_landed_at: INF,
            w_ready: 0.0,
            w_channel_until: INF,
            e_ready: 0.0,
            e_as_until: -1.0,
            r_land_at: INF,
            r_active_until: -1.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("camille kit needs attack.windupFraction")?,

            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_amp: kit.num("gen.Q.empoweredAmp")?,
            q_true_frac: kit.at_level("gen.Q.trueDmgPctByLevel", level)?,
            q_ramp_up_s: kit.num("gen.Q.rampUpS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,

            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_outer_ratio: kit.at_rank("gen.W.outerHpRatio", ranks.w)?,
            w_channel_s: kit.num("gen.W.channelS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_as_pct: kit.at_rank("gen.E.asBuffByRank", ranks.e)? * 100.0,
            e_as_dur_s: kit.num("gen.E.asDurationS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,

            r_dash_s: kit.num("gen.R.dashS")?,
            r_zone_s: kit.at_rank("gen.R.zoneDurationS", ranks.r)?,
            r_onhit_ratio: kit.at_rank("gen.R.onhitCurrentHpRatio", ranks.r)?,

            src_q_recast: intern("Q recast"),
            src_q_true: intern("Q recast true"),
            src_w_outer: intern("W outer"),

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

    fn bonus_as(&self, t: f64) -> f64 {
        if t < self.s.e_as_until {
            self.e_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, _st: &mut St, t: f64, factor: f64) {
        shave(&mut self.s.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.q_armed {
            self.s.q_armed = false;
            if self.s.q_is_second {
                // the delayed recast: doubled bonus damage, part converted to
                // true damage, dealt just before the physical portion
                let bonus = self.q_dmg * self.q_amp;
                let true_dmg = bonus * self.q_true_frac;
                let phys_dmg = bonus - true_dmg;
                e.deal(true_dmg, DType::True, self.src_q_true, false, true, 1.0);
                e.deal(phys_dmg, DType::Physical, self.src_q_recast, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.q_ready = e.st.t + e.basic_cd(self.q_cd);
                self.s.q_awaiting_recast = false;
                self.s.q_is_second = false;
                self.s.q_first_landed_at = INF;
            } else {
                e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.q_first_landed_at = e.st.t;
                self.s.q_awaiting_recast = true;
            }
        }
        if e.st.t < self.s.r_active_until {
            // The Hextech Ultimatum's zone: bonus magic damage on every
            // basic attack against the target, as a percent of its current health
            let amt = self.r_onhit_ratio * pymax(e.st.hp, 0.0);
            e.deal(amt, DType::Magic, SRC_R, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.q_armed || self.s.q_awaiting_recast {
            return INF;
        }
        pymax(self.s.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // Precision Protocol resets Camille's basic attack timer
        e.prime_spellblade();
        self.s.q_armed = true;
        self.s.q_is_second = false;
        let b = self.bonus_as(e.st.t);
        e.st.next_attack = e.st.t + e.attack_windup(b, self.windup_fraction);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // Camille dashes for 0.5 s, then lands and opens the zone
        e.prime_spellblade();
        self.s.r_land_at = e.st.t + self.r_dash_s;
        e.lockout();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.q > 0 && self.s.q_awaiting_recast && !self.s.q_armed {
            let t = pymax(self.s.q_first_landed_at + self.q_ramp_up_s, e.st.t);
            out[n] = (t, Kind::Ev(EV_Q_RECAST));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_channel_until == INF {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            } else {
                out[n] = (self.s.w_channel_until, Kind::Ev(EV_W_LAND));
            }
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
            Kind::Ev(EV_Q_RECAST) => {
                // the delayed recast, a genuine second cast: also resets the
                // basic attack timer
                self.s.q_armed = true;
                self.s.q_is_second = true;
                e.prime_spellblade();
                let b = self.bonus_as(t);
                e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
            }
            Kind::Ev(EV_W_CAST) => {
                // Tactical Sweep's 1.1 s animation: ghosted, no basic attacks,
                // but other abilities can still be cast
                e.prime_spellblade();
                self.s.w_channel_until = t + self.w_channel_s;
                e.st.next_attack = pymax(e.st.next_attack, self.s.w_channel_until);
            }
            Kind::Ev(EV_W_LAND) => {
                self.s.w_channel_until = INF;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Physical, SRC_W, false, true, 1.0);
                e.deal(
                    self.w_outer_ratio * e.target_hp,
                    DType::Physical,
                    self.src_w_outer,
                    false,
                    true,
                    1.0,
                );
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_CAST) => {
                // Hookshot/Wall Dive: assumed terrain is in range, so damage
                // and the attack speed buff land immediately on cast
                e.prime_spellblade();
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.e_as_until = t + self.e_as_dur_s;
                e.lockout();
            }
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                self.s.r_active_until = t + self.r_zone_s;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
