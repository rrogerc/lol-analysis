//! Yasuo. Attacks between casts and fires Steel Tempest on cooldown, tracking
//! Gathering Storm so every third landed Q becomes the knock-up whirlwind;
//! Q's damage lands with the cast, then its cast time keeps Yasuo busy. That
//! knock-up is immediately followed by Last Breath (using Yasuo's own
//! whirlwind as the airborne source) when it is off cooldown, and Sweeping
//! Blade goes out whenever its per-target cooldown allows. Wind Wall is
//! never cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Last Breath's damage, which lands when its knock-up channel ends.
const EV_R_HIT: u8 = 0;
/// Sweeping Blade, gated by its per-target cooldown.
const EV_E_CAST: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Bonus AD from Way of the Wanderer's excess-crit-chance conversion.
    bonus_ad_from_crit: f64,
    /// Steel Tempest's expected damage (base + crit-adjusted AD ratio),
    /// identical for the thrust and the empowered whirlwind against one target.
    q_dmg: f64,
    q_cd_base: f64,
    q_cast_s: f64,
    as_cap_pct: f64,
    as_cap_frac: f64,
    gs_max: i64,
    gs_dur: f64,
    e_dmg: f64,
    e_per_target_cd: f64,
    r_dmg: f64,
    r_cd_base: f64,
    r_knockup_s: f64,
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    gs_stacks: i64,
    gs_until: f64,
    e_ready: f64,
    r_ready: f64,
    /// When Last Breath's pending damage lands (INF: none pending).
    r_hit_at: f64,
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

    /// Cast Last Breath right after a Q knock-up: its damage and attack
    /// lockout land when the knock-up channel ends.
    fn cast_r_now(&mut self, e: &mut Engine, t: f64) {
        self.s.r_ready = t + e.ult_cd(self.r_cd_base);
        self.s.r_hit_at = t + self.r_knockup_s;
        e.prime_spellblade();
        e.st.next_attack = pymax(e.st.next_attack, t + self.r_knockup_s);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let crit_damage_mod = kit.num("gen.P.critDamageMod")?;
        let ad_per_excess_crit_pct = kit.num("gen.P.adPerExcessCritPct")?;
        let doubled_crit = sheet.crit_chance * 2.0;
        let excess = pymax(doubled_crit - 100.0, 0.0);
        let bonus_ad_from_crit = excess * ad_per_excess_crit_pct;
        let eff_crit_chance = pymin(doubled_crit, 100.0);
        let crit_dmg_mult_yasuo = (sheet.crit_damage / 100.0) * crit_damage_mod;
        let mult_expected = 1.0 + (eff_crit_chance / 100.0) * (crit_dmg_mult_yasuo - 1.0);

        let q_base = kit.at_rank("gen.Q.baseDamage", ranks.q)?;
        let q_ad_ratio_frac = kit.num("gen.Q.adRatio")?;
        let ad_total_effective = sheet.ad + bonus_ad_from_crit;
        let q_ad_ratio_val = q_ad_ratio_frac * ad_total_effective;
        let q_dmg = q_base + q_ad_ratio_val * mult_expected;

        let q_cd_base = kit.at_rank("abilities.Q.cooldownS", ranks.q)?;
        let q_cast_s = kit.num("gen.Q.castTimeS")?;
        let as_cap_pct = kit.num("gen.Q.asCdCapBonusAsPct")?;
        let as_cap_frac = kit.num("gen.Q.asCdCapReductionFrac")?;
        let gs_max = kit.num("gen.Q.gatheringStormStacksMax")? as i64;
        let gs_dur = kit.num("gen.Q.gatheringStormDurationS")?;

        let e_base = kit.at_rank("gen.E.baseDamage", ranks.e)?;
        let e_ad_ratio = kit.num("gen.E.adRatio")?;
        let e_ap_ratio = kit.num("gen.E.apRatio")?;
        let e_dmg = e_base + e_ad_ratio * (sheet.ad_bonus + bonus_ad_from_crit) + e_ap_ratio * sheet.ap;
        let e_per_target_cd = kit.at_rank("gen.E.perTargetCooldownS", ranks.e)?;

        let r_base = kit.at_rank("gen.R.baseDamage", ranks.r)?;
        let r_bonus_ad_ratio = kit.num("gen.R.bonusAdRatio")?;
        let r_dmg = r_base + r_bonus_ad_ratio * (sheet.ad_bonus + bonus_ad_from_crit);
        let r_cd_base = kit.at_rank("abilities.R.cooldownS", ranks.r)?;
        let r_knockup_s = kit.num("gen.R.knockupDurationS")?;

        let state = State {
            busy_until: 0.0,
            gs_stacks: 0,
            gs_until: 0.0,
            e_ready: 0.0,
            r_ready: 0.0,
            r_hit_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("yasuo kit needs attack.windupFraction")?,
            bonus_ad_from_crit,
            q_dmg,
            q_cd_base,
            q_cast_s,
            as_cap_pct,
            as_cap_frac,
            gs_max,
            gs_dur,
            e_dmg,
            e_per_target_cd,
            r_dmg,
            r_cd_base,
            r_knockup_s,
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
        e.p.ad + self.bonus_ad_from_crit
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {}

    fn attack_riders(&mut self, _e: &mut Engine) {}

    fn after_attack(&mut self, _e: &mut Engine) {}

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        e.st.next_attack = t + e.attack_period(b);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        // Gathering Storm: stacks decay once their 6s window has passed.
        let active_stacks = if t <= self.s.gs_until { self.s.gs_stacks } else { 0 };
        let empowered = active_stacks >= self.gs_max;

        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();

        if empowered {
            // the whirlwind consumes the stacks, knocks the target up, and
            // still generates a fresh stack of its own
            self.s.gs_stacks = 1;
            self.s.gs_until = t + self.gs_dur;
            if self.ranks.r > 0 && t >= self.s.r_ready {
                self.cast_r_now(e, t);
            }
        } else {
            self.s.gs_stacks = imin(active_stacks + 1, self.gs_max);
            self.s.gs_until = t + self.gs_dur;
        }

        let bonus_as_total = e.p.sheet.bonus_as_pct + self.bonus_as(t);
        let reduction = pymin(bonus_as_total / self.as_cap_pct * self.as_cap_frac, self.as_cap_frac);
        let q_cd_eff = self.q_cd_base * (1.0 - reduction);
        e.st.q_ready = t + e.basic_cd(q_cd_eff);

        // the thrust/whirlwind lands with the cast, then its cast time keeps
        // Yasuo busy: no other cast, no attack until it ends
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, _e: &mut Engine) {
        // no airborne target exists at t = 0; R is only cast off Yasuo's own
        // Q-whirlwind knock-up, from cast_q
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_hit_at != INF {
            out[n] = (self.s.r_hit_at, Kind::Ev(EV_R_HIT));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_HIT) => {
                self.s.r_hit_at = INF;
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_E_CAST) => {
                // per-target cooldown starts on the dash; no cast time, but
                // it still cannot start inside Steel Tempest's cast
                self.s.e_ready = t + self.e_per_target_cd;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
