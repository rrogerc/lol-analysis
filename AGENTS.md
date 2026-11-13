# AGENTS.md

Engineering reference for `lol-analysis`: League of Legends and Teamfight Tactics analytics, two Rust combat engines, and a buildless web dashboard. Three top-level packages (`lol/`, `tft/`, `web/`) sit behind one CLI (`cli.py`) and one SQLite database (`lol.db`, gitignored and rebuilt from committed archives).

## Setup

- **Python**: System Nix Python at `/run/current-system/sw/bin/python3`, the same environment the services run on (defined in `/home/rogerc/dotfiles/nixos/python-env.nix`: `numpy`, `pyarrow`, `scikit-learn`, `scipy`). There is no `.venv` or `requirements.txt`; add packages there.
- **Rust**: `cargo` and `rustc` are not on `PATH`. Build both extensions with `./web/ops/build-engine.sh` (`builds`, `tft`, or default `both`). It sets `PYO3_PYTHON`, enters `nix-shell -p cargo rustc` when needed, and atomically swaps `lol/lol_engine.abi3.so` and `tft/lol_tft.abi3.so`. Both `.so` files are gitignored, so a fresh checkout needs this first.
- **Node**: `/run/current-system/sw/bin/node` runs the CJS UI and worker suites directly. No npm dependencies.
- **Database**: `python3 cli.py import-json` rebuilds `lol.db` from `lol/data/`. With a sibling `../lol-quant` checkout, sync instead: `scaling sync --db-only`, `onetricks sync`, `matchups sync --db-only`.

## Common Commands

```bash
python3 cli.py serve --port 8399 --no-warm --no-open   # smoke test; never use 8321 (see below)
python3 cli.py export --out _site
python3 cli.py status

python3 cli.py scaling report --scaling
python3 cli.py matchups champion yasuo
python3 cli.py items fetch
python3 cli.py builds sim kayle --items "Nashor's Tooth" "Rabadon's Deathcap"
python3 cli.py builds optimize kayle --slots 6
python3 cli.py builds warm

python3 cli.py tft sim ashe --star 2 --items "Guinsoo's Rageblade" "Infinity Edge" "Kraken's Fury"
python3 cli.py tft top ashe --star 2
python3 cli.py tft refresh
python3 cli.py tft fetch-lobby-stats
```

`python3 cli.py <command> --help` lists the rest.

## Repository Map

