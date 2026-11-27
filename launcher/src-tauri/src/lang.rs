// SPDX-License-Identifier: GPL-3.0-or-later
//! The interface language: what the launcher shows when none was picked, and the F1 menu's [gfx] ui_language in step with it.
//!
//! Until the player picks one (launcher.json `lang` empty), both follow the system: the launcher through [`system_choice`], the
//! F1 menu through `ui_language = auto`, which the runtime resolves by the same rules (runtime/src/gfx/menu_i18n.cpp). Once a
//! language is picked, [`f1_value`] gives what fefw.ini has to say.

/// The languages compiled into the launcher (launcher/src/i18n.js). Packs in locales/ add to them.
pub const BUILTIN: [&str; 2] = ["en", "zh"];

/// The system's display language as a locale name ("zh-CN", "zh_TW.UTF-8"), when the system says.
#[cfg(windows)]
pub fn system_tag() -> Option<String> {
    #[link(name = "kernel32")]
    extern "system" {
        fn GetUserDefaultUILanguage() -> u16;
        fn LCIDToLocaleName(locale: u32, name: *mut u16, len: i32, flags: u32) -> i32;
    }
    let mut buf = [0u16; 85]; // LOCALE_NAME_MAX_LENGTH
                              // SAFETY: the buffer is writable for the length passed; the result counts the terminating NUL.
    let n = unsafe { LCIDToLocaleName(u32::from(GetUserDefaultUILanguage()), buf.as_mut_ptr(), buf.len() as i32, 0) };
    (n > 1).then(|| String::from_utf16_lossy(&buf[..n as usize - 1]))
}

/// The same variables, in the same order, that Wine takes the Windows display language from.
#[cfg(not(windows))]
pub fn system_tag() -> Option<String> {
    ["LC_ALL", "LC_MESSAGES", "LANG"].iter().find_map(|k| std::env::var(k).ok().filter(|v| !v.is_empty()))
}

/// The names a language for this locale may go by, best first: "pt_BR.UTF-8" gives "pt-BR", then "pt". Chinese goes by the script:
/// "zh" is the Simplified table, so a Traditional locale never gets it.
fn candidates(tag: &str) -> Vec<String> {
    let tag = tag.split(['.', '@']).next().unwrap_or("").replace('_', "-");
    let mut parts = tag.split('-');
    let primary = parts.next().unwrap_or("").to_ascii_lowercase();
    if primary.is_empty() || primary == "c" || primary == "posix" {
        return Vec::new();
    }
    let rest: Vec<String> = parts.map(|p| p.to_ascii_uppercase()).collect();
    let mut out = vec![tag.clone()];
    if primary == "zh" {
        let has = |names: &[&str]| rest.iter().any(|p| names.contains(&p.as_str()));
        let traditional = has(&["HANT"]) || (!has(&["HANS"]) && has(&["TW", "HK", "MO"]));
        if traditional {
            out.extend(["zh-Hant".to_string(), "zh-TW".to_string()]);
        } else {
            out.extend(["zh-Hans".to_string(), "zh-CN".to_string(), "zh".to_string()]);
        }
    } else {
        out.push(primary);
    }
    out
}

/// The language of `available` that suits the locale, else English.
pub fn match_language(tag: &str, available: &[String]) -> String {
    candidates(tag).iter().find_map(|c| available.iter().find(|a| a.eq_ignore_ascii_case(c))).cloned().unwrap_or_else(|| "en".to_string())
}

/// Every language the launcher can show.
pub fn available() -> Vec<String> {
    let mut out: Vec<String> = BUILTIN.iter().map(|s| s.to_string()).collect();
    out.extend(crate::locale::list_locales().into_iter().map(|l| l.code));
    out
}

/// The launcher's language when none was picked.
pub fn system_choice() -> String {
    system_tag().map(|t| match_language(&t, &available())).unwrap_or_else(|| "en".to_string())
}

