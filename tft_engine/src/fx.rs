//! Everything items, traits and the role add to a unit for one fight (the
//! port of tft.Fx / apply_item / apply_trait / build_fx). Python resolves
//! every number — an item's stat line as (key, value) pairs in the line's
//! order, its passive as plain numbers with the range and role gates
//! already applied, a trait's bonus at the breakpoint being simulated —
//! and this composes them per build in exactly the order the Python engine
//! did, so every sum has the same bits.

use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::pyget::*;
use crate::pyf::pymax;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Form {
    AD,
    AP,
}

impl Form {
    pub fn name(self) -> &'static str {
        match self {
            Form::AD => "AD",
            Form::AP => "AP",
        }
    }
}

/// A stat key of the stat line, a hand-file `adds` entry or a trait's
/// `stats` map.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StatKey {
    AdPct,
    Ap,
    AsPct,
    Crit,
    CritDmg,
    Amp,
    Hp,
    HpMult,
    Armor,
    Mr,
    ManaRegen,
    ManaPerAttack,
    ManaPerCrit,
    Omnivamp,
    Durability,
    /// Trait `adap`: both attack damage and ability power.
    Adap,
    /// Trait `adOrAp`: whichever side the build leans to.
    AdOrAp,
    StartingMana,
    AmpVsTank,
}

impl StatKey {
    pub fn parse(s: &str) -> PyResult<StatKey> {
        Ok(match s {
            "adPct" => StatKey::AdPct,
            "ap" => StatKey::Ap,
            "asPct" => StatKey::AsPct,
            "crit" => StatKey::Crit,
            "critDmg" => StatKey::CritDmg,
            "amp" => StatKey::Amp,
            "hp" => StatKey::Hp,
            "hpMult" => StatKey::HpMult,
            "armor" => StatKey::Armor,
            "mr" => StatKey::Mr,
            "manaRegen" => StatKey::ManaRegen,
            "manaPerAttack" => StatKey::ManaPerAttack,
            "manaPerCrit" => StatKey::ManaPerCrit,
            "omnivamp" => StatKey::Omnivamp,
            "durability" => StatKey::Durability,
            "adap" => StatKey::Adap,
            "adOrAp" => StatKey::AdOrAp,
            "startingMana" => StatKey::StartingMana,
            "ampVsTank" => StatKey::AmpVsTank,
            _ => return Err(pyo3::exceptions::PyValueError::new_err(format!("stat key {s:?}"))),
        })
    }
}

