//! Drivers ported first, one of every pattern: a plain cast (Ahri), a line
//! with a trail (Ashe), an Adaptor with two forms (Akali), a cycle counter
//! (Alune), a channel that lands at its start and queues an event
//! (Aphelios), a manaless attacker (Caitlyn, Kayle), an on-death body from
//! a summon's stats (Yorick), a tracked shield (Rammus), a wind-up whose
//! second half is an event (Sett).

use crate::driver::Driver;
use crate::drivers::helpers::{shield_broke, track_shield};
use crate::fight::Fight;
use crate::fx::Form;
use crate::kit::{CalcId, DType, Kit, RowId};
use crate::pyf::{pyint, pymax, pymin, pyround};
use crate::spec::UnitSpec;

/// Spirit Bomb: area damage around the densest spot, falling off per hex
/// from the epicenter. In the clump the other dummies sit one hex out.
#[derive(Clone)]
pub struct Ahri {
    channel: RowId,
    fall: RowId,
    dmg: CalcId,
}

impl Driver for Ahri {
    const NAME: &'static str = "Ahri";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Ahri { channel: k.row("ChannelTime"), fall: k.row("HexPercentDamageFalloffTooltip"),
               dmg: k.calc("MagicDamageCalc1") }
    }

    fn cast_time(f: &Fight<Self>) -> f64 {
        f.row(f.drv.channel)
    }

    fn cast(f: &mut Fight<Self>) {
        let fall = f.row(f.drv.fall);
        let tg = f.aoe_all();
        for (i, d) in tg.iter().enumerate() {
            let mult = if i == 0 { 1.0 } else { 1.0 - fall };
            f.hit_ability(f.drv.dmg, Some(d), "ability", mult);
        }
    }
}

/// Spirit Rift: an arrow through the line, weaker per dummy hit, then a
/// trail that ticks attack-damage plus a share of max health for a few
/// seconds on everyone standing in it.
#[derive(Clone)]
pub struct Ashe {
    fall: RowId,
    floor: RowId,
    dur: RowId,
    pct: RowId,
    arrow: CalcId,
    trail: CalcId,
}

impl Driver for Ashe {
    const NAME: &'static str = "Ashe";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Ashe { fall: k.row("DamageFalloffPerEnemy"), floor: k.row("MinDamagePercent"),
               dur: k.row("RiftDuration"), pct: k.row("MaxHealthDamagePerSecond"),
               arrow: k.calc("PhysicalDamageCalc1"), trail: k.calc("PhysicalDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let (fall, floor) = (f.row(f.drv.fall), f.row(f.drv.floor));
        let tg = f.aoe_all();
        for (i, d) in tg.iter().enumerate() {
            let mult = pymax(floor, 1.0 - fall * (i as f64));
            f.hit_ability(f.drv.arrow, Some(d), "ability", mult);
        }
        let dur = f.row(f.drv.dur);
        let pct = f.row(f.drv.pct);
        for d in tg.iter() {
            if f.d(d).alive {
                f.dot_ability(f.drv.trail, Some(d), dur, "trail", dur);
                let max_hp = f.d(d).max_hp;
                f.dot(pct * max_hp * dur, dur, DType::Physical, Some(d), "trail", false);
            }
        }
    }
}

/// Kunai Strike. Attack-damage form: a volley, more if the target burns.
/// Ability-power form: magic damage, multiplied against a tank, and a kill
/// casts it again at reduced damage.
#[derive(Clone)]
pub struct Akali {
    phys1: CalcId,
    phys2: CalcId,
    magic1: CalcId,
    tank_mult: RowId,
    recast: RowId,
}

impl Akali {
    fn ad(f: &mut Fight<Self>) {
        let d = match f.target() {
            Some(d) => d,
            None => return,
        };
        let burning = f.d(d).burning(f.t);
        f.hit_ability(f.drv.phys1, Some(d), "ability", 1.0);
        if burning && f.d(d).alive {
            f.hit_ability(f.drv.phys2, Some(d), "burning bonus", 1.0);
        }
    }

