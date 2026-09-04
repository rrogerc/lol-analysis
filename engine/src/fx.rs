//! Item effects: the hand-curated overlay (data/builds/item-effects.json)
//! parsed once per item into typed structs, and the merge of a build's items
//! into one `Fx` — builds.py's merge_effects: list-valued effects concatenate
//! in item order, same-named unique passives keep the first.

use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::sync::{OnceLock, RwLock};

use crate::num::{ByLevel, DType};
use crate::pyget::*;

/// Damage-source labels (the breakdown's keys) interned process-wide. The
/// kit's own labels take the first ids so the engine can test for "auto".
pub type SourceId = u16;

pub const KIT_SOURCES: [&str; 18] = [
    "auto", "Q", "Q empowered", "E", "W", "R", "E onhit", "E active", "wave", "execute",
    "muramana", "eclipse", "botrk", "kraken", "hullbreaker", "spellblade", "malignance",
    "stormsurge",
];
pub const SRC_AUTO: SourceId = 0;
pub const SRC_Q: SourceId = 1;
pub const SRC_Q_EMPOWERED: SourceId = 2;
pub const SRC_E: SourceId = 3;
pub const SRC_W: SourceId = 4;
pub const SRC_R: SourceId = 5;
pub const SRC_E_ONHIT: SourceId = 6;
pub const SRC_E_ACTIVE: SourceId = 7;
pub const SRC_WAVE: SourceId = 8;
pub const SRC_EXECUTE: SourceId = 9;
pub const SRC_MURAMANA: SourceId = 10;
pub const SRC_ECLIPSE: SourceId = 11;
pub const SRC_BOTRK: SourceId = 12;
pub const SRC_KRAKEN: SourceId = 13;
pub const SRC_HULLBREAKER: SourceId = 14;
pub const SRC_SPELLBLADE: SourceId = 15;
pub const SRC_MALIGNANCE: SourceId = 16;
pub const SRC_STORMSURGE: SourceId = 17;

fn sources() -> &'static RwLock<Vec<String>> {
    static TABLE: OnceLock<RwLock<Vec<String>>> = OnceLock::new();
    TABLE.get_or_init(|| RwLock::new(KIT_SOURCES.iter().map(|s| s.to_string()).collect()))
}

pub fn intern(name: &str) -> SourceId {
    {
        let t = sources().read().unwrap();
        if let Some(i) = t.iter().position(|s| s == name) {
            return i as SourceId;
        }
    }
    let mut t = sources().write().unwrap();
    if let Some(i) = t.iter().position(|s| s == name) {
        return i as SourceId;
    }
    t.push(name.to_string());
    (t.len() - 1) as SourceId
}

pub fn source_name(id: SourceId) -> String {
    sources().read().unwrap()[id as usize].clone()
}

pub fn source_count() -> usize {
    sources().read().unwrap().len()
}

#[derive(Clone, Debug)]
pub struct OnHit {
    pub base: f64,
    pub ap_ratio: f64,
    pub bonus_ad_ratio: f64,
    pub max_mana_pct: f64,
    pub self_max_hp_pct: Option<(f64, f64)>, // (melee, ranged)
    pub dtype: DType,
    pub source: SourceId,
}

#[derive(Clone, Debug)]
pub struct OnHitCurrent {
    pub melee_pct: f64,
    pub ranged_pct: f64,
    pub dtype: DType,
}

#[derive(Clone, Debug)]
pub struct DmgAmp {
    pub pct_per_stack: f64,
    pub max_stacks: i64,
}

#[derive(Clone, Debug)]
pub struct FlatAmp {
    pub pct: f64,
    pub dtype: Option<DType>, // None: "all"
}

#[derive(Clone, Debug)]
pub struct Burn {
    pub max_hp_pct_total: Option<f64>,
    pub total_base: f64,
    pub total_ap_ratio: f64,
    pub duration_s: f64,
    pub tick_s: f64,
    pub dtype: DType,
    pub source: SourceId,
}

#[derive(Clone, Debug)]
pub struct ActiveOnce {
    pub base: f64,
    pub by_level: Option<ByLevel>,
    pub ad_ratio: f64,
    pub ap_ratio: f64,
    pub dtype: DType,
    pub source: SourceId,
}

