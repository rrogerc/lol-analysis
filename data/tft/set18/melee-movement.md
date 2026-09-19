# Shared melee engagement estimate

The user accepted a common approximate delay for melee walking and movement.
The finite damage benchmark uses **0.5 seconds per engagement**, covering the
initial engagement and each death of the actor's current target. Short-range
means the equipped range is 1 or 2; AP Nidalee's resolved range 5 is exempt while
her AD range 1 is included. There are no champion-specific discounts for leaps
or speed bursts in this first shared approximation.

This number is a model assumption, not a measured average or a decoded game
constant. The previous source audit recovered historical/legacy movement
speed 500 but no verified current conversion to seconds per hex. The new policy
does not claim exact paths, adjacency, body blocking, jump animation, or lobby
frequency. The shared estimate applies in both clump and spread layouts.

## Event semantics

`tft.dummies_for` declares `meleeRepositionSeconds` on finite damage benchmarks;
`tft.cell_spec` passes it to Rust. Missing or zero leaves a stationary workload.
The equipped range is resolved before movement is enabled. Immortal response
and composition probes, and the separate tank survival presets, do not enable
this policy. No periodic target deaths or reposition events are fabricated.

An independent arrival deadline gates new attacks and casts. Attack cooldowns
continue during travel; readiness is the latest of the existing action gate
and arrival. Existing DoTs, burns, regeneration, item/passive clocks and enemy
pressure keep running. Ordinary movement supplies neither mana nor immunity;
fewer attacks naturally mean fewer attack mana/item triggers. An actor can die
before arrival. At non-tick arrival times, a full bar can cast without waiting
for another quarter-second tick.

Only loss of the current target starts another move. If the destination dies
during travel, the old arrival is cancelled and the new engagement deadline
starts then. Overlapping attempts are counted once in elapsed movement time.
The final target's death ends the fight without adding a useless last move.

`Driver::target_changed` runs at a valid arrival after current-target death:

- Brambleback's leap hits the arrival target, and successive lethal leaps
  incur successive engagement delays. A collateral kill cannot start a leap.
- Akali AP retains the remaining repeat volleys and their damage reduction;
  the next volley resumes at arrival without a second cast/mana-lock charge.
- Master Yi keeps a pending second hit when his first strike kills the current
  victim. Both AD and AP continuations wait for arrival. Same-target double
  strikes retain their existing timing.

Existing zero-delay driver behavior is preserved. These changes implement the
shared assumption; they do not establish live per-champion mobility semantics.

## Verification and sensitivity

`test_tft_movement.py` uses hand-computed attack/arrival times, cooldown overlap,
range controls, incoming lethal pressure, continued regeneration/DoTs, a
non-tick arrival, malformed inputs and both equipped Nidalee forms.
`test_tft_movement_chains.py` covers repeat chains, collateral kills, cancelled
destinations, death during transit and both Yi forms. The UI harness checks
new metadata, elapsed movement, ranged exemptions and historical payloads.

The fixed-build sensitivity test uses the published best items at the highest
legal star, no traits, and both layouts: 84 builds total, 28 with short range and
56 ranged. Each is measured at 0, 0.25, 0.5 and 1 second per engagement. Every zero-
delay result reproduces the published metrics exactly; every ranged result
is unchanged at every tested delay. Items are held fixed for this comparison.

| Engagement delay | Median short-range DPS change |
| --- | ---: |
| 0.25 seconds | -2.58% |
| 0.5 seconds | -4.98% |
| 1 second | -12.94% |

At 0.5 seconds, observed changes range from -3.26% to -8.55%. For clumped targets,
the fixed builds lose 5.02% on AD Nidalee, 4.43% on Brambleback, 5.58% on AP Akali,
and 4.55% on AD Master Yi. After reoptimizing items in the capped-star, clumped,
no-traits leaderboard, Nidalee's winning build switches from AD to ranged AP.
These are modeled sensitivity results, not match
observations or a percentage penalty applied to damage.

Reproducible evidence is under `.cache/tft/melee-reposition/`:
`sensitivity.py/json/log`, the before-source/engine/fixture capture, native build
and verification logs, and the champion-cache rebuild record. Saved result
rows expose `movementTime` and `repositions` only for enabled actors. Movement
time includes overlap with attack cooldowns; it must not be relabeled as pure
extra fight time or exact damage lost.

Engine `a0cc14674fde` passes the full 918-test TFT suite (917 passed, one existing
skip), eight Rust tests, and the damage UI harness against both synthetic
fixtures and the newly computed 65-champion leaderboard / 250-row Nidalee cell.
All 7,670 prior golden fights also pass with their original zero-delay inputs.
The final 1,770 champion cells rebuilt in 132.863 seconds, following an initial
134.247-second rebuild. The final saved-data schema keeps required metrics
mandatory while adding optional movement counters; all 1,770 ranked outputs
match the first movement rebuild exactly. Compared with the preceding
stationary generation, the fixtures contain 216 changed and 1,554 identical
ranked cells. The structured verification record is `melee-movement.json`
beside this document.

The implementation and champion cache rebuild do not publish a new composition
generation. The currently served complete dashboard generation remains pinned
until a separate complete preparation/publication succeeds.
