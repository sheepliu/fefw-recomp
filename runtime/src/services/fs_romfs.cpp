// SPDX-License-Identifier: GPL-3.0-or-later
// Adapted from yuzu src/core/file_sys/fsmitm_romfsbuild.cpp (GPL-2.0-or-later; the table layout, entry sizes, the name hash and the
// bucket count rule) and ScarletNX src/nx/hle/filesystem/fs.c (GPL-3.0-or-later; the same rules, with unsigned name bytes).
// The image is laid out in memory: the header, the four tables and a map of segments. File data is read from the host files on demand.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "fs_romfs.h"

#include <windows.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <filesystem>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <system_error>
#include <thread>
#include <vector>

#include "nxrt/diag.h"
#include "nxrt/log.h"
#include "nxrt/memory.h"
#include "nxrt/nup.h"
#include "nxrt/write_watch.h"

namespace nxrt::services::fs {
namespace {

constexpr uint32_t kNone = 0xFFFFFFFFu;     // "no entry" in every link field
constexpr uint64_t kHeaderSize = 0x50;      // ten u64 fields
constexpr uint64_t kPartitionOfs = 0x200;   // file data starts here; the header is padded to it
constexpr uint64_t kDataAlign = 0x10;       // each file starts at a multiple of this, counted from the partition start
constexpr uint64_t kDirFixed = 0x18;        // directory entry without its name
constexpr uint64_t kFileFixed = 0x20;       // file entry without its name
constexpr size_t kMaxName = 255;            // bytes of UTF-8 in one name
constexpr size_t kMaxPath = 0x300;          // SDK FS_MAX_PATH (0x301 with the terminator); the path counts its leading '/'
constexpr size_t kOpenFiles = 16;           // host files kept open by the cache
constexpr size_t kLongPath = 240;           // host paths this long or longer get the \\?\ prefix

uint64_t align_up(uint64_t v, uint64_t a) { return (v + a - 1) / a * a; }
uint64_t pad4(size_t n) { return align_up(n, 4); }

void put32(uint8_t* p, uint32_t v) {
    for (int i = 0; i < 4; i++) p[i] = static_cast<uint8_t>(v >> (8 * i));
}
void put64(uint8_t* p, uint64_t v) {
    for (int i = 0; i < 8; i++) p[i] = static_cast<uint8_t>(v >> (8 * i));
}

// Bucket hash of an entry named `name` under the directory entry at table offset `parent`. The name bytes count unsigned, as the
// console's char does (yuzu's signed char on x86 differs for non-ASCII names only).
uint32_t name_hash(uint32_t parent, const std::string& name) {
    uint32_t h = parent ^ 123456789u;
    for (unsigned char c : name) {
        h = (h >> 5) | (h << 27);
        h ^= c;
    }
    return h;
}

// Bucket count for n entries: 3 for fewer than 3, n|1 below 19, else the first number from n that no prime up to 17 divides.
uint64_t bucket_count(uint64_t n) {
    if (n < 3) return 3;
    if (n < 19) return n | 1;
    uint64_t c = n;
    while (c % 2 == 0 || c % 3 == 0 || c % 5 == 0 || c % 7 == 0 || c % 11 == 0 || c % 13 == 0 || c % 17 == 0) c++;
    return c;
}

bool to_utf8(std::wstring_view w, std::string& out) {
    out.clear();
    if (w.empty()) return true;
    const int n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, w.data(), static_cast<int>(w.size()), nullptr, 0, nullptr, nullptr);
    if (n <= 0) return false;
    out.resize(static_cast<size_t>(n));
    return WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, w.data(), static_cast<int>(w.size()), out.data(), n, nullptr, nullptr) == n;
}

bool to_wide(const std::string& s, std::wstring& out) {
    out.clear();
    if (s.empty()) return false;
    const int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), static_cast<int>(s.size()), nullptr, 0);
    if (n <= 0) return false;
    out.resize(static_cast<size_t>(n));
    return MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), static_cast<int>(s.size()), out.data(), n) == n;
}

// Host paths that may exceed MAX_PATH get the extended-length prefix. The input must already use backslashes.
std::wstring long_path(std::wstring p) {
    std::replace(p.begin(), p.end(), L'/', L'\\');
    if (p.size() < kLongPath || p.compare(0, 4, L"\\\\?\\") == 0) return p;
    if (p.compare(0, 2, L"\\\\") == 0) return L"\\\\?\\UNC\\" + p.substr(2);
    return L"\\\\?\\" + p;
}

// One folder or file of the host tree. Layout fields are filled in once the tree is complete.
struct Node {
    std::string name;            // UTF-8; empty for the root
    std::string path;            // image path: "" for the root, "/a/b" otherwise
    std::wstring host;           // host path to open
    bool is_dir = false;
    uint64_t size = 0;           // files only
    uint64_t src = 0;            // files: offset of the data in the host file (0, or the extent in a .nup)
    std::vector<Node> kids;      // sorted by name bytes
    uint32_t entry = 0;          // offset of this entry in its table
    uint32_t parent = 0;         // entry offset of the parent directory (the root's is 0)
    uint32_t sibling = kNone;    // next entry in the parent's chain of the same kind
    uint32_t first_child = kNone;  // directories: first subdirectory entry
    uint32_t first_file = kNone;   // directories: first file entry
    uint64_t data = 0;           // files: offset of the data from the partition start
    std::shared_ptr<const std::vector<uint8_t>> mem;   // files: the content after an edit (RomfsEdits), served instead of the host file
};

std::string shown(const Node& n) { return n.path.empty() ? std::string("/") : n.path; }

std::wstring join_host(const std::wstring& dir, const wchar_t* name) {
    std::wstring out;
    out.reserve(dir.size() + 1 + std::wcslen(name));
    out = dir;
    if (!out.empty() && out.back() != L'\\' && out.back() != L'/') out.push_back(L'\\');
    out.append(name);
    return long_path(std::move(out));
}

