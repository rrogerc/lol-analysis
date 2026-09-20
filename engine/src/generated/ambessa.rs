//! Ambessa. Opens with Public Execution (R), whose 0.7s cast plus 0.75s
//! suppression are one continuous lockout ending in its burst and a
//! permanent armor-pen shred; Cunning Sweep is cast on cooldown and always
//! immediately chained into Sundering Slam; Repudiation and Lacerate (with
//! its free dash-triggered second spin) go out on cooldown; Medarda Maxim
//! stacks from every cast arm basic attacks with bonus on-hit damage and
//! attack speed.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Sundering Slam, chained right after Cunning Sweep.
const EV_SLAM: u8 = 0;
/// Public Execution's damage, after the cast time + suppression lockout.
const EV_R_DAMAGE: u8 = 1;
/// Repudiation: cast (brace begins), then the smash once the brace ends.
const EV_W_CAST: u8 = 2;
const EV_W_SMASH: u8 = 3;
/// Lacerate: cast (first spin), then the free second spin after the dash.
const EV_E_CAST: u8 = 4;
const EV_E_SPIN2: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    p_onhit_base: f64,
    p_onhit_adratio: f64,
    p_as_bonus_pct: f64,
    p_max: i64,
    p_stack_dur: f64,
    q1_flat: f64,
    q1_hp_pct_base: f64,
    q1_hp_pct_per_ad100: f64,
    q_cd: f64,
    slam_flat: f64,
    slam_hp_pct_base: f64,
    slam_hp_pct_per_ad100: f64,
    w_dmg: f64,
    w_cd: f64,
    w_brace: f64,
    e_dmg: f64,
    e_cd: f64,
    e_spin2_delay: f64,
    r_dmg: f64,
    r_cast_time: f64,
    r_suppress: f64,
    src_slam: SourceId,
    src_e2: SourceId,
    src_p_onhit: SourceId,
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    p_expire: f64,
    attack_empowered: bool,
    q_slam_at: f64,
    w_ready: f64,
    w_smash_at: f64,
    e_ready: f64,
    e_spin2_at: f64,
    /// While t < this, Ambessa cannot act (Public Execution's channel).
    locked_until: f64,
    r_damage_at: f64,
}

