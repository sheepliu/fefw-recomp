# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate and stage recomp mod packages without changing game files.

IPS offsets here follow the Switch ExeFS convention: a 0x100-byte NSO header
followed by the uncompressed in-memory module image. The compressed NSO's
on-disk segment offsets are *not* IPS offsets.
"""

import argparse
import contextlib
from functools import lru_cache
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import uuid


FORMAT = 1
IPS_LIMIT = 32 * 1024 * 1024
ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PATCH_TYPES = ("exefs-ips", "exefs-patches")
PATCH_SUFFIXES = (".ips", ".ips32", ".pchtxt")

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
    segments = []
    for index, name in enumerate(("text", "rodata", "data")):
        file_offset, memory_offset, size = struct.unpack_from("<III", header, 0x10 + 0x10 * index)
        if size:
            segments.append({"name": name, "file_offset": file_offset, "memory_offset": memory_offset, "size": size})
    return {"game_build_id": build_id, "text_offset": text_offset, "text_size": text_size, "segments": segments}


@lru_cache(maxsize=1)
def native_builder():
    """Load the same pinned parser/writer used by upstream Setup, including in a release ZIP."""
    sibling = Path(__file__).with_name("fefw_build.py")
    source = sibling if sibling.is_file() else Path(__file__).resolve().parents[2] / "game-build/release/fefw_build.py"
    spec = importlib.util.spec_from_file_location("fefw_native_patch_backend", source)
    if not source.is_file() or spec is None or spec.loader is None:
        raise ModError("Native ExeFS patch backend is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not sibling.is_file():
        module.RECOMP = str(source.parents[2] / "recomp")
    return module


def parse_ips(blob):
    if len(blob) > IPS_LIMIT:
        raise ModError("IPS file exceeds 32 MiB limit")
    backend = native_builder()
    try:
        records = backend.parse_ips(blob, "IPS")
    except backend.Fail as exc:
        raise ModError(str(exc)) from exc
    return ("IPS32" if blob.startswith(b"IPS32") else "IPS"), records


def checked_records(records, info):
    """Use native module-segment bounds; data writes are not ARM64 instructions."""
    segments = info.get("segments", [{"memory_offset": info["text_offset"], "size": info["text_size"]}])
    writes = []
    for offset, data in records:
        if offset < 0x100:
            raise ModError(f"patch offset 0x{offset:X} targets the NSO header")
        if not data:
            continue
        start, end = offset - 0x100, offset - 0x100 + len(data)
        if not any(seg["memory_offset"] <= start < end <= seg["memory_offset"] + seg["size"] for seg in segments):
            raise ModError(f"patch offset 0x{offset:X} is outside main text, rodata and data")
        writes.append((start, end, data))
    return writes


def checked_writes(blob, info):
    kind, records = parse_ips(blob)
    return kind, checked_records(records, info)


def checked_patch(path, info):
    if path.suffix.lower() != ".pchtxt" and re.fullmatch(r"[0-9a-fA-F]{16,64}", path.stem):
        if not native_builder().bid_matches(path.stem, info["game_build_id"]):
            raise ModError(f"{path.name}: patch Build ID does not match main (other modules are not supported)")
    if path.suffix.lower() == ".pchtxt":
        backend = native_builder()
        try:
            records = backend.parse_pchtxt(path.read_text(encoding="utf-8-sig"), path.name, info["game_build_id"])
        except (backend.Fail, UnicodeError, ValueError) as exc:
            raise ModError(str(exc)) from exc
        if records is None:
            raise ModError(f"{path.name}: patch Build ID does not match main (other modules are not supported)")
        return "PCHTXT", checked_records(records, info)
    return checked_writes(path.read_bytes(), info)


def patch_entries(manifest):
    if manifest["type"] == "exefs-ips":
        return [{"path": manifest.get("patch"), "sha256": manifest.get("sha256"), "format": manifest.get("ips_format")}]
    entries = manifest.get("patches")
    if not isinstance(entries, list) or not entries or not all(isinstance(e, dict) for e in entries):
        raise ModError("patches must be a nonempty list")
    names = [entry.get("path") for entry in entries]
    if not all(isinstance(n, str) for n in names) or len(names) != len(set(names)):
        raise ModError("patch file paths must be unique")
    return entries


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
    if kind in PATCH_TYPES:
        if manifest.get("target_module") != "main":
            raise ModError(f"{mod_id}: only the main NSO is supported")
        files, writes = [], []
        for entry in patch_entries(manifest):
            source = package_file(root, entry.get("path"))
            if source.suffix.lower() not in PATCH_SUFFIXES:
                raise ModError(f"{mod_id}: unsupported patch file {source.name}")
            expected = entry.get("sha256")
            if not isinstance(expected, str) or not SHA256.fullmatch(expected) or digest(source.read_bytes()) != expected:
                raise ModError(f"{mod_id}: patch sha256 mismatch")
            actual_format, changes = checked_patch(source, info)
            if entry.get("format") != actual_format:
                raise ModError(f"{mod_id}: patch format does not match the file")
            files.append((entry["path"], source))
            writes.extend(changes)
        return files, writes
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


def suggested_id(source):
    label = source.parent.name if source.is_dir() and source.name.lower() == "exefs" else source.stem
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:48]
    if not slug or not slug[0].isalpha() or re.fullmatch(r"[a-f0-9]{16,64}", slug):
        slug = "patch-" + digest(str(source.resolve()).encode("utf-8"))[:12]
    return slug


def native_manifest(source, info, mod_id=None, name=None, version="1.0.0"):
    """Keep a managed selection over native patches; do not rewrite their contents."""
    root = source if source.is_dir() else source.parent
    patch_dir = root / "exefs" if (root / "exefs").is_dir() else root
    if source.is_file():
        paths = [source]
    else:
        paths = sorted((p for p in patch_dir.iterdir() if p.is_file() and p.suffix.lower() in PATCH_SUFFIXES),
                       key=lambda p: p.name.lower())
        if any(p.suffix.lower() in (".dll", ".sites") for p in patch_dir.iterdir() if p.is_file()):
            raise ModError("This folder includes DLL/hook-site mods. Install it through the original mods-folder entry.")
    if not paths:
        raise ModError("No IPS, IPS32 or pchtxt patches in the selected folder")
    entries, ignored = [], []
    for path in paths:
        if path.is_symlink():
            raise ModError(f"symlink patch is not supported: {path}")
        if path.suffix.lower() != ".pchtxt" and re.fullmatch(r"[0-9a-fA-F]{16,64}", path.stem):
            if not native_builder().bid_matches(path.stem, info["game_build_id"]):
                if source.is_dir():
                    ignored.append(f"{path.name}: other module or game Build ID")
                    continue
                raise ModError(f"{path.name}: patch Build ID does not match main (other modules are not supported)")
        if source.is_dir() and path.suffix.lower() == ".pchtxt":
            backend = native_builder()
            try:
                matched = backend.parse_pchtxt(path.read_text(encoding="utf-8-sig"), path.name, info["game_build_id"])
            except (backend.Fail, ValueError) as exc:
                raise ModError(str(exc)) from exc
            if matched is None:
                ignored.append(f"{path.name}: other module or game Build ID")
                continue
        kind, _ = checked_patch(path, info)
        entries.append({"path": path.relative_to(root).as_posix(), "sha256": digest(path.read_bytes()), "format": kind})
    if not entries:
        raise ModError("No patches match main's Build ID in the selected folder")
    manifest = {"manifest_version": FORMAT, "id": mod_id or suggested_id(source), "name": name or (
                source.parent.name if source.is_dir() and source.name.lower() == "exefs" else source.stem),
                "version": version, "type": "exefs-patches", "game_build_id": info["game_build_id"],
                "target_module": "main", "patches": entries}
    if ignored:
        manifest["ignored_files"] = ignored
    return manifest, root


def installed(mods_dir, info):
    packages = {}
    if not mods_dir.exists():
        return packages
    for folder in sorted(mods_dir.iterdir()):
        if not folder.is_dir() or folder.is_symlink() or folder.name.startswith(("-", "_", ".")):
            continue
        # Upstream's loose RomFS / ExeFS / DLL mods share this folder. They are
        # discovered by the native loader, not controlled by mods-state.json.
        if not (folder / "manifest.json").exists() and any(
                (folder / child).is_dir() for child in ("romfs", "exefs")):
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
    """Validate selection; native patch ordering permits overlap (later mod wins)."""
    for mod_id in sorted(enabled):
        if mod_id not in packages:
            raise ModError(f"enabled mod is missing: {mod_id}")


def fingerprint(packages, enabled, build_id):
    patches = []
    for mod_id in sorted(enabled):
        manifest = packages[mod_id][0]
        if manifest["type"] in PATCH_TYPES:
            patches.append([mod_id, manifest["target_module"], patch_entries(manifest)])
    data = {"game_build_id": build_id, "patches": patches}
    return digest(json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def patched_main(plain, mods_dir, packages, enabled, build_id):
    """Compatibility helper; use the upstream writer on a scratch file."""
    check_conflicts(packages, enabled)
    if len(plain) < 0x100 or plain[:4] != b"NSO0" or plain[0x40:0x60].hex().upper() != build_id:
        raise ModError("plain main is not the matching NSO")
    flags = struct.unpack_from("<I", plain, 0x0C)[0]
    if flags & 0x87:
        raise ModError("main must be fully unpacked before applying an IPS")
    for index, segment in enumerate(("text", "rodata", "data")):
        off, _, size = struct.unpack_from("<III", plain, 0x10 + index * 0x10)
        if not size:
            continue
        if off < 0x100 or off + size > len(plain):
            raise ModError(f"invalid uncompressed main .{segment} layout")
        if flags & (8 << index) and digest(plain[off:off + size]) != plain[0xA0 + 32 * index:0xC0 + 32 * index].hex():
            raise ModError(f"original main .{segment} SHA-256 mismatch")
    if not enabled:
        return plain
    backend = native_builder()
    with tempfile.TemporaryDirectory(prefix="fefw-patch-check-") as scratch:
        path = Path(scratch) / "main"
        path.write_bytes(plain)
        info = main_info(path)
        patches = []
        for mod_id in sorted(enabled):
            manifest, expected = packages[mod_id]
            if manifest["type"] not in PATCH_TYPES:
                continue
            _, actual = validate_manifest(manifest, mods_dir / mod_id, info)
            if actual != expected:
                raise ModError(f"{mod_id}: patch writes changed during build")
            patches.append((mod_id, f"{mod_id}/manifest.json", [(a, data) for a, _, data in actual]))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                backend.apply_exefs_patches(str(path), patches, compatible_entries=
                                           backend.compatible_manifest_entries(mods_dir.parent, info))
        except backend.Fail as exc:
            raise ModError(str(exc)) from exc
        return path.read_bytes()


def import_mod(source, mods_dir, info, mod_id=None, name=None, version="1.0.0"):
    if source.is_dir() and (source / "manifest.json").is_file():
        manifest = read_json(source / "manifest.json")
        root = source
    elif source.is_dir() or (source.is_file() and source.suffix.lower() in PATCH_SUFFIXES):
        manifest, root = native_manifest(source, info, mod_id, name, version)
    else:
        raise ModError(f"expected a mod directory or .ips/.ips32/.pchtxt file: {source}")
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
    return {"status": "imported", "id": manifest["id"], "ignored_files": manifest.get("ignored_files", [])}


def list_mods(mods_dir, info):
    packages = installed(mods_dir, info)
    enabled = read_state(mods_dir)["enabled"]
    check_conflicts(packages, enabled)
    return {"game_build_id": info["game_build_id"], "patch_set_sha256": fingerprint(packages, enabled, info["game_build_id"]),
            "mods": [{"id": key, "name": value[0]["name"], "version": value[0]["version"], "type": value[0]["type"], "enabled": key in enabled,
                      "ignored_files": value[0].get("ignored_files", []),
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
