//! A unit's numbers for one star level and one form, resolved by Python
//! (tft.kit_spec): the stats, every curve row at that star, and the
//! ability's calculations with the star's coefficients — plus the
//! calculation folding itself (tft.calc_value's port).
//!
//! Drivers resolve the rows and calcs they read into ids once, in
//! `Driver::new`, and read them by id in the fight.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::pyget::*;

pub const BASE_AP: f64 = 100.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DType {
    Physical,
    Magic,
    True,
}

impl DType {
    pub fn name(self) -> &'static str {
        match self {
            DType::Physical => "physical",
            DType::Magic => "magic",
            DType::True => "true",
        }
    }

    pub fn parse(s: &str) -> PyResult<DType> {
        Ok(match s {
            "physical" => DType::Physical,
            "magic" => DType::Magic,
            "true" => DType::True,
            _ => return Err(pyo3::exceptions::PyValueError::new_err(format!("damage type {s:?}"))),
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct RowId(pub u32);

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct CalcId(pub u32);

impl RowId {
    pub const MISSING: RowId = RowId(u32::MAX);
}

impl CalcId {
    pub const MISSING: CalcId = CalcId(u32::MAX);
}

#[derive(Clone, Copy, Debug)]
pub enum Scaling {
    /// The coefficient is the damage at the unit's base attack damage for
    /// the star; it scales with the attack damage it has.
    AttackDamage,
    /// Flat per 100 ability power.
    AbilityPower,
    HealthMax,
    Armor,
    MagicResist,
    /// A fraction of the current attack damage.
    BasicAttackDamage,
    /// A runtime stack count.
    Stack,
    /// Another calc of the kit.
    Calc(CalcId),
    /// No scaling: the coefficient itself.
    None,
    /// A scaling the engine does not model: reads as 0 (Python warned once).
    Unknown,
    /// A calc reference the kit cannot resolve: an error when evaluated,
    /// as Python's KeyError was.
    MissingCalc,
}

#[derive(Clone, Copy, Debug)]
pub enum Op {
    Add,
    Override,
    Multiply,
    Divide,
    Ignore,
}

#[derive(Clone, Debug)]
pub enum TermKind {
    /// A value the driver supplies at runtime, by key.
    Runtime(String),
    /// A value already resolved at this star.
    Flat(f64),
    Scaled { coef: f64, scaling: Scaling, pre_add: Option<f64> },
}

#[derive(Clone, Debug)]
pub struct Term {
    pub kind: TermKind,
    pub op: Op,
}

#[derive(Clone, Debug)]
pub struct Calc {
    pub name: String,
    pub dtype: DType,
    pub terms: Vec<Term>,
}

/// The base stats of the unit in this form (tft.Sheet's `stats`); the
/// star-scaled health and attack damage are the kit's `hp_star` and
/// `base_ad`, computed by Python so the engine never raises a float to a
/// power. Every field is kept for the drivers (Gnar reads `mana`).
#[derive(Clone, Debug, Default)]
#[allow(dead_code)]
pub struct Stats {
    pub hp: f64,
    pub ad: f64,
    pub as_: f64,
    pub armor: f64,
    pub mr: f64,
    pub mana: f64,
    pub initial_mana: f64,
    pub range: f64,
    pub crit_chance: f64,
    pub crit_mult: f64,
}

impl Stats {
    pub fn from_py(d: &Bound<'_, PyDict>) -> PyResult<Stats> {
        Ok(Stats {
            hp: getf(d, "hp", 0.0)?,
            ad: getf(d, "ad", 0.0)?,
            as_: getf(d, "as", 0.7)?,
            armor: getf(d, "armor", 0.0)?,
            mr: getf(d, "mr", 0.0)?,
            mana: getf(d, "mana", 0.0)?,
            initial_mana: getf(d, "initialMana", 0.0)?,
            range: getf(d, "range", 1.0)?,
            crit_chance: getf(d, "critChance", 0.25)?,
            crit_mult: getf(d, "critMult", 1.4)?,
        })
    }
}

/// Values a driver hands to `calc` for runtime terms and the Stack scaling.
#[derive(Clone, Copy, Debug, Default)]
pub struct Runtime<'a> {
    pub values: &'a [(&'a str, f64)],
}

impl<'a> Runtime<'a> {
    pub const NONE: Runtime<'static> = Runtime { values: &[] };

    fn get(&self, key: &str) -> Option<f64> {
        self.values.iter().find(|(k, _)| *k == key).map(|(_, v)| *v)
    }
}

#[derive(Clone, Debug)]
pub struct Kit {
    pub unit_name: String,
    pub stats: Stats,
    /// Base attack damage at this star (and form): what an AttackDamage
    /// coefficient is the damage at.
    pub base_ad: f64,
    /// Base health at this star, before items.
    pub hp_star: f64,
    rows: Vec<f64>,
    row_names: HashMap<String, u32>,
    calcs: Vec<Calc>,
    calc_names: HashMap<String, u32>,
}

