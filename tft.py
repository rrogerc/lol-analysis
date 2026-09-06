"""Teamfight Tactics build math: optimal item combinations per unit, from
first principles — no match data.

Numbers come from data, mechanics from code:

- data/tft/set<N>/<patch>/metatft.json — MetaTFT's public lookup file for
  the set: every unit's base stats, per-star curve tables and ability
  formulas (Riot's own calculation terms), every item's stat line and
  curve table, every trait's per-breakpoint values, and Riot's role tags.
  The current CommunityDragon export lacks Set 18's ability calculations;
  this structured lookup supplies them, with live corrections below.
- communitydragon.json — the archived Set 18 export for asset references
  and source comparison. Its base stats can lag hotfixes, so they never
  silently replace the corrected simulation inputs.
- data/tft/set<N>/<patch>/bins.json — per-unit timings distilled from
  Community Dragon's character bins: ability cast time, attack windup.
- data/tft/set<N>/<patch>/patchnotes.json — every "old ⇒ new" line of the
  patch's notes, so `lol.py tft check` can flag a snapshot value the notes
  say has changed (MetaTFT's file has lagged the live patch before).
- data/tft/set<N>/<patch>/overrides.json — corrections with provenance,
  applied on top of that patch's snapshot (typically: patch-note values
  the snapshot doesn't have yet). Automatic refreshes reconcile them
  against new notes and source changes before carrying them forward.
- data/tft/set<N>/item-effects.json, trait-effects.json — which item
  passives and trait bonuses the engine models, as references to the
  data's own rows (so a number change in the data flows through).
- tft_engine/ — the compiled engine (Rust, imported as lol_tft from
  lol_tft.abi3.so at the repo root; jobs/build-engine.sh builds it): the
  fight, the item and trait effects, and one short driver per unit — the
  ability's shape (how many targets, over how long, what repeats), reading
  its numbers from the unit's calcs and curve rows. This module resolves
  every number the data holds into a spec the engine consumes (kit_spec,
  item_spec, trait_spec, cell_spec), so numbers stay data and mechanics
  stay code.

The fight is a unit's mana cycle against stat dummies derived from
the set's own units: attacks at the unit's attack speed grant role-based
mana, the ability casts when the bar fills, damage goes through the
armor/MR formula with sunder, shred, crit, Precision and post-mitigation
damage amp. "spread" puts the dummies out of each other's reach (area
abilities hit one), "clump" puts them together (area abilities hit all).

What a unit is scored on follows Riot's role label for it (the second word
of "Attack Caster", "Magic Tank", …):

- Marksman, Caster, Specialist — a carry: the dummies never hit back and
  the build is ranked by the time to kill all three, then by damage.
- Fighter, Assassin — a frontliner: the dummies hit back with the set's
  median attacks and abilities, the unit can die, and the build is ranked
  by kill time, then by damage dealt before dying.
- Tank — three frontliners screen two backline damage dealers. Nearby
  effects reach the frontline; the backline applies physical, mixed, or
  magic-burst pressure, with continuous Wound, Sunder and Shred. Builds rank by hold
  time, including on-death bodies. Survivors of the 60-second benchmark
  are compared again at twice the damage; surviving both is a tie.

Every 3-item combination is simulated and ranked that way.
"""

import glob
from functools import lru_cache
import hashlib
import itertools
import json
import multiprocessing as mp
import os
import re
import statistics
import sys
import time
import urllib.request
from urllib.error import HTTPError
import shutil
import tempfile
from types import SimpleNamespace
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser

from common import BASE_DIR

TFT_DATA_DIR = os.path.join(BASE_DIR, "data", "tft")
CACHE_DIR = os.path.join(BASE_DIR, ".cache", "tft")
SCENARIO_CACHE_DIR = CACHE_DIR   # the name webapp's warmer expects
REFRESH_STATE_FILE = os.path.join(BASE_DIR, "jobs", ".state", "refresh-tft.json")
DEFAULT_SET = 18

METATFT_URL = "https://data.metatft.com/lookups/TFTSet{set}_latest_en_us.json"
CDRAGON_BIN = ("https://raw.communitydragon.org/latest/game/characters/"
               "{name}.cdtb.bin.json")
CDRAGON_TFT = "https://raw.communitydragon.org/latest/cdragon/tft/en_us.json"
PATCH_NOTES_INDEX = "https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/"
PATCH_NOTES_URL = PATCH_NOTES_INDEX + "teamfight-tactics-patch-{slug}/"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) lol-analysis/0.1 (+github.com/rrogerc/lol-analysis)"

# --- the rules of the set that are not in the data ------------------------
AD_PER_STAR = 1.5      # attack damage multiplies by this per star above one
HP_PER_STAR = 1.8      # max health likewise
BASE_AP = 100.0        # every unit's ability power before items
AS_CAP = 5.0           # attacks per second, soft cap
CRIT_EXCESS_TO_DAMAGE = 1.0   # 1% crit chance over 100% -> 1% crit damage
PRECISION_EXTRA_CRIT_DAMAGE = 0.10  # a second source of Precision
MANA_LOCK_S = 1.0      # no mana for a second after a cast starts
CAST_TIME_DEFAULT = 0.25  # when the character bin has no cast time
TICK_S = 0.25          # granularity of regen, burns and timed stacks
# fighters gain attack speed by stage: "5-30% (based on Stage)"; the
# per-stage curve is patch 15.4's "Stage 2-6: 5/10/20/30/30%"
FIGHTER_AS_BY_STAGE = {1: 0.05, 2: 0.05, 3: 0.10, 4: 0.20, 5: 0.30, 6: 0.30}
STAGE = 4              # fighters' role bonus scales with the stage
# per-role mana per attack (Riot's role descriptions); casters also regenerate
ROLE_MANA = {"Assassin": 10, "Fighter": 10, "Marksman": 10, "Caster": 7,
             "Tank": 5}
CASTER_MANA_REGEN = 2.0
# tanks also "gain Mana from taking damage": the long-standing community
# formula, 1% of pre-mitigation plus 3% of post-mitigation damage, capped
# per hit (Riot publishes no numbers for it)
TANK_MANA_PER_PREMIT = 0.01
TANK_MANA_PER_POSTMIT = 0.03
TANK_MANA_PER_HIT_CAP = 42.5
# assassins "take 15% less damage from all enemies except the current target"
ASSASSIN_OFFTARGET_REDUCTION = 0.15
TANK_ROLE_TAG = "Role.Tank"
PRISMATIC_STYLE = 5    # trait breakpoint styles at or above this are chase tiers

FIGHT_DURATION = 20.0  # carries and frontliners
TANK_DURATION = 60.0   # tanks are scored on how long they last, so longer
N_DUMMIES = 3
DUMMY_STAR = 2
# The first enemy is a tougher benchmark; the other slots keep set medians.
FRONT_TANK_DEFENSES = {"hp": 3000, "armor": 70, "mr": 70}
BOARD_SIZE = 8         # the enemy board at STAGE: what a tank ("more likely
                       # to be targeted") is hit by; a fighter fights the
                       # three dummies in front of it
CACHED_ROWS = 250

# Synthetic comparisons: three nearest frontliners screen two damage dealers.
# Carry pressure is calibrated with itemized references in the Rust engine;
# the three presets vary backline damage type/timing at the same DPS budget.
TANK_FRONTLINERS = 3
TANK_BACKLINERS = 2
TANK_REFERENCE_DURATION = 20.0
TANK_REFERENCE_CARRIES = (
    ("Aphelios", ("DA_GuinsoosRageblade", "DA_KrakensFury", "DA_InfinityEdge")),
    ("Ahri", ("DA_SpearOfShojin", "DA_JeweledGauntlet", "DA_ArchangelsStaff")),
)
TANK_THREATS = {
    "mixed": {"label": "Mixed damage", "attackShare": 0.5,
              "spellPhysicalShare": 0.0, "attackInterval": 1.0,
              "castInterval": 4.0, "burst": False,
              "description": "Frontliners screen carries dealing half physical attacks and half staggered magic spells."},
    "physical": {"label": "Physical attacks", "attackShare": 0.85,
                 "spellPhysicalShare": 1.0, "attackInterval": 0.75,
                 "castInterval": 4.0, "burst": False,
                 "description": "Frontliners screen carries dealing physical damage: 85% attacks and 15% spells."},
    "magic": {"label": "Magic burst", "attackShare": 0.15,
              "spellPhysicalShare": 0.0, "attackInterval": 1.5,
              "castInterval": 4.0, "burst": True,
              "description": "Frontliners screen carries dealing 15% physical attacks and 85% magic damage in four-second bursts."},
}
# Game values come from the corrected snapshot, just like item passives.
# These benchmark debuffs have full uptime from combat start. They reduce
# healing and total resists, without reducing shields or granting extra burn.
TANK_DEBUFF_ROWS = {
    "wound": ("DA_Morellonomicon", "WoundPercent"),
    "sunder": ("DA_LastWhisper", "SunderPercent"),
    "shred": ("DA_VoidStaff", "ShredPercent"),
}

# What a unit is scored on, from the team-role half of Riot's label
# ("Attack Fighter" -> Fighter). recommendedItems can point a Specialist
# at another role's table; that wins (Master Yi and Gnar itemize as Fighters,
# Caitlyn as a Marksman).
OBJECTIVE_BY_KIND = {"Tank": "tank", "Fighter": "fighter", "Assassin": "fighter",
                     "Marksman": "carry", "Caster": "carry", "Specialist": "carry"}
OBJECTIVES = {
    "carry": "the dummies never hit back; ranked by time to kill all three, then damage dealt",
    "fighter": "the dummies hit back and the unit can die; ranked by kill time, then damage dealt before dying",
    "tank": "three frontliners screen two backline damage dealers, with continuous heal cut, Sunder and Shred; nearby effects reach the frontline while the backline keeps attacking; ranked by hold time including on-death bodies, with 60-second survivors compared at double damage and builds surviving both tied",
}
PRESSURED = ("fighter", "tank")   # objectives whose fights have the dummies attacking

# Scenario axes. A cell is one (star, geometry, trait context). Every unit
# is simulated at 1★ and 2★, the 1–3 costs at 3★ as well: a 3★ 4- or
# 5-cost is an auto-win, not a build question (Roger's call, 2026-09-05).
STARS = (1, 2, 3)
STARS_BY_COST = {1: (1, 2, 3), 2: (1, 2, 3), 3: (1, 2, 3), 4: (1, 2), 5: (1, 2)}
GEOMETRIES = {
    "spread": "targets out of each other's reach: area abilities hit one",
    "clump": "targets together: area abilities hit every one still standing",
}
TRAIT_CONTEXTS = {
    "bare": "no traits active",
    "low": "the unit's own modeled traits at their first breakpoint",
    "high": "the unit's own modeled traits at their highest breakpoint",
}


def scenarios():
    """{key: scenario} in warm order: every star level any unit is
    simulated at, both geometries, three trait contexts. `unit_scenarios`
    picks a unit's own cells out of these."""
    out = {}
    for star in STARS:
        for geo in GEOMETRIES:
            for ctx in TRAIT_CONTEXTS:
                key = f"s{star}-{geo}-{ctx}"
                out[key] = {"key": key, "star": star, "geometry": geo,
                            "traits": ctx, "threat": "mixed",
                            "label": f"{star}★ · {geo} · traits {ctx}"}
                for threat, profile in TANK_THREATS.items():
                    if threat != "mixed":
                        variant = f"{key}-{threat}"
                        out[variant] = {**out[key], "key": variant, "threat": threat,
                                        "label": f"{out[key]['label']} · {profile['label']}"}
    return out


def fight_duration(unit):
    return TANK_DURATION if unit["objective"] == "tank" else FIGHT_DURATION


SCENARIOS = scenarios()


def unit_stars(unit):
    """The star levels a unit is simulated at, by its cost."""
    return STARS_BY_COST.get(unit["cost"], (1, 2))


def unit_scenarios(unit):
    """The cells a unit has: its star levels × geometries × trait
    contexts, with three incoming damage profiles for tanks."""
    stars = unit_stars(unit)
    return {k: sc for k, sc in SCENARIOS.items() if sc["star"] in stars
            and (unit["objective"] == "tank" or sc["threat"] == "mixed")}


# ---------------------------------------------------------------------------
# snapshots: fetch and load
# ---------------------------------------------------------------------------

def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def fetch_json(url):
    return json.loads(fetch_bytes(url))


def json_hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def set_dir(set_no):
    return os.path.join(TFT_DATA_DIR, f"set{set_no}")


def patch_dirs(set_no):
    """Archived patches of a set, oldest first."""
    d = set_dir(set_no)
    if not os.path.isdir(d):
        return []
    return sorted((p for p in os.listdir(d)
                   if re.fullmatch(r"\d+\.\d+[a-z]?", p)
                   and os.path.exists(os.path.join(d, p, "metatft.json"))
                   and os.path.exists(os.path.join(d, p, "meta.json"))),
                  key=tft_patch_key)


def tft_patch_key(patch):
    """Order TFT hotfixes after their base patch, before the next patch."""
    match = re.fullmatch(r"(\d+)\.(\d+)([a-z]?)", patch)
    if not match:
        raise ValueError(f"invalid TFT patch {patch!r}; expected e.g. 18.1d")
    major, minor, suffix = match.groups()
    return int(major), int(minor), ord(suffix) - ord("a") + 1 if suffix else 0


class PatchNotesParser(HTMLParser):
    """Keep update dates, sections and parent item names with each change."""

    def __init__(self):
        super().__init__()
        self.major = self.section = self.update = ""
        self.heading = None
        self.heading_text = []
        self.updates = []
        self.entries = []
        self.stack = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if self.skip:
            return
        if tag in ("h2", "h3", "h4"):
            self.heading, self.heading_text = tag, []
        elif tag == "li":
            parent = " ".join("".join(self.stack[-1]["text"]).split()).rstrip(":") if self.stack else ""
            self.stack.append({"text": [], "parent": parent, "section": self.section,
                               "update": self.update, "major": self.major})
        elif tag == "br" and self.stack:
            self.stack[-1]["text"].append(" ")

    def handle_data(self, data):
        if self.skip:
            return
        if self.heading:
            self.heading_text.append(data)
        if self.stack:
            self.stack[-1]["text"].append(data)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == self.heading:
            label = " ".join("".join(self.heading_text).split())
            if tag == "h2":
                self.major = self.section = label
                self.update = ""
            elif tag == "h3" and self.major.lower() == "mid-patch updates":
                self.update = label
                if label and label not in self.updates:
                    self.updates.append(label)
            else:
                self.section = label
            self.heading = None
        elif tag == "li" and self.stack:
            entry = self.stack.pop()
            entry["text"] = " ".join("".join(entry["text"]).split())
            self.entries.append(entry)


