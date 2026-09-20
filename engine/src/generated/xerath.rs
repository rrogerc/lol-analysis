//! Xerath. A caster who opens with the forced Rite of the Arcane channel
//! (its Arcane Barrage recasts fire every 0.6 s, each after the first
//! benefiting from an Arcane Perfection stack granted by the previous hit),
//! then weaves Arcanopulse (cast, then a delayed beam), Eye of Destruction
//! and Shocking Orb (each a cast then a delayed impact) on cooldown for the
//! rest of the fight.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Arcanopulse's beam lands (after its recast delay).
const EV_Q_BEAM: u8 = 0;
/// Eye of Destruction: the cast, then the delayed impact.
const EV_W_CAST: u8 = 1;
const EV_W_HIT: u8 = 2;
/// Shocking Orb: the cast, then the delayed impact.
const EV_E_CAST: u8 = 3;
const EV_E_HIT: u8 = 4;
/// Rite of the Arcane: one Arcane Barrage recast.
const EV_R_SHOT: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_recast_delay: f64,
    w_dmg: f64,
    w_cd: f64,
    w_cast_time: f64,
    w_impact_delay: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_time: f64,
    r_dmg: f64,
    r_stack_bonus: f64,
    r_num_shots: i64,
    r_max_stacks: i64,
    r_interval: f64,
    r_first_delay: f64,
    r_channel_dur: f64,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Arcanopulse: when the pending beam lands (INF: none pending).
    q_beam_at: f64,
    w_ready: f64,
    /// Eye of Destruction: when the pending impact lands (INF: none pending).
    w_impact_at: f64,
    e_ready: f64,
    /// Shocking Orb: when the pending impact lands (INF: none pending).
    e_impact_at: f64,
    /// Rite of the Arcane: whether the channel is still running.
    r_active: bool,
    r_next_shot_at: f64,
    r_channel_deadline: f64,
    r_shots_fired: i64,
    r_stacks: i64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            q_beam_at: INF,
            w_ready: 0.0,
            w_impact_at: INF,
            e_ready: 0.0,
            e_impact_at: INF,
            r_active: false,
            r_next_shot_at: INF,
            r_channel_deadline: INF,
            r_shots_fired: 0,
            r_stacks: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_recast_delay: kit.num("gen.Q.recastDelayS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)? * kit.num("gen.W.sweetSpotMult")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_time: kit.num("gen.W.castTimeS")?,
            w_impact_delay: kit.num("gen.W.impactDelayS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_time: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_stack_bonus: kit.hit("gen.R.stackBonus", ranks.r, sheet)?,
            r_num_shots: kit.at_rank("gen.R.numShots", ranks.r)? as i64,
            r_max_stacks: kit.at_rank("gen.R.maxStacks", ranks.r)? as i64,
            r_interval: kit.num("gen.R.recastIntervalS")?,
            r_first_delay: kit.num("gen.R.firstRecastDelayS")?,
            r_channel_dur: kit.num("gen.R.channelDurationS")?,
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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.r_active || self.s.q_beam_at != INF {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // charges instantly (minimum charge) and immediately recasts: the
        // only modeled delay is the 0.5s unable-to-act window before the
        // beam fires
        let t = e.st.t;
        self.s.q_beam_at = t + self.q_recast_delay;
        e.st.next_attack = pymax(e.st.next_attack, self.s.q_beam_at);
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the opening cast: the engine has already primed Spellblade; the
        // channel runs until its recasts are exhausted or 10s passes
        let t = e.st.t;
        self.s.r_active = true;
        self.s.r_shots_fired = 0;
        self.s.r_stacks = 0;
        self.s.r_channel_deadline = t + self.r_channel_dur;
        self.s.r_next_shot_at = t + self.r_first_delay;
        e.st.next_attack = pymax(e.st.next_attack, self.s.r_next_shot_at);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        let t = e.st.t;
        if self.s.q_beam_at != INF {
            out[n] = (self.s.q_beam_at, Kind::Ev(EV_Q_BEAM));
            n += 1;
        }
        if !self.s.r_active {
            if self.ranks.w > 0 {
                if self.s.w_impact_at != INF {
                    out[n] = (self.s.w_impact_at, Kind::Ev(EV_W_HIT));
                } else {
                    out[n] = (pymax(self.s.w_ready, t), Kind::Ev(EV_W_CAST));
                }
                n += 1;
            }
            if self.ranks.e > 0 {
                if self.s.e_impact_at != INF {
                    out[n] = (self.s.e_impact_at, Kind::Ev(EV_E_HIT));
                } else {
                    out[n] = (pymax(self.s.e_ready, t), Kind::Ev(EV_E_CAST));
                }
                n += 1;
            }
        }
        if self.s.r_active {
            out[n] = (self.s.r_next_shot_at, Kind::Ev(EV_R_SHOT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_BEAM) => {
                self.s.q_beam_at = INF;
                e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.st.q_ready = t + e.basic_cd(self.q_cd);
            }
            Kind::Ev(EV_W_CAST) => {
                e.lockout();
                e.prime_spellblade();
                self.s.w_impact_at = t + self.w_impact_delay;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                let _ = self.w_cast_time;
            }
            Kind::Ev(EV_W_HIT) => {
                self.s.w_impact_at = INF;
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_CAST) => {
                e.lockout();
                e.prime_spellblade();
                self.s.e_impact_at = t + self.e_cast_time;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
            Kind::Ev(EV_E_HIT) => {
                self.s.e_impact_at = INF;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_R_SHOT) => {
                let dmg = self.r_dmg + (self.s.r_stacks as f64) * self.r_stack_bonus;
                e.deal(dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                self.s.r_shots_fired += 1;
                self.s.r_stacks = imin(self.s.r_stacks + 1, self.r_max_stacks);
                let next = t + self.r_interval;
                if self.s.r_shots_fired < self.r_num_shots && next <= self.s.r_channel_deadline {
                    self.s.r_next_shot_at = next;
                    e.st.next_attack = pymax(e.st.next_attack, next);
                } else {
                    self.s.r_next_shot_at = INF;
                    self.s.r_active = false;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
