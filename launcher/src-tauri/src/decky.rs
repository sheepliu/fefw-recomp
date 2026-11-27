// SPDX-License-Identifier: GPL-3.0-or-later
//! The Decky plugin, offered on the Play page of a handheld that does not have it. The release's installer (installer/) sets it up,
//! Decky Loader too when that is missing: the launcher only links to it.

use std::fs;
use std::path::Path;

/// Where a session with Steam's Game Mode names itself.
pub const SESSIONS: &str = "/usr/share/wayland-sessions";

/// The latest release's download of the installer.
pub fn installer_url() -> String {
    format!("https://github.com/{}/releases/latest/download/fefw-recomp-install.desktop", crate::update::REPO)
}

/// Whether this system is one the plugin is for, and lacks it: Decky Loader is there already, or a session with Steam's Game Mode
/// (SteamOS and the handheld systems like it run it in gamescope). A desktop without either is not asked.
pub fn wanted(home: &Path, sessions: &Path) -> bool {
    let decky = home.join("homebrew");
    if decky.join("plugins").join("fefw-recomp").join("plugin.json").is_file() {
        return false;
    }
    decky.join("services").join("PluginLoader").is_file()
        || fs::read_dir(sessions).into_iter().flatten().flatten().any(|e| e.file_name().to_string_lossy().contains("gamescope"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn file(path: PathBuf) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, "").unwrap();
    }

    #[test]
    fn offered_to_a_handheld_without_the_plugin() {
        let d = std::env::temp_dir().join(format!("fefw-decky-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        let (home, sessions) = (d.join("home"), d.join("sessions"));
        file(sessions.join("plasma.desktop"));
        assert!(!wanted(&home, &sessions), "a desktop");
        assert!(!wanted(&home, &d.join("nowhere")));
        file(sessions.join("gamescope-session.desktop"));
        assert!(wanted(&home, &sessions), "a handheld");
        fs::remove_file(sessions.join("gamescope-session.desktop")).unwrap();
        file(home.join("homebrew/services/PluginLoader"));
        assert!(wanted(&home, &sessions), "Decky is there");
        file(home.join("homebrew/plugins/fefw-recomp/plugin.json"));
        assert!(!wanted(&home, &sessions), "installed already");
        let _ = fs::remove_dir_all(&d);
    }

    #[test]
    fn the_link_names_the_file_a_release_carries() {
        let url = installer_url();
        assert!(url.starts_with("https://github.com/") && url.ends_with("/releases/latest/download/fefw-recomp-install.desktop"), "{url}");
    }
}
