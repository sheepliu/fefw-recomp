// SPDX-License-Identifier: GPL-3.0-or-later
// Shader cache (src/gfx/nvn/nvn_shaders.{h,cpp}): SHA-1 vectors, the control section, the cache's failure path, and the
// full offline cache: every block of the corpus manifest is looked up through shader_cache() and checked against the
// manifest (size, hash, stage, constants) and against the harness's results.tsv record (reflected bindings).
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "nvn_shaders.h"
#include "nxrt/config.h"
#include "test_framework.h"
#include "project_root.h"
NXTEST_GROUP(gfx_nvn_shaders)

using namespace nxrt::nvn::be;
namespace fs = std::filesystem;

namespace {

const std::string kCorpusDirStr = nxtest::project_path("work/shader_cache/corpus_code");
const char* kCorpusDir = kCorpusDirStr.c_str();

bool read_file(const fs::path& p, std::string& out) {
    std::ifstream f(p, std::ios::binary);
    if (!f) return false;
    std::ostringstream s;
    s << f.rdbuf();
    out = s.str();
    return true;
}

std::vector<std::string> split(const std::string& s, char sep) {
    std::vector<std::string> out;
    std::string cur;
    std::istringstream in(s);
    while (std::getline(in, cur, sep)) out.push_back(cur);
    if (!s.empty() && s.back() == sep) out.push_back("");
    return out;
}

std::vector<std::string> sorted_tokens(const std::string& s) {
    std::vector<std::string> t;
    for (auto& x : split(s, ' '))
        if (!x.empty()) t.push_back(x);
    std::sort(t.begin(), t.end());
    return t;
}

// The control section of a block as the corpus files hold it (0x880 bytes), zeroed except what is set.
void make_control(uint8_t* ctrl, uint32_t code_size, uint32_t const_offset, uint32_t const_size) {
    std::memset(ctrl, 0, 0x880);
    auto put = [&](size_t off, uint32_t v) { std::memcpy(ctrl + off, &v, 4); };
    put(0, kShaderControlMagic);
    put(0x6f8, 0x100);
    put(0x6fc, const_size);
    put(0x700, const_offset);
    put(0x704, code_size);
    put(0x708, 0x30);
    put(0x748, 8);
    put(0x74c, 4);
    put(0x750, 2);
}

}  // namespace

NXTEST(sha1_standard_vectors) {
    CHECK_EQ(sha1_hex("", 0), std::string("da39a3ee5e6b4b0d3255bfef95601890afd80709"));
    CHECK_EQ(sha1_hex("abc", 3), std::string("a9993e364706816aba3e25717850c26c9cd0d89d"));
    const std::string two_block = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
    CHECK_EQ(sha1_hex(two_block.data(), two_block.size()), std::string("84983e441c3bd26ebaae4aa1f95129e5e54670f1"));
    // One million 'a', fed in uneven pieces to exercise the buffering.
    Sha1 s;
    std::string a(1000000, 'a');
    size_t off = 0, step = 1;
    while (off < a.size()) {
        size_t n = std::min(step, a.size() - off);
        s.update(a.data() + off, n);
        off += n;
        step = step * 7 % 131 + 1;
    }
    uint8_t d[20];
    s.finish(d);
    char hex[41];
    for (int i = 0; i < 20; i++) std::snprintf(hex + 2 * i, 3, "%02x", d[i]);
    CHECK_EQ(std::string(hex), std::string("34aa973cd4c4daa4f61eeb2bdbad27316534016f"));
}

// Numbers in a harness record that do not fit an int are refused (the parser used a 32-bit long and overflowed before its range check).
NXTEST(harness_numbers_out_of_range) {
    std::vector<HarnessResource> r;
    CHECK(parse_harness_resources("cb2147483647=s0b1", r));
    CHECK_EQ(r.size(), size_t(1));
    if (!r.empty()) CHECK_EQ(r[0].bank, 2147483647);
    CHECK(!parse_harness_resources("cb2147483648=s0b1", r));
    CHECK(!parse_harness_resources("cb4294967297=s0b1", r));      // wrapped to 1 with a 32-bit accumulator
    CHECK(!parse_harness_resources("cb99999999999=s0b1", r));
    CHECK(!parse_harness_resources("sb[c0+0x100000000]=s0b1", r));
}

