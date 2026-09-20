//! Locke. A mixed auto-attacker/caster: Silver Stake rides every basic attack
//! (and Ashen Pursuit's empowered dash) with missing-health-scaled magic
//! on-hit damage, Ritual Nails fires and free-recasts twice more to stack
//! Soul Nails (consumed by the next damaging hit), Soul Ignition is cast for
//! its attack-speed buff, Ashen Pursuit blinks then dashes on cooldown, and
//! Purgatory opens the fight for its totem nail (and a conditional execute).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Purgatory's totem nail lands (travel + landing delay after the opening cast).
const EV_R_NAIL: u8 = 0;
/// Ritual Nails' free recast.
const EV_Q_RECAST: u8 = 1;
/// Ashen Pursuit is cast (the blink).
const EV_E_CAST: u8 = 2;
/// Ashen Pursuit's empowered dash lands.
const EV_E_DASH: u8 = 3;
/// Soul Ignition is cast (for its attack-speed buff).
const EV_W_CAST: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    p_flat: f64,
    p_missing_cap: f64,

    q_missile_dmg: f64,
    q_stack1: f64,
    q_stack2: f64,
    q_stack3: f64,
    q_cd_base: f64,
    q_recast_interval_s: f64,
    q_ammo: i64,
    q_max_stacks: i64,
    q_nail_duration_s: f64,

    w_as_pct: f64,
    w_duration_s: f64,
    w_cd_base: f64,

    e_blink_dmg: f64,
    e_dash_dmg: f64,
    e_cd_base: f64,
    e_dash_delay_s: f64,

    r_dmg: f64,
    r_execute_threshold: f64,
    r_travel_s: f64,
    r_land_delay_s: f64,

    src_p: SourceId,
    src_q_consume: SourceId,
    src_e_blink: SourceId,
    src_e_dash: SourceId,
    src_r_execute: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_recasts_left: i64,
    q_recast_at: f64,
    nail_stacks: i64,
    nail_expire: f64,
    w_ready: f64,
    w_active_until: f64,
    e_ready: f64,
    e_dash_at: f64,
    r_nail_at: f64,
}

impl GenDriver {
    /// Silver Stake's bonus on-hit magic damage right now: the flat base+AP
    /// term, scaled continuously from 1x at 0% target missing health up to
    /// 2x at 70%+ missing health.
    fn p_onhit(&self, e: &Engine) -> f64 {
        let missing = (e.target_hp - pymax(e.st.hp, 0.0)) / e.target_hp;
        let mult = 1.0 + pymin(missing / self.p_missing_cap, 1.0);
        self.p_flat * mult
    }

    /// One live Soul Nails stack for the fixed duration.
    fn add_nail_stack(&mut self, t: f64) {
        self.s.nail_stacks = imin(self.s.nail_stacks + 1, self.q_max_stacks);
        self.s.nail_expire = t + self.q_nail_duration_s;
    }

