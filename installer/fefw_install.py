#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Installs fefw-recomp on SteamOS and similar Linux systems, in Desktop Mode: the release package, Decky Loader (with
its own installer), the Decky plugin, and the Steam shortcut. Then opens the launcher, where the player chooses their game files and
builds the game. Every step that changes something asks first. Running it again finishes or repairs an earlier run: the game's files
in an install that is there are left alone.

Python's standard library only. A release carries it as fefw-recomp-install.py, which fefw-recomp-install.desktop downloads and runs.

    fefw_install.py                     from the latest release
    fefw_install.py --packages DIR      from the packages in DIR (for testing a build that is not published)
    fefw_install.py --text              in the terminal, also where zenity could show dialogs

A desktop without zenity gets a terminal window.
"""

import argparse
import contextlib
import datetime
import hashlib
import html
import json
import os
import pwd
import secrets
import shutil
import signal
import string
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import zipfile
from pathlib import Path, PurePosixPath

REPO = "metrist38/fefw-recomp"  # a release replaces it with its own repository
APPIMAGE = "fefw-launcher.AppImage"
PLUGIN_FOLDER = "fefw-recomp"
PLUGIN_NAME = "FEFW Recomp"
DECKY_SCRIPT = "https://github.com/SteamDeckHomebrew/decky-installer/releases/latest/download/user_install_script.sh"
NEEDED_BYTES = 30 * 10**9  # an install with the game's files unpacked and built
PACKAGE_END, PLUGIN_END = "-linux-x64.tar.gz", "-decky.zip"
# for the launcher's questions: a launcher too old for its commands then cannot open a window, and ends instead
NO_DISPLAY = "fefw-installer-no-display"

# Run as root with: the unpacked plugin, Decky's plugin folder. The old plugin is moved aside first, so a failed copy leaves it.
INSTALL_PLUGIN = """set -eu
dest="$2/fefw-recomp"
rm -rf "$dest.new" "$dest.old"
cp -R "$1" "$dest.new"
chown -R root:root "$dest.new"
chmod -R u=rwX,go=rX "$dest.new"
if [ -e "$dest" ]; then mv "$dest" "$dest.old"; fi
mv "$dest.new" "$dest"
rm -rf "$dest.old"
systemctl restart plugin_loader
"""


class Stop(Exception):
    """The installer cannot go on; the message says why."""


def plain(text):
    return str(text).strip("\n")


class Terminal:
    """The questions, and what is said, in a terminal. A closed window must not stop the clean-up, so printing never fails."""

    asks_passwords = False  # sudo and passwd ask for them themselves

    def __init__(self, read=input, write=print):
        self.read, self.write = read, write

    def say(self, text=""):
        with contextlib.suppress(OSError, ValueError):
            self.write(text)

    def alert(self, text):
        """Something that must not be missed."""
        self.say(f"\n{plain(text)}")

    def show_secret(self, text):
        self.alert(text)

    def close(self):
        pass

    def line(self, question):
        try:
            return self.read(question).strip()
        except EOFError:
            raise Stop("No answer: run the installer in a terminal.") from None

    def yes(self, question, default=True):
        while True:
            answer = self.line(f"\n{plain(question)} {'[Y/n]' if default else '[y/N]'} ").lower()
            if not answer:
                return default
            if answer in ("y", "yes", "n", "no"):
                return answer.startswith("y")
            self.say("Type y or n.")

    def choose(self, question, choices, cancel=None):
        """The number of a choice; Enter takes the first. (`cancel` is for dialogs, which have a button for it.)"""
        self.say(f"\n{plain(question)}")
        for number, choice in enumerate(choices, 1):
            self.say(f"  {number}. {choice}")
        while True:
            answer = self.line(f"Choose 1-{len(choices)} [1]: ") or "1"
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                return int(answer)

    def folder(self, default):
        """The folder for the game, as typed."""
        self.say("\nWhere should the game go? If it is installed already, type that folder, so that it is not installed twice.")
        return self.line(f"Folder [{default}]: ").strip("'\"") or str(default)

    @contextlib.contextmanager
    def busy(self, text):
        """Around something that takes a while."""
        self.say(f"\n{plain(text)}")
        yield

    def transfer(self, argv, path, size, name, run):
        """Runs a download of `size` bytes into `path`, showing how far it is. curl's exit code."""
        self.say(f"\nDownloading {name} ({size / 1e6:.0f} MB)")
        return run([*argv, "--progress-bar"]).returncode