- `cli.py`: Argparse entrypoint dispatching `serve`, `export`, `status`, `import-json`, `scaling`, `onetricks`, `matchups`, `items`, `builds`, and `tft`.
- `lol/`: Summoner's Rift analytics and theoretical build simulator.
  - `ladder.py`: Game-length win-rate scaling (`stats`, `match_counts` tables; archive in `lol/data/scaling/`) and Master+ one-trick aggregation (`onetricks`, `onetrick_sync` tables) from `../lol-quant` parquet files.
  - `matchups.py`: Thematic counters, comp-adjusted kit synergies, Master+ behavioral trait fingerprints, and 14 unsupervised interaction archetypes (`matchups_snapshot` table; archive in `lol/data/matchups/`).
  - `items.py`: Patch-versioned item snapshots in `lol/data/items/<patch>/`: `ddragon.json` (Data Dragon stats), `meraki.json` (structured stats and passives), and `groups.json` (shop and exclusivity rules distilled from CommunityDragon's item bin).
  - `builds.py`: Champion stat sheets, single-build fight simulation, exhaustive build enumeration, and the `.cache/builds/` cell warmer.
  - `leaderboard.py`: Cross-champion DPS leaderboards read from `.cache/builds/` cells. Never simulates.
  - `codegen/`: Champion kit pipeline (`sources.py`, `dossier.py`, `driver.py`, `golden.py`, plus `dossier-prompt.md` and `driver-guide.md`).
  - `engine/`: Rust PyO3 extension `lol_engine`. Hand-written drivers in `src/drivers.rs`; generated drivers in `src/generated/`.
  - `data/`: `scaling/`, `matchups/`, `items/`, and `builds/` (`item-effects.json`, `kits/<slug>.json`, `champions/<patch>/`, `dossiers/`, `sources/`, `golden/`).
  - `tests/`: `test_ladder.py`, `test_matchups.py`, `test_builds.py`, `test_codegen.py`, `test_ui.cjs`.
- `tft/`: Teamfight Tactics Set 18 combat simulator, planners, and publisher.
  - `sim.py`: Snapshot loading (`tft/data/set18/<patch>/`), unit fight specs, 3-item cell enumeration, 2-item core comparisons, unit leaderboards, and `tft refresh` orchestration (including its exit codes and heartbeat).
  - `board.py`: Hex placement, trait breakpoints and emblems, and symmetric team encounter evaluation.
  - `early.py`: Item-free stage-two board planner (levels 4 and 5) tested against fixed early reference boards, cached in `.cache/tft-early/`.
  - `flex.py`: Combat-free Trait Flex catalog (`catalog(snap)`) and lobby population profiles fetched into `tft/data/set18/lobby-stats-<patch>.json`.
  - `update.py`: Bounded HTTP retry client (`get`, `FetchError`), Riot patch-note reconciliation, and bound patch reviews (`tft/data/set18/patch-reviews/*.json`, `ReviewRequired`).
  - `site.py`: Immutable pre-compressed HTTP bundle builder (`.cache/tft-site/g-<sha256>/`).
  - `golden.py`: Golden fixture generator and comparator (`tft/data/golden/`).
  - `engine/`: Rust PyO3 extension `lol_tft`.
  - `tests/`: `helpers.py`, `test_sim.py`, `test_drivers.py`, `test_items_traits.py`, `test_encounters.py`, `test_planner.py`, `test_update.py`, `test_site.py`, `test_ui.cjs`.
- `web/`: HTTP server, static exporter, buildless frontend, and ops scripts.
  - `db.py`: SQLite schema, `db_connect()`, `patch_key()`, and repo path constants.
  - `server.py`: `cmd_serve` and `cmd_export` with identical `/api/*` JSON shapes, `AutoWarm` for cold LoL build cells, and `TftResponses` for zero-compute TFT serving.
  - `static/`: `index.html`, `styles.css`, `tft-flex-worker.js`, `app.js`, `lol.js`, `tft.js`.
  - `ops/`: `build-engine.sh` and the timer scripts `refresh-items.sh`, `refresh-tft.sh`, `sync-scaling.sh`.
  - `tests/`: `harness.cjs`, the shared VM/DOM test harness.

## Live Host Services (NixOS)

Declared in `/home/rogerc/dotfiles/nixos/services/lol-analysis.nix`. Commits on `main` go live automatically, so treat a commit as a deploy.

- `lol-dashboard.service` (system): `cli.py serve --host 0.0.0.0 --port 8321 --no-open` over Tailscale. **Never bind port `8321` manually.** The unit gives up after 5 failed starts in 5 minutes, so a stray process on that port leaves the real dashboard `failed` while the stray one answers. Smoke-test on a temporary port such as `--port 8399 --no-warm --no-open`.
- `lol-dashboard-reload.path` (system): watches `.git/logs/HEAD` and `.cache/tft/.dashboard-ready`, restarting the dashboard after a 10s debounce.
- User timers (`systemctl --user`):
  - `lol-items-refresh.timer` (daily `07:23`): `web/ops/refresh-items.sh` snapshots item data into `lol/data/items/` and, when it changes, commits only that path in this checkout and pushes (with `pull --rebase --autostash` if GitHub moved). Expect `items:` commits to land under you; pull before pushing.
  - `lol-scaling-sync.timer` (every 6h at `:30`): `web/ops/sync-scaling.sh` runs `scaling sync --db-only` and `onetricks sync`, plus `matchups sync --db-only` when `../lol-quant/data/models/meta.json` exists. `--db-only` leaves the committed archives to deliberate hand-run syncs.
  - `lol-tft-refresh.timer` (every 6h at `:41`): `web/ops/refresh-tft.sh` execs `cli.py tft refresh`, which exits `75` on exhausted temporary fetch failures, `2` when a patch change needs manual review, and `1` otherwise. systemd retries only `75`, 5 minutes apart, at most 3 starts per 30 minutes; `1` and `2` stay failed and visible.
- Heartbeats land in `.cache/ops/<script>.json` and feed the Data tab health table via `web/server.py:local_jobs()`. The items and scaling scripts write theirs from an `EXIT` trap; `tft refresh` writes `refresh-tft.json` itself. `local_jobs()` lists any `web/ops/*.sh` whose header has `# name:` and `# schedule:` comments.

## Architectural Invariants

### Floating-point determinism (both Rust engines)
- Preserve strict IEEE-754 order of operations: never enable fast-math, never rewrite `a * b + c` as `mul_add`, never replace explicit multiplications with `powi`. The golden suites pin engine output bit for bit, so any reordering fails them.
- Both `build.rs` scripts hash `Cargo.toml` and every file under `src/` into `SOURCE_HASH`, so Rust edits invalidate downstream `.cache/` artifacts on their own. `lol_engine` also exports `CORE_HASH` (everything except `src/generated/`) and per-champion `GENERATED_HASHES`.

### LoL build cache and kits
- A champion's cells key on `builds.CORE_HASH` (the bytes of `builds.py` plus `lol_engine.CORE_HASH`), plus that champion's own generated driver hash if it has one. Rewriting one generated driver recomputes only that champion; editing `builds.py` or any hand-written engine source recomputes everyone. Presentation and ranking code therefore belongs in `lol/leaderboard.py`.
- Kit encodings live in `lol/data/builds/kits/<slug>.json`. `builds._paths_stamp()` watches `KITS_DIR`, `BUILDS_DATA_DIR`, and `ITEMS_DATA_DIR`, so kit or item snapshot changes invalidate memoized cell paths.
- The Builds tab ranks reviewed kits only (`builds.kit_champions()`): hand-encoded kits and generated kits marked `"reviewed": true`. A draft from `lol.codegen.driver write` (`"generated": true, "reviewed": false`) stays off the tab until a person reviews it.
- `kits/jax.json` and `src/generated/jax.rs` are the reference example `driver.py write` shows the model and the codegen tests' fixture, not a ranked build. Keep them.
- Golden fixtures in `lol/data/builds/golden/` record their exact `patch` (e.g. `16.18`), and golden tests pass `doc["patch"]` explicitly so routine item snapshots never break historical verification. After a deliberate model change, regenerate at that same patch (`python3 -m lol.codegen.golden --patch <patch>`) so only the model moves, and record in `lol/data/builds/golden/README.md` which cases changed and why.
- Driver codegen (`python3 -m lol.codegen.driver`) compiles candidate drivers in an isolated temp `CARGO_TARGET_DIR` and benchmarks them via `_bench --so-dir`, overriding `sys.modules["lol.lol_engine"]` before importing `lol.builds`.
- Dossier runs (`lol.codegen.dossier`) measure what a model derives from archived sources alone. Each call is `claude -p --tools ""` from a temp directory, with the sources pasted into the prompt, so the model can't read the repo and doesn't auto-load this file. Keep the cwd outside the repo, and keep champion-specific math out of this file.

### TFT publication and combat-free Trait Flex
- `TftResponses` in `web/server.py` never runs simulations, snapshot loads, or leaderboard assembly on a request. It pins `.cache/tft/.dashboard-ready` at startup and serves immutable identity/gzip files from `.cache/tft-site/g-<sha256>/` with `ETag` and `If-None-Match`. New TFT data reaches the dashboard only through `tft refresh` publishing a new bundle. (`cli.py export` is the exception: it warms and dumps TFT cells itself.)
- `tft/flex.py` must stay free of combat-engine imports (`lol_tft`, `tft.sim.engine`, item/trait combat effects).
- Single-player gold estimation (`tftFlexGoldEstimate`) and full-lobby Markov/Monte Carlo estimation (`tftFlexLobbyEstimate`) share one file, `web/static/tft-flex-worker.js`, loaded as a browser Web Worker and via `require()` in Node tests. Keep it loadable in both.

### Buildless frontend (`web/static/`)
- Classic `<script defer>` tags load in strict order: `tft-flex-worker.js`, `app.js`, `lol.js`, `tft.js`. `init()` runs at the end of `tft.js` unless `globalThis.__LOL_NO_AUTO_INIT__` is set (as `web/tests/harness.cjs` does).
- Every relative `api(...)` fetch must resolve identically under `cli.py serve` and a `cli.py export` static bake.

## Verification

Run what your change touches; run all of it before committing anything that spans packages.

1. After any Rust edit, rebuild: `./web/ops/build-engine.sh`
2. Rust unit tests: `nix-shell -p cargo rustc --run "cargo test --manifest-path lol/engine/Cargo.toml --no-default-features && cargo test --manifest-path tft/engine/Cargo.toml --no-default-features"`
3. Python unit and golden suites: `python3 -m unittest`
4. Frontend and worker suites: `node lol/tests/test_ui.cjs && node tft/tests/test_ui.cjs`
