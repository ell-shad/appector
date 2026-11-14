"""The destructive-confirmation dialog (U1).

One reusable ``ConfirmPlanDialog(Adw.Dialog)`` replaces the text-wall
``Gtk.MessageDialog`` confirmations. Structure:

- a *scrollable* body, so the buttons are always on screen at any window
  size (the old dialog could push Remove/Cancel off-screen);
- sections in the order that protects the user: the "also removed"
  dependency cascade comes right after the selection, is expanded by
  default, is never truncated, and is selectable for copying;
- an exact "files that will be deleted" list for Manual/AppImage items;
- a risk-badge row from the policy module;
- an async "disk freed" row (spinner while a worker computes it, U2) — the
  estimate never blocks the dialog;
- the confirm button starts disabled and stays that way while anything is
  still loading or the plan is blocked; the default focus is Cancel; the
  confirm appearance is chosen by the caller (not always destructive).
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

_RISK_BADGE_CSS = {
    "blocked": ("error", "Blocked"),
    "high": ("error", "High risk"),
    "medium": ("warning", "Medium risk"),
    "low": ("success", "Low risk"),
    "normal": ("", ""),
}


def _section_title(text):
    label = Gtk.Label(label=text)
    label.set_xalign(0.0)
    label.add_css_class("heading")
    label.add_css_class("caption")
    return label


def _selectable_list(items, limit=None):
    """A selectable, copyable, never-truncated list of exact values."""
    if not items:
        empty = Gtk.Label(label="None.")
        empty.set_xalign(0.0)
        empty.add_css_class("dim-label")
        return empty

    shown = items if limit is None else items[:limit]
    text = "\n".join(f"• {item}" for item in shown)
    if limit is not None and len(items) > limit:
        text += f"\n• …and {len(items) - limit} more (scroll for all)"

    label = Gtk.Label(label=text)
    label.set_xalign(0.0)
    label.set_valign(Gtk.Align.START)
    label.set_selectable(True)
    label.set_wrap(False)
    label.set_hexpand(True)
    return label


class ConfirmPlanDialog(Adw.Dialog):
    """Confirmation dialog bound to a removal/cleanup plan.

    Args mirror the review's required sections; every one is optional
    except ``title``. ``on_confirm`` is only ever called after the user
    pressed an enabled confirm button on a fully-loaded, unblocked plan.
    """

    def __init__(
        self,
        parent,
        title,
        heading=None,
        selected=None,
        also_removed=None,
        files_to_delete=None,
        warnings=None,
        risk_rows=None,
        notes=None,
        confirm_label="Confirm",
        destructive=True,
        on_confirm=None,
        extra_widget=None,
        on_presented=None,
    ):
        super().__init__()
        self.set_title(title)
        self._on_confirm = on_confirm
        self._blocked = False
        self._loading = 0
        self._confirmed_emitted = False

        self.set_content_width(720)
        self.set_content_height(560)
        self.set_can_close(True)
        self.set_default_widget(None)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        header = Adw.ToolbarView()
        header_bar = Adw.HeaderBar()
        header_bar.set_show_title_buttons(True)
        header.add_top_bar(header_bar)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_top(12)
        body.set_margin_bottom(8)
        body.set_margin_start(16)
        body.set_margin_end(16)

        if heading:
            head = Gtk.Label(label=heading)
            head.set_xalign(0.0)
            head.set_wrap(True)
            body.append(head)

        # 1. The user's own selection.
        if selected:
            body.append(_section_title("You selected"))
            body.append(_selectable_list(selected))

        # 2. The dependency cascade — the part that protects the user, so
        # it is never truncated and expanded by default.
        if also_removed:
            body.append(_section_title("Additional packages that will also be removed"))
            body.append(_selectable_list(also_removed))

        # 3. Exact files for Manual/AppImage items.
        if files_to_delete:
            body.append(_section_title("Files that will be deleted"))
            body.append(_selectable_list(files_to_delete))

        # 4. Risk badges from the policy module.
        if risk_rows:
            body.append(_section_title("Risk assessment"))
            badge_box = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8
            )
            badge_box.set_halign(Gtk.Align.START)
            shown = 0
            for app_name, risk in risk_rows:
                css_class, caption = _RISK_BADGE_CSS.get(risk, ("", risk))
                if not caption:
                    continue
                badge = Gtk.Label(label=f"{app_name}: {caption}")
                badge.add_css_class("pill")
                if css_class:
                    badge.add_css_class(css_class)
                badge_box.append(badge)
                shown += 1
                if shown >= 12:
                    break
            if shown == 0:
                body.remove(body.get_last_child())
            else:
                body.append(badge_box)

        # 5. Warnings (dependency cascade sizes, etc.).
        if warnings:
            body.append(_section_title("Warnings"))
            body.append(_selectable_list(warnings))

        # 6. Notes (backup/restore, Timeshift reminder, ...).
        if notes:
            for note in notes:
                note_label = Gtk.Label(label=note)
                note_label.set_xalign(0.0)
                note_label.set_wrap(True)
                note_label.add_css_class("dim-label")
                body.append(note_label)

        if extra_widget is not None:
            body.append(extra_widget)

        # 7. Disk space: filled asynchronously (U2), spinner until then.
        self._disk_row = None
        self._disk_spinner = None
        self._disk_label = None
        self._append_disk_row(body)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(body)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        header.set_content(scrolled)
        box.append(header)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        buttons.set_margin_top(4)
        buttons.set_margin_bottom(12)
        buttons.set_margin_start(16)
        buttons.set_margin_end(16)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())

        self._confirm_button = Gtk.Button(label=confirm_label)
        if destructive:
            self._confirm_button.add_css_class("destructive-action")
        else:
            self._confirm_button.add_css_class("suggested-action")
        self._confirm_button.connect("clicked", self._on_confirm_clicked)
        self._confirm_button.set_sensitive(False)

        buttons.append(cancel)
        buttons.append(self._confirm_button)
        box.append(buttons)

        self.set_child(box)
        self._cancel_button = cancel

    # -- loading / blocking state -------------------------------------------

    def _append_disk_row(self, body):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_halign(Gtk.Align.START)
        spinner = Gtk.Spinner()
        spinner.start()
        label = Gtk.Label(label="Estimating space to be freed…")
        label.set_xalign(0.0)
        label.add_css_class("dim-label")
        row.append(spinner)
        row.append(label)
        body.append(row)
        self._disk_row = row
        self._disk_spinner = spinner
        self._disk_label = label
        self.begin_loading()

    def begin_loading(self):
        self._loading += 1
        self._update_sensitivity()

    def end_loading(self):
        self._loading = max(0, self._loading - 1)
        self._update_sensitivity()

    def set_blocked(self, reason):
        """Disable confirmation entirely (plan blocked)."""
        self._blocked = bool(reason)
        if reason:
            self._confirm_button.set_tooltip_text(reason)
        self._update_sensitivity()

    def _update_sensitivity(self):
        ready = self._loading == 0 and not self._blocked
        self._confirm_button.set_sensitive(ready)

    # -- async disk estimate --------------------------------------------------

    def set_disk_estimate(self, text):
        """Fill the disk row from a worker (main thread via idle_add)."""
        if self._disk_spinner is not None:
            self._disk_spinner.stop()
            self._disk_spinner.set_visible(False)
        if self._disk_label is not None:
            self._disk_label.set_label(str(text))
            self._disk_label.remove_css_class("dim-label")
        self.end_loading()
        return False

    def set_disk_estimate_async(self, work):
        """Run ``work() -> str`` in a worker and fill the row when done."""
        from .tasks import run_async

        run_async(
            work,
            on_done=lambda text: self.set_disk_estimate(text),
            on_error=lambda _error: self.set_disk_estimate(
                "Estimated space to be freed: unavailable for these items."
            ),
            name="size-estimate",
        )

    # -- response -------------------------------------------------------------

    def _on_confirm_clicked(self, _button):
        if self._confirmed_emitted or not self._confirm_button.get_sensitive():
            return
        self._confirmed_emitted = True
        handler = self._on_confirm
        self.close()
        if handler is not None:
            handler()

    def present(self, parent, *_args):
        # Default focus on Cancel: pressing Enter must never confirm a
        # destructive plan.
        result = super().present(parent, *_args)
        GLib.idle_add(self._focus_cancel)
        return result

    def _focus_cancel(self):
        try:
            self._cancel_button.grab_focus()
        except Exception:
            pass
        return False
