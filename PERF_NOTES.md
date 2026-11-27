# Performance Notes (`fefw-recomp`)

Current performance status, shipped optimizations, key toggles, deferred ideas, and open targets as of **v0.3.1+** (late Nov 2026).

- **Historical v0.2.0 lab log:** [docs/PERF_NOTES_0.2.0.md](docs/PERF_NOTES_0.2.0.md) (archived chronological notes from `stephenjayakar/fefw-recomp` through v0.2.0 / 14 Nov).
- **Benchmarking & A/B harness:** [docs/PERF_ROUTES.md](docs/PERF_ROUTES.md) (`tools/perf/ab_run.py`, `perf_report.py`, `record_to_route.py`).
- **Full config & toggle reference:** [runtime/docs/CONFIG.md](runtime/docs/CONFIG.md).

---

## 1. Bottleneck Profile (Where Time Goes)

From profiling dense town scenes (Dagsion hub/street, ~2,400–2,640 draws/frame) and battle maps:

- **Main game thread (`main`):** ~78–86% of busy CPU samples are in lifted AArch64 game code, with a flat profile (top 40 functions ~25–37%). Roughly ~48% of lifted-code samples land on guest memory loads (pointer chasing: `mov rax, [rax]`), making the game thread largely **memory-latency bound** alongside instruction-fetch/cache pressure across a large `.text` (~147 MB).
  - Main-thread waits are primarily short chains of `Basic_Worker1-4` jobs, `GFX_Worker0-4`, `MOCull_Worker1/2` + Leader, and GPU sync waits when the render thread falls behind.
- **Worker threads:**
  - `Basic_Worker1-4` (~95% lifted code): led by box/plane culling (`f_7101980580`), `f_71005ef6e0`, `f_7100a831a0`, `f_7100a9bb60`, `f_7101978d50`.
  - `MOCull_Worker1/2` + Leader (~99% lifted code): dominated by masked occlusion culling (`f_71019730e0` ~80–84% of workers, a 16k-instruction NEON rasterizer; `f_7101976dc0` ~61% of Leader).
  - `GFX_Worker0-4`: ~42% lifted code (`f_7100a7c620`), rest is NVN command recording.
- **Render thread (NVN → Vulkan replay):** CPU-bound on descriptor set building (`build_set`), draw/buffer binding & compute-out overlap tracking, texture preparation, and Vulkan driver calls (`vkCmd*`, `vkUpdateDescriptorSets`).
- **Fast-travel / arrival frames:** ~290–350 ms of spread-out lifted game logic (allocation/containers, ~14% under `nn::mem::StandardAllocator::Allocate`); zstd decompression is already native.
- **Thread balance, Dagsion hub standing still (26 Nov 2026, `v0.4.0-7-g8e5cff9d`):** Ryzen 5 5600X, RTX 4070, Proton, 3440x1440 DLSS Performance, 120 fps target, frame generation off, 3 runs, ~80 fps (~12.5 ms). CPU per frame, from per-thread usage and mean fps: `nvn-render` ~10.9 ms, `main` ~8.8 ms, the four busiest guest workers ~3.0 ms each (`Basic_Worker1-4`, by name in the occupancy run below). No thread is fully busy, and the `replay_threads` test below shows that speeding up the render thread alone moves frame time little. See the main-thread breakdown below.
- **Where `main`'s frame goes, Dagsion hub standing still** (same setup, 1 run with `[kernel] occupancy = 5`, 79.8 fps, so no visible overhead; averages of three 5 s reports, 12.5 ms frame):
  - On CPU: ~8.9 ms.
  - Waits ended by `Basic_Worker1-4`: ~1.25 ms (~0.86 on addresses, ~0.39 on mutexes).
  - GPU fence waits (`nvn.gpu: sync waits`, none behind the render thread): ~1.0 ms.
  - Waits ended by `GFX_Worker*`: ~0.7 ms (`GFX_Worker0` ~0.56).
  - Waits ended by `MOCull_Leader`: ~0.15 ms. While `main` waited on an address, no MOCull thread was running 92-94% of the time.
  - In all, `main` was blocked in the kernel ~20% of the time (~2.5 ms). During those waits all four Basic workers were busy only ~11% (address) to ~17% (mutex) of the time, which by the diagnostic's own reading points to job chains, not too few workers.
