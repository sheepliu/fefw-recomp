// SPDX-License-Identifier: GPL-3.0-or-later
//! The launcher without its window, for a program that drives it, such as a Steam Deck plugin (launcher/README.md): --status,
//! --check-update, --update, --rebuild, --settings, --set, --fetch-upscaler and --add-to-steam. They use the saved settings (launcher.json) as the window does. Every line on stdout is
//! one JSON object: any number with "event": "progress", then one with "event": "end" and "state": "done", "error" or "cancelled".
//! Exit code 0 for "done", else 1. A line "cancel" on stdin stops --update or --rebuild.

use crate::{cpu, fetch, gpu, ini::Ini, lock, manifest, settings, setup, steam, update, Config};
use serde_json::{json, Value};
use std::fs;
use std::io::BufRead;
use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};

/// Goes up when a command or a field a caller relies on changes.
pub const API: u32 = 1;

const GAME_RUNNING: &str = "The game is running. Close it first.";
const TAURI_CONF: &str = include_str!("../tauri.conf.json");

#[derive(Clone, PartialEq, Debug)]
pub enum Command {
    Status,
    CheckUpdate,
    Update,
    Rebuild,
    Settings,
    /// "id=choice"
    Set(String),
    /// the kind, and whether the licence was accepted
    Fetch(String, bool),
    /// `before_setup`: also when fefw.exe is not built yet. `close_steam`: a running Steam may be shut down (and is started again).
    AddToSteam {
        before_setup: bool,
        close_steam: bool,
    },
}

pub fn command(args: &[String]) -> Option<Command> {
    let flag = |name: &str| args.iter().any(|a| a == name);
    let value = |i: usize| args.get(i + 1).cloned().unwrap_or_default();
    args.iter().enumerate().find_map(|(i, a)| match a.as_str() {
        "--status" => Some(Command::Status),
        "--check-update" => Some(Command::CheckUpdate),
        "--update" => Some(Command::Update),
        "--rebuild" => Some(Command::Rebuild),
        "--settings" => Some(Command::Settings),
        "--set" => Some(Command::Set(value(i))),
        "--fetch-upscaler" => Some(Command::Fetch(value(i), flag("--accept-licence"))),
        "--add-to-steam" => Some(Command::AddToSteam { before_setup: flag("--before-setup"), close_steam: flag("--close-steam") }),
        _ => None,
    })
}

/// Runs one command, prints its lines and returns the exit code.
pub fn run(cmd: Command) -> i32 {
    let result = saved_config().and_then(|cfg| match cmd {
        Command::Status => Ok(status(&cfg)),
        Command::CheckUpdate => check_update(),
        Command::Update => run_update(&cfg),
        Command::Rebuild => rebuild(&cfg),
        Command::Settings => list_settings(&cfg),
        Command::Set(pick) => set_setting(&cfg, &pick),
        Command::Fetch(kind, accepted) => fetch_upscaler(&cfg, &kind, accepted),
        Command::AddToSteam { before_setup, close_steam } => add_to_steam(&cfg, before_setup, close_steam),
    });
    let end = result.unwrap_or_else(|message| json!({ "state": "error", "message": message }));
    let code = if end["state"] == "done" { 0 } else { 1 };
    emit("end", end);
    code
}

/// One line: `fields` (an object) with the event and the interface version.
fn emit(event: &str, mut fields: Value) {
    fields["event"] = event.into();
    fields["api"] = API.into();
    crate::out_line(&fields.to_string());
}

fn conf(key: &str) -> String {
    serde_json::from_str::<Value>(TAURI_CONF).ok().and_then(|v| v[key].as_str().map(String::from)).unwrap_or_default()
}

/// The folder Tauri's app_config_dir() is under, found without a running app.
fn config_base(var: impl Fn(&str) -> Option<PathBuf>) -> Option<PathBuf> {
    if cfg!(windows) {
        var("APPDATA")
    } else {
        var("XDG_CONFIG_HOME").filter(|p| p.is_absolute()).or_else(|| var("HOME").map(|h| h.join(".config")))
    }
}

/// launcher.json, the settings the window saved. None there yet: the defaults. One that cannot be read is an error, not the defaults.
fn saved_config() -> Result<Config, String> {
    let base = config_base(|k| std::env::var_os(k).filter(|v| !v.is_empty()).map(PathBuf::from)).ok_or("No settings folder: HOME is not set.")?;
    read_config(&base.join(conf("identifier")).join("launcher.json"))
}

