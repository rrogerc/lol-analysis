//! Vi. Opens with Cease and Desist, weaves Relentless Force into her attack
//! rhythm for its reset, charges Vault Breaker to its full damage cap on
//! cooldown, and rides Denting Blows off every third stack-applying hit
//! (attacks, the Relentless Force attack, and Vault Breaker's dash).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Vault Breaker's dash lands (the full 1.25 s charge cap).
const EV_Q_LAND: u8 = 0;
/// A Relentless Force charge finishes recharging.
const EV_E_RECHARGE: u8 = 1;
/// Cease and Desist's damage lands (cast time + grab delay).
const EV_R_LAND: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    q_charge_cap_s: f64,
    w_frac: f64,
    w_as_pct: f64,
    w_buff_dur: f64,
    w_shred_dur: f64,
    e_dmg: f64,
    e_recharge_cd: f64,
    e_static_cd: f64,
    e_max_charges: i64,
    r_dmg: f64,
    r_cast_s: f64,
    r_grab_delay_s: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_charging: bool,
    q_land_at: f64,
    e_charges: i64,
    /// When the next spent charge finishes recharging (INF: already full).
    e_recharge_at: f64,
    e_static_ready: f64,
    e_armed: bool,
    /// Denting Blows stacks already applied (0-2; the 3rd application consumes them).
    w_stacks: i64,
    w_as_until: f64,
    /// Cease and Desist's damage still to land (INF once fired).
    r_land_at: f64,
}

impl GenDriver {
    /// A basic attack, the Relentless Force blast, or Vault Breaker's dash
    /// applies one Denting Blows stack; the 3rd consumes them all.
    fn apply_w_stack(&mut self, e: &mut Engine) {
        if self.ranks.w == 0 {
            return;
        }
        if self.s.w_stacks < 2 {
            self.s.w_stacks += 1;
        } else {
            self.s.w_stacks = 0;
            let t = e.st.t;
            let dmg = self.w_frac * e.target_hp;
            e.deal(dmg, DType::Physical, SRC_W, false, false, 1.0);
            e.st.shred_until = t + self.w_shred_dur;
            self.s.w_as_until = t + self.w_buff_dur;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let e_max_charges = kit.num("gen.E.maxCharges")? as i64;
        let w_base = kit.at_rank("gen.W.baseDamagePct", ranks.w)?;
        let w_ad_coef = kit.num("gen.W.adCoefficient")?;
        let w_frac = (w_base + w_ad_coef * sheet.ad_bonus) / 100.0;
        let state = State {
            q_charging: false,
            q_land_at: INF,
            e_charges: e_max_charges,
            e_recharge_at: INF,
            e_static_ready: 0.0,
            e_armed: false,
            w_stacks: 0,
            w_as_until: -1.0,
            r_land_at: INF,
        };
        let _ = level;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("vi kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)? * kit.num("gen.Q.maxDamageMult")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_charge_cap_s: kit.num("gen.Q.chargeCapS")?,
            w_frac,
            w_as_pct: kit.at_rank("gen.W.asPctByRank", ranks.w)?,
            w_buff_dur: kit.num("gen.W.buffDurationS")?,
            w_shred_dur: kit.num("abilities.Q.shred.durationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_recharge_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_static_cd: kit.num("gen.E.staticCooldownS")?,
            e_max_charges,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_grab_delay_s: kit.num("gen.R.grabDelayS")?,
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
        if t < self.s.w_as_until {
            self.w_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        if self.s.e_recharge_at != INF {
            shave(&mut self.s.e_recharge_at, t, factor);
        }
    }

    fn before_attack(&mut self, e: &mut Engine) {
        self.apply_w_stack(e);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.e_armed {
            self.s.e_armed = false;
            e.deal(self.e_dmg, DType::Physical, SRC_E, true, true, 1.0);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.ranks.e > 0 && !self.s.e_armed && self.s.e_charges > 0 && t >= self.s.e_static_ready {
            // Relentless Force, woven in right after an attack: its reset
            // brings the next attack one windup away.
            self.s.e_charges -= 1;
            if self.s.e_recharge_at == INF {
                self.s.e_recharge_at = t + self.e_recharge_cd;
            }
            self.s.e_static_ready = t + self.e_static_cd;
            self.s.e_armed = true;
            e.prime_spellblade();
            e.ability_cast_proc();
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.q_charging {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.q_charging = true;
        self.s.q_land_at = t + self.q_charge_cap_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.q_land_at);
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        e.lockout();
        self.s.r_land_at = t + self.r_cast_s + self.r_grab_delay_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.r_land_at);
    }

    fn events(&self, _e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.q > 0 && self.s.q_charging {
            out[n] = (self.s.q_land_at, Kind::Ev(EV_Q_LAND));
            n += 1;
        }
        if self.ranks.e > 0 && self.s.e_recharge_at != INF {
            out[n] = (self.s.e_recharge_at, Kind::Ev(EV_E_RECHARGE));
            n += 1;
        }
        if self.ranks.r > 0 && self.s.r_land_at != INF {
            out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_LAND) => {
                self.s.q_charging = false;
                self.s.q_land_at = INF;
                e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
                self.apply_w_stack(e);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.st.q_ready = t + e.basic_cd(self.q_cd);
            }
            Kind::Ev(EV_E_RECHARGE) => {
                self.s.e_charges = imin(self.s.e_charges + 1, self.e_max_charges);
                if self.s.e_charges < self.e_max_charges {
                    self.s.e_recharge_at = t + self.e_recharge_cd;
                } else {
                    self.s.e_recharge_at = INF;
                }
            }
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
