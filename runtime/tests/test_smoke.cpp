// SPDX-License-Identifier: GPL-3.0-or-later
#include "test_framework.h"
#include "nxrt/abi.h"
#include "nxrt/runtime.h"
NXTEST_GROUP(smoke)

NXTEST(abi_header_resolves) {
    std::printf("    ABI: %s\n", nxrt::abi_source());
    CHECK(nxrt::abi_source() != nullptr);
    Ctx c{};
    CHECK_EQ(sizeof(c.x) / sizeof(c.x[0]), 32u);
}

NXTEST(session_stamp_has_build_and_notes) {
    const std::string saved = nxrt::app_build();
    nxrt::set_app_build("fefw 9.9.9 (abc1234)");
    nxrt::set_session_note("test_gpu", "first");
    nxrt::set_session_note("test_gpu", "Some GPU");   // a key set again replaces its value
    const std::string s = nxrt::session_stamp();
    nxrt::set_app_build(saved.c_str());
    std::printf("    %s\n", s.c_str());
    CHECK(s.rfind("fefw 9.9.9 (abc1234), ABI ", 0) == 0);
    CHECK(s.find("; test_gpu Some GPU") != std::string::npos);
    CHECK(s.find("first") == std::string::npos);
}
