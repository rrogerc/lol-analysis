//! Milio. A pure ability-and-passive kit: Warm Hugs and Cozy Campfire are
//! self-cast solely to arm Fired Up!, which basic attacks consume on
//! contact for a burst plus a burn DoT, and Ultra Mega Fire Kick goes out on
//! cooldown for its own magic damage (and can consume Fired Up! itself if
//! still armed when its delayed explosion lands). Breath of Life deals no
//! damage and is never cast. Casts go one at a time: Q's pre-fire delay and
//! W's cast time both keep Milio busy exactly like a cast time.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Ultra Mega Fire Kick's explosion, delayed after the cast.
const EV_Q_EXPLOSION: u8 = 0;
/// Warm Hugs: attempting a charge cast, and its charge recharging.
const EV_E_ATTEMPT: u8 = 1;
const EV_E_RECHARGE: u8 = 2;
/// Cozy Campfire: attempting the cast, its wiki-stated second Fired Up!
/// grant 3 s later, and the zone ending (cooldown starts post-effect).
const EV_W_ATTEMPT: u8 = 3;
const EV_W_GRANT2: u8 = 4;
const EV_W_END: u8 = 5;
/// Fired Up!'s burn DoT ticks, up to two concurrent instances.
const EV_BURN_TICK_0: u8 = 6;
const EV_BURN_TICK_1: u8 = 7;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_delay: f64,
    w_cd: f64,
    w_cast_time: f64,
    w_active_duration: f64,
    w_second_offset: f64,
    e_static_cd: f64,
    e_recharge: f64,
    e_max_charges: i64,
    p_burst: f64,
    p_burn_total: f64,
    p_burn_tick_interval: f64,
    p_burn_ticks: i64,
    enchant_duration: f64,
    src_p_burst: SourceId,
    src_p_burn: SourceId,
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// When the currently-armed Fired Up! expires (now < this: armed).
    fired_up_until: f64,
    /// Q's explosion, still pending (INF: none scheduled).
    q_explosion_at: f64,
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    e_charges: i64,
    /// Next charge to finish recharging (INF: charges are full).
    e_next_charge_at: f64,
    e_static_ready: f64,
    w_ready: f64,
    /// INF once fired or when no cast is pending.
    w_grant2_at: f64,
    /// INF when the zone is not currently active.
    w_end_at: f64,
    burn_active: [bool; 2],
    burn_next_tick: [f64; 2],
    burn_ticks_left: [i64; 2],
    burn_tick_amount: [f64; 2],
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

    fn grant_fired_up(&mut self, t: f64) {
        self.s.fired_up_until = t + self.enchant_duration;
    }

    /// Consumes an armed Fired Up! (if any) for its burst and burn.
    fn maybe_consume_fired_up(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t >= self.s.fired_up_until {
            return;
        }
        self.s.fired_up_until = -INF;
        e.deal(self.p_burst, DType::Magic, self.src_p_burst, false, false, 1.0);
        let per_tick = self.p_burn_total / (self.p_burn_ticks as f64);
        let mut slot: i64 = -1;
        for i in 0..2 {
            if !self.s.burn_active[i] {
                slot = i as i64;
                break;
            }
        }
        if slot >= 0 {
            let i = slot as usize;
            self.s.burn_active[i] = true;
            self.s.burn_ticks_left[i] = self.p_burn_ticks;
            self.s.burn_tick_amount[i] = per_tick;
            self.s.burn_next_tick[i] = t + self.p_burn_tick_interval;
        } else {
            // both burn slots busy (rare overlap): deal the full remainder
            // now rather than lose it
            e.deal(self.p_burn_total, DType::Magic, self.src_p_burn, false, false, 1.0);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let e_max_charges = kit.num("gen.E.maxCharges")? as i64;
        let state = State {
            fired_up_until: -INF,
            q_explosion_at: INF,
            busy_until: 0.0,
            e_charges: e_max_charges,
            e_next_charge_at: INF,
            e_static_ready: 0.0,
            w_ready: 0.0,
            w_grant2_at: INF,
            w_end_at: INF,
            burn_active: [false, false],
            burn_next_tick: [0.0, 0.0],
            burn_ticks_left: [0, 0],
            burn_tick_amount: [0.0, 0.0],
        };
        let burn_duration = kit.num("gen.P.burnDurationS")?;
        let burn_tick_interval = kit.num("gen.P.burnTickIntervalS")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_delay: kit.num("gen.Q.castDelayS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_time: kit.num("gen.W.castTimeS")?,
            w_active_duration: kit.num("gen.W.activeDurationS")?,
            w_second_offset: kit.num("gen.W.secondGrantOffsetS")?,
            e_static_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_recharge: kit.at_rank("gen.E.rechargeS", ranks.e)?,
            e_max_charges,
            p_burst: kit.at_level("gen.P.burstAdRatioByLevel", level)? * sheet.ad,
            p_burn_total: kit.at_level("gen.P.burnBaseByLevel", level)?
                + kit.num("gen.P.burnApRatio")? * sheet.ap,
            p_burn_tick_interval: burn_tick_interval,
            p_burn_ticks: (burn_duration / burn_tick_interval) as i64,
            enchant_duration: kit.num("gen.P.enchantDurationS")?,
            src_p_burst: intern("P burst"),
            src_p_burn: intern("P burn"),
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
        shave(&mut self.s.e_static_ready, t, factor);
        if self.s.e_next_charge_at != INF {
            shave(&mut self.s.e_next_charge_at, t, factor);
        }
    }

    fn before_attack(&mut self, e: &mut Engine) {
        self.maybe_consume_fired_up(e);
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
        self.s.q_explosion_at = t + self.q_cast_delay;
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_delay);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_explosion_at != INF {
            out[n] = (self.s.q_explosion_at, Kind::Ev(EV_Q_EXPLOSION));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_charges > 0 {
                out[n] = (self.castable_at(e, self.s.e_static_ready), Kind::Ev(EV_E_ATTEMPT));
                n += 1;
            }
            if self.s.e_next_charge_at != INF {
                out[n] = (self.s.e_next_charge_at, Kind::Ev(EV_E_RECHARGE));
                n += 1;
            }
        }
        if self.ranks.w > 0 {
            if self.s.w_end_at != INF {
                if self.s.w_grant2_at != INF {
                    out[n] = (self.s.w_grant2_at, Kind::Ev(EV_W_GRANT2));
                    n += 1;
                }
                out[n] = (self.s.w_end_at, Kind::Ev(EV_W_END));
                n += 1;
            } else {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_ATTEMPT));
                n += 1;
            }
        }
        for i in 0..2 {
            if self.s.burn_active[i] {
                let kind = if i == 0 { EV_BURN_TICK_0 } else { EV_BURN_TICK_1 };
                out[n] = (self.s.burn_next_tick[i], Kind::Ev(kind));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_EXPLOSION) => {
                self.s.q_explosion_at = INF;
                e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.maybe_consume_fired_up(e);
            }
            Kind::Ev(EV_E_ATTEMPT) => {
                // Warm Hugs has no cast time: it costs nothing and locks
                // nothing out, but still waits for another cast in progress
                self.s.e_charges -= 1;
                self.s.e_static_ready = t + self.e_static_cd;
                if self.s.e_next_charge_at == INF {
                    self.s.e_next_charge_at = t + e.basic_cd(self.e_recharge);
                }
                e.prime_spellblade();
                self.grant_fired_up(t);
            }
            Kind::Ev(EV_E_RECHARGE) => {
                self.s.e_charges = imin(self.s.e_charges + 1, self.e_max_charges);
                if self.s.e_charges < self.e_max_charges {
                    self.s.e_next_charge_at = t + e.basic_cd(self.e_recharge);
                } else {
                    self.s.e_next_charge_at = INF;
                }
            }
            Kind::Ev(EV_W_ATTEMPT) => {
                e.prime_spellblade();
                self.grant_fired_up(t);
                self.s.w_grant2_at = t + self.w_second_offset;
                self.s.w_end_at = t + self.w_active_duration;
                self.busy_for(e, self.w_cast_time);
            }
            Kind::Ev(EV_W_GRANT2) => {
                self.s.w_grant2_at = INF;
                self.grant_fired_up(t);
            }
            Kind::Ev(EV_W_END) => {
                self.s.w_end_at = INF;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
            }
            Kind::Ev(EV_BURN_TICK_0) | Kind::Ev(EV_BURN_TICK_1) => {
                let i = if kind == Kind::Ev(EV_BURN_TICK_0) { 0 } else { 1 };
                e.deal(self.s.burn_tick_amount[i], DType::Magic, self.src_p_burn, false, false, 1.0);
                self.s.burn_ticks_left[i] -= 1;
                if self.s.burn_ticks_left[i] > 0 {
                    self.s.burn_next_tick[i] = t + self.p_burn_tick_interval;
                } else {
                    self.s.burn_active[i] = false;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
