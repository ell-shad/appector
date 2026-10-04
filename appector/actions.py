import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
from datetime import datetime
from email.parser import Parser
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

# ------------------------------------------------------------
# ANSI escape code stripper
# ------------------------------------------------------------
ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
APT_COMMAND_LOCK = threading.Lock()
APT_COMMAND_NAMES = {"apt", "apt-get", "dpkg"}


def _command_uses_apt(cmd) -> bool:
    for argument in map(str, cmd):
        if Path(argument).name in APT_COMMAND_NAMES:
            return True
        if re.search(r"(?<![\w.-])(?:apt-get|dpkg)(?=\s)", argument):
            return True
    return False


def _format_apt_lock_error(output: str) -> str:
    lowered = output.lower()
    lock_messages = (
        "could not get lock",
        "unable to acquire",
        "is locked by another process",
        "dpkg frontend lock",
        "dpkg is locked",
    )
    if any(message in lowered for message in lock_messages):
        return (
            "Another package-management operation is using APT/dpkg. "
            "Wait for it to finish, then try again.\n\n"
            f"{output}"
        )
    return output

def _strip_ansi(text: str) -> str:
    """Removes terminal color and cursor control codes from text."""
    if not text:
        return ""
    return ANSI_ESCAPE_RE.sub('', text)

BATCH_REMOVABLE_MANAGERS = {
    "Snap",
    "Flatpak",
    "AppImage",
    "Manual",
    "APT",
}

SINGLE_REMOVABLE_MANAGERS = {
    "Snap",
    "Flatpak",
    "AppImage",
    "Manual",
    "APT",
}

BLOCKED_PACKAGE_IDS = {
    "appector",
    "app-manager",
    "snapd",
    "core",
    "core18",
    "core20",
    "core22",
    "core24",
    "bare",
    "gtk-common-themes",
    "snapd-desktop-integration",
    "ubuntu-desktop",
    "ubuntu-desktop-minimal",
    "kubuntu-desktop",
    "xubuntu-desktop",
    "lubuntu-desktop",
    "ubuntu-budgie-desktop",
    "ubuntu-mate-desktop",
    "ubuntu-studio-desktop",
    "gnome-shell",
    "gnome-session",
    "gnome-session-bin",
    "gdm3",
    "lightdm",
    "sddm",
    "systemd",
    "init",
    "init-system-helpers",
    "apt",
    "dpkg",
    "bash",
    "dash",
    "coreutils",
    "procps",
    "util-linux",
    "e2fsprogs",
    "kmod",
    "module-init-tools",
    "linux-base",
    "plymouth",
    "policykit-1",
    "polkitd",
    "sudo",
    "ca-certificates",
    "ubuntu-keyring",
    "network-manager",
    "xorg",
    "xserver-xorg-core",
}

BLOCKED_PACKAGE_IDS_LOWER = {
    package.lower()
    for package in BLOCKED_PACKAGE_IDS
}

FLATPAK_BLOCKED_PREFIXES = (
    "org.freedesktop.platform",
    "org.gnome.platform",
    "org.kde.platform",
    "org.electronjs.electron",
    "org.qtproject.qt",
    "com.github.libui",
)

APT_CRITICAL_PACKAGES = {
    "appector",
    "app-manager",
    "apt",
    "bash",
    "ca-certificates",
    "coreutils",
    "dash",
    "dpkg",
    "e2fsprogs",
    "gdm3",
    "gnome-shell",
    "gnome-session",
    "gnome-session-bin",
    "grub-common",
    "grub-pc",
    "grub-efi-amd64",
    "grub-efi-amd64-signed",
    "init",
    "init-system-helpers",
    "kmod",
    "lightdm",
    "linux-base",
    "linux-firmware",
    "linux-image-generic",
    "linux-generic",
    "linux-generic-hwe-22.04",
    "linux-generic-hwe-24.04",
    "module-init-tools",
    "network-manager",
    "plymouth",
    "policykit-1",
    "polkitd",
    "procps",
    "sddm",
    "snapd",
    "sudo",
    "systemd",
    "systemd-sysv",
    "ubuntu-desktop",
    "ubuntu-desktop-minimal",
    "ubuntu-keyring",
    "ubuntu-drivers-common",
    "util-linux",
    "xorg",
    "xserver-xorg-core",
    "xserver-xorg",
    "mutter",
    "kwin-wayland",
    "kwin-x11",
}

APT_CRITICAL_PACKAGES_LOWER = {
    package.lower()
    for package in APT_CRITICAL_PACKAGES
}

APT_CRITICAL_PREFIXES = (
    "grub",
    "gnome-shell",
    "gnome-session",
    "linux-generic",
    "linux-headers",
    "linux-image",
    "linux-lowlatency",
    "linux-modules",
    "linux-firmware",
    "libsystemd",
    "systemd",
    "ubuntu-desktop",
    "xorg",
    "xserver",
    "init",
    "initramfs",
)

APT_CRITICAL_PREFIXES_LOWER = tuple(
    prefix.lower()
    for prefix in APT_CRITICAL_PREFIXES
)

APT_HIGH_RISK_SUBSTRINGS = (
    "desktop",
    "session",
    "shell",
    "display-manager",
    "login",
    "gdm",
    "lightdm",
    "sddm",
    "xorg",
    "wayland",
    "gnome",
    "kde",
    "xfce",
    "cinnamon",
    "mate",
    "budgie",
    "kernel",
    "firmware",
    "bootloader",
    "grub",
)

APT_MAX_REMOVALS = 100

SAFE_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]*$")


def app_key(app) -> str:
    manager = str(getattr(app, "manager", "") or "").strip()
    package_id = str(getattr(app, "package_id", "") or "").strip()

    return f"{manager}::{package_id}"


def _log_action(message: str):
    log_dir = Path.home() / ".local" / "state" / "app-manager"
    log_file = log_dir / "actions.log"
    descriptor = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        home_dir = Path.home().resolve()
        if log_dir.is_symlink() or not log_dir.resolve().is_relative_to(home_dir):
            raise OSError("Action log directory is not a private directory in the home folder.")
        os.chmod(log_dir, 0o700)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(log_file, flags, 0o600)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("Action log path is not a regular file.")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as f:
            descriptor = None
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except OSError:
        logging.getLogger(__name__).warning("Appector could not write its private action log.")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _normalize_package_id(name: str) -> str:
    return str(name or "").strip().split(":")[0].lower()


def _base_package_name(name: str) -> str:
    return str(name or "").split(":", 1)[0]


def _is_safe_package_id(name: str) -> bool:
    name = str(name or "").strip()

    if not name:
        return False

    return bool(SAFE_PACKAGE_ID_RE.match(name))


def is_critical_apt_package_name(name: str) -> bool:
    base = _base_package_name(name).lower()

    if base in APT_CRITICAL_PACKAGES_LOWER:
        return True

    for prefix in APT_CRITICAL_PREFIXES_LOWER:
        if base.startswith(prefix):
            return True

    return False

