//! Nilah. Apotheosis opens the fight (channel then burst), then Formless
//! Blade goes out on cooldown for its own damage and its empowerment window
//! (attack speed plus a 100% AD on-hit cone), Slipstream is spent on a
//! 2-charge system whenever a charge is banked, and basic attacks fill the
//! rest of the time. Jubilant Veil is never cast: it is purely defensive.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Apotheosis: a tick, then the burst.
const EV_R_TICK: u8 = 0;
const EV_R_BURST: u8 = 1;
/// Slipstream: cast when a charge is banked, and a charge recharging.
const EV_E_CAST: u8 = 2;
const EV_E_RECHARGE: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_cd: f64,
    q_dmg: f64,
    q_as_bonus_pct: f64,
    q_buff_dur: f64,
    q_onhit_ratio: f64,
    e_recharge: f64,
    e_dmg: f64,
    e_max_charges: i64,
    r_channel_until: f64,
    r_tick_dmg: f64,
    r_burst_dmg: f64,
    r_num_ticks: i64,
    r_tick_interval: f64,
    src_q_onhit: SourceId,
    src_r_tick: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Formless Blade's empowerment: attacks are empowered until this time.
    q_empowered_until: f64,
    /// Slipstream's charge bank, and when the next recharge completes
    /// (INF: no recharge pending).
    e_charges: i64,
    e_next_charge_at: f64,
    /// Apotheosis: ticks fired so far, the next tick's time (INF: done),
    /// and the burst's time (INF: none pending).
    r_tick_idx: i64,
    r_next_tick_at: f64,
    r_burst_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let active_crit_scaling = kit.num("gen.Q.activeCritScaling")?;
        let crit_chance = sheet.crit_chance / 100.0;
        let crit_damage = sheet.crit_damage / 100.0;
        let q_crit_mult = 1.0 + active_crit_scaling * crit_chance * (crit_damage - 1.0);
        let q_base = kit.hit("gen.Q.damage", ranks.q, sheet)?;

        let e_max_charges = kit.num("gen.E.maxCharges")? as i64;
        let e_start_charges = kit.num("gen.E.startingCharges")? as i64;

        let r_num_ticks = kit.num("gen.R.numTicks")? as i64;
        let r_tick_interval = kit.num("gen.R.tickIntervalS")?;
        let r_channel_dur = kit.num("gen.R.channelDurationS")?;
        let r_channel_until = if ranks.r > 0 { r_channel_dur } else { 0.0 };

        let state = State {
            q_empowered_until: 0.0,
            e_charges: e_start_charges,
            e_next_charge_at: INF,
            r_tick_idx: 0,
            r_next_tick_at: INF,
            r_burst_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("nilah kit needs attack.windupFraction")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_dmg: q_base * q_crit_mult,
            q_as_bonus_pct: kit.at_level("gen.Q.empoweredAsPctByLevel", level)?,
            q_buff_dur: kit.num("gen.Q.buffDurationS")?,
            q_onhit_ratio: kit.num("gen.Q.onhitAdRatio")?,
            e_recharge: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_max_charges,
            r_channel_until,
            r_tick_dmg: kit.hit("gen.R.tickDamage", ranks.r, sheet)?,
            r_burst_dmg: kit.hit("gen.R.burstDamage", ranks.r, sheet)?,
            r_num_ticks,
            r_tick_interval,
            src_q_onhit: intern("Q onhit"),
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

    fn bonus_as(&self, t: f64) -> f64 {
        if t < self.s.q_empowered_until {
            self.q_as_bonus_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_next_charge_at, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if e.st.t < self.s.q_empowered_until {
            e.deal(self.q_onhit_ratio * e.p.ad, DType::Physical, self.src_q_onhit, true, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if e.st.t < self.r_channel_until {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.q_empowered_until = t + self.q_buff_dur;
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_tick_idx = 0;
        self.s.r_next_tick_at = self.r_tick_interval;
        self.s.r_burst_at = self.r_channel_until;
        e.st.next_attack = pymax(e.st.next_attack, t + self.r_channel_until);
        e.prime_spellblade();
        e.ability_cast_proc();
        e.eclipse_hit();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_next_tick_at != INF {
            out[n] = (self.s.r_next_tick_at, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        if self.s.r_burst_at != INF {
            out[n] = (self.s.r_burst_at, Kind::Ev(EV_R_BURST));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_charges > 0 {
                let at = pymax(e.st.t, self.r_channel_until);
                out[n] = (at, Kind::Ev(EV_E_CAST));
                n += 1;
            }
            if self.s.e_next_charge_at != INF {
                out[n] = (self.s.e_next_charge_at, Kind::Ev(EV_E_RECHARGE));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_TICK) => {
                e.deal(self.r_tick_dmg, DType::Physical, self.src_r_tick, false, true, 1.0);
                self.s.r_tick_idx += 1;
                if self.s.r_tick_idx < self.r_num_ticks {
                    self.s.r_next_tick_at = self.r_tick_interval * (self.s.r_tick_idx + 1) as f64;
                } else {
                    self.s.r_next_tick_at = INF;
                }
            }
            Kind::Ev(EV_R_BURST) => {
                self.s.r_burst_at = INF;
                e.deal(self.r_burst_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ult_hatefog();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_charges -= 1;
                if self.s.e_next_charge_at == INF {
                    self.s.e_next_charge_at = t + e.basic_cd(self.e_recharge);
                }
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                let b = self.bonus_as(t);
                e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
            }
            Kind::Ev(EV_E_RECHARGE) => {
                self.s.e_charges = imin(self.s.e_charges + 1, self.e_max_charges);
                if self.s.e_charges < self.e_max_charges {
                    self.s.e_next_charge_at = t + e.basic_cd(self.e_recharge);
                } else {
                    self.s.e_next_charge_at = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
