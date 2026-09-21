//! Drivers of this slice: ported from tft_kits.py (see mod.rs for the list).
//! The carries — marksmen, casters and specialists whose dummies never hit
//! back: poisons that spread, axes that cash their bleeds, channels,
//! bounces, charges and attack replacements.

use crate::driver::Driver;
use crate::fight::{Deal, Fight, TICK_S};
use crate::fx::Form;
use crate::kit::{CalcId, DType, Kit, RowId};
use crate::pyf::{pyint, pymax, pymin};
use crate::spec::UnitSpec;

/// Noxious Blast: poison over fifteen seconds on the target and the
/// nearest unpoisoned enemy, independently of area coverage. Poisons stack.
#[derive(Clone)]
pub struct Cassiopeia {
    dur: RowId,
    poison: CalcId,
}

impl Driver for Cassiopeia {
    const NAME: &'static str = "Cassiopeia";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Cassiopeia { dur: k.row("Duration"), poison: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let dur = f.row(f.drv.dur);
        let d = f.target();
        f.dot_ability(f.drv.poison, d, dur, "poison", 1.0);
        let others = f.alive();
        let mut first = None;
        for x in others.iter() {
            // Poisoned means a live poison, not a permanent history mark.
            // The target list supplies the existing nearest-first proxy.
            if Some(x) != d && !f.d(x).dots.iter().any(|dot|
                dot.src == "poison" && dot.until > f.t) {
                first = Some(x);
                break;
            }
        }
        if let Some(x) = first {
            f.dot_ability(f.drv.poison, Some(x), dur, "poison", 1.0);
        }
    }
}

/// Whirling Death: attacks rotate across the dummies and bleed them; a
/// spinning axe (expected value of its chance) hits harder and bleeds
/// twice; the active axes hit the line, cash the bleeds and return.
#[derive(Clone)]
pub struct Draven {
    n: i64,
    ratio: RowId,
    stacks: RowId,
    bleed_dur: RowId,
    chance: CalcId,
    bleed: CalcId,
    axes: CalcId,
    axes2: CalcId,
}

impl Driver for Draven {
    const NAME: &'static str = "Draven";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Draven { n: 0, ratio: k.row("SpinningAxeDamageRatio"),
                 stacks: k.row("SpinningAxeBleedStacks"), bleed_dur: k.row("BleedDuration"),
                 chance: k.calc("GenericCalc1"), bleed: k.calc("PhysicalDamageCalc1"),
                 axes: k.calc("PhysicalDamageCalc3"), axes2: k.calc("GenericCalc2") }
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        let al = f.alive();
        let n = f.drv.n;
        f.drv.n = n + 1;
        let d = if f.clump { al.get((n as usize) % al.len()) } else { target };
        let p = f.calc(f.drv.chance);
        let ratio = f.row(f.drv.ratio);
        f.hit_attack(d, 1.0 + p * (ratio - 1.0), "auto");
        let bleeds = 1.0 + p * (f.row(f.drv.stacks) - 1.0);
        if f.d(d).alive {
            let dur = f.row(f.drv.bleed_dur);
            f.dot_ability(f.drv.bleed, Some(d), dur, "bleed axes", bleeds);
        }
    }

    fn cast(f: &mut Fight<Self>) {
        let tg = f.aoe_all();
        for d in tg.iter() {
            f.hit_ability(f.drv.axes, Some(d), "giant axes", 1.0);
            if !f.d(d).alive {
                continue;
            }
            let t = f.t;
            let mut rest = 0.0;
            for dot in f.d(d).dots.iter() {
                if dot.src == "bleed axes" {
                    rest += dot.dps * pymax(0.0, dot.until - t);
                }
            }
            f.dm(d).dots.retain(|x| x.src != "bleed axes");
            if rest > 0.0 {
                // "instantly dealing the remaining damage": the same ability
                // damage the ticks would have paid, so it crits with
                // Precision exactly as they do (the rest is kept pre-crit).
                f.deal(rest, DType::Physical, Some(d), "giant axes", Deal::ABILITY);
            }
        }
        // Both passes follow the selected line even if the outward pass
        // kills its anchor. Dead recipients are skipped by the hit helper.
        for d in tg.iter() {
            f.hit_ability_typed(f.drv.axes2, Some(d), "giant axes", 1.0, DType::Physical);
        }
    }
}

