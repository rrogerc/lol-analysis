//! Kit drivers: one per hand-encoded champion, owning the rotation — what
//! the champion does with its attacks and abilities. The engine calls these
//! hooks at each point of the fight (see fight::Driver).

use crate::fight::{shave, Driver, Engine, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// An auto-attacker: Zealous stacks attack speed per attack, Arisen makes
/// her ranged at 6, Aflame (11) rides a wave on every attack at full stacks,
/// E is an always-on on-hit plus an attack-reset active, Q shreds resists.
#[derive(Clone, Debug, PartialEq)]
pub struct KayleDriver {
    ranged: bool,
    attack_range: f64,
    ranks: Ranks,
    as_pct_per_stack: f64,
    max_stacks: i64,
    zeal_perm: bool,
    aflame: bool,
    wave_dmg: f64,
    q_dmg: f64,
    e_onhit: f64,
    q_cd_base: f64,
    e_cd_base: f64,
    q_shred_duration: f64,
    /// `e_active_pct / 100.0`: the rank and the sheet's AP settle it.
    e_active_frac: f64,
    windup_fraction: f64,
    /// The rotation state, and a pristine copy of it: `reset` is that copy,
    /// so a field cannot be added here and forgotten there.
    s: KayleState,
    s0: KayleState,
}

/// Everything of Kayle's rotation a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct KayleState {
    zeal: i64,
    e_ready: f64,
    e_pending: bool,
}

