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

## The TFT tab (tft.py, tft_engine/, data/tft/)

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
- Symmetric composition fights (2026-09-06): `tft_engine/src/symmetric.rs`
  exposes `lol_tft.simulate_match`, running the same champion drivers, item,
  trait, mana, shield/heal and CC rules on both teams. Fixed lanes/rows remain
  an approximation. Recipient defenses resolve damage before donor credit;
  reciprocal procs use a deterministic queue. Test both initiative orders.
  The old `simulate_team` synthetic API remains for mechanics diagnostics.
  `tft_team.py` evaluates six independently authored search opponents and
  three distinct held-out opponents from
  `data/tft/set18/composition-opponents.json`; references obey the same
  cost/star and 6–12-item budget rules. Paired initiatives give 12 search
  fights and 6 held-out fights. Only search win rate ranks boards; exact ties
  share a rank. HP/time are diagnostics and held-out outcomes never select
  or reorder results. Draws/timeouts are non-wins. Pool versions/hashes and
  exact enemy champions/items/positions are published for review.
  Gunblade heals one lowest-percentage-HP living ally and discards overheal.
  Self omnivamp is separate. Quicksilver, full-stack Titan and Vi's spell
  supply their archived CC immunity; Sentinel's Mana Reave affects actual
  opposing mana. Current-set proc/post-death healing eligibility is not fully
  verified: the default broad convention is explicit, and final boards get a
  separate held-out restricted-healing sensitivity check, outside ranking.
  APIs carry `evaluationModel=symmetric-reference-pool-v1`; the UI rejects old
  payloads. Tests: `test_tft_symmetric`, `test_tft_team_engine`, `test_tft_team`,
  `test_tft_opponents`, `test_tft_comp_items`, `test_tft_comps`.
- Brambleback armor ignore (2026-09-06): Frenzy ignores a fraction of the
  armor remaining after Sunder, for his damage only. This corrects the old
  shared reduction and deliberately changes 52 recorded standalone fights
  and his 12 ranked cells. Other champions' recorded fights remain exact.
  The rebaseline follows a full warm; `test_tft_symmetric_regressions` checks
  source isolation, expiry and the hand-calculated 100 × .7 × .5 = 35 armor.
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
  roles, ability names, actual trait activity, equipped items and averaged fight
  contributions. Core/cap and opponent tooltips use saved data without requests.
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
  now come from the shared encounters described above. Checks:
  `node jobs/test-tft-item-ui.cjs`, `python3 -m unittest
  test_tft_ui test_tft_cores test_tft_pair_cores test_tft_core_cache`.
- Composition levels (2026-09-07): `tft_board.py` owns slot costs and weighted
  trait counts. Elder Dragon occupies two team slots and contributes two
  Riftbeast in total: seven champions at level 8, eight at level 9. This follows
  Riot's Enchanted Wilds overview. Four-cost profiles now forbid all 5-costs
  at level 8. After those cores and their ranks are fixed, `tft_caps.py` chooses
  linked level-9 variants: sell one support and add two ordinary 2-star
  legendaries or 2-star Elder; adding one ordinary 2-star legendary without a sale is a
  fallback. Main carry/tank, retained stars/items and total item budget stay
  fixed. Only a sold holder's items can move onto new units. Traits and Alpha
  are recalculated, and a cap may change the carry/tank arrangement.
  Caps compare search wins first, then prefer two legendary slots. A perfect
  score with that preference is an exact stopping bound. Caps never affect
  parent ranking; their held-out checks happen after cap selection. Both
  levels face the same level-8 references, so upgrade results describe that
  transition, not a separate level-9 field. The reference pool is now
  `18-reference-v2-level8`; its four-cost Ezreal board replaces unitemized
  Gnar with 2-star Kobuko to remove its legendary while preserving both traits.
  Symmetric native fights support nine actors; standalone limits are unchanged.
  Existing raw, prepared and compact fights through eight actors were verified
  bit-for-bit against the previous native binary; no goldens were regenerated.
  Metadata/artifacts carry `boardPlanModel=level8-core-level9-cap-v1`, board
  level/capacity/used slots and per-unit slot costs. Optional `level9Upgrade`
  contains the full board, sold/added units, item transfers and trait changes;
  it carries an item-transfer policy instead of fresh item-replacement evidence.
  The UI opens caps lazily and preserves exact champion/cap navigation. Old
  metadata and artifacts remain compatible together during publication handoff.
  The existing timer warms caps with the other calculations before publishing.
  Roger's cap assumption is now 2-star five-costs, including Elder Dragon.
  `tft_caps.CAP_FIVE_COST_STAR` supplies both fight inputs and the c4 profile's
  `level9FiveCostStar`; each cap's selection records `fiveCostStar`. Missing
  profile metadata denotes the earlier 1-star publication during handoff.
  Level-8 five-cost support rules and reference opponents remain independent.
  The original 1-star-cap refresh took 23 min 34 s on 2026-09-07: 1,770 champion
  cells, 1,664 level-8 cores and 416 linked level-9 caps. The cap phase compared
  618 legal rosters and 1,121 allocations; all selected caps reached the exact
  perfect-score bound. All 520,064 core item alternatives and 1,919 saved HTTP
  responses passed the final artifact/publication checks.
  Tests: `test_tft_caps`, `test_tft_nine_units`, `test_tft_comps`,
  `test_tft_comp_traits`, `test_tft_opponents`, and the Node composition UI job.
