//! Drivers of this slice: ported from tft_kits.py (see mod.rs for the list).
//! The second frontline batch — tanks and fighters whose dummies hit back,
//! so bodies, shields, heals and the damage they block all count.

#![allow(unused_imports)]
use crate::driver::Driver;
use crate::drivers::helpers::*;
use crate::fight::{Deal, Fight, Sel};
use crate::fx::Form;
use crate::kit::{CalcId, DType, Kit, RowId, Runtime};
use crate::pyf::{pyint, pymax, pymin, pyround};
use crate::spec::UnitSpec;

/// Spirit of Dread: armour and magic resist while the heal trickles in over
/// the same window ("for 3 seconds" is in Riot's text, not in a row), and
/// spectral riders that hit and stun the nearest few.
#[derive(Clone)]
pub struct Hecarim {
    hot: Option<Hot>,
    resists: RowId,
    n_enemies: RowId,
    stun: RowId,
    heal: CalcId,
    riders: CalcId,
}

impl Hecarim {
    const DREAD_S: f64 = 3.0;
}

impl HasHot for Hecarim {
    fn hot_mut(&mut self) -> &mut Option<Hot> {
        &mut self.hot
    }
}

impl Driver for Hecarim {
    const NAME: &'static str = "Hecarim";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Hecarim { hot: None, resists: k.row("Resists"), n_enemies: k.row("NumEnemies"),
                  stun: k.row("StunDuration"), heal: k.calc("HealthCalc1"),
                  riders: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let r = f.row(f.drv.resists);
        f.buff_resists(r, r, Self::DREAD_S);
        let heal = f.calc(f.drv.heal);
        heal_over_time(f, heal, Self::DREAD_S, "spirit of dread");
        let tg = f.aoe(Some(f.row(f.drv.n_enemies)), false);
        for d in tg.iter() {
            f.hit_ability(f.drv.riders, Some(d), "riders", 1.0);
        }
        let dur = f.row(f.drv.stun);
        f.stun(&tg, dur);
    }

    fn tick(f: &mut Fight<Self>) {
        tick_heal(f);
    }
}

/// Rock and Roll: bonus max health, then a roll into the target. On death he
/// splits into Kruglettes ("two" is in the text, and the Summoner trait would
/// add more) that taunt with the Kruglette's own resists; with the Riftbeast
/// Alpha Mark the Slate Buff shields an ally on the way out.
#[derive(Clone)]
pub struct Krug {
    mini_armor: f64,
    mini_mr: f64,
    trait_shield: RowId,
    mini_hp: CalcId,
    bonus_hp: CalcId,
    roll: CalcId,
}

impl Driver for Krug {
    const NAME: &'static str = "Krug";

    fn new(k: &Kit, u: &UnitSpec) -> Self {
        let s = u.extra_stats("TFT18_KrugMini");
        Krug { mini_armor: s.armor, mini_mr: s.mr, trait_shield: k.row("TraitShieldHealth"),
               mini_hp: k.calc("HealthCalc1"), bonus_hp: k.calc("HealthCalc2"),
               roll: k.calc("PhysicalDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let hp = f.calc(f.drv.bonus_hp);
        f.gain_max_hp(hp);
        f.hit_ability(f.drv.roll, f.target(), "ability", 1.0);
    }

    fn died(f: &mut Fight<Self>) {
        let (armor, mr) = (f.drv.mini_armor, f.drv.mini_mr);
        let hp = f.calc(f.drv.mini_hp);
        // the Summoner row is a total where 1 is the normal count
        let n = pyint(2.0 + pymax(0.0, f.fx.summoner_get(|s| s.extra_summons, 1.0) - 1.0));
        for _ in 0..n {
            f.add_body(hp, armor, mr, "kruglette");
        }
        if f.fx.riftbeast {
            let amount = f.row(f.drv.trait_shield) * f.max_hp();
            f.shield_ally(amount);
        }
    }
}

/// Furious Fists: every attack heals a share of her max health; the cast
/// heals a lump, then attack speed (a multiplier row) and durability for a
/// few seconds. Unstoppable does nothing here.
#[derive(Clone)]
pub struct Vi {
    duration: RowId,
    spell_as: RowId,
    spell_dur: RowId,
    fists: CalcId,
    roar: CalcId,
}

impl Driver for Vi {
    const NAME: &'static str = "Vi";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Vi { duration: k.row("SpellDuration"), spell_as: k.row("SpellAS"),
             spell_dur: k.row("SpellDurability"), fists: k.calc("HealthCalc1"),
             roar: k.calc("HealthCalc3") }
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        f.hit_attack(target, 1.0, "auto");
        let heal = f.calc(f.drv.fists);
        f.heal(heal, "furious fists");
    }

    fn cast(f: &mut Fight<Self>) {
        let dur = f.row(f.drv.duration);
        let heal = f.calc(f.drv.roar);
        f.heal(heal, "primal roar");
        let as_pct = f.row(f.drv.spell_as) - 1.0;
        f.buff_as(as_pct, dur);
        let durability = f.row(f.drv.spell_dur);
        f.buff_durability(durability, dur);
    }
}

