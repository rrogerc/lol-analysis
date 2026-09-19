# Current trait models — Set 18, patch 18.1d

Reviewed September 8, 2026. All **36 traits** are accounted for below. This is a code/source assessment and native regression check, not validation against recorded games.

## Changes in this revision

- Removed the melee-carry limit. Supported one/two-carry arrangements, item budgets and required antiheal still define the board search.
- Compare legal Primal choices throughout roster screening and item refinement. Turtle contributes effective healing; the chosen blessing and alternative aggregate scores are displayed.
- Share Spellweaver AP using real allied cast events, with no duplicate self credit.
- Convert half of Solar’s existing bonus to true damage at five unique three-star champions.

## Blossom

Its base combat bonus is implemented: Blossom members gain **both** 12/30/40/45/100% bonus AD and the corresponding AP points at 3/5/7/9/11, plus 10% maximum health. The HP bonus joins the ordinary item/trait additive pool described in [HP stacking](hp-stacking.md). Ten native checks verify both Master Yi forms at all five tiers. Blossom adds equal normalized AD/AP, so it does not itself change the equipment-based Adaptor comparison.

Wisp purchases, upgrades, availability and acquired rewards are not supplied. Upper thresholds also require roster inputs the current search does not provide.

## Primal choices and scoring limits

Two Primals have four single choices; four Primals have six distinct pairs. The ordinary roster contains only three actual Primals until emblem or Avatar state is supplied, so pair support is tested but not reachable in today’s roster search.

| Choice | Implemented behavior | What the composition score can value |
| --- | --- | --- |
| Tiger | Delayed attack speed after six seconds. | Uses the existing 35% member / 15% nonmember interpretation; additive team stacking is still uncertain. |
| Turtle | Each living ally heals 4% of its current maximum HP every four seconds. | Effective healing, after Wound and missing-health limits, extends frontline protection. |
| Bear | Primal damage executes finite targets below 12% HP. | The score uses immortal full-health probes, so execute value remains unmeasured. |
| Phoenix | Source describes a component every 15 Primal takedowns, at most four. | No invented items or economy multiplier are added to fixed-budget comparisons; progression value remains unmeasured. |

A single blessing choice is used across the entire pressure aggregate (48 profiles in v3, including both focus orientations). All legal choices are compared for each item allocation. Bear/Phoenix share a calculation where their modeled effects are identical; their equal scores do not imply equal real-game value.

Level-nine plans retain earlier choices and may add the second blessing when eligible. Ordered selection history is preserved separately when inactive. This is a conservative no-respec interpretation of the source’s first/second selections; losing and regaining Primal is not fully decoded.

## Coverage of all 36 traits

“Core combat modeled” means the principal fixed-board stats or trigger reach the score. Remaining boundaries are listed even for those traits; it does not certify the full trait or champion in a live match.

