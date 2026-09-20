//! Mordekaiser. Realm of Death opens the fight for its true damage and the
//! target's armor/MR reduction; Obliterate and Death's Grasp are cast on
//! cooldown (Obliterate always isolated, the dummy being the only enemy);
//! basic attacks weave in throughout, carrying Darkness Rise's on-hit magic
//! damage and building its stacks toward the damaging aura. Indestructible
//! is never cast: it has no damage to contribute.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Realm of Death's true damage (and the armor/MR shred) landing.
const EV_R_DAMAGE: u8 = 0;
/// Death's Grasp cast, then its claw's delayed damage.
const EV_E_CAST: u8 = 1;
const EV_E_DAMAGE: u8 = 2;
/// Darkness Rise's aura tick, while active.
const EV_P_AURA: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    p_onhit_dmg: f64,
    p_stack_dur: f64,
    p_stack_cap: i64,
    p_aura_flat: f64,
    p_aura_hp_frac: f64,
    q_dmg: f64,
    q_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    e_delay: f64,
    r_cast_s: f64,
    r_true_frac: f64,
    r_shred_dur: f64,
    src_p_onhit: SourceId,
    src_p_aura: SourceId,
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    p_stack_deadline: f64,
    p_aura_active: bool,
    p_aura_next: f64,
    e_ready: f64,
    e_pending_at: f64,
    r_damage_at: f64,
}

impl GenDriver {
    /// Generates / refreshes a Darkness Rise stack from a basic attack,
    /// Obliterate or Death's Grasp landing on the target; arms the aura at
    /// 3 stacks.
    fn p_on_hit(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.p_stacks > 0 && t <= self.s.p_stack_deadline {
            self.s.p_stacks = imin(self.s.p_stacks + 1, self.p_stack_cap);
        } else {
            self.s.p_stacks = 1;
        }
        self.s.p_stack_deadline = t + self.p_stack_dur;
        if self.s.p_stacks >= self.p_stack_cap && !self.s.p_aura_active {
            self.s.p_aura_active = true;
            self.s.p_aura_next = t + 1.0;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            p_stack_deadline: 0.0,
            p_aura_active: false,
            p_aura_next: INF,
            e_ready: 0.0,
            e_pending_at: INF,
            r_damage_at: INF,
        };
        let q_pre = kit.hit("gen.Q.damage", ranks.q, sheet)?
            + kit.at_level("gen.Q.levelBonusByLevel", level)?;
        let q_iso = kit.at_rank("gen.Q.isolationScalar", ranks.q)?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("mordekaiser kit needs attack.windupFraction")?,
            p_onhit_dmg: kit.num("gen.P.onhit.apRatio")? * sheet.ap,
            p_stack_dur: kit.num("gen.P.stackDurationS")?,
            p_stack_cap: kit.num("gen.P.stackCap")? as i64,
            p_aura_flat: kit.num("gen.P.aura.baseFlat")? + kit.num("gen.P.aura.apRatio")? * sheet.ap,
            p_aura_hp_frac: kit.at_level("gen.P.aura.targetHpFracByLevel", level)?,
            q_dmg: q_pre * q_iso,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_delay: kit.num("gen.E.delayS")?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_true_frac: kit.num("gen.R.trueDamage.targetMaxHpFrac")?,
            r_shred_dur: kit.num("abilities.Q.shred.durationS")?,
            src_p_onhit: intern("P onhit"),
            src_p_aura: intern("P aura"),
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
    }

    fn before_attack(&mut self, e: &mut Engine) {
        self.p_on_hit(e);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        e.deal(self.p_onhit_dmg, DType::Magic, self.src_p_onhit, false, false, 1.0);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.p_on_hit(e);
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the engine has already primed Spellblade and applied the opening
        // lockout; the claw/true-damage lands when the cast completes
        self.s.r_damage_at = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_damage_at != INF {
            out[n] = (self.s.r_damage_at, Kind::Ev(EV_R_DAMAGE));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_pending_at != INF {
                out[n] = (self.s.e_pending_at, Kind::Ev(EV_E_DAMAGE));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.s.p_aura_active {
            out[n] = (self.s.p_aura_next, Kind::Ev(EV_P_AURA));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_DAMAGE) => {
                self.s.r_damage_at = INF;
                let dmg = self.r_true_frac * e.target_hp;
                e.deal(dmg, DType::True, SRC_R, false, false, 1.0);
                e.st.shred_until = t + self.r_shred_dur;
                e.ult_hatefog();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_pending_at = t + self.e_delay;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_DAMAGE) => {
                self.s.e_pending_at = INF;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.p_on_hit(e);
            }
            Kind::Ev(EV_P_AURA) => {
                if t > self.s.p_stack_deadline {
                    self.s.p_aura_active = false;
                    self.s.p_aura_next = INF;
                    self.s.p_stacks = 0;
                } else {
                    let dmg = self.p_aura_flat + self.p_aura_hp_frac * e.target_hp;
                    e.deal(dmg, DType::Magic, self.src_p_aura, false, true, 1.0);
                    self.s.p_aura_next = t + 1.0;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
