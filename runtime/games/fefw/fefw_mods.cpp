// SPDX-License-Identifier: GPL-3.0-or-later
// Package metadata and requests stay on the host; mutations run at guest-owned events.
#include "fefw_game.h"
#include "fefw_mods.h"

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "nxrt/dispatch.h"
#include "nxrt/hooks.h"
#include "nxrt/log.h"
#include "nxrt/memory.h"

namespace nxrt::games::fefw {
namespace {

namespace fs = std::filesystem;
using json = nlohmann::json;
constexpr uint64_t kBase = mem::kMainBase;
constexpr uint64_t kLookup = 0x6ea2c0;
constexpr uint64_t kCommit = 0x8a25c0;
constexpr uint64_t kScoreWriter = 0x8ec500;
constexpr uint64_t kGiftReturn = 0x10262cc;
constexpr char kBuildId[] = "DB93B0125F4850601D61C3EDDE012F8EB2ECFE77000000000000000000000000";

struct Request {
    bool waiting = false;
    bool running = false;
    std::string text;
    std::string iid;
    int number = 0;
    unsigned index = 0;
    Ctx* ctx = nullptr;
    uint64_t score_ptr = 0;
};

struct State {
    std::mutex mu;
    bool item_enabled = false;
    bool friendship_enabled = false;
    bool friendship_x5 = false;
    std::vector<ModItem> items;
    std::vector<ModOperation> operations;
    Request item, friendship;
    uint32_t item_hook = 0, friendship_hook = 0, gift_return_hook = 0;
    uint64_t scratch = 0;
};

State& st() {
    static State s;
    return s;
}

json read_json(const fs::path& p) {
    std::ifstream in(p, std::ios::binary);
    if (!in) throw std::runtime_error("cannot open " + p.string());
    return json::parse(in);
}

ModOperation load_operation(const fs::path& mods, const std::string& id, const std::string& capability) {
    const auto j = read_json(mods / id / "manifest.json");
    if (j.value("manifest_version", 0) != 1 || j.value("capability_version", 0) != 1 ||
        j.value("type", "") != "runtime" || j.value("id", "") != id ||
        j.value("game_build_id", "") != kBuildId) throw std::runtime_error("runtime manifest mismatch: " + id);
    for (const auto& op : j.at("operations")) {
        if (op.value("capability", "") != capability) continue;
        ModOperation out;
        out.capability = capability;
        out.label = op.value("label", id);
        for (const auto& field : op.at("fields")) {
            if (field.value("type", "") == "choice") out.choice_label = field.value("label", "Item");
            if (field.value("type", "") == "integer") {
                out.integer_label = field.value("label", "Amount");
                out.minimum = field.value("minimum", 1);
                out.maximum = field.value("maximum", capability == "inventory.add" ? 999 : 50000);
                out.initial = field.value("default", capability == "inventory.add" ? 1 : 500);
            }
        }
        if (out.minimum < 1 || out.maximum > 50000 || out.maximum < out.minimum ||
            out.initial < out.minimum || out.initial > out.maximum || out.integer_label.empty() ||
            (capability == "inventory.add" && out.choice_label.empty()))
            throw std::runtime_error("invalid runtime fields: " + id);
        return out;
    }
    throw std::runtime_error("unsupported runtime operation: " + id);
}

std::vector<ModItem> read_items(const fs::path& p) {
    const auto j = read_json(p);
    if (!j.is_array() || j.size() > 5000) throw std::runtime_error("invalid item catalog");
    std::vector<ModItem> items;
    items.reserve(j.size());
    for (const auto& row : j) {
        const std::string iid = row.value("iid", "");
        const int index = row.value("item_index", 0);
        if (iid.size() < 5 || iid.size() > 256 || iid.rfind("IID_", 0) != 0 || index <= 0 || index > 65535) continue;
        const std::string name = row.value("name_zh_cn", row.value("name_jp", iid));
        items.push_back({iid, name.empty() ? iid : name, row.value("category_heuristic", "Other"), static_cast<unsigned>(index)});
    }
    return items;
}

uint64_t item_hash(const std::string& iid) {
    uint64_t h = 0xcbf29ce484222325ull;
    for (unsigned char b : iid) h = (h * 0x100000001b3ull) ^ b;
    return h ? h : ~uint64_t{0};
}

// Only the event's guest thread has a valid inventory/TLS context. Nested lifted calls
// use a copy of its CPU state, and guest-mapped scratch instead of host stack addresses.
int on_commit(Ctx* c, uint64_t, void*) {
    static thread_local bool nested = false;
    if (nested) return 0;
    State& s = st();
    std::string iid;
    int quantity = 0;
    unsigned expected = 0;
    {
        std::lock_guard lk(s.mu);
        if (!s.item.waiting) return 0;
        s.item.waiting = false;
        s.item.running = true;
        s.item.text = "Processing inventory event...";
        iid = s.item.iid;
        quantity = s.item.number;
        expected = s.item.index;
    }
    std::string result = "Item lookup failed; nothing added.";
    const uint64_t sp = c->x[31];
    if (s.scratch && mem::is_mapped(c->x[0], 8) && sp >= 0x400 && mem::is_mapped(sp - 0x400, 0x400)) {
        if (GuestFn lookup = fn_lookup(kBase + kLookup); lookup && fn_lookup(kBase + kCommit)) {
            uint64_t descriptor = 0, hash = item_hash(iid);
            std::memcpy(reinterpret_cast<void*>(s.scratch), &descriptor, 8);
            std::memcpy(reinterpret_cast<void*>(s.scratch + 8), &hash, 8);
            Ctx call = *c;
            call.x[0] = s.scratch;
            call.x[1] = s.scratch + 8;
            call.x[31] = (sp - 0x300) & ~uint64_t{15};
            nested = true;
            lookup(&call);
            std::memcpy(&descriptor, reinterpret_cast<const void*>(s.scratch), 8);
            if ((descriptor & 0xffffu) == expected) {
                call = *c;
                call.x[1] = s.scratch;
                call.x[2] = static_cast<uint64_t>(quantity);
                call.x[31] = (sp - 0x300) & ~uint64_t{15};
                dispatch_call(&call, kBase + kCommit);
                result = "Inventory operation completed.";
            } else {
                result = "Item lookup index did not match the catalog; nothing added.";
            }
            nested = false;
        } else result = "Lifted item lookup or commit function is unavailable.";
    } else result = "Inventory context or guest scratch is unavailable.";
    {
        std::lock_guard lk(s.mu);
        s.item.running = false;
        s.item.text = result;
    }
    NXLOG_I("mods", "item-add: %s (iid=%s, quantity=%d)", result.c_str(), iid.c_str(), quantity);
    return 0;
}

// The gift's writer call returns to main+0x10262cc. Dining uses the same writer
// with another return site/mode and must never consume an armed gift request.
int on_score(Ctx* c, uint64_t, void*) {
    State& s = st();
    if (c->x[30] != kBase + kGiftReturn || c->x[2] != 0 || !mem::is_mapped(c->x[0], 16)) return 0;
    std::lock_guard lk(s.mu);
    if (!s.friendship.waiting) return 0;
    s.friendship.waiting = false;
    s.friendship.running = true;
    s.friendship.ctx = c;
    s.friendship.score_ptr = c->x[0];
    c->x[1] = static_cast<uint64_t>(s.friendship_x5 ? s.friendship.number / 5 : s.friendship.number);
    s.friendship.text = "Gift writer reached; awaiting completion...";
    return 0;
}

void on_gift_return(Ctx* c, uint64_t, void*) {
    State& s = st();
    std::lock_guard lk(s.mu);
    if (!s.friendship.running || s.friendship.ctx != c) return;
    s.friendship.running = false;
    s.friendship.ctx = nullptr;
    s.friendship.text = "Gift operation completed.";
    NXLOG_I("mods", "friendship-adjust: gift writer returned (requested=%d)", s.friendship.number);
}

ModRequestStatus status(Request& r) {
    std::lock_guard lk(st().mu);
    return {r.text, r.waiting, r.running};
}

bool cancel(Request& r) {
    std::lock_guard lk(st().mu);
    if (!r.waiting) return false;
    r.waiting = false;
    r.text = "Request cancelled.";
    return true;
}

}  // namespace

void mods_install(uint64_t base) {
    State& s = st();
    if (base != kBase) {
        NXLOG_W("mods", "unexpected main base; runtime mods disabled");
        return;
    }
    s.items.clear();
    s.operations.clear();
    s.friendship_x5 = false;
    try {
        const fs::path mods = fs::current_path() / "mods";
        if (!fs::exists(mods.parent_path() / "mods-state.json")) return;
        const auto enabled = read_json(mods.parent_path() / "mods-state.json").at("enabled");
        if (!enabled.is_array()) return;
        for (const auto& id : enabled) {
            if (!id.is_string()) continue;
            if (id == "friendshipgain5x") s.friendship_x5 = true;
            if (id == "item-add") {
                ModOperation op = load_operation(mods, "item-add", "inventory.add");
                s.items = read_items(mods / "item-add" / "items.json");
                s.item_enabled = !s.items.empty();
                if (s.item_enabled) s.operations.push_back(std::move(op));
            }
            if (id == "friendship-adjust") {
                s.operations.push_back(load_operation(mods, "friendship-adjust", "friendship.adjust"));
                s.friendship_enabled = true;
            }
        }
        if (s.item_enabled) {
            s.scratch = mem::alloc_kernel(16, 16);
            if (!s.scratch) s.item_enabled = false;
            else s.item_hook = hook_add_entry(base + kCommit, &on_commit);
        }
        if (s.friendship_enabled) {
            s.friendship_hook = hook_add_entry(base + kScoreWriter, &on_score);
            s.gift_return_hook = hook_add_inline(base + kGiftReturn, &on_gift_return);
        }
        NXLOG_I("mods", "runtime packages: item-add=%d (%zu items), friendship-adjust=%d, friendshipgain5x=%d",
                s.item_enabled, s.items.size(), s.friendship_enabled, s.friendship_x5);
    } catch (const std::exception& e) {
        for (auto* h : {&s.item_hook, &s.friendship_hook, &s.gift_return_hook}) {
            if (*h) hook_remove(*h);
            *h = 0;
        }
        s.item_enabled = s.friendship_enabled = false;
        s.items.clear();
        s.operations.clear();
        NXLOG_W("mods", "runtime package load failed: %s", e.what());
    }
}

void mods_remove() {
    State& s = st();
    for (auto* h : {&s.item_hook, &s.friendship_hook, &s.gift_return_hook}) {
        if (*h) hook_remove(*h);
        *h = 0;
    }
    std::lock_guard lk(s.mu);
    s.item.waiting = s.friendship.waiting = false;
    s.item_enabled = s.friendship_enabled = false;
    s.items.clear();
    s.operations.clear();
}

bool item_mod_enabled() { return st().item_enabled; }
bool friendship_mod_enabled() { return st().friendship_enabled; }
const std::vector<ModItem>& mod_items() { return st().items; }
const std::vector<ModOperation>& mod_operations() { return st().operations; }
ModRequestStatus item_status() { return status(st().item); }
ModRequestStatus friendship_status() { return status(st().friendship); }
bool cancel_item() { return cancel(st().item); }
bool cancel_friendship() { return cancel(st().friendship); }

bool submit_item(const std::string& iid, int quantity, std::string& error) {
    State& s = st();
    std::lock_guard lk(s.mu);
    if (!s.item_enabled || quantity < 1 || quantity > 999 || s.item.waiting || s.item.running) {
        error = "Mod unavailable, invalid quantity or request already active.";
        return false;
    }
    const auto it = std::find_if(s.items.begin(), s.items.end(), [&](const ModItem& item) { return item.iid == iid; });
    if (it == s.items.end()) { error = "Item is not in the enabled catalog."; return false; }
    s.item.iid = iid;
    s.item.index = it->index;
    s.item.number = quantity;
    s.item.text = "Waiting for an inventory event: store, buy or pick up one item.";
    s.item.waiting = true;
    return true;
}

bool submit_friendship(int amount, std::string& error) {
    State& s = st();
    std::lock_guard lk(s.mu);
    if (!s.friendship_enabled || amount < 1 || amount > 50000 || s.friendship.waiting || s.friendship.running) {
        error = "Mod unavailable, invalid amount or request already active.";
        return false;
    }
    if (s.friendship_x5 && amount % 5) {
        error = "With Friendship 5x enabled, the requested amount must be divisible by 5.";
        return false;
    }
    s.friendship.number = amount;
    s.friendship.text = "Waiting for one gift event.";
    s.friendship.waiting = true;
    return true;
}

}  // namespace nxrt::games::fefw
