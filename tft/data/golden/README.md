# Golden fixtures — compiled TFT benchmark, Set 18 patch 18.1d

`fights.json` and `cells.json` pin the **exact** output of the compiled
Rust/PyO3 engine. The latest generation uses engine `a0cc14674fde`, adding the
user-accepted shared 0.5-second engagement estimate to short-range finite
damage tests. Attack cooldowns overlap movement; melee cross-target chains
resume at arrival. Zero-delay replay preserves all 7,670 preceding fight
cases exactly. The regenerated 1,770 ranked cells contain 216 changes and
1,554 identical results. Independent timing and continuation regressions are
in `test_tft_movement.py` and `test_tft_movement_chains.py`; see the
[movement record](../set18/melee-movement.md) for assumptions and sensitivity.
The complete 918-test TFT suite passes with one existing skip, plus eight
Rust tests. These fixtures do not publish a new dashboard generation.

The preceding generation uses engine `cfe721d84f2e`, adopting
one additive percentage-HP pool for ordinary items and traits. Before
regeneration, all 7,670 previous fight inputs were replayed: all 6,053 cases
with at most one percentage-HP bonus stayed identical; the 1,617 cases with
multiple bonuses changed. The rebuilt ranked cells contain 1,079 changes and
691 identical results out of 1,770. Independent hand-calculated opening and
timed-growth regressions are in `test_tft_hp_stacking.py`; see
[the HP stacking record](../set18/hp-stacking.md) for the adopted formula and
its remaining live-game verification limit.

The preceding generation used engine `6bef32c46ca7`, adding
persistent incoming source targets to composition scoring. All 7,670 previous
standalone fights and all 1,770 ranked cells remain exactly unchanged after
regeneration; the fixtures record the new compiled source identity. Targeting
has separate regressions in `test_tft_persistent_targets.py`; see
[the pressure model report](../set18/pressure-targeting.md).

The preceding generation used engine `4116bc4251fd`, adding
optional Primal Bear/Turtle effects, shared Spellweaver casts and Solar
conversion. All 7,670 previous standalone fights and the ranked-cell checks
remain unchanged; these additions affect composition contexts or explicitly
selected effects. [Trait models](../set18/trait-models.md) records their separate
regressions and the removal of the composition melee-carry limit.

The preceding generation incorporates the
[champion model repairs](../set18/champion-audit/repairs.md): independent target
selection, preservation of original splash/projectile recipients, Lillia's
damage-threshold sleep and pending-spell handling, Pebbles' complete channel
accounting, Kha'Zix's multiplied mana refund, Sivir's initial-kill extensions
and Elder's cast-start protection. Before regeneration, all 7,670 original
inputs were replayed: **7,085 stayed bit-identical and 585 changed**, only for
repaired champions. The new `test_tft_audit_*.py` regressions check explicit
target identities, arithmetic and event times independently of these fixtures.
Theory's three-target population and shared flat resistance reductions have
separate composition regressions. No unverified Android coefficients or timing
curves were adopted, and neither golden agreement nor these repairs establish
complete live-game accuracy.

The preceding generation resolves Nidalee's equipped form
before choosing exposure and range-dependent item effects: AD is an exposed
melee Assassin in finite tests, AP a protected Marksman. Explicit pressure
overrides still apply. AP range uses the named `AdditionalAttackRange` row
added to base range (1 + 4), a source interpretation whose runtime application
has not been independently observed. Before regeneration, all 7,670 previous
standalone inputs were replayed: 7,649 remained bit-identical and 21 changed,
all Nidalee. Per-build role, form, range and pressure metadata are now recorded
in finite result rows. The experimental damage curves were removed; the
finite leaderboard is restored while these corrections remain.
`test_tft_equipped_forms` and `test_tft_theory_auras` cover form-dependent
exposure and shared provider accounting.
`test_tft_execute` independently checks that immortal probes grant no damage
or healing for Gnar's last-enemy removal, while finite removal, normal throws
and ordinary true damage remain valid. That correction affects abstract
response measurements; the archived finite fight values remain unchanged.

The preceding generation adopts one active eight-second
Brambleback Frenzy, refreshed on recast, as an explicit **conservative model
interpretation**. The pinned tooltip establishes its magnitude and duration,
but available sources do not establish overlapping stacks or a mana lock
through the full duration. The generic mana lock remains unchanged.
Before regeneration, all 7,670 previous standalone inputs were replayed:
7,622 stayed bit-identical and 48 changed, all Brambleback. Five focused tests
in `test_tft_brambleback.py` check the refresh/expiry policy, real triple-Rageblade
AD bound, per-copy time-based attack speed, and effective Alpha healing.
The shared frontline-pressure composition scorer has separate analytic and
native regressions; these fixtures record standalone champion benchmarks.

The preceding 2026-09-07 generation incorporates the
[36-trait audit](../set18/trait-audit.md): delayed Primal Tiger, repeating
Riftbeast growth, conditional Vanguard durability, Hunter secondary-target
amplification, documented Summoner counts and removal of Sentinel's unsupported
opening Alpha mana. Before regeneration, all 7,670 original inputs were replayed:
6,749 fights stayed bit-identical and 921 changed, all with an affected trait
context. No unrelated fight changed. Independent trait regressions check the
corrected mechanics before these fixtures record their new outputs.

The 2026-09-06 generation corrected Brambleback's personal armor ignore,
using the audited 18.1d snapshot.
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
