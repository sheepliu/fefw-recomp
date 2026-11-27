#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cross-platform A/B performance runner for scripted routes (docs/PERF_ROUTES.md).

Runs fefw.exe unattended several times per variant, alternating A B, B A, A B, ... so
thermal/background drift falls on both sides, then compares the logs with perf_report.py.

Works on both Linux (through Steam Proton, umu-run, or wine, with automatic Z:/ path
translation and STEAM_COMPAT_MOUNTS) and Windows.

Examples:
  # Config toggle A/B on Linux (auto-detects Steam Proton -> umu-run -> wine)
  python3 tools/perf/ab_run.py --exe path/to/fefw.exe --ini work/play/fefw.ini \\
      --b "NXRT_NVN_REAPER=false" --names on off

  # Pre-release build-vs-build regression test (previous release vs RC)
  python3 tools/perf/ab_run.py --exe-a prev/fefw.exe --exe-b rc/fefw.exe \\
      --ini work/play/fefw.ini --names v0.2.9 rc --stats 300

  # Multiple routes + isolated save copy + cold shader/pipeline cache
  python3 tools/perf/ab_run.py --exe-a prev/fefw.exe --exe-b rc/fefw.exe \\
      --ini work/play/fefw.ini --route tools/perf/routes/fefw_map.tt my_hub.tt \\
      --save-dir work/perf/bench_save --cold --runs 3

  # Record a new route (runs once, prints the record_to_route.py command on exit)
  python3 tools/perf/ab_run.py --exe path/to/fefw.exe --ini work/play/fefw.ini \\
      --record work/perf/rec

  # Verify a route once with screenshots before running a full A/B
  python3 tools/perf/ab_run.py --exe path/to/fefw.exe --ini work/play/fefw.ini \\
      --route tools/perf/routes/fefw_map.tt --verify
