//! Swain. A caster who still attacks between casts. Demonic Ascension is
//! cast once at t=0 and treated as permanent for the fight (draining the
//! dummy, a champion, keeps its Demonic Energy net positive), ticking drain
//! damage every 0.5s and letting Demonflare be manually recast on cooldown.
//! Death's Hand, Vision of Empire and Nevermove are each cast on cooldown,
//! with Vision of Empire and Nevermove's damage landing on a delay.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Vision of Empire: the cast, then its delayed explosion.
const EV_W_CAST: u8 = 0;
const EV_W_EXPLODE: u8 = 1;
/// Nevermove: the cast, then its delayed detonation.
const EV_E_CAST: u8 = 2;
const EV_E_HIT: u8 = 3;
/// Demonic Ascension's drain tick, and a manual Demonflare cast.
const EV_R_TICK: u8 = 4;
const EV_DEMONFLARE: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    w_explode_delay: f64,
    e_dmg: f64,
    e_cd: f64,
    e_travel_delay: f64,
    r_cast_s: f64,
    r_tick_dmg: f64,
    r_tick_interval: f64,
    r_demonflare_dmg: f64,
    r_demonflare_delay: f64,
    r_demonflare_cd: f64,
    src_r_demonflare: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    w_explode_at: f64,
    e_ready: f64,
    e_hit_at: f64,
    r_tick_at: f64,
    demonflare_ready: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            w_ready: 0.0,
            w_explode_at: INF,
            e_ready: 0.0,
            e_hit_at: INF,
            r_tick_at: INF,
            demonflare_ready: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_explode_delay: kit.num("gen.W.explodeDelayS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_travel_delay: kit.num("gen.E.travelDelayS")?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_tick_dmg: kit.hit("gen.R.tickDamage", ranks.r, sheet)?,
            r_tick_interval: kit.num("gen.R.tickIntervalS")?,
            r_demonflare_dmg: kit.hit("gen.R.demonflareDamage", ranks.r, sheet)?,
            r_demonflare_delay: kit.num("gen.R.demonflareCastDelayS")?,
            r_demonflare_cd: kit.num("gen.R.demonflareCooldownS")?,
            src_r_demonflare: intern("R Demonflare"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        true
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
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the stance is treated as permanent for the fight
        let t = e.st.t;
        e.lockout();
        e.prime_spellblade();
        self.s.r_tick_at = t + self.r_cast_s + self.r_tick_interval;
        self.s.demonflare_ready = t + self.r_cast_s + self.r_demonflare_delay;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_explode_at != INF {
                out[n] = (self.s.w_explode_at, Kind::Ev(EV_W_EXPLODE));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_hit_at != INF {
                out[n] = (self.s.e_hit_at, Kind::Ev(EV_E_HIT));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_tick_at != INF {
                out[n] = (self.s.r_tick_at, Kind::Ev(EV_R_TICK));
                n += 1;
            }
            if self.s.demonflare_ready != INF {
                out[n] = (pymax(self.s.demonflare_ready, e.st.t), Kind::Ev(EV_DEMONFLARE));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                // Vision of Empire's cooldown starts on cast; the explosion
                // is a separately timed damage instance
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_explode_at = t + self.w_explode_delay;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_W_EXPLODE) => {
                self.s.w_explode_at = INF;
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_CAST) => {
                // Nevermove's cooldown starts on cast, per the wiki
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_hit_at = t + self.e_travel_delay;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_HIT) => {
                self.s.e_hit_at = INF;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_R_TICK) => {
                self.s.r_tick_at = t + self.r_tick_interval;
                e.deal(self.r_tick_dmg, DType::Magic, SRC_R, false, true, 1.0);
            }
            Kind::Ev(EV_DEMONFLARE) => {
                self.s.demonflare_ready = t + self.r_demonflare_cd;
                e.deal(self.r_demonflare_dmg, DType::Magic, self.src_r_demonflare, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
