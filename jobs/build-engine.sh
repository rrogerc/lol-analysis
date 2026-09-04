#!/usr/bin/env bash
# Builds engine/ (the Rust extension) in release mode and installs the result
# as lol_engine.abi3.so at the repo root. It lands there rather than in
# site-packages because the nix Python env is immutable: Python imports it
# from the checkout, the same way it imports builds.py.
set -euo pipefail
cd "$(dirname "$0")/.."

# pyo3's build script runs an interpreter to settle the ABI. Name it, so the
# answer never depends on what nix-shell happens to put on PATH.
export PYO3_PYTHON=/run/current-system/sw/bin/python3

# cargo is not installed system-wide; nix-shell fetches it from the store.
if command -v cargo >/dev/null 2>&1; then
  cargo build --release --manifest-path engine/Cargo.toml
else
  nix-shell -p cargo rustc --run 'cargo build --release --manifest-path engine/Cargo.toml'
fi

# Swap the .so in atomically: an import racing the build sees the old module
# or the new one, never a half-written file.
cp engine/target/release/liblol_engine.so lol_engine.abi3.so.tmp
mv -f lol_engine.abi3.so.tmp lol_engine.abi3.so

python3 -c 'import lol_engine; print(lol_engine.SOURCE_HASH)'
