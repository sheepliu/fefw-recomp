# SPDX-License-Identifier: GPL-3.0-or-later
"""program.Workers: a lifter worker that dies does not hang the lift (issue #105). Run: python3 recomp/tests/lift_workers_test.py"""
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from nxrecomp import program  # noqa: E402

_spec = None


def init(spec):
    global _spec
    _spec = spec


def die():
    os._exit(1)                         # like a native crash: the process is gone, nothing is raised


def square(i):
    return i * i + _spec['add']


def die_once(i):
    """Task 5 kills its worker `deaths` times in all (one marker file per death), then works."""
    if i == 5:
        for k in range(_spec['deaths']):
            try:
                os.close(os.open(os.path.join(_spec['dir'], 'died%d' % k), os.O_CREAT | os.O_EXCL))
            except FileExistsError:
                continue
            die()
    return square(i)


def always_die(i):
    if i == 5:
        die()
    return square(i)


def fail(i):
    if i == 5:
        raise ValueError('task 5')
    return i


def bad_init(spec):
    raise ValueError('bad spec')


def pid(i):
    return os.getpid()


def beat(i):
    """Busy for a minute, writing to its own file all the while."""
    for _ in range(600):
        with open(os.path.join(_spec['dir'], 'beat%d' % i), 'a') as f:
            f.write('.')
        time.sleep(0.1)


def lifter(method, d):
    """Child process of orphans(): the lifter that gets killed while its workers are busy."""
    multiprocessing.set_start_method(method)
    program.Workers(4, {'dir': d}, print, init).map(beat, range(4))


def fork_fails():
    """Child process of main(): the third fork of a pool fails. It has to end, not wait for the first two."""
    multiprocessing.set_start_method('fork')
    fork, calls = os.fork, []

    def failing_fork():
        calls.append(1)
        if len(calls) == 3:
            raise OSError('no memory for another process')
        return fork()

    os.fork = failing_fork
    w = program.Workers(4, {'add': 1}, print, init)
    try:
        w.map(square, range(40))
    except OSError:
        print('raised')
    finally:
        w.close()


def child(*args):
    return subprocess.Popen([sys.executable, os.path.abspath(__file__)] + list(args), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)


def orphans(method):
    """A killed lifter takes its workers with it, and its output pipe reaches its end."""
    with tempfile.TemporaryDirectory() as d:
        p = child('--lifter', method, d)
        files = [os.path.join(d, 'beat%d' % i) for i in range(4)]
        deadline = time.time() + 30
        while not all(os.path.exists(f) for f in files):
            assert p.poll() is None and time.time() < deadline, (method, 'the workers did not start')
            time.sleep(0.1)
        p.kill()
        p.communicate(timeout=20)       # TimeoutExpired: a worker still holds the pipe
        time.sleep(2)
        sizes = [os.path.getsize(f) for f in files]
        time.sleep(1)
        assert sizes == [os.path.getsize(f) for f in files], (method, 'a worker is still running')


def idle_death():
    """A worker that dies between two maps costs neither a process nor, with one process, the lift."""
    for jobs, pause in ((1, 0), (1, 1), (4, 1)):
        lines = []
        w = program.Workers(4, {'add': 1}, lines.append, init)
        w.jobs = jobs                   # 1: the count after repeated deaths (Workers(1) would run in this process)
        try:
            os.kill(w.map(pid, range(8))[0], signal.SIGTERM)
            time.sleep(pause)
            assert w.map(square, range(8)) == [i * i + 1 for i in range(8)]
            assert w.jobs == jobs and not lines, (jobs, pause, w.jobs, lines)
        finally:
            w.close()


def main():
    want = [i * i + 1 for i in range(40)]
    for jobs, deaths, left in ((1, 0, 1), (8, 0, 8), (8, 1, 6), (8, 3, 3)):
        with tempfile.TemporaryDirectory() as d:
            lines = []
            w = program.Workers(jobs, {'add': 1, 'dir': d, 'deaths': deaths}, lines.append, init)
            try:
                assert w.map(die_once, range(40)) == want, (jobs, deaths)
                assert w.jobs == left and len(lines) == deaths, (w.jobs, lines)
                assert all(s.startswith('nxrecomp: warning:') for s in lines), lines    # what fefw_build.py passes on
                w.reset({'add': 2})
                assert w.map(square, range(40)) == [i + 1 for i in want] and w.jobs == left
            finally:
                w.close()

    for fn, setup, error in ((always_die, init, RuntimeError), (fail, init, ValueError), (square, bad_init, ValueError)):
        lines = []
        w = program.Workers(4, {'add': 1}, lines.append, setup)
        try:
            w.map(fn, range(40))
            raise AssertionError('%s: no error' % fn.__name__)
        except error:
            assert bool(lines) == (fn is always_die), (fn.__name__, lines)    # no retries for what a task or the setup raised
        finally:
            w.close()

    with tempfile.TemporaryDirectory() as d:        # one process left, and it dies after finishing some tasks
        lines = []
        w = program.Workers(4, {'add': 1, 'dir': d, 'deaths': 1}, lines.append, init)
        w.jobs = 1
        try:
            assert w.map(die_once, range(40)) == want and w.jobs == 1
            assert len(lines) == 1 and 'again in a new process.' in lines[0], lines
        finally:
            w.close()

    idle_death()
    for method in multiprocessing.get_all_start_methods():
        orphans(method)
    if hasattr(os, 'fork'):
        p = child('--fork-fails')
        try:
            out = p.communicate(timeout=30)[0]
        finally:
            p.kill()
        assert p.returncode == 0 and b'raised' in out, (p.returncode, out)
    print('lift_workers_test: ok')


if __name__ == '__main__':
    if sys.argv[1:2] == ['--lifter']:
        lifter(*sys.argv[2:])
    elif sys.argv[1:2] == ['--fork-fails']:
        fork_fails()
    else:
        main()