| Trait | Coverage | Scored behavior | Remaining gap |
| --- | --- | --- | --- |
| Adaptor | Partial | 25/35/50% AD or AP on members; selected AD/AP kit and equipped Nidalee role. | Form selection is fixed from equipment/role. Exact treatment of other traits, dynamic stats and ties remains unverified. |
| Apex Predator | Structural | Elder uses 2 slots and contributes 2 Riftbeast total. | Structural behavior is supported. Elder Alpha execute is a separate trait/kit effect that cannot trigger on immortal targets. |
| Attuned | Central reward missing | Alune's fourth-cast full-moon damage exists; team phase buffs contribute zero. | Needs verified initial moon phase and cast-driven team windows: 7% durability at/below half moon, otherwise 7% amp. Do not apply both permanently or guess equal uptime. |
| Avatar | Central reward missing | Only generic Lux laser damage; no selected origin or doubled origin count. | Needs one legal Lux variant, origin count ×2, shared-trait cast mana and variant effects. Use pinned 18.1d values, not July preview numbers. |
| Blackthorn | Central reward missing | No sacrifice or resulting bonuses are scored; the sacrifice hex is empty. | Needs victim identity, actual removal, role/cost/star rewards and team HP. Pinned HP is 175/300/550; missing tier-4 multiplier and tier-6 preview-versus-asset semantics require resolution. |
| Blossom | Core combat modeled | Members gain 12/30/40/45/100% AD and matching AP points, plus 10% max HP. | Wisps, purchases, upgrades and acquired rewards have no supplied state. Adaptor form is selected before traits, but Blossom adds equal normalized AD/AP so it cannot flip that comparison by itself. Master Yi AD and AP both receive both stat bonuses. Without emblem/Avatar states, upper 9/11 thresholds are not reachable by the ordinary distinct-unit board search. |
| Bounty Seeker | Economy | Economy/progression trait; no direct fixed-board combat score. | Needs chosen bounty, progress and reward/economy state to value attainability. Pinned rewards differ from the pre-PBE overview. |
| Brawler | Core combat modeled | Team +120 HP; members add 25/40/65% to the ordinary item/trait HP percentage pool. | Additive stacking is the adopted model interpretation; current Set 18 aggregation is not independently verified. See [HP stacking](hp-stacking.md). |
| Caustic | Partial | Kog'Maw damage applies 30% Sunder/Shred for 4s; theory now shares max provider strength with ally damage. | **Fixed:** shared provider detection ignored Caustic. Shared coverage still assumes opening uptime and does not track actually hit targets or provider death. |
| Coven | Central reward missing | No Essence, rituals or acquired rewards are scored. | Needs history/cashout inputs. Dormant AP-per-Essence rows do not establish live combat AP. Sparse tier-4 loss reward conflicts with archived client value 25. |
| Defender | Core combat modeled | Team share +12 armor/MR; members receive +25/60/120 total instead. | No trait-specific numeric/recipient contradiction found. This is not validation of whole-game accuracy. |
| Eclipse | Central reward missing | No combat effect; eligibility is noted when 3 Solar and 3 Lunar coexist. | The source minUnits=0 is a placeholder, not automatic activation. Needs delayed (10s, then every 3.5s) lowest-health kill logic and a justified enemy-health model. |
| Elderwood | Central reward missing | No Stonebark bodies, Lifebloom ramp or Deepwood Protector are scored. | Needs summoned actors, plant positions/survival and star scaling: +25% HP and +10% AP per total Elderwood star. Existing extras include parts of the plants, but not a verified complete implementation. |
| Emerald Aspect | Partial | Taric self healing/shielding and own two-attack charge score; one ally shield is potential only. | Needs paired ally, its threshold trigger, confirmed charge recipients and area-shield utilization. Pair healing has a preview-versus-pinned-text ambiguity. |
| Executioner | Partial | Precision and +15% crit; at 3/4, 30/40% bonus true bleed over 3s. | Eligible damage origins and post-death bleed payout are not fully verified or valued by isolated response windows. |
| Fae | Partial | Per assumed pixie: 5/8% AD/AP and 2/4% low-health healing; current assumed counts are 3/7. | Actual count/progress/history and golden rewards are missing. Fixed 3/7 opening counts are assumptions, not sourced progression. Do not invent output thresholds. |
| Flora Fatalis | No trigger in theory | Native takedown hooks exist: 10 mana at 1; also 8% max-HP healing at 2. | No takedowns occur in theory profiles, so effective score contribution is zero. Assist window and declared enemy-death assumptions are missing. |
| Greenfather | Central reward missing | No cultivated-hex bonus is scored. Ivern damage is measured; ally shields remain potential. | Needs biome, planted hexes/occupants, prior seeds and timing. Ivern ally amp and post-six-cast AS also lack recipient scoring. Do not assume a fully planted board. |
| Hunter | Partial | +20/30/45/65% AD; +10% amp after 4s on the same primary target, including eligible secondary-target damage. | **Fixed and native-tested:** amp was restricted to the primary recipient. Target-reset behavior has regression coverage; full-game targeting and immortal-target focus uptime remain approximations. |
| Inferno | Partial | Separate 1/1/2/3% max-HP burn channel for 4s; stacks with the ordinary burn channel. | Global single-owner suppression undercounts different sources covering different targets. Wound has no value against nonhealing targets; shop ignition is excluded. |
| Invoker | Partial | Nonmembers +1/1/2/2 mana/s; current member totals +4/5/8/11. | Whether member rows 3/4/6/9 are additional or total is unresolved. Do not replace current totals with another guess. |
| Juggernaut | Core combat modeled | Members 20/30/40% durability; other allies 4/6/8%. | **Fixed:** tier-4 team share was 4%, now 6% from archived CDragon evidence. This reconciles sources; it is not a new patch-note balance change. |
| Lunar | Partial | Members receive 14/20/28/36% AS and matching AP points (twice base share). | Adjacent nonmembers receive no 7/10/14/18% share. Needs explicit adjacency/overlap recipients; Eclipse is also absent. |
| Monolith | Core combat modeled | Malphite receives +10 armor/MR per persistently assigned incoming source, including a stunned source. Opening and live defenses use the same assignment; three source identities are independent of outgoing spread/clump. | Full-game attacker counts and positioning remain abstract. V3 uses two mirrored focus orientations and retargets after death or untargetability; the earlier one-current-source convention is superseded. |
| Old Growth | Central reward missing | Maokai kit is scored, but prior HP stacks are zero and no new stacks accrue. | At 1/2★, each eligible death within 3 hexes gives 30 permanent HP. Needs explicit history and future death/range assumptions. |
| Primal | Partial | Explicit legal blessing selections: delayed Tiger, Turtle effective 4% max HP healing every 4s for living allies, Bear 12%-HP executes on finite targets. Resolver exposes 4 single choices or 6 pairs at an actual 4 Primal count; native fields validated by focused regressions. | Bear cannot trigger on immortal theory targets; Phoenix takedown/component economy has no invented fixed-budget combat credit. Tiger preserves the declared 35% member / 15% nonmember interpretation. Ordinary roster has only 3 actual Primal units until Avatar/emblem state is supplied, so pair support is tested but not naturally reachable by current roster search. |
| Rapidfire | Core combat modeled | Allies +10% AS; members +3/5/9/15% per attack, at most 10 stacks. | Special attack event eligibility and unused duration-row semantics remain unverified. |
| Ravager | Partial | +12/25/40% damage and 10% omnivamp. | The doubled bonus below 50% enemy HP never activates against full-health immortal targets. Needs explicit health-phase assumptions. |
| Riftbeast | Partial | One selected Alpha. At 7, +5% AD/AP/AS, +5 armor/MR, +50 HP and +1 mana/s at opening and every 5s. | One explicit Alpha holder; timed 7-piece growth is scored. Mama Beak armor reduction and Pebbles MR reduction are now shared across theoretical actors by native flat-resistance deltas. Sentinel allied cast mana, Scuttle ally healing and effective Krug ally shields remain incomplete; Elder executes stay inactive on immortal targets. Shops/max-team-size are outside the fixed board. Resolver coverage text now reports the shared strip correctly. |
| Rival | Central reward missing | Base Kha'Zix/Rengar kits only; no trait-state combat value. | Needs takedowns, Kha evolution choices/additional trait counts, Rengar team AD (5% at 30, +0.2% thereafter), and verified Rival(2) cross-cast effects. Detailed source text lives in tier effects, not normalized desc. |
| Solar | Partial | All allies receive shield and bonus damage scaled by actual unique three-star count; at 3 three-stars the existing AS/resists apply. At 5 three-stars, exactly half the existing bonus becomes true damage in ordinary and bridged native paths. No bonus amount is added by conversion. | Eight-three-star recurring four-star ascension remains absent. Existing Solar damage-origin/proc classification is retained, including no recursive bonus procs and exclusion of raw/true source damage. Generic geometry and action timing remain. |
| Spellweaver | Partial | Team +10 AP; current member totals 20/40/65 AP. Each actual Spellweaver cast grants 1/1/2 AP to every living allied Spellweaver, including self exactly once. Shared theory, legacy team and symmetric combat use real native cast events. | Static member total-versus-additional wording remains unverified. Event delivery uses each shared scheduler action boundary, retaining existing action timing assumptions. Generic own-cast AP is not treated as Spellweaver membership. |
| Sprykin | Central reward missing | No BFF actor, rider HP/AS or shared BFF output is scored. | Needs rider/mode and verified BFF kit. Pinned rider HP +15/40/45%, AS +15/35/45%; archived tier-5 sharing ratio is 0.5, missing from sparse lookup. Prototype extras contain contradictions. |
| Summoner | Partial | At 2/3: Yorick summon HP ×1.30/1.45; Azir/Mama damage ×1.45/1.675; Zyra +4/+6 attacks. | **Removed unsupported overcredit:** orphaned ExtraSummons/SummonPower mappings. Azir and Zyra keep their base summon counts. Plant attack cadence remains approximate; no live script trace was obtained. |
| Thornmaiden | Partial | 5% base durability now reaches all allies once. | **Fixed:** previously only Zyra received it. The increase to 10% with at least 6 living plants is still omitted; no unconditional higher bonus is assumed. |
| Vanguard | Core combat modeled | 18/32/42% HP shields for 10s at start and first crossing below 50% HP; tier 6 adds 5% durability only while shielded. | **Fixed:** durability was unconditional. Exact same-time shield ordering follows the engine; native verification is recorded below. |

