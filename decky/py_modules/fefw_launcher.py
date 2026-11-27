# SPDX-License-Identifier: GPL-3.0-or-later
"""Drives fefw-launcher.AppImage's commands without its window (launcher/README.md) for the plugin. Nothing here needs Decky, so the
tests run anywhere. All the update and build logic stays in the launcher."""

import asyncio
import hashlib
import io
import json
import os
import re
import time
import zipfile
import zlib
from collections import deque
from pathlib import Path

APPIMAGE = "fefw-launcher.AppImage"
API = 1
TOO_OLD = ("This install's launcher cannot be driven by the plugin. In Desktop Mode, open the launcher and update to a newer release.")
NEW_TOO_OLD = "The new release's launcher cannot be driven by the plugin: build the game in the launcher, in Desktop Mode."
# Variables of the plugin loader's own Python that would break the programs the launcher starts.
LOADER_VARIABLES = ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH", "XDG_CONFIG_HOME")
DISPLAY_VARIABLES = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "GAMESCOPE_WAYLAND_DISPLAY")
# For the questions: with WAYLAND_DISPLAY unset a window toolkit still tries the session's wayland-0, so a name that is not there.
NO_DISPLAY = {"WAYLAND_DISPLAY": "fefw-plugin-no-display"}
QUESTIONS = ("--status", "--check-update", "--settings", "--set")  # these do not listen for "cancel"
LINE_LIMIT = 4 * 1024 * 1024  # the release notes come as one line
QUERY_TIMEOUT = 120.0
# The shader preparation runs the game. A display that does not work can leave it waiting for ever, with nothing said.
PREPARE_TIMEOUT = 600.0
POLL = 1.0  # how often a silent launcher is looked at
BUSY_AGAIN = 5.0  # how often an install seen busy is looked at again, while the panel is open
# The plugin of the installed release, which its package carries (game-build/scripts/make-release-linux.sh).
PLUGIN_ZIP = "decky/fefw-recomp-decky.zip"
PLUGIN_NAME = "FEFW Recomp"
PLUGIN_FOLDER = "fefw-recomp"
PLUGIN_LIMIT = 20 * 1024 * 1024


class LauncherError(Exception):
    pass


def is_install(folder):
    """A release package's folder."""
    folder = Path(folder)
    return (folder / APPIMAGE).is_file() and (folder / "tools" / "build" / "fefw_build.py").is_file()


def find_display(proc="/proc", uid=None):
    """The display variables of the user's running Steam client, which has the session's: the loader runs without one. {} when
    there is none to be found."""
    uid = os.getuid() if uid is None else uid
    for entry in sorted(Path(proc).glob("[0-9]*"), key=lambda p: int(p.name)):
        try:
            if entry.stat().st_uid != uid or (entry / "comm").read_text().strip() != "steam":
                continue
            raw = (entry / "environ").read_bytes()
        except OSError:
            continue
        env = dict(item.split("=", 1) for item in raw.decode("utf-8", "replace").split("\0") if "=" in item)
        found = {k: env[k] for k in DISPLAY_VARIABLES if env.get(k)}
        if "DISPLAY" in found or "WAYLAND_DISPLAY" in found:
            return found
    return {}


def environment(base, home, display=None, run_dir=None):
    """The environment the launcher gets: the user's, without the loader's own variables, and with no display unless one is given."""
    env = {k: v for k, v in base.items() if k not in LOADER_VARIABLES and k not in DISPLAY_VARIABLES}
    env["HOME"] = str(home)
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    run_dir = Path(run_dir or f"/run/user/{os.getuid()}")
    if run_dir.is_dir():  # the Steam runtime looks for the user's session here
        env.setdefault("XDG_RUNTIME_DIR", str(run_dir))
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={run_dir}/bus")
    env.update(display or {})
    return env


