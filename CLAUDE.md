# CLAUDE.md

Engineering reference for `lol-analysis`. The repository is organized into three top-level packages (`lol/`, `tft/`, `web/`) fronted by a single CLI (`cli.py`).

## Repository Map

- `cli.py`: Argparse entrypoint dispatching `serve`, `export`, `status`, `import-json`, `scaling`, `onetricks`, `matchups`, `items`, `builds`, and `tft`.
- `lol/`: Summoner's Rift analytics and theoretical build simulator.
  - `ladder.py`: Game-length win-rate scaling (`stats`, `match_counts` tables in `lol.db`, committed archive in `lol/data/scaling/`) and Master+ one-trick aggregation (`onetricks`, `onetrick_sync` tables) sourced from `../lol-quant` parquet files.
  - `matchups.py`: Thematic counters, comp-adjusted kit synergies, Master+ behavioral trait fingerprints, and 14 unsupervised interaction archetypes (`matchups_snapshot` table in `lol.db`, committed archive in `lol/data/matchups/`).
  - `items.py`: Patch-versioned static item snapshots in `lol/data/items/<patch>/` combining Data Dragon stats, Meraki passives/nicknames, and CommunityDragon `items.bin.json` shop/exclusivity rules.
  - `builds.py`: Champion stat sheets, single-build fight and tank survival simulation, exhaustive build enumeration, and `.cache/builds/` cell warmer.
  - `leaderboard.py`: Cross-champion DPS and survival leaderboards assembled from `.cache/builds/` cells.
  - `codegen/`: Champion kit pipeline (`sources.py`, `dossier.py`, `driver.py`, `golden.py`, plus prompt/guide markdown).
  - `engine/`: Rust PyO3 extension (`lol_engine`, compiled to `lol/lol_engine.abi3.so`). Hand-written reference drivers live in `src/drivers/`; generated champion drivers live in `src/generated/`.
  - `data/`: `scaling/`, `matchups/`, `items/`, and `builds/` (`item-effects.json`, `kits/<slug>.json`, `champions/`, `dossiers/`, `sources/`, `golden/`).
  - `tests/`: `test_ladder.py`, `test_matchups.py`, `test_builds.py`, `test_codegen.py`, `test_ui.cjs`.
- `tft/`: Teamfight Tactics Set 18 combat simulator, planners, and publisher.
  - `sim.py`: Snapshot loading (`tft/data/set18/<patch>/`), unit fight specs, 3-item cell enumeration, 2-item core comparisons, unit leaderboards, and `tft refresh` orchestration.
  - `board.py`: Board hex placement, trait breakpoints and emblem application, and symmetric team encounter evaluation.
  - `early.py`: Levels 4 to 7 early-game board planner cached in `.cache/tft-early/`.
  - `flex.py`: Combat-free Trait Flex catalog (`catalog(snap)`) and empirical lobby play-rate fetcher (`tft/data/set18/lobby-play-rates.json`).
  - `update.py`: Bounded HTTP retry client (exit code `75` on exhausted temporary failures), Riot patch-note reconciliation, and bound patch-review validation (`tft/data/set18/patch-reviews/*.json`, exit code `2` when manual review is required).
  - `site.py`: Immutable pre-compressed HTTP bundle builder (`.cache/tft-site/g-<sha256>/`).
  - `golden.py`: Golden fixture generator and comparator (`tft/data/golden/`).
  - `engine/`: Rust PyO3 extension (`lol_tft`, compiled to `tft/lol_tft.abi3.so`).
  - `tests/`: `helpers.py`, `test_sim.py`, `test_drivers.py`, `test_items_traits.py`, `test_encounters.py`, `test_planner.py`, `test_update.py`, `test_site.py`, `test_ui.cjs`.
- `web/`: HTTP server, static exporter, buildless frontend, and operations scripts.
  - `db.py`: SQLite schema (`lol.db`), `db_connect()`, `patch_key()`, and repo path constants.
  - `server.py`: `cmd_serve` and `cmd_export` exposing identical `/api/*` JSON shapes, `AutoWarm` for cold LoL build cells, and `TftResponses` for zero-compute TFT serving.
  - `static/`: `index.html`, `styles.css`, `tft-flex-worker.js`, `app.js`, `lol.js`, `tft.js`.
  - `ops/`: `build-engine.sh` and scheduled timer scripts (`refresh-items.sh`, `refresh-tft.sh`, `sync-scaling.sh`).
  - `tests/`: `harness.cjs` shared VM/DOM test harness.

## Tooling and Environment

- **Python**: System Nix Python at `/run/current-system/sw/bin/python3` (includes `pyarrow` for `../lol-quant` parquet syncs). There is no `.venv`.
- **Rust**: `cargo` and `rustc` are not installed globally on `PATH`. Build both extensions with `./web/ops/build-engine.sh` (accepts `builds`, `tft`, or defaults to `both`), which sets `PYO3_PYTHON=/run/current-system/sw/bin/python3`, invokes `nix-shell -p cargo rustc` when needed, and atomically swaps `lol/lol_engine.abi3.so` and `tft/lol_tft.abi3.so`.
- **Node**: `/run/current-system/sw/bin/node` runs the CJS UI and worker solver test suites directly with zero npm dependencies.

