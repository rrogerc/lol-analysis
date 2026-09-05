//! Drivers of this slice: ported from tft_kits.py (see mod.rs for the list).
//! The fighters — the dummies hit back, so the body, the heals and the
//! shields matter as much as the damage.

use crate::driver::Driver;
use crate::fight::{Deal, Fight, Sel, TICK_S};
use crate::fx::Form;
use crate::kit::{CalcId, DType, Kit, RowId};
use crate::pyf::{pyint, pymax, pymin, pysum};
use crate::spec::UnitSpec;

/// Defensive Sweep: slice the target and take a shield for a couple of
/// seconds. The shield is a flat curve row, so it does not scale.
#[derive(Clone)]
pub struct Camille {
    shield: RowId,
    shield_dur: RowId,
    slice: CalcId,
}

impl Driver for Camille {
    const NAME: &'static str = "Camille";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Camille { shield: k.row("AbilityShield"), shield_dur: k.row("ShieldDuration"),
                  slice: k.calc("PhysicalDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        f.hit_ability(f.drv.slice, f.target(), "sweep", 1.0);
        let (amount, dur) = (f.row(f.drv.shield), f.row(f.drv.shield_dur));
        f.shield(amount, dur, "sweep", false);
    }
}

/// Jaws of The Beast: a bite that heals him for a share of the damage it
/// actually lands, and attack speed that stacks for the rest of the fight
/// (the row is a multiplier: 1.2 is +20% a cast).
#[derive(Clone)]
pub struct Warwick {
    as_buff: RowId,
    bite: CalcId,
    heal_pct: CalcId,
}

impl Driver for Warwick {
    const NAME: &'static str = "Warwick";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Warwick { as_buff: k.row("AttackSpeedBuff"), bite: k.calc("PhysicalDamageCalc1"),
                  heal_pct: k.calc("GenericCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let dmg = f.hit_ability(f.drv.bite, f.target(), "bite", 1.0);
        let heal = dmg * f.calc(f.drv.heal_pct);
        f.heal(heal, "bite");
        f.as_extra += f.row(f.drv.as_buff) - 1.0;
        f.as_extra_until = 1e9;
    }
}

/// Crimson Fury: bonus attack damage for a spell of frenzy, during which
/// he ignores part of every dummy's armor — modelled as a sunder, which is
/// exactly his own armor ignore since nothing else damages them. A kill
/// leaps at the next target. With the Riftbeast Alpha Mark (Red Buff) his
/// attacks burn and heal him for a share of his max health.
#[derive(Clone)]
pub struct Brambleback {
    /// (until, attack damage granted) per live spell of frenzy.
    frenzy: Vec<(f64, f64)>,
    duration: RowId,
    frenzy_ad: RowId,
    burn_amount: RowId,
    burn_dur: RowId,
    trait_heal: RowId,
    ignore: CalcId,
    leap: CalcId,
}

impl Driver for Brambleback {
    const NAME: &'static str = "Brambleback";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Brambleback { frenzy: Vec::new(), duration: k.row("Duration"),
                      frenzy_ad: k.row("FrenzyADPercent"), burn_amount: k.row("BurnAmount"),
                      burn_dur: k.row("TraitBurnDuration"),
                      trait_heal: k.row("TraitMaxHealthHeal"), ignore: k.calc("GenericCalc1"),
                      leap: k.calc("PhysicalDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let (dur, ad) = (f.row(f.drv.duration), f.row(f.drv.frenzy_ad));
        f.ad_extra += ad;
        let t = f.t;
        f.drv.frenzy.push((t + dur, ad));
        let ignore = f.calc(f.drv.ignore);
        let al = f.alive();
        for d in al.iter() {
            f.sunder(d, ignore, dur);
        }
    }

    fn tick(f: &mut Fight<Self>) {
        let t = f.t;
        let done = !f.drv.frenzy.is_empty()
            && f.drv.frenzy.iter().any(|&(until, _)| t >= until - 1e-9);
        if done {
            let spent = pysum(f.drv.frenzy.iter().filter(|x| t >= x.0 - 1e-9).map(|x| x.1));
            f.ad_extra -= spent;
            f.drv.frenzy.retain(|x| t < x.0 - 1e-9);
        }
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        f.hit_attack(target, 1.0, "auto");
        if f.fx.riftbeast {
            let (pct, dur) = (f.row(f.drv.burn_amount) / 100.0, f.row(f.drv.burn_dur));
            f.burn(target, pct, dur, false);
            let heal = f.row(f.drv.trait_heal) * f.max_hp();
            f.heal(heal, "red buff");
        }
    }