/// Forest's Flurry: a hit and stacking attack speed; every fourth cast
/// spends the attack speed on a piercing blast.
#[derive(Clone)]
pub struct Ezreal {
    casts: i64,
    num_casts: RowId,
    fall: RowId,
    floor: RowId,
    bolt: CalcId,
    blast: CalcId,
    speed: CalcId,
}

impl Driver for Ezreal {
    const NAME: &'static str = "Ezreal";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Ezreal { casts: 0, num_casts: k.row("NumCasts"), fall: k.row("DamageReductionPerHit"),
                 floor: k.row("MinDamagePercent"), bolt: k.calc("PhysicalDamageCalc1"),
                 blast: k.calc("PhysicalDamageCalc2"), speed: k.calc("AttackSpeedCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let n = f.drv.casts + 1;
        f.drv.casts = n;
        f.hit_ability(f.drv.bolt, f.target(), "ability", 1.0);
        if n % pyint(f.row(f.drv.num_casts)) == 0 {
            f.as_extra = 0.0;
            f.as_extra_until = 0.0;
            let (fall, floor) = (f.row(f.drv.fall), f.row(f.drv.floor));
            let tg = f.aoe_all();
            for (i, d) in tg.iter().enumerate() {
                let mult = pymax(floor, 1.0 - fall * (i as f64));
                f.hit_ability(f.drv.blast, Some(d), "blast", mult);
            }
        } else {
            let v = f.calc(f.drv.speed);
            f.as_extra += v;
            f.as_extra_until = 1e9;
        }
    }
}

/// Belchy Bubble. Ability-power form: a hit and a poison cloud around it.
/// Attack-damage form: a hit and a splash on the dummies a hex away (its
/// slow does nothing here). With the Riftbeast Alpha Mark (Purple Buff),
/// ability power — or attack damage in the other form — every five seconds.
#[derive(Clone)]
pub struct Gromp {
    buff_at: Option<f64>,
    poison_dur: RowId,
    timer: RowId,
    timed_ad: RowId,
    timed_ap: RowId,
    phys1: CalcId,
    phys2: CalcId,
    magic1: CalcId,
    magic2: CalcId,
}

impl Driver for Gromp {
    const NAME: &'static str = "Gromp";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Gromp { buff_at: None, poison_dur: k.row("PoisonDurationAP"), timer: k.row("TraitTimer"),
                timed_ad: k.row("TraitTimedAD"), timed_ap: k.row("TraitTimedAP"),
                phys1: k.calc("PhysicalDamageCalc1"), phys2: k.calc("PhysicalDamageCalc2"),
                magic1: k.calc("MagicDamageCalc1"), magic2: k.calc("MagicDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        if f.sheet.form == Some(Form::AD) {
            // "within a 1 hex radius": the target too, as the poison cloud does
            let tg = f.adjacent(None);
            f.hit_ability(f.drv.phys1, f.target(), "ability", 1.0);
            for d in tg.iter() {
                f.hit_ability(f.drv.phys2, Some(d), "splash", 1.0);
            }
            return;
        }
        let tg = f.aoe_all();
        f.hit_ability(f.drv.magic1, f.target(), "ability", 1.0);
        for d in tg.iter() {
            let dur = f.row(f.drv.poison_dur);
            f.dot_ability(f.drv.magic2, Some(d), dur, "cloud", 1.0);
        }
    }

    fn tick(f: &mut Fight<Self>) {
        if f.fx.riftbeast {
            let nxt = match f.drv.buff_at {
                Some(x) => x,
                None => f.row(f.drv.timer),
            };
            if f.t >= nxt - 1e-9 {
                if f.sheet.form == Some(Form::AD) {
                    let v = f.row(f.drv.timed_ad);
                    f.ad_extra += v;
                } else {
                    let v = f.row(f.drv.timed_ap);
                    f.ap_stack += v * 100.0;
                }
                f.drv.buff_at = Some(nxt + f.row(f.drv.timer));
            }
        }
    }
}

/// Karmic Bond: damage over a short tether, then a burst around the target.
#[derive(Clone)]
pub struct Karma {
    bursts: Vec<(f64, usize)>,
    tether_dur: RowId,
    tether: CalcId,
    burst: CalcId,
}

