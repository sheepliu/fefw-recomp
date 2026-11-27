# SPDX-License-Identifier: GPL-3.0-or-later
"""apply_exefs_patches: records in the alignment gaps widen the segment before. Run: python game-build/release/exefs_gap_test.py"""
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fefw_build as f  # noqa: E402


def make(segs, body):
    h = bytearray(0x100)
    h[0:4] = b'NSO0'
    for i, (fo, mo, sz) in enumerate(segs):
        h[0x10 + 0x10 * i:0x14 + 0x10 * i] = fo.to_bytes(4, 'little')
        h[0x14 + 0x10 * i:0x18 + 0x10 * i] = mo.to_bytes(4, 'little')
        h[0x18 + 0x10 * i:0x1C + 0x10 * i] = sz.to_bytes(4, 'little')
        h[0x60 + 4 * i:0x64 + 4 * i] = sz.to_bytes(4, 'little')
    return bytes(h) + body


def seg(o, i):
    fo, mo, sz = (int.from_bytes(o[0x10 + 0x10 * i + k:0x14 + 0x10 * i + k], 'little') for k in (0, 4, 8))
    return fo, mo, sz


def run(*files):
    """Apply one patch file per list of records (named w0, w1, ... in order) to a synthetic NSO -> the patched bytes."""
    segs = [(0x100, 0, 0xD00), (0x100 + 0xD00, 0x1000, 0x800), (0x100 + 0x1500, 0x2000, 0x400)]
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'main')
        with open(p, 'wb') as out:
            out.write(make(segs, b'T' * 0xD00 + b'R' * 0x800 + b'D' * 0x400 + b'TAIL'))
        f.apply_exefs_patches(p, [('m%d' % i, 'w%d' % i, recs) for i, recs in enumerate(files)])
        with open(p, 'rb') as patched:
            return patched.read()


def main():
    o = run([(0x10, b'\xcc')])                                   # in-segment only: nothing widens
    assert seg(o, 0) == (0x100, 0, 0xD00) and len(o) == 0x100 + 0x1900 + 4
    o = run([(0xE00, b'\xaa' * 4), (0x1FFC, b'\xbb' * 8)])       # both gaps; the second record runs on into data
    assert seg(o, 0) == (0x100, 0, 0x1000) and seg(o, 1) == (0x1100, 0x1000, 0x1000) and seg(o, 2) == (0x2100, 0x2000, 0x400)
    assert o[-4:] == b'TAIL'
    assert o[0x100 + 0xE00:0x100 + 0xE04] == b'\xaa' * 4 and o[0x100 + 0x1FFC:0x100 + 0x2004] == b'\xbb' * 8
    for i in range(3):
        fo, _, sz = seg(o, i)
        assert o[0xA0 + 0x20 * i:0xC0 + 0x20 * i] == hashlib.sha256(o[fo:fo + sz]).digest()
    try:
        run([(0x2500, b'x')])                                    # past data: still an error
        assert False
    except f.Fail:
        pass
    for gap in (0xE00, 0x1900):                                  # two files on the same gap bytes (after text, after rodata): refused
        try:
            run([(gap, b'\xaa' * 8)], [(gap + 4, b'\xbb' * 8)])
            assert False
        except f.Fail as e:
            assert 'w0 and w1' in str(e) and '0x%x' % (gap + 4) in str(e)
    run([(0xE00, b'\xaa' * 4)], [(0xE04, b'\xbb' * 4)])          # separate gap bytes: both apply
    run([(0xE00, b'\xaa' * 8), (0xE04, b'\xbb' * 4)])            # one file may rewrite its own gap bytes
    o = run([(0x10, b'\xaa')], [(0x10, b'\xbb')])                # inside a segment the later file still wins
    assert o[0x100 + 0x10] == 0xBB
    print('exefs_gap_test: ok')


if __name__ == '__main__':
    main()
