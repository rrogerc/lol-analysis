//! Vladimir. Opens with Hemoplague (its burst lands after a 4 s delayed
//! event, and it amps the target for that window). Transfusion has a 0.25 s
//! cast time and is cast on cooldown, empowered whenever Crimson Rush's
//! 0/1/2 counter has reached 2. Sanguine Pool and Tides of Blood are each
//! modelled as a full action lock (no attacks or other casts) run
//! sequentially on their own cooldowns, and neither starts inside
//! Transfusion's cast: Sanguine Pool ticks four times over 2 s, Tides of
//! Blood always charges for exactly 1 s (its ramp to max damage) before
//! releasing.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const EV_W_CAST: u8 = 0;
const EV_W_TICK: u8 = 1;
const EV_E_START: u8 = 2;
const EV_E_RELEASE: u8 = 3;
const EV_R_BURST: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,

    q_base_dmg: f64,
    q_emp_dmg: f64,
    q_cd: f64,
    q_surge_need: i64,
    q_cast_s: f64,

    w_tick_dmg: f64,
    w_cd: f64,
    w_lock_s: f64,
    w_tick_interval: f64,
    w_tick_count: i64,

    e_max_dmg: f64,
    e_cd: f64,
    e_charge_hold_s: f64,

    r_dmg: f64,
    r_amp_pct: f64,
    r_duration_s: f64,

    src_q_emp: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_stacks: i64,

    w_ready: f64,
    w_active: bool,
    w_tick_idx: i64,
    w_next_tick_at: f64,

    e_ready: f64,
    e_charging: bool,
    e_release_at: f64,

    r_burst_at: f64,

    busy_until: f64,
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
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        // Crimson Pact: flat, non-recursive conversion from the pre-passive
        // item/level stats, added into every other ability's scaling.
        let hp_for_ap_pct = kit.num("gen.P.hpForApPct")?;
        let ap_to_hp_pct = kit.num("gen.P.apToHpPct")?;
        let p_ap_bonus = hp_for_ap_pct / 100.0 * sheet.hp_bonus;
        let p_hp_bonus = ap_to_hp_pct / 100.0 * sheet.ap;

        let mut sheet2 = sheet.clone();
        sheet2.ap = sheet.ap + p_ap_bonus;
        sheet2.hp_bonus = sheet.hp_bonus + p_hp_bonus;
        sheet2.hp = sheet.hp + p_hp_bonus;

        let r_amp_pct = kit.num("gen.R.ampPct")?;
        let r_dmg = kit.hit("gen.R.damage", ranks.r, &sheet2)? * (1.0 + r_amp_pct / 100.0);

        let w_tick_count = kit.num("gen.W.ticksCount")? as i64;
        let w_total = kit.hit("gen.W.damage", ranks.w, &sheet2)?;

        let state = State {
            q_stacks: 0,
            w_ready: 0.0,
            w_active: false,
            w_tick_idx: 0,
            w_next_tick_at: INF,
            e_ready: 0.0,
            e_charging: false,
            e_release_at: INF,
            r_burst_at: INF,
            busy_until: 0.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,

            q_base_dmg: kit.hit("gen.Q.damage", ranks.q, &sheet2)?,
            q_emp_dmg: kit.hit("gen.Q.empoweredDamage", ranks.q, &sheet2)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_surge_need: kit.num("gen.Q.surgeStacksNeeded")? as i64,
            q_cast_s: kit.num("gen.Q.castTimeS")?,

            w_tick_dmg: w_total / (w_tick_count as f64),
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_lock_s: kit.num("gen.W.lockDurationS")?,
            w_tick_interval: kit.num("gen.W.tickIntervalS")?,
            w_tick_count,

            e_max_dmg: kit.hit("gen.E.maxDamage", ranks.e, &sheet2)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_charge_hold_s: kit.num("gen.E.rampToMaxS")?,

            r_dmg,
            r_amp_pct,
            r_duration_s: kit.num("gen.R.durationS")?,

            src_q_emp: intern("Q empowered"),

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
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        let empowered = self.s.q_stacks >= self.q_surge_need;
        if empowered {
            e.deal(self.q_emp_dmg, DType::Magic, self.src_q_emp, false, true, 1.0);
            self.s.q_stacks = 0;
        } else {
            e.deal(self.q_base_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        }
        self.s.q_stacks = imin(self.s.q_stacks + 1, self.q_surge_need);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        e.st.kit_amp_pct = self.r_amp_pct;
        e.st.kit_amp_mult = 1.0 + self.r_amp_pct / 100.0;
        e.st.kit_amp_until = t + self.r_duration_s;
        self.s.r_burst_at = t + self.r_duration_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 && !self.s.w_active {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.s.w_active {
            out[n] = (self.s.w_next_tick_at, Kind::Ev(EV_W_TICK));
            n += 1;
        }
        if self.ranks.e > 0 && !self.s.e_charging {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_START));
            n += 1;
        }
        if self.s.e_charging {
            out[n] = (self.s.e_release_at, Kind::Ev(EV_E_RELEASE));
            n += 1;
        }
        if self.s.r_burst_at != INF {
            out[n] = (self.s.r_burst_at, Kind::Ev(EV_R_BURST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.busy_until = pymax(self.s.busy_until, t + self.w_lock_s);
                e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
                e.deal(self.w_tick_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.w_active = true;
                self.s.w_tick_idx = 1;
                self.s.w_next_tick_at = t + self.w_tick_interval;
            }
            Kind::Ev(EV_W_TICK) => {
                e.deal(self.w_tick_dmg, DType::Magic, SRC_W, false, true, 1.0);
                self.s.w_tick_idx += 1;
                if self.s.w_tick_idx >= self.w_tick_count {
                    self.s.w_active = false;
                    self.s.w_next_tick_at = INF;
                } else {
                    self.s.w_next_tick_at = t + self.w_tick_interval;
                }
            }
            Kind::Ev(EV_E_START) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_charging = true;
                self.s.e_release_at = t + self.e_charge_hold_s;
                self.s.busy_until = pymax(self.s.busy_until, self.s.e_release_at);
                e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
            }
            Kind::Ev(EV_E_RELEASE) => {
                self.s.e_charging = false;
                self.s.e_release_at = INF;
                e.deal(self.e_max_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_BURST) => {
                self.s.r_burst_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
