import datetime
import hashlib
import io
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import fefw_install as fi  # noqa: E402

# A stand-in launcher: records its arguments and display, and answers like the real commands.
LAUNCHER = r"""#!/bin/sh
here=$(dirname "$0")
echo "$* DISPLAY=${DISPLAY-unset} WAYLAND_DISPLAY=${WAYLAND_DISPLAY-unset}" >> "$here/calls"
case "$1" in
--status)
    [ -e "$here/old" ] && exit 1
    busy=null; [ -e "$here/busy" ] && busy='"game"'
    built=false; [ -e "$here/built" ] && built=true
    ids='[]'; [ -e "$here/in-steam" ] && ids='[7]'
    echo "{\"event\":\"end\",\"api\":1,\"state\":\"done\",\"busy\":$busy,\"setup_complete\":$built,\"steam_shortcuts\":$ids}" ;;
--add-to-steam)
    : > "$here/in-steam"
    echo '{"event":"end","api":1,"state":"done","message":"Added.","steam_shortcuts":[7]}' ;;
*) : > "$here/opened" ;;
esac
"""


def script(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def package(folder, name="fefw-recomp-9.9.9", extra=()):
    path = folder / "fefw-recomp-9.9.9-linux-x64.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for member, text in ((f"{name}/fefw-launcher.AppImage", LAUNCHER), (f"{name}/tools/build/fefw_build.py", "# build\n"),
                             *extra):
            data = text.encode()
            info = tarfile.TarInfo(member)
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    return path


def plugin(folder, version="9.9.9", extra=(), name="FEFW Recomp"):
    path = folder / f"fefw-recomp-{version}-decky.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("fefw-recomp/plugin.json", json.dumps({"name": name}))
        z.writestr("fefw-recomp/package.json", json.dumps({"version": version}))
        z.writestr("fefw-recomp/main.py", "# plugin\n")
        z.writestr("fefw-recomp/dist/index.js", "// panel\n")
        for member, text in extra:
            z.writestr(member, text)
    return path


class Answers:
    """A terminal that answers from a list, and keeps what it was asked and told."""

    def __init__(self, *answers):
        self.answers, self.asked, self.told = list(answers), [], []
        self.terminal = fi.Terminal(self.read, self.told.append)

    def read(self, question):
        self.asked.append(question)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def text(self):
        return "\n".join(self.told)


class Temp(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)


class Packages(Temp):
    def test_package_is_unpacked_whole(self):
        dest = self.root / "game"
        dest.mkdir()
        fi.unpack_package(package(self.root), dest)
        self.assertTrue(fi.is_install(dest))
        self.assertTrue(os.access(dest / "fefw-launcher.AppImage", os.X_OK))
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".fefw")], [])

    def test_package_that_reaches_outside_is_refused(self):
        for bad in ("../escaped", "fefw-recomp-9.9.9/../../escaped", "/escaped", "other-folder/file"):
            with self.subTest(bad):
                archive = package(self.root, extra=[(bad, "x")])
                with self.assertRaises(fi.Stop):
                    fi.unpack_package(archive, self.root / "game")
                self.assertFalse((self.root / "game").exists())
                self.assertFalse((self.root / "escaped").exists())

    def test_folder_with_files_is_not_replaced(self):
        dest = self.root / "game"
        dest.mkdir()
        (dest / "mine").write_text("keep")
        with self.assertRaises(fi.Stop):
            fi.unpack_package(package(self.root), dest)
        self.assertEqual((dest / "mine").read_text(), "keep")

    def test_plugin_must_be_ours_and_stay_inside(self):
        self.assertTrue((fi.unpack_plugin(plugin(self.root), self.root / "a") / "dist" / "index.js").is_file())
        for archive in (plugin(self.root, "1.0.0", extra=[("fefw-recomp/../../escaped", "x")]),
                        plugin(self.root, "1.0.1", extra=[("elsewhere/file", "x")]),
                        plugin(self.root, "1.0.2", name="Another plugin")):
            with self.subTest(archive.name), self.assertRaises(fi.Stop):
                fi.unpack_plugin(archive, self.root / "b")
        self.assertFalse((self.root / "escaped").exists())

    def test_release_assets(self):
        names = ["fefw-recomp-1.2.3-win64.zip", "fefw-recomp-1.2.3-win64-symbols.zip", "fefw-recomp-1.2.3-linux-x64.tar.gz",
                 "fefw-recomp-1.2.3-decky.zip", "fefw-recomp-install.py"]
        found = fi.release_assets({"assets": [{"name": n} for n in names]})
        self.assertEqual((found["package"]["name"], found["plugin"]["name"]), (names[2], names[3]))
        self.assertEqual(fi.release_assets({"assets": []}), {"package": None, "plugin": None})