## Verification

Engine `4116bc4251fd` passes 854 Python tests, with one existing unique-item-pool skip (855 run), including all 7,670 archived fights and all 1,770 ranked-cell checks. All eight Rust unit tests pass. Every previous standalone fight remains bit-identical. Additional native probes verify all Blossom tiers/forms and the newly added trait mechanics.

Complete item-search comparisons agree with the independent reference on winners, detailed evidence and search coverage. Both winners and their full Primal-choice evidence were also replayed on the final source revision. With Primal choices, the reported nine-item search compares 3,230 allocations in 36.191 s cold / 36.107 s repeated in Rust. The dense twelve-item workload without active Primal compares 5,583 in 29.422 s / 29.213 s. Snapshot/anchor preparation is excluded; these are optimizer timings, not a full rebuild estimate.

A complete real-roster sample through seed generation, item refinement, board composition and validation chose Turtle. Its score was 54,286,116 versus 51,367,873 for Tiger under the declared assumptions; the generated-data UI check passed.

The trait-choice generation was rebuilt in 83 min 49 s and published as `g-ae3fa683e59d701b8bc31c119a0af44c2418a9cc92360ae3fd3fbd38ef573990`. Generated-data UI validation passed for all 1,664 cores and 416 level-nine upgrades, including 2,080 clipboard roster round trips. Its four live metadata/status endpoints agreed at publication, with all 1,770 champion cells and eight composition contexts ready. Exact sources, measurements, selected blessing counts and that publication state are in [trait-models.json](trait-models.json). The [earlier trait audit](trait-audit.md) remains historical evidence.

The subsequent v3 targeting generation, `g-574cbbba155f`, replaces the one-current-source convention with persistent incoming owners and 48 profiles. It is published and passed the same complete UI/copy checks. Current behavior, tank comparisons, remaining HP-model questions and rebuild evidence are in the [pressure-targeting report](pressure-targeting.md).

Primary evidence: the pinned [trait lookup](18.1d/metatft.json), [Riot-derived client archive](18.1d/communitydragon.json), and [Riot’s overview](https://teamfighttactics.leagueoflegends.com/en-sg/news/game-updates/enchanted-wilds-overview/). The overview predates release; pinned reviewed values take precedence.
