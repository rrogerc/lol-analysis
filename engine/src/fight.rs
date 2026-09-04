//! The combat engine: one deterministic expected-value fight of a build
//! against a stat dummy — builds.py's `simulate`, transcribed so every
//! floating-point operation happens in the same order with the same
//! operands (the fixtures under data/builds/golden pin that).

use crate::fsum::fsum;
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// The kinds of timed event, in their tie order (the Python engine broke
/// ties by the kind's name: attack < burn < e_charge < e_release < mal < q
/// < r < ss < w_tick).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Kind {
    Attack,
    Burn(usize),
    ECharge,
    ERelease,
    Mal,
    Q,
    R,
    Ss,
    WTick,
}

#[derive(Clone, Debug)]
pub struct FightResult {
    pub total: f64,
    pub dps: f64,
    pub ttk: Option<f64>,
    pub ttk_eff: Option<f64>,
    pub ttk_exp: Option<f64>,
    pub attacks: i64,
    pub phantom_hits: i64,
    pub hp_left: f64,
    /// (source, damage) best-first, ties in first-dealt order — the
    /// Python breakdown dict's order.
    pub breakdown: Vec<(SourceId, f64)>,
}

#[derive(Clone, Copy, Debug)]
pub struct Target {
    pub hp: f64,
    pub armor: f64,
    pub mr: f64,
    pub duration: f64,
    pub bonus_hp: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct Opts {
    pub use_ult: bool,
    pub prestacked: bool,
    pub stop_after: f64,
    pub breakdown: bool,
    pub blend: bool,
}

/// `St.set`: which of the four "not yet" fields have been written.
pub const S_COMBAT_T0: u8 = 1;
pub const S_TTK: u8 = 2;
pub const S_TTK_EFF: u8 = 4;
pub const S_EXEC_P: u8 = 8;

/// The engine's fight state (the `st` dict). The four values that used to be
/// `Option<f64>` are a plain `f64` plus a bit in `set`: an `Option<f64>` is
/// 16 bytes and its discriminant is another branch on a path that reads it
/// on every damage instance. They go back to `Option` once, where the
/// `FightResult` is built.
#[derive(Clone, Copy, Debug)]
pub struct St {
    pub t: f64,
    pub hp: f64,
    pub seething: i64,
    pub phantom: i64,
    pub kraken: i64,
    pub dark: i64,
    pub attacks: i64,
    pub phantom_hits: i64,
    pub q_ready: f64,
    pub shred_until: f64,
    pub mal_shred_until: f64,
    pub kit_amp_pct: f64,
    /// `1.0 + kit_amp_pct / 100.0`, set wherever `kit_amp_pct` is.
    pub kit_amp_mult: f64,
    pub kit_amp_until: f64,
    pub combat_t0: f64,
    pub mal_until: f64,
    pub next_mal: f64,
    pub mal_tick: f64,
    pub sb_primed: bool,
    pub sb_icd_until: f64,
    pub flurry_until: f64,
    pub flurry_ready: f64,
    pub ss_at: f64,
    pub ss_done: bool,
    pub once_done: bool,
    pub energize: f64,
    pub en_last: f64,
    pub cleaver: i64,
    pub blood: i64,
    pub shojin: i64,
    pub eclipse: i64,
    pub nth: i64,
    pub hz_until: f64,
    pub hz_first: bool,
    pub ma_until: f64,
    pub hex_until: f64,
    pub post_r_attacks: i64,
    pub sundered_used: bool,
    pub r_impact: f64,
    pub next_attack: f64,
    pub ev_t: f64,
    pub ev_hp0: f64,
    pub ev_dmg: f64,
    pub prev_ev_t: f64,
    pub total: f64,
    pub ttk: f64,
    pub ttk_eff: f64,
    pub exec_p: f64,
    pub set: u8,
}

#[derive(Clone, Copy, Debug)]
struct BurnState {
    until: f64,
    next: f64,
}

// Which unique passives this build carries, as one word. `deal`, `do_attack`
// and `attack_speed` between them probe a dozen `Option`s spread over `Fx`
// dozens of times per fight; the answers cannot change during a fight, so
// they are settled once in `Engine::new` and tested as bits. Every probe is
// replaced one-for-one — no branch is dropped by reasoning about its value.
const F_HYPERSHOT: u32 = 1 << 0;
const F_SHOJIN: u32 = 1 << 1; // ability_amp_stacking
const F_MANA_ACTIVE: u32 = 1 << 2;
const F_ALT_PEN: u32 = 1 << 3;
const F_ARMOR_SHRED: u32 = 1 << 4;
const F_OPENER_LETH: u32 = 1 << 5;
const F_ULT_BURN: u32 = 1 << 6;
const F_MR_SHRED: u32 = 1 << 7;
const F_MAGIC_CRIT: u32 = 1 << 8;
const F_PROC_ONCE: u32 = 1 << 9; // ability_proc_once
const F_STORMSURGE: u32 = 1 << 10;
const F_EXECUTE: u32 = 1 << 11;
const F_NAVORI: u32 = 1 << 12;
const F_FLURRY: u32 = 1 << 13;
const F_PHANTOM: u32 = 1 << 14;
const F_KRAKEN: u32 = 1 << 15;
const F_AS_STACKING: u32 = 1 << 16;
const F_SUNDERED: u32 = 1 << 17; // first_attack_crit_floor_ev
const F_ULT_STEROID: u32 = 1 << 18; // ult_attack_steroid
const F_ENERGIZED: u32 = 1 << 19;
const F_NTH_HIT: u32 = 1 << 20;
const F_FIRST_ATTACK: u32 = 1 << 21; // first_attack_bonus
const F_SPELLBLADE: u32 = 1 << 22;
const F_ON_ULT_CAST: u32 = 1 << 23;
const F_HIT_PAIR: u32 = 1 << 24; // hit_pair_proc (Eclipse)
const F_MANA_PROC: u32 = 1 << 25; // ability_mana_proc (Muramana)

/// The bits `deal`'s own body branches on (the resist chain keeps its own —
/// `deal_simple` still goes through `phys_mult`/`mag_mult`). With none of
/// these set and `amp_is_one`, every branch they guard is provably not taken.
const DEAL_FLAGS: u32 = F_HYPERSHOT | F_SHOJIN | F_MANA_ACTIVE | F_ARMOR_SHRED | F_MR_SHRED
    | F_MAGIC_CRIT | F_PROC_ONCE | F_STORMSURGE | F_EXECUTE;

// The scheduler's tie-break, packed. `Kind`'s derived `Ord` compares the
// declaration order first and only then `Burn`'s index, so `(ord << 16) |
// burn_index` is order-isomorphic to it as long as a build has fewer than
// 65_536 burns. Sixteen bytes of enum become four bytes of integer.
const R_ATTACK: u32 = 0 << 16;
const R_BURN: u32 = 1 << 16;
const R_ECHARGE: u32 = 2 << 16;
const R_ERELEASE: u32 = 3 << 16;
const R_MAL: u32 = 4 << 16;
const R_Q: u32 = 5 << 16;
const R_R: u32 = 6 << 16;
const R_SS: u32 = 7 << 16;
const R_WTICK: u32 = 8 << 16;

#[inline(always)]
fn rank_of(k: Kind) -> u32 {
    match k {
        Kind::Attack => R_ATTACK,
        Kind::Burn(i) => {
            debug_assert!(i < 0x1_0000);
            R_BURN | i as u32
        }
        Kind::ECharge => R_ECHARGE,
        Kind::ERelease => R_ERELEASE,
        Kind::Mal => R_MAL,
        Kind::Q => R_Q,
        Kind::R => R_R,
        Kind::Ss => R_SS,
        Kind::WTick => R_WTICK,
    }
}

#[inline(always)]
fn kind_of(rank: u32) -> Kind {
    match rank >> 16 {
        0 => Kind::Attack,
        1 => Kind::Burn((rank & 0xFFFF) as usize),
        2 => Kind::ECharge,
        3 => Kind::ERelease,
        4 => Kind::Mal,
        5 => Kind::Q,
        6 => Kind::R,
        7 => Kind::Ss,
        _ => Kind::WTick,
    }
}

/// Debug-only: the packed rank orders every variant exactly as `Kind`'s
/// derived `Ord` does, and round-trips through `kind_of`.
#[cfg(debug_assertions)]
fn check_rank_order() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        let all: Vec<Kind> = [Kind::Attack, Kind::ECharge, Kind::ERelease, Kind::Mal, Kind::Q,
                              Kind::R, Kind::Ss, Kind::WTick]
            .into_iter()
            .chain((0..9).map(Kind::Burn))
            .collect();
        for &a in &all {
            assert_eq!(kind_of(rank_of(a)), a, "{a:?} round-trip");
            for &b in &all {
                assert_eq!(rank_of(a) < rank_of(b), a < b, "{a:?} < {b:?}");
                assert_eq!(rank_of(a) == rank_of(b), a == b, "{a:?} == {b:?}");
            }
        }
    });
}

#[cfg(debug_assertions)]
mod fastpath_stats {
    use std::sync::atomic::{AtomicU64, Ordering};
    pub static SIMPLE: AtomicU64 = AtomicU64::new(0);
    pub static TOTAL: AtomicU64 = AtomicU64::new(0);
    /// Report the share of fights taking `deal_simple`, every 100k fights.
    pub fn note(simple: bool) {
        if simple {
            SIMPLE.fetch_add(1, Ordering::Relaxed);
        }
        let n = TOTAL.fetch_add(1, Ordering::Relaxed) + 1;
        if n % 100_000 == 0 {
            let s = SIMPLE.load(Ordering::Relaxed);
            eprintln!("[deal_simple] {s}/{n} fights = {:.1}%", 100.0 * s as f64 / n as f64);
        }
    }
}

/// Most burns one build can stack: `Fx::merge` takes at most one per item
/// and a build holds at most `MAX_ITEMS` of them (see enumerate.rs).
pub const MAX_BURNS: usize = 8;

