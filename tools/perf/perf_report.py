# SPDX-License-Identifier: GPL-3.0-or-later
"""Performance summary of fefw.log files, and an A/B comparison of two sets of runs.

    python -I tools/perf/perf_report.py run.log [run2.log ...]                  one summary per log
    python -I tools/perf/perf_report.py --a a1.log a2.log --b b1.log b2.log     A against B (median of each side's runs)
             [--names off on] [--markdown]

What it reads (all from the log, nothing else):
  - scenes: `hitch: scene '<name>': ...` lines, written by a scripted route's `@log SCENE start/end <name>` (docs/PERF_ROUTES.md):
    exact frame statistics of that window, compared scene by scene;
  - without scenes: the steady 10 s windows of the hitch monitor (`hitch: N frames in ...`: every window after the first whose worst
    frame is under 50 ms), and inside their time span the `nvn.rstats` lines ([nvn] replay_stats) and `nvn.gpustats` lines
    ([prof] gpu_stats) when the run had them;
  - the setup of each run (output size, DLSS mode and model, HDR, the build's switches that log themselves), with warnings when the
    runs of a comparison differ in setup or a run looks like an old build.
"""
import argparse
import re
import statistics
import sys

TS = re.compile(r'^\[\s*([0-9.]+) ')

SCENE_BEGIN = re.compile(r"hitch: scene '([^']*)' begins at frame (\d+)")
SCENE = re.compile(r"hitch: scene '([^']*)': (\d+) frames in ([0-9.]+) s(?: \(tail of a longer scene\))?: mean ([0-9.]+) fps, 1% low ([0-9.]+) fps, worst ([0-9.]+) ms, "
                   r"(\d+) over 25 ms, (\d+) over 50 ms, (\d+) over 2x the median \(([0-9.]+) ms\); render thread ms/frame: replay ([0-9.]+), "
                   r"present ([0-9.]+) \(limiter ([0-9.]+)\), idle ([0-9.]+)")
SCRIPT_SCENE_START = re.compile(r"script: SCENE start (.+)$")
SCRIPT_SCENE_END = re.compile(r"script: SCENE end (.+)$")
SLOW_FRAME = re.compile(r"hitch: frame \d+ took ([0-9.]+) ms \(t=([0-9.]+) s\)")
WINDOW = re.compile(r"hitch: (\d+) frames in ([0-9.]+) s: mean ([0-9.]+) fps, 1% low ([0-9.]+) fps, worst ([0-9.]+) ms, (\d+) over 2x the median(?: \(([0-9.]+) ms\))?")
RSTATS = re.compile(r"nvn\.rstats: (\d+) frames: replay ([0-9.]+) ms/frame = draw setup ([0-9.]+) .*?\| (\d+) draws/frame.*?per draw us:.*? sets ([0-9.]+)")
GPU = re.compile(r"nvn\.gpustats: (\d+) frames: GPU ms/frame avg ([0-9.]+) p50 ([0-9.]+) p90 ([0-9.]+) p99 ([0-9.]+)")
MOVIE = re.compile(
    r"movie: stats: packets (\d+), pictures (\d+), frames acquired (\d+), released (\d+)"
    r"(?:, empty \d+ \(starved (\d+)\), skipped (\d+))?"
    r"; decode ([0-9.]+) ms per packet \(slowest ([0-9.]+)\) over (\d+)"
)

SETUP = [
    ('build', re.compile(r'host symbols: 0 from the linker map \(no map'), lambda m: 'no linker map (a packaged build?)'),
    ('build', re.compile(r'host symbols: \d+ from the linker map (.*)$'), lambda m: m.group(1).strip()),
    ('cpu build', re.compile(r'boot: fefw\.exe build: (\S+)'), lambda m: m.group(1)),
    ('output', re.compile(r'fefw: render config 0: output (\d+x\d+) from (\d+x\d+)'), lambda m: f'{m.group(1)} from {m.group(2)}'),
    ('hdr', re.compile(r'gfx: HDR: mode (\d+), scene (\w+), wanted output (\w+)'), lambda m: f'{m.group(3)} (scene {m.group(2)})'),
    ('upscaler', re.compile(r'nvn\.dlss: network \d+: first evaluation, ([^:]+):'), lambda m: m.group(1).strip()),
    ('dlss model', re.compile(r'dlss: (?:DLSS|DLAA): mode (\w+), model preset (\w+)'), lambda m: f'{m.group(1)}, preset {m.group(2)}'),
    ('float add', re.compile(r'float adds to buffers: (.*)$'), lambda m: m.group(1).split(' (')[0]),
    ('reaper', re.compile(r'reaper: Vulkan objects'), lambda m: 'on'),
    ('ultrawide', re.compile(r'fefw: ultrawide: output (\d+x\d+)'), lambda m: m.group(1)),
]
WARN = [
    (re.compile(r'sl\.interposer\.dll is missing'), 'Streamline DLLs missing: DLSS did not run'),
    (re.compile(r'out of video memory|VK_ERROR_OUT_OF_DEVICE_MEMORY'), 'ran out of video memory'),
    (re.compile(r'run ended: (fail|error)'), 'the scripted route failed (see the tt lines)'),
]
CONFIG = re.compile(r'config: \[(\w+)\] (\w+) = (.*) \((env|ini)\)$')