/// What [gfx] ui_language has to become for the picked language `saved`, given what fefw.ini says now (`current`). None: leave it.
/// - Nothing picked: left alone; "auto" follows the system, and anything else is the player's own edit.
/// - A value the launcher could not have written (a table the player named by hand) is left alone.
/// - A launcher language the F1 menu has no table for (`has_table`) is English there.
pub fn f1_value(saved: &str, current: &str, available: &[String], has_table: impl Fn(&str) -> bool) -> Option<String> {
    let (saved, current) = (saved.trim(), current.trim());
    if saved.is_empty() {
        return None;
    }
    let ours = ["", "auto", "en-US"].iter().chain(BUILTIN.iter()).any(|v| current.eq_ignore_ascii_case(v))
        || available.iter().any(|a| current.eq_ignore_ascii_case(a));
    if !ours {
        return None;
    }
    // "zh" ships with the release; any other table has to be in the run folder
    let known = saved == "zh" || (saved.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') && has_table(saved));
    let want = if known { saved } else { "en" };
    (want != current).then(|| want.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn langs(l: &[&str]) -> Vec<String> {
        l.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn simplified_chinese_locales_get_chinese() {
        let a = langs(&["en", "zh"]);
        for tag in ["zh-CN", "zh-SG", "zh_CN.UTF-8", "zh", "zh-Hans-HK"] {
            assert_eq!(match_language(tag, &a), "zh", "{tag}");
        }
    }

    #[test]
    fn traditional_chinese_locales_do_not_get_the_simplified_table() {
        let a = langs(&["en", "zh"]);
        for tag in ["zh-TW", "zh_HK.UTF-8", "zh-MO", "zh-Hant-CN"] {
            assert_eq!(match_language(tag, &a), "en", "{tag}");
        }
        assert_eq!(match_language("zh_TW.UTF-8", &langs(&["en", "zh", "zh-TW"])), "zh-TW");
        assert_eq!(match_language("zh-HK", &langs(&["en", "zh", "zh-Hant"])), "zh-Hant");
    }

    #[test]
    fn packs_match_the_full_name_before_the_language() {
        let a = langs(&["en", "zh", "pt", "pt-BR", "de"]);
        assert_eq!(match_language("pt_BR.UTF-8", &a), "pt-BR");
        assert_eq!(match_language("pt-PT", &a), "pt");
        assert_eq!(match_language("de_DE@euro", &a), "de");
        assert_eq!(match_language("fr-FR", &a), "en");
        assert_eq!(match_language("C.UTF-8", &a), "en");
        assert_eq!(match_language("", &a), "en");
    }

    #[test]
    fn the_menu_follows_a_picked_language() {
        let a = langs(&["en", "zh"]);
        let none = |_: &str| false;
        for current in ["", "auto", "en"] {
            assert_eq!(f1_value("zh", current, &a, none).as_deref(), Some("zh"), "{current}");
        }
        // English is written out: "auto" would follow a Chinese system
        assert_eq!(f1_value("en", "auto", &a, none).as_deref(), Some("en"));
        assert_eq!(f1_value("en", "zh", &a, none).as_deref(), Some("en"));
        assert_eq!(f1_value("zh", "zh", &a, none), None);
    }

    #[test]
    fn nothing_picked_and_hand_written_values_are_left_alone() {
        let a = langs(&["en", "zh"]);
        for current in ["", "auto", "zh", "de"] {
            assert_eq!(f1_value("", current, &a, |_| true), None, "{current}");
        }
        assert_eq!(f1_value("en", "de", &a, |_| true), None);
        assert_eq!(f1_value("zh", "my-table", &a, |_| true), None);
    }

    #[test]
    fn a_pack_without_a_menu_table_is_english_there() {
        let a = langs(&["en", "zh", "de", "fr"]);
        assert_eq!(f1_value("de", "zh", &a, |c| c == "de").as_deref(), Some("de"));
        assert_eq!(f1_value("fr", "de", &a, |c| c == "de").as_deref(), Some("en"));
        assert_eq!(f1_value("../x", "auto", &a, |_| true).as_deref(), Some("en"));
    }
}
