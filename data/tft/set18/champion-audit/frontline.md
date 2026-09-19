# Frontline champion mechanics audit — Set 18, pinned 18.1d

Audit covers all **20** drivers in `frontline.rs` and `frontline2.rs`. Engine source hash `75b1dc3aed55577eb967d10f3ea9904790ab01f61cbf917be54ef1e01f0b9b45`; saved baseline `515a7a167347acb61148016e4966ffc0be538785e670c05ad441113d752b3a13`; snapshot inputs `1c7e857debea22df4f16936771bd1ea07800f59bdbd3f05a0574434c9e18067c`.

This is a source/code audit with controlled native probes, not live-game verification. No production files were edited, no rebuild was run, and no full item enumeration was performed.

## Highest-confidence findings

- **Fiddlesticks selects the wrong spread recipients.** The source says three nearest enemies. Native code uses geometry-limited `aoe(3,false)`: a controlled single cast hit only target 0 when spread, versus targets 0/1/2 when clumped. Drain damage was 55.263 versus 165.789. His flat MR strip is also local to his own theory probes, so other allied magic damage gains no benefit.
- **Lillia receives unconditional wake-up damage.** Source requires 1,000 damage to awaken a sleeping target. The native primary-target wake-up dealt 150 at 0.25 s after only 100.5 prior damage. Raising the threshold to 1e9 changed nothing. Secondary targets never awaken from damage. This is documented as an assumption, but conflicts directly with the stated condition.
- **Several ally abilities are absent.** Rakan’s decaying ally AS; Shen’s ally empowered attacks; Taric’s paired-ally trigger, area shields and empowered attacks; Sentinel’s Alpha ally mana regeneration; Scuttlecrab’s Alpha threshold healing; and Kruglette death shields are missing or only partially represented. These omissions can materially alter composition value.
- **Amumu’s source calculations conflict.** The exported nested HealthCalc1 scales the flat heal by the HP percentage, while the footer says percent HP plus the full AP heal. The driver follows the footer. Burning stun duration and per-recipient scope still need stronger source/runtime evidence; do not guess a fix.

## Coverage by champion

