# Champion mechanics audit — Set 18, patch 18.1d

Audited September 8, 2026. **All 65 modeled shop champions were reviewed,
including both forms of the five Adaptors. The model is not yet accurate
enough to call every champion verified.** This review found reproducible
implementation discrepancies, missing team contributions, and mechanics
whose sources do not establish the real behavior.

This is a source and code audit with controlled Rust observations. It is not
a comparison against recorded live fights. The original audit changed no
production mechanics or rankings. The subsequent
[champion model repairs](champion-audit/repairs.md) implement supported corrections
in 18 champions and shared targeting, sleep and resistance-reduction behavior;
that report records the current decisions and remaining gaps for all 65 champions.

The [source and timing follow-up](champion-audit/sources-and-timing.md)
identifies the newer Unreal/Android data pipeline and outlines how movement,
attack windups, cast recovery and target changes should enter the model.

The [Android extraction pilot](champion-audit/android-pilot.md) acquired 124
real Unreal gameplay assets and decoded standard numeric curves. Custom spell
and movement property layouts still require additional decoding; the pilot
does not yet justify new production timing rules.

## What was reviewed

Each driver was checked against the pinned tooltip, curve rows, calculation
expressions, overrides and existing tests: damage scaling; forms and relevant
star levels; attack/cast/mana timing; target selection and damage distribution;
passives and stacks; healing, shields and control; summons; and relevant
item/trait interactions. The review also checks where these mechanics reach
the finite leaderboard, theoretical composition scorer and shared combat.

This does not replace a fresh audit of every item and trait. The existing
[36-trait audit](trait-audit.md) covers those trait limitations separately.
Higher-star effects outside the dashboard's legal stars, economy, prior-round
history and unimplemented Lux variants are identified as boundaries, not
silently counted as modeled mechanics.

| Driver group | Champions | Detailed review |
| --- | ---: | --- |
| `carries.rs` | 15, including both Gromp forms | [Carry spell mechanics](champion-audit/carries.md) |
| `a.rs` and `late.rs` | 19, including both Akali, Kog'Maw and Nidalee forms | [Other champions and Adaptors](champion-audit/a-late.md) |
| `frontline.rs` and `frontline2.rs` | 20 | [Frontline and support mechanics](champion-audit/frontline.md) |
| `fighters.rs` | 11, including both Master Yi forms | [Fighter mechanics](champion-audit/fighters.md) |

The union of these lists was checked against the snapshot: 65 unique champion
APIs, no missing champions, and all five AD/AP pairs. Assessment labels describe
individual mechanics. A source-supported formula is not a claim that the
champion's targeting, procs or timing are verified in the game.

## Findings that matter most

| Finding | Observed evidence | Consequence |
| --- | --- | --- |
| Independent targets are confused with adjacent targets | Fiddlesticks drains one enemy in spread and three in clump, despite specifying three nearest enemies. Cassiopeia, Teemo, Kog'Maw and Alune have related selection problems. | Multi-target champions lose value for reasons unrelated to their actual targeting rules. |
| A spell can lose its original target after a kill | LeBlanc, Yunara and Gnar can skip a surviving secondary after the primary dies. Elder's lethal auto can splash the next isolated enemy in spread. Gromp and Draven also recompute later recipients after a lethal hit. | Finite clear times can gain or lose damage through unintended retargeting. |
| Lillia's wake-up condition is bypassed | Her primary immediately takes wake-up damage after only 100.5 damage, below the stated 1,000 threshold. Raising the threshold to one billion leaves the trace identical. | Extra damage is awarded unconditionally; primary sleep is lost, while secondary sleep never responds to subsequent damage. |
| Explicit damage-based healing uses a different damage amount | A synthetic 500-damage Warwick bite into 1 remaining HP records 1 damage but 100 healing. AP Yi similarly heals 175 from a hit that removes 1 HP. | Standalone explicit heals use unclipped damage, while its damage ledger, omnivamp and bridged damage return use clipped damage. This inconsistency affects finite tests; immortal composition probes have no lethal overkill. Live overkill-heal rules still need confirmation. |
| Timing and resource events are inconsistent | Elder's protection starts at landing, after the supposed flight. Pebbles pays only 2.75 seconds of a full 2.857-second channel. Kha'Zix's isolation mana bypasses Adaptive Helm's multiplier. | Survival, damage and cast cadence can be wrong even with correct base coefficients. |
| Team support is missing | Rakan's ally AS, Shen/Taric's ally attack buffs, Lux's ally mana and several Alpha effects are absent. Fiddlesticks' flat MR strip does not help other actors in theory. | Self-contained damage and sustain are favored over champions whose value comes through teammates. This is a structural source of bias. |

Other important unresolved cases include Yorick's AD tooltip icon versus an
AP-scaled calculation; Ivern's unresolved shield multiplier; Amumu's healing
footer versus calculation expression; Rammus/Malphite recast and shield-break
behavior; Akali's instantaneous kill chains; empowered-attack crit/mana rules;
Zyra's assumed plant cadence; and Veigar/Maokai's absent prior-round stacks.
The detailed reviews separate these from demonstrated implementation errors.

## What the available game data establishes

The snapshot has a MetaTFT lookup stamped PBE, generated August 16, with
reviewed patch 18.1d corrections. Its `verifiedAt` field means the published
patch-note checks passed; it does not mean every ability was validated.

