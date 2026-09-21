//! Ezreal. Mystic Shot goes out on cooldown, shortening every ability's
//! current cooldown (including its own and the ultimate) on hit; Essence
//! Flux refreshes its mark whenever off cooldown, and the mark is detonated
//! for its bonus damage by whichever of an attack, Mystic Shot or Arcane
//! Shift lands on the dummy next; Arcane Shift goes out on cooldown; Trueshot
//! Barrage opens the fight and is recast on cooldown if it comes up again.
//! Rising Spell Force stacks attack speed on every ability hit. Casts go one
//! at a time: each holds one shared busy_until for its cast time.

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
    q_cast_s: f64,
    w_dmg: f64,
    w_cd: f64,
    w_mark_dur: f64,
    w_cast_s: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
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
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    p_stacks: i64,
    /// When the current batch of Rising Spell Force stacks expires.
    p_until: f64,
    w_ready: f64,
    /// Whether Essence Flux's mark is currently sitting on the dummy.
    w_mark_active: bool,
    w_mark_until: f64,
    e_ready: f64,
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
        if self.ranks.r > 0 {
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
            busy_until: 0.0,
            p_stacks: 0,
            p_until: -1.0,
            w_ready: 0.0,
            w_mark_active: false,
            w_mark_until: -1.0,
            e_ready: 0.0,
            r_ready: 0.0,
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
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_mark_dur: kit.num("gen.W.markDurationS")?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
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
        self.castable_at(e, e.st.q_ready)
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
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast (the engine has primed Spellblade): the damage
        // lands with the cast, which then keeps Ezreal busy for its cast time
        let t = e.st.t;
        e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.ult_hatefog();
        self.stack_p(t);
        self.try_detonate_w(e, true);
        self.apply_q_cdr(e, t);
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
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
                e.prime_spellblade();
                self.busy_for(e, self.w_cast_s);
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.stack_p(t);
                self.try_detonate_w(e, true);
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_R_CAST) => {
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.stack_p(t);
                self.try_detonate_w(e, true);
                self.apply_q_cdr(e, t);
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                self.busy_for(e, self.r_cast_s);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
