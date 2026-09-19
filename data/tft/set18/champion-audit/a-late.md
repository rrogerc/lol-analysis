# Full champion mechanics audit: a.rs and late.rs

All **19 champions / 22 forms** in these files were inspected. Status: completed with open findings. Engine `75b1dc3aed55577eb967d10f3ea9904790ab01f61cbf917be54ef1e01f0b9b45`. No production edits or rebuild.

This is an audit of the pinned 18.1d model, not live-game verification. The lookup is PBE-derived with audited corrections; the stored bin summary lacks complete spell scripts. Arithmetic tests establish consistency under the chosen model, while hit timing, proc flags, recast behavior and geometry often remain assumptions.

**Verification:** 41 focused existing tests passed; controlled probes of AoE, Yorick's scaling, Veigar's kills and Sett's timing completed. No full item enumeration. The consolidated [findings.json](findings.json) retains evidence, source locators, forms and test references. Exact calculation terms, coefficients and rows remain in the source snapshot pinned by hash.

## Priority findings

- Independent multi-target selection for Teemo, Kog'Maw and Alune is incorrectly reduced by spread geometry.
- Kayle's wave is skipped when the preceding basic auto kills its primary target; runtime source confirmation is needed.
- Yorick's strike AP calculation conflicts with the AD tooltip icon.
- Ivern's targeting around allies, ally buffs and unresolved shield damage-amp scaling remain unmodeled or uncertain.
- Rammus's overlapping resistance buffs and tracking of only the newest shield require dedicated recast evidence and tests.
- Sett's heal lands immediately, while the punch is delayed; the Sett and Aphelios kit notes are stale.
- Veigar's prior-combat AP history is absent, and granting AP for every kill he makes goes beyond the confirmed text.
- Akali's instantaneous kill chains with compounded reductions, Alune's phase cycle, Azir's mana lock through all commands, and summon proc and timing policies remain assumptions.

## Per-champion audit

### Ahri (TFT18_Ahri)

Source: `tft_engine/src/drivers/a.rs:19`. Forms: base. No live-runtime certification.

- **damage_scaling — source_backed_implementation:** MagicDamageCalc1 uses the AP-scaled OrbDamage row; secondary hits are multiplied by 0.8. Evidence: AP coefficients: 425/640/3500; HexPercentDamageFalloffTooltip = 0.2.
- **attack_cast_mana_timing — deliberate_approximation:** ChannelTime overrides generic cast time; damage lands when the channel finishes. No missile travel or aim time. Evidence: Driver a.rs:33; bins only expose a default castTime. Needed: Observe cast/channel/impact sequence and interruption handling.
- **target_selection — deliberate_approximation:** In clump, the first nearby target receives full damage and every other nearby target receives one-hex falloff. In spread, only the primary target is hit. Evidence: The tooltip asks for the densest center within 4 hexes and damage within 3 hexes; the kit explicitly assumes a one-hex clump.
- **buffs_summons_sustain_cc — not_applicable:** No intrinsic summon, heal, shield or CC is specified in this pinned ability. Evidence: Spirit Bomb ability text.
- **item_trait_interactions — unverified_runtime:** Ability crit/procs and Spellweaver per-cast AP use common helpers; dynamic travel/area membership is absent. Evidence: hit_ability per recipient; shared Fx/cast hooks. Needed: Validate per-target proc rules and actual projectile/area timing.

Specific existing tests: none located beyond generic/golden coverage and the audit probes.

### Ashe (TFT18_Ashe)

Source: `tft_engine/src/drivers/a.rs:51`. Forms: base. No live-runtime certification.

- **damage_scaling — verified_arithmetic:** Arrow scales as row damage at star-specific base AD. Trail combines AD+AP damage/second and separately modeled max-HP physical damage. Evidence: TestCalcs.test_ashe_arrow_is_the_damage_at_base_ad and test_star_scaling_of_calcs. Native driver multiplies trail per-second calc by duration.
- **attack_cast_mana_timing — deliberate_approximation:** Generic cast timing; trail is scheduled as DoT on the initially selected enemies. Evidence: a.rs:69-86; default engine cast and tick hooks. Needed: Projectile speed, time crossing each enemy, and entering/leaving the trail.
- **target_selection — deliberate_approximation:** The line hits all clump targets or one spread target. Linear per-enemy falloff with a floor is implemented. At the modeled 1–2 stars, every target after the first receives the 20% floor. Evidence: DamageFalloffPerEnemy = 0.8 and MinDamagePercent = 0.2; tooltip says line through most enemies.
- **crowd_control — not_modeled:** The trail does not apply its 20% attack-speed Slow. Evidence: Tooltip and ChillPercent = 20; driver never reads this row.
- **item_trait_interactions — unverified_runtime:** Arrow and flat trail use ability classification, while separately scheduled percent-HP trail is marked non-ability. Evidence: dot_ability versus dot(...,false) in a.rs:82-84. Needed: Check whether percent-HP trail should share ability crit/item-proc eligibility.

