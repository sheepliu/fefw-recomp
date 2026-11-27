// SPDX-License-Identifier: GPL-3.0-or-later
// Shared backend calls, helpers, and reactive state for both the Quick Access panel and the Steam game-page tabs.
import { callable, FileSelectionType, openFilePicker } from "@decky/api";
import { useSyncExternalStore } from "react";
import { needsRebuild, playButtonKey, resolvePlayButton, type PlayButtonState } from "./play-button";
import { askToUpdate, type DeckyRoutes, type PluginOffer } from "./self_update";
import type { SettingsList } from "./settings";
import { isShortcutAppId } from "./shortcuts";
import type { SleepStatus, Timers } from "./sleep";

export const NOT_ASKED = "Decky could not be asked: to update the plugin, run the installer in Desktop Mode.";

export const decky = () => (window as unknown as { DeckyBackend?: DeckyRoutes }).DeckyBackend;

export interface Result {
  state: "done" | "error" | "cancelled";
  message: string;
  plugin_update?: PluginOffer; // after an update of the game that brought a newer plugin
}

export interface UpdateRelease {
  version: string;
  notes?: string;
  page?: string;
  size?: number;
}

export interface InstallStatus {
  version: string;
  dump_dir?: string;
  busy: "game" | "job" | null;
  setup_complete: boolean;
  rebuild: boolean | null;
  rebuild_reason: string;
  problem: string | null;
  steam_shortcuts?: number[]; // not told by a launcher from before it
}

export interface JobState {
  step: "update" | "rebuild" | "download";
  message: string;
  fraction: number | null;
  counting?: boolean; // the part that runs says how far it is; not told by a backend from before it
  cancelling: boolean;
  elapsed: number;
}

export interface State {
  install_dir: string | null;
  home: string;
  status: InstallStatus | null;
  update: { newer: boolean; latest: UpdateRelease | null } | null;
  settings: SettingsList | null;
  job: JobState | null;
  result: Result | null;
  error: string | null;
  keep_awake: boolean;
  held_sleep: Timers | null;
  plugin_update: PluginOffer | null; // the installed release's plugin, when it is newer than this one
}

export const getState = callable<[], State>("get_state");
export const watch = callable<[], State>("watch");
export const setInstall = callable<[folder: string], State>("set_install");
export const refresh = callable<[], State>("refresh");
export const reread = callable<[], State>("reread");
export const start = callable<[kind: "update" | "rebuild"], State>("start");
export const cancel = callable<[], State>("cancel");
export const setSetting = callable<[setting: string, choice: string], State>("set_setting");
export const download = callable<[kind: string, setting: string, choice: string], State>("download");
export const setKeepAwake = callable<[on: boolean], State>("set_keep_awake");
export const holdSleep = callable<[battery: number, ac: number], void>("hold_sleep");
export const releaseSleep = callable<[], void>("release_sleep");
export const note = callable<[text: string], void>("note");

export const SLEEP_TEXT: Record<SleepStatus, string> = {
  off: "Auto-sleep will be turned off until this has finished. The power button still works",
  held: "Auto-sleep is off until this has finished. The power button still works",
  already: "Auto-sleep is already off in Steam's settings",
  waiting: "Reading Steam's sleep settings",
  unavailable: "Could not change Steam's auto-sleep: keep the device awake",
};

// Shared derived UI helpers so QAM, game-page tabs, and Play button stay consistent.
export const busyText = (status: InstallStatus | null | undefined): string | null =>
  status?.busy === "game"
    ? "Close the game first."
    : status?.busy === "job"
      ? "The launcher is already working on this install."
      : null;

export { needsRebuild };

export const jobStepTitle = (step: JobState["step"]): string =>
  step === "update" ? "Updating" : step === "download" ? "Downloading" : "Building the game";

// seconds as 4:05, or 1:02:09 from an hour
export const clock = (seconds: number) => {
  const two = (n: number) => String(n).padStart(2, "0");
  const h = Math.floor(seconds / 3600);
  const m = Math.floor(seconds / 60) % 60;
  return h ? `${h}:${two(m)}:${two(seconds % 60)}` : `${m}:${two(seconds % 60)}`;
};

