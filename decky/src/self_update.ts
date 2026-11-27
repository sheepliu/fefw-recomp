// SPDX-License-Identifier: GPL-3.0-or-later
// Updating this plugin to the one the installed release carries: Decky's own installer is asked to, which shows the player a
// question, and on a yes replaces the plugin and loads it again. The plugin's settings stay.
//
// Decky's `utilities/install_plugin` is what its store and "install from ZIP" use, not part of the plugin API (@decky/api). This
// file imports nothing, so the tests run it as it is.

export interface PluginOffer {
  version: string;
  artifact: string; // file://, a checked copy of the release's ZIP
  hash: string; // its SHA-256, which Decky checks again
}

export interface DeckyRoutes {
  callable<Args extends unknown[], Return>(route: string): (...args: Args) => Promise<Return>;
}

export const PLUGIN_NAME = "FEFW Recomp"; // plugin.json's, by which Decky finds the plugin to replace
const UPDATE = 2; // Decky's PluginInstallType

// False when Decky could not be asked.
export const askToUpdate = async (decky: DeckyRoutes | undefined, offer: PluginOffer): Promise<boolean> => {
  try {
    if (!decky) return false;
    const install = decky.callable<[string, string, string, string, number], void>("utilities/install_plugin");
    await install(offer.artifact, PLUGIN_NAME, offer.version, offer.hash, UPDATE);
    return true;
  } catch {
    return false;
  }
};
