//! Jayce, toggling Transform on cooldown between Cannon Stance (Shock Blast,
//! Hyper Charge, Acceleration Gate) and Hammer Stance (To the Skies!,
//! Lightning Field, Thundering Blow). Each Transform arms the new stance's
//! on-attack empowered effect (Cannon's armor/MR shred, Hammer's bonus
//! magic damage), Acceleration Gate keeps a supercharge window up for
//! Shock Blast, and Hyper Charge empowers and resets the next 3 attacks.
//! Thundering Blow has a real 0.25 s cast time (gen.E.castTimeS): it keeps
//! Jayce busy until it ends, exactly when its damage lands; every other
//! cast here has no cast time but still waits out that busy window.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Transform is ready to swap stance again.
const EV_TRANSFORM: u8 = 0;
/// The current stance's W (Hyper Charge or Lightning Field) is cast.
const EV_W_CAST: u8 = 1;
/// Hyper Charge's empower window lapses without using all its attacks.
const EV_W_EXPIRE: u8 = 2;
/// A Lightning Field tick.
const EV_W_TICK: u8 = 3;
/// The current stance's E (Acceleration Gate or Thundering Blow) is cast.
const EV_E_CAST: u8 = 4;
/// Thundering Blow's damage, landing at the end of its cast time.
const EV_E_HIT: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_cannon_dmg: f64,
    q_cannon_cd: f64,
    q_cannon_empower: f64,
    q_hammer_dmg: f64,
    q_hammer_cd: f64,

    w_hyper_dmg: f64,
    w_hyper_num: i64,
    w_hyper_window: f64,
    w_hyper_as: f64,
    w_hyper_cd: f64,
    w_field_tick_dmg: f64,
    w_field_ticks: i64,
    w_field_tick_interval: f64,
    w_field_cd: f64,

    e_cast_s: f64,
    e_blow_hp_ratio: f64,
    e_blow_ad_dmg: f64,
    e_blow_post_lockout: f64,
    e_blow_cd: f64,
    e_gate_duration: f64,
    e_gate_cd: f64,

    r_shred_dur: f64,
    r_hammer_dmg: f64,
    r_cd: f64,

    src_w_onhit: SourceId,
    src_w_field: SourceId,
    src_r_hammer: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    in_cannon: bool,
    transform_ready: f64,
    cannon_q_ready: f64,
    hammer_q_ready: f64,
    /// A Thundering Blow shared lockout on Shock Blast/To the Skies!.
    q_lockout_until: f64,
    cannon_w_ready: f64,
    hammer_w_ready: f64,
    /// Hyper Charge's active window (INF: none active).
    w_active_until: f64,
    w_attacks_left: i64,
    field_active: bool,
    field_next_tick: f64,
    field_ticks_left: i64,
    cannon_e_ready: f64,
    hammer_e_ready: f64,
    /// Acceleration Gate's supercharge window for Shock Blast.
    gate_active_until: f64,
    /// Thundering Blow's pending damage instant (INF: none pending).
    e_hit_at: f64,
    cannon_shred_pending: bool,
    hammer_bonus_pending: bool,
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
            in_cannon: false,
            transform_ready: 0.0,
            cannon_q_ready: 0.0,
            hammer_q_ready: 0.0,
            q_lockout_until: 0.0,
            cannon_w_ready: 0.0,
            hammer_w_ready: 0.0,
            w_active_until: INF,
            w_attacks_left: 0,
            field_active: false,
            field_next_tick: INF,
            field_ticks_left: 0,
            cannon_e_ready: 0.0,
            hammer_e_ready: 0.0,
            gate_active_until: INF,
            e_hit_at: INF,
            cannon_shred_pending: false,
            hammer_bonus_pending: false,
        };
        let w_field_total = kit.hit("gen.W.lightningField.damage", ranks.w, sheet)?;
        let w_field_ticks = kit.num("gen.W.lightningField.ticks")? as i64;
        let e_blow_ad_ratio = kit.num("gen.E.hammer.bonusAdRatio")?;
        Ok(GenDriver {
            ranks,
            attack_range: kit.num("gen.R.cannonAttackRange")?,
            windup_fraction: kit.windup_fraction.ok_or("jayce kit needs attack.windupFraction")?,

            q_cannon_dmg: kit.hit("gen.Q.cannon.damage", ranks.q, sheet)?,
            q_cannon_cd: kit.num("gen.Q.cannon.cooldownS")?,
            q_cannon_empower: kit.num("gen.Q.cannon.empowerMultiplier")?,
            q_hammer_dmg: kit.hit("gen.Q.hammer.damage", ranks.q, sheet)?,
            q_hammer_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,

            w_hyper_dmg: kit.hit("gen.W.hyperCharge.damage", ranks.w, sheet)?,
            w_hyper_num: kit.num("gen.W.hyperCharge.numAttacks")? as i64,
            w_hyper_window: kit.num("gen.W.hyperCharge.windowS")?,
            w_hyper_as: kit.num("gen.W.hyperCharge.bonusAsPct")?,
            w_hyper_cd: kit.at_rank("gen.W.hyperCharge.cooldownS", ranks.w)?,
            w_field_tick_dmg: w_field_total / (w_field_ticks as f64),
            w_field_ticks,
            w_field_tick_interval: kit.num("gen.W.lightningField.tickIntervalS")?,
            w_field_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,

            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_blow_hp_ratio: kit.at_rank("gen.E.hammer.targetMaxHpRatio", ranks.e)?,
            e_blow_ad_dmg: e_blow_ad_ratio * sheet.ad_bonus,
            e_blow_post_lockout: kit.num("gen.E.hammer.postLockoutS")?,
            e_blow_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_gate_duration: kit.num("gen.E.cannon.gateDurationS")?,
            e_gate_cd: kit.num("gen.E.cannon.cooldownS")?,

            r_shred_dur: kit.num("abilities.Q.shred.durationS")?,
            r_hammer_dmg: kit.at_level("gen.R.hammerDamageByLevel", level)?
                + kit.num("gen.R.hammerBonusAdRatio")? * sheet.ad_bonus,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,

            src_w_onhit: intern("W onhit"),
            src_w_field: intern("W tick"),
            src_r_hammer: intern("R onhit"),

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

    fn bonus_as(&self, t: f64) -> f64 {
        if t < self.s.w_active_until {
            self.w_hyper_as
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, _st: &mut St, t: f64, factor: f64) {
        shave(&mut self.s.cannon_q_ready, t, factor);
        shave(&mut self.s.hammer_q_ready, t, factor);
        shave(&mut self.s.cannon_w_ready, t, factor);
        shave(&mut self.s.hammer_w_ready, t, factor);
        shave(&mut self.s.cannon_e_ready, t, factor);
        shave(&mut self.s.hammer_e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.cannon_shred_pending {
            self.s.cannon_shred_pending = false;
            e.st.shred_until = t + self.r_shred_dur;
        }
        if self.s.hammer_bonus_pending {
            self.s.hammer_bonus_pending = false;
            e.deal(self.r_hammer_dmg, DType::Magic, self.src_r_hammer, false, false, 1.0);
        }
        if self.s.w_attacks_left > 0 && t < self.s.w_active_until {
            self.s.w_attacks_left -= 1;
            e.deal(self.w_hyper_dmg, DType::Physical, self.src_w_onhit, true, false, 1.0);
            if self.s.w_attacks_left == 0 {
                self.s.cannon_w_ready = t + e.basic_cd(self.w_hyper_cd);
                self.s.w_active_until = INF;
            }
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        let ready = if self.s.in_cannon {
            self.s.cannon_q_ready
        } else {
            self.s.hammer_q_ready
        };
        self.castable_at(e, pymax(ready, self.s.q_lockout_until))
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.in_cannon {
            self.s.cannon_q_ready = t + e.basic_cd(self.q_cannon_cd);
            let dmg = if t < self.s.gate_active_until {
                self.q_cannon_dmg * self.q_cannon_empower
            } else {
                self.q_cannon_dmg
            };
            e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
            e.lockout();
        } else {
            self.s.hammer_q_ready = t + e.basic_cd(self.q_hammer_cd);
            e.deal(self.q_hammer_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        }
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.in_cannon = true;
        self.s.cannon_shred_pending = true;
        self.s.transform_ready = t + e.ult_cd(self.r_cd);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        let t = e.st.t;
        if self.ranks.r > 0 {
            let mut rt = self.s.transform_ready;
            if self.s.in_cannon && self.s.w_active_until != INF {
                rt = pymax(rt, self.s.w_active_until);
            }
            out[n] = (self.castable_at(e, rt), Kind::Ev(EV_TRANSFORM));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.in_cannon {
                if self.s.w_active_until != INF {
                    out[n] = (self.s.w_active_until, Kind::Ev(EV_W_EXPIRE));
                    n += 1;
                } else {
                    out[n] = (self.castable_at(e, self.s.cannon_w_ready), Kind::Ev(EV_W_CAST));
                    n += 1;
                }
            } else if !self.s.field_active {
                out[n] = (self.castable_at(e, self.s.hammer_w_ready), Kind::Ev(EV_W_CAST));
                n += 1;
            }
            if self.s.field_active {
                out[n] = (self.s.field_next_tick, Kind::Ev(EV_W_TICK));
                n += 1;
            }
        }
        if self.ranks.e > 0 {
            if self.s.in_cannon {
                out[n] = (self.castable_at(e, self.s.cannon_e_ready), Kind::Ev(EV_E_CAST));
                n += 1;
            } else if self.s.e_hit_at != INF {
                out[n] = (self.s.e_hit_at, Kind::Ev(EV_E_HIT));
                n += 1;
            } else {
                out[n] = (self.castable_at(e, self.s.hammer_e_ready), Kind::Ev(EV_E_CAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_TRANSFORM) => {
                // switching stance discards, rather than applies, a still
                // pending empowered attack from the old stance
                self.s.cannon_shred_pending = false;
                self.s.hammer_bonus_pending = false;
                self.s.in_cannon = !self.s.in_cannon;
                self.s.transform_ready = t + e.ult_cd(self.r_cd);
                if self.s.in_cannon {
                    self.s.cannon_shred_pending = true;
                } else {
                    self.s.hammer_bonus_pending = true;
                }
            }
            Kind::Ev(EV_W_CAST) => {
                if self.s.in_cannon {
                    self.s.w_active_until = t + self.w_hyper_window;
                    self.s.w_attacks_left = self.w_hyper_num;
                    e.ability_cast_proc();
                    e.prime_spellblade();
                    // Hyper Charge resets Jayce's basic attack timer
                    let b = self.bonus_as(t);
                    e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
                } else {
                    self.s.hammer_w_ready = t + e.basic_cd(self.w_field_cd);
                    self.s.field_active = true;
                    self.s.field_ticks_left = self.w_field_ticks;
                    self.s.field_next_tick = t + self.w_field_tick_interval;
                    e.ability_cast_proc();
                    e.eclipse_hit();
                    e.prime_spellblade();
                }
            }
            Kind::Ev(EV_W_EXPIRE) => {
                if self.s.w_attacks_left > 0 {
                    self.s.cannon_w_ready = t + e.basic_cd(self.w_hyper_cd);
                }
                self.s.w_active_until = INF;
                self.s.w_attacks_left = 0;
            }
            Kind::Ev(EV_W_TICK) => {
                e.deal(self.w_field_tick_dmg, DType::Magic, self.src_w_field, false, true, 1.0);
                self.s.field_ticks_left -= 1;
                if self.s.field_ticks_left > 0 {
                    self.s.field_next_tick = t + self.w_field_tick_interval;
                } else {
                    self.s.field_active = false;
                    self.s.field_next_tick = INF;
                }
            }
            Kind::Ev(EV_E_CAST) => {
                if self.s.in_cannon {
                    self.s.cannon_e_ready = t + e.basic_cd(self.e_gate_cd);
                    self.s.gate_active_until = t + self.e_gate_duration;
                    e.prime_spellblade();
                } else {
                    self.s.hammer_e_ready = t + e.basic_cd(self.e_blow_cd);
                    self.s.e_hit_at = t + self.e_cast_s;
                    e.prime_spellblade();
                    self.busy_for(e, self.e_cast_s);
                }
            }
            Kind::Ev(EV_E_HIT) => {
                self.s.e_hit_at = INF;
                let dmg = self.e_blow_hp_ratio * e.target_hp + self.e_blow_ad_dmg;
                e.deal(dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.q_lockout_until = t + self.e_blow_post_lockout;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