- **Thread balance, one battle map (same setup, 1 run of a locally recorded route):** at the 120 fps cap (8.33 ms) while idle and moving the cursor. CPU per frame: `nvn-render` ~4.9–6.2 ms, `main` ~3.3–4.0 ms, busiest workers ~0.8 ms; GPU ~4.5–4.8 ms. This scene has headroom on this PC, so A/B it by CPU time per thread, not fps. Attacks and enemy phases are not measured.
- **`main` function profile, Dagsion hub standing still (26 Nov 2026, Linux `perf`, same setup):** one run, ~13.5 s, ~42,000 user-space samples of `main` (kernel time not sampled); 76 fps against ~80 unsampled. Game build made 26 Nov with the runtime from `8e5cff9d`.
  - Lifted code of `main` ~76% in listed functions, plus ~9% in entries outside the function table (mostly the two culling originals below); `fefw_rt.dll` 11.9%; `sdk` 3.7% (part of the game code figures); Wine, drivers and unknown ~3%.
  - Flat: top 1 / 10 / 40 / 100 functions = 2.9 / 15 / 35 / 54%.
  - The lifted originals of the native culling tests are the 1st and 4th hottest entries: `f_7101980580` 2.9% and `f_71019786c0` 1.3% (`FEFW_HLE_CULL` off). A shared helper they and nearby functions call adds ~0.6%. No other game function is above ~1.5% (`f_7101978d50` 1.4%).
  - Calls from the game through the runtime, self time: `dispatch_call` 2.9%, `import_resolve_named` 0.7%, `host_import` 0.6%, `count_import` 0.5%.
  - **Runtime atomics were not inlined (found in this profile; fixed with `-mcx16` in `runtime/CMakeLists.txt`):** MSVC STL's `<atomic>` gives all its functions `target("cx16")` under clang on x64, and the runtime was compiled without `-mcx16`, so clang refused to inline them (`-Rpass-missed=inline`: "conflicting target features"). Each `std::atomic` load, store, exchange or add was then a call. Seen in the installed release `fefw_rt.dll` too (~1,100 call sites to the load helpers). Recompiling `core/dispatch.cpp` with `-mcx16` took its calls to the helpers from 47 to 0. Self time inside the helpers: `nvn-render` 2.7%, `main` 1.4%, a `GFX_Worker`-like thread 4.1%, Basic workers 0.4% (inlining keeps the memory access itself). Both game CPU variants already require CMPXCHG16B.
    - With the flag, direct calls to `std::atomic` functions in `fefw_rt.dll` went from 3,818 to 0. A/B of the runtime DLL alone (`bb75e6ad`, 26/27 Nov, setup as the section 1 balance, 3 runs each; still / walk / camera turn): CPU per frame `nvn-render` -2.7 / -2.4 / -2.6%, `main` -1.8 / -2.1 / -2.1%, Basic workers unchanged, whole process about -1.7%; mean fps +1.5 / +1.4 / +1.9%; median frame time -1.4 / -2.2 / -1.3%. Per-run ranges were separate for render CPU in all three scenes; fps ranges overlapped standing still. Loading scene too noisy to judge.
