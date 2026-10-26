//! Skarner. Impale (R) opens the fight and briefly locks out Shattered
//! Earth (Q); Q is recast on cooldown (a 0.35s cast that arms 3 empowered
//! basic attacks whose 3rd landing deals bonus max-health damage and
//! restarts Q's cooldown), then keeps Skarner busy for that cast time;
//! Seismic Bastion (W) is cast on cooldown; Threads of Vibration (P) stacks
//! Quaking off every landed attack and Impale, triggering a max-health
//! damage-over-time at 3 stacks. Ixtal's Impact (E) is never cast: it
//! cannot land damage against a stationary dummy (see the kit's `unused`
//! field).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Threads of Vibration's damage-over-time ticks.
const EV_P_TICK: u8 = 0;
/// Safety valve: Shattered Earth's 5s empower window expiring unconsumed.
const EV_Q_EXPIRE: u8 = 1;
/// Seismic Bastion is cast; then its damage lands.
const EV_W_CAST: u8 = 2;
const EV_W_DMG: u8 = 3;
/// Impale's damage lands (after the opening cast).
const EV_R_DMG: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    p_ratio: f64,
    p_dur: f64,
    p_tick_interval: f64,
    p_stack_count: i64,
    src_p: SourceId,

    q_hit_dmg: f64,
    q_target_hp_ratio: f64,
    q_as_pct: f64,
    q_cd_base: f64,
    q_window_s: f64,
    q_empower_count: i64,
    q_cast_s: f64,
    src_q_slam: SourceId,

    w_hit_dmg: f64,
    w_cast_s: f64,
    w_cd_base: f64,

    r_hit_dmg: f64,
    r_cast_s: f64,
    r_suppress_s: f64,

    /// Q is locked out while Impale's cast and disable phase run.
    q_block_until: f64,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,

    p_stacks: i64,
    p_expire: f64,
    p_ticks_left: i64,
    p_tick_amt: f64,
    p_next_tick: f64,

    q_stacks_armed: i64,
    q_active_until: f64,
    q_expire_at: f64,

    w_ready: f64,
    w_dmg_at: f64,

    r_dmg_at: f64,
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

    fn apply_quaking(&mut self, e: &mut Engine, t: f64) {
        if t > self.s.p_expire {
            self.s.p_stacks = 0;
        }
        self.s.p_stacks = imin(self.s.p_stacks + 1, self.p_stack_count);
        self.s.p_expire = t + self.p_dur;
        if self.s.p_stacks >= self.p_stack_count {
            let total = self.p_ratio * e.target_hp;
            let ticks = (self.p_dur / self.p_tick_interval) as i64;
            self.s.p_tick_amt = total / (ticks as f64);
            self.s.p_ticks_left = ticks;
            self.s.p_next_tick = t + self.p_tick_interval;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let r_cast_s = kit.num("gen.R.castTimeS")?;
        let r_suppress_s = kit.num("gen.R.suppressDurationS")?;
        let q_block_until = if ranks.r > 0 { r_cast_s + r_suppress_s } else { 0.0 };

        let state = State {
            busy_until: 0.0,
            p_stacks: 0,
            p_expire: -INF,
            p_ticks_left: 0,
            p_tick_amt: 0.0,
            p_next_tick: INF,
            q_stacks_armed: 0,
            q_active_until: -INF,
            q_expire_at: INF,
            w_ready: 0.0,
            w_dmg_at: INF,
            r_dmg_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("skarner kit needs attack.windupFraction")?,

            p_ratio: kit.at_level("gen.P.percentHealthDamageByLevel", level)?,
            p_dur: kit.num("gen.P.durationS")?,
            p_tick_interval: kit.num("gen.P.tickIntervalS")?,
            p_stack_count: kit.num("gen.P.stackCount")? as i64,
            src_p: intern("P dot"),

            q_hit_dmg: kit.hit("gen.Q.hitDamage", ranks.q, sheet)?,
            q_target_hp_ratio: kit.num("gen.Q.targetMaxHpRatio")?,
            q_as_pct: kit.at_rank("gen.Q.attackSpeedPct", ranks.q)?,
            q_cd_base: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_window_s: kit.num("gen.Q.windowS")?,
            q_empower_count: kit.num("gen.Q.empowerCount")? as i64,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            src_q_slam: intern("Q slam"),

            w_hit_dmg: kit.hit("gen.W.hitDamage", ranks.w, sheet)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            w_cd_base: kit.at_rank("abilities.W.cooldownS", ranks.w)?,

            r_hit_dmg: kit.hit("gen.R.hitDamage", ranks.r, sheet)?,
            r_cast_s,
            r_suppress_s,

            q_block_until,

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
        if t < self.s.q_active_until {
            self.q_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.apply_quaking(e, t);
        if self.s.q_stacks_armed > 0 {
            e.deal(self.q_hit_dmg, DType::Physical, SRC_Q, false, false, 1.0);
            self.s.q_stacks_armed -= 1;
            if self.s.q_stacks_armed == 0 {
                let slam = self.q_target_hp_ratio * e.target_hp;
                e.deal(slam, DType::Physical, self.src_q_slam, false, false, 1.0);
                e.ability_cast_proc();
                e.st.q_ready = t + e.basic_cd(self.q_cd_base);
                self.s.q_active_until = t;
                self.s.q_expire_at = INF;
            }
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(self.castable_at(e, e.st.q_ready), self.q_block_until)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.q_stacks_armed = self.q_empower_count;
        self.s.q_active_until = t + self.q_window_s;
        self.s.q_expire_at = t + self.q_window_s;
        e.st.q_ready = INF;
        let b = self.bonus_as(t);
        e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_dmg_at = t + self.r_cast_s;
        e.prime_spellblade();
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.p_ticks_left > 0 {
            out[n] = (self.s.p_next_tick, Kind::Ev(EV_P_TICK));
            n += 1;
        }
        if self.s.q_expire_at != INF {
            out[n] = (self.s.q_expire_at, Kind::Ev(EV_Q_EXPIRE));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_dmg_at != INF {
                out[n] = (self.s.w_dmg_at, Kind::Ev(EV_W_DMG));
            } else {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.r > 0 && self.s.r_dmg_at != INF {
            out[n] = (self.s.r_dmg_at, Kind::Ev(EV_R_DMG));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_P_TICK) => {
                e.deal(self.s.p_tick_amt, DType::Magic, self.src_p, false, false, 1.0);
                self.s.p_ticks_left -= 1;
                if self.s.p_ticks_left > 0 {
                    self.s.p_next_tick = t + self.p_tick_interval;
                } else {
                    self.s.p_next_tick = INF;
                }
            }
            Kind::Ev(EV_Q_EXPIRE) => {
                if self.s.q_stacks_armed > 0 {
                    self.s.q_stacks_armed = 0;
                    e.st.q_ready = t + e.basic_cd(self.q_cd_base);
                }
                self.s.q_active_until = t;
                self.s.q_expire_at = INF;
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_dmg_at = t + self.w_cast_s;
                self.s.w_ready = INF;
                e.prime_spellblade();
                self.busy_for(e, self.w_cast_s);
            }
            Kind::Ev(EV_W_DMG) => {
                e.deal(self.w_hit_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.w_ready = t + e.basic_cd(self.w_cd_base);
                self.s.w_dmg_at = INF;
            }
            Kind::Ev(EV_R_DMG) => {
                e.deal(self.r_hit_dmg, DType::Magic, SRC_R, false, true, 1.0);
                self.apply_quaking(e, t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.s.r_dmg_at = INF;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
