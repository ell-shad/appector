import os
import re
import shlex
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

# ------------------------------------------------------------
# ANSI escape code stripper
# ------------------------------------------------------------
ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def _strip_ansi(text: str) -> str:
    """Removes terminal color and cursor control codes from text."""
    if not text:
        return ""
    return ANSI_ESCAPE_RE.sub('', text)

BATCH_REMOVABLE_MANAGERS = {
    "Snap",
    "Flatpak",
    "AppImage",
    "APT",
}

SINGLE_REMOVABLE_MANAGERS = {
    "Snap",
    "Flatpak",
    "AppImage",
    "APT",
}

BLOCKED_PACKAGE_IDS = {
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
    try:
        log_dir = Path.home() / ".local" / "state" / "app-manager"
        log_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_dir / "actions.log"

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


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
        return "low", "Removes a standalone AppImage file."

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
        return f"rm \"{package_id}\""

    return "Removal preview not available"


# ------------------------------------------------------------
# Basic command execution helpers
# ------------------------------------------------------------

def _run_command_raw(cmd, timeout=900):
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["PAGER"] = "cat"

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

        return result.returncode, output
    except subprocess.TimeoutExpired:
        return 124, "Command timed out."
    except Exception as e:
        return 1, str(e)

def install_deb(path, output_callback=None, delete_source=False):
    """
    Installs a local .deb file using apt-get.

    Returns:
        success: bool
        message: str
    """

    deb_path = Path(path).expanduser()

    if not deb_path.is_file():
        return False, "Selected file not found."

    if deb_path.suffix.lower() != ".deb":
        return False, "Selected file is not a .deb package."

    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"
    pkexec_command = shutil.which("pkexec")

    if pkexec_command:
        cmd = [
            pkexec_command,
            apt_get_command,
            "install",
            "-y",
            str(deb_path),
        ]
    else:
        cmd = [
            apt_get_command,
            "install",
            "-y",
            str(deb_path),
        ]

    _log_action(f"DEB_INSTALL_START cmd={shlex.join(cmd)}")

    success, message = _run_command_stream(cmd, output_callback)

    if success:
        message = _maybe_delete_source_file(deb_path, delete_source, message)
        _log_action(f"DEB_INSTALL_SUCCESS path={deb_path}")
    else:
        _log_action(f"DEB_INSTALL_FAILED path={deb_path} error={message}")

    return success, message


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

    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,  # Prevents scripts from hanging waiting for input
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        timer = None

        if timeout:
            timer = threading.Timer(timeout, process.kill)
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

        if timer:
            timer.cancel()

        returncode = process.returncode
        output = "\n".join(lines).strip()

        if returncode == 0:
            return True, output if output else "Command completed successfully."

        return False, output if output else f"Command failed with exit code {returncode}."

    except Exception as e:
        return False, str(e)
        
def _maybe_delete_source_file(path, delete_source, message):
    if not delete_source:
        return message

    try:
        source_path = Path(path).expanduser()

        if source_path.is_file():
            source_path.unlink()

            if message:
                return message + "\n\nDeleted installation file after successful installation."

            return "Deleted installation file after successful installation."
    except Exception:
        pass

    return message

# ------------------------------------------------------------
# Batch .deb installation
# ------------------------------------------------------------

def install_deb_batch(paths, output_callback=None, delete_source=False):
    """
    Installs multiple local .deb files using one apt-get transaction.

    Returns:
        success: bool
        message: str
    """

    if not paths:
        return False, "No .deb files selected."

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
    pkexec_command = shutil.which("pkexec")

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
            deleted = []

            for p in deb_paths:
                try:
                    p.unlink()
                    deleted.append(p.name)
                except Exception:
                    continue

            if deleted:
                message += "\n\nDeleted installation files:\n"
                message += "\n".join(f"• {name}" for name in deleted)

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
    """

    if not paths:
        return True, "No .flatpakref files selected."

    total = len(paths)
    success_count = 0
    failures = []

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
        else:
            first_line = (message or "").splitlines()[0] if message else "Installation failed"
            failures.append(f"{name}: {first_line}")

        if output_callback:
            status = "OK" if success else "FAIL"
            output_callback(f"[{index}/{total}] {status} {name}")

    summary = f"{success_count}/{total} Flatpak reference files installed."

    if failures:
        summary += "\n\nFailures:\n"
        summary += "\n".join(failures[:20])

        if len(failures) > 20:
            summary += f"\n…and {len(failures) - 20} more"

    return success_count == total, summary
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

    message = "Use batch removal for this package type."
    _log_action(f"REMOVE_SKIPPED {key} manager={manager} reason=use-batch")
    return False, message


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

    returncode, output = _run_command_raw(cmd)

    if returncode != 0:
        _log_action("APT_SIMULATION_FAILED")
        return False, output, []

    removed_packages = _parse_apt_simulation_removed_packages(output)

    _log_action(f"APT_SIMULATION_SUCCESS removed_count={len(removed_packages)}")

    return True, output, removed_packages


def _parse_apt_simulation_removed_packages(output: str):
    removed = []

    for line in output.splitlines():
        if line.startswith("Remv "):
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
        f"Total packages to remove: {len(removed_packages)}"
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
    else:
        _log_action(f"APT_REMOVE_FAILED {key} error={message}")

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

    appimages = []
    snaps = []
    user_flatpaks = []
    system_flatpaks = []
    apts = []

    for app in apps:
        manager = getattr(app, "manager", "")

        if manager == "AppImage":
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
        len(appimages)
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

    success, message = _run_command_stream(cmd, output_callback)

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

    success, message = _install_flatpak_location(
        str(ref_path),
        user_install,
        output_callback,
    )

    if success:
        message = _maybe_delete_source_file(ref_path, delete_source, message)
        return True, message

    url = _parse_flatpakref_url(ref_path)

    if url:
        url_success, url_message = _install_flatpak_location(
            url,
            user_install,
            output_callback,
        )

        if url_success:
            url_message = _maybe_delete_source_file(ref_path, delete_source, url_message)
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

    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"
    pkexec_command = shutil.which("pkexec")

    cmd = [
        apt_get_command,
        "purge",
        "-y",
    ] + list(package_ids)

    if pkexec_command:
        cmd = [pkexec_command] + cmd

    _log_action(f"LEFTOVER_PURGE_START cmd={shlex.join(cmd)}")

    try:
        success, message = _run_command_stream(cmd, output_callback)
    except NameError:
        success, message = _run_command(cmd)

    if success:
        _log_action("LEFTOVER_PURGE_SUCCESS")
    else:
        _log_action(f"LEFTOVER_PURGE_FAILED error={message}")

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
    import re
    import subprocess

    cmd = ["apt-get", "--simulate", "autoremove"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        output = result.stdout + "\n" + result.stderr

        # Parse packages to be removed
        packages = []
        in_removal_section = False

        for line in output.splitlines():
            if "The following packages will be REMOVED" in line:
                in_removal_section = True
                continue

            if in_removal_section:
                stripped = line.strip()

                # End of section: empty line or next section header
                if not stripped or stripped.startswith("The following") or stripped.startswith("0 upgraded"):
                    break

                # Package names are space-separated, possibly with trailing commas
                parts = stripped.split()
                for part in parts:
                    pkg = part.strip().rstrip(",")
                    if pkg and re.match(r"^[a-zA-Z0-9]", pkg):
                        # Remove architecture suffix if present (e.g., :amd64)
                        pkg = pkg.split(":")[0]
                        if pkg not in packages:
                            packages.append(pkg)

        # Parse disk space freed
        space_freed = ""
        for line in output.splitlines():
            lower = line.lower()
            if "freed" in lower or "disk space" in lower:
                space_freed = line.strip()
                break

        return True, packages, space_freed, output

    except subprocess.TimeoutExpired:
        return False, [], "", "Command timed out."
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
    import re
    import shutil
    import subprocess
    import threading

    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"
    pkexec_command = shutil.which("pkexec")

    cmd = [apt_get_command, "autoremove", "-y"]

    if pkexec_command:
        cmd = [pkexec_command] + cmd

    try:
        _log_action(f"APT_AUTOREMOVE_START cmd={' '.join(cmd)}")
    except Exception:
        pass

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        timer = threading.Timer(900, process.kill)
        timer.start()

        lines = []

        if process.stdout:
            for line in process.stdout:
                if output_callback:
                    try:
                        output_callback(line.rstrip())
                    except Exception:
                        pass
                lines.append(line)

        process.wait()
        timer.cancel()

        returncode = process.returncode
        output = "".join(lines).strip()

        if returncode == 0:
            try:
                _log_action("APT_AUTOREMOVE_SUCCESS")
            except Exception:
                pass
            return True, output if output else "Autoremove completed successfully."

        try:
            _log_action(f"APT_AUTOREMOVE_FAILED error={output}")
        except Exception:
            pass

        return False, output if output else f"Autoremove failed with exit code {returncode}."

    except Exception as e:
        return False, str(e)


# ------------------------------------------------------------
# Flatpak Unused Runtimes
# ------------------------------------------------------------

def get_flatpak_unused_preview():
    """
    Attempts to detect unused Flatpak runtimes.

    Returns:
        success: bool
        runtimes: list[str]
        raw_output: str
    """
    import subprocess

    # flatpak doesn't have a direct "list unused" command,
    # but we can check what uninstall --unused would do
    # by running a dry-run style check.
    # Unfortunately flatpak doesn't support --dry-run well,
    # so we'll list all runtimes and let the user confirm.

    cmd = ["flatpak", "list", "--runtime", "--columns=application,version"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        output = result.stdout.strip()
        runtimes = []

        for line in output.splitlines():
            line = line.strip()
            if line:
                parts = line.split("\t")
                if parts:
                    runtimes.append(parts[0])

        return True, runtimes, output

    except subprocess.TimeoutExpired:
        return False, [], "Command timed out."
    except Exception as e:
        return False, [], str(e)


def execute_flatpak_unused_cleanup(output_callback=None):
    """
    Executes flatpak uninstall --unused.
    Streams output line-by-line via output_callback.

    Returns:
        success: bool
        message: str
    """
    import shutil
    import subprocess
    import threading

    flatpak_command = shutil.which("flatpak")

    if not flatpak_command:
        return False, "flatpak command not found."

    cmd = [flatpak_command, "uninstall", "--unused", "-y"]

    try:
        _log_action(f"FLATPAK_UNUSED_CLEANUP_START cmd={' '.join(cmd)}")
    except Exception:
        pass

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        timer = threading.Timer(900, process.kill)
        timer.start()

        lines = []

        if process.stdout:
            for line in process.stdout:
                if output_callback:
                    try:
                        output_callback(line.rstrip())
                    except Exception:
                        pass
                lines.append(line)

        process.wait()
        timer.cancel()

        returncode = process.returncode
        output = "".join(lines).strip()

        if returncode == 0:
            try:
                _log_action("FLATPAK_UNUSED_CLEANUP_SUCCESS")
            except Exception:
                pass
            return True, output if output else "Flatpak unused cleanup completed successfully."

        try:
            _log_action(f"FLATPAK_UNUSED_CLEANUP_FAILED error={output}")
        except Exception:
            pass

        return False, output if output else f"Cleanup failed with exit code {returncode}."

    except Exception as e:
        return False, str(e)
