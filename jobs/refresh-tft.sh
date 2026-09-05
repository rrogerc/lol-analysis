#!/usr/bin/env bash
# name: Refresh TFT data
# schedule: daily 07:41 (systemd user timer on the home server)
# Snapshots the current TFT patch's unit, item and trait data (MetaTFT's
# lookup file, Community Dragon timings, the patch notes) into data/tft/
# and pushes the commit; a new patch on the news index starts a new patch
# directory, which the dashboard picks up on the restart that follows.
# Then reconciles the snapshot with the patch notes: a stale number makes
# `lol.py tft check` exit 2, which the Automation panel shows in red until
# that patch's overrides.json is fixed by hand (check drafts the snippet).
# Logs: journalctl --user -u lol-tft-refresh
set -euo pipefail
cd "$(dirname "$0")/.."

# Heartbeat for the dashboard's health indicator: record when this job last
# finished and how, whatever the outcome. jobs/.state/ is gitignored.
mkdir -p jobs/.state
trap 'rc=$?; printf "{\"finishedAt\":\"%s\",\"exit\":%d}\n" \
  "$(date -u +%FT%TZ)" "$rc" > "jobs/.state/$(basename "$0" .sh).json"' EXIT

python3 lol.py tft fetch --force

# meta.json records the fetch time, so it changes every run; only the data
# itself is worth a commit. A new patch shows up as an untracked directory.
if [ -n "$(git status --porcelain -- data/tft ':(exclude)data/tft/*/*/meta.json')" ]; then
  git add data/tft
  git commit -m "tft: refresh snapshot for current patch"
  # --autostash: see jobs/refresh-items.sh — rebase refuses while any other
  # path is dirty, and a local commit that never pushes leaves GitHub behind
  git pull --rebase --autostash --quiet
  git push --quiet
else
  git checkout --quiet -- 'data/tft/*/*/meta.json'
fi

python3 lol.py tft check
