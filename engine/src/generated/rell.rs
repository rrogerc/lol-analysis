//! Rell. Opens with Magnet Storm's pull and damage-over-time field, then
//! alternates Ferromancy between Crash Down (dismount, deals damage and
//! applies a Break the Mold stack) and Mount Up (mount, arms the next basic
//! attack) on one shared cooldown, casts Shattering Strike on cooldown,
//! casts Full Tilt on cooldown to keep its next-attack-or-Q explosion armed
//! (self-cast, since no ally exists, but the explosion still lands on the
//! dummy through the qualifying attack or Q), and attacks continuously so
//! Break the Mold's on-hit damage and stacking resistance shred apply on
//! every hit.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Ferromancy comes off its shared cooldown and casts whichever form is up.
const EV_W_CAST: u8 = 0;
/// Magnet Storm's damage-over-time field ticks every 0.25 s for 2 s.
const EV_R_TICK: u8 = 1;
/// Magnet Storm's cooldown comes up again (never reached in a short fight).
const EV_R_CAST: u8 = 2;
/// Full Tilt is cast (self-cast, no ally) to arm the next attack/Q explosion.
const EV_E_CAST: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    p_onhit_armor_coef: f64,
    p_onhit_mr_coef: f64,
    p_max_stacks: i64,
    p_stack_duration: f64,

    q_dmg: f64,
    q_cd: f64,

    w_crash_dmg: f64,
    w_mount_dmg: f64,
    w_cd: f64,
    w_dismount_resist_pct: f64,
    w_dismount_as_pct: f64,
    w_mount_cast_s: f64,
    w_mount_window_s: f64,

    e_pct: f64,
    e_cd: f64,
    e_window: f64,

    r_tick_dmg: f64,
    r_cd: f64,
    r_tick_interval: f64,
    r_ticks_total: i64,

    src_p_onhit: SourceId,
    src_w_crash: SourceId,
    src_w_mount: SourceId,
    src_r_tick: SourceId,
    src_e: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    mounted: bool,
    w_ready: f64,
    mount_armed: bool,
    mount_expire: f64,
    p_stacks: i64,
    p_last_hit: f64,
    r_ready: f64,
    r_ticks_left: i64,
    r_tick_next: f64,
    e_ready: f64,
    e_armed: bool,
    e_expire: f64,
}