/// The half of a fight's setup that no target can change: the build's stat
/// sheet, its merged effects, and every amount that follows from those. It
/// is built once per (item combination, boots class) and shared by that
/// class's fights — `Engine::new` used to redo all of it per target.
pub struct Prep<'a> {
    pub sheet: &'a Sheet,
    pub fx: &'a Fx,
    pub ranks: Ranks,
    crit_c: f64,
    crit_ev: f64,
    auto_amp: f64,
    sheet_bonus_as: f64,
    base_as: f64,
    as_ratio: f64,
    lethality: f64,
    magic_pen_flat: f64,
    crit_damage: f64,
    shred_armor: f64,
    shred_mr: f64,
    hyper_amp: f64,
    mana_amp: f64,
    shojin_per: f64,
    shojin_max: i64,
    crit_below: f64,
    crit_below_pct: f64,
    crit_below_ev: f64,
    opener_until: f64,
    opener_leth: f64,
    alt_max: i64,
    alt_per: f64,
    cleaver_per: f64,
    cleaver_max: i64,
    blood_per: f64,
    blood_max: i64,
    mal_reduction: f64,
    has_dmg_amps: bool,
    /// Which unique passives this build has (F_* above), `F_EXECUTE` set
    /// whenever an item carries one: the blend's counterfactual leg clears
    /// that single bit for its own fight.
    flags: u32,
    /// `base_amp` provably returns the literal 1.0 it starts from.
    amp_is_one: bool,
    onhits: Vec<(f64, DType, SourceId)>,
    onhits_current: Vec<(f64, DType)>,
    /// The item actives that fire on engage, amounts already worked out.
    actives_once: Vec<(f64, DType, SourceId)>,
    ad: f64,
    move_speed: f64,
    energize_per_attack: f64,
    kraken_base: f64,
    nth_need: i64,
    nth_dmg: f64,
    spellblade_dmg: f64,
    /// `1.0 - navori_cdr / 100.0`: the item's percentage never changes.
    navori_factor: f64,
    /// `kraken_amp / 100.0`: `kraken_amp` is settled in this constructor.
    kraken_amp_frac: f64,
    /// `fb.base + fb.per_lethality * sheet.lethality`: both the item's
    /// numbers and the sheet's lethality are fixed for the fight.
    first_attack_amt: f64,
    /// `once.base + once.ap_ratio * sheet.ap`: item numbers and sheet AP.
    proc_once_amt: f64,
    /// `ss.base + ss.ap_ratio * sheet.ap`: item numbers and sheet AP.
    stormsurge_amt: f64,
    /// `mp.pct_by_level.at(level) / 100.0 * sheet.mana`: the level and the
    /// sheet's mana are both fixed for the fight.
    muramana_amt: f64,
    /// `ult_attack_steroid.attacks`, or `i64::MIN` when absent so that
    /// `post_r_attacks < steroid_attacks` is false without probing the item.
    steroid_attacks: i64,
    /// `1.0 - armor_pen_pct / 100.0` / `1.0 - magic_pen_pct / 100.0`: the
    /// sheet's percent pen is fixed for the fight, so `stack_pct_pen`'s first
    /// factor is too (see `stack_pct_pen_pre`).
    armor_pen_factor: f64,
    magic_pen_factor: f64,
    /// `execute_pct / 100.0`; the fight multiplies it by its target's HP.
    exec_frac: Option<f64>,
    /// Eclipse's `pct / 100.0`, the same way.
    eclipse_frac: f64,
    /// Each burn's per-tick amount with the target factored out: a flat burn
    /// outright, a max-HP burn as `pct / ticks / 100.0` for the target's HP
    /// to scale.
    burn_amt: [f64; MAX_BURNS],
    /// Bit per burn whose tick scales with the target's max HP.
    burn_hp_mask: u32,
    n_burns: usize,
    /// The ult's impact delay and its damage, and Malignance's per-tick
    /// amount: all three are settled by the kit, the ranks and the sheet.
    r_delay_s: Option<f64>,
    r_dmg: Option<f64>,
    mal_tick_amt: f64,
    /// The fight state before a target is known: each fight copies this and
    /// patches the two fields the target seeds (`hp` and `ev_hp0`).
    st0: St,
}

/// One fight in progress: the target's numbers, the state the fight moves,
/// and the memos that live and die with it. Everything else it reads is in
/// the `Prep` it borrows.
pub struct Engine<'a, 'p> {
    pub p: &'p Prep<'a>,
    pub target_hp: f64,
    target_armor: f64,
    target_mr: f64,
    target_bonus_hp: f64,
    breakdown: bool,
    /// `Prep::flags` with `F_EXECUTE` cleared on the counterfactual leg.
    flags: u32,
    /// No bit `deal`'s body branches on is set and `amp_is_one`: `deal_simple`.
    simple_deal: bool,
    exec_hp: f64,
    /// `Prep::eclipse_frac * target.hp`.
    eclipse_amt: f64,
    /// `Prep::burn_amt` with this target's HP applied to the max-HP burns.
    burn_amts: [f64; MAX_BURNS],
    /// `base_amp(dt, secs)` memo: `amp_secs` is the `secs` the three slots
    /// were filled for, `amp_have` which of them are filled.
    amp_secs: i64,
    amp_have: u8,
    amp_cache: [f64; 3],
    /// `attack_speed` memo: the key, field by field, then the value and the
    /// two reciprocals callers take from it (each computed from the cached
    /// speed, never from each other). `u64::MAX` is not a `to_bits()` any
    /// finite kit bonus produces, so it stands for "empty".
    as_key_bonus: u64,
    as_key_seething: i64,
    as_key_bits: u8,
    as_val: f64,
    /// bit 0: `as_recip` filled; bit 1: `as_windup` filled.
    as_derived: u8,
    as_recip: f64,
    as_windup: f64,
    pub st: St,
    /// One-entry memo of the resist multiplier per damage type. Everything the
    /// chain reads is either a per-fight constant or a component of the key,
    /// so an equal key means an identical result — by construction, not by
    /// recomputation. `u64::MAX` is unreachable for a packed key.
    phys_key: u64,
    phys_val: f64,
    mag_key: u64,
    mag_val: f64,
    burns: [BurnState; MAX_BURNS],
    /// Stormsurge's rolling damage window. The buffer belongs to the caller
    /// (see `Prep::fight`); a fight borrows it and hands it back, so a class
    /// of fights allocates it at most once.
    dmg_log: Vec<(f64, f64)>,
    /// First entry of `dmg_log` still inside Stormsurge's window.
    ss_head: usize,
    bd: Vec<f64>,
    bd_seen: Vec<bool>,
    bd_order: Vec<SourceId>,
}

/// One kit's rotation: the hooks the engine calls at each point of a fight.
pub trait Driver: Sized {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, prestacked: bool)
        -> Result<Self, String>;
    /// Put every field a fight moves back where `new` left it, so one driver
    /// serves a whole boots class. Each driver keeps its rotation state in
    /// one `Copy` struct and a pristine copy beside it, so this is that copy
    /// and nothing can drift out of it.
    fn reset(&mut self);
    fn ranged(&self) -> bool;
    fn attack_range(&self) -> f64;
    /// Kit-side bonus attack speed (stacking passives), in percent.
    fn bonus_as(&self) -> f64 {
        0.0
    }
    /// Navori's on-attack CDR over the basic cooldowns the kit keeps.
    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
    }
    fn before_attack(&mut self, _e: &mut Engine) {}
    fn attack_riders(&mut self, _e: &mut Engine) {}
    fn after_attack(&mut self, _e: &mut Engine) {}
    fn schedule_attack(&mut self, e: &mut Engine) {
        let b = self.bonus_as();
        e.st.next_attack = e.st.t + e.attack_period(b);
    }
    /// Earliest moment Q can be cast; INF when it can't be.
    fn q_at(&self, e: &Engine) -> f64 {
        if e.p.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }
    fn cast_q(&mut self, e: &mut Engine);
    fn cast_r(&mut self, _e: &mut Engine) {}
    /// Extra timed events, at most two, written into `out`.
    fn events(&self, _e: &Engine, _out: &mut [(f64, Kind); 2]) -> usize {
        0
    }
    fn on_event(&mut self, _e: &mut Engine, kind: Kind) {
        panic!("unhandled event {kind:?}");
    }
}

#[inline(always)]
pub fn shave(v: &mut f64, t: f64, factor: f64) {
    if *v > t {
        *v = t + (*v - t) * factor;
    }
}

