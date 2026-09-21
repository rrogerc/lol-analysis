//! Kayle. Attacks continuously to hold Zeal (permanently maxed at level 16,
//! stacked otherwise) for bonus attack speed and, at level 11+ while Exalted,
//! the Aflame fire wave on every swing. Starfire Spellblade's active is
//! rearmed the instant it is ready and rides the very next attack for its
//! reset and missing-health on-hit; Radiant Blast goes out on cooldown for
//! its damage and Sundered shred; Divine Judgment is self-cast at t=0.
//! Casts go one at a time: Q and R both have a cast time that keeps Kayle
//! busy (no other cast, no attack) for its length; W is never cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Radiant Blast's sword lands (fixed cast delay from the cast start).
const EV_Q_HIT: u8 = 0;
/// Divine Judgment's swords land (fixed delay from the cast start).
const EV_R_HIT: u8 = 1;
/// Zeal stacks lapse if unrefreshed for their duration.
const EV_ZEAL_EXPIRE: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range_v: f64,
    ranged_flag: bool,
    windup_fraction: f64,
    /// Whether Transcendent (permanent max Zeal / Exalted) is unlocked.
    transcendent: bool,
    /// Whether the Aflame fire wave is unlocked (level 11+).
    aflame_unlocked: bool,
    p_as_per_stack: f64,
    p_max_stacks: i64,
    p_stack_duration: f64,
    p_wave_dmg: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_delay: f64,
    q_cast_time: f64,
    q_shred_duration: f64,
    e_passive_dmg: f64,
    /// Fraction of the target's missing health dealt by E's active.
    e_active_pct: f64,
    e_cd: f64,
    r_dmg: f64,
    r_delay: f64,
    r_cast_time: f64,
    src_e_onhit: SourceId,
    src_e_active: SourceId,
    src_p_wave: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    zeal_stacks: i64,
    /// When the current Zeal stacks lapse if unrefreshed (INF: permanent/none).
    zeal_expire: f64,
    e_armed: bool,
    e_ready: f64,
    /// When Q's damage lands (INF: none pending).
    q_hit_at: f64,
    /// When R's damage lands (INF: none pending).
    r_hit_at: f64,
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
        let level_arisen = kit.num("gen.P.levelArisen")? as i64;
        let level_aflame = kit.num("gen.P.levelAflame")? as i64;
        let level_transcendent = kit.num("gen.P.levelTranscendent")? as i64;
        let range_arisen = kit.num("gen.P.rangeArisen")?;
        let range_transcendent = kit.num("gen.P.rangeTranscendent")?;

        let ranged_flag = level >= level_arisen;
        let attack_range_v = if level >= level_transcendent {
            range_transcendent
        } else if level >= level_arisen {
            range_arisen
        } else {
            sheet.base_attack_range
        };
        let transcendent = level >= level_transcendent;
        let aflame_unlocked = level >= level_aflame;

        let p_as_per_stack = kit.num("gen.P.asPerStack")?;
        let p_max_stacks = kit.num("gen.P.maxStacks")? as i64;
        let p_stack_duration = kit.num("gen.P.stackDurationS")?;
        let wave_base = kit.at_level("gen.P.fireWaveDamageByLevel", level)?;
        let wave_ap_ratio = kit.num("gen.P.fireWaveApRatio")?;
        let wave_bonusad_ratio = kit.num("gen.P.fireWaveBonusAdRatio")?;
        let p_wave_dmg = wave_base + wave_ap_ratio * sheet.ap + wave_bonusad_ratio * sheet.ad_bonus;

        let q_dmg = kit.hit("gen.Q.damage", ranks.q, sheet)?;
        let q_cd = kit.at_rank("abilities.Q.cooldownS", ranks.q)?;
        let q_cast_delay = kit.num("gen.Q.castDelayS")?;
        let q_cast_time = kit.num("gen.Q.castTimeS")?;
        let q_shred_duration = kit.num("abilities.Q.shred.durationS")?;

        let e_passive_dmg = kit.hit("gen.E.passiveDamage", ranks.e, sheet)?;
        let e_active_base = kit.at_rank("gen.E.activeExecutePercent", ranks.e)?;
        let e_active_ap_coef = kit.num("gen.E.activeApCoefPct")?;
        let e_active_pct = e_active_base / 100.0 + (e_active_ap_coef / 100.0) * (sheet.ap / 100.0);
        let e_cd = kit.at_rank("abilities.E.cooldownS", ranks.e)?;

        let r_dmg = kit.hit("gen.R.damage", ranks.r, sheet)?;
        let r_delay = kit.num("gen.R.invulnDurationS")?;
        let r_cast_time = kit.num("gen.R.castTimeS")?;

        let state = State {
            busy_until: 0.0,
            zeal_stacks: 0,
            zeal_expire: INF,
            e_armed: false,
            e_ready: 0.0,
            q_hit_at: INF,
            r_hit_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range_v,
            ranged_flag,
            windup_fraction: kit.windup_fraction.ok_or("kayle kit needs attack.windupFraction")?,
            transcendent,
            aflame_unlocked,
            p_as_per_stack,
            p_max_stacks,
            p_stack_duration,
            p_wave_dmg,
            q_dmg,
            q_cd,
            q_cast_delay,
            q_cast_time,
            q_shred_duration,
            e_passive_dmg,
            e_active_pct,
            e_cd,
            r_dmg,
            r_delay,
            r_cast_time,
            src_e_onhit: intern("E onhit"),
            src_e_active: intern("E active"),
            src_p_wave: intern("P wave"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        self.ranged_flag
    }

    fn attack_range(&self) -> f64 {
        self.attack_range_v
    }

    fn bonus_as(&self, _t: f64) -> f64 {
        if self.transcendent {
            self.p_max_stacks as f64 * self.p_as_per_stack
        } else {
            self.s.zeal_stacks as f64 * self.p_as_per_stack
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        // Zeal stacks (only tracked before Transcendent, which is permanent)
        if !self.transcendent {
            self.s.zeal_stacks = imin(self.s.zeal_stacks + 1, self.p_max_stacks);
            self.s.zeal_expire = e.st.t + self.p_stack_duration;
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // Starfire Spellblade's passive on-hit rides every attack
        if self.ranks.e > 0 {
            e.deal(self.e_passive_dmg, DType::Magic, self.src_e_onhit, false, false, 1.0);
        }
        // Starfire Spellblade's active, if armed, rides this attack too
        if self.s.e_armed {
            self.s.e_armed = false;
            self.s.e_ready = e.st.t + e.basic_cd(self.e_cd);
            let missing = pymax(e.target_hp - pymax(e.st.hp, 0.0), 0.0);
            let amt = self.e_active_pct * missing;
            e.deal(amt, DType::Magic, self.src_e_active, false, false, 1.0);
        }
        // Aflame fire wave while Exalted (permanently at 16, or at 5 stacks)
        if self.aflame_unlocked {
            let exalted = self.transcendent || self.s.zeal_stacks >= self.p_max_stacks;
            if exalted {
                e.deal(self.p_wave_dmg, DType::Magic, self.src_p_wave, true, false, 1.0);
            }
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.ranks.e > 0 && !self.s.e_armed && t >= self.s.e_ready {
            // arm Starfire Spellblade's active; it resets the attack timer
            self.s.e_armed = true;
            e.prime_spellblade();
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
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.q_hit_at = t + self.q_cast_delay;
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_time);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_hit_at = t + self.r_delay;
        e.prime_spellblade();
        self.busy_for(e, self.r_cast_time);
    }

    fn events(&self, _e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_hit_at != INF {
            out[n] = (self.s.q_hit_at, Kind::Ev(EV_Q_HIT));
            n += 1;
        }
        if self.s.r_hit_at != INF {
            out[n] = (self.s.r_hit_at, Kind::Ev(EV_R_HIT));
            n += 1;
        }
        if !self.transcendent && self.s.zeal_expire != INF {
            out[n] = (self.s.zeal_expire, Kind::Ev(EV_ZEAL_EXPIRE));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        match kind {
            Kind::Ev(EV_Q_HIT) => {
                self.s.q_hit_at = INF;
                e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.st.shred_until = e.st.t + self.q_shred_duration;
            }
            Kind::Ev(EV_R_HIT) => {
                self.s.r_hit_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_ZEAL_EXPIRE) => {
                self.s.zeal_stacks = 0;
                self.s.zeal_expire = INF;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