// Enumerates the direct children of dir.host into dir.kids (sorted by name bytes) using FindFirstFileExW, which returns attributes,
// reparse tag and 64-bit file size in one batched directory call instead of per-entry stat calls.
bool scan_dir_entries(Node& dir, std::string& msg) {
    const std::wstring pattern = join_host(dir.host, L"*");
    WIN32_FIND_DATAW fd{};
    HANDLE h = FindFirstFileExW(pattern.c_str(), FindExInfoBasic, &fd, FindExSearchNameMatch, nullptr, FIND_FIRST_EX_LARGE_FETCH);
    if (h == INVALID_HANDLE_VALUE) {
        if (GetLastError() == ERROR_FILE_NOT_FOUND) return true;   // empty folder with no . / .. entries
        msg = "cannot read folder " + shown(dir);
        return false;
    }
    struct Finder {
        HANDLE h;
        ~Finder() { FindClose(h); }
    } guard{h};

    for (;;) {
        const wchar_t* wname = fd.cFileName;
        const bool dot = wname[0] == L'.' && (wname[1] == L'\0' || (wname[1] == L'.' && wname[2] == L'\0'));
        if (!dot) {
            std::string name;
            if (!to_utf8(wname, name) || name.empty() || name.size() > kMaxName) {
                msg = "name cannot be stored in a RomFS (1 to 255 bytes of UTF-8): " + shown(dir) + "/" + name;
                return false;
            }
            Node k;
            k.name = std::move(name);
            k.path = dir.path + "/" + k.name;
            if (k.path.size() > kMaxPath) {
                msg = "path too long for a RomFS (0x300 bytes): " + k.path;
                return false;
            }
            k.host = join_host(dir.host, wname);
            const DWORD attr = fd.dwFileAttributes;
            if ((attr & FILE_ATTRIBUTE_REPARSE_POINT) != 0 &&
                (fd.dwReserved0 == IO_REPARSE_TAG_SYMLINK || fd.dwReserved0 == IO_REPARSE_TAG_MOUNT_POINT)) {
                msg = "symbolic link cannot be described: " + k.path;
                return false;
            }
            if ((attr & FILE_ATTRIBUTE_DIRECTORY) != 0) {
                k.is_dir = true;
            } else if ((attr & FILE_ATTRIBUTE_DEVICE) != 0) {
                msg = "not a file or folder: " + k.path;
                return false;
            } else {
                k.size = (static_cast<uint64_t>(fd.nFileSizeHigh) << 32) | static_cast<uint64_t>(fd.nFileSizeLow);
            }
            dir.kids.push_back(std::move(k));
        }
        if (!FindNextFileW(h, &fd)) {
            const DWORD err = GetLastError();
            if (err == ERROR_NO_MORE_FILES) break;
            msg = "cannot read folder " + shown(dir);
            return false;
        }
    }
    // std::string's operator< compares unsigned char values, which is the byte order the RomFS hash tables expect.
    std::sort(dir.kids.begin(), dir.kids.end(), [](const Node& a, const Node& b) { return a.name < b.name; });
    return true;
}

// Fills root.kids from the host folder root.host, recursively, using a bounded worker pool so directory enumeration overlaps I/O
// latency (important on Wine/FUSE/network drives with 20k+ folders). Calls diag_progress() periodically so a slow-but-moving scan
// never trips the freeze watchdog. False with msg set at the first entry that cannot be described.
bool scan(Node& root, std::string& msg) {
    if (!scan_dir_entries(root, msg)) return false;

    std::deque<Node*> queue;
    for (Node& k : root.kids) {
        if (k.is_dir) queue.push_back(&k);
    }
    if (queue.empty()) return true;

    constexpr size_t kMaxWorkers = 8;
    const unsigned hw = std::thread::hardware_concurrency();
    const size_t total_threads = std::min<size_t>(kMaxWorkers, std::max<size_t>(2, hw ? hw : 4));
    const size_t helper_count = total_threads > 1 ? total_threads - 1 : 0;

    std::mutex mu;
    std::condition_variable cv;
    size_t active = 0;
    std::atomic<bool> failed{false};
    std::atomic<uint32_t> dirs_done{1};
    std::string err_msg;

    auto worker = [&] {
        for (;;) {
            Node* cur = nullptr;
            {
                std::unique_lock<std::mutex> lk(mu);
                cv.wait(lk, [&] { return failed.load(std::memory_order_relaxed) || !queue.empty() || active == 0; });
                if (failed.load(std::memory_order_relaxed) || (queue.empty() && active == 0)) return;
                cur = queue.front();
                queue.pop_front();
                active++;
            }

            std::string local_err;
            const bool ok = scan_dir_entries(*cur, local_err);
            if ((dirs_done.fetch_add(1, std::memory_order_relaxed) & 63u) == 0) diag_progress();

            {
                std::lock_guard<std::mutex> lk(mu);
                active--;
                if (!ok) {
                    if (!failed.exchange(true, std::memory_order_relaxed)) err_msg = std::move(local_err);
                    cv.notify_all();
                    return;
                }
                bool pushed = false;
                for (Node& k : cur->kids) {
                    if (k.is_dir) {
                        queue.push_back(&k);
                        pushed = true;
                    }
                }
                if (pushed || (queue.empty() && active == 0)) cv.notify_all();
            }
        }
    };

    std::vector<std::thread> helpers;
    helpers.reserve(helper_count);
    for (size_t i = 0; i < helper_count; i++) helpers.emplace_back(worker);
    worker();
    for (std::thread& t : helpers) t.join();

    if (failed.load(std::memory_order_relaxed)) {
        msg = std::move(err_msg);
        return false;
    }
    return true;
}

void sort_tree(Node& d) {
    std::sort(d.kids.begin(), d.kids.end(), [](const Node& a, const Node& b) { return a.name < b.name; });
    for (Node& k : d.kids)
        if (k.is_dir) sort_tree(k);
}