#[derive(Clone, Debug)]
pub struct Energized {
    pub bonus: f64,
    pub dtype: DType,
    pub extra_stacks_per_attack: f64,
    pub source: SourceId,
}

#[derive(Clone, Debug)]
pub struct Spellblade {
    pub base_ad_ratio: f64,
    pub ap_ratio: f64,
    pub per_crit_chance_pct: f64,
    pub icd_s: f64,
    pub dtype: DType,
    pub reapply_onhit: bool,
}

#[derive(Clone, Debug)]
pub struct AsStacking {
    pub pct_per_stack: f64,
    pub max_stacks: i64,
}

#[derive(Clone, Debug)]
pub struct Phantom {
    pub stacks_needed: i64,
}

#[derive(Clone, Debug)]
pub struct Kraken {
    pub base_by_level: ByLevel,
    pub ranged_mult: f64,
    pub missing_melee: f64,
    pub missing_ranged: f64,
    pub dtype: DType,
}

#[derive(Clone, Debug)]
pub struct Stacks {
    pub pct_per_stack: f64,
    pub max_stacks: i64,
}

#[derive(Clone, Debug)]
pub struct MagicCrit {
    pub below_target_hp_pct: f64,
    pub crit_dmg_pct: f64,
}

#[derive(Clone, Debug)]
pub struct UltBurn {
    pub total_base: f64,
    pub total_ap_ratio: f64,
    pub duration_s: f64,
    pub mr_reduction: f64,
}

#[derive(Clone, Debug)]
pub struct Flurry {
    pub as_pct: f64,
    pub duration_s: f64,
    pub cooldown_s: f64,
    pub refund_on_hit_s: f64,
    pub refund_crit_extra_s: f64,
}

#[derive(Clone, Debug)]
pub struct Stormsurge {
    pub threshold_pct: f64,
    pub window_s: f64,
    pub delay_s: f64,
    pub base: f64,
    pub ap_ratio: f64,
    pub dtype: DType,
}

#[derive(Clone, Debug)]
pub struct AbilityManaProc {
    pub pct_by_level: ByLevel,
    pub dtype: DType,
}

#[derive(Clone, Debug)]
pub struct AbilityProcOnce {
    pub base: f64,
    pub ap_ratio: f64,
    pub dtype: DType,
    pub source: SourceId,
}

#[derive(Clone, Debug)]
pub struct ManaActive {
    pub amp_base_pct: f64,
    pub amp_per_100_bonus_mana: f64,
    pub duration_s: f64,
    pub basic_cd_faster_pct: f64,
}

#[derive(Clone, Debug)]
pub struct OnUltCast {
    pub as_pct: f64,
    pub duration_s: f64,
}

#[derive(Clone, Debug)]
pub struct UltAttackSteroid {
    pub attacks: i64,
    pub as_pct: f64,
    pub crit_floor_ev: f64,
}

#[derive(Clone, Debug)]
pub struct HitPairProc {
    pub max_hp_pct_melee: f64,
    pub max_hp_pct_ranged: f64,
    pub dtype: DType,
}

#[derive(Clone, Debug)]
pub struct NthHitProc {
    pub stacks_needed_melee: i64,
    pub stacks_needed_ranged: i64,
    pub base_ad_ratio_melee: f64,
    pub base_ad_ratio_ranged: f64,
    pub self_max_hp_pct_melee: f64,
    pub self_max_hp_pct_ranged: f64,
    pub dtype: DType,
}

#[derive(Clone, Debug)]
pub struct Hypershot {
    pub amp_pct: f64,
    pub duration_s: f64,
}

#[derive(Clone, Debug)]
pub struct OpenerLethality {
    pub duration_s: f64,
    pub ranged: f64,
    pub melee: f64,
}

#[derive(Clone, Debug)]
pub struct FirstAttackBonus {
    pub base: f64,
    pub per_lethality: f64,
    pub dtype: DType,
    pub source: SourceId,
}

#[derive(Clone, Debug)]
pub struct AttackAmp {
    pub max_pct: f64,
    pub max_at_range: f64,
}

