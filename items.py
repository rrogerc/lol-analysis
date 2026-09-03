"""Item data for build math: per-patch snapshots of static item stats.

`lol.py items fetch` archives three sources into data/items/<patch>/:

- ddragon.json — Riot's Data Dragon item.json, filtered to Summoner's Rift
  items. Canonical and patch-versioned, but its structured stats block omits
  lethality, % armor pen, ability haste and crit damage (they only appear in
  the HTML description text).
- meraki.json — Meraki Analytics items.json, same item ids with every stat
  fully structured (plus passives/actives with numbers). Meraki only serves
  the latest patch, so the committed snapshot here IS the historical archive.
- groups.json — Riot's shop rules, distilled from the raw item bin (via
  CommunityDragon) plus ddragon's purchasable flags: each SR item's ownership
  groups with per-group caps ("Limited to 1"), currency-gated item ids, and
  retired-from-shop ids.

No DB tables yet; the build-math layer will define its own shape on top of
these raw snapshots.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from common import DATA_DIR, patch_key

ITEMS_DATA_DIR = os.path.join(DATA_DIR, "items")

DDRAGON_VERSIONS = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_ITEMS = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json"
MERAKI_ITEMS = "https://cdn.merakianalytics.com/riot/lol/resources/latest/en-US/items.json"
# Riot's own item bin, via CommunityDragon. The ONLY machine-readable source
# for the in-game "Limited to 1 <group> item" rules: each item lists its
# mItemGroups, and each group record carries mMaxGroupOwnable. ddragon
# publishes the group definitions but tags no items with them, and meraki
# doesn't expose them at all — both are useless for this.
CDRAGON_ITEM_BIN = "https://raw.communitydragon.org/{patch}/game/items.cdtb.bin.json"


def item_groups(bin_data, sr_items):
    """Distil the 16MB item bin down to what the build math needs:
    {"maxOwnable": {group: n}, "items": {id: [group, ...]},
     "currencyGated": {id: currency}}.

    `maxOwnable`/`items` are the in-game "Limited to 1 <group> item" rules.
    Group names are sometimes unresolved hashes like '{cf1a44c0}' — fine,
    only identity matters for exclusivity.

    `currencyGated` flags items you cannot simply buy with gold: they need a
    non-gold currency, which on Summoner's Rift means the Feats of Strength
    boot upgrades (dead since V26.01 removed Feats, though the requirement
    still sits in the data) and the support-quest line. ddragon still lists
    them as purchasable map-11 items, so this is the only reliable way to
    keep them out of a pool of freely-buyable items.

    `retired` flags items ddragon marks unpurchasable that STILL carry a
    recipe — a live item always sells, so this combination means the item
    was pulled from the shop and its data left behind (Opportunity,
    Trailblazer). Items that are unpurchasable with NO recipe are the
    transformations (Seraph's Embrace, Muramana) and stay legal."""
    caps = {k: v["mMaxGroupOwnable"] for k, v in bin_data.items()
            if isinstance(v, dict) and v.get("__type") == "ItemGroup"
            and isinstance(v.get("mMaxGroupOwnable"), int)
            and v["mMaxGroupOwnable"] >= 1}
    items, gated = {}, {}
    for v in bin_data.values():
        if not (isinstance(v, dict) and "itemID" in v):
            continue
        iid = str(v["itemID"])
        if iid not in sr_items:
            continue
        gs = [g for g in v.get("mItemGroups", []) if g in caps]
        if gs:
            items[iid] = sorted(gs)
        if v.get("mRequiredBuffCurrencyName"):
            gated[iid] = v["mRequiredBuffCurrencyName"]
    retired = sorted(iid for iid, it in sr_items.items()
                     if not it["gold"]["purchasable"] and it.get("from"))
    used = {g for gs in items.values() for g in gs}
    return {"maxOwnable": {g: caps[g] for g in sorted(used)},
            "items": items, "currencyGated": gated, "retired": retired}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "lol-analysis"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def short_patch(version):
    return ".".join(version.split(".")[:2])


def resolve_version(versions, wanted):
    """Map '16.15' or '16.15.1' to the newest matching ddragon version."""
    if wanted in versions:
        return wanted
    for v in versions:  # versions.json is newest-first
        if short_patch(v) == wanted:
            return v
    sys.exit(f"No ddragon version matches '{wanted}'. Latest: {versions[:3]}")


def read_or_none(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read()


def cmd_fetch(args):
    versions = fetch_json(DDRAGON_VERSIONS)
    version = resolve_version(versions, args.version) if args.version else versions[0]
    patch = short_patch(version)
    is_latest = version == versions[0]
    out_dir = os.path.join(ITEMS_DATA_DIR, patch)
    meta_path = os.path.join(out_dir, "meta.json")

    if os.path.exists(meta_path) and not args.force:
        with open(meta_path) as f:
            meta = json.load(f)
        print(f"data/items/{patch} already fetched ({meta['fetchedAt']}, "
              f"ddragon {meta['ddragonVersion']}). Use --force to refresh.")
        return

    dd = fetch_json(DDRAGON_ITEMS.format(version=version))
    # Map 11 is Summoner's Rift. Ids >= 100000 are alternate-mode copies of
    # SR items (e.g. 323003 duplicating 3003) that flag map 11 anyway. What
    # remains still includes non-shop system entries (turret buffs, removed
    # items) — the build-math layer should treat meraki coverage as the
    # signal for "a real item".
    sr = {iid: it for iid, it in dd["data"].items()
          if it.get("maps", {}).get("11") and int(iid) < 100000}

    meraki, meraki_note = {}, None
    if is_latest:
        try:
            mk = fetch_json(MERAKI_ITEMS)
            meraki = {iid: it for iid, it in mk.items() if iid in sr}
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            meraki_note = f"meraki fetch failed: {e}"
    else:
        meraki_note = "meraki serves only the latest patch; older snapshot has ddragon only"
    if meraki_note:
        print(f"Warning: {meraki_note}")

    groups, groups_note = None, None
    try:
        groups = item_groups(fetch_json(CDRAGON_ITEM_BIN.format(patch=patch)), sr)
    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
        groups_note = f"item-group fetch failed: {e}"
        print(f"Warning: {groups_note} — build exclusivity will fall back to "
              f"the previous snapshot")

    # Write only when upstream actually changed, so a daily --force run (the
    # refresh workflow) leaves the tree untouched — and commits nothing —
    # until a new patch or a meraki correction lands. fetchedAt therefore
    # means "when this snapshot last changed".
    new_files = {"ddragon.json": json.dumps(sr, separators=(",", ":"))}
    if meraki:
        new_files["meraki.json"] = json.dumps(meraki, separators=(",", ":"))
    if groups:
        new_files["groups.json"] = json.dumps(groups, separators=(",", ":"))
    if os.path.exists(meta_path) and all(
            read_or_none(os.path.join(out_dir, name)) == body
            for name, body in new_files.items()):
        with open(meta_path) as f:
            prev = json.load(f)
        print(f"data/items/{patch}: unchanged upstream "
              f"(snapshot from {prev['fetchedAt']}).")
        return

    os.makedirs(out_dir, exist_ok=True)
    for name, body in new_files.items():
        with open(os.path.join(out_dir, name), "w") as f:
            f.write(body)
    meta = {
        "patch": patch,
        "ddragonVersion": version,
        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ddragonItems": len(sr),
        "merakiItems": len(meraki),
        "groupedItems": len(groups["items"]) if groups else 0,
        "sources": {"ddragon": DDRAGON_ITEMS.format(version=version),
                    "meraki": MERAKI_ITEMS if meraki else None,
                    "itemGroups": (CDRAGON_ITEM_BIN.format(patch=patch)
                                   if groups else None)},
    }
    if meraki_note:
        meta["note"] = meraki_note
    if groups_note:
        meta["groupsNote"] = groups_note
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    # Purchasable items meraki hasn't structured are real coverage gaps for
    # the build math; the rest of the ddragon-only set is system junk.
    gaps = sorted(sr[i]["name"] for i in sr
                  if meraki and i not in meraki and sr[i]["gold"]["purchasable"])
    print(f"data/items/{patch}: {len(sr)} SR items from ddragon {version}, "
          f"{len(meraki)} with structured stats from meraki"
          + (f", {len(groups['items'])} with ownership-limited groups."
             if groups else "."))
    if gaps:
        print(f"  {len(gaps)} purchasable without meraki stats: " + ", ".join(gaps))
    print("Commit data/items/ to archive this patch's snapshot.")


def snapshots():
    """All archived snapshot metas, oldest patch first."""
    metas = []
    if os.path.isdir(ITEMS_DATA_DIR):
        for patch in os.listdir(ITEMS_DATA_DIR):
            meta_path = os.path.join(ITEMS_DATA_DIR, patch, "meta.json")
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    metas.append(json.load(f))
    return sorted(metas, key=lambda m: patch_key(m["patch"]))


def latest_snapshot():
    metas = snapshots()
    return metas[-1] if metas else None


def cmd_status(args):
    if not os.path.isdir(ITEMS_DATA_DIR):
        print("No item snapshots yet — run `lol.py items fetch`.")
        return
    print(f"{'Patch':<8} {'ddragon ver':<12} {'Items':<7} {'Meraki':<7} {'Fetched at':<22}")
    print("-" * 58)
    for m in snapshots():
        print(f"{m['patch']:<8} {m['ddragonVersion']:<12} {m['ddragonItems']:<7} "
              f"{m['merakiItems']:<7} {m['fetchedAt']:<22}")
