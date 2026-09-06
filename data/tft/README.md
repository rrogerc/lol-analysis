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
python3 -m unittest test_tft test_tft_data test_tft_update test_tft_refresh test_tft_ui
python3 jobs/tft_compare.py
```

Golden fixtures record their patch and concrete dummy setup. They were
deliberately regenerated from the compiled engine for patch 18.1d and the
first tank's fixed 3,000 HP / 70 armor / 70 MR benchmark; see
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