/// The unique passives (SINGLETON_FX in builds.py): the first item carrying
/// one wins the merge.
#[derive(Clone, Debug, Default)]
pub struct Singletons {
    pub spellblade: Option<Spellblade>,
    pub as_stacking: Option<AsStacking>,
    pub phantom: Option<Phantom>,
    pub kraken: Option<Kraken>,
    pub alt_pen: Option<Stacks>,
    pub magic_crit: Option<MagicCrit>,
    pub ult_burn: Option<UltBurn>,
    pub navori_cdr: Option<f64>,
    pub flurry: Option<Flurry>,
    pub execute_pct: Option<f64>,
    pub giant_slayer: Option<f64>, // maxPct
    pub stormsurge: Option<Stormsurge>,
    pub ability_mana_proc: Option<AbilityManaProc>,
    pub ability_proc_once: Option<AbilityProcOnce>,
    pub armor_shred: Option<Stacks>,
    pub mr_shred: Option<Stacks>,
    pub ability_amp_stacking: Option<Stacks>,
    pub mana_active: Option<ManaActive>,
    pub on_ult_cast: Option<OnUltCast>,
    pub ult_attack_steroid: Option<UltAttackSteroid>,
    pub hit_pair_proc: Option<HitPairProc>,
    pub nth_hit_proc: Option<NthHitProc>,
    pub hypershot: Option<Hypershot>,
    pub opener_lethality: Option<OpenerLethality>,
    pub first_attack_bonus: Option<FirstAttackBonus>,
    pub first_attack_crit_floor_ev: Option<f64>,
    pub attack_amp: Option<AttackAmp>,
}

/// One item's overlay entry: what the stat sheet reads plus what the fight
/// reads.
#[derive(Clone, Debug, Default)]
pub struct ItemFx {
    // stat-sheet side (resolve_stats)
    pub ap_mult: f64,
    pub stacked: Vec<(crate::sheet::SK, f64)>,
    pub ap_from_bonus_hp_pct: f64,
    pub ad_from_bonus_hp_pct: f64,
    pub ap_from_bonus_mana_pct: f64,
    pub ad_from_max_mana_pct: f64,
    pub crit_chance_stacked_pct: f64,
    pub basic_ability_haste: f64,
    pub haste_from_bonus_ad: Option<(f64, f64)>, // (base, perBonusAdPct)
    pub needs_mana: bool,
    // fight side, list-valued
    pub onhit: Vec<OnHit>,
    pub onhit_current_hp: Option<OnHitCurrent>,
    pub dmg_amp: Option<DmgAmp>,
    pub flat_amp: Option<FlatAmp>,
    pub burn: Option<Burn>,
    pub active_once: Option<ActiveOnce>,
    pub energized: Option<Energized>,
    pub s: Singletons,
}

/// A build's merged effects.
#[derive(Clone, Debug, Default)]
pub struct Fx {
    pub onhit: Vec<OnHit>,
    pub onhit_current_hp: Vec<OnHitCurrent>,
    pub dmg_amps: Vec<DmgAmp>,
    pub flat_amps: Vec<FlatAmp>,
    pub burns: Vec<Burn>,
    pub actives_once: Vec<ActiveOnce>,
    pub energized: Vec<Energized>,
    pub s: Singletons,
}

fn parse_by_level(d: &Bound<'_, PyDict>) -> PyResult<ByLevel> {
    let levels = getvecf(d, "levels")?.ok_or_else(|| PyKeyError::new_err("levels"))?;
    if levels.len() != 2 {
        return Err(PyKeyError::new_err("levels"));
    }
    Ok(ByLevel { from: reqf(d, "from")?, to: reqf(d, "to")?, lo: levels[0] as i64,
                 hi: levels[1] as i64 })
}

fn dtype_of(d: &Bound<'_, PyDict>) -> PyResult<DType> {
    Ok(DType::parse(&reqs(d, "damageType")?))
}

fn parse_onhit(d: &Bound<'_, PyDict>) -> PyResult<OnHit> {
    let self_max = match getd(d, "selfMaxHpPct")? {
        Some(s) => Some((reqf(&s, "melee")?, reqf(&s, "ranged")?)),
        None => None,
    };
    Ok(OnHit {
        base: reqf(d, "base")?,
        ap_ratio: getf(d, "apRatio", 0.0)?,
        bonus_ad_ratio: getf(d, "bonusAdRatio", 0.0)?,
        max_mana_pct: getf(d, "maxManaPct", 0.0)?,
        self_max_hp_pct: self_max,
        dtype: dtype_of(d)?,
        source: intern(&gets(d, "source", "onhit")?),
    })
}

