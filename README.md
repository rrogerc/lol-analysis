# LoL Analysis

Monorepo for League of Legends analysis. One CLI (`lol.py`), one SQLite
database (`lol.db`), one web dashboard — with each analysis domain as its own
module and committed JSON archive:

- **scaling** (`scaling.py`, `data/scaling/`) — which champions scale into the
  late game: win rates by game length, aggregated from the Riot-API soloq
  crawl in `../lol-quant`. Champions are tracked per role: every lane with
  more than 10% play rate gets its own entry, so flex picks like Gragas have
  separate top/jungle/mid stats.
- **items** (`items.py`, `data/items/`) — static item data for build math.
  `lol.py items fetch` snapshots the current patch's Summoner's Rift items
  from three sources: Riot's Data Dragon (canonical, but key offensive stats
  only live in description text), Meraki Analytics (every stat fully
  structured — lethality, % pen, haste, crit damage), and Riot's raw item
  bin via CommunityDragon, distilled to `groups.json` ("Limited to 1"
  ownership groups, currency-gated and retired items). Meraki serves only
  the latest patch, so the committed per-patch snapshots are the historical
  archive. A systemd user timer on the home server runs
  `jobs/refresh-items.sh` daily and pushes when a new patch lands, so the
  local checkout is always current (the Data tab lists all such jobs).
- **builds** (`builds.py`, `data/builds/`) — theoretical build math, no match
  data: resolve champion + items into exact stat sheets with the real in-game
  rules (growth curve, AS ratio and cap, pen ordering, Rabadon multiplier).
  Champion base stats are snapshotted per patch via
  `lol.py builds fetch-champion <name>` (ddragon canonical, meraki for AS
  ratio/windup — and for AD growth, which ddragon has published as 0 for
  every champion since 16.5 while Riot's game files still carry it);
  text-only item passives live in the hand-curated
  `data/builds/item-effects.json`, and ability kits in hand-encoded
  `data/builds/<champ>.json` — Kayle and Vladimir so far, each paired
  with a rotation driver (`engine/src/drivers.rs`) that says what the
  champion does with its attacks and abilities; the engine itself is
  champion-agnostic (clock, target, item procs, damage pipeline). On top:
  a deterministic expected-value combat engine (`builds sim` — event
  timeline, on-hit procs, Guinsoo phantom hits, pen ordering, EV crit;
  compiled Rust in `engine/`, driven from `builds.py`)
  and a build enumerator (`builds optimize` — ranks every item-pool
  combination against a stat dummy; `--budget`/`--require` for partial
  builds). A kit can rule items out of its pool (Vladimir has no mana,
  so the Tear items and Actualizer never enter his enumeration) or
  declare that the champion never auto-attacks (Vladimir, as played:
  on-hit, crit and spellblade items then rank on their raw stats).
  The enumeration pool covers every mage, marksman, assassin, and
  bruiser damage item (76 items + the 7 boots gold can buy; the Feats of
  Strength upgrades are not) — AP, on-hit, crit, executes,
  burns, Energized, shreds, spellblades, item actives, fully-stacked tear
  items; the few items whose passives can't be modeled yet (plus
  support/tank items) are excluded rather than misranked on stats
  alone, each with its reason recorded under `"excluded"` in
  `item-effects.json`. Four scenarios, all full builds at level 16: vs
  a squishy, a bruiser and a tank stat dummy, plus *overall*. The three
  targets are simulated in one pass, which also fills the overall cell:
  every build ranked on all three at once, by the geometric mean of its
  expected kill times (each target counts equally in percentage terms; a
  build must kill every target to rank above one that leaves any
  standing). That pass ranks ~115M legal builds — every tier-2 boots
  with every legal five-item combination from a ~75-item pool — against
  each target. The enumerator fans out across CPU cores in blocks and
  prunes exactly: it publishes every cell's running 500th-best in shared
  memory, drops results that can't place, and stops a fight the moment
  its clock passes the point where the build can no longer place on that
  target's list nor on the overall one (a survivor ranks below every
  killer, and a kill still to come lands later than the clock), so the
  lists come out exactly as an unpruned pass ranks them (a test checks
  that). Boots that fight alike share one simulation: Mercury's Treads,
  Plated Steelcaps and Gluttonous Greaves always; Boots of Swiftness too
  unless the build carries an Energized item, since move speed only
  charges those; Berserker's Greaves too for a champion who never
  attacks. A previous result's rows are scored first, so the bounds are
  tight from the start. The sheet, the fight and that inner loop are
  compiled (`engine/`, Rust through PyO3, imported as `lol_engine`; build
  it with `jobs/build-engine.sh`): a champion takes 5–11 s on 16 cores (the
  whole warm about 17 s), where the pure-Python engine it replaced took
  half an hour. Its output
  is pinned bit for bit by the fixtures in `data/builds/golden`
  (`test_builds.TestGolden`; regenerate with `jobs/gen_golden.py` only
  after a deliberate model change). Progress lines land in
  `.cache/builds/warm.log`.
  The dashboard never simulates on request: `lol.py builds warm`
  precomputes every (champion, scenario) cell — its
  top 500 builds, each with its fight against every target — into
  `.cache/builds/` (gitignored) under a hash of every input, and `serve`
  runs it in the background whenever a cell is cold, so a code or data
  change just makes cells recompute. Runes are not modeled yet.
  Math is pinned by hand-computed tests: `python3 -m unittest
  test_builds`.

