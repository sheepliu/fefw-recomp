// SPDX-License-Identifier: GPL-3.0-or-later
//! First-run setup from a source checkout. The repository ships no game code: every step runs one of the repository's own scripts
//! on the player's PC, against the player's own dump, and writes only inside the checkout (`game/`, `work/`, build folders) and the
//! run folder. The dump itself is only read.
//!
//! Steps, in order (each is skipped when its output is already there and current, so Set up after a pause continues):
//!   python   tools/.venv with recomp/requirements.txt (uv)
//!   exefs    game/dump/exefs: the dump's NSOs, `main` written uncompressed (tools/nso/prepare_exefs.py)
//!   lift     work/build/fefw/fefw_*.lib: lift and compile the six modules (recomp/scripts/build_game.cmd fefw), main with main.sites
//!            plus the installed mods' exefs/*.sites (lift_sites_file); not done while a mod has a site the build lacks
//!   textures tools/textures/third_party: astc-encoder and bc7enc sources for the ASTC to BC7 transcoder (fetch_third_party.ps1)
//!   exe      game-build/fefw/build/release/fefw.exe (game-build/scripts/build_fefw.ps1)
//!   harness  the shader translator (tools/shaders: Ryujinx sources staged, dotnet build)
//!   shaders  work/shaders/spirv: shaders found in the romfs and main, translated to SPIR-V
//!   ini      fefw.ini in the run folder (settings.rs)
//! Pause kills the running step's process tree; the build tools keep finished work (ninja, the harness's results.tsv).

use serde::Serialize;
use std::collections::VecDeque;
use std::fs;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

pub const STEPS: [(&str, &str); 9] = [
    ("python", "Python environment"),
    ("exefs", "Game code from your dump"),
    ("lift", "Recompile the game code"),
    ("textures", "Texture transcoder sources"),
    ("exe", "Build fefw.exe"),
    ("harness", "Shader translator"),
    ("shaders", "Translate the shaders"),
    ("ini", "Write fefw.ini"),
    ("prepare", "Prepare the shaders for your GPU"),
];

/// The steps of the release package (no toolchain on the player's PC: the bundled Python and Zig build fefw.exe).
pub const PACKAGE_STEPS: [(&str, &str); 4] = [
    ("build", "Recompile the game code from your dump"),
    ("shaders", "Translate the shaders"),
    ("ini", "Write fefw.ini"),
    ("prepare", "Prepare the shaders for your GPU"),
];

/// Every path the setup and Play use, derived from the checkout (or the release package), the dump and the run folder.
#[derive(Clone)]
pub struct Paths {
    /// the checkout; in the release package, the install folder
    pub repo: PathBuf,
    pub exefs: PathBuf,
    pub romfs: PathBuf,
    /// the dump's dlc/ folder (add-on content the player dumped), when it holds any; or the folder given with `--prepare --dlc`
    pub dlc: Option<PathBuf>,
    pub game_dir: PathBuf,
    /// the release package's tools/ folder (tools/build/fefw_build.py next to the launcher); None in a checkout
    pub package: Option<PathBuf>,
    /// the launcher's picked interface language (launcher.json `lang`; empty: none picked), for the F1 menu
    pub lang: String,
    /// "Check for updates at start (launcher and game)" is off (launcher.json `skip_update_check`)
    pub skip_update_check: bool,
}

impl Paths {
    fn in_game(&self, rel: &[&str]) -> PathBuf {
        rel.iter().fold(self.game_dir.clone(), |p, r| p.join(r))
    }
    pub fn python(&self) -> PathBuf {
        match &self.package {
            Some(t) => package_python(t),
            None => self.repo.join("tools").join(".venv").join("Scripts").join("python.exe"),
        }
    }
    pub fn plain_exefs(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["exefs"]),
            None => self.repo.join("game").join("dump").join("exefs"),
        }
    }
    /// RomFS mods ([fs] mods): <run folder>/mods/<name>/romfs
    pub fn mods(&self) -> PathBuf {
        self.in_game(&["mods"])
    }
    pub fn libs(&self) -> PathBuf {
        self.repo.join("work").join("build").join("fefw")
    }
    pub fn exe(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["fefw.exe"]),
            None if self.dev_mod_build() => self.in_game(&["mod-build", "active", "fefw.exe"]),
            None => exe_path(&self.repo),
        }
    }
    pub fn dev_mod_build(&self) -> bool {
        self.package.is_none() && (self.in_game(&["mods-state.json"]).is_file()
            || self.in_game(&["mod-build", "done.json"]).is_file() || has_native_code_mods(&self.game_dir))
    }
    pub fn runtime_main(&self) -> String {
        if self.dev_mod_build() {
            crate::dump::fwd(&self.in_game(&["mod-build", "active", "main"]))
        } else {
            String::new()
        }
    }
    pub fn harness(&self) -> PathBuf {
        match &self.package {
            Some(t) => t.join("spirv_harness").join(if cfg!(windows) { "spirv_harness.exe" } else { "spirv_harness" }),
            None => self.repo.join("tools").join("shaders").join("build").join("bin").join("Release").join("net10.0").join("spirv_harness.dll"),
        }
    }
    pub fn extractor(&self) -> PathBuf {
        match &self.package {
            Some(t) => t.join("shaders").join("extract_corpus.py"),
            None => self.repo.join("tools").join("shaders").join("ampere").join("extract_corpus.py"),
        }
    }
    pub fn corpus(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["shader_cache", "corpus"]),
            None => self.repo.join("work").join("shaders").join("corpus"),
        }
    }
    pub fn spirv(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["shader_cache", "spirv"]),
            None => self.repo.join("work").join("shaders").join("spirv"),
        }
    }
    pub fn bc7(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["shader_cache", "bc7"]),
            None => self.repo.join("work").join("bc7_cache"),
        }
    }
    /// The list of the game's shader programs (written by the extractor), which fefw.exe --prepare-shaders compiles; the runtime reads
    /// it from the folder above the SPIR-V folder, and writes prepare.state and (when it gives up) this launcher writes prepare.skipped there.
    pub fn programs_bin(&self) -> PathBuf {
        self.spirv().parent().map(|d| d.join("programs.bin")).unwrap_or_else(|| self.spirv().join("programs.bin"))
    }
    fn prepare_file(&self, name: &str) -> PathBuf {
        self.spirv().parent().map(|d| d.join(name)).unwrap_or_else(|| self.spirv().join(name))
    }
    fn shader_stamp(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["shader_cache", "setup.json"]),
            None => self.repo.join("work").join("shaders").join("setup.json"),
        }
    }
    pub fn log(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["setup.log"]),
            None => self.repo.join("work").join("setup.log"),
        }
    }
}

// ---- hook sites of mods (mods/<name>/exefs/*.sites; game-build/release/fefw_build.py collect_mod_sites) ------------------------------
// A code mod can hook only an instruction main was lifted with as a site; a mod that needs its own lists them in exefs/*.sites. The
// release package's build stamp covers them (fefw_build.py --check). In a checkout, Set up lifts main with main.sites plus these
// sites (FEFW_SITES, recomp/games/fefw/CMakeLists.txt), and the lift step is not done while a mod has a site the build lacks.

const MOD_SITE_KINDS: [&str; 3] = ["inline", "midhook", "entryhook"];

/// A site list: its @nsobid (upper case, "" when it has none) and its (kind, address) lines.
fn read_site_list(path: &Path) -> Option<(String, Vec<(String, u64)>)> {
    let text = fs::read_to_string(path).ok()?;
    let mut bid = String::new();
    let mut sites = Vec::new();
    for line in text.lines() {
        let line = line.split('#').next().unwrap_or("");
        let mut t = line.split_whitespace();
        let (Some(kind), Some(arg)) = (t.next(), t.next()) else { continue };
        if kind.eq_ignore_ascii_case("@nsobid") {
            bid = arg.to_ascii_uppercase();
        } else if let Ok(a) = u64::from_str_radix(arg.trim_start_matches("0x").trim_start_matches("0X"), 16) {
            sites.push((kind.to_string(), a));
        }
    }
    Some((bid, sites))
}

/// A mod's build ID against main's: a prefix of it, the rest zeros (as fefw_build.py bid_matches and the runtime's same_build).
fn same_build(mod_bid: &str, main_bid: &str) -> bool {
    if main_bid.is_empty() {
        return true;
    }
    let mut full = main_bid.to_ascii_uppercase();
    while full.len() < 64 {
        full.push('0');
    }
    let t = mod_bid.to_ascii_uppercase();
    t.len() >= 16 && t.len() <= 64 && full.starts_with(&t) && full[t.len()..].chars().all(|c| c == '0')
}

/// main's build ID (NSO header +0x40, 32 bytes) as hex and its code (.text memory offset and size, header +0x14 / +0x18); None
/// when the file cannot be read.
fn main_header(p: &Paths) -> Option<(String, u64, u64)> {
    let mut head = [0u8; 0x60];
    fs::File::open(p.plain_exefs().join("main")).and_then(|mut f| f.read_exact(&mut head)).ok()?;
    let word = |o: usize| u32::from_le_bytes([head[o], head[o + 1], head[o + 2], head[o + 3]]) as u64;
    Some((head[0x40..0x60].iter().map(|b| format!("{b:02X}")).collect(), word(0x14), word(0x18)))
}

/// One site a mod adds: the mod's name (`<folder>` or `<folder>/<subfolder>`), `<mod>/exefs/<file>:<line>`, kind, guest address.
type ModSite = (String, String, String, u64);

const MAIN_BASE: u64 = 0x71_0000_0000;
const NEXT_MODULE: u64 = 0x75_0000_0000;
/// This game's program ID (04007EF01F8AE000): a 16-digit hex mods subfolder that is another title's ID is skipped.
pub const PROGRAM_ID: u64 = 0x0400_7EF0_1F8A_E000;

/// The mods as the runtime finds them (runtime/src/services/common.cpp find_mods, game-build/release/fefw_build.py find_mod_dirs):
/// a folder below mods/ that has romfs/ or exefs/ is a mod; names starting with -, _ or . are off; a 16-digit hex folder that is
/// another title's program ID is skipped. Sorted by name (lower case), so the mod whose name sorts last wins where two add the
/// same site.
fn find_mod_dirs(root: &Path) -> Vec<(String, PathBuf)> {
    fn walk(d: &Path, name: &str, depth: u32, out: &mut Vec<(String, PathBuf)>) {
        if depth > 0 && (d.join("romfs").is_dir() || d.join("exefs").is_dir()) {
            out.push((name.to_string(), d.to_path_buf()));
            return;
        }
        if depth >= 4 {
            return;
        }
        let Ok(rd) = fs::read_dir(d) else { return };
        let mut subs: Vec<PathBuf> = rd.flatten().map(|e| e.path()).filter(|p| p.is_dir()).collect();
        subs.sort();
        for sub in subs {
            let Some(folder) = sub.file_name().map(|n| n.to_string_lossy().into_owned()) else { continue };
            if folder.is_empty() || folder.starts_with(['-', '_', '.']) {
                continue; // switched off
            }
            if folder.len() == 16 && folder.chars().all(|c| c.is_ascii_hexdigit()) && u64::from_str_radix(&folder, 16).ok() != Some(PROGRAM_ID) {
                continue; // another title's mods
            }
            let joined = if name.is_empty() { folder } else { format!("{name}/{folder}") };
            walk(&sub, &joined, depth + 1, out);
        }
    }
    let mut out = Vec::new();
    if root.is_dir() {
        walk(root, "", 0, &mut out);
    }
    out.sort_by_key(|(name, _)| name.to_lowercase());
    out
}

