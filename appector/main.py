"""Command-line entry point.

Deliberately free of GTK imports at module level so `appector --version`,
the root-refusal check, and CLI error paths work (and are tested) on
headless systems where libadwaita is not installed. The GUI is imported
lazily only after those checks pass.
"""

import argparse
import os
import sys

from . import __version__


def build_argument_parser():
    parser = argparse.ArgumentParser(
        prog="appector",
        description="Open the Appector Linux application manager.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "files",
        metavar="FILE",
        nargs="*",
        help="install files to review (.deb, .flatpakref, .AppImage)",
    )
    return parser


def main(argv=None):
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)

    if os.geteuid() == 0:
        print(
            "Appector is a desktop application and must not be run as root.",
            file=sys.stderr,
        )
        return 1

    # Imported lazily: this is the first point that genuinely needs GTK.
    from .app import AppectorApp

    app = AppectorApp()
    if arguments.files:
        app.hold_files_to_open(arguments.files)
    return app.run(None)