Shared plumbing lives in `common.py` (DB, paths, patch ordering) and
`webapp.py` (the serve/export web shell).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
jobs/build-engine.sh   # the builds engine (Rust); uses cargo, or nix-shell -p cargo rustc
```

If you're starting from a fresh checkout (no `lol.db`), rebuild it from the
committed JSON archive:

```bash
.venv/bin/python lol.py import-json
```

## Getting data

Scaling data comes from the `../lol-quant` Riot-API crawl, read in place —
nothing is copied between the repos, but `lol.db` is an aggregate that has
to be rebuilt from the crawl's parquet. On the home server a systemd user
timer runs `jobs/sync-scaling.sh` every six hours (`scaling sync
--db-only`), so the Scaling tab follows the crawler on its own and its
subtitle shows when it last synced. To refresh by hand:

```bash
python3 lol.py scaling sync
```

This refreshes all three tiers with their canonical definitions:

- `soloq_masters_plus` — every game
- `soloq_mastery` — pilot has ≥ 20 season games on the champion
- `soloq_otp` — one-tricks: champion is ≥ 80% of the player's role games

A hand-run sync also mirrors JSON to `data/scaling/<patch>/<tier>/` —
commit that when you want a checkpoint the archive on GitHub can rebuild
the DB from via `import-json` (the timer skips it: an 8 MB rewrite four
times a day would bloat the repo). For a one-off custom slice (other thresholds, specific
platforms, a different tier name), use `lol.py scaling import-soloq`
directly.

## The dashboard (recommended)

```bash
.venv/bin/python lol.py serve
```

Opens `http://127.0.0.1:8321` — an interactive local dashboard.

On the home server nobody runs that command: the NixOS system unit
`lol-dashboard.service` (declared in `dotfiles/nixos/configuration.nix`)
runs it on :8321 with `Restart=always`, and `lol-dashboard-reload.path`
restarts it about 10 s after every commit or pull of this repo. A serve
started by hand there either fails to bind or, if it got there first,
squats the port and leaves the unit failed while it answers — so use
another port for experiments (`--port 8399`), restart with a commit or
`sudo systemctl restart lol-dashboard`, and read `systemctl status
lol-dashboard` / `journalctl -u lol-dashboard` for its state. The same
notes live in `CLAUDE.md` for anyone (or anything) working here.

The tabs:

- **Overview** — win-rate-by-game-length chart + sortable per-bucket rankings,
  with tier/lane/min-games filters. Click table rows to chart champions.
- **Champion** — any champion's win-rate curve per lane, plus their win rate
  across patches.
- **Builds** — the theoretical damage model's ranked full builds per
  champion, vs a squishy, a bruiser, a tank, or overall. Every row shows
  the build's expected kill time against each target and how far behind
  that target's best it is; the overall scenario ranks by the geometric
  mean of those times. Click a build for its damage-source breakdown
  against each target. Every scenario is precomputed (`lol.py builds
  warm`, which
  `serve` runs in the background whenever something is cold — `--no-warm`
  turns that off); a cell that isn't computed yet says so and fills in
  when it lands. Restart serve after editing `builds.py` or `engine/`
  (rebuild first; on the server: commit, or `sudo systemctl restart
  lol-dashboard`) — the tab tells you when it is running older code than
  is on disk.
- **Data** — systemd unit health, automation job status, and what's in the
  database, per tier and patch.

## CLI equivalents

```bash
# Ranked win-rate tables per game-length bucket (0-15 ... 40+ min)
.venv/bin/python lol.py scaling report --top 10 --min-games 5000

# Rank by scaling instead: late-game WR (35+ min) minus early WR (0-20 min)
.venv/bin/python lol.py scaling report --scaling

# One champion's win-rate curve, per lane
.venv/bin/python lol.py scaling champion kayle

# Self-contained interactive HTML dashboard (chart + sortable table)
.venv/bin/python lol.py scaling dashboard && open dashboard.html

# What's in the database
.venv/bin/python lol.py status
```

Analysis commands default to the most recently imported tier and all of its
patches; narrow with `--tier`, `--patches`, `--lane`, `--min-games`. Add
`--csv out.csv` to a report to export it.

## Notes

- Champion names are lowercase slugs (`missfortune`, `aurelionsol`).
- `data/scaling/platforms.json` records which regions the crawl covers
  (written by `scaling sync`, shown in the UI next to the tier description).
- `lol.db` is gitignored; the `data/` JSON tree is the committed archive and
  can rebuild the DB via `import-json` at any time.
