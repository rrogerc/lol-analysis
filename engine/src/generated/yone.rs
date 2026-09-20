//! Yone. Basic attacks alternate Steel (physical) and Azakana (half physical
//! / half magic) forever; Way of the Hunter doubles crit chance (capped at
//! 100%), softens crit damage and turns excess crit chance into bonus AD.
//! Mortal Steel goes out on cooldown (its empowered cast cannot double-hit a
//! lone target, so it is never modeled separately); Spirit Cleave goes out
//! on cooldown; Fate Sealed opens the fight; Soul Unbound is held for its
//! full Spirit Form window to mark as much of Yone's own damage as possible
//! before detonating it as true damage.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const EV_R_GUST: u8 = 0;
const EV_W: u8 = 1;
const EV_E_CAST: u8 = 2;
const EV_E_RECAST: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    /// Passive: bonus AD from excess crit chance, and the expected-crit
    /// multiplier (Yone's own doubled-chance / softened-damage version),
    /// plus the AD compensation factor fed to the engine's own crit math
    /// on basic attacks so its expected output matches `e_yone`.
    extra_bonus_ad: f64,
    e_yone: f64,
    crit_compensation: f64,
    magic_split: f64,

    q_cd_base: f64,
    q_as_cd_percent: f64,
    q_as_cd_max: f64,
    q_dmg: f64,

    w_cd_base: f64,
    w_as_cd_percent: f64,
    w_as_cd_max: f64,
    w_half_base: f64,
    w_half_ratio: f64,

    e_cd_base: f64,
    e_mark_pct: f64,
    e_spirit_duration_s: f64,

    r_phys_dmg: f64,
    r_mag_dmg: f64,
    r_cast_s: f64,
    r_gust_delay_s: f64,

    src_w_mag: SourceId,
    src_r_mag: SourceId,
    src_azakana_mag: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    azakana_swing: bool,
    w_ready: f64,
    e_ready: f64,
    e_active: bool,
    e_recast_at: f64,
    e_marks: f64,
    r_gust_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let crit_chance_mult = kit.num("gen.P.critChanceMultiplier")?;
        let crit_to_ad = kit.num("gen.P.critToAdPerPercent")?;
        let crit_damage_mod = kit.num("gen.P.critDamageMod")?;
        let magic_split = kit.num("gen.P.magicDamageSplit")?;

        let p0 = sheet.crit_chance / 100.0;
        let m0 = sheet.crit_damage / 100.0;
        let doubled_frac = pymin(p0 * crit_chance_mult, 1.0);
        let excess_pct = pymax(p0 * crit_chance_mult * 100.0 - 100.0, 0.0);
        let extra_bonus_ad = excess_pct * crit_to_ad;
        let m_eff = 1.0 + (m0 - 1.0) * crit_damage_mod;
        let e_yone = 1.0 + doubled_frac * (m_eff - 1.0);
        let e_std = 1.0 + p0 * (m0 - 1.0);
        let crit_compensation = e_yone / e_std;

        let q_cd_base = kit.at_rank("abilities.Q.cooldownS", ranks.q)?;
        let q_as_cd_percent = kit.num("gen.Q.asCdPercent")?;
        let q_as_cd_max = kit.num("gen.Q.asCdMax")?;
        let q_base = kit.at_rank("gen.Q.damageBase", ranks.q)?;
        let q_ad_ratio = kit.num("gen.Q.adRatio")?;
        let q_ad_component = q_ad_ratio * (sheet.ad + extra_bonus_ad);
        let q_dmg = q_base + q_ad_component * e_yone;

        let w_cd_base = kit.at_rank("abilities.W.cooldownS", ranks.w)?;
        let w_as_cd_percent = kit.num("gen.W.asCdPercent")?;
        let w_as_cd_max = kit.num("gen.W.asCdMax")?;
        let w_half_base = kit.at_rank("gen.W.halfBase", ranks.w)?;
        let w_half_ratio = kit.at_rank("gen.W.halfTargetMaxHpRatio", ranks.w)?;

        let e_cd_base = kit.at_rank("abilities.E.cooldownS", ranks.e)?;
        let e_mark_pct = kit.at_rank("gen.E.markPercent", ranks.e)?;
        let e_spirit_duration_s = kit.num("gen.E.spiritFormDurationS")?;

        let r_phys_base = kit.at_rank("gen.R.physicalBase", ranks.r)?;
        let r_phys_ratio = kit.num("gen.R.physicalBonusAdRatio")?;
        let r_mag_base = kit.at_rank("gen.R.magicBase", ranks.r)?;
        let r_mag_ratio = kit.num("gen.R.magicBonusAdRatio")?;
        let r_phys_dmg = r_phys_base + r_phys_ratio * (sheet.ad_bonus + extra_bonus_ad);
        let r_mag_dmg = r_mag_base + r_mag_ratio * (sheet.ad_bonus + extra_bonus_ad);
        let r_cast_s = kit.num("gen.R.castTimeS")?;
        let r_gust_delay_s = kit.num("gen.R.gustDelayS")?;

        let state = State {
            azakana_swing: false,
            w_ready: 0.0,
            e_ready: 0.0,
            e_active: false,
            e_recast_at: INF,
            e_marks: 0.0,
            r_gust_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("yone kit needs attack.windupFraction")?,
            extra_bonus_ad,
            e_yone,
            crit_compensation,
            magic_split,
            q_cd_base,
            q_as_cd_percent,
            q_as_cd_max,
            q_dmg,
            w_cd_base,
            w_as_cd_percent,
            w_as_cd_max,
            w_half_base,
            w_half_ratio,
            e_cd_base,
            e_mark_pct,
            e_spirit_duration_s,
            r_phys_dmg,
            r_mag_dmg,
            r_cast_s,
            r_gust_delay_s,
            src_w_mag: intern("W magic"),
            src_r_mag: intern("R magic"),
            src_azakana_mag: intern("P azakana magic"),
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
        let real_ad = e.p.ad + self.extra_bonus_ad;
        let phys_frac = 1.0 - self.magic_split;
        let per_swing = if self.s.azakana_swing { real_ad * phys_frac } else { real_ad };
        per_swing * self.crit_compensation
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let real_ad = e.p.ad + self.extra_bonus_ad;
        let phys_frac = 1.0 - self.magic_split;
        if self.s.azakana_swing {
            let half_phys = real_ad * phys_frac;
            let half_magic = real_ad * self.magic_split;
            let magic_amt = half_magic * self.e_yone;
            e.deal(magic_amt, DType::Magic, self.src_azakana_mag, false, false, 1.0);
            if self.s.e_active {
                let phys_expected = half_phys * self.e_yone;
                self.s.e_marks += (phys_expected + magic_amt) * self.e_mark_pct;
            }
        } else if self.s.e_active {
            let phys_expected = real_ad * self.e_yone;
            self.s.e_marks += phys_expected * self.e_mark_pct;
        }
        self.s.azakana_swing = !self.s.azakana_swing;
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let bonus_as = e.p.sheet.bonus_as_pct;
        let reduction = pymin(bonus_as / 100.0 * self.q_as_cd_percent, self.q_as_cd_max);
        let effective_base = self.q_cd_base * (1.0 - reduction);
        e.st.q_ready = e.st.t + e.basic_cd(effective_base);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        if self.s.e_active {
            self.s.e_marks += self.q_dmg * self.e_mark_pct;
        }
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_gust_at = e.st.t + self.r_cast_s + self.r_gust_delay_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_gust_at != INF {
            out[n] = (self.s.r_gust_at, Kind::Ev(EV_R_GUST));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_active {
                out[n] = (self.s.e_recast_at, Kind::Ev(EV_E_RECAST));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_GUST) => {
                self.s.r_gust_at = INF;
                e.deal(self.r_phys_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.deal(self.r_mag_dmg, DType::Magic, self.src_r_mag, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                if self.s.e_active {
                    self.s.e_marks += (self.r_phys_dmg + self.r_mag_dmg) * self.e_mark_pct;
                }
            }
            Kind::Ev(EV_W) => {
                let bonus_as = e.p.sheet.bonus_as_pct;
                let reduction = pymin(bonus_as / 100.0 * self.w_as_cd_percent, self.w_as_cd_max);
                let effective_base = self.w_cd_base * (1.0 - reduction);
                self.s.w_ready = t + e.basic_cd(effective_base);
                let w_amt = self.w_half_base + self.w_half_ratio * e.target_hp;
                e.deal(w_amt, DType::Physical, SRC_W, false, true, 1.0);
                e.deal(w_amt, DType::Magic, self.src_w_mag, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                if self.s.e_active {
                    self.s.e_marks += (w_amt + w_amt) * self.e_mark_pct;
                }
                e.lockout();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_active = true;
                self.s.e_recast_at = t + self.e_spirit_duration_s;
                self.s.e_marks = 0.0;
                self.s.azakana_swing = false;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_RECAST) => {
                self.s.e_active = false;
                self.s.e_recast_at = INF;
                self.s.e_ready = t + e.basic_cd(self.e_cd_base);
                let amt = self.s.e_marks;
                e.deal(amt, DType::True, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.e_marks = 0.0;
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
