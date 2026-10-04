import argparse
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio

from . import __version__
from .window import MainWindow


class AppectorApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.appector.appector",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )

        self.window = None

    def do_activate(self):
        if not self.window:
            self.window = MainWindow(self)

        self.window.present()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="appector",
        description="Open the Appector Linux application manager.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.parse_args(argv)

    if os.geteuid() == 0:
        print(
            "Appector is a desktop application and must not be run as root.",
            file=sys.stderr,
        )
        return 1

    app = AppectorApp()
    return app.run(None)