/// The installed mods' sites for this game build, checked as game-build/release/fefw_build.py collect_mod_sites and write_sites
/// check them, with the same messages: a line that is not `<kind> <hex address>`, a file without @nsobid, an address that is not
/// an instruction in main's code. Unknown site kinds are skipped (issue #165). The first problem is the error. A mod's exefs/
/// folder counts whether or not its mod.ini switches exefs off (fefw_build.py keeps those sites too, so switching a mod needs no
/// Set up).
fn mod_sites(p: &Paths) -> Result<Vec<ModSite>, String> {
    let header = main_header(p);
    let bid = header.as_ref().map(|h| h.0.clone()).unwrap_or_default();
    let mut out = Vec::new();
    for (name, d) in find_mod_dirs(&p.mods()) {
        let Ok(files) = fs::read_dir(d.join("exefs")) else { continue };
        let mut files: Vec<PathBuf> =
            files.flatten().map(|e| e.path()).filter(|f| f.is_file() && f.extension().is_some_and(|x| x.eq_ignore_ascii_case("sites"))).collect();
        files.sort_by_key(|f| f.file_name().map(|n| n.to_string_lossy().to_lowercase()));
        for f in files {
            let file = f.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            let where_ = format!("{name}/exefs/{file}");
            let Ok(text) = fs::read_to_string(&f) else { continue };
            let mut mbid: Option<String> = None;
            let mut recs = Vec::new();
            for (i, line) in text.trim_start_matches('\u{feff}').lines().enumerate() {
                let ln = i + 1;
                let t: Vec<&str> = line.split('#').next().unwrap_or("").split_whitespace().collect();
                if t.is_empty() {
                    continue;
                }
                if t[0].eq_ignore_ascii_case("@nsobid") {
                    if t.len() != 2 {
                        return Err(format!("{where_}:{ln}: @nsobid takes one build ID"));
                    }
                    mbid = Some(t[1].to_string());
                    continue;
                }
                if !MOD_SITE_KINDS.contains(&t[0]) {
                    // a kind another port defines, or a later one: skipped, as the release script does (issue #165)
                    continue;
                }
                let addr = (t.len() == 2)
                    .then(|| u64::from_str_radix(t[1].trim_start_matches("0x").trim_start_matches("0X"), 16).ok())
                    .flatten()
                    .ok_or_else(|| format!("{where_}:{ln}: expected \"<kind> <guest address in hex>\""))?;
                recs.push((ln, t[0].to_string(), addr));
            }
            let Some(mbid) = mbid else {
                return Err(format!("{where_} has no @nsobid line (the build ID of the main its sites are for)"));
            };
            if !same_build(&mbid, &bid) {
                continue; // another version of the game
            }
            for (ln, kind, addr) in recs {
                if addr % 4 != 0 || !(MAIN_BASE..NEXT_MODULE).contains(&addr) {
                    return Err(format!("{where_}:{ln}: 0x{addr:x} is not an instruction address in main (main is at 0x{MAIN_BASE:x})"));
                }
                if let Some((_, text_off, text_size)) = &header {
                    let off = addr - MAIN_BASE;
                    if !(*text_off..text_off + text_size).contains(&off) {
                        return Err(format!("{where_}:{ln}: 0x{addr:x} is outside main's code"));
                    }
                }
                out.push((name.clone(), format!("{where_}:{ln}"), kind, addr));
            }
        }
    }
    Ok(out)
}

/// The mods' sites missing from one lifted site list (their names, in mod order).
fn mods_missing_from(p: &Paths, list: &Path) -> Vec<String> {
    let Some((_, lifted)) = read_site_list(list) else { return vec![] };
    let lifted: std::collections::HashSet<(String, u64)> = lifted.into_iter().collect();
    let mut names: Vec<String> = Vec::new();
    for (name, _, kind, a) in mod_sites(p).unwrap_or_default() {
        if !lifted.contains(&(kind, a)) && !names.contains(&name) {
            names.push(name);
        }
    }
    names
}

/// The site list the lift uses and the last lift used: work/build/fefw/main_with_mods.sites when Set up wrote one, else
/// main.sites itself. The configure-time copy beside the libraries (work/build/fefw/main.sites) is not used: CMake writes it
/// before compiling, so a lift that was paused or failed already looks complete there while the libraries are still the old ones.
fn lift_sites_path(p: &Paths) -> PathBuf {
    let combined = p.libs().join("main_with_mods.sites");
    if combined.is_file() {
        combined
    } else {
        p.repo.join("recomp").join("games").join("fefw").join("main.sites")
    }
}

/// A site list's lines without comments, each as its words (a patch line has two more than a hook's kind and address).
fn site_lines(path: &Path) -> Option<std::collections::HashSet<String>> {
    let text = fs::read_to_string(path).ok()?;
    let words = |l: &str| l.split('#').next().unwrap_or("").split_whitespace().collect::<Vec<_>>().join(" ").to_ascii_lowercase();
    Some(text.lines().map(words).filter(|l| !l.is_empty()).collect())
}

/// Whether the libraries were lifted with another main.sites than the repo has now (an update changed it): main.sites against
/// CMake's copy beside the libraries (work/build/fefw/main.sites). A line the copy lacks counts, and so does a line only the copy
/// has unless it is a hook: the copy may hold mods' hooks, and a hook nobody attaches to does no harm, but a patch that is gone
/// is still in the code. Comments do not count. CMake writes the copy before compiling; a Set up lift that was paused or failed
/// is caught by cpu_variant.txt instead (lift_variant_ok). False when either list is missing (nothing to compare).
fn lifted_sites_stale(p: &Paths) -> bool {
    let own = p.repo.join("recomp").join("games").join("fefw").join("main.sites");
    let (Some(want), Some(lifted)) = (site_lines(&own), site_lines(&p.libs().join("main.sites"))) else { return false };
    let hook = |l: &str| l.split(' ').next().is_some_and(|kind| MOD_SITE_KINDS.contains(&kind));
    !want.is_subset(&lifted) || lifted.difference(&want).any(|l| !hook(l))
}

/// Mods with a hook site that the lifted module libraries lack (the lift step's note).
pub fn mods_missing_lift_sites(p: &Paths) -> Vec<String> {
    mods_missing_from(p, &lift_sites_path(p))
}

/// Mods with a hook site that the built fefw.exe was not lifted with (its fefw.sites next to it). Empty when the exe or its list
/// is not there yet (the lift / exe steps are not done then anyway), or when a .sites file has an error (reported by
/// mod_sites_problem instead).
pub fn mods_missing_sites(p: &Paths) -> Vec<String> {
    mods_missing_from(p, &p.exe().with_extension("sites"))
}

/// A problem in the mods' .sites files (the first one), as the release script reports it.
fn mod_sites_problem(p: &Paths) -> Option<String> {
    mod_sites(p).err()
}

/// Whether the lift step has nothing to do: the libraries are newer than the NSO, were lifted with the current main.sites and no
/// mod site is missing from the list they were lifted with. When Set up wrote a combined list, main's library must also be newer
/// than it: Set up writes the list before starting the lift (and CMake copies it beside the libraries at configure time, before
/// compiling), so a lift that was paused or failed is not mistaken for a finished one and Set up lifts again.
fn lift_done(p: &Paths) -> bool {
    if p.dev_mod_build() {
        return dev_build_ok(p);
    }
    if !libs_ok(p) || lifted_sites_stale(p) || mod_sites_problem(p).is_some() || !mods_missing_lift_sites(p).is_empty() {
        return false;
    }
    let combined = p.libs().join("main_with_mods.sites");
    !combined.is_file() || mtime(&p.libs().join("fefw_main.lib")) >= mtime(&combined)
}

/// Checkout: the site list the lift uses, main.sites plus the mods' sites (written only when it changes, since the lift depends on
/// the file). Without mod sites it is main.sites itself. A problem in a .sites file stops Set up with its message.
fn lift_sites_file(p: &Paths) -> Result<PathBuf, String> {
    let own_path = p.repo.join("recomp").join("games").join("fefw").join("main.sites");
    let extra = mod_sites(p)?;
    if extra.is_empty() {
        let _ = fs::remove_file(p.libs().join("main_with_mods.sites")); // a list from mods that are gone must not stand for this lift
        return Ok(own_path);
    }
    let own = fs::read_to_string(&own_path).map_err(|e| format!("cannot read {}: {e}", own_path.display()))?;
    let have: std::collections::HashSet<(String, u64)> = read_site_list(&own_path).map(|(_, s)| s).unwrap_or_default().into_iter().collect();
    let mut text = own.trim_end().to_string() + "\n\n# ---- added by mods (exefs/*.sites)\n";
    let mut seen = std::collections::HashSet::new();
    for (_, where_, kind, a) in extra {
        if have.contains(&(kind.clone(), a)) || !seen.insert((kind.clone(), a)) {
            continue;
        }
        text += &format!("{kind} 0x{a:x}   # {where_}\n");
    }
    let out = p.libs().join("main_with_mods.sites");
    if fs::read_to_string(&out).ok().as_deref() != Some(text.as_str()) {
        fs::create_dir_all(p.libs()).map_err(|e| e.to_string())?;
        fs::write(&out, text).map_err(|e| format!("cannot write {}: {e}", out.display()))?;
    }
    Ok(out)
}

/// Cache checks until the build output or a mod package/enablement changes.
type CheckKey = (PathBuf, Option<std::time::SystemTime>, u64, Option<std::time::SystemTime>, u64,
                 Vec<(PathBuf, Option<std::time::SystemTime>, u64)>);
static BUILD_CHECK: Mutex<Option<(CheckKey, bool)>> = Mutex::new(None);
static DEV_BUILD_CHECK: Mutex<Option<(CheckKey, bool)>> = Mutex::new(None);

fn mod_revision(game_dir: &Path) -> Vec<(PathBuf, Option<std::time::SystemTime>, u64)> {
    let mut pending = vec![game_dir.join("mods"), game_dir.join("mods-state.json")];
    let mut entries = Vec::new();
    while let Some(path) = pending.pop() {
        let metadata = fs::symlink_metadata(&path).ok();
        entries.push((path.strip_prefix(game_dir).unwrap_or(&path).to_path_buf(),
                      metadata.as_ref().and_then(|m| m.modified().ok()), metadata.as_ref().map_or(0, |m| m.len())));
        if metadata.as_ref().is_some_and(|m| m.is_dir() && !m.file_type().is_symlink()) {
            if let Ok(children) = fs::read_dir(path) {
                pending.extend(children.filter_map(Result::ok).map(|entry| entry.path()));
            }
        }
    }
    entries.sort_by(|a, b| a.0.cmp(&b.0));
    entries
}

/// A hash of the files under `mods` that go into the build (exefs/*.ips, *.pchtxt, *.sites: fefw_build.py collect_exefs_patches,
/// collect_mod_sites): their paths, sizes and modification times. Adding, changing, removing or switching off (renaming) a mod
/// that has them changes it, so the next check asks fefw_build.py again. It may change for a file the build ignores (another
/// game version's patch, say); that only costs one more check.
fn mods_fingerprint(mods: &Path) -> u64 {
    native_build_fingerprint(mods, true)
}

