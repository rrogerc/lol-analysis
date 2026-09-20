//! Draven. An auto-attacker whose Spinning Axe (Q) arms up to two future
//! basic attacks with bonus physical damage, Blood Rush (W) is a flat-cooldown
//! attack-speed buff, Stand Aside (E) is a cast-time nuke, and Whirling Death
//! (R) opens the fight with an outbound pass and a recast return pass.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Blood Rush is cast (its buff has no separate landing event).
const EV_W_CAST: u8 = 0;
/// Stand Aside is cast; then its damage lands at the cast time's end.
const EV_E_CAST: u8 = 1;
const EV_E_SWING: u8 = 2;
/// Whirling Death's outbound pass lands; then its recast return pass.
const EV_R_SWING: u8 = 3;
const EV_R_RECAST: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_cd: f64,
    q_dmg: f64,
    q_max_charges: i64,
    w_cd: f64,
    w_as_pct: f64,
    w_dur: f64,
    e_cd: f64,
    e_dmg: f64,
    e_cast_s: f64,
    r_dmg: f64,
    r_cast_s: f64,
    r_recast_delay_s: f64,
    src_q: SourceId,
    src_e: SourceId,
    src_r: SourceId,
    src_r2: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Held Spinning Axe charges, each empowering a future attack.
    q_charges: i64,
    w_ready: f64,
    /// While `t < w_buff_until`, Blood Rush's attack-speed bonus applies.
    w_buff_until: f64,
    e_ready: f64,
    /// When Stand Aside's damage lands (INF: none pending).
    e_swing_at: f64,
    /// Whirling Death's outbound landing and its recast trigger (INF: none pending).
    r_swing_at: f64,
    r_recast_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            q_charges: 0,
            w_ready: 0.0,
            w_buff_until: -1.0,
            e_ready: 0.0,
            e_swing_at: INF,
            r_swing_at: INF,
            r_recast_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_max_charges: kit.num("gen.Q.maxCharges")? as i64,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_as_pct: kit.at_rank("gen.W.asPctByRank", ranks.w)?,
            w_dur: kit.num("gen.W.durationS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_recast_delay_s: kit.num("gen.R.recastMinDelayS")?,
            src_q: intern("Q empowered"),
            src_e: intern("E"),
            src_r: intern("R"),
            src_r2: intern("R recast"),
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
        if t < self.s.w_buff_until {
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

    fn after_attack(&mut self, e: &mut Engine) {
        // Spinning Axe: a held charge empowers this attack with a single
        // instance of bonus physical basic damage.
        if self.s.q_charges > 0 {
            self.s.q_charges -= 1;
            e.deal(self.q_dmg, DType::Physical, self.src_q, true, true, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // No cast time: recasting just refreshes the buff and adds a charge
        // if one is not already held (capped at two).
        self.s.q_charges = imin(self.s.q_charges + 1, self.q_max_charges);
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.ability_cast_proc();
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // The opening cast: the engine has primed Spellblade and held the
        // first attack past its own 0.25s lockout; the outbound pass lands
        // when the 0.5s cast time ends, and the return pass may be forced
        // at the earliest at 1s after this cast.
        let t = e.st.t;
        self.s.r_swing_at = t + self.r_cast_s;
        self.s.r_recast_at = t + self.r_recast_delay_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let t = e.st.t;
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_swing_at != INF {
                out[n] = (self.s.e_swing_at, Kind::Ev(EV_E_SWING));
            } else {
                out[n] = (pymax(self.s.e_ready, t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.s.r_swing_at != INF {
            out[n] = (self.s.r_swing_at, Kind::Ev(EV_R_SWING));
            n += 1;
        }
        if self.s.r_recast_at != INF {
            out[n] = (self.s.r_recast_at, Kind::Ev(EV_R_RECAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_buff_until = t + self.w_dur;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.ability_cast_proc();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_swing_at = t + self.e_cast_s;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.ability_cast_proc();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_SWING) => {
                self.s.e_swing_at = INF;
                e.deal(self.e_dmg, DType::Physical, self.src_e, false, true, 1.0);
                e.eclipse_hit();
            }
            Kind::Ev(EV_R_SWING) => {
                self.s.r_swing_at = INF;
                e.deal(self.r_dmg, DType::Physical, self.src_r, false, true, 1.0);
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_R_RECAST) => {
                self.s.r_recast_at = INF;
                e.ability_cast_proc();
                e.prime_spellblade();
                e.deal(self.r_dmg, DType::Physical, self.src_r2, false, true, 1.0);
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