impl<'a> Prep<'a> {
    /// Everything a fight needs that the target cannot change, settled once.
    /// `ranged`/`atk_range` come from the driver, which is target-free too.
    #[allow(clippy::too_many_arguments)]
    pub fn build(sheet: &'a Sheet, kit: &'a Kit, fx: &'a Fx, level: i64, ranks: Ranks,
                 ranged: bool, atk_range: f64) -> Result<Prep<'a>, String> {
        let s = &fx.s;
        let crit_c = sheet.crit_chance / 100.0;
        let crit_ev = 1.0 + crit_c * (sheet.crit_damage / 100.0 - 1.0);
        // Hexoptics' Magnification: scales with distance to the target, capped
        let mut auto_amp = 1.0;
        if let Some(aa) = &s.attack_amp {
            auto_amp += aa.max_pct / 100.0 * pymin(1.0, atk_range / aa.max_at_range);
        }
        let hyper_amp = match &s.hypershot {
            Some(h) => 1.0 + h.amp_pct / 100.0,
            None => 1.0,
        };
        let mana_amp = match &s.mana_active {
            Some(m) => 1.0 + (m.amp_base_pct + m.amp_per_100_bonus_mana * sheet.mana_bonus / 100.0)
                / 100.0,
            None => 1.0,
        };
        let (shojin_per, shojin_max) = match &s.ability_amp_stacking {
            Some(x) => (x.pct_per_stack / 100.0, x.max_stacks),
            None => (0.0, 0),
        };
        let (crit_below, crit_below_pct, crit_below_ev) = match &s.magic_crit {
            Some(m) => (m.below_target_hp_pct, m.crit_dmg_pct, m.crit_dmg_pct / 100.0),
            None => (0.0, 0.0, 0.0),
        };
        // the target's HP is the only thing missing from the execute's
        // threshold, so the fraction is settled here and scaled per fight
        let exec_frac = match s.execute_pct {
            Some(p) => Some(p / 100.0),
            None => None,
        };
        let (opener_until, opener_leth) = match &s.opener_lethality {
            Some(o) => (o.duration_s, if ranged { o.ranged } else { o.melee }),
            None => (-1.0, 0.0),
        };
        let (alt_max, alt_per) = match &s.alt_pen {
            Some(a) => (a.max_stacks, a.pct_per_stack),
            None => (0, 0.0),
        };
        let (cleaver_per, cleaver_max) = match &s.armor_shred {
            Some(a) => (a.pct_per_stack, a.max_stacks),
            None => (0.0, 0),
        };
        let (blood_per, blood_max) = match &s.mr_shred {
            Some(a) => (a.pct_per_stack, a.max_stacks),
            None => (0.0, 0),
        };
        let mal_reduction = match &s.ult_burn {
            Some(u) => u.mr_reduction,
            None => 0.0,
        };
        // on-hit damage is fixed by the sheet, so each entry is worked out
        // once per boots class — nothing here depends on the target
        let mut onhits = Vec::with_capacity(fx.onhit.len());
        for oh in &fx.onhit {
            let mut amt = oh.base + oh.ap_ratio * sheet.ap + oh.bonus_ad_ratio * sheet.ad_bonus
                + oh.max_mana_pct / 100.0 * sheet.mana;
            if let Some((melee, rng)) = oh.self_max_hp_pct {
                // Titanic Hydra: % of OWN max health
                let pct = if ranged { rng } else { melee };
                amt += pct / 100.0 * sheet.hp;
            }
            onhits.push((amt, oh.dtype, oh.source));
        }
        let onhits_current: Vec<(f64, DType)> = fx
            .onhit_current_hp
            .iter()
            .map(|oh| ((if ranged { oh.ranged_pct } else { oh.melee_pct }) / 100.0, oh.dtype))
            .collect();
        // item actives (Rocketbelt, Gunblade, hydra actives) fire on engage;
        // their amounts are the item's numbers and the sheet's, so they are
        // worked out here rather than at the top of every fight
        let actives_once: Vec<(f64, DType, SourceId)> = fx
            .actives_once
            .iter()
            .map(|a| {
                let amt = a.base
                    + (match a.by_level { Some(b) => b.at(level), None => 0.0 })
                    + a.ad_ratio * sheet.ad
                    + a.ap_ratio * sheet.ap;
                (amt, a.dtype, a.source)
            })
            .collect();
        // Energize: 6 stacks per attack (+ item bonuses) plus 1 per 24 units
        // moved — assumed kiting at full move speed between attacks
        let mut extra = 0.0f64;
        for en in &fx.energized {
            extra += en.extra_stacks_per_attack;
        }
        let energize_per_attack = 6.0 + extra;
        let (mut kraken_base, mut kraken_amp) = (0.0, 0.0);
        if let Some(k) = &s.kraken {
            kraken_base = k.base_by_level.at(level);
            if ranged {
                kraken_base *= k.ranged_mult;
            }
            kraken_amp = if ranged { k.missing_ranged } else { k.missing_melee };
        }
        let (mut nth_need, mut nth_dmg) = (0, 0.0);
        if let Some(n) = &s.nth_hit_proc {
            // Hullbreaker's Skipper
            nth_need = if ranged { n.stacks_needed_ranged } else { n.stacks_needed_melee };
            nth_dmg = (if ranged { n.base_ad_ratio_ranged } else { n.base_ad_ratio_melee })
                * sheet.ad_base
                + (if ranged { n.self_max_hp_pct_ranged } else { n.self_max_hp_pct_melee })
                    / 100.0
                    * sheet.hp;
        }
        let mut spellblade_dmg = 0.0;
        if let Some(sb) = &s.spellblade {
            spellblade_dmg = sb.base_ad_ratio * sheet.ad_base + sb.ap_ratio * sheet.ap
                + sb.per_crit_chance_pct * sheet.crit_chance;
        }
        // Fight-invariant amounts, worked out once. Every operand below is
        // either an item's own number or a sheet stat — none of them moves
        // during a fight, or between the fights of one build — and each
        // expression keeps its operators and their order.
        let navori_factor = match s.navori_cdr {
            Some(navori) => 1.0 - navori / 100.0,
            None => 0.0,
        };
        let kraken_amp_frac = kraken_amp / 100.0;
        let first_attack_amt = match &s.first_attack_bonus {
            Some(fb) => fb.base + fb.per_lethality * sheet.lethality,
            None => 0.0,
        };
        let proc_once_amt = match &s.ability_proc_once {
            Some(once) => once.base + once.ap_ratio * sheet.ap,
            None => 0.0,
        };
        let stormsurge_amt = match &s.stormsurge {
            Some(ss) => ss.base + ss.ap_ratio * sheet.ap,
            None => 0.0,
        };
        let muramana_amt = match &s.ability_mana_proc {
            Some(mp) => mp.pct_by_level.at(level) / 100.0 * sheet.mana,
            None => 0.0,
        };
        let eclipse_frac = match &s.hit_pair_proc {
            Some(hp_cfg) => {
                let pct = if ranged { hp_cfg.max_hp_pct_ranged } else { hp_cfg.max_hp_pct_melee };
                pct / 100.0
            }
            None => 0.0,
        };
        if fx.burns.len() > MAX_BURNS {
            return Err(format!("a build carries {} burns; MAX_BURNS is {MAX_BURNS}",
                               fx.burns.len()));
        }
        let mut burn_amt = [0.0f64; MAX_BURNS];
        let mut burn_hp_mask = 0u32;
        for (i, b) in fx.burns.iter().enumerate() {
            let ticks = b.duration_s / b.tick_s;
            burn_amt[i] = match b.max_hp_pct_total {
                // Liandry's: % of target max HP — the target's factor is the
                // one thing left for the fight to apply
                Some(pct) => {
                    burn_hp_mask |= 1 << i;
                    pct / ticks / 100.0
                }
                // Blackfire Torch: flat + AP ratio, total over the duration
                None => (b.total_base + b.total_ap_ratio * sheet.ap) / ticks,
            };
        }
        let steroid_attacks = match &s.ult_attack_steroid {
            Some(u) => u.attacks,
            None => i64::MIN,
        };
        // the ult, whose damage and Malignance burn are the kit's and the
        // sheet's; `damage` is only read when the ult is actually cast, so a
        // kit that never casts one is not asked for it here either
        let r_dmg = match (&kit.r.damage, ranks.r > 0) {
            (Some(d), true) => Some(d.hit(ranks.r, sheet)),
            _ => None,
        };
        let mal_tick_amt = match &s.ult_burn {
            Some(ub) => {
                let ticks = ub.duration_s / 0.25;
                (ub.total_base + ub.total_ap_ratio * sheet.ap) / ticks
            }
            None => 0.0,
        };
        let mut st0 = St {
            t: 0.0,
            // `hp` and `ev_hp0` are the target's max HP; every fight patches
            // them into its own copy (see `Engine::new`)
            hp: 0.0,
            seething: 0,
            phantom: 0,
            kraken: 0,
            dark: 0,
            attacks: 0,
            phantom_hits: 0,
            q_ready: 0.0,
            shred_until: -1.0,
            mal_shred_until: -1.0,
            kit_amp_pct: 0.0,
            kit_amp_mult: 1.0,
            kit_amp_until: -1.0,
            combat_t0: 0.0,
            mal_until: -1.0,
            next_mal: INF,
            mal_tick: 0.0,
            sb_primed: false,
            sb_icd_until: -1.0,
            flurry_until: -1.0,
            flurry_ready: 0.0,
            ss_at: INF,
            ss_done: false,
            once_done: false,
            energize: 0.0,
            en_last: 0.0,
            cleaver: 0,
            blood: 0,
            shojin: 0,
            eclipse: 0,
            nth: 0,
            hz_until: -1.0,
            hz_first: false,
            ma_until: -1.0,
            hex_until: -1.0,
            post_r_attacks: 1_000_000_000,
            sundered_used: false,
            r_impact: INF,
            next_attack: 0.0,
            ev_t: -1.0,
            ev_hp0: 0.0,
            ev_dmg: 0.0,
            prev_ev_t: 0.0,
            total: 0.0,
            ttk: 0.0,
            ttk_eff: 0.0,
            exec_p: 0.0,
            set: 0,
        };
        if kit.attack_never {
            // a kit played without autos: nothing that rides an attack ever fires
            st0.next_attack = INF;
        }
        if let Some(m) = &s.mana_active {
            // Actualizer: cast on engage, empowered for 8s
            st0.ma_until = m.duration_s;
        }
        let mut flags = 0u32;
        for (bit, on) in [
            (F_HYPERSHOT, s.hypershot.is_some()),
            (F_SHOJIN, s.ability_amp_stacking.is_some()),
            (F_MANA_ACTIVE, s.mana_active.is_some()),
            (F_ALT_PEN, s.alt_pen.is_some()),
            (F_ARMOR_SHRED, s.armor_shred.is_some()),
            (F_OPENER_LETH, s.opener_lethality.is_some()),
            (F_ULT_BURN, s.ult_burn.is_some()),
            (F_MR_SHRED, s.mr_shred.is_some()),
            (F_MAGIC_CRIT, s.magic_crit.is_some()),
            (F_PROC_ONCE, s.ability_proc_once.is_some()),
            (F_STORMSURGE, s.stormsurge.is_some()),
            (F_EXECUTE, s.execute_pct.is_some()),
            (F_NAVORI, s.navori_cdr.is_some()),
            (F_FLURRY, s.flurry.is_some()),
            (F_PHANTOM, s.phantom.is_some()),
            (F_KRAKEN, s.kraken.is_some()),
            (F_AS_STACKING, s.as_stacking.is_some()),
            (F_SUNDERED, s.first_attack_crit_floor_ev.is_some()),
            (F_ULT_STEROID, s.ult_attack_steroid.is_some()),
            (F_ENERGIZED, !fx.energized.is_empty()),
            (F_NTH_HIT, s.nth_hit_proc.is_some()),
            (F_FIRST_ATTACK, s.first_attack_bonus.is_some()),
            (F_SPELLBLADE, s.spellblade.is_some()),
            (F_ON_ULT_CAST, s.on_ult_cast.is_some()),
            (F_HIT_PAIR, s.hit_pair_proc.is_some()),
            (F_MANA_PROC, s.ability_mana_proc.is_some()),
        ] {
            if on {
                flags |= bit;
            }
        }
        // with no amp of any kind, `base_amp` runs neither loop and skips the
        // Giant Slayer term, returning the literal 1.0 it starts from
        let amp_is_one =
            fx.dmg_amps.is_empty() && fx.flat_amps.is_empty() && fx.s.giant_slayer.is_none();
        Ok(Prep {
            sheet,
            fx,
            ranks,
            crit_c,
            crit_ev,
            auto_amp,
            sheet_bonus_as: sheet.bonus_as_pct,
            base_as: sheet.base_as,
            as_ratio: sheet.as_ratio,
            lethality: sheet.lethality,
            magic_pen_flat: sheet.magic_pen_flat,
            crit_damage: sheet.crit_damage,
            shred_armor: kit.q.shred_pct_armor,
            shred_mr: kit.q.shred_pct_mr,
            hyper_amp,
            mana_amp,
            shojin_per,
            shojin_max,
            crit_below,
            crit_below_pct,
            crit_below_ev,
            opener_until,
            opener_leth,
            alt_max,
            alt_per,
            cleaver_per,
            cleaver_max,
            blood_per,
            blood_max,
            mal_reduction,
            has_dmg_amps: !fx.dmg_amps.is_empty(),
            flags,
            amp_is_one,
            onhits,
            onhits_current,
            actives_once,
            ad: sheet.ad,
            move_speed: sheet.move_speed,
            energize_per_attack,
            kraken_base,
            nth_need,
            nth_dmg,
            spellblade_dmg,
            navori_factor,
            kraken_amp_frac,
            first_attack_amt,
            proc_once_amt,
            stormsurge_amt,
            muramana_amt,
            steroid_attacks,
            armor_pen_factor: 1.0 - sheet.armor_pen_pct / 100.0,
            magic_pen_factor: 1.0 - sheet.magic_pen_pct / 100.0,
            exec_frac,
            eclipse_frac,
            burn_amt,
            burn_hp_mask,
            n_burns: fx.burns.len(),
            r_delay_s: kit.r.delay_s,
            r_dmg,
            mal_tick_amt,
            st0,
        })
    }
}