NXTEST(shader_hash_is_sha1_prefix) {
    CHECK_EQ(shader_hash(reinterpret_cast<const uint8_t*>("abc"), 3), std::string("a9993e364706816a"));
}

NXTEST(read_control_fields) {
    uint8_t ctrl[0x880];
    make_control(ctrl, 0x1400, 0x1300, 0x18);
    ShaderControl c = read_control(ctrl);
    CHECK(c.valid);
    CHECK_EQ(c.code_size, 0x1400u);
    CHECK_EQ(c.const_offset, 0x1300u);
    CHECK_EQ(c.const_size, 0x18u);
    CHECK_EQ(c.program_start, 0x30u);
    CHECK_EQ(c.program_size, 0x100u);
    CHECK_EQ(c.local_size[0], 8u);
    CHECK_EQ(c.local_size[1], 4u);
    CHECK_EQ(c.local_size[2], 2u);
    std::memset(ctrl, 0, sizeof ctrl);
    CHECK(!read_control(ctrl).valid);
}

NXTEST(read_control_on_first_corpus_block) {
    std::string manifest, code, ctrl;
    if (!read_file(fs::path(kCorpusDir) / "manifest.tsv", manifest)) {
        nxtest::skip("corpus manifest missing");
        return;
    }
    auto lines = split(manifest, '\n');
    if (lines.size() < 2) {
        nxtest::skip("corpus manifest empty");
        return;
    }
    auto head = split(lines[0], '\t');
    auto row = split(lines[1], '\t');
    auto col = [&](const char* name) {
        for (size_t i = 0; i < head.size(); i++)
            if (head[i] == name) return row[i];
        return std::string();
    };
    if (!read_file(fs::path(kCorpusDir) / col("code_file"), code) ||
        !read_file(fs::path(kCorpusDir) / col("ctrl_file"), ctrl) || ctrl.size() < 0x880) {
        nxtest::skip("corpus files missing");
        return;
    }
    ShaderControl c = read_control(reinterpret_cast<const uint8_t*>(ctrl.data()));
    CHECK(c.valid);
    CHECK_EQ(uint64_t(c.code_size), uint64_t(code.size()));
    CHECK_EQ(uint64_t(c.const_offset), std::stoull(col("const_off"), nullptr, 16));
    CHECK_EQ(uint64_t(c.const_size), std::stoull(col("const_size"), nullptr, 16));
    CHECK_EQ(uint64_t(c.program_start), std::stoull(col("program_start"), nullptr, 16));
}

NXTEST(cache_missing_directory_fails_quietly) {
    ShaderCache c;
    CHECK(!c.open("D:/no/such/shader/cache"));
    CHECK(!c.is_open());
    uint8_t code[64] = {}, ctrl[0x880];
    make_control(ctrl, sizeof code, 0, 0);
    CHECK(c.find(code, ctrl) == nullptr);
    CHECK(c.find_by_hash("0123456789abcdef") == nullptr);
    CHECK_EQ(c.loaded(), size_t(0));
    CHECK_EQ(c.missing(), size_t(0));
}

NXTEST(cache_rejects_malformed_control) {
    ShaderCache c;
    uint8_t code[64] = {}, ctrl[0x880] = {};  // no magic
    CHECK(c.find(code, ctrl) == nullptr);
    CHECK(c.find(nullptr, ctrl) == nullptr);
}

