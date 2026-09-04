//! The stat sheet: champion base stats at a level plus a build's items, by
//! the in-game rules — builds.py's resolve_stats, operation for operation.

use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::fx::ItemFx;
use crate::kit::Pact;
use crate::num::{growth, pymin, stack_pct_pen, stat_at, AS_CAP};
use crate::pyget::*;

/// The sheet keys an item stat lands in (ITEM_STAT_MAP's values).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(usize)]
pub enum SK {
    Haste = 0,
    ApFlat,
    Armor,
    ArmorPenPct,
    AdBonus,
    BonusAsPct,
    CritChance,
    CritDamageBonus,
    HealShieldPower,
    Hp,
    Lethality,
    Lifesteal,
    MagicPenFlat,
    MagicPenPct,
    Mr,
    Mana,
    MsFlat,
    MsPct,
    Omnivamp,
    Tenacity,
}

pub const SK_COUNT: usize = 20;

impl SK {
    pub fn parse(name: &str) -> Option<SK> {
        Some(match name {
            "haste" => SK::Haste,
            "ap_flat" => SK::ApFlat,
            "armor" => SK::Armor,
            "armor_pen_pct" => SK::ArmorPenPct,
            "ad_bonus" => SK::AdBonus,
            "bonus_as_pct" => SK::BonusAsPct,
            "crit_chance" => SK::CritChance,
            "crit_damage_bonus" => SK::CritDamageBonus,
            "heal_shield_power" => SK::HealShieldPower,
            "hp" => SK::Hp,
            "lethality" => SK::Lethality,
            "lifesteal" => SK::Lifesteal,
            "magic_pen_flat" => SK::MagicPenFlat,
            "magic_pen_pct" => SK::MagicPenPct,
            "mr" => SK::Mr,
            "mana" => SK::Mana,
            "ms_flat" => SK::MsFlat,
            "ms_pct" => SK::MsPct,
            "omnivamp" => SK::Omnivamp,
            "tenacity" => SK::Tenacity,
            _ => return None,
        })
    }
}

/// Champion base stats (ddragon, with meraki's AS ratio, crit damage and AD
/// growth fallback already applied by the Python side).
#[derive(Clone, Copy, Debug)]
pub struct ChampBase {
    pub hp: f64,
    pub hp_per: f64,
    pub mp: f64,
    pub mp_per: f64,
    pub armor: f64,
    pub armor_per: f64,
    pub mr: f64,
    pub mr_per: f64,
    pub ad: f64,
    pub ad_per: f64,
    pub base_as: f64,
    pub as_per: f64,
    pub as_ratio: f64,
    pub crit_damage_base: f64,
    pub move_speed: f64,
    pub attack_range: f64,
}

impl ChampBase {
    pub fn from_py(d: &Bound<'_, PyDict>) -> PyResult<ChampBase> {
        Ok(ChampBase {
            hp: reqf(d, "hp")?,
            hp_per: reqf(d, "hp_per")?,
            mp: reqf(d, "mp")?,
            mp_per: reqf(d, "mp_per")?,
            armor: reqf(d, "armor")?,
            armor_per: reqf(d, "armor_per")?,
            mr: reqf(d, "mr")?,
            mr_per: reqf(d, "mr_per")?,
            ad: reqf(d, "ad")?,
            ad_per: reqf(d, "ad_per")?,
            base_as: reqf(d, "base_as")?,
            as_per: reqf(d, "as_per")?,
            as_ratio: reqf(d, "as_ratio")?,
            crit_damage_base: reqf(d, "crit_damage_base")?,
            move_speed: reqf(d, "move_speed")?,
            attack_range: reqf(d, "attack_range")?,
        })
    }
}

/// An item's mapped, nonzero stats in the order resolve_stats folds them.
pub fn parse_stat_pairs(v: &Bound<'_, PyAny>) -> PyResult<Vec<(SK, f64)>> {
    let mut out = Vec::new();
    for pair in v.try_iter()? {
        let pair = pair?;
        let name: String = pair.get_item(0)?.extract()?;
        let val: f64 = pair.get_item(1)?.extract()?;
        let key = SK::parse(&name).ok_or_else(|| PyKeyError::new_err(name))?;
        out.push((key, val));
    }
    Ok(out)
}

#[derive(Clone, Debug)]
pub struct Sheet {
    pub ad_base: f64,
    pub ad_bonus: f64,
    pub ad: f64,
    pub ap: f64,
    pub ap_flat: f64,
    pub ap_mult: f64,
    pub attack_speed: f64,
    pub base_as: f64,
    pub as_ratio: f64,
    pub bonus_as_pct: f64,
    pub crit_chance: f64,
    pub crit_damage: f64,
    pub haste: f64,
    pub cd_mult: f64,
    pub basic_cd_mult: f64,
    pub hp: f64,
    pub hp_bonus: f64,
    pub mana: f64,
    pub mana_bonus: f64,
    pub armor: f64,
    pub mr: f64,
    pub lethality: f64,
    pub armor_pen_pct: f64,
    pub magic_pen_flat: f64,
    pub magic_pen_pct: f64,
    pub lifesteal: f64,
    pub omnivamp: f64,
    pub tenacity: f64,
    pub heal_shield_power: f64,
    pub move_speed: f64,
    pub base_attack_range: f64,
}

