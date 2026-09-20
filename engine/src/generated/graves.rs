//! Graves. His basic attack (New Destiny) sprays 4 pellets (6 on crit) that
//! together replace the engine's own AD hit, blended into one expected-value
//! instance per attack; an ammo system of 2 shells gates when attacks can
//! happen at all; Quickdraw is woven in on cooldown purely for its reload +
//! attack-reset (it deals no damage) and its own cooldown melts per pellet
//! landed; Q is cast on cooldown for its initial hit and delayed detonation;
//! W is cast on cooldown; R opens the fight.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Q's powder trail detonation, 2 s after the initial hit.
const EV_Q_DET: u8 = 0;
/// W cast on cooldown.
const EV_W_CAST: u8 = 1;
/// Quickdraw cast on cooldown.
const EV_E_CAST: u8 = 2;
/// R's shell + cone explosion, after its 0.25 s cast.
const EV_R_SWING: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Passive: base pellet AD ratio at this level, the extra-pellet ratio,
    /// pellet counts (normal/crit) and the crit-bonus coefficient.
    p_ratio: f64,
    p_multi_ratio: f64,
    p_pellets_normal: f64,
    p_pellets_crit: f64,
    p_crit_coef: f64,
    /// Flat reload time for an empty (2-shell) clip.
    reload_dur: f64,
    q_init_dmg: f64,
    q_det_dmg: f64,
    q_det_delay: f64,
    q_cd: f64,
    q_cost: f64,
    w_dmg: f64,
    w_cd: f64,
    w_cost: f64,
    e_cd: f64,
    e_cost: f64,
    e_cdr_per_pellet: f64,
    r_dmg: f64,
    r_falloff: f64,
    r_cast_s: f64,
    r_cost: f64,
    src_p: SourceId,
    src_w: SourceId,
    src_q_det: SourceId,
    src_r_falloff: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Shells currently in the clip (0-2); reloading is tracked separately
    /// so Quickdraw can cancel an in-progress reload.
    shells: i64,
    reloading: bool,
    /// Q's pending detonation time (INF: none pending).
    q_det_at: f64,
    w_ready: f64,
    e_ready: f64,
    /// R's pending swing time (INF once used or never cast).
    r_swing_at: f64,
    mana: f64,
}

