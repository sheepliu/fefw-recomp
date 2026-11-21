// SPDX-License-Identifier: GPL-3.0-or-later
//! Downloads of the upscaler files (FSR 3, XeSS, DLSS) on the player's request, from the vendors' own GitHub repositories.
//! Nothing here is mirrored or shipped by this project: each file comes from the URL below, is checked against a pinned SHA-256
//! and stays the property of its vendor under their licence (the launcher shows the licence and asks first; see LEGAL.md).
//!
//! The Streamline files live inside NVIDIA's 276 MB release zip. Only the few files needed are read out of it: the zip's central
//! directory and each file's compressed bytes are fetched with HTTP range requests and inflated here.

use serde::Serialize;
use std::fs;
use std::io::{Read, Write};
use std::path::Path;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use crate::update::{curl, sha256_file};

struct File {
    name: &'static str,
    /// lowercase hex SHA-256 of the file as saved
    sha256: &'static str,
    size: u64,
    /// a direct URL; empty for files read out of the Streamline zip
    url: &'static str,
}

macro_rules! amd_v24 {
    ($n:literal) => {
        concat!("https://raw.githubusercontent.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/v2.4.0/Kits/FidelityFX/signedbin/", $n)
    };
}

const FSR: [File; 3] = [
    File {
        name: "amd_fidelityfx_upscaler_vk.dll",
        sha256: "4a7e895af6e0b615d40a84d59eb8c2b1054ad91cdd0c739567940270d51bdab7",
        size: 36_135_696,
        url: amd_v24!("amd_fidelityfx_upscaler_vk.dll"),
    },
    File {
        name: "amd_fidelityfx_loader_vk.dll",
        sha256: "38a56a01fb3565c0757d9f5c1e4e69de4b65e5f2e5769b323b200dc1f142bab9",
        size: 39_184,
        url: amd_v24!("amd_fidelityfx_loader_vk.dll"),
    },
    File {
        name: "amd_fidelityfx_vk.dll",
        sha256: "a1624cc4238fef046f30c4d80ce3f47be63fc5f5373f49e3ee9edb9960f54c78",
        size: 9_332_432,
        url: "https://raw.githubusercontent.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/v1.1.4/PrebuiltSignedDLL/amd_fidelityfx_vk.dll",
    },
];

const XESS: [File; 1] = [File {
    name: "libxess.dll",
    sha256: "251659dd84a3e84de67c886a4186e01f3eca49b00641906fe38bb6b807e5d5b7",
    size: 77_795_704,
    url: "https://raw.githubusercontent.com/intel/xess/v3.0.2/bin/libxess.dll",
}];

/// NVIDIA's Streamline 2.14.16 release (the version the runtime is built against) and the one folder of it that is used.
const SL_ZIP: &str = "https://github.com/NVIDIA-RTX/Streamline/releases/download/v2.14.16/streamline-sdk-v2.14.16.zip";
const SL_ZIP_SIZE: u64 = 276_016_170;
const SL_DIR: &str = "bin/x64/";

const fn sl(name: &'static str, sha256: &'static str, size: u64) -> File {
    File { name, sha256, size, url: "" }
}
const DLSS: [File; 4] = [
    sl("sl.interposer.dll", "b1d7395aebd7b9722841d64dcf7433680020b6a8e421f851c16d5ba0c2182edf", 652_928),
    sl("sl.common.dll", "38eea3b4209afc455636ea372b5933d65fcc13becec6c84d101dcfc4d8648f75", 843_904),
    sl("sl.dlss.dll", "18871eea87c90345da97d8109faa81ea7d5840e8d99b51b55b0e46cfa177f84e", 422_016),
    sl("nvngx_dlss.dll", "d00b5b0ab29c3ca101b70c3a5ecb3a01b7ef33766a3383a28a18623083e6123e", 58_956_912),
];
const DLSS_G: [File; 5] = [
    sl("sl.dlss_g.dll", "3ed9538df724993bea8a050195a6a78b9c8c8a566d5b05ade477aab3c390e90f", 637_056),
    sl("sl.reflex.dll", "443577e869352436ff7ec8f4d9c90418d3e95368436ce219261e0f4f07a38122", 388_736),
    sl("sl.pcl.dll", "5698c38e722318681f215c9fe68e45b9bedd51378acc699d8eee26e961e57770", 360_064),
    sl("nvngx_dlssg.dll", "8d59d49b36410dd6b62fef2503a5871d347415bd85d943fa0848563a95dd50c7", 7_506_544),
    sl("NvLowLatencyVk.dll", "2a77dc3e1c724b7eea5755be0ae7423752e79a2459fae72181a9f00e3507e5d6", 57_840),
];