// Fills root from the RomFS of a .nup package: every file is a range of the package file.
bool scan_nup(const std::string& nup_path, Node& root, std::string& msg) {
    nup::Content c;
    if (!nup::open(nup_path, c, &msg)) return false;
    if (c.romfs.empty()) {
        msg = nup_path + " has no RomFS";
        return false;
    }
    for (const nup::File& f : c.romfs) {
        Node* d = &root;
        size_t at = 0;
        for (size_t slash; (slash = f.path.find('/', at)) != std::string::npos; at = slash + 1) {
            const std::string name = f.path.substr(at, slash - at);
            auto it = std::find_if(d->kids.begin(), d->kids.end(), [&](const Node& k) { return k.is_dir && k.name == name; });
            if (it == d->kids.end()) {
                Node k;
                k.is_dir = true;
                k.name = name;
                k.path = d->path + "/" + name;
                d->kids.push_back(std::move(k));
                it = d->kids.end() - 1;
            }
            d = &*it;
        }
        Node k;
        k.name = f.path.substr(at);
        k.path = "/" + f.path;
        k.host = root.host;
        k.size = f.size;
        k.src = f.offset;
        if (k.name.size() > kMaxName || k.path.size() > kMaxPath) {
            msg = "name or path too long for a RomFS: " + k.path;
            return false;
        }
        d->kids.push_back(std::move(k));
    }
    sort_tree(root);
    return true;
}

// ---- overlays (RomFS mods) --------------------------------------------------------------------------------------------------------
bool same_name_nocase(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); i++) {
        unsigned char x = static_cast<unsigned char>(a[i]), y = static_cast<unsigned char>(b[i]);
        if (x >= 'A' && x <= 'Z') x = static_cast<unsigned char>(x + 32);
        if (y >= 'A' && y <= 'Z') y = static_cast<unsigned char>(y + 32);
        if (x != y) return false;
    }
    return true;
}

uint32_t count_files(const Node& n) {
    if (!n.is_dir) return 1;
    uint32_t c = 0;
    for (const Node& k : n.kids) c += count_files(k);
    return c;
}

// Gives n (and everything under it) its image path under the directory `parent`. False when a path gets too long.
bool repath(Node& n, const std::string& parent, std::string& msg) {
    n.path = parent + "/" + n.name;
    if (n.path.size() > kMaxPath) {
        msg = "path too long for a RomFS (0x300 bytes): " + n.path;
        return false;
    }
    for (Node& k : n.kids)
        if (!repath(k, n.path, msg)) return false;
    return true;
}

// Lays the overlay directory `add` over `base`. `taken` counts the files taken from the overlay.
bool merge(Node& base, Node& add, uint32_t& taken, std::string& msg) {
    for (Node& k : add.kids) {
        auto it = std::find_if(base.kids.begin(), base.kids.end(), [&](const Node& b) { return b.name == k.name; });
        if (it == base.kids.end())
            it = std::find_if(base.kids.begin(), base.kids.end(), [&](const Node& b) { return same_name_nocase(b.name, k.name); });
        if (it == base.kids.end()) {
            if (!repath(k, base.path, msg)) return false;
            taken += count_files(k);
            base.kids.push_back(std::move(k));
        } else if (it->is_dir && k.is_dir) {
            if (!merge(*it, k, taken, msg)) return false;
        } else if (!it->is_dir && !k.is_dir) {
            it->host = k.host;
            it->size = k.size;
            it->src = 0;
            taken++;
        } else {
            // a file where the game has a folder, or the other way round: the overlay's entry replaces the game's
            const std::string name = it->name;
            *it = std::move(k);
            it->name = name;
            if (!repath(*it, base.path, msg)) return false;
            taken += count_files(*it);
        }
    }
    return true;
}

void flatten(Node& n, std::vector<Node*>& dirs, std::vector<Node*>& files) {
    for (Node& k : n.kids) {
        if (k.is_dir) {
            dirs.push_back(&k);
            flatten(k, dirs, files);
        } else {
            files.push_back(&k);
        }
    }
}

struct Segment {
    enum class Kind : uint8_t { kHeader, kTables, kZero, kFile, kMem };
    uint64_t off = 0;            // image offset
    uint64_t len = 0;
    Kind kind = Kind::kZero;
    uint64_t src = 0;            // kHeader/kTables: offset in that buffer; kFile: offset in the host file
    uint32_t file = 0;           // kFile, kMem: index into Layout::files
};

struct FileRec {
    std::wstring host;
    uint64_t size = 0;
    uint64_t image_off = 0;
    uint64_t src = 0;            // offset of the data in the host file
    std::string path;            // path inside the image ("/a/b.bin"), for the first-read note
    std::shared_ptr<const std::vector<uint8_t>> mem;   // set: the content is here (an edited file), the host file is not read
    int16_t overlay = -1;        // the RomFS mod the file comes from (index into Layout::overlay_names), -1: the game's
};

