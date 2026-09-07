# Golden fixtures — compiled TFT benchmark, Set 18 patch 18.1d

`fights.json` and `cells.json` pin the **exact** output of the compiled
Rust/PyO3 engine. They are deliberately regenerated on 2026-09-06 for
Brambleback's personal armor ignore, using the audited 18.1d snapshot.
During Frenzy, his armor ignore applies to the armor remaining after Sunder;
it does not reduce armor for teammates. Before regeneration, the same
recorded inputs changed 52 of 7,670 fights, all Brambleback, and his 12 ranked
cells. The other champions' standalone results were unchanged. Symmetric
composition combat has separate regressions in `test_tft_symmetric.py` and
`test_tft_symmetric_regressions.py`.

Scuttlecrab's confirmed burrow attack lock is retained.
The heal and durability start at ability land; attacks and recasts remain
blocked for the resolved burrow duration. The existing one-second mana lock
is retained. Shared composition fights have separate deterministic regressions
in `test_tft_team_engine.py`; their new API preserves other standalone results.

Murkwolf's previously corrected leap is retained. His AD and AP
contributions each apply once, following the archived ability footer. The
corrected base-stat card values also lower the non-tank dummy's median
ability damage from 335 to 318. These are deliberate changes to both
Murkwolf's damage and the derived benchmark; see `../README.md`.

Azir's adopted six-command mana lock is retained.
Attack mana and regeneration remain blocked until the sixth command ends,
then resume without an extra delay. The first tank's higher base defenses
are retained. Permanent team-supplied Sunder and Shred remain active on every
carry/fighter target. Both are currently 30% and apply once;
matching item or native effects cannot stack or prolong a stronger timed
effect. The first enemy tank now has **3,000 HP, 110 base armor and
110 base MR**, reduced to **77 armor / 77 MR** in damage tests. The other
carry/fighter targets' 45/45 and 40/40 become 31.5/31.5 and 28/28.

The protected-backline tank model, itemized reference damage calibration,
nearest-enemy targeting and delayed enemy casts are retained. Tanks face
two more frontliners at 1,800 HP / 45 armor / 45 MR
and two backliners at 1,440 HP / 40 armor / 40 MR. All five remain alive.
The three tank profiles have equal average damage budgets, calibrated
against itemized carries, with most damage coming from protected backliners.
Tank survival tests use the same first tank's new base defenses without
outgoing team resistance reduction; fighter offense keeps the median
attack/mana model. See `../README.md`.

Earlier generations fixed omnivamp against immortal targets, reported
opening stats after driver initialization, and stopped at the exact requested
fight horizon. Those changes, Wound/Sunder/Shred, opening EHP and the
double-pressure comparison remain covered. Rule tests independently check
damage, healing, EHP, targeting and timing before fixtures are regenerated.

The original fixtures pinned the pre-Rust Python engine on patch 18.1.
Each new file preserves that artifact's SHA-256 and original provenance
under `provenance.previousFixture`. This rebaseline records the compiled
engine's results; it does not claim the changed benchmark reproduces the
old Python outputs.

Provenance now includes the exact legacy and three tank dummy specifications, snapshot/effect
hashes, compiled engine source hash, generator hash, change reason, and
whether the generating checkout had uncommitted changes. `commit` records
the checkout's HEAD; `worktreeDirty` makes clear when HEAD alone does not
identify the generating sources. The source and input hashes identify
those inputs directly.

- `fights.json`: 7,670 cases, one per (unit, scenario cell, build) — the cell's two
  best cached builds, two drawn with a fixed seed from every legal 3-item
  multiset, and the empty build in the bare trait context — with the opening
  sheet numbers (`sheet`) and the unrounded `simulate` result (`result`).
  Floats are stored as Python `repr`, which round-trips exactly; a
  non-finite value would be the string `"inf"` / `"-inf"` / `"nan"` (none
  occur today).
- `cells.json`: the top 20 rows of all 1,770 cells as the dashboard shows them
  (rounded), plus the build count and the driver's name, so the
  enumeration's order and the row formatting are pinned too.

`test_tft.TestGolden` and `jobs/tft_compare.py` replay the recorded set,
patch and dummy specification. The tests also require that the archived
dummy specification matches the current benchmark. Every fight is compared
bit for bit (an int and a float of equal value count as the same number);
the default test run recomputes a sample of cells. Run
`TFT_GOLDEN_ALL=1 python3 -m unittest test_tft` to recompute all 1,770.

After an authorized deliberate model change and a complete warm cache,
regenerate with `python3 jobs/gen_tft_golden.py --reason 'Describe the model change'`.
**A deliberate model change must regenerate these files in the same
commit.** Routine data refreshes keep these fixtures anchored to their
recorded patch; data corrections have separate regression tests in
`test_tft_data.py`.
