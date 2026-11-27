#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Packs the built plugin into a zip (run `pnpm run build` first).

package.py                     out/fefw-recomp-decky.zip, with package.json's version
package.py --version 0.4.0     out/fefw-recomp-0.4.0-decky.zip, a release's: Decky reads the version from package.json, so the zip's
                               copy gets it
"""

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# the folder Decky installs: dist, main.py, package.json, plugin.json and the license are its layout
FILES = ("dist/index.js", "main.py", "package.json", "plugin.json", "README.md", "py_modules/fefw_launcher.py")
FOLDER = "fefw-recomp"


def pack(here, license_file, out_dir, version=None):
    """Writes the zip and returns its path."""
    if version is not None and not re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", version):
        raise ValueError(f"not a version: {version!r}")
    files = {name: Path(here) / name for name in FILES}
    files["LICENSE"] = Path(license_file)
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing: " + ", ".join(missing))
    out = Path(out_dir) / ("fefw-recomp-decky.zip" if version is None else f"fefw-recomp-{version}-decky.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path in files.items():
            if name == "package.json" and version is not None:
                meta = json.loads(path.read_text(encoding="utf-8"))
                meta["version"] = version
                z.writestr(f"{FOLDER}/{name}", json.dumps(meta, indent=2) + "\n")
            else:
                z.write(path, f"{FOLDER}/{name}")
    return out


def main():
    parser = argparse.ArgumentParser(description="Packs the built Decky plugin.")
    parser.add_argument("--version", help="the release's version")
    args = parser.parse_args()
    try:
        print(pack(HERE, HERE.parent / "LICENSE", HERE / "out", args.version))
    except (ValueError, OSError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
