# Fighters mechanics audit

11 champions in fighters.rs, base forms and relevant Adaptor/Alpha/transform variants. Source/code audit, not live-game certification.

## Camille — base

**Assessment:** source-supported within abstract target model.

Single-target physical slice uses one AD-scaled term plus an AP term; flat 90 shield for 2 seconds at two stars. No AP shield term exists in the pinned calculation data.

- Generic cast/mana/attack timing remains unverified. The two-second shield may overlap on recast because the shared shield list stacks grants; recast behavior is not established by the tooltip.

Evidence: `tft_engine/src/drivers/fighters.rs`, pinned tooltip/calculations and overrides for `TFT18_Camille`. Existing coverage: `test_tft.TestDrivers`, `test_tft.TestBody`.

## Warwick — base

**Assessment:** confirmed accounting inconsistency.

Single-target bite, AP-scaled 20% damage-based healing at 100 AP, and a permanent additive 20% attack-speed gain per cast.

- Standalone hit_ability returns damage before clipping lethal overkill, but the damage ledger and omnivamp use clipped damage. A 500-damage bite into 1 remaining HP credits 1 damage and 100 healing, not 0.2; bridged combat returns clipped dealt damage. This is a mode inconsistency and contradicts the model’s own effective-damage contract; live overkill-heal semantics still deserve observation.
- Permanent AS is represented additively; mana lock and timing use shared assumptions.

Evidence: `tft_engine/src/drivers/fighters.rs`, pinned tooltip/calculations and overrides for `TFT18_Warwick`. Existing coverage: `test_tft.TestCalcs.test_ad_rows_scale_like_base_ad`, `test_tft.TestBody.test_omnivamp_heals_from_damage_dealt`.
Controlled native observations: `warwick_overkill_heal` in `observations.json`, group `fighters`.

## Brambleback — base and Riftbeast Alpha

**Assessment:** partial / runtime unknown.

One live Frenzy refreshes 80% AD for 8 seconds at two stars. Personal armor ignore is 10% plus 30% per 100 AP, after shared Sunder. Alpha attacks burn 1% max HP per second for 4 seconds and heal 4% own max HP, capped by missing HP and Wound.

- The source says leap when the target dies; driver attaches an immediate leap to own kill callback. Kills by allies and loss of a target need a target-death event, distinct from personal kill credit. Theory targets never die, so this passive has no composition contribution.
- Frenzy overlap and mana-lock duration are explicitly conservative assumptions, not live-verified mechanics.
- Leap movement, range, travel time, and whether the leap chains through kills need runtime validation. No Wound is inferred merely from its burn.

Evidence: `tft_engine/src/drivers/fighters.rs`, pinned tooltip/calculations and overrides for `TFT18_Brambleback`. Existing coverage: `test_tft_brambleback`, `test_tft_comp_utility`.

## Diana — base

**Assessment:** partial targeting model.

Six AP-scaled orbs (100 each at two stars/100 AP) and a flat 225 shield lasting 2 seconds. The source does not use the dormant OrbShieldPercentDamage row.

- Orb count is conserved, but even round-robin distribution is assumed. No two-hex spatial query or missile timing.
- If all originally selected targets die mid-orb-loop, fallback selects any living enemy, potentially extending beyond the original two-hex area.
- Shield overlap/refresh semantics are unverified.

Evidence: `tft_engine/src/drivers/fighters.rs`, pinned tooltip/calculations and overrides for `TFT18_Diana`. Existing coverage: `test_tft.TestDrivers`.

## Morgana — base

**Assessment:** partial / trigger semantics unknown.

25% omnivamp, three-target initial blast, four-second marks that can overlap, and a four-second zone at its per-second damage rate. Curse bonus uses the count of unexpired marks.

- The three nearby blast targets use the generic clump/spread selector and become one in spread. Actual reachable count is not modeled.
- Curse bonus is granted on her autos and later blasts, not zone ticks, item damage or ally damage. The tooltip says cursed enemies take more damage per curse without establishing the exact event filter; do not invent a per-tick proc rule.
- The zone’s two-hex area and 0.25-second source tick rate are represented by the abstract target set and shared tick loop.

Evidence: `tft_engine/src/drivers/fighters.rs`, pinned tooltip/calculations and overrides for `TFT18_Morgana`. Existing coverage: `test_tft.TestDrivers`, `test_tft.TestAuditFixes.test_a_dot_pays_for_the_time_elapsed_only`.

## Rengar — base

**Assessment:** partial targeting / runtime unknown.

Chooses the living enemy with lowest HP fraction, stabs once, then heals between the AP-scaled minimum and maximum using the victim’s post-hit missing-health fraction.

- The source’s three-hex reach is ignored. A leap does not itself move the actor or change subsequent auto targeting.
- Linear interpolation and using post-hit rather than pre-hit missing health are reasonable readings, not verified runtime behavior.
- Rivals augment behavior and movement are outside the basic kit evaluator.

Evidence: `tft_engine/src/drivers/fighters.rs`, pinned tooltip/calculations and overrides for `TFT18_Rengar`. Existing coverage: `test_tft.TestDrivers`.

## Elder Dragon — base and Riftbeast Alpha

**Assessment:** confirmed timing / target-anchor discrepancies.

Autos splash at 75% damage; first cast grants 15% omnivamp, global 1.25-second stun and 3-second Ignite at 2% enemy max HP physical damage per second. Breath has 20% falloff per enemy with a 20% floor. Alpha executes below 12%; immortal removal is blocked.

