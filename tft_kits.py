"""Set 18 unit drivers: the shape of each ability — how many targets, over
how long, what repeats — with every number read from the unit's own
calculations and curve rows in the snapshot. A driver is a few lines; the
engine (tft.Fight) does the mana cycle, crit, resists, amps, on-hit
effects, and — for fighters and tanks, whose dummies hit back — the unit's
health, shields, healing, durability and death.

Hooks (tft.Driver): `init(f)` at the start, `cast_time(f)`, `attack(f,
target)` (default: `f.hit_attack(target)`), `cast(f)`, `tick(f)` every
0.25 s, `hit(f, attacker, damage)` after the unit takes damage, `kill(f,
target)` when a dummy dies to the unit, `died(f)` when the unit falls.

Targets: `f.target()` is the dummy being attacked; `f.alive()` all that
stand; `f.aoe(n, exclude_primary)` is who an area ability reaches — every
standing dummy (up to n) in the clump, only the current target spread out;
`f.adjacent()` the same for "adjacent"/melee-range effects. `d.marks` is a
scratch dict per dummy.

Damage: `f.hit_ability(calc, target, src=, mult=, dtype=, runtime=)`
resolves the calc at the unit's current stats and applies its type;
`f.hit_attack(target, mult=)` is a basic attack with on-hits;
`f.dot_ability(calc, target, duration, ...)` spreads it over time;
`f.deal(amount, dtype, target, src, ability=, crit=)` is raw; `f.calc(name)`
and `f.row(name)` read the unit's numbers (an Adaptor's active form's
rows and calcs replace the base ones; `f.sheet.form` says which).
`f.stun(targets, seconds)` denies the dummies' attacks and casts;
`f.sunder(d, pct, dur)` / `f.shred(d, pct, dur)` / `f.burn(d, pct, dur)`;
`d.armor_flat` / `d.mr_flat` strip flat resists.

The unit's body (only meaningful where the dummies hit back): `f.hp`,
`f.max_hp()`, `f.hp_frac()`, `f.alive_unit`; `f.heal(amount, src)`,
`f.shield(amount, duration, src, decays=)`, `f.buff_resists(armor, mr,
duration)`, `f.buff_durability(fraction, duration)`, `f.untargetable(s)`,
`f.gain_max_hp(amount)`, `f.armor_extra` / `f.mr_extra` (permanent),
`f.add_body(hp, armor, mr, name)` for an on-death body that taunts and
keeps the dummies busy (register it in `died`). Ally effects are counted,
not simulated: `f.heal_ally(amount)`, `f.shield_ally(amount)`.

Stats: `f.ad()`, `f.ap()`, `f.attack_speed()`; `f.buff_as(pct, duration)`
or `f.as_extra` + `f.as_extra_until` for attack speed, `f.ad_extra`
(fraction of base) and `f.ap_extra` (flat) for driver-granted stats,
`f.amp_extra`. Mana: `f.mana`, `f.sheet.mana_max` (set to 0 in `init` for
a unit that does not cast on mana), `f.lock_until` (raise it to hold the
mana lock through a buff). `f.state` is the driver's scratch dict; `f.t`
the clock; `f.clump` the geometry; `f.fx.summoner` the Summoner trait's
rows when active (damageMult, healthMult, extraSummons, extraAttacks).
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
    """Kunai Strike. Attack-damage form: a volley, more if the target burns.
    Ability-power form: magic damage, multiplied against a tank. Either way a
    kill casts it again at reduced damage."""

    def cast(self, f):
        (self._ad if f.sheet.form == "AD" else self._ap)(f)

    def _ad(self, f):
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

    def _ap(self, f):
        mult = 1.0
        for _ in range(4):
            d = f.target()
            if d is None:
                break
            f.hit_ability("MagicDamageCalc1", d,
                          mult=mult * (f.row("TankDamageMultiplierAP") if d.is_tank else 1.0))
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
    """Belchy Bubble. Ability-power form: a hit and a poison cloud around it.
    Attack-damage form: a hit and a splash on the dummies a hex away (its
    slow does nothing here). With the Riftbeast Alpha Mark (Purple Buff),
    ability power — or attack damage in the other form — every five
    seconds."""

    def cast(self, f):
        if f.sheet.form == "AD":
            tgt = f.target()
            f.hit_ability("PhysicalDamageCalc1", tgt)
            for d in f.adjacent():
                if d is not tgt:
                    f.hit_ability("PhysicalDamageCalc2", d, src="splash")
            return
        f.hit_ability("MagicDamageCalc1", f.target())
        for d in f.aoe():
            f.dot_ability("MagicDamageCalc2", d, f.row("PoisonDurationAP"), src="cloud")

    def tick(self, f):
        if f.fx.riftbeast:
            nxt = f.state.get("buff_at", f.row("TraitTimer"))
            if f.t >= nxt - 1e-9:
                if f.sheet.form == "AD":
                    f.ad_extra += f.row("TraitTimedAD")
                else:
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


SET = 18


def _heal_over_time(f, total, duration, src="ability"):
    """Queue `total` healing spread over `duration` seconds; `_tick_heal`
    pays out the elapsed share on every 0.25 s tick."""
    if duration > 0:
        f.state["hot"] = [f.t + duration, total / duration, src, f.t]


def _tick_heal(f):
    hot = f.state.get("hot")
    if hot is None:
        return
    span, hot[3] = min(f.t, hot[0]) - hot[3], f.t
    if span > 0:
        f.heal(hot[1] * span, hot[2])
    if f.t >= hot[0]:
        f.state["hot"] = None


class Kobuko(Driver):
    """Dance of Life: healing spread over the duration, and the next attack
    is replaced by a bash (the bash is the whole swing, as the text says)."""
    def cast(self, f):
        _heal_over_time(f, f.calc("HealthCalc1"), f.row("Duration"), "dance of life")
        f.state["bash"] = True

    def attack(self, f, target):
        if f.state.pop("bash", False):
            f.hit_ability("MagicDamageCalc1", target, src="bash")
        else:
            f.hit_attack(target)

    def tick(self, f):
        _tick_heal(f)


class Leona(Driver):
    """Shield Bash: bonus armor and magic resist from the start of combat,
    decaying linearly to nothing over the decay duration; the active bashes
    the target for damage off her armor (so it is worth most early) and
    stuns it."""
    def init(self, f):
        f.state["peak"] = f.state["on"] = f.calc("GenericCalc1")
        f.armor_extra += f.state["on"]
        f.mr_extra += f.state["on"]

    def tick(self, f):
        left = f.state["peak"] * max(0.0, 1.0 - f.t / f.row("DecayDuration"))
        f.armor_extra += left - f.state["on"]
        f.mr_extra += left - f.state["on"]
        f.state["on"] = left

    def cast(self, f):
        d = f.target()
        f.hit_ability("MagicDamageCalc1", d, src="bash")
        f.stun([d], f.row("StunDuration"))


class Ornn(Driver):
    """Bellows Breath: a shield, then a cone over the dummies in reach. The
    Forge Power quest pays out Artifact Anvils between rounds, so nothing
    of it lands inside a fight."""
    def cast(self, f):
        f.shield(f.calc("ShieldCalc1"), f.row("ShieldDuration"), "ability")
        for d in f.aoe():
            f.hit_ability("MagicDamageCalc1", d, src="cone")


class Rakan(Driver):
    """Entrancing Dance: a shield on himself. The decaying attack speed he
    hands the ally who has dealt the most damage has no ally to land on."""
    def cast(self, f):
        f.shield(f.calc("ShieldCalc1"), f.row("ShieldDuration"), "ability")


class RekSai(Driver):
    """Uproot: the passive regenerates health every tick of its own tick
    rate, tripled for a few seconds after a cast; the active lunges out,
    damaging and knocking up the adjacent dummies."""
    def tick(self, f):
        rate = f.row("PassiveTickRate")
        nxt = f.state.get("regen_at", rate)
        while f.t >= nxt - 1e-9:
            mult = f.row("SpellHealthRegenMultiplier") if f.t < f.state.get("boost", 0.0) else 1.0
            f.heal(f.calc("HealthCalc2") * mult, "burrow regen")
            nxt += rate
        f.state["regen_at"] = nxt

    def cast(self, f):
        f.state["boost"] = f.t + f.row("SpellMultiplierDuration")
        tg = f.adjacent()
        for d in tg:
            f.hit_ability("MagicDamageCalc1", d, src="uproot")
        f.stun(tg, f.row("KnockupDuration"))


class Yorick(Driver):
    """Last Rites: the active heals him and strikes the target. On death a
    Spirit Walker with the ghoul's health — multiplied by the Summoner
    trait when it is active — taunts and holds the dummies; its resists are
    the summon's own stats."""
    def init(self, f):
        f.state["spirit"] = tft.load_snapshot().extras["TFT18_Yorick_Spirit"]["stats"]

    def cast(self, f):
        f.heal(f.calc("HealthCalc1"), "last rites")
        f.hit_ability("PhysicalDamageCalc1", f.target(), src="strike")

    def died(self, f):
        s = f.state["spirit"]
        f.add_body(f.calc("HealthCalc2") * f.fx.summoner.get("healthMult", 1.0),
                   s["armor"], s["mr"], "spirit walker")


