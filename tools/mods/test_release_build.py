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
        self.sites = self.build / "main.sites"
        self.sites.write_text("# test sites\n", encoding="utf-8")

    def apply(self):
        main = self.staged / "main"
        patches = builder.manifest_patches(self.game, modpack.main_info(main))
        builder.apply_exefs_patches(str(main), patches)
        return patches

    def install(self):
        ips = self.game / "gain.ips32"
        ips.write_bytes(b"IPS32" + b"\0\0\x01\x20" + b"\0\x04" + b"\x01\x02\x03\x04" + b"EEOF")
        modpack.import_mod(ips, self.mods, modpack.main_info(self.staged / "main"), "gain")
        modpack.set_enabled(self.mods, modpack.main_info(self.staged / "main"), "gain", True)

    def test_mod_set_detects_change_and_publish_keeps_runtime_image_in_sync(self):
        self.install()
        patches = self.apply()
        self.assertEqual(patches[0][0], "gain")
        self.assertEqual((self.staged / "main").read_bytes()[0x120:0x124], b"\x01\x02\x03\x04")
        exe = self.build / "candidate.exe"
        exe.write_bytes(b"new executable")
        tools = {"exefs_patches": builder.exefs_patch_stamp(patches)}
        done = {"format": builder.FORMAT, "tools": tools,
                "active_main_sha256": builder.sha256(self.staged / "main"), "exe_sha256": builder.sha256(exe)}
        with mock.patch.object(builder, "tool_stamp", side_effect=lambda v, p, s: {"exefs_patches": p} if p else {}):
            builder.publish(str(self.game), str(self.build), str(self.staged), str(exe), done, self.sites, BUILD_ID)
            self.assertEqual(builder.check(str(self.game), "fma"), (True, "fefw.exe matches this release"))
            (self.game / "fefw.exe").write_bytes(b"changed")
            self.assertIn("does not match", builder.check(str(self.game), "fma")[1])
            (self.game / "fefw.exe").write_bytes(b"new executable")
            modpack.set_enabled(self.mods, modpack.main_info(self.game / "exefs" / "main"), "gain", False)
            self.assertIn("patches in mods changed", builder.check(str(self.game), "fma")[1])

    def test_failed_publish_restores_old_exe_and_main(self):
        active = self.game / "exefs"
        active.mkdir()
        (active / "main").write_bytes(plain_main())
        (self.game / "fefw.exe").write_bytes(b"old executable")
        self.install()
        (self.game / "fefw.sites").write_bytes(b"old sites")
        self.apply()
        exe = self.build / "candidate.exe"
        exe.write_bytes(b"new executable")
        with mock.patch.object(builder, "write_json", side_effect=OSError("out of disk")):
            with self.assertRaisesRegex(OSError, "out of disk"):
                builder.publish(str(self.game), str(self.build), str(self.staged), str(exe), {}, self.sites, BUILD_ID)
        self.assertEqual((active / "main").read_bytes(), plain_main())
        self.assertEqual((self.game / "fefw.exe").read_bytes(), b"old executable")
        self.assertEqual((self.game / "fefw.sites").read_bytes(), b"old sites")
        self.assertEqual((self.build / "previous-fefw.exe").read_bytes(), b"old executable")

    def test_native_loose_mods_coexist_with_manifest_packages(self):
        self.install()
        native = self.mods / "native-code" / "exefs"
        native.mkdir(parents=True)
        native.joinpath(BUILD_ID + ".ips32").write_bytes(
            b"IPS32" + b"\0\0\x01\x30\0\x04\x05\x06\x07\x08EEOF")
        self.mods.joinpath("native-textures", "romfs").mkdir(parents=True)
        patches = builder.collect_exefs_patches(self.game, BUILD_ID)
        patches += builder.manifest_patches(self.game, modpack.main_info(self.staged / "main"))
        self.assertEqual(len(patches), 2)
        builder.apply_exefs_patches(str(self.staged / "main"), patches)
        self.assertEqual((self.staged / "main").read_bytes()[0x130:0x134], b"\x05\x06\x07\x08")

    def test_manifest_patches_do_not_bypass_hook_checks(self):
        self.install()
        patches = builder.manifest_patches(self.game, modpack.main_info(self.staged / "main"))
        with mock.patch.object(builder, "site_addresses", return_value={0x20: "entryhook"}):
            with self.assertRaisesRegex(builder.Fail, "cannot be combined"):
                builder.apply_exefs_patches(str(self.staged / "main"), patches)
        self.assertEqual((self.staged / "main").read_bytes(), plain_main())

    def test_known_entry_compatibility_is_scoped_to_entry_sites(self):
        self.install()
        patches = builder.manifest_patches(self.game, modpack.main_info(self.staged / "main"))
        allowed = {("gain/manifest.json", 0x20, b"\x01\x02\x03\x04")}
        with mock.patch.object(builder, "site_addresses", return_value={0x20: "inline"}):
            with self.assertRaisesRegex(builder.Fail, "cannot be combined"):
                builder.apply_exefs_patches(str(self.staged / "main"), patches, compatible_entries=allowed)
        with mock.patch.object(builder, "site_addresses", return_value={0x20: "entryhook"}):
            builder.apply_exefs_patches(str(self.staged / "main"), patches, compatible_entries=allowed)
