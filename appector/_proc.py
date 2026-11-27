"""Process execution and activity logging primitives.

Split out of `actions.py` so the residual-configuration subsystem and the
package-manager operations can share one implementation of "run a command
safely and log it" without importing each other.

Everything here is deliberately small and side-effect free apart from running
the requested command and appending to the private activity log.

Safety model of the runner (S7):

- every command runs with ``start_new_session=True`` so it can be terminated
  as a whole process group without touching Appector itself;
- timeout and cancellation kill the group (falling back to the direct child
  when the group contains processes we may not signal, e.g. a setuid pkexec);
- privileged commands are additionally wrapped in ``timeout(1)`` *under*
  pkexec, so a root process is killed by a root timeout even where we could
  not signal it;
- output is decoded with ``errors="replace"`` (maintainer scripts may print
  non-UTF-8 bytes), ANSI-stripped, and truncated to a bounded tail so
  runaway output cannot exhaust memory;
- ``LC_ALL=C`` is forced by default so callers may match on tool output;
- APT commands are serialised behind a lock, selected by an explicit
  ``uses_apt`` argument rather than by sniffing the command line.
"""

import logging
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .paths import is_acceptable_private_dir, state_dir

ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
APT_COMMAND_LOCK = threading.Lock()
APT_COMMAND_NAMES = {"apt", "apt-get", "dpkg"}

_ACTIVITY_LOG_NAME = "actions.log"
_ACTIVITY_LOG_MAX_BYTES = 1024 * 1024
_ACTIVITY_LOG_KEEP = 3

# How much combined output a single command may retain (the tail is kept).
MAX_OUTPUT_BYTES = 512 * 1024

# Environment a privileged command needs but pkexec strips (S5). Applied by
# inserting an explicit `env` command into the privileged argv, because
# setting these in the parent environment has no effect on the sanitized
# environment pkexec gives the target process.
PKEXEC_ENV = {
    "DEBIAN_FRONTEND": "noninteractive",
    "NEEDRESTART_MODE": "a",
    "APT_LISTCHANGES_FRONTEND": "none",
    "LC_ALL": "C",
}

# Conffile prompt policy for non-interactive APT transactions: keep the
# currently-installed version, or the default when the local file was never
# modified. Without this, a dpkg that decides to prompt hangs the run (its
# stdin is /dev/null) or fails.
APT_NONINTERACTIVE_OPTIONS = [
    "-o", "Dpkg::Options::=--force-confold",
    "-o", "Dpkg::Options::=--force-confdef",
]


@dataclass
class RunResult:
    """Outcome of one command run."""

    returncode: int = 0
    output: str = ""
    timed_out: bool = False
    cancelled: bool = False
    exited_cleanly: bool = True
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled


def _command_uses_apt(cmd) -> bool:
    """Best-effort detection; callers should pass ``uses_apt`` explicitly."""
    for argument in map(str, cmd):
        if Path(argument).name in APT_COMMAND_NAMES:
            return True
        if re.search(r"(?<![\w.-])(?:apt-get|dpkg)(?=\s)", argument):
            return True
    return False


def _format_apt_lock_error(output: str) -> str:
    lowered = output.lower()
    lock_messages = (
        "could not get lock",
        "unable to acquire",
        "is locked by another process",
        "dpkg frontend lock",
        "dpkg is locked",
    )
    if any(message in lowered for message in lock_messages):
        return (
            "Another package-management operation is using APT/dpkg. "
            "Wait for it to finish, then try again.\n\n"
            f"{output}"
        )
    return output


def _strip_ansi(text: str) -> str:
    """Removes terminal color and cursor control codes from text."""
    if not text:
        return ""
    return ANSI_ESCAPE_RE.sub('', text)


def _apt_lock_busy_message() -> str:
    return (
        "Another APT/dpkg operation is already running in Appector. "
        "Wait for it to finish, then try again."
    )


