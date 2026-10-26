//! Sett. A melee auto-attacker whose Pit Grit alternates Left and Right
//! Punches (Right Punch riding a fixed, fast windup and a bonus-damage
//! on-hit), Knuckle Down arms the next two attacks with target-max-health
//! scaling damage and resets the attack timer, Haymaker and Facebreaker are
//! cast on cooldown for their flat damage, and The Show Stopper opens the
//! fight for its single, large burst hit.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// The empower window lapsing before both attacks land.
const EV_Q_WINDOW: u8 = 0;
/// Haymaker: the cast starting, then its damage landing.
const EV_W_CAST: u8 = 1;
const EV_W_LAND: u8 = 2;
/// Facebreaker: the cast starting, then its damage landing.
const EV_E_CAST: u8 = 3;
const EV_E_LAND: u8 = 4;
/// The Show Stopper's slam, after its travel time.
const EV_R_LAND: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    p_right_dmg: f64,
    p_right_windup_frac: f64,
    src_p: SourceId,

    q_base_dmg: f64,
    q_target_hp_frac: f64,
    q_cd: f64,
    q_window_s: f64,
    q_num: i64,

    w_dmg: f64,
    w_cast_s: f64,
    w_cd: f64,

    e_dmg: f64,
    e_cast_s: f64,
    e_self_lockout_s: f64,
    e_cd: f64,

    r_dmg: f64,
    r_target_hp_frac: f64,
    r_travel_s: f64,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Whether the next NATURAL (non Q-forced) attack should be a Right Punch.
    next_is_right: bool,
    /// The current attack's type and whether it is a Q-empowered attack,
    /// set in before_attack and read through attack_riders/after_attack.
    cur_is_right: bool,
    cur_is_q: bool,
    /// Knuckle Down: empowered attacks left to consume, and the deadline
    /// after which the empowerment lapses (INF: none pending).
    q_stacks: i64,
    q_window_until: f64,
    w_ready: f64,
    /// When Haymaker's damage lands (INF: no cast pending).
    w_cast_at: f64,
    e_ready: f64,
    /// When Facebreaker's damage lands (INF: no cast pending).
    e_cast_at: f64,
    /// When The Show Stopper's slam lands (INF: none pending).
    r_cast_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_right_base = kit.at_level("gen.P.rightPunchBaseByLevel", level)?;
        let p_right_ad_ratio = kit.num("gen.P.rightPunchBonusAdRatio")?;
        let p_right_dmg = p_right_base + p_right_ad_ratio * sheet.ad_bonus;
        let p_right_windup_frac = kit.num("gen.P.rightPunchWindupFraction")?;

        let q_base_dmg = kit.at_rank("gen.Q.baseDamage", ranks.q)?;
        let q_target_hp_flat = kit.num("gen.Q.targetMaxHpFlat")?;
        let q_target_hp_per_ad = kit.at_rank("gen.Q.targetMaxHpPerAd", ranks.q)?;
        let q_target_hp_frac = q_target_hp_flat + q_target_hp_per_ad * sheet.ad;
        let q_cd = kit.at_rank("abilities.Q.cooldownS", ranks.q)?;
        let q_window_s = kit.num("gen.Q.empowerWindowS")?;
        let q_num = kit.num("gen.Q.numEmpowered")? as i64;

        let w_dmg = kit.at_rank("gen.W.baseDamage", ranks.w)?;
        let w_cast_s = kit.num("gen.W.castTimeS")?;
        let w_cd = kit.at_rank("abilities.W.cooldownS", ranks.w)?;

        let e_dmg = kit.hit("gen.E.damage", ranks.e, sheet)?;
        let e_cast_s = kit.num("gen.E.castTimeS")?;
        let e_self_lockout_s = kit.num("gen.E.selfLockoutS")?;
        let e_cd = kit.at_rank("abilities.E.cooldownS", ranks.e)?;

        let r_dmg = kit.hit("gen.R.damage", ranks.r, sheet)?;
        let r_target_hp_frac = kit.at_rank("gen.R.targetMaxHpRatio", ranks.r)?;
        let r_travel_s = kit.num("gen.R.travelDelayS")?;

        let state = State {
            next_is_right: false,
            cur_is_right: false,
            cur_is_q: false,
            q_stacks: 0,
            q_window_until: INF,
            w_ready: 0.0,
            w_cast_at: INF,
            e_ready: 0.0,
            e_cast_at: INF,
            r_cast_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("sett kit needs attack.windupFraction")?,
            p_right_dmg,
            p_right_windup_frac,
            src_p: intern("P onhit"),
            q_base_dmg,
            q_target_hp_frac,
            q_cd,
            q_window_s,
            q_num,
            w_dmg,
            w_cast_s,
            w_cd,
            e_dmg,
            e_cast_s,
            e_self_lockout_s,
            e_cd,
            r_dmg,
            r_target_hp_frac,
            r_travel_s,
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

    fn before_attack(&mut self, _e: &mut Engine) {
        if self.s.q_stacks > 0 {
            // Knuckle Down guarantees Left then Right: the first remaining
            // stack (2) is the Left Punch, the last (1) is the Right Punch.
            self.s.cur_is_right = self.s.q_stacks == 1;
            self.s.cur_is_q = true;
        } else {
            self.s.cur_is_right = self.s.next_is_right;
            self.s.cur_is_q = false;
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.s.cur_is_right {
            e.deal(self.p_right_dmg, DType::Physical, self.src_p, false, false, 1.0);
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        // the alternation continues from whichever punch this was
        self.s.next_is_right = !self.s.cur_is_right;
        if self.s.cur_is_q {
            let dmg = self.q_base_dmg + self.q_target_hp_frac * e.target_hp;
            e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            self.s.q_stacks -= 1;
            if self.s.q_stacks <= 0 {
                self.s.q_stacks = 0;
                self.s.q_window_until = INF;
                e.st.q_ready = t + e.basic_cd(self.q_cd);
            }
            self.s.cur_is_q = false;
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.s.cur_is_right {
            // a Right Punch just landed: the next (Left) attack resumes the
            // normal attack cadence
            e.st.next_attack = t + e.attack_period(b);
        } else {
            // a Left Punch just landed: the Right Punch follows on its own
            // fixed, fast windup
            e.st.next_attack = t + self.p_right_windup_frac * e.attack_windup(b, self.windup_fraction);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.q_stacks > 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.q_stacks = self.q_num;
        self.s.q_window_until = t + self.q_window_s;
        e.prime_spellblade();
        // Knuckle Down resets Sett's basic attack timer
        e.st.next_attack = pymin(e.st.next_attack, t);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_cast_at = t + self.r_travel_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_window_until != INF {
            out[n] = (self.s.q_window_until, Kind::Ev(EV_Q_WINDOW));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_cast_at != INF {
                out[n] = (self.s.w_cast_at, Kind::Ev(EV_W_LAND));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_cast_at != INF {
                out[n] = (self.s.e_cast_at, Kind::Ev(EV_E_LAND));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.s.r_cast_at != INF {
            out[n] = (self.s.r_cast_at, Kind::Ev(EV_R_LAND));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_WINDOW) => {
                // the empower window lapsed before both attacks were used
                self.s.q_stacks = 0;
                self.s.q_window_until = INF;
                e.st.q_ready = t + e.basic_cd(self.q_cd);
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_cast_at = t + self.w_cast_s;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_W_LAND) => {
                self.s.w_cast_at = INF;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                // Grit is always 0 in this one-sided fight: flat base damage only
                e.deal(self.w_dmg, DType::True, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_cast_at = t + self.e_cast_s;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_LAND) => {
                self.s.e_cast_at = INF;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                // Facebreaker's self-lockout after its cast time
                e.st.next_attack = pymax(e.st.next_attack, t + self.e_self_lockout_s);
            }
            Kind::Ev(EV_R_LAND) => {
                self.s.r_cast_at = INF;
                let dmg = self.r_dmg + self.r_target_hp_frac * e.target_hp;
                e.deal(dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
