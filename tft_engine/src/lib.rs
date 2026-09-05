//! The TFT engine: the fight, the item and trait effects, every unit's
//! driver and the enumeration of a cell, compiled. tft.py keeps the data
//! loading, the cache, the warm and the CLI, resolves every number into a
//! cell spec and calls in here through `run_cell`, `simulate`,
//! `compose_fx` and `calc_value`.
//!
//! `SOURCE_HASH` is a sha256 over these sources (build.rs), part of every
//! cache key: a result is only valid for the code that produced it.

mod driver;
mod drivers;
mod enumerate;
mod fight;
mod fx;
mod kit;
mod pyf;
mod pyget;
mod spec;

use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::driver::Driver;
use crate::fight::{FightResult, Opening};
use crate::fx::{build_fx, Fx};
use crate::kit::{CalcId, Kit, Runtime};
use crate::spec::CellSpec;

fn opening_to_py<'py>(py: Python<'py>, o: &Opening) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("ad", o.ad)?;
    d.set_item("ap", o.ap)?;
    d.set_item("as", o.as_)?;
    d.set_item("crit", o.crit)?;
    d.set_item("critMult", o.crit_mult)?;
    d.set_item("precision", o.precision)?;
    d.set_item("hp", o.hp)?;
    d.set_item("armor", o.armor)?;
    d.set_item("mr", o.mr)?;
    d.set_item("durability", o.durability)?;
    d.set_item("omnivamp", o.omnivamp)?;
    d.set_item("form", o.form.map(|f| f.name()))?;
    d.set_item("manaStart", o.mana_start)?;
    d.set_item("manaMax", o.mana_max)?;
    Ok(d)
}

fn result_to_py<'py>(py: Python<'py>, r: &FightResult) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("killTime", r.kill_time)?;
    d.set_item("total", r.total)?;
    d.set_item("dps", r.dps)?;
    d.set_item("rawTotal", r.raw_total)?;
    d.set_item("attacks", r.attacks)?;
    d.set_item("casts", r.casts)?;
    d.set_item("castTimes", PyList::new(py, r.cast_times.iter())?)?;
    let bd = PyDict::new(py);
    for (src, dmg) in &r.breakdown {
        bd.set_item(*src, *dmg)?;
    }
    d.set_item("breakdown", bd)?;
    d.set_item("left", PyList::new(py, r.left.iter())?)?;
    d.set_item("t", r.t)?;
    d.set_item("aliveTime", r.alive_time)?;
    d.set_item("died", r.died)?;
    d.set_item("diedAt", r.died_at)?;
    d.set_item("hpLeft", r.hp_left)?;
    d.set_item("absorbed", r.absorbed)?;
    d.set_item("taken", r.taken)?;
    d.set_item("mitigated", r.mitigated)?;
    d.set_item("healed", r.healed)?;
    d.set_item("shielded", r.shielded)?;
    d.set_item("denied", r.denied)?;
    d.set_item("allyHeal", r.ally_heal)?;
    d.set_item("allyShield", r.ally_shield)?;
    d.set_item("ccTime", r.cc_time)?;
    d.set_item("hitsTaken", r.hits_taken)?;
    d.set_item("dummyCasts", PyList::new(py, r.dummy_casts.iter())?)?;
    d.set_item("dummyAttacks", PyList::new(py, r.dummy_attacks.iter())?)?;
    let p = PyDict::new(py);
    p.set_item("mana", r.probe.mana)?;
    p.set_item("lockUntil", r.probe.lock_until)?;
    p.set_item("castingUntil", r.probe.casting_until)?;
    p.set_item("asStack", r.probe.as_stack)?;
    p.set_item("adapStackN", r.probe.adap_stack_n)?;
    p.set_item("untargetableUntil", r.probe.untargetable_until)?;
    p.set_item("shieldsActive", r.probe.shields_active)?;
    p.set_item("maxHp", r.probe.max_hp)?;
    d.set_item("probe", p)?;
    if let Some(tr) = &r.trace {
        let evs = PyList::empty(py);
        for e in tr {
            evs.append((e.t, e.kind, e.amount, e.target, e.src, e.hp))?;
        }
        d.set_item("trace", evs)?;
    }
    Ok(d)
}

