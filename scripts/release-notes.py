#!/usr/bin/env python3
"""Extract release notes and pre-release status for a tag from CHANGELOG.md.

Both the GitHub Release workflow and the APT repository workflow need to know
the same two things about a tag: what its notes say, and whether it is a
pre-release. That decision is not cosmetic here. /releases/latest excludes
pre-releases, so it decides whether Appector's in-app update check can see the
release at all, and the APT workflow must never promote a pre-release into the
stable channel. Both consumers therefore share this one implementation rather
than each parsing the changelog separately.

The pre-release status comes from the heading *suffix*, not from the version
number, because the version number alone cannot express it:

    ## [0.2.1] - 2026-10-10              full release
    ## [0.3.0] - 2026-11-01 - Pre-release  not advertised

Usage:
    scripts/release-notes.py TAG CHANGELOG [NOTES_OUT] [PRERELEASE_OUT]

Writes the rendered notes and a marker file containing "1" (pre-release) or
"0" (full release). Exits non-zero when the tag has no changelog section, so a
release cannot be published with empty or wrong notes.
"""

import argparse
import re
import sys
from pathlib import Path

# A heading whose suffix mentions a pre-release marker.
PRERELEASE_PATTERN = re.compile(r"prerelease|pre-release|rc\d*\b", re.IGNORECASE)


def parse_changelog(changelog_text, version):
    """Return (notes, is_prerelease) for a version, or None if absent."""
    heading = re.compile(
        rf"^## \[{re.escape(version)}\](?:\s+-\s*(?P<suffix>.*))?$"
    )
    lines = changelog_text.splitlines()

    for index, line in enumerate(lines):
        match = heading.match(line)
        if not match:
            continue

        notes = []
        for item in lines[index + 1:]:
            if item.startswith("## "):
                break
            notes.append(item)

        suffix = (match.group("suffix") or "").strip()
        return "\n".join(notes).strip(), bool(PRERELEASE_PATTERN.search(suffix))

    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, for example v0.2.1")
    parser.add_argument(
        "changelog",
        nargs="?",
        default="CHANGELOG.md",
        help="path to CHANGELOG.md (default: CHANGELOG.md)",
    )
    parser.add_argument(
        "notes_out", nargs="?", default=None, help="write rendered notes here"
    )
    parser.add_argument(
        "prerelease_out",
        nargs="?",
        default=None,
        help="write the pre-release marker here ('1' or '0')",
    )
    arguments = parser.parse_args(argv)

    version = arguments.tag.removeprefix("v")
    if not version:
        parser.error("a tag is required, for example v0.2.1")

    changelog_path = Path(arguments.changelog)
    if not changelog_path.is_file():
        print(
            f"error: changelog not found: {changelog_path}", file=sys.stderr
        )
        return 1

    result = parse_changelog(
        changelog_path.read_text(encoding="utf-8"), version
    )
    if result is None:
        print(
            f"error: no changelog section found for {arguments.tag}.",
            file=sys.stderr,
        )
        return 1

    notes, is_prerelease = result
    if not notes:
        print(
            f"error: the {arguments.tag} changelog section is empty.",
            file=sys.stderr,
        )
        return 1

    if arguments.notes_out:
        Path(arguments.notes_out).write_text(notes + "\n", encoding="utf-8")
    if arguments.prerelease_out:
        Path(arguments.prerelease_out).write_text(
            "1\n" if is_prerelease else "0\n", encoding="utf-8"
        )

    print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())