impl<'a, 'p> Engine<'a, 'p> {
    /// The target's half of the setup, on top of a `Prep` the class shares.
    /// `no_execute` runs the fight as if no item carried an execute: the
    /// blend's counterfactual leg. Nulling `fx.s.execute_pct` had exactly two
    /// observable effects — `exec_hp` fell to the `None` arm's 0.0 and the
    /// execute guard in `deal` went false — so the flag reproduces both
    /// without deep-cloning the whole `Fx`.
    fn new(p: &'p Prep<'a>, target: &Target, breakdown: bool, no_execute: bool,
           dmg_log: Vec<(f64, f64)>) -> Engine<'a, 'p> {
        let flags = if no_execute { p.flags & !F_EXECUTE } else { p.flags };
        let simple_deal = p.amp_is_one && flags & DEAL_FLAGS == 0;
        #[cfg(debug_assertions)]
        fastpath_stats::note(simple_deal);
        let exec_hp = match p.exec_frac {
            Some(f) if !no_execute => f * target.hp,
            // `None => 0.0`, which is also what a nulled execute_pct gave
            _ => 0.0,
        };
        // the burns' target-scaled halves; the rest was settled in `Prep`
        let mut burn_amts = [0.0f64; MAX_BURNS];
        for i in 0..p.n_burns {
            burn_amts[i] = if p.burn_hp_mask & (1 << i) != 0 {
                p.burn_amt[i] * target.hp
            } else {
                p.burn_amt[i]
            };
        }
        let mut st = p.st0;
        st.hp = target.hp;
        st.ev_hp0 = target.hp;
        // the breakdown vectors are the only readers of the interned-source
        // table, and `source_count()` takes a process-wide RwLock: only pay
        // for it when a breakdown is actually wanted
        let (bd, bd_seen) = if breakdown {
            let n_src = source_count();
            (vec![0.0; n_src], vec![false; n_src])
        } else {
            (Vec::new(), Vec::new())
        };
        Engine {
            p,
            target_hp: target.hp,
            target_armor: target.armor,
            target_mr: target.mr,
            target_bonus_hp: target.bonus_hp,
            breakdown,
            flags,
            simple_deal,
            exec_hp,
            eclipse_amt: p.eclipse_frac * target.hp,
            burn_amts,
            amp_secs: i64::MIN,
            amp_have: 0,
            amp_cache: [0.0; 3],
            as_key_bonus: u64::MAX,
            as_key_seething: 0,
            as_key_bits: 0,
            as_val: 0.0,
            as_derived: 0,
            as_recip: 0.0,
            as_windup: 0.0,
            st,
            phys_key: u64::MAX,
            phys_val: 0.0,
            mag_key: u64::MAX,
            mag_val: 0.0,
            burns: [BurnState { until: -1.0, next: INF }; MAX_BURNS],
            dmg_log,
            ss_head: 0,
            bd,
            bd_seen,
            bd_order: Vec::new(),
        }
    }

    /// Everything `attack_speed_slow` reads that a fight can move: the kit's
    /// own bonus, the Seething stacks, and the three windows.
    #[inline(always)]
    fn as_key(&self, kit_bonus: f64) -> (u64, i64, u8) {
        let st = &self.st;
        let mut bits = 0u8;
        if self.flags & F_FLURRY != 0 && st.t < st.flurry_until {
            bits |= 1;
        }
        if self.flags & F_ON_ULT_CAST != 0 && st.t < st.hex_until {
            bits |= 2;
        }
        if self.flags & F_ULT_STEROID != 0 && st.post_r_attacks < self.p.steroid_attacks {
            bits |= 4;
        }
        (kit_bonus.to_bits(), st.seething, bits)
    }

    /// The attack speed, memoized: at level 16 with permanent Zealous stacks
    /// it is the same number for the whole fight, and it is otherwise a
    /// divide and a cap per attack.
    #[inline(always)]
    pub fn attack_speed(&mut self, kit_bonus: f64) -> f64 {
        let (b, se, bits) = self.as_key(kit_bonus);
        if b != self.as_key_bonus || se != self.as_key_seething || bits != self.as_key_bits {
            let v = self.attack_speed_slow(kit_bonus);
            self.as_key_bonus = b;
            self.as_key_seething = se;
            self.as_key_bits = bits;
            self.as_val = v;
            self.as_derived = 0;
        }
        debug_assert_eq!(self.as_val.to_bits(), self.attack_speed_slow(kit_bonus).to_bits());
        self.as_val
    }

    /// `1.0 / attack_speed(..)`, cached in its own slot.
    #[inline(always)]
    pub fn attack_period(&mut self, kit_bonus: f64) -> f64 {
        let a = self.attack_speed(kit_bonus);
        if self.as_derived & 1 == 0 {
            self.as_recip = 1.0 / a;
            self.as_derived |= 1;
        }
        debug_assert_eq!(self.as_recip.to_bits(), (1.0 / a).to_bits());
        self.as_recip
    }

    /// `windup / attack_speed(..)`, cached in its own slot — a different
    /// numerator, so never derived from `attack_period`. `windup` is the
    /// driver's fixed windup fraction, so it does not enter the key.
    #[inline(always)]
    pub fn attack_windup(&mut self, kit_bonus: f64, windup: f64) -> f64 {
        let a = self.attack_speed(kit_bonus);
        if self.as_derived & 2 == 0 {
            self.as_windup = windup / a;
            self.as_derived |= 2;
        }
        debug_assert_eq!(self.as_windup.to_bits(), (windup / a).to_bits());
        self.as_windup
    }

    #[inline(never)]
    fn attack_speed_slow(&self, kit_bonus: f64) -> f64 {
        let s = &self.p.fx.s;
        let st = &self.st;
        let mut bonus = self.p.sheet_bonus_as + kit_bonus;
        if self.flags & F_AS_STACKING != 0 {
            let a = s.as_stacking.as_ref().expect("as_stacking");
            bonus += st.seething as f64 * a.pct_per_stack;
        }
        if self.flags & F_FLURRY != 0 {
            let f = s.flurry.as_ref().expect("flurry");
            if st.t < st.flurry_until {
                bonus += f.as_pct;
            }
        }
        if self.flags & F_ON_ULT_CAST != 0 {
            let u = s.on_ult_cast.as_ref().expect("on_ult_cast");
            if st.t < st.hex_until {
                bonus += u.as_pct;
            }
        }
        if self.flags & F_ULT_STEROID != 0 {
            let u = s.ult_attack_steroid.as_ref().expect("ult_attack_steroid");
            if st.post_r_attacks < u.attacks {
                bonus += u.as_pct;
            }
        }
        pymin(self.p.base_as + self.p.as_ratio * bonus / 100.0, AS_CAP)
    }

