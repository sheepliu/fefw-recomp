// SPDX-License-Identifier: GPL-3.0-or-later
// Guest address space on the host (layout and contract: include/nxrt/memory.h).
//  - fixed reservations at exact addresses (images, stacks, the kernel region), committed on demand;
//  - the heap: one pagefile-backed section (SEC_RESERVE) with a primary view at kHeapBase and alias views of the same
//    section pages, so one physical page can be seen at two guest addresses;
//  - a Horizon-style vm map kept beside the real mappings for svcQueryMemory.
#include "nxrt/game.h"
#include "nxrt/memory.h"

#include "nxrt/config.h"
#include "nxrt/diag.h"
#include "nxrt/gauge.h"
#include "nxrt/log.h"
#include "nxrt/write_watch.h"

#include <windows.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <functional>
#include <cstdio>
#include <map>
#include <mutex>
#include <string>
#include <vector>

namespace nxrt::mem {
namespace {

constexpr const char* kTag = "mem";
constexpr uint64_t kAddrLimit = 0x800000000000ull;  // user-mode address limit of x64 Windows (47 bits)
constexpr uint64_t kVmEnd = 0x8000000000ull;         // end of the address space reported by vm_query
constexpr int kAliasTries = 16;

uint64_t align_up(uint64_t v, uint64_t a) { return (v + a - 1) & ~(a - 1); }

// Whole pages covering [addr, addr+size). False when the range leaves the user address space.
bool page_span(uint64_t addr, uint64_t size, uint64_t* start, uint64_t* end) {
    if (size > kAddrLimit || addr > kAddrLimit - size) return false;
    *start = addr & ~(kPage - 1);
    *end = align_up(addr + size, kPage);
    return true;
}

DWORD protect_of(Prot p) {
    switch (p) {
        case Prot::R: return PAGE_READONLY;
        case Prot::RW: return PAGE_READWRITE;
        case Prot::RX: return PAGE_EXECUTE_READ;
        case Prot::RWX: return PAGE_EXECUTE_READWRITE;
        case Prot::None: break;
    }
    return PAGE_NOACCESS;
}

// ---- fixed reservations (guarded by g_fixed_mu) ----
struct Fixed {
    uint64_t size;
    std::string tag;
};
std::mutex g_fixed_mu;
std::map<uint64_t, Fixed> g_fixed;

// ---- module state ----
std::mutex g_state_mu;                    // init / shutdown
std::atomic<bool> g_inited{false};
std::atomic<uint64_t> g_default_heap{0};  // bytes, from Config or [memory] heap_gb

std::mutex g_kernel_mu;
uint64_t g_kernel_next = kKernelBase;     // guarded by g_kernel_mu

std::mutex g_vm_mu;
std::map<uint64_t, VmInfo> g_vm;          // start -> block, guarded by g_vm_mu

// ---- heap (guarded by g_heap_mu) ----
std::mutex g_heap_mu;
HANDLE g_section = nullptr;
uint64_t g_heap_size = 0;                 // 0 = no heap; otherwise the section size = size of the primary view
std::vector<uint64_t> g_commit_bits;      // one bit per 4 KiB page of the section, set by heap_commit
uint64_t g_committed = 0;                 // bytes of pages whose bit is set
// Per 4 KiB page of the section, what the host reports for it: 0 never committed, 1 committed but no-access (a decommitted page or one
// the guest made inaccessible), 2 committed and accessible but not writable, 3 committed read-write. VirtualQuery over the section view
// walks every page table entry of the run of identical pages (about 75 ms for a 3 GiB committed heap), so is_mapped, protect and the
// write tracker's arm (heap_rw_pages) answer heap ranges from this table. (4: kLazy below.)
std::vector<uint8_t> g_heap_cls;
// [memory] heap_lazy_commit: 4 = reserved for the guest heap by heap_commit but not committed yet. It answers as read-write (3) everywhere in
// this file; the pages are committed when first touched (lazy_fault) or before anything that needs them committed (protect, heap_rw_pages,
// alias and private views, heap_touch). On the console the game's heap gets its pages as the allocator uses them; without this, SetHeapSize
// commits the whole of it at boot.
constexpr uint8_t kLazy = 4;
std::atomic<bool> g_lazy{false};          // set by heap_create
std::atomic<uint64_t> g_lazy_faults{0}, g_lazy_bytes{0};
std::atomic<uint64_t> g_lazy_raced{0};    // faults on a page another thread committed first (the access ran again)
std::atomic<uint64_t> g_heap_epoch{0};    // bumped by every change of the table, inside the change's OsWriteScope
std::atomic<bool> g_wine_section_ok{false};  // set by the Vulkan back end after probing section-alias host imports under Wine
struct Alias {
    uint64_t size;
    uint64_t offset;                      // section offset of the first byte of the view
};
std::map<uint64_t, Alias> g_aliases;      // view address -> record
// Alias region in use: start -> size for every live view and for every candidate address the kernel refused.
std::map<uint64_t, uint64_t> g_alias_used;

// ---- gauges (include/nxrt/gauge.h); each callback takes the lock of the state it reads ----
// Removes its gauge when this file's statics are torn down (declared after them, so it goes first).
struct GaugeHook {
    const char* name;
    GaugeHook(const char* n, std::function<int64_t()> f) : name(n) { nxrt::gauge_register(n, std::move(f)); }
    ~GaugeHook() { nxrt::gauge_unregister(name); }
};
GaugeHook gauge_heap_reserved("mem.heap_reserved", [] { return static_cast<int64_t>(heap_reserved()); });   // section size
GaugeHook gauge_heap_committed("mem.heap_committed", [] { return static_cast<int64_t>(heap_committed()); });   // committed through heap_commit
GaugeHook gauge_aliases("mem.aliases", [] {   // heap alias views live (g_heap_mu)
    std::lock_guard<std::mutex> lk(g_heap_mu);
    return static_cast<int64_t>(g_aliases.size());
});
GaugeHook gauge_alias_used("mem.alias_used", [] {   // alias-region ranges in use, plus addresses the kernel refused (never freed: only grows)
    std::lock_guard<std::mutex> lk(g_heap_mu);
    return static_cast<int64_t>(g_alias_used.size());
});
GaugeHook gauge_fixed("mem.fixed_regions", [] {   // fixed reservations of this module (images, stacks, kernel region)
    std::lock_guard<std::mutex> lk(g_fixed_mu);
    return static_cast<int64_t>(g_fixed.size());
});
GaugeHook gauge_fixed_bytes("mem.fixed_bytes", [] {
    std::lock_guard<std::mutex> lk(g_fixed_mu);
    uint64_t n = 0;
    for (const auto& kv : g_fixed) n += kv.second.size;
    return static_cast<int64_t>(n);
});
GaugeHook gauge_vm_blocks("mem.vm_blocks", [] {   // blocks of the Horizon-style vm map kept for QueryMemory
    std::lock_guard<std::mutex> lk(g_vm_mu);
    return static_cast<int64_t>(g_vm.size());
});

// [memory] fast_query = false: ask the host (VirtualQuery) instead of the page table above; for before/after measurements.
bool fast_query() {
    static const bool v = config_get_bool("memory", "fast_query", true);
    return v;
}

// True when [start, end) lies inside the heap primary view (g_heap_mu held).
bool in_heap_view(uint64_t start, uint64_t end) {
    return fast_query() && g_heap_size && !g_heap_cls.empty() && start >= kHeapBase && end <= kHeapBase + g_heap_size && start < end;
}

// Page table reads and writes (g_heap_mu held except for lazy_try's unlocked pre-check). Every element access is atomic.
uint8_t get_cls(uint64_t p) { return std::atomic_ref<uint8_t>(g_heap_cls[p]).load(std::memory_order_relaxed); }
void set_cls(uint64_t first, uint64_t end, uint8_t cls) {
    for (uint64_t p = first; p < end; ++p) std::atomic_ref<uint8_t>(g_heap_cls[p]).store(cls, std::memory_order_relaxed);
}

bool bit_test(uint64_t page) { return (g_commit_bits[page / 64] >> (page % 64)) & 1; }
void bit_set(uint64_t page) { g_commit_bits[page / 64] |= uint64_t{1} << (page % 64); }
void bit_clear(uint64_t page) { g_commit_bits[page / 64] &= ~(uint64_t{1} << (page % 64)); }

// First address >= from at which `size` bytes are free in the alias region; 0 when there is none.
uint64_t find_free_alias(uint64_t from, uint64_t size) {
    const uint64_t limit = kAliasBase + kAliasSize;
    uint64_t cand = std::max(from, kAliasBase);
    for (;;) {
        if (cand >= limit || size > limit - cand) return 0;
        bool moved = false;
        auto it = g_alias_used.upper_bound(cand);
        if (it != g_alias_used.begin()) {
            auto p = std::prev(it);
            if (p->first + p->second > cand) {
                cand = align_up(p->first + p->second, kAllocGranularity);
                moved = true;
            }
        }
        if (!moved && it != g_alias_used.end() && it->first < cand + size) {
            cand = align_up(it->first + it->second, kAllocGranularity);
            moved = true;
        }
        if (!moved) return cand;
    }
}

// ---- out of memory ----
// The cause the crash report starts with when the system cannot commit the game's heap: the game answers with svcBreak, or the access that
// needed the page faults, and neither says why. The launcher shows this sentence after "The game stopped: " (launcher/src-tauri/src/
// memory.rs has the same one).
constexpr const char* kOutOfMemoryHint =
    "it ran out of memory. Close other programs and try again; on Windows, keep the page file on (system managed).";
bool g_oom_hinted = false;   // under g_heap_mu: the hint is heap_commit's to clear

// A commit refused for lack of commit charge. Windows grows a system-managed page file after such a failure and other processes give memory
// back, so it is worth trying again: kCommitRetries times, kCommitRetryMs apart.
bool out_of_commit(DWORD e) { return e == ERROR_COMMITMENT_LIMIT || e == ERROR_NOT_ENOUGH_MEMORY || e == ERROR_COMMITMENT_MINIMUM; }
constexpr int kCommitRetries = 40;
constexpr DWORD kCommitRetryMs = 250;
uint64_t g_lazy_gave_up_ms = 0;   // under g_heap_mu: when a first-use commit last failed after all its waiting (0: never)

// ---- [memory] heap_lazy_commit ----
// Commits the kLazy pages among [p0, p1) (page numbers of the section; lk holds g_heap_mu). A commit refused for lack of memory is tried
// again as heap_commit does, with the lock released meanwhile: the pages are looked at again after each wait. False if one could not be
// committed.
bool lazy_commit(std::unique_lock<std::mutex>& lk, uint64_t p0, uint64_t p1) {
    if (!g_lazy.load(std::memory_order_relaxed)) return true;
    int attempt = 0;
    for (uint64_t p = p0; p < std::min<uint64_t>(p1, g_heap_cls.size());) {
        if (get_cls(p) != kLazy) { ++p; continue; }
        const uint64_t end = std::min<uint64_t>(p1, g_heap_cls.size());
        uint64_t q = p;
        while (q < end && get_cls(q) == kLazy) ++q;
        if (!VirtualAlloc(reinterpret_cast<void*>(kHeapBase + p * kPage), static_cast<SIZE_T>((q - p) * kPage), MEM_COMMIT, PAGE_READWRITE)) {
            const DWORD e = GetLastError();
            // Once a whole wait has failed, later calls do not wait again for a while: the same fault asks twice (the write tracker's handler,
            // then lazy_fault) before it reaches the crash handler.
            const bool gave_up = g_lazy_gave_up_ms && GetTickCount64() - g_lazy_gave_up_ms < uint64_t{kCommitRetries} * kCommitRetryMs;
            if (out_of_commit(e) && !gave_up && attempt < kCommitRetries) {
                if (attempt++ == 0)
                    NXLOG_W(kTag, "first use of heap pages [%llx, +%llx): the system is out of commit (error %lu); waiting up to 10 s for the page file to grow",
                            static_cast<unsigned long long>(p * kPage), static_cast<unsigned long long>((q - p) * kPage), static_cast<unsigned long>(e));
                lk.unlock();
                Sleep(kCommitRetryMs);
                lk.lock();
                if (!g_heap_size) return false;   // the heap went away meanwhile
                continue;
            }
            NXLOG_E(kTag, "lazy commit [%llx, +%llx): VirtualAlloc failed, error %lu", static_cast<unsigned long long>(p * kPage),
                    static_cast<unsigned long long>((q - p) * kPage), static_cast<unsigned long>(e));
            if (out_of_commit(e)) {
                g_lazy_gave_up_ms = GetTickCount64();
                crash_set_memory_hint(kOutOfMemoryHint);   // stays: another thread's smaller commit working says nothing about this one
            }
            return false;
        }
        set_cls(p, q, 3);   // the same answer as kLazy: no epoch change
        for (uint64_t r = p; r < q; ++r)
            if (!bit_test(r)) { bit_set(r); g_committed += kPage; }
        g_lazy_bytes.fetch_add((q - p) * kPage, std::memory_order_relaxed);
        p = q;
    }
    return true;
}

// First touch of a kLazy page from any thread: commit its 2 MiB granule and run the access again. Nothing in this file touches guest memory
// while holding g_heap_mu, so taking it here cannot deadlock on this thread. True when the access can run again.
bool lazy_try(uint64_t a) {
    // acquire: with g_lazy seen set, so is the table lazy_setup was called after, which keeps its size while g_lazy stays set
    if (!g_lazy.load(std::memory_order_acquire) || a < kHeapBase || a - kHeapBase >= kHeapMax) return false;
    const uint64_t p = (a - kHeapBase) / kPage;
    // the write tracker's faults come here first: a quick look without the lock
    if (p >= g_heap_cls.size() || get_cls(p) != kLazy) return false;
    std::unique_lock lk(g_heap_mu);
    if (a - kHeapBase >= g_heap_size) return false;
    // Another thread's first touch committed the page while this one waited for the lock: the access can run again. (Once only: the retried
    // access finds the page committed, and a fault on it after that is not a lazy one.)
    if (get_cls(p) == 3 && bit_test(p)) {
        g_lazy_raced.fetch_add(1, std::memory_order_relaxed);
        return true;
    }
    if (get_cls(p) != kLazy) return false;   // a real fault (or the write tracker's): not ours
    constexpr uint64_t kGranPages = (2ull << 20) / kPage;
    const uint64_t first = p & ~(kGranPages - 1);
    if (!lazy_commit(lk, first, first + kGranPages)) return false;
    g_lazy_faults.fetch_add(1, std::memory_order_relaxed);
    return true;
}

// The vectored handler. Until the write tracker installs its own (which asks lazy_try first) this is the only one, and a thread can fault on a
// page another thread committed before this thread's quick look found it no longer lazy. So a fault on a committed heap page whose protection
// now allows the access runs again.
bool lazy_fault_handled(uint64_t kind, uint64_t a) {
    if (lazy_try(a)) return true;
    if ((kind != 0 && kind != 1) || a < kHeapBase || a - kHeapBase >= kHeapMax) return false;
    {
        std::lock_guard lk(g_heap_mu);
        const uint64_t p = (a - kHeapBase) / kPage;
        if (a - kHeapBase >= g_heap_size || get_cls(p) != 3 || !bit_test(p)) return false;
    }
    // Bounded per thread and page, as the write tracker's late-write retry: a fault that keeps coming back still reaches the crash handler.
    thread_local uint64_t t_page = 0;
    thread_local int t_count = 0;
    const uint64_t pg = a & ~(kPage - 1);
    if (pg != t_page) t_page = pg, t_count = 0;
    if (++t_count > 8) return false;
    MEMORY_BASIC_INFORMATION mbi{};
    if (VirtualQuery(reinterpret_cast<void*>(a), &mbi, sizeof mbi) != sizeof mbi || mbi.State != MEM_COMMIT || (mbi.Protect & PAGE_GUARD))
        return false;
    const DWORD pr = mbi.Protect & 0xff;
    const bool ok = pr == PAGE_READWRITE || pr == PAGE_EXECUTE_READWRITE || (kind == 0 && (pr == PAGE_READONLY || pr == PAGE_EXECUTE_READ));
    if (ok) g_lazy_raced.fetch_add(1, std::memory_order_relaxed);
    return ok;
}

LONG CALLBACK lazy_fault(EXCEPTION_POINTERS* ep) {
    if (!g_lazy.load(std::memory_order_relaxed) || ep->ExceptionRecord->ExceptionCode != EXCEPTION_ACCESS_VIOLATION ||
        ep->ExceptionRecord->NumberParameters < 2)
        return EXCEPTION_CONTINUE_SEARCH;
    const DWORD last = GetLastError();   // the interrupted code may be about to read it
    const bool ok = lazy_fault_handled(ep->ExceptionRecord->ExceptionInformation[0], ep->ExceptionRecord->ExceptionInformation[1]);
    SetLastError(last);
    return ok ? EXCEPTION_CONTINUE_EXECUTION : EXCEPTION_CONTINUE_SEARCH;
}

// [memory] heap_lazy_commit (default off), at heap_create (g_heap_mu held): heap_commit only marks its pages, and they are committed at their
// first use. `faults`: an access to an uncommitted page of this heap faults. The page table is what knows the pages, so not with [memory]
// fast_query = false.
void lazy_setup(bool faults) {
    g_lazy = false;
    if (!nxrt::config_get_bool("memory", "heap_lazy_commit", false)) return;
    static void* const veh = AddVectoredExceptionHandler(1, lazy_fault);
    const char* no = !faults ? "Wine does not fault on a section's uncommitted pages"
                     : !fast_query() ? "[memory] fast_query = false"
                     : !veh ? "AddVectoredExceptionHandler failed" : nullptr;
    if (no) {
        NXLOG_W(kTag, "heap_lazy_commit: %s: the heap is committed whole", no);
        return;
    }
    g_lazy = true;
    NXLOG_I(kTag, "heap: pages are committed at their first use ([memory] heap_lazy_commit)");
}

GaugeHook gauge_lazy_faults("mem.lazy_faults", [] { return static_cast<int64_t>(g_lazy_faults.load(std::memory_order_relaxed)); });
GaugeHook gauge_lazy_raced("mem.lazy_raced", [] { return static_cast<int64_t>(g_lazy_raced.load(std::memory_order_relaxed)); });
GaugeHook gauge_lazy_mb("mem.lazy_committed_mb", [] { return static_cast<int64_t>(g_lazy_bytes.load(std::memory_order_relaxed) >> 20); });

}  // namespace

// ---------------------------------------------------------------------------------------------------------------------
// init / shutdown

bool init(const Config& cfg) {
    std::lock_guard lk(g_state_mu);
    if (g_inited.load()) return true;

    uint64_t heap = cfg.heap_reserve;
    if (heap == 0) {
        int64_t gb = nxrt::config_get_int("memory", "heap_gb", nxrt::game::info().platform == nxrt::game::Platform::Switch2 ? 12 : 6);
        if (gb < 1 || gb > 1024) {
            NXLOG_W(kTag, "[memory] heap_gb=%lld is out of range: using 6", static_cast<long long>(gb));
            gb = 6;
        }
        heap = static_cast<uint64_t>(gb) << 30;
    }
    g_default_heap.store(align_up(heap, kAllocGranularity));

    if (cfg.reserve_regions) {
        if (!reserve_fixed(kStackBase, kStackSize, "stack")) {
            NXLOG_E(kTag, "init failed: the stack region could not be reserved");
            return false;
        }
        if (!reserve_fixed(kKernelBase, kKernelSize, "kernel")) {
            NXLOG_E(kTag, "init failed: the kernel region could not be reserved");
            release_fixed(kStackBase);
            return false;
        }
    }
    {
        std::lock_guard kl(g_kernel_mu);
        g_kernel_next = kKernelBase;
    }
    g_inited.store(true);
    NXLOG_I(kTag, "init: stack %llx (+%llx), kernel %llx (+%llx), default heap %llu MiB, regions %s",
            static_cast<unsigned long long>(kStackBase), static_cast<unsigned long long>(kStackSize),
            static_cast<unsigned long long>(kKernelBase), static_cast<unsigned long long>(kKernelSize),
            static_cast<unsigned long long>(g_default_heap.load() >> 20), cfg.reserve_regions ? "reserved" : "skipped");
    return true;
}

void shutdown() {
    std::lock_guard lk(g_state_mu);
    {
        std::lock_guard hl(g_heap_mu);
        for (auto& [view, rec] : g_aliases) {
            (void)rec;
            if (!UnmapViewOfFile(reinterpret_cast<void*>(view))) {
                DWORD e = GetLastError();
                NXLOG_E(kTag, "shutdown: UnmapViewOfFile(alias %llx) failed, error %lu",
                        static_cast<unsigned long long>(view), static_cast<unsigned long>(e));
            }
        }
        g_aliases.clear();
        g_alias_used.clear();
        if (g_heap_size && !g_section) {
            VirtualFree(reinterpret_cast<void*>(kHeapBase), 0, MEM_RELEASE);   // the private heap (heap_create)
        } else if (g_heap_size) {
            if (!UnmapViewOfFile(reinterpret_cast<void*>(kHeapBase))) {
                DWORD e = GetLastError();
                NXLOG_E(kTag, "shutdown: UnmapViewOfFile(heap) failed, error %lu", static_cast<unsigned long>(e));
            }
        }
        if (g_section) CloseHandle(g_section);
        g_section = nullptr;
        g_heap_size = 0;
        g_lazy = false;   // before the table goes
        g_commit_bits.clear();
        g_heap_cls.clear();
        g_committed = 0;
        g_lazy_gave_up_ms = 0;
    }
    {
        std::lock_guard fl(g_fixed_mu);
        for (auto& [addr, f] : g_fixed) {
            (void)f;
            if (!VirtualFree(reinterpret_cast<void*>(addr), 0, MEM_RELEASE)) {
                DWORD e = GetLastError();
                NXLOG_E(kTag, "shutdown: VirtualFree(%llx) failed, error %lu", static_cast<unsigned long long>(addr),
                        static_cast<unsigned long>(e));
            }
        }
        g_fixed.clear();
    }
    {
        std::lock_guard vl(g_vm_mu);
        g_vm.clear();
    }
    {
        std::lock_guard kl(g_kernel_mu);
        g_kernel_next = kKernelBase;
    }
    g_default_heap.store(0);
    g_inited.store(false);
    NXLOG_I(kTag, "shutdown: all mappings of this module released");
}

bool initialised() { return g_inited.load(); }

// ---------------------------------------------------------------------------------------------------------------------
// fixed mappings

bool reserve_fixed(uint64_t addr, uint64_t size, const char* tag) {
    if (size == 0 || addr % kAllocGranularity != 0 || addr >= kAddrLimit || size > kAddrLimit - addr) {
        NXLOG_E(kTag, "reserve %llx (+%llx) %s: address must be 64 KiB aligned and inside the user address space",
                static_cast<unsigned long long>(addr), static_cast<unsigned long long>(size), tag ? tag : "");
        return false;
    }
    size = align_up(size, kPage);
    std::lock_guard lk(g_fixed_mu);
    void* p = VirtualAlloc(reinterpret_cast<void*>(addr), static_cast<SIZE_T>(size), MEM_RESERVE, PAGE_NOACCESS);
    if (!p) {
        DWORD e = GetLastError();
        NXLOG_E(kTag, "reserve %llx (+%llx) %s: VirtualAlloc failed, error %lu",
                static_cast<unsigned long long>(addr), static_cast<unsigned long long>(size), tag ? tag : "",
                static_cast<unsigned long>(e));
        return false;
    }
    if (reinterpret_cast<uint64_t>(p) != addr) {  // cannot happen with MEM_RESERVE at a fixed address; never accept it
        VirtualFree(p, 0, MEM_RELEASE);
        NXLOG_E(kTag, "reserve %llx %s: got %p instead", static_cast<unsigned long long>(addr), tag ? tag : "", p);
        return false;
    }
    g_fixed[addr] = Fixed{size, tag ? tag : ""};
    NXLOG_D(kTag, "reserved %llx (+%llx) %s", static_cast<unsigned long long>(addr),
            static_cast<unsigned long long>(size), tag ? tag : "");
    return true;
}

bool release_fixed(uint64_t addr) {
    std::lock_guard lk(g_fixed_mu);
    auto it = g_fixed.find(addr);
    if (it == g_fixed.end()) {
        NXLOG_W(kTag, "release %llx: no reservation of this module starts there", static_cast<unsigned long long>(addr));
        return false;
    }
    if (!VirtualFree(reinterpret_cast<void*>(addr), 0, MEM_RELEASE)) {
        DWORD e = GetLastError();
        NXLOG_E(kTag, "release %llx: VirtualFree failed, error %lu", static_cast<unsigned long long>(addr),
                static_cast<unsigned long>(e));
        return false;
    }
    g_fixed.erase(it);
    return true;
}

bool commit(uint64_t addr, uint64_t size, Prot p) {
    uint64_t start = 0, end = 0;
    if (!page_span(addr, size, &start, &end)) return false;
    if (start == end) return true;
    const ww::OsWriteScope wws(reinterpret_cast<void*>(start), end - start);   // the write tracker keeps out of the range while it changes
    if (!VirtualAlloc(reinterpret_cast<void*>(start), static_cast<SIZE_T>(end - start), MEM_COMMIT, protect_of(p))) {
        DWORD e = GetLastError();
        NXLOG_E(kTag, "commit %llx (+%llx): VirtualAlloc failed, error %lu", static_cast<unsigned long long>(start),
                static_cast<unsigned long long>(end - start), static_cast<unsigned long>(e));
        return false;
    }
    return true;
}

bool decommit(uint64_t addr, uint64_t size) {
    uint64_t start = 0, end = 0;
    if (!page_span(addr, size, &start, &end)) return false;
    if (start == end) return true;
    const ww::OsWriteScope wws(reinterpret_cast<void*>(start), end - start);
    if (!VirtualFree(reinterpret_cast<void*>(start), static_cast<SIZE_T>(end - start), MEM_DECOMMIT)) {
        DWORD e = GetLastError();
        NXLOG_E(kTag, "decommit %llx (+%llx): VirtualFree failed, error %lu", static_cast<unsigned long long>(start),
                static_cast<unsigned long long>(end - start), static_cast<unsigned long>(e));
        return false;
    }
    return true;
}

bool protect(uint64_t addr, uint64_t size, Prot p) {
    uint64_t start = 0, end = 0;
    if (!page_span(addr, size, &start, &end)) return false;
    if (start == end) return true;
    const DWORD np = protect_of(p);
    const ww::OsWriteScope wws(reinterpret_cast<void*>(start), end - start);   // pages the write tracker armed get this protection, not its own
    {   // heap: the page table says whether every page is committed, so one VirtualProtect does it (no VirtualQuery over the view)
        std::unique_lock hk(g_heap_mu);
        if (in_heap_view(start, end)) {
            const uint64_t p0 = (start - kHeapBase) / kPage, p1 = (end - kHeapBase) / kPage;
            bool all = true;
            for (uint64_t q = p0; q < p1 && all; ++q) all = get_cls(q) != 0;
            if (all) all = lazy_commit(hk, p0, p1) && in_heap_view(start, end);   // VirtualProtect refuses uncommitted pages
            DWORD old = 0;
            if (all && VirtualProtect(reinterpret_cast<void*>(start), static_cast<SIZE_T>(end - start), np, &old)) {
                set_cls(p0, p1, np == PAGE_NOACCESS ? uint8_t{1} : np == PAGE_READWRITE ? uint8_t{3} : uint8_t{2});
                g_heap_epoch.fetch_add(1, std::memory_order_seq_cst);
                return true;
            }
        }
    }
    // Pass 1: every page must be committed, so a failure never leaves the range half changed.
    for (uint64_t cur = start; cur < end;) {
        MEMORY_BASIC_INFORMATION mbi{};
        if (VirtualQuery(reinterpret_cast<void*>(cur), &mbi, sizeof mbi) != sizeof mbi || mbi.RegionSize == 0) {
            NXLOG_E(kTag, "protect %llx: VirtualQuery failed, error %lu", static_cast<unsigned long long>(cur),
                    static_cast<unsigned long>(GetLastError()));
            return false;
        }
        if (mbi.State != MEM_COMMIT) {
            NXLOG_E(kTag, "protect %llx: page is not committed", static_cast<unsigned long long>(cur));
            return false;
        }
        cur = std::min<uint64_t>(end, reinterpret_cast<uint64_t>(mbi.BaseAddress) + mbi.RegionSize);
    }
    // Pass 2: region by region (a range can span several allocations, each has its own region).
    g_heap_epoch.fetch_add(1, std::memory_order_seq_cst);
    for (uint64_t cur = start; cur < end;) {
        MEMORY_BASIC_INFORMATION mbi{};
        VirtualQuery(reinterpret_cast<void*>(cur), &mbi, sizeof mbi);
        const uint64_t chunk_end = std::min<uint64_t>(end, reinterpret_cast<uint64_t>(mbi.BaseAddress) + mbi.RegionSize);
        DWORD old = 0;
        if (!VirtualProtect(reinterpret_cast<void*>(cur), static_cast<SIZE_T>(chunk_end - cur), np, &old)) {
            NXLOG_E(kTag, "protect %llx (+%llx): VirtualProtect failed, error %lu", static_cast<unsigned long long>(cur),
                    static_cast<unsigned long long>(chunk_end - cur), static_cast<unsigned long>(GetLastError()));
            return false;
        }
        cur = chunk_end;
    }
    return true;
}

uint64_t alloc_kernel(uint64_t size, uint64_t align) {
    if (!initialised() && !init()) return 0;
    if (align == 0) align = kPage;
    if (align & (align - 1)) {
        NXLOG_E(kTag, "alloc_kernel: alignment %llx is not a power of two", static_cast<unsigned long long>(align));
        return 0;
    }
    align = std::max(align, kPage);
    if (size == 0) size = 1;
    if (size > kKernelSize) return 0;
    size = align_up(size, kPage);

    std::lock_guard lk(g_kernel_mu);
    const uint64_t a = align_up(g_kernel_next, align);
    if (a < g_kernel_next || a > kKernelBase + kKernelSize || size > kKernelBase + kKernelSize - a) {
        NXLOG_E(kTag, "alloc_kernel: kernel region exhausted (%llx used of %llx)",
                static_cast<unsigned long long>(g_kernel_next - kKernelBase),
                static_cast<unsigned long long>(kKernelSize));
        return 0;
    }
    if (!commit(a, size, Prot::RW)) return 0;
    g_kernel_next = a + size;
    return a;
}

bool is_mapped(uint64_t addr, uint64_t size) {
    uint64_t start = 0, end = 0;
    if (!page_span(addr, size, &start, &end)) return false;
    {
        std::lock_guard hk(g_heap_mu);
        if (in_heap_view(start, end)) {
            const uint64_t p0 = (start - kHeapBase) / kPage, p1 = (end - kHeapBase) / kPage;
            for (uint64_t q = p0; q < p1; ++q)
                if (get_cls(q) < 2) return false;
            return true;
        }
    }
    for (uint64_t cur = start; cur < end;) {
        MEMORY_BASIC_INFORMATION mbi{};
        if (VirtualQuery(reinterpret_cast<void*>(cur), &mbi, sizeof mbi) != sizeof mbi || mbi.RegionSize == 0) return false;
        if (mbi.State != MEM_COMMIT) return false;
        if ((mbi.Protect & 0xFF) == PAGE_NOACCESS || (mbi.Protect & PAGE_GUARD)) return false;
        cur = std::min<uint64_t>(end, reinterpret_cast<uint64_t>(mbi.BaseAddress) + mbi.RegionSize);
    }
    return true;
}

// ---------------------------------------------------------------------------------------------------------------------
// heap section

void set_wine_section_supported(bool ok) { g_wine_section_ok.store(ok, std::memory_order_release); }
bool wine_section_supported() { return g_wine_section_ok.load(std::memory_order_acquire); }

bool heap_create(uint64_t reserve_bytes) {
    if (!initialised() && !init()) return false;
    std::lock_guard lk(g_heap_mu);
    if (g_heap_size) return true;  // a heap already exists: nothing to do

    const uint64_t want = reserve_bytes ? reserve_bytes : g_default_heap.load();
    if (want == 0) {
        NXLOG_E(kTag, "heap_create: no size given and no configured default");
        return false;
    }
    const uint64_t size = align_up(want, kAllocGranularity);
    if (size > kHeapMax) {
        NXLOG_E(kTag, "heap_create: %llu bytes exceed the heap region limit of %llu bytes",
                static_cast<unsigned long long>(size), static_cast<unsigned long long>(kHeapMax));
        return false;
    }
    // [memory] heap_section: auto (default) = a section on Windows, or under Wine when the GPU driver passed the section-import
    // probe (memory.h); 1 = always, 0 = never. true / on / yes and false / off / no work too.
    std::string mode = nxrt::config_get_str("memory", "heap_section", "auto");
    for (char& ch : mode) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    if (mode == "false" || mode == "off" || mode == "no") mode = "0";
    if (mode == "true" || mode == "on" || mode == "yes") mode = "1";
    if (mode != "0" && mode != "1" && mode != "auto" && !mode.empty())
        NXLOG_W(kTag, "[memory] heap_section = %s is not auto, 0 or 1: auto is used", mode.c_str());
    const HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    const bool wine = ntdll && GetProcAddress(ntdll, "wine_get_version");
    const bool wine_ok = wine && g_wine_section_ok.load(std::memory_order_acquire);
    if (mode == "0" || (mode != "1" && wine && !wine_ok)) {
        void* p = VirtualAlloc(reinterpret_cast<void*>(kHeapBase), static_cast<SIZE_T>(size), MEM_RESERVE, PAGE_NOACCESS);
        if (reinterpret_cast<uint64_t>(p) != kHeapBase) {
            NXLOG_E(kTag, "heap_create: VirtualAlloc(MEM_RESERVE %llu MiB) at %llx failed, error %lu", static_cast<unsigned long long>(size >> 20),
                    static_cast<unsigned long long>(kHeapBase), static_cast<unsigned long>(GetLastError()));
            if (p) VirtualFree(p, 0, MEM_RELEASE);
            return false;
        }
        g_heap_size = size;
        g_commit_bits.assign((size / kPage + 63) / 64, 0);
        g_heap_cls.assign(static_cast<size_t>(size / kPage), 0);
        g_committed = 0;
        lazy_setup(true);
        NXLOG_I(kTag, "heap: private memory of %llu MiB at %llx, no section (%s): no alias views, pools import at their own address",
                static_cast<unsigned long long>(size >> 20), static_cast<unsigned long long>(kHeapBase),
                mode == "0" ? "[memory] heap_section = 0" : "Wine, GPU cannot import section views");
        return true;
    }
    HANDLE sec = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE | SEC_RESERVE,
                                    static_cast<DWORD>(size >> 32), static_cast<DWORD>(size & 0xFFFFFFFFu), nullptr);
    if (!sec) {
        DWORD e = GetLastError();
        NXLOG_E(kTag, "heap_create: CreateFileMapping(%llu MiB) failed, error %lu",
                static_cast<unsigned long long>(size >> 20), static_cast<unsigned long>(e));
        return false;
    }
    void* v = MapViewOfFileEx(sec, FILE_MAP_ALL_ACCESS, 0, 0, static_cast<SIZE_T>(size),
                              reinterpret_cast<void*>(kHeapBase));
    if (!v) {
        DWORD e = GetLastError();
        NXLOG_E(kTag, "heap_create: MapViewOfFileEx at %llx failed, error %lu",
                static_cast<unsigned long long>(kHeapBase), static_cast<unsigned long>(e));
        CloseHandle(sec);
        return false;
    }
    if (reinterpret_cast<uint64_t>(v) != kHeapBase) {
        NXLOG_E(kTag, "heap_create: primary view landed at %p, not %llx", v, static_cast<unsigned long long>(kHeapBase));
        UnmapViewOfFile(v);
        CloseHandle(sec);
        return false;
    }
    g_section = sec;
    g_heap_size = size;
    g_commit_bits.assign((size / kPage + 63) / 64, 0);
    g_heap_cls.assign(static_cast<size_t>(size / kPage), 0);
    g_committed = 0;
    lazy_setup(!wine);   // Wine lets an access to a section's uncommitted page through: nothing would say it was first used
    NXLOG_I(kTag, "heap: section of %llu MiB, primary view at %llx%s", static_cast<unsigned long long>(size >> 20),
            static_cast<unsigned long long>(kHeapBase), wine ? (mode == "1" ? " (Wine, forced)" : " (Wine, probed)") : "");
    return true;
}