    fn ap(f: &mut Fight<Self>) {
        let mut mult = 1.0;
        for _ in 0..4 {
            let d = match f.target() {
                Some(d) => d,
                None => break,
            };
            let m = mult * if f.d(d).is_tank { f.row(f.drv.tank_mult) } else { 1.0 };
            f.hit_ability(f.drv.magic1, Some(d), "ability", m);
            if f.d(d).alive {
                break;
            }
            mult *= f.row(f.drv.recast);
        }
    }
}

impl Driver for Akali {
    const NAME: &'static str = "Akali";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Akali { phys1: k.calc("PhysicalDamageCalc1"), phys2: k.calc("PhysicalDamageCalc2"),
                magic1: k.calc("MagicDamageCalc1"), tank_mult: k.row("TankDamageMultiplierAP"),
                recast: k.row("RecastDamageReduction") }
    }

    fn cast(f: &mut Fight<Self>) {
        if f.sheet.form == Some(Form::AD) {
            Self::ad(f);
        } else {
            Self::ap(f);
        }
    }
}

/// Moonfall: nine shards over the nearest three (the rest move on when one
/// dies); every fourth cast the moon is full and crashes on the whole
/// board instead, split over everyone standing whatever the geometry.
#[derive(Clone)]
pub struct Alune {
    casts: i64,
    n_enemies: RowId,
    n_shards: RowId,
    shard: CalcId,
    moon: CalcId,
}

impl Driver for Alune {
    const NAME: &'static str = "Alune";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Alune { casts: 0, n_enemies: k.row("NumEnemies"), n_shards: k.row("NumMoonshards"),
                shard: k.calc("MagicDamageCalc1"), moon: k.calc("MagicDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        f.drv.casts += 1;
        let n = f.drv.casts;
        if n % 4 == 0 {
            let tg = f.alive();
            let mult = 1.0 / (tg.len() as f64);
            for d in tg.iter() {
                f.hit_ability(f.drv.moon, Some(d), "full moon", mult);
            }
            return;
        }
        let tg = f.aoe(Some(f.row(f.drv.n_enemies)), false);
        let shards = pyint(f.row(f.drv.n_shards));
        for i in 0..shards {
            let mut al = f.alive_of(&tg);
            if al.is_empty() {
                al = f.alive();
            }
            if al.is_empty() {
                break;
            }
            let d = al.get((i as usize) % al.len());
            f.hit_ability(f.drv.shard, Some(d), "moonshards", 1.0);
        }
    }
}

/// Moonlight's Onslaught: swipes spread evenly over the two seconds (more
/// with bonus attack speed), every third one an attack for on-hit purposes
/// and the per-attack stacks, then — when the onslaught ends — a blast
/// split among the dummies in reach.
#[derive(Clone)]
pub struct Aphelios {
    /// (start, end, swipes, done) while an onslaught runs.
    onslaught: Option<(f64, f64, i64, i64)>,
    duration: RowId,
    base_swipes: RowId,
    as_per_swipe: RowId,
    per_auto: RowId,
    swipe: CalcId,
    blast: CalcId,
}

const APHELIOS_BLAST: u32 = 1;

impl Aphelios {
    fn pay_swipes(f: &mut Fight<Self>) {
        let (start, end, swipes, _) = match f.drv.onslaught {
            Some(o) => o,
            None => return,
        };
        let due = if end > start {
            pyint(pyround((swipes as f64) * pymin(1.0, (f.t - start) / (end - start))))
        } else {
            swipes
        };
        let per_auto = pyint(f.row(f.drv.per_auto));
        while f.drv.onslaught.map(|o| o.3 < due).unwrap_or(false) {
            let d = match f.target() {
                Some(d) => d,
                None => break,
            };
            let done = {
                let o = f.drv.onslaught.as_mut().unwrap();
                o.3 += 1;
                o.3
            };
            f.hit_ability(f.drv.swipe, Some(d), "swipes", 1.0);
            if done % per_auto == 0 && f.d(d).alive {
                f.simulated_attack(d);
            }
        }
    }

    fn blast(f: &mut Fight<Self>) {
        if let Some(o) = f.drv.onslaught.as_mut() {
            // swipes the ticks have not paid out yet land with the blast
            o.1 = f.t;
            Self::pay_swipes(f);
        }
        f.drv.onslaught = None;
        let tg = f.aoe_all();
        let mult = 1.0 / (tg.len() as f64);
        for d in tg.iter() {
            f.hit_ability(f.drv.blast, Some(d), "blast", mult);
        }
    }
}

