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
#[derive(Clone, Debug, Default)]
pub struct TraitFx {
    #[allow(dead_code)]
    pub api: String,
    pub name: String,
    pub stats: Vec<(StatKey, f64)>,
    pub precision: bool,
    pub as_per_attack_stack: Option<(f64, f64)>,
    pub ap_per_cast: Option<f64>,
    pub amp_after_same_target: Option<(f64, f64)>,
    pub bleed: Option<(f64, f64)>,
    pub burn_on_hit: Option<(f64, f64)>,
    pub bonus_magic_pct: Option<f64>,
    pub ravager: Option<(f64, f64, f64)>,
    pub pixies: Option<f64>,
    pub riftbeast: bool,
    pub durability: Option<f64>,
    pub shield_at_start: Option<(f64, f64)>,
    pub shield_at_hp: Option<(f64, f64, f64)>,
    pub resists_per_attacker: Option<(f64, f64)>,
    pub omnivamp: Option<f64>,
    pub takedown: Option<(f64, f64)>,
    pub fae_heal: Option<(f64, f64)>,
    pub summoner: Option<Summoner>,
    pub caustic: Option<(f64, f64)>,
    pub note: Option<String>,
}

impl TraitFx {
    pub fn from_py(d: &Bound<'_, PyDict>) -> PyResult<TraitFx> {
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
            precision: truthy(d, "precision")?,
            as_per_attack_stack: tuple2(d, "asPerAttackStack")?,
            ap_per_cast: getopt(d, "apPerCast")?,
            amp_after_same_target: tuple2(d, "ampAfterSameTarget")?,
            bleed: tuple2(d, "bleed")?,
            burn_on_hit: tuple2(d, "burnOnHit")?,
            bonus_magic_pct: getopt(d, "bonusMagicPct")?,
            ravager: tuple3(d, "ravager")?,
            pixies: getopt(d, "pixies")?,
            riftbeast: truthy(d, "riftbeast")?,
            durability: getopt(d, "durability")?,
            shield_at_start: tuple2(d, "shieldAtStart")?,
            shield_at_hp: tuple3(d, "shieldAtHp")?,
            resists_per_attacker: tuple2(d, "resistsPerAttacker")?,
            omnivamp: getopt(d, "omnivamp")?,
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
    pub hp_mult: f64,
    pub armor: f64,
    pub mr: f64,
    pub mana_regen: f64,
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
    pub ravager: Option<(f64, f64, f64)>,
    pub riftbeast: bool,
    pub form: Option<Form>,
    pub omnivamp: f64,
    pub durabilities: Vec<f64>,
    pub durability_by_health: Vec<(f64, f64, f64)>,
    pub attack_damage_taken: f64,
    pub thorns: Vec<(f64, f64)>,
    pub resists_per_attacker: [f64; 2],
    pub heal_per_interval: Vec<(f64, f64)>,
    pub regen_missing_pct: f64,
    pub shield_at_hp: Vec<(f64, f64, f64, bool)>,
    pub shield_at_start: Vec<(f64, f64)>,
    pub resists_at_start: Vec<(f64, f64, f64)>,
    pub untargetable_at_hp: Vec<(f64, f64, f64)>,
    pub mana_at_hp: Vec<(f64, f64)>,
    pub adap_per_hit: bool,
    pub ionic_spark: f64,
    pub ally_heal_pct: f64,
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
            mana_per_crit: 0.0, mana_mult: 1.0, adap_mult: 1.0, starting_mana: 0.0,
            precision: 0, amp_vs_tank: 0.0,
            as_per_second: Vec::new(), ad_per_attack: Vec::new(), adap_per_attack: Vec::new(),
            ap_per_interval: Vec::new(), ap_after: Vec::new(), amp_per_crit: Vec::new(),
            as_per_attack_stack: Vec::new(), ap_per_cast: 0.0,
            sunder_on_hit: Vec::new(), shred_on_hit: Vec::new(), burn_on_hit: Vec::new(),
            sunder_aura: 0.0, shred_aura: 0.0, burn_aura: None, amp_after_same_target: None,
            bleed_pct: 0.0, bleed_dur: 0.0, bonus_magic_pct: 0.0, ravager: None,
            riftbeast: false, form: None,
            omnivamp: 0.0, durabilities: Vec::new(), durability_by_health: Vec::new(),
            attack_damage_taken: 1.0, thorns: Vec::new(), resists_per_attacker: [0.0, 0.0],
            heal_per_interval: Vec::new(), regen_missing_pct: 0.0, shield_at_hp: Vec::new(),
            shield_at_start: Vec::new(), resists_at_start: Vec::new(),
            untargetable_at_hp: Vec::new(), mana_at_hp: Vec::new(), adap_per_hit: false,
            ionic_spark: 0.0, ally_heal_pct: 0.0, hojs: Vec::new(), heal_on_takedown: 0.0,
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
            StatKey::HpMult => self.hp_mult *= v,
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
            self.hp_mult *= v;
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
        if t.precision {
            self.precision += 1;
        }
        if let Some(x) = t.as_per_attack_stack {
            self.as_per_attack_stack.push(x);
        }
        if let Some(v) = t.ap_per_cast {
            self.ap_per_cast += v;
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
