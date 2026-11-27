// SPDX-License-Identifier: GPL-3.0-or-later
// Starting the game against a stand-in for Steam: node --test tests/*.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { pickShortcut, shortcutGameId, startGame } from "../src/play.ts";

const world = (known: Record<number, { gameid?: string }>) => {
  const runs: unknown[][] = [];
  return {
    runs,
    apps: { RunGame: (...args: unknown[]) => void runs.push(args) },
    store: { GetAppOverviewByAppID: (id: number) => known[id] ?? null },
  };
};

test("a shortcut's game id", () => {
  // 0x80000000 << 32 | 0x02000000, past what a plain number holds
  assert.equal(shortcutGameId(0x80000000), "9223372036888330240");
  assert.equal(shortcutGameId(2437868028), ((2437868028n << 32n) + 33554432n).toString());
  assert.equal(shortcutGameId(-1857099268), shortcutGameId(2437868028)); // the same id read as a signed number
});

test("the signed-in account's shortcut is started", () => {
  const w = world({ 22: {} });
  assert.equal(startGame(w.apps, w.store, [11, 22]), true);
  assert.deepEqual(w.runs, [[shortcutGameId(22), "", -1, 100]]);
});

test("Steam's own game id is used when it gives one", () => {
  const w = world({ 11: { gameid: "1234" } });
  startGame(w.apps, w.store, [11]);
  assert.equal(w.runs[0][0], "1234");
});

test("without Steam's list the first shortcut is taken", () => {
  assert.equal(pickShortcut([11, 22], undefined), 11);
  assert.equal(pickShortcut([], undefined), null);
});

test("it says when the game could not be started", () => {
  const w = world({});
  assert.equal(startGame(undefined, w.store, [11]), false);
  assert.equal(startGame(w.apps, w.store, []), false);
  const broken = { RunGame: () => { throw new Error("gone"); } };
  assert.equal(startGame(broken, w.store, [11]), false);
});