fn read_config(path: &Path) -> Result<Config, String> {
    match fs::read_to_string(path) {
        Ok(text) => serde_json::from_str(&text).map_err(|e| format!("cannot read {}: {e}", path.display())),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Config::default()),
        Err(e) => Err(format!("cannot read {}: {e}", path.display())),
    }
}

/// Whether `a` and `b` are one folder, also through links.
fn same_dir(a: &Path, b: &Path) -> bool {
    let real = |p: &Path| fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf());
    a == b || real(a) == real(b)
}

/// Whether the folder of a Windows path as Wine shows it ("Z:/a/b", "S:/b") is one of `dirs`. Z: is the whole file system. What
/// another letter stands for is the prefix's business, so there the rest of the path has to be the end of the folder's.
fn wine_dir_is(drive: char, rest: &str, dirs: &[PathBuf]) -> bool {
    if drive.eq_ignore_ascii_case(&'z') {
        return dirs.iter().any(|d| same_dir(Path::new(&format!("/{rest}")), d));
    }
    let parts = |s: &str| s.split(['/', '\\']).filter(|x| !x.is_empty()).map(str::to_lowercase).collect::<Vec<_>>();
    let tail = parts(rest);
    dirs.iter().any(|d| {
        let real = fs::canonicalize(d).unwrap_or_else(|_| d.clone());
        [d, &real].iter().any(|d| parts(&d.to_string_lossy()).ends_with(&tail))
    })
}

/// Whether a process's command line (/proc/<pid>/cmdline) names the fefw.exe of one of `dirs`: the game itself, or Wine or Proton
/// about to start it. `cwd`: the process's working folder, for a name without a full path; unknown, such a name counts.
fn names_game(cmdline: &[u8], cwd: Option<&Path>, dirs: &[PathBuf]) -> bool {
    cmdline.split(|&b| b == 0).any(|arg| {
        let arg = String::from_utf8_lossy(arg).replace('\\', "/");
        let (folder, name) = arg.rsplit_once('/').unwrap_or(("", &arg));
        if !name.eq_ignore_ascii_case("fefw.exe") {
            return false;
        }
        let mut chars = folder.chars();
        if let (Some(drive @ ('a'..='z' | 'A'..='Z')), Some(':')) = (chars.next(), chars.next()) {
            return wine_dir_is(drive, chars.as_str().trim_start_matches('/'), dirs);
        }
        let folder = if arg.starts_with('/') {
            Path::new("/").join(folder.trim_start_matches('/'))
        } else {
            match cwd {
                Some(c) => c.join(folder),
                None => return true,
            }
        };
        dirs.iter().any(|d| same_dir(&folder, d))
    })
}

/// Windows: a running program's file cannot be opened for writing (a sharing violation). Nothing is written.
fn in_use(exe: &Path) -> bool {
    const SHARING_VIOLATION: i32 = 32;
    cfg!(windows) && fs::OpenOptions::new().write(true).open(exe).is_err_and(|e| e.raw_os_error() == Some(SHARING_VIOLATION))
}

/// Whether the fefw.exe of one of `dirs` runs, however it was started (Steam, a window of the launcher). Another install's does not
/// count.
pub(crate) fn game_running(dirs: &[PathBuf]) -> bool {
    if cfg!(windows) {
        return dirs.iter().any(|d| in_use(&d.join("fefw.exe")));
    }
    if !cfg!(target_os = "linux") {
        return false;
    }
    let Ok(rd) = fs::read_dir("/proc") else { return false };
    rd.flatten().filter(|e| e.file_name().to_string_lossy().parse::<u32>().is_ok()).any(|e| {
        let cwd = fs::read_link(e.path().join("cwd")).ok();
        fs::read(e.path().join("cmdline")).is_ok_and(|c| names_game(&c, cwd.as_deref(), dirs))
    })
}

/// Where this install's fefw.exe and the files it runs with are: the launcher's folder and the run folder, and in a checkout the
/// build's.
pub(crate) fn install_dirs(cfg: &Config) -> Vec<PathBuf> {
    let mut dirs = vec![crate::exe_dir()];
    if let Some(repo) = crate::repo_of(cfg) {
        dirs.push(crate::game_dir_of(cfg, &repo));
        if crate::package_tools().is_none() {
            dirs.extend(setup::exe_path(&repo).parent().map(Path::to_path_buf));
        }
    }
    dirs
}