NXTEST(corpus_all_blocks_through_shader_cache) {
    std::string manifest;
    if (!read_file(fs::path(kCorpusDir) / "manifest.tsv", manifest)) {
        nxtest::skip("corpus manifest missing");
        return;
    }
    if (!shader_cache().is_open()) {
        nxtest::skip("shader cache not open (no results.tsv in the configured or default folder)");
        return;
    }
    const fs::path dir(kCorpusDir);
    const std::string cache_dir = shader_cache().dir();
    std::string tsv;
    if (!read_file(fs::path(cache_dir) / "results.tsv", tsv)) {
        nxtest::skip("results.tsv unreadable");
        return;
    }
    std::unordered_map<std::string, std::string> resources;
    parse_results_tsv(tsv, resources);

    std::vector<std::string> head, failures, unknown;
    size_t rows = 0, found = 0, mismatches = 0, bindings = 0, with_storage = 0;
    std::map<std::string, size_t> stages, sources;
    auto fail_row = [&](const std::string& id, const std::string& why) {
        if (mismatches++ < 10) failures.push_back(id + ": " + why);
    };
    for (const std::string& raw : split(manifest, '\n')) {
        std::string line = raw;
        if (!line.empty() && line.back() == '\r') line.pop_back();
        auto cells = split(line, '\t');
        if (cells.size() < 13) continue;
        if (head.empty()) {
            head = cells;
            continue;
        }
        auto col = [&](const char* name) {
            for (size_t i = 0; i < head.size(); i++)
                if (head[i] == name) return cells[i];
            return std::string();
        };
        rows++;
        const std::string id = col("id"), hash = col("code_sha1_16"), stage = col("stage");
        std::string code, ctrl;
        if (!read_file(dir / col("code_file"), code) || !read_file(dir / col("ctrl_file"), ctrl) || ctrl.size() < 0x880) {
            fail_row(id, "code or control file missing");
            continue;
        }
        const auto* cb = reinterpret_cast<const uint8_t*>(code.data());
        const auto* cc = reinterpret_cast<const uint8_t*>(ctrl.data());
        ShaderControl sc = read_control(cc);
        if (!sc.valid) {
            fail_row(id, "control magic");
            continue;
        }
        if (sc.code_size != code.size() || sc.code_size != std::stoul(col("code_size"), nullptr, 16)) {
            fail_row(id, "code size at control +0x704 differs from the file");
            continue;
        }
        if (shader_hash(cb, sc.code_size) != hash) {
            fail_row(id, "shader_hash(code) differs from code_sha1_16");
            continue;
        }
        const ShaderInfo* info = shader_cache().find(cb, cc);
        if (!info) {
            fail_row(id, "not found in the cache");
            continue;
        }
        found++;
        if (found == 1) {  // the same block again returns the same ShaderInfo, and find_by_hash sees it
            CHECK(shader_cache().find(cb, cc) == info);
            CHECK(shader_cache().find_by_hash(hash) == info);
        }
        if (info->hash != hash) fail_row(id, "ShaderInfo.hash differs");
        if (stage_name(info->stage) != stage) fail_row(id, "stage " + std::string(stage_name(info->stage)) + " vs " + stage);
        if (info->constants.size() != std::stoul(col("const_size"), nullptr, 16) ||
            info->constants.size() != sc.const_size)
            fail_row(id, "constant data length differs from const_size");
        auto rec = resources.find(hash);
        if (rec == resources.end()) {
            fail_row(id, "no results.tsv record");
        } else {
            std::string mine = describe_like_harness(info->refl);
            if (sorted_tokens(mine) != sorted_tokens(rec->second))
                fail_row(id, "reflected \"" + mine + "\" vs harness \"" + rec->second + "\"");
        }
        stages[stage_name(info->stage)]++;
        bool storage = false;
        for (const auto& b : info->refl.bindings) {
            bindings++;
            sources[source_name(b.nvn.source)]++;
            if (b.nvn.source == NvnSource::Unknown) {
                unknown.push_back(id + " set" + std::to_string(b.set) + " binding" + std::to_string(b.binding) + " " +
                                  b.name);
                fail_row(id, "binding " + b.name + " has no NVN meaning");
            }
            if (b.type == DescriptorType::StorageBuffer) storage = true;
        }
        if (storage) with_storage++;
    }

    std::printf("corpus: %zu blocks checked, %zu found in %s, %zu mismatches, %zu bindings, %zu blocks with storage "
                "buffers\n",
                rows, found, cache_dir.c_str(), mismatches, bindings, with_storage);
    for (auto& [k, v] : stages) std::printf("  stage %-9s %zu\n", k.c_str(), v);
    for (auto& [k, v] : sources) std::printf("  binding %-28s %zu\n", k.c_str(), v);
    for (auto& u : unknown) std::printf("  UNKNOWN meaning: %s\n", u.c_str());
    for (auto& f : failures) std::printf("  FAIL %s\n", f.c_str());
    std::printf("  loaded %zu, missing %zu\n", shader_cache().loaded(), shader_cache().missing());
    CHECK(rows > 0);
    CHECK_EQ(found, rows);
    CHECK_EQ(mismatches, size_t(0));
}