"""
import argparse
import datetime
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = sys.platform == 'win32'
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Path translation and Linux runner discovery (mirrors launcher/src-tauri/src/play.rs)
# ---------------------------------------------------------------------------

def to_game_path(p: Path) -> str:
    """Host path as fefw.exe (a Windows binary) sees it: native on Windows, Z:/... on Linux."""
    s = str(p.resolve())
    if IS_WIN:
        return s
    return 'Z:' + s.replace('\\', '/')


def _vdf_values(text: str, key: str) -> list[str]:
    quoted = f'"{key}"'
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith(quoted):
            continue
        rest = s[len(quoted):].strip()
        if len(rest) >= 2 and rest[0] == '"' and rest[-1] == '"':
            out.append(rest[1:-1].replace('\\\\', '\\'))
    return out


def _steam_libraries() -> tuple[Path, list[Path]] | None:
    home = os.environ.get('HOME')
    if not home:
        return None
    home_p = Path(home)
    candidates = [
        '.local/share/Steam',
        '.steam/steam',
        '.steam/root',
        '.var/app/com.valvesoftware.Steam/.local/share/Steam',
    ]
    for rel in candidates:
        root = home_p / rel
        if not root.exists():
            continue
        try:
            root = root.resolve(strict=True)
        except OSError:
            continue
        vdf = root / 'steamapps' / 'libraryfolders.vdf'
        if not vdf.is_file():
            continue
        text = vdf.read_text(encoding='utf-8', errors='replace')
        libs: list[Path] = []
        for raw in [str(root)] + _vdf_values(text, 'path'):
            try:
                lib = Path(raw).resolve(strict=True)
            except OSError:
                continue
            if (lib / 'steamapps').is_dir() and lib not in libs:
                libs.append(lib)
        return root, libs
    return None


def _pick_proton(libs: list[Path]) -> Path | None:
    override = os.environ.get('PROTON_DIR')
    if override:
        p = Path(override)
        return p if (p / 'proton').is_file() else None

    def rank(name: str) -> tuple[int, list[int]]:
        if 'Experimental' in name:
            return (2, [])
        nums = [int(x) for x in re.findall(r'\d+', name)]
        return (1 if nums else 0, nums)

    found: list[tuple[str, Path]] = []
    for lib in libs:
        common = lib / 'steamapps' / 'common'
        if not common.is_dir():
            continue
        for entry in common.iterdir():
            if entry.name.startswith('Proton') and (entry / 'proton').is_file():
                found.append((entry.name, entry))
    if not found:
        return None
    found.sort(key=lambda pair: rank(pair[0]))
    return found[-1][1]


def _steam_proton_words() -> list[str] | None:
    res = _steam_libraries()
    if not res:
        return None
    root, libs = res
    pdir = _pick_proton(libs)
    if not pdir:
        return None
    xdg = os.environ.get('XDG_DATA_HOME')
    if xdg and os.path.isabs(xdg):
        base = Path(xdg)
    else:
        home = os.environ.get('HOME')
        if not home:
            return None
        base = Path(home) / '.local' / 'share'
    data = base / 'fefw-recomp' / 'proton'
    data.mkdir(parents=True, exist_ok=True)
    words = [
        f'STEAM_COMPAT_DATA_PATH={data}',
        f'STEAM_COMPAT_CLIENT_INSTALL_PATH={root}',
    ]
    manifest_p = pdir / 'toolmanifest.vdf'
    manifest = manifest_p.read_text(encoding='utf-8', errors='replace') if manifest_p.is_file() else ''
    appids = _vdf_values(manifest, 'require_tool_appid')
    if appids:
        appid = appids[0]
        for lib in libs:
            acf_p = lib / 'steamapps' / f'appmanifest_{appid}.acf'
            if not acf_p.is_file():
                continue
            acf = acf_p.read_text(encoding='utf-8', errors='replace')
            dirs = _vdf_values(acf, 'installdir')
            if not dirs:
                continue
            entry = lib / 'steamapps' / 'common' / dirs[0] / '_v2-entry-point'
            if entry.is_file():
                words.extend([str(entry), '--verb=waitforexitandrun', '--'])
                break
    words.extend([str(pdir / 'proton'), 'waitforexitandrun'])
    return words


def resolve_runner(spec: str) -> list[str]:
    """Return runner words (optional leading KEY=VAL env pairs followed by executable + args)."""
    if IS_WIN:
        return []
    spec = (spec or 'auto').strip()
    if spec == 'auto':
        w = _steam_proton_words()
        if w:
            return w
        for name in ('umu-run', 'wine'):
            found = shutil.which(name)
            if found:
                return [found]
        raise SystemExit(
            'No Linux runner found for fefw.exe. Install Steam + Proton (Experimental), '
            'umu-run, or wine, or pass --runner "<command>".'
        )
    if spec == 'proton':
        w = _steam_proton_words()
        if not w:
            raise SystemExit('Steam Proton was not found (set PROTON_DIR or pass a custom --runner).')
        return w
    if spec in ('umu', 'umu-run'):
        found = shutil.which('umu-run')
        if not found:
            raise SystemExit('umu-run not found on PATH.')
        return [found]
    if spec == 'wine':
        found = shutil.which('wine')
        if not found:
            raise SystemExit('wine not found on PATH.')
        return [found]
    return shlex.split(spec)


def _split_runner(words: list[str]) -> tuple[dict[str, str], list[str]]:
    env: dict[str, str] = {}
    i = 0
    while i < len(words):
        w = words[i]
        if '=' in w:
            k, v = w.split('=', 1)
            if k and '/' not in k:
                env[k] = v
                i += 1
                continue
        break
    return env, words[i:]


def _is_umu(cmd_words: list[str]) -> bool:
    it = iter(cmd_words)
    in_env = False
    for w in it:
        if not in_env and w == 'env':
            in_env = True
            continue
        if in_env and w in ('-u', '--unset'):
            next(it, None)
            continue
        if not (in_env and w.startswith('-')) and '=' not in w:
            return Path(w).name == 'umu-run'
    return False


def _ini_linux_dirs(ini_path: Path) -> list[Path]:
    dirs = [ini_path.parent.resolve()]
    if not ini_path.is_file():
        return dirs
    sec = ''
    want = {
        ('fs', 'romfs'): False,
        ('fs', 'exefs'): False,
        ('fs', 'mods'): False,
        ('fs', 'save_dir'): False,
        ('aoc', 'dir'): False,
        ('nvn', 'shader_cache'): True,
        ('nvn', 'bc7_cache'): False,
    }
    for raw in ini_path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = raw.strip()
        if not line or line[0] in ';#':
            continue
        if line.startswith('[') and line.endswith(']'):
            sec = line[1:-1].strip().lower()
            continue
        if '=' not in line:
            continue
        k, v = (x.strip() for x in line.split('=', 1))
        key = (sec, k.lower())
        if key not in want:
            continue
        p = v.replace('\\', '/')
        if p.startswith(('Z:', 'z:')):
            p = p[2:]
        if p.startswith('/'):
            path = Path(p)
            dirs.append(path.parent if want[key] else path)
    return dirs


def _container_mounts(dirs: list[Path]) -> str | None:
    home = Path(os.environ['HOME']).resolve() if os.environ.get('HOME') else None
    shared = (Path('/mnt'), Path('/media'), Path('/run/media'))
    kept: list[Path] = []
    for d in dirs:
        try:
            d = d.resolve()
        except OSError:
            pass
        s = str(d)
        if not s.startswith('/') or ':' in s:
            continue
        if any(d == sh or sh in d.parents for sh in shared):
            continue
        if home is not None and (d == home or home in d.parents):
            continue
        if any(d == k or k in d.parents for k in kept):
            continue
        kept = [k for k in kept if not (k == d or d in k.parents)]
        kept.append(d)
    return ':'.join(str(p) for p in kept) if kept else None


# ---------------------------------------------------------------------------
# Variant & execution helpers
# ---------------------------------------------------------------------------

def parse_vars(specs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for spec in specs or []:
        for part in spec.split(';'):
            p = part.strip()
            if not p:
                continue
            if '=' not in p:
                raise SystemExit(f"Invalid env spec (expected NAME=value): '{p}'")
            k, v = p.split('=', 1)
            k = k.strip()
            if not k:
                raise SystemExit(f"Invalid env spec (empty name): '{p}'")
            out[k] = v.strip()
    return out


def build_launch(
    exe: Path,
    ini: Path,
    log: Path,
    runner_words: list[str],
    extra_env: dict[str, str],
    mount_dirs: list[Path],
) -> tuple[list[str], dict[str, str], Path]:
    run_dir = ini.parent.resolve()
    env = os.environ.copy()
    runner_env, runner_cmd = _split_runner(runner_words)
    env.update(runner_env)
    env.update(extra_env)

    # Clear Python host vars that shouldn't leak into Proton/umu-run if running from a venv
    if not IS_WIN:
        if 'STEAM_COMPAT_MOUNTS' not in env:
            dirs = _ini_linux_dirs(ini) + [exe.parent.resolve(), log.parent.resolve()] + mount_dirs
            m = _container_mounts(dirs)
            if m:
                env['STEAM_COMPAT_MOUNTS'] = m
        if _is_umu(runner_cmd) and 'GAMEID' not in env:
            env['GAMEID'] = '0'

    # Provide NXRT_LAUNCHER_DIR if upscalers/ lives next to exe or run_dir and caller did not set it
    if 'NXRT_LAUNCHER_DIR' not in env:
        for cand in (exe.parent.resolve(), run_dir):
            if (cand / 'upscalers').is_dir():
                env['NXRT_LAUNCHER_DIR'] = to_game_path(cand)
                break

    args = [str(exe.resolve()), '--ini', to_game_path(ini), '--log', to_game_path(log)]
    cmd = runner_cmd + args
    return cmd, env, run_dir


def run_process(cmd: list[str], env: dict[str, str], cwd: Path, timeout_s: int | None) -> tuple[int | None, float]:
    t0 = time.monotonic()
    popen_kwargs: dict = {'cwd': str(cwd), 'env': env}
    if not IS_WIN:
        popen_kwargs['start_new_session'] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)
    try:
        rc = proc.wait(timeout=timeout_s if timeout_s and timeout_s > 0 else None)
        return rc, time.monotonic() - t0
    except subprocess.TimeoutExpired:
        if IS_WIN:
            proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return None, time.monotonic() - t0
    except KeyboardInterrupt:
        if IS_WIN:
            proc.terminate()
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()
        raise


def default_exe() -> Path | None:
    cand = REPO_ROOT / 'game-build' / 'fefw' / 'build' / 'release' / 'fefw.exe'
    return cand if cand.is_file() else None


def default_ini() -> Path | None:
    for cand in (REPO_ROOT / 'work' / 'play' / 'fefw.ini', Path(r'C:\fefw-run\fefw.ini')):
        if cand.is_file():
            return cand
    return None


def prepare_save_copy(src: Path, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return dest


def run_report(logs_a: list[Path], logs_b: list[Path], names: list[str], report_path: Path) -> None:
    rep_script = SCRIPT_DIR / 'perf_report.py'
    base_args = [
        sys.executable, '-I', str(rep_script),
        '--a', *[str(p) for p in logs_a],
        '--b', *[str(p) for p in logs_b],
        '--names', names[0], names[1],
    ]
    # Print readable plain-text table to stdout
    subprocess.run(base_args, check=False)
    # Write GitHub Markdown tables to report.md
    md = subprocess.run(base_args + ['--markdown'], capture_output=True, text=True, check=False)
    report_path.write_text(md.stdout, encoding='utf-8')
    print(f'report: {report_path}')


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--a', action='append', default=[], help='Env overrides for A (NAME=val;NAME2=val2; repeatable)')
    ap.add_argument('--b', action='append', default=[], help='Env overrides for B (NAME=val;NAME2=val2; repeatable)')
    ap.add_argument('--names', nargs=2, default=['A', 'B'], metavar=('NAME_A', 'NAME_B'), help='Labels for the two sides')
    ap.add_argument('--runs', type=int, default=3, help='Runs per variant (default 3, alternating A B, B A, ...)')
    ap.add_argument('--exe', type=Path, default=None, help='Path to fefw.exe (shared by A and B unless --exe-a/--exe-b set)')
    ap.add_argument('--exe-a', type=Path, default=None, help='Path to fefw.exe for variant A (e.g. previous release)')
    ap.add_argument('--exe-b', type=Path, default=None, help='Path to fefw.exe for variant B (e.g. release candidate)')
    ap.add_argument('--ini', type=Path, default=None, help='Path to fefw.ini (shared unless --ini-a/--ini-b set)')
    ap.add_argument('--ini-a', type=Path, default=None, help='Path to fefw.ini for variant A')
    ap.add_argument('--ini-b', type=Path, default=None, help='Path to fefw.ini for variant B')
    ap.add_argument('--route', type=Path, nargs='+', default=None, help='One or more .tt route scripts (default: tools/perf/routes/fefw_map.tt)')
    ap.add_argument('--out', type=Path, default=None, help='Output directory (default: work/perf/<timestamp>)')
    ap.add_argument('--timeout', type=int, default=600, help='Per-run timeout in seconds (default 600, 0 = none)')
    ap.add_argument('--runner', default='auto', help='Linux runner: auto (Steam Proton -> umu-run -> wine), proton, umu-run, wine, or custom command')
    ap.add_argument('--save-dir', type=Path, default=None, help='Benchmark save folder; copied fresh per run into NXRT_FS_SAVE_DIR unless --no-copy-save')
    ap.add_argument('--no-copy-save', action='store_true', help='Use --save-dir directly without copying per run')
    ap.add_argument('--cold', action='store_true', help='Cold shader/pipeline cache per run (isolated pipeline_cache/keys/order + fresh spirv_salt)')
    ap.add_argument('--stats', type=int, nargs='?', const=300, default=0, metavar='FRAMES',
                    help='Enable per-scene [nvn] replay_stats and [prof] gpu_stats every N frames (default 300 when flag passed)')
    ap.add_argument('--verify', action='store_true', help='Run each route once on variant A to inspect logs and screenshots')
    ap.add_argument('--record', type=Path, default=None, metavar='REC_DIR',
                    help='Launch the game once in input-recording mode (saves input.nxin + trace.jsonl into REC_DIR)')
    ap.add_argument('--dry-run', action='store_true', help='Print the resolved commands and env overrides without running fefw.exe')
    args = ap.parse_args()

    # Resolve executables and inis
    base_exe = args.exe or default_exe()
    exe_a = args.exe_a or base_exe
    exe_b = args.exe_b or base_exe
    if not exe_a or not exe_b:
        ap.error('Provide --exe (or both --exe-a and --exe-b).')
    for label, p in (('exe-a', exe_a), ('exe-b', exe_b)):
        if not p.is_file():
            ap.error(f'{label} not found: {p}')

    base_ini = args.ini or default_ini()
    ini_a = args.ini_a or base_ini
    ini_b = args.ini_b or base_ini
    if not ini_a or not ini_b:
        ap.error('Provide --ini (or both --ini-a and --ini-b).')
    for label, p in (('ini-a', ini_a), ('ini-b', ini_b)):
        if not p.is_file():
            ap.error(f'{label} not found: {p}')

    if args.save_dir and not args.save_dir.is_dir():
        ap.error(f'--save-dir not found: {args.save_dir}')

    runner_words = resolve_runner(args.runner)
    vars_a = parse_vars(args.a)
    vars_b = parse_vars(args.b)

    # --- Recording helper mode ------------------------------------------------
    if args.record is not None:
        rec_dir = args.record.resolve()
        rec_dir.mkdir(parents=True, exist_ok=True)
        log_path = rec_dir / 'record.log'
        extra = dict(vars_a)
        extra['NXRT_LOG'] = to_game_path(log_path)
        extra['NXRT_TEST_RECORD'] = to_game_path(rec_dir / 'input.nxin')
        extra['NXRT_TEST_OUT'] = to_game_path(rec_dir)
        extra.setdefault('NXRT_TEST_FRAME_SOURCE', 'tick')  # needed on builds before v0.2.7 (372f485)
        mount_dirs = [rec_dir]
        if args.save_dir:
            save_use = args.save_dir.resolve() if args.no_copy_save else prepare_save_copy(args.save_dir.resolve(), rec_dir / 'save_copy')
            extra['NXRT_FS_SAVE_DIR'] = to_game_path(save_use)
            mount_dirs.append(save_use)
        cmd, env, cwd = build_launch(exe_a, ini_a, log_path, runner_words, extra, mount_dirs)
        print(f'runner: {" ".join(runner_words) or "(native)"}')
        print(f'exe:    {exe_a.resolve()}')
        print(f'ini:    {ini_a.resolve()}')
        print(f'rec:    {rec_dir}')
        if args.dry_run:
            print('cmd:   ', cmd)
            print('env:   ', {k: v for k, v in extra.items()})
            return 0
        print('Play your route with a controller, note scene start/end seconds from launch, then close the window.')
        rc, secs = run_process(cmd, env, cwd, timeout_s=0)
        print(f'recording ended (exit {rc} after {secs:.0f} s)')
        print('Convert to a route with:')
        print(f'  python3 tools/perf/record_to_route.py {rec_dir} -o tools/perf/routes/my_route.tt --scene name:start_s:end_s')
        return 0 if rc == 0 else 1

    # --- Route(s) & output directory ------------------------------------------
    routes = args.route if args.route else [SCRIPT_DIR / 'routes' / 'fefw_map.tt']
    for r in routes:
        if not r.is_file():
            ap.error(f'route not found: {r}')
    routes = [r.resolve() for r in routes]

    ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    out_root = (args.out or (REPO_ROOT / 'work' / 'perf' / ts)).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    print(f'runner: {" ".join(runner_words) or "(native)"}')
    if exe_a.resolve() == exe_b.resolve():
        print(f'exe:    {exe_a.resolve()}')
    else:
        print(f'exe {args.names[0]}: {exe_a.resolve()}')
        print(f'exe {args.names[1]}: {exe_b.resolve()}')
    if ini_a.resolve() == ini_b.resolve():
        print(f'ini:    {ini_a.resolve()}')
    else:
        print(f'ini {args.names[0]}: {ini_a.resolve()}')
        print(f'ini {args.names[1]}: {ini_b.resolve()}')
    print(f'routes: {", ".join(str(r) for r in routes)}')
    print(f'out:    {out_root}')
    if args.save_dir:
        print(f'save:   {args.save_dir.resolve()} ({"in place" if args.no_copy_save else "fresh copy per run"})')
    if args.cold:
        print('cold:   isolated pipeline_cache/keys/order + fresh NXRT_NVN_SPIRV_SALT per run')
    if args.stats:
        print(f'stats:  replay_stats={args.stats}, gpu_stats={args.stats}')
    print(f'{args.names[0]}: {vars_a or "(ini defaults)"}')
    if not args.verify:
        print(f'{args.names[1]}: {vars_b or "(ini defaults)"}')
        print(f'{args.runs} runs each per route, alternating; do not touch the game window while it runs.')
    else:
        print('verify mode: running each route once on variant A.')

    variants = [
        (args.names[0], exe_a.resolve(), ini_a.resolve(), vars_a),
        (args.names[1], exe_b.resolve(), ini_b.resolve(), vars_b),
    ]
    all_var_keys = set(vars_a) | set(vars_b)

    multi_route = len(routes) > 1
    for route in routes:
        route_out = (out_root / route.stem) if multi_route else out_root
        route_out.mkdir(parents=True, exist_ok=True)
        if multi_route:
            print(f'\n=== route: {route.name} ===')

        logs: list[list[Path]] = [[], []]
        run_count = 1 if args.verify else args.runs
        for r_idx in range(1, run_count + 1):
            order = [0] if args.verify else ([0, 1] if r_idx % 2 == 1 else [1, 0])
            for v in order:
                vname, vexe, vini, vvars = variants[v]
                tag = f'{vname}-{r_idx}-{datetime.datetime.now().strftime("%H%M%S%f")[:9]}'
                log_path = route_out / f'{vname}_{r_idx}.log'
                tt_out = route_out / f'tt_{vname}_{r_idx}'
                shots_dir = route_out / f'shots_{vname}_{r_idx}'
                mount_dirs = [route.parent, route_out]

                # Clear variables set only by the other variant so host env cannot bleed across
                extra: dict[str, str] = {}
                for k, val in vvars.items():
                    extra[k] = val.replace('{run}', tag)
                if args.stats:
                    extra.setdefault('NXRT_NVN_REPLAY_STATS', str(args.stats))
                    extra.setdefault('NXRT_PROF_GPU_STATS', str(args.stats))
                if args.cold:
                    cold_dir = route_out / f'cold_{vname}_{r_idx}'
                    cold_dir.mkdir(parents=True, exist_ok=True)
                    extra.setdefault('NXRT_NVN_PIPELINE_CACHE', to_game_path(cold_dir / 'pipeline_cache.bin'))
                    extra.setdefault('NXRT_NVN_PIPELINE_KEYS', to_game_path(cold_dir / 'pipeline_keys.bin'))
                    extra.setdefault('NXRT_NVN_SHADER_ORDER', to_game_path(cold_dir / 'shader_order.txt'))
                    extra.setdefault('NXRT_NVN_SPIRV_SALT', tag)
                if args.save_dir:
                    if args.no_copy_save:
                        save_use = args.save_dir.resolve()
                    else:
                        save_use = prepare_save_copy(args.save_dir.resolve(), route_out / f'save_{vname}_{r_idx}')
                    extra['NXRT_FS_SAVE_DIR'] = to_game_path(save_use)
                    mount_dirs.append(save_use)

                extra['NXRT_LOG'] = to_game_path(log_path)
                extra['NXRT_TEST_SCRIPT'] = to_game_path(route)
                extra['NXRT_TEST_OUT'] = to_game_path(tt_out)
                extra['NXRT_TEST_SHOT_DIR'] = to_game_path(shots_dir)
                extra.setdefault('NXRT_TEST_FRAME_SOURCE', 'tick')  # needed on builds before v0.2.7 (372f485)
                extra.setdefault('NXRT_DIAG_WATCHDOG_SECONDS', '30')  # headroom for Proton boot/preload before frame 0

                cmd, env, cwd = build_launch(vexe, vini, log_path, runner_words, extra, mount_dirs)
                # Ensure env keys belonging solely to the other side are unset
                for k in all_var_keys:
                    if k not in vvars:
                        env.pop(k, None)

                print(f'run {r_idx}/{run_count}: {vname} ...', end='', flush=True)
                if args.dry_run:
                    print(' (dry-run)')
                    print('  cwd:', cwd)
                    print('  cmd:', cmd)
                    print('  env:', {k: v for k, v in sorted(extra.items())})
                    continue

                rc, secs = run_process(cmd, env, cwd, timeout_s=args.timeout)
                if rc is None:
                    print(f' stopped after {args.timeout} s (check screenshots in {shots_dir})')
                    continue
                print(f' exit {rc} after {secs:.0f} s')
                if rc != 0:
                    if args.verify:
                        print(f'WARNING: route did not pass (exit {rc}); check tt lines in {log_path} and {shots_dir}')
                        logs[v].append(log_path)
                    else:
                        print(f'WARNING: route did not pass (exit {rc}); excluding {log_path.name} from report (check tt lines and {shots_dir})')
                else:
                    logs[v].append(log_path)

        if args.dry_run:
            continue
        if args.verify:
            if logs[0]:
                subprocess.run([sys.executable, '-I', str(SCRIPT_DIR / 'perf_report.py'), str(logs[0][0])], check=False)
                print(f'screenshots: {route_out / f"shots_{args.names[0]}_1"}')
        elif logs[0] and logs[1]:
            run_report(logs[0], logs[1], args.names, route_out / 'report.md')
        else:
            print('WARNING: not enough finished runs to compare')

    return 0


if __name__ == '__main__':
    sys.exit(main())
