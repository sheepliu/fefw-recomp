// SPDX-License-Identifier: GPL-3.0-or-later
// The HUD-less colour of frame generation (FSR 3 and DLSS-G; [dlss] frame_gen, [dlss] frame_gen_hudless): the picture of the frame without
// the game's UI, so that frame generation interpolates the scene only and takes the UI from the real frame (where the presented picture
// and the HUD-less one differ).
//
// The game draws its UI into a layer of its own and blends that layer over the finished scene in one draw, after its DLSS evaluation and
// after the scene's post effects (Fortune's Weave: the tone map into a 2560x1440 RGBA8 sRGB target, the UI layer blended over it, then a
// copy of that target into the window texture). The capture is the target of that blend, just before it: a blended draw after the frame's
// evaluation, into the window texture itself or into a texture that an unblended draw wrote after the evaluation, of the window texture's
// NVN and Vulkan formats, and of the size of the picture the evaluator produced (nvn_framegen_select.h). Every candidate of the frame is
// judged, not only the first: a later one of a better rank, or of the same rank after an earlier one (the last blended draw is the one the
// copy into the window follows), takes the capture, and only a target a present has already accepted is kept for good. At the present the
// capture is used when it is the presented window texture, or when it was then drawn into the presented window texture and is the size of
// the window's crop; it goes through the present pass, as the picture does, into an image of the backbuffer's size and format.
#pragma once
#include <functional>

#include "nvn_gpu_internal.h"

namespace nxrt::nvn::be::gpu {

bool fg_hudless_on();      // [dlss] frame_gen and [dlss] frame_gen_hudless (default 1), read once
void fg_note_eval(VkExtent2D eval_size);   // replay: this frame's evaluation fed frame generation; eval_size is the picture it produced
// replay, before a draw into colour target t (blended: blending on target 0): true = call fg_hudless_capture now, with no pass open
bool fg_hudless_candidate(Texture* t, bool blended);
void fg_hudless_capture(Frame& f, Texture* t);
// present, after the picture is recorded: draw(src, src_extent, crop, dst) records the picture from src (a view in
// SHADER_READ_ONLY_OPTIMAL of an image of src_extent, of which crop is shown) into dst (COLOR_ATTACHMENT_OPTIMAL, the backbuffer's size
// and format) as the present does
using FgPictureDraw = std::function<void(VkImageView, VkExtent2D, const int32_t*, VkImageView)>;
// crop: the window's crop as presented (in image texels; w == 0: all)
void fg_hudless_present(Window* w, Texture* presented, Frame& f, VkExtent2D bb, VkFormat bb_format, bool again, const int32_t crop[4],
                        const FgPictureDraw& draw);

}  // namespace nxrt::nvn::be::gpu
