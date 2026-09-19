# TFT snapshots and live patches

The active snapshot is the newest archived TFT patch, including hotfix
suffixes (`18.1 < 18.1b < 18.1d < 18.2`). Patch 18.2 was reviewed against
Riot's September 9 article, rebuilt and published on September 10. The
[18.2 review](set18/patch-reviews/18.2-review.md) records the source mappings,
remaining modeling limitations and publication verification. The archived
18.1d snapshot retains its earlier review through the August 31 balance
changes and September 1 bug-fix update.

## Sources

- `metatft.json` contains structured unit stats, roles, ability formulas,
  item curves and trait curves. The currently available Set 18 lookup is
  still marked PBE, generated August 16. It needs the documented live
  corrections in `overrides.json`.
- `communitydragon.json` archives the Set 18 portion of CommunityDragon's
  export for asset references and comparisons. The export fetched on
  September 5 was last modified August 29. It maps all 65 shop champions
  and 36 traits, but lacks usable ability calculations and alternate
  Adaptor forms, and retains several pre-hotfix stats. It cannot safely
  replace the full simulation input.
- `bins.json` contains CommunityDragon's per-unit timings. Missing bins
  are recorded as stable 404 entries; exhausted transport/server errors stop
  a refresh without being mistaken for missing timing data.
- `patchnotes.json` retains Riot's dated update headings, categories and
  parent item names instead of flattening away their context.
- `audit.json` maps published values to specific unit/form/item/trait
  fields and binds that review to hashes of the lookup, timings and patch-note
  document. It also records unresolved source ambiguities; passing the
  numeric checks is not a claim that every game mechanic is verified.
- `meta.json` records source URLs, upstream modification dates, hashes,
  fetch time and the time the patch-note checks passed.

## Refreshing

```sh
python3 lol.py tft refresh
```

The NixOS user timer `lol-tft-refresh.timer` runs `jobs/refresh-tft.sh` at
00:41, 06:41, 12:41 and 18:41 in the machine's local timezone. It catches
up after downtime. The declarative units live in
`~/Developer/dotfiles/nixos/configuration.nix`; user lingering keeps the
timer available without a login session. The job updates local data and
caches without committing or pushing to Git.

Each run checks Riot's latest patch article, including dated mid-patch
updates even when the base patch number is unchanged. It downloads the
sources into staging and compares them to the previously audited snapshot.
`tft_update.py` can reconcile supported numeric changes with an unambiguous
field mapping and matching old values. It checks changes in the underlying
definitions, preserves justified corrections, and records its evidence in
the new audit. Unknown mechanics, ambiguous values, changed timing data,
unsupported champions and source rollbacks stop the refresh for review.
This automates supported balance changes, not arbitrary engine changes or
new TFT sets.

Riot bullets retain nested form/star headings and separately labeled numeric
statements. XP purchase costs are outside the fixed-level combat model; this
does not exempt other system changes. Reviewed exceptions live in
`set18/patch-reviews/<patch>.json`. A manifest binds the exact lookup, timing
and parsed-note hashes plus the previous audit. Every correction identifies
its source entry, target coordinates, observed baseline, expected values and
reason. It can resolve a stale old value or a compound formula without
relaxing normal continuity checks. Overlapping checks retain untouched stars
and their history. Simple verified encodings can be reused later; decomposed
formulas require another review if changed. Exact mechanics dispositions
document retained approximations, rather than claiming those mechanics were
implemented. See [the 18.2 review](set18/patch-reviews/18.2-review.md).

Downloads use `tft_http.py`: at most four attempts within a 90-second overall
deadline, including DNS and body reads. Temporary DNS/network failures and
429/500/502/503/504 responses retry with bounded backoff; permanent HTTP,
certificate, parsing and validation failures do not. The user service allows
four hours for a full rebuild. Only exhausted temporary download failures
(exit 75) trigger its five-minute retry, with at most three starts per
30 minutes. Review-required (exit 2) and other failures (exit 1) do not restart
immediately. The checked-in drop-in under `jobs/systemd/` matches the Nix unit;
installing it does not require switching unrelated NixOS configuration.
Longer server-requested `Retry-After` delays are saved as `retryNotBefore`;
the job skips fetching until that time instead of retrying early.

Each refresh uses the same staged snapshot to prepare all missing champion
build scenarios and all eight composition contexts, using the bounded
composition worker pool. `tft_site.py` then prepares the complete HTTP
responses: saved builds, compositions, metadata, core comparisons and
leaderboards, with gzip compression and ETags. Even the small metadata
calibration fights happen here rather than on a page request.

Failed downloads, validation, either calculation stage or response preparation
leave the active snapshot and previously published responses intact.
Successful publication writes `.cache/tft/.dashboard-ready`, including both
analysis revisions and the immutable site descriptor;
the NixOS `lol-dashboard-reload.path` restarts the dashboard about ten seconds
later. Existing cache generations stay available during that handover.
Unchanged simulation inputs and response preparation reuse their caches and
do not restart the server. A composition-only change still publishes and
reloads its new results.

The server pins the last complete response bundle under `.cache/tft-site/`.
TFT page requests read those prepared files; they never start background
calculations, rebuild rankings or compress responses. The two former
server-owned TFT warmers are removed; LoL's separate build warmer is unchanged.
Clients receive the precompressed representation when supported and can
revalidate it with an ETag. Returning from a champion to the same composition
reuses the already validated browser data. Explicit retries and revision
changes still fetch, and collapsed board details render on first opening.
Source metadata, audit limitations and assets have a separate presentation
hash. Changes to those publish fresh responses without invalidating fight
calculations. Metadata/status expose `publicationRevision`, allowing open tabs
to refresh presentation details while retaining unchanged composition data.
Fetch/check timestamps alone do not rebuild the response bundle; the live
refresh status supplies the latest check time.

The job records progress and results in `jobs/.state/refresh-tft.json`.
It retains recent terminal runs and the last unresolved review blocker, so
a later network failure cannot hide the original patch-review problem.
The TFT tab shows the last check, calculation progress, failures and changes
that need review. Open tabs check for a new revision every minute and reload
their current champion/scenario when it is ready. Logs:

```sh
journalctl --user -u lol-tft-refresh.service -n 50
systemctl --user list-timers lol-tft-refresh.timer
```

