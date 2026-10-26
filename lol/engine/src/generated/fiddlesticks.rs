//! Fiddlesticks. A pure caster: Crowstorm opens the fight (a 1.5 s channel,
//! then 20 ticks over 5 s during which Q/W/E may still be cast), Bountiful
//! Harvest is a 2 s channel of 8 ticks that refunds cooldown on completion,
//! Reap is a plain-cast AoE, and Terrify is woven in automatically whenever
//! it is off cooldown and Fiddlesticks isn't mid-cast of something else.

use crate::fight::{Driver, Engine, Events, Kind};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Try to start Crowstorm, Bountiful Harvest or Reap (priority order).
const EV_ACT: u8 = 0;
/// Bountiful Harvest's periodic tick.
const EV_W_TICK: u8 = 1;
/// Reap's damage, at the end of its cast time.
const EV_E_HIT: u8 = 2;
/// Crowstorm's channel ends: it blinks in and starts ticking.
const EV_R_CHANNEL_END: u8 = 3;
/// Crowstorm's periodic tick.
const EV_R_TICK: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,

    q_min: f64,
    q_percent: f64,
    q_cast_time: f64,
    q_cd: f64,

    w_tick_dmg: f64,
    w_missing_ratio: f64,
    w_cast_time: f64,
    w_tick_interval: f64,
    w_refund_frac: f64,
    w_total_ticks: i64,
    w_cd: f64,

    e_dmg: f64,
    e_cast_time: f64,
    e_cd: f64,

    r_tick_dmg: f64,
    r_channel_time: f64,
    r_tick_interval: f64,
    r_total_ticks: i64,
    r_cd: f64,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Until when Fiddlesticks is mid-Terrify-cast (blocks starting another ability).
    q_cast_until: f64,

    w_ready: f64,
    /// Next Bountiful Harvest tick (INF while not channeling).
    w_tick_at: f64,
    w_tick_idx: i64,

    e_ready: f64,
    /// When Reap's damage lands (INF while none pending).
    e_hit_at: f64,

    r_ready: f64,
    /// When Crowstorm's channel completes and it blinks in (INF while none pending).
    r_channel_end_at: f64,
    /// Next Crowstorm tick (INF while not actively ticking).
    r_tick_at: f64,
    r_tick_idx: i64,
}

impl GenDriver {
    /// Fiddlesticks is busy starting or finishing a cast: no new ability may
    /// begin (Terrify still resolves via q_at's own check).
    fn busy(&self, t: f64) -> bool {
        self.s.w_tick_at != INF
            || self.s.e_hit_at != INF
            || self.s.r_channel_end_at != INF
            || t < self.s.q_cast_until
    }

    fn start_r(&mut self, e: &mut Engine, t: f64) {
        self.s.r_channel_end_at = t + self.r_channel_time;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        e.prime_spellblade();
    }

    fn start_w(&mut self, e: &mut Engine, t: f64) {
        let channel_start = t + self.w_cast_time;
        self.s.w_tick_at = channel_start + self.w_tick_interval;
        self.s.w_tick_idx = 0;
        self.s.w_ready = t + e.basic_cd(self.w_cd);
        e.prime_spellblade();
    }

