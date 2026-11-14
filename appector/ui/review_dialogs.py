"""Structured .deb / .flatpakref review dialog (U10, F13).

Replaces the plain-text dump with a structured, scrollable review: per-
package sections with a metadata grid, security callouts (maintainer
scripts, setuid binaries, /etc/apt files, systemd units) as warning rows,
exact file lists, and the APT simulation output — selectable and
copyable, never silently truncated.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

_METADATA_FIELDS = (
    ("Package", "name"),
    ("Version", "version"),
    ("Architecture", "architecture"),
    ("Maintainer", "maintainer"),
    ("Description", "description"),
    ("Dependencies", "depends"),
    ("Pre-dependencies", "pre_depends"),
    ("Recommends", "recommends"),
    ("File size", "size"),
    ("SHA-256", "sha256"),
)


def _value_label(text):
    label = Gtk.Label(label=str(text or "—"))
    label.set_xalign(0.0)
    label.set_valign(Gtk.Align.START)
    label.set_hexpand(True)
    label.set_selectable(True)
    label.set_wrap(True)
    return label


def _key_label(text):
    label = Gtk.Label(label=text)
    label.set_xalign(0.0)
    label.set_valign(Gtk.Align.START)
    label.add_css_class("dim-label")
    return label


def _warning_row(text):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
    label = Gtk.Label(label=text)
    label.set_xalign(0.0)
    label.set_wrap(True)
    label.set_selectable(True)
    label.set_hexpand(True)
    box.append(icon)
    box.append(label)
    return box


def _list_section(parent_box, title, items, css_class="caption"):
    if not items:
        return
    heading = Gtk.Label(label=title)
    heading.set_xalign(0.0)
    heading.add_css_class("heading")
    parent_box.append(heading)
    shown = items[:40]
    text = "\n".join(f"• {item}" for item in shown)
    if len(items) > 40:
        text += f"\n• …and {len(items) - 40} more"
    label = Gtk.Label(label=text)
    label.set_xalign(0.0)
    label.set_valign(Gtk.Align.START)
    label.set_selectable(True)
    label.set_wrap(False)
    label.set_hexpand(True)
    parent_box.append(label)


class DebReviewDialog(Adw.Dialog):
    """Review dialog for locally selected .deb packages (U10/F13).

    ``on_install()`` runs only after the user presses Install on a fully
    rendered review; the dialog never blocks the main loop.
    """

    def __init__(self, parent, review, on_install=None, delete_source=False):
        super().__init__()
        self.set_title("Review Debian package installation")
        self.set_content_width(760)
        self.set_content_height(620)
        self.set_can_close(True)

        self._on_install = on_install

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_top(12)
        body.set_margin_bottom(8)
        body.set_margin_start(16)
        body.set_margin_end(16)

        intro = Gtk.Label(label=(
            "Review every package below before authorizing the installation. "
            "Locally selected packages are not signature-verified: the SHA-256 "
            "values identify these exact files, they do not establish that the "
            "publisher is trustworthy."
        ))
        intro.set_xalign(0.0)
        intro.set_wrap(True)
        body.append(intro)

        for record in review.package_records:
            body.append(self._package_section(record))

        body.append(self._simulation_section(review.simulation))

        notes = [
            "Installing a Debian package runs its maintainer scripts with "
            "administrator privileges (through the Appector helper).",
            "The APT simulation is a preview; system state may change before "
            "installation, so review the final authorization prompt as well.",
        ]
        if delete_source:
            notes.append(
                "After a successful installation, the original .deb files are "
                "moved to Trash only if they are unchanged since this review."
            )
        for note in notes:
            label = Gtk.Label(label=note)
            label.set_xalign(0.0)
            label.set_wrap(True)
            label.add_css_class("dim-label")
            body.append(label)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(body)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        toolbar.set_content(scrolled)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_vexpand(True)
        outer.append(toolbar)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        buttons.set_margin_top(4)
        buttons.set_margin_bottom(12)
        buttons.set_margin_start(16)
        buttons.set_margin_end(16)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())

        install = Gtk.Button(label="Install packages")
        install.add_css_class("destructive-action")
        install.connect("clicked", self._on_install_clicked)

        buttons.append(cancel)
        buttons.append(install)
        outer.append(buttons)

        self.set_child(outer)
        self._install_button = install
        self._emitted = False

    def _on_install_clicked(self, _button):
        if self._emitted:
            return
        self._emitted = True
        handler = self._on_install
        self.close()
        if handler is not None:
            handler()

    def _package_section(self, record):
        frame = Gtk.Frame()
        frame.set_margin_top(2)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title = Gtk.Label(label=f"{record.get('filename', '?')}")
        title.set_xalign(0.0)
        title.add_css_class("heading")
        box.append(title)

        grid = Gtk.Grid()
        grid.set_column_spacing(14)
        grid.set_row_spacing(6)
        for row, (label_text, key) in enumerate(_METADATA_FIELDS):
            value = record.get(key, "")
            if not value:
                continue
            grid.attach(_key_label(label_text), 0, row, 1, 1)
            grid.attach(_value_label(value), 1, row, 1, 1)
        box.append(grid)

        for warning in record.get("warnings", []):
            box.append(_warning_row(warning))

        _list_section(box, "Maintainer scripts (run as root)",
                      record.get("maintainer_scripts", []))
        _list_section(box, "Files installed under /etc",
                      record.get("etc_files", [])[:40])
        _list_section(box, "Files installed under /etc/apt",
                      record.get("apt_config_files", []))

        frame.set_child(box)
        return frame

    def _simulation_section(self, simulation):
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        heading = Gtk.Label(label="APT transaction simulation (no changes made)")
        heading.set_xalign(0.0)
        heading.add_css_class("heading")
        box.append(heading)

        text = (simulation or "").strip()
        if len(text) > 12000:
            text = text[:12000] + "\n…(output truncated for display)"
        label = Gtk.Label(label=text or "APT reported no transaction details.")
        label.set_xalign(0.0)
        label.set_valign(Gtk.Align.START)
        label.set_selectable(True)
        label.set_wrap(False)
        label.set_hexpand(True)
        box.append(label)

        frame.set_child(box)
        return frame


class FlatpakRefReviewDialog(Adw.Dialog):
    """Review dialog for .flatpakref files (S3/U10)."""

    def __init__(self, parent, reviews, on_install=None):
        super().__init__()
        self.set_title("Review Flatpak reference installation")
        self.set_content_width(680)
        self.set_content_height(560)
        self.set_can_close(True)

        self._on_install = on_install

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_top(12)
        body.set_margin_bottom(8)
        body.set_margin_start(16)
        body.set_margin_end(16)

        intro = Gtk.Label(label=(
            "Installing a .flatpakref file adds its remote, its GPG key and "
            "any runtime repository it declares to your Flatpak configuration. "
            "Appector refuses references that use plain HTTP."
        ))
        intro.set_xalign(0.0)
        intro.set_wrap(True)
        body.append(intro)

        for review_text in reviews:
            frame = Gtk.Frame()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_margin_top(10)
            box.set_margin_bottom(10)
            box.set_margin_start(12)
            box.set_margin_end(12)
            label = Gtk.Label(label=review_text)
            label.set_xalign(0.0)
            label.set_valign(Gtk.Align.START)
            label.set_selectable(True)
            label.set_wrap(True)
            box.append(label)
            frame.set_child(box)
            body.append(frame)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(body)
        scrolled.set_vexpand(True)
        toolbar.set_content(scrolled)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(toolbar)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        buttons.set_margin_top(4)
        buttons.set_margin_bottom(12)
        buttons.set_margin_start(16)
        buttons.set_margin_end(16)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        install = Gtk.Button(label="Install references")
        install.add_css_class("suggested-action")
        install.connect("clicked", self._on_install_clicked)

        buttons.append(cancel)
        buttons.append(install)
        outer.append(buttons)

        self.set_child(outer)
        self._emitted = False

    def _on_install_clicked(self, _button):
        if self._emitted:
            return
        self._emitted = True
        handler = self._on_install
        self.close()
        if handler is not None:
            handler()