fn idle(dirs: &[PathBuf]) -> Result<(), String> {
    if game_running(dirs) {
        Err(GAME_RUNNING.into())
    } else {
        Ok(())
    }
}

/// Calls `stop` when the caller writes "cancel" on stdin. The end of stdin is not a cancel: a caller may give none.
fn on_cancel(stop: impl Fn() + Send + 'static) {
    std::thread::spawn(move || {
        for line in std::io::stdin().lock().lines() {
            match line {
                Ok(l) if l.trim() == "cancel" => stop(),
                Ok(_) => {}
                Err(_) => break,
            }
        }
    });
}

fn status(cfg: &Config) -> Value {
    let install = crate::exe_dir();
    let package = crate::package_tools().is_some();
    let busy = if game_running(&install_dirs(cfg)) {
        Some("game")
    } else if lock::held(&install) {
        Some("job")
    } else {
        None
    };
    let game_dir = crate::repo_of(cfg).map(|r| crate::game_dir_of(cfg, &r));
    let paths = cpu::check().map_or_else(|| crate::paths(cfg), Err);
    let exe = match &paths {
        Ok(p) => Some(p.exe()),
        // without a usable dump the package's exe is still where it is built
        Err(_) => game_dir.as_ref().filter(|_| package).map(|d| d.join("fefw.exe")),
    };
    let (rebuild, reason) = match &game_dir {
        Some(d) if package => update::rebuild_needed(&install, d),
        _ => (None, String::new()),
    };
    let shown = |p: &Path| crate::dump::fwd(p);
    let shortcuts = exe.as_deref().map(crate::steam::shortcut_ids).unwrap_or_default();
    json!({
        "state": "done",
        "version": crate::current_version(conf("version")),
        "package": package,
        "install_dir": shown(&install),
        "game_dir": game_dir.as_deref().map(shown),
        "dump_dir": cfg.dump_dir,
        "busy": busy,
        "exe_exists": exe.is_some_and(|e| e.is_file()),
        "setup_complete": paths.as_ref().is_ok_and(setup::complete),
        "problem": paths.err(),
        "rebuild": rebuild,
        "rebuild_reason": reason,
        "steam_shortcuts": shortcuts,
    })
}

fn check_update() -> Result<Value, String> {
    let c = update::check(&crate::current_version(conf("version")), crate::package_tools().is_some())?;
    Ok(json!({ "state": "done", "current": c.current, "newer": c.newer, "package": c.package, "latest": c.latest }))
}

fn run_update(cfg: &Config) -> Result<Value, String> {
    if crate::package_tools().is_none() {
        return Err("Updates are for the release package. In a checkout, use git.".into());
    }
    let install = crate::exe_dir();
    let _lock = lock::acquire(&install)?;
    let dirs = install_dirs(cfg);
    idle(&dirs)?;
    let game_dir = crate::repo_of(cfg).map(|r| crate::game_dir_of(cfg, &r)).unwrap_or_else(|| install.clone());
    let check = update::check(&crate::current_version(conf("version")), true)?;
    let release = check.latest.filter(|_| check.newer);
    let Some(release) = release else {
        // an earlier update may still wait for its rebuild
        let (rebuild, reason) = update::rebuild_needed(&install, &game_dir);
        return Ok(json!({
            "state": "done",
            "updated": false,
            "version": check.current,
            "rebuild": rebuild,
            "rebuild_reason": reason,
            "message": "This is the latest version.",
        }));
    };
    if release.asset_url.is_empty() {
        return Err(format!("Release {} has no package for this system yet.", release.version));
    }
    let shared = Arc::new(update::Shared::default());
    let s = shared.clone();
    on_cancel(move || s.cancel.store(true, Ordering::SeqCst));
    let progress = |s: update::Status| {
        emit("progress", json!({ "phase": s.phase, "done": s.done, "total": s.total, "message": s.message }));
    };
    // the download takes a while: the game may have been started meanwhile
    let ready = || idle(&dirs);
    let job = update::Run { shared: &shared, install, game_dir, emit: &progress, ready: &ready };
    match job.run(&release) {
        Ok(s) => Ok(json!({
            "state": "done",
            "updated": true,
            "version": s.version,
            "rebuild": s.rebuild,
            "rebuild_reason": s.rebuild_reason,
            "message": format!("Version {} is installed.", release.version),
        })),
        Err(message) if message == update::CANCELLED => Ok(json!({ "state": "cancelled", "message": message })),
        Err(message) => Err(message),
    }
}

