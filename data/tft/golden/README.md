# Golden fixtures — pre-Rust Python TFT engine

`fights.json` and `cells.json` pin the **exact** output of the Python TFT
engine (`tft.py` + `tft_kits.py` at the commit named in each file's
`provenance`, Set 18 patch 18.1) so the Rust port can be proved
bit-identical.

- `fights.json`: one case per (unit, scenario cell, build) — the cell's two
  best cached builds, two drawn with a fixed seed from every legal 3-item
  multiset, and the empty build in the bare trait context — with the opening
  sheet numbers (`sheet`) and the unrounded `simulate` result (`result`).
  Floats are stored as Python `repr`, which round-trips exactly; a
  non-finite value would be the string `"inf"` / `"-inf"` / `"nan"` (none
  occur today).
- `cells.json`: the top 20 rows of every cell as the dashboard shows them
  (rounded), plus the build count and the driver's name, so the
  enumeration's order and the row formatting are pinned too.

Replayed by `test_tft.TestGolden`, which re-runs every fight through the
engine and diffs bit for bit (an int and a float of equal value count as the
same number), and recomputes a sample of the cells; `jobs/gen_tft_golden.py
--check` recomputes every cell. Regenerate with `jobs/gen_tft_golden.py`
from a fully warm `.cache/tft`. **These files are not regenerated on a whim:
any deliberate model change must regenerate them in the same commit** — a
silent drift means the golden files no longer describe the engine.