Specific existing tests: `test_tft.py:107 TestCalcs.test_ashe_arrow_is_the_damage_at_base_ad`; `test_tft.py:140 TestCalcs.test_star_scaling_of_calcs`; `test_tft.py:323 TestManaCycle.test_cast_times`.

### Akali (TFT18_Akali)

Source: `tft_engine/src/drivers/a.rs:92`. Forms: AD, AP. No live-runtime certification.

- **damage_scaling — verified_arithmetic:** The AD base hit combines AD-scaled physical and AP-scaled flat portions, with a separate AD-scaled bonus if the target was already burning. AP uses magic AP scaling and a 1.5 tank multiplier. Evidence: TestCalcs.test_akali_mixes_ad_and_flat_ap; AP TankDamageMultiplierAP = 1.5.
- **forms — source_backed_implementation:** Both AD/AP kits dispatch from equipped form; both remain melee/current-target attacks. Evidence: a.rs:139-145; existing adaptor form tests.
- **target_selection — source_backed_implementation:** No intrinsic AoE multiplier. AP jumps to the next current target only after a kill. Evidence: Both tooltips describe current-target volleys; source loop chooses f.target().
- **attack_cast_mana_timing — unverified_runtime:** AP repeats up to 4 hits at the same timestamp, with a 0.7 multiplier compounded after each kill. These calls do not enter Fight.cast again. Evidence: a.rs:113-126; RecastDamageReduction = 0.7, ManaRefund row present. Needed: Recast delay, chain limit, whether effectiveness compounds or stays at 70%, and whether recasts trigger cast-based items/traits.
- **buffs_stacks_item_trait_interactions — unverified_runtime:** AD burning eligibility is sampled before its main hit; AP tank flag and crit/procs come from generic helpers. Evidence: a.rs:106-110 and 120-125. Needed: Confirm burning-bonus timing and passive/on-hit eligibility of each volley/recast.

Specific existing tests: `test_tft.py:123 TestCalcs.test_akali_mixes_ad_and_flat_ap`; `test_tft.py:485 TestRoles.test_adaptor_forms`.

### Alune (TFT18_Alune)

Source: `tft_engine/src/drivers/a.rs:152`. Forms: base. No live-runtime certification.

- **damage_scaling — source_backed_implementation:** A normal cast delivers all 9 AP-scaled shards. Full moon divides one AP-scaled total across all surviving enemies. Evidence: NumMoonshards = 9; MoonshardDamage = 50/75/500; MoonDamage = 2350/3600/7500.
- **target_selection — source_discrepancy:** Spread gives all 9 shards to one target instead of splitting them among the 3 nearest; clump divides them among 3. Full-moon global split is present. Evidence: NumEnemies = 3 and the tooltip specifies the nearest 3; a.rs:181 uses local aoe(3). The native probe deals 675 total in both cases, but to 1 versus 3 recipients.
- **attack_cast_mana_timing — unverified_runtime:** Generic 0.25 s fallback; all shards resolve immediately in a loop. Every fourth cast is hardcoded full moon, starting from ordinary phase. Evidence: a.rs:169-178; kit note explicitly assumes four-cast phase cycle. Needed: Actual phase sequence/start/reset behavior, shard flight cadence and full-moon delay.
- **buffs_stacks_item_trait_interactions — not_modeled:** Attuned team durability/damage-amp phases are not applied by this driver/scorer. Evidence: Attuned source Durability = 0.93 and DamageAmp = 0.07; kit note discloses omission.
- **sustain_summons_cc — not_applicable:** No intrinsic summon/heal/shield/CC described by the ability; Attuned durability is the separate omitted trait effect. Evidence: Moonfall/Attuned descriptions.

Specific existing tests: none located beyond generic/golden coverage and the audit probes.

### Aphelios (TFT18_Aphelios)

Source: `tft_engine/src/drivers/a.rs:200`. Forms: base. No live-runtime certification.

