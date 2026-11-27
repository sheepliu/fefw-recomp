// SPDX-License-Identifier: GPL-3.0-or-later
#include "test_framework.h"

#include <cstdio>
#include <cstring>

#include "video_alloc_stats.h"

NXTEST_GROUP(gfx_video_alloc_stats)

using nxrt::gfx::VideoAllocStats;

NXTEST(no_figures_no_text) {
    VideoAllocStats s;
    char t[512] = "x";
    CHECK(nxrt::gfx::video_alloc_text(s, t, sizeof t) == 0);
    CHECK(t[0] == 0);
}

NXTEST(free_inside_blocks_and_outside) {
    VideoAllocStats s;
    s.valid = true;
    s.video = {6144ull << 20, 4096ull << 20, 90, 7000};
    s.other = {512ull << 20, 384ull << 20, 6, 40};
    s.small = {320ull << 20, 256ull << 20, 20, 5000};
    s.video_free_ranges = 1200;
    s.video_largest_free = 48ull << 20;
    s.driver_bytes = 9216ull << 20;
    CHECK(s.video.free_bytes() == (2048ull << 20));
    CHECK(nxrt::gfx::video_alloc_free_percent(s.video) == 33);
    CHECK(s.outside_bytes() == ((9216ull - 6144 - 512) << 20));
    char t[512];
    CHECK(nxrt::gfx::video_alloc_text(s, t, sizeof t) > 0);
    std::printf("    %s\n", t);
    CHECK(std::strstr(t, "video heap 90 blocks / 6144 MiB taken"));
    CHECK(std::strstr(t, "7000 allocations / 4096 MiB handed out"));
    CHECK(std::strstr(t, "2048 MiB free inside the blocks (33%, 1200 ranges, largest 48 MiB)"));
    CHECK(std::strstr(t, "small-texture pools 20 blocks / 320 MiB, 64 MiB free"));
    CHECK(std::strstr(t, "other heaps 512 MiB taken, 128 MiB free"));
    CHECK(std::strstr(t, "outside the allocator 2560 MiB"));
}

// A gauge read between two allocations can be behind the allocator: never negative.
NXTEST(driver_figure_behind) {
    VideoAllocStats s;
    s.valid = true;
    s.video = {100ull << 20, 120ull << 20, 2, 3};
    s.driver_bytes = 50ull << 20;
    CHECK(s.video.free_bytes() == 0);
    CHECK(s.outside_bytes() == 0);
    CHECK(nxrt::gfx::video_alloc_free_percent(nxrt::gfx::VideoAllocGroup{}) == 0);
}

NXTEST(vma_block_sizing_for_small_and_large_heaps) {
    constexpr uint64_t kMiB = 1ull << 20;
    constexpr uint64_t kGiB = 1ull << 30;
    // Unknown heap: keep VMA default
    CHECK_EQ(nxrt::gfx::vma_preferred_block_size(0), 0ull);
    // 4 GB discrete card (e.g. GTX 1650, 3.84 GiB heap): 32 MiB blocks
    CHECK_EQ(nxrt::gfx::vma_preferred_block_size(3935 * kMiB), 32 * kMiB);
    // 6 GB card / Steam Deck (6.0 GiB heap): 64 MiB blocks
    CHECK_EQ(nxrt::gfx::vma_preferred_block_size(6 * kGiB), 64 * kMiB);
    // 8+ GB card: VMA default (256 MiB)
    CHECK_EQ(nxrt::gfx::vma_preferred_block_size(8 * kGiB), 0ull);
    // The boundaries belong to the smaller size
    CHECK_EQ(nxrt::gfx::vma_preferred_block_size(4608 * kMiB), 32 * kMiB);
    CHECK_EQ(nxrt::gfx::vma_preferred_block_size(4608 * kMiB + 1), 64 * kMiB);
    CHECK_EQ(nxrt::gfx::vma_preferred_block_size(7168 * kMiB), 64 * kMiB);
    CHECK_EQ(nxrt::gfx::vma_preferred_block_size(7168 * kMiB + 1), 0ull);
}