impl Driver for KayleDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, prestacked: bool)
        -> Result<Self, String> {
        let zeal = kit.zealous.as_ref().ok_or("kayle kit needs passive.zealous")?;
        let arisen = kit.arisen.as_ref().ok_or("kayle kit needs passive.arisen")?;
        let aflame = kit.aflame.as_ref().ok_or("kayle kit needs passive.aflame")?;
        let wave = aflame.wave.as_ref().ok_or("kayle kit needs passive.aflame.wave")?;
        let zeal_perm = level >= zeal.permanent_at_level;
        let ranged = level >= arisen.level;
        let aflame_on = level >= aflame.level;
        // form passives override the champion's base attack range
        let mut attack_range = sheet.base_attack_range;
        for form in [Some(arisen), kit.transcendent.as_ref()].into_iter().flatten() {
            if let Some(r) = form.attack_range {
                if level >= form.level {
                    attack_range = r;
                }
            }
        }
        // per-fight constants: the Q's damage, the E on-hit, the wave's damage
        let q_dmg = if ranks.q > 0 {
            kit.q.damage.as_ref().ok_or("kayle kit needs Q.damage")?.hit(ranks.q, sheet)
        } else {
            0.0
        };
        let e_onhit = if ranks.e > 0 {
            kit.e.onhit.as_ref().ok_or("kayle kit needs E.onhit")?.hit(ranks.e, sheet)
        } else {
            0.0
        };
        let wave_dmg = wave.base_by_level.at(level) + wave.bonus_ad_ratio * sheet.ad_bonus
            + wave.ap_ratio * sheet.ap;
        let e_active_frac = if ranks.e > 0 {
            let act = kit.e.active.as_ref().ok_or("kayle kit needs E.active")?;
            (act.missing_hp_pct[(ranks.e - 1) as usize]
                + act.missing_hp_pct_per_100_ap * sheet.ap / 100.0)
                / 100.0
        } else {
            0.0 / 100.0
        };
        let state = KayleState {
            zeal: if zeal_perm || prestacked { zeal.max_stacks } else { 0 },
            e_ready: 0.0,
            e_pending: false,
        };
        Ok(KayleDriver {
            ranged,
            attack_range,
            ranks,
            as_pct_per_stack: zeal.as_pct_per_stack,
            max_stacks: zeal.max_stacks,
            zeal_perm,
            aflame: aflame_on,
            wave_dmg,
            q_dmg,
            e_onhit,
            q_cd_base: if ranks.q > 0 { kit.q.cooldown_s[(ranks.q - 1) as usize] } else { 0.0 },
            e_cd_base: if ranks.e > 0 { kit.e.cooldown_s[(ranks.e - 1) as usize] } else { 0.0 },
            q_shred_duration: kit.q.shred_duration_s,
            e_active_frac,
            windup_fraction: kit.windup_fraction.ok_or("kayle kit needs attack.windupFraction")?,
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        self.ranged
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self) -> f64 {
        self.s.zeal as f64 * self.as_pct_per_stack
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {
        if !self.zeal_perm {
            self.s.zeal = imin(self.s.zeal + 1, self.max_stacks);
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.ranks.e > 0 {
            e.deal(self.e_onhit, DType::Magic, SRC_E_ONHIT, false, false, 1.0);
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.e_pending {
            let missing = e.target_hp - pymax(e.st.hp, 0.0);
            e.deal(self.e_active_frac * missing, DType::Magic, SRC_E_ACTIVE, self.aflame,
                   true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            self.s.e_pending = false;
        }
        if self.aflame && self.s.zeal >= self.max_stacks {
            e.deal(self.wave_dmg, DType::Magic, SRC_WAVE, true, true, 1.0);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        // E weave: cast right after an auto to use the attack reset
        let t = e.st.t;
        if self.ranks.e > 0 && t >= self.s.e_ready && !self.s.e_pending {
            self.s.e_pending = true;
            self.s.e_ready = t + e.basic_cd(self.e_cd_base);
            e.prime_spellblade();
            let b = self.bonus_as();
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            let b = self.bonus_as();
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd_base);
        e.st.shred_until = t + self.q_shred_duration;
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }
}

/// A caster who pays health, not mana: R opens the fight, E is charged for
/// 1s and released with attacks and Q held, W is cast as the charge begins,
/// Q is cast the moment it's castable and every third cast is empowered.
#[derive(Clone, Debug, PartialEq)]
pub struct VladimirDriver {
    ranged: bool,
    attack_range: f64,
    ranks: Ranks,
    q_dmg: f64,
    q_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    w_tick: f64,
    w_cd: f64,
    every_nth_cast: i64,
    /// `1.0 + bonus_damage_pct / 100.0`: the kit's own number.
    rush_mult: f64,
    charge_full_s: f64,
    w_duration_s: f64,
    w_ticks: i64,
    w_tick_s: f64,
    r_amp_pct: f64,
    r_delay_s: f64,
    /// The rotation state, and a pristine copy of it (see `KayleDriver`).
    s: VladState,
    s0: VladState,
}

/// Everything of Vladimir's rotation a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct VladState {
    e_ready: f64,
    w_ready: f64,
    q_casts: i64,
    busy_until: f64,
    charge_until: f64,
    pool_until: f64,
    w_ticks_left: i64,
    w_next: f64,
}

impl VladimirDriver {
    fn castable_at(&self, e: &Engine, ready: f64) -> f64 {
        let st = &e.st;
        let mut t = pymax(pymax(pymax(ready, st.t), self.s.busy_until), self.s.pool_until);
        if self.s.charge_until != INF {
            // a cast would cut the charge short
            t = pymax(t, self.s.charge_until);
        }
        t
    }

    fn cast_done(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.busy_until = t + ABILITY_LOCKOUT_S;
        e.st.next_attack = pymax(e.st.next_attack, t + ABILITY_LOCKOUT_S);
    }

    fn cast_w(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.w_ready = t + e.basic_cd(self.w_cd);
        self.s.pool_until = t + self.w_duration_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.pool_until);
        self.s.w_ticks_left = self.w_ticks;
        self.s.w_next = t;
        e.prime_spellblade();
    }
}

impl Driver for VladimirDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let _ = level;
        let attack_range = sheet.base_attack_range;
        let rush = kit.q.crimson_rush.as_ref().ok_or("vladimir kit needs Q.crimsonRush")?;
        let (q, w, e) = (ranks.q, ranks.w, ranks.e);
        let q_dmg = if q > 0 {
            kit.q.damage.as_ref().ok_or("vladimir kit needs Q.damage")?.hit(q, sheet)
        } else {
            0.0
        };
        let e_dmg = if e > 0 {
            kit.e.damage_max.as_ref().ok_or("vladimir kit needs E.damage.max")?.hit(e, sheet)
        } else {
            0.0
        };
        let w_ticks = kit.w.ticks.unwrap_or(0);
        let w_tick = if w > 0 {
            kit.w.damage.as_ref().ok_or("vladimir kit needs W.damage")?.hit(w, sheet)
                / w_ticks as f64
        } else {
            0.0
        };
        let state = VladState {
            e_ready: 0.0,
            w_ready: 0.0,
            q_casts: 0,
            busy_until: 0.0,
            charge_until: INF,
            pool_until: -1.0,
            w_ticks_left: 0,
            w_next: INF,
        };
        Ok(VladimirDriver {
            ranged: attack_range > MELEE_MAX_RANGE,
            attack_range,
            ranks,
            q_dmg,
            q_cd: if q > 0 { kit.q.cooldown_s[(q - 1) as usize] } else { INF },
            e_dmg,
            e_cd: if e > 0 { kit.e.cooldown_s[(e - 1) as usize] } else { INF },
            w_tick,
            w_cd: if w > 0 { kit.w.cooldown_s[(w - 1) as usize] } else { INF },
            every_nth_cast: rush.every_nth_cast,
            rush_mult: 1.0 + rush.bonus_damage_pct / 100.0,
            charge_full_s: kit.e.charge_full_s.unwrap_or(0.0),
            w_duration_s: kit.w.duration_s.unwrap_or(0.0),
            w_ticks,
            w_tick_s: kit.w.tick_s.unwrap_or(0.0),
            r_amp_pct: kit.r.amp_pct.unwrap_or(0.0),
            r_delay_s: kit.r.delay_s.unwrap_or(0.0),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        self.ranged
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q > 0 {
            self.castable_at(e, e.st.q_ready)
        } else {
            INF
        }
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.q_casts += 1;
        let mut amt = self.q_dmg;
        let empowered = self.s.q_casts % self.every_nth_cast == 0;
        if empowered {
            amt *= self.rush_mult;
        }
        e.deal(amt, DType::Magic, if empowered { SRC_Q_EMPOWERED } else { SRC_Q }, false, true,
               1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.cast_done(e);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.kit_amp_pct = self.r_amp_pct;
        e.st.kit_amp_mult = 1.0 + self.r_amp_pct / 100.0;
        e.st.kit_amp_until = t + self.r_delay_s;
        self.s.busy_until = t + ABILITY_LOCKOUT_S;
    }

    fn events(&self, e: &Engine, out: &mut [(f64, Kind); 2]) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            if self.s.charge_until != INF {
                out[n] = (self.s.charge_until, Kind::ERelease);
            } else {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::ECharge);
            }
            n += 1;
        }
        if self.s.w_ticks_left != 0 {
            out[n] = (self.s.w_next, Kind::WTick);
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::ECharge => {
                if self.castable_at(e, self.s.e_ready) > t {
                    return; // a cast at this instant took priority; try again
                }
                self.s.charge_until = t + self.charge_full_s;
                e.st.next_attack = pymax(e.st.next_attack, self.s.charge_until);
                if self.ranks.w > 0 && t >= self.s.w_ready {
                    self.cast_w(e);
                }
            }
            Kind::ERelease => {
                self.s.charge_until = INF;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.cast_done(e);
            }
            Kind::WTick => {
                e.deal(self.w_tick, DType::Magic, SRC_W, false, true, 1.0);
                self.s.w_ticks_left -= 1;
                self.s.w_next = if self.s.w_ticks_left != 0 { t + self.w_tick_s } else { INF };
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