- **damage_scaling — source_backed_implementation:** Swipes use AD scaling; final blast combines AD and AP scaling and is split by recipient count. Evidence: PhysicalDamageCalc1/2 rows; the native probe deals 735 blast damage in both spread and clump.
- **attack_cast_mana_timing — verified_arithmetic:** The two-second channel delivers swipes across ticks and the blast at the end. The adopted mana lock lasts for the channel plus 1 s. Evidence: Two dedicated TestAuditFixes channel/onslaught regressions passed.
- **buffs_stacks_item_trait_interactions — unverified_runtime:** Swipe count adds floor(bonusAS/0.2) to a base of 5, using capped effective AS; every third swipe adds per-attack stacks/on-hit effects without another damage hit or mana. Evidence: Runtime part of GenericCalc1 unresolved; NumSwipesTriggerSimulatedAutos = 3; fight.rs:2100 simulated_attack. Needed: Exact swipe count rounding/stat input beyond AS cap, hit schedule, and simulated-auto proc rules.
- **target_selection — deliberate_approximation:** Swipes retarget current living enemy; blast uses local all-clump/one-spread coverage. Total split is conserved. Evidence: Two-hex-radius blast in tooltip; a.rs:242-253.
- **documentation — source_discrepancy:** Kit note says swipes land at the cast, but current driver/test correctly spread them across the channel. Evidence: kits.json Aphelios note versus TestAuditFixes.test_aphelios_onslaught_takes_its_two_seconds.

Specific existing tests: `test_tft.py:864 TestAuditFixes.test_a_channel_locks_through_itself_and_the_second_after`; `test_tft.py:871 TestAuditFixes.test_aphelios_onslaught_takes_its_two_seconds`.

### Caitlyn (TFT18_Caitlyn)

Source: `tft_engine/src/drivers/a.rs:295`. Forms: base. No live-runtime certification.

- **damage_scaling — source_backed_implementation:** Headshot combines AD+AP physical damage; normal auto is replaced on every third attack. Evidence: PhysicalDamageCalc1; AttacksBeforeHeadshot = 2; TestDrivers.test_caitlyn_headshots_every_third_attack.
- **attack_cast_mana_timing — source_backed_implementation:** Mana bar disabled; no real casts. Counter persists for the fight. Evidence: a.rs:308-323; tooltip passive every third attack; manaless regression.
- **target_selection_sustain_cc — not_applicable:** Single-target headshot; no intrinsic AoE/summon/heal/shield/CC specified. Evidence: Headshot tooltip.
- **item_trait_interactions — unverified_runtime:** Headshot is an ability hit, so requires Precision to crit and uses ability rather than basic on-hit handling. Evidence: a.rs:317; kits.json explicitly adopts this classification. Needed: Live empowered-attack crit and item-proc flags; whether replacement attacks inherit any ordinary on-hit behavior.
- **unused_rows — source_backed_implementation:** Unused ramping calc/Stack term is not treated as an extra headshot mechanic: RampingPercent is zero and tooltip has no ramp. Evidence: Caitlyn curve RampingPercent = 0; PhysicalDamageCalc2/3.

Specific existing tests: `test_tft.py:441 TestDrivers.test_caitlyn_headshots_every_third_attack`.

### Kayle (TFT18_Kayle)

Source: `tft_engine/src/drivers/a.rs:327`. Forms: base. No live-runtime certification.

- **damage_scaling — source_backed_implementation:** Star-specific bonus magic and wave calcs are AP-scaled; no mana. Two-star attacks add timed native Shred. Evidence: MagicDamageCalc1/2; TestDrivers wave and manaless tests; native Shred redundancy regression.
- **target_selection — deliberate_approximation:** Three-star waves hit every other clump enemy and no spread enemies; no wave line/width modeled. Evidence: a.rs:368-372; source says other units hit by waves.
- **lethal_primary_attack — source_discrepancy:** If the base auto kills its primary, an early return suppresses all subsequent passive/wave damage, including surviving secondary targets. Evidence: a.rs:348-350; The controlled finite probe confirms that 40+40 wave damage becomes 0 when only the primary target HP changes to 1. Needed: Verify live emission/impact ordering; source does not state this primary-survival condition.
- **item_trait_interactions — unverified_runtime:** Bonus/wave hits use ability crit/proc rules. Native Shred is added after current primary bonus damage. Evidence: a.rs:352-365; generic hit_ability. Needed: Confirm empowered-hit crit flags and same-attack Shred application order.
- **supported_scope — deliberate_approximation:** Only the supported 1–3 stars are evaluated; four-star infinite range and larger third wave are not implemented. Evidence: Tooltip fourth ascension exists; unit_stars excludes 4.

Specific existing tests: `test_tft.py:435 TestDrivers.test_kayle_waves_need_company`; `test_tft_target_debuffs.py:164 test_kayles_native_shred_is_redundant_and_does_not_extend_stronger_shred`.

### Yorick (TFT18_Yorick)

