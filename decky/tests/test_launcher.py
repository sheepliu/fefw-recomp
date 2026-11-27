# SPDX-License-Identifier: GPL-3.0-or-later
"""The plugin's backend against a stand-in launcher: python3 -m unittest discover -s tests"""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "py_modules"))
import fefw_launcher as fl  # noqa: E402

# What each command does comes from plan.json beside it; what it was run with goes to runs.jsonl.
FAKE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
argument = sys.argv[1]
extra = sys.argv[2:]
with open(os.path.join(here, "runs.jsonl"), "a") as f:
    f.write(json.dumps({"argument": argument, "extra": extra, "env": dict(os.environ), "pid": os.getpid()}) + "\n")
def out(event, **fields):
    print(json.dumps(dict(fields, event=event, api=fields.pop("api", 1))), flush=True)
for do, value in json.load(open(os.path.join(here, "plan.json")))[argument]:
    if do == "progress": out("progress", **value)
    elif do == "end": out("end", **value); sys.exit(0 if value["state"] == "done" else 1)
    elif do == "text": print(value, flush=True)
    elif do == "sleep": time.sleep(value)
    elif do == "await-cancel":
        for line in sys.stdin:
            if line.strip() == "cancel": break
        out("end", state="cancelled", message="Paused."); sys.exit(1)
    elif do == "child": subprocess.Popen(["sleep", str(value)])
    elif do == "plan": json.dump(value, open(os.path.join(here, "plan.json"), "w"))  # as an update replaces the launcher
    elif do == "die": print(value, file=sys.stderr, flush=True); sys.exit(101)
