//! Kassadin. Riftwalk opens the fight and is recast whenever affordable,
//! Null Sphere and Force Pulse go out on cooldown while mana allows, Nether
//! Blade is re-armed after every attack for its own on-hit-replacing bonus
//! and attack-timer reset, and every cast shaves Force Pulse's cooldown.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Force Pulse's own cast, once its current cooldown (with CDR from other
/// casts) is up and mana allows.
const EV_E_CAST: u8 = 0;
/// Riftwalk's own recast (not the opening one, which the engine drives via
/// `cast_r`), and the blink's damage landing after its cast time.
const EV_R_CAST: u8 = 1;
const EV_R_LAND: u8 = 2;
/// Nether Blade's empowered attack expiring unused (defensive: in practice
/// it is almost always consumed by the very next swing).
const EV_W_EXPIRE: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cost: f64,
    q_cd: f64,
    e_dmg: f64,
    e_cost: f64,
    e_cd: f64,
    e_cdr: f64,
    w_active_dmg: f64,
    w_onhit_dmg: f64,
    w_window_s: f64,
    w_cd: f64,
    r_base_dmg: f64,
    r_stack_dmg: f64,
    r_cd: f64,
    r_cast_time_s: f64,
    r_stack_dur: f64,
    r_max_stacks: i64,
    r_base_cost: f64,
    r_cost_ratio: f64,
    src_w_onhit: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    mana: f64,
    e_ready: f64,
    w_ready: f64,
    w_armed: bool,
    w_armed_until: f64,
    /// Set when the just-landed attack was the empowered one: the next
    /// attack's timer is reset to a bare windup.
    just_reset: bool,
    r_ready: f64,
    r_stacks: i64,
    r_stack_expiry: f64,
    /// When the pending Riftwalk's damage lands (INF: none pending).
    r_land_at: f64,
    /// The stack count that cast used, held until it lands.
    pending_r_stacks: i64,
}

impl GenDriver {
    /// Riftwalk's stacks in effect at time `t` (they lapse once their 15s
    /// duration, refreshed on every cast, runs out).
    fn effective_r_stacks(&self, t: f64) -> i64 {
        if t < self.s.r_stack_expiry {
            self.s.r_stacks
        } else {
            0
        }
    }

    /// Riftwalk's mana cost at a given held-stack count: base x ratio^stacks.
    fn r_cast_cost(&self, stacks: i64) -> f64 {
        let mut mult = 1.0;
        let mut i = 0;
        while i < stacks {
            mult *= self.r_cost_ratio;
            i += 1;
        }
        self.r_base_cost * mult
    }

    /// Force Pulse's passive: any ability cast shaves its current cooldown.
    fn reduce_e_cd(&mut self, t: f64) {
        self.s.e_ready = pymax(t, self.s.e_ready - self.e_cdr);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let mana_start_pct = kit.num("gen.P.manaStartFullPct")?;
        let state = State {
            mana: sheet.mana * mana_start_pct / 100.0,
            e_ready: 0.0,
            w_ready: 0.0,
            w_armed: false,
            w_armed_until: INF,
            just_reset: false,
            r_ready: 0.0,
            r_stacks: 0,
            r_stack_expiry: 0.0,
            r_land_at: INF,
            pending_r_stacks: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("kassadin kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cost: kit.at_rank("gen.Q.costMana", ranks.q)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cost: kit.at_rank("gen.E.costMana", ranks.e)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cdr: kit.num("gen.E.cdrPerCastS")?,
            w_active_dmg: kit.hit("gen.W.activeDamage", ranks.w, sheet)?,
            w_onhit_dmg: kit.hit("gen.W.onhitDamage", ranks.w, sheet)?,
            w_window_s: kit.num("gen.W.windowS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            r_base_dmg: kit.hit("gen.R.baseDamage", ranks.r, sheet)?,
            r_stack_dmg: kit.hit("gen.R.stackDamage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_time_s: kit.num("gen.R.castTimeS")?,
            r_stack_dur: kit.num("gen.R.stackDurationS")?,
            r_max_stacks: kit.num("gen.R.maxStacks")? as i64,
            r_base_cost: kit.num("gen.R.baseCostMana")?,
            r_cost_ratio: kit.num("gen.R.costRatio")?,
            src_w_onhit: intern("W onhit"),
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
        shave(&mut self.s.e_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // the passive on-hit only applies when this attack is NOT the
        // Nether Blade-empowered one (the active's damage replaces it)
        if self.ranks.w > 0 && !self.s.w_armed {
            e.deal(self.w_onhit_dmg, DType::Magic, self.src_w_onhit, false, false, 1.0);
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.w_armed {
            self.s.w_armed = false;
            self.s.w_armed_until = INF;
            // cdstart = post-effect: the cooldown starts as the attack lands
            self.s.w_ready = e.st.t + e.basic_cd(self.w_cd);
            e.deal(self.w_active_dmg, DType::Magic, SRC_W, false, true, 1.0);
            self.s.just_reset = true;
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.s.just_reset {
            // Nether Blade's landing reset: the next attack is a bare windup
            self.s.just_reset = false;
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
        // arm Nether Blade for the following attack as soon as it is ready
        if self.ranks.w > 0 && !self.s.w_armed && t >= self.s.w_ready {
            self.s.w_armed = true;
            self.s.w_armed_until = t + self.w_window_s;
            self.reduce_e_cd(t);
            e.prime_spellblade();
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.mana < self.q_cost {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        self.s.mana -= self.q_cost;
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        self.reduce_e_cd(e.st.t);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack past the 0.25s cast
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        let stacks = self.effective_r_stacks(t);
        let cost = self.r_cast_cost(stacks);
        if self.s.mana < cost {
            return;
        }
        self.s.mana -= cost;
        self.s.pending_r_stacks = stacks;
        self.s.r_land_at = t + self.r_cast_time_s;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.reduce_e_cd(t);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            let ready_t = pymax(self.s.e_ready, e.st.t);
            if self.s.mana >= self.e_cost {
                out[n] = (ready_t, Kind::Ev(EV_E_CAST));
                n += 1;
            }
        }
        if self.s.r_land_at != INF {
            out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
            n += 1;
        } else if self.ranks.r > 0 {
            let ready_t = pymax(self.s.r_ready, e.st.t);
            let stacks = self.effective_r_stacks(ready_t);
            let cost = self.r_cast_cost(stacks);
            if self.s.mana >= cost {
                out[n] = (ready_t, Kind::Ev(EV_R_CAST));
                n += 1;
            }
        }
        if self.s.w_armed {
            out[n] = (self.s.w_armed_until, Kind::Ev(EV_W_EXPIRE));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.mana -= self.e_cost;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.reduce_e_cd(t);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_R_CAST) => {
                let stacks = self.effective_r_stacks(t);
                let cost = self.r_cast_cost(stacks);
                self.s.mana -= cost;
                self.s.pending_r_stacks = stacks;
                self.s.r_land_at = t + self.r_cast_time_s;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                self.reduce_e_cd(t);
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                let stacks = self.s.pending_r_stacks;
                let dmg = self.r_base_dmg + self.r_stack_dmg * (stacks as f64);
                e.deal(dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.s.r_stacks = imin(stacks + 1, self.r_max_stacks);
                self.s.r_stack_expiry = t + self.r_stack_dur;
            }
            Kind::Ev(EV_W_EXPIRE) => {
                self.s.w_armed = false;
                self.s.w_armed_until = INF;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
