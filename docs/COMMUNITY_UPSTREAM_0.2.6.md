# Mod Host on community upstream v0.2.6

## Source

The base is `metrist38/fefw-recomp` release `v0.2.6`,
commit `62969a6fa95f24d2196386628273d7ebb1336f0d`. It is pinned to the
release, not the newer master branch. The Mod Host delta comes from this
fork's tested original-upstream 0.2.0 integration.

## Integration

- Retain the community launcher's setup, settings and native mod improvements.
- Add the launcher Mods page and existing manifest-based F1 runtime packages.
- Feed manifest IPS packages through the upstream patch writer, preserving
  native `.ips`, IPS32, `.pchtxt`, RomFS and DLL-hook mod discovery.
- Native loose mod directories do not require a Mod Host manifest.
- Enforce protected hook-site checks. Two exact, previously tested Mod Host
  multiplier packages have hash- and build-ID-checked entry-hook compatibility.
  This does not allow arbitrary patches at hook sites.
- Pass the combined site list to checkout builds and publish `fefw.sites`
  with the matching runtime main and executable.
- Preserve the keyboard text input fix for F1 item search and numeric fields.
- Use the fork's update repository as the default update feed.
- Give Vulkan-Headers and volk distinct archive cache names; their pinned
  versions and hashes are unchanged.

## Verification

- Production Tauri launcher build passed, version 0.2.6.
- Mod package/build regression tests: 19 passed, including native/manifest
  coexistence, protected hook conflicts and publication rollback.
- Launcher Rust tests: 32 passed, 3 ignored (network/download and external
  integration checks). Corrected a Linux-path test expectation to use native
  path separators when the test is run on Windows; runtime logic unchanged.
- Game-independent runtime DLL built successfully with LLVM 23.1.5, Release.
  This compiles the F1 menu and game hooks without rebuilding any game code.
- Actual pre-existing item 2x and friendship 5x packages produce the same
  staged main bytes as the previous patch system.
- No game executable was recompiled or replaced. In-game testing remains
  required after the player runs Setup.
- Remote push, release publication and workflow re-enablement are deferred
  until the local test passes.

## Local test

Build and start `launcher/src-tauri/target/release/fefw-launcher.exe` in this
checkout. Confirm the repository setting selects this checkout and the game
folder selects its own `work/play`, then run Setup to compile the game.
Existing player files can be copied into the new run folder; do not copy old
build records or an old game executable and assume they validate the new runtime.

Verify game startup, previous saves, ordinary gameplay, IPS multiplier effects,
F1 item search/addition, gift amount input, apply/cancel, and existing native
RomFS mods. This checkout's game output is separate from the older checkout.