def parse(path):
    r = {'path': path, 'setup': {}, 'scenes': {}, 'script_scenes': {}, 'windows': [], 'slow': [], 'rstats': [], 'gpu': [], 'movie': [], 'warn': [], 'overrides': {}, 'end': 0.0}
    scene_starts = {}
    script_starts = {}
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = TS.match(line)
            t = float(m.group(1)) if m else r['end']
            r['end'] = max(r['end'], t)
            if 'script: SCENE ' in line:
                ms = SCRIPT_SCENE_START.search(line)
                if ms:
                    script_starts[ms.group(1).strip()] = t
                    continue
                me = SCRIPT_SCENE_END.search(line)
                if me:
                    name = me.group(1).strip()
                    if name in script_starts:
                        r['script_scenes'].setdefault(name, []).append((script_starts.pop(name), t))
                    continue
            if 'hitch: frame ' in line:
                mf = SLOW_FRAME.search(line)
                if mf:
                    r['slow'].append((float(mf.group(2)), float(mf.group(1))))
                    continue
            if 'hitch: scene' in line:
                mb = SCENE_BEGIN.search(line)
                if mb:
                    scene_starts[mb.group(1)] = t
                    continue
                m = SCENE.search(line)
                if m:
                    g = m.groups()
                    secs = float(g[2])
                    t0 = scene_starts.pop(g[0], max(0.0, t - secs))
                    r['scenes'].setdefault(g[0], []).append({
                        't0': t0, 't1': t,
                        'frames': int(g[1]), 'secs': secs, 'fps': float(g[3]), 'low1': float(g[4]), 'worst': float(g[5]),
                        'over25': int(g[6]), 'over50': int(g[7]), 'over2x': int(g[8]), 'median_ms': float(g[9]),
                        'replay_ms': float(g[10]), 'present_ms': float(g[11]), 'limiter_ms': float(g[12]), 'idle_ms': float(g[13])})
                continue
            m = WINDOW.search(line)
            if m:
                w = {'t': t, 'frames': int(m.group(1)), 'secs': float(m.group(2)), 'fps': float(m.group(3)),
                     'low1': float(m.group(4)), 'worst': float(m.group(5)), 'over2x': int(m.group(6))}
                if m.group(7) is not None:
                    w['median_ms'] = float(m.group(7))
                r['windows'].append(w)
                continue
            m = RSTATS.search(line)
            if m:
                prev_t = r['rstats'][-1]['t'] if r['rstats'] else None
                r['rstats'].append({'t': t, 'prev_t': prev_t, 'frames': int(m.group(1)), 'replay_ms': float(m.group(2)),
                                    'setup_ms': float(m.group(3)), 'draws': int(m.group(4)), 'sets_us': float(m.group(5))})
                continue
            m = GPU.search(line)
            if m:
                prev_t = r['gpu'][-1]['t'] if r['gpu'] else None
                r['gpu'].append({'t': t, 'prev_t': prev_t, 'frames': int(m.group(1)), 'avg': float(m.group(2)),
                                 'p50': float(m.group(3)), 'p99': float(m.group(5))})
                continue
            m = MOVIE.search(line)
            if m:
                prev = r['movie'][-1] if r['movie'] else None
                acq = int(m.group(3))
                starved = int(m.group(5)) if m.group(5) is not None else None
                dt = (t - prev['t']) if prev and 0.0 < t - prev['t'] <= 10.0 and acq >= prev['acq'] else None
                dacq = (acq - prev['acq']) if dt is not None else None
                dstarved = (starved - prev['starved']) if (dt is not None and starved is not None and prev['starved'] is not None and starved >= prev['starved']) else None
                r['movie'].append({
                    't': t, 'prev_t': prev['t'] if dt is not None else None,
                    'acq': acq, 'dt': dt, 'dacq': dacq,
                    'starved': starved, 'dstarved': dstarved,
                    'decode_ms': float(m.group(7)), 'slowest_ms': float(m.group(8)), 'over': int(m.group(9)),
                })
                continue
            m = CONFIG.search(line)
            if m:
                r['overrides'][f'[{m.group(1)}] {m.group(2)}'] = f'{m.group(3)} ({m.group(4)})'
            for key, rx, fmt in SETUP:
                if key not in r['setup']:
                    m = rx.search(line)
                    if m:
                        r['setup'][key] = fmt(m)
            for rx, msg in WARN:
                if rx.search(line) and msg not in r['warn']:
                    r['warn'].append(msg)
    if 'float add' not in r['setup']:
        r['warn'].append('no "float adds to buffers" line: a build from before PR #89 (or the log was cut short)')
    if not r['scenes'] and r['script_scenes']:
        r['warn'].append('scenes approximated from 10 s hitch windows (build before v0.2.7 without hitch_scene)')
    return r


