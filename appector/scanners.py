import os
import gzip
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Dict
from urllib.parse import unquote, urlsplit

from .models import AppEntry


# ------------------------------------------------------------
# Desktop file helpers
# ------------------------------------------------------------

def parse_desktop_file(path: str) -> Tuple[str, List[str], bool, bool, str, str]:
    name = ""
    categories: List[str] = []
    no_display = False
    hidden = False
    icon = ""
    exec_line = ""

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()

                if line.startswith("NoDisplay=true"):
                    no_display = True
                elif line.startswith("Hidden=true"):
                    hidden = True
                elif line.startswith("Name=") and not name:
                    name = line.split("=", 1)[1].strip()
                elif line.startswith("Categories="):
                    value = line.split("=", 1)[1].strip()
                    categories = [c for c in value.split(";") if c]
                elif line.startswith("Icon=") and not icon:
                    icon = line.split("=", 1)[1].strip()
                elif line.startswith("Exec=") and not exec_line:
                    exec_line = line.split("=", 1)[1].strip()
    except Exception:
        pass

    return name, categories, no_display, hidden, icon, exec_line


def find_flatpak_icon(app_id: str) -> str:
    candidates = [
        Path("/var/lib/flatpak/exports/share/applications") / f"{app_id}.desktop",
        Path.home() / ".local/share/flatpak/exports/share/applications" / f"{app_id}.desktop",
    ]

    for candidate in candidates:
        if candidate.exists():
            icon = get_desktop_icon(candidate)
            if icon:
                return icon

    return ""


def find_snap_icon(snap_name: str) -> str:
    desktop_dir = Path("/var/lib/snapd/desktop/applications")

    if not desktop_dir.exists():
        return ""

    patterns = [
        f"{snap_name}.desktop",
        f"{snap_name}_*.desktop",
        f"snap_{snap_name}.desktop",
        f"snap_{snap_name}_*.desktop",
    ]

    for pattern in patterns:
        try:
            for path in desktop_dir.glob(pattern):
                icon = get_desktop_icon(path)
                if icon:
                    return icon
        except Exception:
            continue

    # Fallback: scan snap desktop entries and match by Exec/content.
    try:
        for path in desktop_dir.glob("*.desktop"):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            if (
                f"/snap/{snap_name}" in text
                or f"snap run {snap_name}" in text
                or f"snap {snap_name}" in text
            ):
                icon = get_desktop_icon(path)
                if icon:
                    return icon
    except Exception:
        pass

    return ""
    
