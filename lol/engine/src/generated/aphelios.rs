//! Aphelios (Patch 16.22). Five weapons, two held: a main weapon that attacks
//! and whose Q is the one castable, an off-hand weapon that every Q borrows,
//! and three queued behind them. Each has 50 Moonlight; the main weapon spends
//! it and is replaced from the queue when it runs out. Phase swaps the two
//! hands. Attacks come first and every cast is woven into the gap after one.
//!
//! Weapon Master's attack damage and attack speed are added here; its
//! lethality has no slot on the stat sheet, so it is applied by lowering the
//! armor the engine mitigates with (see `sync_lethality`).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const CALIBRUM: i64 = 0;
const SEVERUM: i64 = 1;
const GRAVITUM: i64 = 2;
const INFERNUM: i64 = 3;
const CRESCENDUM: i64 = 4;
const WEAPONS: usize = 5;
/// Spectral Chakrams that can be on their way back at once.
const FLIGHTS: usize = 8;

/// At one instant: a finished swap first, then hits already on their way
/// (the locked-on attack, the volley, an Onslaught shot, a sentry hit, a
/// Chakram caught), and only then the next free action.
const EV_PHASE_DONE: u8 = 0;
const EV_R_ATTACK: u8 = 1;
const EV_VOLLEY: u8 = 2;
const EV_ONSLAUGHT: u8 = 3;
const EV_SENTRY: u8 = 4;
const EV_CHAKRAM: u8 = 5;
const EV_ACT: u8 = 6;

/// What the rotation does next (`plan`).
#[derive(Clone, Copy, Debug, PartialEq)]
enum Act {
    Wait,
    CastQ,
    CastR,
    Phase,
}

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    level: i64,
    attack_range: f64,
    q_unlocked: bool,

    // Weapon Master (P): the statistics skill points buy.
    p_ad: f64,
    p_as_pct: f64,
    p_lethality: f64,

    // Moonlight.
    ammo_cap: f64,
    ammo_per_attack: f64,
    ammo_per_cast: f64,
    assembly_s: f64,
    assembly_q_lock_s: f64,

    // Each weapon's Q: its cooldown, and how long the cast holds everything.
    q_cd: [f64; WEAPONS],
    q_lock: [f64; WEAPONS],

    // Calibrum: the mark, and Moonshot.
    mark_duration: f64,
    mark_dmg: f64,
    moonshot_dmg: f64,
    // Severum: Onslaught.
    ons_duration: f64,
    ons_starting: f64,
    ons_as_coeff: f64,
    ons_ad_ratio: f64,
    ons_onhit: f64,
    // Gravitum: the slow, and Binding Eclipse.
    slow_duration: f64,
    eclipse_dmg: f64,
    // Infernum: its attack, and Duskwave.
    infernum_ratio: f64,
    duskwave_dmg: f64,
    volley_delay: f64,
    volley_ad_ratio: f64,
    // Crescendum: Chakrams, the flight, and Sentry.
    ck_first: f64,
    ck_step: f64,
    ck_min: f64,
    ck_max: i64,
    ck_duration: f64,
    cr_linger: f64,
    cr_out_time: f64,
    cr_in_speed: f64,
    cr_in_per_as: f64,
    cr_windup: f64,
    cr_windup_crit: f64,
    sentry_dmg: f64,
    sentry_arm: f64,
    sentry_active: f64,
    sentry_base_as: f64,
    sentry_as_ratio: f64,
    // Phase (W).
    phase_cd: f64,
    phase_swap: f64,
    // Moonlight Vigil (R).
    r_cd: f64,
    r_cast: f64,
    r_dmg: f64,
    r_infernum_bonus: f64,
    r_attack_delay: f64,
    r_attack_ad_ratio: f64,
    r_crit_mod: f64,
    r_mark_bonus: f64,
    r_chakrams: i64,

    src_mark: SourceId,
    src_chakram: SourceId,
    src_moonshot: SourceId,
    src_onslaught: SourceId,
    src_eclipse: SourceId,
    src_duskwave: SourceId,
    src_volley: SourceId,
    src_sentry: SourceId,
    src_r_attack: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    main: i64,
    off: i64,
    queue: [i64; 3],
    ammo: [f64; WEAPONS],
    q_ready: [f64; WEAPONS],
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// The next weapon is being assembled until here: no attack, no cast.
    assembly_until: f64,
    phase_ready: f64,
    phase_done_at: f64,
    last_attack_t: f64,

    // The attack in progress.
    in_attack: bool,
    attack_weapon: i64,
    /// A marked target's attack fired by the off-hand weapon (Calibrum main).
    offhand_attack: bool,
    mark_attack: bool,
    out_of_moonlight: bool,

    mark_until: f64,
    mark_empowered: bool,
    slow_until: f64,

    ons_next: f64,
    ons_left: i64,
    ons_idx: i64,
    ons_step: f64,
    ons_off: i64,

    volley_at: f64,
    volley_weapon: i64,

    sentry_next: f64,
    sentry_end: f64,
    sentry_weapon: i64,
    sentry_first: bool,

    r_ready: f64,
    r_attack_at: f64,
    r_weapon: i64,

    chakrams: i64,
    ck_expire: f64,
    ck_at: [f64; FLIGHTS],
    ck_n: [i64; FLIGHTS],

    // Weapon Master's lethality, as the armor the engine is shown.
    leth_on: bool,
    leth_cleaver: i64,
    leth_dark: i64,
    armor_true: f64,
    armor_shown: f64,
}

impl GenDriver {
    /// The earliest a cast readied at `ready` can start: not before now, not
    /// inside another cast or a weapon's assembly, not into a stasis.
    fn castable_at(&self, e: &Engine, ready: f64) -> f64 {
        pymax(
            pymax(pymax(ready, e.st.t), pymax(self.s.busy_until, self.s.assembly_until)),
            e.hold_until(),
        )
    }

    /// A cast with a cast time just started: no other cast and no attack
    /// until it ends (an attack already due later keeps its time).
    fn busy_for(&mut self, e: &mut Engine, cast_s: f64) {
        self.s.busy_until = e.st.t + cast_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
    }

    // ---- Weapon Master's lethality ------------------------------------

