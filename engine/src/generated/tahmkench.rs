//! Tahm Kench. Auto-attacks and casts Tongue Lash on cooldown while An
//! Acquired Taste rides both, stacking on the target; Abyssal Dive goes out
//! on cooldown for its delayed AoE nuke (refunded every cast since the
//! dummy counts as an enemy champion); once the target holds 3 stacks,
//! Devour swallows it and Regurgitate is recast the instant it is legal for
//! its target-max-health magic damage. Casts go one at a time: Tongue Lash
//! and Devour each keep Tahm Kench busy for their 0.25 s cast time, and
//! Abyssal Dive's channel/delay/lockout and Devour's swallow/lockout hold
//! attacks and other casts for as long as the dossier states.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Abyssal Dive is cast, then its delayed emergence deals damage.
const EV_W_CAST: u8 = 0;
const EV_W_DAMAGE: u8 = 1;
/// Devour swallows the target at 3 stacks, then Regurgitate deals damage.
const EV_R_DEVOUR: u8 = 2;
const EV_R_REGURG: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    /// An Acquired Taste's bonus magic damage: fixed for the whole fight.
    p_onhit_dmg: f64,

    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,

    w_dmg: f64,
    w_cd: f64,
    w_refund_pct: f64,
    w_channel_s: f64,
    w_post_delay_s: f64,
    w_post_lockout_s: f64,

    r_base_dmg: f64,
    /// Regurgitate's percent of the target's maximum health (base + AP term).
    r_hp_pct: f64,
    r_cd: f64,
    r_cast_s: f64,
    r_recast_delay_s: f64,
    r_post_lockout_s: f64,
    r_stacks_needed: i64,

    src_p_attack: SourceId,
    src_p_q: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    /// A cast with a cast time (Q, Devour) started: nothing else until this.
    busy_until: f64,
    w_ready: f64,
    /// When Abyssal Dive's delayed damage lands (INF: none pending).
    w_damage_at: f64,
    /// Until when attacks/Q are held for the dive's channel + lockout.
    w_busy_until: f64,
    r_ready: f64,
    /// When the pending Regurgitate may be recast (INF: none pending).
    r_regurg_at: f64,
    /// Until when attacks/Q are held for the swallow + post-cast lockout.
    r_busy_until: f64,
}

impl GenDriver {
    /// Every source of "busy" combined: a cast time in progress, Abyssal
    /// Dive's channel/lockout, and Devour's swallow/lockout.
    fn all_busy_until(&self) -> f64 {
        pymax(self.s.busy_until, pymax(self.s.w_busy_until, self.s.r_busy_until))
    }

    /// The earliest a cast readied at `ready` can start: not before now, and
    /// not inside another cast or busy window.
    fn castable_at(&self, e: &Engine, ready: f64) -> f64 {
        pymax(pymax(ready, e.st.t), self.all_busy_until())
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
        let bonus_hp = sheet.hp_bonus;
        let ap = sheet.ap;

        let p_base = kit.at_level("gen.P.baseByLevel", level)?;
        let p_bonus_hp_coef = kit.num("gen.P.bonusHpCoef")?;
        let p_ap_per100_coef = kit.num("gen.P.apRatioPer100BonusHp")?;
        let p_ap_per100_divisor = kit.num("gen.P.apRatioPer100BonusHpDivisor")?;
        let p_onhit_dmg = p_base
            + p_bonus_hp_coef * bonus_hp
            + p_ap_per100_coef * bonus_hp * ap / p_ap_per100_divisor;

        let r_hp_base_ratio = kit.num("gen.R.targetMaxHpBaseRatio")?;
        let r_hp_ap_coef = kit.num("gen.R.targetMaxHpRatioPerAp")?;
        let r_hp_pct = r_hp_base_ratio + r_hp_ap_coef * ap;

        let state = State {
            p_stacks: 0,
            busy_until: 0.0,
            w_ready: 0.0,
            w_damage_at: INF,
            w_busy_until: 0.0,
            r_ready: 0.0,
            r_regurg_at: INF,
            r_busy_until: 0.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit
                .windup_fraction
                .ok_or("tahmkench kit needs attack.windupFraction")?,

            p_onhit_dmg,

            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,

            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_refund_pct: kit.at_rank("gen.W.championRefundPct", ranks.w)?,
            w_channel_s: kit.num("gen.W.channelS")?,
            w_post_delay_s: kit.num("gen.W.postChannelDelayS")?,
            w_post_lockout_s: kit.num("gen.W.postBlinkLockoutS")?,

            r_base_dmg: kit.at_rank("gen.R.baseDamage", ranks.r)?,
            r_hp_pct,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_recast_delay_s: kit.num("gen.R.recastDelayS")?,
            r_post_lockout_s: kit.num("gen.R.postRegurgitateLockoutS")?,
            r_stacks_needed: kit.num("gen.R.stacksNeeded")? as i64,

            src_p_attack: intern("P onhit"),
            src_p_q: intern("P proc"),

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
    }

    fn before_attack(&mut self, _e: &mut Engine) {
        // An Acquired Taste stacks on-attack, capped at its max.
        self.s.p_stacks = imin(self.s.p_stacks + 1, self.r_stacks_needed);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        e.deal(self.p_onhit_dmg, DType::Magic, self.src_p_attack, false, false, 1.0);
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        let mut na = t + e.attack_period(b);
        na = pymax(na, self.all_busy_until());
        e.st.next_attack = na;
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
        // An Acquired Taste rides Tongue Lash as its own damage instance.
        e.deal(self.p_onhit_dmg, DType::Magic, self.src_p_q, false, false, 1.0);
        self.s.p_stacks = imin(self.s.p_stacks + 1, self.r_stacks_needed);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_damage_at != INF {
                out[n] = (self.s.w_damage_at, Kind::Ev(EV_W_DAMAGE));
            } else {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_regurg_at != INF {
                out[n] = (self.s.r_regurg_at, Kind::Ev(EV_R_REGURG));
                n += 1;
            } else if self.s.p_stacks >= self.r_stacks_needed {
                out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_DEVOUR));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                // no cast time: the channel and its lockout hold attacks/Q
                self.s.w_damage_at = t + self.w_channel_s + self.w_post_delay_s;
                self.s.w_busy_until =
                    t + self.w_channel_s + self.w_post_delay_s + self.w_post_lockout_s;
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_DAMAGE) => {
                self.s.w_damage_at = INF;
                // the dummy is always hit: the cooldown refund always applies
                let full_cd = e.basic_cd(self.w_cd);
                self.s.w_ready = t + full_cd * (1.0 - self.w_refund_pct);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_R_DEVOUR) => {
                self.s.p_stacks = 0;
                e.prime_spellblade();
                // Devour's own 0.25 s cast time, then the swallow and the
                // post-Regurgitate lockout hold everything further.
                self.busy_for(e, self.r_cast_s);
                self.s.r_regurg_at = t + self.r_recast_delay_s;
                self.s.r_busy_until = t + self.r_recast_delay_s + self.r_post_lockout_s;
            }
            Kind::Ev(EV_R_REGURG) => {
                self.s.r_regurg_at = INF;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                let amt = self.r_base_dmg + self.r_hp_pct * e.target_hp;
                e.deal(amt, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
