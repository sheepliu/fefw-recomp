// SPDX-License-Identifier: GPL-3.0-or-later
// Shared by the tests that need the ASTC -> BC7 transcoder (tools/textures): a build configured with NXRT_ASTC2BC7=OFF links the
// stand-in (src/gfx/nvn/nvn_a2b_off.cpp), where every ASTC decode and encode fails. Those tests skip there instead of failing.
#pragma once
#include "astc2bc7.h"
#include "test_framework.h"

namespace astctest {

// true when the transcoder is linked; otherwise marks the running case as skipped
inline bool transcoder_or_skip() {
    if (a2b_bc7_encoder(nullptr) != A2B_ENCODER_NONE) return true;
    nxtest::skip("no ASTC transcoder in this build (NXRT_ASTC2BC7=OFF)");
    return false;
}

}  // namespace astctest
