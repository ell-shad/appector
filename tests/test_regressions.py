"""Regression tests for bugs verified during the code review.

Each test here pins one finding from the security/data-safety review
(review reference in each docstring). They were written before the fixes
and initially failed; any change that re-introduces the bug must turn one
of these red.
"""

import csv
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from appector import _proc, policy, scanners


class S7bNonUtf8OutputTests(unittest.TestCase):
    """S7b: exit-0 commands with non-UTF-8 output were reported as failures."""

    def test_exit_zero_with_non_utf8_output_succeeds(self):
        returncode, output = _proc._run_command_raw(
            ["sh", "-c", "printf 'ok \\\\377\\\\376'; exit 0"],
            timeout=30,
        )
        self.assertEqual(returncode, 0)
        self.assertIn("ok", output)
        self.assertNotIn("codec", output)


class S7LogHardeningTests(unittest.TestCase):
    """S7: _log_action wrote caller text verbatim (forged log lines)."""

    def test_log_action_escapes_newlines(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.object(Path, "home", return_value=home):
                _proc._log_action("FAKE event\nFORGED extra line")
                from appector.paths import state_dir

                log_file = state_dir() / "actions.log"
                content = log_file.read_text(encoding="utf-8")
        self.assertIn("FAKE event\\\\nFORGED extra line", content)
        # Exactly one physical log line must be written.
        lines = [line for line in content.splitlines() if "FAKE" in line or "FORGED" in line]
        self.assertEqual(len(lines), 1)


class S7cLocaleTests(unittest.TestCase):
    """S7: the streaming runner must force LC_ALL=C (callers match on text)."""

    def test_run_command_stream_forces_c_locale(self):
        success, output = _proc._run_command_stream(
            ["sh", "-c", 'printf "%s" "$LC_ALL"'],
            timeout=30,
        )
        self.assertTrue(success, output)
        self.assertEqual(output.strip(), "C")

    def test_run_command_can_cancel(self):
        cancel = threading.Event()

        def stream_line(_line):
            cancel.set()

        result = _proc.run(
            ["sh", "-c", "echo line1; sleep 30; echo line2"],
            timeout=60,
            stream_cb=stream_line,
            cancel=cancel,
        )
        self.assertTrue(result.cancelled)
        self.assertIn("cancelled", result.output.lower())

    def test_run_command_timeout_kills_process_group(self):
        start = time.monotonic()
        result = _proc.run(
            ["sh", "-c", "sleep 600 & sleep 600"],
            timeout=1,
            keep_bytes=4096,
        )
        elapsed = time.monotonic() - start
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 124)
        self.assertLess(elapsed, 30, "timeout must not wait for the full sleep")


class S7LockTests(unittest.TestCase):
    """S7: flatpak/snap commands must not be mistaken for APT commands."""

    def test_flatpak_is_not_serialised_as_apt(self):
        self.assertFalse(_proc._command_uses_apt(["flatpak", "uninstall", "app"]))
        self.assertFalse(_proc._command_uses_apt(["snap", "remove", "firefox"]))
        self.assertTrue(_proc._command_uses_apt(["apt-get", "remove", "vim"]))


class F2DuplicateDetectionTests(unittest.TestCase):
    """F2: unrelated Flatpaks/Snaps were flagged as duplicates."""

    def test_flatpaks_sharing_exec_name_are_not_duplicates(self):
        from appector.models import AppEntry, mark_duplicate_apps

        a = AppEntry(
            "Client", "Flatpak", "flathub", "com.spotify.Client", exec_name="client"
        )
        b = AppEntry(
            "Client", "Flatpak", "flathub", "org.example.Other.Client",
            exec_name="client",
        )
        mark_duplicate_apps([a, b])
        self.assertFalse(a.is_duplicate)
        self.assertFalse(b.is_duplicate)

    def test_real_duplicates_still_detected(self):
        from appector.models import AppEntry, mark_duplicate_apps

        a = AppEntry("Firefox", "APT", "Ubuntu", "firefox", exec_name="firefox")
        b = AppEntry("Firefox", "Flatpak", "flathub", "org.mozilla.firefox",
                     exec_name="firefox")
        mark_duplicate_apps([a, b])
        self.assertTrue(a.is_duplicate)
        self.assertTrue(b.is_duplicate)


