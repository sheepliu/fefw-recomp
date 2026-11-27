// SPDX-License-Identifier: GPL-3.0-or-later
// GPU crash/device-loss diagnostics (declared in nxrt/diag.h):
//   - first unexpected Vulkan failure (plus a dedicated slot for the first VK_ERROR_DEVICE_LOST so an earlier recoverable
//     error such as VK_ERROR_OUT_OF_DEVICE_MEMORY cannot mask a subsequent device loss)
//   - bounded 64-slot ring of recent compute dispatches on the render thread
//   - shared composer for device-lost dumps (same sections as the freeze dump)
#include <windows.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <string>

#include "nxrt/config.h"
#include "nxrt/diag.h"
#include "nxrt/log.h"

namespace nxrt {
namespace {

using Clock = std::chrono::steady_clock;
const Clock::time_point g_t0 = Clock::now();

constexpr int kVkDeviceLost = -4;

uint64_t ms_since_start() {
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - g_t0).count());
}

const char* file_name(const char* path) {
    if (!path) return "";
    const char* slash = std::strrchr(path, '\\');
    const char* fwd = std::strrchr(path, '/');
    if (fwd > slash) slash = fwd;
    return slash ? slash + 1 : path;
}

// VK_SUCCESS is 0 and Vulkan errors are negative, so nxrt core needs no Vulkan header here.
const char* result_name(int r) {
    switch (r) {
        case -1: return "VK_ERROR_OUT_OF_HOST_MEMORY";
        case -2: return "VK_ERROR_OUT_OF_DEVICE_MEMORY";
        case -3: return "VK_ERROR_INITIALIZATION_FAILED";
        case -4: return "VK_ERROR_DEVICE_LOST";
        case -5: return "VK_ERROR_MEMORY_MAP_FAILED";
        case -6: return "VK_ERROR_LAYER_NOT_PRESENT";
        case -7: return "VK_ERROR_EXTENSION_NOT_PRESENT";
        case -8: return "VK_ERROR_FEATURE_NOT_PRESENT";
        case -9: return "VK_ERROR_INCOMPATIBLE_DRIVER";
        case -10: return "VK_ERROR_TOO_MANY_OBJECTS";
        case -11: return "VK_ERROR_FORMAT_NOT_SUPPORTED";
        case -12: return "VK_ERROR_FRAGMENTED_POOL";
        case -13: return "VK_ERROR_UNKNOWN";
        case -1000000000: return "VK_ERROR_SURFACE_LOST_KHR";
        case -1000000001: return "VK_ERROR_NATIVE_WINDOW_IN_USE_KHR";
        case -1000001004: return "VK_ERROR_OUT_OF_DATE_KHR";
        case -1000069000: return "VK_ERROR_OUT_OF_POOL_MEMORY";
        case -1000072003: return "VK_ERROR_INVALID_EXTERNAL_HANDLE";
        case -1000161000: return "VK_ERROR_FRAGMENTATION";
        case -1000255000: return "VK_ERROR_FULL_SCREEN_EXCLUSIVE_MODE_LOST_EXT";
        case 1000001003: return "VK_SUBOPTIMAL_KHR";
        default: return "VK_ERROR";
    }
}

// Normal non-error results produced during healthy runs (timeouts, resizes, surface recreation).
bool result_expected(int r) {
    switch (r) {
        case 0:            // VK_SUCCESS
        case 1:            // VK_NOT_READY
        case 2:            // VK_TIMEOUT
        case 5:            // VK_INCOMPLETE
        case 1000001003:   // VK_SUBOPTIMAL_KHR
        case -1000001004:  // VK_ERROR_OUT_OF_DATE_KHR
        case -1000000000:  // VK_ERROR_SURFACE_LOST_KHR
            return true;
        default:
            return false;
    }
}

// ---- first Vulkan failure records -----------------------------------------------------------------------------------------
enum : int { kEmpty = 0, kWriting = 1, kReady = 2 };

struct VkFailureSlot {
    std::atomic<int> state{kEmpty};
    int result = 0;
    const char* call = "";
    const char* file = "";
    int line = 0;
    DWORD tid = 0;
    char thread[48] = {};
    uint64_t ms = 0;
    uint64_t frames = 0;
    char submit[400] = {};
};

VkFailureSlot g_first_err;
VkFailureSlot g_first_lost;
std::atomic<std::string (*)()> g_submit_state{nullptr};

void copy_cstr(char* dst, size_t cap, const char* src) {
    if (!cap) return;
    if (!src) { dst[0] = '\0'; return; }
    const size_t n = std::strlen(src);
    const size_t m = n < cap - 1 ? n : cap - 1;
    std::memcpy(dst, src, m);
    dst[m] = '\0';
}

