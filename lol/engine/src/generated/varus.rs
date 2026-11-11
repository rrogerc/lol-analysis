//! Varus (Patch 16.21). Chain of Corruption opens the fight, applying three
//! staggered Blight stacks over its root window; Hail of Arrows and Piercing
//! Arrow detonate full three-stack sets whenever Varus can build stacks (unless
//! an immediate cast executes the target), and Piercing Arrow dynamically picks
//! its discrete 0.125 s charge step k in 0..=10 on every cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St, S_COMBAT_T0};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Event ordering matters on exact timestamp ties: R stack applications resolve
/// first, then pending arrow/zone impacts, then new ability casts.
const EV_R_STACK: u8 = 0;
const EV_Q_RELEASE: u8 = 1;
const EV_E_LAND: u8 = 2;
const EV_E_DETON2: u8 = 3;
const EV_R_CAST: u8 = 4;
const EV_E_CAST: u8 = 5;

fn reduce_cd(target: &mut f64, full_cd: f64, t: f64, pct: f64) {
    if *target > t {
        *target = pymax(t, *target - full_cd * pct);
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    level: i64,
    attack_range: f64,

    q_cd_base: f64,
    q_dmg: f64,
    q_step_s: f64,
    q_step_amp: f64,
    q_max_steps: i64,

    w_onhit_dmg: f64,
    w_hp_pct: f64,
    w_max_stacks: i64,
    w_stack_dur: f64,
    w_cdr_per_stack: f64,
    w_active_pct: f64,
    w_active_cd_base: f64,

    e_dmg: f64,
    e_cd_base: f64,
    e_cast_s: f64,
    e_land_delay: f64,
    e_deton2_delay: f64,

    r_dmg: f64,
    r_cd_base: f64,
    r_cast_s: f64,
    r_first_stack_delay: f64,
    r_stack_interval: f64,
    r_stacks_total: i64,

    src_w_onhit: SourceId,
    src_w_deton_q: SourceId,
    src_w_deton_e: SourceId,
    src_w_deton_r: SourceId,
    src_w_active: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    blight_stacks: i64,
    blight_expire: f64,
    busy_until: f64,
    q_release_at: f64,
    q_charge_cur: f64,
    q_mult_cur: f64,
    w_active_ready: f64,
    e_ready: f64,
    e_land_at: f64,
    e_deton2_at: f64,
    r_ready: f64,
    r_stack_at: f64,
    r_stacks_left: i64,
}

impl GenDriver {
    fn castable_at(&self, e: &Engine, ready: f64) -> f64 {
        pymax(pymax(ready, e.st.t), self.s.busy_until)
    }

    fn busy_for(&mut self, e: &mut Engine, cast_s: f64) {
        self.s.busy_until = e.st.t + cast_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
    }

    fn stacks_at(&self, t: f64) -> i64 {
        if self.s.blight_stacks <= 0 || t >= self.s.blight_expire {
            0
        } else {
            self.s.blight_stacks
        }
    }

    fn add_blight(&mut self, t: f64, n: i64) {
        let cur = self.stacks_at(t);
        self.s.blight_stacks = imin(cur + n, self.w_max_stacks);
        self.s.blight_expire = t + self.w_stack_dur;
    }

    /// Blight stacks present at `t_eval` when starting from `t_now` and
    /// accounting for any pending Chain of Corruption stack ticks in between.
    fn stacks_after_r(&self, t_now: f64, t_eval: f64, include_exact_now: bool) -> i64 {
        let mut st = self.stacks_at(t_now);
        let mut exp = if st > 0 { self.s.blight_expire } else { 0.0 };
        let mut left = self.s.r_stacks_left;
        let mut r_t = self.s.r_stack_at;
        while left > 0 && r_t <= t_eval + 1e-9 {
            let due = if include_exact_now { r_t >= t_now } else { r_t > t_now };
            if due {
                if r_t >= exp {
                    st = 0;
                }
                st = imin(st + 1, self.w_max_stacks);
                exp = r_t + self.w_stack_dur;
            }
            left -= 1;
            r_t += self.r_stack_interval;
        }
        if st <= 0 || t_eval >= exp {
            0
        } else {
            st
        }
    }

    fn detonate(&mut self, e: &mut Engine, mult: f64, src: SourceId) {
        let t = e.st.t;
        let stacks_i = self.stacks_at(t);
        self.s.blight_stacks = 0;
        if stacks_i <= 0 {
            return;
        }
        let stacks = stacks_i as f64;
        let dmg = self.w_hp_pct * stacks * mult * e.target_hp;
        e.deal(dmg, DType::Magic, src, false, false, 1.0);

        let total_pct = self.w_cdr_per_stack * mult * stacks;
        let q_full = e.basic_cd(self.q_cd_base);
        let e_full = e.basic_cd(self.e_cd_base);
        let w_full = e.basic_cd(self.w_active_cd_base);
        reduce_cd(&mut e.st.q_ready, q_full, t, total_pct);
        reduce_cd(&mut self.s.e_ready, e_full, t, total_pct);
        reduce_cd(&mut self.s.w_active_ready, w_full, t, total_pct);
    }

    fn fire_q(&mut self, e: &mut Engine, charge_s: f64, mult: f64) {
        let t = e.st.t;
        let use_w = self.ranks.w > 0 && t >= self.s.w_active_ready;
        // Set Q and W active cooldowns before detonating Blight so Q's own
        // Blight consumption refunds both Q and W active properly.
        e.st.q_ready = pymax(t, t + e.basic_cd(self.q_cd_base) - charge_s);
        if use_w {
            self.s.w_active_ready = t + e.basic_cd(self.w_active_cd_base);
        }
        e.deal(self.q_dmg * mult, DType::Physical, SRC_Q, false, true, 1.0);
        self.detonate(e, mult, self.src_w_deton_q);
        if use_w {
            let missing = e.target_hp - pymax(e.st.hp, 0.0);
            let dmg = self.w_active_pct * mult * missing;
            e.deal(dmg, DType::Magic, self.src_w_active, false, true, 1.0);
        }
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, ABILITY_LOCKOUT_S);
    }

    fn fire_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ult_hatefog();
        self.detonate(e, 1.0, self.src_w_deton_r);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.r_stacks_left = self.r_stacks_total;
        self.s.r_stack_at = t + self.r_first_stack_delay;
        self.s.r_ready = t + e.ult_cd(self.r_cd_base);
        self.busy_for(e, self.r_cast_s);
    }

    fn est_phys_mult(&self, e: &Engine, t: f64, extra_cleaver: i64) -> f64 {
        let sheet = e.p.sheet;
        let s = &e.p.fx.s;
        let dark_pen = match &s.alt_pen {
            Some(a) => imin(e.st.dark, a.max_stacks) as f64 * a.pct_per_stack,
            None => 0.0,
        };
        let shred = match &s.armor_shred {
            Some(a) => imin(e.st.cleaver + extra_cleaver, a.max_stacks) as f64 * a.pct_per_stack,
            None => 0.0,
        };
        let mut leth = sheet.lethality;
        if let Some(o) = &s.opener_lethality {
            if t < o.duration_s {
                leth += o.ranged;
            }
        }
        resist_mult(eff_resist(
            e.target_armor,
            0.0,
            shred,
            stack_pct_pen(sheet.armor_pen_pct, dark_pen),
            leth,
        ))
    }

    fn est_mag_mult(&self, e: &Engine, t: f64, extra_blood: i64) -> f64 {
        let sheet = e.p.sheet;
        let s = &e.p.fx.s;
        let dark_pen = match &s.alt_pen {
            Some(a) => imin(e.st.dark, a.max_stacks) as f64 * a.pct_per_stack,
            None => 0.0,
        };
        let mal = match &s.ult_burn {
            Some(u) if t < e.st.mal_shred_until => u.mr_reduction,
            _ => 0.0,
        };
        let shred = match &s.mr_shred {
            Some(a) => imin(e.st.blood + extra_blood, a.max_stacks) as f64 * a.pct_per_stack,
            None => 0.0,
        };
        resist_mult(eff_resist(
            e.target_mr,
            mal,
            shred,
            stack_pct_pen(sheet.magic_pen_pct, dark_pen),
            sheet.magic_pen_flat,
        ))
    }

    fn est_amp(
        &self,
        e: &Engine,
        dt: DType,
        t: f64,
        ability: bool,
        extra_shojin: i64,
        hp_at_hit: f64,
    ) -> f64 {
        if dt == DType::True {
            return 1.0;
        }
        let fx = e.p.fx;
        let s = &fx.s;
        let sheet = e.p.sheet;
        let mut amp = 1.0;
        if !fx.dmg_amps.is_empty() {
            let t0 = if e.st.set & S_COMBAT_T0 != 0 { e.st.combat_t0 } else { t };
            let secs = imax(0, (t - t0 + 1e-9) as i64);
            for a in &fx.dmg_amps {
                amp *= 1.0 + a.pct_per_stack / 100.0 * imin(a.max_stacks, secs) as f64;
            }
        }
        for a in &fx.flat_amps {
            if a.dtype.is_none() || a.dtype == Some(dt) {
                amp *= 1.0 + a.pct / 100.0;
            }
        }
        if let Some(max_pct) = s.giant_slayer {
            amp *= 1.0 + pymin(max_pct, e.target_bonus_hp / 100.0) / 100.0;
        }
        if let Some(h) = &s.hypershot {
            if t < e.st.hz_until || (ability && !e.st.hz_first) {
                if t < e.st.hz_until {
                    amp *= 1.0 + h.amp_pct / 100.0;
                }
            }
        }
        if ability {
            if let Some(sh) = &s.ability_amp_stacking {
                let st = imin(e.st.shojin + extra_shojin, sh.max_stacks);
                amp *= 1.0 + sh.pct_per_stack / 100.0 * st as f64;
            }
            if let Some(m) = &s.mana_active {
                if t < e.st.ma_until {
                    let pct = m.amp_base_pct + m.amp_per_100_bonus_mana * sheet.mana_bonus / 100.0;
                    amp *= 1.0 + pct / 100.0;
                }
            }
        }
        if dt == DType::Magic {
            if let Some(mc) = &s.magic_crit {
                if hp_at_hit / e.target_hp * 100.0 < mc.below_target_hp_pct {
                    amp *= mc.crit_dmg_pct / 100.0;
                }
            }
        }
        amp
    }

    fn est_cur_as(&self, e: &Engine, t: f64) -> f64 {
        let sheet = e.p.sheet;
        let s = &e.p.fx.s;
        let mut bonus = sheet.bonus_as_pct;
        if let Some(a) = &s.as_stacking {
            bonus += e.st.seething as f64 * a.pct_per_stack;
        }
        if let Some(f) = &s.flurry {
            if t < e.st.flurry_until {
                bonus += f.as_pct;
            }
        }
        if let Some(u) = &s.on_ult_cast {
            if t < e.st.hex_until {
                bonus += u.as_pct;
            }
        }
        if let Some(u) = &s.ult_attack_steroid {
            if e.st.post_r_attacks < u.attacks {
                bonus += u.as_pct;
            }
        }
        pymin(sheet.base_as + sheet.as_ratio * bonus / 100.0, AS_CAP)
    }

    /// Post-mitigation damage dealt when Piercing Arrow starts at `t_start` and
    /// releases after `k` charge steps (`k in 0..=q_max_steps`).
    fn est_q_burst(&self, e: &Engine, t_start: f64, k: i64) -> f64 {
        let c = (k as f64) * self.q_step_s;
        let t_rel = t_start + c;
        let mult = 1.0 + (k as f64) * self.q_step_amp;
        let stacks = self.stacks_after_r(e.st.t, t_rel, k > 0 || t_start > e.st.t) as f64;

        let mut hp = pymax(e.st.hp, 0.0);
        let mut total = 0.0;
        let mut cleaver_add = 0;

        // 1. Piercing Arrow physical hit (ability = true).
        let q_m = self.est_phys_mult(e, t_rel, 0);
        let q_amp = self.est_amp(e, DType::Physical, t_rel, true, 0, hp);
        let q_hit = (self.q_dmg * mult) * q_amp * q_m;
        hp = pymax(hp - q_hit, 0.0);
        total += q_hit;
        cleaver_add += 1;

        if let Some(once) = &e.p.fx.s.ability_proc_once {
            if !e.st.once_done {
                let raw = once.base + once.ap_ratio * e.p.sheet.ap;
                let m = match once.dtype {
                    DType::Physical => self.est_phys_mult(e, t_rel, cleaver_add),
                    DType::Magic => self.est_mag_mult(e, t_rel, 0),
                    DType::True => 1.0,
                };
                let o_hit = raw * self.est_amp(e, once.dtype, t_rel, false, 1, hp) * m;
                hp = pymax(hp - o_hit, 0.0);
                total += o_hit;
                if once.dtype == DType::Physical {
                    cleaver_add += 1;
                }
            }
        }

        // 2. Blight stack detonation (magic proc, before W active missing-HP).
        if stacks > 0.0 {
            let det_raw = self.w_hp_pct * stacks * mult * e.target_hp;
            let det_m = self.est_mag_mult(e, t_rel, 0);
            let det_amp = self.est_amp(e, DType::Magic, t_rel, false, 1, hp);
            let det_hit = det_raw * det_amp * det_m;
            hp = pymax(hp - det_hit, 0.0);
            total += det_hit;
        }

        // 3. Blighted Quiver active missing-HP execute (ability = true).
        if self.ranks.w > 0 && t_rel >= self.s.w_active_ready {
            let missing = e.target_hp - hp;
            let w_raw = self.w_active_pct * mult * missing;
            let w_m = self.est_mag_mult(e, t_rel, 0);
            let w_amp = self.est_amp(e, DType::Magic, t_rel, true, 1, hp);
            let w_hit = w_raw * w_amp * w_m;
            hp = pymax(hp - w_hit, 0.0);
            total += w_hit;
        }

        // 4. Muramana and Eclipse procs on cast.
        if let Some(mp) = &e.p.fx.s.ability_mana_proc {
            let raw = mp.pct_by_level.at(self.level) / 100.0 * e.p.sheet.mana;
            let m = match mp.dtype {
                DType::Physical => self.est_phys_mult(e, t_rel, cleaver_add),
                DType::Magic => self.est_mag_mult(e, t_rel, 0),
                DType::True => 1.0,
            };
            let hit = raw * self.est_amp(e, mp.dtype, t_rel, false, 1, hp) * m;
            hp = pymax(hp - hit, 0.0);
            total += hit;
            if mp.dtype == DType::Physical {
                cleaver_add += 1;
            }
        }
        if let Some(ec) = &e.p.fx.s.hit_pair_proc {
            if e.st.eclipse >= 1 {
                let raw = ec.max_hp_pct_ranged / 100.0 * e.target_hp;
                let m = match ec.dtype {
                    DType::Physical => self.est_phys_mult(e, t_rel, cleaver_add),
                    DType::Magic => self.est_mag_mult(e, t_rel, 0),
                    DType::True => 1.0,
                };
                total += raw * self.est_amp(e, ec.dtype, t_rel, false, 1, hp) * m;
            }
        }

        total
    }

    fn est_e_burst(&self, e: &Engine, t_cast: f64) -> f64 {
        let t_land = t_cast + self.e_land_delay;
        let stacks = self.stacks_after_r(e.st.t, t_land, true) as f64;
        let hp = pymax(e.st.hp, 0.0);
        let e_hit = self.e_dmg
            * self.est_amp(e, DType::Physical, t_land, true, 0, hp)
            * self.est_phys_mult(e, t_land, 0);
        let hp_after = pymax(hp - e_hit, 0.0);
        let det_hit = if stacks > 0.0 {
            (self.w_hp_pct * stacks * e.target_hp)
                * self.est_amp(e, DType::Magic, t_land, false, 1, hp_after)
                * self.est_mag_mult(e, t_land, 0)
        } else {
            0.0
        };
        e_hit + det_hit
    }

    fn onhit_phantom_mult(&self, e: &Engine) -> f64 {
        let s = &e.p.fx.s;
        if let Some(p) = &s.phantom {
            let ready = match &s.as_stacking {
                Some(a) => e.st.seething >= a.max_stacks,
                None => true,
            };
            if ready && p.stacks_needed > 0 {
                return 1.0 + 1.0 / ((p.stacks_needed + 1) as f64);
            }
        }
        1.0
    }

    /// Expected post-mitigation damage of one basic attack at `t` when target
    /// health is `hp_est`. When `amortize` is false, returns the immediate single
    /// swing damage for lethal checks; when true, includes multi-hit expectations.
    fn est_auto_swing(&self, e: &Engine, t: f64, hp_est: f64, amortize: bool) -> f64 {
        let sheet = e.p.sheet;
        let fx = e.p.fx;
        let s = &fx.s;
        let phys_m = self.est_phys_mult(e, t, 0);
        let mag_m = self.est_mag_mult(e, t, 0);
        let phys_amp = self.est_amp(e, DType::Physical, t, false, 0, hp_est);
        let mag_amp = self.est_amp(e, DType::Magic, t, false, 0, hp_est);

        let crit_c = sheet.crit_chance / 100.0;
        let mut crit_ev = 1.0 + crit_c * (sheet.crit_damage / 100.0 - 1.0);
        if let Some(sun) = s.first_attack_crit_floor_ev {
            if !e.st.sundered_used && crit_ev < sun {
                crit_ev = sun;
            }
        }
        let mut auto_amp = 1.0;
        if let Some(aa) = &s.attack_amp {
            auto_amp += aa.max_pct / 100.0 * pymin(1.0, self.attack_range / aa.max_at_range);
        }
        let mut total = e.p.ad * auto_amp * crit_ev * phys_amp * phys_m;

        let oh_mult = if amortize { self.onhit_phantom_mult(e) } else { 1.0 };
        let mut onhit_sum = 0.0;
        if self.ranks.w > 0 {
            onhit_sum += self.w_onhit_dmg * mag_amp * mag_m;
        }
        for oh in &fx.onhit {
            let mut raw = oh.base
                + oh.ap_ratio * sheet.ap
                + oh.bonus_ad_ratio * sheet.ad_bonus
                + oh.max_mana_pct / 100.0 * sheet.mana;
            if let Some((_, rng)) = oh.self_max_hp_pct {
                raw += rng / 100.0 * sheet.hp;
            }
            let m = match oh.dtype {
                DType::Physical => phys_amp * phys_m,
                DType::Magic => mag_amp * mag_m,
                DType::True => 1.0,
            };
            onhit_sum += raw * m;
        }
        for oh in &fx.onhit_current_hp {
            let raw = oh.ranged_pct / 100.0 * hp_est;
            let m = match oh.dtype {
                DType::Physical => phys_amp * phys_m,
                DType::Magic => mag_amp * mag_m,
                DType::True => 1.0,
            };
            onhit_sum += raw * m;
        }
        total += oh_mult * onhit_sum;

        if amortize {
            if let Some(kr) = &s.kraken {
                let base = kr.base_by_level.at(self.level) * kr.ranged_mult;
                let miss = 1.0 - pymax(hp_est, 0.0) / e.target_hp;
                let raw = base * (1.0 + kr.missing_ranged / 100.0 * miss) / 3.0;
                let m = match kr.dtype {
                    DType::Physical => phys_amp * phys_m,
                    DType::Magic => mag_amp * mag_m,
                    DType::True => 1.0,
                };
                total += raw * m;
            }
            if !fx.energized.is_empty() {
                let mut extra = 0.0;
                for en in &fx.energized {
                    extra += en.extra_stacks_per_attack;
                }
                let rate = (10.0 + extra) / 100.0;
                for en in &fx.energized {
                    let m = match en.dtype {
                        DType::Physical => phys_amp * phys_m,
                        DType::Magic => mag_amp * mag_m,
                        DType::True => 1.0,
                    };
                    total += rate * en.bonus * m;
                }
            }
            if let Some(nh) = &s.nth_hit_proc {
                if nh.stacks_needed_ranged > 0 {
                    let raw = nh.base_ad_ratio_ranged * sheet.ad_base
                        + nh.self_max_hp_pct_ranged / 100.0 * sheet.hp;
                    let m = match nh.dtype {
                        DType::Physical => phys_amp * phys_m,
                        DType::Magic => mag_amp * mag_m,
                        DType::True => 1.0,
                    };
                    total += raw * m / ((nh.stacks_needed_ranged + 1) as f64);
                }
            }
        } else if e.st.sb_primed || t >= e.st.sb_icd_until {
            if let Some(sb) = &s.spellblade {
                let raw = sb.base_ad_ratio * sheet.ad_base
                    + sb.ap_ratio * sheet.ap
                    + sb.per_crit_chance_pct * sheet.crit_chance;
                let m = match sb.dtype {
                    DType::Physical => phys_amp * phys_m,
                    DType::Magic => mag_amp * mag_m,
                    DType::True => 1.0,
                };
                total += raw * m;
            }
        }

        total
    }

    /// Smallest charge step `k in 0..=q_max_steps` whose burst starting at
    /// `t_start` kills the target, or `-1` if even full charge does not kill.
    fn earliest_lethal_step(&self, e: &Engine, t_start: f64) -> i64 {
        let hp_need = e.st.hp * (1.0 + 1e-9);
        if self.est_q_burst(e, t_start, self.q_max_steps) < hp_need {
            return -1;
        }
        let mut k = 0;
        while k < self.q_max_steps {
            if self.est_q_burst(e, t_start, k) >= hp_need {
                return k;
            }
            k += 1;
        }
        self.q_max_steps
    }

    /// Chooses the optimal discrete Piercing Arrow charge step `k in 0..=10`
    /// when `cast_q` fires at `e.st.t`.
    fn choose_q_steps(&self, e: &Engine) -> i64 {
        let t = e.st.t;
        let cur_as = self.est_cur_as(e, t);
        let period = 1.0 / cur_as;

        // 1. Lethal check: never overcharge past the killing step, and prefer
        // a shorter charge + immediate follow-up attack if that kills earlier.
        let k_kill = self.earliest_lethal_step(e, t);
        if k_kill == 0 {
            return 0;
        }
        if k_kill > 0 {
            let mut best_k = k_kill;
            let mut best_finish = t + (k_kill as f64) * self.q_step_s;
            let mut k = 0;
            while k < k_kill {
                let c = (k as f64) * self.q_step_s;
                let b = self.est_q_burst(e, t, k);
                let rem = pymax(e.st.hp - b, 0.0);
                let t_atk = pymax(e.st.next_attack, t + c + ABILITY_LOCKOUT_S);
                let swing = self.est_auto_swing(e, t_atk, rem, false);
                if swing >= rem * (1.0 + 1e-9) && t_atk < best_finish - 1e-9 {
                    best_finish = t_atk;
                    best_k = k;
                }
                k += 1;
            }
            return best_k;
        }

        // 2. Sustained / non-lethal tradeoff across k in 0..=q_max_steps.
        let b0 = self.est_q_burst(e, t, 0);
        let hp_post = pymax(e.st.hp - b0, 0.0);
        let oh_mult = self.onhit_phantom_mult(e);
        let mut swing_val = self.est_auto_swing(e, t, hp_post, true);

        let q_full = e.basic_cd(self.q_cd_base);
        let e_full = e.basic_cd(self.e_cd_base);
        let st_0 = self.stacks_at(t) as f64;
        let q_ready_0 = pymax(t, t + q_full * (1.0 - self.w_cdr_per_stack * st_0));
        let stack_s = if self.ranks.w > 0 {
            (self.w_max_stacks as f64) / (cur_as * oh_mult)
        } else {
            0.0
        };
        let atk_base = pymax(e.st.next_attack, t + ABILITY_LOCKOUT_S);
        let next_q_0 = pymax(q_ready_0, atk_base + pymax(0.0, stack_s - period));

        let phys_m = self.est_phys_mult(e, t, 0);
        let mag_m = self.est_mag_mult(e, t, 0);
        let det_per_stack = self.w_hp_pct
            * e.target_hp
            * self.est_amp(e, DType::Magic, t, false, 1, hp_post)
            * mag_m;
        let q_tap_cycle_val = self.q_dmg
            * self.est_amp(e, DType::Physical, t, true, 0, hp_post)
            * phys_m
            + (self.w_max_stacks as f64) * det_per_stack;
        let q_cycle_rate = q_tap_cycle_val / pymax(next_q_0 - t, 1.0);

        let e_cycle_val = if self.ranks.e > 0 {
            self.e_dmg * self.est_amp(e, DType::Physical, t, true, 0, hp_post) * phys_m
                + (self.w_max_stacks as f64) * det_per_stack
        } else {
            0.0
        };
        let e_cycle_rate = if self.ranks.e > 0 && e_full > 0.0 {
            e_cycle_val / e_full
        } else {
            0.0
        };

        let w_full = e.basic_cd(self.w_active_cd_base);
        let w_cycle_val = if self.ranks.w > 0 {
            self.w_active_pct
                * (0.5 * e.target_hp)
                * self.est_amp(e, DType::Magic, t, true, 1, hp_post)
                * mag_m
        } else {
            0.0
        };
        let w_cycle_rate = if self.ranks.w > 0 && w_full > 0.0 {
            w_cycle_val / w_full
        } else {
            0.0
        };

        // Navori Flickerblade: each delayed attack forfeits 15% remaining
        // cooldown shave across Q, W active, and E, and the attacks that
        // rebuild Blight stacks already compress remaining cooldowns.
        let mut nav_keep = 1.0;
        if let Some(nav_pct) = e.p.fx.s.navori_cdr {
            let nav_frac = nav_pct / 100.0;
            let mut i = 0;
            while i < self.w_max_stacks {
                nav_keep *= 1.0 - nav_frac;
                i += 1;
            }
            let rem_q = pymax(0.0, q_ready_0 - atk_base);
            let rem_e = if self.ranks.e > 0 { pymax(0.0, self.s.e_ready - atk_base) } else { 0.0 };
            let w_ready_0 = if self.ranks.w > 0 {
                let base_w = if t >= self.s.w_active_ready {
                    t + w_full
                } else {
                    self.s.w_active_ready
                };
                pymax(t, base_w - w_full * self.w_cdr_per_stack * st_0)
            } else {
                0.0
            };
            let rem_w = if self.ranks.w > 0 { pymax(0.0, w_ready_0 - atk_base) } else { 0.0 };
            swing_val += nav_frac
                * (rem_q * q_cycle_rate + rem_e * e_cycle_rate + rem_w * w_cycle_rate);
        }

        let e_wait_0 = if self.ranks.e > 0 {
            pymax(0.0, self.s.e_ready - t - e_full * self.w_cdr_per_stack * st_0)
        } else {
            INF
        };
        let q_shaved_0 = atk_base + pymax(0.0, q_ready_0 - atk_base) * nav_keep;
        let next_q_eff_0 = pymax(q_shaved_0, atk_base + pymax(0.0, stack_s - period));
        let e_shaved_0 = atk_base + pymax(0.0, t + e_wait_0 - atk_base) * nav_keep;
        let next_e_0 = pymax(e_shaved_0, atk_base + pymax(0.0, stack_s - period));

        let mut best_k = 0;
        let mut best_score = -INF;
        let mut k = 0;
        while k <= self.q_max_steps {
            let c = (k as f64) * self.q_step_s;
            let mult = 1.0 + (k as f64) * self.q_step_amp;
            let b_k = if k == 0 { b0 } else { self.est_q_burst(e, t, k) };

            let atk_k = pymax(e.st.next_attack, t + c + ABILITY_LOCKOUT_S);
            let atk_delay = atk_k - atk_base;
            let auto_loss = atk_delay * cur_as * swing_val;

            let st_k = if k == 0 {
                st_0
            } else {
                self.stacks_after_r(t, t + c, true) as f64
            };
            let q_ready_k = pymax(t + c, t + q_full * (1.0 - self.w_cdr_per_stack * mult * st_k));
            let q_shaved_k = atk_k + pymax(0.0, q_ready_k - atk_k) * nav_keep;
            let next_q_k = pymax(q_shaved_k, atk_k + pymax(0.0, stack_s - period));
            let q_cadence_gain = (next_q_eff_0 - next_q_k) * q_cycle_rate;

            let e_ready_k = if self.ranks.e > 0 {
                pymax(t + c, self.s.e_ready - e_full * self.w_cdr_per_stack * mult * st_k)
            } else {
                INF
            };
            let e_shaved_k = atk_k + pymax(0.0, e_ready_k - atk_k) * nav_keep;
            let next_e_k = pymax(e_shaved_k, atk_k + pymax(0.0, stack_s - period));
            let e_cadence_gain = if self.ranks.e > 0 {
                (next_e_0 - next_e_k) * e_cycle_rate
            } else {
                0.0
            };

            let score = b_k + q_cadence_gain + e_cadence_gain - auto_loss;
            if score > best_score + 1e-9 {
                best_score = score;
                best_k = k;
            }
            k += 1;
        }
        best_k
    }
}

