//! Amumu. Despair toggles on at t=0 and ticks every 0.5s for the fight;
//! Curse of the Sad Mummy opens the fight; Bandage Toss goes out on cooldown
//! and recharges through a 2-charge system; Tantrum is cast on cooldown.
//! Cursed Touch adds 10% bonus true damage to Amumu's own magic hits against
//! an already-marked target, then (re)marks the target for 3s. Casts go one
//! at a time: Q, E and R each have a 0.25s cast time and a single
//! `busy_until` in the state keeps them, and the attacks between them, from
//! overlapping.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Despair ticks; Tantrum is cast on cooldown; Curse of the Sad Mummy lands
/// at the end of its cast time.
const EV_W_TICK: u8 = 0;
const EV_E_CAST: u8 = 1;
const EV_R_SWING: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    curse_dur: f64,
    p_amp: f64,
    q_dmg: f64,
    q_max_charges: i64,
    q_start_charges: i64,
    q_recharge_s: f64,
    q_static_cd: f64,
    q_cast_s: f64,
    w_tick_base: f64,
    w_hp_frac: f64,
    w_interval: f64,
    w_offset: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    r_dmg: f64,
    r_cast_s: f64,
    src_p: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// The time until which the target is marked with Curse.
    cursed_until: f64,
    w_tick_next: f64,
    e_ready: f64,
    /// When Curse of the Sad Mummy's damage lands (INF once resolved).
    r_swing_at: f64,
    q_charges: i64,
    /// When the next Bandage Toss charge finishes recharging (INF if none pending).
    q_regen_at: f64,
    q_static_ready: f64,
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

    fn q_avail(&self, t: f64) -> i64 {
        let mut c = self.s.q_charges;
        if self.s.q_regen_at <= t {
            c = imin(c + 1, self.q_max_charges);
        }
        c
    }

    /// Deals a magic ability hit, amplifying it with Cursed Touch's bonus
    /// true damage if the target was already marked, then (re)applies Curse.
    fn cursed_hit(&mut self, e: &mut Engine, dmg: f64, src: SourceId) {
        let t = e.st.t;
        let already_cursed = t < self.s.cursed_until;
        e.deal(dmg, DType::Magic, src, false, true, 1.0);
        if already_cursed {
            e.deal(dmg * self.p_amp, DType::True, self.src_p, false, false, 1.0);
        }
        self.s.cursed_until = t + self.curse_dur;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let q_start = kit.num("gen.Q.startCharges")? as i64;
        let state = State {
            busy_until: 0.0,
            cursed_until: -INF,
            w_tick_next: kit.num("gen.W.tickOffsetS")?,
            e_ready: 0.0,
            r_swing_at: INF,
            q_charges: q_start,
            q_regen_at: INF,
            q_static_ready: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("amumu kit needs attack.windupFraction")?,
            curse_dur: kit.num("gen.P.curseDurationS")?,
            p_amp: kit.num("gen.P.damageAmpFrac")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_max_charges: kit.num("gen.Q.maxCharges")? as i64,
            q_start_charges: q_start,
            q_recharge_s: kit.at_rank("gen.Q.rechargeS", ranks.q)?,
            q_static_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_tick_base: kit.at_rank("gen.W.tickBase", ranks.w)?,
            w_hp_frac: kit.at_rank("gen.W.tickHpFracByRank", ranks.w)?
                + kit.num("gen.W.hpFracPerApOver100")? * (sheet.ap / 100.0),
            w_interval: kit.num("gen.W.tickIntervalS")?,
            w_offset: kit.num("gen.W.tickOffsetS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_p: intern("P"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
        self.s.q_charges = self.q_start_charges;
        self.s.w_tick_next = self.w_offset;
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

    fn shave_cooldowns(&mut self, _st: &mut St, t: f64, factor: f64) {
        shave(&mut self.s.q_static_ready, t, factor);
        if self.s.q_regen_at != INF {
            shave(&mut self.s.q_regen_at, t, factor);
        }
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // Cursed Touch: a basic attack has no magic damage in this kit, so
        // it only (re)marks the target.
        self.s.cursed_until = e.st.t + self.curse_dur;
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        let t = e.st.t;
        let ready = if self.q_avail(t) > 0 { self.s.q_static_ready } else { self.s.q_regen_at };
        self.castable_at(e, ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.q_regen_at <= t {
            self.s.q_charges = imin(self.s.q_charges + 1, self.q_max_charges);
            self.s.q_regen_at = INF;
        }
        self.s.q_charges -= 1;
        if self.s.q_regen_at == INF {
            self.s.q_regen_at = t + self.q_recharge_s;
        }
        self.s.q_static_ready = t + self.q_static_cd;

        self.cursed_hit(e, self.q_dmg, SRC_Q);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the opening cast (the engine has primed Spellblade): its cast time
        // keeps Amumu busy, and the damage lands when it ends
        self.s.r_swing_at = e.st.t + self.r_cast_s;
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            // a tick, not a cast: it reports its own time and never waits
            out[n] = (self.s.w_tick_next, Kind::Ev(EV_W_TICK));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.r_swing_at != INF {
            // the delayed damage from an already-started cast: its own time
            out[n] = (self.s.r_swing_at, Kind::Ev(EV_R_SWING));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_TICK) => {
                self.s.w_tick_next = t + self.w_interval;
                let dmg = self.w_tick_base + self.w_hp_frac * e.target_hp;
                self.cursed_hit(e, dmg, SRC_W);
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.cursed_hit(e, self.e_dmg, SRC_E);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_R_SWING) => {
                self.s.r_swing_at = INF;
                self.cursed_hit(e, self.r_dmg, SRC_R);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
