//! The cell spec: everything one unit's fights need, resolved by Python
//! (tft.cell_spec) into plain numbers — the unit and its kits per form,
//! the dummies, the role's and the traits' contributions, the item pool
//! (or, for a single fight, the build's items).

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict};

use crate::fx::{Form, ItemFx, RoleFx, TraitFx};
use crate::kit::{Kit, RowId, Stats};
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
    pub fn name(self) -> &'static str {
        match self {
            Self::Assassin => "Assassin", Self::Fighter => "Fighter", Self::Marksman => "Marksman",
            Self::Caster => "Caster", Self::Tank => "Tank", Self::Specialist => "Specialist",
        }
    }

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
    pub fn name(self) -> &'static str {
        match self { Self::Carry => "carry", Self::Fighter => "fighter", Self::Tank => "tank" }
    }

    pub fn parse(s: &str) -> PyResult<Objective> {
        Ok(match s {
            "carry" => Objective::Carry,
            "fighter" => Objective::Fighter,
            "tank" => Objective::Tank,
            _ => return Err(PyValueError::new_err(format!("objective {s:?}"))),
        })
    }
}

/// The cast window of one form (data/tft/set<N>/cast-timing.json, resolved
/// by tft.cast_timing_spec): TFTraits' figures, a third-party source adopted
/// as a model rule and not verified in game. Seconds; they never scale with
/// attack speed.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CastWindow {
    /// Cast animation plus any channel after it: no attacks and no mana.
    pub busy: f64,
    /// When the ability takes effect, from the start of the cast; missing
    /// keeps the character bin's cast time.
    pub effect_at: Option<f64>,
    /// A mana lock the source states as a fixed time from the start of the
    /// cast (Ahri's runs past her animation); zero when it has none.
    pub mana_lock: f64,
}

/// One form's attack and cast timeline. The attack figures are seconds at
/// base attack speed and scale with the attack animation.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CastTiming {
    /// From the start of an attack until it lands.
    pub attack_delay: f64,
    /// After the landing until the unit can cast or attack again.
    pub attack_recovery: f64,
    /// Missing: no cast animation on record, a cast keeps the flat rule.
    pub cast: Option<CastWindow>,
}

