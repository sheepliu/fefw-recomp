// SPDX-License-Identifier: GPL-3.0-or-later
//! The settings recomplauncher.json describes (RecompLauncher's manifest, in the package): each has choices, and each choice the
//! fefw.ini keys it writes. The Decky plugin reads and picks them through headless.rs, so a new choice needs no new plugin.
//!
//! Two rules of the game come on top, as in the Settings page: 120 fps wins when both frame rate switches are on, and a fixed
//! render size ([fefw] render) would override a picked render scale or preset, so picking one clears it.

use crate::ini::Ini;
use crate::settings::Edit;
use serde_json::Value;
use std::collections::BTreeMap;

pub struct Setting {
    pub id: String,
    pub label: String,
    pub help: String,
    /// the launcher's Settings heading it comes under ("Picture", "Frame rate", ...)
    pub group: String,
    pub choices: Vec<Choice>,
    /// "section/key" -> the value in force when fefw.ini does not name it
    defaults: BTreeMap<String, String>,
}

pub struct Choice {
    pub label: String,
    /// "section/key" -> value
    pub set: BTreeMap<String, String>,
}

fn pairs(v: &Value) -> BTreeMap<String, String> {
    v.as_object()
        .map(|o| o.iter().filter_map(|(k, v)| Some((k.clone(), v.as_str()?.to_string()))).filter(|(k, _)| k.contains('/')).collect())
        .unwrap_or_default()
}

/// The manifest's settings that are a pick from a list (sliders are left to the launcher).
pub fn parse(text: &str) -> Result<Vec<Setting>, String> {
    let v: Value = serde_json::from_str(text).map_err(|e| format!("recomplauncher.json cannot be read: {e}"))?;
    let name = |v: &Value, key: &str| v[key].as_str().unwrap_or_default().to_string();
    Ok(v["settings"]
        .as_array()
        .ok_or("recomplauncher.json has no settings.")?
        .iter()
        .filter_map(|s| {
            let choices: Vec<Choice> = s["choices"]
                .as_array()?
                .iter()
                .map(|c| Choice { label: name(c, "label"), set: pairs(&c["set"]) })
                .filter(|c| !c.label.is_empty() && !c.set.is_empty())
                .collect();
            (!choices.is_empty()).then(|| Setting {
                id: name(s, "id"),
                label: name(s, "label"),
                help: name(s, "help"),
                group: name(s, "group"),
                choices,
                defaults: pairs(&s["defaults"]),
            })
        })
        .collect())
}

/// A value as the runtime reads it: switches and numbers by meaning, text without case.
fn meaning(v: &str) -> String {
    let v = v.trim().to_ascii_lowercase();
    match v.as_str() {
        "true" | "yes" | "on" => "1".into(),
        "false" | "no" | "off" => "0".into(),
        _ => v.parse::<f64>().map_or(v.clone(), |n| n.to_string()),
    }
}

fn split(key: &str) -> (&str, &str) {
    key.split_once('/').unwrap_or(("", key))
}

fn fixed_render(s: &Setting, ini: &Ini) -> bool {
    s.choices.iter().any(|c| c.set.contains_key("fefw/render_ratio")) && ini.get("fefw", "render").is_some_and(|v| !v.trim().is_empty())
}

/// The choice fefw.ini holds now, None when it holds something else.
pub fn current<'a>(s: &'a Setting, ini: &Ini) -> Option<&'a Choice> {
    if fixed_render(s, ini) {
        return None;
    }
    let held = |key: &str| {
        let (section, name) = split(key);
        meaning(&ini.get(section, name).or_else(|| s.defaults.get(key).cloned()).unwrap_or_default())
    };
    s.choices.iter().find(|c| {
        c.set.iter().all(|(key, value)| {
            let now = if key == "fefw/fps60" && c.set.contains_key("fefw/fps120") && held("fefw/fps120") == "1" { "0".into() } else { held(key) };
            now == meaning(value)
        })
    })
}

