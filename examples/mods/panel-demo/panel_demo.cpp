// SPDX-License-Identifier: GPL-3.0-or-later
// A sample code mod: its own settings on the F1 menu's Mods tab (mod API version 4, runtime/include/nxrt/mod_api.h). It changes
// nothing in the game and needs no hook sites, so it needs no Set up. Building and installing: README.md beside this file.
//
// What a mod with a panel has to get right:
//   1. check api->version >= 4 before using add_panel;
//   2. the panel function runs on the thread that presents frames, on_frame on the game's main thread: share only atomics (or locked
//      data) between them, and leave game calls and slow work, such as writing files, to on_frame;
//   3. the port saves nothing for a mod: this one keeps its own settings file.
#define _CRT_SECURE_NO_WARNINGS   // Visual Studio: std::sscanf is the portable call
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <string>

#include <nxrt/mod_api.h>

namespace {

const NxModApi* g_api = nullptr;
std::string g_settings_path;

// The settings: written by the panel, read by the frame callback
std::atomic<int> g_enabled{1};
std::atomic<int> g_count{5};
std::atomic<float> g_scale{0.5f};

std::atomic<bool> g_unsaved{false};
std::atomic<bool> g_save_wanted{false};   // the panel asks, on_frame saves
std::atomic<uint64_t> g_frames{0};

void load_settings() {
    char text[128] = {};
    const int64_t size = g_api->read_file(g_settings_path.c_str(), text, sizeof text - 1);
    if (size < 0 || size >= static_cast<int64_t>(sizeof text)) return;   // no file yet, or not one this mod wrote: the defaults
    int enabled = 0, count = 0;
    float scale = 0.f;
    if (std::sscanf(text, "enabled=%d count=%d scale=%f", &enabled, &count, &scale) != 3) return;
    // the file can be edited by hand: keep every value in range
    g_enabled = enabled != 0;
    g_count = std::clamp(count, 0, 10);
    g_scale = std::clamp(scale, 0.f, 1.f);
}

void save_settings() {
    g_unsaved = false;   // first: a change made while this writes marks the settings unsaved again
    char text[128];
    const int n = std::snprintf(text, sizeof text, "enabled=%d\ncount=%d\nscale=%.2f\n", g_enabled.load(), g_count.load(),
                                static_cast<double>(g_scale.load()));
    if (g_api->write_file(g_settings_path.c_str(), text, static_cast<uint64_t>(n))) {
        g_api->log(g_api, 2, "settings saved");
    } else {
        g_unsaved = true;
        g_api->log(g_api, 1, "the settings file cannot be written");
    }
}

// Once per presented frame, on the game's main thread. A real mod does its work here, with the guest registers in c.
void on_frame(Ctx*, void*) {
    ++g_frames;
    if (g_save_wanted.exchange(false)) save_settings();
}

// The panel: called every frame it is on screen, on the presenting thread.
void panel(const NxModUi* ui, void*) {
    ui->text("A sample mod. Nothing here changes the game.");

    int enabled = g_enabled;
    if (ui->checkbox("Enabled", &enabled)) {
        g_enabled = enabled;
        g_unsaved = true;
    }
    ui->tooltip("The two sliders are greyed out while this is off.");

    ui->begin_disabled(!enabled);
    int count = g_count;
    if (ui->slider_int("Count", &count, 0, 10, nullptr)) {
        g_count = std::clamp(count, 0, 10);   // not every runtime limits a typed value: check it before use
        g_unsaved = true;
    }
    float scale = g_scale;
    if (ui->slider_float("Scale", &scale, 0.f, 1.f, nullptr)) {
        g_scale = std::clamp(scale, 0.f, 1.f);
        g_unsaved = true;
    }
    ui->end_disabled();

    ui->separator("Settings file");
    if (ui->button("Save")) g_save_wanted = true;
    ui->same_line();
    if (ui->button("Defaults")) {
        g_enabled = 1;
        g_count = 5;
        g_scale = 0.5f;
        g_unsaved = true;
    }
    if (g_unsaved) ui->text_colored(0xFFA842FF, "Not saved yet");   // 0xRRGGBBAA
    else ui->text_disabled("Nothing to save");

    if (ui->tree_node("Details", 0)) {
        char line[64];
        std::snprintf(line, sizeof line, "Frames since the mod started: %llu", static_cast<unsigned long long>(g_frames.load()));
        ui->text(line);
        ui->tree_pop();
    }
}

}  // namespace

extern "C" __declspec(dllexport) int nxmod_init(const NxModApi* api, const char* mod_dir) {
    if (api->version < 4) {
        api->log(api, 1, "this mod needs mod API version 4 (settings panels)");
        return 1;   // nonzero: the mod declines
    }
    g_api = api;
    g_settings_path = std::string(mod_dir) + "/settings.txt";
    load_settings();
    api->on_frame(&on_frame, nullptr);
    api->add_panel("Sample mod", &panel, nullptr);
    return 0;
}
