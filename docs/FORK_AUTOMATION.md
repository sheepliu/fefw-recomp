# Fork release automation

The `sheepliu/fefw-recomp` fork tracks `Goldoire/fefw-recomp:master`.
`.github/workflows/launcher-sync.yml` checks for upstream commits hourly and
can also be run from Actions > Sync upstream and publish mod-host package.
It merges upstream master, reads the most recent published upstream Release,
and names the fork prerelease `modhost-<upstream-tag>-<fork-commit-prefix>`.
When that tag already exists, the run skips rebuilding and publishing.
Upstream merge conflicts stop the run for manual resolution.

The Windows job builds the production Tauri launcher, the game-independent
runtime DLL and the project's official offline Setup package. It runs mod
framework tests and scans the ZIP to ensure it contains no game code, dump,
saves, shader cache, IPS patch or mod package. The release includes the ZIP,
its SHA-256 checksum and notes with the upstream release tag, exact fork
commit, changes and installation steps. Publishing happens only after a
successful package build. The user's PC still builds `fefw.exe` from their
own game dump; GitHub never translates or uploads game data.

The source keeps the F1 runtime implementation, but the two gameplay IPS
patches and the `item-add`/`friendship-adjust` package folders are not tracked
and are not distributed. Players provide and import compatible packages
themselves. The fork prerelease tag does not represent an official upstream
binary release.