class F3NormalizeExecTests(unittest.TestCase):
    """F3: normalize_exec broke on quoted paths and wrapped commands."""

    def test_normalize_exec_unwraps_and_quotes(self):
        cases = {
            "/usr/bin/flatpak run --branch=stable com.spotify.Client":
                "com.spotify.client",
            '"/opt/My App/app" %U': "app",
            "gamemoderun /usr/bin/game %U": "game",
            "env FIREFOX_SNAP=1 firefox %u": "firefox",
            "sh -c 'exec foo'": "foo",
            "/usr/bin/env python3 -m http.server": "python3",
            "snap run firefox": "firefox",
            "pkexec /usr/sbin/dangerous-tool": "dangerous-tool",
            "flatpak run org.gimp.GIMP": "org.gimp.gimp",
            "": "",
        }
        for exec_line, expected in cases.items():
            with self.subTest(exec_line=exec_line):
                self.assertEqual(scanners.normalize_exec(exec_line), expected)


class F4AptStatusTests(unittest.TestCase):
    """F4: "installed" substring matched not-installed and half-installed."""

    def test_scan_apt_uses_status_abbrev_ii(self):
        import tempfile

        dpkg_output = (
            "vim:amd64\tii\t8.2\tinstall ok installed\n"
            "broken:amd64\tiU\t1.0\tinstall await broken\n"
            "gone:amd64\tun\t2.0\tunknown ok not-installed\n"
            "rcpkg:amd64\trc\t3.0\tdeinstall ok config-files\n"
            "arched:amd64\tii\t4.0\tinstall ok installed\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            desktop_dir = Path(temporary) / "applications"
            desktop_dir.mkdir()
            desktop_file = desktop_dir / "vim.desktop"
            desktop_file.write_text(
                "[Desktop Entry]\nType=Application\nName=Vim\nExec=vim %F\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "appector.scanners.subprocess.check_output",
                    return_value=dpkg_output,
                ),
                patch.object(
                    scanners,
                    "get_package_owners",
                    return_value={str(desktop_file): "vim"},
                ),
                patch.object(
                    scanners,
                    "_desktop_directories",
                    lambda: [desktop_dir],
                ),
            ):
                apps = scanners.scan_apt()
        package_ids = {app.package_id for app in apps}
        # Only truly installed (ii) packages count, keyed by name:arch.
        self.assertEqual(package_ids, {"vim:amd64"})


class F5HistoryLogParsingTests(unittest.TestCase):
    """F5: history.log "(1.0, automatic)" produced a bogus package token."""

    def test_history_log_parses_name_arch_and_automatic_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            log_dir = Path(temporary) / "apt"
            log_dir.mkdir()
            (log_dir / "history.log").write_text(
                "Start-Date: 2024-05-01  10:11:12\n"
                "Install: libfoo:amd64 (1.0, automatic), libbar:amd64 (2.0)\n"
                "End-Date: 2024-05-01  10:12:00\n",
                encoding="utf-8",
            )
            original_path = scanners.Path

            def fake_path(value="."):
                if str(value) in ("/var/log/apt",):
                    return log_dir
                if str(value) in ("/var/log", "/var/lib/dpkg/info"):
                    return Path(temporary)
                return original_path(value)

            with patch("appector.scanners.Path", side_effect=fake_path):
                dates = scanners.get_apt_install_dates()
        self.assertEqual(dates.get("libfoo:amd64"), "2024-05-01")
        self.assertEqual(dates.get("libbar:amd64"), "2024-05-01")
        self.assertNotIn("automatic)", dates)
        self.assertNotIn("automatic", dates)


class F7DesktopActionGroupTests(unittest.TestCase):
    """F7: NoDisplay inside [Desktop Action ...] hid the whole app."""

    def test_desktop_action_group_is_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "app.desktop"
            desktop.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Demo\n"
                "Exec=demo\n"
                "\n"
                "[Desktop Action new]\n"
                "Name=New Window\n"
                "NoDisplay=true\n",
                encoding="utf-8",
            )
            name, _categories, no_display, hidden, icon, exec_line = (
                scanners.parse_desktop_file(str(desktop))
            )
        self.assertEqual(name, "Demo")
        self.assertFalse(no_display)
        self.assertFalse(hidden)
        self.assertEqual(exec_line, "demo")