impl Sheet {
    /// The numbers of a sheet dict resolve_stats returned (the engine's
    /// inputs; display-only keys are ignored).
    pub fn from_py(d: &Bound<'_, PyDict>) -> PyResult<Sheet> {
        Ok(Sheet {
            ad_base: reqf(d, "ad_base")?,
            ad_bonus: reqf(d, "ad_bonus")?,
            ad: reqf(d, "ad")?,
            ap: reqf(d, "ap")?,
            ap_flat: getf(d, "ap_flat", 0.0)?,
            ap_mult: getf(d, "ap_mult", 1.0)?,
            attack_speed: getf(d, "attack_speed", 0.0)?,
            base_as: reqf(d, "base_as")?,
            as_ratio: reqf(d, "as_ratio")?,
            bonus_as_pct: reqf(d, "bonus_as_pct")?,
            crit_chance: reqf(d, "crit_chance")?,
            crit_damage: reqf(d, "crit_damage")?,
            haste: getf(d, "haste", 0.0)?,
            cd_mult: getf(d, "cd_mult", 1.0)?,
            basic_cd_mult: reqf(d, "basic_cd_mult")?,
            hp: reqf(d, "hp")?,
            hp_bonus: reqf(d, "hp_bonus")?,
            mana: reqf(d, "mana")?,
            mana_bonus: reqf(d, "mana_bonus")?,
            armor: getf(d, "armor", 0.0)?,
            mr: getf(d, "mr", 0.0)?,
            lethality: reqf(d, "lethality")?,
            armor_pen_pct: reqf(d, "armor_pen_pct")?,
            magic_pen_flat: reqf(d, "magic_pen_flat")?,
            magic_pen_pct: reqf(d, "magic_pen_pct")?,
            lifesteal: getf(d, "lifesteal", 0.0)?,
            omnivamp: getf(d, "omnivamp", 0.0)?,
            tenacity: getf(d, "tenacity", 0.0)?,
            heal_shield_power: getf(d, "heal_shield_power", 0.0)?,
            move_speed: reqf(d, "move_speed")?,
            base_attack_range: reqf(d, "base_attack_range")?,
        })
    }

    pub fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        d.set_item("ad_base", self.ad_base)?;
        d.set_item("ad_bonus", self.ad_bonus)?;
        d.set_item("ap", self.ap)?;
        d.set_item("ap_flat", self.ap_flat)?;
        d.set_item("ap_mult", self.ap_mult)?;
        d.set_item("attack_speed", self.attack_speed)?;
        d.set_item("base_as", self.base_as)?;
        d.set_item("as_ratio", self.as_ratio)?;
        d.set_item("bonus_as_pct", self.bonus_as_pct)?;
        d.set_item("crit_chance", self.crit_chance)?;
        d.set_item("crit_damage", self.crit_damage)?;
        d.set_item("haste", self.haste)?;
        d.set_item("cd_mult", self.cd_mult)?;
        d.set_item("basic_cd_mult", self.basic_cd_mult)?;
        d.set_item("hp", self.hp)?;
        d.set_item("hp_bonus", self.hp_bonus)?;
        d.set_item("mana", self.mana)?;
        d.set_item("mana_bonus", self.mana_bonus)?;
        d.set_item("armor", self.armor)?;
        d.set_item("mr", self.mr)?;
        d.set_item("lethality", self.lethality)?;
        d.set_item("armor_pen_pct", self.armor_pen_pct)?;
        d.set_item("magic_pen_flat", self.magic_pen_flat)?;
        d.set_item("magic_pen_pct", self.magic_pen_pct)?;
        d.set_item("lifesteal", self.lifesteal)?;
        d.set_item("omnivamp", self.omnivamp)?;
        d.set_item("tenacity", self.tenacity)?;
        d.set_item("heal_shield_power", self.heal_shield_power)?;
        d.set_item("move_speed", self.move_speed)?;
        d.set_item("base_attack_range", self.base_attack_range)?;
        d.set_item("ad", self.ad)?;
        Ok(d)
    }
}