// The preload pack (nvn_shaders.cpp "the preload pack") round trip: a cache whose order file lists 40 real shaders writes
// <dir>.preload.pack.new after its boot preload; the next cache renames it to preload.pack and serves those shaders from it with the
// .spv files gone. Real .spv files because load_stub reflects them; skipped without a shader cache.
NXTEST(preload_pack_round_trip) {
    if (!shader_cache().is_open()) {
        nxtest::skip("shader cache not open (no results.tsv in the configured or default folder)");
        return;
    }
    std::string tsv;
    if (!read_file(fs::path(shader_cache().dir()) / "results.tsv", tsv)) {
        nxtest::skip("results.tsv unreadable");
        return;
    }
    const fs::path dir = fs::temp_directory_path() / "nxrt_test_preload_pack" / "spirv";
    std::error_code ec;
    fs::remove_all(dir.parent_path(), ec);
    fs::create_directories(dir);
    std::vector<std::string> lines = split(tsv, '\n'), hashes;
    std::string out_tsv = lines[0] + "\n", order;
    for (size_t i = 1; i < lines.size() && hashes.size() < 40; i++) {
        const std::string hash = lines[i].substr(0, lines[i].find('\t'));
        if (hash.size() != 16 || !fs::copy_file(fs::path(shader_cache().dir()) / (hash + ".spv"), dir / (hash + ".spv"), ec)) continue;
        hashes.push_back(hash);
        out_tsv += lines[i] + "\n";
        order += hash + "\n";
    }
    if (hashes.size() < 40) {
        nxtest::skip("fewer than 40 .spv files in the shader cache");
        return;
    }
    std::ofstream(dir / "results.tsv", std::ios::binary) << out_tsv;
    std::ofstream(dir.parent_path() / "order.txt", std::ios::binary) << order;
    const std::string order_path = (dir.parent_path() / "order.txt").string(), dir_s = dir.generic_string();
    nxrt::config_set("nvn", "shader_order", order_path.c_str());
    const fs::path fresh = dir.parent_path() / "spirv.preload.pack.new", pack = dir.parent_path() / "spirv.preload.pack";
    {
        ShaderCache c;
        CHECK(c.open(dir_s));
        for (int i = 0; i < 300 && !fs::exists(fresh); i++) std::this_thread::sleep_for(std::chrono::milliseconds(100));   // the writer runs after the preload
    }
    CHECK(fs::exists(fresh));
    for (const std::string& h : hashes) fs::remove(dir / (h + ".spv"));
    {
        ShaderCache c;
        CHECK(c.open(dir_s));
        CHECK(fs::exists(pack) && !fs::exists(fresh));
        size_t served = 0;
        for (const std::string& h : hashes)
            if (const ShaderInfo* s = c.load_stub(h); s && !s->spirv.empty()) served++;
        CHECK_EQ(served, hashes.size());
    }
    nxrt::config_set("nvn", "shader_order", "shader_order.txt");
    fs::remove_all(dir.parent_path(), ec);
}