class Alistar(Driver):
    """Triumphant Roar: heals himself and the two lowest-health allies (the
    count is in Riot's text, not in a row; ally healing is counted, not
    simulated), then slams the target for damage and a stun. The cleanse
    has nothing to remove here."""
    def cast(self, f):
        f.heal(f.calc("HealthCalc1"), "roar")
        f.heal_ally(f.calc("HealthCalc2") * 2)
        d = f.target()
        f.hit_ability("MagicDamageCalc1", d, src="slam")
        f.stun([d], f.row("StunDuration"))


class Elise(Driver):
    """Spider Queen: the first cast transforms — bonus max health, and from
    then on every attack carries bonus magic damage and heals her. Later
    casts grant decaying attack speed; the row is a multiplier (2.75 =
    +175%) and decays to nothing, so half of it is applied flat for the
    duration."""
    def cast(self, f):
        if not f.state.get("spider"):
            f.state["spider"] = True
            f.gain_max_hp(f.row("SpiderHealthBuff"))
            return
        f.buff_as((f.row("DecayingAS") - 1.0) / 2.0, f.row("ASBuffDuration"))

    def attack(self, f, target):
        f.hit_attack(target)
        if f.state.get("spider"):
            f.hit_ability("MagicDamageCalc1", target, src="spider fangs")
            f.heal(f.calc("HealthCalc1"), "spider fangs")


class Scuttlecrab(Driver):
    """Can You Dig It?: attacks are a dance hitting every adjacent dummy (on-
    hit effects land on the one it is facing), and the active burrows for
    durability plus a heal — a share of it up front, the rest over the
    burrow. The Riftbeast Green Buff heals allies, and the coins are gold."""
    def attack(self, f, target):
        for d in f.adjacent():
            if d is target:
                f.hit_ability("PhysicalDamageCalc1", d, src="dance")
            else:
                f.deal(f.calc("PhysicalDamageCalc1"), "physical", d, "dance")

    def cast(self, f):
        dur, total = f.row("BurrowDuration"), f.calc("HealthCalc1")
        up = f.row("HealPercentInitial")
        f.buff_durability(f.row("BurrowDurability"), dur)
        f.heal(total * up, "burrow")
        _heal_over_time(f, total * (1.0 - up), dur, "burrow")

    def tick(self, f):
        _tick_heal(f)


