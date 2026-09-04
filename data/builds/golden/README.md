# Golden fixtures — pre-Rust Python damage engine

`engine-fights.json` and `enumerate.json` pin the **exact** output of the
Python engine (`builds.py` at commit **d2922e6**, item patch 16.17) so the
Rust port can be proved bit-identical. `engine-fights.json` holds one case per
(build, target, flag-variant): its inputs plus the `sheet`, `fx`, `ranks` and
`simulate` result. `enumerate.json` holds three `enumerate_builds` runs with
their ordered result rows. Floats are stored as Python `repr`, which
round-trips exactly; a non-finite value would be stored as the string `"inf"`
/ `"-inf"` / `"nan"` (none occur today).

Replayed by `test_builds.TestGolden`, which re-runs every case through the
engine and diffs bit for bit (an int and a float of equal value count as the
same number: the Python engine let Vladimir's integer ult delay leak through
the clock as `4`, the Rust engine says `4.0`). Regenerate with
`jobs/gen_golden.py`. **These files are not regenerated on a whim: any
deliberate model change must regenerate them in the same commit** — a silent
drift means the golden files no longer describe the engine.
