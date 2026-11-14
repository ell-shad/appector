"""Cancellable progress dialog (U3), replacing both progress windows.

Design points from the review:

- non-modal-blocking ``Adw.Dialog`` with a ``Cancel`` button that
  terminates the running command's process group through a
  ``CancelToken`` (the runner supports cancellation, S7);
- ``Close`` is always enabled once the task ended or errored, so a failed
  worker can never trap the user behind a dead window;
- output lines are batched and flushed on a ~100 ms timer instead of one
  ``idle_add`` per line;
- the buffer keeps a bounded tail so hours of package output cannot eat
  memory.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from .tasks import CancelToken

_MAX_LINES = 4000
_FLUSH_INTERVAL_MS = 100


class TaskProgressDialog(Adw.Dialog):
    """Progress dialog for one background task.

    Typical use::

        dialog = TaskProgressDialog(parent, "Removing apps",
                                     cancel_token=token)
        dialog.present(parent)
        run_async(work, dialog.finish_success, dialog.finish_failure)
    """

    def __init__(self, parent, title="Working…", cancel_token: CancelToken = None):
        super().__init__()
        self.set_title(title)
        self._cancel_token = cancel_token
        self._pending_lines = []
        self._flush_source = None
        self._finished = False
        self._pulse_source = None

        self.set_content_width(640)
        self.set_content_height(380)
        self.set_can_close(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(12)
        box.set_margin_start(16)
        box.set_margin_end(16)

        self._status_label = Gtk.Label(label="Preparing…")
        self._status_label.set_xalign(0.0)
        self._status_label.set_wrap(True)
        self._status_label.set_hexpand(True)

        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_hexpand(True)
        self._progress_bar.pulse()

        self._buffer = Gtk.TextBuffer()
        text_view = Gtk.TextView()
        text_view.set_buffer(self._buffer)
        text_view.set_editable(False)
        text_view.set_cursor_visible(False)
        text_view.set_monospace(True)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(text_view)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        self._details_expander = Gtk.Expander(label="Details")
        self._details_expander.set_child(scrolled)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)

        self._cancel_button = Gtk.Button(label="Cancel")
        self._cancel_button.add_css_class("destructive-action")
        self._cancel_button.connect("clicked", self._on_cancel_clicked)

        self._close_button = Gtk.Button(label="Close")
        self._close_button.connect("clicked", lambda *_: self.close())
        self._close_button.set_visible(False)

        buttons.append(self._cancel_button)
        buttons.append(self._close_button)

        box.append(self._status_label)
        box.append(self._progress_bar)
        box.append(self._details_expander)
        box.append(buttons)
        self.set_child(box)

        self._pulse_source = GLib.timeout_add(120, self._on_pulse)
        self._flush_source = GLib.timeout_add(_FLUSH_INTERVAL_MS, self._on_flush)

    # -- UI callbacks ------------------------------------------------------

    def _on_pulse(self):
        if self._finished:
            return False
        self._progress_bar.pulse()
        return True

    def _on_flush(self):
        self.flush_output()
        return not self._finished

    def _on_cancel_clicked(self, _button):
        if self._cancel_token is not None:
            self._cancel_token.cancel()
            self.set_status("Cancelling…")
        self._finish_ui(cancelled=True)

    # -- Public API --------------------------------------------------------

    def set_status(self, text):
        self._status_label.set_label(str(text or ""))

    def set_fraction(self, current, total):
        try:
            fraction = float(current) / float(total)
        except (TypeError, ValueError, ZeroDivisionError):
            fraction = 1.0
        self._progress_bar.set_fraction(min(max(fraction, 0.0), 1.0))

    def append_output(self, line):
        """Queue one output line; flushed in batches on the main loop."""
        if not line:
            return False
        self._pending_lines.append(str(line))
        if len(self._pending_lines) > _MAX_LINES:
            self._pending_lines = self._pending_lines[-_MAX_LINES:]
        return False

    def flush_output(self):
        if not self._pending_lines:
            return False
        lines, self._pending_lines = self._pending_lines, []
        end_iter = self._buffer.get_end_iter()
        self._buffer.insert(end_iter, "\n".join(lines) + "\n")

        line_count = self._buffer.get_line_count()
        if line_count > _MAX_LINES:
            start = self._buffer.get_start_iter()
            cut = self._buffer.get_iter_at_line(line_count - _MAX_LINES)
            self._buffer.delete(start, cut)

        # Scroll only while details are open.
        if self._details_expander.get_expanded():
            mark = self._buffer.get_insert()
            text_view = self._details_expander.get_child().get_child()
            text_view.scroll_mark_onscreen(mark)
        return False

    def _finish_ui(self, cancelled=False, success=True):
        if self._finished:
            return
        self._finished = True
        if self._pulse_source is not None:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = None
        self._cancel_button.set_visible(False)
        self._close_button.set_visible(True)
        self._details_expander.set_expanded(True)
        if cancelled:
            self._progress_bar.set_fraction(0.0)
            self.set_status("Cancelled.")
        else:
            self._progress_bar.set_fraction(1.0 if success else 0.0)

    def finish_success(self, message="Done."):
        """Complete the dialog on success (safe to pass to run_async)."""
        self.flush_output()
        self._finish_ui(success=True)
        self.set_status(str(message))
        return False

    def finish_failure(self, error_or_message):
        """Complete the dialog on failure; always leaves Close usable."""
        self.flush_output()
        self._finish_ui(success=False)
        message = (
            str(getattr(error_or_message, "args", error_or_message) or "")
            if not isinstance(error_or_message, str)
            else error_or_message
        )
        if isinstance(error_or_message, BaseException):
            message = f"{type(error_or_message).__name__}: {error_or_message}"
        self.set_status("Failed.")
        self.append_output(message)
        self.flush_output()
        return False

    def finish_results(self, message):
        """Complete with a results summary shown in the details view."""
        self.flush_output()
        self._finish_ui(success=True)
        self.set_status("Finished.")
        self.append_output(message)
        self.flush_output()
        return False

    # -- compatibility shims for the replaced progress windows -------------

    def start_indeterminate(self, message):
        """Compat: the dialog always shows an indeterminate pulse."""
        self.set_status(message)

    def start_pulse(self):
        """Compat: the dialog pulses itself until finished."""

    def stop_pulse(self):
        """Compat: `_finish_ui` already stops the pulse."""

    def close_window(self):
        """Compat: close after flushing pending output."""
        self.flush_output()
        self.close()

    def finish_update_results(self, message, success):
        """Compat for the old updates flow."""
        if success:
            self.finish_results(message)
        else:
            self.finish_failure(message)
