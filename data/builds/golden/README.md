# Golden fixtures — the damage engine's output, pinned

`engine-fights.json` and `enumerate.json` pin the **exact** output of the
engine (item patch 16.18). Kayle's and Vladimir's cases are what the pre-Rust
Python engine (`builds.py` at commit **d2922e6**) computed, and they proved
the Rust port bit-identical; when the fixtures were regenerated on 2026-09-07
to add Twitch (`lol_engine 6979fdd6…`, on top of commit e291d15) every one of
those 2,565 cases and the three original enumeration runs came out byte for
byte the same, and the file's provenance now names the Rust engine.
Regenerated again later on 2026-09-07 for the Fiendhunter Bolts model
(Opening Barrage's crit-weighted empowered crit and its true-damage rider,
`lol_engine fb379dd8…`): 167 of the 3,870 fight cases changed, every one of
them a build holding the item (92 Kayle, 75 Twitch; Vladimir's kit never
auto-attacks), and every other case came out byte for byte the same. No
enumeration run's pool holds the item, so `enumerate.json` changed only in
its provenance header.
Regenerated on 2026-09-18 to add Kassadin (`lol_engine 6f54b786…`, on top of
commit ba1a3a3), which also moved the fixtures to item patch 16.18: the
16.18 snapshot (ba1a3a3) had left `TestGolden.test_fights` failing on the
patch label alone. Before regenerating, every one of the 3,870 old cases was
replayed with the new engine at 16.17 and came out bit-identical (the only
differences were the new data key `costIncreasePct` in the 239 Actualizer
cases' merged effects); regenerated at 16.18 every old case keeps its build,
sheet and result byte for byte (the pool's numbers did not move between the
two patches) and the four old enumeration runs are identical. What changed
in the old cases is shape only: sheets gained `ult_cd_mult` (ultimate
haste, read by a kit that recasts its ult) and Actualizer's `manaActive`
gained `costIncreasePct`.
`engine-fights.json` holds one case per (build, target, flag-variant): its
inputs plus the `sheet`, `fx`, `ranks` and `simulate` result (5,249 cases:
129 Kayle builds, 126 Vladimir, 129 Twitch, 131 Kassadin). `enumerate.json`
holds five `enumerate_builds` runs (a Kayle test fixture and a 12-item pool
per champion) with their ordered result rows. Floats are stored as Python
`repr`, which round-trips exactly; a non-finite value would be stored as the
string `"inf"` / `"-inf"` / `"nan"` (none occur today).

Replayed by `test_builds.TestGolden`, which re-runs every case through the
engine and diffs bit for bit (an int and a float of equal value count as the
same number: the Python engine let Vladimir's integer ult delay leak through
the clock as `4`, the Rust engine says `4.0`). Regenerate with
`jobs/gen_golden.py`. **These files are not regenerated on a whim: any
deliberate model change must regenerate them in the same commit** — a silent
drift means the golden files no longer describe the engine.

`survival.json` (added 2026-09-19 with the Survival tier) pins the other
side of a fight: 157 Dr. Mundo builds from the tank pool — the hand-picked
tankiest builds and one of every save, each pool item at least twice, the
rest random and legal — each fought by Kayle's and Kassadin's pinned 16.18
overall winners (314 cases: every number of `builds.survive`'s result,
the defender's report and the tank's sheet included), and one
`enumerate_survival` pass over a nine-item pool. Replayed by
`test_builds.TestSurvivalGolden`; regenerate with
`jobs/gen_golden.py --only survival` after a deliberate change to the
Survival model. The damage fixtures above did not move when it was added:
the defender only exists in a Survival fight.