impl Kit {
    pub fn from_py(d: &Bound<'_, PyDict>, unit_name: &str) -> PyResult<Kit> {
        let stats = Stats::from_py(&reqd(d, "stats")?)?;
        let base_ad = reqf(d, "baseAd")?;
        let hp_star = reqf(d, "hpStar")?;
        let mut rows = Vec::new();
        let mut row_names = HashMap::new();
        for (k, v) in reqd(d, "rows")?.iter() {
            let name: String = k.extract()?;
            row_names.insert(name, rows.len() as u32);
            rows.push(v.extract::<f64>()?);
        }
        // calcs may reference each other: name them first, then read terms
        let calcs_d = reqd(d, "calcs")?;
        let mut names: Vec<String> = Vec::new();
        let mut calc_names = HashMap::new();
        for (k, _) in calcs_d.iter() {
            let name: String = k.extract()?;
            calc_names.insert(name.clone(), names.len() as u32);
            names.push(name);
        }
        let mut calcs = Vec::new();
        for name in &names {
            let cd = reqd(&calcs_d, name)?;
            let dtype = DType::parse(&reqs(&cd, "dtype")?)?;
            let mut terms = Vec::new();
            for t in getlist(&cd, "terms")? {
                let t = dict_of(&t)?;
                let op = match gets(&t, "op", "")?.as_str() {
                    "add" => Op::Add,
                    "override" => Op::Override,
                    "multiply" => Op::Multiply,
                    "divide" => Op::Divide,
                    _ => Op::Ignore,
                };
                let kind = match gets(&t, "type", "scaled")?.as_str() {
                    "runtime" => TermKind::Runtime(gets(&t, "row", "runtime")?),
                    "flat" => TermKind::Flat(reqf(&t, "value")?),
                    _ => {
                        let scaling = match get(&t, "scaling")? {
                            None => Scaling::None,
                            Some(s) => {
                                let s: String = s.extract()?;
                                match s.as_str() {
                                    "AttackDamage" => Scaling::AttackDamage,
                                    "AbilityPower" => Scaling::AbilityPower,
                                    "HealthMax" => Scaling::HealthMax,
                                    "Armor" => Scaling::Armor,
                                    "MagicResist" => Scaling::MagicResist,
                                    "BasicAttackDamage" => Scaling::BasicAttackDamage,
                                    "Stack" => Scaling::Stack,
                                    other => {
                                        let is_calc = ["Calc1", "Calc2", "Calc3", "Calc4"]
                                            .iter().any(|suf| other.ends_with(suf));
                                        if is_calc {
                                            match calc_names.get(other) {
                                                Some(&i) => Scaling::Calc(CalcId(i)),
                                                None => Scaling::MissingCalc,
                                            }
                                        } else {
                                            Scaling::Unknown
                                        }
                                    }
                                }
                            }
                        };
                        let pre_add = match get(&t, "preAdd")? {
                            Some(v) => Some(v.extract::<f64>()?),
                            None => None,
                        };
                        TermKind::Scaled { coef: reqf(&t, "coef")?, scaling, pre_add }
                    }
                };
                terms.push(Term { kind, op });
            }
            calcs.push(Calc { name: name.clone(), dtype, terms });
        }
        Ok(Kit { unit_name: unit_name.to_string(), stats, base_ad, hp_star, rows, row_names,
                 calcs, calc_names })
    }

    /// The id of a curve row, `RowId::MISSING` when the kit has none by
    /// that name (reading it in the fight is an error, as in Python).
    pub fn row(&self, name: &str) -> RowId {
        match self.row_names.get(name) {
            Some(&i) => RowId(i),
            None => RowId::MISSING,
        }
    }

    pub fn calc(&self, name: &str) -> CalcId {
        let short = name.strip_prefix("TFTCalculationAttributes.").unwrap_or(name);
        match self.calc_names.get(short) {
            Some(&i) => CalcId(i),
            None => CalcId::MISSING,
        }
    }

    #[inline]
    pub fn row_value(&self, id: RowId) -> f64 {
        match self.rows.get(id.0 as usize) {
            Some(v) => *v,
            None => panic!("{}: a driver read a curve row the kit does not have", self.unit_name),
        }
    }

    pub fn calc_dtype(&self, id: CalcId) -> DType {
        self.calc_ref(id).dtype
    }

    fn calc_ref(&self, id: CalcId) -> &Calc {
        match self.calcs.get(id.0 as usize) {
            Some(c) => c,
            None => panic!("{}: a driver read a calc the kit does not have", self.unit_name),
        }
    }

    /// tft.calc_value: fold a calculation at the given stats.
    pub fn calc_value(&self, id: CalcId, ad: f64, ap: f64, max_hp: f64, armor: f64, mr: f64,
                      base_ad: f64, runtime: Runtime<'_>) -> f64 {
        let calc = self.calc_ref(id);
        let mut acc = 0.0;
        for term in &calc.terms {
            let v = match &term.kind {
                TermKind::Runtime(key) => match runtime.get(key) {
                    Some(v) => v,
                    None => runtime.get("runtime").unwrap_or(0.0),
                },
                TermKind::Flat(v) => *v,
                TermKind::Scaled { coef, scaling, pre_add } => {
                    let mut x = match scaling {
                        Scaling::AttackDamage => if base_ad != 0.0 { ad / base_ad } else { 1.0 },
                        Scaling::AbilityPower => ap / 100.0,
                        Scaling::HealthMax => max_hp,
                        Scaling::Armor => armor,
                        Scaling::MagicResist => mr,
                        Scaling::BasicAttackDamage => ad,
                        Scaling::Stack => runtime.get("Stack").unwrap_or(0.0),
                        Scaling::Calc(other) => self.calc_value(*other, ad, ap, max_hp, armor, mr,
                                                                base_ad, runtime),
                        Scaling::None => 1.0,
                        Scaling::Unknown => 0.0,
                        Scaling::MissingCalc => panic!("{}: {} scales with a calc the kit does not have",
                                                       self.unit_name, calc.name),
                    };
                    if let Some(p) = pre_add {
                        x += p;
                    }
                    coef * x
                }
            };
            match term.op {
                Op::Add => acc += v,
                Op::Override => acc = v,
                Op::Multiply => acc *= v,
                Op::Divide => acc = if v != 0.0 { acc / v } else { 0.0 },
                Op::Ignore => {}
            }
        }
        acc
    }
}
