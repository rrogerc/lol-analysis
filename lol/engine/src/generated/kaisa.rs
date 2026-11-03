//! Kai'Sa (Patch 16.21). Living Weapon evolutions resolve dynamically from
//! permanent stats; Plasma stacks ride basic attacks and Void Seeker with
//! exact pre-attack and rollover missing-health detonations; Icathian Rain
//! fires 6 or 12 discrete physical missiles over 1.0 s without interrupting
//! attacks; Void Seeker and Supercharge weave into basic-attack backswings;
//! Killer Instinct resets the basic-attack timer once Plasma marks the target.

use crate::fight::{shave, Driver, Engine, Events, Kind, St, S_COMBAT_T0};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Event ordering on exact timestamp ties: pending Q missiles resolve first,
/// then completing Void Seeker bolts, then Void Seeker casts, then Supercharge.
const EV_Q_V0: u8 = 0;
const EV_Q_V1: u8 = 1;
const EV_W_LAND: u8 = 2;
const EV_W_CAST: u8 = 3;
const EV_E_CAST: u8 = 4;

#[derive(Clone, Copy, Debug, PartialEq)]
struct Volley {
    start_at: f64,
    idx: i64,
    proced: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    level: i64,
    attack_range: f64,
    windup_fraction: f64,
    e_windup_fraction: f64,

    // Passive: Second Skin / Plasma / Caustic Wounds.
    p_base_dmg: f64,
    p_perstack_dmg: f64,
    p_max_stacks: i64,
    p_duration: f64,
    p_execute_pct: f64,

    // Q: Icathian Rain (6 missiles unevolved, 12 evolved).
    q_first_dmg: f64,
    q_sub_dmg: f64,
    q_missiles: i64,
    q_cd: f64,
    q_missile_delay: f64,
    q_volley_dur: f64,
    q_missile_step: f64,

    // W: Void Seeker (2 Plasma stacks unevolved, 3 + 75% CD refund evolved).
    w_dmg: f64,
    w_cd: f64,
    w_cast_time: f64,
    w_stacks: i64,
    w_cd_mult: f64,

    // E: Supercharge.
    e_as_pct: f64,
    e_buff_dur: f64,
    e_cdr_per_attack: f64,
    e_cd: f64,
    e_cast_base: f64,
    e_cast_floor: f64,

    // R: Killer Instinct.
    r_cd: f64,
    r_plasma_window: f64,

    src_p_cw: SourceId,
    src_p_exec: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    busy_until: f64,
    p_stacks: i64,
    p_expire_at: f64,
    p_active_until: f64,
    p_done_on_swing: bool,
    q_v0: Volley,
    q_v1: Volley,
    w_ready: f64,
    w_resolve_at: f64,
    e_ready: f64,
    e_buff_start: f64,
    e_buff_until: f64,
    r_ready: f64,
}

impl GenDriver {
    fn castable_at(&self, e: &Engine, ready: f64) -> f64 {
        pymax(pymax(pymax(ready, e.st.t), self.s.busy_until), e.hold_until())
    }

    fn eff_next_attack(&self, e: &Engine) -> f64 {
        pymax(e.st.next_attack, e.hold_until())
    }

    fn busy_for(&mut self, e: &mut Engine, cast_s: f64) {
        self.s.busy_until = e.st.t + cast_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
    }

    fn stacks_at(&self, t: f64) -> i64 {
        if self.s.p_stacks <= 0 || t >= self.s.p_expire_at {
            0
        } else {
            self.s.p_stacks
        }
    }

    fn e_buff_active(&self, t: f64) -> bool {
        t >= self.s.e_buff_start && t < self.s.e_buff_until
    }

    /// Applies 1 Plasma stack, dealing Caustic Wounds magic damage for the
    /// stacks already on the target. Returns true when this application is
    /// the 5th stack that consumes all stacks and triggers detonation.
    fn apply_cw_stack(&mut self, e: &mut Engine) -> bool {
        let t = e.st.t;
        let stacks_before = self.stacks_at(t);
        let dmg = self.p_base_dmg + self.p_perstack_dmg * (stacks_before as f64);
        let tot0 = e.st.total;
        e.deal(dmg, DType::Magic, self.src_p_cw, false, false, 1.0);
        if dmg > 0.0 && e.st.total <= tot0 {
            return false;
        }
        self.s.p_active_until = t + self.r_plasma_window;
        if stacks_before >= self.p_max_stacks {
            self.s.p_stacks = 0;
            self.s.p_expire_at = 0.0;
            true
        } else {
            self.s.p_stacks = stacks_before + 1;
            self.s.p_expire_at = t + self.p_duration;
            false
        }
    }