fn fx_to_py<'py>(py: Python<'py>, fx: &Fx) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("adPct", fx.ad_pct)?;
    d.set_item("ap", fx.ap)?;
    d.set_item("asPct", fx.as_pct)?;
    d.set_item("crit", fx.crit)?;
    d.set_item("critDmg", fx.crit_dmg)?;
    d.set_item("amp", fx.amp)?;
    d.set_item("hp", fx.hp)?;
    d.set_item("hpMult", fx.hp_mult)?;
    d.set_item("armor", fx.armor)?;
    d.set_item("mr", fx.mr)?;
    d.set_item("manaRegen", fx.mana_regen)?;
    d.set_item("manaPerAttack", fx.mana_per_attack)?;
    d.set_item("manaPerCrit", fx.mana_per_crit)?;
    d.set_item("manaMult", fx.mana_mult)?;
    d.set_item("adapMult", fx.adap_mult)?;
    d.set_item("startingMana", fx.starting_mana)?;
    d.set_item("precision", fx.precision)?;
    d.set_item("ampVsTank", fx.amp_vs_tank)?;
    d.set_item("asPerSecond", fx.as_per_second.clone())?;
    d.set_item("adPerAttack", fx.ad_per_attack.clone())?;
    d.set_item("adapPerAttack", fx.adap_per_attack.clone())?;
    d.set_item("apPerInterval", fx.ap_per_interval.clone())?;
    d.set_item("apAfter", fx.ap_after.clone())?;
    d.set_item("ampPerCrit", fx.amp_per_crit.clone())?;
    d.set_item("asPerAttackStack", fx.as_per_attack_stack.clone())?;
    d.set_item("apPerCast", fx.ap_per_cast)?;
    d.set_item("sunderOnHit", fx.sunder_on_hit.clone())?;
    d.set_item("shredOnHit", fx.shred_on_hit.clone())?;
    d.set_item("burnOnHit", fx.burn_on_hit.clone())?;
    d.set_item("sunderAura", fx.sunder_aura)?;
    d.set_item("shredAura", fx.shred_aura)?;
    d.set_item("burnAura", fx.burn_aura)?;
    d.set_item("ampAfterSameTarget", fx.amp_after_same_target)?;
    d.set_item("bleedPct", fx.bleed_pct)?;
    d.set_item("bleedDur", fx.bleed_dur)?;
    d.set_item("bonusMagicPct", fx.bonus_magic_pct)?;
    d.set_item("ravager", fx.ravager)?;
    d.set_item("riftbeast", fx.riftbeast)?;
    d.set_item("form", fx.form.map(|f| f.name()))?;
    d.set_item("omnivamp", fx.omnivamp)?;
    d.set_item("durabilities", fx.durabilities.clone())?;
    d.set_item("durabilityByHealth", fx.durability_by_health.clone())?;
    d.set_item("durability", fx.durability())?;
    d.set_item("attackDamageTaken", fx.attack_damage_taken)?;
    d.set_item("thorns", fx.thorns.clone())?;
    d.set_item("resistsPerAttacker", fx.resists_per_attacker.to_vec())?;
    d.set_item("healPerInterval", fx.heal_per_interval.clone())?;
    d.set_item("regenMissingPct", fx.regen_missing_pct)?;
    d.set_item("shieldAtHp", fx.shield_at_hp.clone())?;
    d.set_item("shieldAtStart", fx.shield_at_start.clone())?;
    d.set_item("resistsAtStart", fx.resists_at_start.clone())?;
    d.set_item("untargetableAtHp", fx.untargetable_at_hp.clone())?;
    d.set_item("manaAtHp", fx.mana_at_hp.clone())?;
    d.set_item("adapPerHit", fx.adap_per_hit)?;
    d.set_item("ionicSpark", fx.ionic_spark)?;
    d.set_item("allyHealPct", fx.ally_heal_pct)?;
    d.set_item("hojs", fx.hojs.clone())?;
    d.set_item("healOnTakedown", fx.heal_on_takedown)?;
    d.set_item("manaOnTakedown", fx.mana_on_takedown)?;
    d.set_item("faeHeal", fx.fae_heal)?;
    d.set_item("summoner", fx.summoner.map(|s| {
        (s.damage_mult, s.health_mult, s.extra_summons, s.extra_attacks, s.summon_power)
    }))?;
    d.set_item("caustic", fx.caustic)?;
    d.set_item("notes", fx.notes.clone())?;
    Ok(d)
}

