"""Installed-application discovery.

Scanners for APT, Snap, Flatpak, AppImage and "manual" installs, plus the
residual-configuration (leftover) scanner. Every subprocess call carries a
timeout (F6), every parse is locale-independent (LC_ALL=C), and dpkg owner
lookups are batched into a single call per scan.

The desktop-file parser honours group boundaries ([Desktop Entry] only,
F7), and APT identities are `name:arch` everywhere (F4, S2).
"""

import configparser
import gzip
import os
import re
import shlex
import shutil
import subprocess
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .models import AppEntry
from .paths import appimage_dir, legacy_appimage_dir
from .policy import SNAP_SKIP_NAMES, _base_package_name

# Per-call timeouts: a hung snapd or flatpak must never freeze the scan.
SCAN_TIMEOUT = 60
LIST_TIMEOUT = 30

# Wrapper commands whose argument is the real program (F3). Unwrapped while
# another candidate token remains.
EXEC_WRAPPERS = {
    "env",
    "exec",
    "sh",
    "bash",
    "dash",
    "sudo",
    "pkexec",
    "flatpak",
    "snap",
    "gtk-launch",
    "gamemoderun",
    "gamemoderun-wrapper",
    "dbus-launch",
    "nice",
    "nohup",
    "optirun",
    "primusrun",
    "vglrun",
}

# Sub-commands to skip while unwrapping a wrapper before the real target.
EXEC_SUBCOMMANDS = {"run", "spread", "launch", "shell"}

# Shell names whose `-c` argument is itself a command line.
_EXEC_SHELLS = {"sh", "bash", "dash", "rbash", "zsh"}

FIELD_CODE_RE = re.compile(r"%[a-zA-Z%]")
ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# ------------------------------------------------------------
# Desktop file helpers
# ------------------------------------------------------------


def _read_desktop_entry(path) -> configparser.ConfigParser:
    """Parse only the [Desktop Entry] group of a .desktop file (F7).

    `configparser` with interpolation off and strict off accepts the real
    world's desktop files (duplicate keys, % placeholders) while keeping
    group boundaries, so `NoDisplay` inside a [Desktop Action ...] group no
    longer hides the whole application.
    """
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=False,
        delimiters=("=",),
        inline_comment_prefixes=("#",),
    )
    parser.optionxform = str.lower
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error):
        pass
    return parser


def _entry_value(parser, key: str, locale: str = "") -> str:
    if not parser.has_section("Desktop Entry"):
        return ""
    entry = parser["Desktop Entry"]
    if key in entry and str(entry[key] or "").strip():
        return str(entry[key]).strip()
    if locale:
        for suffix in (f"[{locale}]", f"[{locale.split('_', 1)[0]}]"):
            for candidate in entry:
                if candidate.lower() == f"{key}{suffix}".lower():
                    value = str(entry[candidate] or "").strip()
                    if value:
                        return value
    else:
        # Any localized variant beats an empty plain key.
        for candidate in entry:
            if candidate.lower().startswith(f"{key}["):
                value = str(entry[candidate] or "").strip()
                if value:
                    return value
    return ""


def _desktop_visible(parser) -> bool:
    """F7: every hide/show rule evaluated for the [Desktop Entry] group."""
    entry = parser["Desktop Entry"] if parser.has_section("Desktop Entry") else {}

    def flag(key: str) -> bool:
        return str(entry.get(key, "")).strip().lower() in {"true", "1"}

    if flag("nodisplay") or flag("hidden"):
        return False

    entry_type = str(entry.get("type", "")).strip().lower()
    if entry_type and entry_type != "application":
        return False

    try_exec = str(entry.get("tryexec", "")).strip()
    if try_exec and not shutil.which(try_exec):
        # A resolved path (with a slash) that is not executable also hides.
        if "/" in try_exec or not shutil.which(try_exec):
            if not (Path(try_exec).is_file() and os.access(try_exec, os.X_OK)):
                return False

    current_desktops = {
        value.strip().lower()
        for value in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")
        if value.strip()
    }
    not_show_in = {
        value.strip().lower()
        for value in str(entry.get("notshowin", "")).split(";")
        if value.strip()
    }
    only_show_in = {
        value.strip().lower()
        for value in str(entry.get("onlyshowin", "")).split(";")
        if value.strip()
    }
    if current_desktops:
        if current_desktops & not_show_in:
            return False
        if only_show_in and not (current_desktops & only_show_in):
            return False
    elif only_show_in:
        # Restricted to desktops we cannot identify; an uninstaller should
        # still let the user see and remove the entry.
        pass

    return True


