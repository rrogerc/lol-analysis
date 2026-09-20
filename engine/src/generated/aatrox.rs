//! Aatrox. World Ender is cast at t=0 for its bonus-AD window, Deathbringer
//! Stance's empowered attack (assumed pre-charged) rides basic attacks and
//! resets on consumption, The Darkin Blade is chained through all three
//! sweetspotted casts at its 1s static gap, Infernal Chains is cast on
//! cooldown for both its hits, and Umbral Dash is a pure attack-timer reset
//! used the instant it is available.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Infernal Chains: cast on cooldown, then its delayed pull hit.
const EV_W_CAST: u8 = 0;
const EV_W_PULL: u8 = 1;
/// Umbral Dash: cast the instant it is off cooldown, resetting the attack timer.
const EV_E_READY: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Deathbringer Stance: bonus damage as a fraction of the target's max
    /// health, the level-based full recharge time, and the two on-hit
    /// cooldown refunds.
    p_pct: f64,
    p_cd_base: f64,
    p_cd_reduce_hit: f64,
    p_cd_reduce_sweet: f64,
    /// The Darkin Blade: base and AD ratio (constant across the three
    /// casts), the per-cast ramp bonus and the sweetspot bonus, its
    /// cooldown, and the static gap between casts.
    q_base: f64,
    q_adratio: f64,
    q_ramp_bonus: f64,
    q_sweet_bonus: f64,
    q_cd: f64,
    q_static_gap: f64,
    /// Infernal Chains.
    w_base: f64,
    w_adratio: f64,
    w_cd: f64,
    w_pull_delay: f64,
    /// Umbral Dash.
    e_cd: f64,
    /// World Ender: bonus AD as a fraction of total AD, and its duration.
    r_ad_ratio: f64,
    r_active_s: f64,
    src_p: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// When Deathbringer Stance is next ready (0.0: pre-charged at t=0).
    p_ready: f64,
    /// Which Darkin Blade cast is next: 0, 1 or 2.
    q_stage: i64,
    w_ready: f64,
    /// When the pending Infernal Chains pull lands (INF: none pending).
    w_pull_at: f64,
    e_ready: f64,
    /// When World Ender's bonus AD ends (a time before any real t: inactive).
    r_until: f64,
    /// Whether the attack now landing was armed by Deathbringer Stance
    /// (decided at the start of its windup, before_attack).
    armed_this_attack: bool,
}

impl GenDriver {
    fn reduce_p(&mut self, t: f64, amount: f64) {
        if self.s.p_ready > t {
            self.s.p_ready = pymax(t, self.s.p_ready - amount);
        }
    }

    fn do_w_cast(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let ad = self.attack_damage(e);
        let dmg = self.w_base + self.w_adratio * ad;
        e.deal(dmg, DType::Physical, SRC_W, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.reduce_p(t, self.p_cd_reduce_hit);
        self.s.w_pull_at = t + self.w_pull_delay;
        self.s.w_ready = t + e.basic_cd(self.w_cd);
        e.lockout();
    }

    fn do_w_pull(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.w_pull_at = INF;
        let ad = self.attack_damage(e);
        let dmg = self.w_base + self.w_adratio * ad;
        e.deal(dmg, DType::Physical, SRC_W, false, true, 1.0);
        self.reduce_p(t, self.p_cd_reduce_hit);
    }

    fn do_e(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.e_ready = t + e.basic_cd(self.e_cd);
        e.prime_spellblade();
        let b = self.bonus_as(t);
        let reset_at = t + e.attack_windup(b, self.windup_fraction);
        e.st.next_attack = pymin(e.st.next_attack, reset_at);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_ready: 0.0,
            q_stage: 0,
            w_ready: 0.0,
            w_pull_at: INF,
            e_ready: 0.0,
            r_until: -1.0,
            armed_this_attack: false,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("aatrox kit needs attack.windupFraction")?,
            p_pct: kit.at_level("gen.P.pctMaxHpByLevel", level)?,
            p_cd_base: kit.at_level("gen.P.cooldownByLevel", level)?,
            p_cd_reduce_hit: kit.num("gen.P.cdReduceOnHit")?,
            p_cd_reduce_sweet: kit.num("gen.P.cdReduceSweetspot")?,
            q_base: kit.at_rank("gen.Q.baseByRank", ranks.q)?,
            q_adratio: kit.at_rank("gen.Q.adRatioByRank", ranks.q)?,
            q_ramp_bonus: kit.num("gen.Q.rampBonus")?,
            q_sweet_bonus: kit.num("gen.Q.sweetspotBonus")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_static_gap: kit.num("gen.Q.staticGapS")?,
            w_base: kit.at_rank("gen.W.baseByRank", ranks.w)?,
            w_adratio: kit.num("gen.W.adRatio")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_pull_delay: kit.num("gen.W.pullDelayS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_ad_ratio: kit.at_rank("gen.R.adRatioByRank", ranks.r)?,
            r_active_s: kit.num("gen.R.activeDurationS")?,
            src_p: intern("P empowered"),
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

    fn attack_damage(&self, e: &Engine) -> f64 {
        if self.ranks.r > 0 && e.st.t < self.s.r_until {
            e.p.ad + e.p.ad * self.r_ad_ratio
        } else {
            e.p.ad
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        // Deathbringer Stance: if it becomes ready mid-windup it does not
        // arm this attack, so the check happens at the start of the windup.
        self.s.armed_this_attack = self.s.p_ready <= e.st.t;
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.armed_this_attack {
            let bonus = self.p_pct * e.target_hp;
            e.deal(bonus, DType::Magic, self.src_p, false, false, 1.0);
            self.s.p_ready = t + self.p_cd_base;
        } else {
            self.reduce_p(t, self.p_cd_reduce_hit);
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
        let stage = self.s.q_stage;
        let ad = self.attack_damage(e);
        let ramp = 1.0 + self.q_ramp_bonus * (stage as f64);
        let sweet = 1.0 + self.q_sweet_bonus;
        let dmg = (self.q_base + self.q_adratio * ad) * ramp * sweet;
        e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.reduce_p(t, self.p_cd_reduce_sweet);
        if stage < 2 {
            self.s.q_stage = stage + 1;
            e.st.q_ready = t + self.q_static_gap;
        } else {
            self.s.q_stage = 0;
            e.st.q_ready = t + e.basic_cd(self.q_cd);
        }
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_until = e.st.t + self.r_active_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.s.w_pull_at != INF {
            out[n] = (self.s.w_pull_at, Kind::Ev(EV_W_PULL));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_READY));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        match kind {
            Kind::Ev(EV_W_CAST) => self.do_w_cast(e),
            Kind::Ev(EV_W_PULL) => self.do_w_pull(e),
            Kind::Ev(EV_E_READY) => self.do_e(e),
            other => panic!("unhandled event {other:?}"),
        }
    }
}
