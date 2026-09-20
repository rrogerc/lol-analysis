//! Shaco. Opens with Deceive for its guaranteed-crit empowered attack and
//! Hallucinate for its explosion/mini-box payload (assumed to resolve right
//! after the cast, see kit notes), then weaves Two-Shiv Poison on cooldown
//! and re-throws Jack in the Box on cooldown while basic-attacking;
//! Backstab rides every attack and the from-behind bonuses on Q and E are
//! assumed active for the whole fight.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Jack in the Box is cast; then its ticks while sprung.
const EV_W_CAST: u8 = 0;
const EV_W_TICK: u8 = 1;
/// Two-Shiv Poison is cast (damage lands immediately).
const EV_E_CAST: u8 = 2;
/// Hallucinate's explosion (assumed to land right after the cast); then the
/// three mini-boxes' recurring ticks.
const EV_R_EXPLOSION: u8 = 3;
const EV_R_TICK: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,

    p_dmg: f64,

    q_stealth_s: f64,
    q_cd: f64,
    q_bonus_dmg: f64,

    w_dmg: f64,
    w_cd: f64,
    w_arm_s: f64,
    w_active_s: f64,
    w_tick_interval_s: f64,
    w_num_ticks: i64,

    e_dmg: f64,
    e_backstab_dmg: f64,
    e_cd: f64,
    e_execute_threshold: f64,
    e_execute_mult: f64,

    r_explosion_dmg: f64,
    r_box_dmg: f64,
    r_num_boxes: f64,
    r_cast_s: f64,
    r_tick_interval_s: f64,
    r_num_ticks: i64,

    src_p: SourceId,
    src_e_backstab: SourceId,
    src_r_box: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_armed: bool,
    w_ready: f64,
    w_tick_next: f64,
    w_ticks_left: i64,
    e_ready: f64,
    r_explosion_at: f64,
    r_tick_next: f64,
    r_ticks_left: i64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let non_crit_mod_coef = kit.num("gen.Q.nonCritBackstabMod")?;
        let p = sheet.crit_chance / 100.0;
        let cd_mult = sheet.crit_damage / 100.0;
        let non_crit_mod = 1.0 + non_crit_mod_coef * (cd_mult - 1.0);
        let expected_crit_mult = (1.0 - p) * non_crit_mod + p * cd_mult;

        let q_base = kit.hit("gen.Q.damage", ranks.q, sheet)?;

        let p_base_level = kit.at_level("gen.P.damage.baseByLevel", level)?;
        let p_bonus_ad_ratio = kit.num("gen.P.damage.bonusAdRatio")?;

        let e_backstab_base_level = kit.at_level("gen.E.backstabDamage.baseByLevel", level)?;
        let e_backstab_ap_ratio = kit.num("gen.E.backstabDamage.apRatio")?;

        let w_active_s = kit.num("gen.W.activeDurationS")?;
        let w_tick_interval_s = kit.num("gen.W.tickIntervalS")?;

        let r_box_lifetime_s = kit.num("gen.R.boxLifetimeS")?;
        let r_tick_interval_s = kit.num("gen.R.tickIntervalS")?;

        let state = State {
            q_armed: false,
            w_ready: 0.0,
            w_tick_next: INF,
            w_ticks_left: 0,
            e_ready: 0.0,
            r_explosion_at: INF,
            r_tick_next: INF,
            r_ticks_left: 0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,

            p_dmg: p_base_level + p_bonus_ad_ratio * sheet.ad_bonus,

            q_stealth_s: kit.at_rank("gen.Q.stealthDurationS", ranks.q)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_bonus_dmg: q_base * expected_crit_mult,

            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_arm_s: kit.num("gen.W.armTimeS")?,
            w_active_s,
            w_tick_interval_s,
            w_num_ticks: (w_active_s / w_tick_interval_s) as i64,

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_backstab_dmg: e_backstab_base_level + e_backstab_ap_ratio * sheet.ap,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_execute_threshold: kit.num("gen.E.executeThreshold")?,
            e_execute_mult: kit.num("gen.E.executeMult")?,

            r_explosion_dmg: kit.hit("gen.R.explosion", ranks.r, sheet)?,
            r_box_dmg: kit.hit("gen.R.boxDamage", ranks.r, sheet)?,
            r_num_boxes: kit.num("gen.R.numBoxes")?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_tick_interval_s,
            r_num_ticks: (r_box_lifetime_s / r_tick_interval_s) as i64,

            src_p: intern("P backstab"),
            src_e_backstab: intern("E backstab"),
            src_r_box: intern("R box"),

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

    fn attack_riders(&mut self, e: &mut Engine) {
        // Backstab: bonus physical damage on every attack, assumed always
        // landing from behind; affected by crit like the wiki states.
        e.deal(self.p_dmg, DType::Physical, self.src_p, true, false, 1.0);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // Deceive's empowered-attack bonus rides the next attack to land.
        if self.s.q_armed {
            self.s.q_armed = false;
            e.deal(self.q_bonus_dmg, DType::Physical, SRC_Q, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
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
        self.s.q_armed = true;
        // Cooldown is post-effect: it starts once the stealth duration ends.
        e.st.q_ready = t + self.q_stealth_s + e.basic_cd(self.q_cd);
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // The opening cast: the engine has primed Spellblade and held the
        // first attack past the cast; the explosion is assumed to resolve
        // when the cast completes (see kit notes).
        self.s.r_explosion_at = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
            if self.s.w_ticks_left > 0 {
                out[n] = (self.s.w_tick_next, Kind::Ev(EV_W_TICK));
                n += 1;
            }
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.r_explosion_at != INF {
            out[n] = (self.s.r_explosion_at, Kind::Ev(EV_R_EXPLOSION));
            n += 1;
        }
        if self.s.r_ticks_left > 0 {
            out[n] = (self.s.r_tick_next, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_ticks_left = self.w_num_ticks;
                self.s.w_tick_next = t + self.w_arm_s + self.w_tick_interval_s;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_W_TICK) => {
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                if self.s.w_ticks_left == self.w_num_ticks {
                    // First tick of this cast: the moment the box's damage lands.
                    e.ability_cast_proc();
                    e.eclipse_hit();
                }
                self.s.w_ticks_left -= 1;
                if self.s.w_ticks_left > 0 {
                    self.s.w_tick_next = t + self.w_tick_interval_s;
                } else {
                    self.s.w_tick_next = INF;
                }
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                let hp_ratio = pymax(e.st.hp, 0.0) / e.target_hp;
                let mult = if hp_ratio < self.e_execute_threshold {
                    self.e_execute_mult
                } else {
                    1.0
                };
                e.deal(self.e_dmg * mult, DType::Magic, SRC_E, false, true, 1.0);
                e.deal(self.e_backstab_dmg * mult, DType::Magic, self.src_e_backstab, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_R_EXPLOSION) => {
                self.s.r_explosion_at = INF;
                e.deal(self.r_explosion_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                // The three deployed mini-boxes start ticking right away.
                self.s.r_ticks_left = self.r_num_ticks;
                self.s.r_tick_next = t + self.r_tick_interval_s;
            }
            Kind::Ev(EV_R_TICK) => {
                // Three mini-boxes firing simultaneously at the lone target,
                // modeled as one combined magic damage instance.
                e.deal(self.r_box_dmg * self.r_num_boxes, DType::Magic, self.src_r_box, false, true, 1.0);
                self.s.r_ticks_left -= 1;
                if self.s.r_ticks_left > 0 {
                    self.s.r_tick_next = t + self.r_tick_interval_s;
                } else {
                    self.s.r_tick_next = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
