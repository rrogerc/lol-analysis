//! Yunara. Transcend One's Self opens the fight, activating Cultivation of
//! Spirit for its full duration (covering the whole modeled fight),
//! upgrading Arc of Judgment into the free, ultimate-tagged Arc of Ruin and
//! Kanmei's Steps into Untouchable Shadow. Basic attacks carry Vow of the
//! First Lands' crit-amp and Cultivation of Spirit's on-hit magic damage;
//! Arc of Ruin is cast on cooldown under source R (the dossier states its
//! damage counts as ultimate damage, and it permanently replaces W once R is
//! learned); Untouchable Shadow is cast purely for its attack reset. Without
//! Transcend One's Self (e.g. level 1), Cultivation of Spirit instead builds
//! Unleash stacks from attacks and self-activates at the cap, and W remains
//! the un-upgraded Arc of Judgment under source W.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Arc of Ruin / Arc of Judgment lands on cast (after the usual lockout).
const EV_W_CAST: u8 = 0;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    windup_reduction_frac: f64,
    /// Vow of the First Lands: fraction of a crit's pre-mitigation damage
    /// dealt as bonus magic damage (10% + 0.1% per AP, fixed for the fight).
    p_amp_frac: f64,
    q_passive_dmg: f64,
    q_active_dmg: f64,
    q_active_as_pct: f64,
    q_active_dur: f64,
    q_stack_cap: i64,
    q_stack_per_attack: i64,
    w_cd: f64,
    w_init_dmg: f64,
    w_linger_dmg: f64,
    r_ruin_dmg: f64,
    r_dur: f64,
    e_cd: f64,
    src_p: SourceId,
    src_q_onhit: SourceId,
    src_q_active_onhit: SourceId,
    src_w_init: SourceId,
    src_w_linger: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Cultivation of Spirit's active window (until this time). Set once for
    /// the whole fight by Transcend One's Self, or cycled manually otherwise.
    q_active_until: f64,
    q_stacks: i64,
    /// Cultivation of Spirit's active just triggered: the next attack should
    /// take its reset (a shortened windup instead of a full period).
    q_reset_pending: bool,
    w_ready: f64,
    e_ready: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let ap = sheet.ap;
        let p_amp_base = kit.num("gen.P.ampBaseFrac")?;
        let p_amp_per_ap = kit.num("gen.P.ampPerApFrac")?;
        let state = State {
            q_active_until: -INF,
            q_stacks: 0,
            q_reset_pending: false,
            w_ready: 0.0,
            e_ready: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("yunara kit needs attack.windupFraction")?,
            windup_reduction_frac: kit.num("gen.Q.windupReductionFrac")?,
            p_amp_frac: p_amp_base + p_amp_per_ap * ap,
            q_passive_dmg: kit.hit("gen.Q.passiveDamage", ranks.q, sheet)?,
            q_active_dmg: kit.hit("gen.Q.activeDamage", ranks.q, sheet)?,
            q_active_as_pct: kit.at_rank("gen.Q.activeAsPct", ranks.q)? * 100.0,
            q_active_dur: kit.num("gen.Q.activeDurationS")?,
            q_stack_cap: kit.num("gen.Q.stackCap")? as i64,
            q_stack_per_attack: kit.num("gen.Q.stackPerAttackChampion")? as i64,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_init_dmg: kit.hit("gen.W.initialDamage", ranks.w, sheet)?,
            w_linger_dmg: kit.hit("gen.W.lingerTotalDamage", ranks.w, sheet)?,
            r_ruin_dmg: kit.hit("gen.R.ruinDamage", ranks.r, sheet)?,
            r_dur: kit.num("gen.R.durationS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            src_p: intern("P crit amp"),
            src_q_onhit: intern("Q onhit"),
            src_q_active_onhit: intern("Q active onhit"),
            src_w_init: intern("W initial"),
            src_w_linger: intern("W linger"),
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
        if t < self.s.q_active_until {
            self.q_active_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, _st: &mut St, t: f64, factor: f64) {
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.ranks.q > 0 {
            e.deal(self.q_passive_dmg, DType::Magic, self.src_q_onhit, false, false, 1.0);
            if t < self.s.q_active_until {
                e.deal(self.q_active_dmg, DType::Magic, self.src_q_active_onhit, false, false, 1.0);
            } else {
                self.s.q_stacks = imin(self.s.q_stacks + self.q_stack_per_attack, self.q_stack_cap);
                if self.s.q_stacks >= self.q_stack_cap {
                    self.s.q_stacks = 0;
                    self.s.q_active_until = t + self.q_active_dur;
                    self.s.q_reset_pending = true;
                    e.prime_spellblade();
                }
            }
        }
        // Vow of the First Lands: expected bonus magic damage from this
        // attack's critical strike (single application; see notes).
        let crit_chance = e.p.sheet.crit_chance / 100.0;
        let crit_dmg_mult = e.p.sheet.crit_damage / 100.0;
        let crit_total = e.p.ad * crit_dmg_mult;
        let bonus = crit_chance * self.p_amp_frac * crit_total;
        e.deal(bonus, DType::Magic, self.src_p, false, false, 1.0);
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        let e_want = self.ranks.e > 0 && t >= self.s.e_ready;
        if e_want || self.s.q_reset_pending {
            if e_want {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.prime_spellblade();
            }
            self.s.q_reset_pending = false;
            let active = t < self.s.q_active_until;
            let wf = if active {
                self.windup_fraction * (1.0 - self.windup_reduction_frac)
            } else {
                self.windup_fraction
            };
            e.st.next_attack = t + e.attack_windup(b, wf);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, _e: &Engine) -> f64 {
        INF
    }

    fn cast_q(&mut self, _e: &mut Engine) {}

    fn cast_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.q_active_until = t + self.r_dur;
        self.s.q_reset_pending = true;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        if self.ranks.w > 0 {
            out[0] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            1
        } else {
            0
        }
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        match kind {
            Kind::Ev(EV_W_CAST) => {
                let t = e.st.t;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                if self.ranks.r > 0 {
                    // Arc of Ruin: permanently replaces W once Transcend
                    // One's Self is learned; the dossier states its damage
                    // counts as ultimate damage, so it is dealt under R.
                    e.deal(self.r_ruin_dmg, DType::Magic, SRC_R, false, true, 1.0);
                } else {
                    e.deal(self.w_init_dmg, DType::Magic, self.src_w_init, false, true, 1.0);
                    e.deal(self.w_linger_dmg, DType::Magic, self.src_w_linger, false, true, 1.0);
                }
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
                if self.ranks.r > 0 {
                    e.ult_hatefog();
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