class S10CsvInjectionTests(unittest.TestCase):
    """S10: exported names could execute as formulas in spreadsheets."""

    def test_csv_export_escapes_formula_cells(self):
        from appector.export import csv_safe_value

        for dangerous in (
            "=HYPERLINK(\"http://evil\")",
            "+1+1",
            "-2+3",
            "@SUM(A1:A2)",
            "\ttab-led",
            "\rcr-led",
        ):
            with self.subTest(value=dangerous):
                safe = csv_safe_value(dangerous)
                self.assertFalse(re.match(r"^[=+\-@\t\r]", safe), safe)

    def test_csv_writer_escapes_names(self):
        from appector.export import write_app_export

        app = SimpleNamespace(
            name="=HYPERLINK(\"http://evil\", \"click\")",
            manager="APT",
            source="Ubuntu repository",
            package_id="evil-pkg",
            version="1.0",
            installed_at="2024-05-01",
            category="Utilities",
            details="",
            exec_name="evil-pkg",
            is_duplicate=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "export.csv"
            write_app_export(target, [app], "csv")
            with open(target, newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
        header = rows[0]
        name_index = header.index("name")
        self.assertTrue(rows[1][name_index].startswith("'="))
        self.assertIn("HYPERLINK", rows[1][name_index])


class S11PolicyQualityTests(unittest.TestCase):
    """S11: substring rules over-blocked snap/APT names."""

    def test_corectrl_is_not_a_blocked_snap(self):
        self.assertFalse(policy.is_blocked(SimpleNamespace(manager="Snap", package_id="corectrl")))
        self.assertFalse(policy.is_blocked(SimpleNamespace(manager="Snap", package_id="core-keeper")))

    def test_core_snaps_remain_blocked(self):
        for name in ("core", "core18", "core20", "core22", "core24"):
            self.assertTrue(
                policy.is_blocked(SimpleNamespace(manager="Snap", package_id=name)),
                name,
            )

    def test_gnome_calculator_is_not_high_risk(self):
        for name in ("gnome-calculator", "gnome-sudoku", "kdenlive", "powershell",
                     "desktop-file-utils", "climate-data"):
            risk, _reason = policy.get_removal_risk(
                SimpleNamespace(manager="APT", package_id=name)
            )
            self.assertNotEqual(risk, "high", name)
            self.assertNotEqual(risk, "blocked", name)

    def test_critical_packages_still_blocked(self):
        for name in ("gnome-shell", "systemd", "sudo", "bash", "apt"):
            risk, _reason = policy.get_removal_risk(
                SimpleNamespace(manager="APT", package_id=name)
            )
            self.assertEqual(risk, "blocked", name)


class S2MultiarchResidualTests(unittest.TestCase):
    """S2: residual sets must be keyed on name:arch, not bare names."""

    def test_residual_set_is_arch_qualified(self):
        import appector.residual as residual

        dpkg_output = (
            "rc\tlibfoo:i386\n"
            "ii\tlibfoo:amd64\n"
            "rc\tplain\n"
        )
        fake_run = SimpleNamespace(returncode=0, stdout=dpkg_output, stderr="")
        with patch("appector.residual.subprocess.run", return_value=fake_run) as run:
            residual_set = residual._dpkg_residual_package_set()
        # The query itself must ask for the architecture-qualified name;
        # ${Package} omits it and collapses all architectures into one entry.
        query_format = run.call_args.args[0][2]
        self.assertIn("${binary:Package}", query_format)
        self.assertNotIn("${Package}\t", query_format)
        self.assertEqual(residual_set, {"libfoo:i386", "plain"})

    def test_purge_blocked_when_other_arch_is_installed(self):
        """rc libfoo:i386 with installed libfoo:amd64 must refuse to purge."""
        import appector.residual as residual

        dpkg_status = (
            "rc\tlibfoo:i386\n"
            "ii\tlibfoo:amd64\n"
        )
        fake_run = SimpleNamespace(returncode=0, stdout=dpkg_status, stderr="")
        with patch("appector.residual.subprocess.run", return_value=fake_run):
            success, message = residual.purge_leftover_configs(["libfoo"])
        self.assertFalse(success)
        self.assertIn("libfoo:amd64", message)


class SafePackageIdPropertyTests(unittest.TestCase):
    """Section 9 item 3: property tests for SAFE_PACKAGE_ID_RE."""

    def test_option_and_shell_metacharacters_are_rejected(self):
        from appector.policy import _is_safe_package_id

        for value in (
            "", " ", "-rf", "--flag", "a;b", "a b", "a|b", "a&b", "a$(x)",
            "a`x`", "a\nb", "a\tb", "../etc/passwd", "a/b", "\\", "'", '"',
            "a;b", "(){:;}:", "%s", "a#b",
        ):
            with self.subTest(value=value):
                self.assertFalse(_is_safe_package_id(value))

    def test_valid_names_are_accepted(self):
        from appector.policy import _is_safe_package_id

        for value in (
            "vim", "libfoo:amd64", "org.gimp.GIMP", "a+b:c.d_e-f",
            "2to3", "g++-13", "linux-image-6.8.0-49-generic",
        ):
            with self.subTest(value=value):
                self.assertTrue(_is_safe_package_id(value))


class F9AppImageInstallTests(unittest.TestCase):
    """F9: AppImage validation and install hardening."""

    def _source(self, root, marker=b"\x02", name="Demo.AppImage"):
        path = root / name
        # Minimal fake: ELF magic (4) + reserved + type marker at byte 8.
        header = b"\x7fELF" + b"\x00" * 4 + marker + b"\x00" * 500
        path.write_bytes(header + b"payload")
        return path

    def test_type2_appimage_is_accepted(self):
        from appector.actions import _validate_appimage_header

        ok, reason = _validate_appimage_header(
            b"\x7fELF" + b"\x00" * 4 + b"\x02" + b"\x00" * 3
        )
        self.assertTrue(ok, reason)

    def test_type1_appimage_is_accepted(self):
        from appector.actions import _validate_appimage_header

        ok, _reason = _validate_appimage_header(
            b"\x7fELF" + b"\x00" * 4 + b"\x01" + b"\x00" * 3
        )
        self.assertTrue(ok)

    def test_plain_elf_is_rejected(self):
        """F9: a renamed ELF executable is not an AppImage."""
        from appector.actions import _validate_appimage_header

        ok, reason = _validate_appimage_header(
            b"\x7fELF" + b"\x00" * 4 + b"\x00" + b"\x00" * 3
        )
        self.assertFalse(ok)
        self.assertIn("not a type 1 or type 2 AppImage", reason)

    def test_non_elf_is_rejected(self):
        from appector.actions import _validate_appimage_header

        ok, _reason = _validate_appimage_header(b"MZ" + b"\x00" * 100)
        self.assertFalse(ok)

    def test_install_rejects_renamed_elf(self):
        import tempfile

        from appector import actions

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "NotAnApp.AppImage"
            fake.write_bytes(b"\x7fELF" + b"\x00" * 200)
            with patch.object(Path, "home", return_value=root):
                success, message = actions.install_appimage(str(fake))
        self.assertFalse(success)
        self.assertIn("not a type 1 or type 2 AppImage", message)

    def test_install_uses_content_hash_identity(self):
        """F9: two different AppImages with the same name are distinct."""
        import tempfile

        from appector import actions

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._source(root, name="Demo.AppImage")
            with patch.object(Path, "home", return_value=root):
                success, message = actions.install_appimage(str(first))
                self.assertTrue(success, message)

                # Same file name, different content.
                second = self._source(root, name="Demo.AppImage")
                second.write_bytes(second.read_bytes() + b"-v2")
                success, message = actions.install_appimage(str(second))
                self.assertTrue(success, message)

                from appector.paths import appimage_dir

                copies = list(appimage_dir().glob("Demo-*.AppImage"))
        self.assertEqual(len(copies), 2, "different content must not collide")

    def test_no_partial_files_left_behind(self):
        import tempfile

        from appector import actions

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            with (
                patch.object(Path, "home", return_value=root),
                patch(
                    "appector.actions.os.fsync",
                    side_effect=OSError("disk full mid-copy"),
                ),
            ):
                success, _message = actions.install_appimage(str(source))
            self.assertFalse(success)
            from appector.paths import appimage_dir

            parts = list(appimage_dir().glob(".*.part"))
        self.assertEqual(parts, [], "no .part file may survive a failure")


class ScannerOwnershipParserTests(unittest.TestCase):
    """Regression: dpkg-query -S parsing was inverted (package as path key).

    The old parser split on the FIRST colon, so `dpkg: /usr/bin/dpkg`
    produced key "dpkg" — every ownership lookup missed and APT desktop
    files were treated as unowned (Manual) entries.
    """

    def test_live_dpkg_ownership_maps_path_to_package(self):
        if not Path("/usr/bin/dpkg").exists():
            self.skipTest("no dpkg on this system")
        owners = scanners.get_package_owners(["/usr/bin/dpkg"])
        self.assertEqual(owners.get("/usr/bin/dpkg"), "dpkg")

    def test_parser_output_shapes(self):
        with patch(
            "appector.scanners.subprocess.run"
        ) as run:
            run.return_value = SimpleNamespace(
                returncode=0,
                stdout=(
                    "dpkg: /usr/bin/dpkg\n"
                    "vim-tiny:vim, vim-tiny: /usr/bin/vim.tiny\n"
                    "diversion by vim-tiny from: /usr/bin/vim\n"
                    "local: /usr/local/bin/tool\n"
                ),
                stderr="",
            )
            owners = scanners.get_package_owners(["/usr/bin/dpkg"])
        self.assertEqual(owners.get("/usr/bin/dpkg"), "dpkg")
        # Multi-owner lines map to the first package.
        self.assertEqual(owners.get("/usr/bin/vim.tiny"), "vim-tiny:vim")
        # Diversions and local-modification markers are skipped.
        self.assertNotIn("/usr/bin/vim", owners)
        self.assertNotIn("/usr/local/bin/tool", owners)

    def test_ownership_call_never_mixes_stderr_with_capture(self):
        """The old call raised ValueError on every invocation (stderr +
        capture_output); the scanner then returned no owners at all."""
        import inspect

        source = inspect.getsource(scanners.get_package_owners)
        self.assertNotIn(
            "stderr=subprocess.DEVNULL",
            source,
            "stderr=DEVNULL must not be combined with capture_output=True",
        )


class F10ManualScanDirsTests(unittest.TestCase):
    """F10: user-configurable manual scan directories."""

    def test_extra_dirs_extend_the_scan(self):
        import tempfile

        from unittest.mock import patch as _patch

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            extra = home / "mytools"
            extra.mkdir()
            tool = extra / "mytool"
            tool.write_text("#!/bin/sh\n", encoding="utf-8")
            tool.chmod(0o755)
            with _patch.object(Path, "home", return_value=home):
                apps = scanners.scan_manual_apps(
                    desktop_dirs=[home / "nonexistent-applications"],
                    binary_dirs=[],
                    extra_dirs=[str(extra)],
                )
        self.assertTrue(
            any("mytool" in app.package_id for app in apps),
            [app.package_id for app in apps],
        )

    def test_extra_dirs_do_not_duplicate_defaults(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.object(Path, "home", return_value=home):
                base = scanners._default_binary_dirs(home)
                apps = scanners.scan_manual_apps(
                    desktop_dirs=[home / "nonexistent-applications"],
                    binary_dirs=list(base),
                    extra_dirs=[str(home / "bin")],
                )
        # home/bin appears exactly once in the effective scan set: the
        # call must not crash and must not double-scan.
        self.assertIsInstance(apps, list)


class S12FlatpakHttpsOnlyTests(unittest.TestCase):
    """S3: plain-HTTP .flatpakref URLs must be refused."""

    def test_http_flatpakref_url_is_rejected(self):
        from appector.actions import install_flatpak_source

        success, message = install_flatpak_source(
            "ref", "http://evil.example/x.flatpakref"
        )
        self.assertFalse(success)
        self.assertIn("https", message.lower())


if __name__ == "__main__":
    unittest.main()
