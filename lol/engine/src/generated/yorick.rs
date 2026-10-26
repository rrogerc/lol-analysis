//! Yorick. No allies, summons or minions exist in this fight, so his Mist
//! Walkers, the Maiden of the Mist and Touch of the Maiden (which only ever
//! triggers off the Maiden's own attacks) never happen: only his personal
//! actions matter. He auto-attacks continuously, weaving in Last Rites (Q,
//! no cast time; casting it resets his attack timer and arms his very next
//! attack with bonus physical damage, its own cooldown starting only once
//! that attack lands) and Mourning Mist (E, a 0.25 s cast that nukes and
//! armor-shreds the target, keeping Yorick busy for that long) on cooldown.
//! R and W are never cast: R has no direct damage of its own here and W
//! deals 0 true damage.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Mourning Mist is cast, on cooldown.
const EV_E_CAST: u8 = 0;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    e_cd: f64,
    e_cast_s: f64,
    /// Mourning Mist's percent-of-target-max-health rate: base at this rank,
    /// plus this many extra percentage points per 100 AP.
    e_base_pct: f64,
    e_ap_per_100: f64,
    e_shred_dur: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// Last Rites is armed and waiting for the next attack to land.
    q_armed: bool,
    e_ready: f64,
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
            q_armed: false,
            e_ready: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("yorick kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_base_pct: kit.at_rank("gen.E.healthDamagePctBase", ranks.e)?,
            e_ap_per_100: kit.num("gen.E.apPer100Pct")?,
            e_shred_dur: kit.num("gen.E.shred.durationS")?,
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
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // Last Rites' empowered attack: its bonus rides the attack that
        // consumes it, and its cooldown starts only now (post-effect)
        if self.s.q_armed {
            self.s.q_armed = false;
            e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
            e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.q_armed {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // no cast time: it arms the next attack and resets the attack timer
        self.s.q_armed = true;
        e.ability_cast_proc();
        e.prime_spellblade();
        let b = self.bonus_as(e.st.t);
        e.st.next_attack = e.st.t + e.attack_windup(b, self.windup_fraction);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        match kind {
            Kind::Ev(EV_E_CAST) => {
                let t = e.st.t;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                let ap = e.p.sheet.ap;
                let pct = self.e_base_pct + ap / 100.0 * self.e_ap_per_100;
                let dmg = pct / 100.0 * e.target_hp;
                e.deal(dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.st.shred_until = t + self.e_shred_dur;
                self.busy_for(e, self.e_cast_s);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
