import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio

from .window import MainWindow


class AppManagerApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.example.AppManager",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )

        self.window = None

    def do_activate(self):
        if not self.window:
            self.window = MainWindow(self)

        self.window.present()


def main():
    app = AppManagerApp()
    app.run(None)