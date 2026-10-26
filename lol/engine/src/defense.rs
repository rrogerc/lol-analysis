//! The defender: a build seen from the side that TAKES the damage — the
//! Survival tier. A fight's target is normally a stat dummy (health, armor,
//! magic resist and nothing else); with a `Defender` it is a champion whose
//! items and kit act on the damage on its way in — reductions, shields,
//! Death's Dance's delay, stasis and revives, resists that grow in combat,
//! regeneration and heals — and whose health can climb back.
//!
//! The attacker is untouched: its driver runs exactly as against a dummy and
//! every instance still goes through `Engine::deal`, which hands it here
//! (`def_absorb`) between the attacker's mitigation and the health bar. The
//! defender never deals damage: whatever needs a hit of its own (lifesteal,
//! omnivamp, on-hit healing, Heartsteel's stacks) is out of scope, and the
//! fight is scored on how long the defender lasts (`FightResult::ttk`).
//!
//! Everything here is inert without a defender: a dummy's fight never
//! reaches this module (`Engine::dfd` is `None`), so the damage tier's
//! fights stay bit-identical to their golden fixtures.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::fight::{Engine, S_TTK, S_TTK_EFF};
use crate::fx::{SourceId, SRC_AUTO};
use crate::num::*;
use crate::pyget::*;
use crate::sheet::{ChampBase, Sheet};

// ---------------------------------------------------------------------------
// item effects: the `defense` object of an item-effects.json entry
// ---------------------------------------------------------------------------

