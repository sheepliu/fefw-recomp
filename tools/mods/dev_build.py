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
import tempfile
import uuid


MODULES = ("main", "sdk", "subsdk0", "subsdk1", "subsdk2", "rtld")
BUILD_FILES = (
    "game-build/release/fefw_build.py",
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


def release_builder(repo):
    script = Path(__file__).resolve().parents[2] / "game-build" / "release" / "fefw_build.py"
    spec = importlib.util.spec_from_file_location("fefw_release_builder", script)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    builder.RECOMP = str(repo / "recomp")
    return builder


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
        "sites": root / "main.sites",
        "libs": repo / "work" / "build" / ("fefw-mod-" + key),
        "gen": repo / "work" / "lift" / ("fefw-mod-" + key),
        "exe": repo / "work" / "build" / ("fefw-mod-exe-" + key) / "fefw.exe",
    }


def selection(repo, game):
    p = paths(repo, game)
    original = p["source"] / "main"
    builder = release_builder(repo)
    mp = builder.modpack()
    info = mp.main_info(original)
    build_id = info["game_build_id"]
    try:
        patches = builder.selected_patches(game, info)
        sites = builder.collect_mod_sites(game, build_id)
    except builder.Fail as exc:
        raise ValueError(str(exc)) from exc
    stamp = {
        "format": 2,
        "repo": str(repo.resolve()),
        "game_dir": str(game.resolve()),
        "game_build_id": info["game_build_id"],
        "original_sha256": sha256(original),
        "source_modules_sha256": {name: sha256(p["source"] / name) for name in MODULES[1:]},
        "patch_set_sha256": builder.exefs_patch_stamp(patches),
        "mod_sites_sha256": builder.mod_sites_stamp(sites),
        "builder_sha256": sha256(Path(__file__)),
        "modpack_sha256": sha256(Path(mp.__file__)),
        "build_files_sha256": {name: sha256(repo / name) for name in BUILD_FILES},
    }
    return builder, patches, sites, stamp


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
    builder, patches, sites, stamp = selection(repo, game)
    p["input"].mkdir(parents=True, exist_ok=True)
    for name in MODULES:
        source, target = p["source"] / name, p["input"] / name
        if name == "main":
            # Scratch image only; keep the stable input's mtime if the bytes did not change.
            with tempfile.TemporaryDirectory(dir=p["input"].parent) as scratch:
                candidate = Path(scratch) / "main"
                shutil.copyfile(source, candidate)
                try:
                    entries = builder.compatible_manifest_entries(game, builder.modpack().main_info(source))
                    builder.apply_exefs_patches(str(candidate), patches, sites, entries)
                    site_list = Path(builder.write_sites(str(candidate), sites, scratch))
                except builder.Fail as exc:
                    raise ValueError(str(exc)) from exc
                replace_bytes(p["sites"], site_list.read_bytes())
                replace_bytes(target, candidate.read_bytes())
            stamp["candidate_main_sha256"] = sha256(target)
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
        for name, key in (("main", "candidate_main_sha256"), ("fefw.exe", "exe_sha256"),
                          ("fefw.sites", "sites_sha256")):
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
    if any(not lib.is_file() for lib in libs) or libs[0].stat().st_mtime_ns < max(
            candidate.stat().st_mtime_ns, p["sites"].stat().st_mtime_ns):
        raise RuntimeError("candidate game libraries do not match the staged main")
    if exe.stat().st_mtime_ns < max(lib.stat().st_mtime_ns for lib in libs):
        raise RuntimeError("candidate executable predates its game libraries")

    p["active"].mkdir(parents=True, exist_ok=True)
    p["previous"].mkdir(parents=True, exist_ok=True)
    sites = exe.with_suffix(".sites")
    if not sites.is_file():
        raise RuntimeError("candidate executable is missing its hook site list")
    targets = [(candidate, p["active"] / "main"), (exe, p["active"] / "fefw.exe"),
               (sites, p["active"] / "fefw.sites")]
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
        done = dict(stamp, exe_sha256=sha256(targets[1][1]), sites_sha256=sha256(targets[2][1]))
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
    env = dict(os.environ, NX_BUILD=str(p["libs"]), NX_EXEFS=str(p["input"]), NX_GEN=str(p["gen"]),
               NX_SITES=str(p["sites"]), NX_JOBS=str(jobs))
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
