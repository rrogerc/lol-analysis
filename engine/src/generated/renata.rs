//! Renata Glasc. Leverage marks the dummy on Renata's first attack for a
//! one-time bonus (it never expires against a single stationary target);
//! Bailout is self-cast immediately for a ramping attack-speed buff (it
//! deals no direct damage of its own); Handshake goes out on cooldown with
//! its recast landing right after; Loyalty Program is cast on cooldown for
//! one magic-damage hit; Hostile Takeover is never cast since it deals no
//! damage here.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Bailout is cast on cooldown (self-target).
const EV_W_CAST: u8 = 0;
/// Loyalty Program is cast on cooldown.
const EV_E_CAST: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Leverage: fraction of the target's max HP (plus AP) per proc, and
    /// how long the mark lasts.
    p_pct_frac: f64,
    p_mark_dur_s: f64,
    src_p: SourceId,
    q_dmg: f64,
    q_cd: f64,
    src_q_recast: SourceId,
    /// Bailout: base bonus attack speed in PERCENT (already includes the AP
    /// ratio, constant for the fight), and how long it ramps/lasts.
    w_as_pct: f64,
    w_dur_s: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// When Leverage's mark on the dummy expires (0.0: never marked yet).
    p_mark_until: f64,
    w_ready: f64,
    w_cast_at: f64,
    /// When Bailout's buff ends (-1.0: never cast yet).
    w_active_until: f64,
    e_ready: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_mark_until: 0.0,
            w_ready: 0.0,
            w_cast_at: 0.0,
            w_active_until: -1.0,
            e_ready: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("renata kit needs attack.windupFraction")?,
            p_pct_frac: kit.at_level("gen.P.pctByLevel", level)? + sheet.ap * kit.num("gen.P.apRatio")?,
            p_mark_dur_s: kit.num("gen.P.markDurationS")?,
            src_p: intern("P"),
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            src_q_recast: intern("Q recast"),
            w_as_pct: kit.at_rank("gen.W.asBase", ranks.w)? + sheet.ap * kit.num("gen.W.apRatioPctPerAp")?,
            w_dur_s: kit.num("gen.W.durationS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
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
        if t >= self.s.w_active_until {
            return 0.0;
        }
        let elapsed = t - self.s.w_cast_at;
        let ramp = pymin(elapsed / self.w_dur_s, 1.0);
        self.w_as_pct * (1.0 + ramp)
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        // Leverage: the mark procs its bonus only on the hit that first
        // applies it; against this single target it then just refreshes.
        let t = e.st.t;
        if t >= self.s.p_mark_until {
            e.deal(self.p_pct_frac * e.target_hp, DType::Magic, self.src_p, false, false, 1.0);
        }
        self.s.p_mark_until = t + self.p_mark_dur_s;
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // The initial hook, then the recast knockback, both landing on the
        // same instant (its travel and the tether are not modeled beyond
        // the usual cast lockout); the cooldown starts once both land.
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.deal(self.q_dmg, DType::Magic, self.src_q_recast, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.lockout();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
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
            Kind::Ev(EV_W_CAST) => {
                // no cast time: an instant self-buff, cooldown starts on-cast;
                // it deals no direct damage of its own, only the attack speed
                // ramp read through bonus_as
                self.s.w_cast_at = t;
                self.s.w_active_until = t + self.w_dur_s;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
