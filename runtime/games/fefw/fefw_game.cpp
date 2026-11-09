// SPDX-License-Identifier: GPL-3.0-or-later
// Fire Emblem: Fortune's Weave: the whole game module as one nxrt::game::Info (fefw_game.h). Play-mode hooks come here as they are found.
#include "fefw_game.h"

#include "play_game.h"
#include "play_types.h"

namespace nxrt::games::fefw {

namespace {

// Frame update: the game's present wrapper (main 0x5d170: nvnWindowSetPresentInterval when the interval changed, nvnQueuePresentTexture),
// once per presented frame on the main thread, after the frame's input was read: it pumps play mode (window events, the gamepad into the
// npad state the next frame samples). Pacing stays in the present (nvn_present.cpp, the game's own rate): [latency] pace defaults to
// present for a game that sets present_interval_hz (extras/lowlat.cpp), so this hook never runs the limiter.
constexpr uint64_t kPresentWrapper = 0x5d170;

// play mode's install / remove / frame: the game hooks (fefw_play.cpp) and the button glyphs (fefw_prompts.cpp)
void install_all(uint64_t base, play::GameHost* host, const play::Settings& s) {
    play_install(base, host, s);
    mods_install(base);
    prompts_install(host);
}
void remove_all() {
    mods_remove();
    prompts_remove();
    play_remove();
}
void frame_all(bool, double, play::GameFrame*) { prompts_frame(); }

const play::GamePlay& game_play() {
    static const play::GamePlay t = [] {
        play::GamePlay p;
        p.frame_update = kPresentWrapper;
        p.install = &install_all;
        p.remove = &remove_all;
        p.frame = &frame_all;
        p.settings_menu = &settings_menu;   // fefw_menu.cpp: the F1 menu in place of the base settings page
        return p;
    }();
    return t;
}

}  // namespace

const game::Info& full_info() {
    static const game::Info info = [] {
        game::Info g = core_info();
        g.play = &game_play();
        return g;
    }();
    return info;
}

}  // namespace nxrt::games::fefw

void nxrt_register_game_fefw() { nxrt::game::set_current(&nxrt::games::fefw::full_info()); }
