# Set 18 trait scoring audit — patch 18.1d

This is the historical September 7 audit. See the
[current trait models](trait-models.md) for subsequent Primal choices and healing,
shared Spellweaver casts, Solar conversion, shared flat reductions and the removal
of the melee-carry limit, with current verification and remaining gaps.

Audit date: 2026-09-07. All **36 traits** were checked against their archived
numbers and descriptions, effect mapping, board recipients, native mechanics
and actual use by the theoretical scorer. The ten Riftbeast Alpha variants
were checked separately below. An implemented field or active trait icon does
not establish that its combat benefit reaches the score.

The strongest confirmed problems were missing team recipients, missing timed
effects, and unsupported legacy rows. The corrections below do not establish
that every trait is accurate, or that a particular cost tier must be stronger.
Roster candidate coverage is a separate source of ranking error.

## Source boundaries

Use the pinned [lookup][Snapshot], [Riot-derived client archive][Client traits],
[patch corrections][Overrides] and [audit provenance][Audit], located by the
API in each row. The lookup is marked PBE and was generated August 16; its
18.1d corrections are explicit. Archived CommunityDragon metadata reports an
August 29 source timestamp. The [official patch article][Riot patch] was checked
through the August 31 balance and September 1 bug-fix sections.

The [July 27 Riot overview][Riot overview] establishes broad mechanics but is
pre-release. Its Fae, Lux, Blackthorn and bounty values must not overwrite pinned
18.1d evidence. Current source URLs are mutable. Additional direct character-bin
requests were blocked or unavailable; archived bins primarily provide champion
timing, not complete trait scripts. Unresolved runtime semantics remain explicit.

## Corrections made during this audit

- **Roster screening:** native EHP × DPS now includes the actual board's trait
  effects before pruning candidates, comparing all eligible Alpha holders.
  The previous isolated-unit sum missed trait supports. A regression shows
  1-cost Varus activating Inferno with Amumu and beating an extra 4-cost
  Aphelios during screening, despite losing under the old proxy. No cost or
  trait-count points were added.
- **Thornmaiden:** 5% base durability now reaches the whole team once, rather
  than only Zyra. The six-live-plant increase remains unsupported.
- **Caustic:** the trait joins shared Sunder/Shred provider detection in both
  native and reference scorers. The strongest provider wins; percentages do
  not add. Shared opening uptime remains an approximation.
- **Summoner:** removed undocumented `ExtraSummons` and unused `SummonPower`
  mappings. Active descriptions/`curveValues` establish only summon HP,
  damage and plant attack-count bonuses. This removes unsupported legacy-row
  overcredit; it is not a claim of a verified live script trace.
- **Juggernaut (4):** corrected nonmember durability from 4% to 6% using
  archived client evidence. The generic sparse-curve lookup rule is unchanged.
- **Vanguard (6):** the 5% durability bonus now depends on a live shield.
- **Hunter:** after the four-second stable-target timer, damage amplification
  now reaches eligible secondary-target damage as well as the primary target.
- **Primal / Riftbeast:** Tiger starts after six seconds and includes the team
  share; Riftbeast (7) repeats its full sourced growth packet every five seconds.
- **Sentinel Alpha:** removal of the unsupported opening self-mana bonus is
  part of the audit correction. Its sourced per-cast team mana remains omitted.

Native verification status is recorded under Checks; a source-backed change
and a completed runtime check are separate claims.

## All 36 traits

“Scored” means supported under the declared EHP × DPS assumptions. The UI uses
`supported`, `partial`, `unmodeled`, `structural` or `economy`, with applicable
`coverageNotes`. Partial support is not presented as full coverage. Economy and
persistent-state omissions do not justify arbitrary flat combat bonuses.

