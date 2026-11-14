import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gtk, Adw, Gio, GLib, Gdk, GdkPixbuf, Pango

from .scanners import scan_all, scan_leftover_configs
from .models import mark_duplicate_apps
from .app_item import AppItem
from .details import DetailsWindow
from .ui.confirm_dialog import ConfirmPlanDialog
from .ui.review_dialogs import FlatpakRefReviewDialog
from .ui.progress_dialog import TaskProgressDialog
from .ui.tasks import CancelToken, run_async
from . import __version__
from .updater import check_for_update
from .actions import (
    app_key,
    execute_batch_removal,
    prepare_apt_batch_preview,
    add_flathub_remote,
    flathub_remote_exists,
    install_flatpak_source,
    install_deb_batch,
    install_appimage_batch,
    install_flatpak_ref_batch,
    install_flatpak_app_id_batch,
    extract_flathub_app_id,
    parse_flatpak_app_inputs,
    review_deb_batch,
    get_apt_autoremove_preview,
    execute_apt_autoremove,
    get_flatpak_unused_preview,
    execute_flatpak_unused_cleanup,
    check_available_updates,
    execute_available_updates,
    get_removal_size_estimate,

)
from ._proc import _log_action, state_dir
from .policy import can_remove, get_removal_risk, is_blocked, is_critical_apt_package_name
from .residual import (
    get_purge_backup_summaries,
    get_residual_conffile_details,
    list_purge_backups,
    prune_old_purge_backups,
    purge_leftover_configs,
    simulate_leftover_purge,
)
from .residual import RESIDUAL_PURGE_BACKUP_KEEP


# Single source of truth for keyboard shortcuts. The accelerators registered
# with the application and the shortcuts window are both generated from this
# table, so the help can never drift from the real bindings.
SHORTCUT_ACCELERATORS = (
    # (action name, accelerators)
    ("win.toggle-search", ["<Ctrl>f"]),
    ("win.refresh", ["<Ctrl>r"]),
    ("win.install", ["<Ctrl>o"]),
    ("win.cleanup-residuals", ["<Ctrl><Shift>k"]),
    ("win.show-log", ["<Ctrl>l"]),
    ("win.check-updates", ["<Ctrl>u"]),
    ("win.check-appector-updates", ["<Ctrl><Shift>u"]),
    ("win.export-app-list", ["<Ctrl><Shift>e"]),
    ("win.export-app-list-json", ["<Ctrl><Shift>j"]),
    ("win.shortcuts", ["<Ctrl>question"]),
    ("win.mark-all", ["<Ctrl>a"]),
    ("win.clear-marks", ["<Ctrl><Shift>a"]),
    # U5: a bare Delete hijacked the Delete key inside text entries, and
    # <Ctrl>a overrode select-all. Ctrl+Delete cannot clash, and the two
    # handlers additionally refuse to run while an editable has focus.
    ("win.remove-marked", ["<Ctrl>Delete"]),
)

# Grouped for the shortcuts window, in the order the groups are shown.
SHORTCUT_GROUPS = (
    (
        "General",
        (
            ("Search apps", "win.toggle-search"),
            ("Refresh installed app list", "win.refresh"),
            ("Install apps", "win.install"),
            ("Cleanup and residuals", "win.cleanup-residuals"),
            ("Activity Log", "win.show-log"),
            ("Check for installed app updates", "win.check-updates"),
            ("Check for Appector updates", "win.check-appector-updates"),
            ("Export installed app list as CSV", "win.export-app-list"),
            ("Export installed app list as JSON", "win.export-app-list-json"),
            ("Keyboard shortcuts", "win.shortcuts"),
        ),
    ),
    (
        "Sidebar scope",
        (
            ("All items", "win.scope-all"),
            ("Applications only", "win.scope-apps"),
            ("System items only", "win.scope-system"),
        ),
    ),
    (
        "Selection",
        (
            ("Mark all visible", "win.mark-all"),
            ("Clear marks", "win.clear-marks"),
            ("Remove marked", "win.remove-marked"),
        ),
    ),
)

_SCOPE_ACTIONS = {
    "all": ("scope-all", "<Ctrl>1"),
    "apps": ("scope-apps", "<Ctrl>2"),
    "system": ("scope-system", "<Ctrl>3"),
}


