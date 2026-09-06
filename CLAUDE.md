# lol-analysis — notes for whoever works here next

## The dashboard on :8321 is a NixOS system service. Never launch it by hand.

- `lol-dashboard.service` runs `lol.py serve --host 0.0.0.0 --port 8321
  --no-open` as rogerc from this checkout, with `Restart=always`. It is
  declared in `~/Developer/dotfiles/nixos/configuration.nix`
  (`systemd.services.lol-dashboard`), not in this repo and not as a user
  unit — check it with plain `systemctl status lol-dashboard`, never
  `systemctl --user`. Logs: `journalctl -u lol-dashboard -n 50`.
- `lol-dashboard-reload.path` (same file) restarts it about 10 s after
  this repo's HEAD moves or `.cache/tft/.dashboard-ready` changes after a
  successful TFT refresh. Every commit or pull also restarts serve.
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
  (gitignored). Build it with `jobs/build-engine.sh` (both engines; `builds`
  or `tft` for one): it uses `cargo` if present, else `nix-shell -p cargo
  rustc`; ~15 s cold, ~3 s warm. Without
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

## The TFT tab (tft.py, tft_engine/, data/tft/)

- Live snapshot: `data/tft/set18/18.1d/`. See `data/tft/README.md` for
  source coverage, hotfix tracking and the staged refresh/audit workflow.
  `tft refresh` recognizes dated mid-patch updates, reconciles supported
  numeric changes, checks the audit and calculates staged builds before
  publishing. Changes needing review remain under `.pending/`. Run
  `python3 -m unittest test_tft test_tft_data test_tft_update test_tft_refresh`.
- The champion picker groups by cost and uses CommunityDragon portraits
  and trait icons. `api_meta` supplies readable trait descriptions and the
  exact bonuses from `trait_spec` for each selected breakpoint; icons fall
  back to a letter if unavailable. Trait details work by hover, keyboard
  focus and tap. The fight diagram is a schematic of the three ordered
  dummy slots, not a hex board: a heavy first tank (3,000 HP, 70 armor,
  70 MR), a median 2★ tank, then a median 2★ non-tank. The first tank's
  offense still uses the tank medians. Geometry changes nearby-target coverage; individual drivers can
  override the normal target order. The UI shows actual dummy stats and
  distinguishes the eight-unit incoming pressure in tank scenarios from
  three attackers for fighters and none for carries. UI metadata checks:
  `python3 -m unittest test_tft_ui`.