class Sejuani(Driver):
    """Sun's Wrath: a shield, then a cone and a line over the dummies in
    reach."""
    def cast(self, f):
        f.shield(f.calc("ShieldCalc1"), f.row("ShieldDuration"), "ability")
        for d in f.aoe():
            f.hit_ability("MagicDamageCalc1", d, src="cone")
        for d in f.aoe():
            f.hit_ability("MagicDamageCalc2", d, src="line")


class Shen(Driver):
    """Ki Barrier: a shield on himself and one on a damaged ally (counted,
    not simulated), and his next few attacks come faster and carry bonus
    magic damage. The ally's copy of that buff is not simulated."""
    def cast(self, f):
        dur = f.row("MagicShieldDuration")
        f.shield(f.calc("ShieldCalc1"), dur, "ability")
        f.shield_ally(f.calc("ShieldCalc2"))
        f.state["ki"] = int(f.row("NumAttacksBuff"))
        f.as_extra = f.row("AttackSpeedBuff")
        f.as_extra_until = 1e9

    def attack(self, f, target):
        f.hit_attack(target)
        n = f.state.get("ki", 0)
        if n > 0:
            f.state["ki"] = n - 1
            f.hit_ability("MagicDamageCalc1", target, src="ki strike")
            if n == 1:
                f.as_extra_until = f.t


class Fiddlesticks(Driver):
    """Harvest: strips flat magic resist off the nearest few dummies, then
    channels for the cast duration, draining that damage out of each of
    them and that healing into himself over the channel."""
    def cast_time(self, f):
        return f.row("CastDuration")

    def cast(self, f):
        dur = f.row("CastDuration")
        for d in f.aoe(count=f.row("NumTargets")):
            d.mr_flat += f.row("MRReduction")
            f.dot_ability("MagicDamageCalc1", d, dur, src="drain")
        _heal_over_time(f, f.calc("HealthCalc1"), dur, "drain")

    def tick(self, f):
        _tick_heal(f)


SET = 18


def _track_shield(f, amount, duration, src):
    """Shield the unit and remember the engine's entry for it, so the driver
    can tell a shield that was spent from one that merely ran out (the
    engine drops both from `f.shields`, a spent one at zero)."""
    n = len(f.shields)
    f.shield(amount, duration, src)
    f.state["sh"] = f.shields[-1] if len(f.shields) > n else None


def _shield_broke(f):
    """True once — at the moment the tracked shield is fully absorbed."""
    sh = f.state.get("sh")
    if sh is None:
        return False
    if sh[0] <= 0:
        f.state["sh"] = None
        return True
    if f.t > sh[1]:
        f.state["sh"] = None    # expired with health left: no break
    return False


class Hecarim(Driver):
    """Spirit of Dread: armour and magic resist while the heal trickles in
    over the same window ("for 3 seconds" is in Riot's text, not in a row),
    and spectral riders that hit and stun the nearest few."""
    DREAD_S = 3.0

    def cast(self, f):
        r = f.row("Resists")
        f.buff_resists(r, r, self.DREAD_S)
        f.state["dread"] = [f.t + self.DREAD_S, f.calc("HealthCalc1") / self.DREAD_S]
        tg = f.aoe(f.row("NumEnemies"))
        for d in tg:
            f.hit_ability("MagicDamageCalc1", d, src="riders")
        f.stun(tg, f.row("StunDuration"))

    def tick(self, f):
        st = f.state.get("dread")
        if st is None or not f.alive_unit:
            return
        span = min(tft.TICK_S, st[0] - (f.t - tft.TICK_S))
        if span > 0:
            f.heal(st[1] * span, "spirit of dread")
        if f.t >= st[0]:
            f.state["dread"] = None


class Krug(Driver):
    """Rock and Roll: bonus max health, then a roll into the target. On death
    he splits into Kruglettes ("two" is in the text, and the Summoner trait
    would add more) that taunt with the Kruglette's own resists; with the
    Riftbeast Alpha Mark the Slate Buff shields an ally on the way out."""
    def init(self, f):
        f.state["mini"] = tft.load_snapshot().extras["TFT18_KrugMini"]["stats"]

    def cast(self, f):
        f.gain_max_hp(f.calc("HealthCalc2"))
        f.hit_ability("PhysicalDamageCalc1", f.target())

    def died(self, f):
        mini = f.state["mini"]
        hp = f.calc("HealthCalc1")
        for _ in range(int(2 + f.fx.summoner.get("extraSummons", 0))):
            f.add_body(hp, mini["armor"], mini["mr"], "kruglette")
        if f.fx.riftbeast:
            f.shield_ally(f.row("TraitShieldHealth") * f.max_hp())


class Rammus(Driver):
    """Defensive Ball Curl: a shield and heavy resists for a few seconds —
    the taunt is already the model, the dummies have nobody else to hit. If
    the shield is spent rather than expiring the ball uncurls, and the burst
    scales with the armour and magic resist he has at that moment."""
    def cast(self, f):
        dur = f.row("Duration")
        _track_shield(f, f.calc("ShieldCalc1"), dur, "ball curl")
        r = f.row("ArmorMR")
        f.buff_resists(r, r, dur)

    def hit(self, f, attacker, damage):
        if _shield_broke(f):
            for d in f.aoe():
                f.hit_ability("PhysicalDamageCalc1", d, src="shield break")


class Vi(Driver):
    """Furious Fists: every attack heals a share of her max health; the cast
    heals a lump, then attack speed (a multiplier row) and durability for a
    few seconds. Unstoppable does nothing here."""
    def attack(self, f, target):
        f.hit_attack(target)
        f.heal(f.calc("HealthCalc1"), "furious fists")

    def cast(self, f):
        dur = f.row("SpellDuration")
        f.heal(f.calc("HealthCalc3"), "primal roar")
        f.buff_as(f.row("SpellAS") - 1.0, dur)
        f.buff_durability(f.row("SpellDurability"), dur)