    /// The on-hit package a damaging attack or Ashen Pursuit's dash applies:
    /// Silver Stake's bonus on-hit, plus consuming any live Soul Nails stacks.
    fn onhit_effects(&mut self, e: &mut Engine) {
        let dmg = self.p_onhit(e);
        e.deal(dmg, DType::Magic, self.src_p, false, false, 1.0);
        if self.s.nail_stacks > 0 && e.st.t < self.s.nail_expire {
            let bonus = match self.s.nail_stacks {
                1 => self.q_stack1,
                2 => self.q_stack2,
                _ => self.q_stack3,
            };
            e.deal(bonus, DType::Magic, self.src_q_consume, false, false, 1.0);
            self.s.nail_stacks = 0;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            q_recasts_left: 0,
            q_recast_at: INF,
            nail_stacks: 0,
            nail_expire: -1.0,
            w_ready: 0.0,
            w_active_until: -1.0,
            e_ready: 0.0,
            e_dash_at: INF,
            r_nail_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("locke kit needs attack.windupFraction")?,

            p_flat: kit.at_level("gen.P.onhitBase", level)? + kit.num("gen.P.apRatio")? * sheet.ap,
            p_missing_cap: kit.num("gen.P.missingHpCapPct")? / 100.0,

            q_missile_dmg: kit.hit("gen.Q.missileDamage", ranks.q, sheet)?,
            q_stack1: kit.hit("gen.Q.stack1", ranks.q, sheet)?,
            q_stack2: kit.hit("gen.Q.stack2", ranks.q, sheet)?,
            q_stack3: kit.hit("gen.Q.stack3", ranks.q, sheet)?,
            q_cd_base: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_recast_interval_s: kit.num("gen.Q.recastIntervalS")?,
            q_ammo: kit.num("gen.Q.ammo")? as i64,
            q_max_stacks: kit.num("gen.Q.maxStacks")? as i64,
            q_nail_duration_s: kit.num("gen.Q.nailDurationS")?,

            w_as_pct: kit.at_level("gen.W.asPctByLevel", level)? * 100.0,
            w_duration_s: kit.num("gen.W.durationS")?,
            w_cd_base: kit.at_rank("abilities.W.cooldownS", ranks.w)?,

            e_blink_dmg: kit.hit("gen.E.blinkDamage", ranks.e, sheet)?,
            e_dash_dmg: kit.hit("gen.E.dashDamage", ranks.e, sheet)?,
            e_cd_base: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_dash_delay_s: kit.num("gen.E.dashDelayS")?,

            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_execute_threshold: kit.at_rank("gen.R.executeThresholdByRank", ranks.r)?
                + kit.num("gen.R.executePerStack")? * kit.num("gen.R.assumedSealedStacks")?,
            r_travel_s: kit.num("gen.R.travelS")?,
            r_land_delay_s: kit.num("gen.R.landDelayS")?,

            src_p: intern("P onhit"),
            src_q_consume: intern("Q stack consume"),
            src_e_blink: intern("E blink"),
            src_e_dash: intern("E dash"),
            src_r_execute: intern("R execute"),

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
        if t < self.s.w_active_until { self.w_as_pct } else { 0.0 }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        self.onhit_effects(e);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.q_missile_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        self.add_nail_stack(t);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
        self.s.q_recasts_left = self.q_ammo - 1;
        self.s.q_recast_at = t + self.q_recast_interval_s;
        e.st.q_ready = INF;
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_nail_at = t + self.r_travel_s + self.r_land_delay_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.r > 0 && self.s.r_nail_at != INF {
            out[n] = (self.s.r_nail_at, Kind::Ev(EV_R_NAIL));
            n += 1;
        }
        if self.ranks.q > 0 && self.s.q_recast_at != INF {
            out[n] = (self.s.q_recast_at, Kind::Ev(EV_Q_RECAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_dash_at != INF {
                out[n] = (self.s.e_dash_at, Kind::Ev(EV_E_DASH));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_NAIL) => {
                self.s.r_nail_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                let cur = pymax(e.st.hp, 0.0);
                if cur > 0.0 && cur <= self.r_execute_threshold * e.target_hp {
                    e.deal(cur, DType::True, self.src_r_execute, false, true, 1.0);
                }
            }
            Kind::Ev(EV_Q_RECAST) => {
                e.deal(self.q_missile_dmg, DType::Magic, SRC_Q, false, true, 1.0);
                self.add_nail_stack(t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.q_recasts_left -= 1;
                if self.s.q_recasts_left > 0 {
                    self.s.q_recast_at = t + self.q_recast_interval_s;
                } else {
                    self.s.q_recast_at = INF;
                    e.st.q_ready = t + e.basic_cd(self.q_cd_base);
                }
            }
            Kind::Ev(EV_E_CAST) => {
                e.deal(self.e_blink_dmg, DType::Magic, self.src_e_blink, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
                self.s.e_dash_at = t + self.e_dash_delay_s;
                self.s.e_ready = INF;
            }
            Kind::Ev(EV_E_DASH) => {
                self.s.e_dash_at = INF;
                e.deal(self.e_dash_dmg, DType::Magic, self.src_e_dash, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.onhit_effects(e);
                self.s.e_ready = t + e.basic_cd(self.e_cd_base);
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_active_until = t + self.w_duration_s;
                self.s.w_ready = self.s.w_active_until + e.basic_cd(self.w_cd_base);
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
