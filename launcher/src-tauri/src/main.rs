// SPDX-License-Identifier: GPL-3.0-or-later
//! Launcher for the fefw recomp (fefw.exe). It works on a checkout of this repository: it reads the player's own dump, runs the
//! repository's own lift and build scripts on this PC, writes fefw.ini in a run folder and starts the game. It ships no game files.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod art;
mod cpu;
mod decky;
mod dump;
mod fetch;
mod gpu;
mod headless;
mod ini;
mod lang;
mod mods;
mod locale;
mod lock;
mod lsfg;
mod manifest;
mod memory;
mod nup;
mod pad_volume;
mod play;
mod report;
mod screen;
mod settings;
mod setup;
mod shipped;
mod steam;
mod update;

use serde::{Deserialize, Serialize};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::{Arc, OnceLock};
use tauri::Emitter;
use tauri::{AppHandle, Manager, State};
use tauri_plugin_dialog::DialogExt;

/// The launcher's own options, in the user's app config folder.
#[derive(Serialize, Deserialize, Clone, Default)]
#[serde(default)]
struct Config {
    /// The repository checkout. Empty: found from the launcher's own location.
    repo_dir: String,
    /// The dump folder the player picked (exefs/ and romfs/, or base/ and updates/).
    dump_dir: String,
    /// Where fefw.ini, controls.ini, the log and the saves live. Empty: <repo>/work/play.
    game_dir: String,
    /// Build processes. 0: one fewer than the CPU threads.
    jobs: u32,
    minimize_on_play: bool,
    /// do not ask GitHub for a newer release when the launcher starts
    skip_update_check: bool,
    /// opt-in: send anonymized crash/performance logs after a run when an issue is detected
    send_reports: bool,
    /// whether the one-time opt-in prompt for issue reports has already been answered
    reports_prompted: bool,
    /// Linux: the offer of the Decky plugin on the Play page was declined
    decky_declined: bool,
    /// Linux: the command that starts fefw.exe (Wine or Proton, e.g. `umu-run` or `wine`). Empty: Steam's own Proton if it is
    /// installed, else umu-run, else wine. Unused on Windows.
    runner: String,
    /// The picked interface language ("en", "zh", or the code of a language pack in locales/), for the launcher and the F1 menu.
    /// Empty: none picked, so both follow the system (lang.rs). Only set_language changes it.
    lang: String,
    /// Linux: LSFG-VK frame generation on top of the game (lsfg.rs)
    lsfg: bool,
    /// 2..=4; 0 (unset) is 2
    lsfg_multiplier: u32,
    /// "0.25".."1.0"; empty is 1.0
    lsfg_flow_scale: String,
    lsfg_performance: bool,
}

fn config_path(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_config_dir().map_err(|e| e.to_string())?;
    fs::create_dir_all(&dir).map_err(|e| format!("cannot create {}: {}", dir.display(), e))?;
    Ok(dir.join("launcher.json"))
}

/// The dump folder passed with --game: used in place of the saved one for this run (what the folder picker would have set).
static GAME_ARG: OnceLock<String> = OnceLock::new();

/// The saved launcher.json, without the --game override.
fn load_saved_config(app: &AppHandle) -> Config {
    config_path(app).ok().and_then(|p| fs::read_to_string(p).ok()).and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default()
}

fn load_config(app: &AppHandle) -> Config {
    let mut cfg = load_saved_config(app);
    if let Some(g) = GAME_ARG.get() {
        cfg.dump_dir = g.clone();
    }
    cfg
}

fn is_package_dir(dir: &Path) -> bool {
    dir.join("tools").join("build").join("fefw_build.py").is_file()
}

fn resolve_exe_dir(current_exe: Option<&Path>, appdir: Option<&Path>, appimage: Option<&Path>) -> PathBuf {
    #[cfg(target_os = "linux")]
    {
        // RecompLauncher (and --appimage-extract) unpacks the AppImage into the install folder, putting the binary at
        // <install>/usr/bin/fefw-launcher with tools/ at <install>/tools/ and no $APPIMAGE.
        if let Some(exe) = current_exe {
            if let Some(bin) = exe.parent() {
                if bin.file_name() == Some(std::ffi::OsStr::new("bin")) {
                    if let Some(usr) = bin.parent() {
                        if usr.file_name() == Some(std::ffi::OsStr::new("usr")) {
                            if let Some(root) = usr.parent() {
                                if is_package_dir(root) {
                                    return root.to_path_buf();
                                }
                            }
                        }
                    }
                }
            }
        }
        if let Some(dir) = appdir.filter(|d| !d.as_os_str().is_empty() && is_package_dir(d)) {
            return dir.to_path_buf();
        }
        // Mounted .AppImage: runs from a temporary mount, while the package sits next to the .AppImage file.
        if let Some(d) = appimage.filter(|p| !p.as_os_str().is_empty()).and_then(|p| p.parent()).filter(|d| !d.as_os_str().is_empty()) {
            return d.to_path_buf();
        }
    }
    #[cfg(not(target_os = "linux"))]
    let _ = (appdir, appimage);
    current_exe.and_then(|p| p.parent().map(|d| d.to_path_buf())).unwrap_or_else(|| PathBuf::from("."))
}

fn exe_dir() -> PathBuf {
    let current_exe = std::env::current_exe().ok();
    let appdir = std::env::var_os("APPDIR").map(PathBuf::from);
    let appimage = std::env::var_os("APPIMAGE").map(PathBuf::from);
    resolve_exe_dir(current_exe.as_deref(), appdir.as_deref(), appimage.as_deref())
}

/// The release package's tools/ folder, when the launcher runs from the release zip (tools/build/fefw_build.py next to it).
fn package_tools() -> Option<PathBuf> {
    let dir = exe_dir();
    is_package_dir(&dir).then(|| dir.join("tools"))
}

fn is_repo(dir: &Path) -> bool {
    dir.join("recomp").join("games").join("fefw").join("config.py").is_file() && dir.join("runtime").join("CMakeLists.txt").is_file()
}

/// The checkout the launcher runs in: the configured one, else the first parent of the launcher's exe that is one.
fn repo_of(cfg: &Config) -> Option<PathBuf> {
    if package_tools().is_some() {
        // the release package: the install folder plays the checkout's part
        return Some(if cfg.game_dir.trim().is_empty() { exe_dir() } else { PathBuf::from(cfg.game_dir.trim()) });
    }
    if !cfg.repo_dir.trim().is_empty() {
        let p = PathBuf::from(cfg.repo_dir.trim());
        return is_repo(&p).then_some(p);
    }
    let exe = std::env::current_exe().ok()?;
    exe.ancestors().skip(1).take(8).find(|d| is_repo(d)).map(|d| d.to_path_buf())
}

fn game_dir_of(cfg: &Config, repo: &Path) -> PathBuf {
    if cfg.game_dir.trim().is_empty() {
        if package_tools().is_some() {
            return exe_dir();
        }
        repo.join("work").join("play")
    } else {
        PathBuf::from(cfg.game_dir.trim())
    }
}

fn need_repo(cfg: &Config) -> Result<PathBuf, String> {
    repo_of(cfg).ok_or_else(|| {
        "The repository was not found. Build the launcher inside a checkout of this project, or set the repository folder in Settings.".to_string()
    })
}