def get_desktop_icon(path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("Icon="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""

def map_categories(categories: List[str]) -> str:
    mapping = {
        "audiovideo": "Multimedia",
        "audio": "Multimedia",
        "video": "Multimedia",
        "player": "Multimedia",
        "tv": "Multimedia",
        "development": "Development",
        "ide": "Development",
        "game": "Games",
        "graphics": "Graphics",
        "network": "Internet",
        "internet": "Internet",
        "office": "Office",
        "education": "Education",
        "science": "Science",
        "settings": "Settings",
        "system": "System",
        "utility": "Utilities",
        "utilities": "Utilities",
        "accessories": "Utilities",
    }

    for c in categories:
        result = mapping.get(c.lower())
        if result:
            return result

    return "Other"


def get_package_owner(path: str) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["dpkg", "-S", path],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return out.split(":", 1)[0].strip()
    except Exception:
        return None


# ------------------------------------------------------------
# Leftover scanner
# ------------------------------------------------------------

def scan_leftover_configs() -> List[AppEntry]:
    """Find APT packages in dpkg's residual-config state (removed, config remains)."""

    apps: List[AppEntry] = []

    out = subprocess.check_output(
        [
            "dpkg-query",
            "-W",
            "-f=${db:Status-Abbrev}\t${Package}\n",
        ],
        text=True,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        timeout=30,
        env={**os.environ, "LC_ALL": "C"},
    )

    for line in out.splitlines():
        parts = line.split("\t", 1)

        if len(parts) < 2:
            continue

        status = parts[0].strip()
        package_name = parts[1].strip()

        if status[:2] == "rc" and package_name:
            # Defense-in-depth: dpkg package names are constrained; skip
            # anything unexpected rather than turning it into UI state.
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+:-]*", package_name):
                continue
            apps.append(
                AppEntry(
                    name=package_name,
                    manager="Leftover",
                    source="APT residual configuration",
                    package_id=package_name,
                    version="",
                    installed_at=None,
                    category="System",
                    is_gui_app=False,
                    details=(
                        "Package is already removed; only its configuration files remain. "
                        "Purging deletes those files."
                    ),
                    icon="",
                )
            )

    return apps


# ------------------------------------------------------------
# Scanners
# ------------------------------------------------------------

def _read_log_lines(path: Path) -> List[str]:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
                return f.readlines()
        else:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.readlines()
    except Exception:
        return []

def get_apt_install_dates() -> Dict[str, str]:
    """
    Attempts to find the first install date for APT packages.

    Sources:
    - /var/log/dpkg.log*
    - /var/log/apt/history.log*

    Returns:
        {
            package_name: "YYYY-MM-DD"
        }
    """

    dates: Dict[str, str] = {}

    log_dir = Path("/var/log")

    # ------------------------------------------------------------
    # Parse dpkg logs
    # Example:
    # 2024-05-01 10:11:10 install vlc:amd64 <none> 3.0.20
    # ------------------------------------------------------------
    if log_dir.exists():
        dpkg_logs = [
            p
            for p in log_dir.glob("dpkg.log*")
            if p.is_file()
        ]

        for log_file in sorted(dpkg_logs):
            for line in _read_log_lines(log_file):
                parts = line.strip().split()

                if len(parts) >= 5 and parts[2] == "install":
                    date_str = parts[0]
                    package_name = parts[3].split(":", 1)[0]
                    previous_state = parts[4]

                    if previous_state == "<none>" and package_name:
                        existing = dates.get(package_name)

                        if not existing or date_str < existing:
                            dates[package_name] = date_str

    # ------------------------------------------------------------
    # Parse APT history logs as fallback
    # Example:
    # Start-Date: 2024-05-01  10:11:12
    # Install: vlc:amd64 (3.0.20-1), ...
    # ------------------------------------------------------------
    apt_log_dir = Path("/var/log/apt")

    if apt_log_dir.exists():
        history_logs = [
            p
            for p in apt_log_dir.glob("history.log*")
            if p.is_file()
        ]

        for log_file in sorted(history_logs):
            current_date = None

            for line in _read_log_lines(log_file):
                line = line.strip()

                if line.startswith("Start-Date:"):
                    try:
                        current_date = line.split(":", 1)[1].strip().split()[0]
                    except Exception:
                        current_date = None

                elif line.startswith("Install:") and current_date:
                    value = line.split(":", 1)[1]

                    for token in value.split(","):
                        token = token.strip()

                        if not token:
                            continue

                        package_name = token.split(":", 1)[0].split()[0]

                        if package_name:
                            existing = dates.get(package_name)

                            if not existing or current_date < existing:
                                dates[package_name] = current_date

    return dates

def get_apt_source_map(package_names: List[str]) -> Dict[str, str]:
    """
    Attempts to detect where an APT package came from.

    Uses python-apt if available.

    Possible labels:
    - Ubuntu repository
    - PPA
    - Third-party repository
    - Local .deb
    - APT / Debian
    """

    source_map: Dict[str, str] = {}

    if not package_names:
        return source_map

    try:
        import apt_pkg

        apt_pkg.init()
        cache = apt_pkg.Cache()
    except Exception:
        return source_map

    for package_name in package_names:
        try:
            pkg = cache[package_name]
        except Exception:
            continue

        current_version = getattr(pkg, "current_ver", None)

        if not current_version:
            continue

        label = "Local .deb"
        has_repository = False

        try:
            file_list = current_version.file_list
        except Exception:
            file_list = []

        for entry in file_list:
            try:
                file_item = entry[0] if isinstance(entry, tuple) else entry

                filename = getattr(file_item, "filename", "") or ""
                origin = getattr(file_item, "origin", "") or ""
                site = getattr(file_item, "site", "") or ""
            except Exception:
                continue

            if filename == "/var/lib/dpkg/status":
                continue

            has_repository = True

            low_filename = filename.lower()
            low_origin = origin.lower()
            low_site = site.lower()

            if (
                "ppa" in low_filename
                or "ppa" in low_site
                or "ppa.launchpad" in low_filename
                or "ppa.launchpad" in low_site
            ):
                label = "PPA"
                break

            if (
                low_origin == "ubuntu"
                or "archive.ubuntu.com" in low_filename
                or "security.ubuntu.com" in low_filename
                or ".ubuntu.com" in low_site
            ):
                label = "Ubuntu repository"
            elif label == "Local .deb":
                label = "Third-party repository"

        if not has_repository:
            label = "Local .deb"

        source_map[package_name] = label

    return source_map

def normalize_exec(exec_line: str) -> str:
    """
    Extracts the base executable name from a desktop Exec= line.
    Example: 'env FIREFOX_SNAP=1 firefox %u' -> 'firefox'
    """
    if not exec_line:
        return ""
    
    parts = exec_line.split()
    for part in parts:
        if "=" in part and not part.startswith("/"):
            continue # skip env vars
        if part.startswith("%"):
            continue # skip desktop args like %u, %f
        
        base = Path(part).name.lower()
        if base and base not in ("env", "sh", "bash", "sudo"):
            return base
            
    return ""

def scan_apt() -> List[AppEntry]:
    """
    APT scanner.

    Current strategy:
    - find .desktop files
    - map them to dpkg packages
    - show them as GUI apps
    """

    versions = {}

    try:
        out = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Status}\n"],
            text=True,
            stderr=subprocess.DEVNULL,
        )

        for line in out.splitlines():
            parts = line.split("\t")

            if len(parts) < 3:
                continue

            pkg, version, status = parts[0], parts[1], parts[2]

            if "installed" in status:
                versions[pkg] = version

    except Exception:
        return []

    apps: List[AppEntry] = []
    seen_packages = set()

    desktop_dirs = [
        "/usr/share/applications",
        "/usr/local/share/applications",
        os.path.expanduser("~/.local/share/applications"),
    ]

    for directory in desktop_dirs:
        if not os.path.isdir(directory):
            continue

        try:
            entries = os.listdir(directory)
        except Exception:
            continue

        for filename in entries:
            if not filename.endswith(".desktop"):
                continue

            path = os.path.join(directory, filename)

            name, categories, no_display, hidden, icon, exec_line = parse_desktop_file(path)

            if no_display or hidden or not name:
                continue

            package = get_package_owner(path)

            if not package or package not in versions:
                continue

            if package in seen_packages:
                continue

            seen_packages.add(package)
            
            exec_name = normalize_exec(exec_line) or package.lower()

            apps.append(
                AppEntry(
                    name=name,
                    manager="APT",
                    source="APT / Debian",
                    package_id=package,
                    version=versions.get(package, ""),
                    installed_at=None,
                    category=map_categories(categories),
                    is_gui_app=True,
                    details=path,
                    icon=icon,
                    exec_name=exec_name,
                )
            )

    if apps:
        install_dates = get_apt_install_dates()
        source_map = get_apt_source_map(list(seen_packages))

        for app in apps:
            app.installed_at = install_dates.get(app.package_id)

            source = source_map.get(app.package_id)
            if source:
                app.source = source

    return apps

