// SPDX-License-Identifier: GPL-3.0-or-later
// [gfx] gpu_checkpoints (default off): NVIDIA's diagnostic checkpoints (VK_NV_device_diagnostic_checkpoints) or AMD's buffer markers
// (VK_AMD_buffer_marker) for device losses.
//
// With this on, the replay puts a marker before every draw and dispatch (through the deferred-recording queue, so the order is kept).
// Each marker is a number into a ring of records here that holds the draw's or dispatch's shader hashes and call arguments. After a loss
// the report names the work the GPU had started (top of pipe) but not finished (bottom of pipe), and the records between.
// A marker per draw costs some GPU and render-thread time, so this is for diagnostic runs, not for play.
#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include <volk.h>

#include "nxrt/config.h"
#include "nxrt/gfx.h"
#include "nxrt/log.h"
#include "nvn_gpu_internal.h"
#include "nvn_checkpoints.h"

namespace nxrt::nvn::be::gpu {
namespace {

constexpr const char* kTag = "nvn.ckpt";
constexpr uint64_t kRing = 1u << 16;   // records kept: far more than the draws of the frames in flight

struct Rec {
    std::atomic<uint64_t> seq{0};      // written last: a reader trusts the record only when this is the marker it looks for
    uint64_t present = 0;              // presents at the time it was recorded
    char kind = 0;                     // 'd' draw, 'c' dispatch
    char a[20] = {}, b[20] = {}, c[20] = {};   // vertex, fragment, geometry (draws) or compute (dispatches) shader hashes
    char what[16] = {};                // ckpt_args: the Vulkan draw call ("draw", "indexed", "indirect", ...)
    uint32_t v[5] = {};                // its counts (see ckpt_args)
    uint64_t addr = 0;                 // indirect: the argument buffer's address
    uint32_t host[5] = {};             // indirect: the arguments as the CPU's copy of that memory held them when recorded
    uint8_t nhost = 0;
};

Rec* g_ring = nullptr;
uint64_t g_seq = 0;                    // render thread
int g_state = -1;                      // -1 not decided, 0 off, 1 on
// VK_AMD_buffer_marker: [0] the last marker the GPU reached (top of pipe), [1] the last one all earlier work had finished by (bottom).
bool g_amd = false;
VkBuffer g_mbuf = VK_NULL_HANDLE;
VkDeviceMemory g_mmem = VK_NULL_HANDLE;
volatile uint32_t* g_mmap = nullptr;

void copy_hash(char (&dst)[20], const char* src) {
    if (!src) { dst[0] = '-'; dst[1] = 0; return; }
    std::strncpy(dst, src, sizeof dst - 1);
    dst[sizeof dst - 1] = 0;
}

const char* stage_name(VkPipelineStageFlagBits s) {
    switch (s) {
        case VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT: return "top of pipe (started)";
        case VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT: return "bottom of pipe (work before marker finished)";
        default: return "stage";
    }
}

std::string describe(uint64_t seq) {
    char b[240];
    const Rec& r = g_ring[seq % kRing];
    if (!seq || r.seq.load(std::memory_order_acquire) != seq) {
        std::snprintf(b, sizeof b, "marker %llu (no longer recorded)", static_cast<unsigned long long>(seq));
        return b;
    }
    if (r.kind == 'c')
        std::snprintf(b, sizeof b, "marker %llu: dispatch, cs %s (present %llu)", static_cast<unsigned long long>(seq), r.a,
                      static_cast<unsigned long long>(r.present));
    else
        std::snprintf(b, sizeof b, "marker %llu: draw, vs %s fs %s%s%s (present %llu)", static_cast<unsigned long long>(seq), r.a, r.b,
                      r.c[0] && r.c[0] != '-' ? " gs " : "", r.c[0] && r.c[0] != '-' ? r.c : "", static_cast<unsigned long long>(r.present));
    std::string out = b;
    if (r.what[0]) {
        char c[240];
        std::snprintf(c, sizeof c, "; %s %u %u %u %u %u", r.what, r.v[0], r.v[1], r.v[2], r.v[3], r.v[4]);
        out += c;
        if (r.addr) {
            std::snprintf(c, sizeof c, " at 0x%llx", static_cast<unsigned long long>(r.addr));
            out += c;
            if (r.nhost) {
                out += ", CPU copy:";
                for (uint8_t i = 0; i < r.nhost; ++i) { std::snprintf(c, sizeof c, " %u", r.host[i]); out += c; }
            }
        }
    }
    return out;
}

// AMD: the two numbers in the marker buffer. Markers are 32-bit there; the ring's sequence numbers are rebuilt from the latest one recorded.
std::string report_amd() {
    std::string out = "  GPU markers ([gfx] gpu_checkpoints, VK_AMD_buffer_marker):\n";
    const uint32_t top = g_mmap[0], bottom = g_mmap[1];
    auto full = [](uint32_t m) -> uint64_t {   // the newest sequence number whose low 32 bits are m
        uint64_t s = (g_seq & ~0xffffffffull) | m;
        if (s > g_seq && s >= (1ull << 32)) s -= 1ull << 32;
        return s;
    };
    const uint64_t started = top ? full(top) : 0, finished = bottom ? full(bottom) : 0;
    char b[200];
    std::snprintf(b, sizeof b, "    markers recorded %llu; last reached (top of pipe) %llu; all work before marker %llu finished (bottom of pipe)\n",
                  static_cast<unsigned long long>(g_seq), static_cast<unsigned long long>(started), static_cast<unsigned long long>(finished));
    out += b;
    if (!started) return out + "    the GPU reached no marker: it failed before the first draw or dispatch\n";
    out += "    last reached: " + describe(started) + "\n";
    // the work from the bottom marker up to the top one had been reached by the GPU but not finished when it failed
    const uint64_t first = std::max(finished ? finished : 1, started > 24 ? started - 24 : 1);
    if (started >= first) {
        out += "    in flight (reached, not known finished), oldest first:\n";
        for (uint64_t s = first; s <= started; ++s) out += "      " + describe(s) + "\n";
    }
    return out;
}

// The device-lost report's section: called once, on the thread that saw the loss.
std::string report() {
    if (g_amd && g_mmap) return report_amd();
    if (!g_ring || !vkGetQueueCheckpointDataNV) return "";
    const VkQueue q = device().queue();
    uint32_t n = 0;
    vkGetQueueCheckpointDataNV(q, &n, nullptr);
    std::string out = "  GPU checkpoints ([gfx] gpu_checkpoints):\n";
    if (!n) return out + "    the driver reports none\n";
    std::vector<VkCheckpointDataNV> d(n, VkCheckpointDataNV{VK_STRUCTURE_TYPE_CHECKPOINT_DATA_NV});
    vkGetQueueCheckpointDataNV(q, &n, d.data());
    uint64_t started = 0, finished = 0;
    for (uint32_t i = 0; i < n; ++i) {
        const uint64_t seq = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(d[i].pCheckpointMarker));
        out += "    ";
        out += stage_name(d[i].stage);
        out += ": ";
        out += describe(seq);
        out += "\n";
        if (d[i].stage == VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT) started = std::max(started, seq);
        if (d[i].stage == VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT) finished = std::max(finished, seq);
    }
    // the work from the bottom marker up to the top one had been reached by the GPU and is not known finished (marker N is recorded before op N)
    if (started > 0 && started >= finished) {
        const uint64_t first = std::max(finished ? finished : 1, started > 24 ? started - 24 : 1);
        out += "    in flight (reached, not known finished), oldest first:\n";
        for (uint64_t s = first; s <= started; ++s) out += "      " + describe(s) + "\n";
    }
    return out;
}

// The marker buffer: 16 bytes of host-visible, coherent memory, so the CPU can still read it after the device is lost.
bool amd_buffer() {
    const VkDevice dev = device().device();
    VkBufferCreateInfo bi = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bi.size = 16;
    bi.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    bi.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    if (vkCreateBuffer(dev, &bi, nullptr, &g_mbuf) != VK_SUCCESS) return false;
    VkMemoryRequirements mr;
    vkGetBufferMemoryRequirements(dev, g_mbuf, &mr);
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(device().physical(), &mp);
    const VkMemoryPropertyFlags want = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    uint32_t ty = UINT32_MAX;
    for (uint32_t i = 0; i < mp.memoryTypeCount && ty == UINT32_MAX; ++i)   // prefer memory that is not device-local (survives a reset best)
        if ((mr.memoryTypeBits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want &&
            !(mp.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT))
            ty = i;
    for (uint32_t i = 0; i < mp.memoryTypeCount && ty == UINT32_MAX; ++i)
        if ((mr.memoryTypeBits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want) ty = i;
    VkMemoryAllocateInfo ai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = mr.size;
    ai.memoryTypeIndex = ty;
    void* p = nullptr;
    if (ty == UINT32_MAX || vkAllocateMemory(dev, &ai, nullptr, &g_mmem) != VK_SUCCESS || vkBindBufferMemory(dev, g_mbuf, g_mmem, 0) != VK_SUCCESS ||
        vkMapMemory(dev, g_mmem, 0, VK_WHOLE_SIZE, 0, &p) != VK_SUCCESS) {
        NXLOG_W(kTag, "GPU markers: could not make the marker buffer: off");
        return false;
    }
    g_mmap = static_cast<volatile uint32_t*>(p);
    g_mmap[0] = 0;   // two stores: a chained assignment reads the volatile back (deprecated in C++20)
    g_mmap[1] = 0;
    return true;
}

bool on() {
    if (g_state < 0) {
        g_state = 0;
        if (nxrt::config_get_bool("gfx", "gpu_checkpoints", false)) {
            if (device().features().gpu_checkpoints && vkCmdSetCheckpointNV && vkGetQueueCheckpointDataNV) {
                g_ring = new Rec[kRing];
                g_state = 1;
                gfx::set_device_lost_extra(&report);
                NXLOG_I(kTag, "GPU checkpoints on: a marker before every draw and dispatch, named in the device-lost report ([gfx] gpu_checkpoints)");
            } else if (device().features().buffer_marker_amd && vkCmdWriteBufferMarkerAMD && amd_buffer()) {
                g_ring = new Rec[kRing];
                g_amd = true;
                g_state = 1;
                gfx::set_device_lost_extra(&report);
                NXLOG_I(kTag, "GPU markers on (VK_AMD_buffer_marker): the GPU writes a number before every draw and dispatch, named in the device-lost report ([gfx] gpu_checkpoints)");
            } else {
                NXLOG_W(kTag, "[gfx] gpu_checkpoints is on, but this driver offers neither VK_NV_device_diagnostic_checkpoints nor VK_AMD_buffer_marker: off");
            }
        }
    }
    return g_state == 1;
}

void mark(VkCommandBuffer cb, char kind, const char* a, const char* b, const char* c) {
    const uint64_t seq = ++g_seq;
    Rec& r = g_ring[seq % kRing];
    r.seq.store(0, std::memory_order_relaxed);
    r.kind = kind;
    r.present = counters().presents.load(std::memory_order_relaxed);
    copy_hash(r.a, a);
    copy_hash(r.b, b);
    copy_hash(r.c, c);
    r.what[0] = 0;
    r.addr = 0;
    r.nhost = 0;
    r.seq.store(seq, std::memory_order_release);
    if (g_amd) {
        const uint32_t m = static_cast<uint32_t>(seq) ? static_cast<uint32_t>(seq) : 1;
        vkCmdWriteBufferMarkerAMD(cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, g_mbuf, 0, m);
        vkCmdWriteBufferMarkerAMD(cb, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, g_mbuf, 4, m);
        return;
    }
    vkCmdSetCheckpointNV(cb, reinterpret_cast<const void*>(static_cast<uintptr_t>(seq)));
}

}  // namespace

void ckpt_draw(VkCommandBuffer cb, const char* vs, const char* fs, const char* gs) {
    if (on()) mark(cb, 'd', vs, fs, gs);
}

void ckpt_dispatch(VkCommandBuffer cb, const char* cs) {
    if (on()) mark(cb, 'c', cs, nullptr, nullptr);
}

void ckpt_args(const char* what, uint32_t v0, uint32_t v1, uint32_t v2, uint32_t v3, uint32_t v4, uint64_t addr, const void* host, uint32_t nhost) {
    if (g_state != 1 || !g_seq) return;
    Rec& r = g_ring[g_seq % kRing];
    const uint64_t seq = r.seq.load(std::memory_order_relaxed);
    if (seq != g_seq) return;
    r.seq.store(0, std::memory_order_relaxed);
    std::strncpy(r.what, what, sizeof r.what - 1);
    r.what[sizeof r.what - 1] = 0;
    const uint32_t vv[5] = {v0, v1, v2, v3, v4};
    std::memcpy(r.v, vv, sizeof vv);
    r.addr = addr;
    r.nhost = 0;
    if (host && nhost) {
        r.nhost = static_cast<uint8_t>(std::min<uint32_t>(nhost, 5));
        std::memcpy(r.host, host, r.nhost * sizeof(uint32_t));
    }
    r.seq.store(seq, std::memory_order_release);
}

}  // namespace nxrt::nvn::be::gpu
