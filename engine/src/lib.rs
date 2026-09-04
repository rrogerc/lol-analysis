//! The builds engine: stat sheets, the combat simulation and the
//! enumeration's inner loop, compiled. builds.py keeps the data loading,
//! the parent process of the enumeration, the cache and the CLI, and calls
//! in here through `simulate`, `resolve_sheet`, `geo_mean` and `Ctx`.
//!
//! `SOURCE_HASH` is a sha256 over these sources (build.rs), part of every
//! cache key: a result is only valid for the code that produced it.

mod drivers;
mod enumerate;
mod fight;
mod fsum;
mod fx;
mod kit;
mod num;
mod pyget;
mod sheet;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict};

use crate::fight::{Opts, Target};
use crate::num::Ranks;
use crate::pyget::*;

fn ranks_of(d: &Bound<'_, PyDict>) -> PyResult<Ranks> {
    Ok(Ranks { q: reqi(d, "Q")?, w: reqi(d, "W")?, e: reqi(d, "E")?, r: reqi(d, "R")? })
}

/// One fight vs a stat dummy — see builds.simulate for the contract.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (sheet, kit, fx, level, ranks, target_hp, target_armor, target_mr, duration,
                    use_ult=true, prestacked=false, target_bonus_hp=0.0, stop_after=f64::INFINITY,
                    breakdown=true, blend=true))]
fn simulate<'py>(py: Python<'py>, sheet: &Bound<'py, PyDict>, kit: &Bound<'py, PyDict>,
                 fx: &Bound<'py, PyDict>, level: i64, ranks: &Bound<'py, PyDict>, target_hp: f64,
                 target_armor: f64, target_mr: f64, duration: f64, use_ult: bool, prestacked: bool,
                 target_bonus_hp: f64, stop_after: f64, breakdown: bool, blend: bool)
    -> PyResult<Bound<'py, PyAny>> {
    let sheet = sheet::Sheet::from_py(sheet)?;
    let kit = kit::Kit::from_py(kit)?;
    let fx = fx::Fx::from_py(fx)?;
    let ranks = ranks_of(ranks)?;
    let target = Target { hp: target_hp, armor: target_armor, mr: target_mr, duration,
                          bonus_hp: target_bonus_hp };
    let r = fight::simulate(&sheet, &kit, &fx, level, ranks, &target,
                            Opts { use_ult, prestacked, stop_after, breakdown, blend })
        .map_err(PyValueError::new_err)?;
    match r {
        Some(f) => Ok(enumerate::fight_to_py(py, &f)?.into_any()),
        None => Ok(py.None().into_bound(py)),
    }
}

/// The numeric half of builds.resolve_stats: `items` is [(stat pairs, overlay
/// entry), ...] in build order, `pact` Vladimir's Crimson Pact or None.
#[pyfunction]
#[pyo3(signature = (base, level, items, pact=None))]
fn resolve_sheet<'py>(py: Python<'py>, base: &Bound<'py, PyDict>, level: i64,
                      items: &Bound<'py, PyAny>, pact: Option<&Bound<'py, PyDict>>)
    -> PyResult<Bound<'py, PyDict>> {
    let base = sheet::ChampBase::from_py(base)?;
    let mut parsed: Vec<(Vec<(sheet::SK, f64)>, fx::ItemFx)> = Vec::new();
    for it in items.try_iter()? {
        let it = it?;
        let stats = sheet::parse_stat_pairs(&it.get_item(0)?)?;
        let fxd = dict_of(&it.get_item(1)?)?;
        parsed.push((stats, fx::ItemFx::from_py(&fxd)?));
    }
    let refs: Vec<(&[(sheet::SK, f64)], &fx::ItemFx)> =
        parsed.iter().map(|(s, f)| (s.as_slice(), f)).collect();
    let pact = match pact {
        Some(p) => Some(kit::Pact { ap_per_30_bonus_hp: reqf(p, "apPer30BonusHp")?,
                                    bonus_hp_per_ap: reqf(p, "bonusHpPerAp")? }),
        None => None,
    };
    sheet::resolve(&base, level, &refs, pact).to_py(py)
}

/// math.exp(fsum(log(max(x, 1e-9)))/n): the geometric mean the rankings use.
#[pyfunction]
fn geo_mean(xs: Vec<f64>) -> f64 {
    fsum::geo_mean(&xs)
}

/// Correctly rounded sum, math.fsum's bits.
#[pyfunction]
#[pyo3(name = "fsum")]
fn fsum_py(xs: Vec<f64>) -> f64 {
    fsum::fsum(xs)
}

#[pymodule]
fn lol_engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(simulate, m)?)?;
    m.add_function(wrap_pyfunction!(resolve_sheet, m)?)?;
    m.add_function(wrap_pyfunction!(geo_mean, m)?)?;
    m.add_function(wrap_pyfunction!(fsum_py, m)?)?;
    m.add_class::<enumerate::Ctx>()?;
    m.add("DRIVERS", fight::DRIVERS.to_vec())?;
    m.add("SOURCE_HASH", env!("LOL_ENGINE_SOURCE_HASH"))?;
    m.add("VERSION", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
