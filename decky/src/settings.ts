// SPDX-License-Identifier: GPL-3.0-or-later
// How the launcher's settings (--settings) read in a drop-down, both for the narrow Quick Access panel (the four changed most) and
// the full-width game page tab (all of them). This file imports nothing, so the tests run it as it is.

export interface Choice {
  label: string;
  available: boolean;
  download: string | null; // files to fetch first
}

export interface SettingItem {
  id: string;
  label: string;
  help?: string;
  group?: string; // the launcher's Settings heading; not told by a launcher from before it
  current: string | null;
  choices: Choice[];
}

export interface SettingsList {
  settings: SettingItem[];
  consent: Record<string, string>;
}

export interface FormattedOption {
  choice: string;
  label: string;
  download: string | null;
}

export interface FormattedSetting {
  id: string;
  label: string;
  help: string;
  current: string | null;
  options: FormattedOption[];
}

export interface SettingGroup {
  title: string;
  settings: FormattedSetting[];
}

export const SHOWN = ["preset", "fps_target", "output", "upscaler"];

// The manifest says what an upscaler needs ("(needs AMD's files)"); here the choice itself says whether they are still to come.
const plain = (label: string) => label.replace(/\s*\(needs [^)]*\)$/, "");

const formatSetting = (s: SettingItem): FormattedSetting => ({
  id: s.id,
  label: s.label,
  help: s.help ?? "",
  current: s.current,
  // what cannot be picked here stays listed only while it is what is picked
  options: s.choices
    .filter((c) => c.available || c.label === s.current)
    .map((c) => ({ choice: c.label, label: c.download ? `${plain(c.label)} (download)` : plain(c.label), download: c.download })),
});

export const shownSettings = (list: SettingsList): FormattedSetting[] =>
  SHOWN.flatMap((id) => list.settings.filter((s) => s.id === id)).map(formatSetting);

// A setting left with one choice here (frame generation on a Steam Deck, where only Off is supported) has nothing to pick.
export const allSettings = (list: SettingsList): FormattedSetting[] =>
  list.settings.map(formatSetting).filter((s) => s.options.length > 1);

// Under the launcher's Settings headings, in the manifest's order; a launcher from before `group` puts them all under Settings.
export const groupedSettings = (list: SettingsList): SettingGroup[] => {
  const groups = new Map<string, FormattedSetting[]>();
  for (const item of list.settings) {
    const s = formatSetting(item);
    if (s.options.length < 2) continue;
    const title = item.group || "Settings";
    groups.set(title, [...(groups.get(title) ?? []), s]);
  }
  return [...groups].map(([title, settings]) => ({ title, settings }));
};
