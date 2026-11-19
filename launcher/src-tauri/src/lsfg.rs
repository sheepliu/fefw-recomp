// SPDX-License-Identifier: GPL-3.0-or-later
//! Linux: frame generation by LSFG-VK (https://lsfg-vk.dev), a Vulkan layer that works on the finished picture. It needs two things we
//! do not ship: the layer itself, and `lsfg-vk.dll` from the `lsfg-vk` beta branch of Lossless Scaling on Steam (the default branch only
//! has the Windows `Lossless.dll`, which the layer cannot read). This finds both and builds the environment the layer is configured
//! with (`LSFGVK_ENV=1`), so the player's own lsfg-vk config file stays untouched.
//!
//! The layer can be installed from Settings (`install`): on CachyOS it is the distribution's `lsfg-vk` package, through Shelly; anywhere
//! else (SteamOS and Bazzite, whose system is read-only) it is LSFG-VK's own build from builds.lsfg-vk.dev, checked against a pinned
//! SHA-256 and unpacked into ~/.local as its install instructions say, which needs no root.

use serde::Serialize;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

/// LSFG-VK's install instructions, for doing it by hand
pub const INSTALL_URL: &str = "https://lsfg-vk.dev/docs/installation/";

/// LSFG-VK's own build of its 2.0.0 release (layer, 32-bit layer, lsfg-vk-cli and lsfg-vk-ui), as published on builds.lsfg-vk.dev
const BUILD_URL: &str = "https://builds.lsfg-vk.dev/lsfg-vk-2.0.0.tar.xz";
const BUILD_SHA256: &str = "08bdbdf373a111022df87dac7aa87e3b564bb841f961552e3ca85fea12b5aa74";
/// the manifests in that build, whose library_path is relative to them (../../../lib)
const MANIFESTS: [&str; 2] = ["VkLayer_LSFGVK_frame_generation.json", "VkLayer_LSFGVK_frame_generation.x86.json"];

/// an install is running: one at a time, whichever Settings page started it
static INSTALLING: AtomicBool = AtomicBool::new(false);

#[derive(Serialize, Clone, Default)]
pub struct Status {
    /// the Vulkan layer is installed (an lsfg-vk manifest in a vulkan/implicit_layer.d folder)
    pub layer: bool,
    /// `lsfg-vk.dll` in the Lossless Scaling folder, forward slashes
    pub dll: String,
    /// Lossless Scaling is installed but only has Lossless.dll: it is on the default branch
    pub wrong_branch: bool,
    /// how `install` would install the layer here: "shelly" (CachyOS's package) or "local" (LSFG-VK's build into ~/.local)
    pub install: String,
    /// `install` is running
    pub installing: bool,
}

fn data_dirs() -> Vec<PathBuf> {
    let mut v = Vec::new();
    if let Some(d) = std::env::var_os("XDG_DATA_HOME").map(PathBuf::from).filter(|p| p.is_absolute()) {
        v.push(d);
    } else if let Some(h) = std::env::var_os("HOME") {
        v.push(PathBuf::from(h).join(".local/share"));
    }
    match std::env::var_os("XDG_DATA_DIRS") {
        Some(d) if !d.is_empty() => v.extend(std::env::split_paths(&d)),
        _ => v.extend(["/usr/local/share", "/usr/share"].map(PathBuf::from)),
    }
    v.push(PathBuf::from("/etc"));
    v
}

fn has_layer() -> bool {
    data_dirs().iter().any(|d| {
        std::fs::read_dir(d.join("vulkan/implicit_layer.d")).is_ok_and(|rd| {
            rd.flatten().any(|e| {
                let n = e.file_name().to_string_lossy().to_ascii_lowercase();
                n.contains("lsfg") && n.ends_with(".json")
            })
        })
    })
}

fn lossless_dirs() -> Vec<PathBuf> {
    let Some((_, libs)) = crate::play::steam_libraries() else { return vec![] };
    libs.iter().map(|l| l.join("steamapps/common/Lossless Scaling")).filter(|d| d.is_dir()).collect()
}