    /// A build's base amp by damage type after `secs` whole seconds in combat.
    fn base_amp(&self, dt: DType, secs: i64) -> f64 {
        let mut amp = 1.0;
        for a in &self.p.fx.dmg_amps {
            amp *= 1.0 + a.pct_per_stack / 100.0 * imin(a.max_stacks, secs) as f64;
        }
        for a in &self.p.fx.flat_amps {
            // Abyssal Mask: always-on, one damage type
            if a.dtype.is_none() || a.dtype == Some(dt) {
                amp *= 1.0 + a.pct / 100.0;
            }
        }
        if let Some(max_pct) = self.p.fx.s.giant_slayer {
            // 1% per 100 target bonus HP, capped
            amp *= 1.0 + pymin(max_pct, self.target_bonus_hp / 100.0) / 100.0;
        }
        amp
    }

    /// `base_amp` memoized whole on `(dt, secs)` — never split into factors.
    /// `secs` only ever advances, so one generation of three slots is enough.
    #[inline(always)]
    fn base_amp_memo(&mut self, dt: DType, secs: i64) -> f64 {
        if self.p.amp_is_one {
            return 1.0;
        }
        let i = dt as usize;
        if secs != self.amp_secs {
            self.amp_secs = secs;
            self.amp_have = 0;
        } else if self.amp_have & (1 << i) != 0 {
            return self.amp_cache[i];
        }
        let v = self.base_amp(dt, secs);
        self.amp_cache[i] = v;
        self.amp_have |= 1 << i;
        v
    }

    /// The physical resist multiplier, verbatim: the expression `deal` used
    /// to evaluate inline, moved out unchanged.
    #[inline(never)]
    fn phys_mult_slow(&self, t: f64) -> f64 {
        let qs_on = t < self.st.shred_until;
        let dark_pen = if self.flags & F_ALT_PEN != 0 {
            imin(self.st.dark, self.p.alt_max) as f64 * self.p.alt_per
        } else {
            0.0
        };
        let mut shred = if qs_on { self.p.shred_armor } else { 0.0 };
        if self.flags & F_ARMOR_SHRED != 0 {
            // Black Cleaver: % armor reduction stacks
            shred += self.st.cleaver as f64 * self.p.cleaver_per;
        }
        let mut leth = self.p.lethality;
        if self.flags & F_OPENER_LETH != 0 && t < self.p.opener_until {
            leth += self.p.opener_leth;
        }
        resist_mult(eff_resist(self.target_armor, 0.0, shred,
                               stack_pct_pen_pre(self.p.armor_pen_factor, dark_pen), leth))
    }

    /// The magic resist multiplier, verbatim (see `phys_mult_slow`).
    #[inline(never)]
    fn mag_mult_slow(&self, t: f64) -> f64 {
        let qs_on = t < self.st.shred_until;
        let dark_pen = if self.flags & F_ALT_PEN != 0 {
            imin(self.st.dark, self.p.alt_max) as f64 * self.p.alt_per
        } else {
            0.0
        };
        let mal = if self.flags & F_ULT_BURN != 0 && t < self.st.mal_shred_until {
            self.p.mal_reduction
        } else {
            0.0
        };
        let mut shred = if qs_on { self.p.shred_mr } else { 0.0 };
        if self.flags & F_MR_SHRED != 0 {
            // Bloodletter's Curse: % MR reduction stacks
            shred += self.st.blood as f64 * self.p.blood_per;
        }
        resist_mult(eff_resist(self.target_mr, mal, shred,
                               stack_pct_pen_pre(self.p.magic_pen_factor, dark_pen),
                               self.p.magic_pen_flat))
    }

    /// The only things `phys_mult_slow` reads that a fight can change: the Q
    /// shred window, the opener-lethality window, the capped Dark stacks and
    /// the Cleaver stacks. Packed, they key a one-entry memo.
    #[inline(always)]
    fn phys_mult(&mut self, t: f64) -> f64 {
        let dark = imin(self.st.dark, self.p.alt_max);
        let cleaver = self.st.cleaver;
        debug_assert!((0..0x1_0000).contains(&dark) && (0..0x1_0000).contains(&cleaver));
        let key = (t < self.st.shred_until) as u64
            | (((t < self.p.opener_until) as u64) << 1)
            | ((dark as u64) << 2)
            | ((cleaver as u64) << 18);
        if key == self.phys_key {
            debug_assert_eq!(self.phys_val.to_bits(), self.phys_mult_slow(t).to_bits());
            return self.phys_val;
        }
        let v = self.phys_mult_slow(t);
        self.phys_key = key;
        self.phys_val = v;
        v
    }

    /// As `phys_mult`, for magic: the Q shred window, Malignance's MR-shred
    /// window, the capped Dark stacks and the Bloodletter stacks.
    #[inline(always)]
    fn mag_mult(&mut self, t: f64) -> f64 {
        let dark = imin(self.st.dark, self.p.alt_max);
        let blood = self.st.blood;
        debug_assert!((0..0x1_0000).contains(&dark) && (0..0x1_0000).contains(&blood));
        let key = (t < self.st.shred_until) as u64
            | (((t < self.st.mal_shred_until) as u64) << 1)
            | ((dark as u64) << 2)
            | ((blood as u64) << 18);
        if key == self.mag_key {
            debug_assert_eq!(self.mag_val.to_bits(), self.mag_mult_slow(t).to_bits());
            return self.mag_val;
        }
        let v = self.mag_mult_slow(t);
        self.mag_key = key;
        self.mag_val = v;
        v
    }

    #[inline(always)]
    fn record(&mut self, source: SourceId, dmg: f64) {
        let i = source as usize;
        if i >= self.bd.len() {
            self.bd.resize(i + 1, 0.0);
            self.bd_seen.resize(i + 1, false);
        }
        if !self.bd_seen[i] {
            self.bd_seen[i] = true;
            self.bd_order.push(source);
        }
        self.bd[i] += dmg;
    }

    #[inline(always)]
    pub fn deal(&mut self, amount: f64, dtype: DType, source: SourceId, crit_mod: bool,
                ability: bool, ev_floor: f64) {
        if self.simple_deal {
            self.deal_simple(amount, dtype, source, crit_mod, ability, ev_floor);
        } else {
            self.deal_full(amount, dtype, source, crit_mod, ability, ev_floor);
        }
    }

    fn deal_full(&mut self, amount: f64, dtype: DType, source: SourceId, crit_mod: bool,
                 ability: bool, ev_floor: f64) {
        let fx: &'a Fx = self.p.fx;
        let s = &fx.s;
        let t = self.st.t;
        // Liandry's Suffering, Riftmaker's Void Corruption: a stack per whole
        // second in combat, with a hair of tolerance at second boundaries.
        // `combat_t0` has no other reader, so it is only worth tracking when
        // there is an amp to stack.
        let secs = if self.p.has_dmg_amps {
            if self.st.set & S_COMBAT_T0 == 0 {
                // the first damage dealt opens combat
                self.st.combat_t0 = t;
                self.st.set |= S_COMBAT_T0;
            }
            (t - self.st.combat_t0 + 1e-9) as i64
        } else {
            0
        };
        let mut amp = self.base_amp_memo(dtype, secs);
        if self.flags & F_HYPERSHOT != 0 && t < self.st.hz_until {
            amp *= self.p.hyper_amp;
        }
        if t <= self.st.kit_amp_until && dtype != DType::True {
            amp *= self.st.kit_amp_mult;
        }
        // "increased basic damage" is the attack itself
        if source == SRC_AUTO {
            amp *= self.p.auto_amp;
        }
        if ability {
            if self.flags & F_SHOJIN != 0 {
                // Shojin's Focused Will
                amp *= 1.0 + self.p.shojin_per * self.st.shojin as f64;
            }
            if self.flags & F_MANA_ACTIVE != 0 && t < self.st.ma_until {
                amp *= self.p.mana_amp;
            }
        }
        let mult = match dtype {
            DType::Physical => self.phys_mult(t),
            DType::True => 1.0,
            DType::Magic => self.mag_mult(t),
        };
        // Cinderbloom is a deterministic crit below the HP threshold
        let mut hp = self.st.hp;
        let below = self.flags & F_MAGIC_CRIT != 0 && dtype == DType::Magic
            && hp / self.target_hp * 100.0 < self.p.crit_below;
        let mut ev = 1.0;
        if crit_mod {
            ev = self.p.crit_ev;
            if below {
                ev = self.p.crit_c * self.p.crit_damage / 100.0
                    + (1.0 - self.p.crit_c) * self.p.crit_below_pct / 100.0;
            }
            if ev < ev_floor {
                // guaranteed-crit attacks (Sundered Sky)
                ev = ev_floor;
            }
        } else if below {
            ev = self.p.crit_below_ev;
        }
        let dmg = amount * amp * mult * ev;
        // Damage arrives in batches at discrete times; track each batch so
        // the killing blow is credited only for the share actually needed.
        if t != self.st.ev_t {
            self.st.prev_ev_t = if self.st.ev_t > 0.0 { self.st.ev_t } else { 0.0 };
            self.st.ev_t = t;
            self.st.ev_hp0 = hp;
            self.st.ev_dmg = 0.0;
        }
        self.st.ev_dmg += dmg;
        hp -= dmg;
        self.st.hp = hp;
        self.st.total += dmg;
        if self.breakdown {
            self.record(source, dmg);
        }
        // stacking shreds/amps build off the damage just dealt
        if dtype == DType::Physical && self.flags & F_ARMOR_SHRED != 0 {
            let c = self.st.cleaver + 1;
            self.st.cleaver = if c < self.p.cleaver_max { c } else { self.p.cleaver_max };
        }
        if ability {
            if self.flags & F_MR_SHRED != 0 && dtype == DType::Magic {
                let b = self.st.blood + 1;
                self.st.blood = if b < self.p.blood_max { b } else { self.p.blood_max };
            }
            if self.flags & F_SHOJIN != 0 {
                let sh = self.st.shojin + 1;
                self.st.shojin = if sh < self.p.shojin_max { sh } else { self.p.shojin_max };
            }
            if self.flags & F_HYPERSHOT != 0 {
                let h = s.hypershot.as_ref().expect("hypershot");
                if !self.st.hz_first {
                    // the opening cast is the one made from 600+ range
                    self.st.hz_first = true;
                    self.st.hz_until = t + h.duration_s;
                }
            }
            for (b, sb) in fx.burns.iter().zip(self.burns.iter_mut()) {
                if sb.next == INF {
                    sb.next = t + b.tick_s;
                }
                sb.until = t + b.duration_s;
            }
            if self.flags & F_PROC_ONCE != 0 {
                let once = s.ability_proc_once.as_ref().expect("ability_proc_once");
                if !self.st.once_done {
                    self.st.once_done = true;
                    let amt = self.p.proc_once_amt;
                    self.deal(amt, once.dtype, once.source, false, false, 1.0);
                }
            }
        }
        hp = self.st.hp;
        if self.flags & F_STORMSURGE != 0 {
            let storm = s.stormsurge.as_ref().expect("stormsurge");
            if !self.st.ss_done && self.st.ss_at == INF {
                self.dmg_log.push((t, dmg));
                let window = t - storm.window_s;
                // The clock never goes backwards, so neither does `window`:
                // the entries the filter kept are always a contiguous suffix,
                // and its start only ever moves forward. Same elements, same
                // order, same `fsum` — the rescan was quadratic for nothing.
                while let Some(&(tt, _)) = self.dmg_log.get(self.ss_head) {
                    if tt >= window {
                        break;
                    }
                    self.ss_head += 1;
                }
                let recent = fsum(self.dmg_log[self.ss_head..].iter().map(|(_, d)| *d));
                if recent >= storm.threshold_pct / 100.0 * self.target_hp {
                    self.st.ss_at = t + storm.delay_s;
                }
            }
        }
        let mut exec_amt = 0.0;
        if self.flags & F_EXECUTE != 0 && self.st.set & S_TTK == 0 && 0.0 < hp
            && hp <= self.exec_hp {
            exec_amt = hp;
            if self.breakdown {
                self.record(SRC_EXECUTE, exec_amt);
            }
            self.st.total += exec_amt;
            self.st.ev_dmg += exec_amt;
            self.st.hp = 0.0;
            hp = 0.0;
        }
        if hp <= 0.0 && self.st.set & S_TTK == 0 {
            self.st.ttk = t;
            self.st.set |= S_TTK;
            // Effective (ranking) kill time: interpolate back over the gap
            // by the fraction of the batch actually needed.
            let frac = if self.st.ev_dmg > 0.0 {
                pymin(1.0, self.st.ev_hp0 / self.st.ev_dmg)
            } else {
                1.0
            };
            self.st.ttk_eff = self.st.prev_ev_t + frac * (t - self.st.prev_ev_t);
            self.st.set |= S_TTK_EFF;
            if exec_amt > 0.0 {
                let batch = self.st.ev_dmg - exec_amt;
                self.st.exec_p = if batch > 0.0 { pymin(1.0, self.exec_hp / batch) } else { 1.0 };
                self.st.set |= S_EXEC_P;
            }
        }
    }