/// resolve_stats: `items` in build order, each its stat pairs and overlay
/// entry; `pact` is Vladimir's Crimson Pact when the kit has one.
pub fn resolve(base: &ChampBase, level: i64, items: &[(&[(SK, f64)], &ItemFx)],
               pact: Option<Pact>) -> Sheet {
    let mut agg = [0.0f64; SK_COUNT];
    let mut ap_mult = 1.0f64;
    let mut pen_armor = 0.0f64;
    let mut pen_magic = 0.0f64;
    for (stats, fx) in items {
        for &(k, v) in stats.iter() {
            match k {
                SK::ArmorPenPct => pen_armor = stack_pct_pen(pen_armor, v),
                SK::MagicPenPct => pen_magic = stack_pct_pen(pen_magic, v),
                _ => agg[k as usize] += v,
            }
        }
        // AP increases (Rabadon, Blackfire) compound multiplicatively
        ap_mult *= 1.0 + fx.ap_mult;
        // permanent-stack items (Rod of Ages) are assumed fully stacked
        for &(k, v) in &fx.stacked {
            agg[k as usize] += v;
        }
    }
    agg[SK::ArmorPenPct as usize] = pen_armor;
    agg[SK::MagicPenPct as usize] = pen_magic;
    // Stat-granting passives that need the item totals; all land before the
    // AP multiplier, matching the in-game order.
    let base_mana = stat_at(base.mp, base.mp_per, level);
    let mut basic_haste = 0.0f64;
    for (_, fx) in items {
        agg[SK::ApFlat as usize] += fx.ap_from_bonus_hp_pct / 100.0 * agg[SK::Hp as usize];
        agg[SK::AdBonus as usize] += fx.ad_from_bonus_hp_pct / 100.0 * agg[SK::Hp as usize];
        agg[SK::ApFlat as usize] += fx.ap_from_bonus_mana_pct / 100.0 * agg[SK::Mana as usize];
        agg[SK::AdBonus as usize] += fx.ad_from_max_mana_pct / 100.0
            * (base_mana + agg[SK::Mana as usize]);
        agg[SK::CritChance as usize] += fx.crit_chance_stacked_pct;
        basic_haste += fx.basic_ability_haste;
    }
    for (_, fx) in items {
        // Famine (Endless Hunger): haste from total bonus AD
        if let Some((b, per)) = fx.haste_from_bonus_ad {
            agg[SK::Haste as usize] += b + per / 100.0 * agg[SK::AdBonus as usize];
        }
    }

    let bonus_as = base.as_per * growth(level) + agg[SK::BonusAsPct as usize];
    let attack_speed = pymin(base.base_as + base.as_ratio * bonus_as / 100.0, AS_CAP);

    // Vladimir's Crimson Pact, closed form of the Riftmaker feedback
    let mut pact_hp = 0.0f64;
    let ap;
    if let Some(p) = pact {
        let per_hp = p.ap_per_30_bonus_hp / 30.0;
        let per_ap = p.bonus_hp_per_ap;
        // Python sums these with sum(); only one item (Riftmaker) carries the
        // stat, so a plain fold is the same bits.
        let mut rift = 0.0f64;
        for (_, fx) in items {
            rift += fx.ap_from_bonus_hp_pct;
        }
        let rift = rift / 100.0;
        let ap_pact = per_hp * agg[SK::Hp as usize];
        ap = ap_mult * (agg[SK::ApFlat as usize] + ap_pact * (1.0 - rift * per_ap))
            / (1.0 - ap_mult * rift * per_ap);
        agg[SK::ApFlat as usize] = ap / ap_mult;
        pact_hp = per_ap * (ap - ap_pact);
        for (_, fx) in items {
            // Overlord's: bonus AD counts that health too
            agg[SK::AdBonus as usize] += fx.ad_from_bonus_hp_pct / 100.0 * pact_hp;
        }
    } else {
        ap = agg[SK::ApFlat as usize] * ap_mult;
    }
    let haste = agg[SK::Haste as usize];
    let ad_base = stat_at(base.ad, base.ad_per, level);
    let ad_bonus = agg[SK::AdBonus as usize];
    Sheet {
        ad_base,
        ad_bonus,
        ad: ad_base + ad_bonus,
        ap,
        ap_flat: agg[SK::ApFlat as usize],
        ap_mult,
        attack_speed,
        base_as: base.base_as,
        as_ratio: base.as_ratio,
        bonus_as_pct: bonus_as,
        crit_chance: pymin(agg[SK::CritChance as usize], 100.0),
        crit_damage: base.crit_damage_base + agg[SK::CritDamageBonus as usize],
        haste,
        cd_mult: 100.0 / (100.0 + haste),
        basic_cd_mult: 100.0 / (100.0 + haste + basic_haste),
        hp: stat_at(base.hp, base.hp_per, level) + agg[SK::Hp as usize] + pact_hp,
        hp_bonus: agg[SK::Hp as usize] + pact_hp,
        mana: stat_at(base.mp, base.mp_per, level) + agg[SK::Mana as usize],
        mana_bonus: agg[SK::Mana as usize],
        armor: stat_at(base.armor, base.armor_per, level) + agg[SK::Armor as usize],
        mr: stat_at(base.mr, base.mr_per, level) + agg[SK::Mr as usize],
        lethality: agg[SK::Lethality as usize],
        armor_pen_pct: agg[SK::ArmorPenPct as usize],
        magic_pen_flat: agg[SK::MagicPenFlat as usize],
        magic_pen_pct: agg[SK::MagicPenPct as usize],
        lifesteal: agg[SK::Lifesteal as usize],
        omnivamp: agg[SK::Omnivamp as usize],
        tenacity: agg[SK::Tenacity as usize],
        heal_shield_power: agg[SK::HealShieldPower as usize],
        move_speed: (base.move_speed + agg[SK::MsFlat as usize])
            * (1.0 + agg[SK::MsPct as usize] / 100.0),
        base_attack_range: base.attack_range,
    }
}
