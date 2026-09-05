"""Set 18 unit drivers: the shape of each ability — how many targets, over
how long, what repeats — with every number read from the unit's own
calculations and curve rows in the snapshot. A driver is a few lines; the
engine (tft.Fight) does the mana cycle, crit, resists, amps and on-hit
effects.

Conventions: `f.target()` is the dummy being attacked; `f.aoe(n)` is who an
area ability reaches — every standing dummy (up to n) in the clump, only
the current target when spread out. Ability damage goes through
`f.hit_ability(calc, target, ...)`, which resolves the calc at the unit's
current AD/AP and applies the calc's damage type.
"""

import tft
from tft import Driver

SET = 18


class Simple(Driver):
    """Single-target cast: one calc on the current target."""
    calc = "MagicDamageCalc1"

    def cast(self, f):
        f.hit_ability(self.calc, f.target())


class Ahri(Driver):
    """Spirit Bomb: area damage around the densest spot, falling off per hex
    from the epicenter. In the clump the other dummies sit one hex out."""
    def cast_time(self, f):
        return f.row("ChannelTime")

    def cast(self, f):
        fall = f.row("HexPercentDamageFalloffTooltip")
        for i, d in enumerate(f.aoe()):
            f.hit_ability("MagicDamageCalc1", d, mult=1.0 if i == 0 else 1.0 - fall)


class Ashe(Driver):
    """Spirit Rift: an arrow through the line, weaker per dummy hit, then a
    trail that ticks attack-damage plus a share of max health for a few
    seconds on everyone standing in it."""
    def cast(self, f):
        fall, floor = f.row("DamageFalloffPerEnemy"), f.row("MinDamagePercent")
        tg = f.aoe()
        for i, d in enumerate(tg):
            f.hit_ability("PhysicalDamageCalc1", d, mult=max(floor, 1.0 - fall * i))
        dur = f.row("RiftDuration")
        pct = f.row("MaxHealthDamagePerSecond")
        for d in tg:
            if d.alive:
                f.dot_ability("PhysicalDamageCalc2", d, dur, src="trail", mult=dur)
                f.dot(pct * d.max_hp * dur, dur, "physical", d, "trail")


class Akali(Driver):
    """Kunai Strike (attack variant): damage, more if the target burns;
    a kill casts it again at reduced damage."""
    def cast(self, f):
        mult = 1.0
        for _ in range(4):
            d = f.target()
            if d is None:
                break
            burning = (f.t <= d.burn_until and d.burn_pct > 0) or \
                (d.burn_stack > 0 and f.t <= d.marks.get("burn_stack_until", 0.0))
            f.hit_ability("PhysicalDamageCalc1", d, mult=mult)
            if burning and d.alive:
                f.hit_ability("PhysicalDamageCalc2", d, src="burning bonus", mult=mult)
            if d.alive:
                break
            mult *= f.row("RecastDamageReduction")


class Alune(Driver):
    """Moonfall: nine shards over the nearest three; every fourth cast the
    moon is full and crashes on everyone instead (the Attuned phase cycle
    is assumed to be four casts long)."""
    def cast(self, f):
        n = f.state.get("casts", 0) + 1
        f.state["casts"] = n
        if n % 4 == 0:
            tg = f.aoe()
            for d in tg:
                f.hit_ability("MagicDamageCalc2", d, src="full moon", mult=1.0 / len(tg))
            return
        tg = f.aoe(count=f.row("NumEnemies"))
        for i in range(int(f.row("NumMoonshards"))):
            f.hit_ability("MagicDamageCalc1", tg[i % len(tg)], src="moonshards")


class Aphelios(Driver):
    """Moonlight's Onslaught: swipes for two seconds (more with bonus attack
    speed), every third one an attack for on-hit purposes, then a blast
    split among the dummies in reach."""
    def cast_time(self, f):
        return f.row("Duration")

    def cast(self, f):
        s = f.sheet
        bonus_as = f.attack_speed() / s.base_as - 1.0
        swipes = int(f.row("NumAttacksBase")) + int(max(0.0, bonus_as) / f.row("AS_NeededForExtraSwipe"))
        d = f.target()
        per_auto = int(f.row("NumSwipesTriggerSimulatedAutos"))
        for i in range(swipes):
            if d is None or not d.alive:
                d = f.target()
                if d is None:
                    return
            f.hit_ability("PhysicalDamageCalc1", d, src="swipes")
            if (i + 1) % per_auto == 0 and d.alive:
                f.on_hit_effects(d, False)
        tg = f.aoe()
        for d in tg:
            f.hit_ability("PhysicalDamageCalc2", d, src="blast", mult=1.0 / len(tg))