/// The run folder's fefw.ini and the manifest's settings. Before Set up there is no ini to change.
fn settings_and_ini(cfg: &Config) -> Result<(Vec<manifest::Setting>, Ini, PathBuf), String> {
    let repo = crate::need_repo(cfg)?;
    let game_dir = crate::game_dir_of(cfg, &repo);
    // the package ships the manifest beside the launcher, wherever the run folder is; a checkout has it at its root
    let path = if crate::package_tools().is_some() { crate::exe_dir() } else { repo }.join("recomplauncher.json");
    let all = manifest::parse(&fs::read_to_string(&path).map_err(|e| format!("cannot read {}: {e}", path.display()))?)?;
    let ini = fs::read(game_dir.join(settings::GAME_INI)).map_err(|_| "The game has no settings yet: run Set up first.".to_string())?;
    Ok((all, Ini::parse(&String::from_utf8_lossy(&ini)), game_dir))
}

/// Whether a caller may offer this value of [dlss] upscaler, and the download that has to come first (--fetch-upscaler).
fn upscaler_choice(value: &str, files: &crate::UpscalerFiles, nvidia: bool) -> (bool, Option<&'static str>) {
    match value {
        "fsr3" => (true, (!files.fsr_upscaling.found).then_some("fsr")),
        "xess" => (true, (!files.xess.found).then_some("xess")),
        // NVIDIA's files are downloaded in the window only
        "dlss" => (nvidia && files.dlss.found, None),
        _ => (true, None),
    }
}

/// Whether a caller may offer this value of [dlss] frame_gen, by the Settings page's rules: the game's own FSR 3 frame generation
/// does not work under Proton, so it is not offered on Linux, and DLSS frame generation needs an NVIDIA GPU and Streamline's files.
fn frame_gen_choice(value: &str, dlss_framegen_found: bool, nvidia: bool, windows: bool) -> bool {
    match value {
        "fsr3" => windows,
        "dlss" => nvidia && dlss_framegen_found,
        _ => true,
    }
}

/// A folder (or file) named in fefw.ini, as a path of this system. The game reads it under Windows or Wine: either slash, on Linux
/// drive Z: for the file system's root, and a relative path from the run folder. Another drive is one only the game sees. "" stays.
fn host_path(setting: &str, game_dir: &Path, windows: bool) -> Result<String, String> {
    let s = setting.trim().replace('\\', "/");
    if s.is_empty() {
        return Ok(s);
    }
    let drive = s.chars().next().filter(char::is_ascii_alphabetic).filter(|_| s[1..].starts_with(':'));
    match drive {
        Some(_) if windows => Ok(s),
        Some('z' | 'Z') if s[2..].starts_with('/') => Ok(s[2..].to_string()),
        Some(_) => Err(format!("The folder {s} in fefw.ini is on a drive only the game sees: change it in the launcher's Settings.")),
        None if s.starts_with('/') => Ok(s),
        None => Ok(crate::dump::fwd(&game_dir.join(s))),
    }
}

fn upscaler_files(cfg: &Config, ini: &Ini, game_dir: &Path) -> Result<crate::UpscalerFiles, String> {
    // a folder that cannot be found from here is looked for as written, and so shows as missing: the download then says why
    let dir = |key: &str| {
        let raw = ini.get("upscale", key).unwrap_or_default();
        host_path(&raw, game_dir, cfg!(windows)).unwrap_or(raw)
    };
    crate::upscaler_state(cfg, dir("fsr_dll"), dir("dlss_dll"), Some(dir("xess_dll")), None)
}

fn list_settings(cfg: &Config) -> Result<Value, String> {
    let (all, ini, game_dir) = settings_and_ini(cfg)?;
    let files = upscaler_files(cfg, &ini, &game_dir)?;
    let nvidia = gpu::get().nvidia;
    let shown: Vec<Value> = all
        .iter()
        .map(|s| {
            let choices: Vec<Value> = s
                .choices
                .iter()
                .map(|c| {
                    let (mut available, download) = c.set.get("dlss/upscaler").map_or((true, None), |v| upscaler_choice(v, &files, nvidia));
                    if let Some(v) = c.set.get("dlss/frame_gen") {
                        available &= frame_gen_choice(v, files.dlss_framegen.found, nvidia, cfg!(windows));
                    }
                    json!({ "label": c.label, "available": available, "download": download })
                })
                .collect();
            json!({ "id": s.id, "label": s.label, "help": s.help, "group": s.group, "current": manifest::current(s, &ini).map(|c| &c.label), "choices": choices })
        })
        .collect();
    let consent: serde_json::Map<String, Value> = ["fsr", "xess"].iter().filter_map(|k| Some((k.to_string(), fetch::consent(k)?.into()))).collect();
    Ok(json!({ "state": "done", "settings": shown, "consent": consent }))
}

