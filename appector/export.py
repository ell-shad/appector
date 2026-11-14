"""Installed-application list export (S10, F14).

Headless-testable exporter shared by the GUI. Cells that spreadsheets would
interpret as formulas (= + - @, or leading TAB/CR) are prefixed with an
apostrophe, the CSV injection guard; exports are written atomically with
user-only permissions and replace symlinks instead of following them.
"""

import csv
import json
import os
import stat
import tempfile
from pathlib import Path

EXPORT_FIELDS = (
    "name",
    "manager",
    "source",
    "package_id",
    "version",
    "installed_at",
    "category",
    "exec_name",
    "details",
    "is_duplicate",
    "is_gui_app",
)

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe_value(value) -> str:
    """Neutralize spreadsheet formula injection in one CSV cell (S10)."""
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _app_row(app) -> dict:
    row = {}
    for field_name in EXPORT_FIELDS:
        raw = getattr(app, field_name, "")
        if isinstance(raw, bool):
            row[field_name] = "yes" if raw else "no"
        else:
            row[field_name] = "" if raw is None else str(raw)
    return row


def write_app_export(path, apps, file_format="csv", only_visible_of=None) -> int:
    """Write the app list to *path* as CSV or JSON. Returns the row count.

    ``apps`` may be pre-filtered by the caller (export selected/visible
    only); this function only serializes what it is given. The file is
    created 0600 via a temporary file in the destination directory and
    ``os.replace``d into place, so a pre-existing symlink at *path* is
    replaced rather than followed.
    """
    if file_format not in {"csv", "json"}:
        raise ValueError(f"Unsupported export format: {file_format}")

    rows = [_app_row(app) for app in (apps or [])]
    destination = Path(path)
    descriptor = None
    temporary_path = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".appector-export-",
            dir=str(destination.parent),
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="" if file_format == "csv" else None,
        ) as output:
            descriptor = None
            if file_format == "json":
                json.dump(rows, output, ensure_ascii=False, indent=2)
                output.write("\n")
            else:
                writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS)
                writer.writeheader()
                for row in rows:
                    writer.writerow(
                        {key: csv_safe_value(value) for key, value in row.items()}
                    )
        os.replace(temporary_path, destination)
        temporary_path = None
        os.chmod(destination, 0o600)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
    return len(rows)


def export_summary(count: int, file_format: str, path) -> str:
    format_name = file_format.upper()
    return (
        f"Exported {count} installed app(s) as {format_name} to:\n{path}"
    )


def is_regular_non_symlink(path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False
