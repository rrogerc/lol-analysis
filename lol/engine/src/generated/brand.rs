//! Brand. A mage whose damage rides Ablaze: every ability hit stacks it
//! (up to 3, refreshing its shared expiry), reaching 3 stacks arms a delayed
//! ring explosion, and Pillar of Flame is empowered 25% against a live
//! Ablaze target. Casts go one at a time: each of Sear, Pillar of Flame,
//! Conflagration and Pyroclasm has a 0.25s cast time that keeps Brand busy
//! (no other cast, no attack) until it ends, tracked with one shared
//! `busy_until`. Pyroclasm opens the fight and is recast on cooldown, each
//! cast landing 3 separate hits on the sole target (only the first of the
//! three is itself a new cast).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const EV_W_CAST: u8 = 0;
const EV_W_HIT: u8 = 1;
const EV_E_CAST: u8 = 2;
const EV_ABLAZE_TICK: u8 = 3;
const EV_EXPLOSION: u8 = 4;
const EV_R_HIT2: u8 = 5;
const EV_R_HIT3: u8 = 6;
const EV_R_CAST: u8 = 7;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_dmg: f64,
    q_cd: f64,
    q_cast_time: f64,

    w_dmg: f64,
    w_cd: f64,
    w_bonus_pct: f64,
    w_cast_time: f64,
    w_eruption_delay: f64,

    e_dmg: f64,
    e_cd: f64,
    e_cast_time: f64,

    r_dmg: f64,
    r_cd: f64,
    r_bounce_delay: f64,
    r_cast_time: f64,

    ablaze_max: i64,
    ablaze_duration: f64,
    ablaze_tick_interval: f64,
    ablaze_tick_frac: f64,
    explosion_delay: f64,
    explosion_immune_s: f64,
    p_explosion_ratio: f64,

    src_p_dot: SourceId,
    src_p_explosion: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    busy_until: f64,

    ablaze_count: i64,
    ablaze_until: f64,
    next_tick_at: f64,
    explosion_at: f64,
    explosion_immune_until: f64,

    w_ready: f64,
    w_hit_at: f64,

    e_ready: f64,

    r_ready: f64,
    r_hit2_at: f64,
    r_hit3_at: f64,
}

impl GenDriver {
    /// The earliest a cast readied at `ready` can start: not before now, and
    /// not inside another cast.
    fn castable_at(&self, e: &Engine, ready: f64) -> f64 {
        pymax(pymax(ready, e.st.t), self.s.busy_until)
    }

    /// A cast with a cast time just started: no other cast and no attack
    /// until it ends (an attack already due later keeps its time).
    fn busy_for(&mut self, e: &mut Engine, cast_s: f64) {
        self.s.busy_until = e.st.t + cast_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
    }

    fn apply_ablaze(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let was_active = self.s.ablaze_count > 0 && t < self.s.ablaze_until;
        if t < self.s.explosion_immune_until {
            if self.s.ablaze_count == 0 {
                self.s.ablaze_count = 1;
            }
        } else {
            self.s.ablaze_count = imin(self.s.ablaze_count + 1, self.ablaze_max);
        }
        if !was_active {
            self.s.next_tick_at = t + self.ablaze_tick_interval;
        }
        self.s.ablaze_until = t + self.ablaze_duration;
        if self.s.ablaze_count >= self.ablaze_max && self.s.explosion_at == INF {
            self.s.explosion_at = t + self.explosion_delay;
        }
    }

    fn fire_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.apply_ablaze(e);
        self.s.r_hit2_at = t + self.r_bounce_delay * 2.0;
        self.s.r_hit3_at = t + self.r_bounce_delay * 4.0;
        self.busy_for(e, self.r_cast_time);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let pct_frac = kit.at_level("gen.P.explosionPctByLevel", level)? / 100.0;
        let ap_pct_per_100 = kit.num("gen.P.explosionApPctPer100Ap")?;
        let ap_coef = (ap_pct_per_100 / 100.0) / 100.0;
        let p_explosion_ratio = pct_frac + ap_coef * sheet.ap;

        let ablaze_duration = kit.num("gen.P.ablazeDurationS")?;
        let ablaze_tick_interval = kit.num("gen.P.ablazeTickIntervalS")?;
        let ablaze_dot_pct = kit.num("gen.P.ablazeDotPct")?;
        let ticks = ablaze_duration / ablaze_tick_interval;
        let ablaze_tick_frac = (ablaze_dot_pct / 100.0) / ticks;

