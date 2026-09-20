//! Maokai. A caster who opens with Nature's Grasp (all 5 brambles landing
//! together on the stationary target when the cast finishes), then loops
//! Bramble Smash, Sapling Toss and Twisted Advance on cooldown, attacking
//! in every gap. Sap Magic (P) never procs against a dummy that never
//! damages Maokai, so it is left out entirely.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Twisted Advance is cast (instant, no cast time).
const EV_W: u8 = 0;
/// Sapling Toss is cast; its cast time is the engine's default lockout.
const EV_E: u8 = 1;
/// Nature's Grasp's five brambles land, after its 0.5s cast.
const EV_R_HIT: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_target_hp_ratio: f64,
    q_cd: f64,
    /// Bramble Smash's cast time plus its extra post-cast lockout.
    q_lockout_s: f64,
    w_dmg: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    r_dmg: f64,
    r_cast_s: f64,
    r_hits: i64,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    e_ready: f64,
    /// When Nature's Grasp's brambles land (INF: none pending).
    r_hit_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            w_ready: 0.0,
            e_ready: 0.0,
            r_hit_at: INF,
        };
        let q_cast_s = kit.num("gen.Q.castTimeS")?;
        let q_extra_lockout = kit.num("gen.Q.extraLockoutS")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_target_hp_ratio: kit.at_rank("gen.Q.targetHpRatio", ranks.q)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_lockout_s: q_cast_s + q_extra_lockout,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_hits: kit.num("gen.R.brambleCount")? as i64,
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

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        let dmg = self.q_dmg + self.q_target_hp_ratio * e.target_hp;
        e.deal(dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.st.next_attack = pymax(e.st.next_attack, t + self.q_lockout_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack; the wall's brambles land once the (longer)
        // cast time ends
        let t = e.st.t;
        self.s.r_hit_at = t + self.r_cast_s;
        e.st.next_attack = pymax(e.st.next_attack, t + self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E));
            n += 1;
        }
        if self.s.r_hit_at != INF {
            out[n] = (self.s.r_hit_at, Kind::Ev(EV_R_HIT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                // no cast time: an instant cast, no attack lockout
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                // Sapling Toss's cast time equals the engine's default lockout
                e.lockout();
            }
            Kind::Ev(EV_R_HIT) => {
                self.s.r_hit_at = INF;
                let mut i = 0;
                while i < self.r_hits {
                    e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                    i += 1;
                }
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
