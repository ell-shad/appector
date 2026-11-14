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