Source: `tft_engine/src/drivers/a.rs:382`. Forms: base. No live-runtime certification.

- **damage_scaling — source_conflict:** Driver uses AP for the physical strike because normalized PhysicalDamageCalc1 says AbilityPower, while tooltip shows an AD icon. Evidence: The probe deals 225 at baseline, 225 with doubled AD and 450 with doubled AP. Pinned calc conflicts with tooltip icon. Needed: Resolve strike scaling from a better raw spell calculation/runtime; do not silently choose an interpretation.
- **healing — source_backed_implementation:** Active applies AP-scaled heal capped to actual missing HP, then strikes. Evidence: HealthCalc1 uses StrikeHeal values of 280/325/435; generic heal helper.
- **summons — deliberate_approximation:** On death, add one body with flat ghoul HP plus 20% of current maxHP, multiplied by Summoner healthMult. It uses the summon-specific resists. Body soaks damage but never attacks. Evidence: a.rs:393-407; HealthCalc2 and kits note. Body lifecycle regressions passed. Needed: Spirit attack cadence/damage/targeting and true taunt behavior in a real board.
- **attack_cast_mana_timing — unverified_runtime:** Generic cast and tank mana rules; pending cast cannot finish after champion death. Evidence: TestSharedHealthAndPressure death/cast and symmetric body tests. Needed: Spell impact timing and Spirit spawn/taunt timing beyond current event model.
- **item_trait_interactions — source_backed_implementation:** Summoner specifically grants Yorick summon health; it does not promise the damage/extra-attack bonuses assigned to other Summoners. Evidence: Pinned Summoner per-champion wording. Items stay on champion; body only receives explicitly assigned HP/resists.

Specific existing tests: `test_tft_symmetric.py:247 test_body_holds_after_champion_dies_without_casting`; `test_tft_team_engine.py:137 test_a_dead_champion_cannot_finish_cast_while_summon_holds`; `test_tft_nine_units.py:48 test_ninth_on_death_body_holds_after_every_champion_dies`.

### Rammus (TFT18_Rammus)

Source: `tft_engine/src/drivers/a.rs:416`. Forms: base. No live-runtime certification.

- **damage_scaling — source_backed_implementation:** Break burst uses current Armor+MR times star ratio, including active buffs; shield uses AP scaling. Evidence: ArmorMRDamageRatio = 0.5/0.75/1.2; Shield = 350/450/550. No dedicated Rammus rule test located.
- **healing_shields_buffs — unverified_runtime:** At 2 stars, each cast adds a 450 shield and +60 Armor/MR for 4 s. Overlapping casts add resistance buffs rather than refreshing one instance. Evidence: a.rs:432-437 and Fight.buff_resists pushes another timed entry. Needed: Confirm same-ability recast refresh/stack semantics before certifying defensive budget.
- **shield_break_trigger — unverified_runtime:** Only the newest shield index is tracked. Fully absorbed shield triggers burst; expiration does not. Older overlapping shields can break without a watched callback. Evidence: a.rs:416,435,440-447; helpers.rs shield_broke. Needed: Confirm overlap is possible/intended, whether every shield can trigger, and expiration versus break semantics.
- **target_selection_cc — deliberate_approximation:** Taunt is not represented by a targeting switch in this driver. Burst uses clump/one-spread instead of the actual 2-hex radius. Evidence: Tooltip taunt and DamageHexRange = 2; kit assumes isolated enemies already attack Rammus.
- **attack_cast_mana_item_interactions — unverified_runtime:** Tank incoming-damage mana can accelerate recasts while shields remain; this interacts with additive buffs and single tracked index. Evidence: Common tank mana and shield code; no targeted overlap regression found. Needed: Recast schedule and defensive item interactions under actual incoming attack/cast cadence.

Specific existing tests: none located beyond generic/golden coverage and the audit probes.

### Sett (TFT18_Sett)

Source: `tft_engine/src/drivers/a.rs:456`. Forms: base. No live-runtime certification.

