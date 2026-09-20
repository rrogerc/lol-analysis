//! Akshan. Auto-attacks feed Dirty Fighting: each primary attack, and its
//! own delayed second shot, add a stack, and the third stack detonates a
//! magic proc. Avengerang goes out and comes back on cooldown, Heroic Swing
//! fires its two mandatory shots on cooldown, and Comeuppance channels to
//! full bullets before firing its volley.

use crate::fight::{Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Avengerang's homing return pass, landing after the assumed travel time.
const EV_Q_RETURN: u8 = 0;
/// Heroic Swing: the first (start-of-swing) shot, and the second
/// (dismount) shot after the minimum third-cast delay.
const EV_E_CAST: u8 = 1;
const EV_E_SHOT2: u8 = 2;
/// Comeuppance: the channel starting, and the volley that ends it.
const EV_R_CAST: u8 = 3;
const EV_R_FIRE: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Dirty Fighting: stack cap, stack duration, second shot's AD ratio and
    /// the third-stack magic proc's flat value already resolved for level/AP.
    p_stack_max: i64,
    p_stack_duration: f64,
    p2_dmg: f64,
    p_proc_dmg: f64,
    /// Avengerang: damage per pass, its cooldown and assumed travel time.
    q_dmg: f64,
    q_cd: f64,
    q_travel: f64,
    /// Heroic Swing: damage per shot (attack-speed and crit factors already
    /// folded in), the minimum delay before the second shot, and cooldown.
    e_shot_dmg: f64,
    e_shot2_delay: f64,
    e_cd: f64,
    /// Comeuppance: damage per bullet (crit factor already folded in),
    /// bullet count, channel length and cooldown.
    r_bullet_dmg: f64,
    r_bullet_count: f64,
    r_channel_s: f64,
    r_cd: f64,
    src_p2: SourceId,
    src_p_proc: SourceId,
    src_e: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    p_stack_expire: f64,
    /// When the pending boomerang return lands (INF: none in flight).
    q_return_at: f64,
    /// When Heroic Swing is next ready, and when its pending second shot
    /// lands (INF: none pending).
    e_ready: f64,
    e_shot2_at: f64,
    /// When the pending Comeuppance channel ends (INF: not channeling).
    r_channel_end: f64,
    /// When the next channel may start (only used once not channeling).
    r_ready: f64,
}

impl GenDriver {
    /// A basic attack, an ability hit, or the second shot applies a stack of
    /// Dirty Fighting; the third consumes them all for the magic proc.
    fn apply_dirty_fighting(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t > self.s.p_stack_expire {
            self.s.p_stacks = 0;
        }
        self.s.p_stacks += 1;
        self.s.p_stack_expire = t + self.p_stack_duration;
        if self.s.p_stacks >= self.p_stack_max {
            self.s.p_stacks = 0;
            e.deal(self.p_proc_dmg, DType::Magic, self.src_p_proc, false, false, 1.0);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let r_bullet_base = kit.hit("gen.R.perBullet", ranks.r, sheet)?;
        let r_crit_mod = kit.num("gen.R.critDamageMod")?;
        let r_crit_factor = 1.0
            + (r_crit_mod * sheet.crit_chance / 100.0) * (sheet.crit_damage / 100.0 - 1.0);

        let e_base = kit.hit("gen.E.damage", ranks.e, sheet)?;
        let e_as_coef = kit.num("gen.E.attackSpeedCoefficient")?;
        let e_as_mult = 1.0 + e_as_coef * sheet.bonus_as_pct / 100.0;
        let e_crit_mod = kit.num("gen.E.critDamageMod")?;
        let e_crit_factor = 1.0
            + (e_crit_mod * sheet.crit_chance / 100.0) * (sheet.crit_damage / 100.0 - 1.0);

        let state = State {
            p_stacks: 0,
            p_stack_expire: 0.0,
            q_return_at: INF,
            e_ready: 0.0,
            e_shot2_at: INF,
            r_channel_end: INF,
            r_ready: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_stack_max: kit.num("gen.P.maxStacks")? as i64,
            p_stack_duration: kit.num("gen.P.stackDurationS")?,
            p2_dmg: kit.num("gen.P.secondShotAdRatio")? * sheet.ad,
            p_proc_dmg: kit.at_level("gen.P.magicProcBase", level)?
                + kit.num("gen.P.magicProcApRatio")? * sheet.ap,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_travel: kit.num("gen.Q.travelTimeS")?,
            e_shot_dmg: e_base * e_as_mult * e_crit_factor,
            e_shot2_delay: kit.num("gen.E.thirdCastDelayS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_bullet_dmg: r_bullet_base * r_crit_factor,
            r_bullet_count: kit.at_rank("gen.R.bulletCount", ranks.r)?,
            r_channel_s: kit.num("gen.R.channelDurationS")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            src_p2: intern("P second shot"),
            src_p_proc: intern("P proc"),
            src_e: intern("E"),
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

    fn after_attack(&mut self, e: &mut Engine) {
        // the primary attack's own stack of Dirty Fighting
        self.apply_dirty_fighting(e);
        // the delayed second shot, modeled as landing immediately: its own
        // independently-critting damage and its own stack
        e.deal(self.p2_dmg, DType::Physical, self.src_p2, true, false, 1.0);
        self.apply_dirty_fighting(e);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // the outgoing throw; the cooldown does not start until the return
        // pass lands, so hold q_ready at INF until then
        e.st.q_ready = INF;
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        self.apply_dirty_fighting(e);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
        self.s.q_return_at = e.st.t + self.q_travel;
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the opening cast: begin the channel; the volley fires on EV_R_FIRE
        self.s.r_channel_end = e.st.t + self.r_channel_s;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_return_at != INF {
            out[n] = (self.s.q_return_at, Kind::Ev(EV_Q_RETURN));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_shot2_at != INF {
                out[n] = (self.s.e_shot2_at, Kind::Ev(EV_E_SHOT2));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_channel_end != INF {
                out[n] = (self.s.r_channel_end, Kind::Ev(EV_R_FIRE));
            } else {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_RETURN) => {
                self.s.q_return_at = INF;
                e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
                self.apply_dirty_fighting(e);
                e.st.q_ready = t + e.basic_cd(self.q_cd);
            }
            Kind::Ev(EV_E_CAST) => {
                // first (start-of-swing) shot lands immediately; the
                // cooldown does not start until the second shot lands
                self.s.e_ready = INF;
                e.deal(self.e_shot_dmg, DType::Physical, self.src_e, false, true, 1.0);
                self.apply_dirty_fighting(e);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
                self.s.e_shot2_at = t + self.e_shot2_delay;
            }
            Kind::Ev(EV_E_SHOT2) => {
                // final (dismount) shot; the post-effect cooldown starts now
                self.s.e_shot2_at = INF;
                e.deal(self.e_shot_dmg, DType::Physical, self.src_e, false, true, 1.0);
                self.apply_dirty_fighting(e);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_channel_end = t + self.r_channel_s;
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_FIRE) => {
                self.s.r_channel_end = INF;
                let missing = pymax(e.target_hp - pymax(e.st.hp, 0.0), 0.0);
                let missing_pct = pymin(missing / e.target_hp, 1.0);
                let mult = 1.0 + 2.0 * missing_pct;
                let amt = self.r_bullet_dmg * self.r_bullet_count * mult;
                e.deal(amt, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                self.s.r_ready = t + e.ult_cd(self.r_cd);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
