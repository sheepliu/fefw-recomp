// SPDX-License-Identifier: GPL-3.0-or-later
// Start-up timeline: the moments of a start, in seconds since the log began, gathered into one log line at the first presented
// frame, with the longest step named. Each name is kept once (its first mark). Any thread.
#pragma once

namespace nxrt {

// The moment `what` (a string literal) happened, now. Later marks of the same name are ignored.
void boot_mark(const char* what);
// The first presented frame: marks it and writes the timeline line (once).
void boot_first_frame();

}  // namespace nxrt
