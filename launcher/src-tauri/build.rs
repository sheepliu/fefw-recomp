use std::fmt::Write;
use std::path::PathBuf;
use std::{env, fs};

fn main() {
    shipped_locales();
    tauri_build::build()
}

/// Packs every language file of the repository into the launcher (src/shipped.rs): the F1 menu's tables and the launcher's packs.
/// Adding a file to either folder is all it takes.
fn shipped_locales() {
    let manifest = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("set by cargo"));
    let mut files = Vec::new();
    for (folder, extension) in [("../../locales", "ini"), ("../locales", "json")] {
        let folder = manifest.join(folder);
        println!("cargo:rerun-if-changed={}", folder.display());
        let entries = fs::read_dir(&folder).unwrap_or_else(|e| panic!("cannot read {}: {e}", folder.display()));
        for entry in entries {
            let path = entry.expect("a folder entry").path();
            if path.extension().is_some_and(|x| x == extension) {
                let name = path.file_name().expect("a file").to_string_lossy().into_owned();
                files.push((name, path));
            }
        }
    }
    files.sort();
    let mut out = String::from("/// (file name, content) of every language file in the repository.\npub const FILES: &[(&str, &str)] = &[\n");
    for (name, path) in files {
        writeln!(out, "    ({name:?}, include_str!({:?})),", path.display().to_string()).expect("writing to a string");
    }
    out.push_str("];\n");
    let target = PathBuf::from(env::var_os("OUT_DIR").expect("set by cargo")).join("shipped_locales.rs");
    fs::write(&target, out).unwrap_or_else(|e| panic!("cannot write {}: {e}", target.display()));
}
