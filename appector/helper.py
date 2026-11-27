"""The privileged helper (root side). See ADR-0003.

Installed as `/usr/lib/appector/appector-helper` (root-owned, 0755) and
invoked through pkexec. Reads one JSON request from stdin, re-validates
everything as root, performs exactly one named action, and writes JSON-lines
events to stdout. Never uses a shell; never installs a user-writable path.

This module is importable so the validation and handlers can be unit-tested
without root (tests inject subprocess fakes); the `main()` entry point is
what the installed program runs.
"""

import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from ._proc import APT_NONINTERACTIVE_OPTIONS, _run_command_raw
from .plans import Plan, check_apt_removal_plan, check_apt_upgrade_plan
from .policy import (
    APT_MAX_REMOVALS,
    _is_safe_package_id,
    _parse_apt_simulation_removed_packages,
)

PROTOCOL_VERSION = 1

MAX_PACKAGES = {
    "remove-apt": APT_MAX_REMOVALS,
    "purge-residual": 100,
    "remove-snap": 100,
    "remove-flatpak-system": 100,
}

ALLOWED_ACTIONS = {
    "install-deb",
    "remove-apt",
    "purge-residual",
    "remove-snap",
    "remove-flatpak-system",
    "apt-autoremove",
    "apt-upgrade",
    "flatpak-update-system",
}

NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SNAP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FLATPAK_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9._-]+)*$")
DEB_PATH_RE = re.compile(r"^/[^\n]+\.[Dd][Ee][Bb]$")

MAX_STRING_LENGTH = 4096
MAX_OUTPUT_EVENTS = 100000

HELPER_STAGING_ROOT = Path("/var/lib/appector/staging")


class RequestError(Exception):
    """The request is malformed or violates policy; nothing was executed."""


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def _require(condition, message):
    if not condition:
        raise RequestError(message)


def _string(value, field, max_length=MAX_STRING_LENGTH):
    _require(
        isinstance(value, str) and value and len(value) <= max_length,
        f"Field {field!r} must be a non-empty string of at most "
        f"{max_length} characters.",
    )
    _require("\n" not in value and "\r" not in value, f"Field {field!r} must not contain newlines.")
    return value


def _string_list(value, field, cap):
    _require(isinstance(value, list), f"Field {field!r} must be a list.")
    _require(len(value) <= cap, f"Field {field!r} lists more than {cap} entries.")
    result = []
    for item in value:
        result.append(_string(item, field, 512))
    _require(
        all(item and "\x00" not in item for item in result),
        f"Field {field!r} contains invalid entries.",
    )
    return result