    fn kill(f: &mut Fight<Self>, _target: usize) {
        let d = f.target();
        if d.is_some() {
            f.hit_ability(f.drv.leap, d, "leap", 1.0);
        }
    }
}

/// Pale Barrier: a shield, and moonlight orbs spread evenly over the
/// dummies in reach.
#[derive(Clone)]
pub struct Diana {
    shield_dur: RowId,
    n_orbs: RowId,
    shield: CalcId,
    orb: CalcId,
}

impl Driver for Diana {
    const NAME: &'static str = "Diana";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Diana { shield_dur: k.row("ShieldDuration"), n_orbs: k.row("NumOrbs"),
                shield: k.calc("ShieldCalc1"), orb: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let amount = f.calc(f.drv.shield);
        let dur = f.row(f.drv.shield_dur);
        f.shield(amount, dur, "barrier", false);
        let tg = f.aoe_all();
        let orbs = pyint(f.row(f.drv.n_orbs));
        for i in 0..orbs {
            let mut al = f.alive_of(&tg);
            if al.is_empty() {
                al = f.alive();
            }
            if al.is_empty() {
                break;
            }
            let d = al.get((i as usize) % al.len());
            f.hit_ability(f.drv.orb, Some(d), "orbs", 1.0);
        }
    }
}

/// Withering Curse: omnivamp from the start; the blast curses the nearest
/// few and leaves a zone ticking on everyone in it for the same time. Each
/// live curse on a dummy adds flat magic damage to every hit she lands on
/// it, and curses stack with repeat casts.
#[derive(Clone)]
pub struct Morgana {
    omnivamp: RowId,
    spell_dur: RowId,
    n_cursed: RowId,
    blast: CalcId,
    zone: CalcId,
    curse: CalcId,
}

impl Morgana {
    /// `_curse_bonus`: the live curses on `d` each add their flat damage.
    fn curse_bonus(f: &mut Fight<Self>, d: usize) {
        let t = f.t;
        let n = f.d(d).mark_times.iter().filter(|&&until| t < until).count();
        if n != 0 && f.d(d).alive {
            let amount = f.calc(f.drv.curse) * (n as f64);
            f.deal(amount, DType::Magic, Some(d), "curse", Deal::ABILITY);
        }
    }
}

impl Driver for Morgana {
    const NAME: &'static str = "Morgana";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Morgana { omnivamp: k.row("Omnivamp"), spell_dur: k.row("SpellDuration"),
                  n_cursed: k.row("NumEnemiesCursed"), blast: k.calc("MagicDamageCalc1"),
                  zone: k.calc("MagicDamageCalc2"), curse: k.calc("MagicDamageCalc3") }
    }

    fn init(f: &mut Fight<Self>) {
        f.sheet.omnivamp += f.row(f.drv.omnivamp);
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        f.hit_attack(target, 1.0, "auto");
        Self::curse_bonus(f, target);
    }

    fn cast(f: &mut Fight<Self>) {
        let dur = f.row(f.drv.spell_dur);
        let cursed = f.aoe(Some(f.row(f.drv.n_cursed)), false);
        for d in cursed.iter() {
            f.hit_ability(f.drv.blast, Some(d), "blast", 1.0);
            Self::curse_bonus(f, d);
            let t = f.t;
            f.dm(d).mark_times.push(t + dur);
        }
        let zone = f.aoe_all();
        for d in zone.iter() {
            f.dot_ability(f.drv.zone, Some(d), dur, "withering zone", dur);
        }
    }
}

/// Savagery: jump onto the dummy with the least health left as a share of
/// its own, stab it, then heal — the more health it is missing, the closer
/// the heal to its maximum.
#[derive(Clone)]
pub struct Rengar {
    stab: CalcId,
    heal_lo: CalcId,
    heal_hi: CalcId,
}

impl Driver for Rengar {
    const NAME: &'static str = "Rengar";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Rengar { stab: k.calc("PhysicalDamageCalc1"), heal_lo: k.calc("HealthCalc1"),
                 heal_hi: k.calc("HealthCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let al = f.alive();
        let d = match f.min_by(&al, |x| x.hp / x.max_hp) {
            Some(d) => d,
            None => return,
        };
        f.hit_ability(f.drv.stab, Some(d), "stab", 1.0);
        let (lo, hi) = (f.calc(f.drv.heal_lo), f.calc(f.drv.heal_hi));
        let (hp, max_hp) = (f.d(d).hp, f.d(d).max_hp);
        f.heal(lo + (hi - lo) * (1.0 - hp / max_hp), "savagery");
    }
}