// [nvn] shader_pack_all (default on): the pack written after the boot preload holds every shader of results.tsv, not only the order file's
// (10 of 40 here); with the .spv files gone the next cache serves all 40 from it (resident: from memory). The order file gets the shaders
// the game loads within a few seconds (its own writer thread), not only at a shutdown.
NXTEST(preload_pack_all_shaders) {
    if (!shader_cache().is_open()) {
        nxtest::skip("shader cache not open (no results.tsv in the configured or default folder)");
        return;
    }
    std::string tsv;
    if (!read_file(fs::path(shader_cache().dir()) / "results.tsv", tsv)) {
        nxtest::skip("results.tsv unreadable");
        return;
    }
    const fs::path dir = fs::temp_directory_path() / "nxrt_test_pack_all" / "spirv";
    std::error_code ec;
    fs::remove_all(dir.parent_path(), ec);
    fs::create_directories(dir);
    std::vector<std::string> lines = split(tsv, '\n'), hashes;
    std::string out_tsv = lines[0] + "\n", order;
    for (size_t i = 1; i < lines.size() && hashes.size() < 40; i++) {
        const std::string hash = lines[i].substr(0, lines[i].find('\t'));
        if (hash.size() != 16 || !fs::copy_file(fs::path(shader_cache().dir()) / (hash + ".spv"), dir / (hash + ".spv"), ec)) continue;
        hashes.push_back(hash);
        out_tsv += lines[i] + "\n";
        if (hashes.size() <= 10) order += hash + "\n";
    }
    if (hashes.size() < 40) {
        nxtest::skip("fewer than 40 .spv files in the shader cache");
        return;
    }
    std::ofstream(dir / "results.tsv", std::ios::binary) << out_tsv;
    std::ofstream(dir.parent_path() / "order.txt", std::ios::binary) << order;
    const std::string order_path = (dir.parent_path() / "order.txt").string(), dir_s = dir.generic_string();
    nxrt::config_set("nvn", "shader_order", order_path.c_str());
    const fs::path fresh = dir.parent_path() / "spirv.preload.pack.new", pack = dir.parent_path() / "spirv.preload.pack";
    {
        ShaderCache c;
        CHECK(c.open(dir_s));
        for (int i = 0; i < 600 && !fs::exists(fresh); i++) std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    CHECK(fs::exists(fresh));
    for (const std::string& h : hashes) fs::remove(dir / (h + ".spv"));
    {
        ShaderCache c;
        CHECK(c.open(dir_s));
        CHECK(fs::exists(pack) && !fs::exists(fresh));
        std::this_thread::sleep_for(std::chrono::milliseconds(500));       // the resident read of the pack
        size_t served = 0;
        for (const std::string& h : hashes)
            if (const ShaderInfo* s = c.load_stub(h); s && !s->spirv.empty()) served++;
        CHECK_EQ(served, hashes.size());
    }
    nxrt::config_set("nvn", "shader_order", "shader_order.txt");
    fs::remove_all(dir.parent_path(), ec);
}

NXTEST(reflect_compute_ssbo_written_and_atomic) {
    // Minimal compute SPIR-V with 4 storage buffers and 1 storage texel buffer:
    //   set 1 binding 0: read-only (OpLoad only)                    -> written=false, atomic=false
    //   set 1 binding 1: stored via OpStore                         -> written=true,  atomic=false
    //   set 1 binding 2: modified via OpAtomicFAddEXT               -> written=true,  atomic=true
    //   set 1 binding 3: selected via OpSelect + OpStore            -> written=true,  atomic=false
    //   set 3 binding 0: imageBuffer via OpImageTexelPointer+Atomic -> written=true,  atomic=true
    std::vector<uint32_t> spv = {0x07230203u, 0x00010300u, 0u, 64u, 0u};
    auto emit = [&](std::initializer_list<uint32_t> words) {
        spv.push_back(static_cast<uint32_t>(words.size() << 16) | *words.begin());
        spv.insert(spv.end(), words.begin() + 1, words.end());
    };
    emit({15, 5, 1, 0x6e69616du, 0});   // OpEntryPoint GLCompute %1 "main"
    emit({71, 20, 34, 1}); emit({71, 20, 33, 0});   // set 1 binding 0
    emit({71, 21, 34, 1}); emit({71, 21, 33, 1});   // set 1 binding 1
    emit({71, 22, 34, 1}); emit({71, 22, 33, 2});   // set 1 binding 2
    emit({71, 23, 34, 1}); emit({71, 23, 33, 3});   // set 1 binding 3
    emit({71, 24, 34, 3}); emit({71, 24, 33, 0});   // set 3 binding 0 (storage texel buffer)
    emit({71, 13, 2});                              // OpDecorate %struct Block
    emit({19, 2});                                  // %2 = OpTypeVoid
    emit({33, 3, 2});                               // %3 = OpTypeFunction %void
    emit({22, 10, 32});                             // %10 = OpTypeFloat 32
    emit({21, 11, 32, 0});                          // %11 = OpTypeInt 32 0
    emit({20, 12});                                 // %12 = OpTypeBool
    emit({30, 13, 10});                             // %13 = OpTypeStruct %float
    emit({32, 14, 12, 13});                         // %14 = OpTypePointer StorageBuffer %struct
    emit({32, 15, 12, 10});                         // %15 = OpTypePointer StorageBuffer %float
    emit({25, 25, 11, 5, 0, 0, 0, 2, 33});          // %25 = OpTypeImage %uint Buffer 0 0 0 2 R32ui
    emit({32, 26, 0, 25});                          // %26 = OpTypePointer UniformConstant %img
    emit({32, 27, 11, 11});                         // %27 = OpTypePointer Image %uint
    emit({43, 11, 16, 0});                          // %16 = OpConstant uint 0
    emit({43, 10, 17, 0x3f800000u});                // %17 = OpConstant float 1.0
    emit({41, 12, 18});                             // %18 = OpConstantTrue %bool
    emit({59, 14, 20, 12});                         // %20 = OpVariable StorageBuffer (binding 0)
    emit({59, 14, 21, 12});                         // %21 = OpVariable StorageBuffer (binding 1)
    emit({59, 14, 22, 12});                         // %22 = OpVariable StorageBuffer (binding 2)
    emit({59, 14, 23, 12});                         // %23 = OpVariable StorageBuffer (binding 3)
    emit({59, 26, 24, 0});                          // %24 = OpVariable UniformConstant (storage texel buffer)
    emit({54, 2, 1, 0, 3});                         // OpFunction %1
    emit({248, 30});                                // OpLabel
    emit({65, 15, 31, 20, 16});                     // %31 = OpAccessChain %20[0]
    emit({61, 10, 32, 31});                         // %32 = OpLoad %31 (read-only)
    emit({65, 15, 33, 21, 16});                     // %33 = OpAccessChain %21[0]
    emit({62, 33, 32});                             // OpStore %33 %32
    emit({65, 15, 34, 22, 16});                     // %34 = OpAccessChain %22[0]
    emit({6035, 10, 35, 34, 16, 16, 17});           // %35 = OpAtomicFAddEXT %34
    emit({65, 15, 36, 23, 16});                     // %36 = OpAccessChain %23[0]
    emit({169, 15, 37, 18, 33, 36});                // %37 = OpSelect %ptr %true %33 %36
    emit({62, 37, 17});                             // OpStore %37 %17
    emit({60, 27, 38, 24, 16, 16});                 // %38 = OpImageTexelPointer %24 0 0
    emit({234, 11, 39, 38, 16, 16, 16});            // %39 = OpAtomicIAdd %38
    emit({253});                                    // OpReturn
    emit({56});                                     // OpFunctionEnd

    ShaderReflection refl;
    std::string err;
    REQUIRE(reflect_spirv(spv.data(), spv.size(), refl, &err));
    CHECK(refl.stage == ShaderStage::Compute);
    const DescriptorBinding* b0 = refl.find(1, 0);
    const DescriptorBinding* b1 = refl.find(1, 1);
    const DescriptorBinding* b2 = refl.find(1, 2);
    const DescriptorBinding* b3 = refl.find(1, 3);
    const DescriptorBinding* b4 = refl.find(3, 0);
    REQUIRE(b0 && b1 && b2 && b3 && b4);
    CHECK(!b0->written && !b0->atomic);
    CHECK(b1->written && !b1->atomic);
    CHECK(b2->written && b2->atomic);
    CHECK(b3->written && !b3->atomic);
    CHECK(b4->type == DescriptorType::StorageTexelBuffer);
    CHECK(b4->written && b4->atomic);
}