- **damage_scaling — source_backed_implementation:** Heal=(BaseHeal+0.12maxHP)*AP/100; punch=AD-scaled ConeDamage+0.06maxHP. Evidence: HealthCalc1 chains HealthCalc2/3; PhysicalDamageCalc1; The probe records a 534.2 heal and a 354.6 punch at the 2-star baseline.
- **passive_mana — source_backed_implementation:** The first drop below 40% HP grants ManaCalc1 directly, independent of the normal mana lock. The modeled 1–2 stars use a value of 100. Evidence: ManaHPThreshold = 0.4; ManaGain = 100 at 1–2 stars; a.rs:476-479.
- **healing_timing — deliberate_approximation:** Entire heal lands immediately when wind-up begins; the punch is scheduled 0.5 s later. Evidence: The native probe records the cast and 534.2 heal at 0.25 s, then the punch at 0.75 s. Tooltip says rapidly healing before punch; HealDuration = 0.5. Needed: Actual distribution of healing through wind-up, interruption and damage arrival ordering.
- **target_selection — deliberate_approximation:** Large cone becomes all-clump/one-spread. Evidence: a.rs:494-499; no coordinates/cone direction.
- **documentation — source_discrepancy:** Kit note says heal and cone both land at wind-up start; current punch is delayed. Evidence: kits.json Sett note versus a.rs:490-498.

Specific existing tests: none located beyond generic/golden coverage and the audit probes.

### Veigar (TFT18_Veigar)

Source: `tft_engine/src/drivers/late.rs:17`. Forms: base. No live-runtime certification.

- **damage_scaling — source_backed_implementation:** Deals AP-scaled damage of 265 at 2 stars, increasing to 400 below 30% maxHP. Evidence: HighHPSpellDamage/LowHPSpellDamage and HPThreshold = 0.3.
- **permanent_stacks — not_modeled:** No starting permanent AP history input is supplied: each standalone fight starts from zero earned stacks. Evidence: Fresh Driver and Sheet initialization; tooltip displays permanent current bonus. Needed: Recorded prior-game stack count is required for a late-game/capped comparison.
- **kill_trigger — unverified_runtime:** Every kill he makes adds 1.5 flat AP, including an ordinary auto. Tooltip ties the gain to target death after the blast; the KillBuffer row, valued at 0.2, is unused. Evidence: In the probe, the opening auto kills a 1 HP target; the next spell deals 268.975 rather than 265. late.rs:44-45. Needed: Spell-only/buffered kill credit versus all own takedowns; source cannot establish current broad interpretation.
- **attack_cast_mana_targeting — deliberate_approximation:** Single-target generic cast timing; HP threshold checked when cast damage resolves, not at target selection. Evidence: late.rs:32-41; no intrinsic AoE described.
- **item_trait_interactions — unverified_runtime:** Item/burn kills can also invoke kill hook; repeated simulations reset earned AP. Evidence: Generic Fight kill dispatch. Needed: Proc kill attribution and persistent-stack initialization.

Specific existing tests: none located beyond generic/golden coverage and the audit probes.

### Teemo (TFT18_Teemo)

Source: `tft_engine/src/drivers/late.rs:53`. Forms: base. No live-runtime certification.

- **damage_scaling — source_backed_implementation:** At 2 stars, the two clusters each use 90 AP-scaled damage, followed by a 200 AP-scaled giant mushroom. Evidence: APDamage and BigMushroomDamage rows; controlled cast totals of 380/740 under the current selections.
- **target_selection — source_discrepancy:** Each cluster uses local aoe(3), losing two stated independent nearest recipients in spread. Evidence: The tooltip specifies 2 clusters; NumEnemiesHit = 3; late.rs:68-75.
- **attack_cast_mana_timing — unverified_runtime:** Both clusters and giant resolve in one cast callback, with no projectile sequencing. Evidence: A generic 0.25 s cast landing is followed by nested loops. Needed: Actual mushroom launch/flight/landing cadence and target locking.
- **economy — not_modeled:** Foraged rerolls, tactician HP and XP are ignored by combat score. Evidence: Tooltip forage chance and Red/Green/Yellow rows; explicitly documented.
- **item_trait_interactions — unverified_runtime:** Each recipient/cluster is a separate ability hit and therefore an independent on-hit helper call. Evidence: late.rs:71 and 75. Needed: Multi-hit item/proc eligibility and application order.

Specific existing tests: none located beyond generic/golden coverage and the audit probes.

### Zyra (TFT18_Zyra)

Source: `tft_engine/src/drivers/late.rs:83`. Forms: base. No live-runtime certification.

