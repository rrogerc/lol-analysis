#!/usr/bin/env bash
# name: Sync scaling data
# schedule: every 6h at :30 (systemd user timer on the home server)
# Re-aggregates the ../lol-quant crawl's parquet into lol.db so the Scaling
# tab follows the crawler without anyone running `lol.py scaling sync` by
# hand. --db-only leaves the committed data/scaling archive to deliberate
# hand-run syncs (an 8 MB rewrite four times a day would bloat the repo).
# Logs: journalctl --user -u lol-scaling-sync
set -euo pipefail
cd "$(dirname "$0")/.."

# Heartbeat for the dashboard's health indicator: record when this job last
# finished and how, whatever the outcome. jobs/.state/ is gitignored.
mkdir -p jobs/.state
trap 'rc=$?; printf "{\"finishedAt\":\"%s\",\"exit\":%d}\n" \
  "$(date -u +%FT%TZ)" "$rc" > "jobs/.state/$(basename "$0" .sh).json"' EXIT

python3 lol.py scaling sync --db-only