class Caitlyn(Driver):
    """Headshot: every third attack is replaced by an ability hit. Ammo,
    not mana: mana items do nothing."""
    def init(self, f):
        f.sheet.mana_max = 0
        f.state["n"] = 0

    def attack(self, f, target):
        f.state["n"] += 1
        if f.state["n"] % (int(f.row("AttacksBeforeHeadshot")) + 1) == 0:
            f.hit_ability("PhysicalDamageCalc1", target, src="headshot")
        else:
            f.hit_attack(target)


class Cassiopeia(Driver):
    """Noxious Blast: poison over fifteen seconds on the target and, in the
    clump, the nearest unpoisoned dummy. Poisons stack."""
    def cast(self, f):
        dur = f.row("Duration")
        d = f.target()
        f.dot_ability("MagicDamageCalc1", d, dur, src="poison")
        d.marks["poisoned"] = True
        others = [x for x in f.aoe(exclude_primary=True) if not x.marks.get("poisoned")]
        if others:
            f.dot_ability("MagicDamageCalc1", others[0], dur, src="poison")
            others[0].marks["poisoned"] = True


class Draven(Driver):
    """Whirling Death: attacks rotate across the dummies and bleed them;
    a spinning axe (expected value of its chance) hits harder and bleeds
    twice; the active axes hit the line, cash the bleeds and return."""
    def attack(self, f, target):
        al = f.alive()
        n = f.state.get("n", 0)
        f.state["n"] = n + 1
        d = al[n % len(al)] if f.clump else target
        p = f.calc("GenericCalc1")
        ratio = f.row("SpinningAxeDamageRatio")
        f.hit_attack(d, mult=1.0 + p * (ratio - 1.0))
        bleeds = 1.0 + p * (f.row("SpinningAxeBleedStacks") - 1.0)
        if d.alive:
            f.dot_ability("PhysicalDamageCalc1", d, f.row("BleedDuration"), src="bleed axes", mult=bleeds)

    def cast(self, f):
        for d in f.aoe():
            f.hit_ability("PhysicalDamageCalc3", d, src="giant axes")
            if not d.alive:
                continue
            rest = 0.0
            for dot in d.dots:
                if dot[3] == "bleed axes":
                    rest += dot[0] * max(0.0, dot[1] - f.t)
            d.dots = [x for x in d.dots if x[3] != "bleed axes"]
            if rest > 0:
                f.deal(rest, "physical", d, "giant axes", crit=False)
        for d in f.aoe():
            f.hit_ability("GenericCalc2", d, src="giant axes", dtype="physical")


class Ezreal(Driver):
    """Forest's Flurry: a hit and stacking attack speed; every fourth cast
    spends the attack speed on a piercing blast."""
    def cast(self, f):
        n = f.state.get("casts", 0) + 1
        f.state["casts"] = n
        f.hit_ability("PhysicalDamageCalc1", f.target())
        if n % int(f.row("NumCasts")) == 0:
            f.as_extra = 0.0
            f.as_extra_until = 0.0
            fall, floor = f.row("DamageReductionPerHit"), f.row("MinDamagePercent")
            for i, d in enumerate(f.aoe()):
                f.hit_ability("PhysicalDamageCalc2", d, src="blast", mult=max(floor, 1.0 - fall * i))
        else:
            f.as_extra += f.calc("AttackSpeedCalc1")
            f.as_extra_until = 1e9


class Gromp(Driver):
    """Belchy Bubble (magic variant): a hit and a poison cloud around it.
    With the Riftbeast Alpha Mark, ability power every five seconds."""
    def cast(self, f):
        f.hit_ability("MagicDamageCalc1", f.target())
        for d in f.aoe():
            f.dot_ability("MagicDamageCalc2", d, f.row("PoisonDurationAP"), src="cloud")

    def tick(self, f):
        if f.fx.riftbeast:
            nxt = f.state.get("buff_at", f.row("TraitTimer"))
            if f.t >= nxt - 1e-9:
                f.ap_stack += f.row("TraitTimedAP") * 100.0
                f.state["buff_at"] = nxt + f.row("TraitTimer")


