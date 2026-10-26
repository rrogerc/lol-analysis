#!/usr/bin/env bash
# Builds the two Rust extensions in release mode and installs them inside
# their respective game packages: lol/engine -> lol/lol_engine.abi3.so and
# tft/engine -> tft/lol_tft.abi3.so. They land there rather than in
# site-packages because the nix Python env is immutable: Python imports
# them from the checkout alongside the rest of each game package.
#
#   web/ops/build-engine.sh [builds|tft]    (default: both)
set -euo pipefail
cd "$(dirname "$0")/../.."

# pyo3's build script runs an interpreter to settle the ABI. Name it, so the
# answer never depends on what nix-shell happens to put on PATH.
export PYO3_PYTHON=/run/current-system/sw/bin/python3

which=${1:-both}

build() {  # build <crate dir> <lib name> <module file>
  local dir=$1 lib=$2 mod=$3
  # cargo is not installed system-wide; nix-shell fetches it from the store.
  if command -v cargo >/dev/null 2>&1; then
    cargo build --release --manifest-path "$dir/Cargo.toml"
  else
    nix-shell -p cargo rustc --run "cargo build --release --manifest-path $dir/Cargo.toml"
  fi
  # Swap the .so in atomically: an import racing the build sees the old
  # module or the new one, never a half-written file.
  cp "$dir/target/release/lib$lib.so" "$mod.tmp"
  mv -f "$mod.tmp" "$mod"
}

if [ "$which" = both ] || [ "$which" = builds ]; then
  build lol/engine lol_engine lol/lol_engine.abi3.so
  python3 -c 'from lol import lol_engine; print("lol_engine", lol_engine.SOURCE_HASH)'
fi
if [ "$which" = both ] || [ "$which" = tft ]; then
  build tft/engine lol_tft tft/lol_tft.abi3.so
  python3 -c 'from tft import lol_tft; print("lol_tft", lol_tft.SOURCE_HASH)'
fi