#[derive(Serialize, Clone, Default)]
pub struct Status {
    /// idle, running, done, error
    pub state: String,
    /// fsr, xess, dlss, dlss_framegen
    pub kind: String,
    pub message: String,
    pub done: u64,
    pub total: u64,
    /// where the files went
    pub folder: String,
}

#[derive(Default)]
pub struct Shared {
    pub status: Mutex<Status>,
    pub cancel: AtomicBool,
}

/// What a cancelled run returns as its error, so the caller can tell it from a failure.
pub const CANCELLED: &str = "Download cancelled.";

impl Shared {
    /// The status, even if a thread panicked while holding it (the status is plain data, so it is still usable).
    pub fn lock(&self) -> std::sync::MutexGuard<'_, Status> {
        self.status.lock().unwrap_or_else(|e| e.into_inner())
    }
    fn set(&self, f: impl FnOnce(&mut Status)) {
        f(&mut self.lock());
    }
    fn check_cancel(&self) -> Result<(), String> {
        if self.cancel.load(Ordering::SeqCst) {
            Err(CANCELLED.into())
        } else {
            Ok(())
        }
    }
}

/// The folder a kind of file goes to (upscalers/<sub> next to the launcher, a place the game searches) and the files to fetch.
fn plan(kind: &str) -> Result<(&'static str, Vec<&'static File>), String> {
    Ok(match kind {
        "fsr" => ("fsr", FSR.iter().collect()),
        "xess" => ("xess", XESS.iter().collect()),
        "dlss" => ("streamline", DLSS.iter().collect()),
        "dlss_framegen" => ("streamline", DLSS.iter().chain(DLSS_G.iter()).collect()),
        other => return Err(format!("Unknown download: {other}")),
    })
}

/// `custom` is the folder set in Settings for this upscaler (a file in it counts, as for the game), or empty for upscalers/<sub> next to the launcher.
pub fn run(shared: &Shared, base: &Path, kind: &str, custom: &str) -> Result<String, String> {
    let (sub, files) = plan(kind)?;
    let custom = custom.trim();
    let dest = if custom.is_empty() {
        base.join("upscalers").join(sub)
    } else {
        let p = Path::new(custom);
        if p.is_file() {
            p.parent().map(|d| d.to_path_buf()).unwrap_or_else(|| p.to_path_buf())
        } else {
            p.to_path_buf()
        }
    };
    shared.set(|s| s.folder = dest.display().to_string());
    fs::create_dir_all(&dest).map_err(|e| format!("cannot create {}: {e}", dest.display()))?;
    shared.set(|s| s.message = "Checking the files already there".into());
    let mut todo: Vec<&File> = Vec::new();
    for f in files {
        shared.check_cancel()?;
        if !has(&dest, f) {
            todo.push(f);
        }
    }
    if todo.is_empty() {
        return Ok(format!("The files are already in {}.", dest.display()));
    }
    let mut zip: Option<Zip> = None;
    for (i, f) in todo.iter().enumerate() {
        shared.check_cancel()?;
        let label = format!("{} ({} of {})", f.name, i + 1, todo.len());
        let part = dest.join(format!("{}.part", f.name));
        let _ = fs::remove_file(&part);
        let url = f.url.to_string();
        if !url.is_empty() {
            get(shared, &url, None, &part, &label, f.size)?;
        } else {
            if zip.is_none() {
                shared.set(|s| s.message = "Reading NVIDIA's release".into());
                zip = Some(Zip::open(shared, &dest)?);
            }
            zip.as_ref().unwrap().extract(shared, f, &part, &label)?;
        }
        shared.set(|s| s.message = format!("Checking {}", f.name));
        let got = sha256_file(&part);
        let got = match got {
            Ok(g) => g,
            Err(e) => {
                let _ = fs::remove_file(&part);
                return Err(e);
            }
        };
        if got != f.sha256 {
            let _ = fs::remove_file(&part);
            return Err(format!(
                "{} does not match the expected file (SHA-256 {got}). It was not installed. Try again, and report it if it keeps happening.",
                f.name
            ));
        }
        let target = dest.join(f.name);
        let _ = fs::remove_file(&target);
        fs::rename(&part, &target).map_err(|e| format!("cannot save {}: {e}", target.display()))?;
    }
    Ok(format!("Saved {} file{} to {}.", todo.len(), if todo.len() == 1 { "" } else { "s" }, dest.display()))
}

fn has(dir: &Path, f: &File) -> bool {
    let p = dir.join(f.name);
    fs::metadata(&p).map(|m| m.len() == f.size).unwrap_or(false) && sha256_file(&p).map(|h| h == f.sha256).unwrap_or(false)
}