fn pairs(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Vec<(StatKey, f64)>> {
    let mut out = Vec::new();
    for p in getlist(d, key)? {
        let k: String = p.get_item(0)?.extract()?;
        let v: f64 = p.get_item(1)?.extract()?;
        out.push((StatKey::parse(&k)?, v));
    }
    Ok(out)
}

fn vecf(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<Vec<f64>>> {
    getvecf(d, key)
}

fn tuple2(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<(f64, f64)>> {
    Ok(vecf(d, key)?.map(|v| (v[0], v[1])))
}

fn tuple3(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<(f64, f64, f64)>> {
    Ok(vecf(d, key)?.map(|v| (v[0], v[1], v[2])))
}

/// One pool item, resolved (tft.item_spec).
#[derive(Clone, Debug, Default)]
pub struct ItemFx {
    pub api: String,
    pub name: String,
    pub unique: bool,
    pub stats: Vec<(StatKey, f64)>,
    pub precision: i64,
    pub amp_vs_tank: Option<f64>,
    pub as_per_second: Option<(f64, Option<f64>)>,
    pub ad_per_attack: Option<(f64, f64, f64)>,
    pub adap_per_attack: Option<(f64, f64, f64)>,
    pub ap_per_interval: Option<(f64, f64)>,
    pub ap_after: Option<(f64, f64)>,
    pub amp_per_crit: Option<(f64, f64, f64)>,
    pub mana_per_attack: Option<f64>,
    pub mana_per_crit: Option<f64>,
    pub mana_mult: Option<f64>,
    pub adap_mult: Option<f64>,
    pub starting_mana: Option<f64>,
    pub adds: Vec<(StatKey, f64)>,
    pub sunder_on_hit: Option<(f64, f64)>,
    pub shred_on_hit: Option<(f64, f64)>,
    pub burn_on_hit: Option<(f64, f64)>,
    pub sunder_aura: Option<f64>,
    pub shred_aura: Option<f64>,
    pub burn_aura: Option<(f64, f64)>,
    pub sunder_aura_by_range: Option<(f64, f64)>,
    pub shred_aura_by_range: Option<(f64, f64)>,
    pub burn_aura_by_range: Option<(f64, f64, f64)>,
    pub ionic_spark_by_range: Option<(f64, f64)>,
    pub hp_mult: Option<f64>,
    pub durability: Option<f64>,
    pub durability_by_health: Option<(f64, f64, f64)>,
    pub attack_damage_taken: Option<f64>,
    pub thorns: Option<(f64, f64)>,
    pub resists_per_attacker: Option<(f64, f64)>,
    pub heal_per_interval: Option<(f64, f64)>,
    pub regen_missing_pct: Option<f64>,
    pub shield_at_hp: Option<(f64, f64, f64, bool)>,
    pub shield_at_start: Option<(f64, f64)>,
    pub resists_at_start: Option<(f64, f64, f64)>,
    pub untargetable_at_hp: Option<(f64, f64, f64)>,
    pub mana_at_hp: Option<(f64, f64)>,
    pub adap_per_hit: bool,
    pub ionic_spark: Option<f64>,
    pub ally_heal_pct: Option<f64>,
    pub cc_immune_duration: Option<f64>,
    pub unstoppable_at_max_stacks: bool,
    pub hoj: Option<(f64, f64, f64, f64)>,
    pub note: Option<String>,
}

impl ItemFx {
    pub fn from_py(d: &Bound<'_, PyDict>) -> PyResult<ItemFx> {
        let shield = match vecf(d, "shieldAtHp")? {
            Some(v) => Some((v[0], v[1], v[2], v[3] != 0.0)),
            None => None,
        };
        let asps = match get(d, "asPerSecond")? {
            Some(v) => {
                let pct: f64 = v.get_item(0)?.extract()?;
                let until = v.get_item(1)?;
                let until = if until.is_none() { None } else { Some(until.extract::<f64>()?) };
                Some((pct, until))
            }
            None => None,
        };
        Ok(ItemFx {
            api: gets(d, "api", "")?,
            name: gets(d, "name", "")?,
            unique: truthy(d, "unique")?,
            stats: pairs(d, "stats")?,
            precision: geti(d, "precision", 0)?,
            amp_vs_tank: getopt(d, "ampVsTank")?,
            as_per_second: asps,
            ad_per_attack: tuple3(d, "adPerAttack")?,
            adap_per_attack: tuple3(d, "adapPerAttack")?,
            ap_per_interval: tuple2(d, "apPerInterval")?,
            ap_after: tuple2(d, "apAfter")?,
            amp_per_crit: tuple3(d, "ampPerCrit")?,
            mana_per_attack: getopt(d, "manaPerAttack")?,
            mana_per_crit: getopt(d, "manaPerCrit")?,
            mana_mult: getopt(d, "manaMult")?,
            adap_mult: getopt(d, "adapMult")?,
            starting_mana: getopt(d, "startingMana")?,
            adds: pairs(d, "adds")?,
            sunder_on_hit: tuple2(d, "sunderOnHit")?,
            shred_on_hit: tuple2(d, "shredOnHit")?,
            burn_on_hit: tuple2(d, "burnOnHit")?,
            sunder_aura: getopt(d, "sunderAura")?,
            shred_aura: getopt(d, "shredAura")?,
            burn_aura: tuple2(d, "burnAura")?,
            sunder_aura_by_range: range_pair(d, "sunderAuraByRange", true)?,
            shred_aura_by_range: range_pair(d, "shredAuraByRange", true)?,
            burn_aura_by_range: range_burn(d)?,
            ionic_spark_by_range: range_pair(d, "ionicSparkByRange", false)?,
            hp_mult: getopt(d, "hpMult")?,
            durability: getopt(d, "durability")?,
            durability_by_health: tuple3(d, "durabilityByHealth")?,
            attack_damage_taken: getopt(d, "attackDamageTaken")?,
            thorns: tuple2(d, "thorns")?,
            resists_per_attacker: tuple2(d, "resistsPerAttacker")?,
            heal_per_interval: tuple2(d, "healPerInterval")?,
            regen_missing_pct: getopt(d, "regenMissingPct")?,
            shield_at_hp: shield,
            shield_at_start: tuple2(d, "shieldAtStart")?,
            resists_at_start: tuple3(d, "resistsAtStart")?,
            untargetable_at_hp: tuple3(d, "untargetableAtHp")?,
            mana_at_hp: tuple2(d, "manaAtHp")?,
            adap_per_hit: truthy(d, "adapPerHit")?,
            ionic_spark: getopt(d, "ionicSpark")?,
            ally_heal_pct: getopt(d, "allyHealPct")?,
            cc_immune_duration: getopt(d, "ccImmuneDuration")?,
            unstoppable_at_max_stacks: truthy(d, "unstoppableAtMaxStacks")?,
            hoj: match vecf(d, "hoj")? {
                Some(v) => Some((v[0], v[1], v[2], v[3])),
                None => None,
            },
            note: match get(d, "note")? {
                Some(v) => Some(v.extract()?),
                None => None,
            },
        })
    }
}

fn getopt(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<f64>> {
    match get(d, key)? {
        Some(v) => Ok(Some(v.extract()?)),
        None => Ok(None),
    }
}

fn range_pair(d: &Bound<'_, PyDict>, key: &str, fraction: bool) -> PyResult<Option<(f64, f64)>> {
    match vecf(d, key)? {
        None => Ok(None),
        Some(values) if values.len() == 2 && values.iter().all(|v| v.is_finite() && *v >= 0.0)
            && (!fraction || values[0] <= 1.0) => Ok(Some((values[0], values[1]))),
        Some(_) => Err(pyo3::exceptions::PyValueError::new_err(format!("{key}: invalid effect/range pair"))),
    }
}

fn range_burn(d: &Bound<'_, PyDict>) -> PyResult<Option<(f64, f64, f64)>> {
    match vecf(d, "burnAuraByRange")? {
        None => Ok(None),
        Some(values) if values.len() == 3 && values.iter().all(|v| v.is_finite() && *v >= 0.0)
            && values[0] <= 1.0 && values[1] > 0.0 => Ok(Some((values[0], values[1], values[2]))),
        Some(_) => Err(pyo3::exceptions::PyValueError::new_err("burnAuraByRange: invalid rate/duration/range")),
    }
}

/// The Summoner trait's rows, for the drivers whose units summon.
#[derive(Clone, Copy, Debug, Default)]
pub struct Summoner {
    pub damage_mult: Option<f64>,
    pub health_mult: Option<f64>,
    pub extra_summons: Option<f64>,
    pub extra_attacks: Option<f64>,
    pub summon_power: Option<f64>,
}

/// One trait at one breakpoint, resolved (tft.trait_spec).
/// Delayed or recurring combat stat grants. These use the same stat units
/// as opening traits; interval zero means a single grant.
#[derive(Clone, Copy, Debug, Default)]
pub struct TimedStats {
    pub after: f64,
    pub interval: f64,
    pub ad_pct: f64,
    pub ap: f64,
    pub as_pct: f64,
    pub armor: f64,
    pub mr: f64,
    pub hp: f64,
    pub mana_regen: f64,
}

impl TimedStats {
    fn from_py(d: &Bound<'_, PyDict>) -> PyResult<Self> {
        let mut result = Self { after: getf(d, "after", 0.0)?, interval: getf(d, "interval", 0.0)?,
                                ..Self::default() };
        if !result.after.is_finite() || result.after <= 0.0
            || !result.interval.is_finite() || result.interval < 0.0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "timedStats requires a positive finite delay and nonnegative finite interval"));
        }
        for (key, value) in pairs(d, "stats")? {
            if !value.is_finite() || value < 0.0 {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "timedStats grants must be finite and nonnegative"));
            }
            match key {
                StatKey::AdPct => result.ad_pct += value,
                StatKey::Ap => result.ap += value,
                StatKey::AsPct => result.as_pct += value,
                StatKey::Armor => result.armor += value,
                StatKey::Mr => result.mr += value,
                StatKey::Hp => result.hp += value,
                StatKey::ManaRegen => result.mana_regen += value,
                _ => return Err(pyo3::exceptions::PyValueError::new_err("unsupported timedStats stat")),
            }
        }
        Ok(result)
    }
}

