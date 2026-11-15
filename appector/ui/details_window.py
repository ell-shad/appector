"""Redesigned app details window (U9).

Replaces the old grid-in-Gtk.Dialog with an ``Adw.Dialog``:

- a preview card with icon, name, manager pill and version;
- a metadata group (Adw.PreferencesGroup-style rows, selectable values);
- the removal risk as a labelled badge (policy-driven, not a guess);
- for duplicates: exactly which installs the entry collides with;
- the removal preview as its own section, selectable for copying.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from ..actions import get_removal_preview
from ..policy import can_remove_single, get_removal_risk

_RISK_CSS = {
    "blocked": "error",
    "high": "error",
    "medium": "warning",
    "low": "success",
    "normal": "",
}

_RISK_LABEL = {
    "blocked": "Blocked by safety policy",
    "high": "High risk",
    "medium": "Medium risk",
    "low": "Low risk",
    "normal": "Normal",
}


def _row(title, value, selectable=True):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    title_label = Gtk.Label(label=title)
    title_label.set_xalign(0.0)
    title_label.add_css_class("dim-label")
    value_label = Gtk.Label(label=str(value or "—"))
    value_label.set_xalign(1.0)
    value_label.set_hexpand(True)
    value_label.set_wrap(True)
    if selectable:
        value_label.set_selectable(True)
    box.append(title_label)
    box.append(value_label)
    return box


class DetailsWindow(Adw.Dialog):
    """App details for one entry (U9)."""

    def __init__(self, parent, app, on_remove=None):
        super().__init__()
        self.set_title(getattr(app, "name", "App details") or "App details")
        self.set_content_width(600)
        self.set_content_height(620)
        self.set_can_close(True)

        self.app = app
        self._on_remove = on_remove
        self._removal_in_progress = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        # Adw.HeaderBar has no set_show_title_buttons; enable both sides.
        header.set_show_start_title_buttons(True)
        header.set_show_end_title_buttons(True)
        toolbar.add_top_bar(header)

        if can_remove_single(app):
            remove_button = Gtk.Button(label="Remove…")
            remove_button.add_css_class("destructive-action")
            remove_button.connect("clicked", self.on_remove_clicked)
            header.pack_start(remove_button)
            self.remove_button = remove_button
        else:
            self.remove_button = None

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_top(12)
        body.set_margin_bottom(8)
        body.set_margin_start(16)
        body.set_margin_end(16)

        body.append(self._preview_card(app))
        body.append(self._risk_card(app))
        body.append(self._metadata_group(app))
        body.append(self._removal_section(app))

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(body)
        scrolled.set_vexpand(True)
        toolbar.set_content(scrolled)
        self.set_child(toolbar)

    # -- cards ------------------------------------------------------------

    def _preview_card(self, app):
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        card.set_margin_top(4)

        icon = getattr(app, "icon", "")
        image = None
        if icon:
            try:
                if "/" in icon or icon.startswith("."):
                    picture = Gtk.Picture.new_for_filename(icon)
                else:
                    picture = Gtk.Image.new_from_icon_name(icon)
                    picture.set_pixel_size(64)
                image = picture
            except Exception:
                image = None
        if image is None:
            image = Gtk.Image.new_from_icon_name(
                "application-x-executable-symbolic"
            )
            try:
                image.set_pixel_size(48)
            except Exception:
                pass

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        name = Gtk.Label(label=getattr(app, "name", "") or "—")
        name.set_halign(Gtk.Align.START)
        name.add_css_class("title-2")
        name.set_wrap(True)
        title_box.append(name)

        pill_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for text, css in self._pills(app):
            pill = Gtk.Label(label=text)
            pill.add_css_class("pill")
            if css:
                pill.add_css_class(css)
            pill_row.append(pill)
        title_box.append(pill_row)

        card.append(image)
        card.append(title_box)
        return card

    @staticmethod
    def _pills(app):
        pills = []
        manager = str(getattr(app, "manager", "") or "?")
        pills.append((manager, "caption"))
        version = str(getattr(app, "version", "") or "")
        if version:
            pills.append((version, "caption"))
        installed_at = str(getattr(app, "installed_at", "") or "")
        if installed_at:
            pills.append((f"installed {installed_at}", "caption"))
        if getattr(app, "is_duplicate", False):
            pills.append(("duplicate install", "warning"))
        return pills

    def _risk_card(self, app):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        try:
            risk, reason = get_removal_risk(
                app, metadata=getattr(app, "apt_metadata", None)
            )
        except Exception:
            risk, reason = "normal", ""

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        badge = Gtk.Label(label=_RISK_LABEL.get(risk, risk))
        badge.add_css_class("pill")
        css = _RISK_CSS.get(risk, "")
        if css:
            badge.add_css_class(css)
        row.append(badge)
        if reason:
            why = Gtk.Label(label=reason)
            why.set_xalign(0.0)
            why.set_wrap(True)
            why.add_css_class("dim-label")
            row.append(why)
        box.append(row)

        duplicate_with = list(getattr(app, "duplicate_with", []) or [])
        if duplicate_with:
            title = Gtk.Label(label="Also installed as")
            title.set_xalign(0.0)
            title.add_css_class("dim-label")
            box.append(title)
            for entry in duplicate_with:
                label = Gtk.Label(label=f"• {entry}")
                label.set_xalign(0.0)
                label.set_selectable(True)
                box.append(label)
        return box

    @staticmethod
    def _metadata_group(app):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        heading = Gtk.Label(label="Details")
        heading.set_xalign(0.0)
        heading.add_css_class("heading")
        box.append(heading)

        fields = (
            ("Source", getattr(app, "source", "")),
            ("Package ID", getattr(app, "package_id", "")),
            ("Category", getattr(app, "category", "")),
            ("Installed", getattr(app, "installed_at", "") or "Unknown"),
            ("Details", getattr(app, "details", "")),
        )
        for title, value in fields:
            if value:
                box.append(_row(title, value))
        return box

    @staticmethod
    def _removal_section(app):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        heading = Gtk.Label(label="Removal preview")
        heading.set_xalign(0.0)
        heading.add_css_class("heading")
        box.append(heading)

        preview = Gtk.Label(label=get_removal_preview(app))
        preview.set_xalign(0.0)
        preview.set_valign(Gtk.Align.START)
        preview.set_selectable(True)
        preview.set_wrap(True)
        box.append(preview)
        return box

    # -- removal ------------------------------------------------------------

    def on_remove_clicked(self, _button):
        if self._removal_in_progress:
            return
        self._removal_in_progress = True
        try:
            if self._on_remove is not None:
                self._on_remove(self)
        finally:
            self._removal_in_progress = False