/// curl of `url` (or a byte range of it) into `out`, with progress from the size of the file so far and cancel.
fn get(shared: &Shared, url: &str, range: Option<(u64, u64)>, out: &Path, label: &str, expect: u64) -> Result<(), String> {
    let mut c = curl();
    c.args(["--retry", "3", "--speed-limit", "1024", "--speed-time", "60"]);
    if let Some((a, b)) = range {
        // a server that ignores Range would send the whole 276 MB file: refuse it up front when it says how big the body is
        c.arg("--range").arg(format!("{a}-{b}")).arg("--max-filesize").arg((b - a + 1).to_string());
    }
    c.args(["--write-out", "%{http_code}"]);
    let mut child = c
        .arg("--output")
        .arg(out)
        .arg(url)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("cannot run curl: {e}"))?;
    let mb = |b: u64| b as f64 / (1024.0 * 1024.0);
    let status = loop {
        if let Some(s) = child.try_wait().map_err(|e| e.to_string())? {
            break s;
        }
        if shared.cancel.load(Ordering::SeqCst) {
            let _ = child.kill();
            let _ = child.wait();
            let _ = fs::remove_file(out);
            return Err(CANCELLED.into());
        }
        let got = fs::metadata(out).map(|m| m.len()).unwrap_or(0);
        shared.set(|s| {
            s.done = got;
            s.total = expect;
            s.message =
                if expect == 0 { format!("Downloading {label}") } else { format!("Downloading {label}: {:.0} of {:.0} MiB", mb(got), mb(expect)) };
        });
        std::thread::sleep(Duration::from_millis(250));
    };
    if !status.success() {
        let mut err = String::new();
        if let Some(mut e) = child.stderr.take() {
            let _ = e.read_to_string(&mut err);
        }
        let _ = fs::remove_file(out);
        return Err(format!("The download failed: {}", err.trim()));
    }
    if let Some((a, b)) = range {
        let mut code = String::new();
        if let Some(mut o) = child.stdout.take() {
            let _ = o.read_to_string(&mut code);
        }
        let len = fs::metadata(out).map(|m| m.len()).unwrap_or(0);
        if code.trim() != "206" || len != b - a + 1 {
            let _ = fs::remove_file(out);
            return Err(format!("The server did not return the requested part of the file (HTTP {}, {len} bytes).", code.trim()));
        }
    }
    Ok(())
}

const LAYOUT: &str = "NVIDIA's release zip has an unexpected layout.";

fn le16(b: &[u8], at: usize) -> Result<u16, String> {
    b.get(at..at + 2).map(|x| u16::from_le_bytes([x[0], x[1]])).ok_or_else(|| LAYOUT.to_string())
}
fn le32(b: &[u8], at: usize) -> Result<u32, String> {
    b.get(at..at + 4).map(|x| u32::from_le_bytes([x[0], x[1], x[2], x[3]])).ok_or_else(|| LAYOUT.to_string())
}
fn le64(b: &[u8], at: usize) -> Result<u64, String> {
    b.get(at..at + 8).map(|x| u64::from_le_bytes(x.try_into().unwrap())).ok_or_else(|| LAYOUT.to_string())
}

struct Entry {
    name: String,
    method: u16,
    compressed: u64,
    local: u64,
}

/// The central directory of the release zip, read with range requests.
struct Zip {
    entries: Vec<Entry>,
}

