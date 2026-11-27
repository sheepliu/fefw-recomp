// SPDX-License-Identifier: GPL-3.0-or-later
// [gfx] astc_gpu: ASTC textures the GPU cannot sample are decoded and encoded on the GPU instead of by the CPU transcoder (#122, #128, #133:
// a new area kept six to eight cores busy for minutes, its textures drawn as stand-ins meanwhile). Implementation: nvn_astc_gpu.cpp; shaders:
// shaders/astc_decode.comp, bc7_encode.comp, bc1_encode.comp (compiled into nvn_astc_gpu_shaders.inc by scripts/gen_astc_gpu_shaders.py).
//
// The path of a texture without a cache entry:
//   * prepare_texture (TexCaps::gpu_astc) hands out the untiled ASTC blocks instead of transcoding them (PreparedTexture::gpu_astc); a
//     background job (nvn_texprep.cpp) does that as it reads the cache, so nothing waits on the render thread.
//   * tex_ensure_current gives them to astc_gpu_upload. Recorded into the frame's command buffer: the blocks go from staging into a
//     device-local scratch buffer, a compute pass decodes them to RGBA8 behind them, a second one encodes those texels to the image's
//     format (BC7; BC1 for a texture [gfx] astc_bc stores so; none for an RGBA8 image), and a buffer-to-image copy fills the levels. The
//     scratch buffer is reused by every texture (chunks of at most 32 MiB of texels) and released after ten seconds without use.
//   * Budget: a texture the GPU is done with sooner than the render thread would be with its stand-in (1.5 ms at the measured rate) is always
//     transcoded in the frame that first uses it. A larger one goes at once only while the present's budget lasts ([gfx] astc_gpu_ms at 30
//     presents a second, a quarter of the present interval; a long frame raises it for the frames after it). Over it the texture is queued:
//     it shows the CPU path's quick stand-in, or what it showed before, and astc_gpu_pump (start of each submission, once per present)
//     records it over the following presents, a large level in bands of about 2 ms.
//   * Cache ([gfx] astc_gpu_cache; auto: only on a GPU slower than 2.5 ms per M texel, where a transcode costs more than reading an entry):
//     the encoded blocks are also copied to a host buffer; when the GPU has run the frame a worker thread writes them to [nvn] bc7_cache
//     under the GPU encoders' own key, so later runs read them like any entry.
// Fallback to the CPU transcoder: [gfx] astc_gpu = off; a device whose pipelines fail to build or whose self-test (the decode compared with
// the CPU decoder on random blocks, both encoders' blocks decoded again and held to an error bound) fails; and, per texture, sparse and 3D
// textures, host pixel edits and BC5 images. A failed scratch or staging allocation falls back for that texture.
// Tests: tests/gfx/test_nvn_astc_gpu.cpp; [nvn] astc_gpu_probe, astc_gpu_fail_test and astc_gpu_slow (docs/CONFIG.md).
#pragma once
#include <cstdint>
#include <vector>

#include <volk.h>

#include "nxrt/gfx.h"
#include "nvn_core.h"

namespace nxrt::nvn::be {
struct PreparedTexture;
namespace gpu {
struct Frame;

// The rates (texels per ms of GPU time) of the last timed transcode passes: what the cost estimates and the hand-back to the CPU go by.
// One pass says little either way. The first passes of a session, in a load's frames, time ten times slower than a fast GPU is; and a
// slow GPU has the odd fast pass (e.g. a GTX 970 averaged 36 to 45 ms per M texel over a session with at least one pass under 2.5, so
// "the best pass" kept it on the GPU path until a heavy load cost it the device). So:
//   * estimates use the window's median when it is slower than the smoothed rate (one fast pass moves the smoothed rate by a fifth of
//     itself and made hundreds of textures look free);
//   * the GPU is slow when even the fastest quarter of the window is slower than the limit, and it is handed off when that has held at
//     every pass for hold_ns and for a whole new window of passes: a second burst at least that much later, or one burst that long.
//     (Time alone is not enough: after a slow load a fast GPU may get a pass a second, and eight of those turn the verdict.)
// No Vulkan in it (tests/gfx/test_nvn_astc_gpu.cpp).
struct AstcRateWindow {
    static constexpr uint32_t kSize = 32;
    double r[kSize] = {};
    uint32_t n = 0, at = 0;
    uint64_t slow_since_ns = 0;
    uint32_t slow_passes = 0;          // passes added since then

