# TFT snapshots and live patches

The active snapshot is the newest archived TFT patch, including hotfix
suffixes (`18.1 < 18.1b < 18.1d < 18.2`). Patch 18.1d is checked against
Riot's 18.1 article through the August 31 balance changes and September 1
bug-fix update. Riot labels those sections by date; D is the third
mid-patch update after the initial release.

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
  are recorded as 404s; transport/server errors abort a refresh.
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
The TFT tab shows the last check, calculation progress, failures and changes
that need review. Open tabs check for a new revision every minute and reload
their current champion/scenario when it is ready. Logs:

```sh
journalctl --user -u lol-tft-refresh.service -n 50
systemctl --user list-timers lol-tft-refresh.timer
```

Review candidates are retained under `set18/.pending/<patch>/`. To resolve
one, inspect the changed sources and mechanics, add justified patch-specific
`audit.json`/`overrides.json`, then rerun `tft refresh`. Never update audit
hashes merely to pass validation. Manual `tft fetch --patch 18.1d --force`
still requires an existing matching audit; `tft check --patch 18.1d` reports
its numeric checks. Supplying a base version such as `18.1` resolves to its
current hotfix. Restart the dashboard after code changes so it loads the
new engine and cache generation. Prefer `tft refresh`, which prepares the
complete publication before signaling that reload; a manual individual-cache
warm does not activate new dashboard responses on its own.

## Verification

