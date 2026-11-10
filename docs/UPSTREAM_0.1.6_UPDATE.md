# Upstream 0.1.6 integration record (2026-11-10)

Target: `launcher/`, `runtime/`, `game-build/`, `tools/mods/`, and `.github/workflows/launcher-sync.yml` under this repository. The upstream release `v0.1.6` (`04535f54769ca9a7143bb4d0672a1b304ed1c851`) updates launcher and game runtime behavior. Preserve the fork's Mods page, staged ExeFS workflow, F1 runtime-mod interface, and keyboard text-input fix while integrating it.

Before edits, source was at commit `66cb51f837cdef484e7a6d6407cfd9efaf4f881e`; backup ref: `backup/pre-upstream-v0.1.6-20261110`. The original executable was `launcher/src-tauri/target/release/fefw-launcher.exe` (SHA-256 `1098061CD3097B3E6AE74A67AA40C9944B01E9630A3A7620029FD8D73D53B763`); backup: `work/mod-backups/pre-upstream-v0.1.6-20261110/fefw-launcher-66cb51f.exe` (same SHA-256).

Rollback: switch to `backup/pre-upstream-v0.1.6-20261110` for the previous source, after preserving any new work. Restore the launcher executable with:

```powershell
Copy-Item -LiteralPath 'work/mod-backups/pre-upstream-v0.1.6-20261110/fefw-launcher-66cb51f.exe' -Destination 'launcher/src-tauri/target/release/fefw-launcher.exe' -Force
```

No game executable, user's saves, personal configuration, or previously packaged portable game is changed as part of the launcher source update.

## Integration and verification

- Merged upstream `v0.1.6`, preserving the fork Mods page, staged ExeFS and runtime mod handling, F1 Mods page, CJK catalog font, and SDL text input. Integrated upstream Joy-Con 2 mouse hooks and UI translations. The launcher's RomFS note now points to its ExeFS/runtime Mods page rather than incorrectly saying these patches are unsupported.
- Added upstream's `runtime/games/fefw/fefw_mouse.cpp` to both source-freshness lists, `launcher/src-tauri/src/setup.rs` and `tools/mods/dev_build.py`.
- `python -X utf8 -m unittest discover -s tools/mods -p 'test_*.py'`: 16 tests passed.
- `npm.cmd ci`, `npm.cmd run build`, and `cargo test --locked`: success (14 passed, 1 pre-existing ignored).
- `cmake --build game-build/fefw/build/release --target nxrt_game_fefw --parallel 8`: completed; compiled native runtime libraries without building or replacing the game's executable. Upstream compilation warnings remain.
- `npm.cmd run tauri build -- --no-bundle`: Release launcher built at `launcher/src-tauri/target/release/fefw-launcher.exe`, SHA-256 `7265A80F1C069E495E526E7BC11C12800068C043B08F4D1B2509DF4430DEA0A0`.

This update integrates the game runtime sources, but the player's `fefw.exe` still needs a separate Setup build before testing 0.1.6 behavior. Do not assume that replacing only the launcher binary upgrades an already compiled game.