def parse_desktop_file(path: str) -> tuple[str, list[str], bool, bool, str, str]:
    """Return (name, categories, no_display, hidden, icon, exec) for one file.

    `no_display` is True when the entry must not be listed at all (F7):
    NoDisplay/Hidden, non-Application types, missing TryExec, or a ShowIn
    restriction that excludes the running desktop.
    """
    parser = _read_desktop_entry(path)
    if not parser.has_section("Desktop Entry"):
        return "", [], False, False, "", ""

    entry = parser["Desktop Entry"]
    name = _entry_value(parser, "name")
    icon = _entry_value(parser, "icon")
    exec_line = _entry_value(parser, "exec")
    categories = [
        value for value in str(entry.get("categories", "")).split(";") if value
    ]

    visible = _desktop_visible(parser)
    explicit_hidden = str(entry.get("hidden", "")).strip().lower() in {"true", "1"}
    return name, categories, not visible, explicit_hidden, icon, exec_line


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


_SNAP_ICON_CACHE: dict[str, tuple[float, str]] = {}


def find_snap_icon(snap_name: str) -> str:
    """Snap launcher icon, cached per snapd desktop directory generation (F6)."""
    desktop_dir = Path("/var/lib/snapd/desktop/applications")
    if not desktop_dir.exists():
        return ""

    try:
        generation = desktop_dir.stat().st_mtime
    except OSError:
        generation = 0.0

    cached = _SNAP_ICON_CACHE.get(snap_name)
    if cached and cached[0] == generation:
        return cached[1]

    icon = _find_snap_icon_uncached(desktop_dir, snap_name)
    _SNAP_ICON_CACHE[snap_name] = (generation, icon)
    return icon


def _find_snap_icon_uncached(desktop_dir: Path, snap_name: str) -> str:
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
                text = path.read_text(encoding="utf-8", errors="replace")
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
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("Icon="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def map_categories(categories: list[str]) -> str:
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


# ------------------------------------------------------------
# dpkg ownership (batched, F6)
# ------------------------------------------------------------


def _dpkg_env():
    return {**os.environ, "LC_ALL": "C"}


def _escape_dpkg_glob(path: str) -> str:
    """Escape fnmatch metacharacters so dpkg-query -S matches literally."""
    return re.sub(r"([*?\[\]\\])", r"\\\1", path)


def get_package_owners(paths) -> dict[str, str]:
    """Map each path to its owning dpkg package in one dpkg-query call.

    Paths that no package owns (or that cannot be resolved) are absent from
    the result. Diversion lines are ignored.
    """
    unique_paths = list(dict.fromkeys(str(p) for p in paths if str(p or "").strip()))
    if not unique_paths:
        return {}

    try:
        # stderr=DEVNULL is redundant with capture_output and would raise
        # ValueError ("stdout and stderr arguments may not be used with
        # capture_output") on every call; stdout/stderr come from
        # capture_output instead.
        #
        # dpkg-query -S treats its arguments as glob patterns, so
        # metacharacters in file names ([...] in a user launcher, say)
        # must be escaped for a literal match (F11).
        result = subprocess.run(
            [
                "dpkg-query",
                "-S",
                "--",
                *(_escape_dpkg_glob(p) for p in unique_paths),
            ],
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=SCAN_TIMEOUT,
            env=_dpkg_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}

    owners: dict[str, str] = {}
    for line in result.stdout.splitlines():
        # dpkg-query -S prints "package[, local: ...]: /absolute/path".
        # The path follows the LAST colon on the line: package names can
        # contain colons (name:arch), paths cannot (they are absolute).
        package, separator, path = line.rpartition(":")
        if not separator:
            continue
        # Multi-owner lines list several packages, comma-separated;
        # the first owner wins.
        package = package.split(",")[0].strip()
        path = path.strip()
        if not path.startswith("/") or not package:
            continue
        # "diversion by <pkg> from:" and "local:" are dpkg annotations,
        # not ownership records.
        if package.startswith("diversion") or package in {"local", "local:"}:
            continue
        if path not in owners:
            owners[path] = package
    return owners


def get_package_owner(path: str) -> str | None:
    return get_package_owners([path]).get(str(path))


# ------------------------------------------------------------
# Leftover scanner
# ------------------------------------------------------------


def scan_leftover_configs() -> list[AppEntry]:
    """Find APT packages in dpkg's residual-config state (removed, config remains).

    Identities are ``name:arch`` (S2): a bare name would let
    ``apt-get purge libfoo`` resolve to the native architecture and purge an
    installed instance of another architecture.
    """
    result = subprocess.run(
        [
            "dpkg-query",
            "-W",
            "-f=${db:Status-Abbrev}\t${binary:Package}\n",
        ],
        text=True,
        errors="replace",
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=LIST_TIMEOUT,
        env=_dpkg_env(),
    )
    if result.returncode != 0:
        raise OSError(
            result.stderr.strip() or "Could not list residual configurations."
        )

    apps: list[AppEntry] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        package_name = parts[1].strip()
        if status[:2] != "rc" or not package_name:
            continue
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
                    "Package is already removed; only its configuration files "
                    "remain. Purging deletes those files."
                ),
                icon="",
            )
        )
    return apps


