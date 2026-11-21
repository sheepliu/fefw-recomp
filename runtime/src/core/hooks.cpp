// SPDX-License-Identifier: GPL-3.0-or-later
// Site hooks (hooks.h): nx_site_inline / nx_site_midhook / nx_hook_entry, the runtime half of the lifter's site lists.
// Design: an immutable snapshot (address -> hooks of each kind) published through an atomic pointer. Registration copies the snapshot,
// edits the copy and publishes it; retired snapshots stay allocated until hooks_clear() because a guest thread may still be reading one.
// The call path takes no lock. The site-list scheme the lifted callouts come from is ssbu-recomp's tools/lifter/relift_sites.py (via
// recomp nxrecomp/program.py load_sites); nothing of ssbu's code is used here.
#include "nxrt/hooks.h"

#include <atomic>
#include <memory>
#include <mutex>
#include <unordered_map>
#include <vector>

#include "nxrt/log.h"

namespace nxrt {
namespace {

// Per-thread cache-line-aligned counter shards so hot inline sites running in parallel across
// worker threads (e.g. fefw's cloth hooks) do not bounce a single atomic cache line across cores.
constexpr size_t kCallShards = 32;  // power of two

struct alignas(64) CallShard {
    std::atomic<uint64_t> count{0};
};

struct alignas(64) SiteShard {
    std::atomic<uint64_t> calls[3] = {};
};

SiteShard g_site_shards[kCallShards];
std::atomic<uint32_t> g_next_shard{0};

inline size_t thread_shard() {
    thread_local const size_t s = g_next_shard.fetch_add(1, std::memory_order_relaxed) & (kCallShards - 1);
    return s;
}

struct Hook {
    uint32_t id = 0;
    HookKind kind = HookKind::Inline;
    uint64_t addr = 0;
    InlineHookFn inl = nullptr;
    SiteHookFn site = nullptr;
    void* user = nullptr;
    CallShard shards[kCallShards];

    void inc(size_t shard) { shards[shard].count.fetch_add(1, std::memory_order_relaxed); }
    uint64_t total_calls() const {
        uint64_t sum = 0;
        for (const CallShard& s : shards) sum += s.count.load(std::memory_order_relaxed);
        return sum;
    }
};

struct FlatSlot {
    uint64_t addr = 0;
    Hook* first = nullptr;     // nullptr = empty slot; common 1-hook case avoids any extra indirection
    uint32_t more_off = 0;     // start index in Snapshot::overflow when more_count > 0
    uint32_t more_count = 0;
};

struct FlatTable {
    std::vector<FlatSlot> slots;   // empty, or power-of-two capacity with load factor <= 0.5
    size_t mask = 0;

    static size_t hash(uint64_t addr) {
        // Guest AArch64 instructions are 4-byte aligned: drop low zero bits before mixing.
        uint64_t x = addr >> 2;
        x ^= x >> 33;
        x *= 0xff51afd7ed558ccdULL;
        x ^= x >> 33;
        return static_cast<size_t>(x);
    }

