// SPDX-License-Identifier: GPL-3.0-or-later
// The code mods' panels (mod API version 4, nxrt/mod_api.h add_panel): services/mods_dll.cpp keeps them, the F1 menu draws them on its
// Mods tab (gfx/overlay.cpp draw_mod_panels). From EmblemWeave (its #71, by Mi-Kel), by way of ObeliskChamber.
#pragma once
#include <string>
#include <vector>

#include "nxrt/mod_api.h"

namespace nxrt::services::modapi {

struct PanelInfo {
    std::string title;           // as the mod gave it
    std::string mod;             // the mod that added it (folder/dll), for the tooltip; empty when not added by a starting mod
    NxModPanelFn fn = nullptr;
    void* user = nullptr;
};

// api->add_panel
int add_panel(const char* title, NxModPanelFn fn, void* user);
// The panels of the mods that started, in the order added (a copy: any thread)
std::vector<PanelInfo> panels();
// Whether there is any (cheap: the menu asks every frame)
bool has_panels();

// The overlay says whether a panel is on screen; api->panel_visible answers it
void set_panel_visible(bool visible);
int panel_visible();

}  // namespace nxrt::services::modapi
