//! Drivers of this slice: ported from tft_kits.py (see mod.rs for the list).
//! The frontline: bodies that heal, shield, stun and hold — Kobuko, Leona,
//! Ornn, Rakan, Rek'Sai, Alistar, Elise, Scuttlecrab, Sejuani, Shen and
//! Fiddlesticks.

use crate::driver::Driver;
use crate::drivers::helpers::{heal_over_time, tick_heal, HasHot, Hot};
use crate::fight::{Deal, Fight, Sel};
use crate::kit::{CalcId, DType, Kit, RowId};
use crate::pyf::{pyint, pymax};
use crate::spec::UnitSpec;

/// Dance of Life: healing spread over the duration, and the next attack is
/// replaced by a bash (the bash is the whole swing, as the text says).
#[derive(Clone)]
pub struct Kobuko {
    hot: Option<Hot>,
    bash: bool,
    duration: RowId,
    heal: CalcId,
    dmg: CalcId,
}

impl HasHot for Kobuko {
    fn hot_mut(&mut self) -> &mut Option<Hot> {
        &mut self.hot
    }
}

impl Driver for Kobuko {
    const NAME: &'static str = "Kobuko";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Kobuko { hot: None, bash: false, duration: k.row("Duration"),
                 heal: k.calc("HealthCalc1"), dmg: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let total = f.calc(f.drv.heal);
        let dur = f.row(f.drv.duration);
        heal_over_time(f, total, dur, "dance of life");
        f.drv.bash = true;
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        if std::mem::replace(&mut f.drv.bash, false) {
            f.hit_ability(f.drv.dmg, Some(target), "bash", 1.0);
        } else {
            f.hit_attack(target, 1.0, "auto");
        }
    }

    fn tick(f: &mut Fight<Self>) {
        tick_heal(f);
    }
}

/// Shield Bash: bonus armor and magic resist from the start of combat,
/// decaying linearly to nothing over the decay duration; the active bashes
/// the target for damage off her armor (so it is worth most early) and
/// stuns it.
#[derive(Clone)]
pub struct Leona {
    peak: f64,
    on: f64,
    decay: RowId,
    stun_dur: RowId,
    generic: CalcId,
    dmg: CalcId,
}

impl Driver for Leona {
    const NAME: &'static str = "Leona";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Leona { peak: 0.0, on: 0.0, decay: k.row("DecayDuration"),
                stun_dur: k.row("StunDuration"), generic: k.calc("GenericCalc1"),
                dmg: k.calc("MagicDamageCalc1") }
    }

    fn init(f: &mut Fight<Self>) {
        let v = f.calc(f.drv.generic);
        f.drv.peak = v;
        f.drv.on = v;
        f.armor_extra += v;
        f.mr_extra += v;
    }

    fn tick(f: &mut Fight<Self>) {
        let left = f.drv.peak * pymax(0.0, 1.0 - f.t / f.row(f.drv.decay));
        f.armor_extra += left - f.drv.on;
        f.mr_extra += left - f.drv.on;
        f.drv.on = left;
    }

    fn cast(f: &mut Fight<Self>) {
        let d = f.target();
        f.hit_ability(f.drv.dmg, d, "bash", 1.0);
        if let Some(i) = d {
            let dur = f.row(f.drv.stun_dur);
            f.stun(&Sel::one(i), dur);
        }
    }
}

/// Bellows Breath: a shield, then a cone over the dummies in reach. The
/// Forge Power quest pays out Artifact Anvils between rounds, so nothing of
/// it lands inside a fight.
#[derive(Clone)]
pub struct Ornn {
    shield_dur: RowId,
    shield: CalcId,
    dmg: CalcId,
}