impl GenDriver {
    fn apply_p_stack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t - self.s.p_last_hit > self.p_stack_duration {
            self.s.p_stacks = 0;
        }
        self.s.p_stacks = imin(self.s.p_stacks + 1, self.p_max_stacks);
        self.s.p_last_hit = t;
        if self.s.p_stacks >= self.p_max_stacks {
            e.st.shred_until = t + self.p_stack_duration;
        }
    }

    fn start_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.lockout();
        e.prime_spellblade();
        e.ability_cast_proc();
        e.eclipse_hit();
        e.ult_hatefog();
        self.s.r_ticks_left = self.r_ticks_total;
        self.s.r_tick_next = t + self.r_tick_interval;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
    }

    /// If Full Tilt is armed and still within its window, this qualifying
    /// attack or Shattering Strike triggers its explosion on the dummy.
    fn try_consume_e(&mut self, e: &mut Engine) {
        if !self.s.e_armed {
            return;
        }
        if e.st.t <= self.s.e_expire {
            self.s.e_armed = false;
            let dmg = self.e_pct * e.target_hp;
            e.deal(dmg, DType::Magic, self.src_e, false, true, 1.0);
        } else {
            self.s.e_armed = false;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let r_duration = kit.num("gen.R.durationS")?;
        let r_tick_interval = kit.num("gen.R.tickIntervalS")?;
        let r_ticks_total = pymax(1.0, r_duration / r_tick_interval + 0.5) as i64;

        let e_base = kit.at_rank("gen.E.percentHealthDamage", ranks.e)?;
        let e_ap_coef = kit.num("gen.E.apPerHundredCoef")?;
        let e_pct = e_base + (e_ap_coef / 100.0) * sheet.ap;

        let state = State {
            mounted: true,
            w_ready: 0.0,
            mount_armed: false,
            mount_expire: 0.0,
            p_stacks: 0,
            p_last_hit: 0.0,
            r_ready: 0.0,
            r_ticks_left: 0,
            r_tick_next: INF,
            e_ready: 0.0,
            e_armed: false,
            e_expire: 0.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("rell kit needs attack.windupFraction")?,

            p_onhit_armor_coef: kit.num("gen.P.onhitArmorCoef")?,
            p_onhit_mr_coef: kit.num("gen.P.onhitMrCoef")?,
            p_max_stacks: kit.num("gen.P.maxStacks")? as i64,
            p_stack_duration: kit.num("gen.P.stackDurationS")?,

            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,

            w_crash_dmg: kit.hit("gen.W.crashDownDamage", ranks.w, sheet)?,
            w_mount_dmg: kit.hit("gen.W.mountUpDamage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_dismount_resist_pct: kit.num("gen.W.dismountResistPct")?,
            w_dismount_as_pct: kit.num("gen.W.dismountAsPct")?,
            w_mount_cast_s: kit.num("gen.W.mountUpCastTimeS")?,
            w_mount_window_s: kit.num("gen.W.mountUpWindowS")?,

            e_pct,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_window: kit.num("gen.E.windowS")?,

            r_tick_dmg: kit.hit("gen.R.tickDamage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_tick_interval,
            r_ticks_total,

            src_p_onhit: intern("P onhit"),
            src_w_crash: intern("W crash"),
            src_w_mount: intern("W mount"),
            src_r_tick: intern("R tick"),
            src_e: intern("E explosion"),

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
        if self.s.mounted { 0.0 } else { self.w_dismount_as_pct }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        if self.s.mount_armed && e.st.t > self.s.mount_expire {
            self.s.mount_armed = false;
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        let mult = if self.s.mounted {
            1.0
        } else {
            1.0 + self.w_dismount_resist_pct / 100.0
        };
        let armor_eff = e.p.sheet.armor * mult;
        let mr_eff = e.p.sheet.mr * mult;
        let dmg = self.p_onhit_armor_coef * armor_eff + self.p_onhit_mr_coef * mr_eff;
        e.deal(dmg, DType::Magic, self.src_p_onhit, false, false, 1.0);
        self.apply_p_stack(e);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.mount_armed {
            self.s.mount_armed = false;
            e.deal(self.w_mount_dmg, DType::Magic, self.src_w_mount, false, true, 1.0);
        }
        self.try_consume_e(e);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        self.apply_p_stack(e);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.try_consume_e(e);
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.start_r(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_ticks_left > 0 {
                out[n] = (self.s.r_tick_next, Kind::Ev(EV_R_TICK));
            } else {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                if self.s.mounted {
                    self.s.mounted = false;
                    e.deal(self.w_crash_dmg, DType::Magic, self.src_w_crash, false, true, 1.0);
                    self.apply_p_stack(e);
                    e.ability_cast_proc();
                    e.eclipse_hit();
                    e.prime_spellblade();
                    e.lockout();
                } else {
                    self.s.mounted = true;
                    self.s.mount_armed = true;
                    self.s.mount_expire = t + self.w_mount_window_s;
                    e.prime_spellblade();
                    e.st.next_attack = t + self.w_mount_cast_s;
                }
                self.s.w_ready = t + e.basic_cd(self.w_cd);
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_armed = true;
                self.s.e_expire = t + self.e_window;
                e.prime_spellblade();
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
            Kind::Ev(EV_R_TICK) => {
                e.deal(self.r_tick_dmg, DType::Magic, self.src_r_tick, false, true, 1.0);
                self.apply_p_stack(e);
                self.s.r_ticks_left -= 1;
                if self.s.r_ticks_left > 0 {
                    self.s.r_tick_next = t + self.r_tick_interval;
                } else {
                    self.s.r_tick_next = INF;
                }
            }
            Kind::Ev(EV_R_CAST) => {
                self.start_r(e);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