def validate_request(request):
    """Validate an untrusted request and return a normalized copy.

    Raises RequestError on any violation. The schema is strict: unknown
    top-level keys are rejected so new fields cannot smuggle options past
    older validations.
    """
    _require(isinstance(request, dict), "Request must be a JSON object.")

    allowed_keys = {"version", "action", "nonce", "packages", "purge",
                    "plan", "plan_hash", "files", "installation", "options"}
    unknown = set(request) - allowed_keys
    _require(not unknown, f"Unknown request fields: {', '.join(sorted(unknown))}.")

    version = request.get("version")
    _require(version == PROTOCOL_VERSION, f"Unsupported protocol version: {version!r}")

    action = request.get("action")
    _require(action in ALLOWED_ACTIONS, f"Unknown action: {action!r}")

    nonce = request.get("nonce")
    _require(
        isinstance(nonce, str) and NONCE_RE.match(nonce),
        "A 32-hex-character nonce is required.",
    )

    normalized = {
        "version": version,
        "action": action,
        "nonce": nonce,
    }

    if action in {"remove-apt", "purge-residual", "remove-snap",
                  "remove-flatpak-system", "apt-upgrade", "flatpak-update-system"}:
        packages = _string_list(request.get("packages"), "packages",
                                MAX_PACKAGES.get(action, 100))
        _require(packages, "At least one package identity is required.")
        normalized["packages"] = packages

        if action in {"remove-apt", "purge-residual", "apt-upgrade"}:
            for package_id in packages:
                _require(
                    _is_safe_package_id(package_id),
                    f"Unsafe APT package identity: {package_id!r}",
                )
        elif action == "remove-snap":
            for package_id in packages:
                _require(
                    SNAP_NAME_RE.match(package_id),
                    f"Unsafe snap name: {package_id!r}",
                )
        elif action == "remove-flatpak-system":
            for package_id in packages:
                _require(
                    FLATPAK_REF_RE.match(package_id),
                    f"Unsafe Flatpak ref: {package_id!r}",
                )

    if action in {"remove-apt", "purge-residual", "apt-autoremove", "apt-upgrade"}:
        # The plan binds execution to the previewed transaction (S4).
        plan_payload = request.get("plan")
        _require(isinstance(plan_payload, dict), "A confirmed plan is required.")
        plan_keys = set(plan_payload)
        _require(
            plan_keys <= {"action", "package_ids", "removed_preview", "text", "plan_hash"},
            f"Unknown plan fields: {', '.join(sorted(plan_keys))}.",
        )
        plan = Plan(
            action=_string(plan_payload.get("action", ""), "plan.action", 64),
            package_ids=_string_list(
                plan_payload.get("package_ids", []), "plan.package_ids", 1000
            ),
            removed_preview=_string_list(
                plan_payload.get("removed_preview", []), "plan.removed_preview", 5000
            ),
            text="",
        )
        normalized["plan"] = plan
        normalized["plan_hash"] = request.get("plan_hash")
        _require(
            isinstance(normalized["plan_hash"], str)
            and SHA256_RE.match(normalized["plan_hash"]),
            "plan_hash must be the SHA-256 of the confirmed plan.",
        )
        _require(
            plan.plan_hash == normalized["plan_hash"],
            "Plan hash mismatch: the plan does not match its hash.",
        )

    if action == "remove-apt":
        normalized["purge"] = bool(request.get("purge", False))
        _require(
            normalized["plan"].action in {"remove", "purge", "batch-remove"},
            "Plan action does not match a removal.",
        )

    if action == "apt-upgrade":
        _require(
            normalized["plan"].action == "apt-upgrade",
            "Plan action does not match an upgrade.",
        )
        _require(
            not normalized["plan"].removed_preview,
            "An upgrade plan must not preview any removals.",
        )

    if action == "apt-autoremove":
        _require(
            normalized["plan"].action == "apt-autoremove",
            "Plan action does not match autoremove.",
        )

    if action == "remove-flatpak-system":
        installation = request.get("installation", "system")
        _require(
            isinstance(installation, str)
            and re.match(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$", installation)
            and len(installation) <= 64,
            f"Unsafe installation name: {installation!r}",
        )
        normalized["installation"] = installation

    if action == "install-deb":
        files = request.get("files")
        _require(isinstance(files, list) and files, "At least one file is required.")
        _require(len(files) <= 50, "At most 50 packages per install request.")
        normalized_files = []
        for item in files:
            _require(isinstance(item, dict), "Each file entry must be an object.")
            _require(
                set(item) == {"path", "sha256"},
                "Each file entry must have exactly 'path' and 'sha256'.",
            )
            path = _string(item["path"], "files.path")
            _require(
                DEB_PATH_RE.match(path) and os.path.isabs(path),
                f"Not an absolute .deb path: {path!r}",
            )
            sha256 = _string(item["sha256"], "files.sha256")
            _require(SHA256_RE.match(sha256), "sha256 must be 64 hex characters.")
            normalized_files.append({"path": path, "sha256": sha256})
        normalized["files"] = normalized_files

    if action == "purge-residual":
        normalized["skip_unreadable_backup"] = bool(
            (request.get("options") or {}).get("skip_unreadable_backup", False)
        )
        _require(
            normalized["plan"].action in {"purge", "batch-remove"},
            "Plan action does not match a residual purge.",
        )

    return normalized


# ---------------------------------------------------------------------------
# Root-side execution helpers
# ---------------------------------------------------------------------------


def _target_user():
    """The invoking user, for backups written into their home."""
    for variable in ("PKEXEC_UID", "SUDO_UID"):
        value = os.environ.get(variable)
        if value and value.isdigit():
            try:
                return pwd.getpwuid(int(value))
            except KeyError:
                continue
    return None


def _apt_command(apt_get, arguments, packages):
    return [
        apt_get,
        *arguments,
        *APT_NONINTERACTIVE_OPTIONS,
        "-y",
        "--",
        *packages,
    ]


def _simulate_apt(apt_get, arguments, packages):
    command = [apt_get, "--simulate", *arguments, "--", *packages]
    returncode, output = _run_command_raw(
        command,
        timeout=300,
        env_overrides={"LC_ALL": "C"},
        uses_apt=True,
    )
    removed = _parse_apt_simulation_removed_packages(output) if returncode == 0 else []
    return returncode, output, removed


class EventStream:
    """Writes JSON-lines events as they happen, with a hard cap."""

    def __init__(self, stream, nonce, cap=MAX_OUTPUT_EVENTS):
        self._stream = stream
        self._nonce = nonce
        self._cap = cap
        self.emitted = 0

    def emit(self, event_type, **fields):
        if self.emitted >= self._cap:
            return
        self.emitted += 1
        payload = {"type": event_type, "nonce": self._nonce}
        payload.update(fields)
        try:
            self._stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._stream.flush()
        except (OSError, ValueError):
            pass


def _run_streaming(stream, cmd, uses_apt):
    """Run one command as root, streaming output as JSON events."""
    from ._proc import run as run_command

    result = run_command(
        cmd,
        timeout=1800,
        env_overrides={"LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive",
                       "NEEDRESTART_MODE": "a", "APT_LISTCHANGES_FRONTEND": "none"},
        stream_cb=lambda line: stream.emit(
            "output", line=line[:MAX_STRING_LENGTH]
        ),
        uses_apt=uses_apt,
    )
    return result


def _critical_apt_gate(packages, plan, actual_removed, residual=False):
    ok, message = check_apt_removal_plan(
        plan, packages, actual_removed, residual=residual
    )
    if ok:
        # check_apt_removal_plan already covers critical + cap + growth.
        return True, ""
    return False, message


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


def _stage_deb_sources(files, staging_dir):
    """S1: copy each source into root-owned staging while hashing.

    Root opens every source exactly once, with O_NOFOLLOW and a
    regular-file check, and hashes *the bytes it copies*. The privileged
    install then runs only on the root-owned copies, so a file swapped in
    the user-writable source directory after review cannot be installed.
    """
    staged = []
    for entry in files:
        source_path = Path(entry["path"])
        expected = entry["sha256"]
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            source_fd = os.open(source_path, flags)
        except OSError as error:
            raise RequestError(
                f"Could not open reviewed package {source_path}: {error}"
            ) from error
        destination_fd = None
        destination = staging_dir / f"{len(staged):04d}-{source_path.name}"
        try:
            opened_stat = os.fstat(source_fd)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise RequestError(
                    f"Reviewed package is not a regular file: {source_path}"
                )
            destination_fd = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
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
            if digest.hexdigest() != expected:
                # S1 acceptance: a swapped staged file results in refusal.
                raise RequestError(
                    f"Reviewed package changed before installation: "
                    f"{source_path.name}. The reviewed SHA-256 no longer "
                    "matches; review the package again."
                )
        except RequestError:
            _unlink_quietly(destination)
            raise
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            if destination_fd is not None:
                os.close(destination_fd)
        staged.append(destination)
    return staged


def _unlink_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def handle_install_deb(request, stream):
    apt_get = shutil.which("apt-get")
    if not apt_get:
        return {"ok": False, "message": "apt-get is not available."}

    try:
        HELPER_STAGING_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(HELPER_STAGING_ROOT, 0o700)
    except OSError as error:
        return {"ok": False, "message": f"Could not create staging area: {error}"}
    with tempfile.TemporaryDirectory(prefix="install-", dir=str(HELPER_STAGING_ROOT)) as tmp:
        staging_dir = Path(tmp)
        os.chmod(staging_dir, 0o700)
        try:
            staged = _stage_deb_sources(request["files"], staging_dir)
        except RequestError as error:
            return {"ok": False, "message": str(error)}

        command = _apt_command(
            apt_get,
            ["install"],
            [str(path) for path in staged],
        )
        result = _run_streaming(stream, command, uses_apt=True)
        if not result.success:
            return {
                "ok": False,
                "message": result.output or "apt-get install failed.",
                "returncode": result.returncode,
            }
        return {"ok": True, "message": "Installed reviewed packages.", "staged": len(staged)}


def handle_remove_apt(request, stream):
    apt_get = shutil.which("apt-get")
    if not apt_get:
        return {"ok": False, "message": "apt-get is not available."}

    packages = request["packages"]
    plan = request["plan"]
    action = "purge" if request.get("purge") else "remove"

    returncode, output, actual_removed = _simulate_apt(apt_get, [action], packages)
    if returncode != 0:
        return {"ok": False, "message": f"APT simulation failed:\n{output}"}

    ok, message = _critical_apt_gate(packages, plan, actual_removed)
    if not ok:
        return {"ok": False, "message": message}

    command = _apt_command(apt_get, [action], packages)
    result = _run_streaming(stream, command, uses_apt=True)
    return {
        "ok": result.success,
        "message": result.output or ("Removed." if result.success else "Removal failed."),
        "returncode": result.returncode,
    }


def handle_purge_residual(request, stream):
    """Backup + purge of residual configurations, all as root (S2/S9)."""
    apt_get = shutil.which("apt-get")
    if not apt_get:
        return {"ok": False, "message": "apt-get is not available."}

    packages = request["packages"]
    plan = request["plan"]

    from . import residual as residual_module

    user = _target_user()
    backup_note = ""
    backup_dir = None
    if user is not None:
        # Write the backup into the invoking user's state directory so they
        # can inspect and restore it; the copy is chowned to them.
        os.environ["HOME"] = user.pw_dir
        os.environ.pop("XDG_STATE_HOME", None)

    try:
        status_map = residual_module._dpkg_status_map()
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "message": f"Could not verify residual state: {error}"}

    residual_set = {
        package
        for package, status in status_map.items()
        if status[:2] == "rc"
    }
    missing = [p for p in packages if p not in residual_set]
    if missing:
        return {
            "ok": False,
            "message": (
                "No longer in residual-configuration state (they may have "
                "been reinstalled):\n" + "\n".join(missing)
            ),
        }
    conflicts = residual_module._multiarch_conflicts(packages, status_map)
    if conflicts:
        return {
            "ok": False,
            "message": (
                "Multi-arch conflict: another architecture instance of "
                "these packages is installed:\n"
                + "\n".join(
                    f"{package}: {', '.join(others)}"
                    for package, others in conflicts.items()
                )
            ),
        }

    try:
        backup_dir, backed_up_count = residual_module._backup_residual_conffiles(
            packages,
            skip_unreadable=request.get("skip_unreadable_backup", False),
        )
    except OSError as error:
        return {"ok": False, "message": f"Backup failed; purge not run:\n{error}"}

    if user is not None and backup_dir is not None:
        _chown_tree(backup_dir, user.pw_uid, user.pw_gid)
        backup_note = (
            f"Pre-purge configuration backup written to {backup_dir} "
            f"({backed_up_count} file(s))."
        )
        stream.emit(
            "backup", path=str(backup_dir), files=backed_up_count
        )

    def _abort(message):
        # S9: a backup made for a purge that never starts is discarded
        # again, so aborted attempts cannot evict older, useful backups
        # from the keep-count.
        if backup_dir is not None:
            residual_module._discard_backup(backup_dir)
        return {"ok": False, "message": message}

    # Re-verify immediately before the purge (the backup was slow).
    try:
        status_after = residual_module._dpkg_status_map()
    except (OSError, subprocess.TimeoutExpired) as error:
        return _abort(f"Re-verification failed:\n{error}")
    changed = [
        p for p in packages
        if status_after.get(p, "")[:2] != "rc"
    ]
    if changed:
        return _abort(
            "Left residual state during the backup (reinstalled?):\n"
            + "\n".join(changed)
            + "\nThe pre-purge backup was discarded again."
        )
    late_conflicts = residual_module._multiarch_conflicts(packages, status_after)
    if late_conflicts:
        return _abort(
            "Multi-arch conflict appeared during the backup.\n"
            "The pre-purge backup was discarded again."
        )

    # Plan binding for the purge set. Residual purges are exempt from the
    # critical-name rule (every identity was verified to be in rc state,
    # i.e. only leftover configuration remains); plan binding, growth and
    # the removal cap still apply.
    returncode, output, actual_removed = _simulate_apt(apt_get, ["purge"], packages)
    if returncode != 0:
        return _abort(f"APT simulation failed:\n{output}")
    ok, message = _critical_apt_gate(
        packages, plan, actual_removed, residual=True
    )
    if not ok:
        return _abort(f"{message}\nThe pre-purge backup was discarded again.")

    command = _apt_command(apt_get, ["purge"], packages)
    result = _run_streaming(stream, command, uses_apt=True)
    return {
        "ok": result.success,
        "message": (result.output or "") + (f"\n\n{backup_note}" if backup_note else ""),
        "returncode": result.returncode,
    }


