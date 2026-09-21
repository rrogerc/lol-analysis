//! Syndra. A burst mage: Dark Sphere is recast on cooldown (its cooldown reduced
//! by Unleashed Power's passive ability haste), its delayed sphere is grabbed and
//! immediately thrown by Force of Will the moment the latter is off cooldown,
//! Scatter the Weak is cast on cooldown, and Unleashed Power opens the fight with
//! the minimum three self-conjured spheres since none are banked yet. Its effect
//! lands at the start of the 0.25 s cast, which then keeps Syndra busy until it ends.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Dark Sphere's delayed landing (damage + a Dark Sphere becomes available).
const EV_Q_LAND: u8 = 0;
/// Scatter the Weak: the cast begins, then its damage lands at cast time end.
const EV_E_CAST: u8 = 1;
const EV_E_DMG: u8 = 2;
/// Force of Will's grab-and-throw, taken the instant a sphere is available.
const EV_W_CAST: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_cd: f64,
    q_land_delay: f64,
    /// Unleashed Power's passive: flat ability haste applied to Dark Sphere only.
    q_r_haste: f64,
    q_dmg: f64,
    w_dmg: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    /// Unleashed Power's total damage: per-sphere damage times the assumed spheres.
    r_dmg_total: f64,
    r_cast_s: f64,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    w_ready: f64,
    e_ready: f64,
    /// When a pending Dark Sphere lands (INF: none pending).
    q_land_at: f64,
    /// When a cast Scatter the Weak's damage lands (INF: none pending).
    e_dmg_at: f64,
    /// Live, ungrabbed Dark Spheres available for Force of Will.
    sphere_count: i64,
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
        let q_r_haste = if ranks.r > 0 { kit.at_rank("gen.R.qHasteByRank", ranks.r)? } else { 0.0 };
        let r_per_sphere = if ranks.r > 0 { kit.hit("gen.R.damagePerSphere", ranks.r, sheet)? } else { 0.0 };
        let r_spheres = kit.num("gen.R.assumedSpheres")?;
        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            e_ready: 0.0,
            q_land_at: INF,
            e_dmg_at: INF,
            sphere_count: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_land_delay: kit.num("gen.Q.landDelayS")?,
            q_r_haste,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg_total: r_per_sphere * r_spheres,
            r_cast_s: kit.num("gen.R.castTimeS")?,
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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        // ability haste from items/runes plus Unleashed Power's flat bonus on Q
        let haste = e.p.sheet.haste + self.q_r_haste;
        let cd = self.q_cd * 100.0 / (100.0 + haste);
        e.st.q_ready = t + cd;
        self.s.q_land_at = t + self.q_land_delay;
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the effect takes place at the start of the cast; the engine has
        // already primed Spellblade and delayed the first attack for this
        // opening cast; the cast time then keeps Syndra busy
        e.deal(self.r_dmg_total, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.ult_hatefog();
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let t = e.st.t;
        let mut n = 0;
        if self.s.q_land_at != INF {
            out[n] = (self.s.q_land_at, Kind::Ev(EV_Q_LAND));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_dmg_at != INF {
                out[n] = (self.s.e_dmg_at, Kind::Ev(EV_E_DMG));
            } else {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.ranks.w > 0 && self.s.sphere_count > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_LAND) => {
                self.s.q_land_at = INF;
                e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.sphere_count += 1;
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_dmg_at = t + self.e_cast_s;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_E_DMG) => {
                self.s.e_dmg_at = INF;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.sphere_count -= 1;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.prime_spellblade();
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
