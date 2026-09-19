# Persistent incoming targets for composition capacity

The composition scorer uses `ehp-damage-capacity-v3`. It corrects a mismatch
between incoming pressure and per-attacker defenses: v2 split every packet
among the entire eligible frontline while giving each frontliner one current
attacker for Gargoyle and Monolith. Increasing raw DPS could not repair that
attacker-count convention.

## Current contract

Three abstract incoming sources keep individual targets. At the start, sources
0 and 2 target the first eligible frontliner; source 1 targets the second, or
the first if only one is eligible. Each source retains its owner, including
while stunned. Death or untargetability makes it choose the first eligible
entry in the fixed priority. Returning from untargetability does not pull
sources back from surviving owners. Holding bodies retain their slots.

The main tank leads that priority. Other actual frontliners follow in descending
unfocused opening EHP against a 50/50 mix, with champion API breaking exact ties.
This ordering is fixed per item allocation using the first profile's other
conditions. A second orientation swaps the first two entries; both receive
equal weight. The resulting 48 profiles preserve the two pressure levels,
three physical/magic mixes, two antiheal levels and two incoming-control cases.
This is an explicit formation sensitivity assumption, not reconstructed hex
positioning or a distribution estimated from match data.

All assigned sources count toward Gargoyle and Monolith before champion
initialization and throughout measurement. Opening EHP uses those same
assignments to derive the workload. Whole packets and their lethal excess
keep their original source identity. Spent, denied and unspent raw damage must
still sum to the incoming budget. Source masks are cached to avoid rewriting
unchanged target slots, and immutable opening state is reused across mixtures
and pressure levels. Heavy scoring remains in Rust.

Expanded composition profiles show the initial attacker counts and retargeting
priority. Detailed payloads contain `pressureTargetOrder` and
`initialPressureTargets`; compact search results omit these diagnostics.
Publication validates the complete formation pairing and pressure accounting.
Saved v1/v2 generations retain their original UI contracts during rollout.

## Controlled tank comparison

The same two published four-cost, nine-item boards were held fixed. Only the
main tank's three items varied: all 165 triples from nine tank items, with all
legal Primal choices. Item stats and HP stacking rules were unchanged.

| Board's main tank | Two Warmog's + Gargoyle, old rank | New rank | Old deficit to triple Warmog's | New deficit |
| --- | ---: | ---: | ---: | ---: |
| Sett, clump | 11 | 7 | 6.52% | 6.00% |
| Amumu, spread | 8 | 4 | 4.19% | 3.46% |

Triple Warmog's still wins both controlled comparisons. Correct source
assignment improves Gargoyle's relative standing without proving that the
remaining HP preference is accurate in a real game. Warmog's percentage-HP
stacking semantics remain unverified by primary runtime evidence; no additive
stacking rule or duplicate-item penalty was introduced here. These are fixed
roster comparisons, not claims about the final rebuilt winners.

## Verification

Engine `6bef32c46ca7` passes 21 new routing regressions, including source masks
before initialization, one/two/reserve frontliners, lethal excess, same-source
physical/magic parts, untargetability, on-death bodies, source CC and input
permutations. The independent Python aggregation/analytic tests also pass.
The full TFT suite ran 888 tests with one existing skip; all eight Rust unit
tests pass. All 7,670 previous standalone champion fights and all 1,770 regenerated ranked
champion cells remain bit-identical. The regenerated golden tests also pass.

Complete item-search comparisons agree between native execution and the Python
reference, including winners, item evidence and comparison counts: 3,981
allocations for the nine-item case and 4,478 for the dense twelve-item case.
The official native runs, pinned to CPU 2, took 96.31/95.51 seconds and
50.82/50.54 seconds for cold/repeated passes. Later benchmark passes overlapped
the rebuild on other CPUs. A separate native preview before rebuilding took
89.41 and 47.81 seconds on CPU 0 and matches the same complete reference output.
These measurements indicate cost and verify parity; they are not isolated
microbenchmark speedups. The previous 24-profile model's searches took about
36 and 29 seconds with different comparison counts.

The complete rebuild took **6,444.755 seconds (1 hour 47 minutes 25 seconds)**:
130.898 seconds for champion benchmarks, 6,250.918 seconds for compositions,
and 62.882 seconds for saved responses. Generation
`g-574cbbba155f53206dce4cb966b2a4ab8ab80c5d70d684d4d5cd36689b2fbb68`
is published. Generated-data UI validation passed for all 1,664 cores and 416
upgrades, including 2,080 clipboard roster round trips. All four live TFT
metadata/status endpoints agree on this generation and report all 1,770
champion cells and eight composition contexts ready; the system service is active.

The new search **did not reduce overall Warmog preference**. Among three-item
main/secondary tank appearances in core boards, at least two Warmogs rose from
1,039/1,494 (69.5%) to 1,258/1,663 (75.6%); triple Warmogs rose from 530/1,494
(35.5%) to 645/1,663 (38.8%). These are repeated board appearances across the
same search categories, with changed rosters and item allocations, not
independent match observations. Persistent targeting fixes the source-count
convention; the broader HP preference remains unresolved. Verify percentage-HP
stacking before changing that arithmetic or adding another pressure assumption.

Exact measurements and publication evidence are in
[pressure-targeting.json](pressure-targeting.json). Reproduction logs and
controlled comparisons are in `.cache/tft/persistent-targeting/`.

Core implementation: `tft_engine/src/theory_fight.rs`,
`tft_engine/src/theory.rs`, `tft_theory.py`, `tft_unit_profiles.py`.
Independent evidence: `test_tft_persistent_targets.py`,
`test_tft_theory.py`, `test_tft_theory_native.py`, and the saved controlled
comparisons in `.cache/tft/persistent-targeting/board-comparisons.json`.