/// A once-a-fight effect that fires when damage would take the holder below
/// `threshold` of its maximum health: a shield (Sterak's, Maw, Shieldbow) or
/// bonus health plus a heal over time (Protoplasm Harness).
#[derive(Clone, Debug, Default)]
pub struct Lifeline {
    pub threshold: f64,
    pub magic_only: bool,
    pub duration_s: f64,
    /// Sterak's: the shield holds this long, then decays at a steady rate to
    /// nothing at `duration_s`.
    pub decay_after_s: Option<f64>,
    pub shield_base: f64,
    /// Shieldbow: +value a level from the given level on.
    pub shield_per_level_from: Option<(i64, f64)>,
    pub shield_bonus_ad_ratio: f64,
    pub shield_bonus_hp_pct: f64,
    pub ranged_mult: f64,
    pub bonus_health: Option<ByLevel>,
    pub heal: Option<ByLevel>,
    pub heal_bonus_armor_ratio: f64,
    pub heal_bonus_mr_ratio: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct Steadfast {
    pub stacks: i64,
    pub stack_duration_s: f64,
    pub icd_s: f64,
    pub bonus_mr: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct Voidborn {
    pub after_s: f64,
    pub bonus_resist_frac: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct Drain {
    pub period_s: f64,
    pub bonus_hp_frac: f64,
    pub heal_frac: f64,
    pub radius: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct Revive {
    pub stasis_s: f64,
    pub base_hp_frac: f64,
}

/// One item's defensive effects, or a build's merged: every field is a
/// unique passive in game (or an item a build can only hold once), so the
/// first item carrying one wins.
#[derive(Clone, Debug, Default)]
pub struct DefItem {
    pub attack_dr: f64,
    pub crit_dr: f64,
    /// Death's Dance: (melee share, ranged share, seconds).
    pub defer: Option<(f64, f64, f64)>,
    pub lifeline: Option<Lifeline>,
    pub magic_shield_max_hp: f64,
    pub steadfast: Option<Steadfast>,
    pub voidborn: Option<Voidborn>,
    pub heal_amp: f64,
    pub regen_amp: f64,
    pub drain: Option<Drain>,
    pub as_slow: f64,
    pub as_slow_radius: f64,
    pub revive: Option<Revive>,
    pub stasis_s: Option<f64>,
    pub spell_shield: bool,
}

fn parse_by_level(d: &Bound<'_, PyDict>) -> PyResult<ByLevel> {
    let levels = getvecf(d, "levels")?
        .ok_or_else(|| PyValueError::new_err("byLevel needs levels"))?;
    if levels.len() != 2 {
        return Err(PyValueError::new_err("byLevel levels must be [lo, hi]"));
    }
    Ok(ByLevel { from: reqf(d, "from")?, to: reqf(d, "to")?, lo: levels[0] as i64,
                 hi: levels[1] as i64 })
}

impl DefItem {
    /// The `defense` object of one item-effects.json entry (the whole entry
    /// is passed; an entry without one gives an empty `DefItem`).
    pub fn from_entry(entry: &Bound<'_, PyDict>) -> PyResult<DefItem> {
        let Some(d) = getd(entry, "defense")? else {
            return Ok(DefItem::default());
        };
        let mut out = DefItem {
            attack_dr: getf(&d, "attackDamageReductionPct", 0.0)? / 100.0,
            crit_dr: getf(&d, "critDamageReductionPct", 0.0)? / 100.0,
            magic_shield_max_hp: getf(&d, "magicShieldMaxHpPct", 0.0)? / 100.0,
            heal_amp: getf(&d, "healShieldAmpPct", 0.0)? / 100.0,
            regen_amp: getf(&d, "regenAmpPct", 0.0)? / 100.0,
            as_slow: getf(&d, "attackSpeedSlowPct", 0.0)? / 100.0,
            as_slow_radius: getf(&d, "radius", 0.0)?,
            spell_shield: truthy(&d, "spellShield")?,
            ..DefItem::default()
        };
        if let Some(p) = getd(&d, "deferPct")? {
            out.defer = Some((reqf(&p, "melee")? / 100.0, reqf(&p, "ranged")? / 100.0,
                              reqf(&d, "deferS")?));
        }
        if let Some(l) = getd(&d, "lifeline")? {
            let per_level = match getvecf(&l, "shieldPerLevelFrom")? {
                Some(v) if v.len() == 2 => Some((v[0] as i64, v[1])),
                Some(_) => return Err(PyValueError::new_err("shieldPerLevelFrom is [level, per]")),
                None => None,
            };
            out.lifeline = Some(Lifeline {
                threshold: reqf(&l, "thresholdPct")? / 100.0,
                magic_only: truthy(&l, "magicOnly")?,
                duration_s: reqf(&l, "durationS")?,
                decay_after_s: if has(&l, "decayAfterS")? { Some(reqf(&l, "decayAfterS")?) } else { None },
                shield_base: getf(&l, "shieldBase", 0.0)?,
                shield_per_level_from: per_level,
                shield_bonus_ad_ratio: getf(&l, "shieldBonusAdRatio", 0.0)?,
                shield_bonus_hp_pct: getf(&l, "shieldBonusHpPct", 0.0)?,
                ranged_mult: getf(&l, "rangedMult", 1.0)?,
                bonus_health: match getd(&l, "bonusHealthByLevel")? {
                    Some(b) => Some(parse_by_level(&b)?),
                    None => None,
                },
                heal: match getd(&l, "healByLevel")? {
                    Some(b) => Some(parse_by_level(&b)?),
                    None => None,
                },
                heal_bonus_armor_ratio: getf(&l, "healBonusArmorRatio", 0.0)?,
                heal_bonus_mr_ratio: getf(&l, "healBonusMrRatio", 0.0)?,
            });
        }
        if let Some(s) = getd(&d, "steadfast")? {
            out.steadfast = Some(Steadfast {
                stacks: reqi(&s, "stacks")?,
                stack_duration_s: reqf(&s, "stackDurationS")?,
                icd_s: reqf(&s, "stackIcdS")?,
                bonus_mr: reqf(&s, "bonusMr")?,
            });
        }
        if let Some(v) = getd(&d, "voidborn")? {
            out.voidborn = Some(Voidborn { after_s: reqf(&v, "afterS")?,
                                           bonus_resist_frac: reqf(&v, "bonusResistPct")? / 100.0 });
        }
        if let Some(x) = getd(&d, "drain")? {
            out.drain = Some(Drain {
                period_s: reqf(&x, "periodS")?,
                bonus_hp_frac: reqf(&x, "bonusHpPct")? / 100.0,
                heal_frac: reqf(&x, "healPct")? / 100.0,
                radius: reqf(&x, "radius")?,
            });
        }
        if let Some(r) = getd(&d, "revive")? {
            out.revive = Some(Revive { stasis_s: reqf(&r, "stasisS")?,
                                       base_hp_frac: reqf(&r, "baseHpPct")? / 100.0 });
        }
        if let Some(s) = getd(&d, "stasis")? {
            out.stasis_s = Some(reqf(&s, "durationS")?);
        }
        Ok(out)
    }

    /// A build's merged effects, items in build order.
    pub fn merge<'a, I: IntoIterator<Item = &'a DefItem>>(items: I) -> DefItem {
        let mut m = DefItem::default();
        for it in items {
            if m.attack_dr == 0.0 { m.attack_dr = it.attack_dr; }
            if m.crit_dr == 0.0 { m.crit_dr = it.crit_dr; }
            if m.defer.is_none() { m.defer = it.defer; }
            if m.lifeline.is_none() { m.lifeline = it.lifeline.clone(); }
            if m.magic_shield_max_hp == 0.0 { m.magic_shield_max_hp = it.magic_shield_max_hp; }
            if m.steadfast.is_none() { m.steadfast = it.steadfast; }
            if m.voidborn.is_none() { m.voidborn = it.voidborn; }
            if m.heal_amp == 0.0 { m.heal_amp = it.heal_amp; }
            if m.regen_amp == 0.0 { m.regen_amp = it.regen_amp; }
            if m.drain.is_none() { m.drain = it.drain; }
            if m.as_slow == 0.0 {
                m.as_slow = it.as_slow;
                m.as_slow_radius = it.as_slow_radius;
            }
            if m.revive.is_none() { m.revive = it.revive; }
            if m.stasis_s.is_none() { m.stasis_s = it.stasis_s; }
            m.spell_shield |= it.spell_shield;
        }
        m
    }
}

// ---------------------------------------------------------------------------
// the defending kit (data/builds/<slug>.json with "role": "tank")
// ---------------------------------------------------------------------------

/// Heart Zapper, resolved at the build's level and rank.
#[derive(Clone, Copy, Debug)]
pub struct GuardW {
    pub cd: f64,
    pub cost_frac: f64,
    pub duration_s: f64,
    pub initial_s: f64,
    pub initial_frac: f64,
    pub later_frac: f64,
    pub heal_hit: f64,
    pub heal_miss: f64,
    pub lockout_s: f64,
    pub radius: f64,
}

/// Maximum Dosage, resolved at the build's rank; the per-champion bonus is
/// applied against the attacker when a fight starts.
#[derive(Clone, Copy, Debug)]
pub struct GuardR {
    pub missing_frac: f64,
    pub heal_frac: f64,
    pub duration_s: f64,
    pub bonus_per_champion: f64,
    pub radius: f64,
}

/// What the defending champion's kit does for it while it takes damage.
#[derive(Clone, Debug, Default)]
pub struct Guard {
    /// Innate regeneration as a share of maximum health per second.
    pub regen_frac_per_s: f64,
    /// Bonus attack damage per point of maximum health (Blunt Force Trauma):
    /// only Maw of Malmortius's shield reads the defender's AD.
    pub ad_per_max_hp: f64,
    pub w: Option<GuardW>,
    pub r: Option<GuardR>,
}

fn rank_value(v: &[f64], rank: i64, what: &str) -> PyResult<f64> {
    v.get((rank - 1) as usize)
        .copied()
        .ok_or_else(|| PyValueError::new_err(format!("{what} has no rank {rank}")))
}

impl Guard {
    /// The kit's defensive half at `level` and `ranks` (Dr. Mundo's shape).
    pub fn from_kit(kit: &Bound<'_, PyDict>, level: i64, ranks: Ranks) -> PyResult<Guard> {
        let mut g = Guard::default();
        if let Some(p) = getd(kit, "passive")? {
            if let Some(v) = getvecf(&p, "maxHealthRegenPctPer5sByLevel")? {
                let i = (imin(imax(level, 1), v.len() as i64) - 1) as usize;
                g.regen_frac_per_s = v[i] / 100.0 / 5.0;
            }
        }
        let ab = reqd(kit, "abilities")?;
        if ranks.e > 0 {
            if let Some(e) = getd(&ab, "E")? {
                if let Some(p) = getd(&e, "passive")? {
                    if let Some(v) = getvecf(&p, "bonusAdPctMaxHealth")? {
                        g.ad_per_max_hp = rank_value(&v, ranks.e, "E.passive")? / 100.0;
                    }
                }
            }
        }
        if ranks.w > 0 {
            if let Some(w) = getd(&ab, "W")? {
                if let Some(gh) = getd(&w, "grayHealth")? {
                    let cds = getvecf(&w, "cooldownS")?.unwrap_or_default();
                    let init = parse_by_level(&reqd(&gh, "initialPctByLevel")?)?;
                    g.w = Some(GuardW {
                        cd: rank_value(&cds, ranks.w, "W.cooldownS")?,
                        cost_frac: reqf(&w, "currentHealthCostPct")? / 100.0,
                        duration_s: reqf(&w, "durationS")?,
                        initial_s: reqf(&gh, "initialS")?,
                        initial_frac: init.at(level) / 100.0,
                        later_frac: reqf(&gh, "laterPct")? / 100.0,
                        heal_hit: reqf(&gh, "healPctChampionHit")? / 100.0,
                        heal_miss: reqf(&gh, "healPctNoHit")? / 100.0,
                        lockout_s: reqf(&gh, "recastLockoutS")?,
                        radius: reqf(&w, "radius")?,
                    });
                }
            }
        }
        if ranks.r > 0 {
            if let Some(r) = getd(&ab, "R")? {
                if has(&r, "missingHealthPct")? {
                    let mut bonus = 0.0;
                    let mut radius = 0.0;
                    if let Some(n) = getd(&r, "perNearbyChampion")? {
                        if ranks.r >= reqi(&n, "rank")? {
                            bonus = reqf(&n, "pct")? / 100.0;
                        }
                        radius = reqf(&n, "radius")?;
                    }
                    g.r = Some(GuardR {
                        missing_frac: rank_value(&getvecf(&r, "missingHealthPct")?.unwrap_or_default(),
                                                 ranks.r, "R.missingHealthPct")? / 100.0,
                        heal_frac: rank_value(&getvecf(&r, "maxHealthHealPct")?.unwrap_or_default(),
                                              ranks.r, "R.maxHealthHealPct")? / 100.0,
                        duration_s: reqf(&r, "durationS")?,
                        bonus_per_champion: bonus,
                        radius,
                    });
                }
            }
        }
        Ok(g)
    }
}

// ---------------------------------------------------------------------------
// one defending build
// ---------------------------------------------------------------------------

/// Everything a defending build brings to a fight, settled once per build:
/// its stat sheet's defensive half, its merged item effects and its kit.
#[derive(Clone, Debug)]
pub struct Defense {
    pub level: i64,
    pub hp: f64,
    pub bonus_hp: f64,
    pub base_hp: f64,
    pub base_armor: f64,
    pub base_mr: f64,
    pub bonus_armor: f64,
    pub bonus_mr: f64,
    /// Bonus attack damage at full health: items plus the kit's (Maw reads it).
    pub bonus_ad: f64,
    pub ranged: bool,
    /// Base health regeneration per second, items' "base health regen"
    /// percentages included.
    pub regen_flat_per_s: f64,
    pub basic_cd_mult: f64,
    pub fx: DefItem,
    pub guard: Guard,
}

impl Defense {
    pub fn new(sheet: &Sheet, base: &ChampBase, level: i64, fx: DefItem, guard: Guard,
               ranged: bool) -> Defense {
        let base_hp = stat_at(base.hp, base.hp_per, level);
        let base_armor = stat_at(base.armor, base.armor_per, level);
        let base_mr = stat_at(base.mr, base.mr_per, level);
        let base_regen = stat_at(base.hp_regen, base.hp_regen_per, level);
        Defense {
            level,
            hp: sheet.hp,
            bonus_hp: sheet.hp_bonus,
            base_hp,
            base_armor,
            base_mr,
            bonus_armor: sheet.armor - base_armor,
            bonus_mr: sheet.mr - base_mr,
            bonus_ad: sheet.ad_bonus + guard.ad_per_max_hp * sheet.hp,
            ranged,
            regen_flat_per_s: base_regen * (1.0 + sheet.hp_regen_pct / 100.0) / 5.0,
            basic_cd_mult: sheet.basic_cd_mult,
            fx,
            guard,
        }
    }
}

// ---------------------------------------------------------------------------
// the fight's state on the defending side
// ---------------------------------------------------------------------------

/// Death's Dance bleeds in flight: merged by end time, so one batch of
/// damage (every instance at one instant) is one entry.
pub const MAX_BLEEDS: usize = 32;
/// Most Maximum Dosage thresholds one probe fight watches (see `probe`).
pub const MAX_PROBE: usize = 16;

/// What the defender's side of one fight did, for the dashboard: every
/// number is health (or damage kept off it), summed over the fight.
#[derive(Clone, Debug, Default)]
pub struct DefReport {
    /// damage that reached the defender after reductions: shields, health
    /// and Death's Dance's store together
    pub taken: f64,
    pub hp_lost: f64,
    pub hp_spent: f64,
    pub regen: f64,
    pub healed_w: f64,
    pub healed_r: f64,
    pub healed_lifeline: f64,
    pub healed_drain: f64,
    pub r_base_health: f64,
    pub lifeline_health: f64,
    pub revive_health: f64,
    pub shield_magic: f64,
    pub shield_lifeline: f64,
    pub deferred: f64,
    pub bleed: f64,
    pub blocked: f64,
    pub negated: f64,
    pub reduced_attack: f64,
    pub reduced_crit: f64,
    pub w_casts: i64,
    pub r_at: Option<f64>,
    pub lifeline_at: Option<f64>,
    pub zhonya_at: Option<f64>,
    pub revive_at: Option<f64>,
    pub steadfast_at: Option<f64>,
    pub voidborn_at: Option<f64>,
    pub max_hp_peak: f64,
    /// the Maximum Dosage threshold this fight ran with (1.0 = at the first
    /// hit; 0.0 = only when a hit would kill)
    pub r_threshold: Option<f64>,
    /// A probe fight's crossings: for each watched threshold, the number of
    /// the (non-lethal) hit after which health first sat at or below it, -1
    /// if none did. Every fight is the same up to its Maximum Dosage cast,
    /// so thresholds first crossed by the same hit make the same fight.
    pub cross: [i64; MAX_PROBE],
}

/// One fight's moving parts on the defending side.
#[derive(Clone, Debug)]
pub struct DefState {
    pub max_hp: f64,
    pub bonus_hp: f64,
    /// continuous processes are settled up to here
    pub last_t: f64,
    pub dead: bool,
    pub kaenic: f64,
    pub ll_used: bool,
    pub ll_shield: f64,
    pub ll_until: f64,
    pub ll_decay_from: f64,
    pub ll_decay_rate: f64,
    pub ll_last: f64,
    pub ll_magic_only: bool,
    pub proto_rate: f64,
    pub proto_until: f64,
    pub proto_bonus: f64,
    pub bleeds: [(f64, f64); MAX_BLEEDS],
    pub n_bleeds: usize,
    pub stasis_until: f64,
    pub zhonya_ready: bool,
    pub revive_ready: bool,
    pub spell_shield: bool,
    pub fon_stacks: i64,
    pub fon_next_ok: f64,
    pub fon_until: f64,
    pub fon_on: bool,
    pub jak_at: f64,
    pub jak_on: bool,
    pub drain_next: f64,
    pub w_ready: f64,
    pub w_start: f64,
    pub w_end: f64,
    pub w_active: bool,
    pub w_gray: f64,
    pub r_ready: bool,
    pub r_rate: f64,
    pub r_until: f64,
    pub r_threshold: f64,
    /// The attacker's attacks and casts wait until here: the defender is in
    /// stasis (NEG_INFINITY: never binding).
    pub hold_until: f64,
    /// hits landed so far (`def_after_hit`), and the thresholds a probe
    /// fight watches (`n_probe` of them)
    pub hits: i64,
    pub probe: [f64; MAX_PROBE],
    pub n_probe: usize,
    pub rep: DefReport,
}

/// A defending build ready to fight: its `Defense` and the attacker-facing
/// constants a fight derives from it, plus the state the fight moves.
#[derive(Clone, Debug)]
pub struct Defender {
    pub d: Defense,
    /// the attacker's range decides what reaches it: Heart Zapper's
    /// detonation, Unending Despair's drain, Frozen Heart's aura, Maximum
    /// Dosage's nearby-champion bonus
    pub w_hits: bool,
    pub drain_heal: f64,
    pub as_mult: f64,
    pub r_missing: f64,
    pub r_heal: f64,
    pub s0: DefState,
    pub s: DefState,
}

impl Defender {
    /// `attacker_range` and `attacker_mr` are the attacker's (its attack range
    /// stands in for where it fights from; its magic resist mitigates the
    /// drain whose damage decides Unending Despair's heal). `r_threshold` is
    /// the Maximum Dosage policy: cast once a hit leaves the defender at or
    /// below that share of its maximum health (1.0: at the first hit; 0.0:
    /// only to keep a hit from killing it).
    pub fn new(d: Defense, attacker_range: f64, attacker_mr: f64, r_threshold: f64) -> Defender {
        let fx = &d.fx;
        let w_hits = match &d.guard.w {
            Some(w) => attacker_range <= w.radius,
            None => false,
        };
        let drain_heal = match &fx.drain {
            Some(dr) if attacker_range <= dr.radius => {
                // magic damage after the attacker's resist, healed back 250%
                dr.heal_frac * resist_mult(attacker_mr)
            }
            _ => 0.0,
        };
        let as_mult = if fx.as_slow > 0.0 && attacker_range <= fx.as_slow_radius {
            1.0 - fx.as_slow
        } else {
            1.0
        };
        let (r_missing, r_heal) = match &d.guard.r {
            Some(r) => {
                let near = if attacker_range <= r.radius { r.bonus_per_champion } else { 0.0 };
                (r.missing_frac + near, r.heal_frac + near)
            }
            None => (0.0, 0.0),
        };
        let s0 = DefState {
            max_hp: d.hp,
            bonus_hp: d.bonus_hp,
            last_t: 0.0,
            dead: false,
            kaenic: fx.magic_shield_max_hp * d.hp * (1.0 + fx.heal_amp),
            ll_used: fx.lifeline.is_none(),
            ll_shield: 0.0,
            ll_until: -1.0,
            ll_decay_from: INF,
            ll_decay_rate: 0.0,
            ll_last: 0.0,
            ll_magic_only: false,
            proto_rate: 0.0,
            proto_until: -1.0,
            proto_bonus: 0.0,
            bleeds: [(0.0, 0.0); MAX_BLEEDS],
            n_bleeds: 0,
            stasis_until: -1.0,
            zhonya_ready: fx.stasis_s.is_some(),
            revive_ready: fx.revive.is_some(),
            spell_shield: fx.spell_shield,
            fon_stacks: 0,
            fon_next_ok: 0.0,
            fon_until: -1.0,
            fon_on: false,
            jak_at: match &fx.voidborn { Some(v) => v.after_s, None => INF },
            jak_on: false,
            drain_next: match &fx.drain { Some(dr) if drain_heal > 0.0 => dr.period_s, _ => INF },
            w_ready: if d.guard.w.is_some() { 0.0 } else { INF },
            w_start: -1.0,
            w_end: INF,
            w_active: false,
            w_gray: 0.0,
            r_ready: d.guard.r.is_some(),
            r_rate: 0.0,
            r_until: -1.0,
            r_threshold,
            hold_until: f64::NEG_INFINITY,
            hits: 0,
            probe: [0.0; MAX_PROBE],
            n_probe: 0,
            rep: DefReport { r_threshold: d.guard.r.map(|_| r_threshold), max_hp_peak: d.hp,
                             cross: [-1; MAX_PROBE], ..DefReport::default() },
        };
        Defender { w_hits, drain_heal, as_mult, r_missing, r_heal, s: s0.clone(), s0, d }
    }

    /// Back to the state `new` left, for the next fight.
    pub fn reset(&mut self) {
        self.s = self.s0.clone();
    }

    /// Make this a probe fight: record which hit first leaves health at or
    /// below each of `thresholds` (at most MAX_PROBE) in `DefReport::cross`.
    pub fn probe(&mut self, thresholds: &[f64]) {
        let n = thresholds.len().min(MAX_PROBE);
        self.s0.probe[..n].copy_from_slice(&thresholds[..n]);
        self.s0.n_probe = n;
        self.s = self.s0.clone();
    }

    pub fn crit_dr(&self) -> f64 {
        self.d.fx.crit_dr
    }
}

/// The shares of the defender's regeneration, heals and bleeds between two
/// instants — the continuous half of a fight.
struct Rates {
    regen: f64,
    r: f64,
    proto: f64,
    bleed: f64,
}

impl<'a, 'p> Engine<'a, 'p> {
    /// Until when the attacker's attacks and casts wait (a defender's stasis).
    #[inline(always)]
    pub(crate) fn hold_until(&self) -> f64 {
        match &self.dfd {
            Some(d) => d.s.hold_until,
            None => f64::NEG_INFINITY,
        }
    }

    #[inline]
    fn dfd_mut(&mut self) -> &mut Defender {
        self.dfd.as_deref_mut().expect("a defended fight")
    }

    /// Health up by `amount` (never past the maximum), keeping the batch the
    /// kill-time interpolation reads consistent. Returns what landed.
    fn def_gain(&mut self, amount: f64) -> f64 {
        let max = self.dfd.as_deref().expect("a defended fight").s.max_hp;
        let room = pymax(max - self.st.hp, 0.0);
        let got = pymin(amount, room);
        if got > 0.0 {
            self.st.hp += got;
            if self.st.ev_t == self.st.t {
                self.st.ev_hp0 += got;
            }
        }
        got
    }

    /// Maximum health changed (Maximum Dosage, Protoplasm Harness): what
    /// the attacker reads off the target's maximum follows it.
    fn def_max_hp(&mut self, max_hp: f64, bonus_hp: f64) {
        {
            let s = &mut self.dfd_mut().s;
            s.max_hp = max_hp;
            s.bonus_hp = bonus_hp;
            s.rep.max_hp_peak = pymax(s.rep.max_hp_peak, max_hp);
        }
        self.target_hp = max_hp;
        self.target_bonus_hp = bonus_hp;
        self.retarget();
    }

    /// Resists changed (Jak'Sho, Force of Nature): the resist memos are
    /// keyed on the fight's constants, so they start over.
    fn def_resists(&mut self) {
        let (armor, mr) = {
            let dd = self.dfd.as_deref().expect("a defended fight");
            let (d, s) = (&dd.d, &dd.s);
            let jak = if s.jak_on {
                1.0 + d.fx.voidborn.map(|v| v.bonus_resist_frac).unwrap_or(0.0)
            } else {
                1.0
            };
            let fon = if s.fon_on { d.fx.steadfast.map(|f| f.bonus_mr).unwrap_or(0.0) } else { 0.0 };
            (d.base_armor + d.bonus_armor * jak, d.base_mr + (d.bonus_mr + fon) * jak)
        };
        self.set_target_resists(armor, mr);
    }

    /// The opening: Heart Zapper goes out before the attacker's first hit.
    pub(crate) fn def_start(&mut self) {
        let t = self.st.t;
        self.def_w_cast(t);
    }

    /// Heart Zapper: pay 8% of current health, open the grey-health window.
    fn def_w_cast(&mut self, t: f64) {
        let (cost, cd, dur) = {
            let dd = self.dfd.as_deref().expect("a defended fight");
            let w = match &dd.d.guard.w { Some(w) => *w, None => return };
            if dd.s.w_active || t < dd.s.w_ready || t < dd.s.stasis_until {
                return;
            }
            (w.cost_frac * self.st.hp, w.cd * dd.d.basic_cd_mult, w.duration_s)
        };
        self.st.hp -= cost;
        if self.st.ev_t == t {
            self.st.ev_hp0 -= cost;
        }
        let s = &mut self.dfd_mut().s;
        s.rep.hp_spent += cost;
        s.rep.w_casts += 1;
        s.w_active = true;
        s.w_start = t;
        s.w_end = t + dur;
        s.w_gray = 0.0;
        s.w_ready = t + cd;
    }

    /// Heart Zapper detonates: the grey health comes back, all of it if the
    /// detonation reaches the attacker.
    fn def_w_detonate(&mut self) {
        let heal = {
            let dd = self.dfd.as_deref().expect("a defended fight");
            let w = dd.d.guard.w.expect("an active Heart Zapper");
            let share = if dd.w_hits { w.heal_hit } else { w.heal_miss };
            dd.s.w_gray * share * (1.0 + dd.d.fx.heal_amp)
        };
        let got = self.def_gain(heal);
        let s = &mut self.dfd_mut().s;
        s.rep.healed_w += got;
        s.w_active = false;
        s.w_gray = 0.0;
        s.w_end = INF;
    }

    /// Maximum Dosage: base health from the missing health at once, then its
    /// heal for ten seconds.
    fn def_r_cast(&mut self) {
        let t = self.st.t;
        let (b, max, bonus, rate, until) = {
            let dd = self.dfd.as_deref().expect("a defended fight");
            let r = dd.d.guard.r.expect("a Maximum Dosage");
            let missing = pymax(dd.s.max_hp - self.st.hp, 0.0);
            let b = dd.r_missing * missing;
            (b, dd.s.max_hp + b, dd.s.bonus_hp, dd.r_heal / r.duration_s, t + r.duration_s)
        };
        {
            let s = &mut self.dfd_mut().s;
            s.r_ready = false;
            s.r_rate = rate;
            s.r_until = until;
            s.rep.r_at = Some(t);
            s.rep.r_base_health += b;
        }
        self.def_max_hp(max, bonus);
        // the base health arrives as current health too — not a heal
        self.st.hp += b;
        if self.st.ev_t == t {
            self.st.ev_hp0 += b;
        }
    }

    /// The defender's next timed event (INF: none): Heart Zapper's cast and
    /// detonation, Unending Despair's drain, Jak'Sho waking, Force of
    /// Nature's stacks lapsing. Its own casts wait out a stasis.
    pub(crate) fn def_next(&self) -> f64 {
        let dd = self.dfd.as_deref().expect("a defended fight");
        let s = &dd.s;
        if s.dead {
            return INF;
        }
        let mut t = INF;
        if s.w_active {
            t = pymin(t, pymax(s.w_end, s.stasis_until));
        } else if s.w_ready < INF {
            t = pymin(t, pymax(s.w_ready, s.stasis_until));
        }
        t = pymin(t, pymax(s.drain_next, s.stasis_until));
        if !s.jak_on {
            t = pymin(t, s.jak_at);
        }
        if s.fon_stacks > 0 {
            t = pymin(t, s.fon_until);
        }
        t
    }

    /// Everything of the defender's due at the clock.
    pub(crate) fn def_event(&mut self) {
        let t = self.st.t;
        let (w_due, cast_due, drain_due, jak_due, fon_due) = {
            let s = &self.dfd.as_deref().expect("a defended fight").s;
            let free = t >= s.stasis_until;
            (s.w_active && t >= s.w_end && free,
             !s.w_active && t >= s.w_ready && free,
             t >= s.drain_next && free,
             !s.jak_on && t >= s.jak_at,
             s.fon_stacks > 0 && t >= s.fon_until)
        };
        if jak_due {
            let s = &mut self.dfd_mut().s;
            s.jak_on = true;
            s.rep.voidborn_at = Some(t);
            self.def_resists();
        }
        if fon_due {
            let was_on = {
                let s = &mut self.dfd_mut().s;
                s.fon_stacks = 0;
                let was = s.fon_on;
                s.fon_on = false;
                was
            };
            if was_on {
                self.def_resists();
            }
        }
        if w_due {
            self.def_w_detonate();
        }
        if cast_due {
            self.def_w_cast(t);
        }
        if drain_due {
            let (heal, period) = {
                let dd = self.dfd.as_deref().expect("a defended fight");
                let dr = dd.d.fx.drain.expect("a drain");
                (dd.drain_heal * dr.bonus_hp_frac * dd.s.bonus_hp * (1.0 + dd.d.fx.heal_amp),
                 dr.period_s)
            };
            let got = self.def_gain(heal);
            let s = &mut self.dfd_mut().s;
            s.rep.healed_drain += got;
            s.drain_next = t + period;
        }
    }

    fn def_rates(&self, t: f64) -> Rates {
        let dd = self.dfd.as_deref().expect("a defended fight");
        let (d, s) = (&dd.d, &dd.s);
        let regen = (d.regen_flat_per_s + d.guard.regen_frac_per_s * s.max_hp)
            * (1.0 + d.fx.regen_amp);
        let heal_amp = 1.0 + d.fx.heal_amp;
        let r = if t < s.r_until { s.r_rate * s.max_hp * heal_amp } else { 0.0 };
        let proto = if t < s.proto_until { s.proto_rate * heal_amp } else { 0.0 };
        let mut bleed = 0.0;
        for &(rate, until) in &s.bleeds[..s.n_bleeds] {
            if until > t {
                bleed += rate;
            }
        }
        Rates { regen, r, proto, bleed }
    }

    /// Settle the continuous processes — regeneration, Maximum Dosage's and
    /// Protoplasm Harness's heals, Death's Dance's bleed — up to `t_to`,
    /// piecewise between the instants where a rate changes. Health never
    /// climbs past its maximum; a bleed that outruns the heals kills at the
    /// exact instant health reaches zero (Guardian Angel permitting).
    pub(crate) fn def_advance(&mut self, t_to: f64) {
        loop {
            let (t, dead) = {
                let s = &self.dfd.as_deref().expect("a defended fight").s;
                (s.last_t, s.dead)
            };
            if dead || t_to <= t {
                return;
            }
            // the next instant any rate changes, or the target time
            let seg_end = {
                let s = &self.dfd.as_deref().expect("a defended fight").s;
                let mut e = t_to;
                for x in [s.r_until, s.proto_until, s.stasis_until,
                          if s.w_active { s.w_start + self.def_w_initial() } else { INF }] {
                    if x > t && x < e {
                        e = x;
                    }
                }
                for &(_, until) in &s.bleeds[..s.n_bleeds] {
                    if until > t && until < e {
                        e = until;
                    }
                }
                e
            };
            let dt = seg_end - t;
            let rt = self.def_rates(t);
            let in_stasis = t < self.dfd.as_deref().unwrap().s.stasis_until;
            let heal = rt.regen + rt.r + rt.proto;
            let bleed = if in_stasis { 0.0 } else { rt.bleed };
            if in_stasis {
                self.dfd_mut().s.rep.negated += rt.bleed * dt;
            }
            let hp = self.st.hp;
            let max = self.dfd.as_deref().unwrap().s.max_hp;
            let net = heal - bleed;
            let mut died_at = None;
            let mut new_hp;
            let healed;
            if net >= 0.0 {
                new_hp = hp + net * dt;
                if new_hp > max {
                    new_hp = pymax(max, hp);
                }
                // what the heals added, the bleed they covered included
                healed = (new_hp - hp) + bleed * dt;
            } else {
                new_hp = hp + net * dt;
                healed = heal * dt;
                if new_hp <= 0.0 {
                    let tau = hp / -net;
                    died_at = Some(t + tau);
                    new_hp = 0.0;
                }
            }
            let span = match died_at { Some(x) => x - t, None => dt };
            // credit the heals in proportion to their rates
            if heal > 0.0 {
                let healed = if died_at.is_some() { heal * span } else { healed };
                let s = &mut self.dfd_mut().s;
                s.rep.regen += healed * rt.regen / heal;
                s.rep.healed_r += healed * rt.r / heal;
                s.rep.healed_lifeline += healed * rt.proto / heal;
            }
            if bleed > 0.0 {
                let bled = bleed * span;
                let frac = self.def_w_share(t);
                let s = &mut self.dfd_mut().s;
                s.rep.bleed += bled;
                s.rep.hp_lost += bled;
                if s.w_active {
                    s.w_gray += frac * bled;
                }
            }
            self.st.hp = new_hp;
            let end = match died_at { Some(x) => x, None => seg_end };
            {
                let s = &mut self.dfd_mut().s;
                s.last_t = end;
                // drop the bleeds that ran out
                let mut k = 0;
                for i in 0..s.n_bleeds {
                    if s.bleeds[i].1 > end {
                        s.bleeds[k] = s.bleeds[i];
                        k += 1;
                    }
                }
                s.n_bleeds = k;
            }
            if let Some(x) = died_at {
                if !self.def_revive(x) {
                    self.def_die(x, x);
                    return;
                }
            } else {
                // Protoplasm Harness's bonus health lasts its duration: gone at
                // its end, and health above the new maximum with it
                let (expired, max, bonus, gain) = {
                    let s = &self.dfd.as_deref().unwrap().s;
                    (s.proto_bonus > 0.0 && seg_end >= s.proto_until, s.max_hp, s.bonus_hp,
                     s.proto_bonus)
                };
                if expired {
                    self.dfd_mut().s.proto_bonus = 0.0;
                    self.def_max_hp(max - gain, bonus - gain);
                    if self.st.hp > max - gain {
                        self.st.hp = max - gain;
                    }
                }
            }
        }
    }

    fn def_w_initial(&self) -> f64 {
        self.dfd.as_deref().and_then(|dd| dd.d.guard.w).map(|w| w.initial_s).unwrap_or(0.0)
    }

    /// Heart Zapper's storage share at `t` (0 when it is not out).
    fn def_w_share(&self, t: f64) -> f64 {
        let dd = self.dfd.as_deref().expect("a defended fight");
        match (&dd.d.guard.w, dd.s.w_active) {
            (Some(w), true) => {
                if t - dd.s.w_start <= w.initial_s { w.initial_frac } else { w.later_frac }
            }
            _ => 0.0,
        }
    }

    /// The defender is dead at `t` (for good).
    fn def_die(&mut self, t: f64, eff: f64) {
        self.st.hp = 0.0;
        if self.st.set & S_TTK == 0 {
            self.st.ttk = t;
            self.st.set |= S_TTK;
            self.st.ttk_eff = eff;
            self.st.set |= S_TTK_EFF;
        }
        self.dfd_mut().s.dead = true;
    }

    /// Guardian Angel: a lethal blow (or bleed) at `t` becomes four seconds
    /// of stasis and half the base health back. False when it is spent.
    pub(crate) fn def_revive(&mut self, t: f64) -> bool {
        let (ready, rv, base_hp) = {
            let dd = self.dfd.as_deref().expect("a defended fight");
            (dd.s.revive_ready, dd.d.fx.revive, dd.d.base_hp)
        };
        if !ready {
            return false;
        }
        let rv = rv.expect("a revive");
        let back = rv.base_hp_frac * base_hp;
        {
            let s = &mut self.dfd_mut().s;
            s.revive_ready = false;
            s.rep.revive_at = Some(t);
            s.rep.revive_health += back;
            s.stasis_until = t + rv.stasis_s;
            // a resurrection ends Maximum Dosage (the wiki) and the bleed
            // of the blow it undoes
            s.r_until = pymin(s.r_until, t);
            s.n_bleeds = 0;
        }
        self.st.hp = back;
        // a fresh batch: the next blow's interpolation starts from here
        self.st.ev_t = t;
        self.st.ev_hp0 = back;
        self.st.ev_dmg = 0.0;
        self.dfd_mut().s.hold_until = pymax(self.dfd_mut().s.hold_until, t + rv.stasis_s);
        true
    }

    /// One damage instance on its way to the defender's health, after the
    /// attacker's own mitigation: `None` when it never lands (a spell shield
    /// or stasis took it), otherwise (what reached the defender, what came
    /// off its health now). Reductions, Death's Dance's store, shields and
    /// the once-a-fight saves happen here in that order; the blow itself is
    /// left for `deal` to land.
    pub(crate) fn def_absorb(&mut self, dmg: f64, dtype: DType, source: SourceId, ability: bool)
        -> Option<(f64, f64)> {
        let t = self.st.t;
        let (in_stasis, shielded) = {
            let s = &self.dfd.as_deref().expect("a defended fight").s;
            (t < s.stasis_until, ability && s.spell_shield)
        };
        if in_stasis {
            self.dfd_mut().s.rep.negated += dmg;
            return None;
        }
        if shielded {
            let s = &mut self.dfd_mut().s;
            s.spell_shield = false;
            s.rep.blocked += dmg;
            return None;
        }
        let mut dmg = dmg;
        let attack_dr = self.dfd.as_deref().unwrap().d.fx.attack_dr;
        if attack_dr > 0.0 && source == SRC_AUTO {
            let cut = dmg * attack_dr;
            dmg -= cut;
            self.dfd_mut().s.rep.reduced_attack += cut;
        }
        let received = dmg;
        // Death's Dance: a share of physical and magic damage bleeds later
        let defer = self.dfd.as_deref().unwrap().d.fx.defer;
        if let Some((melee, ranged, secs)) = defer {
            if dtype != DType::True {
                let share = if self.dfd.as_deref().unwrap().d.ranged { ranged } else { melee };
                let stored = dmg * share;
                dmg -= stored;
                let s = &mut self.dfd_mut().s;
                s.rep.deferred += stored;
                let until = t + secs;
                let rate = stored / secs;
                if s.n_bleeds > 0 && s.bleeds[s.n_bleeds - 1].1 == until {
                    s.bleeds[s.n_bleeds - 1].0 += rate;
                } else if s.n_bleeds < MAX_BLEEDS {
                    s.bleeds[s.n_bleeds] = (rate, until);
                    s.n_bleeds += 1;
                } else {
                    // never reached in practice (a bleed per instant, three
                    // seconds each): fold into the last one rather than drop
                    s.bleeds[MAX_BLEEDS - 1].0 += rate;
                }
            }
        }
        self.dfd_mut().s.rep.taken += received;
        // shields, then the lifeline if this blow would cross its threshold
        let mut left = self.def_shields(dmg, dtype);
        let (ll_armed, threshold, magic_only) = {
            let dd = self.dfd.as_deref().unwrap();
            match (&dd.d.fx.lifeline, dd.s.ll_used) {
                (Some(l), false) => (true, l.threshold, l.magic_only),
                _ => (false, 0.0, false),
            }
        };
        if ll_armed && (!magic_only || dtype == DType::Magic) {
            let max = self.dfd.as_deref().unwrap().s.max_hp;
            if self.st.hp - left < threshold * max {
                self.def_lifeline();
                left = self.def_shields(left, dtype);
            }
        }
        // a blow that would kill: Heart Zapper detonates early, Maximum
        // Dosage goes out, Zhonya's is pressed — whatever saves him, in that
        // order (Guardian Angel waits for the blow itself: see `deal`)
        if self.st.hp - left <= 0.0 {
            let (w_ok, r_ok, z_ok) = {
                let dd = self.dfd.as_deref().unwrap();
                let s = &dd.s;
                let w_ok = match &dd.d.guard.w {
                    Some(w) => s.w_active && t - s.w_start >= w.lockout_s,
                    None => false,
                };
                (w_ok, s.r_ready, s.zhonya_ready)
            };
            if w_ok {
                self.def_w_detonate();
            }
            if self.st.hp - left <= 0.0 && r_ok {
                self.def_r_cast();
            }
            if self.st.hp - left <= 0.0 && z_ok {
                let dur = self.dfd.as_deref().unwrap().d.fx.stasis_s.expect("a stasis");
                let s = &mut self.dfd_mut().s;
                s.zhonya_ready = false;
                s.rep.zhonya_at = Some(t);
                s.stasis_until = t + dur;
                s.rep.negated += left;
                // what the shields and the store took stays taken
                self.dfd_mut().s.hold_until = pymax(self.dfd_mut().s.hold_until, t + dur);
                return None;
            }
        }
        Some((received, left))
    }

    /// Run `dmg` through the shields standing (magic-only ones first for
    /// magic damage); returns what gets past them.
    fn def_shields(&mut self, dmg: f64, dtype: DType) -> f64 {
        let t = self.st.t;
        let s = &mut self.dfd_mut().s;
        let mut left = dmg;
        if dtype == DType::Magic && s.kaenic > 0.0 && left > 0.0 {
            let used = pymin(s.kaenic, left);
            s.kaenic -= used;
            left -= used;
            s.rep.shield_magic += used;
        }
        if s.ll_shield > 0.0 {
            if t >= s.ll_until {
                s.ll_shield = 0.0;
            } else if t > s.ll_decay_from {
                let from = pymax(s.ll_last, s.ll_decay_from);
                s.ll_shield = pymax(0.0, s.ll_shield - s.ll_decay_rate * (t - from));
            }
            s.ll_last = t;
            if left > 0.0 && s.ll_shield > 0.0 && (!s.ll_magic_only || dtype == DType::Magic) {
                let used = pymin(s.ll_shield, left);
                s.ll_shield -= used;
                left -= used;
                s.rep.shield_lifeline += used;
            }
        }
        left
    }

    /// The lifeline fires: a shield, or Protoplasm Harness's bonus health and
    /// heal over time.
    fn def_lifeline(&mut self) {
        let t = self.st.t;
        let (l, d_level, bonus_ad, bonus_hp, ranged, bonus_armor, bonus_mr, heal_amp, max) = {
            let dd = self.dfd.as_deref().unwrap();
            let d = &dd.d;
            (d.fx.lifeline.clone().expect("a lifeline"), d.level, d.bonus_ad, dd.s.bonus_hp,
             d.ranged, d.bonus_armor, d.bonus_mr, 1.0 + d.fx.heal_amp, dd.s.max_hp)
        };
        {
            let s = &mut self.dfd_mut().s;
            s.ll_used = true;
            s.rep.lifeline_at = Some(t);
        }
        if let Some(bh) = &l.bonus_health {
            // Protoplasm Harness: more maximum (and current) health for the
            // duration, and a heal spread over it
            let gain = bh.at(d_level);
            let heal = match &l.heal {
                Some(h) => h.at(d_level) + l.heal_bonus_armor_ratio * bonus_armor
                    + l.heal_bonus_mr_ratio * bonus_mr,
                None => 0.0,
            };
            {
                let s = &mut self.dfd_mut().s;
                s.proto_bonus = gain;
                s.proto_rate = heal / l.duration_s;
                s.proto_until = t + l.duration_s;
                s.rep.lifeline_health += gain;
            }
            self.def_max_hp(max + gain, bonus_hp + gain);
            self.st.hp += gain;
            if self.st.ev_t == t {
                self.st.ev_hp0 += gain;
            }
            return;
        }
        let mut amt = l.shield_base;
        if let Some((from, per)) = l.shield_per_level_from {
            if d_level >= from {
                amt += per * (d_level - from + 1) as f64;
            }
        }
        amt += l.shield_bonus_ad_ratio * bonus_ad + l.shield_bonus_hp_pct / 100.0 * bonus_hp;
        if ranged {
            amt *= l.ranged_mult;
        }
        amt *= heal_amp;
        let s = &mut self.dfd_mut().s;
        s.ll_shield = amt;
        s.ll_until = t + l.duration_s;
        s.ll_last = t;
        s.ll_magic_only = l.magic_only;
        match l.decay_after_s {
            Some(hold) => {
                s.ll_decay_from = t + hold;
                s.ll_decay_rate = amt / (l.duration_s - hold);
            }
            None => {
                s.ll_decay_from = INF;
                s.ll_decay_rate = 0.0;
            }
        }
    }

    /// After a blow landed: Heart Zapper stores its share, Force of Nature
    /// counts it, and Maximum Dosage goes out once health is at or below the
    /// fight's threshold.
    pub(crate) fn def_after_hit(&mut self, dtype: DType, loss: f64) {
        let t = self.st.t;
        let share = self.def_w_share(t);
        let mut fon_changed = false;
        let hp = self.st.hp;
        {
            let dd = self.dfd.as_deref_mut().unwrap();
            let steadfast = dd.d.fx.steadfast;
            let s = &mut dd.s;
            s.hits += 1;
            for k in 0..s.n_probe {
                if s.rep.cross[k] < 0 && hp <= s.probe[k] * s.max_hp {
                    s.rep.cross[k] = s.hits;
                }
            }
            if s.w_active {
                s.w_gray += share * loss;
            }
            if let (Some(f), DType::Magic) = (steadfast, dtype) {
                if s.fon_stacks > 0 && t >= s.fon_until {
                    // they lapsed before this one
                    s.fon_stacks = 0;
                    if s.fon_on {
                        s.fon_on = false;
                        fon_changed = true;
                    }
                }
                if t >= s.fon_next_ok && s.fon_stacks < f.stacks {
                    s.fon_stacks += 1;
                    s.fon_next_ok = t + f.icd_s;
                    if s.fon_stacks == f.stacks && !s.fon_on {
                        s.fon_on = true;
                        s.rep.steadfast_at = Some(t);
                        fon_changed = true;
                    }
                }
                if s.fon_stacks > 0 {
                    s.fon_until = t + f.stack_duration_s;
                }
            }
        }
        if fon_changed {
            self.def_resists();
        }
        let (r_due, dead) = {
            let s = &self.dfd.as_deref().unwrap().s;
            (s.r_ready && self.st.hp <= s.r_threshold * s.max_hp, s.dead)
        };
        if r_due && !dead && self.st.hp > 0.0 {
            self.def_r_cast();
        }
    }
}
