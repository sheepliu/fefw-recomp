// SPDX-License-Identifier: GPL-3.0-or-later
// Tests for the Switch 2 AV1 movie decoder (media/movie_vdec.cpp): the dav1d thread hook (third_party/dav1d/src/win32/thread.c),
// frame-level parallelism settings, and the SSE2 I420 -> NV12 converter (#40).
#include <windows.h>

#include <atomic>
#include <cstdint>
#include <mutex>
#include <set>
#include <vector>

#include "nxrt/movie_vdec.h"
#include "test_framework.h"

NXTEST_GROUP(media_vdec)   // outside the #if: CMake registers the group from this line whether or not dav1d is built

namespace {
void ref_i420_to_nv12(uint8_t* o, uint32_t w, uint32_t h, const uint8_t* py, ptrdiff_t sy, const uint8_t* pu, const uint8_t* pv, ptrdiff_t suv) {
    const uint32_t cw = (w + 1) / 2, ch = (h + 1) / 2;
    for (uint32_t r = 0; r < h; ++r)
        for (uint32_t x = 0; x < w; ++x) o[static_cast<size_t>(r) * w + x] = py[static_cast<ptrdiff_t>(r) * sy + x];
    uint8_t* q = o + static_cast<size_t>(w) * h;
    for (uint32_t r = 0; r < ch; ++r) {
        const uint8_t* u = pu + static_cast<ptrdiff_t>(r) * suv;
        const uint8_t* v = pv + static_cast<ptrdiff_t>(r) * suv;
        for (uint32_t x = 0; x < cw; ++x) {
            *q++ = u[x];
            *q++ = v[x];
        }
    }
}
}  // namespace

NXTEST(i420_to_nv12_matches_scalar_reference) {
    struct Case {
        uint32_t w, h;
        ptrdiff_t sy, suv;
    };
    const Case cases[] = {
        {16, 8, 16, 8},          // compact, cw < 16 (tail only)
        {32, 16, 32, 16},        // compact, cw == 16 (exact SIMD vector)
        {34, 10, 64, 32},        // strided, cw == 17 (SIMD + 1-byte tail)
        {62, 14, 128, 64},       // strided, cw == 31 (SIMD + 15-byte tail)
        {1920, 64, 1920, 960},   // 1080p-width compact
        {2560, 32, 2624, 1344},  // 1440p-width strided (dav1d padded stride)
    };
    for (const Case& tc : cases) {
        const uint32_t cw = (tc.w + 1) / 2, ch = (tc.h + 1) / 2;
        std::vector<uint8_t> y(static_cast<size_t>(tc.sy) * tc.h);
        std::vector<uint8_t> u(static_cast<size_t>(tc.suv) * ch);
        std::vector<uint8_t> v(static_cast<size_t>(tc.suv) * ch);
        for (size_t i = 0; i < y.size(); ++i) y[i] = static_cast<uint8_t>((i * 37 + 11) & 0xff);
        for (size_t i = 0; i < u.size(); ++i) u[i] = static_cast<uint8_t>((i * 53 + 7) & 0xff);
        for (size_t i = 0; i < v.size(); ++i) v[i] = static_cast<uint8_t>((i * 97 + 3) & 0xff);

        const size_t nv12_bytes = static_cast<size_t>(tc.w) * tc.h + static_cast<size_t>(cw) * 2 * ch;
        std::vector<uint8_t> got(nv12_bytes, 0xaa), want(nv12_bytes, 0x55);
        nxrt::media::i420_to_nv12(got.data(), tc.w, tc.h, y.data(), tc.sy, u.data(), v.data(), tc.suv);
        ref_i420_to_nv12(want.data(), tc.w, tc.h, y.data(), tc.sy, u.data(), v.data(), tc.suv);
        CHECK(got == want);
    }
}

#if __has_include(<dav1d/dav1d.h>)
#include <dav1d/dav1d.h>
extern "C" void (*dav1d_nxrt_thread_hook)(int begin);

namespace {
std::atomic<int> g_begins{0}, g_ends{0}, g_prio_ok{0};
std::mutex g_mu;
std::set<DWORD> g_tids;

void hook(int begin) {
    if (begin) {
        SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);
        if (GetThreadPriority(GetCurrentThread()) == THREAD_PRIORITY_ABOVE_NORMAL) g_prio_ok++;
        std::lock_guard<std::mutex> lk(g_mu);
        g_tids.insert(GetCurrentThreadId());
        g_begins++;
    } else {
        g_ends++;
    }
}
}  // namespace

NXTEST(dav1d_thread_hook_runs_on_each_task_thread) {
    g_begins = g_ends = g_prio_ok = 0;
    g_tids.clear();
    dav1d_nxrt_thread_hook = hook;
    Dav1dSettings s;
    dav1d_default_settings(&s);
    s.n_threads = 4;
    s.max_frame_delay = 0;
    CHECK(dav1d_get_frame_delay(&s) >= 2);
    Dav1dContext* c = nullptr;
    REQUIRE(dav1d_open(&c, &s) == 0);
    // dav1d_open creates its task threads (n_threads of them) but does not wait for them to run: wait here, 5 s at most
    for (int i = 0; i < 500 && g_begins.load() < 4; ++i) Sleep(10);
    const int begun = g_begins.load();
    CHECK_EQ(begun, 4);
    CHECK_EQ(g_prio_ok.load(), begun);
    {
        std::lock_guard<std::mutex> lk(g_mu);
        CHECK_EQ(static_cast<int>(g_tids.size()), begun);   // a thread of its own each time
        CHECK(g_tids.count(GetCurrentThreadId()) == 0);     // never the caller's thread
    }
    dav1d_close(&c);
    CHECK_EQ(g_ends.load(), begun);                      // dav1d_close joins them: every end ran
    dav1d_nxrt_thread_hook = nullptr;
}

NXTEST(dav1d_without_hook_still_opens) {
    dav1d_nxrt_thread_hook = nullptr;
    Dav1dSettings s;
    dav1d_default_settings(&s);
    s.n_threads = 2;
    Dav1dContext* c = nullptr;
    REQUIRE(dav1d_open(&c, &s) == 0);
    dav1d_close(&c);
    CHECK(c == nullptr);
}
#else   // built with -DNXRT_DAV1D=OFF: one skipped case, so `nxrt_tests media_vdec` still finds a test
NXTEST(dav1d_thread_hook_runs_on_each_task_thread) { nxtest::skip("built without dav1d (NXRT_DAV1D=OFF)"); }
#endif
