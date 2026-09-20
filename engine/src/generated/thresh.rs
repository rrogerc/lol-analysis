//! Thresh. A mixed auto-attacker/ability caster: Damnation's assumed soul
//! count adds flat AP before any AP scaling is evaluated, Death Sentence is
//! cast on cooldown and recast immediately to cut short its own attack lock,
//! Flay's active goes out on cooldown while its passive empowers every
//! basic attack with a damage ramp that resets on landing, and The Box opens
//! the fight for its single burst of damage.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Flay's active cast (damage lands at cast start).
const EV_E_CAST: u8 = 0;
/// The Box's damage, landing when its cast completes.
const EV_R_HIT: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    q_hit_cdr: f64,
    q_recast_delay: f64,
    e_dmg: f64,
    e_cd: f64,
    e_per_soul_total: f64,
    e_ad_ratio: f64,
    e_full_charge: f64,
    r_dmg: f64,
    r_cast_s: f64,
    src_e_onhit: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    e_ready: f64,
    /// When The Box's damage lands (INF: none pending).
    r_hit_at: f64,
    /// The time of the last landed basic attack, driving Flay's ramp.
    last_attack_t: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let souls = kit.num("gen.P.assumedSouls")?;
        let ap_per_soul = kit.num("gen.P.apPerSoul")?;
        let mut sheet2 = sheet.clone();
        sheet2.ap += souls * ap_per_soul;

        let e_full_charge = kit.num("gen.E.fullChargeDurationS")?;
        let state = State {
            e_ready: 0.0,
            r_hit_at: INF,
            last_attack_t: -e_full_charge,
        };
        let _ = level;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("thresh kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, &sheet2)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_hit_cdr: kit.num("gen.Q.hitBonusCooldownS")?,
            q_recast_delay: kit.num("gen.Q.recastDelayS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, &sheet2)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_per_soul_total: kit.num("gen.E.passiveDmgPerSoul")? * souls,
            e_ad_ratio: kit.at_rank("gen.E.passiveAdRatio", ranks.e)?,
            e_full_charge,
            r_dmg: kit.hit("gen.R.damage", ranks.r, &sheet2)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_e_onhit: intern("E onhit"),
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
        // Flay's passive: every basic attack carries bonus magic damage
        // whose AD ratio has ramped since the last landed attack.
        let t = e.st.t;
        let elapsed = pymax(0.0, t - self.s.last_attack_t);
        let frac = pymin(1.0, elapsed / self.e_full_charge);
        let ratio = frac * self.e_ad_ratio;
        let bonus = self.e_per_soul_total + ratio * e.p.ad;
        e.deal(bonus, DType::Magic, self.src_e_onhit, false, false, 1.0);
        self.s.last_attack_t = t;
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
        // Landing always cuts the current cooldown by 2s (no spell shield on a dummy).
        e.st.q_ready = pymax(t, t + e.basic_cd(self.q_cd) - self.q_hit_cdr);
        // Recast as Deathly Leap as soon as possible to end the Shackled attack-lock early.
        e.st.next_attack = pymax(e.st.next_attack, t + self.q_recast_delay);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack past the cast; the walls' damage lands
        // when the cast completes
        self.s.r_hit_at = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_hit_at != INF {
            out[n] = (self.s.r_hit_at, Kind::Ev(EV_R_HIT));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_HIT) => {
                self.s.r_hit_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
