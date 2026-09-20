//! Ezreal. Mystic Shot goes out on cooldown, shortening every ability's
//! current cooldown (including its own and the ultimate) on hit; Essence
//! Flux refreshes its mark whenever off cooldown, and the mark is detonated
//! for its bonus damage by whichever of an attack, Mystic Shot or Arcane
//! Shift lands on the dummy next; Arcane Shift goes out on cooldown; Trueshot
//! Barrage opens the fight and is recast on cooldown if it comes up again.
//! Rising Spell Force stacks attack speed on every ability hit.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Essence Flux is off cooldown and cast again.
const EV_W_CAST: u8 = 0;
/// Arcane Shift is off cooldown and cast again.
const EV_E_CAST: u8 = 1;
/// Trueshot Barrage is off cooldown and cast again (after the t = 0 opener).
const EV_R_CAST: u8 = 2;
/// Trueshot Barrage's damage lands, after its 1 s cast time.
const EV_R_HIT: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Rising Spell Force: percent bonus AS per stack, its duration, cap.
    p_as_pct: f64,
    p_dur: f64,
    p_max: i64,
    q_dmg: f64,
    q_cd: f64,
    /// Mystic Shot's current-cooldown refund on hit, applied to Q/W/E/R.
    q_cdr: f64,
    w_dmg: f64,
    w_cd: f64,
    w_mark_dur: f64,
    e_dmg: f64,
    e_cd: f64,
    r_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    src_w_deton: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    /// When the current batch of Rising Spell Force stacks expires.
    p_until: f64,
    w_ready: f64,
    /// Whether Essence Flux's mark is currently sitting on the dummy.
    w_mark_active: bool,
    w_mark_until: f64,
    e_ready: f64,
    /// Trueshot Barrage's next-cast readiness (only meaningful once it is
    /// not mid-cast, i.e. `r_hit_at == INF`).
    r_ready: f64,
    /// When a cast Trueshot Barrage's damage lands (INF: none pending).
    r_hit_at: f64,
}

impl GenDriver {
    /// Gains (or refreshes) a Rising Spell Force stack from an ability hit.
    fn stack_p(&mut self, t: f64) {
        if t > self.s.p_until {
            self.s.p_stacks = 0;
        }
        self.s.p_stacks = imin(self.s.p_stacks + 1, self.p_max);
        self.s.p_until = t + self.p_dur;
    }

    /// Mystic Shot's on-hit effect: shortens Q/W/E/R's current cooldowns.
    fn apply_q_cdr(&mut self, e: &mut Engine, t: f64) {
        e.st.q_ready = pymax(t, e.st.q_ready - self.q_cdr);
        if self.ranks.w > 0 {
            self.s.w_ready = pymax(t, self.s.w_ready - self.q_cdr);
        }
        if self.ranks.e > 0 {
            self.s.e_ready = pymax(t, self.s.e_ready - self.q_cdr);
        }
        if self.ranks.r > 0 && self.s.r_hit_at == INF {
            self.s.r_ready = pymax(t, self.s.r_ready - self.q_cdr);
        }
    }

    /// Detonates Essence Flux's mark if it is still active, whether the hit
    /// that triggers it came from an attack or from an ability cast.
    fn try_detonate_w(&mut self, e: &mut Engine, via_ability: bool) {
        if !self.s.w_mark_active {
            return;
        }
        let t = e.st.t;
        if t <= self.s.w_mark_until {
            self.s.w_mark_active = false;
            e.deal(self.w_dmg, DType::Magic, self.src_w_deton, false, true, 1.0);
            if via_ability {
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            self.stack_p(t);
        } else {
            self.s.w_mark_active = false;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            p_until: -1.0,
            w_ready: 0.0,
            w_mark_active: false,
            w_mark_until: -1.0,
            e_ready: 0.0,
            r_ready: 0.0,
            r_hit_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_as_pct: kit.num("gen.P.asPerStack")? * 100.0,
            p_dur: kit.num("gen.P.stackDurationS")?,
            p_max: kit.num("gen.P.maxStacks")? as i64,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cdr: kit.num("gen.Q.cdrOnHitS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_mark_dur: kit.num("gen.W.markDurationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_w_deton: intern("W detonation"),
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
        if t <= self.s.p_until {
            self.s.p_stacks as f64 * self.p_as_pct
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
        self.try_detonate_w(e, false);
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
        e.deal(self.q_dmg, DType::Physical, SRC_Q, true, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.stack_p(t);
        self.try_detonate_w(e, true);
        self.apply_q_cdr(e, t);
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade; hold
        // attacks for the full 1 s cast (longer than the standard lockout)
        let t = e.st.t;
        self.s.r_hit_at = t + self.r_cast_s;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        e.st.next_attack = pymax(e.st.next_attack, t + self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_hit_at != INF {
                out[n] = (self.s.r_hit_at, Kind::Ev(EV_R_HIT));
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
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_mark_active = true;
                self.s.w_mark_until = t + self.w_mark_dur;
                self.stack_p(t);
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.stack_p(t);
                self.try_detonate_w(e, true);
                e.lockout();
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_hit_at = t + self.r_cast_s;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.prime_spellblade();
                e.st.next_attack = pymax(e.st.next_attack, t + self.r_cast_s);
            }
            Kind::Ev(EV_R_HIT) => {
                self.s.r_hit_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.stack_p(t);
                self.try_detonate_w(e, true);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
