//! Rakan. His damage is almost entirely abilities: Gleaming Quill on
//! cooldown (a 0.25 s cast, then its cooldown starts only once the
//! projectile vanishes, assumed thrown at melee range), Grand Entrance
//! dashing in and hitting after its arrival delay, The Quickness opening the
//! fight for its one guaranteed collision hit, and Battle Dance cast on
//! cooldown purely to prime Spellblade-style effects (it deals no damage
//! itself). Casts go one at a time: only Gleaming Quill and The Quickness
//! have a cast time, and every other cast waits for one in progress.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Grand Entrance is cast, then its delayed damage lands.
const EV_W_CAST: u8 = 0;
const EV_W_HIT: u8 = 1;
/// Battle Dance is cast (no damage, just an on-cast proc).
const EV_E_CAST: u8 = 2;
/// The Quickness's damage lands after its cast time / collision.
const EV_R_HIT: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_travel_s: f64,
    q_cast_s: f64,
    w_dmg: f64,
    w_cd: f64,
    w_delay_s: f64,
    e_cd: f64,
    r_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    w_ready: f64,
    /// When Grand Entrance's delayed damage lands (INF: none pending).
    w_hit_at: f64,
    e_ready: f64,
    /// When The Quickness's collision damage lands (INF: none pending).
    r_hit_at: f64,
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
        let attack_range = sheet.base_attack_range;
        let q_speed = kit.num("gen.Q.travelSpeedUnitsPerSec")?;
        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            w_hit_at: INF,
            e_ready: 0.0,
            r_hit_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_travel_s: attack_range / q_speed,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_delay_s: kit.num("gen.W.hitDelayS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        false
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
        // Gleaming Quill: a 0.25 s cast whose damage lands as the cast
        // starts. Assumed thrown at melee range, so the projectile
        // "vanishes" (hits) almost immediately after; the post-effect
        // cooldown timer starts then, on top of the cast time itself.
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.st.q_ready = e.st.t + self.q_cast_s + self.q_travel_s + e.basic_cd(self.q_cd);
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // The opening cast: the engine has already primed Spellblade and
        // held the first attack past the flat cast lockout. The dash's
        // collision damage lands once the 0.5s cast time ends.
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_hit_at = t + self.r_cast_s;
        // Grand Entrance-independent: The Quickness's cooldown starts on cast.
        let _ = e.ult_cd(self.r_cd);
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_hit_at != INF {
                out[n] = (self.s.w_hit_at, Kind::Ev(EV_W_HIT));
            } else {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
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
            Kind::Ev(EV_W_CAST) => {
                // Grand Entrance: no cast time, dash assumed to land at
                // melee range immediately; the damage lands after the
                // wiki's stated arrival delay. Cooldown starts on cast.
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_hit_at = t + self.w_delay_s;
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_HIT) => {
                self.s.w_hit_at = INF;
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_CAST) => {
                // Battle Dance: deals no damage, cast purely for an on-cast
                // proc (e.g. Spellblade); no cast time; cooldown starts on cast.
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_HIT) => {
                self.s.r_hit_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
