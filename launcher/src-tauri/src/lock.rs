// SPDX-License-Identifier: GPL-3.0-or-later
//! One job at a time in an install folder: a setup, an update, a game being started. The window and a launcher run without one
//! (headless.rs) are separate processes, so they hold launcher.lock there while they change files. The system drops the lock when
//! the process ends, however it ends.

use std::fs::{File, OpenOptions, TryLockError};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

pub const BUSY: &str = "Another setup or update is running for this install. Wait for it to finish.";
const NAME: &str = "launcher.lock";
/// How often, and how far apart, a taken lock is asked for again: held() has it for a moment.
const TRIES: u32 = 20;
const PAUSE: Duration = Duration::from_millis(25);

/// The installs this process holds the lock of.
static HERE: Mutex<Vec<PathBuf>> = Mutex::new(Vec::new());

/// Held until dropped.
pub struct Lock(Option<(File, PathBuf)>);

impl Drop for Lock {
    fn drop(&mut self) {
        // off the list first: the file, and with it the lock, goes after this
        if let Some((_, install)) = &self.0 {
            let mut here = HERE.lock().unwrap();
            if let Some(i) = here.iter().position(|d| d == install) {
                here.remove(i);
            }
        }
    }
}

/// Err(BUSY) while someone else holds it. A folder or file system that cannot lock is not a reason to refuse: the job goes on
/// without one (and says itself when it cannot write there).
pub fn acquire(install: &Path) -> Result<Lock, String> {
    let Ok(f) = OpenOptions::new().create(true).write(true).truncate(false).open(install.join(NAME)) else {
        return Ok(Lock(None));
    };
    for attempt in 1.. {
        match f.try_lock() {
            Ok(()) => break,
            Err(TryLockError::WouldBlock) if attempt < TRIES => std::thread::sleep(PAUSE),
            Err(TryLockError::WouldBlock) => return Err(BUSY.into()),
            Err(TryLockError::Error(_)) => return Ok(Lock(None)),
        }
    }
    HERE.lock().unwrap().push(install.to_path_buf());
    Ok(Lock(Some((f, install.to_path_buf()))))
}

/// For a short write by the window: as acquire, but None when this process holds the lock already. The window's pages stay usable
/// while its own setup or update runs.
pub fn for_write(install: &Path) -> Result<Option<Lock>, String> {
    if HERE.lock().unwrap().iter().any(|d| d == install) {
        return Ok(None);
    }
    acquire(install).map(Some)
}

/// Whether anyone holds it, without making the file. A shared lock, so that two askers do not take each other for a job.
pub fn held(install: &Path) -> bool {
    File::open(install.join(NAME)).is_ok_and(|f| matches!(f.try_lock_shared(), Err(TryLockError::WouldBlock)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("fefw-lock-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn one_holder_at_a_time() {
        let d = tmp("one");
        let first = acquire(&d).unwrap();
        assert_eq!(acquire(&d).err().as_deref(), Some(BUSY));
        drop(first);
        assert!(acquire(&d).is_ok());
        assert!(acquire(&d.join("missing")).is_ok());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn asking_changes_nothing_and_does_not_refuse_a_job() {
        let d = tmp("ask");
        assert!(!held(&d));
        assert!(!d.join(NAME).exists());
        let job = acquire(&d).unwrap();
        assert!(held(&d));
        drop(job);
        assert!(!held(&d));
        // someone asking at the moment a job starts: it waits for them
        let asker = File::open(d.join(NAME)).unwrap();
        asker.try_lock_shared().unwrap();
        assert!(!held(&d), "two askers");
        let waiting = std::thread::spawn(move || {
            std::thread::sleep(PAUSE * 3);
            drop(asker);
        });
        assert!(acquire(&d).is_ok());
        waiting.join().unwrap();
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn the_holder_may_write() {
        let d = tmp("write");
        let short = for_write(&d).unwrap();
        assert!(short.is_some());
        drop(short);
        let job = acquire(&d).unwrap();
        assert!(for_write(&d).unwrap().is_none());
        drop(job);
        // held by someone else: another handle on the file stands for another process
        let other = File::open(d.join(NAME)).unwrap();
        other.try_lock().unwrap();
        assert_eq!(for_write(&d).err().as_deref(), Some(BUSY));
        let _ = std::fs::remove_dir_all(&d);
    }
}
