// SPDX-License-Identifier: GPL-3.0-or-later
//! Linux: a DualSense on a USB cable plays its vibration (and its speaker) through an audio output of its own, so their
//! strength follows that output's volume, which can start well below full. Proton gives the game no way to change it (the
//! Windows call reports full volume and refuses to set it), so the launcher raises it while a game it started runs, and puts it
//! back afterwards. On Windows the game does this itself (runtime/src/gfx/pad_volume.cpp).
//!
//! What was there before is written to a file before anything is changed, so a volume that could not be put back (the pad was
//! unplugged first, or the launcher closed) is put back later.

use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Mutex, PoisonError};
use std::thread::JoinHandle;
use std::time::Duration;

/// A volume of 100%, as pactl counts it.
const FULL: u32 = 65536;
/// How often a pad plugged in while the game runs is looked for.
const LOOK: Duration = Duration::from_secs(3);

/// Output name to its channels' volumes before they were raised.
type Saved = BTreeMap<String, Vec<u32>>;
/// Runs pactl with these arguments: its output, or None when it is missing or fails.
type Pactl<'a> = dyn FnMut(&[&str]) -> Option<String> + 'a;

/// Whether the settings ask for it: `[pad]` of controls.ini (None when the game has not written the file yet, which is the
/// defaults). Not when nothing would vibrate through the pad's audio output anyway.
pub fn wanted(controls: Option<&str>) -> bool {
    let ini = crate::ini::Ini::parse(controls.unwrap_or_default());
    let get = |key: &str| ini.get("pad", key).unwrap_or_default().trim().to_ascii_lowercase();
    let off = |key: &str| matches!(get(key).as_str(), "0" | "false" | "no" | "off");
    !off("dualsense_full_volume") && !off("rumble") && !matches!(get("haptic").as_str(), "off" | "none" | "rumble" | "standard")
}

/// The DualSense outputs in `pactl list short sinks` that can carry vibration (four channels).
fn pad_outputs(list: &str) -> Vec<String> {
    list.lines()
        .filter_map(|line| {
            let mut columns = line.split('\t').skip(1);
            let name = columns.next()?;
            let spec = columns.nth(1)?;
            let pad = name.contains("DualSense") || name.contains("Sony_Interactive_Entertainment_Wireless_Controller");
            (pad && spec.split_whitespace().any(|w| w == "4ch")).then(|| name.to_string())
        })
        .collect()
}

/// The channels' volumes in `pactl get-sink-volume` ("Volume: front-left: 26214 /  40% / -23.88 dB,   front-right: ..."), or
/// None when there are none.
fn volumes(text: &str) -> Option<Vec<u32>> {
    let parts: Vec<&str> = text.lines().find(|l| l.starts_with("Volume:"))?.split('/').collect();
    let v: Option<Vec<u32>> = parts.windows(2).filter(|w| w[1].trim().ends_with('%')).map(|w| w[0].split_whitespace().last()?.parse().ok()).collect();
    v.filter(|v| !v.is_empty())
}

fn volume_of(pactl: &mut Pactl, name: &str) -> Option<Vec<u32>> {
    volumes(&pactl(&["get-sink-volume", name])?)
}

fn is_full(v: &[u32]) -> bool {
    v.iter().all(|&c| c == FULL)
}

/// Raises every pad output not seen yet to full volume. `handled` keeps each from being raised again while it stays, so that a
/// player who turns it down during the game is left alone. `record` is called with what to put back before anything changes.
fn raise(pactl: &mut Pactl, saved: &mut Saved, handled: &mut HashSet<String>, record: &mut dyn FnMut(&Saved) -> bool) {
    let Some(list) = pactl(&["list", "short", "sinks"]) else { return };
    let present = pad_outputs(&list);
    handled.retain(|name| present.contains(name));
    for name in present {
        if handled.contains(&name) {
            continue;
        }
        let Some(now) = volume_of(pactl, &name) else { continue };
        // already full: nothing to change, and a volume saved earlier (which this one may be our own raise of) is kept
        if !is_full(&now) {
            saved.insert(name.clone(), now);
            if !record(saved) {
                saved.remove(&name);
                continue; // without a record of what to put back, leave it as it is
            }
            pactl(&["set-sink-volume", &name, &FULL.to_string()]);
        }
        handled.insert(name);
    }
}