def patch_notes_document(html, base_patch):
    """Riot lists newest hotfix sections first; first update is B, then C/D."""
    parser = PatchNotesParser()
    parser.feed(html)
    explicit = re.findall(rf"\b{re.escape(base_patch)}([b-z])\b", unescape(html), re.I)
    suffix = max((s.lower() for s in explicit), default="")
    if not suffix and parser.updates:
        suffix = chr(ord("a") + len(parser.updates))
    patch = base_patch + suffix
    changes = []
    # Riot rotates the recommendations beneath the article independently
    # of patch changes. Only the article's own sections belong to the audit.
    entries = [entry for entry in parser.entries if entry["major"]
               and entry["major"].lower() != "related articles" and entry["text"]]
    for entry in entries:
        text = entry["text"]
        if "⇒" not in text:
            continue
        # A sentence can contain multiple independent changes. Keep their
        # labels together instead of treating the first new value as a label.
        for part in re.split(r"\.\s+(?=[A-Z][^⇒]*?:)", text):
            match = re.fullmatch(r"(.+?):\s*(.*?)\s*⇒\s*(.+)", part)
            if not match:
                continue
            what, old, new = match.groups()
            if entry["parent"]:
                what = entry["parent"] + " " + what
            changes.append({"what": what, "old": old, "new": new,
                            "section": entry["section"], "update": entry["update"],
                            "major": entry["major"]})
    if not changes:
        raise ValueError("Riot patch notes contained no readable balance changes")
    return {"patch": patch, "basePatch": base_patch, "updates": parser.updates,
            "revisionSource": "explicit label" if explicit else "dated mid-patch update sequence",
            "url": PATCH_NOTES_URL.format(slug=base_patch.replace(".", "-")),
            "changes": changes, "notes": entries}


def latest_patch_slug(set_no):
    """The newest `teamfight-tactics-patch-<set>-<n>` on the news index."""
    html = fetch_bytes(PATCH_NOTES_INDEX).decode("utf-8", "ignore")
    slugs = set(re.findall(rf"teamfight-tactics-patch-({set_no}-\d+)", html))
    if not slugs:
        return None
    return max(slugs, key=lambda s: int(s.split("-")[1]))


def parse_patch_notes(html):
    return patch_notes_document(html, "0.0")["changes"]


def fetch_source(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=60) as response:
        raw = response.read()
        source = {"url": url, "lastModified": response.headers.get("Last-Modified"),
                  "sha256": hashlib.sha256(raw).hexdigest()}
    return json.loads(raw), source


def communitydragon_set(data, set_no):
    sets = [s for s in data.get("setData", []) if s.get("number") == set_no]
    if len(sets) != 1:
        raise ValueError(f"expected one CommunityDragon set {set_no}, found {len(sets)}")
    selected = sets[0]
    item_ids = set(selected.get("items", []))
    return {"set": set_no, "mutator": selected.get("mutator"),
            "champions": selected["champions"], "traits": selected["traits"],
            "items": [item for item in data.get("items", []) if item["apiName"] in item_ids]}


def distill_bin(b):
    """The few timings we need from a character bin."""
    out = {"castTime": None, "attackWindup": None, "missileSpeed": None}
    for key, v in b.items():
        if not isinstance(v, dict) or v.get("__type") != "SpellObject":
            continue
        sp = v.get("mSpell") or {}
        leaf = key.split("/")[-1]
        if leaf.endswith("Spell") or "Ability" in leaf:
            if sp.get("mCastTime") is not None:
                out["castTime"] = sp["mCastTime"]
        elif leaf.endswith("BasicAttack"):
            out["attackWindup"] = sp.get("spellCastTime")
            out["missileSpeed"] = sp.get("missileSpeed")
    return out


