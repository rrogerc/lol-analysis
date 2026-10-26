//! Wukong. A melee auto-attacker whose kit rides his attacks: Crushing Blow
//! arms his next attack for bonus damage, an armor shred and an attack
//! reset, and every damage instance shaves time off its own cooldown;
//! Nimbus Strike is cast on cooldown for magic damage and bonus attack
//! speed; Cyclone opens the fight and runs both its 2s spins back to back
//! for maximum cooldown-reduction value on Crushing Blow. Warrior
//! Trickster's clone is not modeled (see kit notes).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// A Cyclone tick (0.25s); the fixed recast between its two spins; Nimbus
/// Strike's cast, on cooldown.
const EV_R_TICK: u8 = 0;
const EV_R_RECAST: u8 = 1;
const EV_E_CAST: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cdr_per_hit: f64,
    q_shred_dur: f64,
    e_dmg: f64,
    e_cd: f64,
    e_as_pct: f64,
    e_as_dur: f64,
    r_tick_ad: f64,
    r_tick_hp_ratio: f64,
    r_tick_interval: f64,
    r_spin_dur: f64,
    r_recast_lockout: f64,
    ticks_per_spin: i64,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_armed: bool,
    /// Whether the attack that just landed was Crushing Blow's, so
    /// `schedule_attack` gives it the reset instead of a normal period.
    q_reset_pending: bool,
    e_ready: f64,
    e_as_until: f64,
    /// Cyclone: when it was cast, which tick is next (0..2*ticks_per_spin),
    /// and whether the fixed mid-cast recast has fired yet.
    r_t0: f64,
    r_tick_i: i64,
    r_recast_done: bool,
}

impl GenDriver {
    /// Every instance of Wukong's damage shaves 0.5s off Crushing Blow's
    /// cooldown, per its own description.
    fn apply_q_cdr(&self, e: &mut Engine) {
        e.st.q_ready = e.st.q_ready - self.q_cdr_per_hit;
    }

    /// The absolute time of the next Cyclone tick, given how many have
    /// already landed.
    fn r_next_tick_time(&self) -> f64 {
        let i = self.s.r_tick_i;
        if i < self.ticks_per_spin {
            self.s.r_t0 + self.r_tick_interval * ((i + 1) as f64)
        } else {
            self.s.r_t0
                + self.r_spin_dur
                + self.r_recast_lockout
                + self.r_tick_interval * ((i - self.ticks_per_spin + 1) as f64)
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let ticks_per_spin_f = kit.num("gen.R.spinDurationS")? / kit.num("gen.R.tickIntervalS")?;
        let ticks_per_spin = (ticks_per_spin_f + 0.5) as i64;
        let state = State {
            q_armed: false,
            q_reset_pending: false,
            e_ready: 0.0,
            e_as_until: -1.0,
            r_t0: 0.0,
            r_tick_i: 0,
            r_recast_done: false,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit
                .windup_fraction
                .ok_or("monkeyking kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cdr_per_hit: kit.num("gen.Q.cdrPerHitS")?,
            q_shred_dur: kit.num("abilities.Q.shred.durationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_as_pct: kit.at_rank("gen.E.attackSpeedBonusPct", ranks.e)? * 100.0,
            e_as_dur: kit.num("gen.E.attackSpeedDurationS")?,
            r_tick_ad: kit.hit("gen.R.tickDamage", ranks.r, sheet)?,
            r_tick_hp_ratio: kit.at_rank("gen.R.tickMaxHpRatio", ranks.r)?,
            r_tick_interval: kit.num("gen.R.tickIntervalS")?,
            r_spin_dur: kit.num("gen.R.spinDurationS")?,
            r_recast_lockout: kit.num("gen.R.recastLockoutS")?,
            ticks_per_spin,
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
        if t < self.s.e_as_until {
            self.e_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // this landed attack is itself a damage instance
        self.apply_q_cdr(e);
        if self.s.q_armed {
            self.s.q_armed = false;
            self.s.q_reset_pending = true;
            let t = e.st.t;
            e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            e.st.shred_until = t + self.q_shred_dur;
            e.st.q_ready = t + e.basic_cd(self.q_cd);
            // and so is the bonus damage that just landed alongside it
            self.apply_q_cdr(e);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.s.q_reset_pending {
            // Crushing Blow resets Wukong's attack timer
            self.s.q_reset_pending = false;
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.q_armed {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // arms the next attack; the damage lands (and the cooldown starts)
        // when that attack connects, in after_attack
        self.s.q_armed = true;
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_t0 = e.st.t;
        self.s.r_tick_i = 0;
        self.s.r_recast_done = false;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.r > 0 {
            let total = 2 * self.ticks_per_spin;
            if self.s.r_tick_i < total {
                out[n] = (self.r_next_tick_time(), Kind::Ev(EV_R_TICK));
                n += 1;
            }
            if !self.s.r_recast_done {
                let recast_at = self.s.r_t0 + self.r_spin_dur + self.r_recast_lockout;
                out[n] = (recast_at, Kind::Ev(EV_R_RECAST));
                n += 1;
            }
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_TICK) => {
                let i = self.s.r_tick_i;
                if i == 0 || i == self.ticks_per_spin {
                    // the first tick of each spin is where its damage begins
                    e.ability_cast_proc();
                    e.eclipse_hit();
                }
                let dmg = self.r_tick_ad + self.r_tick_hp_ratio * e.target_hp;
                e.deal(dmg, DType::Physical, SRC_R, false, true, 1.0);
                self.apply_q_cdr(e);
                self.s.r_tick_i += 1;
            }
            Kind::Ev(EV_R_RECAST) => {
                self.s.r_recast_done = true;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_CAST) => {
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.apply_q_cdr(e);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.e_as_until = t + self.e_as_dur;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
