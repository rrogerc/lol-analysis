//! Drivers of this slice: ported from tft_kits.py (see mod.rs for the list).

#![allow(unused_imports)]
use crate::driver::Driver;
use crate::drivers::helpers::*;
use crate::fight::{Deal, Fight, Sel};
use crate::fx::Form;
use crate::kit::{CalcId, DType, Kit, RowId, Runtime};
use crate::pyf::{pyint, pymax, pymin, pyround};
use crate::spec::UnitSpec;

/// Primordial Burst: one blast on the target, the bigger calc instead when
/// it stands below a share of its max health. Every kill permanently adds
/// ability power — the row is written as a share of the 100 ability power
/// every unit starts with, so it is added as that many points.
#[derive(Clone)]
pub struct Veigar {
    threshold: RowId,
    ap_on_kill: RowId,
    burst: CalcId,
    low: CalcId,
}

impl Driver for Veigar {
    const NAME: &'static str = "Veigar";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Veigar { threshold: k.row("HPThreshold"), ap_on_kill: k.row("APOnKill"),
                 burst: k.calc("MagicDamageCalc1"), low: k.calc("MagicDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let d = match f.target() {
            Some(d) => d,
            None => return,
        };
        if f.d(d).hp < f.row(f.drv.threshold) * f.d(d).max_hp {
            f.hit_ability(f.drv.low, Some(d), "low health", 1.0);
        } else {
            f.hit_ability(f.drv.burst, Some(d), "ability", 1.0);
        }
    }

    fn kill(f: &mut Fight<Self>, _target: usize) {
        f.ap_extra += f.row(f.drv.ap_on_kill);
    }
}

/// Fungus Among Us: two clusters of mushrooms over the nearest few, then a
/// giant one on the target. The two clusters are spelled out in Riot's text
/// and have no row of their own. Foraging is economy.
#[derive(Clone)]
pub struct Teemo {
    n_enemies: RowId,
    shroom: CalcId,
    giant: CalcId,
}

impl Driver for Teemo {
    const NAME: &'static str = "Teemo";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Teemo { n_enemies: k.row("NumEnemiesHit"), shroom: k.calc("MagicDamageCalc1"),
                giant: k.calc("MagicDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        for _ in 0..2 {
            let tg = f.aoe(Some(f.row(f.drv.n_enemies)), false);
            for d in tg.iter() {
                f.hit_ability(f.drv.shroom, Some(d), "mushrooms", 1.0);
            }
        }
        f.hit_ability(f.drv.giant, f.target(), "giant mushroom", 1.0);
    }
}

/// Rampant Growth: plants that spit at the nearest dummy a fixed number of
/// times each, one attack a second (their attack speed is nowhere in the
/// data). Summoner adds plants and attacks; her Thornmaiden durability is
/// the engine's.
#[derive(Clone)]
pub struct Zyra {
    /// One plant patch: (the second its next volley is due, volleys left,
    /// how many plants fire together).
    plants: Vec<(f64, i64, i64)>,
    n_plants: RowId,
    n_attacks: RowId,
    spit: CalcId,
}

impl Driver for Zyra {
    const NAME: &'static str = "Zyra";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Zyra { plants: Vec::new(), n_plants: k.row("NumPlantsToSpawn"),
               n_attacks: k.row("ThornSpitterNumAttacks"), spit: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let n = pyint(f.row(f.drv.n_plants))
            + (pyint(f.fx.summoner_get(|s| s.extra_summons, 1.0)) - 1).max(0);
        let shots = pyint(f.row(f.drv.n_attacks)
                          + f.fx.summoner_get(|s| s.extra_attacks, 0.0));
        let t = f.t;
        f.drv.plants.push((t, shots, n));
    }

    fn tick(f: &mut Fight<Self>) {
        let mut plants = std::mem::take(&mut f.drv.plants);
        for p in plants.iter_mut() {
            while p.1 > 0 && f.t >= p.0 - 1e-9 {
                for _ in 0..p.2 {
                    f.hit_ability(f.drv.spit, f.target(), "plants", 1.0);
                }
                p.0 += 1.0;
                p.1 -= 1;
            }
        }
        plants.retain(|p| p.1 > 0);
        f.drv.plants = plants;
    }
}

/// Triggerseed: a shield on several allies — counted, not simulated, and
/// able to crit with Precision — then magic damage around them. The
/// shield's calc resolves to nothing, so its amount is the curve row per
/// 100 ability power. The allies' damage amp and the attack speed after six
/// casts are ally buffs: not simulated.
#[derive(Clone)]
pub struct Ivern {
    amount: RowId,
    n_allies: RowId,
    burst: CalcId,
}