/// Puts the saved volumes back on the outputs that are there. One that is no longer full was changed by someone since, and is
/// left as it is. One that is gone stays saved for later.
fn restore(pactl: &mut Pactl, saved: &mut Saved) {
    let Some(list) = pactl(&["list", "short", "sinks"]) else { return };
    let present = pad_outputs(&list);
    saved.retain(|name, before| {
        if !present.contains(name) {
            return true;
        }
        let Some(now) = volume_of(pactl, name) else { return true };
        if !is_full(&now) {
            return false;
        }
        let values: Vec<String> = before.iter().map(u32::to_string).collect();
        let mut args = vec!["set-sink-volume", name.as_str()];
        args.extend(values.iter().map(String::as_str));
        pactl(&args).is_none()
    });
}

fn host_pactl(args: &[&str]) -> Option<String> {
    let mut cmd = Command::new("pactl");
    crate::setup::host_env(&mut cmd);
    let out = cmd.args(args).env("LC_ALL", "C").stdin(Stdio::null()).stderr(Stdio::null()).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).into_owned())
}

fn load(file: &Path) -> Saved {
    std::fs::read_to_string(file).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default()
}

fn store(file: &Path, saved: &Saved) -> bool {
    if saved.is_empty() {
        return !file.exists() || std::fs::remove_file(file).is_ok();
    }
    // written beside it and moved over it: a write cut short must not take the earlier record with it
    serde_json::to_string(saved).is_ok_and(|t| crate::settings::write_atomic(file, &t).is_ok())
}

/// The recovery `recover` started, until a session has seen it finish.
static RECOVERY: Mutex<Option<JoinHandle<()>>> = Mutex::new(None);

/// Puts back, in the background, what an earlier session could not.
pub fn recover(file: PathBuf) {
    start_recovery(move || restore_leftover(&file));
}

fn start_recovery(job: impl FnOnce() + Send + 'static) {
    *RECOVERY.lock().unwrap_or_else(PoisonError::into_inner) = Some(std::thread::spawn(job));
}

/// A session starts only after the recovery: one that ran beside it would find the pad at the full volume left over, take it
/// as dealt with, and then have it lowered for the rest of the game.
fn wait_for_recovery() {
    if let Some(t) = RECOVERY.lock().unwrap_or_else(PoisonError::into_inner).take() {
        let _ = t.join();
    }
}

fn restore_leftover(file: &Path) {
    let mut saved = load(file);
    if saved.is_empty() {
        return;
    }
    restore(&mut host_pactl, &mut saved);
    store(file, &saved);
}

/// Raises the pad's volume while it lives; `finish` puts it back.
pub struct Session {
    stop: mpsc::Sender<()>,
    thread: JoinHandle<()>,
}

impl Session {
    /// `file`: where the volumes to put back are kept.
    pub fn start(file: PathBuf) -> Session {
        Session::start_with(file, host_pactl)
    }

    fn start_with(file: PathBuf, mut pactl: impl FnMut(&[&str]) -> Option<String> + Send + 'static) -> Session {
        let (stop, stopped) = mpsc::channel();
        let thread = std::thread::spawn(move || {
            wait_for_recovery();
            let mut saved = load(&file);
            let mut handled = HashSet::new();
            loop {
                raise(&mut pactl, &mut saved, &mut handled, &mut |s| store(&file, s));
                if stopped.recv_timeout(LOOK) != Err(RecvTimeoutError::Timeout) {
                    break;
                }
            }
            restore(&mut pactl, &mut saved);
            store(&file, &saved);
        });
        Session { stop, thread }
    }

