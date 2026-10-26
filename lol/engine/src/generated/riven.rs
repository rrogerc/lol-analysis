//! Riven. Opens with Blade of the Exile (R) for its locked-in bonus AD and
//! Wind Slash access (its 0.25 s cast time keeps her busy first), then
//! immediately chains the three Broken Wings (Q) casts, keeps Ki Burst (W)
//! on cooldown (its own 0.2667 s cast time also keeps her busy), fires Wind
//! Slash once the burst has landed, and lets Runic Blade (P) consume a
//! Charge on every basic attack.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Ki Burst comes off cooldown.
const EV_W: u8 = 0;
/// Blade of the Exile's bonuses (AD, Wind Slash access) apply.
const EV_R_BONUS: u8 = 1;
/// Blade of the Exile's empowerment ends.
const EV_R_END: u8 = 2;
/// Wind Slash is cast.
const EV_WINDSLASH: u8 = 3;
/// A Runic Blade Charge stack expires.
const EV_CHARGE_DECAY: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Runic Blade: fraction of total AD dealt on-hit, and stack rules.
    p_pct_level: f64,
    p_max_stacks: i64,
    p_stack_duration: f64,
    src_p: SourceId,
    p_dmg_base: f64,
    p_dmg_with_r: f64,
    /// Broken Wings.
    q_cd: f64,
    q_intercast_s: f64,
    q_dmg_base: f64,
    q_dmg_with_r: f64,
    /// Ki Burst.
    w_cd: f64,
    w_cast_s: f64,
    w_dmg_base: f64,
    w_dmg_with_r: f64,
    /// Blade of the Exile / Wind Slash.
    r_cast_s: f64,
    r_bonus_delay_s: f64,
    r_duration_s: f64,
    r_bonus_ad: f64,
    r_min_dmg: f64,
    r_max_dmg: f64,
    r_missing_cap_frac: f64,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    charges: i64,
    charge_until: f64,
    /// Broken Wings: sub-cast index (0..3) of the current combo, and the
    /// time of the last sub-cast (for the 0.3125s static gap).
    q_combo_n: i64,
    q_last_cast: f64,
    /// The first combo's completion, gating Wind Slash's timing.
    q_burst_recorded: bool,
    q_burst_done_at: f64,
    w_ready: f64,
    r_active: bool,
    /// INF once fired.
    r_bonus_at: f64,
    r_end_at: f64,
    wind_slash_avail_at: f64,
    wind_slash_used: bool,
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

    /// A Runic Blade Charge is generated or refreshed by an ability cast.
    fn add_charge(&mut self, t: f64) {
        self.s.charges = imin(self.s.charges + 1, self.p_max_stacks);
        self.s.charge_until = t + self.p_stack_duration;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_pct_level = kit.at_level("gen.P.damagePctByLevel", level)?;
        let p_max_stacks = kit.num("gen.P.maxStacks")? as i64;
        let p_stack_duration = kit.num("gen.P.stackDurationS")?;

        let q_cd = kit.at_rank("abilities.Q.cooldownS", ranks.q)?;
        let q_intercast_s = kit.num("gen.Q.interCastS")?;
        let q_base = kit.at_rank("gen.Q.damage.base", ranks.q)?;
        let q_coef = kit.at_rank("gen.Q.damage.bonusAdRatio", ranks.q)?;

        let w_cd = kit.at_rank("abilities.W.cooldownS", ranks.w)?;
        let w_cast_s = kit.num("gen.W.castTimeS")?;
        let w_base = kit.at_rank("gen.W.damage.base", ranks.w)?;
        let w_coef = kit.at_rank("gen.W.damage.bonusAdRatio", ranks.w)?;

        let r_cast_s = kit.num("gen.R.castTimeS")?;
        let r_bonus_delay_s = kit.num("gen.R.bonusDelayS")?;
        let r_duration_s = kit.num("gen.R.durationS")?;
        let r_ad_pct = kit.num("gen.R.bonusAdPct")?;
        let r_bonus_ad = sheet.ad * r_ad_pct;
        let r_min_base = kit.at_rank("gen.R.minDamage.base", ranks.r)?;
        let r_min_coef = kit.at_rank("gen.R.minDamage.bonusAdRatio", ranks.r)?;
        let r_max_base = kit.at_rank("gen.R.maxDamage.base", ranks.r)?;
        let r_max_coef = kit.at_rank("gen.R.maxDamage.bonusAdRatio", ranks.r)?;
        let r_missing_cap_frac = kit.num("gen.R.missingHpCapPct")? / 100.0;

        let ad_bonus_base = sheet.ad_bonus;
        let ad_bonus_with_r = sheet.ad_bonus + r_bonus_ad;
        let ad_total_base = sheet.ad;
        let ad_total_with_r = sheet.ad + r_bonus_ad;

        let q_dmg_base = q_base + q_coef * ad_bonus_base;
        let q_dmg_with_r = q_base + q_coef * ad_bonus_with_r;
        let w_dmg_base = w_base + w_coef * ad_bonus_base;
        let w_dmg_with_r = w_base + w_coef * ad_bonus_with_r;
        let r_min_dmg = r_min_base + r_min_coef * ad_bonus_with_r;
        let r_max_dmg = r_max_base + r_max_coef * ad_bonus_with_r;
        let p_dmg_base = ad_total_base * p_pct_level;
        let p_dmg_with_r = ad_total_with_r * p_pct_level;

        let q_burst_done_at0 = if ranks.q > 0 { INF } else { 0.0 };

        let state = State {
            busy_until: 0.0,
            charges: 0,
            charge_until: 0.0,
            q_combo_n: 0,
            q_last_cast: 0.0,
            q_burst_recorded: false,
            q_burst_done_at: q_burst_done_at0,
            w_ready: 0.0,
            r_active: false,
            r_bonus_at: INF,
            r_end_at: INF,
            wind_slash_avail_at: INF,
            wind_slash_used: ranks.r == 0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("riven kit needs attack.windupFraction")?,
            p_pct_level,
            p_max_stacks,
            p_stack_duration,
            src_p: intern("P onhit"),
            p_dmg_base,
            p_dmg_with_r,
            q_cd,
            q_intercast_s,
            q_dmg_base,
            q_dmg_with_r,
            w_cd,
            w_cast_s,
            w_dmg_base,
            w_dmg_with_r,
            r_cast_s,
            r_bonus_delay_s,
            r_duration_s,
            r_bonus_ad,
            r_min_dmg,
            r_max_dmg,
            r_missing_cap_frac,
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

    fn bonus_as(&self, _t: f64) -> f64 {
        0.0
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        e.p.ad + if self.s.r_active { self.r_bonus_ad } else { 0.0 }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // Runic Blade: consume a Charge for bonus on-hit damage, once per
        // attack (the wiki notes it is not a generic on-hit proc).
        let t = e.st.t;
        if self.s.charges > 0 && t >= self.s.charge_until {
            self.s.charges = 0;
        }
        if self.s.charges > 0 {
            self.s.charges -= 1;
            self.s.charge_until = t + self.p_stack_duration;
            let dmg = if self.s.r_active { self.p_dmg_with_r } else { self.p_dmg_base };
            e.deal(dmg, DType::Physical, self.src_p, true, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.q_combo_n > 0 {
            self.castable_at(e, self.s.q_last_cast + self.q_intercast_s)
        } else {
            self.castable_at(e, e.st.q_ready)
        }
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.q_combo_n == 0 {
            // the main cooldown starts on the first sub-cast
            e.st.q_ready = t + e.basic_cd(self.q_cd);
        }
        let dmg = if self.s.r_active { self.q_dmg_with_r } else { self.q_dmg_base };
        e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.add_charge(t);
        self.s.q_combo_n += 1;
        self.s.q_last_cast = t;
        // resets Riven's attack timer and orders an attack on the target
        let b = self.bonus_as(t);
        e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        if self.s.q_combo_n >= 3 {
            if !self.s.q_burst_recorded {
                self.s.q_burst_recorded = true;
                self.s.q_burst_done_at = t;
            }
            self.s.q_combo_n = 0;
        }
        // Broken Wings has no cast time: no other cast waits for it, but the
        // dash still holds the next attack back (already re-set above).
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: its 0.25 s cast time keeps Riven busy before Q's
        // combo (or anything else) can start
        let t = e.st.t;
        self.s.r_bonus_at = t + self.r_bonus_delay_s;
        self.s.r_end_at = t + self.r_duration_s;
        self.s.wind_slash_avail_at = t + self.r_bonus_delay_s;
        self.add_charge(t);
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_bonus_at != INF {
                out[n] = (self.s.r_bonus_at, Kind::Ev(EV_R_BONUS));
                n += 1;
            }
            if self.s.r_end_at != INF {
                out[n] = (self.s.r_end_at, Kind::Ev(EV_R_END));
                n += 1;
            }
            if !self.s.wind_slash_used {
                let gate = pymax(self.s.wind_slash_avail_at, self.s.q_burst_done_at);
                out[n] = (self.castable_at(e, gate), Kind::Ev(EV_WINDSLASH));
                n += 1;
            }
        }
        if self.s.charges > 0 {
            out[n] = (self.s.charge_until, Kind::Ev(EV_CHARGE_DECAY));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                let dmg = if self.s.r_active { self.w_dmg_with_r } else { self.w_dmg_base };
                e.deal(dmg, DType::Physical, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.add_charge(t);
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.busy_for(e, self.w_cast_s);
            }
            Kind::Ev(EV_R_BONUS) => {
                self.s.r_active = true;
                self.s.r_bonus_at = INF;
            }
            Kind::Ev(EV_R_END) => {
                self.s.r_active = false;
                self.s.r_end_at = INF;
            }
            Kind::Ev(EV_WINDSLASH) => {
                self.s.wind_slash_used = true;
                let missing = pymax(e.target_hp - pymax(e.st.hp, 0.0), 0.0);
                let frac = missing / e.target_hp;
                let ratio = pymin(frac / self.r_missing_cap_frac, 1.0);
                let dmg = self.r_min_dmg + (self.r_max_dmg - self.r_min_dmg) * ratio;
                e.deal(dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.add_charge(t);
                e.ult_hatefog();
                e.lockout();
            }
            Kind::Ev(EV_CHARGE_DECAY) => {
                self.s.charges = 0;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
