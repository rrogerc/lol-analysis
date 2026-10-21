# Archived late-game composition analysis

Archived on **2026-10-11**. The late-game Compositions dashboard was retired,
and normal refreshes, immutable publications and static exports no longer
prepare or serve it. Its implementation, shared helpers, tests and caches
remain available; the Early comps, Trait flex and champion views continue
in the active dashboard.

Exporting over an older static site moves its composition response files into
`archive/tft-compositions/<timestamp>/` within that export, preserving them
outside the active API directory.

The material below preserves the former analysis and publication workflow as
historical documentation. References to its UI, endpoints, automatic warms and
published generations describe that former workflow, not current behavior.

For an explicit offline diagnostic warm, run from the repository root:

```sh
python3 lol.py tft comps warm
```

This command is manual only and does not activate dashboard responses. Use
`--workers 1 --only c4-spread-mixed` to limit the diagnostic to one serial
context. See the [active TFT documentation](../README.md) for current refresh
and verification commands.

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

[team-planner.json](../set18/team-planner.json) pins all 65 champion IDs from
[Riot's client planner export](https://raw.communitydragon.org/16.19/plugins/rcp-be-lol-game-data/global/default/v1/tftchampions-teamplanner.json)
(16.19, live 18.3). Riot renumbered Ivern (405 → 404) and Lux (413 → 405) in the
16.18 client; the catalog, first pinned from 16.17, exported both wrongly from
18.2 until it was refreshed on September 23. Re-check it against the `latest`
export when a patch lands. The combat lookup's `code` fields, a PBE build,
already had the new IDs, but they are still not a fallback. The [version-two encoder](https://github.com/nkhoit/tftkit/blob/1c0025d6883ae96d842e5fadaa9d3f28bc543899/web/traits/team-code.js)
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
model exists. A timed Sunder/Shred (Caustic, Last Whisper, Void Staff, an
ability's own) lands on the target its holder hits and lasts its duration
there; allies hitting that same generic target share it while it runs, so
nothing is reduced before the first hit or after the provider stops hitting
(until 2026-09-22 the strongest provider covered every target from the first
second). Only an aura (Evenshroud, Ionic Spark) is standing coverage of the
frontline. One provider owns each nonstacking ordinary/Inferno burn
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

The [persistent-targeting report](../set18/pressure-targeting.md) records the v3
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
[repair record](../set18/champion-audit/repairs.json) contains full revisions and
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
and regaining the trait is unverified. See the [current trait models](../set18/trait-models.md)
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

The [36-trait audit](../set18/trait-audit.md) records source evidence, corrected
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
