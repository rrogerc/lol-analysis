# LoL Scaling Analysis

Scrapes League of Legends champion win rates by game length from Lolalytics and
analyzes which champions scale into the late game. Champions are tracked
per role: every lane with more than 10% play rate gets its own entry, so flex
picks like Gragas have separate top/jungle/mid stats.

Everything lives in one SQLite database (`lol.db`) and one CLI (`lol.py`).
There is no config file — what to scrape and what to analyze are just flags.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

If you have a legacy `data/` JSON tree, import it once:

```bash
.venv/bin/python lol.py import-json
```

## The dashboard (recommended)

```bash
.venv/bin/python lol.py serve
```

Opens `http://127.0.0.1:8321` — an interactive local dashboard where you can do
everything without touching the CLI:

- **Overview** — win-rate-by-game-length chart + sortable scaling table, with
  tier/lane/min-games filters. Click table rows to chart champions.
- **Champion** — any champion's win-rate curve per lane, plus their win rate
  across patches.
- **Data & Scrape** — see what's in the database and launch scrapes from the
  browser with a live progress log. New data appears when the scrape finishes.

## CLI equivalents

```bash
# Scrape a tier. Detects the current patch automatically, scrapes every
# season patch that's missing from the DB, always refreshes the current one,
# and skips patches lolalytics no longer serves.
.venv/bin/python lol.py scrape --tier diamond_plus

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

# Aggregate the Riot-API soloq crawl from ../lol-quant into its own tier
# (soloq_masters_plus). Re-run any time the crawl has new games; needs pyarrow.
.venv/bin/python lol.py import-soloq
```

Analysis commands default to the most recently scraped tier and all of its
patches; narrow with `--tier`, `--patches`, `--lane`, `--min-games`. Add
`--csv out.csv` to a report to export it.

## Notes

- The scraper normally runs without a browser (curl_cffi impersonates Chrome's
  TLS fingerprint). If Cloudflare starts serving a JS challenge it falls back
  to a headless browser automatically, and a visible one as a last resort.
- Champion names are lolalytics slugs (`missfortune`, `aurelionsol`).
- `lol.db` is gitignored; the legacy `data/` JSON tree is kept as an archive
  and can rebuild the DB via `import-json` at any time.
- Lolalytics prunes old patches (all of season 15, and 16.2, are already
  gone) — scrape a season while it's live if you want to keep it.
