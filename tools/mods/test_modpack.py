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

    def test_overlaps_follow_native_mod_name_order(self):
        self.install("first", ips32((0x120, b"\x00" * 8)))
        self.install("second", ips32((0x124, b"\x11" * 4)))
        modpack.set_enabled(self.mods, self.info, "first", True)
        modpack.set_enabled(self.mods, self.info, "second", True)
        self.assertEqual(modpack.read_state(self.mods)["enabled"], ["first", "second"])
        packages = modpack.installed(self.mods, self.info)
        original = self.main.read_bytes() + b"\0" * 0x400
        patched = modpack.patched_main(original, self.mods, packages, ["first", "second"], BUILD_ID)
        self.assertEqual(patched[0x120:0x128], b"\0" * 4 + b"\x11" * 4)

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
                   ips32((0x120, b"\0" * 4))[:-1]]
        for blob in invalid:
            with self.subTest(blob=blob), self.assertRaises(modpack.ModError):
                modpack.parse_ips(blob)

    def test_header_and_unmapped_ranges_are_rejected(self):
        blobs = [ips32((0xFC, b"\0" * 4)), ips32((0x500, b"\0" * 4))]
        for blob in blobs:
            with self.subTest(blob=blob), self.assertRaises(modpack.ModError):
                modpack.checked_writes(blob, self.info)

    def test_native_byte_writes_self_overlap_and_branch_data_are_accepted(self):
        cases = [ips32((0x121, b"\0" * 4)), ips32((0x120, b"\0" * 3)),
                 ips32((0x120, b"\0" * 8), (0x124, b"\x01" * 4)),
                 ips32((0x120, (0x140000FF).to_bytes(4, "little")))]
        for blob in cases:
            with self.subTest(blob=blob):
                kind, writes = modpack.checked_writes(blob, self.info)
                self.assertEqual(kind, "IPS32")
                self.assertTrue(writes)

    def full_main(self):
        data = bytearray(self.main.read_bytes() + b"\0" * 0x440)
        struct.pack_into("<I", data, 0x0C, 0x38)
        struct.pack_into("<I", data, 0x60, 0x400)
        for index, file_off, memory_off in ((1, 0x500, 0x1000), (2, 0x520, 0x2000)):
            struct.pack_into("<III", data, 0x10 + index * 0x10, file_off, memory_off, 0x20)
            struct.pack_into("<I", data, 0x60 + index * 4, 0x20)
        for index in range(3):
            off, _, size = struct.unpack_from("<III", data, 0x10 + index * 0x10)
            data[0xA0 + index * 32:0xC0 + index * 32] = hashlib.sha256(data[off:off + size]).digest()
        self.main.write_bytes(data)
        self.info = modpack.main_info(self.main)
        return bytes(data)

    def test_byte_patches_cover_rodata_and_data_and_refresh_each_segment_hash(self):
        original = self.full_main()
        patch = self.raw_patch("数据修改.ips32", ips32((0x1101, b"\x11\x22"), (0x2103, b"\x33")))
        result = modpack.import_mod(patch, self.mods, self.info)
        modpack.set_enabled(self.mods, self.info, result["id"], True)
        changed = modpack.patched_main(original, self.mods, modpack.installed(self.mods, self.info),
                                      [result["id"]], BUILD_ID)
        self.assertEqual(changed[0x501:0x503], b"\x11\x22")
        self.assertEqual(changed[0x523], 0x33)
        self.assertEqual(changed[0xC0:0xE0], hashlib.sha256(changed[0x500:0x520]).digest())
        self.assertEqual(changed[0xE0:0x100], hashlib.sha256(changed[0x520:0x540]).digest())
        self.assertEqual(changed[0x100:0x500], original[0x100:0x500])
        self.assertEqual(self.main.read_bytes(), original)

    def test_pchtxt_uses_native_blocks_offset_shift_and_build_id(self):
        original = self.full_main()
        patch = self.raw_patch("属性修改.pchtxt", (
            f"@nsobid-{BUILD_ID}\n@flag offset_shift 0x100\n@enabled\n"
            "00001001 AABB\n@disabled\n00002000 CCDD\n@stop\n").encode("utf-8"))
        result = modpack.import_mod(patch, self.mods, self.info)
        packages = modpack.installed(self.mods, self.info)
        changed = modpack.patched_main(original, self.mods, packages, [result["id"]], BUILD_ID)
        self.assertEqual(changed[0x501:0x503], b"\xAA\xBB")
        self.assertEqual(changed[0x520:0x522], b"\0\0")
        patch.write_text("@nsobid-" + "CD" * 32 + "\n@enabled\n00000120 AABB\n", encoding="utf-8")
        with self.assertRaisesRegex(modpack.ModError, "Build ID"):
            modpack.import_mod(patch, self.mods, self.info, "wrong-build")

    def test_native_folder_import_reports_skipped_modules_and_honors_disable(self):
        source = self.root / "社区补丁"
        source.joinpath("exefs").mkdir(parents=True)
        matching = source / "exefs" / (BUILD_ID + ".ips")
        matching.write_bytes(ips32((0x120, b"\x01\x02\x03\x04")))
        other = source / "exefs" / ("CD" * 32 + ".ips")
        other.write_bytes(ips32((0x120, b"\x05\x06\x07\x08")))
        result = modpack.import_mod(source, self.mods, self.info)
        self.assertEqual(len(result["ignored_files"]), 1)
        backend = modpack.native_builder()
        self.assertEqual(backend.selected_patches(self.root, self.info), [])
        modpack.set_enabled(self.mods, self.info, result["id"], True)
        selected = backend.selected_patches(self.root, self.info)
        self.assertEqual(len(selected), 1, "managed exefs must not also be scanned as loose patches")
        self.assertEqual(selected[0][2], [(0x20, b"\x01\x02\x03\x04")])
        modpack.set_enabled(self.mods, self.info, result["id"], False)
        self.assertEqual(backend.selected_patches(self.root, self.info), [])
        self.assertEqual(matching.read_bytes(), ips32((0x120, b"\x01\x02\x03\x04")))
        self.assertEqual(other.read_bytes(), ips32((0x120, b"\x05\x06\x07\x08")))

    def test_legacy_manifest_packages_still_import_and_apply(self):
        patch = self.raw_patch("old.ips32", ips32((0x120, b"\x01\x02\x03\x04")))
        package = self.root / "legacy"
        package.mkdir()
        (package / patch.name).write_bytes(patch.read_bytes())
        (package / "manifest.json").write_text(json.dumps(
            modpack.imported_manifest(patch, "legacy", "Old", "1.0", BUILD_ID)), encoding="utf-8")
        modpack.import_mod(package, self.mods, self.info)
        listed = modpack.set_enabled(self.mods, self.info, "legacy", True)
        self.assertEqual(listed["mods"][0]["type"], "exefs-ips")
        self.assertEqual(modpack.installed(self.mods, self.info)["legacy"][1],
                         [(0x20, 0x24, b"\x01\x02\x03\x04")])

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
        with self.assertRaisesRegex(modpack.ModError, "sha256 mismatch"):
            modpack.patched_main(original, self.mods, packages, ["first"], BUILD_ID)


if __name__ == "__main__":
    unittest.main()
