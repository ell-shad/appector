"""Install-apps dialog builder (U4 extraction from window.py).

Pure widget construction; all behaviour is injected as callbacks so the
builder stays testable without Appector state.
"""

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk


def build_install_dialog(
    parent,
    on_browse,
    on_response,
    on_mode_changed,
    on_state_changed,
    on_batch_text_changed,
    on_batch_expander_changed,
    add_drop_target,
    set_paths,
    update_state,
    initial_paths=None,
):
    """Build the "Install apps" dialog; the caller presents it."""
    dialog = Gtk.Dialog(
        transient_for=parent,
        modal=True,
    )
    dialog.set_resizable(False)
    dialog.set_property("title", "Install apps")
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
    dialog.mode_id_button = Gtk.CheckButton(label="Flathub app ID or URL")
    dialog.mode_ref_button = Gtk.CheckButton(
        label="Install files (.deb / .flatpakref / .AppImage)"
    )

    dialog.mode_ref_button.set_group(dialog.mode_id_button)
    dialog.mode_ref_button.set_active(True)

    # App ID entry
    dialog.id_entry = Gtk.Entry()
    dialog.id_entry.set_placeholder_text(
        "org.gimp.GIMP or https://flathub.org/en/apps/org.gimp.GIMP"
    )
    dialog.id_entry.set_hexpand(True)

    # Flatpakref entry + browse
    dialog.file_entry = Gtk.Entry()
    dialog.file_entry.set_placeholder_text(
        "/path/to/package.deb, .flatpakref, or .AppImage"
    )
    dialog.file_entry.set_hexpand(True)

    browse_button = Gtk.Button(label="Browse files…")
    browse_button.connect("clicked", on_browse, dialog)

    # Scope
    dialog.user_check = Gtk.CheckButton(label="Install for current user only")
    dialog.user_check.set_active(True)
    dialog.delete_source_check = Gtk.CheckButton(
        label="Move selected source files to Trash after successful installation"
    )
    dialog.delete_source_check.set_tooltip_text(
        "Files are moved to your desktop Trash only after a successful install. "
        "Failed installs leave their source files untouched."
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

    id_label = Gtk.Label(label="Flatpak app ID or Flathub app page URL:")
    id_label.set_xalign(0.0)
    dialog.id_label = id_label

    dialog.id_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    dialog.id_box.append(id_label)
    dialog.id_box.append(dialog.id_entry)

    dialog.batch_expander = Gtk.Expander(label="Install multiple apps")
    batch_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    batch_hint = Gtk.Label(
        label="Paste one Flatpak app ID or Flathub app page URL per line."
    )
    batch_hint.set_xalign(0.0)
    batch_hint.set_wrap(True)
    batch_hint.add_css_class("dim-label")
    batch_box.append(batch_hint)

    dialog.batch_text_view = Gtk.TextView()
    dialog.batch_text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    dialog.batch_text_view.set_monospace(True)
    dialog.batch_text_view.set_left_margin(8)
    dialog.batch_text_view.set_right_margin(8)
    dialog.batch_text_view.set_top_margin(6)
    dialog.batch_text_view.set_bottom_margin(6)

    batch_scroller = Gtk.ScrolledWindow()
    batch_scroller.set_policy(
        Gtk.PolicyType.AUTOMATIC,
        Gtk.PolicyType.AUTOMATIC,
    )
    batch_scroller.set_min_content_height(100)
    batch_scroller.set_max_content_height(150)
    batch_scroller.set_child(dialog.batch_text_view)

    batch_frame = Gtk.Frame()
    batch_frame.set_child(batch_scroller)
    batch_box.append(batch_frame)

    dialog.batch_status_label = Gtk.Label(label="")
    dialog.batch_status_label.set_xalign(0.0)
    dialog.batch_status_label.set_wrap(True)
    dialog.batch_status_label.add_css_class("dim-label")
    batch_box.append(dialog.batch_status_label)

    dialog.batch_expander.set_child(batch_box)
    dialog.id_box.append(dialog.batch_expander)

    ref_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    ref_row.append(dialog.file_entry)
    ref_row.append(browse_button)

    dialog.ref_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    file_hint = Gtk.Label(
        label=(
            "Select or drop .deb, .flatpakref, and/or .AppImage files. "
            "AppImages are copied into your user applications folder."
        )
    )
    file_hint.set_xalign(0.0)
    file_hint.add_css_class("dim-label")
    dialog.ref_box.append(file_hint)
    dialog.ref_box.append(ref_row)
    dialog.ref_box.append(dialog.delete_source_check)

    content.append(mode_box)
    content.append(dialog.id_box)
    content.append(dialog.ref_box)
    content.append(dialog.user_check)
    content.append(dialog.status_label)

    dialog.mode_id_button.connect(
        "toggled",
        on_mode_changed,
        dialog,
    )

    dialog.user_check.connect(
        "toggled",
        on_state_changed,
        dialog,
    )

    dialog.id_entry.connect(
        "changed",
        on_state_changed,
        dialog,
    )

    dialog.batch_text_view.get_buffer().connect(
        "changed",
        on_batch_text_changed,
        dialog,
    )

    dialog.batch_expander.connect(
        "notify::expanded",
        on_batch_expander_changed,
        dialog,
    )

    dialog.file_entry.connect(
        "changed",
        on_state_changed,
        dialog,
    )

    add_drop_target(dialog)

    dialog.connect("response", on_response)

    if initial_paths:
        set_paths(dialog, initial_paths)
        dialog.mode_ref_button.set_active(True)

    update_state(dialog)

