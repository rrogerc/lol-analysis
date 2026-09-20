//! Veigar. A pure caster: Baleful Strike (Q) and Dark Matter (W) go out on
//! cooldown, Primordial Burst (R) opens the fight and is recast whenever it
//! comes off cooldown, and Phenomenal Evil Power (P) grants +1 AP per ability
//! hit, feeding straight back into all three damage instances. Event Horizon
//! (E) deals no damage and is never cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Dark Matter is cast, then its damage lands after the impact delay.
const EV_W_CAST: u8 = 0;
const EV_W_IMPACT: u8 = 1;
/// Primordial Burst recast (the opening cast is handled by `cast_r`).
const EV_R_CAST: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// AP gained per stack of Phenomenal Evil, and stacks gained per hit.
    p_ap_per_stack: f64,
    p_hit_stacks: i64,
    /// Dark Matter's stack-gated cooldown reduction: stacks needed per step
    /// and the multiplicative factor (1 - increment) applied each step.
    w_stacks_per_cdr: i64,
    w_cdr_factor: f64,
    q_base: f64,
    q_ap_ratio: f64,
    q_cd_base: f64,
    w_base: f64,
    w_ap_ratio: f64,
    w_cd_base_s: f64,
    w_impact_delay_s: f64,
    r_base: f64,
    r_ap_ratio: f64,
    r_cd_base: f64,
    r_missing_coeff: f64,
    r_max_mult: f64,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    w_ready: f64,
    /// When a pending Dark Matter's damage lands (INF: none pending).
    w_impact_at: f64,
    /// The snapshotted damage of a pending Dark Matter cast.
    w_pending_dmg: f64,
    r_ready: f64,
}

impl GenDriver {
    fn current_ap(&self, e: &Engine) -> f64 {
        e.p.sheet.ap + (self.s.p_stacks as f64) * self.p_ap_per_stack
    }

    /// Dark Matter's cooldown before ability haste: base 8s reduced by a
    /// factor of 0.9 for every 50 stacks of Phenomenal Evil accumulated.
    fn w_base_cd(&self) -> f64 {
        let n = self.s.p_stacks / self.w_stacks_per_cdr;
        let mut mult = 1.0;
        let mut i: i64 = 0;
        while i < n {
            mult = mult * self.w_cdr_factor;
            i += 1;
        }
        self.w_cd_base_s * mult
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            w_ready: 0.0,
            w_impact_at: INF,
            w_pending_dmg: 0.0,
            r_ready: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_ap_per_stack: kit.num("gen.P.apPerStack")?,
            p_hit_stacks: kit.num("gen.P.abilityHitStacks")? as i64,
            w_stacks_per_cdr: kit.num("gen.P.stacksPerDarkMatterCDR")? as i64,
            w_cdr_factor: 1.0 - kit.num("gen.P.darkMatterCDRIncrement")?,
            q_base: kit.at_rank("gen.Q.damage.base", ranks.q)?,
            q_ap_ratio: kit.at_rank("gen.Q.damage.apRatio", ranks.q)?,
            q_cd_base: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_base: kit.at_rank("gen.W.damage.base", ranks.w)?,
            w_ap_ratio: kit.at_rank("gen.W.damage.apRatio", ranks.w)?,
            w_cd_base_s: kit.num("gen.W.baseCooldownS")?,
            w_impact_delay_s: kit.num("gen.W.impactDelayS")?,
            r_base: kit.at_rank("gen.R.damage.base", ranks.r)?,
            r_ap_ratio: kit.at_rank("gen.R.damage.apRatio", ranks.r)?,
            r_cd_base: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_missing_coeff: kit.num("gen.R.missingHpCoeff")?,
            r_max_mult: kit.num("gen.R.maxExecuteMult")?,
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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let ap = self.current_ap(e);
        let dmg = self.q_base + self.q_ap_ratio * ap;
        e.deal(dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd_base);
        self.s.p_stacks += self.p_hit_stacks;
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // delayed the first attack past the 0.25 s cast
        if self.ranks.r == 0 {
            return;
        }
        let ap = self.current_ap(e);
        let base = self.r_base + self.r_ap_ratio * ap;
        let missing_frac = (e.target_hp - pymax(e.st.hp, 0.0)) / e.target_hp;
        let mult = pymin(1.0 + missing_frac * self.r_missing_coeff, self.r_max_mult);
        let dmg = base * mult;
        e.deal(dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.ult_hatefog();
        self.s.p_stacks += self.p_hit_stacks;
        self.s.r_ready = e.st.t + e.ult_cd(self.r_cd_base);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
            if self.s.w_impact_at != INF {
                out[n] = (self.s.w_impact_at, Kind::Ev(EV_W_IMPACT));
                n += 1;
            }
        }
        if self.ranks.r > 0 {
            out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                let ap = self.current_ap(e);
                let dmg = self.w_base + self.w_ap_ratio * ap;
                self.s.w_pending_dmg = dmg;
                self.s.w_impact_at = t + self.w_impact_delay_s;
                self.s.w_ready = t + e.basic_cd(self.w_base_cd());
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_W_IMPACT) => {
                self.s.w_impact_at = INF;
                e.deal(self.s.w_pending_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.p_stacks += self.p_hit_stacks;
            }
            Kind::Ev(EV_R_CAST) => {
                let ap = self.current_ap(e);
                let base = self.r_base + self.r_ap_ratio * ap;
                let missing_frac = (e.target_hp - pymax(e.st.hp, 0.0)) / e.target_hp;
                let mult = pymin(1.0 + missing_frac * self.r_missing_coeff, self.r_max_mult);
                let dmg = base * mult;
                e.prime_spellblade();
                e.lockout();
                e.deal(dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.s.p_stacks += self.p_hit_stacks;
                self.s.r_ready = t + e.ult_cd(self.r_cd_base);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
