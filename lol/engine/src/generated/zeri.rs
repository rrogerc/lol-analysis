//! Zeri. Living Battery replaces her basic attacks with magic zap/full-charge
//! damage gated by a 0-100 charge counter; Burst Fire (Q) is an independent,
//! attack-speed-gated multi-hit physical burst fired alongside those attacks;
//! Spark Surge (E) arms Burst Fire's first hit with bonus magic damage and
//! resets her attack timer and Q; Lightning Crash (R) opens the fight with a
//! nova hit and puts her into Overcharged (fewer, still-AD-scaling Burst Fire
//! rounds, +30% bonus AS, refreshed by landed hits).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Ultrashock Laser: it is cast, then its damage lands after its cast time.
const EV_W_CAST: u8 = 0;
const EV_W_LAND: u8 = 1;
/// Spark Surge: instant cast on cooldown.
const EV_E_CAST: u8 = 2;
/// Lightning Crash: damage lands after its 0.25s cast.
const EV_R_LAND: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    max_charge: f64,
    charge_per_attack: f64,
    charge_per_qcast: f64,

    p_zap_dmg: f64,
    p_exec_threshold: f64,
    p_full_flat: f64,
    p_full_hp_pct: f64,

    q_per_hit: f64,
    q_missiles: i64,
    q_energized_missiles: i64,
    q_as_cap: f64,

    w_dmg: f64,
    w_cd: f64,
    w_cast_base: f64,
    w_cast_min: f64,
    w_cast_as_scalar: f64,

    e_bonus_dmg: f64,
    e_cd: f64,
    e_buff_duration: f64,
    e_cdr_per_hit: f64,
    e_cdr_avg_from_q: f64,

    r_dmg: f64,
    r_cast_time: f64,
    r_duration: f64,
    r_extend: f64,
    r_bonus_as_pct: f64,

    src_p_zap: SourceId,
    src_p_full: SourceId,
    src_w: SourceId,
    src_e: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    charge: f64,
    pending_full: bool,
    w_ready: f64,
    w_land_at: f64,
    e_ready: f64,
    e_buff_until: f64,
    r_until: f64,
    r_land_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let max_charge = kit.num("gen.P.maxCharge")?;
        let charge_per_attack = kit.num("gen.P.chargePerAttack")?;
        let charge_per_qcast = kit.num("gen.P.chargePerQCast")?;
        let start_charge = kit.num("gen.P.startCharge")?;

        let p_zap_base = kit.at_level("gen.P.zapBaseByLevel", level)?;
        let p_zap_ap_ratio = kit.num("gen.P.zapApRatio")?;
        let p_zap_dmg = p_zap_base + p_zap_ap_ratio * sheet.ap;

        let p_exec_base = kit.at_level("gen.P.executeThresholdByLevel", level)?;
        let p_exec_ap_ratio = kit.num("gen.P.executeApRatio")?;
        let p_exec_threshold = p_exec_base + p_exec_ap_ratio * sheet.ap;

        let p_full_base = kit.at_level("gen.P.fullChargeBaseByLevel", level)?;
        let p_full_ap_ratio = kit.num("gen.P.fullChargeApRatio")?;
        let p_full_flat = p_full_base + p_full_ap_ratio * sheet.ap;
        let p_full_hp_pct = kit.at_level("gen.P.fullChargeTargetHpPctByLevel", level)?;

        let q_as_cap = kit.num("gen.Q.attackSpeedCap")?;
        let excess_mult = kit.num("gen.Q.excessAsToAdMult")?;
        let base_as_no_bonus = sheet.attack_speed / (1.0 + sheet.bonus_as_pct / 100.0);
        let cap_bonus_as_pct = (q_as_cap / base_as_no_bonus - 1.0) * 100.0;
        let excess = pymax(0.0, sheet.bonus_as_pct - cap_bonus_as_pct);
        let bonus_ad_from_as = excess_mult * excess;

        let q_base = kit.at_rank("gen.Q.baseByRank", ranks.q)?;
        let q_ad_ratio = kit.at_rank("gen.Q.adRatioByRank", ranks.q)?;
        let q_missiles = kit.num("gen.Q.numberOfMissiles")? as i64;
        let q_energized_missiles = kit.num("gen.Q.energizedMissiles")? as i64;
        let q_total = q_base + q_ad_ratio * (sheet.ad + bonus_ad_from_as);
        let q_per_hit = q_total / (q_missiles as f64);

        let w_base = kit.at_rank("gen.W.baseByRank", ranks.w)?;
        let w_ad_ratio = kit.num("gen.W.adRatio")?;
        let w_ap_ratio = kit.num("gen.W.apRatio")?;
        let w_dmg = w_base + w_ad_ratio * (sheet.ad + bonus_ad_from_as) + w_ap_ratio * sheet.ap;
        let w_cd = kit.at_rank("abilities.W.cooldownS", ranks.w)?;
        let w_cast_base = kit.num("gen.W.castTimeBase")?;
        let w_cast_min = kit.num("gen.W.castTimeMin")?;
        let w_cast_as_scalar = kit.num("gen.W.castTimeASScalar")?;

        let e_bonus_base = kit.at_rank("gen.E.bonusDamageBaseByRank", ranks.e)?;
        let e_bonus_ap_ratio = kit.num("gen.E.bonusApRatio")?;
        let crit_chance_frac = sheet.crit_chance / 100.0;
        let crit_damage_mult = sheet.crit_damage / 100.0;
        let e_bonus_dmg = (e_bonus_base + e_bonus_ap_ratio * sheet.ap)
            * (1.0 + crit_chance_frac * (crit_damage_mult - 1.0));
        let e_cd = kit.at_rank("abilities.E.cooldownS", ranks.e)?;
        let e_buff_duration = kit.num("gen.E.buffDurationS")?;
        let e_cdr_per_hit = kit.num("gen.E.cdReductionPerHit")?;
        let e_cdr_per_crit_hit = kit.num("gen.E.cdReductionPerCritHit")?;
        let e_cdr_avg_from_q = e_cdr_per_hit + crit_chance_frac * (e_cdr_per_crit_hit - e_cdr_per_hit);

        let r_base = kit.at_rank("gen.R.novaBaseByRank", ranks.r)?;
        let r_bonus_ad_ratio = kit.num("gen.R.bonusAdRatio")?;
        let r_ap_ratio = kit.num("gen.R.apRatio")?;
        let r_dmg = r_base + r_bonus_ad_ratio * (sheet.ad_bonus + bonus_ad_from_as) + r_ap_ratio * sheet.ap;
        let r_cast_time = kit.num("gen.R.castTimeS")?;
        let r_duration = kit.num("gen.R.overchargedDurationS")?;
        let r_extend = kit.num("gen.R.extendPerHitS")?;
        let r_bonus_as_pct = kit.num("gen.R.bonusASPct")?;

        let state = State {
            charge: start_charge,
            pending_full: false,
            w_ready: 0.0,
            w_land_at: INF,
            e_ready: 0.0,
            e_buff_until: -1.0,
            r_until: -1.0,
            r_land_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("zeri kit needs attack.windupFraction")?,
            max_charge,
            charge_per_attack,
            charge_per_qcast,
            p_zap_dmg,
            p_exec_threshold,
            p_full_flat,
            p_full_hp_pct,
            q_per_hit,
            q_missiles,
            q_energized_missiles,
            q_as_cap,
            w_dmg,
            w_cd,
            w_cast_base,
            w_cast_min,
            w_cast_as_scalar,
            e_bonus_dmg,
            e_cd,
            e_buff_duration,
            e_cdr_per_hit,
            e_cdr_avg_from_q,
            r_dmg,
            r_cast_time,
            r_duration,
            r_extend,
            r_bonus_as_pct,
            src_p_zap: intern("P zap"),
            src_p_full: intern("P full charge"),
            src_w: intern("W"),
            src_e: intern("E onhit"),
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
        if t < self.s.r_until {
            self.r_bonus_as_pct * 100.0
        } else {
            0.0
        }
    }

    fn attack_damage(&self, _e: &Engine) -> f64 {
        // Living Battery replaces the physical attack damage entirely.
        0.0
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {
        self.s.pending_full = self.s.charge >= self.max_charge;
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.pending_full {
            let amt = self.p_full_flat + self.p_full_hp_pct * e.target_hp;
            e.deal(amt, DType::Magic, self.src_p_full, false, true, 1.0);
            e.ability_cast_proc();
            self.s.charge = 0.0;
            if t < self.s.r_until {
                self.s.r_until = pymin(self.s.r_until + self.r_extend, t + self.r_duration);
            }
            self.s.e_ready -= self.e_cdr_per_hit;
        } else {
            let cur_hp = pymax(e.st.hp, 0.0);
            if cur_hp < self.p_exec_threshold {
                e.deal(cur_hp, DType::Magic, self.src_p_zap, false, true, 1.0);
            } else {
                e.deal(self.p_zap_dmg, DType::Magic, self.src_p_zap, false, true, 1.0);
            }
            e.ability_cast_proc();
            self.s.charge = pymax(self.s.charge - self.charge_per_attack, 0.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let overcharged = t < self.s.r_until;
        let rounds = if overcharged { self.q_energized_missiles } else { self.q_missiles };
        let lightning_active = t < self.s.e_buff_until;

        let mut i: i64 = 0;
        while i < rounds {
            e.deal(self.q_per_hit, DType::Physical, SRC_Q, true, false, 1.0);
            i += 1;
        }
        if lightning_active && rounds > 0 {
            e.deal(self.e_bonus_dmg, DType::Magic, self.src_e, false, false, 1.0);
        }
        e.ability_cast_proc();
        e.eclipse_hit();

        self.s.charge = pymin(self.s.charge + self.charge_per_qcast, self.max_charge);
        self.s.e_ready -= self.e_cdr_avg_from_q;
        if t < self.s.r_until {
            self.s.r_until = pymin(self.s.r_until + self.r_extend, t + self.r_duration);
        }

        let bonus_as = self.bonus_as(t);
        let period = pymax(e.attack_period(bonus_as), 1.0 / self.q_as_cap);
        e.st.q_ready = t + period;
    }

    fn cast_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_land_at = t + self.r_cast_time;
        e.prime_spellblade();
        e.lockout();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_land_at != INF {
            out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_land_at != INF {
                out[n] = (self.s.w_land_at, Kind::Ev(EV_W_LAND));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.s.r_until = t + self.r_duration;
            }
            Kind::Ev(EV_W_CAST) => {
                let bonus_as_pct = e.p.sheet.bonus_as_pct + self.bonus_as(t);
                let reduction = self.w_cast_as_scalar * (bonus_as_pct / 100.0);
                let cast_time = pymax(self.w_cast_min, self.w_cast_base - reduction);
                self.s.w_land_at = t + cast_time;
                self.s.w_ready = INF;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_W_LAND) => {
                self.s.w_land_at = INF;
                e.deal(self.w_dmg, DType::Physical, self.src_w, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.w_ready = t + e.basic_cd(self.w_cd);
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_buff_until = t + self.e_buff_duration;
                e.prime_spellblade();
                e.st.next_attack = t;
                e.st.q_ready = t;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
