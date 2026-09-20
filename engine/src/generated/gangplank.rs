//! Gangplank. Opens with Cannon Barrage (R), fires Parrrley (Q) on cooldown,
//! and lets Trial by Fire (P) automatically ride the next basic attack once
//! it is off cooldown, ticking its true-damage burn over 2.5s. Powder Keg
//! (E) and Remove Scurvy (W) are not part of the damage fight (see kit notes
//! and the "unused" entry).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Trial by Fire's burn ticks; Cannon Barrage's waves.
const EV_P_TICK: u8 = 0;
const EV_R_WAVE: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    p_cd: f64,
    p_tick_interval: f64,
    p_ticks: i64,
    p_tick_amount: f64,
    src_p: SourceId,

    q_dmg: f64,
    q_cd: f64,

    r_dmg: f64,
    r_cast_s: f64,
    r_cluster_interval: f64,
    r_intra_delay: f64,
    r_waves_total: i64,
    r_cluster_size: i64,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_ready: f64,
    p_dot_active: bool,
    p_ticks_remaining: i64,
    p_next_tick: f64,
    /// How many of Cannon Barrage's waves have already landed (>= total: none pending).
    r_idx: i64,
}

impl GenDriver {
    fn wave_time(&self, idx: i64) -> f64 {
        self.r_cast_s
            + (idx / self.r_cluster_size) as f64 * self.r_cluster_interval
            + (idx % self.r_cluster_size) as f64 * self.r_intra_delay
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool) -> Result<Self, String> {
        let p_duration = kit.num("gen.P.durationS")?;
        let p_tick_interval = kit.num("gen.P.tickIntervalS")?;
        let p_ticks = (p_duration / p_tick_interval) as i64;
        let p_base = kit.at_level("gen.P.baseByLevel", level)?;
        let p_ad_ratio = kit.num("gen.P.adRatio")?;
        let p_total = p_base + p_ad_ratio * sheet.ad_bonus;
        let p_tick_amount = p_total / p_ticks as f64;

        let state = State {
            p_ready: 0.0,
            p_dot_active: false,
            p_ticks_remaining: 0,
            p_next_tick: INF,
            r_idx: 0,
        };

        let r_waves_total = kit.num("gen.R.totalWaves")? as i64;

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit
                .windup_fraction
                .ok_or("gangplank kit needs attack.windupFraction")?,

            p_cd: kit.num("gen.P.cooldownS")?,
            p_tick_interval,
            p_ticks,
            p_tick_amount,
            src_p: intern("P burn"),

            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,

            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_cluster_interval: kit.num("gen.R.clusterIntervalS")?,
            r_intra_delay: kit.num("gen.R.intraClusterDelayS")?,
            r_waves_total,
            r_cluster_size: kit.num("gen.R.clusterSize")? as i64,

            s: State { r_idx: r_waves_total, ..state },
            s0: State { r_idx: r_waves_total, ..state },
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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.p_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t >= self.s.p_ready {
            self.s.p_ready = t + e.basic_cd(self.p_cd);
            self.s.p_dot_active = true;
            self.s.p_ticks_remaining = self.p_ticks;
            self.s.p_next_tick = t + self.p_tick_interval;
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, true, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        self.s.r_idx = 0;
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn events(&self, _e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.p_dot_active {
            out[n] = (self.s.p_next_tick, Kind::Ev(EV_P_TICK));
            n += 1;
        }
        if self.s.r_idx < self.r_waves_total {
            out[n] = (self.wave_time(self.s.r_idx), Kind::Ev(EV_R_WAVE));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_P_TICK) => {
                e.deal(self.p_tick_amount, DType::True, self.src_p, false, false, 1.0);
                self.s.p_ticks_remaining -= 1;
                if self.s.p_ticks_remaining > 0 {
                    self.s.p_next_tick = t + self.p_tick_interval;
                } else {
                    self.s.p_dot_active = false;
                }
            }
            Kind::Ev(EV_R_WAVE) => {
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                self.s.r_idx += 1;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
