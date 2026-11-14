"""APT residual-configuration purge.

Owns the entire "purge packages that are already removed but still have
configuration on disk" flow: previews, safety gates, the pre-purge backup, and
the privileged purge itself.

Safety model (see ADR 0002):

    validate names -> verify dpkg rc state -> simulate the purge -> back up
    conffiles -> RE-VERIFY rc state -> purge -> verify residuals are gone

The rc re-check after the backup closes the reinstall-then-purge window: the
backup is slow, and a package reinstalled during it must not be purged as if
it were only residual configuration.

Multiarch (S2): every identity in this flow is ``name:arch`` as printed by
``${binary:Package}``. A bare name could mean any architecture, and
``apt-get purge libfoo`` resolves to the *native* architecture — purging an
installed package when only the ``:i386`` instance was residual. Packages
whose base name has any non-rc instance on another architecture are refused
outright.

Backups cover dpkg-registered conffiles only. Maintainer `postrm` scripts can
delete more, which is why the APT simulation is the authoritative preview.
Appector never restores automatically.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import helper_client
from .helper_client import HelperUnavailable
from ._proc import _log_action, _run_command_raw
from .paths import is_acceptable_private_dir, state_dir
from .plans import Plan
from .policy import (
    _base_package_name,
    _deduplicate_package_ids,
    _format_package_list,
    _is_safe_package_id,
    _package_arch,
    _parse_apt_simulation_removed_packages,
    _split_safe_package_ids,
    is_critical_apt_package_name,
)


# Residual-config purge limits. Purging only deletes configuration files of
# already-removed packages, but a huge batch can still fill the home
# filesystem with backup copies, so cap both the request and the backup.
RESIDUAL_MAX_PACKAGES = 100
RESIDUAL_MAX_BACKUP_FILES = 2000
RESIDUAL_MAX_BACKUP_BYTES = 100 * 1024 * 1024
RESIDUAL_PURGE_BACKUP_KEEP = 10

# dpkg conffile lines look like "<absolute path> <md5>" with an optional
# trailing marker such as "obsolete".
#
# This pattern is used two ways: per-line with `re.match` inside
# `_dpkg_conffile_entries`, and over the whole multi-line dpkg-query output
# with `finditer` for size estimation. Two details matter for the second use:
#   * `re.MULTILINE` so `^` matches at the start of every conffile line rather
#     than only the first line of the output.
#   * `[ \t]` instead of `\s` in the separators so the optional trailing group
#     can never swallow the newline plus following records.
CONFFILE_LINE_RE = re.compile(
    r"^[ \t]*(/\S+)[ \t]+([0-9a-f]{32})(?:[ \t]+.*)?$",
    re.MULTILINE,
)


def _dpkg_status_map(timeout=30):
    """Return ``{name:arch: status-abbrev}`` for every dpkg entry.

    Architecture-qualified (S2): this is the shared basis for the residual
    set and for the multiarch cross-instance check.
    """
    result = subprocess.run(
        [
            "dpkg-query",
            "-W",
            "-f=${db:Status-Abbrev}\t${binary:Package}\n",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode != 0:
        raise OSError(
            result.stderr.strip()
            or "Could not verify residual APT configurations."
        )
    status_map = {}
    for line in result.stdout.splitlines():
        status, separator, package = line.partition("\t")
        if separator and package:
            status_map[package.strip()] = status.strip()
    return status_map


def _dpkg_residual_package_set(timeout=30):
    """Return the set of ``name:arch`` packages currently in dpkg's rc state."""
    return {
        package
        for package, status in _dpkg_status_map(timeout=timeout).items()
        if status[:2] == "rc"
    }