    /// `deal_full` with every branch deleted whose condition is provably
    /// false for this build: none of DEAL_FLAGS is set and `amp_is_one`, so
    /// `base_amp` is the literal 1.0, `has_dmg_amps` is false (`secs` is 0
    /// and nothing else reads `combat_t0`), `below` is false, `exec_amt`
    /// stays 0.0, and nothing here can call `deal` again. Every surviving
    /// float operation is literally the one above, in the same order.
    fn deal_simple(&mut self, amount: f64, dtype: DType, source: SourceId, crit_mod: bool,
                   ability: bool, ev_floor: f64) {
        let fx: &'a Fx = self.p.fx;
        let t = self.st.t;
        let mut amp = 1.0;
        if t <= self.st.kit_amp_until && dtype != DType::True {
            // kit-side, not item-side: Vladimir's R keeps this alive
            amp *= self.st.kit_amp_mult;
        }
        // "increased basic damage" is the attack itself
        if source == SRC_AUTO {
            amp *= self.p.auto_amp;
        }
        let mult = match dtype {
            DType::Physical => self.phys_mult(t),
            DType::True => 1.0,
            DType::Magic => self.mag_mult(t),
        };
        let mut hp = self.st.hp;
        let mut ev = 1.0;
        if crit_mod {
            ev = self.p.crit_ev;
            if ev < ev_floor {
                // guaranteed-crit attacks (Sundered Sky)
                ev = ev_floor;
            }
        }
        let dmg = amount * amp * mult * ev;
        // Damage arrives in batches at discrete times; track each batch so
        // the killing blow is credited only for the share actually needed.
        if t != self.st.ev_t {
            self.st.prev_ev_t = if self.st.ev_t > 0.0 { self.st.ev_t } else { 0.0 };
            self.st.ev_t = t;
            self.st.ev_hp0 = hp;
            self.st.ev_dmg = 0.0;
        }
        self.st.ev_dmg += dmg;
        hp -= dmg;
        self.st.hp = hp;
        self.st.total += dmg;
        if self.breakdown {
            self.record(source, dmg);
        }
        if ability {
            for (b, sb) in fx.burns.iter().zip(self.burns.iter_mut()) {
                if sb.next == INF {
                    sb.next = t + b.tick_s;
                }
                sb.until = t + b.duration_s;
            }
        }
        hp = self.st.hp;
        if hp <= 0.0 && self.st.set & S_TTK == 0 {
            self.st.ttk = t;
            self.st.set |= S_TTK;
            // Effective (ranking) kill time: interpolate back over the gap
            // by the fraction of the batch actually needed.
            let frac = if self.st.ev_dmg > 0.0 {
                pymin(1.0, self.st.ev_hp0 / self.st.ev_dmg)
            } else {
                1.0
            };
            self.st.ttk_eff = self.st.prev_ev_t + frac * (t - self.st.prev_ev_t);
            self.st.set |= S_TTK_EFF;
        }
    }

    pub fn prime_spellblade(&mut self) {
        if self.flags & F_SPELLBLADE != 0 && self.st.t >= self.st.sb_icd_until {
            self.st.sb_primed = true;
        }
    }

    /// Muramana's Shock: bonus physical damage per damaging ability cast.
    pub fn ability_cast_proc(&mut self) {
        let fx: &'a Fx = self.p.fx;
        if self.flags & F_MANA_PROC != 0 {
            let mp = fx.s.ability_mana_proc.as_ref().expect("ability_mana_proc");
            let amt = self.p.muramana_amt;
            let dt = mp.dtype;
            self.deal(amt, dt, SRC_MURAMANA, false, false, 1.0);
        }
    }

    /// Eclipse: attacks and damaging casts each grant one stack; every 2nd
    /// stack procs (% max HP).
    pub fn eclipse_hit(&mut self) {
        let fx: &'a Fx = self.p.fx;
        if self.flags & F_HIT_PAIR == 0 {
            return;
        }
        let hp_cfg = fx.s.hit_pair_proc.as_ref().expect("hit_pair_proc");
        self.st.eclipse += 1;
        if self.st.eclipse >= 2 {
            self.st.eclipse = 0;
            let dt = hp_cfg.dtype;
            let amt = self.eclipse_amt;
            self.deal(amt, dt, SRC_ECLIPSE, false, false, 1.0);
        }
    }

    /// Basic-ability cooldown after haste (Shojin) and Actualizer's window.
    pub fn basic_cd(&self, base_cd: f64) -> f64 {
        let mut cd = base_cd * self.p.sheet.basic_cd_mult;
        if self.flags & F_MANA_ACTIVE != 0 {
            let m = self.p.fx.s.mana_active.as_ref().expect("mana_active");
            if self.st.t < self.st.ma_until {
                cd /= 1.0 + m.basic_cd_faster_pct / 100.0;
            }
        }
        cd
    }

    /// A cast just happened: the next auto waits out its animation.
    pub fn lockout(&mut self) {
        self.st.next_attack = pymax(self.st.next_attack, self.st.t) + ABILITY_LOCKOUT_S;
    }

    /// Everything riding a basic attack hit (reapplied by a phantom hit).
    fn apply_onhits<D: Driver>(&mut self, drv: &mut D) {
        // indexed, not zipped: `self.deal` needs `&mut self`, which an
        // iterator borrowed out of `self.p.onhits` would hold
        for i in 0..self.p.onhits.len() {
            let (amt, dtype, source) = self.p.onhits[i];
            self.deal(amt, dtype, source, false, false, 1.0);
        }
        drv.attack_riders(self);
        for i in 0..self.p.onhits_current.len() {
            let (pct, dtype) = self.p.onhits_current[i];
            let amt = pct * pymax(self.st.hp, 0.0);
            self.deal(amt, dtype, SRC_BOTRK, false, false, 1.0);
        }
    }

