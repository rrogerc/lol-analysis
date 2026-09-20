"""The Builds tab's leaderboard: every champion's best build under one
scenario, ranked against each other.

Read-only over the precomputed cells — builds.warm computes them, nothing here
simulates. The top row of a champion's cell IS its best build under that
scenario's ranking, so comparing champions needs nothing a cell does not
already hold. That is also why this is not in builds.py: that file's bytes are
part of every cell's cache key, and an edit there recomputes the whole roster
(about two hours).
"""

import json
import os
from functools import lru_cache

import builds

INF = float("inf")


def leaderboard_scenarios():
    """The scenarios with a leaderboard, in SCENARIOS order: the damage tiers'.
    The Survival tier ranks a single tank so far, which compares nothing."""
    return [k for k, sc in builds.SCENARIOS.items()
            if builds.tier_objective(sc["tier"]) == "damage"]


def rank_key(row, scenario):
    """Sort key of a champion's best build, best first, on the numbers its own
    table shows (a cell keeps them rounded). Against one target: the expected
    kill time, and for a champion that never kills it the most damage, as
    builds.rank_key orders builds. Overall: champions that kill every target
    before those that leave one standing, then the geometric mean of the kill
    times, as builds.overall_key does. What orders one champion's builds
    inside an attack tick (the interpolated time: damage to spare) is left
    out: across champions an equal time is an equal result."""
    if scenario.get("overall"):
        unkilled = len(row["vs"]) - row["kills"]
        return (unkilled, row["mean"] if row["mean"] is not None else INF)
    if row["ttk"] is not None:
        return (0, row["ttkExp"])
    return (1, -row["total"])


@lru_cache(maxsize=2048)
def _cell_best(path, mtime_ns, size):
    """What the leaderboard keeps of one version of a cell file: its top row
    (a cell is half a megabyte of rows). The file's metadata is part of the
    key, so a recomputed cell is read again; callers must not modify the
    result."""
    with open(path) as f:
        cell = json.load(f)
    rows = cell.get("rows") or []
    return {"row": rows[0] if rows else None, "error": cell.get("error"),
            "kitPatch": cell.get("kitPatch"),
            "buildsEvaluated": cell.get("buildsEvaluated"),
            "computedAt": cell.get("computedAt")}


def cached_leaderboard(key, paths=None):
    """One row per champion of the scenario's tier: the top row of its cell,
    ranked best first, equal results sharing a rank. Never computes: a
    champion whose cell is cold is listed under `pending` (the ranks are
    provisional until `complete`), one whose machine-written driver failed in
    the enumeration under `failed`."""
    if key not in leaderboard_scenarios():
        raise ValueError(f"unknown leaderboard scenario '{key}'")
    paths = paths if paths is not None else builds.cell_paths()
    sc = builds.SCENARIOS[key]
    scenario = {"key": key, **sc,
                "targets": [{"key": k, **builds.SCENARIOS[k]}
                            for k in builds.tier_targets(sc["tier"])]}
    rows, pending, failed = [], [], []
    champions = builds.tier_champions(sc["tier"])
    for slug in champions:
        kit = builds.load_kit(slug)
        who = {"champion": slug, "championName": kit.get("name", slug),
               # a machine-written kit and driver (jobs/kit_driver.py), and
               # whether a person has been through it: as api_builds_meta
               "generated": bool(kit.get("generated")),
               "reviewed": bool(kit.get("reviewed", not kit.get("generated")))}
        path = paths[(slug, key)]
        try:
            st = os.stat(path)
        except FileNotFoundError:
            pending.append(who)
            continue
        best = _cell_best(path, st.st_mtime_ns, st.st_size)
        if best["row"] is None:
            failed.append({**who, "error": best["error"] or "no build was ranked"})
            continue
        rows.append({**best["row"], **who, "kitPatch": best["kitPatch"],
                     "buildsEvaluated": best["buildsEvaluated"],
                     "computedAt": best["computedAt"]})
    rows.sort(key=lambda r: (rank_key(r, scenario), r["championName"], r["champion"]))
    previous, rank = None, 0
    for n, row in enumerate(rows, 1):
        score = rank_key(row, scenario)
        if score != previous:
            rank = n
        row["rank"] = rank  # among champions; the cell's own was 1
        previous = score
    return {"scenario": scenario, "complete": not pending,
            "expectedCount": len(champions),
            "readyCount": len(champions) - len(pending),
            "pending": pending, "failed": failed, "rows": rows}
