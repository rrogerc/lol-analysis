"""Required antiheal coverage from confirmed, usable Set 18 sources.

The pinned 18.1d tooltips/rows in data/tft/set18/18.1d/metatft.json identify
Morellonomicon, Red Buff, Sunfire Cape and active Inferno as Wound sources.
Cinderling's base ability separately applies 20% Wound for 4 seconds. Burn
alone is not evidence of Wound: Brambleback's Alpha burn and Elder Dragon's
Ignite are not accepted. Radiant items are outside the modeled standard pool.

This is a composition requirement, not a score bonus or a promise of full
uptime. Native effect resolution checks application/reach without a fight.
"""
from copy import deepcopy
import math

import tft
from tft_unit_profiles import UnitProfiles


INFERNO = "DA_18_Inferno"
CINDERLING = "TFT18_Cinderling"
_ITEMS = {
    "DA_Morellonomicon": ("WoundPercent", "BurnAndWoundDuration", "burnOnHit"),
    "DA_RedBuff": ("Wound", "Duration", "burnOnHit"),
    "DA_SunfireCape": ("WoundPercent", "BurnWoundDuration", "burnAura"),
}


def _row(entity, name, column=1):
    values = entity.get("curve", {}).get(name)
    if not values:
        return 0.0
    value = tft.curve_at(values, column)
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0.0


def _channel(value):
    return (isinstance(value, (list, tuple)) and len(value) >= 2
            and all(isinstance(number, (int, float)) and math.isfinite(number) and number > 0
                    for number in value[:2]))


class AntihealPolicy:
    """A board's antiheal sources, cached by equipped loadout and Alpha state.

    ``has_source`` supports partial DP states. ``legal`` requires a complete
    roster selection; item replacements and support sales must be checked
    against the policy for their resulting roster/trait effects.
    """
    def __init__(self, snap, members, effects, *, profiles=None):
        self.snap, self.profiles = snap, profiles
        self.stars = {member["api"]: member["star"] for member in members}
        self.effects = deepcopy(effects)
        self._item_sources = {}
        self._base_sources = {api: [] for api in self.stars}
        self._item_definitions = {}
        for api, (wound_row, duration_row, application) in _ITEMS.items():
            item = snap.items.get(api)
            if not item:
                continue
            wound, duration = _row(item, wound_row) / 100.0, _row(item, duration_row)
            if 0 < wound <= 1 and duration > 0:
                self._item_definitions[api] = {"type": "item", "api": api, "name": item["name"],
                                               "wound": wound, "duration": duration,
                                               "application": application}

        inferno = snap.traits.get(INFERNO, {})
        inferno_wound = _row(inferno, "WoundPercent") / 100.0
        for api, star in self.stars.items():
            for effect in self.effects.get(api, []):
                if (effect.get("api") == INFERNO and _channel(effect.get("burnOnHit"))
                        and 0 < inferno_wound <= 1):
                    self._base_sources[api].append({"type": "trait", "api": INFERNO, "unitApi": api,
                        "name": inferno.get("name", "Inferno"), "wound": inferno_wound,
                        "duration": float(effect["burnOnHit"][1]), "application": "damage"})
                    break
            if api == CINDERLING:
                unit = snap.units[api]
                wound = _row(unit, "WoundLevel", star) / 100.0
                duration = _row(unit, "WoundDuration", star)
                if (0 < wound <= 1 and duration > 0 and _row(unit, "BurnAmount", star) > 0
                        and _row(unit, "BurnDuration", star) > 0):
                    self._base_sources[api].append({"type": "ability", "api": api, "unitApi": api,
                        "name": unit["name"], "abilityName": unit.get("ability", {}).get("name"),
                        "wound": wound, "duration": duration, "application": "ability"})

    def _items_for(self, api, option):
        items = tuple(sorted(option.get("items", ())))
        providers = sorted(set(items).intersection(self._item_definitions))
        if not providers or api not in self.stars:
            return ()
        key = api, items, bool(option.get("alpha"))
        if key in self._item_sources:
            return self._item_sources[key]
        if self.profiles is None:
            # Geometry changes coverage, not whether these sources can apply.
            self.profiles = UnitProfiles(self.snap, "clump")
        spec = self.profiles.spec(api, self.stars[api], self.effects.get(api, []), items, alpha=key[2])
        fx = tft.engine().compose_fx(spec)
        native_items = {item["api"]: item for item in spec["items"]}
        sources = []
        for item_api in providers:
            definition = self._item_definitions[item_api]
            application = definition["application"]
            native = native_items.get(item_api, {})
            channel = native.get(application)
            if application == "burnAura" and native.get("burnAuraByRange"):
                deferred = native["burnAuraByRange"]
                channel = deferred[:2] if fx["range"] <= deferred[2] else None
            active = fx.get(application)
            if application == "burnOnHit":
                active = next((entry for entry in active or () if _channel(entry)), None)
            if _channel(channel) and _channel(active):
                sources.append(dict(definition, unitApi=api))
        self._item_sources[key] = tuple(sources)
        return self._item_sources[key]

    def has_source(self, api, option):
        if api not in self.stars:
            return False
        if self._base_sources[api]:
            return True
        return bool(self._items_for(api, option))

    def legal(self, selected):
        return (set(selected) == set(self.stars)
                and any(self.has_source(api, option) for api, option in selected.items()))

    def sources(self, selected):
        sources = []
        for api, option in sorted(selected.items()):
            if api in self.stars:
                sources.extend(self._base_sources[api])
                sources.extend(self._items_for(api, option))
        return [dict(source) for source in sources]
