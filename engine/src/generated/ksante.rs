//! K'Sante. Opens with All Out (R), then weaves Ntofo Strikes (Q) on cooldown
//! between attacks and charges Path Maker (W) to its maximum window every
//! cast for its best All-Out true-damage percentage. Dauntless Instinct (P)
//! marks enemies hit by an ability; the next basic attack against a marked
//! target consumes it for bonus physical damage. All Out additionally grants
//! a flat bonus-physical-damage instance on every basic attack, ability cast
//! and mark proc while it is active.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// R's damage lands after its cast time.
const EV_R_HIT: u8 = 0;
/// W starts charging, then recasts (deals damage) at the end of the charge.
const EV_W_CAST: u8 = 1;
const EV_W_RECAST: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    // Dauntless Instinct (P)
    p_flat: f64,
    p_mark_pct: f64,
    p_mark_dur: f64,
    /// All Out's bonus-physical-damage fraction of the target's max health,
    /// already folded with K'Sante's own bonus armor/MR (constant per fight).
    p_allout_pct: f64,

    // Ntofo Strikes (Q)
    q_base_dmg: f64,
    q_bonus_armor_ratio: f64,
    q_bonus_mr_ratio: f64,
    /// Base cooldown after the bonus-resistance formula, before ability haste.
    q_cd_base: f64,
    q_cd_allout_mult: f64,

    // Path Maker (W)
    w_phys_base_dmg: f64,
    w_phys_hp_ratio: f64,
    w_phys_bonus_armor_ratio: f64,
    w_phys_bonus_mr_ratio: f64,
    w_true_base_dmg: f64,
    w_true_hp_ratio: f64,
    w_true_bonus_armor_ratio: f64,
    w_true_bonus_mr_ratio: f64,
    w_cd: f64,
    w_charge_s: f64,

    // All Out (R)
    r_base_dmg: f64,
    r_as_pct: f64,
    r_allout_dur: f64,
    r_cast_s: f64,

    bonus_armor: f64,
    bonus_mr: f64,

    src_p_mark: SourceId,
    src_p_attack: SourceId,
    src_p_allout: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    w_charging: bool,
    w_charge_end: f64,
    /// When R's own damage lands (INF if none pending / already resolved).
    r_cast_at: f64,
    /// The instant All Out ends (a sentinel below 0 means it never started).
    all_out_until: f64,
    mark_active: bool,
    mark_expiry: f64,
}

impl GenDriver {
    fn apply_mark(&mut self, t: f64) {
        self.s.mark_active = true;
        self.s.mark_expiry = t + self.p_mark_dur;
    }

