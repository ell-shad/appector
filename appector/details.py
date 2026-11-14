"""App details window (U9 redesign).

The removal flows (APT simulation -> confirm -> helper; non-APT confirm;
AppImage/Manual validation in execute_removal) keep their behaviour; they
are presented on the redesigned card layout from
`appector/ui/details_window.py`, use ConfirmPlanDialog (U1) and run_async
workers (U3) instead of raw threads.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk, GLib

from .actions import (
    execute_apt_removal,
    execute_removal,
    get_removal_preview,
    get_removal_size_estimate,
    prepare_apt_removal,
)
from .ui.confirm_dialog import ConfirmPlanDialog
from .ui.details_window import DetailsWindow as _DetailsCard
from .ui.tasks import run_async


class DetailsWindow(_DetailsCard):
    """Details card with the removal flows attached."""

    def __init__(self, parent, app):
        super().__init__(parent, app, on_remove=self.start_removal)
        self._parent_window = parent

    # ------------------------------------------------------------
    # Removal entry point
    # ------------------------------------------------------------

    def start_removal(self, _dialog=None):
        if self._removal_in_progress:
            return

        manager = getattr(self.app, "manager", "")

        if manager == "APT":
            self._removal_in_progress = True
            if self.remove_button:
                self.remove_button.set_sensitive(False)
            run_async(
                self.apt_simulation_worker,
                on_error=lambda error: self._recovery(f"{error}"),
                name="details-apt-preview",
            )
        else:
            self.show_non_apt_confirm()

    def _recovery(self, message):
        """A crashed worker must re-enable the Remove button."""
        self._removal_in_progress = False
        if self.remove_button:
            self.remove_button.set_sensitive(True)
        self.show_result(False, f"The removal preview failed:\n{message}")
        return False

    # ------------------------------------------------------------
    # Non-APT removal flow
    # ------------------------------------------------------------

    def _removal_files(self):
        paths = getattr(self.app, "removal_paths", None) or []
        if isinstance(paths, str):
            paths = paths.splitlines()
        return [str(path) for path in paths] or None

    def show_non_apt_confirm(self):
        name = getattr(self.app, "name", "this app")

        def on_confirm():
            self._removal_in_progress = True
            if self.remove_button:
                self.remove_button.set_sensitive(False)
            run_async(
                self.removal_worker,
                on_error=lambda error: self._recovery(f"{error}"),
                name="details-removal",
            )

        dialog = ConfirmPlanDialog(
            self._parent_window,
            f"Remove {name}?",
            heading="Review what will be removed before continuing.",
            selected=[f"{name} ({getattr(self.app, 'manager', '')})"],
            files_to_delete=self._removal_files(),
            notes=[get_removal_preview(self.app)],
            confirm_label="Remove",
            destructive=True,
            on_confirm=on_confirm,
        )
        dialog.set_disk_estimate_async(
            lambda: get_removal_size_estimate([self.app])
        )
        dialog.present(self._parent_window)

    def removal_worker(self):
        success, message = execute_removal(self.app)
        GLib.idle_add(self.on_removal_finished, success, message)

    # ------------------------------------------------------------
    # APT removal flow
    # ------------------------------------------------------------

    def apt_simulation_worker(self):
        success, message, plan, warnings = prepare_apt_removal(self.app)
        GLib.idle_add(
            self.on_apt_simulation_finished,
            success,
            message,
            plan,
            warnings,
        )

    def on_apt_simulation_finished(self, success, message, plan=None, warnings=None):
        warnings = warnings or ""
        self._apt_plan = plan
        self._removal_in_progress = False

        if self.remove_button:
            self.remove_button.set_sensitive(True)

        if not success:
            self.show_result(False, message)
            return False

        name = getattr(self.app, "name", "this package")

        # The simulation's bullet list is the exact "also removed" set.
        removed_preview = [
            line[2:] for line in message.splitlines()
            if line.startswith("• ")
        ]

        def on_confirm():
            self._removal_in_progress = True
            if self.remove_button:
                self.remove_button.set_sensitive(False)
            run_async(
                self.apt_removal_worker,
                on_error=lambda error: self._recovery(f"{error}"),
                name="details-apt-removal",
            )

        dialog = ConfirmPlanDialog(
            self._parent_window,
            f"Remove {name} using APT?",
            heading=(
                "APT simulation preview — no changes have been made. "
                "Everything the simulation says will be removed is listed "
                "below; the list is complete and copyable."
            ),
            selected=[name],
            also_removed=[
                entry for entry in removed_preview if entry != name
            ] or None,
            warnings=[warnings] if warnings else None,
            notes=[message],
            confirm_label="Remove",
            destructive=True,
            on_confirm=on_confirm,
        )
        dialog.present(self._parent_window)
        return False

    def apt_removal_worker(self):
        success, message = execute_apt_removal(
            self.app,
            plan=getattr(self, "_apt_plan", None),
        )
        GLib.idle_add(self.on_removal_finished, success, message)

    # ------------------------------------------------------------
    # Removal result
    # ------------------------------------------------------------

    def on_removal_finished(self, success, message):
        self._removal_in_progress = False

        if success:
            if self.remove_button:
                self.remove_button.set_label("Removed")
                self.remove_button.set_sensitive(False)
        elif self.remove_button:
            self.remove_button.set_sensitive(True)

        self.show_result(success, message)

        if success:
            parent = self._parent_window
            if parent and hasattr(parent, "reload"):
                GLib.idle_add(parent.reload)
            self.close()

        return False

    def show_result(self, success, message):
        GLib.idle_add(self._show_result_idle, success, message)
        return False

    @staticmethod
    def _show_result_idle(success, message):
        try:
            dialog = Gtk.MessageDialog(
                transient_for=None,
                modal=True,
            )
            dialog.set_property(
                "message-type",
                Gtk.MessageType.INFO if success else Gtk.MessageType.ERROR,
            )
            dialog.set_property(
                "text", "Removal completed" if success else "Removal failed"
            )
            dialog.set_property("secondary-text", message)
            dialog.add_button("Close", Gtk.ResponseType.CLOSE)
            dialog.connect("response", lambda d, _r: d.close())
            dialog.present()
        except Exception:
            pass
        return False