/// Tantrum: a heartbeat on its own tick rate that heals him and hits the
/// dummies in reach; the cast bursts everyone nearby and stuns them, for the
/// longer duration when the target is already Burning.
#[derive(Clone)]
pub struct Amumu {
    beat: Option<f64>,
    tick_rate: RowId,
    heal_pct: RowId,
    stun: RowId,
    heal: CalcId,
    tantrum: CalcId,
    burst: CalcId,
    burning_stun: CalcId,
}

impl Driver for Amumu {
    const NAME: &'static str = "Amumu";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Amumu { beat: None, tick_rate: k.row("PassiveTickRate"),
                heal_pct: k.row("PassiveHealPercent"), stun: k.row("StunDuration"),
                heal: k.calc("HealthCalc2"), tantrum: k.calc("MagicDamageCalc1"),
                burst: k.calc("MagicDamageCalc3"), burning_stun: k.calc("GenericCalc1") }
    }

    fn tick(f: &mut Fight<Self>) {
        if !f.alive_unit {
            return;
        }
        let rate = f.row(f.drv.tick_rate);
        let nxt = match f.drv.beat {
            Some(b) => b,
            None => rate,
        };
        if f.t < nxt - 1e-9 {
            return;
        }
        f.drv.beat = Some(nxt + rate);
        // the data folds HealthCalc1 as 2.2% of HealthCalc2 plus 2.2% of max
        // health; the footer says it is the percentage plus HealthCalc2
        let amount = f.row(f.drv.heal_pct) * f.max_hp() + f.calc(f.drv.heal);
        f.heal(amount, "tantrum");
        let tg = f.adjacent(None);
        for d in tg.iter() {
            f.hit_ability(f.drv.tantrum, Some(d), "tantrum", 1.0);
        }
    }

    fn cast(f: &mut Fight<Self>) {
        let burning = match f.target() {
            Some(d) => f.d(d).burning(f.t),
            None => false,
        };
        let tg = f.aoe_all();
        for x in tg.iter() {
            f.hit_ability(f.drv.burst, Some(x), "ability", 1.0);
        }
        let dur = if burning { f.calc(f.drv.burning_stun) } else { f.row(f.drv.stun) };
        f.stun(&tg, dur);
    }
}

/// Lilting Lullaby: a heal and butterflies at the nearest few. Her own
/// attacks wake the dummy she is hitting at once — it takes the wake-up
/// damage and never sleeps — while the others sleep out the full duration
/// with nothing around to wake them.
#[derive(Clone)]
pub struct Lillia {
    n_enemies: RowId,
    wakeup: RowId,
    sleep: RowId,
    heal: CalcId,
    butterflies: CalcId,
}

impl Driver for Lillia {
    const NAME: &'static str = "Lillia";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Lillia { n_enemies: k.row("NumEnemiesToFireAt"), wakeup: k.row("WakeupDamage"),
                 sleep: k.row("SleepDuration"), heal: k.calc("HealthCalc1"),
                 butterflies: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let heal = f.calc(f.drv.heal);
        f.heal(heal, "lullaby");
        let prim = f.target();
        let tg = f.aoe(Some(f.row(f.drv.n_enemies)), false);
        for d in tg.iter() {
            f.hit_ability(f.drv.butterflies, Some(d), "butterflies", 1.0);
        }
        for d in tg.iter() {
            if Some(d) == prim {
                let amount = f.row(f.drv.wakeup) * f.d(d).max_hp;
                f.deal(amount, DType::Magic, Some(d), "wake-up", Deal::ABILITY);
            } else {
                let dur = f.row(f.drv.sleep);
                f.stun(&Sel::one(d), dur);
            }
        }
    }
}

/// Petrified Bark: a shield, and when it is spent (not when it expires) a
/// wave of dark energy scaling with the armour and magic resist he has then.
/// "Petrified" carries no numbers in the data, so it is left out.
#[derive(Clone)]
pub struct Malphite {
    tracked: Option<usize>,
    duration: RowId,
    shield: CalcId,
    wave: CalcId,
}

impl Driver for Malphite {
    const NAME: &'static str = "Malphite";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Malphite { tracked: None, duration: k.row("ShieldDuration"),
                   shield: k.calc("ShieldCalc1"), wave: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let amount = f.calc(f.drv.shield);
        let dur = f.row(f.drv.duration);
        f.drv.tracked = track_shield(f, amount, dur, "petrified bark");
    }

    fn hit(f: &mut Fight<Self>, _attacker: Option<usize>, _damage: f64) {
        let (broke, keep) = shield_broke(f, f.drv.tracked);
        f.drv.tracked = keep;
        if broke {
            let tg = f.aoe_all();
            for d in tg.iter() {
                f.hit_ability(f.drv.wave, Some(d), "shield break", 1.0);
            }
        }
    }
}

/// Azure Shockwave: a shield, then a fissure that knocks up, damages and
/// Mana Reaves everyone in its path. With the Riftbeast Alpha Mark the Blue
/// Buff's mana regen on himself.
#[derive(Clone)]
pub struct Sentinel {
    self_regen: RowId,
    duration: RowId,
    knockup: RowId,
    reave: RowId,
    shield: CalcId,
    fissure: CalcId,
}