def _kill_process_tree(process, *, graceful_first=True):
    """Terminate a child process and its whole process group.

    ``start_new_session=True`` makes the child a group leader, so killing
    the group reaches grandchildren (sh -c scripts, apt-get under pkexec).
    Some members may be setuid-root and unsignallable by us; those are left
    to the root-side ``timeout`` wrapper, and errors are swallowed rather
    than reported as command failures.
    """
    def _signal(sig):
        try:
            pgid = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = None
        if pgid is not None and pgid > 0:
            try:
                os.killpg(pgid, sig)
                return True
            except (PermissionError, ProcessLookupError, OSError):
                pass
        try:
            process.send_signal(sig)
            return True
        except (PermissionError, ProcessLookupError, OSError, ValueError):
            return False

    if graceful_first:
        _signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
    _signal(signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _wrap_privileged(command, timeout):
    """Adapt a pkexec command line for the runner.

    Inserts, under pkexec:

    1. an ``env`` command carrying the non-interactive settings pkexec
       strips from the environment (S5 interim fix), and
    2. a root-side ``timeout`` so a hung privileged process is killed by a
       process that is allowed to signal it (S7a).
    """
    wrapped = [command[0]]
    rest = list(command[1:])
    if rest and Path(rest[0]).name == "env":
        # Already wrapped by the caller.
        wrapped.extend(rest)
        return wrapped

    wrapped.append(shutil.which("env") or "/usr/bin/env")
    wrapped.extend(f"{key}={value}" for key, value in PKEXEC_ENV.items())

    timeout_command = shutil.which("timeout")
    if timeout and timeout_command:
        wrapped.extend(
            [
                timeout_command,
                "--signal=TERM",
                "--kill-after=10s",
                str(timeout),
            ]
        )
    wrapped.extend(rest)
    return wrapped


def run(
    cmd,
    *,
    timeout=900,
    env_overrides=None,
    stream_cb=None,
    cancel=None,
    uses_apt=None,
    keep_bytes=MAX_OUTPUT_BYTES,
):
    """Run a command once, returning a :class:`RunResult`.

    ``stream_cb(line)`` receives ANSI-stripped lines as they arrive; the
    bounded tail is always also collected into ``output``. ``cancel`` is a
    ``threading.Event``; when set, the process group is terminated and the
    result reports ``cancelled=True``. ``uses_apt`` must be passed by
    callers that know whether the command touches APT/dpkg (it falls back
    to detection otherwise).
    """
    command = [str(part) for part in cmd]
    if not command:
        return RunResult(returncode=1, output="No command was provided.")

    if uses_apt is None:
        uses_apt = _command_uses_apt(command)

    if uses_apt and not APT_COMMAND_LOCK.acquire(blocking=False):
        return RunResult(
            returncode=1,
            output=_apt_lock_busy_message(),
            exited_cleanly=False,
        )

    env = os.environ.copy()
    # Forced C locale: callers match on tool output ("Remv ", "already
    # installed", ...), which is translated under any other locale.
    env["LC_ALL"] = "C"
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["PAGER"] = "cat"
    if env_overrides:
        env.update(env_overrides)

    # pkexec strips the environment; carry the settings inside the argv.
    run_command = list(command)
    if Path(run_command[0]).name == "pkexec":
        run_command = _wrap_privileged(run_command, timeout)
        # The root-side timeout enforces the deadline; keep our own timer as
        # a backstop for the case where even pkexec cannot be signalled.
        own_timeout = timeout + 30 if timeout else None
    else:
        own_timeout = timeout

    process = None
    selector = None
    timer = None
    timed_out = threading.Event()
    result = RunResult()

    try:
        process = subprocess.Popen(
            run_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            bufsize=0,
        )

        if own_timeout:

            def _on_timeout():
                timed_out.set()
                _kill_process_tree(process)

            timer = threading.Timer(own_timeout, _on_timeout)
            timer.daemon = True
            timer.start()

        selector = selectors.DefaultSelector()
        stdout_fd = process.stdout.fileno()
        selector.register(stdout_fd, selectors.EVENT_READ)

        lines = []
        retained_bytes = 0
        truncated = False
        pending = b""

        while True:
            events = selector.select(timeout=0.2)
            if events:
                # A readable pipe means data or EOF; read() never blocks here.
                chunk = os.read(stdout_fd, 65536)
                if not chunk:
                    break
                pending += chunk
                if len(pending) > keep_bytes:
                    # A child that writes megabytes without a newline must
                    # not grow the buffer unboundedly; keep the newest tail
                    # (the already-collected lines stay bounded separately).
                    pending = pending[-keep_bytes:]
                while b"\n" in pending:
                    raw_line, pending = pending.split(b"\n", 1)
                    line = _strip_ansi(
                        raw_line.decode("utf-8", errors="replace").rstrip("\r")
                    )
                    if retained_bytes > keep_bytes:
                        if lines:
                            lines.pop(0)
                            truncated = True
                    else:
                        retained_bytes += len(line) + 1
                    lines.append(line)
                    if stream_cb is not None:
                        try:
                            stream_cb(line)
                        except Exception:  # a display callback must not kill the run
                            pass
            elif process.poll() is not None:
                # The child exited; give any in-flight write one last look.
                if selector.select(timeout=0):
                    continue
                break

            if cancel is not None and cancel.is_set():
                result.cancelled = True
                _kill_process_tree(process)
                break
            if timed_out.is_set():
                break

        if pending:
            line = _strip_ansi(pending.decode("utf-8", errors="replace").rstrip("\r"))
            lines.append(line)
            if stream_cb is not None:
                try:
                    stream_cb(line)
                except Exception:
                    pass

        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process, graceful_first=False)
            result.exited_cleanly = False

        result.returncode = process.returncode
        if truncated:
            lines.insert(0, "…older output truncated…")
        result.output = "\n".join(lines).strip()

        if timed_out.is_set():
            result.timed_out = True
            result.returncode = 124
            result.output = (
                f"{result.output}\nCommand timed out after {timeout}s."
                if result.output
                else f"Command timed out after {timeout}s."
            ).strip()
        elif result.cancelled:
            result.returncode = 130
            result.output = (
                f"{result.output}\nCommand was cancelled." if result.output
                else "Command was cancelled."
            ).strip()

        return result
    except FileNotFoundError as error:
        return RunResult(returncode=127, output=f"Command not found: {error}",
                         exited_cleanly=False)
    except OSError as error:
        return RunResult(returncode=1, output=str(error), exited_cleanly=False)
    finally:
        if timer is not None:
            timer.cancel()
        if selector is not None:
            selector.close()
        if process is not None and process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        if uses_apt:
            APT_COMMAND_LOCK.release()


