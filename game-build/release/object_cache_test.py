# SPDX-License-Identifier: GPL-3.0-or-later
"""compile_all's object cache: which units a later build compiles again. Run: python game-build/release/object_cache_test.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fefw_build as f  # noqa: E402


class FakeZig:
    """Stands in for Runner: `zig version` answers self.version, a compile writes an object that depends on its source."""

    def __init__(self):
        self.version, self.compiled = '1.0', []

    def run(self, cmd, env=None, cwd=None, on_line=None):
        if cmd[1] == 'version':
            return 0, self.version
        src, out = cmd[cmd.index('-c') + 1], cmd[cmd.index('-o') + 1]
        with open(src, 'rb') as i, open(out, 'wb') as o:
            o.write(b'object of ' + i.read())
        self.compiled.append(os.path.relpath(src, os.path.dirname(os.path.dirname(src))).replace('\\', '/'))
        return 0, ''


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as out:
        out.write(text)


def main():
    f.say = lambda *a: None                                              # the launcher's progress lines
    names = [m[0] for m in f.MODULES]
    every = sorted('%s/%s.cpp' % (n, u) for n in names for u in ('c_000', 'module_%s' % n))
    with tempfile.TemporaryDirectory() as d:
        gen, cache = os.path.join(d, 'lift'), os.path.join(d, 'objcache')
        f.RECOMP = os.path.join(d, 'recomp')
        header = os.path.join(f.RECOMP, 'include', 'nxrecomp', 'cxx.h')
        write(header, 'shared')
        for n in names:
            write(os.path.join(gen, n, '.lifted'), 'ok\n')
            write(os.path.join(gen, n, 'funcs.h'), 'funcs of ' + n)
            for u in ('c_000', 'module_%s' % n):
                write(os.path.join(gen, n, u + '.cpp'), '%s %s' % (n, u))
        zig = FakeZig()

        def build(variant='fma'):
            """compile_all and the pruning after a link -> (the units compiled, the objects)."""
            zig.compiled = []
            objects = f.compile_all(zig, gen, cache, 2, {}, variant)
            f.prune_objects(cache, objects)
            paths = sorted(p for ps in objects.values() for p in ps)
            assert len(paths) == len(every) and all(os.path.isfile(p) for p in paths)
            assert paths == sorted(os.path.join(cache, n, x) for n in names for x in os.listdir(os.path.join(cache, n)))
            return sorted(zig.compiled), objects

        compiled, first = build()
        assert compiled == every
        compiled, again = build()
        assert compiled == [] and {n: sorted(p) for n, p in again.items()} == {n: sorted(p) for n, p in first.items()}

        write(os.path.join(gen, 'main', 'c_000.cpp'), 'changed')
        assert build()[0] == ['main/c_000.cpp']
        write(os.path.join(gen, 'sdk', 'funcs.h'), 'changed')            # a module's header: that module
        assert build()[0] == ['sdk/c_000.cpp', 'sdk/module_sdk.cpp']
        write(header, 'changed')                                         # a shared header: everything
        assert build()[0] == every
        zig.version = '1.1'
        assert build()[0] == every
        assert build('compat')[0] == every                               # other flags

        objects = build()[1]
        with open(next(p for p in objects['rtld'] if 'module_rtld' in p), 'wb'):
            pass                                                         # an object a power loss left empty
        write(os.path.join(cache, 'main', 'c_000.0.o.tmp'), 'half')      # a stopped build's leftover
        assert build()[0] == ['rtld/module_rtld.cpp']
    print('ok')


if __name__ == '__main__':
    main()
