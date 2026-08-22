# LoL Scaling Analysis

Analyzes which League of Legends champions scale into the late game, using
champion win rates by game length aggregated from the Riot-API soloq crawl in
`../lol-quant`. Champions are tracked per role: every lane with more than 10%
play rate gets its own entry, so flex picks like Gragas have separate
top/jungle/mid stats.

Everything lives in one SQLite database (`lol.db`) and one CLI (`lol.py`).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

If you're starting from a fresh checkout (no `lol.db`), rebuild it from the
committed JSON archive:

```bash
.venv/bin/python lol.py import-json
```

## Getting data

Data comes from the `../lol-quant` Riot-API crawl, read in place — nothing is
copied between the repos. Whenever the crawl has new games:

```bash
.venv/bin/python lol.py sync
```

This refreshes all three tiers with their canonical definitions:

- `soloq_masters_plus` — every game
- `soloq_mastery` — pilot has ≥ 20 season games on the champion
- `soloq_otp` — one-tricks: champion is ≥ 80% of the player's role games

Sync writes to `lol.db` and mirrors JSON to `data/<patch>/<tier>/` — commit
and push that to update the published site (its CI rebuilds the DB from
`data/` and exports a static snapshot to GitHub Pages). For a one-off custom
slice (other thresholds, specific platforms, a different tier name), use
`lol.py import-soloq` directly.

## The dashboard (recommended)

```bash
.venv/bin/python lol.py serve
```

Opens `http://127.0.0.1:8321` — an interactive local dashboard:

- **Overview** — win-rate-by-game-length chart + sortable per-bucket rankings,
  with tier/lane/min-games filters. Click table rows to chart champions.
- **Champion** — any champion's win-rate curve per lane, plus their win rate
  across patches.
- **Data** — what's in the database, per tier and patch.

## CLI equivalents

```bash
# Ranked win-rate tables per game-length bucket (0-15 ... 40+ min)
.venv/bin/python lol.py report --top 10 --min-games 5000

# Rank by scaling instead: late-game WR (35+ min) minus early WR (0-20 min)
.venv/bin/python lol.py report --scaling

# One champion's win-rate curve, per lane
.venv/bin/python lol.py champion kayle

# Self-contained interactive HTML dashboard (chart + sortable table)
.venv/bin/python lol.py dashboard && open dashboard.html

# What's in the database
.venv/bin/python lol.py status
```

Analysis commands default to the most recently imported tier and all of its
patches; narrow with `--tier`, `--patches`, `--lane`, `--min-games`. Add
`--csv out.csv` to a report to export it.

## Notes

- Champion names are lowercase slugs (`missfortune`, `aurelionsol`).
- `lol.db` is gitignored; the `data/` JSON tree is the committed archive and
  can rebuild the DB via `import-json` at any time.
