#!/usr/bin/env python3
"""Re-rank a published composition artifact under the removal-aware objective.

Read-only. It takes the boards a published `ehp-damage-capacity-v3` artifact
already contains and scores each one again with `tft_removal`, which fights it
against the synthetic reference opponents through the two-sided engine. That
answers the only question worth asking before paying for a rebuild: does
making damage kill something change the ranking, and by how much.

    python3 jobs/tft_removal_compare.py .cache/tft-comps/c2-clump-mixed-<rev>.json
    python3 jobs/tft_removal_compare.py <artifact> --budget 9 --structure single

It does NOT re-run the search. Every board here was found under the published
objective, so a carry the old model never tried is not in the file; the
comparison bounds how much the ranking moves, it does not find the new winner.
"""
import argparse
import collections
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tft                                                          # noqa: E402
import tft_removal                                                  # noqa: E402
from tft_comp_traits import resolve_board_traits                    # noqa: E402


def boards_of(artifact, budget, structure):
    results = artifact["results"]
    budgets = [str(budget)] if budget else sorted(results, key=int)
    for key in budgets:
        for name, rows in sorted(results[key].items()):
            if structure and name != structure:
                continue
            for row in rows:
                yield key, name, row


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("artifact")
    parser.add_argument("--budget", type=int, default=9, help="item budget (0 = every one)")
    parser.add_argument("--structure", default=None, help="single/duoCarry/duoTank/both")
    parser.add_argument("--limit", type=int, default=0, help="score at most this many boards")
    parser.add_argument("--out", default=None, help="write the full comparison as JSON")
    args = parser.parse_args()

    artifact = json.load(open(args.artifact))
    snap = tft.load_snapshot(artifact["set"])
    geometry = artifact["geometry"]
    evaluator = tft_removal.Evaluator(snap, geometry, artifact["threat"], prepared=True)
    print(f"{artifact['key']} · {artifact['evaluationModel']} → {tft_removal.MODEL}")
    for board in evaluator.boards:
        print(f"  opponent {board['opponentId']}: {board['label']}")
    print()

    rows = list(boards_of(artifact, args.budget, args.structure))
    if args.limit:
        rows = rows[:args.limit]
    started = time.perf_counter()
    scored = []
    for budget, structure, row in rows:
        members = [{"api": unit["api"], "star": unit["star"]} for unit in row["units"]]
        resolved = resolve_board_traits(snap, members, None)
        selected = {unit["api"]: {"items": list(unit["itemApis"]), "alpha": False}
                    for unit in row["units"]}
        carry = next(u["api"] for u in row["units"] if u["slug"] == row["mainCarry"])
        tank = next(u["api"] for u in row["units"] if u["slug"] == row["mainTank"])
        out = evaluator.evaluate(members, resolved["effects"], selected, carry, tank)
        scored.append({"budget": budget, "structure": structure, "id": row["id"],
                       "mainCarry": row["mainCarry"], "mainTank": row["mainTank"],
                       "publishedScore": row["metrics"]["theoryScore"],
                       "publishedRank": row["rank"],
                       "removalScore": out["metrics"]["removalScore"],
                       "clearTime": out["metrics"]["removalClearTime"],
                       "boardsCleared": out["metrics"]["boardsCleared"],
                       "survivors": out["metrics"]["survivors"]})
    elapsed = time.perf_counter() - started
    fights = evaluator.stats["sharedFightsSimulated"]
    print(f"{len(scored)} boards, {fights} fights, {elapsed:.1f}s "
          f"({elapsed / max(fights, 1) * 1000:.2f} ms/fight)\n")

    published = sorted(scored, key=lambda row: -row["publishedScore"])
    removal = sorted(scored, key=lambda row: (-row["removalScore"], row["id"]))
    place = {row["id"]: index for index, row in enumerate(removal, 1)}
    print(f"{'#':>3} {'carry':10s} {'tank':11s} {'published':>11s} "
          f"{'clear':>7s} {'cleared':>8s} {'left':>5s} {'new #':>6s}")
    for index, row in enumerate(published[:20], 1):
        print(f"{index:>3} {row['mainCarry']:10s} {row['mainTank']:11s} "
              f"{row['publishedScore']:11.3g} {row['clearTime']:7.2f} "
              f"{row['boardsCleared']:8.2f} {row['survivors']:5.1f} {place[row['id']]:>6}")

    def carries(order):
        counts = collections.Counter(row["mainCarry"] for row in order[:len(order) // 4 or 1])
        return ", ".join(f"{name} {count}" for name, count in counts.most_common(5))
    print(f"\ntop quarter by the published score: {carries(published)}")
    print(f"top quarter by removal:             {carries(removal)}")
    best = removal[0]
    print(f"\nbest board under removal: {best['mainCarry']} / {best['mainTank']} — "
          f"clears in {best['clearTime']:.2f}s, "
          f"{best['boardsCleared']:.0%} of the reference opponents, "
          f"was #{best['publishedRank']} of its own group")
    if args.out:
        json.dump({"artifact": os.path.basename(args.artifact), "model": tft_removal.MODEL,
                   "opponents": [b["opponentId"] for b in evaluator.boards], "boards": scored},
                  open(args.out, "w"), indent=1)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
