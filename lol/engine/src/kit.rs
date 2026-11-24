//! A champion's kit encoding (data/builds/<slug>.json) parsed into the
//! numbers the drivers read. Everything is optional at parse time; a driver
//! demands what its rotation needs when it is built.

use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};

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

/// Twitch's Deadly Venom: what one stack deals each second (by level, plus an
/// AP ratio), how many stack, how long they last, how often they tick.
#[derive(Clone, Debug)]
pub struct Venom {
    /// Level 1 first; a level past the end reads the last entry.
    pub per_stack_by_level: Vec<f64>,
    pub ap_ratio: f64,
    pub max_stacks: i64,
    pub duration_s: f64,
    pub tick_s: f64,
}

/// An attack-speed steroid an ability grants: percent by rank, for a while.
#[derive(Clone, Debug)]
pub struct AsBuff {
    pub pct: Vec<f64>,
    pub duration_s: f64,
}

/// A lingering area that stacks the kit's passive on whoever stands in it.
#[derive(Clone, Debug)]
pub struct Cloud {
    pub duration_s: f64,
    pub tick_s: f64,
    pub stacks_per_tick: i64,
}

/// Kassadin's Nether Blade active: the next attack inside `window_s` deals
/// this on top of the passive on-hit and gives back a share of the missing
/// mana (quintupled against a champion, which the stat dummy stands in for).
#[derive(Clone, Debug)]
pub struct Empowered {
    pub damage: DamageSpec,
    pub window_s: f64,
    pub missing_mana_pct: Vec<f64>,
    pub champion_mult: f64,
}

/// Kassadin's Riftwalk: the blink's own damage, what each standing stack
/// adds to it, and how the mana cost doubles per stack.
#[derive(Clone, Debug)]
pub struct Riftwalk {
    pub base: DamageSpec,
    pub per_stack: DamageSpec,
    pub max_stacks: i64,
    pub stack_duration_s: f64,
    pub mana_cost_mult: f64,
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
    // Twitch: Ambush's steroid, Venom Cask's stacks and cloud, Contaminate's
    // per-stack halves and the stacks it waits for, Spray and Pray's bonus AD
    pub attack_speed: Option<AsBuff>,
    pub stacks_on_hit: Option<i64>,
    pub cloud: Option<Cloud>,
    pub per_stack_phys: Option<DamageSpec>,
    pub per_stack_magic: Option<DamageSpec>,
    pub max_stacks: Option<i64>,
    pub bonus_ad: Option<Vec<f64>>,
    // Kassadin: what a cast costs, Force Pulse's per-cast cooldown refund,
    // Nether Blade's empowered attack and Riftwalk's stacking blink
    pub mana: Vec<f64>,
    pub cd_reduction_per_cast_s: f64,
    pub empowered: Option<Empowered>,
    pub riftwalk: Option<Riftwalk>,
}

/// Which rotation driver a kit needs (engine/src/drivers.rs). Settled
/// once, when the kit is parsed, so a fight never matches on a string.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DriverId {
    Kayle,
    Vladimir,
    Twitch,
    Kassadin,
    /// A driver under engine/src/generated/, looked up by the champion's slug.
    Generated,
}

impl DriverId {
    /// The driver a champion slug names, `None` when the engine has none
    /// (`simulate` says so, exactly as it always did).
    pub fn of(champion: &str) -> Option<DriverId> {
        match champion {
            "kayle" => Some(DriverId::Kayle),
            "vladimir" => Some(DriverId::Vladimir),
            "twitch" => Some(DriverId::Twitch),
            "kassadin" => Some(DriverId::Kassadin),
            other if crate::generated::NAMES.contains(&other) => Some(DriverId::Generated),
            _ => None,
        }
    }
}

/// FxHash, rustc's own hasher: a rotate, an xor and a multiply per 8 bytes.
/// The generated drivers probe the kit's path maps in `Driver::new`, which
/// runs once per boots class, tens of millions of times an enumeration, so
/// the hash has to be cheap. The keys are the kit's own paths, so SipHash's
/// flooding resistance bought nothing here.
#[derive(Clone, Copy, Default)]
pub struct FxHasher {
    hash: u64,
}

impl FxHasher {
    #[inline]
    fn add(&mut self, word: u64) {
        self.hash = (self.hash.rotate_left(5) ^ word).wrapping_mul(0x51_7c_c1_b7_27_22_0a_95);
    }
}