Our ingestion keeps only `castTime`, `attackWindup` and `missileSpeed` from
character BIN exports (`tft.py:417`). Only `castTime` reaches the combat spec;
attack windup and projectile travel are not implemented. The 63 resolved
generic cast times are all 0.25 seconds; Alune and Kobuko fall back to the
same value unless their driver supplies another duration. Explicit channel
drivers can override that generic value.

For this audit, all 79 asset URLs referenced by the current roster were read
from the full CommunityDragon character exports into a temporary directory:

- 74 returned HTTP 200, including an export for every primary champion.
- Five returned HTTP 404: Akali AP, Master Yi AP, Gromp AD, Kog'Maw AP and
  Nidalee AD. This establishes absence at those URLs, not absence from the
  installed game.
- 63 of the 65 main spells contain only generic `DataValue` / `OtherValue`
  rows. Alune and Kobuko contain different fields that also do not establish
  their full current kits. For example, the
  [Akali AD export](https://raw.communitydragon.org/latest/game/characters/da_18_akali_ad.cdtb.bin.json)
  has a script name and generic spell data, not its executing spell logic.
- Re-distilling those files reproduces all cached timing fields exactly.
  Fetching them again therefore does not by itself repair the model.
- Primary base-stat comparisons found three stale values already corrected
  by the pinned Riot patch overrides: Draven AS, Elder AD and Yi resists.
  Gromp's apparent AD difference disappears after comparing the correct AP
  form. These findings do not justify replacing the reviewed stats.

The [source manifest](champion-audit/source-manifest.json) preserves URLs,
HTTP status, SHA256, modification dates, metadata and these comparisons.
`latest` is mutable and is not assumed to equal the installed client or the
pinned patch. The separate archived compatibility exports consulted during
the frontline review have the same completeness limitation.

The initial audit had no accessible installation. A subsequent
[Mac client-data inspection](champion-audit/installed-client.md) connected
over Tailscale and recovered live and PBE archives. It confirms useful source
metadata and the planner codes, but the inspected primary spell records
retain the same placeholder-data limitations. The follow-up preserves its
own build versions and hashes; the original audit provenance remains intact.

An installation can provide a patch-specific source. A read-only extraction
should retain the client version, WAD file hashes, full BIN records, relevant
set tables and unresolved hashed fields. The Rust
[wadtools project](https://github.com/LeagueToolkit/wadtools) supports listing
and extracting WAD contents; [ritobin](https://github.com/moonshadow565/ritobin)
can convert BIN records. Extracted data should be compared before adoption.
Some targeting, recast and proc semantics may still require controlled
in-game observations.

## Recommended repair order

1. **Fix shared, demonstrated accounting and event problems.** Preserve a
   spell's target/area identity across lethal hits, settle partial channels,
   honor explicit conditions, and route resource gains through a consistent
   API. Specify attempted, post-mitigation and effective damage separately
   before changing damage-based healing. Give each correction a small
   source-based regression case.
2. **Separate enemy population from area coverage.** The theoretical scorer
   currently constructs one target for spread and three for clump
   (`tft_theory.py:64`). Even a correct nearest-three selector cannot choose
   three from a one-target population. Represent independent nearest-N
   targeting separately from adjacency, lines, fixed projectile counts and
   split totals. Keep coverage assumptions explicit without trying to
   reproduce an entire lobby or adding a universal AoE multiplier.
3. **Implement support with actual recipients.** Ally buffs must alter the
   chosen ally's attacks/casts; heals and shields need missing HP, expiry and
   absorption before earning EHP. The current theory deliberately records
   ally healing/shielding as potential with no team-EHP credit. Fixing this
   should use recipient state, not flat support or synergy points.
4. **Resolve high-impact unknowns from stronger evidence.** Start with
   Adaptor form/attack rules, Akali recast timing, overlapping self buffs,
   summon cadence/inheritance and permanent-stack inputs for capped boards.
   Recover source formulas where possible; observe only the remaining
   disputed interactions in game.
5. **Re-evaluate rankings after mechanics are corrected.** Preserve the EHP
   and damage framework while establishing trustworthy champion inputs.
   Changing the 20-second leaderboard cutoff or adding a role penalty would
   not repair these mechanics. Benchmark the corrected Rust engine before a
   complete enumeration and publication.

## Verification and retained evidence

- The broad native driver/calculation/body/mana/data/form checks and relevant
  Brambleback, Murkwolf and execute suites passed **89 tests**. The all-driver
  case exercises legal stars, two geometries, trait contexts and selected
  offensive/defensive builds; it is a numerical smoke test, not a live oracle.
- Frontline checks passed 54 tests; the other champion/form checks passed
  41; the carry review ran 11 selected tests. These suites overlap, so those
  counts must not be added into a claimed unique-test total.
- Small synthetic Rust traces reproduce the discrepancies above. Earlier
  AoE probes were reused. No item enumeration, full refresh or live-game
  capture was performed for this audit.
- [Structured findings](champion-audit/findings.json) retain per-champion
  assessments, source locations, checks, gaps and hashes of the audited code
  and snapshot. [Observations](champion-audit/observations.json) retain native
  outputs and available probe constructors as inert text. Exact coefficients
  remain in the referenced, hash-pinned snapshot rather than being copied
  into another source of truth.

Native engine: `75b1dc3aed55577eb967d10f3ea9904790ab01f61cbf917be54ef1e01f0b9b45`.
Snapshot input: `1c7e857debea22df4f16936771bd1ea07800f59bdbd3f05a0574434c9e18067c`.
Baseline revision: `515a7a167347acb61148016e4966ffc0be538785e670c05ad441113d752b3a13`.
