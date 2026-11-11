//! Zed, reviewed for patch 16.22.
//!
//! Death Mark opens the fight: Zed is gone for the 0.6 s delay and the
//! 0.35 s dash, attacks the moment he lands, and the mark he leaves stores
//! what he and his Shadows deal for 3 s before it detonates. Attacks come
//! first and casts are woven in behind them: Living Shadow and Shadow Slash
//! have no cast time, and Razor Shuriken's 0.25 s is spent right after an
//! attack lands, or wherever it fits before the next one is due.
//!
//! The Shadows. Death Mark leaves one where it was cast and Living Shadow
//! places another; each one still standing when a Razor Shuriken's cast time
//! ends throws a full shuriken of its own. Living Shadow has no other worth
//! in a damage fight, so it is cast as early as it can be without giving up
//! a shuriken its Shadow would have thrown. Shadow Slash damages a target
//! once however many Shadows slash, and takes 3 s off Living Shadow's
//! running cooldown.
//!
//! The mark. It stores PRE-mitigation physical and magic damage from every
//! source (attacks with their critical strikes, abilities, the passive, item
//! procs and actives), never true damage. The engine deals item damage on
//! its own and only keeps a post-mitigation total, so the driver watches
//! that total: what the kit deals itself is stored at face value, and what
//! the items deal is turned back into pre-mitigation damage with the
//! multiplier the engine applied (`phys_factor`), less the true damage the
//! items ride on an attack (`attack_aside`). Magic item procs are turned back
//! with the physical multiplier too, the one inexact step: the detonation is
//! physical, so the two multipliers nearly cancel.

use crate::fight::{shave, Driver, Engine, Events, Kind, St, S_COMBAT_T0};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Living Shadow, then Shadow Slash: the casts without a cast time, ahead of
/// a shuriken due at the same instant.
const EV_W_CAST: u8 = 0;
const EV_E_CAST: u8 = 1;
/// Death Mark detonates: the last hit of its instant. Whatever else is due
/// at that very moment lands first and comes too late to be stored (a Shadow
/// Slash with no ability haste, cast on landing, is back up exactly then).
const EV_R_DETONATE: u8 = 2;