    fn do_attack<D: Driver>(&mut self, drv: &mut D) {
        let fx: &'a Fx = self.p.fx;
        let s = &fx.s;
        let t = self.st.t;
        self.st.attacks += 1;
        if self.flags & F_NAVORI != 0 {
            // on-attack: shave 15% off remaining basic CDs
            drv.shave_cooldowns(&mut self.st, t, self.p.navori_factor);
        }
        if self.flags & F_FLURRY != 0 {
            let f = s.flurry.as_ref().expect("flurry");
            if t >= self.st.flurry_ready {
                self.st.flurry_until = t + f.duration_s;
                self.st.flurry_ready = t + f.cooldown_s;
            }
            // on-hit refund (1s, 2s on crit -> EV blend) pulls the next window in
            self.st.flurry_ready -= f.refund_on_hit_s + self.p.crit_c * f.refund_crit_extra_s;
        }
        // on-attack stack machinery first (pre-hit state decides the procs)
        let mut phantom_now = false;
        if self.flags & F_PHANTOM != 0 {
            let p = s.phantom.as_ref().expect("phantom");
            if self.st.phantom >= p.stacks_needed {
                phantom_now = true;
                self.st.phantom = 0;
            }
        }
        let mut kraken_now = false;
        if self.flags & F_KRAKEN != 0 {
            if self.st.kraken >= 2 {
                kraken_now = true;
                self.st.kraken = 0;
            } else {
                self.st.kraken += 1;
            }
        }
        if self.flags & F_AS_STACKING != 0 {
            let a = s.as_stacking.as_ref().expect("as_stacking");
            self.st.seething = imin(self.st.seething + 1, a.max_stacks);
            // the consuming attack grants no Phantom stack
            if self.flags & F_PHANTOM != 0 {
                let p = s.phantom.as_ref().expect("phantom");
                if !phantom_now && self.st.seething == a.max_stacks {
                    self.st.phantom = imin(self.st.phantom + 1, p.stacks_needed);
                }
            }
        }
        if self.flags & F_ALT_PEN != 0 {
            let a = s.alt_pen.as_ref().expect("alt_pen");
            if self.st.attacks % 2 == 0 {
                // every other hit is a Dark hit
                self.st.dark = imin(self.st.dark + 1, a.max_stacks);
            }
        }
        drv.before_attack(self);

        let mut floor = 1.0;
        if self.flags & F_SUNDERED != 0 {
            let sundered = s.first_attack_crit_floor_ev.expect("first_attack_crit_floor_ev");
            if !self.st.sundered_used {
                self.st.sundered_used = true; // Sundered Sky: once per target
                floor = sundered;
            }
        }
        if self.flags & F_ULT_STEROID != 0 {
            let u = s.ult_attack_steroid.as_ref().expect("ult_attack_steroid");
            if self.st.post_r_attacks < u.attacks {
                floor = pymax(floor, u.crit_floor_ev);
            }
        }
        let ad = self.p.ad;
        self.deal(ad, DType::Physical, SRC_AUTO, true, false, floor);
        self.apply_onhits(drv);
        if self.flags & F_ENERGIZED != 0 {
            self.st.energize += (t - self.st.en_last) * self.p.move_speed / 24.0;
            self.st.en_last = t;
            self.st.energize += self.p.energize_per_attack;
            if self.st.energize >= 100.0 {
                self.st.energize -= 100.0;
                // `fx` is borrowed out of `self` as `&'a Fx`, so iterating it
                // does not conflict with `self.deal`
                for en in &fx.energized {
                    let (bonus, dt, src) = (en.bonus, en.dtype, en.source);
                    self.deal(bonus, dt, src, false, false, 1.0);
                }
            }
        }
        self.eclipse_hit();
        if self.flags & F_NTH_HIT != 0 {
            let n = s.nth_hit_proc.as_ref().expect("nth_hit_proc");
            if self.st.nth >= self.p.nth_need {
                self.st.nth = 0;
                let (dmg, dt) = (self.p.nth_dmg, n.dtype);
                self.deal(dmg, dt, SRC_HULLBREAKER, false, false, 1.0);
            } else {
                self.st.nth += 1;
            }
        }
        if self.flags & F_FIRST_ATTACK != 0 {
            let fb = s.first_attack_bonus.as_ref().expect("first_attack_bonus");
            if self.st.attacks == 1 {
                // Umbral: opens from unseen
                let amt = self.p.first_attack_amt;
                self.deal(amt, fb.dtype, fb.source, false, false, 1.0);
            }
        }
        self.st.post_r_attacks += 1;
        if self.st.sb_primed {
            let sb = s.spellblade.as_ref().expect("primed spellblade");
            let (dmg, dt, icd, reapply) = (self.p.spellblade_dmg, sb.dtype, sb.icd_s, sb.reapply_onhit);
            self.deal(dmg, dt, SRC_SPELLBLADE, false, false, 1.0);
            self.st.sb_primed = false;
            self.st.sb_icd_until = t + icd;
            if reapply {
                // Dusk and Dawn: on-hits land twice
                self.apply_onhits(drv);
            }
        }
        if kraken_now {
            let k = s.kraken.as_ref().expect("kraken");
            let missing = 1.0 - pymax(self.st.hp, 0.0) / self.target_hp;
            let amt = self.p.kraken_base * (1.0 + self.p.kraken_amp_frac * missing);
            self.deal(amt, k.dtype, SRC_KRAKEN, false, false, 1.0);
        }
        drv.after_attack(self);
        if phantom_now {
            self.st.phantom_hits += 1;
            self.apply_onhits(drv);
        }
        drv.schedule_attack(self);
    }

    fn burn_tick(&mut self, idx: usize) {
        let fx: &'a Fx = self.p.fx;
        let b = &fx.burns[idx];
        let tick = self.burn_amts[idx];
        let (dt, src, tick_s) = (b.dtype, b.source, b.tick_s);
        self.deal(tick, dt, src, false, false, 1.0);
        let t = self.st.t;
        let bs = &mut self.burns[idx];
        bs.next = if t + tick_s <= bs.until { t + tick_s } else { INF };
    }

    fn breakdown_out(&self) -> Vec<(SourceId, f64)> {
        let mut out: Vec<(SourceId, f64)> =
            self.bd_order.iter().map(|&s| (s, self.bd[s as usize])).collect();
        // dict(sorted(items, key=lambda kv: -kv[1])): stable, best first
        out.sort_by(|a, b| (-a.1).partial_cmp(&(-b.1)).unwrap());
        out
    }
}


impl<'a> Prep<'a> {
    /// One fight of this build against `target`. `drv` is the class's driver:
    /// it is reset to the state `Driver::new` left it in before anything
    /// moves, so one driver serves every fight of the class. `log` is
    /// Stormsurge's scratch buffer, handed back before this returns.
    ///
    /// `opts.prestacked` is only carried through to the counterfactual leg:
    /// the driver it would build was already built, with the `prestacked`
    /// this `Prep` was prepared for.
    pub fn fight<D: Driver>(&self, drv: &mut D, target: &Target, opts: Opts,
                            log: &mut Vec<(f64, f64)>)
        -> Result<Option<FightResult>, String> {
        drv.reset();
        self.fight_ex::<D>(drv, target, opts, false, log)
    }

