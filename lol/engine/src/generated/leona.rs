//! Leona. A melee auto-attacker: Shield of Daybreak arms her next basic
//! attack for bonus magic damage riding the hit, and the consumed attack
//! grants an extra reset; Eclipse and Zenith Blade go out on cooldown for
//! their own magic damage instances; Solar Flare opens the fight and is
//! recast on cooldown for its delayed impact damage. Zenith Blade and Solar
//! Flare each have a real 0.25s cast time that keeps Leona busy: no other
//! cast, no attack until it ends.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Eclipse: cast, then its detonation.
const EV_W_CAST: u8 = 0;
const EV_W_DETONATE: u8 = 1;
/// Zenith Blade: cast (its damage lands on cast).
const EV_E_CAST: u8 = 2;
/// Solar Flare: a recast, then its delayed impact.
const EV_R_CAST: u8 = 3;
const EV_R_IMPACT: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    w_guard_s: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    r_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    r_impact_delay: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// Shield of Daybreak is armed on a basic attack.
    q_armed: bool,
    /// Set true for the one attack that consumes the armed Q, read by
    /// after_attack (for the damage) and schedule_attack (for the reset).
    q_consumed_this_attack: bool,
    w_ready: f64,
    /// When the pending Eclipse detonation lands (INF: none pending).
    w_detonate_at: f64,
    e_ready: f64,
    r_ready: f64,
    /// When the pending Solar Flare impact lands (INF: none pending).
    r_impact_at: f64,
}

impl GenDriver {
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
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            q_armed: false,
            q_consumed_this_attack: false,
            w_ready: 0.0,
            w_detonate_at: INF,
            e_ready: 0.0,
            r_ready: 0.0,
            r_impact_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("leona kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_guard_s: kit.num("gen.W.guardDurationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_impact_delay: kit.num("gen.R.impactDelayS")?,
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

    fn before_attack(&mut self, _e: &mut Engine) {
        // an armed Q is spent on this attack
        self.s.q_consumed_this_attack = self.s.q_armed;
        self.s.q_armed = false;
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.q_consumed_this_attack {
            e.deal(self.q_dmg, DType::Magic, SRC_Q, true, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            // Q's cooldown starts post-effect: now, when the hit lands
            e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.s.q_consumed_this_attack {
            // the empowered attack's extra reset: only a windup away
            self.s.q_consumed_this_attack = false;
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.q_armed {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // instant: arms the next basic attack, no cast time, no damage yet
        self.s.q_armed = true;
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: engine has already primed Spellblade and
        // delayed the first attack for us; the cast itself keeps Leona busy
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_impact_at = t + self.r_impact_delay;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_detonate_at != INF {
                out[n] = (self.s.w_detonate_at, Kind::Ev(EV_W_DETONATE));
            } else {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_impact_at != INF {
                out[n] = (self.s.r_impact_at, Kind::Ev(EV_R_IMPACT));
            } else {
                out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                // cast time none, cooldown starts on cast; the detonation
                // is guaranteed to hit the stationary dummy 3s later
                self.s.w_detonate_at = t + self.w_guard_s;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_DETONATE) => {
                self.s.w_detonate_at = INF;
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_impact_at = t + self.r_impact_delay;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.prime_spellblade();
                self.busy_for(e, self.r_cast_s);
            }
            Kind::Ev(EV_R_IMPACT) => {
                self.s.r_impact_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