impl GenDriver {
    fn gain_stack(&mut self, t: f64) {
        if self.s.p_stacks < self.p_max {
            self.s.p_stacks += 1;
        }
        self.s.p_expire = t + self.p_stack_dur;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            p_expire: INF,
            attack_empowered: false,
            q_slam_at: INF,
            w_ready: 0.0,
            w_smash_at: INF,
            e_ready: 0.0,
            e_spin2_at: INF,
            locked_until: 0.0,
            r_damage_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("ambessa kit needs attack.windupFraction")?,
            p_onhit_base: kit.at_level("gen.P.onhitBaseByLevel", level)?,
            p_onhit_adratio: kit.num("gen.P.onhitBonusAdRatio")?,
            p_as_bonus_pct: kit.num("gen.P.onhitBonusAsPct")? * 100.0,
            p_max: kit.num("gen.P.maxStacks")? as i64,
            p_stack_dur: kit.num("gen.P.stackDurationS")?,
            q1_flat: kit.hit("gen.Q.cunningSweep.damage", ranks.q, sheet)?,
            q1_hp_pct_base: kit.at_rank("gen.Q.cunningSweep.targetHpPct", ranks.q)?,
            q1_hp_pct_per_ad100: kit.num("gen.Q.cunningSweep.targetHpPctPerBonusAd100")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            slam_flat: kit.hit("gen.Q.sunderingSlam.damage", ranks.q, sheet)?,
            slam_hp_pct_base: kit.at_rank("gen.Q.sunderingSlam.targetHpPct", ranks.q)?,
            slam_hp_pct_per_ad100: kit.num("gen.Q.sunderingSlam.targetHpPctPerBonusAd100")?,
            w_dmg: kit.hit("gen.W.damageLow", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_brace: kit.num("gen.W.braceDurationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_spin2_delay: kit.num("gen.E.secondSpinDelayS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_time: kit.num("gen.R.castTimeS")?,
            r_suppress: kit.num("gen.R.suppressDurationS")?,
            src_slam: intern("Q Sundering Slam"),
            src_e2: intern("E second spin"),
            src_p_onhit: intern("P onhit"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        false
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self, t: f64) -> f64 {
        let active = if t >= self.s.p_expire { 0 } else { self.s.p_stacks };
        if active > 0 {
            self.p_as_bonus_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t >= self.s.p_expire {
            self.s.p_stacks = 0;
            self.s.p_expire = INF;
        }
        if self.s.p_stacks > 0 {
            self.s.p_stacks -= 1;
            self.s.attack_empowered = true;
            if self.s.p_stacks == 0 {
                self.s.p_expire = INF;
            }
        } else {
            self.s.attack_empowered = false;
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // Medarda Maxim's bonus damage is tied to the attack itself landing,
        // not to every on-hit application, so it lives here rather than in
        // attack_riders (which an item could call more than once per swing).
        if self.s.attack_empowered {
            let dmg = self.p_onhit_base + self.p_onhit_adratio * e.p.sheet.ad_bonus;
            e.deal(dmg, DType::Physical, self.src_p_onhit, true, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(pymax(e.st.q_ready, e.st.t), self.s.locked_until)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        let ad_bonus = e.p.sheet.ad_bonus;
        let pct = self.q1_hp_pct_base + self.q1_hp_pct_per_ad100 * (ad_bonus / 100.0);
        let dmg = self.q1_flat + pct * e.target_hp;
        e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
        self.gain_stack(t);
        self.s.q_slam_at = t + ABILITY_LOCKOUT_S;
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // Public Execution: permanent armor-pen passive is switched on now,
        // and the cast+suppression channel blocks everything until the
        // damage lands.
        let t = e.st.t;
        e.st.shred_until = INF;
        self.s.locked_until = t + self.r_cast_time + self.r_suppress;
        e.lockout();
        e.st.next_attack = pymax(e.st.next_attack, self.s.locked_until);
        self.s.r_damage_at = self.s.locked_until;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_slam_at != INF {
            out[n] = (self.s.q_slam_at, Kind::Ev(EV_SLAM));
            n += 1;
        }
        if self.s.r_damage_at != INF {
            out[n] = (self.s.r_damage_at, Kind::Ev(EV_R_DAMAGE));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_smash_at != INF {
                out[n] = (self.s.w_smash_at, Kind::Ev(EV_W_SMASH));
            } else {
                out[n] = (
                    pymax(pymax(self.s.w_ready, e.st.t), self.s.locked_until),
                    Kind::Ev(EV_W_CAST),
                );
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_spin2_at != INF {
                out[n] = (self.s.e_spin2_at, Kind::Ev(EV_E_SPIN2));
            } else {
                out[n] = (
                    pymax(pymax(self.s.e_ready, e.st.t), self.s.locked_until),
                    Kind::Ev(EV_E_CAST),
                );
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_SLAM) => {
                self.s.q_slam_at = INF;
                let ad_bonus = e.p.sheet.ad_bonus;
                let pct = self.slam_hp_pct_base + self.slam_hp_pct_per_ad100 * (ad_bonus / 100.0);
                let dmg = self.slam_flat + pct * e.target_hp;
                e.deal(dmg, DType::Physical, self.src_slam, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
                self.gain_stack(t);
            }
            Kind::Ev(EV_R_DAMAGE) => {
                self.s.r_damage_at = INF;
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                self.gain_stack(t);
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_smash_at = t + self.w_brace;
                self.s.w_ready = INF;
                e.lockout();
                e.prime_spellblade();
                self.gain_stack(t);
            }
            Kind::Ev(EV_W_SMASH) => {
                self.s.w_smash_at = INF;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Physical, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = INF;
                self.s.e_spin2_at = t + self.e_spin2_delay;
                e.lockout();
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.gain_stack(t);
            }
            Kind::Ev(EV_E_SPIN2) => {
                self.s.e_spin2_at = INF;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Physical, self.src_e2, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
