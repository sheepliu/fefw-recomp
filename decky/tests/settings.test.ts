// SPDX-License-Identifier: GPL-3.0-or-later
// The panel's choice of settings: node --test tests/*.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { allSettings, groupedSettings, shownSettings, type SettingsList } from "../src/settings.ts";

const choice = (label: string, available = true, download: string | null = null) => ({ label, available, download });
const list: SettingsList = {
  consent: {},
  settings: [
    { id: "upscaler", label: "Upscaler", help: "Scales the image up.", current: "NVIDIA DLSS (needs Streamline files)", choices: [
      choice("Built-in temporal (TAAU)"),
      choice("NVIDIA DLSS (needs Streamline files)", false),
      choice("AMD FSR 3 (needs AMD's files)", true, "fsr"),
      choice("Intel XeSS (needs libxess.dll)", false),
    ] },
    { id: "reflections", label: "Reflections", help: "Water and floor reflections.", current: null, choices: [choice("Full"), choice("Off")] },
    { id: "preset", label: "Graphics preset", current: null, choices: [choice("Low"), choice("High")] },
    { id: "npc_distance", label: "Hub NPC draw distance", current: null, choices: [] },
  ],
};

test("the main settings, in the panel's order", () => {
  assert.deepEqual(shownSettings(list).map((s) => s.id), ["preset", "upscaler"]);
});

test("all choice-based settings keep manifest order and help text", () => {
  const all = allSettings(list);
  assert.deepEqual(all.map((s) => s.id), ["upscaler", "reflections", "preset"]);
  assert.equal(all[0].help, "Scales the image up.");
  assert.equal(all[2].help, "");
});

test("choices say when files come first, and unavailable ones stay only while picked", () => {
  const upscaler = shownSettings(list)[1];
  assert.deepEqual(upscaler.options.map((o) => o.label), ["Built-in temporal (TAAU)", "NVIDIA DLSS", "AMD FSR 3 (download)"]);
  assert.deepEqual(upscaler.options[2], { choice: "AMD FSR 3 (needs AMD's files)", label: "AMD FSR 3 (download)", download: "fsr" });
});

test("a setting left with nothing to pick is not listed", () => {
  const frameGen: SettingsList = {
    consent: {},
    settings: [
      { id: "frame_gen", label: "Frame generation", current: "Off", choices: [
        choice("Off"),
        choice("NVIDIA DLSS (RTX 40 or newer)", false),
        choice("AMD FSR 3 (Windows only; needs AMD's file)", false),
      ] },
    ],
  };
  assert.deepEqual(allSettings(frameGen), []);
  // one already in fefw.ini stays listed, so that it can be switched off
  const picked = { ...frameGen.settings[0], current: "AMD FSR 3 (Windows only; needs AMD's file)" };
  assert.deepEqual(allSettings({ consent: {}, settings: [picked] })[0].options.map((o) => o.choice), [
    "Off",
    "AMD FSR 3 (Windows only; needs AMD's file)",
  ]);
});

test("groupedSettings follows the manifest's groups, in its order", () => {
  const two = [choice("A"), choice("B")];
  const groups = groupedSettings({
    consent: {},
    settings: [
      { id: "preset", label: "Preset", group: "Picture", current: null, choices: two },
      { id: "fps_target", label: "Frame rate", group: "Frame rate", current: null, choices: two },
      { id: "upscaler", label: "Upscaler", group: "Picture", current: null, choices: two },
      { id: "future_opt", label: "Future option", current: null, choices: two },
      { id: "single", label: "Single", group: "Input", current: null, choices: [choice("Only")] },
    ],
  });
  assert.deepEqual(
    groups.map((g) => [g.title, g.settings.map((s) => s.id)]),
    [
      ["Picture", ["preset", "upscaler"]],
      ["Frame rate", ["fps_target"]],
      ["Settings", ["future_opt"]],
    ],
  );
});
