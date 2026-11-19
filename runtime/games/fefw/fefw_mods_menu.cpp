// SPDX-License-Identifier: GPL-3.0-or-later
#include "fefw_mods.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <string>
#include <vector>

#include "imgui.h"

namespace nxrt::games::fefw {
namespace {

bool contains_folded(const std::string& haystack, const std::string& needle) {
    if (needle.empty()) return true;
    auto fold = [](unsigned char ch) { return static_cast<unsigned char>(std::tolower(ch)); };
    return std::search(haystack.begin(), haystack.end(), needle.begin(), needle.end(),
                       [&](char a, char b) { return fold(a) == fold(b); }) != haystack.end();
}

void item_page(const ModOperation& op) {
    static std::array<char, 128> search{};
    static std::string selected;
    static int amount = 1;
    static std::string error;
    const auto& items = mod_items();
    if (selected.empty() && !items.empty()) selected = items.front().iid;
    ImGui::TextUnformatted(op.label.c_str());
    ImGui::SetNextItemWidth(-1);
    const std::string filter_label = op.choice_label + " (name / IID)";
    ImGui::InputText(filter_label.c_str(), search.data(), search.size());
    std::vector<const ModItem*> shown;
    shown.reserve(items.size());
    for (const auto& item : items) {
        if (contains_folded(item.name, search.data()) || contains_folded(item.iid, search.data()) ||
            contains_folded(item.category, search.data())) shown.push_back(&item);
    }
    ImGui::TextDisabled("%zu / %zu", shown.size(), items.size());
    if (ImGui::BeginChild("##itemlist", ImVec2(0, ImGui::GetTextLineHeightWithSpacing() * 9),
                          ImGuiChildFlags_Borders | ImGuiChildFlags_NavFlattened)) {
        ImGuiListClipper clipper;
        clipper.Begin(static_cast<int>(shown.size()));
        while (clipper.Step()) for (int i = clipper.DisplayStart; i < clipper.DisplayEnd; ++i) {
            const ModItem& item = *shown[static_cast<size_t>(i)];
            ImGui::PushID(item.iid.c_str());
            const std::string label = item.name + "  [" + item.iid + "]";
            if (ImGui::Selectable(label.c_str(), selected == item.iid)) selected = item.iid;
            ImGui::PopID();
        }
    }
    ImGui::EndChild();
    ImGui::SetNextItemWidth(140);
    if (ImGui::InputInt(op.integer_label.c_str(), &amount)) amount = std::clamp(amount, op.minimum, op.maximum);
    const auto status = item_status();
    ImGui::BeginDisabled(status.waiting || status.running || selected.empty());
    if (ImGui::Button("Apply##item")) {
        if (submit_item(selected, amount, error)) error.clear();
    }
    ImGui::EndDisabled();
    ImGui::SameLine();
    ImGui::BeginDisabled(!status.waiting);
    if (ImGui::Button("Cancel##item")) cancel_item();
    ImGui::EndDisabled();
    ImGui::TextWrapped("%s", error.empty() ? status.text.c_str() : error.c_str());
}

void friendship_page(const ModOperation& op) {
    static int amount = op.initial;
    static std::string error;
    ImGui::TextUnformatted(op.label.c_str());
    ImGui::SetNextItemWidth(140);
    if (ImGui::InputInt(op.integer_label.c_str(), &amount)) amount = std::clamp(amount, op.minimum, op.maximum);
    const auto status = friendship_status();
    ImGui::BeginDisabled(status.waiting || status.running);
    if (ImGui::Button("Apply##friendship")) {
        if (submit_friendship(amount, error)) error.clear();
    }
    ImGui::EndDisabled();
    ImGui::SameLine();
    ImGui::BeginDisabled(!status.waiting);
    if (ImGui::Button("Cancel##friendship")) cancel_friendship();
    ImGui::EndDisabled();
    ImGui::TextWrapped("%s", error.empty() ? status.text.c_str() : error.c_str());
}

}  // namespace

void mods_page() {
    ImGui::TextUnformatted("Runtime mods");
    ImGui::Separator();
    const auto& operations = mod_operations();
    for (size_t index = 0; index < operations.size(); ++index) {
        const auto& op = operations[index];
        if (index) { ImGui::Spacing(); ImGui::Separator(); ImGui::Spacing(); }
        ImGui::PushID(static_cast<int>(index));
        if (op.capability == "inventory.add" && item_mod_enabled()) item_page(op);
        else if (op.capability == "friendship.adjust" && friendship_mod_enabled()) friendship_page(op);
        ImGui::PopID();
    }
    if (operations.empty())
        ImGui::TextDisabled("No runtime mod enabled. Install and enable one in the launcher, then restart the game.");
}

}  // namespace nxrt::games::fefw