def steady(r):
    """The steady part of a run: hitch windows after the first with the worst frame under 50 ms, and the stats lines inside them."""
    win = [w for i, w in enumerate(r['windows']) if i > 0 and w['worst'] < 50.0]
    out = {'windows': len(win)}
    if not win:
        return out
    frames = sum(w['frames'] for w in win)
    secs = sum(w['secs'] for w in win)
    out['fps'] = frames / secs if secs else 0
    out['low1'] = statistics.median(w['low1'] for w in win)
    out['worst'] = max(w['worst'] for w in win)
    spans = [(w['t'] - w['secs'], w['t']) for w in win]
    inside = lambda t: any(a - 1.0 <= t <= b + 1.0 for a, b in spans)
    rs = [s for s in r['rstats'] if inside(s['t'])]
    if rs:
        out['replay_ms'] = statistics.mean(s['replay_ms'] for s in rs)
        out['draws'] = statistics.mean(s['draws'] for s in rs)
        out['replay_per_1k'] = statistics.mean(1000.0 * s['replay_ms'] / s['draws'] for s in rs if s['draws'])
        out['sets_us'] = statistics.mean(s['sets_us'] for s in rs)
    gp = [g for g in r['gpu'] if inside(g['t'])]
    if gp:
        out['gpu_ms'] = statistics.mean(g['avg'] for g in gp)
        out['gpu_p99'] = statistics.mean(g['p99'] for g in gp)
    return out


METRICS = [   # key, label, better (+1 higher, -1 lower), format
    ('fps', 'mean fps', 1, '{:.1f}'),
    ('low1', '1% low fps', 1, '{:.1f}'),
    ('worst', 'worst frame ms', -1, '{:.1f}'),
    ('over25', 'frames over 25 ms', -1, '{:.0f}'),
    ('over50', 'frames over 50 ms', -1, '{:.0f}'),
    ('median_ms', 'median frame ms', -1, '{:.2f}'),
    ('replay_ms', 'render thread replay ms/frame', -1, '{:.2f}'),
    ('replay_per_1k', 'replay ms per 1000 draws', -1, '{:.2f}'),
    ('sets_us', 'descriptor sets us/draw', -1, '{:.2f}'),
    ('draws', 'draws/frame', 0, '{:.0f}'),
    ('gpu_ms', 'GPU ms/frame', -1, '{:.2f}'),
    ('gpu_p99', 'GPU ms/frame p99', -1, '{:.2f}'),
    ('movie_fps', 'video fps', 1, '{:.1f}'),
    ('movie_decode_ms', 'video decode ms/pkt', -1, '{:.2f}'),
    ('movie_decode_max', 'video slowest decode ms', -1, '{:.1f}'),
    ('movie_starved', 'video starved frames', -1, '{:.0f}'),
    ('windows', 'steady 10 s windows', 0, '{:.0f}'),
]


