import re
from dataclasses import dataclass, field


@dataclass
class AppEntry:
    name: str
    manager: str
    source: str
    package_id: str
    version: str = ""
    installed_at: str | None = None
    category: str = "Other"
    is_gui_app: bool = True
    details: str = ""
    icon: str = ""
    exec_name: str = ""
    is_duplicate: bool = False
    removal_paths: list[str] = field(default_factory=list)
    # F2: app_key strings of the installs this entry collides with, so the
    # UI can say *what* a duplicate duplicates instead of only flagging it.
    duplicate_with: list[str] = field(default_factory=list)
    # F11: explicit Flatpak installation name ("user", "system", or a
    # custom one); avoids inferring the scope from a substring of details.
    installation: str = ""


def _identity(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def mark_duplicate_apps(apps):
    """Mark GUI apps that share a normalized name or executable across installs.

    Flatpak and Snap apps have globally unique IDs, so their exec_name is not
    an identity (F2: two unrelated Flatpaks both derived exec_name "client"
    from their ID and were flagged as duplicates). They participate in
    matching through their display name only.
    """
    groups = {}
    weak_groups = {}
    ignored_identities = {
        "python",
        "python3",
        "java",
        "sh",
        "bash",
        "files",
        "settings",
        "app",
        "run",
        "electron",
    }

    for app in apps:
        app.is_duplicate = False
        app.duplicate_with = []
        if not app.is_gui_app:
            continue

        if str(app.manager) in {"Flatpak", "Snap"}:
            # Unique-ID managers: the package ID is the only strong
            # identity; display-name collisions between two Flatpaks are
            # not duplicates (F2), though a name shared across *different*
            # managers still is.
            strong = [_identity(app.package_id)]
            weak = _identity(app.name)
        else:
            strong = [_identity(app.exec_name)]
            weak = _identity(app.name)

        for identity in strong:
            if identity and len(identity) >= 4 and identity not in ignored_identities:
                groups.setdefault(identity, []).append(app)
        if weak and len(weak) >= 4 and weak not in ignored_identities:
            weak_groups.setdefault(weak, []).append(app)

    def _mark(group):
        installations = {
            (app.manager, app.package_id, app.details)
            for app in group
        }
        if len(installations) > 1:
            for app in group:
                app.is_duplicate = True
                app.duplicate_with = sorted(
                    {
                        f"{other.manager}::{other.package_id}"
                        for other in group
                        if other is not app
                    }
                )

    for group in groups.values():
        _mark(group)

    for group in weak_groups.values():
        managers = {app.manager for app in group}
        # Same-manager name collisions are only meaningful for managers
        # without unique IDs; Flatpak-vs-Flatpak "Client" is a naming
        # artifact, not a duplicate install.
        if "Flatpak" in managers or "Snap" in managers:
            if len(managers) < 2:
                continue
        _mark(group)


def _version_is_digit(character):
    # dpkg compares version bytes, so only ASCII 0-9 are numeric.
    return "0" <= character <= "9"


def _version_is_letter(character):
    return "a" <= character <= "z" or "A" <= character <= "Z"


def _version_order(character):
    if character == "~":
        return -1
    if _version_is_digit(character):
        return 0
    if _version_is_letter(character):
        return ord(character)
    return ord(character) + 256


def _compare_version_part(left, right):
    """Compare one Debian version component, following dpkg's algorithm."""
    left_index = right_index = 0

    while left_index < len(left) or right_index < len(right):
        while (
            left_index < len(left) and not _version_is_digit(left[left_index])
        ) or (
            right_index < len(right) and not _version_is_digit(right[right_index])
        ):
            left_order = (
                _version_order(left[left_index])
                if left_index < len(left)
                else 0
            )
            right_order = (
                _version_order(right[right_index])
                if right_index < len(right)
                else 0
            )

            if left_order != right_order:
                return -1 if left_order < right_order else 1

            left_index += 1
            right_index += 1

        while left_index < len(left) and left[left_index] == "0":
            left_index += 1
        while right_index < len(right) and right[right_index] == "0":
            right_index += 1

        first_difference = 0

        while (
            left_index < len(left)
            and _version_is_digit(left[left_index])
            and right_index < len(right)
            and _version_is_digit(right[right_index])
        ):
            if not first_difference:
                first_difference = ord(left[left_index]) - ord(right[right_index])
            left_index += 1
            right_index += 1

        if left_index < len(left) and _version_is_digit(left[left_index]):
            return 1
        if right_index < len(right) and _version_is_digit(right[right_index]):
            return -1
        if first_difference:
            return -1 if first_difference < 0 else 1

    return 0


def compare_debian_versions(left, right):
    """Compare Debian version strings, returning -1, 0, or 1.

    The table shows versions from APT, Flatpak, Snap and other sources, but
    Debian ordering handles all of them better than lexical ordering:
    numeric segments compare as numbers, `~` sorts before a release, and
    epochs/revisions are significant. Versions that are not Debian-shaped
    still compare deterministically by their remaining segments.
    """
    def split(value):
        text = str(value or "").strip()
        epoch = 0

        if ":" in text:
            head, rest = text.split(":", 1)
            if head.isascii() and head.isdigit():
                epoch = int(head)
                text = rest

        if "-" in text:
            upstream, revision = text.rsplit("-", 1)
        else:
            upstream, revision = text, ""

        return epoch, upstream, revision

    left_epoch, left_upstream, left_revision = split(left)
    right_epoch, right_upstream, right_revision = split(right)

    if left_epoch != right_epoch:
        return -1 if left_epoch < right_epoch else 1

    return (
        _compare_version_part(left_upstream, right_upstream)
        or _compare_version_part(left_revision, right_revision)
    )
