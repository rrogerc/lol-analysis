# TFT snapshots and live patches

The active snapshot is the newest archived TFT patch, including hotfix
suffixes (`18.1 < 18.1b < 18.1d < 18.2`). Patch 18.1d is checked against
Riot's 18.1 article through the August 31 balance changes and September 1
bug-fix update. Riot labels those sections by date; D is the third
mid-patch update after the initial release.

## Sources

- `metatft.json` contains structured unit stats, roles, ability formulas,
  item curves and trait curves. The currently available Set 18 lookup is
  still marked PBE, generated August 16. It needs the documented live
  corrections in `overrides.json`.
- `communitydragon.json` archives the Set 18 portion of CommunityDragon's
  export for asset references and comparisons. The export fetched on
  September 5 was last modified August 29. It maps all 65 shop champions
  and 36 traits, but lacks usable ability calculations and alternate
  Adaptor forms, and retains several pre-hotfix stats. It cannot safely
  replace the full simulation input.
- `bins.json` contains CommunityDragon's per-unit timings. Missing bins
  are recorded as 404s; transport/server errors abort a refresh.
- `patchnotes.json` retains Riot's dated update headings, categories and
  parent item names instead of flattening away their context.
- `audit.json` maps published values to specific unit/form/item/trait
  fields and binds that review to hashes of the lookup, timings and patch-note
  document. It also records unresolved source ambiguities; passing the
  numeric checks is not a claim that every game mechanic is verified.
- `meta.json` records source URLs, upstream modification dates, hashes,
  fetch time and the time the patch-note checks passed.

## Refreshing

```sh
python3 lol.py tft refresh
```

The NixOS user timer `lol-tft-refresh.timer` runs `jobs/refresh-tft.sh` at
00:41, 06:41, 12:41 and 18:41 in the machine's local timezone. It catches
up after downtime. The declarative units live in
`~/Developer/dotfiles/nixos/configuration.nix`; user lingering keeps the
timer available without a login session. The job updates local data and
caches without committing or pushing to Git.

Each run checks Riot's latest patch article, including dated mid-patch
updates even when the base patch number is unchanged. It downloads the
sources into staging and compares them to the previously audited snapshot.
`tft_update.py` can reconcile supported numeric changes with an unambiguous
field mapping and matching old values. It checks changes in the underlying
definitions, preserves justified corrections, and records its evidence in
the new audit. Unknown mechanics, ambiguous values, changed timing data,
unsupported champions and source rollbacks stop the refresh for review.
This automates supported balance changes, not arbitrary engine changes or
new TFT sets.

All missing build scenarios are calculated from the staged snapshot before
publication. Failed downloads, validation or calculations leave the active
snapshot intact. Successful publication writes `.cache/tft/.dashboard-ready`;
the NixOS `lol-dashboard-reload.path` restarts the dashboard about ten seconds
later. Existing cache generations stay available during that handover.
Unchanged simulation inputs reuse the cache and do not restart the server.

The job records progress and results in `jobs/.state/refresh-tft.json`.
The TFT tab shows the last check, calculation progress, failures and changes
that need review. Open tabs check for a new revision every minute and reload
their current champion/scenario when it is ready. Logs:

```sh
journalctl --user -u lol-tft-refresh.service -n 50
systemctl --user list-timers lol-tft-refresh.timer
```

Review candidates are retained under `set18/.pending/<patch>/`. To resolve
one, inspect the changed sources and mechanics, add justified patch-specific
`audit.json`/`overrides.json`, then rerun `tft refresh`. Never update audit
hashes merely to pass validation. Manual `tft fetch --patch 18.1d --force`
still requires an existing matching audit; `tft check --patch 18.1d` reports
its numeric checks. Supplying a base version such as `18.1` resolves to its
current hotfix. Restart the dashboard after code changes so it loads the
new engine and cache generation.

## Verification

```sh
python3 -m unittest test_tft test_tft_tanks test_tft_data test_tft_update test_tft_refresh test_tft_ui
python3 jobs/tft_compare.py
```

