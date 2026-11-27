# SPDX-License-Identifier: GPL-3.0-or-later
# nxrt: game-independent runtime skeleton (OS/HLE layer) for the Metroid Prime Remastered static recompilation.
# Build: scripts\build.ps1   (clang-cl + Ninja, Release)   Test: ctest --test-dir build\release
cmake_minimum_required(VERSION 3.25)
# Static MSVC runtime (/MT): players need no Visual C++ Redistributable. Set before project() so every target below, third-party code
# included, uses it; the libraries this one links must agree (a mismatch is a link error).
if(NOT DEFINED CMAKE_MSVC_RUNTIME_LIBRARY)
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()
project(nxrt C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_C_STANDARD 11)

if(NOT WIN32)
    message(FATAL_ERROR "nxrt targets native Windows x64 only")
endif()

option(NXRT_BUILD_TESTS "Build the nxrt_tests executable and register its CTest entries" ON)

# The generated-code ABI header (Ctx, call_indirect, ...) lives in the recompiler (recomp/include/nxrecomp/).
# Found next to the checkout (../recomp) or, for a git worktree under runtime-wt/<name>, two levels up (../../recomp).
set(NXRT_RECOMP_INCLUDE "${CMAKE_CURRENT_SOURCE_DIR}/../recomp/include" CACHE PATH "Directory that holds nxrecomp/")
if(NOT EXISTS "${NXRT_RECOMP_INCLUDE}/nxrecomp/ctx.h" AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/../../recomp/include/nxrecomp/ctx.h")
    get_filename_component(_nxrt_ri "${CMAKE_CURRENT_SOURCE_DIR}/../../recomp/include" ABSOLUTE)
    set(NXRT_RECOMP_INCLUDE "${_nxrt_ri}" CACHE PATH "Directory that holds nxrecomp/" FORCE)
endif()

if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    add_compile_options(/W3 -Wno-unused-parameter -Wno-missing-field-initializers -Wno-cast-function-type-mismatch
                        -Wno-cast-function-type-strict -Wno-language-extension-token)
    # CMPXCHG16B. MSVC's <atomic> gives all its functions target("cx16") under clang on x64: without the flag clang may not inline
    # them into code that lacks it, and every std::atomic load, store or add becomes a call ("conflicting target features"). Both
    # builds of the game code need the instruction anyway.
    if(CMAKE_SYSTEM_PROCESSOR MATCHES "AMD64|x86_64")
        add_compile_options($<$<COMPILE_LANGUAGE:C,CXX>:-mcx16>)
    endif()
endif()
# ThinLTO: the C and C++ objects are LLVM bitcode, and lld-link optimises across source files at the link. On by default for Release
# builds with clang and lld-link; the configure says which it is.
set(_nxrt_lto_default OFF)
if(CMAKE_BUILD_TYPE STREQUAL "Release" AND CMAKE_CXX_COMPILER_ID MATCHES "Clang" AND CMAKE_LINKER MATCHES "lld-link")
    set(_nxrt_lto_default ON)
endif()
option(NXRT_LTO "Build the runtime with ThinLTO (needs clang-cl and lld-link)" ${_nxrt_lto_default})
if(NXRT_LTO)
    if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang" OR NOT CMAKE_LINKER MATCHES "lld-link")
        message(FATAL_ERROR "NXRT_LTO needs clang-cl and lld-link (compiler ${CMAKE_CXX_COMPILER_ID}, linker ${CMAKE_LINKER})")
    endif()
    add_compile_options($<$<COMPILE_LANGUAGE:C,CXX>:-flto=thin>)
endif()
message(STATUS "nxrt: ThinLTO ${NXRT_LTO} (linker ${CMAKE_LINKER})")
# Profile-guided builds of the runtime (docs/PGO.md). Release builds with clang 23 or newer use the committed profile,
# pgo/fefw_rt.profdata; a function whose source changed since it was recorded is built without it. NXRT_PGO_PROFILE names
# another profile, or "off" none. NXRT_PGO_GENERATE builds the instrumented runtime that records a new one (a scripted test run
# writes the counters when it ends: games/fefw/fefw_play.cpp).
option(NXRT_PGO_GENERATE "Instrumented runtime that records an LLVM profile (never for a release)" OFF)
set(NXRT_PGO_PROFILE "auto" CACHE STRING "LLVM profile of the runtime: auto (pgo/fefw_rt.profdata), off, or a .profdata file")
set(NXRT_PGO_RUNTIME_DIR "" CACHE PATH "Folder of clang_rt.profile.lib for NXRT_PGO_GENERATE, where the linker does not find it itself")
set(_nxrt_pgo "${NXRT_PGO_PROFILE}")
if(_nxrt_pgo STREQUAL "auto")
    set(_nxrt_pgo "")
    if(NOT NXRT_PGO_GENERATE AND CMAKE_BUILD_TYPE STREQUAL "Release" AND CMAKE_CXX_COMPILER_ID MATCHES "Clang"
       AND NOT CMAKE_CXX_COMPILER_VERSION VERSION_LESS 23 AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/pgo/fefw_rt.profdata")
        set(_nxrt_pgo "${CMAKE_CURRENT_SOURCE_DIR}/pgo/fefw_rt.profdata")
    endif()
elseif(_nxrt_pgo STREQUAL "off")
    set(_nxrt_pgo "")
elseif(_nxrt_pgo)
    # the compiler runs in the build folder: a relative path is taken from the top-level source folder
    get_filename_component(_nxrt_pgo "${_nxrt_pgo}" ABSOLUTE BASE_DIR "${CMAKE_SOURCE_DIR}")
endif()
if((NXRT_PGO_GENERATE OR _nxrt_pgo) AND NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    message(FATAL_ERROR "NXRT_PGO_GENERATE and NXRT_PGO_PROFILE need clang-cl")
endif()
if(NXRT_PGO_GENERATE AND _nxrt_pgo)
    message(FATAL_ERROR "NXRT_PGO_GENERATE records a profile and NXRT_PGO_PROFILE uses one: set only one of them")
endif()
if(NXRT_PGO_GENERATE)
    add_compile_options($<$<COMPILE_LANGUAGE:C,CXX>:-fprofile-instr-generate>)
    if(NXRT_PGO_RUNTIME_DIR)
        link_directories("${NXRT_PGO_RUNTIME_DIR}")     # every target made from here on; nxrt hands it to its users too
    endif()
    message(WARNING "nxrt: instrumented runtime (NXRT_PGO_GENERATE): for recording a profile only, not for a release")
elseif(_nxrt_pgo)
    if(NOT EXISTS "${_nxrt_pgo}")
        message(FATAL_ERROR "NXRT_PGO_PROFILE: no such file: ${_nxrt_pgo}")
    endif()
    # the file's hash in the command line: a new profile under the same name recompiles everything (stale objects would not link
    # together under ThinLTO), and a change of the file configures again
    file(MD5 "${_nxrt_pgo}" _nxrt_pgo_id)
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_nxrt_pgo}")
    add_compile_options("$<$<COMPILE_LANGUAGE:C,CXX>:-fprofile-instr-use=${_nxrt_pgo}>"
                        $<$<COMPILE_LANGUAGE:C,CXX>:-Wno-profile-instr-out-of-date>
                        $<$<COMPILE_LANGUAGE:C,CXX>:-Wno-profile-instr-unprofiled>)
    add_compile_definitions($<$<COMPILE_LANGUAGE:C,CXX>:NXRT_PGO_PROFILE_ID_${_nxrt_pgo_id}>)
    message(STATUS "nxrt: profile ${_nxrt_pgo} (id ${_nxrt_pgo_id})")
else()
    message(STATUS "nxrt: no profile")
endif()
add_compile_definitions(NOMINMAX WIN32_LEAN_AND_MEAN _CRT_SECURE_NO_WARNINGS _WIN32_WINNT=0x0A00)

option(NXRT_BUILD_GFX "Build the graphics shell (SDL3 + Vulkan + NVN front end, src/gfx, cmake/gfx.cmake)" ON)
# Game modules (games/<id>/, docs/GAME_BOUNDARY.md): the title-specific code a game exe links. The test executables register one of them, so
# the tests of the base run against that game's tables (title facts, sites, probes, hooks) as the game exe does.
option(NXRT_BUILD_GAMES "Build the game modules in games/" ON)
set(NXRT_GAMES "fefw" CACHE STRING "ids of the game modules to build (games/<id>/), a list")
set(NXRT_TEST_GAME "fefw" CACHE STRING "id of the game module the test executables register (empty: none)")
if(NOT NXRT_BUILD_GAMES OR NOT NXRT_TEST_GAME IN_LIST NXRT_GAMES)
    set(NXRT_TEST_GAME "")      # the test game must be one of the modules built
endif()

file(GLOB_RECURSE NXRT_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp")
list(FILTER NXRT_SOURCES EXCLUDE REGEX "/src/gfx/")   # the graphics shell is its own library (cmake/gfx.cmake)
add_library(nxrt STATIC ${NXRT_SOURCES})
if(NXRT_PGO_GENERATE AND NXRT_PGO_RUNTIME_DIR)
    target_link_directories(nxrt PUBLIC "${NXRT_PGO_RUNTIME_DIR}")     # fefw_rt and other targets of an enclosing project
endif()
target_include_directories(nxrt PUBLIC include)
if(EXISTS "${NXRT_RECOMP_INCLUDE}")
    target_include_directories(nxrt PUBLIC "${NXRT_RECOMP_INCLUDE}")
endif()
target_link_libraries(nxrt PUBLIC psapi winhttp)
# Media Foundation (src/media/movie_mf.cpp, Switch 1 movies) is loaded only when a movie asks for it: Windows N editions ship without
# it, and a load-time import would keep the game from starting there at all (0xC0000135).
target_link_libraries(nxrt PUBLIC delayimp)
target_link_options(nxrt PUBLIC /DELAYLOAD:mfplat.dll /DELAYLOAD:mfreadwrite.dll)
# The release check (include/nxrt/update_check.h): only a build that names its release asks GitHub (game-build/fefw sets these for the
# release package).
if(NXRT_RELEASE_VERSION)
    set(_nxrt_update_defs "NXRT_RELEASE_VERSION=\"${NXRT_RELEASE_VERSION}\"" "NXRT_UPDATE_REPO=\"${NXRT_UPDATE_REPO}\"")
    if(NXRT_RELEASE_BY_TAG)
        list(APPEND _nxrt_update_defs NXRT_RELEASE_BY_TAG=1)
    endif()
    set_source_files_properties("${CMAKE_CURRENT_SOURCE_DIR}/src/diag/update_check.cpp" PROPERTIES COMPILE_DEFINITIONS "${_nxrt_update_defs}")
endif()
# The known ini keys for config_log_unread (src/diag/config.cpp): regenerated when a source file or CONFIG.md changes.
set(NXRT_CONFIG_KEYS "${CMAKE_CURRENT_BINARY_DIR}/generated/config_keys.inc")
file(GLOB_RECURSE NXRT_CONFIG_KEY_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp" "${CMAKE_CURRENT_SOURCE_DIR}/src/*.h"
     "${CMAKE_CURRENT_SOURCE_DIR}/games/*.cpp" "${CMAKE_CURRENT_SOURCE_DIR}/../game-build/fefw/src/*.cpp")
add_custom_command(OUTPUT "${NXRT_CONFIG_KEYS}"
    COMMAND "${CMAKE_COMMAND}" "-DROOTS=${CMAKE_CURRENT_SOURCE_DIR}/src\;${CMAKE_CURRENT_SOURCE_DIR}/games\;${CMAKE_CURRENT_SOURCE_DIR}/../game-build/fefw/src"
            "-DDOC=${CMAKE_CURRENT_SOURCE_DIR}/docs/CONFIG.md" "-DOUT=${NXRT_CONFIG_KEYS}" -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/config_keys.cmake"
    DEPENDS ${NXRT_CONFIG_KEY_SOURCES} "${CMAKE_CURRENT_SOURCE_DIR}/docs/CONFIG.md" "${CMAKE_CURRENT_SOURCE_DIR}/cmake/config_keys.cmake"
    COMMENT "Listing the known ini keys" VERBATIM)
set_source_files_properties("${CMAKE_CURRENT_SOURCE_DIR}/src/diag/config.cpp" PROPERTIES OBJECT_DEPENDS "${NXRT_CONFIG_KEYS}")
target_sources(nxrt PRIVATE "${NXRT_CONFIG_KEYS}")
target_include_directories(nxrt PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/generated")
include(cmake/media_deps.cmake)   # libvpx for the Switch 2 video decoders (optional)

if(NXRT_BUILD_TESTS)
    enable_testing()
    file(GLOB NXRT_TEST_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/tests/*.cpp")
    # The tests of the test game's own runtime-side code (games/<id>/tests/core) run in the same executable.
    if(NXRT_TEST_GAME)
        file(GLOB NXRT_GAME_CORE_TEST_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/games/${NXRT_TEST_GAME}/tests/core/*.cpp")
        list(APPEND NXRT_TEST_SOURCES ${NXRT_GAME_CORE_TEST_SOURCES})
    endif()
    add_executable(nxrt_tests ${NXRT_TEST_SOURCES})
    target_include_directories(nxrt_tests PRIVATE tests)
    # nxrt calls into the graphics shell (switch2.cpp: HDR metadata, tt_core.cpp: screenshot writes), so the tests link it when it is built
    target_link_libraries(nxrt_tests PRIVATE nxrt $<TARGET_NAME_IF_EXISTS:nxrt_gfx>)
    # A code mod built against the public header, loaded by tests/test_mod_api.cpp: the mod side of the ABI.
    add_library(nxrt_test_mod SHARED tests/mods/test_mod.cpp)
    target_include_directories(nxrt_test_mod PRIVATE include)
    if(EXISTS "${NXRT_RECOMP_INCLUDE}")
        target_include_directories(nxrt_test_mod PRIVATE "${NXRT_RECOMP_INCLUDE}")
    endif()
    add_dependencies(nxrt_tests nxrt_test_mod)
    target_compile_definitions(nxrt_tests PRIVATE NXRT_TEST_MOD_PATH="$<TARGET_FILE:nxrt_test_mod>")
    target_compile_definitions(nxrt_tests PRIVATE NXRT_TEST_DATA_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/data")
    # A linker map next to the exe: the crash handler and the watchdog name host code from it. Keep host allocations low
    # (HIGHENTROPYVA:NO) so the guest ranges at 0x71_0000_0000 and up are free; a 64 MiB main stack.
    target_link_options(nxrt_tests PRIVATE /MAP /STACK:67108864 /HIGHENTROPYVA:NO /DEBUG)

    # One CTest entry per test group; the group names are listed by `nxrt_tests --list`. The list is read at configure
    # time from the NXTEST_GROUP lines of the test sources so that CMake needs no edit when a group is added.
    set(NXRT_GROUPS "")
    foreach(src ${NXRT_TEST_SOURCES})
        file(STRINGS "${src}" lines REGEX "^NXTEST_GROUP\\(")
        foreach(l ${lines})
            string(REGEX REPLACE "^NXTEST_GROUP\\(([a-z0-9_]+)\\).*$" "\\1" g "${l}")
            list(APPEND NXRT_GROUPS ${g})
        endforeach()
    endforeach()
    list(REMOVE_DUPLICATES NXRT_GROUPS)
    foreach(g ${NXRT_GROUPS})
        add_test(NAME ${g} COMMAND nxrt_tests ${g} WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}")
        set_tests_properties(${g} PROPERTIES TIMEOUT 120)
    endforeach()
    if(TEST write_watch)
        set_tests_properties(write_watch PROPERTIES TIMEOUT 300)   # 3000 region rebuilds under four faulting writers: 90-110 s on 4 cores
    endif()
    message(STATUS "nxrt test groups: ${NXRT_GROUPS}")

    # Lifted-code integration tests (tests/lift): need the recompiler, its Python environment and the AArch64 clang.
    option(NXRT_BUILD_LIFT_TESTS "Lift test programs with nxrecomp and run them on the runtime (CTest entries lift_*)" ON)
    # The AArch64 clang the guest programs are built with (recomp/oracle/e2e/build_prog.py reads LLVM_BIN): the environment's LLVM_BIN, else
    # the folder of the host compiler when it is LLVM's (clang-cl next to clang.exe), else LLVM's default install folder.
    set(_nxrt_llvm_default "C:/Program Files/LLVM/bin")
    if(DEFINED ENV{LLVM_BIN})
        file(TO_CMAKE_PATH "$ENV{LLVM_BIN}" _nxrt_llvm_default)
    else()
        get_filename_component(_nxrt_cc_dir "${CMAKE_C_COMPILER}" DIRECTORY)
        if(EXISTS "${_nxrt_cc_dir}/clang.exe")
            set(_nxrt_llvm_default "${_nxrt_cc_dir}")
        endif()
    endif()
    set(NXRT_LIFT_LLVM_BIN "${_nxrt_llvm_default}" CACHE PATH "LLVM bin folder with clang.exe, ld.lld and llvm-objdump for the AArch64 guest programs of tests/lift")
    if(NOT NXRT_BUILD_LIFT_TESTS)
        message(STATUS "nxrt: lifted-code integration tests (tests/lift) are off (NXRT_BUILD_LIFT_TESTS=OFF)")
    elseif(NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/../recomp/cmake/NxRecomp.cmake" OR NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/../tools/.venv/Scripts/python.exe")
        message(STATUS "nxrt: lifted-code integration tests (tests/lift) are off: ../recomp or ../tools/.venv not found")
    elseif(NOT EXISTS "${NXRT_LIFT_LLVM_BIN}/clang.exe")
        message(STATUS "nxrt: lifted-code integration tests (tests/lift) are off: no clang.exe in NXRT_LIFT_LLVM_BIN (${NXRT_LIFT_LLVM_BIN}); set it or LLVM_BIN")
    else()
        add_subdirectory(tests/lift)
    endif()
endif()

if(NXRT_BUILD_GFX)
    include(cmake/gfx.cmake)
endif()

if(NXRT_BUILD_GAMES)
    foreach(_game ${NXRT_GAMES})
        add_subdirectory(games/${_game})
    endforeach()
endif()
# Each test executable registers the most of the test game it links: the core part (runtime tests), the whole module (graphics tests). tests/test_main.cpp calls NXRT_TEST_GAME_REGISTER before the first test.
if(NXRT_BUILD_TESTS AND NXRT_TEST_GAME)
    set(_g "${NXRT_TEST_GAME}")
    foreach(pair "nxrt_tests;nxrt_game_${_g}_core;nxrt_register_game_${_g}_core" "nxrt_gfx_tests;nxrt_game_${_g};nxrt_register_game_${_g}")
        list(GET pair 0 exe)
        list(GET pair 1 lib)
        list(GET pair 2 fn)
        if(TARGET ${exe} AND TARGET ${lib})
            target_link_libraries(${exe} PRIVATE ${lib})
            target_compile_definitions(${exe} PRIVATE NXRT_TEST_GAME_REGISTER=${fn})
        endif()
    endforeach()
endif()