def get_snap_install_dates(revisions: Dict[str, str]) -> Dict[str, str]:
    """
    Attempts to find Snap install dates.

    Primary source:
    - snap changes

    Fallback:
    - timestamps of /var/lib/snapd/snaps/*.snap files
    """

    dates: Dict[str, str] = {}

    # ------------------------------------------------------------
    # Try snap changes
    # Example summary:
    # Install "firefox" snap
    # ------------------------------------------------------------
    commands = [
        ["snap", "changes", "--abs-time"],
        ["snap", "changes"],
    ]

    for cmd in commands:
        try:
            out = subprocess.check_output(
                cmd,
                text=True,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "LC_ALL": "C"},
            )
        except Exception:
            continue

        for line in out.splitlines():
            if 'Install "' not in line:
                continue

            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", line)
            name_match = re.search(r'Install "([^"]+)"', line)

            if not date_match or not name_match:
                continue

            snap_name = name_match.group(1)
            date_str = date_match.group(1)

            existing = dates.get(snap_name)

            if not existing or date_str < existing:
                dates[snap_name] = date_str

        if dates:
            break

    # ------------------------------------------------------------
    # Fallback: use snap file timestamps
    # ------------------------------------------------------------
    snap_dir = Path("/var/lib/snapd/snaps")

    if snap_dir.exists():
        for snap_name, revision in revisions.items():
            if snap_name in dates:
                continue

            timestamps = []

            if revision:
                exact_file = snap_dir / f"{snap_name}_{revision}.snap"

                if exact_file.exists():
                    try:
                        timestamps.append(exact_file.stat().st_mtime)
                    except Exception:
                        pass

            # Also check other revisions and use the earliest timestamp.
            try:
                for path in snap_dir.glob(f"{snap_name}_*.snap"):
                    try:
                        timestamps.append(path.stat().st_mtime)
                    except Exception:
                        continue
            except Exception:
                pass

            if timestamps:
                earliest = min(timestamps)
                dates[snap_name] = datetime.fromtimestamp(earliest).date().isoformat()

    return dates

