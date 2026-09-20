//! Heimerdinger. UPGRADE!!! is toggled at t=0 and spent on the next
//! Hextech Micro-Rockets cast for Hextech Rocket Swarm; H-28G Evolution
//! Turrets are deployed on Q's own cooldown until 3 are up and then tick
//! rapid-fire damage forever (they never die to a stationary dummy) while
//! their shared beam charges purely from W and E landing hits and fires
//! once per active turret whenever it crosses 100%; W and E otherwise go
//! out on cooldown, and basic attacks fill the gaps.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// A recurring tick of rapid-fire damage from every active turret.
const EV_TURRET_TICK: u8 = 0;
/// Hextech Micro-Rockets / Hextech Rocket Swarm, on cooldown.
const EV_W_CAST: u8 = 1;
/// CH-2 Electron Storm Grenade, on cooldown.
const EV_E_CAST: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_cd: f64,
    w_cd: f64,
    e_cd: f64,
    q_rapid: f64,
    q_beam: f64,
    w_normal_total: f64,
    w_rockets: f64,
    e_dmg: f64,
    r_swarm_total: f64,
    r_swarm_total_rockets: f64,
    turret_max: i64,
    tick_interval: f64,
    charge_per_rocket: f64,
    charge_on_e: f64,
    src_q_rapid: SourceId,
    src_q_beam: SourceId,
    src_w_swarm: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    turret_count: i64,
    beam_charge: f64,
    /// When the next turret rapid-fire tick lands (INF: no turret yet).
    next_tick_at: f64,
    w_ready: f64,
    e_ready: f64,
    /// UPGRADE!!! is armed: the next W cast becomes Rocket Swarm.
    r_armed: bool,
}

impl GenDriver {
    /// Adds beam charge and fires the turret's beam, once per active
    /// turret, for every full 100% crossed.
    fn add_charge(&mut self, e: &mut Engine, pct: f64) {
        self.s.beam_charge += pct;
        while self.s.beam_charge >= 100.0 && self.s.turret_count > 0 {
            self.s.beam_charge -= 100.0;
            let dmg = self.q_beam * (self.s.turret_count as f64);
            e.deal(dmg, DType::Magic, self.src_q_beam, false, false, 1.0);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let w_initial = kit.hit("gen.W.initialDamage", ranks.w, sheet)?;
        let w_sub = kit.hit("gen.W.subsequentDamage", ranks.w, sheet)?;
        let w_rockets = kit.num("gen.W.rocketsPerCast")?;
        let w_normal_total = w_initial + w_sub * (w_rockets - 1.0);

        let r_swarm_initial = kit.hit("gen.R.swarmInitialDamage", ranks.r, sheet)?;
        let r_swarm_tier1 = kit.hit("gen.R.swarmTier1Damage", ranks.r, sheet)?;
        let r_swarm_tier2 = kit.hit("gen.R.swarmTier2Damage", ranks.r, sheet)?;
        let r_swarm_tier1_count = kit.num("gen.R.swarmTier1Count")?;
        let r_swarm_tier2_count = kit.num("gen.R.swarmTier2Count")?;
        let r_swarm_total = r_swarm_initial
            + r_swarm_tier1 * r_swarm_tier1_count
            + r_swarm_tier2 * r_swarm_tier2_count;
        let r_swarm_total_rockets = 1.0 + r_swarm_tier1_count + r_swarm_tier2_count;

        let state = State {
            turret_count: 0,
            beam_charge: 0.0,
            next_tick_at: INF,
            w_ready: 0.0,
            e_ready: 0.0,
            r_armed: false,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            q_rapid: kit.hit("gen.Q.rapidfireDamage", ranks.q, sheet)?,
            q_beam: kit.hit("gen.Q.beamDamage", ranks.q, sheet)?,
            w_normal_total,
            w_rockets,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            r_swarm_total,
            r_swarm_total_rockets,
            turret_max: kit.num("gen.Q.maxTurrets")? as i64,
            tick_interval: kit.num("gen.Q.assumedTickIntervalS")?,
            charge_per_rocket: kit.num("gen.W.beamChargePerRocketPct")?,
            charge_on_e: kit.num("gen.E.beamChargeOnHitPct")?,
            src_q_rapid: intern("Q rapidfire"),
            src_q_beam: intern("Q beam"),
            src_w_swarm: intern("W swarm"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        true
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self, _t: f64) -> f64 {
        0.0
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        e.p.ad
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {}

    fn after_attack(&mut self, _e: &mut Engine) {}

    fn schedule_attack(&mut self, e: &mut Engine) {
        let b = self.bonus_as(e.st.t);
        e.st.next_attack = e.st.t + e.attack_period(b);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.turret_count >= self.turret_max {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        if self.s.turret_count == 0 {
            self.s.next_tick_at = t + self.tick_interval;
        }
        self.s.turret_count = imin(self.s.turret_count + 1, self.turret_max);
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        let _ = e;
        if self.ranks.r > 0 {
            self.s.r_armed = true;
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.turret_count > 0 {
            out[n] = (self.s.next_tick_at, Kind::Ev(EV_TURRET_TICK));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
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
            Kind::Ev(EV_TURRET_TICK) => {
                let dmg = self.q_rapid * (self.s.turret_count as f64);
                e.deal(dmg, DType::Magic, self.src_q_rapid, false, false, 1.0);
                self.s.next_tick_at = t + self.tick_interval;
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                if self.s.r_armed {
                    self.s.r_armed = false;
                    e.deal(self.r_swarm_total, DType::Magic, self.src_w_swarm, false, true, 1.0);
                    self.add_charge(e, self.charge_per_rocket * self.r_swarm_total_rockets);
                } else {
                    e.deal(self.w_normal_total, DType::Magic, SRC_W, false, true, 1.0);
                    self.add_charge(e, self.charge_per_rocket * self.w_rockets);
                }
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.add_charge(e, self.charge_on_e);
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
