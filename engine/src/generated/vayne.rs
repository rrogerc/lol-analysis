//! Vayne. An auto-attacker: Final Hour opens the fight for bonus AD and a
//! Tumble cooldown reduction, Tumble is woven in on cooldown for its attack
//! reset and empowered bonus damage, Condemn goes out on cooldown, and
//! Silver Bolts stacks off every on-hit application (attacks and Condemn),
//! consuming the third stack for true damage.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Condemn: cast (after which its 0.25 s cast time delays the bolt landing).
const EV_E_CAST: u8 = 0;
const EV_E_LAND: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_cd: f64,
    q_dmg: f64,
    q_bonus_ad_ratio: f64,
    q_empower_window: f64,
    e_cd: f64,
    e_cast_time: f64,
    e_dmg: f64,
    e_bonus_ad_ratio: f64,
    r_ad_bonus: f64,
    r_duration: f64,
    r_tumble_cdr: f64,
    w_floor: f64,
    w_hp_ratio: f64,
    w_stack_duration: f64,
    w_stacks_needed: i64,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_armed: bool,
    q_armed_until: f64,
    w_stacks: i64,
    w_stack_expire: f64,
    /// When Final Hour ends (-1: never cast / not learned).
    r_until: f64,
    e_ready: f64,
    /// When the pending Condemn bolt lands (INF: none pending).
    e_land_at: f64,
}

impl GenDriver {
    /// Applies a Silver Bolts stack from an on-hit attack or Condemn,
    /// consuming all three for true damage on the third.
    fn apply_w_stack(&mut self, e: &mut Engine, t: f64) {
        if self.ranks.w == 0 {
            return;
        }
        if t > self.s.w_stack_expire {
            self.s.w_stacks = 0;
        }
        self.s.w_stacks += 1;
        self.s.w_stack_expire = t + self.w_stack_duration;
        if self.s.w_stacks >= self.w_stacks_needed {
            self.s.w_stacks = 0;
            let dmg = pymax(self.w_floor, self.w_hp_ratio * e.target_hp);
            e.deal(dmg, DType::True, SRC_W, false, false, 1.0);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            q_armed: false,
            q_armed_until: 0.0,
            w_stacks: 0,
            w_stack_expire: 0.0,
            r_until: -1.0,
            e_ready: 0.0,
            e_land_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("vayne kit needs attack.windupFraction")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_bonus_ad_ratio: kit.at_rank("gen.Q.damage.bonusAdRatio", ranks.q)?,
            q_empower_window: kit.num("gen.Q.empowerDurationS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_time: kit.num("gen.E.castTimeS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_bonus_ad_ratio: kit.at_rank("gen.E.damage.bonusAdRatio", ranks.e)?,
            r_ad_bonus: kit.at_rank("gen.R.bonusAD", ranks.r)?,
            r_duration: kit.at_rank("gen.R.durationS", ranks.r)?,
            r_tumble_cdr: kit.at_rank("gen.R.tumbleCdrPct", ranks.r)?,
            w_floor: kit.at_rank("gen.W.floor", ranks.w)?,
            w_hp_ratio: kit.at_rank("gen.W.targetMaxHpRatio", ranks.w)?,
            w_stack_duration: kit.num("gen.W.stackDurationS")?,
            w_stacks_needed: kit.num("gen.W.stacksNeeded")? as i64,
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
        if e.st.t < self.s.r_until {
            e.p.ad + self.r_ad_bonus
        } else {
            e.p.ad
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.apply_w_stack(e, t);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.q_armed {
            self.s.q_armed = false;
            let t = e.st.t;
            if t <= self.s.q_armed_until {
                let extra = if t < self.s.r_until {
                    self.r_ad_bonus * self.q_bonus_ad_ratio
                } else {
                    0.0
                };
                let dmg = self.q_dmg + extra;
                e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
            }
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let cdr = if t < self.s.r_until { self.r_tumble_cdr } else { 0.0 };
        let base = self.q_cd * (1.0 - cdr);
        e.st.q_ready = t + e.basic_cd(base);
        self.s.q_armed = true;
        self.s.q_armed_until = t + self.q_empower_window;
        let b = self.bonus_as(t);
        // Tumble resets the attack timer: the empowered attack follows one
        // windup later
        e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        e.prime_spellblade();
        e.ability_cast_proc();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_until = t + self.r_duration;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        if self.ranks.e == 0 {
            return 0;
        }
        let mut n = 0;
        if self.s.e_land_at != INF {
            out[n] = (self.s.e_land_at, Kind::Ev(EV_E_LAND));
            n += 1;
        } else {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_land_at = t + self.e_cast_time;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_LAND) => {
                self.s.e_land_at = INF;
                let extra = if t < self.s.r_until {
                    self.r_ad_bonus * self.e_bonus_ad_ratio
                } else {
                    0.0
                };
                let dmg = self.e_dmg + extra;
                e.deal(dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.apply_w_stack(e, t);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