impl Driver for Ornn {
    const NAME: &'static str = "Ornn";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Ornn { shield_dur: k.row("ShieldDuration"), shield: k.calc("ShieldCalc1"),
               dmg: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let amount = f.calc(f.drv.shield);
        let dur = f.row(f.drv.shield_dur);
        f.shield(amount, dur, "ability", false);
        let tg = f.aoe_all();
        for d in tg.iter() {
            f.hit_ability(f.drv.dmg, Some(d), "cone", 1.0);
        }
    }
}

/// Entrancing Dance: a shield on himself. The decaying attack speed he hands
/// the ally who has dealt the most damage has no ally to land on.
#[derive(Clone)]
pub struct Rakan {
    shield_dur: RowId,
    shield: CalcId,
}

impl Driver for Rakan {
    const NAME: &'static str = "Rakan";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Rakan { shield_dur: k.row("ShieldDuration"), shield: k.calc("ShieldCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let amount = f.calc(f.drv.shield);
        let dur = f.row(f.drv.shield_dur);
        f.shield(amount, dur, "ability", false);
    }
}

/// Uproot: the passive regenerates health every tick of its own tick rate,
/// tripled for a few seconds after a cast; the active lunges out, damaging
/// and knocking up the adjacent dummies.
#[derive(Clone)]
pub struct RekSai {
    /// `f.state["regen_at"]`; None until the first tick sets it (the default
    /// Python reads is the tick rate itself).
    regen_at: Option<f64>,
    boost: f64,
    tick_rate: RowId,
    regen_mult: RowId,
    boost_dur: RowId,
    knockup: RowId,
    regen: CalcId,
    dmg: CalcId,
}

impl Driver for RekSai {
    const NAME: &'static str = "RekSai";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        RekSai { regen_at: None, boost: 0.0, tick_rate: k.row("PassiveTickRate"),
                 regen_mult: k.row("SpellHealthRegenMultiplier"),
                 boost_dur: k.row("SpellMultiplierDuration"), knockup: k.row("KnockupDuration"),
                 regen: k.calc("HealthCalc2"), dmg: k.calc("MagicDamageCalc1") }
    }

    fn tick(f: &mut Fight<Self>) {
        let rate = f.row(f.drv.tick_rate);
        let mut nxt = match f.drv.regen_at {
            Some(v) => v,
            None => rate,
        };
        while f.t >= nxt - 1e-9 {
            let mult = if f.t < f.drv.boost { f.row(f.drv.regen_mult) } else { 1.0 };
            let amount = f.calc(f.drv.regen) * mult;
            f.heal(amount, "burrow regen");
            nxt += rate;
        }
        f.drv.regen_at = Some(nxt);
    }

    fn cast(f: &mut Fight<Self>) {
        f.drv.boost = f.t + f.row(f.drv.boost_dur);
        let tg = f.adjacent(None);
        for d in tg.iter() {
            f.hit_ability(f.drv.dmg, Some(d), "uproot", 1.0);
        }
        let dur = f.row(f.drv.knockup);
        f.stun(&tg, dur);
    }
}

/// Triumphant Roar: heals himself and the two lowest-health allies (the
/// count is in Riot's text, not in a row; ally healing is counted, not
/// simulated), then slams the target for damage and a stun. The cleanse has
/// nothing to remove here.
#[derive(Clone)]
pub struct Alistar {
    stun_dur: RowId,
    heal: CalcId,
    ally: CalcId,
    dmg: CalcId,
}

impl Driver for Alistar {
    const NAME: &'static str = "Alistar";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Alistar { stun_dur: k.row("StunDuration"), heal: k.calc("HealthCalc1"),
                  ally: k.calc("HealthCalc2"), dmg: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let heal = f.calc(f.drv.heal);
        f.heal(heal, "roar");
        let ally = f.calc(f.drv.ally) * 2.0;
        f.heal_ally(ally);
        let d = f.target();
        f.hit_ability(f.drv.dmg, d, "slam", 1.0);
        if let Some(i) = d {
            let dur = f.row(f.drv.stun_dur);
            f.stun(&Sel::one(i), dur);
        }
    }
}

