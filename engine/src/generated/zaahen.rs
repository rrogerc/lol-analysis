//! Zaahen. A melee auto-attacker whose Determination passive dynamically
//! grows his bonus AD as attacks and abilities land, feeding both his basic
//! attacks and every bonus-AD-ratio ability term. The Darkin Glaive arms his
//! next basic attack for bonus damage, auto-recasting 1.5s after it lands
//! for a second empowered attack; Dreaded Return and Aureate Rush go out on
//! cooldown with a delayed hit; Grim Deliverance opens the fight and casts
//! only once given its long cooldown.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// The recast of Q auto-arms itself, then expires unused if it never lands.
const EV_Q_RECAST_ARM: u8 = 0;
const EV_Q_RECAST_EXPIRE: u8 = 1;
/// W is cast on cooldown; its damage lands after its cast time.
const EV_W_CAST: u8 = 2;
const EV_W_HIT: u8 = 3;
/// E is cast on cooldown; its damage lands after the dash's travel time.
const EV_E_CAST: u8 = 4;
const EV_E_HIT: u8 = 5;
/// R's shockwave lands after the cast time plus the slam delay.
const EV_R_SWING: u8 = 6;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    p_stack_pct: f64,
    p_max_stacks: i64,
    p_max_mult: f64,
    q_cd: f64,
    q1_base: f64,
    q_coeff: f64,
    q2_base: f64,
    q_recast_delay_s: f64,
    q_recast_window_s: f64,
    w_cd: f64,
    w_cast_time_s: f64,
    w1_base: f64,
    w1_coeff: f64,
    w2_base: f64,
    w2_coeff: f64,
    e_cd: f64,
    e_travel_s: f64,
    e_base: f64,
    e_coeff: f64,
    e_hp_ratio: f64,
    r_cast_time_s: f64,
    r_slam_delay_s: f64,
    r_base: f64,
    r_coeff: f64,
    src_q_recast: SourceId,
    src_w2: SourceId,
    src_e_magic: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    /// True from Q's first cast until its cooldown truly starts (post-effect).
    q_busy: bool,
    /// 0 none, 1 first empowerment armed, 2 recast empowerment armed.
    q_armed: i64,
    q_recast_arm_at: f64,
    q_recast_expire_at: f64,
    w_ready: f64,
    w_hit_at: f64,
    e_ready: f64,
    e_hit_at: f64,
    r_swing_at: f64,
}

impl GenDriver {
    fn gain_stack(&mut self) {
        self.s.p_stacks = imin(self.s.p_stacks + 1, self.p_max_stacks);
    }

    /// The bonus AD Determination currently grants, updating dynamically
    /// with Zaahen's other bonus AD.
    fn passive_bonus_ad(&self, e: &Engine) -> f64 {
        let mult = if self.s.p_stacks >= self.p_max_stacks { self.p_max_mult } else { 1.0 };
        e.p.sheet.ad_bonus * self.p_stack_pct * (self.s.p_stacks as f64) * mult
    }

