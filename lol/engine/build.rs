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
    // Two more handles for the cache keys. A machine-written driver
    // (src/generated/<slug>.rs) only ever runs for its own champion, so a cell
    // keys on the CORE (everything but that directory) plus, for a generated
    // champion, its own driver: rewriting one driver leaves every other
    // champion's cells warm. SOURCE_HASH stays the hash of everything, for
    // "is this build stale".
    let mut hasher = Sha256::new();
    let mut core = Sha256::new();
    let mut generated: Vec<String> = Vec::new();
    for path in &files {
        let rel = path.strip_prefix(&root).expect("path under manifest dir");
        let rel_s = rel.to_string_lossy().replace('\\', "/");
        let bytes = fs::read(path).expect("read source file");
        let is_generated = rel_s.starts_with("src/generated/");
        for h in [Some(&mut hasher), if is_generated { None } else { Some(&mut core) }]
            .into_iter().flatten() {
            h.update(rel.to_string_lossy().as_bytes());
            h.update(b"\0");
            h.update((bytes.len() as u64).to_le_bytes());
            h.update(&bytes);
        }
        if is_generated && rel_s.ends_with(".rs") && !rel_s.ends_with("/mod.rs") {
            let name = rel_s["src/generated/".len()..rel_s.len() - 3].to_string();
            let d: String = Sha256::digest(&bytes).iter().map(|b| format!("{b:02x}")).collect();
            generated.push(format!("{name}={d}"));
        }
    }
    let hex: String = hasher.finalize().iter().map(|b| format!("{b:02x}")).collect();
    let core_hex: String = core.finalize().iter().map(|b| format!("{b:02x}")).collect();

    println!("cargo:rustc-env=LOL_ENGINE_CORE_HASH={core_hex}");
    println!("cargo:rustc-env=LOL_ENGINE_GENERATED_HASHES={}", generated.join(";"));
    println!("cargo:rustc-env=LOL_ENGINE_SOURCE_HASH={hex}");
    println!("cargo:rerun-if-changed=src");
    println!("cargo:rerun-if-changed=Cargo.toml");
}