def _in_spans(entry, spans, fallback_fps):
    """True when at least half of an rstats/gpustats sample's time span overlaps one of `spans`."""
    t1 = entry['t']
    fps = fallback_fps if fallback_fps > 1.0 else 60.0
    est_dur = entry['frames'] / fps
    prev = entry.get('prev_t')
    if prev is not None and 0.0 < t1 - prev <= max(2.0 * est_dur, 1.0):
        t0 = prev
    else:
        t0 = max(prev if prev is not None else 0.0, t1 - est_dur)
    dur = t1 - t0
    if dur <= 0.0:
        return any(a <= t1 <= b + 0.5 for a, b in spans)
    for a, b in spans:
        ov = min(t1, b) - max(t0, a)
        if ov >= 0.5 * dur:
            return True
    return False


def _win_in_spans(w, spans, min_frac=0.5):
    t1 = w['t']
    t0 = max(0.0, t1 - w['secs'])
    dur = t1 - t0
    if dur <= 0.0:
        return any(a <= t1 <= b for a, b in spans)
    for a, b in spans:
        ov = min(t1, b) - max(t0, a)
        if ov >= min_frac * dur:
            return True
    return False


def _movie_in_spans(entry, spans):
    t1 = entry['t']
    t0 = entry['prev_t'] if entry['prev_t'] is not None else max(0.0, t1 - 5.0)
    dur = t1 - t0
    if dur <= 0.0:
        return any(a <= t1 <= b + 0.5 for a, b in spans)
    for a, b in spans:
        if min(t1, b) - max(t0, a) >= 0.5 * dur:
            return True
    return False


def _attach_nvn_stats(m, r, spans, fps, include_replay_ms=False):
    rs = [s for s in r['rstats'] if _in_spans(s, spans, fps)]
    if rs:
        if include_replay_ms:
            m['replay_ms'] = statistics.mean(s['replay_ms'] for s in rs)
        m['draws'] = statistics.mean(s['draws'] for s in rs)
        nz = [s for s in rs if s['draws']]
        if nz:
            m['replay_per_1k'] = statistics.mean(1000.0 * s['replay_ms'] / s['draws'] for s in nz)
        m['sets_us'] = statistics.mean(s['sets_us'] for s in rs)
    gp = [g for g in r['gpu'] if _in_spans(g, spans, fps)]
    if gp:
        m['gpu_ms'] = statistics.mean(g['avg'] for g in gp)
        m['gpu_p99'] = statistics.mean(g['p99'] for g in gp)
    mv = [v for v in r['movie'] if _movie_in_spans(v, spans)]
    if mv:
        tot_over = sum(v['over'] for v in mv if v['over'] > 0)
        if tot_over > 0:
            m['movie_decode_ms'] = sum(v['decode_ms'] * v['over'] for v in mv if v['over'] > 0) / tot_over
        else:
            m['movie_decode_ms'] = statistics.mean(v['decode_ms'] for v in mv)
        m['movie_decode_max'] = max(v['slowest_ms'] for v in mv)
        dts = [(v['dacq'], v['dt']) for v in mv if v['dt'] is not None and v['dacq'] is not None]
        if dts:
            tot_dt = sum(dt for _, dt in dts)
            if tot_dt > 0:
                m['movie_fps'] = sum(da for da, _ in dts) / tot_dt
        st = [v['dstarved'] for v in mv if v['dstarved'] is not None]
        if st:
            m['movie_starved'] = sum(st)


def measures(r):
    """{scene name: metrics}; without scenes one entry, '(steady windows)'."""
    if r['scenes']:
        out = {}
        for name, xs in r['scenes'].items():
            m = {k: statistics.mean(x[k] for x in xs) for k in xs[0] if k not in ('t0', 't1')}
            spans = [(x['t0'], x['t1']) for x in xs]
            _attach_nvn_stats(m, r, spans, m.get('fps', 60.0), include_replay_ms=False)
            out[name] = m
        return out
    if r['script_scenes']:
        out = {}
        for name, spans in r['script_scenes'].items():
            m = {}
            win = []
            for frac in (0.8, 0.5, 0.25):
                win = [w for w in r['windows'] if _win_in_spans(w, spans, frac)]
                if win:
                    break
            if win:
                frames = sum(w['frames'] for w in win)
                secs = sum(w['secs'] for w in win)
                m['fps'] = frames / secs if secs else 0.0
                m['low1'] = statistics.median(w['low1'] for w in win)
                m['worst'] = max(w['worst'] for w in win)
                meds = [w['median_ms'] for w in win if 'median_ms' in w]
                if meds:
                    m['median_ms'] = statistics.median(meds)
            slow = [ms for st, ms in r['slow'] if any(a <= st <= b for a, b in spans)]
            if slow:
                m['worst'] = max(m.get('worst', 0.0), max(slow))
            _attach_nvn_stats(m, r, spans, m.get('fps', 60.0), include_replay_ms=False)
            if m:
                out[name] = m
        if out:
            return out
    return {'(steady windows)': steady(r)}


