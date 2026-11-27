# SPDX-License-Identifier: GPL-3.0-or-later
"""The Decky plugin's backend: the panel's calls, handed to py_modules/fefw_launcher.py."""

import os

import decky

import fefw_launcher


class Plugin:
    async def _main(self):
        async def done(result):
            decky.logger.info("job ended: %s", result)
            await decky.emit("fefw_job_done", result)

        self.controller = fefw_launcher.Controller(
            os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json"), decky.DECKY_USER_HOME, on_done=done,
            plugin_version=decky.DECKY_PLUGIN_VERSION, data_dir=decky.DECKY_PLUGIN_RUNTIME_DIR)

    async def _unload(self):
        await self.controller.close()

    def _answer(self, action=None):
        """The state after `action`; what it refused is in "error"."""
        if action:
            try:
                action()
            except fefw_launcher.LauncherError as e:
                self.controller.error = str(e)
        return self.controller.state()

    async def get_state(self):
        return self._answer()

    async def watch(self):
        return self.controller.watch()

    async def set_install(self, folder: str):
        return self._answer(lambda: self.controller.set_install(folder))

    async def refresh(self):
        await self.controller.refresh()
        return self._answer()

    async def reread(self):
        await self.controller.reread()
        return self._answer()

    async def start(self, kind: str):
        return self._answer(lambda: self.controller.start(kind))

    async def set_keep_awake(self, on: bool):
        return self._answer(lambda: self.controller.set_keep_awake(on))

    async def hold_sleep(self, battery: int, ac: int):
        # not _answer: the panel must know when this could not be recorded, and then leaves the timers alone
        self.controller.hold_sleep(battery, ac)

    async def release_sleep(self):
        self.controller.release_sleep()

    async def note(self, text: str):
        decky.logger.info("panel: %s", text)

    async def set_setting(self, setting: str, choice: str):
        try:
            await self.controller.set_setting(setting, choice)
        except fefw_launcher.LauncherError as e:
            self.controller.error = str(e)
        return self._answer()

    async def download(self, kind: str, setting: str, choice: str):
        return self._answer(lambda: self.controller.download(kind, setting, choice))

    async def cancel(self):
        return self._answer(self.controller.cancel)
