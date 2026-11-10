# SPDX-License-Identifier: GPL-3.0-or-later
"""Build checkout ExeFS mods without changing the dump or the existing game executable."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


MODULES = ("main", "sdk", "subsdk0", "subsdk1", "subsdk2", "rtld")
BUILD_FILES = (
    "recomp/scripts/build_game.cmd", "recomp/games/fefw/CMakeLists.txt",
    "recomp/games/fefw/main.sites", "game-build/scripts/build_fefw.ps1",
    "game-build/fefw/CMakeLists.txt",
    "runtime/games/fefw/CMakeLists.txt", "runtime/games/fefw/fefw_game.cpp",
    "runtime/games/fefw/fefw_game.h", "runtime/games/fefw/fefw_menu.cpp",
    "runtime/games/fefw/fefw_mods.cpp", "runtime/games/fefw/fefw_mods.h",
    "runtime/games/fefw/fefw_mods_menu.cpp", "runtime/games/fefw/fefw_mouse.cpp",
    "runtime/src/gfx/overlay.cpp",
    "runtime/src/gfx/play/play.cpp",
    "runtime/third_party/nlohmann/json.hpp",
)


def modpack():
    spec = importlib.util.spec_from_file_location("fefw_modpack", Path(__file__).with_name("modpack.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def paths(repo, game):
    key = hashlib.sha256(str(game.resolve()).encode("utf-8")).hexdigest()[:12]
    root = game / "mod-build"
    return {
        "source": repo / "game" / "dump" / "exefs",
        "input": root / "input-exefs",
        "active": root / "active",
        "previous": root / "previous",
        "done": root / "done.json",
        "libs": repo / "work" / "build" / ("fefw-mod-" + key),
        "gen": repo / "work" / "lift" / ("fefw-mod-" + key),
        "exe": repo / "work" / "build" / ("fefw-mod-exe-" + key) / "fefw.exe",
    }


def selection(repo, game):
    p = paths(repo, game)
    original = p["source"] / "main"
    mp = modpack()
    info = mp.main_info(original)
    packages = mp.installed(game / "mods", info)
    enabled = mp.read_state(game / "mods")["enabled"]
    mp.check_conflicts(packages, enabled)
    stamp = {
        "format": 1,
        "game_dir": str(game.resolve()),
        "game_build_id": info["game_build_id"],
        "original_sha256": sha256(original),
        "source_modules_sha256": {name: sha256(p["source"] / name) for name in MODULES[1:]},
        "patch_set_sha256": mp.fingerprint(packages, enabled, info["game_build_id"]),
        "builder_sha256": sha256(Path(__file__)),
        "modpack_sha256": sha256(Path(mp.__file__)),
        "build_files_sha256": {name: sha256(repo / name) for name in BUILD_FILES},
    }
    return mp, packages, enabled, stamp


def read_done(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def replace_bytes(path, data):
    if path.is_file() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def prepare(repo, game):
    p = paths(repo, game)
    mp, packages, enabled, stamp = selection(repo, game)
    for name in MODULES:
        source, target = p["source"] / name, p["input"] / name
        if name == "main":
            patched = mp.patched_main(source.read_bytes(), game / "mods", packages, enabled, stamp["game_build_id"])
            replace_bytes(target, patched)
            stamp["candidate_main_sha256"] = hashlib.sha256(patched).hexdigest()
        else:
            if not source.is_file():
                raise FileNotFoundError(source)
            if not target.is_file() or sha256(source) != sha256(target):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    return stamp


def check(repo, game):
    p = paths(repo, game)
    done = read_done(p["done"])
    if not isinstance(done, dict):
        return False, "no completed checkout mod build"
    try:
        _, _, _, selected = selection(repo, game)
        if any(done.get(key) != value for key, value in selected.items()):
            return False, "enabled patches or original main changed"
        for name, key in (("main", "candidate_main_sha256"), ("fefw.exe", "exe_sha256")):
            file = p["active"] / name
            if not file.is_file() or sha256(file) != done.get(key):
                return False, f"active {name} does not match the completed build"
    except (OSError, ValueError) as exc:
        return False, str(exc)
    return True, "checkout EXE and runtime main match enabled patches"


def publish(repo, game, stamp):
    p = paths(repo, game)
    _, _, _, selected = selection(repo, game)
    if any(stamp.get(key) != value for key, value in selected.items()):
        raise RuntimeError("mod selection or original main changed during build")
    candidate = p["input"] / "main"
    exe = p["exe"]
    if sha256(candidate) != stamp["candidate_main_sha256"] or not exe.is_file():
        raise RuntimeError("candidate main or executable is missing or changed")
    libs = [p["libs"] / f"fefw_{name}.lib" for name in MODULES]
    if any(not lib.is_file() or lib.stat().st_mtime_ns < candidate.stat().st_mtime_ns for lib in libs):
        raise RuntimeError("candidate game libraries do not match the staged main")
    if exe.stat().st_mtime_ns < max(lib.stat().st_mtime_ns for lib in libs):
        raise RuntimeError("candidate executable predates its game libraries")

    p["active"].mkdir(parents=True, exist_ok=True)
    p["previous"].mkdir(parents=True, exist_ok=True)
    targets = [(candidate, p["active"] / "main"), (exe, p["active"] / "fefw.exe")]
    old_done = p["done"].read_bytes() if p["done"].exists() else None
    backups = []
    for _, target in targets:
        if target.exists():
            backup = p["previous"] / (target.name + "-" + sha256(target)[:16] + ".bak")
            shutil.copy2(target, backup)
            backups.append(backup)
        else:
            backups.append(None)
    promoted = []
    try:
        for source, target in targets:
            temp = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
            try:
                shutil.copy2(source, temp)
                os.replace(temp, target)
                promoted.append(target)
            finally:
                temp.unlink(missing_ok=True)
        done = dict(stamp, exe_sha256=sha256(targets[1][1]))
        replace_bytes(p["done"], (json.dumps(done, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception:
        for target, backup in zip((item[1] for item in targets), backups):
            if target in promoted:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    shutil.copy2(backup, target)
        if old_done is None:
            p["done"].unlink(missing_ok=True)
        else:
            replace_bytes(p["done"], old_done)
        raise
    return done


def build(repo, game, jobs):
    stamp = prepare(repo, game)
    p = paths(repo, game)
    env = dict(os.environ, NX_BUILD=str(p["libs"]), NX_EXEFS=str(p["input"]), NX_GEN=str(p["gen"]), NX_JOBS=str(jobs))
    subprocess.run(["cmd.exe", "/d", "/c", str(repo / "recomp" / "scripts" / "build_game.cmd"), "fefw"],
                   cwd=repo, env=env, check=True)
    subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(repo / "game-build" / "scripts" / "build_fefw.ps1"), "-Jobs", str(jobs),
                    "-LibDir", str(p["libs"]), "-BuildDir", str(p["exe"].parent)], cwd=repo, check=True)
    return publish(repo, game, stamp)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "build"))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--game", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=10)
    args = parser.parse_args(argv)
    args.repo = args.repo.resolve()
    args.game = args.game.resolve()
    try:
        if args.command == "check":
            current, reason = check(args.repo, args.game)
            print(json.dumps({"current": current, "reason": reason}, ensure_ascii=False))
            return 0 if current else 1
        if args.jobs < 1:
            parser.error("--jobs must be positive")
        done = build(args.repo, args.game, args.jobs)
        print(json.dumps({"published": True, "exe_sha256": done["exe_sha256"],
                          "main_sha256": done["candidate_main_sha256"]}))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"checkout mod build: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
