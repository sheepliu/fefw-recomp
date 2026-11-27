// SPDX-License-Identifier: GPL-3.0-or-later
// Starting the game through Steam, which gives it the focus, the controller layout, the overlay and Proton as a start from the
// library does. The game is the non-Steam shortcut the launcher reports (--status, steam_shortcuts).
//
// SteamClient.Apps and appStore are undocumented parts of the Steam client. This file imports nothing, so the tests run it as it is.

export interface SteamApps {
  RunGame(gameId: string, options: string, listIndex: number, launchSource: number): unknown;
}

export interface AppStore {
  GetAppOverviewByAppID?(appid: number): { gameid?: string } | null | undefined;
}

// A non-Steam game's id as RunGame takes it: the shortcut's id above Steam's mark for shortcuts.
export const shortcutGameId = (appid: number): string => ((BigInt(appid >>> 0) << 32n) | 0x02000000n).toString();

// The launcher lists a shortcut of every account on the device. The signed-in account's is the one Steam knows; when it cannot say,
// the first.
export const pickShortcut = (ids: number[], store: AppStore | undefined): number | null =>
  ids.find((id) => !!store?.GetAppOverviewByAppID?.(id)) ?? ids[0] ?? null;

// False when Steam could not be asked.
export const startGame = (apps: SteamApps | undefined, store: AppStore | undefined, ids: number[]): boolean => {
  try {
    const appid = pickShortcut(ids, store);
    if (!apps || appid === null) return false;
    apps.RunGame(store?.GetAppOverviewByAppID?.(appid)?.gameid ?? shortcutGameId(appid), "", -1, 100);
    return true;
  } catch {
    return false;
  }
};