def _chown_tree(root, uid, gid):
    """Hand a root-written backup to the invoking user.

    Symlinks are never followed: the backup may legitimately contain
    symlinks (recreated conffile links), and chowning their *targets* would
    transfer ownership of arbitrary files to the invoking user.
    """
    root = Path(root)
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            os.chown(dirpath, uid, gid, follow_symlinks=False)
            for filename in filenames:
                try:
                    os.chown(
                        Path(dirpath) / filename,
                        uid,
                        gid,
                        follow_symlinks=False,
                    )
                except OSError:
                    pass
    except OSError:
        pass


def handle_remove_snap(request, stream):
    snap = shutil.which("snap")
    if not snap:
        return {"ok": False, "message": "snap is not available."}
    results = []
    ok_all = True
    for package in request["packages"]:
        result = _run_streaming(
            stream, [snap, "remove", "--", package], uses_apt=False
        )
        results.append((package, result.success))
        ok_all = ok_all and result.success
    message = "\n".join(
        f"{'OK' if success else 'FAILED'} {package}"
        for package, success in results
    )
    return {"ok": ok_all, "message": message}


def handle_remove_flatpak_system(request, stream):
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return {"ok": False, "message": "flatpak is not available."}
    installation = request.get("installation", "system")
    scope = ["--system"] if installation == "system" else [f"--installation={installation}"]
    command = [flatpak, "uninstall", "-y", *scope, "--", *request["packages"]]
    result = _run_streaming(stream, command, uses_apt=False)
    return {
        "ok": result.success,
        "message": result.output or ("Removed." if result.success else "Flatpak removal failed."),
    }