#[derive(Clone, Debug, Default)]
pub struct TraitFx {
    #[allow(dead_code)]
    pub api: String,
    pub name: String,
    pub stats: Vec<(StatKey, f64)>,
    pub timed_stats: Vec<TimedStats>,
    pub precision: bool,
    pub as_per_attack_stack: Option<(f64, f64)>,
    pub ap_per_cast: Option<f64>,
    pub amp_after_same_target: Option<(f64, f64)>,
    pub bleed: Option<(f64, f64)>,
    pub burn_on_hit: Option<(f64, f64)>,
    pub bonus_magic_pct: Option<f64>,
    pub bonus_true_pct: Option<f64>,
    pub ravager: Option<(f64, f64, f64)>,
    pub pixies: Option<f64>,
    pub riftbeast: bool,
    pub durability: Option<f64>,
    pub durability_while_shielded: Option<f64>,
    pub shield_at_start: Option<(f64, f64)>,
    pub shield_at_hp: Option<(f64, f64, f64)>,
    pub resists_per_attacker: Option<(f64, f64)>,
    pub omnivamp: Option<f64>,
    pub execute_below_hp: Option<f64>,
    pub heal_per_interval: Option<(f64, f64)>,
    pub takedown: Option<(f64, f64)>,
    pub fae_heal: Option<(f64, f64)>,
    pub summoner: Option<Summoner>,
    pub caustic: Option<(f64, f64)>,
    pub note: Option<String>,
}

