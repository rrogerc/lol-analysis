//! Taric. Bravado (P) is the core engine: every ability cast (Q/W/E) grants
//! or refreshes up to two stored empowered basic attacks (100% total attack
//! speed and bonus magic damage from bonus armor) and shaves the remaining
//! cooldowns of Q/W/E. Landing an empowered attack grants a Starlight's
//! Touch (Q) charge, which is spent the instant one is available purely to
//! re-trigger Bravado; Bastion (W) is self-cast on cooldown for its armor
//! and its own Bravado proc; Dazzle (E) is cast on cooldown for its damage,
//! its cast and its delayed hit tracked as two separate events so each has
//! its own, always-advancing readiness. Cosmic Radiance (R) is never cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Dazzle's damage lands (after its 1s wind-up).
const EV_E_HIT: u8 = 0;
/// Dazzle is cast (its readiness poll).
const EV_E_CAST: u8 = 1;
/// Bastion is self-cast (its readiness poll).
const EV_W_CAST: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    p_dmg_base: f64,
    p_armor_coef: f64,
    p_duration: f64,
    p_as_bonus: f64,
    q_cd: f64,
    q_max_charges: i64,
    w_cd: f64,
    w_armor_bonus: f64,
    e_cd: f64,
    e_dmg_base: f64,
    e_armor_coef: f64,
    e_windup_s: f64,
    bonus_armor_before_w: f64,
    bonus_armor_after_w: f64,
    /// The fraction of a basic ability's remaining cooldown KEPT by a
    /// Bravado proc (1.0: no reduction, 0.0: instantly ready).
    brav_keep_frac: f64,
    src_p: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Bravado's stored empowered attacks (0-2) and when they expire.
    brav_charges: i64,
    brav_until: f64,
    /// Set by `before_attack`, read by `after_attack`.
    attack_empowered: bool,
    /// Starlight's Touch's stocked charges (from empowered attacks only).
    q_charges: i64,
    w_ready: f64,
    w_active: bool,
    e_ready: f64,
    /// When Dazzle's damage lands (INF: not mid wind-up).
    e_hit_at: f64,
}

impl GenDriver {
    /// The bonus armor Bravado's on-attack damage and Dazzle's damage read.
    fn bonus_armor(&self) -> f64 {
        if self.s.w_active {
            self.bonus_armor_after_w
        } else {
            self.bonus_armor_before_w
        }
    }

    /// After casting Q/W/E: Bravado's stored-attack queue and its Q/W/E
    /// cooldown reduction.
    fn bravado_proc(&mut self, e: &mut Engine, t: f64) {
        let cur = if t <= self.s.brav_until { self.s.brav_charges } else { 0 };
        if cur <= 1 {
            self.s.brav_charges = 2;
            self.s.brav_until = t + self.p_duration;
        } else {
            self.s.brav_charges = cur;
        }
        let factor = self.brav_keep_frac;
        shave(&mut e.st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let w_armor_pct = kit.at_rank("gen.W.armorPct", ranks.w)?;
        let bonus_armor_before_w = sheet.armor;
        let w_armor_bonus = w_armor_pct * sheet.armor;
        let bonus_armor_after_w = bonus_armor_before_w + w_armor_bonus;
        let haste = sheet.haste;
        let state = State {
            brav_charges: 0,
            brav_until: 0.0,
            attack_empowered: false,
            q_charges: 0,
            w_ready: 0.0,
            w_active: false,
            e_ready: 0.0,
            e_hit_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("taric kit needs attack.windupFraction")?,
            p_dmg_base: kit.at_level("gen.P.damageByLevel", level)?,
            p_armor_coef: kit.num("gen.P.armorCoef")?,
            p_duration: kit.num("gen.P.durationS")?,
            p_as_bonus: kit.num("gen.P.attackSpeedBonusPct")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_max_charges: kit.at_rank("gen.Q.maxCharges", ranks.q)? as i64,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_armor_bonus,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_dmg_base: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_armor_coef: kit.num("gen.E.armorCoef")?,
            e_windup_s: kit.num("gen.E.windupS")?,
            bonus_armor_before_w,
            bonus_armor_after_w,
            brav_keep_frac: 1.0 - haste / (haste + 100.0),
            src_p: intern("P onhit"),
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
        if self.s.brav_charges > 0 && t <= self.s.brav_until {
            self.p_as_bonus
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.brav_charges > 0 && t <= self.s.brav_until {
            if self.s.brav_charges == 2 {
                // the first attack of the pair refreshes the window
                self.s.brav_until = t + self.p_duration;
            }
            self.s.brav_charges -= 1;
            self.s.attack_empowered = true;
        } else {
            self.s.brav_charges = 0;
            self.s.attack_empowered = false;
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.attack_empowered {
            let dmg = self.p_dmg_base + self.p_armor_coef * self.bonus_armor();
            e.deal(dmg, DType::Magic, self.src_p, false, false, 1.0);
            self.s.q_charges = imin(self.s.q_charges + 1, self.q_max_charges);
            self.s.attack_empowered = false;
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.q_charges < 1 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.q_charges = 0;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.bravado_proc(e, t);
        e.prime_spellblade();
        e.lockout();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.e_hit_at != INF {
            out[n] = (self.s.e_hit_at, Kind::Ev(EV_E_HIT));
            n += 1;
        } else if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                // no cast time: Dazzle winds up for 1s before its damage;
                // the cooldown starts at the cast
                self.s.e_hit_at = t + self.e_windup_s;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.bravado_proc(e, t);
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_HIT) => {
                self.s.e_hit_at = INF;
                let dmg = self.e_dmg_base + self.e_armor_coef * self.bonus_armor();
                e.deal(dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_active = true;
                self.bravado_proc(e, t);
                e.prime_spellblade();
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