bool heap_exists() {
    std::lock_guard lk(g_heap_mu);
    return g_heap_size != 0;
}

bool heap_has_section() {
    std::lock_guard lk(g_heap_mu);
    return g_section != nullptr;
}

uint64_t heap_reserved() {
    std::lock_guard lk(g_heap_mu);
    return g_heap_size;
}

// Lock order: the write tracker's scope is taken before g_heap_mu, as protect() does. The scope waits for an ImportFreeze over the range,
// and an ImportFreeze holds its slot while it reads the heap's page table (heap_rw_pages, under g_heap_mu): waiting with the lock held,
// neither would end. The commit-limit retries sleep without the lock too, so the page table stays readable for those seconds.
// [memory] heap_lazy_commit: pages never committed are only marked here (nothing about their protection changes, so no scope).
bool heap_commit(uint64_t offset, uint64_t size) {
    uint64_t start = 0, end = 0;
    {
        std::lock_guard lk(g_heap_mu);
        if (!g_heap_size) {
            NXLOG_W(kTag, "heap_commit: no heap");
            return false;
        }
        if (size == 0) return true;
        if (offset > g_heap_size || size > g_heap_size - offset) {
            NXLOG_E(kTag, "heap_commit: [%llx, +%llx) is outside the %llu MiB section",
                    static_cast<unsigned long long>(offset), static_cast<unsigned long long>(size),
                    static_cast<unsigned long long>(g_heap_size >> 20));
            return false;
        }
        start = offset & ~(kPage - 1);
        end = align_up(offset + size, kPage);
        if (g_lazy) {
            // a range with a page committed before (or decommitted: no-access now) is committed whole here, as without the switch
            bool all_new = true;
            for (uint64_t p = start / kPage; p < end / kPage && all_new; ++p) all_new = get_cls(p) == 0 || get_cls(p) == kLazy;
            if (all_new) {
                set_cls(start / kPage, end / kPage, kLazy);
                g_heap_epoch.fetch_add(1, std::memory_order_seq_cst);
                return true;
            }
        }
    }
    const ww::OsWriteScope wws(reinterpret_cast<void*>(kHeapBase + start), end - start);   // MEM_COMMIT makes committed pages read-write again
    std::unique_lock lk(g_heap_mu);
    if (!g_heap_size || end > g_heap_size) return false;   // the heap went away meanwhile
    // The game sets a heap of about 9 GB at boot and all of it is committed here. When the system's commit charge is near its limit (just after
    // the launcher's setup, or with a small page file) this fails, the game turns the result into nn::os OutOfMemory (0x1003) and calls svcBreak.
    // out_of_commit (above): wait a little and try again.
    bool ok = VirtualAlloc(reinterpret_cast<void*>(kHeapBase + start), static_cast<SIZE_T>(end - start), MEM_COMMIT, PAGE_READWRITE) != nullptr;
    DWORD e = ok ? 0 : GetLastError();
    for (int attempt = 1; !ok && attempt <= kCommitRetries; ++attempt) {
        if (!out_of_commit(e)) break;
        if (attempt == 1)
            NXLOG_W(kTag, "heap_commit [%llx, +%llx): the system is out of commit (error %lu); waiting up to 10 s for the page file to grow",
                    static_cast<unsigned long long>(start), static_cast<unsigned long long>(end - start), static_cast<unsigned long>(e));
        lk.unlock();
        Sleep(kCommitRetryMs);
        lk.lock();
        if (!g_heap_size || end > g_heap_size) return false;
        ok = VirtualAlloc(reinterpret_cast<void*>(kHeapBase + start), static_cast<SIZE_T>(end - start), MEM_COMMIT, PAGE_READWRITE) != nullptr;
        e = ok ? 0 : GetLastError();
        if (ok) NXLOG_I(kTag, "heap_commit: committed after %d retries", attempt);
    }
    if (!ok) {
        NXLOG_E(kTag, "heap_commit [%llx, +%llx): VirtualAlloc failed, error %lu",
                static_cast<unsigned long long>(start), static_cast<unsigned long long>(end - start),
                static_cast<unsigned long>(e));
        if (out_of_commit(e)) {
            crash_set_memory_hint(kOutOfMemoryHint);
            g_oom_hinted = true;
        }
        return false;
    }
    if (g_oom_hinted) {         // the game asked again and got it: whatever ends the run now has another cause
        crash_set_memory_hint("");
        g_oom_hinted = false;
    }
    for (uint64_t p = start / kPage; p < end / kPage; p++) {
        if (!bit_test(p)) {
            bit_set(p);
            g_committed += kPage;
        }
        set_cls(p, p + 1, 3);    // MEM_COMMIT with PAGE_READWRITE applies to pages that were committed already too
    }
    g_heap_epoch.fetch_add(1, std::memory_order_seq_cst);
    return true;
}