class Karma(Driver):
    """Karmic Bond: damage over a short tether, then a burst around the
    target."""
    def cast(self, f):
        d = f.target()
        dur = f.row("TetherDuration")
        f.dot_ability("MagicDamageCalc1", d, dur, src="tether")
        f.state.setdefault("bursts", []).append(f.t + dur)

    def tick(self, f):
        due = [b for b in f.state.get("bursts", []) if f.t >= b - 1e-9]
        if due:
            f.state["bursts"] = [b for b in f.state["bursts"] if f.t < b - 1e-9]
            for _ in due:
                for d in f.aoe():
                    f.hit_ability("MagicDamageCalc2", d, src="burst")


class Kayle(Driver):
    """Solar Judgement: no mana; each star level unlocks another passive on
    her attacks — bonus magic damage, then shred, then waves that hit
    everyone else."""
    def init(self, f):
        f.sheet.mana_max = 0

    def attack(self, f, target):
        star = f.sheet.star
        f.hit_attack(target)
        if not target.alive:
            return
        f.hit_ability("MagicDamageCalc1", target, src="ascension")
        if star >= 2 and target.alive:
            pct, dur = f.row("ShredLevel") / 100.0, f.row("ShredDuration")
            if f.t >= target.shred_until or pct >= target.shred:
                target.shred = pct
            target.shred_until = max(target.shred_until, f.t + dur)
        if star >= 3:
            for d in f.aoe(exclude_primary=True):
                f.hit_ability("MagicDamageCalc2", d, src="waves")


class KhaZix(Driver):
    """Taste Their Fear: leaps to the farthest dummy in reach; an isolated
    target takes more and refunds mana. Spread out, every target is
    isolated."""
    def cast(self, f):
        al = f.alive()
        d = al[-1] if f.clump else f.target()
        if f.clump and len(al) > 1:
            f.hit_ability("MagicDamageCalc1", d)
        else:
            f.hit_ability("MagicDamageCalc2", d, src="isolated")
            f.mana += f.row("IsolateManaGrant")


class LeBlanc(Driver):
    """Mirror Image: a hit, and less to the adjacent dummies."""
    def cast(self, f):
        f.hit_ability("MagicDamageCalc1", f.target())
        for d in f.aoe(exclude_primary=True):
            f.hit_ability("MagicDamageCalc2", d, src="splash")


class Lux(Driver):
    """Final Spark: a laser through the line, weaker per dummy passed."""
    def cast(self, f):
        fall, floor = f.row("DamageReductionPerUnit"), f.row("MinimumFalloffDamageRatio")
        for i, d in enumerate(f.aoe()):
            f.hit_ability("MagicDamageCalc1", d, mult=max(floor, 1.0 - fall * i))


class Pebbles(Driver):
    """Azure Laser: channels while draining mana, ticking damage and flat
    magic-resist reduction on the target. With the Riftbeast Alpha Mark,
    mana regen accrues per seconds channeled."""
    def cast_time(self, f):
        return 1.0 / f.row("PercentManaPerSecond")

    def cast(self, f):
        f.state["channel_until"] = f.t + self.cast_time(f)
        f.state["last"] = f.t
        f.mana = 0.0

    def tick(self, f):
        until = f.state.get("channel_until", 0.0)
        if f.t > until + 1e-9:
            return
        d = f.target()
        if d is None:
            return
        span = min(tft.TICK_S, until - f.state["last"])
        f.state["last"] = f.t
        if span <= 0:
            return
        f.hit_ability("MagicDamageCalc1", d, src="laser", mult=span)
        d.mr_flat += f.row("MRReduction") * span
        if f.fx.riftbeast:
            f.state["channeled"] = f.state.get("channeled", 0.0) + span
            per = f.row("TraitChannelSecondsTooltip")
            while f.state["channeled"] >= per:
                f.state["channeled"] -= per
                f.fx.manaRegen += f.row("TraitManaRegenTooltip")


class Sivir(Driver):
    """Boomerang Blade: a hit, then bounces between nearby dummies; a kill
    adds bounces. Alone, there is nothing to bounce to."""
    def cast(self, f):
        f.hit_ability("PhysicalDamageCalc1", f.target())
        if not f.clump:
            return
        bounces = int(f.row("NumBounces"))
        i = 0
        while i < bounces:
            al = f.alive()
            if len(al) < 2:
                break
            d = al[i % len(al)]
            f.hit_ability("PhysicalDamageCalc2", d, src="bounces")
            if not d.alive:
                bounces += int(f.row("BonusKillBounces"))
            i += 1