- Purely theoretical, no match data (Roger's call 2026-09-04): one unit's
  mana-cycle fight against three stat dummies derived from the set's own
  units at 2★ (two tanks, then a median non-tank; the first tank's defenses
  are fixed at 3,000 HP / 70 armor / 70 MR by `FRONT_TANK_DEFENSES`, per
  Roger's 2026-09-05 request), every 3-item
  multiset of the 35 craftable completed items. Every shop unit of the set
  is modeled (65 in Set 18). Cells per unit: 1★ and 2★ for every cost,
  3★ only for the 1–3 costs (a 3★ 4- or 5-cost is an auto-win, not a
  build question — Roger's call 2026-09-05; `STARS_BY_COST`,
  `unit_scenarios`) × spread/clump × traits bare/low/high: 18 cells for a
  1–3 cost, 12 for a 4–5 cost, 1,026 in all, and the dashboard's star
  buttons follow the unit. The fights run in the compiled engine
  `tft_engine/` (Rust, PyO3, imported as `lol_tft` from `lol_tft.abi3.so`
  at the repo root; `jobs/build-engine.sh tft` builds it, `jobs/build-engine.sh`
  both engines): a cell of 7,770 builds takes 10–20 ms on the 16 threads
  and the whole warm about 19 s wall (1,026 cells, measured 2026-09-05; the
  pure-Python engine took 13 minutes, 0.3–3 s a cell). Cache `.cache/tft/`,
  keyed by tft.py + `lol_tft.SOURCE_HASH` (a sha256 of tft_engine/src
  stamped by build.rs) + the snapshot + the hand files, so an edit to
  either recomputes everything; serve auto-warms it like builds (log
  `.cache/tft/warm.log`). Editing `tft_engine/src` without rebuilding
  makes `source_stale()` true; run the build script, then restart serve
  (commit, or `sudo systemctl restart lol-dashboard`). Without the .so,
  `tft fetch`/`check`/`status` still work; a fight raises with the build
  command.
- The engine is a port of the Python engine and preserves its float
  operation order. The original golden fixtures verified the port bit for
  bit. The current fixtures were deliberately regenerated for patch 18.1d
  and Roger's 3,000 HP / 70 armor / 70 MR first tank on 2026-09-05; see
  `data/tft/golden/README.md` for provenance. They pin 4,446 fights and
  the top 20 rows of every cell. `test_tft.TestGolden` replays every fight plus a sample of cells
  (`TFT_GOLDEN_ALL=1` for all 1,026; `jobs/tft_compare.py` prints per-unit
  detail). A deliberate model change regenerates the fixtures with
  `jobs/gen_tft_golden.py` from a warm cache in the same commit. The port
  keeps every float operation in Python's order: `pyf.rs` has Python's
  `max`/`min` (first wins ties), banker's `round`, truncating `int` and
  `pysum` — CPython ≥ 3.12 adds floats with Neumaier compensation, so a
  running total is off by an ulp (Azir with two Striker's Flails caught
  it); no `powi`, no `mul_add`, no fast-math; Python passes star-scaled
  health and attack damage. Python resolves every number into a cell spec
  (`kit_spec`: rows at the star, calc terms with the star's coefficient;
  `item_spec`: the stat line as ordered pairs plus the passive with the
  range/role gates applied; `trait_spec`; `cell_spec` with the dummies,
  role, traits and pool) and Rust composes them per build in apply_item's
  order. The opening sheet a row reports carries crit, precision, omnivamp
  and mana as they stand after the fight (what the Python `_sim_task` read
  off the sheet), the rest from before it.
- The engine's shape (`tft_engine/src`): `fight.rs` is tft.Fight/Sheet/
  Dummy (the driver-facing API is its module doc), `fx.rs` Fx +
  apply_item/apply_trait/build_fx, `kit.rs` calc_value, `enumerate.rs` the
  per-cell enumeration on std threads with the GIL released (results by
  build index, ranking = rank_key + the api-name tuple tie-break),
  `driver.rs` the Driver trait, `drivers/` one file per slice. A `Fight<D>`
  is generic over the driver; hooks are associated fns taking the whole
  fight with the driver's state at `f.drv`; rows and calcs are ids resolved
  once in `D::new(&Kit)` (one instance per form's kit, cloned per fight);
  targets are indices, `f.after(delay, tag)` queues `D::event`; shields
  are never removed (a dead flag) so Rammus/Malphite can watch one; a
  dummy's `mark`/`mark_times` replace Python's `d.marks`. `lol.py tft sim
  --trace` prints the fight's event timeline (the tests read the same
  trace and the end-of-fight `probe`).
- WHAT A UNIT IS SCORED ON FOLLOWS RIOT'S ROLE LABEL (2026-09-05): the
  data's `role` ("Attack Caster", "Magic Tank", …; `roleData` is the
  authoritative tag list — the per-unit `roleTags` are inconsistent, Akali
  lacks Role.Attack and Rengar's tags contradict his role) gives
  `unit["kind"]` and `unit["objective"]` via `OBJECTIVE_BY_KIND`:
  Marksman/Caster/Specialist → "carry" (dummies never hit back; ranked by
  kill time, then damage), Fighter/Assassin → "fighter" (the dummies hit
  back, the unit can die; ranked by kill time, then damage dealt before
  dying, 20 s), Tank → "tank" (same pressure, up to `TANK_DURATION` 60 s;
  ranked by how long the unit holds the dummies — an on-death body such
  as Yorick's spirit counts — then damage). A unit's `recommendedItems`
  role wins when it names another role (Master Yi and Gnar itemize as
  Fighters, Caitlyn as a Marksman). The role also sets mana per attack
  (10 / caster 7 + 2 regen / tank 5, plus tank mana from damage taken:
  1% pre + 3% post-mitigation, 42.5 cap — the community formula, Riot
  publishes none), the fighter attack speed by stage and the assassin's
  15% off-target reduction (both from Riot's role text).
- The pressure: in fighter and tank fights each dummy attacks with its
  group's median attack damage and speed (from one period in), gains mana
  per attack like a unit (tank dummies also from damage taken) and casts
  its group's median ability number, split physical/magic by the group's
  share of Attack-type roles; a stun or untargetability denies those
  swings and the amount is credited as `denied`. The unit's body: resists,
  Bramble's attack reduction, durability (sources multiply: 20% and 15%
  make 32% — the wiki says it is not additive, nobody publishes the
  formula), shields (oldest first), health, omnivamp, heals; every
  defensive item and trait passive is in the hand files. Carries are
  never hit, so those passives are inert for them by design.
- Adaptor units carry both forms (`unit["forms"]` from the file's
  `extraAbilities`); `adaptor_form` picks AD when the build's bonus AD
  fraction beats its bonus AP per 100 (ties → the role's damage type),
  the trait bonus follows, and the form's stats/calcs/rows replace the
  base ones (`Sheet`). Riot never documents the actual rule.
- Numbers are data, mechanics are code. `lol.py tft fetch` archives
  MetaTFT's public lookup JSON (the structured ability source currently
  used here — the CommunityDragon export checked September 5 lacks Set 18
  calculations and alternate Adaptor forms; its JSON still has base
  stats and settled Fiddlesticks/Ivern's health and Kog'Maw's attack
  damage, which the PBE file lacks — those are overrides with that
  source), the CommunityDragon set export and character-bin timings, and
  the patch's dated notes under `data/tft/set<N>/<patch>/`. The MetaTFT file can be a
  pre-launch PBE build (its `_metadata.patch` says so), so ALWAYS run
  `lol.py tft check` after a fetch: it matches the notes to curve rows and
  base stats, prints an `overrides.json` snippet for anything stale, and
  exits 2 while anything is. The patch directory's own `overrides.json`
  carries those corrections with their source. Automatic refreshes validate
  source changes and patch-note mappings before carrying corrections to a
  new patch; a curve override lists per-star values from 1★ (a
  shorter list leaves higher stars alone); `stats` overrides fill missing
  base stats. Shop units are the file's `shopUnit` flag (cost and traits
  alone would count Elise's spider form twice).
- `jobs/refresh-tft.sh` runs `tft refresh` every six hours (00:41, 06:41,
  12:41, 18:41 local time; persistent Nix user timer `lol-tft-refresh`,
  next to `lol-items-refresh` in the dotfiles config). It fetches the
  current patch/hotfix, reconciles known numeric changes in `tft_update.py`,
  validates and warms all changed scenarios before publication. Unknown
  mechanics, ambiguous mappings or unverified source changes preserve the
  working snapshot and report `needs-review` (exit 2). The local job never
  commits or pushes. `jobs/.state/refresh-tft.json` supplies the UI status;
  `.cache/tft/.dashboard-ready` signals the existing system reload path
  only when a complete cache generation changes. The TFT tab polls for new
  revisions and preserves the user's current selection when reloading.
- Hand files under `data/tft/set<N>/` reference the data's own rows and
  never write numbers (the two literals, Dragon's Claw's "every 2
  seconds" and Hecarim's "3 seconds", are in Riot's text with no row):
  `item-effects.json` (which passives the engine models, and `excluded`),
  `trait-effects.json` (per-breakpoint trait bonuses), `kits.json`
  (dashboard notes per unit: what each driver assumes). Item plain stats,
  omnivamp and durability included, come from the stat line automatically
  (`parse_stat_line`, keyed by the icon).
- A driver is one short Rust struct per unit in `tft_engine/src/drivers/`
  (`driver.rs` lists the hooks, `fight.rs`'s module doc the helpers,
  `drivers/a.rs` one of every pattern): the ability's shape only, reading
  `f.calc(id)` and `f.row(id)`; shields/heals/stuns/bodies go through
  `f.shield`, `f.heal`, `f.stun`, `f.add_body`; ally effects are only
  counted (`f.heal_ally`, `f.shield_ally`). Riftbeast units apply their
  named buff when `f.fx.riftbeast`. Add a unit = a struct + a `DRIVERS`
  entry, a `NAMES` entry and a `with_driver!` arm in `drivers/mod.rs`
  (+ trait-effects entries if its traits are new), then the build script.
  A new set = new drivers, new hand files, `DEFAULT_SET`. Python only
  knows the drivers through `lol_tft.DRIVERS` (api name → driver name).
- Assumptions to remember (all in tft.py constants or noted in the UI):
  AD ×1.5 and HP ×1.8 per star, a 1 s mana lock after a cast (the wiki's
  "can't accumulate mana for the second thereafter" — it blocks attack
  mana and regen alike; a channel a driver declares, Aphelios's 2 s
  onslaught, Ahri's 1.75 s, Varus's 2 s wind-up, locks through its length
  plus that second, and Tristana's charge and Xayah's feathers are locks
  too, since a 0/50 marksman at 10 mana an attack would otherwise never
  leave them), an ability's damage landing when its animation (0.25 s, the
  bins' default for every unit) or its declared channel ends (`Driver.lands`,
  `Fight.after`; so no kill is ever at t=0), curve rows
  hold the previous star's value, fighters' role attack speed at stage 4
  (Riot's 15.4 curve: stage 2–6 = 5/10/20/30/30%), damage amp additive
  and post-mitigation, negative resists floored at 0, Blossom/Elderwood
  "high" stops below the 11-unit prismatic tier, Fae pixies 3 and 7, the
  Riftbeast Alpha Mark on the unit, Primal = Tiger, Zyra's plants attack
  once a second, Mega Gnar casts on 10 mana per attack. Traits that are
  economy or need a specific board (Coven, Elderwood, Sprykin, Blackthorn,
  Emerald Aspect, Old Growth, Greenfather, Rival, Attuned, Bounty Seeker,
  Avatar, Apex Predator) are reported as unmodeled.
- Run `lol.py tft refresh` for an immediate local update. If review is
  needed, inspect `set18/.pending/<patch>/`, update the patch audit and
  justified overrides (or mechanics code), then rerun. Never just rebind
  audit hashes to pass a check. Tests: `python3 -m unittest test_tft
  test_tft_data test_tft_update test_tft_refresh` (needs
  the built engine, reads the archived snapshot; every driver runs
  through every scenario with damage and tank items, and the golden
  fights replay); `cargo test --no-default-features` in tft_engine/ for
  the Rust-side unit tests (pyf's Python semantics).
- The AttackDamage calc convention was wrong in the first version and is
  fixed (2026-09-04): a coefficient is the damage at the unit's base
  attack damage for that star (the rows grow ×1.5 per star like base AD;
  Warwick's 200/300/450 bite is 500% AD throughout), not a percentage
  over 100 — the old reading made every AD ability 1.3–2.5× too weak.
- The 2026-09-05 damage-math audit (prompted by Soraka ranking far below
  Aphelios; `test_tft.TestAuditFixes` pins each fix): overrides.json curve
  rows now reach the calcs' coefficient lists (they never did: every
  patch-note damage correction was silently ignored while `tft check`
  reported it applied); channelled casts land when they end instead of at
  the cast (Aphelios's swipes spread over the 2 s, the blast after; that
  alone was 20–45% of his displayed DPS and made 21 3★ cells "kill at
  0.0 s, DPS 5e12"); starting mana is capped at the bar (three Protector's
  Vows made a 30-mana unit start at 60/30 and chain-cast through the
  lock); regen is blocked for exactly the lock; a second copy of Titan's
  no longer stacks twice as fast, a second Hand of Justice is no longer
  dropped, two Striker's Flails no longer share one cap (Bramble,
  Evenshroud, Edge of Night, Steadfast likewise per copy); an ability's
  damage-over-time crits with Precision like its direct damage; a DoT pays
  for elapsed time only; a cast's attack-speed buff applies from the
  triggering attack; a tick-started cast holds the attack due that
  instant; Akali's AD form lost the AP form's recast; Alune's full moon
  splits over everyone whatever the geometry; Sivir's first bounce leaves
  the target; Elder Dragon's landing no longer ignites twice; Riftbeast's
  capstone stats are modeled (trait overrides go by breakpoint column,
  `"traits"` in overrides.json). Open interpretations, deliberately left:
  attack replacements (Xayah's feathers, Nidalee's javelins, Scuttlecrab's
  dance) are ability damage that crits only with Precision; "N nearest
  enemies" targeting hits one dummy spread out; Solar's bonus is 7% of
  post-mitigation damage, itself mitigated; damage amp does not touch true
  damage (burns); the 3★ rows of every 4- and 5-cost are the PBE file's
  enormous values (Sett 5000, Taric 10000), which is one more reason those
  cells are no longer computed; the K–R fighters and tanks were not
  re-audited (the agent for that slice hit a rate limit).

## Tests

- `python3 -m unittest test_builds` (needs the built engine; ~2 s).
- `python3 -m unittest test_tft` (needs the built TFT engine; ~2 s;
  `TFT_GOLDEN_ALL=1` recomputes every golden cell, ~30 s).
