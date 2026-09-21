//! Aphelios. Main weapon cycles Calibrum -> Gravitum -> Infernum -> Crescendum
//! as Moonlight is spent by attacks and Q casts (off-hand modeled as fixed
//! Severum for the fight, see notes); Q's effect follows the current main
//! weapon, and Moonlight Vigil opens the fight while Calibrum is main. Casts
//! go one at a time: every Q cast and the R cast keep Aphelios busy for their
//! own cast time before any other cast or attack starts.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const CALIBRUM: i64 = 0;
const GRAVITUM: i64 = 1;
const INFERNUM: i64 = 2;
const CRESCENDUM: i64 = 3;

/// Moonlight Vigil's delayed smite+sky-attack; Duskwave's delayed off-hand
/// volley; Sentry's autonomous attack ticks.
const EV_R_SWING: u8 = 0;
const EV_DUSKWAVE_VOLLEY: u8 = 1;
const EV_SENTRY_TICK: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    p_ad_bonus: f64,
    p_as_bonus_pct: f64,
    infernum_mult: f64,
    ammo_cap: f64,
    ammo_per_q: f64,
    assembly_time_s: f64,
    q_lockout_s: f64,
    calibrum_mark_dur_s: f64,
    calibrum_snipe_base: f64,
    calibrum_snipe_adratio: f64,
    gravitum_slow_dur_s: f64,
    duskwave_volley_delay_s: f64,
    sentry_arm_delay_s: f64,
    sentry_active_dur_s: f64,
    q_cd_calibrum: f64,
    q_cd_gravitum: f64,
    q_cd_infernum: f64,
    q_cd_crescendum: f64,
    q_dmg_calibrum: f64,
    q_dmg_gravitum: f64,
    q_dmg_infernum: f64,
    q_dmg_crescendum: f64,
    /// Cast times: shared by Moonshot and Duskwave (both 0.4 s); the other
    /// two weapons' Qs have their own cast time.
    q_cast_s: f64,
    q_cast_gravitum_s: f64,
    q_cast_crescendum_s: f64,
    r_cast_s: f64,
    r_smite_dmg: f64,
    r_calibrum_mark_bonus: f64,
    src_calibrum_snipe: SourceId,
    src_duskwave_volley: SourceId,
    src_sentry_tick: SourceId,
    src_r_sky: SourceId,
    src_r_mark: SourceId,
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    main_idx: i64,
    main_ammo: f64,
    weapon_lock_until: f64,
    q_lock_until: f64,
    gravitum_slow_until: f64,
    mark_active: bool,
    mark_deadline: f64,
    volley_pending: bool,
    volley_time: f64,
    sentry_active: bool,
    sentry_end: f64,
    sentry_next_tick: f64,
    r_swing_at: f64,
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
}

impl GenDriver {
    fn spend_ammo(&mut self, e: &mut Engine, amount: f64) {
        self.s.main_ammo -= amount;
        if self.s.main_ammo <= 0.0 {
            let t = e.st.t;
            self.s.main_idx = (self.s.main_idx + 1) % 4;
            self.s.main_ammo = self.ammo_cap;
            self.s.weapon_lock_until = t + self.assembly_time_s;
            self.s.q_lock_until = t + self.q_lockout_s;
        }
    }

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
        let cap = kit.num("gen.P.maxStacks")?;
        let cap_i = cap as i64;
        let ad_stacks = imin(cap_i, level);
        let as_stacks = imin(cap_i, imax(0, level - cap_i));
        let p_ad_bonus = ad_stacks as f64 * kit.num("gen.P.adPerRank")?;
        let p_as_bonus_pct = as_stacks as f64 * kit.num("gen.P.asPerRank")? * 100.0;
        let bonus_ad_total = sheet.ad_bonus + p_ad_bonus;

