"""Units and breakpoints for the browser's item-independent trait search.

The catalog includes every shop unit, including units and traits without a
combat model. It does not choose items, resolve combat effects, or simulate.
"""

from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from tft_board import ELDER_DRAGON, RIFTBEAST, SOURCE_HASH as BOARD_HASH, unit_slots


SCHEMA = "tft-trait-flex-v1"
SOURCE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def source_stale():
    import tft_lobby

    return (hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != SOURCE_HASH
            or hashlib.sha256(Path(tft_lobby.__file__).read_bytes()).hexdigest() != tft_lobby.SOURCE_HASH)


def shop_model(snap):
    """Reviewed shop parameters, with tier populations from this exact roster."""
    import tft

    try:
        model = json.loads((Path(tft.set_dir(snap.set_no)) / "shop-model.json").read_bytes())
    except FileNotFoundError:
        return None
    odds, bags, slots = (model.get(key) for key in ("costOdds", "bagCopies", "visibleSlots"))
    if (model.get("schema") != "tft-level-eight-shops-v1" or model.get("set") != snap.set_no
            or model.get("level") != 8 or model.get("refreshCost") != 2
            or not isinstance(odds, list) or len(odds) != 5
            or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in odds)
            or not math.isclose(sum(odds), 1, abs_tol=1e-12)
            or not isinstance(bags, list) or len(bags) != 5
            or any(type(value) is not int or value < 3 for value in bags)
            or slots not in ([4, 5], [5])):
        raise ValueError("invalid TFT level-eight shop model")
    populations = Counter(unit["cost"] for unit in snap.units.values())
    return dict(model, championCounts=[populations[cost] for cost in range(1, 6)])


def catalog(snap):
    """Extract only unit identity, base roles, slot usage and trait thresholds.

    Levels retain every activation threshold, including one-unit solo traits.
    Shared levels separately identify thresholds needing at least two units'
    contributions for callers that need that distinction.
    Default role and intrinsic range determine the line; item-dependent form
    changes are not chosen.
    """
    import tft
    import tft_lobby

    unit_icons, trait_icons = tft.ui_icons(snap)
    traits = []
    for api, trait in sorted(snap.traits.items(), key=lambda pair: (pair[1]["name"], pair[0])):
        levels = sorted({int(level) for level in trait["levels"] if level > 0})
        traits.append({"api": api, "name": trait["name"], "icon": trait_icons.get(api),
                       "levels": levels, "sharedLevels": [level for level in levels if level >= 2]})

    units = []
    for api, unit in sorted(snap.units.items(), key=lambda pair: (pair[1]["cost"], pair[1]["name"], pair[0])):
        trait_apis = list(dict.fromkeys(unit["traitApis"]))
        if any(trait not in snap.traits for trait in trait_apis):
            raise ValueError(f"unknown trait on {api} in TFT trait catalog")
        contributions = {trait: 1 for trait in trait_apis}
        if api == ELDER_DRAGON and RIFTBEAST in contributions:
            contributions[RIFTBEAST] = 2
        attack_range = unit["stats"]["range"]
        extra_range = unit.get("curve", {}).get("AdditionalAttackRange")
        if extra_range:
            # Nidalee's default Marksman kit stores its innate four-hex bonus
            # in the ability, leaving the sheet's base range at one.
            attack_range += tft.curve_at(extra_range, 1)
        objective = unit["objective"]
        units.append({"api": api, "slug": tft.unit_slug(unit), "name": unit["name"],
                      "cost": unit["cost"], "icon": unit_icons.get(api),
                      "role": unit["roleName"], "kind": unit["kind"], "objective": objective,
                      "range": attack_range,
                      "frontline": objective in ("tank", "fighter") and attack_range <= 2,
                      "backline": objective == "carry" and attack_range > 2,
                      "slotCost": unit_slots(api), "traitApis": trait_apis,
                      "traitContributions": contributions})

    result = {"schema": SCHEMA, "set": snap.set_no, "patch": snap.patch,
              "level": 8, "boardSlots": 8, "units": units, "traits": traits}
    shop = shop_model(snap)
    if shop is not None:
        result["shop"] = shop
        try:
            result["lobbyStats"] = tft_lobby.catalog_profiles(snap)
        except ValueError as error:
            # Population failures affect the lobby approximation, while the
            # unit/trait search and uncontested shop model remain available.
            result["lobbyStats"] = {"schema": tft_lobby.SCHEMA, "profiles": [],
                                    "defaultProfile": None, "error": str(error)}
    return dict(result, revision=tft.json_hash([SOURCE_HASH, BOARD_HASH, tft_lobby.SOURCE_HASH, result]))
