//! Twitch. Ambush is resolved before the fight starts (its bonus attack
//! speed window runs from t=0), Venom Cask goes out on cooldown seeding and
//! refreshing Deadly Venom (which ticks true damage once a second while
//! kept alive by attacks), Contaminate is cast on cooldown for its
//! per-stack physical/magic damage, and Spray and Pray is kept up for its
//! bonus AD while its bolts behave like ordinary attacks against one target.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Deadly Venom's once-a-second true-damage tick.
const EV_P_TICK: u8 = 0;
/// Venom Cask cast on cooldown, and its 1 s zone ticks after landing.
const EV_W_CAST: u8 = 1;
const EV_W_ZONE: u8 = 2;
/// Contaminate cast on cooldown.
const EV_E_CAST: u8 = 3;
/// Spray and Pray recast on cooldown (its opener is `cast_r`).
const EV_R_CAST: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    p_dmg_per_stack: f64,
    p_max_stacks: i64,
    p_duration: f64,
    p_tick_interval: f64,

    q_as_pct: f64,
    q_as_duration: f64,

    w_cd: f64,
    w_zone_ticks: i64,
    w_zone_tick_interval: f64,

    e_cd: f64,
    e_base: f64,
    e_per_stack_phys: f64,
    e_per_stack_magic: f64,

    r_cd: f64,
    r_bonus_ad: f64,
    r_duration: f64,

    src_p: SourceId,
    src_e_phys: SourceId,
    src_e_magic: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    p_until: f64,
    p_next_tick: f64,

    w_ready: f64,
    w_zone_ticks_left: i64,
    w_zone_next: f64,

    e_ready: f64,

    r_active_until: f64,
    r_next_ready: f64,
}

impl GenDriver {
    /// Deadly Venom is applied/refreshed by an attack landing, or by
    /// Venom Cask landing or ticking its zone.
    fn apply_venom(&mut self, t: f64) {
        self.s.p_stacks = imin(self.s.p_stacks + 1, self.p_max_stacks);
        self.s.p_until = t + self.p_duration;
        if self.s.p_next_tick == INF {
            self.s.p_next_tick = t + self.p_tick_interval;
        }
    }

    fn do_cast_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_active_until = t + self.r_duration;
        self.s.r_next_ready = t + e.ult_cd(self.r_cd);
        e.prime_spellblade();
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_stacks: 0,
            p_until: 0.0,
            p_next_tick: INF,
            w_ready: 0.0,
            w_zone_ticks_left: 0,
            w_zone_next: INF,
            e_ready: 0.0,
            r_active_until: 0.0,
            r_next_ready: INF,
        };

        let e_per_stack_magic = if ranks.e > 0 {
            kit.num("gen.E.perStackApRatio")? * sheet.ap
        } else {
            0.0
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("twitch kit needs attack.windupFraction")?,

            p_dmg_per_stack: kit.at_level("gen.P.perStackPerSecond.baseByLevel", level)?
                + kit.num("gen.P.perStackPerSecond.apRatio")? * sheet.ap,
            p_max_stacks: kit.num("gen.P.maxStacks")? as i64,
            p_duration: kit.num("gen.P.durationS")?,
            p_tick_interval: kit.num("gen.P.tickIntervalS")?,

            q_as_pct: kit.at_rank("gen.Q.asByRank", ranks.q)? * 100.0,
            q_as_duration: kit.num("gen.Q.asDurationS")?,

            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_zone_ticks: kit.num("gen.W.maxStacksPerCast")? as i64 - 1,
            w_zone_tick_interval: kit.num("gen.W.zoneTickIntervalS")?,

            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_base: kit.hit("gen.E.baseDamage", ranks.e, sheet)?,
            e_per_stack_phys: kit.hit("gen.E.perStackPhysical", ranks.e, sheet)?,
            e_per_stack_magic,

            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_bonus_ad: kit.at_rank("gen.R.bonusAdByRank", ranks.r)?,
            r_duration: kit.num("gen.R.durationS")?,

            src_p: intern("P"),
            src_e_phys: intern("E physical"),
            src_e_magic: intern("E magic"),

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

    fn bonus_as(&self, t: f64) -> f64 {
        if t < self.q_as_duration {
            self.q_as_pct
        } else {
            0.0
        }
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        if e.st.t < self.s.r_active_until {
            e.p.ad + self.r_bonus_ad
        } else {
            e.p.ad
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        self.apply_venom(e.st.t);
    }

    fn q_at(&self, _e: &Engine) -> f64 {
        // Ambush is resolved before the fight starts and never recast.
        INF
    }

    fn cast_q(&mut self, _e: &mut Engine) {}

    fn cast_r(&mut self, e: &mut Engine) {
        self.do_cast_r(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.p_next_tick != INF {
            out[n] = (self.s.p_next_tick, Kind::Ev(EV_P_TICK));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.s.w_zone_next != INF {
            out[n] = (self.s.w_zone_next, Kind::Ev(EV_W_ZONE));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 && self.s.r_next_ready != INF {
            out[n] = (pymax(self.s.r_next_ready, e.st.t), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_P_TICK) => {
                if self.s.p_stacks > 0 {
                    let dmg = self.s.p_stacks as f64 * self.p_dmg_per_stack;
                    e.deal(dmg, DType::True, self.src_p, false, false, 1.0);
                }
                if t + self.p_tick_interval <= self.s.p_until {
                    self.s.p_next_tick = t + self.p_tick_interval;
                } else {
                    self.s.p_stacks = 0;
                    self.s.p_next_tick = INF;
                }
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.apply_venom(t);
                self.s.w_zone_ticks_left = self.w_zone_ticks;
                self.s.w_zone_next = t + self.w_zone_tick_interval;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_W_ZONE) => {
                self.apply_venom(t);
                self.s.w_zone_ticks_left -= 1;
                if self.s.w_zone_ticks_left > 0 {
                    self.s.w_zone_next = t + self.w_zone_tick_interval;
                } else {
                    self.s.w_zone_next = INF;
                }
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                let stacks = self.s.p_stacks as f64;
                let phys = self.e_base + stacks * self.e_per_stack_phys;
                e.deal(phys, DType::Physical, self.src_e_phys, false, true, 1.0);
                if self.e_per_stack_magic > 0.0 {
                    let magic = stacks * self.e_per_stack_magic;
                    e.deal(magic, DType::Magic, self.src_e_magic, false, true, 1.0);
                }
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_R_CAST) => {
                self.do_cast_r(e);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
