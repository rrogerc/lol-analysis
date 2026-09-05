"""LoL analysis monorepo — one CLI, one SQLite DB, one web app.

Domains (each is a module with an archive under data/ and, if needed, tables in lol.db):
  scaling    champion win rates by game length from the ../lol-quant soloq crawl
  items      static item data snapshots (ddragon + meraki + item-bin shop rules)
  builds     theoretical build math: stat sheets, damage sim, build optimizer

Usage:
  .venv/bin/python lol.py scaling sync              # refresh soloq tiers from ../lol-quant
  .venv/bin/python lol.py scaling report [--scaling]
  .venv/bin/python lol.py scaling champion kayle
  .venv/bin/python lol.py items fetch               # snapshot current patch's item data
  .venv/bin/python lol.py builds warm               # precompute the dashboard's build scenarios
  .venv/bin/python lol.py serve                     # local web dashboard
  .venv/bin/python lol.py status                    # what's in the database
  .venv/bin/python lol.py import-json               # rebuild lol.db from data/
  .venv/bin/python lol.py export                    # static snapshot of the site
"""

import argparse
import os
import sys

from common import BASE_DIR
import builds
import items
import scaling
import tft
import webapp


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def slice_args(sp):
        sp.add_argument("--tier", help="tier (default: most recently imported)")
        sp.add_argument("--patches", nargs="+", help="patches to include (default: all in DB)")

    def quant_args(sp):
        sp.add_argument("--quant-dir", default=os.path.join(BASE_DIR, "..", "lol-quant"),
                        help="path to the lol-quant checkout (default: ../lol-quant)")
        sp.add_argument("--platforms", nargs="+",
                        help="riot platforms to include, e.g. kr euw1 na1 (default: all)")

    # ----- shared, whole-repo commands -----
    sp = sub.add_parser("serve", help="run the local web dashboard")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8321)
    sp.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    sp.add_argument("--no-warm", action="store_true",
                    help="don't precompute cold builds scenarios in the background")
    sp.set_defaults(func=webapp.cmd_serve)

    sp = sub.add_parser("export", help="write the web app as a self-contained static site")
    sp.add_argument("--out", default="_site")
    sp.set_defaults(func=webapp.cmd_export)

    sp = sub.add_parser("status", help="show what's in the database")
    sp.set_defaults(func=scaling.cmd_status)

    sp = sub.add_parser("import-json", help="rebuild lol.db from the data/ JSON tree")
    sp.add_argument("--data-dir", default="data")
    sp.set_defaults(func=scaling.cmd_import_json)

    # ----- scaling domain -----
    sc = sub.add_parser("scaling", help="champion scaling: win rates by game length")
    scsub = sc.add_subparsers(dest="scaling_cmd", required=True)

    sp = scsub.add_parser("report", help="ranked win-rate tables per time bucket")
    slice_args(sp)
    sp.add_argument("--lane", choices=["top", "jungle", "middle", "bottom", "support"])
    sp.add_argument("--min-games", type=int, default=1000)
    sp.add_argument("--top", "-n", type=int, default=10)
    sp.add_argument("--scaling", action="store_true",
                    help="rank by scaling (late WR minus early WR) instead")
    sp.add_argument("--csv", help="also write results to this CSV file")
    sp.set_defaults(func=scaling.cmd_report)

    sp = scsub.add_parser("champion", help="one champion's win-rate curve per lane")
    sp.add_argument("name", help="lowercase champion slug, e.g. missfortune")
    slice_args(sp)
    sp.set_defaults(func=scaling.cmd_champion)

    sp = scsub.add_parser("dashboard", help="generate a self-contained HTML dashboard")
    slice_args(sp)
    sp.add_argument("--out", default="dashboard.html")
    sp.add_argument("--min-games", type=int, default=1000,
                    help="drop champion-roles with fewer total games")
    sp.add_argument("--min-bucket-games", type=int, default=1000,
                    help="blank out chart points backed by fewer games "
                         "(1000 ~= +/-3pp at 95%%)")
    sp.set_defaults(func=scaling.cmd_dashboard)

    sp = scsub.add_parser("sync",
                          help="refresh all three soloq tiers from ../lol-quant in one go")
    quant_args(sp)
    sp.add_argument("--min-champ-games", type=int, default=20,
                    help="soloq_mastery threshold: season games on the champion (default 20)")
    sp.add_argument("--otp-share", type=float, default=80,
                    help="soloq_otp threshold: champion's share of the player's "
                         "role games in %% (default 80)")
    sp.add_argument("--db-only", action="store_true",
                    help="refresh lol.db but leave the data/scaling JSON archive "
                         "untouched (what the jobs/sync-scaling.sh timer runs)")
    sp.set_defaults(func=scaling.cmd_sync)

    sp = scsub.add_parser("import-soloq",
                          help="aggregate lol-quant's Riot soloq parquet into the DB")
    quant_args(sp)
    sp.add_argument("--tier",
                    help="tier name to store the data under "
                         "(default: soloq_masters_plus, or soloq_mastery with --min-champ-games)")
    sp.add_argument("--min-lane-rate", type=float, default=10,
                    help="keep a lane if its play rate exceeds this %% (default 10)")
    sp.add_argument("--min-champ-games", type=int, default=0,
                    help="only count games where the player has at least this many "
                         "season games on the champion (default: off)")
    sp.add_argument("--otp-share", type=float, default=0,
                    help="one-trick filter: champion must be at least this %% of the "
                         "player's games in the role (e.g. 80); implies a 20-game "
                         "champion floor unless --min-champ-games is set")
    sp.add_argument("--db-only", action="store_true",
                    help="refresh lol.db but leave the data/scaling JSON archive untouched")
    sp.set_defaults(func=scaling.cmd_import_soloq)

    # ----- items domain -----
    it = sub.add_parser("items", help="static item data snapshots (for build math)")
    itsub = it.add_subparsers(dest="items_cmd", required=True)

    sp = itsub.add_parser("fetch",
                          help="snapshot the current patch's item data into data/items/")
    sp.add_argument("--version",
                    help="ddragon version or short patch to fetch instead of the "
                         "latest, e.g. 16.15 (older patches get ddragon only — "
                         "meraki has no history)")
    sp.add_argument("--force", action="store_true",
                    help="refetch even if this patch is already archived")
    sp.set_defaults(func=items.cmd_fetch)

    sp = itsub.add_parser("status", help="list archived item snapshots")
    sp.set_defaults(func=items.cmd_status)

    # ----- builds domain -----
    bd = sub.add_parser("builds", help="theoretical build math: stat sheets, damage sim")
    bdsub = bd.add_subparsers(dest="builds_cmd", required=True)

    sp = bdsub.add_parser("fetch-champion",
                          help="snapshot a champion's static data into data/builds/champions/")
    sp.add_argument("name", help="champion name or slug, e.g. vladimir")
    sp.add_argument("--version",
                    help="ddragon version or short patch to fetch (default: latest)")
    sp.add_argument("--force", action="store_true",
                    help="refetch even if this patch is already archived")
    sp.set_defaults(func=builds.cmd_fetch_champion)

    sp = bdsub.add_parser("stats",
                          help="exact stat sheet for a champion + item build")
    sp.add_argument("name", help="champion name or slug, e.g. vladimir")
    sp.add_argument("--level", type=int, default=18)
    sp.add_argument("--items", nargs="*", default=[],
                    help="item names, meraki nicknames, or ids")
    sp.add_argument("--patch", help="patch to use (default: newest snapshots)")
    sp.set_defaults(func=builds.cmd_stats)

    sp = bdsub.add_parser("items", help="search the purchasable item pool")
    sp.add_argument("query", nargs="?", default="")
    sp.set_defaults(func=builds.cmd_items)

    def sim_args(sp):
        sp.add_argument("name",
                        help="champion with a kit encoding: kayle, vladimir")
        sp.add_argument("--level", type=int, default=16)
        sp.add_argument("--patch", help="patch to use (default: newest snapshots)")
        sp.add_argument("--target-hp", type=int, default=2800)
        sp.add_argument("--target-bonus-hp", type=float, default=0,
                        help="item/rune share of target HP (Giant Slayer)")
        sp.add_argument("--armor", type=float, default=80)
        sp.add_argument("--mr", type=float, default=60)
        sp.add_argument("--duration", type=float, default=8,
                        help="fight length in seconds (default 8)")
        sp.add_argument("--no-ult", action="store_true", help="don't cast R")
        sp.add_argument("--prestacked", action="store_true",
                        help="start with passive stacks already up")
        sp.add_argument("--max-order",
                        help="ability max order, e.g. Q,E,W (default: the kit's)")

    sp = bdsub.add_parser("sim",
                          help="simulate damage vs a stat dummy for one build")
    sim_args(sp)
    sp.add_argument("--items", nargs="*", default=[],
                    help="item names, meraki nicknames, or ids")
    sp.set_defaults(func=builds.cmd_sim)

    sp = bdsub.add_parser("warm",
                          help="precompute every dashboard scenario that is cold, cheapest first")
    sp.set_defaults(func=builds.cmd_warm)

    sp = bdsub.add_parser("optimize",
                          help="enumerate and rank builds for one scenario")
    sim_args(sp)
    sp.add_argument("--slots", type=int, default=6,
                    help="build size incl. boots (default 6)")
    sp.add_argument("--budget", type=int,
                    help="gold cap; also allows smaller builds")
    sp.add_argument("--require", nargs="*",
                    help="items every build must contain")
    sp.add_argument("--pool", nargs="*",
                    help="candidate items (default: the modeled damage pool)")
    sp.add_argument("--top", type=int, default=15)
    sp.set_defaults(func=builds.cmd_optimize)

    # ----- tft domain -----
    tf = sub.add_parser("tft", help="Teamfight Tactics: optimal items per unit, from first principles")
    tfsub = tf.add_subparsers(dest="tft_cmd", required=True)

    def tft_args(sp):
        sp.add_argument("--set", type=int, default=tft.DEFAULT_SET, help="TFT set number (default 18)")
        sp.add_argument("--patch", help="archived patch to use (default: newest)")

    sp = tfsub.add_parser("fetch", help="snapshot the set's unit/item/trait data for a patch")
    sp.add_argument("--set", type=int, default=tft.DEFAULT_SET)
    sp.add_argument("--patch", help="TFT patch label, e.g. 18.1 (default: newest patch notes)")
    sp.add_argument("--force", action="store_true", help="refetch even if archived")
    sp.set_defaults(func=tft.cmd_fetch)

    sp = tfsub.add_parser("status", help="list archived snapshots")
    sp.set_defaults(func=tft.cmd_status)

    sp = tfsub.add_parser("check", help="compare the snapshot with the patch notes' changes")
    tft_args(sp)
    sp.set_defaults(func=tft.cmd_check)

    sp = tfsub.add_parser("units", help="list the set's units and which have a driver")
    tft_args(sp)
    sp.set_defaults(func=tft.cmd_units)

    def fight_args(sp):
        sp.add_argument("name", help="unit name, e.g. ashe")
        sp.add_argument("--star", type=int, default=2, choices=(1, 2, 3, 4))
        sp.add_argument("--geometry", default="clump", choices=list(tft.GEOMETRIES))
        sp.add_argument("--traits", default="bare", choices=list(tft.TRAIT_CONTEXTS))
        sp.add_argument("--duration", type=float, default=None,
                        help="fight length in seconds (default: 20, tanks 60)")
        tft_args(sp)

    sp = tfsub.add_parser("sim", help="simulate one build against the dummies")
    fight_args(sp)
    sp.add_argument("--items", nargs="*", default=[], help="item names, e.g. 'Infinity Edge'")
    sp.add_argument("--trace", action="store_true", help="print the fight's timeline")
    sp.set_defaults(func=tft.cmd_sim)

    sp = tfsub.add_parser("top", help="rank every 3-item build for one unit and scenario")
    fight_args(sp)
    sp.add_argument("--top", type=int, default=15)
    sp.set_defaults(func=tft.cmd_top)

    sp = tfsub.add_parser("warm", help="precompute every dashboard cell that is cold")
    sp.add_argument("--unit", help="only this unit slug")
    sp.set_defaults(func=tft.cmd_warm)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except ModuleNotFoundError as e:
        sys.exit(f"Missing dependency: {e.name}. Run with the project venv:\n"
                 "  .venv/bin/python lol.py ...")
