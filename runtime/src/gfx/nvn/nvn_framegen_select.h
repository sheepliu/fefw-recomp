// SPDX-License-Identifier: GPL-3.0-or-later
// The rule that picks the HUD-less capture target of frame generation (nvn_framegen.cpp): what makes a colour target a candidate, and which
// of the frame's candidates the capture keeps. Free of Vulkan and of the NVN objects, so the runtime and the CPU tests
// (tests/test_framegen_select.cpp) decide a scene the same way.
//
// A Vulkan format alone does not say what a target is: with the HDR scene the game's 8-bit sRGB targets are kept as RGBA16F
// (extras::hdr_promote_target) and its R11G11B10F ones are promoted to RGBA16F too ([gfx] float16_scene), so the tone map's output and the
// render targets of the post effects it was computed from all end up RGBA16F. The NVN format a target was built with is then the only format
// that tells them apart, and the evaluated picture's size is what tells an offscreen target from a smaller one of the same format. Both are
// required only where the formats are ambiguous (a promotion or a fallback), so an SDR frame, where every image holds the format its NVN
// format names, is judged exactly as before.
#pragma once
#include <cstdint>

namespace nxrt::nvn::be::gpu::fgsel {

// A texture's formats.
struct Fmt {
    int nvn = 0;         // the NVNformat it was built with
    int vk = 0;          // the Vulkan format its image holds (VkFormat)
    int native_vk = 0;   // the Vulkan format its NVN format's table names; other than vk after a promotion or a format fallback
    bool ambiguous() const { return vk != native_vk; }
};

// What the last present left for this frame's selection.
struct Scene {
    bool have_window = false;
    Fmt window;                          // the window texture last presented
    uint32_t eval_w = 0, eval_h = 0;     // the picture the upscaler evaluated, in image texels (0: not known)
    uint32_t shown_w = 0, shown_h = 0;   // the part of the window texture the last present showed (a crop, or all of it)
    uint32_t known_good = 0;             // a texture id a present has accepted as the capture (0: none yet)
    bool copied = false;                 // a window texture was drawn after this frame's capture: the picture is already in it
};

// A draw into colour target 0, after the frame's evaluation.
struct Look {
    uint32_t id = 0;
    uint32_t w = 0, h = 0;         // its level 0, in image texels
    Fmt fmt;
    bool window_texture = false;   // one of the window's textures
    bool blended = false;          // the draw blends into it
    bool written = false;          // a draw without blending wrote it after the evaluation
};

// The tiers a candidate can land in, best last. When the evaluated size and the region presented differ (a cropped present), an offscreen
// target of the region presented (kInner, which passes present-time validation) outranks one of the evaluated size only (kEvalOnly).
enum Tier { kReject = 0, kEvalOnly = 1, kInner = 2, kWindow = 3, kKnown = 4 };

struct Choice {
    bool ok = false;       // the target could be the frame's capture
    bool place = false;    // capture it now: nothing is held yet, or it wins over what is held
    int tier = kReject;
    const char* why = "";  // why it is or is not a candidate (logged)
    const char* pick = ""; // why it is or is not the capture the frame keeps (logged)
};

// `cur` is the tier of the capture the frame holds so far, `cur_id` that target's id (both kReject/0 when there is none). A capture is kept
// over an equal one unless the equal one is a later draw: the last blended draw of the frame is the one the game's copy of the picture into
// the window follows, and a target a present has already accepted is never given up.
inline Choice judge(const Scene& s, const Look& c, int cur, uint32_t cur_id) {
    Choice r;
    if (!c.blended) {
        r.why = "the draw does not blend into it";
        return r;
    }
    if (c.window_texture) {
        if (c.w < s.shown_w || c.h < s.shown_h) {
            r.why = "a window texture smaller than the region presented";
            return r;
        }
        r.tier = kWindow;
        if (c.w == s.eval_w && c.h == s.eval_h && s.eval_w)
            r.why = "the window texture itself, of the evaluated picture's size";
        else if (c.w == s.shown_w && c.h == s.shown_h)
            r.why = "the window texture itself, of the region presented's size";
        else
            r.why = "the window texture itself, larger than the evaluated picture (a crop of it is presented)";
    } else {
        if (!c.written) {
            r.why = "no draw without blending wrote it after the evaluation";
            return r;
        }
        if (!s.have_window) {
            r.why = "no window texture has been presented yet";
            return r;
        }
        if (c.fmt.vk != s.window.vk) {
            r.why = "its Vulkan format is not the window texture's";
            return r;
        }
        if ((c.fmt.ambiguous() || s.window.ambiguous()) && c.fmt.nvn != s.window.nvn) {
            r.why = "its NVN format is not the window texture's (the images hold promoted or fallback formats, so the Vulkan format alone "
                     "does not say what a target is)";
            return r;
        }
        const bool eval_size = s.eval_w && c.w == s.eval_w && c.h == s.eval_h;
        const bool shown_size = s.shown_w && c.w == s.shown_w && c.h == s.shown_h;
        if (!eval_size && !shown_size) {
            r.why = "its size is neither the evaluated picture's nor the region presented's";
            return r;
        }
        r.tier = (shown_size || !s.shown_w) ? kInner : kEvalOnly;
        r.why = eval_size ? "an offscreen target of the evaluated picture's size" : "an offscreen target of the region presented's size";
    }
    if (c.id == s.known_good && s.known_good) {
        r.tier = kKnown;
        r.why = "the target a present already accepted as the capture";
    }
    r.ok = true;
    if (cur != kReject && s.copied) {
        // the frame's picture is already in the window texture: another target drawn now is not what was presented, whatever it is
        r.pick = "the target captured earlier stays: the frame's picture is already in the window texture";
        return r;
    }
    if (cur == kReject) {
        r.place = true;
        r.pick = "the frame's first candidate";
    } else if (r.tier > cur) {
        r.place = true;
        r.pick = "it outranks the target captured earlier";
    } else if (r.tier == cur && c.id != cur_id && cur_id != s.known_good) {
        r.place = true;
        r.pick = "it is the later of two candidates of the same rank (the frame's final blended draw)";
    } else {
        r.pick = "the target captured earlier stays";
    }
    return r;
}

// Present-time update to Scene::known_good. A target accepted via copy is remembered; a direct window-texture capture clears any earlier
// offscreen memory; a remembered target whose capture was not shown is forgotten so later frames can pick again (frames with no capture
// keep the remembered target).
inline uint32_t next_known_good(uint32_t known_good, uint32_t cap_id, bool direct, bool via_copy) {
    if (direct) return 0;
    if (via_copy) return cap_id;
    if (cap_id && cap_id == known_good) return 0;
    return known_good;
}

}  // namespace nxrt::nvn::be::gpu::fgsel
