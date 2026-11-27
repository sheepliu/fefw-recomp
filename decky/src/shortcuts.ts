// SPDX-License-Identifier: GPL-3.0-or-later
// Matching Steam's game page against the launcher's non-Steam shortcut ids, and building the replacement tab list.
// This file imports nothing, so the tests run it as it is.

// Steam's id for the game page's "Activity" tab, which non-Steam shortcuts open on by default.
export const ACTIVITY_TAB_ID = "WhatsNew";

// Steam surfaces non-Steam shortcut appids (high bit set) as signed 32-bit integers in some places; coerce to unsigned.
export const toUnsignedAppId = (appid: number | null | undefined): number => (appid ?? 0) >>> 0;

export const isShortcutAppId = (
  appid: number | null | undefined,
  shortcuts: readonly number[] | null | undefined,
): boolean => {
  const id = toUnsignedAppId(appid);
  if (id === 0 || !shortcuts?.length) return false;
  return shortcuts.some((s) => (s >>> 0) === id);
};

// Replaces Steam's default tabs on the fefw-recomp shortcut page. If Steam's current activeTab is not among the new tabs,
// falls back to the first replacement tab so content is never blank.
export const replaceTabStrip = <T extends { id: string }>(
  replacementTabs: readonly T[],
  activeTab: string | undefined,
): { tabs: T[]; activeTab: string | undefined } => {
  const tabs = [...replacementTabs];
  if (!tabs.length) return { tabs, activeTab };
  const valid = activeTab !== undefined && tabs.some((t) => t.id === activeTab);
  return { tabs, activeTab: valid ? activeTab : tabs[0].id };
};
