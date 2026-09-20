//! Lee Sin. Sonic Wave (Q1) opens the fight, Resonating Strike (Q2) is
//! recast the instant it lands, Tempest (E) and Dragon's Rage (R) go out on
//! cooldown; Flurry (P) arms +40% bonus attack speed on the next 2 attacks
//! after every damaging cast. Safeguard/Iron Will (W) and Cripple never fire
//! since neither deals damage to the dummy.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Tempest is cast on cooldown; Dragon's Rage is cast on cooldown (its
/// opening cast is handled by `cast_r`, only recasts use this event).
const EV_E_CAST: u8 = 0;
const EV_R_CAST: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    p_as_pct: f64,
    p_window_s: f64,
    q_cd: f64,
    q1_dmg: f64,
    q2_dmg: f64,
    q_missing_mod: f64,
    e_dmg: f64,
    e_cd: f64,
    r_dmg: f64,
    r_cd: f64,
    src_q2: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Flurry: attacks still empowered, and how long that lasts.
    p_flurry_charges: i64,
    p_flurry_until: f64,
    /// Sonic Wave has landed and Resonating Strike is due immediately.
    q_pending_recast: bool,
    e_ready: f64,
    /// Dragon's Rage's next ready time; INF until the opening cast arms it.
    r_ready: f64,
}

impl GenDriver {
    /// Arms Flurry: the next 2 attacks that land within the window gain
    /// bonus attack speed.
    fn arm_flurry(&mut self, t: f64) {
        self.s.p_flurry_charges = 2;
        self.s.p_flurry_until = t + self.p_window_s;
    }

    /// Dragon's Rage's single-target hit, used for both the opening cast
    /// and any later recast.
    fn fire_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.arm_flurry(t);
        e.lockout();
        self.s.r_ready = t + e.ult_cd(self.r_cd);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_flurry_charges: 0,
            p_flurry_until: 0.0,
            q_pending_recast: false,
            e_ready: 0.0,
            r_ready: INF,
        };
        let _ = level;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("leesin kit needs attack.windupFraction")?,
            p_as_pct: kit.num("gen.P.asPct")?,
            p_window_s: kit.num("gen.P.windowS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q1_dmg: kit.hit("gen.Q.q1Damage", ranks.q, sheet)?,
            q2_dmg: kit.hit("gen.Q.q2Damage", ranks.q, sheet)?,
            q_missing_mod: kit.num("gen.Q.missingHealthMod")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            src_q2: intern("Q recast"),
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
        if self.s.p_flurry_charges > 0 && t < self.s.p_flurry_until {
            self.p_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        if self.s.p_flurry_charges > 0 && e.st.t < self.s.p_flurry_until {
            self.s.p_flurry_charges -= 1;
        } else {
            self.s.p_flurry_charges = 0;
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.q_pending_recast {
            return e.st.t;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.q_pending_recast {
            // Resonating Strike: consumes the mark, scaled by the target's
            // missing health (0 to 100% bonus damage).
            self.s.q_pending_recast = false;
            let missing = (e.target_hp - pymax(e.st.hp, 0.0)) / e.target_hp;
            let missing = pymax(pymin(missing, 1.0), 0.0);
            let amt = self.q2_dmg * (1.0 + missing * self.q_missing_mod);
            e.deal(amt, DType::Physical, self.src_q2, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            e.prime_spellblade();
            self.arm_flurry(t);
        } else {
            // Sonic Wave: the cooldown starts here, not on the recast.
            e.st.q_ready = t + e.basic_cd(self.q_cd);
            e.deal(self.q1_dmg, DType::Physical, SRC_Q, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            e.prime_spellblade();
            self.arm_flurry(t);
            self.s.q_pending_recast = true;
            e.lockout();
        }
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.fire_r(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 && self.s.r_ready != INF {
            out[n] = (self.s.r_ready, Kind::Ev(EV_R_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.arm_flurry(t);
                e.lockout();
            }
            Kind::Ev(EV_R_CAST) => {
                self.fire_r(e);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
