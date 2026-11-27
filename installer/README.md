# Installer for SteamOS and similar

`fefw-recomp-install.desktop` is the one file a player downloads, in Desktop Mode, and opens. It downloads the latest release's
`fefw-recomp-install.py` (`fefw_install.py` here) and runs it, asking in zenity's dialogs (as Decky's installer does). A browser may
save the file as `...desktop.download`: remove the `.download`.

Over SSH, or with `--text`, `python3 fefw-recomp-install.py` asks in the terminal instead. On a desktop without zenity it opens a
terminal window for that.

Every step that changes something asks first:

1. Where the game goes (`~/Games/fefw-recomp`), or where it is installed already. A new install downloads the release's Linux
   package, checks it against GitHub's SHA-256 of it and unpacks it. An install that is there keeps its files: updates come from the
   plugin or the launcher.
2. Decky Loader, when it is not there: its own installer, with its own questions.
3. The Decky plugin from the release, and the game's folder written into the plugin's settings.
4. The Steam shortcut, with its library art (the launcher's `--add-to-steam`; Steam closes and starts again).
5. The launcher, to choose the game files and build the game.

Running it again finishes or repairs an earlier run, including a restart of Decky that did not happen. The download file runs one
installer at a time.

## Administrator rights

Only the plugin step needs them (Decky's plugin folder belongs to root), for one command: copy the plugin there and restart the
loader. With an account that has a password, `sudo` asks for it, or a dialog does and hands it to `sudo` on its input; it is not
saved, and never part of a command line. With none, the player chooses:

- a temporary password (random, shown while it exists), removed again as soon as the command ends, also after an error, Ctrl+C or a
  closed window. A power cut or a forced kill in those seconds leaves it set: `sudo passwd -d <user>` removes it. None is set while
  wrong passwords have the account locked, since it could not be removed then;
- setting their own with `passwd`, which stays;
- skipping the plugin.

## Testing

    python3 -m unittest discover -s tests

The tests use stand-ins for `sudo`, `passwd` and the launcher. To try packages that are not published, put a
`*-linux-x64.tar.gz` and a `*-decky.zip` (a run of the Release workflow with publishing off makes both) in a folder:

    python3 fefw_install.py --packages <folder>
