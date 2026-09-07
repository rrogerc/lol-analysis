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

    fn bonus_as(&self, _t: f64) -> f64 {
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
            let b = self.bonus_as(t);
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            let b = self.bonus_as(t);
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

    fn events(&self, e: &Engine, out: &mut [(f64, Kind); 4]) -> usize {
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

/// A marksman whose poison keeps the books: every on-hit stacks Deadly Venom
/// on the target (six at most, ticking true damage once a second), Ambush is
/// cast before the fight and its attack speed starts when the camouflage
/// breaks, Spray and Pray opens the fight with bonus attack damage, Venom
/// Cask is thrown while the stacks are short of six and Contaminate is cast
/// the moment they are not.
#[derive(Clone, Debug, PartialEq)]
pub struct TwitchDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Ambush's attack speed (percent) and how long it runs once the
    /// camouflage breaks.
    q_as_pct: f64,
    q_as_duration: f64,
    /// One second of one venom stack at this level and AP.
    venom_per_stack: f64,
    venom_max: i64,
    venom_duration: f64,
    venom_tick: f64,
    w_cd: f64,
    w_stacks_on_hit: i64,
    cloud_ticks: i64,
    cloud_tick_s: f64,
    cloud_stacks: i64,
    e_cd: f64,
    e_base: f64,
    /// The per-stack physical part without its bonus-AD term, which reads the
    /// bonus attack damage of the moment at `e_stack_bad` per point.
    e_stack_static: f64,
    e_stack_bad: f64,
    /// The per-stack magic part, settled by the sheet's AP.
    e_stack_magic: f64,
    e_stacks_needed: i64,
    r_bonus_ad: f64,
    r_duration: f64,
    /// The sheet's bonus attack damage, for Contaminate's ratio.
    ad_bonus: f64,
    /// The rotation state, and a pristine copy of it (see `KayleDriver`).
    s: TwitchState,
    s0: TwitchState,
}

/// Everything of Twitch's rotation a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct TwitchState {
    /// Deadly Venom stacks on the target, when they lapse, and the next tick
    /// (INF while nothing is ticking).
    stacks: i64,
    venom_until: f64,
    venom_next: f64,
    /// Ambush's camouflage is still up (the fight opens from it); once it
    /// breaks the attack speed runs until `q_until`.
    q_pending: bool,
    q_until: f64,
    w_ready: f64,
    cloud_left: i64,
    cloud_next: f64,
    e_ready: f64,
    r_until: f64,
}

impl TwitchDriver {
    /// The venom stacks on the target at `t`: none once they have lapsed.
    fn stacks_at(&self, t: f64) -> i64 {
        if t > self.s.venom_until {
            0
        } else {
            self.s.stacks
        }
    }

    /// Bonus attack damage at `t`: the sheet's, plus Spray and Pray's while
    /// it runs.
    fn bonus_ad_at(&self, t: f64) -> f64 {
        if t < self.s.r_until {
            self.ad_bonus + self.r_bonus_ad
        } else {
            self.ad_bonus
        }
    }

    /// `n` applications of Deadly Venom at `t`: lapsed stacks are gone first,
    /// the count is capped, the duration refreshed, and the once-a-second
    /// tick started if it is not already running (a refresh never moves it).
    fn add_stacks(&mut self, t: f64, n: i64) {
        if t > self.s.venom_until {
            self.s.stacks = 0;
        }
        self.s.stacks = imin(self.s.stacks + n, self.venom_max);
        self.s.venom_until = t + self.venom_duration;
        if self.s.venom_next == INF {
            self.s.venom_next = t + self.venom_tick;
        }
    }

    /// The camouflage breaks (an attack, or a cask): Ambush's attack speed
    /// starts now.
    fn break_stealth(&mut self, t: f64) {
        if self.s.q_pending {
            self.s.q_pending = false;
            self.s.q_until = t + self.q_as_duration;
        }
    }
}

