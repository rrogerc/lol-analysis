//! Lucian. An auto-attacker whose passive rides his attacks: Lightslinger is
//! armed by any ability cast and consumed by the next basic attack, firing a
//! second shot 0.25s later. Piercing Light and Ardent Blaze are flat casts
//! on cooldown, Relentless Pursuit is cast purely for its attack reset and
//! Lightslinger-arm (and gets a cooldown refund off Lightslinger hits), and
//! The Culling opens the fight as a single channeled burst whose bullet
//! count is fixed from the build's crit stats.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// The Lightslinger second shot lands; W and E come off cooldown; the
/// channel of The Culling ends and deals its damage.
const EV_LL_SHOT: u8 = 0;
const EV_W: u8 = 1;
const EV_E: u8 = 2;
const EV_R_END: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Lightslinger: bonus-AD ratio of the second shot at this level, the
    /// window it must be consumed in, and the delay before it fires.
    ll_ratio: f64,
    ll_window: f64,
    ll_delay: f64,
    q_dmg: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    e_cd: f64,
    /// Relentless Pursuit's cooldown refund per Lightslinger hit on the
    /// (always-champion) dummy.
    e_cdr_champ: f64,
    /// The Culling's total damage across every bullet, computed once from
    /// the build's crit chance and crit damage, and the channel's duration.
    r_dmg_total: f64,
    r_duration: f64,
    src_p: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    e_ready: f64,
    /// Lightslinger armed by an ability cast, and until when it can still
    /// be consumed by an attack.
    ll_armed: bool,
    ll_until: f64,
    /// When the pending second shot lands (INF: none pending).
    ll_pending_at: f64,
    /// While t is below this, Piercing Light and Ardent Blaze are locked
    /// out and no basic attack occurs (The Culling's channel).
    channel_end: f64,
    /// The Culling has been cast and its channel-end damage is still due.
    r_pending: bool,
}

impl GenDriver {
    fn arm_lightslinger(&mut self, t: f64) {
        self.s.ll_armed = true;
        self.s.ll_until = t + self.ll_window;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            w_ready: 0.0,
            e_ready: 0.0,
            ll_armed: false,
            ll_until: 0.0,
            ll_pending_at: INF,
            channel_end: 0.0,
            r_pending: false,
        };

        let r_base_shots = kit.num("gen.R.numShots")?;
        let r_crit_mod = kit.num("gen.R.critValueMod")?;
        let crit_chance_frac = sheet.crit_chance / 100.0;
        let crit_damage_mult = sheet.crit_damage / 100.0;
        let raw_shots = r_base_shots
            * (1.0 + r_crit_mod * crit_chance_frac * (crit_damage_mult - 1.0));
        let r_total_shots = (raw_shots as i64) as f64;
        let r_per_bullet = kit.hit("gen.R.damage", ranks.r, sheet)?;

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("lucian kit needs attack.windupFraction")?,
            ll_ratio: kit.at_level("gen.P.onhitAdRatioByLevel", level)?,
            ll_window: kit.num("gen.P.armWindowS")?,
            ll_delay: kit.num("gen.P.secondShotDelayS")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cdr_champ: kit.num("gen.E.cdrChampionS")?,
            r_dmg_total: r_total_shots * r_per_bullet,
            r_duration: kit.num("gen.R.durationS")?,
            src_p: intern("P second shot"),
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

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.ll_armed {
            self.s.ll_armed = false;
            if t <= self.s.ll_until {
                self.s.ll_pending_at = t + self.ll_delay;
            }
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(pymax(e.st.q_ready, e.st.t), self.s.channel_end)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.arm_lightslinger(e.st.t);
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.channel_end = t + self.r_duration;
        self.s.r_pending = true;
        e.st.next_attack = pymax(e.st.next_attack, self.s.channel_end);
        e.prime_spellblade();
        self.arm_lightslinger(t);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.ll_pending_at != INF {
            out[n] = (self.s.ll_pending_at, Kind::Ev(EV_LL_SHOT));
            n += 1;
        }
        if self.ranks.w > 0 {
            let ready = pymax(pymax(self.s.w_ready, e.st.t), self.s.channel_end);
            out[n] = (ready, Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E));
            n += 1;
        }
        if self.s.r_pending {
            out[n] = (self.s.channel_end, Kind::Ev(EV_R_END));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_LL_SHOT) => {
                self.s.ll_pending_at = INF;
                let dmg = self.ll_ratio * e.p.ad;
                e.deal(dmg, DType::Physical, self.src_p, true, false, 1.0);
                // Relentless Pursuit's cooldown ticks down on the dummy
                // (an enemy champion) each time this shot lands.
                self.s.e_ready = pymax(t, self.s.e_ready - self.e_cdr_champ);
            }
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.arm_lightslinger(t);
                e.lockout();
            }
            Kind::Ev(EV_E) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.prime_spellblade();
                self.arm_lightslinger(t);
                // Attack reset, but never lets an attack slip out during
                // The Culling's channel.
                let b = self.bonus_as(t);
                let reset_at = t + e.attack_windup(b, self.windup_fraction);
                e.st.next_attack = pymax(reset_at, self.s.channel_end);
            }
            Kind::Ev(EV_R_END) => {
                self.s.r_pending = false;
                e.deal(self.r_dmg_total, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
