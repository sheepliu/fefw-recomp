# Profile-guided runtime (PGO)

`fefw_rt.dll` (the render thread's NVN replay, the kernel and service code) is built with a recorded execution profile,
`runtime/pgo/fefw_rt.profdata`. The compiler uses it to inline and lay out the code the game actually runs. The lifted game code
in `fefw.exe` is not profiled, and players need nothing for this: it is part of the DLL.

## Which builds use it

A Release configure with clang 23 or newer uses the committed profile (`NXRT_PGO_PROFILE=auto`, the default), together with
ThinLTO (`NXRT_LTO`). The configure prints `nxrt: profile ... (id ...)` or `nxrt: no profile`.

| `-DNXRT_PGO_PROFILE=` | Effect |
|---|---|
| `auto` | `runtime/pgo/fefw_rt.profdata` in Release builds, none otherwise |
| `off` | no profile |
| a `.profdata` file | that profile (a relative path is taken from the top-level source folder) |

A function whose source changed since the recording is built without its profile; the rest keeps it. Nothing fails, so the
profile slowly loses effect as the runtime changes. Record it again after large changes to the render path.

The file is read by the LLVM version that wrote it or a newer one: record it with the version CI uses.

## Recording a new profile

The profile runtime (`clang_rt.profile.lib`) comes with LLVM's Windows installer. A cross build on Linux needs one built from the
same LLVM version's `compiler-rt/lib/profile`, with the static C runtime; name its folder with `-DNXRT_PGO_RUNTIME_DIR`.

1. Build the instrumented DLL in a folder of its own:

   ```
   cmake -S game-build/fefw -B work/build-rtdll-pgogen -G Ninja -DCMAKE_BUILD_TYPE=Release <toolchain options> \
       -DFEFW_RT_DLL=ON -DNXRT_DAV1D_ASM=ON -DNXRT_PGO_GENERATE=ON -DNXRT_LTO=OFF
   cmake --build work/build-rtdll-pgogen --target fefw_rt
   ```

2. Put that `fefw_rt.dll` into a copy of a play folder and run scripted routes ([PERF_ROUTES.md](PERF_ROUTES.md)) with
   `LLVM_PROFILE_FILE` set, for example `Z:/path/rt-%p.profraw` under Wine or Proton. The counters are written when a scripted
   run ends (the log says `profile counters written`); a run closed another way records nothing. The instrumented game runs
   slower, which does not spoil the profile.

3. Merge, and replace the committed file:

   ```
   llvm-profdata merge -o runtime/pgo/fefw_rt.profdata rt-*.profraw
   ```

4. Compare a normal Release build against the previous one on a route that was **not** part of the recording.

## The committed profile

Recorded on 27 Nov 2026 with clang 23.1.5 from two scripted runs: a battle map (idle, camera, cursor) and the Leda transition
with its first cutscene. The Dagsion hub was left out and used for the comparison (PERF_NOTES.md). It holds counters and function
names of the runtime and its libraries, no game code and no file paths.
