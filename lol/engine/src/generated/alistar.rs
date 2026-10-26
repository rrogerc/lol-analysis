//! Alistar. A melee brawler whose damage comes from casting his three basic
//! abilities on cooldown: Pulverize (Q) is a plain cast (0.25 s cast time,
//! so it keeps Alistar busy while it resolves), Headbutt (W) is a plain
//! instant cast, and Trample (E) ticks every 0.5 s for 5 s while building
//! stacks that arm his next basic attack's on-hit bonus damage. Unbreakable
//! Will (R) deals no damage and is never cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Headbutt is cast; Trample is (re)cast; a Trample tick lands.
const EV_W: u8 = 0;
const EV_E_CAST: u8 = 1;
const EV_E_TICK: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    /// Pulverize's cast time: standard (unbuffered), since Q and W are cast
    /// independently in this rotation.
    q_cast_s: f64,
    w_dmg: f64,
    w_cd: f64,
    /// Trample's damage per tick (its total damage split over its ticks).
    e_tick_dmg: f64,
    e_tick_count: i64,
    e_tick_interval: f64,
    e_cd: f64,
    e_max_stacks: i64,
    /// How long the 5-stack empowered attack stays armed.
    e_armed_window: f64,
    /// Trample's bonus on-hit proc damage, by the caster's level.
    e_bonus_dmg: f64,
    src_e_onhit: SourceId,
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
    /// When the pending Trample tick lands (INF: no ticks pending).
    e_next_tick: f64,
    e_ticks_left: i64,
    e_stacks: i64,
    /// Whether the empowered attack is armed, and until when.
    e_armed: bool,
    e_armed_until: f64,
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
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let e_total = kit.hit("gen.E.tickDamage", ranks.e, sheet)?;
        let e_tick_count_f = kit.num("gen.E.tickCount")?;
        if e_tick_count_f <= 0.0 {
            return Err("gen.E.tickCount must be positive".to_string());
        }
        let e_tick_count = e_tick_count_f as i64;
        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            e_ready: 0.0,
            e_next_tick: INF,
            e_ticks_left: 0,
            e_stacks: 0,
            e_armed: false,
            e_armed_until: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("alistar kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_tick_dmg: e_total / e_tick_count_f,
            e_tick_count,
            e_tick_interval: kit.num("gen.E.tickIntervalS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_max_stacks: kit.num("gen.E.maxStacks")? as i64,
            e_armed_window: kit.num("gen.E.armedWindowS")?,
            e_bonus_dmg: kit.at_level("gen.E.bonusDamageByLevel", level)?,
            src_e_onhit: intern("E onhit"),
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
        0.0
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Trample's 5-stack empowered attack: an on-hit proc, not an attack
        // reset, that consumes the stacks for bonus magic damage.
        if self.s.e_armed && e.st.t <= self.s.e_armed_until {
            self.s.e_armed = false;
            self.s.e_stacks = 0;
            e.deal(self.e_bonus_dmg, DType::Magic, self.src_e_onhit, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // a plain cast with a real cast time: it lands with the cast, then
        // keeps Alistar busy (no other cast, no attack) until it ends
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.e_ticks_left > 0 {
            // a tick, not a cast: it always lands on its own schedule
            out[n] = (self.s.e_next_tick, Kind::Ev(EV_E_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                // no cast time: lands with the cast, holds nothing else up
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_CAST) => {
                // no cast time. cdstart = on-cast: the cooldown begins now,
                // not when the ticking ends. A new cast never overlaps the
                // previous one's ticks (cooldown always exceeds the 5 s
                // duration), so it is safe to reset the stacks here.
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_next_tick = t + self.e_tick_interval;
                self.s.e_ticks_left = self.e_tick_count;
                self.s.e_stacks = 0;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_TICK) => {
                e.deal(self.e_tick_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.s.e_stacks = imin(self.s.e_stacks + 1, self.e_max_stacks);
                if self.s.e_stacks >= self.e_max_stacks {
                    self.s.e_armed = true;
                    self.s.e_armed_until = t + self.e_armed_window;
                }
                self.s.e_ticks_left -= 1;
                if self.s.e_ticks_left > 0 {
                    self.s.e_next_tick = t + self.e_tick_interval;
                } else {
                    self.s.e_next_tick = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