impl Driver for Karma {
    const NAME: &'static str = "Karma";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Karma { bursts: Vec::new(), tether_dur: k.row("TetherDuration"),
                tether: k.calc("MagicDamageCalc1"), burst: k.calc("MagicDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let d = match f.target() {
            Some(d) => d,
            None => return,
        };
        let dur = f.row(f.drv.tether_dur);
        f.dot_ability(f.drv.tether, Some(d), dur, "tether", 1.0);
        let at = f.t + dur;
        f.drv.bursts.push((at, d));
    }

    fn tick(f: &mut Fight<Self>) {
        let t = f.t;
        let mut i = 0;
        while i < f.drv.bursts.len() {
            let (at, center) = f.drv.bursts[i];
            if t < at - 1e-9 {
                i += 1;
                continue;
            }
            f.drv.bursts.remove(i);
            // The tether's recipient is its burst center, never whichever
            // enemy becomes the current attack target. Whether the game
            // cancels the burst after early death remains unresolved; keep
            // the existing delayed burst in the retained abstract geometry.
            let tg = f.aoe_around(center, None, false);
            for d in tg.iter() {
                f.hit_ability(f.drv.burst, Some(d), "burst", 1.0);
            }
        }
    }
}

/// Taste Their Fear: leaps to the farthest dummy in reach; an isolated
/// target takes more and refunds mana. Spread out, every target is isolated.
#[derive(Clone)]
pub struct KhaZix {
    grant: RowId,
    leap: CalcId,
    isolated: CalcId,
}

impl Driver for KhaZix {
    const NAME: &'static str = "KhaZix";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        KhaZix { grant: k.row("IsolateManaGrant"), leap: k.calc("MagicDamageCalc1"),
                 isolated: k.calc("MagicDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let al = f.alive();
        let d = if f.clump { al.last() } else { f.target() };
        if f.clump && al.len() > 1 {
            f.hit_ability(f.drv.leap, d, "ability", 1.0);
        } else {
            f.hit_ability(f.drv.isolated, d, "isolated", 1.0);
            let m = f.row(f.drv.grant);
            // The spell's refund bypasses its own lock, while retaining
            // all-source mana multipliers such as Adaptive Helm.
            f.gain_mana_opt(m, false);
        }
    }
}

/// Mirror Image: a hit, and less to the adjacent dummies.
#[derive(Clone)]
pub struct LeBlanc {
    main: CalcId,
    splash: CalcId,
}

impl Driver for LeBlanc {
    const NAME: &'static str = "LeBlanc";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        LeBlanc { main: k.calc("MagicDamageCalc1"), splash: k.calc("MagicDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let tg = f.aoe(None, true);
        f.hit_ability(f.drv.main, f.target(), "ability", 1.0);
        for d in tg.iter() {
            f.hit_ability(f.drv.splash, Some(d), "splash", 1.0);
        }
    }
}

/// Final Spark: a laser through the line, weaker per dummy passed.
#[derive(Clone)]
pub struct Lux {
    fall: RowId,
    floor: RowId,
    laser: CalcId,
}

impl Driver for Lux {
    const NAME: &'static str = "Lux";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Lux { fall: k.row("DamageReductionPerUnit"), floor: k.row("MinimumFalloffDamageRatio"),
              laser: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let (fall, floor) = (f.row(f.drv.fall), f.row(f.drv.floor));
        let tg = f.aoe_all();
        for (i, d) in tg.iter().enumerate() {
            let mult = pymax(floor, 1.0 - fall * (i as f64));
            f.hit_ability(f.drv.laser, Some(d), "ability", mult);
        }
    }
}

/// Azure Laser: "begin consuming {PercentManaPerSecond} max Mana per second
/// and channeling a laser" — the channel drains her bar, overflow included,
/// ticking damage and flat magic-resist reduction on the target until the
/// bar is empty. There is no mana lock: whatever comes in while she channels,
/// regen above all, is drained too and lengthens the laser (TFTraits: "No
/// mana lock: mana keeps coming in during the ability, which drains max mana
/// and ends at 0 mana. Mana regen makes it last longer." — third party,
/// adopted like Azir's lock, not verified in game). She does not attack
/// while channeling. With the Riftbeast Alpha Mark, mana regen accrues per
/// seconds channeled.
#[derive(Clone)]
pub struct Pebbles {
    /// From the cast until the bar runs dry.
    channeling: bool,
    /// The laser and the drain are settled through this time.
    last: f64,
    channeled: f64,
    /// The end event that still counts: every newer projection replaces it.
    end_tag: u32,
    per_second: RowId,
    mr_reduction: RowId,
    trait_seconds: RowId,
    trait_regen: RowId,
    laser: CalcId,
}

impl Pebbles {
    /// Mana the channel consumes per second.
    fn drain(f: &Fight<Self>) -> f64 {
        f.row(f.drv.per_second) * f.sheet.mana_max
    }

