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

/// The engine's fight state (the `st` dict).
#[derive(Clone, Debug)]
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
    pub kit_amp_until: f64,
    pub combat_t0: Option<f64>,
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
    pub ttk: Option<f64>,
    pub ttk_eff: Option<f64>,
    pub exec_p: Option<f64>,
}

#[derive(Clone, Copy, Debug)]
struct BurnState {
    until: f64,
    next: f64,
}

pub struct Engine<'a> {
    pub sheet: &'a Sheet,
    pub fx: &'a Fx,
    pub kit: &'a Kit,
    pub level: i64,
    pub ranks: Ranks,
    pub target_hp: f64,
    target_armor: f64,
    target_mr: f64,
    pub duration: f64,
    target_bonus_hp: f64,
    breakdown: bool,
    pub ranged: bool,
    crit_c: f64,
    crit_ev: f64,
    auto_amp: f64,
    sheet_bonus_as: f64,
    base_as: f64,
    as_ratio: f64,
    lethality: f64,
    armor_pen_pct: f64,
    magic_pen_pct: f64,
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
    exec_hp: f64,
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
    onhits: Vec<(f64, DType, SourceId)>,
    onhits_current: Vec<(f64, DType)>,
    ad: f64,
    move_speed: f64,
    energize_per_attack: f64,
    kraken_base: f64,
    kraken_amp: f64,
    nth_need: i64,
    nth_dmg: f64,
    spellblade_dmg: f64,
    pub st: St,
    burns: Vec<BurnState>,
    dmg_log: Vec<(f64, f64)>,
    bd: Vec<f64>,
    bd_seen: Vec<bool>,
    bd_order: Vec<SourceId>,
}