bool record_slot(VkFailureSlot& slot, int result, const char* call, const char* file, int line) {
    int expected = kEmpty;
    if (!slot.state.compare_exchange_strong(expected, kWriting, std::memory_order_acq_rel)) return false;
    slot.result = result;
    slot.call = call ? call : "";
    slot.file = file_name(file);
    slot.line = line;
    slot.tid = GetCurrentThreadId();
    copy_cstr(slot.thread, sizeof slot.thread, log_thread_name());
    slot.ms = ms_since_start();
    slot.frames = diag_frame_count();
    slot.submit[0] = '\0';
    if (auto fn = g_submit_state.load(std::memory_order_acquire)) {
        std::string s = fn();
        while (!s.empty() && (s.back() == '\n' || s.back() == '\r')) s.pop_back();
        copy_cstr(slot.submit, sizeof slot.submit, s.c_str());
    }
    slot.state.store(kReady, std::memory_order_release);
    return true;
}

std::string format_slot(const char* label, const VkFailureSlot& slot) {
    char b[640];
    std::snprintf(b, sizeof b, "%s: %s (%d) from %s at %s:%d (thread %s tid=%lu, t=%.3f s, frames %llu)\n",
                  label, result_name(slot.result), slot.result, slot.call, slot.file, slot.line,
                  slot.thread[0] ? slot.thread : "-", static_cast<unsigned long>(slot.tid),
                  static_cast<double>(slot.ms) / 1000.0, static_cast<unsigned long long>(slot.frames));
    std::string out = b;
    if (slot.submit[0]) {
        out += "  ";
        out += slot.submit;
        out += '\n';
    }
    return out;
}

// ---- recent dispatches ring -----------------------------------------------------------------------------------------------
constexpr size_t kDispatchSlots = 64;

struct DispatchSlot {
    std::atomic<uint8_t> held{0};
    DispatchNote note;
};

DispatchSlot g_dispatch[kDispatchSlots];
std::atomic<uint64_t> g_dispatch_count{0};

bool read_dispatch_slot(DispatchSlot& s, DispatchNote& out) {
    uint8_t expected = 0;
    if (!s.held.compare_exchange_strong(expected, 1, std::memory_order_acquire)) return false;
    out = s.note;
    s.held.store(0, std::memory_order_release);
    return true;
}

}  // namespace

void diag_set_submit_state(std::string (*fn)()) { g_submit_state.store(fn, std::memory_order_release); }

void diag_vk_note(int result, const char* call, const char* file, int line) {
    if (result_expected(result)) return;
    const bool first_err = record_slot(g_first_err, result, call, file, line);
    const bool first_lost = (result == kVkDeviceLost) && record_slot(g_first_lost, result, call, file, line);
    if (first_err) {
        NXLOG_E("gfx", "first Vulkan failure: %s (%d) from %s at %s:%d (frames %llu)",
                result_name(result), result, call ? call : "", file_name(file), line,
                static_cast<unsigned long long>(g_first_err.frames));
    } else if (first_lost) {
        NXLOG_E("gfx", "first VK_ERROR_DEVICE_LOST (-4) from %s at %s:%d (frames %llu)",
                call ? call : "", file_name(file), line,
                static_cast<unsigned long long>(g_first_lost.frames));
    }
}

std::string diag_vk_failure_text(bool include_none) {
    const bool have_err = g_first_err.state.load(std::memory_order_acquire) == kReady;
    const bool have_lost = g_first_lost.state.load(std::memory_order_acquire) == kReady;
    if (!have_err && !have_lost) return include_none ? "GPU first failure: (none)\n" : "";
    std::string out;
    if (have_err) out += format_slot("GPU first failure", g_first_err);
    // Only emit the second slot when an earlier non-device-lost error preceded VK_ERROR_DEVICE_LOST.
    if (have_lost && (!have_err || g_first_err.result != kVkDeviceLost)) {
        out += format_slot("GPU first device lost", g_first_lost);
    }
    return out;
}

void diag_note_dispatch(const DispatchNote& note) {
    static const bool registered = (diag_add_report(&diag_dispatch_text), true);
    (void)registered;
    const uint64_t n = g_dispatch_count.load(std::memory_order_relaxed) + 1;
    DispatchSlot& s = g_dispatch[static_cast<size_t>((n - 1) % kDispatchSlots)];
    DispatchNote copy = note;
    copy.index = n;
    uint8_t expected = 0;
    while (!s.held.compare_exchange_weak(expected, 1, std::memory_order_acquire)) {
        expected = 0;
        YieldProcessor();
    }
    s.note = copy;
    s.held.store(0, std::memory_order_release);
    g_dispatch_count.store(n, std::memory_order_release);
}