def scan_snap() -> List[AppEntry]:
    try:
        out = subprocess.check_output(
            ["snap", "list"],
            text=True,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": "C"},
        )
    except Exception:
        return []

    apps = []
    revisions: Dict[str, str] = {}

    lines = out.splitlines()

    if len(lines) < 2:
        return []

    skip_names = {
        "snapd",
        "core",
        "core18",
        "core20",
        "core22",
        "core24",
        "bare",
        "gtk-common-themes",
        "snapd-desktop-integration",
    }

    for line in lines[1:]:
        parts = line.split()

        if len(parts) < 5:
            continue

        name = parts[0]
        version = parts[1]
        revision = parts[2] if len(parts) > 2 else ""
        notes = " ".join(parts[5:]) if len(parts) > 5 else ""
        normalized_name = name.lower()

        if name in skip_names:
            continue

        if "base" in notes.lower():
            continue

        revisions[name] = revision
        component_snap = (
            "content" in notes.lower()
            or "daemon" in notes.lower()
            or normalized_name.startswith(("gnome-", "gtk-"))
            or normalized_name.endswith(("-ffmpeg", "-platform"))
        )

        icon = find_snap_icon(name)

        apps.append(
            AppEntry(
                name=name.replace("-", " ").title(),
                manager="Snap",
                source="Snap Store",
                package_id=name,
                version=version,
                installed_at=None,
                category="Component" if component_snap else "Apps",
                is_gui_app=not component_snap,
                details=(
                    f"Snap component; notes: {notes or 'none'}"
                    if component_snap
                    else "Detected from snap list"
                ),
                icon=icon,
                exec_name=name.lower(),
            )
        )

    if apps:
        install_dates = get_snap_install_dates(revisions)

        for app in apps:
            app.installed_at = install_dates.get(app.package_id)

    return apps

def find_flatpak_install_date(app_id: str, installation: str) -> Optional[str]:
    """
    Best-effort Flatpak install date detection.

    Uses filesystem metadata from Flatpak app directories and exported
    desktop entries.

    Note:
    This may sometimes reflect the last update time rather than the
    original install time.
    """

    installation_lower = (installation or "").lower()

    user_app_dir = Path.home() / ".local/share/flatpak/app" / app_id
    system_app_dir = Path("/var/lib/flatpak/app") / app_id

    candidates = []

    if "user" in installation_lower:
        candidates.append(user_app_dir)
        candidates.append(system_app_dir)
    else:
        candidates.append(system_app_dir)
        candidates.append(user_app_dir)

    expanded_candidates = []

    for candidate in candidates:
        if candidate.exists():
            expanded_candidates.append(candidate)

        current = candidate / "current"

        if current.exists():
            expanded_candidates.append(current)

    # Fallback desktop entry timestamps.
    user_desktop = (
        Path.home()
        / ".local/share/flatpak/exports/share/applications"
        / f"{app_id}.desktop"
    )

    system_desktop = (
        Path("/var/lib/flatpak/exports/share/applications")
        / f"{app_id}.desktop"
    )

    if "user" in installation_lower:
        expanded_candidates.append(user_desktop)
        expanded_candidates.append(system_desktop)
    else:
        expanded_candidates.append(system_desktop)
        expanded_candidates.append(user_desktop)

    timestamps = []

    for path in expanded_candidates:
        try:
            timestamps.append(path.stat().st_mtime)
        except Exception:
            continue

    if not timestamps:
        return None

    earliest = min(timestamps)

    return datetime.fromtimestamp(earliest).date().isoformat()

