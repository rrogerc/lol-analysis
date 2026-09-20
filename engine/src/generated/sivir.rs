//! Sivir. Opens with R (on-attack cooldown refund on Q/W) then W (attack
//! speed + bounce empowerment); attacks continuously, casting Q on cooldown
//! (assumed at max range for its double outgoing+return hit) and recasting W
//! whenever it comes off cooldown.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Boomerang Blade's damage (both the outgoing and returning hit) lands.
const EV_Q_LAND: u8 = 0;
/// Ricochet is (re)cast as soon as it is off cooldown.
const EV_W_CAST: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_cd: f64,
    q_dmg: f64,
    q_cast_time_s: f64,
    q_as_reduction_per: f64,
    q_as_reduction_cap: f64,
    q_travel_s: f64,

    w_cd: f64,
    w_as_pct: f64,
    w_buff_dur: f64,
    w_bounce_dmg: f64,

    r_dur: f64,
    r_cdr: f64,

    src_w_onhit: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// When Q's pending damage (outgoing + return) lands (INF: none pending).
    q_land_at: f64,
    w_ready: f64,
    /// Until when Ricochet's buff (attack speed + bounce) is active.
    w_active_until: f64,
    /// Until when On the Hunt's on-attack cooldown refund is active.
    r_active_until: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let base_q = kit.hit("gen.Q.damage", ranks.q, sheet)?;
        let crit_ratio = kit.num("gen.Q.critRatio")?;
        let crit_chance = sheet.crit_chance / 100.0;
        let crit_dmg = sheet.crit_damage / 100.0;
        let q_dmg = base_q * (1.0 + crit_ratio * crit_chance * (crit_dmg - 1.0));
        let r_dur = kit.at_rank("gen.R.durationS", ranks.r)?;

        let state = State {
            q_land_at: INF,
            w_ready: 0.0,
            w_active_until: -1.0,
            r_active_until: -1.0,
        };
        let _ = level;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("sivir kit needs attack.windupFraction")?,

            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_dmg,
            q_cast_time_s: kit.num("gen.Q.castTimeS")?,
            q_as_reduction_per: kit.num("gen.Q.asCastReductionPerBonusAS")?,
            q_as_reduction_cap: kit.num("gen.Q.asCastReductionCap")?,
            q_travel_s: kit.num("gen.Q.maxRangeUnits")? / kit.num("gen.Q.outgoingMissileSpeed")?,

            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_as_pct: kit.at_rank("gen.W.bonusAttackSpeed", ranks.w)? * 100.0,
            w_buff_dur: kit.num("gen.W.buffDurationS")?,
            w_bounce_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,

            r_dur,
            r_cdr: kit.num_or("gen.R.attackCdrS", 0.0),

            src_w_onhit: intern("W onhit"),

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
        if t < self.s.w_active_until {
            self.w_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {}

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.ranks.w > 0 && e.st.t < self.s.w_active_until {
            e.deal(self.w_bounce_dmg, DType::Physical, self.src_w_onhit, true, false, 1.0);
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.r_active_until > e.st.t {
            let t = e.st.t;
            e.st.q_ready = pymax(t, e.st.q_ready - self.r_cdr);
            self.s.w_ready = pymax(t, self.s.w_ready - self.r_cdr);
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
        let total_bonus_as = e.p.sheet.bonus_as_pct + self.bonus_as(t);
        let reduction = pymin(self.q_as_reduction_cap, self.q_as_reduction_per * (total_bonus_as / 100.0));
        let cast_time = self.q_cast_time_s * (1.0 - reduction);
        self.s.q_land_at = t + cast_time + self.q_travel_s;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_active_until = e.st.t + self.r_dur;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_land_at != INF {
            out[n] = (self.s.q_land_at, Kind::Ev(EV_Q_LAND));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_LAND) => {
                self.s.q_land_at = INF;
                e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
                e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_W_CAST) => {
                let already_active = t < self.s.w_active_until;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_active_until = t + self.w_buff_dur;
                e.prime_spellblade();
                if !already_active {
                    let b = self.bonus_as(t);
                    e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
