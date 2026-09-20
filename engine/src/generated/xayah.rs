//! Xayah. An auto-attacker whose kit rides feathers: Clean Cuts consumes a
//! stack per attack (planting a feather, the primary hit unchanged since a
//! stationary dummy has no second target for the splash), Double Daggers
//! plants two more feathers on delayed missiles, Deadly Plumage buffs attack
//! speed and adds an on-hit feather, Bladecaller recalls whatever feathers
//! are on the ground for a crit-scaling burst, and Featherstorm opens the
//! fight for its flat burst since its cooldown far exceeds the fight length.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Double Daggers' two missiles land on delayed timers, not on cast.
const EV_Q1: u8 = 0;
const EV_Q2: u8 = 1;
/// Deadly Plumage is cast on cooldown for its frenzy.
const EV_W: u8 = 2;
/// Bladecaller is cast on cooldown once a feather is planted.
const EV_E: u8 = 3;
/// Featherstorm's feathers land after its leap delay.
const EV_R_SWING: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    p_max: i64,
    p_per_cast: i64,
    p_dur: f64,
    feather_dur: f64,
    q_dmg1: f64,
    q_dmg2: f64,
    q_cd: f64,
    q_cast_start: f64,
    q_cast_min: f64,
    q_cast_scalar: f64,
    q_delay1: f64,
    q_delay2: f64,
    w_as_pct: f64,
    w_dur: f64,
    w_onhit_pct: f64,
    w_cd: f64,
    e_dmg: f64,
    e_crit_ratio: f64,
    e_falloff: f64,
    e_cd: f64,
    r_dmg: f64,
    r_attack_delay_s: f64,
    r_untargetable_s: f64,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    p_expire: f64,
    /// Feathers currently planted on the ground: their plant times.
    feather_times: [f64; 8],
    feather_count: usize,
    w_ready: f64,
    w_until: f64,
    e_ready: f64,
    q_hit1_at: f64,
    q_hit2_at: f64,
    /// Featherstorm's leap window: no attacks or casts until this time.
    r_busy_until: f64,
    r_swing_at: f64,
}

impl GenDriver {
    fn add_p_stacks(&mut self, t: f64) {
        self.s.p_stacks = imin(self.s.p_stacks + self.p_per_cast, self.p_max);
        self.s.p_expire = t + self.p_dur;
    }

    fn add_feather(&mut self, t: f64) {
        if self.s.feather_count < 8 {
            self.s.feather_times[self.s.feather_count] = t;
            self.s.feather_count += 1;
        }
    }

