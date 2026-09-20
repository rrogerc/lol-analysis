//! Kai'Sa. Plasma stacks (Caustic Wounds) ride basic attacks and Void
//! Seeker, detonating for missing-health damage at 5 stacks; Icathian Rain
//! fires on cooldown for a burst of missile damage; Void Seeker fires on
//! cooldown for its own bolt damage plus Plasma, refunding its cooldown on
//! hit; Supercharge is charged on cooldown for its attack-speed buff, its
//! charge fully blocking attacks; Killer Instinct is never cast (no damage,
//! no useful reset).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Icathian Rain's missiles land (a single aggregated instance).
const EV_Q_LAND: u8 = 0;
/// Void Seeker is cast; then its bolt resolves at the cast's end.
const EV_W_CAST: u8 = 1;
const EV_W_LAND: u8 = 2;
/// Supercharge is cast (begins its charge); then the charge completes.
const EV_E_CAST: u8 = 3;
const EV_E_END: u8 = 4;
/// A Plasma stack lapses after 4s without reapplication.
const EV_P_EXPIRE: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    // Passive: Second Skin / Plasma / Caustic Wounds.
    p_base_dmg: f64,
    p_perstack_dmg: f64,
    p_max_stacks: i64,
    p_duration: f64,
    p_execute_pct: f64,
    w_stacks_evolved: i64,
    // Q: Icathian Rain.
    q_dmg: f64,
    q_cd: f64,
    q_missile_delay: f64,
    // W: Void Seeker.
    w_dmg: f64,
    w_cd: f64,
    w_cast_time: f64,
    w_refund_frac: f64,
    // E: Supercharge.
    e_as_pct: f64,
    e_buff_dur: f64,
    e_cdr_per_attack: f64,
    e_cd: f64,
    e_cast_base: f64,
    e_cast_floor: f64,
    src_p_cw: SourceId,
    src_p_exec: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    /// When the current Plasma stack lapses (INF: none held).
    p_expire_at: f64,
    /// When Icathian Rain's pending missiles land (INF: none pending).
    q_land_at: f64,
    w_ready: f64,
    /// When a cast Void Seeker's bolt resolves (INF: not mid-cast).
    w_resolve_at: f64,
    e_ready: f64,
    /// When a charging Supercharge completes (INF: not charging).
    e_charge_end: f64,
    /// Until when Supercharge's bonus attack speed buff runs.
    e_buff_until: f64,
}

impl GenDriver {
    /// One Plasma application: deals Caustic Wounds damage for the stack
    /// count already held, then either adds a stack or (at 4 held) consumes
    /// them all. Returns whether this application detonated.
    fn cw_stack_apply(&mut self, e: &mut Engine) -> bool {
        let t = e.st.t;
        let stacks_before = self.s.p_stacks;
        let dmg = self.p_base_dmg + self.p_perstack_dmg * (stacks_before as f64);
        e.deal(dmg, DType::Magic, self.src_p_cw, false, false, 1.0);
        self.s.p_expire_at = t + self.p_duration;
        if stacks_before >= self.p_max_stacks {
            self.s.p_stacks = 0;
            true
        } else {
            self.s.p_stacks += 1;
            false
        }
    }

