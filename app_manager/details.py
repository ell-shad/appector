import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk, GLib

import threading

from .actions import (
    get_removal_preview,
    can_remove_single,
    execute_removal,
    prepare_apt_removal,
    execute_apt_removal,
    get_removal_size_estimate,
)


class DetailsWindow(Gtk.Window):
    def __init__(self, parent, app):
        super().__init__(
            title="App Details",
            transient_for=parent,
            modal=True,
            default_width=560,
            default_height=600,
        )

        self.app = app
        self.remove_button = None
        self._removal_in_progress = False

        header = Gtk.HeaderBar()
        header.set_show_title_buttons(False)

        title_label = Gtk.Label(label=getattr(app, "name", "App Details"))
        title_label.add_css_class("heading")

        header.set_title_widget(title_label)

        if can_remove_single(app):
            self.remove_button = Gtk.Button(label="Remove")
            self.remove_button.add_css_class("destructive-action")
            self.remove_button.connect("clicked", self.on_remove_clicked)
            header.pack_start(self.remove_button)

        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", self.on_close_clicked)
        header.pack_end(close_button)

        self.set_titlebar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)

        grid = Gtk.Grid()
        grid.set_column_spacing(18)
        grid.set_row_spacing(10)
        grid.set_hexpand(True)
        grid.set_vexpand(True)

        installed_value = getattr(app, "installed_at", "") or "Unknown"

        fields = [
            ("Name", getattr(app, "name", "") or "-"),
            ("Manager", getattr(app, "manager", "") or "-"),
            ("Source", getattr(app, "source", "") or "-"),
            ("Package ID", getattr(app, "package_id", "") or "-"),
            ("Version", getattr(app, "version", "") or "-"),
            ("Installed", installed_value),
            ("Category", getattr(app, "category", "") or "-"),
            ("Details", getattr(app, "details", "") or "-"),
            ("Removal preview", get_removal_preview(app)),
        ]

        for i, (label_text, value_text) in enumerate(fields):
            key_label = Gtk.Label(label=label_text)
            key_label.add_css_class("dim-label")
            key_label.set_halign(Gtk.Align.START)
            key_label.set_valign(Gtk.Align.START)
            key_label.set_xalign(0.0)

            value_label = Gtk.Label(label=str(value_text))
            value_label.set_wrap(True)
            value_label.set_selectable(True)
            value_label.set_halign(Gtk.Align.START)
            value_label.set_valign(Gtk.Align.START)
            value_label.set_xalign(0.0)
            value_label.set_hexpand(True)

            grid.attach(key_label, 0, i, 1, 1)
            grid.attach(value_label, 1, i, 1, 1)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(grid)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)

        content.append(scrolled)
        self.set_child(content)

    def on_close_clicked(self, button):
        self.close()

    # ------------------------------------------------------------
    # Removal entry point
    # ------------------------------------------------------------

    def on_remove_clicked(self, button):
        if self._removal_in_progress:
            return

        manager = getattr(self.app, "manager", "")

        if manager == "APT":
            self._removal_in_progress = True

            if self.remove_button:
                self.remove_button.set_sensitive(False)

            thread = threading.Thread(
                target=self.apt_simulation_worker,
                daemon=True,
            )
            thread.start()
        else:
            self.show_non_apt_confirm()

    # ------------------------------------------------------------
    # Non-APT removal flow
    # ------------------------------------------------------------

    def show_non_apt_confirm(self):
        name = getattr(self.app, "name", "this app")
        preview = get_removal_preview(self.app)
        preview += "\n\n" + get_removal_size_estimate([self.app])

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
        )

        dialog.set_property("message-type", Gtk.MessageType.WARNING)
        dialog.set_property("text", f"Remove {name}?")
        dialog.set_property("secondary-text", preview)

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)

        remove_button = dialog.add_button("Remove", Gtk.ResponseType.OK)
        remove_button.add_css_class("destructive-action")

        dialog.connect("response", self.on_non_apt_confirm_response)
        dialog.present()

    def on_non_apt_confirm_response(self, dialog, response):
        dialog.close()

        if response != Gtk.ResponseType.OK:
            return

        self._removal_in_progress = True

        if self.remove_button:
            self.remove_button.set_sensitive(False)

        thread = threading.Thread(target=self.remove_worker, daemon=True)
        thread.start()

    def remove_worker(self):
        success, message = execute_removal(self.app)
        GLib.idle_add(self.on_removal_finished, success, message)

    # ------------------------------------------------------------
    # APT removal flow
    # ------------------------------------------------------------

    def apt_simulation_worker(self):
        success, message, removed_packages, warnings = prepare_apt_removal(self.app)

        GLib.idle_add(
            self.on_apt_simulation_finished,
            success,
            message,
            removed_packages,
            warnings,
        )

    def on_apt_simulation_finished(self, success, message, removed_packages, warnings):
        self._removal_in_progress = False

        if self.remove_button:
            self.remove_button.set_sensitive(True)

        if not success:
            self.show_result(False, message)
            return False

        text = message

        if warnings:
            text += "\n\nWarnings:\n"
            text += warnings

        name = getattr(self.app, "name", "this package")

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
        )

        dialog.set_property("message-type", Gtk.MessageType.WARNING)
        dialog.set_property("text", f"Remove {name} using APT?")
        dialog.set_property("secondary-text", text)

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)

        remove_button = dialog.add_button("Remove", Gtk.ResponseType.OK)
        remove_button.add_css_class("destructive-action")

        dialog.connect("response", self.on_apt_confirm_response)
        dialog.present()

        return False

    def on_apt_confirm_response(self, dialog, response):
        dialog.close()

        if response != Gtk.ResponseType.OK:
            return

        self._removal_in_progress = True

        if self.remove_button:
            self.remove_button.set_sensitive(False)

        thread = threading.Thread(target=self.apt_removal_worker, daemon=True)
        thread.start()

    def apt_removal_worker(self):
        success, message = execute_apt_removal(self.app)
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
        else:
            if self.remove_button:
                self.remove_button.set_sensitive(True)

        self.show_result(success, message)

        if success:
            parent = self.get_transient_for()
            if parent and hasattr(parent, "reload"):
                parent.reload()

        return False

    def show_result(self, success, message):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
        )

        if success:
            dialog.set_property("message-type", Gtk.MessageType.INFO)
            dialog.set_property("text", "Removal completed")
        else:
            dialog.set_property("message-type", Gtk.MessageType.ERROR)
            dialog.set_property("text", "Removal failed")

        dialog.set_property("secondary-text", message)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)

        dialog.connect("response", lambda d, response: d.close())
        dialog.present()