- **ThinLTO for the runtime (`NXRT_LTO`, default on for Release builds with clang and `lld-link`):** objects are bitcode and `lld-link` optimises across files; `fefw_rt.dll` grows ~3% (14.36 → 14.84 MB), direct calls drop ~7%. A/B of the DLL alone, with `-mcx16` on both sides (26/27 Nov, setup as the section 1 balance, Dagsion, 3 runs each; still / walk / camera turn): `nvn-render` CPU per frame -3.0 / -2.6 / -1.5% (per-run ranges separate except the turn), `main` unchanged (+0.4 / -0.5 / +0.7%), mean fps +1.2 / +1.3 / +0.7%, median frame time -1.0 / -1.6 / -0.8%. Loading scene too noisy to judge. Runtime tests under Wine gave the same results with and without it; graphics tests not run.
- **PGO for the runtime ([docs/PGO.md](docs/PGO.md), committed profile `runtime/pgo/fefw_rt.profdata`):** front-end instrumentation of `fefw_rt.dll` only; 7,347 of 23,922 functions ran in the recording (a battle-map route and the Leda transition with its first cutscene; the Dagsion hub held out). `fefw_rt.dll` 14.84 → 12.91 MB. A/B of the DLL alone, ThinLTO and `-mcx16` on both sides (27 Nov, setup as the section 1 balance, the same game exe on both sides, 3 runs each). Dagsion, not recorded (still / walk / camera turn): `nvn-render` CPU per frame -10.5 / -10.0 / -10.5%, whole process -3.6 / -2.9 / -3.6%, `main` unchanged (-0.9 / -0.1 / -1.1%), mean fps +5.4 / +4.2 / +3.8%, median frame time -6.0 / -4.0 / -2.5%, 1% low +7.4 / +5.4 / +9.4%; per-run ranges of render CPU and mean fps separate in all three. Worst frame standing still went 26.4 → 38.7 ms (ranges barely apart; this figure swings between batches), walking and turning showed no consistent change. Battle map (recorded; at the 120 fps cap): `nvn-render` CPU -9.7 to -10.6%. Runtime tests under Wine gave the same results with and without the profile.
- **`nvn-render` function profile, Dagsion hub standing still (27 Nov 2026, `perf`, `fma` build with native culling, ThinLTO runtime, setup as the section 1 balance):** one run, ~45,700 user-space samples of the thread, 81.7 fps.
  - `fefw_rt.dll` 62.1%, `libnvidia-glcore` 26.6%, Wine, winevulkan, libc and others ~11%.
  - Far more concentrated than `main`: top 1 / 10 / 20 / 40 runtime functions = 8.2 / 35.7 / 46.7 / 56.0% of the thread, of 287 runtime functions seen.
  - Leaders: `build_set` 8.2%, `begin_draw` 7.1%, `run_list` 6.4% (together 21.7%), then `pool_lookup` 2.5%, `resolve_in_pool` 2.2%, `resolve_texture_raw` 2.2%, `resolve_read` 2.1%, `prepare_textures` 1.9%, `is_color_feedback` 1.7%, `graphics_pipeline` 1.5%.
  - Under ThinLTO a function's samples include what was inlined into it. Smaller library calls seen: `memmove` 0.9%, the STL's `__std_min_8u` 0.9%, `memcmp` 0.6%.
  - **The run had `[nvn] replay_stats = 300` and `[prof] gpu_stats = 300` on**, so the shares above include the stats code: about 4.5% of the thread's samples were in `replay_stats`' own code (`rs_mark`, `RsScope` and the other helpers 3.8%, the stats block in `build_set` 0.8%), mostly not run in normal play. Leave it off when profiling the render thread.
  - By source line (same samples): 1.6-1.7% was `pool_lookup` searching its `std::map`; 1.4% the copy of each `VkWriteDescriptorSet` into its vector in `build_set` (a 16-byte load over a pointer just stored, by the disassembly); `__std_min_8u` came from a three-value `std::min` on uniform buffer binds. All three changed afterwards (section 3).

---

## 2. CPU & Recompiler / Codegen (`recomp/`, `game-build/fefw/`)

### Shipped Optimizations
- **Local NZCV flags (`opt = ('flags',)` in `recomp/nxrecomp/opt.py`):** Keeps AArch64 `NZCV` in function-local `NxFlags` instead of spilling 4 fields to `Ctx` on every flag-setting instruction (per-function backward liveness check; ~-18% x86 instructions in hot functions, ~-12–14% main CPU time).
- **Whole-register vector stores (`opt = ('vstore',)` + `neon.h`):** Emits scalar-into-lane-0 results, `MOVI` pairs, and `c->v[n] = <V128>` as single 16-byte stores (`nx_vst`), eliminating partial-write store-forwarding stalls (-4.8% static instructions).
- **`NX_NOESC` (`noescape`) on `Ctx *c` (`recomp/include/nxrecomp/ctx.h`):** Marks `Ctx *` as `noescape` on lifted functions and runtime ABI entries, paired with `-mllvm -capture-tracking-max-uses-to-explore=100000` in `fefw_build.py` / `NxRecomp.cmake`. Prevents LLVM from reloading `Ctx` fields after every guest memory store (-20% context loads, -17% context stores, ~-8–9% main CPU).
- **Inlined indirect-call dispatch (`NX_INLINE_INDIRECT`, `recomp/include/nxrecomp/cxx.h`):** Expands `call_indirect` inline to `nx_call_indirect`, indexing the main module's flat offset table (`nx_fn_flat` in `runtime/src/core/dispatch.cpp`) right at each `BLR`/`BR` site so every call site keeps its own x86 branch predictor slot. Fallback uses a 1,024-entry per-thread cache + lock-free hash table.
- **Hot-code packing & cross-TU inlining (`recomp/games/fefw/hot.txt`, `inline.txt`):**
  - `hot_order` packs the 3,409 hottest functions (99.5% of samples) into `.text$nxh<rank>` sections (.text 189 → 147 MB).
  - `inline_edges` copies 1,039 small hot callees (≤60 insns) as `static inline` into their hot callers' TUs (measured neutral on its own, kept in config).