bool heap_decommit(uint64_t offset, uint64_t size) {
    uint64_t start = 0, end = 0;
    {
        std::lock_guard lk(g_heap_mu);
        if (!g_heap_size) return false;
        if (size == 0) return true;
        if (offset > g_heap_size || size > g_heap_size - offset) {
            NXLOG_E(kTag, "heap_decommit: [%llx, +%llx) is outside the section", static_cast<unsigned long long>(offset),
                    static_cast<unsigned long long>(size));
            return false;
        }
        start = offset & ~(kPage - 1);
        end = align_up(offset + size, kPage);
    }
    const ww::OsWriteScope wws(reinterpret_cast<void*>(kHeapBase + start), end - start);   // before g_heap_mu, as in heap_commit
    std::lock_guard lk(g_heap_mu);
    if (!g_heap_size || end > g_heap_size) return false;   // the heap went away meanwhile
    // VirtualFree(MEM_DECOMMIT) is refused on pages of a section view (ERROR_INVALID_PARAMETER), so the pages are made
    // PAGE_NOACCESS through the primary view instead: guest accesses fault, the host pages stay charged to the section
    // (as in smo-recomp's SetMemoryHeapSize), and the alias views still show the section's contents.
    for (uint64_t p = start / kPage; p < end / kPage;) {
        if (!bit_test(p)) {
            if (get_cls(p) == kLazy) {   // never used: back to reserved
                set_cls(p, p + 1, 0);
                g_heap_epoch.fetch_add(1, std::memory_order_seq_cst);
            }
            p++;
            continue;
        }
        uint64_t run = p;
        while (run < end / kPage && bit_test(run)) run++;
        DWORD old = 0;
        if (!VirtualProtect(reinterpret_cast<void*>(kHeapBase + p * kPage), static_cast<SIZE_T>((run - p) * kPage),
                            PAGE_NOACCESS, &old)) {
            DWORD e = GetLastError();
            NXLOG_E(kTag, "heap_decommit [%llx, +%llx): VirtualProtect failed, error %lu",
                    static_cast<unsigned long long>(p * kPage), static_cast<unsigned long long>((run - p) * kPage),
                    static_cast<unsigned long>(e));
            return false;
        }
        for (uint64_t q = p; q < run; q++) {
            bit_clear(q);
            g_committed -= kPage;
            set_cls(q, q + 1, 1);
        }
        g_heap_epoch.fetch_add(1, std::memory_order_seq_cst);
        p = run;
    }
    return true;
}