/// Setup's paths from the configuration, when the dump is usable.
fn paths(cfg: &Config) -> Result<setup::Paths, String> {
    let repo = need_repo(cfg)?;
    let report = dump::scan(&cfg.dump_dir, &builds_of(&repo));
    let chosen = report.chosen.ok_or_else(|| {
        if cfg.dump_dir.trim().is_empty() {
            "Choose your game files first.".to_string()
        } else {
            format!("The game files at {} are not usable: {}", cfg.dump_dir, report.errors.join(" "))
        }
    })?;
    let game_dir = game_dir_of(cfg, &repo);
    Ok(setup::Paths {
        repo,
        exefs: PathBuf::from(chosen.exefs),
        romfs: PathBuf::from(chosen.romfs),
        dlc: report.dlc_dir.map(PathBuf::from),
        game_dir,
        package: package_tools(),
        lang: cfg.lang.clone(),
        skip_update_check: cfg.skip_update_check,
    })
}

/// The supported builds of main: the package's dump_hashes.json, else the checkout's recomp/games/fefw/config.py.
fn builds_of(repo: &Path) -> Vec<(String, String)> {
    if let Some(t) = package_tools() {
        let v: serde_json::Value =
            fs::read_to_string(t.join("build").join("dump_hashes.json")).ok().and_then(|x| serde_json::from_str(&x).ok()).unwrap_or_default();
        if let (Some(id), Some(ver)) = (v["main_build_id"].as_str(), v["version"].as_str()) {
            return vec![(id.to_uppercase(), ver.to_string())];
        }
        return vec![];
    }
    dump::supported_builds(repo)
}

fn jobs_of(cfg: &Config) -> usize {
    if cfg.jobs > 0 {
        return cfg.jobs as usize;
    }
    std::thread::available_parallelism().map(|n| n.get().saturating_sub(1)).unwrap_or(2).max(1)
}

#[derive(Serialize)]
struct Overview {
    repo_dir: String,
    repo_ok: bool,
    game_dir: String,
    exe: String,
    exe_exists: bool,
}

#[tauri::command]
fn get_config(app: AppHandle) -> Config {
    load_config(&app)
}

#[tauri::command]
fn save_config(app: AppHandle, mut config: Config) -> Result<(), String> {
    // the settings page sends back what get_config gave it: keep the --game folder out of launcher.json
    if GAME_ARG.get().is_some_and(|g| *g == config.dump_dir) {
        config.dump_dir = load_saved_config(&app).dump_dir;
    }
    // a page holding an older configuration must not undo a language switch or report-prompt answer made since it loaded
    let saved = load_saved_config(&app);
    config.lang = saved.lang;
    config.reports_prompted = saved.reports_prompted || config.reports_prompted || config.send_reports != saved.send_reports;
    config.decky_declined = saved.decky_declined || config.decky_declined;
    // the game makes the same check when it is started: a switch here covers it too. fefw.ini first: when it cannot be written, the
    // switch stays unsaved and can be made again.
    if config.skip_update_check != saved.skip_update_check {
        let _lock = lock::for_write(&exe_dir())?;
        if let Some(repo) = repo_of(&config) {
            settings::set_update_check(&game_dir_of(&config, &repo), !config.skip_update_check)?;
        }
    }
    store_config(&app, &config)
}

#[tauri::command]
fn set_send_reports(app: AppHandle, enabled: bool) -> Result<(), String> {
    let mut cfg = load_saved_config(&app);
    cfg.send_reports = enabled;
    cfg.reports_prompted = true;
    store_config(&app, &cfg)
}

/// What the Play page's offer of the Decky plugin shows. None: nothing to offer.
#[derive(Serialize)]
struct DeckyOffer {
    /// the install's folder, for the installer's question, when it would not find it by itself
    folder: Option<String>,
}

#[tauri::command]
fn decky_offer(app: AppHandle) -> Option<DeckyOffer> {
    let home = std::env::var_os("HOME").map(PathBuf::from)?;
    // the installer takes a release package's folder: a checkout's launcher offers nothing
    let offered = cfg!(target_os = "linux") && package_tools().is_some() && !load_saved_config(&app).decky_declined;
    if !offered || !decky::wanted(&home, Path::new(decky::SESSIONS)) {
        return None;
    }
    let install = exe_dir();
    Some(DeckyOffer { folder: (install != home.join("Games").join("fefw-recomp")).then(|| dump::fwd(&install)) })
}

#[tauri::command]
fn open_decky_installer() -> Result<(), String> {
    play::open_with_shell(Path::new(&decky::installer_url()))
}

#[tauri::command]
fn decline_decky(app: AppHandle) -> Result<(), String> {
    let mut cfg = load_saved_config(&app);
    cfg.decky_declined = true;
    store_config(&app, &cfg)
}

fn store_config(app: &AppHandle, config: &Config) -> Result<(), String> {
    let text = serde_json::to_string_pretty(config).map_err(|e| e.to_string())?;
    settings::write_atomic(&config_path(app)?, &text).map_err(|e| format!("cannot save launcher settings: {e}"))
}

/// Windows: a warning when little memory is left for the game to take.
#[tauri::command]
fn memory_warning() -> Option<memory::Warning> {
    memory::warning()
}

/// The language the launcher shows: the picked one, else the system's.
#[tauri::command]
fn ui_language(app: AppHandle) -> String {
    let picked = load_saved_config(&app).lang;
    if picked.is_empty() {
        lang::system_choice()
    } else {
        picked
    }
}

/// A language picked in the sidebar: saved, and written to fefw.ini for the F1 menu when the file is there.
#[tauri::command]
fn set_language(app: AppHandle, code: String) -> Result<(), String> {
    let mut cfg = load_saved_config(&app);
    cfg.lang = code;
    store_config(&app, &cfg)?;
    // under another process's setup or update the run folder is left alone: Set up and Play bring it in step
    let Ok(_lock) = lock::for_write(&exe_dir()) else { return Ok(()) };
    sync_run_folder(&cfg)
}

/// A release package's own language files, brought up to this launcher's (shipped.rs). A checkout's are the sources: left alone.
fn refresh_package_locales() {
    if package_tools().is_some() {
        shipped::refresh(&locale::locales_dir());
    }
}

/// The run folder's language tables, [gfx] ui_language and an update check turned off, brought in step with the launcher (settings.rs).
fn sync_run_folder(cfg: &Config) -> Result<(), String> {
    // no checkout or package found: no run folder to keep in step
    let Some(repo) = repo_of(cfg) else { return Ok(()) };
    let game_dir = game_dir_of(cfg, &repo);
    // each is tried, whether or not the other fails
    let opt_out = settings::apply_update_opt_out(&game_dir, cfg.skip_update_check);
    let language = settings::apply_menu_language(&game_dir, &repo, package_tools().as_deref(), &cfg.lang);
    opt_out.and(language)
}

