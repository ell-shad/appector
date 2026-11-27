"""Flatpak operations, extracted from actions.py (ADR-0002).

One cohesive surface for the Flatpak package manager: remotes, app-ID and
.flatpakref installs (https-only, review-bound — S3), batch installs,
and the unused-runtime cleanup. `actions` re-exports these names for
callers; new Flatpak code belongs here.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

# _maybe_delete_source_file and get_removal_size_estimate live in
# actions.py; importing them here would create a cycle (actions imports
# this module for the re-exports), so they are resolved lazily at the
# call sites that need them.
from .._proc import _log_action, _run_command_stream
from ..paths import state_dir  # noqa: F401  (kept for callers patching paths)

FLATPAK_PROMPT_RE = re.compile(
    r"(\[(?:y/n|Y/n|n/Y|n/y)\])|(proceed with these changes)",
    re.IGNORECASE,
)

FLATHUB_REMOTE_NAME = "flathub"
FLATHUB_REMOTE_URL = "https://dl.flathub.org/repo/"


def _maybe_delete_source_file(path, delete_source, message):
    """Lazy re-export to avoid a circular import with actions."""
    from ..actions import _maybe_delete_source_file as _impl

    return _impl(path, delete_source, message)


def _run_command(cmd, **kwargs):
    """Lazy re-export to avoid a circular import with _proc."""
    from .._proc import _run_command as _impl

    return _impl(cmd, **kwargs)


def get_removal_size_estimate(*args, **kwargs):
    """Lazy re-export to avoid a circular import with actions."""
    from ..actions import get_removal_size_estimate as _impl

    return _impl(*args, **kwargs)



def get_flatpak_remotes(user_install: bool = True):
    """
    Returns a list of configured Flatpak remote names.
    """

    flatpak_command = shutil.which("flatpak")

    if not flatpak_command:
        return []

    scope = "--user" if user_install else "--system"

    cmd = [
        flatpak_command,
        "remotes",
        scope,
        "--columns=name",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=10,
            env={**os.environ, "LC_ALL": "C"},
        )

        if result.returncode != 0:
            return []

        return [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        ]
    except (OSError, subprocess.SubprocessError):
        return []


def flathub_remote_exists(user_install: bool = True) -> bool:
    """
    Checks whether the Flathub remote exists in the selected scope.
    """

    try:
        remotes = get_flatpak_remotes(user_install)
        return FLATHUB_REMOTE_NAME in remotes
    except Exception:
        return False


def add_flathub_remote(user_install: bool = True):
    """
    Adds the Flathub remote.

    For user installation:
        flatpak remote-add --user --if-not-exists flathub <url>

    For system installation:
        pkexec flatpak remote-add --system --if-not-exists flathub <url>
    """

    flatpak_command = shutil.which("flatpak")

    if not flatpak_command:
        return False, "flatpak command not found."

    scope = "--user" if user_install else "--system"

    base_cmd = [
        flatpak_command,
        "remote-add",
        scope,
        "--if-not-exists",
        FLATHUB_REMOTE_NAME,
        FLATHUB_REMOTE_URL,
    ]

    if user_install:
        cmd = base_cmd
    else:
        # S6: no silent unprivileged fallback — adding a system-wide remote
        # needs root, and pretending otherwise fails late and confusingly.
        pkexec_command = shutil.which("pkexec")
        if not pkexec_command:
            return False, (
                "pkexec is required to add a system-wide Flatpak remote, but "
                "it is not installed."
            )
        cmd = [pkexec_command, *base_cmd]

    _log_action(f"FLATHUB_ADD_START cmd={shlex.join(cmd)}")

    success, message = _run_command(cmd)

    if success:
        _log_action(f"FLATHUB_ADD_SUCCESS scope={scope}")
    else:
        _log_action(f"FLATHUB_ADD_FAILED scope={scope} error={message}")

    return success, message


def _flatpak_install_command(
    target: str,
    user_install: bool = True,
    remote: str | None = None,
):
    """
    Builds a Flatpak install command for a target.

    Target can be:
    - app ID (requires an explicit remote so a different configured remote
      cannot satisfy the ID, S3)
    - local .flatpakref path (absolute)
    - remote .flatpakref URL (https:// only)
    """

    flatpak_command = shutil.which("flatpak")

    if not flatpak_command:
        return None, "flatpak command not found."

    scope = "--user" if user_install else "--system"

    cmd = [
        flatpak_command,
        "install",
        "-y",
        scope,
    ]

    if remote:
        cmd.append(remote)

    # Option terminator: nothing after this can be parsed as a flag, even
    # if a file name or app id were to start with "-". Local file targets
    # are passed as absolute paths by their callers (S3); URLs and app IDs
    # are used verbatim.
    cmd.append("--")
    cmd.append(str(target))

    if not user_install:
        pkexec_command = shutil.which("pkexec")
        if not pkexec_command:
            return None, (
                "pkexec is required to install system-wide Flatpak apps, but "
                "it is not installed."
            )
        cmd = [pkexec_command, *cmd]

    return cmd, None


def _flatpak_is_installed(app_id: str, user_install: bool) -> bool:
    """Locale-independent installed pre-check via `flatpak info` (F11)."""
    flatpak_command = shutil.which("flatpak")
    if not flatpak_command:
        return False
    scope = "--user" if user_install else "--system"
    try:
        result = subprocess.run(
            [flatpak_command, "info", "--app", scope, app_id],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def install_flatpak_app_id(app_id: str, user_install: bool = True, output_callback=None):
    """
    Installs a Flatpak app by app ID.

    The remote is passed explicitly (`flatpak install flathub <id>`) so the
    ID cannot be resolved through a different configured remote (S3).
    """

    app_id = app_id.strip()

    if not app_id:
        return False, "Flatpak app ID is empty."

    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", app_id):
        return False, "Invalid Flatpak app ID.\n\nExample: org.gimp.GIMP"

    if not flathub_remote_exists(user_install):
        return False, (
            "The Flathub remote is not configured in this scope.\n"
            "Add Flathub first, then install the app."
        )

    if _flatpak_is_installed(app_id, user_install):
        _log_action(f"FLATPAK_INSTALL_ALREADY_INSTALLED app_id={app_id}")
        return True, f"Already installed: {app_id}"

    cmd, error = _flatpak_install_command(
        app_id, user_install, remote=FLATHUB_REMOTE_NAME
    )

    if error:
        return False, error

    _log_action(f"FLATPAK_INSTALL_START cmd={shlex.join(cmd)}")

    def filtered_output(line):
        if output_callback and "already installed" not in line.lower():
            output_callback(line)

    success, message = _run_command_stream(cmd, filtered_output if output_callback else None)

    if not success and _flatpak_output_is_already_installed(message):
        _log_action(f"FLATPAK_INSTALL_ALREADY_INSTALLED app_id={app_id}")
        return True, f"Already installed: {app_id}"

    if success:
        _log_action(f"FLATPAK_INSTALL_SUCCESS app_id={app_id}")
    else:
        if (
            "No refs found" in message
            or "No remote refs" in message
            or "Unable to find" in message
        ):
            message += (
                "\n\nHint: The app was not found in your configured Flatpak remotes.\n"
                "If you are installing from Flathub, add the Flathub remote first."
            )

        _log_action(f"FLATPAK_INSTALL_FAILED app_id={app_id} error={message}")

    return success, message


def _flatpak_output_is_already_installed(message: str):
    return "already installed" in (message or "").lower()


def parse_flatpak_app_inputs(text: str):
    """Validate and deduplicate newline-separated Flatpak IDs or Flathub URLs."""
    app_ids = []
    issues = []
    seen = set()

    for line_number, raw_value in enumerate(text.splitlines(), start=1):
        value = raw_value.strip()

        if not value:
            continue

        app_id = extract_flathub_app_id(value) or value

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", app_id):
            issues.append(f"Line {line_number}: invalid app ID or Flathub URL.")
            continue

        if app_id in seen:
            issues.append(f"Line {line_number}: duplicate app ignored ({app_id}).")
            continue

        seen.add(app_id)
        app_ids.append(app_id)

    return app_ids, issues


def install_flatpak_app_id_batch(app_ids, user_install: bool = True, output_callback=None):
    """Install a sequence of validated Flatpak app IDs, continuing after failures."""
    if not app_ids:
        return True, "Installed (0): none\nAlready installed (0): none\nFailed (0): none", 0

    installed = []
    already_installed = []
    failures = []
    success_count = 0
    total = len(app_ids)

    for index, app_id in enumerate(app_ids, start=1):
        if output_callback:
            output_callback(f"[{index}/{total}] Installing {app_id}…")

        success, message = install_flatpak_app_id(
            app_id,
            user_install,
            output_callback,
        )

        if success:
            success_count += 1
            if _flatpak_output_is_already_installed(message):
                already_installed.append(app_id)
                result = "ALREADY INSTALLED"
            else:
                installed.append(app_id)
                result = "INSTALLED"
        else:
            message_lines = (message or "").splitlines()
            first_line = message_lines[0] if message_lines else "Installation failed"
            failures.append(f"{app_id}: {first_line}")
            result = "FAILED"

        if output_callback:
            output_callback(f"[{index}/{total}] {result} {app_id}")

    summary = _format_flatpak_batch_summary(
        installed,
        already_installed,
        failures,
    )

    return not failures, summary, success_count


def _format_flatpak_batch_summary(installed, already_installed, failures):
    def format_section(title, entries):
        if not entries:
            return f"{title} (0): none"
        return f"{title} ({len(entries)}):\n" + "\n".join(
            f"  {entry}" for entry in entries[:20]
        )

    sections = [
        format_section("Installed", installed),
        format_section("Already installed", already_installed),
        format_section("Failed", failures),
    ]

    if len(installed) > 20 or len(already_installed) > 20 or len(failures) > 20:
        sections.append("Lists are limited to the first 20 entries per section.")

    return "\n\n".join(sections)


def extract_flathub_app_id(value: str):
    """Return the app ID from a Flathub app page URL, if value is one."""
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or hostname is None
        or hostname.lower() not in {"flathub.org", "www.flathub.org"}
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return None

    segments = [segment for segment in parsed.path.split("/") if segment]

    if (
        len(segments) == 3
        and re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", segments[0])
        and segments[1] == "apps"
    ):
        app_id = segments[2]
    elif len(segments) == 2 and segments[0] == "apps":
        app_id = segments[1]
    else:
        return None

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", app_id):
        return None

    return app_id


FLATPAKREF_REVIEW_FIELDS = (
    ("Name", "App name"),
    ("Branch", "Branch"),
    ("Url", "Repository URL"),
    ("SuggestRemoteName", "Suggested remote name"),
    ("RuntimeRepo", "Runtime repository"),
    ("IsRuntime", "Is a runtime"),
)


def parse_flatpakref_file(path):
    """Parse a local .flatpakref file into its key/value pairs (S3).

    The file is simple `Key=Value` text without groups. Returns a dict with
    lowercased keys.
    """
    fields = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                fields.setdefault(key.strip().lower(), value.strip())
    except OSError:
        pass
    return fields


def flatpakref_https_ok(fields) -> bool:
    """S3: repositories declared by a ref file must use https://."""
    for key in ("url", "runtimerepo"):
        value = str(fields.get(key, "") or "").strip()
        if value and value.lower().startswith("http://"):
            return False
    return True


def flatpakref_review_text(path) -> str:
    """Human-readable review of one local .flatpakref file (S3).

    The user must see what a ref file will add (remote, GPG key, runtime
    repository) before it is handed to flatpak.
    """
    fields = parse_flatpakref_file(path)
    if not fields:
        return f"Could not read the Flatpak reference file: {path}"

    lines = [f"Flatpak reference file: {Path(path).name}", ""]
    for key, label in FLATPAKREF_REVIEW_FIELDS:
        value = fields.get(key.lower(), "")
        if value:
            lines.append(f"{label}: {value}")

    has_gpg_key = bool(fields.get("gpgkey"))
    lines.append(
        "Embedded GPG key: yes (the remote will be trusted automatically)"
        if has_gpg_key
        else "Embedded GPG key: no"
    )

    url = fields.get("url", "")
    if url.lower().startswith("http://"):
        lines.append(
            "WARNING: the repository URL uses plain HTTP and could be "
            "altered in transit. Appector refuses to install it."
        )

    lines.append("")
    lines.append(
        "Installing a .flatpakref file adds its remote, its GPG key, and "
        "any runtime repository it declares to your Flatpak configuration."
    )
    return "\n".join(lines)


def _install_flatpak_location(location: str, user_install: bool = True, output_callback=None):
    """
    Installs from a location.

    Location can be:
    - local .flatpakref path (absolute)
    - remote .flatpakref URL (https:// only; http:// is refused by the
      callers that accept URLs)
    """
    if not str(location).startswith(("http://", "https://")):
        location = str(Path(location).expanduser().absolute())

    cmd, error = _flatpak_install_command(location, user_install)

    if error:
        return False, error

    _log_action(f"FLATPAK_INSTALL_LOCATION_START cmd={shlex.join(cmd)}")

    success, message = _run_command_stream(cmd, output_callback)

    if success:
        _log_action(f"FLATPAK_INSTALL_LOCATION_SUCCESS location={location}")
    else:
        _log_action(f"FLATPAK_INSTALL_LOCATION_FAILED location={location} error={message}")

    return success, message


def install_flatpak_ref_file(
    path: str,
    user_install: bool = True,
    output_callback=None,
    delete_source: bool = False,
    reviewed_fields=None,
):
    """
    Install a local .flatpakref file (S3).

    The file must have been shown to the user first; when ``reviewed_fields``
    is provided (the parsed keys captured by the review dialog) the file is
    re-parsed before install and any change refuses the install. The
    Url=-fallback install was removed: Url= is an OSTree repository URL, not
    an installable reference, so the fallback only fetched a
    file-controlled URL for no benefit.
    """

    ref_path = Path(path).expanduser().absolute()

    if not ref_path.is_file():
        return False, "Selected .flatpakref file not found."

    fields = parse_flatpakref_file(ref_path)
    if not fields:
        return False, f"Could not read the Flatpak reference file: {ref_path}"

    if reviewed_fields is not None and fields != reviewed_fields:
        _log_action(f"FLATPAK_REF_INSTALL_BLOCKED reason=file-changed path={ref_path}")
        return False, (
            "The .flatpakref file changed after you reviewed it. No "
            "installation was run; review the file again."
        )

    if not flatpakref_https_ok(fields):
        _log_action(f"FLATPAK_REF_INSTALL_BLOCKED reason=plain-http path={ref_path}")
        return False, (
            "The .flatpakref file declares a repository over plain HTTP. "
            "Its contents (including the GPG key) could be altered in "
            "transit, so Appector only installs references over HTTPS."
        )

    def filtered_output(line):
        if output_callback and "already installed" not in line.lower():
            output_callback(line)

    success, message = _install_flatpak_location(
        str(ref_path),
        user_install,
        filtered_output if output_callback else None,
    )

    if success or _flatpak_output_is_already_installed(message):
        if _flatpak_output_is_already_installed(message) and not success:
            message = f"Already installed: {ref_path.stem}"
            _log_action(f"FLATPAK_REF_ALREADY_INSTALLED path={ref_path}")
        message = _maybe_delete_source_file(ref_path, delete_source, message)
        return success or True, message

    return False, message


def install_flatpak_source(
    source_type: str,
    value: str,
    user_install: bool = True,
    output_callback=None,
    delete_source: bool = False,
):
    """
    Unified Flatpak installation entry point.

    source_type:
        "id"  -> Flathub/Flatpak app ID
        "ref" -> .flatpakref file or URL
    """

    value = value.strip()

    if not value:
        return False, "No Flatpak source provided."

    flathub_app_id = extract_flathub_app_id(value)

    if source_type == "id":
        return install_flatpak_app_id(
            flathub_app_id or value,
            user_install,
            output_callback,
        )

    if source_type == "ref":
        if flathub_app_id:
            return install_flatpak_app_id(
                flathub_app_id,
                user_install,
                output_callback,
            )

        if value.startswith("http://"):
            # S3: a plain-HTTP .flatpakref can be altered in transit
            # (Url=/GPGKey= swapped), so it is refused before anything is
            # fetched or run.
            _log_action("FLATPAK_REF_INSTALL_BLOCKED reason=plain-http-url")
            return False, (
                "Plain-HTTP Flatpak references are refused: the file (and "
                "the repository and GPG key it declares) could be altered "
                "in transit. Use an https:// URL instead."
            )

        if value.startswith("https://"):
            return _install_flatpak_location(
                value,
                user_install,
                output_callback,
            )

        return install_flatpak_ref_file(
            value,
            user_install,
            output_callback,
            delete_source,
        )

    return False, "Unknown Flatpak source type."


def get_flatpak_unused_preview():
    """
    Asks Flatpak for its unused-removal plan and declines the confirmation.

    The preview is produced by answering "n" to Flatpak's confirmation prompt,
    so nothing is removed. If the prompt cannot be recognised the check fails
    instead of showing a preview that may not match the real transaction.

    Returns:
        success: bool
        preview_text: str
        raw_output: str
    """
    flatpak_command = shutil.which("flatpak")
    if not flatpak_command:
        return False, "", "flatpak command not found."

    outputs = []
    previews = []
    refs_by_scope = {}
    env = os.environ.copy()
    env["LC_ALL"] = "C"

    for scope in ("--user", "--system"):
        cmd = [
            flatpak_command,
            "uninstall",
            scope,
            "--unused",
        ]
        try:
            result = subprocess.run(
                cmd,
                input="n\n",
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, "", f"Flatpak unused-runtime check timed out ({scope})."
        except OSError as error:
            return False, "", f"Could not check unused Flatpak runtimes ({scope}): {error}"

        output = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part.strip()
        )
        outputs.append(f"[{scope.removeprefix('--')}]
{output}".rstrip())

        if result.returncode == 0 and "Nothing unused to uninstall" in output:
            continue

        # Flatpak exits non-zero when the confirmation is declined. Treat that
        # as a valid preview only when the prompt is clearly recognised and
        # nothing was written to stderr.
        declined_prompt = (
            bool(FLATPAK_PROMPT_RE.search(output))
            and not result.stderr.strip()
        )

        if result.returncode != 0 and not declined_prompt:
            error = output or (
                f"Flatpak unused-runtime check failed with exit code {result.returncode}."
            )
            return False, "", f"{scope}: {error}"

        if output:
            refs = list(dict.fromkeys(re.findall(
                r"\b((?:[A-Za-z0-9_-]+\.)+[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+)\b",
                output,
            )))
            if refs:
                refs_by_scope[scope] = refs
            preview = "\n".join(
                line
                for line in output.splitlines()
                if not FLATPAK_PROMPT_RE.search(line)
            ).strip()
            if preview:
                previews.append(
                    f"{scope.removeprefix('--').title()} installation:\n{preview}"
                )

    if refs_by_scope:
        size_estimate = get_removal_size_estimate(
            [],
            flatpak_refs_by_scope=refs_by_scope,
        )
        previews.append(size_estimate)

    return True, "\n\n".join(previews), "\n\n".join(outputs)


def execute_flatpak_unused_cleanup(output_callback=None):
    """
    Executes flatpak uninstall --unused.
    Streams output line-by-line via output_callback.

    Returns:
        success: bool
        message: str
    """
    flatpak_command = shutil.which("flatpak")

    if not flatpak_command:
        return False, "flatpak command not found."

    results = []
    failures = []
    for scope in ("--user", "--system"):
        cmd = [flatpak_command, "uninstall", scope, "--unused", "-y"]
        _log_action(f"FLATPAK_UNUSED_CLEANUP_START cmd={shlex.join(cmd)}")
        success, message = _run_command_stream(
            cmd,
            output_callback=output_callback,
            timeout=900,
        )
        scope_name = scope.removeprefix("--")
        results.append(f"{scope_name}: {message}")
        if not success:
            failures.append(scope_name)

    combined = "\n\n".join(results)
    if failures:
        _log_action(
            f"FLATPAK_UNUSED_CLEANUP_FAILED scopes={','.join(failures)}"
        )
        return False, f"Cleanup failed for: {', '.join(failures)}.\n\n{combined}"

    _log_action("FLATPAK_UNUSED_CLEANUP_SUCCESS")
    return True, combined