class Command:
    """One run of a launcher command."""

    def __init__(self, install, argument, env, extra=()):
        self.install, self.argument, self.env = Path(install), argument, env
        self.extra = tuple(extra)  # the argument's value and further flags
        self.process = None
        self.timed_out = False
        self._cancelled = False
        self._starting = None  # an event, set once the program has started or failed to

    async def run(self, on_progress=None, timeout=None, prepare_timeout=PREPARE_TIMEOUT):
        """The command's end line. LauncherError when it gives none."""
        self._starting = asyncio.Event()
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(self.install / APPIMAGE), self.argument, *self.extra, cwd=str(self.install), env=self.env, limit=LINE_LIMIT,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
        except OSError as e:
            raise LauncherError(f"Cannot start the launcher: {e}") from e
        finally:
            self._starting.set()
        if self._cancelled:
            self.cancel()
        errors = deque(maxlen=5)
        drain = asyncio.ensure_future(self._drain(errors))
        try:
            return await self._answer(drain, errors, on_progress, timeout, prepare_timeout)
        finally:
            drain.cancel()  # something the launcher started may keep its output open: not waited for
            transport = getattr(self.process, "_transport", None)
            if transport and self.process.returncode is not None:
                transport.close()

    async def _answer(self, drain, errors, on_progress, timeout, prepare_timeout):
        try:
            end = await asyncio.wait_for(self._read(on_progress, prepare_timeout), timeout)
        except asyncio.TimeoutError:
            await self.stop()
            raise LauncherError("The launcher did not answer.") from None
        except ValueError as e:  # a line over the limit, or another interface version
            await self.stop()
            raise LauncherError(f"The launcher sent an answer that cannot be read: {e}") from e
        except LauncherError:
            await self.stop()
            raise
        try:
            await asyncio.wait_for(self.process.wait(), 10)
        except asyncio.TimeoutError:
            await self.stop()
        if end is None:
            await asyncio.wait([drain], timeout=0.2)  # its last words
            detail = " ".join(errors)
            raise LauncherError(f"The launcher stopped without an answer. {detail}".strip())
        return end

    async def _drain(self, errors):
        while True:
            line = await self.process.stderr.readline()
            if not line:
                return
            errors.append(line.decode("utf-8", "replace").strip())

    async def _read(self, on_progress, prepare_timeout):
        """The end line, or None when the launcher ended without one."""
        phase, silent, poll = None, 0.0, min(POLL, prepare_timeout)
        while True:
            try:
                raw = await asyncio.wait_for(self.process.stdout.readline(), poll)
            except asyncio.TimeoutError:
                silent += poll
                if self.process.returncode is not None:  # gone, though something it started may hold its output open
                    return None
                if phase == "prepare" and silent >= prepare_timeout:
                    if self.timed_out:
                        raise LauncherError("Preparing the shaders took too long, and the launcher did not stop.")
                    self.timed_out, silent = True, 0.0
                    self.cancel()
                continue
            silent = 0.0
            if not raw:
                return None
            try:
                line = json.loads(raw)
                event = line["event"]
            except (ValueError, KeyError, TypeError):
                continue  # not one of the interface's lines
            if line.get("api") != API:
                raise ValueError(f"interface version {line.get('api')}, the plugin knows {API}")
            if event == "end":
                return line
            if event == "progress":
                phase = line.get("phase")
                if on_progress:
                    on_progress(line)

    def cancel(self):
        """Asks the launcher to stop: it ends what it started itself."""
        self._cancelled = True
        if self.process and self.process.returncode is None:
            try:
                self.process.stdin.write(b"cancel\n")
            except (OSError, RuntimeError):
                pass

    async def stop(self, grace=3.0):
        """Cancel, and end the launcher if it does not stop (a question at once: it would not). An update is never ended: it takes
        no cancel while it moves the new files into place, and stopping it there would leave a mix of two releases."""
        self.cancel()
        if self._starting:  # a program still being started has to be there to be stopped
            try:
                await asyncio.wait_for(self._starting.wait(), grace)
            except asyncio.TimeoutError:
                return
        if not self.process or self.process.returncode is not None:
            return
        try:
            await asyncio.wait_for(self.process.wait(), 0 if self.argument in QUESTIONS else grace)
        except asyncio.TimeoutError:
            if self.argument == "--update":
                self._leave()
                return
            try:
                os.killpg(self.process.pid, 9)
                await asyncio.wait_for(self.process.wait(), 1)  # gone when this returns
            except (OSError, asyncio.TimeoutError):
                pass

    def _leave(self):
        """Lets the launcher run on: asyncio ends a program that is still running when its transport is closed or collected."""
        transport = getattr(self.process, "_transport", None)
        if transport is not None:
            transport.close = lambda: None  # also what collecting it calls