    fn detonate_plasma(&self, e: &mut Engine) {
        let missing = e.target_hp - pymax(e.st.hp, 0.0);
        e.deal(self.p_execute_pct * missing, DType::Magic, self.src_p_exec, false, false, 1.0);
    }

    fn volley_next_at(&self, v: Volley) -> f64 {
        if v.idx >= self.q_missiles || v.start_at >= INF {
            INF
        } else if v.idx == 0 {
            v.start_at
        } else if v.idx == self.q_missiles - 1 {
            v.start_at + self.q_volley_dur
        } else {
            v.start_at + (v.idx as f64) * self.q_missile_step
        }
    }

    fn step_volley(&mut self, e: &mut Engine, slot: u8) {
        let mut v = if slot == 0 { self.s.q_v0 } else { self.s.q_v1 };
        if v.idx >= self.q_missiles || v.start_at >= INF {
            return;
        }
        let dmg = if v.idx == 0 { self.q_first_dmg } else { self.q_sub_dmg };
        if !v.proced {
            let tot0 = e.st.total;
            e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
            if e.st.total > tot0 {
                v.proced = true;
                e.ability_cast_proc();
                e.eclipse_hit();
            }
        } else {
            // Subsequent missiles apply persistent area ability damage (refreshing
            // burns and stacking Black Cleaver per hit) without adding extra
            // Spear of Shojin stacks beyond the cast's single stack.
            let sh = e.st.shojin;
            e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
            e.st.shojin = sh;
        }
        v.idx += 1;
        if v.idx >= self.q_missiles {
            v.start_at = INF;
        }
        if slot == 0 {
            self.s.q_v0 = v;
        } else {
            self.s.q_v1 = v;
        }
    }

