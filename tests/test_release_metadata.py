"""Tests for scripts/release-notes.py.

The pre-release flag is load-bearing in two places: GitHub's
/releases/latest endpoint excludes pre-releases, so it decides whether
Appector's in-app update check can see a release at all, and the APT
repository workflow refuses to publish a pre-release into the stable channel.
Both consumers share this script precisely so they cannot disagree.
"""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release-notes.py"

_spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)


CHANGELOG = """\
# Changelog

Introductory text that must not leak into release notes.

## [Unreleased]

- Not released yet.

## [1.2.0] - 2026-10-10

### Added

- A real feature.

### Fixed

- A real fix.

## [1.1.0] - 2026-09-01 - Prerelease

### Added

- Unfinished work.

## [1.0.0]
"""


class ParseChangelogTests(unittest.TestCase):
    def parse(self, version, changelog=CHANGELOG):
        return release_notes.parse_changelog(changelog, version)

    def test_finds_a_full_release(self):
        notes, is_prerelease = self.parse("1.2.0")
        self.assertFalse(is_prerelease)
        self.assertIn("A real feature.", notes)
        self.assertIn("A real fix.", notes)

    def test_notes_stop_at_the_next_heading(self):
        notes, _ = self.parse("1.2.0")
        self.assertNotIn("Unfinished work", notes)
        self.assertNotIn("Not released yet", notes)
        self.assertNotIn("Introductory text", notes)

    def test_notes_are_trimmed(self):
        notes, _ = self.parse("1.0.0")
        self.assertEqual(notes, notes.strip())

    def test_pre_release_suffix_is_detected(self):
        _, is_prerelease = self.parse("1.1.0")
        self.assertTrue(is_prerelease)

    def test_heading_without_a_date_is_still_found(self):
        self.assertIsNotNone(self.parse("1.0.0"))

    def test_missing_version_returns_none(self):
        self.assertIsNone(self.parse("9.9.9"))

    def test_version_is_not_matched_as_a_regex(self):
        # A version containing regex metacharacters must not match another
        # heading; "1.2.0" must not match "1x2x0".
        self.assertIsNone(self.parse("1.2.0", "## [1x2x0] - 2026-01-01\n\n- x\n"))

    def test_pre_release_markers_are_recognised(self):
        for suffix in ("Prerelease", "pre-release", "Pre-Release", "rc1", "RC2"):
            with self.subTest(suffix=suffix):
                changelog = f"## [2.0.0] - 2026-01-01 - {suffix}\n\n- x\n"
                _, is_prerelease = self.parse("2.0.0", changelog)
                self.assertTrue(is_prerelease, suffix)

    def test_ordinary_suffix_is_not_a_pre_release(self):
        changelog = "## [2.0.0] - 2026-01-01 - Initial release\n\n- x\n"
        _, is_prerelease = self.parse("2.0.0", changelog)
        self.assertFalse(is_prerelease)


class ReleaseNotesCommandTests(unittest.TestCase):
    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory()
        self.root = Path(self._scratch.name)
        self.addCleanup(self._scratch.cleanup)
        self.changelog = self.root / "CHANGELOG.md"
        self.changelog.write_text(CHANGELOG, encoding="utf-8")

    def _run(self, *args):
        return subprocess.run(
            ["python3", str(SCRIPT), *args],
            capture_output=True,
            text=True,
        )

    def test_writes_notes_and_a_full_release_marker(self):
        notes_out = self.root / "notes.md"
        prerelease_out = self.root / "prerelease"
        result = self._run(
            "v1.2.0", str(self.changelog), str(notes_out), str(prerelease_out)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("A real feature.", notes_out.read_text())
        self.assertEqual(prerelease_out.read_text(), "0\n")

    def test_writes_a_pre_release_marker(self):
        notes_out = self.root / "notes.md"
        prerelease_out = self.root / "prerelease"
        result = self._run(
            "v1.1.0", str(self.changelog), str(notes_out), str(prerelease_out)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(prerelease_out.read_text(), "1\n")
        self.assertIn("Unfinished work.", notes_out.read_text())

    def test_tag_without_a_v_prefix_still_works(self):
        result = self._run("1.2.0", str(self.changelog))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("A real feature.", result.stdout)

    def test_unknown_tag_fails_rather_than_publishing_empty_notes(self):
        result = self._run("v9.9.9", str(self.changelog))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no changelog section", result.stderr)

    def test_empty_section_fails(self):
        self.changelog.write_text("## [3.0.0]\n", encoding="utf-8")
        result = self._run("v3.0.0", str(self.changelog))
        self.assertNotEqual(result.returncode, 0)

    def test_missing_changelog_fails(self):
        result = self._run("v1.2.0", str(self.root / "absent.md"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changelog not found", result.stderr)


class ProjectChangelogTests(unittest.TestCase):
    """The real changelog must stay parseable for the current version."""

    def test_current_version_has_a_section(self):
        import re

        source = (REPO_ROOT / "appector" / "__init__.py").read_text(
            encoding="utf-8"
        )
        version = re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE)
        self.assertIsNotNone(version)
        version = version.group(1)

        result = release_notes.parse_changelog(
            (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version
        )
        self.assertIsNotNone(result, f"CHANGELOG.md has no section for {version}")
        notes, is_prerelease = result
        self.assertTrue(notes, f"the {version} section is empty")
        # A version already tagged on the remote must not be marked pre-release,
        # or /releases/latest will not show it.
        tags = subprocess.run(
            ["git", "tag", "-l", f"v{version}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if tags:
            self.assertFalse(
                is_prerelease,
                f"v{version} already exists but is marked as a pre-release",
            )


if __name__ == "__main__":
    unittest.main()