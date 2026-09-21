//! Kalista. Opens with Pierce for its own damage and a first Rend stack;
//! every basic attack applies/refreshes a Rend stack on-hit, and Rend is
//! cast on cooldown to consume whatever stacks are up. Sentinel and Fate's
//! Call are never cast: Sentinel's bonus damage and Fate's Call's entire
//! kit both require an Oathsworn ally that does not exist in this fight.
//! Martial Poise's attack-speed cap is enforced as a constant negative
//! offset on the kit's own attack-speed contribution. Casts go one at a
//! time: Pierce and Rend each keep Kalista busy for their 0.25 s cast time.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Rend is cast, consuming the current stacks.
const EV_E_CAST: u8 = 0;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    e_dmg: f64,
    /// Base + AD + AP damage for each stack beyond the first, precomputed
    /// against this build's static AD/AP.
    e_add_per_stack: f64,
    e_cd: f64,
    e_cast_s: f64,
    stack_duration_s: f64,
    max_stacks: i64,
    /// The kit's own bonus-attack-speed contribution: 0, or a negative
    /// offset that cancels any of the build's attack speed past the 2.1
    /// attacks/second cap Martial Poise cannot keep up with.
    as_adjust_pct: f64,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    e_ready: f64,
    e_stacks: i64,
    e_last_stack_t: f64,
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

    /// A Rend stack lands (an attack on-hit, or Pierce): refresh the window,
    /// resetting the count first if it had already fully expired.
    fn add_stack(&mut self, t: f64) {
        if self.s.e_stacks > 0 && t - self.s.e_last_stack_t > self.stack_duration_s {
            self.s.e_stacks = 0;
        }
        self.s.e_stacks = imin(self.s.e_stacks + 1, self.max_stacks);
        self.s.e_last_stack_t = t;
    }

    /// The stacks actually available right now (0 if the window lapsed).
    fn current_stacks(&self, t: f64) -> i64 {
        if self.s.e_stacks > 0 && t - self.s.e_last_stack_t > self.stack_duration_s {
            0
        } else {
            self.s.e_stacks
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let as_cap = kit.num("gen.P.asCap")?;
        let base_as = sheet.attack_speed;
        let total_as = base_as * (1.0 + sheet.bonus_as_pct / 100.0);
        let as_adjust_pct = if total_as > as_cap {
            (as_cap / base_as - 1.0) * 100.0 - sheet.bonus_as_pct
        } else {
            0.0
        };

        let e_add_base = kit.at_rank("gen.E.additionalBase", ranks.e)?;
        let e_add_adr = kit.at_rank("gen.E.additionalBonusAdRatio", ranks.e)?;
        let e_add_apr = kit.num("gen.E.additionalApRatio")?;
        let e_add_per_stack = e_add_base + e_add_adr * sheet.ad_bonus + e_add_apr * sheet.ap;

        let state = State {
            busy_until: 0.0,
            e_ready: 0.0,
            e_stacks: 0,
            e_last_stack_t: 0.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_add_per_stack,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            stack_duration_s: kit.num("gen.E.stackDurationS")?,
            max_stacks: kit.num("gen.E.maxStacks")? as i64,
            as_adjust_pct,
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

    fn bonus_as(&self, _t: f64) -> f64 {
        self.as_adjust_pct
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Rend's passive: basic attacks apply a stack on-hit.
        self.add_stack(e.st.t);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        // Pierce also applies a Rend stack.
        self.add_stack(e.st.t);
        self.busy_for(e, self.q_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 && self.current_stacks(e.st.t) >= 1 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                let stacks = self.current_stacks(t);
                let extra = stacks - 1;
                let dmg = self.e_dmg + (extra as f64) * self.e_add_per_stack;
                e.deal(dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.e_stacks = 0;
                self.busy_for(e, self.e_cast_s);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