    fn cur_bonus_as_no_e(&self, e: &Engine, t: f64) -> f64 {
        let s = &e.p.fx.s;
        let mut bonus = e.p.sheet.bonus_as_pct;
        if let Some(a) = &s.as_stacking {
            bonus += (e.st.seething as f64) * a.pct_per_stack;
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
        bonus
    }

    fn as_from_bonus(&self, e: &Engine, total_bonus_pct: f64) -> f64 {
        let sheet = e.p.sheet;
        let raw = sheet.base_as + sheet.as_ratio * total_bonus_pct / 100.0;
        if let Some(d) = &e.dfd {
            if d.as_mult != 1.0 {
                return pymin(raw * d.as_mult, AS_CAP);
            }
        }
        pymin(raw, AS_CAP)
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
            if t < e.st.hz_until {
                amp *= 1.0 + h.amp_pct / 100.0;
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

    fn est_volley_dmg_until(
        &self,
        e: &Engine,
        v: Volley,
        t_horizon: f64,
        hp_est: f64,
        cleaver_add: &mut i64,
    ) -> f64 {
        if v.idx >= self.q_missiles || v.start_at >= INF {
            return 0.0;
        }
        let mut total = 0.0;
        let mut i = v.idx;
        while i < self.q_missiles {
            let t_m = if i == 0 {
                v.start_at
            } else if i == self.q_missiles - 1 {
                v.start_at + self.q_volley_dur
            } else {
                v.start_at + (i as f64) * self.q_missile_step
            };
            if t_m > t_horizon + 1e-9 {
                break;
            }
            let raw = if i == 0 { self.q_first_dmg } else { self.q_sub_dmg };
            let sh_add = if !v.proced && i > v.idx { 1 } else { 0 };
            let hit = raw
                * self.est_amp(e, DType::Physical, t_m, true, sh_add, hp_est)
                * self.est_phys_mult(e, t_m, *cleaver_add);
            total += hit;
            *cleaver_add += 1;
            i += 1;
        }
        total
    }

    /// Estimates Void Seeker's total damage (bolt + successive Plasma stacks +
    /// detonation if triggered) when cast at `t_cast` (resolving at `t_land`).
    fn est_w_burst(&self, e: &Engine, t_cast: f64, stacks_in: i64, hp_in: f64) -> (f64, i64) {
        let t_land = t_cast + self.w_cast_time;
        let mut hp = pymax(hp_in, 0.0);
        let mut total = 0.0;
        let mut stacks = stacks_in;

        let w_hit = self.w_dmg
            * self.est_amp(e, DType::Magic, t_land, true, 0, hp)
            * self.est_mag_mult(e, t_land, 0);
        hp = pymax(hp - w_hit, 0.0);
        total += w_hit;

        let mag_m_after = self.est_mag_mult(e, t_land, 1);
        let mut k = 0;
        while k < self.w_stacks {
            let cw_raw = self.p_base_dmg + self.p_perstack_dmg * (stacks as f64);
            let cw_hit = cw_raw * self.est_amp(e, DType::Magic, t_land, false, 1, hp) * mag_m_after;
            hp = pymax(hp - cw_hit, 0.0);
            total += cw_hit;
            if stacks >= self.p_max_stacks {
                stacks = 0;
                let exec_raw = self.p_execute_pct * (e.target_hp - hp);
                let exec_hit = exec_raw
                    * self.est_amp(e, DType::Magic, t_land, false, 1, hp)
                    * mag_m_after;
                hp = pymax(hp - exec_hit, 0.0);
                total += exec_hit;
            } else {
                stacks += 1;
            }
            k += 1;
        }
        (total, stacks)
    }

    /// Simulates `n_swings` basic attacks starting at `t_first_swing` spaced by
    /// `swing_period` (plus in-flight Q missiles and any pending or castable W
    /// resolving by `t_horizon`) against a non-defended target to check whether
    /// that sequence already executes the target.
    #[allow(clippy::too_many_arguments)]
    fn sim_attacks_kill(
        &self,
        e: &Engine,
        t_now: f64,
        t_horizon: f64,
        t_first_swing: f64,
        swing_period: f64,
        n_swings: i64,
        allow_ready_w: bool,
    ) -> bool {
        if e.dfd.is_some() {
            return false;
        }
        let exec_hp = match e.p.fx.s.execute_pct {
            Some(p) => p / 100.0 * e.target_hp,
            None => 0.0,
        };
        let mut hp = pymax(e.st.hp, 0.0);
        let mut stacks = self.stacks_at(t_now);

        let (w_cast, mut w_land) = if self.s.w_resolve_at < INF {
            if self.s.w_resolve_at <= t_horizon + 1e-9 {
                (self.s.w_resolve_at - self.w_cast_time, self.s.w_resolve_at)
            } else {
                (INF, INF)
            }
        } else if allow_ready_w && self.ranks.w > 0 {
            let earliest_w = pymax(pymax(self.s.w_ready, t_now), self.s.busy_until);
            let tc = if n_swings > 0
                && earliest_w < t_first_swing
                && t_first_swing < earliest_w + self.w_cast_time - 1e-9
            {
                t_first_swing
            } else {
                earliest_w
            };
            let tl = tc + self.w_cast_time;
            if tl <= t_horizon + 1e-9 {
                (tc, tl)
            } else if earliest_w + self.w_cast_time <= t_horizon + 1e-9 {
                (earliest_w, earliest_w + self.w_cast_time)
            } else {
                (INF, INF)
            }
        } else {
            (INF, INF)
        };

        let mut cleaver_add = 0;
        let mut cleaver_all = 0;
        let q_all = self.est_volley_dmg_until(e, self.s.q_v0, t_horizon, hp, &mut cleaver_all)
            + self.est_volley_dmg_until(e, self.s.q_v1, t_horizon, hp, &mut cleaver_all);
        let (q_pre, mut q_post) = if w_land < INF {
            let qp = self.est_volley_dmg_until(e, self.s.q_v0, w_land, hp, &mut cleaver_add)
                + self.est_volley_dmg_until(e, self.s.q_v1, w_land, hp, &mut cleaver_add);
            (qp, pymax(0.0, q_all - qp))
        } else {
            cleaver_add = cleaver_all;
            (q_all, 0.0)
        };
        hp = pymax(hp - q_pre, 0.0);

        if n_swings <= 0 && w_land < INF {
            let (w_d, st_after) = self.est_w_burst(e, w_cast, stacks, hp);
            hp = pymax(hp - w_d - q_post, 0.0);
            stacks = st_after;
            q_post = 0.0;
            w_land = INF;
        }
        if hp <= exec_hp + 1e-9 {
            return true;
        }

        let sheet = e.p.sheet;
        let fx = e.p.fx;
        let s = &fx.s;
        let crit_c = sheet.crit_chance / 100.0;
        let base_crit_ev = 1.0 + crit_c * (sheet.crit_damage / 100.0 - 1.0);
        let mut auto_amp = 1.0;
        if let Some(aa) = &s.attack_amp {
            auto_amp += aa.max_pct / 100.0 * pymin(1.0, self.attack_range / aa.max_at_range);
        }
        let mut sundered_used = e.st.sundered_used;
        let mut kraken = e.st.kraken;
        let mut phantom = e.st.phantom;
        let mut seething = e.st.seething;
        let mut sb_primed = e.st.sb_primed;

        let mut t_swing = t_first_swing;
        let mut i = 0;
        while i < n_swings {
            if t_swing > t_horizon + 1e-9 {
                break;
            }
            if w_land < t_swing - 1e-9 {
                let (w_d, st_after) = self.est_w_burst(e, w_cast, stacks, hp);
                hp = pymax(hp - w_d - q_post, 0.0);
                stacks = st_after;
                q_post = 0.0;
                cleaver_add = imax(cleaver_add, cleaver_all);
                if s.spellblade.is_some() && w_cast >= e.st.sb_icd_until - 1e-9 {
                    sb_primed = true;
                }
                w_land = INF;
                if hp <= exec_hp + 1e-9 {
                    return true;
                }
            }

            let phys_m = self.est_phys_mult(e, t_swing, cleaver_add);
            let mag_m = self.est_mag_mult(e, t_swing, 0);
            let phys_amp = self.est_amp(e, DType::Physical, t_swing, false, 0, hp);
            let mag_amp = self.est_amp(e, DType::Magic, t_swing, false, 0, hp);

            let mut phantom_now = false;
            if let Some(p) = &s.phantom {
                if phantom >= p.stacks_needed {
                    phantom_now = true;
                    phantom = 0;
                }
            }
            let mut kraken_now = false;
            if s.kraken.is_some() {
                if kraken >= 2 {
                    kraken_now = true;
                    kraken = 0;
                } else {
                    kraken += 1;
                }
            }
            if let Some(a) = &s.as_stacking {
                seething = imin(seething + 1, a.max_stacks);
                if let Some(p) = &s.phantom {
                    if !phantom_now && seething == a.max_stacks {
                        phantom = imin(phantom + 1, p.stacks_needed);
                    }
                }
            }

            let mut p_done = false;
            if stacks >= self.p_max_stacks {
                let cw4 = (self.p_base_dmg + self.p_perstack_dmg * (stacks as f64)) * mag_amp * mag_m;
                hp = pymax(hp - cw4, 0.0);
                stacks = 0;
                let exec_d = self.p_execute_pct * (e.target_hp - hp) * mag_amp * mag_m;
                hp = pymax(hp - exec_d, 0.0);
                p_done = true;
            }

            let mut crit_ev = base_crit_ev;
            if let Some(sun) = s.first_attack_crit_floor_ev {
                if !sundered_used {
                    sundered_used = true;
                    if crit_ev < sun {
                        crit_ev = sun;
                    }
                }
            }
            let auto_d = e.p.ad * auto_amp * crit_ev * phys_amp * phys_m;
            hp = pymax(hp - auto_d, 0.0);
            cleaver_add += 1;

            let mut oh_passes = if phantom_now { 2 } else { 1 };
            let mut sb_reapply = false;
            if sb_primed {
                sb_primed = false;
                if let Some(sb) = &s.spellblade {
                    let raw = sb.base_ad_ratio * sheet.ad_base
                        + sb.ap_ratio * sheet.ap
                        + sb.per_crit_chance_pct * sheet.crit_chance;
                    let m = match sb.dtype {
                        DType::Physical => phys_amp * phys_m,
                        DType::Magic => mag_amp * mag_m,
                        DType::True => 1.0,
                    };
                    hp = pymax(hp - raw * m, 0.0);
                    sb_reapply = sb.reapply_onhit;
                }
            }
            if sb_reapply {
                oh_passes += 1;
            }

            let mut pass = 0;
            while pass < oh_passes {
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
                    hp = pymax(hp - raw * m, 0.0);
                }
                if pass == 0 && p_done {
                    p_done = false;
                } else {
                    let cw = (self.p_base_dmg + self.p_perstack_dmg * (stacks as f64))
                        * mag_amp
                        * mag_m;
                    hp = pymax(hp - cw, 0.0);
                    if stacks >= self.p_max_stacks {
                        stacks = 0;
                        let exec_d = self.p_execute_pct * (e.target_hp - hp) * mag_amp * mag_m;
                        hp = pymax(hp - exec_d, 0.0);
                    } else {
                        stacks += 1;
                    }
                }
                for oh in &fx.onhit_current_hp {
                    let raw = oh.ranged_pct / 100.0 * hp;
                    let m = match oh.dtype {
                        DType::Physical => phys_amp * phys_m,
                        DType::Magic => mag_amp * mag_m,
                        DType::True => 1.0,
                    };
                    hp = pymax(hp - raw * m, 0.0);
                }
                pass += 1;
            }

            if kraken_now {
                if let Some(kr) = &s.kraken {
                    let base = kr.base_by_level.at(self.level) * kr.ranged_mult;
                    let miss = 1.0 - hp / e.target_hp;
                    let raw = base * (1.0 + kr.missing_ranged / 100.0 * miss);
                    let m = match kr.dtype {
                        DType::Physical => phys_amp * phys_m,
                        DType::Magic => mag_amp * mag_m,
                        DType::True => 1.0,
                    };
                    hp = pymax(hp - raw * m, 0.0);
                }
            }

            if hp <= exec_hp + 1e-9 {
                return true;
            }
            let prev_swing = t_swing;
            t_swing += swing_period;
            if w_land < INF && w_cast <= prev_swing + 1e-9 {
                t_swing = pymax(t_swing, w_land);
            }
            i += 1;
        }
        if w_land < INF {
            let (w_d, _) = self.est_w_burst(e, w_cast, stacks, hp);
            hp = pymax(hp - w_d, 0.0);
        }
        hp = pymax(hp - q_post, 0.0);
        let _ = stacks;
        hp <= exec_hp + 1e-9
    }

    fn w_cast_at(&self, e: &Engine) -> f64 {
        let earliest = self.castable_at(e, self.s.w_ready);
        let next_atk = self.eff_next_attack(e);
        if next_atk < INF
            && earliest < next_atk
            && next_atk < earliest + self.w_cast_time - 1e-9
        {
            // An attack is imminent inside W's 0.4 s cast window: let the auto
            // land first so W fits cleanly into the next backswing for zero
            // lost attack time, unless Q + W alone executes and 1 auto would not.
            let w_solo_executes = self.sim_attacks_kill(
                e,
                earliest,
                earliest + self.w_cast_time,
                INF,
                1.0,
                0,
                true,
            ) && !self.sim_attacks_kill(
                e,
                earliest,
                next_atk,
                next_atk,
                1.0,
                1,
                false,
            );
            if !w_solo_executes {
                return next_atk;
            }
        }
        earliest
    }

    fn e_cast_at(&self, e: &Engine) -> f64 {
        let next_atk = self.eff_next_attack(e);
        if next_atk >= INF {
            return INF;
        }
        let earliest = self.castable_at(e, self.s.e_ready);
        let b_no_e = self.cur_bonus_as_no_e(e, earliest);
        let b_cur = b_no_e + self.bonus_as(earliest);
        let cast_s = pymax(self.e_cast_base / (1.0 + b_cur / 100.0), self.e_cast_floor);
        let a0 = self.as_from_bonus(e, b_no_e);
        let a1 = self.as_from_bonus(e, b_no_e + self.e_as_pct);
        let period0 = 1.0 / a0;

        // Align E into an attack backswing: if E comes ready mid-swing where
        // charging now would forfeit elapsed swing progress, wait for the auto.
        let lost_now = pymax(0.0, earliest + cast_s - next_atk);
        let lost_after = pymax(0.0, cast_s - period0);
        let (t_cand, lost_s) = if earliest < next_atk && lost_now > lost_after + 1e-9 {
            (next_atk, lost_after)
        } else {
            (earliest, lost_now)
        };

        // 1. AS-cap / net-attack check: if the 4 s buff does not gain more
        // attacks than the charge disarm forfeits, skip E.
        let gained_atks = self.e_buff_dur * (a1 - a0);
        let lost_atks = lost_s * a0;
        if gained_atks <= lost_atks + 1e-9 {
            return INF;
        }

        // 2. Short-horizon lethal check: if charge disarm delays the next auto
        // (`lost_s > 0`), only charge E when the target survives past the
        // attack count where E's post-charge AS buff catches up.
        if lost_s > 1e-9 && e.dfd.is_none() {
            let m_raw = (lost_s * a0 * a1 / (a1 - a0) + 1.0 - 1e-9) as i64;
            let m_breakeven = imin(imax(1, m_raw), 10 + 10);
            let t_first_no_e = if t_cand > earliest {
                t_cand
            } else {
                next_atk
            };
            let n_total = if t_cand > earliest {
                m_breakeven + 1
            } else {
                m_breakeven
            };
            let t_horizon = t_first_no_e + ((n_total - 1) as f64) * period0;
            if self.sim_attacks_kill(
                e,
                e.st.t,
                t_horizon,
                t_first_no_e,
                period0,
                n_total,
                true,
            ) {
                return INF;
            }
        }

        t_cand
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
        let base_ad_lvl1 = kit.num("gen.P.baseAdLevel1")?;
        let q_evo_ad = kit.num("gen.Q.evolveBonusAd")?;
        let q_evolved = (sheet.ad - base_ad_lvl1) + 1e-9 >= q_evo_ad;

        // Living Weapon W evolution counts permanent item AP (including
        // Rabadon's permanent +30% AP multiplier, while excluding Blackfire
        // Torch's temporary +4% in-combat burn AP).
        let perm_ap = if (sheet.ap_mult - 1.0) * 10.0 >= 1.0 {
            sheet.ap_flat * (1.0 + 3.0 / 10.0)
        } else {
            sheet.ap_flat
        };
        let w_evo_ap = kit.num("gen.W.evolveAp")?;
        let w_evolved = perm_ap + 1e-9 >= w_evo_ap;

        let q_missiles = (if q_evolved {
            kit.num("gen.Q.missilesEvolved")?
        } else {
            kit.num("gen.Q.missiles")?
        }) as i64;
        let q_first_dmg = kit.hit("gen.Q.missileDamage", ranks.q, sheet)?;
        let q_sub_dmg = q_first_dmg * kit.num("gen.Q.extraHitMult")?;
        let q_volley_dur = kit.num("gen.Q.volleyDurationS")?;
        let q_missile_step = if q_missiles > 1 {
            q_volley_dur / ((q_missiles - 1) as f64)
        } else {
            0.0
        };

        let w_stacks = (if w_evolved {
            kit.num("gen.P.voidSeekerStacksEvolved")?
        } else {
            kit.num("gen.P.voidSeekerStacks")?
        }) as i64;
        let w_cd_mult = if w_evolved {
            1.0 - kit.num("gen.W.cooldownRefundPct")? / 100.0
        } else {
            1.0
        };

        let empty_volley = Volley {
            start_at: INF,
            idx: q_missiles,
            proced: false,
        };
        let state = State {
            busy_until: 0.0,
            p_stacks: 0,
            p_expire_at: 0.0,
            p_active_until: 0.0,
            p_done_on_swing: false,
            q_v0: empty_volley,
            q_v1: empty_volley,
            w_ready: 0.0,
            w_resolve_at: INF,
            e_ready: 0.0,
            e_buff_start: INF,
            e_buff_until: 0.0,
            r_ready: INF,
        };

        Ok(GenDriver {
            ranks,
            level,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("kaisa kit needs attack.windupFraction")?,
            e_windup_fraction: kit.num("gen.E.buffWindupCastTimeS")?
                / kit.num("gen.E.attackTotalTimeS")?,

            p_base_dmg: kit.at_level("gen.P.baseDamageByLevel", level)?
                + kit.num("gen.P.apRatioBase")? * sheet.ap,
            p_perstack_dmg: kit.at_level("gen.P.perStackDamageByLevel", level)?
                + kit.num("gen.P.apRatioPerStack")? * sheet.ap,
            p_max_stacks: kit.num("gen.P.maxStacks")? as i64,
            p_duration: kit.num("gen.P.durationS")?,
            p_execute_pct: kit.num("gen.P.executeRatio")?
                + kit.num("gen.P.executeApRatio")? * sheet.ap,

            q_first_dmg,
            q_sub_dmg,
            q_missiles,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_missile_delay: kit.num("gen.Q.missileDelayS")?,
            q_volley_dur,
            q_missile_step,

            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_time: kit.num("gen.W.castTimeS")?,
            w_stacks,
            w_cd_mult,

            e_as_pct: kit.at_rank("gen.E.bonusAsPctByRank", ranks.e)?,
            e_buff_dur: kit.num("gen.E.buffDurationS")?,
            e_cdr_per_attack: kit.num("gen.E.cdrPerAttackS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_base: kit.num("gen.E.castTimeBaseS")?,
            e_cast_floor: kit.num("gen.E.castTimeFloorS")?,

            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_plasma_window: kit.num("gen.R.plasmaWindowS")?,

            src_p_cw: intern("P Caustic Wounds"),
            src_p_exec: intern("P Detonation"),
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
        if self.e_buff_active(t) {
            self.e_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.ranks.e > 0 && self.s.e_ready > t {
            self.s.e_ready = pymax(t, self.s.e_ready - self.e_cdr_per_attack);
        }
        // When an attack triggers the 5th Plasma stack, Caustic Wounds and the
        // missing-health detonation evaluate before the basic attack's damage.
        if self.stacks_at(t) >= self.p_max_stacks {
            if self.apply_cw_stack(e) {
                self.detonate_plasma(e);
            }
            self.s.p_done_on_swing = true;
        } else {
            self.s.p_done_on_swing = false;
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.s.p_done_on_swing {
            self.s.p_done_on_swing = false;
            return;
        }
        if self.apply_cw_stack(e) {
            self.detonate_plasma(e);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.ranks.r > 0
            && t >= self.s.r_ready
            && t < self.s.p_active_until
            && t >= self.s.busy_until
        {
            let wf = if self.e_buff_active(t) {
                self.e_windup_fraction
            } else {
                self.windup_fraction
            };
            let b0 = self.cur_bonus_as_no_e(e, t) + self.bonus_as(t);
            let a_cur = self.as_from_bonus(e, b0);
            let t_r_atk = t + wf / a_cur;
            let period = 1.0 / a_cur;

            // Skip firing R right before an immediate Q + W execute (0 extra
            // attacks needed) so W casts at `t` instead of waiting on a reset
            // swing or overwriting R's reset timer with W's 0.4 s lockout.
            let w_solo_now = self.ranks.w > 0
                && self.s.w_resolve_at >= INF
                && self.castable_at(e, self.s.w_ready) <= t + 1e-9
                && t_r_atk < t + self.w_cast_time - 1e-9
                && self.sim_attacks_kill(e, t, t + self.w_cast_time, INF, period, 0, true)
                && !self.sim_attacks_kill(e, t, t_r_atk, t_r_atk, period, 1, false);

            let mut hold_for_sb = false;
            if !w_solo_now
                && e.p.fx.s.spellblade.is_some()
                && e.p.fx.s.on_ult_cast.is_none()
                && e.p.fx.s.ult_attack_steroid.is_none()
                && !e.st.sb_primed
                && t < e.st.sb_icd_until
            {
                let n_with_r = if t_r_atk <= e.st.sb_icd_until + 1e-9 {
                    1 + imax(0, ((e.st.sb_icd_until - t_r_atk + 1e-9) / period) as i64)
                } else {
                    0
                };
                if !self.sim_attacks_kill(
                    e,
                    t,
                    e.st.sb_icd_until,
                    t_r_atk,
                    period,
                    n_with_r,
                    true,
                ) {
                    hold_for_sb = true;
                }
            }
            if !w_solo_now && !hold_for_sb {
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.prime_spellblade();
                if let Some(u) = &e.p.fx.s.on_ult_cast {
                    e.st.hex_until = t + u.duration_s;
                }
                if e.p.fx.s.ult_attack_steroid.is_some() {
                    e.st.post_r_attacks = 0;
                }
                let b = self.bonus_as(t);
                e.st.next_attack = t + wf / e.attack_speed(b);
                return;
            }
        }
        let b = self.bonus_as(t);
        e.st.next_attack = t + e.attack_period(b);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.q_v0.idx < self.q_missiles && self.s.q_v1.idx < self.q_missiles {
            return INF;
        }
        // Icathian Rain is an instant cast that can fire during Supercharge's
        // charge, waiting only if Void Seeker is currently mid-cast.
        let w_busy = if self.s.w_resolve_at < INF {
            self.s.w_resolve_at
        } else {
            0.0
        };
        pymax(pymax(e.st.q_ready, e.st.t), w_busy)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        let volley = Volley {
            start_at: t + self.q_missile_delay,
            idx: 0,
            proced: false,
        };
        if self.s.q_v0.idx >= self.q_missiles {
            self.s.q_v0 = volley;
        } else {
            self.s.q_v1 = volley;
        }
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r > 0 {
            // Killer Instinct requires Plasma on the target within the last 4 s,
            // so undo the engine's t = 0 ult pre-effects and arm R for the first
            // post-Plasma attack reset.
            self.s.r_ready = 0.0;
            e.st.sb_primed = false;
            e.st.next_attack = 0.0;
            e.st.hex_until = -1.0;
            e.st.post_r_attacks = (1.0 / 1e-9) as i64;
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        let v0_t = self.volley_next_at(self.s.q_v0);
        if v0_t < INF {
            out[n] = (v0_t, Kind::Ev(EV_Q_V0));
            n += 1;
        }
        let v1_t = self.volley_next_at(self.s.q_v1);
        if v1_t < INF {
            out[n] = (v1_t, Kind::Ev(EV_Q_V1));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_resolve_at < INF {
                out[n] = (self.s.w_resolve_at, Kind::Ev(EV_W_LAND));
                n += 1;
            } else {
                out[n] = (self.w_cast_at(e), Kind::Ev(EV_W_CAST));
                n += 1;
            }
        }
        if self.ranks.e > 0 {
            let e_at = self.e_cast_at(e);
            if e_at < INF {
                out[n] = (e_at, Kind::Ev(EV_E_CAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_V0) => self.step_volley(e, 0),
            Kind::Ev(EV_Q_V1) => self.step_volley(e, 1),
            Kind::Ev(EV_W_CAST) => {
                if self.castable_at(e, self.s.w_ready) > t || self.s.w_resolve_at < INF {
                    return;
                }
                self.s.w_ready = INF;
                self.s.w_resolve_at = t + self.w_cast_time;
                e.prime_spellblade();
                self.busy_for(e, self.w_cast_time);
            }
            Kind::Ev(EV_W_LAND) => {
                self.s.w_resolve_at = INF;
                let tot0 = e.st.total;
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                if self.w_dmg <= 0.0 || e.st.total > tot0 {
                    // Void Seeker landed (not negated by spell shield or stasis):
                    // apply 2 or 3 Plasma stacks successively, detonating
                    // immediately if the 5th stack is reached before rollover
                    // stacks, and grant Evolved W's 75% champion-hit refund.
                    let mut k = 0;
                    while k < self.w_stacks {
                        if self.apply_cw_stack(e) {
                            self.detonate_plasma(e);
                        }
                        k += 1;
                    }
                    e.ability_cast_proc();
                    e.eclipse_hit();
                    self.s.w_ready = t + e.basic_cd(self.w_cd) * self.w_cd_mult;
                } else {
                    self.s.w_ready = t + e.basic_cd(self.w_cd);
                }
            }
            Kind::Ev(EV_E_CAST) => {
                if self.castable_at(e, self.s.e_ready) > t {
                    return;
                }
                let total_bonus_as = self.cur_bonus_as_no_e(e, t) + self.bonus_as(t);
                let cast_time =
                    pymax(self.e_cast_base / (1.0 + total_bonus_as / 100.0), self.e_cast_floor);
                let t_end = t + cast_time;
                self.s.e_buff_start = t_end;
                self.s.e_buff_until = t_end + self.e_buff_dur;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.prime_spellblade();
                self.busy_for(e, cast_time);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
