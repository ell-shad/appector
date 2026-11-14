"""Integration tests for the structured .deb review (F13/U10).

Builds real .deb archives with dpkg-deb (the tool the production code
invokes) and asserts the extracted review facts: maintainer scripts,
setuid binaries, /etc and /etc/apt files, and systemd units.
"""

import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from appector import actions

DPKG_DEB = shutil.which("dpkg-deb")


def build_deb(root: Path, name="demo", *, setuid=False, scripts=(),
              extra_files=(), conffiles=()):
    """Build a small real .deb with dpkg-deb and return its path."""
    package_root = root / "pkg"
    control_dir = package_root / "DEBIAN"
    control_dir.mkdir(parents=True, exist_ok=True)

    control_fields = [
        "Package: " + name,
        "Version: 1.0",
        "Architecture: all",
        "Maintainer: Test Publisher <test@example.org>",
        "Description: Test package for the structured review",
        "Depends: libc6",
    ]
    (control_dir / "control").write_text("\n".join(control_fields) + "\n",
                                         encoding="utf-8")

    if scripts:
        for script in scripts:
            path = control_dir / script
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
    if conffiles:
        (control_dir / "conffiles").write_text(
            "\n".join(conffiles) + "\n", encoding="utf-8"
        )

    binary = package_root / "usr/bin/demo-tool"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\necho demo\n", encoding="utf-8")
    binary.chmod(0o755)
    if setuid:
        binary.chmod(binary.stat().st_mode | stat.S_ISUID)

    for extra in extra_files:
        target = package_root / extra
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# demo content\n", encoding="utf-8")

    deb_path = root / f"{name}.deb"
    subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(package_root),
         str(deb_path)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return deb_path


@unittest.skipIf(DPKG_DEB is None, "dpkg-deb is not available")
class StructuredReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _review(self, **deb_kwargs):
        deb = build_deb(self.root, **deb_kwargs)
        review, error = actions.review_deb_batch([str(deb)])
        self.addCleanup(lambda: review and review.close())
        return review, error

    def test_plain_package_yields_clean_record(self):
        review, error = self._review()
        self.assertEqual(error, "")
        self.assertIsNotNone(review)
        record = review.package_records[0]
        self.assertEqual(record["name"], "demo")
        self.assertEqual(record["version"], "1.0")
        self.assertEqual(record["architecture"], "all")
        self.assertEqual(record["maintainer_scripts"], [])
        self.assertEqual(record["setuid_files"], [])
        self.assertEqual(record["warnings"], [])
        self.assertTrue(record["sha256"])
        self.assertGreater(record["size"], 0)

    def test_maintainer_scripts_are_reported(self):
        review, error = self._review(scripts=("postinst", "prerm"))
        self.assertEqual(error, "")
        record = review.package_records[0]
        self.assertEqual(record["maintainer_scripts"], ["postinst", "prerm"])
        self.assertTrue(any(
            "maintainer scripts" in warning
            for warning in record["warnings"]
        ))

    def test_setuid_binary_is_reported(self):
        review, error = self._review(setuid=True)
        self.assertEqual(error, "")
        record = review.package_records[0]
        self.assertEqual(record["setuid_files"], ["usr/bin/demo-tool"])
        self.assertTrue(any(
            "setuid" in warning for warning in record["warnings"]
        ))

    def test_apt_and_systemd_files_are_reported(self):
        review, error = self._review(
            extra_files=(
                "etc/apt/apt.conf.d/99-demo",
                "etc/demo.conf",
                "lib/systemd/system/demo.service",
            )
        )
        self.assertEqual(error, "")
        record = review.package_records[0]
        self.assertIn("etc/apt/apt.conf.d/99-demo", record["apt_config_files"])
        self.assertIn("etc/demo.conf", record["etc_files"])
        self.assertIn("lib/systemd/system/demo.service", record["systemd_units"])
        self.assertTrue(any(
            "/etc/apt" in warning for warning in record["warnings"]
        ))

    def test_all_warnings_flattens_across_packages(self):
        deb_a = build_deb(self.root, name="pkga", scripts=("postinst",))
        deb_b = build_deb(self.root, name="pkgb", setuid=True)
        review, error = actions.review_deb_batch([str(deb_a), str(deb_b)])
        self.addCleanup(lambda: review and review.close())
        self.assertEqual(error, "")
        self.assertEqual(len(review.package_records), 2)
        flattened = review.all_warnings
        self.assertTrue(any("pkga" in w and "maintainer scripts" in w
                            for w in flattened))
        self.assertTrue(any("pkgb" in w and "setuid" in w for w in flattened))

    def test_security_findings_pure_function(self):
        findings = actions._security_findings([
            ("-rwsr-xr-x", "usr/bin/su"),
            ("-rw-r--r--", "etc/apt/sources.list.d/evil.list"),
            ("-rw-r--r--", "usr/share/doc/x/copyright"),
            ("-rw-r--r--", "lib/systemd/system/x.service"),
        ])
        self.assertEqual(findings["setuid_files"], ["usr/bin/su"])
        self.assertEqual(findings["apt_config_files"],
                         ["etc/apt/sources.list.d/evil.list"])
        self.assertEqual(findings["systemd_units"], ["lib/systemd/system/x.service"])
        self.assertEqual(len(findings["warnings"]), 3)


if __name__ == "__main__":
    unittest.main()
