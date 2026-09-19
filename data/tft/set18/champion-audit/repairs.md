# Champion model repairs — September 8, 2026

All **65 champions / 70 base-and-Adaptor forms** were reviewed. Source-supported
repairs were implemented in **18 champion models**, together with shared targeting,
sleep and resistance-reduction changes. Every model still has declared limitations;
this is not a claim that all champions reproduce live combat.

The [structured roster](repairs.json) records each repair and remaining gap. The
[earlier audit](../champion-audit.md) and [Android pilot](android-pilot.md) remain
historical evidence. No unverified Android coefficient or timing curve was adopted.

Known support omissions remain: Rakan's ally attack speed, Shen's and Taric's
ally attack bonuses, several Alpha buffs, and summoned entities are incomplete.
These need recipient-aware ally effects and resolved pairing, decay, overlap or
trigger rules. They are recorded as missing mechanics, separately from uncertain
source values. Theory still gives ally healing and shields no team-EHP credit.

## Changes that affect rankings

- Independent nearest-N targeting now works in spread formations. Both theoretical
  layouts contain three targets; adjacency no longer changes enemy population.
- Primary damage, secondary splash, persistent bursts and returning projectiles keep
  their original recipients or centers when an earlier component kills someone.
- Lillia uses the damage threshold, wakes once, preserves other stuns and receives
  credit for wake damage triggered by allies, items and persistent damage. Only
  spells already waiting to cast resume on waking; future timers remain intact,
  and refreshed sleep uses the current expiry.
- Fiddlesticks and Gnar share applied flat resistance reductions with theoretical
  teammates. Synchronization applies each new delta once.
- Pebbles pays for actual elapsed channel time, including partial intervals. Kha’Zix
  isolation mana receives all-source multipliers. Sivir gets kill extensions from
  the initial blade and retains prior-victim identity across bounces.
- Elder’s existing first-flight protection window starts at cast start and ends at
  landing. The duration and untargetability representation remain approximations.

## Roster decisions

“Repaired” means at least one supported issue was corrected. “Reviewed” means the
current implementation was retained because no additional repair was justified
by available evidence. Neither label means fully verified.

| Champion | Decision | Specific corrections |
| --- | --- | --- |
| Ahri | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Akali | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Alistar | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Alune | Repaired | Independent nearest-three targeting no longer reduces to local AoE coverage in spread. Nine shards keep their existing total and are distributed among the selected living targets. |
| Amumu | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Aphelios | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Ashe | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Azir | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Brambleback | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Caitlyn | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Camille | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Cassiopeia | Repaired | The secondary poison selects an independent unpoisoned enemy in either layout. Expired poison no longer permanently excludes an enemy from secondary selection. |
| Cinderling | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Diana | Repaired | Orbs cannot leave the modeled nearby area when the original recipients are exhausted. |
| Draven | Repaired | Outbound and returning axes retain the same original recipients after lethal hits. |
| Elder Dragon | Repaired | Splash recipients are captured before a lethal primary auto.; First-flight protection starts at cast start and ends at landing; landing effects retain their existing event time. |
| Elise | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Ezreal | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Fiddlesticks | Repaired | Independent nearest-target selection and sharing applied flat MR reduction with theoretical teammates. |
| Gnar | Repaired | Lethal throws preserve their original secondary recipients.; Transformation stuns the same original area it damaged, without switching to an isolated survivor. |
| Gromp | Repaired | Both forms capture cloud or splash recipients before the primary hit, preventing a lethal hit from switching the area to an isolated survivor. |
| Hecarim | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Ivern | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Karma | Repaired | Delayed bursts retain their original tether center after target changes or death; the existing delayed-burst policy is retained. |
| Kayle | Repaired | Freeze secondary wave recipients before the primary attack components. Primary death no longer cancels the wave or mistakenly excludes the next primary. Existing spread/clump coverage and star unlocks remain unchanged. |
| Kennen | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Kha'Zix | Repaired | Isolation mana bypasses the mana lock while receiving all-source mana multipliers, including Adaptive Helm. |
| Kobuko | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Kog'Maw | Repaired | Both equipped forms select current target plus one additional living enemy independently of AoE coverage. AP damage over time uses those same two identities. |
| Krug | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| LeBlanc | Repaired | Splash recipients are captured before the primary hit so a lethal hit cannot skip a valid secondary. |
| Leona | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Lillia | Repaired | Damage-threshold Sleep and one-shot wake damage across standalone, shared-target, symmetric and theory paths; other stuns remain intact. |
| Lux | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Malphite | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Mama Beak | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Maokai | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Master Yi | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Morgana | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Murkwolf | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Nidalee | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Ornn | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Pebbles | Repaired | Channel damage settles actual elapsed time, including the first and final partial intervals. |
| Rakan | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Rammus | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Rek'Sai | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Rengar | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Scuttlecrab | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Sejuani | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Sentinel | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Sett | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Shen | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Sivir | Repaired | Kills from the initial blade grant extra bounces. Bounces retain previous-victim identity, including after death; a projectile may reach the sole survivor from a dead previous target. |
| Soraka | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Taric | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Teemo | Repaired | Each mushroom cluster independently selects up to three living enemies regardless of clump/nearby coverage. The giant mushroom remains a primary-target hit. |
| Tristana | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Varus | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Veigar | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Vi | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Warwick | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Xayah | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Yorick | Reviewed | Existing model retained; remaining gaps are in the structured roster. |
| Yunara | Repaired | Secondary recipients are captured before the primary hit so a lethal hit cannot exclude the new primary. |
| Zyra | Reviewed | Existing model retained; remaining gaps are in the structured roster. |