    const FlatSlot* find(uint64_t addr) const {
        if (slots.empty()) return nullptr;
        size_t i = hash(addr) & mask;
        for (;;) {
            const FlatSlot& e = slots[i];
            if (!e.first) return nullptr;
            if (e.addr == addr) return &e;
            i = (i + 1) & mask;
        }
    }
};

struct Snapshot {
    FlatTable table[3];                       // indexed by HookKind (Inline, Mid, Entry)
    std::vector<Hook*> overflow;              // hooks[1..] for addresses with >1 hook of a kind
    std::vector<std::shared_ptr<Hook>> keep;  // keeps Hook objects alive while retired snapshots may be read
};

std::mutex g_write;
std::atomic<const Snapshot*> g_snap{nullptr};
std::vector<std::unique_ptr<const Snapshot>> g_retired;          // owns every published snapshot (the current one is the last)
std::vector<std::shared_ptr<Hook>> g_all;                        // by registration order, for hook_calls / counting
uint32_t g_next_id = 1;

// Builds a fresh immutable snapshot from g_all (caller holds g_write).
std::unique_ptr<Snapshot> build_snapshot() {
    auto snap = std::make_unique<Snapshot>();
    snap->keep = g_all;
    for (int k = 0; k < 3; ++k) {
        std::unordered_map<uint64_t, std::vector<Hook*>> grouped;
        for (const auto& h : g_all)
            if (static_cast<int>(h->kind) == k) grouped[h->addr].push_back(h.get());
        if (grouped.empty()) continue;
        size_t cap = 16;
        while (cap < grouped.size() * 2) cap <<= 1;
        FlatTable& t = snap->table[k];
        t.slots.resize(cap);
        t.mask = cap - 1;
        for (const auto& kv : grouped) {
            size_t i = FlatTable::hash(kv.first) & t.mask;
            while (t.slots[i].first) i = (i + 1) & t.mask;
            FlatSlot& sl = t.slots[i];
            sl.addr = kv.first;
            sl.first = kv.second[0];
            if (kv.second.size() > 1) {
                sl.more_off = static_cast<uint32_t>(snap->overflow.size());
                sl.more_count = static_cast<uint32_t>(kv.second.size() - 1);
                snap->overflow.insert(snap->overflow.end(), kv.second.begin() + 1, kv.second.end());
            }
        }
    }
    return snap;
}

void publish(std::unique_ptr<Snapshot> s) {
    const Snapshot* raw = s.get();
    g_retired.push_back(std::move(s));
    g_snap.store(raw, std::memory_order_release);
}

uint32_t add(HookKind kind, uint64_t addr, InlineHookFn inl, SiteHookFn site, void* user) {
    if (!(kind == HookKind::Inline ? inl != nullptr : site != nullptr)) return 0;
    std::lock_guard<std::mutex> lk(g_write);
    auto h = std::make_shared<Hook>();
    h->id = g_next_id++;
    h->kind = kind;
    h->addr = addr;
    h->inl = inl;
    h->site = site;
    h->user = user;
    g_all.push_back(h);
    publish(build_snapshot());
    return h->id;
}

}  // namespace

uint32_t hook_add_inline(uint64_t addr, InlineHookFn fn, void* user) { return add(HookKind::Inline, addr, fn, nullptr, user); }
uint32_t hook_add_mid(uint64_t addr, SiteHookFn fn, void* user) { return add(HookKind::Mid, addr, nullptr, fn, user); }
uint32_t hook_add_entry(uint64_t addr, SiteHookFn fn, void* user) { return add(HookKind::Entry, addr, nullptr, fn, user); }

bool hook_remove(uint32_t id) {
    std::lock_guard<std::mutex> lk(g_write);
    bool found = false;
    for (size_t i = 0; i < g_all.size(); i++) {
        if (g_all[i]->id == id) {
            g_all.erase(g_all.begin() + static_cast<ptrdiff_t>(i));
            found = true;
            break;
        }
    }
    if (!found) return false;
    publish(build_snapshot());
    return true;
}

size_t hook_count() {
    std::lock_guard<std::mutex> lk(g_write);
    return g_all.size();
}

size_t hook_count_at(uint64_t addr, HookKind kind) {
    const Snapshot* s = g_snap.load(std::memory_order_acquire);
    if (!s) return 0;
    const FlatSlot* sl = s->table[static_cast<int>(kind)].find(addr);
    return sl ? static_cast<size_t>(1u + sl->more_count) : 0;
}

uint64_t hook_calls(uint32_t id) {
    std::lock_guard<std::mutex> lk(g_write);
    for (auto& h : g_all)
        if (h->id == id) return h->total_calls();
    return 0;
}

uint64_t hook_site_calls(HookKind kind) {
    const int k = static_cast<int>(kind);
    uint64_t sum = 0;
    for (const SiteShard& s : g_site_shards) sum += s.calls[k].load(std::memory_order_relaxed);
    return sum;
}

void hooks_clear() {
    std::lock_guard<std::mutex> lk(g_write);
    g_snap.store(nullptr, std::memory_order_release);
    g_retired.clear();
    g_all.clear();
    for (SiteShard& s : g_site_shards)
        for (auto& c : s.calls) c.store(0, std::memory_order_relaxed);
}

}  // namespace nxrt

extern "C" {

void nx_site_inline(Ctx* c, uint64_t addr) {
    const size_t shard = nxrt::thread_shard();
    nxrt::g_site_shards[shard].calls[0].fetch_add(1, std::memory_order_relaxed);
    const nxrt::Snapshot* s = nxrt::g_snap.load(std::memory_order_acquire);
    if (!s) return;
    const nxrt::FlatSlot* sl = s->table[0].find(addr);
    if (!sl) return;
    nxrt::Hook* h = sl->first;
    h->inc(shard);
    h->inl(c, addr, h->user);
    for (uint32_t i = 0; i < sl->more_count; ++i) {
        nxrt::Hook* m = s->overflow[sl->more_off + i];
        m->inc(shard);
        m->inl(c, addr, m->user);
    }
}

static int run_chain(Ctx* c, uint64_t addr, nxrt::HookKind kind) {
    const int k = static_cast<int>(kind);
    const size_t shard = nxrt::thread_shard();
    nxrt::g_site_shards[shard].calls[k].fetch_add(1, std::memory_order_relaxed);
    const nxrt::Snapshot* s = nxrt::g_snap.load(std::memory_order_acquire);
    if (!s) return 0;
    const nxrt::FlatSlot* sl = s->table[k].find(addr);
    if (!sl) return 0;
    nxrt::Hook* h = sl->first;
    h->inc(shard);
    if (h->site(c, addr, h->user)) return 1;
    for (uint32_t i = 0; i < sl->more_count; ++i) {
        nxrt::Hook* m = s->overflow[sl->more_off + i];
        m->inc(shard);
        if (m->site(c, addr, m->user)) return 1;
    }
    return 0;
}

int nx_site_midhook(Ctx* c, uint64_t addr) { return run_chain(c, addr, nxrt::HookKind::Mid); }
int nx_hook_entry(Ctx* c, uint64_t addr) { return run_chain(c, addr, nxrt::HookKind::Entry); }

}  // extern "C"