def fmt(key, v):
    for k, _, _, f in METRICS:
        if k == key:
            return f.format(v)
    return f'{v}'


def table(rows, md):
    if md:
        out = ['| ' + ' | '.join(rows[0]) + ' |', '|' + '---|' * len(rows[0])]
        out += ['| ' + ' | '.join(r) + ' |' for r in rows[1:]]
        return '\n'.join(out)
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return '\n'.join('  '.join(c.ljust(w[i]) if i == 0 else c.rjust(w[i]) for i, c in enumerate(r)) for r in rows)


def describe(r):
    lines = [f"{r['path']}  ({r['end']:.0f} s)"]
    for k, v in r['setup'].items():
        lines.append(f'  {k}: {v}')
    for k, v in sorted(r['overrides'].items()):
        if v.endswith('(env)'):
            lines.append(f'  set by environment: {k} = {v[:-6]}')
    for w in r['warn']:
        lines.append(f'  WARNING: {w}')
    return '\n'.join(lines)


def summary(paths, md):
    for p in paths:
        r = parse(p)
        print(describe(r))
        for name, m in measures(r).items():
            rows = [['scene ' + name, 'value']] + [[label, fmt(k, m[k])] for k, label, _, _ in METRICS if k in m]
            print(table(rows, md))
        print()


def compare(a_paths, b_paths, names, md):
    A = [parse(p) for p in a_paths]
    B = [parse(p) for p in b_paths]
    for r in A + B:
        print(describe(r))
    print()
    # setup differences between the two sides (the point of an A/B is that only the switch differs)
    for key in sorted({k for r in A + B for k in r['setup']}):
        va = {r['setup'].get(key, '-') for r in A}
        vb = {r['setup'].get(key, '-') for r in B}
        if len(va | vb) > 1 and key != 'build':
            print(f"NOTE: '{key}' differs: {names[0]} {sorted(va)} / {names[1]} {sorted(vb)}")
    ma = [measures(r) for r in A]
    mb = [measures(r) for r in B]
    scenes = sorted({s for m in ma + mb for s in m})
    for sc in scenes:
        rows = [[f'scene {sc}', f'{names[0]} (n={sum(sc in m for m in ma)})', f'{names[1]} (n={sum(sc in m for m in mb)})', 'change']]
        for k, label, better, _ in METRICS:
            xa = [m[sc][k] for m in ma if sc in m and k in m[sc]]
            xb = [m[sc][k] for m in mb if sc in m and k in m[sc]]
            if not xa and not xb:
                continue
            a = statistics.median(xa) if xa else None
            b = statistics.median(xb) if xb else None
            ch = ''
            if a is not None and b is not None and a:
                pct = (b - a) / abs(a) * 100.0
                verdict = '' if not better or abs(pct) < 3 else (' better' if (pct > 0) == (better > 0) else ' worse')
                ch = f'{pct:+.1f}%{verdict}'
            if len(xa) > 1 or len(xb) > 1:
                spread = lambda xs: f' [{fmt(k, min(xs))}..{fmt(k, max(xs))}]' if len(xs) > 1 else ''
                rows.append([label, (fmt(k, a) + spread(xa)) if a is not None else '-', (fmt(k, b) + spread(xb)) if b is not None else '-', ch])
            else:
                rows.append([label, fmt(k, a) if a is not None else '-', fmt(k, b) if b is not None else '-', ch])
        print(table(rows, md))
        print()
    print('A change under 3% is within run-to-run noise for hand-played routes; a scripted route (docs/PERF_ROUTES.md) narrows that.')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('logs', nargs='*')
    ap.add_argument('--a', nargs='+', default=[])
    ap.add_argument('--b', nargs='+', default=[])
    ap.add_argument('--names', nargs=2, default=['A', 'B'])
    ap.add_argument('--markdown', action='store_true')
    a = ap.parse_args()
    if a.a or a.b:
        if not (a.a and a.b):
            ap.error('--a and --b go together')
        compare(a.a, a.b, a.names, a.markdown)
    elif a.logs:
        summary(a.logs, a.markdown)
    else:
        ap.print_help()
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
