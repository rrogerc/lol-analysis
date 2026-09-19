# Additive percentage-health model

Ordinary item and trait maximum-health bonuses share one additive percentage
pool. This interpretation was selected on 2026-09-09 after reviewing the
previous model's Warmog preference and the available stacking evidence.

`max HP = (star-scaled champion HP + flat bonus HP) * (1 + sum of HP bonuses)`

Data still uses factor notation: Warmog's `hpMult=1.18` contributes +18%;
Blossom's 1.10 contributes +10%; Brawler's 1.25/1.40/1.65 contributes
+25/40/65%. The baseline 1 is included once. Each item copy contributes its
flat health and percentage; there is no duplicate-item penalty or item cap.

Two-star Amumu has 2340 base HP in the pinned 18.1d snapshot. Without other HP
effects, zero through three Warmogs give 2340, 3351.2, 4542.4 and 5913.6 HP.
Previously, two and three items compounded to 4650.616 and 6309.24288 HP.
Sett with Brawler(2), Blossom and three Warmogs instead uses
`(2160 + 120 + 1500) * (1 + .25 + .10 + .54) = 7144.2 HP`, compared with the
previous 8539.65882. These are model arithmetic comparisons, not game readouts.

## Implementation scope

`tft_engine/src/fx.rs` converts item and trait factors to bonus fractions through
the same `Fx::add` path. `Sheet::new` in `fight.rs` applies the combined factor
once, after flat health. All standalone, team, symmetric and theoretical
paths consume these shared stats. Timed flat-HP grants use the same combined
factor. Champion abilities already expressed as gains based on current max
HP retain their separate calculations; blindly pooling or multiplying those
again would count HP amplification twice. Resistances and durability retain
their existing aggregation rules.

`test_tft_hp_stacking.py` provides independent hand-calculated regression
cases for the adopted contract. Golden fixtures were regenerated for this
deliberate numerical model change after the current champion cells were warm.
The compiled source hash invalidates calculation caches automatically.
Rebuild and comparison artifacts are under `.cache/tft/additive-hp/`.

## Evidence and remaining uncertainty

The extracted official Android 18.1 curves confirm the flat and percentage
coefficients for Warmog, Bramble, Dragon's Claw, Brawler and Blossom. They do
not establish stacking: custom gameplay-effect operation/channel fields could
not be decoded with the available native schemas.

[SuperGoody's original Set 12 experiment](https://www.reddit.com/r/TeamfightTactics/comments/1ewsnuv/a_guide_on_itemising_tanks_how_armour_magic/)
supports a common additive pool for two Sterak's and Shapeshifter. The original
screenshots were inspected: Jayce's maximum HP is 2696 before transformation,
3399 afterward and 4571 after both item procs. The last reading matches
`(600 * 1.8^2 + 400) * (1 + .45 + .25 + .25) = 4570.8`; per-effect compounding
would give 5310.625. This is historical evidence, not current Set 18 validation.

Additive stacking is an explicit adopted assumption. Current-game item-item
and item-trait readings remain useful independent verification. Detailed
source records, image hashes and the former engine's 28-case arithmetic
matrix are preserved in `.cache/tft/hp-stacking-verification/`.

## Verification and controlled comparisons

Engine `cfe721d84f2e` passes all five new HP regression tests, 123 focused
combat/composition tests, and eight Rust unit tests. After the 1,770 champion
scenarios and golden fixtures were regenerated, the full TFT suite ran 893
tests: 892 passed and one existing unique-item-pool case was skipped.

All 7,670 previous fight inputs were replayed before updating fixtures. The
6,053 cases with at most one percentage-HP bonus remained identical; all
1,617 cases with multiple bonuses changed. The rebuilt champion rankings
changed 1,079 of 1,770 cells; 691 stayed identical. These checks establish
implementation consistency, not independent current-game validation.

The same original four-cost, nine-item Sett/clump and Amumu/spread boards used
in the previous targeting review were reevaluated. Each compared all 165
triples from nine tank items, holding the roster, stars, other gear, Alpha,
trait inputs, pressure profiles and legal Primal options fixed. Each allocation
still optimizes over those same Primal options.

| Main tank | New best tank items | Triple-Warmog rank, before → after | New winner's advantage over triple Warmog |
| --- | --- | ---: | ---: |
| Sett | Two Warmogs + Crownguard | 1 → 5 | 1.16% |
| Amumu | Two Warmogs + Dragon's Claw | 1 → 2 | 1.71% |

The 330-allocation diagnostic took 9.88 seconds on CPU 8. These are controlled
comparisons, not the final composition leaderboard. Both winners still use
two Warmogs; this arithmetic change does not force a particular item mixture.
Complete paired rankings and input hashes are preserved in
`.cache/tft/additive-hp/board-comparisons.json` and `controlled-inputs.json`.

Both complete production item-search workloads agree with the independent
Python aggregation reference, including winners, complete replacement
evidence and every search count: 3,242 allocations for the nine-item case and
4,183 for the dense twelve-item case. Native cold/repeated runs on CPU 2 took
75.03/75.14 seconds and 46.05/46.22 seconds. A separate native preview on CPU 6
matches those results exactly. The benchmark overlapped work on other cores;
these timings indicate cost and verify consistency, not an isolated speedup.
All numerical searches continue to use the Rust engine in production.

## Published results

Generation `g-1b8d8c0db83f9dd19bad4103d4ff9c0ca866695c3f53073a943a11ad6a2badeb`
was published on 2026-09-09. All four live metadata/status endpoints were
observed on that generation, with all 1,770 champion cells and eight
composition contexts ready. The dashboard system service is active.
Actual generated-data UI validation passed for all 1,664 core boards and
416 upgrades, including 2,080 exact clipboard roster round trips.

Across three-item main/secondary tank appearances in the generated core boards:

| Tank equipment | Before | After |
| --- | ---: | ---: |
| At least two Warmogs | 1,258 / 1,663 (75.65%) | 689 / 1,612 (42.74%) |
| Three Warmogs | 645 / 1,663 (38.79%) | 49 / 1,612 (3.04%) |

The same counting method covers all contexts, budgets and arrangements,
excluding upgrades and support-role frontliners. Rosters and allocations
change during the search, so these are repeated board appearances rather than
independent match observations or a fixed-roster comparison. Warmog's flat HP
and listed percentage were not changed; only ordinary percentage aggregation
changed. The reduced preference does not establish complete live-game accuracy.

Rebuilding took **7,142.628 seconds (1 hour 59 minutes 3 seconds)** in total:
194.053 seconds for champion scenarios, 6,882.191 seconds for compositions,
and 66.296 seconds for saved responses, plus small preparation overhead.
Champion preparation ran separately while verification ran; the total sums
those calculation stages and excludes compilation, verification-only waiting,
UI validation and publication checks. Exact measurements, publication hashes,
test results and both tank distributions are in [hp-stacking.json](hp-stacking.json).
