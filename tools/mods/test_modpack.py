import json
import contextlib
import hashlib
import io
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

import modpack


BUILD_ID = "AB" * 32


def ips32(*records):
    result = bytearray(b"IPS32")
    for offset, payload in records:
        result.extend(offset.to_bytes(4, "big"))
        result.extend(len(payload).to_bytes(2, "big"))
        result.extend(payload)
    return bytes(result + b"EEOF")


def ips(*records):
    result = bytearray(b"PATCH")
    for offset, payload in records:
        result.extend(offset.to_bytes(3, "big"))
        result.extend(len(payload).to_bytes(2, "big"))
        result.extend(payload)
    return bytes(result + b"EOF")


class ModPackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="modpack-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "含中文路径"
        self.root.mkdir()
        self.mods = self.root / "mods"
        self.main = self.root / "main"
        header = bytearray(0x100)
        header[:4] = b"NSO0"
        header[0x40:0x60] = bytes.fromhex(BUILD_ID)
        struct.pack_into("<III", header, 0x10, 0x100, 0x0, 0x400)
        self.main.write_bytes(header)
        self.info = modpack.main_info(self.main)

    def raw_patch(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def install(self, name, data):
        return modpack.import_mod(self.raw_patch(name + ".ips32", data), self.mods, self.info, name)

    def test_ips32_import_and_build_fingerprint(self):
        self.assertEqual(self.info["text_size"], 0x400)
        path = self.raw_patch("two.ips32", ips32((0x120, b"\x01\x02\x03\x04")))
        self.assertEqual(modpack.import_mod(path, self.mods, self.info, "two")["status"], "imported")
        self.assertEqual(modpack.import_mod(path, self.mods, self.info, "two")["status"], "unchanged")
        before = modpack.list_mods(self.mods, self.info)
        self.assertFalse(before["mods"][0]["enabled"])
        enabled = modpack.set_enabled(self.mods, self.info, "two", True)
        self.assertTrue(enabled["mods"][0]["enabled"])
        self.assertNotEqual(before["patch_set_sha256"], enabled["patch_set_sha256"])
        self.assertEqual(enabled["patch_set_sha256"], modpack.list_mods(self.mods, self.info)["patch_set_sha256"])
        modpack.set_enabled(self.mods, self.info, "two", False)
        self.assertEqual(modpack.list_mods(self.mods, self.info)["patch_set_sha256"], before["patch_set_sha256"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(modpack.main(["--main", str(self.main), "--mods-dir", str(self.mods), "list"]), 0)
        self.assertEqual(json.loads(output.getvalue())["game_build_id"], BUILD_ID)
        cli = subprocess.run([sys.executable, "-I", str(Path(modpack.__file__)), "--main", str(self.main),
                              "--mods-dir", str(self.mods), "list"], capture_output=True, text=True,
                             encoding="utf-8", check=True)
        self.assertEqual(json.loads(cli.stdout)["game_build_id"], BUILD_ID)

    def test_conflict_preserves_existing_state(self):
        self.install("first", ips32((0x120, b"\x00" * 8)))
        self.install("second", ips32((0x124, b"\x11" * 4)))
        modpack.set_enabled(self.mods, self.info, "first", True)
        with self.assertRaisesRegex(modpack.ModError, "first vs second.*main\\+0x24"):
            modpack.set_enabled(self.mods, self.info, "second", True)
        self.assertEqual(modpack.read_state(self.mods)["enabled"], ["first"])

    def test_nonoverlapping_patches_and_legacy_ips(self):
        self.install("first", ips32((0x120, b"\x00" * 8)))
        self.install("third", ips((0x128, b"\x10" * 4)))
        modpack.set_enabled(self.mods, self.info, "first", True)
        result = modpack.set_enabled(self.mods, self.info, "third", True)
        self.assertEqual(sum(item["enabled"] for item in result["mods"]), 2)

    def test_rle_and_invalid_patch_cases(self):
        rle = b"PATCH" + b"\x00\x01\x20" + b"\x00\x00" + b"\x00\x04\xAA" + b"EOF"
        self.assertEqual(modpack.checked_writes(rle, self.info)[1][0][2], b"\xAA" * 4)
        invalid = [b"", b"PATCH", b"PATCH" + b"\x00\x01\x20\x00\x04\xAA" + b"EOF",
                   ips((0x120, b"\0" * 4)) + b"\0\0\0", ips32((0x120, b"\0" * 4))[:-1],
                   b"PATCH" + b"\x00\x01\x20\x00\x00\x00\x00\xAAEOF"]
        for blob in invalid:
            with self.subTest(blob=blob), self.assertRaises(modpack.ModError):
                modpack.parse_ips(blob)

    def test_bad_ranges_alignment_self_overlap(self):
        blobs = [ips32((0xFC, b"\0" * 4)), ips32((0x500, b"\0" * 4)),
                 ips32((0x121, b"\0" * 4)), ips32((0x120, b"\0" * 3)),
                 ips32((0x120, b"\0" * 8), (0x124, b"\x01" * 4)),
                 ips32((0x120, (0x140000FF).to_bytes(4, "little")))]
        for blob in blobs:
            with self.subTest(blob=blob), self.assertRaises(modpack.ModError):
                modpack.checked_writes(blob, self.info)

    def test_wrong_build_hash_duplicate_id_and_rollback(self):
        path = self.raw_patch("first.ips32", ips32((0x120, b"\0" * 4)))
        manifest = modpack.imported_manifest(path, "first", "First", "1.0", BUILD_ID)
        manifest["game_build_id"] = "00" * 32
        with self.assertRaisesRegex(modpack.ModError, "Build ID"):
            modpack.validate_manifest(manifest, self.root, self.info)
        manifest["game_build_id"] = BUILD_ID
        manifest["sha256"] = "00" * 32
        with self.assertRaisesRegex(modpack.ModError, "sha256"):
            modpack.validate_manifest(manifest, self.root, self.info)
        self.assertEqual(modpack.import_mod(path, self.mods, self.info, "first")["status"], "imported")
        path.write_bytes(ips32((0x124, b"\0" * 4)))
        with self.assertRaisesRegex(modpack.ModError, "already installed"):
            modpack.import_mod(path, self.mods, self.info, "first")
        self.assertEqual(modpack.read_state(self.mods)["enabled"], [])

    def test_runtime_metadata_does_not_claim_execution(self):
        package = self.root / "runtime"
        package.mkdir()
        (package / "catalog.json").write_text("{}", encoding="utf-8")
        manifest = {"manifest_version": 1, "id": "custom-action", "name": "Custom", "version": "1.0",
                    "type": "runtime", "game_build_id": BUILD_ID, "capability_version": 1,
                    "capabilities": ["inventory.add"], "resources": ["catalog.json"],
                    "operations": [{"id": "add", "label": "Add", "capability": "inventory.add",
                                    "fields": [{"name": "count", "label": "Count", "type": "integer"}]}]}
        (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = modpack.import_mod(package, self.mods, self.info)
        self.assertEqual(result["status"], "imported")
        listed = modpack.list_mods(self.mods, self.info)["mods"][0]
        self.assertEqual(listed["status"], "metadata_only")
        self.assertEqual(listed["version"], "1.0")
        with self.assertRaisesRegex(modpack.ModError, "not implemented"):
            modpack.set_enabled(self.mods, self.info, "custom-action", True)
        self.assertEqual(modpack.read_state(self.mods)["enabled"], [])
        manifest["resources"] = ["../main"]
        with self.assertRaisesRegex(modpack.ModError, "escapes"):
            modpack.validate_manifest(manifest, package, self.info)

    def test_known_runtime_packages_enable_without_changing_ips_fingerprint(self):
        for mod_id, spec in modpack.SUPPORTED_RUNTIME.items():
            package = self.root / mod_id
            package.mkdir()
            for filename in spec["resources"]:
                (package / filename).write_text("[]", encoding="utf-8")
            manifest = {"manifest_version": 1, "id": mod_id, "name": mod_id,
                        "version": "1.0", "type": "runtime", "game_build_id": BUILD_ID,
                        "capability_version": 1, **spec}
            (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            modpack.import_mod(package, self.mods, self.info)
        before = modpack.list_mods(self.mods, self.info)
        self.assertTrue(all(m["status"] == "runtime_supported" for m in before["mods"]))
        for mod_id in modpack.SUPPORTED_RUNTIME:
            modpack.set_enabled(self.mods, self.info, mod_id, True)
        after = modpack.list_mods(self.mods, self.info)
        self.assertEqual(before["patch_set_sha256"], after["patch_set_sha256"])
        self.assertTrue(all(m["enabled"] for m in after["mods"]))

    def test_corrupt_state_and_manifest_fail_closed(self):
        self.install("valid", ips32((0x120, b"\x00" * 4)))
        modpack.state_path(self.mods).write_text('{"format":1,"enabled":[{}]}', encoding="utf-8")
        with self.assertRaisesRegex(modpack.ModError, "invalid enabled"):
            modpack.read_state(self.mods)
        (self.mods / "valid" / "manifest.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(modpack.ModError, "invalid package manifest"):
            modpack.installed(self.mods, self.info)

    def test_patched_main_uses_memory_offsets_updates_hash_and_preserves_source(self):
        self.install("first", ips32((0x120, b"\x01\x02\x03\x04")))
        packages = modpack.installed(self.mods, self.info)
        data = bytearray(self.main.read_bytes() + b"\x00" * 0x400)
        struct.pack_into("<I", data, 0x0C, 0x08)
        struct.pack_into("<I", data, 0x60, 0x400)
        data[0xA0:0xC0] = hashlib.sha256(data[0x100:0x500]).digest()
        original = bytes(data)
        result = modpack.patched_main(original, self.mods, packages, ["first"], BUILD_ID)
        self.assertEqual(original[0x120:0x124], b"\0" * 4)
        self.assertEqual(result[0x120:0x124], b"\x01\x02\x03\x04")
        self.assertEqual(result[0xA0:0xC0], hashlib.sha256(result[0x100:0x500]).digest())
        self.assertEqual(modpack.patched_main(original, self.mods, packages, [], BUILD_ID), original)
        damaged = bytearray(original)
        damaged[0x100] = 1
        with self.assertRaisesRegex(modpack.ModError, "original main .text SHA"):
            modpack.patched_main(bytes(damaged), self.mods, packages, ["first"], BUILD_ID)
        (self.mods / "first" / "first.ips32").write_bytes(ips32((0x124, b"\0" * 4)))
        with self.assertRaisesRegex(modpack.ModError, "IPS sha256 changed"):
            modpack.patched_main(original, self.mods, packages, ["first"], BUILD_ID)


if __name__ == "__main__":
    unittest.main()