class Amumu(Driver):
    """Tantrum: a heartbeat on its own tick rate that heals him and hits the
    dummies in reach; the cast bursts everyone nearby and stuns them, for
    the longer duration when the target is already Burning."""
    def tick(self, f):
        if not f.alive_unit:
            return
        rate = f.row("PassiveTickRate")
        nxt = f.state.get("beat", rate)
        if f.t < nxt - 1e-9:
            return
        f.state["beat"] = nxt + rate
        f.heal(f.calc("HealthCalc1"), "tantrum")
        for d in f.adjacent():
            f.hit_ability("MagicDamageCalc1", d, src="tantrum")

    def cast(self, f):
        d = f.target()
        burning = d is not None and ((f.t <= d.burn_until and d.burn_pct > 0)
                                     or (d.burn_stack > 0
                                         and f.t <= d.marks.get("burn_stack_until", 0.0)))
        tg = f.aoe()
        for x in tg:
            f.hit_ability("MagicDamageCalc3", x)
        f.stun(tg, f.calc("GenericCalc1") if burning else f.row("StunDuration"))


class Lillia(Driver):
    """Lilting Lullaby: a heal and butterflies at the nearest few. Her own
    attacks wake the dummy she is hitting at once — it takes the wake-up
    damage and never sleeps — while the others sleep out the full duration
    with nothing around to wake them."""
    def cast(self, f):
        f.heal(f.calc("HealthCalc1"), "lullaby")
        prim = f.target()
        tg = f.aoe(f.row("NumEnemiesToFireAt"))
        for d in tg:
            f.hit_ability("MagicDamageCalc1", d, src="butterflies")
        for d in tg:
            if d is prim:
                f.deal(f.row("WakeupDamage") * d.max_hp, "magic", d, "wake-up")
            else:
                f.stun([d], f.row("SleepDuration"))


class Malphite(Driver):
    """Petrified Bark: a shield, and when it is spent (not when it expires) a
    wave of dark energy scaling with the armour and magic resist he has
    then. "Petrified" carries no numbers in the data, so it is left out."""
    def cast(self, f):
        _track_shield(f, f.calc("ShieldCalc1"), f.row("ShieldDuration"), "petrified bark")

    def hit(self, f, attacker, damage):
        if _shield_broke(f):
            for d in f.aoe():
                f.hit_ability("MagicDamageCalc1", d, src="shield break")


class Sentinel(Driver):
    """Azure Shockwave: a shield, then a fissure that knocks up, damages and
    Mana Reaves everyone in its path. With the Riftbeast Alpha Mark the Blue
    Buff's mana regen on himself."""
    def init(self, f):
        if f.fx.riftbeast:
            f.fx.manaRegen += f.row("TraitSelfManaRegen")

    def cast(self, f):
        f.shield(f.calc("ShieldCalc1"), f.row("ShieldDuration"), "shockwave")
        tg = f.aoe()
        for d in tg:
            f.hit_ability("MagicDamageCalc1", d, src="fissure")
        f.stun(tg, f.row("KnockupDuration"))
        reave = f.row("ManaReaveFlat")
        for d in tg:
            if d.alive:
                d.mana = max(0.0, d.mana - reave)


class Sett(Driver):
    """Haymaker: the first time he falls below the threshold, a burst of
    mana; the cast winds up for the heal's duration, healing and then
    punching a cone."""
    def hit(self, f, attacker, damage):
        if "mana" not in f.state and f.hp_frac() < f.row("ManaHPThreshold"):
            f.state["mana"] = True
            f.mana += f.calc("ManaCalc1")

    def cast_time(self, f):
        return f.row("HealDuration")

    def cast(self, f):
        f.heal(f.calc("HealthCalc1"), "wind-up")
        for d in f.aoe():
            f.hit_ability("PhysicalDamageCalc1", d, src="haymaker")


class Maokai(Driver):
    """Sow the Seeds: a sapling at the nearest dummy for every chunk of
    damage he blocks, a fistful more when he falls, and a cast that hits the
    target and heals a lump plus a share of the health he is missing."""
    def hit(self, f, attacker, damage):
        n = int(f.mitigated / f.row("DamageMitigatedPerSapling")) - f.state.get("saplings", 0)
        if n > 0:
            f.state["saplings"] = f.state.get("saplings", 0) + n
            for _ in range(n):
                f.hit_ability("MagicDamageCalc1", f.target(), src="saplings")

    def cast(self, f):
        f.hit_ability("MagicDamageCalc2", f.target())
        f.heal(f.calc("HealthCalc1")
               + f.row("ActiveMissingHealthHeal") * (f.max_hp() - f.hp), "sow the seeds")

    def died(self, f):
        for _ in range(int(f.row("SaplingsOnDeath"))):
            f.hit_ability("MagicDamageCalc1", f.target(), src="saplings")


class Taric(Driver):
    """Emerald Radiance: the first time he drops below the threshold, the
    same shield on him and on one ally; the cast heals and charges his next
    couple of attacks with bonus magic damage (the paired ally's copy of the
    charge is not simulated)."""
    def hit(self, f, attacker, damage):
        if "radiance" not in f.state and f.hp_frac() < f.row("PassivePercentHealthThreshold"):
            f.state["radiance"] = True
            amt = f.calc("ShieldCalc1")
            f.shield(amt, f.row("PassiveShieldDuration"), "emerald radiance")
            f.shield_ally(amt)

    def cast(self, f):
        f.heal(f.calc("HealthCalc1"), "radiance")
        f.state["shatter"] = int(f.row("NumAttacks"))

    def attack(self, f, target):
        f.hit_attack(target)
        n = f.state.get("shatter", 0)
        if n > 0:
            f.state["shatter"] = n - 1
            f.hit_ability("MagicDamageCalc1", target, src="shatter")