class ProgressWindow:
    def __init__(self, process):
        self.process = process

    def send(self, line):
        with contextlib.suppress(OSError, ValueError):
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()

    def gone(self):
        """Whether the player closed it, or pressed Cancel."""
        return self.process.poll() is not None

    def close(self):
        with contextlib.suppress(OSError, ValueError):
            self.process.stdin.close()
        end(self.process)


def end(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class Zenity:
    """The same in zenity's dialogs. What is said is shown with the next dialog."""

    asks_passwords = True
    TITLE = "FEFW Recomp installer"
    WIDTH = 560

    def __init__(self, run=subprocess.run, start=subprocess.Popen, pause=time.sleep):
        self.run, self.start, self.pause = run, start, pause
        self.said = []

    def say(self, text=""):
        if plain(text):
            self.said.append(plain(text))

    def _text(self, text=""):
        parts, self.said = [*self.said, plain(text)], []
        # zenity reads its text as markup
        return html.escape("\n\n".join(p for p in parts if p), quote=False)

    def _dialog(self, kind, *arguments, **options):
        """(whether it was accepted, what it printed)."""
        argv = ["zenity", kind, f"--title={self.TITLE}", f"--width={self.WIDTH}", *arguments]
        done = self.run(argv, capture_output=True, text=True, **options)
        if done.returncode not in (0, 1):
            raise Stop(f"A dialog failed: {done.stderr.strip() or done.returncode}")
        return done.returncode == 0, done.stdout.rstrip("\n")

    def alert(self, text):
        self._dialog("--info", f"--text={self._text(text)}")

    def show_secret(self, text):
        # on stdin: any program on the system can read another's command line
        self._dialog("--text-info", "--height=220", input=f"{plain(text)}\n")

    def close(self):
        if self.said:
            self.alert("")

    def yes(self, question, default=True):
        extra = [] if default else ["--default-cancel"]
        return self._dialog("--question", "--ok-label=Yes", "--cancel-label=No", *extra, f"--text={self._text(question)}")[0]

    def choose(self, question, choices, cancel=None):
        rows = []
        for number, choice in enumerate(choices, 1):
            rows += ["TRUE" if number == 1 else "FALSE", str(number), choice]
        text = self._text(question)
        while True:
            accepted, picked = self._dialog("--list", "--radiolist", f"--text={text}", "--column=", "--column=Number",
                                            "--column=Choice", "--hide-column=2", "--print-column=2", "--hide-header",
                                            "--height=360", *rows)
            if not accepted:
                if cancel is None:
                    raise Stop("Stopped. Run the installer again to finish.")
                return cancel
            if picked.isdigit() and 1 <= int(picked) <= len(choices):
                return int(picked)

    def folder(self, default):
        while True:
            choice = self.choose("Where should the game go?",
                                 [str(default), "Another folder, or the one the game is installed in already"])
            if choice == 1:
                return str(default)
            accepted, picked = self._dialog("--file-selection", "--directory")
            if accepted and picked:
                folder = Path(picked)
                with contextlib.suppress(OSError):
                    # a drive or a games folder: the game gets a folder of its own in it
                    if not is_install(folder) and any(folder.iterdir()):
                        folder = folder / "fefw-recomp"
                return str(folder)

    def password(self, prompt):
        """What was typed, or None when the player cancelled."""
        accepted, typed = self._dialog("--entry", "--hide-text", f"--text={self._text(prompt)}")
        return typed if accepted else None

    def _window(self, text, *arguments):
        argv = ["zenity", "--progress", f"--title={self.TITLE}", f"--width={self.WIDTH}", "--text=Please wait", *arguments]
        window = ProgressWindow(self.start(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           text=True))
        # on stdin, as it may name a password; one line, in which zenity reads \n as a new one
        text = html.escape(plain(text), quote=False).replace("\\", "\\\\").replace("\n", "\\n")
        window.send(f"# {text}")
        return window

    @contextlib.contextmanager
    def busy(self, text):
        window = self._window(text, "--pulsate", "--no-cancel")
        try:
            yield
        finally:
            window.close()

    def transfer(self, argv, path, size, name, run):
        window = self._window(f"Downloading {name}", "--percentage=0")
        download = None
        try:
            download = self.start([*argv, "--silent"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while download.poll() is None:
                have = path.stat().st_size if path.is_file() else 0
                window.send(f"# Downloading {name}: {have / 1e6:.0f} of {size / 1e6:.0f} MB")
                # 100 would tell zenity it has finished
                window.send(str(min(99, have * 100 // size) if size else 0))
                if window.gone():
                    raise Stop("The download was cancelled. Run the installer again to continue it.")
                self.pause(0.5)
            return download.returncode
        finally:
            if download is not None:
                end(download)
            window.close()


def is_install(folder):
    """A release package's folder, as the plugin knows it."""
    folder = Path(folder)
    return (folder / APPIMAGE).is_file() and (folder / "tools" / "build" / "fefw_build.py").is_file()


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def end_line(output):
    """A launcher command's last line, or None when it gave none (a launcher without the command)."""
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("event") == "end" and value.get("api") == 1:
            return value
    return None


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def release_assets(release):
    """{"package": asset, "plugin": asset or None} of a GitHub release's answer."""
    def find(end):
        return next((a for a in release.get("assets") or []
                     if str(a.get("name", "")).startswith("fefw-recomp-") and a["name"].endswith(end)), None)

    return {"package": find(PACKAGE_END), "plugin": find(PLUGIN_END)}


def download(asset, folder, ui, run=subprocess.run):
    """The asset, downloaded into `folder` and checked against GitHub's SHA-256 of it. A part file is continued."""
    name = Path(asset["name"]).name
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:") or len(digest) != len("sha256:") + 64:
        raise Stop(f"GitHub lists no checksum for {name}, so it cannot be verified.")
    want, size = digest[len("sha256:"):].lower(), int(asset.get("size") or 0)
    path = Path(folder) / name
    have = path.stat().st_size if path.is_file() else 0
    if have > size or (have == size and have and sha256_file(path) != want):
        path.unlink()
        have = 0
    if not (have and have == size):
        argv = ["curl", "--fail", "--location", "--show-error", "--retry", "3", "--continue-at", "-", "--output", str(path),
                asset["browser_download_url"]]
        if ui.transfer(argv, path, size, name, run):
            raise Stop(f"The download of {name} failed. Run the installer again to continue it.")
    if sha256_file(path) != want:
        path.unlink()
        raise Stop(f"{name} does not match its checksum and was deleted. Run the installer again.")
    return path


def _inside(name):
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and path.parts != ()


def unpack_package(archive, dest):
    """Unpacks the release's tarball as the folder `dest`, which is not there or is empty. All of it or nothing."""
    dest = Path(dest)
    stage = Path(tempfile.mkdtemp(prefix=".fefw-unpack-", dir=dest.parent))
    try:
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            tops = {PurePosixPath(m.name).parts[0] for m in members if _inside(m.name)}
            if len(tops) != 1 or not all(_inside(m.name) and (m.isfile() or m.isdir()) for m in members):
                raise Stop(f"{Path(archive).name} is not a fefw-recomp package.")
            safer = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(stage, members=members, **safer)
        top = stage / tops.pop()
        if not is_install(top):
            raise Stop(f"{Path(archive).name} is not a fefw-recomp package.")
        (top / APPIMAGE).chmod(0o755)
        if dest.is_dir():
            dest.rmdir()  # fails if something appeared in it
        top.rename(dest)
    except (OSError, tarfile.TarError) as e:
        raise Stop(f"Cannot unpack into {dest}: {e}") from None
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def unpack_plugin(archive, stage):
    """Unpacks the plugin's zip into `stage` and returns its folder."""
    wrong = Stop(f"{Path(archive).name} is not the FEFW Recomp plugin.")
    try:
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if not _inside(name) or PurePosixPath(name).parts[0] != PLUGIN_FOLDER:
                    raise wrong
                if name.endswith("/"):
                    continue
                path = Path(stage) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(z.read(name))
    except (OSError, zipfile.BadZipFile) as e:
        raise Stop(f"Cannot unpack {Path(archive).name}: {e}") from None
    folder = Path(stage) / PLUGIN_FOLDER
    if read_json(folder / "plugin.json").get("name") != PLUGIN_NAME or not (folder / "main.py").is_file():
        raise wrong
    return folder


def same_files(new, installed):
    """Whether every file of `new` is in `installed`, byte for byte."""
    try:
        return all((installed / p.relative_to(new)).read_bytes() == p.read_bytes() for p in new.rglob("*") if p.is_file())
    except OSError:
        return False


class Admin:
    """Running one command as root, whatever the account's password situation is. A temporary password is removed again."""

    SUDO = ["sudo", "-S", "-k", "-p", ""]  # the password on stdin, and asked for every time
    LOCKED = "Your account is locked for a few minutes because of wrong passwords. Wait ten minutes."

    def __init__(self, ui, run=subprocess.run, user=None, faillock_conf="/etc/security/faillock.conf"):
        self.ui, self.run = ui, run
        self.user = user or pwd.getpwuid(os.getuid()).pw_name
        self.faillock_conf = Path(faillock_conf)

    def password_state(self):
        """"P" (a password is set), "NP" (none), another of passwd's letters, or "" when it cannot tell."""
        done = self.run(["passwd", "-S"], capture_output=True, text=True, env=dict(os.environ, LC_ALL="C"))
        words = done.stdout.split() if done.returncode == 0 else []
        return {"PS": "P"}.get(words[1], words[1]) if len(words) > 1 else ""

    def locked_out(self, now=None):
        """Whether wrong passwords have locked the account for the moment (pam_faillock): sudo then refuses the right one too."""
        limits = {"deny": 3, "unlock_time": 600, "fail_interval": 900}  # pam_faillock's own
        with contextlib.suppress(OSError):
            for line in self.faillock_conf.read_text(errors="replace").splitlines():
                name, _, value = line.partition("=")
                if name.strip() in limits and value.strip().isdigit():
                    limits[name.strip()] = int(value)
        try:
            done = self.run(["faillock", "--user", self.user], capture_output=True, text=True)
        except OSError:
            return False
        failures = []
        for line in done.stdout.splitlines():
            words = line.split()
            if len(words) > 2 and words[-1] == "V":
                with contextlib.suppress(ValueError):
                    failures.append(datetime.datetime.strptime(f"{words[0]} {words[1]}", "%Y-%m-%d %H:%M:%S"))
        if not failures or not limits["deny"]:
            return False
        last = max(failures)
        recent = sum((last - f).total_seconds() <= limits["fail_interval"] for f in failures)
        # an unlock time of 0: locked until someone resets it
        waiting = not limits["unlock_time"] or ((now or datetime.datetime.now()) - last).total_seconds() < limits["unlock_time"]
        return recent >= limits["deny"] and waiting

    def command(self, argv, doing):
        """Runs `argv` as root; `doing` says what is happening meanwhile. False when the player chose not to, or it failed."""
        if self.run(["sudo", "-n", "true"], capture_output=True).returncode == 0:
            with self.ui.busy(doing):
                return self.run(["sudo", *argv]).returncode == 0
        state = self.password_state()
        if state == "NP":
            choice = self.ui.choose(
                "This step needs administrator rights, and your account has no password yet.",
                ["Use a temporary password, removed again right after this step",
                 "Set your own password now (it stays)",
                 "Skip this step"], cancel=3)
            if choice == 1:
                if self.locked_out():
                    self.ui.say(f"{self.LOCKED} None was set. It could not be removed meanwhile.")
                    return False
                return self._with_temporary_password(argv, doing)
            if choice == 3:
                return False
            if self.ui.asks_passwords:
                password = self._set_own_password()
                return password is not None and self._with_password(argv, doing, password)
            if self.run(["passwd"]).returncode:
                self.ui.say("No password was set.")
                return False
        elif state != "P":
            self.ui.say(f"This step needs administrator rights, and the account {self.user} has no password that can be used "
                        "for them.")
            return False
        if self.ui.asks_passwords:
            password = self._ask_password()
            return password is not None and self._with_password(argv, doing, password)
        self.ui.say("\nThis step needs administrator rights.")
        if self.run(["sudo", "-v"]).returncode:
            return False
        with self.ui.busy(doing):
            return self.run(["sudo", *argv]).returncode == 0

    def _with_password(self, argv, doing, password):
        with self.ui.busy(doing):
            return self.run([*self.SUDO, *argv], input=f"{password}\n", text=True).returncode == 0

    def _ask_password(self):
        """The account's password, once sudo has accepted it; None when the player cancelled or it did not work."""
        prompt = "This step needs administrator rights. Type your password."
        for _ in range(3):
            if self.locked_out():
                self.ui.say(f"{self.LOCKED} Run the installer again then.")
                return None
            password = self.ui.password(prompt)
            if password is None:
                return None
            done = self.run([*self.SUDO, "true"], input=f"{password}\n", text=True, capture_output=True)
            if done.returncode == 0:
                return password
            why = done.stderr.strip().splitlines()
            prompt = f"That did not work{': ' + why[-1] if why else ''}\nType your password again."
        self.ui.say("The password was not accepted.")
        return None

    def _set_own_password(self):
        """Sets the password the player types twice, and returns it; None when they cancelled."""
        prompt = "Choose a password for your account. It stays: remember it."
        while True:
            first = self.ui.password(prompt)
            if first is None:
                return None
            again = self.ui.password("Type the new password again.")
            if again is None:
                return None
            if not first or first != again:
                prompt = "The two did not match. Choose a password for your account."
                continue
            done = self.run(["passwd"], input=f"{first}\n{first}\n", text=True, capture_output=True)
            if done.returncode == 0 and self.password_state() == "P":
                return first
            prompt = "The system did not accept that password. Choose another."

    def _with_temporary_password(self, argv, doing):
        # letters and digits, with each kind present for a strict password rule
        alphabet = string.ascii_letters + string.digits
        password = "".join(secrets.choice(alphabet) for _ in range(17)) + "aZ7"
        notice = (f"{doing}\n\nTemporary password: {password}\n"
                  "It is removed when this step ends. If the installer is killed before that, remove it with this command, which "
                  f"asks for the password:\nsudo passwd -d {self.user}")
        left = False
        try:
            with self.ui.busy(notice):
                try:
                    if self.run(["passwd"], input=f"{password}\n{password}\n", text=True, capture_output=True).returncode:
                        self.ui.say("The temporary password could not be set.")
                        return False
                    return self.run([*self.SUDO, *argv], input=f"{password}\n", text=True).returncode == 0
                finally:
                    left = not self._remove_password(password)
        finally:
            if left:
                self.ui.show_secret(f"*** The temporary password is still set: {password}\n"
                                    f"*** Remove it with:  sudo passwd -d {self.user}")

    def _remove_password(self, password):
        """Whether the account is without a password again."""
        # nothing may interrupt this: signals wait, and sudo runs in a session of its own, which a closing window does not reach
        held = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            if self.password_state() == "NP":
                return True
            self.run([*self.SUDO, "passwd", "-d", self.user], input=f"{password}\n", text=True, capture_output=True,
                     start_new_session=True)
            if self.password_state() != "NP":
                return False
            self.ui.say("The temporary password was removed.")
            return True
        finally:
            for number, handler in held.items():
                signal.signal(number, handler)


class Installer:
    def __init__(self, ui, home, packages=None, run=subprocess.run, start=subprocess.Popen, env=None, user=None):
        self.ui, self.say = ui, ui.say
        self.home = Path(home)
        self.packages = Path(packages) if packages else None
        self.run, self.start = run, start
        self.env = dict(os.environ if env is None else env)
        self.admin = Admin(ui, run, user)
        self.decky = self.home / "homebrew"
        self.plugin_settings = self.decky / "settings" / PLUGIN_FOLDER / "settings.json"
        # there while Decky may not have loaded what is in its plugin folder: the files alone do not show that
        self.restart_pending = self.home / ".cache" / "fefw-recomp-install" / "restart-pending"
        self.summary = []
        self._release = None

    # ---- what is on this system ----

    def steam_root(self):
        for folder in (".steam/steam", ".local/share/Steam", ".var/app/com.valvesoftware.Steam/.local/share/Steam"):
            if (self.home / folder / "userdata").is_dir():
                return self.home / folder
        return None

    def has_proton(self, steam):
        libraries = [steam]
        with contextlib.suppress(OSError):
            for line in (steam / "steamapps" / "libraryfolders.vdf").read_text(errors="replace").splitlines():
                words = line.split('"')
                if len(words) >= 4 and words[1] == "path":
                    libraries.append(Path(words[3]))
        folders = [library / "steamapps" / "common" for library in libraries]
        for folder in folders + [steam / "compatibilitytools.d"]:
            with contextlib.suppress(OSError):
                if any("proton" in entry.name.lower() and entry.is_dir() for entry in folder.iterdir()):
                    return True
        return False

    # ---- the steps ----

    def release(self):
        """The assets to install: of the latest release, or the files in --packages."""
        if self._release is None:
            if self.packages:
                local = {}
                for kind, end in (("package", PACKAGE_END), ("plugin", PLUGIN_END)):
                    found = sorted(self.packages.glob("*" + end))
                    if len(found) > 1:
                        raise Stop(f"More than one *{end} in {self.packages}.")
                    local[kind] = found[0] if found else None
                self._release = local
            else:
                url = f"https://api.github.com/repos/{REPO}/releases/latest"
                done = self.run(["curl", "--fail", "--location", "--silent", "--show-error", "--max-time", "30", "--retry", "2",
                                 "--user-agent", "fefw-installer", "--header", "Accept: application/vnd.github+json", url],
                                capture_output=True, text=True)
                try:
                    answer = json.loads(done.stdout) if done.returncode == 0 else None
                except ValueError:
                    answer = None
                if not isinstance(answer, dict):
                    raise Stop(f"Could not get the latest release from GitHub. {done.stderr.strip()}")
                self._release = release_assets(answer)
        return self._release

    def fetch(self, kind, folder):
        """The release's `kind` as a file, downloaded into `folder` if need be. None when the release has none."""
        asset = self.release()[kind]
        if asset is None or self.packages:
            return asset
        return download(asset, folder, self.ui, self.run)

    def choose_folder(self):
        default = self.home / "Games" / "fefw-recomp"
        for known in (read_json(self.plugin_settings).get("install_dir"), default):
            if known and is_install(known):
                self.say(f"\nThe game is installed in {known}.")
                if self.ui.yes("Use that install?"):
                    return Path(known)
                break
        while True:
            folder = Path(self.ui.folder(default))
            if str(folder) == "~" or str(folder).startswith("~/"):
                folder = self.home / str(folder)[2:]
            if not folder.is_absolute():
                self.say("Type the whole path, starting with / or ~/.")
            elif '"' in str(folder):
                self.say('A folder with a " in its path cannot be added to Steam.')
            elif is_install(folder):
                return folder
            elif folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
                self.say("That folder has other files in it. Choose a new or empty folder, or the one the game is installed in.")
            else:
                return folder

    def install_game(self, folder):
        parent = folder.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise Stop(f"Cannot create {parent}: {e}") from None
        free = shutil.disk_usage(parent).free
        if free < NEEDED_BYTES:
            short = f"Only {free / 1e9:.0f} GB is free there. The game needs about {NEEDED_BYTES / 1e9:.0f} GB once it is built."
            if not self.ui.yes(f"{short}\nInstall anyway?", default=False):
                raise Stop("Free some space, or choose a folder on another drive.")
        if not self.ui.yes(f"Install the game into {folder}?"):
            raise Stop("Nothing was installed.")
        downloads = parent / ".fefw-recomp-download"
        downloads.mkdir(exist_ok=True)
        archive = self.fetch("package", downloads)
        if archive is None:
            raise Stop("The release has no Linux package.")
        with self.ui.busy("Unpacking the game"):
            unpack_package(archive, folder)
        shutil.rmtree(downloads, ignore_errors=True)
        self.summary.append(f"The game is in {folder}.")

    def own_scope(self):
        """What goes before a program that must outlive this window (the launcher, and a Steam it restarts): a systemd scope of
        its own, where the session can make one."""
        prefix = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
        try:
            done = self.run([*prefix, "true"], env=self.env, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return []
        return prefix if done.returncode == 0 else []

    def launcher(self, folder, *arguments, display=True, timeout=300):
        """The end line of one of the launcher's commands, or None."""
        argv = [str(folder / APPIMAGE), *arguments]
        env = dict(self.env)
        if not display:
            env.pop("DISPLAY", None)
            env["WAYLAND_DISPLAY"] = NO_DISPLAY
        else:
            argv = [*self.own_scope(), *argv]
        try:
            done = self.run(argv, cwd=str(folder), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, errors="replace", timeout=timeout, start_new_session=True)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return end_line(done.stdout)

    def open_launcher(self, folder):
        argv = [*self.own_scope(), str(folder / APPIMAGE)]
        try:
            self.start(argv, cwd=str(folder), env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self.say(f"The launcher did not start: {e}")

    def install_decky(self):
        """Whether Decky Loader is there, after offering its own installer when it is not."""
        loader = self.decky / "services" / "PluginLoader"
        if loader.is_file():
            return True
        self.say("\nDecky Loader is not installed. Its plugin lets you update and build the game from Game Mode.")
        if not self.ui.yes("Install Decky Loader with its own installer? It asks its own questions."):
            return False
        with tempfile.TemporaryDirectory(prefix="fefw-decky-") as stage:
            script = Path(stage) / "user_install_script.sh"
            got = self.run(["curl", "--fail", "--location", "--silent", "--show-error", "--connect-timeout", "60",
                            "--output", str(script), DECKY_SCRIPT])
            if got.returncode == 0:
                self.run(["bash", str(script)], env=self.env, cwd=stage)
        if not loader.is_file():
            self.say("Decky Loader was not installed. Run this installer again to try once more.")
            return False
        self.summary.append("Decky Loader was installed.")
        return True

    def save_install_dir(self, folder):
        """Tells the plugin where the game is, keeping its other settings."""
        settings = read_json(self.plugin_settings)
        settings["install_dir"] = str(folder)
        try:
            self.plugin_settings.parent.mkdir(parents=True, exist_ok=True)
            part = self.plugin_settings.with_suffix(".part")
            part.write_text(json.dumps(settings), encoding="utf-8")
            part.replace(self.plugin_settings)
        except OSError:
            return False
        return True

    @staticmethod
    def _restore(path, content):
        with contextlib.suppress(OSError):
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)

    def install_plugin(self, folder):
        installed = self.decky / "plugins" / PLUGIN_FOLDER
        with tempfile.TemporaryDirectory(prefix="fefw-plugin-") as stage:
            archive = self.fetch("plugin", stage)
            if archive is None:
                self.say("\nThe release has no Decky plugin, so none was installed.")
                return
            new = unpack_plugin(archive, stage)
            current = same_files(new, installed)
            moved = read_json(self.plugin_settings).get("install_dir") != str(folder)
            pending = self.restart_pending.is_file()
            if current and not moved and not pending:
                self.say("\nThe Decky plugin is up to date.")
                return
            version = read_json(new / "package.json").get("version", "?")
            old = read_json(installed / "package.json").get("version")
            if current and moved:
                question = f"Point the Decky plugin at {folder}?"
            elif current:
                question = "An earlier run did not get Decky to restart. Restart it now, to load the plugin?"
            elif old:
                question = f"Replace the Decky plugin ({old}) with the release's ({version})?"
            else:
                question = f"Install the Decky plugin ({version})?"
            if not self.ui.yes(question):
                return
            # before the loader restarts, which reads it
            before = self.plugin_settings.read_bytes() if self.plugin_settings.is_file() else None
            saved = self.save_install_dir(folder)
            if current:
                argv = ["systemctl", "restart", "plugin_loader"]
            else:
                argv = ["sh", "-c", INSTALL_PLUGIN, "sh", str(new), str(installed.parent)]
            with contextlib.suppress(OSError):
                self.restart_pending.parent.mkdir(parents=True, exist_ok=True)
                self.restart_pending.touch()
            done = False
            try:
                done = self.admin.command(argv, "Installing the Decky plugin")
            finally:
                if saved and not done:  # as it was, so that the next run sees there is still something to do
                    self._restore(self.plugin_settings, before)
            if not done:
                self.say("The plugin could not be installed and loaded. Run this installer again to try once more.")
                return
            with contextlib.suppress(OSError):
                self.restart_pending.unlink()
        self.summary.append("The Decky plugin is installed: open it from the quick access menu in Game Mode.")
        if not saved:
            self.summary.append(f"In the plugin, choose the game's folder: {folder}")

    def add_to_steam(self, folder, status):
        added = bool(status.get("steam_shortcuts"))
        if added:
            question, default = "The game is in Steam already. Refresh its shortcut and artwork?", False
        else:
            question, default = "Add the game to Steam?", True
        if not self.ui.yes(f"{question} Steam closes and starts again, so close any game that is running.", default):
            return
        with self.ui.busy("Adding the game to Steam. Steam can take a minute to close."):
            answer = self.launcher(folder, "--add-to-steam", "--before-setup", "--close-steam")
        if answer is None or answer.get("state") != "done":
            self.say((answer or {}).get("message") or "The launcher could not add the game to Steam.")
            self.summary.append("The game was not added to Steam: run this installer again, or use the launcher's Play page.")
            return
        self.say(answer.get("message", ""))
        self.summary.append("The game is in your Steam library.")

    def go(self):
        say = self.say
        say("This installs the recomp. It contains no game code or data: you need your own dump of the game.")
        if "gamescope" in self.env.get("XDG_CURRENT_DESKTOP", "").lower():
            raise Stop("Run the installer in Desktop Mode: it closes Steam, which Game Mode cannot do without.")
        steam = self.steam_root()
        if steam is None:
            say("\nSteam was not found (or nobody has signed in to it), so the Steam and Decky steps are skipped.")
        elif not self.has_proton(steam):
            say("\nNo Proton was found. Before building, install \"Proton Experimental\" from your Steam library.")

        folder = self.choose_folder()
        if not is_install(folder):
            self.install_game(folder)
        else:
            say("Its files are left as they are. Updates come from the plugin, or the launcher's About page.")

        with self.ui.busy("Checking the game"):
            status = self.launcher(folder, "--status", display=False, timeout=120)
        if status is None or status.get("state") != "done":
            say(f"\nThe launcher in {folder} is older than this installer, or does not start.")
            if self.ui.yes("Open it, to update it on its About page?"):
                self.open_launcher(folder)
            raise Stop("Run this installer again after updating.")
        if status.get("busy"):
            what = "The game is running" if status["busy"] == "game" else "The launcher or the plugin is working on the game"
            raise Stop(f"{what}. Run this installer again when that has finished.")

        if steam is not None:
            # Decky first: Steam has to start again for it, which adding the game does
            if self.install_decky():
                self.install_plugin(folder)
            self.add_to_steam(folder, status)

        say("\n".join(["\nDone.", *(f"  {line}" for line in self.summary)]))
        if not status.get("setup_complete"):
            if self.ui.yes("The game is not built yet: in the launcher, choose your game files and press Set up.\n\n"
                           "Open the launcher now?"):
                self.open_launcher(folder)


def front_end(text, env, ui_run=subprocess.run):
    """Dialogs on a desktop with zenity, else the terminal."""
    if not text and (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")) and shutil.which("zenity", path=env.get("PATH")):
        return Zenity(ui_run)
    return Terminal()


def in_a_terminal(argv, env, tty):
    """The command that runs the installer again in a terminal window, for a desktop without zenity when it was started without
    one (the download file does that). None when there is no need, or no terminal program to be found."""
    if tty or not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return None
    for terminal in (["konsole", "--hold", "-e"], ["gnome-terminal", "--"], ["xdg-terminal-exec"]):
        if shutil.which(terminal[0], path=env.get("PATH")):
            return [*terminal, sys.executable, "-I", str(Path(__file__).resolve()), *argv, "--text"]
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description="Installs fefw-recomp, its Decky plugin and its Steam shortcut.")
    parser.add_argument("--packages", metavar="DIR", help="install the packages in DIR instead of the latest release's")
    parser.add_argument("--text", action="store_true", help="ask in the terminal, not in dialogs")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("Run the installer as yourself, not as root: it asks for administrator rights where it needs them.")
        return 1
    ui = front_end(args.text, os.environ)
    if isinstance(ui, Terminal):
        # --text asks for this terminal; without it, a desktop that has no zenity gets one
        window = None if args.text else in_a_terminal(argv, os.environ, sys.stdin is not None and sys.stdin.isatty())
        if window:
            os.execvp(window[0], window)
        ui.say("FEFW Recomp installer")

    def stop(number, _frame):
        raise KeyboardInterrupt

    for number in (signal.SIGTERM, signal.SIGHUP):  # a closed window or a log-out: leave through the clean-up
        signal.signal(number, stop)
    try:
        Installer(ui, Path.home(), args.packages).go()
        ui.close()
    except Stop as e:
        return failed(ui, str(e))
    except KeyboardInterrupt:
        # no dialog: whatever sent the signal is not waiting for an answer
        print("\nStopped. Run the installer again to finish.", file=sys.stderr)
        return 130
    except Exception as e:  # without a terminal, a traceback would be seen by nobody
        traceback.print_exc()
        return failed(ui, f"Something went wrong: {e}\nRun the installer again to finish.")
    return 0


def failed(ui, message):
    with contextlib.suppress(Stop, OSError):
        ui.alert(message)
    return 1


if __name__ == "__main__":
    sys.exit(main())
