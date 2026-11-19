# Mod package validator and build input

`modpack.py` is a standalone, standard-library-only **preflight and staging**
tool. The tool itself does not modify the game dump, launch the game, or start
a rebuild. Both the **release-package** builder and the checkout's developer
CMake builder now read enabled IPS packages from the selected run directory.
`release_build_supported` means
the build can consume that patch; it does not mean the patch is already built
or verified in game. `metadata_only` means the runtime package has no
matching execution backend. The launcher has a Mods page for importing, enabling,
checking conflicts, and returning to Setup to rebuild; see
`../../docs/MODDING_ROADMAP.md`.
While Setup runs, the page blocks imports and enablement changes. An import
validation failure keeps the selected file and form values so they can be
corrected without browsing again. A conflicting enablement reports the two
mod IDs and offset, leaving the prior selection intact.

An installation uses `mods/<id>/manifest.json` for each package and a separate
`mods-state.json` file beside `mods/` for enabled IDs. Raw IPS imports need an
explicit `--id` to prevent three Build-ID-named files replacing one another.
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

Only `main`'s 4-byte-aligned `.text` writes are accepted for now. IPS and
IPS32 support ordinary and RLE records but reject truncate extensions,
trailing bytes, overlapping writes, out-of-bounds writes, and writes to the
NSO header. The IPS record address is the **module memory offset + 0x100**,
not an offset into the original compressed NSO file. An IPS checksum and
Build ID must match the manifest and selected `main`. Conflicts are checked
before enabling; no conflicting state is written.

When the release launcher runs Set up, `tools/build/fefw_build.py` verifies the
original dump and builds against a temporary uncompressed NSO under the game
directory's `build/input-exefs/`. It updates the patched text checksum and
uses that image for translation. Only after successful linking does it publish
the paired `fefw.exe` and runtime `exefs/main`, keeping backups at
`build/previous-fefw.exe` and `build/previous-main`. `--check` compares the
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
translation/linking does it publish the pair to `<run directory>/mod-build/active/`
and write `mod-build/done.json`. The original dump and the checkout's old
`game-build/fefw/build/release/fefw.exe` remain unchanged. Old active files
are backed up by hash under `mod-build/previous/`; a publication error restores
the old pair. Launcher's `fefw.ini` points `[fs] main` to the active main and
the game starts from the paired active EXE. Run `dev_build.py check --repo
<repo> --game <run directory>` to verify the enabled patch set and artifacts;
Play requires a rebuild after any mismatch. An installation with no
`mods-state.json` keeps the original checkout build path. Disabling the last
patch still uses the isolated path and rebuilds an unpatched pair. This has
only been exercised with mocked compiler invocations and synthetic NSOs, not
with a complete PC-game build or in-game behavior test.

Run the validator tests without a game dump:

```powershell
cd '<checkout>'
python -X utf8 -m unittest discover -s tools/mods -p 'test_*.py' -v
```