def _multiarch_conflicts(package_ids, status_map):
    """S2: refuse a residual whose base name has a non-rc instance elsewhere.

    ``apt-get purge libfoo`` targets the native architecture, so a bare-name
    request next to an installed other-arch instance can purge the installed
    package. Even arch-qualified requests are refused here, because the
    simulation's confirmation gate compares base names and could be satisfied
    by the wrong instance.
    """
    conflicts = {}
    for package_id in package_ids:
        base = _base_package_name(package_id)
        for other, status in status_map.items():
            if other == package_id or _base_package_name(other) != base:
                continue
            if status[:2] != "rc":
                conflicts.setdefault(package_id, []).append(other)
    return conflicts


def _dpkg_conffile_entries(package_id, timeout=30):
    """Return validated (path, md5) conffile entries for one package."""
    if not _is_safe_package_id(package_id):
        raise OSError(f"Refusing unsafe package name: {package_id}")
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Conffiles}\n", "--", package_id],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode != 0:
        raise OSError(
            result.stderr.strip()
            or f"Could not list configuration files for {package_id}."
        )
    entries = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        match = CONFFILE_LINE_RE.match(line)
        if not match:
            if line.lstrip().startswith("/"):
                raise OSError(
                    "Could not safely parse dpkg conffile entry for "
                    f"{package_id}: {line}"
                )
            continue
        entries.append((match.group(1), match.group(2)))
    return entries


def _validate_conffile_path(source_value):
    source_path = Path(source_value)
    if (
        not source_path.is_absolute()
        or ".." in source_path.parts
        or os.path.normpath(source_value) != source_value
        or source_path == Path("/")
    ):
        raise OSError(f"Refusing unsafe dpkg conffile path: {source_value}")
    return source_path


def get_residual_conffile_details(package_ids, timeout=30):
    """Map each residual package to its dpkg-registered conffile paths.

    Used for pre-purge previews so the GUI can show what would be backed up
    and purged instead of only package names.
    """
    details = {}
    for package_id in _deduplicate_package_ids(package_ids):
        try:
            details[package_id] = [
                path for path, _md5 in _dpkg_conffile_entries(package_id, timeout=timeout)
            ]
        except OSError:
            details[package_id] = []
    return details


def simulate_leftover_purge(package_ids, timeout=120):
    """Simulate `apt-get purge` for residual configs without changing state."""
    package_ids = _deduplicate_package_ids(package_ids)
    if not package_ids:
        return True, "", []
    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"
    cmd = [apt_get_command, "--simulate", "purge", "--", *package_ids]
    _log_action(f"LEFTOVER_PURGE_SIMULATION cmd={shlex.join(cmd)}")
    returncode, output = _run_command_raw(
        cmd,
        timeout=timeout,
        env_overrides={"LC_ALL": "C"},
        uses_apt=True,
    )
    if returncode != 0:
        _log_action("LEFTOVER_PURGE_SIMULATION_FAILED")
        return False, output, []
    purged = _parse_apt_simulation_removed_packages(output)
    _log_action(f"LEFTOVER_PURGE_SIMULATION_SUCCESS purged_count={len(purged)}")
    return True, output, purged


def _same_package_instance(requested, simulated):
    """Compare a requested `name[:arch]` against a simulation token (S2)."""
    if requested == simulated:
        return True
    requested_base = _base_package_name(requested)
    simulated_base = _base_package_name(simulated)
    if requested_base != simulated_base:
        return False
    # A simulation token without an architecture is only an acceptable match
    # for a request without one, and only on a single-architecture system;
    # when either side carries an arch, demand agreement.
    requested_arch = _package_arch(requested)
    simulated_arch = _package_arch(simulated)
    return not requested_arch or not simulated_arch


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


def _write_json_private_update(path, data):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    _write_json_private(temporary, data)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _ensure_backup_parents(directory: Path, files_dir: Path):
    """S9: create backup directories without ever following a symlink.

    ``mkdir(parents=True)`` happily descends through a symlink planted in an
    existing parent, letting a later ``O_CREAT|O_NOFOLLOW`` open escape the
    backup tree through that intermediate link. Creating one level at a time
    and refusing symlinks keeps every parent a real directory inside
    ``files_dir``.
    """
    relative = directory.relative_to(files_dir)
    current = files_dir
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise OSError(
                "Refusing to back up through a symlinked directory inside "
                f"the backup area: {current}"
            )
        if not current.exists():
            current.mkdir(mode=0o700)
        elif not current.is_dir():
            raise OSError(
                f"Backup destination parent is not a directory: {current}"
            )