    /// Mana regeneration per second, as the engine's ticks pay it.
    fn income(f: &Fight<Self>) -> f64 {
        f.fx.mana_regen * f.fx.mana_mult
    }

    /// `span` seconds of laser: the damage, the magic-resist strip and the
    /// Teal Buff's channel time.
    fn pay(f: &mut Fight<Self>, span: f64) {
        if span <= 0.0 {
            return;
        }
        let d = match f.target() {
            Some(d) => d,
            None => return,
        };
        f.hit_ability(f.drv.laser, Some(d), "laser", span);
        let red = f.row(f.drv.mr_reduction) * span;
        f.dm(d).mr_flat += red;
        if f.fx.riftbeast {
            f.drv.channeled += span;
            let per = f.row(f.drv.trait_seconds);
            while f.drv.channeled >= per {
                f.drv.channeled -= per;
                let regen = f.row(f.drv.trait_regen);
                f.fx.mana_regen += regen;
            }
        }
    }

    /// Hold her attacks — and the engine's own cast check, the draining bar
    /// starts full — until the bar is due to run dry, and queue that exact
    /// instant once no tick can come before it. The engine pays regen in a
    /// lump on its ticks, so between them the bar is the mana on the books
    /// less what has drained since, plus the regen accrued and not yet paid.
    fn project(f: &mut Fight<Self>) {
        f.drv.end_tag = f.drv.end_tag.wrapping_add(1);
        let net = Self::drain(f) - Self::income(f);
        if net <= 0.0 {
            // Regen keeps pace with the drain: the laser never stops.
            f.casting_until = 1e9;
            return;
        }
        let left = pymax(0.0, f.mana / net - (f.t - f.drv.last));
        f.casting_until = f.t + left;
        if left <= TICK_S {
            f.after(left, f.drv.end_tag);
        }
    }
}

impl Driver for Pebbles {
    const NAME: &'static str = "Pebbles";
    const LANDS_AT_START: bool = true;

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Pebbles { channeling: false, last: 0.0, channeled: 0.0, end_tag: 0,
                  per_second: k.row("PercentManaPerSecond"), mr_reduction: k.row("MRReduction"),
                  trait_seconds: k.row("TraitChannelSecondsTooltip"),
                  trait_regen: k.row("TraitManaRegenTooltip"), laser: k.calc("MagicDamageCalc1") }
    }

    /// A bare bar's channel. `cast` replaces the window the engine derives
    /// from it with the drain's own.
    fn cast_time(f: &Fight<Self>) -> f64 {
        1.0 / f.row(f.drv.per_second)
    }

    fn cast(f: &mut Fight<Self>) {
        // The engine has taken the cast's cost off the bar and kept the
        // overflow; this ability spends the bar by draining it instead.
        f.mana += f.sheet.mana_max;
        f.drv.channeling = true;
        f.drv.last = f.t;
        // No mana lock: regen and procs keep coming in from the cast on.
        f.lock_until = f.t;
        Self::project(f);
    }

    fn tick(f: &mut Fight<Self>) {
        if !f.drv.channeling {
            return;
        }
        if !f.alive_unit {
            // A shared fight can kill her mid-channel: the laser dies with her.
            f.drv.channeling = false;
            return;
        }
        let span = f.t - f.drv.last;
        if span > 0.0 {
            // The engine has just paid this interval's regen into the bar.
            let drain = Self::drain(f);
            let bar = f.mana - drain * span;
            f.drv.last = f.t;
            if bar <= 0.0 {
                // Dry inside this interval with its end event held up (a
                // stun in a shared fight) or a rounding away: the bar fell
                // `net` a second, so it was overdrawn for `over` seconds.
                // The regen accrued after that stays on the bar.
                let net = drain - Self::income(f);
                let over = if net > 0.0 { pymin(span, -bar / net) } else { span };
                f.drv.channeling = false;
                f.mana = bar + drain * over;
                f.casting_until = f.t;
                Self::pay(f, span - over);
                return;
            }
            f.mana = bar;
            Self::pay(f, span);
        }
        Self::project(f);
    }

    /// The bar runs dry: the last stretch of laser, then she attacks again.
    fn event(f: &mut Fight<Self>, tag: u32) {
        if !f.drv.channeling || tag != f.drv.end_tag {
            return;
        }
        let span = f.t - f.drv.last;
        let net = Self::drain(f) - Self::income(f);
        if net <= 0.0 || f.mana / net - span > 1e-6 {
            // Mana came in since the projection (a takedown's): not yet.
            Self::project(f);
            return;
        }
        // (a stun in a shared fight can hold this event past the dry bar)
        let ran = pymin(span, f.mana / net);
        f.drv.channeling = false;
        f.drv.last = f.t;
        // The regen accrued since the last tick went into the drain; the
        // engine's next tick pays only for the time after this instant.
        f.mana = 0.0;
        f.lock_until = f.t;
        f.casting_until = f.t;
        Self::pay(f, ran);
    }
}