    fn bonus_ad_total(&self, e: &Engine) -> f64 {
        e.p.sheet.ad_bonus + self.passive_bonus_ad(e)
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            q_busy: false,
            q_armed: 0,
            q_recast_arm_at: INF,
            q_recast_expire_at: INF,
            w_ready: 0.0,
            w_hit_at: INF,
            e_ready: 0.0,
            e_hit_at: INF,
            r_swing_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("zaahen kit needs attack.windupFraction")?,
            p_stack_pct: kit.at_level("gen.P.bonusAdPctByLevel", level)? / 100.0,
            p_max_stacks: kit.num("gen.P.maxStacks")? as i64,
            p_max_mult: kit.num("gen.P.maxStacksMultiplier")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q1_base: kit.at_rank("gen.Q.q1Base", ranks.q)?,
            q_coeff: kit.at_rank("gen.Q.qCoeff", ranks.q)?,
            q2_base: kit.at_rank("gen.Q.q2Base", ranks.q)?,
            q_recast_delay_s: kit.num("gen.Q.recastDelayS")?,
            q_recast_window_s: kit.num("gen.Q.recastWindowS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_time_s: kit.num("gen.W.castTimeS")?,
            w1_base: kit.at_rank("gen.W.initialBase", ranks.w)?,
            w1_coeff: kit.num("gen.W.initialCoeff")?,
            w2_base: kit.at_rank("gen.W.secondaryBase", ranks.w)?,
            w2_coeff: kit.num("gen.W.secondaryCoeff")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_travel_s: kit.num("gen.E.dashDistance")? / kit.num("gen.E.dashSpeed")?,
            e_base: kit.at_rank("gen.E.outerBase", ranks.e)?,
            e_coeff: kit.num("gen.E.outerCoeff")?,
            e_hp_ratio: kit.at_rank("gen.E.targetMaxHpRatio", ranks.e)?,
            r_cast_time_s: kit.num("gen.R.castTimeS")?,
            r_slam_delay_s: kit.num("gen.R.slamDelayS")?,
            r_base: kit.at_rank("gen.R.base", ranks.r)?,
            r_coeff: kit.num("gen.R.coeff")?,
            src_q_recast: intern("Q recast"),
            src_w2: intern("W secondary"),
            src_e_magic: intern("E magic"),
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

    fn attack_damage(&self, e: &Engine) -> f64 {
        e.p.ad + self.passive_bonus_ad(e)
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {
        // Determination: any landed attack generates a stack.
        self.gain_stack();
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.q_armed == 1 {
            // The first empowered attack: its ability-side bonus rides on
            // top of the engine's own normal attack damage.
            let bonus_ad = self.bonus_ad_total(e);
            let amt = self.q1_base + self.q_coeff * bonus_ad;
            e.deal(amt, DType::Physical, SRC_Q, true, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            self.gain_stack();
            self.s.q_armed = 0;
            self.s.q_recast_arm_at = t + self.q_recast_delay_s;
            self.s.q_recast_expire_at = t + self.q_recast_delay_s + self.q_recast_window_s;
        } else if self.s.q_armed == 2 {
            // The recast's empowered attack: its own bonus damage, and this
            // is where Q's real (post-effect) cooldown begins.
            let bonus_ad = self.bonus_ad_total(e);
            let amt = self.q2_base + self.q_coeff * bonus_ad;
            e.deal(amt, DType::Physical, self.src_q_recast, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            self.s.q_armed = 0;
            self.s.q_busy = false;
            self.s.q_recast_expire_at = INF;
            e.st.q_ready = t + e.basic_cd(self.q_cd);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.q_busy {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        self.s.q_busy = true;
        self.s.q_armed = 1;
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the opening cast: the shockwave lands after the cast time plus
        // the slam delay
        self.s.r_swing_at = e.st.t + self.r_cast_time_s + self.r_slam_delay_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_recast_arm_at < INF {
            out[n] = (self.s.q_recast_arm_at, Kind::Ev(EV_Q_RECAST_ARM));
            n += 1;
        }
        if self.s.q_recast_expire_at < INF {
            out[n] = (self.s.q_recast_expire_at, Kind::Ev(EV_Q_RECAST_EXPIRE));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_hit_at < INF {
                out[n] = (self.s.w_hit_at, Kind::Ev(EV_W_HIT));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_hit_at < INF {
                out[n] = (self.s.e_hit_at, Kind::Ev(EV_E_HIT));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.s.r_swing_at < INF {
            out[n] = (self.s.r_swing_at, Kind::Ev(EV_R_SWING));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_RECAST_ARM) => {
                self.s.q_recast_arm_at = INF;
                if self.s.q_armed == 0 {
                    self.s.q_armed = 2;
                    e.prime_spellblade();
                }
            }
            Kind::Ev(EV_Q_RECAST_EXPIRE) => {
                self.s.q_recast_expire_at = INF;
                if self.s.q_armed == 2 {
                    // the recast window closed unused: the cooldown starts now
                    self.s.q_armed = 0;
                    self.s.q_busy = false;
                    e.st.q_ready = t + e.basic_cd(self.q_cd);
                }
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = INF;
                self.s.w_hit_at = t + self.w_cast_time_s;
                e.lockout();
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_HIT) => {
                self.s.w_hit_at = INF;
                let bonus_ad = self.bonus_ad_total(e);
                let amt1 = self.w1_base + self.w1_coeff * bonus_ad;
                e.deal(amt1, DType::Physical, SRC_W, false, true, 1.0);
                let amt2 = self.w2_base + self.w2_coeff * bonus_ad;
                e.deal(amt2, DType::Physical, self.src_w2, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.gain_stack();
                self.gain_stack();
                self.s.w_ready = t + e.basic_cd(self.w_cd);
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = INF;
                self.s.e_hit_at = t + self.e_travel_s;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_HIT) => {
                self.s.e_hit_at = INF;
                let bonus_ad = self.bonus_ad_total(e);
                let amt = self.e_base + self.e_coeff * bonus_ad;
                e.deal(amt, DType::Physical, SRC_E, false, true, 1.0);
                let magic = self.e_hp_ratio * e.target_hp;
                e.deal(magic, DType::Magic, self.src_e_magic, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.gain_stack();
                self.gain_stack();
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
            Kind::Ev(EV_R_SWING) => {
                self.s.r_swing_at = INF;
                let bonus_ad = self.bonus_ad_total(e);
                let amt = self.r_base + self.r_coeff * bonus_ad;
                e.deal(amt, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.gain_stack();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
