# Mod package validator and build input

`modpack.py` is a standalone, standard-library-only **preflight and staging**
tool. The tool itself does not modify the game dump, launch the game, or start
a rebuild. Both the **release-package** builder and the checkout's developer
CMake builder read enabled patch packages from the selected run directory.
`release_build_supported` means
the build can consume that patch; it does not mean the patch is already built
or verified in game. `metadata_only` means the runtime package has no
matching execution backend. The launcher has a Mods page for importing, enabling,
validating files, and returning to Setup to rebuild; see
`../../docs/COMMUNITY_UPSTREAM_0.2.6.md`.
While Setup runs, the page blocks imports and enablement changes. An import
validation failure keeps the selected file and form values so they can be
corrected without browsing again. The original upstream mods-folder entry
continues to work alongside the UI.

An installation uses `mods/<id>/manifest.json` for each package and a separate
`mods-state.json` file beside `mods/` for enabled IDs. Imports can be IPS,
IPS32, pchtxt, a patch-only ExeFS folder, or an existing manifest package.
An omitted `--id` is generated automatically; users need not rename patch
files. Folder imports report any files skipped for other module/build IDs.
The input `--main` is an **original, unmodified** game's ExeFS `main`. For a
release installation, `--mods-dir` must be the same `<game directory>/mods`
whose `fefw.exe` the launcher builds (normally the directory containing the
release launcher). The path below is a **preview only**; it does not affect an
installed game. Do not apply a patch to the original dump.

```powershell
$root = (Resolve-Path .).Path
$main = Join-Path $root 'game\dump\exefs\main'
$mods = Join-Path $root 'work\modpack-preview\mods'
$ips = Join-Path $root 'patch.ips32'
python -X utf8 "$root\tools\mods\modpack.py" --main $main --mods-dir $mods import $ips --id example-patch --name 'Example Patch'
python -X utf8 "$root\tools\mods\modpack.py" --main $main --mods-dir $mods enable example-patch
python -X utf8 "$root\tools\mods\modpack.py" --main $main --mods-dir $mods list
python -X utf8 "$root\tools\mods\modpack.py" --main $main --mods-dir $mods disable example-patch
```

For a self-contained package, import a directory containing this manifest and
the referenced patch file (forward-slash relative paths only):

```json
{
  "manifest_version": 1,
  "id": "example-patch",
  "name": "Example Patch",
  "version": "1.0.0",
  "type": "exefs-ips",
  "game_build_id": "DB93B0125F4850601D61C3EDDE012F8EB2ECFE77000000000000000000000000",
  "target_module": "main",
  "ips_format": "IPS32",
  "patch": "patch.ips",
  "sha256": "the lowercase 64-character SHA-256 of patch.ips"
}
```

The old `exefs-ips` manifest remains readable. New imports use
`type: "exefs-patches"` with `target_module: "main"` and a `patches` array;
each entry has a relative `path`, `sha256`, and `format` (`IPS`, `IPS32`,
or `PCHTXT`). One folder can contain several matching patches.

The tool loads the pinned upstream `fefw_build.py` for parsing and writing.
It accepts byte writes in main's **text, rodata and data**, including
unaligned data, instead of treating every word as an ARM64 instruction.
IPSwitch enabled/disabled blocks and offset shifts follow the native parser.
The IPS record address is the **module memory offset + 0x100**, not an offset
into a compressed NSO file. Checksums, Build IDs, segment boundaries and
protected hook sites are still checked. Overlaps follow upstream ordering:
mod names sort first and later writes win. No arbitrary cross-module support
or ability to write into NSO segment gaps has been added.

Managed packages with an `exefs/` child are not additionally scanned as loose
native patches; this keeps their UI enable/disable state effective. Original
native folders without a managed patch manifest keep the upstream behavior.
DLL/site-list mods should use the original mods-folder entry.

When the release launcher runs Set up, `tools/build/fefw_build.py` verifies the
original dump and builds against a temporary uncompressed NSO under the game
directory's `build/exefs/`. It updates checksums for all modified segments and
uses that image for translation. Only after successful linking does it publish
the matching `fefw.exe`, hook-site list and runtime `exefs/main`, keeping backups
in `build/`. `--check` compares the
enabled ordered patch set, runtime NSO hash, executable hash, and build tools;
switching IPS patches makes Setup require another full build. Currently there
is **no incremental recompilation** or in-game patch-effect verification.

Runtime manifests can be imported with `type: "runtime"`,
`capability_version: 1`, lists of `capabilities`, `operations`, and optional
`resources`. Each operation declares `id`, `label`, `capability`, and `fields`
with `name`, `label`, and `type` (`integer`, `choice`, `text`). Only matching
runtime implementations can be enabled; their forms appear under F1 > Mods
after restarting the game. The package format does not execute arbitrary
scripts or promise that an unknown capability is implemented. The emulator's
old GDB-based packages are **not** compatible runtime packages for the PC
recomp.

In a source checkout, import and enable IPS packages under the *selected run
directory* (normally `<repo>/work/play/mods`), using the original unmodified
`<repo>/game/dump/exefs/main` for `--main`. Launcher's Set up then calls
`tools/mods/dev_build.py build --repo <repo> --game <run directory>` with the
checkout Python environment. The developer build consumes a staged ExeFS in
`<run directory>/mod-build/input-exefs`, keeps generated source and libraries
in separate `work/lift/fefw-mod-*` and `work/build/fefw-mod-*` directories,
and builds an EXE in `work/build/fefw-mod-exe-*`. Only after successful
translation/linking does it publish the EXE/main/site-list set to `<run directory>/mod-build/active/`
and write `mod-build/done.json`. The original dump and the checkout's old
`game-build/fefw/build/release/fefw.exe` remain unchanged. Old active files
are backed up by hash under `mod-build/previous/`; a publication error restores
the old pair. Launcher's `fefw.ini` points `[fs] main` to the active main and
the game starts from the paired active EXE. Run `dev_build.py check --repo
<repo> --game <run directory>` to verify the enabled patch set and artifacts;
Play requires a rebuild after any mismatch. An installation with no
`mods-state.json` and no native code patches keeps the original checkout build path. Disabling the last
patch still uses the isolated path and rebuilds an unpatched pair. The
regression coverage includes synthetic NSOs and previous real multiplier
packages. A new community patch still needs its own in-game verification.

Run the validator tests without a game dump:

```powershell
cd '<checkout>'
python -X utf8 -m unittest discover -s tools/mods -p 'test_*.py' -v
```
