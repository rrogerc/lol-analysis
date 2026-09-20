//! Varus. Chain of Corruption opens the fight; Piercing Arrow (always fully
//! charged) and Hail of Arrows go out on cooldown; auto-attacks fill every
//! gap, feeding Blighted Quiver's on-hit damage and stacks, which every
//! ability hit detonates for bonus magic damage and cooldown refund.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Chain of Corruption is cast, and its delayed Blight stack application.
const EV_R_CAST: u8 = 0;
const EV_R_STACK: u8 = 1;
/// Hail of Arrows is cast, and its delayed landing.
const EV_E_CAST: u8 = 2;
const EV_E_LAND: u8 = 3;

fn reduce_cd(target: &mut f64, full_cd: f64, t: f64, pct: f64) {
    if *target > t {
        *target = pymax(t, *target - full_cd * pct);
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_cd_base: f64,
    q_dmg: f64,
    q_charge_s: f64,

    w_onhit_dmg: f64,
    w_hp_pct: f64,
    w_max_stacks: i64,
    w_stack_dur: f64,
    w_cdr_per_stack: f64,
    w_charge_mult: f64,
    w_active_pct: f64,
    w_active_cd_base: f64,

    e_dmg: f64,
    e_cd_base: f64,
    e_land_delay: f64,

    r_dmg: f64,
    r_cd_base: f64,
    r_stack_delay: f64,

    src_w_onhit: SourceId,
    src_w_deton_q: SourceId,
    src_w_deton_e: SourceId,
    src_w_deton_r: SourceId,
    src_w_active: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    blight_stacks: i64,
    blight_expire: f64,
    w_active_ready: f64,
    e_ready: f64,
    e_land_at: f64,
    r_ready: f64,
    r_stack_at: f64,
}

impl GenDriver {
    /// Detonates all current Blight stacks on an ability hit: magic damage
    /// off the target's maximum health, and a cooldown refund on Q, W's
    /// active and E, all boosted if triggered via a (always fully-charged) Q.
    fn detonate(&mut self, e: &mut Engine, via_q: bool, src: SourceId) {
        let t = e.st.t;
        if self.s.blight_stacks <= 0 || t >= self.s.blight_expire {
            self.s.blight_stacks = 0;
            return;
        }
        let stacks = self.s.blight_stacks as f64;
        let mult = if via_q { self.w_charge_mult } else { 1.0 };
        let dmg = self.w_hp_pct * stacks * mult * e.target_hp;
        e.deal(dmg, DType::Magic, src, false, false, 1.0);

        let total_pct = self.w_cdr_per_stack * mult * stacks;
        let q_full = e.basic_cd(self.q_cd_base);
        let e_full = e.basic_cd(self.e_cd_base);
        let w_full = e.basic_cd(self.w_active_cd_base);
        reduce_cd(&mut e.st.q_ready, q_full, t, total_pct);
        reduce_cd(&mut self.s.e_ready, e_full, t, total_pct);
        reduce_cd(&mut self.s.w_active_ready, w_full, t, total_pct);

        self.s.blight_stacks = 0;
    }

    fn fire_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
        self.detonate(e, false, self.src_w_deton_r);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
        self.s.r_stack_at = t + self.r_stack_delay;
        self.s.r_ready = t + e.ult_cd(self.r_cd_base);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            blight_stacks: 0,
            blight_expire: 0.0,
            w_active_ready: 0.0,
            e_ready: 0.0,
            e_land_at: INF,
            r_ready: INF,
            r_stack_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("varus kit needs attack.windupFraction")?,

            q_cd_base: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_charge_s: kit.num("gen.Q.chargeDurationS")?,

            w_onhit_dmg: kit.hit("gen.W.onhit", ranks.w, sheet)?,
            w_hp_pct: kit.at_rank("gen.W.detonation.baseHpPct", ranks.w)?
                + kit.num("gen.W.detonation.apCoefPerAp")? * sheet.ap,
            w_max_stacks: kit.num("gen.W.maxStacks")? as i64,
            w_stack_dur: kit.num("gen.W.stackDurationS")?,
            w_cdr_per_stack: kit.num("gen.W.cdrPerStack")?,
            w_charge_mult: kit.num("gen.W.chargeBonusMult")?,
            w_active_pct: kit.at_rank("gen.W.active.missingHpPct", ranks.w)?,
            w_active_cd_base: kit.at_rank("abilities.W.cooldownS", ranks.w)?,

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd_base: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_land_delay: kit.num("gen.E.landDelayS")?,

            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd_base: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_stack_delay: kit.num("gen.R.stackDelayS")?,

            src_w_onhit: intern("W onhit"),
            src_w_deton_q: intern("W deton Q"),
            src_w_deton_e: intern("W deton E"),
            src_w_deton_r: intern("W deton R"),
            src_w_active: intern("W active"),

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
        shave(&mut self.s.w_active_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        self.s.blight_stacks = imin(self.s.blight_stacks + 1, self.w_max_stacks);
        self.s.blight_expire = e.st.t + self.w_stack_dur;
        e.deal(self.w_onhit_dmg, DType::Magic, self.src_w_onhit, false, false, 1.0);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        // Piercing Arrow is always charged to the 1.25s effect cap: its
        // damage and detonation bonus are at maximum, its cooldown is
        // unaffected (the post-effect reduction cancels the charge time),
        // and attacks are held back for the charge duration.
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        self.detonate(e, true, self.src_w_deton_q);
        if self.ranks.w > 0 && t >= self.s.w_active_ready {
            let missing = e.target_hp - pymax(e.st.hp, 0.0);
            let dmg = self.w_active_pct * self.w_charge_mult * missing;
            e.deal(dmg, DType::Magic, self.src_w_active, false, true, 1.0);
            self.s.w_active_ready = t + e.basic_cd(self.w_active_cd_base);
        }
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.st.next_attack = pymax(e.st.next_attack, t + self.q_charge_s);
        e.st.q_ready = t + e.basic_cd(self.q_cd_base);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r > 0 {
            self.fire_r(e);
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.r > 0 {
            out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        if self.s.r_stack_at != INF {
            out[n] = (self.s.r_stack_at, Kind::Ev(EV_R_STACK));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.e_land_at != INF {
            out[n] = (self.s.e_land_at, Kind::Ev(EV_E_LAND));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_CAST) => {
                self.fire_r(e);
            }
            Kind::Ev(EV_R_STACK) => {
                self.s.blight_stacks = self.w_max_stacks;
                self.s.blight_expire = t + self.w_stack_dur;
                self.s.r_stack_at = INF;
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd_base);
                self.s.e_land_at = t + self.e_land_delay;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_LAND) => {
                self.s.e_land_at = INF;
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                self.detonate(e, false, self.src_w_deton_e);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