#[tauri::command]
fn get_overview(app: AppHandle) -> Overview {
    let cfg = load_config(&app);
    match repo_of(&cfg) {
        Some(repo) => {
            let game = game_dir_of(&cfg, &repo);
            let exe = if package_tools().is_some() {
                game.join("fefw.exe")
            } else if game.join("mods-state.json").is_file() || game.join("mod-build").join("done.json").is_file()
                || setup::has_native_code_mods(&game) {
                game.join("mod-build").join("active").join("fefw.exe")
            } else {
                setup::exe_path(&repo)
            };
            Overview {
                repo_dir: dump::fwd(&repo),
                repo_ok: true,
                game_dir: dump::fwd(&game_dir_of(&cfg, &repo)),
                exe_exists: exe.is_file(),
                exe: dump::fwd(&exe),
            }
        }
        None => Overview { repo_dir: cfg.repo_dir.clone(), repo_ok: false, game_dir: String::new(), exe: String::new(), exe_exists: false },
    }
}

#[tauri::command]
async fn pick_folder(app: AppHandle, title: String, start: Option<String>) -> Option<String> {
    let mut dlg = app.dialog().file().set_title(title);
    if let Some(s) = start.filter(|s| PathBuf::from(s).is_dir()) {
        dlg = dlg.set_directory(s);
    }
    dlg.blocking_pick_folder().and_then(|p| p.into_path().ok()).map(|p| dump::fwd(&p))
}

#[tauri::command]
async fn pick_nup(app: AppHandle, start: Option<String>) -> Option<String> {
    let mut dlg = app.dialog().file().set_title("Choose your .nup package").add_filter("NUP package", &["nup"]);
    if let Some(s) = start.filter(|s| PathBuf::from(s).is_dir()) {
        dlg = dlg.set_directory(s);
    }
    dlg.blocking_pick_file().and_then(|p| p.into_path().ok()).map(|p| dump::fwd(&p))
}

#[tauri::command]
async fn pick_mod_file(app: AppHandle) -> Option<String> {
    app.dialog().file().set_title("Choose an ExeFS patch")
        .add_filter("ExeFS patch", &["ips", "ips32", "pchtxt"])
        .blocking_pick_file().and_then(|p| p.into_path().ok()).map(|p| dump::fwd(&p))
}

fn mod_paths(app: &AppHandle) -> Result<mods::ModPaths, String> {
    let p = paths(&load_config(app))?;
    let script = match &p.package {
        Some(tools) => tools.join("build").join("modpack.py"),
        None => p.repo.join("tools").join("mods").join("modpack.py"),
    };
    Ok(mods::ModPaths {
        python: p.python(),
        script,
        main: p.exefs.join("main"),
        mods: p.game_dir.join("mods"),
    })
}

#[tauri::command]
async fn list_mods(app: AppHandle) -> Result<serde_json::Value, String> {
    let p = mod_paths(&app)?;
    tauri::async_runtime::spawn_blocking(move || mods::invoke(p, &["list".into()]))
        .await.map_err(|e| e.to_string())?
}

#[tauri::command]
async fn import_mod(app: AppHandle, state: State<'_, Arc<setup::Shared>>, source: String,
                    id: Option<String>, name: Option<String>) -> Result<serde_json::Value, String> {
    if state.progress.lock().unwrap().state == "running" {
        return Err("Setup is running. Pause it before importing a mod.".into());
    }
    let p = mod_paths(&app)?;
    let mut args = vec!["import".into(), source];
    if let Some(id) = id.filter(|v| !v.trim().is_empty()) {
        args.extend(["--id".into(), id]);
    }
    if let Some(name) = name.filter(|v| !v.trim().is_empty()) {
        args.extend(["--name".into(), name]);
    }
    tauri::async_runtime::spawn_blocking(move || mods::invoke(p, &args))
        .await.map_err(|e| e.to_string())?
}

#[tauri::command]
async fn set_mod_enabled(app: AppHandle, state: State<'_, Arc<setup::Shared>>,
                         id: String, enabled: bool) -> Result<serde_json::Value, String> {
    if state.progress.lock().unwrap().state == "running" {
        return Err("Setup is running. Pause it before changing mods.".into());
    }
    let p = mod_paths(&app)?;
    let args = vec![if enabled { "enable" } else { "disable" }.into(), id];
    tauri::async_runtime::spawn_blocking(move || mods::invoke(p, &args))
        .await.map_err(|e| e.to_string())?
}

#[derive(Serialize, Clone)]
struct NupProgress {
    done: u64,
    total: u64,
    file: String,
}

/// Unpacks a .nup (https://github.com/a55uka/nup) into `<repo>/game/nup_dump` (base/ and updates/<version>/, the layout of a dump
/// folder) and returns that folder. Emits "nup-progress". A finished unpack of the same package is reused.
#[tauri::command]
async fn import_nup(app: AppHandle, state: State<'_, Arc<setup::Shared>>, path: String) -> Result<String, String> {
    if state.progress.lock().unwrap().state == "running" {
        return Err("Setup is running. Pause it first.".into());
    }
    // held while the dump is written: a rebuild reads it
    let _lock = lock::acquire(&exe_dir())?;
    let cfg = load_config(&app);
    let repo = need_repo(&cfg)?;
    let out = if package_tools().is_some() { game_dir_of(&cfg, &repo).join("nup_dump") } else { repo.join("game").join("nup_dump") };
    let src = PathBuf::from(path.trim());
    let emitter = app.clone();
    let res = tauri::async_runtime::spawn_blocking(move || {
        let mut last = 0u64;
        nup::extract(
            &src,
            &out,
            &mut |done, total, file| {
                if total == 0 || done == total || done - last >= total / 100 {
                    last = done;
                    let _ = emitter.emit("nup-progress", NupProgress { done, total, file: file.to_string() });
                }
            },
            &|| false,
        )
    })
    .await
    .map_err(|e| e.to_string())??;
    Ok(dump::fwd(&res))
}

#[tauri::command]
fn validate_dump(app: AppHandle, path: String) -> Result<dump::DumpReport, String> {
    Ok(dump::scan(&path, &builds_of(&need_repo(&load_config(&app))?)))
}

#[tauri::command]
fn save_dump(app: AppHandle, path: String) -> Result<dump::DumpReport, String> {
    let mut cfg = load_config(&app);
    let report = dump::scan(&path, &builds_of(&need_repo(&cfg)?));
    if report.chosen.is_none() {
        return Err(report.errors.join(" "));
    }
    cfg.dump_dir = report.path.clone();
    save_config(app, cfg)?;
    Ok(report)
}

#[tauri::command]
fn read_settings(app: AppHandle) -> Result<settings::SettingsDump, String> {
    let cfg = load_config(&app);
    Ok(settings::read(&game_dir_of(&cfg, &need_repo(&cfg)?)))
}

#[tauri::command]
fn write_settings(app: AppHandle, edits: Vec<settings::Edit>) -> Result<(), String> {
    let _lock = lock::for_write(&exe_dir())?;
    let cfg = load_config(&app);
    settings::apply(&game_dir_of(&cfg, &need_repo(&cfg)?), &edits)
}

/// What one upscaler / frame generation feature finds (Settings): `found` only when every file it needs is in one folder.
#[derive(Serialize)]
struct FeatureFiles {
    found: bool,
    /// the folder the game will use (found), or the files missing and where they were looked for
    detail: String,
}