    fn start_e(&mut self, e: &mut Engine, t: f64) {
        self.s.e_hit_at = t + self.e_cast_time;
        self.s.e_ready = t + e.basic_cd(self.e_cd);
        e.prime_spellblade();
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let w_drain_duration = kit.num("gen.W.drainDurationS")?;
        let w_tick_interval = kit.num("gen.W.tickIntervalS")?;
        let w_total_ticks = (w_drain_duration / w_tick_interval) as i64;

        let r_duration = kit.num("gen.R.durationS")?;
        let r_tick_interval = kit.num("gen.R.tickIntervalS")?;
        let r_total_ticks = (r_duration / r_tick_interval) as i64;

        let state = State {
            q_cast_until: 0.0,
            w_ready: 0.0,
            w_tick_at: INF,
            w_tick_idx: 0,
            e_ready: 0.0,
            e_hit_at: INF,
            r_ready: 0.0,
            r_channel_end_at: INF,
            r_tick_at: INF,
            r_tick_idx: 0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,

            q_min: kit.at_rank("gen.Q.minDamage", ranks.q)?,
            q_percent: kit.hit("gen.Q.percentOfCurrentHp", ranks.q, sheet)?,
            q_cast_time: kit.num("gen.Q.castTimeS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,

            w_tick_dmg: kit.hit("gen.W.perTick", ranks.w, sheet)?,
            w_missing_ratio: kit.at_rank("gen.W.missingHpRatio", ranks.w)?,
            w_cast_time: kit.num("gen.W.castTimeS")?,
            w_tick_interval,
            w_refund_frac: kit.num("gen.W.cdRefundFraction")?,
            w_total_ticks,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cast_time: kit.num("gen.E.castTimeS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,

            r_tick_dmg: kit.hit("gen.R.perTick", ranks.r, sheet)?,
            r_channel_time: kit.num("gen.R.channelTimeS")?,
            r_tick_interval,
            r_total_ticks,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,

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

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.busy(e.st.t) {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.q_cast_until = t + self.q_cast_time;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        let dmg = pymax(self.q_min, self.q_percent * pymax(e.st.hp, 0.0));
        e.deal(dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.start_r(e, t);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.w_tick_at != INF {
            out[n] = (self.s.w_tick_at, Kind::Ev(EV_W_TICK));
            n += 1;
        }
        if self.s.e_hit_at != INF {
            out[n] = (self.s.e_hit_at, Kind::Ev(EV_E_HIT));
            n += 1;
        }
        if self.s.r_channel_end_at != INF {
            out[n] = (self.s.r_channel_end_at, Kind::Ev(EV_R_CHANNEL_END));
            n += 1;
        }
        if self.s.r_tick_at != INF {
            out[n] = (self.s.r_tick_at, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        if !self.busy(e.st.t) {
            let mut ready = INF;
            if self.ranks.w > 0 {
                ready = pymin(ready, self.s.w_ready);
            }
            if self.ranks.e > 0 {
                ready = pymin(ready, self.s.e_ready);
            }
            if self.ranks.r > 0 {
                ready = pymin(ready, self.s.r_ready);
            }
            if ready != INF {
                out[n] = (pymax(ready, e.st.t), Kind::Ev(EV_ACT));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_ACT) => {
                if self.ranks.r > 0 && self.s.r_ready <= t {
                    self.start_r(e, t);
                } else if self.ranks.w > 0 && self.s.w_ready <= t {
                    self.start_w(e, t);
                } else if self.ranks.e > 0 && self.s.e_ready <= t {
                    self.start_e(e, t);
                }
            }
            Kind::Ev(EV_W_TICK) => {
                let idx = self.s.w_tick_idx;
                let is_last = idx == self.w_total_ticks - 1;
                let mut amt = self.w_tick_dmg;
                if is_last {
                    let missing = pymax(e.target_hp - pymax(e.st.hp, 0.0), 0.0);
                    amt += self.w_missing_ratio * missing;
                }
                e.deal(amt, DType::Magic, SRC_W, false, true, 1.0);
                if idx == 0 {
                    e.ability_cast_proc();
                    e.eclipse_hit();
                }
                if is_last {
                    let remaining = pymax(self.s.w_ready - t, 0.0);
                    self.s.w_ready = t + remaining * (1.0 - self.w_refund_frac);
                    self.s.w_tick_at = INF;
                } else {
                    self.s.w_tick_idx = idx + 1;
                    self.s.w_tick_at = t + self.w_tick_interval;
                }
            }
            Kind::Ev(EV_E_HIT) => {
                self.s.e_hit_at = INF;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_R_CHANNEL_END) => {
                self.s.r_channel_end_at = INF;
                self.s.r_tick_idx = 0;
                self.s.r_tick_at = t + self.r_tick_interval;
            }
            Kind::Ev(EV_R_TICK) => {
                let idx = self.s.r_tick_idx;
                e.deal(self.r_tick_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ult_hatefog();
                if idx == 0 {
                    e.ability_cast_proc();
                    e.eclipse_hit();
                }
                let is_last = idx == self.r_total_ticks - 1;
                if is_last {
                    self.s.r_tick_at = INF;
                } else {
                    self.s.r_tick_idx = idx + 1;
                    self.s.r_tick_at = t + self.r_tick_interval;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