| Trait / API | What reaches the score or was corrected | Remaining inputs, omissions or uncertainty | Evidence |
| --- | --- | --- | --- |
| **Adaptor** (2/3/4)<br>`DA_18_Adaptor` | 25/35/50% AD or AP on members; selected AD/AP kit and equipped Nidalee role. | Form selection is fixed from equipment/role. Exact treatment of other traits, dynamic stats and ties remains unverified. | [Map] [FX] |
| **Apex Predator** (1)<br>`DA_18_ApexPredator` | Elder uses 2 slots and contributes 2 Riftbeast total. | Structural behavior is supported. Elder Alpha execute is a separate trait/kit effect that cannot trigger on immortal targets. | [Board] [Fighters] [Riot overview] |
| **Attuned** (1)<br>`DA_AluneUniqueTrait18` | Alune's fourth-cast full-moon damage exists; team phase buffs contribute zero. | Needs verified initial moon phase and cast-driven team windows: 7% durability at/below half moon, otherwise 7% amp. Do not apply both permanently or guess equal uptime. | [Resolver] [Early] |
| **Avatar** (1)<br>`DA_18_LuxUniqueTrait` | Only generic Lux laser damage; no selected origin or doubled origin count. | Needs one legal Lux variant, origin count ×2, shared-trait cast mana and variant effects. Use pinned 18.1d values, not July preview numbers. | [Board] [Resolver] [Carries] [Overrides] |
| **Blackthorn** (2/4/6)<br>`DA_18_Blackthorn` | No sacrifice or resulting bonuses are scored; the sacrifice hex is empty. | Needs victim identity, actual removal, role/cost/star rewards and team HP. Pinned HP is 175/300/550; missing tier-4 multiplier and tier-6 preview-versus-asset semantics require resolution. | [Resolver] [Snapshot] [Client traits] |
| **Blossom** (3/5/7/9/11)<br>`DA_18_Blossom` | Members gain 12/30/40/45/100% AD and matching AP points, plus 10% max HP. | Wisp purchases, upgrades and acquired combat/economy rewards have no input state. | [Map] [Resolver] [Snapshot] |
| **Bounty Seeker** (1)<br>`DA_DravenUniqueTrait18` | Economy/progression trait; no direct fixed-board combat score. | Needs chosen bounty, progress and reward/economy state to value attainability. Pinned rewards differ from the pre-PBE overview. | [Snapshot] [Carries] [Riot overview] |
| **Brawler** (2/4/6)<br>`DA_18_Brawler` | Team +120 HP; members also ×1.25/1.40/1.65 HP. | General stacking of separate percentage-HP sources is an engine assumption, not independently established by this audit. | [Map] [Resolver] [FX] |
| **Caustic** (1)<br>`DA_18_Caustic` | Kog'Maw damage applies 30% Sunder/Shred for 4s; theory now shares max provider strength with ally damage. | **Fixed:** shared provider detection ignored Caustic. Shared coverage still assumes opening uptime and does not track actually hit targets or provider death. | [Map] [Theory] [Reference] [Team tests] |
| **Coven** (3/4/5/7)<br>`DA_18_Coven` | No Essence, rituals or acquired rewards are scored. | Needs history/cashout inputs. Dormant AP-per-Essence rows do not establish live combat AP. Sparse tier-4 loss reward conflicts with archived client value 25. | [Resolver] [Snapshot] [Client traits] |
| **Defender** (2/4/6)<br>`DA_18_Defender` | Team share +12 armor/MR; members receive +25/60/120 total instead. | No trait-specific numeric/recipient contradiction found. This is not validation of whole-game accuracy. | [Map] [Resolver] |
| **Eclipse** (0)<br>`DA_18_Eclipse` | No combat effect; eligibility is noted when 3 Solar and 3 Lunar coexist. | The source minUnits=0 is a placeholder, not automatic activation. Needs delayed (10s, then every 3.5s) lowest-health kill logic and a justified enemy-health model. | [Resolver] [Snapshot] |
| **Elderwood** (3/5/7/9/11)<br>`DA_18_Elderwood` | No Stonebark bodies, Lifebloom ramp or Deepwood Protector are scored. | Needs summoned actors, plant positions/survival and star scaling: +25% HP and +10% AP per total Elderwood star. Existing extras include parts of the plants, but not a verified complete implementation. | [Resolver] [Snapshot] [Riot overview] |
| **Emerald Aspect** (1)<br>`DA_Emerald18` | Taric self healing/shielding and own two-attack charge score; one ally shield is potential only. | Needs paired ally, its threshold trigger, confirmed charge recipients and area-shield utilization. Pair healing has a preview-versus-pinned-text ambiguity. | [Resolver] [Frontline] [Riot overview] |
| **Executioner** (2/3/4)<br>`DA_18_Executioner` | Precision and +15% crit; at 3/4, 30/40% bonus true bleed over 3s. | Eligible damage origins and post-death bleed payout are not fully verified or valued by isolated response windows. | [Map] [FX] [Fight] |
| **Fae** (2/4)<br>`DA_18_Fae` | Per assumed pixie: 5/8% AD/AP and 2/4% low-health healing; current assumed counts are 3/7. | Actual count/progress/history and golden rewards are missing. Fixed 3/7 opening counts are assumptions, not sourced progression. Do not invent output thresholds. | [Map] [Fight] [Snapshot] |
| **Flora Fatalis** (1/2)<br>`DA_FloraFatalis18` | Native takedown hooks exist: 10 mana at 1; also 8% max-HP healing at 2. | No takedowns occur in theory profiles, so effective score contribution is zero. Assist window and declared enemy-death assumptions are missing. | [Map] [Fight] [Theory] |
| **Greenfather** (1)<br>`DA_18_Greenfather` | No cultivated-hex bonus is scored. Ivern damage is measured; ally shields remain potential. | Needs biome, planted hexes/occupants, prior seeds and timing. Ivern ally amp and post-six-cast AS also lack recipient scoring. Do not assume a fully planted board. | [Resolver] [Late] [Riot overview] |
| **Hunter** (2/3/4/5)<br>`DA_18_Hunter` | +20/30/45/65% AD; +10% amp after 4s on the same primary target, including eligible secondary-target damage. | **Fixed and native-tested:** amp was restricted to the primary recipient. Target-reset behavior has regression coverage; full-game targeting and immortal-target focus uptime remain approximations. | [Map] [Fight] [Class tests] |
| **Inferno** (2/3/5/7)<br>`DA_18_Inferno` | Separate 1/1/2/3% max-HP burn channel for 4s; stacks with the ordinary burn channel. | Global single-owner suppression undercounts different sources covering different targets. Wound has no value against nonhealing targets; shop ignition is excluded. | [Map] [Fight] [Theory] |
| **Invoker** (2/3/4/5)<br>`DA_18_Invoker` | Nonmembers +1/1/2/2 mana/s; current member totals +4/5/8/11. | Whether member rows 3/4/6/9 are additional or total is unresolved. Do not replace current totals with another guess. | [Map] [Resolver] |
| **Juggernaut** (2/4/6)<br>`DA_Juggernaut18` | Members 20/30/40% durability; other allies 4/6/8%. | **Fixed:** tier-4 team share was 4%, now 6% from archived CDragon evidence. This reconciles sources; it is not a new patch-note balance change. | [Overrides] [Resolver] [Class tests] |
| **Lunar** (2/3/4/5)<br>`DA_18_Lunar` | Members receive 14/20/28/36% AS and matching AP points (twice base share). | Adjacent nonmembers receive no 7/10/14/18% share. Needs explicit adjacency/overlap recipients; Eclipse is also absent. | [Map] [Resolver] [Snapshot] |
| **Monolith** (1)<br>`DA_18_Battlemage` | +10 armor/MR per targeting enemy on Malphite. | Incoming attacker count is currently coupled to spread/clump target count; this can change EHP at the same raw DPS. Needs a separate visible focus-count input. | [Resolver] [Fight] [Theory] |
| **Old Growth** (1)<br>`DA_18_Maokai_UniqueTrait` | Maokai kit is scored, but prior HP stacks are zero and no new stacks accrue. | At 1/2★, each eligible death within 3 hexes gives 30 permanent HP. Needs explicit history and future death/range assumptions. | [Resolver] [Frontline] |
| **Primal** (2/4)<br>`DA_Primal18` | Assumed Tiger now starts after 6s: current interpretation is +35% AS to members, +15% to other allies. | **Fixed:** old +35% opening member-only buff. Blessing choice/second blessing and whether member 35% includes the team 15% remain unresolved; Bear, Turtle and Phoenix are absent. | [Map] [Resolver] [FX] [Timing tests] |
| **Rapidfire** (2/3/4/5)<br>`DA_18_Rapidfire` | Allies +10% AS; members +3/5/9/15% per attack, at most 10 stacks. | Special attack event eligibility and unused duration-row semantics remain unverified. | [Map] [FX] [Fight] |
| **Ravager** (2/4/6)<br>`DA_18_Slayer` | +12/25/40% damage and 10% omnivamp. | The doubled bonus below 50% enemy HP never activates against full-health immortal targets. Needs explicit health-phase assumptions. | [Map] [Fight] [Theory] |
| **Riftbeast** (3/5/7/10)<br>`DA_Riftbeast18` | One selected Alpha. At 7, +5% AD/AP/AS, +5 armor/MR, +50 HP and +1 mana/s at opening and every 5s. | **Fixed:** recurring growth was omitted. Alpha support is uneven (matrix below); shop/team-size rewards are excluded. Do not add unused timer-heal/AS rows. | [Map] [Resolver] [FX] [Timing tests] |
| **Rival** (1/1/2)<br>`DA_18_Rival` | Base Kha'Zix/Rengar kits only; no trait-state combat value. | Needs takedowns, Kha evolution choices/additional trait counts, Rengar team AD (5% at 30, +0.2% thereafter), and verified Rival(2) cross-cast effects. Detailed source text lives in tier effects, not normalized desc. | [Resolver] [Carries] [Fighters] |
| **Solar** (3)<br>`DA_18_Solar` | All allies: 5% HP shield for 12s and 7% bonus magic damage, both +1.5 points per unique 3★; at three 3★, +18% AS and +15 armor/MR. | At five 3★, half the bonus damage should become true damage; at eight 3★, recurring 4★ ascension is absent. Eclipse is absent. | [Resolver] [Fight] [Snapshot] |
| **Spellweaver** (2/4/6)<br>`DA_18_Spellweaver` | Team +10 AP; current member totals 20/40/65 AP; own casts add 1/1/2 AP. | Casts do not grant AP to other Spellweavers. Member total-versus-additional AP wording is unresolved. A timed ally-cast model is needed, not a flat AP bonus. | [Map] [Resolver] [Fight] |
| **Sprykin** (3/5/7)<br>`DA_18_Sprykin` | No BFF actor, rider HP/AS or shared BFF output is scored. | Needs rider/mode and verified BFF kit. Pinned rider HP +15/40/45%, AS +15/35/45%; archived tier-5 sharing ratio is 0.5, missing from sparse lookup. Prototype extras contain contradictions. | [Resolver] [Snapshot] [Client traits] |
| **Summoner** (2/3)<br>`DA_18_Summoner` | At 2/3: Yorick summon HP ×1.30/1.45; Azir/Mama damage ×1.45/1.675; Zyra +4/+6 attacks. | **Removed unsupported overcredit:** orphaned ExtraSummons/SummonPower mappings. Azir and Zyra keep their base summon counts. Plant attack cadence remains approximate; no live script trace was obtained. | [Map] [Late] [Early] [Team tests] |
| **Thornmaiden** (1)<br>`DA_18_ZyraUniqueTrait` | 5% base durability now reaches all allies once. | **Fixed:** previously only Zyra received it. The increase to 10% with at least 6 living plants is still omitted; no unconditional higher bonus is assumed. | [Map] [Resolver] [Late] [Team tests] |
| **Vanguard** (2/4/6)<br>`DA_18_Vanguard` | 18/32/42% HP shields for 10s at start and first crossing below 50% HP; tier 6 adds 5% durability only while shielded. | **Fixed:** durability was unconditional. Exact same-time shield ordering follows the engine; native verification is recorded below. | [Map] [FX] [Fight] [Class tests] |