- First-cast protection starts inside the landing callback. Controlled half-second flight takes hits before landing and becomes protected after landing, reversing the source sequence. The source says invulnerable; the model uses untargetable, which also redirects pressure and may differ for ongoing effects.
- Adjacent splash is selected after the primary auto. In spread, killing target 0 produces a 75-damage splash on isolated target 1. The selector has lost the original impact center.
- Breath uses all clumped targets or one spread target, not a line through the current target. Flight/breath duration and projectile travel are unverified.
- Alpha threshold scanning runs after own actions and every tick, rather than strictly on a damage event; this distinction matters with ally damage in shared combat.

Evidence: `tft_engine/src/drivers/fighters.rs`, pinned tooltip/calculations and overrides for `TFT18_ElderDragon`. Existing coverage: `test_tft_symmetric_regressions`, `test_tft_execute`.
Controlled native observations: `elder_flight_timing`, `elder_lethal_auto_spread`, `elder_lethal_auto_clump` in `observations.json`, group `fighters`.

## Murkwolf — base and Riftbeast Alpha

**Assessment:** partial / observed target continuation mismatch.

Leap contains one AD and one AP contribution; next two attacks gain 175% bonus AS and one additional physical hit. Alpha grants Precision plus missing-health-dependent crit chance. Existing formula correction remains intact.

- Leap ignores the source’s three-hex reach. Controlled leap strikes lowest-absolute-HP target 2, but both subsequent empowered attacks go to the old target 0. The driver does not update target/position on leap.
- The ordinary attack and empowered bonus are separate: killing with the ordinary attack loses the bonus. The source does not settle the exact combined-hit/on-hit/crit ordering.
- Each own kill adds two empowered attacks from a data row not mentioned in the current tooltip; this needs confirmation rather than assuming every dormant row is active.
- Alpha crit chance is capped at 100%; any intended crit-overflow conversion for this dynamic gain needs observation.

Evidence: `tft_engine/src/drivers/fighters.rs`, pinned tooltip/calculations and overrides for `TFT18_Murkwolf`. Existing coverage: `test_tft_murkwolf`.
Controlled native observations: `murkwolf_leap_followup` in `observations.json`, group `fighters`.

## Kennen — base

**Assessment:** partial / charge semantics unknown.

Counts living burning targets and snapshots 15 AP per target into this cast; shields, hits every selected enemy on the rush, then splits a fixed total firestorm across them over two seconds. Damage is conserved rather than multiplied by target count.

- The source does not establish whether the gained AP persists beyond this cast; the driver removes it immediately after creating shield and damage snapshots.
- No charge-up stage, three-hex group search, movement, or two-hex storm geometry beyond the generic cast and clump/spread coverage.
- Burn detection is available on abstract targets, but coverage from allies and timing of the charge snapshot can change the AP gained.

Evidence: `tft_engine/src/drivers/fighters.rs`, pinned tooltip/calculations and overrides for `TFT18_Kennen`. Existing coverage: `test_tft.TestDrivers`.

## Master Yi — AD and AP

**Assessment:** confirmed AP accounting inconsistency / partial attack semantics.

Manaless double strike every third attack. AD gains 12% plus 3% per 100 AP additive permanent AS per double strike. AP adds magic damage and heals for 35% of it; AP base autos use the separate 15-AD row.

- AP explicit healing uses unclipped hit_ability result: a controlled 500-magic-damage hit into 1 HP credits 1 damage but 175 healing. Immortal theory probes are unaffected by lethal overkill.
- The second physical hit precedes AP bonus damage, so the magic bonus disappears if that physical hit kills. Double-strike crit/on-hit and retargeting between its two hits need direct runtime evidence.
- Takedown movement-speed burst has no effect because there is no movement simulation.

Evidence: `tft_engine/src/drivers/fighters.rs`, pinned tooltip/calculations and overrides for `TFT18_MasterYi`. Existing coverage: `test_tft_data`, `test_tft.TestDrivers`, `test_tft.TestManaCycle.test_manaless_units_never_cast`.
Controlled native observations: `yi_overkill_heal` in `observations.json`, group `fighters`.

## Gnar — mini and Mega

**Assessment:** confirmed target-anchor discrepancy / partial transformation.

Rage gains 5 per second and per attack up to 50; transform grants 600 HP at two stars, deals physical damage, strips AP-scaled flat armor/MR, and stuns. Mega uses a mana bar and assumed Fighter mana per attack. Finite last-enemy removal works; immortal targets cannot be removed for damage credit.

- Throw hits the primary before selecting other targets. If the primary dies, the next living primary is wrongly excluded: controlled clump throw hits 0 and 2, skipping 1.
- Largest-group transform, two-hex splash, farthest-target landing and throw path are not represented by the clump/spread selector.
- Transformation is dispatched by rage tick/attack and bypasses ordinary cast counting and on-cast hooks. Whether these should trigger in game is unresolved.
- ADPerRage and TransformRangeReduction rows are not applied; their active semantics are not established by the pinned tooltip.

Evidence: `tft_engine/src/drivers/fighters.rs`, pinned tooltip/calculations and overrides for `TFT18_Gnar`. Existing coverage: `test_tft_execute`, `test_tft_theory_auras`.
Controlled native observations: `gnar_lethal_throw_spread`, `gnar_lethal_throw_clump` in `observations.json`, group `fighters`.

