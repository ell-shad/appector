import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk, GLib


class ProgressWindow(Gtk.Window):
    def __init__(self, parent, title="Working…"):
        super().__init__(
            title=title,
            transient_for=parent,
            modal=True,
            default_width=460,
        )

        self.set_resizable(False)
        self.set_deletable(False)
        

        self._pulse_source_id = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)

        self.label = Gtk.Label(label="Please wait…")
        self.label.set_wrap(True)
        self.label.set_xalign(0.0)

        self.progressbar = Gtk.ProgressBar()
        self.progressbar.set_hexpand(True)

        box.append(self.label)
        box.append(self.progressbar)

        self.set_child(box)

    def update(self, current, total, message):
        self.label.set_label(message)

        if total and total > 0:
            fraction = float(current) / float(total)
            if fraction > 1.0:
                fraction = 1.0
            self.progressbar.set_fraction(fraction)
        else:
            self.progressbar.pulse()

        return False

    def start_indeterminate(self, message):
        self.label.set_label(message)

        if self._pulse_source_id is None:
            self._pulse_source_id = GLib.timeout_add(100, self._pulse)

    def _pulse(self):
        self.progressbar.pulse()
        return True

    def stop_indeterminate(self):
        if self._pulse_source_id is not None:
            GLib.source_remove(self._pulse_source_id)
            self._pulse_source_id = None

    def close_window(self):
        self.stop_indeterminate()
        self.close()
class InstallProgressWindow(Gtk.Window):
    def __init__(self, parent, title="Installing"):
        super().__init__(
            title=title,
            transient_for=parent,
            modal=True,
            default_width=620,
            default_height=180,
        )

        self.set_resizable(False)
        self.set_deletable(False)
        self.set_default_size(640, 380)

        self._pulse_source_id = None
        self._max_lines = 5000

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)

        self.status_label = Gtk.Label(label="Preparing…")
        self.status_label.set_xalign(0.0)
        self.status_label.set_wrap(True)

        self.progressbar = Gtk.ProgressBar()

        self._buffer = Gtk.TextBuffer()
        self._textview = Gtk.TextView()
        self._textview.set_buffer(self._buffer)
        self._textview.set_editable(False)
        self._textview.set_monospace(True)
        self._textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(self._textview)
        scrolled.set_min_content_height(240)
        scrolled.set_vexpand(True)

        self.expander = Gtk.Expander(label="Installation details")
        self.expander.set_child(scrolled)

        self.close_button = Gtk.Button(label="Close")
        self.close_button.set_sensitive(False)
        self.close_button.connect("clicked", self._on_close_clicked)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        button_box.set_halign(Gtk.Align.END)
        button_box.append(self.close_button)

        box.append(self.status_label)
        box.append(self.progressbar)
        box.append(self.expander)
        box.append(button_box)

        self.set_child(box)

    def _on_close_clicked(self, button):
        self.close_window()

    def start_pulse(self):
        if self._pulse_source_id is None:
            self._pulse_source_id = GLib.timeout_add(120, self._pulse)

    def _pulse(self):
        self.progressbar.pulse()
        return True

    def stop_pulse(self):
        if self._pulse_source_id is not None:
            GLib.source_remove(self._pulse_source_id)
            self._pulse_source_id = None

    def set_status(self, text):
        self.status_label.set_label(text)

    def append_output(self, line):
        if not line:
            return False

        end_iter = self._buffer.get_end_iter()
        self._buffer.insert(end_iter, line + "\n")

        line_count = self._buffer.get_line_count()

        if line_count > self._max_lines:
            start_iter = self._buffer.get_start_iter()
            cut_iter = self._buffer.get_iter_at_line(line_count - self._max_lines)
            self._buffer.delete(start_iter, cut_iter)

        if self.expander.get_expanded():
            self._textview.scroll_to_iter(
                self._buffer.get_end_iter(),
                0.0,
                False,
                0.0,
                1.0,
            )

        return False

    def finish_failure(self, message=None):
        self.stop_pulse()
        self.progressbar.set_fraction(0.0)
        self.set_status("Installation failed.")

        if message:
            self.append_output("")
            self.append_output(message)

        self.close_button.set_sensitive(True)

    def close_window(self):
        self.stop_pulse()
        self.close()
