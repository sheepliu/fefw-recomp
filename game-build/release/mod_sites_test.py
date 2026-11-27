# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for collect_mod_sites() unknown site-kind skipping and stderr warning (fefw_build.py)."""
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fefw_build  # noqa: E402

BID = 'DB93B0125F4850601D61C3EDDE012F8EB2ECFE77'
OTHER_BID = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'


def write_sites(root, mod, filename, text):
    ex = os.path.join(root, 'mods', mod, 'exefs')
    os.makedirs(ex, exist_ok=True)
    with open(os.path.join(ex, filename), 'w', encoding='utf-8') as f:
        f.write(text)


def test_unknown_kinds_skipped_and_warned_once():
    fefw_build.WARNED_SITES.clear()
    with tempfile.TemporaryDirectory() as d:
        write_sites(
            d, 'MixedMod', 'a.sites',
            f'@nsobid {BID}\n'
            'patch 0x7100000000\n'
            'midHook 0x7100000004\n'
            'midhook 0x7100000008\n',
        )
        # Different build ID: must not warn or collect.
        write_sites(
            d, 'OtherBuild', 'b.sites',
            f'@nsobid {OTHER_BID}\n'
            'dynimm 0x7100000010\n',
        )

        err1 = io.StringIO()
        with contextlib.redirect_stderr(err1):
            sites1 = fefw_build.collect_mod_sites(d, BID)
        assert [(k, a) for _, k, a in sites1] == [('midhook', 0x7100000008)], sites1
        warn1 = [l for l in err1.getvalue().splitlines() if 'warning:' in l]
        assert len(warn1) == 2, warn1
        assert '"patch"' in warn1[0] and '"midHook"' in warn1[1], warn1
        assert 'dynimm' not in err1.getvalue(), err1.getvalue()

        # Second call in the same run must not repeat the stderr warnings.
        err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            sites2 = fefw_build.collect_mod_sites(d, BID)
        assert sites2 == sites1
        assert err2.getvalue() == '', err2.getvalue()


def test_unknown_only_mod_produces_empty_sites():
    fefw_build.WARNED_SITES.clear()
    with tempfile.TemporaryDirectory() as d:
        write_sites(
            d, 'UnknownOnly', 'u.sites',
            f'@nsobid {BID}\n'
            'dynimm 0x7100000004\n',
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sites = fefw_build.collect_mod_sites(d, BID)
        assert sites == [], sites
        assert fefw_build.mod_sites_stamp(sites) is None
        assert '"dynimm"' in err.getvalue(), err.getvalue()


if __name__ == '__main__':
    test_unknown_kinds_skipped_and_warned_once()
    test_unknown_only_mod_produces_empty_sites()
    print('mod_sites_test: ok')