class Downloads(Temp):
    def asset(self, data=b"the package" * 1000, digest=None):
        source = self.root / "source"
        source.write_bytes(data)
        return {"name": "fefw-recomp-1-linux-x64.tar.gz", "size": len(data), "browser_download_url": source.as_uri(),
                "digest": "sha256:" + (digest or hashlib.sha256(data).hexdigest())}

    def test_downloads_and_verifies_and_does_not_fetch_twice(self):
        asset = self.asset()
        calls = []

        def run(argv, **options):
            calls.append(argv)
            return subprocess.run([a for a in argv if a != "--progress-bar"] + ["--silent"], **options)

        into = self.root / "into"
        into.mkdir()
        ui = Answers().terminal
        path = fi.download(asset, into, ui, run)
        self.assertEqual(path.read_bytes(), (self.root / "source").read_bytes())
        fi.download(asset, into, ui, run)
        self.assertEqual(len(calls), 1)
        path.write_bytes(path.read_bytes()[:100])  # a part file is continued
        self.assertEqual(fi.download(asset, into, ui, run).read_bytes(), (self.root / "source").read_bytes())

    def test_wrong_or_missing_checksum(self):
        into = self.root / "into"
        into.mkdir()
        quiet = lambda argv, **o: subprocess.run(argv + ["--silent"], capture_output=True)
        with self.assertRaisesRegex(fi.Stop, "checksum"):
            fi.download(self.asset(digest="0" * 64), into, Answers().terminal, quiet)
        self.assertEqual(list(into.iterdir()), [])
        asset = self.asset()
        del asset["digest"]
        with self.assertRaisesRegex(fi.Stop, "cannot be verified"):
            fi.download(asset, into, Answers().terminal, quiet)


class FakeSystem:
    """passwd and sudo: an account whose password state changes as the commands say."""

    def __init__(self, state="NP", cached=False, fail=None, failures=()):
        self.state, self.cached, self.fail, self.failures = state, cached, fail, failures
        self.password, self.calls = None, []

    def __call__(self, argv, input=None, **options):
        self.calls.append(argv)
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")
        no = subprocess.CompletedProcess(argv, 1, "", "")
        if argv == ["sudo", "-n", "true"]:
            return ok() if self.cached else no
        if argv == ["faillock", "--user", "deck"]:
            lines = [f"{when:%Y-%m-%d %H:%M:%S} SVC   sudo    V" for when in self.failures]
            return ok("deck:\nWhen                Type  Source    Valid\n" + "\n".join(lines) + "\n")
        if argv == ["passwd", "-S"]:
            return ok(f"deck {self.state} 2026-01-01 0 99999 7 -1\n")
        if argv == ["passwd"]:
            self.password = input.splitlines()[0] if input else "typed"
            self.state = "P"
            return ok()
        if argv[0] == "sudo":
            if "-S" in argv and input != f"{self.password}\n":
                return no
            if "-d" in argv:
                if self.fail == "remove":
                    return no
                self.state, self.password = "NP", None
                return ok()
            if self.fail == "command":
                return no
            if self.fail == "interrupt" and argv[-1] == "the-command":
                raise KeyboardInterrupt
            return ok()
        raise AssertionError(argv)