/// Writes one choice into fefw.ini, as the Settings page does. The game reads these when it starts.
fn set_setting(cfg: &Config, pick: &str) -> Result<Value, String> {
    let (id, label) = pick.split_once('=').ok_or("--set needs id=choice.")?;
    let _lock = lock::acquire(&crate::exe_dir())?;
    let (all, ini, game_dir) = settings_and_ini(cfg)?;
    let setting = all.iter().find(|s| s.id == id).ok_or_else(|| format!("There is no setting \"{id}\"."))?;
    settings::apply(&game_dir, &manifest::edits(setting, label, &ini)?)?;
    Ok(json!({ "state": "done", "message": format!("{}: {label}", setting.label) }))
}

/// The Settings page's Download button, for a caller that showed the player the consent text and had it accepted.
fn fetch_upscaler(cfg: &Config, kind: &str, accepted: bool) -> Result<Value, String> {
    let Some(consent) = fetch::consent(kind) else {
        return Err(format!("\"{kind}\" cannot be downloaded without the window."));
    };
    if !accepted {
        return Err(format!("{consent} Accept it with --accept-licence."));
    }
    let install = crate::exe_dir();
    let _lock = lock::acquire(&install)?;
    let (_, ini, game_dir) = settings_and_ini(cfg)?;
    // into the folder set for this upscaler, as the game looks there first
    let custom = host_path(&ini.get("upscale", &format!("{kind}_dll")).unwrap_or_default(), &game_dir, cfg!(windows))?;
    let shared = Arc::new(fetch::Shared::default());
    let s = shared.clone();
    on_cancel(move || s.cancel.store(true, Ordering::SeqCst));
    let result = std::thread::scope(|scope| {
        let job = scope.spawn(|| fetch::run(&shared, &install, kind, &custom));
        let mut said = String::new();
        while !job.is_finished() {
            let now = shared.lock().clone();
            if !now.message.is_empty() && now.message != said {
                emit("progress", json!({ "phase": "download", "done": now.done, "total": now.total, "message": now.message }));
                said = now.message;
            }
            std::thread::sleep(std::time::Duration::from_millis(250));
        }
        job.join().unwrap_or_else(|_| Err("The download stopped unexpectedly.".into()))
    });
    match result {
        Ok(message) => Ok(json!({ "state": "done", "message": message })),
        Err(message) if message == fetch::CANCELLED => Ok(json!({ "state": "cancelled", "message": message })),
        Err(message) => Err(message),
    }
}

/// The Play page's Add to Steam. An installer may add the game before it is built: Play fails until it is, and Steam need not be
/// closed a second time.
fn add_to_steam(cfg: &Config, before_setup: bool, close_steam: bool) -> Result<Value, String> {
    let repo = crate::need_repo(cfg)?;
    let game_dir = crate::game_dir_of(cfg, &repo);
    let exe = if crate::package_tools().is_some() { game_dir.join("fefw.exe") } else { setup::exe_path(&repo) };
    if !before_setup && !exe.is_file() {
        return Err("fefw.exe is not built yet. Run Set up first.".into());
    }
    let _lock = lock::acquire(&crate::exe_dir())?;
    fs::create_dir_all(&game_dir).map_err(|e| format!("cannot create {}: {e}", game_dir.display()))?;
    let vars = crate::steam_vars(cfg, &game_dir)?;
    // what fefw.ini points the game at, for Flatpak Steam's sandbox; the dump only once one is chosen
    let mut reads = vec![game_dir.clone(), crate::exe_dir()];
    if let Ok(p) = crate::paths(cfg) {
        reads.extend([p.romfs.clone(), p.plain_exefs()]);
        reads.extend(p.dlc);
    }
    let message = steam::add(&exe, &game_dir, &vars, &reads, close_steam)?;
    Ok(json!({ "state": "done", "message": message, "steam_shortcuts": steam::shortcut_ids(&exe) }))
}