#[derive(Serialize)]
struct UpscalerFiles {
    fsr_upscaling: FeatureFiles,
    fsr_framegen: FeatureFiles,
    dlss: FeatureFiles,
    dlss_framegen: FeatureFiles,
    /// the Streamline folder the game uses (the one holding sl.interposer.dll), empty when none does: where a download has to go
    dlss_dir: String,
    /// NVIDIA files in the Streamline folder the game does not use (not needed by DLSS, nor by frame generation when it is off)
    dlss_unused: Vec<String>,
    xess: FeatureFiles,
}

/// The folders the game searches (runtime/src/gfx/extras/ffx_files.cpp, temporal_dlss.cpp, temporal_xess.cpp): the setting's folder (a file in it counts), then
/// `upscalers/<sub>` next to the game, next to the launcher and in the run folder.
fn upscaler_dirs(setting: &str, sub: &str, game: &Path, exe: &Path) -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = Vec::new();
    let s = setting.trim();
    if !s.is_empty() {
        let p = PathBuf::from(s);
        v.push(if p.is_file() { p.parent().map(|d| d.to_path_buf()).unwrap_or(p) } else { p });
    }
    for base in [exe.parent().map(|d| d.to_path_buf()).unwrap_or_default(), exe_dir(), game.to_path_buf()] {
        let d = base.join("upscalers").join(sub);
        if !v.contains(&d) {
            v.push(d);
        }
    }
    v
}

fn files_in(dirs: &[PathBuf], sets: &[&[&str]], what: &str) -> FeatureFiles {
    for d in dirs {
        for set in sets {
            if set.iter().all(|f| d.join(f).is_file()) {
                return FeatureFiles { found: true, detail: dump::fwd(d) };
            }
        }
    }
    // the first folder holding any of the files says which ones it lacks
    for d in dirs {
        let set = sets[0];
        let have: Vec<&str> = set.iter().copied().filter(|f| d.join(f).is_file()).collect();
        if !have.is_empty() {
            let lack: Vec<&str> = set.iter().copied().filter(|f| !d.join(f).is_file()).collect();
            return FeatureFiles { found: false, detail: format!("{} lacks {}", dump::fwd(d), lack.join(", ")) };
        }
    }
    FeatureFiles { found: false, detail: format!("{} not found in {}", what, dirs.iter().map(|d| dump::fwd(d)).collect::<Vec<_>>().join(", ")) }
}

#[tauri::command]
fn upscaler_files(
    app: AppHandle,
    fsr_dir: String,
    dlss_dir: String,
    xess_dir: Option<String>,
    frame_gen_dir: Option<String>,
) -> Result<UpscalerFiles, String> {
    upscaler_state(&load_config(&app), fsr_dir, dlss_dir, xess_dir, frame_gen_dir)
}

/// upscaler_files for these settings, also for a run without the window (headless.rs).
fn upscaler_state(
    cfg: &Config,
    fsr_dir: String,
    dlss_dir: String,
    xess_dir: Option<String>,
    frame_gen_dir: Option<String>,
) -> Result<UpscalerFiles, String> {
    let repo = need_repo(cfg)?;
    let game = game_dir_of(cfg, &repo);
    let exe = if package_tools().is_some() { game.join("fefw.exe") } else { setup::exe_path(&repo) };
    let with_extra_defaults = |mut v: Vec<PathBuf>| {
        if let Some(d) = exe.parent() {
            for extra in [d.join("upscalers").join("fsr3"), d.to_path_buf()] {
                if !v.contains(&extra) {
                    v.push(extra);
                }
            }
        }
        v
    };
    // ffx_files.cpp default_dirs also looks in upscalers/fsr3 and in the executable's own folder
    let fsr = with_extra_defaults(upscaler_dirs(&fsr_dir, "fsr", &game, &exe));
    // frame generation: [dlss] frame_gen_dll, else the FSR folder setting (never both), then the same defaults
    let fg_setting = frame_gen_dir.as_deref().filter(|s| !s.trim().is_empty()).unwrap_or(&fsr_dir);
    let fsr_fg = with_extra_defaults(upscaler_dirs(fg_setting, "fsr", &game, &exe));
    // Streamline: the game takes the setting's folder, else the first default folder holding sl.interposer.dll, and needs every file there
    let sl = upscaler_dirs(&dlss_dir, "streamline", &game, &exe);
    let sl_pick: Vec<PathBuf> = if !dlss_dir.trim().is_empty() {
        sl[..1].to_vec()
    } else {
        // none holds sl.interposer.dll: temporal_dlss.cpp sl_dir() then settles on the executable's own folder
        let mut all = sl.clone();
        if let Some(d) = exe.parent() {
            all.push(d.to_path_buf());
        }
        sl.iter().find(|d| d.join("sl.interposer.dll").is_file()).cloned().map(|d| vec![d]).unwrap_or(all)
    };
    let dlss_dir_used = sl_pick.iter().find(|d| d.join("sl.interposer.dll").is_file()).map(|d| dump::fwd(d)).unwrap_or_default();
    // XeSS (runtime/src/gfx/extras/temporal_xess.cpp): libxess.dll in the setting's folder, upscalers/xess, else next to the game
    let mut xess = upscaler_dirs(xess_dir.as_deref().unwrap_or(""), "xess", &game, &exe);
    if let Some(d) = exe.parent() {
        xess.push(d.to_path_buf());
    }
    const DLSS: [&str; 4] = ["sl.interposer.dll", "sl.common.dll", "sl.dlss.dll", "nvngx_dlss.dll"];
    const DLSS_G: [&str; 9] = [
        "sl.interposer.dll",
        "sl.common.dll",
        "sl.dlss.dll",
        "nvngx_dlss.dll",
        "sl.dlss_g.dll",
        "sl.reflex.dll",
        "sl.pcl.dll",
        "nvngx_dlssg.dll",
        "NvLowLatencyVk.dll",
    ];
    // Files of the Streamline SDK the game never asks for. Streamline skips the plugins, but NVIDIA's NGX still loads a stray nvngx_*.dll it finds
    // there (nvngx_deepdvc.dll). sl.pcl.dll stays: the game's Streamline needs it. The frame generation files are never listed: removing them
    // while frame generation is off breaks it the next time it is turned on.
    const DLSS_UNUSED: [&str; 7] =
        ["sl.deepdvc.dll", "sl.directsr.dll", "sl.dlss_d.dll", "sl.nis.dll", "sl.nvperf.dll", "nvngx_deepdvc.dll", "nvngx_dlssd.dll"];
    let dlss_unused: Vec<String> = match sl_pick.iter().find(|d| d.join("sl.interposer.dll").is_file()) {
        Some(d) => DLSS_UNUSED.iter().filter(|f| d.join(f).is_file()).map(|f| f.to_string()).collect(),
        None => Vec::new(),
    };
    Ok(UpscalerFiles {
        fsr_upscaling: files_in(&fsr, &[&["amd_fidelityfx_upscaler_vk.dll"], &["amd_fidelityfx_vk.dll"]], "amd_fidelityfx_upscaler_vk.dll"),
        fsr_framegen: files_in(&fsr_fg, &[&["amd_fidelityfx_vk.dll"]], "amd_fidelityfx_vk.dll (FidelityFX SDK v1.1.4)"),
        dlss: files_in(&sl_pick, &[&DLSS], "the Streamline files"),
        dlss_framegen: files_in(&sl_pick, &[&DLSS_G], "the Streamline frame generation files"),
        dlss_dir: dlss_dir_used,
        dlss_unused,
        xess: files_in(&xess, &[&["libxess.dll"]], "libxess.dll"),
    })
}