impl Driver for Sentinel {
    const NAME: &'static str = "Sentinel";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Sentinel { self_regen: k.row("TraitSelfManaRegen"), duration: k.row("ShieldDuration"),
                   knockup: k.row("KnockupDuration"), reave: k.row("ManaReaveFlat"),
                   shield: k.calc("ShieldCalc1"), fissure: k.calc("MagicDamageCalc1") }
    }

    fn init(f: &mut Fight<Self>) {
        if f.fx.riftbeast {
            f.fx.mana_regen += f.row(f.drv.self_regen);
        }
    }

    fn cast(f: &mut Fight<Self>) {
        let amount = f.calc(f.drv.shield);
        let dur = f.row(f.drv.duration);
        f.shield(amount, dur, "shockwave", false);
        let tg = f.aoe_all();
        for d in tg.iter() {
            f.hit_ability(f.drv.fissure, Some(d), "fissure", 1.0);
        }
        let knockup = f.row(f.drv.knockup);
        f.stun(&tg, knockup);
        let reave = f.row(f.drv.reave);
        for i in tg.iter() {
            if f.d(i).alive {
                let d = f.dm(i);
                d.mana = pymax(0.0, d.mana - reave);
            }
        }
    }
}

/// Sow the Seeds: a sapling at the nearest dummy for every chunk of damage he
/// blocks, a fistful more when he falls, and a cast that hits the target and
/// heals a lump plus a share of the health he is missing.
#[derive(Clone)]
pub struct Maokai {
    saplings: i64,
    per_sapling: RowId,
    missing_heal: RowId,
    on_death: RowId,
    sapling: CalcId,
    strike: CalcId,
    heal: CalcId,
}

impl Driver for Maokai {
    const NAME: &'static str = "Maokai";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Maokai { saplings: 0, per_sapling: k.row("DamageMitigatedPerSapling"),
                 missing_heal: k.row("ActiveMissingHealthHeal"),
                 on_death: k.row("SaplingsOnDeath"), sapling: k.calc("MagicDamageCalc1"),
                 strike: k.calc("MagicDamageCalc2"), heal: k.calc("HealthCalc1") }
    }

    fn hit(f: &mut Fight<Self>, _attacker: Option<usize>, _damage: f64) {
        let n = pyint(f.mitigated / f.row(f.drv.per_sapling)) - f.drv.saplings;
        if n > 0 {
            f.drv.saplings += n;
            for _ in 0..n {
                f.hit_ability(f.drv.sapling, f.target(), "saplings", 1.0);
            }
        }
    }

    fn cast(f: &mut Fight<Self>) {
        f.hit_ability(f.drv.strike, f.target(), "ability", 1.0);
        let amount = f.calc(f.drv.heal) + f.row(f.drv.missing_heal) * (f.max_hp() - f.hp);
        f.heal(amount, "sow the seeds");
    }

    fn died(f: &mut Fight<Self>) {
        let n = pyint(f.row(f.drv.on_death));
        for _ in 0..n {
            f.hit_ability(f.drv.sapling, f.target(), "saplings", 1.0);
        }
    }
}

/// Emerald Radiance: the first time he drops below the threshold, the same
/// shield on him and on one ally; the cast heals and charges his next couple
/// of attacks with bonus magic damage (the paired ally's copy of the charge
/// is not simulated).
#[derive(Clone)]
pub struct Taric {
    radiance: bool,
    shatter: i64,
    threshold: RowId,
    shield_dur: RowId,
    n_attacks: RowId,
    shield: CalcId,
    heal: CalcId,
    charge: CalcId,
}

impl Driver for Taric {
    const NAME: &'static str = "Taric";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Taric { radiance: false, shatter: 0, threshold: k.row("PassivePercentHealthThreshold"),
                shield_dur: k.row("PassiveShieldDuration"), n_attacks: k.row("NumAttacks"),
                shield: k.calc("ShieldCalc1"), heal: k.calc("HealthCalc1"),
                charge: k.calc("MagicDamageCalc1") }
    }

    fn hit(f: &mut Fight<Self>, _attacker: Option<usize>, _damage: f64) {
        if !f.drv.radiance && f.hp_frac() < f.row(f.drv.threshold) {
            f.drv.radiance = true;
            let amt = f.calc(f.drv.shield);
            let dur = f.row(f.drv.shield_dur);
            f.shield(amt, dur, "emerald radiance", false);
            f.shield_ally(amt);
        }
    }

    fn cast(f: &mut Fight<Self>) {
        let heal = f.calc(f.drv.heal);
        f.heal(heal, "radiance");
        f.drv.shatter = pyint(f.row(f.drv.n_attacks));
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        f.hit_attack(target, 1.0, "auto");
        let n = f.drv.shatter;
        if n > 0 {
            f.drv.shatter = n - 1;
            f.hit_ability(f.drv.charge, Some(target), "shatter", 1.0);
        }
    }
}