impl Driver for Ivern {
    const NAME: &'static str = "Ivern";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Ivern { amount: k.row("ShieldAmount"), n_allies: k.row("NumAlliesToShield"),
                burst: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let mut shield = f.row(f.drv.amount) * f.ap() / 100.0;
        if f.sheet.precision {
            shield *= f.sheet.crit_ev();
        }
        for _ in 0..pyint(f.row(f.drv.n_allies)) {
            f.shield_ally(shield);
        }
        let tg = f.aoe_all();
        for d in tg.iter() {
            f.hit_ability(f.drv.burst, Some(d), "ability", 1.0);
        }
    }
}

/// Razor Leaves: five leaves converging on the target for one total hit,
/// then a burn. With the Riftbeast Alpha Mark (Scarlet Buff) every cast adds
/// attack damage. Wound does nothing here: the dummies never heal.
#[derive(Clone)]
pub struct Cinderling {
    burn_amount: RowId,
    burn_duration: RowId,
    trait_ad: RowId,
    leaves: CalcId,
}

impl Driver for Cinderling {
    const NAME: &'static str = "Cinderling";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Cinderling { burn_amount: k.row("BurnAmount"), burn_duration: k.row("BurnDuration"),
                     trait_ad: k.row("TraitADOnCast"), leaves: k.calc("PhysicalDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let d = match f.target() {
            Some(d) => d,
            None => return,
        };
        f.hit_ability(f.drv.leaves, Some(d), "ability", 1.0);
        let pct = f.row(f.drv.burn_amount) / 100.0;
        let dur = f.row(f.drv.burn_duration);
        f.burn(d, pct, dur, false);
        if f.fx.riftbeast {
            f.ad_extra += f.row(f.drv.trait_ad);
        }
    }
}

/// Raining Artillery: acid on the target and the next nearest. The
/// attack-damage form hits a dummy below the health threshold with the
/// bigger calc; the ability-power form adds damage over time. Caustic's
/// shred and sunder ride the engine's on-hit effects.
#[derive(Clone)]
pub struct KogMaw {
    threshold: RowId,
    dot_duration: RowId,
    phys1: CalcId,
    phys2: CalcId,
    magic1: CalcId,
    magic2: CalcId,
}

impl Driver for KogMaw {
    const NAME: &'static str = "KogMaw";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        KogMaw { threshold: k.row("ADBonusDamageThreshold"), dot_duration: k.row("APBonusDOTDuration"),
                 phys1: k.calc("PhysicalDamageCalc1"), phys2: k.calc("PhysicalDamageCalc2"),
                 magic1: k.calc("MagicDamageCalc1"), magic2: k.calc("MagicDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let tg = f.aoe(Some(2.0), false);
        if f.sheet.form == Some(Form::AD) {
            let thr = f.row(f.drv.threshold);
            for d in tg.iter() {
                let low = f.d(d).hp < thr * f.d(d).max_hp;
                if low {
                    f.hit_ability(f.drv.phys2, Some(d), "low health", 1.0);
                } else {
                    f.hit_ability(f.drv.phys1, Some(d), "ability", 1.0);
                }
            }
            return;
        }
        for d in tg.iter() {
            f.hit_ability(f.drv.magic1, Some(d), "ability", 1.0);
            let dur = f.row(f.drv.dot_duration);
            f.dot_ability(f.drv.magic2, Some(d), dur, "acid", 1.0);
        }
    }
}

/// Flock Family: the cast summons the beaks for a few seconds (a duration
/// that scales with ability power in the data); while they stand, every
/// attack of hers lands the whole flock's damage on the same target. With
/// the Riftbeast Alpha Mark (Orange Buff) each physical hit — hers and every
/// beak's — strips flat armor.
#[derive(Clone)]
pub struct MamaBeak {
    beaks_until: f64,
    n_minis: RowId,
    armor_reduc: RowId,
    duration: CalcId,
    beaks: CalcId,
}

impl Driver for MamaBeak {
    const NAME: &'static str = "MamaBeak";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        MamaBeak { beaks_until: 0.0, n_minis: k.row("NumMinisToSpawn"),
                   armor_reduc: k.row("ArmorReduc"), duration: k.calc("GenericCalc1"),
                   beaks: k.calc("PhysicalDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let until = f.t + f.calc(f.drv.duration);
        f.drv.beaks_until = until;
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        f.hit_attack(target, 1.0, "auto");
        let mut hits: i64 = 1;
        if f.t < f.drv.beaks_until {
            let n = pyint(f.row(f.drv.n_minis));
            let mult = (n as f64) * f.fx.summoner_get(|s| s.damage_mult, 1.0);
            f.hit_ability(f.drv.beaks, Some(target), "tiny beaks", mult);
            hits += n;
        }
        if f.fx.riftbeast {
            let cut = f.row(f.drv.armor_reduc) * (hits as f64);
            f.dm(target).armor_flat += cut;
        }
    }
}