int heap_rw_pages(uint64_t addr, uint64_t size) {
    uint64_t start = 0, end = 0;
    if (!page_span(addr, size, &start, &end)) return 0;
    std::unique_lock lk(g_heap_mu);
    if (!in_heap_view(start, end)) return -1;
    const uint64_t p0 = (start - kHeapBase) / kPage, p1 = (end - kHeapBase) / kPage;
    bool lazy = false;
    for (uint64_t q = p0; q < p1; ++q) {
        if (get_cls(q) == kLazy) lazy = true;
        else if (get_cls(q) != 3) return 0;
    }
    if (!lazy) return 1;
    // the callers import or protect the range: it must be committed. The commit may let the lock go, so look again after it.
    if (!lazy_commit(lk, p0, p1) || !in_heap_view(start, end)) return 0;
    for (uint64_t q = p0; q < p1; ++q)
        if (get_cls(q) != 3) return 0;
    return 1;
}

bool heap_lazy_fault(uint64_t addr) {
    const DWORD last = GetLastError();
    const bool ok = lazy_try(addr);
    SetLastError(last);
    return ok;
}

bool heap_touch(uint64_t addr, uint64_t size) {
    uint64_t start = 0, end = 0;
    if (!g_lazy.load(std::memory_order_relaxed) || !size || !page_span(addr, size, &start, &end)) return true;
    std::unique_lock lk(g_heap_mu);
    if (!in_heap_view(start, end)) return true;
    return lazy_commit(lk, (start - kHeapBase) / kPage, (end - kHeapBase) / kPage);
}

