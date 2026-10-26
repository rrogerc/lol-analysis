# lol-analysis

League of Legends and Teamfight Tactics analytics, combat simulators, and dashboard. One CLI (`cli.py`), one SQLite database (`lol.db`), two native Rust combat engines, and a buildless static web frontend.

## Layout

```text
cli.py              Unified CLI entrypoint
lol/                Summoner's Rift analytics and build simulator
  ladder.py         Champion game-length scaling and Master+ one-trick aggregation
  items.py          Patch item snapshots (Data Dragon + Meraki + CommunityDragon shop rules)
  builds.py         Stat sheets, fight/survival simulation, exhaustive build optimizer
  leaderboard.py    Cross-champion DPS and survival leaderboards
  codegen/          Champion kit dossier and Rust driver generation pipeline
  engine/           Rust PyO3 combat engine (builds to lol/lol_engine.abi3.so)
  data/             Committed scaling, item, and champion kit/golden archives
  tests/            LoL Python unittest suite and Node UI suite
tft/                Teamfight Tactics Set 18 simulator and planners
  sim.py            Snapshot loader, unit build/core enumeration, leaderboards, refresh
  board.py          Board geometry, trait breakpoints/emblems, symmetric team encounters
  early.py          Levels 4 to 7 early-game board planner
  flex.py           Trait Flex graph solver and empirical lobby play-rate fetcher
  update.py         Bounded HTTP retries, patch-note reconciliation, bound patch reviews
  site.py           Immutable pre-compressed HTTP publication bundles
  golden.py         Golden fight fixture generator and verifier
  engine/           Rust PyO3 TFT combat engine (builds to tft/lol_tft.abi3.so)
  data/             Committed Set 18 snapshots, patch reviews, and golden fixtures
  tests/            TFT Python unittest suite and Node UI/solver suite
web/                Dashboard server, static exporter, frontend, and operations
  db.py             SQLite schema, connection helper, and shared repo paths
  server.py         Local HTTP server (`serve`) and static snapshot builder (`export`)
  static/           Buildless HTML, CSS, JS, and Trait Flex Web Worker
  ops/              Engine build script, scheduled timer scripts, systemd reference config
  tests/            Shared Node VM/DOM harness for UI suites
```

## Setup

Build both Rust combat extensions (`lol/lol_engine.abi3.so` and `tft/lol_tft.abi3.so`):

```bash
./web/ops/build-engine.sh
```

Populate `lol.db` from the committed `lol/data/scaling` archive (or sync directly from a sibling `../lol-quant` checkout):

```bash
python3 cli.py import-json
# or, when ../lol-quant is present:
python3 cli.py scaling sync --db-only
python3 cli.py onetricks sync
```

## Common Commands

```bash
# Web dashboard and static export
python3 cli.py serve --port 8399 --no-open
python3 cli.py export --out _site

# Summoner's Rift: scaling, one-tricks, items, and builds
python3 cli.py status
python3 cli.py scaling report --scaling
python3 cli.py scaling champion kayle
python3 cli.py items fetch
python3 cli.py builds sim kayle --items "Nashor's Tooth" "Rabadon's Deathcap"
python3 cli.py builds optimize kayle --slots 6
python3 cli.py builds warm

# Teamfight Tactics: unit builds, early comps, trait flex, and scheduled refresh
python3 cli.py tft status
python3 cli.py tft units
python3 cli.py tft sim ashe --star 2 --items "Guinsoo's Rageblade" "Infinity Edge" "Kraken's Fury"
python3 cli.py tft top ashe --star 2
python3 cli.py tft warm
python3 cli.py tft refresh
python3 cli.py tft fetch-lobby-stats
```

## Tests

```bash
# Python unit and golden suites (LoL + TFT)
python3 -m unittest

# Frontend and Web Worker solver suites
node lol/tests/test_ui.cjs
node tft/tests/test_ui.cjs

# Rust crate unit tests
nix-shell -p cargo rustc --run "cargo test --manifest-path lol/engine/Cargo.toml --no-default-features && cargo test --manifest-path tft/engine/Cargo.toml --no-default-features"
```
