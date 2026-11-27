#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of analyze_reports.py (and what it reads from fetch_reports.py) on made-up reports:

python3 tools/analyze_reports_test.py
"""

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import analyze_reports as a
import fetch_reports as f


def summary(end: float, fps: float, low: float = 50.0) -> str:
    return (f"[{end:.6f} I t2 hitch] hitch: 600 frames in 10.0 s: mean {fps} fps, 1% low {low} fps, "
            "worst 40.0 ms, 0 over 2x the median (16.7 ms)")


def clock(at: float, target: int) -> str:
    return f"[{at:.6f} I t3 fefw] fefw: game clock: 1.000x real time over 10 s (target {target} fps, display 60, x)"


def log_of(count: int, fps: float, target: int = 60, first: float = 110.0) -> str:
    lines = []
    for i in range(count):
        end = first + 10 * i
        lines += [clock(end - 5, target), summary(end, fps)]
    return "\n".join(lines)


class FpsTest(unittest.TestCase):
    def test_boot_is_left_out(self):
        self.assertEqual(len(a.fps_windows(log_of(10, 60, first=10.0))), 1)   # only the window ending at 100 s

    def test_loading_screens_are_left_out(self):
        load = ("\n[150.000000 I t1 nvn-render] nvn.replay: loading start (present 1, 1 draws)"
                "\n[165.000000 I t1 nvn-render] nvn.replay: loading end (present 9, 40 draws)")
        lines = sorted((log_of(12, 60) + load).splitlines(), key=lambda x: float(a._TIME.match(x)[1]))
        # 148 s to 168 s covers the windows ending at 150, 160 and 170 s
        self.assertEqual(len(a.fps_windows("\n".join(lines))), 9)

    def test_windows_around_a_target_change_are_left_out(self):
        log = log_of(6, 60) + "\n" + log_of(6, 30, target=30, first=170.0)
        targets = [t for t, _, _ in a.fps_windows(log)]
        self.assertEqual(targets, [60] * 5 + [30] * 5)

    def test_no_clock_line_no_judgement(self):
        self.assertEqual(a.fps_windows(summary(120.0, 60.0)), [])


class ExcerptTest(unittest.TestCase):
    def test_exception_report_keeps_its_host_stack(self):
        report = "\n".join(
            ["HOST EXCEPTION 0xC0000005 at host address 0x1 on thread main (tid 1)", *["  memory"] * 40,
             "guest registers:", "  x0 0", "guest frame chain (x29): lr=0x1", "host call stack (innermost first):",
             *[f"  fefw_rt.dll+0x{i:x}" for i in range(32)], "guest call stack (innermost first): f_1", "threads:",
             "  main tid=1", "last files opened (oldest first):"])
        kept = a.crash_excerpt(report)
        self.assertEqual(sum("fefw_rt.dll+0x" in x for x in kept), 32)
        self.assertEqual(sum(x == "  memory" for x in kept), 29)   # 30 header lines with the title
        self.assertEqual(kept[-1], "guest call stack (innermost first): f_1")
        self.assertNotIn("  x0 0", kept)

    def test_thread_report_keeps_the_first_stacks(self):
        threads = [x for name in "abc" for x in (f"thread {name}", f"  #0 {name}.dll+0x1")]
        kept = a.crash_excerpt("\n".join(["FREEZE: no frame tick", *threads]))
        self.assertEqual(kept, ["FREEZE: no frame tick", *threads[:4]])


class SyncTest(unittest.TestCase):
    def test_a_damaged_upload_date_is_repaired(self):
        row = {"id": "aaaa", "created_at": "2026-11-26T00:00:00Z"}
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "aaaa"
            dest.mkdir()
            (dest / "report.json").write_text("{}", encoding="utf-8")
            (dest / "server.json").write_text('{"id": "aa', encoding="utf-8")
            args = argparse.Namespace(out=tmp, offset=0, limit=None)
            with mock.patch.object(f, "_list_page", return_value=[row]), contextlib.redirect_stdout(io.StringIO()):
                f._sync("unused", "unused", args)
            self.assertEqual(json.loads((dest / "server.json").read_text(encoding="utf-8")), row)
            self.assertEqual(sorted(p.name for p in dest.iterdir()), ["report.json", "server.json"])


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        menus = log_of(20, 60, target=60, first=500.0)
        self.add("aaaa", trigger="hitch", build_id="fefw 0.4.1 (abc)", settings={"fefw.fps120": "1"},
                 log=log_of(15, 90, target=120) + "\n" + menus)
        self.add("bbbb", trigger="crash", build_id="0.4.0", exit_code=5, gpu="unknown", settings=["not", "a", "dict"],
                 crash_files={"fefw_crash.txt": "GPU DEVICE LOST\n  GPU: Test GPU (vendor 0x1, x)\nthread main\n  #0 a.dll+0x1\n"},
                 log="[1.000000 E t1 gfx] gfx: bad \x1b[31mthing 7\n[2.000000 E t1 gfx] gfx: bad \x1b[31mthing 8")

    def add(self, name, created="2026-11-26T00:00:00Z", **data):
        d = self.root / name
        d.mkdir()
        (d / "report.json").write_text(json.dumps(data), encoding="utf-8")
        (d / "server.json").write_text(json.dumps({"created_at": created}), encoding="utf-8")

    def run_tool(self, *argv) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            a.main([*argv, "--reports", str(self.root)])
        return out.getvalue()

    def test_fps_judges_only_play_at_the_setting(self):
        result = json.loads(self.run_tool("fps", "--json", "--list"))
        (session,) = result["sessions"]
        self.assertEqual(session["setting"], 120)
        self.assertAlmostEqual(session["reached"], 0.75)
        self.assertEqual(session["at_target"], 0)
        self.assertEqual(result["groups"][0]["group"], {"setting": "120"})

    def test_summary_names_failures_and_gpu(self):
        result = json.loads(self.run_tool("summary", "--json"))
        self.assertEqual(result["failures"], {"GPU DEVICE LOST": ["bbbb"]})
        self.assertEqual(result["counts"]["gpu"]["Test GPU"], 1)
        self.assertEqual(result["counts"]["version"], {"0.4.1": 1, "0.4.0": 1})

    def test_filters(self):
        self.assertIn("1 of 1 reports match", self.run_tool("search", "thing", "--trigger", "crash"))
        self.assertIn("0 of 0 reports match", self.run_tool("search", "thing", "--since", "2026-11-27"))
        self.assertIn("0 of 1 reports match", self.run_tool("search", "thing", "--set", "setting=120"))

    def test_output_has_no_control_characters(self):
        for out in (self.run_tool("search", "thing"), self.run_tool("show", "bb")):
            self.assertNotIn("\x1b", out)
        shown = self.run_tool("show", "bb")
        self.assertIn("2x [2.000000 E t1 gfx]", shown)
        self.assertIn("#0 a.dll+0x1", shown)

    def test_unreadable_report_is_skipped(self):
        (self.root / "cccc").mkdir()
        (self.root / "cccc" / "report.json").write_text("{not json", encoding="utf-8")
        self.assertIn("2 reports", self.run_tool("summary"))


if __name__ == "__main__":
    unittest.main()