# ------------------------------------------------------------
# Install dates
# ------------------------------------------------------------


def _read_log_lines(path: Path, max_bytes: int = 1 * 1024 * 1024) -> list[str]:
    """Read the (bounded) tail of a log file.

    Log files are root-owned in practice, but a hostile multi-gigabyte file
    in a scanned location must not exhaust memory: at most ``max_bytes``
    are read and only the tail is kept.
    """
    try:
        if path.suffix == ".gz":
            # Gzip cannot seek from the end cheaply; stream and keep the tail.
            tail: deque[str] = deque()
            tail_bytes = 0
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    tail.append(line)
                    tail_bytes += len(line)
                    while tail_bytes > max_bytes and len(tail) > 1:
                        tail_bytes -= len(tail.popleft())
            return list(tail)
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
                f.readline()  # drop the likely partial first line
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace").splitlines(keepends=True)
    except (OSError, EOFError, UnicodeError):
        return []


# "libfoo:amd64 (1.0, automatic), libbar (2.0)" — name[:arch], not the old
# split(",") which produced the bogus package "automatic)" (F5).
APT_HISTORY_ENTRY_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9.+~_-]*)(?::([A-Za-z0-9][A-Za-z0-9._-]*))?"
    r"\s*\(([^)]*)\)"
)


def get_apt_install_dates(package_ids=None) -> dict[str, str]:
    """First install date per package, keyed by ``name:arch`` (F5).

    Sources, in order: /var/log/dpkg.log*, /var/log/apt/history.log*,
    then the mtime of /var/lib/dpkg/info/<pkg>.list for packages whose
    install predates every retained log (the review's rotation fallback).
    """
    dates: dict[str, str] = {}
    log_dir = Path("/var/log")

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
                    package_name = parts[3]
                    previous_state = parts[4]

                    if previous_state == "<none>" and package_name:
                        existing = dates.get(package_name)

                        if not existing or date_str < existing:
                            dates[package_name] = date_str

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

                elif line.startswith(("Install:", "Upgrade:")) and current_date:
                    value = line.split(":", 1)[1]

                    for match in APT_HISTORY_ENTRY_RE.finditer(value):
                        name = match.group(1)
                        arch = match.group(2)
                        entry_id = f"{name}:{arch}" if arch else name
                        existing = dates.get(entry_id)

                        if not existing or current_date < existing:
                            dates[entry_id] = current_date

    if package_ids:
        for package_id in package_ids:
            if dates.get(package_id):
                continue
            fallback = _dpkg_list_date(package_id)
            if fallback:
                dates[package_id] = fallback

    return dates