    /// The stat sheet is settled before the driver exists, so the passive's
    /// lethality cannot be added to it. Flat penetration comes last in the
    /// engine's chain, `max(0, armor * (1 - shred) * (1 - pen) - lethality)`,
    /// so the same mitigation follows from showing the engine an armor lower
    /// by `lethality / ((1 - shred) * (1 - pen))`, floored at 0: every source
    /// of physical damage then meets it, item procs included. Shred (Black
    /// Cleaver) and percent pen (Terminus) move during a fight, so this runs
    /// at every hook and rewrites the armor when either has changed. A
    /// defender that moves its own armor (Jak'Sho) is noticed by its number
    /// no longer being the one written here.
    fn sync_lethality(&mut self, e: &mut Engine) {
        if self.p_lethality <= 0.0 {
            return;
        }
        let seen = e.target_armor;
        if !self.s.leth_on || seen != self.s.armor_shown {
            self.s.leth_on = true;
            self.s.armor_true = seen;
            self.s.leth_cleaver = -1;
        }
        let (cleaver, dark) = (e.st.cleaver, e.st.dark);
        if cleaver == self.s.leth_cleaver && dark == self.s.leth_dark {
            return;
        }
        self.s.leth_cleaver = cleaver;
        self.s.leth_dark = dark;
        let fx = &e.p.fx.s;
        let shred = match &fx.armor_shred {
            Some(a) => imin(cleaver, a.max_stacks) as f64 * a.pct_per_stack,
            None => 0.0,
        };
        let dark_pen = match &fx.alt_pen {
            Some(a) => imin(dark, a.max_stacks) as f64 * a.pct_per_stack,
            None => 0.0,
        };
        let pen = stack_pct_pen(e.p.sheet.armor_pen_pct, dark_pen);
        let keep = (1.0 - shred / 100.0) * (1.0 - pen / 100.0);
        let shown = if self.s.armor_true > 0.0 && keep > 0.0 {
            pymax(self.s.armor_true - self.p_lethality / keep, 0.0)
        } else {
            self.s.armor_true
        };
        self.s.armor_shown = shown;
        let mr = e.target_mr;
        e.set_target_resists(shown, mr);
    }

    // ---- stats of the moment ------------------------------------------

