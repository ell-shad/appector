"""Tests for the optional Installed column and Debian-aware version order."""

import ast
import unittest
from pathlib import Path

from appector.models import compare_debian_versions


ROOT = Path(__file__).resolve().parents[1]
WINDOW = ROOT / "appector" / "window.py"
APP = ROOT / "appector" / "app.py"


def _method(path, class_name, method_name):
    module = ast.parse(path.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == method_name:
                    return ast.unparse(child)
    raise AssertionError(f"{class_name}.{method_name} not found in {path}")


class DebianVersionOrderTests(unittest.TestCase):
    def assert_before(self, left, right):
        self.assertEqual(compare_debian_versions(left, right), -1, (left, right))
        self.assertEqual(compare_debian_versions(right, left), 1, (right, left))

    def test_numeric_segments_compare_as_numbers(self):
        self.assert_before("1.9", "1.10")
        self.assert_before("2.0", "10.0")

    def test_tilde_sorts_before_the_release(self):
        self.assert_before("1.0~rc1", "1.0")
        self.assert_before("1.0~~", "1.0~")

    def test_epoch_and_revision_are_significant(self):
        self.assert_before("1.0-1", "1.0-2")
        self.assert_before("1:9.9", "2:1.0")
        self.assert_before("1.0", "1.0-1")

    def test_missing_and_equal_versions_sort_together(self):
        self.assertEqual(compare_debian_versions("", ""), 0)
        self.assertEqual(compare_debian_versions(None, ""), 0)
        self.assertEqual(compare_debian_versions("2:1.0-3", "2:1.0-3"), 0)


class TablePolishContractTests(unittest.TestCase):
    def test_version_column_uses_the_debian_comparator(self):
        source = _method(WINDOW, "MainWindow", "create_column")
        self.assertIn("Gtk.CustomSorter", source)
        self.assertIn("compare_version_items", source)

    def test_installed_column_is_optional_and_remembered(self):
        source = WINDOW.read_text(encoding="utf-8")
        self.assertIn('"installed_at"', source)
        self.assertIn("Show installed column", source)
        self.assertIn('"toggle-installed-column"', source)
        self.assertIn('"show_installed_column"', source)

    def test_spinner_covers_the_whole_scan(self):
        self.assertIn(
            "_set_scanning(True)",
            _method(WINDOW, "MainWindow", "on_reload_clicked"),
        )
        self.assertIn(
            "_set_scanning(False)",
            _method(WINDOW, "MainWindow", "populate_apps"),
        )
        self.assertIn(
            "_set_scanning(False)",
            _method(WINDOW, "MainWindow", "_worker_failed"),
        )

    def test_quit_is_an_application_action(self):
        self.assertRegex(
            _method(APP, "AppectorApp", "__init__"),
            r"SimpleAction\.new\(['\"]quit['\"]",
        )
        self.assertIn('"app.quit"', WINDOW.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
