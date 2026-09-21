//! Blitzcrank. Rocket Grab hits and lands a guaranteed follow-up attack;
//! Overdrive is a timed attack-speed self-buff kept refreshed; Power Fist
//! arms the next attack via an attack-reset for bonus physical damage;
//! Static Field stacks a zap per attack while off cooldown (one consumed
//! per second) and its active detonation is cast on cooldown for a magic
//! burst. Rocket Grab and Static Field's active each have a 0.25s cast time
//! that keeps Blitzcrank busy; casts go one after another.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Overdrive's refresh.
const EV_W: u8 = 0;
/// Static Field's active detonation, cast on cooldown.
const EV_R_CAST: u8 = 1;
/// Static Field's passive: one banked stack zaps the target.
const EV_R_TICK: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_as_pct: f64,
    w_duration: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    r_active_dmg: f64,
    r_tick_dmg: f64,
    r_tick_interval: f64,
    r_cd: f64,
    r_cast_s: f64,
    src_e: SourceId,
    src_r_tick: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// Overdrive: when the attack-speed buff expires, and when it may recast.
    w_until: f64,
    w_ready: f64,
    /// Power Fist: armed on the next attack, and when it may recast (post-effect).
    e_armed: bool,
    e_ready: f64,
    /// Static Field: when the active may next be cast (also the passive's
    /// off-cooldown gate), banked stacks, and the next zap's time (INF: none banked).
    r_ready: f64,
    r_stacks: i64,
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
            w_until: -1.0,
            w_ready: 0.0,
            e_armed: false,
            e_ready: 0.0,
            r_ready: 0.0,
            r_stacks: 0,
            r_next_tick: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("blitzcrank kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_as_pct: kit.at_rank("gen.W.attackSpeedPct", ranks.w)?,
            w_duration: kit.num("gen.W.durationS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_active_dmg: kit.hit("gen.R.activeDamage", ranks.r, sheet)?,
            r_tick_dmg: kit.hit("gen.R.passiveDamage", ranks.r, sheet)?,
            r_tick_interval: kit.num("gen.R.tickIntervalS")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_e: intern("E"),
            src_r_tick: intern("R tick"),
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
        if t < self.s.w_until {
            self.w_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Static Field's passive: a stack banks on-hit while the ability is
        // off cooldown; the 1-second drain timer starts with the first stack.
        if self.ranks.r > 0 && e.st.t >= self.s.r_ready {
            self.s.r_stacks += 1;
            if self.s.r_next_tick == INF {
                self.s.r_next_tick = e.st.t + self.r_tick_interval;
            }
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.e_armed {
            self.s.e_armed = false;
            self.s.e_ready = e.st.t + e.basic_cd(self.e_cd);
            e.deal(self.e_dmg, DType::Physical, self.src_e, true, true, 1.0);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.ranks.e > 0 && !self.s.e_armed && t >= self.s.e_ready {
            // Power Fist, armed right after an attack: its reset brings the
            // next attack one windup away
            self.s.e_armed = true;
            e.prime_spellblade();
            e.ability_cast_proc();
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        // the guaranteed follow-up attack, once the target has arrived: the
        // cast time, then the dossier's separate post-hit lockout, then a
        // normal windup
        let b = self.bonus_as(t);
        e.st.next_attack = pymax(
            e.st.next_attack,
            t + self.q_cast_s + ABILITY_LOCKOUT_S + e.attack_windup(b, self.windup_fraction),
        );
        self.busy_for(e, self.q_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.r > 0 {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        if self.s.r_next_tick != INF {
            out[n] = (self.s.r_next_tick, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                self.s.w_until = t + self.w_duration;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.deal(self.r_active_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                self.busy_for(e, self.r_cast_s);
            }
            Kind::Ev(EV_R_TICK) => {
                self.s.r_stacks -= 1;
                e.deal(self.r_tick_dmg, DType::Magic, self.src_r_tick, false, true, 1.0);
                if self.s.r_stacks > 0 {
                    self.s.r_next_tick = t + self.r_tick_interval;
                } else {
                    self.s.r_next_tick = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