- Compositions (2026-09-06): `tft_comps.py` searches eight occupied team slots
  with same-cost main carry/tank, at least four target-cost units, realistic
  support costs/stars, and one shared 6–12-item budget (default 9). It compares
  single/duo carry/tank arrangements. `tft_comp_traits.py` resolves exact
  breakpoints, team shares and one Alpha Mark. Items remain ideal craftable
  items; component demand is shown but item bags/drops are not constrained.
  Individual loadout calculations screen starting allocations only. Every
  one-item candidate is retained, without a special Gunblade healing group.
  `tft_comp_items.ItemSearch` refines finalists from multiple seeds by every
  legal single-item replacement, transfers/exchanges and selected paired
  changes; only additional search wins accept changes. Per-holder evidence
  lists every legal replacement's lost/gained winning matchups. This remains
  bounded roster/local search, not a global optimum or a live-winrate claim.
  Eight artifacts cover 4 cost plans × 2 formations, all 7 budgets and 4 arrangements.
  The old synthetic pressure selector is gone; canonical keys retain '-mixed'
  and legacy UI pressure URLs normalize to that reference-pool context.
  Standalone champion threat controls remain separate.
  `python3 lol.py tft comps warm` uses up to 8 spawned workers. After each
  context's dependent roster screening, independent finalist item searches
  share that pool, including when only one cold context remains. Results
  merge in their original order; held-out validation follows fixed rankings.
  `--workers 1` and explicit `--only c1-clump-mixed` run serially. Workers
  receive the supplied snapshot; parent validates revisions and publishes
  complete contexts atomically. Native batches use one thread per worker.
  `lol_tft.prepare_actor` retains immutable resolved inputs; `simulate_matches`
  creates fresh combat state and returns results in input order. Compact
  search reports omit per-unit diagnostics; final reports replay full fights.
  Exact compact results persist under `.cache/tft-comps/fight-scores/` in
  SQLite WAL databases. Keys include engine/data/model revisions and resolved
  combat inputs, positions, opponents, duration and healing policy. Doubles
  retain their bits; corrupt entries are recomputed. This only reuses identical
  fights and does not narrow the search. Cache-free fixed workloads measured
  1.78–1.94× faster than the previous implementation on 2026-09-06; distinguish
  first-run timings from reuse when reporting performance.
  The full eight-context run from an empty composition cache took 1,117.4 s
  (18 min 37 s), versus the prior 3,287.7 s (54 min 48 s), with 8 workers on
  this host. All 1,664 boards, 520,064 replacement records and logical search
  counts match the frozen generation. The separately rebuilt 1,770 champion
  cells took 126.2 s and match their complete previous artifacts as well.
  A repeat composition rebuild, removing the eight result files while
  retaining unit benchmarks and exact fight scores, took 79.5 s. It reused
  6,959,652 fights and still ran 39,936 full diagnostic fights. This is a
  separate cache-reuse measurement, not a claim about first-time simulation.
  Cache revisions include item-search code, opponent data and the baseline
  engine/data revision. Warm all 1,770 baseline cells and all 8 composition
  contexts before signaling `tft.dashboard_ready`; never hand-launch 8321.
  Persistent UI checks: `node jobs/test-tft-composition-ui.cjs` and
  `node jobs/test-tft-item-ui.cjs`. See `data/tft/README.md` for methodology
  and limitations; do not tune opponent items/pressure to force desired items.
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
- Purely theoretical, no match data (Roger's call 2026-09-04): one unit's
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
  Scuttlecrab burrow attack lock on 2026-09-06; see
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