uint64_t heap_epoch() { return g_heap_epoch.load(std::memory_order_seq_cst); }

uint64_t limits_ram_bytes() {
    static const uint64_t r = [] {
        const int64_t test_gb = config_get_int("memory", "test_ram_gb", 0);
        if (test_gb > 0) return static_cast<uint64_t>(test_gb) << 30;
        MEMORYSTATUSEX ms{};
        ms.dwLength = sizeof ms;
        return GlobalMemoryStatusEx(&ms) ? static_cast<uint64_t>(ms.ullTotalPhys) : uint64_t{0};
    }();
    return r;
}

std::atomic<uint64_t> g_limits_video_heap{0};   // the largest device-local heap, from the graphics device (0 until it exists)

void limits_note_video_heap(uint64_t bytes) { g_limits_video_heap.store(bytes, std::memory_order_relaxed); }
uint64_t limits_video_heap_bytes() { return g_limits_video_heap.load(std::memory_order_relaxed); }

// [memory] scale_limits = auto (default) | true | false. auto: on for a PC with 20 GiB of RAM or less (the 8 and 16 GB classes) or a graphics
// card whose largest device-local heap is 8.5 GiB or less (8 GB cards report 7.8 to 8.0); off above both. Decided at the first call after the
// device exists (the RAM alone before it), and logged once with the reason.
bool limits_scaled() {
    static const std::string mode = [] {
        std::string v = config_get_str("memory", "scale_limits", "auto");
        for (char& ch : v) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
        return v;
    }();
    if (mode != "auto" && mode != "") {
        static const bool v = config_get_bool("memory", "scale_limits", false);
        static std::atomic<bool> logged{false};
        if (!logged.exchange(true))
            NXLOG_I(kTag, "memory limits: %s ([memory] scale_limits = %s; RAM %.1f GiB)", v ? "scaled to this PC" : "the fixed defaults", mode.c_str(),
                    static_cast<double>(limits_ram_bytes()) / (1024.0 * 1024.0 * 1024.0));
        return v;
    }
    const uint64_t ram = limits_ram_bytes(), vram = limits_video_heap_bytes();
    const bool small_ram = ram && ram <= (20ull << 30);
    const bool small_card = vram && vram <= (17ull << 29);   // 8.5 GiB
    const bool v = small_ram || small_card;
    static std::atomic<int> logged{0};   // 1: logged before the device existed, 2: logged with it
    const int want = vram ? 2 : 1;
    if (logged.load(std::memory_order_relaxed) < want && logged.exchange(want) < want)
        NXLOG_I(kTag, "memory limits: %s ([memory] scale_limits = auto; RAM %.1f GiB%s, video memory %s)", v ? "scaled to this PC" : "the fixed defaults",
                static_cast<double>(ram) / (1024.0 * 1024.0 * 1024.0), small_ram ? " (20 GiB or less)" : "",
                vram ? (small_card ? "8.5 GiB or less" : "over 8.5 GiB") : "not known yet");
    return v;
}

