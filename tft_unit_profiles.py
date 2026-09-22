"""Unit response curves for theoretical composition evaluation.

These are measurements against immortal generic targets, not matches against
chosen champion boards. Target defenses, area coverage, incoming raw DPS,
damage mix, pulse interval and debuffs are explicit inputs. Native sampling
keeps the existing ability timing and effective self sustain mechanics while
removing target kills and win/loss scoring.

Only healing that repaired missing health and shields that absorbed damage
count as effective self sustain. Ally output remains *potential*: a caller
must model a recipient before crediting it as effective team durability.
Surviving a requested horizon is a lower bound on survival, not immortality.
"""

from collections import Counter, OrderedDict
from copy import deepcopy
import math

import tft
from tft_comp_traits import RIFTBEAST


PROFILE_VERSION = 3
PROFILE_CACHE_LIMIT = 4096


def _number(value, name, *, positive=False, fraction=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    if fraction and value > 1:
        raise ValueError(f"{name} must be a fraction from 0 to 1")
    return value


def generic_targets(*, incoming_dps=0.0, physical_share=0.5, target_hp=2500.0,
                    target_armor=80.0, target_mr=80.0, target_count=3,
                    pressure_interval=1.0, wound=0.0, sunder=0.0, shred=0.0,
                    target_sunder=0.0, target_shred=0.0):
    """An abstract damage budget; every target has identical explicit stats.

    The physical share is delivered as attacks and the magic share as
    scheduled spells, with no critical strike multiplier or enemy mana bar.
    Sources stagger their first pulses evenly through one interval. Coverage
    (spread/clump) is applied separately by the existing unit ability driver.
    """
    incoming_dps = _number(incoming_dps, "incoming_dps")
    physical_share = _number(physical_share, "physical_share", fraction=True)
    target_hp = _number(target_hp, "target_hp", positive=True)
    target_armor = _number(target_armor, "target_armor")
    target_mr = _number(target_mr, "target_mr")
    pressure_interval = _number(pressure_interval, "pressure_interval", positive=True)
    debuffs = {key: _number(value, key, fraction=True)
               for key, value in (("wound", wound), ("sunder", sunder), ("shred", shred))}
    target_debuffs = {"sunder": _number(target_sunder, "target_sunder", fraction=True),
                      "shred": _number(target_shred, "target_shred", fraction=True)}
    if type(target_count) is not int or not 1 <= target_count <= 8:
        raise ValueError("target_count must be an integer from 1 to 8")
    pulse = incoming_dps * pressure_interval / target_count
    slots = []
    for index in range(target_count):
        start = pressure_interval * (index + 1) / target_count
        slots.append({
            "hp": target_hp, "armor": target_armor, "mr": target_mr,
            "kind": "tank", "nearby": True, "streams": 1,
            "ad": pulse * physical_share, "as": 1.0 / pressure_interval,
            "attackStart": start,
            "ability": pulse * (1.0 - physical_share), "physicalShare": 0.0,
            "castInterval": pressure_interval if physical_share < 1 and pulse > 0 else 0.0,
            "castStart": start,
            "manaMax": 0.0, "manaStart": 0.0,
            "manaPerAttack": 0.0, "manaFromDamage": False,
        })
    return {"slots": slots, "critEv": 1.0,
            "enemyDebuffs": debuffs, "targetDebuffs": target_debuffs}


def residual_mixed_ehp(pools, physical_share):
    """Mix each sequential health pool before adding their capacities."""
    physical_share = _number(physical_share, "physical_share", fraction=True)
    total = 0.0
    for pool in pools:
        physical, magic = pool["physicalEhp"], pool["magicEhp"]
        if physical_share == 1:
            total += physical
        elif physical_share == 0:
            total += magic
        elif physical > 0 and magic > 0:
            total += 1.0 / (physical_share / physical + (1.0 - physical_share) / magic)
    return total


def _burn_adjusted(spec, item_burn, inferno_burn):
    """Assign each nonstacking team burn channel to one measured holder.

    Cinderling's native burn and Brambleback's Alpha burn use the item
    channel. Removing them preserves every other item/trait/ability effect.
    Shared templates and item records are never mutated.
    """
    result = dict(spec)
    if not item_burn:
        for key in ("pool", "items"):
            result[key] = [{key: value for key, value in item.items()
                            if key not in ("burnOnHit", "burnAura", "burnAuraByRange")}
                           for item in spec[key]]
        native = {"TFT18_Cinderling": "BurnDuration", "TFT18_Brambleback": "TraitBurnDuration"}
        duration = native.get(spec["unit"]["api"])
        if duration:
            result["kits"] = {name: dict(kit, rows=dict(kit["rows"], BurnAmount=0.0,
                                                       **{duration: 0.0}))
                              for name, kit in spec["kits"].items()}
    if not inferno_burn:
        result["traits"] = [dict(trait) for trait in spec["traits"]]
        for trait in result["traits"]:
            if trait.get("api") == "DA_18_Inferno":
                trait.pop("burnOnHit", None)
    return result


class UnitProfiles:
    """Cache reusable immutable measurements for one data snapshot.

    ``effects`` holds the board's already resolved trait effects for this
    member, matching ``tft.cell_spec``'s native ``traits`` representation.
    Cached return values must be treated as read-only by callers.
    """

    def __init__(self, snap, geometry, *, cache_limit=PROFILE_CACHE_LIMIT):
        if geometry not in tft.GEOMETRIES:
            raise ValueError("unknown target geometry")
        if type(cache_limit) is not int or cache_limit < 1:
            raise ValueError("cache_limit must be a positive integer")
        self.snap, self.geometry = snap, geometry
        self.cache_limit = cache_limit
        self.item_fx = tft.load_item_effects(snap.set_no)
        self.templates, self.items = {}, {}
        self.results = OrderedDict()
        self.stats = Counter()

    def spec(self, api, star, effects, items, *, alpha=False, item_burn=True,
             inferno_burn=True, **conditions):
        """Build a complete standalone spec without named opponent inputs."""
        if api not in self.snap.units:
            raise ValueError(f"unknown champion {api!r}")
        if type(star) is not int or star not in tft.unit_stars(self.snap.units[api]):
            raise ValueError("unsupported champion star level")
        items = tuple(sorted(items))
        if len(items) > 3:
            raise ValueError("a unit can hold at most three items")
        if type(item_burn) is not bool or type(inferno_burn) is not bool:
            raise ValueError("burn channel flags must be booleans")
        dummy = generic_targets(**conditions)
        # The probes stand in the geometry's frontline lanes, so an ability
        # with a stated radius measures this board the way it measures the
        # damage board instead of covering every target that counts.
        tft.board_positions(dummy["slots"], self.geometry)
        signature = api, star, tft.json_hash(effects), bool(alpha)
        if signature not in self.templates:
            unit = self.snap.units[api]
            template = tft.cell_spec(self.snap, unit, star, self.geometry, [], dummy,
                                     duration=1.0, pressure=False, item_fx=self.item_fx)
            template.update(immortal=True, targetDebuffs={}, traits=deepcopy(effects))
            for effect in template["traits"]:
                if effect.get("api") == RIFTBEAST:
                    effect["riftbeast"] = bool(alpha)
            self.templates[signature] = template
        for item in items:
            if (api, item) not in self.items:
                self.items[api, item] = tft.item_spec(
                    self.snap, item, self.item_fx, self.snap.units[api])
        native_items = [self.items[api, item] for item in items]
        if any(item.get("unique") and items.count(item["api"]) > 1 for item in native_items):
            raise ValueError("a unique item cannot be repeated on one unit")
        result = dict(self.templates[signature], dummies={"critEv": 1.0, "slots": dummy["slots"]},
                      enemyDebuffs=dummy["enemyDebuffs"],
                      targetDebuffs=dummy["targetDebuffs"],
                      pressure=conditions.get("incoming_dps", 0.0) > 0,
                      items=native_items)
        form = None
        resolved = {}
        if result["unit"]["hasForms"]:
            resolved = tft.engine().compose_fx(result)
            form = resolved["form"]
        result["unit"] = dict(result["unit"], form=form,
                              **{key: resolved[key] for key in ("kind", "objective", "range") if key in resolved})
        if api == "TFT18_Nidalee" and form == "AD":
            # The cougar is a melee Assassin; preserve the stat-based form
            # tie-breaker while applying the transformed combat role.
            result["unit"].update(kind="Assassin", range=result["kits"]["AD"]["stats"]["range"])
        if not item_burn or not inferno_burn:
            result = _burn_adjusted(result, item_burn, inferno_burn)
        return result

    def curve(self, api, star, effects, items, times, *, alpha=False, **conditions):
        """Return opening EHP and cumulative output at every requested time.

        Samples include all events at their time. A champion that dies keeps
        its final damage and sustain totals for later samples. ``holding``
        also includes on-death bodies; ``alive`` describes the champion.

        ``incoming`` is raw damage received, including lethal overkill;
        ``incomingSpent`` removes that overkill. ``denied`` counts raw attacks
        skipped by stun/untargetability, but delayed scheduled magic pulses
        are not all counted as denial. ``residualMixedEhp`` values current HP,
        active shields and spawned bodies using current defenses. Temporary
        defenses may expire, so it is a capacity estimate at that instant.
        """
        times = tuple(_number(value, "time") for value in times)
        if not times or len(times) > 4096 or any(a >= b for a, b in zip(times, times[1:])):
            raise ValueError("times must contain 1 to 4096 strictly increasing values")
        key = (api, star, tft.json_hash(effects), tuple(sorted(items)), times,
               bool(alpha), tft.json_hash(conditions))
        if key in self.results:
            self.stats["cacheHits"] += 1
            self.results.move_to_end(key)
            return self.results[key]
        spec = self.spec(api, star, effects, items, alpha=alpha, **conditions)
        opening, samples = tft.engine().measure_response(spec, times)
        physical_share = conditions.get("physical_share", 0.5)
        for observation in (opening, *samples):
            observation["residualMixedEhp"] = residual_mixed_ehp(
                observation["residualPools"], physical_share)
        result = {"opening": opening, "kind": opening.get("kind", spec["unit"]["kind"]),
                  "form": opening["form"], "range": opening.get("range", spec["unit"]["range"]),
                  "samples": samples}
        self.stats["measurements"] += 1
        self.results[key] = result
        while len(self.results) > self.cache_limit:
            self.results.popitem(last=False)
        return result

    def response(self, api, star, effects, items, *, duration, alpha=False, **conditions):
        """Convenience wrapper for one requested measurement horizon."""
        result = self.curve(api, star, effects, items, [duration], alpha=alpha, **conditions)
        return {key: value for key, value in result.items() if key != "samples"} | result["samples"][0]

    def measure_team(self, specs, fronts, *, window, pressure, physical_share,
                     control_interval=8.0, control_duration=0.0, target_order=None):
        """Raw shared-pressure observations for the independent Python oracle.

        Production scoring keeps these values in Rust. The oracle converts
        the native combat observations and independently computes capacity.
        ``target_order`` contains frontline caller indices in priority order;
        exported target order and initial source ownership remain caller-indexed.
        """
        targeting = {} if target_order is None else {"target_order": target_order}
        result = tft.engine().measure_theory_team(specs, fronts, window, pressure,
            physical_share, control_interval, control_duration, **targeting)
        for sample in result["samples"]:
            sample["residualMixedEhp"] = residual_mixed_ehp(sample["residualPools"], physical_share)
        return result

    def theory_opening(self, api, star, effects, items, *, alpha=False, source_mask=None, **conditions):
        """Opening state with optional assignments for three pressure sources.

        Bits 0 through 2 identify assigned sources, so 0 means unfocused and 7 means
        all three. An explicit mask also reports initial targetability. None
        retains the native default opening API and its existing sample shape.
        """
        spec = self.spec(api, star, effects, items, alpha=alpha, **conditions)
        frontline = (self.snap.units[api]["objective"] in ("tank", "fighter")
                     or spec["unit"]["kind"] == "Assassin")
        targeting = {} if source_mask is None else {"source_mask": source_mask}
        sample = tft.engine().theory_opening(spec, frontline, **targeting)
        sample["residualMixedEhp"] = residual_mixed_ehp(sample["residualPools"], conditions.get("physical_share", .5))
        return {"opening": sample, "kind": spec["unit"]["kind"], "form": spec["unit"].get("form")}