/// Heat Without Equal: attacks splash onto the dummies beside the target.
/// The first cast is the flight — untargetable for the cast time, then a
/// stun on everyone, omnivamp, an Ignite burning a share of max health,
/// and Flame Breath straight away; every later cast is Flame Breath alone,
/// a line weaker per dummy passed and another Ignite. With the Riftbeast
/// Alpha Mark (Elder Dragon Buff) a dummy pushed under the execute
/// threshold is finished off.
#[derive(Clone)]
pub struct ElderDragon {
    landed: bool,
    aoe_ratio: RowId,
    stun_dur: RowId,
    omnivamp: RowId,
    fall: RowId,
    floor: RowId,
    ignite_dur: RowId,
    ignite_pct: RowId,
    execute_thr: RowId,
    breath: CalcId,
}

impl ElderDragon {
    fn ignite(f: &mut Fight<Self>, targets: &Sel) {
        let (dur, pct) = (f.row(f.drv.ignite_dur), f.row(f.drv.ignite_pct));
        for d in targets.iter() {
            let max_hp = f.d(d).max_hp;
            f.dot(pct * max_hp * dur, dur, DType::Physical, Some(d), "ignite", false);
        }
    }

    fn execute(f: &mut Fight<Self>) {
        if !f.fx.riftbeast {
            return;
        }
        let thr = f.row(f.drv.execute_thr);
        let al = f.alive();
        for d in al.iter() {
            let (hp, max_hp) = (f.d(d).hp, f.d(d).max_hp);
            if hp < thr * max_hp {
                f.deal(hp, DType::True, Some(d), "execute", Deal::PLAIN);
            }
        }
    }
}

impl Driver for ElderDragon {
    const NAME: &'static str = "ElderDragon";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        ElderDragon { landed: false, aoe_ratio: k.row("AttackAoERatio"),
                      stun_dur: k.row("StunDuration"), omnivamp: k.row("Omnivamp"),
                      fall: k.row("DamageReductionPerHit"),
                      floor: k.row("MinimumDamageThreshold"),
                      ignite_dur: k.row("IgniteDuration"),
                      ignite_pct: k.row("IgniteMaxHealthDamage"),
                      execute_thr: k.row("TraitExecuteThreshold"),
                      breath: k.calc("PhysicalDamageCalc2") }
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        f.hit_attack(target, 1.0, "auto");
        let ratio = f.row(f.drv.aoe_ratio);
        let adj = f.adjacent(None);
        for d in adj.iter() {
            if d != target {
                f.hit_attack(d, ratio, "splash");
            }
        }
        Self::execute(f);
    }

    fn cast(f: &mut Fight<Self>) {
        let landing = !f.drv.landed;
        if landing {
            f.drv.landed = true;
            let ct = Self::cast_time(f);
            f.untargetable(ct);
            let al = f.alive();
            let stun = f.row(f.drv.stun_dur);
            f.stun(&al, stun);
            f.sheet.omnivamp += f.row(f.drv.omnivamp);
            let al = f.alive();
            Self::ignite(f, &al);
        }
        let (fall, floor) = (f.row(f.drv.fall), f.row(f.drv.floor));
        let tg = f.aoe_all();
        for (i, d) in tg.iter().enumerate() {
            let mult = pymax(floor, 1.0 - fall * (i as f64));
            f.hit_ability(f.drv.breath, Some(d), "flame breath", mult);
        }
        if !landing {
            // the landing's Ignite already covers everyone
            Self::ignite(f, &tg);
        }
        Self::execute(f);
    }

    fn tick(f: &mut Fight<Self>) {
        Self::execute(f);
    }
}

/// Rending Claws: leap onto the dummy with the least health left, then a
/// couple of empowered attacks — far faster, each carrying bonus physical
/// damage — with more granted by every kill. With the Riftbeast Alpha Mark
/// (Grey Buff) he has Precision and crit chance that climbs as his own
/// health falls.
#[derive(Clone)]
pub struct Murkwolf {
    /// The crit chance he started with, once the Alpha Mark is on him.
    crit: Option<f64>,
    empowered: i64,
    base_crit: RowId,
    bonus_crit: RowId,
    empower_aspd: RowId,
    n_empowered: RowId,
    n_on_kill: RowId,
    leap: CalcId,
    empowered_hit: CalcId,
}

