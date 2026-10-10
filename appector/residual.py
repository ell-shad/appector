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

from ._proc import _log_action, _run_command_raw, _run_command_stream
from .policy import (
    _base_package_name,
    _is_safe_package_id,
    is_critical_apt_package_name,
)


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
    """Extract package names from `Remv`/`Purg` lines of an apt simulation."""
    removed = []
    for line in output.splitlines():
        if line.startswith(("Remv ", "Purg ")):
            parts = line.split()
            if len(parts) >= 2:
                removed.append(_base_package_name(parts[1]))
    seen = set()
    unique = []
    for package_name in removed:
        if package_name not in seen:
            seen.add(package_name)
            unique.append(package_name)
    return unique


def _format_package_list(packages, limit=50):
    lines = [f"• {name}" for name in packages[:limit]]
    if len(packages) > limit:
        lines.append(f"• …and {len(packages) - limit} more")
    return "\n".join(lines)


# Residual-config purge limits. Purging only deletes configuration files of
# already-removed packages, but a huge batch can still fill the home
# filesystem with backup copies, so cap both the request and the backup.
RESIDUAL_MAX_PACKAGES = 100
RESIDUAL_MAX_BACKUP_FILES = 2000
RESIDUAL_MAX_BACKUP_BYTES = 100 * 1024 * 1024
RESIDUAL_PURGE_BACKUP_KEEP = 10

SAFE_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]*$")

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


def _dpkg_residual_package_set(timeout=30):
    """Return the set of packages currently in dpkg's rc state."""
    result = subprocess.run(
        [
            "dpkg-query",
            "-W",
            "-f=${db:Status-Abbrev}\t${Package}\n",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode != 0:
        raise OSError(
            result.stderr.strip()
            or "Could not verify residual APT configurations."
        )
    residual = set()
    for line in result.stdout.splitlines():
        status, separator, package = line.partition("\t")
        if separator and status[:2] == "rc":
            package = package.strip()
            if package:
                residual.add(package)
    return residual


def _dpkg_conffile_entries(package_id, timeout=30):
    """Return validated (path, md5) conffile entries for one package."""
    if not _is_safe_package_id(package_id):
        raise OSError(f"Refusing unsafe package name: {package_id}")
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Conffiles}\n", "--", package_id],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
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
    )
    if returncode != 0:
        _log_action("LEFTOVER_PURGE_SIMULATION_FAILED")
        return False, output, []
    purged = _parse_apt_simulation_removed_packages(output)
    _log_action(f"LEFTOVER_PURGE_SIMULATION_SUCCESS purged_count={len(purged)}")
    return True, output, purged


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


def _backup_residual_conffiles(package_ids):
    """Securely copy dpkg-authoritative conffiles before irreversible purge."""
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
    state_dir = Path.home() / ".local" / "state" / "app-manager"
    backup_root = state_dir / "purge-backups"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    home_dir = Path.home().resolve()
    if (
        state_dir.is_symlink()
        or not state_dir.resolve().is_relative_to(home_dir)
    ):
        raise OSError(f"Backup state directory is outside the home directory: {state_dir}")
    backup_root.mkdir(mode=0o700, exist_ok=True)
    if backup_root.is_symlink() or not backup_root.resolve().is_relative_to(home_dir):
        raise OSError(f"Backup directory is outside the home directory: {backup_root}")
    os.chmod(state_dir, 0o700)
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

                destination = files_dir / source_value.lstrip("/")
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def _write_json_private_update(path, data):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    _write_json_private(temporary, data)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


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
    backup_root = Path.home() / ".local" / "state" / "app-manager" / "purge-backups"
    try:
        if not backup_root.is_dir() or backup_root.is_symlink():
            return []
        entries = [path for path in backup_root.iterdir() if path.is_dir()]
    except OSError:
        return []
    entries.sort(key=lambda path: path.name, reverse=True)
    return entries


