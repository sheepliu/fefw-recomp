// SPDX-License-Identifier: GPL-3.0-or-later
//! Keeps the shipped language files current. An update leaves a file in locales/ where it is, since it may be the player's own, and
//! discards the new one (update.rs). So every language file travels inside the executable (build.rs), and a copy on disk that the
//! player never edited is replaced by it.
//!
//! `<name>.shipped` beside a file records the hash of the version last put or seen there. A file that still has that hash was not
//! edited; any other difference is the player's, and stays.

use crate::settings::write_atomic;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::Path;

include!(concat!(env!("OUT_DIR"), "/shipped_locales.rs"));

/// [`hash`] of every zh.ini committed before the launcher began recording the one it installed: a file that is one of these was
/// never edited by the player. A closed list; later versions, and every other language, are known from the record.
const EARLIER_ZH: [&str; 25] = [
    "05d8400e675b5c165e390f2c2461c981b84d27d9622594061631ef2b22462b2c",
    "0c21218c7d34426cd575f68595113c13a62c96fb08cbec648f0fdbdff5aaa9f5",
    "13ed1b2ec010264e0b86f91b80e5cd7945a777d40e822d18f6eacc9b433b027a",
    "382eea8637ca1f1065ceb7c2c6a7d894c1540fece55f91336555eb9f6e72510a",
    "3c5b521ba1c3d49a4015c91ef6af226ceaca2241d7ee5d4526198fa9beeaf5b1",
    "417e141c4607b061c32e7943eb916ed387063d4d77993d8d5ab503d6741bae71",
    "42e2126a20750c2e4e40c78bdca7c0fdfcaeed7085d91387e86a5392c3323430",
    "4760023a8b90173258bb4db747758c9576cae9ff7fa1df4cc0623915be6c3ce2",
    "5217c7642e622fb183f4b5297a1350139d0ee89b35f92ef39b88731985c9813f",
    "6259c8a813e30137c6bb2b98a1e93bb335461e14a8d8b228082e51638af8cee9",
    "6697865ad10386a6eea033fbee19edf6efc9c2fa81b1af2bee02fde24bf46245",
    "6c82ec84148083b5ac8cb31f9729ef709553d75f0dcb01cb254a7bfb1d11e230",
    "760bc22ca18ca8ea0132bb93371cdc7baad4d9c32a540b6c531f7378193b645c",
    "763c3cb047102ea7dcf23dd994e7086f1ed2e835572abdae8778778724581bb8",
    "8475147655df8bd251bc83b99d9dae8f1e1ba8cb01df70a5dff490299e523bf8",
    "8a1bf682156c7099037e324335de9dc7cc9d9c60bde62e7e59e8aed241a24038",
    "8ff28b9c63256722d6a5d8a2ced4aacdc6941eebf764e1ce9b1a8d44791add9d",
    "99eae0586b9fd4415493ea84c99d851122db10ae6333a38af475eff0c144e68a",
    "9ac95979289803e8ae24278b9e0477444a29db8348558632505182d92743f53b",
    "9f6c4053bece811aab740f41df4e0e0f41db3f804e616d82f232e8118af8e3a5",
    "a37eddf2bdbab1de39231e37170a212b89271e46c4a9a3407b01bdd48e9d9ce8",
    "adda65f82f7ddc3b2808b25e7a8b7e215ab11baf7121c4b4b89627fdc8630f45",
    "e05d9494d74bc1fd5051d7431b8fd5d9793b1dba1b4351f5155fa774192e28e6",
    "eedd8f0e5988380bb2bcd85fe750cebfb9575f515432f42c9642135c726a55b7",
    "f53b201db631836c79d613ad7d9511b28f1073f4e4a625e2a4ae21701a6811dd",
];

/// SHA-256 of a language file, whatever its line endings and with or without a byte-order mark.
fn hash(bytes: &[u8]) -> String {
    let text = String::from_utf8_lossy(bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes)).replace("\r\n", "\n");
    Sha256::digest(text.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}

/// Brings the shipped language files in `folder` (a run folder's or the package's locales/) up to this launcher's, except those
/// the player edited. A file that is not there is not added.
pub fn refresh(folder: &Path) {
    for (name, text) in FILES {
        let earlier: &[&str] = if *name == "zh.ini" { &EARLIER_ZH } else { &[] };
        refresh_file(folder, name, text, earlier);
    }
}

fn refresh_file(folder: &Path, name: &str, shipped: &str, earlier: &[&str]) {
    let path = folder.join(name);
    let record = folder.join(format!("{name}.shipped"));
    let Ok(bytes) = fs::read(&path) else { return };
    let (have, new) = (hash(&bytes), hash(shipped.as_bytes()));
    let recorded = fs::read_to_string(&record).unwrap_or_default();
    if have != new {
        if recorded.trim() != have && !earlier.contains(&have.as_str()) {
            return; // the player's own
        }
        if let Err(e) = write_atomic(&path, shipped) {
            eprintln!("[locales] {e}");
            return;
        }
    }
    if recorded.trim() != new {
        if let Err(e) = write_atomic(&record, &new) {
            eprintln!("[locales] {e}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("fefw-shipped-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    fn shipped(name: &str) -> &'static str {
        FILES.iter().find(|(n, _)| *n == name).unwrap_or_else(|| panic!("{name} is not packed in")).1
    }

    #[test]
    fn both_kinds_of_language_file_are_packed_in() {
        for (name, text) in FILES {
            assert!(name.ends_with(".ini") || name.ends_with(".json"), "{name}");
            assert!(!text.is_empty(), "{name}");
        }
        shipped("zh.ini");
        shipped("template.json");
    }

    #[test]
    fn an_untouched_file_is_brought_up_to_date_and_an_edited_one_is_not() {
        let dir = tmp("any");
        let file = dir.join("de.json");
        let record = dir.join("de.json.shipped");
        let read = || fs::read_to_string(&file).unwrap();

        // the version an earlier launcher recorded, since untouched (Windows line endings or not)
        fs::write(&file, "old\r\n").unwrap();
        fs::write(&record, hash(b"old\n")).unwrap();
        refresh_file(&dir, "de.json", "new\n", &[]);
        assert_eq!(read(), "new\n");
        assert_eq!(fs::read_to_string(&record).unwrap(), hash(b"new\n"));

        // the player's edit stays, now and on the next start
        fs::write(&file, "mine\n").unwrap();
        refresh_file(&dir, "de.json", "newer\n", &[]);
        refresh_file(&dir, "de.json", "newer\n", &[]);
        assert_eq!(read(), "mine\n");
    }

    #[test]
    fn a_file_without_a_record_is_the_players_unless_it_is_a_known_old_version() {
        let dir = tmp("unrecorded");
        let file = dir.join("de.ini");
        fs::write(&file, "old\n").unwrap();

        refresh_file(&dir, "de.ini", "new\n", &[]);
        assert_eq!(fs::read_to_string(&file).unwrap(), "old\n");

        refresh_file(&dir, "de.ini", "new\n", &[&hash(b"old\n")]);
        assert_eq!(fs::read_to_string(&file).unwrap(), "new\n");
    }

    #[test]
    fn a_file_seen_as_shipped_is_recorded_and_a_missing_one_is_not_added() {
        let dir = tmp("record");
        fs::write(dir.join("zh.ini"), shipped("zh.ini")).unwrap();

        refresh(&dir);

        assert_eq!(fs::read_to_string(dir.join("zh.ini.shipped")).unwrap(), hash(shipped("zh.ini").as_bytes()));
        assert!(!dir.join("template.json").exists());
        assert!(!dir.join("template.json.shipped").exists());
    }
}