fn parse_onhit_current(d: &Bound<'_, PyDict>) -> PyResult<OnHitCurrent> {
    Ok(OnHitCurrent { melee_pct: reqf(d, "meleePct")?, ranged_pct: reqf(d, "rangedPct")?,
                      dtype: dtype_of(d)? })
}

fn parse_dmg_amp(d: &Bound<'_, PyDict>) -> PyResult<DmgAmp> {
    Ok(DmgAmp { pct_per_stack: reqf(d, "pctPerStack")?, max_stacks: reqi(d, "maxStacks")? })
}

fn parse_flat_amp(d: &Bound<'_, PyDict>) -> PyResult<FlatAmp> {
    let dt = reqs(d, "damageType")?;
    Ok(FlatAmp { pct: reqf(d, "pct")?,
                 dtype: if dt == "all" { None } else { Some(DType::parse(&dt)) } })
}

fn parse_burn(d: &Bound<'_, PyDict>) -> PyResult<Burn> {
    let max_hp = if has(d, "maxHpPctTotal")? { Some(reqf(d, "maxHpPctTotal")?) } else { None };
    Ok(Burn {
        max_hp_pct_total: max_hp,
        total_base: if max_hp.is_none() { reqf(d, "totalBase")? } else { getf(d, "totalBase", 0.0)? },
        total_ap_ratio: if max_hp.is_none() { reqf(d, "totalApRatio")? } else { getf(d, "totalApRatio", 0.0)? },
        duration_s: reqf(d, "durationS")?,
        tick_s: reqf(d, "tickS")?,
        dtype: dtype_of(d)?,
        source: intern(&gets(d, "source", "burn")?),
    })
}

fn parse_active_once(d: &Bound<'_, PyDict>) -> PyResult<ActiveOnce> {
    Ok(ActiveOnce {
        base: getf(d, "base", 0.0)?,
        by_level: match getd(d, "byLevel")? { Some(b) => Some(parse_by_level(&b)?), None => None },
        ad_ratio: getf(d, "adRatio", 0.0)?,
        ap_ratio: getf(d, "apRatio", 0.0)?,
        dtype: dtype_of(d)?,
        source: intern(&gets(d, "source", "active")?),
    })
}

fn parse_energized(d: &Bound<'_, PyDict>) -> PyResult<Energized> {
    Ok(Energized {
        bonus: reqf(d, "bonus")?,
        dtype: dtype_of(d)?,
        extra_stacks_per_attack: getf(d, "extraStacksPerAttack", 0.0)?,
        source: intern(&gets(d, "source", "energized")?),
    })
}

fn parse_stacks(d: &Bound<'_, PyDict>) -> PyResult<Stacks> {
    Ok(Stacks { pct_per_stack: reqf(d, "pctPerStack")?, max_stacks: reqi(d, "maxStacks")? })
}

/// A dict-valued unique passive: present and non-empty, as Python's
/// truthiness test on the dict would have it.
fn getfx<'py>(d: &Bound<'py, PyDict>, key: &str) -> PyResult<Option<Bound<'py, PyDict>>> {
    Ok(getd(d, key)?.filter(|x| !x.is_empty()))
}