- **damage_scaling_summoner — verified_arithmetic:** At 2 stars: 2 plants, with 55 AP-scaled damage per shot. Each plant fires 10/14/16 shots with no trait or Summoner columns 1/2, respectively. No inappropriate Summoner damageMult is applied. Evidence: TestSummonerDocumentedEffects.test_zyra_keeps_two_plants_with_only_documented_extra_attacks passed.
- **summon_timing — deliberate_approximation:** All plants shoot together immediately at next tick and once per second; each new cast adds another independent patch. Evidence: late.rs:100-125; kits note explicitly assumes 1 attack/s because snapshot lacks plant AS. Needed: True plant attack speed, initial delay, lifetime/overlap and inheritance at spawn versus each shot.
- **target_selection — deliberate_approximation:** Every plant follows current target; distributed spawn locations and each plant nearest enemy are absent. Evidence: Tooltip plants around battlefield; current f.target() per shot.
- **buffs_stacks_traits — not_modeled:** Base Thornmaiden team durability is handled by trait resolver; conditional dynamic plant-count upgrade remains separately disclosed. Evidence: TestTraitTeamResolution tests base durability and no fabricated six-plant start.
- **item_interactions — unverified_runtime:** Plant attacks use current owner AP and ability-hit/proc helpers on every shot. Evidence: late.rs:116-117 calls hit_ability, not a separate autonomous actor. Needed: Summon crit/item inheritance, snapshot-versus-dynamic AP and proc restrictions.

Specific existing tests: `test_tft_trait_team.py:150 test_zyra_keeps_two_plants_with_only_documented_extra_attacks`; `test_tft_trait_team.py:22 test_thornmaiden_base_durability_reaches_every_member_once`.

### Ivern (TFT18_Ivern)

Source: `tft_engine/src/drivers/late.rs:131`. Forms: base. No live-runtime certification.

- **damage_scaling — source_backed_implementation:** Direct explosion uses AP-scaled MagicDamageCalc1. Shield fallback uses ShieldAmount*AP/100 and expected crit multiplier if Precision. Evidence: MagicDamage = 140/210/2000; ShieldAmount = 165/300/3000.
- **shield_scaling — source_conflict:** ShieldCalc1 includes unmodeled OutgoingDamageMultiplier. Driver bypasses it; tooltip shows damage-amp scaling, which the fallback does not account for. Evidence: Kit note, normalized ShieldCalc1 unresolved term, runtime warning in focused tests. Needed: Recover exact shield damage-amp formula from raw spell calculations/runtime.
- **healing_shields — source_backed_implementation:** At 1–2 stars, two allies receive shields for 6 s in the shared engine; isolated/theory output is potential shielding with no invented recipient utilization. Evidence: Dedicated distinct-allies and expiry tests passed; theory limitation explicit.
- **target_selection — not_modeled:** Damage is one aoe_all pass, not bursts centered on the actual shielded allies. Evidence: late.rs:153-158; no ally center selection or overlap rules.
- **buffs_stacks_item_interactions — not_modeled:** Ally damage amp and stacking attack speed after cast threshold are omitted; ability/shield crit assumptions use common Precision EV. Evidence: Ivern tooltip and kit note; DamageAmpAmount/CastThreshold/AttackSpeed rows unused by driver.

Specific existing tests: `test_tft_team_engine.py:169 test_ivern_shields_distinct_allies`; `test_tft_team_engine.py:182 test_ally_shield_expires_at_its_ability_duration`; `test_tft_unit_profiles.py:117 test_ally_output_is_reported_as_potential_without_crediting_self_sustain`.

### Cinderling (TFT18_Cinderling)

Source: `tft_engine/src/drivers/late.rs:167`. Forms: base. No live-runtime certification.

- **damage_scaling — source_backed_implementation:** The five leaves are represented as one AD+AP total physical hit. At 2-star base stats, the AD-scaled portion is 465 and the AP-scaled portion is 45. Evidence: PhysicalDamageCalc1; source explicitly gives total damage to current target.
- **burn_wound — source_backed_implementation:** BurnAmount is divided by 100 and uses duration row; base 20% Wound availability is recognized by AntihealPolicy. Nonhealing benchmark targets cannot express Wound value. Evidence: CompUtility base-Wound test and native burn-suppression regression; source WoundLevel = 20.
- **alpha_stacks — source_backed_implementation:** With Alpha, TraitADOnCast is added as an AD fraction after each cast; initial earned stacks reset each simulation. Evidence: late.rs:192-194; Scarlet Buff wording.
- **attack_cast_mana_targeting — deliberate_approximation:** Single-target generic cast; all five leaves collapse to one landing, then burn. Evidence: Kit note; no intrinsic AoE specified.
- **item_trait_interactions — unverified_runtime:** Aggregated leaves perform one ability-hit proc sequence; nonstacking burn ownership may suppress this burn when another source owns the team channel. Evidence: late.rs:187 and UnitProfiles burn suppression. Needed: Leaf hit-by-hit procs and live Wound/burn duration/ownership interactions.

Specific existing tests: `test_tft_comp_utility.py:51 test_cinderling_base_ability_is_twenty_percent_wound_without_alpha`; `test_tft_unit_profiles.py:262 test_native_burn_suppression_preserves_other_ability_output`.

