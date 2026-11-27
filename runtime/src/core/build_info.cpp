// SPDX-License-Identifier: GPL-3.0-or-later
#include <mutex>
#include <string>
#include <utility>
#include <vector>

#include "nxrt/abi.h"
#include "nxrt/runtime.h"
namespace nxrt {
#define NXRT_STR2(x) #x
#define NXRT_STR(x) NXRT_STR2(x)
const char* abi_source() { return "recomp/include/nxrecomp/ctx.h, ABI version " NXRT_STR(NXRECOMP_ABI_VERSION); }
int abi_version() { return NXRECOMP_ABI_VERSION; }
namespace {
std::string g_app_build;
std::mutex g_notes_mu;
std::vector<std::pair<std::string, std::string>> g_notes;   // in the order first set
}
void set_app_build(const char* b) { g_app_build = b ? b : ""; }
const char* app_build() { return g_app_build.c_str(); }

void set_session_note(const char* key, const std::string& value) {
    if (!key || !*key) return;
    std::lock_guard<std::mutex> lk(g_notes_mu);
    for (auto& n : g_notes) {
        if (n.first == key) {
            n.second = value;
            return;
        }
    }
    g_notes.emplace_back(key, value);
}

std::string session_stamp() {
    std::string s = *app_build() ? app_build() : "nxrt (no build given)";
    s += ", ABI " NXRT_STR(NXRECOMP_ABI_VERSION);
    std::lock_guard<std::mutex> lk(g_notes_mu);
    for (const auto& n : g_notes) s += "; " + n.first + " " + n.second;
    return s;
}
}  // namespace nxrt
