import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from email.parser import Parser
from pathlib import Path

from ._proc import (
    _log_action,
    _run_command_raw,
    _run_command_stream,
)
from . import helper_client
from .helper_client import HelperUnavailable
from .paths import appimage_dir, cache_dir, legacy_appimage_dir
from .plans import (
    Plan,
    check_apt_removal_plan,
    check_apt_upgrade_plan,
)
from .policy import (
    APT_MAX_REMOVALS,
    SINGLE_REMOVABLE_MANAGERS,
    _base_package_name,
    _format_package_list,
    _is_safe_package_id,
    _parse_apt_simulation_removed_packages,
    can_remove,
    can_remove_single,
    is_blocked,
    is_critical_apt_package_name,
)
# Flatpak surface moved to managers/flatpak.py (ADR-0002 next step);
# re-exported here so existing callers and tests keep working.
from .managers.flatpak import (  # noqa: F401
    get_flatpak_remotes,
    flathub_remote_exists,
    add_flathub_remote,
    install_flatpak_app_id,
    parse_flatpak_app_inputs,
    install_flatpak_app_id_batch,
    extract_flathub_app_id,
    parse_flatpakref_file,
    flatpakref_https_ok,
    flatpakref_review_text,
    install_flatpak_ref_file,
    install_flatpak_source,
    get_flatpak_unused_preview,
    execute_flatpak_unused_cleanup,
)
from .managers.flatpak import (  # noqa: F401
    _flatpak_install_command,
    _flatpak_is_installed,
    _flatpak_output_is_already_installed,
    _format_flatpak_batch_summary,
    _install_flatpak_location,
)
from .managers.flatpak import (  # noqa: F401
    FLATPAK_PROMPT_RE,
    FLATHUB_REMOTE_NAME,
    FLATPAKREF_REVIEW_FIELDS,
)
from .residual import (
    CONFFILE_LINE_RE,
    _purge_backup_note,
)

def app_key(app) -> str:
    manager = str(getattr(app, "manager", "") or "").strip()
    package_id = str(getattr(app, "package_id", "") or "").strip()

    return f"{manager}::{package_id}"


def get_removal_preview(app) -> str:
    manager = getattr(app, "manager", "")
    package_id = getattr(app, "package_id", "")

    if not package_id:
        return "Unknown"

    if manager in SINGLE_REMOVABLE_MANAGERS and is_blocked(app):
        return "Blocked by safety policy"

    if manager == "Flatpak":
        return f"flatpak uninstall {package_id}"

    if manager == "Snap":
        return f"snap remove {package_id}"

    if manager == "APT":
        return f"sudo apt-get remove {package_id}"

    if manager == "AppImage":
        if getattr(app, "removal_paths", None):
            return (
                "Remove the Appector launcher and managed AppImage copy:\n"
                + "\n".join(_manual_removal_paths(app))
            )
        return f"rm \"{package_id}\""

    if manager == "Manual":
        paths = _manual_removal_paths(app)
        if not paths:
            return "No safe launcher or executable files were detected."
        return "Remove these files only:\n" + "\n".join(paths)

    return "Removal preview not available"