fn native_build_fingerprint(mods: &Path, include_sites: bool) -> u64 {
    use std::hash::{Hash, Hasher};
    fn walk(dir: &Path, depth: u32, include_sites: bool, out: &mut Vec<(String, u64, Option<std::time::SystemTime>)>) {
        let Ok(rd) = fs::read_dir(dir) else { return };
        for e in rd.flatten() {
            let path = e.path();
            let Ok(ft) = e.file_type() else { continue };
            if ft.is_dir() {
                if depth < 6 {
                    walk(&path, depth + 1, include_sites, out);
                }
                continue;
            }
            let in_exefs = dir.file_name().is_some_and(|n| n.eq_ignore_ascii_case("exefs"));
            let ext = path.extension().and_then(|x| x.to_str()).unwrap_or("").to_ascii_lowercase();
            if in_exefs && (matches!(ext.as_str(), "ips" | "ips32" | "pchtxt") || (include_sites && ext == "sites")) {
                let m = e.metadata().ok();
                out.push((path.to_string_lossy().into_owned(), m.as_ref().map_or(0, |m| m.len()), m.and_then(|m| m.modified().ok())));
            }
        }
    }
    let mut files = Vec::new();
    walk(mods, 0, include_sites, &mut files);
    files.sort();
    let mut h = std::collections::hash_map::DefaultHasher::new();
    files.hash(&mut h);
    h.finish()
}

pub fn has_native_code_mods(game: &Path) -> bool {
    use std::hash::{Hash, Hasher};
    let mut empty = std::collections::hash_map::DefaultHasher::new();
    (&[] as &[(String, u64, Option<std::time::SystemTime>)]).hash(&mut empty);
    // Upstream now handles standalone .sites in its regular checkout lift.
    native_build_fingerprint(&game.join("mods"), false) != empty.finish()
}

fn package_build_ok(p: &Paths) -> bool {
    let Some(tools) = &p.package else { return false };
    let done = p.in_game(&["build", "done.json"]);
    let (Ok(dm), true) = (fs::metadata(&done), p.exe().is_file()) else { return false };
    let key: CheckKey = (p.game_dir.clone(), dm.modified().ok(), dm.len(), mtime(&p.exe()),
                         mods_fingerprint(&p.mods()), mod_revision(&p.game_dir));
    if let Some((k, b)) = BUILD_CHECK.lock().unwrap().as_ref() {
        if *k == key {
            return *b;
        }
    }
    let mut cmd = Command::new(p.python());
    cmd.arg("-I").arg(tools.join("build").join("fefw_build.py")).arg("--check").arg("--game").arg(&p.game_dir);
    // the build for this processor (cpu.rs): a fefw.exe built for the other one is not current, so Set up rebuilds it
    if let Some(v) = crate::cpu::build_variant() {
        cmd.args(["--cpu", v]);
    }
    quiet(&mut cmd);
    let ok = cmd.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false);
    *BUILD_CHECK.lock().unwrap() = Some((key, ok));
    ok
}

/// The release package's Python: tools/python/python.exe (the embeddable package) on Windows. On Linux tools/python/bin/python3
/// (a relocatable build) when the package has one, else the system's python3, which then needs the lifter's packages (zbic, lz4,
/// capstone, zstandard) installed.
pub(crate) fn package_python(tools: &Path) -> PathBuf {
    if cfg!(windows) {
        return tools.join("python").join("python.exe");
    }
    let bundled = tools.join("python").join("bin").join("python3");
    if bundled.is_file() {
        return bundled;
    }
    which("python3").unwrap_or(bundled)
}

pub fn exe_path(repo: &Path) -> PathBuf {
    repo.join("game-build").join("fefw").join("build").join("release").join("fefw.exe")
}

pub const MODULES: [&str; 6] = ["main", "sdk", "subsdk0", "subsdk1", "subsdk2", "rtld"];

#[derive(Serialize, Clone, Default)]
pub struct StepState {
    pub id: String,
    pub title: String,
    /// "done", "todo", "running", "error"
    pub state: String,
    pub detail: String,
}

#[derive(Serialize, Clone, Default)]
pub struct Progress {
    /// "idle", "running", "done", "cancelled", "error"
    pub state: String,
    pub step: String,
    /// the part of the step the tool says it is in (fefw_build.py's "lift", "compile", "link"...), empty when it says none
    pub part: String,
    pub done: u64,
    pub total: u64,
    pub message: String,
}

/// The running job's state, shared with the page.
#[derive(Default)]
pub struct Shared {
    pub progress: Mutex<Progress>,
    pub log: Mutex<VecDeque<String>>,
    pub cancel: AtomicBool,
    pub pid: AtomicU32,
}

const LOG_LINES: usize = 600;

fn mtime(p: &Path) -> Option<std::time::SystemTime> {
    fs::metadata(p).ok().and_then(|m| m.modified().ok())
}

/// NSO flags (0x0C) of a file, None when it is not a readable NSO.
fn nso_flags(p: &Path) -> Option<u32> {
    let mut h = [0u8; 16];
    fs::File::open(p).ok()?.read_exact(&mut h).ok()?;
    (&h[..4] == b"NSO0").then(|| u32::from_le_bytes([h[12], h[13], h[14], h[15]]))
}

fn python_ok(p: &Paths) -> bool {
    if !p.python().is_file() {
        return false;
    }
    let mut cmd = Command::new(p.python());
    cmd.args(["-I", "-c", "import zbic, lz4, capstone, unicorn, elftools, zstandard"]);
    quiet(&mut cmd);
    cmd.stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false)
}

fn exefs_ok(p: &Paths) -> bool {
    let dir = p.plain_exefs();
    let stamp = fs::read_to_string(dir.join("source.txt")).unwrap_or_default();
    MODULES.iter().all(|m| dir.join(m).is_file())
        && nso_flags(&dir.join("main")).is_some_and(|f| f & 0x87 == 0)
        && stamp.lines().next().map(|l| l.trim()) == Some(crate::dump::fwd(&p.exefs).as_str())
}

/// The processor target (cpu.rs: "fma" or "compat") the libraries in `libs` were compiled for, kept beside them by the lift step. A
/// folder without the file was built before the compatible variant existed, so it is the FMA3 build.
fn lift_variant_ok(libs: &Path, want: &str) -> bool {
    fs::read_to_string(libs.join("cpu_variant.txt")).map(|s| s.trim().to_string()).unwrap_or_else(|_| "fma".into()) == want
}

fn libs_ok(p: &Paths) -> bool {
    if p.dev_mod_build() {
        return dev_build_ok(p);
    }
    let main = mtime(&p.plain_exefs().join("main"));
    MODULES.iter().all(|m| {
        let lib = p.libs().join(format!("fefw_{m}.lib"));
        lib.is_file() && mtime(&lib) >= main
    }) && lift_variant_ok(&p.libs(), crate::cpu::build_variant().unwrap_or("fma"))
}

fn exe_ok(p: &Paths) -> bool {
    if p.dev_mod_build() {
        return dev_build_ok(p);
    }
    let exe = mtime(&p.exe());
    exe.is_some() && MODULES.iter().all(|m| mtime(&p.libs().join(format!("fefw_{m}.lib"))) <= exe) && mods_missing_sites(p).is_empty()
}

fn dev_build_ok(p: &Paths) -> bool {
    if !p.python().is_file() {
        return false;
    }
    let done = p.in_game(&["mod-build", "done.json"]);
    let mut revisions = mod_revision(&p.game_dir);
    for name in MODULES {
        let path = p.plain_exefs().join(name);
        let meta = fs::metadata(&path).ok();
        revisions.push((path, meta.as_ref().and_then(|m| m.modified().ok()), meta.as_ref().map_or(0, |m| m.len())));
    }
    for path in [p.in_game(&["mod-build", "active", "main"]), p.in_game(&["mod-build", "active", "fefw.sites"]),
                 p.exe(), p.repo.join("tools").join("mods").join("dev_build.py"),
                 p.repo.join("tools").join("mods").join("modpack.py")] {
        let meta = fs::metadata(&path).ok();
        revisions.push((path, meta.as_ref().and_then(|m| m.modified().ok()), meta.as_ref().map_or(0, |m| m.len())));
    }
    for name in ["game-build/release/fefw_build.py", "recomp/scripts/build_game.cmd", "recomp/games/fefw/CMakeLists.txt",
                 "recomp/games/fefw/main.sites", "game-build/scripts/build_fefw.ps1",
                 "game-build/fefw/CMakeLists.txt", "runtime/games/fefw/CMakeLists.txt",
                 "runtime/games/fefw/fefw_game.cpp", "runtime/games/fefw/fefw_game.h",
                 "runtime/games/fefw/fefw_menu.cpp", "runtime/games/fefw/fefw_mods.cpp",
                 "runtime/games/fefw/fefw_mods.h", "runtime/games/fefw/fefw_mods_menu.cpp",
                 "runtime/games/fefw/fefw_mouse.cpp",
                 "runtime/src/gfx/overlay.cpp", "runtime/src/gfx/play/play.cpp",
                 "runtime/third_party/nlohmann/json.hpp"] {
        let path = p.repo.join(name);
        let meta = fs::metadata(&path).ok();
        revisions.push((path, meta.as_ref().and_then(|m| m.modified().ok()), meta.as_ref().map_or(0, |m| m.len())));
    }
    let dm = fs::metadata(&done).ok();
    let key: CheckKey = (p.game_dir.clone(), dm.as_ref().and_then(|m| m.modified().ok()),
                         dm.as_ref().map_or(0, |m| m.len()), mtime(&p.exe()), mods_fingerprint(&p.mods()), revisions);
    if let Some((prior, ok)) = DEV_BUILD_CHECK.lock().unwrap().as_ref() {
        if *prior == key {
            return *ok;
        }
    }
    let mut cmd = Command::new(p.python());
    cmd.arg("-I").arg(p.repo.join("tools").join("mods").join("dev_build.py"))
        .arg("check").arg("--repo").arg(&p.repo).arg("--game").arg(&p.game_dir);
    quiet(&mut cmd);
    let ok = cmd.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
        .status().map(|s| s.success()).unwrap_or(false);
    *DEV_BUILD_CHECK.lock().unwrap() = Some((key, ok));
    ok
}

fn textures_ok(p: &Paths) -> bool {
    p.repo.join("tools").join("textures").join("third_party").join("astc-encoder").join("Source").join("astcenc.h").is_file()
}

const SHADER_TRANSLATOR_VERSION: u32 = 4; // 4: integer abs without SAbs, which hangs NVIDIA GPUs; 3: real derivatives for the NVN2 shuffle pattern (#29 eyes); 2: portable 32-lane warps (game-build/release/fefw_build.py has the same number)

fn shaders_ok(p: &Paths) -> bool {
    let stamp = fs::read_to_string(p.shader_stamp()).unwrap_or_default();
    let v: serde_json::Value = serde_json::from_str(&stamp).unwrap_or_default();
    p.spirv().join("results.tsv").is_file()
        && v["complete"].as_bool() == Some(true)
        && v["translator_version"].as_u64() == Some(SHADER_TRANSLATOR_VERSION as u64)
        && v["romfs"].as_str() == Some(crate::dump::fwd(&p.romfs).as_str())
        && v["no_float64"].as_bool().unwrap_or(false) == no_float64()
}