def install_flatpak(app_id: str, user_install: bool = True):
    """
    Installs a Flatpak app by searching configured remotes.
    """
    app_id = app_id.strip()

    if not app_id:
        return False, "Flatpak app ID is empty."

    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", app_id):
        return False, "Invalid Flatpak app ID.\n\nExample: org.gimp.GIMP"

    flatpak_command = shutil.which("flatpak")

    if not flatpak_command:
        return False, "flatpak command not found."

    scope = "--user" if user_install else "--system"

    # IMPORTANT: We intentionally omit the remote name (like "flathub").
    # If the remote isn't configured for the specific scope, Flatpak treats 
    # the remote name as an app ID and fails. By omitting it, Flatpak 
    # automatically searches all configured remotes for the app_id.
    cmd = [
        flatpak_command,
        "install",
        "-y",
        scope,
        app_id,
    ]

    # System installations require privilege escalation
    if not user_install:
        pkexec_command = shutil.which("pkexec")
        if pkexec_command:
            cmd = [
                pkexec_command,
                flatpak_command,
                "install",
                "-y",
                scope,
                app_id,
            ]

    _log_action(f"FLATPAK_INSTALL_START cmd={shlex.join(cmd)}")

    success, message = _run_command(cmd)

    if success:
        _log_action(f"FLATPAK_INSTALL_SUCCESS app_id={app_id}")
    else:
        # Provide a helpful hint if it fails due to missing remote/app
        if "No refs found" in message or "No remote refs" in message:
            scope_name = "user" if user_install else "system"
            message += (
                f"\n\nHint: The app was not found in your configured Flatpak remotes "
                f"for the {scope_name} scope.\n\n"
                "If you haven't added the Flathub repository yet, you can add it via terminal:\n"
                "flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo"
            )
        _log_action(f"FLATPAK_INSTALL_FAILED app_id={app_id} error={message}")

    return success, message

def is_blocked(app) -> bool:
    manager = str(getattr(app, "manager", "") or "").strip()
    package_id = str(getattr(app, "package_id", "") or "").strip()

    if not package_id:
        return True

    normalized = _normalize_package_id(package_id)

    if normalized in BLOCKED_PACKAGE_IDS_LOWER:
        return True

    if manager == "Snap":
        if normalized.startswith("core"):
            return True

    if manager == "Flatpak":
        for prefix in FLATPAK_BLOCKED_PREFIXES:
            if normalized.startswith(prefix.lower()):
                return True

    if manager == "APT":
        if is_critical_apt_package_name(package_id):
            return True

    return False


def can_remove(app) -> bool:
    """
    Used for batch removal.
    """
    manager = str(getattr(app, "manager", "") or "").strip()

    return manager in BATCH_REMOVABLE_MANAGERS and not is_blocked(app)


def can_remove_single(app) -> bool:
    """
    Used for single-app removal from the details window.
    """
    manager = str(getattr(app, "manager", "") or "").strip()

    return manager in SINGLE_REMOVABLE_MANAGERS and not is_blocked(app)