class Rights(unittest.TestCase):
    def admin(self, system, *answers):
        talk = Answers(*answers)
        return fi.Admin(talk.terminal, system, user="deck", faillock_conf="/nonexistent"), talk

    def test_temporary_password_is_removed(self):
        for fail, done in ((None, True), ("command", False)):
            system = FakeSystem(fail=fail)
            admin, talk = self.admin(system, "")
            self.assertEqual(admin.command(["the-command"], "Working"), done)
            self.assertEqual(system.state, "NP", fail)
            self.assertIn("The temporary password was removed.", talk.text())

    def test_temporary_password_is_removed_after_an_interrupt(self):
        before = signal.getsignal(signal.SIGINT)
        system = FakeSystem(fail="interrupt")
        admin, _ = self.admin(system, "1")
        with self.assertRaises(KeyboardInterrupt):
            admin.command(["the-command"], "Working")
        self.assertEqual(system.state, "NP")
        self.assertEqual(signal.getsignal(signal.SIGINT), before)

    def test_temporary_password_that_cannot_be_removed_is_shown(self):
        system = FakeSystem(fail="remove")
        admin, talk = self.admin(system, "1")
        admin.command(["the-command"], "Working")
        self.assertIn(f"*** The temporary password is still set: {system.password}", talk.text())
        self.assertIn("sudo passwd -d deck", talk.text())

    def test_temporary_passwords_differ(self):
        seen = set()
        for _ in range(3):
            admin, talk = self.admin(FakeSystem(), "1")
            admin.command(["x"], "Working")
            seen.add(next(line for line in talk.told if "Temporary password:" in line))
        self.assertEqual(len(seen), 3)

    def test_no_temporary_password_while_the_account_is_locked(self):
        now = datetime.datetime.now()
        ago = lambda *seconds: [now - datetime.timedelta(seconds=s) for s in seconds]
        system = FakeSystem(failures=ago(30, 20, 10))
        admin, talk = self.admin(system, "1")
        self.assertFalse(admin.command(["the-command"], "Working"))
        self.assertEqual(system.state, "NP")
        self.assertNotIn(["passwd"], system.calls)
        self.assertIn("None was set.", talk.text())
        for failures, locked in ((ago(20, 10), False), (ago(900, 800, 700), False), (ago(2000, 20, 10), False), ([], False),
                                 (ago(599, 598, 597), True)):
            admin, _ = self.admin(FakeSystem(failures=failures))
            self.assertEqual(admin.locked_out(now), locked, failures)

    def test_own_password_stays(self):
        system = FakeSystem()
        admin, _ = self.admin(system, "2")
        self.assertTrue(admin.command(["the-command"], "Working"))
        self.assertEqual(system.state, "P")
        self.assertFalse(any("-d" in call for call in system.calls))

    def test_skip_changes_nothing(self):
        system = FakeSystem()
        admin, _ = self.admin(system, "3")
        self.assertFalse(admin.command(["the-command"], "Working"))
        self.assertEqual(system.calls, [["sudo", "-n", "true"], ["passwd", "-S"]])

    def test_existing_password_and_cached_rights_are_not_touched(self):
        for system in (FakeSystem("P"), FakeSystem("P", cached=True), FakeSystem("NP", cached=True)):
            before = system.state
            admin, talk = self.admin(system)
            self.assertTrue(admin.command(["the-command"], "Working"))
            self.assertEqual(system.state, before)
            self.assertEqual(talk.asked, [])
            self.assertFalse(any(call == ["passwd"] or "-d" in call for call in system.calls))

    def test_locked_account(self):
        system = FakeSystem("L")
        admin, _ = self.admin(system)
        self.assertFalse(admin.command(["the-command"], "Working"))
        self.assertEqual(system.state, "L")