| Champion | Active formula / state | Audit status | Main remaining issue |
| --- | --- | --- | --- |
| Kobuko | Next attack is replaced by magic damage: 0.10 H + (70/105/150) A at stars 1/2/3. Heal (265/315/460) A + 0.07 H evenly over 2 seconds. | active core matches pinned text | Quarter-second HoT payout instead of a continuous or source-confirmed per-tick schedule. |
| Leona | Magic bash = (1.5/2.7/4.05) × current armor. Opening bonus armor and MR = (60/70/80) A, linearly decaying to zero over 12 seconds. | active core matches pinned text | Passive decay is updated on the quarter-second engine tick. |
| Ornn | Cone magic damage = (200/300/450) A. Self shield = (400/460/550) A for 4 seconds. | active core matches pinned text | Cone orientation, travel and sequential hits are replaced by coarse nearby selection. |
| Rakan | No direct active damage; ordinary basic attacks remain. Self shield = (270/320/415) A for 4 seconds. | known omitted ally mechanic | RKN_ALLY_AS: Source grants decaying ally AS of 90%/100%/130% × A over 4 seconds; driver never reads AttackSpeedCalc1 or AttackSpeedDuration. No ally receives it in standalone, legacy team, symmetric, or theory execution. |
| Rek'Sai | Uproot magic damage = (70/105/160) A to adjacent enemies. Every 1 second heal (10/14/20) + 0.01 H; multiplier 3 for 3 seconds after casting. | active core matches pinned text | Adjacency and lunge movement are coarse target selection; no movement path. |
| Alistar | Current-target magic slam = (100/150/225) A and 1.5-second stun. Self heal = (200/260/320) A + 0.08 H; two ally heals each (80/105/130) A. | known omitted cleanse and theory ally credit | ALI_CLEANSE: Pinned ability explicitly cleanses disables; the driver has no cleanse operation. Its comment that there is nothing to remove does not describe current enemy-control scenarios. |
| Elise | After first transform, every normal attack adds (35/50/80) A magic damage. First cast adds 375/475/725 max HP and fills that amount; transformed attacks heal (55/90/170) A. | documented attack speed approximation | Decaying AS is deliberately replaced with its arithmetic average, so burst timing, attack count boundaries, proc stacking and recast timing can differ. |
| Scuttlecrab | Dance replaces basic damage and deals 100% current AD to adjacent targets. Heal (325/400/675) A; 30% at landing and 70% over 3 seconds, with 15% durability during burrow. | known alpha omission with burrow regressions | SCT_GREEN: Green Buff source restores 15% max HP to allies falling below 50%, with TraitHealDuration=3; no Green Buff health trigger is installed or read by this driver. |
| Sejuani | Cone (90/135/205) A plus line (120/180/270) A magic damage. Self shield = (225/250/275) A + 0.10 H for 4 seconds. | active core with geometry abstraction | Different cone/line shapes and sequential strike timing are collapsed to the same nearby set and timestamp. |
| Shen | Next 3 self attacks add (35/55/80) A magic damage. Self shield (325/400/500) A; one ally shield (200/275/375) A, both 4 seconds. | known omitted ally attack buff | SHN_ALLY_ATTACKS: The source gives the ally the same three-attack AS and bonus-damage buff. The driver only buffs its own attack counter/as_extra and sends a shield request. |
| Fiddlesticks | Drain (70/105/170) A magic damage to each selected target over 2 seconds after subtracting 10 flat MR. Self heal (395/470/790) A over the same 2 seconds; not multiplied by target count. | confirmed targeting discrepancy | FID_NEAREST: One 2-star cast against five living probes drained only [0] in spread for 55.2631578947 damage versus [0,1,2] in clump for 165.789473684. Source NumTargets=3 and explicitly says nearest, not adjacent. |
| Hecarim | Riders deal (80/120/195) A magic damage to 3 nearest living enemies. Gain 50 armor and MR for 3 seconds; heal (375/475/685) A over the window. | active core with targeting regressions | Nearest means stable ordered slots, not actual distances. |
| Krug | Physical roll = (135/200/335) × current AD / same-star base AD + 0.08 H. Every cast grants (175/225/325) A maximum and current HP. On death, two bodies each have (50/100/180) + 0.12 H with Kruglette snapshot armor/MR. | known body and alpha abstractions | KRG_SLATE: Alpha Slate Buff says Krug and Kruglettes shield allies on death. The driver reports one shield request at Krug’s death (8% of Krug max HP for row duration 5), with no Kruglette death shield hooks. |
| Vi | Ordinary basic attacks; no direct active damage. Every attack heals 0.02 H. Cast heals (225/300/400) A; grants 85%/100%/125% AS and 15% durability for 3 seconds. | active core with control regression | Common temporary AS slot refreshes instead of stacking. |
| Amumu | Passive each second deals (12/18/150) A to adjacent targets; active deals (100/150/2000) A to nearby AoE. Driver heals (20/25/100) A + (2.5%/2.5%/4%) H per second, following the footer’s sum. | source calculation conflict and unverified recipient scope | AMU_CALC_FOOTER: Normalized HealthCalc1 multiplies both HealthCalc2 and max HP by PassiveHealPercent; raw footer instead says percentage max HP plus the full HealthCalc2. Driver follows the footer, so it intentionally does not call HealthCalc1. |
| Lillia | Initial butterfly magic damage (90/135/2000) A; source conditional extra damage is 10% target max HP after 1,000 damage wakes a sleeping target. Self heal = (300/400/800) A. | confirmed conditional overcredit | LIL_WAKE: At star 2, primary wake-up dealt 150 after mitigation at 0.25 seconds after only 100.5 damage, below the source’s 1,000 threshold. Raising DamageNeededToAwaken to 1e9 did not change the trace. |
| Malphite | On tracked shield absorption break: magic damage (40/60/2000) A + (0.30/0.45/9.99) × (current armor + current MR). Self shield = (700/850/2000) A for 4 seconds; no resistance scaling in the current shield calc. | active shield break with unverified state | Petrified state is entirely omitted because no numerical behavior was established. |
| Sentinel | Fissure magic damage = (100/150/3500) A; knockup 1/1.25/8 seconds and Mana Reave +10 next-cast cost. Self shield = (400/500/2000) A + 0.10 H for 4 seconds. | known alpha omission with reave regressions | SNT_BLUE: Source Alpha Blue Buff grants allies +2 Mana Regen each time Sentinel casts. No recipient-aware event implements it; the stale +5 opening-self-regen row was previously removed. |
| Maokai | Each blocked-damage threshold emits one current-target sapling for 7%/7%/75% H magic damage; active deals 6%/6%/100% H; death emits 3/5/10 saplings. Active heals (330/400/2000) A + 10%/10%/100% missing HP. | known persistent trait omission | MAO_OLD_GROWTH: Old Growth’s persistent health state is not represented by the current board input/resolver. Driver itself only handles combat saplings and heal. |
| Taric | Next 2 self attacks add (100/150/1000) A magic damage. Active heal (200/300/3000) A. Passive self shield is (175/350/10000) + (10%/10%/100%) H, once below 50% self HP, for 3/3/99 seconds; one equal ally shield request is counted. | known paired ally and area omissions | TAR_PAIR_TRIGGER: No paired ally state exists in the driver; paired-ally threshold and both area emission centers are absent. |