impl Hasher for FxHasher {
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        let mut words = bytes.chunks_exact(8);
        for w in &mut words {
            self.add(u64::from_le_bytes(w.try_into().unwrap()));
        }
        let tail = words.remainder();
        if !tail.is_empty() {
            let mut w = [0u8; 8];
            w[..tail.len()].copy_from_slice(tail);
            // the tail's length rides above its bytes, so zero padding can't
            // make two tails hash alike
            self.add(u64::from_le_bytes(w) | ((tail.len() as u64) << 56));
        }
    }

    #[inline]
    fn write_u8(&mut self, i: u8) {
        self.add(i as u64);
    }

    #[inline]
    fn finish(&self) -> u64 {
        self.hash
    }
}

/// A map keyed by dotted kit path.
pub type PathMap<V> = HashMap<String, V, BuildHasherDefault<FxHasher>>;

/// The fields of a damage block (see `Kit::hit`).
const DAMAGE_FIELDS: [&str; 7] = ["base", "apRatio", "adRatio", "bonusAdRatio", "maxHpRatio",
                                  "bonusHpRatio", "maxManaRatio"];

/// One leaf of a kit encoding, for the generated drivers: they read the kit
/// by dotted path ("abilities.Q.damage.base") instead of through typed
/// fields, so a new champion adds no parsing code.
#[derive(Clone, Debug, PartialEq)]
pub enum KitValue {
    Num(f64),
    List(Vec<f64>),
    Text(String),
    Flag(bool),
}

fn flatten(prefix: &str, v: &Bound<'_, PyAny>, out: &mut PathMap<KitValue>) -> PyResult<()> {
    if let Ok(d) = v.cast::<PyDict>() {
        for (k, item) in d.iter() {
            let key: String = k.extract()?;
            let path = if prefix.is_empty() { key } else { format!("{prefix}.{key}") };
            flatten(&path, &item, out)?;
        }
    } else if let Ok(b) = v.extract::<bool>() {
        if v.is_instance_of::<pyo3::types::PyBool>() {
            out.insert(prefix.to_string(), KitValue::Flag(b));
        } else if let Ok(x) = v.extract::<f64>() {
            out.insert(prefix.to_string(), KitValue::Num(x));
        }
    } else if let Ok(x) = v.extract::<f64>() {
        out.insert(prefix.to_string(), KitValue::Num(x));
    } else if let Ok(xs) = v.extract::<Vec<f64>>() {
        out.insert(prefix.to_string(), KitValue::List(xs));
    } else if let Ok(t) = v.extract::<String>() {
        out.insert(prefix.to_string(), KitValue::Text(t));
    }
    Ok(())
}

/// Every damage block of a flattened kit by its path: each path with at
/// least one of `DAMAGE_FIELDS` directly under it.
fn damage_blocks(values: &PathMap<KitValue>) -> PathMap<DamageSpec> {
    let mut out = PathMap::default();
    for key in values.keys() {
        let Some((path, field)) = key.rsplit_once('.') else { continue };
        if DAMAGE_FIELDS.contains(&field) && !out.contains_key(path) {
            out.insert(path.to_string(), damage_block(values, path));
        }
    }
    out
}

/// The block at `path`: a missing `base` is 0, a `base` shorter than six
/// ranks repeats its last entry, and a ratio that isn't a number is absent.
fn damage_block(values: &PathMap<KitValue>, path: &str) -> DamageSpec {
    let num = |field: &str| match values.get(&format!("{path}.{field}")) {
        Some(KitValue::Num(x)) => Some(*x),
        _ => None,
    };
    let mut base = match values.get(&format!("{path}.base")) {
        Some(KitValue::List(xs)) => xs.clone(),
        Some(KitValue::Num(x)) => vec![*x],
        _ => vec![0.0],
    };
    let last = *base.last().unwrap_or(&0.0);
    base.resize(base.len().max(6), last);
    DamageSpec {
        base,
        bonus_ad_ratio: num("bonusAdRatio").unwrap_or(0.0),
        ad_ratio: num("adRatio").unwrap_or(0.0),
        ap_ratio: num("apRatio").unwrap_or(0.0),
        max_hp_ratio: num("maxHpRatio"),
        bonus_hp_ratio: num("bonusHpRatio"),
        max_mana_ratio: num("maxManaRatio"),
    }
}