SET = 18


class Camille(Driver):
    """Defensive Sweep: slice the target and take a shield for a couple of
    seconds. The shield is a flat curve row, so it does not scale."""
    def cast(self, f):
        f.hit_ability("PhysicalDamageCalc1", f.target(), src="sweep")
        f.shield(f.row("AbilityShield"), f.row("ShieldDuration"), "sweep")


class Warwick(Driver):
    """Jaws of The Beast: a bite that heals him for a share of the damage
    it actually lands, and attack speed that stacks for the rest of the
    fight (the row is a multiplier: 1.2 is +20% a cast)."""
    def cast(self, f):
        dmg = f.hit_ability("PhysicalDamageCalc1", f.target(), src="bite")
        f.heal(dmg * f.calc("GenericCalc1"), "bite")
        f.as_extra += f.row("AttackSpeedBuff") - 1.0
        f.as_extra_until = 1e9


class Brambleback(Driver):
    """Crimson Fury: bonus attack damage for a spell of frenzy, during
    which he ignores part of every dummy's armor — modelled as a sunder,
    which is exactly his own armor ignore since nothing else damages them.
    A kill leaps at the next target. With the Riftbeast Alpha Mark (Red
    Buff) his attacks burn and heal him for a share of his max health."""
    def cast(self, f):
        dur, ad = f.row("Duration"), f.row("FrenzyADPercent")
        f.ad_extra += ad
        f.state.setdefault("frenzy", []).append((f.t + dur, ad))
        ignore = f.calc("GenericCalc1")
        for d in f.alive():
            f.sunder(d, ignore, dur)

    def tick(self, f):
        fr = f.state.get("frenzy")
        if fr and any(f.t >= until - 1e-9 for until, _ in fr):
            f.ad_extra -= sum(a for until, a in fr if f.t >= until - 1e-9)
            f.state["frenzy"] = [x for x in fr if f.t < x[0] - 1e-9]

    def attack(self, f, target):
        f.hit_attack(target)
        if f.fx.riftbeast:
            f.burn(target, f.row("BurnAmount") / 100.0, f.row("TraitBurnDuration"))
            f.heal(f.row("TraitMaxHealthHeal") * f.max_hp(), "red buff")

    def kill(self, f, target):
        d = f.target()
        if d is not None:
            f.hit_ability("PhysicalDamageCalc1", d, src="leap")


class Diana(Driver):
    """Pale Barrier: a shield, and moonlight orbs spread evenly over the
    dummies in reach."""
    def cast(self, f):
        f.shield(f.calc("ShieldCalc1"), f.row("ShieldDuration"), "barrier")
        tg = f.aoe()
        for i in range(int(f.row("NumOrbs"))):
            f.hit_ability("MagicDamageCalc1", tg[i % len(tg)], src="orbs")


class Morgana(Driver):
    """Withering Curse: omnivamp from the start; the blast curses the
    nearest few and leaves a zone ticking on everyone in it for the same
    time. Each live curse on a dummy adds flat magic damage to every hit
    she lands on it, and curses stack with repeat casts."""
    def init(self, f):
        f.sheet.omnivamp += f.row("Omnivamp")

    def _curse_bonus(self, f, d):
        n = sum(1 for until in d.marks.get("curses", ()) if f.t < until)
        if n and d.alive:
            f.deal(f.calc("MagicDamageCalc3") * n, "magic", d, "curse")

    def attack(self, f, target):
        f.hit_attack(target)
        self._curse_bonus(f, target)

    def cast(self, f):
        dur = f.row("SpellDuration")
        for d in f.aoe(f.row("NumEnemiesCursed")):
            f.hit_ability("MagicDamageCalc1", d, src="blast")
            self._curse_bonus(f, d)
            d.marks.setdefault("curses", []).append(f.t + dur)
        for d in f.aoe():
            f.dot_ability("MagicDamageCalc2", d, dur, src="withering zone", mult=dur)


class Rengar(Driver):
    """Savagery: jump onto the dummy with the least health left as a share
    of its own, stab it, then heal — the more health it is missing, the
    closer the heal to its maximum."""
    def cast(self, f):
        d = min(f.alive(), key=lambda x: x.hp / x.max_hp)
        f.hit_ability("PhysicalDamageCalc1", d, src="stab")
        lo, hi = f.calc("HealthCalc1"), f.calc("HealthCalc2")
        f.heal(lo + (hi - lo) * (1.0 - d.hp / d.max_hp), "savagery")


class ElderDragon(Driver):
    """Heat Without Equal: attacks splash onto the dummies beside the
    target. The first cast is the flight — untargetable for the cast time,
    then a stun on everyone, omnivamp, an Ignite burning a share of max
    health, and Flame Breath straight away; every later cast is Flame
    Breath alone, a line weaker per dummy passed and another Ignite. With
    the Riftbeast Alpha Mark (Elder Dragon Buff) a dummy pushed under the
    execute threshold is finished off."""
    def attack(self, f, target):
        f.hit_attack(target)
        ratio = f.row("AttackAoERatio")
        for d in f.adjacent():
            if d is not target:
                f.hit_attack(d, mult=ratio, src="splash")
        self._execute(f)

    def cast(self, f):
        if not f.state.get("landed"):
            f.state["landed"] = True
            f.untargetable(self.cast_time(f))
            f.stun(f.alive(), f.row("StunDuration"))
            f.sheet.omnivamp += f.row("Omnivamp")
            self._ignite(f, f.alive())
        fall, floor = f.row("DamageReductionPerHit"), f.row("MinimumDamageThreshold")
        tg = f.aoe()
        for i, d in enumerate(tg):
            f.hit_ability("PhysicalDamageCalc2", d, src="flame breath",
                          mult=max(floor, 1.0 - fall * i))
        self._ignite(f, tg)
        self._execute(f)

    def tick(self, f):
        self._execute(f)

    def _ignite(self, f, targets):
        dur, pct = f.row("IgniteDuration"), f.row("IgniteMaxHealthDamage")
        for d in targets:
            f.dot(pct * d.max_hp * dur, dur, "physical", d, "ignite")

    def _execute(self, f):
        if not f.fx.riftbeast:
            return
        thr = f.row("TraitExecuteThreshold")
        for d in f.alive():
            if d.hp < thr * d.max_hp:
                f.deal(d.hp, "true", d, "execute", ability=False, crit=False)