impl Murkwolf {
    fn empower(f: &mut Fight<Self>, n: i64) {
        f.drv.empowered = n;
        f.as_extra = if n > 0 { f.row(f.drv.empower_aspd) - 1.0 } else { 0.0 };
        f.as_extra_until = if n > 0 { 1e9 } else { f.t };
    }
}

impl Driver for Murkwolf {
    const NAME: &'static str = "Murkwolf";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Murkwolf { crit: None, empowered: 0, base_crit: k.row("TraitBaseCrit"),
                   bonus_crit: k.row("TraitBonusCrit"), empower_aspd: k.row("EmpowerAspd"),
                   n_empowered: k.row("NumEmpoweredAttacks"),
                   n_on_kill: k.row("NumEmpoweredAttacksGainedOnKill"),
                   leap: k.calc("PhysicalDamageCalc1"),
                   empowered_hit: k.calc("PhysicalDamageCalc2") }
    }

    fn init(f: &mut Fight<Self>) {
        if f.fx.riftbeast {
            f.sheet.precision = true;
            f.drv.crit = Some(f.sheet.crit_chance);
            Self::tick(f);
        }
    }

    fn tick(f: &mut Fight<Self>) {
        if let Some(crit) = f.drv.crit {
            let (lo, hi) = (f.row(f.drv.base_crit), f.row(f.drv.bonus_crit));
            f.sheet.crit_chance = pymin(1.0, crit + lo + (hi - lo) * (1.0 - f.hp_frac()));
        }
    }

    fn cast(f: &mut Fight<Self>) {
        let al = f.alive();
        let d = f.min_by(&al, |x| x.hp);
        f.hit_ability(f.drv.leap, d, "leap", 1.0);
        let n = f.drv.empowered + pyint(f.row(f.drv.n_empowered));
        Self::empower(f, n);
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        let n = f.drv.empowered;
        f.hit_attack(target, 1.0, "auto");
        if n > 0 {
            f.hit_ability(f.drv.empowered_hit, Some(target), "empowered", 1.0);
            let left = f.drv.empowered - 1;
            Self::empower(f, left);
        }
    }

    fn kill(f: &mut Fight<Self>, _target: usize) {
        let n = f.drv.empowered + pyint(f.row(f.drv.n_on_kill));
        Self::empower(f, n);
    }
}

/// Firestorm: charges up for ability power per burning dummy, shields
/// himself, rushes through the group, then leaves a firestorm whose total
/// damage is split over everyone it covers and ticks for a couple of
/// seconds.
#[derive(Clone)]
pub struct Kennen {
    ap_per_burning: RowId,
    shield_dur: RowId,
    storm_dur: RowId,
    shield: CalcId,
    rush: CalcId,
    storm: CalcId,
}

impl Driver for Kennen {
    const NAME: &'static str = "Kennen";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Kennen { ap_per_burning: k.row("APPerBurningEnemy"), shield_dur: k.row("ShieldDuration"),
                 storm_dur: k.row("FirestormDuration"), shield: k.calc("ShieldCalc1"),
                 rush: k.calc("MagicDamageCalc1"), storm: k.calc("MagicDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let t = f.t;
        let al = f.alive();
        let mut burning = 0i64;
        for d in al.iter() {
            if f.d(d).burning(t) {
                burning += 1;
            }
        }
        let charge = f.row(f.drv.ap_per_burning) * 100.0 * (burning as f64);
        f.ap_extra += charge;
        let amount = f.calc(f.drv.shield);
        let shield_dur = f.row(f.drv.shield_dur);
        f.shield(amount, shield_dur, "firestorm", false);
        let tg = f.aoe_all();
        for d in tg.iter() {
            f.hit_ability(f.drv.rush, Some(d), "rush", 1.0);
        }
        let dur = f.row(f.drv.storm_dur);
        for d in tg.iter() {
            let mult = 1.0 / (tg.len() as f64);
            f.dot_ability(f.drv.storm, Some(d), dur, "firestorm", mult);
        }
        f.ap_extra -= charge;
    }
}

/// Wuju Style: no mana. Every third attack is a Double Strike that hits
/// twice ("every third" is spelled out in the ability text and has no
/// curve row). As an Adaptor his AD form banks stacking attack speed off
/// each Double Strike; his AP form adds bonus magic damage and heals him
/// for a share of it.
#[derive(Clone)]
pub struct MasterYi {
    n: i64,
    heal_pct: RowId,
    magic: CalcId,
    as_gain: CalcId,
}

impl MasterYi {
    const DOUBLE_EVERY: i64 = 3;
}

