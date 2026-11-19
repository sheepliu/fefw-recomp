fefw-recomp @VERSION@ (Windows, 64-bit)

A static recompilation of Fire Emblem: Fortune's Weave for PC. This package contains no game code or data: you need your own
decrypted dump of the game (update 131072), as a folder with exefs/ and romfs/ or as a .nup package.

1. Run fefw-launcher.exe and pick your dump (Setup page).
2. Press Set up. On this PC it checks your dump, recompiles the game code into fefw.exe and translates the shaders. Nothing else
   needs to be installed. The first run takes a while; you can pause and continue later.
3. Press Play.

Updating: extract the new release over this folder and press Set up again if the launcher asks. Your saves are kept (save/).

Mods: use the launcher's Mods page to import IPS/IPS32/pchtxt patches, patch-only ExeFS folders, or supported runtime packages.
The mod state is stored in mods-state.json beside mods/. Press Set up again after changing enabled IPS patches: the build
reads your original dump only, translates a separate patched copy, and keeps previous-fefw.exe / previous-main in build/
for rollback. Patch parsing/writing follows upstream; main text/rodata/data are supported, but other modules, segment gaps
and protected hook conflicts remain unsupported. No manual patch filename changes are required in the Mods page.
Supported runtime packages appear in the
in-game F1 Mods tab after a restart; packages are installed separately and are not included in this release.

Licences: LICENSE.txt, LEGAL.txt and licenses/.
