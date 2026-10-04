import json
import re
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


RELEASE_REPOSITORY = "ell-shad/appector"
LATEST_RELEASE_URL = (
    f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases/latest"
)


def _debian_version(version):
    version = version.strip()
    if version.startswith("v"):
        version = version[1:]
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*", version):
        raise ValueError("GitHub release version is not a valid Debian package version.")
    try:
        result = subprocess.run(
            ["dpkg", "--validate-version", version],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Could not validate the release version: {error}") from error
    if result.returncode != 0:
        raise ValueError("GitHub release version is not a valid Debian package version.")
    return version


def _is_newer(latest, current):
    latest_version = _debian_version(latest)
    current_version = _debian_version(current)
    try:
        result = subprocess.run(
            ["dpkg", "--compare-versions", latest_version, "gt", current_version],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Could not compare Debian package versions: {error}") from error
    if result.returncode not in (0, 1):
        raise RuntimeError("dpkg could not compare the application versions.")
    return result.returncode == 0


def check_for_update(current_version):
    request = Request(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Appector-update-checker",
        },
    )

    try:
        with urlopen(request, timeout=10) as response:
            release = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            raise RuntimeError(
                "GitHub has no published stable release yet, or the public "
                "release repository is unavailable."
            ) from error
        raise RuntimeError(
            f"GitHub returned HTTP {error.code} while checking for updates."
        ) from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"Could not reach GitHub to check for updates: {error}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("GitHub returned an invalid update response.") from error

    if not isinstance(release, dict):
        raise RuntimeError("GitHub returned an invalid release response.")

    tag_name = release.get("tag_name")
    html_url = release.get("html_url")
    if not isinstance(tag_name, str) or not isinstance(html_url, str):
        raise RuntimeError("The latest GitHub release is missing its tag or URL.")

    parsed_url = urlsplit(html_url)
    expected_path = f"/{RELEASE_REPOSITORY}/releases/tag/"
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "github.com"
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.path != expected_path + tag_name
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise RuntimeError("GitHub returned an unsafe or unexpected release URL.")

    return {
        "version": tag_name.removeprefix("v"),
        "url": html_url,
        "available": _is_newer(tag_name, current_version),
    }