## Per-champion observations and gaps

### Kobuko — `TFT18_Kobuko`

Observed code: `tft_engine/src/drivers/frontline.rs:16`. Bash hits the current attack target; healing is self-only. Cast arms one bash and replaces the current HoT slot. Bash waits for the next attack; heal pays at quarter-second ticks.

- Declared abstraction: Quarter-second HoT payout instead of a continuous or source-confirmed per-tick schedule.
- Runtime unverified: No character-bin cast time was resolved; the common cast-time fallback is used.
- Runtime unverified: Basic-attack replacement is flagged as ability damage; ordinary attack crit versus Precision-only crit needs runtime classification evidence.
- Runtime unverified: Recast behavior while a prior HoT or bash remains active is not established.
- Focused checks: No focused ability-mechanic regression identified; fixture/trait mentions and generic goldens do not establish coverage.
- Test gaps: No focused source-based bash damage/crit, heal amount, or recast regression.

### Leona — `TFT18_Leona`

Observed code: `tft_engine/src/drivers/frontline.rs:63`. Current target, with 1.5-second stun. Passive applied during native initialization; decay recomputed each driver tick; active lands after 0.25-second cast.

- Declared abstraction: Passive decay is updated on the quarter-second engine tick.
- Runtime unverified: Whether later AP changes should rescale the opening passive; code snapshots the opening bonus.
- Runtime unverified: Live stun/attack windup and simultaneous-event ordering are not verified.
- Focused checks: test_tft_tanks.TestOpeningEhp.test_opening_includes_defensive_driver_initialization.
- Test gaps: No independent decay-at-boundary or armor-scaled bash regression.

### Ornn — `TFT18_Ornn`

Observed code: `tft_engine/src/drivers/frontline.rs:110`. Geometry-based nearby cone: nearby clump or primary when spread. Shield and all cone hits occur together at the 0.25-second ability landing.

- Declared abstraction: Cone orientation, travel and sequential hits are replaced by coarse nearby selection.
- Declared abstraction: Forge Power and Artifact Anvil rewards are economy across rounds and do not enter a single-fight score.
- Runtime unverified: Shield refresh/stacking on overlapping recasts; precise cone targeting and interruption behavior.
- Focused checks: No focused ability-mechanic regression identified; fixture/trait mentions and generic goldens do not establish coverage.
- Test gaps: No focused shield amount/expiry or cone-recipient test.

### Rakan — `TFT18_Rakan`

Observed code: `tft_engine/src/drivers/frontline.rs:138`. Only self shield implemented. Source additionally selects the ally with highest damage dealt this combat. Shield lands at 0.25 seconds. Source ally AS duration is 4 seconds; the buff has no runtime hook.

- **confirmed omission:** Source grants decaying ally AS of 90%/100%/130% × A over 4 seconds; driver never reads AttackSpeedCalc1 or AttackSpeedDuration. No ally receives it in standalone, legacy team, symmetric, or theory execution. Undercredits Rakan support value and the item/trait/recipient interactions of his active.
- Declared abstraction: Recipient support effects are not represented in the production theory actor scheduler.
- Runtime unverified: Tie-breaking for highest-damage ally, self eligibility, exact decay curve and recast refresh/stacking.
- Focused checks: No focused ability-mechanic regression identified; fixture/trait mentions and generic goldens do not establish coverage.
- Test gaps: No highest-damage-recipient, AS decay, or ally throughput test.

