//! Miss Fortune. Bullet Time opens the fight as an uninterruptible channel
//! whose waves are timed events; afterward Double Up alternates with basic
//! attacks on the shared attack timer, Strut is recast on cooldown for its
//! attack-speed active, Make It Rain ticks on cooldown, and Love Tap's
//! one-time mark bonus lands on whichever attack (auto or Double Up) hits
//! the still-unmarked dummy first.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Strut recast (on cooldown, self-buff, no cast time).
const EV_W: u8 = 0;
/// Make It Rain: the cast, then its ticks.
const EV_E_CAST: u8 = 1;
const EV_E_TICK: u8 = 2;
/// Bullet Time: the next wave.
const EV_R_WAVE: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Love Tap: precomputed bonus physical damage (byLevel coef * total AD).
    p_dmg: f64,
    src_p: SourceId,
    q_dmg: f64,
    q_cd: f64,
    /// Strut: percent bonus attack speed while active, its duration, and
    /// the flat cooldown refund from Love Tap's mark.
    w_as_pct: f64,
    w_cd: f64,
    w_duration: f64,
    w_refund: f64,
    /// Make It Rain: total damage split evenly over its ticks.
    e_tick_dmg: f64,
    e_ticks: i64,
    e_tick_interval: f64,
    e_cast_s: f64,
    e_cd: f64,
    /// Bullet Time: per-wave damage (expected crit folded in), wave count,
    /// and the wiki's wave timing.
    r_wave_dmg: f64,
    r_waves_n: i64,
    r_first_wave: f64,
    r_last_wave: f64,
    r_wave_interval: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_marked: bool,
    w_ready: f64,
    w_until: f64,
    e_ready: f64,
    e_active: bool,
    e_tick_idx: i64,
    e_tick_next: f64,
    r_active: bool,
    r_wave_idx: i64,
    r_wave_next: f64,
    r_cast_t: f64,
    /// No other action (attack, Q, W, E) may start before this: holds the
    /// whole Bullet Time channel.
    busy_until: f64,
}

impl GenDriver {
    /// Love Tap can only ever proc once against a single dummy (the mark
    /// never expires without a second enemy to attack).
    fn love_tap_proc(&mut self, e: &mut Engine) {
        if !self.s.p_marked {
            self.s.p_marked = true;
            e.deal(self.p_dmg, DType::Physical, self.src_p, false, false, 1.0);
            self.s.w_ready = pymax(e.st.t, self.s.w_ready - self.w_refund);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let crit_chance = sheet.crit_chance / 100.0;
        let crit_dmg_mult = sheet.crit_damage / 100.0;
        let r_crit_bonus = kit.num("gen.R.critBonusMult")?;
        let r_expected_mult = 1.0 + crit_chance * r_crit_bonus * (crit_dmg_mult - 1.0);
        let r_waves_n = kit.at_rank("gen.R.waves", ranks.r)? as i64;
        let r_first_wave = kit.num("gen.R.firstWaveS")?;
        let r_last_wave = kit.num("gen.R.lastWaveS")?;
        let r_wave_interval = if r_waves_n > 1 {
            (r_last_wave - r_first_wave) / ((r_waves_n - 1) as f64)
        } else {
            0.0
        };
        let e_ticks = kit.num("gen.E.ticks")? as i64;
        let e_total = kit.hit("gen.E.damage", ranks.e, sheet)?;
        let state = State {
            p_marked: false,
            w_ready: 0.0,
            w_until: -1.0,
            e_ready: 0.0,
            e_active: false,
            e_tick_idx: 0,
            e_tick_next: 0.0,
            r_active: false,
            r_wave_idx: 0,
            r_wave_next: INF,
            r_cast_t: 0.0,
            busy_until: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("missfortune kit needs attack.windupFraction")?,
            p_dmg: kit.at_level("gen.P.byLevel", level)? * sheet.ad,
            src_p: intern("P"),
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_as_pct: kit.at_rank("gen.W.asPct", ranks.w)? * 100.0,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_duration: kit.num("gen.W.durationS")?,
            w_refund: kit.num("gen.W.loveTapRefundS")?,
            e_tick_dmg: e_total / (e_ticks as f64),
            e_ticks,
            e_tick_interval: kit.num("gen.E.tickIntervalS")?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_wave_dmg: kit.hit("gen.R.damage", ranks.r, sheet)? * r_expected_mult,
            r_waves_n,
            r_first_wave,
            r_last_wave,
            r_wave_interval,
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

    fn bonus_as(&self, t: f64) -> f64 {
        if t < self.s.w_until {
            self.w_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Love Tap: a basic attack's on-attack application.
        self.love_tap_proc(e);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(pymax(pymax(e.st.q_ready, e.st.next_attack), self.s.busy_until), e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // Double Up occupies the attack timer's slot: it consumes this
        // attack instead of running alongside it.
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.love_tap_proc(e);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, true, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        let b = self.bonus_as(t);
        e.st.next_attack = t + e.attack_period(b);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_active = true;
        self.s.r_wave_idx = 0;
        self.s.r_cast_t = t;
        self.s.r_wave_next = t + self.r_first_wave;
        self.s.busy_until = t + self.r_last_wave;
        e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
        e.prime_spellblade();
        e.ability_cast_proc();
        e.eclipse_hit();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(pymax(self.s.w_ready, self.s.busy_until), e.st.t), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_active {
                out[n] = (self.s.e_tick_next, Kind::Ev(EV_E_TICK));
            } else {
                out[n] = (pymax(pymax(self.s.e_ready, self.s.busy_until), e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.s.r_active {
            out[n] = (self.s.r_wave_next, Kind::Ev(EV_R_WAVE));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_until = t + self.w_duration;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_active = true;
                self.s.e_tick_idx = 0;
                self.s.e_tick_next = t + self.e_cast_s;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.lockout();
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_TICK) => {
                e.deal(self.e_tick_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.s.e_tick_idx += 1;
                if self.s.e_tick_idx >= self.e_ticks {
                    self.s.e_active = false;
                } else {
                    self.s.e_tick_next = t + self.e_tick_interval;
                }
            }
            Kind::Ev(EV_R_WAVE) => {
                e.deal(self.r_wave_dmg, DType::Physical, SRC_R, false, true, 1.0);
                self.s.r_wave_idx += 1;
                if self.s.r_wave_idx >= self.r_waves_n {
                    self.s.r_active = false;
                    e.ult_hatefog();
                } else {
                    self.s.r_wave_next = self.s.r_cast_t + self.r_first_wave
                        + (self.s.r_wave_idx as f64) * self.r_wave_interval;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
