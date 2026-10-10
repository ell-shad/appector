"""Process execution and activity logging primitives.

Split out of `actions.py` so the residual-configuration subsystem and the
package-manager operations can share one implementation of "run a command
safely and log it" without importing each other.

Everything here is deliberately small and side-effect free apart from running
the requested command and appending to the private activity log.
"""

import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
APT_COMMAND_LOCK = threading.Lock()
APT_COMMAND_NAMES = {"apt", "apt-get", "dpkg"}

_ACTIVITY_LOG_NAME = "actions.log"


def _command_uses_apt(cmd) -> bool:
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


def state_dir() -> Path:
    """The per-user Appector state directory."""
    return Path.home() / ".local" / "state" / "app-manager"


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
        home_dir = Path.home().resolve()
        if log_dir.is_symlink() or not log_dir.resolve().is_relative_to(home_dir):
            raise OSError(
                "Action log directory is not a private directory in the home folder."
            )
        os.chmod(log_dir, 0o700)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(log_file, flags, 0o600)
        file_stat = os.fstat(descriptor)
        if not _stat.S_ISREG(file_stat.st_mode):
            raise OSError("Action log path is not a regular file.")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as f:
            descriptor = None
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except OSError:
        logging.getLogger(__name__).warning(
            "Appector could not write its private action log."
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _run_command_raw(cmd, timeout=900, env_overrides=None):
    """Run a command and return (returncode, combined output).

    Serialises APT/dpkg work behind a lock so two Appector windows cannot race
    for the dpkg frontend lock, and normalises interactive APT behaviour.
    """
    uses_apt = _command_uses_apt(cmd)
    if uses_apt and not APT_COMMAND_LOCK.acquire(blocking=False):
        return 1, (
            "Another APT/dpkg operation is already running in Appector. "
            "Wait for it to finish, then try again."
        )

    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["PAGER"] = "cat"
    if env_overrides:
        env.update(env_overrides)

    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        stdout = _strip_ansi(result.stdout).strip()
        stderr = _strip_ansi(result.stderr).strip()

        if stdout and stderr:
            output = f"{stdout}\n{stderr}"
        else:
            output = stdout if stdout else stderr

        if result.returncode:
            output = _format_apt_lock_error(output)
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return 124, "Command timed out."
    except Exception as e:
        return 1, str(e)
    finally:
        if uses_apt:
            APT_COMMAND_LOCK.release()


def _run_command(cmd):
    returncode, output = _run_command_raw(cmd)

    if returncode == 0:
        return True, output if output else "Command completed successfully."

    return False, output if output else "Command failed."


def _run_command_stream(cmd, output_callback=None, timeout=900):
    """Run a command, streaming combined output line by line.

    stdin is closed and APT is forced non-interactive so maintainer scripts
    cannot block waiting for input.
    """
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["PAGER"] = "cat"
    uses_apt = _command_uses_apt(cmd)
    if uses_apt and not APT_COMMAND_LOCK.acquire(blocking=False):
        return False, (
            "Another APT/dpkg operation is already running in Appector. "
            "Wait for it to finish, then try again."
        )

    process = None
    timer = None
    timed_out = threading.Event()

    try:
        command = list(cmd)
        if (
            timeout
            and command
            and Path(command[0]).name == "pkexec"
        ):
            timeout_command = shutil.which("timeout")
            if timeout_command:
                command = [
                    command[0],
                    timeout_command,
                    "--signal=TERM",
                    "--kill-after=10s",
                    str(timeout),
                    *command[1:],
                ]

        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        if timeout:
            def terminate_process():
                if process.poll() is None:
                    timed_out.set()
                    try:
                        process.kill()
                    except (PermissionError, ProcessLookupError, OSError):
                        pass

            timer = threading.Timer(timeout, terminate_process)
            timer.start()

        lines = []

        if process.stdout:
            for line in process.stdout:
                clean_line = _strip_ansi(line).rstrip()

                if output_callback:
                    try:
                        output_callback(clean_line)
                    except Exception:
                        pass

                lines.append(clean_line)

        process.wait()

        returncode = process.returncode
        output = "\n".join(lines).strip()

        if timed_out.is_set() or returncode == 124:
            return False, output if output else "Command timed out."

        if returncode == 0:
            return True, output if output else "Command completed successfully."

        output = output or f"Command failed with exit code {returncode}."
        return False, _format_apt_lock_error(output)

    except Exception as e:
        return False, str(e)
    finally:
        if timer:
            timer.cancel()
            if timer.is_alive():
                timer.join(timeout=1)
        if uses_apt:
            APT_COMMAND_LOCK.release()