impl GenDriver {
    /// The expected-value pellet blend shared by the passive hit itself and
    /// Quickdraw's per-pellet cooldown reduction.
    fn crit_terms(&self, e: &Engine) -> (f64, f64) {
        let cc = e.p.sheet.crit_chance / 100.0;
        let cd = e.p.sheet.crit_damage / 100.0;
        let crit_mult = 1.0 + self.p_crit_coef * (cd - 1.0);
        let noncrit_ratio = 1.0 + (self.p_pellets_normal - 1.0) * self.p_multi_ratio;
        let crit_ratio = 1.0 + (self.p_pellets_crit - 1.0) * self.p_multi_ratio;
        let expected_ratio = (1.0 - cc) * noncrit_ratio + cc * crit_ratio * crit_mult;
        let expected_pellets = (1.0 - cc) * self.p_pellets_normal + cc * self.p_pellets_crit;
        (expected_ratio, expected_pellets)
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            shells: 2,
            reloading: false,
            q_det_at: INF,
            w_ready: 0.0,
            e_ready: 0.0,
            r_swing_at: INF,
            mana: sheet.mana,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("graves kit needs attack.windupFraction")?,
            p_ratio: kit.at_level("gen.P.pelletAdRatioByLevel", level)?,
            p_multi_ratio: kit.num("gen.P.multiPelletRatio")?,
            p_pellets_normal: kit.num("gen.P.pelletCountNormal")?,
            p_pellets_crit: kit.num("gen.P.pelletCountCrit")?,
            p_crit_coef: kit.num("gen.P.critDamageCoef")?,
            reload_dur: kit.num("gen.P.reload2ShellsS")?,
            q_init_dmg: kit.hit("gen.Q.initialDamage", ranks.q, sheet)?,
            q_det_dmg: kit.hit("gen.Q.detonationDamage", ranks.q, sheet)?,
            q_det_delay: kit.num("gen.Q.detonationDelayS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cost: kit.at_rank("gen.Q.costMana", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cost: kit.at_rank("gen.W.costMana", ranks.w)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cost: kit.at_rank("gen.E.costMana", ranks.e)?,
            e_cdr_per_pellet: kit.num("gen.E.cdrPerPelletS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_falloff: kit.hit("gen.R.falloffDamage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_cost: kit.at_rank("gen.R.costMana", ranks.r)?,
            src_p: intern("P"),
            src_w: intern("W"),
            src_q_det: intern("Q detonation"),
            src_r_falloff: intern("R falloff"),
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

    fn attack_damage(&self, _e: &Engine) -> f64 {
        // the engine's own AD hit is suppressed; before_attack deals the
        // full expected-value pellet spray instead
        0.0
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        // this attack is happening now, so any pending reload has finished
        self.s.reloading = false;
        self.s.shells -= 1;
        let (expected_ratio, expected_pellets) = self.crit_terms(e);
        let dmg = e.p.ad * self.p_ratio * expected_ratio;
        e.deal(dmg, DType::Physical, self.src_p, false, false, 1.0);
        if self.ranks.e > 0 && self.s.e_ready != INF && self.s.e_ready > e.st.t {
            let reduction = self.e_cdr_per_pellet * expected_pellets;
            self.s.e_ready = pymax(e.st.t, self.s.e_ready - reduction);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.shells <= 0 {
            // out of shells: start the reload, the clip is full again the
            // instant it finishes
            self.s.reloading = true;
            let done = t + self.reload_dur;
            self.s.shells = 2;
            e.st.next_attack = done;
        } else {
            let b = self.bonus_as(t);
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.mana < self.q_cost {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        self.s.mana -= self.q_cost;
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_init_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        self.s.q_det_at = e.st.t + self.q_det_delay;
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 || self.s.mana < self.r_cost {
            return;
        }
        self.s.mana -= self.r_cost;
        self.s.r_swing_at = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_det_at != INF {
            out[n] = (self.s.q_det_at, Kind::Ev(EV_Q_DET));
            n += 1;
        }
        if self.ranks.w > 0 && self.s.w_ready != INF {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 && self.s.e_ready != INF {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.r_swing_at != INF {
            out[n] = (self.s.r_swing_at, Kind::Ev(EV_R_SWING));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_DET) => {
                self.s.q_det_at = INF;
                e.deal(self.q_det_dmg, DType::Physical, self.src_q_det, false, true, 1.0);
            }
            Kind::Ev(EV_W_CAST) => {
                if self.s.mana >= self.w_cost {
                    self.s.mana -= self.w_cost;
                    e.deal(self.w_dmg, DType::Magic, self.src_w, false, true, 1.0);
                    e.ability_cast_proc();
                    e.eclipse_hit();
                    e.prime_spellblade();
                    self.s.w_ready = t + e.basic_cd(self.w_cd);
                    e.lockout();
                } else {
                    self.s.w_ready = INF;
                }
            }
            Kind::Ev(EV_E_CAST) => {
                if self.s.mana >= self.e_cost {
                    self.s.mana -= self.e_cost;
                    if self.s.reloading {
                        self.s.reloading = false;
                        self.s.shells = 1;
                    } else {
                        self.s.shells = imin(self.s.shells + 1, 2);
                    }
                    e.prime_spellblade();
                    let b = self.bonus_as(t);
                    e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
                    self.s.e_ready = t + e.basic_cd(self.e_cd);
                } else {
                    self.s.e_ready = INF;
                }
            }
            Kind::Ev(EV_R_SWING) => {
                self.s.r_swing_at = INF;
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.deal(self.r_falloff, DType::Physical, self.src_r_falloff, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
