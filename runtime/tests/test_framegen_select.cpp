// SPDX-License-Identifier: GPL-3.0-or-later
// The rule that picks the HUD-less capture target (nvn_framegen_select.h, used by nvn_framegen.cpp), scene by scene. Pure data, no Vulkan and
// no device: the frame's draws are offered in the order the game records them, and the case states what the frame ends up capturing.
//
// The scenes are Fortune's Weave's (issue 75's reporter's log): in HDR the window texture is an RGBA8 sRGB target kept as RGBA16F
// (extras::hdr_promote_target), so the frame's 8-bit sRGB targets hold RGBA16F too, and the four 2560x1440 targets t10, t64, t144 and t155
// are told apart by their NVN format and size only. In SDR nothing is promoted and every image holds the format its NVN format names.
#include "../src/gfx/nvn/nvn_framegen_select.h"

#include "test_framework.h"
NXTEST_GROUP(framegen_select)

namespace {
namespace fgsel = nxrt::nvn::be::gpu::fgsel;

// NVNformat and VkFormat values: compared for equality only, never interpreted.
constexpr int kRgba8Srgb = 1;
constexpr int kR11G11B10F = 2;
constexpr int kRgba16f = 3;
constexpr int kVkRgba8Srgb = 43;
constexpr int kVkRgba16f = 97;
constexpr int kVkB10G11R11 = 122;

fgsel::Fmt rgba8_promoted() { return fgsel::Fmt{kRgba8Srgb, kVkRgba16f, kVkRgba8Srgb}; }   // an 8-bit sRGB target kept as float16 (HDR scene)
fgsel::Fmt rgba8_sdr() { return fgsel::Fmt{kRgba8Srgb, kVkRgba8Srgb, kVkRgba8Srgb}; }
fgsel::Fmt r11_promoted() { return fgsel::Fmt{kR11G11B10F, kVkRgba16f, kVkB10G11R11}; }   // [gfx] float16_scene
fgsel::Fmt rgba16f_native() { return fgsel::Fmt{kRgba16f, kVkRgba16f, kVkRgba16f}; }

fgsel::Scene scene(const fgsel::Fmt& window, uint32_t w, uint32_t h) {
    fgsel::Scene s;
    s.have_window = true;
    s.window = window;
    s.eval_w = w;
    s.eval_h = h;
    s.shown_w = w;
    s.shown_h = h;
    return s;
}

fgsel::Look target(uint32_t id, uint32_t w, uint32_t h, const fgsel::Fmt& f) {
    fgsel::Look c;
    c.id = id;
    c.w = w;
    c.h = h;
    c.fmt = f;
    c.blended = true;
    c.written = true;
    return c;
}

fgsel::Look window_texture(uint32_t id, uint32_t w, uint32_t h, const fgsel::Fmt& f) {
    fgsel::Look c = target(id, w, h, f);
    c.window_texture = true;
    return c;
}

// The game's frame, as the replay offers its draws: every blended draw after the evaluation goes through here, in order.
struct Frame {
    explicit Frame(const fgsel::Scene& s) : sc(s) {}
    bool offer(const fgsel::Look& c) {
        const fgsel::Choice ch = fgsel::judge(sc, c, picked ? tier : fgsel::kReject, picked);
        last = ch;
        if (!ch.ok || !ch.place) return false;
        picked = c.id;
        tier = ch.tier;
        return true;
    }
    const fgsel::Scene& sc;
    uint32_t picked = 0;
    int tier = fgsel::kReject;
    fgsel::Choice last;
};
}  // namespace

// The issue's failure: a 320x180 glare-chain target of the window's promoted format came before the 2560x1440 target the UI is blended over.
NXTEST(a_small_target_of_the_window_format_is_not_a_candidate) {
    const fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    Frame f(sc);
    CHECK(!f.offer(target(148, 320, 180, r11_promoted())));
    CHECK_EQ(f.picked, 0u);
    CHECK(!f.last.ok);
    CHECK_EQ(f.last.tier, static_cast<int>(fgsel::kReject));
    CHECK(f.last.why[0] != '\0');   // named in the log
}

// 320x180 is not the only thing that makes a target wrong: the same size and Vulkan format, another NVN format, is a post effect, not the
// UI's composite.
NXTEST(a_full_size_post_effect_target_is_not_a_candidate) {
    const fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    Frame f(sc);
    CHECK(!f.offer(target(155, 2560, 1440, r11_promoted())));
    CHECK(!f.offer(target(160, 2560, 1440, rgba16f_native())));
    CHECK_EQ(f.picked, 0u);
}

// The scene's own target: the evaluated picture's size, the window texture's NVN format and Vulkan format, written after the evaluation.
NXTEST(the_scene_target_of_the_evaluated_size_is_captured) {
    const fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    Frame f(sc);
    CHECK(f.offer(target(10, 2560, 1440, rgba8_promoted())));
    CHECK_EQ(f.picked, 10u);
    CHECK_EQ(f.tier, static_cast<int>(fgsel::kInner));
}