int64_t limit_by_ram(int64_t over16, int64_t at16, int64_t at8) {
    if (!limits_scaled()) return over16;
    const uint64_t r = limits_ram_bytes();
    if (r && r <= (10ull << 30)) return at8;
    if (r && r <= (20ull << 30)) return at16;
    return over16;
}

uint64_t heap_committed() {
    std::lock_guard lk(g_heap_mu);
    return g_committed;
}

uint64_t heap_alias(uint64_t offset, uint64_t size, uint64_t at, Prot p) {
    std::unique_lock lk(g_heap_mu);
    if (!g_heap_size || !g_section) {
        NXLOG_W(kTag, "heap_alias: %s", g_heap_size ? "the heap is private memory (no section): no alias views" : "no heap");
        return 0;
    }
    if (size == 0 || offset % kAllocGranularity || size % kAllocGranularity || offset > g_heap_size ||
        size > g_heap_size - offset) {
        NXLOG_W(kTag, "heap_alias: [%llx, +%llx) must be 64 KiB multiples inside the section",
                static_cast<unsigned long long>(offset), static_cast<unsigned long long>(size));
        return 0;
    }
    if (at % kAllocGranularity) {
        NXLOG_W(kTag, "heap_alias: target %llx is not 64 KiB aligned", static_cast<unsigned long long>(at));
        return 0;
    }
    if (!lazy_commit(lk, offset / kPage, (offset + size) / kPage)) return 0;   // a view of uncommitted section pages would fault
    if (!g_section || offset > g_heap_size || size > g_heap_size - offset) return 0;   // the commit may have let the lock go
    const DWORD access = (p == Prot::R) ? FILE_MAP_READ : FILE_MAP_ALL_ACCESS;
    const DWORD off_hi = static_cast<DWORD>(offset >> 32);
    const DWORD off_lo = static_cast<DWORD>(offset & 0xFFFFFFFFu);

    ww::ban_section(offset, size);   // before the view exists: writes through it would not fault in the primary view
    if (at != 0) {
        void* v = MapViewOfFileEx(g_section, access, off_hi, off_lo, static_cast<SIZE_T>(size), reinterpret_cast<void*>(at));
        if (!v) {
            DWORD e = GetLastError();
            NXLOG_E(kTag, "heap_alias at %llx (section %llx, +%llx): MapViewOfFileEx failed, error %lu",
                    static_cast<unsigned long long>(at), static_cast<unsigned long long>(offset),
                    static_cast<unsigned long long>(size), static_cast<unsigned long>(e));
            return 0;
        }
        if (reinterpret_cast<uint64_t>(v) != at) {
            UnmapViewOfFile(v);
            NXLOG_E(kTag, "heap_alias: view landed at %p, not %llx", v, static_cast<unsigned long long>(at));
            return 0;
        }
        g_aliases.emplace(at, Alias{size, offset});
        g_alias_used[at] = size;
        NXLOG_I(kTag, "heap alias: section %llx (+%llx) at %llx", static_cast<unsigned long long>(offset),
                static_cast<unsigned long long>(size), static_cast<unsigned long long>(at));
        return at;
    }

    uint64_t cand = kAliasBase;
    for (int tries = 0; tries < kAliasTries; tries++) {
        cand = find_free_alias(cand, size);
        if (!cand) break;
        void* v = MapViewOfFileEx(g_section, access, off_hi, off_lo, static_cast<SIZE_T>(size), reinterpret_cast<void*>(cand));
        if (v && reinterpret_cast<uint64_t>(v) == cand) {
            g_aliases.emplace(cand, Alias{size, offset});
            g_alias_used[cand] = size;
            NXLOG_I(kTag, "heap alias: section %llx (+%llx) at %llx", static_cast<unsigned long long>(offset),
                    static_cast<unsigned long long>(size), static_cast<unsigned long long>(cand));
            return cand;
        }
        if (v) UnmapViewOfFile(v);
        NXLOG_W(kTag, "heap_alias: %llx is taken (error %lu): marking it unusable",
                static_cast<unsigned long long>(cand), static_cast<unsigned long>(GetLastError()));
        g_alias_used[cand] = size;  // refused by the kernel: never offer it again
    }
    NXLOG_E(kTag, "heap_alias: no free range for %llx bytes in the alias region", static_cast<unsigned long long>(size));
    return 0;
}

