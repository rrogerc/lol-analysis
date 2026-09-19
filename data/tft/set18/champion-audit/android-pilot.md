# Android gameplay extraction pilot

September 8, 2026. This pilot acquired real Set 18 Unreal gameplay assets for
Brambleback, Azir, Akali, both Nidalee forms, and Caitlyn as a ranged control.
The assets contain substantially more behavioral information than the legacy
League WAD/BIN placeholders. Acquisition and partial decoding succeeded;
custom gameplay property decoding remains incomplete. No production combat
change or leaderboard rebuild has been justified by this extraction yet.

## What was acquired

The official Riot launch manifest is
[AB4C45E0FD7F4250](https://teamfighttactics.secure.dyn.riotcdn.net/channels/public/releases/AB4C45E0FD7F4250.manifest),
version `rls-18.1.0.5358213.Set18.Shipping.live`. Its SHA256 is
`7ddbc1e57761067b6388c7abdc7a8b2a28c7c2b419be44eb27b47b3f29f15046`.
It lists 73 files, including a 3.73 GB gameplay container and separate
localization/video packages. The decoder checks RMAN 2.1, the compressed-body
length and the sum of each file's chunk sizes.

Only the necessary gameplay container ranges were fetched. The pilot extracted
124 non-UI assets, totaling 878,038 raw bytes, from 90 requested container
ranges. These include actor/form definitions, spell abilities, effects,
attack/cast/leap timelines, projectile definitions, and eight curve tables.
Every requested range is covered by retrieved RMAN chunks; all extracted
package sizes and SHA256 records were checked. The local `.ucas` is a sparse
partial file, not a complete game download. The JSON exporter restricts package
reads to the downloaded pilot assets.

The streaming manifest omits global ScriptObjects. Matching Android globals
were recovered with nested ZIP byte-range reads from the exact launch package,
version `18.1-5358213` / Android version code `8358213`, on
[APKPure](https://apkpure.net/tft-tactics/com.riotgames.league.teamfighttactics/download/18.1-5358213).
Both globals passed ZIP CRC32 checks and have recorded SHA256 values. This
mirror acquisition does not independently verify the complete APK signature;
the full APK was not downloaded or executed. About 5 MB including indexes was
needed. The Android globals resolve 71,944 ScriptObjects.

A separately retrieved official Windows package from the exact same source
revision helped diagnose the dependency. Brambleback's spell and cast-timeline
packages are byte-identical between the two platforms. Their global containers
are different, so final Android decoding uses the Android globals.

## What can be decoded reliably

The inspected packages have `PKG_UnversionedProperties` and no embedded package
version information. Object names can be resolved, but custom property values
need their matching schema. A successful extraction or a readable string pool
does not establish a gameplay rule.

A limited decoder uses only standard Unreal curve schemas from the
[UAssetAPI fixtures](https://github.com/atenfyr/UAssetAPI/tree/3228c1e86261aa08131f7ec0ff1a395f5d0b2a84/UAssetAPI.Tests/TestAssets).
The selected native curve schemas and enums match between the UE 5.5 and 5.6
fixtures. No TFT property layouts were invented. The parser's UE 5.6 selection
is a compatibility setting, not a claim that the unversioned assets declare an
exact engine minor version.

All eight curve tables decoded successfully, with 49 rows in total. Each
deserialization ended exactly at its declared export boundary, with no
diagnostics. The [preserved curve evidence](android-curves.json) contains
values, asset hashes and those boundary checks. These exports include
Brambleback's twelve numeric rows, Nidalee's `ASToCastTime` RichCurve and
Azir's `DashSpeed` SimpleCurve. Their
interpolation and extrapolation modes are preserved, including the distinction
between constant star-level rows and linear timing curves.

For Brambleback's two-star inputs, eleven of the twelve decoded rows match the
frozen model. Launch `AutoAttackDamage` is 172.5 while the pinned 18.1d model
uses 165. This specific difference is unresolved: the model's source is an
August 16 PBE lookup with selected later corrections, and the patch label does
not prove that every row is newer than the Android launch data. No published
Brambleback change was found in the archived patch-note changes. Preserve
reviewed corrections and investigate unmatched values before adoption. The
decoded `Duration` row is 8 and `FrenzyADPercent` is 0.8 at
two stars; these values alone do not establish the buff's event or mana-lock
ownership.

The comparison of all available star/form rows found 86 matches across 102
comparisons. These include repeated rows across Akali forms, so they are not
102 unique source rows. Differences include unresolved source-version conflicts
and flattened unused row representations. They must not all be attributed to
later hotfixes. In particular, Akali's three-star basic
AD actually uses `baseAd` 90 (AD form) / 67.5 (AP form), which agrees with the
decoded linear curves even though the generic row dictionary holds 60 / 45.
The comparison does not establish a basic-attack damage bug.

Limited native Blueprint decoding also recovered compiled function statements
for Brambleback and Akali. Brambleback's graph calls
`BP_ApplyGameplayEffectToOwner` and stores a result in `ManaLockHandle`;
Akali's graph checks `IsActorDead` and conditionally applies
`GE_18_Akali_ManaGain`. These are typed code observations, stronger than string
co-occurrence. The custom class defaults, effect components and timeline
semantics are still incomplete, so this is not a full behavioral decode.

## Why the timing changes remain gated

| Mechanic | Evidence recovered | Still needed before changing combat |
| --- | --- | --- |
| Brambleback Frenzy | Decoded duration/AD rows and compiled effect-application/mana-handle statements. | Typed effect properties and the complete consuming graph must establish when the lock starts, what owns it and what ends it. An eight-second buff is not by itself proof of an eight-second mana lock. |
| Nidalee cast scaling | A decoded `ASToCastTime` curve plus separate human/AP and cougar/AD assets. | Identify the active consumer, input units/transforms and whether the result controls animation rate, effect release or action recovery. |
| Azir dash scaling | A decoded `DashSpeed` curve and soldier attack/dash assets. | Identify the consumer and whether the curve describes a duration, multiplier or speed. |
| Attack and leap timing | Dedicated pilot attack/cast/leap timeline assets are intact. | Decode the TFT timeline structures and their event bindings. Asset names or untyped float offsets cannot establish timing rules. |
| Akali repeat casts | Intact spell/kunai/landing assets for the existing AD/AP form definitions. | Decode target identity, hit events, kill condition and repeat scheduling before replacing the current instantaneous repeat loop. |

Brambleback's final bounded probe resolves all 49 direct spell script references
and all five imported packages. Its Frenzy effect resolves all 24 direct script
references and all three imports. The two shared mana assets were already
within the retrieved RMAN chunks and could be enabled without another download.
Five Brambleback functions decode without warnings, including explicit
`GE_ManaLock_C` application, storage in `ManaLockHandle`, and removal through
`RemoveActiveGameplayEffect`. Missing files are no longer this probe's barrier;
the custom property layouts and their timing meanings remain the barrier.

The current Rust `cast_time` hook simultaneously changes action gating, effect
timing and generic mana lock. Passing an unverified curve value into that hook
could introduce several errors at once. The next implementation should separate
these responsibilities only after the relevant action/consumer is decoded.

The remaining barrier includes the custom native types `TFTGameplayEffect`,
`TFTAbilitiesEffectComponent`, `TFTChronoEffectComponent` and
`TFTRestrictToPhaseGameplayEffectComponent`. Blank Unreal fixture mappings do
not define these TFT types. A verified layout for the relevant types or a
structured export from a decoder that understands them is required; a complete
roster mapping is not necessary if the specific mechanic can be decoded and
validated independently.

## Reproducible baseline and artifacts

The pilot froze 124 exact current-model cases: 48 finite, 48 matched immortal,
and 28 protected immortal controls. They cover legal loadouts, relevant capped
stars, bare/high traits, spread/clump conditions and the reported triple-Guinsoo
Brambleback build. The native binary, exact specs, source hashes and traces are
saved. All 124 cases and 76 response measurements replayed without differences
under the frozen and current engines. This verifies the comparison baseline,
not agreement with the live game.

Local evidence is under `.cache/tft/android-pilot/`:

- `source-manifest.json`, `manifest-files.json`, `raw-manifest.json` and
  `acquisition-checks.json`: source identity, chunk map and integrity checks.
- `archives/*.source-*.json`: exact retrieved CDN ranges and compressed hashes.
- `android-core/README.md`, `range_extract.py` and `extraction-manifest.json`:
  Android globals acquisition, reproduction and provenance limits.
- `package-summary-findings.json`, `cue-*.json` and corresponding logs:
  raw package flags, successful native exports and explicit decoding failures.
- `baseline/README.md`, `capture.py`, `replay.py` and `cases/`: frozen model
  corpus and instructions for before/after comparisons.
- `compare_curves.py`: compares decoded star-indexed rows with frozen native
  inputs without adopting the source values.
- `final-verification.json`: final evidence checks and production-source hash
  comparison against the frozen baseline.
- `tooling/rman/`: preserved acquisition/helper source. The RMAN dependency is
  [cdragon-rs](https://github.com/CommunityDragon/cdragon-rs) commit
  `b8ae9dbcec8e97ff189d26c9b3bf891bbd7166af`.
- `tooling/unreal/`: pinned decoder source, dependency locks, native curve
  schemas, tool provenance and validated reproduction commands.

Archive parsing/extraction uses native Rust tools; JSON decoding uses pinned
CUE4Parse. Production simulations and numerical searches remain in Rust.
No full enumeration, saved-generation publication or game installation was
performed for this pilot.