impl TraitFx {
    pub fn from_py(d: &Bound<'_, PyDict>) -> PyResult<TraitFx> {
        let bonus_true_pct: Option<f64> = getopt(d, "bonusTruePct")?;
        if bonus_true_pct.is_some_and(|value| !value.is_finite() || value < 0.0) {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "bonusTruePct requires a finite nonnegative fraction"));
        }
        let execute_below_hp: Option<f64> = getopt(d, "executeBelowHp")?;
        if execute_below_hp.is_some_and(|value| !value.is_finite() || !(0.0..=1.0).contains(&value)) {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "executeBelowHp requires a finite fraction between 0 and 1"));
        }
        let heal_per_interval = tuple2(d, "healPerInterval")?;
        if heal_per_interval.is_some_and(|(fraction, interval)|
            !fraction.is_finite() || !(0.0..=1.0).contains(&fraction)
                || !interval.is_finite() || interval <= 0.0) {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "trait healPerInterval requires a fraction between 0 and 1 and a positive finite interval"));
        }
        let summoner = match getd(d, "summoner")? {
            Some(s) => Some(Summoner {
                damage_mult: getopt(&s, "damageMult")?,
                health_mult: getopt(&s, "healthMult")?,
                extra_summons: getopt(&s, "extraSummons")?,
                extra_attacks: getopt(&s, "extraAttacks")?,
                summon_power: getopt(&s, "summonPower")?,
            }),
            None => None,
        };
        Ok(TraitFx {
            api: gets(d, "api", "")?,
            name: gets(d, "name", "")?,
            stats: pairs(d, "stats")?,
            timed_stats: getlist(d, "timedStats")?.iter()
                .map(|value| TimedStats::from_py(&dict_of(value)?)).collect::<PyResult<_>>()?,
            precision: truthy(d, "precision")?,
            as_per_attack_stack: tuple2(d, "asPerAttackStack")?,
            ap_per_cast: getopt(d, "apPerCast")?,
            amp_after_same_target: tuple2(d, "ampAfterSameTarget")?,
            bleed: tuple2(d, "bleed")?,
            burn_on_hit: tuple2(d, "burnOnHit")?,
            bonus_magic_pct: getopt(d, "bonusMagicPct")?,
            bonus_true_pct,
            ravager: tuple3(d, "ravager")?,
            pixies: getopt(d, "pixies")?,
            riftbeast: truthy(d, "riftbeast")?,
            durability: getopt(d, "durability")?,
            durability_while_shielded: getopt(d, "durabilityWhileShielded")?,
            shield_at_start: tuple2(d, "shieldAtStart")?,
            shield_at_hp: tuple3(d, "shieldAtHp")?,
            resists_per_attacker: tuple2(d, "resistsPerAttacker")?,
            omnivamp: getopt(d, "omnivamp")?,
            execute_below_hp,
            heal_per_interval,
            takedown: tuple2(d, "takedown")?,
            fae_heal: tuple2(d, "faeHeal")?,
            summoner,
            caustic: tuple2(d, "caustic")?,
            note: match get(d, "note")? {
                Some(v) => Some(v.extract()?),
                None => None,
            },
        })
    }
}

/// The role's own contribution (tft.build_fx's first lines).
#[derive(Clone, Copy, Debug, Default)]
pub struct RoleFx {
    pub mana_regen: f64,
    pub as_pct: f64,
}

