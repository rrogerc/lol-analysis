# Golden fixtures — compiled TFT benchmark, Set 18 patch 18.1d

`fights.json` and `cells.json` pin the **exact** output of the compiled
Rust/PyO3 engine. They were deliberately regenerated on 2026-09-05 after
changing the first enemy tank to **3,000
HP, 70 armor and 70 MR**, using the audited 18.1d snapshot. The second tank
remains at 1,800 HP / 45 armor / 45 MR, and the rear non-tank at 1,440 HP /
40 armor / 40 MR: 6,240 total HP. Enemy offense and pressure rules are
unchanged.

The original fixtures pinned the pre-Rust Python engine on patch 18.1.
Each new file preserves that artifact's SHA-256 and original provenance
under `provenance.previousFixture`. This rebaseline records the compiled
engine's results; it does not claim the changed benchmark reproduces the
old Python outputs.

Provenance now includes the exact dummy specification, snapshot/effect
hashes, compiled engine source hash, generator hash, change reason, and
whether the generating checkout had uncommitted changes. `commit` records
the checkout's HEAD; `worktreeDirty` makes clear when HEAD alone does not
identify the generating sources. The source and input hashes identify
those inputs directly.

- `fights.json`: 4,446 cases, one per (unit, scenario cell, build) — the cell's two
  best cached builds, two drawn with a fixed seed from every legal 3-item
  multiset, and the empty build in the bare trait context — with the opening
  sheet numbers (`sheet`) and the unrounded `simulate` result (`result`).
  Floats are stored as Python `repr`, which round-trips exactly; a
  non-finite value would be the string `"inf"` / `"-inf"` / `"nan"` (none
  occur today).
- `cells.json`: the top 20 rows of all 1,026 cells as the dashboard shows them
  (rounded), plus the build count and the driver's name, so the
  enumeration's order and the row formatting are pinned too.

`test_tft.TestGolden` and `jobs/tft_compare.py` replay the recorded set,
patch and dummy specification. The tests also require that the archived
dummy specification matches the current benchmark. Every fight is compared
bit for bit (an int and a float of equal value count as the same number);
the default test run recomputes a sample of cells. Run
`TFT_GOLDEN_ALL=1 python3 -m unittest test_tft` to recompute all 1,026.

After an authorized deliberate model change and a complete warm cache,
regenerate with `python3 jobs/gen_tft_golden.py --reason 'Describe the model change'`.
**A deliberate model change must regenerate these files in the same
commit.** Routine data refreshes keep these fixtures anchored to their
recorded patch; data corrections have separate regression tests in
`test_tft_data.py`.
