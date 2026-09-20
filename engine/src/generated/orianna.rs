//! Orianna. An auto-attacker whose passive rides her attacks: Clockwork
//! Winding stacks (0/1/2) increase a bonus magic on-hit. Command: Attack
//! is wired as the engine's Q; Command: Dissonance, Command: Protect and
//! Command: Shockwave go out on cooldown as timed events, Shockwave also
//! opening the fight through the engine's t = 0 ult cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Command: Dissonance and Command: Protect, cast on cooldown.
const EV_W: u8 = 0;
const EV_E: u8 = 1;
/// Command: Shockwave: the cast (after which its damage is scheduled), and
/// the swing landing at the end of its 0.5 s cast time.
const EV_R_CAST: u8 = 2;
const EV_R_SWING: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Clockwork Winding: flat + AP bonus on-hit damage, and its stacking.
    p_onhit_flat: f64,
    p_stack_mult: f64,
    p_stack_cap: i64,
    p_stack_dur: f64,
    q_dmg_full: f64,
    q_dmg_reduced: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    r_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    src_p: SourceId,
    src_q_reduced: SourceId,
    src_w: SourceId,
    src_e: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    p_stack_until: f64,
    /// Stacks active for the attack currently landing (set in
    /// `before_attack`, read in `attack_riders`).
    p_active_stacks: i64,
    w_ready: f64,
    e_ready: f64,
    /// Command: Shockwave's next cast time, and when a pending cast's
    /// damage lands (INF: none pending).
    r_ready: f64,
    r_swing_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            p_stack_until: -1.0,
            p_active_stacks: 0,
            w_ready: 0.0,
            e_ready: 0.0,
            r_ready: 0.0,
            r_swing_at: INF,
        };
        let q_dmg_full = kit.hit("gen.Q.damage", ranks.q, sheet)?;
        let q_reduced_frac = kit.num("gen.Q.reducedPct")? / 100.0;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("orianna kit needs attack.windupFraction")?,
            p_onhit_flat: kit.at_level("gen.P.baseByLevel", level)? + kit.num("gen.P.apRatio")? * sheet.ap,
            p_stack_mult: kit.num("gen.P.stackMult")?,
            p_stack_cap: kit.num("gen.P.stackCap")? as i64,
            p_stack_dur: kit.num("gen.P.stackDurationS")?,
            q_dmg_full,
            q_dmg_reduced: q_dmg_full * q_reduced_frac,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_p: intern("P"),
            src_q_reduced: intern("Q reduced"),
            src_w: intern("W"),
            src_e: intern("E"),
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
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t > self.s.p_stack_until {
            self.s.p_stacks = 0;
        }
        // this attack's on-hit is scaled by the stacks earned by earlier
        // attacks; it then adds/refreshes its own stack for the next one
        self.s.p_active_stacks = self.s.p_stacks;
        self.s.p_stacks = imin(self.s.p_stacks + 1, self.p_stack_cap);
        self.s.p_stack_until = t + self.p_stack_dur;
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        let mult = 1.0 + self.p_stack_mult * (self.s.p_active_stacks as f64);
        e.deal(self.p_onhit_flat * mult, DType::Magic, self.src_p, false, false, 1.0);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        // the Ball hits the dummy once passing through, once more (reduced)
        // arriving at the same spot
        e.deal(self.q_dmg_full, DType::Magic, SRC_Q, false, true, 1.0);
        e.deal(self.q_dmg_reduced, DType::Magic, self.src_q_reduced, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the swing lands when the 0.5 s cast time ends
        let t = e.st.t;
        self.s.r_swing_at = t + self.r_cast_s;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        e.lockout();
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_swing_at != INF {
                out[n] = (self.s.r_swing_at, Kind::Ev(EV_R_SWING));
            } else {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, self.src_w, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, self.src_e, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_swing_at = t + self.r_cast_s;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.lockout();
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_SWING) => {
                self.s.r_swing_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
