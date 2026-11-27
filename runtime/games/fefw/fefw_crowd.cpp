// SPDX-License-Identifier: GPL-3.0-or-later
// Fire Emblem: Fortune's Weave: how many townspeople the hubs show ([fefw] npc_distance in fefw.ini or NXRT_FEFW_NPC_DISTANCE, 1 = the
// game's own; the F1 page's "NPC limit" row writes it).
//
// The hub crowd update (0x1041880, map::zv crowd) shows and hides the townspeople through 0xd039e0 (chara, mode: 0 shown, 1 hidden,
// 2 the crowd's far / released state). Three of the game's limits decide who is drawn, and npc_distance k multiplies all three (tests
// with each one alone still left NPCs popping in a few metres away):
//   - the model limit: "hub character model hash limit" (70), the most distinct NPC models the crowd keeps loaded. At the limit a
//     townsperson whose model is not loaded is put in the far state and not drawn, even close by. This is most of the hub pop-in.
//   - the display distance: "hub character display distance" (80 m), the radius the crowd shows townspeople within.
//   - the draw budget: townspeople are shown nearest first while max(cost / 600000, count / 80) stays under 1.0; once it is reached,
//     every further townsperson that frame is hidden.
// A limit whose factor is 1 gets no hook: at k = 1 nothing is installed and the game runs as it is.
#include "fefw_game.h"

#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <vector>

#include "nxrt/abi.h"
#include "nxrt/config.h"
#include "nxrt/hooks.h"
#include "nxrt/log.h"

namespace nxrt::games::fefw {

namespace {

constexpr const char* kTag = "fefw";

uint32_t fbits(float f) {
    uint32_t u;
    std::memcpy(&u, &f, 4);
    return u;
}

double g_npc = 1.0;                                                // npc_distance
double g_npc_model = 1.0, g_npc_radius = 1.0, g_npc_budget = 1.0;   // what each limit uses (npc_distance unless an ini key overrides it)

// ---- display distance -----------------------------------------------------------------------------------------------------------------
// The update reads the game parameter "hub character display distance" (an int in metres) into s8, then stores R^2, R^2, (R + 40)^2 and
// (R + 100)^2 as the distances at which it shows and hides the townspeople. k multiplies R (the 40 / 100 m margins stay as they are).
constexpr uint64_t kHubRadius = 0x1041c54;     // inline before `fmul s0, s8, s8`: s8 = R
std::atomic<uint32_t> g_last_r{0xffffffff};

void hook_hub_radius(::Ctx* c, uint64_t, void*) {
    const float r = c->v[8].f[0];
    const float scaled = static_cast<float>(r * g_npc_radius);
    c->v[8].f[0] = scaled;
    if (g_last_r.exchange(fbits(r), std::memory_order_relaxed) != fbits(r))
        NXLOG_I(kTag, "hub NPC display distance: the game's %.0f m -> %.0f m (x%.2f)", r, scaled, g_npc_radius);
}

// ---- draw budget ----------------------------------------------------------------------------------------------------------------------
// Each of the update's three passes sets s9 = 1.0 (the limit of max(cost / 600000, count / 80), the same table as the field's
// VisibleController); once it is reached the update sets crowd + 20 and hides every further townsperson that frame. k raises s9 to k.
constexpr uint64_t kCrowdBudget[] = {0x1042680, 0x1042898, 0x1042c20};   // inline after each `fmov s9, #1.0`

void hook_crowd_budget(::Ctx* c, uint64_t, void*) { c->v[9].f[0] = static_cast<float>(g_npc_budget); }

// ---- model limit ----------------------------------------------------------------------------------------------------------------------
// "hub character model hash limit" is stored at crowd + 16 (an int, 70 in the game; crowd + 24 counts the loaded models). k multiplies
// it (rounded up).
constexpr uint64_t kModelLimit = 0x1041b18;    // inline before `str w8, [x19, #16]`: w8 = the limit

void hook_model_limit(::Ctx* c, uint64_t, void*) {
    const int64_t game = static_cast<int32_t>(c->x[8]);
    const int64_t ours = game > 0 ? static_cast<int64_t>(std::ceil(static_cast<double>(game) * g_npc_model)) : game;
    c->x[8] = static_cast<uint32_t>(ours);
    static std::atomic<int64_t> last{-1};
    if (last.exchange(game) != game)
        NXLOG_I(kTag, "hub NPC model limit: the game's %lld -> %lld", static_cast<long long>(game), static_cast<long long>(ours));
}

std::vector<uint32_t> g_hooks;   // crowd_remove takes these out

void add_inline(uint64_t addr, InlineHookFn fn) {
    if (const uint32_t id = hook_add_inline(addr, fn)) g_hooks.push_back(id);
}

}  // namespace

void crowd_install(uint64_t base) {
    const double npc = config_get_double("fefw", "npc_distance", 1.0);
    g_npc = npc > 1.0 ? std::fmin(npc, 4.0) : 1.0;
    // Testing / modding: each of the three limits can be set on its own in the ini (a factor, 1 = the game's own); not on the F1 page.
    auto own = [](const char* key) {
        const double v = config_get_double("fefw", key, 0.0);
        return v > 0.0 ? (v > 1.0 ? std::fmin(v, 8.0) : 1.0) : -1.0;
    };
    const double m = own("npc_model_limit"), r = own("npc_radius"), b = own("npc_budget");
    g_npc_model = m > 0 ? m : g_npc;
    g_npc_radius = r > 0 ? r : g_npc;
    g_npc_budget = b > 0 ? b : g_npc;
    if (g_npc_radius > 1.0) add_inline(base + kHubRadius, &hook_hub_radius);
    if (g_npc_model > 1.0) add_inline(base + kModelLimit, &hook_model_limit);
    if (g_npc_budget > 1.0)
        for (uint64_t off : kCrowdBudget) add_inline(base + off, &hook_crowd_budget);
    if (!g_hooks.empty())
        NXLOG_I(kTag, "hub NPCs x%.2f ([fefw] npc_distance; 1 = the game's own): model limit x%.2f%s, display distance x%.2f%s, budget x%.2f%s",
                g_npc, g_npc_model, m > 0 ? " ([fefw] npc_model_limit)" : "", g_npc_radius, r > 0 ? " ([fefw] npc_radius)" : "",
                g_npc_budget, b > 0 ? " ([fefw] npc_budget)" : "");
}

void crowd_remove() {
    for (uint32_t id : g_hooks) hook_remove(id);
    g_hooks.clear();
}

}  // namespace nxrt::games::fefw
