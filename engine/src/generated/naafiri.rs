//! Naafiri. Packmates (P/W's extra summons/R's per-Packmate hits) are other
//! units and do nothing in a solo fight. The rotation opens with The Call of
//! the Pack (W) for its +20% total AD hunt buff, then Hounds' Pursuit (R)
//! once its channel can start, then loops Eviscerate (E) and Darkin Daggers
//! (Q, always recast on the still-bleeding target as soon as allowed) on
//! cooldown, attacking between casts.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const EV_W_CAST: u8 = 0;
const EV_R_CAST: u8 = 1;
const EV_R_HIT: u8 = 2;
const EV_E_CAST: u8 = 3;
const EV_Q_TICK: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_cd: f64,
    q_cast_time: f64,
    q_recast_lockout: f64,
    q_init_static: f64,
    q_init_ratio: f64,
    q_bleed_static: f64,
    q_bleed_ratio: f64,
    q_bleed_interval: f64,
    q_bleed_ticks_total: i64,
    q_second_base: f64,
    q_second_ratio: f64,
    q_second_missing_base_mult: f64,
    q_second_missing_ad_mult: f64,

    w_cd: f64,
    w_cast_time: f64,
    w_hunt_duration: f64,
    hunt_bonus_ad: f64,

    e_cd: f64,
    e_dash_static: f64,
    e_dash_ratio: f64,
    e_flurry_static: f64,
    e_flurry_ratio: f64,

    r_cd: f64,
    r_channel_time: f64,
    r_static: f64,
    r_ratio: f64,

    src_q_bleed: SourceId,
    src_q_bonus: SourceId,
    src_e_flurry: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    e_ready: f64,
    r_ready: f64,
    /// Earliest time Naafiri is free to start her next cast (a cast/channel
    /// with a duration longer than the standard 0.25 s lockout holds this).
    busy_until: f64,
    hunt_active_from: f64,
    hunt_until: f64,
    r_channel_pending: bool,
    r_hit_at: f64,
    q_awaiting_recast: bool,
    q_recast_earliest: f64,
    q_bleeding: bool,
    q_apply_t: f64,
    q_total_bleed: f64,
    q_per_tick: f64,
    q_ticks_done: i64,
}

