//! Shen. Twilight Assault (Q) is recast on cooldown to keep the next 3 basic
//! attacks empowered with magic on-hit damage that scales off the target's
//! maximum health (the enhanced, champion-hit tier is assumed to always
//! apply, granting 50% bonus attack speed alongside it); Shadow Dash (E)
//! goes out on cooldown for its flat + bonus-health physical nuke. Ki
//! Barrier, Spirit's Refuge and Stand United are defensive-only and never
//! cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Shadow Dash goes out on cooldown.
const EV_E_CAST: u8 = 0;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_cd: f64,
    /// The empowered attack's level-based flat portion (same at both tiers).
    q_flat: f64,
    /// Fraction of the target's maximum health added at the base tier.
    q_base_pct_frac: f64,
    /// Fraction of the target's maximum health added at the enhanced tier.
    q_enhanced_pct_frac: f64,
    q_num_attacks: i64,
    q_window_s: f64,
    q_as_pct: f64,
    e_cd: f64,
    e_dmg: f64,
    src_q_onhit: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_charges: i64,
    q_window_until: f64,
    /// Whether the current empowerment window is the enhanced (champion-hit) tier.
    q_enhanced: bool,
    q_as_until: f64,
    e_ready: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let ap = sheet.ap;
        let base_pct = kit.at_rank("gen.Q.basePercent", ranks.q)?;
        let base_ap_coef = kit.num("gen.Q.baseApCoef")?;
        let enh_pct = kit.at_rank("gen.Q.enhancedPercent", ranks.q)?;
        let enh_ap_coef = kit.num("gen.Q.enhancedApCoef")?;
        let q_flat = kit.at_level("gen.Q.flatByLevel", level)?;
        let state = State {
            q_charges: 0,
            q_window_until: -INF,
            q_enhanced: false,
            q_as_until: -INF,
            e_ready: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_flat,
            q_base_pct_frac: (base_pct + base_ap_coef * ap) / 100.0,
            q_enhanced_pct_frac: (enh_pct + enh_ap_coef * ap) / 100.0,
            q_num_attacks: kit.num("gen.Q.numEnhancedAttacks")? as i64,
            q_window_s: kit.num("gen.Q.attackBuffDurationS")?,
            q_as_pct: kit.num("gen.Q.steroidAsPct")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            src_q_onhit: intern("Q onhit"),
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
        if t < self.s.q_as_until {
            self.q_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.s.q_charges > 0 && e.st.t < self.s.q_window_until {
            self.s.q_charges -= 1;
            let pct = if self.s.q_enhanced {
                self.q_enhanced_pct_frac
            } else {
                self.q_base_pct_frac
            };
            let dmg = self.q_flat + pct * e.target_hp;
            e.deal(dmg, DType::Magic, self.src_q_onhit, false, false, 1.0);
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
        // recast overwrites: a fresh 3-attack window at the enhanced tier
        self.s.q_charges = self.q_num_attacks;
        self.s.q_window_until = t + self.q_window_s;
        self.s.q_enhanced = true;
        self.s.q_as_until = t + self.q_window_s;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        if self.ranks.e == 0 {
            return 0;
        }
        out[0] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
        1
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