/// Downloads the files of one upscaler (fsr, xess, dlss, dlss_framegen) from the vendor into the folder set in Settings for it
/// (`dir`), else upscalers/<name> next to the launcher. The page asks the player first (the licence and the source are shown there); see fetch.rs.
#[tauri::command]
fn fetch_upscaler(app: AppHandle, state: State<Arc<fetch::Shared>>, kind: String, dir: Option<String>) -> Result<(), String> {
    // not begun under another process's job; not held on to, as Play need not wait for a download
    drop(lock::for_write(&exe_dir())?);
    {
        let mut s = state.lock();
        if s.state == "running" {
            return Err("A download is already running.".into());
        }
        // cleared under the lock that marks the run as started, so a Stop can neither be lost to it nor leak into this run from before
        state.cancel.store(false, Ordering::SeqCst);
        *s = fetch::Status { state: "running".into(), kind: kind.clone(), message: "Starting".into(), ..Default::default() };
    }
    let shared = state.inner().clone();
    let base = exe_dir();
    let dir = dir.unwrap_or_default();
    std::thread::spawn(move || {
        let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| fetch::run(&shared, &base, &kind, &dir)))
            .unwrap_or_else(|_| Err("The download stopped unexpectedly.".into()));
        let mut s = shared.lock();
        match res {
            Ok(m) => (s.state, s.message) = ("done".into(), m),
            Err(e) if e == fetch::CANCELLED => (s.state, s.message) = ("cancelled".into(), e),
            Err(e) => (s.state, s.message) = ("error".into(), e),
        }
        let _ = app.emit("fetch-status", s.clone());
    });
    Ok(())
}

/// The state of the download. A finished one (done, error, cancelled) is reported once and then the state is idle again,
/// so the page that is open when it ends, or the next one opened, tells the player how it went.
#[tauri::command]
fn fetch_status(state: State<Arc<fetch::Shared>>) -> fetch::Status {
    let mut st = state.lock();
    let mut s = st.clone();
    if s.state.is_empty() {
        s.state = "idle".into();
    }
    if matches!(s.state.as_str(), "done" | "error" | "cancelled") {
        *st = fetch::Status::default();
    }
    s
}

#[tauri::command]
fn cancel_fetch(state: State<Arc<fetch::Shared>>) {
    let s = state.lock();
    if s.state == "running" {
        state.cancel.store(true, Ordering::SeqCst);
    }
}

#[derive(Serialize)]
struct SetupStatus {
    progress: setup::Progress,
    steps: Vec<setup::StepState>,
    complete: bool,
    tools: Vec<setup::Tool>,
    log: Vec<String>,
    jobs: usize,
    /// running from the release package (no build tools needed)
    package: bool,
    /// why this PC's processor cannot run the compiled game (missing CMPXCHG16B / SSE4.2 / POPCNT), if it cannot
    cpu_problem: Option<String>,
    /// set when this processor gets the compatible build of the game code (no FMA3: cpu.rs)
    cpu_note: Option<String>,
}

#[tauri::command]
fn setup_status(app: AppHandle, state: State<Arc<setup::Shared>>) -> SetupStatus {
    let cfg = load_config(&app);
    let mut progress = state.progress.lock().unwrap().clone();
    if progress.state.is_empty() {
        progress.state = "idle".into();
    }
    let running = (progress.state == "running").then(|| progress.step.clone());
    let steps = paths(&cfg).map(|p| setup::steps(&p, running.as_deref())).unwrap_or_default();
    SetupStatus {
        complete: !steps.is_empty() && steps.iter().all(|s| s.state == "done"),
        steps,
        progress,
        tools: setup::check_tools(package_tools().is_some(), repo_of(&cfg).as_deref()),
        package: package_tools().is_some(),
        cpu_problem: cpu::check(),
        cpu_note: cpu::note(),
        log: state.log.lock().unwrap().iter().cloned().collect(),
        jobs: jobs_of(&cfg),
    }
}

#[tauri::command]
fn start_setup(app: AppHandle, state: State<Arc<setup::Shared>>) -> Result<(), String> {
    if let Some(why) = cpu::check() {
        return Err(why);
    }
    let cfg = load_config(&app);
    let p = paths(&cfg)?;
    let lock;
    {
        let mut pr = state.progress.lock().unwrap();
        if pr.state == "running" {
            return Err("Setup is already running.".into());
        }
        lock = lock::acquire(&exe_dir())?;
        *pr = setup::Progress { state: "running".into(), step: "python".into(), message: "Starting...".into(), ..Default::default() };
    }
    let shared = state.inner().clone();
    shared.cancel.store(false, Ordering::SeqCst);
    let jobs = jobs_of(&cfg);
    let runner = cfg.runner;
    std::thread::spawn(move || {
        let a1 = app.clone();
        let a2 = app.clone();
        let emit = move |p: setup::Progress| {
            let _ = a1.emit("setup-status", p);
        };
        let emit_log = move |lines: Vec<String>| {
            let _ = a2.emit("setup-log", lines);
        };
        let end = setup::run(&shared, &p, jobs, &runner, &emit, &emit_log);
        drop(lock); // before the page hears the end, and may start the next job
        let _ = app.emit("setup-status", end);
    });
    Ok(())
}

#[tauri::command]
fn cancel_setup(state: State<Arc<setup::Shared>>) {
    state.cancel.store(true, Ordering::SeqCst);
    setup::kill_tree(state.pid.load(Ordering::SeqCst));
}

#[tauri::command]
fn play(app: AppHandle, state: State<Arc<setup::Shared>>) -> Result<(), String> {
    if state.progress.lock().unwrap().state == "running" {
        return Err("Setup is running. Wait for it or pause it first.".into());
    }
    let cfg = load_config(&app);
    let p = paths(&cfg)?;
    if !setup::complete(&p) {
        return Err("Setup is not finished: open Setup and press Set up.".into());
    }
    // not while an update or a rebuild changes the files, and held until the game's process is there for them to see
    let lock = lock::acquire(&exe_dir())?;
    settings::ensure_play_paths(&p.game_dir, &p)?;
    let mut extra_env = Vec::new();
    if cfg.lsfg && cfg!(target_os = "linux") {
        let st = lsfg::status();
        if let Some(why) = lsfg::problem(&st) {
            return Err(why);
        }
        extra_env = lsfg::env(Path::new(&st.dll), cfg.lsfg_multiplier, &cfg.lsfg_flow_scale, cfg.lsfg_performance);
    }
    let report_opts = report::Options {
        config_dir: app.path().app_config_dir().ok(),
        dump_dir: cfg.dump_dir.clone(),
        launcher_dir: exe_dir(),
        launcher_version: env!("CARGO_PKG_VERSION").to_string(),
    };
    // a DualSense on a cable vibrates as strongly as its audio output is loud; on Windows the game sees to that itself
    let controls = std::fs::read_to_string(p.game_dir.join(settings::CONTROLS_INI)).ok();
    let volume = (cfg!(target_os = "linux") && pad_volume::wanted(controls.as_deref())).then(|| pad_volume_file(&app)).flatten();
    play::start(&app, &p.exe(), &p.game_dir, &exe_dir(), cfg.minimize_on_play, &cfg.runner, extra_env, lock, report_opts, volume)
}