## Live Host Services (NixOS)

Declared in `/home/rogerc/Developer/dotfiles/nixos/services/lol-analysis.nix`:

- `lol-dashboard.service` (system unit): runs `python cli.py serve --host 0.0.0.0 --port 8321 --no-open` over Tailscale. **Never manually bind port `8321`**; use a temporary port such as `--port 8399 --no-warm --no-open` when smoke-testing `serve`.
- `lol-dashboard-reload.path` (system path unit): watches `.git/logs/HEAD` and `.cache/tft/.dashboard-ready`, restarting `lol-dashboard.service` after a 10s debounce so new commits and fresh TFT publications go live automatically.
- User timers (`systemctl --user`):
  - `lol-items-refresh.timer` (daily `07:23`): runs `web/ops/refresh-items.sh`, snapshotting current item data into `lol/data/items/` and pushing when changed.
  - `lol-scaling-sync.timer` (every 6h at `:30`): runs `web/ops/sync-scaling.sh` (`scaling sync --db-only` + `onetricks sync`).
  - `lol-tft-refresh.timer` (every 6h at `:41`): runs `web/ops/refresh-tft.sh` (`cli.py tft refresh`), retrying exit `75` up to 3 times per 30 minutes and halting visibly on exit `1` or `2`.
- Scheduled scripts write exit heartbeats to `.cache/ops/<job>.json` (read by `web/server.py:local_jobs()` for the Data tab health table).

## Key Architectural Invariants

### Floating-Point Determinism (Both Rust Engines)
- Both `lol/engine` and `tft/engine` preserve strict IEEE-754 order of operations: never enable fast-math, never rewrite `a * b + c` as `mul_add`, and never replace explicit multiplications with `powi`.
- Both `build.rs` scripts hash every `Cargo.toml` and `src/**/*.rs` file into `SOURCE_HASH` (with per-champion `GENERATED_HASHES` in `lol_engine`), invalidating downstream `.cache/` artifacts automatically when Rust code changes.

### LoL Build Cache (`CORE_HASH`) and Kits
- `lol/builds.py` hashes its own bytes plus `lol_engine.SOURCE_HASH` into `CORE_HASH`. Keep presentation/leaderboard code in `lol/leaderboard.py` so UI/ranking tweaks do not invalidate all 173 champions' `.cache/builds/` cells.
- Champion kit encodings live in `lol/data/builds/kits/<slug>.json`. `builds._paths_stamp()` watches `KITS_DIR`, `BUILDS_DATA_DIR`, and `ITEMS_DATA_DIR` so any kit or item snapshot update invalidates memoized cell paths.
- Golden fixtures in `lol/data/builds/golden/` record their exact `patch` (e.g. `16.18`); golden tests pass `doc["patch"]` explicitly so routine `refresh-items.sh` snapshots on newer patches never break historical math verification.
- Driver codegen (`python3 -m lol.codegen.driver`) compiles candidate `.rs` drivers in an isolated temp Cargo target directory and benchmarks them via `_bench --so-dir`, overriding `sys.modules["lol.lol_engine"]` before importing `lol.builds`.

### TFT Publication and Combat-Free Trait Flex
- `web/server.py` (`TftResponses`) never runs TFT simulations, snapshot loads, or leaderboard assembly on HTTP requests. It pins `.cache/tft/.dashboard-ready` at startup and serves immutable identity/gzip files from `.cache/tft-site/g-<sha256>/` with `ETag` and `If-None-Match` support.
- `tft/flex.py` (`catalog(snap)`) must remain completely free of combat-engine imports (`lol_tft`, `tft.sim.engine`, item/trait combat effects). Both single-player gold estimation (`tftFlexGoldEstimate`) and full-lobby Markov/Monte Carlo estimation (`tftFlexLobbyEstimate`) live in one shared file, `web/static/tft-flex-worker.js`, loaded both as a browser Web Worker and directly via `require()` in Node tests.

### Buildless Frontend (`web/static/`)
- Classic `<script defer>` tags load in strict order: `tft-flex-worker.js` -> `app.js` -> `lol.js` -> `tft.js`, with `init()` invoked at the end of `tft.js` unless `globalThis.__LOL_NO_AUTO_INIT__` is set by `web/tests/harness.cjs`.
- Every relative `api(...)` fetch resolves identically whether served dynamically by `cli.py serve` or baked as static files by `cli.py export`.

## Verification Checklist

1. Rebuild native extensions after any Rust edit: `./web/ops/build-engine.sh`
2. Rust unit tests: `nix-shell -p cargo rustc --run "cargo test --manifest-path lol/engine/Cargo.toml --no-default-features && cargo test --manifest-path tft/engine/Cargo.toml --no-default-features"`
3. Python test discovery: `python3 -m unittest`
4. Frontend & worker suites: `node lol/tests/test_ui.cjs && node tft/tests/test_ui.cjs`