class Murkwolf(Driver):
    """Rending Claws: leap onto the dummy with the least health left, then
    a couple of empowered attacks — far faster, each carrying bonus
    physical damage — with more granted by every kill. With the Riftbeast
    Alpha Mark (Grey Buff) he has Precision and crit chance that climbs as
    his own health falls."""
    def init(self, f):
        if f.fx.riftbeast:
            f.sheet.precision = True
            f.state["crit"] = f.sheet.crit_chance
            self.tick(f)

    def tick(self, f):
        if "crit" in f.state:
            lo, hi = f.row("TraitBaseCrit"), f.row("TraitBonusCrit")
            f.sheet.crit_chance = min(1.0, f.state["crit"] + lo
                                      + (hi - lo) * (1.0 - f.hp_frac()))

    def _empower(self, f, n):
        f.state["empowered"] = n
        f.as_extra = f.row("EmpowerAspd") - 1.0 if n > 0 else 0.0
        f.as_extra_until = 1e9 if n > 0 else f.t

    def cast(self, f):
        f.hit_ability("PhysicalDamageCalc1", min(f.alive(), key=lambda x: x.hp),
                      src="leap")
        self._empower(f, f.state.get("empowered", 0) + int(f.row("NumEmpoweredAttacks")))

    def attack(self, f, target):
        n = f.state.get("empowered", 0)
        f.hit_attack(target)
        if n > 0:
            f.hit_ability("PhysicalDamageCalc2", target, src="empowered")
            self._empower(f, f.state.get("empowered", n) - 1)

    def kill(self, f, target):
        self._empower(f, f.state.get("empowered", 0)
                      + int(f.row("NumEmpoweredAttacksGainedOnKill")))


class Kennen(Driver):
    """Firestorm: charges up for ability power per burning dummy, shields
    himself, rushes through the group, then leaves a firestorm whose total
    damage is split over everyone it covers and ticks for a couple of
    seconds."""
    def cast(self, f):
        burning = sum(1 for d in f.alive()
                      if (f.t <= d.burn_until and d.burn_pct > 0)
                      or (d.burn_stack > 0 and f.t <= d.marks.get("burn_stack_until", 0.0)))
        charge = f.row("APPerBurningEnemy") * 100.0 * burning
        f.ap_extra += charge
        f.shield(f.calc("ShieldCalc1"), f.row("ShieldDuration"), "firestorm")
        tg = f.aoe()
        for d in tg:
            f.hit_ability("MagicDamageCalc1", d, src="rush")
        dur = f.row("FirestormDuration")
        for d in tg:
            f.dot_ability("MagicDamageCalc2", d, dur, src="firestorm", mult=1.0 / len(tg))
        f.ap_extra -= charge


class MasterYi(Driver):
    """Wuju Style: no mana. Every third attack is a Double Strike that
    hits twice ("every third" is spelled out in the ability text and has
    no curve row). As an Adaptor his AD form banks stacking attack speed
    off each Double Strike; his AP form adds bonus magic damage and heals
    him for a share of it."""
    DOUBLE_EVERY = 3

    def init(self, f):
        f.sheet.mana_max = 0

    def attack(self, f, target):
        n = f.state.get("n", 0) + 1
        f.state["n"] = n
        f.hit_attack(target)
        if n % self.DOUBLE_EVERY:
            return
        d = target if target.alive else f.target()
        if d is None:
            return
        f.hit_attack(d, src="double strike")
        if f.sheet.form == "AP":
            dmg = f.hit_ability("MagicDamageCalc1", d, src="double strike")
            f.heal(dmg * f.row("APForm_HealPercent"), "double strike")
        else:
            f.as_extra += f.calc("AttackSpeedCalc1")
            f.as_extra_until = 1e9


class Gnar(Driver):
    """Rage Gene: no mana — Rage builds per second and per attack until he
    transforms into Mega Gnar, gaining max health, hitting and stunning
    the group and stripping flat resists off them. Mega Gnar then casts
    Grab n' Throw on mana at a Fighter's ten per attack: the target takes
    the throw, the dummies it passes through take the rest, and the last
    dummy standing is thrown off the board."""
    def init(self, f):
        f.sheet.mana_max = 0
        f.state["rage"] = 0.0

    def attack(self, f, target):
        f.hit_attack(target)
        self._rage(f, f.row("RagePerAttack"))

    def tick(self, f):
        self._rage(f, f.row("RagePerSecond") * tft.TICK_S)

    def _rage(self, f, amount):
        if f.state.get("mega"):
            return
        f.state["rage"] += amount
        if f.state["rage"] < f.row("TransformRageMax"):
            return
        f.state["mega"] = True
        f.gain_max_hp(f.calc("HealthCalc1"))
        strip = f.calc("GenericCalc1")
        for d in f.aoe():
            f.hit_ability("PhysicalDamageCalc3", d, src="transform")
            d.armor_flat += strip
            d.mr_flat += strip
        f.stun(f.aoe(), f.row("StunDuration"))
        f.sheet.mana_max = f.sheet.stats["mana"]
        f.sheet.mana_per_attack = tft.ROLE_MANA["Fighter"]

    def cast(self, f):
        d = f.target()
        if len(f.alive()) == 1:
            f.deal(d.hp, "true", d, "thrown off", ability=False, crit=False)
            return
        f.hit_ability("PhysicalDamageCalc1", d, src="throw")
        for o in f.aoe(exclude_primary=True):
            f.hit_ability("PhysicalDamageCalc2", o, src="passed through")