def _discard_backup(backup_dir: Path):
    """Delete an unused backup directory (purge never started)."""
    try:
        root = backup_root_dir()
        if backup_dir.parent != root or backup_dir.is_symlink():
            return
        shutil.rmtree(backup_dir)
    except OSError:
        pass


def backup_root_dir() -> Path:
    return state_dir() / "purge-backups"


def _backup_residual_conffiles(package_ids, skip_unreadable=False):
    """Securely copy dpkg-authoritative conffiles before irreversible purge.

    ``skip_unreadable`` (S9) records root-only conffiles as "unreadable" in
    the manifest instead of aborting the whole purge; the choice is surfaced
    to the user by the caller, and recorded in the activity log.
    """
    package_ids = _deduplicate_package_ids(package_ids)
    _valid, invalid = _split_safe_package_ids(package_ids)
    if invalid:
        raise OSError(
            "Blocked unsafe or invalid package names: " + ", ".join(invalid)
        )
    if len(package_ids) > RESIDUAL_MAX_PACKAGES:
        raise OSError(
            f"Refusing to back up {len(package_ids)} packages at once "
            f"(limit is {RESIDUAL_MAX_PACKAGES}). Purge in smaller batches."
        )
    state_root = state_dir()
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not is_acceptable_private_dir(state_root):
        raise OSError(
            f"Backup state directory is not private: {state_root}"
        )
    backup_root = state_root / "purge-backups"
    backup_root.mkdir(mode=0o700, exist_ok=True)
    if backup_root.is_symlink() or not is_acceptable_private_dir(backup_root):
        raise OSError(f"Backup directory is not private: {backup_root}")
    os.chmod(state_root, 0o700)
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
        "limits": {
            "max_files": RESIDUAL_MAX_BACKUP_FILES,
            "max_bytes": RESIDUAL_MAX_BACKUP_BYTES,
        },
        "note": (
            "Backup covers dpkg-registered conffiles only. Maintainer purge "
            "scripts may remove additional files; review the APT simulation "
            "preview before purging."
        ),
    }
    manifest_path = backup_dir / "manifest.json"
    _write_json_private(manifest_path, manifest)
    records_by_path = {}
    total_backed_up_bytes = 0
    backed_up_files = 0

    try:
        for package_id in package_ids:
            entries = _dpkg_conffile_entries(package_id)

            for source_value, _md5 in entries:
                source_path = _validate_conffile_path(source_value)
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
                except PermissionError as permission_error:
                    if not skip_unreadable:
                        raise OSError(
                            "A configuration file could not be read with user "
                            "permissions (it is probably root-only):\n\n"
                            f"  {source_value}\n\n"
                            "Purging is blocked so that no file is deleted "
                            "without a backup copy. You can retry with "
                            "\"skip unreadable files\" if you accept that "
                            "this specific file will not be backed up."
                        ) from permission_error
                    _log_action(
                        "PURGE_BACKUP_SKIPPED_UNREADABLE "
                        f"package={package_id} path={source_value}"
                    )
                    manifest["files"].append({
                        "package": package_id,
                        "packages": [package_id],
                        "path": source_value,
                        "status": "unreadable",
                    })
                    records_by_path[source_value] = manifest["files"][-1]
                    continue

                destination = files_dir / source_value.lstrip("/")
                _ensure_backup_parents(destination.parent, files_dir)
                if backed_up_files + 1 > RESIDUAL_MAX_BACKUP_FILES:
                    raise OSError(
                        f"Refusing to back up more than {RESIDUAL_MAX_BACKUP_FILES} "
                        "configuration files at once. Purge in smaller batches."
                    )
                if (
                    stat.S_ISREG(source_stat.st_mode)
                    and total_backed_up_bytes + source_stat.st_size
                    > RESIDUAL_MAX_BACKUP_BYTES
                ):
                    raise OSError(
                        "Refusing to back up more than "
                        f"{RESIDUAL_MAX_BACKUP_BYTES // (1024 * 1024)} MiB of "
                        "configuration files at once. Purge in smaller batches."
                    )
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
                if record.get("type") in {"file", "symlink"}:
                    backed_up_files += 1
                    if record.get("type") == "file":
                        total_backed_up_bytes += int(record.get("size", 0) or 0)
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


