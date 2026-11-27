# SPDX-License-Identifier: GPL-3.0-or-later
"""The release zip (package.py), packed from a stand-in tree."""

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import package  # noqa: E402


class Pack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.here = Path(self.tmp.name)
        for name in package.FILES:
            (self.here / name).parent.mkdir(parents=True, exist_ok=True)
            (self.here / name).write_text(name)
        (self.here / "package.json").write_text('{"name": "p", "version": "0.1.0"}')
        (self.here / "LICENSE").write_text("licence")

    def tearDown(self):
        self.tmp.cleanup()

    def pack(self, version=None):
        out = package.pack(self.here, self.here / "LICENSE", self.here / "out", version)
        with zipfile.ZipFile(out) as z:
            return out.name, sorted(z.namelist()), json.loads(z.read("fefw-recomp/package.json"))

    def test_a_release_zip_carries_the_releases_version(self):
        name, names, meta = self.pack("0.4.0")
        self.assertEqual(name, "fefw-recomp-0.4.0-decky.zip")
        self.assertEqual(names, sorted(f"fefw-recomp/{n}" for n in package.FILES + ("LICENSE",)))
        self.assertEqual(meta, {"name": "p", "version": "0.4.0"})
        self.assertEqual(json.loads((self.here / "package.json").read_text())["version"], "0.1.0")  # the source stays

    def test_without_a_version_the_files_go_in_as_they_are(self):
        name, _, meta = self.pack()
        self.assertEqual((name, meta["version"]), ("fefw-recomp-decky.zip", "0.1.0"))

    def test_a_bad_version_or_a_missing_file_stops_it(self):
        for bad in ("", "1.0", "v1.0.0", "1.0.0/../x", "1.0.0 "):
            with self.assertRaises(ValueError):
                self.pack(bad)
        (self.here / "dist" / "index.js").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "index.js"):
            self.pack("0.4.0")


if __name__ == "__main__":
    unittest.main()
