//! Stamps the compiled extension with a sha256 over its own sources.
//!
//! The builds cache keys every cell on a hash of its inputs, `builds.py`
//! included; once the engine lives here, Python needs the same handle on the
//! Rust half. `SOURCE_HASH` is that handle, readable at import time.

use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

/// Every file under `dir`, recursively, in no particular order.
fn collect(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).expect("read source directory") {
        let path = entry.expect("read directory entry").path();
        if path.is_dir() {
            collect(&path, out);
        } else {
            out.push(path);
        }
    }
}

fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));

    let mut files = vec![root.join("Cargo.toml")];
    collect(&root.join("src"), &mut files);
    files.sort();

    // Path, then a NUL, then the length, then the bytes: no rename or split of
    // a file can collide with a different tree that happens to concatenate the
    // same way.
    let mut hasher = Sha256::new();
    for path in &files {
        let rel = path.strip_prefix(&root).expect("path under manifest dir");
        hasher.update(rel.to_string_lossy().as_bytes());
        hasher.update(b"\0");
        let bytes = fs::read(path).expect("read source file");
        hasher.update((bytes.len() as u64).to_le_bytes());
        hasher.update(&bytes);
    }
    let hex: String = hasher.finalize().iter().map(|b| format!("{b:02x}")).collect();

    println!("cargo:rustc-env=LOL_TFT_SOURCE_HASH={hex}");
    println!("cargo:rerun-if-changed=src");
    println!("cargo:rerun-if-changed=Cargo.toml");
}