#[derive(Clone, Debug)]
pub struct Kit {
    /// Every leaf of the encoding by dotted path (see `KitValue`).
    pub values: PathMap<KitValue>,
    /// Every damage block of the encoding, parsed once here (`Kit::hit`).
    damages: PathMap<DamageSpec>,
    pub champion: String,
    pub driver: Option<DriverId>,
    pub attack_never: bool,
    pub windup_fraction: Option<f64>,
    pub zealous: Option<Zealous>,
    pub arisen: Option<Form>,
    pub aflame: Option<Aflame>,
    pub transcendent: Option<Form>,
    pub crimson_pact: Option<Pact>,
    pub deadly_venom: Option<Venom>,
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
        max_mana_ratio: if has(d, "maxManaRatio")? { Some(reqf(d, "maxManaRatio")?) } else { None },
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
    if let Some(a) = getd(&d, "attackSpeed")? {
        ab.attack_speed = Some(AsBuff {
            pct: getvecf(&a, "pct")?.ok_or_else(|| PyKeyError::new_err("pct"))?,
            duration_s: reqf(&a, "durationS")?,
        });
    }
    if has(&d, "stacksOnHit")? {
        ab.stacks_on_hit = Some(reqi(&d, "stacksOnHit")?);
    }
    if let Some(c) = getd(&d, "cloud")? {
        ab.cloud = Some(Cloud { duration_s: reqf(&c, "durationS")?, tick_s: reqf(&c, "tickS")?,
                                stacks_per_tick: reqi(&c, "stacksPerTick")? });
    }
    if let Some(ps) = getd(&d, "perStack")? {
        if let Some(p) = getd(&ps, "physical")? {
            ab.per_stack_phys = Some(parse_damage_spec(&p)?);
        }
        if let Some(m) = getd(&ps, "magic")? {
            ab.per_stack_magic = Some(parse_damage_spec(&m)?);
        }
    }
    if has(&d, "maxStacks")? {
        ab.max_stacks = Some(reqi(&d, "maxStacks")?);
    }
    ab.bonus_ad = getvecf(&d, "bonusAd")?;
    ab.mana = getvecf(&d, "mana")?.unwrap_or_default();
    ab.cd_reduction_per_cast_s = getf(&d, "cooldownReductionPerCastS", 0.0)?;
    if let Some(em) = getd(&d, "empowered")? {
        ab.empowered = Some(Empowered {
            damage: parse_damage_spec(&reqd(&em, "damage")?)?,
            window_s: reqf(&em, "windowS")?,
            missing_mana_pct: getvecf(&em, "missingManaPct")?
                .ok_or_else(|| PyKeyError::new_err("missingManaPct"))?,
            champion_mult: reqf(&em, "championMult")?,
        });
    }
    if let Some(rw) = getd(&d, "riftwalk")? {
        ab.riftwalk = Some(Riftwalk {
            base: parse_damage_spec(&reqd(&rw, "base")?)?,
            per_stack: parse_damage_spec(&reqd(&rw, "perStack")?)?,
            max_stacks: reqi(&rw, "maxStacks")?,
            stack_duration_s: reqf(&rw, "stackDurationS")?,
            mana_cost_mult: reqf(&rw, "manaCostMult")?,
        });
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
        let mut venom = None;
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
            if let Some(v) = getd(p, "deadlyVenom")? {
                let per = reqd(&v, "perStackPerSecond")?;
                venom = Some(Venom {
                    per_stack_by_level: getvecf(&per, "byLevel")?
                        .ok_or_else(|| PyKeyError::new_err("byLevel"))?,
                    ap_ratio: getf(&per, "apRatio", 0.0)?,
                    max_stacks: reqi(&v, "maxStacks")?,
                    duration_s: reqf(&v, "durationS")?,
                    tick_s: reqf(&v, "tickS")?,
                });
            }
        }
        let abilities = reqd(d, "abilities")?;
        let champion = reqs(d, "champion")?;
        let mut values = PathMap::default();
        flatten("", d.as_any(), &mut values)?;
        Ok(Kit {
            damages: damage_blocks(&values),
            values,
            driver: DriverId::of(&champion),
            champion,
            attack_never,
            windup_fraction: windup,
            zealous,
            arisen,
            aflame,
            transcendent,
            crimson_pact: pact,
            deadly_venom: venom,
            q: parse_ability(getd(&abilities, "Q")?)?,
            w: parse_ability(getd(&abilities, "W")?)?,
            e: parse_ability(getd(&abilities, "E")?)?,
            r: parse_ability(getd(&abilities, "R")?)?,
        })
    }
}

/// The generated drivers' view of a kit: values by dotted path, with errors
/// that name the path. All of it runs in `Driver::new`, never inside a fight,
/// but the enumerator builds a driver once per boots class, so each read is
/// one hash probe with no path formatting and no copying beyond the Vec that
/// `list` returns.
impl Kit {
    pub fn has(&self, path: &str) -> bool {
        self.values.contains_key(path)
    }

    /// A number; a missing path is an error that names it.
    pub fn num(&self, path: &str) -> Result<f64, String> {
        match self.values.get(path) {
            Some(KitValue::Num(x)) => Ok(*x),
            Some(other) => Err(format!("kit value {path} is {other:?}, not a number")),
            None => Err(format!("kit has no value {path}")),
        }
    }

    /// A number, or `default` when the kit does not state it.
    pub fn num_or(&self, path: &str, default: f64) -> f64 {
        self.num(path).unwrap_or(default)
    }