    fn active_feather_count(&self, t: f64) -> i64 {
        let mut c = 0;
        let mut i = 0;
        while i < self.s.feather_count {
            if t - self.s.feather_times[i] < self.feather_dur {
                c += 1;
            }
            i += 1;
        }
        c
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            p_expire: 0.0,
            feather_times: [0.0; 8],
            feather_count: 0,
            w_ready: 0.0,
            w_until: -1.0,
            e_ready: 0.0,
            q_hit1_at: INF,
            q_hit2_at: INF,
            r_busy_until: 0.0,
            r_swing_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_max: kit.num("gen.P.stackMax")? as i64,
            p_per_cast: kit.num("gen.P.stacksPerCast")? as i64,
            p_dur: kit.num("gen.P.stackDurationS")?,
            feather_dur: kit.num("gen.P.featherDurationS")?,
            q_dmg1: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_dmg2: kit.hit("gen.Q.damage2", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_start: kit.num("gen.Q.castTimeStartS")?,
            q_cast_min: kit.num("gen.Q.castTimeMinS")?,
            q_cast_scalar: kit.num("gen.Q.castTimeScalarPer100AS")?,
            q_delay1: kit.num("gen.Q.missile1DelayS")?,
            q_delay2: kit.num("gen.Q.missile2DelayS")?,
            w_as_pct: kit.at_rank("gen.W.asPctByRank", ranks.w)?,
            w_dur: kit.num("gen.W.asDurationS")?,
            w_onhit_pct: kit.num("gen.W.onhitPct")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_crit_ratio: kit.num("gen.E.critRatio")?,
            e_falloff: kit.num("gen.E.falloffPerHit")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_attack_delay_s: kit.num("gen.R.attackDelayS")?,
            r_untargetable_s: kit.num("gen.R.untargetableS")?,
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

    fn before_attack(&mut self, e: &mut Engine) {
        // Clean Cuts: an available, unexpired stack empowers this attack to
        // plant a feather; the primary hit itself is unchanged (no second
        // target for the splash on a lone dummy).
        let t = e.st.t;
        if self.s.p_stacks > 0 && t < self.s.p_expire {
            self.s.p_stacks -= 1;
            self.add_feather(t);
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Deadly Plumage's extra feather: 25% of the triggering attack's
        // damage, sharing its expected crit scaling.
        if e.st.t < self.s.w_until {
            let dmg = e.p.ad * self.w_onhit_pct / 100.0;
            e.deal(dmg, DType::Physical, SRC_W, true, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(pymax(e.st.q_ready, e.st.t), self.s.r_busy_until)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let total_as = e.p.sheet.bonus_as_pct + self.bonus_as(t);
        let cast_time = pymax(
            self.q_cast_min,
            self.q_cast_start - self.q_cast_scalar * (total_as / 100.0),
        );
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.q_hit1_at = t + self.q_delay1;
        self.s.q_hit2_at = t + self.q_delay2;
        e.st.next_attack = pymax(e.st.next_attack, t + cast_time);
        self.add_p_stacks(t);
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_busy_until = t + self.r_untargetable_s;
        self.s.r_swing_at = t + self.r_attack_delay_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.r_busy_until);
        self.add_p_stacks(t);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        let t = e.st.t;
        if self.s.q_hit1_at != INF {
            out[n] = (self.s.q_hit1_at, Kind::Ev(EV_Q1));
            n += 1;
        }
        if self.s.q_hit2_at != INF {
            out[n] = (self.s.q_hit2_at, Kind::Ev(EV_Q2));
            n += 1;
        }
        if self.ranks.w > 0 {
            let cand = pymax(pymax(self.s.w_ready, t), self.s.r_busy_until);
            out[n] = (cand, Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 && self.active_feather_count(t) > 0 {
            let cand = pymax(pymax(self.s.e_ready, t), self.s.r_busy_until);
            out[n] = (cand, Kind::Ev(EV_E));
            n += 1;
        }
        if self.s.r_swing_at != INF {
            out[n] = (self.s.r_swing_at, Kind::Ev(EV_R_SWING));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q1) => {
                self.s.q_hit1_at = INF;
                e.deal(self.q_dmg1, DType::Physical, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.add_feather(t);
            }
            Kind::Ev(EV_Q2) => {
                self.s.q_hit2_at = INF;
                e.deal(self.q_dmg2, DType::Physical, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.add_feather(t);
            }
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_until = t + self.w_dur;
                self.add_p_stacks(t);
                e.prime_spellblade();
            }
            Kind::Ev(EV_E) => {
                let n = self.active_feather_count(t);
                if n > 0 {
                    let nf = n as f64;
                    let mult = nf - self.e_falloff * (nf - 1.0) / 2.0 * nf;
                    let cc = e.p.sheet.crit_chance / 100.0;
                    let cd = e.p.sheet.crit_damage / 100.0;
                    let cf = 1.0 + self.e_crit_ratio * cc * (cd - 1.0);
                    let total = self.e_dmg * mult * cf;
                    e.deal(total, DType::Physical, SRC_E, false, true, 1.0);
                    e.ability_cast_proc();
                    e.eclipse_hit();
                }
                self.s.feather_count = 0;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.add_p_stacks(t);
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_SWING) => {
                self.s.r_swing_at = INF;
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