    /// The detonation's missing-health bonus, evaluated against the
    /// target's current health (read after whatever damage precedes it).
    fn p_execute_dmg(&self, e: &Engine) -> f64 {
        let missing = e.target_hp - pymax(e.st.hp, 0.0);
        self.p_execute_pct * missing
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            p_expire_at: INF,
            q_land_at: INF,
            w_ready: 0.0,
            w_resolve_at: INF,
            e_ready: 0.0,
            e_charge_end: INF,
            e_buff_until: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("kaisa kit needs attack.windupFraction")?,
            p_base_dmg: kit.at_level("gen.P.baseDamageByLevel", level)?
                + kit.num("gen.P.apRatioBase")? * sheet.ap,
            p_perstack_dmg: kit.at_level("gen.P.perStackDamageByLevel", level)?
                + kit.num("gen.P.apRatioPerStack")? * sheet.ap,
            p_max_stacks: kit.num("gen.P.maxStacks")? as i64,
            p_duration: kit.num("gen.P.durationS")?,
            p_execute_pct: kit.num("gen.P.executeRatio")? + kit.num("gen.P.executeApRatio")? * sheet.ap,
            w_stacks_evolved: kit.num("gen.P.voidSeekerStacksEvolved")? as i64,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_missile_delay: kit.num("gen.Q.missileDelayS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_time: kit.num("gen.W.castTimeS")?,
            w_refund_frac: 1.0 - kit.num("gen.W.cooldownRefundPct")? / 100.0,
            e_as_pct: kit.at_rank("gen.E.bonusAsPctByRank", ranks.e)?,
            e_buff_dur: kit.num("gen.E.buffDurationS")?,
            e_cdr_per_attack: kit.num("gen.E.cdrPerAttackS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_base: kit.num("gen.E.castTimeBaseS")?,
            e_cast_floor: kit.num("gen.E.castTimeFloorS")?,
            src_p_cw: intern("P Caustic Wounds"),
            src_p_exec: intern("P Detonation"),
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
        if t < self.s.e_buff_until { self.e_as_pct } else { 0.0 }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        // Supercharge's current cooldown is reduced 0.5s per attack while
        // it is cooling down (not while it is mid-charge).
        if self.ranks.e > 0 && self.s.e_charge_end == INF && self.s.e_ready > e.st.t {
            self.s.e_ready = pymax(e.st.t, self.s.e_ready - self.e_cdr_per_attack);
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Every basic attack applies 1 Plasma stack on-hit.
        let detonated = self.cw_stack_apply(e);
        if detonated {
            let bonus = self.p_execute_dmg(e);
            e.deal(bonus, DType::Magic, self.src_p_exec, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // Icathian Rain has no cast time; its missiles land 0.4s later.
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.q_land_at = t + self.q_missile_delay;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_land_at != INF {
            out[n] = (self.s.q_land_at, Kind::Ev(EV_Q_LAND));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_resolve_at != INF {
                out[n] = (self.s.w_resolve_at, Kind::Ev(EV_W_LAND));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_charge_end != INF {
                out[n] = (self.s.e_charge_end, Kind::Ev(EV_E_END));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.s.p_expire_at != INF {
            out[n] = (self.s.p_expire_at, Kind::Ev(EV_P_EXPIRE));
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
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_resolve_at = t + self.w_cast_time;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_W_LAND) => {
                self.s.w_resolve_at = INF;
                let mut detonated = false;
                for _ in 0..self.w_stacks_evolved {
                    if self.cw_stack_apply(e) {
                        detonated = true;
                    }
                }
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                if detonated {
                    let bonus = self.p_execute_dmg(e);
                    e.deal(bonus, DType::Magic, self.src_p_exec, false, false, 1.0);
                }
                e.ability_cast_proc();
                e.eclipse_hit();
                // Evolved Void Seeker refunds 75% of the remaining cooldown
                // on hitting an enemy champion (the dummy always counts).
                let remaining = self.s.w_ready - t;
                if remaining > 0.0 {
                    self.s.w_ready = t + remaining * self.w_refund_frac;
                }
            }
            Kind::Ev(EV_E_CAST) => {
                let total_bonus_as = e.p.sheet.bonus_as_pct + self.bonus_as(t);
                let cast_time = pymax(self.e_cast_base / (1.0 + total_bonus_as / 100.0), self.e_cast_floor);
                self.s.e_charge_end = t + cast_time;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                // attack commands are converted to movement for the whole charge
                e.st.next_attack = pymax(e.st.next_attack, self.s.e_charge_end);
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_END) => {
                self.s.e_charge_end = INF;
                self.s.e_buff_until = t + self.e_buff_dur;
            }
            Kind::Ev(EV_P_EXPIRE) => {
                self.s.p_stacks = 0;
                self.s.p_expire_at = INF;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