class WholeRun(Temp):
    """The installer against local packages, a stand-in home folder, and stand-ins for sudo, systemctl and chown."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(fi, "NEEDED_BYTES", 0)
        patch.start()
        self.addCleanup(patch.stop)
        self.home = self.root / "home"
        (self.home / ".local/share/Steam/userdata").mkdir(parents=True)
        (self.home / ".local/share/Steam/steamapps/common/Proton Experimental").mkdir(parents=True)
        (self.home / "homebrew/plugins").mkdir(parents=True)
        script(self.home / "homebrew/services/PluginLoader", "")
        self.packages = self.root / "packages"
        self.packages.mkdir()
        package(self.packages)
        plugin(self.packages)
        bin = self.root / "bin"
        script(bin / "sudo", '#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n')
        script(bin / "chown", "#!/bin/sh\n")
        script(bin / "systemctl", f'#!/bin/sh\necho "$*" >> "{self.root}/systemctl"\n')
        script(bin / "systemd-run", '#!/bin/sh\nwhile case "$1" in --*) true ;; *) false ;; esac; do shift; done\nexec "$@"\n')
        self.env = {"PATH": f"{bin}:/usr/bin:/bin", "HOME": str(self.home), "DISPLAY": ":0"}
        self.game = self.home / "Games/fefw-recomp"
        self.settings = self.home / "homebrew/settings/fefw-recomp/settings.json"
        self.installed = self.home / "homebrew/plugins/fefw-recomp"

    def install(self, *answers, packages=True):
        talk = Answers(*answers)

        def run(argv, **options):
            options.setdefault("env", self.env)
            stand_ins = self.root / "bin"
            argv = [str(stand_ins / argv[0]) if (stand_ins / argv[0]).is_file() else argv[0], *argv[1:]]
            return subprocess.run(argv, **options)

        installer = fi.Installer(talk.terminal, self.home, self.packages if packages else None, run=run,
                                 start=lambda argv, **o: subprocess.run(argv, **o), env=self.env, user="deck")
        with mock.patch.dict(os.environ, {"PATH": self.env["PATH"]}):  # for the shell the plugin install runs
            installer.go()
        return talk

    def existing(self, folder):
        folder.parent.mkdir(parents=True, exist_ok=True)
        fi.unpack_package(package(self.packages), folder)

    def calls(self):
        return (self.game / "calls").read_text().splitlines()

    def restarts(self):
        path = self.root / "systemctl"
        return path.read_text().splitlines() if path.is_file() else []

    def test_fresh_install_then_run_again(self):
        # folder, install, plugin, add to Steam, open the launcher
        talk = self.install("", "y", "y", "y", "y")
        self.assertTrue(fi.is_install(self.game))
        self.assertEqual((self.installed / "dist/index.js").read_text(), "// panel\n")
        self.assertEqual(stat.S_IMODE((self.installed / "main.py").stat().st_mode), 0o644)
        self.assertEqual(json.loads(self.settings.read_text()), {"install_dir": str(self.game)})
        self.assertEqual(self.restarts(), ["restart plugin_loader"])
        self.assertEqual(self.calls(), [
            "--status DISPLAY=unset WAYLAND_DISPLAY=fefw-installer-no-display",
            "--add-to-steam --before-setup --close-steam DISPLAY=:0 WAYLAND_DISPLAY=unset",
            " DISPLAY=:0 WAYLAND_DISPLAY=unset"])
        self.assertIn("The game is in your Steam library.", talk.text())

        # again: the game's files stay, nothing needs rights, and Steam is left alone unless asked
        (self.game / "built").touch()
        (self.game / "tools/build/fefw_build.py").write_text("# newer\n")
        (self.game / "calls").unlink()
        talk = self.install("y", "")
        self.assertEqual((self.game / "tools/build/fefw_build.py").read_text(), "# newer\n")
        self.assertIn("The Decky plugin is up to date.", talk.text())
        self.assertEqual(self.restarts(), ["restart plugin_loader"])
        self.assertEqual([c.split()[0] for c in self.calls()], ["--status"])

    def test_fresh_install_in_dialogs(self):
        dialogs = Dialogs(("list", "Where should the game go?", 0, "1"), yes("Install the game into"),
                          yes("Install the Decky plugin"), yes("Add the game to Steam?"),
                          yes("The game is in your Steam library."))

        def run(argv, **options):
            if argv[0] == "zenity":
                return dialogs.run(argv, **options)
            options.setdefault("env", self.env)
            stand_in = self.root / "bin" / argv[0]
            return subprocess.run([str(stand_in) if stand_in.is_file() else argv[0], *argv[1:]], **options)

        ui = fi.Zenity(run, dialogs.start, pause=lambda seconds: None)
        started = []
        with mock.patch.dict(os.environ, {"PATH": self.env["PATH"]}):
            fi.Installer(ui, self.home, self.packages, run=run, start=lambda argv, **o: started.append(argv), env=self.env,
                         user="deck").go()
        ui.close()  # the summary was in the last question: nothing more
        self.assertTrue((self.installed / "main.py").is_file())
        self.assertEqual(started[0][-1], str(self.game / "fefw-launcher.AppImage"))
        self.assertEqual(dialogs.script, [])

    def test_declining_everything_after_the_game(self):
        self.install("", "y", "n", "n", "n")
        self.assertFalse(self.installed.exists())
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.restarts(), [])
        self.assertFalse((self.game / "in-steam").exists())
        self.assertFalse((self.game / "opened").exists())

    def test_declining_the_game_installs_nothing(self):
        with self.assertRaises(fi.Stop):
            self.install("", "n")
        self.assertFalse(self.game.exists())

    def test_existing_install_elsewhere_and_the_plugin_follows_it(self):
        self.install("", "y", "y", "n", "n")
        other = self.root / "sd card" / "fefw"
        self.existing(other)
        self.settings.write_text(json.dumps({"install_dir": str(self.game), "keep_awake": False, "held_sleep": None}))
        # not that install; the folder; point the plugin at it; Steam; launcher
        talk = self.install("n", str(other), "y", "n", "n")
        self.assertIn(f"Point the Decky plugin at {other}?", "".join(talk.asked))
        self.assertEqual(json.loads(self.settings.read_text()),
                         {"install_dir": str(other), "keep_awake": False, "held_sleep": None})
        self.assertEqual(self.restarts(), ["restart plugin_loader"] * 2)

    def test_failed_plugin_install_leaves_the_old_one_and_its_settings(self):
        self.install("", "y", "y", "n", "n")
        plugin(self.packages, "10.0.0").replace(self.packages / "fefw-recomp-9.9.9-decky.zip")
        script(self.root / "bin" / "sudo", "#!/bin/sh\nexit 1\n")
        before = self.settings.read_bytes()
        with mock.patch.object(fi.Admin, "password_state", lambda self: "L"):
            talk = self.install("y", "y", "n", "n")
        self.assertEqual(json.loads((self.installed / "package.json").read_text())["version"], "9.9.9")
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertIn("The plugin could not be installed and loaded.", talk.text())

    def test_a_restart_that_failed_is_tried_again(self):
        self.install("", "y", "y", "n", "n")
        plugin(self.packages, "10.0.0").replace(self.packages / "fefw-recomp-9.9.9-decky.zip")
        systemctl = self.root / "bin" / "systemctl"
        working = systemctl.read_text()
        script(systemctl, "#!/bin/sh\nexit 1\n")  # the files go in, Decky does not come back
        talk = self.install("y", "y", "n", "n")
        self.assertIn("could not be installed and loaded", talk.text())
        self.assertEqual(json.loads((self.installed / "package.json").read_text())["version"], "10.0.0")
        script(systemctl, working)
        (self.root / "systemctl").unlink()
        talk = self.install("y", "y", "n", "n")
        self.assertIn("Restart it now", "".join(talk.asked))
        self.assertEqual(self.restarts(), ["restart plugin_loader"])
        talk = self.install("y", "n", "n")  # and then there is nothing left to do
        self.assertIn("The Decky plugin is up to date.", talk.text())

    def test_folder_questions(self):
        used = self.root / "used"
        used.mkdir()
        (used / "mine").write_text("x")
        talk = Answers("relative/path", str(used), '/a"b', "~/Spiele")
        self.assertEqual(fi.Installer(talk.terminal, self.home, user="deck").choose_folder(), self.home / "Spiele")
        self.assertEqual(len(talk.asked), 4)

    def test_old_launcher_stops_before_changing_anything_else(self):
        self.existing(self.game)
        (self.game / "old").touch()
        with self.assertRaisesRegex(fi.Stop, "after updating"):
            self.install("y", "n")
        self.assertFalse(self.installed.exists())

    def test_running_game_stops(self):
        self.existing(self.game)
        (self.game / "busy").touch()
        with self.assertRaisesRegex(fi.Stop, "The game is running"):
            self.install("y")
        self.assertFalse(self.installed.exists())

    def test_game_mode_is_refused(self):
        self.env["XDG_CURRENT_DESKTOP"] = "gamescope"
        with self.assertRaisesRegex(fi.Stop, "Desktop Mode"):
            self.install()
        self.assertFalse(self.game.exists())

    def test_without_decky_it_asks_first(self):
        (self.home / "homebrew/services/PluginLoader").unlink()
        talk = self.install("", "y", "n", "n", "n")
        self.assertIn("Install Decky Loader", "".join(talk.asked))
        self.assertFalse(self.installed.exists())


class Dialogs:
    """zenity: answers each dialog from a list of (kind, what must be in it, exit code, output)."""

    def __init__(self, *script):
        self.script, self.shown, self.windows, self.inputs = list(script), [], [], []

    def run(self, argv, **options):
        kind = argv[1].lstrip("-")
        text = next((a[len("--text="):] for a in argv if a.startswith("--text=")), "")
        self.shown.append((kind, text, argv))
        self.inputs.append(options.get("input"))
        if not self.script:
            raise AssertionError(f"unexpected {kind} dialog: {text}")
        want, part, code, out = self.script.pop(0)
        if kind != want or part not in text:
            raise AssertionError(f"expected a {want} dialog with {part!r}, got {kind}: {text}")
        return subprocess.CompletedProcess(argv, code, out + "\n" if out else "", "")

    def start(self, argv, **options):
        if argv[0] != "zenity":
            raise AssertionError(argv)
        window = Process()
        window.argv = argv
        self.windows.append(window)
        return window

    @property
    def ui(self):
        return fi.Zenity(self.run, self.start, pause=lambda seconds: None)


class Process:
    """A program that is running until it is ended, or until `ends_after` polls."""

    def __init__(self, ends_after=None, code=0):
        self.stdin, self.returncode, self.code, self.left = io.StringIO(), None, code, ends_after
        self.stdin.close = lambda: None  # what was written stays readable
        self.terminated = False

    def poll(self):
        if self.returncode is None and self.left is not None:
            if self.left == 0:
                self.returncode = self.code
            self.left -= 1
        return self.returncode

    def terminate(self):
        self.terminated, self.returncode = True, -15

    def wait(self, timeout=None):
        return self.returncode


def yes(part):
    return ("question", part, 0, "")


def no(part):
    return ("question", part, 1, "")


class Zenity(Temp):
    def test_what_was_said_comes_with_the_next_dialog_as_plain_text(self):
        dialogs = Dialogs(yes("One\n\nA &lt;b&gt; &amp; folder\n\nGo?"), ("info", "Last", 0, ""))
        ui = dialogs.ui
        ui.say("\nOne")
        ui.say("A <b> & folder")
        self.assertTrue(ui.yes("Go?"))
        ui.say("Last")
        ui.close()
        ui.close()  # nothing left to show
        self.assertEqual(len(dialogs.shown), 2)

    def test_choices_and_cancel(self):
        dialogs = Dialogs(("list", "Which?", 0, ""), ("list", "Which?", 0, "2"), ("list", "Which?", 1, ""),
                          ("list", "Which?", 1, ""))
        ui = dialogs.ui
        self.assertEqual(ui.choose("Which?", ["a", "b", "c"]), 2)  # nothing picked: asked again
        self.assertEqual(ui.choose("Which?", ["a", "b"], cancel=2), 2)
        with self.assertRaisesRegex(fi.Stop, "Stopped"):
            ui.choose("Which?", ["a", "b"])
        self.assertEqual(dialogs.shown[0][2][-9:], ["TRUE", "1", "a", "FALSE", "2", "b", "FALSE", "3", "c"])

    def test_a_picked_folder_with_other_files_gets_a_folder_for_the_game(self):
        games = self.root / "Games"
        games.mkdir()
        (games / "other").mkdir()
        empty = self.root / "empty"
        empty.mkdir()
        installed = self.root / "installed"
        fi.unpack_package(package(self.root), installed)
        for picked, expected in ((games, games / "fefw-recomp"), (empty, empty), (installed, installed)):
            dialogs = Dialogs(("list", "Where should the game go?", 0, "2"), ("file-selection", "", 0, str(picked)))
            self.assertEqual(dialogs.ui.folder(self.root / "default"), str(expected))
        dialogs = Dialogs(("list", "Where", 0, "2"), ("file-selection", "", 1, ""), ("list", "Where", 0, "1"))
        self.assertEqual(dialogs.ui.folder(self.root / "default"), str(self.root / "default"))

    def test_failing_zenity_is_not_taken_for_a_no(self):
        with self.assertRaisesRegex(fi.Stop, "A dialog failed"):
            Dialogs(("question", "", 255, "")).ui.yes("Go?")

    def download(self, window_polls, download_polls, code=0):
        window, download = Process(window_polls), Process(download_polls, code)
        started = []

        def start(argv, **options):
            started.append(argv)
            return window if argv[0] == "zenity" else download

        ui = fi.Zenity(None, start, pause=lambda seconds: None)
        path = self.root / "file"
        path.write_bytes(b"x" * 50)
        return window, download, started, lambda: ui.transfer(["curl", "--url"], path, 200, "package", None)

    def test_download_shows_how_far_it_is(self):
        window, download, started, go = self.download(None, 2)
        self.assertEqual(go(), 0)
        self.assertEqual(started[1], ["curl", "--url", "--silent"])
        self.assertIn("\n25\n", window.stdin.getvalue())
        self.assertTrue(window.terminated)
        self.assertFalse(download.terminated)

    def test_download_cancelled(self):
        window, download, _, go = self.download(1, None)
        with self.assertRaisesRegex(fi.Stop, "cancelled"):
            go()
        self.assertTrue(download.terminated)

    def test_download_that_fails(self):
        self.assertEqual(self.download(None, 0, code=22)[3](), 22)

    def test_which_front_end(self):
        script(self.root / "zenity", "#!/bin/sh\n")
        path = {"PATH": str(self.root)}
        self.assertIsInstance(fi.front_end(False, dict(path, WAYLAND_DISPLAY="wayland-0")), fi.Zenity)
        self.assertIsInstance(fi.front_end(False, dict(path, DISPLAY=":0")), fi.Zenity)
        for text, env in ((True, dict(path, DISPLAY=":0")), (False, path), (False, {"PATH": "/nonexistent", "DISPLAY": ":0"})):
            self.assertIsInstance(fi.front_end(text, env), fi.Terminal)


class TerminalWindow(Temp):
    def test_a_desktop_without_zenity_gets_a_terminal(self):
        script(self.root / "konsole", "#!/bin/sh\n")
        env = {"PATH": str(self.root), "WAYLAND_DISPLAY": "wayland-0"}
        command = fi.in_a_terminal(["--packages", "/p"], env, tty=False)
        self.assertEqual(command[:3], ["konsole", "--hold", "-e"])
        self.assertEqual(command[-5:], ["-I", str(HERE / "fefw_install.py"), "--packages", "/p", "--text"])
        self.assertIsNone(fi.in_a_terminal([], env, tty=True))  # it is in one
        self.assertIsNone(fi.in_a_terminal([], {"PATH": str(self.root)}, tty=False))  # no desktop
        self.assertIsNone(fi.in_a_terminal([], dict(env, PATH="/nonexistent"), tty=False))


class DialogRights(unittest.TestCase):
    def admin(self, system, *script):
        dialogs = Dialogs(*script)
        ui = dialogs.ui
        return fi.Admin(ui, system, user="deck", faillock_conf="/nonexistent"), dialogs, ui

    def passwords(self, system):
        return [call for call in system.calls if call == ["passwd"]]

    def test_existing_password_is_asked_for_until_right(self):
        system = FakeSystem("P")
        system.password = "right"
        admin, dialogs, _ = self.admin(system, ("entry", "Type your password", 0, "wrong"),
                                       ("entry", "That did not work", 0, "right"))
        self.assertTrue(admin.command(["the-command"], "Working"))
        self.assertIn(["sudo", "-S", "-k", "-p", "", "the-command"], system.calls)
        self.assertEqual(len(dialogs.windows), 1)
        self.assertFalse(any("right" in argument for call in system.calls for argument in call))

    def test_cancelled_or_never_right(self):
        system = FakeSystem("P")
        system.password = "right"
        admin, _, _ = self.admin(system, ("entry", "password", 1, ""))
        self.assertFalse(admin.command(["the-command"], "Working"))
        admin, _, ui = self.admin(system, *[("entry", "password", 0, "wrong")] * 3)
        self.assertFalse(admin.command(["the-command"], "Working"))
        self.assertIn("The password was not accepted.", ui.said)
        self.assertNotIn(["sudo", "-S", "-k", "-p", "", "the-command"], system.calls)

    def test_own_password_typed_twice(self):
        system = FakeSystem()
        admin, _, _ = self.admin(system, ("list", "no password yet", 0, "2"), ("entry", "Choose a password", 0, "one"),
                                 ("entry", "again", 0, "two"), ("entry", "did not match", 0, "mine"), ("entry", "again", 0, "mine"))
        self.assertTrue(admin.command(["the-command"], "Working"))
        self.assertEqual((system.state, system.password), ("P", "mine"))

    def test_own_password_cancelled(self):
        for script_ in ([("entry", "Choose", 1, "")], [("entry", "Choose", 0, "a"), ("entry", "again", 1, "")]):
            system = FakeSystem()
            admin, _, _ = self.admin(system, ("list", "no password yet", 0, "2"), *script_)
            self.assertFalse(admin.command(["the-command"], "Working"))
            self.assertEqual((system.state, self.passwords(system)), ("NP", []))

    def test_temporary_password_is_shown_while_set_and_removed(self):
        system = FakeSystem()
        admin, dialogs, ui = self.admin(system, ("list", "no password yet", 0, "1"))
        self.assertTrue(admin.command(["the-command"], "Working"))
        self.assertEqual(system.state, "NP")
        shown = dialogs.windows[0].stdin.getvalue()
        self.assertTrue(shown.startswith("# Working\\n\\nTemporary password: "), shown)
        self.assertEqual(shown.count("\n"), 1)
        self.assertIn("sudo passwd -d deck", shown)
        password = shown.split("Temporary password: ")[1].split("\\n")[0]
        self.assertEqual(len(password), 20)
        for argv in [dialogs.windows[0].argv, *(shown_[2] for shown_ in dialogs.shown)]:
            self.assertNotIn(password, " ".join(argv))
        self.assertTrue(dialogs.windows[0].terminated)
        self.assertIn("The temporary password was removed.", ui.said)

    def test_a_password_left_behind_gets_a_dialog_of_its_own(self):
        system = FakeSystem(fail="remove")
        admin, dialogs, _ = self.admin(system, ("list", "no password yet", 0, "1"), ("text-info", "", 0, ""))
        admin.command(["the-command"], "Working")
        self.assertIn(f"still set: {system.password}", dialogs.inputs[-1])
        self.assertNotIn(system.password, " ".join(dialogs.shown[-1][2]))

    def test_closing_the_choice_skips(self):
        system = FakeSystem()
        admin, _, _ = self.admin(system, ("list", "no password yet", 1, ""))
        self.assertFalse(admin.command(["the-command"], "Working"))
        self.assertEqual(self.passwords(system), [])


class Shipped(unittest.TestCase):
    def test_a_second_launch_leaves_the_first_alone(self):
        text = (HERE / "fefw-recomp-install.desktop").read_text()
        line = next(line for line in text.splitlines() if line.startswith("Exec="))
        shell, option, command = shlex.split(line[len("Exec="):])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # stands in for the installer: runs until told to stop
            (root / "release.py").write_text(
                "import pathlib, time\n"
                f"root = pathlib.Path({str(root)!r})\n"
                "with open(root / 'runs', 'a') as runs: runs.write('run\\n')\n"
                "while not (root / 'stop').exists(): time.sleep(0.05)\n")
            script(root / "bin" / "zenity", f'#!/bin/sh\necho "$@" >> "{root}/dialogs"\n')
            released = f"https://github.com/{fi.REPO}/releases/latest/download/fefw-recomp-install.py"
            argv = [shell, option, command.replace(released, (root / "release.py").as_uri())]
            env = {"HOME": temp, "PATH": f"{root}/bin:/usr/bin:/bin"}
            first = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL)
            try:
                for _ in range(200):
                    if (root / "runs").is_file():
                        break
                    time.sleep(0.05)
                second = subprocess.run(argv, env=env, capture_output=True, timeout=20)
                self.assertEqual(second.returncode, 1)
                self.assertIn("already running", (root / "dialogs").read_text())
            finally:
                (root / "stop").touch()
                first.wait(timeout=20)
            self.assertEqual((root / "runs").read_text(), "run\n")
            self.assertEqual(subprocess.run(argv, env=env, capture_output=True, timeout=20).returncode, 0)  # free again


    def test_desktop_file_runs_the_released_script(self):
        text = (HERE / "fefw-recomp-install.desktop").read_text()
        command = next(line for line in text.splitlines() if line.startswith("Exec="))
        self.assertIn("zenity --error", command)
        self.assertIn(f"https://github.com/{fi.REPO}/releases/latest/download/fefw-recomp-install.py", command)
        self.assertIn("Terminal=false", text)
        # characters a desktop entry would need escaped inside the quotes
        for character in "$`\\":
            self.assertNotIn(character, command)


if __name__ == "__main__":
    unittest.main()