    /// `no_execute` is the blend's counterfactual leg (see `Engine::new`). It
    /// is deliberately not a field of `Opts`: `Opts` is the shape the Python
    /// wrapper and the enumerator construct, and neither should have to know
    /// about it. `drv` arrives already reset.
    fn fight_ex<D: Driver>(&self, drv: &mut D, target: &Target, opts: Opts, no_execute: bool,
                           log: &mut Vec<(f64, f64)>)
        -> Result<Option<FightResult>, String> {
        #[cfg(debug_assertions)]
        check_rank_order();
        let fx: &'a Fx = self.fx;
        log.clear();
        let mut e = Engine::new(self, target, opts.breakdown, no_execute, std::mem::take(log));
        // opening casts at t=0, before the first auto
        if opts.use_ult && self.ranks.r > 0 {
            e.st.r_impact = self.r_delay_s.ok_or("kit R needs delayS")?;
            e.prime_spellblade();
            e.st.next_attack += ABILITY_LOCKOUT_S;
            if let Some(u) = &fx.s.on_ult_cast {
                // Hexplate Overdrive starts on cast
                e.st.hex_until = u.duration_s;
            }
            if fx.s.ult_attack_steroid.is_some() {
                // Fiendhunter's next-3-attacks window
                e.st.post_r_attacks = 0;
            }
            drv.cast_r(&mut e);
        }
        // item actives (Rocketbelt, Gunblade, hydra actives) fire on engage
        for i in 0..self.actives_once.len() {
            let (amt, dtype, source) = self.actives_once[i];
            e.deal(amt, dtype, source, false, false, 1.0);
        }

        let duration = target.duration;
        let stop_after = opts.stop_after;
        // an unbounded fight can never trip the cut: `t_next > stop_after` with
        // `stop_after >= duration` implies `t_next > duration`, which broke out
        // one line earlier
        let bounded = stop_after < duration;
        let has_mal = e.flags & F_ULT_BURN != 0;
        let has_ss = e.flags & F_STORMSURGE != 0;
        let n_burns = self.n_burns;
        let mut evs = [(INF, Kind::ECharge); 2];
        loop {
            // the next event: the earliest of everything scheduled; at the same
            // instant, the kind that sorts first, then the earlier burn. The
            // ordinal is packed (see `rank_of`) so the tie-break is an integer
            // compare rather than a 16-byte enum one; the two float comparisons
            // are exactly the ones this loop always made.
            let mut t_next = e.st.next_attack;
            let mut best = R_ATTACK;
            if has_mal {
                // Without Malignance `next_mal` is INF for the whole fight, so
                // this candidate could only win a tie at t_next == INF — and the
                // loop breaks on `t_next > duration` before dispatching that.
                let x = e.st.next_mal;
                if x < t_next || (x == t_next && R_MAL < best) {
                    t_next = x;
                    best = R_MAL;
                }
            }
            for i in 0..n_burns {
                let x = e.burns[i].next;
                let k = R_BURN | i as u32;
                if x < t_next || (x == t_next && k < best) {
                    t_next = x;
                    best = k;
                }
            }
            if has_ss {
                // as for Mal: `ss_at` stays INF without Stormsurge
                let x = e.st.ss_at;
                if x < t_next || (x == t_next && R_SS < best) {
                    t_next = x;
                    best = R_SS;
                }
            }
            let x = e.st.r_impact;
            if x < t_next || (x == t_next && R_R < best) {
                t_next = x;
                best = R_R;
            }
            // so Q casts the moment it's ready, not at the next event
            let q_at = drv.q_at(&e);
            if q_at < t_next || (q_at == t_next && R_Q < best) {
                t_next = q_at;
                best = R_Q;
            }
            let n = drv.events(&e, &mut evs);
            for &(x, k) in &evs[..n] {
                let k = rank_of(k);
                if x < t_next || (x == t_next && k < best) {
                    t_next = x;
                    best = k;
                }
            }
            if t_next > duration || e.st.hp <= 0.0 {
                break;
            }
            if bounded && t_next > stop_after {
                *log = std::mem::take(&mut e.dmg_log);
                return Ok(None);
            }
            e.st.t = t_next;
            // castable now? q_at only grows with the clock
            if q_at <= t_next {
                drv.cast_q(&mut e);
                if best == R_ATTACK && e.st.next_attack > e.st.t {
                    continue; // the lockout pushed this auto; re-pick the next event
                }
            }
            // the same arms as `match kind`, on the packed ordinal: `kind_of` is
            // only needed for the kinds the driver handles
            match best & !0xFFFFu32 {
                R_ATTACK => e.do_attack(drv),
                R_BURN => e.burn_tick((best & 0xFFFF) as usize),
                R_SS => {
                    e.st.ss_at = INF;
                    e.st.ss_done = true;
                    let ss = fx.s.stormsurge.as_ref().expect("stormsurge");
                    let amt = self.stormsurge_amt;
                    e.deal(amt, ss.dtype, SRC_STORMSURGE, false, false, 1.0);
                }
                R_MAL => {
                    let tick = e.st.mal_tick;
                    e.deal(tick, DType::Magic, SRC_MALIGNANCE, false, false, 1.0);
                    let t = e.st.t;
                    e.st.next_mal = if t + 0.25 <= e.st.mal_until { t + 0.25 } else { INF };
                }
                R_R => {
                    e.st.r_impact = INF;
                    let dmg = self.r_dmg.ok_or("kit R needs damage")?;
                    e.deal(dmg, DType::Magic, SRC_R, false, true, 1.0);
                    e.ability_cast_proc();
                    e.eclipse_hit();
                    if let Some(h) = &fx.s.hypershot {
                        // R is always a 600+ range cast
                        let t = e.st.t;
                        e.st.hz_until = pymax(e.st.hz_until, t + h.duration_s);
                    }
                    if let Some(ub) = &fx.s.ult_burn {
                        let t = e.st.t;
                        e.st.mal_tick = self.mal_tick_amt;
                        e.st.mal_until = t + ub.duration_s;
                        e.st.mal_shred_until = t + ub.duration_s;
                        e.st.next_mal = t + 0.25;
                    }
                }
                R_Q => {}
                _ => drv.on_event(&mut e, kind_of(best)),
            }
        }
        // the Stormsurge window is dead from here on; hand its buffer back so
        // the next fight of this class reuses the allocation
        *log = std::mem::take(&mut e.dmg_log);

        // Expected kill time: blend the executing timeline with the one where
        // the window was missed, weighted by how often real crit would land in it.
        // the one place the fight's "not yet" values become `Option`s again
        let st_ttk = if e.st.set & S_TTK != 0 { Some(e.st.ttk) } else { None };
        let st_ttk_eff = if e.st.set & S_TTK_EFF != 0 { Some(e.st.ttk_eff) } else { None };
        let st_exec_p = if e.st.set & S_EXEC_P != 0 { Some(e.st.exec_p) } else { None };
        let mut ttk_exp = st_ttk;
        let p_eff = match st_exec_p {
            Some(p) if p != 0.0 => p,
            _ => 1.0,
        };
        if opts.blend && st_ttk.is_some() && p_eff < 1.0 {
            // the same build, run with the execute switched off: nulling
            // `fx.s.execute_pct` only ever reached `exec_hp` and `deal`'s guard
            drv.reset();
            let alt = self.fight_ex::<D>(drv, target,
                                         Opts { use_ult: opts.use_ult,
                                                prestacked: opts.prestacked,
                                                stop_after: INF, breakdown: false, blend: false },
                                         true, log)?
                .expect("an unbounded fight always returns");
            let p = st_exec_p.unwrap();
            ttk_exp = Some(p * st_ttk.unwrap()
                + (1.0 - p) * (match alt.ttk { Some(x) => x, None => duration }));
        }
        let fight = match st_ttk {
            Some(ttk) => pymin(duration, ttk),
            None => duration,
        };
        let total = e.st.total;
        Ok(Some(FightResult {
            total,
            dps: if fight != 0.0 { total / fight } else { 0.0 },
            ttk: st_ttk,
            ttk_eff: st_ttk_eff,
            ttk_exp,
            attacks: e.st.attacks,
            phantom_hits: e.st.phantom_hits,
            hp_left: pymax(e.st.hp, 0.0),
            breakdown: e.breakdown_out(),
        }))
    }
}

/// A build's driver and its `Prep`, built together: the driver settles
/// `ranged` and the attack range the `Prep` needs.
pub fn prepare<'a, D: Driver>(sheet: &'a Sheet, kit: &'a Kit, fx: &'a Fx, level: i64,
                              ranks: Ranks, prestacked: bool) -> Result<(Prep<'a>, D), String> {
    let drv = D::new(kit, sheet, level, ranks, prestacked)?;
    let prep = Prep::build(sheet, kit, fx, level, ranks, drv.ranged(), drv.attack_range())?;
    Ok((prep, drv))
}

/// The rotation a build fights with, picked once from the kit. The enum keeps
/// the fight bodies monomorphised without dragging the driver type through
/// the enumerator's own loops.
#[derive(Clone)]
enum Rotation {
    Kayle(crate::drivers::KayleDriver),
    Vladimir(crate::drivers::VladimirDriver),
}

/// One build's fights: the target-independent setup and its driver, built
/// once and run against each target in turn.
pub struct Sim<'a> {
    prep: Prep<'a>,
    drv: Rotation,
    /// Debug-only: the driver as `Driver::new` built it, to check `reset`
    /// against once a fight has actually moved it (see `check_reset`).
    #[cfg(debug_assertions)]
    fresh: Rotation,
}

impl<'a> Sim<'a> {
    pub fn new(sheet: &'a Sheet, kit: &'a Kit, fx: &'a Fx, level: i64, ranks: Ranks,
               prestacked: bool) -> Result<Sim<'a>, String> {
        let (prep, drv) = match kit.driver {
            Some(crate::kit::DriverId::Kayle) => {
                let (p, d) = prepare::<crate::drivers::KayleDriver>(sheet, kit, fx, level, ranks,
                                                                    prestacked)?;
                (p, Rotation::Kayle(d))
            }
            Some(crate::kit::DriverId::Vladimir) => {
                let (p, d) = prepare::<crate::drivers::VladimirDriver>(sheet, kit, fx, level,
                                                                       ranks, prestacked)?;
                (p, Rotation::Vladimir(d))
            }
            None => return Err(no_driver(kit)),
        };
        #[cfg(debug_assertions)]
        let fresh = drv.clone();
        Ok(Sim {
            prep,
            drv,
            #[cfg(debug_assertions)]
            fresh,
        })
    }

    /// One fight against `target`. The driver is reset first, so a `Sim` runs
    /// its targets in any order and as often as the caller likes. `log` is
    /// Stormsurge's scratch: pass the same buffer every time and the whole
    /// enumeration grows it once. `opts.prestacked` has no effect here — the
    /// driver it decides was settled by `Sim::new`.
    pub fn fight(&mut self, target: &Target, opts: Opts, log: &mut Vec<(f64, f64)>)
        -> Result<Option<FightResult>, String> {
        let prep = &self.prep;
        let r = match &mut self.drv {
            Rotation::Kayle(d) => prep.fight(d, target, opts, log),
            Rotation::Vladimir(d) => prep.fight(d, target, opts, log),
        };
        #[cfg(debug_assertions)]
        self.check_reset();
        r
    }

    /// Debug-only: the fight just moved the driver, so resetting it here and
    /// comparing it with the copy `Driver::new` produced catches the one way
    /// reusing a driver across a class could go wrong — a field that a fight
    /// writes but `reset` does not restore.
    #[cfg(debug_assertions)]
    fn check_reset(&mut self) {
        match (&mut self.drv, &self.fresh) {
            (Rotation::Kayle(d), Rotation::Kayle(f)) => {
                d.reset();
                debug_assert!(d == f, "KayleDriver::reset left a field behind:\n{d:?}\n{f:?}");
            }
            (Rotation::Vladimir(d), Rotation::Vladimir(f)) => {
                d.reset();
                debug_assert!(d == f, "VladimirDriver::reset left a field behind:\n{d:?}\n{f:?}");
            }
            _ => unreachable!("the rotation never changes"),
        }
    }
}

fn no_driver(kit: &Kit) -> String {
    let other = &kit.champion;
    format!("no engine driver for '{other}' — a kit encoding needs matching \
             rotation logic in engine/src/drivers.rs")
}

/// One fight of a build against a stat dummy: `None` when the clock passed
/// `stop_after` with the dummy still standing.
pub fn simulate(sheet: &Sheet, kit: &Kit, fx: &Fx, level: i64, ranks: Ranks, target: &Target,
                opts: Opts) -> Result<Option<FightResult>, String> {
    match kit.driver {
        Some(crate::kit::DriverId::Kayle) =>
            simulate_with::<crate::drivers::KayleDriver>(sheet, kit, fx, level, ranks, target, opts),
        Some(crate::kit::DriverId::Vladimir) =>
            simulate_with::<crate::drivers::VladimirDriver>(sheet, kit, fx, level, ranks, target,
                                                            opts),
        None => Err(no_driver(kit)),
    }
}

/// `simulate` with the driver named at the call site, for a caller that
/// knows it and wants no dispatch at all.
pub fn simulate_with<D: Driver>(sheet: &Sheet, kit: &Kit, fx: &Fx, level: i64, ranks: Ranks,
                                target: &Target, opts: Opts)
    -> Result<Option<FightResult>, String> {
    let (prep, mut drv) = prepare::<D>(sheet, kit, fx, level, ranks, opts.prestacked)?;
    let mut log = Vec::new();
    prep.fight(&mut drv, target, opts, &mut log)
}


pub const DRIVERS: [&str; 2] = ["kayle", "vladimir"];
