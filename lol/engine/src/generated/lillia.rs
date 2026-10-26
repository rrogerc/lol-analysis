//! Lillia. Every ability applies Dream Dust (a refreshing 3s/6-tick %max-HP
//! DoT); the opener is Swirlseed to seed a Dream Dust mark, then Lilting
//! Lullaby to arm the sleep-consuming wake-up bonus, after which Blooming
//! Blows, Swirlseed and Watch Out! Eep! are all played on cooldown with
//! basic attacks filling the gaps. Casts go one at a time: a single
//! busy_until keeps every cast with a cast time (Q 0.25s, E 0.4s, W's
//! effective 0.75s strike delay, R 0.4s) from overlapping another cast or an
//! attack.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Watch Out! Eep! is cast, then its strike lands when the cast time ends.
const EV_W_CAST: u8 = 0;
const EV_W_IMPACT: u8 = 1;
/// Swirlseed is cast on cooldown (its damage lands at the end of its cast).
const EV_E_CAST: u8 = 2;
/// Lilting Lullaby is cast once a Dream Dust mark exists, then its missile
/// lands (a real delay after the cast ends) and arms the drowsy/asleep window.
const EV_R_CAST: u8 = 3;
const EV_R_MISSILE: u8 = 4;
/// Dream Dust's next damage tick.
const EV_DUST_TICK: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    p_dot_frac: f64,
    p_ticks: i64,
    p_interval: f64,
    q_dmg: f64,
    q_true_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_dmg: f64,
    w_cd: f64,
    w_cast_s: f64,
    e_dmg: f64,
    e_cd: f64,
    e_travel: f64,
    e_cast_s: f64,
    r_dmg: f64,
    r_cd: f64,
    r_travel: f64,
    r_drowsy: f64,
    r_sleep: f64,
    r_cast_s: f64,
    src_q_true: SourceId,
    src_p: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    w_ready: f64,
    w_impact_at: f64,
    e_ready: f64,
    r_ready: f64,
    r_missile_at: f64,
    r_sleep_start: f64,
    r_sleep_end: f64,
    r_wake_available: bool,
    dust_ticks_left: i64,
    dust_next_tick: f64,
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

    /// Any Lillia ability hit refreshes Dream Dust to a fresh 6-tick cycle.
    fn refresh_dust(&mut self, t: f64) {
        self.s.dust_ticks_left = self.p_ticks;
        self.s.dust_next_tick = t + self.p_interval;
    }

    /// If a sleeping target is due a qualifying hit, consume the debuff for
    /// R's bonus magic damage; that damage itself re-applies Dream Dust.
    fn check_r_wakeup(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.r_wake_available && t >= self.s.r_sleep_start && t <= self.s.r_sleep_end {
            self.s.r_wake_available = false;
            e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
            self.refresh_dust(t);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let dot_base_pct = kit.num("gen.P.dotBasePct")?;
        let dot_ap_pct_per100 = kit.num("gen.P.dotApPctPer100")?;
        let p_dot_frac = dot_base_pct / 100.0 + (dot_ap_pct_per100 / 100.0 / 100.0) * sheet.ap;
        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            w_impact_at: INF,
            e_ready: 0.0,
            r_ready: 0.0,
            r_missile_at: INF,
            r_sleep_start: 0.0,
            r_sleep_end: 0.0,
            r_wake_available: false,
            dust_ticks_left: 0,
            dust_next_tick: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("lillia kit needs attack.windupFraction")?,
            p_dot_frac,
            p_ticks: kit.num("gen.P.ticks")? as i64,
            p_interval: kit.num("gen.P.intervalS")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_true_dmg: kit.hit("gen.Q.trueDamage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_travel: kit.num("gen.E.travelS")?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.wakeDamage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_travel: kit.num("gen.R.travelS")?,
            r_drowsy: kit.num("gen.R.drowsyS")?,
            r_sleep: kit.num("gen.R.sleepS")?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_q_true: intern("Q true"),
            src_p: intern("P dot"),
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

    fn after_attack(&mut self, e: &mut Engine) {
        // a basic attack can be the qualifying hit that consumes R's sleep
        self.check_r_wakeup(e);
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
        e.deal(self.q_true_dmg, DType::True, self.src_q_true, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.refresh_dust(e.st.t);
        self.check_r_wakeup(e);
        self.busy_for(e, self.q_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_impact_at != INF {
                out[n] = (self.s.w_impact_at, Kind::Ev(EV_W_IMPACT));
            } else {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_missile_at != INF {
                out[n] = (self.s.r_missile_at, Kind::Ev(EV_R_MISSILE));
                n += 1;
            } else if self.s.dust_ticks_left > 0 {
                // only offer R once a Dream Dust mark actually exists
                out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
                n += 1;
            }
        }
        if self.s.dust_ticks_left > 0 {
            out[n] = (self.s.dust_next_tick, Kind::Ev(EV_DUST_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                // no damage on this cast: the strike lands when the
                // (effective 0.75s) cast time ends
                e.prime_spellblade();
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.busy_for(e, self.w_cast_s);
                self.s.w_impact_at = self.s.busy_until;
            }
            Kind::Ev(EV_W_IMPACT) => {
                self.s.w_impact_at = INF;
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.refresh_dust(t);
                self.check_r_wakeup(e);
            }
            Kind::Ev(EV_E_CAST) => {
                // travel is assumed negligible: the damage lands at cast
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                let impact_t = t + self.e_travel;
                e.st.next_attack = pymax(e.st.next_attack, impact_t);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.refresh_dust(impact_t);
                self.check_r_wakeup(e);
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.prime_spellblade();
                self.busy_for(e, self.r_cast_s);
                // the missile's travel is a real delay after the cast ends
                self.s.r_missile_at = self.s.busy_until + self.r_travel;
            }
            Kind::Ev(EV_R_MISSILE) => {
                self.s.r_missile_at = INF;
                let asleep_start = t + self.r_drowsy;
                self.s.r_sleep_start = asleep_start;
                self.s.r_sleep_end = asleep_start + self.r_sleep;
                self.s.r_wake_available = true;
            }
            Kind::Ev(EV_DUST_TICK) => {
                self.s.dust_ticks_left -= 1;
                let amt = self.p_dot_frac * e.target_hp / (self.p_ticks as f64);
                e.deal(amt, DType::Magic, self.src_p, false, true, 1.0);
                if self.s.dust_ticks_left > 0 {
                    self.s.dust_next_tick = t + self.p_interval;
                } else {
                    self.s.dust_next_tick = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