## Verification and source boundaries

In the saved two-star, no-trait, spread damage benchmark, independently optimized
legal three-item builds produce these before/after clear times:

| Champion | Previous model | Repaired model |
| --- | ---: | ---: |
| Teemo | Did not clear within 20 s | 17.25 s |
| Cassiopeia | Did not clear within 20 s | 19.50 s |
| Elder Dragon | 12.25 s | 14.67 s |
| Alune | 8.50 s | 10.00 s |

Independent targeting restores missing output for Teemo and Cassiopeia. Elder
loses unintended splash into an isolated survivor; Alune distributes its shards
among the selected enemies. These are results under the declared benchmark,
not measured game fights or a general champion-strength ranking.

The new regressions use explicit damage amounts, target identities and timestamps.
They reproduce the old defects, then pass on the corrected engine. A comparison
against all 7,670 previous standalone fights changes 585 results, only for repaired
champions. Kha’Zix’s refund correction is covered by a dedicated multiplier test even
though the sampled historical loadouts do not expose it.

The exact input/target geometry, generic cast assumptions and pressure model remain
visible. Initial butterfly damage is applied before Sleep; reapplication refreshes
one sleep and threshold. Shared wake hits settle at the originating action boundary
at the same timestamp. These ordering choices are not live-verified. Missing movement,
exact animation/projectile timing, ally-stat buffs, some proc rules and permanent
histories still prevent calling every champion accurate.

Final engine `9640b491f083` passes **817 Python tests (one existing skip)** and
all eight Rust unit tests. The Python run replays every one of the 7,670 current
golden fights and all 1,770 ranked cells; 62 new regression methods cover the
repairs. The conditional unique-item test is skipped because the craftable pool
contains no unique item; native synthetic tests cover uniqueness separately.

The complete rebuild took **4,083.247 seconds (68 minutes 3 seconds)** for 1,770
champion scenarios, eight composition contexts and saved HTTP responses. The
generated-data UI checks validated 1,664 cores and 416 level-nine upgrades, with
2,080 exact Team Planner roster round trips. Both four-cost contexts contain the
upgrades. Generation `g-e855e95ca90b` was published and observed through the running
dashboard's champion and composition metadata/status endpoints, all ready.

Full hashes, optimizer comparisons, test commands, context counts and publication
observations are recorded in the verification section of [repairs.json](repairs.json).