def prune_old_purge_backups(keep=RESIDUAL_PURGE_BACKUP_KEEP):
    """Keep only the newest `keep` purge backups. Returns (removed, kept)."""
    backups = list_purge_backups()
    if keep < 0:
        keep = 0
    to_remove = backups[keep:]
    removed = 0
    for path in to_remove:
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            # Refuse to delete anything outside the backup root.
            if path.parent != (
                Path.home() / ".local" / "state" / "app-manager" / "purge-backups"
            ):
                continue
            shutil.rmtree(path)
            removed += 1
        except OSError:
            continue
    return removed, len(backups) - removed


def purge_leftover_configs(package_ids, output_callback=None):
    """
    Purges leftover APT configuration packages.

    These packages are already removed, but their configuration files remain.

    Safety model:
    - Validate names, verify dpkg rc state, and simulate the purge first.
    - Back up dpkg-registered conffiles; abort if the backup is incomplete.
    - Re-verify rc state after the backup (TOCTOU): a package reinstalled
      between verification and purge must not be purged as if it were only
      residual config.
    - Purge with `pkexec apt-get purge -y ...`, then verify residuals are gone.

    Uses:

        pkexec apt-get purge -y package1 package2 ...
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
        residual_packages = _dpkg_residual_package_set()
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"Could not verify residual APT configurations: {error}"

    packages_to_purge = [p for p in valid if p in residual_packages]
    skipped_packages = [p for p in valid if p not in residual_packages]
    if not packages_to_purge:
        return False, (
            "None of the selected packages are currently in dpkg's "
            "residual-configuration state. No packages were purged."
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
    sim_purged_bases = {_base_package_name(p) for p in sim_purged}
    not_confirmed = [
        p for p in packages_to_purge
        if _base_package_name(p) not in sim_purged_bases
    ]
    if not_confirmed:
        return False, (
            "APT simulation did not confirm purging:\n\n"
            f"{_format_package_list(not_confirmed)}\n\n{sim_output}"
        )
    extra_purged = sorted(
        p for p in sim_purged_bases
        if p not in {_base_package_name(p) for p in packages_to_purge}
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

    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"
    pkexec_command = shutil.which("pkexec")

    try:
        backup_dir, backed_up_count = _backup_residual_conffiles(packages_to_purge)
    except OSError as error:
        _log_action(f"LEFTOVER_PURGE_BLOCKED backup_error={error}")
        return False, (
            "Purge was not started because Appector could not securely back up "
            f"the remaining configuration files.\n\n{error}"
        )

    # Re-verify after the (potentially slow) backup: if a package left rc
    # state meanwhile (e.g. reinstalled), purging it now would remove an
    # installed package, not just residual config.
    try:
        residual_after_backup = _dpkg_residual_package_set()
    except (OSError, subprocess.TimeoutExpired) as error:
        _log_action(f"LEFTOVER_PURGE_BLOCKED reason=reverify-failed backup={backup_dir}")
        return False, (
            "Purge was not started because residual state could not be "
            f"re-verified after backup.\n\n{error}\n\n"
            f"{_purge_backup_note(backup_dir, backed_up_count)}"
        )
    changed = [p for p in packages_to_purge if p not in residual_after_backup]
    if changed:
        _log_action(f"LEFTOVER_PURGE_BLOCKED reason=state-changed backup={backup_dir}")
        return False, (
            "Purge was not started because these packages left "
            "residual-configuration state after the backup (they may have been "
            "reinstalled):\n\n"
            + "\n".join(changed)
            + f"\n\n{_purge_backup_note(backup_dir, backed_up_count)}"
        )

    cmd = [
        apt_get_command,
        "purge",
        "-y",
        "--",
    ] + packages_to_purge

    if pkexec_command:
        cmd = [pkexec_command] + cmd

    _log_action(f"LEFTOVER_PURGE_START cmd={shlex.join(cmd)}")

    success, message = _run_command_stream(cmd, output_callback)

    notes = [f"{_purge_backup_note(backup_dir, backed_up_count)}"]
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
        message += "\n\n" + "\n\n".join(notes)
        if skipped_packages:
            message += (
                "\n\nSkipped packages no longer in residual-configuration state:\n"
                + "\n".join(skipped_packages)
            )
    else:
        _log_action(f"LEFTOVER_PURGE_FAILED backup={backup_dir} error={message}")
        message += "\n\n" + "\n\n".join(notes)

    return success, message
    