/// The shaders are translated without 64-bit floats (the harness's --no-float64, as Ryujinx does on such GPUs) when this PC has an
/// Intel GPU: the Arc A770 has no shaderFloat64 and lost the device on the first shader that used them (GitHub issue #45). Only the
/// rounding of the game's few 64-bit integer conversions changes, so a PC that also has another GPU is translated this way too.
/// FEFW_NO_FLOAT64=1 forces it on any GPU, =0 off. A stamp made the other way is not current, so Set up translates the shaders again.
fn no_float64() -> bool {
    match std::env::var("FEFW_NO_FLOAT64").as_deref() {
        Ok("1") => true,
        Ok("0") => false,
        _ => crate::gpu::get().intel,
    }
}

/// The shader preparation has finished for some GPU and driver (the runtime's prepare.state says "complete: 1"), or was given up on.
/// Whether it is still current for this GPU and driver is the runtime's check (fefw.exe --prepare-shaders --if-needed, instant when it is).
fn prepare_ok(p: &Paths) -> bool {
    p.prepare_file("prepare.skipped").is_file()
        || fs::read_to_string(p.prepare_file("prepare.state")).map(|s| s.lines().any(|l| l.trim() == "complete: 1")).unwrap_or(false)
}

/// What is done already, step by step (the page's checklist).
pub fn steps(p: &Paths, running: Option<&str>) -> Vec<StepState> {
    let list: &[(&str, &str)] = if p.package.is_some() { &PACKAGE_STEPS } else { &STEPS };
    let done = |id: &str| match id {
        "build" => package_build_ok(p),
        "python" => python_ok(p),
        "exefs" => exefs_ok(p),
        "lift" => lift_done(p),
        "exe" => exe_ok(p),
        "textures" => textures_ok(p),
        "harness" => p.harness().is_file() && shaders_ok(p),
        "shaders" => shaders_ok(p),
        "ini" => crate::settings::play_paths_ok(&p.game_dir, p),
        "prepare" => prepare_ok(p),
        _ => false,
    };
    list.iter()
        .map(|(id, title)| StepState {
            id: id.to_string(),
            title: title.to_string(),
            state: if running == Some(*id) {
                "running".into()
            } else if done(id) {
                "done".into()
            } else {
                "todo".into()
            },
            detail: if *id == "lift" && p.package.is_none() { missing_sites_detail(p) } else { String::new() },
        })
        .collect()
}

/// The lift step's note: a problem in a mod's .sites file, or the mods with hook sites the build lacks.
fn missing_sites_detail(p: &Paths) -> String {
    if let Some(problem) = mod_sites_problem(p) {
        return problem;
    }
    let names = mods_missing_lift_sites(p);
    if names.is_empty() {
        String::new()
    } else {
        format!("new hook sites from {}: Set up lifts the game with them", names.join(", "))
    }
}

pub fn complete(p: &Paths) -> bool {
    // the shader preparation only saves compile time while playing: a game that has not been prepared (yet) is still playable
    steps(p, None).iter().all(|s| s.state == "done" || s.id == "prepare")
}

/// A program started from the AppImage gets the host's environment, not the AppImage's: its AppRun points PYTHONHOME, GTK_*,
/// GIO/GST/QT module paths and more into its temporary mount, which breaks Python programs (umu-run, Proton's script) and anything
/// that outlives the launcher (Steam started again by Add to Steam). Variables naming the mount lose those entries (and go when
/// nothing is left); the AppImage's own variables go. Nothing changes outside an AppImage.
pub(crate) fn host_env(cmd: &mut Command) {
    if !cfg!(target_os = "linux") {
        return;
    }
    // keep variables the caller already set on cmd (e.g. a custom runner's leading NAME=value assignments)
    let explicit: std::collections::HashSet<std::ffi::OsString> = cmd.get_envs().map(|(k, _)| k.to_os_string()).collect();
    let vars: Vec<(String, String)> = std::env::vars_os().filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?))).collect();
    for (k, v) in appimage_fixes(&vars) {
        if explicit.contains(std::ffi::OsStr::new(&k)) {
            continue;
        }
        match v {
            Some(v) => cmd.env(k, v),
            None => cmd.env_remove(k),
        };
    }
}

/// What host_env changes in `vars` (an environment with an AppImage's APPDIR in it): (name, new value), None removes it.
pub(crate) fn appimage_fixes(vars: &[(String, String)]) -> Vec<(String, Option<String>)> {
    // only an AppImage's AppRun (mounted via $APPIMAGE, or unpacked with <APPDIR>/AppRun): on Windows (';' lists) or with an
    // unrelated APPDIR, nothing is touched
    if !cfg!(target_os = "linux") {
        return vec![];
    }
    let Some(appdir) = vars.iter().find(|(k, _)| k == "APPDIR").map(|(_, d)| d.trim_end_matches('/').to_string()).filter(|d| d.len() > 1) else {
        return vec![];
    };
    let has_appimage = vars.iter().any(|(k, v)| k == "APPIMAGE" && !v.is_empty());
    let unpacked_apprun = Path::new(&appdir).join("AppRun").is_file();
    if !has_appimage && !unpacked_apprun {
        return vec![];
    }
    let mut out = vec![];
    for (k, v) in vars {
        if matches!(k.as_str(), "APPDIR" | "APPIMAGE" | "ARGV0" | "OWD" | "PYTHONHOME" | "PYTHONPATH" | "PYTHONDONTWRITEBYTECODE" | "PERLLIB") {
            out.push((k.clone(), None));
        } else if k.starts_with("RECOMPLAUNCHER") || matches!(k.as_str(), "TMPDIR" | "TEMP" | "TMP" | "PWD") {
            // an unpacked AppImage puts AppRun in the install folder, so RecompLauncher's own paths live under APPDIR too
            continue;
        } else if v.contains(&appdir) {
            let kept: Vec<&str> = v.split(':').filter(|p| !p.is_empty() && !p.contains(&appdir)).collect();
            out.push((k.clone(), (!kept.is_empty()).then(|| kept.join(":"))));
        }
    }
    out
}

pub(crate) fn quiet(cmd: &mut Command) {
    host_env(cmd);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS (the compilers it starts inherit it)
        cmd.creation_flags(0x0800_0000 | 0x0000_4000);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // its own process group, so kill_tree ends everything it starts (the lifter's workers, the compilers)
        cmd.process_group(0);
    }
}

/// Ends a process and everything it started (cmake, ninja, the compilers, the lifter's workers).
pub fn kill_tree(pid: u32) {
    if pid == 0 {
        return;
    }
    #[cfg(windows)]
    {
        let mut cmd = Command::new("taskkill");
        cmd.args(["/T", "/F", "/PID", &pid.to_string()]);
        quiet(&mut cmd);
        let _ = cmd.stdout(Stdio::null()).stderr(Stdio::null()).status();
    }
    #[cfg(unix)]
    {
        // quiet() made the process a group leader: a negative pid signals the whole group
        let _ = Command::new("kill").args(["-KILL", "--", &format!("-{pid}")]).stdout(Stdio::null()).stderr(Stdio::null()).status();
    }
}

/// ninja's "[12/345] ..." prefix.
fn ninja_progress(line: &str) -> Option<(u64, u64)> {
    let rest = line.strip_prefix('[')?;
    let (nums, _) = rest.split_once(']')?;
    let (a, b) = nums.split_once('/')?;
    Some((a.trim().parse().ok()?, b.trim().parse().ok()?))
}

/// "@progress <part> N M <text>" (fefw_build.py), "scan: 12 345" (extract_corpus.py --progress) or the harness's "done N/M" style
/// lines: the part (empty when the line names none), done and total.
fn tool_progress(line: &str) -> Option<(String, u64, u64)> {
    if let Some(rest) = line.strip_prefix("@progress ") {
        let mut it = rest.split_whitespace();
        let part = it.next()?.to_string();
        return Some((part, it.next()?.parse().ok()?, it.next()?.parse().ok()?));
    }
    if let Some(rest) = line.strip_prefix("scan: ").or_else(|| line.strip_prefix("prepare: ")) {
        let mut it = rest.split_whitespace();
        return Some((String::new(), it.next()?.parse().ok()?, it.next()?.parse().ok()?));
    }
    ninja_progress(line).map(|(d, t)| (String::new(), d, t))
}

pub struct Runner<'a> {
    pub shared: &'a Arc<Shared>,
    pub emit: &'a (dyn Fn(Progress) + Sync),
    pub emit_log: &'a (dyn Fn(Vec<String>) + Sync),
    pub log_file: Mutex<Option<fs::File>>,
    pub llvm_bin: Option<PathBuf>,
    pub log_path: PathBuf,
}

impl Runner<'_> {
    fn log(&self, lines: Vec<String>) {
        if lines.is_empty() {
            return;
        }
        {
            let mut q = self.shared.log.lock().unwrap();
            for l in &lines {
                q.push_back(l.clone());
                while q.len() > LOG_LINES {
                    q.pop_front();
                }
            }
        }
        if let Some(f) = self.log_file.lock().unwrap().as_mut() {
            use std::io::Write;
            for l in &lines {
                let _ = writeln!(f, "{l}");
            }
        }
        (self.emit_log)(lines);
    }

    fn progress(&self, step: &str, part: &str, done: u64, total: u64, message: &str) {
        let p = Progress { state: "running".into(), step: step.into(), part: part.into(), done, total, message: message.into() };
        *self.shared.progress.lock().unwrap() = p.clone();
        (self.emit)(p);
    }

    /// Runs one command to the end, streaming its output into the log. Ok(true) = success, Ok(false) = paused.
    fn run(&self, step: &str, message: &str, mut cmd: Command, cwd: &Path) -> Result<bool, String> {
        if self.shared.cancel.load(Ordering::SeqCst) {
            return Ok(false);
        }
        self.progress(step, "", 0, 0, message);
        self.log(vec![format!("> {:?}", cmd)]);
        cmd.current_dir(cwd);
        cmd.env("PYTHONUNBUFFERED", "1").env("NINJA_STATUS", "[%f/%t] ");
        if let Some(bin) = &self.llvm_bin {
            cmd.env("NX_LLVM", bin).env("NX_LLVM_BIN", bin);
        }
        // A PYTHONPATH / PYTHONHOME left by another Python install is inherited by every Python this step starts, tools/.venv's
        // included, and can make it import an extension built for another version (EmblemWeave issue 43). Only this child loses them.
        // The prepare step runs extract_corpus.py with -I and fefw.exe through the Linux runner (which may be a user-installed umu-run).
        if step != "prepare" {
            cmd.env_remove("PYTHONPATH").env_remove("PYTHONHOME").env("PYTHONNOUSERSITE", "1");
        }
        quiet(&mut cmd);
        let mut child = cmd
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("could not start {:?}: {}", cmd.get_program(), e))?;
        self.shared.pid.store(child.id(), Ordering::SeqCst);
        let (tx, rx) = std::sync::mpsc::channel::<String>();
        let tx2 = tx.clone();
        let out = child.stdout.take().unwrap();
        let err = child.stderr.take().unwrap();
        let read = |r: Box<dyn Read + Send>, tx: std::sync::mpsc::Sender<String>| {
            std::thread::spawn(move || {
                for l in BufReader::new(r).split(b'\n').map_while(Result::ok) {
                    let s = String::from_utf8_lossy(&l).trim_end_matches('\r').to_string();
                    if tx.send(s).is_err() {
                        break;
                    }
                }
            })
        };
        let t1 = read(Box::new(out), tx);
        let t2 = read(Box::new(err), tx2);
        let mut batch = Vec::new();
        let mut script_error: Option<String> = None;
        let mut last = std::time::Instant::now();
        loop {
            if self.shared.cancel.load(Ordering::SeqCst) {
                kill_tree(child.id());
            }
            match rx.recv_timeout(Duration::from_millis(150)) {
                Ok(l) => {
                    if let Some((part, d, t)) = tool_progress(&l) {
                        let text = l.strip_prefix("@progress ").and_then(|r| r.splitn(4, ' ').nth(3)).unwrap_or(message).to_string();
                        self.progress(step, &part, d, t, &text);
                    }
                    if let Some(e) = l.strip_prefix("@error ") {
                        script_error = Some(e.replace("\\n", "\n"));
                    }
                    batch.push(l);
                }
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
            }
            if !batch.is_empty() && (last.elapsed() > Duration::from_millis(250) || batch.len() > 200) {
                self.log(std::mem::take(&mut batch));
                last = std::time::Instant::now();
            }
        }
        self.log(batch);
        let _ = t1.join();
        let _ = t2.join();
        let st = child.wait().map_err(|e| e.to_string())?;
        self.shared.pid.store(0, Ordering::SeqCst);
        if self.shared.cancel.load(Ordering::SeqCst) {
            return Ok(false);
        }
        if st.success() {
            Ok(true)
        } else if st.code() == Some(2) && script_error.is_none() && step == "build" {
            Ok(false)
        } else if let Some(e) = script_error {
            Err(e)
        } else {
            Err(format!("{} failed ({}). The last lines of the log say why; the full log is {}.", message, st, crate::dump::fwd(&self.log_path)))
        }
    }
}

