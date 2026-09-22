# lol-analysis — notes for whoever works here next

## Computation architecture

- Use Rust for heavy computation: simulations, scoring, numerical aggregation,
  expensive search loops and their caches. Keep Python focused on data loading,
  orchestration, CLI/API plumbing and readable verification references.
- Batch work across the Python/Rust boundary. Avoid repeated serialization,
  hashing and validation inside computational loops.
- Profile representative workloads before large rebuilds, and verify numerical
  parity and search coverage when moving computation between implementations.

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
- Buy order (2026-09-18): the top `BUY_ORDER_ROWS` (10) rows of each cell
  carry `buyOrder`, the five items after the boots in a suggested purchase
  order; `items` keeps the enumeration's order (the seeds read it). Every
  partial build (31 subsets, boots owned) is fought at `BUY_STAGE_LEVELS`
  (7/9/11/13/15 by items owned) against `stage_target`s: the full dummies
  scaled down to the dropped first-item preset's shares by level 9, item
  HP lost first. `buy_order` scores all 120 orders by the sum over stages
  of kill time × the next item's gold (overall cells: the geometric mean
  over the three targets). Rod of Ages (`stackedStats`) is pinned first;
  Seraph's/Muramana (`BUY_NOT_FIRST`) are never first, since their Tear
  needs time. Damage only; about 0.06 s a champion; fights and goldens
  unchanged. Tests: `TestBuyOrder`,
  `TestScenarioCache.test_top_rows_carry_buy_orders`,
  `node jobs/test-builds-item-ui.cjs`.
- Fiendhunter Bolts (2026-09-07): `ultAttackSteroid` in item-effects.json
  models Opening Barrage as expected values over the sheet's crit chance.
  The three attacks after R gain 50% AS; one that would not have crit is
  empowered to crit for `critDmgPct` (80%) of the crit bonus, so Infinity
  Edge scales it; one that rolls a crit crits normally and adds
  `trueDmgPct` (15%) of its pre-mitigation attack damage as true damage
  (`barrage` in the breakdown, dealt in the attack's batch and amped once
  like any instance; `Prep.barrage_ev` / `barrage_true_per_ad`). The old
  entry was a fixed 1.6 floor with no true damage, which gave Kayle's
  100%-crit builds nothing but the attack speed and left the item 171st
  vs squishy; it now tops squishy (3-attack kill, 1.07–1.16 s vs 1.21 s)
  and bruiser, is 2nd vs tank. Deliberate model change: the goldens were
  regenerated and only fights holding the item moved (167 of 3,870).
  Readings left open: the 15% counts the attack's own crit-inflated damage
  and no on-hit riders; Kayle's crit-modified waves are not forced to crit
  by the window; the 8 s window and the ult haste never bind in one fight.
  Regression: `test_builds.TestEngine.test_fiendhunter_opening_barrage`.
