# lol-analysis — notes for whoever works here next

## The dashboard on :8321 is a NixOS system service. Never launch it by hand.

- `lol-dashboard.service` runs `lol.py serve --host 0.0.0.0 --port 8321
  --no-open` as rogerc from this checkout, with `Restart=always`. It is
  declared in `~/Developer/dotfiles/nixos/configuration.nix`
  (`systemd.services.lol-dashboard`), not in this repo and not as a user
  unit — check it with plain `systemctl status lol-dashboard`, never
  `systemctl --user`. Logs: `journalctl -u lol-dashboard -n 50`.
- `lol-dashboard-reload.path` (same file) restarts it about 10 s after
  this repo's HEAD moves, so every commit or pull restarts serve.
- A hand-started `lol.py serve` on 8321 squats the port: the unit then
  fails with "Address already in use", hits its start limit, and the
  dashboard's Services card shows it failed while the squatter answers.
  This happened Aug 22–30 2026 and again on 2026-09-04. If serve is
  needed for an experiment, use another port (`--port 8399`) and kill it
  afterwards. If :8321 answers while the unit reports failed, find the
  squatter with `pgrep -af 'lol.py serve'`, kill it, then
  `sudo systemctl restart lol-dashboard` (sudo is Roger's).
- After editing `builds.py` (or `engine/`, once rebuilt) the Builds tab
  says it is running older code than is on disk until serve restarts.
  Restart by committing, or with `sudo systemctl restart lol-dashboard`.
  systemd stops the whole cgroup, so the background warm and its workers
  go with it.
- Other units in that nix file: `lol-items-refresh` (daily item snapshot,
  user timer) and `lol-scaling-sync` (six-hourly, user timer); the
  dashboard's Data tab shows their state.

## The builds warm

- serve never simulates on request: whenever a scenario cell is cold it
  spawns `lol.py builds warm` (log: `.cache/builds/warm.log`, progress
  lines every 30 s). `serve --no-warm` disables that. Killing the warm
  child turns auto-warm off until serve restarts.
- The numbers come from the compiled engine in `engine/` (Rust, PyO3),
  imported as `lol_engine` from `lol_engine.abi3.so` at the repo root
  (gitignored). Build it with `jobs/build-engine.sh`: it uses `cargo` if
  present, else `nix-shell -p cargo rustc`; ~15 s cold, ~3 s warm. Without
  it `import builds` fails with a message saying so. The warm runs on the
  unit's CPython; PyPy is no longer involved (`warm_python()` only honours
  `$LOL_WARM_PYTHON`).
- Cells are keyed by a hash of every input including `builds.py` and the
  engine sources (`lol_engine.SOURCE_HASH`, stamped by `engine/build.rs`),
  so an edit to either recomputes everything: about 17 s for both
  champions on this 16-thread box with the previous cells as seeds
  (Kayle 11 s, Vladimir 5.4 s, measured 2026-09-04 after the hot-path
  rewrite; the first Rust cut took 32 s, the pure-Python engine 59 min).
  A cold cache (no seeds) takes ~26 s: the first blocks run unbounded
  and the kill-time guess gets repaired by a second pass. Editing `engine/src`
  without rebuilding makes `source_stale()` true — the Builds tab says so;
  run the build script, then restart serve.
- The enumerator's inner loop is `engine/src/enumerate.rs` (Ctx.run_block);
  the parent side — blocks, merge, the shared bounds table `_Bounds`, the
  checked guess — stays in `builds.py`. It prunes exactly: results must
  stay identical to an unpruned pass, and `test_builds` checks that.
- The fight engine builds the target-independent half of a fight once per
  boots class (`Prep` in `fight.rs`, driven through `Sim`) and runs the
  three targets against it; per-hit values that depend only on a few
  discrete stacks (resist multipliers, attack speed, the amp per combat
  second) are memoized and, in a debug build, re-checked bit for bit
  against the slow path on every call. Items are dense indices inside
  `Ctx` (no hashing in the loop) and a class does no heap allocation per
  fight. Benchmarks on this hybrid CPU must be pinned to a P-core
  (`taskset -c 2`) and interleaved with the baseline; monomorphising the
  whole block loop per driver measured as a 3.5% loss (I-cache), so only
  the fight itself is generic over the driver.