def _exchange_directories(left, right):
    """Atomically replace an existing archive on Linux, including on SIGKILL."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), right)


def cmd_fetch(args, *, automatic=False, prepare=None, progress=None):
    """Fetch, validate and publish; prepare can warm staged data first."""
    report = progress or (lambda **fields: None)
    set_no = args.set
    previous = load_snapshot(set_no) if automatic else None
    requested = args.patch
    if requested:
        tft_patch_key(requested)
        base_patch = re.sub(r"[a-z]$", "", requested)
    else:
        slug = latest_patch_slug(set_no)
        if not slug:
            sys.exit(f"No patch notes found for set {set_no} on the news "
                     "index — pass --patch explicitly (e.g. 18.1).")
        base_patch = slug.replace("-", ".")
    if int(base_patch.split(".")[0]) != set_no:
        raise ValueError(f"patch {base_patch} does not belong to set {set_no}")
    print(f"Fetching Riot's patch {base_patch} notes and mid-patch updates …")
    notes = patch_notes_document(fetch_bytes(PATCH_NOTES_URL.format(
        slug=base_patch.replace(".", "-"))).decode("utf-8"), base_patch)
    patch = notes["patch"]
    report(phase="fetching", targetPatch=patch, message=f"Downloading data for {patch}.")
    if automatic and tft_patch_key(patch) < tft_patch_key(previous.patch):
        from tft_update import ReviewRequired
        raise ReviewRequired(f"The source reports older patch {patch}; keeping {previous.patch} and retrying on the next check.")
    if requested and requested not in (base_patch, patch):
        raise ValueError(f"the current sources describe {patch}, not requested {requested}; "
                         "use an archived snapshot for historical patches")
    out_dir = os.path.join(set_dir(set_no), patch)
    if os.path.exists(os.path.join(out_dir, "metatft.json")) and not args.force:
        print(f"Set {set_no} patch {patch} is already archived at "
              f"{os.path.relpath(out_dir)} (use --force to refetch).")
        return
    print(f"Fetching MetaTFT lookup for set {set_no} …")
    data, lookup_source = fetch_source(METATFT_URL.format(set=set_no))
    if data.get("_metadata", {}).get("set") != f"TFTSet{set_no}":
        raise ValueError("the lookup returned the wrong TFT set")
    units = real_units(data)
    if not units:
        raise ValueError("the lookup contains no shop champions")
    print(f"  {len(units)} units, {len(data['items'])} items, "
          f"{len(data['traits'])} traits; MetaTFT stamp: "
          f"{data.get('_metadata', {}).get('patch')} generated "
          f"{data.get('_metadata', {}).get('generated')}")
    print("Fetching CommunityDragon's set export for source comparison and assets …")
    cdragon_raw, cdragon_source = fetch_source(CDRAGON_TFT)
    cdragon = communitydragon_set(cdragon_raw, set_no)
    cdragon_source["use"] = "asset references and cross-checks; ability calculations are incomplete"
    lookup_source["use"] = "stats, roles and ability/item/trait calculations, with audited corrections"
    print("Fetching character bins from Community Dragon …")
    bins = {}
    for u in units:
        for asset in u.get("assetNames") or []:
            name = asset.lower()
            try:
                bins[asset] = distill_bin(fetch_json(CDRAGON_BIN.format(name=name)))
            except HTTPError as e:
                if e.code != 404:
                    raise
                bins[asset] = {"error": str(e)[:80]}
    ok = sum(1 for b in bins.values() if b.get("castTime") is not None)
    print(f"  {len(bins)} bins, {ok} with a cast time")
    meta = {"set": set_no, "patch": patch,
            "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metatft": data.get("_metadata"),
            "sources": {"metatft": lookup_source, "communitydragon": cdragon_source,
                        "bins": CDRAGON_BIN, "patchNotes": notes["url"]},
            "patchNotesUpdates": notes["updates"]}
    # Download and check in a staging directory. A failed fetch/check must
    # never replace the snapshot that the dashboard is currently using.
    os.makedirs(set_dir(set_no), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fetch-", dir=set_dir(set_no)) as staging:
        for name, content in (("metatft.json", data), ("communitydragon.json", cdragon),
                              ("bins.json", bins), ("patchnotes.json", notes), ("meta.json", meta)):
            with open(os.path.join(staging, name), "w") as f:
                json.dump(content, f, separators=(",", ":"), sort_keys=True)
        # Corrections and the review are patch-specific. Never copy them
        # automatically to another patch/hotfix and call it verified.
        for name in ("overrides.json", "audit.json"):
            source = os.path.join(out_dir, name)
            if os.path.exists(source):
                shutil.copyfile(source, os.path.join(staging, name))
            elif automatic and name == "overrides.json":
                source = os.path.join(previous.dir, name)
                if os.path.exists(source):
                    # Used only to evaluate the candidate. Reconciliation
                    # must validate every carried correction before publication.
                    shutil.copyfile(source, os.path.join(staging, name))
        report(phase="validating", message=f"Checking {patch} against the active snapshot and patch notes.")
        candidate = Snapshot(set_no, patch, directory=staging)
        findings, unmatched = check_patch_notes(candidate)
        needs_review = [f for f in findings if f["status"] != "current"]
        if automatic and (not candidate.audit or needs_review):
            from tft_update import reconcile
            try:
                overrides, audit = reconcile(candidate, previous, notes)
                audit.setdefault("baselineReviewedAt", previous.audit.get("checkedAt"))
                audit["checkedAt"] = meta["fetchedAt"]
                for name, content in (("overrides.json", overrides), ("audit.json", audit)):
                    with open(os.path.join(staging, name), "w") as f:
                        json.dump(content, f, indent=2)
                candidate = Snapshot(set_no, patch, directory=staging)
                findings, unmatched = check_patch_notes(candidate)
                needs_review = [f for f in findings if f["status"] != "current"]
            except Exception:
                pending = os.path.join(set_dir(set_no), ".pending", patch)
                shutil.copytree(staging, pending, dirs_exist_ok=True)
                raise
        if not candidate.audit or needs_review:
            pending = os.path.join(set_dir(set_no), ".pending", patch)
            shutil.copytree(staging, pending, dirs_exist_ok=True)
            raise ValueError(f"{patch} needs a patch audit before it can be published; "
                             f"add/update {out_dir}/audit.json and overrides.json. "
                             f"Downloaded candidate: {pending}. The previous snapshot is unchanged.")
        if len(candidate.units) != len(units) or len(modeled_units(candidate)) != len(units):
            raise ValueError("The new snapshot has missing champion stats or unsupported champions; keeping current builds.")
        if prepare:
            try:
                prepare(candidate)
            except BaseException:
                pending = os.path.join(set_dir(set_no), ".pending", patch)
                shutil.copytree(staging, pending, dirs_exist_ok=True)
                raise
        meta["verifiedAt"] = meta["fetchedAt"]
        meta["verification"] = "published patch-note checks passed; see audit.json for remaining source limitations"
        with open(os.path.join(staging, "meta.json"), "w") as f:
            json.dump(meta, f, indent=1)
        if os.path.exists(out_dir):
            # Preserve extra archive files (for example review notes), then
            # swap the whole directory. An interruption cannot leave a
            # partly published archive. TemporaryDirectory removes the old
            # generation after the exchange.
            for name in os.listdir(out_dir):
                source, dest = os.path.join(out_dir, name), os.path.join(staging, name)
                if not os.path.exists(dest):
                    if os.path.isdir(source):
                        shutil.copytree(source, dest)
                    else:
                        shutil.copy2(source, dest)
            shutil.copymode(out_dir, staging)
            _exchange_directories(staging, out_dir)
        else:
            os.chmod(staging, 0o755)
            os.rename(staging, out_dir)
    print(f"Archived under {os.path.relpath(out_dir)}/")
    _SNAP.pop((set_no, patch), None)
    return load_snapshot(set_no, patch)


def real_units(data):
    """The set's shop champions. The file flags them (`shopUnit`); it also
    lists summons, transformed forms, props and monsters, some of which
    carry a cost and traits, so the flag is the only reliable test. An
    older file without the flag falls back to costed-with-traits."""
    units = data["units"]
    if any("shopUnit" in u for u in units):
        return [u for u in units if u.get("shopUnit")]
    return [u for u in units
            if u.get("traits") and u.get("cost") in (1, 2, 3, 4, 5)
            and "hp" in (u.get("stats") or {})]


def role_kind(tags):
    """The team-role half of a role: Assassin, Fighter, Marksman, Caster,
    Tank or Specialist."""
    for kind in ("Assassin", "Fighter", "Marksman", "Caster", "Tank", "Specialist"):
        if f"Role.{kind}" in tags:
            return kind
    return "Specialist"


def curve_at(curve, star):
    """A curve table row's value at a star level. Rows list breakpoints as
    [star, value]; a missing star holds the previous breakpoint's value
    (the resolved per-star coefficients in the data show it: a row
    [[1, .3], [3, .5]] resolves to .3, .3, .5). Below the first breakpoint
    the first value applies."""
    val = None
    for s, v in curve:
        if s <= star:
            val = v
        else:
            break
    if val is None:
        val = curve[0][1]
    return val


def override_curve(row, vals):
    """A curve row with hand values applied: one value makes the row that
    constant at every star; several replace the first stars in order and
    leave the higher ones as they were."""
    if len(vals) == 1:
        return [[1, vals[0]], [4, vals[0]]]
    have = [curve_at(row, s) for s in range(1, 5)] if row else [vals[0]] * 4
    for i, v in enumerate(vals[:4]):
        have[i] = v
    return [[s, v] for s, v in zip(range(1, 5), have)]


def override_trait_curve(row, vals):
    """A trait row with hand values: `vals` maps breakpoint columns (as
    strings, column 1 being the first breakpoint; column 0 is the inactive
    value) to new values; other columns keep what they were."""
    have = {int(c): v for c, v in (row or [])}
    for c, v in vals.items():
        have[int(c)] = v
    return [[c, have[c]] for c in sorted(have)]


class Snapshot:
    """One archived patch of a set, with hand overrides applied."""

    def __init__(self, set_no, patch, directory=None):
        self.set_no, self.patch = set_no, patch
        self.dir = directory or os.path.join(set_dir(set_no), patch)
        with open(os.path.join(self.dir, "metatft.json")) as f:
            self.raw = json.load(f)
        with open(os.path.join(self.dir, "meta.json")) as f:
            self.meta = json.load(f)
        audit_path = os.path.join(self.dir, "audit.json")
        self.audit = None
        if os.path.exists(audit_path):
            with open(audit_path) as f:
                self.audit = json.load(f)
        cdragon_path = os.path.join(self.dir, "communitydragon.json")
        self.communitydragon = {}
        if os.path.exists(cdragon_path):
            with open(cdragon_path) as f:
                self.communitydragon = json.load(f)
        bins_path = os.path.join(self.dir, "bins.json")
        self.bins = {}
        if os.path.exists(bins_path):
            with open(bins_path) as f:
                self.bins = json.load(f)
        self.overrides = {"units": {}, "items": {}, "traits": {}}
        opath = os.path.join(self.dir, "overrides.json")
        if os.path.exists(opath):
            with open(opath) as f:
                o = json.load(f)
            for k in self.overrides:
                self.overrides[k] = o.get(k) or {}
        self.roles = self.raw.get("roleData") or {}
        self.role_names = self.raw.get("roles") or {}
        self.units = {}
        shop = real_units(self.raw)
        for u in shop:
            unit = self._unit(u)
            if unit["stats"]["hp"] is None:
                print(f"Warning: {u['apiName']} has no health in the data or in "
                      f"overrides.json (\"stats\": {{\"hp\": …}}); skipped", file=sys.stderr)
                continue
            self.units[u["apiName"]] = unit
        # the rest of the file: summons, transformed forms, monsters — kept
        # for drivers that need a summon's numbers
        shop_apis = {u["apiName"] for u in shop}
        self.extras = {u["apiName"]: self._unit(u) for u in self.raw["units"]
                       if u["apiName"] not in shop_apis}
        for api in self.overrides["units"]:
            if api not in self.units and api not in self.extras:
                print(f"Warning: overrides.json names unknown unit {api}", file=sys.stderr)
        self.units_by_name = {u["name"]: u for u in self.units.values()}
        self.items = {i["apiName"]: self._item(i) for i in self.raw["items"]}
        self.traits = {t["apiName"]: self._trait(t) for t in self.raw["traits"]}
        self.traits_by_name = {t["name"]: t for t in self.traits.values()}
        # A running dashboard keeps this loaded generation until reload.
        # Re-reading replaced files here would pair old stats with new caches.
        self._input_hash = json_hash([self.raw, self.bins, self.overrides])

    def _stats(self, s, curve, ov):
        def stat(key, default):
            v = s.get(key)
            return default if v is None else v
        ad = s.get("damage")
        if ad is None:  # a few units only carry their attack damage as a curve row
            for row in ("AutoAttackDamage", "BasicAttackDamage", "AutoAttackDamageSmall"):
                if row in curve:
                    ad = curve_at(curve[row], 1)
                    break
        stats = {"hp": s.get("hp"), "ad": 0.0 if ad is None else ad,
                 "as": stat("attackSpeed", 0.7),
                 "armor": stat("armor", 0.0), "mr": stat("magicResist", 0.0),
                 "mana": stat("mana", 0.0), "initialMana": stat("initialMana", 0.0),
                 "range": stat("range", 1), "critChance": stat("critChance", 0.25),
                 "critMult": stat("critMultiplier", 1.4)}
        stats.update(ov.get("stats") or {})
        return stats

    def _curve(self, u, ov):
        # curveValues carries rows the tooltip references that curveTable
        # lacks (Elise's spider rows, Kog'Maw's threshold); curveTable wins
        curve = dict(u.get("curveValues") or {})
        curve.update(u.get("curveTable") or {})
        for row, vals in (ov.get("curve") or {}).items():
            curve[row] = override_curve(curve.get(row), vals)
        return curve

    def _calcs(self, calcs, curve, ov):
        """The ability's calculations, with every overridden curve row
        written into the terms that carry that row: the data resolves each
        scaled term's per-star coefficient list from its row (they are
        identical for every term in the set), and the engine reads the
        list, so a hand correction has to reach both or it changes the
        tooltip check and not the damage."""
        rows = set(ov.get("curve") or {})
        out = {}
        for name, calc in (calcs or {}).items():
            terms = []
            for term in calc.get("terms") or []:
                row = term.get("row")
                if row in rows and row in curve and term.get("coefficient") is not None:
                    term = dict(term, coefficient=[curve_at(curve[row], s) for s in range(1, 5)])
                terms.append(term)
            out[name] = dict(calc, terms=terms)
        return out

    def _unit(self, u):
        s = u.get("stats") or {}
        ov = self.overrides["units"].get(u["apiName"]) or {}
        curve = self._curve(u, ov)
        calcs = self._calcs((u.get("ability") or {}).get("attributeCalcs"), curve, ov)
        stats = self._stats(s, curve, ov)
        assets = u.get("assetNames") or []
        b = next((self.bins[a] for a in assets if a in self.bins
                  and self.bins[a].get("castTime") is not None), {})
        # Riot's role: the unit's own tag list is inconsistent (Akali lacks
        # Role.Attack, Rengar's tags say Assassin while his role says
        # Fighter), so the role's own definition is the source of truth
        role = u.get("role")
        rd = self.roles.get(role) or {}
        tags = list(rd.get("roleTags") or u.get("roleTags") or [])
        kind = role_kind(tags)
        rec = u.get("recommendedItems") or ""
        rec_kind = None
        m = re.match(r"DA_RecommendedItemsTable_(?:Attack|Magic|Hybrid)(\w+)$", rec)
        if m and m.group(1) in OBJECTIVE_BY_KIND:
            rec_kind = m.group(1)
        # Adaptor forms: the AD and AP kits (Lux's extra abilities are her
        # Avatar trait variants, not forms)
        forms = {}
        for key, v in (u.get("extraAbilities") or {}).items():
            if v.get("variant") not in ("AD", "AP"):
                continue
            fov = (ov.get("forms") or {}).get(v["variant"]) or {}
            fcurve = self._curve(v, fov)
            forms[v["variant"]] = {
                "name": v.get("name"), "desc": v.get("desc"),
                "calcs": self._calcs(v.get("attributeCalcs"), fcurve, fov), "curve": fcurve,
                "stats": self._stats(v["stats"], fcurve, fov) if v.get("stats") else None,
            }
        return {
            "api": u["apiName"], "name": u["name"], "cost": u["cost"],
            "role": role, "roleName": rd.get("name") or self.role_names.get(role) or role,
            "roleTags": tags, "kind": kind, "attack": "Role.Attack" in tags,
            "resource": (rd.get("resourceType") or "Mana").split(".")[-1],
            "itemKind": rec_kind or kind,
            "objective": OBJECTIVE_BY_KIND[rec_kind or kind],
            "traits": list(u.get("traits") or []),
            "traitApis": list(u.get("traitApiNames") or []),
            "stats": stats, "curve": curve, "calcs": calcs, "forms": forms,
            "ability": {"name": (u.get("ability") or {}).get("name"),
                        "desc": (u.get("ability") or {}).get("desc")},
            "castTime": ov.get("castTime", b.get("castTime")),
            "assets": assets,
        }

    def _item(self, i):
        curve = dict(i.get("curveTable") or {})
        ov = self.overrides["items"].get(i["apiName"]) or {}
        for row, v in (ov.get("curve") or {}).items():
            curve[row] = [[1, v]]
        return {"api": i["apiName"], "name": i["name"],
                "composition": i.get("composition") or [],
                "unique": bool(i.get("unique")), "curve": curve,
                "statLine": i.get("statLine") or "", "desc": i.get("desc") or "",
                "tags": i.get("tags") or []}

    def _trait(self, t):
        curve = dict(t.get("curveTable") or {})
        ov = self.overrides["traits"].get(t["apiName"]) or {}
        for row, vals in (ov.get("curve") or {}).items():
            curve[row] = override_trait_curve(curve.get(row), vals)
        levels = [e["minUnits"] for e in t.get("effects") or []]
        styles = [e.get("style", 0) for e in t.get("effects") or []]
        return {"api": t["apiName"], "name": t["name"], "type": t.get("type"),
                "desc": t.get("desc") or "", "curve": curve, "levels": levels,
                "styles": styles,
                "units": [x["unit"] for x in t.get("units") or []]}

    def unit(self, key):
        """A unit by api name or display name (case-insensitive)."""
        if key in self.units:
            return self.units[key]
        low = key.lower()
        for u in self.units.values():
            if u["name"].lower() == low or u["api"].lower() == low \
                    or u["api"].lower().split("_", 1)[-1] == low:
                return u
        raise KeyError(f"no unit {key!r} in set {self.set_no} patch {self.patch}")

    def item(self, key):
        if key in self.items:
            return self.items[key]
        low = key.lower().replace("'", "").replace(" ", "")
        for it in self.items.values():
            if it["name"].lower().replace("'", "").replace(" ", "") == low \
                    or it["api"].lower() == low:
                return it
        raise KeyError(f"no item {key!r}")

    def hash_inputs(self):
        """The simulation inputs captured when this snapshot was loaded."""
        return self._input_hash


_SNAP = {}


def load_snapshot(set_no=None, patch=None):
    set_no = set_no or DEFAULT_SET
    patches = patch_dirs(set_no)
    if not patches:
        sys.exit(f"No TFT snapshot for set {set_no} — run `lol.py tft fetch` first.")
    if patch is None:
        patch = patches[-1]
    elif patch not in patches:
        sys.exit(f"No snapshot for set {set_no} patch {patch}; have {', '.join(patches)}")
    key = (set_no, patch)
    if key not in _SNAP:
        _SNAP[key] = Snapshot(set_no, patch)
    return _SNAP[key]


# ---------------------------------------------------------------------------
# hand-curated effect tables (numbers stay in the data, referenced by row)
# ---------------------------------------------------------------------------

def _hand_file(set_no, name):
    path = os.path.join(set_dir(set_no), name)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def load_item_effects(set_no):
    return _hand_file(set_no, "item-effects.json")


def load_trait_effects(set_no):
    return _hand_file(set_no, "trait-effects.json")


def load_kits(set_no):
    return _hand_file(set_no, "kits.json")


def row_value(curve, spec, star=1):
    """Resolve a hand-file reference: a number, or {"row": name, "scale":
    k, "minusOne": bool, "mult": m, "col": breakpoint index}."""
    if isinstance(spec, (int, float)):
        return float(spec)
    row = curve.get(spec["row"])
    if row is None:
        raise KeyError(f"row {spec['row']!r} missing from curve table")
    v = curve_at(row, spec.get("col", star))
    if spec.get("minusOne"):
        v -= 1.0
    v *= spec.get("scale", 1.0)
    v *= spec.get("mult", 1.0)
    if "min" in spec:   # a multiplier row that is 0 below its breakpoint
        v = max(v, spec["min"])
    return v


# stat line icons -> engine stat keys, with the value convention per format
_ICON_STAT = {"ad": "adPct", "ap": "ap", "as": "asPct", "critchance": "crit",
              "damageamp": "amp", "health": "hp", "armor": "armor",
              "mr": "mr", "manaregen": "manaRegen", "omnivamp": "omnivamp",
              "dura": "durability"}


def parse_stat_line(item):
    """An item's stat line, as {stat: value} in engine units: adPct and
    crit and amp as fractions, ap as flat ability power, asPct as a
    fraction of base attack speed, hp/armor/mr/manaRegen flat. The data
    writes each row in whichever convention its tooltip formatter expects,
    so the format attribute decides how to read it."""
    stats = {}
    for m in re.finditer(r"<TFT(?:CurveTable|Attribute)\s+([^>]*)/>", item["statLine"]):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        if attrs.get("type") != "stat":
            continue
        row = attrs.get("row") or attrs.get("fallbackRow")
        icon = (attrs.get("icon") or "").split(".")[-1].lower()
        key = _ICON_STAT.get(icon)
        if not key or row not in item["curve"]:
            continue
        v = curve_at(item["curve"][row], 1)
        fmt = (attrs.get("format") or "").lower()   # the data spells it three ways
        if key == "adPct":
            v = v if fmt == "percent" else v / 100.0
        elif key == "ap":
            v = v * 100.0 if fmt == "percent" else v
        elif key == "asPct":
            v = v - 1.0 if fmt == "percentminusone" else (v if fmt == "percent" else v / 100.0)
        elif key in ("crit", "amp", "omnivamp"):
            v = v if fmt == "percent" else v / 100.0
        elif key == "durability":
            # "invertedPercent" rows hold the damage multiplier (0.92 = 8%)
            v = 1.0 - v if fmt == "invertedpercent" else (v if fmt == "percent" else v / 100.0)
        stats[key] = stats.get(key, 0.0) + v
    return stats


# ---------------------------------------------------------------------------
# the fight: sheet + effects + engine
# ---------------------------------------------------------------------------

def unit_role_kind(unit):
    return unit.get("kind") or role_kind(unit.get("roleTags") or [])


def unit_trait_contexts(snap, unit, trait_fx):
    """{context: [(trait api, breakpoint column)]} for the unit's own
    traits that have a modeled effect. Unmodeled traits are reported."""
    modeled, unmodeled = [], []
    for api in unit["traitApis"]:
        t = snap.traits.get(api)
        if t is None:
            continue
        if api in trait_fx and t["levels"]:
            modeled.append(api)
        else:
            unmodeled.append(t["name"])
    out = {"bare": [], "low": [], "high": []}
    for api in modeled:
        t = snap.traits[api]
        # "high" stops short of the prismatic chase tiers (style 5+), which
        # need emblems or a whole board of the trait
        reachable = [i for i, st in enumerate(t["styles"], 1) if st < PRISMATIC_STYLE]
        out["low"].append((api, 1))
        out["high"].append((api, max(reachable) if reachable else len(t["levels"])))
    return out, unmodeled


def calc_type(name):
    n = name.split(".")[-1]
    if n.startswith("Physical"):
        return "physical"
    if n.startswith("Magic"):
        return "magic"
    if n.startswith("True"):
        return "true"
    return "magic"




_WARNED_SCALINGS = set()


def _warn_scaling(unit_name, calc_name, scaling):
    """A scaling the engine does not know contributes nothing; say so once
    rather than fold a silent zero into a fight."""
    key = (unit_name, calc_name, scaling)
    if key not in _WARNED_SCALINGS:
        _WARNED_SCALINGS.add(key)
        print(f"Warning: {unit_name} {calc_name.split('.')[-1]} scales with {scaling!r}, "
              f"which the engine does not model (read as 0)", file=sys.stderr)


def unit_primary_damage(unit, star):
    """The biggest damage number on the unit's ability card at `star`, as
    the data resolves it (100 ability power, first-star base stats)."""
    best = 0.0
    for name, calc in unit["calcs"].items():
        if "Damage" not in name.split(".")[-1]:
            continue
        vals = calc.get("values") or []
        if len(vals) >= star and vals[star - 1] is not None:
            best = max(best, float(vals[star - 1]))
    return best


def tank_debuffs(snap):
    """Continuous enemy debuffs at the corrected snapshot's item values."""
    return {name: curve_at(snap.items[api]["curve"][row], 1) * 0.01
            for name, (api, row) in TANK_DEBUFF_ROWS.items()}