fn which(exe: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path).map(|d| d.join(exe)).find(|p| p.is_file())
}

/// A tool on PATH or in one of its usual install folders.
fn find_tool(names: &[&str], extra: &[PathBuf]) -> Option<PathBuf> {
    names.iter().find_map(|n| which(n)).or_else(|| extra.iter().find(|p| p.is_file()).cloned())
}

fn local_appdata(rel: &str) -> PathBuf {
    PathBuf::from(std::env::var("LOCALAPPDATA").unwrap_or_default()).join(rel)
}

pub fn powershell() -> PathBuf {
    find_tool(&["pwsh.exe"], &[PathBuf::from(r"C:\Program Files\PowerShell\7\pwsh.exe")]).unwrap_or_else(|| PathBuf::from("powershell.exe"))
}

fn workspace_llvm_bin(repo: &Path) -> Option<PathBuf> {
    // The local compiler can live beside the checkout, outside the tracked project.
    for toolchain in [repo.parent()?.join("toolchain"), repo.join("work").join("toolchain")] {
        let Ok(entries) = fs::read_dir(toolchain) else { continue };
        let mut candidates: Vec<(Vec<u32>, PathBuf)> = entries.filter_map(Result::ok)
            .filter_map(|entry| {
                let name = entry.file_name();
                let version = name.to_str()?.strip_prefix("clang+llvm-")?.split('-').next()?;
                let parts: Vec<u32> = version.split('.').map(str::parse).collect::<Result<_, _>>().ok()?;
                if parts.first().copied().unwrap_or(0) < 23 { return None; }
                let bin = entry.path().join("bin");
                bin.join("clang-cl.exe").is_file().then_some((parts, bin))
            }).collect();
        candidates.sort_by(|a, b| a.0.cmp(&b.0));
        if let Some((_, bin)) = candidates.pop() { return Some(bin); }
    }
    None
}

fn find_llvm_bin(repo: Option<&Path>) -> Option<PathBuf> {
    for candidate in [
        std::env::var_os("NX_LLVM").map(PathBuf::from),
        std::env::var_os("NX_LLVM_BIN").map(PathBuf::from),
        std::env::var_os("NX_TOOLCHAIN").map(|p| PathBuf::from(p).join("llvm").join("bin")),
    ].into_iter().flatten() {
        if candidate.join("clang-cl.exe").is_file() { return Some(candidate); }
    }
    if let Some(path) = which("clang-cl.exe") { return path.parent().map(Path::to_path_buf); }
    let installed = PathBuf::from(std::env::var("ProgramFiles").unwrap_or_else(|_| r"C:\Program Files".into())).join(r"LLVM\bin");
    if installed.join("clang-cl.exe").is_file() { return Some(installed); }
    repo.and_then(workspace_llvm_bin)
}

/// One build prerequisite and whether it was found (the page lists them before Set up).
#[derive(Serialize, Clone)]
pub struct Tool {
    pub name: String,
    pub ok: bool,
    pub found: String,
    pub hint: String,
}

pub fn check_tools(package: bool, repo: Option<&Path>) -> Vec<Tool> {
    if package {
        return vec![]; // everything the release package needs is in it
    }
    let pf = |rel: &str| PathBuf::from(std::env::var("ProgramFiles").unwrap_or_else(|_| r"C:\Program Files".into())).join(rel);
    let pf86 = |rel: &str| PathBuf::from(std::env::var("ProgramFiles(x86)").unwrap_or_else(|_| r"C:\Program Files (x86)".into())).join(rel);
    let llvm = find_llvm_bin(repo).map(|bin| bin.join("clang-cl.exe"));
    let items: Vec<(&str, Option<PathBuf>, &str)> = vec![
        ("Visual Studio C++ tools", find_tool(&[], &[pf86(r"Microsoft Visual Studio\Installer\vswhere.exe")]),
         "Visual Studio 2022 Build Tools or newer with \"Desktop development with C++\"."),
        ("LLVM (clang-cl)", llvm,
         "LLVM 23 or newer: place clang+llvm-23* under the workspace toolchain folder, or set NX_LLVM to its bin folder."),
        ("CMake", find_tool(&["cmake.exe"], &[pf(r"CMake\bin\cmake.exe")]), "CMake 3.25 or newer (winget install Kitware.CMake)."),
        ("Ninja", find_tool(&["ninja.exe"], &[local_appdata(r"Microsoft\WinGet\Links\ninja.exe")]), "winget install Ninja-build.Ninja"),
        ("uv (Python)", find_tool(&["uv.exe"], &[local_appdata(r"Microsoft\WinGet\Links\uv.exe")]), "winget install astral-sh.uv"),
        (".NET SDK", find_tool(&["dotnet.exe"], &[pf(r"dotnet\dotnet.exe")]), ".NET 10 SDK (winget install Microsoft.DotNet.SDK.10)."),
        ("Git", find_tool(&["git.exe"], &[pf(r"Git\cmd\git.exe")]), "winget install Git.Git (fetches the shader translator's sources)."),
    ];
    items
        .into_iter()
        .map(|(name, p, hint)| Tool {
            name: name.into(),
            ok: p.is_some(),
            found: p.map(|p| crate::dump::fwd(&p)).unwrap_or_default(),
            hint: hint.into(),
        })
        .collect()
}

/// The Ryujinx source tree the shader translator is built from (MIT). `RYUBING_DIR`, else `<repo>/work/refs/ryubing`, cloned on
/// first use from the project's public repository.
const RYUBING_GIT: &str = "https://git.ryujinx.app/ryubing/ryujinx.git";
/// The revision tools/shaders' patches were made against (tools/shaders/scripts/setup_ryujinx.ps1).
const RYUBING_COMMIT: &str = "a82350bb774f70fcbd41c9987bf67a3775409963";

fn run_package_steps(r: &Runner, p: &Paths, tools: &Path, jobs: usize, runner_setting: &str) -> Result<bool, String> {
    let game = &p.game_dir;
    fs::create_dir_all(game).map_err(|e| format!("cannot create {}: {}", game.display(), e))?;
    if !p.python().is_file() {
        return Err(format!(
            "The bundled Python is missing ({}). Extract the release zip again so tools/ is complete.",
            crate::dump::fwd(&p.python())
        ));
    }
    if !package_build_ok(p) {
        let mut c = Command::new(p.python());
        c.arg("-I").arg(tools.join("build").join("fefw_build.py")).arg("--exefs").arg(&p.exefs).arg("--game").arg(game);
        c.args(["--jobs", &jobs.to_string()]);
        let variant = crate::cpu::build_variant();
        if let Some(v) = variant {
            c.args(["--cpu", v]);
        }
        let what = if variant == Some("compat") {
            "Recompiling the game code from your dump (the compatible build for processors without FMA3)"
        } else {
            "Recompiling the game code from your dump"
        };
        if !r.run("build", what, c, game)? {
            return Ok(false);
        }
        *BUILD_CHECK.lock().unwrap() = None;
    }
    shader_steps(r, p, jobs, game)?;
    if r.shared.cancel.load(Ordering::SeqCst) {
        return Ok(false);
    }
    r.progress("ini", "", 0, 0, "Writing fefw.ini");
    if let Some(note) = crate::settings::ensure_play_paths(&p.game_dir, p)? {
        r.log(vec![note]);
    }
    prepare_step(r, p, jobs, runner_setting)
}

/// Size and modification time of a file ("" when missing): enough to tell that it was replaced.
fn file_stamp(path: &Path) -> String {
    fs::metadata(path)
        .map(|m| {
            let t = m.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs()).unwrap_or(0);
            format!("{}:{}", m.len(), t)
        })
        .unwrap_or_default()
}

/// What the shader corpus was extracted from; a corpus whose marker differs is extracted again.
fn corpus_marker(p: &Paths) -> serde_json::Value {
    serde_json::json!({
        "translator_version": SHADER_TRANSLATOR_VERSION,
        "romfs": crate::dump::fwd(&p.romfs),
        "exefs": crate::dump::fwd(&p.plain_exefs()),
        "main": file_stamp(&p.plain_exefs().join("main")),
        "extractor": file_stamp(&p.extractor()),
    })
}

/// Blocks the harness set aside because they crashed the translator (status "crash" in results.tsv).
fn crashed_shaders(p: &Paths) -> usize {
    fs::read_to_string(p.spirv().join("results.tsv")).unwrap_or_default().lines().skip(1).filter(|l| l.split('\t').nth(4) == Some("crash")).count()
}

