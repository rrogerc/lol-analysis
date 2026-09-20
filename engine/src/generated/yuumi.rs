//! Yuumi. No ally exists in the fight, so she is never attached: Prowling
//! Projectile only ever deals its base missile damage, Zoomies buffs Yuumi
//! herself, and You and Me! / Feline Friendship never do anything relevant
//! to damage. Final Chapter opens the fight and fires its 5 waves on a
//! fixed schedule while locking out Q and basic attacks, matching its
//! channel-interaction table.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Zoomies is cast as soon as it is off cooldown.
const EV_E_CAST: u8 = 0;
/// Final Chapter starts a new channel once its post-effect cooldown ends.
const EV_R_CAST: u8 = 1;
/// One of the channel's 5 waves lands.
const EV_R_WAVE: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    e_as_pct: f64,
    e_cd: f64,
    e_duration_s: f64,
    r_dmg: f64,
    r_cd: f64,
    r_reduced_mult: f64,
    r_waves: i64,
    r_wave_interval: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    e_ready: f64,
    e_buff_until: f64,
    r_ready: f64,
    /// INF while no channel is running.
    r_wave_next_at: f64,
    r_waves_left: i64,
}

impl GenDriver {
    /// Starts a Final Chapter channel: schedules its first wave now and
    /// locks Prowling Projectile and basic attacks until the last wave.
    fn start_channel(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_wave_next_at = t;
        self.s.r_waves_left = self.r_waves;
        let lock_until = t + (self.r_waves - 1) as f64 * self.r_wave_interval;
        e.st.q_ready = pymax(e.st.q_ready, lock_until);
        e.st.next_attack = pymax(e.st.next_attack, lock_until);
        e.prime_spellblade();
        e.ability_cast_proc();
        e.eclipse_hit();
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            e_ready: 0.0,
            e_buff_until: 0.0,
            r_ready: 0.0,
            r_wave_next_at: INF,
            r_waves_left: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("yuumi kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            e_as_pct: kit.hit("gen.E.asBonus", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_duration_s: kit.num("gen.E.durationS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_reduced_mult: kit.num("gen.R.reducedMult")?,
            r_waves: kit.num("gen.R.numWaves")? as i64,
            r_wave_interval: kit.num("gen.R.waveIntervalS")?,
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
        if t < self.s.e_buff_until {
            self.e_as_pct
        } else {
            0.0
        }
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
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: start the channel immediately
        self.start_channel(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_wave_next_at != INF {
                out[n] = (self.s.r_wave_next_at, Kind::Ev(EV_R_WAVE));
            } else {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_buff_until = t + self.e_duration_s;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_CAST) => {
                self.start_channel(e);
            }
            Kind::Ev(EV_R_WAVE) => {
                let idx = self.r_waves - self.s.r_waves_left;
                let dmg = if idx == 0 {
                    self.r_dmg
                } else {
                    self.r_dmg * self.r_reduced_mult
                };
                e.deal(dmg, DType::Magic, SRC_R, false, true, 1.0);
                self.s.r_waves_left -= 1;
                if self.s.r_waves_left > 0 {
                    self.s.r_wave_next_at = t + self.r_wave_interval;
                } else {
                    self.s.r_wave_next_at = INF;
                    self.s.r_ready = t + e.ult_cd(self.r_cd);
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