### Kog'Maw (TFT18_KogMaw)

Source: `tft_engine/src/drivers/late.rs:202`. Forms: AD, AP. No live-runtime certification.

- **damage_scaling_forms — source_backed_implementation:** AD combines AD+AP physical damage, multiplied by 1.4 below its HP threshold; AP uses initial magic damage plus an AP-scaled DoT lasting 3 s. Both forms implemented. Evidence: Both normalized form descriptions/calcs; late.rs:220-244.
- **target_selection — source_discrepancy:** Both form tooltips specify target plus other nearest enemy; aoe(2) suppresses second target in spread. Evidence: The controlled AD probe deals 250 versus 500; AP uses same selection.
- **attack_cast_mana_timing — deliberate_approximation:** Generic landing; threshold checked per selected target. AP DoT duration is modeled, but no acid projectile flight. Evidence: late.rs:225-243.
- **buffs_stacks_traits — verified_arithmetic:** Caustic maximum Sunder/Shred participates in shared providers without summing duplicate reductions. Evidence: TestCausticProviderReference and native trait-team scorer regressions exist.
- **item_interactions — unverified_runtime:** DoT and impact use different helper paths; form selection is item-derived before trait stats in shared Fx construction. Evidence: Common Fx and dot_ability/hit_ability helpers. Needed: Crit/proc timing for impact versus DoT; actual mixed-bonus form selection rules.

Specific existing tests: `test_tft_trait_team.py:50 test_caustic_joins_both_shared_reductions_using_maximum_not_sum`; `test_tft_trait_team.py:73 test_caustic_improves_another_carry_and_matches_reference`.

### Mama Beak (TFT18_MamaBeak)

Source: `tft_engine/src/drivers/late.rs:248`. Forms: base. No live-runtime certification.

- **damage_scaling_summons — source_backed_implementation:** Four beaks target same enemy as owner; each owner attack adds n*AD-scaled MiniDamage, with documented Summoner damageMult. Evidence: Tooltip explicitly same enemy; late.rs:270-283.
- **attack_cast_mana_timing — unverified_runtime:** Beak duration is MiniDuration*AP/100; recast refreshes one beaks_until rather than creating independent flocks. Beak damage lands with owner attack. Evidence: GenericCalc1 AP-scaled 5 s baseline; one timestamp field. Needed: Whether repeated casts refresh or add flocks and exact owner/beak attack sequence.
- **alpha_debuff — unverified_runtime:** After owner+aggregated beak damage, flat armor reduction is added for all counted physical hits at once. Native no-bridge armor_flat is persistent. Evidence: ArmorReduc = 2; hit count 1+n; source says dealing physical damage reduces armor. Needed: Per-hit armor reduction order and whether later beaks benefit from earlier hits in the same attack.
- **target_selection — source_backed_implementation:** Single target is intentional; no source-supported missing AoE. Evidence: Flock Family tooltip.
- **item_trait_interactions — unverified_runtime:** Aggregating beaks into one ability hit preserves base total against static resists, but not necessarily individual crit/proc/armor-strip ordering. Evidence: late.rs:278 calls hit_ability once with n multiplier. Needed: Summon item/crit inheritance, burn eligibility and last-hit attribution.

Specific existing tests: none located beyond generic/golden coverage and the audit probes.

### Azir (TFT18_Azir)

Source: `tft_engine/src/drivers/late.rs:291`. Forms: base. No live-runtime certification.

- **damage_scaling_summoner — verified_arithmetic:** Six commands replace autos; At the 2-star baseline, two soldiers each deal 69, multiplied by 1.45/1.675 with the documented Summoner tiers. Evidence: TestSummonerDocumentedEffects.test_azir_keeps_two_soldiers_with_only_documented_damage_multipliers passed.
- **attack_cast_mana_timing — unverified_runtime:** Adopted mana lock blocks attack mana and regen until all six commands are consumed, then releases with no extra delay. Evidence: Dedicated test_tft_azir regressions passed under this adopted model; tests explicitly do not establish live timing. Needed: Live command-window mana lock, sixth-command mana, and item effects during commands.
- **buffs_stacks — source_backed_implementation:** AttackSpeed = 2.5 means +150% bonus AS for the command count, removed after the sixth command. The additional Summoner summon row is deliberately not activated. Evidence: NumAttacks = 6; AttackSpeed = 2.5; current trait map excludes stale ExtraSummons.
- **target_selection — source_backed_implementation:** Soldiers strike current target as one multiplied ability hit; source does not promise area cleave. Evidence: Arise tooltip and late.rs:331-332.
- **item_interactions — unverified_runtime:** Each command is ability damage/procs, not base auto damage; soldier count is aggregated. Evidence: hit_ability; initial attack before cast still counts as normal auto. Needed: Empowered-command crit/on-hit flags and per-soldier proc eligibility.