## Riftbeast Alpha coverage

| Holder | Sourced benefit | Effective coverage / missing state |
| --- | --- | --- |
| Cinderling | 22% AD each cast | implemented and scored for the holder. AD extra is a percentage, not flat AD; normal burn is separate from Alpha. No extra wound-related bonuses inferred from dormant rows. |
| Pebbles | 2 mana regeneration every 4 seconds actually channeled | implemented and scored for the holder. Its ordinary flat MR reduction is local to its isolated target; added team benefit from longer shredding is not shared. |
| Murkwolf | Precision and 25–75% added crit based on missing health | implemented and scored under modeled self pressure. Actual target access and kill-driven movement remain outside the capacity model. |
| Gromp | 30% AD or 30 AP every 5 seconds by selected form | implemented and scored for the holder. Do not add TraitAllyAD/AP orphaned rows as additional live aura without source text. |
| Scuttlecrab | Allies crossing 50% health restore 15% maximum health | entire Alpha effect absent; not even emitted as potential output. Driver never reads fx.riftbeast or the Alpha heal rows. Model recipient threshold/history and effective healing; confirm any timing/persistence rule before adding it. |
| Mama Beak | Each physical hit reduces target armor by its ArmorReduc row | holder benefits; shared ally damage benefit absent. Uses flat armor reduction, not percentage Sunder; cannot be represented by simply adding a permanent percentage provider. Requires target/time-dependent shared flat reduction. |
| Krug | Krug and Kruglette deaths shield allies for 8% maximum health | only one potential shield emitted on Krug death; not scored; Kruglette death shields absent. Generic Body has no per-body death shield hook. Receiver maximum-health basis and recipient count need source clarification. |
| Brambleback | Attacks burn and restore 4% own maximum health | self heal is scored; normal burn credited only when selected as its channel owner. Non-owner burn suppression preserves healing. Frenzy uses one eight-second AD/personal armor-ignore buff refreshed on recast as an explicit conservative interpretation; pinned tooltip and compatibility spell export do not establish stacking or a duration-long mana lock. Generic mana lock remains in use. Same-target leap triggers are unavailable against immortal targets. |
| Sentinel | Each cast grants allies 2 mana regeneration | Unsupported opening +5 self mana/s removed as an audit correction; the actual +2 mana/s per cast to allies remains unmodeled. Needs verified cast-event and recipient state. |
| Elder Dragon | Damage executes enemies below 12% health | driver implemented, but never activates against immortal full-health targets. No Alpha execution value enters theoretical scores. Do not invent execute damage or kill frequency. |

