//! Rumble. Heat gates Q/W/E: each Q/E cast spends/generates 20 Heat, a cast
//! made at >=50 Heat (Danger Zone) is empowered 50%, and reaching 150 Heat
//! forces a 4s Overheat that disables Q/W/E/R while granting bonus attack
//! speed and on-hit magic damage. W is never cast (no modeled damage). R is
//! opened at t=0 and ticks its burning field; Q has no cast time and ticks
//! its cone; E has a 0.25s cast time (its damage lands at the cast, then it
//! keeps Rumble busy for 0.25s) and is fired off cooldown/charges while not
//! Overheated and not already busy with another cast, refreshing a flat MR
//! shred.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const EV_Q_TICK: u8 = 0;
const EV_R_TICK: u8 = 1;
const EV_OVERHEAT_END: u8 = 2;
const EV_E_CHARGE: u8 = 3;
const EV_E_CAST: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    // Q
    q_tick_dmg: f64,
    q_target_hp_ratio: f64,
    q_cd: f64,
    q_ticks: i64,
    q_tick_rate: f64,
    q_overheat_mult: f64,

    // E
    e_dmg: f64,
    e_overheat_mult: f64,
    e_cd: f64,
    e_charges_max: i64,
    e_starting_charges: i64,
    e_recharge_s: f64,
    e_shred_dur: f64,
    e_cast_time_s: f64,

    // R
    r_tick_dmg: f64,
    r_ticks: i64,
    r_tick_rate: f64,
    r_delay_s: f64,

    // P / Heat
    p_as_pct: f64,
    p_onhit_base: f64,
    p_onhit_ap_ratio: f64,
    p_onhit_target_hp_ratio: f64,
    heat_decay: f64,
    heat_grace: f64,
    heat_per_cast: f64,
    dz_heat: f64,
    overheat_heat: f64,
    overheat_dur: f64,

    src_p_onhit: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    busy_until: f64,
    heat: f64,
    heat_t: f64,
    /// Time Overheat ends; -1.0 means "not overheating".
    overheat_end: f64,
    e_charges: i64,
    e_charge_next: f64,
    e_ready: f64,
    q_tick_next: f64,
    q_ticks_left: i64,
    q_empowered: bool,
    r_tick_next: f64,
    r_ticks_left: i64,
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

    fn current_heat(&self, t: f64) -> f64 {
        let elapsed = t - self.s.heat_t;
        if elapsed <= self.heat_grace {
            self.s.heat
        } else {
            pymax(0.0, self.s.heat - self.heat_decay * (elapsed - self.heat_grace))
        }
    }

    fn add_heat(&mut self, t: f64, pre_heat: f64) {
        let new_heat = pre_heat + self.heat_per_cast;
        if new_heat >= self.overheat_heat {
            self.s.heat = self.overheat_heat;
            self.s.heat_t = t;
            self.s.overheat_end = t + self.overheat_dur;
        } else {
            self.s.heat = new_heat;
            self.s.heat_t = t;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            heat: 0.0,
            heat_t: 0.0,
            overheat_end: -1.0,
            e_charges: kit.num("gen.E.startingCharges")? as i64,
            e_charge_next: INF,
            e_ready: 0.0,
            q_tick_next: INF,
            q_ticks_left: 0,
            q_empowered: false,
            r_tick_next: INF,
            r_ticks_left: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("rumble kit needs attack.windupFraction")?,

            q_tick_dmg: kit.hit("gen.Q.tickDamage", ranks.q, sheet)?,
            q_target_hp_ratio: kit.at_rank("gen.Q.tickTargetMaxHpRatio", ranks.q)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_ticks: kit.num("gen.Q.ticks")? as i64,
            q_tick_rate: kit.num("gen.Q.tickRateS")?,
            q_overheat_mult: kit.num("gen.Q.overheatMulti")?,

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_overheat_mult: kit.num("gen.E.overheatMulti")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_charges_max: kit.num("gen.E.chargesMax")? as i64,
            e_starting_charges: kit.num("gen.E.startingCharges")? as i64,
            e_recharge_s: kit.num("gen.E.rechargeS")?,
            e_shred_dur: kit.num("gen.E.shredDurationS")?,
            e_cast_time_s: kit.num("gen.E.castTimeS")?,

            r_tick_dmg: kit.hit("gen.R.tickDamage", ranks.r, sheet)?,
            r_ticks: kit.num("gen.R.ticks")? as i64,
            r_tick_rate: kit.num("gen.R.tickRateS")?,
            r_delay_s: kit.num("gen.R.castDelayS")?,

            p_as_pct: kit.at_level("gen.P.bonusAsByLevel", level)? * 100.0,
            p_onhit_base: kit.at_level("gen.P.onhitByLevel", level)?,
            p_onhit_ap_ratio: kit.num("gen.P.onhitApRatio")?,
            p_onhit_target_hp_ratio: kit.num("gen.P.onhitTargetMaxHpRatio")?,
            heat_decay: kit.num("gen.P.heatDecayPerS")?,
            heat_grace: kit.num("gen.P.heatGraceS")?,
            heat_per_cast: kit.num("gen.P.heatPerCast")?,
            dz_heat: kit.num("gen.P.dangerZoneHeat")?,
            overheat_heat: kit.num("gen.P.overheatHeat")?,
            overheat_dur: kit.num("gen.P.overheatDurationS")?,

            src_p_onhit: intern("P onhit"),

            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
        self.s.e_charges = self.e_starting_charges;
    }

    fn ranged(&self) -> bool {
        self.attack_range > MELEE_MAX_RANGE
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self, t: f64) -> f64 {
        if t < self.s.overheat_end {
            self.p_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t < self.s.overheat_end {
            let dmg = self.p_onhit_base
                + self.p_onhit_ap_ratio * e.p.sheet.ap
                + self.p_onhit_target_hp_ratio * e.target_hp;
            e.deal(dmg, DType::Magic, self.src_p_onhit, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if e.st.t < self.s.overheat_end {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        let pre_heat = self.current_heat(t);
        let empowered = pre_heat >= self.dz_heat;
        self.add_heat(t, pre_heat);
        self.s.q_empowered = empowered;
        self.s.q_ticks_left = self.q_ticks;
        self.s.q_tick_next = t;
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_ticks_left = self.r_ticks;
        self.s.r_tick_next = t + self.r_delay_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_tick_next != INF {
            out[n] = (self.s.q_tick_next, Kind::Ev(EV_Q_TICK));
            n += 1;
        }
        if self.s.r_tick_next != INF {
            out[n] = (self.s.r_tick_next, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        if self.s.overheat_end > -0.5 {
            out[n] = (self.s.overheat_end, Kind::Ev(EV_OVERHEAT_END));
            n += 1;
        }
        if self.s.e_charge_next != INF {
            out[n] = (self.s.e_charge_next, Kind::Ev(EV_E_CHARGE));
            n += 1;
        }
        if self.ranks.e > 0 {
            let overheating = e.st.t < self.s.overheat_end;
            if self.s.e_charges > 0 && !overheating {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_TICK) => {
                let mult = if self.s.q_empowered { self.q_overheat_mult } else { 1.0 };
                let dmg = (self.q_tick_dmg + self.q_target_hp_ratio * e.target_hp) * mult;
                e.deal(dmg, DType::Magic, SRC_Q, false, true, 1.0);
                self.s.q_ticks_left -= 1;
                if self.s.q_ticks_left > 0 {
                    self.s.q_tick_next = t + self.q_tick_rate;
                } else {
                    self.s.q_tick_next = INF;
                }
            }
            Kind::Ev(EV_R_TICK) => {
                let first = self.s.r_ticks_left == self.r_ticks;
                e.deal(self.r_tick_dmg, DType::Magic, SRC_R, false, true, 1.0);
                if first {
                    e.ability_cast_proc();
                    e.eclipse_hit();
                    e.ult_hatefog();
                }
                self.s.r_ticks_left -= 1;
                if self.s.r_ticks_left > 0 {
                    self.s.r_tick_next = t + self.r_tick_rate;
                } else {
                    self.s.r_tick_next = INF;
                }
            }
            Kind::Ev(EV_OVERHEAT_END) => {
                self.s.heat = 0.0;
                self.s.heat_t = t;
                self.s.overheat_end = -1.0;
            }
            Kind::Ev(EV_E_CHARGE) => {
                self.s.e_charges = imin(self.s.e_charges + 1, self.e_charges_max);
                if self.s.e_charges < self.e_charges_max {
                    self.s.e_charge_next = t + self.e_recharge_s;
                } else {
                    self.s.e_charge_next = INF;
                }
            }
            Kind::Ev(EV_E_CAST) => {
                let was_max = self.s.e_charges == self.e_charges_max;
                self.s.e_charges -= 1;
                if was_max {
                    self.s.e_charge_next = t + self.e_recharge_s;
                }
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                let pre_heat = self.current_heat(t);
                let empowered = pre_heat >= self.dz_heat;
                self.add_heat(t, pre_heat);
                let mult = if empowered { self.e_overheat_mult } else { 1.0 };
                e.deal(self.e_dmg * mult, DType::Magic, SRC_E, false, true, 1.0);
                e.st.shred_until = t + self.e_shred_dur;
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_time_s);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
