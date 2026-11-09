# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate and stage recomp mod packages without changing game files.

IPS offsets here follow the Switch ExeFS convention: a 0x100-byte NSO header
followed by the uncompressed in-memory module image. The compressed NSO's
on-disk segment offsets are *not* IPS offsets.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import uuid


FORMAT = 1
IPS_LIMIT = 32 * 1024 * 1024
ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")

# Keep the runtime backend's supported operations explicit. Importing a package
# with an unknown capability never silently promises that it can execute.
SUPPORTED_RUNTIME = {
    "item-add": {
        "capabilities": ["inventory.add"],
        "resources": ["items.json"],
        "operations": [{"id": "add", "label": "Add item", "capability": "inventory.add", "fields": [
            {"name": "iid", "label": "Search items", "type": "choice"},
            {"name": "quantity", "label": "Quantity", "type": "integer", "minimum": 1, "maximum": 999, "default": 1},
        ]}],
    },
    "friendship-adjust": {
        "capabilities": ["friendship.adjust"],
        "resources": [],
        "operations": [{"id": "gift", "label": "Next gift increase", "capability": "friendship.adjust", "fields": [
            {"name": "amount", "label": "Next gift increase", "type": "integer", "minimum": 1, "maximum": 50000, "default": 500},
        ]}],
    },
}


def supported_runtime(manifest):
    spec = SUPPORTED_RUNTIME.get(manifest["id"])
    return (manifest["type"] == "runtime" and spec is not None and
            all(manifest.get(key) == value for key, value in spec.items()))


class ModError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModError(f"cannot read {path}: {exc}") from exc


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main_info(path):
    with path.open("rb") as src:
        header = src.read(0x100)
    if len(header) < 0x100 or header[:4] != b"NSO0":
        raise ModError(f"{path}: expected an NSO main header")
    build_id = header[0x40:0x60].hex().upper()
    text_offset, text_size = struct.unpack_from("<II", header, 0x14)
    if not text_size or text_offset % 4 or text_size % 4:
        raise ModError(f"{path}: invalid NSO text segment")
    return {"game_build_id": build_id, "text_offset": text_offset, "text_size": text_size}


def parse_ips(blob):
    """Return (format, [(file_offset, bytes)]); reject truncated/ambiguous records."""
    if len(blob) > IPS_LIMIT:
        raise ModError("IPS file exceeds 32 MiB limit")
    if blob.startswith(b"IPS32"):
        kind, cursor, width, marker = "IPS32", 5, 4, b"EEOF"
    elif blob.startswith(b"PATCH"):
        kind, cursor, width, marker = "IPS", 5, 3, b"EOF"
    else:
        raise ModError("expected PATCH or IPS32 magic")
    records = []
    while True:
        if blob[cursor:cursor + len(marker)] == marker:
            cursor += len(marker)
            if cursor != len(blob):
                raise ModError("trailing data / truncate extension is not supported")
            if not records:
                raise ModError("empty IPS patch")
            return kind, records
        if cursor + width + 2 > len(blob):
            raise ModError("truncated IPS record or missing EOF marker")
        offset = int.from_bytes(blob[cursor:cursor + width], "big")
        cursor += width
        size = int.from_bytes(blob[cursor:cursor + 2], "big")
        cursor += 2
        if size:
            if cursor + size > len(blob):
                raise ModError(f"truncated IPS record at 0x{offset:X}")
            payload = blob[cursor:cursor + size]
            cursor += size
        else:
            if cursor + 3 > len(blob):
                raise ModError(f"truncated IPS RLE record at 0x{offset:X}")
            count = int.from_bytes(blob[cursor:cursor + 2], "big")
            if count == 0:
                raise ModError(f"empty IPS RLE record at 0x{offset:X}")
            payload = blob[cursor + 2:cursor + 3] * count
            cursor += 3
        records.append((offset, payload))


