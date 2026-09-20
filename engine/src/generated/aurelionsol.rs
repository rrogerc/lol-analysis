//! Aurelion Sol. A pure caster: Breath of Light is cast once and treated as
//! an uninterrupted channel for the whole fight (beam ticks every 0.125s,
//! its burst fires every full second, split into a flat+AP magic instance
//! and a true-damage instance scaled by an assumed fixed Stardust count),
//! Singularity ticks on cooldown, and Falling Star opens the fight once as
//! its empowered form, The Skies Descend, whose star impact is the only
//! damage dealt (a target hit by the impact never also takes the
//! shockwave). Astral Flight is never cast: on a stationary dummy it has no
//! combat value.

use crate::fight::{Driver, Engine, Events, Kind};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const EV_Q_BEAM: u8 = 0;
const EV_Q_BURST: u8 = 1;
const EV_R_IMPACT: u8 = 2;
const EV_E_CAST: u8 = 3;
const EV_E_TICK: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    // Breath of Light
    q_beam_tick_dmg: f64,
    q_beam_interval_s: f64,
    q_burst_dmg: f64,
    q_burst_true_frac: f64,
    q_burst_interval_s: f64,
    // Singularity
    e_tick_dmg: f64,
    e_tick_interval_s: f64,
    e_delay_s: f64,
    e_tick_count: i64,
    e_cd: f64,
    // The Skies Descend
    r_impact_dmg: f64,
    r_delay_s: f64,
    src_q_burst: SourceId,
    src_q_burst_true: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_started: bool,
    q_next_beam: f64,
    q_next_burst: f64,
    r_impact_at: f64,
    e_ready: f64,
    e_active: bool,
    e_next_tick: f64,
    e_ticks_done: i64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            q_started: false,
            q_next_beam: INF,
            q_next_burst: INF,
            r_impact_at: INF,
            e_ready: 0.0,
            e_active: false,
            e_next_tick: INF,
            e_ticks_done: 0,
        };
        let beam_interval = kit.num("gen.Q.beamTickIntervalS")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_beam_tick_dmg: kit.hit("gen.Q.dps", ranks.q, sheet)? * beam_interval,
            q_beam_interval_s: beam_interval,
            q_burst_dmg: kit.hit("gen.Q.burst", ranks.q, sheet)?,
            q_burst_true_frac: kit.num("gen.P.trueDmgPerStackOfTargetMaxHp")?
                * kit.num("gen.P.assumedStacks")?,
            q_burst_interval_s: kit.num("gen.Q.burstIntervalS")?,
            e_tick_dmg: kit.hit("gen.E.tick", ranks.e, sheet)?,
            e_tick_interval_s: kit.num("gen.E.tickIntervalS")?,
            e_delay_s: kit.num("gen.E.delayS")?,
            e_tick_count: kit.num("gen.E.tickCount")? as i64,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_impact_dmg: kit.hit("gen.R.impact", ranks.r, sheet)?,
            r_delay_s: kit.num("gen.R.delayS")?,
            src_q_burst: intern("Q burst"),
            src_q_burst_true: intern("Q burst true"),
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
        if self.ranks.q == 0 || self.s.q_started {
            return INF;
        }
        e.st.t
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // the whole-fight channel: start the beam ticks and the once-a-
        // second burst, never to be recast (the channel is treated as
        // uninterrupted and never-ending)
        let t = e.st.t;
        self.s.q_started = true;
        self.s.q_next_beam = t + self.q_beam_interval_s;
        self.s.q_next_burst = t + self.q_burst_interval_s;
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: The Skies Descend's star strikes after its delay
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_impact_at = e.st.t + self.r_delay_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_next_beam != INF {
            out[n] = (self.s.q_next_beam, Kind::Ev(EV_Q_BEAM));
            n += 1;
        }
        if self.s.q_next_burst != INF {
            out[n] = (self.s.q_next_burst, Kind::Ev(EV_Q_BURST));
            n += 1;
        }
        if self.s.r_impact_at != INF {
            out[n] = (self.s.r_impact_at, Kind::Ev(EV_R_IMPACT));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_active {
                out[n] = (self.s.e_next_tick, Kind::Ev(EV_E_TICK));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_BEAM) => {
                e.deal(self.q_beam_tick_dmg, DType::Magic, SRC_Q, false, true, 1.0);
                self.s.q_next_beam = t + self.q_beam_interval_s;
            }
            Kind::Ev(EV_Q_BURST) => {
                e.deal(self.q_burst_dmg, DType::Magic, self.src_q_burst, false, true, 1.0);
                let true_dmg = self.q_burst_true_frac * e.target_hp;
                e.deal(true_dmg, DType::True, self.src_q_burst_true, false, true, 1.0);
                self.s.q_next_burst = t + self.q_burst_interval_s;
            }
            Kind::Ev(EV_R_IMPACT) => {
                self.s.r_impact_at = INF;
                e.deal(self.r_impact_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_E_CAST) => {
                // 0.2s cast time, then the 0.5s (from cast start) delay
                // before the black hole appears and starts ticking
                self.s.e_active = true;
                self.s.e_ticks_done = 0;
                self.s.e_next_tick = t + self.e_delay_s;
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_TICK) => {
                e.deal(self.e_tick_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.s.e_ticks_done += 1;
                if self.s.e_ticks_done < self.e_tick_count {
                    self.s.e_next_tick = t + self.e_tick_interval_s;
                } else {
                    // duration ended: post-effect cooldown starts now
                    self.s.e_active = false;
                    self.s.e_next_tick = INF;
                    self.s.e_ready = t + e.basic_cd(self.e_cd);
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
