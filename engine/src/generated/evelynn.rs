//! Evelynn. Damage comes almost entirely from abilities: Hate Spike opens
//! with its dart and then fires its 3 free recasts, each also carrying one
//! of the mark's 3 bonus-damage charges; Allure is cast purely to mature its
//! mark into a magic resist shred (it deals no direct damage against a
//! champion-type target, see `unused.W`); Whiplash opens empowered (Evelynn
//! is assumed to start the fight under Demon Shade) and thereafter on
//! cooldown as the plain version; Last Caress fires the moment it is off
//! cooldown.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Hate Spike's free recasts, spaced 0.5 s apart.
const EV_Q_RECAST: u8 = 0;
/// Allure: cast on cooldown, then its mark matures into the shred.
const EV_W_CAST: u8 = 1;
const EV_W_MATURE: u8 = 2;
/// Whiplash, cast on cooldown (empowered only the first time).
const EV_E_CAST: u8 = 3;
/// Last Caress: a later cast (if the cooldown allows one) and its delayed damage.
const EV_R_CAST: u8 = 4;
const EV_R_DMG: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dart_dmg: f64,
    q_bonus_dmg: f64,
    q_cd: f64,
    q_recast_count: i64,
    q_recast_interval: f64,
    w_cd: f64,
    w_mature_s: f64,
    w_shred_dur: f64,
    e_base_flat: f64,
    e_emp_flat: f64,
    /// Whiplash's health-ratio term, already resolved to a fraction using
    /// the build's static AP (base and empowered).
    e_base_pct: f64,
    e_emp_pct: f64,
    e_cd: f64,
    r_dmg: f64,
    r_crit_mult: f64,
    r_crit_threshold: f64,
    r_cast_s: f64,
    r_cd: f64,
    src_q_recast: SourceId,
    src_q_bonus: SourceId,
    src_e_emp: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    recast_remaining: i64,
    recast_next_at: f64,
    w_ready: f64,
    /// When Allure's held mark matures into the shred (INF: none pending).
    w_mature_at: f64,
    e_ready: f64,
    /// Whether the next Whiplash cast is the Demon-Shade-empowered one.
    e_empowered_available: bool,
    r_ready: f64,
    /// When a cast Last Caress's damage lands (INF: none pending).
    r_damage_at: f64,
}

impl GenDriver {
    /// Starts Last Caress's cast: its damage lands after the cast time.
    fn begin_r_cast(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_damage_at = t + self.r_cast_s;
        e.lockout();
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let ap = sheet.ap;
        let e_base_pct_flat = kit.num("gen.E.basePercentHealth")?;
        let e_base_ap_coef = kit.num("gen.E.baseApCoef")?;
        let e_emp_pct_flat = kit.num("gen.E.empoweredPercentHealth")?;
        let e_emp_ap_coef = kit.num("gen.E.empoweredApCoef")?;
        let e_base_pct = (e_base_pct_flat + e_base_ap_coef * ap) / 100.0;
        let e_emp_pct = (e_emp_pct_flat + e_emp_ap_coef * ap) / 100.0;

        let state = State {
            recast_remaining: 0,
            recast_next_at: INF,
            w_ready: 0.0,
            w_mature_at: INF,
            e_ready: 0.0,
            e_empowered_available: true,
            r_ready: 0.0,
            r_damage_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dart_dmg: kit.hit("gen.Q.dartDamage", ranks.q, sheet)?,
            q_bonus_dmg: kit.hit("gen.Q.bonusDamage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_recast_count: kit.num("gen.Q.recastCount")? as i64,
            q_recast_interval: kit.num("gen.Q.recastIntervalS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_mature_s: kit.num("gen.W.matureS")?,
            w_shred_dur: kit.num("abilities.Q.shred.durationS")?,
            e_base_flat: kit.hit("gen.E.baseDamage", ranks.e, sheet)?,
            e_emp_flat: kit.hit("gen.E.empoweredDamage", ranks.e, sheet)?,
            e_base_pct,
            e_emp_pct,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_crit_mult: kit.num("gen.R.critMultiplier")?,
            r_crit_threshold: kit.num("gen.R.critThreshold")?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            src_q_recast: intern("Q recast"),
            src_q_bonus: intern("Q bonus"),
            src_e_emp: intern("E empowered"),
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
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
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
        e.deal(self.q_dart_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
        // the 3 free recasts, spaced 0.5 s apart; each also carries one of
        // the mark's 3 bonus-damage charges
        self.s.recast_remaining = self.q_recast_count;
        self.s.recast_next_at = t + self.q_recast_interval;
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r > 0 {
            self.begin_r_cast(e);
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        let t = e.st.t;
        if self.s.recast_next_at != INF {
            out[n] = (self.s.recast_next_at, Kind::Ev(EV_Q_RECAST));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.s.w_mature_at != INF {
            out[n] = (self.s.w_mature_at, Kind::Ev(EV_W_MATURE));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.r_damage_at != INF {
            out[n] = (self.s.r_damage_at, Kind::Ev(EV_R_DMG));
            n += 1;
        } else if self.ranks.r > 0 {
            out[n] = (pymax(self.s.r_ready, t), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_RECAST) => {
                if self.s.recast_remaining > 0 {
                    e.deal(self.q_dart_dmg, DType::Magic, self.src_q_recast, false, true, 1.0);
                    e.deal(self.q_bonus_dmg, DType::Magic, self.src_q_bonus, false, true, 1.0);
                    e.ability_cast_proc();
                    e.eclipse_hit();
                    e.prime_spellblade();
                    self.s.recast_remaining -= 1;
                }
                if self.s.recast_remaining > 0 {
                    self.s.recast_next_at = t + self.q_recast_interval;
                } else {
                    self.s.recast_next_at = INF;
                }
            }
            Kind::Ev(EV_W_CAST) => {
                // no direct damage against a champion target: cast purely
                // to start the 2.5 s maturity timer for the shred
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.prime_spellblade();
                e.lockout();
                self.s.w_mature_at = t + self.w_mature_s;
            }
            Kind::Ev(EV_W_MATURE) => {
                e.st.shred_until = t + self.w_shred_dur;
                self.s.w_mature_at = INF;
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                let empowered = self.s.e_empowered_available;
                self.s.e_empowered_available = false;
                let (flat, pct, src) = if empowered {
                    (self.e_emp_flat, self.e_emp_pct, self.src_e_emp)
                } else {
                    (self.e_base_flat, self.e_base_pct, SRC_E)
                };
                let dmg = flat + pct * e.target_hp;
                e.deal(dmg, DType::Magic, src, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_R_CAST) => {
                self.begin_r_cast(e);
            }
            Kind::Ev(EV_R_DMG) => {
                self.s.r_damage_at = INF;
                let hp_ratio = pymax(e.st.hp, 0.0) / e.target_hp;
                let mut dmg = self.r_dmg;
                if hp_ratio < self.r_crit_threshold {
                    dmg = dmg * self.r_crit_mult;
                }
                e.deal(dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                self.s.r_ready = t + e.ult_cd(self.r_cd);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