### Rek'Sai — `TFT18_RekSai`

Observed code: `tft_engine/src/drivers/frontline.rs:161`. Adjacency follows clump/primary geometry; source stun lasts 1 second. Regen starts at t=1; cast lands at 0.25 seconds and sets boost until land+3; endpoint uses strict time<expiry.

- Declared abstraction: Adjacency and lunge movement are coarse target selection; no movement path.
- Runtime unverified: Exact boost-boundary inclusion and any untargetability during the lunge.
- Focused checks: test_tft_tanks.TestTankFormation.test_local_aoe_and_adjacency_leave_protected_backliners_in_range_to_fire.
- Test gaps: No independent per-second regen/boost expiry regression.

### Alistar — `TFT18_Alistar`

Observed code: `tft_engine/src/drivers/frontline.rs:215`. Source chooses the two lowest-percent-HP allies; native team/symmetric modes have heal recipient plumbing, whereas theory records uncredited ally potential. Self heal, ally-heal request, slam and stun occur at the 0.25-second landing.

- **confirmed omission:** Pinned ability explicitly cleanses disables; the driver has no cleanse operation. Its comment that there is nothing to remove does not describe current enemy-control scenarios. The disabled-state interaction is absent; the exact expected cast/cleanse trigger still requires runtime evidence.
- Declared abstraction: Ally healing is potential-only in theory and does not buy allied EHP.
- Runtime unverified: Whether Alistar may cast or complete the cleanse while already disabled; shared engine blocks casts during stun.
- Runtime unverified: Exact recipient eligibility/self exclusion and tie-breaking in live play.
- Focused checks: test_tft_team_engine.TestSharedHealingAndShields.test_alistar_preserves_per_recipient_healing_amount; test_tft_symmetric.TestSymmetricAbilities.test_real_enemy_heal_repairs_damage_in_shared_pool; test_tft_symmetric.TestSymmetricAbilities.test_wound_reduces_real_enemy_healing; test_tft_unit_profiles.TestNativeResponse.test_ally_output_is_reported_as_potential_without_crediting_self_sustain.
- Test gaps: No cleanse/control-state regression.

### Elise — `TFT18_Elise`

Observed code: `tft_engine/src/drivers/frontline.rs:249`. Self transform and current-target attacks. Pinned human/spider base stats are identical; no extra AD or resist transform was proven missing. Subsequent casts use a flat +87.5% AS for 4 seconds instead of +175% decaying AS.

- Declared abstraction: Decaying AS is deliberately replaced with its arithmetic average, so burst timing, attack count boundaries, proc stacking and recast timing can differ.
- Runtime unverified: Exact AS decay function and buff refresh policy.
- Runtime unverified: Max-HP grant filling current health and interrupted-transform timing require runtime confirmation.
- Focused checks: No focused ability-mechanic regression identified; fixture/trait mentions and generic goldens do not establish coverage.
- Test gaps: No independent first/subsequent cast, transformed on-hit/heal, or decay regression.

### Scuttlecrab — `TFT18_Scuttlecrab`

Observed code: `tft_engine/src/drivers/frontline.rs:295`. Adjacent geometry; ability/on-hit application is attached to the primary dance target, secondary dance damage uses plain ability deal. 0.25-second animation followed by 3-second no-attack/no-recast burrow; generic mana lock remains separate.

- **confirmed omission:** Green Buff source restores 15% max HP to allies falling below 50%, with TraitHealDuration=3; no Green Buff health trigger is installed or read by this driver. Alpha support healing is absent, not merely reduced by recipient utilization.
- Declared abstraction: Body placement and dancing movement are not modeled.
- Declared abstraction: Coins and other unused legacy rows have no current-combat credit.
- Runtime unverified: Dance basic-attack crit versus ability crit classification and secondary on-hit eligibility.
- Runtime unverified: Green Buff trigger limits, reset rules and self eligibility; do not infer them from the two threshold rows.
- Runtime unverified: Burrow mana-lock duration is intentionally not asserted as live-verified.
- Focused checks: test_tft_scuttlecrab.TestScuttlecrabBurrow.*; test_tft_symmetric.TestSymmetricAbilities.test_scuttle_burrow_prevents_attacks_on_both_sides; test_tft_team_engine.TestSharedHealthAndPressure.test_scuttle_cannot_attack_during_shared_fight_burrow.
- Test gaps: No source-confirmed replacement crit/on-hit or Green Buff recipient tests.

