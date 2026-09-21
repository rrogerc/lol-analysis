//! Bard. A basic-attacker whose Meeps (Traveler's Call) ride his attacks
//! with bonus magic damage, resolved for an assumed Chime count; Cosmic
//! Binding goes out on cooldown for flat+AP magic damage, its 0.25 s cast
//! time keeping Bard busy until it ends. Caretaker's Shrine, Magical
//! Journey and Tempered Fate never damage the dummy and are not cast.

use crate::fight::{Driver, Engine, Events, Kind};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// A Meep spawns (or would, subject to the cap).
const EV_MEEP_SPAWN: u8 = 0;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    meep_spawn_cd: f64,
    meep_dmg_base: f64,
    meep_ap_ratio: f64,
    max_meeps: i64,
    src_meep: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    busy_until: f64,
    meep_stacks: i64,
    meep_ready_at: f64,
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
        let meep_spawn_cd = kit.num("gen.P.meepSpawnCdS")?;
        let starting_meeps = kit.num("gen.P.startingMeeps")? as i64;
        let state = State {
            busy_until: 0.0,
            meep_stacks: starting_meeps,
            meep_ready_at: meep_spawn_cd,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            meep_spawn_cd,
            meep_dmg_base: kit.num("gen.P.meepDamageBase")?,
            meep_ap_ratio: kit.num("gen.P.meepApRatio")?,
            max_meeps: kit.num("gen.P.maxMeeps")? as i64,
            src_meep: intern("P meep"),
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

    fn attack_riders(&mut self, e: &mut Engine) {
        // a Meep, if banked, is consumed on-attack for bonus magic damage
        if self.s.meep_stacks > 0 {
            self.s.meep_stacks -= 1;
            let dmg = self.meep_dmg_base + self.meep_ap_ratio * e.p.sheet.ap;
            e.deal(dmg, DType::Magic, self.src_meep, false, false, 1.0);
        }
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

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        out[0] = (pymax(self.s.meep_ready_at, e.st.t), Kind::Ev(EV_MEEP_SPAWN));
        1
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        match kind {
            Kind::Ev(EV_MEEP_SPAWN) => {
                self.s.meep_stacks = imin(self.s.meep_stacks + 1, self.max_meeps);
                self.s.meep_ready_at = e.st.t + self.meep_spawn_cd;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