void* heap_private_view(uint64_t offset, uint64_t size) {
    std::unique_lock lk(g_heap_mu);
    if (!g_section || size == 0 || offset % kAllocGranularity || offset > g_heap_size || size > g_heap_size - offset) return nullptr;
    // the view maps whole pages, and a fault through it is not the lazy handler's: a partial last page is committed too
    if (!lazy_commit(lk, offset / kPage, align_up(offset + size, kPage) / kPage) || !g_section) return nullptr;
    return MapViewOfFile(g_section, FILE_MAP_ALL_ACCESS, static_cast<DWORD>(offset >> 32), static_cast<DWORD>(offset & 0xFFFFFFFFu), static_cast<SIZE_T>(size));
}

bool heap_private_view_unmap(void* view) { return view && UnmapViewOfFile(view) != 0; }

bool heap_alias_unmap(uint64_t view_addr) {
    std::lock_guard lk(g_heap_mu);
    auto it = g_aliases.find(view_addr);
    if (it == g_aliases.end()) {
        NXLOG_W(kTag, "heap_alias_unmap: %llx is not an alias view", static_cast<unsigned long long>(view_addr));
        return false;
    }
    if (!UnmapViewOfFile(reinterpret_cast<void*>(view_addr))) {
        NXLOG_E(kTag, "heap_alias_unmap %llx: UnmapViewOfFile failed, error %lu",
                static_cast<unsigned long long>(view_addr), static_cast<unsigned long>(GetLastError()));
        return false;
    }
    g_alias_used.erase(view_addr);
    g_aliases.erase(it);
    return true;
}