### Sejuani — `TFT18_Sejuani`

Observed code: `tft_engine/src/drivers/frontline.rs:355`. Both cone and line use the same aoe_all selection, giving identical recipients in each geometry. Shield, cone, and line all land at the 0.25-second cast endpoint.

- Declared abstraction: Different cone/line shapes and sequential strike timing are collapsed to the same nearby set and timestamp.
- Runtime unverified: Whether the line can reach a protected backliner, exact direction selection and delay between strikes.
- Focused checks: No focused ability-mechanic regression identified; fixture/trait mentions and generic goldens do not establish coverage.
- Test gaps: No independent shield formula or distinct cone/line target/timing regression.

### Shen — `TFT18_Shen`

Observed code: `tft_engine/src/drivers/frontline.rs:389`. Self attack empowerment and an ally shield request; source buffs both champions’ next 3 attacks. Cast lands at 0.25 seconds, resets 3 charges and +40% self AS; AS expires after consuming the third charge.

- **confirmed omission:** The source gives the ally the same three-attack AS and bonus-damage buff. The driver only buffs its own attack counter/as_extra and sends a shield request. Undercredits offensive support, including a paired carry’s item interactions.
- Declared abstraction: Ally shield is uncredited potential in theory.
- Declared abstraction: Nearby damaged recipient selection is not modeled by theory’s independent actor probes.
- Runtime unverified: Shield and remaining empowered-attack refresh rules on early recast.
- Runtime unverified: Exact attack-speed rescheduling when the cast lands midway through an attack period.
- Focused checks: test_tft_symmetric.TestSymmetricAbilities.test_real_enemy_shield_absorbs_later_attacks.
- Test gaps: No three-charge/AS expiry or allied empowered-attack regression.

### Fiddlesticks — `TFT18_Fiddlesticks`

Observed code: `tft_engine/src/drivers/frontline.rs:436`. Source: 3 nearest living enemies. Driver: aoe(3,false), which returns only the primary in spread and only nearby targets in clump. LANDS_AT_START; cast_time overridden to 2 seconds. MR strip and DoTs/HoT start at cast start; basic attacks pause for the channel.

- **confirmed by native probe:** One 2-star cast against five living probes drained only [0] in spread for 55.2631578947 damage versus [0,1,2] in clump for 165.789473684. Source NumTargets=3 and explicitly says nearest, not adjacent. Undercredits spread/nearest-target drain and MR strip; change selection independently of geometry where three enemies are represented.
- **confirmed theory omission:** Each theory actor owns independent probe MR; only stuns and the declared shared providers are synchronized. Fiddlesticks writes target.mr_flat locally, and theory provider parsing does not export this spell’s flat MR reduction. Other allied magic damage receives no value from his active flat-MR strip.
- Declared abstraction: Drain uses common DoTs/HoT, with quarter-second payment and no spatial channel range check.
- Declared abstraction: Flat MR reduction is permanent and cumulative because source names no duration; actual stacking policy is not verified.
- Runtime unverified: Whether control cancels the channel and its queued DoT/HoT, and whether already queued drain should stop on death.
- Runtime unverified: Exact first drain tick and channel animation versus generic 0.25-second compatibility cast metadata.
- Focused checks: observations.json (groups.frontline):fiddlesticks_spread; observations.json (groups.frontline):fiddlesticks_clump.
- Test gaps: No committed nearest-target, shared flat-MR benefit, or channel interruption regression.

### Hecarim — `TFT18_Hecarim`

Observed code: `tft_engine/src/drivers/frontline2.rs:18`. alive().take(3) is independent of clump/spread, including newly exposed farther targets after a death. 0.25-second cast, 3-second resists/HoT; stun 1.5/1.5/1.75 seconds.