def checked_writes(blob, info):
    kind, records = parse_ips(blob)
    text_start = info["text_offset"]
    text_end = text_start + info["text_size"]
    writes = []
    for offset, data in records:
        memory_offset = offset - 0x100
        end = memory_offset + len(data)
        if not (text_start <= memory_offset < end <= text_end):
            raise ModError(f"IPS offset 0x{offset:X} is outside main .text (or targets the NSO header)")
        if memory_offset % 4 or len(data) % 4:
            raise ModError(f"IPS offset 0x{offset:X} is not aligned to AArch64 instructions")
        for pos in range(0, len(data), 4):
            word = int.from_bytes(data[pos:pos + 4], "little")
            if word & 0x7C000000 == 0x14000000:  # AArch64 B / BL, signed imm26 words
                imm = word & 0x03FFFFFF
                displacement = (imm - (1 << 26) if imm & (1 << 25) else imm) * 4
                target = memory_offset + pos + displacement
                if not (text_start <= target < text_end):
                    raise ModError(f"branch at main+0x{memory_offset + pos:X} leaves main .text")
        writes.append((memory_offset, end, data))
    writes.sort(key=lambda x: x[0])
    for left, right in zip(writes, writes[1:]):
        if left[1] > right[0]:
            raise ModError(f"overlapping records in one IPS at main+0x{right[0]:X}")
    return kind, writes


