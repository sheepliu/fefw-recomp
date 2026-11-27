// SPDX-License-Identifier: GPL-3.0-or-later
// The auto-sleep keeper against a stand-in for Steam: node --test tests/*.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { decodeTimers, encodeTimers, SleepKeeper, type Timers } from "../src/sleep.ts";

const bytes = (base64: string) => Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));

const world = (initial: Partial<Timers> | null) => {
  const events: string[] = [];
  let held: Timers | null = null;
  let current: Partial<Timers> = { ...initial };
  let callback: ((data: Uint8Array) => void) | null = null;
  let refuse = false;
  const steam = {
    SetSetting(base64: string) {
      if (refuse) throw new Error("refused");
      current = decodeTimers(bytes(base64));
      events.push(`write ${current.battery}/${current.ac}`);
      callback?.(bytes(base64));
    },
    RegisterForSettingsArrayChanges(cb: (data: Uint8Array) => void) {
      callback = cb;
      if (initial) cb(bytes(encodeTimers({ battery: 0, ac: 0, ...initial })));
      return { unregister: () => void (callback = null) };
    },
  };
  const records = {
    hold: async (battery: number, ac: number) => {
      events.push(`hold ${battery}/${ac}`);
      held ??= { battery, ac };
    },
    release: async () => {
      events.push("release");
      held = null;
    },
  };
  const keeper = new SleepKeeper(steam, records, () => {});
  return {
    keeper,
    events,
    tick: (wanted: boolean) => keeper.tick(wanted, held),
    held: () => held,
    current: () => current,
    refuse: (on: boolean) => void (refuse = on),
  };
};

test("timers survive the trip, and other settings are skipped", () => {
  const timers = { battery: 900, ac: 3600 };
  assert.deepEqual(decodeTimers(bytes(encodeTimers(timers))), timers);
  assert.equal(encodeTimers({ battery: 0, ac: 0 }), btoa("\x98\xdc\x0b\x00\xa0\xdc\x0b\x00"));
  // field 1 (a string), field 2 (fixed 32), field 3 (fixed 64), then the battery timer alone
  const mixed = Uint8Array.from([0x0a, 2, 65, 66, 0x15, 1, 2, 3, 4, 0x19, 1, 2, 3, 4, 5, 6, 7, 8, 0x98, 0xdc, 0x0b, 0xac, 0x02]);
  assert.deepEqual(decodeTimers(mixed), { battery: 300 });
  assert.deepEqual(decodeTimers(Uint8Array.from([0x98])), {});
});

test("the timers are recorded, turned off for the job and put back", async () => {
  const w = world({ battery: 300, ac: 900 });
  await w.tick(true);
  await w.tick(true);
  assert.deepEqual(w.events, ["hold 300/900", "write 0/0"]);
  assert.equal(w.keeper.status, "held");
  await w.tick(false);
  await w.tick(false);
  assert.deepEqual(w.events.slice(2), ["write 300/900", "release"]);
  assert.deepEqual([w.held(), w.current(), w.keeper.status], [null, { battery: 300, ac: 900 }, "off"]);
});

test("timers already off are left alone", async () => {
  const w = world({ battery: 0, ac: 0 });
  await w.tick(true);
  await w.tick(false);
  assert.deepEqual([w.events, w.keeper.status], [[], "off"]);
  await w.tick(true);
  assert.equal(w.keeper.status, "already");
});

test("a record left by an earlier run is put back, and held to during a job", async () => {
  const events: string[] = [];
  const steam = {
    SetSetting: (base64: string) => void events.push(JSON.stringify(decodeTimers(bytes(base64)))),
    RegisterForSettingsArrayChanges: () => ({ unregister() {} }),
  };
  const records = { hold: async () => void events.push("hold"), release: async () => void events.push("release") };
  const keeper = new SleepKeeper(steam, records, () => {});
  const old = { battery: 300, ac: 0 };
  await keeper.tick(true, old); // reloaded during the job
  await keeper.tick(true, old);
  await keeper.tick(false, old);
  assert.deepEqual(events, ['{"battery":0,"ac":0}', '{"battery":300,"ac":0}', "release"]);
});

test("nothing is written when Steam does not say what the timers are", async () => {
  const w = world(null);
  await w.tick(true);
  assert.equal(w.keeper.status, "waiting");
  for (let i = 0; i < 10; i++) await w.tick(true);
  assert.deepEqual([w.events, w.keeper.status], [[], "unavailable"]);
});

test("a failed write keeps the record and is tried again", async () => {
  const w = world({ battery: 300, ac: 900 });
  await w.tick(true);
  w.refuse(true);
  await w.tick(false);
  assert.deepEqual([w.keeper.status, w.held()], ["unavailable", { battery: 300, ac: 900 }]);
  w.refuse(false);
  await w.tick(false);
  assert.deepEqual([w.held(), w.current()], [null, { battery: 300, ac: 900 }]);
});

test("without Steam's settings it says so", async () => {
  const keeper = new SleepKeeper(undefined, { hold: async () => {}, release: async () => {} }, () => {});
  await keeper.tick(true, null);
  assert.equal(keeper.status, "unavailable");
});