- Declared abstraction: Nearest means stable ordered slots, not actual distances.
- Declared abstraction: Riders land simultaneously without modeled travel time.
- Runtime unverified: Overlapping cast HoTs overwrite one slot while resistance buffs stack as separate entries; source does not settle the live recast behavior.
- Focused checks: test_tft_tanks.TestTankFormation.test_hecarim_stuns_three_fronts_while_backline_attacks_and_casts; test_tft_tanks.TestTankFormation.test_a_dead_frontline_exposes_the_next_nearest_backliner_to_hecarim; test_tft_tanks.TestScheduledTankPressure.test_stunned_scheduled_cast_waits_then_restarts_its_interval; test_tft_theory_pressure.TestTheoryControl.test_three_target_stun_only_covers_the_targets_in_reach.
- Test gaps: No source-confirmed overlapping-cast/resist stacking test.

### Krug — `TFT18_Krug`

Observed code: `tft_engine/src/drivers/frontline2.rs:71`. Roll hits current target; two Kruglettes hold pressure serially after the champion dies. 0.25-second cast; bodies appear synchronously on death.

- **confirmed omission:** Alpha Slate Buff says Krug and Kruglettes shield allies on death. The driver reports one shield request at Krug’s death (8% of Krug max HP for row duration 5), with no Kruglette death shield hooks. Missing later shields and uncertain area/recipient counts; theory gives even the one reported ally shield no EHP credit.
- Declared abstraction: Bodies soak damage but do not attack, despite the Kruglette data containing attack stats.
- Declared abstraction: Body pressure is serial rather than two simultaneous entities.
- Runtime unverified: Exact Slate recipients, whose max HP determines each shield, and repeated cast max-HP stacking rules.
- Runtime unverified: Unused APDamage/HigherHealthMultiplier/StunDuration rows do not establish additional active mechanics absent from tooltip.
- Focused checks: test_tft_tanks.TestEnemyDebuffs.test_wound_does_not_reduce_max_health_grants; test_tft_tanks.TestTankSurvivalCap.test_surviving_death_body_counts_as_capped_and_inherits_resist_debuffs; test_tft_unit_profiles.TestNativeResponse.test_spawned_bodies_keep_their_own_defenses_and_each_discards_overkill; test_tft_team_engine.TestSharedHealthAndPressure.test_a_dead_champion_cannot_finish_cast_while_summon_holds.
- Test gaps: No Kruglette attacks, body-death shield or source-backed Slate recipient regression.

### Vi — `TFT18_Vi`

Observed code: `tft_engine/src/drivers/frontline2.rs:118`. Self-only effects. 0.25-second landing; unstoppable represented by cc_immune_until for the same 3-second window.

- Declared abstraction: Common temporary AS slot refreshes instead of stacking.
- Runtime unverified: Does Unstoppable also cleanse control already applied before landing? Current hook blocks new CC only.
- Runtime unverified: Exact passive heal trigger on an attack that kills its target or misses.
- Focused checks: test_tft_symmetric.TestSymmetricAbilities.test_vi_is_unstoppable_during_her_resolved_ability_duration; test_tft.TestBody.test_heal_caps_at_max_health.
- Test gaps: No hand-computed passive 2% healing, active AS amplitude or existing-control cleanse test.

### Amumu — `TFT18_Amumu`

Observed code: `tft_engine/src/drivers/frontline2.rs:157`. Source distinguishes 1-hex passive and 2-hex active. Coarse nearby flags collapse radii; all active recipients use the current primary target’s pre-hit Burning status. 0.25-second cast; passive starts at 1 second. Base stun 1/1/6; current GenericCalc1 resolves to 2.5/2.5/12.5 when primary is Burning.

- **source data conflict:** Normalized HealthCalc1 multiplies both HealthCalc2 and max HP by PassiveHealPercent; raw footer instead says percentage max HP plus the full HealthCalc2. Driver follows the footer, so it intentionally does not call HealthCalc1. Canonical calculation data and displayed attributes can disagree with the scoring formula; resolve the extraction before simplifying the driver.
- Declared abstraction: Different passive and active radii use the same coarse nearby group.
- Runtime unverified: Whether Burning is checked independently per victim or once on the current target.
- Runtime unverified: GenericCalc2 is represented as StunDuration + 0.5 and GenericCalc1 adds StunDuration again; this needs source expression/runtime verification before changing the 2.5-second result.
- Focused checks: test_tft_data.TestLivePatchAudit.test_amumu_heal_and_yi_ap_resists_reach_engine_inputs; test_tft_tanks.TestTankFormation.test_local_aoe_and_adjacency_leave_protected_backliners_in_range_to_fire.
- Test gaps: No source-backed mixed-Burning recipient test, shared Burning-state test, or independently resolved augmented stun calculation.