class Soraka(Driver):
    """Starcall: a star on the target; a target already starred takes three
    more, smaller ones."""
    def cast(self, f):
        d = f.target()
        f.hit_ability("MagicDamageCalc1", d)
        if d.marks.get("star"):
            for _ in range(int(f.row("NumAdditionalStars"))):
                if d.alive:
                    f.hit_ability("MagicDamageCalc2", d, src="extra stars")
        d.marks["star"] = True


class Tristana(Driver):
    """Explosive Charge: attack speed for four seconds, then a blast that
    grows with the attacks made meanwhile, split over the dummies in
    reach. She keeps attacking through the cast."""
    def cast_time(self, f):
        return 0.0

    def cast(self, f):
        dur = f.row("Duration")
        f.buff_as(f.calc("AttackSpeedCalc1"), dur)
        f.state["charge"] = [f.t + dur, 0]
        f.lock_until = max(f.lock_until, f.t + dur)   # mana-locked while the charge ticks

    def attack(self, f, target):
        f.hit_attack(target)
        ch = f.state.get("charge")
        if ch and f.t < ch[0]:
            ch[1] += 1

    def tick(self, f):
        ch = f.state.get("charge")
        if ch and f.t >= ch[0] - 1e-9:
            f.state["charge"] = None
            tg = f.aoe()
            for d in tg:
                f.hit_ability("PhysicalDamageCalc1", d, src="explosion", mult=1.0 / len(tg))
                f.hit_ability("PhysicalDamageCalc2", d, src="explosion", mult=ch[1] / len(tg))


class Varus(Driver):
    """Piercing Arrow: winds up, then a line shot weaker per dummy passed."""
    def cast_time(self, f):
        return f.row("SpellDuration")

    def cast(self, f):
        fall, floor = f.row("DamageReductionPerHit"), f.row("MinDamagePercent")
        for i, d in enumerate(f.aoe()):
            f.hit_ability("PhysicalDamageCalc1", d, mult=max(floor, 1.0 - fall * i))


class Xayah(Driver):
    """Deadly Plumage: attack speed for five attacks, which become feathers
    that deal ability damage and strip flat armor."""
    def cast_time(self, f):
        return 0.0

    def cast(self, f):
        f.state["feathers"] = int(f.row("NumAttacks"))
        f.as_extra = f.row("AttackSpeed") - 1.0
        f.as_extra_until = 1e9
        f.lock_until = 1e9   # mana-locked until the feathers are spent

    def attack(self, f, target):
        n = f.state.get("feathers", 0)
        if n <= 0:
            f.hit_attack(target)
            return
        f.state["feathers"] = n - 1
        f.hit_ability("PhysicalDamageCalc1", target, src="feathers")
        target.armor_flat += f.calc("GenericCalc1")
        if n - 1 == 0:
            f.as_extra_until = f.t
            f.lock_until = f.t


class Yunara(Driver):
    """Cultivation of Spirit: a hit, then a split to two nearby dummies."""
    def cast(self, f):
        f.hit_ability("PhysicalDamageCalc1", f.target())
        for d in f.aoe(count=f.row("NumSecondaryTargets"), exclude_primary=True):
            f.hit_ability("PhysicalDamageCalc2", d, src="split")


DRIVERS = {
    "TFT18_Ahri": Ahri, "TFT18_Akali": Akali, "TFT18_Alune": Alune, "TFT18_Ashe": Ashe,
    "TFT18_Aphelios": Aphelios, "TFT18_Caitlyn": Caitlyn,
    "TFT18_Cassiopeia": Cassiopeia, "TFT18_Draven": Draven, "TFT18_Ezreal": Ezreal,
    "TFT18_Gromp": Gromp, "TFT18_Karma": Karma, "TFT18_Kayle": Kayle,
    "TFT18_KhaZix": KhaZix, "TFT18_LeBlanc": LeBlanc, "TFT18_Lux_Base": Lux,
    "TFT18_Pebbles": Pebbles, "TFT18_Sivir": Sivir, "TFT18_Soraka": Soraka,
    "TFT18_Tristana": Tristana, "TFT18_Varus": Varus, "TFT18_Xayah": Xayah,
    "TFT18_Yunara": Yunara,
}


def has_driver(unit):
    return unit["api"] in DRIVERS


def driver_for(unit):
    cls = DRIVERS.get(unit["api"])
    if cls is None:
        raise KeyError(f"no driver for {unit['api']}")
    return cls()