def handle_apt_autoremove(request, stream):
    apt_get = shutil.which("apt-get")
    if not apt_get:
        return {"ok": False, "message": "apt-get is not available."}

    returncode, output, actual_removed = _simulate_apt(apt_get, ["autoremove"], [])
    if returncode != 0:
        return {"ok": False, "message": f"APT simulation failed:\n{output}"}
    ok, message = _critical_apt_gate([], request["plan"], actual_removed)
    if not ok:
        return {"ok": False, "message": message}

    result = _run_streaming(
        stream, [apt_get, "autoremove", "-y"], uses_apt=True
    )
    return {"ok": result.success, "message": result.output or "Autoremove finished.",
            "returncode": result.returncode}


def handle_apt_upgrade(request, stream):
    apt_get = shutil.which("apt-get")
    if not apt_get:
        return {"ok": False, "message": "apt-get is not available."}
    packages = request["packages"]

    returncode, output, actual_removed = _simulate_apt(
        apt_get, ["install", "--only-upgrade"], packages
    )
    if returncode != 0:
        return {"ok": False, "message": f"APT simulation failed:\n{output}"}
    upgrade_ok, upgrade_message = check_apt_upgrade_plan(actual_removed)
    if not upgrade_ok:
        return {"ok": False, "message": upgrade_message}
    if sorted(packages) != sorted(request["plan"].package_ids):
        return {"ok": False, "message": "Upgrade set no longer matches the confirmed plan."}

    command = _apt_command(apt_get, ["install", "--only-upgrade"], packages)
    result = _run_streaming(stream, command, uses_apt=True)
    return {
        "ok": result.success,
        "message": result.output or ("Updated." if result.success else "Update failed."),
        "returncode": result.returncode,
    }