/// A hair of tolerance where two times reached along different sums meet (a
/// Shadow fading as a shuriken's cast time ends).
const EPS: f64 = 1e-9;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    p_pct: f64,
    p_threshold: f64,
    p_icd: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_cd: f64,
    w_shadow_dur: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cdr_w: f64,
    r_ad_ratio: f64,
    r_stored_ratio: f64,
    /// The delay plus the dash: how long Death Mark keeps Zed away.
    r_away_s: f64,
    r_mark_dur: f64,
    r_shadow_dur: f64,
    src_p: SourceId,
    src_q_mimic: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    w_ready: f64,
    w_shadow_until: f64,
    e_ready: f64,
    r_shadow_until: f64,
    p_ready: f64,
    /// When Death Mark detonates (INF: no mark on the dummy).
    mark_until: f64,
    /// The pre-mitigation physical and magic damage the mark has stored.
    stored: f64,
    /// The engine's damage total at the last look: what it has grown by
    /// since is what the items dealt in between.
    seen: f64,
    /// Inside an attack: the part of it that is not stored as item damage
    /// (the items' true damage, Hexoptics' share, the passive's own hit).
    aside: f64,
    /// The engine's multiplier on physical damage as the attack began.
    attack_factor: f64,
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

    // ---- the engine's multipliers, as it applies them ------------------

    /// What the build's items amplify one instance of `dtype` by right now:
    /// the engine's `base_amp` (Liandry's and Riftmaker by the second, Abyssal
    /// Mask, Giant Slayer) and Horizon Focus's window.
    fn item_amp(&self, e: &Engine, dtype: DType) -> f64 {
        let fx = e.p.fx;
        let t = e.st.t;
        let mut amp = 1.0;
        if !fx.dmg_amps.is_empty() {
            // whole seconds in combat; the first damage dealt opens it
            let secs = if e.st.set & S_COMBAT_T0 != 0 {
                (t - e.st.combat_t0 + EPS) as i64
            } else {
                0
            };
            for a in &fx.dmg_amps {
                amp *= 1.0 + a.pct_per_stack / 100.0 * imin(a.max_stacks, secs) as f64;
            }
        }
        for a in &fx.flat_amps {
            if a.dtype.is_none() || a.dtype == Some(dtype) {
                amp *= 1.0 + a.pct / 100.0;
            }
        }
        if let Some(max_pct) = fx.s.giant_slayer {
            amp *= 1.0 + pymin(max_pct, e.target_bonus_hp / 100.0) / 100.0;
        }
        if let Some(h) = &fx.s.hypershot {
            if t < e.st.hz_until {
                amp *= 1.0 + h.amp_pct / 100.0;
            }
        }
        amp
    }

    /// What the engine multiplies a physical item proc by right now: the
    /// items' amplification and the armor left after Black Cleaver's shred,
    /// percent penetration (Terminus's included) and lethality.
    fn phys_factor(&self, e: &Engine) -> f64 {
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
                leth += if self.ranged() { o.ranged } else { o.melee };
            }
        }
        let armor = eff_resist(e.target_armor, 0.0, shred,
                               stack_pct_pen(sheet.armor_pen_pct, dark_pen), leth);
        self.item_amp(e, DType::Physical) * resist_mult(armor)
    }

    /// Hexoptics' Magnification on the attack itself, as a share of it.
    fn magnified(&self, e: &Engine) -> f64 {
        match &e.p.fx.s.attack_amp {
            Some(aa) => aa.max_pct / 100.0 * pymin(1.0, self.attack_range / aa.max_at_range),
            None => 0.0,
        }
    }

    /// The part of the attack about to land that the mark does not store as
    /// item damage, after mitigation, as the engine will deal it: the true
    /// damage the items add (the crit share of Fiendhunter's Opening Barrage,
    /// Umbral Glaive's opener), and what Hexoptics adds to the attack, which
    /// is an amplification and no part of its pre-mitigation damage.
    fn attack_aside(&self, e: &Engine, factor: f64) -> f64 {
        let sheet = e.p.sheet;
        let s = &e.p.fx.s;
        let chance = sheet.crit_chance / 100.0;
        let crit = sheet.crit_damage / 100.0;
        let lens = self.magnified(e);
        let mut true_raw = 0.0;
        // the attack's expected critical strike: the build's, or what an item
        // guarantees this attack (Sundered Sky's first, the Barrage's three)
        let mut ev = 1.0 + chance * (crit - 1.0);
        if let Some(floor) = s.first_attack_crit_floor_ev {
            if !e.st.sundered_used {
                ev = pymax(ev, floor);
            }
        }
        if let Some(u) = &s.ult_attack_steroid {
            if e.st.post_r_attacks < u.attacks {
                ev = pymax(ev, chance * crit + (1.0 - chance) * (u.crit_dmg_frac * crit));
                true_raw += e.p.ad * (chance * u.true_dmg_frac * (1.0 + lens) * crit);
            }
        }
        if let Some(fb) = &s.first_attack_bonus {
            if fb.dtype == DType::True && e.st.attacks == 1 {
                true_raw += fb.base + fb.per_lethality * sheet.lethality;
            }
        }
        true_raw * self.item_amp(e, DType::True) + e.p.ad * ev * lens * factor
    }

    // ---- what Death Mark stores ----------------------------------------

    fn marked(&self) -> bool {
        self.s.mark_until != INF
    }

    /// A hit landing now is stored: the mark is on the dummy, and the hit is
    /// not at the very instant it detonates.
    fn storing(&self, e: &Engine) -> bool {
        self.marked() && e.st.t < self.s.mark_until
    }

    /// The items' damage since the last look, outside an attack (their
    /// actives, an Eclipse proc on a cast, burn ticks): stored as the
    /// physical damage that would have dealt it.
    fn absorb(&mut self, e: &Engine) {
        let post = e.st.total - self.s.seen;
        self.s.seen = e.st.total;
        if post > 0.0 && self.marked() {
            self.s.stored += post / self.phys_factor(e);
        }
    }

    /// One instance of the kit's own ability damage: the mark stores it at
    /// face value. (An item proc dealt from inside this hit, Luden's Echo's
    /// single one, is passed over.)
    fn hit(&mut self, e: &mut Engine, raw: f64, source: SourceId) {
        self.absorb(e);
        e.deal(raw, DType::Physical, source, false, true, 1.0);
        self.s.seen = e.st.total;
        if self.storing(e) {
            self.s.stored += raw;
        }
    }

    // ---- the rotation ---------------------------------------------------

    /// How many Razor Shurikens a Shadow placed at `x` would throw: the casts
    /// from `q` on, one per cooldown, whose cast time ends before it fades.
    fn mimics(&self, x: f64, q: f64, cd: f64) -> i64 {
        let room = x + self.w_shadow_dur - self.q_cast_s - q;
        if room < -EPS {
            0
        } else {
            ((room + EPS) / cd) as i64 + 1
        }
    }

    /// A Razor Shuriken cast at `c` would hold up the next attack. It waits
    /// for that attack to land, unless a Shadow that would throw with it now
    /// has faded by then: a shuriken is worth more than a quarter second of
    /// an attack.
    fn waits_for_attack(&self, e: &Engine, c: f64) -> bool {
        let thrown = c + self.q_cast_s;
        if thrown <= e.st.next_attack {
            return false;
        }
        let late = pymax(c, e.st.next_attack) + self.q_cast_s;
        let fades = |until: f64| thrown <= until + EPS && late > until + EPS;
        !(fades(self.s.w_shadow_until) || fades(self.s.r_shadow_until))
    }

    /// When the next Razor Shuriken goes out as things stand: once it is
    /// ready, or behind the attack it would otherwise hold up.
    fn q_plan(&self, e: &Engine) -> f64 {
        let c = self.castable_at(e, e.st.q_ready);
        if self.waits_for_attack(e, c) {
            pymax(c, e.st.next_attack)
        } else {
            c
        }
    }

    /// When Living Shadow is cast: as soon as it can be while its Shadow
    /// still throws as many shurikens as one placed right at the next cast,
    /// at that cast otherwise. INF while there is nothing for it to mimic.
    fn w_at(&self, e: &Engine) -> f64 {
        if self.ranks.w == 0 || self.ranks.q == 0 {
            return INF;
        }
        let now = self.castable_at(e, self.s.w_ready);
        let q = pymax(self.q_plan(e), now);
        let cd = e.basic_cd(self.q_cd);
        if self.mimics(now, q, cd) >= self.mimics(q, q, cd) {
            now
        } else {
            q
        }
    }

    fn cast_w(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.w_ready = t + e.basic_cd(self.w_cd);
        // a new Living Shadow replaces the one before it
        self.s.w_shadow_until = t + self.w_shadow_dur;
        e.prime_spellblade();
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            w_shadow_until: -INF,
            e_ready: 0.0,
            r_shadow_until: -INF,
            p_ready: 0.0,
            mark_until: INF,
            stored: 0.0,
            seen: 0.0,
            aside: 0.0,
            attack_factor: 1.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_pct: kit.at_level("gen.P.pctByLevel", level)?,
            p_threshold: kit.num("gen.P.thresholdPct")?,
            p_icd: kit.num("gen.P.icdS")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_shadow_dur: kit.num("gen.W.shadowDurationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cdr_w: kit.num("gen.E.shadowHitCdrS")?,
            r_ad_ratio: kit.num("gen.R.adRatio")?,
            r_stored_ratio: kit.at_rank("gen.R.storedDamageRatio", ranks.r)?,
            r_away_s: kit.num("gen.R.dashDelayS")? + kit.num("gen.R.dashTravelS")?,
            r_mark_dur: kit.num("gen.R.markDurationS")?,
            r_shadow_dur: kit.num("gen.R.shadowDurationS")?,
            src_p: intern("P"),
            src_q_mimic: intern("Q mimic"),
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

    fn before_attack(&mut self, e: &mut Engine) {
        self.absorb(e);
        let factor = self.phys_factor(e);
        self.s.attack_factor = factor;
        self.s.aside = self.attack_aside(e, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Contempt for the Weak: bonus magic damage on-hit against a target
        // below half health, once per 10 s; it never critically strikes
        let t = e.st.t;
        if t >= self.s.p_ready && pymax(e.st.hp, 0.0) < self.p_threshold * e.target_hp {
            self.s.p_ready = t + self.p_icd;
            let amt = self.p_pct * e.target_hp;
            let before = e.st.total;
            e.deal(amt, DType::Magic, self.src_p, false, false, 1.0);
            self.s.aside += e.st.total - before;
            if self.storing(e) {
                self.s.stored += amt;
            }
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        // the attack and everything the items hung on it are dealt: store
        // them as the pre-mitigation damage they were
        let post = e.st.total - self.s.seen - self.s.aside;
        self.s.seen = e.st.total;
        self.s.aside = 0.0;
        if self.storing(e) {
            self.s.stored += post / self.s.attack_factor;
        }
        let b = self.bonus_as(e.st.t);
        e.st.next_attack = e.st.t + e.attack_period(b);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        let c = self.castable_at(e, e.st.q_ready);
        // attacks come first: a shuriken whose cast time would hold up the
        // next attack waits for it to land (the loop asks again right after)
        if self.waits_for_attack(e, c) {
            return INF;
        }
        // and so do the casts that cost no time: a Shadow placed now throws
        // this shuriken too, and Shadow Slash is not held up behind it
        if self.w_at(e) <= c {
            return INF;
        }
        if self.ranks.e > 0 && self.castable_at(e, self.s.e_ready) <= c {
            return INF;
        }
        c
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.hit(e, self.q_dmg, SRC_Q);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        // each Shadow still standing when the cast time ends throws its own
        let thrown = t + self.q_cast_s;
        if thrown <= self.s.w_shadow_until + EPS {
            self.hit(e, self.q_dmg, self.src_q_mimic);
        }
        if thrown <= self.s.r_shadow_until + EPS {
            self.hit(e, self.q_dmg, self.src_q_mimic);
        }
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        let landed = t + self.r_away_s;
        // Living Shadow goes out before the dash when its Shadow loses no
        // shuriken to the time Zed is away
        if self.ranks.w > 0 && self.ranks.q > 0 {
            let cd = e.basic_cd(self.q_cd);
            let q = pymax(e.st.q_ready, landed);
            if self.mimics(t, q, cd) >= self.mimics(q, q, cd) {
                self.cast_w(e);
            }
        }
        // the Shadow is left at the cast; the mark lands with Zed and stores
        // from then on (the item actives the engine fires at the cast are
        // counted as used on landing, inside the mark)
        self.s.r_shadow_until = t + self.r_shadow_dur;
        self.s.mark_until = landed + self.r_mark_dur;
        // no cast time, but Zed neither casts nor attacks until the dash
        // ends; he is ordered to attack as he reappears
        self.s.busy_until = landed;
        e.st.next_attack = pymax(e.st.next_attack, landed);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        let w = self.w_at(e);
        if w != INF {
            out[n] = (w, Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.marked() {
            out[n] = (self.s.mark_until, Kind::Ev(EV_R_DETONATE));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_DETONATE) => {
                self.absorb(e);
                let dmg = self.r_ad_ratio * e.p.ad + self.r_stored_ratio * self.s.stored;
                self.s.mark_until = INF;
                self.s.stored = 0.0;
                e.deal(dmg, DType::Physical, SRC_R, false, true, 1.0);
                self.s.seen = e.st.total;
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_W_CAST) => self.cast_w(e),
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.hit(e, self.e_dmg, SRC_E);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                // Zed's own slash hit an enemy champion: Living Shadow's
                // running cooldown is cut
                if t < self.s.w_ready {
                    self.s.w_ready = pymax(t, self.s.w_ready - self.e_cdr_w);
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
