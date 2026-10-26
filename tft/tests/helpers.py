"""Shared test fixtures and simulation helpers for the TFT test suite."""

import copy

from tft import sim as tft

# Mechanics regressions pin patch 18.1d so publishing newer patches does not
# silently shift hand-computed values. Live snapshots are checked separately.
SNAP = tft.load_snapshot(18, "18.1d")
ENGINE = tft.engine()
ITEM_FX = tft.load_item_effects(SNAP.set_no)
TRAIT_FX = tft.load_trait_effects(SNAP.set_no)
DUMMY = dict(tft.dummies_for(SNAP), targetDebuffs={}, meleeRepositionSeconds=0.0)


def immortal(spec):
    """Return a copy of the dummy spec with unkillable health for timing tests."""
    return dict(spec, slots=[dict(s, hp=10 ** 6) for s in spec["slots"]])


def one_hitter(pre=100.0, period=1.0, attackers=1):
    """Dummies that cannot die and hit for `pre` once per period without crit."""
    slots = []
    for i, s in enumerate(DUMMY["slots"]):
        s = dict(s, hp=10 ** 6, ad=pre if i < attackers else 0.0, ability=0.0,
                 manaMax=0.0, manaStart=0.0, manaPerAttack=0.0, manaFromDamage=False)
        s["as"] = 1.0 / period
        slots.append(s)
    return dict(DUMMY, slots=slots, critEv=1.0, board=None, enemyDebuffs={})


def item(name):
    return SNAP.item(name)["api"]


def spec_for(unit_name, star=2, items=(), fx=(), geometry="clump", traits=(), duration=None,
             dummy=None, pressure=None, driver=None, timed=False):
    """Build a cell spec for one fight of `unit_name` with optional synthetic `fx`."""
    u = SNAP.unit(unit_name)
    spec = tft.cell_spec(SNAP, u, star, geometry, list(traits), dummy or DUMMY, duration, pressure,
                         ITEM_FX, TRAIT_FX, items=[item(x) for x in items], driver=driver,
                         cast_timing=None if timed else {})
    for extra in fx:
        spec["items"].append({"api": "test", "name": "test", "unique": False, "stats": [],
                              "adds": [], **extra})
    return spec


def run(unit_name, **kw):
    """Run one traced fight: (sheet, result)."""
    return ENGINE.simulate(spec_for(unit_name, **kw), True)


def fx_of(unit_name, **kw):
    return ENGINE.compose_fx(spec_for(unit_name, **kw))


def events(res, kind=None, src=None, target=None):
    return [e for e in res["trace"]
            if (kind is None or e[1] == kind) and (src is None or e[4] == src)
            and (target is None or e[3] == target)]


def sim(unit_name, items=(), star=2, geometry="clump", traits=(), duration=None, dummy=None,
        pressure=None, driver=None):
    """Run one fight through the public path with the unit's own driver."""
    u = SNAP.unit(unit_name)
    return tft.simulate(SNAP, u, star, [item(x) for x in items], geometry, list(traits),
                        dummy or DUMMY, duration, ITEM_FX, TRAIT_FX, driver, pressure)


def generic_targets(*, incoming_dps=0.0, physical_share=0.5, target_hp=2500.0,
                    target_armor=80.0, target_mr=80.0, target_count=3,
                    pressure_interval=1.0, wound=0.0, sunder=0.0, shred=0.0,
                    target_sunder=0.0, target_shred=0.0):
    """Synthetic target slots with staggered incoming physical/magic pressure."""
    pulse = incoming_dps * pressure_interval / target_count
    slots = []
    for index in range(target_count):
        start = pressure_interval * (index + 1) / target_count
        slots.append({
            "hp": float(target_hp),
            "armor": float(target_armor),
            "mr": float(target_mr),
            "kind": "tank",
            "nearby": True,
            "streams": 1,
            "ad": pulse * physical_share,
            "as": 1.0 / pressure_interval,
            "attackStart": start,
            "ability": pulse * (1.0 - physical_share),
            "physicalShare": 0.0,
            "castInterval": pressure_interval if physical_share < 1 and pulse > 0 else 0.0,
            "castStart": start,
            "manaMax": 0.0,
            "manaStart": 0.0,
            "manaPerAttack": 0.0,
            "manaFromDamage": False,
        })
    return {
        "slots": slots,
        "critEv": 1.0,
        "enemyDebuffs": {"wound": float(wound), "sunder": float(sunder), "shred": float(shred)},
        "targetDebuffs": {"sunder": float(target_sunder), "shred": float(target_shred)},
    }