def get_removal_risk(app):
    """
    Returns:

        risk_level, reason

    risk_level:
        "low"
        "normal"
        "medium"
        "high"
        "blocked"
    """

    if is_blocked(app):
        return "blocked", "Blocked by safety policy."

    manager = str(getattr(app, "manager", "") or "").strip()
    package_id = str(getattr(app, "package_id", "") or "").strip()
    normalized = _normalize_package_id(package_id)

    if manager == "Leftover":
        return "low", "Leftover configuration cleanup."

    if manager == "AppImage":
        if getattr(app, "removal_paths", None):
            return "low", "Removes the launcher and managed AppImage copy; app data is kept."
        return "low", "Removes a standalone AppImage file."

    if manager == "Manual":
        return "normal", "Removes the detected launcher and executable files only; app data is kept."

    if manager == "Flatpak":
        return "low", "Removes a Flatpak application."

    if manager == "Snap":
        if normalized.startswith("core"):
            return "blocked", "Snap runtime/base snaps are blocked."
        return "normal", ""

    if manager == "APT":
        if is_critical_apt_package_name(package_id):
            return "blocked", "Critical APT package."

        for substring in APT_HIGH_RISK_SUBSTRINGS:
            if substring in normalized:
                return (
                    "high",
                    "This package may affect your desktop session or core system.",
                )

        if normalized.startswith("lib"):
            return (
                "medium",
                "Shared library removal may affect other applications.",
            )

        return "normal", ""

    return "normal", ""

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
            try:
                result = subprocess.run(
                    [
                        "dpkg-query",
                        "-W",
                        "-f=${Conffiles}\n",
                        package_id,
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if result.returncode == 0:
                    residual_conf_files.extend(
                        match.group(1)
                        for match in re.finditer(
                            r"(?m)^\s*(/\S+)\s+[0-9a-f]{32}\s*$",
                            result.stdout,
                        )
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

    apt_ids = list(dict.fromkeys(package_id for package_id in apt_ids if package_id))
    if apt_ids:
        command = [
            "dpkg-query",
            "-W",
            "-f=${binary:Package}\t${Installed-Size}\n",
            *apt_ids,
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
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

def _run_command_raw(cmd, timeout=900, env_overrides=None):
    uses_apt = _command_uses_apt(cmd)
    if uses_apt and not APT_COMMAND_LOCK.acquire(blocking=False):
        return 1, (
            "Another APT/dpkg operation is already running in Appector. "
            "Wait for it to finish, then try again."
        )

    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["PAGER"] = "cat"
    if env_overrides:
        env.update(env_overrides)

    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        stdout = _strip_ansi(result.stdout).strip()
        stderr = _strip_ansi(result.stderr).strip()

        if stdout and stderr:
            output = f"{stdout}\n{stderr}"
        else:
            output = stdout if stdout else stderr

        if result.returncode:
            output = _format_apt_lock_error(output)
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return 124, "Command timed out."
    except Exception as e:
        return 1, str(e)
    finally:
        if uses_apt:
            APT_COMMAND_LOCK.release()

def _run_command(cmd):
    returncode, output = _run_command_raw(cmd)

    if returncode == 0:
        return True, output if output else "Command completed successfully."

    return False, output if output else "Command failed."

def _run_command_stream(cmd, output_callback=None, timeout=900):
    """
    Runs a command and streams output line-by-line.
    Prevents hanging by closing stdin and forces non-interactive mode for APT/dpkg.
    """
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["PAGER"] = "cat"
    uses_apt = _command_uses_apt(cmd)
    if uses_apt and not APT_COMMAND_LOCK.acquire(blocking=False):
        return False, (
            "Another APT/dpkg operation is already running in Appector. "
            "Wait for it to finish, then try again."
        )

    process = None
    timer = None
    timed_out = threading.Event()

    try:
        command = list(cmd)
        if (
            timeout
            and command
            and Path(command[0]).name == "pkexec"
        ):
            timeout_command = shutil.which("timeout")
            if timeout_command:
                command = [
                    command[0],
                    timeout_command,
                    "--signal=TERM",
                    "--kill-after=10s",
                    str(timeout),
                    *command[1:],
                ]

        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,  # Prevents scripts from hanging waiting for input
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        if timeout:
            def terminate_process():
                if process.poll() is None:
                    timed_out.set()
                    try:
                        process.kill()
                    except (PermissionError, ProcessLookupError, OSError):
                        pass

            timer = threading.Timer(timeout, terminate_process)
            timer.start()

        lines = []

        if process.stdout:
            for line in process.stdout:
                # Strip weird terminal escape codes
                clean_line = _strip_ansi(line).rstrip()
                
                if output_callback:
                    try:
                        output_callback(clean_line)
                    except Exception:
                        pass

                lines.append(clean_line)

        process.wait()

        returncode = process.returncode
        output = "\n".join(lines).strip()

        if timed_out.is_set() or returncode == 124:
            return False, output if output else "Command timed out."

        if returncode == 0:
            return True, output if output else "Command completed successfully."

        output = output or f"Command failed with exit code {returncode}."
        return False, _format_apt_lock_error(output)

    except Exception as e:
        return False, str(e)
    finally:
        if timer:
            timer.cancel()
            if timer.is_alive():
                timer.join(timeout=1)
        if uses_apt:
            APT_COMMAND_LOCK.release()
        
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


def install_appimage(path, delete_source=False):
    """Copy an AppImage into the per-user app directory and add a launcher."""
    source = Path(path).expanduser()
    if source.suffix.lower() != ".appimage":
        return False, f"Not an AppImage file: {source}"
    if not source.is_file():
        return False, f"AppImage file not found: {source}"

    try:
        with source.open("rb") as appimage_file:
            if appimage_file.read(4) != b"\x7fELF":
                return False, f"File is not a valid ELF AppImage: {source.name}"
    except OSError as error:
        return False, f"Could not read AppImage {source}: {error}"

    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", source.stem).strip("-") or "appimage"
    app_dir = Path.home() / ".local/share/app-manager/appimages"
    desktop_dir = Path.home() / ".local/share/applications"
    try:
        resolved_source = source.resolve(strict=True)
        if resolved_source.is_relative_to(app_dir.resolve()):
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

    digest = hashlib.sha256(str(resolved_source).encode("utf-8")).hexdigest()[:10]
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
        shutil.copy2(source, installed_image)
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
        desktop_file.write_text(desktop_content, encoding="utf-8")
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
        "Already installed ({}): ".format(len(already_installed))
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
    if apt_command:
        returncode, output = _run_command_raw(
            [apt_command, "list", "--upgradable"],
            timeout=90,
            env_overrides={"LC_ALL": "C"},
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
    return successful_checks > 0, {
        "updates": updates,
        "checks": checks,
        "errors": errors,
        "notes": notes,
    }


def execute_available_updates(updates, output_callback=None):
    """Install selected updates using each package manager's native updater."""
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
            cmd = [command, "install", "--only-upgrade", "-y", *package_ids]
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
            cmd = [command, "refresh", *package_ids]

        if (manager == "APT" or manager == "Snap" or scope == "system") and os.geteuid() != 0:
            pkexec_command = shutil.which("pkexec")
            if not pkexec_command:
                failures.append(
                    f"{manager}: pkexec is required to update system packages."
                )
                continue
            cmd.insert(0, pkexec_command)

        title = f"{manager}{f' ({scope})' if scope else ''}"
        if output_callback:
            output_callback(f"Updating {title}: {', '.join(package_ids)}")
        success, output = _run_command_stream(cmd, output_callback)
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
    ):
        self._temporary_directory = temporary_directory
        self.original_paths = original_paths
        self.staged_paths = staged_paths
        self.hashes = hashes
        self.simulation = simulation
        self.summary = summary
        self._closed = False

    def close(self):
        if self._closed:
            return
        self._temporary_directory.cleanup()
        self._closed = True

    def trash_unchanged_sources(self):
        results = []
        for path, expected_hash in zip(self.original_paths, self.hashes):
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
        )
    except OSError as error:
        return None, f"Could not create private package-review storage: {error}"
    staged_paths = []
    hashes = []
    package_summaries = []

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
    for path, expected_hash in zip(staged_paths, review.hashes):
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

    pkexec_command = shutil.which("pkexec")
    if os.geteuid() != 0 and not pkexec_command:
        return False, (
            "pkexec is required to authorize a system-wide Debian package "
            "installation. Appector itself must not be run as root."
        )

    cmd = [
        apt_get_command,
        "install",
        "-y",
    ] + [str(p) for p in deb_paths]

    if pkexec_command:
        cmd = [pkexec_command] + cmd

    _log_action(f"DEB_BATCH_INSTALL_START cmd={shlex.join(cmd)}")

    success, message = _run_command_stream(cmd, output_callback)

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
            allowed_roots = [
                Path.home() / ".local/share/app-manager/appimages",
                Path.home() / ".local/share/applications",
            ]
            validated_paths = []
            for raw_path in removal_paths:
                path = Path(os.path.abspath(Path(raw_path).expanduser()))
                if (
                    not path.is_file()
                    or not any(path.is_relative_to(root) for root in allowed_roots)
                ):
                    message = f"Refusing to remove an invalid AppImage installation file: {raw_path}"
                    _log_action(f"REMOVE_BLOCKED {key} manager=AppImage reason=invalid-managed-path")
                    return False, message
                if path.parent == allowed_roots[1] and (
                    not path.name.startswith("app-manager-appimage-")
                    or path.suffix != ".desktop"
                ):
                    message = f"Refusing to remove an unowned desktop entry: {raw_path}"
                    _log_action(f"REMOVE_BLOCKED {key} manager=AppImage reason=unowned-desktop-file")
                    return False, message
                validated_paths.append(path)

            desktop_files = [
                path for path in validated_paths
                if path.parent == allowed_roots[1]
            ]
            if len(desktop_files) != 1:
                message = "The integrated AppImage launcher is missing or ambiguous."
                _log_action(f"REMOVE_BLOCKED {key} manager=AppImage reason=invalid-desktop-count")
                return False, message
            try:
                desktop_content = desktop_files[0].read_text(encoding="utf-8")
            except OSError as error:
                return False, f"Could not verify the AppImage launcher: {error}"
            if (
                "X-AppManager-Installed-AppImage=true" not in desktop_content
                or f"X-AppManager-AppImage-Path={package_id}" not in desktop_content
            ):
                message = "The launcher does not identify this as an Appector installation."
                _log_action(f"REMOVE_BLOCKED {key} manager=AppImage reason=unverified-desktop")
                return False, message

            if not any(
                path.parent == allowed_roots[0] and path == Path(package_id)
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

        path = Path(package_id)

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
            Path.home() / ".opencode/bin",
            Path.home() / "bin",
            Path.home() / "Applications",
            Path("/usr/local/bin"),
            Path("/opt"),
        ]
        validated_paths = []
        for value in paths:
            path = Path(os.path.abspath(Path(value).expanduser()))
            if not path.is_absolute() or not path.is_file():
                message = f"Manual app file no longer exists or is not a regular file: {value}"
                _log_action(f"REMOVE_FAILED {key} manager=Manual reason=invalid-path path={value}")
                return False, message
            if not any(path.is_relative_to(root) for root in allowed_roots):
                message = f"Refusing to remove a file outside known manual-install locations: {value}"
                _log_action(f"REMOVE_BLOCKED {key} manager=Manual reason=outside-allowed-path")
                return False, message
            validated_paths.append(str(path))

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
    ] + list(package_ids)

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


def _parse_apt_simulation_removed_packages(output: str):
    removed = []

    for line in output.splitlines():
        if line.startswith(("Remv ", "Purg ")):
            parts = line.split()

            if len(parts) >= 2:
                package_name = _base_package_name(parts[1])
                removed.append(package_name)

    seen = set()
    unique_removed = []

    for package_name in removed:
        if package_name not in seen:
            seen.add(package_name)
            unique_removed.append(package_name)

    return unique_removed


# ------------------------------------------------------------
# Single APT removal
# ------------------------------------------------------------

def prepare_apt_removal(app, purge=False):
    manager = getattr(app, "manager", "")
    package_id = getattr(app, "package_id", "")

    if manager != "APT":
        return False, "This is not an APT package.", [], ""

    if not can_remove_single(app):
        return False, "This APT package is blocked by safety policy.", [], ""

    if not package_id:
        return False, "APT package ID is missing.", [], ""

    success, output, removed_packages = simulate_apt_remove_multiple([package_id], purge=purge)

    if not success:
        message = (
            "APT simulation failed.\n\n"
            f"{output}"
        )
        return False, message, removed_packages, ""

    removed_base_names = {_base_package_name(p) for p in removed_packages}
    target_name = _base_package_name(package_id)

    if target_name not in removed_base_names:
        message = (
            f"APT simulation did not confirm removal of '{package_id}'.\n\n"
            "It may already be removed, held, or not removable by APT.\n\n"
            f"{output}"
        )
        return False, message, removed_packages, ""

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
        return False, message, removed_packages, ""

    if len(removed_packages) > APT_MAX_REMOVALS:
        message = (
            "Blocked by safety policy.\n\n"
            f"APT simulation says {len(removed_packages)} packages would be removed.\n"
            f"The current safety limit is {APT_MAX_REMOVALS} packages.\n\n"
            "Please review this manually in a terminal."
        )
        return False, message, removed_packages, ""

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

    return True, message, removed_packages, "\n".join(warnings)


def execute_apt_removal(app, purge=False):
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

    removed_base_names = {_base_package_name(p) for p in removed_packages}
    target_name = _base_package_name(package_id)

    if target_name not in removed_base_names:
        message = (
            f"APT simulation did not confirm removal of '{package_id}'.\n\n"
            f"{sim_output}"
        )
        _log_action(f"APT_REMOVE_BLOCKED {key} reason=target-not-in-simulation")
        return False, message

    critical_removed = [
        p
        for p in removed_packages
        if is_critical_apt_package_name(p)
    ]

    if critical_removed:
        message = (
            "Blocked by safety policy.\n\n"
            "APT simulation says the following critical packages would be removed:\n\n"
            f"{_format_package_list(critical_removed)}"
        )
        _log_action(f"APT_REMOVE_BLOCKED {key} reason=critical-packages")
        return False, message

    if len(removed_packages) > APT_MAX_REMOVALS:
        message = (
            "Blocked by safety policy.\n\n"
            f"APT simulation says {len(removed_packages)} packages would be removed.\n"
            f"The current safety limit is {APT_MAX_REMOVALS} packages."
        )
        _log_action(f"APT_REMOVE_BLOCKED {key} reason=too-many-packages")
        return False, message

    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"
    pkexec_command = shutil.which("pkexec")
    backup_note = ""
    if purge:
        try:
            backup_dir, backed_up_count = _backup_residual_conffiles(
                removed_packages
            )
        except OSError as error:
            _log_action(f"APT_REMOVE_BLOCKED {key} reason=purge-backup-failed")
            return False, (
                "Purge was not started because Appector could not securely back "
                f"up the affected configuration files.\n\n{error}"
            )
        backup_note = _purge_backup_note(backup_dir, backed_up_count)

    apt_action = "purge" if purge else "remove"

    if pkexec_command:
        cmd = [
            pkexec_command,
            apt_get_command,
            apt_action,
            "-y",
            package_id,
        ]
    else:
        cmd = [
            apt_get_command,
            apt_action,
            "-y",
            package_id,
        ]

    _log_action(f"APT_REMOVE_START {key} cmd={shlex.join(cmd)}")

    success, message = _run_command(cmd)

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

def execute_batch_removal(apps, progress_callback=None, purge=False):

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
            details = getattr(app, "details", "") or ""
            if "system" in details.lower():
                system_flatpaks.append(app)
            else:
                user_flatpaks.append(app)
        elif manager == "APT":
            apts.append(app)

    total_steps = (
        len(manual_apps)
        + len(appimages)
        + (1 if user_flatpaks else 0)
        + (1 if apts else 0)
        + (1 if (snaps or system_flatpaks or apts) else 0)
    )

    current_step = 0

    def report(message):
        nonlocal current_step

        current_step += 1

        if progress_callback:
            progress_callback(current_step, total_steps, message)

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
    # User Flatpak removals
    # ------------------------------------------------------------
    if user_flatpaks:
        report(f"Removing {len(user_flatpaks)} Flatpak app(s)…")

        script_results, output = _execute_user_flatpak_batch(user_flatpaks)

        for app in user_flatpaks:
            package_id = getattr(app, "package_id", "") or ""
            key = ("Flatpak", package_id)

            success, message = script_results.get(
                key,
                (False, output if output else "Flatpak removal failed."),
            )

            results_by_key[app_key(app)] = (success, message)

    # ------------------------------------------------------------
    # APT validation/simulation
    # ------------------------------------------------------------
    apt_valid_apps = []
    apt_ids = []

    if apts:
        report("Checking APT dependencies…")

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
                removed_base_names = {
                    _base_package_name(p)
                    for p in removed_packages
                }

                missing = [
                    p
                    for p in apt_ids
                    if _base_package_name(p) not in removed_base_names
                ]

                critical_removed = [
                    p
                    for p in removed_packages
                    if is_critical_apt_package_name(p)
                ]

                if missing:
                    message = (
                        "APT simulation did not confirm removal for:\n\n"
                        f"{_format_package_list(missing)}\n\n"
                        f"{sim_output}"
                    )

                    for app in apt_valid_apps:
                        results_by_key[app_key(app)] = (False, message)

                    apt_valid_apps = []
                    apt_ids = []

                elif critical_removed:
                    message = (
                        "Blocked by safety policy.\n\n"
                        "APT simulation says the following critical packages would be removed:\n\n"
                        f"{_format_package_list(critical_removed)}"
                    )

                    for app in apt_valid_apps:
                        results_by_key[app_key(app)] = (False, message)

                    apt_valid_apps = []
                    apt_ids = []

                elif len(removed_packages) > APT_MAX_REMOVALS:
                    message = (
                        "Blocked by safety policy.\n\n"
                        f"APT simulation says {len(removed_packages)} packages would be removed.\n"
                        f"The current safety limit is {APT_MAX_REMOVALS} packages."
                    )

                    for app in apt_valid_apps:
                        results_by_key[app_key(app)] = (False, message)

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
        report("Removing apps with administrator privileges…")

        backup_note = ""
        if purge and apt_ids:
            try:
                backup_dir, backed_up_count = _backup_residual_conffiles(
                    removed_packages
                )
                backup_note = _purge_backup_note(backup_dir, backed_up_count)
            except OSError as error:
                message = (
                    "Privileged removal was not started because Appector could "
                    "not securely back up the APT configuration files.\n\n"
                    f"{error}"
                )
                for app in privileged_apps:
                    results_by_key[app_key(app)] = (False, message)
                privileged_apps = []
                privileged_expected = []
                apt_ids = []
                snap_ids = []
                system_flatpak_ids = []

        script = _build_privileged_script(
            snap_exe,
            snap_ids,
            flatpak_exe,
            system_flatpak_ids,
            apt_get_exe,
            apt_ids,
            purge=purge,
        )

        if not script:
            for app in privileged_apps:
                results_by_key[app_key(app)] = (
                    False,
                    "Could not build privileged removal script.",
                )
        else:
            script_results, output = _execute_privileged_script(
                script,
                privileged_expected,
            )

            for app in privileged_apps:
                manager = getattr(app, "manager", "")
                package_id = getattr(app, "package_id", "") or ""

                key = (manager, package_id)

                success, message = script_results.get(
                    key,
                    (False, output if output else "Privileged removal failed."),
                )
                if backup_note:
                    message = f"{message}\n\n{backup_note}"

                results_by_key[app_key(app)] = (success, message)

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
# Flatpak user batch
# ------------------------------------------------------------

def _execute_user_flatpak_batch(apps):
    flatpak_exe = shutil.which("flatpak")

    expected = []
    package_ids = []

    invalid_results = {}

    for app in apps:
        package_id = getattr(app, "package_id", "") or ""

        if not _is_safe_package_id(package_id):
            invalid_results[("Flatpak", package_id)] = (
                False,
                "Unsafe or invalid Flatpak package ID.",
            )
            continue

        package_ids.append(package_id)
        expected.append(("Flatpak", package_id))

    if not flatpak_exe:
        results = dict(invalid_results)

        for key in expected:
            results[key] = (False, "flatpak command not found.")

        return results, "flatpak command not found."

    lines = []

    if not _append_loop_commands(
        lines,
        "Flatpak",
        flatpak_exe,
        ["uninstall", "-y"],
        package_ids,
    ):
        results = dict(invalid_results)

        for key in expected:
            results[key] = (False, "Could not build Flatpak removal script.")

        return results, "Could not build Flatpak removal script."

    script = "\n".join(lines)

    sh_exe = shutil.which("sh") or "/bin/sh"
    cmd = [sh_exe, "-c", script]

    returncode, output = _run_command_raw(cmd)

    results = _parse_script_results(output, expected)

    if returncode != 0:
        for key in expected:
            success, message = results.get(key, (False, "No result captured."))

            if not success and message == "No result captured.":
                results[key] = (False, output if output else "Flatpak removal failed.")

    results.update(invalid_results)

    return results, output


# ------------------------------------------------------------
# Flatpak remote / Flathub helpers
# ------------------------------------------------------------

FLATHUB_REMOTE_NAME = "flathub"
FLATHUB_REMOTE_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"


def get_flatpak_remotes(user_install: bool = True):
    """
    Returns a list of configured Flatpak remote names.
    """

    flatpak_command = shutil.which("flatpak")

    if not flatpak_command:
        return []

    scope = "--user" if user_install else "--system"

    cmd = [
        flatpak_command,
        "remotes",
        scope,
        "--columns=name",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            return []

        return [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        ]
    except Exception:
        return []


def flathub_remote_exists(user_install: bool = True) -> bool:
    """
    Checks whether the Flathub remote exists in the selected scope.
    """

    try:
        remotes = get_flatpak_remotes(user_install)
        return FLATHUB_REMOTE_NAME in remotes
    except Exception:
        return False


def add_flathub_remote(user_install: bool = True):
    """
    Adds the Flathub remote.

    For user installation:
        flatpak remote-add --user --if-not-exists flathub <url>

    For system installation:
        pkexec flatpak remote-add --system --if-not-exists flathub <url>
    """

    flatpak_command = shutil.which("flatpak")

    if not flatpak_command:
        return False, "flatpak command not found."

    scope = "--user" if user_install else "--system"

    base_cmd = [
        flatpak_command,
        "remote-add",
        scope,
        "--if-not-exists",
        FLATHUB_REMOTE_NAME,
        FLATHUB_REMOTE_URL,
    ]

    if user_install:
        cmd = base_cmd
    else:
        pkexec_command = shutil.which("pkexec")

        if pkexec_command:
            cmd = [pkexec_command] + base_cmd
        else:
            cmd = base_cmd

    _log_action(f"FLATHUB_ADD_START cmd={shlex.join(cmd)}")

    success, message = _run_command(cmd)

    if success:
        _log_action(f"FLATHUB_ADD_SUCCESS scope={scope}")
    else:
        _log_action(f"FLATHUB_ADD_FAILED scope={scope} error={message}")

    return success, message


# ------------------------------------------------------------
# Flatpak installation helpers
# ------------------------------------------------------------

def _flatpak_install_command(target: str, user_install: bool = True):
    """
    Builds a Flatpak install command for a target.

    Target can be:
    - app ID
    - local .flatpakref path
    - remote .flatpakref URL
    """

    flatpak_command = shutil.which("flatpak")

    if not flatpak_command:
        return None, "flatpak command not found."

    scope = "--user" if user_install else "--system"

    cmd = [
        flatpak_command,
        "install",
        "-y",
        scope,
        target,
    ]

    if not user_install:
        pkexec_command = shutil.which("pkexec")

        if pkexec_command:
            cmd = [pkexec_command] + cmd

    return cmd, None


def install_flatpak_app_id(app_id: str, user_install: bool = True, output_callback=None):
    """
    Installs a Flatpak app by app ID.
    """

    app_id = app_id.strip()

    if not app_id:
        return False, "Flatpak app ID is empty."

    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", app_id):
        return False, "Invalid Flatpak app ID.\n\nExample: org.gimp.GIMP"

    cmd, error = _flatpak_install_command(app_id, user_install)

    if error:
        return False, error

    _log_action(f"FLATPAK_INSTALL_START cmd={shlex.join(cmd)}")

    def filtered_output(line):
        if output_callback and "already installed" not in line.lower():
            output_callback(line)

    success, message = _run_command_stream(cmd, filtered_output if output_callback else None)

    if not success and _flatpak_output_is_already_installed(message):
        _log_action(f"FLATPAK_INSTALL_ALREADY_INSTALLED app_id={app_id}")
        return True, f"Already installed: {app_id}"

    if success:
        _log_action(f"FLATPAK_INSTALL_SUCCESS app_id={app_id}")
    else:
        if (
            "No refs found" in message
            or "No remote refs" in message
            or "Unable to find" in message
        ):
            message += (
                "\n\nHint: The app was not found in your configured Flatpak remotes.\n"
                "If you are installing from Flathub, add the Flathub remote first."
            )

        _log_action(f"FLATPAK_INSTALL_FAILED app_id={app_id} error={message}")

    return success, message


def _flatpak_output_is_already_installed(message: str):
    return "already installed" in (message or "").lower()


def parse_flatpak_app_inputs(text: str):
    """Validate and deduplicate newline-separated Flatpak IDs or Flathub URLs."""
    app_ids = []
    issues = []
    seen = set()

    for line_number, raw_value in enumerate(text.splitlines(), start=1):
        value = raw_value.strip()

        if not value:
            continue

        app_id = extract_flathub_app_id(value) or value

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", app_id):
            issues.append(f"Line {line_number}: invalid app ID or Flathub URL.")
            continue

        if app_id in seen:
            issues.append(f"Line {line_number}: duplicate app ignored ({app_id}).")
            continue

        seen.add(app_id)
        app_ids.append(app_id)

    return app_ids, issues


def install_flatpak_app_id_batch(app_ids, user_install: bool = True, output_callback=None):
    """Install a sequence of validated Flatpak app IDs, continuing after failures."""
    if not app_ids:
        return True, "Installed (0): none\nAlready installed (0): none\nFailed (0): none", 0

    installed = []
    already_installed = []
    failures = []
    success_count = 0
    total = len(app_ids)

    for index, app_id in enumerate(app_ids, start=1):
        if output_callback:
            output_callback(f"[{index}/{total}] Installing {app_id}…")

        success, message = install_flatpak_app_id(
            app_id,
            user_install,
            output_callback,
        )

        if success:
            success_count += 1
            if _flatpak_output_is_already_installed(message):
                already_installed.append(app_id)
                result = "ALREADY INSTALLED"
            else:
                installed.append(app_id)
                result = "INSTALLED"
        else:
            message_lines = (message or "").splitlines()
            first_line = message_lines[0] if message_lines else "Installation failed"
            failures.append(f"{app_id}: {first_line}")
            result = "FAILED"

        if output_callback:
            output_callback(f"[{index}/{total}] {result} {app_id}")

    summary = _format_flatpak_batch_summary(
        installed,
        already_installed,
        failures,
    )

    return not failures, summary, success_count


def _format_flatpak_batch_summary(installed, already_installed, failures):
    def format_section(title, entries):
        if not entries:
            return f"{title} (0): none"
        return f"{title} ({len(entries)}):\n" + "\n".join(
            f"  {entry}" for entry in entries[:20]
        )

    sections = [
        format_section("Installed", installed),
        format_section("Already installed", already_installed),
        format_section("Failed", failures),
    ]

    if len(installed) > 20 or len(already_installed) > 20 or len(failures) > 20:
        sections.append("Lists are limited to the first 20 entries per section.")

    return "\n\n".join(sections)


def extract_flathub_app_id(value: str):
    """Return the app ID from a Flathub app page URL, if value is one."""
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or hostname is None
        or hostname.lower() not in {"flathub.org", "www.flathub.org"}
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return None

    segments = [segment for segment in parsed.path.split("/") if segment]

    if (
        len(segments) == 3
        and re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", segments[0])
        and segments[1] == "apps"
    ):
        app_id = segments[2]
    elif len(segments) == 2 and segments[0] == "apps":
        app_id = segments[1]
    else:
        return None

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", app_id):
        return None

    return app_id


def _parse_flatpakref_url(path: Path):
    """
    Parses a .flatpakref file and returns the Url= value if present.
    """

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()

                if line.lower().startswith("url="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass

    return None


def _install_flatpak_location(location: str, user_install: bool = True, output_callback=None):
    """
    Installs from a location.

    Location can be:
    - local .flatpakref path
    - remote .flatpakref URL
    """

    cmd, error = _flatpak_install_command(location, user_install)

    if error:
        return False, error

    _log_action(f"FLATPAK_INSTALL_LOCATION_START cmd={shlex.join(cmd)}")

    success, message = _run_command_stream(cmd, output_callback)

    if success:
        _log_action(f"FLATPAK_INSTALL_LOCATION_SUCCESS location={location}")
    else:
        _log_action(f"FLATPAK_INSTALL_LOCATION_FAILED location={location} error={message}")

    return success, message


def install_flatpak_ref_file(
    path: str,
    user_install: bool = True,
    output_callback=None,
    delete_source: bool = False,
):
    """
    Installs a local .flatpakref file.

    If direct installation fails, this also tries to parse the Url=
    from the .flatpakref file and install that URL.
    """

    ref_path = Path(path).expanduser()

    if not ref_path.is_file():
        return False, "Selected .flatpakref file not found."

    def filtered_output(line):
        if output_callback and "already installed" not in line.lower():
            output_callback(line)

    success, message = _install_flatpak_location(
        str(ref_path),
        user_install,
        filtered_output if output_callback else None,
    )

    if success:
        message = _maybe_delete_source_file(ref_path, delete_source, message)
        return True, message

    if _flatpak_output_is_already_installed(message):
        message = f"Already installed: {ref_path.stem}"
        message = _maybe_delete_source_file(ref_path, delete_source, message)
        _log_action(f"FLATPAK_REF_ALREADY_INSTALLED path={ref_path}")
        return True, message

    url = _parse_flatpakref_url(ref_path)

    if url:
        url_success, url_message = _install_flatpak_location(
            url,
            user_install,
            filtered_output if output_callback else None,
        )

        if url_success:
            url_message = _maybe_delete_source_file(ref_path, delete_source, url_message)
            return True, url_message

        if _flatpak_output_is_already_installed(url_message):
            url_message = f"Already installed: {ref_path.stem}"
            url_message = _maybe_delete_source_file(ref_path, delete_source, url_message)
            _log_action(f"FLATPAK_REF_ALREADY_INSTALLED path={ref_path}")
            return True, url_message

        message += (
            "\n\nAlso tried URL from .flatpakref file:\n"
            f"{url_message}"
        )

    return False, message


def install_flatpak_source(
    source_type: str,
    value: str,
    user_install: bool = True,
    output_callback=None,
    delete_source: bool = False,
):
    """
    Unified Flatpak installation entry point.

    source_type:
        "id"  -> Flathub/Flatpak app ID
        "ref" -> .flatpakref file or URL
    """

    value = value.strip()

    if not value:
        return False, "No Flatpak source provided."

    flathub_app_id = extract_flathub_app_id(value)

    if source_type == "id":
        return install_flatpak_app_id(
            flathub_app_id or value,
            user_install,
            output_callback,
        )

    if source_type == "ref":
        if flathub_app_id:
            return install_flatpak_app_id(
                flathub_app_id,
                user_install,
                output_callback,
            )

        if value.startswith("http://") or value.startswith("https://"):
            return _install_flatpak_location(
                value,
                user_install,
                output_callback,
            )

        return install_flatpak_ref_file(
            value,
            user_install,
            output_callback,
            delete_source,
        )

    return False, "Unknown Flatpak source type."

# ------------------------------------------------------------
# Privileged grouped script
# ------------------------------------------------------------

def _build_privileged_script(
    snap_exe,
    snap_ids,
    flatpak_exe,
    system_flatpak_ids,
    apt_get_exe,
    apt_ids,
    purge=False,
):
    lines = []

    if snap_ids:
        if not snap_exe:
            return None

        if not _append_loop_commands(
            lines,
            "Snap",
            snap_exe,
            ["remove"],
            snap_ids,
        ):
            return None

    if system_flatpak_ids:
        if not flatpak_exe:
            return None

        if not _append_loop_commands(
            lines,
            "Flatpak",
            flatpak_exe,
            ["uninstall", "-y"],
            system_flatpak_ids,
        ):
            return None

    if apt_ids:
        if not apt_get_exe:
            return None

        for package_id in apt_ids:
            if not _is_safe_package_id(package_id):
                return None

            lines.append(f"echo APP_MANAGER_START APT {package_id}")

        quoted_packages = " ".join(
            shlex.quote(package_id)
            for package_id in apt_ids
        )

        apt_action = "purge" if purge else "remove"

        apt_command = " ".join(
            [
                shlex.quote(apt_get_exe),
                apt_action,
                "-y",
                quoted_packages,
            ]
        )

        lines.append(apt_command)
        lines.append("status=$?")

        for package_id in apt_ids:
            lines.append(f"echo APP_MANAGER_END APT {package_id} $status")

    if not lines:
        return None

    return "\n".join(lines)


def _execute_privileged_script(script, expected):
    sh_exe = shutil.which("sh") or "/bin/sh"
    pkexec_exe = shutil.which("pkexec")

    if pkexec_exe:
        cmd = [
            pkexec_exe,
            sh_exe,
            "-c",
            script,
        ]
    else:
        cmd = [
            sh_exe,
            "-c",
            script,
        ]

    _log_action("PRIVILEGED_BATCH_SCRIPT_START")

    returncode, output = _run_command_raw(cmd)

    results = _parse_script_results(output, expected)

    if returncode != 0:
        for key in expected:
            success, message = results.get(key, (False, "No result captured."))

            if not success and message == "No result captured.":
                results[key] = (
                    False,
                    output if output else "Privileged removal failed.",
                )

    if returncode == 0:
        _log_action("PRIVILEGED_BATCH_SCRIPT_SUCCESS")
    else:
        _log_action(f"PRIVILEGED_BATCH_SCRIPT_FAILED returncode={returncode}")

    return results, output


def _append_loop_commands(lines, manager, executable, args, package_ids):
    for package_id in package_ids:
        if not _is_safe_package_id(package_id):
            return False

        quoted_package = shlex.quote(package_id)

        command_parts = [
            shlex.quote(executable)
        ]

        for arg in args:
            command_parts.append(shlex.quote(arg))

        command_parts.append(quoted_package)

        command = " ".join(command_parts)

        lines.append(f"echo APP_MANAGER_START {manager} {package_id}")
        lines.append(command)
        lines.append("status=$?")
        lines.append(f"echo APP_MANAGER_END {manager} {package_id} $status")

    return True


def _parse_script_results(output, expected):
    statuses = {}

    for line in output.splitlines():
        if line.startswith("APP_MANAGER_END "):
            parts = line.split()

            if len(parts) >= 4:
                manager = parts[1]
                package_id = parts[2]

                try:
                    status = int(parts[3])
                except Exception:
                    status = 1

                statuses[(manager, package_id)] = status

    results = {}

    for key in expected:
        status = statuses.get(key)

        if status == 0:
            results[key] = (True, "Removed.")
        elif status is None:
            results[key] = (False, "No result captured.")
        else:
            results[key] = (False, f"Command exited with code {status}.")

    return results


def _format_package_list(packages, limit=50):
    lines = []

    for package_name in packages[:limit]:
        lines.append(f"• {package_name}")

    if len(packages) > limit:
        lines.append(f"• …and {len(packages) - limit} more")

    return "\n".join(lines)


def _write_json_private(path, data):
    encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.write(b"\n")
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _backup_residual_conffiles(package_ids):
    """Securely copy dpkg-authoritative conffiles before irreversible purge."""
    state_dir = Path.home() / ".local" / "state" / "app-manager"
    backup_root = state_dir / "purge-backups"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    home_dir = Path.home().resolve()
    if (
        state_dir.is_symlink()
        or not state_dir.resolve().is_relative_to(home_dir)
    ):
        raise OSError(f"Backup state directory is outside the home directory: {state_dir}")
    backup_root.mkdir(mode=0o700, exist_ok=True)
    if backup_root.is_symlink() or not backup_root.resolve().is_relative_to(home_dir):
        raise OSError(f"Backup directory is outside the home directory: {backup_root}")
    os.chmod(state_dir, 0o700)
    os.chmod(backup_root, 0o700)

    backup_dir = backup_root / (
        datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex
    )
    backup_dir.mkdir(mode=0o700)
    files_dir = backup_dir / "files"
    files_dir.mkdir(mode=0o700)
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(),
        "status": "incomplete",
        "packages": list(package_ids),
        "files": [],
    }
    manifest_path = backup_dir / "manifest.json"
    _write_json_private(manifest_path, manifest)
    records_by_path = {}

    try:
        for package_id in package_ids:
            result = subprocess.run(
                ["dpkg-query", "-W", "-f=${Conffiles}\\n", package_id],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise OSError(
                    result.stderr.strip()
                    or f"Could not list configuration files for {package_id}."
                )

            for line in result.stdout.splitlines():
                match = re.match(r"^\s*(/\S+)\s+([0-9a-f]{32})(?:\s+.*)?$", line)
                if not match:
                    if line.lstrip().startswith("/"):
                        raise OSError(
                            f"Could not safely parse dpkg conffile entry for "
                            f"{package_id}: {line}"
                        )
                    continue
                source_value = match.group(1)
                source_path = Path(source_value)
                if (
                    not source_path.is_absolute()
                    or ".." in source_path.parts
                    or os.path.normpath(source_value) != source_value
                    or source_path == Path("/")
                ):
                    raise OSError(
                        f"Refusing unsafe dpkg conffile path: {source_value}"
                    )
                if source_value in records_by_path:
                    if package_id not in records_by_path[source_value]["packages"]:
                        records_by_path[source_value]["packages"].append(package_id)
                    continue

                try:
                    source_stat = source_path.lstat()
                except FileNotFoundError:
                    manifest["files"].append({
                        "package": package_id,
                        "packages": [package_id],
                        "path": source_value,
                        "status": "missing",
                    })
                    records_by_path[source_value] = manifest["files"][-1]
                    continue

                destination = files_dir / source_value.lstrip("/")
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                record = {
                    "package": package_id,
                    "packages": [package_id],
                    "path": source_value,
                    "mode": stat.S_IMODE(source_stat.st_mode),
                    "uid": source_stat.st_uid,
                    "gid": source_stat.st_gid,
                    "mtime_ns": source_stat.st_mtime_ns,
                    "size": source_stat.st_size,
                }

                if stat.S_ISLNK(source_stat.st_mode):
                    link_target = os.readlink(source_path)
                    current_stat = source_path.lstat()
                    if (
                        current_stat.st_ino != source_stat.st_ino
                        or current_stat.st_dev != source_stat.st_dev
                        or not stat.S_ISLNK(current_stat.st_mode)
                    ):
                        raise OSError(
                            f"Configuration symlink changed during backup: {source_path}"
                        )
                    os.symlink(link_target, destination)
                    record.update(type="symlink", link_target=link_target)
                elif stat.S_ISREG(source_stat.st_mode):
                    flags = os.O_RDONLY
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    source_fd = os.open(source_path, flags)
                    destination_fd = None
                    try:
                        opened_stat = os.fstat(source_fd)
                        if (
                            not stat.S_ISREG(opened_stat.st_mode)
                            or opened_stat.st_ino != source_stat.st_ino
                            or opened_stat.st_dev != source_stat.st_dev
                        ):
                            raise OSError(
                                f"Configuration file changed during backup: {source_path}"
                            )
                        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        if hasattr(os, "O_NOFOLLOW"):
                            destination_flags |= os.O_NOFOLLOW
                        destination_fd = os.open(
                            destination,
                            destination_flags,
                            0o600,
                        )
                        digest = hashlib.sha256()
                        with os.fdopen(source_fd, "rb") as source:
                            source_fd = -1
                            with os.fdopen(destination_fd, "wb") as output:
                                destination_fd = None
                                while True:
                                    chunk = source.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    digest.update(chunk)
                                    output.write(chunk)
                        final_stat = os.stat(source_path, follow_symlinks=False)
                        if (
                            final_stat.st_ino != opened_stat.st_ino
                            or final_stat.st_dev != opened_stat.st_dev
                            or final_stat.st_size != opened_stat.st_size
                            or final_stat.st_mtime_ns != opened_stat.st_mtime_ns
                        ):
                            raise OSError(
                                f"Configuration file changed during backup: {source_path}"
                            )
                        record.update(
                            type="file",
                            sha256=digest.hexdigest(),
                            backup=str(destination.relative_to(backup_dir)),
                        )
                    finally:
                        if source_fd >= 0:
                            os.close(source_fd)
                        if destination_fd is not None:
                            os.close(destination_fd)
                else:
                    raise OSError(
                        f"Refusing to purge unsupported configuration file type: "
                        f"{source_path}"
                    )
                manifest["files"].append(record)
                _write_json_private_update(manifest_path, manifest)

        manifest["status"] = "complete"
        _write_json_private_update(manifest_path, manifest)
    except Exception as error:
        manifest["error"] = str(error)
        try:
            _write_json_private_update(manifest_path, manifest)
        except OSError:
            pass
        raise OSError(
            f"Configuration backup is incomplete at {backup_dir}; purge was not run. "
            f"{error}"
        ) from error

    return backup_dir, sum(
        1 for record in manifest["files"] if record.get("type") in {"file", "symlink"}
    )


def _write_json_private_update(path, data):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    _write_json_private(temporary, data)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _purge_backup_note(backup_dir, file_count):
    noun = "file" if file_count == 1 else "files"
    return (
        f"Pre-purge configuration backup retained at {backup_dir} "
        f"({file_count} existing {noun})."
    )


def purge_leftover_configs(package_ids, output_callback=None):
    """
    Purges leftover APT configuration packages.

    These packages are already removed, but their configuration files remain.

    Uses:

        pkexec apt-get purge -y package1 package2 ...
    """

    if not package_ids:
        return True, "No leftover packages selected."

    # Safety validation: allow only normal package-name characters.
    safe_package_regex = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]*$")

    invalid_packages = [
        package_id
        for package_id in package_ids
        if not safe_package_regex.match(package_id)
    ]

    if invalid_packages:
        return False, (
            "Blocked unsafe or invalid package names:\n\n"
            + "\n".join(invalid_packages)
        )

    try:
        status_result = subprocess.run(
            [
                "dpkg-query",
                "-W",
                "-f=${db:Status-Abbrev}\t${Package}\n",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"Could not verify residual APT configurations: {error}"

    if status_result.returncode != 0:
        return False, (
            status_result.stderr.strip()
            or "Could not verify residual APT configurations."
        )

    residual_packages = set()
    for line in status_result.stdout.splitlines():
        status, separator, package = line.partition("\t")
        if separator and status[:2] == "rc":
            residual_packages.add(package.strip())

    packages_to_purge = [
        package_id
        for package_id in dict.fromkeys(package_ids)
        if package_id in residual_packages
    ]
    skipped_packages = [
        package_id
        for package_id in dict.fromkeys(package_ids)
        if package_id not in residual_packages
    ]
    if not packages_to_purge:
        return False, (
            "None of the selected packages are currently in dpkg's "
            "residual-configuration state. No packages were purged."
        )

    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"
    pkexec_command = shutil.which("pkexec")

    try:
        backup_dir, backed_up_count = _backup_residual_conffiles(packages_to_purge)
    except OSError as error:
        _log_action(f"LEFTOVER_PURGE_BLOCKED backup_error={error}")
        return False, (
            "Purge was not started because Appector could not securely back up "
            f"the remaining configuration files.\n\n{error}"
        )

    cmd = [
        apt_get_command,
        "purge",
        "-y",
    ] + packages_to_purge

    if pkexec_command:
        cmd = [pkexec_command] + cmd

    _log_action(f"LEFTOVER_PURGE_START cmd={shlex.join(cmd)}")

    try:
        success, message = _run_command_stream(cmd, output_callback)
    except NameError:
        success, message = _run_command(cmd)

    if success:
        _log_action("LEFTOVER_PURGE_SUCCESS")
        message += f"\n\n{_purge_backup_note(backup_dir, backed_up_count)}"
        if skipped_packages:
            message += (
                "\n\nSkipped packages no longer in residual-configuration state:\n"
                + "\n".join(skipped_packages)
            )
    else:
        _log_action(f"LEFTOVER_PURGE_FAILED backup={backup_dir} error={message}")
        message += (
            f"\n\n{_purge_backup_note(backup_dir, backed_up_count)}"
        )

    return success, message
    
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
    """
    cmd = ["apt-get", "--simulate", "autoremove"]
    try:
        returncode, output = _run_command_raw(
            cmd,
            timeout=60,
            env_overrides={"LC_ALL": "C"},
        )
        if returncode != 0:
            return False, [], "", output or (
                f"APT autoremove simulation failed with exit code {returncode}."
            )

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

        return True, packages, space_freed, output

    except Exception as e:
        return False, [], "", str(e)


def execute_apt_autoremove(output_callback=None):
    """
    Executes apt-get autoremove with pkexec.
    Streams output line-by-line via output_callback.

    Returns:
        success: bool
        message: str
    """
    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"
    pkexec_command = shutil.which("pkexec")

    cmd = [apt_get_command, "autoremove", "-y"]

    if pkexec_command:
        cmd = [pkexec_command] + cmd

    _log_action(f"APT_AUTOREMOVE_START cmd={shlex.join(cmd)}")
    success, message = _run_command_stream(
        cmd,
        output_callback=output_callback,
        timeout=900,
    )
    if success:
        _log_action("APT_AUTOREMOVE_SUCCESS")
        return True, message

    _log_action(f"APT_AUTOREMOVE_FAILED error={message}")
    return False, message


# ------------------------------------------------------------
# Flatpak Unused Runtimes
# ------------------------------------------------------------

def get_flatpak_unused_preview():
    """
    Asks Flatpak for its unused-removal plan and declines the confirmation.

    Returns:
        success: bool
        preview_text: str
        raw_output: str
    """
    flatpak_command = shutil.which("flatpak")
    if not flatpak_command:
        return False, "", "flatpak command not found."

    outputs = []
    previews = []
    refs_by_scope = {}
    env = os.environ.copy()
    env["LC_ALL"] = "C"

    for scope in ("--user", "--system"):
        cmd = [
            flatpak_command,
            "uninstall",
            scope,
            "--unused",
        ]
        try:
            result = subprocess.run(
                cmd,
                input="n\n",
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, "", f"Flatpak unused-runtime check timed out ({scope})."
        except OSError as error:
            return False, "", f"Could not check unused Flatpak runtimes ({scope}): {error}"

        output = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part.strip()
        )
        outputs.append(f"[{scope.removeprefix('--')}]\n{output}".rstrip())

        if result.returncode == 0 and "Nothing unused to uninstall" in output:
            continue

        if result.returncode != 0:
            declined_prompt = (
                not result.stderr.strip()
                and (
                    "[Y/n]" in output
                    or "[y/N]" in output
                    or "Proceed with these changes" in output
                )
            )
            if not declined_prompt:
                error = output or (
                    f"Flatpak unused-runtime check failed with exit code {result.returncode}."
                )
                return False, "", f"{scope}: {error}"

        if output:
            refs = list(dict.fromkeys(re.findall(
                r"\b((?:[A-Za-z0-9_-]+\.)+[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+)\b",
                output,
            )))
            if refs:
                refs_by_scope[scope] = refs
            preview = "\n".join(
                line
                for line in output.splitlines()
                if not any(
                    prompt in line.lower()
                    for prompt in (
                        "proceed with these changes",
                        "[y/n]",
                        "[n/y]",
                    )
                )
            ).strip()
            if preview:
                previews.append(f"{scope.removeprefix('--').title()} installation:\n{preview}")

    if refs_by_scope:
        size_estimate = get_removal_size_estimate(
            [],
            flatpak_refs_by_scope=refs_by_scope,
        )
        previews.append(size_estimate)

    return True, "\n\n".join(previews), "\n\n".join(outputs)


def execute_flatpak_unused_cleanup(output_callback=None):
    """
    Executes flatpak uninstall --unused.
    Streams output line-by-line via output_callback.

    Returns:
        success: bool
        message: str
    """
    flatpak_command = shutil.which("flatpak")

    if not flatpak_command:
        return False, "flatpak command not found."

    results = []
    failures = []
    for scope in ("--user", "--system"):
        cmd = [flatpak_command, "uninstall", scope, "--unused", "-y"]
        _log_action(f"FLATPAK_UNUSED_CLEANUP_START cmd={shlex.join(cmd)}")
        success, message = _run_command_stream(
            cmd,
            output_callback=output_callback,
            timeout=900,
        )
        scope_name = scope.removeprefix("--")
        results.append(f"{scope_name}: {message}")
        if not success:
            failures.append(scope_name)

    combined = "\n\n".join(results)
    if failures:
        _log_action(
            f"FLATPAK_UNUSED_CLEANUP_FAILED scopes={','.join(failures)}"
        )
        return False, f"Cleanup failed for: {', '.join(failures)}.\n\n{combined}"

    _log_action("FLATPAK_UNUSED_CLEANUP_SUCCESS")
    return True, combined