def _timers(value):
    """{"battery": seconds, "ac": seconds}, or None for anything else."""
    if isinstance(value, dict) and all(type(value.get(k)) is int and 0 <= value[k] < 2**31 for k in ("battery", "ac")):
        return {"battery": value["battery"], "ac": value["ac"]}
    return None


def _version(text):
    """A version as something to compare, in semantic versioning's order (1.2.0-beta.2 < 1.2.0-beta.10 < 1.2.0), or None."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", str(text or ""))
    if not match:
        return None
    early = match[4]
    parts = [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in early.split(".")] if early else []
    return int(match[1]), int(match[2]), int(match[3]), not early, parts


def _inside_plugin(name):
    parts = (name[:-1] if name.endswith("/") else name).split("/")  # a folder's entry ends in /
    return parts[0] == PLUGIN_FOLDER and "\\" not in name and all(part not in ("", ".", "..") for part in parts)


def read_plugin(data):
    """The version of the plugin in a release's ZIP, or None for anything Decky must not be handed: it unpacks as root, and a
    plugin that asks for it runs as root."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            # one entry for each file, so that what is checked here is what gets unpacked
            if len(set(names)) != len(names) or not all(map(_inside_plugin, names)):
                return None
            about = json.loads(z.read(f"{PLUGIN_FOLDER}/plugin.json"))
            version = json.loads(z.read(f"{PLUGIN_FOLDER}/package.json"))["version"]
        if about["name"] != PLUGIN_NAME or about["flags"] != []:
            return None
    except (zipfile.BadZipFile, zlib.error, OSError, ValueError, KeyError, TypeError, RuntimeError, NotImplementedError):
        return None
    return version if _version(version) else None


def plugin_offer(install, current, keep_dir):
    """What to hand Decky's installer, {"version", "artifact", "hash"}, when the install's release carries a newer plugin than
    `current`; else None. The checked ZIP is copied to keep_dir: Decky removes the old plugin before it checks the hash, so the
    file must stay as it is until then, which the install's does not while the game is updated. Earlier copies stay too: a question
    of Decky's that is still open names one (a release adds a few tens of KB)."""
    source = Path(install) / PLUGIN_ZIP
    try:
        if source.stat().st_size > PLUGIN_LIMIT:
            return None
        data = source.read_bytes()
    except OSError:
        return None
    version = read_plugin(data)
    if not version or not _version(current) or _version(version) <= _version(current):
        return None
    digest = hashlib.sha256(data).hexdigest()
    kept = Path(keep_dir) / f"plugin-{digest[:16]}.zip"
    try:
        if not kept.is_file() or kept.read_bytes() != data:
            kept.parent.mkdir(parents=True, exist_ok=True)
            part = kept.with_suffix(".part")
            part.write_bytes(data)
            part.replace(kept)
    except OSError:
        return None
    return {"version": version, "artifact": f"file://{kept}", "hash": digest}


