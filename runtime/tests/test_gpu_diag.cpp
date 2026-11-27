// SPDX-License-Identifier: GPL-3.0-or-later
// Unit tests for GPU crash/device-loss diagnostics (nxrt/diag.h, src/diag/gpu_diag.cpp).
#include "test_framework.h"

#include "nxrt/config.h"
#include "nxrt/diag.h"

#include <windows.h>

#include <cstdlib>
#include <cstring>
#include <string>

NXTEST_GROUP(gpu_diag)

namespace {

bool has(const std::string& s, const char* needle) { return s.find(needle) != std::string::npos; }

std::string fake_submit_state() { return "NVN back end: last submitted serial 42, timeline value 40\n"; }

void feed_dispatches(uint64_t count) {
    for (uint64_t i = 1; i <= count; ++i) {
        nxrt::DispatchNote n;
        std::snprintf(n.shader, sizeof n.shader, "%016llx", static_cast<unsigned long long>(0x1000000000000000ull + i));
        n.serial = 100 + i;
        n.barriers = static_cast<uint32_t>(i % 5);
        n.bindings = 7;
        if (i == count) {
            n.flags = nxrt::DispatchNote::kIndirect | nxrt::DispatchNote::kBarrierBefore;
            n.indirect = 0x78001f2a9000ull;
        } else {
            n.x = static_cast<uint32_t>(i);
            n.y = 2;
            n.z = 1;
        }
        nxrt::diag_note_dispatch(n);
    }
}

}  // namespace

NXTEST(expected_results_are_ignored) {
    nxrt::diag_gpu_reset_for_test();
    nxrt::diag_vk_note(0, "vkSuccess", __FILE__, __LINE__);
    nxrt::diag_vk_note(1, "vkNotReady", __FILE__, __LINE__);
    nxrt::diag_vk_note(2, "vkTimeout", __FILE__, __LINE__);
    nxrt::diag_vk_note(5, "vkIncomplete", __FILE__, __LINE__);
    nxrt::diag_vk_note(1000001003, "vkSuboptimal", __FILE__, __LINE__);
    nxrt::diag_vk_note(-1000001004, "vkOutOfDate", __FILE__, __LINE__);
    nxrt::diag_vk_note(-1000000000, "vkSurfaceLost", __FILE__, __LINE__);
    CHECK_EQ(nxrt::diag_vk_failure_text(), std::string(""));
    CHECK_MSG(has(nxrt::diag_vk_failure_text(true), "GPU first failure: (none)"), "include_none should report (none)");
}

NXTEST(first_failure_and_device_lost_not_masked_by_oom) {
    nxrt::diag_gpu_reset_for_test();
    nxrt::diag_set_submit_state(&fake_submit_state);

    // An earlier recoverable OOM records g_first_err, but must NOT mask a subsequent VK_ERROR_DEVICE_LOST.
    nxrt::diag_vk_note(-2, "vkAllocateMemory", "vk_device.cpp", 42);
    nxrt::diag_vk_note(-4, "vkQueueSubmit2 (nvn batch)", "nvn_gpu.cpp", 364);
    nxrt::diag_vk_note(-4, "vkWaitForFences (frame slot)", "vk_renderer.cpp", 802);

    const std::string text = nxrt::diag_vk_failure_text(true);
    CHECK_MSG(has(text, "GPU first failure: VK_ERROR_OUT_OF_DEVICE_MEMORY (-2) from vkAllocateMemory"), "first error missing");
    CHECK_MSG(has(text, "GPU first device lost: VK_ERROR_DEVICE_LOST (-4) from vkQueueSubmit2 (nvn batch)"), "first device lost masked by OOM");
    CHECK_MSG(!has(text, "vkWaitForFences (frame slot)"), "follow-on device lost overwrote first device lost");
    CHECK_MSG(has(text, "last submitted serial 42, timeline value 40"), "submit state missing");

    // When the very first error IS VK_ERROR_DEVICE_LOST, it prints once (no duplicate second line).
    nxrt::diag_gpu_reset_for_test();
    nxrt::diag_vk_note(-4, "vkQueueSubmit2 (nvn batch)", "nvn_gpu.cpp", 364);
    const std::string lost_only = nxrt::diag_vk_failure_text(true);
    CHECK_MSG(has(lost_only, "GPU first failure: VK_ERROR_DEVICE_LOST (-4) from vkQueueSubmit2 (nvn batch)"), "device lost missing");
    CHECK_MSG(!has(lost_only, "GPU first device lost:"), "duplicate device lost line printed");
}

