//! Anivia. A pure caster whose damage is entirely from Flash Frost, Frostbite
//! and Glacial Storm: R is toggled on once and left running for the whole
//! fight, Q is cast (both the passthrough and its immediate recast, sharing
//! one 0.25 s cast time) on cooldown, and E is recast on cooldown, doubled
//! whenever the target is Iced (which Q's own casts, and later every
//! fully-formed tick of R, keep almost permanently active). Casts go one at
//! a time: Q and E each keep Anivia busy for their 0.25 s cast time, so
//! whichever comes due next waits for the other to finish.

use crate::fight::{Driver, Engine, Events, Kind};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Frostbite recast when it comes off cooldown; Glacial Storm's next tick.
const EV_E_CAST: u8 = 0;
const EV_R_TICK: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    ranged: bool,
    q_passthrough: f64,
    q_explosion: f64,
    q_cd: f64,
    q_cast_s: f64,
    iced_duration_s: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    e_iced_mult: f64,
    r_tick_dmg: f64,
    r_tick_interval: f64,
    r_growth_mult: f64,
    r_empowered_mult: f64,
    n_growth_ticks: i64,
    src_q_explosion: SourceId,
    src_r_growth: SourceId,
    src_r_empowered: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// The target is Iced until this time (doubles Frostbite).
    iced_until: f64,
    e_ready: f64,
    /// How many Glacial Storm ticks have fired so far.
    r_tick_index: i64,
    /// The time of the next Glacial Storm tick (INF: not toggled on).
    r_next_tick: f64,
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
            iced_until: -INF,
            e_ready: 0.0,
            r_tick_index: 0,
            r_next_tick: INF,
        };
        let growth_time_s = kit.num("gen.R.growthTimeS")?;
        let r_tick_interval = kit.num("gen.R.tickIntervalS")?;
        let n_growth_ticks = (growth_time_s / r_tick_interval).round() as i64;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            ranged: sheet.base_attack_range > MELEE_MAX_RANGE,
            q_passthrough: kit.hit("gen.Q.passthrough", ranks.q, sheet)?,
            q_explosion: kit.hit("gen.Q.explosion", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            iced_duration_s: kit.num("gen.Q.icedDurationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_iced_mult: kit.num("gen.E.icedMultiplier")?,
            r_tick_dmg: kit.hit("gen.R.tickDamage", ranks.r, sheet)?,
            r_tick_interval,
            r_growth_mult: kit.num("gen.R.growthTickMultiplier")?,
            r_empowered_mult: kit.num("gen.R.empoweredMultiplier")?,
            n_growth_ticks,
            src_q_explosion: intern("Q explosion"),
            src_r_growth: intern("R growth tick"),
            src_r_empowered: intern("R empowered tick"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        self.ranged
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        // cooldown starts post-effect; since both hits are modeled as
        // landing together at the cast, that is now
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_passthrough, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.iced_until = t + self.iced_duration_s;
        // the manual recast: a separate ability activation
        e.deal(self.q_explosion, DType::Magic, self.src_q_explosion, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.iced_until = t + self.iced_duration_s;
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // toggling on has no cast time and is not an ability activation:
        // no busy_for, no on-cast procs here
        self.s.r_tick_index = 0;
        self.s.r_next_tick = e.st.t + self.r_tick_interval;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 && self.s.r_next_tick != INF {
            // a tick, not a cast: it reports its own time
            out[n] = (self.s.r_next_tick, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                let mut dmg = self.e_dmg;
                if t < self.s.iced_until {
                    dmg = dmg * self.e_iced_mult;
                }
                e.deal(dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_R_TICK) => {
                self.s.r_tick_index += 1;
                self.s.r_next_tick = t + self.r_tick_interval;
                if self.s.r_tick_index <= self.n_growth_ticks {
                    let dmg = self.r_tick_dmg * self.r_growth_mult;
                    e.deal(dmg, DType::Magic, self.src_r_growth, false, true, 1.0);
                } else {
                    let dmg = self.r_tick_dmg * self.r_empowered_mult;
                    e.deal(dmg, DType::Magic, self.src_r_empowered, false, true, 1.0);
                    // only a fully-formed tick refreshes the Iced mark
                    self.s.iced_until = t + self.iced_duration_s;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