- Any engine change must keep the fights bit-identical unless it is a
  deliberate model change: `test_builds.TestGolden` replays
  `data/builds/golden` (every fight of ~250 builds and three enumeration
  passes, as the pre-Rust Python engine computed them). A deliberate
  change regenerates the fixtures with `jobs/gen_golden.py` in the same
  commit. Keep the Rust strict: no `target-cpu`, no fast-math, no
  `mul_add`; floating point has to stay operation-for-operation what the
  Python engine did.

## The TFT tab (tft.py, tft_kits.py, data/tft/)

- Purely theoretical, no match data (Roger's call 2026-09-04): a carry's
  mana-cycle fight against three stat dummies derived from the set's own
  units at 2★ (two median tanks, then a median non-tank), every 3-item
  multiset of the 35 craftable completed items, ranked by time to kill all
  three then damage. Twelve cells per unit: 2★/3★ × spread/clump × traits
  bare/low/high. Pure Python; a fight is ~0.25 ms, a cell ~0.5 s on the
  16 threads, the whole warm a couple of minutes. Cache `.cache/tft/`,
  keyed by tft.py + tft_kits.py + the snapshot + the hand files; serve
  auto-warms it like builds (log `.cache/tft/warm.log`).
- Numbers are data, mechanics are code. `lol.py tft fetch` archives
  MetaTFT's public lookup JSON (the only machine-readable source of the
  current set's ability numbers — Community Dragon lost them when Set 18
  moved to curve tables), the cdragon character-bin timings and the
  patch's "old ⇒ new" lines under `data/tft/set<N>/<patch>/`. The MetaTFT
  file can be a pre-launch PBE build (its `_metadata.patch` says so), so
  ALWAYS run `lol.py tft check` after a fetch: it matches the notes to
  curve rows and base stats, prints an `overrides.json` snippet for
  anything stale, and exits 2 while anything is. The patch directory's
  own `overrides.json` carries those corrections with their source (per
  patch, so a new patch starts clean); a curve override lists per-star
  values from 1★ (a shorter list leaves higher stars alone).
- Automated like the item snapshot: `jobs/refresh-tft.sh` (daily 07:41,
  nix user timer `lol-tft-refresh`, declared next to `lol-items-refresh`
  in the dotfiles nix config) refetches the current patch, commits and
  pushes only when the data changed, then runs `tft check` — a red
  "failed (exit 2)" in the Automation panel means stale numbers waiting
  for an overrides.json edit, not a broken job.
- Hand files under `data/tft/set<N>/` reference the data's own rows and
  never write numbers: `item-effects.json` (which passives the engine
  models, and `excluded`), `trait-effects.json` (per-breakpoint trait
  bonuses), `kits.json` (dashboard notes). Item plain stats come from the
  stat line automatically (`parse_stat_line`, keyed by the icon).
- `tft_kits.py` is one short driver class per unit (22 carries of Set 18):
  the ability's shape only, reading `f.calc(...)` and `f.row(...)`. Add a
  unit = a driver + a `DRIVERS` entry (+ trait-effects entries if its
  traits are new). A new set = new drivers, new hand files, `DEFAULT_SET`.
- Assumptions to remember (all in tft.py constants or noted in the UI):
  AD ×1.5 and HP ×1.8 per star, 1 s mana lock from a cast, curve rows
  hold the previous star's value, fighters' role attack speed at stage 4,
  damage amp additive and post-mitigation, negative resists floored at 0,
  Blossom/Elderwood "high" stops below the 11-unit prismatic tier, Fae
  pixies 3 and 7, the Riftbeast Alpha Mark on the unit, Primal = Tiger.
  Tanks, healers and buffers are not modeled yet (no driver).
- Per patch, by hand if the job hasn't: `lol.py tft fetch` → `tft check`
  → edit that patch's overrides.json → commit (the reload unit restarts
  serve, which warms). Tests: `python3 -m unittest test_tft` (~0.1 s,
  reads the committed snapshot).

## Tests

- `python3 -m unittest test_builds` (needs the built engine; ~2 s).
- `python3 -m unittest test_tft` (~0.1 s).
