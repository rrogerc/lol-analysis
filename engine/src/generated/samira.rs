//! Samira. A mixed auto-attacker: every blade attack (basic attacks in melee
//! range, Flair's slash, Blade Whirl's two slashes, Wild Rush's dash) carries
//! Daredevil Impulse's missing-health magic damage and generates a Style
//! stack; Flair is cast on cooldown, Blade Whirl is cast on cooldown despite
//! its attack lockout, Wild Rush is cast on cooldown for its damage and
//! attack speed window, and Inferno Trigger fires as soon as 6 Style stacks
//! and its cooldown both allow.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Blade Whirl is cast, or (if none pending) checked for readiness.
const EV_W_CAST: u8 = 0;
/// Blade Whirl's second slash, 0.75s after the first.
const EV_W_SLASH2: u8 = 1;
/// Wild Rush is cast on cooldown.
const EV_E_CAST: u8 = 2;
/// The next Inferno Trigger shot in a firing sequence.
const EV_R_SHOT: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Daredevil Impulse's blade bonus: flat base and AD ratio at this level.
    p_bonus_base: f64,
    p_bonus_ad_ratio: f64,
    style_duration: f64,
    style_max: i64,
    /// Flair's damage, with its halved expected crit bonus already baked in.
    q_dmg: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    w_spin_dur: f64,
    e_dmg: f64,
    e_cd: f64,
    e_as_pct: f64,
    e_as_dur: f64,
    r_dmg: f64,
    r_cd: f64,
    r_num_shots: i64,
    r_active_dur: f64,
    r_shot_interval: f64,
    src_p: SourceId,
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    /// While t < this, basic attacks and Flair are locked out by the spin.
    w_block_until: f64,
    /// The pending second slash's time, or INF if none pending.
    w_slash2_at: f64,
    e_ready: f64,
    /// Wild Rush's bonus attack speed lasts until this time.
    e_as_until: f64,
    r_ready: f64,
    /// While t < this, Inferno Trigger is firing: attacks and Flair are locked out.
    r_active_until: f64,
    r_shots_left: i64,
    r_next_shot_at: f64,
    style_stacks: i64,
    style_last_hit: f64,
}

impl GenDriver {
    fn style_now(&self, t: f64) -> i64 {
        if t - self.s.style_last_hit > self.style_duration {
            0
        } else {
            self.s.style_stacks
        }
    }

    /// A blade attack or damaging ability hit against the target: builds/refreshes Style.
    fn note_hit(&mut self, t: f64) {
        let cur = self.style_now(t);
        self.s.style_stacks = imin(cur + 1, self.style_max);
        self.s.style_last_hit = t;
    }

    /// Daredevil Impulse's bonus magic damage on a blade attack.
    fn blade_bonus(&self, e: &Engine) -> f64 {
        let ad = e.p.ad;
        let missing = pymax(
            pymin((e.target_hp - pymax(e.st.hp, 0.0)) / e.target_hp, 1.0),
            0.0,
        );
        (self.p_bonus_base + self.p_bonus_ad_ratio * ad) * (1.0 + missing)
    }

    /// Casts Inferno Trigger if 6 Style stacks and its cooldown both allow, and nothing else blocks it.
    fn maybe_cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        if self.s.r_active_until > t {
            return;
        }
        if t < self.s.w_block_until {
            return;
        }
        if t < self.s.r_ready {
            return;
        }
        if self.style_now(t) < self.style_max {
            return;
        }
        self.s.style_stacks = 0;
        self.s.style_last_hit = t;
        self.s.r_active_until = t + self.r_active_dur;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.s.r_shots_left = self.r_num_shots;
        self.s.r_next_shot_at = t;
        e.st.next_attack = pymax(e.st.next_attack, self.s.r_active_until);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
    }
}