pub fn status() -> Status {
    if !cfg!(target_os = "linux") {
        return Status::default();
    }
    let dirs = lossless_dirs();
    let dll = dirs.iter().map(|d| d.join("lsfg-vk.dll")).find(|p| p.is_file());
    Status {
        layer: has_layer(),
        wrong_branch: dll.is_none() && dirs.iter().any(|d| d.join("Lossless.dll").is_file()),
        dll: dll.map(|p| crate::dump::fwd(&p)).unwrap_or_default(),
        install: if shelly().is_some() { "shelly" } else { "local" }.into(),
        installing: INSTALLING.load(Ordering::SeqCst),
    }
}

fn os_id(os_release: &str) -> String {
    os_release
        .lines()
        .find_map(|l| l.strip_prefix("ID="))
        .map(|v| v.trim().trim_matches(|c| c == '"' || c == '\'').to_ascii_lowercase())
        .unwrap_or_default()
}

/// Shelly on CachyOS, the one system whose repositories carry an lsfg-vk package the launcher installs.
fn shelly() -> Option<PathBuf> {
    let release = std::fs::read_to_string("/etc/os-release").or_else(|_| std::fs::read_to_string("/usr/lib/os-release")).unwrap_or_default();
    if os_id(&release) != "cachyos" {
        return None;
    }
    std::env::split_paths(&std::env::var_os("PATH")?).map(|d| d.join("shelly")).find(|p| p.is_file())
}

/// Installs the layer (see the top of this file) and says what was done.
pub fn install() -> Result<String, String> {
    if !cfg!(target_os = "linux") {
        return Err("LSFG-VK is only for Linux.".into());
    }
    if INSTALLING.swap(true, Ordering::SeqCst) {
        return Err("LSFG-VK is already being installed.".into());
    }
    struct Done;
    impl Drop for Done {
        fn drop(&mut self) {
            INSTALLING.store(false, Ordering::SeqCst);
        }
    }
    let _done = Done;
    if has_layer() {
        return Ok("The LSFG-VK layer is already installed.".into());
    }
    let done = match shelly() {
        Some(shelly) => install_package(&shelly)?,
        None => install_local()?,
    };
    if !has_layer() {
        return Err(format!("{done} But the LSFG-VK layer is still not found. Install it by hand ({INSTALL_URL})."));
    }
    Ok(done)
}

/// `shelly install standard lsfg-vk` as root through pkexec, which asks for the password in a desktop window (Shelly itself would ask
/// sudo for it on a terminal the launcher does not have). Shelly finds the player from pkexec's PKEXEC_UID.
fn install_package(shelly: &Path) -> Result<String, String> {
    let out = Command::new("pkexec")
        .arg(shelly)
        .args(["install", "standard", "--no-confirm", "--needed", "lsfg-vk"])
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("cannot run pkexec to install the lsfg-vk package: {e}. Install it with Shelly yourself (shelly install lsfg-vk)."))?;
    match out.status.code() {
        Some(0) => Ok("Installed the lsfg-vk package with Shelly.".into()),
        Some(126 | 127) => Err("The lsfg-vk package was not installed: the administrator password was not given.".into()),
        _ => {
            let text = String::from_utf8_lossy(&out.stderr).to_string() + &String::from_utf8_lossy(&out.stdout);
            let tail: Vec<&str> = text.lines().map(str::trim).filter(|l| !l.is_empty()).collect();
            Err(format!("Shelly could not install the lsfg-vk package: {}", tail[tail.len().saturating_sub(3)..].join(" ")))
        }
    }
}

