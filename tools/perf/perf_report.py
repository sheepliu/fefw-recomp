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

SCENE = re.compile(r"hitch: scene '([^']*)': (\d+) frames in ([0-9.]+) s: mean ([0-9.]+) fps, 1% low ([0-9.]+) fps, worst ([0-9.]+) ms, "
                   r"(\d+) over 25 ms, (\d+) over 50 ms, (\d+) over 2x the median \(([0-9.]+) ms\); render thread ms/frame: replay ([0-9.]+), "
                   r"present ([0-9.]+) \(limiter ([0-9.]+)\), idle ([0-9.]+)")
WINDOW = re.compile(r"hitch: (\d+) frames in ([0-9.]+) s: mean ([0-9.]+) fps, 1% low ([0-9.]+) fps, worst ([0-9.]+) ms, (\d+) over 2x the median")
RSTATS = re.compile(r"nvn\.rstats: (\d+) frames: replay ([0-9.]+) ms/frame = draw setup ([0-9.]+) .*?\| (\d+) draws/frame.*?per draw us:.*? sets ([0-9.]+)")
GPU = re.compile(r"nvn\.gpustats: \d+ frames: GPU ms/frame avg ([0-9.]+) p50 ([0-9.]+) p90 ([0-9.]+) p99 ([0-9.]+)")

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
    r = {'path': path, 'setup': {}, 'scenes': {}, 'windows': [], 'rstats': [], 'gpu': [], 'warn': [], 'overrides': {}, 'end': 0.0}
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = TS.match(line)
            t = float(m.group(1)) if m else r['end']
            r['end'] = max(r['end'], t)
            if 'hitch: scene' in line:
                m = SCENE.search(line)
                if m:
                    g = m.groups()
                    r['scenes'].setdefault(g[0], []).append({
                        'frames': int(g[1]), 'secs': float(g[2]), 'fps': float(g[3]), 'low1': float(g[4]), 'worst': float(g[5]),
                        'over25': int(g[6]), 'over50': int(g[7]), 'over2x': int(g[8]), 'median_ms': float(g[9]),
                        'replay_ms': float(g[10]), 'present_ms': float(g[11]), 'limiter_ms': float(g[12]), 'idle_ms': float(g[13])})
                continue
            m = WINDOW.search(line)
            if m:
                r['windows'].append({'t': t, 'frames': int(m.group(1)), 'secs': float(m.group(2)), 'fps': float(m.group(3)),
                                     'low1': float(m.group(4)), 'worst': float(m.group(5)), 'over2x': int(m.group(6))})
                continue
            m = RSTATS.search(line)
            if m:
                r['rstats'].append({'t': t, 'replay_ms': float(m.group(2)), 'setup_ms': float(m.group(3)), 'draws': int(m.group(4)),
                                    'sets_us': float(m.group(5))})
                continue
            m = GPU.search(line)
            if m:
                r['gpu'].append({'t': t, 'avg': float(m.group(1)), 'p50': float(m.group(2)), 'p99': float(m.group(4))})
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
    ('windows', 'steady 10 s windows', 0, '{:.0f}'),
]


def measures(r):
    """{scene name: metrics}; without scenes one entry, '(steady windows)'."""
    if r['scenes']:
        return {name: {k: statistics.mean(x[k] for x in xs) for k in xs[0]} for name, xs in r['scenes'].items()}
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