impl Driver for GenDriver {
    fn new(
        kit: &Kit,
        sheet: &Sheet,
        level: i64,
        ranks: Ranks,
        _prestacked: bool,
    ) -> Result<Self, String> {
        let state = State {
            w_ready: 0.0,
            w_block_until: -INF,
            w_slash2_at: INF,
            e_ready: 0.0,
            e_as_until: -INF,
            r_ready: 0.0,
            r_active_until: -INF,
            r_shots_left: 0,
            r_next_shot_at: INF,
            style_stacks: 0,
            style_last_hit: -INF,
        };

        let cc = sheet.crit_chance / 100.0;
        let cd = sheet.crit_damage / 100.0;
        let q_crit_mod = kit.num("gen.Q.critDamageMod")?;
        let q_base = kit.hit("gen.Q.damage", ranks.q, sheet)?;
        let q_dmg = q_base * (1.0 + cc * q_crit_mod * (cd - 1.0));

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit
                .windup_fraction
                .ok_or("samira kit needs attack.windupFraction")?,
            p_bonus_base: kit.at_level("gen.P.bonusDamageBaseByLevel", level)?,
            p_bonus_ad_ratio: kit.at_level("gen.P.bonusDamageAdRatioByLevel", level)?,
            style_duration: kit.num("gen.P.styleDurationS")?,
            style_max: kit.num("gen.P.styleMaxStacks")? as i64,
            q_dmg,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_spin_dur: kit.num("gen.W.spinDurationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_as_pct: kit.at_rank("gen.E.bonusAsPct", ranks.e)?,
            e_as_dur: kit.num("gen.E.bonusAsDurationS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_num_shots: kit.num("gen.R.numShots")? as i64,
            r_active_dur: kit.num("gen.R.activeDurationS")?,
            r_shot_interval: kit.num("gen.R.shotIntervalS")?,
            src_p: intern("P blade bonus"),
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
        if t < self.s.e_as_until {
            self.e_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let bonus = self.blade_bonus(e);
        e.deal(bonus, DType::Magic, self.src_p, false, false, 1.0);
        self.note_hit(t);
        self.maybe_cast_r(e);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        let block = pymax(self.s.r_active_until, self.s.w_block_until);
        pymax(pymax(e.st.q_ready, block), e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        let bonus = self.blade_bonus(e);
        e.deal(bonus, DType::Magic, self.src_p, false, false, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.note_hit(t);
        e.lockout();
        self.maybe_cast_r(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_slash2_at != INF {
                out[n] = (self.s.w_slash2_at, Kind::Ev(EV_W_SLASH2));
            } else {
                let block = self.s.r_active_until;
                out[n] = (pymax(pymax(self.s.w_ready, block), e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.r_shots_left > 0 {
            out[n] = (self.s.r_next_shot_at, Kind::Ev(EV_R_SHOT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_block_until = t + self.w_spin_dur;
                self.s.w_slash2_at = t + self.w_spin_dur;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.st.next_attack = pymax(e.st.next_attack, self.s.w_block_until);
                e.deal(self.w_dmg, DType::Physical, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                let bonus = self.blade_bonus(e);
                e.deal(bonus, DType::Magic, self.src_p, false, false, 1.0);
                self.note_hit(t);
            }
            Kind::Ev(EV_W_SLASH2) => {
                self.s.w_slash2_at = INF;
                e.deal(self.w_dmg, DType::Physical, SRC_W, false, true, 1.0);
                let bonus = self.blade_bonus(e);
                e.deal(bonus, DType::Magic, self.src_p, false, false, 1.0);
                self.note_hit(t);
                self.maybe_cast_r(e);
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_as_until = t + self.e_as_dur;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                let bonus = self.blade_bonus(e);
                e.deal(bonus, DType::Magic, self.src_p, false, false, 1.0);
                self.note_hit(t);
                self.maybe_cast_r(e);
            }
            Kind::Ev(EV_R_SHOT) => {
                self.s.r_shots_left -= 1;
                e.deal(self.r_dmg, DType::Physical, SRC_R, true, true, 1.0);
                e.ult_hatefog();
                self.note_hit(t);
                if self.s.r_shots_left > 0 {
                    self.s.r_next_shot_at = t + self.r_shot_interval;
                } else {
                    self.s.r_next_shot_at = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