/// Boomerang Blade: a hit, then bounces between nearby dummies (the first
/// bounce leaves the target it just hit); a kill adds bounces. Alone,
/// there is nothing to bounce to.
#[derive(Clone)]
pub struct Sivir {
    bounces: RowId,
    kill_bounces: RowId,
    blade: CalcId,
    bounce: CalcId,
}

impl Driver for Sivir {
    const NAME: &'static str = "Sivir";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Sivir { bounces: k.row("NumBounces"), kill_bounces: k.row("BonusKillBounces"),
                blade: k.calc("PhysicalDamageCalc1"), bounce: k.calc("PhysicalDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let primary = match f.target() {
            Some(d) => d,
            None => return,
        };
        f.hit_ability(f.drv.blade, Some(primary), "ability", 1.0);
        if !f.clump {
            return;
        }
        let mut bounces = pyint(f.row(f.drv.bounces));
        if !f.d(primary).alive {
            bounces += pyint(f.row(f.drv.kill_bounces));
        }
        let mut previous = primary;
        let mut i: i64 = 0;
        while i < bounces {
            let al = f.alive();
            // A bounce leaves the enemy just hit, even if that enemy died.
            // Retain the existing target-order proxy for the nearby chain.
            let d = match al.iter().find(|&d| d > previous)
                .or_else(|| al.iter().find(|&d| d != previous)) {
                Some(d) => d,
                None => break,
            };
            f.hit_ability(f.drv.bounce, Some(d), "bounces", 1.0);
            if !f.d(d).alive {
                bounces += pyint(f.row(f.drv.kill_bounces));
            }
            previous = d;
            i += 1;
        }
    }
}

/// Starcall: a star on the target; a target already starred takes three
/// more, smaller ones.
#[derive(Clone)]
pub struct Soraka {
    extra: RowId,
    star: CalcId,
    small: CalcId,
}

impl Driver for Soraka {
    const NAME: &'static str = "Soraka";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Soraka { extra: k.row("NumAdditionalStars"), star: k.calc("MagicDamageCalc1"),
                 small: k.calc("MagicDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let d = match f.target() {
            Some(d) => d,
            None => return,
        };
        f.hit_ability(f.drv.star, Some(d), "ability", 1.0);
        if f.d(d).mark {
            let n = pyint(f.row(f.drv.extra));
            for _ in 0..n {
                if f.d(d).alive {
                    f.hit_ability(f.drv.small, Some(d), "extra stars", 1.0);
                }
            }
        }
        f.dm(d).mark = true;
    }
}

/// Explosive Charge: attack speed for four seconds, then a blast that grows
/// with the attacks made meanwhile, split over the dummies in reach. She
/// keeps attacking through the cast.
#[derive(Clone)]
pub struct Tristana {
    /// (when the charge blows, the attacks made under it) while it runs.
    charge: Option<(f64, i64)>,
    duration: RowId,
    speed: CalcId,
    blast: CalcId,
    per_attack: CalcId,
}

impl Driver for Tristana {
    const NAME: &'static str = "Tristana";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Tristana { charge: None, duration: k.row("Duration"), speed: k.calc("AttackSpeedCalc1"),
                   blast: k.calc("PhysicalDamageCalc1"), per_attack: k.calc("PhysicalDamageCalc2") }
    }

    fn cast_time(_f: &Fight<Self>) -> f64 {
        0.0
    }

    fn cast(f: &mut Fight<Self>) {
        let dur = f.row(f.drv.duration);
        let pct = f.calc(f.drv.speed);
        f.buff_as(pct, dur);
        f.drv.charge = Some((f.t + dur, 0));
        // "each attack while casting": mana-locked for as long as the
        // charge runs and no longer (TFTraits: "The mana lock lasts the
        // 4.00 s the effect runs. Attacks continue." — a third-party
        // statement, adopted like Azir's, not verified in game).
        f.lock_until = pymax(f.lock_until, f.t + dur);
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        f.hit_attack(target, 1.0, "auto");
        let t = f.t;
        if let Some(ch) = f.drv.charge.as_mut() {
            if t < ch.0 {
                ch.1 += 1;
            }
        }
    }

    fn tick(f: &mut Fight<Self>) {
        let ch = match f.drv.charge {
            Some(ch) => ch,
            None => return,
        };
        if f.t >= ch.0 - 1e-9 {
            f.drv.charge = None;
            let tg = f.aoe_all();
            let n = tg.len() as f64;
            for d in tg.iter() {
                f.hit_ability(f.drv.blast, Some(d), "explosion", 1.0 / n);
                f.hit_ability(f.drv.per_attack, Some(d), "explosion", (ch.1 as f64) / n);
            }
        }
    }
}

