//! Kled. A mixed auto-attacker/ability caster who never dismounts against a
//! stationary target: Bear Trap on a Rope goes out on cooldown and its
//! tether pulls 1.75 s later, Violent Tendencies passively arms while off
//! cooldown and its 4th attack is empowered, Jousting is cast then recast at
//! its 0.5 s minimum delay every cycle, and Chaaaaaaaarge!!! is cast once at
//! the opening at its assumed fully-ramped damage.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Q's tether pulls this long after the first hit.
const EV_Q_POP: u8 = 0;
/// Jousting's first dash cast, and its recast 0.5 s later.
const EV_E_CAST: u8 = 1;
const EV_E_RECAST: u8 = 2;
/// Violent Tendencies' 4-attack window times out without being consumed.
const EV_W_TIMEOUT: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_first_dmg: f64,
    q_pull_dmg: f64,
    q_cd: f64,
    q_pop_time: f64,

    w_flat: f64,
    w_pct: f64,
    w_cd_base: f64,
    w_window_dur: f64,
    w_champ_refund: f64,
    w_as_pct: f64,
    w_need: i64,

    e_dmg: f64,
    e_cd_base: f64,
    e_recast_delay: f64,

    r_pct: f64,

    src_q_pull: SourceId,
    src_e_recast: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// When Q's pending tether pulls (INF: none pending).
    q_pop_at: f64,
    /// Violent Tendencies: ready time (buff available once past this),
    /// attacks consumed so far in the current window, and when that window
    /// times out (INF: no window pending).
    w_cd_ready: f64,
    w_stacks: i64,
    w_window_end: f64,
    /// Jousting: when it may next be cast, and its pending recast (INF:
    /// none pending).
    e_ready: f64,
    e_recast_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let bonus_ad = sheet.ad_bonus;
        let bonus_hp = sheet.hp_bonus;

        let w_pct_base = kit.at_rank("gen.W.pctBase", ranks.w)?;
        let w_pct_ad_per100 = kit.num("gen.W.pctPerBonusAD100")?;
        let w_pct_hp_per100 = kit.num("gen.W.pctPerBonusHP100")?;
        let w_pct = (w_pct_base + bonus_ad / 100.0 * w_pct_ad_per100
            + bonus_hp / 100.0 * w_pct_hp_per100) / 100.0;

        let r_pct_base = kit.at_rank("gen.R.pctBase", ranks.r)?;
        let r_ad_coef = kit.num("gen.R.adCoefPer100")?;
        let r_pct = r_pct_base + bonus_ad / 100.0 * r_ad_coef;

        let state = State {
            q_pop_at: INF,
            w_cd_ready: 0.0,
            w_stacks: 0,
            w_window_end: INF,
            e_ready: 0.0,
            e_recast_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("kled kit needs attack.windupFraction")?,

            q_first_dmg: kit.hit("gen.Q.firstDamage", ranks.q, sheet)?,
            q_pull_dmg: kit.hit("gen.Q.pullDamage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_pop_time: kit.num("gen.Q.tetherPopTimeS")?,

            w_flat: kit.at_rank("gen.W.flatBase", ranks.w)?,
            w_pct,
            w_cd_base: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_window_dur: kit.num("gen.W.windowDurationS")?,
            w_champ_refund: kit.num("gen.W.champCooldownRefundS")?,
            w_as_pct: kit.num("gen.W.attackSpeedPct")?,
            w_need: kit.num("gen.W.attacksToEmpower")? as i64,

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd_base: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_recast_delay: kit.num("gen.E.recastDelayS")?,

            r_pct,

            src_q_pull: intern("Q pull"),
            src_e_recast: intern("E recast"),

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
        if self.ranks.w == 0 {
            return 0.0;
        }
        if t < self.s.w_cd_ready {
            0.0
        } else {
            self.w_as_pct
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_cd_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        if self.ranks.w == 0 {
            return;
        }
        let t = e.st.t;
        if t < self.s.w_cd_ready {
            // on cooldown: every on-hit attack against the target (a
            // champion) refunds the champion rate
            self.s.w_cd_ready = pymax(t, self.s.w_cd_ready - self.w_champ_refund);
        } else {
            if self.s.w_stacks == 0 {
                self.s.w_window_end = t + self.w_window_dur;
            }
            self.s.w_stacks += 1;
            if self.s.w_stacks >= self.w_need {
                // the 4th attack: the extra damage instance lands before
                // this attack's own damage, does not trigger on-hit, and
                // the ability starts its cooldown now
                let dmg = self.w_flat + self.w_pct * e.target_hp;
                e.deal(dmg, DType::Physical, SRC_W, false, false, 1.0);
                self.s.w_stacks = 0;
                self.s.w_window_end = INF;
                self.s.w_cd_ready = t + e.basic_cd(self.w_cd_base);
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
        e.deal(self.q_first_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.q_pop_at = t + self.q_pop_time;
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let dmg = self.r_pct * e.target_hp;
        e.deal(dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_pop_at != INF {
            out[n] = (self.s.q_pop_at, Kind::Ev(EV_Q_POP));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_recast_at != INF {
                out[n] = (self.s.e_recast_at, Kind::Ev(EV_E_RECAST));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.ranks.w > 0 && self.s.w_window_end != INF {
            out[n] = (self.s.w_window_end, Kind::Ev(EV_W_TIMEOUT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_POP) => {
                self.s.q_pop_at = INF;
                e.deal(self.q_pull_dmg, DType::Physical, self.src_q_pull, false, true, 1.0);
            }
            Kind::Ev(EV_E_CAST) => {
                // the cooldown starts at this first dash, not the recast
                self.s.e_ready = t + e.basic_cd(self.e_cd_base);
                self.s.e_recast_at = t + self.e_recast_delay;
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_RECAST) => {
                self.s.e_recast_at = INF;
                e.deal(self.e_dmg, DType::Physical, self.src_e_recast, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_TIMEOUT) => {
                // the window expired without landing 4 attacks: go on
                // cooldown now anyway
                self.s.w_stacks = 0;
                self.s.w_window_end = INF;
                self.s.w_cd_ready = t + e.basic_cd(self.w_cd_base);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