/// Spider Queen: the first cast transforms — bonus max health, and from then
/// on every attack carries bonus magic damage and heals her. Later casts
/// grant decaying attack speed; the row is a multiplier (2.75 = +175%) and
/// decays to nothing, so half of it is applied flat for the duration.
#[derive(Clone)]
pub struct Elise {
    spider: bool,
    hp_buff: RowId,
    decaying_as: RowId,
    as_dur: RowId,
    fangs: CalcId,
    heal: CalcId,
}

impl Driver for Elise {
    const NAME: &'static str = "Elise";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Elise { spider: false, hp_buff: k.row("SpiderHealthBuff"),
                decaying_as: k.row("DecayingAS"), as_dur: k.row("ASBuffDuration"),
                fangs: k.calc("MagicDamageCalc1"), heal: k.calc("HealthCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        if !f.drv.spider {
            f.drv.spider = true;
            let hp = f.row(f.drv.hp_buff);
            f.gain_max_hp(hp);
            return;
        }
        let pct = (f.row(f.drv.decaying_as) - 1.0) / 2.0;
        let dur = f.row(f.drv.as_dur);
        f.buff_as(pct, dur);
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        f.hit_attack(target, 1.0, "auto");
        if f.drv.spider {
            f.hit_ability(f.drv.fangs, Some(target), "spider fangs", 1.0);
            let heal = f.calc(f.drv.heal);
            f.heal(heal, "spider fangs");
        }
    }
}

/// Can You Dig It?: attacks are a dance hitting every adjacent dummy (on-hit
/// effects land on the one it is facing), and the active burrows for
/// durability plus a heal — a share of it up front, the rest over the
/// burrow. The Riftbeast Green Buff heals allies, and the coins are gold.
#[derive(Clone)]
pub struct Scuttlecrab {
    hot: Option<Hot>,
    burrow_dur: RowId,
    up: RowId,
    durability: RowId,
    dance: CalcId,
    heal: CalcId,
}

impl HasHot for Scuttlecrab {
    fn hot_mut(&mut self) -> &mut Option<Hot> {
        &mut self.hot
    }
}

impl Driver for Scuttlecrab {
    const NAME: &'static str = "Scuttlecrab";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Scuttlecrab { hot: None, burrow_dur: k.row("BurrowDuration"),
                      up: k.row("HealPercentInitial"), durability: k.row("BurrowDurability"),
                      dance: k.calc("PhysicalDamageCalc1"), heal: k.calc("HealthCalc1") }
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        // ability damage in the attack's place: on-hit effects on the target,
        // crit only with Precision (the convention for every attack replacement)
        let tg = f.adjacent(None);
        for d in tg.iter() {
            if d == target {
                f.hit_ability(f.drv.dance, Some(d), "dance", 1.0);
            } else {
                let amount = f.calc(f.drv.dance);
                f.deal(amount, DType::Physical, Some(d), "dance", Deal::ABILITY);
            }
        }
    }

    fn cast(f: &mut Fight<Self>) {
        let dur = f.row(f.drv.burrow_dur);
        let total = f.calc(f.drv.heal);
        let up = f.row(f.drv.up);
        let durability = f.row(f.drv.durability);
        f.buff_durability(durability, dur);
        f.heal(total * up, "burrow");
        heal_over_time(f, total * (1.0 - up), dur, "burrow");
    }

    fn tick(f: &mut Fight<Self>) {
        tick_heal(f);
    }
}

/// Sun's Wrath: a shield, then a cone and a line over the dummies in reach.
#[derive(Clone)]
pub struct Sejuani {
    shield_dur: RowId,
    shield: CalcId,
    cone: CalcId,
    line: CalcId,
}