@lru_cache(maxsize=32)
def _reference_carry_dps(spec_json):
    """Cache by the complete resolved inputs, so snapshot/model edits reprice it."""
    spec = json.loads(spec_json)
    _, result = engine().simulate(spec, False)
    return result["total"] / spec["duration"]


def tank_threats(snap, star=DUMMY_STAR):
    """Equal-budget profiles: median frontline damage plus two itemized carries.

    Reference fights measure pre-mitigation damage against one immortal tank
    with zero resists, no traits, and a fixed 20-second window. We retain the
    synthetic backline cadence/split to compare tank builds consistently.
    """
    references = []
    for name, items in TANK_REFERENCE_CARRIES:
        unit = snap.unit(name)
        dummy = {"slots": [{"hp": FRONT_TANK_DEFENSES["hp"], "armor": 0.0,
                             "mr": 0.0, "kind": "tank"}]}
        spec = cell_spec(snap, unit, 2, "spread", [], dummy,
                         duration=TANK_REFERENCE_DURATION, pressure=False, items=items)
        spec["immortal"] = True
        references.append({"name": unit["name"], "api": unit["api"], "star": 2,
                           "itemApis": list(items), "items": [snap.items[api]["name"] for api in items],
                           "duration": TANK_REFERENCE_DURATION,
                           "dps": _reference_carry_dps(json.dumps(spec, sort_keys=True))})
    dummy = dummies_for(snap, star=star)
    tank = dummy["tank"]
    mana_rate = tank["manaPerAttack"] * tank["as"]
    cast_interval = tank["manaMax"] / mana_rate + MANA_LOCK_S
    front_attacks = TANK_FRONTLINERS * tank["ad"] * dummy["critEv"] * tank["as"]
    front_spells = TANK_FRONTLINERS * tank["ability"] / cast_interval
    frontline_dps = front_attacks + front_spells
    backline_dps = sum(reference["dps"] for reference in references)
    total_dps = frontline_dps + backline_dps
    profiles = []
    for key, profile in TANK_THREATS.items():
        back_attacks = backline_dps * profile["attackShare"]
        back_spells = backline_dps - back_attacks
        physical_spells = front_spells * tank["physicalShare"] + back_spells * profile["spellPhysicalShare"]
        profiles.append(dict(profile, key=key,
                             attackers=TANK_FRONTLINERS + TANK_BACKLINERS,
                             frontlineAttackers=TANK_FRONTLINERS, backlineAttackers=TANK_BACKLINERS,
                             dps=total_dps, frontlineDps=frontline_dps, backlineDps=backline_dps,
                             frontlineAttackInterval=1.0 / tank["as"], frontlineCastInterval=cast_interval,
                             backlineAttackShare=profile["attackShare"],
                             backlineSpellPhysicalShare=profile["spellPhysicalShare"],
                             attackShare=(front_attacks + back_attacks) / total_dps,
                             spellPhysicalShare=physical_spells / (front_spells + back_spells),
                             physicalShare=(front_attacks + back_attacks + physical_spells) / total_dps,
                             referenceCarries=references))
    return profiles


def dummies_for(snap, n=N_DUMMIES, star=DUMMY_STAR, threat=None):
    """A heavy first tank, median tanks after it, then a median non-tank.
    The first tank uses fixed benchmark health/armor/MR; other defenses
    come from the set's units at `star`. Tank-only effects (Giant Slayer's
    amp) apply to the frontline, not to everything. Each carries its group's median
    offense too, for the fights where the dummies hit back: attack damage
    and speed, the ability's biggest number on its mana cadence, split
    physical/magic by the group's share of Attack-type roles. A tank
    `threat` uses three frontline targets and two protected backline sources
    calibrated against itemized carry damage. `n` controls legacy fights."""
    if threat is not None and threat not in TANK_THREATS:
        raise ValueError(f"unknown tank threat {threat!r}")
    if n < 2:
        raise ValueError("the benchmark requires at least two dummy slots")
    tanks = [u for u in snap.units.values() if u["kind"] == "Tank"]
    others = [u for u in snap.units.values() if u["kind"] != "Tank"]
    if not tanks or not others:
        raise SystemExit("the snapshot has no tank-role or non-tank units")

    def median_of(units, kind):
        med = lambda xs: statistics.median(list(xs))
        casters = [u for u in units if u["stats"]["mana"] > 0 and ROLE_MANA.get(u["kind"], 0) > 0]
        return {"kind": kind,
                "hp": round(med(u["stats"]["hp"] * HP_PER_STAR ** (star - 1) for u in units)),
                "armor": med(u["stats"]["armor"] for u in units),
                "mr": med(u["stats"]["mr"] for u in units),
                "ad": round(med(u["stats"]["ad"] * AD_PER_STAR ** (star - 1) for u in units), 1),
                "as": med(u["stats"]["as"] for u in units),
                "ability": round(med(unit_primary_damage(u, star) for u in units)),
                "physicalShare": round(sum(1 for u in units if u["attack"]) / len(units), 2),
                "manaMax": med(u["stats"]["mana"] for u in casters),
                "manaStart": med(u["stats"]["initialMana"] for u in casters),
                "manaPerAttack": med(ROLE_MANA[u["kind"]] for u in casters),
                "manaFromDamage": kind == "tank"}
    tank, other = median_of(tanks, "tank"), median_of(others, "non-tank")
    slots = [dict(tank) for _ in range(n - 1)] + [dict(other)]
    if n > 1:
        slots[0].update(FRONT_TANK_DEFENSES)
        slots[0]["fixedDefenses"] = True
    crit_ev = 1.0 + 0.25 * 0.4    # every unit's base crit
    # a tank is hit by the whole enemy board: BOARD_SIZE units split by the
    # set's tank share, the tanks over the tank slots, the rest behind
    n_tanks = round(BOARD_SIZE * len(tanks) / (len(tanks) + len(others)))
    board = [0] * n
    for i in range(n_tanks):
        board[i % (n - 1)] += 1
    board[-1] = BOARD_SIZE - n_tanks

    def dps(streams):   # pre-mitigation damage per second, casting on attacks alone
        out = 0.0
        for s, k in zip(slots, streams):
            rate = s["manaPerAttack"] * s["as"]
            out += k * (s["ad"] * crit_ev * s["as"]
                        + (s["ability"] / (s["manaMax"] / rate + MANA_LOCK_S) if rate else 0.0))
        return round(out)
    out = {"count": n, "star": star, "slots": slots, "tank": tank, "other": other,
           "tanks": len(tanks), "others": len(others),
           "totalHp": sum(s["hp"] for s in slots),
           "critEv": crit_ev, "pressureDps": dps([1] * n),
           "board": board, "boardSize": BOARD_SIZE, "boardPressureDps": dps(board)}
    if threat is not None:
        profile = next(p for p in tank_threats(snap, star) if p["key"] == threat)
        front = [dict(tank, nearby=True, line="frontline", label=f"Frontliner {i + 1}")
                 for i in range(TANK_FRONTLINERS)]
        front[0].update(FRONT_TANK_DEFENSES, fixedDefenses=True)
        for i, slot in enumerate(front):
            slot.update({
                "attackStart": (i + 1) * profile["frontlineAttackInterval"] / TANK_FRONTLINERS,
                "castInterval": profile["frontlineCastInterval"],
                "castStart": (slot["manaMax"] - slot["manaStart"]) / (slot["manaPerAttack"] * slot["as"])
                             + i * profile["frontlineAttackInterval"] / TANK_FRONTLINERS,
                "manaStart": 0.0, "manaPerAttack": 0.0, "manaFromDamage": False,
            })
        back = [dict(other, nearby=False, line="backline", label=f"Backline carry {i + 1}")
                for i in range(TANK_BACKLINERS)]
        per_attacker = profile["backlineDps"] / TANK_BACKLINERS
        for i, slot in enumerate(back):
            slot.update({
                "ad": per_attacker * profile["backlineAttackShare"] * profile["attackInterval"] / crit_ev,
                "as": 1.0 / profile["attackInterval"],
                "attackStart": (i + 1) * profile["attackInterval"] / TANK_BACKLINERS,
                "ability": per_attacker * (1.0 - profile["backlineAttackShare"]) * profile["castInterval"],
                "physicalShare": profile["backlineSpellPhysicalShare"],
                "castInterval": profile["castInterval"],
                "castStart": profile["castInterval"] if profile["burst"]
                             else (i + 1) * profile["castInterval"] / TANK_BACKLINERS,
                "manaStart": 0.0, "manaPerAttack": 0.0, "manaFromDamage": False,
            })
        slots = front + back
        out.update(threat=profile, enemyDebuffs=tank_debuffs(snap), slots=slots,
                   count=len(slots), totalHp=sum(slot["hp"] for slot in slots),
                   board=[1] * len(slots), boardSize=len(slots),
                   pressureDps=profile["dps"], boardPressureDps=profile["dps"])
    return out


# ---------------------------------------------------------------------------
# the compiled engine: specs that hand Rust every number resolved
# ---------------------------------------------------------------------------

TFT_ENGINE_DIR = os.path.join(BASE_DIR, "tft_engine")

try:
    import lol_tft as _lol_tft
except ImportError:   # fetch, check and status work without it; a fight does not
    _lol_tft = None


def engine():
    """The compiled engine (tft_engine/, imported as lol_tft from the repo
    root); a missing build says how to make one."""
    if _lol_tft is None:
        raise ImportError("the TFT engine is not built (lol_tft.abi3.so at the repo root): "
                          "run jobs/build-engine.sh tft — it uses cargo, or fetches one "
                          "through nix-shell")
    return _lol_tft


def engine_source_hash():
    """The hash tft_engine/build.rs stamps into the module, recomputed from
    the sources on disk: Cargo.toml and every file under src/, sorted,
    each as its path, a NUL, its length and its bytes."""
    root = TFT_ENGINE_DIR
    files = [os.path.join(root, "Cargo.toml")]
    for d, _, names in os.walk(os.path.join(root, "src")):
        files += [os.path.join(d, n) for n in names]
    h = hashlib.sha256()
    for path in sorted(files):
        with open(path, "rb") as f:
            data = f.read()
        h.update(os.path.relpath(path, root).encode())
        h.update(b"\0")
        h.update(len(data).to_bytes(8, "little"))
        h.update(data)
    return h.hexdigest()


def _code_hash(py_bytes, engine_hash):
    return hashlib.sha256(py_bytes + engine_hash.encode()).hexdigest()


# The code is part of every cache key: a result is only valid for the code
# that produced it — this module's source and the engine's (lol_tft bakes a
# hash of tft_engine/src at build time). Hashed once at import, so a serve
# that keeps running after an edit still recognises the results matching
# the code it runs — and can report itself stale (source_stale).
with open(os.path.abspath(__file__), "rb") as _f:
    SOURCE_HASH = _code_hash(_f.read(), _lol_tft.SOURCE_HASH if _lol_tft else engine_source_hash())


def source_stale():
    """Whether the code on disk differs from what this process runs: tft.py
    edited, or tft_engine/ edited without a rebuild (jobs/build-engine.sh)."""
    with open(os.path.abspath(__file__), "rb") as f:
        return _code_hash(f.read(), engine_source_hash()) != SOURCE_HASH


_KNOWN_SCALINGS = ("AttackDamage", "AbilityPower", "HealthMax", "Armor", "MagicResist",
                   "BasicAttackDamage", "Stack")


def kit_spec(unit, star, form=None):
    """A unit's kit for the engine at one star level and form (the merge
    tft.Sheet did): the stats with health and attack damage already
    star-scaled, every curve row at the star, and each calc's terms with
    the star's coefficient — so the engine folds calcs exactly as
    calc_value did without touching a curve or a power."""
    calcs, curve, s = unit["calcs"], unit["curve"], unit.get("stats") or {}
    if form is not None and form in (unit.get("forms") or {}):
        fm = unit["forms"][form]
        calcs = {**calcs, **fm["calcs"]}
        curve = {**curve, **fm["curve"]}
        if fm["stats"]:
            s = {**s, **{k: v for k, v in fm["stats"].items() if v is not None}}
    base_ad = s.get("ad") or 0.0
    row = {"AD": "AutoAttackDamage", "AP": "AutoAttackDamageAP"}.get(form)
    if row and row in curve:
        base_ad = curve_at(curve[row], 1)
    base_ad = base_ad * AD_PER_STAR ** (star - 1)
    hp_star = (s.get("hp") or 0.0) * HP_PER_STAR ** (star - 1)
    rows = {}
    for name, r in curve.items():
        v = curve_at(r, star) if r else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            rows[name] = float(v)
    out_calcs = {}
    for full, calc in calcs.items():
        short = full.split(".")[-1]
        terms = []
        for term in calc.get("terms") or []:
            ttype = term.get("type")
            op = term.get("op")
            if ttype == "runtime":
                terms.append({"type": "runtime", "row": term.get("row") or "runtime", "op": op})
            elif ttype == "flat":
                r = term.get("row")
                if r in curve:
                    v = curve_at(curve[r], star)
                else:
                    vals = term.get("coefficient") or term.get("values") or [0.0]
                    v = float(vals[min(star, len(vals)) - 1] or 0.0)
                terms.append({"type": "flat", "value": float(v), "op": op})
            else:
                coefs = term.get("coefficient")
                if coefs is None and term.get("row") in curve:
                    coef = curve_at(curve[term["row"]], star)
                elif coefs is None:
                    coef = 0.0
                else:
                    coef = coefs[min(star, len(coefs)) - 1]
                    if coef is None:
                        coef = 0.0
                scaling = term.get("scaling")
                if not (scaling is None or scaling in _KNOWN_SCALINGS
                        or scaling.endswith(("Calc1", "Calc2", "Calc3", "Calc4"))):
                    _warn_scaling(unit["name"], full, scaling)
                    scaling = "unknown"
                pre = term.get("preAdd")
                if pre is not None:
                    pre = float(pre[min(star, len(pre)) - 1] or 0.0)
                terms.append({"type": "scaled", "coef": float(coef), "scaling": scaling,
                              "preAdd": pre, "op": op})
        out_calcs[short] = {"dtype": calc_type(full), "terms": terms}
    stats = {k: s.get(k) for k in ("hp", "ad", "as", "armor", "mr", "mana", "initialMana",
                                   "range", "critChance", "critMult")}
    return {"stats": stats, "baseAd": base_ad, "hpStar": hp_star, "rows": rows, "calcs": out_calcs}