impl Driver for Aphelios {
    const NAME: &'static str = "Aphelios";
    const LANDS_AT_START: bool = true;

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Aphelios { onslaught: None, duration: k.row("Duration"), base_swipes: k.row("NumAttacksBase"),
                   as_per_swipe: k.row("AS_NeededForExtraSwipe"),
                   per_auto: k.row("NumSwipesTriggerSimulatedAutos"),
                   swipe: k.calc("PhysicalDamageCalc1"), blast: k.calc("PhysicalDamageCalc2") }
    }

    fn cast_time(f: &Fight<Self>) -> f64 {
        f.row(f.drv.duration)
    }

    fn cast(f: &mut Fight<Self>) {
        let bonus_as = f.attack_speed() / f.sheet.base_as - 1.0;
        let swipes = pyint(f.row(f.drv.base_swipes))
            + pyint(pymax(0.0, bonus_as) / f.row(f.drv.as_per_swipe));
        let dur = f.row(f.drv.duration);
        f.drv.onslaught = Some((f.t, f.t + dur, swipes, 0));
        f.after(dur, APHELIOS_BLAST);
    }

    fn tick(f: &mut Fight<Self>) {
        Self::pay_swipes(f);
    }

    fn event(f: &mut Fight<Self>, tag: u32) {
        if tag == APHELIOS_BLAST {
            Self::blast(f);
        }
    }
}

/// Headshot: every third attack is replaced by an ability hit. Ammo, not
/// mana: mana items do nothing.
#[derive(Clone)]
pub struct Caitlyn {
    n: i64,
    before: RowId,
    headshot: CalcId,
}

impl Driver for Caitlyn {
    const NAME: &'static str = "Caitlyn";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Caitlyn { n: 0, before: k.row("AttacksBeforeHeadshot"), headshot: k.calc("PhysicalDamageCalc1") }
    }

    fn init(f: &mut Fight<Self>) {
        f.sheet.mana_max = 0.0;
        f.drv.n = 0;
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        f.drv.n += 1;
        if f.drv.n % (pyint(f.row(f.drv.before)) + 1) == 0 {
            f.hit_ability(f.drv.headshot, Some(target), "headshot", 1.0);
        } else {
            f.hit_attack(target, 1.0, "auto");
        }
    }
}

/// Solar Judgement: no mana; each star level unlocks another passive on
/// her attacks — bonus magic damage, then shred, then waves that hit
/// everyone else.
#[derive(Clone)]
pub struct Kayle {
    shred_level: RowId,
    shred_dur: RowId,
    ascension: CalcId,
    waves: CalcId,
}

impl Driver for Kayle {
    const NAME: &'static str = "Kayle";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Kayle { shred_level: k.row("ShredLevel"), shred_dur: k.row("ShredDuration"),
                ascension: k.calc("MagicDamageCalc1"), waves: k.calc("MagicDamageCalc2") }
    }

    fn init(f: &mut Fight<Self>) {
        f.sheet.mana_max = 0.0;
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        let star = f.sheet.star;
        f.hit_attack(target, 1.0, "auto");
        if !f.d(target).alive {
            return;
        }
        f.hit_ability(f.drv.ascension, Some(target), "ascension", 1.0);
        if star >= 2 && f.d(target).alive {
            let (pct, dur) = (f.row(f.drv.shred_level) / 100.0, f.row(f.drv.shred_dur));
            let t = f.t;
            let d = f.dm(target);
            if t >= d.shred_until || pct >= d.shred {
                d.shred = pct;
            }
            d.shred_until = pymax(d.shred_until, t + dur);
        }
        if star >= 3 {
            let others = f.aoe(None, true);
            for d in others.iter() {
                f.hit_ability(f.drv.waves, Some(d), "waves", 1.0);
            }
        }
    }
}

/// Last Rites: the active heals him and strikes the target. On death a
/// Spirit Walker with the ghoul's health — multiplied by the Summoner trait
/// when it is active — taunts and holds the dummies; its resists are the
/// summon's own stats.
#[derive(Clone)]
pub struct Yorick {
    spirit_armor: f64,
    spirit_mr: f64,
    heal: CalcId,
    strike: CalcId,
    spirit_hp: CalcId,
}