// Issue 75's sequence, in order: the glare chain's small target first, then the scene's own target. The first is refused, so it cannot end
// the search (the latch at nvn_framegen.cpp:115), and the frame is captured.
NXTEST(a_bad_candidate_does_not_end_the_search) {
    const fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    Frame f(sc);
    CHECK(!f.offer(target(148, 320, 180, r11_promoted())));
    CHECK(!f.offer(target(64, 2560, 1440, r11_promoted())));
    CHECK(f.offer(target(144, 2560, 1440, rgba8_promoted())));
    CHECK_EQ(f.picked, 144u);
}

// Four targets of the evaluated size and the window's formats tie (t10, t64, t144, t155 in the reporter's scene): the later blended draw
// takes the capture, because the game's copy of the picture into the window follows the frame's last one.
NXTEST(a_later_tied_candidate_takes_the_capture) {
    const fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    Frame f(sc);
    CHECK(f.offer(target(10, 2560, 1440, rgba8_promoted())));
    CHECK(f.offer(target(64, 2560, 1440, rgba8_promoted())));
    CHECK(f.offer(target(144, 2560, 1440, rgba8_promoted())));
    CHECK_EQ(f.picked, 144u);
    CHECK(f.last.pick[0] != '\0');   // the tie-break is named in the log
}

// ...until a present accepts one of them: from then on that target is the capture even when another tied draw comes after it.
NXTEST(the_target_a_present_accepted_is_kept) {
    fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    sc.known_good = 10;
    Frame f(sc);
    CHECK(f.offer(target(64, 2560, 1440, rgba8_promoted())));
    CHECK(f.offer(target(10, 2560, 1440, rgba8_promoted())));
    CHECK(!f.offer(target(144, 2560, 1440, rgba8_promoted())));
    CHECK_EQ(f.picked, 10u);
    CHECK_EQ(f.tier, static_cast<int>(fgsel::kKnown));
}

// Once the game has drawn the captured picture into the window texture, no later target can be what was presented: the frame keeps the
// capture it copied (a same-sized target drawn after that is another pass, not the picture).
NXTEST(a_capture_after_the_window_copy_is_not_replaced) {
    fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    Frame f(sc);
    CHECK(f.offer(target(10, 2560, 1440, rgba8_promoted())));
    sc.copied = true;   // the replay saw a draw into a window texture
    CHECK(!f.offer(target(64, 2560, 1440, rgba8_promoted())));
    CHECK_EQ(f.picked, 10u);
    CHECK(f.last.pick[0] != '\0');
}

// A larger window texture, of which only the evaluated region is presented: the window target is still selectable.
NXTEST(a_larger_window_texture_presented_through_a_crop_is_captured) {
    const fgsel::Scene sc = scene(rgba8_sdr(), 1920, 1080);
    Frame f(sc);
    const fgsel::Look w = window_texture(8, 3840, 2160, rgba8_sdr());
    CHECK(f.offer(w));
    CHECK_EQ(f.picked, 8u);
    CHECK_EQ(f.tier, static_cast<int>(fgsel::kWindow));
}

NXTEST(a_window_texture_smaller_than_the_region_presented_is_not_captured) {
    const fgsel::Scene sc = scene(rgba8_sdr(), 1920, 1080);
    Frame f(sc);
    CHECK(!f.offer(window_texture(8, 1280, 720, rgba8_sdr())));
    CHECK_EQ(f.picked, 0u);
}

// The window texture itself is a candidate before anything was captured (after a capture, a draw into it is the game's copy of the picture,
// which the replay decides before this rule is asked).
NXTEST(the_window_texture_of_the_evaluated_size_is_captured) {
    const fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    Frame f(sc);
    CHECK(f.offer(window_texture(8, 2560, 1440, rgba8_promoted())));
    CHECK_EQ(f.picked, 8u);
    CHECK_EQ(f.tier, static_cast<int>(fgsel::kWindow));
}

// A draw without blending of its own after the evaluation (a pass that only blends over something else) is not the UI's composite.
NXTEST(a_target_not_written_after_the_evaluation_is_not_a_candidate) {
    const fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    Frame f(sc);
    fgsel::Look c = target(10, 2560, 1440, rgba8_promoted());
    c.written = false;
    CHECK(!f.offer(c));
}

NXTEST(a_blended_draw_is_the_only_kind_offered) {
    const fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    Frame f(sc);
    fgsel::Look c = target(10, 2560, 1440, rgba8_promoted());
    c.blended = false;
    CHECK(!f.offer(c));
}

NXTEST(no_window_texture_presented_yet_is_not_a_candidate) {
    fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    sc.have_window = false;
    Frame f(sc);
    CHECK(!f.offer(target(10, 2560, 1440, rgba8_promoted())));
}

