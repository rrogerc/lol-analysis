//! Ezreal. Every ability of his is a projectile, and nothing happens until it
//! arrives: a cast starts a flight (the dummy's distance over the wiki's
//! missile speed) and the damage, the Rising Spell Force stack, the Essence
//! Flux detonation and Mystic Shot's cooldown refund all wait for the hit,
//! while the cooldown itself starts at the cast. Mystic Shot goes out on
//! cooldown and its hit shortens every ability's current cooldown (its own and
//! the ultimate included); Essence Flux refreshes its mark whenever off
//! cooldown, and the mark is detonated for its bonus damage by whichever of an
//! attack, Mystic Shot or Arcane Shift lands on the dummy next; Arcane Shift
//! goes out on cooldown; Trueshot Barrage opens the fight and is recast on
//! cooldown if it comes up again. Casts go one at a time: each holds one
//! shared busy_until for its cast time. Since Mystic Shot's refund can bring
//! an ability back up before its own projectile has landed, each ability
//! keeps a small board of the flights still in the air.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Mystic Shot's bolt reaches the dummy: its damage and its refund.
const EV_Q_HIT: u8 = 0;
/// Essence Flux's orb reaches the dummy and leaves its mark.
const EV_W_LAND: u8 = 1;
/// Arcane Shift's homing bolt reaches the dummy.
const EV_E_HIT: u8 = 2;
/// Trueshot Barrage's arc reaches the dummy.
const EV_R_HIT: u8 = 3;
/// Essence Flux is off cooldown and cast again.
const EV_W_CAST: u8 = 4;
/// Arcane Shift is off cooldown and cast again.
const EV_E_CAST: u8 = 5;
/// Trueshot Barrage is off cooldown and cast again (after the t = 0 opener).
const EV_R_CAST: u8 = 6;

/// How many of one ability's projectiles may be in the air at once. Any two
/// casts are at least one cast time apart (they share `busy_until`) and no
/// flight here lasts two of those, so two is the most that can ever overlap;
/// the rest is slack.
const IN_FLIGHT: usize = 4;

/// The arrival times of one ability's projectiles still in the air; a slot
/// holding INF is empty.
type Flights = [f64; IN_FLIGHT];

/// The earliest arrival on a board (INF: nothing in the air).
fn soonest(board: &Flights) -> f64 {
    let mut best = INF;
    for &x in board {
        best = pymin(best, x);
    }
    best
}

/// Puts an arrival on the board, or reports that every slot was taken — which
/// the cast times make unreachable, and which the caller answers by landing
/// the projectile at once rather than losing it.
fn launch(board: &mut Flights, at: f64) -> bool {
    for x in board.iter_mut() {
        if *x == INF {
            *x = at;
            return true;
        }
    }
    false
}