impl Driver for Yorick {
    const NAME: &'static str = "Yorick";

    fn new(k: &Kit, u: &UnitSpec) -> Self {
        let s = u.extra_stats("TFT18_Yorick_Spirit");
        Yorick { spirit_armor: s.armor, spirit_mr: s.mr, heal: k.calc("HealthCalc1"),
                 strike: k.calc("PhysicalDamageCalc1"), spirit_hp: k.calc("HealthCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let heal = f.calc(f.drv.heal);
        f.heal(heal, "last rites");
        f.hit_ability(f.drv.strike, f.target(), "strike", 1.0);
    }

    fn died(f: &mut Fight<Self>) {
        let hp = f.calc(f.drv.spirit_hp) * f.fx.summoner_get(|s| s.health_mult, 1.0);
        f.add_body(hp, f.drv.spirit_armor, f.drv.spirit_mr, "spirit walker");
    }
}

/// Defensive Ball Curl: a shield and heavy resists for a few seconds — the
/// taunt is already the model, the dummies have nobody else to hit. If the
/// shield is spent rather than expiring the ball uncurls, and the burst
/// scales with the armour and magic resist he has at that moment.
#[derive(Clone)]
pub struct Rammus {
    tracked: Option<usize>,
    duration: RowId,
    armor_mr: RowId,
    shield: CalcId,
    burst: CalcId,
}

impl Driver for Rammus {
    const NAME: &'static str = "Rammus";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Rammus { tracked: None, duration: k.row("Duration"), armor_mr: k.row("ArmorMR"),
                 shield: k.calc("ShieldCalc1"), burst: k.calc("PhysicalDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let dur = f.row(f.drv.duration);
        let amount = f.calc(f.drv.shield);
        f.drv.tracked = track_shield(f, amount, dur, "ball curl");
        let r = f.row(f.drv.armor_mr);
        f.buff_resists(r, r, dur);
    }

    fn hit(f: &mut Fight<Self>, _attacker: Option<usize>, _damage: f64) {
        let (broke, keep) = shield_broke(f, f.drv.tracked);
        f.drv.tracked = keep;
        if broke {
            let tg = f.aoe_all();
            for d in tg.iter() {
                f.hit_ability(f.drv.burst, Some(d), "shield break", 1.0);
            }
        }
    }
}

/// Haymaker: the first time he falls below the threshold, a burst of mana;
/// the cast winds up for the heal's duration, healing and then punching a
/// cone.
#[derive(Clone)]
pub struct Sett {
    mana_given: bool,
    threshold: RowId,
    heal_dur: RowId,
    mana: CalcId,
    heal: CalcId,
    punch: CalcId,
}

const SETT_PUNCH: u32 = 1;

impl Driver for Sett {
    const NAME: &'static str = "Sett";
    const LANDS_AT_START: bool = true;

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Sett { mana_given: false, threshold: k.row("ManaHPThreshold"), heal_dur: k.row("HealDuration"),
               mana: k.calc("ManaCalc1"), heal: k.calc("HealthCalc1"), punch: k.calc("PhysicalDamageCalc1") }
    }

    fn hit(f: &mut Fight<Self>, _attacker: Option<usize>, _damage: f64) {
        if !f.drv.mana_given && f.hp_frac() < f.row(f.drv.threshold) {
            f.drv.mana_given = true;
            f.mana += f.calc(f.drv.mana);
        }
    }

    fn cast_time(f: &Fight<Self>) -> f64 {
        f.row(f.drv.heal_dur)
    }

    fn cast(f: &mut Fight<Self>) {
        let heal = f.calc(f.drv.heal);
        f.heal(heal, "wind-up");
        let dur = f.row(f.drv.heal_dur);
        f.after(dur, SETT_PUNCH);
    }

    fn event(f: &mut Fight<Self>, tag: u32) {
        if tag == SETT_PUNCH {
            let tg = f.aoe_all();
            for d in tg.iter() {
                f.hit_ability(f.drv.punch, Some(d), "haymaker", 1.0);
            }
        }
    }
}