// SDR, where no image holds a promoted or fallback format: the rule is the one before this change, a match of the Vulkan format (and of the
// size the present accepts), so a frame selects what it selected before.
NXTEST(sdr_keeps_the_vulkan_format_rule) {
    const fgsel::Scene sc = scene(rgba8_sdr(), 1920, 1080);
    Frame f(sc);
    CHECK(f.offer(target(10, 1920, 1080, rgba8_sdr())));
    CHECK_EQ(f.picked, 10u);
    // another NVN format, the same Vulkan format: accepted in SDR, because nothing was promoted and the Vulkan format is the NVN format
    Frame g(sc);
    CHECK(g.offer(target(11, 1920, 1080, fgsel::Fmt{kR11G11B10F, kVkRgba8Srgb, kVkRgba8Srgb})));
    CHECK_EQ(g.picked, 11u);
    // a different Vulkan format is not the window texture's, promoted or not
    Frame h(sc);
    CHECK(!h.offer(target(12, 1920, 1080, rgba16f_native())));
    // a different size is neither the evaluated picture's nor the region presented's
    Frame i(sc);
    CHECK(!i.offer(target(13, 1280, 720, rgba8_sdr())));
}

// R11G11B10F taking the silent RGBA16F fallback (nvn_format.cpp:154-155, a GPU with no B10G11R11): the window texture and a natively
// RGBA16F target hold the same Vulkan format, and the NVN format is what says which one the game's picture was drawn into.
NXTEST(a_fallback_format_needs_the_nvn_format_too) {
    const fgsel::Scene sc = scene(r11_promoted(), 1920, 1080);
    Frame f(sc);
    CHECK(!f.offer(target(20, 1920, 1080, rgba16f_native())));
    CHECK(f.offer(target(21, 1920, 1080, r11_promoted())));
    CHECK_EQ(f.picked, 21u);
}

// A candidate of the region presented rather than of the evaluated picture: the game presented a crop of the scene target.
NXTEST(the_region_presented_may_stand_for_the_evaluated_size) {
    fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    sc.shown_w = 1920;
    sc.shown_h = 1080;
    Frame f(sc);
    CHECK(f.offer(target(30, 1920, 1080, rgba8_promoted())));
    CHECK_EQ(f.picked, 30u);
}

// Nothing known about the evaluated picture (no evaluation this frame) leaves the size the region presented decides.
NXTEST(without_an_evaluated_size_the_region_presented_decides) {
    fgsel::Scene sc = scene(rgba8_promoted(), 0, 0);
    sc.shown_w = 2560;
    sc.shown_h = 1440;
    Frame f(sc);
    CHECK(f.offer(target(10, 2560, 1440, rgba8_promoted())));
    Frame g(sc);
    CHECK(!g.offer(target(11, 1280, 720, rgba8_promoted())));
}

// When the evaluated size and the region presented differ (cropped present), a target matching the region presented (which passes
// present-time validation) outranks one matching only the evaluated size, in either draw order.
NXTEST(shown_size_outranks_eval_only_size_when_cropped) {
    fgsel::Scene sc = scene(rgba8_promoted(), 2560, 1440);
    sc.shown_w = 1920;
    sc.shown_h = 1080;

    Frame f(sc);
    CHECK(f.offer(target(20, 2560, 1440, rgba8_promoted())));
    CHECK_EQ(f.tier, static_cast<int>(fgsel::kEvalOnly));
    CHECK(f.offer(target(30, 1920, 1080, rgba8_promoted())));
    CHECK_EQ(f.picked, 30u);
    CHECK_EQ(f.tier, static_cast<int>(fgsel::kInner));
    // a later eval-only target cannot clobber the shown-size capture
    CHECK(!f.offer(target(40, 2560, 1440, rgba8_promoted())));
    CHECK_EQ(f.picked, 30u);
}

// Present-time updates to known_good: remembered on via_copy, cleared if the remembered target is no longer shown or if the game switches
// to direct window rendering, and preserved across frames that simply had no capture.
NXTEST(known_good_clears_when_unshown_or_direct) {
    uint32_t kg = 0;
    kg = fgsel::next_known_good(kg, 10, false, true);    // accepted via copy: remember t10
    CHECK_EQ(kg, 10u);
    kg = fgsel::next_known_good(kg, 0, false, false);    // frame with no UI composite: keep t10
    CHECK_EQ(kg, 10u);
    kg = fgsel::next_known_good(kg, 64, false, false);   // another unshown target: keep t10
    CHECK_EQ(kg, 10u);
    kg = fgsel::next_known_good(kg, 10, false, false);   // t10 itself was not shown: forget it
    CHECK_EQ(kg, 0u);
    kg = fgsel::next_known_good(kg, 144, false, true);   // new accepted target: remember t144
    CHECK_EQ(kg, 144u);
    kg = fgsel::next_known_good(kg, 8, true, false);     // switched to direct window capture: clear offscreen memory
    CHECK_EQ(kg, 0u);
}
