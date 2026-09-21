//! Morgana. A caster whose damage comes from her abilities: Soul Shackles
//! opens the fight and lands its guaranteed delayed aftereffect 3s later
//! (the dummy can never break the tether), Tormented Shadow is recast on
//! cooldown for its on-cast hit and nine further ticks, and Dark Binding
//! goes out on cooldown. Every landed ability hit shaves 5% of Tormented
//! Shadow's total cooldown off its live timer (Soul Siphon). Casts go one
//! at a time: each of Q, W and R keeps Morgana busy for its own cast time.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Tormented Shadow is cast (on-cast hit lands immediately).
const EV_W_CAST: u8 = 0;
/// A subsequent Tormented Shadow tick.
const EV_W_TICK: u8 = 1;
/// Soul Shackles' initial hit lands (after its cast time).
const EV_R_INIT: u8 = 2;
/// Soul Shackles' guaranteed aftereffect hit, 3s after the initial hit.
const EV_R_AFTER: u8 = 3;
/// A later Soul Shackles cast (only relevant in fights longer than its cooldown).
const EV_R_RECAST: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_dmg: f64,
    w_cd: f64,
    w_interval: f64,
    w_tick_count: i64,
    w_amp_max: f64,
    w_cdr_frac: f64,
    w_cast_s: f64,
    r_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    r_chain_s: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// When Tormented Shadow may next be cast.
    w_ready: f64,
    /// Time of the next pending tick (INF: none).
    w_next_tick: f64,
    /// Ticks still owed from the current cast (including the one at w_next_tick).
    w_ticks_left: i64,
    /// Pending Soul Shackles initial-hit / aftereffect times (INF: none).
    r_init_at: f64,
    r_after_at: f64,
    /// When Soul Shackles may next be cast again (INF: none pending).
    r_ready: f64,
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

    /// Fires (or re-fires) Soul Shackles: schedules its initial hit after
    /// the cast time and keeps Morgana busy for that cast time.
    fn fire_r(&mut self, e: &mut Engine) {
        self.s.r_init_at = e.st.t + self.r_cast_s;
        e.prime_spellblade();
        self.busy_for(e, self.r_cast_s);
    }

    /// One Soul Shackles hit landing (initial or aftereffect): identical
    /// magic damage each time.
    fn deal_r(&mut self, e: &mut Engine) {
        e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.ult_hatefog();
        self.soul_siphon(e);
    }

    /// One Tormented Shadow tick, amplified by the target's live missing
    /// health (0% missing: base value, 100% missing: double).
    fn deal_w_tick(&mut self, e: &mut Engine) {
        let missing = (e.target_hp - pymax(e.st.hp, 0.0)) / e.target_hp;
        let amp = 1.0 + missing * self.w_amp_max;
        e.deal(self.w_dmg * amp, DType::Magic, SRC_W, false, true, 1.0);
        self.soul_siphon(e);
    }

    /// Soul Siphon: shave 5% of W's total (post-haste) cooldown off its
    /// currently-counting cooldown timer, never before now.
    fn soul_siphon(&mut self, e: &Engine) {
        let refund = self.w_cdr_frac * e.basic_cd(self.w_cd);
        let t = e.st.t;
        if self.s.w_ready > t {
            self.s.w_ready = pymax(t, self.s.w_ready - refund);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            w_next_tick: INF,
            w_ticks_left: 0,
            r_init_at: INF,
            r_after_at: INF,
            r_ready: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_interval: kit.num("gen.W.tickIntervalS")?,
            w_tick_count: kit.num("gen.W.tickCount")? as i64,
            w_amp_max: kit.num("gen.W.missingHealthAmpMax")?,
            w_cdr_frac: kit.num("gen.W.cdRefundFrac")?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_chain_s: kit.num("gen.R.chainDurationS")?,
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
        self.soul_siphon(e);
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.fire_r(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.s.w_next_tick < INF {
            out[n] = (self.s.w_next_tick, Kind::Ev(EV_W_TICK));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_init_at < INF {
                out[n] = (self.s.r_init_at, Kind::Ev(EV_R_INIT));
                n += 1;
            }
            if self.s.r_after_at < INF {
                out[n] = (self.s.r_after_at, Kind::Ev(EV_R_AFTER));
                n += 1;
            }
            if self.s.r_ready < INF {
                out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_RECAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.deal_w_tick(e);
                self.s.w_ticks_left = self.w_tick_count - 1;
                self.s.w_next_tick = if self.s.w_ticks_left > 0 { t + self.w_interval } else { INF };
                self.busy_for(e, self.w_cast_s);
            }
            Kind::Ev(EV_W_TICK) => {
                self.deal_w_tick(e);
                self.s.w_ticks_left -= 1;
                self.s.w_next_tick = if self.s.w_ticks_left > 0 { t + self.w_interval } else { INF };
            }
            Kind::Ev(EV_R_INIT) => {
                self.s.r_init_at = INF;
                self.deal_r(e);
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                self.s.r_after_at = t + self.r_chain_s;
            }
            Kind::Ev(EV_R_AFTER) => {
                self.s.r_after_at = INF;
                self.deal_r(e);
            }
            Kind::Ev(EV_R_RECAST) => {
                self.s.r_ready = INF;
                self.fire_r(e);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