/// tft.Fx: the composed effects of one build.
#[derive(Clone, Debug)]
pub struct Fx {
    pub ad_pct: f64,
    pub ap: f64,
    pub as_pct: f64,
    pub crit: f64,
    pub crit_dmg: f64,
    pub amp: f64,
    pub hp: f64,
    /// Ordinary item and trait HP bonuses share one additive percentage pool.
    /// Inputs retain factor notation (1.18 means +18%); this is 1 + their sum.
    pub hp_mult: f64,
    pub armor: f64,
    pub mr: f64,
    pub mana_regen: f64,
    pub timed_stats: Vec<TimedStats>,
    pub mana_per_attack: f64,
    pub mana_per_crit: f64,
    pub mana_mult: f64,
    pub adap_mult: f64,
    pub starting_mana: f64,
    pub precision: i64,
    pub amp_vs_tank: f64,
    pub as_per_second: Vec<(f64, Option<f64>)>,
    pub ad_per_attack: Vec<(f64, f64, f64)>,
    pub adap_per_attack: Vec<(f64, f64, f64)>,
    pub ap_per_interval: Vec<(f64, f64)>,
    pub ap_after: Vec<(f64, f64)>,
    pub amp_per_crit: Vec<(f64, f64, f64)>,
    pub as_per_attack_stack: Vec<(f64, f64)>,
    pub ap_per_cast: f64,
    pub spellweaver_ap_per_cast: f64,
    pub sunder_on_hit: Vec<(f64, f64)>,
    pub shred_on_hit: Vec<(f64, f64)>,
    /// (pct of max hp per second, duration, stacks with the item burn?)
    pub burn_on_hit: Vec<(f64, f64, bool)>,
    pub sunder_aura: f64,
    pub shred_aura: f64,
    pub burn_aura: Option<(f64, f64)>,
    pub amp_after_same_target: Option<(f64, f64)>,
    pub bleed_pct: f64,
    pub bleed_dur: f64,
    pub bonus_magic_pct: f64,
    pub bonus_true_pct: f64,
    pub ravager: Option<(f64, f64, f64)>,
    pub riftbeast: bool,
    pub form: Option<Form>,
    pub omnivamp: f64,
    pub durabilities: Vec<f64>,
    pub durability_while_shielded: Vec<f64>,
    pub durability_by_health: Vec<(f64, f64, f64)>,
    pub attack_damage_taken: f64,
    pub thorns: Vec<(f64, f64)>,
    pub resists_per_attacker: [f64; 2],
    pub heal_per_interval: Vec<(f64, f64)>,
    pub heal_interval_sources: Vec<&'static str>,
    pub execute_below_hp: f64,
    pub regen_missing_pct: f64,
    pub shield_at_hp: Vec<(f64, f64, f64, bool)>,
    pub shield_at_start: Vec<(f64, f64)>,
    pub resists_at_start: Vec<(f64, f64, f64)>,
    pub untargetable_at_hp: Vec<(f64, f64, f64)>,
    pub mana_at_hp: Vec<(f64, f64)>,
    pub adap_per_hit: bool,
    pub ionic_spark: f64,
    pub ally_heal_pct: f64,
    pub cc_immune_duration: f64,
    pub unstoppable_at_max_stacks: bool,
    pub hojs: Vec<(f64, f64, f64, f64)>,
    pub heal_on_takedown: f64,
    pub mana_on_takedown: f64,
    pub fae_heal: Option<(f64, f64)>,
    pub summoner: Option<Summoner>,
    pub caustic: Option<(f64, f64)>,
    pub notes: Vec<String>,
}