class Controller:
    """What the panel shows and does: the chosen install, its status, and at most one job (an update or a rebuild)."""

    def __init__(self, settings_file, home, base_env=None, display=find_display, on_done=None, plugin_version=None,
                 data_dir=None):
        self.settings_file = Path(settings_file)
        self.home = Path(home)
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.display = display
        self.on_done = on_done
        self.plugin_version, self.data_dir = plugin_version, data_dir  # this plugin's, and where it may keep files
        self.plugin_update = None  # plugin_offer()'s, for the chosen install
        self._updated = False  # whether the job replaced the install's release
        saved = self._load()
        self.install_dir = saved.get("install_dir") or None
        self.keep_awake = saved.get("keep_awake") is not False  # on unless it was turned off
        # Steam's auto-sleep timers as they were before the panel turned them off for a job (src/sleep.ts), until put back
        self.held_sleep = _timers(saved.get("held_sleep"))
        self.status = None
        self.update = None
        self.settings = None  # None until read, and with a launcher that lists none
        self.job = None
        self.result = None
        self.error = None
        self.prepare_timeout = PREPARE_TIMEOUT
        self._command = None  # the job's
        self._running = set()  # every command under way, the panel's questions too
        # the latest status read and update check: an earlier one that answers late says nothing
        self._status_number = self._check_number = self._settings_number = 0
        self.busy_again = BUSY_AGAIN
        self._status_at = 0.0  # when the status was last asked for
        self._looking = 0  # refreshes under way
        self._watching = None
        self._task = None
        self._cancel = False
        self._closing = False
        self._started = 0.0

    def _load(self):
        try:
            saved = json.loads(self.settings_file.read_text())
        except (OSError, ValueError):
            return {}
        return saved if isinstance(saved, dict) else {}

    def _save(self):
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        saved = {"install_dir": self.install_dir, "keep_awake": self.keep_awake, "held_sleep": self.held_sleep}
        # whole or not at all: held_sleep is the only way back to the user's timers
        part = self.settings_file.with_suffix(".part")
        part.write_text(json.dumps(saved))
        part.replace(self.settings_file)

    def set_keep_awake(self, on):
        self.keep_awake = bool(on)
        self._save()

    def hold_sleep(self, battery, ac):
        """Records the timers about to be turned off. The first record stays until released: later ones would read "never"."""
        if self.held_sleep is None:
            timers = _timers({"battery": battery, "ac": ac})
            if timers is None:
                raise LauncherError("Those are not sleep timers.")
            self._record(timers)

    def release_sleep(self):
        self._record(None)

    def _record(self, timers):
        """Saved, or unchanged: the panel turns the timers off only for a record that would outlast a restart."""
        before, self.held_sleep = self.held_sleep, timers
        try:
            self._save()
        except OSError:
            self.held_sleep = before
            raise

    def state(self):
        # counted here, so that it is right when the panel is opened again
        job = self.job and dict(self.job, elapsed=int(time.monotonic() - self._started))
        return {"install_dir": self.install_dir, "home": str(self.home), "status": self.status, "update": self.update,
                "settings": self.settings, "job": job, "result": self.result, "error": self.error,
                "keep_awake": self.keep_awake, "held_sleep": self.held_sleep, "plugin_update": self.plugin_update}

    def set_install(self, folder):
        if self.job:
            raise LauncherError("Wait for the running job to finish.")
        if not is_install(folder):
            raise LauncherError("That folder is not the game's: choose the one with fefw-launcher.AppImage in it.")
        self.install_dir = str(folder)
        self._save()
        self.status = self.update = self.settings = self.result = self.error = None
        self.plugin_update = None

    def _new(self, argument, display=None, extra=()):
        if self._closing:
            raise LauncherError("The plugin is closing.")
        if not self.install_dir or not is_install(self.install_dir):
            raise LauncherError("Choose the game's folder first.")
        env = environment(self.base_env, self.home, display)
        return Command(self.install_dir, argument, env, extra)

    async def refresh(self):
        """Reads the install's status and asks for a newer release. Problems end up in `error`. Not during a job."""
        if not self.job:
            self._look_for_plugin()
            await self._look()

    async def reread(self):
        """Reads the status and settings again, which stay on this device: no update check over the network. A look nobody
        asked for (the plugin starting, the game page opened, a game closed), so a failure leaves what was read before. Not
        during a job."""
        if self.job or not self.install_dir or self._looking:
            return
        self._looking += 1
        try:
            if await self._read_status(quietly=True):
                await self._read_settings()
        finally:
            self._looking -= 1

    def _look_for_plugin(self):
        """Needs no answer from the launcher: a newer release's may be one only its newer plugin understands."""
        ready = self.data_dir and self.install_dir and is_install(self.install_dir)
        self.plugin_update = plugin_offer(self.install_dir, self.plugin_version, self.data_dir) if ready else None

    async def _run(self, command, *args, **kwargs):
        """command.run, known to close() while it lasts."""
        self._running.add(command)
        try:
            return await command.run(*args, **kwargs)
        finally:
            self._running.discard(command)

    def watch(self):
        """The state, for the panel's regular question while it is open. An install last seen busy (the game running, the
        launcher's window at work) is looked at again, so that Play and the other buttons come back by themselves."""
        due = (self.status and self.status.get("busy") and time.monotonic() - self._status_at >= self.busy_again
               and not (self.job or self._looking or self._closing) and (not self._watching or self._watching.done()))
        if due:
            self._watching = asyncio.ensure_future(self._read_status(quietly=True))
        return self.state()

    async def _look(self):
        self._looking += 1
        try:
            if await self._read_status():
                await self._read_settings()
                await self._check_update()
        finally:
            self._looking -= 1

    async def _read_settings(self):
        """The settings the panel can change. A launcher from before them is not an error: there are none to show."""
        if self._closing:
            return
        folder = self.install_dir
        self._settings_number += 1
        number = self._settings_number
        try:
            # with no display, as the status: a launcher without this command then ends instead of opening its window
            answer = await self._run(self._new("--settings", NO_DISPLAY), timeout=QUERY_TIMEOUT)
        except LauncherError:
            answer = None
        if number == self._settings_number and folder == self.install_dir:
            self.settings = answer if answer and answer.get("state") == "done" else None

    async def set_setting(self, setting, choice):
        """Writes one choice; the game reads it at its next start."""
        if self.job:
            raise LauncherError("Wait for the running job to finish.")
        if not self.settings:
            raise LauncherError("The settings have not been read yet.")
        self.error = None
        # what was asked before this change describes the settings before it
        self._settings_number += 1
        answer = await self._run(self._new("--set", NO_DISPLAY, [f"{setting}={choice}"]), timeout=QUERY_TIMEOUT)
        await self._read_settings()
        if answer.get("state") != "done":
            raise LauncherError(answer.get("message") or "The setting could not be changed.")

    async def _read_status(self, quietly=False):
        """Reads the install's status, which stays on this device. False when it cannot be read, with the reason in `error`.
        quietly: a look nobody asked for, which leaves the error shown and, if it fails, what was read before."""
        if self._closing:
            return False
        if not quietly:
            self.error = None
        self._status_at = time.monotonic()
        folder = self.install_dir
        self._status_number += 1
        number = self._status_number
        try:
            # with no display: a launcher too old for these commands then cannot open its window, and ends instead
            try:
                status = await self._run(self._new("--status", NO_DISPLAY), timeout=QUERY_TIMEOUT)
            except LauncherError as e:
                raise LauncherError(f"{TOO_OLD} ({e})") from e
            if status.get("state") != "done":
                raise LauncherError(status.get("message") or "The status could not be read.")
        except LauncherError as e:
            if number == self._status_number and not quietly:
                self._forget(folder)
                self.error = str(e)
            return False
        if number != self._status_number or folder != self.install_dir:
            return False
        self.status = status
        return True

    async def _check_update(self):
        """Asks for a newer release, over the network."""
        if self._closing:
            return
        folder = self.install_dir
        self._check_number += 1
        number = self._check_number
        try:
            check = await self._run(self._new("--check-update", NO_DISPLAY), timeout=QUERY_TIMEOUT)
            if check.get("state") != "done":
                raise LauncherError(check.get("message") or "The update check failed.")
        except LauncherError as e:
            if number == self._check_number and folder == self.install_dir:
                self.update = None  # what an earlier check said may no longer hold
                if not self.job:
                    self.error = str(e)
            return
        if number == self._check_number and folder == self.install_dir:
            self.update = check

    def _forget_everything(self):
        self.status = self.update = self.settings = None

    def _forget(self, folder):
        """What was read before is not shown for an install that can no longer be read: it may have changed."""
        if folder == self.install_dir:
            self._forget_everything()

    def download(self, kind, setting, choice):
        """Starts the download of an upscaler's files, then picks the choice that needs them. For a player who accepted the
        text in settings["consent"][kind]."""
        if not self.settings or kind not in self.settings.get("consent", {}):
            raise LauncherError("That download is not offered.")
        self.start("download", (kind, setting, choice))

    def start(self, kind, download=None):
        """Starts "update" (which goes on to rebuild when the new release needs it), "rebuild", or download()'s job."""
        if kind not in ("update", "rebuild", "download") or (kind == "download") != bool(download):
            raise LauncherError(f"Unknown job: {kind}")
        if self.job:
            raise LauncherError("A job is already running.")
        if not self.status:
            raise LauncherError("The install's status has not been read yet.")
        self._cancel = False
        self.result = self.error = None
        # what was asked before the job describes the install before it
        self._status_number += 1
        self._check_number += 1
        self._settings_number += 1
        self._download = download
        self._updated, self.plugin_update = False, None
        self._started = time.monotonic()
        self.job = {"kind": kind, "step": kind, "phase": None, "message": "Starting", "fraction": None, "counting": False,
                    "cancelling": False}
        self._task = asyncio.ensure_future(self._job(kind))

    async def _job(self, kind):
        try:
            result = await self._steps(kind)
        except LauncherError as e:
            result = {"state": "error", "message": str(e)}
        except Exception as e:  # the panel must not be left with a job that never ends
            result = {"state": "error", "message": f"The plugin failed: {e!r}"}
        self._command = None
        read = await self._read_status()  # still the job's: another cannot start on what was true before this one
        if read:
            await self._read_settings()
        self.update = None  # nor is an update from before it offered again
        if result["message"].endswith(NEW_TOO_OLD):
            self.error = None  # the result says it, without advising the update that was just made
        self._look_for_plugin()
        if self._updated and self.plugin_update and result["state"] != "cancelled":
            result = dict(result, plugin_update=self.plugin_update)  # the panel asks to update the plugin with it
        self.result = result
        self.job = None
        if self.on_done and not self._closing:
            await self.on_done(result)
        if read:
            await self._check_update()  # after the result: without a network it takes long to fail

    async def _steps(self, kind):
        if kind == "download":
            files, setting, choice = self._download
            end = await self._step("download", "--fetch-upscaler", extra=[files, "--accept-licence"])
            if end["state"] != "done" or self._cancel:
                return self._outcome(end)
            self._command = self._new("--set", NO_DISPLAY, [f"{setting}={choice}"])
            end = await self._run(self._command, timeout=QUERY_TIMEOUT)
            if end.get("state") != "done":
                return {"state": "error", "message": f"Downloaded, but not selected: {end.get('message') or 'unknown error'}"}
            return {"state": "done", "message": f"Downloaded and selected. It applies the next time the game starts."}
        if kind == "update":
            end = await self._step("update", "--update")
            self._updated = end["state"] == "done" and end.get("updated") is not False
            if end["state"] != "done" or self._cancel:
                return self._outcome(end)
            if end.get("rebuild") is False:
                return {"state": "done", "message": end.get("message") or "Updated."}
            # a launcher without the commands would ignore --rebuild and, with a display, open its window
            if not self._cancel and not await self._driveable():
                return {"state": "error", "message": f"{end.get('message') or 'Updated.'} {NEW_TOO_OLD}"}
        # the new launcher's, after an update
        return self._outcome(await self._step("rebuild", "--rebuild", self.display()))

    async def _driveable(self):
        """Whether the install's launcher answers --status, asked with no display."""
        try:
            self._command = self._new("--status", NO_DISPLAY)
            return (await self._run(self._command, timeout=QUERY_TIMEOUT)).get("state") == "done"
        except LauncherError:
            return False

    async def _step(self, step, argument, display=None, extra=()):
        if self._cancel:
            return {"state": "cancelled"}
        self._command = command = self._new(argument, display, extra)
        self.job.update(step=step, phase=None, message="Starting", fraction=None, counting=False)

        def progress(line):
            done, total = line.get("done") or 0, line.get("total") or 0
            fraction = line.get("fraction")
            if fraction is None and total:
                fraction = min(done / total, 1.0)
            # counting: the part that runs says how far it is. Linking says "0 of 1" until it ends, a step starting says nothing
            counting = total > 1 or 0 < total <= done
            self.job.update(phase=line.get("phase"), message=line.get("message") or "", fraction=fraction, counting=counting)

        end = await self._run(command, progress, prepare_timeout=self.prepare_timeout)
        if command.timed_out and end["state"] == "cancelled" and not self._cancel:
            # the game is built; only the preparation, which saves compile time while playing, was stopped
            return {"state": "done", "message": "Ready to play. Preparing the shaders took too long and was stopped."}
        return end

    def _outcome(self, end):
        if end["state"] == "cancelled":
            return {"state": "cancelled", "message": "Cancelled."}
        return {"state": end["state"], "message": end.get("message") or ""}

    def cancel(self):
        if not self.job:
            return
        self._cancel = True
        self.job["cancelling"] = True
        if self._command:
            self._command.cancel()

    async def close(self):
        """The plugin is being unloaded: nothing new is started and what runs is stopped, apart from an update that does not stop
        in time (Command.stop). The loader allows five seconds."""
        self._closing = self._cancel = True
        await asyncio.gather(*(command.stop() for command in list(self._running)))
        if self._task:
            # it ends by itself, as nothing more is started; cancelling it while it starts a program can hang before Python 3.12
            await asyncio.wait([self._task], timeout=1)
