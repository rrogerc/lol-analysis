//! Malzahar. A pure caster: no basic attacks. Nether Grasp opens the fight
//! (single cast, its cooldown never returns within the window), Malefic
//! Visions follows to start its DoT, then Call of the Void (delayed 0.4s
//! hit) and Void Swarm (summoning Voidlings from Zz'Rot Swarm stacks) go out
//! on cooldown for the rest of the fight.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Call of the Void's delayed hit.
const EV_Q_HIT: u8 = 0;
/// Malefic Visions: the on-cooldown cast, and its recurring tick.
const EV_E_CAST: u8 = 1;
const EV_E_TICK: u8 = 2;
/// Void Swarm: the on-cooldown cast, and the recurring Voidling attack tick.
const EV_W_CAST: u8 = 3;
const EV_W_VOID_TICK: u8 = 4;
/// Nether Grasp: the tether beam's recurring tick, and Null Zone's.
const EV_R_TETHER: u8 = 5;
const EV_R_ZONE: u8 = 6;

/// Fixed slots for tracked Voidlings (at most 3 are summoned per Void Swarm
/// cast, and its cooldown is shorter than a Voidling's duration, so two
/// generations can overlap).
const N_VOID: usize = 6;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    q_dmg: f64,
    q_cd: f64,
    q_delay: f64,
    e_dmg_total: f64,
    e_tick_dmg: f64,
    e_ticks: i64,
    e_tick_interval: f64,
    e_duration: f64,
    e_cd: f64,
    w_cd: f64,
    w_per_attack: f64,
    w_duration: f64,
    w_summon_delay: f64,
    w_stack_cap: i64,
    w_attack_interval: f64,
    r_beam_tick: f64,
    r_beam_ticks: i64,
    r_beam_interval: f64,
    r_zone_frac_per_tick: f64,
    r_zone_ticks: i64,
    r_zone_interval: f64,
    src_w_onhit: SourceId,
    src_r_zone: SourceId,
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_hit_at: f64,
    w_ready: f64,
    e_ready: f64,
    e_active: bool,
    e_dot_end: f64,
    e_tick_next: f64,
    r_tether_next: f64,
    r_tether_done: i64,
    r_zone_next: f64,
    r_zone_done: i64,
    stacks: i64,
    void_idx: i64,
    void_start: [f64; N_VOID],
    void_end: [f64; N_VOID],
    void_tick_next: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let e_duration = kit.num("gen.E.durationS")?;
        let e_tick_interval = kit.num("gen.E.tickIntervalS")?;
        let e_ticks = (e_duration / e_tick_interval) as i64;
        let e_dmg_total = kit.hit("gen.E.damage", ranks.e, sheet)?;
        let e_tick_dmg = if e_ticks > 0 { e_dmg_total / (e_ticks as f64) } else { 0.0 };

        let voidling_base_rank = kit.at_rank("gen.W.voidlingBaseDamage", ranks.w)?;
        let voidling_level = kit.at_level("gen.W.byLevel", level)?;
        let w_ap_ratio = kit.num("gen.W.apRatio")?;
        let w_ad_ratio = kit.num("gen.W.adRatio")?;
        let w_per_attack = voidling_base_rank + voidling_level
            + sheet.ap * w_ap_ratio + sheet.ad_bonus * w_ad_ratio;

        let r_beam_total = kit.hit("gen.R.beamDamage", ranks.r, sheet)?;
        let r_beam_ticks = kit.num("gen.R.beamTicks")? as i64;
        let r_beam_tick = if r_beam_ticks > 0 { r_beam_total / (r_beam_ticks as f64) } else { 0.0 };

        let r_zone_pct = kit.at_rank("gen.R.zoneMaxHpPctByRank", ranks.r)?;
        let r_zone_ap_per100 = kit.num("gen.R.zoneApPctPer100")?;
        let r_zone_frac_total = r_zone_pct / 100.0 + (sheet.ap / 100.0) * (r_zone_ap_per100 / 100.0);
        let r_zone_duration = kit.num("gen.R.zoneDurationS")?;
        let r_zone_interval = kit.num("gen.R.zoneTickIntervalS")?;
        let r_zone_ticks = (r_zone_duration / r_zone_interval) as i64;
        let r_zone_frac_per_tick = if r_zone_ticks > 0 {
            r_zone_frac_total / (r_zone_ticks as f64)
        } else {
            0.0
        };

        let state = State {
            q_hit_at: INF,
            w_ready: 0.0,
            e_ready: 0.0,
            e_active: false,
            e_dot_end: 0.0,
            e_tick_next: INF,
            r_tether_next: INF,
            r_tether_done: 0,
            r_zone_next: INF,
            r_zone_done: 0,
            stacks: 0,
            void_idx: 0,
            void_start: [INF; N_VOID],
            void_end: [INF; N_VOID],
            void_tick_next: INF,
        };

        Ok(GenDriver {
            ranks,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_delay: kit.num("gen.Q.postCastDelayS")?,
            e_dmg_total,
            e_tick_dmg,
            e_ticks,
            e_tick_interval,
            e_duration,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_per_attack,
            w_duration: kit.at_rank("gen.W.durationByRank", ranks.w)?,
            w_summon_delay: kit.num("gen.W.summonDelayS")?,
            w_stack_cap: kit.num("gen.W.stackCap")? as i64,
            w_attack_interval: kit.num("gen.W.assumedAttackIntervalS")?,
            r_beam_tick,
            r_beam_ticks,
            r_beam_interval: kit.num("gen.R.beamTickIntervalS")?,
            r_zone_frac_per_tick,
            r_zone_ticks,
            r_zone_interval,
            src_w_onhit: intern("W onhit"),
            src_r_zone: intern("R zone"),
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
        0.0
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.stacks = imin(self.s.stacks + 1, self.w_stack_cap);
        self.s.q_hit_at = t + self.q_delay;
        e.prime_spellblade();
        e.ability_cast_proc();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.stacks = imin(self.s.stacks + 1, self.w_stack_cap);
        self.s.r_tether_next = t + self.r_beam_interval;
        self.s.r_tether_done = 0;
        self.s.r_zone_next = t + self.r_zone_interval;
        self.s.r_zone_done = 0;
        e.ability_cast_proc();
        e.eclipse_hit();
        e.lockout();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_hit_at != INF {
            out[n] = (self.s.q_hit_at, Kind::Ev(EV_Q_HIT));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.e_tick_next != INF {
            out[n] = (self.s.e_tick_next, Kind::Ev(EV_E_TICK));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.s.void_tick_next != INF {
            out[n] = (self.s.void_tick_next, Kind::Ev(EV_W_VOID_TICK));
            n += 1;
        }
        if self.s.r_tether_next != INF {
            out[n] = (self.s.r_tether_next, Kind::Ev(EV_R_TETHER));
            n += 1;
        }
        if self.s.r_zone_next != INF {
            out[n] = (self.s.r_zone_next, Kind::Ev(EV_R_ZONE));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_HIT) => {
                self.s.q_hit_at = INF;
                e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
                e.eclipse_hit();
                if self.s.e_active {
                    self.s.e_dot_end = t + self.e_duration;
                }
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.stacks = imin(self.s.stacks + 1, self.w_stack_cap);
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_active = true;
                self.s.e_dot_end = t + self.e_duration;
                self.s.e_tick_next = t + self.e_tick_interval;
                e.prime_spellblade();
                e.ability_cast_proc();
                e.eclipse_hit();
                e.lockout();
            }
            Kind::Ev(EV_E_TICK) => {
                if self.s.e_active && t < self.s.e_dot_end {
                    e.deal(self.e_tick_dmg, DType::Magic, SRC_E, false, true, 1.0);
                    let next = t + self.e_tick_interval;
                    if next < self.s.e_dot_end {
                        self.s.e_tick_next = next;
                    } else {
                        self.s.e_tick_next = INF;
                        self.s.e_active = false;
                    }
                } else {
                    self.s.e_tick_next = INF;
                    self.s.e_active = false;
                }
            }
            Kind::Ev(EV_W_CAST) => {
                let num_summons = 1 + self.s.stacks;
                self.s.stacks = 0;
                let mut i: i64 = 0;
                while i < num_summons {
                    let start = t + self.w_summon_delay * ((i + 1) as f64);
                    let end = start + self.w_duration;
                    let idx = (self.s.void_idx as usize) % N_VOID;
                    self.s.void_idx += 1;
                    self.s.void_start[idx] = start;
                    self.s.void_end[idx] = end;
                    if self.s.void_tick_next == INF || start < self.s.void_tick_next {
                        self.s.void_tick_next = start;
                    }
                    i += 1;
                }
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_VOID_TICK) => {
                let mut any_future = false;
                let mut idx = 0usize;
                while idx < N_VOID {
                    if self.s.void_start[idx] <= t && t < self.s.void_end[idx] {
                        e.deal(self.w_per_attack, DType::Magic, self.src_w_onhit, false, false, 1.0);
                    }
                    if self.s.void_end[idx] > t || self.s.void_start[idx] > t {
                        any_future = true;
                    }
                    idx += 1;
                }
                if any_future {
                    self.s.void_tick_next = t + self.w_attack_interval;
                } else {
                    self.s.void_tick_next = INF;
                }
            }
            Kind::Ev(EV_R_TETHER) => {
                if self.s.r_tether_done < self.r_beam_ticks {
                    e.deal(self.r_beam_tick, DType::Magic, SRC_R, false, true, 1.0);
                    self.s.r_tether_done += 1;
                    if self.s.r_tether_done == 1 {
                        e.ult_hatefog();
                    }
                    if self.s.r_tether_done < self.r_beam_ticks {
                        self.s.r_tether_next = t + self.r_beam_interval;
                    } else {
                        self.s.r_tether_next = INF;
                    }
                } else {
                    self.s.r_tether_next = INF;
                }
            }
            Kind::Ev(EV_R_ZONE) => {
                if self.s.r_zone_done < self.r_zone_ticks {
                    let dmg = self.r_zone_frac_per_tick * e.target_hp;
                    e.deal(dmg, DType::Magic, self.src_r_zone, false, true, 1.0);
                    self.s.r_zone_done += 1;
                    if self.s.r_zone_done < self.r_zone_ticks {
                        self.s.r_zone_next = t + self.r_zone_interval;
                    } else {
                        self.s.r_zone_next = INF;
                    }
                } else {
                    self.s.r_zone_next = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