/// Every build of the spec's pool, sorted best first: returns
/// (build count, [(pool indices, opening sheet, result), ...]) for the
/// top `top` rows. `workers` 0 uses every core.
#[pyfunction]
#[pyo3(signature = (spec, top=250, workers=0))]
fn run_cell<'py>(py: Python<'py>, spec: &Bound<'py, PyDict>, top: usize, workers: usize)
    -> PyResult<(usize, Bound<'py, PyList>)> {
    let spec = CellSpec::from_py(spec)?;
    let name = spec.driver.clone();
    let (n, rows) = with_driver!(name.as_str(), D, {
        debug_assert_eq!(D::NAME, name);
        Ok::<_, PyErr>(py.detach(|| enumerate::run_cell::<D>(&spec, top, workers)))
    })?;
    let out = PyList::empty(py);
    for row in &rows {
        let combo = PyList::new(py, row.combo.iter())?;
        out.append((combo, opening_to_py(py, &row.opening)?, result_to_py(py, &row.res)?))?;
    }
    Ok((n, out))
}

/// One fight with the spec's `items`: (opening sheet, result). With
/// `trace` the result carries every event.
#[pyfunction]
#[pyo3(signature = (spec, trace=false))]
fn simulate<'py>(py: Python<'py>, spec: &Bound<'py, PyDict>, trace: bool)
    -> PyResult<(Bound<'py, PyDict>, Bound<'py, PyDict>)> {
    let spec = CellSpec::from_py(spec)?;
    let name = spec.driver.clone();
    let (o, r) = with_driver!(name.as_str(), D, {
        debug_assert_eq!(D::NAME, name);
        let drivers = enumerate::Drivers::<D>::new(&spec);
        let items: Vec<&fx::ItemFx> = spec.items.iter().collect();
        Ok::<_, PyErr>(enumerate::run_fight::<D>(&spec, &drivers, &items, trace))
    })?;
    Ok((opening_to_py(py, &o)?, result_to_py(py, &r)?))
}

/// The composed effects of the spec's `items` and traits (tft.build_fx).
#[pyfunction]
fn compose_fx<'py>(py: Python<'py>, spec: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyDict>> {
    let spec = CellSpec::from_py(spec)?;
    let items: Vec<&fx::ItemFx> = spec.items.iter().collect();
    let fx = build_fx(spec.role, &items, &spec.traits, spec.unit.has_forms, spec.unit.attack);
    fx_to_py(py, &fx)
}

/// tft.calc_value on a kit spec: fold `name` at the given stats.
#[pyfunction]
#[pyo3(signature = (kit, name, ad, ap, max_hp, armor, mr, base_ad, runtime=None))]
#[allow(clippy::too_many_arguments)]
fn calc_value(kit: &Bound<'_, PyDict>, name: &str, ad: f64, ap: f64, max_hp: f64, armor: f64,
              mr: f64, base_ad: f64, runtime: Option<&Bound<'_, PyDict>>) -> PyResult<f64> {
    let kit = Kit::from_py(kit, "kit")?;
    let id = kit.calc(name);
    if id == CalcId::MISSING {
        return Err(PyKeyError::new_err(format!("no calc {name}")));
    }
    let mut values: Vec<(String, f64)> = Vec::new();
    if let Some(rt) = runtime {
        for (k, v) in rt.iter() {
            values.push((k.extract()?, v.extract()?));
        }
    }
    let refs: Vec<(&str, f64)> = values.iter().map(|(k, v)| (k.as_str(), *v)).collect();
    Ok(kit.calc_value(id, ad, ap, max_hp, armor, mr, base_ad, Runtime { values: &refs }))
}

#[pymodule]
fn lol_tft(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_cell, m)?)?;
    m.add_function(wrap_pyfunction!(simulate, m)?)?;
    m.add_function(wrap_pyfunction!(compose_fx, m)?)?;
    m.add_function(wrap_pyfunction!(calc_value, m)?)?;
    let drivers = PyDict::new(m.py());
    for (api, name) in drivers::DRIVERS {
        drivers.set_item(*api, *name)?;
    }
    m.add("DRIVERS", drivers)?;
    m.add("NAMES", drivers::NAMES.to_vec())?;
    m.add("SOURCE_HASH", env!("LOL_TFT_SOURCE_HASH"))?;
    m.add("VERSION", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}

