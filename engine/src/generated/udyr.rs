//! Udyr. No true ultimate: Q/W/E/R are four interchangeable Stances sharing a
//! 1.5s global cooldown; the opening t=0 hook casts Storm Stance (R) so its
//! blizzard starts immediately, then Q, W and E are woven in 1.5s apart so
//! every ability's own 6s cooldown lines up with a repeating (R, Q, W, E)
//! cycle. Only Wilding Claw (Q) is ever Awaken-recast, since it is the only
//! Stance whose Awaken effect deals damage worth the shared Awakened Spirit
//! charge. Monk Training's attack-speed buff is approximated as a flat 4s
//! window refreshed by every Stance cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const EV_W_CAST: u8 = 0;
const EV_E_CAST: u8 = 1;
const EV_R_CAST: u8 = 2;
const EV_Q_AWAKEN: u8 = 3;
const EV_R_TICK: u8 = 4;
const EV_LIGHTNING_TICK: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,

    gcd_s: f64,
    monk_as_pct: f64,
    monk_dur: f64,
    awaken_delay: f64,
    awaken_window: f64,
    awaken_cd_base: f64,

    q_cd: f64,
    w_cd: f64,
    e_cd: f64,
    r_cd: f64,

    q1_pct: f64,
    q4_dmg: f64,
    q_as_pct: f64,
    q_as_dur: f64,
    q_charge_count: i64,

    q_awaken_as_pct: f64,
    q_awaken_as_dur: f64,
    q6_pct: f64,
    lightning_pct: f64,
    lightning_bounces: i64,
    lightning_interval: f64,

    r_onhit_dmg: f64,
    r_blizzard_dmg: f64,
    r_tick_interval: f64,
    r_ticks: i64,
    r_charge_count: i64,

    src_q4: SourceId,
    src_q_awaken: SourceId,
    src_q_lightning: SourceId,
    src_r_onhit: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    gcd_until: f64,

    w_ready: f64,
    e_ready: f64,
    r_ready: f64,

    q_cast_t: f64,
    awaken_used_for_this_q: bool,
    awaken_ready: f64,

    monk_until: f64,
    q4_until: f64,
    q_as_until: f64,
    q_awaken_as_until: f64,

    q_charges: i64,
    q_awaken_pending: bool,
    q_lightning_charges: i64,
    r_charges: i64,

    light_remaining: i64,
    light_next: f64,

    r_tick_remaining: i64,
    r_tick_next: f64,
}