NXTEST(dispatch_ring_wraparound_and_formatting) {
    nxrt::diag_gpu_reset_for_test();
    _putenv_s("NXRT_DIAG_DISPATCH_RING", "");

    // Missing compute shader entry.
    {
        nxrt::DispatchNote m;
        std::strcpy(m.missing, "deadbeefcafebabe");
        m.serial = 99;
        m.x = 4;
        m.y = 1;
        m.z = 1;
        m.barriers = 2;
        m.flags = nxrt::DispatchNote::kMissingCs;
        nxrt::diag_note_dispatch(m);
    }
    const std::string miss_text = nxrt::diag_dispatch_text();
    CHECK_MSG(has(miss_text, "cs - (missing deadbeefcafebabe)  4x1x1  direct  barriers 2"), "missing CS dispatch not formatted");

    // Overflow past 64 slots: ring keeps the newest 64 in oldest-first order.
    nxrt::diag_gpu_reset_for_test();
    feed_dispatches(70);
    const std::string ring = nxrt::diag_dispatch_text();
    CHECK_MSG(has(ring, "recent dispatches (last 64 of 70, oldest first"), "ring header wrong");
    CHECK_MSG(!has(ring, "  #6  "), "evicted dispatch #6 still present");
    CHECK_MSG(has(ring, "  #7  submit 107"), "oldest retained dispatch #7 missing");
    CHECK_MSG(has(ring, "  #70  submit 170  cs 1000000000000046  indirect args 0x78001f2a9000  barriers 0  bindings 7 (shadow copy-in barrier)"),
              "newest indirect dispatch formatting wrong");

    // Depth clamp via config env.
    _putenv_s("NXRT_DIAG_DISPATCH_RING", "4");
    const std::string small = nxrt::diag_dispatch_text();
    CHECK_MSG(has(small, "recent dispatches (last 4 of 70"), "depth override ignored");
    CHECK_MSG(!has(small, "  #66  "), "entry older than depth=4 included");
    CHECK_MSG(has(small, "  #67  "), "entry #67 missing at depth=4");

    _putenv_s("NXRT_DIAG_DISPATCH_RING", "0");
    CHECK_EQ(nxrt::diag_dispatch_text(), std::string(""));
    _putenv_s("NXRT_DIAG_DISPATCH_RING", "");
}

NXTEST(device_lost_dump_has_full_sections) {
    nxrt::diag_gpu_reset_for_test();
    nxrt::diag_set_submit_state(&fake_submit_state);
    nxrt::diag_vk_note(-4, "vkQueueSubmit2 (nvn batch)", "nvn_gpu.cpp", 364);
    feed_dispatches(3);

    const std::string dump = nxrt::diag_device_lost_dump("vkWaitForFences (frame slot)", -4, 0,
                                                         "  GPU: Test GPU\n  window: windowed\n");
    CHECK_MSG(has(dump, "GPU DEVICE LOST (reason: VK_ERROR_DEVICE_LOST)"), "device lost header missing");
    CHECK_MSG(has(dump, "GPU: Test GPU"), "context missing");
    CHECK_MSG(has(dump, "GPU first failure: VK_ERROR_DEVICE_LOST (-4) from vkQueueSubmit2 (nvn batch)"), "first failure missing");
    CHECK_MSG(has(dump, "memory at device loss:"), "memory section missing");
    CHECK_MSG(has(dump, "system RAM "), "system RAM line missing");
    CHECK_MSG(has(dump, "last files opened (oldest first):"), "recent files section missing");
    CHECK_MSG(has(dump, "recent dispatches (last 3 of 3"), "dispatch ring section missing");

    nxrt::diag_gpu_reset_for_test();
}

namespace {
std::string g_crash_report;
__declspec(noinline) void capture_crash(EXCEPTION_POINTERS* ep) { g_crash_report = nxrt::crash_build_report(ep); }
__declspec(noinline) void raise_for_gpu_diag() {
    __try {
        RaiseException(0xE0000042u, 0, 0, nullptr);
    } __except (capture_crash(GetExceptionInformation()), EXCEPTION_EXECUTE_HANDLER) {
    }
}
}  // namespace

NXTEST(crash_report_includes_memory_and_gpu_failure_only_when_set) {
    nxrt::diag_gpu_reset_for_test();
    g_crash_report.clear();
    raise_for_gpu_diag();
    CHECK_MSG(has(g_crash_report, "memory at the crash:"), "crash report missing memory section");
    CHECK_MSG(!has(g_crash_report, "GPU first failure:"), "crash report should omit GPU failure when none occurred");

    nxrt::diag_vk_note(-4, "vkQueueSubmit2 (nvn batch)", "nvn_gpu.cpp", 373);
    g_crash_report.clear();
    raise_for_gpu_diag();
    CHECK_MSG(has(g_crash_report, "GPU first failure: VK_ERROR_DEVICE_LOST (-4) from vkQueueSubmit2 (nvn batch)"),
              "crash report missing GPU failure");
    nxrt::diag_gpu_reset_for_test();
}
