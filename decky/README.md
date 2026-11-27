# fefw recomp, Decky plugin

Updates and rebuilds the game from the Steam Deck's Quick Access Menu (Game Mode), through
[Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader). Early: tried on CachyOS Handheld, not on SteamOS.

The panel: choose the game's folder (the one with `fefw-launcher.AppImage`), see its version, **Play** (starts the game's Steam
shortcut, which the launcher's **Add to Steam** makes), change the graphics preset, frame rate, resolution and upscaler, **Update** (then builds the game when
the new release needs it), **Build the game**, **Cancel**. On the game's own Steam library page (matched by `steam_shortcuts`), the
plugin also replaces Steam's default tabs with **Setup**, **Settings** (all choice-based settings from `--settings`, grouped like the
launcher), and **About**, and replaces Steam's **Play** button with **Set up** when the game needs building or a live progress state
while a job is running.
First-time setup stays in Desktop Mode, in the launcher: the plugin uses
the settings it saved there.

A job, an update, or anything in the way is shown at the top of the panel; **Check for updates** stays at the bottom.

All the work is the launcher's (`--status`, `--check-update`, `--update`, `--rebuild`, see `launcher/README.md`). The installed
release has to be one with those commands.

## Build

Node 22.18 or newer (the panel's tests run TypeScript directly) and pnpm 9:

```
pnpm install
pnpm run check
pnpm test            # the backend against a stand-in launcher, the auto-sleep keeper against a stand-in Steam
pnpm run build
python3 package.py   # out/fefw-recomp-decky.zip
```

CI runs these on every pull request and keeps the zip with the run. A release (`.github/workflows/linux-release.yml`) publishes
`fefw-recomp-<version>-decky.zip` beside the packages, with the release's version in it (`package.py --version`).

## Install for testing

Unpack the zip into Decky's plugin folder (`~/homebrew/plugins/`, which may need `sudo`) and restart the loader
(`sudo systemctl restart plugin_loader`). The plugin runs as your user, not as root. Its log is under `~/homebrew/logs/`.

## How it works

- `py_modules/fefw_launcher.py` has the logic and needs nothing from Decky; `main.py` only passes the panel's calls on; `src/` is
  the panel.
- The loader runs without a display. `--rebuild` gets the one of your running Steam client, so that the shader preparation can run;
  without one the launcher skips that step. If the step says nothing for ten minutes it is stopped, and the game is ready without it.
- `--status` and `--check-update` are run with no usable display, so a launcher too old for them cannot open its window. After an
  update the new launcher is asked the same way before it is told to build, and one without the commands is left alone.
- **Play** asks Steam to run the shortcut (`src/play.ts`, an undocumented part of the Steam client), so the game starts as from the
  library. It shows when the game is built and nothing is at work on it. Which shortcuts there are comes from the launcher. While
  the panel is open, an install seen busy is looked at again every few seconds, so Play comes back when the game ends.
- **The plugin updates itself** to the one in the installed release (`decky/fefw-recomp-decky.zip` in the Linux package), when that
  is newer. After an update of the game made from the panel it asks Decky to install it, and Decky asks the player; the panel also
  has a button for it, for an update made in the launcher or a question answered no. It uses Decky's own installer
  (`utilities/install_plugin`, not part of the plugin API), which keeps the plugin's settings. Decky runs as root and removes the old
  plugin before it checks the new one's hash, so the plugin first checks the ZIP (this plugin's name, no `root` flag, nothing
  outside its folder) and hands over a copy of exactly those bytes.
- **Settings** come from the launcher (`--settings`, from `recomplauncher.json`); the panel shows four of them (`src/settings.ts`).
  They apply when the game next starts. Picking FSR 3 or XeSS without its files shows the vendor's terms, as the launcher does, and
  only on acceptance downloads them and then selects it. DLSS is listed only when its files are there, on an NVIDIA GPU.
- The panel shows the time a job has run: some steps report nothing for minutes.
- While a job runs, Steam's auto-sleep is turned off and put back afterwards, unless **Prevent auto-sleep** under the progress
  bar is off (remembered). The power button works as usual. It uses an undocumented part of the Steam client (`src/sleep.ts`); the
  old values are saved first, and put back at the next load if the plugin stopped in between. Not a `systemd-inhibit` block: Steam
  ignores one for its timers, and a sleep the block refuses leaves Steam stuck on a black screen until a restart.
- Closing the panel does not stop a job. Unloading the plugin cancels it.