        let state = State {
            busy_until: 0.0,
            ablaze_count: 0,
            ablaze_until: 0.0,
            next_tick_at: INF,
            explosion_at: INF,
            explosion_immune_until: 0.0,
            w_ready: 0.0,
            w_hit_at: INF,
            e_ready: 0.0,
            r_ready: 0.0,
            r_hit2_at: INF,
            r_hit3_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("brand kit needs attack.windupFraction")?,

            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_time: kit.num("gen.Q.castTimeS")?,

            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_bonus_pct: kit.num("gen.W.empoweredBonusPct")?,
            w_cast_time: kit.num("gen.W.castTimeS")?,
            w_eruption_delay: kit.num("gen.W.eruptionDelayS")?,

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_time: kit.num("gen.E.castTimeS")?,

            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_bounce_delay: kit.num("gen.R.bounceDelayS")?,
            r_cast_time: kit.num("gen.R.castTimeS")?,

            ablaze_max: kit.num("gen.P.ablazeMaxStacks")? as i64,
            ablaze_duration,
            ablaze_tick_interval,
            ablaze_tick_frac,
            explosion_delay: kit.num("gen.P.explosionDelayS")?,
            explosion_immune_s: kit.num("gen.P.explosionImmuneS")?,
            p_explosion_ratio,

            src_p_dot: intern("P ablaze"),
            src_p_explosion: intern("P explosion"),

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

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.apply_ablaze(e);
        self.busy_for(e, self.q_cast_time);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.fire_r(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_hit_at != INF {
                out[n] = (self.s.w_hit_at, Kind::Ev(EV_W_HIT));
            } else {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.next_tick_at != INF {
            out[n] = (self.s.next_tick_at, Kind::Ev(EV_ABLAZE_TICK));
            n += 1;
        }
        if self.s.explosion_at != INF {
            out[n] = (self.s.explosion_at, Kind::Ev(EV_EXPLOSION));
            n += 1;
        }
        if self.s.r_hit2_at != INF {
            out[n] = (self.s.r_hit2_at, Kind::Ev(EV_R_HIT2));
            n += 1;
        }
        if self.s.r_hit3_at != INF {
            out[n] = (self.s.r_hit3_at, Kind::Ev(EV_R_HIT3));
            n += 1;
        }
        if self.ranks.r > 0 && self.s.r_hit2_at == INF && self.s.r_hit3_at == INF {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_hit_at = t + self.w_cast_time + self.w_eruption_delay;
                e.prime_spellblade();
                self.busy_for(e, self.w_cast_time);
            }
            Kind::Ev(EV_W_HIT) => {
                self.s.w_hit_at = INF;
                let empowered = self.s.ablaze_count > 0 && t < self.s.ablaze_until;
                let mut dmg = self.w_dmg;
                if empowered {
                    dmg *= 1.0 + self.w_bonus_pct / 100.0;
                }
                e.deal(dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.apply_ablaze(e);
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.apply_ablaze(e);
                self.busy_for(e, self.e_cast_time);
            }
            Kind::Ev(EV_ABLAZE_TICK) => {
                if self.s.ablaze_count > 0 && t < self.s.ablaze_until {
                    let dmg = self.ablaze_tick_frac * (self.s.ablaze_count as f64) * e.target_hp;
                    e.deal(dmg, DType::Magic, self.src_p_dot, false, false, 1.0);
                    self.s.next_tick_at = t + self.ablaze_tick_interval;
                } else {
                    self.s.ablaze_count = 0;
                    self.s.next_tick_at = INF;
                }
            }
            Kind::Ev(EV_EXPLOSION) => {
                self.s.explosion_at = INF;
                let dmg = self.p_explosion_ratio * e.target_hp;
                e.deal(dmg, DType::Magic, self.src_p_explosion, false, false, 1.0);
                self.s.ablaze_count = 1;
                self.s.ablaze_until = t + self.ablaze_duration;
                self.s.next_tick_at = t + self.ablaze_tick_interval;
                self.s.explosion_immune_until = t + self.explosion_immune_s;
            }
            Kind::Ev(EV_R_HIT2) => {
                self.s.r_hit2_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                self.apply_ablaze(e);
            }
            Kind::Ev(EV_R_HIT3) => {
                self.s.r_hit3_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                self.apply_ablaze(e);
                e.ult_hatefog();
            }
            Kind::Ev(EV_R_CAST) => {
                self.fire_r(e);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