Alpha source definitions are in the pinned lookup's champion ability records;
implementations are in [early][Early], [carry][Carries], [fighter][Fighters],
[frontline][Frontline 1], [additional frontline][Frontline] and [late][Late]
drivers. Self mechanics, allied output
and target debuffs must be audited separately: isolated holder damage can be
correct while its team benefit is entirely missing.

## Remaining model work

Several important verticals still lack their central combat reward:
Blackthorn sacrifices, Elderwood plants, Sprykin's BFF/rider and Greenfather
hexes. Other missing inputs include Lux variants, Fae pixies, Rival evolution
and takedown history, Old Growth stacks, Primal blessings and Taric pairing.
These are explicit state problems, not evidence that those traits are weak.

Cross-unit cast and recipient effects require native time-aware support:
Spellweaver AP sharing, Attuned phases, Sentinel mana, Lunar adjacency,
Thornmaiden plant thresholds and effective allied healing/shields. A synchronized
abstract allied cohort can retain generic pressure assumptions; named enemy
boards are not required. A simpler sampled timeline correction must disclose
its feedback/uptime approximation rather than pretending to converge.

Target-health phases, per-target debuff/burn coverage and incoming focus count
also matter. Full-health immortal targets suppress executes/takedowns and
below-half bonuses. One global burn owner misses disjoint target coverage.
Outgoing area coverage must not silently choose the number of incoming attackers.