- The Builds table shows item icons instead of names (2026-09-06):
  `api/builds/meta.json` carries an `items` catalog (`builds.item_catalog`)
  with the ddragon icon URL (version from the snapshot's meta.json), the
  in-game tooltip parsed by `parse_dd_description` into styled text runs
  (never HTML), and item-effects.json's covers/note. The page maps rows to
  entries by item name; without the catalog (an older serve) icons show
  initials. The `#tft-tooltip` element is shared by both tabs and sits
  outside the view sections. Checks: `test_builds.TestItemCatalog`,
  `node jobs/test-builds-item-ui.cjs`.

- Twitch (2026-09-07): the third kit (`data/builds/twitch.json`, `TwitchDriver`
  in `engine/src/drivers.rs`), numbers from the wiki cross-checked against
  Riot's 16.17 character bin. Model: Ambush is cast before the fight and never
  recast — its 40–60% attack speed runs 6 s from the first attack or cask that
  breaks the camouflage (the wiki grants it only on breaking stealth, so a
  recast's 1 s fade costs more than it returns inside 15 s); Spray and Pray at
  t=0 from camouflage, +30/45/60 bonus AD for 6 s and no damage (a damage-less
  ult schedules no impact; `Driver::attack_damage` feeds the bonus to attacks,
  Contaminate's ratio and AD-ratio item actives); Venom Cask at the opening
  and only while the target is short of six stacks (a stack on impact, one a
  second for 3 s); Contaminate the moment six stacks are up and it is off
  cooldown, never early to finish a kill (physical base + per-stack, the AP
  half a separate "E magic" instance); Deadly Venom ticks once a second per
  stack (true damage, 1–5 by level + 3% AP, refreshed by every on-hit, phantom
  hits included, not ability damage). Skill order E > Q > W. Champion
  snapshots now carry `riot.json` (Riot's CharacterRecords/Root via
  CommunityDragon, fetched by `fetch-champion`): `champ_base` reads its AD
  growth where ddragon publishes 0 (Twitch 3; meraki's 25.08 entry still says
  3.1). The engine hooks added for him (`bonus_as(t)`, `attack_damage`, four
  driver events, new `Kind`s after the old ones) left Kayle's and Vladimir's
  fights bit-identical: the golden fixtures were regenerated with his cases
  added and the old 2,565 cases and three runs verified unchanged. A cold
  Twitch tier warms in ~41 s. Tests: `test_builds.TestTwitchKit`,
  `TestTwitchEngine`; the dashboard shows the kit's `notes`.
- Kassadin (2026-09-18): the fourth kit (`data/builds/kassadin.json`,
  `KassadinDriver`), numbers from Riot's 16.18 character bin cross-checked
  against the wiki (V26.18: Null Sphere 80% AP, Riftwalk 80–110; the bin's
  `RiftWalkBaseDamage` row is stale, `RBaseDamage` is live). The first kit
  that keeps a mana pool: the sheet's max mana is the budget, every cast
  pays, a spell that cannot be paid waits, nothing regenerates; Nether
  Blade's empowered attack refunds 20–30% of missing mana (×5, the dummy
  counting as a champion). Riftwalk opens through the engine's t=0 ult path
  (the kit's R has no `damage`, so no single impact is scheduled) and is
  recast on cooldown via `Kind::RCast` while its 40 × 2^stacks cost is there
  (four stacks at most, damage counts the stacks before the cast, +2%/+1%
  max mana); Q and E on cooldown when affordable; W the moment it is up
  (instant; the reset only ever pulls the next attack in; "W onhit" is proc
  damage on every on-hit, "W" the empowered attack's ability damage;
  Muramana on the W cast per V11.8, no extra Eclipse hit); every Q/W/R cast
  takes 0.75 s off Force Pulse; a 0.25 s cast blocks other casts without
  pushing an attack already due later. Skill order E > W > Q: Riot's file
  and 16.18's most played; Q > E > W wins more and gave the same kill times
  at 16. Engine additions, inert for the other kits: sheet `ult_cd_mult`
  from item `ultimateAbilityHaste` (Malignance 20, Fiendhunter 30, Hexplate
  30), `Engine::ult_cd`, `ult_hatefog` (a live zone is refreshed and keeps
  its tick cadence — the wiki's one-zone rule), `mana_cost_mult`
  (Actualizer's `costIncreasePct` 100 during its window) and
  `DamageSpec.max_mana_ratio`. Recasts do NOT reopen Hexplate's Overdrive
  or Fiendhunter's Opening Barrage: the wiki's item data gives them 30 s and
  45 s cooldowns (ddragon's text drops them), so each fires once per fight;
  reopening them per Riftwalk had made AD-crit Kassadin top bruiser/tank.
  Still unmodeled for him: mana regen and Essence Reaver's refund. The
  goldens moved to item patch 16.18 in the same regeneration (the 16.18
  snapshot had left `TestGolden.test_fights` failing on the label alone);
  every Kayle/Vladimir/Twitch case replayed bit-identical at 16.17 and
  again at 16.18. Kassadin's tier warmed in 15–28 s (two cold runs). Tests:
  `test_builds.TestKassadinKit`, `TestKassadinEngine`.
- Survival tier and Dr. Mundo (2026-09-19): tanks rank on how long they
  last, not on damage. Roger's calls: the tank only takes damage (it never
  fights back, so lifesteal, omnivamp, Heartsteel's stacks, Thornmail's
  wounds and Unending Despair's damage count for nothing); the attackers
  are Kayle and Kassadin with their current best builds (the top row of
  their `SURVIVAL_ATTACKER_CELL`, full-overall); Mundo is the first tank
  (`data/builds/drmundo.json`, "role": "tank", numbers from Riot's 16.18
  bin cross-checked on the wiki). Scenarios `survive-kayle`,
  `survive-kassadin`, `survive-overall`: 30 s fights ranked by time to die
  (the kill time; past a survived fight, 30 s + health left / DPS taken);
  overall = fewer attackers that kill it, then the geometric mean. A
  survival cell's hash includes its attackers' cell paths, and `tiers()`
  puts damage tiers first so a warm fills them before it. Engine:
  `engine/src/defense.rs` is the defender (item `defense` objects in
  item-effects.json, never read by a damage fight or the damage boots
  classes; `Guard` = the kit's defenses), wired into `deal` through a
  `#[cold]` `deal_defended` and into a second copy of the fight loop per
  driver (`fight_ex<D, const DEF>`); regeneration, heals over time and
  Death's Dance's bleed are integrated continuously between events with
  the exact death crossing; stasis holds the attacker's attacks and casts
  (`hold_until`); resist changes reset the resist memos and max-health
  changes re-derive what the attacker scaled off it. Mundo: innate regen
  0.4–2.3% max health/5 s, W at the start and on cooldown (8% current
  health; 93%/25% grey storage at 16, healed back whole within 325 —
  Kassadin — or half — Kayle at 625), R at the best of `R_THRESHOLDS`
  (every 10%, and always to survive a killing blow; +5 points per nearby
  champion at rank 3, so 30%/65%); Q and E are not cast. The threshold
  search is exact but runs a probe fight first: thresholds first crossed by
  the same hit share one fight. `survive.rs` (`SurvCtx`) enumerates on std
  threads: 45-item `TANK_POOL` (every item with health, armor, MR or a
  `defense` effect) x 4 defensive boots classes, 7,588,392 builds in about
  21 s on 16 threads. Modeling choices open to revision: Heartsteel zero
  stacks; Zhonya's pressed only against a killing blow; Jak'Sho one stack a
  second (the wiki's strategy line says the first combat grants all five);
  Frozen Heart multiplies the attacker's total attack speed before the cap
  (old forum testing); Death's Dance as an even bleed; a spell shield eats
  the first ability instance; Sterak's decays at a steady rate after 0.75 s.
  Kassadin keeps no mana regeneration, so many builds outlast his mana — his
  cell is largely "does he run dry first". Not modeled: crowd control and
  tenacity, movement, several attackers at once, counter-building. Items'
  "Base Health Regen" now parses (`hp_regen_pct`, in `ENGINE_IGNORES`).
  Damage path cost, pinned A/B against the pre-change engine (CPU 2, one
  thread, a 30-item pool, five interleaved runs): Kayle +0.1%, Kassadin
  −0.8%, Twitch −2.2%, Vladimir +2.4% — the defended copy of the loop gives
  each driver's rotation methods a second caller, which costs Vladimir's
  single-call-site inlining (a runtime flag instead measured +4–7%);
  damage goldens unchanged. Editing item-effects.json re-keys every cell,
  so a serve still running unchanged code re-warms (it did during this
  work). Tests: `TestSurvivalEngine` (every mechanic hand-computed against
  a clean level-1 Kassadin), `TestSurvivalSearch` (probe = grid),
  `TestSurvivalEnumeration` (pass = one fight per build),
  `TestScenarioCache.test_survival_cells`, `TestSurvivalGolden`
  (`data/builds/golden/survival.json`, `jobs/gen_golden.py --only
  survival`), `node jobs/test-builds-survival-ui.cjs [--cell file]`.
  `lol.py builds survive drmundo --items ... --attacker kayle` prints one
  fight.

- Machine-written champions (2026-09-19): every champion without a
  hand-encoded kit has a kit and a rotation driver written by Claude Sonnet
  (`jobs/kit_driver.py write <slug>... | --all`), from its dossier
  (`data/builds/dossiers/<slug>.json`, written by `jobs/kit_dossier.py` and
  checked by `jobs/kit_sources.py` against the archived bin and wiki under
  `data/builds/sources/`; that README has the whole pipeline and its
  measurements). They are UNREVIEWED drafts: the kit says `"generated": true,
  "reviewed": false`, the Builds tab finds them through a search box (the
  hand-encoded champions stay buttons), tags the selected one "unreviewed" and
  shows a banner with the kit's `assumed` numbers and `unused` abilities; the
  kit's `notes` spell out the rotation it plays. Written blind for the four
  hand-modeled champions, such drivers ranked 120 random builds like the
  hand-written ones where the rotation agreed (Spearman 0.90-0.97 for Kayle,
  Twitch, Kassadin, kill times 0-15% slower) and unlike them where a ruling
  differed (Vladimir 0.26-0.51: the blind driver basic-attacks, the hand kit
  rules `attack.never`). Known weak spots: pets (Naafiri's driver fights
  without her Packmates), max orders (Garen's maxes Q over E), and anything
  under `assumed`. Reviewing one = reading its notes, fixing the kit or the
  driver by hand (or `write <slug> --force`), and setting `"reviewed": true`.
  How it is built: a generated driver is ONE file,
  `engine/src/generated/<slug>.rs`, exporting `pub struct GenDriver` that
  implements the same `Driver` trait as the hand-written ones; it reads its
  numbers from the kit by dotted path (`Kit::num/at_rank/at_level/hit`, the
  kit keeps them under `gen.*`; no per-champion parsing code), schedules its
  own events as `Kind::Ev(i)` (ranked after every named kind, before the
  defender's `R_DEF`; `MAX_EVENTS` 8), and runs behind one vtable
  (`dyn_driver.rs`, `Rotation::Generated`) so the fight is monomorphised once
  for all of them, not 170 times. `generated/mod.rs` is rewritten from the
  directory by `kit_driver.py registry`: never edit it. `generated/jax.rs` +
  `data/builds/jax.json` are the hand-written reference the model is shown,
  `jobs/kit-driver-guide.md` what it is told. A candidate is linted (no game
  number in the Rust, no std, no unsafe, state in `s`/`s0` with `reset` =
  `self.s = self.s0`; every kit number traceable to the dossier, the sheet or
  the wiki, or listed under `assumed`), compiled in a private copy of the
  engine, then fought (`kit_driver.py check`): five levels, five item sets
  against the three presets, with and without the ult, no panic, no endless
  event (the fight loop aborts one that keeps coming due: a generated driver's
  classic bug), the breakdown adds up, damage never falls as the fight
  lengthens or an item is added, every damaging ability shows up or is waived
  under `unused`. Failures go back to the model with the history of earlier
  rounds (without it Yunara relabeled one damage source back and forth for
  five rounds). Of 172 drivers 129 passed on the first round and not one round
  failed to compile; about $100 cost-equivalent on the Claude plan through
  `claude -p` (no API key), $0.25 for a simple kit, $4.87 for Aphelios.
  `kit_driver.py compare <slug>` is the blind-vs-hand measurement, `annotate`
  sets `manaless` from the wiki's resource. Dr. Mundo is reserved (his
  hand-written tank kit). The hand-written drivers and both golden sets are
  untouched by all of this.
  Cache keys: a cell keys on `CORE_HASH` (builds.py + the engine WITHOUT
  `src/generated/`, stamped by build.rs) plus, for a generated champion, its
  own driver's hash (`GENERATED_HASHES`): writing or rewriting one driver
  leaves every other champion's cells warm, while an edit to builds.py or the
  engine core still recomputes everything — with the roster that is about two
  hours on this box (20-70 s a champion), so expect it after any such commit.
  The warm computes the hand-encoded champions of every tier first, then the
  roster; a generated driver that errors in the enumeration gets an error cell
  (the page says so) instead of staying cold and respawning the warm.
  `cell_paths()` is memoized on the files and settings it reads (it is asked
  on every status poll). `SHOW_GENERATED = False` takes the roster off the
  dashboard. Tests: `python3 -m unittest test_kit_driver test_kit_sources`,
  `node jobs/test-builds-generated-ui.cjs`.
- Leaderboard (2026-09-20): the Builds tab's second view ("Champion builds" /
  "Leaderboard") compares champions under one damage scenario: a row per
  champion = the TOP ROW of its cell (its best build by that scenario's own
  ranking), ranked across champions by the same metric — a target's board by
  expected kill time, then most damage for champions that never kill; overall
  by targets left standing, then the geometric mean. Only the rounded numbers
  a cell keeps are available, so equal values share a rank (no
  damage-to-spare tie-break across champions). It is read-only over saved
  cells and lives in `builds_leaderboard.py`, NOT builds.py, on purpose:
  builds.py's bytes are in every cell's key, so an edit there re-warms the
  roster (~2 h) and the board needs nothing a cell lacks. Keep it that way; a
  change that needs new cell fields is a builds.py edit and pays that warm.
  `cached_leaderboard(key)` memoizes each cell's top row on (path, mtime,
  size): ~0.8 s for a scenario's first request after a serve restart (172
  half-megabyte cells), ~25 ms after; cold cells go under `pending` (ranks
  provisional, the page polls), error cells under `failed`. Served at
  `/api/builds/leaderboard/<scenario>.json` — the route must stay BEFORE the
  cells' `/<slug>/<scenario>` route, which would read "leaderboard" as a
  champion (an older serve answers 404 and the page says to restart) — and
  written by `lol.py export`. The Survival tier has no board (one tank).
  Page: selecting a row makes that champion the tab's (breakdown, the draft
  banner in the breakdown card, pool and kit notes below follow it); its name
  opens its ranked builds; Find and "Reviewed kits only" filter without
  re-ranking; the hash carries `bview=leaderboard`, `lbq`, `lbrev`, `lbs`
  (+ `lbd=d` descending).
  Sorting (2026-09-20): every heading but the build is a sort button
  (`boardColumns`' `key`/`sort`, `boardSort`, `boardSorted`): best first, a
  second select reverses, # stays the scenario's rank. Page-only — nothing
  in builds_leaderboard.py or a cell changed. A target's column sorts on
  [target left standing, the time shown], so a ≈ time comes after every kill;
  a blank is last either way; equal values keep rank order. `vs-<target>` is
  one key on every board (a target's own "Kill time" included), so the order
  outlasts a change of scenario; a column the board lacks (Mean, DPS, Damage)
  leaves it as ranked. Mind what it compares: the overall board by "vs tank"
  orders each champion's best OVERALL build by its tank fight, whereas the
  vs-tank scenario ranks each champion's best build against the tank.
  What the first board showed (2026-09-20): ten machine-written kits (Ahri,
  Annie, Aurora, Cho'Gath, Heimerdinger, LeBlanc, Lee Sin, Malphite, Mel,
  Nidalee; Blitzcrank too vs squishy) kill the squishy at 0.00 s — their
  drivers land the whole opening combo at t=0 with no cast or travel time
  (Ahri: E, Q, Q return and R, zero attacks, DPS 0) — which zeroes the
  geometric mean and ties them at #1 overall; Ezreal (0.22/0.51/0.73 s, no
  attacks) and others near the top are the same kind of draft. The board
  shows these as they are, marked "instant" and "unreviewed": fix the
  drivers (or teach `kit_driver.py check` to reject a t=0 kill), never the
  board. The hand-encoded four rank 57 (Kassadin), 72 (Kayle), 99 (Twitch)
  and 170 (Vladimir) of 172 overall. Also fixed here: `.item-icon` isolates
  its stacking context, so buy-order badges no longer paint over the sticky
  table header when a row scrolls under it (both tables). Tests: `python3 -m
  unittest test_builds_leaderboard`, `node jobs/test-builds-leaderboard-ui.cjs
  [--board saved-leaderboard.json]`.
- Cast times in the machine-written drivers (2026-09-20): those 0.00 s kills
  are fixed in the drivers, as that note asked. What was wrong: the engine has
  no cast time of its own (`e.deal` lands at the clock, `e.lockout()` only
  pushes the next ATTACK 0.25 s; the `castTimeS` fields in the hand-written
  kit files are read by nobody), so a rotation is only sequenced if its driver
  does it. The hand-written kits do (`busy_until` in Vladimir and Kassadin);
  the guide never asked for it and Jax, the reference, has no basic ability
  with a cast time, so most generated drivers cast everything that was ready
  in one instant and the build search optimized for it. The rule now, the
  hand-written kits' own: CASTS GO ONE AFTER ANOTHER — a cast takes effect as
  it starts (as the engine's attack lands at the start of its cycle) and then
  keeps the champion busy for its cast time, the dossier's `castTime`, not a
  flat 0.25: no other cast, no attack; an ability without a cast time costs
  nothing but still waits for a cast in progress; a delay the sources put
  after the cast (Rupture's 0.625 s) is an event. It is in
  `jobs/kit-driver-guide.md` ("Cast times", with the `castable_at` /
  `busy_for` pair every driver should carry) and in `generated/jax.rs` (the
  ult's swing lands with the cast and holds Leap Strike and Counter Strike
  0.25 s). `kit_driver.py check` enforces it: `cast_floors` takes each
  ability's cast time from the dossier (the shortest the wiki states for the
  first cast; a kit overrides one by listing `gen.<slot>.castTimeS` under
  `assumed`), and where the wiki says nothing AT ALL (not "none" — nothing)
  Riot's own `mCastTime`, which `kit_sources` carries into the numbers sheet,
  fills in (`sheet_cast_times`). Two checks use them. `lint_cast_times` is the
  one that matters: EVERY ability with a cast time must carry it in the kit
  and have the driver read it from there, or declare the slot `unused`. The
  other, `first_landings` + `cast_time_errors`, reads the opening off fights
  of growing length (0.01 s steps, with the ult and without) and requires that
  of the abilities landed by a time T all but the longest were cast and
  finished inside T — sound whenever their damage lands, but only a check on
  casts that OVERLAP, so it cannot see a lone cast that costs nothing (Diana's
  Q: her W and E really are instant, so her free Q had nothing to overlap
  with). That blind spot hid 133 of the 367 abilities with a cast time, which
  is why the static lint exists. Also a symptom guard: neither `BURST_SETS`
  build may leave the squishy dead at t = 0. `check --all` runs
  every admitted driver against the built engine (4 s): run it after changing
  a check. `write --repair <slug>...` / `--failing` sends an admitted kit and
  driver back to the model with the checks they fail instead of starting from
  nothing (`--force` still does); it keeps `_provenance` and now sets
  `manaless` itself. Measured, in two rounds: the overlap check failed 54 of
  172 (8 of them killing at 0.00 s), all repaired, 52 on the first round
  (Renata second, Nidalee — two forms — fourth), $16 with 5 workers; then the
  static lint failed 83 more, all repaired on the first round, $18 with 3
  workers. 137 drivers in all, about $35. The diffs are
  small (the gate, the cast times read from the kit, here and there a delay
  the dossier states), and the repaired champions' old top builds kill the
  squishy a median 0.25 s later, bruiser and tank unchanged in the median
  (attack-heavy kits got FASTER: `lockout()` had pushed the next attack
  0.25 s per cast even when it was due later). Only the repaired champions
  and Jax re-keyed, warmed by hand (53 champions in 40 minutes, then 83 in
  55). The board after both: no kept row of any cell kills at 0.00 s; the ten
  kill the squishy at 0.25-1.25 s and rank 1 (Heimerdinger, below) to 99
  (Cho'Gath) overall; the hand-encoded four rank 54 (Kassadin), 69 (Kayle),
  96 (Twitch) and 169 (Vladimir). Over the 127 repaired champions' ORIGINAL
  top builds the kill times are close to a wash — median +0.00 s on squishy
  and bruiser, -0.25 s on tank, and MORE got faster than slower (78 vs 31 on
  the tank) — because `e.lockout()` had spent a flat 0.25 s of attack time on
  every cast while `busy_for` holds the attack only to the end of the real
  cast. Biggest moves: Jhin +2.00 s on squishy (his ult is now a channel),
  Urgot -3.10, Sylas -3.16 on bruiser, Mel -2.45 on tank. Known limits, deliberately left: damage still lands at
  the START of a cast, here and in the hand-written four, so every kill time
  is early by about one cast time (a stricter rule, damage at the END of the
  cast, fails 71 more drivers that already follow the hand-written
  convention; making it the engine's would re-key everything and move the
  goldens); missile and dash travel are not modeled; an opening ult with a
  long cast now holds the rotation (Ezreal's 1 s Trueshot Barrage, Jhin's
  Curtain Call: legal, worth a review — the guide now says `cast_r` may leave
  the ult for later); Heimerdinger tops the overall board (0.25 / 0.50 /
  2.81 s) because `add_charge` fires a beam for every 100% of charge and a
  20-rocket swarm grants 400% at once: four beams in one instant, a modeling
  flaw of his own (the dossier leaves the beam's charging unsettled; capping
  the charge at 100% is the likely fix). Seen once while warming by hand: after a
  champion's enumeration reached 100%, one pool worker sat in a futex wait
  and the parent waited on it for ever (no cell written for 8 minutes);
  `kill -9` of the worker let the warm carry on and the cells were right.
  Not diagnosed (no ptrace here). A guess: `warm()`'s Python-level SIGTERM
  handler is inherited by the forked workers, so `Pool.terminate()` runs
  `sys.exit(143)` inside them wherever they are, and cannot end one that is
  blocked below Python; the same warm logged "Exception ignored in atexit
  callback ... SystemExit: 143" from workers four times. Resetting SIGTERM
  to the default in `_enum_init` would be the fix (a builds.py edit: it
  re-keys every cell).
  A trap worth remembering: `Bench.evaluate` runs the checks in a subprocess
  that used to find the driver by NAME under `engine/src/generated/`, so a
  check reading the Rust judged the ADMITTED driver, not the candidate — the
  static lint was then unsatisfiable by editing the driver and 8 champions
  burned all five rounds on one unchanging message ($12). It passes
  `--driver` now; anything else that reads a candidate's files must too.
  Tests: `test_kit_driver.TestCastTimes`, `TestRepair`, `TestReferenceDriver`.

- Ezreal's projectiles (2026-09-21): the first generated driver with a flight
  time, hand-edited after Roger pointed out that his cooldown refund was
  instant. What was true: `e.deal` lands at the clock, so all four of his
  abilities hit the dummy the instant they were cast and Mystic Shot's 1.5 s
  refund was applied there too. What the refund's timing is worth depends on
  the regime, and both halves are checked: where the cooldown left after a
  cast is longer than the flight, NOTHING — the refund only subtracts 1.5 s
  from a cooldown with more than a flight to run, so Q's cadence is
  `cd - 1.5` either way (an AD-crit build casts Q at 1.00/4.00/7.00/10.00 s
  before and after). Where it is shorter, the refund used to arrive before
  the bolt did: at maximum basic-ability haste (110 with Shojin) inside
  Actualizer's 30% window Q's cooldown is 1.57 s, so the refund left 0.07 s
  of it and the 0.25 s cast time was all that held the next Mystic Shot back
  (that one follows from the old code, not from a run of it); Q now comes up
  exactly when its own bolt lands and goes out every 0.275 s, measured. The model: a cast starts a
  flight of `attack range / the wiki's missile speed`
  (`gen.<slot>.missileSpeed`: Q 2000, W 1700, E's homing bolt 2000, R 2000 —
  0.275 s at his own 550 range, 0.324 s for W) and the damage, the Rising
  Spell Force stack, the Essence Flux mark and the refund all wait for the
  arrival, while the cooldown still starts at the cast, as in game. The
  distance is the one the engine already assumes for a target (`Prep::build`
  scales Hexoptics' Magnification by the attack range); E's 475-unit blink is
  NOT modeled, so its bolt flies the full standing distance, the slowest
  reading. Because the refund can bring an ability back up before its own
  projectile has landed, each ability keeps a board of the flights in the air
  (`IN_FLIGHT` 4, `launch`/`arrived`/`soonest`) instead of one pending
  arrival; a full board lands the projectile at once rather than lose it, so
  the slot count cannot cost damage. Two other things wrong in the same
  driver were fixed with it, both inert in a 15 s fight (R is not recast):
  Trueshot Barrage applied Mystic Shot's refund, which it does not, and a
  recast R did not prime Sheen. One that is not inert: Essence Flux's
  application granted no Rising Spell Force stack although the kit's notes
  and the wiki both say every ability hit does (the cap is reached either way
  in most fights — the naked level-16 fight is unchanged to the unit).
  Effect on his cells: the squishy winner is the same build 0.25 s slower
  (1.45 -> 1.70 s), and the ability-haste builds that had won the longer
  fights lost their edge — the old #1 overall (Ionian Boots, Seraph's,
  Actualizer, Lord Dominik's, Eclipse, Muramana) is now 17th with a mean of
  2.37 s instead of 2.02, the old #1 vs tank is 61st (2.75 -> 3.30 s), and
  the new winners — Infinity Edge with attack-speed boots in place of the
  ability-haste boots and Eclipse — were 18th, 244th and outside the kept
  rows. On the leaderboard he goes from where 2.02 would have put him, 39th,
  to 65th of 172 overall, and 17th to 27th vs the tank. The kit stays
  `"reviewed": false`: the dossier's open questions about the detonation's
  on-hit procs are untouched. Only Ezreal re-keyed (the driver hash), so a
  hand warm of his four cells (40 s) was the whole rebuild.
  `jobs/kit-driver-guide.md` still tells a model NOT to model flight, and now
  says a `--repair` of Ezreal must keep his. Checks: `python3
  jobs/kit_driver.py check ezreal` and `check --all` (0 of 172 fail),
  `python3 -m unittest test_builds test_kit_driver test_kit_sources
  test_builds_leaderboard`, the four Node builds harnesses.
- Actualizer is out of the damage pool (2026-09-21): Roger's call — he does
  not want the item in his builds. `DEFAULT_POOL` is 75 items, and the reason
  sits beside every other exclusion, under `"excluded"` in item-effects.json,
  so the Builds tab prints it. Only the ENUMERATION lost it: the engine still
  models Mana Made Real (`manaActive` — the amp, the 30% faster basic
  cooldowns, the doubled mana costs), `builds sim --items actualizer` still
  fights with it, `test_actualizer_doubles_what_casts_cost` still passes, and
  putting it back is one line in each file. It was never in `TANK_POOL`, so
  the Survival tier's own pool is untouched — but Dr. Mundo's cells still
  move, because his attackers are Kayle's and Kassadin's best builds and
  Kassadin's had the item. How much it had been winning: of the 660 cells
  whose top row was captured before the warm pruned it (all but the
  hand-encoded four, Dr. Mundo, Aatrox, Ahri and Akali, recomputed before the
  snapshot), Actualizer was in the #1 build of 131 — a fifth of the board.
  The pool is in builds.py's bytes and item-effects.json is in every cell's
  key, so this re-keys all 691 cells: the whole roster is re-warmed by hand
  after a change like this, about 9 cells a minute on this box.
  The goldens need nothing: both fixture sets carry their own item lists
  (`test_enumerate` passes `candidates=run["pool"]`, `TestSurvivalGolden` its
  own attacker builds), and all of them replay unchanged. `test_manaless_pool`
  now asserts the item is out of the pool and that Vladimir drops two mana
  items rather than three. `TestSurvivalSearch` still feeds Kassadin an
  Actualizer build as a fixed input: a mechanics fixture, not the live best
  build.

## The One-tricks tab (onetricks.py)

- Counts, per champion, the players on each crawled region's latest Master+
  ladder (lol-quant's ladder ledger: last full frame plus later deltas) for
  whom that champion is more than 85% of their ranked solo games in a role
  (the crawl's `role` is Riot's per-game `teamPosition`), with at least 20
  games on it. The denominator is the role's games, not all games as
  onetricks.gg uses: Roger's definition. The season is the crawl's patches
  of the newest major version. The total is distinct players; one who
  one-tricks the champion in two roles is in both role columns (common:
  Yasuo, Vel'Koz, Katarina).
- `lol.py onetricks sync` replaces the `onetricks` and `onetrick_sync`
  tables; `jobs/sync-scaling.sh` runs it after the scaling sync, so it
  rides the existing six-hourly timer. It streams the parquet through a
  pyarrow grouped count: ~18 s, ~8 GB peak, most of it the metadata of the
  crawl's ~28k uncompacted parquet files (a one-column scan alone peaks
  near 5 GB). Serve only reads the tables (`/api/onetricks.json`).
- Tests: `python3 -m unittest test_onetricks`, `node jobs/test-onetricks-ui.cjs`.

## The TFT tab (tft.py, tft_engine/, data/tft/)

- One formation for every fight (2026-09-21): the damage board was three
  dummies, ALL of them flagged `nearby`, so every "enemies within N hexes"
  effect covered every enemy that had to die — and the ranking is time to
  clear the board, so a 1-hex cloud on 3 of 3 was paid three times its face
  value. Roger spotted it on Gromp (his whole ability is such a cloud: 61%
  of his damage, 5th of 42 clumped against 22nd spread) and asked for the
  tank board's shape. Damage tests now use it: `FRONTLINERS` (3) median
  tanks, the heaviest first, screening `BACKLINERS` (2) median non-tanks,
  and ONLY the frontline is `nearby`. Nothing in the engine was added — the
  distinction already existed and was inert: `f.aoe*`/`f.adjacent` respect
  the flag (so they reach the three in front, or only the current target
  when the frontline is spread) while `f.alive()` and `f.nearest(n)` never
  did and still reach all five. `TANK_FRONTLINERS`/`TANK_BACKLINERS` were
  renamed `FRONTLINERS`/`BACKLINERS` because both boards use them; the two
  boards now differ in who ATTACKS, not in who is standing there. Knock-ons,
  all deliberate: dummy health 6,240 -> 9,480, so `FIGHT_DURATION` is 30 s
  (at 20 s, 27 of 42 units stopped clearing and the board collapsed into its
  damage tiebreak; at 30 s five do, against three before); on the damage
  board the backline is a target only (slot `streams` 0, read by `cell_spec`
  when the objective is not tank), so a fighter still eats the three
  attackers in front of it, though its pressure moves 200 -> 172 because
  those three are now three frontliners rather than two tanks and a carry;
  a tank `threat` arms all five as before. What moved on the clumped bare
  board: Gromp 5 -> 18, and the large-radius kits fall hardest — Kennen
  12 -> 38, Diana 9 -> 31, Lux 27 -> 42, Ahri 29 -> 40 — while whole-board
  and nearest-N kits rise (Alune 33 -> 13, Mama Beak 28 -> 12). THAT IS THE
  KNOWN LIMIT: `nearby` is a boolean, the engine's only notion of distance,
  so a 1-hex cloud and a 3-hex bomb are still treated alike — both capped at
  the frontline now instead of both reaching everything. Right for the many
  small-radius abilities, wrong for the few large ones (Ahri `HexRadius` 3
  aimed at the densest cluster within 4, Kennen's 2-hex firestorm). The fix
  is per-ability radii, which needs a hand file: only 9 of the 30 drivers
  using a nearby-limited helper have a radius row in Riot's data, the other
  21 (Gromp included) state it in tooltip prose. Both golden sets were
  regenerated. `TestNoTimeline`'s frozen flat-cast numbers now build the old
  three-slot board and 20 s fight themselves, so they keep pinning the cast
  rules alone. Prose, the dashboard's fight diagram and the CLI's dummy line
  all show the two lines. Details: `data/tft/README.md` "One formation for
  every fight".
- Gromp's cast animation, still open (2026-09-21): TFTraits states no effect
  time for him, so the engine lands his bubble at the bin's `mCastTime`,
  which is a flat 0.25 for all 63 units in `bins.json` — inside a 1.02 s
  animation. Landing it at the end of the window, or at 0.25 s plus his own
  missile flight (`missileSpeed` 1800 over `rangeWorld` 890 = 0.49 s), both
  give the same cell and cost him about 1 s of kill time. 24 form timelines
  have no effect time; re-ranking the old board under the late-landing rule
  moved Gromp most of anyone near the top (5 -> 11), then Karma and LeBlanc.
  NOT changed: it is a systemic default, not a Gromp fact, and the formation
  above was the larger half of his number. Missile flight is not modeled for
  any unit (40 have a `missileSpeed`).
- Leaderboard audit (2026-09-20). Roger asked whether the unit-damage board
  was off; it was, for three independent reasons, and all three are fixed
  here. Read the three bullets below together — they were one change, and
  both golden sets were regenerated once at the end of it.
- Per-unit cast timelines (2026-09-20): a cast is no longer a flat 0.25 s
  animation and a one-second lock. `data/tft/set18/cast-timing.json` carries
  every unit's cast animation, channel, effect time, attack delay and
  recovery, transcribed from TFTraits' published cast timelines — a third
  party whose own disclaimer is "not 100% accurate: they combine publicly
  available game data with gameplay observation", adopted as a model rule
  with the standing of Azir's six-command lock and the 0.5 s melee
  reposition: NOT verified in game. The rule: the fight's first attack lands
  its attack delay in; a cast the attack made possible starts at that
  attack's unlock point (landing + recovery); from there the unit neither
  attacks nor gains mana from any source for the animation and any channel;
  the ability lands at `effectAt` inside that window; when the window closes
  a fresh attack starts, even earlier than the old period would have allowed
  (the cast cuts the rest of the attack — Rengar's 0.35 s animation is an
  animation cancel). Delay and recovery scale with attack speed; the cast
  windows never do. A driver may take the window over and the engine never
  overrules it afterwards (Pebbles' drain; Azir/Murkwolf/Nidalee's held
  bars). A lock that lasts "as long as the effect runs" is measured from the
  LANDING, where this engine starts an effect, so it ends up to the effect
  time later than the seconds TFTraits prints from the cast (Diana +0.66 s,
  Mama Beak +0.27 s, Brambleback +0.25 s). A unit the file gives no timeline
  (Kayle, Caitlyn's and Master Yi's casts, summons, synthetic fixtures, the
  dummies) keeps the flat `MANA_LOCK_S`/`CAST_TIME_DEFAULT` rules bit for
  bit. Only the `lockRule`s that are a fixed time from the cast
  (`castAnimation`, `channel`, `untilEffectEnds`) are applied by the engine;
  `effectDuration`, `shieldHolds`, `empoweredAttacks` and `none` belong to
  the unit's driver. Two constants moved with it: crit chance over 100%
  converts at 0.8, not 1.0 (Riot's patch 13.18 "increased from 50% to 80%
  conversion", the latest primary statement; TFTips' Set 18 page still says
  ×0.5 — unverified for Set 18), and Blue Buff's "10% additional AD and AP
  from all sources" multiplies the BONUS only, not the base (adopted by
  analogy with Adaptive Helm's "additional Mana from all sources"; the item
  note says so). The file enters `cell_paths`, so editing it re-keys every
  cell. Acceptance: for the 27 units whose bar and mana rate match TFTraits'
  assumptions, the engine's first cast is within 0.01 s of their published
  figure; the other 35 differ only because TFTraits times every unit from 0
  mana. Effects: the damage board's median DPS falls ~8%, long animations
  fall and manaless attackers rise (Alune 7→34, Soraka 35→42, Master Yi
  22→13, Caitlyn 27→16, Rengar 23→4), and the tank pressure budget,
  calibrated from two production carry fights, drops from 1277 to 1109 raw
  DPS, so every tank and fighter fight now takes ~13% less incoming damage.
  `lol.py tft sim`/`units` print each unit's window. Tests:
  `test_tft_cast_timing`.
- Driver mana locks, Pebbles' drain and targeting fixes (2026-09-20): a unit
  no longer buys its next cast back while its own effect still runs. From
  TFTraits' per-unit statements (third party, adopted like Azir's, not
  verified in game), written as absolute times or an Azir-style hold released
  with `lock_until = f.t` — never `+ MANA_LOCK_S`, which is gone from every
  driver: Nidalee's AP javelins (3 empowered attacks — she used to refill her
  bar on her own javelins and never leave them, which is most of why she read
  858 DPS at #2), Murkwolf's empowered attacks (2), Mama Beak's flock (its
  AP-scaled duration), Brambleback's Frenzy (8 s), Diana's barrier while it
  holds (≤ `ShieldDuration`), Shen's three ki strikes, Elise's
  `ASBuffDuration`, Vi's `SpellDuration`, and the shield tanks Ornn, Rakan,
  Sejuani, Rammus, Malphite and Sentinel (locked while the shield stands,
  their own duration row at most, released the moment it is fully absorbed —
  `helpers.rs` `ShieldLock`/`shield_lock`/`shield_lock_broke`). The lock
  blocks attack mana, the regen ticks and the 1%-pre/3%-post mana a tank
  takes off damage alike; `gain_mana_opt(.., false)` procs (Protector's Vow)
  still bypass it. Tristana and Xayah lose the extra second after their
  effect. This kills the audit's tank artefact: Rammus 3★ and Malphite 2★ had
  a 60-second survivor with Adaptive Helm + two Archangel's (22–23 casts,
  re-shielding off damage-taken mana while the last shield still stood); no
  tank has one now, in any threat preset. Pebbles is the one unit the audit
  found UNDERSTATED: Azure Laser is a drain, not a fixed channel — the bar
  she cast with, overflow included, drains at `PercentManaPerSecond` × max
  mana with no lock, regen lengthens the laser, the channel ends at the exact
  instant the bar empties, and a channel whose regen outpaces the drain runs
  to the end of the fight. She goes 40→31 on the board, and the model
  reproduces tftflow's "~17.1 mana regen to go infinite". Also: Alune's full
  moon is every 5th cast, not every 4th (five phases in `extras.traitTooltips`
  `DA_AluneUniqueTrait18_Tooltip_Phase1..5`, and Attuned "cycles to a new
  phase after each cast"; New Moon still assumed at combat start); Murkwolf no
  longer reads `NumEmpoweredAttacksGainedOnKill`, which no tooltip mentions (a
  dormant row is not a mechanic); Diana's leftover orbs are lost instead of
  reaching a dummy that was never within two hexes spread out; Morgana's blast
  uses `nearest(3)` like every other N-separate-enemies spell; Aphelios's
  swipe count survives 0.6/0.2 = 2.9999…; Ashe's %max-Health trail and
  Draven's cashed bleed crit with Precision like the ticks they replace.
  Tests: `test_tft_driver_fixes`.
- The refresh was stuck for ten days, and one correction was on the wrong row
  (2026-09-20): forty consecutive refreshes failed on `XP From Level 8-9`.
  Two defects met. `tft_update._outside` knew the XP table only in the main
  article's shape; `tft.PatchNotesParser` knew only 18.1's hotfix shape
  (`<h2>Mid-Patch Updates</h2>` with an `<h3>` per date), so 18.2's
  `<h2>MID-PATCH UPDATE</h2>` + `<h4>SEPTEMBER 14</h4>` left `updates` empty
  and the candidate kept the label `18.2` — and a same-label candidate never
  loads a review file, so NO manifest could have unblocked it. Both shapes are
  recognised now and the candidate is `18.2b`. The same audit found Riot's
  "Nidalee Empowered Attack Damage 285/425 ⇒ 300/450" mapped onto
  `EmpoweredDamage` (the ordinary javelins, 170/255) instead of
  `ThirdAttackEmpoweredDamage` (320/480): published AP Nidalee threw three
  300/450 javelins. Both signs were on the page, so `tft_update` now refuses a
  numeric note mapped to a row more than 5% from the note's old value while a
  sibling row of the same entity/form is at most half as far, or whose sibling
  the staged lookup already moved to the note's new value, unless the mapping
  carries `siblingRowAcknowledgement {rows, reason}`; `_row_semantics` no
  longer drops every row with "attack" in its name, only `…AttackDamage…`.
  `tft.source_disagreements` compares the eight base stats a unit fights with
  against the archived CommunityDragon export (Adaptor forms resolved as
  `kit_spec` does): `tft check` lists them, and once an audit carries
  `sourceCrossCheck` an unreviewed one fails the check and blocks an
  unattended publication — the published 18.2 ran on MetaTFT's 2026-08-16 PBE
  build with Kha'Zix at 850 health (live 950) and Pebbles at 30 attack damage
  (live 35), with the export that said so archived beside it. The manifest
  gained `lookupChanges` (a value the regenerated lookup moved with no note
  behind it), `definitionDispositions` and `sourceDisagreements`;
  `policyVersion` is 2. `18.2b.json` decides 6 mid-patch mappings, 32
  dispositions, 165 coordinates and 9 source disagreements — 59 of those
  coordinates rest on the lookup alone (42 are 4★, which nothing enumerates)
  and say so. Tests: `test_tft_update_guards`.
- Automatic update recovery (2026-09-10): `tft_http.py` bounds each download
  to four attempts/90 seconds, including DNS and body reads. Only exhausted
  transient fetch failures exit 75; review-required exits 2 and permanent
  failures exit 1. `lol-tft-refresh` is a USER service, with a four-hour
  startup limit and exit-75-only retries after five minutes (three starts
  per 30 minutes). The Nix declaration and checked-in drop-in under
  `jobs/systemd/` must stay aligned. Never restart the system dashboard by
  hand to run an update; let complete publication signal its existing path.
  Status retains recent failures and a pending review blocker separately.
- Patch reviews: `data/tft/set18/patch-reviews/<patch>.json` explicitly binds
  corrections and scoped mechanics dispositions to the downloaded source
  hashes and previous audit. `tft_update.py` validates the prior values,
  exact note identities, raw definition changes and final target values.
  Do not refresh hashes merely to approve new evidence. Preserve forms,
  star levels and trait columns; split superseded checks rather than leave
  conflicting expectations. Compound formulas are not automatically reusable
  numeric mappings. See `18.2-review.md` for source conflicts and retained
  geometry, terrain and summon-timing limitations. Regressions:
  `test_tft_{http,patch_parser,patch_review,update,refresh}.py`.

- Melee movement estimate (2026-09-09): the finite damage benchmark declares
  `MELEE_REPOSITION_SECONDS = 0.5` in `tft.py`. Equipped ranges 1–2 wait that
  long at initial engagement and after their current target dies; ranged
  forms (including AP Nidalee at range5) are unchanged. This is the user's
  accepted shared approximation, not decoded walking or jump timing. Rust
  tracks an independent arrival gate; attack cooldowns overlap it, new casts
  wait for arrival, and existing damage/regen/incoming pressure keep running.
  Akali AP recasts, Brambleback leaps and Yi's transferred second strike resume
  through `Driver::target_changed` at arrival. Superseded moves are cancelled;
  collateral kills do not move the actor. Explicit zero-delay inputs retain
  the previous stationary behavior. Tank presets and immortal composition
  probes do not enable this finite-target policy. Saved cells/leaderboards
  expose the assumption and actual elapsed movement time; old payloads retain
  their original explanation. See `data/tft/set18/melee-movement.md` and
  `test_tft_movement{,_chains}.py`. This change's champion cells are rebuilt
  independently; a full composition/site publication is a separate rebuild.
- Percentage HP (2026-09-09): ordinary item and trait bonuses share one
  additive pool: `(star-scaled base HP + flat HP) * (1 + sum(bonus HP %))`.
  The existing `hpMult` input keeps factor notation; 1.18 contributes 0.18.
  Both item and generic trait paths use `Fx::add`; timed flat HP uses the
  combined factor once. Spell calculations based on current max HP remain
  separate. The user selected this interpretation after reviewing historical
  in-game evidence; current Set 18 operation/channel decoding is unresolved.
  See `data/tft/set18/hp-stacking.{md,json}` and `test_tft_hp_stacking.py`.
  Engine `cfe721d84f2e`: 893 TFT tests with one existing skip, eight Rust tests,
  and complete reference/native item-search parity at 3,242/4,183 allocations.
  All 6,053 previous fights with at most one ordinary HP bonus stay identical;
  the 1,617 multi-bonus fights change. Regenerated ranked cells change in
  1,079/1,770 cases. Published `g-1b8d8c0db83f` passes all 1,664 core / 416 cap
  UI checks and 2,080 copy round trips; all four live endpoints agree.
  Among three-item core tank appearances, at least two Warmogs falls from
  75.65% to 42.74%, and triple Warmogs from 38.79% to 3.04%. These are repeated
  generated-board appearances, not match data. Rebuild stages total
  7,142.628 s (1 h 59 m 3 s), excluding compilation and verification waiting.
- Persistent incoming targets (2026-09-09): capacity v3 uses 48 profiles,
  including two mirrored focus orientations. Incoming sources keep individual
  owners; Gargoyle and Monolith count all assigned sources before initialization
  and throughout measurement. See `data/tft/set18/pressure-targeting.md` for
  the exact formation, regressions, benchmarks and remaining HP-stacking question.
  At that generation, three Warmogs still won both controlled original tank
  comparisons; the subsequent additive-HP results are recorded above.
  Targeting alone did not resolve the health preference. Source masks and prepared openings are cached
  in Rust; the Python reference independently checks assignment and aggregation.
  Engine `6bef32c46ca7`: 888 TFT tests with one existing skip, eight Rust tests,
  and complete native/reference optimizer parity at 3,981/4,478 allocations.
  All 7,670 standalone fights and 1,770 ranked-cell fixtures are unchanged.
  Rebuild took 6,444.755 s (1 h 47 m 25 s); published `g-574cbbba155f` passed
  all 1,664 core / 416 cap UI checks and 2,080 copy round trips. All four live
  endpoints agree. At least two Warmogs among three-item tank appearances
  increased from 69.5% to 75.6%; this is a targeting fix, not a resolved HP bias.
- Trait choices and melee-policy removal (2026-09-08): no melee-count cap
  remains in allocation, refinement or publication. Existing one/two-carry
  arrangements and antiheal requirements remain. Both equipped forms survive
  shortlisting; use native role/pressure resolution for Nidalee.
  `tft_comp_traits.primal_options` enumerates four singles at two Primals and
  six pairs at four. `tft_theory.Evaluator(optimize_primal=True)` is used by
  `Search`; every item allocation is compared against all legal choices with
  one selected aggregate across the 24 profiles. Bear/Phoenix share identical
  immortal-target/fixed-budget physics but remain separate evidence rows;
  their unmeasured execute/economy value must stay visible. Ordinary roster
  search cannot reach four Primals without Avatar/emblem state. Level-nine
  plans retain earlier choices through `required_primal` and preserve latent
  `primalHistory`; losing/regaining activation remains unverified. UI rows use
  optional `primal.selected`, `primal.required` and `primal.alternatives`.
  Native Turtle heals each living actor every four seconds; Spellweaver shares
  AP on actual eligible allied casts without doubling self credit; Solar
  converts half its existing bonus to true damage at five three-stars.
  Current core/gap details for all 36 traits are in
  `data/tft/set18/trait-models.{md,json}`. Engine `4116bc4251fd` preserves every
  prior standalone fight. Tests include `test_tft_primal`,
  `test_tft_primal_choices`, `test_tft_trait_shared_casts`, the updated carry
  policy/cap tests and the composition UI harness. The full suite ran 855
  tests with one existing skip. Complete Primal-aware item-search parity
  passed: 3,230 allocations at 36.191 s cold / 36.107 s repeated (reported nine
  items), 5,583 at 29.422 s / 29.213 s (dense twelve items without active Primal).
  These are pinned-CPU optimizer timings, excluding snapshot/anchor preparation.
  Full refresh took 83 min 49 s; publication `g-ae3fa683e59d` passed
  actual-data UI/copy checks for 1,664 cores and 416 caps.
  All four live TFT metadata/status endpoints agree on that generation and
  report all 1,770 baseline cells and eight composition contexts ready.
- Champion repairs (2026-09-08): all 65 champions / 70 base-and-Adaptor forms
  were reviewed; 18 received supported corrections. The current decisions,
  remaining per-champion gaps and verification record are in
  `data/tft/set18/champion-audit/repairs.{md,json}`. Nearest-N selection is
  independent of adjacency, lethal primary hits preserve secondary recipients,
  and Lillia uses source-attributed damage-threshold sleep across all fight
  paths. Theory shares newly applied flat resistance deltas exactly once.
  Pebbles settles elapsed channel time including endpoint fractions, Kha'Zix
  applies all-source mana multipliers to refunds, and Sivir extends the initial
  blade's kills and retains the previous victim across bounces. Elder starts
  its existing first-flight protection at cast start; duration/untargetability
  remain explicit approximations. No unverified Android numeric/timing rows
  were adopted. The previous 7,670-fight comparison changed 585 cases, only for
  repaired champions. New regressions are `test_tft_audit_*.py`; passing them
  does not validate live movement, timing, support, history or proc semantics.
  Final engine `9640b491f083` passes 817 Python tests (one existing unique-pool
  skip), including all 7,670 golden fights and all 1,770 golden cells, plus eight
  Rust unit tests. Full preparation took 4,083.247 s (68 min 3 s). Generation
  `g-e855e95ca90b` passed actual-payload UI checks for 1,664 cores, 416 level-nine
  upgrades and 2,080 exact planner-code roster round trips, then was published
  and observed through the active service. See the structured repair record
  for complete hashes, remaining limitations and verification commands.
- Damage comparisons use the finite-target clear-time/DPS leaderboard.
  The experimental elapsed-time damage curves, slider, native curve API and
  extra cache payloads were removed at the user's request. Keep the equipped
  Nidalee corrections: form determines Assassin/Marksman role, range and
  automatic exposure before combat; explicit pressure overrides still apply.
  Also retain the confirmed execute correction: an immortal probe grants no
  removal damage or sustain, preventing Gnar from cashing out the same HP
  every cast. Finite/bridged executions, ordinary throws and true damage
  remain valid. Tests: `test_tft_equipped_forms`, `test_tft_execute`,
  `test_tft_theory_auras`, `test_tft_leaderboard` and
  `jobs/test-tft-damage-ui.cjs`. The UI can read `finiteDiagnostic` from an
  older pinned curve-era generation during publication, without treating its
  damage-potential metrics as finite results. Heavy calculations remain Rust.
- Composition clipboard export: each core and expanded level-nine upgrade has
  `Copy to TFT`, using the version-two Team Planner roster format. Import IDs
  come from `data/tft/set18/team-planner.json` (Riot client data with source and
  asset joins); MetaTFT's combat lookup has stale IDs for Ivern and Lux. Codes
  carry champions only, with Elder counted once; no items, stars, positions,
  Alpha or Lux origin. `tft_site._team_planner` adds `teamPlanner` to saved
  composition metadata and its presentation hash, preserving calculation
  caches. UI clipboard failure falls back to legacy copy, then selected manual
  text. Check `test_tft_site` and `jobs/test-tft-composition-ui.cjs`. Publish
  with `tft_site.prepare(snap)` and `tft.dashboard_ready`, without a warm.
- Trait audit (2026-09-07): `data/tft/set18/trait-audit.md` covers all 36
  traits and the ten Alpha variants against the pinned 18.1d sources. Roster
  screening now calls the native theoretical scorer with resolved team traits
  and all legal Alpha holders at every beam step; it no longer sums isolated
  unit baselines before traits enter. Costs and trait counts add no score.
  `coverage` / `coverageNotes` distinguish partial or missing trait effects in
  composition tooltips; legacy `modeled` only promises some engine support.
  Corrected mechanics include delayed Primal Tiger team attack speed, repeating
  Riftbeast growth, shield-dependent Vanguard durability, Juggernaut(4)'s
  audited 6% nonmember durability, Thornmaiden's base team durability, shared
  Caustic reductions, Hunter amplification on secondary targets, and removal
  of undocumented Summoner extra summons and Sentinel's opening Alpha mana.
  Do not call all traits accurate: shared casts, summoned-board entities,
  position/recipient choices, histories and some source semantics remain
  unresolved. The audit records these individually; never substitute arbitrary
  flat synergy points or dormant curve rows for missing mechanics. Heavy
  numerical work remains native; Python resolves inputs and orchestrates.
- Theoretical composition capacity v3: `tft_theory.Evaluator` is
  the Python adapter to native `lol_tft.TheoryScorer` in
  `tft_engine/src/theory.rs`. It replaces
  named-opponent win ranking with `evaluationModel=ehp-damage-capacity-v3`.
  Score is frontline EHP × team DPS, aggregated geometrically across 48
  equally weighted sensitivity profiles: raw DPS 1,000/2,000 × physical share
  0/50/100% × incoming antiheal 0/33% × no enemy control or a 1.5-second
  frontline stun every eight seconds × main-first/secondary-first targeting.
  Control immunity is respected. Targets
  have 3,000 HP and 100 armor/MR; both layouts contain three targets. Spread
  changes adjacency coverage rather than deleting enemies, so independent
  nearest-N spells retain their target count. These are visible
  stress assumptions, not opponent frequencies; walking is not simulated.
  Native persistent actors share one live frontline pressure budget in
  half-second pulses, including a proportional final pulse. Pressure is
  emitted by three rotating source channels (`incomingSourceCount=3`),
  independent of outgoing area coverage. Single-target control
  can suppress one channel, not all incoming pressure; area control can cover
  up to three. Sources retain individual target ownership, including during
  source CC. Initial owners are first/second/first eligible frontliner; all
  three focus the only eligible frontliner. The main tank leads a fixed
  priority sorted by unfocused 50/50 opening EHP, with API ties; the second
  orientation swaps its first two entries. Opening and live per-attacker
  defenses count all assigned sources. A source retargets to the first eligible
  priority entry only after death or untargetability; lethal excess follows
  that same source. This is an explicit two-orientation formation assumption,
  not actual hex positioning. Planned window = opening
  frontline EHP / raw DPS; observed window ends at that point or earlier
  frontline collapse. All protected output stops when the last frontline
  champion or holding body dies. Unsupported derived windows fail explicitly;
  there is no 30-second win/loss cutoff. Adjusted EHP = raw pressure spent
  excluding lethal overkill
  + observed denial + remaining health/shield/body EHP; consumed shields and
  effective self healing are already included. DPS = protected damage /
  observed time.
  Protection time = adjusted EHP / pressure; damage capacity = score / pressure.
  Metrics are geometric means, with zero inputs yielding zero; exact score
  ties share ranks. Per-unit DPS is allocated by mean shares to sum to the
  aggregate; `measuredDps` is its arithmetic mean and `damageTaken` is raw spent
  pressure. A first response extension from remaining EHP is allowed only if
  the frontline survives its planned workload. Future ramping and temporary
  buffs remain approximate; algebraic protection/capacity estimates are shown
  separately from observed collapse. Scenario fields include
  `plannedMeasurementWindow`, `measurementWindow`, `frontlineCollapsed` and
  `incomingBudget = spentPressure + deniedPressure + unspentPressure`.
  Detailed rows also identify `pressureTargetOrder` and
  `initialPressureTargets`; compact scoring omits these board diagnostics.
  Metadata and cap inputs retain `targeting`, `incomingSourceCount=3` and
  `pressureAllocation=persistent-source-targets`. UI rollout accepts saved
  v1/v2 generations using their original schemas and explanations.
  No item-duplicate penalty is added. Ally healing
  and shields remain potential output with no team-EHP credit. Provider
  Sunder/Shred use opening uptime; each nonstacking burn channel has one owner.
  Targets do not die/heal; executes, takedowns, hex movement and real lobby
  frequencies are outside this model. Do not tune assumptions to force an
  item category to win. See `data/tft/README.md` for the current methodology.
  Tests: `python3 -m unittest test_tft_unit_profiles test_tft_theory
  test_tft_theory_native test_tft_comp_items test_tft_comps test_tft_caps`, plus
  `node jobs/test-tft-composition-ui.cjs`. Arithmetic/regression success does
  not establish accuracy against recorded live fights.
  The UI can still display a pinned v1 generation with its historical
  independent-response explanation; v1/v2 data, scores and revisions must not
  be mixed. Existing v1 performance/parity claims are historical, not evidence
  of v2 throughput or accuracy.
- Native theory execution: Python loads inputs, resolves board traits, prepares
  legal roster/item candidates and owns metadata/API/UI publication.
  `TheoryScorer.register(spec)` parses each immutable loadout once and returns
  an integer ID; `evaluate_many` scores batches of allocation ID lists in Rust.
  Rust owns equipped roles, shared-provider ownership and burn suppression,
  persistent actor/event state, pressure and control delivery, targetability,
  observed collapse, EHP/DPS accounting and scenario aggregation. Independent
  cached curves no longer drive board scoring. Prepared loadouts and reusable
  results avoid repeated parsing; `stats()` exposes execution/cache diagnostics.
  `ReferenceEvaluator` independently aggregates raw native shared-pressure
  observations via `UnitProfiles.measure_team` and `theory_opening`. Injected
  analytic providers independently solve conserved pressure cases. The
  reference remains a verification oracle, not the production batch scorer
  or an automatic fallback.
  After changing this path, rebuild the extension, run
  `test_tft_theory_native` parity/failure checks, and measure representative
  cold and repeated allocation batches before starting a full composition
  rebuild. `jobs/tft_theory_verify.py capture --out PATH` freezes oracle
  outputs, `replay --corpus PATH` checks the native results, and
  `benchmark --out PATH --cpu 2 --repeats 1` times unchanged production
  `ItemSearch` workloads for the reported nine-item board and a dense
  twelve-item board. It compares winners, complete evidence and search counts;
  shared anchor preparation is outside the timed optimizer sections.
  Current v2 measurements after champion repairs (2026-09-08, engine
  `9640b491f083`, CPU 2): reported-nine-item search compares 3,080 allocations
  in 11.876 s cold / 12.138 s repeated; dense-twelve-item search compares 5,583
  in 28.086 s / 27.731 s. Both converge and preserve the frozen independent
  reference's winners, complete item evidence and search counts. All 24 profiles
  use three outgoing targets in both layouts. Snapshot/anchor preparation is
  excluded; these are optimizer measurements, not a full dashboard warm.
  Earlier v2 measurements (2026-09-07, engine `3adc7dc4a3f2`, CPU 2,
  24 profiles): complete reported-nine-item search compared 3,496 allocations
  in 10.427 s cold / 10.270 s repeated; dense-twelve-item search compared
  6,265 in 23.561 s / 23.287 s. Both converged with exact cold/repeated
  winner, evidence and search-count agreement. Snapshot/anchor preparation
  is excluded. These are optimizer timings, not a full dashboard warm;
  historical v1 timings use a different model/workload and do not establish
  a speed ratio across versions. See `data/tft/README.md` for the record.
  The subsequent full v2 preparation measured 3,182.05 s (53 min 2 s),
  including 1,770 baseline champion cells, eight composition contexts and
  immutable HTTP responses. The complete site's UI/copy harness passed all
  1,664 cores and 416 caps, including 2,080 exact planner-code roster round trips.
  Report measured timings with workload/cache state; no speedup or
  full-warm estimate follows from the Rust rewrite alone. Keep the published
  dashboard generation active until the complete replacement is validated.
- Retained combat corrections (2026-09-07): Nidalee's equipped form uses the
  native form calculation; AP is a protected Marksman, AD a frontline
  Assassin. Theory contributions and the UI retain form/role/ability fields;
  the legacy match API also retains collision-free positions. Sentinel Mana
  Reave raises the next cast's mana cost, preserves stored mana and clears
  on cast. Repeated reaves keep the strongest increase; separate current-set
  stacking confirmation remains unavailable. Fixed-interval dummy spells
  remain timed. Tests: `test_tft_adaptor_positions`, `test_tft_mana_reave`,
  `test_tft_unit_profiles`. The prior golden replay found all 7,670 archived
  standalone fights unchanged by these corrections.
- Scheduled publication and saved HTTP responses (2026-09-06): the existing
  six-hour `lol-tft-refresh` timer now calls `tft.prepare_dashboard` through
  `tft refresh`: warm all champion cells, warm all composition contexts with
  the bounded worker pool, then build `tft_site.py` responses. The staged
  snapshot activates only after all three steps succeed. `dashboard_ready`
  checks both analyses and stores the complete site descriptor plus both
  revisions; a composition-only change also triggers the normal reload.
  `.cache/tft-site/g-*/` contains immutable copies/hardlinks of the existing
  JSON responses, prebuilt metadata/core comparisons/leaderboards, gzip files
  and a manifest with per-representation ETags. The server pins the last ready
  site until reload and only reads its files. It never starts a TFT warmer or
  runs the two metadata calibration fights on a request; preparation owns
  those too. LoL's separate AutoWarm remains. Dynamic refresh status is a
  small file read; it cannot change the served generation's readiness.
  Browsers revalidate response ETags, and returning from a champion reuses
  the already validated composition object. Loading text describes saved
  data; collapsed board details render when opened. A separate presentation
  hash covers source metadata, audit limitations and assets; metadata-only
  updates publish without invalidating fight caches. Metadata/status carry
  `publicationRevision` so open tabs refresh those details while retaining
  unchanged composition results. Fetch/check timestamps alone reuse the site;
  current check times come from refresh status. Tests:
  `test_tft_refresh`, `test_tft_site`, `test_tft_serving`, plus the two Node
  TFT UI jobs. Run `lol.py tft refresh` for an immediate complete publication;
  warming an individual cache alone does not activate it in the dashboard.
- Legacy symmetric combat diagnostics: `tft_engine/src/symmetric.rs`
  exposes `simulate_match`, using champion/item/trait/mana/shield/heal/CC rules
  on both teams with fixed lanes and rows. Recipient defenses resolve damage
  before donor credit; reciprocal procs use a deterministic queue. The
  synthetic `simulate_team` API also remains available. `tft_team.py` and
  `composition-opponents.json` retain their authored opponents, paired
  initiatives/positions, win counts and held-out/restricted-healing checks
  for mechanics diagnostics only. They do not select current compositions
  or enter their model revision. `prepare_actor`, `simulate_matches` and the
  exact SQLite WAL cache under `.cache/tft-comps/fight-scores/` likewise
  belong to this legacy path; old match throughput/rebuild timings do not
  characterize the theoretical evaluator. Tests: `test_tft_symmetric`,
  `test_tft_team_engine`, `test_tft_team`, `test_tft_opponents`,
  `test_tft_prepared`, `test_tft_team_batch`, `test_tft_match_cache` and
  `test_tft_exact_verify`. Gunblade ally targeting and broad/restricted
  proc/post-death healing conventions remain explicit diagnostic assumptions.
- Brambleback armor ignore (2026-09-06): Frenzy ignores a fraction of the
  armor remaining after Sunder, for his damage only. This corrects the old
  shared reduction and deliberately changes 52 recorded standalone fights
  and his 12 ranked cells. Other champions' recorded fights remain exact.
  The rebaseline follows a full warm; `test_tft_symmetric_regressions` checks
  source isolation, expiry and the hand-calculated 100 × .7 × .5 = 35 armor.
  Current Frenzy policy keeps one active eight-second buff, refreshed on
  recast. This is conservative; stacking semantics remain unverified.
  (Superseded 2026-09-20: his mana is now locked for the eight seconds it
  runs, so ordinary mana can no longer refresh a live Frenzy — only a proc
  that ignores the lock can.)
- Scuttlecrab burrow (2026-09-06): Roger confirmed it cannot attack while
  burrowed. Healing/durability still start when the cast lands; the driver
  blocks attacks/recasts for the resolved three-second duration. Existing
  one-second mana lock is preserved (a longer mana lock was not established).
  This deliberately changes Scuttle's standalone fights and rankings; regenerate
  their golden fixtures after the full baseline warm. Ally-shield API additions
  and other shared-engine plumbing preserve other standalone combat results.
  Tests: `python3 -m unittest test_tft_scuttlecrab`.
- Composition item navigation and recipe previews (2026-09-06): champion
  names/portraits also show details on hover or keyboard focus: board stars,
  roles, equipped ability names, actual trait activity, items and theoretical
  unit contributions. Core/cap tooltips use saved data without requests.
  Selecting a champion opens its individual item analysis with the board's star,
  geometry and tank threat, initially without traits. The view states that
  its fight differs from the board's actual traits/shared allocation; Back
  preserves composition controls and restores focus. Champion responses must
  match both champion IDs, scenario key and objective before rendering.
  `coreAnalysis.optimal` exposes the true best three-item build even if no
  flexible pair qualifies. Item metadata includes exact two-component recipes.
  The first preview completion stays optimal for the selected core; the other
  two prefer different component footprints within the existing 5% tolerance,
  minimizing maximum component copies, then repeated-component pairs, then
  performance. Turning the preference off restores performance order. These
  are presentation choices; they do not alter scores. Composition item choices
  now come from the theoretical capacity objective above. Checks:
  `node jobs/test-tft-item-ui.cjs`, `python3 -m unittest
  test_tft_ui test_tft_cores test_tft_pair_cores test_tft_core_cache`.
- Composition levels: `tft_board.py` owns slot costs and weighted trait
  counts. Elder Dragon occupies two slots and contributes two Riftbeast:
  seven champions at level 8, eight at level 9. Four-cost profiles forbid
  all 5-costs at level 8. After cores and ranks are fixed, `tft_caps.py`
  compares selling one support for two distinct ordinary 2-star legendaries
  or 2-star Elder; adding one ordinary 2-star legendary without a sale is
  also allowed. Main carry/tank and retained stars/items stay fixed. Only
  sold items transfer to new units; all legal splits and Alpha choices are
  compared, and the resulting arrangement may change. Every legal cap
  transition is evaluated: continuous capacity has no perfect-win upper
  bound. Exact score ties prefer two legendary slots, then lower purchase
  gold sold and stable IDs. Parent and cap use identical theoretical inputs;
  `theoryScoreDelta` reports their difference without affecting parent rank.
  `CAP_FIVE_COST_STAR=2` supplies cap stars and `level9FiveCostStar` metadata.
  `boardPlanModel=level8-core-level9-cap-v1` carries level/slot occupancy;
  `level9Upgrade` contains the board, sold/added units, item transfers, trait
  changes and `itemPolicy` instead of fresh replacement evidence. The UI
  opens caps lazily. Tests: `test_tft_caps`, `test_tft_nine_units`,
  `test_tft_comps`, `test_tft_comp_traits` and the Node composition UI job.
- Composition search and publication: `tft_comps.py` fills eight slots
  with matched-cost mains, at least three target-cost units, declared support
  costs/stars and one shared 6–12-item budget (default 9). Four arrangements
  cover single/duo carry/tank allocations. Planning constraints are not shop
  probabilities. There is no melee-carry cap at either planning level. Native
  equipped forms still determine actual roles and pressure (AD Nidalee is melee,
  AP is ranged); both AD/AP forms survive loadout shortlisting. Native
  `optimize_loadouts(..., preserve_forms=True)` retains each form's best
  candidates before truncation; the default remains false for other callers.
  Every core and cap also
  requires a usable antiheal source; `AntihealPolicy` audits active traits,
  items and native providers. Recheck after item/Alpha changes and support
  sales; a cap cannot lose its only source. These are hard planning constraints,
  without damage multipliers or guessed healing-denial score bonuses.
  Confirmed sources: Inferno 2+, Morello/Red Buff/usable Sunfire (33% Wound),
  and Cinderling's base ability (20%, no Alpha required). Brambleback Alpha
  and Elder Ignite do not explicitly specify Wound in the pinned evidence
  and do not qualify. `antihealSources` identifies actual holders in board details.
  Regression suites: `test_tft_carry_policy` and `test_tft_comp_utility`.
  This constraint/leaderboard-rollback generation prepared in 2,824.90 s
  (47 min 5 s): 1,770 champion cells and all eight composition contexts.
  Final validation covered 1,664 cores, 416 caps and 49,920 pressure rows;
  all boards had antiheal, no core exceeded one melee carry, and 14 caps used
  two. All item searches converged within the existing bound. The 755-test
  TFT suite passed with one existing skip, as did all 2,080 UI/copy round trips.
  All 1,770 finite winners and 442,500 displayed builds matched the previous
  finite results exactly. These counts verify the declared model and rules.
  `tft_comp_traits.py` resolves actual breakpoints, team shares
  and one Alpha. Items are ideal craftable choices, without inventory limits.
  Roster screening uses native unitemized EHP × DPS with resolved board traits
  and all legal Alpha holders, without main-role, trait-count or cost points.
  Beam width four, sixteen diverse boards and four support swaps bound the
  search. Itemless screening and limited beam width can still discard good
  completed rosters, so this is not a global optimum.
  Standalone loadouts supply diverse allocation seeds, retaining all single
  items and offensive/defensive/utility options. `ItemSearch` refines two seeds
  with every legal single replacement, transfers/exchanges and up to 64
  complete-loadout/paired interactions per round. Only strict capacity gains
  accept moves; 24 accepted improvements per seed is a bound, disclosed by
  `itemAnalysis.converged=false` with complete final single-item evidence.
  Convergence is local to tested moves. Evidence reports score/percentage
  changes and improved/degraded pressure profiles, not winning matchups.
  Eight artifacts cover four cost plans × two formations, all seven budgets
  and four arrangements. Canonical keys retain `-mixed` for the theoretical
  pressure range; standalone champion threat settings are separate.
  `python3 lol.py tft comps warm` uses up to eight spawned workers. Dependent
  roster screening precedes independent finalist jobs; fixed-order merge and
  ranking precede input/arithmetic consistency checks and optional caps.
  `--workers 1` and explicit `--only c1-clump-mixed` run serially. Parent and
  workers use one loaded snapshot and validate source/data revisions before
  atomic complete-context publication; missing/failed work never marks ready.
  Revisions include theory/unit-profile wrappers, search/items/traits/board/
  cap code and native engine/data inputs, not legacy named-opponent files.
  Metadata/artifacts expose `modelRevision`, `evaluationModel`, methodology
  and declared scenarios. Prepared native loadouts and results allow batched
  candidate scoring, with shared-pressure actor advancement and numerical
  aggregation in Rust. The legacy
  persistent match-score cache is not used. Benchmark a representative native
  batch and verify oracle parity before committing to all eight contexts.
  TFT requests only read
  prepared responses. Warm all champion cells and all eight composition
  contexts, then build the immutable site bundle before publication/reload
  via `tft refresh`; warming one cache does not activate the dashboard.
  Do not mix or relabel old payloads as the new model, and never hand-launch
  8321. Checks: `test_tft_refresh`, `test_tft_site`, `test_tft_serving`,
  `node jobs/test-tft-composition-ui.cjs` and `node jobs/test-tft-item-ui.cjs`.
- Champion leaderboards (2026-09-06): the TFT subview compares each unit's
  optimal three-item build, with separate damage and tank survival boards.
  Default `best` means the highest allowed star (3★ 1–3 costs / 2★ 4–5
  costs); fixed-star controls keep the same exclusions. Formation, traits
  and tank threat are shared across each comparison. A row opens its exact
  champion scenario. Per-cell `best` records retain the winning item IDs
  and unrounded primary metrics, so cross-champion ranks do not use rounded
  table values. Equal outcomes share ranks; double-capped tanks stay tied.
  `cached_leaderboard` reads only caches and reports missing cells as pending;
  the 72 `/api/tft/leaderboard/<selection>.json` paths also work in static
  exports. Existing combat math and golden rankings are unchanged. Run
  `python3 -m unittest test_tft_leaderboard test_tft_core_cache`.
- Murkwolf leap correction (2026-09-06): patch-specific `damageFormulas`
  in `overrides.json` replaces the malformed nested AP expression with
  additive AD/AP terms from the original curve rows. The archived ability
  footer supports one AP contribution; the character bin does not expose
  the live runtime formula. Base-stat damage is now 120/180/270 at 1/2/3
  stars, not 500/1,050/2,250. Snapshot resolves both combat coefficients and
  card values, so the median non-tank dummy's ability also changes 335 to
  318. All 1,770 scenarios and golden fixtures are regenerated. Other calc
  references and empowered attacks are unchanged. Regressions:
  `python3 -m unittest test_tft_murkwolf`; details: `data/tft/README.md`.
- Azir/Protector's Vow audit and mana-lock change (2026-09-06): Vow's 20 starting mana is
  applied once and capped; its defensive stats and low-health procs do not
  contribute in unpressured carry fights. The early first cast explains
  its strong opening result. Roger adopted the six-command mana lock
  described by TFTraits: attack/item mana and regeneration stay blocked
  until the sixth command is spent, then resume without an extra second.
  The sixth command itself grants no mana, and the next regen tick pays
  only for time after release. This is an adopted model rule, not newly
  verified primary runtime data; the character bin does not expose it.
  `kits.json` describes the current rule. Other audit findings remain open.
  Details: `data/tft/README.md`; regressions: `python3 -m unittest test_tft_azir`.
- Carry/fighter target reduction (2026-09-05): every damage-test target has
  permanent team-supplied Sunder and Shred (currently 30% each, read from
  the corrected Last Whisper/Void Staff rows). `targetDebuffs` is distinct
  from incoming tank `enemyDebuffs`. Raw dummy defenses stay unchanged;
  the engine takes the strongest active reduction once, so the first
  tank's 110/110 becomes 77/77 and matching items add no extra reduction.
  Custom mechanics fixtures opt out with `targetDebuffs: {}`. Tank survival
  defaults are unchanged. The UI/CLI show effective target defenses.
  Regressions: `python3 -m unittest test_tft_target_debuffs test_tft_ui`.
- Core analysis above the ranked builds (2026-09-05): `lol_tft.analyze_cell`
  exports compact scores for every legal build from the same enumeration,
  preserving `run_cell` and fight rankings. `tft.analyze_cores` groups full
  builds by pairs for carries/fighters and singles for tanks, with a visible
  5% tolerance, completion options and fully reoptimized item exclusions.
  Carry/fighter recommendations also use `lol_tft.score_pairs` to simulate
  every legal two-item pair on its own in the same fight. Stronger two-item
  pairs suppress weaker suggestions sharing a near-optimal three-item
  finish; only already-kept pairs suppress others, avoiding transitive
  merges. Pair clear time/damage decide priority; full builds still decide
  eligibility, completion losses and replacement costs. Carry/fighter
  cores need at least two third-item choices within 5% before grouping;
  `minCompletions` records this requirement. When no pair qualifies, the
  panel explains why while the full-build table stays available. `selectionMode`,
  `pairBuildsEvaluated`, `groupedCoreCount` and candidate `spike` distinguish
  these results from legacy caches. The UI shows three completions, one
  loss column and no repeated core heading; each row lists the core first
  and its finishing item(s) last. Remaining rows expand lazily.
  Duplicate cores separately test at most one copy. Recommendations use
  primary performance gaps, not rank or match frequency; missing clears
  and capped survival cannot supply fabricated percentages. Per-cell
  `coreAnalysis.coreStats` retains every core for the cache-only
  `/api/tft/<slug>/cores.json` comparison endpoint (also in static exports).
  See `data/tft/README.md` for formulas and limits. Tests:
  `python3 -m unittest test_tft_scores test_tft_cores test_tft_core_cache test_tft_pairs test_tft_pair_cores`.
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
  dummy slots, not a hex board: a heavy first tank (3,000 HP, 110 armor,
  110 MR), a median 2★ tank, then a median 2★ non-tank. The first tank's
  offense still uses the tank medians. Geometry changes nearby-target coverage; individual drivers can
  override the normal target order. The UI shows actual dummy stats and
  distinguishes three frontline blockers plus two backline damage dealers
  with synthetic tank damage presets from
  three attackers for fighters and none for carries. UI metadata checks:
  `python3 -m unittest test_tft_ui`.
- Standalone champion analysis, without match data (Roger's call 2026-09-04): one unit's
  mana-cycle fight against stat dummies derived from the set's own
  units at 2★ (two tanks then a non-tank for carries/fighters; three
  frontline tanks and two backliners for tanks; the first tank's defenses
  are fixed at 3,000 HP / 110 armor / 110 MR by `FRONT_TANK_DEFENSES`, per
  Roger's 2026-09-06 request), every 3-item
  multiset of the 35 craftable completed items. Every shop unit of the set
  is modeled (65 in Set 18). Cells per unit: 1★ and 2★ for every cost,
  3★ only for the 1–3 costs (a 3★ 4- or 5-cost is an auto-win, not a
  build question — Roger's call 2026-09-05; `STARS_BY_COST`,
  `unit_scenarios`) × spread/clump × traits bare/low/high: 18 cells for a
  1–3 cost, 12 for a 4–5 cost, multiplied by three threat presets for tanks
  (1,770 in all), and the dashboard's star
  buttons follow the unit. The fights run in the compiled engine
  `tft_engine/` (Rust, PyO3, imported as `lol_tft` from `lol_tft.abi3.so`
  at the repo root; `jobs/build-engine.sh tft` builds it, `jobs/build-engine.sh`
  both engines): a cell of 7,770 builds takes 10–20 ms on the 16 threads
  and the whole warm about 19 s wall (1,026 cells, measured 2026-09-05; the
  pure-Python engine took 13 minutes, 0.3–3 s a cell). Cache `.cache/tft/`,
  keyed by tft.py + `lol_tft.SOURCE_HASH` (a sha256 of tft_engine/src
  stamped by build.rs) + the snapshot + the hand files, so an edit to
  either recomputes everything. The scheduled `tft refresh` prepares both
  analyses before publishing saved HTTP responses (calculation log:
  `.cache/tft/refresh-warm.log`). Editing `tft_engine/src` without rebuilding
  makes `source_stale()` true; run the build script, then `tft refresh` to
  publish a complete generation and signal the normal service reload. Without the .so,
  `tft fetch`/`check`/`status` still work; a fight raises with the build
  command.
- The engine is a port of the Python engine and preserves its float
  operation order. The original golden fixtures verified the port bit for
  bit. The current fixtures were deliberately regenerated for patch 18.1d
  and the tank threat/debuff/EHP model plus permanent team resistance
  reduction for carries/fighters, with Roger's revised first tank of
  3,000 HP / 110 base armor / 110 base MR and adopted Azir six-command
  mana lock, followed by the Murkwolf leap/formula-card correction and
  Scuttlecrab burrow attack lock on 2026-09-06 and the audited trait corrections
  on 2026-09-07 (921 affected historical fights, 6,749 unchanged); see
  `data/tft/golden/README.md` for provenance. They pin 7,670 fights and
  the top 20 rows of every cell. `test_tft.TestGolden` replays every fight plus a sample of cells
  (`TFT_GOLDEN_ALL=1` for all 1,770; `jobs/tft_compare.py` prints per-unit
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
  dying, 20 s), Tank → "tank" (frontline and backline pressure, up to `TANK_DURATION` 60 s;
  ranked by how long the unit holds the dummies — an on-death body such
  as Yorick's spirit counts). Tank damage profiles are mixed, physical
  attacks and magic burst, each with three nearby frontline blockers and
  two distant damage dealers. Nearby AoE/auras reach only the frontline;
  explicit global/farthest abilities still reach the backline. Hecarim
  selects three nearest enemies regardless of clumping. Scheduled enemy
  casts wait through CC, then resume without lost casts or catch-up bursts.
  The shared damage budget is calibrated from two 2★, three-item reference
  carries (Aphelios and Ahri), plus three median frontliners: about 1,265
  raw DPS on 18.1d, 86% from the backline. Calibration uses a 20-second
  zero-resist immortal-target fight with no traits and is cached by resolved
  inputs; it does not replay those carry timelines into the tank fight.
  `api_meta.tankDummies` supplies the five-slot preview before a cell loads.
  Continuous Wound/Sunder/Shred use corrected item rows
  (currently 33%/30%/30%). Survivors at 60 s get a second test with twice
  the incoming damage; surviving both is a tie. Opening EHP includes
  driver initialization and reduced resists, but excludes shields/heals
  and attack-only reduction. `test_tft_tanks` pins these interactions;
  see `data/tft/README.md`. A unit's `recommendedItems`
  role wins when it names another role (Master Yi and Gnar itemize as
  Fighters, Caitlyn as a Marksman). The role also sets mana per attack
  (10 / caster 7 + 2 regen / tank 5, plus tank mana from damage taken:
  1% pre + 3% post-mitigation, 42.5 cap — the community formula, Riot
  publishes none), the fighter attack speed by stage and the assassin's
  15% off-target reduction (both from Riot's role text).
- The legacy pressure (fighters and custom dummy fights): each dummy attacks with its
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
  validates and warms changed champion and composition scenarios, then builds
  compressed HTTP responses before publication. Unknown
  mechanics, ambiguous mappings or unverified source changes preserve the
  working snapshot and report `needs-review` (exit 2). The local job never
  commits or pushes. `jobs/.state/refresh-tft.json` supplies the UI status;
  `.cache/tft/.dashboard-ready` signals the existing system reload path
  only when a complete publication changes. The TFT tab polls for new
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
  AD ×1.5 and HP ×1.8 per star, the cast window of the unit's own timeline
  (`cast-timing.json`, 2026-09-20: no attacks and no mana of any kind while
  the animation and any channel play; a unit the file does not cover keeps
  the old flat 1 s mana lock, the wiki's "can't accumulate mana for the
  second thereafter", and a driver's declared channel locks through its
  length plus that second), effect-long locks where the driver holds them
  (Tristana's charge, Xayah's feathers, Nidalee's javelins and the rest —
  a 0/50 marksman at 10 mana an attack would otherwise never leave them),
  an ability's damage landing at its timeline's effect time, else when its
  animation (0.25 s, the bins' default for every unit) or its declared
  channel ends (`Driver.lands`, `Fight.after`; so no kill is ever at t=0),
  curve rows
  hold the previous star's value, fighters' role attack speed at stage 4
  (Riot's 15.4 curve: stage 2–6 = 5/10/20/30/30%), damage amp additive
  and post-mitigation, negative resists floored at 0, Blossom/Elderwood
  "high" stops below the 11-unit prismatic tier, Fae pixies 3 and 7, the
  Riftbeast Alpha Mark on the unit, standalone Primal = Tiger (compositions
  optimize explicit legal blessings), Zyra's plants attack
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
  enemies" targeting hits one dummy spread out (superseded by the repaired
  `f.nearest(n)`, which picks N separate enemies in both geometries — the
  2026-09-07 repairs moved most such spells over and Morgana's blast
  followed on 2026-09-20); Solar's bonus is 7% of
  post-mitigation damage, itself mitigated; damage amp does not touch true
  damage (burns); the 3★ rows of every 4- and 5-cost are the PBE file's
  enormous values (Sett 5000, Taric 10000), which is one more reason those
  cells are no longer computed; the K–R fighters and tanks were not
  re-audited (the agent for that slice hit a rate limit).

## Tests

- `python3 -m unittest test_scaling` (the Overview rankings payload, whose
  game floors must reach the page's lowest min-games option; in-memory DB).
- `python3 -m unittest test_onetricks` (pyarrow; builds a small fake crawl).
- `python3 -m unittest test_builds` (needs the built engine; ~3 s; the
  Survival tier's tests included) and `node jobs/test-builds-item-ui.cjs`,
  `node jobs/test-builds-survival-ui.cjs`.
- `python3 -m unittest test_kit_driver test_kit_sources` (machine-written
  drivers and the dossier pipeline; hermetic, no model is called) and
  `node jobs/test-builds-generated-ui.cjs`.
- `python3 -m unittest test_builds_leaderboard` (needs the built engine; ~1 s;
  its own cells in a temp dir) and `node jobs/test-builds-leaderboard-ui.cjs`.
- `python3 -m unittest test_tft` (needs the built TFT engine; ~2 s;
  `TFT_GOLDEN_ALL=1` recomputes every golden cell, ~30 s).
- Theoretical compositions: `python3 -m unittest test_tft_unit_profiles
  test_tft_theory test_tft_theory_native test_tft_comp_items test_tft_comps
  test_tft_caps`. Run representative native/reference batch benchmarks before
  a full rebuild after scorer performance changes.
- TFT publication and browser contracts: `python3 -m unittest test_tft_refresh
  test_tft_site test_tft_serving`, `node jobs/test-tft-composition-ui.cjs` and
  `node jobs/test-tft-item-ui.cjs`.
