//! Lux. A caster who still auto-attacks: Illumination marks the dummy on every
//! ability hit and is consumed by the next basic attack (or by Final Spark) for
//! bonus magic damage. Light Binding is cast on cooldown, Lucent Singularity is
//! cast then immediately recast for a near-instant detonation, and Final Spark
//! opens the fight and is recast on cooldown thereafter.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Lucent Singularity: the initial cast (starts the cooldown), then its
/// detonation (deals damage) after the assumed travel delay.
const EV_E_CAST: u8 = 0;
const EV_E_DETONATE: u8 = 1;
/// Final Spark: a cast after the opening one (if the cooldown allows it
/// again inside the fight), then its damage after the 1 s cast completes.
const EV_R_CAST: u8 = 2;
const EV_R_DMG: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Illumination: level-based base damage and its AP ratio, and the
    /// mark's duration.
    p_base: f64,
    p_ap_ratio: f64,
    p_mark_dur: f64,
    q_dmg: f64,
    q_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    e_travel_s: f64,
    r_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    src_p_onhit: SourceId,
    src_p_r: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The Illumination mark's expiry (a live mark exists while `t` is
    /// before this); starts unset (in the past).
    mark_until: f64,
    e_ready: f64,
    /// When Lucent Singularity's pending detonation lands (INF: none).
    e_dmg_at: f64,
    r_ready: f64,
    /// When Final Spark's pending damage lands (INF: none).
    r_dmg_at: f64,
}

impl GenDriver {
    /// Illumination's bonus magic damage, read at the moment it procs.
    fn p_amount(&self, e: &Engine) -> f64 {
        self.p_base + self.p_ap_ratio * e.p.sheet.ap
    }

    /// Starts (or restarts) Final Spark's cast: holds the next attack for
    /// its full 1 s cast time and schedules its damage and next readiness.
    fn start_r_cast(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_dmg_at = t + self.r_cast_s;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        e.st.next_attack = pymax(e.st.next_attack, t + self.r_cast_s);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            mark_until: -INF,
            e_ready: 0.0,
            e_dmg_at: INF,
            r_ready: 0.0,
            r_dmg_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("lux kit needs attack.windupFraction")?,
            p_base: kit.at_level("gen.P.damageByLevel", level)?,
            p_ap_ratio: kit.num("gen.P.apRatio")?,
            p_mark_dur: kit.num("gen.P.markDurationS")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_travel_s: kit.num("gen.E.travelTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_p_onhit: intern("P onhit"),
            src_p_r: intern("P via R"),
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

    fn after_attack(&mut self, e: &mut Engine) {
        // Illumination: a live mark is consumed by this attack for bonus
        // on-hit magic damage; the attack does not itself apply a mark.
        if self.s.mark_until > e.st.t {
            let dmg = self.p_amount(e);
            e.deal(dmg, DType::Magic, self.src_p_onhit, false, false, 1.0);
            self.s.mark_until = -INF;
        }
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
        self.s.mark_until = e.st.t + self.p_mark_dur;
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade;
        // extend the attack hold from the default 0.25 s to the full 1 s
        // cast time and schedule the damage
        self.start_r_cast(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            if self.s.e_dmg_at != INF {
                out[n] = (self.s.e_dmg_at, Kind::Ev(EV_E_DETONATE));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_dmg_at != INF {
                out[n] = (self.s.r_dmg_at, Kind::Ev(EV_R_DMG));
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
            Kind::Ev(EV_E_CAST) => {
                // the initial cast: counts as an ability activation for
                // on-cast effects; the detonation (recast) will not
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_dmg_at = t + self.e_travel_s;
                e.ability_cast_proc();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_DETONATE) => {
                self.s.e_dmg_at = INF;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.s.mark_until = t + self.p_mark_dur;
                e.eclipse_hit();
            }
            Kind::Ev(EV_R_CAST) => {
                // a recast after the opening one: the engine's implicit
                // priming only covers the very first action, so trigger
                // the on-cast effects ourselves here
                self.start_r_cast(e);
                e.ability_cast_proc();
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_DMG) => {
                self.s.r_dmg_at = INF;
                let had_mark = self.s.mark_until > t;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                if had_mark {
                    let dmg = self.p_amount(e);
                    e.deal(dmg, DType::Magic, self.src_p_r, false, false, 1.0);
                }
                self.s.mark_until = t + self.p_mark_dur;
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