## Checks

The final native engine used for these checks has source hash
`0efe17e8dc01bbddc1f904d7f21b0e448aede6c5eb7766ec8451389b06efbbce`.

- `test_tft_trait_team.py` checks all-team Thornmaiden recipients, Caustic
  max-provider sharing and another carry's damage, and source-based Summoner
  summon counts/multipliers. All eight cases passed, including native shared
  debuff and frontline EHP checks.
- `test_tft_trait_fixes.py` checks the audited Juggernaut share and conditional
  Vanguard durability, plus Hunter's secondary damage and target reset. All
  nine source/resolver and native cases passed.
- `test_tft_trait_timing.py` checks delayed Primal and recurring Riftbeast packets,
  plus removal of unsupported Sentinel opening mana. All eight resolver and
  native timing cases passed.
- `jobs/test-tft-composition-ui.cjs` passes coverage-label, accessible-note and
  older-artifact fallback tests alongside existing composition contracts.
- The complete TFT suite completed 674 tests successfully, with one existing
  skip, including the deliberately regenerated golden fixtures. Native fields
  and rankings also agree with the current reference corpus of 80 cases across
  30 champions. The standalone replay matches all 7,670 new golden fights.

Replaying the 7,670 historical fights found **6,749 bit-identical results and
921 changed results**, all within corrected trait contexts, with zero unexpected
changes. The per-trait change groups overlap when a fight contains multiple
corrected traits. This is an impact check, not a claim that the old golden
expectations all pass. The comparison was recorded in
`/tmp/tft-trait-golden-comparison.json`; the regenerated fixture provenance and
[golden notes](../golden/README.md) retain the reason and previous artifact identity.