/// The fefw.ini edits that pick `label`.
pub fn edits(s: &Setting, label: &str, ini: &Ini) -> Result<Vec<Edit>, String> {
    let choice = s.choices.iter().find(|c| c.label == label).ok_or_else(|| format!("\"{}\" has no choice \"{label}\".", s.label))?;
    let edit = |key: &str, value: &str| {
        let (section, name) = split(key);
        Edit { file: "game".into(), section: section.into(), key: name.into(), value: value.into() }
    };
    let mut out: Vec<Edit> = choice.set.iter().map(|(k, v)| edit(k, v)).collect();
    if fixed_render(s, ini) {
        out.push(edit("fefw/render", ""));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The manifest the packages ship.
    fn shipped() -> Vec<Setting> {
        parse(include_str!("../../../recomplauncher.json")).unwrap()
    }

    fn setting<'a>(all: &'a [Setting], id: &str) -> &'a Setting {
        all.iter().find(|s| s.id == id).unwrap()
    }

    fn pick(s: &Setting, label: &str, ini: &mut Ini) {
        for e in edits(s, label, ini).unwrap() {
            ini.set(&e.section, &e.key, &e.value);
        }
    }

    fn held<'a>(s: &'a Setting, ini: &Ini) -> Option<&'a str> {
        current(s, ini).map(|c| c.label.as_str())
    }

    #[test]
    fn an_empty_ini_holds_the_defaults() {
        let all = shipped();
        let ini = Ini::parse("");
        for (id, label) in [
            ("preset", "Medium (console)"),
            ("output", "Console (docked 1440p)"),
            ("fps_target", "30 fps (console)"),
            ("upscaler", "Built-in temporal (TAAU)"),
        ] {
            assert_eq!(held(setting(&all, id), &ini), Some(label), "{id}");
        }
        assert!(all.iter().all(|s| s.id != "npc_distance"), "a slider is not a pick from a list");
    }

    #[test]
    fn every_shipped_choice_is_read_back_once_picked() {
        for s in shipped() {
            let mut ini = Ini::parse("");
            for c in s.choices.iter().chain(s.choices.iter().rev()) {
                pick(&s, &c.label, &mut ini);
                assert_eq!(held(&s, &ini), Some(c.label.as_str()), "{}", s.id);
            }
        }
    }

    #[test]
    fn values_count_by_meaning_and_other_values_are_custom() {
        let all = shipped();
        let preset = setting(&all, "preset");
        assert_eq!(held(preset, &Ini::parse("[fefw]\nrender_ratio = 0.50\nshadow_size=1024\nquality = Handheld\nreflections=reduced\nambient_occlusion = false\ncrowd_detail=reduced\nscenery_detail=reduced\n")), Some("Low"));
        assert_eq!(held(preset, &Ini::parse("[fefw]\nshadow_size = 2048\n")), None);
        // the F1 menu may leave fps60 on under 120
        assert_eq!(held(setting(&all, "fps_target"), &Ini::parse("[fefw]\nfps60 = 1\nfps120 = 1\n")), Some("120 fps"));
        assert!(edits(preset, "No such", &Ini::parse("")).is_err());
    }

    #[test]
    fn a_fixed_render_size_is_cleared_by_a_preset() {
        let all = shipped();
        let preset = setting(&all, "preset");
        let mut ini = Ini::parse("[fefw]\nrender = 1280x720\noutput = 1080p\n");
        assert_eq!(held(preset, &ini), None);
        assert_eq!(held(setting(&all, "output"), &ini), Some("1080p"));
        pick(preset, "High", &mut ini);
        assert_eq!((held(preset, &ini), ini.get("fefw", "render").as_deref()), (Some("High"), Some("")));
        assert_eq!(ini.get("fefw", "output").as_deref(), Some("1080p"));
    }
}