    /// Bonus attack speed right now, in percent: the sheet's, the passive's
    /// and whatever the items are running.
    fn bonus_as_pct(&self, e: &Engine) -> f64 {
        let s = &e.p.fx.s;
        let t = e.st.t;
        let mut bonus = e.p.sheet.bonus_as_pct + self.p_as_pct;
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

    /// The seconds between two ordinary attacks right now.
    fn attack_time(&self, e: &Engine) -> f64 {
        let sheet = e.p.sheet;
        let raw = sheet.base_as + sheet.as_ratio * self.bonus_as_pct(e) / 100.0;
        if let Some(d) = &e.dfd {
            if d.as_mult != 1.0 {
                return 1.0 / pymin(raw * d.as_mult, AS_CAP);
            }
        }
        1.0 / pymin(raw, AS_CAP)
    }

    fn ad(&self, e: &Engine) -> f64 {
        e.p.ad + self.p_ad
    }

    fn crit_ev(&self, e: &Engine) -> f64 {
        let sheet = e.p.sheet;
        1.0 + sheet.crit_chance / 100.0 * (sheet.crit_damage / 100.0 - 1.0)
    }

    /// Hexoptics' Magnification, which the engine gives its own attacks:
    /// the attacks abilities make are attacks too.
    fn attack_amp(&self, e: &Engine) -> f64 {
        match &e.p.fx.s.attack_amp {
            Some(aa) => 1.0 + aa.max_pct / 100.0 * pymin(1.0, self.attack_range / aa.max_at_range),
            None => 1.0,
        }
    }

    /// The engine's physical mitigation right now (the armor it is shown
    /// already carries the passive's lethality).
    fn phys_mult(&self, e: &Engine) -> f64 {
        let sheet = e.p.sheet;
        let s = &e.p.fx.s;
        let dark_pen = match &s.alt_pen {
            Some(a) => imin(e.st.dark, a.max_stacks) as f64 * a.pct_per_stack,
            None => 0.0,
        };
        let shred = match &s.armor_shred {
            Some(a) => imin(e.st.cleaver, a.max_stacks) as f64 * a.pct_per_stack,
            None => 0.0,
        };
        let mut leth = sheet.lethality;
        if let Some(o) = &s.opener_lethality {
            if e.st.t < o.duration_s {
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

    fn mag_mult(&self, e: &Engine) -> f64 {
        let sheet = e.p.sheet;
        let s = &e.p.fx.s;
        let dark_pen = match &s.alt_pen {
            Some(a) => imin(e.st.dark, a.max_stacks) as f64 * a.pct_per_stack,
            None => 0.0,
        };
        let mal = match &s.ult_burn {
            Some(u) if e.st.t < e.st.mal_shred_until => u.mr_reduction,
            _ => 0.0,
        };
        let shred = match &s.mr_shred {
            Some(a) => imin(e.st.blood, a.max_stacks) as f64 * a.pct_per_stack,
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

    fn mult(&self, e: &Engine, dtype: DType) -> f64 {
        match dtype {
            DType::Physical => self.phys_mult(e),
            DType::Magic => self.mag_mult(e),
            DType::True => 1.0,
        }
    }

    /// What one attack's worth of AD deals after mitigation: the yardstick
    /// the rotation's choices are weighed in.
    fn attack_unit(&self, e: &Engine) -> f64 {
        self.ad(e) * self.crit_ev(e) * self.attack_amp(e) * self.phys_mult(e)
    }

    /// What one application of the build's on-hit effects deals after
    /// mitigation (Kraken Slayer as a third of its proc).
    fn onhit_unit(&self, e: &Engine) -> f64 {
        let sheet = e.p.sheet;
        let fx = e.p.fx;
        let hp = pymax(e.st.hp, 0.0);
        let mut total = 0.0;
        for oh in &fx.onhit {
            let mut amt = oh.base
                + oh.ap_ratio * sheet.ap
                + oh.bonus_ad_ratio * (sheet.ad_bonus + self.p_ad)
                + oh.max_mana_pct / 100.0 * sheet.mana;
            if let Some((_, rng)) = oh.self_max_hp_pct {
                amt += rng / 100.0 * sheet.hp;
            }
            total += amt * self.mult(e, oh.dtype);
        }
        for oh in &fx.onhit_current_hp {
            total += oh.ranged_pct / 100.0 * hp * self.mult(e, oh.dtype);
        }
        if let Some(k) = &fx.s.kraken {
            let base = k.base_by_level.at(self.level) * k.ranged_mult;
            let missing = 1.0 - hp / e.target_hp;
            total += base * (1.0 + k.missing_ranged / 100.0 * missing) / 3.0 * self.mult(e, k.dtype);
        }
        total
    }

    // ---- attacks abilities make ---------------------------------------

    /// On-hit effects from an attack an ability makes, at `eff` of their
    /// damage (Onslaught: a quarter). Such a hit counts towards Kraken
    /// Slayer's third and stacks Energized like an attack, and triggers
    /// nothing else that rides an attack.
    fn apply_onhits(&mut self, e: &mut Engine, eff: f64) {
        let sheet = e.p.sheet;
        let fx = e.p.fx;
        for oh in &fx.onhit {
            let mut amt = oh.base
                + oh.ap_ratio * sheet.ap
                + oh.bonus_ad_ratio * (sheet.ad_bonus + self.p_ad)
                + oh.max_mana_pct / 100.0 * sheet.mana;
            if let Some((_, rng)) = oh.self_max_hp_pct {
                amt += rng / 100.0 * sheet.hp;
            }
            e.deal(amt * eff, oh.dtype, oh.source, false, false, 1.0);
        }
        for oh in &fx.onhit_current_hp {
            let amt = oh.ranged_pct / 100.0 * pymax(e.st.hp, 0.0);
            e.deal(amt * eff, oh.dtype, SRC_BOTRK, false, false, 1.0);
        }
        if let Some(k) = &fx.s.kraken {
            if e.st.kraken >= 2 {
                e.st.kraken = 0;
                let base = k.base_by_level.at(self.level) * k.ranged_mult;
                let missing = 1.0 - pymax(e.st.hp, 0.0) / e.target_hp;
                let amt = base * (1.0 + k.missing_ranged / 100.0 * missing);
                e.deal(amt * eff, k.dtype, SRC_KRAKEN, false, false, 1.0);
            } else {
                e.st.kraken += 1;
            }
        }
        if !fx.energized.is_empty() {
            let mut stacks = 6.0;
            for en in &fx.energized {
                stacks += en.extra_stacks_per_attack;
            }
            e.st.energize += stacks;
            if e.st.energize >= 100.0 {
                e.st.energize -= 100.0;
                for en in &fx.energized {
                    e.deal(en.bonus * eff, en.dtype, en.source, false, false, 1.0);
                }
            }
        }
    }

    /// What a hit from `weapon` leaves behind when an ability (or the
    /// off-hand, for a marked target) fires it: Calibrum marks, Gravitum
    /// slows, Crescendum sends a spectral Chakram back to be caught.
    fn weapon_hit(&mut self, e: &Engine, weapon: i64) {
        let t = e.st.t;
        if weapon == CALIBRUM {
            self.mark(t, false);
        } else if weapon == GRAVITUM {
            self.s.slow_until = t + self.slow_duration;
        } else if weapon == CRESCENDUM {
            self.launch_chakrams(e, 1);
        }
    }

    /// One mark per target: marking again refreshes it, and an empowered
    /// mark (Moonlight Vigil's) stays empowered until it is consumed.
    fn mark(&mut self, t: f64, empowered: bool) {
        let live = t <= self.s.mark_until;
        self.s.mark_empowered = empowered || (live && self.s.mark_empowered);
        self.s.mark_until = t + self.mark_duration;
    }

    // ---- Crescendum ---------------------------------------------------

    fn chakrams_at(&self, t: f64) -> i64 {
        if t < self.s.ck_expire {
            self.s.chakrams
        } else {
            0
        }
    }

    fn chakrams_in_flight(&self) -> i64 {
        let mut n = 0;
        let mut i = 0;
        while i < FLIGHTS {
            if self.s.ck_at[i] < INF {
                n += self.s.ck_n[i];
            }
            i += 1;
        }
        n
    }

    /// The share of AD `n` held Chakrams add to a Crescendum attack: each
    /// one a step less than the one before, down to a floor.
    fn chakram_ratio(&self, n: i64) -> f64 {
        let mut total = 0.0;
        let mut i = 0;
        while i < n {
            total += pymax(self.ck_first - self.ck_step * (i as f64), self.ck_min);
            i += 1;
        }
        total
    }

    /// The flight back from the target, at the speed attack speed gives it.
    fn return_time(&self, e: &Engine) -> f64 {
        self.attack_range / (self.cr_in_speed + self.cr_in_per_as * self.bonus_as_pct(e) / 100.0)
    }

    /// From one Crescendum hit to the next: the blade waits at the target,
    /// flies back, and the next throw winds up and flies out. Catching it
    /// resets the attack timer, so this is the whole interval, whatever the
    /// attack speed (which only speeds the return and the windup), though
    /// never shorter than the attack speed cap allows.
    fn crescendum_cycle(&self, e: &Engine) -> f64 {
        let crit = e.p.sheet.crit_chance / 100.0;
        let windup = ((1.0 - crit) * self.cr_windup + crit * self.cr_windup_crit) * self.attack_time(e);
        let cycle = self.cr_linger + self.return_time(e) + windup + self.cr_out_time;
        pymax(cycle, 1.0 / AS_CAP)
    }

    /// `n` spectral Chakrams leave the target now and are caught, and only
    /// then counted, after the wait there and the flight back.
    fn launch_chakrams(&mut self, e: &Engine, n: i64) {
        let at = e.st.t + self.cr_linger + self.return_time(e);
        let mut i = 0;
        while i < FLIGHTS {
            if self.s.ck_at[i] == INF {
                self.s.ck_at[i] = at;
                self.s.ck_n[i] = n;
                return;
            }
            i += 1;
        }
        self.s.ck_n[0] += n;
    }

    fn next_catch(&self) -> f64 {
        let mut at = INF;
        let mut i = 0;
        while i < FLIGHTS {
            at = pymin(at, self.s.ck_at[i]);
            i += 1;
        }
        at
    }

    fn catch_chakrams(&mut self, t: f64) {
        let held = self.s.main == CRESCENDUM || self.s.off == CRESCENDUM;
        let mut i = 0;
        while i < FLIGHTS {
            if self.s.ck_at[i] <= t {
                if held {
                    self.s.chakrams = imin(self.chakrams_at(t) + self.s.ck_n[i], self.ck_max);
                    self.s.ck_expire = t + self.ck_duration;
                }
                self.s.ck_at[i] = INF;
                self.s.ck_n[i] = 0;
            }
            i += 1;
        }
    }

    // ---- Moonlight ----------------------------------------------------

    /// Spend Moonlight from the main weapon; true when that empties it.
    fn spend(&mut self, amount: f64) -> bool {
        let w = self.s.main as usize;
        self.s.ammo[w] = pymax(self.s.ammo[w] - amount, 0.0);
        self.s.ammo[w] <= 0.0
    }

    /// The main weapon is out of Moonlight: it goes to the back of the queue
    /// with a full magazine for when it comes round, and the next weapon is
    /// assembled from `from` (the end of whatever spent the last of it). No
    /// attack and no cast during the assembly; the new weapon's Q is held a
    /// little longer. The off-hand is not touched.
    fn assemble_next(&mut self, e: &mut Engine, from: f64) {
        let spent = self.s.main;
        let next = self.s.queue[0];
        self.s.queue[0] = self.s.queue[1];
        self.s.queue[1] = self.s.queue[2];
        self.s.queue[2] = spent;
        self.s.ammo[spent as usize] = self.ammo_cap;
        self.s.main = next;
        if spent == CRESCENDUM {
            self.s.chakrams = 0;
            self.s.ck_expire = -INF;
        }
        self.s.assembly_until = from + self.assembly_s;
        let w = next as usize;
        self.s.q_ready[w] = pymax(self.s.q_ready[w], from + self.assembly_q_lock_s);
        e.st.next_attack = pymax(e.st.next_attack, self.s.assembly_until);
    }

    // ---- the rotation -------------------------------------------------

    /// Which held weapon Moonlight Vigil is best cast with: Crescendum's
    /// six Chakrams, then Infernum's bonus blast, then Calibrum's empowered
    /// mark; Severum's heal and Gravitum's root deal nothing.
    fn r_rank(&self, weapon: i64) -> i64 {
        if weapon == CRESCENDUM {
            3
        } else if weapon == INFERNUM {
            2
        } else if weapon == CALIBRUM {
            1
        } else {
            0
        }
    }

    fn onslaught_shots(&self, e: &Engine) -> i64 {
        (self.ons_starting * (1.0 + self.ons_as_coeff * self.bonus_as_pct(e) / 100.0) + 1e-9) as i64
    }

    /// Onslaught's worth over the attacks it replaces, after mitigation,
    /// with `off` in the off-hand. Its shots deal a share of AD and a
    /// quarter of the on-hit effects; cast right after an attack, it pushes
    /// the next one from one attack interval away to its own end.
    fn onslaught_gain(&self, e: &Engine, off: i64) -> f64 {
        let unit = self.attack_unit(e);
        let onhit = self.onhit_unit(e);
        let shots = self.onslaught_shots(e);
        let mut gain = (shots as f64) * (self.ons_ad_ratio * unit + self.ons_onhit * onhit);
        if off == CALIBRUM {
            gain += self.mark_dmg * self.phys_mult(e);
        } else if off == CRESCENDUM {
            // its shots come back as Chakrams, for the attacks that follow
            let have = self.chakrams_at(e.st.t);
            let more = imin(have + (shots + 1) / 2, self.ck_max);
            let attacks = self.ck_duration / self.crescendum_cycle(e);
            gain += (self.chakram_ratio(more) - self.chakram_ratio(have)) * unit * attacks;
        }
        let lost = self.ons_duration / self.attack_time(e) - 1.0;
        gain - pymax(lost, 0.0) * (unit + onhit)
    }

    /// What `weapon`'s Q is worth after mitigation with `other` in the
    /// off-hand: what a swap to reach it has to beat.
    fn q_value(&self, e: &Engine, weapon: i64, other: i64) -> f64 {
        if weapon == CALIBRUM {
            (self.moonshot_dmg + self.mark_dmg) * self.phys_mult(e)
        } else if weapon == SEVERUM {
            self.onslaught_gain(e, other)
        } else if weapon == GRAVITUM {
            self.eclipse_dmg * self.mag_mult(e)
        } else if weapon == INFERNUM {
            self.duskwave_dmg * self.phys_mult(e)
                + self.volley_ad_ratio * self.attack_unit(e)
                + self.onhit_unit(e)
        } else {
            let hits = self.sentry_active * self.sentry_speed(e) + 1.0;
            hits * self.sentry_dmg * self.crit_ev(e) * self.phys_mult(e)
        }
    }

    /// Whether `weapon`'s Q is worth casting as the main weapon at `t`;
    /// `after_attack` asks about the moment after the next attack instead,
    /// which consumes a waiting mark and lands Gravitum's slow.
    fn q_wanted(&self, e: &Engine, weapon: i64, other: i64, t: f64, after_attack: bool) -> bool {
        if weapon == CALIBRUM {
            after_attack || t > self.s.mark_until
        } else if weapon == SEVERUM {
            self.onslaught_gain(e, other) > 0.0
        } else if weapon == GRAVITUM {
            after_attack || t <= self.s.slow_until
        } else if weapon == CRESCENDUM {
            self.s.sentry_next == INF
        } else {
            true
        }
    }

    /// How much a basic attack with `weapon` in the main hand is worth
    /// against an ordinary one: Infernum's ratio, Crescendum's Chakrams (the
    /// ones flying back included) over its slower or faster interval.
    fn attack_factor(&self, e: &Engine, weapon: i64, t: f64) -> f64 {
        if weapon == INFERNUM {
            self.infernum_ratio
        } else if weapon == CRESCENDUM {
            let n = imin(self.chakrams_at(t) + self.chakrams_in_flight(), self.ck_max);
            (1.0 + self.chakram_ratio(n)) * self.attack_time(e) / self.crescendum_cycle(e)
        } else {
            1.0
        }
    }

    fn sentry_speed(&self, e: &Engine) -> f64 {
        pymin(self.sentry_base_as + self.sentry_as_ratio * self.bonus_as_pct(e) / 100.0, AS_CAP)
    }

    /// The next free action and when. A cast is woven: it starts right after
    /// an attack has landed, or where it ends before the next attack is due,
    /// so `Wait` with a cast in mind means "after the attack about to land".
    /// Phase costs no attack and may be pressed whenever nothing else runs.
    fn plan(&self, e: &Engine) -> (f64, Act) {
        let s = &self.s;
        if s.phase_done_at < INF {
            return (INF, Act::Wait);
        }
        let t = self.castable_at(e, 0.0);
        let woven = t == s.last_attack_t;
        let gap = e.st.next_attack - t;
        let (main, off) = (s.main, s.off);
        let mut held = false;

        if self.ranks.r > 0 && s.r_ready <= t {
            if self.r_rank(off) > self.r_rank(main) && s.phase_ready <= t {
                return (t, Act::Phase);
            }
            if woven || self.r_cast <= gap + 1e-9 {
                return (t, Act::CastR);
            }
            held = true;
        }
        let main_up = self.q_unlocked && s.q_ready[main as usize] <= t;
        if main_up && self.q_wanted(e, main, off, t, false) {
            if woven || self.q_lock[main as usize] <= gap + 1e-9 {
                return (t, Act::CastQ);
            }
            held = true;
        }
        if held || !self.q_unlocked {
            return (INF, Act::Wait);
        }
        let period = self.attack_time(e);
        if s.q_ready[main as usize] <= t + period && self.q_wanted(e, main, off, t, true) {
            return (INF, Act::Wait);
        }
        // Phase: for the off-hand's Q when it is worth more than the attacks
        // made with the weaker hand to reach it, else for the stronger attack
        let tp = pymax(t, s.phase_ready);
        let edge = self.attack_factor(e, off, tp) - self.attack_factor(e, main, tp);
        if s.q_ready[off as usize] <= tp + period && self.q_wanted(e, off, main, tp, true) {
            let cost = 2.0 * pymax(-edge, 0.0) * self.attack_unit(e);
            if self.q_value(e, off, main) > cost {
                return (tp, Act::Phase);
            }
        }
        if edge > 1e-9 {
            return (tp, Act::Phase);
        }
        (INF, Act::Wait)
    }

    fn cast_moonshot(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.moonshot_dmg, DType::Physical, self.src_moonshot, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        self.mark(t, false);
    }

    fn cast_onslaught(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let shots = imax(self.onslaught_shots(e), 1);
        self.s.ons_left = shots;
        self.s.ons_idx = 0;
        self.s.ons_step = self.ons_duration / (shots as f64);
        self.s.ons_next = t;
        self.s.ons_off = self.s.off;
    }

    fn cast_eclipse(&mut self, e: &mut Engine) {
        e.deal(self.eclipse_dmg, DType::Magic, self.src_eclipse, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        self.s.slow_until = -INF;
    }

    fn cast_duskwave(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.duskwave_dmg, DType::Physical, self.src_duskwave, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        self.s.volley_at = t + self.q_lock[INFERNUM as usize];
        self.s.volley_weapon = self.s.off;
    }

    fn cast_sentry(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.sentry_next = t + self.sentry_arm;
        self.s.sentry_end = t + self.sentry_arm + self.sentry_active;
        self.s.sentry_weapon = self.s.off;
        self.s.sentry_first = true;
    }

    fn fire_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.s.r_weapon = self.s.main;
        // what the engine does for an ult at t = 0, at the cast's real time
        e.prime_spellblade();
        if let Some(u) = &e.p.fx.s.on_ult_cast {
            e.st.hex_until = t + u.duration_s;
        }
        if e.p.fx.s.ult_attack_steroid.is_some() {
            e.st.post_r_attacks = 0;
        }
        let mut dmg = self.r_dmg;
        if self.s.main == INFERNUM {
            dmg += self.r_infernum_bonus;
        }
        e.deal(dmg, DType::Physical, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.ult_hatefog();
        self.s.r_attack_at = t + self.r_attack_delay;
        self.busy_for(e, self.r_cast);
    }

    /// Moonlight Vigil's locked-on attack: the weapon it was cast with, on-hit
    /// effects, and a crit-chance-scaled modifier in place of a critical roll.
    fn r_attack(&mut self, e: &mut Engine) {
        let weapon = self.s.r_weapon;
        let sheet = e.p.sheet;
        let modifier =
            1.0 + self.r_crit_mod * (sheet.crit_chance / 100.0) * (sheet.crit_damage / 100.0 - 1.0);
        let mut amt = self.r_attack_ad_ratio * self.ad(e) * modifier * self.attack_amp(e);
        if weapon == INFERNUM {
            amt *= self.infernum_ratio;
        }
        e.deal(amt, DType::Physical, self.src_r_attack, false, false, 1.0);
        self.apply_onhits(e, 1.0);
        let t = e.st.t;
        if weapon == CALIBRUM {
            self.mark(t, true);
        } else if weapon == GRAVITUM {
            self.s.slow_until = t + self.slow_duration;
        } else if weapon == CRESCENDUM {
            self.launch_chakrams(e, self.r_chakrams);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        // Weapon Master: the ranks each statistic has at this level
        let p_ad = kit.at_level("gen.P.weaponMaster.adRanksByLevel", level)?
            * kit.num("gen.P.weaponMaster.adPerRank")?;
        let p_as_pct = kit.at_level("gen.P.weaponMaster.asRanksByLevel", level)?
            * kit.num("gen.P.weaponMaster.asPctPerRank")?;
        let p_lethality = kit.at_level("gen.P.weaponMaster.lethalityRanksByLevel", level)?
            * kit.num("gen.P.weaponMaster.lethalityPerRank")?;
        let bonus_ad = sheet.ad_bonus + p_ad;
        let q_unlocked = ranks.q > 0 && (level as f64) >= kit.num("gen.Q.unlockLevel")?;

        // a Q's damage: base and bonus-AD ratio by champion level, flat AP ratio
        let q_dmg = |name: &str| -> Result<f64, String> {
            Ok(kit.at_level(&format!("gen.Q.{name}.base"), level)?
                + kit.at_level(&format!("gen.Q.{name}.bonusAdRatio"), level)? * bonus_ad
                + kit.num(&format!("gen.Q.{name}.apRatio"))? * sheet.ap)
        };
        let ons_duration = kit.num("gen.Q.onslaught.durationS")?;
        let duskwave_cast = kit.num("gen.Q.duskwaveCastTimeS")?;
        let volley_delay = kit.num("gen.Q.duskwave.volleyDelayS")?;
        let mut q_cd = [0.0; WEAPONS];
        let mut q_lock = [0.0; WEAPONS];
        q_cd[CALIBRUM as usize] = kit.at_level("abilities.Q.cooldownS", level)?;
        q_cd[SEVERUM as usize] = kit.at_level("gen.Q.onslaught.cooldownS", level)?;
        q_cd[GRAVITUM as usize] = kit.at_level("gen.Q.bindingEclipse.cooldownS", level)?;
        q_cd[INFERNUM as usize] = kit.at_level("gen.Q.duskwave.cooldownS", level)?;
        q_cd[CRESCENDUM as usize] = kit.at_level("gen.Q.sentry.cooldownS", level)?;
        // how long each Q keeps attacks waiting: its cast time, Onslaught's
        // whole duration, and for Duskwave the wait for its volley as well
        q_lock[CALIBRUM as usize] = kit.num("gen.Q.moonshotCastTimeS")?;
        q_lock[SEVERUM as usize] = ons_duration;
        q_lock[GRAVITUM as usize] = kit.num("gen.Q.bindingEclipseCastTimeS")?;
        q_lock[INFERNUM as usize] = duskwave_cast + volley_delay;
        q_lock[CRESCENDUM as usize] = kit.num("gen.Q.sentryCastTimeS")?;

        // the starting loadout: main, off-hand and the queue name each weapon once
        let main = kit.num("gen.P.loadout.main")? as i64;
        let off = kit.num("gen.P.loadout.off")? as i64;
        let reserve = kit.list("gen.P.loadout.queue")?;
        if reserve.len() != 3 {
            return Err("aphelios kit: gen.P.loadout.queue must list three weapons".to_string());
        }
        let queue = [reserve[0] as i64, reserve[1] as i64, reserve[2] as i64];
        let mut seen = [false; WEAPONS];
        for w in [main, off, queue[0], queue[1], queue[2]] {
            if w < 0 || w >= WEAPONS as i64 || seen[w as usize] {
                return Err("aphelios kit: gen.P.loadout must name each of the five weapons once"
                    .to_string());
            }
            seen[w as usize] = true;
        }

        // Crescendum's flight out over the champion's own attack range: it
        // starts fast and slows steadily to a minimum speed
        let range = sheet.base_attack_range;
        let v0 = kit.num("gen.P.crescendum.outSpeed")?;
        let decel = kit.num("gen.P.crescendum.outDecel")?;
        let v_min = kit.num("gen.P.crescendum.outMinSpeed")?;
        let t_slow = (v0 - v_min) / decel;
        let d_slow = v0 * t_slow - decel * t_slow * t_slow / 2.0;
        let cr_out_time = if range <= d_slow {
            (v0 - (v0 * v0 - 2.0 * decel * range).sqrt()) / decel
        } else {
            t_slow + (range - d_slow) / v_min
        };
        let attack_total = kit.num("gen.P.crescendum.attackTotalTimeS")?;

        let ammo_cap = kit.num("gen.P.moonlight.capacity")?;
        let state = State {
            main,
            off,
            queue,
            ammo: [ammo_cap; WEAPONS],
            q_ready: [0.0; WEAPONS],
            busy_until: 0.0,
            assembly_until: 0.0,
            phase_ready: 0.0,
            phase_done_at: INF,
            last_attack_t: -INF,
            in_attack: false,
            attack_weapon: main,
            offhand_attack: false,
            mark_attack: false,
            out_of_moonlight: false,
            mark_until: -INF,
            mark_empowered: false,
            slow_until: -INF,
            ons_next: INF,
            ons_left: 0,
            ons_idx: 0,
            ons_step: 0.0,
            ons_off: off,
            volley_at: INF,
            volley_weapon: off,
            sentry_next: INF,
            sentry_end: 0.0,
            sentry_weapon: off,
            sentry_first: false,
            r_ready: INF,
            r_attack_at: INF,
            r_weapon: main,
            chakrams: 0,
            ck_expire: -INF,
            ck_at: [INF; FLIGHTS],
            ck_n: [0; FLIGHTS],
            leth_on: false,
            leth_cleaver: -1,
            leth_dark: -1,
            armor_true: 0.0,
            armor_shown: 0.0,
        };

        Ok(GenDriver {
            ranks,
            level,
            attack_range: range,
            q_unlocked,
            p_ad,
            p_as_pct,
            p_lethality,
            ammo_cap,
            ammo_per_attack: kit.num("gen.P.moonlight.perAttack")?,
            ammo_per_cast: kit.num("gen.P.moonlight.perCast")?,
            assembly_s: kit.num("gen.P.moonlight.assemblyS")?,
            assembly_q_lock_s: kit.num("gen.P.moonlight.assemblyQLockS")?,
            q_cd,
            q_lock,
            mark_duration: kit.num("gen.P.calibrum.markDurationS")?,
            mark_dmg: kit.num("gen.P.calibrum.markBase")?
                + kit.num("gen.P.calibrum.markBonusAdRatio")? * bonus_ad,
            moonshot_dmg: q_dmg("moonshot")?,
            ons_duration,
            ons_starting: kit.num("gen.Q.onslaught.startingAttacks")?,
            ons_as_coeff: kit.num("gen.Q.onslaught.asCoeff")?,
            ons_ad_ratio: kit.at_level("gen.Q.onslaught.adRatio", level)?,
            ons_onhit: kit.num("gen.Q.onslaught.onHitEffectiveness")?,
            slow_duration: kit.num("gen.P.gravitum.slowDurationS")?,
            eclipse_dmg: q_dmg("bindingEclipse")?,
            infernum_ratio: kit.num("gen.P.infernum.attackAdRatio")?,
            duskwave_dmg: q_dmg("duskwave")?,
            volley_delay,
            volley_ad_ratio: kit.num("gen.Q.duskwave.volleyAdRatio")?,
            ck_first: kit.num("gen.P.crescendum.chakramFirstRatio")?,
            ck_step: kit.num("gen.P.crescendum.chakramStepRatio")?,
            ck_min: kit.num("gen.P.crescendum.chakramMinRatio")?,
            ck_max: kit.num("gen.P.crescendum.chakramMax")? as i64,
            ck_duration: kit.num("gen.P.crescendum.chakramDurationS")?,
            cr_linger: kit.num("gen.P.crescendum.lingerS")?,
            cr_out_time,
            cr_in_speed: kit.num("gen.P.crescendum.inSpeed")?,
            cr_in_per_as: kit.num("gen.P.crescendum.inSpeedPerBonusAs")?,
            cr_windup: kit.num("gen.P.crescendum.attackCastTimeS")? / attack_total,
            cr_windup_crit: kit.num("gen.P.crescendum.critAttackCastTimeS")? / attack_total,
            sentry_dmg: q_dmg("sentry")?,
            sentry_arm: kit.num("gen.Q.sentry.armDelayS")?,
            sentry_active: kit.num("gen.Q.sentry.activeDurationS")?,
            sentry_base_as: kit.num("gen.Q.sentry.baseAttackSpeed")?,
            sentry_as_ratio: kit.num("gen.Q.sentry.attackSpeedRatio")?,
            phase_cd: kit.at_rank("abilities.W.cooldownS", 1)?,
            phase_swap: kit.num("gen.W.swapS")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast: kit.num("gen.R.castTimeS")?,
            r_dmg: kit.at_rank("gen.R.base", ranks.r)?
                + kit.num("gen.R.bonusAdRatio")? * bonus_ad
                + kit.num("gen.R.apRatio")? * sheet.ap,
            r_infernum_bonus: kit.at_rank("gen.R.infernumBonusBase", ranks.r)?
                + kit.num("gen.R.infernumBonusAdRatio")? * bonus_ad,
            r_attack_delay: kit.num("gen.R.skyAttackDelayS")?,
            r_attack_ad_ratio: kit.num("gen.R.skyAttackAdRatio")?,
            r_crit_mod: kit.num("gen.R.critDamageMod")?,
            r_mark_bonus: kit.at_rank("gen.R.calibrumMarkBonus", ranks.r)?,
            r_chakrams: kit.num("gen.R.crescendumChakrams")? as i64,
            src_mark: intern("P Calibrum mark"),
            src_chakram: intern("P Chakrams"),
            src_moonshot: intern("Q Moonshot"),
            src_onslaught: intern("Q Onslaught"),
            src_eclipse: intern("Q Binding Eclipse"),
            src_duskwave: intern("Q Duskwave"),
            src_volley: intern("Q Duskwave volley"),
            src_sentry: intern("Q Sentry"),
            src_r_attack: intern("R attack"),
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
        self.p_as_pct
    }

    /// The attack damage of the moment: the sheet's plus the passive's, at
    /// Infernum's ratio while an Infernum attack is being made. (The engine
    /// also asks before the first attack, for item actives: the plain value.)
    fn attack_damage(&self, e: &Engine) -> f64 {
        let ad = self.ad(e);
        if self.s.in_attack && self.s.attack_weapon == INFERNUM {
            ad * self.infernum_ratio
        } else {
            ad
        }
    }

    fn shave_cooldowns(&mut self, _st: &mut St, t: f64, factor: f64) {
        let mut i = 0;
        while i < WEAPONS {
            shave(&mut self.s.q_ready[i], t, factor);
            i += 1;
        }
    }

    fn before_attack(&mut self, e: &mut Engine) {
        self.sync_lethality(e);
        let t = e.st.t;
        self.s.in_attack = true;
        self.s.mark_attack = t <= self.s.mark_until;
        if self.s.mark_attack && self.s.main == CALIBRUM {
            // a marked target is shot with the off-hand weapon, for no Moonlight
            self.s.offhand_attack = true;
            self.s.attack_weapon = self.s.off;
            self.weapon_hit(e, self.s.off);
            return;
        }
        self.s.offhand_attack = false;
        self.s.attack_weapon = self.s.main;
        self.s.out_of_moonlight = self.spend(self.ammo_per_attack);
        if self.s.main == GRAVITUM {
            self.s.slow_until = t + self.slow_duration;
        } else if self.s.main == CRESCENDUM && self.chakrams_at(t) > 0 {
            self.s.ck_expire = t + self.ck_duration;
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        self.sync_lethality(e);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        self.sync_lethality(e);
        let t = e.st.t;
        if e.st.attacks == 1 {
            // Umbral Glaive's first attack scales with lethality, and the
            // engine read the sheet's: the passive's share of it goes here
            if let Some(fb) = &e.p.fx.s.first_attack_bonus {
                let (amt, dtype, source) = (fb.per_lethality * self.p_lethality, fb.dtype, fb.source);
                e.deal(amt, dtype, source, false, false, 1.0);
            }
        }
        if self.s.mark_attack {
            let mut dmg = self.mark_dmg;
            if self.s.mark_empowered {
                dmg += self.r_mark_bonus;
            }
            e.deal(dmg, DType::Physical, self.src_mark, false, false, 1.0);
            self.s.mark_until = -INF;
            self.s.mark_empowered = false;
        }
        if self.s.attack_weapon == CRESCENDUM && !self.s.offhand_attack {
            let n = self.chakrams_at(t);
            if n > 0 {
                let amt = self.ad(e) * self.chakram_ratio(n) * self.attack_amp(e);
                e.deal(amt, DType::Physical, self.src_chakram, true, false, 1.0);
            }
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        self.sync_lethality(e);
        let t = e.st.t;
        self.s.in_attack = false;
        self.s.last_attack_t = t;
        e.st.next_attack = if self.s.attack_weapon == CRESCENDUM && !self.s.offhand_attack {
            t + self.crescendum_cycle(e)
        } else {
            let b = self.bonus_as(t);
            t + e.attack_period(b)
        };
        if self.s.out_of_moonlight {
            self.s.out_of_moonlight = false;
            self.assemble_next(e, t);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        let (t, act) = self.plan(e);
        if act == Act::CastQ {
            t
        } else {
            INF
        }
    }

    fn cast_q(&mut self, e: &mut Engine) {
        self.sync_lethality(e);
        let t = e.st.t;
        let (at, act) = self.plan(e);
        if act != Act::CastQ || at > t {
            return;
        }
        let weapon = self.s.main;
        let w = weapon as usize;
        let lock = self.q_lock[w];
        // Onslaught's cooldown starts when it ends, the others' at the cast
        self.s.q_ready[w] = if weapon == SEVERUM {
            t + lock + e.basic_cd(self.q_cd[w])
        } else {
            t + e.basic_cd(self.q_cd[w])
        };
        e.prime_spellblade();
        if weapon == CALIBRUM {
            self.cast_moonshot(e);
        } else if weapon == SEVERUM {
            self.cast_onslaught(e);
        } else if weapon == GRAVITUM {
            self.cast_eclipse(e);
        } else if weapon == INFERNUM {
            self.cast_duskwave(e);
        } else {
            self.cast_sentry(e);
        }
        // Duskwave's cast ends before its volley does, and Phase or the next
        // cast may follow the cast; attacks wait for the volley
        if weapon == INFERNUM {
            self.busy_for(e, lock - self.volley_delay);
            e.st.next_attack = pymax(e.st.next_attack, t + lock);
        } else {
            self.busy_for(e, lock);
        }
        if self.spend(self.ammo_per_cast) {
            self.assemble_next(e, t + lock);
        }
    }

    /// The engine's opening at t = 0. Moonlight Vigil is woven in after the
    /// first attack instead, so what the engine did for an ult cast now (the
    /// held first attack, Spellblade, the ult-cast item windows) is undone
    /// and redone at the real cast (`fire_r`).
    fn cast_r(&mut self, e: &mut Engine) {
        self.sync_lethality(e);
        if self.ranks.r > 0 {
            self.s.r_ready = 0.0;
            e.st.sb_primed = false;
            e.st.next_attack = 0.0;
            e.st.hex_until = -1.0;
            e.st.post_r_attacks = (1.0 / 1e-9) as i64;
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let s = &self.s;
        let mut n = 0;
        if s.phase_done_at < INF {
            out[n] = (s.phase_done_at, Kind::Ev(EV_PHASE_DONE));
            n += 1;
        }
        if s.r_attack_at < INF {
            out[n] = (s.r_attack_at, Kind::Ev(EV_R_ATTACK));
            n += 1;
        }
        if s.volley_at < INF {
            out[n] = (s.volley_at, Kind::Ev(EV_VOLLEY));
            n += 1;
        }
        if s.ons_next < INF {
            out[n] = (s.ons_next, Kind::Ev(EV_ONSLAUGHT));
            n += 1;
        }
        if s.sentry_next < INF {
            out[n] = (s.sentry_next, Kind::Ev(EV_SENTRY));
            n += 1;
        }
        let catch = self.next_catch();
        if catch < INF {
            out[n] = (catch, Kind::Ev(EV_CHAKRAM));
            n += 1;
        }
        let (at, act) = self.plan(e);
        if act == Act::Phase || act == Act::CastR {
            out[n] = (at, Kind::Ev(EV_ACT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        self.sync_lethality(e);
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_PHASE_DONE) => {
                self.s.phase_done_at = INF;
                // a weapon that ran out meanwhile is being replaced: no swap
                if t >= self.s.assembly_until {
                    let main = self.s.main;
                    self.s.main = self.s.off;
                    self.s.off = main;
                }
            }
            Kind::Ev(EV_R_ATTACK) => {
                self.s.r_attack_at = INF;
                self.r_attack(e);
            }
            Kind::Ev(EV_VOLLEY) => {
                self.s.volley_at = INF;
                let weapon = self.s.volley_weapon;
                let amt = self.volley_ad_ratio * self.ad(e) * self.attack_amp(e);
                e.deal(amt, DType::Physical, self.src_volley, true, false, 1.0);
                self.apply_onhits(e, 1.0);
                self.weapon_hit(e, weapon);
            }
            Kind::Ev(EV_ONSLAUGHT) => {
                // the off-hand weapon fires first, then they alternate
                let weapon = if self.s.ons_idx % 2 == 0 { self.s.ons_off } else { SEVERUM };
                let amt = self.ons_ad_ratio * self.ad(e) * self.attack_amp(e);
                e.deal(amt, DType::Physical, self.src_onslaught, true, false, 1.0);
                self.apply_onhits(e, self.ons_onhit);
                e.eclipse_hit();
                self.weapon_hit(e, weapon);
                self.s.ons_idx += 1;
                self.s.ons_left -= 1;
                self.s.ons_next = if self.s.ons_left > 0 { t + self.s.ons_step } else { INF };
            }
            Kind::Ev(EV_SENTRY) => {
                if t >= self.s.sentry_end {
                    self.s.sentry_next = INF;
                    return;
                }
                e.deal(self.sentry_dmg, DType::Physical, self.src_sentry, true, true, 1.0);
                if self.s.sentry_first {
                    self.s.sentry_first = false;
                    e.ability_cast_proc();
                }
                self.weapon_hit(e, self.s.sentry_weapon);
                let next = t + 1.0 / self.sentry_speed(e);
                self.s.sentry_next = if next < self.s.sentry_end { next } else { INF };
            }
            Kind::Ev(EV_CHAKRAM) => self.catch_chakrams(t),
            Kind::Ev(EV_ACT) => {
                let (at, act) = self.plan(e);
                if at > t {
                    return;
                }
                if act == Act::Phase {
                    self.s.phase_ready = t + self.phase_cd;
                    self.s.phase_done_at = t + self.phase_swap;
                } else if act == Act::CastR {
                    self.fire_r(e);
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
