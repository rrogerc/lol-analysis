//! Lissandra. A caster whose damage is entirely her Q/W/E cast on cooldown
//! (Q wired through the engine, W and E as self-scheduled events) and a
//! single opening Frozen Tomb enemy-cast whose long cooldown means it is
//! never realistically recast inside the fight. Glacial Path's recast is
//! fired purely for on-cast procs (it deals no damage of its own). Casts go
//! one at a time: a single `busy_until` in the state keeps a cast with a
//! cast time (Q, E's initial cast, R) from overlapping any other cast or
//! attack.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Frozen Tomb's ice-field damage, landing after its enemy-cast completes.
const EV_R_DAMAGE: u8 = 0;
/// Ring of Frost is cast (no cast time).
const EV_W_CAST: u8 = 1;
/// Glacial Path is cast; then its damage-less recast (blink).
const EV_E_CAST: u8 = 2;
const EV_E_RECAST: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_dmg: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    e_recast_s: f64,
    r_dmg: f64,
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
    /// When the pending Glacial Path recast may fire (INF: none pending).
    e_recast_at: f64,
    /// When Frozen Tomb's ice-field damage lands (INF: none pending / done).
    r_damage_at: f64,
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
            w_ready: 0.0,
            e_ready: 0.0,
            e_recast_at: INF,
            r_damage_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_recast_s: kit.num("gen.E.recastDelayS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
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
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // The opening cast; the engine has already primed Spellblade and
        // held the first attack. The field's damage lands when the
        // enemy-cast's own cast time finishes, and the cast time itself
        // keeps Lissandra busy until then.
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_damage_at = e.st.t + self.r_cast_s;
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_damage_at != INF {
            out[n] = (self.s.r_damage_at, Kind::Ev(EV_R_DAMAGE));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_recast_at != INF {
                out[n] = (self.castable_at(e, self.s.e_recast_at), Kind::Ev(EV_E_RECAST));
            } else {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_DAMAGE) => {
                self.s.r_damage_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_CAST) => {
                // Cooldown starts on-cast, not on the recast.
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_recast_at = t + self.e_recast_s;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_E_RECAST) => {
                // The blink itself deals no damage and has no cast time,
                // but as a dash it still locks out the next attack.
                self.s.e_recast_at = INF;
                e.prime_spellblade();
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