### Lillia — `TFT18_Lillia`

Observed code: `tft_engine/src/drivers/frontline2.rs:220`. Up to 4 nearby enemies; current target gets unconditional immediate wake-up damage, other chosen targets receive full sleep. All damage and primary wake-up occur at 0.25-second landing; other sleep lasts 1.5/1.75/8 seconds with no damage-based awakening.

- **confirmed by native probe:** At star 2, primary wake-up dealt 150 after mitigation at 0.25 seconds after only 100.5 damage, below the source’s 1,000 threshold. Raising DamageNeededToAwaken to 1e9 did not change the trace. Overcredits immediate percent-max-HP damage and undercredits primary sleep; other victims can be overcredited full-duration control when allies would wake them.
- Declared abstraction: The driver explicitly documents assuming the primary wakes immediately and all other targets never wake. This is intentional but contradicts the stated threshold.
- Declared abstraction: Sleep is modeled as a plain stun, without accumulated incoming damage or wake events.
- Runtime unverified: Whether the initial hit counts toward the threshold, and exact simultaneous-hit/awaken ordering.
- Runtime unverified: Live nearby-target selection and linked-team damage attribution.
- Focused checks: observations.json (groups.frontline):lillia_wakeup.
- Test gaps: No committed damage-threshold, secondary wake-up, or sleep interruption regression.

### Malphite — `TFT18_Malphite`

Observed code: `tft_engine/src/drivers/frontline2.rs:261`. Wave hits coarse nearby AoE; source radius is 2/2/3 hexes. 0.25-second landing; reactive hit callback checks full absorption, not normal expiry.

- Declared abstraction: Petrified state is entirely omitted because no numerical behavior was established.
- Declared abstraction: Only the latest own shield is tracked; overlapping recasts can leave an older shield’s later break unobserved.
- Runtime unverified: Whether petrification prevents attacks/casts or changes defenses/targetability, and whether expiry triggers any live effect.
- Runtime unverified: Whether own shields refresh, replace or stack, which determines whether tracking only the last is correct.
- Runtime unverified: Unused ShieldRatioArmorMR/SpellDamagePercentOfShield rows do not justify adding those terms absent from the active calculations.
- Focused checks: test_tft_symmetric_regressions.TestDeferredDamageOrdering.test_shield_break_damage_lands_before_its_own_on_hit_shred.
- Test gaps: No petrification, multiple own-shield, or expiration-versus-absorption source regression.

### Sentinel — `TFT18_Sentinel`

Observed code: `tft_engine/src/drivers/frontline2.rs:299`. Nearby geometry stands in for the line directed toward most enemies; surviving hit targets receive reave. 0.25-second landing; shield, fissure, stun and reave execute at that timestamp.

- **confirmed omission:** Source Alpha Blue Buff grants allies +2 Mana Regen each time Sentinel casts. No recipient-aware event implements it; the stale +5 opening-self-regen row was previously removed. Undercredits repeated Alpha support casts and ally mana throughput.
- Declared abstraction: Fissure direction/path and travel are not modeled.
- Declared abstraction: Generic incoming pressure channels have no mana bar, so reave has no theory pressure benefit.
- Runtime unverified: Repeated reave stacking uses strongest pending increase; live stacking semantics remain unverified.
- Runtime unverified: Alpha ally inclusion, permanence and stacking duration require runtime evidence.
- Focused checks: test_tft_mana_reave.*; test_tft_symmetric.TestSymmetricAbilities.test_sentinel_reaves_actual_enemy_mana_and_delays_first_cast; test_tft_trait_timing.TestNativeTraitTiming.test_sentinel_alpha_has_no_unsupported_opening_self_regeneration.
- Test gaps: No Alpha allied mana regeneration or true line targeting test.

### Maokai — `TFT18_Maokai`