def handle_flatpak_update_system(request, stream):
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return {"ok": False, "message": "flatpak is not available."}
    command = [
        flatpak, "update", "-y", "--system", "--noninteractive",
        "--", *request["packages"],
    ]
    result = _run_streaming(stream, command, uses_apt=False)
    return {
        "ok": result.success,
        "message": result.output or ("Updated." if result.success else "Update failed."),
    }


HANDLERS = {
    "install-deb": handle_install_deb,
    "remove-apt": handle_remove_apt,
    "purge-residual": handle_purge_residual,
    "remove-snap": handle_remove_snap,
    "remove-flatpak-system": handle_remove_flatpak_system,
    "apt-autoremove": handle_apt_autoremove,
    "apt-upgrade": handle_apt_upgrade,
    "flatpak-update-system": handle_flatpak_update_system,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def process_request(request, stream=sys.stdout, authorized_action=None):
    """Validate and execute one request; returns the result event dict.

    ``authorized_action`` is the action pkexec authorized (argv[1] of the
    helper); the request's action must match it, so a request can never
    execute a different, more privileged operation than the one the user
    approved.
    """
    try:
        if (
            authorized_action is not None
            and request.get("action") not in (None, authorized_action)
        ):
            raise RequestError(
                f"Requested action {request.get('action')!r} does not match "
                f"the authorized action {authorized_action!r}."
            )
        normalized = validate_request(request)
    except RequestError as error:
        result_event = {
            "type": "result",
            "ok": False,
            "message": f"Rejected: {error}",
        }
        _write_event(stream, result_event)
        return result_event

    nonce = normalized["nonce"]
    _write_event(
        stream, {"type": "start", "nonce": nonce, "action": normalized["action"]}
    )
    event_stream = EventStream(stream, nonce)

    handler = HANDLERS[normalized["action"]]
    try:
        result = handler(normalized, event_stream)
    except Exception as error:  # a helper crash must still emit a result
        result = {"ok": False, "message": f"Helper error: {error}"}

    result_event = {
        "type": "result",
        "nonce": nonce,
        "ok": bool(result.get("ok")),
        "message": str(result.get("message", ""))[:65536],
    }
    if "returncode" in result:
        result_event["returncode"] = result["returncode"]
    _write_event(stream, result_event)
    return result_event


def _write_event(stream, event):
    try:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.geteuid() != 0:
        print("appector-helper must run as root (via pkexec).", file=sys.stderr)
        return 1
    if len(argv) != 1:
        print("usage: appector-helper <action>", file=sys.stderr)
        return 2

    try:
        request = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError) as error:
        _write_event(sys.stdout, {
            "type": "result",
            "ok": False,
            "message": f"Rejected: invalid JSON request: {error}",
        })
        return 1

    request = dict(request)
    request.setdefault("action", argv[0])
    result = process_request(request, authorized_action=argv[0])
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