def plain_spec(*, hp=1000, armor=0, mr=0, damage=100, target_armor=0,
               incoming_dps=0, physical_share=1, pressure_interval=1, fx=()):
    """Neutral single-target spec with flat stats and immortal targets."""
    dummy = generic_targets(incoming_dps=incoming_dps, physical_share=physical_share,
                            target_hp=150, target_armor=target_armor, target_mr=0,
                            target_count=1, pressure_interval=pressure_interval)
    spec = copy.deepcopy(spec_for("Ashe", star=1, dummy=dummy, driver="Driver",
                                  pressure=incoming_dps > 0, fx=list(fx)))
    spec.update(immortal=True, autoPressure=False,
                enemyDebuffs=dict(dummy["enemyDebuffs"]), targetDebuffs={})
    kit = spec["kits"]["base"]
    kit.update(baseAd=damage, hpStar=hp)
    kit["stats"].update(hp=hp, ad=damage, armor=armor, mr=mr,
                        mana=10**9, initialMana=0, critChance=0, critMult=1)
    kit["stats"]["as"] = 1.0
    spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
    return spec


def body_spec(*, hp=1000.0, armor=0.0, mr=0.0, pre=100.0, duration=1.5,
              debuffs=None, fx=(), dummy=None, unit="Leona", driver="Driver"):
    """Known health pool spec with no ability unless a driver is requested."""
    spec = copy.deepcopy(spec_for(unit, star=1, driver=driver, duration=duration,
                                  dummy=dummy or one_hitter(pre), fx=fx))
    kit = spec["kits"]["base"]
    kit["hpStar"] = hp
    kit["stats"].update(hp=hp, armor=armor, mr=mr)
    spec["enemyDebuffs"] = debuffs or {}
    return spec


def ally(name="Ashe", *, driver="Driver", hp=1000.0, ad=0.0,
         attack_speed=0.01, armor=0.0, mr=0.0, frontline=True, lane=3,
         priority=0, cast=False, fx=()):
    spec = spec_for(name, star=1, driver=driver, pressure=True, fx=fx)
    kit = spec["kits"]["base"]
    kit["hpStar"] = hp
    kit["baseAd"] = ad
    kit["stats"].update(hp=hp, ad=ad, armor=armor, mr=mr,
                        mana=10000.0 if cast else 0.0,
                        initialMana=10000.0 if cast else 0.0,
                        critChance=0.0, critMult=1.0)
    kit["stats"]["as"] = attack_speed
    spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
    spec["unit"]["castTime"] = 0.25
    return {"spec": spec, "frontline": frontline, "lane": lane, "priority": priority}


def enemy(**changes):
    return {"name": "Synthetic enemy", "hp": 10000.0, "armor": 0.0, "mr": 0.0,
            "kind": "tank", "frontline": True, "lane": 3, "streams": 1,
            "ad": 0.0, "as": 0.0, "ability": 0.0, "physicalShare": 1.0,
            "manaMax": 0.0, **changes}


def flat_calc(member, name, amount):
    member["spec"]["kits"]["base"]["calcs"][name]["terms"] = [
        {"type": "flat", "value": float(amount), "op": "add"}]


def fight(allies, enemies, duration=3.0, geometry="clump"):
    return ENGINE.simulate_team({"allies": allies, "enemies": enemies,
                                 "duration": duration, "geometry": geometry,
                                 "critEv": 1.0, "burnWound": 0.33}, True)


def team_trace(result, kind=None, *, source=None, target=None, name=None):
    return [event for event in result["trace"]
            if (kind is None or event["kind"] == kind)
            and (source is None or event["source"] == source)
            and (target is None or event["target"] == target)
            and (name is None or event.get("sourceName") == name)]


trace = team_trace


