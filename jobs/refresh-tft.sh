#!/usr/bin/env bash
# name: Refresh TFT data, builds and compositions
# schedule: every 6h at :41 (systemd user timer)
# Checks Riot's current patch and dated hotfixes, applies unambiguous
# balance changes, and prepares champion builds, all composition contexts
# and compressed dashboard responses before activating them together.
# The dashboard serves only the last complete generation; opening it does
# not start TFT calculations. Unchanged calculations/responses are reused.
# Unrecognized changes retain the previous builds and report a review status.
# Logs: journalctl --user -u lol-tft-refresh
set -euo pipefail
cd "$(dirname "$0")/.."

exec python3 lol.py tft refresh
