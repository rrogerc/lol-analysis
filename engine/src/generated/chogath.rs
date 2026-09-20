//! Cho'Gath. Feast opens the fight and is recast on cooldown for true
//! damage; Rupture and Feral Scream go out on cooldown as plain-cast magic
//! nukes; Vorpal Spikes is woven in right after an attack lands (for its
//! attack-timer reset) whenever it is off cooldown, then its 3 charges ride
//! the next 3 attacks as on-hit magic damage scaling off the target's max
//! health.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Feral Scream is cast on cooldown.
const EV_W: u8 = 0;
/// Feast is recast on cooldown (the opening cast happens in `cast_r`).
const EV_R: u8 = 1;
/// Vorpal Spikes' 6 s window lapses with charges still unused.
const EV_E_EXPIRE: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    e_dmg: f64,
    /// Vorpal Spikes' target-max-health-% term, as a fraction (0-stack value).
    e_hp_frac: f64,
    e_cd: f64,
    e_attacks_per_cast: i64,
    e_window_s: f64,
    r_dmg: f64,
    r_cd: f64,
    src_w: SourceId,
    src_e: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    /// When Vorpal Spikes may next be cast (INF while its charges are still
    /// pending: the cooldown starts post-effect).
    e_ready: f64,
    /// Empowered attacks still to be consumed (0-3).
    e_charges: i64,
    /// The 6 s window they must land within (INF while unarmed).
    e_window_until: f64,
    /// When Feast may next be cast (INF only before the opening cast runs).
    r_ready: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let _ = level;
        let state = State {
            w_ready: 0.0,
            e_ready: 0.0,
            e_charges: 0,
            e_window_until: INF,
            r_ready: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("chogath kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_hp_frac: kit.at_rank("gen.E.targetMaxHpPct", ranks.e)? / 100.0,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_attacks_per_cast: kit.num("gen.E.attacksPerCast")? as i64,
            e_window_s: kit.num("gen.E.windowS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            src_w: intern("W"),
            src_e: intern("E onhit"),
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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.ranks.e == 0 {
            return;
        }
        if self.s.e_charges > 0 && e.st.t <= self.s.e_window_until {
            self.s.e_charges -= 1;
            let amt = self.e_dmg + self.e_hp_frac * e.target_hp;
            e.deal(amt, DType::Magic, self.src_e, false, false, 1.0);
            if self.s.e_charges == 0 {
                self.s.e_ready = e.st.t + e.basic_cd(self.e_cd);
                self.s.e_window_until = INF;
            }
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.ranks.e > 0
            && self.s.e_charges == 0
            && self.s.e_window_until == INF
            && t >= self.s.e_ready
        {
            // Vorpal Spikes, woven in right after an attack for the reset
            self.s.e_charges = self.e_attacks_per_cast;
            self.s.e_window_until = t + self.e_window_s;
            e.prime_spellblade();
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
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
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack past the 0.25 s cast
        let t = e.st.t;
        e.deal(self.r_dmg, DType::True, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.ult_hatefog();
        self.s.r_ready = t + e.ult_cd(self.r_cd);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.r > 0 {
            out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R));
            n += 1;
        }
        if self.s.e_charges > 0 {
            out[n] = (self.s.e_window_until, Kind::Ev(EV_E_EXPIRE));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, self.src_w, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_R) => {
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.deal(self.r_dmg, DType::True, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
                e.ult_hatefog();
            }
            Kind::Ev(EV_E_EXPIRE) => {
                // the window lapsed with charges unused: the cooldown starts
                // now, post-effect
                self.s.e_charges = 0;
                self.s.e_window_until = INF;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