impl GenDriver {
    fn do_r_cast(&mut self, e: &mut Engine, initial: bool) {
        let t = e.st.t;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.s.gcd_until = t + self.gcd_s;
        self.s.monk_until = t + self.monk_dur;
        self.s.r_charges = self.r_charge_count;
        self.s.r_tick_remaining = self.r_ticks;
        self.s.r_tick_next = t + self.r_tick_interval;
        e.prime_spellblade();
        e.ability_cast_proc();
        e.eclipse_hit();
        let _ = initial;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let bonus_ad = sheet.ad_bonus;
        let bonus_hp = sheet.hp_bonus;
        let ap = sheet.ap;

        let q1_pct = if ranks.q > 0 {
            kit.at_rank("gen.Q.onHit1.baseByRankPct", ranks.q)?
                + kit.num("gen.Q.onHit1.adPctPer100")? * (bonus_ad / 100.0)
        } else {
            0.0
        };

        let onhit_flat_base = kit.at_rank("gen.Q.onHitFlat.base", ranks.q)?;
        let onhit_flat_adratio = kit.num("gen.Q.onHitFlat.adRatio")?;
        let onhit_flat_hpratiopct = kit.at_rank("gen.Q.onHitFlat.bonusHpRatioPctByRank", ranks.q)?;
        let q4_dmg = onhit_flat_base + onhit_flat_adratio * bonus_ad
            + onhit_flat_hpratiopct / 100.0 * bonus_hp;

        let q_as_pct = kit.at_rank("gen.Q.asBuff.baseByRankPct", ranks.q)?;
        let q_as_dur = kit.num("gen.Q.asBuff.durationS")?;
        let q_charge_count = kit.num("gen.Q.chargeCount")? as i64;

        let q_awaken_as_pct = kit.at_level("gen.Q.awaken.asBonusByLevelPct", level)?;
        let q_awaken_as_dur = kit.num("gen.Q.awaken.asBuffDurationS")?;
        let q6_pct = kit.at_level("gen.Q.awaken.maxHpBonusByLevelPct", level)?
            + kit.num("gen.Q.awaken.adPctPer100")? * (bonus_ad / 100.0)
            + kit.num("gen.Q.awaken.hpPctPer100")? * (bonus_hp / 100.0);

        let lightning_pct = kit.at_level("gen.Q.awaken.lightning.perHitByLevelPct", level)?
            + kit.num("gen.Q.awaken.lightning.apPctPer100")? * (ap / 100.0);
        let lightning_bounces = kit.num("gen.Q.awaken.lightning.bounces")? as i64;
        let lightning_interval = kit.num("gen.Q.awaken.lightning.bounceIntervalS")?;

        let r_onhit_dmg = if ranks.r > 0 {
            kit.at_level("gen.R.onHitByLevel", level)? + kit.num("gen.R.onHitApRatio")? * ap
        } else {
            0.0
        };
        let r_blizzard_dmg = kit.hit("gen.R.blizzard.damage", ranks.r, sheet)?;
        let r_tick_interval = kit.num("gen.R.blizzard.tickIntervalS")?;
        let r_ticks = kit.num("gen.R.blizzard.ticks")? as i64;
        let r_charge_count = kit.num("gen.R.chargeCount")? as i64;

        let q_cd = kit.at_rank("abilities.Q.cooldownS", ranks.q)?;
        let w_cd = kit.at_rank("abilities.W.cooldownS", ranks.w)?;
        let e_cd = kit.at_rank("abilities.E.cooldownS", ranks.e)?;
        let r_cd = kit.at_rank("abilities.R.cooldownS", ranks.r)?;

        let gcd_s = kit.num("gen.P.globalCdS")?;
        let monk_as_pct = kit.num("gen.P.monkAsPct")?;
        let monk_dur = kit.num("gen.P.monkDurationS")?;
        let awaken_delay = kit.num("gen.P.awakenCastDelayS")?;
        let awaken_window = kit.num("gen.P.awakenWindowS")?;
        let awaken_cd_base = kit.at_level("gen.P.awakenCdByLevel", level)?;

        let state = State {
            gcd_until: 0.0,
            w_ready: 0.0,
            e_ready: 0.0,
            r_ready: 0.0,
            q_cast_t: -INF,
            awaken_used_for_this_q: true,
            awaken_ready: 0.0,
            monk_until: -INF,
            q4_until: -INF,
            q_as_until: -INF,
            q_awaken_as_until: -INF,
            q_charges: 0,
            q_awaken_pending: false,
            q_lightning_charges: 0,
            r_charges: 0,
            light_remaining: 0,
            light_next: INF,
            r_tick_remaining: 0,
            r_tick_next: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            gcd_s,
            monk_as_pct,
            monk_dur,
            awaken_delay,
            awaken_window,
            awaken_cd_base,
            q_cd,
            w_cd,
            e_cd,
            r_cd,
            q1_pct,
            q4_dmg,
            q_as_pct,
            q_as_dur,
            q_charge_count,
            q_awaken_as_pct,
            q_awaken_as_dur,
            q6_pct,
            lightning_pct,
            lightning_bounces,
            lightning_interval,
            r_onhit_dmg,
            r_blizzard_dmg,
            r_tick_interval,
            r_ticks,
            r_charge_count,
            src_q4: intern("Q onhit"),
            src_q_awaken: intern("Q awakened onhit"),
            src_q_lightning: intern("Q lightning"),
            src_r_onhit: intern("R onhit"),
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
        let mut b = 0.0;
        if t < self.s.monk_until {
            b += self.monk_as_pct;
        }
        if t < self.s.q_as_until {
            b += self.q_as_pct;
        }
        if t < self.s.q_awaken_as_until {
            b += self.q_awaken_as_pct;
        }
        b
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.q_charges > 0 {
            self.s.q_charges -= 1;
            e.deal(self.q1_pct / 100.0 * e.target_hp, DType::Physical, SRC_Q, false, false, 1.0);
            if self.s.q_awaken_pending {
                e.deal(self.q6_pct / 100.0 * e.target_hp, DType::Physical, self.src_q_awaken, false, false, 1.0);
                if self.s.q_charges == 0 {
                    self.s.q_awaken_pending = false;
                }
            }
        }
        if t < self.s.q4_until {
            e.deal(self.q4_dmg, DType::Physical, self.src_q4, false, false, 1.0);
        }
        if self.s.r_charges > 0 {
            self.s.r_charges -= 1;
            e.deal(self.r_onhit_dmg, DType::Magic, self.src_r_onhit, false, false, 1.0);
        }
        if self.s.q_lightning_charges > 0 {
            self.s.q_lightning_charges -= 1;
            if self.s.light_remaining == 0 {
                self.s.light_next = t;
            }
            self.s.light_remaining += self.lightning_bounces;
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, pymax(self.s.gcd_until, e.st.t))
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.gcd_until = t + self.gcd_s;
        self.s.monk_until = t + self.monk_dur;
        self.s.q4_until = t + self.q_as_dur;
        self.s.q_as_until = t + self.q_as_dur;
        self.s.q_charges = self.q_charge_count;
        self.s.q_cast_t = t;
        self.s.awaken_used_for_this_q = false;
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.do_r_cast(e, true);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        let t = e.st.t;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, pymax(self.s.gcd_until, t)), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, pymax(self.s.gcd_until, t)), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            out[n] = (pymax(self.s.r_ready, pymax(self.s.gcd_until, t)), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        if self.ranks.q > 0 && !self.s.awaken_used_for_this_q && self.s.q_cast_t > -INF {
            let check_t = pymax(self.s.q_cast_t + self.awaken_delay, self.s.awaken_ready);
            if check_t <= self.s.q_cast_t + self.awaken_window {
                out[n] = (check_t, Kind::Ev(EV_Q_AWAKEN));
                n += 1;
            }
        }
        if self.s.r_tick_remaining > 0 {
            out[n] = (self.s.r_tick_next, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        if self.s.light_remaining > 0 {
            out[n] = (self.s.light_next, Kind::Ev(EV_LIGHTNING_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.gcd_until = t + self.gcd_s;
                self.s.monk_until = t + self.monk_dur;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.gcd_until = t + self.gcd_s;
                self.s.monk_until = t + self.monk_dur;
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_CAST) => {
                self.do_r_cast(e, false);
            }
            Kind::Ev(EV_Q_AWAKEN) => {
                self.s.awaken_used_for_this_q = true;
                if self.s.awaken_ready <= t {
                    self.s.awaken_ready = t + e.ult_cd(self.awaken_cd_base);
                    self.s.q_awaken_as_until = t + self.q_awaken_as_dur;
                    self.s.q_awaken_pending = true;
                    self.s.q_lightning_charges = self.q_charge_count;
                    e.prime_spellblade();
                }
            }
            Kind::Ev(EV_R_TICK) => {
                e.deal(self.r_blizzard_dmg, DType::Magic, SRC_R, false, true, 1.0);
                self.s.r_tick_remaining -= 1;
                if self.s.r_tick_remaining > 0 {
                    self.s.r_tick_next = t + self.r_tick_interval;
                } else {
                    self.s.r_tick_next = INF;
                }
            }
            Kind::Ev(EV_LIGHTNING_TICK) => {
                e.deal(self.lightning_pct / 100.0 * e.target_hp, DType::Magic, self.src_q_lightning, false, true, 1.0);
                self.s.light_remaining -= 1;
                if self.s.light_remaining > 0 {
                    self.s.light_next = t + self.lightning_interval;
                } else {
                    self.s.light_next = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
