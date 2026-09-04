//! A champion's kit encoding (data/builds/<slug>.json) parsed into the
//! numbers the drivers read. Everything is optional at parse time; a driver
//! demands what its rotation needs when it is built.

use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::num::{ByLevel, DamageSpec};
use crate::pyget::*;

#[derive(Clone, Debug)]
pub struct Zealous {
    pub as_pct_per_stack: f64,
    pub max_stacks: i64,
    pub permanent_at_level: i64,
}

#[derive(Clone, Debug)]
pub struct Form {
    pub level: i64,
    pub attack_range: Option<f64>,
}

#[derive(Clone, Debug)]
pub struct Wave {
    pub base_by_level: ByLevel,
    pub bonus_ad_ratio: f64,
    pub ap_ratio: f64,
}

#[derive(Clone, Debug)]
pub struct Aflame {
    pub level: i64,
    pub wave: Option<Wave>,
}

#[derive(Clone, Copy, Debug)]
pub struct Pact {
    pub ap_per_30_bonus_hp: f64,
    pub bonus_hp_per_ap: f64,
}

#[derive(Clone, Debug)]
pub struct EActive {
    pub missing_hp_pct: Vec<f64>,
    pub missing_hp_pct_per_100_ap: f64,
}

#[derive(Clone, Debug)]
pub struct CrimsonRush {
    pub every_nth_cast: i64,
    pub bonus_damage_pct: f64,
}

#[derive(Clone, Debug, Default)]
pub struct Ability {
    pub cooldown_s: Vec<f64>,
    pub damage: Option<DamageSpec>,
    pub damage_max: Option<DamageSpec>, // Vladimir E: damage.max
    pub shred_pct_armor: f64,
    pub shred_pct_mr: f64,
    pub shred_duration_s: f64,
    pub crimson_rush: Option<CrimsonRush>,
    pub onhit: Option<DamageSpec>,
    pub active: Option<EActive>,
    pub charge_full_s: Option<f64>,
    pub duration_s: Option<f64>,
    pub ticks: Option<i64>,
    pub tick_s: Option<f64>,
    pub delay_s: Option<f64>,
    pub amp_pct: Option<f64>,
}

#[derive(Clone, Debug)]
pub struct Kit {
    pub champion: String,
    pub attack_never: bool,
    pub windup_fraction: Option<f64>,
    pub zealous: Option<Zealous>,
    pub arisen: Option<Form>,
    pub aflame: Option<Aflame>,
    pub transcendent: Option<Form>,
    pub crimson_pact: Option<Pact>,
    pub q: Ability,
    pub w: Ability,
    pub e: Ability,
    pub r: Ability,
}

fn parse_by_level(d: &Bound<'_, PyDict>) -> PyResult<ByLevel> {
    let levels = getvecf(d, "levels")?.ok_or_else(|| PyKeyError::new_err("levels"))?;
    if levels.len() != 2 {
        return Err(PyKeyError::new_err("levels"));
    }
    Ok(ByLevel { from: reqf(d, "from")?, to: reqf(d, "to")?, lo: levels[0] as i64,
                 hi: levels[1] as i64 })
}

pub fn parse_damage_spec(d: &Bound<'_, PyDict>) -> PyResult<DamageSpec> {
    Ok(DamageSpec {
        base: getvecf(d, "base")?.ok_or_else(|| PyKeyError::new_err("base"))?,
        bonus_ad_ratio: getf(d, "bonusAdRatio", 0.0)?,
        ad_ratio: getf(d, "adRatio", 0.0)?,
        ap_ratio: getf(d, "apRatio", 0.0)?,
        max_hp_ratio: if has(d, "maxHpRatio")? { Some(reqf(d, "maxHpRatio")?) } else { None },
        bonus_hp_ratio: if has(d, "bonusHpRatio")? { Some(reqf(d, "bonusHpRatio")?) } else { None },
    })
}

fn parse_form(d: &Bound<'_, PyDict>) -> PyResult<Form> {
    Ok(Form { level: reqi(d, "level")?,
              attack_range: if has(d, "attackRange")? { Some(reqf(d, "attackRange")?) } else { None } })
}

