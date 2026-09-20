//! Karma. A pure caster: Mantra (R) is a free, no-cast-time buff that arms
//! the next Q for extra impact + delayed field damage (both attributed to R,
//! since Mantra itself is what deals that bonus damage), Gathering Fire (P)
//! shortens Mantra's current cooldown by 4s per damage instance landed, and
//! Q / W are recast on cooldown while E (no damage) is never cast.

use crate::fight::{Driver, Engine, Events, Kind};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Mantra comes off cooldown and is recast immediately.
const EV_R_CAST: u8 = 0;
/// Soulflare's field detonation, 1.5s after an empowered Q's impact.
const EV_Q_FIELD: u8 = 1;
/// Focused Resolve is cast when ready.
const EV_W_CAST: u8 = 2;
/// Focused Resolve's guaranteed aftereffect, 2s after the cast.
const EV_W_AFTER: u8 = 3;
/// An unspent Mantra charge decays if never spent on a Q.
const EV_MANTRA_DECAY: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,

    q_dmg: f64,
    q_cd: f64,
    r_q_impact: f64,
    r_q_field: f64,
    field_delay_s: f64,

    w_dmg: f64,
    w_cd: f64,
    tether_s: f64,

    r_cd: f64,
    window_s: f64,

    cdr_per_hit: f64,

    src_r_q_impact: SourceId,
    src_r_q_field: SourceId,
    src_w_after: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    r_ready: f64,
    mantra_charged: bool,
    mantra_decay_at: f64,
    q_field_at: f64,
    w_ready: f64,
    w_after_at: f64,
}

impl GenDriver {
    /// Gathering Fire: reduces Mantra's CURRENT cooldown by 4s for a single
    /// champion-hit damage instance.
    fn reduce_mantra_cd(&mut self, e: &mut Engine) {
        if self.ranks.r > 0 {
            self.s.r_ready = pymax(self.s.r_ready - self.cdr_per_hit, e.st.t);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            r_ready: INF,
            mantra_charged: false,
            mantra_decay_at: INF,
            q_field_at: INF,
            w_ready: 0.0,
            w_after_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,

            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            r_q_impact: kit.hit("gen.R.qBonusImpact", ranks.r, sheet)?,
            r_q_field: kit.hit("gen.R.qBonusField", ranks.r, sheet)?,
            field_delay_s: kit.num("gen.Q.fieldDelayS")?,

            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            tether_s: kit.num("gen.W.tetherS")?,

            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            window_s: kit.num("gen.R.windowS")?,

            cdr_per_hit: kit.num("gen.P.cdrPerHitS")?,

            src_r_q_impact: intern("R empowered Q impact"),
            src_r_q_field: intern("R empowered Q field"),
            src_w_after: intern("W aftereffect"),

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

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);

        // base Inner Flame impact: one champion-hit instance
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        self.reduce_mantra_cd(e);

        if self.s.mantra_charged {
            // Soulflare: bonus impact now (Mantra's damage), field
            // detonation later; both attributed to R since Mantra is what
            // supplies this extra effect
            e.deal(self.r_q_impact, DType::Magic, self.src_r_q_impact, false, true, 1.0);
            self.s.q_field_at = t + self.field_delay_s;
            self.s.mantra_charged = false;
            self.s.mantra_decay_at = INF;
        }

        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: no cost, no cast time, cooldown starts now
        let t = e.st.t;
        self.s.mantra_charged = true;
        self.s.mantra_decay_at = t + self.window_s;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.r > 0 {
            out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        if self.s.q_field_at != INF {
            out[n] = (self.s.q_field_at, Kind::Ev(EV_Q_FIELD));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_after_at != INF {
                out[n] = (self.s.w_after_at, Kind::Ev(EV_W_AFTER));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.s.mantra_decay_at != INF {
            out[n] = (self.s.mantra_decay_at, Kind::Ev(EV_MANTRA_DECAY));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_CAST) => {
                self.s.mantra_charged = true;
                self.s.mantra_decay_at = t + self.window_s;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.prime_spellblade();
            }
            Kind::Ev(EV_Q_FIELD) => {
                self.s.q_field_at = INF;
                e.deal(self.r_q_field, DType::Magic, self.src_r_q_field, false, true, 1.0);
                self.reduce_mantra_cd(e);
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                self.reduce_mantra_cd(e);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
                self.s.w_after_at = t + self.tether_s;
            }
            Kind::Ev(EV_W_AFTER) => {
                self.s.w_after_at = INF;
                e.deal(self.w_dmg, DType::Magic, self.src_w_after, false, true, 1.0);
                self.reduce_mantra_cd(e);
            }
            Kind::Ev(EV_MANTRA_DECAY) => {
                self.s.mantra_charged = false;
                self.s.mantra_decay_at = INF;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
