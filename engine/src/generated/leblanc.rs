//! LeBlanc. A pure ability-damage caster: Sigil of Malice marks the target
//! and any later damaging ability (Q, Mimic: Sigil, Ethereal Chains'
//! initial hit or fracture, Mimic: Ethereal Chains' equivalents) consumes
//! that mark for a second instance; Distortion never interacts with marks;
//! Mimic recasts whichever of Q/W/E was most recently cast (defaulting to
//! Sigil of Malice before any basic ability is cast) with R's own numbers.
//! Casts go one after another: a single `busy_until` gates every cast
//! (Q, E and every Mimic variant except Mimic: Distortion have a 0.25s cast
//! time; Distortion and Mimic: Distortion have none but still lock out the
//! next attack for 0.25s like a dash). Basic attacks are plain (no on-hit,
//! no AS steroid, no reset) so no hook besides the ones below is overridden.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Distortion's initial dash-and-damage cast (or its Mimic equivalent).
const EV_W: u8 = 0;
/// Ethereal Chains' initial hit.
const EV_E_CAST: u8 = 1;
/// Ethereal Chains' delayed fracture, 1.5s after the initial hit.
const EV_E_FRAC: u8 = 2;
/// Mimic, recast on cooldown after the opening cast.
const EV_R: u8 = 3;
/// Mimic: Ethereal Chains' delayed fracture.
const EV_R_FRAC: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_mark_dur: f64,
    q_cast_s: f64,
    w_dmg: f64,
    w_cd: f64,
    w_cast_s: f64,
    e_init_dmg: f64,
    e_delay_dmg: f64,
    e_cd: f64,
    e_tether_dur: f64,
    e_cast_s: f64,
    r_orb_dmg: f64,
    r_mark_dmg: f64,
    r_mark_dur: f64,
    r_distort_dmg: f64,
    r_chain_init_dmg: f64,
    r_chain_frac_dmg: f64,
    r_cd: f64,
    src_q_mark: SourceId,
    src_e_frac: SourceId,
    src_r_mark: SourceId,
    src_r_frac: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// The most recently cast of Q/W/E, which Mimic will copy: 1=Q, 2=W, 3=E.
    /// Starts at 1 (Q) so an opening Mimic defaults to Mimic: Sigil of Malice.
    last_basic: i64,
    /// The single mark slot: when it exists (mark_until > now), the next
    /// damaging ability that can detonate a mark consumes it for mark_dmg.
    mark_until: f64,
    mark_dmg: f64,
    /// Which ability's value mark_dmg holds: 1=Q's, 2=R's (Mimic: Sigil).
    mark_kind: i64,
    w_ready: f64,
    e_ready: f64,
    /// Pending Ethereal Chains fracture time (INF: none pending).
    e_frac_at: f64,
    /// Mimic's own ready time (INF until the opening cast has happened).
    r_ready: f64,
    /// Pending Mimic: Ethereal Chains fracture time (INF: none pending).
    r_frac_at: f64,
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

    /// Consumes the mark on the target, if one is currently active.
    fn try_consume_mark(&mut self, e: &mut Engine, t: f64) {
        if self.s.mark_until > t {
            let src = if self.s.mark_kind == 1 { self.src_q_mark } else { self.src_r_mark };
            e.deal(self.s.mark_dmg, DType::Magic, src, false, true, 1.0);
            self.s.mark_until = -1.0;
        }
    }

    /// Casts Mimic as whichever variant matches the last basic ability cast.
    /// `prime` is false for the opening t=0 cast (the engine already primed
    /// Spellblade for it) and true for every later recast.
    fn do_r_cast(&mut self, e: &mut Engine, prime: bool) {
        let t = e.st.t;
        match self.s.last_basic {
            2 => {
                // Mimic: Distortion never interacts with marks, and has no
                // cast time (like Distortion) but locks out the next attack.
                e.deal(self.r_distort_dmg, DType::Magic, SRC_R, false, true, 1.0);
                if prime {
                    e.prime_spellblade();
                }
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.lockout();
                return;
            }
            3 => {
                // Mimic: Ethereal Chains: reuses E's 0.25s cast time.
                self.try_consume_mark(e, t);
                e.deal(self.r_chain_init_dmg, DType::Magic, SRC_R, false, true, 1.0);
                self.s.r_frac_at = t + self.e_tether_dur;
                if prime {
                    e.prime_spellblade();
                }
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                self.busy_for(e, self.e_cast_s);
            }
            _ => {
                // Mimic: Sigil of Malice (default, or last cast was Q):
                // reuses Q's 0.25s cast time.
                self.try_consume_mark(e, t);
                e.deal(self.r_orb_dmg, DType::Magic, SRC_R, false, true, 1.0);
                self.s.mark_until = t + self.r_mark_dur;
                self.s.mark_dmg = self.r_mark_dmg;
                self.s.mark_kind = 2;
                if prime {
                    e.prime_spellblade();
                }
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                self.busy_for(e, self.q_cast_s);
            }
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            last_basic: 1,
            mark_until: -1.0,
            mark_dmg: 0.0,
            mark_kind: 0,
            w_ready: 0.0,
            e_ready: 0.0,
            e_frac_at: INF,
            r_ready: INF,
            r_frac_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_mark_dur: kit.num("gen.Q.markDurationS")?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            e_init_dmg: kit.hit("gen.E.initialDamage", ranks.e, sheet)?,
            e_delay_dmg: kit.hit("gen.E.delayedDamage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_tether_dur: kit.num("gen.E.tetherDurationS")?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_orb_dmg: kit.hit("gen.R.orbDamage", ranks.r, sheet)?,
            r_mark_dmg: kit.hit("gen.R.markDamage", ranks.r, sheet)?,
            r_mark_dur: kit.num("gen.R.markDurationS")?,
            r_distort_dmg: kit.hit("gen.R.distortionDamage", ranks.r, sheet)?,
            r_chain_init_dmg: kit.hit("gen.R.chainsInitialDamage", ranks.r, sheet)?,
            r_chain_frac_dmg: kit.hit("gen.R.chainsFractureDamage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            src_q_mark: intern("Q mark"),
            src_e_frac: intern("E fracture"),
            src_r_mark: intern("R mark"),
            src_r_frac: intern("R fracture"),
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

    fn bonus_as(&self, _t: f64) -> f64 {
        0.0
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
        let t = e.st.t;
        self.try_consume_mark(e, t);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        self.s.mark_until = t + self.q_mark_dur;
        self.s.mark_dmg = self.q_dmg;
        self.s.mark_kind = 1;
        self.s.last_basic = 1;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.do_r_cast(e, false);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.e_frac_at != INF {
            out[n] = (self.s.e_frac_at, Kind::Ev(EV_E_FRAC));
            n += 1;
        }
        if self.ranks.r > 0 && self.s.r_ready != INF {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R));
            n += 1;
        }
        if self.s.r_frac_at != INF {
            out[n] = (self.s.r_frac_at, Kind::Ev(EV_R_FRAC));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                // no cast time, so no `busy_for`, but a dash still locks the
                // next attack out
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                self.s.last_basic = 2;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_CAST) => {
                self.try_consume_mark(e, t);
                e.deal(self.e_init_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.s.last_basic = 3;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_frac_at = t + self.e_tether_dur;
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_E_FRAC) => {
                // a delayed effect, not a cast: it does not gate on busy_until
                self.s.e_frac_at = INF;
                self.try_consume_mark(e, t);
                e.deal(self.e_delay_dmg, DType::Magic, self.src_e_frac, false, true, 1.0);
            }
            Kind::Ev(EV_R) => {
                self.do_r_cast(e, true);
            }
            Kind::Ev(EV_R_FRAC) => {
                self.s.r_frac_at = INF;
                self.try_consume_mark(e, t);
                e.deal(self.r_chain_frac_dmg, DType::Magic, self.src_r_frac, false, true, 1.0);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
