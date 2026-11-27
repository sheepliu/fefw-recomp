// SPDX-License-Identifier: GPL-3.0-or-later
// One-call bring-up of the runtime skeleton: config, log, symbols, crash handler, memory map, SVC table, watchdog, memory guard.
// Environment switches (all optional; each also exists as an ini key, env wins): see README.md "Configuration".
#pragma once
#include <string>

namespace nxrt {

struct InitOptions {
    const char* ini_path = nullptr;     // nullptr: NXRT_INI env, else "nxrt.ini" in the current directory (missing file is fine)
    const char* log_path = nullptr;     // nullptr: [log] path / NXRT_LOG, else "nxrt.log"
    bool crash_handler = true;          // install the HOST EXCEPTION reporter
    bool watchdog = true;               // start the freeze watchdog ([diag] watchdog_seconds, default 10, < 0 disables)
    bool memguard = true;               // start the memory guard ([memory] limit_gb, default 8)
    bool memory_init = true;            // reserve the stack and kernel regions
    bool load_symbols = true;           // load the linker map next to the exe
    bool services = true;               // register the Horizon system services (services.h) in the ipc registry
    const char* app_build = nullptr;    // the program's version and commit, for the first log line and the crash reports
};

// Idempotent. Logs the effective configuration. Returns false when a mandatory piece (the address-space layout) failed.
bool init(const InitOptions& opt = {});
// Logs the end-of-run summary (svc, ipc, hle stubs, modules, threads) and stops the watchdog and the memory guard.
void shutdown();
// The same summary as text (also written to the log by shutdown()).
std::string summary();
bool initialised();
const char* abi_source();   // which ABI header the runtime was built against
const char* app_build();    // InitOptions::app_build ("" when the program gave none)
void set_app_build(const char* b);   // init() sets it from InitOptions::app_build
// Facts about this run that a log cut from the front would lose (the build, the GPU), repeated by the hitch monitor once a minute
// ([diag] session_stamp_s) and at exit, so a report that keeps only the tail of the log still says what it came from.
void set_session_note(const char* key, const std::string& value);   // any thread; a key set again replaces its value
std::string session_stamp();   // "<app build>, ABI <n>; <key> <value>; ..."

}  // namespace nxrt