Golden fixtures record their patch and concrete dummy setup. They were
deliberately regenerated from the compiled engine for patch 18.1d and the
tank threat/debuff/EHP model, retaining the first tank's fixed
3,000 HP / 70 armor / 70 MR benchmark; see
`golden/README.md` for the original port-verification provenance. Separate
regressions check the benchmark defenses and damage equations. The 18.1d
patch checks and regression tests verify live corrections, including Amumu's
healing percentage and Master Yi's AP-form resists. Artifacts/radiants are
archived and checked where their values can be established, but remain
outside the 35-item craftable build pool. Fishbones and Titanic Hydra have
unresolved source wording/field conflicts recorded in the audit.
Refresh regressions cover supported hotfixes, rejected ambiguous or mechanical
changes, source catch-up, staged calculation failures, rollback prevention,
overlapping jobs and cache continuity during publication.

## Tank benchmarks

Tanks compare three synthetic threats: mixed damage, physical attacks and
magic burst. Each has **three frontline blockers and two backline damage
dealers**. Slots are ordered nearest first. Local area effects and nearby
item auras reach the frontline; explicit global or distant targeting can
still reach the backline. Hecarim's riders choose the three nearest enemies
in both formations, so the frontliners take his stun while both carries
keep attacking. Scheduled spells that become ready during CC wait until it
ends, then resume their cadence without losing a cast or building a queue.

All profiles share an incoming damage budget calibrated by the Rust engine:
three median 2★ frontliners plus the 20-second average damage of two 2★,
three-item reference carries against one immortal 3,000-HP target with zero
resists and no traits. Aphelios uses Guinsoo's Rageblade, Kraken's Fury and
Infinity Edge; Ahri uses Spear of Shojin, Jeweled Gauntlet and Archangel's
Staff. On 18.1d this gives about **1,265 pre-mitigation DPS**, split into
172 from the frontline and 1,093 from the backline. Calibration is cached
by its complete resolved inputs and follows snapshot/item changes.

The presets vary the backline's damage split and timing: mixed is half
physical attacks and half staggered magic spells; physical is 85% attacks
and 15% physical spells; magic burst is 15% physical attacks and 85% magic
spells arriving together every four seconds. The frontline keeps its
median attack/spell cadence in every preset. The UI displays the resulting
whole-board physical/magic split and each line's DPS. These remain fixed
comparison profiles; they do not replay the reference carries' full
timelines, item ramping, movement or overtime.

The first frontliner's defenses stay at 3,000 HP / 70 armor / 70 MR; the
other two use tank medians and both backliners use non-tank medians. Carries
and fighters retain their existing three-target benchmark.

Tank fights assume continuous enemy Wound, Sunder and Shred from combat
start, using the corrected Morellonomicon, Last Whisper and Void Staff
rows: currently 33% less healing and 30% less armor/MR. Healing reduction is
applied before the missing-health cap; shields and max-health grants are
unaffected. Resistance reduction includes temporary bonuses and Gargoyle
stacks, and also applies to on-death bodies.

The primary score is hold time. A build or on-death body still holding at
60 seconds is shown as `60s+`, then tested again with twice the incoming
attack and spell damage. Surviving both tests is a tie. Other ties use
damage denied, then damage dealt. Opening physical/magic EHP reflects
actual combat-start defenses after Sunder/Shred and initial durability;
it excludes healing, shields and attack-only damage reduction. Effective
healing, shields consumed and casts explain the simulated result. Ally
health and survival are not simulated, so team utility is only tallied.

The dashboard adds the two additional threat variants only for tanks.
The mixed scenario retains its existing key; physical and magic keys add
`-physical` and `-magic`. There are 1,770 cached cells for this roster.
For example:

```sh
python3 lol.py tft sim Leona --threat magic --items 'Warmogs Armor' 'Gargoyle Stoneplate' 'Spirit Visage'
python3 lol.py tft top Amumu --threat mixed
```