def _run_command_raw(cmd, timeout=900, env_overrides=None, uses_apt=None):
    """Run a command and return (returncode, combined output).

    Kept as a thin compatibility wrapper over :func:`run`; new code should
    call ``run`` directly.
    """
    result = run(
        cmd,
        timeout=timeout,
        env_overrides=env_overrides,
        uses_apt=uses_apt,
    )
    output = result.output
    if result.returncode:
        output = _format_apt_lock_error(output)
    return result.returncode, output


def _run_command(cmd, **kwargs):
    returncode, output = _run_command_raw(cmd, **kwargs)

    if returncode == 0:
        return True, output if output else "Command completed successfully."

    return False, output if output else "Command failed."


def _run_command_stream(cmd, output_callback=None, timeout=900, env_overrides=None,
                        uses_apt=None, cancel=None):
    """Run a command, streaming combined output line by line.

    stdin is closed and APT is forced non-interactive so maintainer scripts
    cannot block waiting for input. Cancellation and process-group cleanup
    are handled by :func:`run`.
    """
    result = run(
        cmd,
        timeout=timeout,
        env_overrides=env_overrides,
        stream_cb=output_callback,
        uses_apt=uses_apt,
        cancel=cancel,
    )

    if result.cancelled:
        return False, result.output or "Command was cancelled."
    if result.timed_out:
        return False, result.output or "Command timed out."
    if result.returncode == 0:
        return True, result.output if result.output else "Command completed successfully."

    output = result.output or f"Command failed with exit code {result.returncode}."
    return False, _format_apt_lock_error(output)


def _sanitize_log_message(message: str) -> str:
    """Make one log line out of any caller text.

    Package names and paths come from tool output; embedded newlines would
    forge additional log records, so they are escaped visibly.
    """
    return str(message).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def _rotate_log_if_needed(log_file: Path):
    """Rotate the activity log once it exceeds the size cap.

    Keeps ``_ACTIVITY_LOG_KEEP`` rotated copies; rotation failures are
    non-fatal (the log is an audit aid, not a gate).
    """
    try:
        if log_file.stat().st_size < _ACTIVITY_LOG_MAX_BYTES:
            return
        for index in range(_ACTIVITY_LOG_KEEP - 1, 0, -1):
            source = log_file.with_name(f"{_ACTIVITY_LOG_NAME}.{index}")
            destination = log_file.with_name(f"{_ACTIVITY_LOG_NAME}.{index + 1}")
            if source.exists():
                os.replace(source, destination)
        os.replace(log_file, log_file.with_name(f"{_ACTIVITY_LOG_NAME}.1"))
    except OSError:
        pass


def _log_action(message: str):
    """Append to the private per-user activity log.

    Failures are logged as a warning rather than raised: losing an audit line
    must never abort the operation the user asked for. The log may contain
    package names and local paths, so it is written 0600 in a 0700 directory
    and symlinked paths are refused.
    """
    import stat as _stat

    log_dir = state_dir()
    log_file = log_dir / _ACTIVITY_LOG_NAME
    descriptor = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not is_acceptable_private_dir(log_dir):
            raise OSError(
                "Action log directory is not a private directory."
            )
        os.chmod(log_dir, 0o700)
        _rotate_log_if_needed(log_file)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(log_file, flags, 0o600)
        file_stat = os.fstat(descriptor)
        if not _stat.S_ISREG(file_stat.st_mode):
            raise OSError("Action log path is not a regular file.")
        os.fchmod(descriptor, 0o600)
        timestamp = datetime.now().astimezone().isoformat()
        safe_message = _sanitize_log_message(message)
        with os.fdopen(descriptor, "a", encoding="utf-8") as f:
            descriptor = None
            f.write(f"{timestamp} {safe_message}\n")
    except OSError:
        logging.getLogger(__name__).warning(
            "Appector could not write its private action log."
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)


def shlex_join(cmd) -> str:
    """Logging-safe rendering of a command line."""
    return shlex.join(str(part) for part in cmd)