Observed code: `tft_engine/src/drivers/frontline2.rs:339`. Source saplings jump toward a nearby enemy; all saplings are sent directly to the current target without travel or scatter. Blocked thresholds are 650/650/300; active lands after 0.25 seconds; death saplings fire synchronously.

- **confirmed unmodeled state:** Old Growth’s persistent health state is not represented by the current board input/resolver. Driver itself only handles combat saplings and heal. Boards cannot include accumulated unique-trait health from prior eligible events.
- Declared abstraction: Blocked-damage counter is raw incoming minus post-resist/durability damage; shield absorption is excluded.
- Declared abstraction: Saplings are instantaneous direct hits rather than spawned attackers or traveling projectiles.
- Runtime unverified: Whether live blocked damage includes shields, cancelled hits or only mitigation; do not guess from the wording alone.
- Runtime unverified: Unused 3StarBigSaplingsOnCast/BigSaplingAOEDamage rows are not implemented; three-star five-costs are outside the dashboard’s legal stars and rows alone do not prove live triggers.
- Focused checks: No focused ability-mechanic regression identified; fixture/trait mentions and generic goldens do not establish coverage.
- Test gaps: No focused mitigation-counter, shield interaction, sapling recipient, missing-HP heal or death-proc test.

### Taric — `TFT18_Taric`

Observed code: `tft_engine/src/drivers/frontline2.rs:388`. Source triggers when Taric OR the paired ally first drops below 50%, with energy from both shielding allies within 3 hexes. Driver only observes self and requests one ally shield. Passive tested after incoming damage, before final death; active 0.25-second cast resets 2 self attack charges.

- **confirmed omission:** No paired ally state exists in the driver; paired-ally threshold and both area emission centers are absent. Missing team shielding and possible earlier activation.
- **confirmed omission:** Source gives both Taric and the paired ally bonus magic damage on their next 2 attacks; only Taric has charges. Undercredits the pair’s offense and item/crit interactions.
- Declared abstraction: One ally shield is potential-only in theory, not actual area recipient utilization.
- Runtime unverified: Pair selection, area overlap/duplicate-shield policy, self inclusion and lethal-hit threshold ordering.
- Runtime unverified: Unused 3StarPercentHPDamage is not interpreted as an active effect without stronger evidence; three-star five-costs are not in current dashboard search.
- Focused checks: No focused ability-mechanic regression identified; fixture/trait mentions and generic goldens do not establish coverage.
- Test gaps: No paired-ally trigger, area recipient, charge expiry or shield-threshold regression.

## Evidence and verification

- Per-champion descriptions, source paths, file SHA256 values, findings and test references are in [findings.json](findings.json), under `groups.frontline`. Exact rows and calculation terms remain in the referenced, hash-pinned snapshot. Controlled observations are in [observations.json](observations.json), under `groups.frontline`.
- Existing targeted suites passed **54 tests**: `test_tft_tanks`, `test_tft_scuttlecrab`, `test_tft_mana_reave`. Their success does not cover every formula or the newly identified Fiddlesticks/Lillia conditions. No golden replay was used to infer correctness.
- Source numbers come from `data/tft/set18/18.1d/metatft.json` plus its audited overrides. Riot’s [18.1 patch page](https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/teamfight-tactics-patch-18-1/) and archived Riot-derived compatibility bins for [Fiddlesticks](https://raw.communitydragon.org/16.17/game/characters/da_fiddlesticks18.cdtb.bin.json), [Lillia](https://raw.communitydragon.org/16.17/game/characters/da_18_lillia.cdtb.bin.json), [Amumu](https://raw.communitydragon.org/16.17/game/characters/da_amumu18.cdtb.bin.json) and [Malphite](https://raw.communitydragon.org/16.17/game/characters/da_18_malphite.cdtb.bin.json) were inspected. These compatibility bins expose timings and generic spell placeholders; they do not reveal the complete live ability scripts.
- Source dates and patch systems differ: lookup metadata was generated in the Set 18/PBE period and corrected to saved 18.1d; the bins are the archived 16.17 client export. Current URLs are not treated as proof of the pinned state. No installed game data was supplied to this subtask.
- Full paired-ally state, exact movement/cones/lines, source-death channel cancellation, shield stacking/refresh, empowered-attack crit flags and unused higher-star rows remain unresolved. Shared engine/trait issues are also tracked by the root audit.