impl Driver for MasterYi {
    const NAME: &'static str = "MasterYi";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        MasterYi { n: 0, heal_pct: k.row("APForm_HealPercent"),
                   magic: k.calc("MagicDamageCalc1"), as_gain: k.calc("AttackSpeedCalc1") }
    }

    fn init(f: &mut Fight<Self>) {
        f.sheet.mana_max = 0.0;
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        let n = f.drv.n + 1;
        f.drv.n = n;
        f.hit_attack(target, 1.0, "auto");
        if n % Self::DOUBLE_EVERY != 0 {
            return;
        }
        let d = if f.d(target).alive { Some(target) } else { f.target() };
        let d = match d {
            Some(d) => d,
            None => return,
        };
        f.hit_attack(d, 1.0, "double strike");
        if f.sheet.form == Some(Form::AP) {
            let dmg = f.hit_ability(f.drv.magic, Some(d), "double strike", 1.0);
            let heal = dmg * f.row(f.drv.heal_pct);
            f.heal(heal, "double strike");
        } else {
            f.as_extra += f.calc(f.drv.as_gain);
            f.as_extra_until = 1e9;
        }
    }
}

/// Rage Gene: no mana — Rage builds per second and per attack until he
/// transforms into Mega Gnar, gaining max health, hitting and stunning the
/// group and stripping flat resists off them. Mega Gnar then casts Grab n'
/// Throw on mana at a Fighter's ten per attack: the target takes the
/// throw, the dummies it passes through take the rest, and the last dummy
/// standing is thrown off the board.
#[derive(Clone)]
pub struct Gnar {
    rage: f64,
    mega: bool,
    per_attack: RowId,
    per_second: RowId,
    rage_max: RowId,
    stun_dur: RowId,
    health: CalcId,
    strip: CalcId,
    transform: CalcId,
    throw: CalcId,
    passed: CalcId,
}

/// tft.ROLE_MANA["Fighter"]: what Mega Gnar gains per attack.
const FIGHTER_MANA: f64 = 10.0;

impl Gnar {
    fn rage(f: &mut Fight<Self>, amount: f64) {
        if f.drv.mega {
            return;
        }
        f.drv.rage += amount;
        if f.drv.rage < f.row(f.drv.rage_max) {
            return;
        }
        f.drv.mega = true;
        let health = f.calc(f.drv.health);
        f.gain_max_hp(health);
        let strip = f.calc(f.drv.strip);
        let tg = f.aoe_all();
        for d in tg.iter() {
            f.hit_ability(f.drv.transform, Some(d), "transform", 1.0);
            let dummy = f.dm(d);
            dummy.armor_flat += strip;
            dummy.mr_flat += strip;
        }
        let tg = f.aoe_all();
        let stun = f.row(f.drv.stun_dur);
        f.stun(&tg, stun);
        f.sheet.mana_max = f.kit.stats.mana;
        f.sheet.mana_per_attack = FIGHTER_MANA;
    }
}

impl Driver for Gnar {
    const NAME: &'static str = "Gnar";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Gnar { rage: 0.0, mega: false, per_attack: k.row("RagePerAttack"),
               per_second: k.row("RagePerSecond"), rage_max: k.row("TransformRageMax"),
               stun_dur: k.row("StunDuration"), health: k.calc("HealthCalc1"),
               strip: k.calc("GenericCalc1"), transform: k.calc("PhysicalDamageCalc3"),
               throw: k.calc("PhysicalDamageCalc1"), passed: k.calc("PhysicalDamageCalc2") }
    }

    fn init(f: &mut Fight<Self>) {
        f.sheet.mana_max = 0.0;
        f.drv.rage = 0.0;
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        f.hit_attack(target, 1.0, "auto");
        let amount = f.row(f.drv.per_attack);
        Self::rage(f, amount);
    }

    fn tick(f: &mut Fight<Self>) {
        let amount = f.row(f.drv.per_second) * TICK_S;
        Self::rage(f, amount);
    }

    fn cast(f: &mut Fight<Self>) {
        let d = f.target();
        if f.alive().len() == 1 {
            if let Some(i) = d {
                let hp = f.d(i).hp;
                f.deal(hp, DType::True, Some(i), "thrown off", Deal::PLAIN);
            }
            return;
        }
        f.hit_ability(f.drv.throw, d, "throw", 1.0);
        let others = f.aoe(None, true);
        for o in others.iter() {
            f.hit_ability(f.drv.passed, Some(o), "passed through", 1.0);
        }
    }
}
