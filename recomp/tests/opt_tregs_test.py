# SPDX-License-Identifier: GPL-3.0-or-later
"""opt.temps_local: a branch to a piece of the same guest function syncs x8..x17. Run: python3 recomp/tests/opt_tregs_test.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from nxrecomp import opt  # noqa: E402

PIECE, PIECE2, REAL = 0x7100a7807c, 0x7100a77ee0, 0x7100000100
SRC = '\n'.join([
    'void f_7100a77eec(Ctx *__restrict NX_NOESC c) {',
    '  c->fn = 0x7100a77eecULL;',
    '  c->x[10] = c->x[27] + 1ULL;',
    '  c->x[12] = (uint32_t)(~(0x1ULL));',
    '  c->x[30] = 0x7100a77f00ULL; {  f_%x(c);  }' % PIECE,                                    # a BL to it: an ordinary call
    '  f_%x(c);' % REAL,
    '  if (cond(&gf, 0)) { { c->N = gf.N; f_%x(c); gf.N = c->N; gf.k = NXF_RAW; } {  return; } }' % PIECE2,
    '  if (c->x[10] == 0) { {  f_%x(c);  } {  return; } }' % PIECE,
    '  c->x[12] = c->x[11];',
    '  f_%x(c); return;' % REAL,                                                              # a tail call of a real function
    '  f_%x(c); return;' % PIECE,
    '}'])
SYNC = 'c->x[10] = gx10; c->x[11] = gx11; c->x[12] = gx12; f_'
BACK = 'gx10 = c->x[10]; gx11 = c->x[11]; gx12 = c->x[12]; }'


def main():
    plain, n = opt.temps_local(SRC)
    assert n == 3 and SYNC not in plain, plain
    lines = opt.temps_local(SRC, frozenset({PIECE, PIECE2}))[0].split('\n')
    assert lines[2] == '  uint64_t gx10 = c->x[10], gx11 = c->x[11], gx12 = c->x[12];', lines[2]
    for k in (5, 6, 10):                                # the BL, the real call and the real tail call stay as they were
        assert SYNC not in lines[k], lines[k]
    for k in (7, 8, 11):                                # the three branches to pieces store before and load after
        assert SYNC in lines[k] and BACK in lines[k], lines[k]
    print('opt_tregs_test: ok')


if __name__ == '__main__':
    main()
