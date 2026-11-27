#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of check_menu_translations.py:

python3 tools/check_menu_translations_test.py
"""

import tempfile
import unittest
from pathlib import Path

import check_menu_translations as c


def accepted(key: str, value: str) -> bool:
    return c.translation_problem(key, value) is None


class SpecifierTest(unittest.TestCase):
    """The cases of runtime/tests/gfx/test_menu_translation.cpp, and the rules of locales/README.md."""

    def test_matching_specifiers_are_accepted(self):
        self.assertTrue(accepted("every %d s", "chaque %d s"))
        self.assertTrue(accepted("avg %.2f ms  99%% %.1f ms", "moy. %.2f ms  99%% %.1f ms"))
        self.assertTrue(accepted("% of one thread", "单线程占用"))
        self.assertTrue(accepted("% of one thread", "% of a thread"))
        self.assertTrue(accepted("100% (native)", "100%（原生）"))

    def test_different_specifiers_are_refused(self):
        self.assertFalse(accepted("every %d s", "chaque %s s"))
        self.assertFalse(accepted("every %d s", "chaque seconde"))
        self.assertFalse(accepted("%s: %d", "%d: %s"))

    def test_star_and_length_count(self):
        self.assertTrue(accepted("%*d and %.*f", "%*d et %.*f"))
        self.assertFalse(accepted("%*d", "%d"))
        self.assertFalse(accepted("%.*f", "%.2f"))
        self.assertFalse(accepted("%llu", "%u"))

    def test_a_space_is_a_printf_flag(self):
        self.assertFalse(accepted("off", "99% de moins"))
        self.assertFalse(accepted("avg %.1f ms", "99% de %.1f ms"))
        self.assertFalse(accepted("% of one thread", "% d"))

    def test_percent_n_is_refused_in_every_spelling(self):
        for value in ("%n", "% n", "%- n", "%ln"):
            self.assertFalse(accepted("off", value), value)


class TableTest(unittest.TestCase):
    def test_dropped_and_empty_lines_do_not_count(self):
        with tempfile.TemporaryDirectory() as folder:
            table = Path(folder) / "xx.ini"
            table.write_text("# comment\nevery %d s = chaque s\nEmpty =\na = b = c\nx = y\tz\n", encoding="utf-8")
            keys, dropped = c.read_table(table)
        self.assertEqual(keys, {"a", "x = y"})
        self.assertEqual([(number, key) for number, key, _ in dropped], [(2, "every %d s")])

    def test_only_what_the_menu_trims_is_trimmed(self):
        with tempfile.TemporaryDirectory() as folder:
            table = Path(folder) / "xx.ini"
            table.write_bytes(" \ta \t= x \r\n\u00a0b = x\nc\u3000= x\nd = \u3000\n\ufeffe = x\n".encode("utf-8"))
            keys, _ = c.read_table(table)
        self.assertEqual(keys, {"a", "\u00a0b", "c\u3000", "d", "e"})


class SourceTest(unittest.TestCase):
    def strings(self, code: str) -> set:
        return {text for text, _ in c.menu_strings(code)[0]}

    def test_literals_lists_and_either_side_of_a_condition(self):
        code = '''tr("a" " b"); tr(on ? "yes" : "no"); tr(x == "compared" ? y : "z"); m.help = "h";
                  constexpr const char* kFooNames[] = {"one", "two"}; // tr("comment")'''
        self.assertEqual(self.strings(code), {"a b", "yes", "no", "z", "h", "one", "two"})

    def test_ini_combo_row_fallback_label(self):
        code = 'ini_combo_row(m, k, "Label", "Help", names, vals, 5, "Other");'
        self.assertEqual(self.strings(code), {"Label", "Help", "Other"})
        code = '''ini_combo_row(m, k, "Label", "Help", std::array<const char*, 5>{"x"}.data(),
                                std::array<const char*, 5>{"1"}.data(), 5, "Other");'''
        self.assertEqual(self.strings(code), {"Label", "Help", "Other"})
        code = 'ini_combo_row(m, k, "Label", "Help", names, vals, 5);'
        self.assertEqual(self.strings(code), {"Label", "Help"})

    def test_a_default_value_in_a_declaration(self):
        code = '''void ini_combo_row(Menu& m, const IniKey& k, const char* label, const char* help, const char* const* names,
                                     const char* const* vals, int n, const char* other = "Default label") {}'''
        self.assertEqual(self.strings(code), {"Default label"})


if __name__ == "__main__":
    unittest.main()