def match(allies, enemies, duration=3.0, initiative=0, **options):
    return ENGINE.simulate_match({"allies": allies, "enemies": enemies,
                                  "duration": duration, "geometry": "clump",
                                  "initiative": initiative, **options}, True)


def match_events(result, kind=None, side="ally", source=None, name=None):
    return [event for event in result["trace"]
            if (kind is None or event["kind"] == kind)
            and event["side"] == side
            and (source is None or event["source"] == source)
            and (name is None or event.get("sourceName") == name)]


def caster(name="Ahri", damage=100, **changes):
    member = ally(name, driver=name, cast=True, **changes)
    flat_calc(member, "MagicDamageCalc1", damage)
    if name == "Ahri":
        member["spec"]["kits"]["base"]["rows"].update(ChannelTime=0.25, HexPercentDamageFalloffTooltip=0.0)
    if name == "Leona":
        flat_calc(member, "GenericCalc1", 0)
        member["spec"]["kits"]["base"]["rows"]["StunDuration"] = 0.0
    return member


def fixture(name, *, hp=(10000, 10000, 10000), geometry="clump",
            duration=1.25, form=None, fx=()):
    effects = list(fx)
    if form:
        effects.append({"stats": [["adPct", 0.1]] if form == "AD" else [["ap", 10]]})
    dummy = {"critEv": 1.0, "slots": [
        {"hp": health, "armor": 0, "mr": 0, "ad": 0, "as": 0,
         "ability": 0, "manaMax": 0, "kind": "tank"} for health in hp]}
    spec = spec_for(name, star=2, duration=duration, geometry=geometry,
                    pressure=False, dummy=dummy, fx=effects)
    spec.update(autoPressure=False, enemyDebuffs={}, targetDebuffs={})
    spec["role"] = {"manaRegen": 0, "asPct": 0}
    spec["unit"]["castTime"] = 0.25
    for kit in spec["kits"].values():
        kit.update(baseAd=0, hpStar=10000)
        kit["stats"].update(hp=10000, ad=0, armor=0, mr=0,
                            critChance=0, critMult=1,
                            mana=1000, initialMana=1000)
        kit["stats"]["as"] = 0.01
        for calc in kit["calcs"].values():
            calc["terms"] = [{"type": "flat", "value": 0, "op": "add"}]
    return spec


def flat(spec, name, damage):
    for kit in spec["kits"].values():
        if name in kit["calcs"]:
            kit["calcs"][name]["terms"] = [
                {"type": "flat", "value": damage, "op": "add"}]


def rows(spec, **values):
    for kit in spec["kits"].values():
        kit["rows"].update(values)


def carry_result(spec):
    return ENGINE.simulate(spec, True)[1]


def gnar_spec(*, immortal=True, targets=1, target_hp=3000, incoming=0,
              transform=100, fx=()):
    dummy = generic_targets(incoming_dps=incoming, physical_share=1,
                            target_hp=target_hp, target_armor=0, target_mr=0,
                            target_count=targets)
    spec = spec_for("Gnar", star=2, geometry="spread" if targets == 1 else "clump",
                    duration=4.5, pressure=incoming > 0, dummy=dummy, fx=fx)
    spec.update(immortal=immortal, autoPressure=False, enemyDebuffs={}, targetDebuffs={})
    spec["role"] = {"manaRegen": 0, "asPct": 0}
    spec["unit"]["castTime"] = 0.25
    kit = spec["kits"]["base"]
    kit.update(baseAd=0, hpStar=1000)
    kit["stats"].update(hp=1000, armor=0, mr=0, ad=0, critChance=0, critMult=1,
                        mana=10, initialMana=0)
    kit["stats"]["as"] = 1
    kit["rows"].update(RagePerAttack=1, TransformRageMax=1, RagePerSecond=0, StunDuration=0)
    for name, damage in (("HealthCalc1", 0), ("GenericCalc1", 0),
                         ("PhysicalDamageCalc3", transform),
                         ("PhysicalDamageCalc1", 200), ("PhysicalDamageCalc2", 50)):
        kit["calcs"][name] = {"dtype": "physical", "terms": [
            {"type": "flat", "value": damage, "op": "add"}]}
    return spec