impl Default for Fx {
    fn default() -> Fx {
        Fx {
            ad_pct: 0.0, ap: 0.0, as_pct: 0.0, crit: 0.0, crit_dmg: 0.0, amp: 0.0, hp: 0.0,
            hp_mult: 1.0, armor: 0.0, mr: 0.0, mana_regen: 0.0, mana_per_attack: 0.0,
            timed_stats: Vec::new(),
            mana_per_crit: 0.0, mana_mult: 1.0, adap_mult: 1.0, starting_mana: 0.0,
            precision: 0, amp_vs_tank: 0.0,
            as_per_second: Vec::new(), ad_per_attack: Vec::new(), adap_per_attack: Vec::new(),
            ap_per_interval: Vec::new(), ap_after: Vec::new(), amp_per_crit: Vec::new(),
            as_per_attack_stack: Vec::new(), ap_per_cast: 0.0, spellweaver_ap_per_cast: 0.0,
            sunder_on_hit: Vec::new(), shred_on_hit: Vec::new(), burn_on_hit: Vec::new(),
            sunder_aura: 0.0, shred_aura: 0.0, burn_aura: None, amp_after_same_target: None,
            bleed_pct: 0.0, bleed_dur: 0.0, bonus_magic_pct: 0.0, bonus_true_pct: 0.0, ravager: None,
            riftbeast: false, form: None,
            omnivamp: 0.0, durabilities: Vec::new(), durability_while_shielded: Vec::new(),
            durability_by_health: Vec::new(),
            attack_damage_taken: 1.0, thorns: Vec::new(), resists_per_attacker: [0.0, 0.0],
            heal_per_interval: Vec::new(), regen_missing_pct: 0.0, shield_at_hp: Vec::new(),
            heal_interval_sources: Vec::new(), execute_below_hp: 0.0,
            shield_at_start: Vec::new(), resists_at_start: Vec::new(),
            untargetable_at_hp: Vec::new(), mana_at_hp: Vec::new(), adap_per_hit: false,
            ionic_spark: 0.0, ally_heal_pct: 0.0, hojs: Vec::new(), heal_on_takedown: 0.0,
            cc_immune_duration: 0.0, unstoppable_at_max_stacks: false,
            mana_on_takedown: 0.0, fae_heal: None, summoner: None, caustic: None,
            notes: Vec::new(),
        }
    }
}

/// tft.combined_durability: sources stack multiplicatively.
pub fn combined_durability(fractions: impl Iterator<Item = f64>) -> f64 {
    let mut out = 1.0;
    for d in fractions {
        out *= 1.0 - pymax(0.0, d);
    }
    1.0 - out
}

impl Fx {
    /// Fx.add_stats / the generic `setattr(fx, k, getattr(fx, k) + v)`.
    fn add(&mut self, key: StatKey, v: f64, unit_attack: bool) {
        match key {
            StatKey::AdPct => self.ad_pct += v,
            StatKey::Ap => self.ap += v,
            StatKey::AsPct => self.as_pct += v,
            StatKey::Crit => self.crit += v,
            StatKey::CritDmg => self.crit_dmg += v,
            StatKey::Amp => self.amp += v,
            StatKey::Hp => self.hp += v,
            StatKey::HpMult => self.hp_mult += v - 1.0,
            StatKey::Armor => self.armor += v,
            StatKey::Mr => self.mr += v,
            StatKey::ManaRegen => self.mana_regen += v,
            StatKey::ManaPerAttack => self.mana_per_attack += v,
            StatKey::ManaPerCrit => self.mana_per_crit += v,
            StatKey::Omnivamp => self.omnivamp += v,
            StatKey::Durability => self.durabilities.push(v),
            StatKey::StartingMana => self.starting_mana += v,
            StatKey::AmpVsTank => self.amp_vs_tank += v,
            StatKey::Adap => {
                self.ad_pct += v;
                self.ap += v * 100.0;
            }
            StatKey::AdOrAp => {
                let form = match self.form {
                    Some(f) => f,
                    None => if unit_attack { Form::AD } else { Form::AP },
                };
                if form == Form::AD {
                    self.ad_pct += v;
                } else {
                    self.ap += v * 100.0;
                }
            }
        }
    }