std::string diag_dispatch_text() {
    const uint64_t total = g_dispatch_count.load(std::memory_order_acquire);
    if (total == 0) return "";
    int64_t depth = config_get_int("diag", "dispatch_ring", 64);
    if (depth <= 0) return "";
    if (depth > static_cast<int64_t>(kDispatchSlots)) depth = static_cast<int64_t>(kDispatchSlots);
    const uint64_t first = total > static_cast<uint64_t>(depth) ? total - static_cast<uint64_t>(depth) + 1 : 1;
    char b[320];
    std::snprintf(b, sizeof b, "recent dispatches (last %llu of %llu, oldest first; [diag] dispatch_ring):\n",
                  static_cast<unsigned long long>(total - first + 1), static_cast<unsigned long long>(total));
    std::string out = b;
    for (uint64_t i = first; i <= total; ++i) {
        DispatchNote n;
        if (!read_dispatch_slot(g_dispatch[static_cast<size_t>((i - 1) % kDispatchSlots)], n)) {
            out += "  (a dispatch was being written; skipped)\n";
            continue;
        }
        if (n.index != i) {
            out += "  (a dispatch was overwritten while reading; skipped)\n";
            continue;
        }
        char ser[32] = {};
        if (n.serial) std::snprintf(ser, sizeof ser, "  submit %llu", static_cast<unsigned long long>(n.serial));
        const bool miss = (n.flags & DispatchNote::kMissingCs) != 0;
        const char* sh = miss ? "-" : (n.shader[0] ? n.shader : "-");
        const char* miss_pre = (miss && n.missing[0]) ? " (missing " : "";
        const char* miss_key = (miss && n.missing[0]) ? n.missing : "";
        const char* miss_suf = (miss && n.missing[0]) ? ")" : "";
        const char* sh_bar = (n.flags & DispatchNote::kBarrierBefore) ? " (shadow copy-in barrier)" : "";
        if (n.flags & DispatchNote::kIndirect) {
            std::snprintf(b, sizeof b, "  #%llu%s  cs %s%s%s%s  indirect args 0x%llx  barriers %u  bindings %u%s\n",
                          static_cast<unsigned long long>(n.index), ser, sh, miss_pre, miss_key, miss_suf,
                          static_cast<unsigned long long>(n.indirect), n.barriers, n.bindings, sh_bar);
        } else {
            std::snprintf(b, sizeof b, "  #%llu%s  cs %s%s%s%s  %ux%ux%u  direct  barriers %u  bindings %u%s\n",
                          static_cast<unsigned long long>(n.index), ser, sh, miss_pre, miss_key, miss_suf,
                          n.x, n.y, n.z, n.barriers, n.bindings, sh_bar);
        }
        out += b;
    }
    return out;
}

std::string diag_device_lost_dump(const char* where, int result, uint64_t frames, const std::string& context) {
    char b[512];
    std::snprintf(b, sizeof b,
                  "GPU DEVICE LOST (reason: VK_ERROR_DEVICE_LOST)\n"
                  "  %s returned %d after %llu presented frames: the graphics driver reset or removed the device\n"
                  "  (a driver crash or timeout, an out-of-video-memory condition, or a GPU hang). The game was stopped.\n",
                  where ? where : "(unknown)", result, static_cast<unsigned long long>(frames));
    std::string header = b;
    if (!context.empty()) header += context;
    header += frames == 0
                  ? "  What to try: the first frame never finished on the GPU. Update the graphics driver; if it happens in one window mode only "
                    "(windowed, say), use another (F1 > Display) and tell us which works; [gfx] gpu in fefw.ini picks another GPU."
                  : "  What to try: update the graphics driver; on a GPU that shares system memory (laptop, handheld) lower the render scale "
                    "or the texture quality. Please attach this file and fefw.log to an issue.";
    return diag_full_dump(header.c_str(), "device loss");
}

void diag_gpu_reset_for_test() {
    g_first_err.state.store(kEmpty, std::memory_order_release);
    g_first_lost.state.store(kEmpty, std::memory_order_release);
    g_submit_state.store(nullptr, std::memory_order_release);
    g_dispatch_count.store(0, std::memory_order_release);
    for (DispatchSlot& s : g_dispatch) {
        s.held.store(0, std::memory_order_release);
        s.note = DispatchNote{};
    }
}

}  // namespace nxrt