Representative full item-search benchmarks ran on pinned CPU 2, preserving
both seeds, the full item pool and production search limits. Winners, complete
item evidence and allocation counts agreed for cold and warm runs:

| Workload | Allocations compared | Reference cold | Native cold |
| --- | ---: | ---: | ---: |
| Reported nine-item board | 5,024 | 45.437s | 1.071s |
| Dense twelve-item board | 3,194 | 33.962s | 0.827s |

These are measured optimizer timings, not a forecast of the entire build;
shared anchor preparation is outside the timed optimizer sections. Reproduce
the comparison with [the verification helper][Verify].

The full rebuild completed in 467.5 seconds: 1,770 champion scenarios and eight
composition contexts. All 1,664 boards and 416 level-nine upgrades passed
legality, score arithmetic and saved-interface checks; no item search hit its
improvement limit. Publication was verified over HTTP on 2026-09-07, with all
eight contexts ready and the new coverage labels served.

The 416 four-cost results now contain four or five four-cost units, compared
with six to eight previously. The default clumped board has four four-costs
and includes Gromp, Kog'Maw, Krug and Vi as supports. This demonstrates the
screening bias correction, not proof of live-game optimality. Regression and
oracle agreement establish consistency with the implemented model; the
remaining trait omissions above still limit the rankings.

[Snapshot]: 18.1d/metatft.json
[Client traits]: 18.1d/communitydragon.json
[Overrides]: 18.1d/overrides.json
[Audit]: 18.1d/audit.json
[Map]: trait-effects.json
[Board]: ../../../tft_board.py
[Resolver]: ../../../tft_comp_traits.py
[Reference]: ../../../tft_theory.py
[Theory]: ../../../tft_engine/src/theory.rs
[FX]: ../../../tft_engine/src/fx.rs
[Fight]: ../../../tft_engine/src/fight.rs
[Early]: ../../../tft_engine/src/drivers/a.rs
[Carries]: ../../../tft_engine/src/drivers/carries.rs
[Fighters]: ../../../tft_engine/src/drivers/fighters.rs
[Frontline]: ../../../tft_engine/src/drivers/frontline2.rs
[Frontline 1]: ../../../tft_engine/src/drivers/frontline.rs
[Late]: ../../../tft_engine/src/drivers/late.rs
[Team tests]: ../../../test_tft_trait_team.py
[Class tests]: ../../../test_tft_trait_fixes.py
[Timing tests]: ../../../test_tft_trait_timing.py
[Verify]: ../../../jobs/tft_theory_verify.py
[Riot overview]: https://teamfighttactics.leagueoflegends.com/en-sg/news/game-updates/enchanted-wilds-overview/
[Riot patch]: https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/teamfight-tactics-patch-18-1/
