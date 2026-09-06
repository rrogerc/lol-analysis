//! The cell spec: everything one unit's fights need, resolved by Python
//! (tft.cell_spec) into plain numbers — the unit and its kits per form,
//! the dummies, the role's and the traits' contributions, the item pool
//! (or, for a single fight, the build's items).

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::fx::{Form, ItemFx, RoleFx, TraitFx};
use crate::kit::{Kit, Stats};
use crate::pyget::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Assassin,
    Fighter,
    Marksman,
    Caster,
    Tank,
    Specialist,
}

impl Kind {
    pub fn parse(s: &str) -> PyResult<Kind> {
        Ok(match s {
            "Assassin" => Kind::Assassin,
            "Fighter" => Kind::Fighter,
            "Marksman" => Kind::Marksman,
            "Caster" => Kind::Caster,
            "Tank" => Kind::Tank,
            "Specialist" => Kind::Specialist,
            _ => return Err(PyValueError::new_err(format!("role kind {s:?}"))),
        })
    }

    /// tft.ROLE_MANA: mana per attack by role.
    pub fn mana_per_attack(self) -> f64 {
        match self {
            Kind::Assassin | Kind::Fighter | Kind::Marksman => 10.0,
            Kind::Caster => 7.0,
            Kind::Tank => 5.0,
            Kind::Specialist => 0.0,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Objective {
    Carry,
    Fighter,
    Tank,
}

impl Objective {
    pub fn parse(s: &str) -> PyResult<Objective> {
        Ok(match s {
            "carry" => Objective::Carry,
            "fighter" => Objective::Fighter,
            "tank" => Objective::Tank,
            _ => return Err(PyValueError::new_err(format!("objective {s:?}"))),
        })
    }
}

#[derive(Clone, Debug)]
#[allow(dead_code)]   // api and range name the unit in the spec; the gates that read them run in Python
pub struct UnitSpec {
    pub api: String,
    pub name: String,
    pub kind: Kind,
    pub attack: bool,
    pub objective: Objective,
    pub range: f64,
    pub cast_time: Option<f64>,
    pub has_forms: bool,
    /// Stats of the set's non-shop units (summons, transformed forms) a
    /// driver may read: Yorick's spirit, Krug's kruglette.
    pub extras: HashMap<String, Stats>,
}

impl UnitSpec {
    pub fn from_py(d: &Bound<'_, PyDict>) -> PyResult<UnitSpec> {
        let mut extras = HashMap::new();
        if let Some(ex) = getd(d, "extras")? {
            for (k, v) in ex.iter() {
                extras.insert(k.extract::<String>()?, Stats::from_py(&dict_of(&v)?)?);
            }
        }
        Ok(UnitSpec {
            api: reqs(d, "api")?,
            name: reqs(d, "name")?,
            kind: Kind::parse(&reqs(d, "kind")?)?,
            attack: truthy(d, "attack")?,
            objective: Objective::parse(&reqs(d, "objective")?)?,
            range: getf(d, "range", 1.0)?,
            cast_time: match get(d, "castTime")? {
                Some(v) => Some(v.extract()?),
                None => None,
            },
            has_forms: truthy(d, "hasForms")?,
            extras,
        })
    }

    pub fn extra_stats(&self, api: &str) -> &Stats {
        match self.extras.get(api) {
            Some(s) => s,
            None => panic!("{}: the spec carries no stats for {api}", self.name),
        }
    }
}

/// One dummy slot (tft.dummies_for's `slots` entry, armed or not).
#[derive(Clone, Debug)]
pub struct DummySpec {
    pub hp: f64,
    pub armor: f64,
    pub mr: f64,
    pub is_tank: bool,
    /// Whether local area effects can reach this slot; legacy slots are nearby.
    pub nearby: bool,
    pub ad: f64,
    pub as_: f64,
    pub ability: f64,
    pub phys_share: f64,
    pub mana_max: f64,
    pub mana_start: f64,
    pub mana_per_attack: f64,
    pub mana_from_damage: bool,
    /// Optional first attack time; missing keeps the legacy staggered start.
    pub attack_start: Option<f64>,
    /// A positive interval replaces mana-driven casts with a fixed schedule.
    pub cast_interval: f64,
    pub cast_start: Option<f64>,
    /// How many enemy units this slot stands for (a tank fight's board).
    pub streams: i64,
}

impl DummySpec {
    fn from_py(d: &Bound<'_, PyDict>) -> PyResult<DummySpec> {
        let timer = |key: &str| -> PyResult<Option<f64>> {
            match get(d, key)? {
                Some(v) => {
                    let value: f64 = v.extract()?;
                    if !value.is_finite() || value < 0.0 {
                        return Err(PyValueError::new_err(format!(
                            "{key}: expected a finite, nonnegative number")));
                    }
                    Ok(Some(value))
                }
                None => Ok(None),
            }
        };
        Ok(DummySpec {
            hp: reqf(d, "hp")?,
            armor: reqf(d, "armor")?,
            mr: reqf(d, "mr")?,
            is_tank: gets(d, "kind", "tank")? == "tank",
            nearby: match get(d, "nearby")? {
                Some(v) => v.extract()?,
                None => true,
            },
            ad: getf(d, "ad", 0.0)?,
            as_: getf(d, "as", 0.0)?,
            ability: getf(d, "ability", 0.0)?,
            phys_share: getf(d, "physicalShare", 1.0)?,
            mana_max: getf(d, "manaMax", 0.0)?,
            mana_start: getf(d, "manaStart", 0.0)?,
            mana_per_attack: getf(d, "manaPerAttack", 0.0)?,
            mana_from_damage: truthy(d, "manaFromDamage")?,
            attack_start: timer("attackStart")?,
            cast_interval: timer("castInterval")?.unwrap_or(0.0),
            cast_start: timer("castStart")?,
            streams: geti(d, "streams", 1)?,
        })
    }
}

/// Constant enemy debuffs for the pressured benchmark, expressed as fractions.
#[derive(Clone, Copy, Debug, Default)]
pub struct EnemyDebuffs {
    pub wound: f64,
    pub sunder: f64,
    pub shred: f64,
}

impl EnemyDebuffs {
    fn from_py(d: &Bound<'_, PyDict>) -> PyResult<EnemyDebuffs> {
        let fraction = |key: &str| -> PyResult<f64> {
            let value = getf(d, key, 0.0)?;
            if !value.is_finite() || !(0.0..=1.0).contains(&value) {
                return Err(PyValueError::new_err(format!(
                    "enemyDebuffs.{key}: expected a finite fraction from 0 to 1")));
            }
            Ok(value)
        };
        Ok(EnemyDebuffs {
            wound: fraction("wound")?,
            sunder: fraction("sunder")?,
            shred: fraction("shred")?,
        })
    }
}

#[derive(Clone, Debug)]
pub struct CellSpec {
    pub unit: UnitSpec,
    pub star: i64,
    pub kit_base: Kit,
    pub kit_ad: Option<Kit>,
    pub kit_ap: Option<Kit>,
    pub clump: bool,
    pub duration: f64,
    pub pressure: bool,
    pub enemy_debuffs: EnemyDebuffs,
    pub immortal: bool,
    pub dummies: Vec<DummySpec>,
    pub crit_ev: f64,
    pub role: RoleFx,
    pub traits: Vec<TraitFx>,
    pub pool: Vec<ItemFx>,
    pub items: Vec<ItemFx>,
    pub driver: String,
}

impl CellSpec {
    pub fn from_py(d: &Bound<'_, PyDict>) -> PyResult<CellSpec> {
        let unit = UnitSpec::from_py(&reqd(d, "unit")?)?;
        let kits = reqd(d, "kits")?;
        let kit_base = Kit::from_py(&reqd(&kits, "base")?, &unit.name)?;
        let kit_ad = match getd(&kits, "AD")? {
            Some(k) => Some(Kit::from_py(&k, &unit.name)?),
            None => None,
        };
        let kit_ap = match getd(&kits, "AP")? {
            Some(k) => Some(Kit::from_py(&k, &unit.name)?),
            None => None,
        };
        let dd = reqd(d, "dummies")?;
        let mut dummies = Vec::new();
        for s in getlist(&dd, "slots")? {
            dummies.push(DummySpec::from_py(&dict_of(&s)?)?);
        }
        if dummies.is_empty() || dummies.len() > crate::fight::MAX_TARGETS {
            return Err(PyValueError::new_err(format!("{} dummies", dummies.len())));
        }
        let role_d = reqd(d, "role")?;
        let role = RoleFx { mana_regen: getf(&role_d, "manaRegen", 0.0)?,
                            as_pct: getf(&role_d, "asPct", 0.0)? };
        let mut traits = Vec::new();
        for t in getlist(d, "traits")? {
            traits.push(TraitFx::from_py(&dict_of(&t)?)?);
        }
        let mut pool = Vec::new();
        for it in getlist(d, "pool")? {
            pool.push(ItemFx::from_py(&dict_of(&it)?)?);
        }
        let mut items = Vec::new();
        for it in getlist(d, "items")? {
            items.push(ItemFx::from_py(&dict_of(&it)?)?);
        }
        Ok(CellSpec {
            star: geti(d, "star", 1)?,
            kit_base,
            kit_ad,
            kit_ap,
            clump: gets(d, "geometry", "clump")? == "clump",
            duration: reqf(d, "duration")?,
            pressure: truthy(d, "pressure")?,
            enemy_debuffs: match getd(d, "enemyDebuffs")? {
                Some(debuffs) => EnemyDebuffs::from_py(&debuffs)?,
                None => EnemyDebuffs::default(),
            },
            immortal: truthy(d, "immortal")?,
            dummies,
            crit_ev: getf(&dd, "critEv", 1.1)?,
            role,
            traits,
            pool,
            items,
            driver: gets(d, "driver", "Plain")?,
            unit,
        })
    }

    /// tft.Sheet: the form's kit when the file carries that form, the
    /// base kit otherwise.
    pub fn kit_for(&self, form: Option<Form>) -> &Kit {
        match form {
            Some(Form::AD) => self.kit_ad.as_ref().unwrap_or(&self.kit_base),
            Some(Form::AP) => self.kit_ap.as_ref().unwrap_or(&self.kit_base),
            None => &self.kit_base,
        }
    }
}
