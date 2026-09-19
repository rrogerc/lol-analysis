# Installed Mac client data — September 8, 2026

Follow-up: this inspection covered legacy League WAD/BIN data. Riot's
documented Unreal migration and a newly identified Android package source
change the next extraction step; see [better sources and timing](sources-and-timing.md).
The placeholder findings below do not exhaust the newer Unreal data.

SSH access over Tailscale succeeded. Static data was read from both
`/Applications/League of Legends.app` and
`/Applications/League of Legends (PBE).app`. The extraction and comparison
ran on Linux. No game files, model mechanics or published rankings changed.

| Source | Installed game build | Set 18 archive | Common archive |
| --- | --- | ---: | ---: |
| Live | `16.17.8104348` | 108,307,192 bytes | 10,635,033 bytes |
| PBE | `16.18.8147109` | 108,866,389 bytes | 10,635,033 bytes |

These are client build identifiers, not an assertion that the static values
equal every balance hotfix in the model's `18.1d` snapshot. Live and PBE are
preserved separately. Every copied archive's SHA256 matches the remote file.

## Recovered data

- Each Set 18 archive contains 2,191 entries, including **464 actual BIN
  records**. Only 348 have `.bin` filenames; the other 116 are extensionless
  character records. File-extension filtering alone would miss them.
- Each Common archive contains **153 BIN records**. All **1,234 BIN records**
  across the two installations decoded successfully with native Rust tools.
- Fifteen Set 18 BIN records differ between the installations. Across the 65
  primary champions, all 235 decoded spell objects are identical; the 26
  changed fields are character-root stats or resource settings. All decoded
  Common BIN source files are byte-identical, despite different archive
  hashes. Archive hashes alone do not identify a mechanics change.
- The live shared `map22.bin` was also recovered and decoded. It contains
  set, shop, item and trait records, but the Set 18 block also retains old
  Set 10 script/UI metadata. Searches did not recover the model's named
  champion curve-table calculations from this export.
- Four static client JSON files were recovered: TFT champions, items,
  traits and team-planner champions. They include **36 Set 18 trait records**
  and **65 Set 18 planner champions**. Joining by character assets confirms
  all 65 planner codes agree with our existing copy feature.

The large map and client-resource archives were inspected through their
indexes and selected byte ranges. Approximately 3.14 MB of compressed
payload supplied the map record, its table and the four JSON files; copying
the complete multi-gigabyte archives was unnecessary. Those local range
files are explicitly labeled partial archives. Their index hashes, selected
offsets and compressed-payload hashes are retained as provenance.

## What this resolves

The installed files establish a concrete source version and give us
reproducible raw character, attack, spell, trait and roster metadata. They
also provide an independent check of the planner-code mappings and a way
to compare future installed patches.

They do **not** provide a complete, verified combat specification. Among
the 65 modeled primary champions, **63 main spell records still contain
generic `DataValue` / `OtherValue` placeholders**, in both live and PBE.
Alune and Kobuko have different fields but do not resolve their current
full mechanics. The five alternate-form asset names missing from the public
character exports were not found in either extracted Set 18/Common record
collection. This is a result for those collections, not proof that the
complete game cannot represent the forms elsewhere.

The installed primary character fields examined in the follow-up match the
public live export. PBE includes stat changes, some supporting already
reviewed patch overrides and others belonging to another build. They must
not be applied indiscriminately to the current model.

No complete executing spell logic was recovered from the records inspected.
Target selection, proc eligibility, overlapping buffs, exact recast timing
and the source conflicts in the [champion audit](../champion-audit.md) remain
open where the data does not settle them. The demonstrated model bugs can
still be addressed with the existing source-based regression cases.

## Evidence and repeatability

[Installed-source manifest](installed-source-manifest.json) records build
versions, archive hashes, tools, decoding checks and selected shared-file
provenance. [Character comparison](installed-character-findings.md)
records coverage, examined fields and remaining source limitations.

The full raw files, indexes, decoded text and range manifests are retained
under `.cache/tft/client-audit/2026-09-08/`, outside production snapshot
inputs. Tooling is staged under `/tmp/tft-game-tools/`; its manifest pins
the release/source versions and hash dictionaries needed to recreate it.

WAD decompression uses Rust `wadtools` 0.5.7. BIN conversion uses a pinned
Rust `ritobin-tools` build. Three representative BIN-to-text-to-BIN checks
reproduced the original bytes exactly, and a malformed BIN was rejected.
The converter emits Ritobin text rather than CommunityDragon-style JSON.
Unknown property hashes remain explicit; dictionary labels do not establish
what a field does in live combat.

This follow-up validates source access and extraction. It does not mark the
champion model complete, change mechanics, or trigger a leaderboard rebuild.