class Veigar(Driver):
    """Primordial Burst: one blast on the target, the bigger calc instead
    when it stands below a share of its max health. Every kill permanently
    adds ability power — the row is written as a share of the 100 ability
    power every unit starts with, so it is added as that many points."""

    def cast(self, f):
        d = f.target()
        if d.hp < f.row("HPThreshold") * d.max_hp:
            f.hit_ability("MagicDamageCalc2", d, src="low health")
        else:
            f.hit_ability("MagicDamageCalc1", d)

    def kill(self, f, target):
        f.ap_extra += f.row("APOnKill")


class Teemo(Driver):
    """Fungus Among Us: two clusters of mushrooms over the nearest few, then
    a giant one on the target. The two clusters are spelled out in Riot's
    text and have no row of their own. Foraging is economy."""

    def cast(self, f):
        for _ in range(2):
            for d in f.aoe(count=f.row("NumEnemiesHit")):
                f.hit_ability("MagicDamageCalc1", d, src="mushrooms")
        f.hit_ability("MagicDamageCalc2", f.target(), src="giant mushroom")


class Zyra(Driver):
    """Rampant Growth: plants that spit at the nearest dummy a fixed number
    of times each, one attack a second (their attack speed is nowhere in the
    data). Summoner adds plants and attacks; her Thornmaiden durability is
    the engine's."""

    def cast(self, f):
        n = int(f.row("NumPlantsToSpawn")) + max(0, int(f.fx.summoner.get("extraSummons", 1)) - 1)
        shots = int(f.row("ThornSpitterNumAttacks") + f.fx.summoner.get("extraAttacks", 0.0))
        f.state.setdefault("plants", []).append([f.t, shots, n])

    def tick(self, f):
        plants = f.state.setdefault("plants", [])
        for p in plants:
            while p[1] > 0 and f.t >= p[0] - 1e-9:
                for _ in range(p[2]):
                    f.hit_ability("MagicDamageCalc1", f.target(), src="plants")
                p[0] += 1.0
                p[1] -= 1
        f.state["plants"] = [p for p in plants if p[1] > 0]


class Ivern(Driver):
    """Triggerseed: a shield on several allies — counted, not simulated, and
    able to crit with Precision — then magic damage around them. The shield's
    calc resolves to nothing, so its amount is the curve row per 100 ability
    power. The allies' damage amp and the attack speed after six casts are
    ally buffs: not simulated."""

    def cast(self, f):
        shield = f.row("ShieldAmount") * f.ap() / 100.0
        if f.sheet.precision:
            shield *= f.sheet.crit_ev
        for _ in range(int(f.row("NumAlliesToShield"))):
            f.shield_ally(shield)
        for d in f.aoe():
            f.hit_ability("MagicDamageCalc1", d)


class Cinderling(Driver):
    """Razor Leaves: five leaves converging on the target for one total hit,
    then a burn. With the Riftbeast Alpha Mark (Scarlet Buff) every cast adds
    attack damage. Wound does nothing here: the dummies never heal."""

    def cast(self, f):
        d = f.target()
        f.hit_ability("PhysicalDamageCalc1", d)
        f.burn(d, f.row("BurnAmount") / 100.0, f.row("BurnDuration"))
        if f.fx.riftbeast:
            f.ad_extra += f.row("TraitADOnCast")


class KogMaw(Driver):
    """Raining Artillery: acid on the target and the next nearest. The
    attack-damage form hits a dummy below the health threshold with the
    bigger calc; the ability-power form adds damage over time. Caustic's
    shred and sunder ride the engine's on-hit effects."""

    def cast(self, f):
        tg = f.aoe(count=2)
        if f.sheet.form == "AD":
            thr = f.row("ADBonusDamageThreshold")
            for d in tg:
                low = d.hp < thr * d.max_hp
                f.hit_ability("PhysicalDamageCalc2" if low else "PhysicalDamageCalc1",
                              d, src="low health" if low else "ability")
            return
        for d in tg:
            f.hit_ability("MagicDamageCalc1", d)
            f.dot_ability("MagicDamageCalc2", d, f.row("APBonusDOTDuration"), src="acid")


class MamaBeak(Driver):
    """Flock Family: the cast summons the beaks for a few seconds (a duration
    that scales with ability power in the data); while they stand, every
    attack of hers lands the whole flock's damage on the same target. With
    the Riftbeast Alpha Mark (Orange Buff) each physical hit — hers and every
    beak's — strips flat armor."""

    def cast(self, f):
        f.state["beaks_until"] = f.t + f.calc("GenericCalc1")

    def attack(self, f, target):
        f.hit_attack(target)
        hits = 1
        if f.t < f.state.get("beaks_until", 0.0):
            n = int(f.row("NumMinisToSpawn"))
            f.hit_ability("PhysicalDamageCalc1", target, src="tiny beaks",
                          mult=n * f.fx.summoner.get("damageMult", 1.0))
            hits += n
        if f.fx.riftbeast:
            target.armor_flat += f.row("ArmorReduc") * hits


