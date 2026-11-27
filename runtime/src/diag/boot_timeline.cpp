// SPDX-License-Identifier: GPL-3.0-or-later
// Start-up timeline (nxrt/boot_timeline.h): records start-up milestones and logs one summary line at the first presented frame.
// [diag] boot_timeline = false: no line.
#include "nxrt/boot_timeline.h"

#include <atomic>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>

#include "nxrt/config.h"
#include "nxrt/log.h"

namespace nxrt {
namespace {
struct Mark {
    const char* what;
    double t;
};
constexpr int kMaxMarks = 24;
std::mutex g_mu;
Mark g_marks[kMaxMarks];
int g_n = 0;
std::atomic<bool> g_done{false};
}  // namespace

void boot_mark(const char* what) {
    if (!what || g_done.load(std::memory_order_relaxed)) return;
    const double t = log_uptime();
    std::lock_guard<std::mutex> lk(g_mu);
    for (int i = 0; i < g_n; ++i)
        if (std::strcmp(g_marks[i].what, what) == 0) return;
    if (g_n < kMaxMarks) g_marks[g_n++] = {what, t};
}

void boot_first_frame() {
    if (g_done.load(std::memory_order_relaxed)) return;
    boot_mark("first frame");
    std::lock_guard<std::mutex> lk(g_mu);
    if (g_done.exchange(true)) return;
    static const bool on = config_get_bool("diag", "boot_timeline", true);
    if (!on || g_n == 0) return;
    // marks come from several threads: list them in time order
    for (int i = 1; i < g_n; ++i)
        for (int j = i; j > 0 && g_marks[j].t < g_marks[j - 1].t; --j) std::swap(g_marks[j], g_marks[j - 1]);
    std::string line;
    double prev = 0, longest = -1;
    const char* longest_what = "";
    char buf[96];
    for (int i = 0; i < g_n; ++i) {
        const double step = g_marks[i].t - prev;
        if (step > longest) {
            longest = step;
            longest_what = g_marks[i].what;
        }
        std::snprintf(buf, sizeof buf, "%s%s %.2f (+%.2f)", i ? ", " : "", g_marks[i].what, g_marks[i].t, step);
        line += buf;
        prev = g_marks[i].t;
    }
    NXLOG_I("boot", "timeline, seconds since start (+ the step before): %s; longest step: up to %s, %.2f s", line.c_str(), longest_what, longest);
}

}  // namespace nxrt