```sh
python3 -m unittest test_tft test_tft_tanks test_tft_data test_tft_update test_tft_refresh test_tft_ui test_tft_scores test_tft_cores test_tft_core_cache test_tft_target_debuffs test_tft_pairs test_tft_pair_cores test_tft_azir test_tft_murkwolf test_tft_leaderboard test_tft_loadouts test_tft_comp_traits test_tft_comps
python3 jobs/tft_compare.py
python3 -m unittest test_tft_refresh test_tft_site test_tft_serving
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

## Composition cores and level-nine upgrades

The Compositions subview fills eight team slots with distinct shop champions around a
same-cost main carry/fighter and main tank. It compares one carry/one tank,
two carries, two tanks, and two of each under the **same completed-item
budget**. The default is nine items; all budgets from six through twelve
are available. Every shown allocation spends that exact budget with at most
three items per champion. The main pair receive at least two items; a second
carry/tank means another unit of that category receives at least two, and
cannot receive more items than its corresponding main. Other items go to
supports. Main means the upgrade target and formation anchor, not necessarily
the highest damage contributor. Item categories remain unrestricted; their
value is judged by the whole team's encounter outcomes.

The initial planning rules encode Roger's preferences explicitly; they are
not derived shop probabilities:

| Plan | Main pair | 4-cost cap | 5-cost cap |
| --- | --- | --- | --- |
| 1-cost reroll | Both 3★ | 1 support | None |
| 2-cost reroll | Both 3★ | 2 supports | None |
| 3-cost reroll | Both 3★ | 3 supports | 1 support at 1★ |
| 4-cost level-8 core | Both 2★ | 8 | None |

Every level-8 board has at least four champions of its target cost, three to five
frontliners, and at least two damage units. Supports are 2★ except the
explicit 1★ 5-costs; same-cost supports are not automatically upgraded to
3★. Purchase gold is the cost of the copies fielded (1/3/9 copies per star),
excluding XP and rerolls. Component demand comes from the chosen item
recipes; actual drops are not constrained. Cost density is a planning rule;
only search-pool win rate determines the reported rank. Equal rates share a rank.

Elder Dragon occupies **two team slots** and contributes **two Riftbeast in
total**, as stated in [Riot's Enchanted Wilds overview](https://teamfighttactics.leagueoflegends.com/en-sg/news/game-updates/enchanted-wilds-overview/).
A level-8 Elder board therefore has seven champions; a level-9 Elder board has
eight. The shared `tft_board.py` rules apply to roster search, resolved traits,
reference validation and the displayed occupancy. Apex Predator's slot/trait
effect is structural; Elder's combat ability remains in its champion driver.

Four-cost plans select and rank their **level-8 cores without any 5-costs**.
Only after these decisions are fixed does `tft_caps.py` choose an optional
level-9 upgrade for each result. It keeps the main carry and tank, then compares
selling one support and buying two distinct ordinary 5-costs, or selling one
support and buying Elder Dragon. Adding one ordinary 5-cost without selling
anyone is also a fallback. All new legendaries, including Elder Dragon, are **2★**.
Their purchase cost includes three copies each. The cap keeps at least
three target-cost champions and three to six frontliners. Traits may change;
all active Riftbeast Alpha choices are compared again.

Upgrades use the **same completed items**. Surviving units keep their stars and
items; only the sold support's items can transfer, and only onto newly added
units. Every legal split of those freed items is compared. The allocation may
move between the existing single/duo carry/tank arrangements, but the usual
holder limits still apply. No free additional items, item reforging or wholesale
reitemization is assumed. A cap carries `itemPolicy` and transfer details instead
of the core's full item-replacement analysis.

Caps are chosen by search wins, with two legendary slots preferred on ties
(Elder fills both). Remaining ties prefer selling fewer invested champion
copies, then stable champion IDs. Every legal transition under this policy is
compared unless a preferred cap wins all twelve search fights: unseen candidates
cannot improve its score or slot preference. Held-out and healing-sensitivity
checks follow selection. Cap results never select, suppress or rank a level-8
core. Both levels face the **same level-8 reference boards and item budget** to
show the effect of the transition; this is not a benchmark against level-9
opponents or an estimate of the chance of reaching the cap.

`tft_comp_traits.resolve_board_traits` counts the selected champions and
resolves actual breakpoint columns instead of the item builder's generic
bare/low/high contexts. Brawler, Defender, Juggernaut, Invoker, Rapidfire and
Spellweaver team shares reach nonmembers once; members retain their own
effect. Solar's modeled shield, bonus magic damage and 3★ bonuses are
teamwide. Exactly one eligible Riftbeast receives the Alpha Mark. Eclipse's
zero-level placeholder never activates automatically. Blackthorn uses no
sacrifice and grants no sacrifice bonus, preserving every selected combat unit.
Unmodeled plants, BFF/Rider effects, histories, positional bonuses and other
limitations are listed on each relevant board.

`tft_comps.Search` seeds every legal main pair, screens partial and full
boards with a deterministic beam, refines sixteen diverse boards, and checks
four support swaps. Slot-preserving swaps can exchange two ordinary supports
for Elder or Elder for two supports. Every swap resolves traits and initial allocations again.
The screening heuristic does not determine the displayed combat score.

`lol_tft.simulate_match` in `tft_engine/src/symmetric.rs` runs champion drivers
on both sides. Both teams use actual items, traits, mana, incoming damage,
shields, healing, crowd control, reactive effects and death behavior. Damage
is resolved through the recipient's own defenses and health before the donor
receives damage/healing credit. Reciprocal damage uses a deterministic queue.
Symmetric fights support up to nine champions per side. Existing fights with
eight or fewer actors retain exactly the same combat results; standalone dummy
limits are unchanged. Positions remain fixed front/back rows and lanes with simple targeting.
The older synthetic `simulate_team` API remains available for diagnostic tests;
composition rankings no longer use synthetic damage sources.

`data/tft/set18/composition-opponents.json` declares a versioned, independently
authored pool: six search boards and three distinct held-out boards. Every
reference fills eight team slots, including when testing a level-9 upgrade.
References follow the level-8 cost/star rules and have exactly the candidate's item budget,
using predeclared item priorities. Every opponent is tested with both equal-time
initiative orders. A full search comparison therefore contains twelve fights;
the independent held-out check contains six. Opponent rosters, positions, items,
traits and source/model limitations are included in the matchup data. The pool
is fixed during optimization and never loaded from the optimizer's own winners.
Its version, data hash, snapshot and item budget identify the evaluated inputs.
Version `18-reference-v2-level8` replaces the four-cost Ezreal board's unitemized
1★ Gnar with 2★ Kobuko, preserving its Sprykin/Brawler traits while removing its
5-cost. Other reference champions and item priorities are unchanged.

Ranks compare search-pool wins divided by test fights. Draws and timeouts are
not wins. Exact ties share a competition rank; remaining HP, damage and clear
time are diagnostics. Held-out boards are evaluated only after all published
rosters, allocations and ranks are fixed. Their results never select items,
replace boards or reorder the ranking. These benchmark rates are not estimates
of live-game win probability.

The compiled `optimize_loadouts` still screens initial zero-to-three-item
loadouts. Every one-item candidate is retained; Gunblade has no special
ally-healing category. `tft_comp_items.ItemSearch` then refines finalists from
multiple seeds, comparing every legal single-item replacement against the full
search pool. It also checks transfers between champions, exchanges of equipped
items, and selected paired replacements. Only additional benchmark wins accept
a change. Refinement stops when none of these tested changes improves wins;
it does not claim a global optimum over all boards and item combinations.

Each final allocation carries `itemAnalysis`, tied to its search pool and exact
holder/item slots. Every legal replacement reports its win difference and the
specific winning matchups gained or lost while the rest of the board stays
fixed. Equal total wins may exchange matchups. The UI previews three alternatives
and lets the user expand the rest. A preferred-item label requires every tested
replacement to lose wins; equal outcomes remain alternatives within this pool.

Gunblade heals one lowest-percentage-HP living ally and discards excess healing.
Self omnivamp is separate; damage is capped before healing. Quicksilver's archived
immunity duration, Titan's full-stack Unstoppable and Vi's spell immunity
affect incoming crowd control. Sentinel's Mana Reave reduces actual opposing
mana and can delay enemy casts. Shred, Sunder and Wound come from actual providers; ordinary and Inferno
burns each have one independently refreshing channel, with Wound applied once.

Current-set eligibility of burn/trait/item-proc healing and post-death ally
healing is not fully established. The main model retains the previous broad
convention and explicitly passes it to the engine. Final boards additionally
receive `assumptionCheck`: held-out fights repeated with those healing paths
disabled. This is a sensitivity experiment, not a claim that the restricted
rules are correct. It never changes the selected items or rank, and its changed
wins/outcomes are shown beside the held-out results.
Multiple Gunblades on one holder currently combine their ally-healing amount
before selecting that recipient. Independent per-copy recipient timing remains
unverified and is not covered by the restricted-healing experiment.

The displayed unit contributions use actual whole-fight time, including time
after a unit dies. Their DPS sums to the team figure. Frontline time ends when
the last frontliner or its active on-death body falls, capped at fight end.
Heals received, shields absorbed, effective ally healing provided and raw ally
shields granted remain distinct diagnostic fields.

The model retains coarse positions, expected critical strikes, approximated ally
targeting and declared per-kit/trait omissions. Some channel, in-flight and
post-death ability behavior is approximated. A bounded search and a small authored
opponent pool can still favor particular strategies; the separate held-out and
healing-assumption checks expose some of that sensitivity. Full movement and a
calibrated distribution of real player boards are not simulated.
The selected clump/spread coverage assumption applies to both teams; fixed rows
and lanes determine their target preferences.

Eight canonical composition contexts cover four cost plans and two formations,
each with all seven item budgets and four arrangements. The reference pool
replaces synthetic physical/magic pressure presets; canonical API keys retain
`-mixed`, and the UI normalizes older pressure links. Individual champion threat
settings are separate. APIs are `/api/tft/compositions/meta.json`, `status.json`
and `<c1–c4>-<spread|clump>-mixed.json`. Missing artifacts return 202. Requests
never run simulations, and static exports include the same artifacts.

`python3 lol.py tft comps warm` uses a bounded pool of up to eight spawned
workers. Each context first completes its dependent roster screening, then its
independent finalist item searches share the pool. This keeps several workers
useful even when a single context remains. The parent restores original result
order before ranking and schedules held-out validation only after selection is
finished. Independent level-9 jobs follow the finalized four-cost cores, using
otherwise available workers while remaining core calculations have priority.
A context publishes only after its upgrades finish. `--workers 1` or explicit
`--only c1-clump-mixed` runs serially.
Workers receive the parent's loaded snapshot, and the parent validates
source/data revisions before atomic publication. A failed or missing context
never marks the warm complete. Cache revisions include composition code,
item-search code, trait resolution, exact-score cache code, opponent data and
the baseline engine/data revision. Metadata/artifacts carry
`evaluationModel=symmetric-reference-pool-v1` and
`boardPlanModel=level8-core-level9-cap-v1`. Every board includes its level,
capacity, used slots and champion count; units include their slot costs.
Four-cost rows have a linked `level9Upgrade` with the full board, sold/added
champions, item transfers and trait changes. The UI shows the core first and
opens the optional cap on demand. Legacy metadata/artifacts are accepted only
together during a publication handoff, without labeling them as the new policy.

The native `prepare_actor` API resolves immutable champion and item inputs once;
`simulate_matches` runs ordered batches with fresh mutable state for every
fight. Search scoring requests compact results, preserving all scalar values
while avoiding unused per-unit report construction. Selected builds get full
diagnostic replays. Native workers default to one inside the process pool to
avoid nested parallelism. Allocation and eligibility-scan optimizations retain
the same floating-point operation and combat event order.

Identical compact fights can also be reused across searches and processes from
`.cache/tft-comps/fight-scores/`. SQLite WAL stores exact binary doubles with
checksums. Cache identity covers resolved actor/item/opponent inputs, positions,
formation, search/held-out selection, duration and healing policy, plus the
engine/data/model revision. Changed inputs miss the cache; corrupt records are
discarded and recomputed. This changes neither the comparisons performed nor
the winner-selection rule. Full diagnostic requests still run full fights.

On 2026-09-06, fixed one-worker workloads with caching disabled took 0.416 to
0.215 seconds for 240 fights, 4.462 to 2.314 seconds and 6.559 to 3.397 seconds
for complete spread/clump replacement passes, and 15.148 to 8.507 seconds for a
non-perfect board's full 642-allocation refinement (1.78–1.94× faster). Reused
results are a separate benchmark condition. `jobs/tft_exact_verify.py` captures
an independent frozen implementation, replays raw/prepared/compact APIs, times
fixed workloads and compares complete generated artifacts. The performance
change preserves the existing golden fixtures; it does not regenerate them.

Before adding the level-9 planner, the complete eight-context composition rebuild from an empty cache took
1,117.4 seconds (18 min 37 s), compared with the previous generation's
3,287.7 seconds (54 min 48 s), using eight workers on the same host. It still
compares 547,267 item allocations and 521,526 single-item trials. All 1,664
published boards, 520,064 per-slot replacement records and logical search
counts match exactly. First-run reuse was small: 49,956 identical fights were
reused while 6,949,632 were simulated. The 1,770 standalone champion cells
rebuilt separately in 126.2 seconds; their full builds, core analyses and
diagnostics also match, with only calculation timestamps/timings differing.

A separate repeat rebuild removed the eight generated result files but kept
the unit-benchmark and exact-score caches. It took 79.5 seconds, reusing
6,959,652 identical fights while still running 39,936 full diagnostic fights
and 162,561 standalone loadout samples. It performs the same logical search
comparisons. This measures reuse of previous work, not first-time combat
throughput; existing ready artifacts already load directly without rebuilding.

With slot-aware level-8 cores and the original 1★ level-9 upgrades, the full scheduled
refresh on 2026-09-07 took **23 minutes 34 seconds**, including source checks,
1,770 champion cells, all eight composition contexts and compressed publication.
It produced 1,664 level-8 cores and 416 level-9 variants. The cap phase compared
618 legal rosters and 1,121 allocations; all 416 selections reached the exact
perfect-score bound. The final audit checked every board, transition, all
520,064 core item alternatives and all 1,919 published response files.

Relevant regressions are `test_tft_symmetric.py`, `test_tft_team_engine.py`,
`test_tft_team.py`, `test_tft_opponents.py`, `test_tft_comp_items.py` and
`test_tft_comps.py`, `test_tft_caps.py`, `test_tft_nine_units.py`, plus `test_tft_prepared.py`, `test_tft_team_batch.py`,
`test_tft_match_cache.py` and `test_tft_exact_verify.py`. The persistent UI checks are
`node jobs/test-tft-composition-ui.cjs` and `node jobs/test-tft-item-ui.cjs`.

## Champion leaderboards

The TFT Leaderboard compares each champion's optimal three-item build under
one selected formation, trait context and tank pressure profile. Damage
includes carries and fighters; tanks have a separate survival leaderboard.
The default star setting uses 3★ for 1–3 costs and 2★ for 4–5 costs. Fixed
1★, 2★ and 3★ comparisons are also available, with 4–5 costs excluded from
3★. Each champion appears once in an eligible comparison. Clicking a
champion opens the exact build scenario used by that row.

Damage ranks successful clears by time, followed by non-clears by total
damage. The existing models remain in effect: carries are protected while
fighters take incoming damage. The role filter allows comparisons within
either group. Tanks rank by hold time, then the double-pressure test for
survivors of the normal 60-second limit. Equal primary outcomes share a
competition rank, including tanks that survive both limits. Overkill and
utility do not manufacture a cross-champion advantage for tied outcomes;
the winning items still use the existing full build ranking.

Each cached scenario includes a compact `best` record with the winning
item IDs and unrounded performance. The leaderboard reads those records
without running simulations, and caches the small extracted records by
file path, modification time and size. Missing cells are reported as
pending; another star or scenario is never substituted. All 72 selection
paths at `/api/tft/leaderboard/<star>-<geometry>-<traits>-<threat>.json` are
also included in static exports. `star` is `best`, `s1`, `s2` or `s3`, and
the threat suffix is always explicit, including `mixed`.

`test_tft_leaderboard` covers star restrictions, precision, ties, capped
survival, selected conditions, missing/replaced cells and the exact winning
build. This feature does not change combat math or the golden rankings.

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

## Tank benchmarks

Tanks compare three synthetic threats: mixed damage, physical attacks and
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