    /// A list of numbers; a single number reads as a list of one.
    pub fn list(&self, path: &str) -> Result<Vec<f64>, String> {
        match self.values.get(path) {
            Some(KitValue::List(xs)) => Ok(xs.clone()),
            Some(KitValue::Num(x)) => Ok(vec![*x]),
            Some(other) => Err(format!("kit value {path} is {other:?}, not a list")),
            None => Err(format!("kit has no value {path}")),
        }
    }

    /// The value at an ability rank (1-based): a list is indexed (its last
    /// entry stands for any rank past its end), a number is the same at every
    /// rank. Rank 0 (the ability is not learned) reads 0.
    pub fn at_rank(&self, path: &str, rank: i64) -> Result<f64, String> {
        let xs: &[f64] = match self.values.get(path) {
            Some(KitValue::List(xs)) => xs,
            Some(KitValue::Num(x)) => std::slice::from_ref(x),
            Some(other) => return Err(format!("kit value {path} is {other:?}, not a list")),
            None => return Err(format!("kit has no value {path}")),
        };
        if rank <= 0 || xs.is_empty() {
            return Ok(0.0);
        }
        Ok(xs[((rank - 1) as usize).min(xs.len() - 1)])
    }

    /// The value at a champion level (1-18) of an 18-entry list (a shorter
    /// list reads its last entry past its end; a number is level-independent).
    pub fn at_level(&self, path: &str, level: i64) -> Result<f64, String> {
        self.at_rank(path, level.max(1))
    }

    pub fn flag(&self, path: &str) -> bool {
        matches!(self.values.get(path), Some(KitValue::Flag(true)))
    }

    pub fn text(&self, path: &str) -> Option<&str> {
        match self.values.get(path) {
            Some(KitValue::Text(t)) => Some(t.as_str()),
            _ => None,
        }
    }

    /// The damage block at `path` evaluated at an ability rank on a sheet, and
    /// 0 for an ability not learned. A block is the `{base, apRatio, adRatio,
    /// bonusAdRatio, maxHpRatio, bonusHpRatio, maxManaRatio}` under `path`
    /// (see `damage_block`): the three health and mana ratios are PERCENT of
    /// the caster's own stat, the others are fractions.
    pub fn hit(&self, path: &str, rank: i64, sheet: &crate::sheet::Sheet) -> Result<f64, String> {
        if rank <= 0 {
            return Ok(0.0);
        }
        match self.damages.get(path) {
            Some(d) => Ok(d.hit(rank, sheet)),
            None => Err(format!("kit has no damage block {path}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn values(leaves: &[(&str, KitValue)]) -> PathMap<KitValue> {
        leaves.iter().map(|(k, v)| (k.to_string(), v.clone())).collect()
    }

    #[test]
    fn damage_blocks_are_the_paths_with_a_damage_field_under_them() {
        let v = values(&[
            ("gen.W.damage.base", KitValue::List(vec![30.0, 55.0])),
            ("gen.W.damage.note", KitValue::Text("not a field".into())),
            ("gen.P.burst.apRatio", KitValue::Num(0.1)),
            ("gen.P.burst.byLevel.base", KitValue::Num(5.0)),
            ("gen.Q.cooldownS", KitValue::List(vec![10.0, 9.0])),
            ("base", KitValue::Num(1.0)),
        ]);
        let mut paths: Vec<String> = damage_blocks(&v).into_keys().collect();
        paths.sort_unstable();
        assert_eq!(paths, ["gen.P.burst", "gen.P.burst.byLevel", "gen.W.damage"]);
    }

    #[test]
    fn damage_block_pads_base_and_reads_only_numeric_ratios() {
        let v = values(&[
            ("w.base", KitValue::List(vec![30.0, 55.0, 80.0])),
            ("w.apRatio", KitValue::Num(0.45)),
            ("w.maxHpRatio", KitValue::Num(2.0)),
            ("w.adRatio", KitValue::List(vec![1.0])),
            ("p.base", KitValue::Num(12.0)),
            ("e.bonusAdRatio", KitValue::Num(0.6)),
        ]);
        let w = damage_block(&v, "w");
        assert_eq!(w.base, [30.0, 55.0, 80.0, 80.0, 80.0, 80.0]);
        assert_eq!((w.ap_ratio, w.ad_ratio, w.bonus_ad_ratio), (0.45, 0.0, 0.0));
        assert_eq!((w.max_hp_ratio, w.bonus_hp_ratio, w.max_mana_ratio), (Some(2.0), None, None));
        assert_eq!(damage_block(&v, "p").base, [12.0; 6]);
        let e = damage_block(&v, "e");
        assert_eq!((e.base, e.bonus_ad_ratio), (vec![0.0; 6], 0.6));
    }
}
