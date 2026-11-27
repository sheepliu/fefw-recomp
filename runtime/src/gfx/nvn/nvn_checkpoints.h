#pragma once
// [gfx] gpu_checkpoints: a marker before every draw and dispatch, named in the device-lost report (nvn_checkpoints.cpp). Render thread.
#include <volk.h>

namespace nxrt::nvn::be::gpu {

// No-ops unless [gfx] gpu_checkpoints is on and the driver has VK_NV_device_diagnostic_checkpoints or VK_AMD_buffer_marker. Shader hashes
// may be null.
void ckpt_draw(VkCommandBuffer cb, const char* vs, const char* fs, const char* gs);
void ckpt_dispatch(VkCommandBuffer cb, const char* cs);
// The draw call the last ckpt_draw marked: its name and counts (draw: vertices, instances, first vertex, first instance; indexed: indices,
// instances, first index, vertex offset, first instance; indirect: draw count, stride, max count, index type; xfb: stride, slot) and, for an
// indirect draw, the argument buffer's address and up to five words of the CPU's copy of it. A no-op when checkpoints are off.
void ckpt_args(const char* what, uint32_t v0, uint32_t v1, uint32_t v2, uint32_t v3, uint32_t v4, uint64_t addr = 0, const void* host = nullptr,
               uint32_t nhost = 0);

}  // namespace nxrt::nvn::be::gpu