/// Takes every arrival due by `t` off the board, and says how many there were.
fn arrived(board: &mut Flights, t: f64) -> i64 {
    let mut n = 0;
    for x in board.iter_mut() {
        if *x <= t {
            *x = INF;
            n += 1;
        }
    }
    n
}

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
    /// How long each projectile takes to reach the dummy.
    q_flight: f64,
    w_dmg: f64,
    w_cd: f64,
    w_mark_dur: f64,
    w_cast_s: f64,
    w_flight: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    e_flight: f64,
    r_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    r_flight: f64,
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
    /// The projectiles of each ability still on their way.
    q_air: Flights,
    w_air: Flights,
    e_air: Flights,
    r_air: Flights,
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
    /// that triggers it came from an attack or from an ability's projectile.
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

    /// A Mystic Shot bolt connects: its damage, its stack, the mark it may
    /// detonate, and the refund that is the whole point of hitting.
    fn q_land(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.q_dmg, DType::Physical, SRC_Q, true, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        self.stack_p(t);
        self.try_detonate_w(e, true);
        self.apply_q_cdr(e, t);
    }

    /// An Essence Flux orb arrives: no damage, but the mark goes on for its
    /// duration from here.
    fn w_land(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.w_mark_active = true;
        self.s.w_mark_until = t + self.w_mark_dur;
        self.stack_p(t);
    }

    /// An Arcane Shift bolt arrives.
    fn e_land(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        self.stack_p(t);
        self.try_detonate_w(e, true);
    }

    /// A Trueshot Barrage arc arrives.
    fn r_land(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.ult_hatefog();
        self.stack_p(t);
        self.try_detonate_w(e, true);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let air: Flights = [INF; IN_FLIGHT];
        let state = State {
            busy_until: 0.0,
            p_stacks: 0,
            p_until: -1.0,
            q_air: air,
            w_air: air,
            e_air: air,
            r_air: air,
            w_ready: 0.0,
            w_mark_active: false,
            w_mark_until: -1.0,
            e_ready: 0.0,
            r_ready: 0.0,
        };
        // the engine has no positions: every projectile is flown the distance
        // it already assumes for the dummy, the champion's attack range
        let dist = sheet.base_attack_range;
        Ok(GenDriver {
            ranks,
            attack_range: dist,
            p_as_pct: kit.num("gen.P.asPerStack")? * 100.0,
            p_dur: kit.num("gen.P.stackDurationS")?,
            p_max: kit.num("gen.P.maxStacks")? as i64,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cdr: kit.num("gen.Q.cdrOnHitS")?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_flight: dist / kit.num("gen.Q.missileSpeed")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_mark_dur: kit.num("gen.W.markDurationS")?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            w_flight: dist / kit.num("gen.W.missileSpeed")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_flight: dist / kit.num("gen.E.missileSpeed")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_flight: dist / kit.num("gen.R.missileSpeed")?,
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
        // the cooldown starts here and the bolt is on its way; everything it
        // does waits for EV_Q_HIT
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
        if !launch(&mut self.s.q_air, t + self.q_flight) {
            self.q_land(e);
        }
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast (the engine has primed Spellblade): the arc leaves
        // at the start of the cast, which then keeps Ezreal busy for its cast
        // time while the arc flies
        let t = e.st.t;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.busy_for(e, self.r_cast_s);
        if !launch(&mut self.s.r_air, t + self.r_flight) {
            self.r_land(e);
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        // projectiles already on their way: not casts, so they wait for nothing
        let q = soonest(&self.s.q_air);
        if q != INF {
            out[n] = (q, Kind::Ev(EV_Q_HIT));
            n += 1;
        }
        let w = soonest(&self.s.w_air);
        if w != INF {
            out[n] = (w, Kind::Ev(EV_W_LAND));
            n += 1;
        }
        let x = soonest(&self.s.e_air);
        if x != INF {
            out[n] = (x, Kind::Ev(EV_E_HIT));
            n += 1;
        }
        let r = soonest(&self.s.r_air);
        if r != INF {
            out[n] = (r, Kind::Ev(EV_R_HIT));
            n += 1;
        }
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
            Kind::Ev(EV_Q_HIT) => {
                for _ in 0..arrived(&mut self.s.q_air, t) {
                    self.q_land(e);
                }
            }
            Kind::Ev(EV_W_LAND) => {
                for _ in 0..arrived(&mut self.s.w_air, t) {
                    self.w_land(e);
                }
            }
            Kind::Ev(EV_E_HIT) => {
                for _ in 0..arrived(&mut self.s.e_air, t) {
                    self.e_land(e);
                }
            }
            Kind::Ev(EV_R_HIT) => {
                for _ in 0..arrived(&mut self.s.r_air, t) {
                    self.r_land(e);
                }
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.prime_spellblade();
                self.busy_for(e, self.w_cast_s);
                if !launch(&mut self.s.w_air, t + self.w_flight) {
                    self.w_land(e);
                }
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
                if !launch(&mut self.s.e_air, t + self.e_flight) {
                    self.e_land(e);
                }
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.prime_spellblade();
                self.busy_for(e, self.r_cast_s);
                if !launch(&mut self.s.r_air, t + self.r_flight) {
                    self.r_land(e);
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