impl Zip {
    fn open(shared: &Shared, scratch: &Path) -> Result<Zip, String> {
        let tmp = scratch.join("zip-index.part");
        let read = |a: u64, b: u64| -> Result<Vec<u8>, String> {
            get(shared, SL_ZIP, Some((a, b)), &tmp, "NVIDIA's file list", 0)?;
            let d = fs::read(&tmp).map_err(|e| e.to_string());
            let _ = fs::remove_file(&tmp);
            d
        };
        // the end-of-central-directory record is in the last 22 bytes plus a comment of up to 64 KiB
        let tail = read(SL_ZIP_SIZE - 65_600, SL_ZIP_SIZE - 1)?;
        let at = tail.windows(4).rposition(|w| w == b"PK\x05\x06").ok_or(LAYOUT)?;
        let (mut size, mut off) = (le32(&tail, at + 12)? as u64, le32(&tail, at + 16)? as u64);
        if size == 0xFFFF_FFFF || off == 0xFFFF_FFFF {
            // Zip64: the locator just before the record points at the Zip64 end record, which holds the real size and offset
            let loc = at.checked_sub(20).filter(|l| &tail[*l..*l + 4] == b"PK\x06\x07").ok_or(LAYOUT)?;
            let rec = le64(&tail, loc + 8)?;
            if rec + 56 > SL_ZIP_SIZE {
                return Err(LAYOUT.into());
            }
            let z = read(rec, rec + 55)?;
            if z.get(0..4) != Some(b"PK\x06\x06") {
                return Err(LAYOUT.into());
            }
            (size, off) = (le64(&z, 40)?, le64(&z, 48)?);
        }
        if size == 0 || off.checked_add(size).is_none_or(|e| e > SL_ZIP_SIZE) {
            return Err(LAYOUT.into());
        }
        let cd = read(off, off + size - 1)?;
        let mut entries = Vec::new();
        let mut p = 0;
        while p + 46 <= cd.len() && &cd[p..p + 4] == b"PK\x01\x02" {
            let method = le16(&cd, p + 10)?;
            let (mut comp, usize_, mut local) = (le32(&cd, p + 20)? as u64, le32(&cd, p + 24)?, le32(&cd, p + 42)? as u64);
            let (n, e, c) = (le16(&cd, p + 28)? as usize, le16(&cd, p + 30)? as usize, le16(&cd, p + 32)? as usize);
            let name = String::from_utf8_lossy(cd.get(p + 46..p + 46 + n).ok_or(LAYOUT)?).to_string();
            if name.starts_with(SL_DIR) && name.matches('/').count() == 2 {
                // Zip64 extra field (id 1): the 64-bit values of the fields that are 0xFFFFFFFF, in this order
                if comp == 0xFFFF_FFFF || local == 0xFFFF_FFFF {
                    let extra = cd.get(p + 46 + n..p + 46 + n + e).ok_or(LAYOUT)?;
                    let mut q = 0;
                    let mut found = false;
                    while q + 4 <= extra.len() {
                        let (id, len) = (le16(extra, q)?, le16(extra, q + 2)? as usize);
                        if id == 1 {
                            let mut v = q + 4;
                            if usize_ == 0xFFFF_FFFF {
                                v += 8;
                            }
                            if comp == 0xFFFF_FFFF {
                                comp = le64(extra, v)?;
                                v += 8;
                            }
                            if local == 0xFFFF_FFFF {
                                local = le64(extra, v)?;
                            }
                            found = true;
                            break;
                        }
                        q += 4 + len;
                    }
                    if !found {
                        return Err(LAYOUT.into());
                    }
                }
                entries.push(Entry { name, method, compressed: comp, local });
            }
            p += 46 + n + e + c;
        }
        Ok(Zip { entries })
    }

    /// One file of bin/x64, inflated into `out`.
    fn extract(&self, shared: &Shared, f: &File, out: &Path, label: &str) -> Result<(), String> {
        let want = format!("{SL_DIR}{}", f.name);
        let e = self.entries.iter().find(|e| e.name == want).ok_or_else(|| format!("{} is not in NVIDIA's release any more.", f.name))?;
        if e.compressed == 0 || e.local.checked_add(e.compressed).is_none_or(|x| x > SL_ZIP_SIZE) {
            return Err(LAYOUT.into());
        }
        let scratch = out.with_extension("zpart");
        let res = (|| -> Result<(), String> {
            // the local header says how long the name and extra field are before the data starts
            get(shared, SL_ZIP, Some((e.local, e.local + 29)), &scratch, label, 0)?;
            let head = fs::read(&scratch).map_err(|e| e.to_string())?;
            let data = e.local + 30 + le16(&head, 26)? as u64 + le16(&head, 28)? as u64;
            get(shared, SL_ZIP, Some((data, data + e.compressed - 1)), &scratch, label, e.compressed)?;
            shared.set(|s| s.message = format!("Unpacking {}", f.name));
            let src = fs::File::open(&scratch).map_err(|e| e.to_string())?;
            let mut dst = fs::File::create(out).map_err(|e| format!("cannot write {}: {e}", out.display()))?;
            match e.method {
                0 => {
                    std::io::copy(&mut std::io::BufReader::new(src), &mut dst).map_err(|e| e.to_string())?;
                }
                8 => {
                    std::io::copy(&mut flate2::read::DeflateDecoder::new(std::io::BufReader::new(src)), &mut dst)
                        .map_err(|e| format!("cannot unpack {}: {e}", f.name))?;
                }
                m => return Err(format!("{} uses compression method {m}, which is not supported.", f.name)),
            }
            dst.flush().map_err(|e| e.to_string())
        })();
        let _ = fs::remove_file(&scratch);
        if res.is_err() {
            let _ = fs::remove_file(out);
        }
        res
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Downloads everything for real (about 160 MB): FEFW_FETCH_TEST_DIR=<empty folder> cargo test fetch -- --ignored --nocapture
    #[test]
    #[ignore]
    fn downloads_verify() {
        let base = std::path::PathBuf::from(std::env::var("FEFW_FETCH_TEST_DIR").expect("FEFW_FETCH_TEST_DIR"));
        let shared = Shared::default();
        for kind in ["dlss_framegen", "fsr", "xess"] {
            println!("{kind}: {}", run(&shared, &base, kind, "").unwrap());
        }
        // a second run finds everything in place
        assert!(run(&shared, &base, "dlss_framegen", "").unwrap().starts_with("The files are already"));
    }
}
