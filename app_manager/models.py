import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class AppEntry:
    name: str
    manager: str
    source: str
    package_id: str
    version: str = ""
    installed_at: Optional[str] = None
    category: str = "Other"
    is_gui_app: bool = True
    details: str = ""
    icon: str = ""
    exec_name: str = ""
    is_duplicate: bool = False


def mark_duplicate_apps(apps):
    """Mark GUI apps that share a normalized name or executable across installs."""
    groups = {}
    ignored_identities = {
        "python",
        "python3",
        "java",
        "sh",
        "bash",
        "files",
        "settings",
    }

    for app in apps:
        app.is_duplicate = False
        if not app.is_gui_app:
            continue

        identities = {
            re.sub(r"[^a-z0-9]", "", value.lower())
            for value in (app.exec_name, app.name)
            if value
        }

        for identity in identities - ignored_identities:
            if len(identity) >= 4:
                groups.setdefault(identity, []).append(app)

    for group in groups.values():
        installations = {
            (app.manager, app.package_id, app.details)
            for app in group
        }
        if len(installations) > 1:
            for app in group:
                app.is_duplicate = True