impl CastTiming {
    fn from_py(d: &Bound<'_, PyDict>) -> PyResult<CastTiming> {
        let seconds = |key: &str| -> PyResult<Option<f64>> {
            match get(d, key)? {
                Some(v) if !v.is_instance_of::<PyBool>() => {
                    let value: f64 = v.extract()?;
                    if !value.is_finite() || value < 0.0 {
                        return Err(PyValueError::new_err(format!(
                            "timing.{key}: expected a finite, nonnegative number of seconds")));
                    }
                    Ok(Some(value))
                }
                Some(_) => Err(PyValueError::new_err(format!(
                    "timing.{key}: expected a finite, nonnegative number of seconds"))),
                None => Ok(None),
            }
        };
        let cast = match seconds("castAnimation")? {
            Some(animation) => Some(CastWindow {
                busy: animation + seconds("channel")?.unwrap_or(0.0),
                effect_at: seconds("effectAt")?,
                mana_lock: seconds("manaLock")?.unwrap_or(0.0),
            }),
            None => None,
        };
        Ok(CastTiming {
            attack_delay: seconds("attackDelay")?.unwrap_or(0.0),
            attack_recovery: seconds("attackRecovery")?.unwrap_or(0.0),
            cast,
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
    /// Per-form timelines; a unit without an entry keeps the flat rules
    /// (the bin's cast time, a one-second lock, attacks landing when due).
    pub timing: Option<CastTiming>,
    pub timing_ad: Option<CastTiming>,
    pub timing_ap: Option<CastTiming>,
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
        let timing = getd(d, "timing")?;
        let timing_of = |form: &str| -> PyResult<Option<CastTiming>> {
            match &timing {
                Some(forms) => match getd(forms, form)? {
                    Some(entry) => Ok(Some(CastTiming::from_py(&entry)?)),
                    None => Ok(None),
                },
                None => Ok(None),
            }
        };
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
            timing: timing_of("base")?,
            timing_ad: timing_of("AD")?,
            timing_ap: timing_of("AP")?,
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
    pub(crate) fn from_py(d: &Bound<'_, PyDict>) -> PyResult<DummySpec> {
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

/// Permanent team-applied resistance reductions on every enemy target.
/// Separate from EnemyDebuffs, which reduces the simulated unit's defenses.
#[derive(Clone, Copy, Debug, Default)]
pub struct TargetDebuffs {
    pub sunder: f64,
    pub shred: f64,
}

impl TargetDebuffs {
    fn from_py(d: &Bound<'_, PyDict>) -> PyResult<TargetDebuffs> {
        let fraction = |key: &str| -> PyResult<f64> {
            let value = getf(d, key, 0.0)?;
            if !value.is_finite() || !(0.0..=1.0).contains(&value) {
                return Err(PyValueError::new_err(format!(
                    "targetDebuffs.{key}: expected a finite fraction from 0 to 1")));
            }
            Ok(value)
        };
        Ok(TargetDebuffs {
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
    /// Explicit finite-benchmark approximation, in seconds per engagement.
    /// Missing/zero retains stationary fixtures and immortal theory probes.
    pub melee_reposition_seconds: f64,
    pub pressure: bool,
    /// Infer standalone pressure from the equipped form only when the
    /// caller did not explicitly select a pressure condition.
    pub auto_pressure: bool,
    pub enemy_debuffs: EnemyDebuffs,
    pub target_debuffs: TargetDebuffs,
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
        let melee_reposition_seconds = match get(d, "meleeRepositionSeconds")? {
            Some(value) if !value.is_instance_of::<PyBool>() => value.extract::<f64>()?,
            Some(_) => return Err(PyValueError::new_err("meleeRepositionSeconds must be a finite nonnegative number")),
            None => 0.0,
        };
        if !melee_reposition_seconds.is_finite() || melee_reposition_seconds < 0.0 {
            return Err(PyValueError::new_err("meleeRepositionSeconds must be a finite nonnegative number"));
        }
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
            melee_reposition_seconds,
            pressure: truthy(d, "pressure")?,
            auto_pressure: truthy(d, "autoPressure")?,
            enemy_debuffs: match getd(d, "enemyDebuffs")? {
                Some(debuffs) => EnemyDebuffs::from_py(&debuffs)?,
                None => EnemyDebuffs::default(),
            },
            target_debuffs: match getd(d, "targetDebuffs")? {
                Some(debuffs) => TargetDebuffs::from_py(&debuffs)?,
                None => TargetDebuffs::default(),
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

    pub fn kind_for(&self, form: Option<Form>) -> Kind {
        if self.unit.api == "TFT18_Nidalee" {
            match form { Some(Form::AD) => Kind::Assassin, Some(Form::AP) => Kind::Marksman,
                         None => self.unit.kind }
        } else { self.unit.kind }
    }

    pub fn objective_for(&self, form: Option<Form>) -> Objective {
        if self.unit.api == "TFT18_Nidalee" {
            match form { Some(Form::AD) => Objective::Fighter, Some(Form::AP) => Objective::Carry,
                         None => self.unit.objective }
        } else { self.unit.objective }
    }

    pub fn range_for(&self, form: Option<Form>) -> f64 {
        let kit = self.kit_for(form);
        let range = if self.unit.has_forms && form.is_some() { kit.stats.range } else { self.unit.range };
        if self.unit.api == "TFT18_Nidalee" && form == Some(Form::AP) {
            // The pinned AP kit names this intrinsic bonus explicitly;
            // the melee AD form must not inherit it from merged rows.
            let bonus = kit.row("AdditionalAttackRange");
            range + if bonus == RowId::MISSING { 0.0 } else { kit.row_value(bonus) }
        } else { range }
    }

    /// The equipped form's timeline, the base one when the form has none.
    pub fn timing_for(&self, form: Option<Form>) -> Option<CastTiming> {
        match form {
            Some(Form::AD) => self.unit.timing_ad.or(self.unit.timing),
            Some(Form::AP) => self.unit.timing_ap.or(self.unit.timing),
            None => self.unit.timing,
        }
    }

    pub fn pressure_for(&self, form: Option<Form>) -> bool {
        if self.auto_pressure && self.objective_for(form) != self.unit.objective {
            self.objective_for(form) != Objective::Carry
        }
        else { self.pressure }
    }
}
