"""Board capacity and trait contributions shared by composition planners.

Elder Dragon consumes two team slots and contributes two Riftbeast in total.
Source: Riot's Enchanted Wilds overview (August 2026), Riftbeast section:
https://teamfighttactics.leagueoflegends.com/en-sg/news/game-updates/enchanted-wilds-overview/
"""
from collections import Counter
import hashlib
from pathlib import Path


ELDER_DRAGON = "TFT18_ElderDragon"
RIFTBEAST = "DA_Riftbeast18"
APEX_PREDATOR = "DA_18_ApexPredator"
DEFAULT_BOARD_SLOTS = 8
MAX_BOARD_SLOTS = 9
BOARD_PLAN_MODEL = "level8-core-level9-cap-v1"
SOURCE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def unit_slots(api):
    return 2 if api == ELDER_DRAGON else 1


def slots_used(roster):
    """Accept champion APIs or resolved member/unit records."""
    return sum(unit_slots(unit["api"] if isinstance(unit, dict) else unit) for unit in roster)


def trait_counts(snap, roster):
    apis = [unit["api"] if isinstance(unit, dict) else unit for unit in roster]
    counts = Counter(trait for api in apis for trait in set(snap.units[api]["traitApis"]))
    if ELDER_DRAGON in apis:
        # Two total, rather than two additional copies of the listed trait.
        counts[RIFTBEAST] += 1
    return counts