/// A number-valued unique passive: present and nonzero.
fn getnum(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<f64>> {
    Ok(match get(d, key)? {
        Some(v) => {
            let f: f64 = v.extract()?;
            if f != 0.0 { Some(f) } else { None }
        }
        None => None,
    })
}

fn parse_singletons(d: &Bound<'_, PyDict>) -> PyResult<Singletons> {
    let mut s = Singletons::default();
    if let Some(x) = getfx(d, "spellblade")? {
        s.spellblade = Some(Spellblade {
            base_ad_ratio: reqf(&x, "baseAdRatio")?,
            ap_ratio: reqf(&x, "apRatio")?,
            per_crit_chance_pct: getf(&x, "perCritChancePct", 0.0)?,
            icd_s: reqf(&x, "icdS")?,
            dtype: dtype_of(&x)?,
            reapply_onhit: truthy(&x, "reapplyOnhit")?,
        });
    }
    if let Some(x) = getfx(d, "asStacking")? {
        s.as_stacking = Some(AsStacking { pct_per_stack: reqf(&x, "pctPerStack")?,
                                          max_stacks: reqi(&x, "maxStacks")? });
    }
    if let Some(x) = getfx(d, "phantom")? {
        s.phantom = Some(Phantom { stacks_needed: reqi(&x, "stacksNeeded")? });
    }
    if let Some(x) = getfx(d, "kraken")? {
        let m = reqd(&x, "missingHpAmpMaxPct")?;
        s.kraken = Some(Kraken {
            base_by_level: parse_by_level(&reqd(&x, "baseByLevel")?)?,
            ranged_mult: reqf(&x, "rangedMult")?,
            missing_melee: reqf(&m, "melee")?,
            missing_ranged: reqf(&m, "ranged")?,
            dtype: dtype_of(&x)?,
        });
    }
    if let Some(x) = getfx(d, "altPen")? {
        s.alt_pen = Some(parse_stacks(&x)?);
    }
    if let Some(x) = getfx(d, "magicCrit")? {
        s.magic_crit = Some(MagicCrit { below_target_hp_pct: reqf(&x, "belowTargetHpPct")?,
                                        crit_dmg_pct: reqf(&x, "critDmgPct")? });
    }
    if let Some(x) = getfx(d, "ultBurn")? {
        s.ult_burn = Some(UltBurn {
            total_base: reqf(&x, "totalBase")?,
            total_ap_ratio: reqf(&x, "totalApRatio")?,
            duration_s: reqf(&x, "durationS")?,
            mr_reduction: reqf(&x, "mrReduction")?,
        });
    }
    s.navori_cdr = getnum(d, "navoriCdr")?;
    if let Some(x) = getfx(d, "flurry")? {
        s.flurry = Some(Flurry {
            as_pct: reqf(&x, "asPct")?,
            duration_s: reqf(&x, "durationS")?,
            cooldown_s: reqf(&x, "cooldownS")?,
            refund_on_hit_s: reqf(&x, "refundOnHitS")?,
            refund_crit_extra_s: reqf(&x, "refundCritExtraS")?,
        });
    }
    s.execute_pct = getnum(d, "executePct")?;
    if let Some(x) = getfx(d, "giantSlayer")? {
        s.giant_slayer = Some(reqf(&x, "maxPct")?);
    }
    if let Some(x) = getfx(d, "stormsurge")? {
        s.stormsurge = Some(Stormsurge {
            threshold_pct: reqf(&x, "thresholdPct")?,
            window_s: reqf(&x, "windowS")?,
            delay_s: reqf(&x, "delayS")?,
            base: reqf(&x, "base")?,
            ap_ratio: reqf(&x, "apRatio")?,
            dtype: dtype_of(&x)?,
        });
    }
    if let Some(x) = getfx(d, "abilityManaProc")? {
        s.ability_mana_proc = Some(AbilityManaProc {
            pct_by_level: parse_by_level(&reqd(&x, "pctByLevel")?)?,
            dtype: dtype_of(&x)?,
        });
    }
    if let Some(x) = getfx(d, "abilityProcOnce")? {
        s.ability_proc_once = Some(AbilityProcOnce {
            base: reqf(&x, "base")?,
            ap_ratio: reqf(&x, "apRatio")?,
            dtype: dtype_of(&x)?,
            source: intern(&reqs(&x, "source")?),
        });
    }
    if let Some(x) = getfx(d, "armorShred")? {
        s.armor_shred = Some(parse_stacks(&x)?);
    }
    if let Some(x) = getfx(d, "mrShred")? {
        s.mr_shred = Some(parse_stacks(&x)?);
    }
    if let Some(x) = getfx(d, "abilityAmpStacking")? {
        s.ability_amp_stacking = Some(parse_stacks(&x)?);
    }
    if let Some(x) = getfx(d, "manaActive")? {
        s.mana_active = Some(ManaActive {
            amp_base_pct: reqf(&x, "ampBasePct")?,
            amp_per_100_bonus_mana: reqf(&x, "ampPer100BonusMana")?,
            duration_s: reqf(&x, "durationS")?,
            basic_cd_faster_pct: reqf(&x, "basicCdFasterPct")?,
        });
    }
    if let Some(x) = getfx(d, "onUltCast")? {
        s.on_ult_cast = Some(OnUltCast { as_pct: reqf(&x, "asPct")?, duration_s: reqf(&x, "durationS")? });
    }
    if let Some(x) = getfx(d, "ultAttackSteroid")? {
        s.ult_attack_steroid = Some(UltAttackSteroid {
            attacks: reqi(&x, "attacks")?,
            as_pct: reqf(&x, "asPct")?,
            crit_floor_ev: getf(&x, "critFloorEv", 1.0)?,
        });
    }
    if let Some(x) = getfx(d, "hitPairProc")? {
        s.hit_pair_proc = Some(HitPairProc {
            max_hp_pct_melee: reqf(&x, "maxHpPctMelee")?,
            max_hp_pct_ranged: reqf(&x, "maxHpPctRanged")?,
            dtype: dtype_of(&x)?,
        });
    }
    if let Some(x) = getfx(d, "nthHitProc")? {
        s.nth_hit_proc = Some(NthHitProc {
            stacks_needed_melee: reqi(&x, "stacksNeededMelee")?,
            stacks_needed_ranged: reqi(&x, "stacksNeededRanged")?,
            base_ad_ratio_melee: reqf(&x, "baseAdRatioMelee")?,
            base_ad_ratio_ranged: reqf(&x, "baseAdRatioRanged")?,
            self_max_hp_pct_melee: reqf(&x, "selfMaxHpPctMelee")?,
            self_max_hp_pct_ranged: reqf(&x, "selfMaxHpPctRanged")?,
            dtype: dtype_of(&x)?,
        });
    }
    if let Some(x) = getfx(d, "hypershot")? {
        s.hypershot = Some(Hypershot { amp_pct: reqf(&x, "ampPct")?, duration_s: reqf(&x, "durationS")? });
    }
    if let Some(x) = getfx(d, "openerLethality")? {
        s.opener_lethality = Some(OpenerLethality {
            duration_s: reqf(&x, "durationS")?,
            ranged: reqf(&x, "ranged")?,
            melee: reqf(&x, "melee")?,
        });
    }
    if let Some(x) = getfx(d, "firstAttackBonus")? {
        s.first_attack_bonus = Some(FirstAttackBonus {
            base: reqf(&x, "base")?,
            per_lethality: reqf(&x, "perLethality")?,
            dtype: dtype_of(&x)?,
            source: intern(&gets(&x, "source", "opener")?),
        });
    }
    s.first_attack_crit_floor_ev = getnum(d, "firstAttackCritFloorEv")?;
    if let Some(x) = getfx(d, "attackAmp")? {
        s.attack_amp = Some(AttackAmp { max_pct: reqf(&x, "maxPct")?, max_at_range: reqf(&x, "maxAtRange")? });
    }
    Ok(s)
}

impl ItemFx {
    /// One entry of item-effects.json (the value under an item id).
    pub fn from_py(d: &Bound<'_, PyDict>) -> PyResult<ItemFx> {
        let mut stacked = Vec::new();
        if let Some(st) = getd(d, "stackedStats")? {
            for (k, v) in st.iter() {
                let name: String = k.extract()?;
                let key = match name.as_str() {
                    "abilityPower" => crate::sheet::SK::ApFlat,
                    "attackDamage" => crate::sheet::SK::AdBonus,
                    "health" => crate::sheet::SK::Hp,
                    "mana" => crate::sheet::SK::Mana,
                    _ => return Err(PyKeyError::new_err(name)),
                };
                stacked.push((key, v.extract::<f64>()?));
            }
        }
        let onhit = getlist(d, "onhit")?
            .iter()
            .map(|v| parse_onhit(&dict_of(v)?))
            .collect::<PyResult<Vec<_>>>()?;
        let haste = match getd(d, "hasteFromBonusAd")? {
            Some(h) => Some((reqf(&h, "base")?, reqf(&h, "perBonusAdPct")?)),
            None => None,
        };
        Ok(ItemFx {
            ap_mult: getf(d, "apMult", 0.0)?,
            stacked,
            ap_from_bonus_hp_pct: getf(d, "apFromBonusHpPct", 0.0)?,
            ad_from_bonus_hp_pct: getf(d, "adFromBonusHpPct", 0.0)?,
            ap_from_bonus_mana_pct: getf(d, "apFromBonusManaPct", 0.0)?,
            ad_from_max_mana_pct: getf(d, "adFromMaxManaPct", 0.0)?,
            crit_chance_stacked_pct: getf(d, "critChanceStackedPct", 0.0)?,
            basic_ability_haste: getf(d, "basicAbilityHaste", 0.0)?,
            haste_from_bonus_ad: haste,
            needs_mana: truthy(d, "needsMana")?,
            onhit,
            onhit_current_hp: match getd(d, "onhitCurrentHp")? { Some(x) => Some(parse_onhit_current(&x)?), None => None },
            dmg_amp: match getd(d, "dmgAmp")? { Some(x) => Some(parse_dmg_amp(&x)?), None => None },
            flat_amp: match getd(d, "flatAmp")? { Some(x) => Some(parse_flat_amp(&x)?), None => None },
            burn: match getd(d, "burn")? { Some(x) => Some(parse_burn(&x)?), None => None },
            active_once: match getd(d, "activeOnce")? { Some(x) => Some(parse_active_once(&x)?), None => None },
            energized: match getd(d, "energized")? { Some(x) => Some(parse_energized(&x)?), None => None },
            s: parse_singletons(d)?,
        })
    }
}

macro_rules! first_wins {
    ($dst:expr, $src:expr, $($f:ident),*) => {
        $( if $dst.$f.is_none() { $dst.$f = $src.$f.clone(); } )*
    };
}

impl Fx {
    /// builds.py's merge_effects over the items of a build, in build order.
    pub fn merge<'a, I: IntoIterator<Item = &'a ItemFx>>(items: I) -> Fx {
        let mut fx = Fx::default();
        for e in items {
            fx.onhit.extend(e.onhit.iter().cloned());
            if let Some(x) = &e.onhit_current_hp { fx.onhit_current_hp.push(x.clone()); }
            if let Some(x) = &e.dmg_amp { fx.dmg_amps.push(x.clone()); }
            if let Some(x) = &e.flat_amp { fx.flat_amps.push(x.clone()); }
            if let Some(x) = &e.burn { fx.burns.push(x.clone()); }
            if let Some(x) = &e.active_once { fx.actives_once.push(x.clone()); }
            if let Some(x) = &e.energized { fx.energized.push(x.clone()); }
            first_wins!(fx.s, e.s, spellblade, as_stacking, phantom, kraken, alt_pen, magic_crit,
                        ult_burn, navori_cdr, flurry, execute_pct, giant_slayer, stormsurge,
                        ability_mana_proc, ability_proc_once, armor_shred, mr_shred,
                        ability_amp_stacking, mana_active, on_ult_cast, ult_attack_steroid,
                        hit_pair_proc, nth_hit_proc, hypershot, opener_lethality,
                        first_attack_bonus, first_attack_crit_floor_ev, attack_amp);
        }
        fx
    }

    /// The dict merge_effects returns (possibly edited by a caller: a
    /// singleton set to None switches it off).
    pub fn from_py(d: &Bound<'_, PyDict>) -> PyResult<Fx> {
        let mut fx = Fx::default();
        for v in getlist(d, "onhit")? { fx.onhit.push(parse_onhit(&dict_of(&v)?)?); }
        for v in getlist(d, "onhitCurrentHp")? { fx.onhit_current_hp.push(parse_onhit_current(&dict_of(&v)?)?); }
        for v in getlist(d, "dmgAmps")? { fx.dmg_amps.push(parse_dmg_amp(&dict_of(&v)?)?); }
        for v in getlist(d, "flatAmps")? { fx.flat_amps.push(parse_flat_amp(&dict_of(&v)?)?); }
        for v in getlist(d, "burns")? { fx.burns.push(parse_burn(&dict_of(&v)?)?); }
        for v in getlist(d, "activesOnce")? { fx.actives_once.push(parse_active_once(&dict_of(&v)?)?); }
        for v in getlist(d, "energized")? { fx.energized.push(parse_energized(&dict_of(&v)?)?); }
        fx.s = parse_singletons(d)?;
        Ok(fx)
    }
}