/// The parts fefw_build.py runs, in order, each counting from 0 again: the build step's share is split between them, so that a part
/// that cannot count (linking says 0 of 1 until it ends) starts where the one before ended instead of at the step's start.
const BUILD_PARTS: [&str; 5] = ["verify", "lift", "compile", "link", "finish"];

/// How far a whole setup is, from the progress of the step that runs.
pub struct Overall {
    base: f64,
    steps: f64,
    /// the step last seen and how many were
    seen: Mutex<(String, usize)>,
}

impl Overall {
    /// `base`: the share already behind (an unpacked .nup).
    pub fn new(p: &setup::Paths, base: f64) -> Overall {
        Overall::of(counted(&setup::steps(p, None)), base)
    }

    fn of(steps: usize, base: f64) -> Overall {
        Overall { base, steps: steps as f64, seen: Mutex::new((String::new(), 0)) }
    }

    /// 0..1. It goes back when a step starts its next part, which counts from the start again, unless that part is a build part.
    pub fn at(&self, pr: &setup::Progress) -> f64 {
        let mut g = self.seen.lock().unwrap();
        if g.0 != pr.step {
            g.0 = pr.step.clone();
            g.1 += 1;
        }
        let mut within = if pr.total > 0 { (pr.done as f64 / pr.total as f64).min(1.0) } else { 0.0 };
        if pr.step == "build" {
            if let Some(i) = BUILD_PARTS.iter().position(|p| *p == pr.part) {
                within = (i as f64 + within) / BUILD_PARTS.len() as f64;
            }
        }
        (self.base + (1.0 - self.base) * (g.1.saturating_sub(1) as f64 + within) / self.steps).clamp(0.0, 1.0)
    }
}

/// The steps a setup will report: those not done, and the two it runs every time (writing fefw.ini, and the shader preparation,
/// which checks the GPU and driver itself).
fn counted(steps: &[setup::StepState]) -> usize {
    steps.iter().filter(|s| s.state != "done" || matches!(s.id.as_str(), "ini" | "prepare")).count().max(1)
}