    /// All Out's flat bonus physical damage, as its own instance (see notes).
    fn apply_allout_bonus(&mut self, e: &mut Engine, src: SourceId, t: f64) {
        if t < self.s.all_out_until {
            let amt = self.p_allout_pct * e.target_hp;
            e.deal(amt, DType::Physical, src, false, false, 1.0);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let base_armor = kit.at_level("gen.stats.baseArmorByLevel", level)?;
        let base_mr = kit.at_level("gen.stats.baseMrByLevel", level)?;
        let bonus_armor = pymax(0.0, sheet.armor - base_armor);
        let bonus_mr = pymax(0.0, sheet.mr - base_mr);

        let resist_cap = kit.num("gen.Q.cooldownResistCap")?;
        let bonus_res_capped = pymin(bonus_armor + bonus_mr, resist_cap);
        let q_cd_base = pymax(
            kit.num("gen.Q.cooldownMin")?,
            kit.at_rank("abilities.Q.cooldownS", ranks.q)?
                - kit.num("gen.Q.cooldownResistSlope")? * bonus_res_capped,
        );

        let allout_resist_ratio = kit.num("gen.P.allOutResistRatio")?;
        let p_allout_pct = kit.num("gen.P.allOutBase")?
            + allout_resist_ratio * bonus_armor
            + allout_resist_ratio * bonus_mr;

        let state = State {
            w_ready: 0.0,
            w_charging: false,
            w_charge_end: INF,
            r_cast_at: INF,
            all_out_until: -1.0,
            mark_active: false,
            mark_expiry: -1.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("ksante kit needs attack.windupFraction")?,

            p_flat: kit.num("gen.P.flatDamage")?,
            p_mark_pct: kit.at_level("gen.P.markPctByLevel", level)?,
            p_mark_dur: kit.num("gen.P.markDurationS")?,
            p_allout_pct,

            q_base_dmg: kit.at_rank("gen.Q.baseDamage", ranks.q)?,
            q_bonus_armor_ratio: kit.num("gen.Q.bonusArmorRatio")?,
            q_bonus_mr_ratio: kit.num("gen.Q.bonusMrRatio")?,
            q_cd_base,
            q_cd_allout_mult: 1.0 - kit.num("gen.Q.allOutCdrPct")?,

            w_phys_base_dmg: kit.at_rank("gen.W.physDamage.base", ranks.w)?,
            w_phys_hp_ratio: kit.num("gen.W.physDamage.hpRatio")?,
            w_phys_bonus_armor_ratio: kit.num("gen.W.physDamage.bonusArmorRatio")?,
            w_phys_bonus_mr_ratio: kit.num("gen.W.physDamage.bonusMrRatio")?,
            w_true_base_dmg: kit.at_rank("gen.W.trueDamage.base", ranks.w)?,
            w_true_hp_ratio: kit.num("gen.W.trueDamage.hpRatio")?,
            w_true_bonus_armor_ratio: kit.num("gen.W.trueDamage.bonusArmorRatio")?,
            w_true_bonus_mr_ratio: kit.num("gen.W.trueDamage.bonusMrRatio")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_charge_s: kit.num("gen.W.maxChargeS")?,

            r_base_dmg: kit.at_rank("gen.R.baseDamage", ranks.r)?,
            r_as_pct: kit.at_rank("gen.R.attackSpeedPct", ranks.r)? * 100.0,
            r_allout_dur: kit.num("gen.R.allOutDurationS")?,
            r_cast_s: kit.num("gen.R.castTimeS")?,

            bonus_armor,
            bonus_mr,

            src_p_mark: intern("P mark"),
            src_p_attack: intern("P attack bonus"),
            src_p_allout: intern("P allout ability bonus"),

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
        if t < self.s.all_out_until {
            self.r_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.mark_active && t <= self.s.mark_expiry {
            self.s.mark_active = false;
            let target_hp = e.target_hp;
            let mut dmg = self.p_flat + self.p_mark_pct * target_hp;
            if t < self.s.all_out_until {
                dmg += self.p_allout_pct * target_hp;
            }
            e.deal(dmg, DType::Physical, self.src_p_mark, false, false, 1.0);
        }
        if t < self.s.all_out_until {
            let amt = self.p_allout_pct * e.target_hp;
            e.deal(amt, DType::Physical, self.src_p_attack, false, false, 1.0);
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
        let in_allout = t < self.s.all_out_until;
        let cd_base = if in_allout {
            self.q_cd_base * self.q_cd_allout_mult
        } else {
            self.q_cd_base
        };
        e.st.q_ready = t + e.basic_cd(cd_base);
        let dmg = self.q_base_dmg
            + self.q_bonus_armor_ratio * self.bonus_armor
            + self.q_bonus_mr_ratio * self.bonus_mr;
        e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.apply_allout_bonus(e, self.src_p_allout, t);
        self.apply_mark(t);
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_cast_at = t + self.r_cast_s;
        self.s.all_out_until = t + self.r_cast_s + self.r_allout_dur;
        // Path Maker's cooldown is refreshed upon entering All Out.
        self.s.w_ready = t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_cast_at != INF {
            out[n] = (self.s.r_cast_at, Kind::Ev(EV_R_HIT));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_charging {
                out[n] = (self.s.w_charge_end, Kind::Ev(EV_W_RECAST));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_HIT) => {
                self.s.r_cast_at = INF;
                e.deal(self.r_base_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.apply_mark(t);
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_charging = true;
                self.s.w_charge_end = t + self.w_charge_s;
                self.s.w_ready = INF;
                e.prime_spellblade();
                e.st.next_attack = pymax(e.st.next_attack, self.s.w_charge_end);
            }
            Kind::Ev(EV_W_RECAST) => {
                self.s.w_charging = false;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                let in_allout = t < self.s.all_out_until;
                let (dmg, dtype) = if in_allout {
                    let frac = self.w_true_hp_ratio
                        + self.w_true_bonus_armor_ratio * (self.bonus_armor / 100.0)
                        + self.w_true_bonus_mr_ratio * (self.bonus_mr / 100.0);
                    (self.w_true_base_dmg + frac * e.target_hp, DType::True)
                } else {
                    let frac = self.w_phys_hp_ratio
                        + self.w_phys_bonus_armor_ratio * (self.bonus_armor / 100.0)
                        + self.w_phys_bonus_mr_ratio * (self.bonus_mr / 100.0);
                    (self.w_phys_base_dmg + frac * e.target_hp, DType::Physical)
                };
                e.deal(dmg, dtype, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.apply_allout_bonus(e, self.src_p_allout, t);
                self.apply_mark(t);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
