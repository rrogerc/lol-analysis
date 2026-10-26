//! Vex. A pure ability-caster who weaves basic attacks between casts:
//! Shadow Surge opens the fight with its initial hit and an immediate,
//! cast-time-free recast for the mark-consume damage; Mistral Bolt,
//! Personal Space and Looming Darkness are then cast as soon as each is off
//! cooldown and Vex is free. Casts go one at a time: one busy_until in the
//! state holds off every other cast and any attack until a cast's own cast
//! time has passed. Doom 'n Gloom never procs against a stationary target
//! and is not modeled.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Personal Space is cast when off cooldown and free.
const EV_W: u8 = 0;
/// Looming Darkness is cast when off cooldown and free.
const EV_E: u8 = 1;
/// Shadow Surge's initial hit lands (its cast time after the opening cast).
const EV_R: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_dmg: f64,
    w_cd: f64,
    w_cast_s: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    r_initial_dmg: f64,
    r_recast_dmg: f64,
    r_cast_s: f64,
    src_r_recast: SourceId,
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
    /// When Shadow Surge's initial hit lands (INF: none pending).
    r_fire_at: f64,
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
            r_fire_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_initial_dmg: kit.hit("gen.R.initial", ranks.r, sheet)?,
            r_recast_dmg: kit.hit("gen.R.recast", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_r_recast: intern("R recast"),
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
        // the opening cast (the engine has primed Spellblade and held the
        // first attack past 0.25 s already): the initial hit lands when the
        // cast time ends
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_fire_at = e.st.t + self.r_cast_s;
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E));
            n += 1;
        }
        if self.s.r_fire_at != INF {
            out[n] = (self.s.r_fire_at, Kind::Ev(EV_R));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.w_cast_s);
            }
            Kind::Ev(EV_E) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_R) => {
                self.s.r_fire_at = INF;
                // initial hit: Spellblade was already primed at the opening cast
                e.deal(self.r_initial_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                // immediate, cast-time-free recast consuming the mark: no
                // further busy_for, it costs no time
                e.deal(self.r_recast_dmg, DType::Magic, self.src_r_recast, false, true, 1.0);
                e.prime_spellblade();
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