/// Arise!: attack speed and soldiers for the next few attacks, each of which
/// becomes a command — the basic attack is replaced by every soldier's
/// strike, its on-hit effects still landing. Summoner adds a soldier and
/// multiplies their damage.
#[derive(Clone)]
pub struct Azir {
    commands: i64,
    soldiers: i64,
    n_attacks: RowId,
    n_soldiers: RowId,
    attack_speed: RowId,
    strike: CalcId,
}

impl Driver for Azir {
    const NAME: &'static str = "Azir";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Azir { commands: 0, soldiers: 0, n_attacks: k.row("NumAttacks"),
               n_soldiers: k.row("SoldiersToSpawn"), attack_speed: k.row("AttackSpeed"),
               strike: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let commands = pyint(f.row(f.drv.n_attacks));
        let soldiers = pyint(f.row(f.drv.n_soldiers))
            + (pyint(f.fx.summoner_get(|s| s.extra_summons, 1.0)) - 1).max(0);
        f.drv.commands = commands;
        f.drv.soldiers = soldiers;
        f.as_extra = f.row(f.drv.attack_speed) - 1.0;
        f.as_extra_until = 1e9;
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        let n = f.drv.commands;
        if n <= 0 {
            f.hit_attack(target, 1.0, "auto");
            return;
        }
        f.drv.commands = n - 1;
        let mult = (f.drv.soldiers as f64) * f.fx.summoner_get(|s| s.damage_mult, 1.0);
        f.hit_ability(f.drv.strike, Some(target), "soldiers", mult);
        if n == 1 {
            f.as_extra_until = f.t;
        }
    }
}

/// Javelin Toss / Prowler's Pounce. Ability-power form: attack speed for the
/// next few attacks, which become javelins — every third one is thrown at
/// the farthest dummy for the bigger calc ("the 3rd attack" is Riot's own
/// wording, with no row). Attack-damage form: a swipe that ignores a share
/// of the target's armor, and every third cast a heal (only counted:
/// nothing hits her) plus a bonus scaled by the target's missing health.
#[derive(Clone)]
pub struct Nidalee {
    casts: i64,
    javelins: i64,
    thrown: i64,
    armor_ignore: RowId,
    n_casts: RowId,
    missing_bonus: RowId,
    n_javelins: RowId,
    bonus_as: RowId,
    swipe: CalcId,
    pounce_heal: CalcId,
    javelin: CalcId,
    far_javelin: CalcId,
}

impl Driver for Nidalee {
    const NAME: &'static str = "Nidalee";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Nidalee { casts: 0, javelins: 0, thrown: 0,
                  armor_ignore: k.row("ArmorIgnoreRatio"), n_casts: k.row("NumCastsEmpowered"),
                  missing_bonus: k.row("ThirdAttackBonusDamageMissingHealth"),
                  n_javelins: k.row("NumEmpoweredAttacks"), bonus_as: k.row("BonusAttackSpeed"),
                  swipe: k.calc("PhysicalDamageCalc1"), pounce_heal: k.calc("GenericCalc1"),
                  javelin: k.calc("MagicDamageCalc1"), far_javelin: k.calc("MagicDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        if f.sheet.form == Some(Form::AD) {
            let d = match f.target() {
                Some(d) => d,
                None => return,
            };
            f.drv.casts += 1;
            let n = f.drv.casts;
            let missing = 1.0 - f.d(d).hp / f.d(d).max_hp;
            let cut = f.d(d).armor * f.row(f.drv.armor_ignore);   // ignored for this hit only
            f.dm(d).armor_flat += cut;
            f.hit_ability(f.drv.swipe, Some(d), "swipe", 1.0);
            f.dm(d).armor_flat -= cut;
            if n % pyint(f.row(f.drv.n_casts)) == 0 {
                let heal = f.calc(f.drv.pounce_heal);
                f.heal(heal, "pounce");
                let mult = f.row(f.drv.missing_bonus) * missing;
                f.hit_ability(f.drv.swipe, Some(d), "pounce", mult);
            }
            return;
        }
        f.drv.javelins = pyint(f.row(f.drv.n_javelins));
        f.as_extra = f.row(f.drv.bonus_as) - 1.0;
        f.as_extra_until = 1e9;
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        let n = f.drv.javelins;
        if n <= 0 {
            f.hit_attack(target, 1.0, "auto");
            return;
        }
        f.drv.javelins = n - 1;
        f.drv.thrown += 1;
        let thrown = f.drv.thrown;
        if thrown % 3 == 0 {
            f.hit_ability(f.drv.far_javelin, f.alive().last(), "farthest javelin", 1.0);
        } else {
            f.hit_ability(f.drv.javelin, Some(target), "javelins", 1.0);
        }
        if n == 1 {
            f.as_extra_until = f.t;
        }
    }
}