/// LSFG-VK's build, unpacked into ~/.local. Directories already there keep their own permissions (`--no-overwrite-dir`).
fn install_local() -> Result<String, String> {
    let home = std::env::var_os("HOME").map(PathBuf::from).filter(|h| h.is_absolute()).ok_or("HOME is not set, so there is no ~/.local to install LSFG-VK into.")?;
    let prefix = home.join(".local");
    std::fs::create_dir_all(&prefix).map_err(|e| format!("cannot create {}: {e}", prefix.display()))?;
    let archive = std::env::temp_dir().join(format!("fefw-lsfg-vk-{}.tar.xz", std::process::id()));
    let res = download_and_unpack(&archive, &prefix);
    let _ = std::fs::remove_file(&archive);
    res?;
    copy_manifests(&prefix)?;
    Ok(format!("Installed LSFG-VK 2.0.0 from builds.lsfg-vk.dev into {}.", prefix.display()))
}

/// The Vulkan loader looks for the player's layers in XDG_DATA_HOME when it is set, not in ~/.local/share: the manifests go there
/// too (as `data_dirs` searches), pointing at the libraries in ~/.local/lib.
fn copy_manifests(prefix: &Path) -> Result<(), String> {
    let Some(data) = std::env::var_os("XDG_DATA_HOME").map(PathBuf::from).filter(|p| p.is_absolute()) else { return Ok(()) };
    let from = prefix.join("share/vulkan/implicit_layer.d");
    let to = data.join("vulkan/implicit_layer.d");
    if std::fs::canonicalize(&from).ok() == std::fs::canonicalize(&to).ok() && to.is_dir() {
        return Ok(());
    }
    std::fs::create_dir_all(&to).map_err(|e| format!("cannot create {}: {e}", to.display()))?;
    for name in MANIFESTS {
        let src = from.join(name);
        let text = std::fs::read_to_string(&src).map_err(|e| format!("cannot read {}: {e}", src.display()))?;
        let dst = to.join(name);
        std::fs::write(&dst, with_library_dir(&text, &prefix.join("lib"))?).map_err(|e| format!("cannot write {}: {e}", dst.display()))?;
    }
    Ok(())
}

/// A layer manifest whose library_path is the same library file in `lib`.
fn with_library_dir(manifest: &str, lib: &Path) -> Result<String, String> {
    let mut v: serde_json::Value = serde_json::from_str(manifest).map_err(|e| format!("LSFG-VK's layer manifest cannot be read: {e}"))?;
    let path = v.pointer_mut("/layer/library_path").ok_or("LSFG-VK's layer manifest has no library_path.")?;
    let file = path.as_str().and_then(|p| Path::new(p).file_name()).ok_or("LSFG-VK's layer manifest has no library file.")?;
    *path = lib.join(file).to_string_lossy().into_owned().into();
    serde_json::to_string_pretty(&v).map_err(|e| e.to_string())
}

fn download_and_unpack(archive: &Path, prefix: &Path) -> Result<(), String> {
    let out = crate::update::curl()
        .args(["--retry", "3", "--max-time", "300", "--output"])
        .arg(archive)
        .arg(BUILD_URL)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("cannot run curl: {e}"))?;
    if !out.status.success() {
        return Err(format!("Could not download LSFG-VK from {BUILD_URL}: {}", String::from_utf8_lossy(&out.stderr).trim()));
    }
    let got = crate::update::sha256_file(archive)?;
    if got != BUILD_SHA256 {
        return Err(format!("The LSFG-VK download does not match the expected file (SHA-256 {got}). It was not installed."));
    }
    let out = Command::new("tar")
        .args(["-xJf"])
        .arg(archive)
        .args(["--no-overwrite-dir", "--no-same-owner", "-C"])
        .arg(prefix)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("cannot run tar: {e}"))?;
    if !out.status.success() {
        return Err(format!("Could not unpack LSFG-VK into {}: {}", prefix.display(), String::from_utf8_lossy(&out.stderr).trim()));
    }
    Ok(())
}

