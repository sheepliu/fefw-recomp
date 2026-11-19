// SPDX-License-Identifier: GPL-3.0-or-later
//! The launcher only invokes the modpack validator. It never writes to the game dump.

use serde_json::Value;
use std::path::PathBuf;
use std::process::Command;
use std::sync::Mutex;

pub struct ModPaths {
    pub python: PathBuf,
    pub script: PathBuf,
    pub main: PathBuf,
    pub mods: PathBuf,
}

static MOD_OPERATIONS: Mutex<()> = Mutex::new(());

pub fn invoke(paths: ModPaths, args: &[String]) -> Result<Value, String> {
    let _guard = MOD_OPERATIONS.lock().map_err(|_| "Mod operation lock failed".to_string())?;
    if !paths.python.is_file() {
        return Err("Python for mod validation is missing. Run Setup first.".into());
    }
    if !paths.script.is_file() {
        return Err("Mod validator is missing from this installation.".into());
    }
    if !paths.main.is_file() {
        return Err("Original game main is missing. Choose game files in Setup first.".into());
    }
    let output = Command::new(&paths.python)
        .arg("-I")
        .arg(&paths.script)
        .arg("--main")
        .arg(&paths.main)
        .arg("--mods-dir")
        .arg(&paths.mods)
        .args(args)
        .output()
        .map_err(|e| format!("Could not start mod validator: {e}"))?;
    if !output.status.success() {
        let reason = String::from_utf8_lossy(&output.stderr);
        return Err(format!("Mod validation failed: {}", reason.trim()));
    }
    serde_json::from_slice(&output.stdout).map_err(|e| format!("Mod validator returned invalid data: {e}"))
}