/// Finds the shaders in the romfs and main and translates them to SPIR-V (both modes).
fn shader_steps(r: &Runner, p: &Paths, jobs: usize, cwd: &Path) -> Result<bool, String> {
    if shaders_ok(p) {
        return Ok(true);
    }
    if !p.harness().is_file() {
        return Err(format!("The shader translator is missing ({}).", crate::dump::fwd(&p.harness())));
    }
    let _ = fs::remove_file(p.shader_stamp());
    // The corpus is deleted once the shaders are translated, so one that is still there is from a Set up that stopped in
    // the translation. Its marker (written only after the extractor finished) says which dump and extractor made it.
    let marker = p.corpus().join("setup-corpus.json");
    let want = corpus_marker(p);
    let have: serde_json::Value = serde_json::from_str(&fs::read_to_string(&marker).unwrap_or_default()).unwrap_or_default();
    if have == want && p.corpus().join("unique.tsv").is_file() {
        r.log(vec![format!("Reusing the shaders found earlier in {}", crate::dump::fwd(&p.corpus()))]);
    } else {
        let _ = fs::remove_file(&marker);
        let mut c = Command::new(p.python());
        c.arg("-I").arg(p.extractor()).arg("--romfs").arg(&p.romfs).arg("--main").arg(p.plain_exefs().join("main"));
        c.arg("--exefs").arg(p.plain_exefs()).arg("--out").arg(p.corpus()).args(["--jobs", &jobs.min(8).to_string(), "--progress"]);
        c.arg("--programs-bin").arg(p.programs_bin());
        if !r.run("shaders", "Finding the shaders in your romfs", c, cwd)? {
            return Ok(false);
        }
        fs::write(&marker, want.to_string()).map_err(|e| e.to_string())?;
    }
    let lower64 = no_float64();
    if lower64 {
        r.log(vec![
            "An Intel GPU was found: the shaders are translated without 64-bit floats, which Intel Arc A-series cards cannot run (GitHub issue #45)."
                .into(),
        ]);
    }
    // the shaders the preparation compiled are about to change; it runs again after the translation
    let _ = fs::remove_file(p.prepare_file("prepare.state"));
    let _ = fs::remove_file(p.prepare_file("prepare.skipped"));
    let harness = |jobs: usize| {
        let mut c = if p.package.is_some() {
            Command::new(p.harness())
        } else {
            let mut c = Command::new("dotnet");
            c.arg(p.harness());
            c
        };
        c.arg("--batch").arg(p.corpus()).arg("--out").arg(p.spirv()).args(["--jobs", &jobs.to_string()]);
        // the harness also reads FEFW_NO_FLOAT64 itself; =0 has to win over an Intel GPU, so the variable is not passed on
        c.env_remove("FEFW_NO_FLOAT64");
        if lower64 {
            c.arg("--no-float64");
        }
        // SPIR-V validation needs spirv-val from the Vulkan SDK, which players do not have (the release package never validates)
        if p.package.is_some() || (std::env::var_os("VULKAN_SDK").is_none() && which("spirv-val.exe").is_none()) {
            c.arg("--no-val");
        }
        c
    };
    // The harness survives translator crashes itself (it runs the translation in a child process and sets a crashing
    // shader aside, GitHub issue #46). If it still fails, once more with one job: it resumes from its journal.tsv.
    match r.run("shaders", "Translating the shaders to SPIR-V", harness(jobs), cwd) {
        Ok(true) => {}
        Ok(false) => return Ok(false),
        Err(e) => {
            r.log(vec![format!(
                "The shader translation failed ({}); trying again with one job, from where it stopped.",
                e.lines().next().unwrap_or("")
            )]);
            if !r.run("shaders", "Translating the shaders to SPIR-V (again, one at a time)", harness(1), cwd)? {
                return Ok(false);
            }
        }
    }
    let crashed = crashed_shaders(p);
    if crashed > 0 {
        r.log(vec![format!(
            "{crashed} shaders crashed the translator and were left out; the game draws without them (status \"crash\" in {}).",
            crate::dump::fwd(&p.spirv().join("results.tsv"))
        )]);
    }
    let stamp = serde_json::json!({ "complete": true, "translator_version": SHADER_TRANSLATOR_VERSION, "romfs": crate::dump::fwd(&p.romfs), "crashed": crashed, "no_float64": lower64 });
    fs::write(p.shader_stamp(), stamp.to_string()).map_err(|e| e.to_string())?;
    // the corpus is only the extractor's output for the translator: several GB the game never reads
    let _ = fs::remove_dir_all(p.corpus());
    Ok(true)
}

/// Linux with no display for fefw.exe (a launcher run without the window, say by a background service): it does not fail then, it waits
/// for ever. A runner the player set may bring a display of its own (xvfb-run, gamescope), so only the automatic one is judged.
fn no_display(runner_setting: &str, var: impl Fn(&str) -> Option<std::ffi::OsString>) -> bool {
    cfg!(target_os = "linux")
        && runner_setting.trim().is_empty()
        && ["DISPLAY", "WAYLAND_DISPLAY"].iter().all(|k| var(k).is_none_or(|v| v.is_empty()))
}

/// Compiles the shader parts of every program of the game on this GPU and driver (fefw.exe --prepare-shaders), so the first play session has no
/// shader to compile. Needs the translated shaders and fefw.ini. It saves time and nothing else: when it cannot run or fails, Set up goes on
/// (the game then compiles while it is played) and the step is marked given up. Instant when the runtime's state file says this GPU and driver
/// were prepared already (--if-needed).
fn prepare_step(r: &Runner, p: &Paths, jobs: usize, runner_setting: &str) -> Result<bool, String> {
    if !p.exe().is_file() || !shaders_ok(p) || r.shared.cancel.load(Ordering::SeqCst) {
        return Ok(!r.shared.cancel.load(Ordering::SeqCst));
    }
    let skipped = p.prepare_file("prepare.skipped");
    let _ = fs::remove_file(&skipped);
    let give_up = |what: String| {
        r.log(vec![format!("Shader preparation skipped ({what}). The game compiles shaders while you play instead; Set up tries again next time.")]);
        let _ = fs::write(&skipped, what);
    };
    if no_display(runner_setting, |k| std::env::var_os(k)) {
        give_up("there is no display: DISPLAY and WAYLAND_DISPLAY are not set".into());
        return Ok(true);
    }
    if !p.programs_bin().is_file() {
        // a Set up from before the list existed: list the programs again (about half a minute), keeping nothing else of the extraction
        let tmp = p.prepare_file("programs-tmp");
        let mut c = Command::new(p.python());
        c.arg("-I").arg(p.extractor()).arg("--romfs").arg(&p.romfs).arg("--main").arg(p.plain_exefs().join("main"));
        c.arg("--exefs").arg(p.plain_exefs()).arg("--out").arg(&tmp).args(["--jobs", &jobs.min(8).to_string()]);
        c.arg("--programs-bin").arg(p.programs_bin());
        let res = r.run("prepare", "Listing the game's shader programs", c, &p.game_dir);
        let _ = fs::remove_dir_all(&tmp);
        match res {
            Ok(true) => {}
            Ok(false) => return Ok(false),
            Err(e) => {
                give_up(format!("the program list could not be made: {}", e.lines().next().unwrap_or("")));
                return Ok(true);
            }
        }
    }
    // On Linux fefw.exe is a Windows binary and runs through the same Proton / umu-run / Wine runner Play uses (GitHub issue #91).
    let runner = match crate::play::runner(runner_setting) {
        Ok(r) => r,
        Err(e) => {
            give_up(e);
            return Ok(true);
        }
    };
    let mut c = crate::play::exe_command(&p.exe(), &p.game_dir, &runner, &[]);
    c.args(["--prepare-shaders", "--if-needed"]);
    match r.run("prepare", "Preparing the shaders for your GPU (a minute or two, once)", c, &p.game_dir) {
        Ok(true) => Ok(true),
        Ok(false) => Ok(false),
        Err(e) => {
            give_up(format!("fefw.exe --prepare-shaders failed: {}", e.lines().next().unwrap_or("")));
            Ok(true)
        }
    }
}

fn run_steps(r: &Runner, p: &Paths, jobs: usize, runner_setting: &str) -> Result<bool, String> {
    if let Some(tools) = &p.package {
        return run_package_steps(r, p, tools, jobs, runner_setting);
    }
    let repo = &p.repo;
    let py = p.python();
    if !python_ok(p) {
        let uv = find_tool(&["uv.exe"], &[local_appdata(r"Microsoft\WinGet\Links\uv.exe")])
            .ok_or("uv was not found. Install it (winget install astral-sh.uv) and press Set up again.")?;
        if !py.is_file() {
            let mut c = Command::new(&uv);
            c.args(["venv", "tools/.venv", "--python", "3.13"]);
            if !r.run("python", "Creating the Python environment (tools/.venv)", c, repo)? {
                return Ok(false);
            }
        }
        let mut c = Command::new(&uv);
        c.args(["pip", "install", "--python", "tools/.venv", "-r", "recomp/requirements.txt"]);
        if !r.run("python", "Installing the Python packages", c, repo)? {
            return Ok(false);
        }
    }
    if !exefs_ok(p) {
        let mut c = Command::new(&py);
        c.arg("-I").arg("tools/nso/prepare_exefs.py").arg("--exefs").arg(&p.exefs).arg("--out").arg(p.plain_exefs());
        if !r.run("exefs", "Reading the game code from your dump", c, repo)? {
            return Ok(false);
        }
    }
    if !p.dev_mod_build() && !lift_done(p) {
        let sites = lift_sites_file(p)?;
        let mut c = Command::new("cmd.exe");
        c.args(["/d", "/c", "recomp\\scripts\\build_game.cmd", "fefw"]).env("NX_JOBS", jobs.to_string()).env("FEFW_SITES", &sites);
        // the build for this processor (cpu.rs): FMA3 code only where the CPU has FMA3 (recomp/games/fefw/CMakeLists.txt FEFW_CPU_VARIANT)
        let variant = crate::cpu::build_variant().unwrap_or("fma");
        c.env("FEFW_CPU_VARIANT", variant);
        // not a variant while the libraries are being replaced: an interrupted build must not leave the old stamp vouching for a mix
        fs::create_dir_all(p.libs()).map_err(|e| e.to_string())?;
        fs::write(p.libs().join("cpu_variant.txt"), "building").map_err(|e| e.to_string())?;
        if !r.run("lift", "Recompiling the game code (an hour or more the first time)", c, repo)? {
            return Ok(false);
        }
        fs::write(p.libs().join("cpu_variant.txt"), variant).map_err(|e| e.to_string())?;
    }
    if !textures_ok(p) {
        let mut c = Command::new(powershell());
        c.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "tools/textures/scripts/fetch_third_party.ps1"]);
        if !r.run("textures", "Fetching the texture transcoder sources (astc-encoder, bc7enc)", c, repo)? {
            return Ok(false);
        }
    }
    if p.dev_mod_build() && !dev_build_ok(p) {
        let mut c = Command::new(&py);
        c.arg("-I").arg(repo.join("tools").join("mods").join("dev_build.py"))
            .arg("build").arg("--repo").arg(repo).arg("--game").arg(&p.game_dir)
            .arg("--jobs").arg(jobs.to_string());
        if !r.run("lift", "Building the enabled ExeFS mods into a separate game", c, repo)? {
            return Ok(false);
        }
    } else if !p.dev_mod_build() && !exe_ok(p) {
        let mut c = Command::new(powershell());
        c.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "game-build\\scripts\\build_fefw.ps1", "-Jobs", &jobs.to_string(), "-LibDir"])
            .arg(p.libs()); // an OsStr argument: any folder name works
        c.env("FEFW_CPU_VARIANT", crate::cpu::build_variant().unwrap_or("fma"));
        if !r.run("exe", "Building fefw.exe", c, repo)? {
            return Ok(false);
        }
    }
    if !p.harness().is_file() || !shaders_ok(p) {
        let staged = repo.join("tools").join("shaders").join("third_party").join("ryubing");
        if !staged.is_dir() {
            let src = std::env::var("RYUBING_DIR").map(PathBuf::from).unwrap_or_else(|_| repo.join("work").join("refs").join("ryubing"));
            if !src.is_dir() {
                let git = find_tool(&["git.exe"], &[]).ok_or("Git was not found; it is needed once to fetch the shader translator's sources.")?;
                let mut c = Command::new(&git);
                c.args(["clone", "--filter=blob:none", "--no-checkout", RYUBING_GIT]).arg(&src);
                if !r.run("harness", "Fetching the Ryujinx shader translator sources (MIT)", c, repo)? {
                    return Ok(false);
                }
                let mut c = Command::new(&git);
                c.arg("-C").arg(&src).args(["checkout", RYUBING_COMMIT]);
                if !r.run("harness", "Checking out the translator's tested revision", c, repo)? {
                    return Ok(false);
                }
            }
            let mut c = Command::new(powershell());
            c.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "tools\\shaders\\scripts\\setup_ryujinx.ps1", "-Ryubing"]).arg(&src);
            if !r.run("harness", "Staging the translator sources", c, repo)? {
                return Ok(false);
            }
        }
        let mut c = Command::new("dotnet");
        c.args(["build", "tools\\shaders\\harness\\SpirvHarness.csproj", "-c", "Release"]);
        if !r.run("harness", "Building the shader translator", c, repo)? {
            return Ok(false);
        }
    }
    if !shader_steps(r, p, jobs, repo)? {
        return Ok(false);
    }
    r.progress("ini", "", 0, 0, "Writing fefw.ini");
    if let Some(note) = crate::settings::ensure_play_paths(&p.game_dir, p)? {
        r.log(vec![note]);
    }
    r.log(vec![format!("fefw.ini is in {}", crate::dump::fwd(&p.game_dir))]);
    prepare_step(r, p, jobs, runner_setting)
}