export interface StoreSnapshot {
  state: State | null;
  checking: boolean;
  saving: boolean;
  pluginFailed: boolean;
  tabsPatchActive: boolean;
}

let snapshot: StoreSnapshot = {
  state: null,
  checking: false,
  saving: false,
  pluginFailed: false,
  tabsPatchActive: false,
};

// The shortcut ids of the chosen install, kept while its status is unread (a failed read, a look still running) so that the
// game page keeps its tabs. Another folder starts empty: its ids are not the old game's. A read status is believed, even empty.
let shortcuts: { install: string | null; ids: number[] } = { install: null, ids: [] };

function rememberShortcuts(state: State) {
  if (state.install_dir !== shortcuts.install) shortcuts = { install: state.install_dir, ids: [] };
  if (state.status) shortcuts.ids = state.status.steam_shortcuts ?? [];
}

const listeners = new Set<() => void>();
let pollTimer: ReturnType<typeof setInterval> | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let inFlightCheck: Promise<void> | null = null;
let lastPageRereadAt = 0;
let lastPlayingWatchAt = 0;
let qamViews = 0;
let pageViews = 0;
let stopped = false;

// switching between the game page's tabs remounts them: read again at most this often
const PAGE_REREAD_COOLDOWN_MS = 10_000;
// how often the status of a running game is read again while only its game page is open
const PLAYING_WATCH_MS = 60_000;
const BOOT_RETRY_DELAYS_MS = [5_000, 15_000, 30_000];

function emit() {
  for (const listener of listeners) listener();
}

function updateSnapshot(patch: Partial<StoreSnapshot>) {
  if (patch.state) rememberShortcuts(patch.state);
  snapshot = { ...snapshot, ...patch };
  emit();
}

export function setStateSnapshot(next: State) {
  updateSnapshot({ state: next });
}

export function setTabsPatchActive(active: boolean) {
  if (snapshot.tabsPatchActive !== active) {
    updateSnapshot({ tabsPatchActive: active });
  }
}

// The full look the Quick Access panel and "Check for updates" ask for: status, settings and, over the network, a newer release.
// One at a time, so that `checking` stays on until the last one has answered.
export function runCheck(): Promise<void> {
  if (inFlightCheck) return inFlightCheck;
  updateSnapshot({ checking: true });
  inFlightCheck = (async () => {
    try {
      updateSnapshot({ state: await refresh() });
    } finally {
      inFlightCheck = null;
      updateSnapshot({ checking: false });
    }
  })();
  return inFlightCheck;
}

export async function chooseInstallFolder() {
  const current = snapshot.state;
  if (!current) return;
  let picked;
  try {
    picked = await openFilePicker(FileSelectionType.FOLDER, current.install_dir ?? current.home, false, true);
  } catch {
    return; // the picker was closed
  }
  const next = await setInstall(picked.realpath);
  updateSnapshot({ state: next });
  if (!next.error) void runCheck();
}

export async function applySetting(setting: string, choice: string) {
  updateSnapshot({ saving: true });
  try {
    updateSnapshot({ state: await setSetting(setting, choice) });
  } finally {
    updateSnapshot({ saving: false });
  }
}

export async function startDownload(kind: string, setting: string, choice: string) {
  updateSnapshot({ state: await download(kind, setting, choice) });
}

export async function startJob(kind: "update" | "rebuild") {
  updateSnapshot({ state: await start(kind) });
}

export async function cancelJob() {
  updateSnapshot({ state: await cancel() });
}

export async function toggleKeepAwake(on: boolean) {
  updateSnapshot({ state: await setKeepAwake(on) });
}

export async function requestPluginUpdate(offer: PluginOffer) {
  const asked = await askToUpdate(decky(), offer);
  updateSnapshot({ pluginFailed: !asked });
}

// Status and settings, read again on this device (no update check), for a look nobody asked for.
export async function rereadState() {
  updateSnapshot({ state: await reread() });
}