def _dpkg_list_date(package_id: str) -> str | None:
    """mtime of /var/lib/dpkg/info/<pkg>.list (F5 rotation fallback)."""
    info_dir = Path("/var/lib/dpkg/info")
    candidates = [info_dir / f"{package_id}.list"]
    base = _base_package_name(package_id)
    if base != package_id:
        candidates.append(info_dir / f"{base}.list")
    else:
        # The id has no arch; accept the native-arch list too.
        try:
            candidates.extend(info_dir.glob(f"{base}:*.list"))
        except OSError:
            pass
    for candidate in candidates:
        try:
            return datetime.fromtimestamp(candidate.stat().st_mtime).date().isoformat()
        except OSError:
            continue
    return None


# ------------------------------------------------------------
# APT source labels (F8)
# ------------------------------------------------------------

_KNOWN_APT_ORIGINS = (
    ("debian", "Debian"),
    ("ubuntu", "Ubuntu repository"),
    ("raspbian", "Raspbian"),
    ("linux mint", "Linux Mint"),
    ("linuxmint", "Linux Mint"),
    ("pop!os", "Pop!_OS"),
    ("pop_os", "Pop!_OS"),
    ("system76", "Pop!_OS"),
    ("kali", "Kali"),
)


def get_apt_source_map(package_names: list[str]) -> dict[str, str]:
    """Detect where an installed APT package came from.

    Works from the Origin/Label/Site fields of the index files, generically
    across Debian/Ubuntu/Mint/Pop!_OS/Raspbian; PPAs are matched on the
    ppa.launchpad(content).net host rather than any path containing "ppa".
    python3-apt is optional — without it no labels are produced.
    """
    source_map: dict[str, str] = {}

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
                index_label = getattr(file_item, "label", "") or ""
            except Exception:
                continue

            if filename == "/var/lib/dpkg/status":
                continue

            has_repository = True

            combined = " ".join((filename, site, origin, index_label)).lower()

            if re.search(r"ppa\.launchpad(?:content)?\.net", combined):
                label = "PPA"
                break

            matched = False
            for needle, display in _KNOWN_APT_ORIGINS:
                if needle in origin.lower() or needle in index_label.lower():
                    label = display
                    matched = True
                    break
            if matched:
                continue

            for needle, display in _KNOWN_APT_ORIGINS:
                if f"{needle}.com" in site.lower() or f"{needle}.org" in site.lower():
                    label = display
                    matched = True
                    break
            if matched:
                continue

            if label == "Local .deb":
                label = "Third-party repository"

        if not has_repository:
            label = "Local .deb"

        source_map[package_name] = label

    return source_map


# ------------------------------------------------------------
# Exec parsing (F3)
# ------------------------------------------------------------


def normalize_exec(exec_line: str) -> str:
    """Extract the identifying executable from a desktop Exec= line.

    Wrappers (env, flatpak run, snap run, gamemoderun, pkexec, sh -c ...)
    are unwrapped, field codes (%u, %f, ...) are dropped, and quoted paths
    survive via shlex. Returns the lowercased basename of the real target,
    or "" when nothing identifiable remains.
    """
    if not exec_line:
        return ""

    try:
        tokens = shlex.split(exec_line, posix=True)
    except ValueError:
        tokens = exec_line.split()

    return _extract_exec_target(tokens)


def _extract_exec_target(tokens: list[str]) -> str:
    index = 0
    total = len(tokens)

    while index < total:
        token = tokens[index]
        index += 1

        if token.startswith("%"):
            continue
        if ENV_ASSIGNMENT_RE.match(token):
            continue

        base = Path(token).name.lower()

        if base in _EXEC_SHELLS:
            # `sh -c 'exec foo'`: the token after -c is a script, parse it.
            script = None
            while index < total:
                candidate = tokens[index]
                if candidate == "-c":
                    index += 1
                    if index < total:
                        script = tokens[index]
                        index += 1
                    break
                if candidate.startswith("-"):
                    index += 1
                    continue
                script = candidate
                break
            if script:
                try:
                    inner = shlex.split(script, posix=True) if " " in script else [script]
                except ValueError:
                    inner = [script]
                result = _extract_exec_target(inner)
                if result:
                    return result
            continue

        if base in EXEC_WRAPPERS:
            # Skip wrapper options, env assignments and subcommands
            # ("flatpak run", "snap run") to reach the real target.
            while index < total:
                candidate = tokens[index]
                if candidate.startswith("-") and not ENV_ASSIGNMENT_RE.match(candidate):
                    index += 1
                    continue
                if ENV_ASSIGNMENT_RE.match(candidate):
                    index += 1
                    continue
                if Path(candidate).name.lower() in EXEC_SUBCOMMANDS:
                    index += 1
                    continue
                break
            continue

        return base

    return ""


