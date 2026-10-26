//! Nautilus. Staggering Blow (P) rides basic attacks for bonus physical
//! damage once every 6s per target; Dredge Line (Q) is a plain on-cast
//! magic hit; Titan's Wrath (W) raises a shield that empowers attacks with
//! Pain of Wrath (half damage now, half 1.25s later, only on the first
//! attack per shield window) and resets the attack timer for every attack
//! landing while the shield holds; Riptide (E) fires three timed waves,
//! the last two reduced; Depth Charge (R) opens the fight with one
//! underway eruption followed by its bigger final eruption. Casts go one
//! at a time via a single busy_until: R's 0.46s cast, Q's 0.25s cast and
//! E's 0.25s cast each hold off any other cast or attack; W has no cast
//! time but still cannot start inside another cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Titan's Wrath is cast as soon as it is off cooldown.
const EV_W_CAST: u8 = 0;
/// Riptide is cast as soon as it is off cooldown, then its two later waves.
const EV_E_CAST: u8 = 1;
const EV_E_WAVE2: u8 = 2;
const EV_E_WAVE3: u8 = 3;
/// Pain of Wrath's delayed second half.
const EV_POW_TICK: u8 = 4;
/// Depth Charge's underway eruption, then its final eruption.
const EV_R_UNDERWAY: u8 = 5;
const EV_R_FINAL: u8 = 6;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    p_dmg: f64,
    p_per_target_cd: f64,

    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,

    w_cd: f64,
    w_duration: f64,
    w_dot_dmg: f64,
    w_dot_delay: f64,
    w_dot_split: f64,

    e_cd: f64,
    e_wave_dmg: f64,
    e_reduction: f64,
    e_wave2_offset: f64,
    e_wave3_offset: f64,
    e_cast_s: f64,

    r_secondary_dmg: f64,
    r_primary_dmg: f64,
    r_cast_s: f64,
    r_interval_s: f64,
    r_reach_s: f64,

    src_p: SourceId,
    src_e_wave: SourceId,
    src_r_underway: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    p_last_proc: f64,
    w_ready: f64,
    w_shield_until: f64,
    pow_lock_until: f64,
    pow_tick_at: f64,
    pow_tick_amt: f64,
    e_ready: f64,
    e_wave2_at: f64,
    e_wave3_at: f64,
    r_underway_at: f64,
    r_final_at: f64,
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
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            p_last_proc: -INF,
            w_ready: 0.0,
            w_shield_until: -INF,
            pow_lock_until: -INF,
            pow_tick_at: INF,
            pow_tick_amt: 0.0,
            e_ready: 0.0,
            e_wave2_at: INF,
            e_wave3_at: INF,
            r_underway_at: INF,
            r_final_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("nautilus kit needs attack.windupFraction")?,

            p_dmg: kit.at_level("gen.P.bonusDamageByLevel", level)?,
            p_per_target_cd: kit.num("gen.P.perTargetCdS")?,

            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,

            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_duration: kit.num("gen.W.durationS")?,
            w_dot_dmg: kit.hit("gen.W.dot", ranks.w, sheet)?,
            w_dot_delay: kit.num("gen.W.dotDelayS")?,
            w_dot_split: kit.num("gen.W.dotSplitFraction")?,

            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_wave_dmg: kit.hit("gen.E.wave1", ranks.e, sheet)?,
            e_reduction: kit.num("gen.E.reductionRatio")?,
            e_wave2_offset: kit.num("gen.E.wave2OffsetS")?,
            e_wave3_offset: kit.num("gen.E.wave3OffsetS")?,
            e_cast_s: kit.num("gen.E.castTimeS")?,

            r_secondary_dmg: kit.hit("gen.R.secondary", ranks.r, sheet)?,
            r_primary_dmg: kit.hit("gen.R.primary", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_interval_s: kit.num("gen.R.eruptionIntervalS")?,
            r_reach_s: kit.num("gen.R.reachTimeS")?,

            src_p: intern("P"),
            src_e_wave: intern("E wave"),
            src_r_underway: intern("R underway"),

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

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;

        // Staggering Blow: bonus physical damage, gated to once per 6s on
        // this target.
        if t >= self.s.p_last_proc + self.p_per_target_cd {
            self.s.p_last_proc = t;
            e.deal(self.p_dmg, DType::Physical, self.src_p, false, false, 1.0);
        }

        // Titan's Wrath: while the shield holds, every attack applies Pain
        // of Wrath (only the first per window deals damage; later ones are
        // non-damaging stacks) and the attack timer is reset in
        // schedule_attack.
        if self.ranks.w > 0 && t < self.s.w_shield_until && t >= self.s.pow_lock_until {
            self.s.pow_lock_until = t + self.w_dot_delay;
            let half = self.w_dot_dmg * self.w_dot_split;
            e.deal(half, DType::Magic, SRC_W, false, true, 1.0);
            self.s.pow_tick_amt = half;
            self.s.pow_tick_at = t + self.w_dot_delay;
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.ranks.w > 0 && t < self.s.w_shield_until {
            // Titan's Wrath resets Nautilus' basic attack timer.
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
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
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_underway_at = t + self.r_cast_s + self.r_interval_s;
        self.s.r_final_at = t + self.r_cast_s + self.r_reach_s;
        e.prime_spellblade();
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
        if self.s.e_wave2_at != INF {
            out[n] = (self.s.e_wave2_at, Kind::Ev(EV_E_WAVE2));
            n += 1;
        }
        if self.s.e_wave3_at != INF {
            out[n] = (self.s.e_wave3_at, Kind::Ev(EV_E_WAVE3));
            n += 1;
        }
        if self.s.pow_tick_at != INF {
            out[n] = (self.s.pow_tick_at, Kind::Ev(EV_POW_TICK));
            n += 1;
        }
        if self.s.r_underway_at != INF {
            out[n] = (self.s.r_underway_at, Kind::Ev(EV_R_UNDERWAY));
            n += 1;
        }
        if self.s.r_final_at != INF {
            out[n] = (self.s.r_final_at, Kind::Ev(EV_R_FINAL));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                // no cast time: takes effect immediately, costs nothing, but
                // was still gated by castable_at above
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_shield_until = t + self.w_duration;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_wave_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.e_wave2_at = t + self.e_wave2_offset;
                self.s.e_wave3_at = t + self.e_wave3_offset;
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_E_WAVE2) => {
                self.s.e_wave2_at = INF;
                let dmg = self.e_wave_dmg * self.e_reduction;
                e.deal(dmg, DType::Magic, self.src_e_wave, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_WAVE3) => {
                self.s.e_wave3_at = INF;
                let dmg = self.e_wave_dmg * self.e_reduction;
                e.deal(dmg, DType::Magic, self.src_e_wave, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_POW_TICK) => {
                self.s.pow_tick_at = INF;
                e.deal(self.s.pow_tick_amt, DType::Magic, SRC_W, false, false, 1.0);
            }
            Kind::Ev(EV_R_UNDERWAY) => {
                self.s.r_underway_at = INF;
                e.deal(self.r_secondary_dmg, DType::Magic, self.src_r_underway, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
            }
            Kind::Ev(EV_R_FINAL) => {
                self.s.r_final_at = INF;
                e.deal(self.r_primary_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
