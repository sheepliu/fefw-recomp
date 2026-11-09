import hashlib
import importlib.util
import json
from pathlib import Path
from unittest import TestCase, mock
import struct
import tempfile

import modpack


SOURCE = Path(__file__).resolve().parents[2] / "game-build" / "release" / "fefw_build.py"
SPEC = importlib.util.spec_from_file_location("test_release_builder", SOURCE)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
BUILD_ID = "AB" * 32


def plain_main():
    data = bytearray(0x500)
    data[:4] = b"NSO0"
    data[0x40:0x60] = bytes.fromhex(BUILD_ID)
    struct.pack_into("<I", data, 0x0C, 0x08)
    struct.pack_into("<III", data, 0x10, 0x100, 0, 0x400)
    struct.pack_into("<I", data, 0x60, 0x400)
    data[0xA0:0xC0] = hashlib.sha256(data[0x100:0x500]).digest()
    return bytes(data)


class ReleaseBuildTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="release-mod-test-")
        self.addCleanup(self.tmp.cleanup)
        self.game = Path(self.tmp.name) / "游戏目录"
        self.game.mkdir()
        self.build = self.game / "build"
        self.build.mkdir()
        self.staged = self.build / "input-exefs"
        self.staged.mkdir()
        (self.staged / "main").write_bytes(plain_main())
        self.mods = self.game / "mods"

    def install(self):
        ips = self.game / "gain.ips32"
        ips.write_bytes(b"IPS32" + b"\0\0\x01\x20" + b"\0\x04" + b"\x01\x02\x03\x04" + b"EEOF")
        modpack.import_mod(ips, self.mods, modpack.main_info(self.staged / "main"), "gain")
        modpack.set_enabled(self.mods, modpack.main_info(self.staged / "main"), "gain", True)

    def test_mod_set_detects_change_and_publish_keeps_runtime_image_in_sync(self):
        self.install()
        mp, path, packages, enabled, mods = builder.mod_set(self.game, self.staged / "main")
        self.assertEqual(mods["patches"][0][0], "gain")
        patched = mp.patched_main((self.staged / "main").read_bytes(), path, packages, enabled, BUILD_ID)
        (self.staged / "main").write_bytes(patched)
        exe = self.build / "candidate.exe"
        exe.write_bytes(b"new executable")
        done = {"format": builder.FORMAT, "tools": {"test": True}, "mods": mods,
                "active_main_sha256": builder.sha256(self.staged / "main"), "exe_sha256": builder.sha256(exe)}
        with mock.patch.object(builder, "tool_stamp", return_value={"test": True}):
            builder.publish(str(self.game), str(self.build), str(self.staged), str(exe), done)
            self.assertEqual(builder.check(str(self.game)), (True, "fefw.exe matches this release"))
            (self.game / "fefw.exe").write_bytes(b"changed")
            self.assertIn("does not match", builder.check(str(self.game))[1])
            (self.game / "fefw.exe").write_bytes(b"new executable")
            modpack.set_enabled(self.mods, modpack.main_info(self.game / "exefs" / "main"), "gain", False)
            self.assertIn("patches changed", builder.check(str(self.game))[1])

    def test_failed_publish_restores_old_exe_and_main(self):
        active = self.game / "exefs"
        active.mkdir()
        (active / "main").write_bytes(plain_main())
        (self.game / "fefw.exe").write_bytes(b"old executable")
        self.install()
        mp, path, packages, enabled, _ = builder.mod_set(self.game, self.staged / "main")
        (self.staged / "main").write_bytes(mp.patched_main(plain_main(), path, packages, enabled, BUILD_ID))
        exe = self.build / "candidate.exe"
        exe.write_bytes(b"new executable")
        with mock.patch.object(builder, "write_json", side_effect=OSError("out of disk")):
            with self.assertRaisesRegex(OSError, "out of disk"):
                builder.publish(str(self.game), str(self.build), str(self.staged), str(exe), {})
        self.assertEqual((active / "main").read_bytes(), plain_main())
        self.assertEqual((self.game / "fefw.exe").read_bytes(), b"old executable")
        self.assertEqual((self.build / "previous-fefw.exe").read_bytes(), b"old executable")
