import hashlib
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import dev_build
import modpack


BUILD_ID = "AB" * 32


def plain_main():
    data = bytearray(0x500)
    data[:4] = b"NSO0"
    data[0x40:0x60] = bytes.fromhex(BUILD_ID)
    struct.pack_into("<I", data, 0x0C, 0x08)
    struct.pack_into("<III", data, 0x10, 0x100, 0, 0x400)
    struct.pack_into("<I", data, 0x60, 0x400)
    data[0xA0:0xC0] = hashlib.sha256(data[0x100:]).digest()
    return bytes(data)


class CheckoutModBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="checkout-mod-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "源代码"
        self.game = self.repo / "work" / "play"
        self.source = self.repo / "game" / "dump" / "exefs"
        self.source.mkdir(parents=True)
        self.game.mkdir(parents=True)
        (self.source / "main").write_bytes(plain_main())
        for module in dev_build.MODULES[1:]:
            (self.source / module).write_bytes(module.encode("ascii"))
        for relative in dev_build.BUILD_FILES:
            source = self.repo / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(relative, encoding="utf-8")
        self.p = dev_build.paths(self.repo, self.game)
        self.original = (self.source / "main").read_bytes()

    def install(self):
        patch = self.game / "gain.ips32"
        patch.write_bytes(b"IPS32" + b"\x00\x00\x01\x20\x00\x04\x01\x02\x03\x04" + b"EEOF")
        info = modpack.main_info(self.source / "main")
        modpack.import_mod(patch, self.game / "mods", info, "gain")
        modpack.set_enabled(self.game / "mods", info, "gain", True)

    def fake_compilation(self):
        timestamp = self.p["input"].joinpath("main").stat().st_mtime + 2
        self.p["libs"].mkdir(parents=True, exist_ok=True)
        for module in dev_build.MODULES:
            lib = self.p["libs"] / f"fefw_{module}.lib"
            lib.write_bytes(module.encode("ascii"))
            os.utime(lib, (timestamp, timestamp))
        self.p["exe"].parent.mkdir(parents=True, exist_ok=True)
        self.p["exe"].write_bytes(b"translated game")
        os.utime(self.p["exe"], (timestamp + 2, timestamp + 2))

    def test_switching_patches_requires_rebuild_and_never_changes_original(self):
        self.install()
        stamp = dev_build.prepare(self.repo, self.game)
        self.assertNotEqual((self.p["input"] / "main").read_bytes(), self.original)
        self.assertEqual((self.source / "main").read_bytes(), self.original)
        self.fake_compilation()
        dev_build.publish(self.repo, self.game, stamp)
        self.assertTrue(dev_build.check(self.repo, self.game)[0])
        first_main = (self.p["active"] / "main").read_bytes()
        self.assertFalse((self.repo / "game-build" / "fefw" / "build" / "release" / "fefw.exe").exists())
        modpack.set_enabled(self.game / "mods", modpack.main_info(self.source / "main"), "gain", False)
        self.assertFalse(dev_build.check(self.repo, self.game)[0])
        self.assertEqual((self.p["active"] / "main").read_bytes(), first_main)
        stamp = dev_build.prepare(self.repo, self.game)
        self.assertEqual((self.p["input"] / "main").read_bytes(), self.original)
        self.fake_compilation()
        dev_build.publish(self.repo, self.game, stamp)
        self.assertTrue(dev_build.check(self.repo, self.game)[0])
        self.assertEqual((self.p["active"] / "main").read_bytes(), self.original)
        self.assertEqual((self.source / "main").read_bytes(), self.original)
        self.assertTrue(list(self.p["previous"].glob("main-*.bak")))

    def test_failed_publish_rolls_back_the_active_pair(self):
        stamp = dev_build.prepare(self.repo, self.game)
        self.fake_compilation()
        dev_build.publish(self.repo, self.game, stamp)
        done = self.p["done"].read_bytes()
        active_main = (self.p["active"] / "main").read_bytes()
        active_exe = (self.p["active"] / "fefw.exe").read_bytes()
        self.install()
        stamp = dev_build.prepare(self.repo, self.game)
        self.fake_compilation()
        replace = os.replace

        def fail_exe(source, destination):
            if Path(destination) == self.p["active"] / "fefw.exe":
                raise OSError("simulated publishing failure")
            return replace(source, destination)

        with mock.patch.object(dev_build.os, "replace", side_effect=fail_exe):
            with self.assertRaisesRegex(OSError, "simulated publishing failure"):
                dev_build.publish(self.repo, self.game, stamp)
        self.assertEqual((self.p["active"] / "main").read_bytes(), active_main)
        self.assertEqual((self.p["active"] / "fefw.exe").read_bytes(), active_exe)
        self.assertEqual(self.p["done"].read_bytes(), done)

    def test_changed_original_dependency_invalidates_completed_build(self):
        stamp = dev_build.prepare(self.repo, self.game)
        self.fake_compilation()
        dev_build.publish(self.repo, self.game, stamp)
        self.assertTrue(dev_build.check(self.repo, self.game)[0])
        (self.source / "sdk").write_bytes(b"changed SDK")
        self.assertFalse(dev_build.check(self.repo, self.game)[0])

    def test_build_passes_isolated_paths_to_both_existing_build_scripts(self):
        self.install()

        def compile_step(command, **kwargs):
            if len(calls) == 0:
                self.assertEqual(kwargs["env"]["NX_EXEFS"], str(self.p["input"]))
                self.assertEqual(kwargs["env"]["NX_BUILD"], str(self.p["libs"]))
                self.assertEqual(kwargs["env"]["NX_GEN"], str(self.p["gen"]))
                self.fake_compilation()
            else:
                self.assertIn(str(self.p["exe"].parent), command)
                self.assertIn(str(self.p["libs"]), command)
            calls.append(command)

        calls = []
        with mock.patch.object(dev_build.subprocess, "run", side_effect=compile_step):
            done = dev_build.build(self.repo, self.game, 4)
        self.assertEqual(len(calls), 2)
        self.assertTrue(done["exe_sha256"])
        self.assertTrue(dev_build.check(self.repo, self.game)[0])


if __name__ == "__main__":
    unittest.main()
