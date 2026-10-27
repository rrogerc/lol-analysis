"""LoL and TFT analysis CLI: one entrypoint, one SQLite DB, one web app.

Packages:
  lol/   Summoner's Rift scaling, Master+ one-tricks, item snapshots, and Rust build sim
  tft/   Teamfight Tactics unit simulator, early-game planner, trait flex solver, and publisher
  web/   HTTP server, static site exporter, frontend assets, and systemd timer scripts

Usage:
  python3 cli.py scaling sync              # refresh soloq tiers from ../lol-quant
  python3 cli.py scaling report [--scaling]
  python3 cli.py scaling champion kayle
  python3 cli.py onetricks sync            # count one-tricks from ../lol-quant
  python3 cli.py matchups sync             # sync thematic matchups & archetypes
  python3 cli.py items fetch               # snapshot current patch's item data
  python3 cli.py builds warm               # precompute LoL build scenarios
  python3 cli.py tft refresh               # fetch patch, warm TFT, publish site bundle
  python3 cli.py serve                     # local web dashboard
  python3 cli.py status                    # what is in lol.db
  python3 cli.py import-json               # rebuild lol.db from lol/data/
  python3 cli.py export                    # static snapshot of the site
"""

import argparse
import os
import sys

from lol import builds, items, ladder, matchups
from tft import flex as tft_flex, sim as tft
from web import server
from web.db import BASE_DIR


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
    sp.add_argument("--no-open", action="store_true", help="do not auto-open the browser")
    sp.add_argument("--no-warm", action="store_true",
                    help="do not precompute cold LoL build scenarios in the background; TFT uses the refresh timer")
    sp.set_defaults(func=server.cmd_serve)

    sp = sub.add_parser("export", help="write the web app as a self-contained static site")
    sp.add_argument("--out", default="_site")
    sp.set_defaults(func=server.cmd_export)

    sp = sub.add_parser("status", help="show what is in the database")
    sp.set_defaults(func=ladder.cmd_status)

    sp = sub.add_parser("import-json", help="rebuild lol.db from the lol/data/ JSON tree")
    sp.add_argument("--data-dir", default="lol/data")
    sp.set_defaults(func=ladder.cmd_import_json)

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
    sp.set_defaults(func=ladder.cmd_report)

    sp = scsub.add_parser("champion", help="one champion's win-rate curve per lane")
    sp.add_argument("name", help="lowercase champion slug, e.g. missfortune")
    slice_args(sp)
    sp.set_defaults(func=ladder.cmd_champion)

    sp = scsub.add_parser("sync",
                          help="refresh all three soloq tiers from ../lol-quant in one go")
    quant_args(sp)
    sp.add_argument("--min-champ-games", type=int, default=20,
                    help="soloq_mastery threshold: season games on the champion (default 20)")
    sp.add_argument("--otp-share", type=float, default=80,
                    help="soloq_otp threshold: champion's share of the player's ranked solo games in %% (default 80)")
    sp.add_argument("--db-only", action="store_true",
                    help="refresh lol.db but leave the lol/data/scaling JSON archive untouched (used by web/ops/sync-scaling.sh)")
    sp.set_defaults(func=ladder.cmd_sync)

    sp = scsub.add_parser("import-soloq",
                          help="aggregate lol-quant's Riot soloq parquet into the DB")
    quant_args(sp)
    sp.add_argument("--tier",
                    help="tier name to store the data under (default: soloq_masters_plus, or soloq_mastery with --min-champ-games)")
    sp.add_argument("--min-lane-rate", type=float, default=10,
                    help="keep a lane if its play rate exceeds this %% (default 10)")
    sp.add_argument("--min-champ-games", type=int, default=0,
                    help="only count games where the player has at least this many season games on the champion (default: off)")
    sp.add_argument("--otp-share", type=float, default=0,
                    help="one-trick filter: champion must be at least this %% of the player's ranked solo games (e.g. 80)")
    sp.add_argument("--db-only", action="store_true",
                    help="refresh lol.db but leave the lol/data/scaling JSON archive untouched")
    sp.set_defaults(func=ladder.cmd_import_soloq)

    # ----- onetricks domain -----
    ot = sub.add_parser("onetricks", help="one-tricks per champion on the Master+ ladder")
    otsub = ot.add_subparsers(dest="onetricks_cmd", required=True)

    sp = otsub.add_parser("sync", help="count one-tricks from ../lol-quant into the DB")
    quant_args(sp)
    sp.set_defaults(func=ladder.cmd_onetricks_sync)

    # ----- matchups domain -----
    mu = sub.add_parser("matchups", help="thematic counters, synergies, and draft archetypes")
    musub = mu.add_subparsers(dest="matchups_cmd", required=True)

    sp = musub.add_parser("sync", help="build matchups and archetypes from ../lol-quant")
    quant_args(sp)
    sp.add_argument("--shards-per-region", type=int, default=400,
                    help="recent participant parquet shards per region for telemetry traits (default 400)")
    sp.add_argument("--db-only", action="store_true",
                    help="refresh lol.db without rewriting lol/data/matchups/matchups.json")
    sp.set_defaults(func=matchups.cmd_sync)

    sp = musub.add_parser("archetypes", help="list the unsupervised draft archetypes")
    sp.set_defaults(func=matchups.cmd_archetypes)

    sp = musub.add_parser("champion", help="inspect one champion's draft identity")
    sp.add_argument("name", help="champion slug, e.g. yasuo or malphite")
    sp.add_argument("--lane", choices=["top", "jungle", "middle", "bottom", "support"])
    sp.set_defaults(func=matchups.cmd_champion)

    # ----- items domain -----
    it = sub.add_parser("items", help="static item data snapshots (for build math)")
    itsub = it.add_subparsers(dest="items_cmd", required=True)

    sp = itsub.add_parser("fetch",
                          help="snapshot the current patch's item data into lol/data/items/")
    sp.add_argument("--version",
                    help="ddragon version or short patch to fetch instead of the latest, e.g. 16.15")
    sp.add_argument("--force", action="store_true",
                    help="refetch even if this patch is already archived")
    sp.set_defaults(func=items.cmd_fetch)

    sp = itsub.add_parser("status", help="list archived item snapshots")
    sp.set_defaults(func=items.cmd_status)

    # ----- builds domain -----
    bd = sub.add_parser("builds", help="theoretical build math: stat sheets, damage sim")
    bdsub = bd.add_subparsers(dest="builds_cmd", required=True)

    sp = bdsub.add_parser("fetch-champion",
                          help="snapshot a champion's static data into lol/data/builds/champions/")
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
                        help="champion with a kit encoding, e.g. kayle, vladimir, twitch, kassadin")
        sp.add_argument("--level", type=int, default=16)
        sp.add_argument("--patch", help="patch to use (default: newest snapshots)")
        sp.add_argument("--target-hp", type=int, default=2800)
        sp.add_argument("--target-bonus-hp", type=float, default=0,
                        help="item/rune share of target HP (Giant Slayer)")
        sp.add_argument("--armor", type=float, default=80)
        sp.add_argument("--mr", type=float, default=60)
        sp.add_argument("--duration", type=float, default=8,
                        help="fight length in seconds (default 8)")
        sp.add_argument("--no-ult", action="store_true", help="do not cast R")
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

    sp = bdsub.add_parser("survive",
                          help="how long a tank build lasts against a modeled carry's build")
    sp.add_argument("name", help="tank champion with a kit encoding: drmundo")
    sp.add_argument("--items", nargs="*", default=[],
                    help="the tank's items, boots first (names, nicknames, or ids)")
    sp.add_argument("--attacker", default="kayle",
                    help="the attacking champion (default kayle)")
    sp.add_argument("--attacker-items", nargs="*",
                    help="its build (default: its best overall build from the dashboard's cache)")
    sp.add_argument("--level", type=int, default=16)
    sp.add_argument("--duration", type=float, default=30,
                    help="fight length in seconds (default 30)")
    sp.set_defaults(func=builds.cmd_survive)

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
    tf = sub.add_parser("tft", help="Teamfight Tactics: optimal items, early boards, and trait flex")
    tfsub = tf.add_subparsers(dest="tft_cmd", required=True)

    def tft_args(sp):
        sp.add_argument("--set", type=int, default=tft.DEFAULT_SET, help="TFT set number (default 18)")
        sp.add_argument("--patch", help="archived patch to use (default: newest)")

    sp = tfsub.add_parser("fetch", help="snapshot the set's unit/item/trait data for a patch")
    sp.add_argument("--set", type=int, default=tft.DEFAULT_SET)
    sp.add_argument("--patch", help="TFT patch label, e.g. 18.1d (base labels resolve to the newest hotfix)")
    sp.add_argument("--force", action="store_true", help="refetch even if archived")
    sp.set_defaults(func=tft.cmd_fetch)

    sp = tfsub.add_parser("refresh", help="fetch current patch/hotfix, prepare active TFT analyses, and publish saved dashboard responses")
    sp.add_argument("--set", type=int, default=tft.DEFAULT_SET)
    sp.add_argument("--patch", help="patch to check (default: latest patch and hotfix)")
    sp.set_defaults(func=tft.cmd_refresh)

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
        sp.add_argument("--threat", default="mixed", choices=list(tft.TANK_THREATS),
                        help="incoming damage profile for tanks (default: mixed)")
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

    sp = tfsub.add_parser("fetch-lobby-stats", help="fetch empirical lobby play-rate weights for Trait Flex")
    tft_flex.add_fetch_lobby_stats_args(sp)
    sp.set_defaults(func=tft_flex.cmd_fetch_lobby_stats)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except ModuleNotFoundError as e:
        sys.exit(f"Missing dependency: {e.name}.")