impl GenDriver {
    /// The Call of the Pack's +20% total AD, as extra bonus AD, while the
    /// hunt buff is active (from the end of W's cast to 5 s after its start).
    fn hunt_extra(&self, t: f64) -> f64 {
        if t >= self.s.hunt_active_from && t < self.s.hunt_until {
            self.hunt_bonus_ad
        } else {
            0.0
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            w_ready: 0.0,
            e_ready: 0.0,
            r_ready: 0.0,
            busy_until: 0.0,
            hunt_active_from: 0.0,
            hunt_until: -1.0,
            r_channel_pending: false,
            r_hit_at: INF,
            q_awaiting_recast: false,
            q_recast_earliest: 0.0,
            q_bleeding: false,
            q_apply_t: 0.0,
            q_total_bleed: 0.0,
            q_per_tick: 0.0,
            q_ticks_done: 0,
        };
        let w_ad_ratio = kit.num("gen.W.adRatioOfTotalAd")?;
        let bleed_duration = kit.num("gen.Q.bleed.durationS")?;
        let bleed_interval = kit.num("gen.Q.bleed.intervalS")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("naafiri kit needs attack.windupFraction")?,

            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_time: kit.num("gen.Q.castTimeS")?,
            q_recast_lockout: kit.num("gen.Q.recastLockoutS")?,
            q_init_static: kit.hit("gen.Q.initial", ranks.q, sheet)?,
            q_init_ratio: kit.num("gen.Q.initial.bonusAdRatio")?,
            q_bleed_static: kit.hit("gen.Q.bleed", ranks.q, sheet)?,
            q_bleed_ratio: kit.num("gen.Q.bleed.bonusAdRatio")?,
            q_bleed_interval: bleed_interval,
            q_bleed_ticks_total: (bleed_duration / bleed_interval) as i64,
            q_second_base: kit.at_rank("gen.Q.secondCast.base", ranks.q)?,
            q_second_ratio: kit.num("gen.Q.secondCast.bonusAdRatio")?,
            q_second_missing_base_mult: kit.num("gen.Q.secondCast.missingHpBaseMult")?,
            q_second_missing_ad_mult: kit.num("gen.Q.secondCast.missingHpAdMult")?,

            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_time: kit.num("gen.W.castTimeS")?,
            w_hunt_duration: kit.num("gen.W.huntDurationS")?,
            hunt_bonus_ad: w_ad_ratio * sheet.ad,

            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_dash_static: kit.hit("gen.E.dash", ranks.e, sheet)?,
            e_dash_ratio: kit.num("gen.E.dash.bonusAdRatio")?,
            e_flurry_static: kit.hit("gen.E.flurry", ranks.e, sheet)?,
            e_flurry_ratio: kit.num("gen.E.flurry.bonusAdRatio")?,

            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_channel_time: kit.num("gen.R.channelS")?,
            r_static: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_ratio: kit.num("gen.R.damage.bonusAdRatio")?,

            src_q_bleed: intern("Q bleed"),
            src_q_bonus: intern("Q bonus"),
            src_e_flurry: intern("E flurry"),

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
        e.p.ad + self.hunt_extra(e.st.t)
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.q_awaiting_recast {
            pymax(self.s.q_recast_earliest, e.st.t)
        } else {
            pymax(e.st.q_ready, e.st.t)
        }
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if !self.s.q_awaiting_recast {
            // Initial cast: the physical hit and the bleed it applies.
            let extra = self.hunt_extra(t);
            let init_dmg = self.q_init_static + self.q_init_ratio * extra;
            e.deal(init_dmg, DType::Physical, SRC_Q, false, true, 1.0);
            let total_bleed = self.q_bleed_static + self.q_bleed_ratio * extra;
            self.s.q_bleeding = true;
            self.s.q_apply_t = t;
            self.s.q_total_bleed = total_bleed;
            self.s.q_per_tick = total_bleed / (self.q_bleed_ticks_total as f64);
            self.s.q_ticks_done = 0;
            self.s.q_awaiting_recast = true;
            self.s.q_recast_earliest = t + self.q_recast_lockout;
            self.s.busy_until = t + self.q_cast_time;
            e.ability_cast_proc();
            e.eclipse_hit();
            e.prime_spellblade();
            e.lockout();
        } else {
            // Recast on the still-bleeding target: remaining bleed damage,
            // delivered all at once, plus a bonus scaled by missing health
            // (computed before the bleed damage lands).
            let hp_now = pymax(e.st.hp, 0.0);
            let missing_frac = pymin(pymax((e.target_hp - hp_now) / e.target_hp, 0.0), 1.0);
            let delivered = (self.s.q_ticks_done as f64) * self.s.q_per_tick;
            let remaining = pymax(self.s.q_total_bleed - delivered, 0.0);
            let extra = self.hunt_extra(t);
            let bonus_ad_now = e.p.sheet.ad_bonus + extra;
            let bonus_dmg = self.q_second_base * (1.0 + self.q_second_missing_base_mult * missing_frac)
                + self.q_second_ratio * bonus_ad_now * (1.0 + self.q_second_missing_ad_mult * missing_frac);
            e.deal(remaining, DType::Physical, self.src_q_bleed, false, true, 1.0);
            e.deal(bonus_dmg, DType::Physical, self.src_q_bonus, false, true, 1.0);
            self.s.q_bleeding = false;
            self.s.q_awaiting_recast = false;
            e.st.q_ready = t + e.basic_cd(self.q_cd);
            self.s.busy_until = t + self.q_cast_time;
            e.ability_cast_proc();
            e.eclipse_hit();
            e.prime_spellblade();
            e.lockout();
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            let ready = pymax(self.s.w_ready, pymax(self.s.busy_until, e.st.t));
            out[n] = (ready, Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_channel_pending {
                out[n] = (self.s.r_hit_at, Kind::Ev(EV_R_HIT));
            } else {
                let ready = pymax(self.s.r_ready, pymax(self.s.busy_until, e.st.t));
                out[n] = (ready, Kind::Ev(EV_R_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            let ready = pymax(self.s.e_ready, pymax(self.s.busy_until, e.st.t));
            out[n] = (ready, Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.q_bleeding && self.s.q_ticks_done < self.q_bleed_ticks_total {
            let tick_time = self.s.q_apply_t + ((self.s.q_ticks_done + 1) as f64) * self.q_bleed_interval;
            out[n] = (tick_time, Kind::Ev(EV_Q_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                // No direct damage; grants the hunt buff, cooldown starts on-cast.
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.busy_until = t + self.w_cast_time;
                e.st.next_attack = pymax(e.st.next_attack, t + self.w_cast_time);
                self.s.hunt_active_from = t + self.w_cast_time;
                self.s.hunt_until = t + self.w_hunt_duration;
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_CAST) => {
                // Channel start: no damage yet, cooldown starts on-cast.
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                self.s.busy_until = t + self.r_channel_time;
                e.st.next_attack = pymax(e.st.next_attack, t + self.r_channel_time);
                self.s.r_channel_pending = true;
                self.s.r_hit_at = t + self.r_channel_time;
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_HIT) => {
                self.s.r_channel_pending = false;
                self.s.r_hit_at = INF;
                let extra = self.hunt_extra(t);
                let dmg = self.r_static + self.r_ratio * extra;
                e.deal(dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                let extra = self.hunt_extra(t);
                let dash = self.e_dash_static + self.e_dash_ratio * extra;
                let flurry = self.e_flurry_static + self.e_flurry_ratio * extra;
                e.deal(dash, DType::Physical, SRC_E, false, true, 1.0);
                e.deal(flurry, DType::Physical, self.src_e_flurry, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_Q_TICK) => {
                let dmg = self.s.q_per_tick;
                self.s.q_ticks_done += 1;
                e.deal(dmg, DType::Physical, self.src_q_bleed, false, true, 1.0);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