// At boot, a quiet local read (no network) so that the game-page patch knows the shortcut ids before the panel is opened.
// Tried again a few times when it fails, for a game on a drive or SD card that was not mounted yet when Decky loaded.
export async function bootstrapStore(attempt = 0) {
  stopped = false;
  try {
    const s = await getState();
    updateSnapshot({ state: s });
    if (s.install_dir && !s.status && !s.job) {
      const updated = await reread();
      updateSnapshot({ state: updated });
      if (!updated.status && !stopped && attempt < BOOT_RETRY_DELAYS_MS.length) {
        retryTimer = setTimeout(() => void bootstrapStore(attempt + 1), BOOT_RETRY_DELAYS_MS[attempt]);
      }
    }
  } catch {
    if (!stopped && attempt < BOOT_RETRY_DELAYS_MS.length) {
      retryTimer = setTimeout(() => void bootstrapStore(attempt + 1), BOOT_RETRY_DELAYS_MS[attempt]);
    }
  }
}

// The status last read says the game is running (status.busy is read again only while something is open).
export const isGameRunning = (): boolean => snapshot.state?.status?.busy === "game";

export function stopStore() {
  stopped = true;
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (retryTimer !== null) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): StoreSnapshot {
  return snapshot;
}

function syncPollingTimer() {
  const needPoll = qamViews > 0 || pageViews > 0;
  if (needPoll && pollTimer === null) {
    pollTimer = setInterval(() => {
      // While the game runs with only its library page behind it, watch() (which reads the status again of a busy install)
      // is asked rarely, not every 5 s: Steam's game-exit notice reads it at once, and this catches an exit that notice missed.
      if (qamViews === 0 && !snapshot.state?.job && isGameRunning()) {
        if (Date.now() - lastPlayingWatchAt < PLAYING_WATCH_MS) return;
        lastPlayingWatchAt = Date.now();
      }
      void watch().then((s) => updateSnapshot({ state: s }));
    }, 1000);
  } else if (!needPoll && pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

// Keeps the store refreshed and polling while a UI surface (QAM panel or game-page tab) is mounted.
export function retainPolling(source: "qam" | "page"): () => void {
  const wasIdle = qamViews === 0 && pageViews === 0;
  if (source === "qam") qamViews += 1;
  else pageViews += 1;
  syncPollingTimer();

  // the panel looks for updates too; the game page reads only what is on this device, so that what the launcher changed shows
  const look = (s: State | null) => {
    if (!s?.install_dir || s.job) return;
    if (source === "qam") {
      void runCheck();
    } else if (!inFlightCheck && Date.now() - lastPageRereadAt >= PAGE_REREAD_COOLDOWN_MS) {
      lastPageRereadAt = Date.now();
      void rereadState();
    }
  };
  if (wasIdle) {
    void getState().then((s) => {
      updateSnapshot({ state: s });
      look(s);
    });
  } else {
    look(snapshot.state);
  }

  return () => {
    if (source === "qam") qamViews = Math.max(0, qamViews - 1);
    else pageViews = Math.max(0, pageViews - 1);
    syncPollingTimer();
  };
}

export function useStore(): StoreSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

export function useIsFefwShortcut(appid: number): boolean {
  return useSyncExternalStore(
    subscribe,
    () => {
      return isShortcutAppId(appid, shortcuts.ids);
    },
    () => false,
  );
}

export function getPlayButtonState(): PlayButtonState {
  if (!snapshot.state) return { kind: "play" };
  return resolvePlayButton({
    status: snapshot.state.status,
    job: snapshot.state.job,
    checking: snapshot.checking,
  });
}

export function usePlayButtonKey(): string {
  return useSyncExternalStore(
    subscribe,
    () => playButtonKey(getPlayButtonState()),
    () => "play",
  );
}

export function hasUpdateAvailable(): boolean {
  return !!(snapshot.state?.update?.newer || snapshot.state?.plugin_update);
}

export function useHasUpdateAvailable(): boolean {
  return useSyncExternalStore(subscribe, hasUpdateAvailable, () => false);
}