def _set_accessible_label(widget, label):
    """Give an icon-only control a name for screen readers.

    GTK tooltips are not exposed as an accessible name, so an icon-only button
    is announced as just "button" without this.
    """
    try:
        widget.update_property(
            [Gtk.AccessibleProperty.LABEL], [label]
        )
    except Exception:
        # Older GTK bindings may lack the property; the tooltip still helps.
        pass


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(
            application=app,
            title="Appector",
            default_width=1250,
            default_height=760,
        )
        self.set_icon_name("com.appector.appector")

        self.search_text = ""
        self.marked_only = False
        self.duplicates_only = False
        self.scope = "all"
        # Scope-derived visibility flags, kept in sync by
        # `_sync_legacy_scope_flags`.
        self.show_leftovers = True
        self.show_advanced_apps = True
        self.hide_basic_apps = False
        self.current_apps = []
        # Staged removal marks survive restarts; keys that no longer match a
        # scanned app are pruned on the first scan.
        self.marked_keys = self.load_marked_keys()
        self.context_item = None
        self.selected_count = 0
        self.selected_marked_count = 0
        self.removable_marked_count = 0
        self.progress_window = None
        self.install_progress_window = None
        self._selection_ui_pending = False
        self.view_mode = self.load_ui_state().get("view_mode", "table")
        if self.view_mode not in {"table", "grid"}:
            self.view_mode = "table"

        self._setup_css()
        self._setup_actions()

        # ------------------------------------------------------------
        # Header Bar (Redesigned)
        # ------------------------------------------------------------
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(
            title="Appector",
            subtitle="Linux app manager",
        )
        header.set_title_widget(self.window_title)

        # Left: Sidebar toggle
        self.sidebar_toggle_btn = Gtk.ToggleButton(
            icon_name="sidebar-show-symbolic",
            tooltip_text="Toggle Sidebar",
        )
        _set_accessible_label(self.sidebar_toggle_btn, "Toggle sidebar")
        self.sidebar_toggle_btn.set_active(True)
        self.sidebar_toggle_btn.connect("toggled", self.on_sidebar_toggle_btn_toggled)
        header.pack_start(self.sidebar_toggle_btn)

        # Keep display-mode controls beside navigation, separate from the main menu.
        self.table_view_button = Gtk.ToggleButton(
            icon_name="view-list-symbolic",
            tooltip_text="Table view",
        )
        self.grid_view_button = Gtk.ToggleButton(
            icon_name="view-grid-symbolic",
            tooltip_text="Grid view",
        )
        self.grid_view_button.set_group(self.table_view_button)
        self.table_view_button.set_active(self.view_mode == "table")
        self.grid_view_button.set_active(self.view_mode == "grid")
        self.table_view_button.connect(
            "toggled",
            self.on_view_mode_toggled,
            "table",
        )
        self.grid_view_button.connect(
            "toggled",
            self.on_view_mode_toggled,
            "grid",
        )
        view_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        view_controls.add_css_class("linked")
        view_controls.append(self.table_view_button)
        view_controls.append(self.grid_view_button)
        header.pack_start(view_controls)

        # Header count label: shows what is actually on screen, so the
        # subtitle is never stale after filtering.
        self.count_label = Gtk.Label(label="")
        self.count_label.add_css_class("dim-label")
        self.count_label.set_valign(Gtk.Align.CENTER)
        header.pack_end(self.count_label)

        self.reload_button = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text="Refresh",
        )
        _set_accessible_label(self.reload_button, "Refresh installed app list")
        self.reload_button.add_css_class("flat")
        self.reload_button.set_action_name("win.refresh")
        header.pack_end(self.reload_button)

        # ------------------------------------------------------------
        # Primary menu
        # ------------------------------------------------------------
        primary_menu_model = Gio.Menu()

        # Install: only one entry today, so it stays directly at the top
        # level instead of creating a one-item section.
        primary_menu_model.append("Install apps…", "win.install")

        # Maintenance
        maintenance_section = Gio.Menu()
        maintenance_section.append("Cleanup & residuals…", "win.cleanup-residuals")
        primary_menu_model.append_section("Maintenance", maintenance_section)

        # Updates: both update checks belong together.
        updates_section = Gio.Menu()
        updates_section.append("Installed app updates…", "win.check-updates")
        updates_section.append("Appector updates…", "win.check-appector-updates")
        primary_menu_model.append_section("Updates", updates_section)

        # Export: CSV and JSON share a parent instead of two loose items.
        export_section = Gio.Menu()
        export_section.append("Export as CSV…", "win.export-app-list")
        export_section.append("Export as JSON…", "win.export-app-list-json")
        primary_menu_model.append_section("Export", export_section)

        # Tools / help
        tools_section = Gio.Menu()
        tools_section.append("Activity Log", "win.show-log")
        tools_section.append("Keyboard Shortcuts", "win.shortcuts")
        primary_menu_model.append_section("Tools", tools_section)

        # About last, unlabelled so it renders as a plain trailing item.
        primary_menu_model.append("About Appector", "win.about")

        if not hasattr(self, "primary_menu_btn"):
            self.primary_menu_btn = Gtk.MenuButton()
            self.primary_menu_btn.set_icon_name("open-menu-symbolic")
            self.primary_menu_btn.set_tooltip_text("Main Menu")
            _set_accessible_label(self.primary_menu_btn, "Main menu")
            header.pack_end(self.primary_menu_btn)

        self.primary_menu_btn.set_menu_model(primary_menu_model)

        # ------------------------------------------------------------
        # Data model & Column View (Unchanged)
        # ------------------------------------------------------------
        self.list_store = Gio.ListStore(item_type=AppItem)
        self.custom_filter = Gtk.CustomFilter.new(self.filter_func)
        self.filter_model = Gtk.FilterListModel.new(self.list_store, self.custom_filter)
        self.sort_model = Gtk.SortListModel.new(self.filter_model)
        self.selection = Gtk.MultiSelection.new(self.sort_model)
        self.selection.connect("selection-changed", self.on_selection_changed)

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
        source_column = self.create_column("Origin", "source", expand=False, min_width=160, fixed_width=200)
        manager_column = self.create_manager_column()
        version_column = self.create_column("Version", "version", expand=False, min_width=120, fixed_width=160)

        for column in [marked_column, name_column, source_column, manager_column, version_column]:
            self.column_view.append_column(column)

        self.column_widgets = {
            "marked": marked_column, "name": name_column, "source": source_column,
            "manager": manager_column, "version": version_column,
        }

        self.restore_ui_state()
        self.connect("close-request", self.on_close_request)
        # A window of the right size should open at that size, not just hint it.
        self.connect("map", self._on_map_once)

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
        # Kept as attributes so a rebuild can restore the scroll position.
        self.column_scrolled = scrolled

        grid_factory = Gtk.SignalListItemFactory()
        grid_factory.connect("setup", self.on_setup_grid_item)
        grid_factory.connect("bind", self.on_bind_grid_item)
        self.grid_view = Gtk.GridView.new(self.selection, grid_factory)
        self.grid_view.set_min_columns(1)
        self.grid_view.set_max_columns(6)
        self.grid_view.set_single_click_activate(False)
        self.grid_view.connect("activate", self.on_activate)
        self.grid_view.set_hexpand(True)
        self.grid_view.set_vexpand(True)

        grid_scrolled = Gtk.ScrolledWindow()
        grid_scrolled.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        grid_scrolled.set_child(self.grid_view)
        grid_scrolled.set_hexpand(True)
        grid_scrolled.set_vexpand(True)
        self.grid_scrolled = grid_scrolled

        self.view_stack = Gtk.Stack()
        self.view_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.view_stack.add_titled(scrolled, "table", "Table")
        self.view_stack.add_titled(grid_scrolled, "grid", "Grid")
        self.view_stack.set_visible_child_name(self.view_mode)

        self._setup_context_menu()

        # Contextual selection action bar
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

        self.remove_action_bar = Gtk.ActionBar()
        self.remove_marked_summary = Gtk.Label(label="0 marked")
        self.remove_marked_summary.add_css_class("dim-label")
        self.remove_marked_button = Gtk.Button(label="Remove marked…")
        self.remove_marked_button.add_css_class("destructive-action")
        self.remove_marked_button.set_action_name("win.remove-marked")
        self.remove_action_bar.pack_start(self.remove_marked_summary)
        self.remove_action_bar.pack_end(self.remove_marked_button)

        self.remove_action_revealer = Gtk.Revealer()
        self.remove_action_revealer.set_child(self.remove_action_bar)
        self.remove_action_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_UP
        )
        self.remove_action_revealer.set_reveal_child(False)

        # Main Stack for Empty States
        self.main_stack = Gtk.Stack()
        self.main_stack.set_hexpand(True)
        self.main_stack.set_vexpand(True)

        self.empty_page = Adw.StatusPage()
        self.empty_page.set_icon_name("edit-find-symbolic")
        self.empty_page.set_title("No apps found")
        self.empty_page.set_description("Try changing your search or filters.")
        # An empty state should offer the next step, not just state the
        # problem. The button only appears when a filter is actually active.
        self.clear_filters_button = Gtk.Button(label="Clear filters")
        self.clear_filters_button.add_css_class("pill")
        self.clear_filters_button.set_visible(False)
        self.clear_filters_button.connect("clicked", self.on_clear_filters_clicked)
        self.empty_page.set_child(self.clear_filters_button)

        self.main_stack.add_named(self.view_stack, "list")
        self.main_stack.add_named(self.empty_page, "empty")
        self.filter_model.connect("items-changed", self.on_filter_items_changed)

        # Sidebar
        self.sidebar_revealer = self._build_sidebar()
        sidebar_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        sidebar_wrap.set_hexpand(False)
        sidebar_wrap.append(self.sidebar_revealer)
        sidebar_wrap.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # Main content assembly
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.set_hexpand(True)
        main_box.set_vexpand(True)
        main_box.append(self.main_stack)
        main_box.append(self.action_revealer)
        main_box.append(self.remove_action_revealer)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        content.set_hexpand(True)
        content.set_vexpand(True)
        content.append(sidebar_wrap)
        content.append(main_box)

        # F6/U8: "1,204 apps" is not an error, but a silently short count
        # is. The banner names the sources that failed or are missing, and
        # offers the per-source detail.
        self.scan_banner = Adw.Banner()
        self.scan_banner.set_button_label("Details")
        self.scan_banner.connect("button-clicked", self.on_scan_banner_details)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(header)
        outer.append(self.scan_banner)
        outer.append(content)
        outer.set_hexpand(True)
        outer.set_vexpand(True)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(outer)
        self.set_content(self.toast_overlay)
        self._add_file_drop_target(self, self.on_main_files_dropped)

        # Initial scan (single call; a duplicate call previously spawned two
        # concurrent scan threads and toggled button sensitivity twice).
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
            state["view_mode"] = self.view_mode

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

    def _on_map_once(self, *_args):
        """Re-assert the saved window size the first time the window is shown.

        `restore_ui_state` runs during construction, before the window
        manager has applied its constraints, so the requested size is usually
        ignored. Re-applying it after the first map fixes that.
        """
        if getattr(self, "_map_size_applied", False):
            return
        self._map_size_applied = True
        GLib.idle_add(self._apply_window_size_on_map)

    def _apply_window_size_on_map(self):
        """Apply the stored window size; runs from the main loop."""
        state = self.load_ui_state()
        size = state.get("window_size")

        if not (isinstance(size, list) and len(size) == 2):
            return False

        try:
            width = int(size[0])
            height = int(size[1])
        except (TypeError, ValueError):
            return False

        if width > 0 and height > 0:
            try:
                self.set_default_size(width, height)
            except Exception:
                pass
        return False

    def on_close_request(self, *args):
        self.save_ui_state()
        return False

    def _worker_failed(self, what, error):
        """U3: a crashed worker must never leave a modal window behind."""
        for attribute in ("progress_window", "install_progress_window"):
            dialog = getattr(self, attribute, None)
            if dialog is not None:
                try:
                    dialog.finish_failure(error)
                except Exception:
                    try:
                        dialog.close()
                    except Exception:
                        pass
                setattr(self, attribute, None)
        self.reload_button.set_sensitive(True)
        if hasattr(self, "remove_marked_button"):
            self.remove_marked_button.set_sensitive(True)
        self.show_message(
            "Operation failed",
            f"The {what} failed unexpectedly:\n{error}",
            Gtk.MessageType.ERROR,
        )

    def show_toast(self, message):
        try:
            toast = Adw.Toast.new(message)
            toast.set_timeout(3)
            self.toast_overlay.add_toast(toast)
        except Exception:
            self.show_message(
                "Appector",
                message,
                Gtk.MessageType.INFO,
            )

    def on_filter_items_changed(self, *args):
        if getattr(self, "_empty_state_pending", False):
            return

        self._empty_state_pending = True
        GLib.idle_add(self._update_empty_state_idle)

    def _update_count_label(self, visible_count):
        """Show the on-screen item count next to the refresh button."""
        if not hasattr(self, "count_label"):
            return

        total = len(getattr(self, "current_apps", []) or [])
        if visible_count == total:
            text = f"{total} item{'' if total == 1 else 's'}"
        else:
            text = f"{visible_count} of {total}"
        self.count_label.set_label(text)

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

        self._update_count_label(visible_count)

        if visible_count > 0:
            self.main_stack.set_visible_child_name("list")
            return

        title = "No apps found"
        description = "Try changing your search or filters."

        if getattr(self, "search_text", ""):
            title = "No results"
            # U7: the description is parsed as Pango markup, so the search
            # text must be escaped (& or <x> produced warnings and a blank
            # description).
            description = (
                f"No apps match "
                f"“{GLib.markup_escape_text(self.search_text)}”."
            )

        elif getattr(self, "marked_only", False):
            title = "No marked apps"
            description = "Mark apps to see them here."

        elif getattr(self, "duplicates_only", False):
            title = "No duplicates found"
            description = "Your installed apps do not appear to have duplicates."

        elif getattr(self, "scope", "all") == "system" and not getattr(
            self, "show_leftovers", False
        ):
            title = "No system items found"
            description = (
                "No runtimes, components, or leftover APT configurations were "
                "detected."
            )

        elif getattr(self, "scope", "all") == "apps":
            title = "No applications found"
            description = "No installed applications match the current filters."

        self.empty_page.set_title(title)
        self.empty_page.set_description(description)

        # Offer a way out only when a filter is actually narrowing the list.
        can_clear = bool(
            getattr(self, "search_text", "")
            or getattr(self, "marked_only", False)
            or getattr(self, "duplicates_only", False)
            or getattr(self, "scope", "all") != "all"
        )
        if hasattr(self, "clear_filters_button"):
            self.clear_filters_button.set_visible(can_clear)

        self.main_stack.set_visible_child_name("empty")

    def on_clear_filters_clicked(self, _button=None):
        """Reset every active filter so the full list is visible again."""
        self.search_text = ""
        if hasattr(self, "search_entry"):
            self.search_entry.set_text("")
        self.marked_only = False
        self.duplicates_only = False
        if hasattr(self, "marked_only_toggle"):
            self.marked_only_toggle.set_active(False)
        if hasattr(self, "duplicates_only_toggle"):
            self.duplicates_only_toggle.set_active(False)
        self.set_scope("all")
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.update_empty_state()

    def set_scope(self, scope):
        """Switch the sidebar scope selector, keeping state consistent."""
        if scope not in ("all", "apps", "system"):
            return
        if scope == self.scope:
            return

        self.scope = scope
        self._sync_legacy_scope_flags()
        hidden_count = self._unmark_hidden_advanced_items()

        if hidden_count:
            self.show_toast(
                f"{hidden_count} marked item(s) hidden by the scope filter "
                "were unmarked"
            )

        button = getattr(self, "scope_buttons", {}).get(scope)
        if button is not None and not button.get_active():
            button.set_active(True)

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

    def on_check_updates_clicked(self, button=None):
        self.progress_window = TaskProgressDialog(
            self,
            "Checking for installed app updates",
        )
        self.progress_window.present(self)
        self.progress_window.start_indeterminate(
            "Checking available APT, Flatpak, and Snap package updates…"
        )
        run_async(
            self.check_updates_worker,
            on_error=lambda error: self._worker_failed("update check", error),
            name="check-updates",
        )

    def check_updates_worker(self):
        success, message = check_available_updates()
        GLib.idle_add(self.on_check_updates_finished, success, message)

    def on_check_updates_finished(self, success, message):
        if self.progress_window:
            self.progress_window.close_window()
            self.progress_window = None
        update_items = message.get("updates", [])
        dialog = Gtk.Dialog(
            transient_for=self,
            modal=True,
            title="Available updates" if update_items else (
                "Update check results" if success else "Update check failed"
            ),
            default_width=700,
            default_height=620,
        )
        dialog.set_resizable(True)

        content = dialog.get_content_area()
        content.set_spacing(12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_width(540)
        scrolled.set_min_content_height(440)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        results_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
        )
        results_box.set_margin_top(8)
        results_box.set_margin_bottom(8)
        results_box.set_margin_start(8)
        results_box.set_margin_end(8)
        scrolled.set_child(results_box)

        manager_names = {
            "APT": "APT",
            "Flatpak": "Flatpak",
            "Snap": "Snap",
        }
        selections = []
        groups = {}
        for item in update_items:
            key = (item["manager"], item.get("scope"))
            groups.setdefault(key, []).append(item)

        for manager, count in message.get("checks", []):
            header = Gtk.Label(
                label=f"{manager}  ·  {count} update{'s' if count != 1 else ''}",
            )
            header.set_xalign(0)
            header.add_css_class("heading")
            results_box.append(header)

            key = (
                "Flatpak",
                manager.removeprefix("Flatpak (").removesuffix(")")
                if manager.startswith("Flatpak (")
                else None,
            )
            for item in groups.pop(key, []):
                row = Gtk.CheckButton()
                row.set_active(True)
                label = Gtk.Label(
                    label=item["id"] + (
                        f"   {item['detail']}" if item.get("detail") else ""
                    ),
                )
                label.set_xalign(0)
                label.set_wrap(True)
                label.set_selectable(True)
                row.set_child(label)
                row.set_hexpand(True)
                results_box.append(row)
                selections.append((row, item))

            if count == 0:
                empty = Gtk.Label(label="No updates available.")
                empty.set_xalign(0)
                empty.add_css_class("dim-label")
                results_box.append(empty)

        for (manager, scope), items in groups.items():
            label = manager_names.get(manager, manager)
            if scope:
                label += f" ({scope})"
            heading = Gtk.Label(
                label=f"{label}  ·  {len(items)} update{'s' if len(items) != 1 else ''}",
            )
            heading.set_xalign(0)
            heading.add_css_class("heading")
            results_box.append(heading)
            for item in items:
                row = Gtk.CheckButton()
                row.set_active(True)
                text = item["id"] + (
                    f"   {item['detail']}" if item.get("detail") else ""
                )
                row_label = Gtk.Label(label=text)
                row_label.set_xalign(0)
                row_label.set_wrap(True)
                row_label.set_selectable(True)
                row.set_child(row_label)
                row.set_hexpand(True)
                results_box.append(row)
                selections.append((row, item))

        for error in message.get("errors", []):
            error_label = Gtk.Label(label=error)
            error_label.set_xalign(0)
            error_label.set_wrap(True)
            error_label.add_css_class("error")
            results_box.append(error_label)

        for note in message.get("notes", []):
            note_label = Gtk.Label(label=note)
            note_label.set_xalign(0)
            note_label.set_wrap(True)
            note_label.add_css_class("dim-label")
            results_box.append(note_label)

        content.append(scrolled)

        update_button = dialog.add_button(
            "Update selected",
            Gtk.ResponseType.APPLY,
        )
        update_button.set_sensitive(bool(selections))
        close_button = dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.set_default_widget(update_button if selections else close_button)

        def update_selection_state(*_args):
            selected_count = sum(button.get_active() for button, _item in selections)
            update_button.set_label(
                f"Update selected ({selected_count})"
            )
            update_button.set_sensitive(selected_count > 0)

        for check_button, _item in selections:
            check_button.connect("toggled", update_selection_state)
        update_selection_state()

        def on_update_results_response(current_dialog, response):
            if response == Gtk.ResponseType.APPLY:
                selected = [
                    item for check_button, item in selections
                    if check_button.get_active()
                ]
                current_dialog.close()
                self.start_available_updates(selected)
            else:
                current_dialog.close()

        dialog.connect("response", on_update_results_response)
        dialog.present()
        return False

    def start_available_updates(self, updates):
        self._updates_cancel = CancelToken()
        self.install_progress_window = TaskProgressDialog(
            self,
            "Updating apps",
            cancel_token=self._updates_cancel,
        )
        self.install_progress_window.present(self)
        self.install_progress_window.set_status(
            f"Installing {len(updates)} selected update(s)…"
        )
        run_async(
            lambda: self.available_updates_worker(updates),
            on_error=lambda error: self._worker_failed("update installation", error),
            name="install-updates",
        )

    def available_updates_worker(self, updates):
        progress_window = self.install_progress_window

        def output_callback(line):
            if progress_window:
                GLib.idle_add(progress_window.append_output, line)

        success, message = execute_available_updates(
            updates,
            output_callback=output_callback,
        )
        GLib.idle_add(
            self.on_available_updates_finished,
            success,
            message,
            progress_window,
        )

    def on_available_updates_finished(self, success, message, progress_window):
        if progress_window:
            progress_window.finish_update_results(message, success)
        self.reload()
        return False

    def on_export_app_list_clicked(self, button=None):
        self._show_export_app_list_dialog("csv")

    def on_export_app_list_json_clicked(self, button=None):
        self._show_export_app_list_dialog("json")

    def _show_export_app_list_dialog(self, file_format):
        initial_name = f"installed-apps.{file_format}"
        if hasattr(Gtk, "FileDialog"):
            file_dialog = Gtk.FileDialog.new()
            file_dialog.set_title(
                f"Export installed app list as {file_format.upper()}"
            )
            file_dialog.set_initial_name(initial_name)

            def on_saved(dialog, result):
                try:
                    file = dialog.save_finish(result)
                    path = file.get_path() if file else None
                    if path:
                        self._export_installed_apps(path, file_format)
                except GLib.Error as error:
                    # U6: DISMISSED/CANCELLED is the user closing the
                    # chooser — not a failure to report.
                    if error.domain == "gtk-dialog-error-quark" and error.code in (
                        0,  # Gtk.DialogError.DISMISSED
                        1,  # Gtk.DialogError.CANCELLED
                    ):
                        return
                    self.show_message("Export failed", str(error),
                                      Gtk.MessageType.ERROR)
                except Exception as error:
                    self.show_message(
                        "Export failed",
                        str(error),
                        Gtk.MessageType.ERROR,
                    )

            file_dialog.save(self, None, on_saved)
            return

        chooser = Gtk.FileChooserNative.new(
            f"Export installed app list as {file_format.upper()}",
            self,
            Gtk.FileChooserAction.SAVE,
            "Export",
            "Cancel",
        )
        chooser.set_current_name(initial_name)
        chooser.connect(
            "response",
            self.on_export_app_list_chooser_response,
            file_format,
        )
        chooser.show()

    def on_export_app_list_chooser_response(self, chooser, response, file_format):
        if response == Gtk.ResponseType.ACCEPT:
            file = chooser.get_file()
            path = file.get_path() if file else None
            if path:
                self._export_installed_apps(path, file_format)
        chooser.destroy()

    def _export_installed_apps(self, path, file_format="csv"):
        """Export the installed app list via the shared exporter (S10).

        Formula-injection escaping, atomic 0600 writes and symlink
        replacement live in `appector.export`, which is testable headless.
        """
        from .export import write_app_export

        try:
            count = write_app_export(path, self.current_apps, file_format)
        except (OSError, ValueError, TypeError) as error:
            self.show_message(
                "Export failed",
                f"Could not save the installed app list:\n{error}",
                Gtk.MessageType.ERROR,
            )
            return

        self.show_message(
            "Export complete",
            f"Exported {count} installed app(s) as {file_format.upper()} "
            f"to:\n{path}",
            Gtk.MessageType.INFO,
        )

    # ------------------------------------------------------------
    # .deb installation
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # .deb batch installation
    # ------------------------------------------------------------

    def on_install_clicked(self, button=None, initial_paths=None):
        self.show_install_dialog(button, initial_paths)

    def open_install_files(self, paths):
        """Open the install dialog for files passed via CLI / file association."""
        paths = [str(path) for path in (paths or []) if str(path or "").strip()]
        if not paths:
            return

        deb_paths, flatpakref_paths, appimage_paths, unsupported = (
            self._classify_install_paths(paths)
        )
        supported = deb_paths + flatpakref_paths + appimage_paths

        if unsupported:
            self.show_message(
                "Unsupported files",
                (
                    "Only .deb packages, .flatpakref files, and .AppImage "
                    "files can be opened with Appector:\n\n"
                    + "\n".join(unsupported)
                ),
                Gtk.MessageType.WARNING,
            )

        if supported:
            self.show_install_dialog(initial_paths=supported)


    def start_install_files_batch(
        self,
        deb_paths,
        flatpakref_paths,
        appimage_paths=None,
        user_install=True,
        delete_source=False,
        deb_review=None,
    ):
        appimage_paths = appimage_paths or []
        file_count = len(deb_paths) + len(flatpakref_paths) + len(appimage_paths)
        self.install_progress_window = TaskProgressDialog(
            self,
            "Installing apps",
        )
        self.install_progress_window.present(self)
        self.install_progress_window.set_status(
            f"Installing {file_count} package file(s)…"
        )

        run_async(
            lambda: self.install_files_batch_worker(
                deb_paths,
                flatpakref_paths,
                appimage_paths,
                user_install,
                delete_source,
                deb_review,
            ),
            on_error=lambda error: self._worker_failed(
                "installation", error
            ),
            name="install-batch",
        )

    def _confirm_appimage_install(
        self,
        deb_paths,
        flatpakref_paths,
        appimage_paths,
        user_install,
        delete_source,
    ):
        def start_install():
            if deb_paths:
                self.start_deb_install_review(
                    deb_paths,
                    flatpakref_paths,
                    appimage_paths,
                    user_install,
                    delete_source,
                )
            else:
                self.start_install_files_batch(
                    deb_paths,
                    flatpakref_paths,
                    appimage_paths,
                    user_install,
                    delete_source,
                )

        if flatpakref_paths:
            from .actions import flatpakref_review_text, parse_flatpakref_file

            review_texts = []
            reviewed_fields = {}
            for ref_path in flatpakref_paths:
                review_texts.append(flatpakref_review_text(ref_path))
                reviewed_fields[
                    str(Path(ref_path).expanduser().absolute())
                ] = parse_flatpakref_file(ref_path)

            self._flatpakref_reviewed_fields = reviewed_fields
            dialog = FlatpakRefReviewDialog(
                self,
                review_texts,
                on_install=start_install,
            )
            dialog.present(self)
            return

        if not appimage_paths:
            start_install()
            return

        warning = (
            "AppImages are executable programs and are not sandboxed by Appector. "
            "Only install files from publishers you trust. The app will copy each "
            "AppImage into your user applications folder and create a launcher; it "
            "will not run the AppImage during installation. Its name is inferred from "
            "the file name and it will use a generic icon. Some AppImages require "
            "FUSE support to launch."
        )
        if delete_source:
            warning += (
                "\n\nAfter successful installation, selected source files will be "
                "moved to Trash (not permanently deleted); the managed AppImage "
                "copies will remain."
            )
        self._show_confirm_dialog(
            "Review AppImage installation",
            warning,
            "Continue",
            start_install,
        )

    def start_deb_install_review(
        self,
        deb_paths,
        flatpakref_paths,
        appimage_paths,
        user_install,
        delete_source,
    ):
        self.install_progress_window = TaskProgressDialog(
            self,
            "Reviewing Debian packages",
        )
        self.install_progress_window.present(self)
        self.install_progress_window.set_status(
            "Inspecting package metadata and simulating the APT transaction…"
        )

        run_async(
            lambda: self.deb_install_review_worker(
                deb_paths,
                flatpakref_paths,
                appimage_paths,
                user_install,
                delete_source,
            ),
            on_error=lambda error: self._worker_failed(
                "package review", error
            ),
            name="deb-review",
        )

    def deb_install_review_worker(
        self,
        deb_paths,
        flatpakref_paths,
        appimage_paths,
        user_install,
        delete_source,
    ):
        review, error = review_deb_batch(deb_paths)
        GLib.idle_add(
            self.on_deb_install_review_ready,
            review,
            error,
            flatpakref_paths,
            appimage_paths,
            user_install,
            delete_source,
        )

    def on_deb_install_review_ready(
        self,
        review,
        error,
        flatpakref_paths,
        appimage_paths,
        user_install,
        delete_source,
    ):
        if self.install_progress_window:
            self.install_progress_window.stop_pulse()
            self.install_progress_window.close_window()
            self.install_progress_window = None

        if review is None:
            self.show_message(
                "Could not review Debian package",
                error,
                Gtk.MessageType.ERROR,
            )
            return False

        # U10/F13: the structured review dialog shows per-package facts
        # (metadata, maintainer scripts, setuid files, /etc/apt paths,
        # systemd units) instead of a text dump.
        from .ui.review_dialogs import DebReviewDialog

        def on_install():
            self.start_install_files_batch(
                review.staged_paths,
                flatpakref_paths,
                appimage_paths,
                user_install,
                delete_source,
                review,
            )

        dialog = DebReviewDialog(
            self,
            review,
            on_install=on_install,
            delete_source=delete_source,
        )
        dialog.present(self)
        return False

    def install_files_batch_worker(
        self,
        deb_paths,
        flatpakref_paths,
        appimage_paths,
        user_install,
        delete_source,
        deb_review=None,
    ):
        def output_callback(line):
            if self.install_progress_window:
                GLib.idle_add(
                    self.install_progress_window.append_output,
                    line,
                )

        results = []
        success_count = 0
        total_count = len(deb_paths) + len(flatpakref_paths) + len(appimage_paths)

        if deb_paths:
            if not shutil.which("apt-get"):
                results.append("DEB packages:\nAPT is not available.")
            else:
                success, message = install_deb_batch(
                    deb_paths,
                    output_callback=output_callback,
                    delete_source=delete_source,
                    review=deb_review,
                )
                if success:
                    success_count += len(deb_paths)
                    installed_paths = (
                        deb_review.original_paths
                        if deb_review is not None
                        else deb_paths
                    )
                    installed_names = "\n".join(
                        f"  {Path(path).name}" for path in installed_paths
                    )
                    results.append(f"DEB packages installed:\n{installed_names}")
                    cleanup_lines = [
                        line for line in message.splitlines()
                        if line.startswith("Moved to Trash:")
                        or "Source file kept" in line
                        or "Could not move installation file to Trash" in line
                    ]
                    if cleanup_lines:
                        results.append("\n".join(cleanup_lines))
                else:
                    results.append(f"DEB package installation failed:\n{message}")

        if flatpakref_paths:
            if not shutil.which("flatpak"):
                results.append("Flatpak references:\nFlatpak is not available.")
            else:
                success, message, resolved_count = install_flatpak_ref_batch(
                    flatpakref_paths,
                    user_install,
                    output_callback=output_callback,
                    delete_source=delete_source,
                    reviewed_fields_by_path=getattr(
                        self, "_flatpakref_reviewed_fields", None
                    ),
                )
                results.append(f"Flatpak references:\n{message}")
                success_count += resolved_count

        if appimage_paths:
            success, message, resolved_count = install_appimage_batch(
                appimage_paths,
                output_callback=output_callback,
                delete_source=delete_source,
            )
            results.append(f"AppImages:\n{message}")
            success_count += resolved_count

        success = success_count == total_count
        if deb_review is not None:
            deb_review.close()
        GLib.idle_add(
            self.on_install_files_batch_finished,
            success,
            "\n\n".join(results),
        )

    def on_install_files_batch_finished(self, success, message):
        if self.install_progress_window:
            self.install_progress_window.stop_pulse()
            self.reload()

            if success:
                self.install_progress_window.close_window()
                self.install_progress_window = None
                self.show_message(
                    "Installation finished",
                    message,
                    Gtk.MessageType.INFO,
                )
            else:
                self.install_progress_window.finish_batch_results(message)

        return False

    # ------------------------------------------------------------
    # Flatpak installation
    # ------------------------------------------------------------

    def show_install_dialog(self, button=None, initial_paths=None):
        """U4: the install dialog is built in ui/install_dialog.py."""
        from .ui.install_dialog import build_install_dialog

        dialog = build_install_dialog(
            self,
            on_browse=self.on_flatpakref_browse_clicked,
            on_response=self.on_flatpak_install_response,
            on_mode_changed=self.on_flatpak_install_mode_changed,
            on_state_changed=self.on_flatpak_install_state_changed,
            on_batch_text_changed=self.on_flatpak_batch_text_changed,
            on_batch_expander_changed=self.on_flatpak_batch_expander_changed,
            add_drop_target=lambda dialog: self._add_file_drop_target(
                dialog, self.on_installer_files_dropped, dialog
            ),
            set_paths=self._set_installer_dialog_paths,
            update_state=self.update_flatpak_install_dialog_state,
            initial_paths=initial_paths,
        )
        dialog.present(self)

    def _add_file_drop_target(self, widget, callback, *user_data):
        if not hasattr(Gtk, "DropTarget") or not hasattr(Gdk, "FileList"):
            return

        target = Gtk.DropTarget.new(Gdk.FileList.__gtype__, Gdk.DragAction.COPY)
        target.connect("drop", callback, *user_data)
        widget.add_controller(target)

    @staticmethod
    def _paths_from_file_list(file_list):
        if not isinstance(file_list, Gdk.FileList):
            return []

        paths = []
        for file in file_list.get_files():
            path = file.get_path()
            if path:
                paths.append(path)
        return paths

    @staticmethod
    def _classify_install_paths(paths):
        deb_paths = []
        flatpakref_paths = []
        appimage_paths = []
        unsupported = []

        for path in paths:
            suffix = Path(path).suffix.lower()
            if suffix == ".deb":
                deb_paths.append(path)
            elif suffix == ".flatpakref":
                flatpakref_paths.append(path)
            elif suffix == ".appimage":
                appimage_paths.append(path)
            else:
                unsupported.append(path)

        return deb_paths, flatpakref_paths, appimage_paths, unsupported

    def on_main_files_dropped(self, target, file_list, x, y):
        paths = self._paths_from_file_list(file_list)
        if not paths:
            return False

        deb_paths, flatpakref_paths, appimage_paths, unsupported = self._classify_install_paths(paths)
        if unsupported:
            self.show_message(
                "Unsupported dropped files",
                "Drop .deb packages, .flatpakref files, and/or .AppImage files.",
                Gtk.MessageType.WARNING,
            )
            return False

        self.on_install_clicked(
            initial_paths=deb_paths + flatpakref_paths + appimage_paths
        )
        return True

    def on_installer_files_dropped(self, target, file_list, x, y, dialog):
        paths = self._paths_from_file_list(file_list)
        if not paths:
            return False

        deb_paths, flatpakref_paths, appimage_paths, unsupported = self._classify_install_paths(paths)
        if unsupported:
            dialog.status_label.set_label(
                "Drop only .deb, .flatpakref, or .AppImage files."
            )
            return False

        self._set_installer_dialog_paths(
            dialog,
            deb_paths + flatpakref_paths + appimage_paths,
        )
        dialog.mode_ref_button.set_active(True)
        return True

    def _set_installer_dialog_paths(self, dialog, paths):
        dialog.flatpakref_paths = list(dict.fromkeys(paths))
        dialog._setting_file_entry = True
        if len(dialog.flatpakref_paths) == 1:
            dialog.file_entry.set_text(dialog.flatpakref_paths[0])
        else:
            dialog.file_entry.set_text(
                f"{len(dialog.flatpakref_paths)} installation files selected"
            )
        dialog._setting_file_entry = False
        self.update_flatpak_install_dialog_state(dialog)

    def on_flatpak_install_mode_changed(self, button, dialog):
        self.update_flatpak_install_dialog_state(dialog)

    def on_flatpak_batch_text_changed(self, buffer, dialog):
        self.update_flatpak_install_dialog_state(dialog)

    def on_flatpak_batch_expander_changed(self, expander, param, dialog):
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

        dialog.batch_expander.set_visible(mode_id)

        if mode_id:
            dialog.user_check.set_visible(True)
            batch_mode = dialog.batch_expander.get_expanded()
            dialog.id_label.set_visible(not batch_mode)
            dialog.id_entry.set_visible(not batch_mode)

            value = dialog.id_entry.get_text().strip()
            app_id = extract_flathub_app_id(value) or value

            batch_buffer = dialog.batch_text_view.get_buffer()
            batch_start, batch_end = batch_buffer.get_bounds()
            batch_text = batch_buffer.get_text(batch_start, batch_end, True)
            batch_ids, batch_issues = parse_flatpak_app_inputs(batch_text)

            try:
                flathub_exists = flathub_remote_exists(user_install)
            except Exception:
                flathub_exists = False

            scope_name = "user" if user_install else "system"

            if batch_mode:
                invalid_count = sum(
                    "invalid app ID or Flathub URL" in issue
                    for issue in batch_issues
                )
                duplicate_count = len(batch_issues) - invalid_count
                batch_status = (
                    f"{len(batch_ids)} app(s) ready."
                    if batch_ids
                    else "Paste one or more app IDs or Flathub URLs."
                )

                if duplicate_count:
                    batch_status += f" {duplicate_count} duplicate(s) will be skipped."
                if invalid_count:
                    batch_status += f" {invalid_count} invalid line(s) need attention."
                if batch_issues:
                    batch_status += "\n" + "\n".join(batch_issues[:8])

                if flathub_exists:
                    install_enabled = bool(batch_ids) and invalid_count == 0
                    if not batch_issues:
                        batch_status = (
                            f"{len(batch_ids)} app(s) ready for {scope_name} installation."
                        )
                    add_enabled = False
                elif batch_ids:
                    batch_status += (
                        f"\nFlathub remote is not available in {scope_name} scope."
                    )
                    add_enabled = bool(batch_ids) and invalid_count == 0
                    install_enabled = False
                else:
                    add_enabled = False
                    install_enabled = False

                dialog.batch_status_label.set_label(batch_status)
                dialog.batch_status_label.set_visible(True)
                status = "Review the list above before installing."
            else:
                dialog.batch_status_label.set_label("")
                dialog.batch_status_label.set_visible(False)

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

                if extract_flathub_app_id(value):
                    status = (
                        f"Flathub app page detected: {app_id}.\n"
                        f"{status}"
                    )

            if hasattr(dialog, "delete_source_check"):
                dialog.delete_source_check.set_sensitive(False)
                dialog.delete_source_check.set_active(False)
        else:
            dialog.id_label.set_visible(True)
            dialog.id_entry.set_visible(True)
            paths = getattr(dialog, "flatpakref_paths", []) or []
            value = dialog.file_entry.get_text().strip()
            has_flatpakref = any(
                Path(path).suffix.lower() == ".flatpakref"
                for path in paths
            ) or (
                not paths
                and Path(value.split("?", 1)[0]).suffix.lower() == ".flatpakref"
            ) or (
                not paths and bool(extract_flathub_app_id(value))
            )
            dialog.user_check.set_visible(has_flatpakref)

            if paths:
                install_enabled = True
                add_enabled = False

                deb_paths, flatpakref_paths, appimage_paths, unsupported = self._classify_install_paths(paths)
                if unsupported:
                    install_enabled = False
                    status = "Only .deb, .flatpakref, or .AppImage files can be installed here."
                else:
                    status = (
                        f"{len(deb_paths)} .deb and "
                        f"{len(flatpakref_paths)} .flatpakref and "
                        f"{len(appimage_paths)} .AppImage file(s) selected."
                    )
                    if appimage_paths:
                        status += (
                            "\nAppImages will be copied into your user applications "
                            "folder and launched without sandboxing."
                        )

                if hasattr(dialog, "delete_source_check"):
                    dialog.delete_source_check.set_sensitive(not unsupported)
            else:
                flathub_app_id = extract_flathub_app_id(value)

                if flathub_app_id:
                    try:
                        flathub_exists = flathub_remote_exists(user_install)
                    except Exception:
                        flathub_exists = False

                    scope_name = "user" if user_install else "system"
                    status = f"Flathub app page detected: {flathub_app_id}."
                    if flathub_exists:
                        status += f"\nFlathub remote is available in {scope_name} scope."
                        install_enabled = True
                    else:
                        status += (
                            f"\nFlathub remote is not available in {scope_name} scope.\n"
                            "Click Add Flathub to add it."
                        )
                        add_enabled = True
                else:
                    install_enabled = bool(value)
                    add_enabled = False
                    if value and not (
                        value.startswith("http://")
                        or value.startswith("https://")
                        or Path(value).suffix.lower()
                        in {".deb", ".flatpakref", ".appimage"}
                    ):
                        install_enabled = False
                        status = (
                            "Choose a .deb, .flatpakref, or .AppImage file, "
                            "or paste a Flatpak URL."
                        )
                    else:
                        status = "Choose or drop install files, or paste a Flatpak URL."

                if hasattr(dialog, "delete_source_check"):
                    is_url = (
                        value.startswith("http://")
                        or value.startswith("https://")
                    )
                    is_local_file = bool(value) and not is_url

                    dialog.delete_source_check.set_sensitive(is_local_file)

                    if not is_local_file or flathub_app_id:
                        dialog.delete_source_check.set_active(False)

        dialog.status_label.set_label(status)

        dialog.add_flatpak_button.set_visible(mode_id or add_enabled)
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
                batch_buffer = dialog.batch_text_view.get_buffer()
                batch_start, batch_end = batch_buffer.get_bounds()
                batch_text = batch_buffer.get_text(batch_start, batch_end, True)
                batch_ids, batch_issues = parse_flatpak_app_inputs(batch_text)

                if dialog.batch_expander.get_expanded():
                    if batch_issues or not batch_ids:
                        return

                    dialog.close()
                    self.start_flatpak_app_id_batch_install(
                        batch_ids,
                        user_install,
                    )
                    return

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

                deb_paths, flatpakref_paths, appimage_paths, unsupported = self._classify_install_paths(paths)
                if unsupported:
                    dialog.status_label.set_label(
                        "Only .deb, .flatpakref, or .AppImage files can be installed here."
                    )
                    return

                dialog.close()
                self._confirm_appimage_install(
                    deb_paths,
                    flatpakref_paths,
                    appimage_paths,
                    user_install,
                    delete_source,
                )

                return

            value = dialog.file_entry.get_text().strip()

            if not value:
                return

            flathub_app_id = extract_flathub_app_id(value)
            is_url = value.startswith("http://") or value.startswith("https://")

            if not is_url and Path(value).suffix.lower() in {
                ".deb",
                ".flatpakref",
                ".appimage",
            }:
                deb_paths, flatpakref_paths, appimage_paths, unsupported = (
                    self._classify_install_paths([value])
                )
                if unsupported:
                    return
                dialog.close()
                self._confirm_appimage_install(
                    deb_paths,
                    flatpakref_paths,
                    appimage_paths,
                    user_install,
                    dialog.delete_source_check.get_active(),
                )
                return

            delete_source = (
                hasattr(dialog, "delete_source_check")
                and dialog.delete_source_check.get_active()
                and bool(value)
                and not is_url
            )

            dialog.close()

            self.start_flatpak_install(
                "id" if flathub_app_id else "ref",
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

        run_async(
            lambda: self.add_flathub_remote_worker(dialog, user_install),
            on_error=lambda error: self._worker_failed(
                "Flathub remote setup", error
            ),
            name="add-flathub",
        )

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
            file_dialog.set_title("Select installation files")

            installer_filter = Gtk.FileFilter()
            installer_filter.set_name(
                "Installable files (*.deb, *.flatpakref, *.AppImage)"
            )
            installer_filter.add_pattern("*.deb")
            installer_filter.add_pattern("*.flatpakref")
            installer_filter.add_pattern("*.AppImage")
            installer_filter.add_pattern("*.appimage")

            all_filter = Gtk.FileFilter()
            all_filter.set_name("All files")
            all_filter.add_pattern("*")

            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(installer_filter)
            filters.append(all_filter)

            file_dialog.set_filters(filters)

            if hasattr(file_dialog, "open_multiple"):
                def callback(fd, result):
                    try:
                        files_model = fd.open_multiple_finish(result)
                    except GLib.Error as error:
                        # U6: cancelling the file chooser is not an error.
                        if error.domain == "gtk-dialog-error-quark" and error.code in (
                            0,
                            1,
                        ):
                            return
                        return
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
                        self._set_installer_dialog_paths(dialog, paths)
                        dialog.mode_ref_button.set_active(True)

                dialog._file_dialog_callback = callback
                file_dialog.open_multiple(self, None, callback)
                return

        # Fallback for older GTK file chooser
        chooser = Gtk.FileChooserDialog(
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.OPEN,
        )

        chooser.set_property("title", "Select installation files")
        chooser.set_select_multiple(True)

        chooser.add_button("Cancel", Gtk.ResponseType.CANCEL)
        chooser.add_button("Open", Gtk.ResponseType.OK)

        installer_filter = Gtk.FileFilter()
        installer_filter.set_name(
            "Installable files (*.deb, *.flatpakref, *.AppImage)"
        )
        installer_filter.add_pattern("*.deb")
        installer_filter.add_pattern("*.flatpakref")
        installer_filter.add_pattern("*.AppImage")
        installer_filter.add_pattern("*.appimage")

        chooser.add_filter(installer_filter)

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
                self._set_installer_dialog_paths(dialog, paths)
                dialog.mode_ref_button.set_active(True)

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

        self.install_progress_window = TaskProgressDialog(
            self, "Installing Flatpak"
        )
        self.install_progress_window.present(self)
        self.install_progress_window.set_status(f"Installing {label}…")

        run_async(
            lambda: self.flatpak_install_worker(
                source_type, value, user_install, delete_source
            ),
            on_error=lambda error: self._worker_failed(
                "Flatpak installation", error
            ),
            name="flatpak-install",
        )

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
                    (
                        message
                        if "installation file" in message.lower()
                        else "Flatpak installation completed successfully."
                    ),
                    Gtk.MessageType.INFO,
                )
            else:
                self.install_progress_window.finish_failure(message)

        return False

    def start_flatpak_app_id_batch_install(self, app_ids, user_install):
        self.install_progress_window = TaskProgressDialog(
            self,
            "Installing Flatpak apps",
        )
        self.install_progress_window.present(self)
        self.install_progress_window.set_status(
            f"Installing {len(app_ids)} Flatpak app(s)…"
        )

        run_async(
            lambda: self.flatpak_app_id_batch_install_worker(
                app_ids, user_install
            ),
            on_error=lambda error: self._worker_failed(
                "Flatpak installation", error
            ),
            name="flatpak-batch",
        )

    def flatpak_app_id_batch_install_worker(self, app_ids, user_install):
        def output_callback(line):
            if self.install_progress_window:
                GLib.idle_add(
                    self.install_progress_window.append_output,
                    line,
                )

        success, message, success_count = install_flatpak_app_id_batch(
            app_ids,
            user_install,
            output_callback=output_callback,
        )
        GLib.idle_add(
            self.on_flatpak_app_id_batch_install_finished,
            success,
            message,
            success_count,
        )

    def on_flatpak_app_id_batch_install_finished(self, success, message, _success_count):
        if self.install_progress_window:
            self.install_progress_window.stop_pulse()
            self.reload()

            if success:
                self.install_progress_window.close_window()
                self.install_progress_window = None
                self.show_message(
                    "Installation finished",
                    message,
                    Gtk.MessageType.INFO,
                )
            else:
                self.install_progress_window.finish_batch_results(message)

        return False

    # ------------------------------------------------------------
    # Flatpak .flatpakref batch installation
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # Actions for context menu
    # ------------------------------------------------------------

    def _setup_css(self):
        css = """
        .sidebar-group-label {
            font-size: 0.75em;
            font-weight: bold;
            text-transform: uppercase;
            opacity: 0.6;
            margin-bottom: 4px;
            margin-top: 8px;
        }
        .manager-badge {
            font-size: 0.85em;
            font-weight: bold;
            border-radius: 6px;
            padding: 2px 8px;
        }
        /* Subtle tinted backgrounds that work in both light and dark themes */
        .manager-apt { background-color: rgba(224, 27, 36, 0.15); }
        .manager-snap { background-color: rgba(255, 120, 0, 0.15); }
        .manager-flatpak { background-color: rgba(28, 113, 216, 0.15); }
        .manager-appimage { background-color: rgba(38, 162, 105, 0.15); }
        .manager-manual { background-color: rgba(145, 65, 172, 0.15); }
        .manager-leftover { background-color: rgba(119, 118, 123, 0.15); }
        .app-grid-card {
            border-radius: 12px;
            padding: 2px;
        }
        .app-grid-card:hover {
            background-color: alpha(currentColor, 0.04);
        }
        """
        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(css)
        except AttributeError:
            provider.load_from_data(css.encode(), len(css.encode()))

        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _setup_actions(self):
        # Remove previous win group if this method is re-run during development
        try:
            self.insert_action_group("win", None)
        except Exception:
            pass

        self.action_group = Gio.SimpleActionGroup.new()

        # ------------------------------------------------------------
        # Context menu / selection actions
        # ------------------------------------------------------------
        def add_context_action(action_name, method_name, enabled=False):
            action = Gio.SimpleAction.new(action_name, None)
            callback = getattr(self, method_name, None)

            if callback:
                action.connect("activate", callback)
            else:
                action.connect(
                    "activate",
                    lambda a, p, n=method_name: self.show_message(
                        "Not available",
                        f"{n} is not implemented yet.",
                        Gtk.MessageType.WARNING,
                    ),
                )

            action.set_enabled(enabled)
            self.action_group.add_action(action)
            setattr(self, f"action_{action_name.replace('-', '_')}", action)

        add_context_action("details", "on_action_details", False)
        add_context_action("mark-selected", "on_action_mark_selected", False)
        add_context_action("unmark-selected", "on_action_unmark_selected", False)
        add_context_action("remove-marked", "on_action_remove_marked", False)
        add_context_action("mark-all-visible", "on_action_mark_all_visible", True)
        add_context_action("unmark-all-visible", "on_action_unmark_all_visible", True)
        add_context_action("clear-all-marks", "on_action_clear_all_marks", False)

        # ------------------------------------------------------------
        # Header / main-menu actions
        # ------------------------------------------------------------
        def add_window_action(action_name, method_name):
            action = Gio.SimpleAction.new(action_name, None)
            callback = getattr(self, method_name, None)

            if callback:
                # Most window actions accept no arguments.
                action.connect("activate", lambda a, p, cb=callback: cb())
            else:
                action.connect(
                    "activate",
                    lambda a, p, n=method_name: self.show_message(
                        "Not available",
                        f"{n} is not implemented yet.",
                        Gtk.MessageType.WARNING,
                    ),
                )

            action.set_enabled(True)
            self.action_group.add_action(action)
            setattr(self, f"action_{action_name.replace('-', '_')}", action)

        add_window_action("install", "on_install_clicked")

        add_window_action("cleanup-residuals", "on_cleanup_residuals_clicked")
        add_window_action("check-updates", "on_check_updates_clicked")
        add_window_action("check-appector-updates", "on_check_appector_updates")
        add_window_action("export-app-list", "on_export_app_list_clicked")
        add_window_action("export-app-list-json", "on_export_app_list_json_clicked")

        add_window_action("show-log", "on_show_log_clicked")
        add_window_action("refresh", "on_reload_clicked")

        add_window_action("mark-all", "mark_all_visible")
        add_window_action("clear-marks", "clear_all_marks")

        add_window_action("toggle-search", "_focus_search")
        add_window_action("shortcuts", "show_shortcuts_window")
        add_window_action("about", "show_about_dialog")

        # Scope shortcuts need a distinct action per scope, because an
        # accelerator is bound to an action rather than to a parameter.
        for scope, (action_name, _accel) in _SCOPE_ACTIONS.items():
            action = Gio.SimpleAction.new(action_name, None)
            action.connect(
                "activate",
                lambda _a, _p, selected=scope: self.set_scope(selected),
            )
            action.set_enabled(True)
            self.action_group.add_action(action)
            setattr(self, f"action_{action_name.replace('-', '_')}", action)

        # ------------------------------------------------------------
        # Insert action group
        # ------------------------------------------------------------
        self.insert_action_group("win", self.action_group)

        # ------------------------------------------------------------
        # Keyboard shortcuts
        # ------------------------------------------------------------
        app = self.get_application()

        if app:
            try:
                for action_name, accelerators in SHORTCUT_ACCELERATORS:
                    app.set_accels_for_action(action_name, accelerators)
                for _scope, (action_name, accelerator) in _SCOPE_ACTIONS.items():
                    # The action lives in the "win" group, but accelerators
                    # are registered against the fully qualified name.
                    app.set_accels_for_action(f"win.{action_name}", [accelerator])
            except Exception:
                pass



    def on_sidebar_toggle_btn_toggled(self, button):
        if hasattr(self, "sidebar_revealer"):
            self.sidebar_revealer.set_reveal_child(button.get_active())

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


    def show_about_dialog(self):
        if hasattr(Adw, "AboutWindow"):
            about = Adw.AboutWindow(
                transient_for=self,
                application_name="Appector",
                version=__version__,
                developer_name="Elshad Guliyev",
                copyright="© 2026 Elshad Guliyev",
                application_icon="com.appector.appector",
                website="https://github.com/ell-shad/appector",
            )
            about.set_license_type(Gtk.License.GPL_3_0)
            about.present()
        else:
            about = Gtk.AboutDialog(
                transient_for=self,
                modal=True,
            )
            about.set_program_name("Appector")
            about.set_version(__version__)
            about.set_comments("Unified installed app inventory")
            about.set_website("https://github.com/ell-shad/appector")
            about.set_logo_icon_name("com.appector.appector")
            about.set_license_type(Gtk.License.GPL_3_0)
            about.present()

    def on_check_appector_updates(self):
        self.action_check_appector_updates.set_enabled(False)
        self.show_toast("Checking for Appector updates…")
        run_async(
            self._check_appector_updates_worker,
            on_error=lambda error: self._worker_failed(
                "Appector update check", error
            ),
            name="appector-update-check",
        )

    def _check_appector_updates_worker(self):
        try:
            result = check_for_update(__version__)
            GLib.idle_add(self._show_appector_update_result, result, None)
        except (RuntimeError, ValueError, OSError) as error:
            GLib.idle_add(self._show_appector_update_result, None, str(error))

    def _show_appector_update_result(self, result, error):
        self.action_check_appector_updates.set_enabled(True)
        if error:
            self.show_message(
                "Could not check for updates",
                error,
                Gtk.MessageType.ERROR,
            )
        elif result["available"]:
            version = result["version"]
            dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.INFO,
                text=f"Appector {version} is available",
                secondary_text=(
                    f"Open the release page to download "
                    f"appector_{version}_all.deb and SHA256SUMS.\n\n"
                    "Install the upgrade with:\n"
                    f"    sudo apt install ./appector_{version}_all.deb\n\n"
                    "Use apt rather than Ubuntu Software Center: a release .deb "
                    "is not registered as an APT source, so Software Center "
                    "reports the installed Appector as \"already installed\" "
                    "instead of offering the upgrade.\n\n"
                    "Verify the download first:\n"
                    "    sha256sum --check SHA256SUMS"
                ),
            )
            dialog.add_button("Close", Gtk.ResponseType.CLOSE)
            dialog.add_button("Open Release", Gtk.ResponseType.OK)
            dialog.connect("response", self._on_appector_update_dialog_response, result["url"])
            dialog.present()
        else:
            self.show_toast("Appector is up to date.")
        return GLib.SOURCE_REMOVE

    def _on_appector_update_dialog_response(self, dialog, response, release_url):
        dialog.close()
        if response != Gtk.ResponseType.OK:
            return

        try:
            Gio.AppInfo.launch_default_for_uri(release_url, None)
        except GLib.Error as error:
            self.show_message(
                "Could not open release page",
                str(error),
                Gtk.MessageType.ERROR,
            )


    def _focus_search(self):
        if hasattr(self, "sidebar_toggle_btn") and not self.sidebar_toggle_btn.get_active():
            self.sidebar_toggle_btn.set_active(True)

        if hasattr(self, "sidebar_revealer"):
            self.sidebar_revealer.set_reveal_child(True)

        GLib.idle_add(self._grab_search_focus_idle)

    def _grab_search_focus_idle(self):
        if hasattr(self, "search_entry"):
            self.search_entry.grab_focus()
        return False

    def show_shortcuts_window(self):
        """Build the shortcuts window from the shared shortcut table.

        Generating this from SHORTCUT_GROUPS keeps the help in step with the
        accelerators actually registered with the application.
        """
        accels = dict(SHORTCUT_ACCELERATORS)
        for _scope, (action_name, accelerator) in _SCOPE_ACTIONS.items():
            accels[f"win.{action_name}"] = [accelerator]

        def escape(text):
            return (
                text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )

        groups = []
        for group_title, entries in SHORTCUT_GROUPS:
            children = []
            for label, action_name in entries:
                accelerator = " ".join(accels.get(action_name, []))
                children.append(
                    "      <child>\n"
                    '        <object class="GtkShortcutsShortcut">\n'
                    f'          <property name="title">{escape(label)}</property>\n'
                    f'          <property name="accelerator">'
                    f"{escape(accelerator)}</property>\n"
                    "        </object>\n"
                    "      </child>\n"
                )
            groups.append(
                "    <child>\n"
                '      <object class="GtkShortcutsGroup">\n'
                f'        <property name="title">{escape(group_title)}</property>\n'
                + "".join(children)
                + "      </object>\n"
                "    </child>\n"
            )

        ui = (
            "<interface>\n"
            '  <object class="GtkShortcutsWindow" id="shortcuts">\n'
            '    <property name="modal">1</property>\n'
            "    <child>\n"
            '      <object class="GtkShortcutsSection">\n'
            '        <property name="section-name">shortcuts</property>\n'
            + "".join(groups)
            + "      </object>\n"
            "    </child>\n"
            "  </object>\n"
            "</interface>\n"
        )

        builder = Gtk.Builder.new_from_string(ui, -1)
        win = builder.get_object("shortcuts")

        if win:
            win.set_transient_for(self)
            win.present()

    def _build_sidebar(self):
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.set_size_request(248, -1)

        # Scrolled window for top groups
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content_box.set_margin_top(12)
        content_box.set_margin_bottom(12)
        content_box.set_margin_start(12)
        content_box.set_margin_end(12)

        # 1. Search
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_hexpand(True)
        self.search_entry.set_placeholder_text("Search apps")
        _set_accessible_label(self.search_entry, "Search installed apps")
        self.search_entry.connect("search-changed", self.on_search_changed)
        content_box.append(self.search_entry)

        # 2. FILTER
        filter_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        filter_label = Gtk.Label(label="FILTER")
        filter_label.add_css_class("sidebar-group-label")
        filter_label.set_xalign(0.0)
        filter_box.append(filter_label)

        self.marked_only_toggle = Gtk.ToggleButton(label="Marked only")
        self.marked_only_toggle.set_tooltip_text(
            "Show only apps marked for removal"
        )
        self.marked_only_toggle.connect("toggled", self.on_marked_only_toggled)
        filter_box.append(self.marked_only_toggle)

        self.duplicates_only_toggle = Gtk.ToggleButton(label="Duplicates only")
        self.duplicates_only_toggle.set_tooltip_text(
            "Show only apps installed more than once"
        )
        self.duplicates_only_toggle.connect("toggled", self.on_duplicates_only_toggled)
        filter_box.append(self.duplicates_only_toggle)

        # One scope selector replaces the previous pair of independent
        # "Show system items" / "System items only" toggles, which could be
        # active at the same time and produced a confusing empty list.
        scope_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scope_label = Gtk.Label(label="SCOPE")
        scope_label.add_css_class("sidebar-group-label")
        scope_label.set_xalign(0.0)

        self.scope_buttons = {}
        scope_group = None
        for scope in ("all", "apps", "system"):
            button = Gtk.ToggleButton(label=scope.capitalize())
            button.set_hexpand(True)
            button.set_focusable(False)
            button.connect("toggled", self.on_scope_toggled, scope)
            if scope_group is None:
                scope_group = button
            else:
                button.set_group(scope_group)
            self.scope_buttons[scope] = button

        self.scope_buttons["all"].set_active(True)
        self.scope_buttons["all"].set_tooltip_text(
            "Show installed applications and system items"
        )
        self.scope_buttons["apps"].set_tooltip_text(
            "Show applications only; hide runtimes, components, and "
            "residual configurations"
        )
        self.scope_buttons["system"].set_tooltip_text(
            "Show system items only: runtimes, components, and residual "
            "configurations"
        )

        scope_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        scope_row.set_homogeneous(True)
        for scope in ("all", "apps", "system"):
            scope_row.append(self.scope_buttons[scope])
        scope_row.add_css_class("linked")

        # U9: per-manager filter chips; counts refresh with every scan.
        manager_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        manager_label = Gtk.Label(label="PACKAGE SOURCE")
        manager_label.add_css_class("sidebar-group-label")
        manager_label.set_xalign(0.0)
        manager_box.append(manager_label)

        self.manager_filter_buttons = {}
        self.active_manager_filters = set()
        for manager in ("APT", "Snap", "Flatpak", "AppImage", "Manual", "Leftover"):
            chip = Gtk.ToggleButton(label=f"{manager} (0)")
            chip.set_focusable(False)
            chip.set_tooltip_text(f"Show only {manager} items")
            chip.connect("toggled", self.on_manager_filter_toggled, manager)
            self.manager_filter_buttons[manager] = chip
            manager_box.append(chip)

        content_box.append(filter_box)
        content_box.append(manager_box)
        scope_holder.append(scope_label)
        scope_holder.append(scope_row)
        content_box.append(scope_holder)

        # 4. SELECTION
        selection_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        selection_label = Gtk.Label(label="SELECTION")
        selection_label.add_css_class("sidebar-group-label")
        selection_label.set_xalign(0.0)
        selection_box.append(selection_label)

        selection_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        selection_btns.set_homogeneous(True)

        self.mark_all_button = Gtk.Button(
            label="Mark all",
            icon_name="list-add-symbolic",
        )
        self.mark_all_button.set_action_name("win.mark-all")
        self.mark_all_button.set_tooltip_text(
            "Mark every currently visible app that is allowed to be removed"
        )
        selection_btns.append(self.mark_all_button)

        self.clear_all_marks_button = Gtk.Button(
            label="Clear",
            icon_name="edit-clear-all-symbolic",
        )
        self.clear_all_marks_button.set_action_name("win.clear-marks")
        self.clear_all_marks_button.set_tooltip_text("Remove every mark")
        selection_btns.append(self.clear_all_marks_button)

        selection_box.append(selection_btns)
        content_box.append(selection_box)

        scrolled.set_child(content_box)
        sidebar_box.append(scrolled)

        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        revealer.set_reveal_child(True)
        revealer.set_child(sidebar_box)

        return revealer

    # ------------------------------------------------------------
    # APT residual configuration cleanup
    # ------------------------------------------------------------

    def on_leftover_cleanup_clicked(self, button=None):
        self.progress_window = TaskProgressDialog(
            self,
            "Checking leftover APT configurations",
        )
        self.progress_window.present(self)
        self.progress_window.start_indeterminate(
            "Checking for removed packages with remaining configuration files…"
        )
        run_async(
            self.leftover_cleanup_preview_worker,
            on_error=lambda error: self._worker_failed(
                "leftover configuration check", error
            ),
            name="leftover-preview",
        )

    def leftover_cleanup_preview_worker(self):
        try:
            leftovers = scan_leftover_configs()
        except subprocess.CalledProcessError as error:
            message = error.stderr.strip() or str(error)
            success, leftovers = False, []
        except OSError as error:
            success, leftovers, message = False, [], str(error)
        except Exception as error:
            success, leftovers, message = False, [], str(error)
        else:
            success, message = True, ""

        GLib.idle_add(
            self.on_leftover_cleanup_preview_finished,
            success,
            leftovers,
            message,
        )

    def on_leftover_cleanup_preview_finished(self, success, leftovers, error):
        if self.progress_window:
            self.progress_window.close_window()
            self.progress_window = None

        if not success:
            self.show_message(
                "Leftover configuration check failed",
                error or "Could not check residual APT configurations.",
                Gtk.MessageType.ERROR,
            )
            return False

        if not leftovers:
            self.show_message(
                "No leftover APT configurations",
                "No removed APT packages with remaining configuration files were found.",
                Gtk.MessageType.INFO,
            )
            return False

        package_ids = [app.package_id for app in leftovers if app.package_id]
        size_estimate = get_removal_size_estimate(leftovers)
        try:
            conffile_details = get_residual_conffile_details(package_ids[:100])
        except Exception:
            conffile_details = {}
        total_conffiles = sum(
            len(conffile_details.get(app.package_id) or [])
            for app in leftovers
        )
        critical = [
            app.package_id
            for app in leftovers
            if is_critical_apt_package_name(app.package_id)
        ]
        try:
            existing_backups = len(list_purge_backups())
        except Exception:
            existing_backups = 0

        lines = [
            "These packages are already removed. Only their configuration "
            "files remain. Purging does not uninstall any installed package.",
            "",
            "This is different from orphaned packages (installed APT "
            "dependencies) and unused Flatpak runtimes.",
            "",
            f"Residual configurations detected: {len(package_ids)} package(s), "
            f"{total_conffiles} dpkg-registered conffile(s)",
            f"Existing purge backups: {existing_backups}",
            "",
            size_estimate,
            "",
        ]

        if critical:
            lines.extend([
                "System packages among residuals — purging deletes their saved "
                "customizations (a backup is still made):",
                "",
                *[f"• {name}" for name in critical[:10]],
                "",
            ])

        lines.extend([
            "Configurations available to purge:",
            "",
            *[f"• {package_id}" for package_id in package_ids[:30]],
        ])
        if len(package_ids) > 30:
            lines.append(f"• …and {len(package_ids) - 30} more")

        # With more than one candidate, let the user narrow the selection first so
        # a single purge never removes residuals the user did not intend.
        if len(leftovers) > 1:
            dialog, holder = self.show_leftover_purge_select(
                leftovers, conffile_details
            )

            def on_selection(apps, dialog=dialog):
                if not apps:
                    self.show_toast("Purge cancelled")
                    return
                self._confirm_leftover_purge(apps, conffile_details)

            holder["callback"] = on_selection
            dialog.present()
            return False

        self._confirm_leftover_purge(leftovers, conffile_details)
        return False

    def _confirm_leftover_purge(self, leftovers, conffile_details=None):
        """U1/U2: residual-purge confirmation on the plan dialog.

        The APT simulation and the size estimate run in a worker behind a
        progress dialog (they used to block the GTK thread); the plan
        dialog then shows the full package list, the conffile count, and
        the simulation output — with Purge disabled until everything
        loaded, and not offered at all when the simulation is blocked.
        """
        conffile_details = conffile_details or {}
        package_ids = [
            app.package_id for app in leftovers if getattr(app, "package_id", "")
        ]
        if not package_ids:
            return

        def work():
            try:
                sim_success, sim_output, sim_purged = simulate_leftover_purge(
                    package_ids
                )
            except Exception as error:
                sim_success, sim_output, sim_purged = False, str(error), []
            try:
                estimate = get_removal_size_estimate(leftovers)
            except Exception as error:
                estimate = f"Estimated space to be freed: unavailable ({error})"
            return sim_success, sim_output, sim_purged, estimate

        progress = TaskProgressDialog(
            self, "Simulating the purge"
        )
        progress.present(self)
        progress.set_status(
            "Simulating the APT purge (no changes are made)…"
        )

        def on_ready(result):
            progress.close()
            sim_success, sim_output, _sim_purged, estimate = result
            self._show_leftover_purge_plan(
                leftovers, package_ids, conffile_details,
                sim_success, sim_output, estimate,
            )

        run_async(
            work,
            on_done=on_ready,
            on_error=lambda error: self._worker_failed(
                "purge simulation", error
            ),
            name="leftover-simulation",
        )

    def _show_leftover_purge_plan(
        self, leftovers, package_ids, conffile_details,
        sim_success, sim_output, estimate,
    ):
        """Present the residual-purge plan (U1)."""
        total_conffiles = sum(
            len(conffile_details.get(package_id) or [])
            for package_id in package_ids
        )
        critical = [p for p in package_ids if is_critical_apt_package_name(p)]

        try:
            existing_backups = len(list_purge_backups())
        except Exception:
            existing_backups = 0

        warnings = []
        if critical:
            warnings.append(
                "System packages selected — their saved customizations will "
                "be deleted (a backup is still made): "
                + ", ".join(critical[:15])
            )

        notes = [
            "These packages are already removed; only their configuration "
            "files remain.",
            "Appector backs up dpkg-registered conffiles (as root) before "
            "purging, and aborts if any file cannot be backed up safely. "
            "Only the newest 10 backups are kept. This is a recovery copy, "
            "not an automatic restore.",
            "Maintainer purge scripts may remove files beyond the conffiles "
            "listed; review the simulation output.",
            f"Existing purge backups: {existing_backups}",
        ]

        files_to_delete = []
        for package_id in package_ids:
            files_to_delete.extend(conffile_details.get(package_id) or [])

        # The simulation is the authoritative preview — show its output as
        # part of the plan (selectable, scrollable, never a hidden surprise).
        simulation_text = (sim_output or "").strip()
        if simulation_text:
            shown = simulation_text[:1200]
            if len(simulation_text) > 1200:
                shown += "\n…(truncated; the full output is in the activity log)"
            notes.append("APT purge simulation (no changes made):\n" + shown)

        dialog = ConfirmPlanDialog(
            self,
            f"Purge {len(package_ids)} leftover APT configuration(s)?",
            heading=(
                f"{len(package_ids)} package(s), {total_conffiles} "
                "dpkg-registered conffile(s). The simulation below shows "
                "exactly what apt would do; no changes have been made."
            ),
            selected=package_ids,
            files_to_delete=files_to_delete or None,
            warnings=warnings or None,
            notes=notes,
            confirm_label="Purge",
            destructive=True,
            on_confirm=lambda: self.start_purge_leftovers(leftovers),
        )
        dialog.set_disk_estimate(estimate)

        if not sim_success:
            # Purging is blocked: the dialog explains why without offering
            # a Purge button, and the reason is recorded.
            dialog.set_blocked(
                "The APT simulation failed; purging is blocked."
            )
            _log_action(
                "LEFTOVER_PURGE_SIMULATION_BLOCKED "
                f"output={simulation_text[:400]}"
            )

        dialog.present(self)

    def show_leftover_purge_select(self, leftovers, conffile_details=None):
        """Show a selection dialog for residual packages before purging.

        GTK dialogs are event-driven, so this returns the dialog plus a holder
        dict. The holder's "apps" key is updated to the still-checked
        `AppEntry` objects when the dialog closes (empty list if cancelled),
        and its "callback" is invoked with that list. Nothing is purged here;
        the caller runs its own confirmation and purge.
        """
        entries = [
            app for app in leftovers if getattr(app, "package_id", "")
        ]
        if len(entries) <= 1:
            return entries

        conffile_details = conffile_details or {}

        dialog = Gtk.Dialog(
            transient_for=self,
            modal=True,
            title="Select residual configurations to purge",
            default_width=580,
            default_height=520,
        )
        dialog.set_resizable(True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Continue", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        hint = Gtk.Label(
            label=(
                "Uncheck anything you want to keep. Only checked packages will "
                "be backed up and purged. You will see a final preview next."
            )
        )
        hint.set_xalign(0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        content.append(hint)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        list_box.set_margin_top(8)
        checks = []

        for app in entries:
            files = conffile_details.get(app.package_id) or []
            suffix = f" — {len(files)} conffile(s)" if files else ""
            critical = is_critical_apt_package_name(app.package_id)

            row = Gtk.CheckButton()
            row.set_active(True)
            label = Gtk.Label(
                label=f"{app.package_id}{suffix}"
                + ("  (system package)" if critical else "")
            )
            label.set_xalign(0)
            label.set_wrap(True)
            label.set_selectable(True)
            row.set_child(label)
            list_box.append(row)
            checks.append((row, app))

        scrolled.set_child(list_box)
        content.append(scrolled)

        def on_response(current_dialog, response, holder):
            current_dialog.close()
            holder["apps"] = (
                [app for row, app in checks if row.get_active()]
                if response == Gtk.ResponseType.OK
                else []
            )
            if holder.get("callback"):
                holder["callback"](holder["apps"])

        holder = {"apps": entries, "callback": None}
        dialog.connect("response", on_response, holder)
        return dialog, holder

    def on_autoremove_clicked(self, button=None):
        if not shutil.which("apt-get"):
            self.show_message(
                "APT not available",
                "The apt-get command was not found on this system.",
                Gtk.MessageType.WARNING,
            )
            return

        # Show progress while checking preview
        self.progress_window = TaskProgressDialog(
            self, "Checking orphaned packages"
        )
        self.progress_window.present(self)
        self.progress_window.start_indeterminate("Checking for orphaned packages…")

        run_async(
            self.autoremove_preview_worker,
            on_error=lambda error: self._worker_failed(
                "autoremove check", error
            ),
            name="autoremove-preview",
        )

    def autoremove_preview_worker(self):
        success, packages, space_freed, raw_output, plan = get_apt_autoremove_preview()

        GLib.idle_add(
            self.on_autoremove_preview_finished,
            success,
            packages,
            space_freed,
            raw_output,
            plan,
        )

    def on_autoremove_preview_finished(self, success, packages, space_freed, raw_output, plan=None):
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

        title = f"Remove {len(packages)} orphaned packages?"

        def do_autoremove():
            self.install_progress_window = TaskProgressDialog(
                self, "Removing orphaned packages"
            )
            self.install_progress_window.present(self)
            self.install_progress_window.set_status("Removing orphaned packages…")
            run_async(
                lambda: self.autoremove_worker(plan),
                on_error=lambda error: self._worker_failed(
                    "autoremove", error
                ),
                name="autoremove",
            )

        self._show_confirm_dialog(title, message, "Remove", do_autoremove)
        return False

    def autoremove_worker(self, plan=None):
        def output_callback(line):
            if self.install_progress_window:
                GLib.idle_add(
                    self.install_progress_window.append_output,
                    line,
                )

        success, message = execute_apt_autoremove(
            output_callback=output_callback,
            plan=plan,
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
        self.progress_window = TaskProgressDialog(
            self, "Checking Flatpak runtimes"
        )
        self.progress_window.present(self)
        self.progress_window.start_indeterminate(
            "Checking for unused Flatpak runtimes…"
        )

        run_async(
            self.flatpak_cleanup_preview_worker,
            on_error=lambda error: self._worker_failed(
                "Flatpak runtime check", error
            ),
            name="flatpak-cleanup-preview",
        )

    def flatpak_cleanup_preview_worker(self):
        success, preview_text, raw_output = get_flatpak_unused_preview()

        GLib.idle_add(
            self.on_flatpak_cleanup_preview_finished,
            success,
            preview_text,
            raw_output,
        )

    def on_flatpak_cleanup_preview_finished(self, success, preview_text, raw_output):
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

        if not preview_text:
            self.show_message(
                "No unused runtimes",
                "No unused Flatpak runtimes were found.",
                Gtk.MessageType.INFO,
            )
            return False

        message = "Flatpak identified these unused runtimes for removal:\n\n" + preview_text

        title = "Remove unused Flatpak runtimes?"

        def do_cleanup():
            self.install_progress_window = TaskProgressDialog(
                self, "Cleaning Flatpak runtimes"
            )
            self.install_progress_window.present(self)
            self.install_progress_window.set_status(
                "Removing unused Flatpak runtimes…"
            )
            run_async(
                self.flatpak_cleanup_worker,
                on_error=lambda error: self._worker_failed(
                    "Flatpak cleanup", error
                ),
                name="flatpak-cleanup",
            )

        self._show_confirm_dialog(title, message, "Clean Up", do_cleanup)
        return False

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

    def on_cleanup_residuals_clicked(self, button=None):
        dialog = Gtk.Dialog(
            transient_for=self,
            modal=True,
            title="Cleanup & residuals",
            default_width=600,
        )
        dialog.set_resizable(True)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.connect(
            "response",
            lambda current_dialog, _response: current_dialog.close(),
        )

        content = dialog.get_content_area()
        content.set_spacing(12)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)

        warning = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        warning.add_css_class("card")
        warning.set_margin_bottom(4)
        warning_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        warning_icon.set_valign(Gtk.Align.START)
        warning_text = Gtk.Label(
            label=(
                "These cleanup tools are experimental and may remove packages, "
                "runtimes, or configuration files. Apart from limited copies of "
                "dpkg-registered APT conffiles, Appector does not yet provide a "
                "complete backup or one-click restore system. Purge backups keep "
                "only the newest 10 copies and cover conffiles only. Review every "
                "preview carefully; proceed only if you understand the changes. "
                "You are responsible for confirming each cleanup."
            )
        )
        warning_text.set_wrap(True)
        warning_text.set_xalign(0)
        warning.append(warning_icon)
        warning.append(warning_text)
        content.append(warning)

        try:
            backup_count = len(list_purge_backups())
        except Exception:
            backup_count = 0
        try:
            total_files = sum(
                summary.get("backed_up", 0)
                for summary in get_purge_backup_summaries()
            )
        except Exception:
            total_files = 0

        backup_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        backup_row.set_margin_bottom(4)
        backup_label = Gtk.Label()
        backup_label.set_xalign(0)
        backup_label.add_css_class("dim-label")
        backup_label.set_hexpand(True)
        backup_label.set_wrap(True)

        def backup_summary_text(count, files):
            noun = "purge backup" if count == 1 else "purge backups"
            return (
                f"{count} {noun} kept "
                f"({files} configuration file{'s' if files != 1 else ''} "
                f"copied in total) — newest 10 retained."
            )

        backup_label.set_label(backup_summary_text(backup_count, total_files))
        prune_button = Gtk.Button(label="Prune old backups")
        prune_button.set_tooltip_text(
            "Keep only the newest 10 purge backups under "
            "~/.local/state/app-manager/purge-backups. Only backups beyond "
            "that limit are deleted."
        )
        prune_button.set_sensitive(backup_count > 10)
        browse_button = Gtk.Button(label="Browse…")
        browse_button.set_tooltip_text(
            "Inspect saved pre-purge backups: which packages each purge "
            "covered, and the file paths and SHA-256 hashes recorded for it. "
            "A backup with no files means those packages registered no "
            "dpkg configuration files. Appector does not restore files."
        )

        def on_prune_clicked(_button):
            try:
                removed, kept = prune_old_purge_backups()
                try:
                    files = sum(
                        summary.get("backed_up", 0)
                        for summary in get_purge_backup_summaries()
                    )
                except Exception:
                    files = 0
                backup_label.set_label(backup_summary_text(kept, files))
                prune_button.set_sensitive(kept > 10)
                self.show_toast(
                    f"Pruned {removed} old backup(s); {kept} retained."
                    if removed
                    else (
                        "Nothing to prune: only the newest "
                        f"{RESIDUAL_PURGE_BACKUP_KEEP} backups are kept, and "
                        f"you have {kept}."
                    )
                )
            except Exception as error:
                self.show_message(
                    "Prune failed", str(error), Gtk.MessageType.ERROR
                )

        prune_button.connect("clicked", on_prune_clicked)
        browse_button.connect(
            "clicked",
            lambda _button: self.on_show_purge_backups_clicked(dialog),
        )
        backup_row.append(backup_label)
        backup_row.append(browse_button)
        backup_row.append(prune_button)
        content.append(backup_row)

        actions = (
            (
                "Remove unused APT dependencies…",
                "Preview auto-removable packages and estimated disk space.",
                self.on_autoremove_clicked,
            ),
            (
                "Purge leftover APT configurations…",
                "Simulate the purge, review conffiles and backup, then purge.",
                self.on_leftover_cleanup_clicked,
            ),
            (
                "Remove unused Flatpak runtimes…",
                "Review runtimes Flatpak reports as no longer needed.",
                self.on_flatpak_cleanup_clicked,
            ),
        )
        for title, description, callback in actions:
            action_button = Gtk.Button()
            action_button.add_css_class("flat")
            action_button.set_hexpand(True)
            row = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=4,
            )
            row.set_margin_top(8)
            row.set_margin_bottom(8)
            row.set_margin_start(8)
            row.set_margin_end(8)

            heading = Gtk.Label(label=title)
            heading.set_xalign(0)
            heading.add_css_class("heading")
            detail = Gtk.Label(label=description)
            detail.set_xalign(0)
            detail.set_wrap(True)
            detail.add_css_class("dim-label")
            row.append(heading)
            row.append(detail)

            action_button.set_child(row)

            def choose_action(_button, action_callback=callback):
                dialog.close()
                action_callback()

            action_button.connect("clicked", choose_action)
            content.append(action_button)

        dialog.present()
        return False

    def on_show_purge_backups_clicked(self, parent=None):
        """Browse retained pre-purge backups (read-only inspection)."""
        dialog = Gtk.Dialog(
            transient_for=parent or self,
            modal=True,
            title="Pre-purge backups",
            default_width=760,
            default_height=560,
        )
        dialog.set_resizable(True)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.connect("response", lambda d, _r: d.close())

        content = dialog.get_content_area()
        content.set_spacing(10)
        content.set_margin_top(14)
        content.set_margin_bottom(14)
        content.set_margin_start(14)
        content.set_margin_end(14)

        note = Gtk.Label(
            label=(
                "Read-only view of backups Appector made before purging residual "
                "APT configuration. Appector does not restore files for you. To "
                "restore one manually, check the manifest record, confirm the "
                "target path is correct, and copy the saved file back with the "
                "recorded ownership and mode."
            )
        )
        note.set_wrap(True)
        note.set_xalign(0)
        note.add_css_class("dim-label")
        content.append(note)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        results_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10
        )
        scrolled.set_child(results_box)

        try:
            summaries = get_purge_backup_summaries()
        except Exception as error:
            summaries = []
            results_box.append(
                Gtk.Label(label=f"Could not read backups: {error}")
            )

        if not summaries:
            empty = Gtk.Label(
                label=(
                    "No pre-purge backups have been created yet.\n\n"
                    "A backup is written each time Appector purges residual "
                    "APT configuration. Purges that have nothing to copy still "
                    "record a manifest, which is why a backup can list no "
                    "files."
                )
            )
            empty.set_xalign(0)
            empty.set_wrap(True)
            empty.add_css_class("dim-label")
            results_box.append(empty)
        else:
            for summary in summaries:
                results_box.append(
                    self._build_purge_backup_expander(summary)
                )

        content.append(scrolled)
        dialog.present()
        return False

    def _build_purge_backup_expander(self, summary):
        status = summary.get("status", "unknown")
        packages = summary.get("packages") or []
        backed_up = summary.get("backed_up", 0)
        if backed_up:
            file_phrase = (
                f"{backed_up} configuration file"
                f"{'s' if backed_up != 1 else ''} copied"
            )
        else:
            file_phrase = "no config files registered"
        heading_text = (
            f"{summary.get('name', '')} — {status}, "
            f"{len(packages)} package"
            f"{'s' if len(packages) != 1 else ''}, {file_phrase}"
        )
        expander = Gtk.Expander(label=heading_text)
        expander.add_css_class("card")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        created = Gtk.Label(label=f"Created: {summary.get('created_at') or 'unknown'}")
        created.set_xalign(0)
        created.add_css_class("caption")
        created.add_css_class("dim-label")
        box.append(created)

        if summary.get("error"):
            error_label = Gtk.Label(
                label=f"Backup error: {summary['error']}"
            )
            error_label.set_xalign(0)
            error_label.set_wrap(True)
            error_label.add_css_class("error")
            box.append(error_label)

        if summary.get("note"):
            note_label = Gtk.Label(label=summary["note"])
            note_label.set_xalign(0)
            note_label.set_wrap(True)
            note_label.add_css_class("dim-label")
            box.append(note_label)

        file_list = summary.get("files") or []
        if not file_list:
            # An empty manifest is usually good news, not a broken backup:
            # dpkg registered no conffiles for these packages, so there was
            # nothing to copy. Say so, instead of implying a failure.
            explanation = Gtk.Label(
                label=(
                    "No configuration files to back up — dpkg registered no "
                    "conffiles for "
                    + (
                        "this package."
                        if len(summary.get("packages") or []) <= 1
                        else "these packages."
                    )
                    + " Nothing was deleted that Appector had not copied."
                )
            )
            explanation.set_xalign(0)
            explanation.set_wrap(True)
            explanation.add_css_class("dim-label")
            box.append(explanation)
        else:
            for record in file_list[:200]:
                if record.get("status") == "missing":
                    suffix = " (missing at backup time)"
                elif record.get("type") == "symlink":
                    suffix = " (symlink)"
                else:
                    size = record.get("size")
                    suffix = f" ({size} bytes)" if isinstance(size, int) else ""
                line = Gtk.Label(
                    label=f"{record.get('path', '')}{suffix}"
                    f"  — {record.get('package', '')}"
                )
                line.set_xalign(0)
                line.set_wrap(True)
                line.set_selectable(True)
                line.add_css_class("caption")
                box.append(line)
            if len(file_list) > 200:
                box.append(
                    Gtk.Label(
                        label=f"…and {len(file_list) - 200} more recorded files."
                    )
                )

        expander.set_child(box)
        return expander

    def on_view_mode_toggled(self, button, mode):
        if not button.get_active():
            return
        self.view_mode = mode
        if hasattr(self, "view_stack"):
            self.view_stack.set_visible_child_name(mode)
        self.save_ui_state()

    def on_setup_grid_item(self, factory, list_item):
        card = Gtk.Frame()
        card.add_css_class("app-grid-card")

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
        )
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        check = Gtk.CheckButton()
        check.set_valign(Gtk.Align.START)
        icon = Gtk.Image()
        icon.set_pixel_size(40)
        icon.set_valign(Gtk.Align.START)
        name = Gtk.Label()
        name.set_xalign(0)
        name.set_yalign(0)
        name.set_wrap(True)
        name.set_max_width_chars(28)
        name.set_hexpand(True)
        name.add_css_class("heading")
        header.append(check)
        header.append(icon)
        header.append(name)

        package_id = Gtk.Label()
        package_id.set_xalign(0)
        package_id.set_wrap(True)
        package_id.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        package_id.set_max_width_chars(40)
        package_id.set_selectable(True)
        package_id.add_css_class("caption")
        package_id.add_css_class("dim-label")
        origin = Gtk.Label()
        origin.set_xalign(0)
        origin.set_ellipsize(Pango.EllipsizeMode.END)
        origin.set_max_width_chars(40)
        origin.add_css_class("dim-label")
        extra = Gtk.Label()
        extra.set_xalign(0)
        extra.set_ellipsize(Pango.EllipsizeMode.END)
        extra.set_max_width_chars(40)
        extra.add_css_class("caption")
        extra.add_css_class("dim-label")

        content.append(header)
        content.append(package_id)
        content.append(origin)
        content.append(extra)
        card.set_child(content)
        list_item.set_child(card)

    def on_bind_grid_item(self, factory, list_item):
        item = list_item.get_item()
        card = list_item.get_child()
        if item is None or card is None:
            return

        content = card.get_child()
        header = content.get_first_child()
        check = header.get_first_child()
        icon = check.get_next_sibling()
        name = icon.get_next_sibling()
        package_id = header.get_next_sibling()
        origin = package_id.get_next_sibling()
        extra = origin.get_next_sibling()

        handler_id = getattr(check, "_app_manager_handler_id", None)
        if handler_id is not None:
            try:
                check.handler_disconnect(handler_id)
            except (TypeError, RuntimeError):
                pass
        check.set_active(bool(item.marked))
        check.set_sensitive(not is_blocked(item))
        check.set_tooltip_text(
            "This package is protected from removal."
            if is_blocked(item)
            else f"Mark {item.name} for removal"
        )
        check._app_manager_handler_id = check.connect(
            "toggled",
            self.on_check_toggled,
            item,
        )

        name.set_label(item.name or item.package_id)
        name.set_tooltip_text(item.name or item.package_id)
        package_id.set_label(item.package_id)
        package_id.set_tooltip_text(item.package_id)
        origin.set_label(f"{item.manager} · {item.source}")
        origin.set_tooltip_text(f"{item.manager} · {item.source}")
        if extra is not None:
            bits = []
            if getattr(item, "version", ""):
                bits.append(str(item.version))
            if getattr(item, "is_duplicate", False):
                bits.append("Duplicate")
            extra.set_label(" · ".join(bits))
            extra.set_visible(bool(bits))
        self.set_image_from_icon(icon, item.icon)

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
    def create_manager_column(self):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.on_setup_manager_cell)
        factory.connect("bind", self.on_bind_manager_cell)

        column = Gtk.ColumnViewColumn.new("Type", factory)
        expression = Gtk.PropertyExpression.new(AppItem, None, "manager")
        sorter = Gtk.StringSorter.new(expression)

        column.set_sorter(sorter)
        column.set_resizable(True)
        column.set_expand(False)

        try:
            column.set_minimum_width(110)
            column.set_fixed_width(120)
        except AttributeError:
            pass

        return column

    def on_setup_manager_cell(self, factory, list_item):
        label = Gtk.Label()
        label.set_xalign(0.5)
        label.set_halign(Gtk.Align.CENTER)
        label.set_valign(Gtk.Align.CENTER)
        label.set_margin_top(2)
        label.set_margin_bottom(2)
        label.set_margin_start(6)
        label.set_margin_end(6)
        label.add_css_class("manager-badge")
        list_item.set_child(label)

    def on_bind_manager_cell(self, factory, list_item):
        item = list_item.get_item()
        label = list_item.get_child()
        if item is None or label is None:
            return

        manager = getattr(item, "manager", "")
        label.set_label(manager)

        # Reset classes
        for cls in [
            "manager-apt",
            "manager-snap",
            "manager-flatpak",
            "manager-appimage",
            "manager-manual",
            "manager-leftover",
        ]:
            label.remove_css_class(cls)

        # Apply specific tint
        if manager == "APT":
            label.add_css_class("manager-apt")
        elif manager == "Snap":
            label.add_css_class("manager-snap")
        elif manager == "Flatpak":
            label.add_css_class("manager-flatpak")
        elif manager == "AppImage":
            label.add_css_class("manager-appimage")
        elif manager == "Manual":
            label.add_css_class("manager-manual")
        elif manager == "Leftover":
            label.add_css_class("manager-leftover")
    def on_setup_cell(self, factory, list_item):
        label = Gtk.Label()
        label.set_xalign(0.0)
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(Pango.EllipsizeMode.END)

        list_item.set_child(label)

    def on_bind_cell(self, factory, list_item, prop):
        item = list_item.get_item()
        label = list_item.get_child()
        if item is None or label is None:
            return

        value = getattr(item, prop, "")

        if prop == "installed_at":
            if not value:
                value = "—"
                label.add_css_class("dim-label")
            else:
                label.remove_css_class("dim-label")
        elif prop == "version" and not value:
            value = "-"
        elif prop == "marked_label" and not value:
            value = ""

        label.set_label(str(value))

        # Add tooltips for truncated cells
        if prop in ("source", "version"):
            label.set_tooltip_text(str(value) if value and value != "-" and value != "—" else "")

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
        blocked = is_blocked(item)
        check.set_sensitive(not blocked)
        check.set_tooltip_text(
            "This package is protected from removal."
            if blocked
            else f"Mark {getattr(item, 'name', 'app')} for removal"
        )

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

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name_label = Gtk.Label()
        name_label.set_xalign(0.0)
        name_label.set_halign(Gtk.Align.START)
        name_label.set_hexpand(True)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        package_label = Gtk.Label()
        package_label.set_xalign(0.0)
        package_label.set_halign(Gtk.Align.START)
        package_label.set_hexpand(True)
        package_label.set_ellipsize(Pango.EllipsizeMode.END)
        package_label.set_selectable(True)
        package_label.add_css_class("caption")
        package_label.add_css_class("dim-label")
        labels.set_hexpand(True)

        box.append(image)
        labels.append(name_label)
        labels.append(package_label)
        box.append(labels)
        duplicate_label = Gtk.Label(label="Duplicate")
        duplicate_label.add_css_class("caption")
        duplicate_label.add_css_class("dim-label")
        box.append(duplicate_label)

        list_item.set_child(box)

    def on_bind_name_cell(self, factory, list_item):
        item = list_item.get_item()
        box = list_item.get_child()

        if item is None or box is None:
            return

        image = box.get_first_child()

        if image is None:
            return

        labels = image.get_next_sibling()

        if labels is None:
            return

        name_label = labels.get_first_child()
        package_label = name_label.get_next_sibling() if name_label else None
        if name_label is None or package_label is None:
            return

        name = getattr(item, "name", "") or "-"
        package_id = getattr(item, "package_id", "") or ""
        name_label.set_label(name)
        name_label.set_tooltip_text(name)
        package_label.set_label(package_id)
        package_label.set_tooltip_text(package_id)
        package_label.set_visible(bool(package_id))
        duplicate_label = labels.get_next_sibling()
        if duplicate_label:
            duplicate_label.set_visible(bool(getattr(item, "is_duplicate", False)))

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
    def _is_advanced_item(self, item):
        """Leftovers and non-GUI entries are 'system' items for filtering."""
        return (
            getattr(item, "manager", "") == "Leftover"
            or not getattr(item, "is_gui_app", True)
        )

    def filter_func(self, item, user_data=None):
        if not isinstance(item, AppItem):
            return False

        scope = getattr(self, "scope", "all")
        is_advanced = self._is_advanced_item(item)

        if scope == "apps" and is_advanced:
            return False
        if scope == "system" and not is_advanced:
            return False

        if self.marked_only and not getattr(item, "marked", False):
            return False

        if self.duplicates_only and not getattr(item, "is_duplicate", False):
            return False

        # U9: manager filter chips (APT / Snap / Flatpak / AppImage /
        # Manual / Leftover). An empty selection shows every manager.
        active_managers = getattr(self, "active_manager_filters", None)
        if active_managers:
            if str(getattr(item, "manager", "")) not in active_managers:
                return False

        if self.search_text:
            haystack = f"{item.name} {item.source} {item.manager} {item.package_id}".lower()
            if self.search_text.lower() not in haystack:
                return False

        return True

    def _focus_is_editable(self):
        """True when keyboard focus sits in a text entry (U5)."""
        try:
            focus = self.get_focus()
        except Exception:
            return False
        try:
            return isinstance(focus, Gtk.Editable)
        except Exception:
            return False

    def on_search_changed(self, entry):
        """U8: debounce the refilter so typing stays smooth on long lists."""
        self.search_text = entry.get_text().strip()

        pending = getattr(self, "_search_debounce_id", None)
        if pending is not None:
            GLib.source_remove(pending)

        def apply_filter():
            self._search_debounce_id = None
            self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
            return False

        self._search_debounce_id = GLib.timeout_add(250, apply_filter)

    def on_marked_only_toggled(self, button):
        self.marked_only = button.get_active()
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)

    def on_duplicates_only_toggled(self, button):
        self.duplicates_only = button.get_active()
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)

    def on_manager_filter_toggled(self, button, manager):
        """U9: a manager chip filters the list to that package source."""
        if button.get_active():
            self.active_manager_filters.add(manager)
        else:
            self.active_manager_filters.discard(manager)
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.update_empty_state()

    def refresh_manager_chip_counts(self):
        """U9: keep the chip labels in sync with the scanned list."""
        counts = {}
        for app in getattr(self, "current_apps", []):
            manager = str(getattr(app, "manager", "") or "?")
            counts[manager] = counts.get(manager, 0) + 1
        for manager, chip in getattr(
            self, "manager_filter_buttons", {}
        ).items():
            try:
                chip.set_label(f"{manager} ({counts.get(manager, 0)})")
                chip.set_sensitive(counts.get(manager, 0) > 0)
            except Exception:
                continue

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
            details_window.present(self)

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
            details_window.present(self)

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

        run_async(
            self.scan_worker,
            on_error=lambda error: self._worker_failed("scan", error),
            name="scan",
        )

    def reload(self):
        self.on_reload_clicked(self.reload_button)

    def scan_worker(self):
        result = scan_all()
        GLib.idle_add(self.populate_apps, result.apps, result.source_status)

    def populate_apps(self, apps, source_status=None):
        # Keep the last per-source scan status for the UI (F6/U8).
        self.scan_source_status = list(source_status or [])
        present_keys = {app_key(app) for app in apps}
        # Prune staged marks only for sources that scanned successfully:
        # a transient snap list failure must not silently erase the user's
        # Snap marks (F6).
        failed_sources = {
            source.split(" ")[0]
            for source, status, _message in self.scan_source_status
            if status != "ok"
        }
        def _manager_failed(app):
            manager = str(getattr(app, "manager", ""))
            if manager == "Leftover":
                return "APT" in failed_sources
            return manager in failed_sources

        previously_marked = {
            key: app for key, app in (
                (app_key(app), app) for app in getattr(self, "current_apps", [])
            ) if key in self.marked_keys
        }
        self.marked_keys.intersection_update(
            present_keys
            | {
                key for key, app in previously_marked.items()
                if _manager_failed(app)
            }
        )
        self.save_marked_keys()

        self.current_apps = apps
        self.detect_duplicates()
        self.rebuild_list()

        self.window_title.set_subtitle(f"{len(apps)} apps detected")
        self.reload_button.set_sensitive(True)
        self._update_scan_banner()
        self.refresh_manager_chip_counts()
        return False

    def rebuild_list(self):
        """Rebuild the list model without losing selection or scroll (U8).

        `Gio.ListStore.splice` swaps the whole model in one step instead of
        remove_all + append (no flicker, no empty state flash), then the
        previously selected entries are re-selected by identity and the
        scroll position is restored proportionally.
        """
        selected_keys = {app_key(item) for item in self.get_selected_items()}

        scroll_fractions = {}
        for view_name in ("column_scrolled", "grid_scrolled"):
            view = getattr(self, view_name, None)
            if view is None:
                continue
            try:
                adjustment = view.get_vadjustment()
                upper = adjustment.get_upper()
                if upper > 0:
                    scroll_fractions[view_name] = adjustment.get_value() / upper
            except Exception:
                continue

        items = []
        for app in self.current_apps:
            item = AppItem(app)
            key = app_key(app)

            if key in self.marked_keys:
                item.marked = True
                item.marked_label = "✓"

            items.append(item)

        try:
            self.list_store.splice(0, self.list_store.get_n_items(), items)
        except (AttributeError, TypeError):
            # Very old libadwaita stacks: fall back to the clear rebuild.
            self.list_store.remove_all()
            for item in items:
                self.list_store.append(item)

        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)

        # Restore selection by identity: positions changed with the model.
        if selected_keys:
            for position in range(self.sort_model.get_n_items()):
                item = self.sort_model.get_item(position)
                if item is not None and app_key(item) in selected_keys:
                    try:
                        self.selection.select_item(position, False)
                    except Exception:
                        break

        if scroll_fractions:

            def _restore_scroll():
                for view_name, fraction in scroll_fractions.items():
                    view = getattr(self, view_name, None)
                    if view is None:
                        continue
                    try:
                        adjustment = view.get_vadjustment()
                        adjustment.set_value(fraction * adjustment.get_upper())
                    except Exception:
                        continue
                return False

            GLib.idle_add(_restore_scroll)

        self.update_marked_ui()
        self.update_selection_ui()

    def detect_duplicates(self):
        mark_duplicate_apps(self.current_apps)

    def _update_scan_banner(self):
        """F6/U8: surface per-source scan problems instead of a short count."""
        banner = getattr(self, "scan_banner", None)
        if banner is None:
            return
        statuses = getattr(self, "scan_source_status", [])
        failed = [source for source, status, _m in statuses if status == "failed"]
        missing = [source for source, status, _m in statuses
                   if status == "tool-missing"]
        if not failed and not missing:
            banner.set_revealed(False)
            return
        parts = []
        if failed:
            parts.append("failed: " + ", ".join(failed))
        if missing:
            parts.append("not installed: " + ", ".join(missing))
        banner.set_title(
            "Some sources could not be scanned ("
            + "; ".join(parts)
            + ")."
        )
        banner.set_revealed(True)

    def on_scan_banner_details(self, *_args):
        statuses = getattr(self, "scan_source_status", [])
        if not statuses:
            return
        lines = []
        for source, status, message in statuses:
            if status == "ok":
                continue
            label = {
                "failed": "failed",
                "tool-missing": "not installed",
            }.get(status, status)
            detail = f"\n• {source}: {label}"
            if message:
                detail += f"\n  {message}"
            lines.append(detail)
        self.show_message(
            "Scan report",
            "Every source reports how it went:\n" + "\n".join(lines),
            Gtk.MessageType.WARNING,
        )

    # ------------------------------------------------------------
    # Mark persistence
    # ------------------------------------------------------------

    def _state_dir(self):
        # Single definition shared with the activity log and purge backups.
        return state_dir()

    def load_marked_keys(self):
        """Load staged removal marks from the private Appector state file.

        Marks are only a staging selection; removal always requires an
        explicit preview and confirmation, so restoring them is safe. Unknown
        or malformed keys are ignored.
        """
        path = self._state_dir() / "marked.json"
        descriptor = None
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return set()
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                descriptor = None
                data = json.load(source)
        except (OSError, ValueError):
            return set()
        finally:
            if descriptor is not None:
                os.close(descriptor)

        if not isinstance(data, dict):
            return set()

        keys = data.get("marked")
        if not isinstance(keys, list):
            return set()

        return {key for key in keys if isinstance(key, str) and key}

    def save_marked_keys(self):
        """Persist staged removal marks with user-only permissions."""
        path = self._state_dir() / "marked.json"
        descriptor = None
        temporary_path = None
        try:
            self._state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".appector-marked-",
                dir=str(self._state_dir()),
            )
            with os.fdopen(
                descriptor, "w", encoding="utf-8"
            ) as output:
                descriptor = None
                json.dump(
                    {"marked": sorted(self.marked_keys)},
                    output,
                    indent=2,
                )
                output.write("\n")
            # mkstemp already creates the file 0600, and os.replace keeps that
            # mode, so no explicit chmod is needed.
            os.replace(temporary_path, path)
            temporary_path = None
        except (OSError, TypeError, ValueError):
            # Mark persistence is a convenience; never let it break the UI.
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

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

        if hasattr(self, "remove_marked_summary"):
            count = self.removable_marked_count
            noun = "app" if count == 1 else "apps"
            self.remove_marked_summary.set_label(
                f"{count} removable {noun} marked"
            )

        if hasattr(self, "action_remove_marked"):
            self.action_remove_marked.set_enabled(self.removable_marked_count > 0)


        if hasattr(self, "remove_marked_button"):
            self.remove_marked_button.set_tooltip_text(
                f"Preview removal of {self.removable_marked_count} marked app(s)"
            )
            self.remove_marked_button.set_sensitive(
                self.removable_marked_count > 0
            )

        if hasattr(self, "remove_action_revealer"):
            self.remove_action_revealer.set_reveal_child(
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

    def mark_items(self, items):
        changed = False
        skipped_protected = 0

        for item in items:
            if is_blocked(item):
                skipped_protected += 1
                continue

            key = app_key(item)

            if key and key not in self.marked_keys:
                self.marked_keys.add(key)
                changed = True

        if changed:
            self.save_marked_keys()
            self.rebuild_list()
        if skipped_protected:
            self.show_toast(
                f"Skipped {skipped_protected} protected package(s)"
            )

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
        if self._focus_is_editable():
            return
        visible_items = [
            item for item in self.get_visible_items()
            if can_remove(item)
        ]

        changed = False
        marked_count = 0

        for item in visible_items:
            key = app_key(item)

            if key and key not in self.marked_keys:
                self.marked_keys.add(key)
                changed = True
                marked_count += 1

        if changed:
            self.save_marked_keys()
            self.rebuild_list()
            noun = "app" if marked_count == 1 else "apps"
            self.show_toast(
                f"Marked {marked_count} visible removable {noun}"
            )
        else:
            self.show_toast("No unmarked removable apps are visible")

    def clear_all_marks(self):
        if not self.marked_keys:
            self.show_toast("No marked apps to clear")
            return

        self.marked_keys.clear()
        self.save_marked_keys()
        self.rebuild_list()
        self.show_toast("All marks cleared")

    # ------------------------------------------------------------
    # Scope filter
    # ------------------------------------------------------------

    def on_scope_toggled(self, button, scope):
        if not button.get_active():
            return
        self.set_scope(scope)
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.update_empty_state()

    def _sync_legacy_scope_flags(self):
        """Keep the scope-derived visibility booleans consistent.

        `filter_func` decides visibility directly from `self.scope`. These
        flags mirror that decision so other helpers (marked-entry cleanup,
        leftover selection state) stay consistent with what is on screen.
        """
        scope = self.scope
        self.show_advanced_apps = scope in {"all", "system"}
        self.hide_basic_apps = scope == "system"
        self.show_leftovers = scope in {"all", "system"}

    def _entry_hidden_by_advanced_options(self, app):
        """Whether the scope selector hides this entry.

        Mirrors the scope branch of `filter_func` exactly, so marks are only
        cleared for entries that actually disappear from the list.
        """
        is_advanced = self._is_advanced_item(app)
        scope = getattr(self, "scope", "all")

        if scope == "apps":
            return is_advanced
        if scope == "system":
            return not is_advanced
        return False

    def _unmark_hidden_advanced_items(self):
        """Drop marks for entries the scope filter now hides.

        Returns the number of marks that were cleared so callers can tell the
        user why their selection changed.
        """
        hidden_keys = {
            app_key(app)
            for app in self.current_apps
            if self._entry_hidden_by_advanced_options(app)
        }

        removed = len(self.marked_keys & hidden_keys)

        if removed:
            self.marked_keys.difference_update(hidden_keys)
            self.save_marked_keys()

        return removed

    # ------------------------------------------------------------
    # Leftover helpers
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # Leftover purge execution
    # ------------------------------------------------------------

    def start_purge_leftovers(self, leftovers):
        self._pending_leftover_keys = [app_key(app) for app in leftovers]

        package_ids = [
            getattr(app, "package_id", "")
            for app in leftovers
            if getattr(app, "package_id", "")
        ]

        self.install_progress_window = TaskProgressDialog(
            self,
            "Purging leftovers",
        )

        self.install_progress_window.present(self)
        self.install_progress_window.set_status(
            "Purging leftover configuration packages…"
        )

        run_async(
            lambda: self.purge_leftover_worker(package_ids),
            on_error=lambda error: self._worker_failed(
                "leftover purge", error
            ),
            name="purge-leftovers",
        )

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
            self.install_progress_window.stop_pulse()  # compat no-op

            if success:
                self.install_progress_window.finish_batch_results(message)

                for key in getattr(self, "_pending_leftover_keys", []):
                    self.marked_keys.discard(key)

                self.save_marked_keys()
                self._pending_leftover_keys = []

                self.reload()
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
        # U5: accelerators are evaluated in the capture phase, so the
        # keyboard shortcut must not fire while the user is editing text.
        if self._focus_is_editable():
            return
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
            self.progress_window = TaskProgressDialog(
                self, "Preparing removal"
            )
            self.progress_window.present(self)
            self.progress_window.start_indeterminate(
                "Checking APT dependencies…"
            )

            run_async(
                lambda: self.apt_batch_prepare_worker(removable),
                on_error=lambda error: self._worker_failed(
                    "APT dependency check", error
                ),
                name="apt-prepare",
            )
        else:
            self.show_batch_confirm(removable, None)

    def show_batch_confirm(self, removable, apt_preview):
        """U1: the batch confirmation is a scrollable plan dialog.

        The dependency cascade ("also removed") is never truncated, the
        buttons can never fall off-screen, and the disk estimate is computed
        by a worker (U2) instead of blocking the GTK thread.
        """
        marked_entries = self.get_marked_entries()
        skipped = [app for app in marked_entries if not can_remove(app)]

        selected = [f"{app.name} ({getattr(app, 'manager', '')})" for app in removable]
        if len(selected) > 50:
            selected = [*selected[:50], f"…and {len(removable) - 50} more"]

        # The full cascade, never truncated — this is the part that
        # protects the user.
        also_removed = None
        if apt_preview:
            also_removed = apt_preview.get("removed_packages") or None

        warnings = list(apt_preview.get("warnings", [])) if apt_preview else []
        if skipped:
            warnings.append(
                f"{len(skipped)} marked item(s) were skipped by the safety policy."
            )

        # Exact file paths for Manual/AppImage deletions.
        files_to_delete = []
        for app in removable:
            if getattr(app, "manager", "") in {"Manual", "AppImage"}:
                paths = getattr(app, "removal_paths", None) or []
                if isinstance(paths, str):
                    paths = paths.splitlines()
                files_to_delete.extend(str(p) for p in paths)

        risk_rows = []
        for app in removable:
            try:
                risk, _reason = get_removal_risk(app)
            except Exception:
                risk = "normal"
            risk_rows.append((getattr(app, "name", "?"), risk))

        notes = []
        if any(getattr(app, "manager", "") == "APT" for app in removable):
            notes.append(
                "Safety reminder: for APT changes, consider creating a "
                "Timeshift or system snapshot before continuing."
            )

        # Purge choice as a proper child widget (U4), not an attribute
        # bolted onto the dialog object.
        extra_widget = None
        has_apt = any(getattr(app, "manager", "") == "APT" for app in removable)
        purge_holder = {}
        if has_apt:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            purge_check = Gtk.CheckButton(
                label="Purge APT configuration files (backup made as root first)"
            )
            purge_check.set_tooltip_text(
                "The helper backs up dpkg-registered configuration files "
                "before purging; if a file cannot be safely backed up, the "
                "purge does not run. The backup is kept in your private "
                "Appector state."
            )
            purge_note = Gtk.Label(
                label=(
                    "A copy is kept under your Appector state directory "
                    "(purge-backups). If any affected configuration file "
                    "cannot be safely copied, the purge is cancelled. This "
                    "backup does not automatically restore files."
                )
            )
            purge_note.set_xalign(0)
            purge_note.set_wrap(True)
            purge_note.add_css_class("dim-label")
            box.append(purge_check)
            box.append(purge_note)
            extra_widget = box
            purge_holder["check"] = purge_check

        apt_package_ids = (
            apt_preview.get("removed_packages", []) if apt_preview else []
        )

        def on_confirm():
            purge = bool(
                purge_holder.get("check") and purge_holder["check"].get_active()
            )
            self.start_batch_removal(removable, purge, apt_preview)

        dialog = ConfirmPlanDialog(
            self,
            f"Remove {len(removable)} marked apps?",
            heading=(
                "Review the plan below. The list of additional packages is "
                "complete — scroll it, and copy any part of it."
            ),
            selected=selected,
            also_removed=also_removed,
            files_to_delete=files_to_delete or None,
            warnings=warnings or None,
            risk_rows=risk_rows,
            notes=notes or None,
            confirm_label="Remove",
            destructive=True,
            on_confirm=on_confirm,
            extra_widget=extra_widget,
        )
        # U2: the size estimate runs in a worker; the dialog starts with a
        # spinner and a disabled confirm button until the estimate lands.
        dialog.set_disk_estimate_async(
            lambda: get_removal_size_estimate(
                removable, apt_package_ids=apt_package_ids
            )
        )
        dialog.present(self)

    def start_batch_removal(self, removable, purge, apt_preview):
        """Continuation of the confirmed batch plan."""
        self.remove_marked_button.set_sensitive(False)

        self._batch_cancel = CancelToken()
        self.install_progress_window = TaskProgressDialog(
            self,
            "Removing apps",
            cancel_token=self._batch_cancel,
        )
        self.install_progress_window.present(self)
        self.install_progress_window.set_status("Preparing removal…")

        apt_plan = apt_preview.get("plan") if apt_preview else None
        run_async(
            lambda: self.batch_worker(removable, purge, apt_plan),
            on_error=lambda error: self._worker_failed("removal", error),
            name="batch-removal",
        )

    def on_confirm_batch_response(self, dialog, response, removable, apt_plan=None):
        """Legacy dialog response path; delegates to the shared start."""
        dialog.close()

        if response != Gtk.ResponseType.OK:
            return

        purge = False
        if hasattr(dialog, "purge_check"):
            purge = dialog.purge_check.get_active()

        self.start_batch_removal(
            removable, purge, {"plan": apt_plan} if apt_plan else None
        )

    def batch_worker(self, apps, purge=False, apt_plan=None):
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
            apt_plan=apt_plan,
        )

        GLib.idle_add(self.on_batch_finished, results, summary)

    def on_batch_finished(self, results, summary):
        for app, success, _message in results:
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

    def _show_confirm_dialog(self, title, message, confirm_label, on_confirm_callback, *callback_args):
        if hasattr(Adw, "AlertDialog"):
            dialog = Adw.AlertDialog.new(title, message)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("confirm", confirm_label)
            dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def on_response(d, response):
                if response == "confirm":
                    on_confirm_callback(*callback_args)

            dialog.connect("response", on_response)
            dialog.present(self)
        else:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.WARNING,
                text=title,
                secondary_text=message
            )
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            confirm_btn = dialog.add_button(confirm_label, Gtk.ResponseType.OK)
            confirm_btn.add_css_class("destructive-action")
            dialog.set_default_response(Gtk.ResponseType.CANCEL)

            def on_response(d, response):
                if response == Gtk.ResponseType.OK:
                    on_confirm_callback(*callback_args)

            dialog.connect("response", on_response)
            dialog.present()
