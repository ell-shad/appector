"""Removal safety policy.

Pure decision logic about what Appector may remove and how risky it is. No
subprocesses, no filesystem writes: this module answers questions, and
`actions`/`residual` carry them out.

Kept separate from execution so the rules can be read, reviewed, and tested on
their own, and so both the package-manager and residual-purge subsystems share
one definition of "safe" (F15: this is the *only* place shared package-name
helpers and simulation parsing are defined).

APT risk model (S11): risk is decided from dpkg metadata when the caller can
provide it (``metadata`` mapping from `apt_metadata.get_apt_metadata_map`),
plus a small exact-name list. Substring heuristics ("gnome", "desktop", ...)
misfired on packages like gnome-calculator and desktop-file-utils.
"""

import re

# The managers whose apps can be removed. Historically two identical sets
# existed; one constant now backs both names (S11).
REMOVABLE_MANAGERS = {
    "Snap",
    "Flatpak",
    "AppImage",
    "Manual",
    "APT",
}

# Compatibility aliases: callers and tests refer to these names.
BATCH_REMOVABLE_MANAGERS = REMOVABLE_MANAGERS
SINGLE_REMOVABLE_MANAGERS = REMOVABLE_MANAGERS

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
    "pkexec",
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

# Snap runtime/base snaps: exactly "core" or "core<digits>" (S11). The old
# startswith("core") rule also blocked unrelated snaps like corectrl.
SNAP_CORE_RE = re.compile(r"^core[0-9]*$")

# Snap names skipped by the scanner: the blocked set covers runtime snaps and
# helpers; keep one definition (S11) instead of a second hand-maintained copy.
SNAP_SKIP_NAMES = frozenset(BLOCKED_PACKAGE_IDS)

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
    "pkexec",
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
    # Debian and derivatives (S11): the list above is Ubuntu-centric, so a
    # Debian/Mint/Pop!_OS system would let these through.
    "debian-archive-keyring",
    "libc6",
    "libc-bin",
    "login",
    "passwd",
    "mount",
    "base-files",
    "netplan.io",
    "openssh-server",
    "ufw",
    "cryptsetup",
    "lvm2",
    "shim-signed",
    "efibootmgr",
    "dbus",
    "udev",
    "gnome-core",
    "plasma-desktop",
    "xfce4",
    "cinnamon-desktop-environment",
    "mate-desktop-environment",
    "task-cinnamon-desktop",
    "task-gnome-desktop",
    "task-kde-desktop",
    "task-lxde-desktop",
    "task-mate-desktop",
    "task-xfce-desktop",
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

# Exact names that stay "high risk" without dpkg metadata (S11). Kept small
# on purpose: the metadata-driven rules below carry the general cases.
APT_HIGH_RISK_EXACT = {
    "gnome-control-center",
    "gnome-tweaks",
    "gnome-software",
    "snapd-desktop-integration",
    "ubuntu-release-upgrader-gtk",
    "update-manager",
    "update-notifier",
    "software-properties-gtk",
    "xserver-xorg",
    "wayland-protocols",
    "tasksel",
}

# dpkg priorities that make a package part of the base system.
APT_CRITICAL_PRIORITIES = {"required", "important"}

# dpkg sections whose packages are system-critical by nature.
APT_HIGH_RISK_SECTIONS = {"kernel", "admin", "metapackages"}


# An APT removal that would take more than this many packages at once is
# blocked: that scale indicates a dependency cascade, not an app removal.
APT_MAX_REMOVALS = 100


# Package names must survive being passed as an argv element to apt, dpkg,
# flatpak and snap. This rejects anything that could be read as an option or
# as shell syntax rather than a name. Single definition (F15).
SAFE_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]*$")


def _normalize_package_id(name: str) -> str:
    return str(name or "").strip().split(":")[0].lower()


def _base_package_name(name: str) -> str:
    return str(name or "").split(":", 1)[0]


def _package_arch(name: str) -> str:
    _, separator, arch = str(name or "").partition(":")
    return arch if separator else ""


def is_snap_runtime(name: str) -> bool:
    """True for snap base/runtime snaps (core, core18, ...)."""
    return bool(SNAP_CORE_RE.match(_normalize_package_id(name)))


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


def is_critical_apt_metadata(metadata) -> bool:
    """dpkg-metadata view of "the system needs this package" (S11)."""
    if not isinstance(metadata, dict):
        return False
    if str(metadata.get("essential", "")).strip().lower() == "yes":
        return True
    if str(metadata.get("priority", "")).strip().lower() in APT_CRITICAL_PRIORITIES:
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


def _parse_apt_simulation_removed_packages(output: str):
    """Extract package tokens from `Remv`/`Purg` lines of an apt simulation.

    Architecture-qualified (S2): `Purg libfoo:i386` yields ``libfoo:i386``,
    not ``libfoo`` — callers compare exact `name:arch` identities.
    """
    removed = []
    for line in output.splitlines():
        if line.startswith(("Remv ", "Purg ")):
            parts = line.split()
            if len(parts) >= 2:
                removed.append(parts[1])

    seen = set()
    unique_removed = []

    for package_name in removed:
        if package_name not in seen:
            seen.add(package_name)
            unique_removed.append(package_name)

    return unique_removed


def _format_package_list(packages, limit=50):
    lines = [f"• {name}" for name in packages[:limit]]
    if len(packages) > limit:
        lines.append(f"• …and {len(packages) - limit} more")
    return "\n".join(lines)


def is_blocked(app) -> bool:
    manager = str(getattr(app, "manager", "") or "").strip()
    package_id = str(getattr(app, "package_id", "") or "").strip()

    if not package_id:
        return True

    normalized = _normalize_package_id(package_id)

    if normalized in BLOCKED_PACKAGE_IDS_LOWER:
        return True

    if manager == "Snap":
        if is_snap_runtime(package_id):
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


def get_removal_risk(app, metadata=None):
    """
    Returns:

        risk_level, reason

    risk_level:
        "low"
        "normal"
        "medium"
        "high"
        "blocked"

    ``metadata`` is an optional dpkg metadata mapping (see
    `apt_metadata.get_apt_metadata_map`) used for APT risk decisions; when
    absent, decisions fall back to the exact-name lists.
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
        if is_snap_runtime(package_id):
            return "blocked", "Snap runtime/base snaps are blocked."
        return "normal", ""

    if manager == "APT":
        if is_critical_apt_package_name(package_id):
            return "blocked", "Critical APT package."

        if is_critical_apt_metadata(metadata):
            return (
                "blocked",
                "dpkg marks this package as essential or base-system "
                "(required/important priority).",
            )

        if normalized in APT_HIGH_RISK_EXACT:
            return (
                "high",
                "This package is part of the desktop or update infrastructure.",
            )

        section = str((metadata or {}).get("section", "")).strip().lower()
        if section in APT_HIGH_RISK_SECTIONS and section:
            if section == "metapackages":
                return (
                    "high",
                    "Metapackage: removing it can drag whole desktop stacks "
                    "with it.",
                )
            return (
                "high",
                "This package belongs to a system-critical section "
                f"({section}).",
            )

        if normalized.startswith("lib"):
            return (
                "medium",
                "Shared library removal may affect other applications.",
            )

        return "normal", ""

    return "normal", ""
