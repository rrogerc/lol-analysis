//! Garen. A melee auto-attacker whose ability damage rides three pieces:
//! Decisive Strike arms and resets his next attack with bonus physical
//! damage, Judgment is a channelled spin dealing periodic physical damage
//! that ticks more with bonus attack speed and shreds armor every 6th hit,
//! and Demacian Justice opens the fight with a burst of true damage.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Judgment starts its channel; then each spin tick.
const EV_E_CAST: u8 = 0;
const EV_E_TICK: u8 = 1;
/// Demacian Justice's damage lands after its cast time.
const EV_R_DMG: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_dmg: f64,
    q_cd: f64,
    q_window_s: f64,

    e_dmg: f64,
    e_crit_mult: f64,
    e_num_ticks: i64,
    e_interval_s: f64,
    e_duration_s: f64,
    e_cd: f64,
    e_hits_for_shred: i64,
    e_shred_duration_s: f64,

    r_dmg: f64,
    r_missing_ratio: f64,
    r_cast_s: f64,

    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_armed: bool,
    q_arm_until: f64,
    /// When an active Judgment channel last locks basic attacks until.
    e_lock_until: f64,
    e_active: bool,
    e_ready: f64,
    e_cast_t: f64,
    e_next_tick_time: f64,
    e_tick_idx: i64,
    e_hits: i64,
    /// When Demacian Justice's damage lands (INF: none pending).
    r_dmg_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let e_base_ticks = kit.num("gen.E.numTicksBase")? as i64;
        let e_as_per_tick = kit.num("gen.E.asPerTickPct")?;
        let e_extra_ticks = (sheet.bonus_as_pct / e_as_per_tick) as i64;
        let e_num_ticks = e_base_ticks + e_extra_ticks;
        let e_duration_s = kit.num("gen.E.durationS")?;
        let e_interval_s = e_duration_s / (e_num_ticks as f64);

        let e_crit_mod = kit.num("gen.E.critMod")?;
        let cc = sheet.crit_chance / 100.0;
        let cd = sheet.crit_damage / 100.0;
        let e_crit_mult = 1.0 + cc * e_crit_mod * (cd - 1.0);

        let state = State {
            q_armed: false,
            q_arm_until: 0.0,
            e_lock_until: 0.0,
            e_active: false,
            e_ready: 0.0,
            e_cast_t: 0.0,
            e_next_tick_time: INF,
            e_tick_idx: 0,
            e_hits: 0,
            r_dmg_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("garen kit needs attack.windupFraction")?,

            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_window_s: kit.num("gen.Q.attackWindowS")?,

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_crit_mult,
            e_num_ticks,
            e_interval_s,
            e_duration_s,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_hits_for_shred: kit.num("gen.E.hitsForShred")? as i64,
            e_shred_duration_s: kit.num("abilities.Q.shred.durationS")?,

            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_missing_ratio: kit.at_rank("gen.R.missingHpRatio", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,

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
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.q_armed {
            let still_valid = e.st.t <= self.s.q_arm_until;
            self.s.q_armed = false;
            if still_valid {
                e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
            }
        }
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
        self.s.q_armed = true;
        self.s.q_arm_until = t + self.q_window_s;
        e.prime_spellblade();
        // Decisive Strike resets Garen's basic attack timer, but never
        // pulls an attack out of an active Judgment channel's lockout.
        let reset_at = t + e.attack_windup(0.0, self.windup_fraction);
        e.st.next_attack = pymax(reset_at, self.s.e_lock_until);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the opening cast: the engine has primed Spellblade and delayed
        // the first attack past the 0.435 s cast; the damage lands then
        self.s.r_dmg_at = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            if self.s.e_active {
                out[n] = (self.s.e_next_tick_time, Kind::Ev(EV_E_TICK));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.s.r_dmg_at != INF {
            out[n] = (self.s.r_dmg_at, Kind::Ev(EV_R_DMG));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_active = true;
                self.s.e_cast_t = t;
                self.s.e_tick_idx = 0;
                self.s.e_next_tick_time = t + self.e_interval_s;
                self.s.e_lock_until = t + self.e_duration_s;
                // Judgment locks Garen out of basic attacks while it spins
                e.st.next_attack = pymax(e.st.next_attack, self.s.e_lock_until);
                e.prime_spellblade();
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_TICK) => {
                let amt = self.e_dmg * self.e_crit_mult;
                e.deal(amt, DType::Physical, SRC_E, false, true, 1.0);
                self.s.e_hits += 1;
                if self.s.e_hits % self.e_hits_for_shred == 0 {
                    e.st.shred_until = t + self.e_shred_duration_s;
                }
                self.s.e_tick_idx += 1;
                if self.s.e_tick_idx >= self.e_num_ticks {
                    self.s.e_active = false;
                    self.s.e_ready = t + e.basic_cd(self.e_cd);
                } else {
                    self.s.e_next_tick_time =
                        self.s.e_cast_t + (self.s.e_tick_idx as f64) * self.e_interval_s;
                }
            }
            Kind::Ev(EV_R_DMG) => {
                self.s.r_dmg_at = INF;
                let missing = e.target_hp - pymax(e.st.hp, 0.0);
                let amt = self.r_dmg + self.r_missing_ratio * missing;
                e.deal(amt, DType::True, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
