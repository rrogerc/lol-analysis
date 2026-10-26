//! Sylas. Q/W/E go out on cooldown; Chain Lash's chain hit and its
//! 0.6 s-delayed explosion are two damage instances from one cast, with
//! Chain Lash's 0.4 s cast time and Hijack's 0.25 s cast time keeping Sylas
//! busy (no other cast, no attack) until they end. Every ability cast
//! (including the opening Hijack, which deals no damage against a dummy)
//! refreshes a shared Unshackled stack timer, which grants flat bonus attack
//! speed while up and turns the next attack into Petricite Burst's own
//! magic damage instead of a normal physical hit.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Chain Lash's delayed explosion; Kingslayer and Abscond/Abduct on cooldown.
const EV_Q_EXPLODE: u8 = 0;
const EV_W_CAST: u8 = 1;
const EV_E_CAST: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Petricite Burst's primary-target ratios, its flat bonus attack speed,
    /// and the shared stack timer's duration and cap.
    p_ad_coef: f64,
    p_ap_coef: f64,
    p_as_pct: f64,
    p_duration: f64,
    p_max: i64,
    q_dmg: f64,
    q_explode_dmg: f64,
    q_explode_delay: f64,
    q_cast_s: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    r_cast_s: f64,
    src_p: SourceId,
    src_q_explosion: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    busy_until: f64,
    stack_count: i64,
    stack_expire_at: f64,
    empowered_this_attack: bool,
    /// Chain Lash's pending explosion (INF: none pending).
    q_explode_at: f64,
    w_ready: f64,
    e_ready: f64,
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

    /// The Unshackled stacks actually up at time `t` (0 if the shared timer
    /// has already lapsed).
    fn effective_stacks(&self, t: f64) -> i64 {
        if t >= self.s.stack_expire_at {
            0
        } else {
            self.s.stack_count
        }
    }

    /// Any ability cast refreshes the shared timer to a fresh 4 s window,
    /// adding a stack up to the cap.
    fn refresh_stack(&mut self, t: f64) {
        let cur = self.effective_stacks(t);
        self.s.stack_count = imin(cur + 1, self.p_max);
        self.s.stack_expire_at = t + self.p_duration;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            stack_count: 0,
            stack_expire_at: -INF,
            empowered_this_attack: false,
            q_explode_at: INF,
            w_ready: 0.0,
            e_ready: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("sylas kit needs attack.windupFraction")?,
            p_ad_coef: kit.num("gen.P.primaryDamage.adRatio")?,
            p_ap_coef: kit.num("gen.P.primaryDamage.apRatio")?,
            p_as_pct: kit.num("gen.P.bonusAsPct")?,
            p_duration: kit.num("gen.P.stackDurationS")?,
            p_max: kit.num("gen.P.maxStacks")? as i64,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_explode_dmg: kit.hit("gen.Q.explosionDamage", ranks.q, sheet)?,
            q_explode_delay: kit.num("gen.Q.detonationDelayS")?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_p: intern("P"),
            src_q_explosion: intern("Q explosion"),
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
        if self.effective_stacks(t) > 0 {
            self.p_as_pct
        } else {
            0.0
        }
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        if self.s.empowered_this_attack {
            0.0
        } else {
            e.p.ad
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let stacks = self.effective_stacks(t);
        if stacks > 0 {
            self.s.stack_count = stacks - 1;
            self.s.empowered_this_attack = true;
        } else {
            self.s.stack_count = 0;
            self.s.empowered_this_attack = false;
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.empowered_this_attack {
            let dmg = self.p_ad_coef * e.p.ad + self.p_ap_coef * e.p.sheet.ap;
            e.deal(dmg, DType::Magic, self.src_p, true, false, 1.0);
            self.s.empowered_this_attack = false;
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        self.s.q_explode_at = t + self.q_explode_delay;
        self.refresh_stack(t);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // Hijack: no enemy ultimate exists to steal against a dummy, so it
        // deals no damage; it still counts as an ability cast for the stack,
        // and its 0.25 s cast time keeps Sylas busy before anything else.
        let t = e.st.t;
        self.refresh_stack(t);
        e.prime_spellblade();
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_explode_at != INF {
            out[n] = (self.s.q_explode_at, Kind::Ev(EV_Q_EXPLODE));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_EXPLODE) => {
                self.s.q_explode_at = INF;
                e.deal(self.q_explode_dmg, DType::Magic, self.src_q_explosion, false, true, 1.0);
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                self.refresh_stack(t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.refresh_stack(t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