# ------------------------------------------------------------
# APT scanner
# ------------------------------------------------------------


def _desktop_directories() -> list[Path]:
    return [
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        Path.home() / ".local/share/applications",
    ]


def _resolve_apt_id(owner: str, versions: dict[str, str]) -> str | None:
    """Match a dpkg -S owner (name or name:arch) to a scanned identity."""
    if owner in versions:
        return owner
    base = _base_package_name(owner)
    if base != owner and base in versions:
        return base
    # Owner without arch: prefer the native/first scanned instance.
    candidates = sorted(
        package_id
        for package_id in versions
        if _base_package_name(package_id) == base
    )
    return candidates[0] if len(candidates) == 1 else (candidates[0] if candidates else None)


def scan_apt() -> list[AppEntry]:
    """APT scanner: installed (ii) packages that own a .desktop launcher.

    Identities are ``name:arch`` (F4): the status comes from
    ${db:Status-Abbrev}, so half-configured (iU), not-installed and
    residual (rc) entries never appear as installed apps.
    """
    versions: dict[str, str] = {}

    try:
        out = subprocess.check_output(
            [
                "dpkg-query",
                "-W",
                "-f=${binary:Package}\t${db:Status-Abbrev}\t${Version}\n",
            ],
            text=True,
            errors="replace",
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=SCAN_TIMEOUT,
            env=_dpkg_env(),
        )
    except FileNotFoundError:
        # The tool is missing: scan_all reports this separately as
        # "tool-missing", and direct callers get an empty source.
        return []

    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        package_id, status_abbrev, version = parts[0], parts[1], parts[2]
        if status_abbrev[:2] == "ii" and package_id:
            versions[package_id] = version

    apps: list[AppEntry] = []
    seen_packages = set()

    # Deterministic order; multi-app packages prefer the entry whose file
    # name matches the package name (F6).
    for directory in _desktop_directories():
        if not directory.is_dir():
            continue

        try:
            entries = sorted(os.listdir(directory), key=str.lower)
        except Exception:
            continue

        desktop_files = [name for name in entries if name.endswith(".desktop")]
        owners = get_package_owners(
            [str(directory / name) for name in desktop_files]
        )

        by_package: dict[str, list[str]] = {}
        for name in desktop_files:
            path = str(directory / name)
            owner = owners.get(path)
            if not owner:
                continue
            resolved = _resolve_apt_id(owner, versions)
            if not resolved:
                continue
            by_package.setdefault(resolved, []).append(name)

        for package_id, names in by_package.items():
            if package_id in seen_packages:
                continue

            preferred = next(
                (
                    name for name in names
                    if name.rsplit(".", 1)[0].lower()
                    == _base_package_name(package_id).lower()
                ),
                names[0],
            )
            path = directory / preferred

            name, categories, no_display, hidden, icon, exec_line = (
                parse_desktop_file(str(path))
            )
            if no_display or hidden or not name:
                continue

            seen_packages.add(package_id)

            exec_name = normalize_exec(exec_line) or package_id.lower()

            apps.append(
                AppEntry(
                    name=name,
                    manager="APT",
                    source="APT / Debian",
                    package_id=package_id,
                    version=versions.get(package_id, ""),
                    installed_at=None,
                    category=map_categories(categories),
                    is_gui_app=True,
                    details=str(path),
                    icon=icon,
                    exec_name=exec_name,
                )
            )

    if apps:
        package_ids = [app.package_id for app in apps]
        install_dates = get_apt_install_dates(package_ids)
        source_map = get_apt_source_map(
            sorted({_base_package_name(pid) for pid in package_ids})
        )

        for app in apps:
            app.installed_at = (
                install_dates.get(app.package_id)
                or install_dates.get(_base_package_name(app.package_id))
            )

            source = source_map.get(_base_package_name(app.package_id))
            if source:
                app.source = source

    return apps


# ------------------------------------------------------------
# Snap scanner
# ------------------------------------------------------------


