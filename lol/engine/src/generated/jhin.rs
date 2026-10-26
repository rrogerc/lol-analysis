//! Jhin. A ranged auto-attacker gated by his 4-round magazine and 2.5s
//! reload; the 4th shot is approximated as a flat +50% rider plus a
//! missing-health execute rider. His attack speed is fixed (item bonus AS is
//! cancelled from his attack timing and, with crit chance, instead feeds
//! Every Moment Matters' bonus AD, applied to every attack and ability).
//! Casts go one at a time via one `busy_until`: Curtain Call opens the fight
//! and its channel (through the fourth recast's landing) keeps Jhin busy
//! ahead of Q, W and E, each of which then keeps him busy for its own cast
//! time before the next cast or attack can start. Dancing Grenade and Deadly
//! Flourish are cast on cooldown; Captive Audience is cast whenever a Lotus
//! Trap charge is available, its damage landing after an arm+detonation
//! delay; Curtain Call's four recasts fire as fast as their static 1s
//! spacing allows.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const EV_W: u8 = 0;
const EV_E_CAST: u8 = 1;
const EV_E_RECHARGE: u8 = 2;
const EV_E_DET0: u8 = 3;
const EV_E_DET1: u8 = 4;
const EV_E_DET2: u8 = 5;
const EV_R_SHOT: u8 = 6;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    // Passive
    p_max_ammo: i64,
    p_reload_s: f64,
    p_fourth_mult: f64,
    p_execute_pct: f64,
    p_bonus_ad_pct: f64,
    neg_item_as: f64,

    // Q
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,

    // W
    w_dmg: f64,
    w_cd: f64,
    w_cast_s: f64,

    // E
    e_dmg: f64,
    e_cast_s: f64,
    e_arm_s: f64,
    e_det_s: f64,
    e_base_cd: f64,
    e_recharge_s: f64,
    e_max_charges: i64,

    // R
    r_dmg: f64,
    r_fourth_mult: f64,
    r_missing_coef: f64,
    r_channel_cast_s: f64,
    r_shot_delay_s: f64,
    r_shot_fire_delay_s: f64,

    src_p_fourth: SourceId,
    src_p_execute: SourceId,
    src_w: SourceId,
    src_e: SourceId,
    src_r_fourth: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    clip: i64,
    w_ready: f64,
    e_ready: f64,
    e_charges: i64,
    e_recharge_at: f64,
    e_det: [f64; 3],
    r_shot_idx: i64,
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
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_bonus_ad_pct = kit.at_level("gen.P.bonusAdPctByLevel", level)?
            + kit.num("gen.P.critChanceCoef")? * (sheet.crit_chance / 100.0)
            + kit.num("gen.P.bonusAsCoef")? * (sheet.bonus_as_pct / 100.0);
        let ad_adj = sheet.ad * (1.0 + p_bonus_ad_pct);
        let ap = sheet.ap;

        let q_base = kit.at_rank("gen.Q.damage.base", ranks.q)?;
        let q_ad_ratio = kit.at_rank("gen.Q.damage.adRatio", ranks.q)?;
        let q_ap_ratio = kit.num("gen.Q.damage.apRatio")?;
        let q_dmg = q_base + q_ad_ratio * ad_adj + q_ap_ratio * ap;

        let w_base = kit.at_rank("gen.W.damage.base", ranks.w)?;
        let w_ad_ratio = kit.num("gen.W.damage.adRatio")?;
        let w_dmg = w_base + w_ad_ratio * ad_adj;

        let e_base = kit.at_rank("gen.E.damage.base", ranks.e)?;
        let e_ad_ratio = kit.num("gen.E.damage.adRatio")?;
        let e_ap_ratio = kit.num("gen.E.damage.apRatio")?;
        let e_dmg = e_base + e_ad_ratio * ad_adj + e_ap_ratio * ap;

        let r_base = kit.at_rank("gen.R.damage.base", ranks.r)?;
        let r_ad_ratio = kit.num("gen.R.damage.adRatio")?;
        let r_dmg = r_base + r_ad_ratio * ad_adj;

        let e_max_charges = kit.num("gen.E.maxCharges")? as i64;

        let state = State {
            busy_until: 0.0,
            clip: 0,
            w_ready: 0.0,
            e_ready: 0.0,
            e_charges: e_max_charges,
            e_recharge_at: INF,
            e_det: [INF, INF, INF],
            r_shot_idx: 0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("jhin kit needs attack.windupFraction")?,

            p_max_ammo: kit.num("gen.P.maxAmmo")? as i64,
            p_reload_s: kit.num("gen.P.reloadTimeS")?,
            p_fourth_mult: kit.num("gen.P.fourthShotDamageMult")?,
            p_execute_pct: kit.at_level("gen.P.executePctByLevel", level)?,
            p_bonus_ad_pct,
            neg_item_as: -sheet.bonus_as_pct,

            q_dmg,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,

            w_dmg,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,

            e_dmg,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_arm_s: kit.num("gen.E.armTimeS")?,
            e_det_s: kit.num("gen.E.detonationDelayS")?,
            e_base_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_recharge_s: kit.at_rank("gen.E.rechargeS", ranks.e)?,
            e_max_charges,

            r_dmg,
            r_fourth_mult: kit.num("gen.R.fourthShotMult")?,
            r_missing_coef: kit.num("gen.R.missingHpAmpCoef")?,
            r_channel_cast_s: kit.num("gen.R.channelCastTimeS")?,
            r_shot_delay_s: kit.num("gen.R.shotDelayS")?,
            r_shot_fire_delay_s: kit.num("gen.R.shotFireDelayS")?,

            src_p_fourth: intern("P fourth shot"),
            src_p_execute: intern("P execute"),
            src_w: intern("W"),
            src_e: intern("E"),
            src_r_fourth: intern("R fourth shot"),

            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        true
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self, _t: f64) -> f64 {
        // Jhin's attack speed cannot rise past his level-based value: cancel
        // out whatever bonus attack speed the build's items would otherwise
        // add to his attack timing.
        self.neg_item_as
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        e.p.ad * (1.0 + self.p_bonus_ad_pct)
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
        shave(&mut self.s.e_recharge_at, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {
        self.s.clip += 1;
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.clip == self.p_max_ammo {
            let ad = self.attack_damage(e);
            let extra = ad * (self.p_fourth_mult - 1.0);
            e.deal(extra, DType::Physical, self.src_p_fourth, false, false, 1.0);
            let missing = pymax(e.target_hp - pymax(e.st.hp, 0.0), 0.0);
            let exec = self.p_execute_pct * missing;
            e.deal(exec, DType::Physical, self.src_p_execute, false, false, 1.0);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.clip >= self.p_max_ammo {
            self.s.clip = 0;
            e.st.next_attack = t + self.p_reload_s;
        } else {
            let b = self.bonus_as(t);
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // The channel (its Active cast plus the four 1s-spaced recasts)
        // occupies Jhin until the fourth bullet lands: no attack and no
        // other cast starts before then.
        let land3 = self.r_channel_cast_s
            + 3.0 * self.r_shot_delay_s
            + self.r_shot_fire_delay_s;
        self.busy_for(e, land3);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_charges > 0 {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
                n += 1;
            }
            if self.s.e_charges < self.e_max_charges && self.s.e_recharge_at != INF {
                out[n] = (self.s.e_recharge_at, Kind::Ev(EV_E_RECHARGE));
                n += 1;
            }
            if self.s.e_det[0] != INF {
                out[n] = (self.s.e_det[0], Kind::Ev(EV_E_DET0));
                n += 1;
            }
            if self.s.e_det[1] != INF {
                out[n] = (self.s.e_det[1], Kind::Ev(EV_E_DET1));
                n += 1;
            }
            if self.s.e_det[2] != INF {
                out[n] = (self.s.e_det[2], Kind::Ev(EV_E_DET2));
                n += 1;
            }
        }
        if self.ranks.r > 0 && self.s.r_shot_idx < 4 {
            let k = self.s.r_shot_idx as f64;
            let land = self.r_channel_cast_s + k * self.r_shot_delay_s + self.r_shot_fire_delay_s;
            out[n] = (land, Kind::Ev(EV_R_SHOT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Physical, self.src_w, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.w_cast_s);
            }
            Kind::Ev(EV_E_CAST) => {
                let was_full = self.s.e_charges >= self.e_max_charges;
                self.s.e_charges -= 1;
                if was_full {
                    self.s.e_recharge_at = t + e.basic_cd(self.e_recharge_s);
                }
                self.s.e_ready = t + e.basic_cd(self.e_base_cd);
                let land = t + self.e_cast_s + self.e_arm_s + self.e_det_s;
                for i in 0..3 {
                    if self.s.e_det[i] == INF {
                        self.s.e_det[i] = land;
                        break;
                    }
                }
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_E_RECHARGE) => {
                self.s.e_charges = imin(self.s.e_charges + 1, self.e_max_charges);
                if self.s.e_charges < self.e_max_charges {
                    self.s.e_recharge_at = t + e.basic_cd(self.e_recharge_s);
                } else {
                    self.s.e_recharge_at = INF;
                }
            }
            Kind::Ev(EV_E_DET0) => {
                self.s.e_det[0] = INF;
                e.deal(self.e_dmg, DType::Magic, self.src_e, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_DET1) => {
                self.s.e_det[1] = INF;
                e.deal(self.e_dmg, DType::Magic, self.src_e, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_DET2) => {
                self.s.e_det[2] = INF;
                e.deal(self.e_dmg, DType::Magic, self.src_e, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_R_SHOT) => {
                let k = self.s.r_shot_idx;
                self.s.r_shot_idx += 1;
                let missing_frac = pymax(e.target_hp - pymax(e.st.hp, 0.0), 0.0) / e.target_hp;
                let amp = 1.0 + self.r_missing_coef * missing_frac;
                let is_fourth = k == 3;
                let base = if is_fourth { self.r_dmg * self.r_fourth_mult } else { self.r_dmg };
                let dmg = base * amp;
                let src = if is_fourth { self.src_r_fourth } else { SRC_R };
                e.deal(dmg, DType::Physical, src, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