def _purge_backup_note(backup_dir, file_count):
    noun = "file" if file_count == 1 else "files"
    return (
        f"Pre-purge configuration backup retained at {backup_dir} "
        f"({file_count} existing {noun})."
    )


def read_purge_backup(backup_dir):
    """Read and validate one pre-purge backup manifest.

    Returns a summary dict, or None when the manifest is missing, unreadable,
    or fails validation. This never restores anything; it is a read-only
    inspection helper for the GUI.
    """
    manifest_path = Path(backup_dir) / "manifest.json"
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(manifest, dict):
        return None

    files = manifest.get("files")
    if not isinstance(files, list):
        files = []

    entries = []
    for record in files:
        if not isinstance(record, dict):
            continue
        path = record.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        entries.append({
            "path": path,
            "package": record.get("package", ""),
            "packages": record.get("packages", []),
            "type": record.get("type", ""),
            "status": record.get("status", ""),
            "size": record.get("size"),
            "mode": record.get("mode"),
            "uid": record.get("uid"),
            "gid": record.get("gid"),
            "sha256": record.get("sha256"),
            "backup": record.get("backup"),
        })

    packages = manifest.get("packages")
    return {
        "created_at": manifest.get("created_at", ""),
        "status": manifest.get("status", "unknown"),
        "error": manifest.get("error", ""),
        "note": manifest.get("note", ""),
        "packages": packages if isinstance(packages, list) else [],
        "files": entries,
        "missing": sum(1 for e in entries if e["status"] == "missing"),
        "backed_up": sum(1 for e in entries if e["type"] in {"file", "symlink"}),
    }


def get_purge_backup_summaries():
    """Summarise all retained pre-purge backups, newest first."""
    summaries = []
    for path in list_purge_backups():
        data = read_purge_backup(path)
        if data is None:
            summaries.append({
                "path": path,
                "name": path.name,
                "created_at": "",
                "status": "unreadable",
                "packages": [],
                "backed_up": 0,
                "missing": 0,
                "files": [],
                "note": "",
                "error": "Manifest is missing or could not be parsed.",
            })
            continue
        data["name"] = path.name
        data["path"] = path
        summaries.append(data)
    return summaries


def list_purge_backups():
    """List existing pre-purge backup directories, newest first."""
    backup_root = backup_root_dir()
    try:
        if not backup_root.is_dir() or backup_root.is_symlink():
            return []
        entries = [path for path in backup_root.iterdir() if path.is_dir()]
    except OSError:
        return []
    entries.sort(key=lambda path: path.name, reverse=True)
    return entries


def prune_old_purge_backups(keep=RESIDUAL_PURGE_BACKUP_KEEP):
    """Keep only the newest `keep` *usable* purge backups (S9).

    Incomplete or error-marked backups are deleted first — they are the
    debris of blocked attempts — and only then are the newest `keep`
    complete backups retained. Previously the newest 10 by *name* were kept
    regardless of status, so ten blocked attempts could evict every good
    backup. Returns (removed, kept).
    """
    backups = list_purge_backups()
    if keep < 0:
        keep = 0

    root = backup_root_dir()
    usable = []
    removed = 0
    for path in backups:
        summary = read_purge_backup(path)
        is_usable = (
            summary is not None
            and summary.get("status") == "complete"
            and not summary.get("error")
        )
        if not is_usable:
            try:
                if path.is_symlink() or path.parent != root:
                    continue
                shutil.rmtree(path)
                removed += 1
            except OSError:
                usable.append(path)
            continue
        usable.append(path)

    to_remove = usable[keep:]
    for path in to_remove:
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            # Refuse to delete anything outside the backup root.
            if path.parent != root:
                continue
            shutil.rmtree(path)
            removed += 1
        except OSError:
            continue
    return removed, len(usable) - len(to_remove)


