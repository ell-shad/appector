"""Removal safety policy.

Pure decision logic about what Appector may remove and how risky it is. No
subprocesses, no filesystem writes: this module answers questions, and
`actions`/`residual` carry them out.

Kept separate from execution so the rules can be read, reviewed, and tested on
their own, and so both the package-manager and residual-purge subsystems share
one definition of "safe".
"""

import re

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

# An APT removal that would take more than this many packages at once is
# blocked: that scale indicates a dependency cascade, not an app removal.
APT_MAX_REMOVALS = 100


# Package names must survive being passed as an argv element to apt, dpkg,
# flatpak and snap. This rejects anything that could be read as an option or
# as shell syntax rather than a name.
SAFE_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]*$")


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


def _deduplicate_package_ids(package_ids):
    """Deduplicate while preserving order and dropping empty entries."""
    seen = set()
    result = []
    for package_id in package_ids or []:
        value = str(package_id or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _split_safe_package_ids(package_ids):
    valid = []
    invalid = []
    for package_id in _deduplicate_package_ids(package_ids):
        if _is_safe_package_id(package_id):
            valid.append(package_id)
        else:
            invalid.append(package_id)
    return valid, invalid

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
