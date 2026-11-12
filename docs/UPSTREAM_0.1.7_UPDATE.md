# Upstream 0.1.7 integration and launcher build (2026-11-12)

Upstream release: [`v0.1.7`](https://github.com/Goldoire/fefw-recomp/releases/tag/v0.1.7), commit
`0a8adfee46f4eead96f36a6ac678b0be6212fc7a`. Integrated into the local
`master` from fork baseline `d659ce8ed6a502b18d6f0da3be205129904bcc32`.

## Change record

Targets: upstream `launcher/`, `runtime/`, `game-build/`, and related source
files in the local `fefw-recomp` repository. Reason: update the launcher source
and its bundled game runtime/setup support to upstream 0.1.7, then rebuild the
release launcher. No game executable, game dump, save data, or portable package
was replaced.

Before building, the existing launcher at
`launcher/src-tauri/target/release/fefw-launcher.exe` was backed up to
`work/mod-backups/pre-upstream-v0.1.7-20261112/fefw-launcher-d659ce8.exe`.
Backup SHA-256:
`7265A80F1C069E495E526E7BC11C12800068C043B08F4D1B2509DF4430DEA0A0`.
Source rollback ref: `backup/pre-upstream-v0.1.7-20261112`.

The 0.1.7 merge required resolving conflicts in
`game-build/release/fefw_build.py`, `launcher/src-tauri/src/settings.rs`, and
`launcher/src-tauri/src/setup.rs`. Resolutions retain the fork's ExeFS mod
staging, IPS patch fingerprinting, runtime `main` selection, LLVM discovery,
and Setup log path, while incorporating 0.1.7 CPU-variant selection,
cross-platform path handling, and Setup improvements. The release build stamp
format is 4 to invalidate prior stamps after combining the CPU variant and
mod-patch inputs.

## Build and verification

- `python -m py_compile game-build/release/fefw_build.py`: passed.
- `python -X utf8 -m unittest discover -s tools/mods -p 'test_*.py'`: 16 passed.
- `npm.cmd ci` and `npm.cmd run build` in `launcher/`: passed.
- `cargo test --locked` in `launcher/src-tauri/`: 21 passed, 2 ignored.
- `npm.cmd run tauri build -- --no-bundle` in `launcher/`: passed.
- New launcher: `launcher/src-tauri/target/release/fefw-launcher.exe`,
  4,155,904 bytes, SHA-256
  `6E447F283E17BA7470F2C145CF7F0E6679DD6376A09DB06C1C71680653F68384`.

The launcher executable in any installed or portable directory was not
replaced. The game itself was not rebuilt by the launcher build.

## Rollback

To inspect the pre-update source without moving the current branch:

```powershell
git switch --create rollback/pre-upstream-v0.1.7-20261112 backup/pre-upstream-v0.1.7-20261112
```

To restore the previous local launcher artifact:

```powershell
Copy-Item -LiteralPath 'work/mod-backups/pre-upstream-v0.1.7-20261112/fefw-launcher-d659ce8.exe' -Destination 'launcher/src-tauri/target/release/fefw-launcher.exe' -Force
```