def purge_leftover_configs(
    package_ids,
    output_callback=None,
    skip_unreadable_backup=False,
):
    """
    Purges leftover APT configuration packages.

    These packages are already removed, but their configuration files remain.

    Safety model:
    - Validate names (``name:arch``), verify dpkg rc state, and simulate the
      purge first.
    - Refuse any package whose base name has a non-rc instance on another
      architecture (S2): the bare-name purge could hit the installed one.
    - Back up dpkg-registered conffiles; abort if the backup is incomplete.
    - Re-verify rc state after the backup (TOCTOU): a package reinstalled
      between verification and purge must not be purged as if it were only
      residual config. Backups made for a purge that never starts are
      discarded again (S9).
    - Purge with `pkexec apt-get purge -y ...`, then verify residuals are
      gone.

    Uses:

        pkexec apt-get purge -y name:arch ...
    """

    requested = _deduplicate_package_ids(package_ids)
    if not requested:
        return True, "No leftover packages selected."

    valid, invalid = _split_safe_package_ids(requested)
    if invalid:
        return False, (
            "Blocked unsafe or invalid package names:\n\n"
            + "\n".join(invalid)
        )

    if len(valid) > RESIDUAL_MAX_PACKAGES:
        return False, (
            "Blocked by safety policy.\n\n"
            f"{len(valid)} residual packages were selected; the limit is "
            f"{RESIDUAL_MAX_PACKAGES}. Purge in smaller batches."
        )

    try:
        status_map = _dpkg_status_map()
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"Could not verify residual APT configurations: {error}"

    residual_packages = {
        package
        for package, status in status_map.items()
        if status[:2] == "rc"
    }
    # Resolve the request against arch-qualified rc identities. A bare name
    # (legacy marks, older callers) expands to its residual instances so the
    # purge can never be resolved by apt to an *installed* architecture.
    resolved = []
    skipped_packages = []
    for package_id in valid:
        if package_id in residual_packages:
            resolved.append(package_id)
            continue
        if ":" not in package_id:
            arch_instances = sorted(
                candidate
                for candidate in residual_packages
                if _base_package_name(candidate) == package_id
            )
            if arch_instances:
                resolved.extend(arch_instances)
                continue
        skipped_packages.append(package_id)

    packages_to_purge = _deduplicate_package_ids(resolved)
    if not packages_to_purge:
        return False, (
            "None of the selected packages are currently in dpkg's "
            "residual-configuration state. No packages were purged."
        )

    # S2: block when another architecture instance of the same base name is
    # in a non-rc state — apt resolves bare names to the native arch.
    conflicts = _multiarch_conflicts(packages_to_purge, status_map)
    if conflicts:
        blocked_lines = []
        for package_id, others in conflicts.items():
            for other in others:
                blocked_lines.append(
                    f"• {package_id} is residual, but {other} is still "
                    "installed (different architecture). Purging by name "
                    "could remove the installed instance, so Appector "
                    "refuses; purge from a terminal with the explicit "
                    f"':{other.split(':')[-1]}' suffix if you really mean it."
                )
        _log_action(
            "LEFTOVER_PURGE_BLOCKED reason=multiarch "
            f"packages={','.join(sorted(conflicts))}"
        )
        return False, (
            "Blocked by safety policy (multi-arch conflict):\n\n"
            + "\n".join(blocked_lines)
        )

    # Simulate first: catches broken APT state and shows what purge would do.
    # Residual purges should only affect the selected configs; if the
    # simulation reports additional removals, stop and show it.
    sim_success, sim_output, sim_purged = simulate_leftover_purge(packages_to_purge)
    if not sim_success:
        _log_action("LEFTOVER_PURGE_BLOCKED reason=simulation-failed")
        return False, (
            "APT purge simulation failed; no packages were purged.\n\n"
            f"{sim_output}"
        )
    not_confirmed = [
        p for p in packages_to_purge
        if not any(_same_package_instance(p, s) for s in sim_purged)
    ]
    if not_confirmed:
        return False, (
            "APT simulation did not confirm purging:\n\n"
            f"{_format_package_list(not_confirmed)}\n\n{sim_output}"
        )
    extra_purged = sorted(
        s for s in sim_purged
        if not any(_same_package_instance(p, s) for p in packages_to_purge)
    )
    if extra_purged:
        return False, (
            "Blocked: APT simulation says purging would affect packages beyond "
            "the selected residuals:\n\n"
            f"{_format_package_list(extra_purged)}\n\n{sim_output}"
        )
    critical_residuals = [
        p for p in packages_to_purge if is_critical_apt_package_name(p)
    ]

    # The privileged tail — conffile backup as root (S9), rc/multiarch
    # re-verification, plan binding, and the purge itself — runs inside the
    # helper (ADR-0003). The plan binds the purge to the simulated set.
    plan = Plan(
        action="purge",
        package_ids=list(packages_to_purge),
        removed_preview=list(sim_purged),
        text="",
    )

    _log_action(
        f"LEFTOVER_PURGE_START helper=purge-residual packages={len(packages_to_purge)}"
    )

    try:
        result = helper_client.purge_residual(
            packages_to_purge,
            plan=plan,
            skip_unreadable_backup=skip_unreadable_backup,
            output_callback=output_callback,
        )
    except HelperUnavailable as error:
        _log_action("LEFTOVER_PURGE_BLOCKED reason=helper-unavailable")
        return False, f"{error}"
    except OSError as error:
        _log_action("LEFTOVER_PURGE_BLOCKED reason=helper-start-failed")
        return False, f"Could not start the privileged helper: {error}"

    success = result.ok
    message = result.message

    notes = []
    for event in result.events:
        if event.get("type") == "backup":
            notes.append(
                _purge_backup_note(
                    event.get("path"), int(event.get("files", 0) or 0)
                )
            )
    notes.append(
        "This backup covers dpkg-registered conffiles only; maintainer purge "
        "scripts may have removed additional files. There is no automatic "
        "restore: inspect the manifest before copying anything back."
    )
    if critical_residuals:
        notes.append(
            "Note: critical/system packages were among the purged residuals:\n"
            + _format_package_list(critical_residuals)
            + "\nReinstalling them will recreate default configuration; your "
            "previous customizations remain only in the backup above."
        )
    # Prune old backups so repeated purges do not fill the home directory.
    try:
        removed, _kept = prune_old_purge_backups()
        if removed:
            notes.append(
                f"Pruned {removed} older purge backup(s); newest "
                f"{RESIDUAL_PURGE_BACKUP_KEEP} retained."
            )
    except Exception:
        pass

    if success:
        _log_action("LEFTOVER_PURGE_SUCCESS")
        # Post-verify: residuals should be gone now.
        try:
            remaining = _dpkg_residual_package_set()
            lingering = [p for p in packages_to_purge if p in remaining]
        except (OSError, subprocess.TimeoutExpired):
            lingering = []
        if lingering:
            message += (
                "\n\nWarning: these packages are still in residual state:\n"
                + "\n".join(lingering)
            )
        if notes:
            message += "\n\n" + "\n\n".join(notes)
        if skipped_packages:
            message += (
                "\n\nSkipped packages no longer in residual-configuration state:\n"
                + "\n".join(skipped_packages)
            )
    else:
        _log_action(f"LEFTOVER_PURGE_FAILED error={message[:200]}")
        if notes:
            message += "\n\n" + "\n\n".join(notes)

    return success, message