        let q_dmg_calibrum = kit.at_level("gen.Q.calibrum.base", level)?
            + kit.at_level("gen.Q.calibrum.bonusAdRatio", level)? * bonus_ad_total
            + kit.num("gen.Q.calibrum.apRatio")? * sheet.ap;
        let q_dmg_gravitum = kit.at_level("gen.Q.gravitum.base", level)?
            + kit.at_level("gen.Q.gravitum.bonusAdRatio", level)? * bonus_ad_total
            + kit.num("gen.Q.gravitum.apRatio")? * sheet.ap;
        let q_dmg_infernum = kit.at_level("gen.Q.infernum.base", level)?
            + kit.at_level("gen.Q.infernum.bonusAdRatio", level)? * bonus_ad_total
            + kit.num("gen.Q.infernum.apRatio")? * sheet.ap;
        let q_dmg_crescendum = kit.at_level("gen.Q.crescendum.base", level)?
            + kit.at_level("gen.Q.crescendum.bonusAdRatio", level)? * bonus_ad_total
            + kit.num("gen.Q.crescendum.apRatio")? * sheet.ap;

        let q_cd_calibrum = kit.at_level("abilities.Q.cooldownS", level)?;
        let q_cd_gravitum = kit.at_level("gen.Q.gravitumCooldownS", level)?;
        let q_cd_infernum = kit.at_level("gen.Q.infernumCooldownS", level)?;
        let q_cd_crescendum = kit.at_level("gen.Q.crescendumCooldownS", level)?;

        let r_smite_dmg = kit.at_rank("gen.R.smite.base", ranks.r)?
            + kit.num("gen.R.smite.bonusAdRatio")? * bonus_ad_total
            + kit.num("gen.R.smite.apRatio")? * sheet.ap;
        let r_calibrum_mark_bonus = kit.at_rank("gen.R.calibrumMarkBonus", ranks.r)?;

