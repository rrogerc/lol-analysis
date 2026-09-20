//! Corki. A ranged auto-attacker whose kit layers onto his attacks:
//! Hextech Munitions rides every on-hit as bonus true damage, Phosphorus
//! Bomb (Q) and Gatling Gun (E) and Valkyrie (W) are cast the instant they
//! are ready, and Missile Barrage (R) fires on a 2 s cooldown from a stocked
//! ammo pool that recharges over time and is accelerated by on-hit attacks.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// W: cast-readiness check, then its patch's ticks.
const EV_W_CAST: u8 = 0;
const EV_W_TICK: u8 = 1;
/// E: cast-readiness check, then its channel's ticks.
const EV_E_CAST: u8 = 2;
const EV_E_TICK: u8 = 3;
/// R: fire a stocked missile, and separately refill the ammo pool.
const EV_R_FIRE: u8 = 4;
const EV_R_RECHARGE: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    p_onhit_ratio: f64,
    q_dmg: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    w_ticks: i64,
    w_tick_interval: f64,
    e_dmg: f64,
    e_cd: f64,
    e_ticks: i64,
    e_tick_interval: f64,
    r_small_dmg: f64,
    r_big_dmg: f64,
    r_fire_cd: f64,
    r_max_ammo: i64,
    r_recharge_s: f64,
    r_cdr_reduction: f64,
    src_p_onhit: SourceId,
    src_r_big: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    w_active: bool,
    w_ticks_left: i64,
    w_next_tick: f64,
    e_ready: f64,
    e_active: bool,
    e_ticks_left: i64,
    e_next_tick: f64,
    r_ammo: i64,
    r_next_fire: f64,
    /// When the currently-filling ammo charge completes (INF: pool is full).
    r_recharge_at: f64,
    r_shot_count: i64,
}

impl GenDriver {
    /// Fires one stocked missile: damage, Big One accounting, and the
    /// bookkeeping for the next shot and the ammo pool. Does not touch
    /// Spellblade/lockout: the caller decides whether those are needed.
    fn fire_missile(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_ammo -= 1;
        self.s.r_shot_count += 1;
        let is_big = self.s.r_shot_count % 3 == 0;
        let (dmg, src) = if is_big {
            (self.r_big_dmg, self.src_r_big)
        } else {
            (self.r_small_dmg, SRC_R)
        };
        e.deal(dmg, DType::Physical, src, false, true, 1.0);
        e.ability_cast_proc();
        e.ult_hatefog();
        self.s.r_next_fire = t + self.r_fire_cd;
        if self.s.r_ammo < self.r_max_ammo && self.s.r_recharge_at == INF {
            self.s.r_recharge_at = t + e.ult_cd(self.r_recharge_s);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let start_ammo = kit.num("gen.R.assumedStartAmmo")? as i64;
        let state = State {
            w_ready: 0.0,
            w_active: false,
            w_ticks_left: 0,
            w_next_tick: INF,
            e_ready: 0.0,
            e_active: false,
            e_ticks_left: 0,
            e_next_tick: INF,
            r_ammo: start_ammo,
            r_next_fire: 0.0,
            r_recharge_at: INF,
            r_shot_count: 0,
        };
        let r_small_dmg = kit.hit("gen.R.damage", ranks.r, sheet)?;
        let r_multiplier = kit.num("gen.R.bigOneMultiplier")?;
        let crit_frac = sheet.crit_chance / 100.0;
        let cdr_base = kit.num("gen.R.cdrOnHitBase")?;
        let cdr_coef = kit.num("gen.R.cdrOnHitCritCoef")?;
        let _ = level;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("corki kit needs attack.windupFraction")?,
            p_onhit_ratio: kit.num("gen.P.onhitAdRatio")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_ticks: kit.num("gen.W.ticks")? as i64,
            w_tick_interval: kit.num("gen.W.tickIntervalS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_ticks: kit.num("gen.E.ticks")? as i64,
            e_tick_interval: kit.num("gen.E.tickIntervalS")?,
            r_small_dmg,
            r_big_dmg: r_small_dmg * r_multiplier,
            r_fire_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_max_ammo: kit.num("gen.R.maxAmmo")? as i64,
            r_recharge_s: kit.num("gen.R.rechargeS")?,
            r_cdr_reduction: cdr_base * (1.0 + cdr_coef * crit_frac),
            src_p_onhit: intern("P onhit"),
            src_r_big: intern("R big one"),
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
        0.0
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Hextech Munitions: bonus true damage on every on-hit application.
        let ad = e.p.ad;
        e.deal(self.p_onhit_ratio * ad, DType::True, self.src_p_onhit, true, false, 1.0);
        // Missile Barrage: on-hit against a champion accelerates the
        // currently-filling ammo charge, scaling with crit chance.
        if self.ranks.r > 0 && self.s.r_recharge_at != INF {
            self.s.r_recharge_at -= self.r_cdr_reduction;
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
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack past the cast lockout
        if self.ranks.r == 0 || self.s.r_ammo == 0 {
            return;
        }
        self.fire_missile(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_active {
                out[n] = (self.s.w_next_tick, Kind::Ev(EV_W_TICK));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_active {
                out[n] = (self.s.e_next_tick, Kind::Ev(EV_E_TICK));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_ammo < self.r_max_ammo && self.s.r_recharge_at != INF {
                out[n] = (self.s.r_recharge_at, Kind::Ev(EV_R_RECHARGE));
                n += 1;
            }
            if self.s.r_ammo > 0 {
                out[n] = (pymax(self.s.r_next_fire, e.st.t), Kind::Ev(EV_R_FIRE));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                e.prime_spellblade();
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_active = true;
                self.s.w_ticks_left = self.w_ticks;
                self.s.w_next_tick = t + self.w_tick_interval;
            }
            Kind::Ev(EV_W_TICK) => {
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                self.s.w_ticks_left -= 1;
                if self.s.w_ticks_left > 0 {
                    self.s.w_next_tick = t + self.w_tick_interval;
                } else {
                    self.s.w_active = false;
                    self.s.w_next_tick = INF;
                }
            }
            Kind::Ev(EV_E_CAST) => {
                e.prime_spellblade();
                e.ability_cast_proc();
                e.eclipse_hit();
                // cooldown starts on cast, not when the channel ends
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_active = true;
                self.s.e_ticks_left = self.e_ticks;
                self.s.e_next_tick = t + self.e_tick_interval;
            }
            Kind::Ev(EV_E_TICK) => {
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                self.s.e_ticks_left -= 1;
                if self.s.e_ticks_left > 0 {
                    self.s.e_next_tick = t + self.e_tick_interval;
                } else {
                    self.s.e_active = false;
                    self.s.e_next_tick = INF;
                }
            }
            Kind::Ev(EV_R_FIRE) => {
                e.prime_spellblade();
                e.lockout();
                self.fire_missile(e);
            }
            Kind::Ev(EV_R_RECHARGE) => {
                self.s.r_ammo = imin(self.s.r_ammo + 1, self.r_max_ammo);
                if self.s.r_ammo < self.r_max_ammo {
                    self.s.r_recharge_at = t + e.ult_cd(self.r_recharge_s);
                } else {
                    self.s.r_recharge_at = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