    void add(double rate) {
        r[at] = rate;
        at = (at + 1) % kSize;
        if (n < kSize) ++n;
    }
    bool full() const { return n == kSize; }
    // the rate of the pass that `rank` passes of the window were faster than (0: the fastest); 0 for an empty window
    double ranked(uint32_t rank) const {
        if (!n) return 0.0;
        double v[kSize];
        for (uint32_t i = 0; i < n; ++i) v[i] = r[i];
        for (uint32_t i = 0; i < n; ++i)              // 32 values: a plain insertion sort, fastest first
            for (uint32_t j = i; j > 0 && v[j] > v[j - 1]; --j) {
                const double x = v[j];
                v[j] = v[j - 1];
                v[j - 1] = x;
            }
        return v[rank < n ? rank : n - 1];
    }
    double median() const { return ranked(n / 2); }
    // ms per M texel of the slowest pass of the fastest quarter; 0 until the window is full
    double quarter_ms() const {
        const double q = full() ? ranked(kSize / 4 - 1) : 0.0;
        return q > 0.0 ? 1e6 / q : 0.0;
    }
    // After add(): true when the fastest quarter has been slower than limit_ms per M texel at every pass for hold_ns and kSize passes.
    bool slow_for(double limit_ms, uint64_t now_ns, uint64_t hold_ns) {
        if (!full() || !(quarter_ms() > limit_ms)) {
            slow_since_ns = 0;
            slow_passes = 0;
            return false;
        }
        if (!slow_since_ns) {
            slow_since_ns = now_ns ? now_ns : 1;
            slow_passes = 0;
            return false;
        }
        ++slow_passes;
        return now_ns - slow_since_ns >= hold_ns && slow_passes >= kSize;
    }
};

// Any thread. True when the GPU path is on for this device. The first caller with wait builds the pipelines and runs the self-test (0.1 s
// with the driver's shader cache, 1.4 to 2.3 s at a first start; background jobs call it so); without wait the answer is false until that
// has happened (the render thread).
bool astc_gpu_enabled(bool wait);
// The back end is up on a device: the pipelines and the self-test start on a thread of their own, so the first texture does not wait for them.
void astc_gpu_start();
bool astc_gpu_ready();             // any thread: astc_gpu_enabled(true) answers without waiting
// [gfx] astc_gpu_upgrade: textures the GPU encoded are also re-encoded by the background CPU pool of [gfx] astc_quality = high.
bool astc_gpu_upgrade();
// Any thread. [gfx] astc_gpu_cache_read: true when BC7 and BC1 entries of [nvn] bc7_cache are not read and the GPU transcodes such textures again
// (TexCaps::skip_entries). auto: while the GPU is fast (as astc_gpu_cache = auto; also before its rate is measured), unless astc_gpu_cache = on
// or astc_gpu_upgrade asks for entries. On an RTX 4070 Super the hub's load frame took 2.4 to 3.0 s transcoding and 4.0 to 13.3 s reading entries.
bool astc_gpu_skip_entries();

enum class AstcGpu { Done, Deferred, Failed };
// Render thread, no pass open on f.cb. prep: a gpu_astc result for t whose format is the image's. staged: the upload buffer a background
// job copied prep's blocks into, or null (prep.data holds them); taken over (set to null) unless the call fails.
//   Done      the image's levels are filled by commands recorded into f.
//   Deferred  over this present's budget: queued (prep and staged are taken). The caller keeps what the image shows, or makes a stand-in
//             for an image that shows nothing yet; astc_gpu_pump fills it in a later present.
//   Failed    nothing recorded; the caller prepares the texture on the CPU.
AstcGpu astc_gpu_upload(Texture* t, Frame& f, PreparedTexture& prep, gfx::Buffer& staged);
// Render thread: something for astc_gpu_pump to do (queued textures, or a scratch buffer to release); one atomic load.
bool astc_gpu_work();
// Render thread, no pass open: once per present, records queued transcodes within the budget.
void astc_gpu_pump(Frame& f);
void astc_gpu_shutdown();          // device idle: frees the pipelines and buffers; astc_gpu_start builds them again

// nvn_gpu_texture.cpp: copies buffer regions into the image of t (as an upload does) and leaves it sampled. False: t has no usable image.
bool tex_copy_buffer_regions(Texture* t, Frame& f, VkBuffer buf, const std::vector<VkBufferImageCopy>& regions);
// nvn_gpu_texture.cpp: identity of t's image (null: none), to tell whether a queued transcode still belongs to it.
const void* tex_gpu_identity(Texture* t);

}  // namespace gpu
}  // namespace nxrt::nvn::be