// The whole of a file of the tree (a host file, or its range of a package). False when it cannot be read.
bool read_whole(const Node& f, std::vector<uint8_t>& out) {
    HANDLE h = CreateFileW(f.host.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    out.resize(static_cast<size_t>(f.size));
    uint64_t done = 0;
    bool ok = true;
    while (ok && done < f.size) {
        const DWORD chunk = static_cast<DWORD>(std::min<uint64_t>(f.size - done, 1ull << 30));
        const uint64_t ofs = f.src + done;
        OVERLAPPED ov{};
        ov.Offset = static_cast<DWORD>(ofs & 0xFFFFFFFFull);
        ov.OffsetHigh = static_cast<DWORD>(ofs >> 32);
        DWORD got = 0;
        ok = ReadFile(h, out.data() + done, chunk, &got, &ov) && got == chunk;
        done += chunk;
    }
    CloseHandle(h);
    return ok;
}

// Everything build() produces. Replaced as a whole.
struct Layout {
    bool built = false;
    uint64_t size = 0;
    RomfsImage::Stats stats;
    std::array<uint8_t, kHeaderSize> header{};
    std::vector<uint8_t> tables;        // dir hash, dir table, file hash, file table, in that order from dir_hash_table_ofs
    std::vector<Segment> segs;          // contiguous, covering [0, size)
    std::vector<FileRec> files;         // in data order
    std::map<std::string, uint32_t> by_path;
    std::vector<std::string> overlay_names;   // per applied RomFS mod: its name (the folder holding its romfs folder), for the reports
    std::wstring cache_wpath;                 // set when loaded from romfs_cache.bin: removed on any host read error so next launch rescans
};

// ---- base tree cache (romfs_cache.bin) -------------------------------------------------------------------------------------------
constexpr char kCacheMagic[8] = {'N', 'X', 'R', 'C', '0', '0', '0', '1'};
constexpr size_t kMaxCacheBytes = 256ull << 20;

uint64_t fnv1a_update(uint64_t h, const void* p, size_t n) {
    const auto* b = static_cast<const uint8_t*>(p);
    for (size_t i = 0; i < n; i++) h = (h ^ b[i]) * 1099511628211ull;
    return h;
}

uint64_t filetime_u64(const FILETIME& ft) {
    return (static_cast<uint64_t>(ft.dwHighDateTime) << 32) | static_cast<uint64_t>(ft.dwLowDateTime);
}

struct TopFingerprint {
    std::string root_utf8;
    uint64_t root_mtime = 0;
    uint64_t top_hash = 0;
};

// Cheap validation fingerprint: root canonical path, root mtime, and a hash of its direct children (one FindFirstFileExW call).
bool compute_top_fingerprint(const std::wstring& root_host, TopFingerprint& fp) {
    if (!to_utf8(root_host, fp.root_utf8)) return false;
    WIN32_FILE_ATTRIBUTE_DATA fad{};
    if (!GetFileAttributesExW(root_host.c_str(), GetFileExInfoStandard, &fad)) return false;
    fp.root_mtime = filetime_u64(fad.ftLastWriteTime);

    struct Entry {
        std::string name;
        uint8_t is_dir = 0;
        uint64_t size = 0;
        uint64_t mtime = 0;
    };
    std::vector<Entry> top;
    const std::wstring pattern = join_host(root_host, L"*");
    WIN32_FIND_DATAW fd{};
    HANDLE h = FindFirstFileExW(pattern.c_str(), FindExInfoBasic, &fd, FindExSearchNameMatch, nullptr, FIND_FIRST_EX_LARGE_FETCH);
    if (h != INVALID_HANDLE_VALUE) {
        struct Finder {
            HANDLE h;
            ~Finder() { FindClose(h); }
        } guard{h};
        for (;;) {
            const wchar_t* wname = fd.cFileName;
            const bool dot = wname[0] == L'.' && (wname[1] == L'\0' || (wname[1] == L'.' && wname[2] == L'\0'));
            if (!dot) {
                Entry e;
                if (!to_utf8(wname, e.name)) return false;
                e.is_dir = (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0 ? 1 : 0;
                e.size = e.is_dir ? 0 : ((static_cast<uint64_t>(fd.nFileSizeHigh) << 32) | static_cast<uint64_t>(fd.nFileSizeLow));
                e.mtime = filetime_u64(fd.ftLastWriteTime);
                top.push_back(std::move(e));
            }
            if (!FindNextFileW(h, &fd)) break;
        }
    } else if (GetLastError() != ERROR_FILE_NOT_FOUND) {
        return false;
    }
    std::sort(top.begin(), top.end(), [](const Entry& a, const Entry& b) { return a.name < b.name; });
    uint64_t hash = 14695981039346656037ull;
    const uint64_t count = top.size();
    hash = fnv1a_update(hash, &count, sizeof count);
    for (const Entry& e : top) {
        const uint32_t nl = static_cast<uint32_t>(e.name.size());
        hash = fnv1a_update(hash, &nl, sizeof nl);
        hash = fnv1a_update(hash, e.name.data(), e.name.size());
        hash = fnv1a_update(hash, &e.is_dir, sizeof e.is_dir);
        hash = fnv1a_update(hash, &e.size, sizeof e.size);
        hash = fnv1a_update(hash, &e.mtime, sizeof e.mtime);
    }
    fp.top_hash = hash;
    return true;
}

bool load_tree_cache(const std::string& cache_path, const TopFingerprint& want, Node& root) {
    std::wstring wpath;
    if (!to_wide(cache_path, wpath)) return false;
    wpath = long_path(std::move(wpath));
    HANDLE h = CreateFileW(wpath.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    struct Closer {
        HANDLE h;
        ~Closer() { CloseHandle(h); }
    } guard{h};
    LARGE_INTEGER fsz{};
    if (!GetFileSizeEx(h, &fsz) || fsz.QuadPart < 32 || static_cast<uint64_t>(fsz.QuadPart) > kMaxCacheBytes) return false;
    std::vector<uint8_t> buf(static_cast<size_t>(fsz.QuadPart));
    size_t done = 0;
    while (done < buf.size()) {
        const DWORD chunk = static_cast<DWORD>(std::min<size_t>(buf.size() - done, 1u << 24));
        DWORD got = 0;
        if (!ReadFile(h, buf.data() + done, chunk, &got, nullptr) || got == 0) return false;
        done += got;
    }

    if (std::memcmp(buf.data(), kCacheMagic, 8) != 0) return false;
    const size_t payload = buf.size() - 8;
    uint64_t stored_crc = 0;
    std::memcpy(&stored_crc, buf.data() + payload, 8);
    const uint64_t calc_crc = fnv1a_update(14695981039346656037ull, buf.data(), payload);
    if (stored_crc != calc_crc) return false;

    size_t pos = 8;
    auto rd = [&](void* dst, size_t n) -> bool {
        if (pos + n > payload) return false;
        std::memcpy(dst, buf.data() + pos, n);
        pos += n;
        return true;
    };
    uint32_t root_len = 0;
    if (!rd(&root_len, 4) || root_len != want.root_utf8.size() || pos + root_len > payload) return false;
    if (std::memcmp(buf.data() + pos, want.root_utf8.data(), root_len) != 0) return false;
    pos += root_len;

    uint64_t root_mtime = 0, top_hash = 0;
    uint32_t dir_count = 0, file_count = 0;
    if (!rd(&root_mtime, 8) || !rd(&top_hash, 8) || !rd(&dir_count, 4) || !rd(&file_count, 4)) return false;
    if (root_mtime != want.root_mtime || top_hash != want.top_hash || dir_count == 0) return false;

    std::vector<Node> dnodes(dir_count);
    std::vector<uint32_t> dparent(dir_count, 0);
    dnodes[0].is_dir = true;
    dnodes[0].host = root.host;
    std::wstring wn;
    for (uint32_t i = 1; i < dir_count; i++) {
        uint32_t parent = 0;
        uint16_t nlen = 0;
        if (!rd(&parent, 4) || !rd(&nlen, 2) || parent >= i || nlen == 0 || nlen > kMaxName || pos + nlen > payload) return false;
        std::string name(reinterpret_cast<const char*>(buf.data() + pos), nlen);
        pos += nlen;
        if (!to_wide(name, wn)) return false;
        dparent[i] = parent;
        dnodes[i].is_dir = true;
        dnodes[i].name = std::move(name);
        dnodes[i].path = dnodes[parent].path + "/" + dnodes[i].name;
        dnodes[i].host = join_host(dnodes[parent].host, wn.c_str());
    }
    for (uint32_t j = 0; j < file_count; j++) {
        uint32_t parent = 0;
        uint64_t sz = 0;
        uint16_t nlen = 0;
        if (!rd(&parent, 4) || !rd(&sz, 8) || !rd(&nlen, 2) || parent >= dir_count || nlen == 0 || nlen > kMaxName || pos + nlen > payload)
            return false;
        std::string name(reinterpret_cast<const char*>(buf.data() + pos), nlen);
        pos += nlen;
        if (!to_wide(name, wn)) return false;
        Node f;
        f.name = std::move(name);
        f.path = dnodes[parent].path + "/" + f.name;
        f.host = join_host(dnodes[parent].host, wn.c_str());
        f.size = sz;
        dnodes[parent].kids.push_back(std::move(f));
    }
    if (pos != payload) return false;

    for (uint32_t i = dir_count; --i > 0;) {
        dnodes[dparent[i]].kids.push_back(std::move(dnodes[i]));
    }
    sort_tree(dnodes[0]);
    root = std::move(dnodes[0]);
    NXLOG_I("fs", "RomFS tree loaded from cache (%u files, %u directories)", file_count, dir_count);
    return true;
}

void save_tree_cache(const std::string& cache_path, const TopFingerprint& fp, const Node& root) {
    std::wstring wpath;
    if (!to_wide(cache_path, wpath)) return;
    wpath = long_path(std::move(wpath));

    struct FlatDir {
        uint32_t parent;
        const std::string* name;
    };
    struct FlatFile {
        uint32_t parent;
        uint64_t size;
        const std::string* name;
    };
    std::vector<FlatDir> dirs;
    std::vector<FlatFile> files;
    std::vector<std::pair<const Node*, uint32_t>> stack;
    stack.push_back({&root, 0});
    uint32_t next_dir_id = 1;
    // Preorder traversal guarantees parent_idx < dir_idx for every directory.
    while (!stack.empty()) {
        const auto [cur, cur_id] = stack.back();
        stack.pop_back();
        for (const Node& k : cur->kids) {
            if (k.is_dir) {
                const uint32_t child_id = next_dir_id++;
                dirs.push_back({cur_id, &k.name});
                stack.push_back({&k, child_id});
            } else {
                files.push_back({cur_id, k.size, &k.name});
            }
        }
    }

    std::vector<uint8_t> buf;
    buf.reserve(64 + fp.root_utf8.size() + dirs.size() * 24 + files.size() * 40);
    auto wr = [&](const void* src, size_t n) {
        const auto* b = static_cast<const uint8_t*>(src);
        buf.insert(buf.end(), b, b + n);
    };
    wr(kCacheMagic, 8);
    const uint32_t rlen = static_cast<uint32_t>(fp.root_utf8.size());
    wr(&rlen, 4);
    wr(fp.root_utf8.data(), rlen);
    wr(&fp.root_mtime, 8);
    wr(&fp.top_hash, 8);
    const uint32_t dcount = static_cast<uint32_t>(dirs.size() + 1);
    const uint32_t fcount = static_cast<uint32_t>(files.size());
    wr(&dcount, 4);
    wr(&fcount, 4);
    for (const FlatDir& d : dirs) {
        const uint16_t nl = static_cast<uint16_t>(d.name->size());
        wr(&d.parent, 4);
        wr(&nl, 2);
        wr(d.name->data(), nl);
    }
    for (const FlatFile& f : files) {
        const uint16_t nl = static_cast<uint16_t>(f.name->size());
        wr(&f.parent, 4);
        wr(&f.size, 8);
        wr(&nl, 2);
        wr(f.name->data(), nl);
    }
    const uint64_t crc = fnv1a_update(14695981039346656037ull, buf.data(), buf.size());
    wr(&crc, 8);

    const std::wstring tmp = wpath + L".tmp";
    HANDLE h = CreateFileW(tmp.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return;
    size_t done = 0;
    bool ok = true;
    while (ok && done < buf.size()) {
        const DWORD chunk = static_cast<DWORD>(std::min<size_t>(buf.size() - done, 1u << 24));
        DWORD wrote = 0;
        ok = WriteFile(h, buf.data() + done, chunk, &wrote, nullptr) && wrote == chunk;
        done += chunk;
    }
    CloseHandle(h);
    if (ok) {
        if (!MoveFileExW(tmp.c_str(), wpath.c_str(), MOVEFILE_REPLACE_EXISTING)) DeleteFileW(tmp.c_str());
    } else {
        DeleteFileW(tmp.c_str());
    }
}

// Lays out the host folder: entries sorted by full path in each table, file data in that order, 16-byte aligned.
bool make_layout(const std::string& host_dir, const std::vector<std::string>& overlays, const RomfsEdits* edits,
                 const std::string& cache_path, Layout& L, std::string* err) {
    std::string msg;
    std::wstring wdir;
    const bool packaged = nup::is_nup(host_dir);
    if (!to_wide(host_dir, wdir)) {
        if (err) *err = "host folder name is empty or not valid UTF-8";
        return false;
    }
    std::error_code ec;
    const std::filesystem::path abs = std::filesystem::absolute(std::filesystem::path(wdir), ec);
    if (ec || (!packaged && !std::filesystem::is_directory(abs, ec))) {
        if (err) *err = "not a folder: " + host_dir;
        return false;
    }
    Node root;
    root.is_dir = true;
    root.host = long_path(abs.wstring());
    if (packaged) {
        if (!scan_nup(host_dir, root, msg)) {
            if (err) *err = msg;
            return false;
        }
    } else {
        TopFingerprint fp;
        const bool use_cache = !cache_path.empty() && compute_top_fingerprint(root.host, fp);
        if (use_cache && load_tree_cache(cache_path, fp, root)) {
            std::wstring cw;
            if (to_wide(cache_path, cw)) L.cache_wpath = long_path(std::move(cw));
        } else {
            if (!scan(root, msg)) {
                if (err) *err = msg;
                return false;
            }
            if (use_cache) save_tree_cache(cache_path, fp, root);
        }
    }
    uint32_t taken = 0;
    std::vector<std::wstring> overlay_hosts;   // the applied overlays' folders, parallel to L.overlay_names
    for (const std::string& o : overlays) {
        std::wstring wo;
        std::error_code oec;
        const std::filesystem::path oabs = to_wide(o, wo) ? std::filesystem::absolute(std::filesystem::path(wo), oec) : std::filesystem::path();
        if (oabs.empty() || oec || !std::filesystem::is_directory(oabs, oec)) {
            NXLOG_W("fs", "romfs overlay '%s' is not a folder: left out", o.c_str());
            continue;
        }
        Node ov;
        ov.is_dir = true;
        ov.host = long_path(oabs.wstring());
        std::string omsg;
        if (!scan(ov, omsg)) {
            NXLOG_W("fs", "romfs overlay '%s' left out: %s", o.c_str(), omsg.c_str());
            continue;
        }
        // merge into a copy, so an overlay that fails halfway (a path too long) leaves the tree as it was
        Node merged = root;
        uint32_t n = 0;
        if (!merge(merged, ov, n, omsg)) {
            NXLOG_W("fs", "romfs overlay '%s' left out: %s", o.c_str(), omsg.c_str());
            continue;
        }
        root = std::move(merged);
        taken += n;
        NXLOG_I("fs", "romfs overlay '%s': %u files", o.c_str(), n);
        {   // the mod's name: "mods/<name>/romfs" gives <name>, any other folder its own name
            std::filesystem::path q = oabs;
            if (!q.has_filename()) q = q.parent_path();
            std::string leaf;
            const bool is_romfs = to_utf8(q.filename().wstring(), leaf) && same_name_nocase(leaf, "romfs");
            const std::filesystem::path named = is_romfs && q.has_parent_path() ? q.parent_path().filename() : q.filename();
            std::string name;
            if (!to_utf8(named.wstring(), name) || name.empty()) name = o;
            L.overlay_names.push_back(name);
            overlay_hosts.push_back(ov.host);
        }
    }
    if (!overlays.empty()) sort_tree(root);

    std::vector<Node*> dirs{&root}, files;
    flatten(root, dirs, files);
    auto by_path = [](const Node* a, const Node* b) { return a->path < b->path; };
    std::sort(dirs.begin(), dirs.end(), by_path);
    std::sort(files.begin(), files.end(), by_path);

    // Edits by code mods: the file as it stands after the overlays, changed in memory. Its size is the edited one from here on.
    uint32_t edited = 0;
    if (edits && edits->wants && edits->edit) {
        for (Node* f : files) {
            if (!edits->wants(f->path)) continue;
            if (f->size > edits->max_size) {
                NXLOG_W("fs", "romfs: %s is too large to be edited by a mod (%llu bytes): served as it is", f->path.c_str(),
                        static_cast<unsigned long long>(f->size));
                continue;
            }
            std::vector<uint8_t> data;
            if (!read_whole(*f, data)) {
                NXLOG_W("fs", "romfs: %s cannot be read for a mod's edit: served as it is", f->path.c_str());
                continue;
            }
            const uint64_t before = f->size;
            if (!edits->edit(f->path, data)) continue;
            f->size = data.size();
            f->mem = std::make_shared<const std::vector<uint8_t>>(std::move(data));
            edited++;
            NXLOG_I("fs", "romfs: %s edited by a code mod: %llu -> %llu bytes", f->path.c_str(), static_cast<unsigned long long>(before),
                    static_cast<unsigned long long>(f->size));
        }
    }

    // Entry offsets in the two tables (root first: its path is empty).
    uint64_t dir_bytes = 0, file_bytes = 0;
    for (Node* d : dirs) {
        d->entry = static_cast<uint32_t>(dir_bytes);
        dir_bytes += kDirFixed + pad4(d->name.size());
    }
    for (Node* f : files) {
        f->entry = static_cast<uint32_t>(file_bytes);
        file_bytes += kFileFixed + pad4(f->name.size());
    }
    if (dir_bytes >= 0xFFFFFFF0ull || file_bytes >= 0xFFFFFFF0ull) {
        if (err) *err = "too many entries for a RomFS";
        return false;
    }

    // File data: each file at a multiple of 16 from the partition start.
    uint64_t part = 0;
    for (Node* f : files) {
        part = align_up(part, kDataAlign);
        f->data = part;
        part += f->size;
    }

    // Links: each directory's children in name order, as chains of sibling entries.
    for (Node* d : dirs) {
        std::vector<Node*> sub, fil;
        for (Node& k : d->kids) (k.is_dir ? sub : fil).push_back(&k);
        d->first_child = sub.empty() ? kNone : sub.front()->entry;
        d->first_file = fil.empty() ? kNone : fil.front()->entry;
        for (size_t i = 0; i < sub.size(); i++) {
            sub[i]->parent = d->entry;
            sub[i]->sibling = i + 1 < sub.size() ? sub[i + 1]->entry : kNone;
        }
        for (size_t i = 0; i < fil.size(); i++) {
            fil[i]->parent = d->entry;
            fil[i]->sibling = i + 1 < fil.size() ? fil[i + 1]->entry : kNone;
        }
    }

    // Table placement: the header is 0x50 bytes, the partition starts at 0x200, then the four tables, each 4-byte aligned.
    const uint64_t dbuckets = bucket_count(dirs.size());
    const uint64_t fbuckets = bucket_count(files.size());
    const uint64_t dhash_size = 4 * dbuckets, fhash_size = 4 * fbuckets;
    const uint64_t dhash_ofs = align_up(kPartitionOfs + part, 4);
    const uint64_t dtab_ofs = dhash_ofs + dhash_size;
    const uint64_t fhash_ofs = dtab_ofs + dir_bytes;
    const uint64_t ftab_ofs = fhash_ofs + fhash_size;
    const uint64_t image_size = ftab_ofs + file_bytes;

    L.tables.assign(dhash_size + dir_bytes + fhash_size + file_bytes, 0);
    uint8_t* dh = L.tables.data();
    uint8_t* dt = dh + dhash_size;
    uint8_t* fh = dt + dir_bytes;
    uint8_t* ft = fh + fhash_size;
    std::vector<uint32_t> dhead(dbuckets, kNone), fhead(fbuckets, kNone);

    for (Node* d : dirs) {
        const uint32_t b = static_cast<uint32_t>(name_hash(d->parent, d->name) % dbuckets);
        uint8_t* e = dt + d->entry;
        put32(e + 0, d->parent);
        put32(e + 4, d->sibling);
        put32(e + 8, d->first_child);
        put32(e + 12, d->first_file);
        put32(e + 16, dhead[b]);
        put32(e + 20, static_cast<uint32_t>(d->name.size()));
        std::memcpy(e + kDirFixed, d->name.data(), d->name.size());
        dhead[b] = d->entry;
    }
    for (Node* f : files) {
        const uint32_t b = static_cast<uint32_t>(name_hash(f->parent, f->name) % fbuckets);
        uint8_t* e = ft + f->entry;
        put32(e + 0, f->parent);
        put32(e + 4, f->sibling);
        put64(e + 8, f->data);
        put64(e + 16, f->size);
        put32(e + 24, fhead[b]);
        put32(e + 28, static_cast<uint32_t>(f->name.size()));
        std::memcpy(e + kFileFixed, f->name.data(), f->name.size());
        fhead[b] = f->entry;
    }
    for (size_t i = 0; i < dhead.size(); i++) put32(dh + 4 * i, dhead[i]);
    for (size_t i = 0; i < fhead.size(); i++) put32(fh + 4 * i, fhead[i]);

    std::memset(L.header.data(), 0, L.header.size());
    put64(L.header.data() + 0, kHeaderSize);
    put64(L.header.data() + 8, dhash_ofs);
    put64(L.header.data() + 16, dhash_size);
    put64(L.header.data() + 24, dtab_ofs);
    put64(L.header.data() + 32, dir_bytes);
    put64(L.header.data() + 40, fhash_ofs);
    put64(L.header.data() + 48, fhash_size);
    put64(L.header.data() + 56, ftab_ofs);
    put64(L.header.data() + 64, file_bytes);
    put64(L.header.data() + 72, kPartitionOfs);

    // Segments: header, padding, then each file's data with the zero gaps between, the zero gap up to the tables, the tables.
    L.segs.clear();
    L.files.clear();
    L.by_path.clear();
    L.segs.push_back({0, kHeaderSize, Segment::Kind::kHeader, 0, 0});
    L.segs.push_back({kHeaderSize, kPartitionOfs - kHeaderSize, Segment::Kind::kZero, 0, 0});
    uint64_t cursor = kPartitionOfs;
    uint64_t data_bytes = 0;
    for (Node* f : files) {
        const uint64_t img = kPartitionOfs + f->data;
        if (img > cursor) L.segs.push_back({cursor, img - cursor, Segment::Kind::kZero, 0, 0});
        const uint32_t idx = static_cast<uint32_t>(L.files.size());
        L.files.push_back({f->host, f->size, img, f->src, f->path, f->mem});
        // a file from a RomFS mod: its host path lies in that mod's folder (the last mod laid down wins, as in merge)
        for (size_t o = overlay_hosts.size(); o-- > 0;) {
            const std::wstring& oh = overlay_hosts[o];
            if (f->host.size() > oh.size() && f->host.compare(0, oh.size(), oh) == 0 && (f->host[oh.size()] == L'\\' || f->host[oh.size()] == L'/')) {
                L.files.back().overlay = static_cast<int16_t>(o);
                break;
            }
        }
        if (f->size > 0) L.segs.push_back({img, f->size, f->mem ? Segment::Kind::kMem : Segment::Kind::kFile, f->mem ? 0 : f->src, idx});
        L.by_path[f->path] = idx;
        cursor = img + f->size;
        data_bytes += f->size;
    }
    if (dhash_ofs > cursor) L.segs.push_back({cursor, dhash_ofs - cursor, Segment::Kind::kZero, 0, 0});
    L.segs.push_back({dhash_ofs, image_size - dhash_ofs, Segment::Kind::kTables, 0, 0});

    L.size = image_size;
    L.stats.files = static_cast<uint32_t>(files.size());
    L.stats.dirs = static_cast<uint32_t>(dirs.size());
    L.stats.data_bytes = data_bytes;
    L.stats.data_offset = kPartitionOfs;
    L.stats.overlay_files = taken;
    L.stats.edited_files = edited;
    L.built = true;
    return true;
}

// One host file kept open. The box closes its handle when the last reader and the cache have let go of it.
struct HandleBox {
    HANDLE h = INVALID_HANDLE_VALUE;
    HandleBox() = default;
    HandleBox(const HandleBox&) = delete;
    HandleBox& operator=(const HandleBox&) = delete;
    ~HandleBox() {
        if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
    }
};

struct OpenSlot {
    uint32_t file = kNone;
    uint64_t used = 0;
    std::shared_ptr<HandleBox> box;
};

}  // namespace

struct RomfsImage::Impl {
    Layout l;
    mutable std::mutex mu;                  // guards cache, tick and read_seen
    mutable std::vector<uint8_t> read_seen; // per file: read at least once (the game's loads, in the log and the crash report)
    void note_read(uint32_t idx) const {
        {
            std::lock_guard<std::mutex> lk(mu);
            if (idx >= read_seen.size() || read_seen[idx]) return;
            read_seen[idx] = 1;
        }
        const FileRec& fr = l.files[idx];
        NXLOG_D("fs", "romfs: first read of %s", fr.path.c_str());
        // the crash and device-lost reports list these: a file from a mod, or changed by a code mod, says so
        if (fr.overlay >= 0 && static_cast<size_t>(fr.overlay) < l.overlay_names.size())
            diag_note_file("rom", (fr.path + "  [mod: " + l.overlay_names[static_cast<size_t>(fr.overlay)] + (fr.mem ? ", edited by a code mod]" : "]")).c_str());
        else if (fr.mem)
            diag_note_file("rom", (fr.path + "  [edited by a code mod]").c_str());
        else
            diag_note_file("rom", fr.path.c_str());
    }
    mutable std::vector<OpenSlot> cache;    // at most kOpenFiles entries
    mutable uint64_t tick = 0;

    // The handle for file idx, opened if it is not cached. The least recently used entry is dropped when the cache is full.
    std::shared_ptr<HandleBox> acquire(uint32_t idx) const {
        std::lock_guard<std::mutex> lk(mu);
        for (OpenSlot& s : cache) {
            if (s.file == idx) {
                s.used = ++tick;
                return s.box;
            }
        }
        const FileRec& f = l.files[idx];
        HANDLE h = CreateFileW(f.host.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                               OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h == INVALID_HANDLE_VALUE) return nullptr;
        auto box = std::make_shared<HandleBox>();
        box->h = h;
        if (cache.size() < kOpenFiles) {
            cache.push_back(OpenSlot{idx, ++tick, box});
        } else {
            auto victim = std::min_element(cache.begin(), cache.end(),
                                           [](const OpenSlot& a, const OpenSlot& b) { return a.used < b.used; });
            *victim = OpenSlot{idx, ++tick, box};
        }
        return box;
    }

    // Positioned reads (OVERLAPPED offset), so concurrent readers of one handle need no seek lock.
    void invalidate_cache_if_loaded() const {
        if (l.cache_wpath.empty()) return;
        std::lock_guard<std::mutex> lk(mu);
        if (cache_invalidated) return;
        cache_invalidated = true;
        DeleteFileW(l.cache_wpath.c_str());
        NXLOG_W("fs", "romfs: cached file read failed; deleted RomFS cache so the next start rescans");
    }
    mutable bool cache_invalidated = false;

    bool read_host(uint32_t idx, uint64_t ofs, uint8_t* out, uint64_t n) const {
        std::shared_ptr<HandleBox> box = acquire(idx);
        if (!box) {
            invalidate_cache_if_loaded();
            return false;
        }
        if (!mem::heap_touch(reinterpret_cast<uint64_t>(out), n)) return false;   // [memory] heap_lazy_commit: ReadFile fails on uncommitted pages
        const mem::ww::OsWriteScope ws(out, n);   // ReadFile fails on a page the write tracker made read-only
        while (n > 0) {
            const DWORD chunk = static_cast<DWORD>(std::min<uint64_t>(n, 1ull << 30));
            OVERLAPPED ov{};
            ov.Offset = static_cast<DWORD>(ofs & 0xFFFFFFFFull);
            ov.OffsetHigh = static_cast<DWORD>(ofs >> 32);
            DWORD got = 0;
            // A short read means the file shrank since build(): an error, not zeros.
            if (!ReadFile(box->h, out, chunk, &got, &ov) || got != chunk) {
                invalidate_cache_if_loaded();
                return false;
            }
            out += chunk;
            ofs += chunk;
            n -= chunk;
        }
        return true;
    }
};

RomfsImage::RomfsImage() : p_(std::make_unique<Impl>()) {}
RomfsImage::~RomfsImage() = default;

bool RomfsImage::build(const std::string& host_dir, std::string* err, const std::vector<std::string>& overlays, const RomfsEdits* edits,
                       const std::string& cache_path) {
    Layout fresh;
    const bool ok = make_layout(host_dir, overlays, edits, cache_path, fresh, err);
    std::lock_guard<std::mutex> lk(p_->mu);
    p_->cache.clear();
    p_->cache_invalidated = false;
    p_->l = ok ? std::move(fresh) : Layout{};
    p_->read_seen.assign(p_->l.files.size(), 0);
    return ok;
}

bool RomfsImage::built() const { return p_->l.built; }
uint64_t RomfsImage::size() const { return p_->l.size; }
RomfsImage::Stats RomfsImage::stats() const { return p_->l.stats; }

bool RomfsImage::read(uint64_t offset, void* dst, uint64_t len) const {
    const Layout& L = p_->l;
    if (!L.built || offset > L.size || len > L.size - offset) return false;
    if (len == 0) return true;
    uint8_t* out = static_cast<uint8_t*>(dst);
    auto it = std::upper_bound(L.segs.begin(), L.segs.end(), offset,
                               [](uint64_t v, const Segment& g) { return v < g.off; });
    size_t i = static_cast<size_t>(it - L.segs.begin()) - 1;
    while (len > 0) {
        const Segment& g = L.segs[i];
        const uint64_t within = offset - g.off;
        const uint64_t n = std::min(len, g.len - within);
        switch (g.kind) {
            case Segment::Kind::kHeader: std::memcpy(out, L.header.data() + g.src + within, n); break;
            case Segment::Kind::kTables: std::memcpy(out, L.tables.data() + g.src + within, n); break;
            case Segment::Kind::kZero: std::memset(out, 0, n); break;
            case Segment::Kind::kFile:
                p_->note_read(g.file);
                if (!p_->read_host(g.file, g.src + within, out, n)) return false;
                break;
            case Segment::Kind::kMem:
                p_->note_read(g.file);
                std::memcpy(out, L.files[g.file].mem->data() + within, n);
                break;
        }
        out += n;
        offset += n;
        len -= n;
        i++;
    }
    return true;
}

bool RomfsImage::file_extent(const std::string& path, uint64_t* offset, uint64_t* size) const {
    const Layout& L = p_->l;
    auto it = L.by_path.find(path);
    if (it == L.by_path.end()) return false;
    const FileRec& f = L.files[it->second];
    if (offset) *offset = f.image_off;
    if (size) *size = f.size;
    return true;
}

}  // namespace nxrt::services::fs