fn parse_ability(d: Option<Bound<'_, PyDict>>) -> PyResult<Ability> {
    let d = match d {
        Some(d) => d,
        None => return Ok(Ability::default()),
    };
    let mut ab = Ability { cooldown_s: getvecf(&d, "cooldownS")?.unwrap_or_default(), ..Default::default() };
    if let Some(dmg) = getd(&d, "damage")? {
        if has(&dmg, "max")? {
            ab.damage_max = Some(parse_damage_spec(&reqd(&dmg, "max")?)?);
        } else {
            ab.damage = Some(parse_damage_spec(&dmg)?);
        }
        if has(&dmg, "ticks")? {
            ab.ticks = Some(reqi(&dmg, "ticks")?);
        }
        if has(&dmg, "tickS")? {
            ab.tick_s = Some(reqf(&dmg, "tickS")?);
        }
    }
    if let Some(sh) = getd(&d, "shred")? {
        let pct = getf(&sh, "pct", 0.0)?;
        for res in getlist(&sh, "appliesTo")? {
            match res.extract::<String>()?.as_str() {
                "armor" => ab.shred_pct_armor = pct,
                "mr" => ab.shred_pct_mr = pct,
                _ => {}
            }
        }
        ab.shred_duration_s = getf(&sh, "durationS", 0.0)?;
    }
    if let Some(cr) = getd(&d, "crimsonRush")? {
        ab.crimson_rush = Some(CrimsonRush { every_nth_cast: reqi(&cr, "everyNthCast")?,
                                             bonus_damage_pct: reqf(&cr, "bonusDamagePct")? });
    }
    if let Some(oh) = getd(&d, "onhit")? {
        ab.onhit = Some(parse_damage_spec(&oh)?);
    }
    if let Some(act) = getd(&d, "active")? {
        ab.active = Some(EActive {
            missing_hp_pct: getvecf(&act, "missingHpPct")?.ok_or_else(|| PyKeyError::new_err("missingHpPct"))?,
            missing_hp_pct_per_100_ap: reqf(&act, "missingHpPctPer100Ap")?,
        });
    }
    if has(&d, "chargeFullS")? {
        ab.charge_full_s = Some(reqf(&d, "chargeFullS")?);
    }
    if has(&d, "durationS")? {
        ab.duration_s = Some(reqf(&d, "durationS")?);
    }
    if has(&d, "delayS")? {
        ab.delay_s = Some(reqf(&d, "delayS")?);
    }
    if has(&d, "ampPct")? {
        ab.amp_pct = Some(reqf(&d, "ampPct")?);
    }
    Ok(ab)
}

impl Kit {
    pub fn from_py(d: &Bound<'_, PyDict>) -> PyResult<Kit> {
        let attack = getd(d, "attack")?;
        let (attack_never, windup) = match &attack {
            Some(a) => (truthy(a, "never")?,
                        if has(a, "windupFraction")? { Some(reqf(a, "windupFraction")?) } else { None }),
            None => (false, None),
        };
        let passive = getd(d, "passive")?;
        let (mut zealous, mut arisen, mut aflame, mut transcendent, mut pact) = (None, None, None, None, None);
        if let Some(p) = &passive {
            if let Some(z) = getd(p, "zealous")? {
                zealous = Some(Zealous {
                    as_pct_per_stack: reqf(&z, "asPctPerStack")?,
                    max_stacks: reqi(&z, "maxStacks")?,
                    permanent_at_level: reqi(&z, "permanentAtLevel")?,
                });
            }
            if let Some(f) = getd(p, "arisen")? {
                arisen = Some(parse_form(&f)?);
            }
            if let Some(a) = getd(p, "aflame")? {
                let wave = match getd(&a, "wave")? {
                    Some(w) => Some(Wave {
                        base_by_level: parse_by_level(&reqd(&w, "baseByLevel")?)?,
                        bonus_ad_ratio: reqf(&w, "bonusAdRatio")?,
                        ap_ratio: reqf(&w, "apRatio")?,
                    }),
                    None => None,
                };
                aflame = Some(Aflame { level: reqi(&a, "level")?, wave });
            }
            if let Some(f) = getd(p, "transcendent")? {
                transcendent = Some(parse_form(&f)?);
            }
            if let Some(c) = getd(p, "crimsonPact")? {
                pact = Some(Pact { ap_per_30_bonus_hp: reqf(&c, "apPer30BonusHp")?,
                                   bonus_hp_per_ap: reqf(&c, "bonusHpPerAp")? });
            }
        }
        let abilities = reqd(d, "abilities")?;
        Ok(Kit {
            champion: reqs(d, "champion")?,
            attack_never,
            windup_fraction: windup,
            zealous,
            arisen,
            aflame,
            transcendent,
            crimson_pact: pact,
            q: parse_ability(getd(&abilities, "Q")?)?,
            w: parse_ability(getd(&abilities, "W")?)?,
            e: parse_ability(getd(&abilities, "E")?)?,
            r: parse_ability(getd(&abilities, "R")?)?,
        })
    }
}