def item_spec(snap, api, item_fx, unit):
    """One item for the engine: its stat line as (key, value) pairs in the
    line's order and its modeled passive as plain numbers, with the range
    and role gates of apply_item already applied for `unit`."""
    item = snap.items[api]
    spec = (item_fx.get("items") or {}).get(api) or {}
    c = item["curve"]

    def rv(s):
        if isinstance(s, list):
            return sum(rv(x) for x in s)
        return row_value(c, s)
    out = {"api": api, "name": item["name"], "unique": bool(item["unique"]),
           "stats": [[k, v] for k, v in parse_stat_line(item).items()]}
    if spec.get("precision"):
        out["precision"] = int(spec["precision"])
    if "ampVsTank" in spec:
        out["ampVsTank"] = rv(spec["ampVsTank"])
    if "asPerSecond" in spec:
        out["asPerSecond"] = [rv(spec["asPerSecond"]),
                              rv(spec["asStackDuration"]) if "asStackDuration" in spec else None]
    if "adPerAttack" in spec:
        out["adPerAttack"] = [rv(spec["adPerAttack"]), rv(spec["maxStacks"]),
                              rv(spec["asAtMax"]) if "asAtMax" in spec else 0.0]
    if "adapPerAttack" in spec:
        out["adapPerAttack"] = [rv(spec["adapPerAttack"]), rv(spec["maxStacks"]),
                                rv(spec["ampAtMax"]) if "ampAtMax" in spec else 0.0]
    if "apPerInterval" in spec:
        out["apPerInterval"] = [rv(spec["apPerInterval"]), rv(spec["interval"])]
    if "apAfter" in spec:
        out["apAfter"] = [rv(spec["apAfter"]), rv(spec["at"])]
    if "ampPerCrit" in spec:
        out["ampPerCrit"] = [rv(spec["ampPerCrit"]), rv(spec["ampDuration"]), rv(spec["maxStacks"])]
    for k in ("manaPerAttack", "manaPerCrit", "manaMult", "adapMult", "startingMana"):
        if k in spec:
            out[k] = rv(spec[k])
    adds = []
    for k in ("adPct", "ap", "asPct", "crit", "amp", "armor", "mr", "manaRegen"):
        if k in spec:
            adds.append([k, rv(spec[k])])
    if "byRole" in spec:
        kind = unit_role_kind(unit)
        branch = spec["byRole"]["tankOrFighter" if kind in ("Tank", "Fighter") else "other"]
        for k, s in branch.items():
            adds.append([k, rv(s)])
    out["adds"] = adds
    for k in ("sunderOnHit", "shredOnHit", "burnOnHit"):
        if k in spec:
            out[k] = [rv(spec[k]["pct"]), rv(spec[k]["duration"])]
    rng = unit["stats"]["range"]
    if "sunderAura" in spec and rng <= rv(spec["sunderAura"]["hexes"]):
        out["sunderAura"] = rv(spec["sunderAura"]["pct"])
    if "shredAura" in spec and rng <= rv(spec["shredAura"]["hexes"]):
        out["shredAura"] = rv(spec["shredAura"]["pct"])
    if "burnAura" in spec and ("hexes" not in spec["burnAura"]
                               or rng <= rv(spec["burnAura"]["hexes"])):
        out["burnAura"] = [rv(spec["burnAura"]["pct"]), rv(spec["burnAura"]["duration"])]
    for k in ("hpMult", "durability", "attackDamageTaken", "regenMissingPct", "allyHealPct"):
        if k in spec:
            out[k] = rv(spec[k])
    if "durabilityByHealth" in spec:
        db = spec["durabilityByHealth"]
        out["durabilityByHealth"] = [rv(db["below"]), rv(db["above"]), rv(db["threshold"])]
    if "thorns" in spec:
        out["thorns"] = [rv(spec["thorns"]["damage"]), rv(spec["thorns"]["cooldown"])]
    if "resistsPerAttacker" in spec:
        out["resistsPerAttacker"] = [rv(spec["resistsPerAttacker"]["armor"]),
                                     rv(spec["resistsPerAttacker"]["mr"])]
    if "healPerInterval" in spec:
        out["healPerInterval"] = [rv(spec["healPerInterval"]["pct"]), rv(spec["healPerInterval"]["interval"])]
    if "shieldAtHp" in spec:
        sh = spec["shieldAtHp"]
        out["shieldAtHp"] = [rv(sh["threshold"]), rv(sh["pct"]),
                             rv(sh["duration"]) if "duration" in sh else 1e9,
                             1.0 if sh.get("decays") else 0.0]
    if "shieldAtStart" in spec:
        out["shieldAtStart"] = [rv(spec["shieldAtStart"]["pct"]), rv(spec["shieldAtStart"]["duration"])]
    if "resistsAtStart" in spec:
        rs = spec["resistsAtStart"]
        out["resistsAtStart"] = [rv(rs["armor"]), rv(rs["mr"]), rv(rs["duration"])]
    if "untargetableAtHp" in spec:
        un = spec["untargetableAtHp"]
        out["untargetableAtHp"] = [rv(un["threshold"]), rv(un["duration"]), rv(un["healMissing"])]
    if "manaAtHp" in spec:
        out["manaAtHp"] = [rv(spec["manaAtHp"]["threshold"]), rv(spec["manaAtHp"]["mana"])]
    if spec.get("adapPerHit"):
        out["adapPerHit"] = True
    if "ionicSpark" in spec and rng <= rv(spec["ionicSpark"]["hexes"]):
        out["ionicSpark"] = rv(spec["ionicSpark"]["pct"])
    if "hoj" in spec:
        h = spec["hoj"]
        out["hoj"] = [rv(h["adPct"]), rv(h["ap"]), rv(h["omnivamp"]), rv(h["threshold"])]
    if spec.get("note"):
        out["note"] = spec["note"]
    return out


def trait_spec(snap, api, col, trait_fx, unit):
    """One trait at breakpoint column `col` for the engine, per
    trait-effects.json, every row read at that column."""
    t = snap.traits[api]
    spec = trait_fx[api]
    c = t["curve"]

    def rv(s):
        if isinstance(s, list):
            return sum(rv(x) for x in s)
        if isinstance(s, dict):
            return row_value(c, dict(s, col=s.get("col", col)))
        return float(s)
    own_mult = rv(spec["ownMultiplier"]) if "ownMultiplier" in spec else 1.0
    out = {"api": api, "name": t["name"],
           "stats": [[k, rv(s) * own_mult] for k, s in (spec.get("stats") or {}).items()]}
    if spec.get("precision"):
        out["precision"] = True
    if "asPerAttackStack" in spec:
        out["asPerAttackStack"] = [rv(spec["asPerAttackStack"]), rv(spec["maxStacks"])]
    if "apPerCast" in spec:
        out["apPerCast"] = rv(spec["apPerCast"]) * 100.0
    if "ampAfterSameTarget" in spec:
        out["ampAfterSameTarget"] = [rv(spec["ampAfterSameTarget"]["amp"]),
                                     rv(spec["ampAfterSameTarget"]["seconds"])]
    for k in ("bleed", "burnOnHit", "caustic"):
        if k in spec:
            out[k] = [rv(spec[k]["pct"]), rv(spec[k]["duration"])]
    for k in ("bonusMagicPct", "durability", "omnivamp"):
        if k in spec:
            out[k] = rv(spec[k])
    if "ravager" in spec:
        out["ravager"] = [rv(spec["ravager"]["amp"]), rv(spec["ravager"]["threshold"]),
                          rv(spec["ravager"]["multiplier"])]
    if "pixies" in spec:
        cnt = spec["pixies"]["count"]
        n = cnt[col - 1] if col - 1 < len(cnt) else cnt[-1]
        out["pixies"] = rv(spec["pixies"]["adapPerPixie"]) * n
    if spec.get("riftbeast"):
        out["riftbeast"] = True
    if "shieldAtStart" in spec:
        out["shieldAtStart"] = [rv(spec["shieldAtStart"]["pct"]), rv(spec["shieldAtStart"]["duration"])]
    if "shieldAtHp" in spec:
        sh = spec["shieldAtHp"]
        out["shieldAtHp"] = [rv(sh["threshold"]), rv(sh["pct"]), rv(sh["duration"])]
    if "resistsPerAttacker" in spec:
        out["resistsPerAttacker"] = [rv(spec["resistsPerAttacker"]["armor"]),
                                     rv(spec["resistsPerAttacker"]["mr"])]
    if "takedown" in spec:
        out["takedown"] = [rv(spec["takedown"].get("healPct", 0.0)), rv(spec["takedown"].get("mana", 0.0))]
    if "faeHeal" in spec:
        cnt = spec["faeHeal"]["count"]
        n = cnt[col - 1] if col - 1 < len(cnt) else cnt[-1]
        out["faeHeal"] = [rv(spec["faeHeal"]["threshold"]), rv(spec["faeHeal"]["healPerPixie"]) * n]
    if "summoner" in spec:
        out["summoner"] = {k: rv(s) for k, s in spec["summoner"].items()}
    if spec.get("note"):
        out["note"] = spec["note"]
    return out


def driver_name(unit):
    """The engine driver for a unit, by the unit's api name."""
    return engine().DRIVERS.get(unit["api"])


def has_driver(unit):
    return unit["api"] in engine().DRIVERS


def calc_value(unit, name, star, ad, ap, max_hp, armor, mr, runtime=None, base_ad=None):
    """Fold one of the unit's ability calculations at the given stats: the
    engine's calc_value on the unit's kit at `star`. Term conventions (from
    the display values the data resolves them to): an AttackDamage
    coefficient is the damage at the unit's base attack damage for that
    star and scales with the attack damage it has (the rows grow ×1.5 per
    star exactly like base AD, so Warwick's 200/300/450 bite is 500% AD
    throughout); AbilityPower ones are a flat amount per 100 AP;
    HealthMax/Armor/MagicResist/BasicAttackDamage and calc-to-calc
    references are fractions."""
    if base_ad is None:
        base_ad = unit["stats"]["ad"] * AD_PER_STAR ** (star - 1)
    return engine().calc_value(kit_spec(unit, star), name, ad, ap, max_hp, armor, mr, base_ad,
                               runtime or None)


def trait_notes(snap, ctx_traits, trait_fx):
    """What the active traits leave unmodeled: their hand-file notes, in
    context order."""
    return [f"{snap.traits[api]['name']}: {trait_fx[api]['note']}"
            for api, _ in ctx_traits if trait_fx[api].get("note")]


def cell_spec(snap, unit, star, geometry, ctx_traits, dummy_spec, duration=None, pressure=None,
              item_fx=None, trait_fx=None, pool=(), items=(), driver=None):
    """Everything the engine needs for one unit's fights: kits per form,
    dummies (armed and standing for the board per the objective), the
    role's and the traits' contributions, the item pool for an
    enumeration or the build's items for one fight."""
    item_fx = item_fx if item_fx is not None else load_item_effects(snap.set_no)
    trait_fx = trait_fx if trait_fx is not None else load_trait_effects(snap.set_no)
    objective = unit.get("objective", "carry")
    if pressure is None:
        pressure = objective in PRESSURED
    if duration is None:
        duration = fight_duration(unit)
    board = objective == "tank"
    streams = dummy_spec.get("board") if board else None
    slots = [dict(s, streams=int(streams[i]) if streams else 1)
             for i, s in enumerate(dummy_spec["slots"])]
    kits = {"base": kit_spec(unit, star, None)}
    if unit.get("forms"):
        kits["AD"] = kit_spec(unit, star, "AD")
        kits["AP"] = kit_spec(unit, star, "AP")
    kind = unit_role_kind(unit)
    role = {"manaRegen": CASTER_MANA_REGEN if kind == "Caster" else 0.0,
            "asPct": FIGHTER_AS_BY_STAGE[STAGE] if kind == "Fighter" else 0.0}
    return {
        "unit": {"api": unit["api"], "name": unit["name"], "kind": kind, "attack": bool(unit["attack"]),
                 "objective": objective, "range": unit["stats"]["range"],
                 "castTime": unit.get("castTime"), "hasForms": bool(unit.get("forms")),
                 "extras": {api: e["stats"] for api, e in snap.extras.items()}},
        "star": star, "kits": kits, "geometry": geometry, "duration": float(duration),
        "pressure": bool(pressure), "immortal": objective == "tank",
        "enemyDebuffs": dummy_spec.get("enemyDebuffs", tank_debuffs(snap))
                        if board and pressure else {},
        "dummies": {"critEv": dummy_spec.get("critEv", 1.1), "slots": slots},
        "role": role,
        "traits": [trait_spec(snap, api, col, trait_fx, unit) for api, col in ctx_traits],
        "pool": [item_spec(snap, api, item_fx, unit) for api in pool],
        "items": [item_spec(snap, api, item_fx, unit) for api in items],
        "driver": driver or driver_name(unit),
    }


def simulate(snap, unit, star, item_apis, geometry, ctx_traits, dummy_spec, duration=None,
             item_fx=None, trait_fx=None, driver=None, pressure=None, trace=False):
    """One build's fight: (opening sheet, result). The duration and whether
    the dummies hit back follow the unit's objective unless given; `driver`
    names another engine driver ("Driver" is the plain base: attacks only);
    with `trace` the result carries every event as (t, kind, amount,
    target, src, hp)."""
    spec = cell_spec(snap, unit, star, geometry, ctx_traits, dummy_spec, duration, pressure,
                     item_fx, trait_fx, items=list(item_apis), driver=driver)
    return engine().simulate(spec, trace)


def enumerate_builds(snap, unit, star, geometry, ctx_traits, dummy_spec, pool, duration=None,
                     top=None, workers=0, item_fx=None, trait_fx=None, log=None):
    """Every multiset of three pool items (unique items at most once),
    simulated on every core and sorted best first (rank_key, ties by the
    build's api names): ([(combo, sheet, result), ...] for the top rows,
    build count)."""
    spec = cell_spec(snap, unit, star, geometry, ctx_traits, dummy_spec, duration, None,
                     item_fx, trait_fx, pool=list(pool))
    count, rows = engine().run_cell(spec, top or CACHED_ROWS, workers)
    return [(tuple(pool[i] for i in idx), sheet, res) for idx, sheet, res in rows], count


# ---------------------------------------------------------------------------
# enumeration + ranking
# ---------------------------------------------------------------------------