void heap_alias_ranges(std::vector<std::pair<uint64_t, uint64_t>>& out) {
    std::lock_guard lk(g_heap_mu);
    for (const auto& a : g_aliases) out.emplace_back(a.second.offset, a.second.size);
}

size_t heap_alias_count() {
    std::lock_guard lk(g_heap_mu);
    return g_aliases.size();
}

bool heap_offset_of(uint64_t addr, uint64_t* offset) {
    if (!offset) return false;
    std::lock_guard lk(g_heap_mu);
    if (g_heap_size && addr >= kHeapBase && addr - kHeapBase < g_heap_size) {
        *offset = addr - kHeapBase;
        return true;
    }
    auto it = g_aliases.upper_bound(addr);
    if (it == g_aliases.begin()) return false;
    --it;
    if (addr - it->first < it->second.size) {
        *offset = it->second.offset + (addr - it->first);
        return true;
    }
    return false;
}

// ---------------------------------------------------------------------------------------------------------------------
// vm map (svcQueryMemory)

std::string describe_page(uint64_t addr) {
    std::string out;
    char line[256];
    {
        std::unique_lock lk(g_heap_mu, std::try_to_lock);
        if (!lk.owns_lock()) out += "  heap metadata: busy\n";
        else if (addr >= kHeapBase && addr - kHeapBase < g_heap_size) {
            const uint64_t pg = (addr - kHeapBase) / kPage;
            std::snprintf(line, sizeof line, "  heap metadata: page=%llu committed=%d class=%u epoch=%llu (class: 0=reserved 1=noaccess 2=readonly 3=readwrite 4=not yet used)\n",
                (unsigned long long)pg, bit_test(pg), (unsigned)get_cls(pg), (unsigned long long)g_heap_epoch.load());
            out += line;
        } else out += "  heap metadata: outside primary heap\n";
    }
    {
        std::unique_lock lk(g_vm_mu, std::try_to_lock);
        if (!lk.owns_lock()) out += "  guest VM metadata: busy\n";
        else {
            auto it = g_vm.upper_bound(addr);
            if (it != g_vm.begin() && addr - std::prev(it)->second.addr < std::prev(it)->second.size) {
                const auto& v = std::prev(it)->second;
                std::snprintf(line, sizeof line, "  guest VM: base=0x%llx size=0x%llx state=0x%x perm=0x%x\n",
                    (unsigned long long)v.addr, (unsigned long long)v.size, v.state, v.perm);
                out += line;
            } else out += "  guest VM: free\n";
        }
    }
    return out;
}

void vm_set(uint64_t addr, uint64_t size, uint32_t state, uint32_t perm) {
    if (size == 0 || addr + size < addr) return;
    const uint64_t end = addr + size;
    std::lock_guard lk(g_vm_mu);
    // Cut every block that overlaps [addr, end); the parts outside the new range stay.
    auto it = g_vm.lower_bound(addr);
    if (it != g_vm.begin()) {
        auto p = std::prev(it);
        if (p->second.addr + p->second.size > addr) it = p;
    }
    while (it != g_vm.end() && it->second.addr < end) {
        VmInfo b = it->second;
        it = g_vm.erase(it);
        if (b.addr < addr) g_vm.emplace(b.addr, VmInfo{b.addr, addr - b.addr, b.state, b.perm});
        if (b.addr + b.size > end) g_vm.emplace(end, VmInfo{end, b.addr + b.size - end, b.state, b.perm});
    }
    if (state != kMemFree) g_vm.emplace(addr, VmInfo{addr, size, state, perm});
}

VmInfo vm_query(uint64_t addr) {
    std::lock_guard lk(g_vm_mu);
    auto it = g_vm.upper_bound(addr);  // first block that starts after addr
    if (it != g_vm.begin()) {
        auto p = std::prev(it);
        if (addr < p->second.addr + p->second.size) return p->second;
        const uint64_t gap_start = p->second.addr + p->second.size;
        const uint64_t gap_end = it == g_vm.end() ? kVmEnd : it->first;
        return VmInfo{gap_start, gap_end - gap_start, kMemFree, 0};
    }
    const uint64_t gap_end = it == g_vm.end() ? kVmEnd : it->first;
    return VmInfo{0, gap_end, kMemFree, 0};
}

}  // namespace nxrt::mem
