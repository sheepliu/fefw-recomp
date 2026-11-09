// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <string>
#include <vector>

namespace nxrt::games::fefw {

struct ModItem {
    std::string iid;
    std::string name;
    std::string category;
    unsigned index = 0;
};

struct ModOperation {
    std::string capability;
    std::string label;
    std::string choice_label;
    std::string integer_label;
    int minimum = 1;
    int maximum = 999;
    int initial = 1;
};

struct ModRequestStatus {
    std::string text;
    bool waiting = false;
    bool running = false;
};

bool item_mod_enabled();
bool friendship_mod_enabled();
const std::vector<ModItem>& mod_items();
const std::vector<ModOperation>& mod_operations();
ModRequestStatus item_status();
ModRequestStatus friendship_status();
bool submit_item(const std::string& iid, int quantity, std::string& error);
bool submit_friendship(int amount, std::string& error);
bool cancel_item();
bool cancel_friendship();

}  // namespace nxrt::games::fefw