def pool_items(snap, item_fx):
    """The craftable completed items a build may hold: every standard
    completed item (two components) that isn't excluded by hand."""
    excluded = (item_fx.get("excluded") or {})
    out = []
    for api, it in snap.items.items():
        if not api.startswith("DA_") or api in excluded:
            continue
        if any(x in api for x in ("Artifact", "Radiant", "Emblem", "Component", "Spatula")):
            continue
        if len(it["composition"]) != 2:
            continue
        out.append(api)
    return sorted(out, key=lambda a: snap.items[a]["name"])


def rank_key(res, objective="carry"):
    """Carries and fighters: killers by kill time, ties broken by the
    damage they had to spare (the uncapped total, i.e. overkill); the rest
    after them by damage dealt (a fighter's stops when it dies). Tanks: by
    how long they held the dummies, then by the damage their stuns denied,
    then damage dealt. Capped tanks are compared at double pressure before
    utility; surviving both tests leaves them tied."""
    if objective == "tank":
        capped = res.get("survivalCapped", False)
        stress_capped = capped and res.get("stressCapped", False)
        return (-res.get("aliveTime", 0.0), -int(capped),
                -(res.get("stressAliveTime") or 0.0) if capped else 0.0,
                -int(stress_capped),
                0.0 if stress_capped else -res.get("denied", 0.0),
                0.0 if stress_capped else -res["total"])
    if res["killTime"] is not None:
        return (0, res["killTime"], -res.get("rawTotal", res["total"]))
    return (1, 0.0, -res["total"], -res.get("rawTotal", res["total"]))




# ---------------------------------------------------------------------------
# cache + warm (mirrors builds.py: content-addressed cells, one warmer)
# ---------------------------------------------------------------------------

def modeled_units(snap):
    """Units with a driver, in cost then name order."""
    out = [u for u in snap.units.values() if has_driver(u)]
    return sorted(out, key=lambda u: (u["cost"], u["name"]))


def unit_slug(unit):
    return re.sub(r"[^a-z0-9]", "", unit["name"].lower())


def cells(snap=None):
    snap = snap or load_snapshot()
    return [(unit_slug(u), key) for u in modeled_units(snap) for key in unit_scenarios(u)]


def cell_paths(snap=None):
    snap = snap or load_snapshot()
    base = hashlib.sha256(SOURCE_HASH.encode())
    base.update(snap.patch.encode())
    base.update(snap.hash_inputs().encode())
    for fn in ("item-effects.json", "trait-effects.json", "kits.json"):
        p = os.path.join(set_dir(snap.set_no), fn)
        if os.path.exists(p):
            with open(p, "rb") as f:
                base.update(f.read())
    base.update(json.dumps([FIGHT_DURATION, TANK_DURATION, N_DUMMIES, DUMMY_STAR, STAGE,
                            CACHED_ROWS, sorted(SCENARIOS)]).encode())
    paths = {}
    for u in modeled_units(snap):
        slug = unit_slug(u)
        for key, sc in unit_scenarios(u).items():
            h = base.copy()
            h.update(json.dumps([u["api"], key, sc], sort_keys=True).encode())
            paths[(slug, key)] = os.path.join(
                CACHE_DIR, f"{slug}-{key}-{h.hexdigest()[:16]}.json")
    return paths


def cell_ready(snap=None):
    return {f"{slug}/{key}": os.path.exists(p)
            for (slug, key), p in cell_paths(snap).items()}


def cached_scenario(slug, key, paths=None):
    paths = paths or cell_paths()
    if (slug, key) not in paths:
        raise ValueError(f"unknown cell {slug}/{key}")
    p = paths[(slug, key)]
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def cell_rows(snap, unit, out, count=CACHED_ROWS):
    """The cached rows of a cell: the top `count` builds of an enumeration,
    rounded as the dashboard shows them."""
    pressured = unit["objective"] in PRESSURED
    rows = []
    previous_key, previous_rank = None, None
    for n, (combo, sheet, res) in enumerate(out[:count], 1):
        key = rank_key(res, unit["objective"])
        rank = previous_rank if unit["objective"] == "tank" and key == previous_key else n
        previous_key, previous_rank = key, rank
        row = {
            "rank": rank, "items": [snap.items[a]["name"] for a in combo],
            "ad": round(sheet["ad"], 1), "ap": round(sheet["ap"]),
            "attackSpeed": round(sheet["as"], 2),
            "crit": round(sheet["crit"] * 100), "precision": sheet["precision"],
            "killTime": round(res["killTime"], 2) if res["killTime"] is not None else None,
            "dps": round(res["dps"]), "total": round(res["total"]),
            "overkill": round(res["rawTotal"] - res["total"]),
            "attacks": res["attacks"], "casts": res["casts"],
            "breakdown": {k: round(v) for k, v in sorted(res["breakdown"].items(),
                                                          key=lambda kv: -kv[1])},
            "left": [round(x) for x in res["left"]],
        }
        if sheet["form"]:
            row["form"] = sheet["form"]
        if pressured:
            row.update({
                "hp": round(sheet["hp"]), "armor": round(sheet["armor"]), "mr": round(sheet["mr"]),
                "physicalEhp": round(sheet["physicalEhp"]), "magicEhp": round(sheet["magicEhp"]),
                "durability": round(sheet["durability"] * 100), "omnivamp": round(sheet["omnivamp"] * 100),
                "aliveTime": round(res["aliveTime"], 2), "died": res["died"],
                "diedAt": round(res["diedAt"], 2) if res["diedAt"] is not None else None,
                "hpLeft": round(res["hpLeft"]),
                "survivalCapped": res["survivalCapped"],
                "stressAliveTime": round(res["stressAliveTime"], 2)
                                   if res["stressAliveTime"] is not None else None,
                "stressCapped": res["stressCapped"],
                "absorbed": round(res["absorbed"]), "taken": round(res["taken"]),
                "healed": round(res["healed"]), "shielded": round(res["shielded"]),
                "denied": round(res["denied"]), "ccTime": round(res["ccTime"], 1),
                "allyHeal": round(res["allyHeal"]), "allyShield": round(res["allyShield"]),
            })
        rows.append(row)
    return rows


def compute_cell(snap, unit, key, paths, log=None, prune=True):
    sc = unit_scenarios(unit)[key]
    t0 = time.time()
    item_fx = load_item_effects(snap.set_no)
    trait_fx = load_trait_effects(snap.set_no)
    contexts, unmodeled = unit_trait_contexts(snap, unit, trait_fx)
    ctx_traits = contexts[sc["traits"]]
    dummy = dummies_for(snap, threat=sc["threat"] if unit["objective"] == "tank" else None)
    pool = pool_items(snap, item_fx)
    duration = fight_duration(unit)
    out, count = enumerate_builds(snap, unit, sc["star"], sc["geometry"],
                                  ctx_traits, dummy, pool, duration, log=log)
    secs = round(time.time() - t0, 3)
    pressured = unit["objective"] in PRESSURED
    rows = cell_rows(snap, unit, out, CACHED_ROWS)
    fx_notes = trait_notes(snap, ctx_traits, trait_fx)
    traits_active = [{"trait": snap.traits[api]["name"], "breakpoint": snap.traits[api]["levels"][col - 1]}
                     for api, col in ctx_traits]
    payload = {
        "unit": unit_slug(unit), "unitName": unit["name"], "unitApi": unit["api"],
        "cost": unit["cost"], "role": unit["roleName"], "kind": unit["kind"],
        "objective": unit["objective"], "pressured": pressured,
        "scenario": {**sc, "duration": duration, "dummy": dummy, "traitsActive": traits_active,
                     "traitsUnmodeled": unmodeled, "notes": fx_notes,
                     "driver": driver_name(unit)},
        "set": snap.set_no, "patch": snap.patch, "buildsEvaluated": count,
        "rows": rows,
        "computedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "computeSeconds": secs,
    }
    os.makedirs(CACHE_DIR, exist_ok=True)
    slug = unit_slug(unit)
    path = paths[(slug, key)]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, path)
    if prune:
        for old in glob.glob(os.path.join(CACHE_DIR, f"{slug}-{key}-*.json")):
            # The mixed key is also a prefix of its physical/magic variants.
            if old != path and re.fullmatch(re.escape(f"{slug}-{key}-") + r"[0-9a-f]{16}\.json",
                                            os.path.basename(old)):
                os.remove(old)
    return payload