'''

STATUS = {"state": "done", "version": "1.0.0", "busy": None, "setup_complete": True, "rebuild": False, "rebuild_reason": "",
          "problem": None}
CHECK = {"state": "done", "newer": True, "latest": {"version": "1.1.0", "notes": "n" * 200_000}}


SETTINGS = {"state": "done", "settings": [{"id": "upscaler", "current": "TAAU"}], "consent": {"fsr": "AMD's terms"}}


def plan(**commands):
    steps = {"--status": [["end", STATUS]], "--check-update": [["end", CHECK]], "--settings": [["end", SETTINGS]],
             "--set": [["end", {"state": "done", "message": "Saved."}]]}
    steps.update({"--" + k.replace("_", "-"): v for k, v in commands.items()})
    return steps


class Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.install = self.root / "game"
        (self.install / "tools" / "build").mkdir(parents=True)
        (self.install / "tools" / "build" / "fefw_build.py").touch()
        launcher = self.install / fl.APPIMAGE
        launcher.write_text(FAKE)
        launcher.chmod(0o755)
        self.done = []

        async def on_done(result):
            self.done.append(result)

        base = {"PATH": os.environ["PATH"], "LD_LIBRARY_PATH": "/loader", "DISPLAY": ":9", "KEPT": "yes"}
        self.controller = fl.Controller(self.root / "settings" / "settings.json", self.root, base_env=base,
                                        display=lambda: {"DISPLAY": ":1"}, on_done=on_done)

    def tearDown(self):
        self.tmp.cleanup()

    def write_plan(self, **commands):
        (self.install / "plan.json").write_text(json.dumps(plan(**commands)))

    def runs(self):
        return [json.loads(line) for line in (self.install / "runs.jsonl").read_text().splitlines()]

    async def ready(self, **commands):
        self.write_plan(**commands)
        self.controller.set_install(str(self.install))
        await self.controller.refresh()
        self.assertIsNone(self.controller.error)

    async def finish(self):
        await asyncio.wait_for(asyncio.shield(self.controller._task), 20)
        self.assertIsNone(self.controller.job)
        return self.controller.result

    async def until(self, condition):
        for _ in range(500):
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail("did not happen")

    def jobs_run(self):
        return [r["argument"] for r in self.runs() if r["argument"] in ("--update", "--rebuild")]


class Install(Case):
    async def test_only_the_game_folder_is_accepted_and_it_is_remembered(self):
        with self.assertRaises(fl.LauncherError):
            self.controller.set_install(str(self.root))
        self.controller.set_install(str(self.install))
        again = fl.Controller(self.controller.settings_file, self.root)
        self.assertEqual(again.install_dir, str(self.install))

    async def test_status_and_check_run_with_no_real_display_or_the_loaders_variables(self):
        await self.ready()
        self.assertEqual(self.controller.status["version"], "1.0.0")
        self.assertEqual(len(self.controller.update["latest"]["notes"]), 200_000)  # a line longer than asyncio's usual limit
        for run in self.runs():
            self.assertNotIn("DISPLAY", run["env"])
            self.assertEqual(run["env"]["WAYLAND_DISPLAY"], fl.NO_DISPLAY["WAYLAND_DISPLAY"])
            self.assertNotIn("LD_LIBRARY_PATH", run["env"])
            self.assertEqual(run["env"]["KEPT"], "yes")
            self.assertEqual(run["env"]["HOME"], str(self.root))

    async def test_a_launcher_without_the_commands_is_named_as_too_old(self):
        (self.install / "plan.json").write_text(json.dumps({"--status": [["die", "Failed to initialize GTK"]]}))
        self.controller.set_install(str(self.install))
        await self.controller.refresh()
        self.assertIn(fl.TOO_OLD, self.controller.error)
        self.assertIn("GTK", self.controller.error)
        self.assertIsNone(self.controller.status)
        with self.assertRaises(fl.LauncherError):
            self.controller.start("rebuild")

    async def test_a_failed_update_check_drops_the_earlier_answer(self):
        await self.ready()
        self.write_plan(check_update=[["end", {"state": "error", "message": "Could not reach GitHub."}]])
        await self.controller.refresh()
        self.assertEqual((self.controller.update, self.controller.error), (None, "Could not reach GitHub."))
        self.assertEqual(self.controller.status["version"], "1.0.0")

    async def test_another_interface_version_is_refused(self):
        self.write_plan(status=[["end", dict(STATUS, api=2)]])
        self.controller.set_install(str(self.install))
        await self.controller.refresh()
        self.assertIn("interface version 2", self.controller.error)

    async def test_a_launcher_that_dies_while_its_child_holds_the_output_is_noticed(self):
        self.write_plan(status=[["child", 8], ["die", "boom"]])
        self.controller.set_install(str(self.install))
        started = time.monotonic()
        await self.controller.refresh()
        self.assertLess(time.monotonic() - started, 6)
        self.assertIn("without an answer", self.controller.error)


class Jobs(Case):
    async def test_update_goes_on_to_rebuild_with_the_sessions_display(self):
        await self.ready(
            update=[["progress", {"phase": "download", "done": 1, "total": 4, "message": "Downloading"}],
                    ["end", {"state": "done", "rebuild": True, "message": "Installed."}]],
            rebuild=[["text", "not JSON"], ["end", {"state": "done", "message": "Ready to play."}]])
        self.controller.start("update")
        with self.assertRaises(fl.LauncherError):
            self.controller.start("rebuild")
        with self.assertRaises(fl.LauncherError):
            self.controller.set_install(str(self.install))
        self.assertEqual(await self.finish(), {"state": "done", "message": "Ready to play."})
        self.assertEqual(self.done, [self.controller.result])
        self.assertEqual(self.jobs_run(), ["--update", "--rebuild"])
        displays = {r["argument"]: r["env"].get("DISPLAY") for r in self.runs()}
        self.assertEqual((displays["--update"], displays["--rebuild"]), (None, ":1"))

    async def test_an_update_that_needs_no_rebuild_stops_there(self):
        await self.ready(update=[["end", {"state": "done", "rebuild": False, "message": "Installed."}]])
        self.controller.start("update")
        self.assertEqual((await self.finish())["message"], "Installed.")
        self.assertEqual(self.jobs_run(), ["--update"])

    async def test_an_update_to_a_launcher_without_the_commands_does_not_run_it_with_a_display(self):
        older = {"--status": [["die", "Failed to initialize GTK"]]}
        for rebuild in (True, None):
            await self.ready(update=[["plan", older], ["end", {"state": "done", "rebuild": rebuild, "message": "Installed."}]])
            self.controller.start("update")
            self.assertEqual(await self.finish(), {"state": "error", "message": f"Installed. {fl.NEW_TOO_OLD}"})
            # nothing from before the update is left to offer it again
            state = self.controller.state()
            self.assertEqual((state["status"], state["update"], state["error"]), (None, None, None))
            await self.controller.refresh()
            self.assertIn(fl.TOO_OLD, self.controller.error)
        self.assertEqual(self.jobs_run(), ["--update", "--update"])
        for run in self.runs():
            if run["argument"] == "--status":
                self.assertNotIn("DISPLAY", run["env"])

    async def test_a_failed_update_does_not_rebuild(self):
        await self.ready(update=[["end", {"state": "error", "message": "No space."}]])
        self.controller.start("update")
        self.assertEqual(await self.finish(), {"state": "error", "message": "No space."})
        self.assertEqual(self.jobs_run(), ["--update"])

    async def test_the_result_does_not_wait_for_the_update_check(self):
        await self.ready(rebuild=[["end", {"state": "done", "message": "Ready to play."}]])
        self.write_plan(rebuild=[["end", {"state": "done", "message": "Ready to play."}]],
                        check_update=[["sleep", 1.5], ["end", CHECK]])
        self.controller.start("rebuild")
        await self.until(lambda: self.done)
        state = self.controller.state()
        self.assertEqual((state["job"], state["result"]["message"], state["update"]), (None, "Ready to play.", None))
        self.assertFalse(self.controller._task.done())
        await self.finish()
        self.assertEqual(self.controller.update["latest"]["version"], "1.1.0")

    async def test_a_slow_check_from_before_does_not_replace_a_newer_answer(self):
        update = [["end", {"state": "done", "rebuild": False, "message": "Installed."}]]
        old = dict(CHECK, latest={"version": "1.1.0"})
        new = dict(CHECK, newer=False)
        # the first job's check is slow and still names the release that the second job installs
        await self.ready(update=update)
        self.write_plan(update=update, check_update=[["sleep", 1.5], ["end", old]])
        self.controller.start("update")
        await self.until(lambda: self.done)
        first = self.controller._task
        await self.until(lambda: [r["argument"] for r in self.runs()].count("--check-update") == 2)
        self.write_plan(update=update, check_update=[["end", new]])
        self.controller.start("update")
        await self.finish()
        await asyncio.wait_for(first, 10)
        self.assertEqual((self.controller.update["newer"], self.controller.error), (False, None))

    async def test_a_status_asked_before_a_job_does_not_replace_the_jobs(self):
        done = [["end", {"state": "done", "message": "Ready to play."}]]
        await self.ready(rebuild=done)
        self.write_plan(status=[["sleep", 1.5], ["end", dict(STATUS, version="0.9.0")]], rebuild=done)
        asked = asyncio.ensure_future(self.controller.refresh())
        await self.until(lambda: [r["argument"] for r in self.runs()].count("--status") == 2)
        self.write_plan(rebuild=done)
        self.controller.start("rebuild")
        await self.finish()
        await asyncio.wait_for(asked, 10)
        self.assertEqual(self.controller.status["version"], "1.0.0")

    async def test_progress_is_shown_and_cancel_reaches_the_launcher(self):
        await self.ready(update=[["progress", {"phase": "download", "done": 1, "total": 4, "message": "Downloading"}],
                                 ["await-cancel", None]])
        self.controller.start("update")
        await self.until(lambda: self.controller.job["message"] == "Downloading")
        self.assertEqual(self.controller.job["fraction"], 0.25)
        self.assertEqual(self.controller.state()["job"]["elapsed"], 0)
        self.controller._started -= 125  # as if it had run for a while
        self.assertEqual(self.controller.state()["job"]["elapsed"], 125)
        self.controller.cancel()
        self.assertTrue(self.controller.job["cancelling"])
        self.assertEqual((await self.finish())["state"], "cancelled")
        self.assertEqual(self.jobs_run(), ["--update"])

    async def test_a_part_that_cannot_count_is_told_apart(self):
        await self.ready(rebuild=[
            ["progress", {"phase": "build", "part": "compile", "done": 9, "total": 10, "fraction": 0.27, "message": "Compiling"}],
            ["sleep", 0.3],
            ["progress", {"phase": "build", "part": "link", "done": 0, "total": 1, "fraction": 0.3, "message": "Linking fefw.exe"}],
            ["await-cancel", None]])
        self.controller.start("rebuild")
        await self.until(lambda: self.controller.job["message"] == "Compiling")
        self.assertTrue(self.controller.job["counting"])
        await self.until(lambda: self.controller.job["message"] == "Linking fefw.exe")
        self.assertEqual((self.controller.job["counting"], self.controller.job["fraction"]), (False, 0.3))
        self.controller.cancel()
        await self.finish()

    async def test_a_cancel_too_late_for_the_update_still_stops_before_the_rebuild(self):
        await self.ready(update=[["progress", {"phase": "install", "done": 0, "total": 0, "message": "Installing"}], ["sleep", 0.5],
                                 ["end", {"state": "done", "rebuild": True, "message": "Installed."}]])
        self.controller.start("update")
        await self.until(lambda: self.controller.job["message"] == "Installing")
        self.controller.cancel()
        self.assertEqual(await self.finish(), {"state": "done", "message": "Installed."})
        self.assertEqual(self.jobs_run(), ["--update"])

    async def test_a_silent_shader_preparation_is_stopped_and_the_game_counts_as_ready(self):
        await self.ready(rebuild=[["progress", {"phase": "prepare", "done": 0, "total": 0, "fraction": 0.9, "message": "Preparing"}],
                                  ["await-cancel", None]])
        self.controller.prepare_timeout = 0.3
        self.controller.start("rebuild")
        result = await self.finish()
        self.assertEqual(result["state"], "done")
        self.assertIn("took too long", result["message"])

    async def test_silence_in_other_steps_is_waited_out(self):
        await self.ready(rebuild=[["progress", {"phase": "build", "done": 0, "total": 0, "message": "Compiling"}], ["sleep", 1.0],
                                  ["end", {"state": "done", "message": "Ready to play."}]])
        self.controller.prepare_timeout = 0.3
        self.controller.start("rebuild")
        self.assertEqual((await self.finish())["message"], "Ready to play.")

    async def test_unloading_cancels_the_running_job(self):
        await self.ready(rebuild=[["progress", {"phase": "build", "done": 0, "total": 0, "message": "Compiling"}],
                                  ["await-cancel", None]])
        self.controller.start("rebuild")
        await self.until(lambda: self.controller.job["message"] == "Compiling")
        pid = self.runs()[-1]["pid"]
        before = len(self.runs())
        await asyncio.wait_for(self.controller.close(), 5)
        await self.until(lambda: not Path(f"/proc/{pid}").exists())
        # the job is over, and nothing was started or reported on the way out
        self.assertTrue(self.controller._task.done())
        self.assertEqual((len(self.runs()), self.done), (before, []))


def plugin_zip(path, version="1.1.0", name="FEFW Recomp", flags=(), extra=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("fefw-recomp/", "")
        z.writestr("fefw-recomp/plugin.json", json.dumps({"name": name, "flags": list(flags)}))
        z.writestr("fefw-recomp/package.json", json.dumps({"version": version}))
        z.writestr("fefw-recomp/dist/index.js", "// panel")
        for member, text in extra:
            z.writestr(member, text)
    return path.read_bytes()


class PluginZip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.zip = self.root / "game" / fl.PLUGIN_ZIP
        self.kept = self.root / "data"

    def offer(self, current="1.0.0"):
        return fl.plugin_offer(self.root / "game", current, self.kept)

    def test_a_newer_plugin_is_offered_from_a_copy_of_its_own(self):
        data = plugin_zip(self.zip)
        offer = self.offer()
        kept = Path(offer["artifact"][len("file://"):])
        self.assertEqual((offer["version"], offer["hash"]), ("1.1.0", hashlib.sha256(data).hexdigest()))
        self.assertEqual((kept.parent, kept.read_bytes()), (self.kept, data))
        # the install's file changes, as in an update: what was offered stays, for a question that is still open
        plugin_zip(self.zip, "1.2.0")
        self.assertEqual(kept.read_bytes(), data)
        newer = self.offer()
        self.assertEqual(newer["version"], "1.2.0")
        self.assertNotEqual(newer["artifact"], offer["artifact"])
        self.assertEqual(kept.read_bytes(), data)
        self.assertEqual(len(list(self.kept.iterdir())), 2)

    def test_only_a_newer_one(self):
        plugin_zip(self.zip, "1.10.0")
        self.assertIsNotNone(self.offer("1.9.3"))
        for current in ("1.10.0", "1.11.0", "2.0.0", None, "dev"):
            self.assertIsNone(self.offer(current), current)

    def test_a_release_comes_after_its_early_versions(self):
        order = ["1.0.9", "1.1.0-alpha", "1.1.0-beta", "1.1.0-beta.2", "1.1.0-beta.10", "1.1.0-rc.1", "1.1.0", "1.1.1-beta"]
        for older, newer in zip(order, order[1:]):
            with self.subTest(f"{older} < {newer}"):
                plugin_zip(self.zip, newer)
                self.assertIsNotNone(self.offer(older))
                plugin_zip(self.zip, older)
                self.assertIsNone(self.offer(newer))
        plugin_zip(self.zip, "1.1.0+build.7")
        self.assertIsNone(self.offer("1.1.0"))

    def test_an_install_without_one(self):
        self.assertIsNone(self.offer())
        self.zip.parent.mkdir(parents=True)
        self.zip.write_bytes(b"not a zip")
        self.assertIsNone(self.offer())
        self.assertFalse(self.kept.exists())

    def test_what_decky_must_not_unpack(self):
        bad = {
            "asks for root": {"flags": ["root"]},
            "asks for root, the old way": {"flags": ["_root"]},
            "another plugin": {"name": "Something else"},
            "no version": {"version": "next"},
            "a second plugin.json that lands on the first": {"extra": [("fefw-recomp/./plugin.json", '{"flags": ["root"]}')]},
            "outside its folder": {"extra": [("other/main.py", "x")]},
            "up and out": {"extra": [("fefw-recomp/../other/main.py", "x")]},
            "absolute": {"extra": [("/etc/thing", "x")]},
            "a backslash": {"extra": [("fefw-recomp\\..\\x", "x")]},
        }
        for what, how in bad.items():
            with self.subTest(what):
                plugin_zip(self.zip, **how)
                self.assertIsNone(self.offer())
        with self.subTest("the same name twice"), self.assertWarns(UserWarning):
            plugin_zip(self.zip, extra=[("fefw-recomp/plugin.json", '{"name": "FEFW Recomp", "flags": ["root"]}')])
        self.assertIsNone(self.offer())

    def test_the_shipped_plugin_passes(self):
        here = Path(__file__).resolve().parent.parent
        about = json.loads((here / "plugin.json").read_text())
        self.assertEqual((about["name"], about["flags"]), (fl.PLUGIN_NAME, []))


class PluginUpdate(Case):
    def setUp(self):
        super().setUp()
        self.controller.plugin_version, self.controller.data_dir = "1.0.0", self.root / "data"

    async def ready(self):
        self.controller.set_install(str(self.install))
        await self.controller.refresh()

    async def finish(self, kind):
        self.controller.start(kind)
        self.assertIsNone(self.controller.state()["plugin_update"])  # not offered while a job runs
        await self.controller._task
        return self.done[-1]

    def package(self, version):
        plugin_zip(self.install / fl.PLUGIN_ZIP, version)

    async def test_the_panel_sees_it_also_after_an_update_made_elsewhere(self):
        self.package("1.1.0")
        self.write_plan()
        await self.ready()
        self.assertEqual(self.controller.state()["plugin_update"]["version"], "1.1.0")

    async def test_also_when_the_launcher_cannot_be_driven(self):
        self.package("2.0.0")
        self.write_plan(status=[["die", "unknown"]])
        await self.ready()
        self.assertEqual(self.controller.state()["plugin_update"]["version"], "2.0.0")

    async def test_an_update_of_the_game_brings_the_offer_with_its_result(self):
        self.write_plan(update=[["end", {"state": "done", "updated": True, "rebuild": False}]])
        await self.ready()
        self.assertIsNone(self.controller.state()["plugin_update"])
        self.package("1.1.0")  # as the update puts it there
        result = await self.finish("update")
        self.assertEqual(result["plugin_update"]["version"], "1.1.0")
        self.assertEqual(self.controller.state()["plugin_update"], result["plugin_update"])

    async def test_other_jobs_and_a_failed_update_do_not_ask(self):
        self.package("1.1.0")
        self.write_plan(update=[["end", {"state": "error", "message": "No network."}]],
                        rebuild=[["end", {"state": "done", "message": "Built."}]])
        await self.ready()
        for kind in ("update", "rebuild"):
            self.assertNotIn("plugin_update", await self.finish(kind), kind)
            self.assertIsNotNone(self.controller.state()["plugin_update"])  # the button stays

    async def test_a_cancelled_rebuild_after_the_update_does_not_ask(self):
        self.package("1.1.0")
        self.write_plan(update=[["end", {"state": "done", "updated": True, "rebuild": True}]],
                        rebuild=[["end", {"state": "cancelled", "message": "Paused."}]])
        await self.ready()
        self.assertNotIn("plugin_update", await self.finish("update"))

    async def test_another_folder_forgets_it(self):
        self.package("1.1.0")
        self.write_plan()
        await self.ready()
        self.controller.set_install(str(self.install))
        self.assertIsNone(self.controller.state()["plugin_update"])


class Watching(Case):
    def asked(self):
        return [r["argument"] for r in self.runs()].count("--status")

    async def busy(self, reason="game"):
        await self.ready(status=[["end", dict(STATUS, busy=reason)]])
        self.controller.busy_again = 0.1

    async def watch_until(self, condition):
        for _ in range(200):
            self.controller.watch()
            if condition():
                return
            await asyncio.sleep(0.02)
        self.fail("did not happen")

    async def test_a_game_that_ends_is_noticed_while_the_panel_asks(self):
        await self.busy()
        self.write_plan()
        self.assertEqual(self.controller.watch()["status"]["busy"], "game")  # not before it is due
        self.assertEqual(self.asked(), 1)
        await self.watch_until(lambda: self.controller.status["busy"] is None)
        # nothing busy: no more looks
        await asyncio.sleep(0.3)
        self.controller.watch()
        await asyncio.sleep(0.2)
        self.assertEqual(self.asked(), 2)

    async def test_only_one_look_at_a_time_and_none_without_asking(self):
        await self.busy("job")
        self.write_plan(status=[["sleep", 0.6], ["end", dict(STATUS, busy="job")]])
        await asyncio.sleep(0.3)
        self.assertEqual(self.asked(), 1)  # the panel is closed
        for _ in range(10):
            self.controller.watch()
            await asyncio.sleep(0.03)
        self.assertEqual(self.asked(), 2)
        await asyncio.wait_for(self.controller._watching, 5)

    async def test_a_look_that_fails_keeps_what_was_read_and_the_error_shown(self):
        await self.busy()
        self.controller.error = "Something to read."
        self.write_plan(status=[["die", "busy"]])
        await self.watch_until(lambda: self.asked() == 2)
        await asyncio.wait_for(self.controller._watching, 5)
        self.assertEqual((self.controller.status["busy"], self.controller.error), ("game", "Something to read."))

    async def test_a_look_does_not_cut_across_a_check_the_player_asked_for(self):
        await self.busy()
        self.write_plan(status=[["sleep", 0.5], ["end", STATUS]])
        await asyncio.sleep(0.2)
        refresh = asyncio.ensure_future(self.controller.refresh())
        await self.until(lambda: self.asked() == 2)
        await asyncio.sleep(0.2)
        self.controller.watch()
        await asyncio.wait_for(refresh, 10)
        self.assertEqual(self.asked(), 2)
        self.assertEqual((self.controller.status["busy"], self.controller.update["newer"]), (None, True))


class Reread(Case):
    def arguments(self):
        return [r["argument"] for r in self.runs()]

    async def test_status_and_settings_are_read_again_without_the_network(self):
        self.write_plan()
        self.controller.set_install(str(self.install))
        await self.controller.reread()
        self.assertEqual(self.arguments(), ["--status", "--settings"])
        self.assertIsNotNone(self.controller.status)
        self.assertIsNotNone(self.controller.settings)
        self.assertIsNone(self.controller.update)

    async def test_a_failed_read_keeps_what_was_read_and_shows_no_error(self):
        await self.ready()
        before = self.controller.status
        self.write_plan(status=[["die", "unmounted"]])
        await self.controller.reread()
        self.assertEqual((self.controller.status, self.controller.error), (before, None))

    async def test_nothing_is_read_without_a_folder(self):
        await self.controller.reread()
        self.assertFalse((self.install / "runs.jsonl").exists())


class Settings(Case):
    def sent(self, argument):
        return [r["extra"] for r in self.runs() if r["argument"] == argument]

    async def test_they_are_read_with_the_status_and_a_choice_is_written_and_read_back(self):
        await self.ready()
        self.assertEqual(self.controller.state()["settings"]["consent"], {"fsr": "AMD's terms"})
        self.write_plan(settings=[["end", dict(SETTINGS, settings=[{"id": "upscaler", "current": "Bilinear"}])]])
        await self.controller.set_setting("upscaler", "AMD FSR 3 (needs AMD's files)")
        self.assertEqual(self.sent("--set"), [["upscaler=AMD FSR 3 (needs AMD's files)"]])
        self.assertEqual(self.controller.settings["settings"][0]["current"], "Bilinear")
        for run in self.runs():
            self.assertNotIn("DISPLAY", run["env"])

    async def test_a_refused_choice_is_said(self):
        await self.ready(set=[["end", {"state": "error", "message": "Another setup or update is running."}]])
        with self.assertRaisesRegex(fl.LauncherError, "Another setup"):
            await self.controller.set_setting("upscaler", "TAAU")

    async def test_a_launcher_without_them_is_not_an_error(self):
        await self.ready(settings=[["die", "Failed to initialize GTK"]])
        self.assertEqual((self.controller.settings, self.controller.error), (None, None))
        self.assertIsNotNone(self.controller.update)
        with self.assertRaises(fl.LauncherError):
            await self.controller.set_setting("upscaler", "TAAU")
        with self.assertRaises(fl.LauncherError):
            self.controller.download("fsr", "upscaler", "FSR")
        self.assertEqual(self.sent("--set"), [])

    async def test_a_download_is_accepted_fetched_and_then_selected(self):
        await self.ready(fetch_upscaler=[["progress", {"phase": "download", "done": 1, "total": 4, "message": "Downloading"}],
                                         ["end", {"state": "done", "message": "Saved 3 files."}]])
        with self.assertRaises(fl.LauncherError):
            self.controller.download("dlss", "upscaler", "DLSS")
        self.controller.download("fsr", "upscaler", "FSR")
        result = await self.finish()
        self.assertEqual(result["state"], "done")
        self.assertIn("selected", result["message"])
        self.assertEqual(self.sent("--fetch-upscaler"), [["fsr", "--accept-licence"]])
        self.assertEqual(self.sent("--set"), [["upscaler=FSR"]])

    async def test_a_failed_or_cancelled_download_selects_nothing(self):
        await self.ready(fetch_upscaler=[["end", {"state": "error", "message": "The download failed."}]])
        self.controller.download("fsr", "upscaler", "FSR")
        self.assertEqual(await self.finish(), {"state": "error", "message": "The download failed."})
        self.write_plan(fetch_upscaler=[["progress", {"phase": "download", "done": 0, "total": 0, "message": "Downloading"}],
                                        ["await-cancel", None]])
        self.controller.download("fsr", "upscaler", "FSR")
        await self.until(lambda: self.controller.job["message"] == "Downloading")
        self.controller.cancel()
        self.assertEqual((await self.finish())["state"], "cancelled")
        self.assertEqual(self.sent("--set"), [])


class Closing(Case):
    # as the real launcher's, these questions do not listen for "cancel"
    def alive(self):
        return [r["pid"] for r in self.runs() if Path(f"/proc/{r['pid']}").exists()]

    async def test_a_question_under_way_is_stopped_and_nothing_new_starts(self):
        self.write_plan(status=[["sleep", 30]])
        self.controller.set_install(str(self.install))
        asked = asyncio.ensure_future(self.controller.refresh())
        await self.until(lambda: (self.install / "runs.jsonl").exists() and self.runs())
        started = time.monotonic()
        await asyncio.wait_for(self.controller.close(), 5)
        self.assertLess(time.monotonic() - started, 2)  # not asked politely first
        await asyncio.wait_for(asked, 5)
        await self.until(lambda: not self.alive())
        await self.controller.refresh()
        self.assertEqual(len(self.runs()), 1)

    async def test_a_question_still_starting_is_stopped_too(self):
        self.write_plan(status=[["sleep", 30]])
        self.controller.set_install(str(self.install))
        create, started = asyncio.create_subprocess_exec, []

        async def slow(*args, **kwargs):
            await asyncio.sleep(0.2)  # close() comes while the program is being started
            started.append(await create(*args, **kwargs))
            return started[-1]

        with mock.patch("asyncio.create_subprocess_exec", slow):
            asked = asyncio.ensure_future(self.controller.refresh())
            await asyncio.sleep(0.05)
            await asyncio.wait_for(self.controller.close(), 5)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].returncode)  # over when close() returns
        await asyncio.wait_for(asked, 5)

    async def test_an_update_that_does_not_stop_is_left_to_finish(self):
        await self.ready(update=[["progress", {"phase": "install", "done": 0, "total": 0, "message": "Installing"}], ["sleep", 6]])
        self.controller.start("update")
        await self.until(lambda: self.controller.job["message"] == "Installing")
        command = self.controller._command
        pid = self.runs()[-1]["pid"]
        started = time.monotonic()
        await asyncio.wait_for(self.controller.close(), 8)
        self.assertLess(time.monotonic() - started, 5)  # what the loader allows
        # as when the loader's process ends: closing asyncio's side must not end the launcher
        command.process._transport.close()
        await asyncio.sleep(0.3)
        self.assertIn(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0], ("R", "S"))  # running, not ended
        self.controller._task.cancel()
        os.kill(pid, 9)


class Awake(Case):
    def reload(self):
        return fl.Controller(self.controller.settings_file, self.root)

    async def test_the_choice_is_on_until_turned_off_and_is_remembered(self):
        self.assertTrue(self.controller.state()["keep_awake"])
        self.controller.set_keep_awake(False)
        self.assertFalse(self.reload().keep_awake)

    async def test_the_users_timers_are_kept_until_released_even_across_a_reload(self):
        self.controller.set_install(str(self.install))
        self.controller.hold_sleep(300, 900)
        self.controller.hold_sleep(0, 0)  # a later one does not replace what was there first
        again = self.reload()
        self.assertEqual((again.held_sleep, again.install_dir), ({"battery": 300, "ac": 900}, str(self.install)))
        self.assertEqual(again.state()["held_sleep"], again.held_sleep)
        again.release_sleep()
        self.assertIsNone(self.reload().held_sleep)

    async def test_a_record_that_cannot_be_saved_is_not_kept_or_dropped(self):
        def full():
            raise OSError(28, "No space left on device")

        self.controller.set_install(str(self.install))
        save, self.controller._save = self.controller._save, full
        with self.assertRaises(OSError):
            self.controller.hold_sleep(300, 900)
        self.assertIsNone(self.controller.state()["held_sleep"])
        self.controller._save = save
        self.controller.hold_sleep(300, 900)
        self.controller._save = full
        with self.assertRaises(OSError):
            self.controller.release_sleep()
        self.assertEqual(self.controller.held_sleep, {"battery": 300, "ac": 900})

    async def test_only_timers_are_recorded(self):
        for bad in ((-1, 0), (None, 3), (1.5, 2), (True, 2)):
            with self.assertRaises(fl.LauncherError):
                self.controller.hold_sleep(*bad)
        self.assertIsNone(self.controller.held_sleep)


class Display(unittest.TestCase):
    def test_the_steam_clients_display_is_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            for pid, name, env in [("3", "bash", "DISPLAY=:7"), ("20", "steam", "HOME=/h"),
                                   ("100", "steam", "DISPLAY=:1\0WAYLAND_DISPLAY=\0XAUTHORITY=/x\0SECRET=1")]:
                (Path(tmp) / pid).mkdir()
                (Path(tmp) / pid / "comm").write_text(name + "\n")
                (Path(tmp) / pid / "environ").write_text(env + "\0")
            self.assertEqual(fl.find_display(tmp), {"DISPLAY": ":1", "XAUTHORITY": "/x"})
            self.assertEqual(fl.find_display(tmp, uid=os.getuid() + 1), {})

    def test_the_users_session_is_named_when_it_is_there(self):
        with tempfile.TemporaryDirectory() as run:
            env = fl.environment({}, "/home/p", {"DISPLAY": ":1"}, run_dir=run)
            self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={run}/bus")
            self.assertEqual((env["XDG_RUNTIME_DIR"], env["DISPLAY"]), (run, ":1"))
        self.assertNotIn("XDG_RUNTIME_DIR", fl.environment({}, "/home/p", run_dir="/nowhere"))


if __name__ == "__main__":
    unittest.main()