    /// tft.apply_item, in its order of operations.
    pub fn apply_item(&mut self, it: &ItemFx) {
        for &(k, v) in &it.stats {
            self.add(k, v * 1.0, false);
        }
        if it.precision != 0 {
            self.precision += it.precision;
        }
        if let Some(v) = it.amp_vs_tank {
            self.amp_vs_tank += v;
        }
        if let Some(x) = it.as_per_second {
            self.as_per_second.push(x);
        }
        if let Some(x) = it.ad_per_attack {
            self.ad_per_attack.push(x);
        }
        if let Some(x) = it.adap_per_attack {
            self.adap_per_attack.push(x);
        }
        if let Some(x) = it.ap_per_interval {
            self.ap_per_interval.push(x);
        }
        if let Some(x) = it.ap_after {
            self.ap_after.push(x);
        }
        if let Some(x) = it.amp_per_crit {
            self.amp_per_crit.push(x);
        }
        if let Some(v) = it.mana_per_attack {
            self.mana_per_attack += v;
        }
        if let Some(v) = it.mana_per_crit {
            self.mana_per_crit += v;
        }
        if let Some(v) = it.mana_mult {
            self.mana_mult *= v;
        }
        if let Some(v) = it.adap_mult {
            self.adap_mult *= v;
        }
        if let Some(v) = it.starting_mana {
            self.starting_mana += v;
        }
        for &(k, v) in &it.adds {
            self.add(k, v, false);
        }
        if let Some(x) = it.sunder_on_hit {
            self.sunder_on_hit.push(x);
        }
        if let Some(x) = it.shred_on_hit {
            self.shred_on_hit.push(x);
        }
        if let Some((pct, dur)) = it.burn_on_hit {
            self.burn_on_hit.push((pct, dur, false));
        }
        if let Some(v) = it.sunder_aura {
            self.sunder_aura = pymax(self.sunder_aura, v);
        }
        if let Some(v) = it.shred_aura {
            self.shred_aura = pymax(self.shred_aura, v);
        }
        if let Some(x) = it.burn_aura {
            self.burn_aura = Some(x);
        }
        if let Some(v) = it.hp_mult {
            self.add(StatKey::HpMult, v, false);
        }
        if let Some(v) = it.durability {
            self.durabilities.push(v);
        }
        if let Some(x) = it.durability_by_health {
            self.durability_by_health.push(x);
        }
        if let Some(v) = it.attack_damage_taken {
            self.attack_damage_taken *= v;
        }
        if let Some(x) = it.thorns {
            self.thorns.push(x);
        }
        if let Some((a, m)) = it.resists_per_attacker {
            self.resists_per_attacker[0] += a;
            self.resists_per_attacker[1] += m;
        }
        if let Some(x) = it.heal_per_interval {
            self.heal_per_interval.push(x);
            self.heal_interval_sources.push("dragon's claw");
        }
        if let Some(v) = it.regen_missing_pct {
            self.regen_missing_pct += v;
        }
        if let Some(x) = it.shield_at_hp {
            self.shield_at_hp.push(x);
        }
        if let Some(x) = it.shield_at_start {
            self.shield_at_start.push(x);
        }
        if let Some(x) = it.resists_at_start {
            self.resists_at_start.push(x);
        }
        if let Some(x) = it.untargetable_at_hp {
            self.untargetable_at_hp.push(x);
        }
        if let Some(x) = it.mana_at_hp {
            self.mana_at_hp.push(x);
        }
        if it.adap_per_hit {
            self.adap_per_hit = true;
        }
        if let Some(v) = it.ionic_spark {
            self.ionic_spark += v;
        }
        if let Some(v) = it.ally_heal_pct {
            self.ally_heal_pct += v;
        }
        if let Some(duration) = it.cc_immune_duration {
            self.cc_immune_duration = pymax(self.cc_immune_duration, duration);
        }
        self.unstoppable_at_max_stacks |= it.unstoppable_at_max_stacks;
        if let Some(x) = it.hoj {
            self.hojs.push(x);
        }
        if let Some(n) = &it.note {
            self.notes.push(format!("{}: {}", it.name, n));
        }
    }

    /// tft.apply_trait, in its order of operations.
    pub fn apply_trait(&mut self, t: &TraitFx, unit_attack: bool) {
        for &(k, v) in &t.stats {
            self.add(k, v, unit_attack);
        }
        self.timed_stats.extend_from_slice(&t.timed_stats);
        if t.precision {
            self.precision += 1;
        }
        if let Some(x) = t.as_per_attack_stack {
            self.as_per_attack_stack.push(x);
        }
        if let Some(v) = t.ap_per_cast {
            self.ap_per_cast += v;
            if t.api == "DA_18_Spellweaver" {
                self.spellweaver_ap_per_cast += v;
            }
        }
        if let Some(x) = t.amp_after_same_target {
            self.amp_after_same_target = Some(x);
        }
        if let Some((pct, dur)) = t.bleed {
            self.bleed_pct = pymax(self.bleed_pct, pct);
            self.bleed_dur = dur;
        }
        if let Some((pct, dur)) = t.burn_on_hit {
            self.burn_on_hit.push((pct, dur, true));
        }
        if let Some(v) = t.bonus_magic_pct {
            self.bonus_magic_pct += v;
        }
        if let Some(v) = t.bonus_true_pct {
            self.bonus_true_pct += v;
        }
        if let Some(x) = t.ravager {
            self.ravager = Some(x);
        }
        if let Some(v) = t.pixies {
            self.ad_pct += v;
            self.ap += v * 100.0;
        }
        if t.riftbeast {
            self.riftbeast = true;
        }
        if let Some(v) = t.durability {
            self.durabilities.push(v);
        }
        if let Some(v) = t.durability_while_shielded.filter(|v| *v > 0.0) {
            self.durability_while_shielded.push(v);
        }
        if let Some(x) = t.shield_at_start {
            self.shield_at_start.push(x);
        }
        if let Some((thr, pct, dur)) = t.shield_at_hp {
            self.shield_at_hp.push((thr, pct, dur, false));
        }
        if let Some((a, m)) = t.resists_per_attacker {
            self.resists_per_attacker[0] += a;
            self.resists_per_attacker[1] += m;
        }
        if let Some(v) = t.omnivamp {
            self.omnivamp += v;
        }
        if let Some(value) = t.execute_below_hp {
            self.execute_below_hp = pymax(self.execute_below_hp, value);
        }
        if let Some(heal) = t.heal_per_interval {
            self.heal_per_interval.push(heal);
            self.heal_interval_sources.push(if t.api == "DA_Primal18" { "primal turtle" } else { "trait healing" });
        }
        if let Some((heal, mana)) = t.takedown {
            self.heal_on_takedown += heal;
            self.mana_on_takedown += mana;
        }
        if let Some(x) = t.fae_heal {
            self.fae_heal = Some(x);
        }
        if let Some(s) = t.summoner {
            self.summoner = Some(s);
        }
        if let Some(x) = t.caustic {
            self.caustic = Some(x);
        }
        if let Some(n) = &t.note {
            self.notes.push(format!("{}: {}", t.name, n));
        }
    }