/// Piercing Arrow: winds up, then a line shot weaker per dummy passed.
#[derive(Clone)]
pub struct Varus {
    spell_duration: RowId,
    fall: RowId,
    floor: RowId,
    arrow: CalcId,
}

impl Driver for Varus {
    const NAME: &'static str = "Varus";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Varus { spell_duration: k.row("SpellDuration"), fall: k.row("DamageReductionPerHit"),
                floor: k.row("MinDamagePercent"), arrow: k.calc("PhysicalDamageCalc1") }
    }

    fn cast_time(f: &Fight<Self>) -> f64 {
        f.row(f.drv.spell_duration)
    }

    fn cast(f: &mut Fight<Self>) {
        let (fall, floor) = (f.row(f.drv.fall), f.row(f.drv.floor));
        let tg = f.aoe_all();
        for (i, d) in tg.iter().enumerate() {
            let mult = pymax(floor, 1.0 - fall * (i as f64));
            f.hit_ability(f.drv.arrow, Some(d), "ability", mult);
        }
    }
}

/// Deadly Plumage: attack speed for five attacks, which become feathers
/// that deal the ability's damage (with on-hit effects; crit only with
/// Precision, like every attack replacement) and strip flat armor. She is
/// casting for as long as the feathers last — no mana until they are spent
/// — or a 0/50 marksman at 10 mana an attack would never leave them. The
/// lock ends with the last feather (TFTraits: "it lasts 5 empowered
/// attacks"; third party, adopted like Azir's, not verified in game).
#[derive(Clone)]
pub struct Xayah {
    feathers: i64,
    num_attacks: RowId,
    attack_speed: RowId,
    feather: CalcId,
    strip: CalcId,
}

impl Driver for Xayah {
    const NAME: &'static str = "Xayah";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Xayah { feathers: 0, num_attacks: k.row("NumAttacks"), attack_speed: k.row("AttackSpeed"),
                feather: k.calc("PhysicalDamageCalc1"), strip: k.calc("GenericCalc1") }
    }

    fn cast_time(_f: &Fight<Self>) -> f64 {
        0.0
    }

    fn cast(f: &mut Fight<Self>) {
        f.drv.feathers = pyint(f.row(f.drv.num_attacks));
        let speed = f.row(f.drv.attack_speed);
        f.as_extra = speed - 1.0;
        f.as_extra_until = 1e9;
        f.lock_until = 1e9;
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        let n = f.drv.feathers;
        if n <= 0 {
            f.hit_attack(target, 1.0, "auto");
            return;
        }
        f.drv.feathers = n - 1;
        f.hit_ability(f.drv.feather, Some(target), "feathers", 1.0);
        let strip = f.calc(f.drv.strip);
        f.dm(target).armor_flat += strip;
        if n - 1 == 0 {
            f.as_extra_until = f.t;
            // Attack mana is processed before this hook, so the last
            // feather grants none; regen resumes for the time after it.
            f.lock_until = f.t;
        }
    }
}

/// Cultivation of Spirit: a hit, then a split to two nearby dummies.
#[derive(Clone)]
pub struct Yunara {
    secondary: RowId,
    main: CalcId,
    split: CalcId,
}

impl Driver for Yunara {
    const NAME: &'static str = "Yunara";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Yunara { secondary: k.row("NumSecondaryTargets"), main: k.calc("PhysicalDamageCalc1"),
                 split: k.calc("PhysicalDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let tg = f.aoe(Some(f.row(f.drv.secondary)), true);
        f.hit_ability(f.drv.main, f.target(), "ability", 1.0);
        for d in tg.iter() {
            f.hit_ability(f.drv.split, Some(d), "split", 1.0);
        }
    }
}
