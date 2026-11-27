// SPDX-License-Identifier: GPL-3.0-or-later
// Matching Steam shortcut ids and replacing the game-page tab strip: node --test tests/*.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { ACTIVITY_TAB_ID, isShortcutAppId, replaceTabStrip, toUnsignedAppId } from "../src/shortcuts.ts";

test("unsigned coercion handles signed int32 non-Steam shortcut ids", () => {
  assert.equal(toUnsignedAppId(-1857099268), 2437868028);
  assert.equal(toUnsignedAppId(2437868028), 2437868028);
  assert.equal(toUnsignedAppId(undefined), 0);
  assert.equal(toUnsignedAppId(null), 0);
});

test("shortcut matching compares unsigned ids and ignores empty or zero", () => {
  assert.equal(isShortcutAppId(-1857099268, [2437868028]), true);
  assert.equal(isShortcutAppId(2437868028, [11, 2437868028]), true);
  assert.equal(isShortcutAppId(42, [2437868028]), false);
  assert.equal(isShortcutAppId(0, [0]), false);
  assert.equal(isShortcutAppId(2437868028, []), false);
  assert.equal(isShortcutAppId(2437868028, undefined), false);
});

test("replaceTabStrip replaces all tabs and keeps valid activeTab or falls back to first", () => {
  const replacement = [
    { id: ACTIVITY_TAB_ID, title: "Setup" },
    { id: "FefwSettings", title: "Settings" },
    { id: "FefwAbout", title: "About" },
  ];
  const kept = replaceTabStrip(replacement, "FefwSettings");
  assert.deepEqual(kept.tabs.map((t) => t.title), ["Setup", "Settings", "About"]);
  assert.equal(kept.activeTab, "FefwSettings");

  const fallback = replaceTabStrip(replacement, "GameInfo");
  assert.equal(fallback.activeTab, ACTIVITY_TAB_ID);

  const initial = replaceTabStrip(replacement, undefined);
  assert.equal(initial.activeTab, ACTIVITY_TAB_ID);
});
