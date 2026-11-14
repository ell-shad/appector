"""Client for the privileged helper (user side). See ADR-0003.

Spawns `pkexec /usr/lib/appector/appector-helper <action>`, writes one JSON
request to its stdin and reads JSON-lines events from its stdout. Events
whose nonce does not match the request's are dropped, so spoofed output
from package maintainer scripts cannot forge a result.

Fails fast with a clear error when pkexec or the helper is unavailable —
the old code paths silently ran unprivileged and failed late, after
backups had already been made.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from .plans import Plan

HELPER_PATH = Path("/usr/lib/appector/appector-helper")
HELPER_ENV_OVERRIDE = "APECTOR_HELPER"

HELPER_TIMEOUT = 3600


class HelperUnavailable(RuntimeError):
    """pkexec or the helper is not installed; nothing was executed."""


class HelperProtocolError(RuntimeError):
    """The helper produced a malformed or spoofed response."""


@dataclass
class HelperResult:
    ok: bool
    message: str
    events: list[dict] = field(default_factory=list)
    returncode: int | None = None


def helper_path() -> Path | None:
    """Locate the helper: the APECTOR_HELPER override, then the system path."""
    override = os.environ.get(HELPER_ENV_OVERRIDE)
    if override:
        candidate = Path(override)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        raise HelperUnavailable(
            f"APECTOR_HELPER is set to {override}, which is not an executable "
            "file. Point it at the appector-helper program or unset it."
        )
    if HELPER_PATH.is_file() and os.access(HELPER_PATH, os.X_OK):
        return HELPER_PATH
    return None


def require_helper() -> Path:
    path = helper_path()
    if path is None:
        raise HelperUnavailable(
            "The privileged Appector helper is not installed. Install the "
            "appector .deb package (it ships /usr/lib/appector/appector-helper "
            "and the polkit policy), or set APECTOR_HELPER to the helper's "
            "path for development."
        )
    if shutil.which("pkexec") is None and os.geteuid() != 0:
        raise HelperUnavailable(
            "pkexec is required to run privileged Appector operations, but it "
            "is not installed. Install policykit-1 or pkexec."
        )
    return path


def _plan_payload(plan: Plan | None) -> dict | None:
    if plan is None:
        return None
    return {
        "action": plan.action,
        "package_ids": list(plan.package_ids),
        "removed_preview": list(plan.removed_preview),
        "text": "",
        "plan_hash": plan.plan_hash,
    }


def run_helper(
    action: str,
    *,
    packages: list[str] | None = None,
    plan: Plan | None = None,
    purge: bool = False,
    files: list[dict] | None = None,
    installation: str | None = None,
    options: dict | None = None,
    output_callback: Callable[[str], None] | None = None,
) -> HelperResult:
    """Run one helper action and return its result.

    Raises HelperUnavailable / HelperProtocolError; callers surface those
    as operation failures without any partial work.
    """
    path = require_helper()

    import secrets

    nonce = secrets.token_hex(16)

    request = {
        "version": 1,
        "action": action,
        "nonce": nonce,
    }
    if packages is not None:
        request["packages"] = list(packages)
    if plan is not None:
        request["plan"] = _plan_payload(plan)
        request["plan_hash"] = plan.plan_hash
    if purge:
        request["purge"] = True
    if files is not None:
        request["files"] = files
    if installation is not None:
        request["installation"] = installation
    if options:
        request["options"] = options

    command = [str(path), action]
    if os.geteuid() != 0:
        pkexec = shutil.which("pkexec")
        if not pkexec:
            raise HelperUnavailable("pkexec is required but not installed.")
        command = [pkexec, *command]

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as error:
        raise HelperUnavailable(f"Could not start the Appector helper: {error}") from error

    try:
        if process.stdin is None or process.stdout is None:
            raise HelperProtocolError(
                "The helper process was started without usable pipes."
            )
        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.close()
        except (BrokenPipeError, OSError):
            # The helper rejected the request before reading everything, or
            # refused to run; the result event tells us which.
            pass

        events: list[dict] = []
        result_event = None
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # non-JSON noise cannot be a protocol event
            if not isinstance(event, dict):
                continue
            if event.get("nonce") not in (None, nonce):
                continue  # spoofed or foreign event (S6): drop it
            if event.get("type") == "output" and output_callback is not None:
                try:
                    output_callback(str(event.get("line", "")))
                except Exception:
                    pass
            events.append(event)
            if event.get("type") == "result":
                result_event = event
                break

        try:
            stderr = process.stderr.read() if process.stderr else ""
        except (OSError, ValueError):
            stderr = ""
        try:
            returncode = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait()

        if result_event is None:
            detail = (stderr or "").strip()
            raise HelperProtocolError(
                "The Appector helper did not return a result."
                + (f"\n{detail}" if detail else "")
            )

        return HelperResult(
            ok=bool(result_event.get("ok")),
            message=str(result_event.get("message", "")),
            events=events,
            returncode=result_event.get("returncode", returncode),
        )
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


# ---------------------------------------------------------------------------
# High-level operations used by actions.py / residual.py / window.py
# ---------------------------------------------------------------------------


def install_deb(files, output_callback=None) -> HelperResult:
    """files: list of {"path": absolute path, "sha256": reviewed digest}."""
    return run_helper(
        "install-deb",
        files=files,
        output_callback=output_callback,
    )


def remove_apt(packages, plan, purge=False, output_callback=None) -> HelperResult:
    return run_helper(
        "remove-apt",
        packages=packages,
        plan=plan,
        purge=purge,
        output_callback=output_callback,
    )


def purge_residual(packages, plan, skip_unreadable_backup=False,
                   output_callback=None) -> HelperResult:
    return run_helper(
        "purge-residual",
        packages=packages,
        plan=plan,
        options={"skip_unreadable_backup": bool(skip_unreadable_backup)},
        output_callback=output_callback,
    )


def remove_snap(packages, output_callback=None) -> HelperResult:
    return run_helper("remove-snap", packages=packages, output_callback=output_callback)


def remove_flatpak_system(packages, installation=None, output_callback=None) -> HelperResult:
    return run_helper(
        "remove-flatpak-system",
        packages=packages,
        installation=installation,
        output_callback=output_callback,
    )


def apt_autoremove(plan, output_callback=None) -> HelperResult:
    return run_helper("apt-autoremove", plan=plan, output_callback=output_callback)


def apt_upgrade(packages, plan, output_callback=None) -> HelperResult:
    return run_helper(
        "apt-upgrade", packages=packages, plan=plan, output_callback=output_callback
    )


def flatpak_update_system(packages, output_callback=None) -> HelperResult:
    return run_helper(
        "flatpak-update-system", packages=packages, output_callback=output_callback
    )