class Azir(Driver):
    """Arise!: attack speed and soldiers for the next few attacks, each of
    which becomes a command — the basic attack is replaced by every soldier's
    strike, its on-hit effects still landing. Summoner adds a soldier and
    multiplies their damage."""

    def cast(self, f):
        f.state["commands"] = int(f.row("NumAttacks"))
        f.state["soldiers"] = int(f.row("SoldiersToSpawn")) \
            + max(0, int(f.fx.summoner.get("extraSummons", 1)) - 1)
        f.as_extra = f.row("AttackSpeed") - 1.0
        f.as_extra_until = 1e9

    def attack(self, f, target):
        n = f.state.get("commands", 0)
        if n <= 0:
            f.hit_attack(target)
            return
        f.state["commands"] = n - 1
        f.hit_ability("MagicDamageCalc1", target, src="soldiers",
                      mult=f.state["soldiers"] * f.fx.summoner.get("damageMult", 1.0))
        if n == 1:
            f.as_extra_until = f.t


class Nidalee(Driver):
    """Javelin Toss / Prowler's Pounce. Ability-power form: attack speed for
    the next few attacks, which become javelins — every third one is thrown
    at the farthest dummy for the bigger calc ("the 3rd attack" is Riot's own
    wording, with no row). Attack-damage form: a swipe that ignores a share
    of the target's armor, and every third cast a heal (only counted: nothing
    hits her) plus a bonus scaled by the target's missing health."""

    def cast(self, f):
        if f.sheet.form == "AD":
            d = f.target()
            n = f.state["casts"] = f.state.get("casts", 0) + 1
            missing = 1.0 - d.hp / d.max_hp
            cut = d.armor * f.row("ArmorIgnoreRatio")   # ignored for this hit only
            d.armor_flat += cut
            f.hit_ability("PhysicalDamageCalc1", d, src="swipe")
            d.armor_flat -= cut
            if n % int(f.row("NumCastsEmpowered")) == 0:
                f.heal(f.calc("GenericCalc1"), "pounce")
                f.hit_ability("PhysicalDamageCalc1", d, src="pounce",
                              mult=f.row("ThirdAttackBonusDamageMissingHealth") * missing)
            return
        f.state["javelins"] = int(f.row("NumEmpoweredAttacks"))
        f.as_extra = f.row("BonusAttackSpeed") - 1.0
        f.as_extra_until = 1e9

    def attack(self, f, target):
        n = f.state.get("javelins", 0)
        if n <= 0:
            f.hit_attack(target)
            return
        f.state["javelins"] = n - 1
        f.state["thrown"] = thrown = f.state.get("thrown", 0) + 1
        if thrown % 3 == 0:
            f.hit_ability("MagicDamageCalc2", f.alive()[-1], src="farthest javelin")
        else:
            f.hit_ability("MagicDamageCalc1", target, src="javelins")
        if n == 1:
            f.as_extra_until = f.t


DRIVERS = {
    "TFT18_Ahri": Ahri, "TFT18_Akali": Akali, "TFT18_Alistar": Alistar,
    "TFT18_Alune": Alune, "TFT18_Amumu": Amumu, "TFT18_Aphelios": Aphelios,
    "TFT18_Ashe": Ashe, "TFT18_Azir": Azir, "TFT18_Brambleback": Brambleback,
    "TFT18_Caitlyn": Caitlyn, "TFT18_Camille": Camille, "TFT18_Cassiopeia": Cassiopeia,
    "TFT18_Cinderling": Cinderling, "TFT18_Diana": Diana, "TFT18_Draven": Draven,
    "TFT18_ElderDragon": ElderDragon, "TFT18_Elise": Elise, "TFT18_Ezreal": Ezreal,
    "TFT18_Fiddlesticks": Fiddlesticks, "TFT18_Gnar": Gnar, "TFT18_Gromp": Gromp,
    "TFT18_Hecarim": Hecarim, "TFT18_Ivern": Ivern, "TFT18_Karma": Karma,
    "TFT18_Kayle": Kayle, "TFT18_Kennen": Kennen, "TFT18_KhaZix": KhaZix,
    "TFT18_Kobuko": Kobuko, "TFT18_KogMaw": KogMaw, "TFT18_Krug": Krug,
    "TFT18_LeBlanc": LeBlanc, "TFT18_Leona": Leona, "TFT18_Lillia": Lillia,
    "TFT18_Lux_Base": Lux, "TFT18_Malphite": Malphite, "TFT18_MamaBeak": MamaBeak,
    "TFT18_Maokai": Maokai, "TFT18_MasterYi": MasterYi, "TFT18_Morgana": Morgana,
    "TFT18_Murkwolf": Murkwolf, "TFT18_Nidalee": Nidalee, "TFT18_Ornn": Ornn,
    "TFT18_Pebbles": Pebbles, "TFT18_Rakan": Rakan, "TFT18_Rammus": Rammus,
    "TFT18_RekSai": RekSai, "TFT18_Rengar": Rengar, "TFT18_Scuttlecrab": Scuttlecrab,
    "TFT18_Sejuani": Sejuani, "TFT18_Sentinel": Sentinel, "TFT18_Sett": Sett,
    "TFT18_Shen": Shen, "TFT18_Sivir": Sivir, "TFT18_Soraka": Soraka,
    "TFT18_Taric": Taric, "TFT18_Teemo": Teemo, "TFT18_Tristana": Tristana,
    "TFT18_Varus": Varus, "TFT18_Veigar": Veigar, "TFT18_Vi": Vi,
    "TFT18_Warwick": Warwick, "TFT18_Xayah": Xayah, "TFT18_Yorick": Yorick,
    "TFT18_Yunara": Yunara, "TFT18_Zyra": Zyra,
}


def has_driver(unit):
    return unit["api"] in DRIVERS


def driver_for(unit):
    cls = DRIVERS.get(unit["api"])
    if cls is None:
        raise KeyError(f"no driver for {unit['api']}")
    return cls()