/// One kit's rotation: the hooks the engine calls at each point of a fight.
pub trait Driver: Sized {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, prestacked: bool)
        -> Result<Self, String>;
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
        e.st.next_attack = e.st.t + 1.0 / e.attack_speed(b);
    }
    /// Earliest moment Q can be cast; INF when it can't be.
    fn q_at(&self, e: &Engine) -> f64 {
        if e.ranks.q == 0 {
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

impl<'a> Engine<'a> {
    pub fn new(sheet: &'a Sheet, kit: &'a Kit, fx: &'a Fx, level: i64, ranks: Ranks,
               target: &Target, breakdown: bool, ranged: bool, atk_range: f64) -> Engine<'a> {
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
        let exec_hp = match s.execute_pct {
            Some(p) => p / 100.0 * target.hp,
            None => 0.0,
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
        // on-hit damage is fixed by the sheet: work each entry out once
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
        let mut st = St {
            t: 0.0,
            hp: target.hp,
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
            kit_amp_until: -1.0,
            combat_t0: None,
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
            ev_hp0: target.hp,
            ev_dmg: 0.0,
            prev_ev_t: 0.0,
            total: 0.0,
            ttk: None,
            ttk_eff: None,
            exec_p: None,
        };
        if kit.attack_never {
            // a kit played without autos: nothing that rides an attack ever fires
            st.next_attack = INF;
        }
        if let Some(m) = &s.mana_active {
            // Actualizer: cast on engage, empowered for 8s
            st.ma_until = m.duration_s;
        }
        let n_src = source_count();
        Engine {
            sheet,
            fx,
            kit,
            level,
            ranks,
            target_hp: target.hp,
            target_armor: target.armor,
            target_mr: target.mr,
            duration: target.duration,
            target_bonus_hp: target.bonus_hp,
            breakdown,
            ranged,
            crit_c,
            crit_ev,
            auto_amp,
            sheet_bonus_as: sheet.bonus_as_pct,
            base_as: sheet.base_as,
            as_ratio: sheet.as_ratio,
            lethality: sheet.lethality,
            armor_pen_pct: sheet.armor_pen_pct,
            magic_pen_pct: sheet.magic_pen_pct,
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
            exec_hp,
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
            onhits,
            onhits_current,
            ad: sheet.ad,
            move_speed: sheet.move_speed,
            energize_per_attack,
            kraken_base,
            kraken_amp,
            nth_need,
            nth_dmg,
            spellblade_dmg,
            st,
            burns: fx.burns.iter().map(|_| BurnState { until: -1.0, next: INF }).collect(),
            dmg_log: Vec::new(),
            bd: if breakdown { vec![0.0; n_src] } else { Vec::new() },
            bd_seen: if breakdown { vec![false; n_src] } else { Vec::new() },
            bd_order: Vec::new(),
        }
    }

    pub fn attack_speed(&self, kit_bonus: f64) -> f64 {
        let s = &self.fx.s;
        let st = &self.st;
        let mut bonus = self.sheet_bonus_as + kit_bonus;
        if let Some(a) = &s.as_stacking {
            bonus += st.seething as f64 * a.pct_per_stack;
        }
        if let Some(f) = &s.flurry {
            if st.t < st.flurry_until {
                bonus += f.as_pct;
            }
        }
        if let Some(u) = &s.on_ult_cast {
            if st.t < st.hex_until {
                bonus += u.as_pct;
            }
        }
        if let Some(u) = &s.ult_attack_steroid {
            if st.post_r_attacks < u.attacks {
                bonus += u.as_pct;
            }
        }
        pymin(self.base_as + self.as_ratio * bonus / 100.0, AS_CAP)
    }

    /// A build's base amp by damage type after `secs` whole seconds in combat.
    fn base_amp(&self, dt: DType, secs: i64) -> f64 {
        let mut amp = 1.0;
        for a in &self.fx.dmg_amps {
            amp *= 1.0 + a.pct_per_stack / 100.0 * imin(a.max_stacks, secs) as f64;
        }
        for a in &self.fx.flat_amps {
            // Abyssal Mask: always-on, one damage type
            if a.dtype.is_none() || a.dtype == Some(dt) {
                amp *= 1.0 + a.pct / 100.0;
            }
        }
        if let Some(max_pct) = self.fx.s.giant_slayer {
            // 1% per 100 target bonus HP, capped
            amp *= 1.0 + pymin(max_pct, self.target_bonus_hp / 100.0) / 100.0;
        }
        amp
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

    pub fn deal(&mut self, amount: f64, dtype: DType, source: SourceId, crit_mod: bool,
                ability: bool, ev_floor: f64) {
        let fx: &'a Fx = self.fx;
        let s = &fx.s;
        let t = self.st.t;
        if self.st.combat_t0.is_none() {
            // the first damage dealt opens combat
            self.st.combat_t0 = Some(t);
        }
        // Liandry's Suffering, Riftmaker's Void Corruption: a stack per whole
        // second in combat, with a hair of tolerance at second boundaries
        let secs = if self.has_dmg_amps {
            (t - self.st.combat_t0.unwrap() + 1e-9) as i64
        } else {
            0
        };
        let mut amp = self.base_amp(dtype, secs);
        if s.hypershot.is_some() && t < self.st.hz_until {
            amp *= self.hyper_amp;
        }
        if t <= self.st.kit_amp_until && dtype != DType::True {
            amp *= 1.0 + self.st.kit_amp_pct / 100.0;
        }
        // "increased basic damage" is the attack itself
        if source == SRC_AUTO {
            amp *= self.auto_amp;
        }
        if ability {
            if s.ability_amp_stacking.is_some() {
                // Shojin's Focused Will
                amp *= 1.0 + self.shojin_per * self.st.shojin as f64;
            }
            if s.mana_active.is_some() && t < self.st.ma_until {
                amp *= self.mana_amp;
            }
        }
        let qs_on = t < self.st.shred_until;
        let mult = match dtype {
            DType::Physical => {
                let dark_pen = if s.alt_pen.is_some() {
                    imin(self.st.dark, self.alt_max) as f64 * self.alt_per
                } else {
                    0.0
                };
                let mut shred = if qs_on { self.shred_armor } else { 0.0 };
                if s.armor_shred.is_some() {
                    // Black Cleaver: % armor reduction stacks
                    shred += self.st.cleaver as f64 * self.cleaver_per;
                }
                let mut leth = self.lethality;
                if s.opener_lethality.is_some() && t < self.opener_until {
                    leth += self.opener_leth;
                }
                resist_mult(eff_resist(self.target_armor, 0.0, shred,
                                       stack_pct_pen(self.armor_pen_pct, dark_pen), leth))
            }
            DType::True => 1.0,
            DType::Magic => {
                let dark_pen = if s.alt_pen.is_some() {
                    imin(self.st.dark, self.alt_max) as f64 * self.alt_per
                } else {
                    0.0
                };
                let mal = if s.ult_burn.is_some() && t < self.st.mal_shred_until {
                    self.mal_reduction
                } else {
                    0.0
                };
                let mut shred = if qs_on { self.shred_mr } else { 0.0 };
                if s.mr_shred.is_some() {
                    // Bloodletter's Curse: % MR reduction stacks
                    shred += self.st.blood as f64 * self.blood_per;
                }
                resist_mult(eff_resist(self.target_mr, mal, shred,
                                       stack_pct_pen(self.magic_pen_pct, dark_pen),
                                       self.magic_pen_flat))
            }
        };
        // Cinderbloom is a deterministic crit below the HP threshold
        let mut hp = self.st.hp;
        let below = s.magic_crit.is_some() && dtype == DType::Magic
            && hp / self.target_hp * 100.0 < self.crit_below;
        let mut ev = 1.0;
        if crit_mod {
            ev = self.crit_ev;
            if below {
                ev = self.crit_c * self.crit_damage / 100.0
                    + (1.0 - self.crit_c) * self.crit_below_pct / 100.0;
            }
            if ev < ev_floor {
                // guaranteed-crit attacks (Sundered Sky)
                ev = ev_floor;
            }
        } else if below {
            ev = self.crit_below_ev;
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
        if dtype == DType::Physical && s.armor_shred.is_some() {
            let c = self.st.cleaver + 1;
            self.st.cleaver = if c < self.cleaver_max { c } else { self.cleaver_max };
        }
        if ability {
            if s.mr_shred.is_some() && dtype == DType::Magic {
                let b = self.st.blood + 1;
                self.st.blood = if b < self.blood_max { b } else { self.blood_max };
            }
            if s.ability_amp_stacking.is_some() {
                let sh = self.st.shojin + 1;
                self.st.shojin = if sh < self.shojin_max { sh } else { self.shojin_max };
            }
            if let Some(h) = &s.hypershot {
                if !self.st.hz_first {
                    // the opening cast is the one made from 600+ range
                    self.st.hz_first = true;
                    self.st.hz_until = t + h.duration_s;
                }
            }
            for i in 0..self.burns.len() {
                let b = &fx.burns[i];
                let sb = &mut self.burns[i];
                if sb.next == INF {
                    sb.next = t + b.tick_s;
                }
                sb.until = t + b.duration_s;
            }
            if let Some(once) = &s.ability_proc_once {
                if !self.st.once_done {
                    self.st.once_done = true;
                    let amt = once.base + once.ap_ratio * self.sheet.ap;
                    self.deal(amt, once.dtype, once.source, false, false, 1.0);
                }
            }
        }
        hp = self.st.hp;
        if let Some(storm) = &s.stormsurge {
            if !self.st.ss_done && self.st.ss_at == INF {
                self.dmg_log.push((t, dmg));
                let window = t - storm.window_s;
                let recent = fsum(self.dmg_log.iter().filter(|(tt, _)| *tt >= window).map(|(_, d)| *d));
                if recent >= storm.threshold_pct / 100.0 * self.target_hp {
                    self.st.ss_at = t + storm.delay_s;
                }
            }
        }
        let mut exec_amt = 0.0;
        if s.execute_pct.is_some() && self.st.ttk.is_none() && 0.0 < hp && hp <= self.exec_hp {
            exec_amt = hp;
            if self.breakdown {
                self.record(SRC_EXECUTE, exec_amt);
            }
            self.st.total += exec_amt;
            self.st.ev_dmg += exec_amt;
            self.st.hp = 0.0;
            hp = 0.0;
        }
        if hp <= 0.0 && self.st.ttk.is_none() {
            self.st.ttk = Some(t);
            // Effective (ranking) kill time: interpolate back over the gap
            // by the fraction of the batch actually needed.
            let frac = if self.st.ev_dmg > 0.0 {
                pymin(1.0, self.st.ev_hp0 / self.st.ev_dmg)
            } else {
                1.0
            };
            self.st.ttk_eff = Some(self.st.prev_ev_t + frac * (t - self.st.prev_ev_t));
            if exec_amt > 0.0 {
                let batch = self.st.ev_dmg - exec_amt;
                self.st.exec_p = Some(if batch > 0.0 { pymin(1.0, self.exec_hp / batch) } else { 1.0 });
            }
        }
    }

    pub fn prime_spellblade(&mut self) {
        if self.fx.s.spellblade.is_some() && self.st.t >= self.st.sb_icd_until {
            self.st.sb_primed = true;
        }
    }

    /// Muramana's Shock: bonus physical damage per damaging ability cast.
    pub fn ability_cast_proc(&mut self) {
        let fx: &'a Fx = self.fx;
        if let Some(mp) = &fx.s.ability_mana_proc {
            let amt = mp.pct_by_level.at(self.level) / 100.0 * self.sheet.mana;
            let dt = mp.dtype;
            self.deal(amt, dt, SRC_MURAMANA, false, false, 1.0);
        }
    }

    /// Eclipse: attacks and damaging casts each grant one stack; every 2nd
    /// stack procs (% max HP).
    pub fn eclipse_hit(&mut self) {
        let fx: &'a Fx = self.fx;
        let Some(hp_cfg) = &fx.s.hit_pair_proc else {
            return;
        };
        self.st.eclipse += 1;
        if self.st.eclipse >= 2 {
            self.st.eclipse = 0;
            let pct = if self.ranged { hp_cfg.max_hp_pct_ranged } else { hp_cfg.max_hp_pct_melee };
            let dt = hp_cfg.dtype;
            self.deal(pct / 100.0 * self.target_hp, dt, SRC_ECLIPSE, false, false, 1.0);
        }
    }

    /// Basic-ability cooldown after haste (Shojin) and Actualizer's window.
    pub fn basic_cd(&self, base_cd: f64) -> f64 {
        let mut cd = base_cd * self.sheet.basic_cd_mult;
        if let Some(m) = &self.fx.s.mana_active {
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
        for i in 0..self.onhits.len() {
            let (amt, dtype, source) = self.onhits[i];
            self.deal(amt, dtype, source, false, false, 1.0);
        }
        drv.attack_riders(self);
        for i in 0..self.onhits_current.len() {
            let (pct, dtype) = self.onhits_current[i];
            let amt = pct * pymax(self.st.hp, 0.0);
            self.deal(amt, dtype, SRC_BOTRK, false, false, 1.0);
        }
    }

    fn do_attack<D: Driver>(&mut self, drv: &mut D) {
        let fx: &'a Fx = self.fx;
        let s = &fx.s;
        let t = self.st.t;
        self.st.attacks += 1;
        if let Some(navori) = s.navori_cdr {
            // on-attack: shave 15% off remaining basic CDs
            let factor = 1.0 - navori / 100.0;
            drv.shave_cooldowns(&mut self.st, t, factor);
        }
        if let Some(f) = &s.flurry {
            if t >= self.st.flurry_ready {
                self.st.flurry_until = t + f.duration_s;
                self.st.flurry_ready = t + f.cooldown_s;
            }
            // on-hit refund (1s, 2s on crit -> EV blend) pulls the next window in
            self.st.flurry_ready -= f.refund_on_hit_s + self.crit_c * f.refund_crit_extra_s;
        }
        // on-attack stack machinery first (pre-hit state decides the procs)
        let mut phantom_now = false;
        if let Some(p) = &s.phantom {
            if self.st.phantom >= p.stacks_needed {
                phantom_now = true;
                self.st.phantom = 0;
            }
        }
        let mut kraken_now = false;
        if s.kraken.is_some() {
            if self.st.kraken >= 2 {
                kraken_now = true;
                self.st.kraken = 0;
            } else {
                self.st.kraken += 1;
            }
        }
        if let Some(a) = &s.as_stacking {
            self.st.seething = imin(self.st.seething + 1, a.max_stacks);
            // the consuming attack grants no Phantom stack
            if let Some(p) = &s.phantom {
                if !phantom_now && self.st.seething == a.max_stacks {
                    self.st.phantom = imin(self.st.phantom + 1, p.stacks_needed);
                }
            }
        }
        if let Some(a) = &s.alt_pen {
            if self.st.attacks % 2 == 0 {
                // every other hit is a Dark hit
                self.st.dark = imin(self.st.dark + 1, a.max_stacks);
            }
        }
        drv.before_attack(self);

        let mut floor = 1.0;
        if let Some(sundered) = s.first_attack_crit_floor_ev {
            if !self.st.sundered_used {
                self.st.sundered_used = true; // Sundered Sky: once per target
                floor = sundered;
            }
        }
        if let Some(u) = &s.ult_attack_steroid {
            if self.st.post_r_attacks < u.attacks {
                floor = pymax(floor, u.crit_floor_ev);
            }
        }
        let ad = self.ad;
        self.deal(ad, DType::Physical, SRC_AUTO, true, false, floor);
        self.apply_onhits(drv);
        if !fx.energized.is_empty() {
            self.st.energize += (t - self.st.en_last) * self.move_speed / 24.0;
            self.st.en_last = t;
            self.st.energize += self.energize_per_attack;
            if self.st.energize >= 100.0 {
                self.st.energize -= 100.0;
                for i in 0..fx.energized.len() {
                    let en = &fx.energized[i];
                    let (bonus, dt, src) = (en.bonus, en.dtype, en.source);
                    self.deal(bonus, dt, src, false, false, 1.0);
                }
            }
        }
        self.eclipse_hit();
        if let Some(n) = &s.nth_hit_proc {
            if self.st.nth >= self.nth_need {
                self.st.nth = 0;
                let (dmg, dt) = (self.nth_dmg, n.dtype);
                self.deal(dmg, dt, SRC_HULLBREAKER, false, false, 1.0);
            } else {
                self.st.nth += 1;
            }
        }
        if let Some(fb) = &s.first_attack_bonus {
            if self.st.attacks == 1 {
                // Umbral: opens from unseen
                let amt = fb.base + fb.per_lethality * self.lethality;
                self.deal(amt, fb.dtype, fb.source, false, false, 1.0);
            }
        }
        self.st.post_r_attacks += 1;
        if self.st.sb_primed {
            let sb = s.spellblade.as_ref().expect("primed spellblade");
            let (dmg, dt, icd, reapply) = (self.spellblade_dmg, sb.dtype, sb.icd_s, sb.reapply_onhit);
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
            let amt = self.kraken_base * (1.0 + self.kraken_amp / 100.0 * missing);
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
        let fx: &'a Fx = self.fx;
        let b = &fx.burns[idx];
        let ticks = b.duration_s / b.tick_s;
        let tick = match b.max_hp_pct_total {
            // Liandry's: % of target max HP
            Some(pct) => pct / ticks / 100.0 * self.target_hp,
            // Blackfire Torch: flat + AP ratio, total over the duration
            None => (b.total_base + b.total_ap_ratio * self.sheet.ap) / ticks,
        };
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


fn run<'a, D: Driver>(sheet: &'a Sheet, kit: &'a Kit, fx: &'a Fx, level: i64, ranks: Ranks,
                      target: &Target, opts: Opts) -> Result<Option<FightResult>, String> {
    let mut drv = D::new(kit, sheet, level, ranks, opts.prestacked)?;
    let (ranged, atk_range) = (drv.ranged(), drv.attack_range());
    let mut e = Engine::new(sheet, kit, fx, level, ranks, target, opts.breakdown, ranged, atk_range);
    // opening casts at t=0, before the first auto
    if opts.use_ult && ranks.r > 0 {
        e.st.r_impact = kit.r.delay_s.ok_or("kit R needs delayS")?;
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
    for a in &fx.actives_once {
        let amt = a.base
            + (match a.by_level { Some(b) => b.at(level), None => 0.0 })
            + a.ad_ratio * sheet.ad
            + a.ap_ratio * sheet.ap;
        e.deal(amt, a.dtype, a.source, false, false, 1.0);
    }

    let duration = target.duration;
    let stop_after = opts.stop_after;
    let mut evs = [(INF, Kind::ECharge); 2];
    loop {
        // the next event: the earliest of everything scheduled; at the same
        // instant, the kind that sorts first, then the earlier burn
        let mut t_next = e.st.next_attack;
        let mut kind = Kind::Attack;
        let x = e.st.next_mal;
        if x < t_next || (x == t_next && Kind::Mal < kind) {
            t_next = x;
            kind = Kind::Mal;
        }
        for i in 0..e.burns.len() {
            let x = e.burns[i].next;
            let k = Kind::Burn(i);
            if x < t_next || (x == t_next && k < kind) {
                t_next = x;
                kind = k;
            }
        }
        let x = e.st.ss_at;
        if x < t_next || (x == t_next && Kind::Ss < kind) {
            t_next = x;
            kind = Kind::Ss;
        }
        let x = e.st.r_impact;
        if x < t_next || (x == t_next && Kind::R < kind) {
            t_next = x;
            kind = Kind::R;
        }
        // so Q casts the moment it's ready, not at the next event
        let q_at = drv.q_at(&e);
        if q_at < t_next || (q_at == t_next && Kind::Q < kind) {
            t_next = q_at;
            kind = Kind::Q;
        }
        let n = drv.events(&e, &mut evs);
        for &(x, k) in &evs[..n] {
            if x < t_next || (x == t_next && k < kind) {
                t_next = x;
                kind = k;
            }
        }
        if t_next > duration || e.st.hp <= 0.0 {
            break;
        }
        if t_next > stop_after {
            return Ok(None);
        }
        e.st.t = t_next;
        // castable now? q_at only grows with the clock
        if q_at <= t_next {
            drv.cast_q(&mut e);
            if kind == Kind::Attack && e.st.next_attack > e.st.t {
                continue; // the lockout pushed this auto; re-pick the next event
            }
        }
        match kind {
            Kind::Attack => e.do_attack(&mut drv),
            Kind::Burn(idx) => e.burn_tick(idx),
            Kind::Ss => {
                e.st.ss_at = INF;
                e.st.ss_done = true;
                let ss = fx.s.stormsurge.as_ref().expect("stormsurge");
                let amt = ss.base + ss.ap_ratio * sheet.ap;
                e.deal(amt, ss.dtype, SRC_STORMSURGE, false, false, 1.0);
            }
            Kind::Mal => {
                let tick = e.st.mal_tick;
                e.deal(tick, DType::Magic, SRC_MALIGNANCE, false, false, 1.0);
                let t = e.st.t;
                e.st.next_mal = if t + 0.25 <= e.st.mal_until { t + 0.25 } else { INF };
            }
            Kind::R => {
                e.st.r_impact = INF;
                let dmg = kit.r.damage.as_ref().ok_or("kit R needs damage")?.hit(ranks.r, sheet);
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
                    let ticks = ub.duration_s / 0.25;
                    e.st.mal_tick = (ub.total_base + ub.total_ap_ratio * sheet.ap) / ticks;
                    e.st.mal_until = t + ub.duration_s;
                    e.st.mal_shred_until = t + ub.duration_s;
                    e.st.next_mal = t + 0.25;
                }
            }
            Kind::Q => {}
            other => drv.on_event(&mut e, other),
        }
    }

    // Expected kill time: blend the executing timeline with the one where
    // the window was missed, weighted by how often real crit would land in it.
    let mut ttk_exp = e.st.ttk;
    let p_eff = match e.st.exec_p {
        Some(p) if p != 0.0 => p,
        _ => 1.0,
    };
    if opts.blend && e.st.ttk.is_some() && p_eff < 1.0 {
        let mut fx2 = fx.clone();
        fx2.s.execute_pct = None;
        let alt = run::<D>(sheet, kit, &fx2, level, ranks, target,
                           Opts { use_ult: opts.use_ult, prestacked: opts.prestacked,
                                  stop_after: INF, breakdown: false, blend: false })?
            .expect("an unbounded fight always returns");
        let p = e.st.exec_p.unwrap();
        ttk_exp = Some(p * e.st.ttk.unwrap()
            + (1.0 - p) * (match alt.ttk { Some(x) => x, None => duration }));
    }
    let fight = match e.st.ttk {
        Some(ttk) => pymin(duration, ttk),
        None => duration,
    };
    let total = e.st.total;
    Ok(Some(FightResult {
        total,
        dps: if fight != 0.0 { total / fight } else { 0.0 },
        ttk: e.st.ttk,
        ttk_eff: e.st.ttk_eff,
        ttk_exp,
        attacks: e.st.attacks,
        phantom_hits: e.st.phantom_hits,
        hp_left: pymax(e.st.hp, 0.0),
        breakdown: e.breakdown_out(),
    }))
}

/// One fight of a build against a stat dummy: `None` when the clock passed
/// `stop_after` with the dummy still standing.
pub fn simulate(sheet: &Sheet, kit: &Kit, fx: &Fx, level: i64, ranks: Ranks, target: &Target,
                opts: Opts) -> Result<Option<FightResult>, String> {
    match kit.champion.as_str() {
        "kayle" => run::<crate::drivers::KayleDriver>(sheet, kit, fx, level, ranks, target, opts),
        "vladimir" => run::<crate::drivers::VladimirDriver>(sheet, kit, fx, level, ranks, target, opts),
        other => Err(format!("no engine driver for '{other}' — a kit encoding needs matching \
                              rotation logic in engine/src/drivers.rs")),
    }
}

pub const DRIVERS: [&str; 2] = ["kayle", "vladimir"];
