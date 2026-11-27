# DLC and mods

## DLC

DLC is used when you dump it from your own copy, like the game. Put each add-on in `dlc\<title id>\romfs` of your dump folder (next
to `exefs\` and `romfs\`, or next to `base\` and `updates\`). A `.nup` with add-ons is unpacked that way on its own. The **Play**
page lists the DLC it found. RecompLauncher's Easy Setup can also take a folder of add-ons kept elsewhere (the `dlc` folder, one folder per title id); setup points `fefw.ini` at it. This is for RecompLauncher only: this launcher's own Play page expects the dump's `dlc\` and asks you to run Set up again, which goes back to it.

## Mods

RomFS mods go in the `mods\` folder next to the launcher (**Play** > **Open mods folder**), in the layout emulators use:
`mods\<mod name>\romfs\...`. Files in a mod replace the game's files at the same path. If two mods change the same file, the mod
whose name sorts last wins, so you can name them `01-...` and `02-...`. Rename a mod to start with `-` or `_` to turn it off.

Code mods (exefs patches) go in `mods\<mod name>\exefs\`: an `.ips` / IPS32 file named after main's build ID
(`DB93B0125F4850601D61C3EDDE012F8EB2ECFE77.ips`) or a `.pchtxt` with that `@nsobid`. Because the game code is compiled ahead of
time, they are applied when the game is built, not when it starts: after adding, changing or removing one, press **Set up** again
(the Play page says the build is out of date). Patches for the other modules (sdk, subsdk*) are not used, and a patch that touches
an instruction this port hooks makes Set up stop and name it.

Code mods made for this port can also be DLLs in the mod's `exefs\` folder (`mods\<mod name>\exefs\<mod>.dll`), so one mod folder can
carry an emulator's patches and this port's DLL side by side. They load when the game starts and can hook the game at the sites it was
built with, call the game's functions, give the game functions of their own to call (what a new menu entry needs) and edit the game's
data files as they are loaded. A mod can also show its own settings on a **Mods** tab in the **F1** menu, through the same panel
interface as EmblemWeave and ObeliskChamber. A mod that needs hook sites of its own lists them in `exefs\<any name>.sites` next to
its DLL; Set up lifts the game with them, so press **Set up** again after adding or removing such a mod. Unknown site kinds
(for example from another port) are skipped with a warning. A DLL runs with the same
rights as the game: only install mods you trust. `[fs] dll_mods = false` in `fefw.ini` turns them off. Writing one:
[runtime/include/nxrt/mod_api.h](../runtime/include/nxrt/mod_api.h) and
[runtime/docs/MOD_INTERFACES.md](../runtime/docs/MOD_INTERFACES.md), with a sample in
[examples/mods/panel-demo](../examples/mods/panel-demo/README.md).