Review candidates are retained under `set18/.pending/<patch>/`. To resolve
one, inspect the changed sources and mechanics, add a justified, source-bound
manifest in `set18/patch-reviews/`, then rerun `tft refresh`. The reconciler
produces the staged `audit.json`/`overrides.json`. Never update audit
hashes merely to pass validation. Manual `tft fetch --patch 18.2 --force`
still requires an existing matching audit; `tft check --patch 18.2` reports
its numeric checks. Supplying a base version such as `18.1` resolves to its
current hotfix. Restart the dashboard after code changes so it loads the
new engine and cache generation. Prefer `tft refresh`, which prepares the
complete publication before signaling that reload; a manual individual-cache
warm does not activate new dashboard responses on its own.

## Percentage-health stacking

Ordinary item and trait HP bonuses add their percentages together before
applying to star-scaled champion HP plus flat bonus HP. Three Warmogs therefore
give +54% HP, with Brawler and Blossom contributing to that same pool. This is
the adopted model interpretation, supported by historical in-game evidence;
the current Set 18 runtime's aggregation has not been independently decoded.
See [the HP stacking record](set18/hp-stacking.md) for equations, scope and checks.

## Verification

```sh
python3 -m unittest test_tft test_tft_tanks test_tft_data test_tft_update test_tft_refresh test_tft_ui test_tft_scores test_tft_cores test_tft_core_cache test_tft_target_debuffs test_tft_pairs test_tft_pair_cores test_tft_azir test_tft_murkwolf test_tft_leaderboard test_tft_loadouts test_tft_comp_traits test_tft_comps
python3 jobs/tft_compare.py
python3 -m unittest test_tft_refresh test_tft_site test_tft_serving
python3 -m unittest test_tft_http test_tft_patch_parser test_tft_patch_review test_tft_update
python3 -m unittest test_tft_unit_profiles test_tft_theory test_tft_theory_native test_tft_comp_items test_tft_comps test_tft_caps
python3 -m unittest test_tft_model_quality test_tft_mana_reave test_tft_opponents test_tft_team_batch
python3 -m unittest test_tft_equipped_forms test_tft_execute test_tft_theory_auras test_tft_carry_policy test_tft_comp_utility
node jobs/test-tft-composition-ui.cjs
node jobs/test-tft-damage-ui.cjs
```

Golden fixtures record their patch and concrete dummy setup. They were
deliberately regenerated from the compiled engine for patch 18.1d, the
tank threat/debuff/EHP model and team-supplied resistance reduction in
carry/fighter tests, with the first tank's updated fixed
3,000 HP / 110 base armor / 110 base MR benchmark; see
`golden/README.md` for the original port-verification provenance. Separate
regressions check the benchmark defenses and damage equations. The 18.1d
patch checks and regression tests verify live corrections, including Amumu's
healing percentage and Master Yi's AP-form resists. Artifacts/radiants are
archived and checked where their values can be established, but remain
outside the 35-item craftable build pool. Fishbones and Titanic Hydra have
unresolved source wording/field conflicts recorded in the audit.
Refresh regressions cover supported hotfixes, rejected ambiguous or mechanical
changes, source catch-up, staged calculation failures, rollback prevention,
overlapping jobs and cache continuity during publication.

The [65-champion mechanics audit](set18/champion-audit.md) reviews every
current driver and Adaptor form. It records source/code discrepancies,
missing support effects and unresolved timing, targeting and proc rules;
passing regression tests do not establish complete live-game accuracy.
The subsequent [repair report](set18/champion-audit/repairs.md) records implemented
corrections for 18 champions and the remaining limitations for all 65. Shared
repairs separate target population from adjacency, preserve impact recipients,
apply Lillia's damage-threshold sleep and share flat resistance reductions in
composition scoring. No unverified Android balance or timing values were adopted.

## Composition cores and level-nine upgrades

Composition ranking uses the `tft_theory.Evaluator` adapter to native
`lol_tft.TheoryScorer` in `tft_engine/src/theory.rs`, with
`evaluationModel=ehp-damage-capacity-v3`. Its continuous score is **frontline
EHP × team DPS**, evaluated across explicit generic pressure assumptions.
The score is a theoretical capacity index, not a percentage or live-game win
probability. Named enemy boards, win counts and a 30-second fight deadline
are absent from this objective.

Each composition and expanded level-nine upgrade has a **Copy to TFT** button.
Paste its code into the in-game Team Planner. The code carries champions only;
items, stars, positions, Alpha marks and Lux origins are not included. If the
browser blocks clipboard access, the page exposes a selected code for manual
copying.