impl Driver for TwitchDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let venom = kit.deadly_venom.as_ref().ok_or("twitch kit needs passive.deadlyVenom")?;
        let table = &venom.per_stack_by_level;
        if table.is_empty() {
            return Err("twitch kit needs passive.deadlyVenom.perStackPerSecond.byLevel".into());
        }
        let lv = imin(imax(level, 1), table.len() as i64) as usize;
        let venom_per_stack = table[lv - 1] + venom.ap_ratio * sheet.ap;
        let (q_as_pct, q_as_duration) = if ranks.q > 0 {
            let a = kit.q.attack_speed.as_ref().ok_or("twitch kit needs Q.attackSpeed")?;
            (a.pct[(ranks.q - 1) as usize], a.duration_s)
        } else {
            (0.0, 0.0)
        };
        let (w_cd, w_stacks_on_hit, cloud_ticks, cloud_tick_s, cloud_stacks) = if ranks.w > 0 {
            let c = kit.w.cloud.as_ref().ok_or("twitch kit needs W.cloud")?;
            (kit.w.cooldown_s[(ranks.w - 1) as usize],
             kit.w.stacks_on_hit.ok_or("twitch kit needs W.stacksOnHit")?,
             (c.duration_s / c.tick_s) as i64, c.tick_s, c.stacks_per_tick)
        } else {
            (INF, 0, 0, 0.0, 0)
        };
        let (e_cd, e_base, e_stack_static, e_stack_bad, e_stack_magic, e_stacks_needed) =
            if ranks.e > 0 {
                let r = (ranks.e - 1) as usize;
                let base = kit.e.damage.as_ref().ok_or("twitch kit needs E.damage")?;
                let phys = kit.e.per_stack_phys.as_ref()
                    .ok_or("twitch kit needs E.perStack.physical")?;
                let magic = kit.e.per_stack_magic.as_ref()
                    .ok_or("twitch kit needs E.perStack.magic")?;
                (kit.e.cooldown_s[r], base.hit(ranks.e, sheet),
                 phys.base[r] + phys.ap_ratio * sheet.ap, phys.bonus_ad_ratio,
                 magic.hit(ranks.e, sheet),
                 kit.e.max_stacks.ok_or("twitch kit needs E.maxStacks")?)
            } else {
                (INF, 0.0, 0.0, 0.0, 0.0, i64::MAX)
            };
        let (r_bonus_ad, r_duration) = if ranks.r > 0 {
            let b = kit.r.bonus_ad.as_ref().ok_or("twitch kit needs R.bonusAd")?;
            (b[(ranks.r - 1) as usize], kit.r.duration_s.ok_or("twitch kit needs R.durationS")?)
        } else {
            (0.0, 0.0)
        };
        let state = TwitchState {
            stacks: 0,
            venom_until: -1.0,
            venom_next: INF,
            q_pending: ranks.q > 0,
            q_until: -1.0,
            w_ready: 0.0,
            cloud_left: 0,
            cloud_next: INF,
            e_ready: 0.0,
            r_until: -1.0,
        };
        Ok(TwitchDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_as_pct,
            q_as_duration,
            venom_per_stack,
            venom_max: venom.max_stacks,
            venom_duration: venom.duration_s,
            venom_tick: venom.tick_s,
            w_cd,
            w_stacks_on_hit,
            cloud_ticks,
            cloud_tick_s,
            cloud_stacks,
            e_cd,
            e_base,
            e_stack_static,
            e_stack_bad,
            e_stack_magic,
            e_stacks_needed,
            r_bonus_ad,
            r_duration,
            ad_bonus: sheet.ad_bonus,
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
        if t < self.s.q_until {
            self.q_as_pct
        } else {
            0.0
        }
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        if e.st.t < self.s.r_until {
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

    fn before_attack(&mut self, e: &mut Engine) {
        // an attack breaks the camouflage at the start of its windup
        let t = e.st.t;
        self.break_stealth(t);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // on-hit: the main hit, a phantom hit, Dusk and Dawn's second pass
        let t = e.st.t;
        self.add_stacks(t, 1);
    }

    /// Ambush is never recast inside a fight (see the kit's note).
    fn q_at(&self, _e: &Engine) -> f64 {
        INF
    }

    fn cast_q(&mut self, _e: &mut Engine) {
        unreachable!("Twitch's q_at is INF: the engine never casts his Q")
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // Spray and Pray, from camouflage: bonus AD (and range) for a while,
        // no damage of its own; the engine already primed Spellblade and
        // applied the cast lockout
        self.s.r_until = e.st.t + self.r_duration;
    }

    fn events(&self, e: &Engine, out: &mut [(f64, Kind); 4]) -> usize {
        let t = e.st.t;
        let mut n = 0;
        if self.s.venom_next != INF {
            out[n] = (self.s.venom_next, Kind::Venom);
            n += 1;
        }
        if self.s.cloud_left > 0 {
            out[n] = (self.s.cloud_next, Kind::Cloud);
            n += 1;
        } else if self.ranks.w > 0 && self.stacks_at(t) < self.venom_max {
            out[n] = (pymax(self.s.w_ready, t), Kind::WCast);
            n += 1;
        }
        if self.ranks.e > 0 && self.stacks_at(t) >= self.e_stacks_needed {
            out[n] = (pymax(self.s.e_ready, t), Kind::ECast);
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Venom => {
                let amt = self.stacks_at(t) as f64 * self.venom_per_stack;
                e.deal(amt, DType::True, SRC_VENOM, false, false, 1.0);
                self.s.venom_next = if t + self.venom_tick <= self.s.venom_until {
                    t + self.venom_tick
                } else {
                    INF
                };
            }
            Kind::WCast => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.add_stacks(t, self.w_stacks_on_hit);
                self.s.cloud_left = self.cloud_ticks;
                self.s.cloud_next = if self.cloud_ticks > 0 { t + self.cloud_tick_s } else { INF };
                self.break_stealth(t);
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Cloud => {
                self.add_stacks(t, self.cloud_stacks);
                self.s.cloud_left -= 1;
                self.s.cloud_next = if self.s.cloud_left > 0 { t + self.cloud_tick_s } else { INF };
            }
            Kind::ECast => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                let stacks = self.stacks_at(t) as f64;
                let per = self.e_stack_static + self.e_stack_bad * self.bonus_ad_at(t);
                e.deal(self.e_base + stacks * per, DType::Physical, SRC_E, false, true, 1.0);
                let magic = stacks * self.e_stack_magic;
                if magic != 0.0 {
                    e.deal(magic, DType::Magic, SRC_E_MAGIC, false, true, 1.0);
                }
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
                self.break_stealth(t);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