def get_snap_install_dates(revisions: dict[str, str]) -> dict[str, str]:
    """First install date per snap (changes log, then snap file mtimes)."""
    dates: dict[str, str] = {}

    commands = [
        ["snap", "changes", "--abs-time"],
        ["snap", "changes"],
    ]

    for cmd in commands:
        try:
            out = subprocess.check_output(
                cmd,
                text=True,
                errors="replace",
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=SCAN_TIMEOUT,
                env=_dpkg_env(),
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


def scan_snap() -> list[AppEntry]:
    try:
        out = subprocess.check_output(
            ["snap", "list"],
            text=True,
            errors="replace",
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=SCAN_TIMEOUT,
            env=_dpkg_env(),
        )
    except FileNotFoundError:
        # The tool is missing: scan_all reports this separately as
        # "tool-missing", and direct callers get an empty source.
        return []

    apps = []
    revisions: dict[str, str] = {}

    lines = out.splitlines()

    if len(lines) < 2:
        return []

    for line in lines[1:]:
        parts = line.split()

        if len(parts) < 5:
            continue

        name = parts[0]
        version = parts[1]
        revision = parts[2] if len(parts) > 2 else ""
        notes = " ".join(parts[5:]) if len(parts) > 5 else ""
        normalized_name = name.lower()

        # One shared definition of runtime/base snaps (S11): the policy set.
        if normalized_name in {pid.lower() for pid in SNAP_SKIP_NAMES}:
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


# ------------------------------------------------------------
# Flatpak scanner
# ------------------------------------------------------------


def find_flatpak_install_date(app_id: str, installation: str) -> str | None:
    """Best-effort Flatpak install date from deployment/desktop metadata."""
    installation_lower = (installation or "").lower()

    user_app_dir = Path.home() / ".local/share/flatpak/app" / app_id
    system_app_dir = Path("/var/lib/flatpak/app") / app_id

    candidates = []

    if "user" in installation_lower:
        candidates.extend((user_app_dir, system_app_dir))
    else:
        candidates.extend((system_app_dir, user_app_dir))

    expanded_candidates = []

    for candidate in candidates:
        if candidate.exists():
            expanded_candidates.append(candidate)

        current = candidate / "current"

        if current.exists():
            expanded_candidates.append(current)

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
        expanded_candidates.extend((user_desktop, system_desktop))
    else:
        expanded_candidates.extend((system_desktop, user_desktop))

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


def _flatpak_exec_name(app_id: str, installation: str) -> str:
    """Exec identity from the exported .desktop entry (F2), else empty."""
    if "user" in (installation or "").lower():
        candidates = [
            Path.home() / ".local/share/flatpak/exports/share/applications",
            Path("/var/lib/flatpak/exports/share/applications"),
        ]
    else:
        candidates = [
            Path("/var/lib/flatpak/exports/share/applications"),
            Path.home() / ".local/share/flatpak/exports/share/applications",
        ]

    for directory in candidates:
        desktop = directory / f"{app_id}.desktop"
        if not desktop.exists():
            continue
        _name, _categories, _no_display, _hidden, _icon, exec_line = (
            parse_desktop_file(str(desktop))
        )
        exec_name = normalize_exec(exec_line)
        if exec_name and exec_name not in {"flatpak", "app"}:
            return exec_name
    return ""


def scan_flatpak() -> list[AppEntry]:
    try:
        out = subprocess.check_output(
            [
                "flatpak",
                "list",
                "--app",
                "--columns=name,application,version,origin,installation",
            ],
            text=True,
            errors="replace",
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=SCAN_TIMEOUT,
            env=_dpkg_env(),
        )
    except FileNotFoundError:
        # The tool is missing: scan_all reports this separately as
        # "tool-missing", and direct callers get an empty source.
        return []

    apps = []

    for line in out.splitlines():
        cols = line.split("\t")

        if len(cols) < 5:
            continue

        display_name = cols[0].strip()
        app_id = cols[1].strip()
        version = cols[2].strip()
        origin = cols[3].strip()
        installation = cols[4].strip()

        if not app_id:
            continue

        # Real display name from flatpak itself (F2); the ID's last segment
        # is only a fallback and never an identity for duplicate matching.
        name = display_name or app_id.split(".")[-1].replace("_", " ").replace("-", " ").title()
        source = origin if origin else installation

        icon = find_flatpak_icon(app_id)
        installed_at = find_flatpak_install_date(app_id, installation)
        exec_name = _flatpak_exec_name(app_id, installation)

        apps.append(
            AppEntry(
                name=name,
                manager="Flatpak",
                source=source,
                package_id=app_id,
                version=version,
                installed_at=installed_at,
                category="Apps",
                is_gui_app=True,
                details=f"Installation: {installation}",
                icon=icon,
                exec_name=exec_name,
                installation=installation,
            )
        )

    return apps


# ------------------------------------------------------------
# AppImage scanner
# ------------------------------------------------------------


def _managed_appimage_roots() -> list[Path]:
    return [appimage_dir(), legacy_appimage_dir()]


def _is_managed_appimage_path(raw_path: str) -> bool:
    """S8: the launcher must point lexically inside a managed AppImage dir.

    The value is resolved *without* following symlinks (".." is rejected by
    normalization escaping the root), so `.../appimages/../../evil` fails.
    """
    if not raw_path:
        return False
    if ".." in Path(raw_path).parts:
        return False
    normalized = Path(os.path.normpath(raw_path))
    if not normalized.is_absolute():
        return False
    for root in _managed_appimage_roots():
        try:
            if normalized.is_relative_to(Path(os.path.normpath(str(root)))):
                return True
        except OSError:
            continue
    return False


def scan_appimage() -> list[AppEntry]:
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
        if not appimage_path or not _is_managed_appimage_path(appimage_path):
            continue
        appimage = Path(appimage_path)
        if not appimage.is_file():
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
            for path in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
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


# ------------------------------------------------------------
# Manual-install scanner
# ------------------------------------------------------------


def _default_binary_dirs(home: Path) -> list[Path]:
    """Common per-user tool locations (F10); developer-specific paths are
    no longer hard-coded into discovery."""
    return [
        home / ".local/bin",
        home / "bin",
        home / ".cargo/bin",
        home / "go/bin",
        home / ".npm-global/bin",
        home / "Applications",
        Path("/usr/local/bin"),
        Path("/opt"),
    ]


def _classify_manual_launcher(exec_line: str) -> tuple[str, str]:
    """Return (source, category) hints for known non-app launcher types (F10)."""
    lowered = exec_line.lower()
    if re.search(r"(google-chrome|chromium|chrome).*--app=", lowered):
        return "Chrome/Chromium web app", "Internet"
    if re.search(r"\b(steam|steamcmd)\b|steam://", lowered):
        return "Steam shortcut", "Games"
    if re.search(r"\bwine(\b|\.exe)", lowered) or lowered.endswith(".exe"):
        return "Wine program", "Other"
    if re.search(r"\belectron\b", lowered) or "appimage" in lowered:
        return "Bundled app", "Other"
    return "Manual installation", ""


def _resolve_exec_target(exec_line: str) -> Path | None:
    """Absolute path of the executed target, without running anything (F10).

    Only the first non-wrapper token is considered; the result must exist.
    """
    if not exec_line:
        return None
    try:
        tokens = shlex.split(exec_line, posix=True)
    except ValueError:
        tokens = exec_line.split()
    for token in tokens:
        if not token or token.startswith("-") or token.startswith("%"):
            continue
        if ENV_ASSIGNMENT_RE.match(token):
            continue
        candidate = Path(token).expanduser()
        if candidate.is_absolute():
            return candidate if candidate.is_file() else None
        resolved = shutil.which(token)
        return Path(resolved) if resolved else None
    return None


def scan_manual_apps(
    desktop_dirs=None,
    binary_dirs=None,
    extra_dirs=None,
) -> list[AppEntry]:
    """Detect unmanaged desktop launchers and standalone user executables.

    ``extra_dirs`` (F10) are additional user-chosen scan directories; they
    extend, never replace, the defaults.
    """
    home = Path.home()
    if desktop_dirs is None:
        desktop_dirs = [
            home / ".local/share/applications",
            Path("/usr/local/share/applications"),
        ]
    if binary_dirs is None:
        binary_dirs = _default_binary_dirs(home)
    if extra_dirs:
        # User-configured scan locations; existing entries are skipped so a
        # directory cannot be scanned twice.
        for extra in extra_dirs:
            candidate = Path(str(extra)).expanduser()
            if candidate not in binary_dirs:
                binary_dirs.append(candidate)

    apps = []
    manual_exec_names = set()
    seen_paths = set()

    launcher_paths = []
    for directory in desktop_dirs:
        if not directory.is_dir():
            continue
        try:
            launcher_paths.extend(
                directory.glob("*.desktop")
            )
        except OSError:
            continue

    launcher_owners = get_package_owners([str(p) for p in launcher_paths])

    for desktop_path in launcher_paths:
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

        if launcher_owners.get(str(desktop_path)):
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

        source, category_hint = _classify_manual_launcher(exec_line)
        removal_paths = [str(desktop_path)]
        # F10: paths derived from the resolved Exec target only, never by
        # name-matching executables in other directories.
        exec_target = _resolve_exec_target(exec_line)
        if (
            exec_target is not None
            and exec_target.is_file()
            and str(exec_target) != str(desktop_path)
        ):
            removal_paths.append(str(exec_target))

        apps.append(
            AppEntry(
                name=name,
                manager="Manual",
                source=source,
                package_id=str(desktop_path),
                installed_at=installed_at,
                category=category_hint or map_categories(categories),
                is_gui_app=True,
                details=str(desktop_path),
                icon=icon,
                exec_name=exec_name,
                removal_paths=removal_paths,
            )
        )

    executable_candidates = []
    for directory in binary_dirs:
        if not directory.is_dir():
            continue

        try:
            for path in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
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

    executable_owners = get_package_owners(
        [str(path) for path in executable_candidates]
    )
    executable_candidates = [
        path
        for path in executable_candidates
        if (
            path.suffix.lower() != ".appimage"
            and os.access(path, os.X_OK)
            and not executable_owners.get(str(path))
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
            st = path.stat()
            installed_at = datetime.fromtimestamp(st.st_mtime).date().isoformat()
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
                # F10: a bare executable with no .desktop launcher is a CLI
                # tool, not a GUI app; it stays listed but is not treated
                # as a duplicate candidate or an "Application".
                is_gui_app=False,
                details=str(path),
                exec_name=exec_name,
                removal_paths=[str(path)],
            )
        )

    return apps


# ------------------------------------------------------------
# Combined scan
# ------------------------------------------------------------


@dataclass
class ScanResult:
    """Outcome of a full scan (F6): apps plus per-source status.

    `source_status` entries are ``(source, status, message)`` with status one
    of "ok", "tool-missing", "failed" — so the UI can tell "Snap is not
    installed" from "the Snap scan failed", instead of showing a silent
    short count.
    """

    apps: list[AppEntry] = field(default_factory=list)
    source_status: list[tuple[str, str, str]] = field(default_factory=list)

    # Sequence protocol: callers may treat a ScanResult as the app list.
    def __iter__(self):
        return iter(self.apps)

    def __len__(self):
        return len(self.apps)

    def __bool__(self):
        return bool(self.apps)


_SCANNERS = (
    ("APT", "dpkg-query", scan_apt),
    ("Snap", "snap", scan_snap),
    ("Flatpak", "flatpak", scan_flatpak),
    ("AppImage", None, scan_appimage),
    ("Manual", None, scan_manual_apps),
)


def _run_one_scanner(source: str, tool: str | None, scanner):
    if tool is not None and shutil.which(tool) is None:
        return source, [], (source, "tool-missing", f"{tool} is not installed.")
    try:
        apps = scanner() or []
        return source, apps, (source, "ok", f"{len(apps)} item(s)")
    except Exception as error:  # a failing source must not abort the scan
        return source, [], (source, "failed", str(error))


def scan_all() -> ScanResult:
    """Run every scanner (in parallel, F6) and report per-source status."""
    result = ScanResult()

    with ThreadPoolExecutor(max_workers=len(_SCANNERS)) as pool:
        futures = [
            pool.submit(_run_one_scanner, source, tool, scanner)
            for source, tool, scanner in _SCANNERS
        ]
        for future in futures:
            _source, apps, status = future.result()
            result.apps.extend(apps)
            result.source_status.append(status)

    result.apps.sort(key=lambda x: (x.manager, x.name.lower()))
    return result