def package_file(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ModError("package file path must be a nonempty forward-slash relative path")
    rel = Path(relative)
    if rel.is_absolute() or any(part in (".", "..") for part in rel.parts):
        raise ModError(f"package path escapes its folder: {relative}")
    source = root / rel
    if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
        raise ModError(f"missing or unsafe package file: {relative}")
    return source


def validate_manifest(manifest, root, info):
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != FORMAT:
        raise ModError("unsupported manifest_version (expected 1)")
    mod_id = manifest.get("id")
    if not isinstance(mod_id, str) or not ID.fullmatch(mod_id):
        raise ModError("invalid mod id (use lowercase letters, numbers and hyphens)")
    if not all(isinstance(manifest.get(key), str) and manifest[key].strip() for key in ("name", "version")):
        raise ModError("name and version are required")
    if manifest.get("game_build_id") != info["game_build_id"]:
        raise ModError(f"{mod_id}: game Build ID does not match main")
    kind = manifest.get("type")
    if kind == "exefs-ips":
        if manifest.get("target_module") != "main":
            raise ModError(f"{mod_id}: only the main NSO is supported")
        source = package_file(root, manifest.get("patch"))
        data = source.read_bytes()
        if not isinstance(manifest.get("sha256"), str) or not SHA256.fullmatch(manifest["sha256"]) or digest(data) != manifest["sha256"]:
            raise ModError(f"{mod_id}: IPS sha256 mismatch")
        actual_format, writes = checked_writes(data, info)
        if manifest.get("ips_format") != actual_format:
            raise ModError(f"{mod_id}: IPS format does not match the file")
        return [(manifest["patch"], source)], writes
    if kind == "runtime":
        if manifest.get("capability_version") != 1:
            raise ModError(f"{mod_id}: unsupported runtime capability_version")
        capabilities = manifest.get("capabilities")
        operations = manifest.get("operations")
        resources = manifest.get("resources", [])
        if not isinstance(capabilities, list) or not capabilities or not all(isinstance(x, str) and x for x in capabilities):
            raise ModError(f"{mod_id}: capabilities must be a nonempty list")
        if not isinstance(operations, list) or not isinstance(resources, list):
            raise ModError(f"{mod_id}: operations and resources must be lists")
        for op in operations:
            if not isinstance(op, dict) or op.get("capability") not in capabilities or not isinstance(op.get("id"), str):
                raise ModError(f"{mod_id}: invalid operation")
            if not isinstance(op.get("label"), str) or not isinstance(op.get("fields"), list):
                raise ModError(f"{mod_id}: operation requires label and fields")
            for field in op["fields"]:
                if not isinstance(field, dict) or not isinstance(field.get("name"), str) or not isinstance(field.get("label"), str) or field.get("type") not in ("integer", "choice", "text"):
                    raise ModError(f"{mod_id}: invalid operation field")
        return [(name, package_file(root, name)) for name in resources], []
    raise ModError(f"{mod_id}: unsupported mod type {kind!r}")


def imported_manifest(path, mod_id, name, version, build_id):
    if not mod_id:
        raise ModError("raw IPS imports require --id")
    data = path.read_bytes()
    kind, _ = parse_ips(data)
    return {"manifest_version": FORMAT, "id": mod_id, "name": name or mod_id,
            "version": version, "type": "exefs-ips", "game_build_id": build_id,
            "target_module": "main", "ips_format": kind, "patch": path.name, "sha256": digest(data)}


def installed(mods_dir, info):
    packages = {}
    if not mods_dir.exists():
        return packages
    for folder in sorted(mods_dir.iterdir()):
        if not folder.is_dir() or folder.is_symlink() or folder.name.startswith(".stage-"):
            continue
        manifest = read_json(folder / "manifest.json")
        if not isinstance(manifest, dict):
            raise ModError(f"invalid package manifest: {folder}")
        if manifest.get("id") != folder.name:
            raise ModError(f"package folder and mod id differ: {folder}")
        _, writes = validate_manifest(manifest, folder, info)
        packages[folder.name] = (manifest, writes)
    return packages


def state_path(mods_dir):
    return mods_dir.parent / "mods-state.json"


def read_state(mods_dir):
    path = state_path(mods_dir)
    if not path.exists():
        return {"format": FORMAT, "enabled": []}
    state = read_json(path)
    if not isinstance(state, dict) or state.get("format") != FORMAT or not isinstance(state.get("enabled"), list):
        raise ModError(f"invalid state file: {path}")
    names = state["enabled"]
    if not all(isinstance(x, str) and ID.fullmatch(x) for x in names) or len(names) != len(set(names)):
        raise ModError(f"invalid enabled mod IDs: {path}")
    return state


def check_conflicts(packages, enabled):
    writes = []
    for mod_id in sorted(enabled):
        if mod_id not in packages:
            raise ModError(f"enabled mod is missing: {mod_id}")
        for start, end, _ in packages[mod_id][1]:
            writes.append((start, end, mod_id))
    writes.sort()
    for left, right in zip(writes, writes[1:]):
        if left[1] > right[0]:
            raise ModError(f"IPS conflict: {left[2]} vs {right[2]} at main+0x{right[0]:X}")


def fingerprint(packages, enabled, build_id):
    patches = []
    for mod_id in sorted(enabled):
        manifest = packages[mod_id][0]
        if manifest["type"] == "exefs-ips":
            patches.append([mod_id, manifest["target_module"], manifest["sha256"]])
    data = {"game_build_id": build_id, "patches": patches}
    return digest(json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def patched_main(plain, mods_dir, packages, enabled, build_id):
    """Return a patched *uncompressed* NSO; never modify the input bytes.

    The recorded IPS offset is 0x100 + the module's memory address, whereas
    the NSO file's .text offset is read from its header. Other NSO segments are
    preserved byte-for-byte. The checksum must reflect the new .text bytes.
    """
    check_conflicts(packages, enabled)
    if len(plain) < 0x100 or plain[:4] != b"NSO0" or plain[0x40:0x60].hex().upper() != build_id:
        raise ModError("plain main is not the matching NSO")
    flags = struct.unpack_from("<I", plain, 0x0C)[0]
    if flags & 0x87:
        raise ModError("main must be fully unpacked before applying an IPS")
    file_off, mem_off, size = struct.unpack_from("<III", plain, 0x10)
    stored_size = struct.unpack_from("<I", plain, 0x60)[0]
    if file_off < 0x100 or size == 0 or stored_size != size or file_off + size > len(plain):
        raise ModError("invalid uncompressed main .text layout")
    original = plain[file_off:file_off + size]
    if flags & 0x08 and hashlib.sha256(original).digest() != plain[0xA0:0xC0]:
        raise ModError("original main .text SHA-256 mismatch")
    if not enabled:
        return plain
    out = bytearray(plain)
    for mod_id in sorted(enabled):
        manifest, expected = packages[mod_id]
        if manifest["type"] != "exefs-ips":
            continue
        patch = package_file(mods_dir / mod_id, manifest["patch"])
        blob = patch.read_bytes()
        if digest(blob) != manifest["sha256"]:
            raise ModError(f"{mod_id}: IPS sha256 changed during build")
        actual_format, actual = checked_writes(blob, {"text_offset": mem_off, "text_size": size})
        if actual_format != manifest["ips_format"] or actual != expected:
            raise ModError(f"{mod_id}: IPS writes changed during build")
        for start, end, data in expected:
            if not (mem_off <= start < end <= mem_off + size):
                raise ModError(f"{mod_id}: IPS write outside the unpacked .text segment")
            off = file_off + start - mem_off
            out[off:off + len(data)] = data
    out[0xA0:0xC0] = hashlib.sha256(out[file_off:file_off + size]).digest()
    return bytes(out)


def import_mod(source, mods_dir, info, mod_id=None, name=None, version="1.0.0"):
    if source.is_dir():
        manifest = read_json(source / "manifest.json")
        root = source
    elif source.is_file() and source.suffix.lower() in (".ips", ".ips32"):
        manifest = imported_manifest(source, mod_id, name, version, info["game_build_id"])
        root = source.parent
    else:
        raise ModError(f"expected a mod directory or .ips/.ips32 file: {source}")
    files, _ = validate_manifest(manifest, root, info)
    target = mods_dir / manifest["id"]
    if target.exists():
        old = read_json(target / "manifest.json")
        if old == manifest:
            validate_manifest(old, target, info)
            return {"status": "unchanged", "id": manifest["id"]}
        raise ModError(f"{manifest['id']}: already installed with different contents; remove it explicitly before importing")
    mods_dir.mkdir(parents=True, exist_ok=True)
    staging = mods_dir / (".stage-" + uuid.uuid4().hex)
    try:
        staging.mkdir()
        for relative, file in files:
            dest = staging / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, dest)
        write_json(staging / "manifest.json", manifest)
        validate_manifest(manifest, staging, info)
        os.rename(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"status": "imported", "id": manifest["id"]}


def list_mods(mods_dir, info):
    packages = installed(mods_dir, info)
    enabled = read_state(mods_dir)["enabled"]
    check_conflicts(packages, enabled)
    return {"game_build_id": info["game_build_id"], "patch_set_sha256": fingerprint(packages, enabled, info["game_build_id"]),
            "mods": [{"id": key, "name": value[0]["name"], "version": value[0]["version"], "type": value[0]["type"], "enabled": key in enabled,
                      "status": ("runtime_supported" if supported_runtime(value[0]) else "metadata_only") if value[0]["type"] == "runtime" else "release_build_supported"}
                     for key, value in packages.items()]}


def set_enabled(mods_dir, info, mod_id, enabled):
    packages = installed(mods_dir, info)
    if mod_id not in packages:
        raise ModError(f"mod not installed: {mod_id}")
    if enabled and packages[mod_id][0]["type"] == "runtime" and not supported_runtime(packages[mod_id][0]):
        raise ModError(f"{mod_id}: runtime mod execution is not implemented for this package")
    state = read_state(mods_dir)
    selected = set(state["enabled"])
    if enabled:
        selected.add(mod_id)
    else:
        selected.discard(mod_id)
    check_conflicts(packages, selected)
    state["enabled"] = sorted(selected)
    write_json(state_path(mods_dir), state)
    return list_mods(mods_dir, info)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--main", type=Path, required=True, help="the original game exefs/main (read-only)")
    parser.add_argument("--mods-dir", type=Path, required=True, help="installation's mods directory")
    subs = parser.add_subparsers(dest="command", required=True)
    importer = subs.add_parser("import", help="validate and copy an IPS or a package folder")
    importer.add_argument("source", type=Path)
    importer.add_argument("--id", help="required for a raw IPS")
    importer.add_argument("--name")
    importer.add_argument("--version", default="1.0.0")
    subs.add_parser("list", help="list installed packages, enabled state and patch fingerprint")
    for action in ("enable", "disable"):
        subs.add_parser(action).add_argument("id")
    args = parser.parse_args(argv)
    try:
        info = main_info(args.main)
        if args.command == "import":
            result = import_mod(args.source, args.mods_dir, info, args.id, args.name, args.version)
        elif args.command == "list":
            result = list_mods(args.mods_dir, info)
        else:
            result = set_enabled(args.mods_dir, info, args.id, args.command == "enable")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ModError, OSError) as exc:
        print(f"modpack: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