        let state = State {
            main_idx: CALIBRUM,
            main_ammo: kit.num("gen.P.ammoCapacity")?,
            weapon_lock_until: 0.0,
            q_lock_until: 0.0,
            gravitum_slow_until: -INF,
            mark_active: false,
            mark_deadline: 0.0,
            volley_pending: false,
            volley_time: INF,
            sentry_active: false,
            sentry_end: 0.0,
            sentry_next_tick: INF,
            r_swing_at: INF,
            busy_until: 0.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("aphelios kit needs attack.windupFraction")?,
            p_ad_bonus,
            p_as_bonus_pct,
            infernum_mult: kit.num("gen.P.infernumAttackMultiplier")?,
            ammo_cap: kit.num("gen.P.ammoCapacity")?,
            ammo_per_q: kit.num("gen.P.ammoPerQCast")?,
            assembly_time_s: kit.num("gen.P.assemblyTimeS")?,
            q_lockout_s: kit.num("gen.P.qLockoutS")?,
            calibrum_mark_dur_s: kit.num("gen.P.calibrumMarkDurationS")?,
            calibrum_snipe_base: kit.num("gen.P.calibrumSnipe.base")?,
            calibrum_snipe_adratio: kit.num("gen.P.calibrumSnipe.bonusAdRatio")?,
            gravitum_slow_dur_s: kit.num("gen.Q.gravitumSlowDurationS")?,
            duskwave_volley_delay_s: kit.num("gen.Q.duskwaveVolleyDelayS")?,
            sentry_arm_delay_s: kit.num("gen.Q.sentryArmDelayS")?,
            sentry_active_dur_s: kit.num("gen.Q.sentryActiveDurationS")?,
            q_cd_calibrum,
            q_cd_gravitum,
            q_cd_infernum,
            q_cd_crescendum,
            q_dmg_calibrum,
            q_dmg_gravitum,
            q_dmg_infernum,
            q_dmg_crescendum,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_cast_gravitum_s: kit.num("gen.Q.castTimeGravitumS")?,
            q_cast_crescendum_s: kit.num("gen.Q.castTimeCrescendumS")?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_smite_dmg,
            r_calibrum_mark_bonus,
            src_calibrum_snipe: intern("P calibrum snipe"),
            src_duskwave_volley: intern("Q duskwave volley"),
            src_sentry_tick: intern("Q sentry tick"),
            src_r_sky: intern("R sky attack"),
            src_r_mark: intern("R mark bonus"),
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
        self.p_as_bonus_pct
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        let mut ad = e.p.ad + self.p_ad_bonus;
        if self.s.main_idx == INFERNUM {
            ad = ad * self.infernum_mult;
        }
        ad
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.q_lock_until, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.main_idx == GRAVITUM {
            self.s.gravitum_slow_until = t + self.gravitum_slow_dur_s;
        }
        self.spend_ammo(e, 1.0);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.s.main_idx == CALIBRUM && self.s.mark_active {
            let t = e.st.t;
            if t <= self.s.mark_deadline {
                let ad_now = e.p.ad + self.p_ad_bonus;
                let bonus = self.calibrum_snipe_base + self.calibrum_snipe_adratio * ad_now;
                e.deal(bonus, DType::Physical, self.src_calibrum_snipe, false, false, 1.0);
            }
            self.s.mark_active = false;
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        let next = t + e.attack_period(b);
        e.st.next_attack = pymax(next, self.s.weapon_lock_until);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.main_idx == GRAVITUM && e.st.t >= self.s.gravitum_slow_until {
            return INF;
        }
        let ready = pymax(e.st.q_ready, pymax(self.s.q_lock_until, self.s.weapon_lock_until));
        self.castable_at(e, ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        match self.s.main_idx {
            CALIBRUM => {
                e.st.q_ready = t + e.basic_cd(self.q_cd_calibrum);
                e.deal(self.q_dmg_calibrum, DType::Physical, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.spend_ammo(e, self.ammo_per_q);
                self.s.mark_active = true;
                self.s.mark_deadline = t + self.calibrum_mark_dur_s;
                self.busy_for(e, self.q_cast_s);
            }
            GRAVITUM => {
                e.st.q_ready = t + e.basic_cd(self.q_cd_gravitum);
                e.deal(self.q_dmg_gravitum, DType::Magic, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.spend_ammo(e, self.ammo_per_q);
                self.s.gravitum_slow_until = t;
                self.busy_for(e, self.q_cast_gravitum_s);
            }
            INFERNUM => {
                e.st.q_ready = t + e.basic_cd(self.q_cd_infernum);
                e.deal(self.q_dmg_infernum, DType::Physical, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.spend_ammo(e, self.ammo_per_q);
                self.s.volley_pending = true;
                self.s.volley_time = t + self.duskwave_volley_delay_s;
                e.st.next_attack = pymax(e.st.next_attack, self.s.volley_time);
                self.busy_for(e, self.q_cast_s);
            }
            _ => {
                e.st.q_ready = t + e.basic_cd(self.q_cd_crescendum);
                e.prime_spellblade();
                self.spend_ammo(e, self.ammo_per_q);
                self.s.sentry_active = true;
                self.s.sentry_end = t + self.sentry_arm_delay_s + self.sentry_active_dur_s;
                self.s.sentry_next_tick = t + self.sentry_arm_delay_s;
                self.busy_for(e, self.q_cast_crescendum_s);
            }
        }
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_swing_at = t + self.r_cast_s;
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, _e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_swing_at != INF {
            out[n] = (self.s.r_swing_at, Kind::Ev(EV_R_SWING));
            n += 1;
        }
        if self.s.volley_pending {
            out[n] = (self.s.volley_time, Kind::Ev(EV_DUSKWAVE_VOLLEY));
            n += 1;
        }
        if self.s.sentry_active {
            out[n] = (self.s.sentry_next_tick, Kind::Ev(EV_SENTRY_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_SWING) => {
                self.s.r_swing_at = INF;
                e.deal(self.r_smite_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                let ad_now = e.p.ad + self.p_ad_bonus;
                e.deal(ad_now, DType::Physical, self.src_r_sky, true, false, 1.0);
                e.deal(self.r_calibrum_mark_bonus, DType::Physical, self.src_r_mark, false, false, 1.0);
                e.ult_hatefog();
            }
            Kind::Ev(EV_DUSKWAVE_VOLLEY) => {
                self.s.volley_pending = false;
                self.s.volley_time = INF;
                let ad_now = e.p.ad + self.p_ad_bonus;
                e.deal(ad_now, DType::Physical, self.src_duskwave_volley, true, false, 1.0);
            }
            Kind::Ev(EV_SENTRY_TICK) => {
                if t >= self.s.sentry_end {
                    self.s.sentry_active = false;
                } else {
                    e.deal(self.q_dmg_crescendum, DType::Physical, self.src_sentry_tick, true, false, 1.0);
                    let b = self.bonus_as(t);
                    let mut next = t + e.attack_period(b);
                    if next >= self.s.sentry_end {
                        next = self.s.sentry_end;
                        self.s.sentry_active = false;
                    }
                    self.s.sentry_next_tick = next;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
