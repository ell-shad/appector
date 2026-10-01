import gi
import shutil
import json
from pathlib import Path

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gtk, Adw, Gio, GLib, Gdk, GdkPixbuf, Pango

import threading

from .scanners import scan_all
from .app_item import AppItem
from .details import DetailsWindow
from .progress import ProgressWindow, InstallProgressWindow
from .actions import (
    app_key,
    can_remove,
    execute_batch_removal,
    prepare_apt_batch_preview,
    install_deb,
    add_flathub_remote,
    flathub_remote_exists,
    install_flatpak_source,
    install_deb_batch,
    install_flatpak_ref_batch,
    purge_leftover_configs,
    get_apt_autoremove_preview,
    execute_apt_autoremove,
    get_flatpak_unused_preview,
    execute_flatpak_unused_cleanup,
    get_removal_risk,

)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(
            application=app,
            title="App Manager",
            default_width=1250,
            default_height=760,
        )

        self.search_text = ""
        self.marked_only = False
        self.duplicates_only = False
        self.show_leftovers = False
        self.show_advanced_apps = False
        self.hide_basic_apps = False
        self._updating_select_all_leftovers = False
        self.current_apps = []
        self.marked_keys = set()
        self.context_item = None
        self.selected_count = 0
        self.selected_marked_count = 0
        self.removable_marked_count = 0
        self.progress_window = None
        self.install_progress_window = None
        self._selection_ui_pending = False

        self._setup_actions()

        # ------------------------------------------------------------
        # Header
        # ------------------------------------------------------------
        header = Adw.HeaderBar()

        self.window_title = Adw.WindowTitle(
            title="App Manager",
            subtitle="Unified installed app inventory",
        )

        header.set_title_widget(self.window_title)

        self.log_button = Gtk.Button(label="Log")
        self.log_button.set_tooltip_text("Show recent action log")
        self.log_button.add_css_class("flat")
        self.log_button.connect("clicked", self.on_show_log_clicked)
        header.pack_end(self.log_button)

        self.reload_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.reload_button.set_tooltip_text("Refresh")
        self.reload_button.add_css_class("flat")
        self.reload_button.connect("clicked", self.on_reload_clicked)

        try:
            self.reload_button.add_css_class("circular")
        except Exception:
            pass

        header.pack_end(self.reload_button)

        # ------------------------------------------------------------
        # Data model
        # ------------------------------------------------------------
        self.list_store = Gio.ListStore(item_type=AppItem)

        self.custom_filter = Gtk.CustomFilter.new(self.filter_func)
        self.filter_model = Gtk.FilterListModel.new(self.list_store, self.custom_filter)

        self.sort_model = Gtk.SortListModel.new(self.filter_model)

        self.selection = Gtk.MultiSelection.new(self.sort_model)
        self.selection.connect("selection-changed", self.on_selection_changed)

        # ------------------------------------------------------------
        # Column view
        # ------------------------------------------------------------
        self.column_view = Gtk.ColumnView(model=self.selection)
        self.column_view.set_show_column_separators(True)

        try:
            self.column_view.set_show_row_separators(True)
        except AttributeError:
            pass

        try:
            self.column_view.set_reorderable(False)
        except AttributeError:
            pass

        self.column_view.connect("activate", self.on_activate)
        self.column_view.connect("notify::sorter", self.on_sorter_changed)

        self.column_view.set_hexpand(True)
        self.column_view.set_vexpand(True)

        marked_column = self.create_check_column()
        name_column = self.create_name_column()

        source_column = self.create_column(
            "Source",
            "source",
            expand=False,
            min_width=180,
            fixed_width=220,
        )

        installed_column = self.create_column(
            "Installed",
            "installed_at",
            expand=False,
            min_width=130,
            fixed_width=150,
        )

        manager_column = self.create_column(
            "Manager",
            "manager",
            expand=False,
            min_width=110,
            fixed_width=120,
        )

        version_column = self.create_column(
            "Version",
            "version",
            expand=False,
            min_width=120,
            fixed_width=160,
        )

        for column in [
            marked_column,
            name_column,
            source_column,
            installed_column,
            manager_column,
            version_column,
        ]:
            self.column_view.append_column(column)
        self.column_widgets = {
            "marked": marked_column,
            "name": name_column,
            "source": source_column,
            "installed": installed_column,
            "manager": manager_column,
            "version": version_column,
        }

        self.restore_ui_state()
        self.connect("close-request", self.on_close_request)

        try:
            self.column_view.sort_by_column(name_column, Gtk.SortType.ASCENDING)
        except AttributeError:
            self.sort_model.set_sorter(name_column.get_sorter())

        sorter = self.column_view.get_sorter()
        if sorter:
            self.sort_model.set_sorter(sorter)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.column_view)

        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        self.main_stack = Gtk.Stack()
        self.main_stack.set_hexpand(True)
        self.main_stack.set_vexpand(True)

        self.empty_page = Adw.StatusPage()
        self.empty_page.set_icon_name("edit-find-symbolic")
        self.empty_page.set_title("No apps found")
        self.empty_page.set_description("Try changing your search or filters.")

        self.main_stack.add_named(scrolled, "list")
        self.main_stack.add_named(self.empty_page, "empty")

        self.filter_model.connect("items-changed", self.on_filter_items_changed)

        # ------------------------------------------------------------
        # Context menu
        # ------------------------------------------------------------
        self._setup_context_menu()

        # ------------------------------------------------------------
        # Contextual selection action bar
        # ------------------------------------------------------------
        self.action_bar = Gtk.ActionBar()

        self.selection_label = Gtk.Label(label="0 selected")
        self.selection_label.add_css_class("dim-label")
        self.selection_label.set_margin_end(12)

        self.mark_button = Gtk.Button(label="Mark")
        self.mark_button.set_tooltip_text("Mark selected apps for removal")
        self.mark_button.add_css_class("suggested-action")
        self.mark_button.add_css_class("pill")
        self.mark_button.set_sensitive(False)
        self.mark_button.connect("clicked", self.on_mark_clicked)

        self.unmark_button = Gtk.Button(label="Unmark")
        self.unmark_button.set_tooltip_text("Unmark selected apps")
        self.unmark_button.add_css_class("pill")
        self.unmark_button.set_sensitive(False)
        self.unmark_button.connect("clicked", self.on_unmark_clicked)

        self.action_bar.pack_start(self.selection_label)
        self.action_bar.pack_start(self.mark_button)
        self.action_bar.pack_start(self.unmark_button)

        self.action_revealer = Gtk.Revealer()
        self.action_revealer.set_child(self.action_bar)
        self.action_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.action_revealer.set_reveal_child(False)

        # ------------------------------------------------------------
        # Sidebar
        # ------------------------------------------------------------
        self.sidebar_expanded = True
        self.sidebar = self._build_sidebar()

        self.sidebar_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.sidebar_wrap.set_hexpand(False)

        self.sidebar_wrap.append(self.sidebar)
        self.sidebar_wrap.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # ------------------------------------------------------------
        # Main content
        # ------------------------------------------------------------
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.set_hexpand(True)
        main_box.set_vexpand(True)

        main_box.append(self.main_stack)
        main_box.append(self.action_revealer)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        content.set_hexpand(True)
        content.set_vexpand(True)

        content.append(self.sidebar_wrap)
        content.append(main_box)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(header)
        outer.append(content)
        outer.set_hexpand(True)
        outer.set_vexpand(True)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(outer)
        self.set_content(self.toast_overlay)

        # Initial scan
        self.on_reload_clicked(self.reload_button)

    def _ui_state_path(self):
        return self._state_dir() / "ui.json"

    def load_ui_state(self):
        try:
            path = self._ui_state_path()
            data = json.loads(path.read_text(encoding="utf-8"))

            if isinstance(data, dict):
                return data
        except Exception:
            pass

        return {}

    def save_ui_state(self):
        try:
            state = self.load_ui_state()

            if hasattr(self, "column_widgets"):
                widths = {}

                for title, column in self.column_widgets.items():
                    try:
                        width = column.get_width()

                        if width > 0:
                            widths[title] = width
                    except Exception:
                        continue

                state["column_widths"] = widths

            try:
                state["window_size"] = [
                    self.get_width(),
                    self.get_height(),
                ]
            except Exception:
                pass

            self._state_dir().mkdir(parents=True, exist_ok=True)

            self._ui_state_path().write_text(
                json.dumps(state, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def restore_ui_state(self):
        state = self.load_ui_state()

        size = state.get("window_size")

        if isinstance(size, list) and len(size) == 2:
            try:
                self.set_default_size(int(size[0]), int(size[1]))
            except Exception:
                pass

        widths = state.get("column_widths", {})

        if isinstance(widths, dict) and hasattr(self, "column_widgets"):
            for title, width in widths.items():
                column = self.column_widgets.get(title)

                if column:
                    try:
                        column.set_fixed_width(int(width))
                    except Exception:
                        continue

    def on_close_request(self, *args):
        self.save_ui_state()
        return False

    def show_toast(self, message):
        try:
            toast = Adw.Toast.new(message)
            toast.set_timeout(3)
            self.toast_overlay.add_toast(toast)
        except Exception:
            self.show_message(
                "App Manager",
                message,
                Gtk.MessageType.INFO,
            )
            
    def on_filter_items_changed(self, *args):
        if getattr(self, "_empty_state_pending", False):
            return

        self._empty_state_pending = True
        GLib.idle_add(self._update_empty_state_idle)

    def _update_empty_state_idle(self):
        self._empty_state_pending = False

        try:
            self.update_empty_state()
        except Exception:
            pass

        return False

    def update_empty_state(self):
        if not hasattr(self, "main_stack") or not hasattr(self, "empty_page"):
            return

        try:
            visible_count = self.filter_model.get_n_items()
        except Exception:
            return

        if visible_count > 0:
            self.main_stack.set_visible_child_name("list")
            return

        title = "No apps found"
        description = "Try changing your search or filters."

        if getattr(self, "search_text", ""):
            title = "No results"
            description = f"No apps match “{self.search_text}”."

        elif getattr(self, "duplicates_only", False):
            title = "No duplicates found"
            description = "Your installed apps do not appear to have duplicates."

        elif getattr(self, "marked_only", False):
            title = "No marked apps"
            description = "Mark apps to see them here."

        elif getattr(self, "show_leftovers", False):
            title = "No leftovers found"
            description = "No leftover configuration packages were detected."

        self.empty_page.set_title(title)
        self.empty_page.set_description(description)
        self.main_stack.set_visible_child_name("empty")

    def on_show_log_clicked(self, button=None):
        log_path = self._state_dir() / "actions.log"

        lines = []

        if log_path.exists():
            try:
                lines = log_path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                ).splitlines()

                # Show only the latest 500 lines.
                lines = lines[-500:]
            except Exception:
                lines = ["Failed to read log file."]
        else:
            lines = ["No actions logged yet."]

        dialog = Gtk.Dialog(
            transient_for=self,
            modal=True,
            title="Action Log",
            default_width=720,
            default_height=480,
        )

        dialog.set_resizable(True)

        dialog.add_button("Close", Gtk.ResponseType.CLOSE)

        content = dialog.get_content_area()

        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(8)
        content.set_margin_end(8)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        textview = Gtk.TextView()
        textview.set_editable(False)
        textview.set_monospace(True)

        buffer = textview.get_buffer()
        buffer.set_text("\n".join(lines))

        scrolled.set_child(textview)
        content.append(scrolled)

        dialog.connect("response", lambda d, response: d.close())
        dialog.present()

    # ------------------------------------------------------------
    # .deb installation
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # .deb batch installation
    # ------------------------------------------------------------

    def on_install_deb_clicked(self, button=None):
        if not shutil.which("apt-get"):
            self.show_message(
                "APT not available",
                "The apt-get command was not found on this system.",
                Gtk.MessageType.WARNING,
            )
            return

        if hasattr(Gtk, "FileDialog"):
            file_dialog = Gtk.FileDialog.new()
            file_dialog.set_title("Select .deb packages")

            deb_filter = Gtk.FileFilter()
            deb_filter.set_name("Debian packages (*.deb)")
            deb_filter.add_pattern("*.deb")

            all_filter = Gtk.FileFilter()
            all_filter.set_name("All files")
            all_filter.add_pattern("*")

            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(deb_filter)
            filters.append(all_filter)

            file_dialog.set_filters(filters)

            if hasattr(file_dialog, "open_multiple"):
                def callback(fd, result):
                    try:
                        files_model = fd.open_multiple_finish(result)
                    except Exception:
                        return

                    paths = []

                    if files_model:
                        for i in range(files_model.get_n_items()):
                            file = files_model.get_item(i)

                            if file:
                                path = file.get_path()

                                if path:
                                    paths.append(path)

                    if paths:
                        self.show_deb_batch_install_confirm(paths)

                self._deb_file_dialog_callback = callback
                file_dialog.open_multiple(self, None, callback)
                return

        # Fallback for older GTK file chooser
        chooser = Gtk.FileChooserDialog(
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.OPEN,
        )

        chooser.set_property("title", "Select .deb packages")
        chooser.set_select_multiple(True)

        chooser.add_button("Cancel", Gtk.ResponseType.CANCEL)
        chooser.add_button("Open", Gtk.ResponseType.OK)

        deb_filter = Gtk.FileFilter()
        deb_filter.set_name("Debian packages (*.deb)")
        deb_filter.add_pattern("*.deb")

        chooser.add_filter(deb_filter)

        chooser.connect("response", self.on_deb_file_chooser_response)
        chooser.present()

    def on_deb_file_chooser_response(self, chooser, response):
        if response == Gtk.ResponseType.OK:
            files = chooser.get_files()
            paths = []

            if files:
                for i in range(files.get_n_items()):
                    file = files.get_item(i)

                    if file:
                        path = file.get_path()

                        if path:
                            paths.append(path)

            chooser.close()

            if paths:
                self.show_deb_batch_install_confirm(paths)
        else:
            chooser.close()

    def show_deb_batch_install_confirm(self, paths):
        dialog = Gtk.Dialog(
            transient_for=self,
            modal=True,
        )

        dialog.set_resizable(False)
        dialog.set_property("title", "Install .deb packages")

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)

        install_button = dialog.add_button("Install", Gtk.ResponseType.OK)
        install_button.add_css_class("suggested-action")

        content = dialog.get_content_area()
        content.set_spacing(8)

        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        label = Gtk.Label(label=f"{len(paths)} package(s) selected:")
        label.set_xalign(0.0)

        lines = []

        for path in paths[:20]:
            lines.append(f"• {Path(path).name}")

        if len(paths) > 20:
            lines.append(f"• …and {len(paths) - 20} more")

        path_label = Gtk.Label(label="\n".join(lines))
        path_label.set_wrap(True)
        path_label.set_selectable(True)
        path_label.set_xalign(0.0)

        dialog.delete_check = Gtk.CheckButton(
            label="Delete .deb files after successful installation"
        )

        content.append(label)
        content.append(path_label)
        content.append(dialog.delete_check)

        dialog.connect(
            "response",
            self.on_deb_batch_install_confirm_response,
            paths,
        )

        dialog.present()

    def on_deb_batch_install_confirm_response(self, dialog, response, paths):
        if response == Gtk.ResponseType.OK:
            delete_source = dialog.delete_check.get_active()
            dialog.close()
            self.start_deb_batch_install(paths, delete_source)
        else:
            dialog.close()

    def start_deb_batch_install(self, paths, delete_source=False):
        label = f"{len(paths)} .deb package(s)"

        self.install_progress_window = InstallProgressWindow(self, "Installing .deb packages")
        self.install_progress_window.present()
        self.install_progress_window.start_pulse()
        self.install_progress_window.set_status(f"Installing {label}…")

        thread = threading.Thread(
            target=self.deb_batch_install_worker,
            args=(
                paths,
                delete_source,
            ),
            daemon=True,
        )

        thread.start()

    def deb_batch_install_worker(self, paths, delete_source):
        def output_callback(line):
            if self.install_progress_window:
                GLib.idle_add(
                    self.install_progress_window.append_output,
                    line,
                )

        success, message = install_deb_batch(
            paths,
            output_callback=output_callback,
            delete_source=delete_source,
        )

        GLib.idle_add(
            self.on_deb_batch_install_finished,
            success,
            message,
        )

    def on_deb_batch_install_finished(self, success, message):
        if self.install_progress_window:
            self.install_progress_window.stop_pulse()

            if success:
                self.install_progress_window.close_window()
                self.install_progress_window = None
                self.reload()

                self.show_message(
                    "Installation finished",
                    ".deb packages installed successfully.",
                    Gtk.MessageType.INFO,
                )
            else:
                self.install_progress_window.finish_failure(message)

        return False

    def on_show_leftovers_toggled(self, button):
        self.show_leftovers = button.get_active()
        self._unmark_hidden_advanced_items()
        self.rebuild_list()

    def on_show_advanced_apps_toggled(self, button):
        self.show_advanced_apps = button.get_active()
        self._unmark_hidden_advanced_items()
        self.rebuild_list()

    def on_hide_basic_apps_toggled(self, button):
        self.hide_basic_apps = button.get_active()
        self._unmark_hidden_advanced_items()
        self.rebuild_list()
                    
    # ------------------------------------------------------------
    # Flatpak installation
    # ------------------------------------------------------------

    def on_install_flatpak_clicked(self, button=None):
        if not shutil.which("flatpak"):
            self.show_message(
                "Flatpak not available",
                "The flatpak command was not found on this system.",
                Gtk.MessageType.WARNING,
            )
            return

        dialog = Gtk.Dialog(
            transient_for=self,
            modal=True,
        )
        dialog.set_resizable(False)
        dialog.set_property("title", "Install Flatpak")
        dialog.set_default_size(520, -1)

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)

        dialog.add_flatpak_button = dialog.add_button(
            "Add Flathub",
            Gtk.ResponseType.APPLY,
        )

        dialog.install_button = dialog.add_button(
            "Install",
            Gtk.ResponseType.OK,
        )

        dialog.install_button.add_css_class("suggested-action")

        content = dialog.get_content_area()
        content.set_spacing(8)

        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        # Mode selection
        dialog.mode_id_button = Gtk.CheckButton(label="Flathub app ID")
        dialog.mode_ref_button = Gtk.CheckButton(label=".flatpakref file or URL")

        dialog.mode_ref_button.set_group(dialog.mode_id_button)
        dialog.mode_id_button.set_active(True)

        # App ID entry
        dialog.id_entry = Gtk.Entry()
        dialog.id_entry.set_placeholder_text("org.gimp.GIMP")
        dialog.id_entry.set_hexpand(True)

        # Flatpakref entry + browse
        dialog.file_entry = Gtk.Entry()
        dialog.file_entry.set_placeholder_text("/path/to/app.flatpakref")
        dialog.file_entry.set_hexpand(True)

        browse_button = Gtk.Button(label="Browse…")
        browse_button.connect("clicked", self.on_flatpakref_browse_clicked, dialog)

        # Scope
        dialog.user_check = Gtk.CheckButton(label="Install for current user only")
        dialog.user_check.set_active(True)
        dialog.delete_source_check = Gtk.CheckButton(
            label="Delete .flatpakref file after successful installation"
        )
        dialog.delete_source_check.set_sensitive(False)        
        dialog.flatpakref_paths = []
        dialog._setting_file_entry = False
        # Status
        dialog.status_label = Gtk.Label(label="")
        dialog.status_label.add_css_class("dim-label")
        dialog.status_label.set_wrap(True)
        dialog.status_label.set_xalign(0.0)

        # Layout
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        mode_box.append(dialog.mode_id_button)
        mode_box.append(dialog.mode_ref_button)

        id_label = Gtk.Label(label="Flatpak app ID:")
        id_label.set_xalign(0.0)

        dialog.id_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        dialog.id_box.append(id_label)
        dialog.id_box.append(dialog.id_entry)

        ref_label = Gtk.Label(label=".flatpakref file or URL:")
        ref_label.set_xalign(0.0)

        ref_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ref_row.append(dialog.file_entry)
        ref_row.append(browse_button)

        dialog.ref_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        dialog.ref_box.append(ref_label)
        dialog.ref_box.append(ref_row)
        dialog.ref_box.append(dialog.delete_source_check)

        content.append(mode_box)
        content.append(dialog.id_box)
        content.append(dialog.ref_box)
        content.append(dialog.user_check)
        content.append(dialog.status_label)

        dialog.mode_id_button.connect(
            "toggled",
            self.on_flatpak_install_mode_changed,
            dialog,
        )

        dialog.user_check.connect(
            "toggled",
            self.on_flatpak_install_state_changed,
            dialog,
        )

        dialog.id_entry.connect(
            "changed",
            self.on_flatpak_install_state_changed,
            dialog,
        )

        dialog.file_entry.connect(
            "changed",
            self.on_flatpak_install_state_changed,
            dialog,
        )

        dialog.connect("response", self.on_flatpak_install_response)

        self.update_flatpak_install_dialog_state(dialog)
        dialog.present()

    def on_flatpak_install_mode_changed(self, button, dialog):
        self.update_flatpak_install_dialog_state(dialog)

    def on_flatpak_install_state_changed(self, widget, dialog):
        if isinstance(widget, Gtk.Entry):
            if not getattr(dialog, "_setting_file_entry", False):
                dialog.flatpakref_paths = []

        self.update_flatpak_install_dialog_state(dialog)

    def update_flatpak_install_dialog_state(self, dialog):
        if not hasattr(dialog, "mode_id_button"):
            return

        mode_id = dialog.mode_id_button.get_active()

        dialog.id_box.set_visible(mode_id)
        dialog.ref_box.set_visible(not mode_id)

        user_install = dialog.user_check.get_active()
        busy = getattr(dialog, "_busy", False)

        install_enabled = False
        add_enabled = False
        status = ""

        if hasattr(dialog, "delete_source_check"):
            dialog.delete_source_check.set_visible(not mode_id)

        if mode_id:
            app_id = dialog.id_entry.get_text().strip()

            try:
                flathub_exists = flathub_remote_exists(user_install)
            except Exception:
                flathub_exists = False

            scope_name = "user" if user_install else "system"

            if flathub_exists:
                status = f"Flathub remote is available in {scope_name} scope."
                install_enabled = bool(app_id)
                add_enabled = False
            else:
                status = (
                    f"Flathub remote is not available in {scope_name} scope.\n"
                    "Click Add Flathub to add it."
                )
                install_enabled = False
                add_enabled = True

            if hasattr(dialog, "delete_source_check"):
                dialog.delete_source_check.set_sensitive(False)
                dialog.delete_source_check.set_active(False)
        else:
            paths = getattr(dialog, "flatpakref_paths", []) or []
            value = dialog.file_entry.get_text().strip()

            if paths:
                install_enabled = True
                add_enabled = False

                if len(paths) == 1:
                    status = f"Selected file: {Path(paths[0]).name}"
                else:
                    status = f"{len(paths)} .flatpakref files selected."

                if hasattr(dialog, "delete_source_check"):
                    dialog.delete_source_check.set_sensitive(True)
            else:
                install_enabled = bool(value)
                add_enabled = False
                status = "Enter or browse for a .flatpakref file or URL."

                if hasattr(dialog, "delete_source_check"):
                    is_url = value.startswith("http://") or value.startswith("https://")
                    is_local_file = bool(value) and not is_url

                    dialog.delete_source_check.set_sensitive(is_local_file)

                    if not is_local_file:
                        dialog.delete_source_check.set_active(False)

        dialog.status_label.set_label(status)

        dialog.add_flatpak_button.set_sensitive(add_enabled and not busy)
        dialog.install_button.set_sensitive(install_enabled and not busy)

    def on_flatpak_install_response(self, dialog, response):
        if response == Gtk.ResponseType.CANCEL:
            dialog.close()
            return

        if response == Gtk.ResponseType.APPLY:
            user_install = dialog.user_check.get_active()
            self.start_add_flathub_remote(dialog, user_install)
            return

        if response == Gtk.ResponseType.OK:
            if not dialog.install_button.get_sensitive():
                return

            mode_id = dialog.mode_id_button.get_active()
            user_install = dialog.user_check.get_active()

            if mode_id:
                value = dialog.id_entry.get_text().strip()

                if not value:
                    return

                dialog.close()

                self.start_flatpak_install(
                    "id",
                    value,
                    user_install,
                    False,
                )

                return

            paths = getattr(dialog, "flatpakref_paths", []) or []

            if paths:
                delete_source = (
                    hasattr(dialog, "delete_source_check")
                    and dialog.delete_source_check.get_active()
                )

                dialog.close()

                self.start_flatpak_ref_batch_install(
                    paths,
                    user_install,
                    delete_source,
                )

                return

            value = dialog.file_entry.get_text().strip()

            if not value:
                return

            is_url = value.startswith("http://") or value.startswith("https://")

            delete_source = (
                hasattr(dialog, "delete_source_check")
                and dialog.delete_source_check.get_active()
                and bool(value)
                and not is_url
            )

            dialog.close()

            self.start_flatpak_install(
                "ref",
                value,
                user_install,
                delete_source,
            )
    # ------------------------------------------------------------
    # Add Flathub remote
    # ------------------------------------------------------------

    def start_add_flathub_remote(self, dialog, user_install):
        dialog._busy = True

        dialog.status_label.set_label("Adding Flathub remote…")

        dialog.add_flatpak_button.set_sensitive(False)
        dialog.install_button.set_sensitive(False)

        thread = threading.Thread(
            target=self.add_flathub_remote_worker,
            args=(
                dialog,
                user_install,
            ),
            daemon=True,
        )

        thread.start()

    def add_flathub_remote_worker(self, dialog, user_install):
        success, message = add_flathub_remote(user_install)

        GLib.idle_add(
            self.on_add_flathub_remote_finished,
            dialog,
            success,
            message,
        )

    def on_add_flathub_remote_finished(self, dialog, success, message):
        try:
            if not dialog.get_visible():
                return False
        except Exception:
            return False

        dialog._busy = False

        self.update_flatpak_install_dialog_state(dialog)

        if not success:
            dialog.status_label.set_label(
                "Failed to add Flathub remote.\n\n"
                + self._shorten_text(message, 800)
            )

        return False

    # ------------------------------------------------------------
    # Flatpakref browsing
    # ------------------------------------------------------------

    def on_flatpakref_browse_clicked(self, button, dialog):
        if hasattr(Gtk, "FileDialog"):
            file_dialog = Gtk.FileDialog.new()
            file_dialog.set_title("Select .flatpakref files")

            flatpakref_filter = Gtk.FileFilter()
            flatpakref_filter.set_name("Flatpak references (*.flatpakref)")
            flatpakref_filter.add_pattern("*.flatpakref")

            all_filter = Gtk.FileFilter()
            all_filter.set_name("All files")
            all_filter.add_pattern("*")

            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(flatpakref_filter)
            filters.append(all_filter)

            file_dialog.set_filters(filters)

            if hasattr(file_dialog, "open_multiple"):
                def callback(fd, result):
                    try:
                        files_model = fd.open_multiple_finish(result)
                    except Exception:
                        return

                    paths = []

                    if files_model:
                        for i in range(files_model.get_n_items()):
                            file = files_model.get_item(i)

                            if file:
                                path = file.get_path()

                                if path:
                                    paths.append(path)

                    if paths:
                        dialog.flatpakref_paths = paths

                        dialog._setting_file_entry = True

                        if len(paths) == 1:
                            dialog.file_entry.set_text(paths[0])
                        else:
                            dialog.file_entry.set_text(f"{len(paths)} files selected")

                        dialog._setting_file_entry = False

                        self.update_flatpak_install_dialog_state(dialog)

                dialog._file_dialog_callback = callback
                file_dialog.open_multiple(self, None, callback)
                return

        # Fallback for older GTK file chooser
        chooser = Gtk.FileChooserDialog(
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.OPEN,
        )

        chooser.set_property("title", "Select .flatpakref files")
        chooser.set_select_multiple(True)

        chooser.add_button("Cancel", Gtk.ResponseType.CANCEL)
        chooser.add_button("Open", Gtk.ResponseType.OK)

        flatpakref_filter = Gtk.FileFilter()
        flatpakref_filter.set_name("Flatpak references (*.flatpakref)")
        flatpakref_filter.add_pattern("*.flatpakref")

        chooser.add_filter(flatpakref_filter)

        chooser.connect(
            "response",
            self.on_flatpakref_chooser_response,
            dialog,
        )

        chooser.present()

    def on_flatpakref_chooser_response(self, chooser, response, dialog):
        if response == Gtk.ResponseType.OK:
            files = chooser.get_files()
            paths = []

            if files:
                for i in range(files.get_n_items()):
                    file = files.get_item(i)

                    if file:
                        path = file.get_path()

                        if path:
                            paths.append(path)

            if paths:
                dialog.flatpakref_paths = paths

                dialog._setting_file_entry = True

                if len(paths) == 1:
                    dialog.file_entry.set_text(paths[0])
                else:
                    dialog.file_entry.set_text(f"{len(paths)} files selected")

                dialog._setting_file_entry = False

                self.update_flatpak_install_dialog_state(dialog)

        chooser.close()

    # ------------------------------------------------------------
    # Flatpak installation execution
    # ------------------------------------------------------------

    def start_flatpak_install(self, source_type, value, user_install, delete_source=False):
        if source_type == "id":
            label = value
        elif value.startswith("http://") or value.startswith("https://"):
            label = value
        else:
            label = Path(value).name

        self.install_progress_window = InstallProgressWindow(self, "Installing Flatpak")
        self.install_progress_window.present()

        self.install_progress_window.start_pulse()
        self.install_progress_window.set_status(f"Installing {label}…")

        thread = threading.Thread(
            target=self.flatpak_install_worker,
            args=(
                source_type,
                value,
                user_install,
                delete_source,
            ),
            daemon=True,
        )

        thread.start()

    def flatpak_install_worker(self, source_type, value, user_install, delete_source):
        def output_callback(line):
            if self.install_progress_window:
                GLib.idle_add(
                    self.install_progress_window.append_output,
                    line,
                )

        success, message = install_flatpak_source(
            source_type,
            value,
            user_install,
            output_callback=output_callback,
            delete_source=delete_source,
        )

        GLib.idle_add(
            self.on_flatpak_install_finished,
            success,
            message,
        )

    def on_flatpak_install_finished(self, success, message):
        if self.install_progress_window:
            self.install_progress_window.stop_pulse()

            if success:
                self.install_progress_window.close_window()
                self.install_progress_window = None

                self.reload()

                self.show_message(
                    "Installation finished",
                    "Flatpak installation completed successfully.",
                    Gtk.MessageType.INFO,
                )
            else:
                self.install_progress_window.finish_failure(message)

        return False
        
    # ------------------------------------------------------------
    # Flatpak .flatpakref batch installation
    # ------------------------------------------------------------

    def start_flatpak_ref_batch_install(self, paths, user_install, delete_source=False):
        label = f"{len(paths)} .flatpakref file(s)"

        self.install_progress_window = InstallProgressWindow(
            self,
            "Installing Flatpak references",
        )

        self.install_progress_window.present()
        self.install_progress_window.start_pulse()
        self.install_progress_window.set_status(f"Installing {label}…")

        thread = threading.Thread(
            target=self.flatpak_ref_batch_install_worker,
            args=(
                paths,
                user_install,
                delete_source,
            ),
            daemon=True,
        )

        thread.start()

    def flatpak_ref_batch_install_worker(self, paths, user_install, delete_source):
        def output_callback(line):
            if self.install_progress_window:
                GLib.idle_add(
                    self.install_progress_window.append_output,
                    line,
                )

        success, message = install_flatpak_ref_batch(
            paths,
            user_install,
            output_callback=output_callback,
            delete_source=delete_source,
        )

        GLib.idle_add(
            self.on_flatpak_ref_batch_install_finished,
            success,
            message,
        )

    def on_flatpak_ref_batch_install_finished(self, success, message):
        if self.install_progress_window:
            self.install_progress_window.stop_pulse()

            if success:
                self.install_progress_window.close_window()
                self.install_progress_window = None
                self.reload()

                self.show_message(
                    "Installation finished",
                    "Flatpak reference files installed successfully.",
                    Gtk.MessageType.INFO,
                )
            else:
                self.install_progress_window.finish_failure(message)

        return False
    # ------------------------------------------------------------
    # Actions for context menu
    # ------------------------------------------------------------

    def _setup_actions(self):
        self.action_details = Gio.SimpleAction.new("details", None)
        self.action_details.connect("activate", self.on_action_details)
        self.action_details.set_enabled(False)

        self.action_mark_selected = Gio.SimpleAction.new("mark-selected", None)
        self.action_mark_selected.connect("activate", self.on_action_mark_selected)
        self.action_mark_selected.set_enabled(False)

        self.action_unmark_selected = Gio.SimpleAction.new("unmark-selected", None)
        self.action_unmark_selected.connect("activate", self.on_action_unmark_selected)
        self.action_unmark_selected.set_enabled(False)

        self.action_remove_marked = Gio.SimpleAction.new("remove-marked", None)
        self.action_remove_marked.connect("activate", self.on_action_remove_marked)
        self.action_remove_marked.set_enabled(False)

        self.action_mark_all_visible = Gio.SimpleAction.new("mark-all-visible", None)
        self.action_mark_all_visible.connect("activate", self.on_action_mark_all_visible)
        self.action_mark_all_visible.set_enabled(True)

        self.action_unmark_all_visible = Gio.SimpleAction.new("unmark-all-visible", None)
        self.action_unmark_all_visible.set_enabled(True)

        self.action_clear_all_marks = Gio.SimpleAction.new("clear-all-marks", None)
        self.action_clear_all_marks.connect("activate", self.on_action_clear_all_marks)
        self.action_clear_all_marks.set_enabled(False)

        self.action_group = Gio.SimpleActionGroup.new()

        self.action_group.add_action(self.action_details)
        self.action_group.add_action(self.action_mark_selected)
        self.action_group.add_action(self.action_unmark_selected)
        self.action_group.add_action(self.action_remove_marked)
        self.action_group.add_action(self.action_mark_all_visible)
        self.action_group.add_action(self.action_unmark_all_visible)
        self.action_group.add_action(self.action_clear_all_marks)

        self.insert_action_group("win", self.action_group)

    def _setup_context_menu(self):
        empty_model = Gio.Menu()

        self.context_popover = Gtk.PopoverMenu.new_from_model(empty_model)
        self.context_popover.set_parent(self.column_view)
        self.context_popover.set_autohide(True)

        try:
            self.context_popover.set_has_arrow(True)
        except AttributeError:
            pass

        right_click = Gtk.GestureClick.new()
        right_click.set_button(3)
        right_click.connect("released", self.on_right_click_released)

        self.column_view.add_controller(right_click)

    def build_context_menu_model(self):
        model = Gio.Menu()

        details_enabled = bool(self.context_item) or self.selected_count == 1
        mark_enabled = self.selected_count > 0 or bool(self.context_item)

        context_item_marked = False

        if self.context_item:
            context_item_marked = bool(getattr(self.context_item, "marked", False))

        unmark_enabled = self.selected_marked_count > 0 or context_item_marked

        remove_marked_enabled = self.removable_marked_count > 0

        if details_enabled:
            model.append("Details", "win.details")

        if mark_enabled:
            model.append("Mark for Removal", "win.mark-selected")

        if unmark_enabled:
            model.append("Unmark", "win.unmark-selected")

        if remove_marked_enabled:
            model.append("Remove Marked…", "win.remove-marked")

        return model

    def update_context_menu_state(self):
        if not hasattr(self, "action_details"):
            return

        details_enabled = bool(self.context_item) or self.selected_count == 1
        mark_enabled = self.selected_count > 0 or bool(self.context_item)

        context_item_marked = False

        if self.context_item:
            context_item_marked = bool(getattr(self.context_item, "marked", False))

        unmark_enabled = self.selected_marked_count > 0 or context_item_marked

        remove_marked_enabled = self.removable_marked_count > 0

        self.action_details.set_enabled(details_enabled)
        self.action_mark_selected.set_enabled(mark_enabled)
        self.action_unmark_selected.set_enabled(unmark_enabled)
        self.action_remove_marked.set_enabled(remove_marked_enabled)

    # ------------------------------------------------------------
    # Sidebar
    # ------------------------------------------------------------

    def on_sidebar_expand_toggled(self, button):
        if not hasattr(self, "sidebar_expanded"):
            self.sidebar_expanded = True

        self.sidebar_expanded = not self.sidebar_expanded

        if self.sidebar_expanded:
            if hasattr(self, "sidebar_controls"):
                self.sidebar_controls.set_visible(True)

            if hasattr(self, "sidebar"):
                self.sidebar.set_size_request(190, -1)

            if hasattr(self, "sidebar_wrap"):
                self.sidebar_wrap.set_size_request(-1, -1)

            button.set_label("«")
            button.set_tooltip_text("Hide sidebar")
            button.set_halign(Gtk.Align.END)
        else:
            if hasattr(self, "sidebar_controls"):
                self.sidebar_controls.set_visible(False)

            if hasattr(self, "sidebar"):
                self.sidebar.set_size_request(36, -1)

            if hasattr(self, "sidebar_wrap"):
                self.sidebar_wrap.set_size_request(42, -1)

            button.set_label("»")
            button.set_tooltip_text("Show sidebar")
            button.set_halign(Gtk.Align.CENTER)

        # Force GTK to recalculate layout size
        if hasattr(self, "sidebar"):
            self.sidebar.queue_resize()

        if hasattr(self, "sidebar_wrap"):
            self.sidebar_wrap.queue_resize()

    def _sidebar_heading(self, text):
        label = Gtk.Label(label=text)
        label.add_css_class("heading")
        label.set_xalign(0.0)
        label.set_margin_top(6)

        return label

    def _build_sidebar(self):
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        sidebar.set_size_request(210, -1)
        sidebar.set_hexpand(False)

        sidebar.set_margin_top(8)
        sidebar.set_margin_bottom(8)
        sidebar.set_margin_start(8)
        sidebar.set_margin_end(8)

        try:
            sidebar.add_css_class("background")
        except Exception:
            pass

        # ------------------------------------------------------------
        # Top row: hide sidebar button on right side
        # ------------------------------------------------------------
        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        self.sidebar_expand_button = Gtk.Button(label="«")
        self.sidebar_expand_button.set_tooltip_text("Hide sidebar")
        self.sidebar_expand_button.add_css_class("flat")
        self.sidebar_expand_button.set_halign(Gtk.Align.END)
        self.sidebar_expand_button.set_hexpand(True)
        self.sidebar_expand_button.connect("clicked", self.on_sidebar_expand_toggled)

        top_row.append(self.sidebar_expand_button)
        sidebar.append(top_row)

        # ------------------------------------------------------------
        # Sidebar controls
        # ------------------------------------------------------------
        self.sidebar_controls = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )

        # Search
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search")
        self.search_entry.connect("search-changed", self.on_search_changed)

        # Remove button
        self.remove_marked_button = Gtk.Button()
        self.remove_marked_button.set_tooltip_text("Remove marked apps")
        self.remove_marked_button.add_css_class("destructive-action")
        self.remove_marked_button.set_sensitive(False)
        self.remove_marked_button.connect("clicked", self.on_remove_marked_clicked)

        remove_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        remove_box.set_halign(Gtk.Align.CENTER)

        remove_icon = Gtk.Image.new_from_icon_name("user-trash-symbolic")
        self.remove_marked_label = Gtk.Label(label="Remove (0)")

        remove_box.append(remove_icon)
        remove_box.append(self.remove_marked_label)

        self.remove_marked_button.set_child(remove_box)

        # Mark all
        mark_all_button = Gtk.Button(label="Mark all")
        mark_all_button.set_tooltip_text("Mark all visible apps")
        mark_all_button.add_css_class("flat")
        mark_all_button.connect("clicked", self.on_mark_all_visible_clicked)

        # Clear marks
        self.clear_all_marks_button = Gtk.Button(label="Clear marks")
        self.clear_all_marks_button.set_tooltip_text("Clear all marked apps")
        self.clear_all_marks_button.add_css_class("flat")
        self.clear_all_marks_button.set_sensitive(False)
        self.clear_all_marks_button.connect("clicked", self.on_clear_all_marks_clicked)

        # ------------------------------------------------------------
        # View group
        # ------------------------------------------------------------
        self.marked_only_toggle = Gtk.ToggleButton(label="Marked only")
        self.marked_only_toggle.add_css_class("flat")
        self.marked_only_toggle.set_sensitive(False)
        self.marked_only_toggle.connect("toggled", self.on_marked_only_toggled)

        self.duplicates_only_toggle = Gtk.ToggleButton(label="Duplicates only")
        self.duplicates_only_toggle.add_css_class("flat")
        self.duplicates_only_toggle.connect("toggled", self.on_duplicates_only_toggled)

        view_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        view_box.append(self.marked_only_toggle)
        view_box.append(self.duplicates_only_toggle)

        view_expander = self._sidebar_group("View", view_box)

        # ------------------------------------------------------------
        # Tools group
        # ------------------------------------------------------------
        self.install_deb_button = Gtk.Button(label="Install .deb")
        self.install_deb_button.set_tooltip_text("Install a local .deb package")
        self.install_deb_button.add_css_class("flat")
        self.install_deb_button.connect("clicked", self.on_install_deb_clicked)

        self.install_flatpak_button = Gtk.Button(label="Install Flatpak")
        self.install_flatpak_button.set_tooltip_text("Install an app from Flathub")
        self.install_flatpak_button.add_css_class("flat")
        self.install_flatpak_button.connect("clicked", self.on_install_flatpak_clicked)

        tools_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        tools_box.append(self.install_deb_button)
        tools_box.append(self.install_flatpak_button)

        tools_expander = self._sidebar_group("Tools", tools_box)

        # ------------------------------------------------------------
        # Cleanup group
        # ------------------------------------------------------------
        self.autoremove_button = Gtk.Button(label="Clean orphaned packages")
        self.autoremove_button.set_tooltip_text(
            "Remove orphaned APT dependencies that are no longer needed"
        )
        self.autoremove_button.add_css_class("flat")
        self.autoremove_button.connect("clicked", self.on_autoremove_clicked)

        self.flatpak_cleanup_button = Gtk.Button(label="Clean unused runtimes")
        self.flatpak_cleanup_button.set_tooltip_text(
            "Remove unused Flatpak runtimes to free disk space"
        )
        self.flatpak_cleanup_button.add_css_class("flat")
        self.flatpak_cleanup_button.connect("clicked", self.on_flatpak_cleanup_clicked)

        cleanup_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        cleanup_box.append(self.autoremove_button)
        cleanup_box.append(self.flatpak_cleanup_button)

        cleanup_expander = self._sidebar_group("Cleanup", cleanup_box)

        # ------------------------------------------------------------
        # Advanced group
        # ------------------------------------------------------------
        advanced_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        self.show_leftovers_check = Gtk.CheckButton(label="Show leftovers")
        self.show_leftovers_check.set_tooltip_text(
            "Show leftover APT configuration packages"
        )
        self.show_leftovers_check.set_active(self.show_leftovers)
        self.show_leftovers_check.connect("toggled", self.on_show_leftovers_toggled)

        self.show_advanced_apps_check = Gtk.CheckButton(label="Show advanced apps")
        self.show_advanced_apps_check.set_tooltip_text(
            "Show non-GUI/system items when available"
        )
        self.show_advanced_apps_check.set_active(self.show_advanced_apps)
        self.show_advanced_apps_check.connect("toggled", self.on_show_advanced_apps_toggled)

        self.hide_basic_apps_check = Gtk.CheckButton(label="Hide normal apps")
        self.hide_basic_apps_check.set_tooltip_text(
            "Show only advanced/leftover items"
        )
        self.hide_basic_apps_check.set_active(self.hide_basic_apps)
        self.hide_basic_apps_check.connect("toggled", self.on_hide_basic_apps_toggled)

        self.select_all_leftovers_check = Gtk.CheckButton(label="Select all leftovers")
        self.select_all_leftovers_check.set_tooltip_text(
            "Mark or unmark all leftover configuration packages"
        )
        self.select_all_leftovers_check.set_visible(False)
        self.select_all_leftovers_check.set_sensitive(False)
        self.select_all_leftovers_check.connect(
            "toggled",
            self.on_select_all_leftovers_toggled,
        )

        self.purge_leftovers_button = Gtk.Button(label="Purge leftovers")
        self.purge_leftovers_button.set_tooltip_text(
            "Purge marked leftover configuration packages"
        )
        self.purge_leftovers_button.add_css_class("destructive-action")
        self.purge_leftovers_button.set_sensitive(False)
        self.purge_leftovers_button.connect("clicked", self.on_purge_leftovers_clicked)

        advanced_box.append(self.show_leftovers_check)
        advanced_box.append(self.show_advanced_apps_check)
        advanced_box.append(self.hide_basic_apps_check)
        advanced_box.append(Gtk.Separator())
        advanced_box.append(self.select_all_leftovers_check)
        advanced_box.append(self.purge_leftovers_button)

        advanced_expander = self._sidebar_group("Advanced", advanced_box)

        # ------------------------------------------------------------
        # Control order
        # ------------------------------------------------------------
        self.sidebar_controls.append(self.search_entry)
        self.sidebar_controls.append(self.remove_marked_button)
        self.sidebar_controls.append(mark_all_button)
        self.sidebar_controls.append(self.clear_all_marks_button)
        self.sidebar_controls.append(view_expander)
        self.sidebar_controls.append(tools_expander)
        self.sidebar_controls.append(cleanup_expander)
        self.sidebar_controls.append(advanced_expander)

        sidebar.append(self.sidebar_controls)

        return sidebar
        
    def _sidebar_group(self, title, child_box):
        child_box.set_margin_start(10)
        child_box.set_spacing(4)

        expander = Gtk.Expander(label=title)
        expander.set_child(child_box)
        expander.set_hexpand(True)

        try:
            expander.add_css_class("flat")
        except Exception:
            pass

        return expander

    def on_mark_all_visible_clicked(self, button):
        self.mark_all_visible()

    def on_clear_all_marks_clicked(self, button):
        self.clear_all_marks()

    # ------------------------------------------------------------
    # APT Autoremove
    # ------------------------------------------------------------

    def on_autoremove_clicked(self, button=None):
        if not shutil.which("apt-get"):
            self.show_message(
                "APT not available",
                "The apt-get command was not found on this system.",
                Gtk.MessageType.WARNING,
            )
            return

        # Show progress while checking preview
        self.progress_window = ProgressWindow(self, "Checking orphaned packages")
        self.progress_window.present()
        self.progress_window.start_indeterminate("Checking for orphaned packages…")

        thread = threading.Thread(
            target=self.autoremove_preview_worker,
            daemon=True,
        )
        thread.start()

    def autoremove_preview_worker(self):
        success, packages, space_freed, raw_output = get_apt_autoremove_preview()

        GLib.idle_add(
            self.on_autoremove_preview_finished,
            success,
            packages,
            space_freed,
            raw_output,
        )

    def on_autoremove_preview_finished(self, success, packages, space_freed, raw_output):
        if self.progress_window:
            self.progress_window.close_window()
            self.progress_window = None

        if not success:
            self.show_message(
                "Autoremove check failed",
                raw_output,
                Gtk.MessageType.ERROR,
            )
            return False

        if not packages:
            self.show_message(
                "No orphaned packages",
                "No orphaned APT dependencies were found.\n\n"
                "Your system is already clean.",
                Gtk.MessageType.INFO,
            )
            return False

        # Build confirmation dialog
        lines = []
        lines.append("The following orphaned packages will be removed:")
        lines.append("")

        for pkg in packages[:30]:
            lines.append(f"• {pkg}")

        if len(packages) > 30:
            lines.append(f"• …and {len(packages) - 30} more")

        if space_freed:
            lines.append("")
            lines.append(space_freed)

        message = "\n".join(lines)

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
        )
        dialog.set_resizable(False)
        dialog.set_property("message-type", Gtk.MessageType.WARNING)
        dialog.set_property(
            "text",
            f"Remove {len(packages)} orphaned packages?",
        )
        dialog.set_property("secondary-text", message)

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)

        remove_button = dialog.add_button("Remove", Gtk.ResponseType.OK)
        remove_button.add_css_class("destructive-action")

        dialog.connect("response", self.on_autoremove_confirm_response)
        dialog.present()

        return False

    def on_autoremove_confirm_response(self, dialog, response):
        dialog.close()

        if response != Gtk.ResponseType.OK:
            return

        self.install_progress_window = InstallProgressWindow(
            self,
            "Removing orphaned packages",
        )
        self.install_progress_window.present()
        self.install_progress_window.start_pulse()
        self.install_progress_window.set_status("Removing orphaned packages…")

        thread = threading.Thread(
            target=self.autoremove_worker,
            daemon=True,
        )
        thread.start()

    def autoremove_worker(self):
        def output_callback(line):
            if self.install_progress_window:
                GLib.idle_add(
                    self.install_progress_window.append_output,
                    line,
                )

        success, message = execute_apt_autoremove(
            output_callback=output_callback,
        )

        GLib.idle_add(
            self.on_autoremove_finished,
            success,
            message,
        )

    def on_autoremove_finished(self, success, message):
        if self.install_progress_window:
            self.install_progress_window.stop_pulse()

            if success:
                self.install_progress_window.close_window()
                self.install_progress_window = None
                self.reload()
                self.show_message(
                    "Cleanup finished",
                    "Orphaned packages were removed successfully.",
                    Gtk.MessageType.INFO,
                )
            else:
                self.install_progress_window.finish_failure(message)

        return False

    # ------------------------------------------------------------
    # Flatpak Unused Runtimes Cleanup
    # ------------------------------------------------------------

    def on_flatpak_cleanup_clicked(self, button=None):
        if not shutil.which("flatpak"):
            self.show_message(
                "Flatpak not available",
                "The flatpak command was not found on this system.",
                Gtk.MessageType.WARNING,
            )
            return

        # Show progress while checking
        self.progress_window = ProgressWindow(self, "Checking Flatpak runtimes")
        self.progress_window.present()
        self.progress_window.start_indeterminate("Checking for unused Flatpak runtimes…")

        thread = threading.Thread(
            target=self.flatpak_cleanup_preview_worker,
            daemon=True,
        )
        thread.start()

    def flatpak_cleanup_preview_worker(self):
        success, runtimes, raw_output = get_flatpak_unused_preview()

        GLib.idle_add(
            self.on_flatpak_cleanup_preview_finished,
            success,
            runtimes,
            raw_output,
        )

    def on_flatpak_cleanup_preview_finished(self, success, runtimes, raw_output):
        if self.progress_window:
            self.progress_window.close_window()
            self.progress_window = None

        if not success:
            self.show_message(
                "Flatpak check failed",
                raw_output,
                Gtk.MessageType.ERROR,
            )
            return False

        if not runtimes:
            self.show_message(
                "No Flatpak runtimes found",
                "No Flatpak runtimes were found on this system.",
                Gtk.MessageType.INFO,
            )
            return False

        # Build confirmation dialog
        lines = []
        lines.append(
            "The following action will remove unused Flatpak runtimes.\n"
            "Runtimes that are still needed by installed apps will NOT be removed."
        )
        lines.append("")
        lines.append(f"Total Flatpak runtimes currently installed: {len(runtimes)}")
        lines.append("")
        lines.append("Installed runtimes:")

        for rt in runtimes[:20]:
            lines.append(f"• {rt}")

        if len(runtimes) > 20:
            lines.append(f"• …and {len(runtimes) - 20} more")

        message = "\n".join(lines)

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
        )
        dialog.set_resizable(False)
        dialog.set_property("message-type", Gtk.MessageType.WARNING)
        dialog.set_property(
            "text",
            "Remove unused Flatpak runtimes?",
        )
        dialog.set_property("secondary-text", message)

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)

        remove_button = dialog.add_button("Clean Up", Gtk.ResponseType.OK)
        remove_button.add_css_class("destructive-action")

        dialog.connect("response", self.on_flatpak_cleanup_confirm_response)
        dialog.present()

        return False

    def on_flatpak_cleanup_confirm_response(self, dialog, response):
        dialog.close()

        if response != Gtk.ResponseType.OK:
            return

        self.install_progress_window = InstallProgressWindow(
            self,
            "Cleaning Flatpak runtimes",
        )
        self.install_progress_window.present()
        self.install_progress_window.start_pulse()
        self.install_progress_window.set_status("Removing unused Flatpak runtimes…")

        thread = threading.Thread(
            target=self.flatpak_cleanup_worker,
            daemon=True,
        )
        thread.start()

    def flatpak_cleanup_worker(self):
        def output_callback(line):
            if self.install_progress_window:
                GLib.idle_add(
                    self.install_progress_window.append_output,
                    line,
                )

        success, message = execute_flatpak_unused_cleanup(
            output_callback=output_callback,
        )

        GLib.idle_add(
            self.on_flatpak_cleanup_finished,
            success,
            message,
        )

    def on_flatpak_cleanup_finished(self, success, message):
        if self.install_progress_window:
            self.install_progress_window.stop_pulse()

            if success:
                self.install_progress_window.close_window()
                self.install_progress_window = None
                self.reload()
                self.show_message(
                    "Cleanup finished",
                    "Unused Flatpak runtimes were removed successfully.",
                    Gtk.MessageType.INFO,
                )
            else:
                self.install_progress_window.finish_failure(message)

        return False

    # ------------------------------------------------------------
    # Column helpers
    # ------------------------------------------------------------

    def create_column(
        self,
        title: str,
        prop: str,
        expand: bool = False,
        min_width: int = 100,
        fixed_width: int = 160,
    ):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.on_setup_cell)
        factory.connect("bind", self.on_bind_cell, prop)

        column = Gtk.ColumnViewColumn.new(title, factory)

        expression = Gtk.PropertyExpression.new(AppItem, None, prop)
        sorter = Gtk.StringSorter.new(expression)

        column.set_sorter(sorter)
        column.set_resizable(True)
        column.set_expand(expand)

        try:
            column.set_minimum_width(min_width)
            column.set_fixed_width(fixed_width)
        except AttributeError:
            pass

        return column

    def on_setup_cell(self, factory, list_item):
        label = Gtk.Label()
        label.set_xalign(0.0)
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(Pango.EllipsizeMode.END)

        list_item.set_child(label)

    def on_bind_cell(self, factory, list_item, prop):
        item = list_item.get_item()

        value = getattr(item, prop, "")

        if prop == "installed_at" and not value:
            value = "Unknown"
        elif prop == "version" and not value:
            value = "-"
        elif prop == "marked_label" and not value:
            value = ""

        list_item.get_child().set_label(str(value))

    # ------------------------------------------------------------
    # Checkbox column
    # ------------------------------------------------------------

    def create_check_column(self):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.on_setup_check_cell)
        factory.connect("bind", self.on_bind_check_cell)

        column = Gtk.ColumnViewColumn.new("✓", factory)

        expression = Gtk.PropertyExpression.new(AppItem, None, "marked_label")
        sorter = Gtk.StringSorter.new(expression)

        column.set_sorter(sorter)
        column.set_resizable(False)

        try:
            column.set_minimum_width(46)
            column.set_fixed_width(52)
        except AttributeError:
            pass

        return column

    def on_setup_check_cell(self, factory, list_item):
        check = Gtk.CheckButton()

        check.set_halign(Gtk.Align.CENTER)
        check.set_valign(Gtk.Align.CENTER)

        try:
            check.set_focusable(False)
        except AttributeError:
            pass

        list_item.set_child(check)

    def on_bind_check_cell(self, factory, list_item):
        item = list_item.get_item()
        check = list_item.get_child()

        if item is None or check is None:
            return

        old_handler_id = getattr(check, "_app_manager_handler_id", None)

        if old_handler_id is not None:
            try:
                check.handler_disconnect(old_handler_id)
            except Exception:
                pass

        check.set_active(bool(getattr(item, "marked", False)))

        handler_id = check.connect("toggled", self.on_check_toggled, item)
        check._app_manager_handler_id = handler_id

    def on_check_toggled(self, check, item):
        key = app_key(item)
        active = check.get_active()

        if active:
            self.marked_keys.add(key)
        else:
            self.marked_keys.discard(key)

        item.marked = active
        item.marked_label = "✓" if active else ""

        self.save_marked_keys()

        self.update_marked_ui()
        self.update_selection_ui()
        self.update_context_menu_state()

        if self.marked_only:
            self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)

        # Best-effort refresh if currently sorted by the marked column.
        sorter = self.column_view.get_sorter()
        if sorter:
            try:
                self.sort_model.set_sorter(None)
                self.sort_model.set_sorter(sorter)
            except Exception:
                pass

    # ------------------------------------------------------------
    # Leftover config helpers
    # ------------------------------------------------------------

    def get_marked_leftover_entries(self):
        return [
            app
            for app in self.get_marked_entries()
            if getattr(app, "manager", "") == "Leftover"
        ]

    def unmark_all_leftovers(self):
        changed = False

        for app in self.get_leftover_entries():
            key = app_key(app)

            if key and key in self.marked_keys:
                self.marked_keys.remove(key)
                changed = True

        if changed:
            self.save_marked_keys()
            self.rebuild_list()

    def on_select_all_leftovers_toggled(self, button):
        if getattr(self, "_updating_select_all_leftovers", False):
            return

        if button.get_active():
            self.mark_all_leftovers()
        else:
            self.unmark_all_leftovers()

    # ------------------------------------------------------------
    # Leftover purge
    # ------------------------------------------------------------

    def on_purge_leftovers_clicked(self, button=None):
        leftovers = self.get_marked_leftover_entries()

        if not leftovers:
            self.show_toast("No leftovers selected")
            return

        package_ids = [
            getattr(app, "package_id", "")
            for app in leftovers
            if getattr(app, "package_id", "")
        ]

        if not package_ids:
            self.show_message(
                "No valid leftovers selected",
                "Selected leftover items do not have valid package IDs.",
                Gtk.MessageType.WARNING,
            )
            return

        lines = []

        lines.append("The following leftover configuration packages will be purged:")
        lines.append("")

        for package_id in package_ids[:30]:
            lines.append(f"• {package_id}")

        if len(package_ids) > 30:
            lines.append(f"• …and {len(package_ids) - 30} more")

        message = "\n".join(lines)

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
        )

        dialog.set_resizable(False)
        dialog.set_property("message-type", Gtk.MessageType.WARNING)
        dialog.set_property("text", f"Purge {len(package_ids)} leftover packages?")
        dialog.set_property("secondary-text", message)

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)

        purge_button = dialog.add_button("Purge", Gtk.ResponseType.OK)
        purge_button.add_css_class("destructive-action")

        dialog.connect(
            "response",
            self.on_purge_leftovers_confirm_response,
            leftovers,
        )

        dialog.present()

    def on_purge_leftovers_confirm_response(self, dialog, response, leftovers):
        dialog.close()

        if response != Gtk.ResponseType.OK:
            return

        self.start_purge_leftovers(leftovers)

    def start_purge_leftovers(self, leftovers):
        self._pending_leftover_keys = [app_key(app) for app in leftovers]

        package_ids = [
            getattr(app, "package_id", "")
            for app in leftovers
            if getattr(app, "package_id", "")
        ]

        self.install_progress_window = InstallProgressWindow(
            self,
            "Purging leftovers",
        )

        self.install_progress_window.present()
        self.install_progress_window.start_pulse()
        self.install_progress_window.set_status("Purging leftover configuration packages…")

        thread = threading.Thread(
            target=self.purge_leftover_worker,
            args=(package_ids,),
            daemon=True,
        )

        thread.start()

    def purge_leftover_worker(self, package_ids):
        def output_callback(line):
            if self.install_progress_window:
                GLib.idle_add(
                    self.install_progress_window.append_output,
                    line,
                )

        success, message = purge_leftover_configs(
            package_ids,
            output_callback=output_callback,
        )

        GLib.idle_add(
            self.on_purge_leftovers_finished,
            success,
            message,
        )

    def on_purge_leftovers_finished(self, success, message):
        if self.install_progress_window:
            self.install_progress_window.stop_pulse()

            if success:
                self.install_progress_window.close_window()
                self.install_progress_window = None

                for key in getattr(self, "_pending_leftover_keys", []):
                    self.marked_keys.discard(key)

                self.save_marked_keys()
                self._pending_leftover_keys = []

                self.reload()

                self.show_message(
                    "Purge finished",
                    "Leftover configuration packages were purged successfully.",
                    Gtk.MessageType.INFO,
                )
            else:
                self.install_progress_window.finish_failure(message)

        return False

    # ------------------------------------------------------------
    # Icon + name column
    # ------------------------------------------------------------

    def create_name_column(self):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.on_setup_name_cell)
        factory.connect("bind", self.on_bind_name_cell)

        column = Gtk.ColumnViewColumn.new("Name", factory)

        expression = Gtk.PropertyExpression.new(AppItem, None, "name")
        sorter = Gtk.StringSorter.new(expression)

        column.set_sorter(sorter)
        column.set_resizable(True)
        column.set_expand(True)

        try:
            column.set_minimum_width(240)
            column.set_fixed_width(340)
        except AttributeError:
            pass

        return column

    def on_setup_name_cell(self, factory, list_item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        image = Gtk.Image()
        image.set_pixel_size(16)

        label = Gtk.Label()
        label.set_xalign(0.0)
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(Pango.EllipsizeMode.END)

        box.append(image)
        box.append(label)

        list_item.set_child(box)

    def on_bind_name_cell(self, factory, list_item):
        item = list_item.get_item()
        box = list_item.get_child()

        if item is None or box is None:
            return

        image = box.get_first_child()

        if image is None:
            return

        label = image.get_next_sibling()

        if label is None:
            return

        label.set_label(getattr(item, "name", "") or "-")

        self.set_image_from_icon(image, getattr(item, "icon", ""))

    def set_image_from_icon(self, image, icon):
        fallback = "application-x-executable-symbolic"

        image.set_pixel_size(16)

        if not icon:
            image.set_from_icon_name(fallback)
            return

        icon_path = Path(icon).expanduser()

        if icon.startswith("/") or icon.startswith("~"):
            if icon_path.exists():
                try:
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                        str(icon_path),
                        16,
                        16,
                        True,
                    )

                    image.set_from_pixbuf(pixbuf)
                    return
                except Exception:
                    image.set_from_icon_name(fallback)
                    return
            else:
                image.set_from_icon_name(fallback)
                return

        image.set_from_icon_name(icon)

    # ------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------
    def filter_func(self, item, user_data=None):
        if not isinstance(item, AppItem):
            return False

        manager = getattr(item, "manager", "")
        is_leftover = manager == "Leftover"
        is_advanced = is_leftover or not getattr(item, "is_gui_app", True)

        if is_advanced:
            if is_leftover:
                if not self.show_leftovers:
                    return False
            else:
                if not self.show_advanced_apps:
                    return False
        else:
            if self.hide_basic_apps:
                return False

        if self.marked_only and not getattr(item, "marked", False):
            return False

        if self.duplicates_only and not getattr(item, "is_duplicate", False):
            return False

        if self.search_text:
            haystack = f"{item.name} {item.source} {item.manager} {item.package_id}".lower()
            if self.search_text.lower() not in haystack:
                return False

        return True

    def on_search_changed(self, entry):
        self.search_text = entry.get_text().strip()
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)

    def on_marked_only_toggled(self, button):
        self.marked_only = button.get_active()
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        
    def on_duplicates_only_toggled(self, button):
        self.duplicates_only = button.get_active()
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)

    # ------------------------------------------------------------
    # Sorting
    # ------------------------------------------------------------

    def on_sorter_changed(self, column_view, param):
        self.sort_model.set_sorter(column_view.get_sorter())

    # ------------------------------------------------------------
    # Details
    # ------------------------------------------------------------

    def on_activate(self, column_view, position):
        try:
            item = self.selection.get_item(position)
        except Exception:
            item = None

        if item:
            details_window = DetailsWindow(self, item)
            details_window.present()

    # ------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------

    def get_row_item_at_y(self, y):
        try:
            row = self.column_view.get_row_at_y(int(y))
        except AttributeError:
            return None, None
        except Exception:
            return None, None

        if not row:
            return None, None

        try:
            item = row.get_item()
        except Exception:
            item = None

        try:
            position = row.get_position()
        except Exception:
            position = None

        return item, position

    def get_position_of_item(self, item):
        try:
            n = self.sort_model.get_n_items()
        except Exception:
            return None

        for position in range(n):
            try:
                if self.sort_model.get_item(position) is item:
                    return position
            except Exception:
                continue

        return None

    def on_right_click_released(self, gesture, n_press, x, y):
        item, position = self.get_row_item_at_y(y)

        if item is not None:
            self.context_item = item

            if position is None:
                position = self.get_position_of_item(item)

            if position is not None:
                try:
                    if not self.selection.is_selected(position):
                        self.selection.select_item(position, True)
                except Exception:
                    pass
        else:
            selected_items = self.get_selected_items()

            if len(selected_items) == 1:
                self.context_item = selected_items[0]
            else:
                self.context_item = None

        self.update_selection_ui()
        self.update_context_menu_state()

        menu_model = self.build_context_menu_model()

        if menu_model.get_n_items() == 0:
            return

        self.context_popover.set_menu_model(menu_model)

        try:
            rect = Gdk.Rectangle()
            rect.x = int(x)
            rect.y = int(y)
            rect.width = 1
            rect.height = 1

            self.context_popover.set_pointing_to(rect)
        except Exception:
            pass

        self.context_popover.popup()

        try:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        except Exception:
            pass

    def on_action_details(self, action, param):
        item = self.context_item

        if not item:
            selected_items = self.get_selected_items()

            if len(selected_items) == 1:
                item = selected_items[0]

        if item:
            details_window = DetailsWindow(self, item)
            details_window.present()

    def on_action_mark_selected(self, action, param):
        items = self.get_selected_items()

        if not items and self.context_item:
            items = [self.context_item]

        self.mark_items(items)

    def on_action_unmark_selected(self, action, param):
        items = self.get_selected_items()

        if not items and self.context_item:
            items = [self.context_item]

        self.unmark_items(items)

    def on_action_remove_marked(self, action, param):
        self.on_remove_marked_clicked()

    # ------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------

    def on_selection_changed(self, *args):
        if self._selection_ui_pending:
            return

        self._selection_ui_pending = True
        GLib.idle_add(self.update_selection_ui)

    def update_selection_ui(self):
        selected_items = self.get_selected_items()

        self.selected_count = len(selected_items)

        self.selected_marked_count = sum(
            1
            for item in selected_items
            if getattr(item, "marked", False)
        )

        self.selection_label.set_label(f"{self.selected_count} selected")

        self.mark_button.set_sensitive(self.selected_count > 0)

        self.unmark_button.set_visible(self.selected_marked_count > 0)
        self.unmark_button.set_sensitive(self.selected_marked_count > 0)

        self.action_revealer.set_reveal_child(self.selected_count > 0)

        self.update_context_menu_state()

        self._selection_ui_pending = False

        return False

    def get_selected_items(self):
        items = []

        try:
            n = self.selection.get_n_items()
        except Exception:
            try:
                n = self.sort_model.get_n_items()
            except Exception:
                n = 0

        for position in range(n):
            try:
                if self.selection.is_selected(position):
                    item = self.selection.get_item(position)
                    if item:
                        items.append(item)
            except Exception:
                continue

        return items

    # ------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------

    def on_reload_clicked(self, button=None):
        if button:
            button.set_sensitive(False)

        self.reload_button.set_sensitive(False)
        self.window_title.set_subtitle("Scanning…")

        thread = threading.Thread(target=self.scan_worker, daemon=True)
        thread.start()

    def reload(self):
        self.on_reload_clicked(self.reload_button)

    def scan_worker(self):
        apps = scan_all()
        GLib.idle_add(self.populate_apps, apps)

    def populate_apps(self, apps):
        present_keys = {app_key(app) for app in apps}
        self.marked_keys.intersection_update(present_keys)
        self.save_marked_keys()
        
        self.current_apps = apps
        self.detect_duplicates()
        self.rebuild_list()
        
        self.window_title.set_subtitle(f"{len(apps)} apps detected")
        self.reload_button.set_sensitive(True)
        return False

    def rebuild_list(self):
        self.list_store.remove_all()

        for app in self.current_apps:
            item = AppItem(app)
            key = app_key(app)

            if key in self.marked_keys:
                item.marked = True
                item.marked_label = "✓"

            self.list_store.append(item)

        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)

        self.update_marked_ui()
        self.update_selection_ui()

    def detect_duplicates(self):
        """
        Groups apps by their normalized exec_name and flags duplicates.
        Only considers GUI apps to avoid flagging background libraries.
        """
        identity_groups = {}
        
        for app in self.current_apps:
            if not getattr(app, "is_gui_app", True):
                continue
                
            exec_name = getattr(app, "exec_name", "").lower()
            if not exec_name:
                continue
                
            # Ignore generic wrappers
            if exec_name in ("python", "python3", "java", "sh", "bash"):
                continue
                
            if exec_name not in identity_groups:
                identity_groups[exec_name] = []
            identity_groups[exec_name].append(app)
            
        # Flag apps as duplicates if they share an identity and have different managers/paths
        for exec_name, group in identity_groups.items():
            if len(group) > 1:
                # Check if they are actually distinct installations (different managers or package_ids)
                unique_installations = set((app.manager, app.package_id) for app in group)
                if len(unique_installations) > 1:
                    for app in group:
                        app.is_duplicate = True

    # ------------------------------------------------------------
    # Mark persistence
    # ------------------------------------------------------------

    def _state_dir(self):
        return Path.home() / ".local" / "state" / "app-manager"

    def load_marked_keys(self):
        return set()

    def save_marked_keys(self):
        return

    # ------------------------------------------------------------
    # Marking
    # ------------------------------------------------------------

    def get_marked_entries(self):
        return [
            app
            for app in self.current_apps
            if app_key(app) in self.marked_keys
        ]

    def update_marked_ui(self):
        marked_entries = self.get_marked_entries()
        removable_marked = [app for app in marked_entries if can_remove(app)]

        self.removable_marked_count = len(removable_marked)

        marked_available = len(self.marked_keys) > 0

        if hasattr(self, "remove_marked_label"):
            self.remove_marked_label.set_label(
                f"Remove ({self.removable_marked_count})"
            )

        if hasattr(self, "remove_marked_button"):
            self.remove_marked_button.set_tooltip_text(
                f"Remove marked apps ({self.removable_marked_count})"
            )
            self.remove_marked_button.set_sensitive(
                self.removable_marked_count > 0
            )

        if hasattr(self, "marked_only_toggle"):
            self.marked_only_toggle.set_sensitive(marked_available)

            if not marked_available and self.marked_only:
                self.marked_only = False
                self.marked_only_toggle.set_active(False)
                self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)

        self.update_context_menu_state()

        if hasattr(self, "action_clear_all_marks"):
            self.action_clear_all_marks.set_enabled(len(self.marked_keys) > 0)

        if hasattr(self, "clear_all_marks_button"):
            self.clear_all_marks_button.set_sensitive(len(self.marked_keys) > 0)

        marked_leftovers = [
            app
            for app in marked_entries
            if getattr(app, "manager", "") == "Leftover"
        ]

        if hasattr(self, "purge_leftovers_button"):
            self.purge_leftovers_button.set_sensitive(len(marked_leftovers) > 0)

        self.update_select_all_leftovers_state()
        
    def mark_items(self, items):
        changed = False

        for item in items:
            key = app_key(item)

            if key and key not in self.marked_keys:
                self.marked_keys.add(key)
                changed = True

        if changed:
            self.save_marked_keys()
            self.rebuild_list()

    def unmark_items(self, items):
        changed = False

        for item in items:
            key = app_key(item)

            if key and key in self.marked_keys:
                self.marked_keys.remove(key)
                changed = True

        if changed:
            self.save_marked_keys()
            self.rebuild_list()

    def on_mark_clicked(self, button=None):
        items = self.get_selected_items()

        if not items:
            self.show_toast("No apps selected")
            return

        self.mark_items(items)

    def on_unmark_clicked(self, button=None):
        items = self.get_selected_items()

        if not items:
            self.show_toast("No apps selected")
            return

        self.unmark_items(items)

    # ------------------------------------------------------------
    # Bulk marking
    # ------------------------------------------------------------

    def get_visible_items(self):
        items = []

        try:
            n = self.list_store.get_n_items()
        except Exception:
            return items

        for position in range(n):
            try:
                item = self.list_store.get_item(position)
            except Exception:
                continue

            if item and self.filter_func(item):
                items.append(item)

        return items

    def on_action_mark_all_visible(self, action, param):
        self.mark_all_visible()

    def on_action_clear_all_marks(self, action, param):
        self.clear_all_marks()

    def mark_all_visible(self):
        visible_items = self.get_visible_items()

        changed = False

        for item in visible_items:
            key = app_key(item)

            if key and key not in self.marked_keys:
                self.marked_keys.add(key)
                changed = True

        if changed:
            self.save_marked_keys()
            self.rebuild_list()

    def clear_all_marks(self):
        if not self.marked_keys:
            self.show_toast("No marked apps to clear")
            return

        self.marked_keys.clear()
        self.save_marked_keys()
        self.rebuild_list()
        self.show_toast("All marks cleared")

    # ------------------------------------------------------------
    # Advanced visibility helpers
    # ------------------------------------------------------------

    def on_show_leftovers_toggled(self, button):
        self.show_leftovers = button.get_active()
        self._unmark_hidden_advanced_items()
        self.rebuild_list()

    def on_show_advanced_apps_toggled(self, button):
        self.show_advanced_apps = button.get_active()
        self._unmark_hidden_advanced_items()
        self.rebuild_list()

    def on_hide_basic_apps_toggled(self, button):
        self.hide_basic_apps = button.get_active()
        self._unmark_hidden_advanced_items()
        self.rebuild_list()

    def _entry_hidden_by_advanced_options(self, app):
        manager = getattr(app, "manager", "")

        is_leftover = manager == "Leftover"
        is_advanced = is_leftover or not getattr(app, "is_gui_app", True)

        if is_advanced:
            if is_leftover:
                return not self.show_leftovers

            return not self.show_advanced_apps

        return self.hide_basic_apps

    def _unmark_hidden_advanced_items(self):
        hidden_keys = set()

        for app in self.current_apps:
            if self._entry_hidden_by_advanced_options(app):
                hidden_keys.add(app_key(app))

        if hidden_keys:
            self.marked_keys.difference_update(hidden_keys)
            self.save_marked_keys()

    # ------------------------------------------------------------
    # Leftover helpers
    # ------------------------------------------------------------

    def get_leftover_entries(self):
        return [
            app
            for app in self.current_apps
            if getattr(app, "manager", "") == "Leftover"
        ]

    def get_marked_leftover_entries(self):
        return [
            app
            for app in self.get_marked_entries()
            if getattr(app, "manager", "") == "Leftover"
        ]

    def mark_all_leftovers(self):
        changed = False

        for app in self.get_leftover_entries():
            key = app_key(app)

            if key and key not in self.marked_keys:
                self.marked_keys.add(key)
                changed = True

        if changed:
            self.save_marked_keys()
            self.rebuild_list()

    def unmark_all_leftovers(self):
        changed = False

        for app in self.get_leftover_entries():
            key = app_key(app)

            if key and key in self.marked_keys:
                self.marked_keys.remove(key)
                changed = True

        if changed:
            self.save_marked_keys()
            self.rebuild_list()

    def on_select_all_leftovers_toggled(self, button):
        if getattr(self, "_updating_select_all_leftovers", False):
            return

        if button.get_active():
            self.mark_all_leftovers()
        else:
            self.unmark_all_leftovers()

    def update_select_all_leftovers_state(self):
        check = getattr(self, "select_all_leftovers_check", None)

        if not check:
            return

        leftovers = self.get_leftover_entries()
        has_leftovers = bool(leftovers)

        check.set_visible(has_leftovers)
        check.set_sensitive(has_leftovers and self.show_leftovers)

        if not has_leftovers:
            self._updating_select_all_leftovers = True
            check.set_active(False)
            self._updating_select_all_leftovers = False
            return

        leftover_keys = {app_key(app) for app in leftovers}
        all_marked = leftover_keys.issubset(self.marked_keys)

        self._updating_select_all_leftovers = True
        check.set_active(all_marked)
        self._updating_select_all_leftovers = False

    # ------------------------------------------------------------
    # Leftover purge
    # ------------------------------------------------------------

    def on_purge_leftovers_clicked(self, button=None):
        leftovers = self.get_marked_leftover_entries()

        if not leftovers:
            self.show_message(
                "No leftovers selected",
                "Mark one or more leftover configuration packages first.",
                Gtk.MessageType.INFO,
            )
            return

        package_ids = [
            getattr(app, "package_id", "")
            for app in leftovers
            if getattr(app, "package_id", "")
        ]

        if not package_ids:
            self.show_message(
                "No valid leftovers selected",
                "Selected leftover items do not have valid package IDs.",
                Gtk.MessageType.WARNING,
            )
            return

        lines = []

        lines.append("The following leftover configuration packages will be purged:")
        lines.append("")

        for package_id in package_ids[:30]:
            lines.append(f"• {package_id}")

        if len(package_ids) > 30:
            lines.append(f"• …and {len(package_ids) - 30} more")

        message = "\n".join(lines)

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
        )

        dialog.set_resizable(False)
        dialog.set_property("message-type", Gtk.MessageType.WARNING)
        dialog.set_property("text", f"Purge {len(package_ids)} leftover packages?")
        dialog.set_property("secondary-text", message)

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)

        purge_button = dialog.add_button("Purge", Gtk.ResponseType.OK)
        purge_button.add_css_class("destructive-action")

        dialog.connect(
            "response",
            self.on_purge_leftovers_confirm_response,
            leftovers,
        )

        dialog.present()

    def on_purge_leftovers_confirm_response(self, dialog, response, leftovers):
        dialog.close()

        if response != Gtk.ResponseType.OK:
            return

        self.start_purge_leftovers(leftovers)

    def start_purge_leftovers(self, leftovers):
        self._pending_leftover_keys = [app_key(app) for app in leftovers]

        package_ids = [
            getattr(app, "package_id", "")
            for app in leftovers
            if getattr(app, "package_id", "")
        ]

        self.install_progress_window = InstallProgressWindow(
            self,
            "Purging leftovers",
        )

        self.install_progress_window.present()
        self.install_progress_window.start_pulse()
        self.install_progress_window.set_status("Purging leftover configuration packages…")

        thread = threading.Thread(
            target=self.purge_leftover_worker,
            args=(package_ids,),
            daemon=True,
        )

        thread.start()

    def purge_leftover_worker(self, package_ids):
        def output_callback(line):
            if self.install_progress_window:
                GLib.idle_add(
                    self.install_progress_window.append_output,
                    line,
                )

        success, message = purge_leftover_configs(
            package_ids,
            output_callback=output_callback,
        )

        GLib.idle_add(
            self.on_purge_leftovers_finished,
            success,
            message,
        )

    def on_purge_leftovers_finished(self, success, message):
        if self.install_progress_window:
            self.install_progress_window.stop_pulse()

            if success:
                self.install_progress_window.close_window()
                self.install_progress_window = None

                for key in getattr(self, "_pending_leftover_keys", []):
                    self.marked_keys.discard(key)

                self.save_marked_keys()
                self._pending_leftover_keys = []

                self.reload()

                self.show_message(
                    "Purge finished",
                    "Leftover configuration packages were purged successfully.",
                    Gtk.MessageType.INFO,
                )
            else:
                self.install_progress_window.finish_failure(message)

        return False

    # ------------------------------------------------------------
    # APT batch preview
    # ------------------------------------------------------------

    def apt_batch_prepare_worker(self, removable):
        preview = prepare_apt_batch_preview(removable)

        GLib.idle_add(
            self.on_apt_batch_prepare_finished,
            removable,
            preview,
        )

    def on_apt_batch_prepare_finished(self, removable, preview):
        if self.progress_window:
            self.progress_window.close_window()
            self.progress_window = None

        if preview:
            blocked_keys = preview.get("blocked_keys", set())

            if blocked_keys:
                removable = [
                    app
                    for app in removable
                    if app_key(app) not in blocked_keys
                ]

            if preview.get("has_apt") and not preview.get("apt_ok"):
                removable = [
                    app
                    for app in removable
                    if getattr(app, "manager", "") != "APT"
                ]

        if not removable:
            lines = []

            if preview and preview.get("preview"):
                lines.append(self._shorten_text(preview.get("preview")))

            blocked = preview.get("blocked", []) if preview else []

            if blocked:
                lines.append("")
                lines.append("Blocked/skipped APT apps:")
                lines.append("")

                for name, reason in blocked[:20]:
                    lines.append(f"• {name}: {reason}")

                if len(blocked) > 20:
                    lines.append(f"• …and {len(blocked) - 20} more")

            self.show_message(
                "APT batch removal blocked",
                "\n".join(lines) if lines else "APT batch removal was blocked.",
                Gtk.MessageType.WARNING,
            )

            return False

        self.show_batch_confirm(removable, preview)

        return False

    def _shorten_text(self, text, limit=2500):
        if not text:
            return ""

        text = str(text)

        if len(text) <= limit:
            return text

        return text[:limit] + "\n…"

    # ------------------------------------------------------------
    # Batch removal
    # ------------------------------------------------------------

    def on_remove_marked_clicked(self, button=None):
        marked_entries = self.get_marked_entries()
        removable = [app for app in marked_entries if can_remove(app)]

        if not removable:
            self.show_toast("No removable marked apps")
            return

        has_apt = any(
            getattr(app, "manager", "") == "APT"
            for app in removable
        )

        if has_apt:
            self.progress_window = ProgressWindow(self, "Preparing removal")
            self.progress_window.present()
            self.progress_window.start_indeterminate("Checking APT dependencies…")

            thread = threading.Thread(
                target=self.apt_batch_prepare_worker,
                args=(removable,),
                daemon=True,
            )
            thread.start()
        else:
            self.show_batch_confirm(removable, None)

    def show_batch_confirm(self, removable, apt_preview):
        marked_entries = self.get_marked_entries()
        skipped = [app for app in marked_entries if not can_remove(app)]

        lines = []

        lines.append("The following apps will be removed:")
        lines.append("")

        for app in removable[:20]:
            lines.append(f"• {app.name} — {app.manager}")

        if len(removable) > 20:
            lines.append(f"• …and {len(removable) - 20} more")

        if skipped:
            lines.append("")
            lines.append("Skipped items:")
            lines.append("")

            for app in skipped[:10]:
                lines.append(f"• {app.name} — {app.manager}")

            if len(skipped) > 10:
                lines.append(f"• …and {len(skipped) - 10} more")

        if apt_preview:
            blocked = apt_preview.get("blocked", [])

            if blocked:
                lines.append("")
                lines.append("APT blocked/skipped:")
                lines.append("")

                for name, reason in blocked[:10]:
                    lines.append(f"• {name}: {reason}")

                if len(blocked) > 10:
                    lines.append(f"• …and {len(blocked) - 10} more")

            preview_text = apt_preview.get("preview", "")

            if preview_text:
                lines.append("")

                if apt_preview.get("apt_ok"):
                    lines.append("APT dependency preview:")
                else:
                    lines.append("APT warning:")

                lines.append("")
                lines.append(self._shorten_text(preview_text))

            warnings = apt_preview.get("warnings", [])

            if warnings:
                lines.append("")
                lines.append("Warnings:")

                for warning in warnings:
                    lines.append(f"• {warning}")
                    
        risk_warnings = []

        for app in removable[:50]:
            try:
                risk, reason = get_removal_risk(app)
            except Exception:
                risk, reason = "normal", ""

            if risk == "high" and reason:
                risk_warnings.append(f"• {app.name} — {reason}")

        if risk_warnings:
            lines.append("")
            lines.append("High-risk items detected:")
            lines.append("")

            lines.extend(risk_warnings[:20])

            if len(risk_warnings) > 20:
                lines.append(f"• …and {len(risk_warnings) - 20} more")

        if any(getattr(app, "manager", "") == "APT" for app in removable):
            lines.append("")
            lines.append(
                  "Safety reminder: For APT changes, consider creating a Timeshift "
                "or system snapshot before continuing."
             )
        message = "\n".join(lines)
        
        


        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
        )
        dialog.set_resizable(False)
        dialog.set_property("message-type", Gtk.MessageType.WARNING)
        dialog.set_property("text", f"Remove {len(removable)} marked apps?")
        dialog.set_property("secondary-text", message)

        # Purge checkbox (only relevant if APT apps are included)
        has_apt = any(
            getattr(app, "manager", "") == "APT"
            for app in removable
        )

        if has_apt:
            content_area = dialog.get_content_area()

            purge_check = Gtk.CheckButton(
                label="Purge configuration files (APT packages)"
            )
            purge_check.set_tooltip_text(
                "When enabled, APT packages will be purged instead of removed.\n"
                "This deletes configuration files as well."
            )
            content_area.append(purge_check)

            # Store reference on dialog so we can read it later
            dialog.purge_check = purge_check

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        remove_button = dialog.add_button("Remove", Gtk.ResponseType.OK)
        remove_button.add_css_class("destructive-action")
        dialog.connect("response", self.on_confirm_batch_response, removable)
        dialog.present()

    def on_confirm_batch_response(self, dialog, response, removable):
        dialog.close()

        if response != Gtk.ResponseType.OK:
            return

        # Read purge checkbox state
        purge = False
        if hasattr(dialog, "purge_check"):
            purge = dialog.purge_check.get_active()

        self.remove_marked_button.set_sensitive(False)

        self.install_progress_window = InstallProgressWindow(self, "Removing apps")
        self.install_progress_window.present()
        self.install_progress_window.start_pulse()
        self.install_progress_window.set_status("Preparing removal…")

        thread = threading.Thread(
            target=self.batch_worker,
            args=(removable, purge),
            daemon=True,
        )
        thread.start()
    # ------------------------------------------------------------
    # Leftover selection
    # ------------------------------------------------------------
    def unmark_all_leftovers(self):
        changed = False

        for app in self.get_leftover_entries():
            key = app_key(app)

            if key and key in self.marked_keys:
                self.marked_keys.remove(key)
                changed = True

        if changed:
            self.save_marked_keys()
            self.rebuild_list()

    def on_select_all_leftovers_toggled(self, button):
        if getattr(self, "_updating_select_all_leftovers", False):
            return

        if button.get_active():
            self.mark_all_leftovers()
        else:
            self.unmark_all_leftovers()

    def batch_worker(self, apps, purge=False):
        def progress_callback(current, total, message):
            if self.install_progress_window:
                line = f"[{current}/{total}] {message}"
                GLib.idle_add(
                    self.install_progress_window.append_output,
                    line,
                )
                GLib.idle_add(
                    self.install_progress_window.set_status,
                    message,
                )

        results, summary = execute_batch_removal(
            apps,
            progress_callback=progress_callback,
            purge=purge,
        )

        GLib.idle_add(self.on_batch_finished, results, summary)
        
    def on_batch_finished(self, results, summary):
        for app, success, message in results:
            if success:
                self.marked_keys.discard(app_key(app))

        self.save_marked_keys()

        success_count = sum(1 for _, success, _ in results if success)
        failed_count = len(results) - success_count

        lines = []

        for app, success, message in results[:30]:
            status = "OK" if success else "FAIL"
            name = getattr(app, "name", "Unknown")
            first_line = (message or "").splitlines()[0] if message else ""

            lines.append(f"[{status}] {name}: {first_line}")

        if len(results) > 30:
            lines.append(f"…and {len(results) - 30} more")

        detailed = "\n".join(lines)

        if self.install_progress_window:
            self.install_progress_window.stop_pulse()

            if detailed:
                self.install_progress_window.append_output("")
                self.install_progress_window.append_output(detailed)

        self.reload()

        if failed_count == 0:
            if self.install_progress_window:
                self.install_progress_window.close_window()
                self.install_progress_window = None

            self.show_message(
                "Removal finished",
                summary if summary else "Removal completed successfully.",
                Gtk.MessageType.INFO,
            )
        else:
            if self.install_progress_window:
                self.install_progress_window.finish_failure(summary)

        return False

    # ------------------------------------------------------------
    # Message dialog helper
    # ------------------------------------------------------------

    def show_message(self, title, message, message_type=None):
        if message_type is None:
            message_type = Gtk.MessageType.INFO

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
        )
        dialog.set_resizable(False)
        dialog.set_property("message-type", message_type)
        dialog.set_property("text", title)
        dialog.set_property("secondary-text", message)

        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.connect("response", lambda d, response: d.close())

        dialog.present()
        
    # ------------------------------------------------------------
    # Advanced visibility helpers
    # ------------------------------------------------------------

    def _entry_hidden_by_advanced_options(self, app):
        manager = getattr(app, "manager", "")

        is_leftover = manager == "Leftover"
        is_advanced = is_leftover or not getattr(app, "is_gui_app", True)

        if is_advanced:
            if is_leftover:
                return not self.show_leftovers

            return not self.show_advanced_apps

        return self.hide_basic_apps

    def _unmark_hidden_advanced_items(self):
        hidden_keys = set()

        for app in self.current_apps:
            if self._entry_hidden_by_advanced_options(app):
                hidden_keys.add(app_key(app))

        if hidden_keys:
            self.marked_keys.difference_update(hidden_keys)
            self.save_marked_keys()