Specific existing tests: `test_tft_azir.py:13 TestAzirProtectorsVow`; `test_tft_azir.py:85 TestAzirManaLock`; `test_tft_trait_team.py:139 test_azir_keeps_two_soldiers_with_only_documented_damage_multipliers`.

### Nidalee (TFT18_Nidalee)

Source: `tft_engine/src/drivers/late.rs:349`. Forms: AD, AP. No live-runtime certification.

- **forms — source_backed_implementation:** AD uses cougar stats/Assassin/frontline, AP uses Marksman/protected role. Aura reach follows actual form; AP range is interpreted as base range 1 plus AdditionalAttackRange (4), giving 5 hexes. Evidence: Equipped-form, UnitProfiles and carry-policy native tests pass. Needed: AP range application remains explicitly unobserved in runtime; generic bonus-based selection order needs confirmation.
- **damage_scaling_AD — source_backed_implementation:** AD swipe uses AD-scaled calc and temporary personal armor ignore. Every third cast heals AP-scaled amount and adds swipe*1.25*targetMissingHPFraction sampled before swipe. Evidence: AD form rows ArmorIgnoreRatio, NumCastsEmpowered = 3, ThirdAttackBonusDamageMissingHealth = 1.25 and ThirdAttackHeal.
- **damage_scaling_AP — source_backed_implementation:** AP cast grants three replacement javelins at +150% AS; every third thrown javelin uses stronger AP calc. Evidence: NumEmpoweredAttacks = 3; BonusAttackSpeed = 2.5; The native probe records 255 for each ordinary javelin and 480 for the stronger one at 2 stars.
- **attack_cast_mana_timing — unverified_runtime:** AP uses ordinary cast lock rather than Azir-style full empowerment lock; recast resets remaining javelins, while thrown counter continues. AD pounce heal occurs in cast callback. Evidence: late.rs:397-419. Needed: Live AP mana lock, recast/reset behavior and empowerment counter boundaries.
- **target_selection — deliberate_approximation:** AP strong javelin uses last alive index rather than actual furthest-with-fewest-items. AD target-change leap and movement are absent. Evidence: Both form descriptions; kit note discloses leap/range assumptions.
- **healing_item_trait_interactions — unverified_runtime:** AD heal is capped to missing HP; AP javelins use ability crit/procs. Personal armor ignore is implemented via a temporary flat armor adjustment during the hit. Evidence: late.rs:385-393; generic ability/crit/heal helper and aura tests. Needed: Confirm replacement-attack proc flags and personal armor-ignore interaction with shared reductions.

Specific existing tests: `test_tft_equipped_forms.py:51 test_enumeration_resolves_pressure_independently_for_each_build`; `test_tft_unit_profiles.py:207 test_nidalee_form_changes_role_without_mutating_the_snapshot`; `test_tft_adaptor_positions.py:61 test_melee_form_takes_frontline_pressure_while_ranged_form_is_protected`; `test_tft_carry_policy.py:198 test_native_top_one_retains_real_nidalee_ad_and_ap_before_parallel_truncation`.

## Shared boundaries

- **event_timing:** The generic attack/cast clock has 0.25 s upkeep. When absent, castTime defaults to 0.25. Attack windup and missile speed are not passed to native damage impacts. Full cast timing remains uncertain outside explicit channels.
- **crit_and_item_procs:** Expected-value crit and generic hit_attack/hit_ability/on-hit helpers apply. Replacement attacks and aggregated summons/multihits require individual live flag confirmation.
- **geometry:** aoe helper conflates local radius/line coverage and independent nearest-N targeting: all nearby clump or one current spread; no hex paths or dynamic zones.
- **source_provenance:** Pinned descriptions/calculation exports are useful evidence but do not certify live execution, proc flags, projectile timing or hidden server behavior.
- **fresh_combat_state:** Driver/state is recreated per fight. Permanent prior-combat stacks, ally-dependent buff utilization and actual placement are not automatically reconstructed.

Evidence and validation: [findings.json](findings.json), under `groups.a-late` and `groups.a-late.validation.focusedTests`. Probe observations: [observations.json](observations.json), under `groups.a-late-extra` and `groups.other-ranged-aoe`.

Root follow-up: full public BIN download found 74 available assets out of 79 requested; most main spells contain generic rows, not usable runtime formulas. This does not resolve the listed live timing/proc/stacking uncertainties. See `source-manifest.json` for the consolidated source manifest.