    /// Fx.durability: the composed value before any fight-time buff.
    pub fn durability(&self) -> f64 {
        combined_durability(self.durabilities.iter().copied()
            .chain(self.durability_by_health.iter().map(|(low, _, _)| *low)))
    }

    /// The Summoner rows with Python's `.get(key, default)` reading.
    pub fn summoner_get(&self, pick: fn(&Summoner) -> Option<f64>, default: f64) -> f64 {
        match &self.summoner {
            Some(s) => pick(s).unwrap_or(default),
            None => default,
        }
    }
}

/// tft.adaptor_form: which form an Adaptor fights in, from the bonus attack
/// damage (a fraction) against the bonus ability power (per 100), the
/// role's damage type breaking the tie.
pub fn adaptor_form(has_forms: bool, unit_attack: bool, fx: &Fx) -> Option<Form> {
    if !has_forms {
        return None;
    }
    let (ad, ap) = (fx.ad_pct, fx.ap / 100.0);
    if ad > ap + 1e-9 {
        return Some(Form::AD);
    }
    if ap > ad + 1e-9 {
        return Some(Form::AP);
    }
    Some(if unit_attack { Form::AD } else { Form::AP })
}

/// tft.build_fx: role, items in build order, the Adaptor's form, traits.
pub fn build_fx(role: RoleFx, items: &[&ItemFx], traits: &[TraitFx], has_forms: bool,
                unit_attack: bool) -> Fx {
    let mut fx = Fx::default();
    if role.mana_regen != 0.0 {
        fx.mana_regen += role.mana_regen;
    }
    if role.as_pct != 0.0 {
        fx.as_pct += role.as_pct;
    }
    for it in items {
        fx.apply_item(it);
    }
    fx.form = adaptor_form(has_forms, unit_attack, &fx);
    for t in traits {
        fx.apply_trait(t, unit_attack);
    }
    fx
}

/// Form-dependent aura reach is resolved only after all item stats select
/// the equipped form. Ordinary pre-resolved item effects remain unchanged.
pub(crate) fn build_fx_for(spec: &crate::spec::CellSpec, items: &[&ItemFx]) -> Fx {
    let mut fx = build_fx(spec.role, items, &spec.traits, spec.unit.has_forms, spec.unit.attack);
    let range = spec.range_for(fx.form);
    for item in items {
        if let Some((value, reach)) = item.sunder_aura_by_range {
            if range <= reach { fx.sunder_aura = pymax(fx.sunder_aura, value); }
        }
        if let Some((value, reach)) = item.shred_aura_by_range {
            if range <= reach { fx.shred_aura = pymax(fx.shred_aura, value); }
        }
        if let Some((rate, duration, reach)) = item.burn_aura_by_range {
            if range <= reach { fx.burn_aura = Some((rate, duration)); }
        }
        if let Some((value, reach)) = item.ionic_spark_by_range {
            if range <= reach { fx.ionic_spark += value; }
        }
    }
    fx
}
