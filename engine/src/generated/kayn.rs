//! Kayn (Shadow Assassin form). Reaping Slash (dash+swing) and Blade's Reach
//! (instant in this form) are cast on cooldown; once the dummy has been
//! damaged once (the passive mark, treated as permanent thereafter) Umbral
//! Trespass is cast on cooldown too: its opening cast is a no-op, and the
//! vanish/dash/attach/channel/recast timeline is modeled as our own events,
//! landing its damage and resetting Reaping Slash's cooldown on emerge. The
//! Shadow Assassin Bonus applies its magic damage amp to ability damage for
//! the fight's first 3 seconds.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Blade's Reach on cooldown; Umbral Trespass's vanish (starts the channel
/// timeline); Umbral Trespass's recast, which deals the real damage.
const EV_W_CAST: u8 = 0;
const EV_R_START: u8 = 1;
const EV_R_DAMAGE: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    r_dmg: f64,
    r_cd: f64,
    /// Shadow Assassin Bonus: percent of an ability hit dealt again as
    /// bonus magic damage, while the window (set at fight start) is open.
    p_pct: f64,
    /// Umbral Trespass's timeline: dash-in, attach delay, minimum channel
    /// before a recast is legal, and the recast's own delay before damage.
    r_dash_in: f64,
    r_attach_delay: f64,
    r_min_channel: f64,
    r_recast_delay: f64,
    src_p: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    r_ready: f64,
    /// Time Umbral Trespass's current channel, if any, keeps Kayn busy
    /// (also blocks Q and W while in the future).
    r_busy_until: f64,
    r_channeling: bool,
    r_damage_at: f64,
    /// Whether the dummy has ever been marked (damaged); treated as
    /// permanent for the rest of a continuous fight.
    marked: bool,
    /// The Shadow Assassin Bonus window; fixed since combat starts at t=0
    /// and never lapses for 8s against a dummy that is always fought.
    p_window_until: f64,
}

impl GenDriver {
    fn p_bonus_maybe(&mut self, e: &mut Engine, amount: f64) {
        if e.st.t < self.s.p_window_until {
            let bonus = amount * self.p_pct / 100.0;
            e.deal(bonus, DType::Magic, self.src_p, false, false, 1.0);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_duration = kit.num("gen.P.durationS")?;
        let state = State {
            w_ready: 0.0,
            r_ready: 0.0,
            r_busy_until: 0.0,
            r_channeling: false,
            r_damage_at: INF,
            marked: false,
            p_window_until: p_duration,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            p_pct: kit.at_level("gen.P.percentByLevel", level)?,
            r_dash_in: kit.num("gen.R.dashInS")?,
            r_attach_delay: kit.num("gen.R.attachDelayS")?,
            r_min_channel: kit.num("gen.R.minChannelS")?,
            r_recast_delay: kit.num("gen.R.recastDelayS")?,
            src_p: intern("P bonus"),
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
        let ready = pymax(e.st.q_ready, e.st.t);
        pymax(ready, self.s.r_busy_until)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        // the dash: first physical damage instance
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        self.p_bonus_maybe(e, self.q_dmg);
        // the swing: second, identical physical damage instance
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        self.p_bonus_maybe(e, self.q_dmg);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.marked = true;
        e.lockout(); // the swing's 0.25s post-cast lockout
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let t = e.st.t;
        let mut n = 0;
        if self.ranks.w > 0 {
            let ready = pymax(pymax(self.s.w_ready, t), self.s.r_busy_until);
            out[n] = (ready, Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_channeling {
                out[n] = (self.s.r_damage_at, Kind::Ev(EV_R_DAMAGE));
                n += 1;
            } else if self.s.marked {
                out[n] = (pymax(self.s.r_ready, t), Kind::Ev(EV_R_START));
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
                e.deal(self.w_dmg, DType::Physical, SRC_W, false, true, 1.0);
                self.p_bonus_maybe(e, self.w_dmg);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.marked = true;
                // Shadow Assassin removes the cast time: no lockout
            }
            Kind::Ev(EV_R_START) => {
                // the vanish and dash-in; deals no damage itself
                self.s.r_channeling = true;
                let land = t + self.r_dash_in + self.r_attach_delay
                    + self.r_min_channel + self.r_recast_delay;
                self.s.r_damage_at = land;
                self.s.r_busy_until = land;
                e.st.next_attack = pymax(e.st.next_attack, land);
                e.prime_spellblade();
                e.ability_cast_proc();
            }
            Kind::Ev(EV_R_DAMAGE) => {
                self.s.r_channeling = false;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                self.p_bonus_maybe(e, self.r_dmg);
                e.eclipse_hit();
                e.ult_hatefog();
                // Shadow Assassin Bonus: emerging resets Reaping Slash
                e.st.q_ready = t;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