/// Where the controller volumes to put back are kept (pad_volume.rs).
fn pad_volume_file(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_config_dir().ok().map(|d| d.join("pad-volume.json"))
}

/// Opens LSFG-VK's install instructions in the browser (Settings, when the layer is missing). A fixed address, nothing from the page.
#[tauri::command]
fn open_lsfg_install() -> Result<(), String> {
    play::open_with_shell(Path::new(lsfg::INSTALL_URL))
}

/// Linux: installs the LSFG-VK layer (Settings, when it is missing): CachyOS's package through Shelly, else LSFG-VK's build into ~/.local (lsfg.rs).
#[tauri::command]
async fn install_lsfg() -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(lsfg::install).await.map_err(|e| e.to_string())?
}

/// The OS the launcher runs on ("linux", "windows"). The page cannot tell from its user agent: WebKitGTK reports a macOS one.
#[tauri::command]
fn platform() -> &'static str {
    std::env::consts::OS
}

/// Linux: whether the LSFG-VK layer and Lossless Scaling's lsfg-vk.dll are there (Settings).
#[tauri::command]
async fn lsfg_status() -> lsfg::Status {
    tauri::async_runtime::spawn_blocking(lsfg::status).await.unwrap_or_default()
}

/// The variables a Steam shortcut puts in front of %command% (steam::launch_options): NXRT_LAUNCHER_DIR when the game does not find
/// the upscalers next to itself, and LSFG-VK's when it is on (Err: it is on but cannot be used, why).
fn steam_vars(cfg: &Config, game_dir: &Path) -> Result<Vec<(String, String)>, String> {
    let mut v = Vec::new();
    if game_dir != exe_dir() {
        v.push(("NXRT_LAUNCHER_DIR".to_string(), dump::game_path(&exe_dir())));
    }
    if cfg.lsfg {
        let st = lsfg::status();
        if let Some(why) = lsfg::problem(&st) {
            return Err(why);
        }
        v.extend(lsfg::env(Path::new(&st.dll), cfg.lsfg_multiplier, &cfg.lsfg_flow_scale, cfg.lsfg_performance));
    }
    Ok(v)
}

/// Linux: whether Steam is installed, the shortcut to fefw.exe exists, and it is out of date with the settings.
#[tauri::command]
async fn steam_status(app: AppHandle) -> steam::State {
    // off the main thread: it reads the dump folder and walks /proc
    if !cfg!(target_os = "linux") {
        return steam::State::default();
    }
    tauri::async_runtime::spawn_blocking(move || {
        let cfg = load_config(&app);
        match paths(&cfg) {
            // LSFG-VK on but unusable: compare with what it would be without it (Add to Steam then says why it cannot add it)
            Ok(p) => {
                let without = || steam_vars(&Config { lsfg: false, ..cfg.clone() }, &p.game_dir).unwrap_or_default();
                let vars = steam_vars(&cfg, &p.game_dir).unwrap_or_else(|_| without());
                steam::state(&p.exe(), &vars)
            }
            Err(_) => steam::State::default(),
        }
    })
    .await
    .unwrap_or_default()
}

/// Linux: adds fefw.exe to Steam as a non-Steam game. Steam has to be closed for that: when it runs, the player is asked first.
#[tauri::command]
async fn add_to_steam(app: AppHandle) -> Result<String, String> {
    use tauri_plugin_dialog::{MessageDialogButtons, MessageDialogKind};
    let cfg = load_config(&app);
    let p = paths(&cfg)?;
    let exe = p.exe();
    if !exe.is_file() {
        return Err("fefw.exe is not built yet. Run Set up first.".into());
    }
    let vars = steam_vars(&cfg, &p.game_dir)?;
    let close = if steam::state(&exe, &vars).running {
        app.dialog()
            .message(
                "Steam saves its game list when it closes, so it has to close while the launcher adds the game. It is started again right after.",
            )
            .title("Add to Steam")
            .kind(MessageDialogKind::Info)
            .buttons(MessageDialogButtons::OkCancelCustom("Close Steam and add".into(), "Cancel".into()))
            .blocking_show()
    } else {
        false
    };
    if steam::state(&exe, &vars).running && !close {
        return Err("Not added: Steam is still running.".into());
    }
    let game_dir = p.game_dir.clone();
    // what fefw.ini points the game at, for Flatpak Steam's sandbox
    let mut reads = vec![p.game_dir.clone(), p.romfs.clone(), p.plain_exefs(), exe_dir()];
    reads.extend(p.dlc.clone());
    tauri::async_runtime::spawn_blocking(move || steam::add(&exe, &game_dir, &vars, &reads, close)).await.map_err(|e| e.to_string())?
}

#[tauri::command]
fn game_status(state: State<play::PlayState>) -> play::Status {
    state.0.lock().unwrap().clone()
}

#[tauri::command]
fn open_game_file(app: AppHandle, name: String) -> Result<(), String> {
    if !play::REPORT_FILES.contains(&name.as_str()) {
        return Err("That file cannot be opened from here.".into());
    }
    let cfg = load_config(&app);
    let path = game_dir_of(&cfg, &need_repo(&cfg)?).join(&name);
    if !path.is_file() {
        return Err(format!("{name} does not exist."));
    }
    play::open_with_shell(&path)
}

#[tauri::command]
fn open_game_dir(app: AppHandle) -> Result<(), String> {
    let cfg = load_config(&app);
    let dir = game_dir_of(&cfg, &need_repo(&cfg)?);
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    play::open_with_shell(&dir)
}

/// Opens <run folder>/mods ([fs] mods), creating it: RomFS mods go in mods/<name>/romfs.
#[tauri::command]
fn open_mods_dir(app: AppHandle) -> Result<(), String> {
    let cfg = load_config(&app);
    let dir = game_dir_of(&cfg, &need_repo(&cfg)?).join("mods");
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    play::open_with_shell(&dir)
}

#[tauri::command]
fn open_setup_log(app: AppHandle) -> Result<(), String> {
    let cfg = load_config(&app);
    let repo = need_repo(&cfg)?;
    let log = if package_tools().is_some() { game_dir_of(&cfg, &repo).join("setup.log") } else { repo.join("work").join("setup.log") };
    if !log.is_file() {
        return Err("There is no setup log yet.".into());
    }
    play::open_with_shell(&log)
}

/// The graphics adapters by vendor (gpu.rs): Settings shows the NVIDIA-only options only with an NVIDIA GPU.
#[tauri::command]
async fn gpu_info() -> gpu::Gpus {
    tauri::async_runtime::spawn_blocking(gpu::get).await.unwrap_or_default()
}

