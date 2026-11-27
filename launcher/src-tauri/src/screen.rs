// SPDX-License-Identifier: GPL-3.0-or-later
//! The size of this PC's screen. The first Set up uses it to lower [fefw] output on a screen smaller than the console's docked
//! 2560x1440, such as a handheld's (settings.rs).
//!   Windows  Win32_VideoController's current resolution through PowerShell, which Set up already relies on.
//!   Linux    /sys/class/drm/*: the first (preferred) mode of each connected display.
//! No window is needed, so `--prepare` finds it too. With more than one answer the largest counts: a handheld docked to a big
//! screen keeps the bigger picture. FEFW_SCREEN=1280x800 stands in for the probe (tests, or a PC the probe gets wrong).

/// `1280x800` -> (1280, 800); a trailing `i` (an interlaced mode) is ignored.
fn parse_size(s: &str) -> Option<(u32, u32)> {
    let (w, h) = s.trim().trim_end_matches('i').split_once('x')?;
    let (w, h) = (w.parse().ok()?, h.parse().ok()?);
    (w > 0 && h > 0).then_some((w, h))
}

fn largest_of(sizes: impl IntoIterator<Item = (u32, u32)>) -> Option<(u32, u32)> {
    sizes.into_iter().max_by_key(|&(w, h)| u64::from(w) * u64::from(h))
}

#[cfg(windows)]
fn probe() -> Vec<(u32, u32)> {
    use std::os::windows::process::CommandExt;
    let out = std::process::Command::new(crate::setup::powershell())
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_VideoController | ForEach-Object { '{0}x{1}' -f $_.CurrentHorizontalResolution, $_.CurrentVerticalResolution }",
        ])
        .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
        .output();
    match out {
        Ok(o) => String::from_utf8_lossy(&o.stdout).lines().filter_map(parse_size).collect(),
        Err(_) => Vec::new(),
    }
}

#[cfg(not(windows))]
fn probe() -> Vec<(u32, u32)> {
    let Ok(dir) = std::fs::read_dir("/sys/class/drm") else { return Vec::new() };
    let read = |p: std::path::PathBuf| std::fs::read_to_string(p).unwrap_or_default();
    dir.flatten()
        .filter(|d| read(d.path().join("status")).trim() == "connected")
        .filter_map(|d| read(d.path().join("modes")).lines().next().and_then(parse_size))
        .collect()
}

/// The largest screen found, None when nothing says.
pub fn size() -> Option<(u32, u32)> {
    match std::env::var("FEFW_SCREEN") {
        Ok(v) if !v.trim().is_empty() => parse_size(&v),
        _ => largest_of(probe()),
    }
}

/// The [fefw] output a first fefw.ini gets on this screen: the console's own size where 2560x1440 fits (or the screen is unknown),
/// else the largest of 1080p and 720p that fits (720p on anything smaller). The sides are compared long to long, since a handheld's
/// panel may report itself upright (the Steam Deck's is 800x1280).
pub fn output_for(screen: Option<(u32, u32)>) -> &'static str {
    let Some((w, h)) = screen else { return "console" };
    let (long, short) = (w.max(h), w.min(h));
    if long >= 2560 && short >= 1440 {
        "console"
    } else if long >= 1920 && short >= 1080 {
        "1080p"
    } else {
        "720p"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sizes() {
        assert_eq!(parse_size("800x1280\n"), Some((800, 1280)));
        assert_eq!(parse_size("1920x1080i"), Some((1920, 1080)));
        assert_eq!(parse_size("x"), None, "an adapter with no screen");
        assert_eq!(parse_size("0x0"), None);
        assert_eq!(largest_of([(800, 1280), (3840, 2160)]), Some((3840, 2160)));
        assert_eq!(largest_of([]), None);
    }

    #[test]
    fn output_by_screen() {
        for (screen, want) in [
            ((800, 1280), "720p"), // Steam Deck
            ((1280, 800), "720p"),
            ((1600, 900), "720p"),
            ((1920, 1080), "1080p"),
            ((1200, 1920), "1080p"), // an upright 1920x1200 handheld panel
            ((2560, 1080), "1080p"),
            ((2256, 1504), "1080p"),
            ((2560, 1440), "console"),
            ((2560, 1600), "console"),
            ((3440, 1440), "console"),
            ((3840, 2160), "console"),
        ] {
            assert_eq!(output_for(Some(screen)), want, "{screen:?}");
        }
        assert_eq!(output_for(None), "console");
    }
}
