# Performance routes and A/B runs

Hand-played comparisons are noisy: a different spot on the map, a camera turn or a load in one run and not the other can move the
numbers more than the change being tested. A route is a script that plays the same thing every time, unattended, and marks the
stretches to measure ("scenes"). `tools/perf/ab_run.py` runs it several times for each of two settings or builds (on both Linux and
Windows) and `tools/perf/perf_report.py` compares the logs.

Any build of `fefw.exe` + `fefw_rt.dll` works (the test runner is always compiled in), along with a `fefw.ini` and a save to load.
On Linux, `fefw.exe` runs through Steam Proton, `umu-run`, or `wine` (auto-detected by `ab_run.py`, mirroring the launcher). On
Windows, `tools/perf/ab_run.ps1` also remains available for PowerShell.

## 1. The route

`tools/perf/routes/fefw_map.tt` boots the game, presses through the title menu into save slot 000, waits for the map to settle,
then measures two scenes:

- `map_still`: 20 s with nothing moving: the cost of drawing the scene.
- `map_pan`: 11 s with the cursor panning the camera right, up, left and down: camera turns, texture streaming, culling.

Waits are in host time (`@sleep` / `@at`), not frames, so the route covers the same game time at any frame rate.

**Make it fit your save once.** Put the save you want to measure in slot 000 (a battle map with the units on it works well), then
verify the route once and check its screenshots:

```bash
# Linux (auto-detects Steam Proton -> umu-run -> wine, translates paths to Z:/...)
python3 tools/perf/ab_run.py --exe path/to/fefw.exe --ini work/play/fefw.ini \
    --route tools/perf/routes/fefw_map.tt --verify
```

Or on Windows directly:

```cmd
set NXRT_TEST_SCRIPT=C:\fefw-src\tools\perf\routes\fefw_map.tt
set NXRT_TEST_SHOT_DIR=C:\fefw-src\work\perf\shots
game-build\fefw\build\release\fefw.exe --ini C:\fefw-run\fefw.ini
set NXRT_TEST_SCRIPT=
```

The screenshots `01_title` … `05_end` show where each press landed. If the title needs another press, or the slot screen sits
elsewhere, change the `press` and `@sleep` lines (the grammar is in `runtime/include/nxrt/tt_script.h`; note that Fortune's Weave
has no `GameProbes`, so only pad/stick/key inputs, `@sleep`/`@at`, `@shot`, `@log`, and `@pass`/`@fail` apply). The run ends by
itself at `@pass`; the log says `tt: run ended: pass`.

Other routes are new `.tt` files: any number of `@log SCENE start <name>` / `@log SCENE end <name>` pairs.

### Record your own route instead

If the menus need other presses than the template's, or you want to add new scenarios, play the route yourself once and turn the
recording into a script. Use a controller (mouse movement is not carried over), and start from the launch so the recording
includes the menus:

```bash
# Linux or Windows (handles Proton/umu-run/wine + Z:/ paths automatically)
python3 tools/perf/ab_run.py --exe path/to/fefw.exe --ini work/play/fefw.ini \
    --record work/perf/rec
```

Play to the scene, then do what you want measured (stand still, sweep the camera, trigger combat, fast travel), noting roughly
when each part starts and ends (seconds from the launch), and close the window. Then:

```bash
python3 -I tools/perf/record_to_route.py work/perf/rec -o tools/perf/routes/my_route.tt \
    --scene map_still:60:80 --scene map_pan:80:95
```

Each button press and stick movement happens at the same time from the launch as in your recording (`@at`, an absolute time, so
small delays never add up), so the route repeats what you did at any frame rate. `--scene name:start:end` marks the measured parts
(seconds from the launch); without it, add the `@log SCENE start/end` lines by hand. Run it with `--route tools/perf/routes/my_route.tt`.
If you want to compare against older releases before `v0.2.7` (such as `v0.2.0`–`v0.2.6`, which don't support `@at` or
`hitch: scene`), pass `--sleep` to `record_to_route.py` to emit relative `@sleep` waits instead—`perf_report.py` automatically
buckets their 10 s hitch windows, `nvn.rstats`, and `nvn.gpustats` between the `script: SCENE start/end` log lines.

Free movement drifts a little between runs (a load half a second slower, frame timing), enough to miss a spot over a minute of
walking. For a scene in a particular place, save the game there and record only the menus, standing still, turning the camera in
place, or a short (~10–15 s) walk; on a battle map, the tile cursor repeats exactly.

