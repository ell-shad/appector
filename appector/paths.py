"""Filesystem locations, honouring XDG and migrating the legacy layout.

Single source of truth for every directory Appector writes to. The legacy
paths (~/.local/state/app-manager, ~/.cache/app-manager) are migrated once,
lazily: their per-user files are copied into the new location, the old
directory is kept untouched, and everything is written to the new location
from then on. XDG_STATE_HOME / XDG_CACHE_HOME are honoured when set to an
absolute path.
"""

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

APP_NAME = "appector"
LEGACY_APP_NAME = "app-manager"

_LEGACY_MIGRATED = set()


def _home() -> Path:
    return Path.home()


def _legacy_state_dir() -> Path:
    return _home() / ".local" / "state" / LEGACY_APP_NAME


def state_dir() -> Path:
    """Per-user state directory (marks, activity log, purge backups)."""
    preferred = _preferred_state_dir()
    _migrate_legacy_state(_legacy_state_dir(), preferred)
    return _ensure_private(preferred)


def _preferred_state_dir() -> Path:
    value = os.environ.get("XDG_STATE_HOME")
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate / APP_NAME
    return _home() / ".local" / "state" / APP_NAME


def cache_dir() -> Path:
    """Per-user cache directory (staged .deb reviews and similar scratch)."""
    value = os.environ.get("XDG_CACHE_HOME")
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return _ensure_private(candidate / APP_NAME)
    preferred = _home() / ".cache" / APP_NAME
    legacy = _home() / ".cache" / LEGACY_APP_NAME
    if legacy.is_dir() and not preferred.is_dir():
        # Old staging areas still hold pending reviews; keep using them
        # until a scan sweeps the stale ones, then the new name takes over.
        return legacy
    return _ensure_private(preferred)


def appimage_dir() -> Path:
    """Per-user directory holding Appector-managed AppImage copies."""
    return _home() / ".local" / "share" / APP_NAME / "appimages"


def legacy_appimage_dir() -> Path:
    """Directory used for AppImage copies before the rename to Appector."""
    return _home() / ".local" / "share" / LEGACY_APP_NAME / "appimages"


def is_acceptable_private_dir(path: Path) -> bool:
    """Security check shared by the log writer and the purge-backup writer.

    A private directory must not be a symlink, and must sit inside the home
    directory — unless the user explicitly redirected it through an XDG
    variable, in which case the configured directory itself is accepted.
    """
    try:
        if path.is_symlink():
            return False
        if path.resolve().is_relative_to(_home().resolve()):
            return True
    except OSError:
        return False
    return path.resolve() == _preferred_state_dir().resolve()


def _ensure_private(path: Path, mode: int = 0o700) -> Path:
    """Create *path* (and parents) with user-only permissions."""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        if path.is_symlink():
            raise OSError(f"Refusing to use a symlinked state directory: {path}")
        os.chmod(path, mode)
    except OSError as error:
        logger.warning("Appector could not create its directory %s: %s", path, error)
    return path


def _migrate_legacy_state(legacy: Path, preferred: Path) -> None:
    """Copy legacy per-user state into the new location, once, best-effort.

    The legacy directory is kept (never deleted) so a downgrade keeps
    working; failures are logged and non-fatal. `preferred` may point
    outside the home via XDG_STATE_HOME, which the copy honours.
    """
    marker = (str(legacy), str(preferred))
    if marker in _LEGACY_MIGRATED:
        return
    _LEGACY_MIGRATED.add(marker)

    try:
        if legacy == preferred or not legacy.is_dir() or legacy.is_symlink():
            return
        _ensure_private(preferred)
        migrated_any = False

        for name in ("marked.json", "actions.log", "marked.json.tmp"):
            source = legacy / name
            destination = preferred / name
            if source.is_file() and not source.is_symlink() and not destination.exists():
                shutil.copy2(source, destination)
                migrated_any = True

        source_backups = legacy / "purge-backups"
        destination_backups = preferred / "purge-backups"
        if source_backups.is_dir() and not destination_backups.exists():
            shutil.copytree(source_backups, destination_backups, symlinks=True)
            migrated_any = True

        if migrated_any:
            logger.info(
                "Migrated legacy Appector state from %s to %s; the old "
                "directory is kept untouched.",
                legacy,
                preferred,
            )
    except OSError as error:
        logger.warning("Appector could not migrate its legacy state: %s", error)