/// Why LSFG-VK cannot be used now, for the player.
pub fn problem(s: &Status) -> Option<String> {
    if !s.layer {
        return Some("LSFG-VK frame generation is on, but the LSFG-VK Vulkan layer is not installed. Install it in Settings (Install LSFG-VK) or turn the setting off.".into());
    }
    if s.dll.is_empty() {
        return Some(if s.wrong_branch {
            "LSFG-VK frame generation is on, but Lossless Scaling is on Steam's default branch. In Steam open Lossless Scaling > Properties > Betas and pick the \"lsfg-vk\" branch, or turn the setting off."
        } else {
            "LSFG-VK frame generation is on, but Lossless Scaling was not found in your Steam libraries (it must be installed, on its \"lsfg-vk\" branch). Install it or turn the setting off."
        }
        .into());
    }
    None
}

/// The environment that starts the layer for this run. The game's own frame generation is turned off so two are not stacked.
pub fn env(dll: &Path, multiplier: u32, flow_scale: &str, performance: bool) -> Vec<(String, String)> {
    let flow = flow_scale.trim().parse::<f32>().ok().filter(|f| (0.25..=1.0).contains(f)).unwrap_or(1.0);
    vec![
        ("LSFGVK_ENV".into(), "1".into()),
        ("LSFGVK_DLL_PATH".into(), dll.to_string_lossy().into_owned()),
        ("LSFGVK_MULTIPLIER".into(), multiplier.clamp(2, 4).to_string()),
        ("LSFGVK_FLOW_SCALE".into(), format!("{flow}")),
        ("LSFGVK_PERFORMANCE_MODE".into(), if performance { "1" } else { "0" }.into()),
        ("NXRT_DLSS_FRAME_GEN".into(), "0".into()),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn env_clamps_and_turns_the_games_frame_generation_off() {
        let e = env(Path::new("/x/lsfg-vk.dll"), 9, "5", true);
        let get = |k: &str| e.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str());
        assert_eq!(get("LSFGVK_MULTIPLIER"), Some("4"));
        assert_eq!(get("LSFGVK_FLOW_SCALE"), Some("1"));
        assert_eq!(get("LSFGVK_PERFORMANCE_MODE"), Some("1"));
        assert_eq!(get("NXRT_DLSS_FRAME_GEN"), Some("0"));
        assert_eq!(get("LSFGVK_DLL_PATH"), Some("/x/lsfg-vk.dll"));
    }

    #[test]
    fn problems_are_named() {
        assert!(problem(&Status::default()).unwrap().contains("layer"));
        let s = Status { layer: true, wrong_branch: true, ..Default::default() };
        assert!(problem(&s).unwrap().contains("Betas"));
        assert!(problem(&Status { layer: true, dll: "/a".into(), ..Default::default() }).is_none());
    }

    #[test]
    fn copied_manifests_point_at_the_library_by_full_path() {
        let m = r#"{"file_format_version":"1.1.0","layer":{"name":"VK_LAYER_LSFGVK_frame_generation","library_path":"../../../lib/liblsfg-vk-layer.so"}}"#;
        let out: serde_json::Value = serde_json::from_str(&with_library_dir(m, Path::new("/home/p/.local/lib")).unwrap()).unwrap();
        let expected = Path::new("/home/p/.local/lib").join("liblsfg-vk-layer.so");
        assert_eq!(out["layer"]["library_path"].as_str(), expected.to_str());
        assert_eq!(out["layer"]["name"], "VK_LAYER_LSFGVK_frame_generation");
        assert!(with_library_dir("{}", Path::new("/l")).is_err());
    }

    #[test]
    fn os_release_id_is_read() {
        assert_eq!(os_id("NAME=\"CachyOS Linux\"\nID=cachyos\nID_LIKE=arch\n"), "cachyos");
        assert_eq!(os_id("ID=\"bazzite\"\nID_LIKE=\"fedora\""), "bazzite");
        assert_eq!(os_id("ID_LIKE=arch\nID=steamos"), "steamos");
        assert_eq!(os_id("NAME=x"), "");
    }
}
