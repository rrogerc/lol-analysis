//! Mel. A ranged mage whose innate Overwhelm stacks stored magic damage on
//! the target from every attack/ability hit (bursting if it would exceed
//! their current health), while Searing Brilliance arms her next attack with
//! bonus blazing projectiles per ability cast. Q and E are cast on cooldown
//! and R is fired once, after Q and E have each landed at least once, to
//! detonate against a well-stacked target. Casts go one at a time: each of
//! Q, E and R keeps Mel busy for its own cast time before another cast or an
//! attack can start. Rebuttal (W) is never cast: see the kit's "unused" note.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Solar Snare is cast on cooldown, then its field ticks.
const EV_E_CAST: u8 = 0;
const EV_E_TICK: u8 = 1;
/// Golden Eclipse: fired once, after Q and E have each landed once.
const EV_R_CAST: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    // Passive: Overwhelm stored damage, Searing Brilliance bonus missiles.
    p_flat_dmg: f64,
    p_stack_dmg: f64,
    p3_dmg: f64,
    ow_dur: f64,
    sb_dur: f64,
    sb_per_cast: i64,
    sb_max: i64,
    // Q
    q_initial: f64,
    q_sub: f64,
    q_count: i64,
    q_cd: f64,
    q_cast_s: f64,
    // E
    e_orb: f64,
    e_tick: f64,
    e_ticks_count: i64,
    e_tick_interval: f64,
    e_cd: f64,
    e_cast_s: f64,
    // R
    r_base: f64,
    r_perstack: f64,
    r_cd: f64,
    r_cast_s: f64,
    src_q_sub: SourceId,
    src_e_tick: SourceId,
    src_p_burst: SourceId,
    src_p_bonus: SourceId,
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    // Overwhelm ledger on the target.
    ow_stacks: i64,
    ow_until: f64,
    ow_stored: f64,
    // Searing Brilliance stacks on Mel.
    sb_stacks: i64,
    sb_until: f64,
    // E: next allowed cast, and its pending field ticks.
    e_ready: f64,
    e_tick_next: f64,
    e_ticks_left: i64,
    // R: fired at most once.
    r_ready: f64,
    r_cast: bool,
    q_cast_count: i64,
    e_cast_count: i64,
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

    /// Records one instance of Mel's damage applying an Overwhelm stack on
    /// the target, storing its share of magic damage and bursting it all if
    /// the stored total now exceeds the target's current health.
    fn note_overwhelm(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t > self.s.ow_until {
            self.s.ow_stacks = 0;
            self.s.ow_stored = 0.0;
        }
        let first = self.s.ow_stacks == 0;
        self.s.ow_stacks += 1;
        self.s.ow_until = t + self.ow_dur;
        let add = if first {
            self.p_flat_dmg + self.p_stack_dmg
        } else {
            self.p_stack_dmg
        };
        self.s.ow_stored += add;
        let cur_hp = pymax(e.st.hp, 0.0);
        if self.s.ow_stored > cur_hp {
            e.deal(self.s.ow_stored, DType::Magic, self.src_p_burst, false, false, 1.0);
            self.s.ow_stored = 0.0;
        }
    }

    /// Every ability cast grants (refreshes, up to the cap) Searing
    /// Brilliance stacks on Mel.
    fn grant_sb(&mut self, t: f64) {
        if t > self.s.sb_until {
            self.s.sb_stacks = 0;
        }
        self.s.sb_stacks = imin(self.s.sb_stacks + self.sb_per_cast, self.sb_max);
        self.s.sb_until = t + self.sb_dur;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_flat_ap = kit.num("gen.P.flatApRatio")?;
        let p_stack_ap = kit.num("gen.P.stackApRatio")?;
        let p_flat_base = if ranks.r == 0 {
            kit.num("gen.P.flatDamageRank0")?
        } else {
            kit.at_rank("gen.P.flatDamage", ranks.r)?
        };
        let p_stack_base = if ranks.r == 0 {
            kit.num("gen.P.stackDamageRank0")?
        } else {
            kit.at_rank("gen.P.stackDamage", ranks.r)?
        };
        let p3_base = kit.at_level("gen.P.bonusMissileDamageByLevel", level)?;
        let p3_ap = kit.num("gen.P.bonusMissileApRatio")?;

        let e_dot_dur = kit.num("gen.E.dotDurationS")?;
        let e_tick_interval = kit.num("gen.E.tickIntervalS")?;

        let state = State {
            busy_until: 0.0,
            ow_stacks: 0,
            ow_until: 0.0,
            ow_stored: 0.0,
            sb_stacks: 0,
            sb_until: 0.0,
            e_ready: 0.0,
            e_tick_next: INF,
            e_ticks_left: 0,
            r_ready: 0.0,
            r_cast: false,
            q_cast_count: 0,
            e_cast_count: 0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("mel kit needs attack.windupFraction")?,
            p_flat_dmg: p_flat_base + p_flat_ap * sheet.ap,
            p_stack_dmg: p_stack_base + p_stack_ap * sheet.ap,
            p3_dmg: p3_base + p3_ap * sheet.ap,
            ow_dur: kit.num("gen.P.overwhelmDurationS")?,
            sb_dur: kit.num("gen.P.sbDurationS")?,
            sb_per_cast: kit.num("gen.P.sbStacksPerCast")? as i64,
            sb_max: kit.num("gen.P.sbMaxStacks")? as i64,
            q_initial: kit.hit("gen.Q.initialDamage", ranks.q, sheet)?,
            q_sub: kit.hit("gen.Q.subDamage", ranks.q, sheet)?,
            q_count: kit.at_rank("gen.Q.explosionCount", ranks.q)? as i64,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            e_orb: kit.hit("gen.E.orbDamage", ranks.e, sheet)?,
            e_tick: kit.hit("gen.E.tickDamage", ranks.e, sheet)?,
            e_ticks_count: (e_dot_dur / e_tick_interval) as i64,
            e_tick_interval,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_base: kit.hit("gen.R.ultBaseDamage", ranks.r, sheet)?,
            r_perstack: kit.hit("gen.R.ultPerStackDamage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_q_sub: intern("Q explosion"),
            src_e_tick: intern("E tick"),
            src_p_burst: intern("P burst"),
            src_p_bonus: intern("P bonus"),
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
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // the base attack itself applies an Overwhelm stack
        self.note_overwhelm(e);
        let t = e.st.t;
        if t > self.s.sb_until {
            self.s.sb_stacks = 0;
        }
        if self.s.sb_stacks > 0 {
            let n = self.s.sb_stacks;
            self.s.sb_stacks = 0;
            for _ in 0..n {
                e.deal(self.p3_dmg, DType::Magic, self.src_p_bonus, false, false, 1.0);
                self.note_overwhelm(e);
            }
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_initial, DType::Magic, SRC_Q, false, true, 1.0);
        self.note_overwhelm(e);
        for _ in 0..(self.q_count - 1) {
            e.deal(self.q_sub, DType::Magic, self.src_q_sub, false, true, 1.0);
            self.note_overwhelm(e);
        }
        self.grant_sb(e.st.t);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.q_cast_count += 1;
        self.busy_for(e, self.q_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.e_tick_next != INF {
            out[n] = (self.s.e_tick_next, Kind::Ev(EV_E_TICK));
            n += 1;
        }
        let q_gate = self.ranks.q == 0 || self.s.q_cast_count >= 1;
        let e_gate = self.ranks.e == 0 || self.s.e_cast_count >= 1;
        if self.ranks.r > 0 && !self.s.r_cast && self.s.ow_stacks > 0 && q_gate && e_gate {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_orb, DType::Magic, SRC_E, false, true, 1.0);
                self.note_overwhelm(e);
                self.grant_sb(t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.e_cast_count += 1;
                self.s.e_ticks_left = self.e_ticks_count;
                self.s.e_tick_next = t + self.e_tick_interval;
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_E_TICK) => {
                e.deal(self.e_tick, DType::Magic, self.src_e_tick, false, true, 1.0);
                self.note_overwhelm(e);
                self.s.e_ticks_left -= 1;
                if self.s.e_ticks_left > 0 {
                    self.s.e_tick_next = t + self.e_tick_interval;
                } else {
                    self.s.e_tick_next = INF;
                }
            }
            Kind::Ev(EV_R_CAST) => {
                if t > self.s.ow_until {
                    self.s.ow_stacks = 0;
                    self.s.ow_stored = 0.0;
                }
                let stacks = self.s.ow_stacks as f64;
                let dmg = self.r_base + self.r_perstack * stacks;
                e.deal(dmg, DType::Magic, SRC_R, false, true, 1.0);
                self.note_overwhelm(e);
                self.grant_sb(t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                self.s.r_cast = true;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                self.busy_for(e, self.r_cast_s);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