def warm_lock():
    import fcntl
    os.makedirs(CACHE_DIR, exist_ok=True)
    f = open(os.path.join(CACHE_DIR, "lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def warm_running():
    lock = warm_lock()
    if lock is None:
        return True
    lock.close()
    return False


def _say(line):
    print(line, flush=True)


def warm(log=_say, only=None, *, snap=None, prune=True):
    """Compute every cold cell. Returns the count, or None if another
    warmer holds the lock."""
    import signal
    lock = warm_lock()
    if lock is None:
        return None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        for tmp in glob.glob(os.path.join(CACHE_DIR, "*.tmp")):
            os.remove(tmp)
        snap = snap or load_snapshot()
        paths = cell_paths(snap)
        for path in glob.glob(os.path.join(CACHE_DIR, "*.json")):
            m = re.fullmatch(r"([a-z0-9]+)-(s\d-[a-z]+-[a-z]+(?:-[a-z]+)?)-[0-9a-f]{16}\.json",
                             os.path.basename(path))
            if prune and m and (m.group(1), m.group(2)) not in paths:
                os.remove(path)
        units = modeled_units(snap)
        cold = [(u, key) for u in units for key in unit_scenarios(u)
                if not os.path.exists(paths[(unit_slug(u), key)])
                and (only is None or unit_slug(u) == only)]
        done = 0
        for n, (u, key) in enumerate(cold, 1):
            log(f"[{n}/{len(cold)}] {u['name']} {key} …")
            out = compute_cell(snap, u, key, paths, log=log, prune=prune)
            best = out["rows"][0] if out["rows"] else None
            score = "" if not best else (
                f" (held {best['aliveTime']}s{'+' if best['survivalCapped'] else ''})" if out["objective"] == "tank"
                else f" ({best['killTime']}s)" if best["killTime"] is not None
                else f" ({best['total']} dmg)")
            log(f"  {out['buildsEvaluated']:,} builds in {out['computeSeconds']}s"
                + (f" — best {', '.join(best['items'])}{score}" if best else ""))
            done += 1
        return done
    finally:
        lock.close()


# ---------------------------------------------------------------------------
# scheduled refresh
# ---------------------------------------------------------------------------

def snapshot_revision(snap=None):
    snap = snap or load_snapshot()
    # Use the same inputs as the build cache, including item/trait models.
    return json_hash([snap.set_no, snap.patch,
                      sorted(os.path.basename(p) for p in cell_paths(snap).values())])


def refresh_state():
    try:
        with open(REFRESH_STATE_FILE) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json_atomic(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(content, f, sort_keys=True)
    os.replace(tmp, path)


def dashboard_ready(snap):
    """Signal a complete generation; preserve the previous generation's cache."""
    path = os.path.join(CACHE_DIR, ".dashboard-ready")
    try:
        with open(path) as f:
            before = json.load(f)
    except (OSError, ValueError):
        before = None
    if not isinstance(before, dict):
        before = None
    files = sorted(os.path.basename(p) for p in cell_paths(snap).values())
    marker = {"revision": snapshot_revision(snap), "patch": snap.patch, "cacheFiles": files}
    if before != marker:
        write_json_atomic(path, marker)
    # The old server may still be answering until systemd's reload runs.
    # Keep both complete generations; a later successful run reclaims them.
    if before:
        keep = set(files) | set(before.get("cacheFiles", []))
        for filename in os.listdir(CACHE_DIR):
            if (re.fullmatch(r"[a-z0-9]+-s\d-[a-z]+-[a-z]+(?:-[a-z]+)?-[0-9a-f]{16}\.json", filename)
                    and filename not in keep):
                os.remove(os.path.join(CACHE_DIR, filename))


def cmd_refresh(args):
    """Scheduled local update: reconcile, warm, publish, signal and report."""
    import fcntl
    import signal
    from tft_update import ReviewRequired

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, "refresh.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A TFT refresh is already running; skipping this duplicate.")
            return
        now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
        previous_state = refresh_state()
        state = {"status": "running", "phase": "checking", "startedAt": now(),
                 "message": "Checking Riot's patch notes and current source data.",
                 "lastSuccessAt": previous_state.get("lastSuccessAt"), "exit": None}

        def report(**fields):
            state.update(fields)
            write_json_atomic(REFRESH_STATE_FILE, state)

        old_term = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        try:
            report(activePatch=load_snapshot(args.set).patch)

            def prepare(candidate):
                report(phase="warming", message=f"Recalculating changed builds for {candidate.patch}.")
                with open(os.path.join(CACHE_DIR, "refresh-warm.log"), "a") as log:
                    def log_line(line):
                        print(line, file=log, flush=True)
                    deadline = time.monotonic() + 20 * 60
                    while True:
                        count = warm(log=log_line, snap=candidate, prune=False)
                        if count is not None:
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Another build calculation did not finish in time; will retry on the next check.")
                        time.sleep(2)
                ready = cell_ready(candidate)
                if not ready or not all(ready.values()):
                    raise RuntimeError("Some builds did not finish; keeping the previous snapshot.")
                report(computedCells=count, phase="publishing", message=f"All {len(ready)} scenarios are ready for {candidate.patch}.")

            snap = cmd_fetch(SimpleNamespace(set=args.set, patch=getattr(args, "patch", None), force=True),
                             automatic=True, prepare=prepare, progress=report)
            dashboard_ready(snap)
            finished = now()
            count = state.get("computedCells", 0)
            report(status="ok", phase="complete", activePatch=snap.patch, targetPatch=snap.patch,
                   checkedAt=finished, finishedAt=finished, lastSuccessAt=finished, exit=0,
                   revision=snapshot_revision(snap),
                   message=f"Patch {snap.patch} is ready; {count} scenarios recalculated.")
            print(state["message"])
        except BaseException as error:
            needs_review = isinstance(error, ReviewRequired)
            code = 2 if needs_review else error.code if isinstance(error, SystemExit) and isinstance(error.code, int) else 1
            finished = now()
            report(status="needs-review" if needs_review else "failed", phase="stopped",
                   checkedAt=finished, finishedAt=finished, exit=code,
                   message=str(error)[:1000] or "Refresh interrupted; the previous builds remain available.")
            print(state["message"], file=sys.stderr)
            raise SystemExit(code) from error
        finally:
            signal.signal(signal.SIGTERM, old_term)


# ---------------------------------------------------------------------------
# web API
# ---------------------------------------------------------------------------

def cdragon_image_url(asset):
    """Resolve an archived game asset without guessing a MetaTFT asset name."""
    if not isinstance(asset, str):
        return None
    path = asset.strip().lower()
    if (not re.fullmatch(r"assets/[a-z0-9_./-]+\.(?:tex|png)", path)
            or any(part in (".", "..") for part in path.split("/"))):
        return None
    return "https://raw.communitydragon.org/latest/game/" + re.sub(r"\.tex$", ".png", path)


def ui_icons(snap):
    """Prefer exact game IDs; a display-name fallback must be unambiguous."""
    champions = snap.communitydragon.get("champions", [])
    by_api = {u["apiName"]: u for u in champions}
    units = {}
    for api, unit in snap.units.items():
        match = next((by_api[a] for a in [api, *unit["assets"]] if a in by_api), None)
        if match is None:
            named = [u for u in champions if (u.get("name") or "").casefold() == unit["name"].casefold()]
            match = named[0] if len(named) == 1 else {}
        units[api] = next((url for key in ("squareIcon", "tileIcon", "icon")
                           if (url := cdragon_image_url(match.get(key)))), None)
    traits = {t["apiName"]: cdragon_image_url(t.get("icon"))
              for t in snap.communitydragon.get("traits", [])}
    return units, traits


def ui_number(value):
    """Keep binary float artifacts out of display-only trait summaries."""
    return f"{round(value, 4):g}"


def trait_description(trait):
    """Render supported tooltip paragraphs from the corrected curve table.

    Dynamic board state and unknown localization tags are omitted with their
    paragraph. No unresolved template is passed to the browser as prose.
    """
    def curve_text(match):
        attrs = {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', match[1])}
        row = trait["curve"].get(attrs.get("row"))
        if not row:
            raise ValueError("missing tooltip curve")
        columns = [int(attrs["column"])] if "column" in attrs else list(range(1, len(trait["levels"]) + 1))
        values = [curve_at(row, column) for column in columns]
        fmt = attrs.get("format", "").lower()
        if fmt not in ("", "percent", "percentminusone", "invertedpercent"):
            raise ValueError("unknown tooltip format")
        if fmt:
            values = [100 * (v - 1 if fmt == "percentminusone" else 1 - v if fmt == "invertedpercent" else v)
                      for v in values]
        shown = [ui_number(v) for v in values]
        if not shown:
            raise ValueError("missing tooltip columns")
        if len(set(shown)) == 1:
            shown = shown[:1]
        return "/".join(shown) + ("%" if fmt else "")

    paragraphs = []
    text = (trait.get("desc") or "").replace("\\r\\n", "\n").replace("\r", "\n")
    for paragraph in re.split(r"\n\s*\n", text):
        # Runtime trackers appended after a complete sentence are optional;
        # keep that sentence without inventing a current board-state value.
        paragraph = re.sub(r"(?<=[.!?])(?:\s*\{[^{}]*\})+\s*$", "", paragraph)
        try:
            paragraph = re.sub(r"<TFTCurveTable\s+([^>]*?)/>", curve_text, paragraph, flags=re.I)
        except (ValueError, TypeError):
            continue
        if re.search(r"<(?:TFT|img\b)|[{}]|@[^@]*@|%i:", paragraph, re.I):
            continue
        paragraph = re.sub(r"<br\s*/?>", "\n", paragraph, flags=re.I)
        paragraph = unescape(re.sub(r"<[^>]*>", "", paragraph))
        paragraph = " ".join(paragraph.split())
        if paragraph and not paragraph.endswith(":"):
            paragraphs.append(paragraph)
    return "\n\n".join(paragraphs) or None


def trait_bonus_notes(snap, unit, api, column, trait_fx):
    """Describe the same resolved values supplied to the combat engine."""
    fx = trait_spec(snap, api, column, trait_fx, unit)
    number = ui_number
    percent = lambda value: number(value * 100) + "%"
    notes = []
    flat = {"ap": "Ability Power", "hp": "Health", "armor": "Armor", "mr": "Magic Resist", "manaRegen": "Mana Regen"}
    ratios = {"adPct": "Attack Damage", "asPct": "Attack Speed", "crit": "Critical Strike Chance", "amp": "Damage Amp"}
    stats = []
    for key, value in fx["stats"]:
        if not value or (key == "hpMult" and value == 1):
            continue
        if key in flat:
            stats.append(f"+{number(value)} {flat[key]}")
        elif key in ratios:
            stats.append(f"+{percent(value)} {ratios[key]}")
        elif key == "hpMult":
            stats.append(f"+{percent(value - 1)} max Health")
        elif key in ("adap", "adOrAp"):
            joiner = "and" if key == "adap" else "or"
            stats.append(f"+{percent(value)} Attack Damage {joiner} +{number(value * 100)} Ability Power")
    if stats:
        notes.append(", ".join(stats) + ".")
    if fx.get("precision"):
        notes.append("Abilities can critically strike.")
    if "asPerAttackStack" in fx:
        amount, count = fx["asPerAttackStack"]
        notes.append(f"+{percent(amount)} Attack Speed per attack, up to {number(count)} stacks.")
    if fx.get("apPerCast"):
        notes.append(f"+{number(fx['apPerCast'])} Ability Power per cast.")
    if "ampAfterSameTarget" in fx:
        amount, seconds = fx["ampAfterSameTarget"]
        notes.append(f"+{percent(amount)} Damage Amp after {number(seconds)} seconds on the same target.")
    for key, label in (("durability", "Durability"), ("omnivamp", "Omnivamp")):
        if fx.get(key):
            notes.append(f"+{percent(fx[key])} {label}.")
    if fx.get("bleed", [0])[0]:
        amount, seconds = fx["bleed"]
        notes.append(f"Deal {percent(amount)} bonus true damage as bleed over {number(seconds)} seconds.")
    if "burnOnHit" in fx:
        amount, seconds = fx["burnOnHit"]
        notes.append(f"Burn enemies for {percent(amount)} max Health per second for {number(seconds)} seconds.")
    if "caustic" in fx:
        amount, seconds = fx["caustic"]
        notes.append(f"Reduce enemy Armor and Magic Resist by {percent(amount)} for {number(seconds)} seconds.")
    if fx.get("bonusMagicPct"):
        notes.append(f"Deal {percent(fx['bonusMagicPct'])} bonus magic damage.")
    if "ravager" in fx:
        amount, threshold, multiplier = fx["ravager"]
        notes.append(f"+{percent(amount)} damage; multiplied by {number(multiplier)} against enemies below {percent(threshold)} Health.")
    if fx.get("pixies"):
        notes.append(f"Pixies grant +{percent(fx['pixies'])} Attack Damage and +{number(fx['pixies'] * 100)} Ability Power.")
    if "faeHeal" in fx:
        threshold, amount = fx["faeHeal"]
        notes.append(f"Heal {percent(amount)} max Health after falling below {percent(threshold)} Health.")
    if "shieldAtStart" in fx:
        amount, seconds = fx["shieldAtStart"]
        notes.append(f"Start combat with a {percent(amount)} max Health shield for {number(seconds)} seconds.")
    if "shieldAtHp" in fx:
        threshold, amount, seconds = fx["shieldAtHp"]
        notes.append(f"Below {percent(threshold)} Health, gain a {percent(amount)} max Health shield for {number(seconds)} seconds.")
    if "resistsPerAttacker" in fx:
        armor, mr = fx["resistsPerAttacker"]
        notes.append(f"+{number(armor)} Armor and +{number(mr)} Magic Resist per enemy targeting this champion.")
    if "takedown" in fx:
        heal, mana = fx["takedown"]
        rewards = ([f"heal {percent(heal)} max Health"] if heal else []) + ([f"restore {number(mana)} Mana"] if mana else [])
        if rewards:
            notes.append("On takedown, " + " and ".join(rewards) + ".")
    if "summoner" in fx:
        bonuses = fx["summoner"]
        own = {"TFT18_Yorick": ("healthMult", "spirit Health"),
               "TFT18_Azir": ("damageMult", "soldier damage"),
               "TFT18_MamaBeak": ("damageMult", "summon damage")}.get(unit["api"])
        if own:
            notes.append(f"+{percent(bonuses[own[0]] - 1)} {own[1]}.")
        if unit["api"] == "TFT18_Zyra":
            notes.append(f"Plants make {number(bonuses['extraAttacks'])} additional attacks.")
        if unit["api"] in ("TFT18_Azir", "TFT18_Zyra", "TFT18_Krug") and bonuses.get("extraSummons", 1) > 1:
            notes.append(f"+{number(bonuses['extraSummons'] - 1)} additional summon.")
    notes.extend(trait_notes(snap, [(api, column)], trait_fx))
    return notes


def api_meta():
    snap = load_snapshot()
    item_fx = load_item_effects(snap.set_no)
    trait_fx = load_trait_effects(snap.set_no)
    kits = load_kits(snap.set_no)
    unit_icons, trait_icons = ui_icons(snap)
    units = []
    for u in modeled_units(snap):
        contexts, unmodeled = unit_trait_contexts(snap, u, trait_fx)
        units.append({
            "slug": unit_slug(u), "name": u["name"], "api": u["api"],
            "icon": unit_icons.get(u["api"]),
            "cost": u["cost"], "role": u["roleName"], "kind": u["kind"],
            "attack": u["attack"], "objective": u["objective"],
            "stars": list(unit_stars(u)),
            "duration": fight_duration(u),
            "traits": u["traits"], "ability": u["ability"]["name"],
            "traitApis": u["traitApis"],
            "traitBonuses": {context: [{"api": api, "breakpoint": snap.traits[api]["levels"][column - 1],
                                       "notes": trait_bonus_notes(snap, u, api, column, trait_fx)}
                                      for api, column in active] for context, active in contexts.items()},
            "forms": sorted(u["forms"]) if u.get("forms") else [],
            "traitsModeled": [snap.traits[a]["name"] for a, _ in contexts["high"]],
            "traitsUnmodeled": unmodeled,
            "note": (kits.get("units", {}).get(u["api"]) or {}).get("note"),
        })
    pool = pool_items(snap, item_fx)
    items = []
    for api in pool:
        it = snap.items[api]
        spec = (item_fx.get("items") or {}).get(api) or {}
        items.append({"api": api, "name": it["name"],
                      "stats": parse_stat_line(it),
                      "modeled": [k for k in spec if k not in ("note", "name", "covers")],
                      "note": spec.get("note")})
    traits = [{"api": api, "name": trait["name"], "icon": trait_icons.get(api),
               "description": trait_description(trait), "levels": trait["levels"], "styles": trait["styles"],
               "modeled": api in trait_fx and bool(trait["levels"]),
               "note": (trait_fx.get(api) or {}).get("note") if api in trait_fx
                       else "This trait is not modeled in these build results."}
              for api, trait in sorted(snap.traits.items(), key=lambda pair: pair[1]["name"])]
    return {
        "set": snap.set_no, "patch": snap.patch,
        "revision": snapshot_revision(snap),
        "metatft": snap.meta.get("metatft"), "fetchedAt": snap.meta.get("fetchedAt"),
        "sources": snap.meta.get("sources", {}), "verifiedAt": snap.meta.get("verifiedAt"),
        "verification": snap.meta.get("verification"),
        "sourceLimitations": (snap.audit or {}).get("unresolved", []),
        "units": units, "scenarios": list(SCENARIOS.values()),
        "stars": list(STARS), "geometries": GEOMETRIES, "traitContexts": TRAIT_CONTEXTS,
        "tankThreats": tank_threats(snap), "tankDebuffs": tank_debuffs(snap),
        "tankDummies": {key: dummies_for(snap, threat=key) for key in TANK_THREATS},
        "dummy": dummies_for(snap), "items": items, "traits": traits,
        "excluded": item_fx.get("excluded") or {},
        "objectives": OBJECTIVES,
        "rules": {"adPerStar": AD_PER_STAR, "hpPerStar": HP_PER_STAR,
                  "critExcess": CRIT_EXCESS_TO_DAMAGE, "manaLock": MANA_LOCK_S,
                  "asCap": AS_CAP, "stage": STAGE, "duration": FIGHT_DURATION,
                  "tankDuration": TANK_DURATION,
                  "tankMana": [TANK_MANA_PER_PREMIT, TANK_MANA_PER_POSTMIT, TANK_MANA_PER_HIT_CAP],
                  "assassinReduction": ASSASSIN_OFFTARGET_REDUCTION},
        "note": item_fx.get("_note") or "",
    }


# ---------------------------------------------------------------------------
# patch-note reconciliation
# ---------------------------------------------------------------------------

def _nums(s):
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", s)]


def check_audit(snap, notes):
    """Check explicit patch-note targets, including forms, percentages and items.

    Bind the review to the exact lookup and dated patch-note document so a
    changed feed or a new hotfix cannot quietly reuse an obsolete review.
    """
    audit = snap.audit
    findings = []
    for name, have, expected in (
        ("patch", audit.get("patch"), snap.patch),
        ("lookupHash", json_hash(snap.raw), audit.get("lookupHash")),
        ("binsHash", json_hash(snap.bins), audit.get("binsHash")),
        ("patchNotesHash", json_hash(notes), audit.get("patchNotesHash")),
    ):
        if have != expected:
            findings.append({"unit": "Source review", "api": "", "what": name,
                             "where": "audit " + name, "have": have, "old": [],
                             "new": expected, "status": "differs"})
    if not audit.get("checks"):
        findings.append({"unit": "Source review", "api": "", "what": "missing numeric checks",
                         "where": "audit checks", "have": [], "old": [], "new": [], "status": "differs"})
    entities = {"unit": snap.units, "item": snap.items, "trait": snap.traits}
    for check in audit.get("checks", []):
        target = check["target"]
        entity = entities[target["kind"]].get(target["api"])
        data = entity
        if data and target.get("form"):
            data = entity.get("forms", {}).get(target["form"])
        have = []
        where = "stats " + target["stat"] if "stat" in target else "curve " + target["row"]
        if target.get("form"):
            where = target["form"] + " " + where
        if data:
            if "stat" in target:
                have = [data.get("stats", {}).get(target["stat"])]
            else:
                curve = data.get("curve", {}).get(target["row"])
                if curve:
                    have = [curve_at(curve, x) for x in target.get("stars", target.get("columns", [1]))]
        want = check["expected"]
        findings.append({"unit": entity["name"] if entity else target["api"],
                         "api": target["api"], "what": check["what"], "where": where,
                         "have": have, "old": check.get("observedBefore", []), "new": want,
                         "status": "current" if have == want else "differs"})
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
    covered = {normalize(label) for c in audit.get("checks", [])
               for label in (c["what"], c.get("patchLine", {}).get("what", c["what"]))}
    return findings, [ch for ch in notes.get("changes", []) if normalize(ch["what"]) not in covered]


def check_patch_notes(snap):
    """Compare the snapshot's numbers with the patch notes' 'new' values.
    Returns (findings, unmatched): a finding says whether the snapshot
    already carries the new value, still has the old one, or neither."""
    path = os.path.join(snap.dir, "patchnotes.json")
    if not os.path.exists(path):
        raise ValueError(f"missing patch notes for {snap.patch}; cannot verify the snapshot")
    with open(path) as f:
        notes = json.load(f)
    if snap.audit:
        return check_audit(snap, notes)
    changes = notes.get("changes") or []
    findings, unmatched = [], []
    for ch in changes:
        what = ch["what"]
        unit = next((u for u in snap.units.values()
                     if what.lower().startswith(u["name"].lower())), None)
        if unit is None:
            unmatched.append(ch)
            continue
        old, new = _nums(ch["old"]), _nums(ch["new"])
        label = what[len(unit["name"]):].strip().lower()
        stat_key = None
        for k, words in (("mana", ("mana",)), ("as", ("attack speed", "base as")),
                         ("hp", ("health",)), ("ad", ("attack damage", "base ad")),
                         ("armor", ("resists", "armor")), ("mr", ("magic resist",))):
            if any(w in label for w in words) and "ability" not in label:
                stat_key = k
                break
        found = None
        if stat_key == "mana" and len(new) == 2:
            have = [unit["stats"]["initialMana"], unit["stats"]["mana"]]
            found = ("stats initialMana/mana", have, new)
        elif stat_key == "armor" and "resists" in label and len(new) == 1:
            have = [unit["stats"]["armor"], unit["stats"]["mr"]]
            found = ("stats armor/mr", have, new + new)
        elif stat_key in ("as", "hp", "ad", "armor", "mr") and len(new) == 1:
            have = [unit["stats"][stat_key]]
            found = (f"stats {stat_key}", have, new)
        else:
            # an ability number: the curve row whose per-star values match the old or new values
            for row, curve in unit["curve"].items():
                vals = [curve_at(curve, s) for s in range(1, 5)]
                for cand in (new, old):
                    n = len(cand)
                    if n >= 2 and vals[:n] == cand:
                        found = (f"curve {row}", vals[:n], new)
                        break
                if found:
                    break
        if found is None:
            unmatched.append(ch)
            continue
        where, have, want = found
        status = "current" if have[:len(want)] == want else \
            ("stale" if have[:len(old)] == old else "differs")
        findings.append({"unit": unit["name"], "api": unit["api"], "what": what,
                         "where": where, "have": have, "old": old, "new": want,
                         "status": status})
    return findings, unmatched


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_status(args):
    for set_no in sorted(int(d[3:]) for d in os.listdir(TFT_DATA_DIR)
                         if d.startswith("set") and d[3:].isdigit()) if os.path.isdir(TFT_DATA_DIR) else []:
        for patch in patch_dirs(set_no):
            snap = Snapshot(set_no, patch)
            print(f"set {set_no} patch {patch}: {len(snap.units)} units, "
                  f"{len(snap.items)} items, {len(snap.traits)} traits, "
                  f"fetched {snap.meta.get('fetchedAt')}, MetaTFT stamp "
                  f"{(snap.meta.get('metatft') or {}).get('patch')}")


def cmd_check(args):
    snap = load_snapshot(args.set, args.patch)
    findings, unmatched = check_patch_notes(snap)
    stale = [f for f in findings if f["status"] != "current"]
    for f in findings:
        mark = {"current": "ok   ", "stale": "STALE", "differs": "DIFF "}[f["status"]]
        print(f"{mark} {f['unit']:<14} {f['what']:<40} {f['where']:<28} "
              f"have {f['have']} → notes say {f['new']}")
    print(f"\n{len(findings)} matched, {len(stale)} need attention, "
          f"{len(unmatched)} patch-note lines outside the numeric checks:")
    for ch in unmatched[:40]:
        print(f"   ? {ch['what']}: {ch['old']} ⇒ {ch['new']}")
    if snap.audit:
        unresolved = snap.audit.get("unresolved", [])
        print(f"\nPatch-note audit: {len(snap.audit.get('checks', []))} explicit checks; "
              f"{len(unresolved)} source limitations recorded in {snap.dir}/audit.json.")
        if stale:
            sys.exit(2)
        return
    if stale:
        print(f"\nOverride snippet ({os.path.relpath(snap.dir)}/overrides.json):")
        snippet = {"units": {}}
        for f in stale:
            u = snippet["units"].setdefault(f["api"], {})
            if f["where"].startswith("curve "):
                u.setdefault("curve", {})[f["where"][6:]] = f["new"]
            elif f["where"] == "stats initialMana/mana":
                u.setdefault("stats", {}).update({"initialMana": f["new"][0], "mana": f["new"][1]})
            elif f["where"] == "stats armor/mr":
                u.setdefault("stats", {}).update({"armor": f["new"][0], "mr": f["new"][1]})
            else:
                u.setdefault("stats", {})[f["where"].split()[1]] = f["new"][0]
        print(json.dumps(snippet, indent=1))
        # a non-zero exit lets the refresh job's heartbeat flag it
        sys.exit(2)


def cmd_units(args):
    snap = load_snapshot(args.set, args.patch)
    for u in sorted(snap.units.values(), key=lambda u: (u["cost"], u["name"])):
        drv = "driver" if has_driver(u) else "-"
        s = u["stats"]
        print(f"{u['cost']}  {u['name']:<14} {drv:<7} {u['roleName']:<18} {u['objective']:<8} "
              f"{'/'.join(u['traits']):<32} hp {s['hp']:.0f} ad {s['ad']:.0f} as {s['as']:.2f} "
              f"mana {s['initialMana']:.0f}/{s['mana']:.0f} range {s['range']:.0f}"
              f"  cast {u.get('castTime') if u.get('castTime') is not None else '-'}")


def cmd_sim(args):
    snap = load_snapshot(args.set, args.patch)
    unit = snap.unit(args.name)
    items = [snap.item(x)["api"] for x in args.items]
    item_fx = load_item_effects(snap.set_no)
    trait_fx = load_trait_effects(snap.set_no)
    contexts, unmodeled = unit_trait_contexts(snap, unit, trait_fx)
    ctx = contexts[args.traits]
    dummy = dummies_for(snap, threat=getattr(args, "threat", "mixed")
                        if unit["objective"] == "tank" else None)
    spec = cell_spec(snap, unit, args.star, args.geometry, ctx, dummy, args.duration, None,
                     item_fx, trait_fx, items=items)
    fx = engine().compose_fx(spec)
    sheet, res = engine().simulate(spec, bool(getattr(args, "trace", False)))
    print(f"{unit['name']} {args.star}★ with {', '.join(snap.items[a]['name'] for a in items) or 'no items'}"
          f" · {args.geometry} · traits {args.traits}"
          + (f" ({', '.join(snap.traits[a]['name'] + ' ' + str(snap.traits[a]['levels'][c-1]) for a, c in ctx)})" if ctx else ""))
    print(f"  {unit['roleName']} → {unit['objective']} fight ({OBJECTIVES[unit['objective']]})"
          + (f" · form {sheet['form']}" if sheet["form"] else ""))
    mana_start = min(sheet["manaStart"], sheet["manaMax"]) if sheet["manaMax"] else sheet["manaStart"]
    per_attack = (ROLE_MANA.get(unit_role_kind(unit), 0) + fx["manaPerAttack"]
                  + fx["manaPerCrit"] * sheet["crit"]) * fx["manaMult"]
    print(f"  AD {sheet['ad']:.1f}  AP {sheet['ap']:.0f}  AS {sheet['as']:.2f}  "
          f"crit {sheet['crit']*100:.0f}% ×{sheet['critMult']:.2f}  precision {sheet['precision']}  "
          f"mana {mana_start:.0f}/{sheet['manaMax']:.0f} +{per_attack:.1f}/attack"
          f" +{fx['manaRegen'] * fx['manaMult']:.1f}/s"
          f"  HP {sheet['hp']:.0f} armor {sheet['armor']:.0f} MR {sheet['mr']:.0f} "
          f"durability {sheet['durability']*100:.0f}% omnivamp {sheet['omnivamp']*100:.0f}%")
    print("  dummies: " + "; ".join(f"{s['hp']} HP / {s['armor']} armor / {s['mr']} MR ({s['kind']})"
                                    for s in dummy["slots"])
          + f" — median {dummy['star']}★ of {dummy['tanks']} tanks and {dummy['others']} others")
    if unit["objective"] in PRESSURED:
        board = unit["objective"] == "tank"
        streams = dummy["board"] if board else [1] * dummy["count"]
        print("  they hit back: " + "; ".join(
            f"{k}× {s['ad']:.1f} AD at {s['as']:.2f}/s, {s['ability']:.1f} per cast "
            + (f"every {s['castInterval']:g}s" if board else
               f"at {s['manaStart']:.0f}/{s['manaMax']:.0f} mana +{s['manaPerAttack']:.0f}/attack"
               f"{' + damage taken' if s['manaFromDamage'] else ''}")
            + f" ({s['physicalShare']*100:.0f}% physical)"
            for s, k in zip(dummy["slots"], streams))
            + f" — about {dummy['boardPressureDps'] if board else dummy['pressureDps']:.0f} pre-mitigation DPS"
            + (f" ({dummy['threat']['label']}, {dummy['boardSize']} attackers)" if board else ""))
        if board:
            debuffs = dummy["enemyDebuffs"]
            print(f"  continuous enemy debuffs: {debuffs['wound']:.0%} heal cut, "
                  f"{debuffs['sunder']:.0%} Sunder, {debuffs['shred']:.0%} Shred; "
                  f"opening EHP {sheet['physicalEhp']:.0f} physical / {sheet['magicEhp']:.0f} magic")
    kt = f"all dead at {res['killTime']:.2f}s" if res["killTime"] is not None else f"survive ({[round(x) for x in res['left']]} HP left)"
    print(f"  {res['total']:.0f} damage, {res['dps']:.0f} DPS, {res['attacks']} attacks, {res['casts']} casts — {kt}")
    for src, v in sorted(res["breakdown"].items(), key=lambda kv: -kv[1]):
        print(f"    {src:<12} {v:8.0f}  {v / max(res['total'], 1e-9) * 100:5.1f}%")
    if unit["objective"] in PRESSURED:
        fate = f"died at {res['diedAt']:.2f}s" if res["died"] else f"alive with {res['hpLeft']:.0f} HP"
        print(f"  {fate}; held the dummies {res['aliveTime']:.2f}s{'+' if res['survivalCapped'] else ''}; took {res['absorbed']:.0f} pre-mitigation "
              f"({res['taken']:.0f} after resists/durability, {res['shielded']:.0f} on shields), healed {res['healed']:.0f}, "
              f"denied {res['denied']:.0f} by CC/untargetability ({res['ccTime']:.1f} stun-seconds)"
              + (f", allies healed {res['allyHeal']:.0f}" if res["allyHeal"] else "")
              + (f", allies shielded {res['allyShield']:.0f}" if res["allyShield"] else ""))
        if res["stressAliveTime"] is not None:
            print(f"  double incoming damage: held {res['stressAliveTime']:.2f}s"
                  + ("+ (survives both tests; tied)" if res["stressCapped"] else ""))
    if unmodeled or fx["notes"]:
        print("  not modeled: " + "; ".join(unmodeled + fx["notes"]))
    if res.get("trace"):
        print("  timeline:")
        for t, kind, amount, target, src, hp in res["trace"]:
            where = f" → #{target}" if target >= 0 else ""
            what = f" {amount:.1f}" if kind in ("damage", "take", "heal", "shield") else (f" (mana {amount:.0f})" if kind == "cast" else "")
            print(f"    {t:6.2f}s {kind:<7}{what}{where} {src}  hp {hp:.0f}")


def cmd_top(args):
    snap = load_snapshot(args.set, args.patch)
    unit = snap.unit(args.name)
    item_fx = load_item_effects(snap.set_no)
    trait_fx = load_trait_effects(snap.set_no)
    contexts, unmodeled = unit_trait_contexts(snap, unit, trait_fx)
    pool = pool_items(snap, item_fx)
    dummy = dummies_for(snap, threat=getattr(args, "threat", "mixed")
                        if unit["objective"] == "tank" else None)
    t0 = time.time()
    out, count = enumerate_builds(snap, unit, args.star, args.geometry,
                                  contexts[args.traits], dummy, pool, args.duration)
    print(f"{unit['name']} {args.star}★ · {args.geometry} · traits {args.traits} · "
          f"{unit['roleName']} → {unit['objective']} fight: {count:,} builds in {time.time() - t0:.1f}s")
    if unit["objective"] == "tank":
        print(f"  {dummy['threat']['label']} · {dummy['boardSize']} attackers · "
              f"{dummy['boardPressureDps']:.0f} pre-mitigation DPS · continuous heal cut, Sunder and Shred")
    previous_key, previous_rank = None, None
    for n, (combo, sheet, res) in enumerate(out[:args.top], 1):
        key = rank_key(res, unit["objective"])
        rank = previous_rank if unit["objective"] == "tank" and key == previous_key else n
        previous_key, previous_rank = key, rank
        kt = f"{res['killTime']:.2f}s" if res["killTime"] is not None else "survive"
        held = (f"  held {res['aliveTime']:5.1f}s{'+' if res['survivalCapped'] else ''}"
                if unit["objective"] in PRESSURED else "")
        if res["stressAliveTime"] is not None:
            held += f" · 2× damage {res['stressAliveTime']:.1f}s{'+' if res['stressCapped'] else ''}"
        print(f"{rank:3} {', '.join(snap.items[a]['name'] for a in combo):<60} {kt:>8}  "
              f"{res['total']:7.0f} dmg  {res['dps']:5.0f} dps  {res['casts']} casts{held}")


def cmd_warm(args):
    n = warm(only=args.unit)
    if n is None:
        print("Another warm is already running (it holds .cache/tft/lock).")
    elif n == 0:
        print("Nothing to do — every cell is computed for the current code and data.")
    else:
        print(f"Computed {n} cell(s).")
