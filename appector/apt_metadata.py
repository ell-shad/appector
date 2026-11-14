"""Batched dpkg metadata lookups for APT risk decisions (S11).

One `dpkg-query -W` call returns Essential/Priority/Section for every
requested package, so `policy.get_removal_risk` can decide from real dpkg
metadata instead of substring heuristics, without per-package subprocesses
(F6).
"""

import subprocess

from .policy import _is_safe_package_id

_METADATA_TIMEOUT = 30


def get_apt_metadata_map(package_ids: list[str]) -> dict[str, dict]:
    """Return ``{name:arch: {"essential": ..., "priority": ..., "section": ...}}``.

    Packages that cannot be queried are simply absent; callers treat a
    missing entry as "no metadata" and fall back to the policy lists.
    """
    valid = [pid for pid in (package_ids or []) if _is_safe_package_id(pid)]
    if not valid:
        return {}

    command = [
        "dpkg-query",
        "-W",
        "-f=${binary:Package}\t${Essential}\t${Priority}\t${Section}\n",
        "--",
        *valid,
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_METADATA_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}

    metadata = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not parts[0]:
            continue
        name, essential, priority, section = parts
        metadata[name] = {
            "essential": essential,
            "priority": priority,
            "section": section,
        }
    return metadata


def reverse_dependency_count(_package_id: str) -> int:
    """Placeholder for the reverse-dependency signal from the simulation.

    The removal flows read the dependency cascade directly from the APT
    simulation (the number of extra ``Remv`` lines), which is both cheaper
    and more precise than a separate rdepends query, so this helper exists
    only to document that mapping. It returns -1 meaning "unknown".
    """
    return -1
