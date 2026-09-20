//! Nocturne. Umbra Blades (P) periodically empowers his next attack with a
//! rider that lands alongside the engine's own attack damage, and its
//! cooldown drains per attack against the champion-tagged dummy. Duskbringer
//! (Q) is cast on cooldown and its trail grants a temporary bonus-AD steroid
//! to attacks (and, live, to P and R). Shroud of Darkness (W) contributes
//! only its always-on passive attack speed. Unspeakable Horror (E) is cast
//! on cooldown and ticks its DoT in full since the tether never breaks.
//! Paranoia (R) opens the fight, recasts almost immediately for its dash
//! damage, and repeats its cast/recast cycle off cooldown.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Unspeakable Horror: its cast (tether application), then its ticks.
const EV_E_CAST: u8 = 0;
const EV_E_TICK: u8 = 1;
/// Paranoia: the recast that deals damage, then the next initial cast.
const EV_R_RECAST: u8 = 2;
const EV_R_INITIATE: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Umbra Blades: base cooldown, per-attack CDR vs the champion-tagged
    /// dummy, and the two AD ratios (non-crit, crit).
    p_cd: f64,
    p_aa_cdr: f64,
    p_nocrit_ratio: f64,
    p_crit_ratio: f64,
    q_dmg: f64,
    q_cd: f64,
    q_bonus_ad: f64,
    q_buff_dur: f64,
    w_as_pct: f64,
    e_tick_dmg: f64,
    e_cd: f64,
    e_tick_interval: f64,
    e_tick_count: i64,
    r_base: f64,
    r_ratio: f64,
    r_cd: f64,
    r_recast_delay: f64,
    src_p: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_ready: f64,
    p_armed: bool,
    q_buff_until: f64,
    e_ready: f64,
    e_tick_next: f64,
    e_ticks_left: i64,
    r_ready: f64,
    r_recast_at: f64,
}

impl GenDriver {
    /// The current total bonus AD, including Duskbringer's trail steroid
    /// while it is running.
    fn bonus_ad_now(&self, e: &Engine, t: f64) -> f64 {
        e.p.sheet.ad_bonus + if t < self.s.q_buff_until { self.q_bonus_ad } else { 0.0 }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let _ = level;
        let state = State {
            p_ready: 0.0,
            p_armed: false,
            q_buff_until: 0.0,
            e_ready: 0.0,
            e_tick_next: INF,
            e_ticks_left: 0,
            r_ready: 0.0,
            r_recast_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_cd: kit.num("gen.P.cooldownS")?,
            p_aa_cdr: kit.num("gen.P.aaCdrChampionS")?,
            p_nocrit_ratio: kit.num("gen.P.noCritAdRatio")?,
            p_crit_ratio: kit.num("gen.P.critAdRatio")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_bonus_ad: kit.at_rank("gen.Q.bonusAd", ranks.q)?,
            q_buff_dur: kit.num("gen.Q.buffDurationS")?,
            w_as_pct: kit.at_rank("gen.W.passiveAsPct", ranks.w)? * 100.0,
            e_tick_dmg: kit.hit("gen.E.damage", ranks.e, sheet)? / kit.num("gen.E.tickCount")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_tick_interval: kit.num("gen.E.tickIntervalS")?,
            e_tick_count: kit.num("gen.E.tickCount")? as i64,
            r_base: kit.at_rank("gen.R.damage.base", ranks.r)?,
            r_ratio: kit.num("gen.R.damage.bonusAdRatio")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_recast_delay: kit.num("gen.R.recastDelayS")?,
            src_p: intern("P onhit"),
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

    fn bonus_as(&self, _t: f64) -> f64 {
        self.w_as_pct
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        if e.st.t < self.s.q_buff_until {
            e.p.ad + self.q_bonus_ad
        } else {
            e.p.ad
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t >= self.s.p_ready {
            self.s.p_armed = true;
        } else {
            self.s.p_armed = false;
            self.s.p_ready = pymax(t, self.s.p_ready - self.p_aa_cdr);
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.s.p_armed {
            self.s.p_armed = false;
            self.s.p_ready = e.st.t + self.p_cd;
            let ad = self.attack_damage(e);
            let pc = e.p.sheet.crit_chance / 100.0;
            let cd_mult = e.p.sheet.crit_damage / 100.0;
            let rider = ad
                * ((self.p_nocrit_ratio - 1.0) * (1.0 - pc)
                    + (self.p_crit_ratio - 1.0) * pc * cd_mult);
            e.deal(rider, DType::Physical, self.src_p, false, false, 1.0);
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
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.q_buff_until = e.st.t + self.q_buff_dur;
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_recast_at = e.st.t + self.r_recast_delay;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            if self.s.e_tick_next != INF {
                out[n] = (self.s.e_tick_next, Kind::Ev(EV_E_TICK));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_recast_at != INF {
                out[n] = (self.s.r_recast_at, Kind::Ev(EV_R_RECAST));
            } else {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_INITIATE));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_ticks_left = self.e_tick_count;
                self.s.e_tick_next = t + self.e_tick_interval;
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_TICK) => {
                e.deal(self.e_tick_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.s.e_ticks_left -= 1;
                if self.s.e_ticks_left > 0 {
                    self.s.e_tick_next = t + self.e_tick_interval;
                } else {
                    self.s.e_tick_next = INF;
                }
            }
            Kind::Ev(EV_R_RECAST) => {
                self.s.r_recast_at = INF;
                let bonus_ad = self.bonus_ad_now(e, t);
                let dmg = self.r_base + self.r_ratio * bonus_ad;
                e.deal(dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                self.s.r_ready = t + e.ult_cd(self.r_cd);
            }
            Kind::Ev(EV_R_INITIATE) => {
                self.s.r_ready = INF;
                self.s.r_recast_at = t + self.r_recast_delay;
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
