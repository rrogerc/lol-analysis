# lol-analysis — notes for whoever works here next

## The dashboard on :8321 is a NixOS system service. Never launch it by hand.

- `lol-dashboard.service` runs `lol.py serve --host 0.0.0.0 --port 8321
  --no-open` as rogerc from this checkout, with `Restart=always`. It is
  declared in `~/Developer/dotfiles/nixos/configuration.nix`
  (`systemd.services.lol-dashboard`), not in this repo and not as a user
  unit — check it with plain `systemctl status lol-dashboard`, never
  `systemctl --user`. Logs: `journalctl -u lol-dashboard -n 50`.
- `lol-dashboard-reload.path` (same file) restarts it about 10 s after
  this repo's HEAD moves, so every commit or pull restarts serve.
- A hand-started `lol.py serve` on 8321 squats the port: the unit then
  fails with "Address already in use", hits its start limit, and the
  dashboard's Services card shows it failed while the squatter answers.
  This happened Aug 22–30 2026 and again on 2026-09-04. If serve is
  needed for an experiment, use another port (`--port 8399`) and kill it
  afterwards. If :8321 answers while the unit reports failed, find the
  squatter with `pgrep -af 'lol.py serve'`, kill it, then
  `sudo systemctl restart lol-dashboard` (sudo is Roger's).
- After editing `builds.py` the Builds tab says it is running older code
  than is on disk until serve restarts. Restart by committing, or with
  `sudo systemctl restart lol-dashboard`. systemd stops the whole cgroup,
  so the background warm and its workers go with it.
- Other units in that nix file: `lol-items-refresh` (daily item snapshot,
  user timer) and `lol-scaling-sync` (six-hourly, user timer); the
  dashboard's Data tab shows their state.

## The builds warm

- serve never simulates on request: whenever a scenario cell is cold it
  spawns `lol.py builds warm` (log: `.cache/builds/warm.log`, progress
  lines every 30 s). `serve --no-warm` disables that. Killing the warm
  child turns auto-warm off until serve restarts.
- The warm runs on `pypy3` when it is on the unit's PATH (the nix unit
  adds it via `path = [ pkgs.pypy3 ];`) or on `$LOL_WARM_PYTHON`; otherwise
  on the unit's CPython. PyPy is 3–4x faster with bit-identical results.
- Cells are keyed by a hash of every input including the whole of
  `builds.py`, so any edit to it recomputes everything: about an hour on
  CPython, about 20 minutes on PyPy, on this 16-thread box.
- The enumerator prunes exactly (see `_Bounds` in `builds.py`): results
  must stay identical to an unpruned pass, and `test_builds` checks that.
  Any engine change must keep `simulate` bit-identical unless it is a
  deliberate model change.

## Tests

- `python3 -m unittest test_builds` (also passes under `pypy3`).
