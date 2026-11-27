// SPDX-License-Identifier: GPL-3.0-or-later
// Keeps the device from sleeping by itself during a job: Steam's own auto-sleep timers are set to "never" and put back afterwards.
// The power button is not touched. A block on sleep (logind) is no use here: Steam's timers do not go through it, and a sleep it
// refuses leaves Steam's interface stuck behind a black screen until a restart.
//
// The timers are fields 24003 (on battery) and 24004 (plugged in) of Steam's CMsgClientSettings, in seconds, 0 for never, read and
// written as protobuf through SteamClient.Settings. Undocumented: found in https://github.com/JaxonWright/decky-dont-dimmadeck.
// This file imports nothing, so the tests run it as it is.

export interface Timers {
  battery: number;
  ac: number;
}

const FIELD_BATTERY = 24003;
const FIELD_AC = 24004;

// Numbers by arithmetic: a bit shift would cut them at 32 bits.
const writeVarint = (value: number, out: number[]) => {
  while (value >= 128) {
    out.push((value % 128) + 128);
    value = Math.floor(value / 128);
  }
  out.push(value);
};

export const encodeTimers = (timers: Timers): string => {
  const out: number[] = [];
  for (const [field, value] of [[FIELD_BATTERY, timers.battery], [FIELD_AC, timers.ac]]) {
    writeVarint(field * 8, out); // wire type 0: a varint
    writeVarint(value, out);
  }
  return btoa(String.fromCharCode(...out));
};

// The timers a settings message holds: Steam sends parts of its settings, so either may be absent. A message that cannot be walked
// gives what was read up to there.
export const decodeTimers = (bytes: Uint8Array): Partial<Timers> => {
  const found: Partial<Timers> = {};
  let at = 0;
  const varint = (): number | null => {
    let value = 0;
    for (let scale = 1; at < bytes.length; scale *= 128) {
      const byte = bytes[at++];
      value += (byte % 128) * scale;
      if (byte < 128) return value;
    }
    return null;
  };
  while (at < bytes.length) {
    const key = varint();
    if (key === null) break;
    const field = Math.floor(key / 8);
    const wire = key % 8;
    if (wire === 0) {
      const value = varint();
      if (value === null) break;
      if (field === FIELD_BATTERY) found.battery = value;
      if (field === FIELD_AC) found.ac = value;
    } else if (wire === 1) at += 8;
    else if (wire === 5) at += 4;
    else if (wire === 2) {
      const length = varint();
      if (length === null) break;
      at += length;
    } else break;
  }
  return found;
};

// What SteamClient.Settings has to offer.
export interface SteamSettings {
  SetSetting(base64: string): unknown;
  RegisterForSettingsArrayChanges(callback: (data: ArrayBuffer | Uint8Array) => void): { unregister(): void };
}

// The timers as they were, kept by the backend so that they can be put back after a reload or a restart.
export interface Records {
  hold(battery: number, ac: number): Promise<unknown>;
  release(): Promise<unknown>;
}

// off: nothing wanted. held: set to never. already: they were never to begin with. waiting: Steam has not said what they are.
// unavailable: they cannot be read or changed.
export type SleepStatus = "off" | "held" | "already" | "waiting" | "unavailable";

const PATIENCE = 5; // ticks to wait for Steam's settings before giving up

export class SleepKeeper {
  status: SleepStatus = "off";
  private seen: Partial<Timers> = {};
  private waited = 0;
  private busy = false;
  private written = false; // "never" was written for the record that is held
  private subscription: { unregister(): void } | null = null;
  private steam: SteamSettings | undefined;
  private records: Records;
  private log: (text: string) => void;

  // (no parameter properties: the tests run this file with Node's type stripping, which leaves them out)
  constructor(steam: SteamSettings | undefined, records: Records, log: (text: string) => void) {
    this.steam = steam;
    this.records = records;
    this.log = log;
    try {
      this.subscription =
        steam?.RegisterForSettingsArrayChanges((data) => {
          const timers = decodeTimers(data instanceof Uint8Array ? data : new Uint8Array(data));
          if (timers.battery !== undefined || timers.ac !== undefined) {
            Object.assign(this.seen, timers);
            this.log(`Steam's auto-sleep timers: ${JSON.stringify(this.seen)}`);
          }
        }) ?? null;
    } catch (e) {
      this.log(`cannot read Steam's settings: ${e}`);
    }
  }

  // Brings the timers in line. wanted: a job runs and sleep is to be prevented. held: the record the backend keeps, if any.
  // Call it regularly: what fails is tried again.
  async tick(wanted: boolean, held: Timers | null): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      if (!wanted) {
        if (held) await this.restore(held);
        this.status = "off";
        this.waited = 0;
      } else if (held) {
        if (!this.written) await this.write({ battery: 0, ac: 0 });
        this.written = true;
        this.status = "held";
      } else await this.take();
    } catch (e) {
      if (this.status !== "unavailable") this.log(`cannot change Steam's auto-sleep timers: ${e}`);
      this.status = "unavailable";
    } finally {
      this.busy = false;
    }
  }

  private async take() {
    const { battery, ac } = this.seen;
    if (battery === undefined || ac === undefined) {
      // never guessed: what is written back has to be what was there
      this.status = this.steam && ++this.waited < PATIENCE ? "waiting" : "unavailable";
    } else if (battery === 0 && ac === 0) {
      this.status = "already";
    } else {
      await this.records.hold(battery, ac); // kept first: it is the only way back after a crash
      await this.write({ battery: 0, ac: 0 });
      this.written = true;
      this.status = "held";
      this.log(`auto-sleep off for the job (was ${battery} s on battery, ${ac} s plugged in)`);
    }
  }

  private async restore(held: Timers) {
    await this.write(held);
    await this.records.release();
    this.written = false;
    this.seen = { ...held };
    this.log(`auto-sleep put back: ${held.battery} s on battery, ${held.ac} s plugged in`);
  }

  private async write(timers: Timers) {
    if (!this.steam) throw new Error("Steam's settings are not there");
    await this.steam.SetSetting(encodeTimers(timers));
  }

  close() {
    this.subscription?.unregister();
  }
}