    /// Puts the volumes back and returns once that is done.
    pub fn finish(self) {
        let _ = self.stop.send(());
        let _ = self.thread.join();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    const PAD: &str = "alsa_output.usb-Sony_Interactive_Entertainment_DualSense_Wireless_Controller-00.Direct__Direct__sink";

    /// A stand-in for pactl: outputs with their channel count and volumes, and every set call made.
    #[derive(Default)]
    struct Fake {
        sinks: Vec<(String, u32, Vec<u32>)>,
        sets: Vec<String>,
    }

    impl Fake {
        fn run(&mut self, args: &[&str]) -> Option<String> {
            match args {
                ["list", "short", "sinks"] => {
                    Some(self.sinks.iter().enumerate().map(|(i, (n, ch, _))| format!("{i}\t{n}\tPipeWire\ts16le {ch}ch 48000Hz\tIDLE\n")).collect())
                }
                ["get-sink-volume", name] => {
                    let v = &self.sinks.iter().find(|s| s.0 == *name)?.2;
                    let channels: Vec<String> = v.iter().map(|c| format!("ch: {c} /  {}% / -1.00 dB", c * 100 / FULL)).collect();
                    Some(format!("Volume: {}\n        balance 0.00\n", channels.join(",   ")))
                }
                ["set-sink-volume", name, values @ ..] => {
                    self.sets.push(values.join(" "));
                    let s = self.sinks.iter_mut().find(|s| s.0 == *name)?;
                    let new: Vec<u32> = values.iter().map(|v| v.parse().unwrap()).collect();
                    s.2 = if new.len() == 1 { vec![new[0]; s.2.len()] } else { new };
                    Some(String::new())
                }
                _ => None,
            }
        }
    }

    fn raise_on(f: &mut Fake, saved: &mut Saved, handled: &mut HashSet<String>) {
        raise(&mut |a| f.run(a), saved, handled, &mut |_| true);
    }

    #[test]
    fn the_volume_is_raised_once_and_put_back() {
        let mut f = Fake { sinks: vec![(PAD.into(), 4, vec![26214, 26214, 20000, 20000])], ..Default::default() };
        let (mut saved, mut handled) = (Saved::new(), HashSet::new());
        raise_on(&mut f, &mut saved, &mut handled);
        raise_on(&mut f, &mut saved, &mut handled);
        assert_eq!(f.sinks[0].2, vec![FULL; 4]);
        assert_eq!(f.sets, vec!["65536"]);
        restore(&mut |a| f.run(a), &mut saved);
        assert_eq!(f.sinks[0].2, vec![26214, 26214, 20000, 20000]);
        assert!(saved.is_empty());
    }

    #[test]
    fn a_change_by_the_player_is_left_alone() {
        let mut f = Fake { sinks: vec![(PAD.into(), 4, vec![26214; 4])], ..Default::default() };
        let (mut saved, mut handled) = (Saved::new(), HashSet::new());
        raise_on(&mut f, &mut saved, &mut handled);
        f.sinks[0].2 = vec![30000; 4];
        raise_on(&mut f, &mut saved, &mut handled);
        restore(&mut |a| f.run(a), &mut saved);
        assert_eq!(f.sinks[0].2, vec![30000; 4]);
        assert!(saved.is_empty());
    }

    #[test]
    fn an_unplugged_pad_is_put_back_later() {
        let mut f = Fake { sinks: vec![(PAD.into(), 4, vec![26214; 4])], ..Default::default() };
        let (mut saved, mut handled) = (Saved::new(), HashSet::new());
        raise_on(&mut f, &mut saved, &mut handled);
        let gone = f.sinks.pop().unwrap();
        restore(&mut |a| f.run(a), &mut saved);
        assert_eq!(saved.get(PAD), Some(&vec![26214; 4]));
        // plugged in again: the system remembers the raised volume, which must not replace the saved one
        f.sinks.push(gone);
        raise_on(&mut f, &mut saved, &mut HashSet::new());
        assert_eq!(saved.get(PAD), Some(&vec![26214; 4]));
        restore(&mut |a| f.run(a), &mut saved);
        assert_eq!(f.sinks[0].2, vec![26214; 4]);
        assert!(saved.is_empty());
    }

    #[test]
    fn other_outputs_and_a_full_pad_are_not_touched() {
        let mut f = Fake {
            sinks: vec![
                ("alsa_output.usb-Headset-00.analog-stereo".into(), 2, vec![100; 2]),
                ("alsa_output.usb-Sony_Interactive_Entertainment_Wireless_Controller-00.analog-stereo".into(), 2, vec![100; 2]),
                (PAD.into(), 4, vec![FULL; 4]),
            ],
            ..Default::default()
        };
        let (mut saved, mut handled) = (Saved::new(), HashSet::new());
        raise_on(&mut f, &mut saved, &mut handled);
        assert!(f.sets.is_empty() && saved.is_empty());
    }

    #[test]
    fn nothing_changes_without_a_record() {
        let mut f = Fake { sinks: vec![(PAD.into(), 4, vec![26214; 4])], ..Default::default() };
        let mut saved = Saved::new();
        raise(&mut |a| f.run(a), &mut saved, &mut HashSet::new(), &mut |_| false);
        assert!(f.sets.is_empty() && saved.is_empty());
    }

    #[test]
    fn a_session_waits_for_the_recovery() {
        let dir = std::env::temp_dir().join(format!("fefw-pad-volume-test-{}", std::process::id()));
        let file = dir.join("pad-volume.json");
        assert!(store(&file, &Saved::from([(PAD.to_string(), vec![26214; 4])])));
        // left at full by a session that never finished
        let fake = Arc::new(Mutex::new(Fake { sinks: vec![(PAD.into(), 4, vec![FULL; 4])], ..Default::default() }));
        let (f, record) = (fake.clone(), file.clone());
        start_recovery(move || {
            std::thread::sleep(Duration::from_millis(100)); // still at work when Play is pressed
            let mut saved = load(&record);
            restore(&mut |a| f.lock().unwrap().run(a), &mut saved);
            store(&record, &saved);
        });
        let f = fake.clone();
        let session = Session::start_with(file.clone(), move |a| f.lock().unwrap().run(a));
        std::thread::sleep(Duration::from_millis(500));
        assert_eq!(fake.lock().unwrap().sinks[0].2, vec![FULL; 4], "raised again, after the recovery");
        session.finish();
        assert_eq!(fake.lock().unwrap().sinks[0].2, vec![26214; 4]);
        assert!(!file.exists());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn pactl_output_is_read() {
        let text = "Volume: front-left: 26214 /  40% / -23.88 dB,   front-right: 65536 / 100% / 0.00 dB\n        balance 0.00\n";
        assert_eq!(volumes(text), Some(vec![26214, 65536]));
        assert_eq!(volumes("Volume: mono: 0 /   0% / -inf dB\n"), Some(vec![0]));
        assert_eq!(volumes("Failure: No such entity\n"), None);
        let list =
            format!("61\talsa_output.pci-0000.hdmi-stereo\tPipeWire\ts32le 2ch 48000Hz\tIDLE\n18949\t{PAD}\tPipeWire\ts16le 4ch 48000Hz\tIDLE\n");
        assert_eq!(pad_outputs(&list), vec![PAD.to_string()]);
    }

    #[test]
    fn the_settings_can_turn_it_off() {
        assert!(wanted(None));
        assert!(wanted(Some("[pad]\nhaptic = auto\nrumble = 1\n")));
        for off in ["dualsense_full_volume = 0", "rumble = 0", "haptic = off", "haptic = rumble"] {
            assert!(!wanted(Some(&format!("[pad]\n{off}\n"))), "{off}");
        }
    }
}