impl Driver for Sejuani {
    const NAME: &'static str = "Sejuani";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Sejuani { shield_dur: k.row("ShieldDuration"), shield: k.calc("ShieldCalc1"),
                  cone: k.calc("MagicDamageCalc1"), line: k.calc("MagicDamageCalc2") }
    }

    fn cast(f: &mut Fight<Self>) {
        let amount = f.calc(f.drv.shield);
        let dur = f.row(f.drv.shield_dur);
        f.shield(amount, dur, "ability", false);
        let tg = f.aoe_all();
        for d in tg.iter() {
            f.hit_ability(f.drv.cone, Some(d), "cone", 1.0);
        }
        let tg = f.aoe_all();
        for d in tg.iter() {
            f.hit_ability(f.drv.line, Some(d), "line", 1.0);
        }
    }
}

/// Ki Barrier: a shield on himself and one on a damaged ally (counted, not
/// simulated), and his next few attacks come faster and carry bonus magic
/// damage. The ally's copy of that buff is not simulated.
#[derive(Clone)]
pub struct Shen {
    ki: i64,
    shield_dur: RowId,
    n_attacks: RowId,
    as_buff: RowId,
    shield: CalcId,
    ally: CalcId,
    strike: CalcId,
}

impl Driver for Shen {
    const NAME: &'static str = "Shen";

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Shen { ki: 0, shield_dur: k.row("MagicShieldDuration"), n_attacks: k.row("NumAttacksBuff"),
               as_buff: k.row("AttackSpeedBuff"), shield: k.calc("ShieldCalc1"),
               ally: k.calc("ShieldCalc2"), strike: k.calc("MagicDamageCalc1") }
    }

    fn cast(f: &mut Fight<Self>) {
        let dur = f.row(f.drv.shield_dur);
        let amount = f.calc(f.drv.shield);
        f.shield(amount, dur, "ability", false);
        let ally = f.calc(f.drv.ally);
        f.shield_ally(ally);
        f.drv.ki = pyint(f.row(f.drv.n_attacks));
        f.as_extra = f.row(f.drv.as_buff);
        f.as_extra_until = 1e9;
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        f.hit_attack(target, 1.0, "auto");
        let n = f.drv.ki;
        if n > 0 {
            f.drv.ki = n - 1;
            f.hit_ability(f.drv.strike, Some(target), "ki strike", 1.0);
            if n == 1 {
                f.as_extra_until = f.t;
            }
        }
    }
}

/// Harvest: strips flat magic resist off the nearest few dummies, then
/// channels for the cast duration, draining that damage out of each of them
/// and that healing into himself over the channel.
#[derive(Clone)]
pub struct Fiddlesticks {
    hot: Option<Hot>,
    cast_dur: RowId,
    n_targets: RowId,
    mr_red: RowId,
    drain: CalcId,
    heal: CalcId,
}

impl HasHot for Fiddlesticks {
    fn hot_mut(&mut self) -> &mut Option<Hot> {
        &mut self.hot
    }
}

impl Driver for Fiddlesticks {
    const NAME: &'static str = "Fiddlesticks";
    const LANDS_AT_START: bool = true;

    fn new(k: &Kit, _u: &UnitSpec) -> Self {
        Fiddlesticks { hot: None, cast_dur: k.row("CastDuration"), n_targets: k.row("NumTargets"),
                       mr_red: k.row("MRReduction"), drain: k.calc("MagicDamageCalc1"),
                       heal: k.calc("HealthCalc1") }
    }

    fn cast_time(f: &Fight<Self>) -> f64 {
        f.row(f.drv.cast_dur)
    }

    fn cast(f: &mut Fight<Self>) {
        let dur = f.row(f.drv.cast_dur);
        let tg = f.aoe(Some(f.row(f.drv.n_targets)), false);
        for d in tg.iter() {
            let red = f.row(f.drv.mr_red);
            f.dm(d).mr_flat += red;
            f.dot_ability(f.drv.drain, Some(d), dur, "drain", 1.0);
        }
        let total = f.calc(f.drv.heal);
        heal_over_time(f, total, dur, "drain");
    }

    fn tick(f: &mut Fight<Self>) {
        tick_heal(f);
    }
}