The menus of a recorded route depend on load times staying about the same: keep the same save, cache and settings, and leave a few
seconds of margin before each press in the recording (don't rush through the menus).

### Testing beyond the battle map

`fefw_map.tt` only covers static map rendering and cursor panning. For pre-release regression checks, prepare a small local set of
saves (keep them under `work/`—never commit or share save files, see `LEGAL.md`) covering the major subsystems:

1. **Battle map + combat animation**: `map_still` and `map_pan`, followed by selecting a unit and starting an attack/spell so VFX,
   compute dispatches, and combat pipelines are exercised.
2. **Crowded hub / town** (e.g. Dagsion main street): `hub_load` (wrapping the Continue→world load-in), `hub_still` (draw-call /
   CPU baseline), `hub_rotate` (slow 360° right-stick sweep in place—zero positional drift), and optionally a short 10–15 s walk.
3. **Fast travel / area transition**: open the map and travel to another zone to catch memory-pool import and texture-streaming
   hitches.
4. **Cutscene / movie playback**: a save right before an AV1 cutscene to catch decoder and frame-pacing regressions.

Passing `--save-dir work/perf/my_save` to `ab_run.py` copies the save folder fresh for every run (`NXRT_FS_SAVE_DIR`), so
autosaves cannot mutate your benchmark state and every route can simply load slot 000.

## 2. What a scene measures

At `SCENE end` the log gets one line for exactly the frames between the two marks:

```
hitch: scene 'map_still': 2340 frames in 20.0 s: mean 117.0 fps, 1% low 92.3 fps, worst 14.1 ms, 0 over 25 ms, 0 over 50 ms,
       0 over 2x the median (8.40 ms); render thread ms/frame: replay 5.62, present 2.10 (limiter 1.90), idle 0.40
```

Passing `--stats 300` (which sets `[nvn] replay_stats = 300` and `[prof] gpu_stats = 300`) adds render-thread draw/descriptor stats
(`nvn.rstats`) and GPU frame times (`nvn.gpustats`). During cutscene scenes, `perf_report.py` also pulls `movie: stats` lines into
each scene's table (video fps, AV1 decode ms/packet, slowest decode ms, starved frames).

## 3. A/B runs

**Setting/toggle A/B:**

```bash
python3 tools/perf/ab_run.py --exe path/to/fefw.exe --ini work/play/fefw.ini \
    --b "NXRT_NVN_REAPER=false" --names on off
```

**Pre-release build-vs-build A/B (e.g. previous release vs RC):**

```bash
python3 tools/perf/ab_run.py --exe-a prev/fefw.exe --exe-b rc/fefw.exe \
    --ini work/play/fefw.ini --names v0.2.9 rc --stats 300 \
    --route tools/perf/routes/fefw_map.tt work/perf/routes/hub.tt
```

Key options for `tools/perf/ab_run.py`:

- `--a` / `--b`: environment variables, `NAME=value` separated by `;` (or repeated flags). Every ini key has one:
  `NXRT_<SECTION>_<KEY>` (`runtime/docs/CONFIG.md`); it overrides the ini for that run only. Omit one side for "the ini as it is".
- `{run}` in a value becomes a text new to every run (`NXRT_NVN_SPIRV_SALT={run}` forces a fresh driver shader compile each run).
- `--cold`: isolates `pipeline_cache.bin`, `pipeline_keys.bin`, and `shader_order.txt` into a fresh per-run directory and sets a
  new `NXRT_NVN_SPIRV_SALT` so you can test first-run shader/pipeline compile stutter without touching your live caches.
- `--exe-a` / `--exe-b` (and optionally `--ini-a` / `--ini-b`): compare two different builds or install directories.
- `--route r1.tt r2.tt`: run a suite of routes back to back, writing one subfolder and `report.md` per route.
- `--save-dir <dir>`: copy a benchmark save folder fresh for every run (pass `--no-copy-save` to reuse it in place).
- `--stats [N]`: enable `NXRT_NVN_REPLAY_STATS=N` and `NXRT_PROF_GPU_STATS=N` (default 300 frames) for per-scene draw/GPU numbers.
- `--runner`: Linux runner (`auto` tries Steam Proton, then `umu-run`, then `wine`; or pass `proton`, `umu-run`, `wine`, or a
  custom command). Use `--dry-run` to inspect the resolved command and `Z:/` paths before launching.
- `--runs 3` (default): runs each side three times, alternating A B, B A, A B, so drift over the session falls on both.
- Logs, screenshots and `report.md` go to `work/perf/<date-time>` (override with `--out`). `--timeout 600` stops a stuck route.

On Windows PowerShell, `tools/perf/ab_run.ps1` still works for single-exe env A/B runs:

```powershell
pwsh -File tools\perf\ab_run.ps1 -B "NXRT_NVN_REAPER=false" -Names on,off
```

## 4. The report

`report.md` has one Markdown table per scene: each metric's median over the runs of each side, the spread (min..max) when there
were several runs, and the change from A to B (marked better or worse past 3%). Above it, each run's setup (output size, DLSS mode
and model, HDR, which switches were set by the environment) and warnings: a run whose setup differs from the others, Streamline
missing, a build without the newest log lines, a route that failed.

The report also works on any logs, scripted or not:

```bash
python3 -I tools/perf/perf_report.py fefw.log
python3 -I tools/perf/perf_report.py --a a1.log a2.log --b b1.log b2.log --names off on
```

Without scenes it uses the steady 10 s windows of the hitch monitor (all but the first, worst frame under 50 ms).