/// The version updates are compared with: FEFW_BUILD_ID's when the build has one, else the launcher's `own`.
/// FEFW_LAUNCHER_PRETEND_VERSION: test the update path against the published release from an older-looking launcher.
fn current_version(own: String) -> String {
    std::env::var("FEFW_LAUNCHER_PRETEND_VERSION")
        .ok()
        .filter(|v| update::parse_version(v).is_some())
        .unwrap_or_else(|| update::BUILD_ID.map(String::from).unwrap_or(own))
}

#[tauri::command]
async fn check_update(app: AppHandle) -> Result<update::Check, String> {
    let current = current_version(app.package_info().version.to_string());
    let package = package_tools().is_some();
    tauri::async_runtime::spawn_blocking(move || update::check(&current, package)).await.map_err(|e| e.to_string())?
}

#[tauri::command]
fn update_status(state: State<Arc<update::Shared>>) -> update::Status {
    let mut s = state.status.lock().unwrap().clone();
    if s.state.is_empty() {
        s.state = "idle".into();
    }
    s
}

#[tauri::command]
fn start_update(
    app: AppHandle,
    state: State<Arc<update::Shared>>,
    setup_state: State<Arc<setup::Shared>>,
    play_state: State<play::PlayState>,
    release: update::Release,
) -> Result<(), String> {
    if package_tools().is_none() {
        return Err("Updates are for the release package. In a checkout, use git.".into());
    }
    if release.asset_url.is_empty() {
        return Err(format!("Release {} has no package for this system yet.", release.version));
    }
    if setup_state.progress.lock().unwrap().state == "running" {
        return Err("Setup is running. Wait for it or pause it first.".into());
    }
    if play_state.0.lock().unwrap().state == "running" {
        return Err("The game is running. Close it first.".into());
    }
    let lock;
    {
        let mut s = state.status.lock().unwrap();
        if s.state == "running" {
            return Err("An update is already running.".into());
        }
        lock = lock::acquire(&exe_dir())?;
        *s = update::Status {
            state: "running".into(),
            phase: "download".into(),
            message: "Starting".into(),
            version: release.version.clone(),
            ..Default::default()
        };
    }
    let cfg = load_config(&app);
    let install = exe_dir();
    let game_dir = repo_of(&cfg).map(|r| game_dir_of(&cfg, &r)).unwrap_or_else(|| install.clone());
    let shared = state.inner().clone();
    shared.cancel.store(false, Ordering::SeqCst);
    std::thread::spawn(move || {
        let a = app.clone();
        let emit = move |s: update::Status| {
            let _ = a.emit("update-status", s);
        };
        // the window's own game was checked above
        let run = update::Run { shared: &shared, install, game_dir, emit: &emit, ready: &|| Ok(()) };
        let end = match run.run(&release) {
            Ok(s) => s,
            Err(e) => update::Status { state: "error".into(), message: e, version: release.version.clone(), ..Default::default() },
        };
        drop(lock);
        *shared.status.lock().unwrap() = end.clone();
        let _ = app.emit("update-status", end);
    });
    Ok(())
}

#[tauri::command]
fn cancel_update(state: State<Arc<update::Shared>>) {
    state.cancel.store(true, Ordering::SeqCst);
}

/// Starts the (new) launcher from the install folder and closes this one.
#[tauri::command]
fn restart_launcher(app: AppHandle) -> Result<(), String> {
    let dir = exe_dir();
    let exe = if !cfg!(windows) {
        // After an in-launcher update, update::install() places the new fefw-launcher.AppImage in `dir` even if we were
        // started from an unpacked AppRun, so prefer it over the stale extracted AppRun.
        if dir.join("fefw-launcher.AppImage").is_file() {
            dir.join("fefw-launcher.AppImage")
        } else if let Some(a) = std::env::var_os("APPIMAGE").filter(|a| !a.is_empty()) {
            PathBuf::from(a)
        } else if dir.join("AppRun").is_file() {
            dir.join("AppRun")
        } else {
            dir.join("fefw-launcher")
        }
    } else {
        dir.join("fefw-launcher.exe")
    };
    let mut c = std::process::Command::new(&exe);
    setup::host_env(&mut c); // not this AppImage's environment: its mount goes away when this launcher exits
    c.current_dir(&dir).spawn().map_err(|e| format!("cannot start {}: {e}", exe.display()))?;
    app.exit(0);
    Ok(())
}

/// The value of `--name value` or `--name=value`.
fn arg_value(args: &[String], name: &str) -> Option<String> {
    let eq = format!("{name}=");
    let mut it = args.iter();
    while let Some(a) = it.next() {
        if a == name {
            return it.next().cloned();
        }
        if let Some(v) = a.strip_prefix(&eq) {
            return Some(v.to_string());
        }
    }
    None
}

/// One line on stdout, for the program that started us. Never panics when there is no console.
fn out_line(s: &str) {
    let mut o = std::io::stdout();
    let _ = writeln!(o, "{s}");
    let _ = o.flush();
}

/// One line on stderr, and the exit code for a failed --prepare.
fn prepare_fail(msg: &str) -> i32 {
    let line = msg.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or("Setup failed.");
    let _ = writeln!(std::io::stderr(), "{line}");
    1
}