impl Driver for GenDriver {
    fn new(
        kit: &Kit,
        sheet: &Sheet,
        level: i64,
        ranks: Ranks,
        _prestacked: bool,
    ) -> Result<Self, String> {
        let q_charge_s = kit.num("gen.Q.chargeDurationS")?;
        let q_step_s = kit.num("gen.Q.chargeStepS")?;
        let q_max_steps = (q_charge_s / q_step_s + 0.5) as i64;

        let w_base_pct = if ranks.w > 0 {
            kit.at_rank("gen.W.detonation.baseHpPct", ranks.w)?
        } else {
            kit.num("gen.W.detonation.rank0BaseHpPct")?
        };
        let w_hp_pct = w_base_pct + kit.num("gen.W.detonation.apCoefPerAp")? * sheet.ap;

        let state = State {
            blight_stacks: 0,
            blight_expire: 0.0,
            busy_until: 0.0,
            q_release_at: INF,
            q_charge_cur: 0.0,
            q_mult_cur: 1.0,
            w_active_ready: 0.0,
            e_ready: 0.0,
            e_land_at: INF,
            e_deton2_at: INF,
            r_ready: INF,
            r_stack_at: INF,
            r_stacks_left: 0,
        };
        Ok(GenDriver {
            ranks,
            level,
            attack_range: sheet.base_attack_range,

            q_cd_base: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_step_s,
            q_step_amp: kit.num("gen.Q.chargeStepAmp")?,
            q_max_steps,

            w_onhit_dmg: kit.hit("gen.W.onhit", ranks.w, sheet)?,
            w_hp_pct,
            w_max_stacks: kit.num("gen.W.maxStacks")? as i64,
            w_stack_dur: kit.num("gen.W.stackDurationS")?,
            w_cdr_per_stack: kit.num("gen.W.cdrPerStack")?,
            w_active_pct: kit.at_rank("gen.W.active.missingHpPct", ranks.w)?,
            w_active_cd_base: kit.at_rank("abilities.W.cooldownS", ranks.w)?,

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd_base: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_land_delay: kit.num("gen.E.landDelayS")?,
            e_deton2_delay: kit.num("gen.E.secondDetonDelayS")?,

            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd_base: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_first_stack_delay: kit.num("gen.R.firstStackDelayS")?,
            r_stack_interval: kit.num("gen.R.stackIntervalS")?,
            r_stacks_total: kit.num("gen.R.stacksApplied")? as i64,

            src_w_onhit: intern("W onhit"),
            src_w_deton_q: intern("W deton Q"),
            src_w_deton_e: intern("W deton E"),
            src_w_deton_r: intern("W deton R"),
            src_w_active: intern("W active"),

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
        shave(&mut self.s.e_ready, t, factor);
        shave(&mut self.s.w_active_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.ranks.w > 0 {
            self.add_blight(e.st.t, 1);
            e.deal(self.w_onhit_dmg, DType::Magic, self.src_w_onhit, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.q_release_at != INF {
            return INF;
        }
        let earliest = self.castable_at(e, e.st.q_ready);
        let can_stack = self.ranks.w > 0 || self.s.r_stacks_left > 0;
        if !can_stack {
            return earliest;
        }

        let stacks = self.stacks_at(earliest);
        let e_in_flight = self.s.e_land_at != INF;
        let e_ready_first = self.ranks.e > 0
            && !e_in_flight
            && self.castable_at(e, self.s.e_ready) <= earliest;

        if stacks >= self.w_max_stacks && !e_in_flight && !e_ready_first {
            return earliest;
        }

        // Below 3 stacks (or while E is ready/in-flight), only cast Q now if Q
        // executes the target before the next stack/impact event arrives.
        let k_kill = self.earliest_lethal_step(e, earliest);
        if k_kill >= 0 {
            let t_q_kill = earliest + (k_kill as f64) * self.q_step_s;
            let mut rival_t = pymin(e.st.next_attack, self.s.r_stack_at);
            if e_in_flight {
                rival_t = pymin(rival_t, self.s.e_land_at);
            } else if e_ready_first {
                rival_t = pymin(rival_t, self.castable_at(e, self.s.e_ready) + self.e_land_delay);
            }
            if t_q_kill <= rival_t + 1e-9 {
                return earliest;
            }
        }
        INF
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let k = self.choose_q_steps(e);
        let charge_s = (k as f64) * self.q_step_s;
        let mult = 1.0 + (k as f64) * self.q_step_amp;
        if k <= 0 {
            self.fire_q(e, 0.0, 1.0);
        } else {
            self.s.q_release_at = t + charge_s;
            self.s.q_charge_cur = charge_s;
            self.s.q_mult_cur = mult;
            self.busy_for(e, charge_s);
        }
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r > 0 {
            self.fire_r(e);
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_stack_at != INF {
            out[n] = (self.s.r_stack_at, Kind::Ev(EV_R_STACK));
            n += 1;
        }
        if self.s.q_release_at != INF {
            out[n] = (self.s.q_release_at, Kind::Ev(EV_Q_RELEASE));
            n += 1;
        }
        if self.s.e_land_at != INF {
            out[n] = (self.s.e_land_at, Kind::Ev(EV_E_LAND));
            n += 1;
        }
        if self.s.e_deton2_at != INF {
            out[n] = (self.s.e_deton2_at, Kind::Ev(EV_E_DETON2));
            n += 1;
        }
        if self.ranks.r > 0 && self.s.r_ready != INF && self.s.q_release_at == INF {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        if self.ranks.e > 0 && self.s.q_release_at == INF && self.s.e_land_at == INF {
            let earliest = self.castable_at(e, self.s.e_ready);
            let can_stack = self.ranks.w > 0 || self.s.r_stacks_left > 0;
            let ready_for_stacks = !can_stack || self.stacks_at(earliest) >= self.w_max_stacks;
            let lethal_e = !ready_for_stacks
                && self.est_e_burst(e, earliest) >= e.st.hp * (1.0 + 1e-9)
                && earliest + self.e_land_delay
                    <= pymin(e.st.next_attack, self.s.r_stack_at) + 1e-9;
            if ready_for_stacks || lethal_e {
                out[n] = (earliest, Kind::Ev(EV_E_CAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_STACK) => {
                self.add_blight(t, 1);
                self.s.r_stacks_left -= 1;
                self.s.r_stack_at = if self.s.r_stacks_left > 0 {
                    t + self.r_stack_interval
                } else {
                    INF
                };
            }
            Kind::Ev(EV_Q_RELEASE) => {
                let charge_s = self.s.q_charge_cur;
                let mult = self.s.q_mult_cur;
                self.s.q_release_at = INF;
                self.s.q_charge_cur = 0.0;
                self.s.q_mult_cur = 1.0;
                self.fire_q(e, charge_s, mult);
            }
            Kind::Ev(EV_E_LAND) => {
                self.s.e_land_at = INF;
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                self.detonate(e, 1.0, self.src_w_deton_e);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.e_deton2_at = t + self.e_deton2_delay;
            }
            Kind::Ev(EV_E_DETON2) => {
                self.s.e_deton2_at = INF;
                self.detonate(e, 1.0, self.src_w_deton_e);
            }
            Kind::Ev(EV_R_CAST) => {
                if self.castable_at(e, self.s.r_ready) > t || self.s.q_release_at != INF {
                    return;
                }
                self.fire_r(e);
            }
            Kind::Ev(EV_E_CAST) => {
                if self.castable_at(e, self.s.e_ready) > t || self.s.q_release_at != INF {
                    return;
                }
                self.s.e_ready = t + e.basic_cd(self.e_cd_base);
                self.s.e_land_at = t + self.e_land_delay;
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