- **Native HLE replacements (`GameConfig.inline_replace` in `recomp/games/fefw/config.py`, `game-build/fefw/src/fefw_hle.cpp`):**
  - `nnSdk` libc routines: `memcpy`/`memmove`, `memset`, `strlen`, `memcmp`, `strcmp` (`cxx.h` `hle_*`).
  - `ZSTD_decompressMultiFrame` (`0x71000c6a60`): native zstd 1.5.7 with `NX_ORIG` fallback (`FEFW_HLE_ZSTD=0` disables). Eliminates ~250 ms loading-frame spikes.
  - Native culling tests (`0x71019786c0` view-box, `0x7101980580` cylinder-box, ported from ObeliskChamber `ab56dd1`): **on by default** (`FEFW_HLE_CULL=0` runs the lifted originals, `FEFW_HLE_CULL=2` compares the two). They are part of `fefw.exe`, so the change reaches players with the Set up of the release that carries it.
    - Offline test, `recomp/tests/hle_cull_test.cpp` (27 Nov 2026, built with the bundled compiler and each variant's flags, `fma` and `compat`): 3 million random cases, plus a search in each for the exact radius and plane offset at which the answer changes (1,605,534 cylinder and 765,980 view-box changes found): no difference in either variant. Random answers alone do not show a rounding difference: a control build that rounds the cylinder test twice passed 50 million random cases, and the search catches it (449,206 of the cylinder changes); a control that adds up a view plane in another order is caught in 233,829 of the view-box changes.
    - In-game verify (`=2`): no mismatches on the `fma` build (26 Nov: a Dagsion hub route and a hand-played battle map, ~100 s) or the `compat` build (27 Nov: the Dagsion and battle routes), at least 16.7 million cylinder and 4.2 million view-box calls per run. Other maps and areas are not covered.
    - A/B of one build, `=0` against the default (27 Nov, `fma` build with the ThinLTO runtime, setup as the section 1 balance, Dagsion, 3 runs each; still / walk / camera turn): mean fps +1.7 / +1.4 / +1.3% (medians of the runs), `main` CPU per frame -3.8 / -3.6 / -2.1% (per-run ranges separate except the turn), the four busiest workers -7.8 / -7.9 / -6.5% (ranges separate), render thread unchanged. An earlier run on 26 Nov gave +1.4 / +1.0 / +1.2% fps. Loading scene too noisy to judge.
- **Compiler & CPU targets (`game-build/release/fefw_build.py`, `recomp/cmake/NxRecomp.cmake`):**
  - Lifted code built with `-O2` (note: bundled `zig c++` maps `-O1`/`-O2`/`-O3` to clang `-cc1 -O2` with loop/SLP vectorization; MSVC source builds switched from `/O1` to `/O2` in `ffd542d` / `a19645f`), `-g0`, `-fomit-frame-pointer`, `-ffp-contract=off`.
  - Two CPU build variants (`--cpu fma` default: `x86_64+cx16+fma`; `--cpu compat`: `x86_64+cx16+sse4_2+popcnt` + musl `fefw_fma.c`).
  - Developer PGO pipeline (`fefw_build.py --pgo-gen` / `--pgo-use`, with ext-TSP block placement; measured ~-4.5% main CPU).
- **Per-thread ABI counters (`runtime/include/nxrt/tl_counts.h`):** Replaced shared atomic counters on `host_import`, HLE thunks, and SVCs (eliminated 8.9–23.7% main / ~50% worker cache-line bouncing during load spikes).

### Tried, Deferred, or Experimental
- **`opt.py` `'tregs'` (`x8..x17` in locals, `NXRECOMP_OPT=flags,vstore,tregs`): tested, small gain, safety not established, stays off.**
  - Measured ~-2.2% to -3.6% main CPU in v0.2.0 tests. Turned off in `config.py` (`2df9df5`) pending safety verification. `4a6dce9` (ObeliskChamber `b8d0038` / PR #165 / issue #154) syncs `x9..x17` on branches to non-ABI pieces. `obelisk/master` runs it by default, together with opt `'loopbar'` (`f481d3d`, a compiler memory barrier on the back edge of polling loops), which is not ported here.
  - Re-measured 26 Nov 2026 (`v0.4.0-7-g8e5cff9d`, no PGO, setup as the section 1 balance), in an unmerged experimental build with `loopbar` and some registers excluded function by function. Dagsion A/B, 3 runs each (still / walk / camera turn): `main` CPU per frame -2.0 / -2.9 / -2.5%, the four busiest workers -2.3 / -2.7 / -2.3% (per-run ranges overlapped in some scenes), mean fps +1.1 / +0.8 / -0.7%, within noise.
  - Safety: an exploratory static check found places in the lifted game where a value may cross a call or return in these registers. The check itself had gaps, so neither the exclusions nor the short clean test runs show the build was safe.
- **`opt.py` `'xregs'` / `'vregs'` (full guest registers in locals):** Eagerly loading all registers at entry and after every call stretched live ranges, caused host register spills, and grew code size (plus 3/30 e2e failures around jump tables/tail calls). Would require **lazy loading** (load on first use after a sync point) + dirty tracking.
- **Clang 23 vs bundled Zig Clang 22.1.8:** Only -1.1% static instructions on hot set; not worth breaking PGO format / release reproducibility.
- **`-mcpu=znver2`:** Only -0.1% static instructions; neutral in A/B runs.
- **Large pages (2 MiB) for guest heap:** `tlbbench` showed +11–22 ns/miss in L2/L3 (+65 ns past L3), but conflicts with per-4 KiB `VirtualProtect` write-watch and requires `SeLockMemoryPrivilege` + major `memory.cpp` rework. Needs actual hardware DTLB-miss profiling (e.g. AMD uProf / `perf stat`) before considering.
- **Dropping `c->fn` store on function entry:** Only -0.3% static instructions, hurts crash-report precision; skipped.

---

## 3. NVN Render-Thread Replay & GPU Overlap (`runtime/src/gfx/nvn/`)

### Shipped Optimizations (v0.2.0 → v0.3.1+)
- **GPU/CPU overlap & submission splitting:**
  - `[nvn] split_submit_ms = 4` (default on as of `0e10804`, `split_min_draws = 16`): flushes recorded commands at render-pass boundaries mid-submission so the GPU starts work while the render thread keeps recording.
  - `[nvn] submit_batch = auto` defaults to `1` on Windows and Wine+NVIDIA (`0e10804`), keeping `frame` on non-NVIDIA Wine.
- **Hardware-aware GPU barrier & shadow defaults (`e5fc38d`, `5dfc016`, `0c35aec`):**
  - `[nvn] lean_barriers = auto`: enables `dispatch_barriers = tracked`, `game_barriers = tracked`, `batch_ubo_updates`, `feedback_levels`, and `depth_ro_sticky` on NVIDIA and AMD GPUs.
  - `[nvn] narrow_pass_conflicts = true` (`ab56dd1`, ObeliskChamber): narrower Vulkan memory barrier for attachment pass conflicts.
  - `[nvn] compute_shadows = auto` (`5dfc016`): automatically off on unified-memory iGPUs (avoids redundant shadow copies/barriers), on for discrete GPUs.
  - `[gfx] igpu_compat`: turns off `lean_draws` and `residency` on Intel iGPUs/Arc where needed.
- **Render-thread replay hot paths (`c027497`, `a0d3b44`, `742d778`, `11f6727`, `8685a6a`, `0e10804`, `0fc708d`):**
  - **Descriptor sets & layouts:** 256-entry `layout_cache`, 64-entry `alpha_cache` + `ProgLayout::writes_storage` (EmblemWeave `0fc708d`), precomputed `ProgLayout` texture lists & lazy dummy views (`a0d3b44`), deduplicated graphics SSBO pool/mirror lookups (`3x → 1x resolve_read` in `build_set`, `0e10804`), TSR-jittered camera UBO caching (`a0d3b44`).
  - **Binding & command recording:** fused `Op::BindBuffers` / `Op::BindTextures` single-pass diff+update in `run_list`, skipped no-op bind op bodies & `out_before_op`, skipped `memcpy` on `raw_same`/`raw_list_same` hits, uninitialized `RawBytes` for `CmdList::bytes`, enlarged `textures_note_bound` and `g_gfx_fast` caches to 2,048 slots, 64-slot `compute_pipeline` fast cache, cached Vulkan blend state on `GState::blend_vk` (`0e10804`, `11f6727`, `c027497`).
  - **Compute-out & texture checks:** `out_masks = true` (walk only bound buffer slots), non-TLS `PoolGpu` and `OutOverlapCache` on render thread, memoised servable compute-out slots (`out_draw_read_slot`), fused `tex_bind_sampled` with `ViewRequest` cache + identity-swizzle fast path + folded undefined-layout check, fast-path compressed textures out of `is_color_feedback`, fast-path `attach_targets(false)` (`0e10804`, `8685a6a`, `11f6727`).
  - **Barriers & counters:** batched `ReportCounter`/`ReportValue`/`SignalEvent` writes, tracked texture copy barriers, narrowed transfer barriers (`742d778`).
  - **Three small hot paths (27 Nov 2026, from the section 1 line breakdown):** `build_set` fills each `VkWriteDescriptorSet` in the vector instead of copying a local one, and takes its three-value minimum with two plain `std::min` calls; `resolve_in_pool`'s address cache has 1,024 entries instead of 64. `replay_stats` logs that cache's lookups and misses (`nvn.rstats: pool address cache`).
    - Misses, Dagsion hub standing still (1 run each, `replay_stats`): ~1,750 → ~790 per frame of ~26,300 lookups. Why the rest miss is not known.
    - A/B of the DLL alone against `ecba855f`, both with ThinLTO and the committed PGO profile (setup as the section 1 balance, stats off, the same 21 Nov game exe without native culling on both sides, Dagsion, 3 runs each; still / walk / camera turn): `nvn-render` CPU per frame -1.2 / -0.9 / -2.7% (per-run ranges separate except walking), mean fps +0.9 / -0.1 / +0.1% (medians of the runs, within noise). In the camera turn `main` and the busiest workers, which run none of the changed code, also moved by -0.7 / -1.1% with separate ranges, so read the gain as about 1-2%. The three changes were not measured one by one.

### Experimental / Non-Default
- **`[nvn] replay_threads = 2` (`nvn_cmdq.cpp`, default `1`):** Offloads `vkCmd*` and `vkUpdateDescriptorSets` to a dedicated `nvn-rec` worker thread (`replay_batch = 16`). Measured 16.8 → 13.6–14.2 ms render-thread work p50 on RTX 3080 at 4K in v0.2.0.
  - Re-tested 26 Nov 2026 (`v0.4.0-7-g8e5cff9d`, setup as the section 1 balance, 3 runs each; still / walk / camera turn): render-thread replay -18 / -18 / -19%, but mean fps only +3.8 / +3.0 / +1.5%, median frame time -4.8 / -3.5 / -1.2%, 1% low +5.8 / +0.2 / +6.1%. Loading scene too noisy to judge.
  - Cost: +0.6 to +0.8 of a core for the whole process. `nvn-rec` used ~72% of a core standing still while its own counter reported ~25% busy; the rest is probably its idle spin (up to ~100 µs while a scope is open, `worker_main`). `main` and the busiest workers took ~5–7% more CPU per frame.
  - **Stays at `1`.** Not tested on handhelds, battles or Windows. A shorter spin is untried.

---

## 4. Memory, Pool Mirror, Write-Watch & VRAM Residency

- **Device-local pool mirrors (`[nvn] pool_mirror = on`, `mirror_hot_copy = true`, `storage_read_copy = 2`):** Serves vertices, indices, UBOs, and read-only graphics SSBOs from VRAM instead of host PCIe memory.
  - `gfx_ssbo_readonly = true` and `cs_ssbo_readonly = true` (`ebdb5a1`): SPIR-V reflection (`DescriptorBinding::written` / `atomic`) avoids unnecessary GPU-write invalidation, shadow copy-backs, and barriers on read-only SSBOs.
  - `compute_shadow_min_bytes = 4096` (`ebdb5a1`): skips per-dispatch shadow copy-in/out for small non-atomic buffers.
  - Wine section alias probe (`8398593`): enables `pool_mirror` on NVIDIA Linux under Wine/Proton when the driver supports coherent aliased `VK_EXT_external_memory_host` mappings.
- **Write-watch & mirror bookkeeping (`ebdb5a1`, `ab56dd1`, `11f6727`, `8685a6a`, `273f7ad`, `0e10804`):**
  - Lock-free `Region::has_written` + global `write_seq()` and `O(active)` `g_tv_mirrors`/`g_sig_mirrors`/`g_want_mirrors` lists in `mirror_begin_submit()`.
  - Sequential write-watch fault coalescing (`8685a6a`) and elimination of repeated compute-out guard faults (`273f7ad`).
  - Popcount valid-page summary (`write_watch::count_valid`), word-wise `used`/`gpu_inv` bitmaps, 600-present aging for `sig_pages`, `PoolGpu::has_copy_chunk` fast path, inline `page_valid`/`range_valid`, removal of hot-path `LOCK XADD` atomics.
- **VRAM residency & pressure (`ebdb5a1`, `f1c23e6`, `4a6dce9`, `e4c6ce9`, `676819fb`, `83538ad6`):**
  - Proactive/on-demand LRU mirror chunk eviction (`[nvn] pool_mirror_evict_idle_s = 30`) and budgeted paced mirror/ASTC bursts (`273f7ad`, `f1c23e6`) to prevent post-Dagsion slowdowns and VRAM thrash.
  - `tex_residency_tick()` triggers on raw OS VRAM usage (`max(residency_percent + 5, 85)%`) so VMA fragmentation doesn't stall demotions; unified-memory iGPU & Wine `PagefileUsage` fixes (`d513153`, `e4c6ce9`).
  - Smaller VMA preferred block size on small heaps (`9a02cd1b`, `676819fb`), finalized textures borrowing rather than retaining live texture images (fixes ~7 GB shadow-map VRAM leaks, `83538ad6`), and `texprep` format-mismatch re-queue loop / BC5 rewrite remake fixes (`3243b466`, `27933b14`, `0a216d46`).
  - `[memory] scale_limits = auto` (`4a6dce9`): scales caches/budgets on ≤20 GiB RAM or ≤8.5 GiB VRAM systems.

---

## 5. Shaders, Pipelines & Cold-Cache Stutter

- **Zero-disk-I/O shader loading:** Full resident SPIR-V preload pack (`[nvn] shader_pack_all = true`, `shader_pack_resident = true`, ~170 MB in RAM) + background `nvn-order-write` thread (eliminated 300–450 ms game-thread HDD/file-cache stalls on first-use shaders).
- **Async pipeline & shader compilation:**
  - `fefw.exe --prepare-shaders` (`0e1afa2`, `37dfcfe`): precompiles shader parts at Setup.
  - `[nvn] compute_async = prebuild`, `prewarm_wait = false`, `gpl_burst_threads = 2`, `gpl_link_cache = false`: cut cold-cache battle worst frame from 142 ms → 44 ms.
  - Frame-time-paced background speculative compiles (`compile_pace_after_ms = 20`, `compile_pace_ms = 8`), `[nvn] gpl_cold = auto` (off on ≤8 hardware threads) (`ed1af52`), and async GPL deferral kept active across hitches instead of falling back to blocking compiles (`8a11afb1`, `b050dee9`).
- **ASTC GPU transcoding:** Enabled by default on Wine/RADV AMD iGPUs (`5dfc016`); slow-GPU sliding-window handoff (`AstcRateWindow`) and `[gfx] astc_gpu_cap_ms` (`4a6dce9`) protect against TDRs.

---

## 6. Kernel, Scheduling, Logging & Movies

- **Kernel & scheduler (`e5fc38d`):**
  - `[kernel] wait_spin = auto`: enables 30 µs wait/lock spinning on `main` and `Basic_Worker` on ≥8 physical cores (sleeping waits kept on ≤6-core CPUs / Steam Deck).
  - `[sched] starvation_lean = true`: cuts starvation guard CPU overhead from ~88 ms/s → ~24 ms/s.
- **RomFS cache & loading pacing:** `[fs] romfs_cache` is enabled by default (`b0d45059`, cleared on Set up); the frame limiter now stays active during loading screens (`735ee79f`).
- **Async logging (`ab56dd1`, `efc9a79`):** `[log] async` background writer (bounded 4 MiB queue) and `[log] repeat_limit` per-call-site throttling.
- **AV1 Cutscenes (`7c832b0`, `b14478f`):** `dav1d` built with NASM x86-64 AVX2/AVX-512 assembly (15.8 → 69.7+ fps offline decode; runtime warns if built C-only) plus zero-copy NV12 delivery and frame pacing (`#40`).

---

## 7. Benchmarking & Profiling Workflow

- **Reproducible A/B runs:** Use `tools/perf/ab_run.py` + `tools/perf/perf_report.py` with recorded `.tt` routes (see [docs/PERF_ROUTES.md](docs/PERF_ROUTES.md)). Use `--save-dir` so autosaves don't drift between runs.
- **`perf` under Proton (function profiles):** `perf record -e cycles:u -p <pid>` works at `perf_event_paranoid = 2`. It cannot name PE code (the `.text` of `fefw.exe` and `fefw_rt.dll` shows up as `/tmp/perf-<pid>.map`), so match addresses yourself, with the module ranges from `/proc/<pid>/maps`. `fefw.exe` has no symbols, but its function tables (`FnEntry` pairs of guest and host address in `.rdata` / `.data`) recover the guest names, with `.pdata` for the function ranges; `u_` bodies and `o_` originals are not in the tables. For the runtime, `llvm-symbolizer --relative-address` with `fefw_rt.pdb`.
- **Per-thread CPU under Wine / Proton:** the frame budget line reports `process 0`, and the one saved `sample_hz` capture checked (`v0.2.12`) showed `nvn-render` 99.9% waiting, which does not fit its replay load. Reading `utime + stime` from `/proc/<pid>/task/*/stat` on the host works, and the thread names are visible there.
- **`[prof] trace` distorts CPU-bound frames:** one Dagsion run with it on (26 Nov 2026, `v0.4.0-7-g8e5cff9d`) ran at ~61 fps standing still against ~80 untraced, with `main` CPU per frame ~12.6 ms against ~8.8 ms. A likely cause is that `dispatch.cpp` times every HLE call while the trace is on. In that run `main` made ~9 `WaitForAddress` waits per frame of 0.1 ms or longer (median 0.25 ms, ~2.5 ms/frame in all) and almost no GPU fence waits (~0.01 ms/frame). Untraced runs log ~1.1 ms/frame of game GPU fence waits, so don't carry the traced balance over. For which threads end `main`'s waits, use `[kernel] occupancy = N` instead (section 1; no visible overhead).
- **Built-in runtime telemetry (`runtime/docs/CONFIG.md`):**
  - `[prof] replay_stats = N`, `[prof] gpu_stats = N`, `[prof] gpu_passes = true` (per-pass GPU timestamps, profiling only), `[nvn] out_prog_ab = 1`.
  - `[prof] sample_offsets` (supports plain function names as of `ab56dd1`).
  - Frame-budget gauge tracks game-thread guest sync, IPC, and `nvnQueueFinish` waits (`53927504`), prewarm waits are timed (`b050dee9`), and DLSS-G Reflex sleep is excluded from render-work timing with fixed hot-copy bind counts (`c155dac6`).
  - GPU breadcrumbs around residency ticks, ASTC transcodes, and texture restores (`5a1617c`, `35d810f`).
  - `tools/perf/hitch_report.py`, `hitch_detail.py`, `prof_hitch.py`.

---

## 8. Open Opportunities / Next Targets

Ordered roughly by risk-adjusted payoff:

1. **Main-thread CPU is the largest share of the Dagsion hub frame** (~8.9 of 12.5 ms, section 1), ahead of waits on Basic workers, GPU fences and GFX workers. Still missing: the same breakdown for other areas and for attacks / enemy phases, and function profiles of other scenes.
2. **`FEFW_HLE_CULL` is on by default (section 2).** A mismatch report would need a run with `=2` and its `cull_verify.log`; other maps and areas have not been compared in game.
3. **`'tregs'`: stays off.** About -2 to -3% `main` CPU with no visible fps change on a Ryzen 5 5600X, and its safety for this game is not established (section 2).
4. **Cheaper `[nvn] replay_threads = 2`:** The 26 Nov re-test (section 3) found a small fps gain for much more CPU. Try a shorter `nvn-rec` idle spin; re-test only if that removes most of the extra CPU use and keeps the gain.
5. **Target remaining worker/main lifted hotspots for `inline_replace`:** the `main` profile (section 1) is flat apart from the culling tests, so expect little from further single functions there. The import call path in the runtime (`dispatch_call`, `host_import`, `import_resolve_named`, `count_import`: ~4.7% of `main` together) can be changed without re-lifting.
   - `f_71019730e0` (MOCull NEON rasterizer — 80–84% of `MOCull_Worker1/2`). **Low priority for frame time:** in the Dagsion hub, MOCull ends only ~0.15 ms of `main`'s waits per frame (section 1); other scenes not checked.
   - `f_71005ef6e0` / `f_7100a831a0` on `Basic_Worker1-4`.
6. **Further `opt.py` codegen wins:**
   - Vector temporaries `v16..v31` (AAPCS64 caller-saved, never synced at calls/returns; ~1% static win).
   - Lazy-loading `xregs` (callee-saved `x19..x28` across calls with dirty tracking, avoiding the live-range bloat of eager loading).
7. **Render-thread replay code:** expect little fps from it in the Dagsion hub on a Ryzen 5 5600X (sections 1 and 3). Seen in the 27 Nov line breakdown and left alone: `GraphicsKey::hash` (0.8% of the thread, one chain of dependent multiplies) and the first reads of `Texture` fields in the per-draw texture checks (probably cache misses; keeping those fields together is untried). Re-profile the PGO build without `replay_stats` first.
8. **Hardware DTLB-miss check:** Run one Dagsion street capture under `perf stat` (Linux) or AMD uProf (Windows) to measure actual L2 DTLB miss rate on `main` before deciding whether guest-heap 2 MiB large pages are worth pursuing.
