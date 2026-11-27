// SPDX-License-Identifier: GPL-3.0-or-later
//! Running out of memory: a warning on the Play page when Windows has little left to give, and the cause of a run that ended for it.
//!
//! What counts is the commit limit (RAM plus page file), not free RAM. The game commits a 9 GB heap at boot, and more for itself
//! and the graphics driver afterwards.

use serde::Serialize;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

/// The sentence a run that ran out of memory ends with. The runtime's crash report gives the same one (runtime/src/mem/memory.cpp,
/// kOutOfMemoryHint, after "The game stopped: "), and src/rusttext.js translates it.
pub const OUT_OF_MEMORY: &str =
    "The game stopped: it ran out of memory. Close other programs and try again; on Windows, keep the page file on (system managed).";

const GIB: u64 = 1 << 30;
/// Under this much free commit even a page file that Windows can still grow may not keep up with the game's start.
const LOW_FREE: u64 = 4 * GIB;
/// A commit limit this close to the RAM size means the page file is off or tiny: the limit cannot grow.
const SMALL_PAGE_FILE: u64 = 2 * GIB;
/// What the game wants free when the limit cannot grow: the heap, its own memory and the driver's.
const NEED_FIXED_LIMIT: u64 = 20 * GIB;
/// How old the periodic memory line may be at a failure to say anything about it (the runtime writes one every 30 s). Older than
/// that, Windows may have grown the page file or another program given memory back.
const FIGURE_AGE_S: f64 = 45.0;

#[derive(Serialize, Debug, PartialEq)]
pub struct Warning {
    /// "low": little memory left. "noPageFile": the page file is off or tiny, and what is left may not be enough.
    pub kind: &'static str,
    /// Free commit in GiB, one decimal.
    pub free_gb: f64,
}

fn judge(ram: u64, limit: u64, free: u64) -> Option<Warning> {
    let kind = if limit.saturating_sub(ram) < SMALL_PAGE_FILE && free < NEED_FIXED_LIMIT {
        "noPageFile"
    } else if free < LOW_FREE {
        "low"
    } else {
        return None;
    };
    Some(Warning { kind, free_gb: (free as f64 / GIB as f64 * 10.0).round() / 10.0 })
}

/// (RAM, commit limit, free commit) in bytes.
#[cfg(windows)]
fn status() -> Option<(u64, u64, u64)> {
    #[repr(C)]
    #[derive(Default)]
    struct MemoryStatusEx {
        length: u32,
        memory_load: u32,
        total_phys: u64,
        avail_phys: u64,
        total_page_file: u64,
        avail_page_file: u64,
        total_virtual: u64,
        avail_virtual: u64,
        avail_extended_virtual: u64,
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn GlobalMemoryStatusEx(buffer: *mut MemoryStatusEx) -> i32;
    }
    let mut s = MemoryStatusEx { length: std::mem::size_of::<MemoryStatusEx>() as u32, ..Default::default() };
    // SAFETY: the buffer is a MEMORYSTATUSEX with its length set, writable for that many bytes.
    let ok = unsafe { GlobalMemoryStatusEx(&mut s) } != 0;
    ok.then_some((s.total_phys, s.total_page_file, s.avail_page_file))
}

/// Linux hands out memory without such a limit.
#[cfg(not(windows))]
fn status() -> Option<(u64, u64, u64)> {
    None
}

pub fn warning() -> Option<Warning> {
    status().and_then(|(ram, limit, free)| judge(ram, limit, free))
}

/// The end of the log, where a run that failed says why.
pub fn log_tail(path: &Path) -> String {
    const MAX: u64 = 512 * 1024;
    let read = || -> std::io::Result<Vec<u8>> {
        let mut f = std::fs::File::open(path)?;
        let len = f.metadata()?.len();
        f.seek(SeekFrom::Start(len.saturating_sub(MAX)))?;
        let mut bytes = Vec::new();
        f.take(MAX).read_to_end(&mut bytes)?;
        Ok(bytes)
    };
    read().map(|b| String::from_utf8_lossy(&b).into_owned()).unwrap_or_default()
}

/// The number that follows `before` in a line.
fn number_after(line: &str, before: &str) -> Option<f64> {
    let rest = &line[line.find(before)? + before.len()..];
    rest[..rest.find(|c: char| !(c.is_ascii_digit() || c == '.' || c == '-')).unwrap_or(rest.len())].parse().ok()
}

/// A log line's time in seconds: "[  12.345678 I t1 name] ...".
fn line_time(line: &str) -> Option<f64> {
    line.strip_prefix('[')?.split_whitespace().next()?.parse().ok()
}