def scan_flatpak() -> List[AppEntry]:
    try:
        out = subprocess.check_output(
            [
                "flatpak",
                "list",
                "--app",
                "--columns=application,version,origin,installation",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []

    apps = []

    for line in out.splitlines():
        cols = line.split("\t")

        if len(cols) < 4:
            continue

        app_id = cols[0]
        version = cols[1]
        origin = cols[2]
        installation = cols[3]

        name = app_id.split(".")[-1].replace("_", " ").replace("-", " ").title()
        source = origin if origin else installation

        icon = find_flatpak_icon(app_id)
        installed_at = find_flatpak_install_date(app_id, installation)

        exec_name = app_id.split(".")[-1].lower()
        
        apps.append(
            AppEntry(
                name=name,
                manager="Flatpak",
                source=source,
                package_id=app_id,
                version=version,
                installed_at=None,
                category="Apps",
                is_gui_app=True,
                details=f"Installation: {installation}",
                icon=icon,
                exec_name=exec_name,
            )
        )

    return apps


def scan_appimage() -> List[AppEntry]:
    apps = []
    home = Path.home()
    integrated_sources = set()

    desktop_dir = home / ".local/share/applications"
    for desktop_path in desktop_dir.glob("app-manager-appimage-*.desktop"):
        try:
            content = desktop_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if "X-AppManager-Installed-AppImage=true" not in content:
            continue

        appimage_path = next(
            (
                line.partition("=")[2].strip()
                for line in content.splitlines()
                if line.startswith("X-AppManager-AppImage-Path=")
            ),
            "",
        )
        if not appimage_path:
            continue
        appimage = Path(appimage_path)
        if not appimage.is_file() or not appimage_path.startswith(
            str(home / ".local/share/app-manager/appimages") + "/"
        ):
            continue
        source_uri = next(
            (
                line.partition("=")[2].strip()
                for line in content.splitlines()
                if line.startswith("X-AppManager-Source-URI=")
            ),
            "",
        )
        parsed_source_uri = urlsplit(source_uri)
        if parsed_source_uri.scheme == "file":
            integrated_sources.add(unquote(parsed_source_uri.path))

        name, _, _, _, icon, _ = parse_desktop_file(str(desktop_path))
        try:
            installed_at = datetime.fromtimestamp(
                appimage.stat().st_mtime
            ).date().isoformat()
        except OSError:
            installed_at = None
        apps.append(
            AppEntry(
                name=name or appimage.stem,
                manager="AppImage",
                source="Integrated AppImage",
                package_id=str(appimage),
                installed_at=installed_at,
                category="Other",
                is_gui_app=True,
                details=str(appimage),
                icon=icon,
                exec_name=appimage.stem.lower(),
                removal_paths=[str(desktop_path), str(appimage)],
            )
        )

    search_dirs = [
        home / "Applications",
        home / "Downloads",
        Path("/opt"),
        Path("/usr/local/bin"),
    ]

    for directory in search_dirs:
        if not directory.exists():
            continue

        try:
            for path in directory.iterdir():
                if not path.is_file() or path.suffix.lower() != ".appimage":
                    continue
                try:
                    if str(path.resolve()) in integrated_sources:
                        continue
                except OSError:
                    pass
                try:
                    st = path.stat()
                    installed_at = datetime.fromtimestamp(st.st_mtime).date().isoformat()
                except Exception:
                    installed_at = None

                if any(app.package_id == str(path) for app in apps):
                    continue

                name = path.stem.replace("_", " ").replace("-", " ").title()
                exec_name = path.stem.lower()

                apps.append(
                    AppEntry(
                        name=name,
                        manager="AppImage",
                        source="AppImage file",
                        package_id=str(path),
                        version="",
                        installed_at=installed_at,
                        category="Other",
                        is_gui_app=True,
                        details=str(path),
                        icon="",
                        exec_name=exec_name,
                    )
                )
        except Exception:
            continue

    return apps


def scan_manual_apps(
    desktop_dirs=None,
    binary_dirs=None,
) -> List[AppEntry]:
    """Detect unmanaged desktop launchers and standalone user-installed executables."""
    home = Path.home()
    if desktop_dirs is None:
        desktop_dirs = [
            home / ".local/share/applications",
            Path("/usr/local/share/applications"),
        ]
    if binary_dirs is None:
        binary_dirs = [
            home / ".local/bin",
            home / "bin",
            home / ".opencode/bin",
            home / "Applications",
            Path("/usr/local/bin"),
            Path("/opt"),
        ]

    apps = []
    manual_exec_names = set()
    seen_paths = set()

    for directory in desktop_dirs:
        if not directory.is_dir():
            continue

        try:
            desktop_files = directory.glob("*.desktop")
            for desktop_path in desktop_files:
                name, categories, no_display, hidden, icon, exec_line = parse_desktop_file(
                    str(desktop_path)
                )
                try:
                    with desktop_path.open("r", encoding="utf-8", errors="replace") as desktop_file:
                        if any(
                            line.strip() == "X-AppManager-Installed-AppImage=true"
                            for line in desktop_file
                        ):
                            continue
                except OSError:
                    continue
                if no_display or hidden or not name or not exec_line:
                    continue

                if get_package_owner(str(desktop_path)):
                    continue

                exec_name = normalize_exec(exec_line)
                if not exec_name:
                    continue

                try:
                    installed_at = datetime.fromtimestamp(
                        desktop_path.stat().st_mtime
                    ).date().isoformat()
                except OSError:
                    installed_at = None

                manual_exec_names.add(exec_name)
                try:
                    seen_paths.add(desktop_path.resolve())
                except (OSError, RuntimeError):
                    pass
                apps.append(
                    AppEntry(
                        name=name,
                        manager="Manual",
                        source="Manual installation",
                        package_id=str(desktop_path),
                        installed_at=installed_at,
                        category=map_categories(categories),
                        is_gui_app=True,
                        details=str(desktop_path),
                        icon=icon,
                        exec_name=exec_name,
                        removal_paths=[str(desktop_path)],
                    )
                )
        except OSError:
            continue

    executable_candidates = []
    for directory in binary_dirs:
        if not directory.is_dir():
            continue

        try:
            for path in directory.iterdir():
                if path.name.startswith("."):
                    continue

                if path.is_file():
                    executable_candidates.append(path)
                elif directory in {home / "Applications", Path("/opt")} and path.is_dir():
                    try:
                        children = [
                            candidate
                            for candidate in path.iterdir()
                            if candidate.is_file() and os.access(candidate, os.X_OK)
                        ]
                        matching_name = next(
                            (
                                candidate
                                for candidate in children
                                if candidate.stem.lower() == path.name.lower()
                            ),
                            None,
                        )
                        if matching_name:
                            executable_candidates.append(matching_name)
                        elif len(children) == 1:
                            executable_candidates.append(children[0])
                    except OSError:
                        continue
        except OSError:
            continue

    executable_candidates = [
        path
        for path in executable_candidates
        if (
            path.suffix.lower() != ".appimage"
            and os.access(path, os.X_OK)
            and not get_package_owner(str(path))
        )
    ]

    for path in executable_candidates:
        try:
            resolved_path = path.resolve()
        except (OSError, RuntimeError):
            continue

        if resolved_path in seen_paths:
            continue

        exec_name = path.name.lower()
        if exec_name in manual_exec_names:
            continue

        try:
            stat = path.stat()
            installed_at = datetime.fromtimestamp(stat.st_mtime).date().isoformat()
        except OSError:
            installed_at = None

        name = path.stem.replace("_", " ").replace("-", " ").title()
        apps.append(
            AppEntry(
                name=name,
                manager="Manual",
                source="User executable",
                package_id=str(path),
                installed_at=installed_at,
                category="Other",
                is_gui_app=True,
                details=str(path),
                exec_name=exec_name,
                removal_paths=[str(path)],
            )
        )

    executables_by_name = {}
    for path in executable_candidates:
        executables_by_name.setdefault(path.name.lower(), []).append(str(path))

    for app in apps:
        if app.source != "Manual installation":
            continue
        app.removal_paths.extend(
            executables_by_name.get(app.exec_name.lower(), [])
        )

    return apps


def scan_all() -> List[AppEntry]:
    apps: List[AppEntry] = []

    scanners = [
        scan_apt,
        scan_snap,
        scan_flatpak,
        scan_appimage,
        scan_manual_apps,
    ]

    for scanner in scanners:
        try:
            apps.extend(scanner())
        except Exception as e:
            print(f"{scanner.__name__} failed: {e}")

    apps.sort(key=lambda x: (x.manager, x.name.lower()))

    return apps
