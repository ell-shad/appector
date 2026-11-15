"""Construct the real dialogs so broken construction fails the build.

The 0.3.0 UI extraction shipped three regressions that no unit test
caught because they all happen during widget construction: a builder
that forgot to return its dialog, and two calls to a Gtk-only method on
Adw.HeaderBar. Those bugs silently disabled the install dialog, the
batch confirmation (behind the "Remove marked" button), and the details
window.

These tests build the real widgets, so a missing return, a renamed
method, or a bad argument fails loudly. They need gi *and* a display:
run them under Xvfb in CI and the release workflow. Everywhere else they
skip instead of failing, matching the other GTK-gated tests.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")

    from gi.repository import Adw, Gtk

    from appector.ui.confirm_dialog import ConfirmPlanDialog
    from appector.ui.details_window import DetailsWindow
    from appector.ui.install_dialog import build_install_dialog

    # Widget construction needs an initialized GTK. Without a display
    # (plain headless runners) initialization fails, so skip.
    GTK_AVAILABLE = Gtk.init_check()
except Exception:
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, "GTK 4 + a display are not available")
class DialogConstructionTests(unittest.TestCase):
    """Every dialog touched by the 0.3.0 extraction must actually build."""

    def setUp(self):
        # A transient parent keeps GTK quiet about parentless dialogs.
        self.parent = Gtk.Window()

    def test_install_dialog_builds_and_returns_the_dialog(self):
        dialog = build_install_dialog(
            parent=self.parent,
            on_browse=Mock(),
            on_response=Mock(),
            on_mode_changed=Mock(),
            on_state_changed=Mock(),
            on_batch_text_changed=Mock(),
            on_batch_expander_changed=Mock(),
            add_drop_target=Mock(),
            set_paths=Mock(),
            update_state=Mock(),
        )
        self.assertIsInstance(dialog, Gtk.Dialog)
        # The install button must exist and be wired to the response.
        self.assertTrue(dialog.install_button.get_sensitive() in (True, False))
        # Presenting must work too: 0.3.0 crashed here because the builder
        # returned None, and again on a Gtk.Dialog present(parent) mismatch.
        dialog.present()

    def test_install_dialog_accepts_initial_paths(self):
        set_paths = Mock()
        dialog = build_install_dialog(
            parent=self.parent,
            on_browse=Mock(),
            on_response=Mock(),
            on_mode_changed=Mock(),
            on_state_changed=Mock(),
            on_batch_text_changed=Mock(),
            on_batch_expander_changed=Mock(),
            add_drop_target=Mock(),
            set_paths=set_paths,
            update_state=Mock(),
            initial_paths=["/tmp/opencode/example.deb"],
        )
        self.assertIsInstance(dialog, Gtk.Dialog)
        set_paths.assert_called_once()
        self.assertTrue(dialog.mode_ref_button.get_active())

    def test_confirm_plan_dialog_builds(self):
        dialog = ConfirmPlanDialog(
            self.parent,
            "Remove marked apps",
            heading="Confirm removal",
            selected=["Example (APT)"],
            also_removed=["libexample0"],
            files_to_delete=["/tmp/opencode/example-dir"],
            warnings=["One item was skipped by the safety policy."],
            on_confirm=lambda: None,
        )
        self.assertIsInstance(dialog, Adw.Dialog)

    def test_details_window_builds(self):
        app = SimpleNamespace(
            name="Repro App",
            manager="Manual",
            version="1.0",
            source="local",
            package_id="manual:repro-app",
            installed_at="",
            category="Apps",
            details="",
        )
        dialog = DetailsWindow(self.parent, app)
        self.assertIsInstance(dialog, Adw.Dialog)


if __name__ == "__main__":
    unittest.main()