/// --prepare --game <dump folder or .nup> [--dlc <folder of add-ons>]: the same setup the Setup page runs (setup::run), without a window.
/// Prints `progress <0..1> <text>` lines; returns the exit code.
fn prepare_headless(game: Option<String>, dlc: Option<String>) -> i32 {
    let game = match game.filter(|g| !g.trim().is_empty()) {
        Some(g) => g,
        None => return prepare_fail("--prepare needs --game <dump folder or .nup file>."),
    };
    if let Some(why) = cpu::check() {
        return prepare_fail(&why);
    }
    let mut dlc_dir = None;
    if let Some(d) = dlc.filter(|d| !d.trim().is_empty()) {
        let d = match std::path::absolute(d.trim()) {
            Ok(d) => d,
            Err(e) => return prepare_fail(&format!("Cannot use the DLC folder {}: {e}", d.trim())),
        };
        let ours = |id: &String| u64::from_str_radix(id, 16).is_ok_and(|id| id & !0xFFF == setup::PROGRAM_ID + 0x1000 && id & 0xFFF != 0);
        if !dump::scan_dlc(&d).iter().any(ours) {
            return prepare_fail(&format!(
                "No add-ons for this game found in {}. Pick the folder that holds one folder per title id, each with a romfs folder in it.",
                d.display()
            ));
        }
        dlc_dir = Some(d);
    }
    let _lock = match lock::acquire(&exe_dir()) {
        Ok(l) => l,
        Err(e) => return prepare_fail(&e),
    };
    refresh_package_locales();
    let mut cfg = Config { dump_dir: game.trim().to_string(), ..Default::default() };
    let mut base = 0.0f64;
    let src = PathBuf::from(&cfg.dump_dir);
    if src.is_file() && src.extension().map(|e| e.eq_ignore_ascii_case("nup")).unwrap_or(false) {
        // same as the Setup page's .nup button: unpack into nup_dump and use that folder
        let repo = match need_repo(&cfg) {
            Ok(r) => r,
            Err(e) => return prepare_fail(&e),
        };
        let out = if package_tools().is_some() { game_dir_of(&cfg, &repo).join("nup_dump") } else { repo.join("game").join("nup_dump") };
        let mut last = -1.0f64;
        let res = nup::extract(
            &src,
            &out,
            &mut |done, total, _file| {
                if total > 0 {
                    let f = 0.1 * (done as f64 / total as f64).min(1.0);
                    if f - last >= 0.005 {
                        last = f;
                        out_line(&format!("progress {:.3} Unpacking the .nup package", f));
                    }
                }
            },
            &|| false,
        );
        match res {
            Ok(d) => {
                cfg.dump_dir = dump::fwd(&d);
                base = 0.1;
            }
            Err(e) => return prepare_fail(&e),
        }
    }
    let mut p = match paths(&cfg) {
        Ok(p) => p,
        Err(e) => return prepare_fail(&e),
    };
    if dlc_dir.is_some() {
        p.dlc = dlc_dir;
    }
    let shared = Arc::new(setup::Shared::default());
    *shared.progress.lock().unwrap() =
        setup::Progress { state: "running".into(), step: "python".into(), message: "Starting...".into(), ..Default::default() };
    shared.cancel.store(false, Ordering::SeqCst);
    let jobs = jobs_of(&cfg);
    let overall = headless::Overall::new(&p, base);
    let emit = move |pr: setup::Progress| {
        let f = overall.at(&pr);
        let text = pr.message.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or(pr.step.as_str()).to_string();
        out_line(&format!("progress {f:.3} {text}"));
    };
    let emit_log = move |_lines: Vec<String>| {};
    let end = setup::run(&shared, &p, jobs, &cfg.runner, &emit, &emit_log);
    if end.state != "done" {
        let why = if end.message.trim().is_empty() { "Setup did not finish." } else { end.message.as_str() };
        return prepare_fail(why);
    }
    if !setup::complete(&p) {
        return prepare_fail("Setup ended, but the game is not ready. See setup.log.");
    }
    // what the Play button does before it starts fefw.exe: point fefw.ini at the dump
    if let Err(e) = settings::ensure_play_paths(&p.game_dir, &p) {
        return prepare_fail(&e);
    }
    out_line("progress 1 Ready to play");
    0
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let Some(cmd) = headless::command(&args) {
        std::process::exit(headless::run(cmd));
    }
    let game_arg = arg_value(&args, "--game");
    if args.iter().any(|a| a == "--prepare") {
        std::process::exit(prepare_headless(game_arg, arg_value(&args, "--dlc")));
    }
    if let Some(g) = game_arg.filter(|g| PathBuf::from(g.trim()).is_dir()) {
        let _ = GAME_ARG.set(g.trim().to_string());
    }
    if package_tools().is_some() {
        // not under an update that is running: these are its files
        if let Ok(_lock) = lock::acquire(&exe_dir()) {
            update::cleanup(&exe_dir());
        }
    }
    // probed now, off the main thread, so the Settings page finds the answer ready (PowerShell takes a moment on Windows)
    std::thread::spawn(gpu::get);
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(play::PlayState::default())
        .manage(Arc::new(setup::Shared::default()))
        .manage(Arc::new(update::Shared::default()))
        .manage(Arc::new(fetch::Shared::default()))
        .setup(|app| {
            // a controller volume an earlier session could not put back; not while a game it was raised for may still run
            if let Some(file) = pad_volume_file(app.handle()).filter(|_| cfg!(target_os = "linux")) {
                if !headless::game_running(&headless::install_dirs(&load_config(app.handle()))) {
                    pad_volume::recover(file);
                }
            }
            // at every start, so that an update or a new default also reaches a game started without Play; the package first,
            // as the run folder takes its tables from there
            // not under another process's setup or update: Set up and Play bring the run folder in step
            if let Ok(_lock) = lock::acquire(&exe_dir()) {
                refresh_package_locales();
                if let Err(e) = sync_run_folder(&load_config(app.handle())) {
                    eprintln!("[run folder] {e}");
                }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // closing the window pauses a running setup rather than leaving its compilers behind
            if let tauri::WindowEvent::Destroyed = event {
                let st = window.state::<Arc<setup::Shared>>();
                st.cancel.store(true, Ordering::SeqCst);
                setup::kill_tree(st.pid.load(Ordering::SeqCst));
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_config,
            save_config,
            set_send_reports,
            decky_offer,
            open_decky_installer,
            decline_decky,
            ui_language,
            memory_warning,
            set_language,
            get_overview,
            pick_folder,
            pick_nup,
            pick_mod_file,
            list_mods,
            import_mod,
            set_mod_enabled,
            import_nup,
            validate_dump,
            save_dump,
            read_settings,
            write_settings,
            upscaler_files,
            gpu_info,
            fetch_upscaler,
            fetch_status,
            cancel_fetch,
            setup_status,
            start_setup,
            cancel_setup,
            play,
            platform,
            lsfg_status,
            open_lsfg_install,
            install_lsfg,
            game_status,
            steam_status,
            add_to_steam,
            open_game_file,
            open_game_dir,
            open_mods_dir,
            open_setup_log,
            check_update,
            update_status,
            start_update,
            cancel_update,
            restart_launcher,
            locale::list_locales,
            locale::read_locale,
        ])
        .run(tauri::generate_context!())
        .expect("error while running the launcher");
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;

    #[test]
    fn resolve_exe_dir_handles_unpacked_and_mounted_appimage() {
        let base = std::env::temp_dir().join(format!("fefw-exe-dir-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let install = base.join("install");
        std::fs::create_dir_all(install.join("tools/build")).unwrap();
        std::fs::write(install.join("tools/build/fefw_build.py"), "").unwrap();
        let unpacked_exe = install.join("usr/bin/fefw-launcher");
        std::fs::create_dir_all(unpacked_exe.parent().unwrap()).unwrap();
        std::fs::write(&unpacked_exe, "").unwrap();

        // 1. Unpacked AppImage (RecompLauncher): $APPIMAGE unset
        assert_eq!(resolve_exe_dir(Some(&unpacked_exe), Some(&install), None), install);
        // 2. Unpacked AppImage with a leaked parent $APPIMAGE (e.g. RecompLauncher's own AppImage)
        let foreign_appimage = base.join("other/RecompLauncher.AppImage");
        assert_eq!(resolve_exe_dir(Some(&unpacked_exe), None, Some(&foreign_appimage)), install);
        // 3. Mounted .AppImage (/tmp/.mount_.../usr/bin/fefw-launcher)
        let mount = base.join("mount");
        let mounted_exe = mount.join("usr/bin/fefw-launcher");
        let own_appimage = install.join("fefw-launcher.AppImage");
        assert_eq!(resolve_exe_dir(Some(&mounted_exe), Some(&mount), Some(&own_appimage)), install);
        // 4. Dev build (no tools/build/fefw_build.py, no $APPIMAGE)
        let dev_exe = base.join("checkout/target/debug/fefw-launcher");
        assert_eq!(resolve_exe_dir(Some(&dev_exe), None, None), base.join("checkout/target/debug"));

        let _ = std::fs::remove_dir_all(&base);
    }
}