/// Whether a run that ended badly ran out of memory, from its log. An allocation that fails is not enough on its own: full video
/// memory fails the same way, and many failures are harmless. It takes the heap the game could not get, or a failure in the last
/// minute while the system had next to no commit left, by the failing line's own figure or one taken shortly before it.
pub fn ran_out(log: &str) -> bool {
    let end = log.lines().filter_map(line_time).fold(0.0, f64::max);
    let mut last_figure: Option<(f64, f64)> = None; // (time, free commit) of the periodic memory line
    for line in log.lines() {
        // ERROR_COMMITMENT_LIMIT, ERROR_NOT_ENOUGH_MEMORY, ERROR_COMMITMENT_MINIMUM (after the runtime's retries), at SetHeapSize or at a
        // heap page's first use ([memory] heap_lazy_commit)
        if (line.contains("heap_commit [") || line.contains("mem: lazy commit ["))
            && ["1455", "8", "635"].iter().any(|e| line.trim_end().ends_with(&format!("VirtualAlloc failed, error {e}")))
        {
            return true;
        }
        if line.contains("memory: system: RAM ") {
            let free = line.split_once(" GB, commit ").and_then(|(_, rest)| number_after(&format!("commit {rest}"), "commit "));
            if let (Some(at), Some(free)) = (line_time(line), free) {
                last_figure = Some((at, free));
            }
        }
        let Some(t) = line_time(line).filter(|t| *t >= end - 60.0) else {
            continue;
        };
        // for a failure that does not say what was left itself (older runtimes, the movie decoder)
        let low_just_before = last_figure.is_some_and(|(at, free)| t - at <= FIGURE_AGE_S && free < 2.0);
        let vk = line.contains("vk.memory: allocation failed (-1)") || line.contains("vk.memory: allocation failed (-2)");
        let render = line.contains("render thread C++ exception (system commit free ");
        if vk || render {
            // newer runtimes say what the system had left at that moment: that decides, either way
            match number_after(line, "system commit free ") {
                Some(free) if (0.0..1.0).contains(&free) => return true,
                None if low_just_before => return true,
                _ => {}
            }
        }
        // -12: ENOMEM
        if line.contains("movie: AV1 decode error -12") && low_just_before {
            return true;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn warns_without_a_page_file_or_with_little_left() {
        let w = |ram, limit, free| judge(ram * GIB, limit * GIB, free * GIB);
        assert_eq!(w(32, 32, 17), Some(Warning { kind: "noPageFile", free_gb: 17.0 }));
        assert_eq!(w(32, 33, 19).map(|x| x.kind), Some("noPageFile"));
        assert_eq!(w(64, 64, 40), None, "plenty left although the limit cannot grow");
        assert_eq!(w(32, 66, 3), Some(Warning { kind: "low", free_gb: 3.0 }));
        assert_eq!(w(16, 19, 9), None, "a system managed page file grows");
        assert_eq!(w(32, 40, 20), None);
    }

    #[test]
    fn a_heap_that_cannot_be_committed_is_out_of_memory() {
        let line = |e: u32| format!("[ 10.765533 E t3 main] mem: heap_commit [0, +294a00000): VirtualAlloc failed, error {e}\r\n");
        assert!(ran_out(&line(1455)));
        assert!(ran_out(&line(8)));
        assert!(!ran_out(&line(87)), "another error");
        assert!(!ran_out(&line(1455000)));
        assert!(!ran_out("[ 1.0 E t3 main] mem: commit 7100000000 (+1000): VirtualAlloc failed, error 1455\n"));
        assert!(ran_out("[ 900.0 E t3 guest-87] mem: lazy commit [1200000, +200000): VirtualAlloc failed, error 1455\n"));
        assert!(!ran_out("[ 900.0 E t3 guest-87] mem: lazy commit [1200000, +200000): VirtualAlloc failed, error 87\n"));
    }

    #[test]
    fn a_failed_allocation_counts_only_with_no_commit_left() {
        let mem_at = |t: f64, free: &str| {
            format!("[ {t} I t1 -] memory: system: RAM 17.8 GB free of 31.9 GB, commit {free} GB free of 31.9 GB; video memory: 8.0 GB\n")
        };
        let mem = |free: &str| mem_at(30.0, free);
        let vk = |t: f64, extra: &str| {
            format!("[ {t} E t2 nvn-render] vk.memory: allocation failed (-2), bytes 8388608, live 250 (1 failures since the last line, 1 in all){extra}\n")
        };
        let end = "[ 47.0 I t2 -] nvn: bye\n";
        // older runtimes: the periodic line decides
        assert!(ran_out(&(mem("0.2") + &vk(46.2, "") + end)));
        assert!(!ran_out(&(mem("12.0") + &vk(46.2, "") + end)), "video memory, not the system's");
        assert!(!ran_out(&(mem("0.2") + end)), "little left but nothing failed");
        assert!(!ran_out(&(vk(0.9, "") + &mem("0.5") + "[ 600.0 I t1 -] x: y\n")), "a failure long before the end");
        let av1 = "[ 203.9 E t3 movie-vdec] movie: AV1 decode error -12 (packet at 1)\n[ 219.0 W t2 x] y: z\n";
        assert!(ran_out(&(mem_at(180.5, "1.2") + av1)));
        // a figure from minutes before the failure, or from after it, says nothing about it
        assert!(!ran_out(&(mem("0.2") + av1)));
        assert!(!ran_out(&(mem("0.2") + &vk(200.0, "") + "[ 201.0 I t1 -] x: y\n")));
        assert!(!ran_out(&(mem_at(180.5, "9.0") + &vk(203.0, "") + &mem_at(210.5, "0.3"))));
        // newer runtimes: the figure on the line decides
        assert!(ran_out(&(vk(8.7, "; system commit free 0.1 GB") + "[ 8.8 I t1 -] x: y\n")));
        assert!(!ran_out(&(mem("0.5") + &vk(46.2, "; system commit free 9.3 GB") + end)));
        assert!(!ran_out(&(vk(46.2, "; system commit free -1.0 GB") + end)), "the figure could not be read");
        assert!(ran_out("[ 5.0 E t2 nvn-render] nvn.gpu: render thread C++ exception (system commit free 0.0 GB of 31.9 GB): ...\n"));
        assert!(!ran_out(""));
    }
}
