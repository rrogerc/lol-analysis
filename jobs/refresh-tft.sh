#!/usr/bin/env bash
# name: Refresh TFT data and builds
# schedule: every 6h at :41 (systemd user timer)
# Checks Riot's current patch and dated hotfixes, applies unambiguous
# balance changes, and recalculates changed builds before activating them.
# The dashboard reloads only when a complete generation is ready.
# Unrecognized changes retain the previous builds and report a review status.
# Logs: journalctl --user -u lol-tft-refresh
set -euo pipefail
cd "$(dirname "$0")/.."

exec python3 lol.py tft refresh