[team-planner.json](set18/team-planner.json) pins all 65 champion IDs from
[Riot's client planner export](https://raw.communitydragon.org/16.17/plugins/rcp-be-lol-game-data/global/default/v1/tftchampions-teamplanner.json).
The combat lookup's `code` fields are stale for Ivern and Lux and must not be
used as a fallback. The [version-two encoder](https://github.com/nkhoit/tftkit/blob/1c0025d6883ae96d842e5fadaa9d3f28bc543899/web/traits/team-code.js)
uses `02`, ten three-digit hexadecimal champion IDs (empty entries `000`), and
`TFTSet18`. Elder Dragon appears once. `tft_site.py` includes this catalog in
composition presentation metadata, so copying needs no new simulations and
catalog corrections republish without changing calculation revisions.

### What is measured

Each formation uses the following Cartesian product: **48 sensitivity
profiles**, with equal weights rather than estimated opponent frequencies.

| Input | Default values |
| --- | --- |
| Total raw incoming DPS | 1,000 and 2,000 |
| Physical share | 0%, 50%, 100%; the remainder is magic |
| Incoming antiheal | 0% and 33% |
| Enemy control | None; or a 1.5-second frontline stun every 8 seconds |
| Incoming targeting | Main-first and secondary-first focus orientations |
| Generic target defenses | 3,000 HP, 100 armor, 100 MR |
| Target population | Three targets in both spread and clump |
| Area coverage | Spread separates adjacent areas; independent nearest-N spells still select multiple enemies |
| Incoming source channels | Three, independent of outgoing target coverage |
| Pressure cadence | One shared raw budget in half-second pulses, including a proportional final pulse |

`TheoryScorer` advances persistent champion actors on one shared clock against
immortal generic targets. Three incoming sources retain individual frontline
targets. Their initial owners are the first, second and first eligible
frontliner in the declared priority, or all three focus the only eligible
frontliner. Each whole packet goes to its source's owner; a source retargets
only when its owner dies or becomes untargetable. Lethal excess follows the
same source to its next target, conserving the shared budget.
Equipped forms determine the measured combat role: Nidalee's AD Assassin form
contributes frontline capacity, while her AP Marksman form receives backline
protection. Holding bodies continue to count as frontline protection.

The three incoming source channels rotate every half second. A single-target
stun can suppress its one channel, while area control can cover up to three;
changing spread/clump does not change the total incoming source count or raw
budget. Gargoyle and Monolith count **all assigned incoming sources**, including
sources currently stunned, from before champion initialization onward. A tank
can start with one or two attackers and gain more after another frontliner dies;
a lone tank has three. Sources keep their new target when a formerly
untargetable unit returns, while holding bodies retain their assignments.

The main tank leads a fixed formation priority. Other actual frontliners follow
in descending unfocused opening 50/50 EHP, with champion API breaking exact ties.
This priority uses the first profile's other conditions and is fixed across the
allocation's pressure/mix/control profiles. The second focus orientation swaps
the first two entries. Remaining frontliners wait for retargeting. Both
orientations have equal weight; they are a visible formation sensitivity
assumption, not a full hex-position model or measured enemy target distribution.
Opening EHP uses exactly the same initial source assignments as measurement.

Flat resistance reductions applied by champions are shared at the time they
occur, using each actor's new contribution so previously shared reductions are
not added again. Lillia's Sleep also shares a target damage threshold: allied
damage can wake the target, the wake damage belongs to Lillia, and independent
stuns remain active. The generic enemies still have immortal independent HP
probes; sharing these effects does not introduce named opponent boards.

The control schedule is an explicit stress condition with equal weight, not an
estimate of lobby frequency. Control immunity is respected by the native actors;
no movement or target-access penalty is inferred. Duplicate items receive no
artificial repetition penalty beyond their actual modeled behavior and legality.

For each profile, let `P` be incoming raw DPS:

1. Sum opening frontline EHP after the actual stats, items and resolved traits.
   Derive the planned window `W = opening frontline EHP / P`.
2. Measure damage, effective self sustain, raw pressure spent and remaining EHP
   with the shared live pressure budget. Stop at `W`, or earlier when the last
   frontline holder dies. All protected output stops at that collapse; the
   observed window is `min(W, collapse time)`. The final pressure pulse is
   proportional to the remaining observation interval.
3. Adjust frontline EHP to **raw pressure spent excluding lethal overkill +
   observed damage denied + remaining health/shield/body EHP**. Each pool's
   physical/magic defenses are mixed before capacities are added. Effective
   self healing and consumed shields are already reflected in this accounting;
   adding them again would double count them.
4. Compute team DPS as protected damage divided by the observed time. Dead
   units stop acting, and every protected contribution stops at frontline
   collapse. The profile's score is `adjusted EHP × DPS`, estimated protection
   is `adjusted EHP / P`, and estimated protected damage is `score / P`.

The displayed EHP, DPS, protection time and damage capacity are geometric means
across the profiles; any zero input yields zero. The final `theoryScore` is the
aggregate EHP multiplied by aggregate DPS, equivalent to the geometric mean of
profile scores. Exact score ties share a rank. Individual DPS contributions are
allocated by mean damage shares so that they sum to aggregate DPS;
`measuredDps` separately records each unit's arithmetic mean measured DPS.
`damageTaken` reports raw pressure spent, and ally healing/shielding fields
report potential output. Each scenario exposes `plannedMeasurementWindow`,
actual `measurementWindow`, `frontlineCollapsed`, and the raw accounting fields
`incomingBudget`, `spentPressure`, `deniedPressure`, `unspentPressure`.
Detailed board profiles also expose `pressureTargetOrder` (frontline champion
APIs after the orientation swap) and `initialPressureTargets` (three nullable
champion APIs, one per source). Compact search results omit these two diagnostics.
The budget
equals spent plus denied plus unspent pressure. Inputs also include
`controlInterval`, `controlDuration`, `pressureInterval`, `pressureAllocation`
and `incomingSourceCount=3`, `targeting`, and
`pressureAllocation=persistent-source-targets`. Metadata, rows and cap comparisons must agree on
these inputs.

Only a frontline that survives the planned workload receives a **first response
extension** using remaining EHP and measured throughput. This approximates
future ramping, burst and temporary defenses; it is not an exact future fight
prediction. Collapsed rows retain the same algebraic score identities, while
their observations end at collapse. Pulse-boundary overkill and unused pressure
can make capacity estimates differ slightly from actual observed damage; the
table distinguishes observations from estimates. Unsupported derived windows
beyond the native implementation limit fail explicitly rather than receiving
a cutoff score.

Ally healing and shields receive no team-EHP credit until a recipient-utilization
model exists. Actual providers share the strongest Sunder/Shred as an opening
uptime approximation. One provider owns each nonstacking ordinary/Inferno burn
channel; provider timing and replacement after death remain approximate. Target
healing, target deaths, takedowns and executes are not modeled. Incoming
antiheal tests the board's self sustain. Critical strikes use expected values.
Generic magic pressure is timed and has no mana cost, so Mana Reave and
mana-cost retaliation such as Ionic Spark's damage receive no value here.
Area coverage does not reproduce movement, hex targeting or a real lobby.
Per-kit/trait omissions still apply. These assumptions should be challenged as
assumptions, not tuned to force a particular item category to win.

Brambleback's current policy uses one active eight-second Frenzy, refreshed by
recasts. This is a conservative model choice: stacking and recast-mana semantics
remain unverified, and the generic mana-lock rules are unchanged.

The previous v1 model used independent unit curves, fixed frontline pressure
shares and no incoming control. V2 shared pressure equally among all eligible
frontliners at each pulse and exposed each frontliner to one current attacker
for per-attacker defenses. V3 adds persistent ownership and two focus orientations.
The UI can display pinned v1/v2 generations during rollout with their original
explanations. Scores, metadata and cached results from different model versions
must never be mixed or silently relabeled.

The [persistent-targeting report](set18/pressure-targeting.md) records the v3
change, controlled tank comparisons, complete rebuild and remaining HP-stacking
question. The source-count correction did not reduce overall Warmog preference
in the regenerated search; no item stats or HP stacking rules were changed.

### Native execution and performance checks

The production calculation stays in Rust from prepared loadouts through final
scenario aggregation. Python loads the snapshot, resolves board traits and
legal candidates, and registers each distinct champion/star/traits/item spec
with `TheoryScorer.register(spec)`. Registration parses an immutable loadout
once and returns a reusable integer ID. `evaluate_many` accepts batches of
allocation ID lists, the main carry/tank indices and a `details` flag.

Rust determines equipped roles, shared-provider ownership and burn suppression,
prepares opening values and advances a persistent actor cohort through each
declared pressure/control schedule. It handles targetability, pressure
redistribution, observed frontline collapse, EHP/DPS accounting and geometric
aggregation. Independent cached response curves are no longer the production
board-scoring path. Prepared loadouts and reusable results still avoid repeated
input parsing; `stats()` exposes execution/cache diagnostics. Requested score
summaries or detailed observations return to Python; the event loop remains
native.

Python still owns legal roster and item search orchestration, persistence,
metadata, API responses and the UI. This execution change preserves the
EHP × DPS score identities while changing pressure, control and observation
semantics under a new model revision. `tft_theory.ReferenceEvaluator` independently
aggregates raw native shared-pressure observations through
`UnitProfiles.measure_team` and `theory_opening`. It also supports analytic
providers that independently solve a conserved pressure budget. Production
batches use `TheoryScorer`; the reference implementation is a verification
oracle, not an automatic fallback for native failures.

Before a full rebuild after scorer changes:

1. Rebuild the native extension and run `test_tft_theory_native`, including
   native/reference parity and relevant failure cases, plus the affected
   theory/search tests below.
2. Benchmark representative allocation batches with the same snapshot,
   candidates and scenarios on both paths. Measure cold and repeated batches,
   distinguish preparation cost and report native cache counters. Check numerical
   parity alongside timings; smaller scenarios or a reduced search are not a
   performance comparison of the same workload.
3. Use those observed results to decide whether to launch the complete build.
   Prepare and validate all eight contexts before publishing; retain the
   previous dashboard generation throughout calculation or interruption.

No speedup or full-build completion estimate is assumed merely because this
path runs in Rust. Record measured workload, cache state and wall time when
reporting performance.

**Current v2 optimizer measurements after champion repairs (2026-09-08):**
engine `9640b491f083`, CPU 2, all 24 profiles and three outgoing targets in both
layouts. Complete native searches compared 3,080 allocations in 11.876 s cold /
12.138 s repeated for the reported nine-item board, and 5,583 in 28.086 s /
27.731 s for the dense twelve-item board. Both converged, with the same winners,
complete item evidence and search counts as the frozen independent reference.
The reference was captured on `de68a37fe8fe`, before the final pending-spell timer
repair, which the replay confirms leaves these optimizer results unchanged.
Snapshot and shared anchor preparation are outside these measurements.

The complete champion-repair rebuild took **4,083.247 s (68 min 3 s)** for
1,770 champion cells, eight composition contexts and saved HTTP responses.
Generation `g-e855e95ca90b` passed the actual-payload UI checks for 1,664 cores,
416 four-cost level-nine upgrades and all 2,080 planner-code roster round trips.
The published service was observed serving the new complete generation. The
[repair record](set18/champion-audit/repairs.json) contains full revisions and
verification details; these regression checks do not establish live-game accuracy.

**Earlier v2 optimizer measurements (2026-09-07):** native engine
`3adc7dc4a3f2a5f14fd4c8c78d9b8b9ef3e7471b61312e5777cee1c515622f36`,
pinned to CPU 2, evaluated all 24 pressure/control profiles per allocation:

| Complete production item search | Allocations per pass | Native cold | Native repeated |
| --- | ---: | ---: | ---: |
| Reported nine-item board | 3,496 | 10.427 s | 10.270 s |
| Dense twelve-item board | 6,265 | 23.561 s | 23.287 s |

Both searches converged. Cold and repeated passes selected exactly the same
winner and item evidence, with identical search counts. These timed optimizer
sections exclude snapshot loading and shared anchor preparation; they do not
measure a complete dashboard warm. The model and search workloads differ from
v1, so these tables do not establish a speed ratio across model versions.

The subsequent full v2 preparation took **3,182.05 s (53 min 2 s)**, including
1,770 baseline champion cells, all eight composition contexts and immutable
HTTP response preparation. The resulting generation (prefix `g-c61847a87553`) passed
the real-payload UI harness for all 1,664 cores and 416 level-nine caps. All
2,080 Team Planner codes decoded to the displayed champion rosters.

**Historical v1 timings, not v2 performance:** on the saved 18.1d snapshot
(2026-09-07), complete two-seed item searches pinned to one CPU measured the
following cold-cache times without a profiler:

| Workload | Allocations compared | Python reference | Native scorer | Speedup |
| --- | ---: | ---: | ---: | ---: |
| Reported nine-item board | 3,376 | 28.44 s | 0.735 s | 38.7× |
| Dense twelve-item board | 4,533 | 43.68 s | 0.987 s | 44.3× |

Both selected the same items and Alpha holder, with matching replacement
evidence and identical search coverage. Shared anchor preparation is excluded
from these timings; these are v1 optimizer benchmarks, not v2 or full rebuild
timings.

`jobs/tft_theory_verify.py capture --out PATH` freezes reference cases before a
scorer rewrite. Reuse that corpus to check every result field and ranking after
the rewrite; do not replace expected values to resolve a parity failure. With
the frozen corpus available, run:

```sh
python3 jobs/tft_theory_verify.py replay --corpus .cache/tft-theory-native-verify/corpus.json
python3 jobs/tft_theory_verify.py benchmark --out .cache/tft-theory-native-verify/benchmark.json --cpu 2 --repeats 1
```

The benchmark pins one allowed CPU and runs complete production `ItemSearch`
for the reported nine-item board and a dense twelve-item board on both
implementations. It preserves the two seeds, full item pool, screened anchors,
legal moves, interaction limit and convergence bound. Timed optimizer sections
start with a fresh evaluator cache, then repeat using the same evaluator;
snapshot loading and shared anchor preparation happen before those sections.
Winner identities, detailed results, item evidence and search counts must agree.
Progress is printed and the JSON report retains cold/repeated timings and
reference results. Choose a permitted CPU ID if CPU 2 is unavailable.

### Board and item planning

The Compositions view fills eight team slots with distinct shop champions and a
same-cost main carry/fighter and main tank. It compares one carry/one tank,
two carries, two tanks, and two of each under the **same completed-item budget**.
The default is nine items; all budgets from six through twelve are available.
Every allocation spends its exact budget, with at most three items per champion.
The main pair receive at least two items. A secondary carry/tank receives at
least two and cannot receive more than its corresponding main. Other items go
to supports. Item categories are unrestricted; damage and protection determine
their joint value. Main denotes the upgrade target, not necessarily the largest
damage contributor.

There is **no melee-carry limit** at level eight or nine. The existing one/two-carry
arrangements still define item allocation. Equipped forms determine actual roles
and pressure: AD Nidalee is melee and AP Nidalee is ranged. Both forms survive
loadout shortlisting, and double-melee boards are legal throughout allocation,
item refinement and publication.

Primal boards compare every legal blessing choice for each allocation and keep
one choice across the full pressure aggregate. Cards show the selected blessing;
details show alternative scores and their limitations. Level-nine plans retain
earlier choices and may add a second when eligible; reset behavior after losing
and regaining the trait is unverified. See the [current trait models](set18/trait-models.md)
for all 36 traits, Blossom AD/AP, shared Spellweaver casts, Solar conversion and
the score's remaining blind spots.

Every core and level-nine upgrade also requires a usable **antiheal source**.
Active trait effects, item application conditions and native champion sources
are checked against the selected roster, items and Alpha holder. Changing
items or selling a support cannot remove the team's last source. Each board
lists its sources in `antihealSources`. This is a required utility constraint;
the nonhealing target probes do not award a guessed damage bonus for Wound.
The pinned 18.1d sources are Inferno from two units, Morellonomicon, Red Buff,
and Sunfire Cape (33% Wound), plus Cinderling's base ability (20% Wound,
without Alpha). Sunfire must be usable from the holder's resolved range.
Brambleback's Alpha burn and Elder Dragon's Ignite do not explicitly specify
Wound and do not satisfy this requirement. Radiant items are outside the
legal item pool. Source presence does not promise full coverage or uptime.

Native `optimize_loadouts(..., preserve_forms=True)` retains the best builds
separately for each equipped form before truncation. Composition shortlists
also preserve melee/ranged roles and antiheal availability; allocation states
track both requirements. This keeps a high-scoring illegal AD build from
discarding the legal AP option before final team evaluation. The native
function's default behavior remains unchanged for other callers.

The complete rebuilt generation took 2,824.90 seconds (47 minutes 5 seconds)
to prepare. Validation checked all 1,664 cores and 416 upgrades: every board
had antiheal, every core had at most one melee carry, and 14 upgrades used two.
All 49,920 pressure rows passed the arithmetic/conservation checks, all item
searches converged within their configured bound, and all 2,080 planner-code
round trips passed. The restored finite results matched all 1,770 previous
winners and 442,500 displayed builds exactly.

Planning rules encode Roger's preferences, not shop probabilities:

| Plan | Main pair | 4-cost cap | 5-cost cap |
| --- | --- | --- | --- |
| 1-cost reroll | Both 3★ | 1 support | None |
| 2-cost reroll | Both 3★ | 2 supports | None |
| 3-cost reroll | Both 3★ | 3 supports | 1 support at 1★ |
| 4-cost level-8 core | Both 2★ | 8 | None |

Each level-8 board has at least three target-cost champions, three to five
frontliners under the roster-planning roles, and at least two damage units.
Supports are 2★ except the permitted 1★ 5-cost support. Purchase gold counts
fielded copies and excludes XP, rerolls and contesting. Component demand is
shown, but actual drops and inventory are not constrained.

`tft_board.py` makes Elder Dragon occupy two slots and contribute two Riftbeast
in total, producing seven champions at level 8 or eight at level 9.
`tft_comp_traits.resolve_board_traits` resolves the selected board's actual
breakpoints, team shares and one eligible Alpha holder. Eclipse's placeholder
never activates automatically. Blackthorn uses no sacrifice. Unmodeled plants,
BFF/Rider effects, histories and positional bonuses remain listed per board.

`tft_comps.Search` starts from legal main pairs, uses beam width four, refines
sixteen diverse boards and checks up to four support swaps. Initial roster
screening uses **unitemized native EHP × DPS with the actual board traits**,
including team recipients and every eligible Alpha holder. It uses the same
pressure assumptions and derived window as final scoring. Trait counts and
same-cost counts receive no score bonuses. This replaces the isolated-unit
sum that discarded inexpensive trait supports before evaluating their bonuses.
The beam remains approximate: unitemized screening and its limited width can
still miss strong completed or itemized rosters. The compiled standalone
loadout search supplies diverse item seeds, retaining every one-item option
and offensive/defensive/utility alternatives.

The [36-trait audit](set18/trait-audit.md) records source evidence, corrected
mechanics and remaining gaps, including the ten Riftbeast Alpha variants.
Composition trait records expose `coverage` and `coverageNotes` so supported
stats, partial effects, missing mechanics, structural rules and economy can be
distinguished. The compatibility flag `modeled` only means some engine support;
it does not establish that the theoretical score credits all of that trait.
Elderwood plants, Sprykin riders, shared Spellweaver casts and historical trait
state still need explicit inputs or interaction support. A missing effect is
not evidence that its composition is weak in an actual game.

`ItemSearch` refines two selected seeds using every legal single-item
replacement, transfers, equipped-item exchanges and up to 64 screened loadout
or paired-change interactions per round. Only strictly higher theoretical
capacity accepts a move. Each seed is limited to 24 accepted improvements;
`itemAnalysis.converged=false` discloses hitting that limit and still includes
a complete final single-replacement pass. Convergence only means no tested
local move improved the score, not a global optimum. Every final item slot
reports replacement score/percentage deltas and improved/degraded pressure
profiles under the same model revision; exact ties remain alternatives.

Four-cost boards are selected and ranked at **level 8 without 5-costs**.
`tft_caps.py` then compares optional level-9 transitions: sell one support and
add two distinct ordinary 2★ legendaries, sell one support for 2★ Elder Dragon,
or add one ordinary 2★ legendary without selling anyone. Main carry/tank,
retained stars/items and total item count stay fixed. Only the sold holder's
completed items can transfer onto new units; all legal splits and eligible
Alpha holders are compared. Caps retain at least three target-cost champions.

Every legal cap transition is evaluated; continuous capacity has no perfect-win
early-stop bound. Exact score ties prefer two legendary slots, then lower
purchase gold sold and stable IDs. Caps use their parent's same pressure inputs,
report `theoryScoreDelta`, and never affect the parent's rank. Their `itemPolicy`
and transfer details replace the core's item-replacement evidence. This models
a transition, not the probability of reaching it.

### Revisions, publication and checks

Eight canonical contexts cover four cost plans × two formations, each containing
all seven budgets and four arrangements. API keys retain `-mixed` for the
theoretical pressure range; standalone champion threat controls remain separate.
Endpoints are `/api/tft/compositions/meta.json`, `status.json` and
`<c1–c4>-<spread|clump>-mixed.json`. Metadata and artifacts include `modelRevision`,
`evaluationModel`, declared scenarios and methodology, plus
`boardPlanModel=level8-core-level9-cap-v1`. New theory payloads have no opponent
pool, matchup wins or held-out validation fields.

`python3 lol.py tft comps warm` uses up to eight spawned workers. A context's
roster screening precedes its independent finalist item jobs. The parent
restores original result order before ranking; declared-input and arithmetic
consistency checks follow the fixed ranking. Level-9 jobs run after their parent
cores are finalized. `--workers 1` or explicit `--only c1-clump-mixed` runs
serially. Workers use the parent's loaded snapshot, and source/data revision
checks reject mixed generations. Missing/failed work cannot publish a partial
context or mark the warm complete.

Composition revisions cover search, items, board/trait/cap code, the theory and
unit-profile wrappers, and native engine/data inputs. Legacy opponent definitions
and `tft_team.py` do not enter the active composition revision. Prepared loadout
IDs and bounded typed response/result caches live in each native scorer;
allocation batches reuse them within that scorer. The legacy SQLite fight-score
cache does not cache this objective. A cache warm alone does not
activate dashboard responses. `tft refresh` prepares champion cells, all
composition contexts and the immutable site bundle before publication signals a
reload. TFT HTTP requests only read the saved generation; missing response
artifacts return 202 rather than calculating on request. Old and new analysis
payloads must never be mixed or relabeled during handoff.

```sh
python3 -m unittest test_tft_unit_profiles test_tft_theory test_tft_theory_native test_tft_comp_items test_tft_comps test_tft_caps
python3 -m unittest test_tft_refresh test_tft_site test_tft_serving
node jobs/test-tft-composition-ui.cjs
node jobs/test-tft-item-ui.cjs
```

These check capacity arithmetic and failure cases, native/reference parity,
shared-actor pressure/control behavior and self sustain,
item tradeoffs/convergence limits, slot/item conservation, process ordering,
revision/publication guards and saved UI contracts. Passing them establishes
consistency with the implemented model, not validation against live fights.
Standalone champion golden fixtures remain a separate regression suite.

The previous `tft_team.py` / `lol_tft.simulate_match` named-opponent evaluator,
`composition-opponents.json`, prepared/compact match APIs and SQLite exact-score
cache remain available for **legacy combat diagnostics only**. Their win counts,
held-out checks and historical timings do not describe current composition
ranking. Mechanics fixes remain: equipped Nidalee forms use their actual roles,
and Sentinel Mana Reave raises the next cast's cost while preserving mana and
clears on cast. Repeated reaves retain the strongest increase; fixed-interval
synthetic spells remain timed. Relevant diagnostic regressions include
`test_tft_symmetric`, `test_tft_team`, `test_tft_opponents`,
`test_tft_model_quality`, `test_tft_adaptor_positions` and `test_tft_mana_reave`.

## Champion leaderboards

The Damage view again compares each champion's optimal three-item build in
the finite-target clear test, ranked by clear time and then damage for
uncleared targets. The experimental time-slider damage curves and their
native enumeration API have been removed at the user's request. Composition
capacity remains the separate EHP × DPS model described above.

The default stars are 3★ for 1–3 costs and 2★ for 4–5 costs. Fixed 1★, 2★
and 3★ comparisons remain available, with 4–5 costs excluded from 3★.
Formation and trait context are shared across each comparison. Tanks retain
their survival ranking and secondary test at double pressure. Rows open the
same finite champion build scenario used by the leaderboard.

The finite damage benchmark now uses a shared **0.5-second engagement delay**
for equipped attack ranges1–2, at the first engagement and after the current
target dies. This is an explicitly accepted approximation for walking/jumps,
not recovered per-champion timing. The native clock overlaps attack cooldowns
with movement, delays new attacks/casts until arrival, and continues existing
damage, regeneration and incoming pressure. Akali's cross-target recasts,
Brambleback's leaps and Master Yi's transferred second hit also wait for
arrival. The same target incurs no recurring penalty; collateral kills do
not force a move. Ranged forms remain unchanged. The displayed movement time
can overlap cooldowns and is not a direct count of extra fight seconds.
The [movement record](set18/melee-movement.md) describes the inputs, tests and
sensitivity results. Tank survival presets and immortal composition workloads
retain their existing behavior; the latter do not produce target-death walks.

The equipped-form corrections remain. AD Nidalee resolves as an exposed
melee Assassin and AP as a protected Marksman before combat initialization;
explicit pressure overrides still apply. Item auras are gated after form
resolution, including shared Sunder, Shred and burn-provider credit. AP range
is interpreted as base range plus the pinned `AdditionalAttackRange` row:
1 + 4 = 5; AD uses its own one-hex kit. The runtime application of that range
row has not been independently observed; the champion's model note records
this source interpretation. Each finite build carries its actual form,
role, range and pressure metadata.

The Gnar execute correction also remains: an immortal theory probe grants
no damage or healing from last-enemy removal. Gnar cannot repeatedly cash
out its unchanged HP as true damage. Finite removal, ordinary multi-target
throws and legitimate true damage are preserved. These checks do not
establish real-game accuracy; the separate movement approximation is described
above and does not simulate hex paths or body blocking.

Cells retain their unrounded finite `best` record. Leaderboards no longer
compute or save extra damage-curve payloads. Requests only read prepared
results; missing cells remain pending. All 72 selection paths remain at
`/api/tft/leaderboard/<star>-<geometry>-<traits>-<threat>.json`. During a
publication transition the UI can read an older generation's
`finiteDiagnostic`, recalculate finite ranks, and avoid using its damage
potential metrics as clear-test results.

Tests: `test_tft_leaderboard`, `test_tft_equipped_forms`, `test_tft_execute`,
`test_tft_theory_auras` and `jobs/test-tft-damage-ui.cjs`. Heavy enumeration
and fight calculations remain in Rust.

## Murkwolf leap correction

The September 6 formula audit found that the archived lookup multiplies
Murkwolf's AP row twice: `LeapDamageAP * GenericCalc1`, where `GenericCalc1`
already equals `LeapDamageAP * AP / 100`. At two stars and base stats this
produces 150 + 900 = 1,050 damage. The same source's ability footer explicitly
describes one AD contribution plus one AP contribution: 150 + 30 = 180.
The simulation now follows that footer, with 120/180/270 raw damage at
1/2/3 stars and base stats. The live runtime formula is not exposed in the
archived character bin; this correction resolves the documented source
contradiction rather than claiming independent runtime verification.

The patch-specific `overrides.json` records the evidence in Murkwolf's
`damageFormulas` entry. This narrowly replaces his leap with additive
`AttackDamage`/`AbilityPower` terms from the original curve rows. Their
combat coefficients and base-stat card values are resolved together, including
subsequent numeric curve overrides. Other calculation references and his
empowered attacks retain their existing behavior. Automatic refresh keeps
this correction and still rejects unreviewed changes to upstream formulas.

Correcting the card values also lowers the median non-tank dummy's ability
damage from 335 to 318; its defenses and attack stats are unchanged. All
scenario rankings and item-core calculations are regenerated with that
consistent benchmark. `test_tft_murkwolf` checks independent hand values,
AD/AP scaling, actual leap and empowered hits, source preservation, numeric
updates and refresh behavior.

## Carry and fighter target defenses

Every target in damage tests starts with team-supplied Sunder and Shred,
active for the whole fight. The percentages come from the corrected Last
Whisper and Void Staff rows: **30% armor reduction and 30% MR reduction**
on 18.1d. This includes both frontline tanks and the rear non-tank.
The fixed first tank has 3,000 HP and base 110 armor / 110 MR; its effective
defenses are **77 armor / 77 MR**. The next tank's 45/45 becomes 31.5/31.5,
and the rear target's 40/40 becomes 28/28. The dashboard and CLI show
defenses after these reductions.

The engine applies the strongest active percentage once. Last Whisper,
Void Staff and matching auras do not stack another reduction, while their
stats and other effects still work. A stronger timed effect can replace
the baseline until it expires, then the permanent reduction remains.
Flat resistance reduction remains a separate mechanic. No target Wound
or other team utility is assumed by this change.

`targetDebuffs` is separate from `enemyDebuffs`, which describes the
incoming debuffs on a tank being tested. Carry and fighter specs default
to the team reduction; isolated custom tests can explicitly supply
`targetDebuffs: {}`. The tank survival scenarios retain their existing
outgoing target defenses and incoming debuffs. Reference carry pressure
still uses a target with zero resistances to measure raw damage.

## Core items and flexible completions

The dashboard analyzes every legal three-item build before the table is
limited to its top 250 rows. Carries and fighters group those fights by a
two-item foundation; tanks group them by a single item. Carry and fighter
recommendations also simulate every legal two-item pair **without a third
item** (630 pairs in the current pool). Those fights use the selected star
level, traits, targets, resistance reductions, pressure and duration. Their
results measure the pair's strength before the final item is available;
they do not change the fight to an earlier game stage or predict item drops.

A completion is close to best when its primary result is within **5%** of
the best build in that scenario. Carry and fighter clears compare
`100 × (build kill time − best kill time) / best kill time`; if no build
clears, they compare percentage damage lost instead. Tanks compare
percentage hold time lost. Builds that cannot clear are not given an
invented kill time. When the best tank reaches the regular survival cap,
only other regular survivors can compare by the double-pressure test.
If the best survives both tests, other double survivors tie; failed caps
have no exact percentage penalty. Zero-damage fights supply no core
recommendation. Ranking tie-breakers such as overkill do not make an item
essential when the primary result is tied.

Carry/fighter suggestions need at least **two distinct third-item choices**
whose completed builds fall within the 5% band. This eligibility filter runs
before grouping, so a pair with only one strong finish cannot suppress a
more flexible pair. If none qualify, the core panel says so and the full
build rankings remain available. Eligible pairs are ordered by their **actual two-item** result:
clearing pairs by kill time, then non-clearing pairs by damage dealt before
the fight ends (or the fighter dies). Ties favor more strong completions,
then the best full-build result and stable item IDs. The strongest pair is
kept, and weaker pairs sharing any near-optimal three-item completion with
an already-kept pair are grouped under it. Thus AB, AC and BC leading to the
same strong ABC build do not occupy three suggestions. Only kept pairs
suppress others; overlap through a discarded pair does not merge unrelated
choices. Up to six distinct suggestions are shown.

Tank recommendations keep their existing ordering by the number of strong
completions, then best survival. Completion counts measure available choices,
not match frequency or drop probability. The panel initially shows only the
top three completions, with the rest available in a disclosure. One loss
column compares each completed build with the scenario's overall best.
Every completion row shows all three items, with the selected core first
and its finishing item last (or both finishing items after a tank core).
The API also retains the loss against the selected core's best completion.

An item is marked essential within the selected scenario and tolerance
only if the best full build **without any copy** has a measured percentage
loss outside the band. Missing a clear or survival limit is reported as a
separate requirement to reproduce that outcome, with an unknown percentage
cost. For example, dying at 59.999s versus holding for 60s+ cannot establish
a loss greater than 5%. All three slots are reoptimized. Duplicate cores also test
the best build allowing at most one copy, so a strong first copy does not
imply the second is necessary. Multiple equally strong foundations and no
essential items are valid findings.

The scenario comparison reports, separately for each star level,
formation, trait context and tank threat, whether the same foundation has
a completion within 5% of that scenario's own best. It covers all cores,
including those outside the six suggestions, and reports missing caches
without simulating during a web request. It does not average different
star levels or treat these scenarios as equally likely in a real game.

`lol_tft.analyze_cell` exports compact full-precision outcomes for all
builds alongside the usual rich top rows in one enumeration.
`lol_tft.score_pairs` independently batches the legal two-item fights.
`analyze_cores` in `tft.py` produces `coreAnalysis` in each cached cell;
the production carry/fighter path requires complete pair scores and stores
each recommendation's `spike` result. Calls without pair scores retain the
full-build-only analysis and explicitly label their selection mode.
The live and static
APIs expose `api/tft/<champion>/cores.json` for comparisons across settings.
The engine, mathematical fixtures, and cache integration have separate
regressions in `test_tft_scores`, `test_tft_cores`, `test_tft_pairs`,
`test_tft_pair_cores`, and `test_tft_core_cache`.

The item panel keeps `coreAnalysis.optimal` visible as the true best full
build, including when no pair has enough strong completions to qualify.
Exact component recipes are supplied in item metadata, counting repeated
ingredients separately. The optional component preference keeps the selected
core's best completion first, then previews two different component footprints
within 5% of the global optimum. Alternatives minimize the largest component
count, then the sum of `count × (count − 1) / 2` over components, then measured
performance loss. Unfilled preview slots and expanded rows keep performance
order. All results retain their original scores and losses; this preference
does not estimate item-drop probabilities or enforce an inventory. Check it
with `node jobs/test-tft-item-ui.cjs`.

Composition champions link to the individual item panel at their board star
level, geometry and tank threat, starting without traits. These individual
tests differ from the composition's actual board traits and shared allocation,
as the UI explains. The joint composition objective can legitimately choose
offensive items on a designated tank when another frontliner provides most
of the board's durability. Component preferences do not change board scores.

## Azir's adopted mana lock (2026-09-06)

Azir now locks attack mana, item attack mana and regeneration through all
six empowered commands. This replaces the earlier assumption that he could
resume mana after one second and refresh an unfinished command window.
The sixth command grants no mana; ordinary mana gain resumes afterward,
without an additional one-second delay. A regeneration tick crossing the
release time pays only for the portion after release. The lock follows
commands spent, so attack-speed changes and target deaths do not end it early.

Roger adopted this rule based on the six-attack lock described by
[TFTraits](https://www.tftraits.com/champions/azir/). It is the selected model
rule, not a claim of new primary runtime verification: the
[character bin](https://raw.communitydragon.org/latest/game/characters/da_18_azir.cdtb.bin.json)
does not expose this part of the ability. Other timing and audit limitations
still apply.

In the two-star, low-trait damage test, Protector's Vow advances Azir's
first cast from 4.00 s to 1.33 s: its 20 starting mana plus two attacks
fills his 35-mana bar. This brings his soldiers and attack-speed buff
online earlier. Removing Vow's armor, MR, shield and low-health mana
leaves the fight result and trace identical; the carry receives no attacks.
Starting mana is applied once and capped at maximum mana.
`test_tft_azir` checks these item effects and the complete lock, including
Nashor's Tooth/Shojin/Blue Buff builds, changing targets, and partial regen ticks.

Executioner already supplies spell crit in this context, while Summoner
increases soldier damage. Two Deathcaps add 110 AP and 30% damage amp.
Their appeal does not imply that either copy is essential; the core
analysis still checks other items and complete replacement builds.
Riot describes Azir's [Executioner and Summoner interactions](https://teamfighttactics.leagueoflegends.com/en-sg/news/game-updates/enchanted-wilds-overview/).

With the first target now at 110 base armor/MR (77 after team reduction),
enumerating all 7,770 builds gives these results in either geometry:

| Build | Clear time | Result |
| --- | --- | --- |
| Giant Slayer + two Rabadon's Deathcaps | 14.3068 s | First row |
| Protector's Vow + two Rabadon's Deathcaps | 19.7333 s | Row 581, about 38% slower |

These replace the earlier no-lock results of 10.1723 s and 10.6667 s.
Vow still advances the first cast, but it no longer sustains soldiers by
refilling mana during the empowered attacks.

## Standalone tank benchmarks

The champion item analysis compares tanks against three synthetic threats:
mixed damage, physical attacks and
magic burst. Each has **three frontline blockers and two backline damage
dealers**. Slots are ordered nearest first. Local area effects and nearby
item auras reach the frontline; explicit global or distant targeting can
still reach the backline. Hecarim's riders choose the three nearest enemies
in both formations, so the frontliners take his stun while both carries
keep attacking. Scheduled spells that become ready during CC wait until it
ends, then resume their cadence without losing a cast or building a queue.

All profiles share an incoming damage budget calibrated by the Rust engine:
three median 2★ frontliners plus the 20-second average damage of two 2★,
three-item reference carries against one immortal 3,000-HP target with zero
resists and no traits. Aphelios uses Guinsoo's Rageblade, Kraken's Fury and
Infinity Edge; Ahri uses Spear of Shojin, Jeweled Gauntlet and Archangel's
Staff. On 18.1d this gives about **1,265 pre-mitigation DPS**, split into
172 from the frontline and 1,093 from the backline. Calibration is cached
by its complete resolved inputs and follows snapshot/item changes.

The presets vary the backline's damage split and timing: mixed is half
physical attacks and half staggered magic spells; physical is 85% attacks
and 15% physical spells; magic burst is 15% physical attacks and 85% magic
spells arriving together every four seconds. The frontline keeps its
median attack/spell cadence in every preset. The UI displays the resulting
whole-board physical/magic split and each line's DPS. These remain fixed
comparison profiles; they do not replay the reference carries' full
timelines, item ramping, movement or overtime.

The first frontliner's defenses are 3,000 HP / 110 armor / 110 MR; the
other two use tank medians and both backliners use non-tank medians. Carries
and fighters retain their existing three-target benchmark.

Tank fights assume continuous enemy Wound, Sunder and Shred from combat
start, using the corrected Morellonomicon, Last Whisper and Void Staff
rows: currently 33% less healing and 30% less armor/MR. Healing reduction is
applied before the missing-health cap; shields and max-health grants are
unaffected. Resistance reduction includes temporary bonuses and Gargoyle
stacks, and also applies to on-death bodies.

The primary score is hold time. A build or on-death body still holding at
60 seconds is shown as `60s+`, then tested again with twice the incoming
attack and spell damage. Surviving both tests is a tie. Other ties use
damage denied, then damage dealt. Opening physical/magic EHP reflects
actual combat-start defenses after Sunder/Shred and initial durability;
it excludes healing, shields and attack-only damage reduction. Effective
healing, shields consumed and casts explain the simulated result. Ally
health and survival are not simulated, so team utility is only tallied.

The dashboard adds the two additional threat variants only for tanks.
The mixed scenario retains its existing key; physical and magic keys add
`-physical` and `-magic`. There are 1,770 cached cells for this roster.
For example:

```sh
python3 lol.py tft sim Leona --threat magic --items 'Warmogs Armor' 'Gargoyle Stoneplate' 'Spirit Visage'
python3 lol.py tft top Amumu --threat mixed
```