/// The window's Set up with the saved settings: after an update it builds what the new version changed.
fn rebuild(cfg: &Config) -> Result<Value, String> {
    if let Some(why) = cpu::check() {
        return Err(why);
    }
    let install = crate::exe_dir();
    let _lock = lock::acquire(&install)?;
    idle(&install_dirs(cfg))?;
    // as the window does when it opens
    if crate::package_tools().is_some() {
        update::cleanup(&install);
    }
    crate::refresh_package_locales();
    if let Err(e) = crate::sync_run_folder(cfg) {
        eprintln!("[run folder] {e}");
    }
    let p = crate::paths(cfg)?;
    let shared = Arc::new(setup::Shared::default());
    *shared.progress.lock().unwrap() =
        setup::Progress { state: "running".into(), step: "python".into(), message: "Starting...".into(), ..Default::default() };
    let s = shared.clone();
    on_cancel(move || {
        s.cancel.store(true, Ordering::SeqCst);
        setup::kill_tree(s.pid.load(Ordering::SeqCst));
    });
    let overall = Overall::new(&p, 0.0);
    let progress = |pr: setup::Progress| {
        let fraction = overall.at(&pr);
        emit(
            "progress",
            json!({ "phase": pr.step, "part": pr.part, "done": pr.done, "total": pr.total, "fraction": fraction, "message": pr.message }),
        );
    };
    let end = setup::run(&shared, &p, crate::jobs_of(cfg), &cfg.runner, &progress, &|_| {});
    if end.state == "cancelled" {
        return Ok(json!({ "state": "cancelled", "message": end.message }));
    }
    if end.state != "done" {
        return Err(if end.message.trim().is_empty() { "Setup did not finish.".into() } else { end.message });
    }
    if !setup::complete(&p) {
        return Err("Setup ended, but the game is not ready. See setup.log.".into());
    }
    settings::ensure_play_paths(&p.game_dir, &p)?;
    // a step skipped at the end (the shader preparation) reports nothing: finish the bar
    emit("progress", json!({ "phase": "done", "done": 1, "total": 1, "fraction": 1.0, "message": "Ready to play." }));
    Ok(json!({ "state": "done", "message": "Ready to play." }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn a_folder_in_fefw_ini_is_found_from_here() {
        let game = Path::new("/games/run");
        let linux = |s: &str| host_path(s, game, false);
        assert_eq!(linux(""), Ok(String::new()));
        assert_eq!(linux("/opt/fsr"), Ok("/opt/fsr".into()));
        assert_eq!(linux("Z:\\home\\p\\fsr"), Ok("/home/p/fsr".into()));
        assert_eq!(linux("z:/home/p/fsr"), Ok("/home/p/fsr".into()));
        assert_eq!(linux("upscalers\\fsr"), Ok("/games/run/upscalers/fsr".into()));
        assert!(linux("S:\\fsr").is_err());
        assert!(linux("Z:fsr").is_err());
        assert_eq!(host_path("D:\\fsr", game, true), Ok("D:/fsr".into()));
    }

    #[test]
    fn frame_generation_is_offered_as_on_the_settings_page() {
        // the game's FSR 3 frame generation does not work under Proton
        assert!(frame_gen_choice("fsr3", true, true, true));
        assert!(!frame_gen_choice("fsr3", true, true, false));
        // DLSS frame generation needs an NVIDIA GPU and its files, on either system
        assert!(frame_gen_choice("dlss", true, true, false));
        assert!(!frame_gen_choice("dlss", true, false, false));
        assert!(!frame_gen_choice("dlss", false, true, true));
        assert!(frame_gen_choice("0", false, false, false));
    }

    #[test]
    fn commands() {
        assert_eq!(command(&args(&["--status"])), Some(Command::Status));
        assert_eq!(command(&args(&["--check-update"])), Some(Command::CheckUpdate));
        assert_eq!(command(&args(&["--update"])), Some(Command::Update));
        assert_eq!(command(&args(&["--rebuild"])), Some(Command::Rebuild));
        assert_eq!(command(&args(&["--add-to-steam"])), Some(Command::AddToSteam { before_setup: false, close_steam: false }));
        assert_eq!(
            command(&args(&["--add-to-steam", "--before-setup", "--close-steam"])),
            Some(Command::AddToSteam { before_setup: true, close_steam: true })
        );
        assert_eq!(command(&args(&["--settings"])), Some(Command::Settings));
        assert_eq!(command(&args(&["--set", "output=720p"])), Some(Command::Set("output=720p".into())));
        assert_eq!(command(&args(&["--set"])), Some(Command::Set(String::new())));
        assert_eq!(command(&args(&["--fetch-upscaler", "fsr"])), Some(Command::Fetch("fsr".into(), false)));
        assert_eq!(command(&args(&["--fetch-upscaler", "fsr", "--accept-licence"])), Some(Command::Fetch("fsr".into(), true)));
        // the window, and the setup RecompLauncher runs
        assert_eq!(command(&args(&[])), None);
        assert_eq!(command(&args(&["--game", "/dump"])), None);
        assert_eq!(command(&args(&["--prepare", "--game", "/dump", "--dlc="])), None);
    }

    #[test]
    fn overall_progress_follows_the_part_that_runs() {
        let o = Overall::of(2, 0.0);
        let at = |step: &str, done, total| o.at(&setup::Progress { step: step.into(), done, total, ..Default::default() });
        assert_eq!(at("build", 3, 6), 0.25);
        // the step's next part: not held at the furthest point of the one before
        assert_eq!(at("build", 0, 100), 0.0);
        assert_eq!(at("build", 100, 100), 0.5);
        assert_eq!(at("prepare", 0, 0), 0.5);
        assert_eq!(at("prepare", 10, 10), 1.0);
    }

    #[test]
    fn the_build_steps_parts_share_it_in_order() {
        let o = Overall::of(2, 0.0);
        let at = |part: &str, done, total| o.at(&setup::Progress { step: "build".into(), part: part.into(), done, total, ..Default::default() });
        assert_eq!(at("verify", 1, 1), 0.1);
        assert_eq!(at("compile", 50, 100), 0.25);
        // linking cannot count: it starts where compiling ended, not at 0
        assert_eq!(at("link", 0, 1), 0.3);
        assert_eq!(at("finish", 1, 1), 0.5);
    }

    #[test]
    fn the_steps_that_always_run_are_counted() {
        let step = |id: &str, state: &str| setup::StepState { id: id.into(), state: state.into(), ..Default::default() };
        let done: Vec<_> = ["build", "shaders", "ini", "prepare"].iter().map(|id| step(id, "done")).collect();
        assert_eq!(counted(&done), 2);
        assert_eq!(counted(&[step("build", "todo"), step("shaders", "done"), step("ini", "done"), step("prepare", "done")]), 3);
        assert_eq!(counted(&[]), 1);
    }

    #[cfg(windows)]
    #[test]
    fn a_running_program_is_in_use() {
        assert!(in_use(&std::env::current_exe().unwrap()));
        assert!(!in_use(Path::new("no-such-folder/fefw.exe")));
    }

    #[cfg(not(windows))]
    #[test]
    fn the_game_is_found_however_it_was_started() {
        let dirs = [PathBuf::from("/home/p/games/fefw")];
        let runs = |cmdline: &str, cwd: Option<&str>| names_game(cmdline.as_bytes(), cwd.map(Path::new), &dirs);
        // Proton's script and its steam.exe, then the game under the drives Wine shows
        assert!(runs("python3\0/p/proton\0waitforexitandrun\0/home/p/games/fefw/fefw.exe\0", None));
        assert!(runs("c:\\windows\\system32\\steam.exe\0/home/p/games/fefw/fefw.exe\0", None));
        assert!(runs("Z:\\home\\p\\games\\fefw\\fefw.exe\0--prepare-shaders\0", None));
        assert!(runs("S:\\p\\games\\fefw\\FEFW.EXE\0", None));
        assert!(runs("G:\\fefw.exe\0", None));
        // no full path: the working folder says which install
        assert!(runs("umu-run\0./fefw.exe\0", Some("/home/p/games/fefw")));
        assert!(runs("wine\0fefw.exe\0", Some("/home/p/games/fefw")));
        assert!(runs("wine\0fefw.exe\0", None));
        // another install
        assert!(!runs("/home/p/games/other/fefw.exe\0", None));
        assert!(!runs("Z:\\home\\p\\games\\fefw-old\\fefw.exe\0", None));
        assert!(!runs("S:\\p\\other\\fefw\\fefw.exe\0", None));
        assert!(!runs("Z:\\games\\fefw\\fefw.exe\0", None)); // the end of the path, but Z: says where it starts
        assert!(!runs("wine\0./fefw.exe\0", Some("/home/p/games/other")));
        // not the game
        assert!(!runs("/home/p/games/fefw/fefw-launcher.AppImage\0--update\0", None));
        assert!(!runs("less\0/home/p/games/fefw/fefw.log\0", None));
        assert!(!runs("cmake\0--target\0not-fefw.exe\0", None));
        assert!(!runs("", None));
    }

    #[cfg(unix)]
    #[test]
    fn an_install_reached_through_a_link_is_the_same_one() {
        let d = std::env::temp_dir().join(format!("fefw-headless-link-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(d.join("real")).unwrap();
        std::os::unix::fs::symlink(d.join("real"), d.join("link")).unwrap();
        let linked = format!("{}/fefw.exe", d.join("link").display());
        let dirs = [d.join("real")];
        assert!(names_game(linked.as_bytes(), None, &dirs));
        assert!(names_game(format!("Z:{}", linked.replace('/', "\\")).as_bytes(), None, &dirs));
        let _ = fs::remove_dir_all(&d);
    }

    #[cfg(not(windows))]
    #[test]
    fn settings_are_where_the_window_saves_them() {
        let env = |xdg: &'static str| {
            move |k: &str| match k {
                "XDG_CONFIG_HOME" => (!xdg.is_empty()).then(|| PathBuf::from(xdg)),
                "HOME" => Some(PathBuf::from("/home/p")),
                _ => None,
            }
        };
        assert_eq!(config_base(env("")), Some(PathBuf::from("/home/p/.config")));
        assert_eq!(config_base(env("/x")), Some(PathBuf::from("/x")));
        assert_eq!(config_base(env("relative")), Some(PathBuf::from("/home/p/.config")));
        assert_eq!(conf("identifier"), "io.github.goldoire.fefw-launcher");
        assert!(update::parse_version(&conf("version")).is_some());
    }

    #[test]
    fn a_damaged_settings_file_is_not_the_defaults() {
        let d = std::env::temp_dir().join(format!("fefw-headless-{}", std::process::id()));
        fs::create_dir_all(&d).unwrap();
        let path = d.join("launcher.json");
        assert!(read_config(&path).is_ok_and(|c| c.dump_dir.is_empty()));
        fs::write(&path, r#"{"dump_dir": "/dump", "jobs": 3}"#).unwrap();
        assert!(read_config(&path).is_ok_and(|c| c.dump_dir == "/dump" && c.jobs == 3));
        fs::write(&path, "{").unwrap();
        assert!(read_config(&path).is_err());
        let _ = fs::remove_dir_all(&d);
    }
}