def _format_size(byte_count):
    value = float(byte_count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def _parse_human_size(value):
    match = re.search(
        r"([0-9]+(?:[.,][0-9]+)?)\s*(B|bytes?|KiB|MiB|GiB|TiB|KB|MB|GB|TB)",
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    power = 1000 if unit in {"kb", "mb", "gb", "tb"} else 1024
    exponent = {
        "b": 0, "byte": 0, "bytes": 0,
        "kb": 1, "kib": 1,
        "mb": 2, "mib": 2,
        "gb": 3, "gib": 3,
        "tb": 4, "tib": 4,
    }[unit]
    return int(number * power ** exponent)


def _regular_file_sizes(paths):
    total = 0
    found = False
    for value in paths:
        try:
            path = Path(value).expanduser()
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
                found = True
        except OSError:
            continue
    return total if found else None


def get_removal_size_estimate(
    apps,
    apt_package_ids=None,
    flatpak_refs_by_scope=None,
):
    """Return a qualified space estimate from package metadata or managed files."""
    apps = list(apps or [])
    total_bytes = 0
    measured = False
    apt_ids = list(dict.fromkeys(apt_package_ids or []))
    residual_conf_files = []

    for app in apps:
        manager = getattr(app, "manager", "")
        package_id = str(getattr(app, "package_id", "") or "")
        if manager == "APT":
            apt_ids.append(package_id)
        elif manager == "Leftover":
            if not _is_safe_package_id(package_id):
                continue
            try:
                result = subprocess.run(
                    [
                        "dpkg-query",
                        "-W",
                        "-f=${Conffiles}\n",
                        "--",
                        package_id,
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    env={**os.environ, "LC_ALL": "C"},
                )
                if result.returncode == 0:
                    residual_conf_files.extend(
                        match.group(1)
                        for match in CONFFILE_LINE_RE.finditer(result.stdout)
                    )
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif manager == "Flatpak":
            details = str(getattr(app, "details", "") or "").lower()
            scope = "--user" if "user" in details else "--system"
            try:
                result = subprocess.run(
                    ["flatpak", "info", "--show-size", scope, package_id],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                size = _parse_human_size(result.stdout) if result.returncode == 0 else None
                if size is not None:
                    total_bytes += size
                    measured = True
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif manager == "Snap":
            snap_dir = Path("/var/lib/snapd/snaps")
            snap_sizes = _regular_file_sizes(snap_dir.glob(f"{package_id}_*.snap"))
            if snap_sizes is not None:
                total_bytes += snap_sizes
                measured = True
        elif manager in {"AppImage", "Manual"}:
            raw_paths = getattr(app, "removal_paths", None)
            if isinstance(raw_paths, str):
                paths = raw_paths.splitlines()
            elif raw_paths:
                paths = list(raw_paths)
            else:
                paths = [package_id]
            size = _regular_file_sizes(paths)
            if size is not None:
                total_bytes += size
                measured = True

    apt_ids = list(dict.fromkeys(package_id for package_id in apt_ids if _is_safe_package_id(package_id)))
    if apt_ids:
        command = [
            "dpkg-query",
            "-W",
            "-f=${binary:Package}\t${Installed-Size}\n",
            "--",
            *apt_ids,
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "LC_ALL": "C"},
            )
            if result.returncode == 0 or result.stdout:
                for line in result.stdout.splitlines():
                    _, separator, size_kib = line.partition("\t")
                    if separator and size_kib.isdigit():
                        total_bytes += int(size_kib) * 1024
                        measured = True
        except (OSError, subprocess.TimeoutExpired):
            pass

    flatpak_command = shutil.which("flatpak")
    if flatpak_command:
        for scope, refs in (flatpak_refs_by_scope or {}).items():
            for ref in dict.fromkeys(refs):
                try:
                    result = subprocess.run(
                        [
                            flatpak_command,
                            "info",
                            "--show-size",
                            scope,
                            ref,
                        ],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    size = (
                        _parse_human_size(result.stdout)
                        if result.returncode == 0
                        else None
                    )
                    if size is not None:
                        total_bytes += size
                        measured = True
                except (OSError, subprocess.TimeoutExpired):
                    continue

    config_size = _regular_file_sizes(residual_conf_files)
    if config_size is not None:
        total_bytes += config_size
        measured = True

    if not measured:
        return "Estimated space to be freed: unavailable for these items."

    note = (
        "Approximate estimate; shared dependencies, retained app data/configuration, "
        "compression, and filesystem behavior affect actual space reclaimed."
    )
    return (
        f"Estimated space that may be freed: approximately {_format_size(total_bytes)}.\n"
        f"{note}"
    )


# ------------------------------------------------------------
# Basic command execution helpers
# ------------------------------------------------------------

def _maybe_delete_source_file(path, delete_source, message):
    if not delete_source:
        return message

    source_path = Path(path).expanduser()
    gio_command = shutil.which("gio")
    if not gio_command:
        warning = (
            "Could not move installation file to Trash because the gio command "
            "is unavailable; the source file was kept."
        )
        _log_action(f"INSTALL_SOURCE_TRASH_FAILED path={source_path} reason=gio-unavailable")
        return f"{message}\n\n{warning}" if message else warning

    try:
        result = subprocess.run(
            [gio_command, "trash", str(source_path.absolute())],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        warning = (
            f"Could not move installation file to Trash {source_path}; "
            f"the source file was kept: {error}"
        )
        _log_action(f"INSTALL_SOURCE_TRASH_FAILED path={source_path} error={error}")
        return f"{message}\n\n{warning}" if message else warning

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "gio trash failed."
        warning = (
            f"Could not move installation file to Trash {source_path}; "
            f"the source file was kept: {detail}"
        )
        _log_action(f"INSTALL_SOURCE_TRASH_FAILED path={source_path} error={detail}")
        return f"{message}\n\n{warning}" if message else warning

    _log_action(f"INSTALL_SOURCE_TRASH_SUCCESS path={source_path}")
    notice = "Moved installation file to Trash after successful installation."
    return f"{message}\n\n{notice}" if message else notice


def _desktop_entry_value(value):
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


# AppImage type magic (F9): bytes 8-11 hold the ASCII type marker; the
# older code only checked the ELF magic, so any ELF executable renamed to
# .AppImage was accepted and installed as a launcher.
APPIMAGE_HEADER_SIZE = 512
APPIMAGE_TYPE_MAGIC = {
    b"\x01": "AppImage type 1",
    b"\x02": "AppImage type 2",
}


def _validate_appimage_header(header: bytes):
    """(ok, reason) for the first bytes of an AppImage file."""
    if len(header) < 12 or header[:4] != b"\x7fELF":
        return False, "File is not a valid ELF AppImage"
    marker = header[8:9]
    if marker not in APPIMAGE_TYPE_MAGIC:
        return (
            False,
            "File is an ELF binary but not a type 1 or type 2 AppImage",
        )
    return True, ""


def appimage_missing_fuse_hint() -> str:
    """F9: how to run an AppImage when libfuse is unavailable."""
    return (
        "If the app fails to launch with a FUSE error, either install "
        "libfuse2 or run it with --appimage-extract-and-run."
    )


def install_appimage(path, delete_source=False):
    """Copy an AppImage into the per-user app directory and add a launcher."""
    source = Path(path).expanduser()
    if source.suffix.lower() != ".appimage":
        return False, f"Not an AppImage file: {source}"
    if not source.is_file():
        return False, f"AppImage file not found: {source}"

    try:
        with source.open("rb") as appimage_file:
            header = appimage_file.read(APPIMAGE_HEADER_SIZE)
            _ok, reason = _validate_appimage_header(header)
            if not _ok:
                return False, f"{reason}: {source.name}"
            # F9: the identity is the *content* hash — two different
            # AppImages that share a file name are different installs.
            digest_source = hashlib.sha256()
            appimage_file.seek(0)
            for chunk in iter(lambda: appimage_file.read(1024 * 1024), b""):
                digest_source.update(chunk)
            content_hash = digest_source.hexdigest()
    except OSError as error:
        return False, f"Could not read AppImage {source}: {error}"

    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", source.stem).strip("-") or "appimage"
    app_dir = appimage_dir()
    desktop_dir = Path.home() / ".local/share/applications"
    try:
        resolved_source = source.resolve(strict=True)
        if any(
            resolved_source.is_relative_to(root.resolve())
            for root in (app_dir, legacy_appimage_dir())
            if root.exists()
        ):
            for existing_desktop in desktop_dir.glob("app-manager-appimage-*.desktop"):
                try:
                    existing_content = existing_desktop.read_text(encoding="utf-8")
                except OSError:
                    continue
                if (
                    "X-AppManager-Installed-AppImage=true" in existing_content
                    and f"X-AppManager-AppImage-Path={resolved_source}" in existing_content
                ):
                    return True, f"Already installed: {source.stem}"
    except OSError as error:
        return False, f"Could not resolve AppImage file {source}: {error}"

    if "\n" in str(app_dir) or "\n" in str(desktop_dir):
        return False, "AppImage installation paths cannot contain newline characters."

    digest = content_hash[:10]
    installed_image = app_dir / f"{slug}-{digest}.AppImage"
    desktop_file = desktop_dir / f"app-manager-appimage-{slug}-{digest}.desktop"

    if desktop_file.exists() or installed_image.exists():
        try:
            content = desktop_file.read_text(encoding="utf-8")
        except OSError as error:
            return False, f"An AppImage installation already exists but could not be read: {error}"
        if (
            "X-AppManager-Installed-AppImage=true" in content
            and f"X-AppManager-AppImage-Path={installed_image}" in content
            and installed_image.is_file()
        ):
            return True, f"Already installed: {source.stem}"
        return False, f"An AppImage installation already exists at {installed_image}."

    try:
        app_dir.mkdir(parents=True, exist_ok=True)
        desktop_dir.mkdir(parents=True, exist_ok=True)
        # F9: install atomically — a half-written AppImage must never be
        # left behind (or launched) if the copy fails midway.
        temp_image = app_dir / f".{installed_image.name}.part"
        try:
            copy_digest = hashlib.sha256()
            with source.open("rb") as source_file, temp_image.open("wb") as target:
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    copy_digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if copy_digest.hexdigest() != content_hash:
                # F9: hash while copying, so the installed bytes always
                # match the digest embedded in the file and launcher names
                # (the source could change between the two passes).
                temp_image.unlink(missing_ok=True)
                return False, (
                    f"The AppImage changed while it was being installed: "
                    f"{source.name}. Install it again."
                )
            os.replace(temp_image, installed_image)
        except OSError:
            temp_image.unlink(missing_ok=True)
            raise
        installed_image.chmod(0o755)

        display_name = "".join(
            character
            for character in source.stem.replace("_", " ").replace("-", " ").strip()
            if character >= " " and character != "\x7f"
        )
        display_name = display_name or "AppImage"
        desktop_content = "\n".join(
            [
                "[Desktop Entry]",
                "Type=Application",
                f"Name={display_name}",
                f"Exec={_desktop_entry_value(str(installed_image))}",
                "Icon=application-x-executable",
                "Terminal=false",
                "Categories=Utility;",
                "X-AppManager-Installed-AppImage=true",
                f"X-AppManager-AppImage-Path={installed_image}",
                f"X-AppManager-Source-URI={resolved_source.as_uri()}",
                "",
            ]
        )
        temp_desktop = desktop_dir / f".{desktop_file.name}.part"
        try:
            with temp_desktop.open("w", encoding="utf-8") as launcher_file:
                launcher_file.write(desktop_content)
                launcher_file.flush()
                os.fsync(launcher_file.fileno())
            os.replace(temp_desktop, desktop_file)
        except OSError:
            temp_desktop.unlink(missing_ok=True)
            raise
        desktop_file.chmod(0o644)
    except OSError as error:
        try:
            installed_image.unlink(missing_ok=True)
            desktop_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"Could not install AppImage {source.name}: {error}"

    message = (
        f"Installed {display_name} for the current user.\n"
        f"AppImage copy: {installed_image}\n"
        f"Launcher: {desktop_file}\n"
        "The launcher uses a generic icon and a name derived from the file name."
    )
    _log_action(f"APPIMAGE_INSTALL_SUCCESS source={source} installed={installed_image}")
    if delete_source:
        message = _maybe_delete_source_file(source, True, message)
    return True, message


def install_appimage_batch(paths, output_callback=None, delete_source=False):
    """Install multiple AppImages one at a time and report each result."""
    installed = []
    already_installed = []
    failures = []
    source_file_results = []

    for index, path in enumerate(paths, start=1):
        name = Path(path).name
        if output_callback:
            output_callback(f"[{index}/{len(paths)}] Installing {name}…")
        success, message = install_appimage(path, delete_source=delete_source)
        if success and message.startswith("Already installed:"):
            already_installed.append(name)
            status = "ALREADY INSTALLED"
        elif success:
            installed.append(name)
            status = "INSTALLED"
            if delete_source:
                for line in message.splitlines():
                    if (
                        "Moved installation file to Trash" in line
                        or "Could not move installation file to Trash" in line
                    ):
                        source_file_results.append(f"{name}: {line}")
                        break
        else:
            first_line = message.splitlines()[0] if message else "Installation failed"
            failures.append(f"{name}: {first_line}")
            status = "FAILED"
        if output_callback:
            output_callback(f"[{index}/{len(paths)}] {status} {name}")

    lines = [
        f"Installed ({len(installed)}): " + (", ".join(installed) if installed else "none"),
        f"Already installed ({len(already_installed)}): "
        + (", ".join(already_installed) if already_installed else "none"),
        f"Failed ({len(failures)}): " + ("; ".join(failures) if failures else "none"),
    ]
    summary = "\n".join(lines)
    if source_file_results:
        summary += "\n\nSource file cleanup:\n" + "\n".join(source_file_results[:20])
    return not failures, summary, len(installed) + len(already_installed)


def check_available_updates():
    """Return structured update listings from APT, Flatpak, and Snap."""
    updates = []
    errors = []
    notes = []
    checks = []
    successful_checks = 0

    apt_command = shutil.which("apt")
    apt_get_command = shutil.which("apt-get")
    if apt_command:
        # F12: refresh the package lists first, so the result is not the
        # stale cache (the old flow told the user to run apt update).
        if apt_get_command:
            refresh_rc, refresh_output = _run_command_raw(
                [apt_get_command, "update"],
                timeout=300,
                env_overrides={"LC_ALL": "C"},
                uses_apt=True,
            )
            if refresh_rc != 0:
                # The listing below then reflects the previous cache state;
                # say so instead of silently showing stale data as fresh.
                _log_action(
                    "UPDATE_LIST_REFRESH_FAILED rc="
                    f"{refresh_rc} output={refresh_output[:400]}"
                )
                notes.append(
                    "The package lists could not be refreshed (no root "
                    "rights or a repository is unreachable); the APT "
                    "listing may be from the previous refresh."
                )
            # Needrestart (if installed) reports which services need a
            # restart after the coming upgrades; surfaced as a note.
            try:
                needrestart = shutil.which("needrestart")
                if needrestart:
                    _rc, nr_output = _run_command_raw(
                        [needrestart, "-b"],
                        timeout=90,
                        env_overrides={"LC_ALL": "C", "NEEDRESTART_MODE": "l"},
                    )
                    for line in nr_output.splitlines():
                        if line.startswith("NEEDRESTART-SVC") or line.startswith(
                            "NEEDRESTART-KSTA"
                        ):
                            notes.append(f"needrestart: {line.strip()}")
                            break
            except Exception:
                pass
        returncode, output = _run_command_raw(
            [apt_command, "list", "--upgradable"],
            timeout=90,
            env_overrides={"LC_ALL": "C"},
            uses_apt=True,
        )
        if returncode == 0:
            successful_checks += 1
            before_count = len(updates)
            for line in output.splitlines():
                match = re.match(
                    r"^\s*([^/\s]+)/\S+\s+(\S+)\s+\S+\s+"
                    r"\[upgradable from:\s*([^\]]+)\]",
                    line,
                )
                if match:
                    package_id, new_version, old_version = match.groups()
                    updates.append({
                        "manager": "APT",
                        "scope": None,
                        "id": package_id,
                        "detail": f"{old_version} → {new_version}",
                    })
            count = len(updates) - before_count
            checks.append(("APT", count))
        else:
            errors.append(f"APT: {output or 'Update check failed.'}")
    else:
        notes.append("APT is not available on this system.")

    flatpak_command = shutil.which("flatpak")
    if flatpak_command:
        for scope in ("--user", "--system"):
            try:
                result = subprocess.run(
                    [
                        flatpak_command,
                        "remote-ls",
                        "--updates",
                        scope,
                        "--columns=application,version,branch,origin",
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env={**os.environ, "LC_ALL": "C"},
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(f"Flatpak ({scope.removeprefix('--')}): {error}")
                continue
            if result.returncode:
                output = "\n".join(
                    item.strip()
                    for item in (result.stdout, result.stderr)
                    if item.strip()
                )
                errors.append(
                    f"Flatpak ({scope.removeprefix('--')}): "
                    f"{output or 'Update check failed.'}"
                )
            else:
                successful_checks += 1
                before_count = len(updates)
                for line in result.stdout.splitlines():
                    columns = line.split()
                    if not columns or columns[0].lower() in {
                        "application", "name", "id",
                    }:
                        continue
                    updates.append({
                        "manager": "Flatpak",
                        "scope": scope.removeprefix("--"),
                        "id": columns[0],
                        "detail": " · ".join(columns[1:]),
                    })
                checks.append(
                    (f"Flatpak ({scope.removeprefix('--')})", len(updates) - before_count)
                )
    else:
        notes.append("Flatpak is not available on this system.")

    snap_command = shutil.which("snap")
    if snap_command:
        try:
            result = subprocess.run(
                [snap_command, "refresh", "--list"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "LC_ALL": "C"},
            )
            if result.returncode:
                output = "\n".join(
                    item.strip()
                    for item in (result.stdout, result.stderr)
                    if item.strip()
                )
                errors.append(f"Snap: {output or 'Update check failed.'}")
            else:
                successful_checks += 1
                before_count = len(updates)
                for line in result.stdout.splitlines():
                    columns = line.split()
                    if not columns or columns[0].lower() in {
                        "name", "all", "no",
                    }:
                        continue
                    updates.append({
                        "manager": "Snap",
                        "scope": None,
                        "id": columns[0],
                        "detail": " ".join(columns[1:]),
                    })
                checks.append(("Snap", len(updates) - before_count))
        except (OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"Snap: {error}")
    else:
        notes.append("Snap is not available on this system.")

    notes.extend((
        "APT results use the package lists currently cached on this system.",
        "AppImages and manual installations are not checked; they do not share a "
        "standard update source or command.",
    ))
    # F12: a pending reboot requirement belongs in the update report.
    if Path("/var/run/reboot-required").exists():
        reboot_note = "A system restart is required to complete recent updates."
        try:
            hint = Path("/var/run/reboot-required.pkgs").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            hint = ""
        if hint:
            reboot_note += "\nAffected: " + ", ".join(
                sorted({line.strip() for line in hint.splitlines() if line.strip()})[:15]
            )
        notes.append(reboot_note)
    return successful_checks > 0, {
        "updates": updates,
        "checks": checks,
        "errors": errors,
        "notes": notes,
    }


def prepare_available_updates(updates):
    """Group selected updates per manager and preview the APT transaction (S4).

    Returns (plans, errors). The APT group is simulated now; an upgrade that
    would REMOVE anything is refused before it can run.
    """
    grouped = {}
    for update in updates or []:
        manager = update.get("manager")
        scope = update.get("scope")
        package_id = str(update.get("id", ""))
        if manager == "APT" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+.:_-]*", package_id):
            group = ("APT", None)
        elif manager == "Flatpak" and scope in {"user", "system"} and re.fullmatch(
            r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+",
            package_id,
        ):
            group = ("Flatpak", scope)
        elif manager == "Snap" and re.fullmatch(r"[a-z0-9][a-z0-9-]*", package_id):
            group = ("Snap", None)
        else:
            continue
        grouped.setdefault(group, set()).add(package_id)

    plans = []
    errors = []

    apt_ids = sorted(grouped.get(("APT", None), set()))
    if apt_ids:
        apt_get_command = shutil.which("apt-get")
        if not apt_get_command:
            errors.append("APT: apt-get is not available.")
        else:
            returncode, output = _run_command_raw(
                [
                    apt_get_command,
                    "--simulate",
                    "install",
                    "--only-upgrade",
                    "--",
                    *apt_ids,
                ],
                timeout=180,
                env_overrides={"LC_ALL": "C"},
                uses_apt=True,
            )
            if returncode != 0:
                errors.append(f"APT: the upgrade simulation failed.\n{output}")
            else:
                simulated_removed = _parse_apt_simulation_removed_packages(output)
                upgrade_ok, upgrade_message = check_apt_upgrade_plan(
                    simulated_removed
                )
                if not upgrade_ok:
                    errors.append(f"APT: {upgrade_message}")
                else:
                    plans.append(Plan(
                        action="apt-upgrade",
                        package_ids=apt_ids,
                        removed_preview=[],
                    ))

    for (manager, scope), ids in sorted(grouped.items()):
        if manager == "APT":
            continue
        plans.append(Plan(
            action=f"{manager.lower()}-update",
            package_ids=sorted(ids),
            removed_preview=[],
        ) if manager != "Flatpak" else Plan(
            action="flatpak-update",
            package_ids=sorted(ids),
            removed_preview=[],
        ))
        _ = scope

    return plans, errors


def execute_available_updates(updates, output_callback=None):
    """Install selected updates using each package manager's native updater.

    S4: the APT group is re-simulated immediately before running and must
    contain no removals ("an update must not remove packages").
    """
    grouped = {}
    for update in updates or []:
        manager = update.get("manager")
        scope = update.get("scope")
        package_id = str(update.get("id", ""))
        if manager == "APT" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+.:_-]*", package_id):
            group = ("APT", None)
        elif manager == "Flatpak" and scope in {"user", "system"} and re.fullmatch(
            r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+",
            package_id,
        ):
            group = ("Flatpak", scope)
        elif manager == "Snap" and re.fullmatch(r"[a-z0-9][a-z0-9-]*", package_id):
            group = ("Snap", None)
        else:
            continue
        grouped.setdefault(group, set()).add(package_id)

    if not grouped:
        return False, "No valid updates were selected."

    # S4: preview the APT upgrade transaction and refuse removals.
    _plans, prepare_errors = prepare_available_updates(updates)
    if prepare_errors:
        return False, "\n".join(prepare_errors)

    results = []
    failures = []
    manager_order = {"APT": 0, "Flatpak": 1, "Snap": 2}
    scope_order = {None: 0, "user": 0, "system": 1}
    for (manager, scope), package_ids in sorted(
        grouped.items(),
        key=lambda item: (
            manager_order.get(item[0][0], 99),
            scope_order.get(item[0][1], 99),
        ),
    ):
        package_ids = sorted(package_ids)
        if manager == "APT":
            command = shutil.which("apt-get")
            if not command:
                failures.append("APT: apt-get is not available.")
                continue
            # S4: re-simulate immediately before the privileged run; an
            # update transaction that would remove anything is refused.
            sim_rc, sim_output = _run_command_raw(
                [
                    command,
                    "--simulate",
                    "install",
                    "--only-upgrade",
                    "--",
                    *package_ids,
                ],
                timeout=180,
                env_overrides={"LC_ALL": "C"},
                uses_apt=True,
            )
            if sim_rc != 0:
                failures.append(f"APT: upgrade simulation failed.\n{sim_output}")
                continue
            simulated_removed = _parse_apt_simulation_removed_packages(sim_output)
            upgrade_ok, upgrade_message = check_apt_upgrade_plan(simulated_removed)
            if not upgrade_ok:
                failures.append(f"APT: {upgrade_message}")
                continue
            cmd = [command, "install", "--only-upgrade", "-y", "--", *package_ids]
        elif manager == "Flatpak":
            command = shutil.which("flatpak")
            if not command:
                failures.append("Flatpak: flatpak is not available.")
                continue
            cmd = [
                command,
                "update",
                f"--{scope}",
                "--noninteractive",
                *package_ids,
            ]
        else:
            command = shutil.which("snap")
            if not command:
                failures.append("Snap: snap is not available.")
                continue
            cmd = [command, "refresh", "--", *package_ids]

        title = f"{manager}{f' ({scope})' if scope else ''}"
        if output_callback:
            output_callback(f"Updating {title}: {', '.join(package_ids)}")
        if manager == "APT" or (manager == "Flatpak" and scope == "system"):
            # S5/S6: privileged updates go through the named helper action.
            try:
                if manager == "APT":
                    result = helper_client.apt_upgrade(
                        package_ids,
                        plan=Plan(
                            action="apt-upgrade",
                            package_ids=package_ids,
                            removed_preview=[],
                        ),
                        output_callback=output_callback,
                    )
                else:
                    result = helper_client.flatpak_update_system(
                        package_ids, output_callback=output_callback
                    )
            except (HelperUnavailable, OSError) as error:
                failures.append(f"{title}: {error}")
                continue
            success, output = result.ok, result.message
        else:
            success, output = _run_command_stream(cmd, output_callback)
            output = output if output else ""
        if success:
            results.append(f"{title}: updated {len(package_ids)} item(s).")
        else:
            failures.append(f"{title}: {output or 'Update failed.'}")

    summary = "\n".join(results + failures)
    return not failures, summary or "No selected updates could be run."


# ------------------------------------------------------------
# Batch .deb installation
# ------------------------------------------------------------

class DebInstallReview:
    def __init__(
        self,
        temporary_directory,
        original_paths,
        staged_paths,
        hashes,
        simulation,
        summary,
        package_records=None,
    ):
        self._temporary_directory = temporary_directory
        self.original_paths = original_paths
        self.staged_paths = staged_paths
        self.hashes = hashes
        self.simulation = simulation
        self.summary = summary
        # F13/U10: structured per-package facts (fields, maintainer
        # scripts, setuid files, /etc and /etc/apt paths, systemd units)
        # consumed by the structured review dialog.
        self.package_records = list(package_records or [])
        self._closed = False

    @property
    def all_warnings(self):
        """Flattened security warnings across every staged package."""
        warnings = []
        for record in self.package_records:
            for warning in record.get("warnings", []):
                warnings.append(f"{record.get('name', '?')}: {warning}")
        return warnings

    def close(self):
        if self._closed:
            return
        self._temporary_directory.cleanup()
        self._closed = True

    def ensure_staged(self):
        """Re-create any staged copy that disappeared before installation.

        The review exists to bind the install to exact reviewed bytes. That
        guarantee survives a vanished staging file as long as the *original*
        file still hashes to the reviewed value, so the copy is simply made
        again from those bytes. If the original changed, the review is no
        longer valid and installation is refused rather than silently
        proceeding with different content.
        """
        missing = [
            index
            for index, path in enumerate(self.staged_paths)
            if not Path(path).is_file()
        ]
        if not missing:
            return

        for index in missing:
            original = self.original_paths[index]
            expected = self.hashes[index]
            digest = hashlib.sha256()
            try:
                source_fd = os.open(
                    original, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                with os.fdopen(source_fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise OSError("The selected file is no longer a regular file.")
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as error:
                raise OSError(
                    f"The reviewed copy of {Path(original).name} is gone and the "
                    f"original could not be re-read ({error}). Review the file "
                    "again."
                ) from error

            if digest.hexdigest() != expected:
                raise OSError(
                    f"The reviewed copy of {Path(original).name} is gone and the "
                    "original file has changed since the review. Review the "
                    "file again."
                )

            destination = Path(self.staged_paths[index])
            destination.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            destination_fd = os.open(destination, flags, 0o400)
            try:
                copy_fd = os.open(
                    original, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                with os.fdopen(copy_fd, "rb") as source, os.fdopen(
                    destination_fd, "wb"
                ) as output:
                    destination_fd = None
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        output.write(chunk)
            finally:
                if destination_fd is not None:
                    os.close(destination_fd)

        _log_action(
            f"DEB_REVIEW_RESTAGED count={len(missing)}"
        )

    def trash_unchanged_sources(self):
        results = []
        for path, expected_hash in zip(self.original_paths, self.hashes, strict=True):
            digest = hashlib.sha256()
            try:
                source_fd = os.open(
                    path,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                with os.fdopen(source_fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise OSError("The selected file is no longer a regular file.")
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as error:
                results.append(
                    f"Source file kept because it could not be safely checked: "
                    f"{path} ({error})"
                )
                continue

            if digest.hexdigest() != expected_hash:
                results.append(
                    f"Source file kept because it changed after review: {path}"
                )
                continue

            result = _maybe_delete_source_file(path, True, "")
            if "Moved installation file to Trash" in result:
                results.append(f"Moved to Trash: {Path(path).name}")
            else:
                results.append(f"{Path(path).name}: {result}")
        return "\n".join(results)


def _deb_review_scratch_dir():
    """Private scratch space for staged .deb reviews.

    Deliberately not /tmp: system-wide tmpfiles cleaners can remove a staging
    directory between the review dialog and the install, which turned a valid
    review into a spurious "no such file" failure. The per-user cache is only
    ever touched by this user.

    Uses :func:`appector.paths.cache_dir` so XDG_CACHE_HOME is honoured and
    the legacy cache directory is only reused while it still exists.
    """
    root = cache_dir() / "deb-review"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    home = Path.home().resolve()
    if root.is_symlink() or not root.resolve().is_relative_to(home):
        raise OSError(
            f"Package-review scratch directory is outside the home folder: {root}"
        )
    os.chmod(root, 0o700)
    return root



# ---------------------------------------------------------------------------
# Structured review facts (F13/U10)
# ---------------------------------------------------------------------------

# Members of a .deb's control archive that run with root privileges or
# otherwise change system behaviour beyond unpacking files.
MAINTAINER_SCRIPT_NAMES = (
    "preinst", "postinst", "prerm", "postrm", "config", "triggers",
)

_REVIEW_CONTENTS_TIMEOUT = 60


def _dpkg_deb_control_members(dpkg_deb, path, timeout=_REVIEW_CONTENTS_TIMEOUT):
    """Names of the control-archive members (./postinst, ./conffiles, ...)."""
    import subprocess as _subprocess

    try:
        control_tar = _subprocess.run(
            [dpkg_deb, "--ctrl-tarfile", str(path)],
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, _subprocess.TimeoutExpired):
        return []
    if control_tar.returncode != 0:
        return []
    try:
        listing = _subprocess.run(
            ["tar", "-t"],
            input=control_tar.stdout,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, _subprocess.TimeoutExpired):
        return []
    if listing.returncode != 0:
        return []
    members = []
    for line in listing.stdout.decode("utf-8", errors="replace").splitlines():
        name = line.strip().lstrip("./").strip()
        if name:
            members.append(name)
    return members


def _dpkg_deb_contents(dpkg_deb, path, timeout=_REVIEW_CONTENTS_TIMEOUT):
    """(mode, path) pairs of the data archive, from `dpkg-deb --contents`."""
    try:
        result = subprocess.run(
            [dpkg_deb, "--contents", str(path)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    entries = []
    # Lines look like:
    # -rwsr-xr-x root/root 123 2024-01-01 00:00 ./usr/bin/tool
    for line in result.stdout.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6 or not line.startswith("-"):
            continue
        mode = parts[0]
        entry_path = parts[5].strip()
        if entry_path.startswith("./"):
            entry_path = entry_path[2:]
        entries.append((mode, entry_path))
    return entries


def _security_findings(modes_and_paths):
    """Security-relevant facts about a package's data files (F13)."""
    setuid_files = []
    etc_files = []
    apt_config_files = []
    systemd_units = []
    for mode, entry_path in modes_and_paths:
        if entry_path.endswith("/"):
            continue
        permissions = mode[1:10]
        if "s" in permissions or "S" in permissions:
            setuid_files.append(entry_path)
        if entry_path.startswith("etc/") and not entry_path.endswith("/"):
            etc_files.append(entry_path)
            if entry_path.startswith("etc/apt/"):
                apt_config_files.append(entry_path)
        if entry_path.startswith("lib/systemd/system/") or entry_path.startswith(
            "usr/lib/systemd/system/"
        ):
            systemd_units.append(entry_path)

    warnings = []
    if setuid_files:
        warnings.append(
            "Installs setuid/setgid programs (run with elevated privileges "
            "regardless of who starts them): " + ", ".join(setuid_files[:10])
        )
    if apt_config_files:
        warnings.append(
            "Adds files under /etc/apt (package sources, preferences or "
            "authentication could be altered): " + ", ".join(apt_config_files[:10])
        )
    if etc_files and len(etc_files) > 10:
        warnings.append(
            f"Adds {len(etc_files)} configuration files under /etc."
        )
    if systemd_units:
        warnings.append(
            "Installs systemd units (background services or timers will be "
            "registered): " + ", ".join(systemd_units[:10])
        )
    return {
        "setuid_files": setuid_files,
        "etc_files": etc_files,
        "apt_config_files": apt_config_files,
        "systemd_units": systemd_units,
        "warnings": warnings,
    }


def review_deb_batch(paths):
    """Stage local packages, inspect metadata and simulate their APT transaction."""
    if not paths:
        return None, "No .deb files selected."

    originals = []
    seen = set()
    for path in paths:
        source = Path(path).expanduser()
        if source.suffix.lower() != ".deb":
            return None, f"Selected file is not a .deb package: {path}"
        source = source.absolute()
        if str(source) not in seen:
            seen.add(str(source))
            originals.append(source)

    if not originals:
        return None, "No valid .deb files selected."

    dpkg_deb = shutil.which("dpkg-deb")
    dpkg = shutil.which("dpkg")
    apt_get = shutil.which("apt-get")
    if not dpkg_deb or not dpkg or not apt_get:
        return None, "dpkg-deb, dpkg, and apt-get are required to review local packages."

    try:
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="appector-deb-review-",
            dir=_deb_review_scratch_dir(),
        )
    except OSError as error:
        return None, f"Could not create private package-review storage: {error}"
    staged_paths = []
    hashes = []
    package_summaries = []
    package_records = []

    def fail(message):
        temporary_directory.cleanup()
        return None, message

    architecture_status, host_architecture = _run_command_raw(
        [dpkg, "--print-architecture"],
        timeout=30,
        env_overrides={"LC_ALL": "C"},
    )
    if architecture_status != 0 or not host_architecture:
        return fail(f"Could not determine the system architecture: {host_architecture}")
    host_architecture = host_architecture.splitlines()[0].strip()

    for index, source in enumerate(originals):
        source_fd = None
        staged_fd = None
        try:
            source_fd = os.open(
                source,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            staged_path = Path(temporary_directory.name) / f"{index:04d}-{source.name}"
            staged_fd = os.open(
                staged_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            digest = hashlib.sha256()
            with os.fdopen(source_fd, "rb") as source_file:
                source_fd = None
                with os.fdopen(staged_fd, "wb") as staged_file:
                    staged_fd = None
                    source_mode = os.fstat(source_file.fileno()).st_mode
                    if not stat.S_ISREG(source_mode):
                        return fail(f"Selected package is not a regular file: {source}")
                    for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                        digest.update(chunk)
                        staged_file.write(chunk)
        except OSError as error:
            for descriptor in (source_fd, staged_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            return fail(f"Could not safely copy selected package {source}: {error}")

        metadata_status, control = _run_command_raw(
            [dpkg_deb, "--field", str(staged_path)],
            timeout=60,
            env_overrides={"LC_ALL": "C"},
        )
        if metadata_status != 0:
            return fail(f"Could not read Debian package metadata for {source.name}:\n{control}")

        metadata = Parser().parsestr(control)
        package_name = metadata.get("Package", "").strip()
        version = metadata.get("Version", "").strip()
        architecture = metadata.get("Architecture", "").strip()
        if not package_name or not version or not architecture:
            return fail(
                f"Package metadata is missing Package, Version, or Architecture: "
                f"{source.name}"
            )
        if architecture not in {"all", host_architecture}:
            return fail(
                f"{source.name} is for architecture {architecture}, but this "
                f"system is {host_architecture}."
            )

        staged_paths.append(staged_path)
        hashes.append(digest.hexdigest())
        control_members = _dpkg_deb_control_members(dpkg_deb, staged_path)
        maintainer_scripts = sorted(
            name for name in control_members
            if name in MAINTAINER_SCRIPT_NAMES or name.startswith("triggers")
        )
        conffiles = []
        if "conffiles" in control_members:
            try:
                conf_status, conf_listing = _run_command_raw(
                    [dpkg_deb, "--info", str(staged_path), "conffiles"],
                    timeout=30,
                    env_overrides={"LC_ALL": "C"},
                )
                if conf_status == 0:
                    conffiles = [
                        line.strip().lstrip("/")
                        for line in conf_listing.splitlines()
                        if line.strip() and not line.startswith(" ")
                    ]
            except OSError:
                conffiles = []
        findings = _security_findings(
            _dpkg_deb_contents(dpkg_deb, staged_path)
        )
        fields = (
            ("Package", package_name),
            ("Version", version),
            ("Architecture", architecture),
            ("Maintainer", metadata.get("Maintainer", "Not specified").strip()),
            ("Depends", metadata.get("Depends", "Not specified").strip()),
            ("Pre-Depends", metadata.get("Pre-Depends", "Not specified").strip()),
            ("Recommends", metadata.get("Recommends", "Not specified").strip()),
        )
        package_summary = "\n".join(f"{label}: {value}" for label, value in fields)
        description = metadata.get("Description", "").strip().splitlines()
        if description:
            package_summary += "\nDescription: " + description[0][:400]
        try:
            staged_size = staged_path.stat().st_size
        except OSError as error:
            return fail(f"Could not inspect staged package {source.name}: {error}")
        package_summary += (
            f"\nFile size: {staged_size} bytes"
            f"\nSHA-256: {digest.hexdigest()}"
        )
        record = {
            "filename": source.name,
            "name": package_name,
            "version": version,
            "architecture": architecture,
            "maintainer": metadata.get("Maintainer", "Not specified").strip(),
            "description": (description[0] if description else ""),
            "depends": metadata.get("Depends", "").strip(),
            "pre_depends": metadata.get("Pre-Depends", "").strip(),
            "recommends": metadata.get("Recommends", "").strip(),
            "size": staged_size,
            "sha256": digest.hexdigest(),
            "maintainer_scripts": maintainer_scripts,
            "conffiles": conffiles,
            **findings,
        }
        if maintainer_scripts:
            record["warnings"].insert(
                0,
                "Contains maintainer scripts that run as root during install or "
                "removal: " + ", ".join(maintainer_scripts),
            )
        package_records.append(record)
        package_summaries.append(package_summary)
        try:
            os.chmod(staged_path, 0o400)
        except OSError as error:
            return fail(
                f"Could not protect staged package {source.name} from normal "
                f"writes: {error}"
            )

    simulation_status, simulation = _run_command_raw(
        [
            apt_get,
            "--simulate",
            "install",
            *(str(path) for path in staged_paths),
        ],
        timeout=120,
        env_overrides={"LC_ALL": "C"},
    )
    if simulation_status != 0:
        return fail(
            "APT could not produce a transaction preview. Installation is blocked "
            "until a preview can be shown:\n" + simulation
        )

    summary = (
        "\n\n".join(package_summaries)
        + "\n\nAPT transaction simulation (no changes made):\n"
        + (simulation[:12000] or "APT reported no transaction details.")
    )
    if len(simulation) > 12000:
        summary += "\n\n[APT output truncated for display.]"

    return (
        DebInstallReview(
            temporary_directory,
            [str(path) for path in originals],
            staged_paths,
            hashes,
            simulation,
            summary,
            package_records,
        ),
        "",
    )


def install_deb_batch(
    paths,
    output_callback=None,
    delete_source=False,
    review=None,
):
    """
    Installs multiple local .deb files using one apt-get transaction.

    Returns:
        success: bool
        message: str
    """

    if not paths:
        return False, "No .deb files selected."

    if review is None:
        return False, "A package metadata and transaction review is required."

    staged_paths = [Path(path).absolute() for path in paths]
    if staged_paths != [Path(path).absolute() for path in review.staged_paths]:
        return False, "Selected Debian packages no longer match the reviewed files."

    # The staging area can be swept away between the review dialog and this
    # call. Re-create it from the reviewed bytes rather than failing with a
    # bare "no such file"; this refuses if the original has changed, so the
    # review's guarantee is unchanged.
    try:
        review.ensure_staged()
    except OSError as error:
        return False, (
            "The reviewed copy of the selected package is no longer "
            f"available. No package was installed.\n\n{error}"
        )

    for path, expected_hash in zip(staged_paths, review.hashes, strict=True):
        digest = hashlib.sha256()
        package_fd = None
        try:
            package_fd = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(package_fd, "rb") as package_file:
                package_fd = None
                if not stat.S_ISREG(os.fstat(package_file.fileno()).st_mode):
                    return False, (
                        f"Reviewed package is no longer a regular file: "
                        f"{path.name}."
                    )
                for chunk in iter(lambda: package_file.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            return False, f"Could not verify reviewed package {path.name}: {error}"
        finally:
            if package_fd is not None:
                os.close(package_fd)
        if digest.hexdigest() != expected_hash:
            return False, (
                f"Reviewed package changed before installation: {path.name}. "
                "Review the package again."
            )

    deb_paths = []
    invalid_paths = []
    seen = set()

    for path in paths:
        p = Path(path).expanduser()

        if not p.is_file() or p.suffix.lower() != ".deb":
            invalid_paths.append(str(path))
            continue

        if p in seen:
            continue

        seen.add(p)
        deb_paths.append(p)

    if invalid_paths:
        return False, (
            "The following selected files are not valid .deb packages:\n\n"
            + "\n".join(invalid_paths)
        )

    if not deb_paths:
        return False, "No valid .deb files selected."

    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"
    simulation_status, simulation = _run_command_raw(
        [
            apt_get_command,
            "--simulate",
            "install",
            "--",
            *(str(path) for path in deb_paths),
        ],
        timeout=120,
        env_overrides={"LC_ALL": "C"},
    )
    if simulation_status != 0:
        return False, (
            "APT could not re-check the transaction after confirmation. "
            "No package was installed:\n" + simulation
        )
    if simulation != review.simulation:
        return False, (
            "The APT transaction changed after you reviewed it. No package was "
            "installed; select the files again to review the updated plan."
        )

    # Re-hash after the second simulation: the staged files could have been
    # swapped between the first hash check and the simulation above. Checking
    # again here closes the window before the privileged install runs.
    for path, expected_hash in zip(staged_paths, review.hashes, strict=True):
        digest = hashlib.sha256()
        package_fd = None
        try:
            package_fd = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(package_fd, "rb") as package_file:
                package_fd = None
                if not stat.S_ISREG(os.fstat(package_file.fileno()).st_mode):
                    return False, (
                        "Reviewed package is no longer a regular file: "
                        f"{path.name}."
                    )
                for chunk in iter(lambda: package_file.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            return False, f"Could not verify reviewed package {path.name}: {error}"
        finally:
            if package_fd is not None:
                os.close(package_fd)
        if digest.hexdigest() != expected_hash:
            return False, (
                f"Reviewed package changed before installation: {path.name}. "
                "Review the package again."
            )

    # S1: the privileged install runs through the helper, which copies
    # each reviewed file into root-owned staging *while hashing it* and
    # installs only the copies — root never opens a user-writable path
    # after verification.
    helper_files = [
        {"path": str(path), "sha256": expected_hash}
        for path, expected_hash in zip(staged_paths, review.hashes, strict=True)
    ]

    _log_action(
        "DEB_BATCH_INSTALL_START helper=install-deb "
        f"files={len(helper_files)}"
    )

    try:
        result = helper_client.install_deb(helper_files, output_callback)
    except HelperUnavailable as error:
        return False, f"{error}"
    except OSError as error:
        return False, f"Could not start the privileged installer: {error}"

    success = result.ok
    message = result.message or ("Installed." if success else "Installation failed.")

    if success:
        if delete_source:
            results = review.trash_unchanged_sources()
            if results:
                message += "\n\n" + results

        _log_action(f"DEB_BATCH_INSTALL_SUCCESS count={len(deb_paths)}")
    else:
        _log_action(f"DEB_BATCH_INSTALL_FAILED error={message}")

    return success, message


# ------------------------------------------------------------
# Batch .flatpakref installation
# ------------------------------------------------------------

def install_flatpak_ref_batch(
    paths,
    user_install: bool = True,
    output_callback=None,
    delete_source: bool = False,
    reviewed_fields_by_path=None,
):
    """
    Installs multiple .flatpakref files sequentially.

    Returns:
        success: bool
        message: str
        number of resolved files (installed or already installed): int
    """

    if not paths:
        return True, "Installed (0): none\nAlready installed (0): none\nFailed (0): none", 0

    total = len(paths)
    installed = []
    already_installed = []
    failures = []
    success_count = 0
    source_file_results = []

    for index, path in enumerate(paths, start=1):
        p = Path(path).expanduser()
        name = p.name

        if output_callback:
            output_callback(f"[{index}/{total}] Installing {name}…")

        success, message = install_flatpak_ref_file(
            str(p),
            user_install,
            output_callback,
            delete_source,
            reviewed_fields=(reviewed_fields_by_path or {}).get(str(Path(p).expanduser().absolute())),
        )

        if success:
            success_count += 1
            if _flatpak_output_is_already_installed(message):
                already_installed.append(name)
                result = "ALREADY INSTALLED"
            else:
                installed.append(name)
                result = "INSTALLED"
            if delete_source and message:
                for line in message.splitlines():
                    if (
                        "Moved installation file to Trash" in line
                        or "Could not move installation file to Trash" in line
                    ):
                        source_file_results.append(f"{name}: {line}")
                        break
        else:
            first_line = (message or "").splitlines()[0] if message else "Installation failed"
            failures.append(f"{name}: {first_line}")
            result = "FAILED"

        if output_callback:
            output_callback(f"[{index}/{total}] {result} {name}")

    summary = _format_flatpak_batch_summary(installed, already_installed, failures)

    if source_file_results:
        summary += "\n\nSource file cleanup:\n" + "\n".join(source_file_results[:20])

    return not failures, summary, success_count
# ------------------------------------------------------------
# Single non-APT removal
# ------------------------------------------------------------

def execute_removal(app):
    """
    Used mostly for AppImage removal and some non-batch cases.
    """

    key = app_key(app)
    manager = getattr(app, "manager", "")
    package_id = getattr(app, "package_id", "")

    if is_blocked(app):
        message = "Blocked by safety policy."
        _log_action(f"REMOVE_BLOCKED {key} manager={manager} reason=blocked")
        return False, message

    if not can_remove(app):
        message = "Removal is not enabled for this package type yet."
        _log_action(f"REMOVE_SKIPPED {key} manager={manager} reason=not-enabled")
        return False, message

    if not package_id:
        message = "Package ID is missing."
        _log_action(f"REMOVE_FAILED {key} manager={manager} reason=missing-package-id")
        return False, message

    # ------------------------------------------------------------
    # AppImage removal
    # ------------------------------------------------------------
    if manager == "AppImage":
        raw_removal_paths = getattr(app, "removal_paths", None)
        if raw_removal_paths:
            if isinstance(raw_removal_paths, str):
                removal_paths = raw_removal_paths.splitlines()
            else:
                removal_paths = list(raw_removal_paths)
            managed_image_roots = [appimage_dir(), legacy_appimage_dir()]
            applications_root = Path.home() / ".local/share/applications"
            allowed_roots = [*managed_image_roots, applications_root]
            resolved_roots = _resolve_allowed_roots(allowed_roots)
            validated_paths = []
            for raw_path in removal_paths:
                try:
                    lexical_str = _validate_removal_file(
                        raw_path, allowed_roots, resolved_roots
                    )
                except OSError:
                    message = f"Refusing to remove an invalid AppImage installation file: {raw_path}"
                    _log_action(f"REMOVE_BLOCKED {key} manager=AppImage reason=invalid-managed-path")
                    return False, message
                path = Path(lexical_str)
                if path.parent == applications_root and (
                    not path.name.startswith("app-manager-appimage-")
                    or path.suffix != ".desktop"
                ):
                    message = f"Refusing to remove an unowned desktop entry: {raw_path}"
                    _log_action(f"REMOVE_BLOCKED {key} manager=AppImage reason=unowned-desktop-file")
                    return False, message
                validated_paths.append(path)

            desktop_files = [
                path for path in validated_paths
                if path.parent == applications_root
            ]
            if len(desktop_files) != 1:
                message = "The integrated AppImage launcher is missing or ambiguous."
                _log_action(f"REMOVE_BLOCKED {key} manager=AppImage reason=invalid-desktop-count")
                return False, message
            try:
                desktop_content = desktop_files[0].read_text(encoding="utf-8")
            except OSError as error:
                return False, f"Could not verify the AppImage launcher: {error}"
            desktop_keys = _desktop_key_values(desktop_content)
            if (
                desktop_keys.get("x-appmanager-installed-appimage") != "true"
                or desktop_keys.get("x-appmanager-appimage-path") != str(package_id)
            ):
                message = "The launcher does not identify this as an Appector installation."
                _log_action(f"REMOVE_BLOCKED {key} manager=AppImage reason=unverified-desktop")
                return False, message

            if not any(
                path.parent in managed_image_roots and path == Path(package_id)
                for path in validated_paths
            ):
                message = "The installed AppImage copy was not included in its removal record."
                _log_action(f"REMOVE_BLOCKED {key} manager=AppImage reason=missing-installed-image")
                return False, message

            removed = []
            try:
                for path in validated_paths:
                    path.unlink()
                    removed.append(str(path))
            except OSError as error:
                message = (
                    f"Could not remove the integrated AppImage: {error}\n\n"
                    "Removed files:\n" + "\n".join(removed)
                )
                _log_action(f"REMOVE_FAILED {key} manager=AppImage error={error}")
                return False, message

            message = "Removed AppImage launcher and installed copy:\n" + "\n".join(removed)
            _log_action(f"REMOVE_SUCCESS {key} manager=AppImage integrated=true")
            return True, message

        # S8: a standalone AppImage is still a file deletion, so it gets
        # the same allowlist/ownership/regular-file validation as managed
        # removals instead of unlinking package_id unchecked.
        allowed_roots = [
            Path.home() / "Applications",
            Path.home() / "Downloads",
            Path("/opt"),
            Path("/usr/local/bin"),
        ]
        resolved_roots = _resolve_allowed_roots(allowed_roots)
        try:
            lexical = _validate_removal_file(package_id, allowed_roots, resolved_roots)
        except OSError as error:
            _log_action(
                f"REMOVE_BLOCKED {key} manager=AppImage reason=invalid-path"
            )
            return False, str(error)

        path = Path(lexical)

        if not path.is_file():
            message = "AppImage file not found."
            _log_action(f"REMOVE_FAILED {key} manager=AppImage reason=file-not-found")
            return False, message

        cmd_preview = f"rm {shlex.quote(str(path))}"
        _log_action(f"REMOVE_START {key} manager=AppImage cmd={cmd_preview}")

        try:
            path.unlink()
            message = f"Deleted:\n{path}"
            _log_action(f"REMOVE_SUCCESS {key} manager=AppImage")
            return True, message
        except Exception as e:
            message = str(e)
            _log_action(f"REMOVE_FAILED {key} manager=AppImage error={message}")
            return False, message

    if manager == "Manual":
        paths = _manual_removal_paths(app)
        if not paths:
            message = "No safe launcher or executable files were detected."
            _log_action(f"REMOVE_FAILED {key} manager=Manual reason=no-safe-paths")
            return False, message

        allowed_roots = [
            Path.home() / ".local/share/applications",
            Path("/usr/local/share/applications"),
            Path.home() / ".local/bin",
            Path.home() / ".cargo/bin",
            Path.home() / "go/bin",
            Path.home() / ".npm-global/bin",
            # Retained so launchers created by older versions in this
            # developer-specific directory can still be removed.
            Path.home() / ".opencode/bin",
            Path.home() / "bin",
            Path.home() / "Applications",
            Path("/usr/local/bin"),
            Path("/opt"),
        ]
        resolved_roots = _resolve_allowed_roots(allowed_roots)
        validated_paths = []
        for value in paths:
            try:
                validated_paths.append(
                    _validate_removal_file(value, allowed_roots, resolved_roots)
                )
            except OSError as error:
                message = str(error)
                reason = (
                    "outside-allowed-path"
                    if "outside known manual-install" in message
                    else "invalid-path"
                )
                _log_action(f"REMOVE_FAILED {key} manager=Manual reason={reason} path={value}")
                if reason == "outside-allowed-path":
                    _log_action(f"REMOVE_BLOCKED {key} manager=Manual reason=outside-allowed-path")
                return False, message

        requires_privilege = any(
            not os.access(str(Path(path).parent), os.W_OK)
            for path in validated_paths
        )
        if requires_privilege:
            message = (
                "Appector refuses to remove manual app files that require "
                "administrator privileges. Remove this app with its package "
                "manager or another reviewed system-administration method."
            )
            _log_action(f"REMOVE_BLOCKED {key} manager=Manual reason=privileged-path")
            return False, message
        else:
            removed = []
            failures = []
            for value in validated_paths:
                try:
                    Path(value).unlink()
                    removed.append(value)
                except OSError as error:
                    failures.append(f"{value}: {error}")

            if failures:
                message = "Some manual app files could not be removed:\n" + "\n".join(failures)
                if removed:
                    message += "\n\nRemoved:\n" + "\n".join(removed)
                _log_action(f"REMOVE_FAILED {key} manager=Manual error={message}")
                return False, message

        message = "Removed manual app files:\n" + "\n".join(validated_paths)
        _log_action(f"REMOVE_SUCCESS {key} manager=Manual")
        return True, message

    message = "Use batch removal for this package type."
    _log_action(f"REMOVE_SKIPPED {key} manager={manager} reason=use-batch")
    return False, message


def _desktop_key_values(content: str):
    """Parse a .desktop file into its [Desktop Entry] key/value pairs (S8).

    Exact per-line parsing instead of substring search, so a launcher for
    ``x.AppImage`` can no longer satisfy a check for ``x.AppImage.bak``.
    """
    keys = {}
    in_entry = False
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_entry = line.lower() == "[desktop entry]"
            continue
        if not in_entry or "=" not in line:
            continue
        name, _, value = line.partition("=")
        keys.setdefault(name.strip().lower(), value.strip())
    return keys


def _manual_removal_paths(app):
    raw_paths = getattr(app, "removal_paths", None)
    if isinstance(raw_paths, str):
        paths = raw_paths.splitlines()
    elif raw_paths:
        paths = list(raw_paths)
    else:
        paths = [getattr(app, "package_id", "")]

    return list(dict.fromkeys(
        str(Path(path).expanduser())
        for path in paths
        if str(path or "").strip()
    ))


def _resolve_allowed_roots(roots):
    """Resolve allowed roots once so parent-symlink escapes are detected."""
    resolved = []
    for root in roots:
        try:
            resolved.append(Path(root).expanduser().resolve())
        except OSError:
            resolved.append(Path(os.path.abspath(Path(root).expanduser())))
    return resolved


def _validate_removal_file(value, allowed_roots, resolved_roots):
    """Validate one manual/AppImage file before it is unlinked.

    A path shape check proves well-formedness, not authorization, so this
    requires all three properties of a destructive-path target:

    1. **Allowlisted root** - the symlink-resolved path sits under one of the
       allowed roots, and is at least one level below it (so the root itself
       can never be the deletion target).
    2. **Ownership evidence** - the target is owned by the current user. This
       mirrors the kernel's sticky-directory rule and stops Appector from
       deleting files it does not own.
    3. **Regular file or symlink** - directories, FIFOs, sockets and devices are
       refused outright.

    Returns the lexical absolute path string. Symlinks are permitted because
    unlinking removes only the link; their resolved target is never followed.
    """
    lexical = Path(os.path.abspath(Path(value).expanduser()))
    if not lexical.is_absolute():
        raise OSError(f"Manual app file is not an absolute path: {value}")
    if not any(lexical.is_relative_to(root) for root in allowed_roots):
        raise OSError(
            f"Refusing to remove a file outside known manual-install locations: {value}"
        )

    try:
        file_stat = lexical.lstat()
    except FileNotFoundError:
        raise OSError(
            f"Manual app file no longer exists or is not a regular file: {value}"
        ) from None

    is_symlink = stat.S_ISLNK(file_stat.st_mode)
    if not is_symlink and not stat.S_ISREG(file_stat.st_mode):
        raise OSError(
            f"Refusing to remove a file that is not a regular file: {value}"
        )

    # Ownership evidence, read before the deletion decision. Root-owned files
    # in user-writable directories (sticky dirs, world-writable roots) are
    # never ours to remove.
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    if current_uid is not None and file_stat.st_uid != current_uid:
        raise OSError(
            f"Refusing to remove a file that is not owned by the current user: "
            f"{value}"
        )

    # Symlinks are deleted by unlinking the link itself, but the parent
    # chain may still be redirected out of the allowlist (S8): resolve the
    # parent directory and require it to sit inside an allowed root, the
    # same rule non-symlink targets follow.
    if is_symlink:
        try:
            resolved_parent = lexical.parent.resolve()
        except (OSError, RuntimeError) as error:
            raise OSError(
                f"Could not resolve manual app file {value}: {error}"
            ) from error
        for root in resolved_roots:
            if resolved_parent == root or resolved_parent.is_relative_to(root):
                return str(lexical)
        raise OSError(
            f"Refusing to remove a file outside known manual-install locations: {value}"
        )

    try:
        resolved = lexical.resolve()
    except (OSError, RuntimeError) as error:
        raise OSError(
            f"Could not resolve manual app file {value}: {error}"
        ) from error

    for root in resolved_roots:
        if not resolved.is_relative_to(root):
            continue
        # At least one level below the root, so the root can never be the
        # deletion target itself.
        if len(resolved.relative_to(root).parts) < 1:
            raise OSError(
                f"Refusing to remove an allowed root itself: {value}"
            )
        return str(lexical)

    raise OSError(
        f"Refusing to remove a file outside known manual-install locations: {value}"
    )


# ------------------------------------------------------------
# APT simulation helpers
# ------------------------------------------------------------

def simulate_apt_remove_multiple(package_ids, purge=False):
    """
    Runs:
        apt-get --simulate remove pkg1 pkg2 ...
    or:
        apt-get --simulate purge pkg1 pkg2 ...
    """
    if not package_ids:
        return True, "", []

    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"
    apt_action = "purge" if purge else "remove"

    cmd = [
        apt_get_command,
        "-s",
        apt_action,
        "--",
        *list(package_ids),
    ]

    _log_action(f"APT_SIMULATION cmd={shlex.join(cmd)}")

    returncode, output = _run_command_raw(
        cmd,
        env_overrides={"LC_ALL": "C"},
    )

    if returncode != 0:
        _log_action("APT_SIMULATION_FAILED")
        return False, output, []

    removed_packages = _parse_apt_simulation_removed_packages(output)

    _log_action(f"APT_SIMULATION_SUCCESS removed_count={len(removed_packages)}")

    return True, output, removed_packages


# ------------------------------------------------------------
# Single APT removal
# ------------------------------------------------------------

def prepare_apt_removal(app, purge=False):
    """Preview an APT removal and return the binding plan (S4).

    Returns (success, message, plan, warnings); the plan must be handed to
    execute_apt_removal, which refuses to run without it or when the
    re-simulation no longer matches.
    """
    manager = getattr(app, "manager", "")
    package_id = getattr(app, "package_id", "")

    if manager != "APT":
        return False, "This is not an APT package.", None, ""
    if not can_remove_single(app):
        return False, "This APT package is blocked by safety policy.", None, ""
    if not package_id:
        return False, "APT package ID is missing.", None, ""

    success, output, removed_packages = simulate_apt_remove_multiple([package_id], purge=purge)

    if not success:
        message = (
            "APT simulation failed.\n\n"
            f"{output}"
        )
        return False, message, None, ""

    removed_base_names = {_base_package_name(p) for p in removed_packages}
    target_name = _base_package_name(package_id)

    if target_name not in removed_base_names:
        message = (
            f"APT simulation did not confirm removal of '{package_id}'.\n\n"
            "It may already be removed, held, or not removable by APT.\n\n"
            f"{output}"
        )
        return False, message, None, ""

    critical_removed = [
        p
        for p in removed_packages
        if is_critical_apt_package_name(p)
    ]

    if critical_removed:
        message = (
            "Blocked by safety policy.\n\n"
            "APT simulation says the following critical packages would be removed:\n\n"
            f"{_format_package_list(critical_removed)}\n\n"
            "This operation will not be allowed from this app."
        )
        return False, message, None, ""

    if len(removed_packages) > APT_MAX_REMOVALS:
        message = (
            "Blocked by safety policy.\n\n"
            f"APT simulation says {len(removed_packages)} packages would be removed.\n"
            f"The current safety limit is {APT_MAX_REMOVALS} packages.\n\n"
            "Please review this manually in a terminal."
        )
        return False, message, None, ""

    warnings = []

    if len(removed_packages) > 1:
        warnings.append(
            f"This will also remove {len(removed_packages) - 1} related package(s)."
        )

    if len(removed_packages) > 20:
        warnings.append(
            "A relatively large number of packages will be removed."
        )

    message = (
        "APT simulation preview:\n\n"
        "The following packages will be removed:\n\n"
        f"{_format_package_list(removed_packages)}\n\n"
        f"Total packages to remove: {len(removed_packages)}\n\n"
        f"{get_removal_size_estimate([], apt_package_ids=removed_packages)}"
    )

    plan = Plan(
        action="purge" if purge else "remove",
        package_ids=[package_id],
        removed_preview=list(removed_packages),
        text=message,
    )
    return True, message, plan, "\n".join(warnings)


def execute_apt_removal(app, purge=False, plan=None):
    """Execute an APT removal that the user confirmed via `prepare_apt_removal`.

    S4: the confirmed :class:`Plan` is mandatory. The removal is re-simulated
    and refused when the transaction grew or no longer matches the preview.
    """
    key = app_key(app)
    manager = getattr(app, "manager", "")
    package_id = getattr(app, "package_id", "")

    if manager != "APT":
        return False, "This is not an APT package."

    if not can_remove_single(app):
        message = "Blocked by safety policy."
        _log_action(f"APT_REMOVE_BLOCKED {key} reason=blocked")
        return False, message

    if not package_id:
        message = "APT package ID is missing."
        _log_action(f"APT_REMOVE_FAILED {key} reason=missing-package-id")
        return False, message

    sim_success, sim_output, removed_packages = simulate_apt_remove_multiple([package_id], purge=purge)

    if not sim_success:
        _log_action(f"APT_REMOVE_FAILED {key} reason=simulation-failed")
        return False, sim_output

    plan_ok, plan_message = check_apt_removal_plan(
        plan, [package_id], removed_packages
    )
    if not plan_ok:
        _log_action(f"APT_REMOVE_BLOCKED {key} reason=plan-mismatch")
        return False, plan_message

    # S6: the removal runs through the privileged helper, which
    # re-validates the plan and the policy as root. A purge's conffile
    # backup is made there too, as root (S9).
    _log_action(f"APT_REMOVE_START {key} helper=remove-apt purge={purge}")

    try:
        result = helper_client.remove_apt(
            [package_id],
            plan=plan,
            purge=purge,
            output_callback=None,
        )
    except HelperUnavailable as error:
        return False, f"{error}"
    except OSError as error:
        return False, f"Could not start the privileged helper: {error}"

    success = result.ok
    message = result.message or ("Removed." if success else "Removal failed.")
    backup_note = ""
    for event in result.events:
        if event.get("type") == "backup":
            backup_note = _purge_backup_note(
                event.get("path"), int(event.get("files", 0) or 0)
            )

    if success:
        _log_action(f"APT_REMOVE_SUCCESS {key}")
        if backup_note:
            message = f"{message}\n\n{backup_note}"
    else:
        _log_action(f"APT_REMOVE_FAILED {key} error={message}")
        if backup_note:
            message = f"{message}\n\n{backup_note}"

    return success, message

def prepare_apt_batch_preview(apps, purge=False):
    """
    Prepares a preview for batch APT removal.

    Returns a dictionary:

        has_apt:
            True if APT apps were included

        apt_ok:
            True if APT batch simulation passed safety checks

        preview:
            human-readable APT simulation preview

        blocked:
            list of (app_name, reason)

        blocked_keys:
            set of app_key values that should be excluded from removal

        removed_packages:
            packages APT says will be removed

        warnings:
            list of warning strings
    """

    apts = [
        app
        for app in apps
        if getattr(app, "manager", "") == "APT"
    ]

    preview = {
        "has_apt": bool(apts),
        "apt_ok": True,
        "preview": "",
        "blocked": [],
        "blocked_keys": set(),
        "removed_packages": [],
        "warnings": [],
        # S4: the plan binds the confirmed simulation to the execution.
        "plan": None,
    }

    if not apts:
        return preview

    apt_ids = []
    valid_apps = []

    for app in apts:
        name = getattr(app, "name", "") or getattr(app, "package_id", "") or "Unknown"
        package_id = getattr(app, "package_id", "") or ""

        if is_blocked(app):
            preview["blocked"].append((name, "Blocked by safety policy."))
            preview["blocked_keys"].add(app_key(app))
            continue

        if not _is_safe_package_id(package_id):
            preview["blocked"].append((name, "Unsafe or invalid APT package ID."))
            preview["blocked_keys"].add(app_key(app))
            continue

        apt_ids.append(package_id)
        valid_apps.append(app)

    if not apt_ids:
        preview["apt_ok"] = False
        return preview

    success, output, removed_packages = simulate_apt_remove_multiple(apt_ids, purge=purge)

    preview["removed_packages"] = removed_packages
    if success:
        preview["plan"] = Plan(
            action="batch-remove",
            package_ids=list(apt_ids),
            removed_preview=list(removed_packages),
        )

    if not success:
        preview["apt_ok"] = False
        preview["preview"] = output if output else "APT simulation failed."

        for app in valid_apps:
            preview["blocked"].append(
                (
                    getattr(app, "name", "Unknown"),
                    "APT simulation failed.",
                )
            )
            preview["blocked_keys"].add(app_key(app))

        return preview

    removed_base_names = {
        _base_package_name(package_name)
        for package_name in removed_packages
    }

    missing = [
        package_id
        for package_id in apt_ids
        if _base_package_name(package_id) not in removed_base_names
    ]

    if missing:
        preview["apt_ok"] = False

        preview["preview"] = (
            "APT simulation did not confirm removal for:\n\n"
            f"{_format_package_list(missing)}\n\n"
            f"{output}"
        )

        for app in valid_apps:
            preview["blocked"].append(
                (
                    getattr(app, "name", "Unknown"),
                    "Not confirmed by APT simulation.",
                )
            )
            preview["blocked_keys"].add(app_key(app))

        return preview

    critical_removed = [
        package_name
        for package_name in removed_packages
        if is_critical_apt_package_name(package_name)
    ]

    if critical_removed:
        preview["apt_ok"] = False

        preview["preview"] = (
            "Blocked by safety policy.\n\n"
            "APT simulation says the following critical packages would be removed:\n\n"
            f"{_format_package_list(critical_removed)}"
        )

        for app in valid_apps:
            preview["blocked"].append(
                (
                    getattr(app, "name", "Unknown"),
                    "Simulation would remove critical packages.",
                )
            )
            preview["blocked_keys"].add(app_key(app))

        return preview

    if len(removed_packages) > APT_MAX_REMOVALS:
        preview["apt_ok"] = False

        preview["preview"] = (
            "Blocked by safety policy.\n\n"
            f"APT simulation says {len(removed_packages)} packages would be removed.\n"
            f"The current safety limit is {APT_MAX_REMOVALS} packages."
        )

        for app in valid_apps:
            preview["blocked"].append(
                (
                    getattr(app, "name", "Unknown"),
                    "Too many packages would be removed.",
                )
            )
            preview["blocked_keys"].add(app_key(app))

        return preview

    selected_packages = sorted(
        {
            _base_package_name(package_id)
            for package_id in apt_ids
        }
    )

    selected_base_names = set(selected_packages)

    extra_packages = sorted(
        [
            package_name
            for package_name in removed_packages
            if _base_package_name(package_name) not in selected_base_names
        ]
    )

    lines = []

    lines.append("Selected APT packages:")
    lines.append("")
    lines.append(_format_package_list(selected_packages))

    if extra_packages:
        lines.append("")
        lines.append("Additional packages that will also be removed:")
        lines.append("")
        lines.append(_format_package_list(extra_packages))

    lines.append("")
    lines.append(f"Total packages to remove: {len(removed_packages)}")

    preview["preview"] = "\n".join(lines)

    if extra_packages:
        preview["warnings"].append(
            f"This will also remove {len(extra_packages)} additional package(s)."
        )

    if len(removed_packages) > 20:
        preview["warnings"].append(
            "A relatively large number of packages will be removed."
        )

    return preview

# ------------------------------------------------------------
# Batch removal
# ------------------------------------------------------------

def execute_batch_removal(apps, progress_callback=None, purge=False, apt_plan=None):

    """
    Batch removal designed to reduce repeated privilege prompts.

    AppImage:
        removed individually, no privilege needed

    User Flatpak:
        grouped in one non-privileged script

    Snap/system Flatpak/APT:
        grouped into one privileged shell script where possible

    Returns:

        results:
            list of (app, success, message)

        summary:
            short summary string
    """

    apps = list(apps)

    results_by_key = {}

    manual_apps = []
    appimages = []
    snaps = []
    user_flatpaks = []
    system_flatpaks = []
    apts = []

    for app in apps:
        manager = getattr(app, "manager", "")

        if manager == "Manual":
            manual_apps.append(app)
        elif manager == "AppImage":
            appimages.append(app)
        elif manager == "Snap":
            snaps.append(app)
        elif manager == "Flatpak":
            # F11: the scanner records the installation name explicitly;
            # falling back to the old "system" substring check keeps old
            # entries working.
            installation = str(getattr(app, "installation", "") or "").strip().lower()
            details = str(getattr(app, "details", "") or "").lower()
            if installation:
                is_system = installation not in {"user", ""}
            else:
                is_system = "system" in details
            if is_system:
                system_flatpaks.append(app)
            else:
                user_flatpaks.append(app)
        elif manager == "APT":
            apts.append(app)

    # F16: the step total is tracked dynamically so blocked groups can
    # neither stall the bar below 100% nor double-count a group that never
    # runs (every APT item blocked, for example).
    progress_state = {"current": 0, "total": 0}

    def report(message, reserve=False):
        if reserve:
            progress_state["total"] += 1
        else:
            progress_state["current"] += 1
            if progress_state["current"] > progress_state["total"]:
                progress_state["total"] = progress_state["current"]
        if progress_callback:
            progress_callback(
                progress_state["current"],
                progress_state["total"],
                message,
            )

    for app in manual_apps:
        name = getattr(app, "name", "Manual app")
        report(f"Removing {name}…")
        success, message = execute_removal(app)
        results_by_key[app_key(app)] = (success, message)

    # ------------------------------------------------------------
    # AppImage removals
    # ------------------------------------------------------------
    for app in appimages:
        name = getattr(app, "name", "AppImage")
        report(f"Removing {name}…")

        success, message = execute_removal(app)
        results_by_key[app_key(app)] = (success, message)

    # ------------------------------------------------------------
    # User Flatpak removals (no privilege, one command per app)
    # ------------------------------------------------------------
    if user_flatpaks:
        report(f"Removing {len(user_flatpaks)} Flatpak app(s)…")

        for app in user_flatpaks:
            package_id = getattr(app, "package_id", "") or ""
            if not _is_safe_package_id(package_id):
                results_by_key[app_key(app)] = (
                    False,
                    "Unsafe or invalid Flatpak package ID.",
                )
                continue
            installation = str(getattr(app, "installation", "") or "").strip().lower()
            scope = ["--user"] if installation in {"user", ""} else [
                f"--installation={installation}"
            ]
            cmd = [
                shutil.which("flatpak") or "flatpak",
                "uninstall",
                "-y",
                *scope,
                "--",
                package_id,
            ]
            returncode, output = _run_command_raw(cmd, timeout=300)
            success = returncode == 0
            results_by_key[app_key(app)] = (
                success,
                output if success else (
                    output or "Flatpak removal failed."
                ),
            )

    # ------------------------------------------------------------
    # APT validation/simulation
    # ------------------------------------------------------------
    apt_valid_apps = []
    apt_ids = []

    if apts:
        report("Checking APT dependencies…", reserve=True)

        for app in apts:
            package_id = getattr(app, "package_id", "") or ""

            if is_blocked(app):
                results_by_key[app_key(app)] = (
                    False,
                    "Blocked by safety policy.",
                )
                continue

            if not _is_safe_package_id(package_id):
                results_by_key[app_key(app)] = (
                    False,
                    "Unsafe or invalid APT package ID.",
                )
                continue

            apt_valid_apps.append(app)
            apt_ids.append(package_id)

        if apt_ids:
            sim_success, sim_output, removed_packages = simulate_apt_remove_multiple(apt_ids, purge=purge)

            if not sim_success:
                for app in apt_valid_apps:
                    results_by_key[app_key(app)] = (
                        False,
                        sim_output if sim_output else "APT simulation failed.",
                    )

                apt_valid_apps = []
                apt_ids = []
            else:
                # S4: consent is bound to the confirmed plan; a re-simulation
                # that grew (or a missing plan) blocks only the APT group.
                plan_ok, plan_message = check_apt_removal_plan(
                    apt_plan, apt_ids, removed_packages
                )

                if not plan_ok:
                    for app in apt_valid_apps:
                        results_by_key[app_key(app)] = (False, plan_message)

                    apt_valid_apps = []
                    apt_ids = []

    # ------------------------------------------------------------
    # Privileged grouped removal:
    # Snap + system Flatpak + APT
    # ------------------------------------------------------------
    snap_ids = []
    system_flatpak_ids = []
    privileged_apps = []
    privileged_expected = []

    snap_exe = shutil.which("snap") if snaps else None
    flatpak_exe = shutil.which("flatpak") if system_flatpaks else None
    apt_get_exe = shutil.which("apt-get") if apt_valid_apps else None

    if snaps and not snap_exe:
        for app in snaps:
            results_by_key[app_key(app)] = (
                False,
                "snap command not found.",
            )

    if snaps and snap_exe:
        for app in snaps:
            package_id = getattr(app, "package_id", "") or ""

            if not _is_safe_package_id(package_id):
                results_by_key[app_key(app)] = (
                    False,
                    "Unsafe or invalid Snap package ID.",
                )
                continue

            if is_blocked(app):
                results_by_key[app_key(app)] = (
                    False,
                    "Blocked by safety policy.",
                )
                continue

            snap_ids.append(package_id)
            privileged_apps.append(app)
            privileged_expected.append(("Snap", package_id))

    if system_flatpaks and not flatpak_exe:
        for app in system_flatpaks:
            results_by_key[app_key(app)] = (
                False,
                "flatpak command not found.",
            )

    if system_flatpaks and flatpak_exe:
        for app in system_flatpaks:
            package_id = getattr(app, "package_id", "") or ""

            if not _is_safe_package_id(package_id):
                results_by_key[app_key(app)] = (
                    False,
                    "Unsafe or invalid Flatpak package ID.",
                )
                continue

            if is_blocked(app):
                results_by_key[app_key(app)] = (
                    False,
                    "Blocked by safety policy.",
                )
                continue

            system_flatpak_ids.append(package_id)
            privileged_apps.append(app)
            privileged_expected.append(("Flatpak", package_id))

    if apt_valid_apps and not apt_get_exe:
        for app in apt_valid_apps:
            results_by_key[app_key(app)] = (
                False,
                "apt-get command not found.",
            )

        apt_valid_apps = []
        apt_ids = []

    if apt_valid_apps and apt_get_exe:
        for app in apt_valid_apps:
            privileged_apps.append(app)
            privileged_expected.append(
                ("APT", getattr(app, "package_id", "") or "")
            )

    if snap_ids or system_flatpak_ids or apt_ids:
        report("Removing apps with administrator privileges…", reserve=True)

        # S9: the conffile backup for a batch purge is made inside the
        # helper (as root), together with the removal, so a root-only
        # conffile can no longer block the purge or split the transaction.

        # S6: the privileged groups run through the dedicated helper,
        # one named action per manager; the pkexec sh -c text protocol is
        # gone. Failures are isolated per group.
        def fail_group(apps, message):
            for app in apps:
                results_by_key[app_key(app)] = (False, message)

        if snap_ids:
            try:
                result = helper_client.remove_snap(
                    snap_ids, output_callback=None
                )
                snap_message = result.message or (
                    "Removed." if result.ok else "Snap removal failed."
                )
                for app in snaps:
                    if app_key(app) not in results_by_key:
                        results_by_key[app_key(app)] = (result.ok, snap_message)
            except (HelperUnavailable, OSError) as error:
                fail_group(
                    [app for app in snaps if app_key(app) not in results_by_key],
                    str(error),
                )

        if system_flatpak_ids:
            # Group by installation: refs live in specific system
            # installations, and collapsing mixed selections onto the
            # default one would uninstall from the wrong place (or fail).
            flatpak_groups = {}
            for app in system_flatpaks:
                if app_key(app) in results_by_key:
                    continue  # already blocked or failed individually
                installation = str(
                    getattr(app, "installation", "") or ""
                ).strip()
                flatpak_groups.setdefault(installation, []).append(app)
            for installation, group_apps in flatpak_groups.items():
                group_ids = [
                    getattr(app, "package_id", "") or "" for app in group_apps
                ]
                try:
                    result = helper_client.remove_flatpak_system(
                        group_ids,
                        installation=installation or None,
                        output_callback=None,
                    )
                    flatpak_message = result.message or (
                        "Removed." if result.ok else "Flatpak removal failed."
                    )
                    for app in group_apps:
                        results_by_key[app_key(app)] = (
                            result.ok,
                            flatpak_message,
                        )
                except (HelperUnavailable, OSError) as error:
                    fail_group(group_apps, str(error))

        if apt_ids and apt_plan is None:
            # Never fabricate a plan from the live simulation: consent must
            # bind to the preview the user actually confirmed (S4). The
            # gate above usually catches this earlier.
            fail_group(apt_valid_apps, "A confirmed removal plan is required.")
        elif apt_ids:
            try:
                result = helper_client.remove_apt(
                    apt_ids,
                    plan=apt_plan,
                    purge=purge,
                    output_callback=None,
                )
                apt_message = result.message or (
                    "Removed." if result.ok else "APT removal failed."
                )
                events_note = ""
                for event in result.events:
                    if event.get("type") == "backup":
                        events_note = _purge_backup_note(
                            event.get("path"), int(event.get("files", 0) or 0)
                        )
                if events_note:
                    apt_message = f"{apt_message}\n\n{events_note}"
                for app in apt_valid_apps:
                    if app_key(app) not in results_by_key:
                        results_by_key[app_key(app)] = (result.ok, apt_message)
            except (HelperUnavailable, OSError) as error:
                fail_group(
                    [app for app in apt_valid_apps
                     if app_key(app) not in results_by_key],
                    str(error),
                )

    # ------------------------------------------------------------
    # Preserve original order
    # ------------------------------------------------------------
    results = []

    for app in apps:
        key = app_key(app)

        success, message = results_by_key.get(
            key,
            (False, "No removal result."),
        )

        results.append((app, success, message))

    success_count = sum(1 for _, success, _ in results if success)
    failed_count = len(results) - success_count

    summary = f"{success_count} removed, {failed_count} failed."

    _log_action(f"BATCH_REMOVAL_FINISHED {summary}")

    return results, summary


# ------------------------------------------------------------
# APT Autoremove
# ------------------------------------------------------------


def get_apt_autoremove_preview():
    """
    Simulates apt-get autoremove and returns the list of packages
    that would be removed, plus estimated disk space freed.

    Returns:
        success: bool
        packages: list[str]
        space_freed: str
        raw_output: str
        plan: Plan (S4; binds the confirmed autoremove set to execution)
    """
    cmd = ["apt-get", "--simulate", "autoremove"]
    try:
        returncode, output = _run_command_raw(
            cmd,
            timeout=60,
            env_overrides={"LC_ALL": "C"},
            uses_apt=True,
        )
        if returncode != 0:
            return False, [], "", output or (
                f"APT autoremove simulation failed with exit code {returncode}."
            ), None

        packages = []
        for line in output.splitlines():
            match = re.match(r"^\s*Remv\s+([^\s]+)", line)
            if match:
                package = match.group(1).split(":", 1)[0]
                if package not in packages:
                    packages.append(package)

        # Parse disk space freed
        space_freed = ""
        for line in output.splitlines():
            lower = line.lower()
            if "freed" in lower or "disk space" in lower:
                space_freed = line.strip()
                break

        plan = Plan(
            action="autoremove",
            package_ids=[],
            removed_preview=list(packages),
        )
        return True, packages, space_freed, output, plan

    except Exception as e:
        return False, [], "", str(e), None


def execute_apt_autoremove(output_callback=None, plan=None):
    """
    Executes apt-get autoremove with pkexec.

    S4: requires the :class:`Plan` produced by `get_apt_autoremove_preview`.
    The autoremove set is re-simulated first and the shared policy gate
    (plan binding, critical packages, cascade limit) is applied — the old
    flow ran `autoremove -y` with no gate at all.
    """
    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"

    returncode, simulate_output = _run_command_raw(
        [apt_get_command, "--simulate", "autoremove"],
        timeout=120,
        env_overrides={"LC_ALL": "C"},
        uses_apt=True,
    )
    if returncode != 0:
        _log_action("APT_AUTOREMOVE_BLOCKED reason=simulation-failed")
        return False, (
            "Autoremove was not started: the APT simulation failed.\n\n"
            f"{simulate_output}"
        )

    actual_removed = _parse_apt_simulation_removed_packages(simulate_output)
    plan_ok, plan_message = check_apt_removal_plan(plan, [], actual_removed)
    if not plan_ok:
        _log_action("APT_AUTOREMOVE_BLOCKED reason=plan-mismatch")
        return False, plan_message

    _log_action("APT_AUTOREMOVE_START helper=apt-autoremove")
    try:
        result = helper_client.apt_autoremove(
            plan, output_callback=output_callback
        )
    except HelperUnavailable as error:
        return False, f"{error}"
    except OSError as error:
        return False, f"Could not start the privileged helper: {error}"
    success = result.ok
    message = result.message or ("Autoremove finished." if success else "Autoremove failed.")
    if success:
        _log_action("APT_AUTOREMOVE_SUCCESS")
        return True, message

    _log_action(f"APT_AUTOREMOVE_FAILED error={message}")
    return False, message


# ------------------------------------------------------------
# Flatpak Unused Runtimes
# ------------------------------------------------------------



