import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio

from .i18n import init_translations
from .window import MainWindow


class AppectorApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.appector.appector",
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )

        self.window = None
        self._pending_open_paths = None

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", self._on_quit)
        self.add_action(quit_action)

        # U11: bind translations before any widget is built.
        init_translations()

    def _on_quit(self, *_args):
        self.quit()

    def hold_files_to_open(self, paths):
        """Remember CLI-provided files until the window can receive them.

        `app.run()` may activate an already-running primary instance instead
        of activating this process, so the files are simply stored; `do_open`
        is only reached when this process becomes the primary one.
        """
        self._pending_open_paths = list(paths or [])

    def _flush_pending_open_paths(self):
        if self._pending_open_paths is None:
            return
        paths, self._pending_open_paths = self._pending_open_paths, None
        if paths and self.window is not None:
            self.window.open_install_files(paths)

    def do_activate(self):
        if not self.window:
            self.window = MainWindow(self)

        self.window.present()
        self._flush_pending_open_paths()

    def do_open(self, files, _n_files, _hint):
        """Route files passed on the command line into the install flow.

        GTK activates the application first, so the files only need to be
        forwarded once the window exists.
        """
        if not self.window:
            self.do_activate()

        paths = []
        for file in files or []:
            path = file.get_path() if file is not None else None
            if path:
                paths.append(path)

        if paths and self.window is not None:
            self.window.open_install_files(paths)
