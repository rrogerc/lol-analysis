#!/usr/bin/env bash
# name: Refresh item data
# schedule: daily 07:23 (systemd user timer on the home server)
# Snapshots the current patch's item data (ddragon + meraki) into
# data/items/ and pushes the commit, so the local checkout is always
# current and GitHub is the archive. Writes nothing when upstream is
# unchanged. Logs: journalctl --user -u lol-items-refresh
set -euo pipefail
cd "$(dirname "$0")/.."

# Heartbeat for the dashboard's health indicator: record when this job last
# finished and how, whatever the outcome. jobs/.state/ is gitignored.
mkdir -p jobs/.state
trap 'rc=$?; printf "{\"finishedAt\":\"%s\",\"exit\":%d}\n" \
  "$(date -u +%FT%TZ)" "$rc" > "jobs/.state/$(basename "$0" .sh).json"' EXIT

python3 lol.py items fetch --force

if [ -n "$(git status --porcelain data/items)" ]; then
  git add data/items
  git commit -m "items: refresh snapshot for current patch"
  # --autostash: only data/items is staged above, but rebase refuses to run
  # while ANY other path is dirty. Without it, unrelated work-in-progress in
  # this checkout makes the job commit locally and then die before pushing,
  # leaving GitHub silently behind (happened 2026-08-26, exit 128).
  git pull --rebase --autostash --quiet
  git push --quiet
fi