/// Holds a logind sleep and idle inhibitor while it lives, so the PC does not suspend in the middle of a long setup. The lock belongs
/// to a systemd-inhibit that waits on its stdin: dropping this ends it, and so does the launcher exiting or crashing (the pipe closes).
#[cfg(target_os = "linux")]
struct SleepInhibit(std::process::Child);

#[cfg(target_os = "linux")]
impl SleepInhibit {
    fn acquire() -> Option<SleepInhibit> {
        let mut cmd = Command::new("systemd-inhibit");
        host_env(&mut cmd);
        cmd.args(["--what=sleep:idle", "--who=fefw launcher", "--why=Setting up fefw", "--mode=block", "cat"]);
        let mut child = cmd.stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::null()).spawn().ok()?;
        std::thread::sleep(Duration::from_millis(100));
        match child.try_wait() {
            Ok(None) => Some(SleepInhibit(child)),
            _ => None,
        }
    }
}

#[cfg(target_os = "linux")]
impl Drop for SleepInhibit {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// Runs the whole setup on this thread. Returns the final progress (done, cancelled or error).
pub fn run(
    shared: &Arc<Shared>,
    p: &Paths,
    jobs: usize,
    runner_setting: &str,
    emit: &(dyn Fn(Progress) + Sync),
    emit_log: &(dyn Fn(Vec<String>) + Sync),
) -> Progress {
    let _ = fs::create_dir_all(p.repo.join("work"));
    let file = fs::OpenOptions::new().create(true).append(true).open(p.log()).ok();
    let r = Runner {
        shared,
        emit,
        emit_log,
        log_file: Mutex::new(file),
        llvm_bin: if p.package.is_some() { None } else { find_llvm_bin(Some(&p.repo)) },
        log_path: p.log(),
    };
    r.log(vec![format!("--- setup: dump exefs {}, romfs {}", crate::dump::fwd(&p.exefs), crate::dump::fwd(&p.romfs))]);
    // Clear any cached base RomFS tree so re-running Set up after updating/re-dumping the game always rescans fresh.
    let _ = fs::remove_file(p.game_dir.join("romfs_cache.bin"));
    if let Some(exe_dir) = p.exe().parent() {
        let _ = fs::remove_file(exe_dir.join("romfs_cache.bin"));
    }
    #[cfg(target_os = "linux")]
    let _inhibit = SleepInhibit::acquire();
    #[cfg(target_os = "linux")]
    if _inhibit.is_none() {
        r.log(vec!["note: could not block sleep (systemd-inhibit); keep the PC awake until setup finishes".into()]);
    }
    let end = match run_steps(&r, p, jobs, runner_setting) {
        Ok(true) => Progress { state: "done".into(), message: "Setup finished. Press Play.".into(), ..Default::default() },
        Ok(false) => Progress { state: "cancelled".into(), message: "Paused. Set up continues where it stopped.".into(), ..Default::default() },
        Err(e) => {
            r.log(vec![format!("error: {e}")]);
            Progress { state: "error".into(), message: e, ..Default::default() }
        }
    };
    *shared.progress.lock().unwrap() = end.clone();
    end
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn progress_lines() {
        assert_eq!(ninja_progress("[12/345] Building CXX object x.obj"), Some((12, 345)));
        assert_eq!(ninja_progress("[ 3/ 9] x"), Some((3, 9)));
        assert_eq!(ninja_progress("no"), None);
        assert_eq!(tool_progress("scan: 5 10"), Some((String::new(), 5, 10)));
        assert_eq!(tool_progress("prepare: 4900 9943"), Some((String::new(), 4900, 9943)));
        assert_eq!(tool_progress("@progress link 0 1 Linking fefw.exe"), Some(("link".into(), 0, 1)));
        assert_eq!(tool_progress("[12/345] Building CXX object x.obj"), Some((String::new(), 12, 345)));
        assert_eq!(tool_progress("prepare: ready"), None);
        assert_eq!(tool_progress("prepare: done programs=9943 prepared=9943 failed=0 seconds=42.0"), None);
    }

    #[test]
    fn lift_variant_stamp() {
        let dir = std::env::temp_dir().join(format!("fefw_lift_variant_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        assert!(lift_variant_ok(&dir, "fma"), "a folder without a stamp is the FMA3 build");
        assert!(!lift_variant_ok(&dir, "compat"));
        fs::write(dir.join("cpu_variant.txt"), "compat").unwrap();
        assert!(lift_variant_ok(&dir, "compat"));
        assert!(!lift_variant_ok(&dir, "fma"), "switching variant is a new build");
        fs::write(dir.join("cpu_variant.txt"), "building").unwrap();
        assert!(!lift_variant_ok(&dir, "compat") && !lift_variant_ok(&dir, "fma"), "an unfinished build matches no variant");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn mods_fingerprint_follows_build_inputs() {
        let dir = std::env::temp_dir().join(format!("fefw_mods_fp_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let exefs = dir.join("fps").join("exefs");
        fs::create_dir_all(&exefs).unwrap();
        let empty = mods_fingerprint(&dir);
        fs::write(exefs.join("readme.txt"), "x").unwrap();
        fs::write(dir.join("fps").join("note.ips"), "x").unwrap(); // not in exefs/
        assert_eq!(mods_fingerprint(&dir), empty, "files the build does not read");
        fs::write(exefs.join("60fps.pchtxt"), "@nsobid-00").unwrap();
        let one = mods_fingerprint(&dir);
        assert_ne!(one, empty, "a patch added");
        fs::write(exefs.join("60fps.pchtxt"), "@nsobid-0000").unwrap();
        assert_ne!(mods_fingerprint(&dir), one, "a patch changed");
        let changed = mods_fingerprint(&dir);
        fs::rename(dir.join("fps"), dir.join("-fps")).unwrap();
        assert_ne!(mods_fingerprint(&dir), changed, "a mod switched off");
        fs::remove_dir_all(&dir).unwrap();
        assert_eq!(mods_fingerprint(&dir), mods_fingerprint(&dir.join("missing")), "no mods folder");
    }

    #[test]
    fn finds_workspace_llvm_without_environment_variable() {
        let root = std::env::temp_dir().join(format!("fefw-llvm-test-{}", std::process::id()));
        let repo = root.join("checkout");
        let bin = root.join("toolchain").join("clang+llvm-23.1.5-x86_64-pc-windows-msvc").join("bin");
        fs::create_dir_all(&repo).unwrap();
        fs::create_dir_all(&bin).unwrap();
        fs::write(bin.join("clang-cl.exe"), []).unwrap();
        assert_eq!(workspace_llvm_bin(&repo), Some(bin));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn mod_build_ids() {
        let main = "DB93B0125F4850601D61C3EDDE012F8EB2ECFE77000000000000000000000000";
        assert!(same_build("DB93B0125F4850601D61C3EDDE012F8EB2ECFE77", main));
        assert!(same_build("db93b0125f4850601d61c3edde012f8eb2ecfe77", main));
        assert!(!same_build("DB93B0125F4850601D61C3EDDE012F8EB2ECFE78", main));
        assert!(!same_build("DB93B012", main)); // too short to name a build
        assert!(!same_build("45EAC7636FFC32C7", main));
        assert!(same_build("45EAC7636FFC32C7", "")); // no build ID known: assume it
    }

    /// A run folder with one code mod: its sites are reported until the lift's and the exe's site lists have them, and the lift's
    /// list adds them. A problem in a .sites file is reported with the release script's message.
    #[test]
    fn mod_sites_against_the_build() {
        let root = std::env::temp_dir().join(format!("fefw-sites-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let repo = root.join("repo");
        let game = root.join("play");
        let p = Paths {
            repo: repo.clone(),
            exefs: root.join("dump"),
            romfs: root.join("dump"),
            dlc: None,
            game_dir: game.clone(),
            package: None,
            lang: String::new(),
            skip_update_check: false,
        };
        // main's NSO header: .text size at +0x18 covering every site below, the build ID at +0x40
        let mut nso = vec![0u8; 0x60];
        nso[0x18..0x1C].copy_from_slice(&0x0200_0000u32.to_le_bytes());
        nso[0x40..0x54].copy_from_slice(&[
            0xDB, 0x93, 0xB0, 0x12, 0x5F, 0x48, 0x50, 0x60, 0x1D, 0x61, 0xC3, 0xED, 0xDE, 0x01, 0x2F, 0x8E, 0xB2, 0xEC, 0xFE, 0x77,
        ]);
        fs::create_dir_all(p.plain_exefs()).unwrap();
        fs::write(p.plain_exefs().join("main"), &nso).unwrap();
        let own = repo.join("recomp").join("games").join("fefw").join("main.sites");
        fs::create_dir_all(own.parent().unwrap()).unwrap();
        fs::write(&own, "# hook sites of main\ninline 0x71007bf1c0\nmidhook 0x710188bd30\n").unwrap();
        let ex = game.join("mods").join("Some_Mod").join("exefs");
        fs::create_dir_all(&ex).unwrap();
        fs::write(
            ex.join("some.sites"),
            "@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\nmidhook 0x710188bd30   # in main.sites already\nmidhook 0x71016f21f4\n",
        )
        .unwrap();
        // a mod for another game build is left out
        let other = game.join("mods").join("Old_Mod").join("exefs");
        fs::create_dir_all(&other).unwrap();
        fs::write(other.join("old.sites"), "@nsobid 45EAC7636FFC32C7\nmidhook 0x7100001000\n").unwrap();
        // mods are found as the runtime finds them: a switched-off folder (-, _, .) is skipped, another title's 16-digit hex
        // folder is skipped, this title's is walked through, and a mod may sit in a subfolder
        let off = game.join("mods").join("-Off_Mod").join("exefs");
        fs::create_dir_all(&off).unwrap();
        fs::write(off.join("off.sites"), "@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\nmidhook 0x71016f21f8\n").unwrap();
        let other_title = game.join("mods").join("contents").join("0100ABCDEF012345").join("Sub_Mod").join("exefs");
        fs::create_dir_all(&other_title).unwrap();
        fs::write(other_title.join("sub.sites"), "@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\nmidhook 0x71016f21fc\n").unwrap();
        let nested = game.join("mods").join("contents").join("04007EF01F8AE000").join("Nested_Mod").join("exefs");
        fs::create_dir_all(&nested).unwrap();
        fs::write(nested.join("nested.sites"), "@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\nmidhook 0x71016f2200\n").unwrap();

        let sites = mod_sites(&p).unwrap();
        assert_eq!(sites.len(), 3); // the two midhooks of Some_Mod plus Nested_Mod's
        assert!(sites.iter().any(|(n, _, _, _)| n == "Some_Mod"));
        assert!(sites.iter().any(|(n, _, _, _)| n == "contents/04007EF01F8AE000/Nested_Mod"));
        assert!(!sites.iter().any(|(n, _, _, _)| n.contains("Off_Mod") || n.contains("Sub_Mod")));
        // no exe list yet: nothing to compare with
        assert!(mods_missing_sites(&p).is_empty());
        // no lift list yet: compared with base main.sites, missing Some_Mod's and Nested_Mod's new sites
        assert_eq!(mods_missing_lift_sites(&p), vec!["contents/04007EF01F8AE000/Nested_Mod".to_string(), "Some_Mod".to_string()]);
        assert_eq!(missing_sites_detail(&p), "new hook sites from contents/04007EF01F8AE000/Nested_Mod, Some_Mod: Set up lifts the game with them");
        let list = p.exe().with_extension("sites");
        fs::create_dir_all(list.parent().unwrap()).unwrap();
        fs::write(&list, fs::read_to_string(&own).unwrap()).unwrap();
        assert_eq!(mods_missing_sites(&p), vec!["contents/04007EF01F8AE000/Nested_Mod".to_string(), "Some_Mod".to_string()]);
        // an exe and libraries whose times are in order are still not enough while the exe's list lacks a mod's site:
        // without this the exe step is "done", Set up skips build_fefw.ps1 and the lift step asks for a rebuild forever
        fs::create_dir_all(p.libs()).unwrap();
        for m in MODULES {
            fs::write(p.libs().join(format!("fefw_{m}.lib")), "lib").unwrap();
        }
        fs::write(p.exe(), "exe").unwrap();
        assert!(!exe_ok(&p));
        // the lift step is not done either: the libraries are older than nothing was lifted with yet
        assert!(!lift_done(&p));
        let all = "# hook sites of main\ninline 0x71007bf1c0\nmidhook 0x710188bd30\nmidhook 0x71016f21f4\nmidhook 0x71016f2200\n";
        fs::write(&list, all).unwrap();
        assert!(exe_ok(&p));
        fs::write(&list, fs::read_to_string(&own).unwrap()).unwrap();

        // the combined list is about to be written; make sure it lands in a later timestamp tick than the libraries so the
        // freshness comparison below is exact (Linux caches inode timestamps per tick)
        std::thread::sleep(Duration::from_millis(20));
        let lift = lift_sites_file(&p).unwrap();
        assert_eq!(lift, p.libs().join("main_with_mods.sites"));
        let text = fs::read_to_string(&lift).unwrap();
        assert!(text.contains("midhook 0x71016f21f4   # Some_Mod/exefs/some.sites:3"));
        assert!(text.contains("midhook 0x71016f2200   # contents/04007EF01F8AE000/Nested_Mod/exefs/nested.sites:2"));
        assert_eq!(text.matches("0x710188bd30").count(), 1); // not added twice
        assert!(!text.contains("0x7100001000")); // another game build
        assert!(!text.contains("0x71016f21f8")); // switched off
        assert!(!text.contains("0x71016f21fc")); // another title's folder
                                                 // the file is only rewritten when it changes, so the lift is not started again for nothing
        let first = fs::metadata(&lift).unwrap().modified().unwrap();
        assert_eq!(lift_sites_file(&p).unwrap(), lift);
        assert_eq!(fs::metadata(&lift).unwrap().modified().unwrap(), first);
        // the combined list was just written and the libraries were not lifted with it yet: the lift step is still todo, and it
        // stays todo when the lift is paused (Set up writes the list before running the lift)
        assert!(!lift_done(&p));
        assert!(mods_missing_lift_sites(&p).is_empty()); // every mod site is in the list ...
                                                         // ... but the libraries are older than it, so a paused lift is not mistaken for a finished one. A finished lift leaves
                                                         // newer libraries: touch them and the step is done.
        std::thread::sleep(Duration::from_millis(20));
        for m in MODULES {
            fs::write(p.libs().join(format!("fefw_{m}.lib")), "lib").unwrap();
        }
        assert!(lift_done(&p));
        // an update changes main.sites: CMake's copy beside the libraries no longer matches it, so the lift is todo again
        let copy = p.libs().join("main.sites");
        fs::write(&copy, &text).unwrap(); // the combined list: main.sites plus mods' hooks
        assert!(lift_done(&p));
        let before = fs::read_to_string(&own).unwrap();
        fs::write(&own, format!("{before}# a new comment\n")).unwrap();
        assert!(lift_done(&p), "a comment is no change");
        fs::write(&own, format!("{before}inline 0x71007bf1c4\n")).unwrap();
        assert!(!lift_done(&p), "a new site");
        fs::write(&copy, format!("{text}inline 0x71007bf1c4\n")).unwrap();
        assert!(lift_done(&p));
        fs::write(&own, &before).unwrap();
        assert!(lift_done(&p), "a hook that is gone does no harm");
        fs::write(&own, format!("{before}patch 0x71007bf1c8 d503201f d65f03c0\n")).unwrap();
        assert!(!lift_done(&p), "a new patch");
        fs::write(&copy, format!("{text}patch 0x71007bf1c8 d503201f d65f03c0\n")).unwrap();
        assert!(lift_done(&p));
        fs::write(&own, format!("{before}patch 0x71007bf1c8 d503201f d503201f\n")).unwrap();
        assert!(!lift_done(&p), "a patch with another word");
        fs::write(&own, &before).unwrap();
        assert!(!lift_done(&p), "a patch that is gone");
        fs::write(&copy, &text).unwrap();
        assert!(lift_done(&p));
        // the exe built from that list: nothing missing there either
        fs::write(&list, &text).unwrap();
        assert!(mods_missing_sites(&p).is_empty());

        // problems are reported with the release script's messages, and they are the lift step's note meanwhile. Only Some_Mod
        // is left with sites (Nested_Mod's would be outside the shrunken .text below and sort first).
        fs::remove_file(nested.join("nested.sites")).unwrap();
        let bad = ex.join("bad.sites");
        let check = |text: &str, want: &str| {
            fs::write(&bad, text).unwrap();
            let e = mod_sites(&p).unwrap_err();
            assert!(e.contains(want), "{e}");
            assert_eq!(missing_sites_detail(&p), e);
            assert_eq!(mod_sites_problem(&p).as_deref(), Some(e.as_str()));
        };
        // a kind a mod cannot add here (another port's, or a later one) is skipped, not an error (issue #165)
        fs::write(&bad, "@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\npatch 0x7100000000\ndynimm 0x7100000004\n").unwrap();
        let kept = mod_sites(&p).unwrap();
        assert!(!kept.iter().any(|(_, _, k, _)| k == "patch" || k == "dynimm"), "{kept:?}");
        assert!(kept.iter().all(|(_, _, k, _)| MOD_SITE_KINDS.contains(&k.as_str())), "{kept:?}");
        check("@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\nmidhook zz\n", "bad.sites:2: expected \"<kind> <guest address in hex>\"");
        check("midhook 0x71016f21f4\n", "Some_Mod/exefs/bad.sites has no @nsobid line");
        check("@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\nmidhook 0x71016f21f2\n", "is not an instruction address in main");
        // main's code: .text at memory offset 0 with 0x1000 bytes (NSO header +0x14 / +0x18)
        nso[0x18..0x1C].copy_from_slice(&0x1000u32.to_le_bytes());
        fs::write(p.plain_exefs().join("main"), &nso).unwrap();
        check("@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\nmidhook 0x7100002000\n", "0x7100002000 is outside main's code");
        // no mod adds sites any more: the stale combined list is removed, so it cannot stand for a lift made without them
        fs::remove_dir_all(game.join("mods")).unwrap();
        assert!(p.libs().join("main_with_mods.sites").is_file());
        assert_eq!(lift_sites_file(&p).unwrap(), own);
        assert!(!p.libs().join("main_with_mods.sites").exists());
        let _ = fs::remove_dir_all(&root);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn shader_preparation_needs_a_display() {
        let env =
            |set: &'static [(&'static str, &'static str)]| move |k: &str| set.iter().find(|(n, _)| *n == k).map(|(_, v)| std::ffi::OsString::from(v));
        assert!(no_display("", env(&[])));
        assert!(no_display(" ", env(&[("DISPLAY", "")])));
        assert!(!no_display("", env(&[("DISPLAY", ":0")])));
        assert!(!no_display("", env(&[("WAYLAND_DISPLAY", "wayland-1")])));
        assert!(!no_display("xvfb-run wine", env(&[])));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn prepare_step_runs_exe_through_linux_runner() {
        let root = std::env::temp_dir().join(format!("fefw-prepare-runner-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let game = root.join("play");
        let p = Paths {
            repo: root.clone(),
            exefs: root.join("exefs"),
            romfs: root.join("romfs"),
            dlc: None,
            game_dir: game.clone(),
            package: Some(root.join("tools")),
            lang: String::new(),
            skip_update_check: false,
        };
        fs::create_dir_all(p.spirv()).unwrap();
        fs::write(p.exe(), b"MZ").unwrap(); // non-executable PE stub: fails if invoked directly without a runner
        fs::write(p.spirv().join("results.tsv"), "h\n").unwrap();
        let stamp = serde_json::json!({
            "complete": true,
            "translator_version": SHADER_TRANSLATOR_VERSION,
            "romfs": crate::dump::fwd(&p.romfs),
            "crashed": 0,
            "no_float64": no_float64(),
        });
        fs::write(p.shader_stamp(), stamp.to_string()).unwrap();
        fs::write(p.programs_bin(), b"MPRPRG01").unwrap();

        let shared = Arc::new(Shared::default());
        let r = Runner { shared: &shared, emit: &|_| {}, emit_log: &|_| {}, log_file: Mutex::new(None), log_path: p.log(), llvm_bin: None };
        let runner = r#"sh -c 'printf "%s\n" "$@" > args.txt' _"#;
        assert_eq!(prepare_step(&r, &p, 1, runner), Ok(true));
        assert!(!p.prepare_file("prepare.skipped").exists(), "should not fall back to skipped when runner succeeds");
        let args = fs::read_to_string(game.join("args.txt")).unwrap();
        let lines: Vec<&str> = args.lines().collect();
        assert_eq!(lines, [p.exe().to_str().unwrap(), "--prepare-shaders", "--if-needed"]);
        let _ = fs::remove_dir_all(&root);
    }
}
