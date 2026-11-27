"""Regression tests for the 2026-11 repository review fixes.

Each test pins one fix from the review so the bug cannot quietly return:

- the integrated AppImage removal validated desktop launchers against the
  legacy AppImage directory, so every integrated removal failed with
  "missing or ambiguous" (and its unowned-entry guard was dead code);
- residual purges were blocked by the critical-package gate although the
  whole flow exists to clean old kernel/GRUB configuration leftovers;
- mixed system Flatpak selections collapsed onto a single installation;
- ``_read_log_lines`` read whole files into memory and ``dpkg-query -S``
  received unescaped glob patterns;
- the .deb review scratch directory ignored XDG_CACHE_HOME.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from appector import actions, plans, scanners
from appector.plans import Plan


class IntegratedAppImageRemovalTests(unittest.TestCase):
    """execute_removal must find launchers in ~/.local/share/applications."""

    def _install_fake_appimage(self, home: Path):
        image_root = home / ".local" / "share" / "appector" / "appimages"
        desktop_root = home / ".local" / "share" / "applications"
        image_root.mkdir(parents=True)
        desktop_root.mkdir(parents=True)
        image = image_root / "myapp-deadbeefca.AppImage"
        image.write_bytes(b"\x7fELF-fake-appimage")
        desktop = desktop_root / "app-manager-appimage-myapp-deadbeefca.desktop"
        desktop.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Myapp\n"
            f"Exec={image}\n"
            "X-AppManager-Installed-AppImage=true\n"
            f"X-AppManager-AppImage-Path={image}\n",
            encoding="utf-8",
        )
        return image, desktop

    def test_integrated_appimage_removal_removes_launcher_and_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            image, desktop = self._install_fake_appimage(home)
            app = SimpleNamespace(
                name="Myapp",
                manager="AppImage",
                package_id=str(image),
                removal_paths=[str(desktop), str(image)],
            )
            with (
                patch("pathlib.Path.home", return_value=home),
                patch(
                    "appector.actions.appimage_dir",
                    return_value=image.parent,
                ),
                patch(
                    "appector.actions.legacy_appimage_dir",
                    return_value=home / ".local/share/app-manager/appimages",
                ),
            ):
                success, message = actions.execute_removal(app)

            self.assertTrue(success, message)
            self.assertFalse(desktop.exists(), "launcher was not removed")
            self.assertFalse(image.exists(), "managed image was not removed")

    def test_unowned_desktop_entry_in_applications_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            image, desktop = self._install_fake_appimage(home)
            # Simulate a removal record that also contains a desktop file
            # Appector does not own (the guard that used to be dead code).
            foreign = desktop.parent / "some-other-app.desktop"
            foreign.write_text("[Desktop Entry]\nType=Application\n", encoding="utf-8")
            app = SimpleNamespace(
                name="Myapp",
                manager="AppImage",
                package_id=str(image),
                removal_paths=[str(foreign), str(desktop), str(image)],
            )
            with (
                patch("pathlib.Path.home", return_value=home),
                patch(
                    "appector.actions.appimage_dir",
                    return_value=image.parent,
                ),
                patch(
                    "appector.actions.legacy_appimage_dir",
                    return_value=home / ".local/share/app-manager/appimages",
                ),
            ):
                success, message = actions.execute_removal(app)

            self.assertFalse(success)
            self.assertIn("unowned desktop entry", message)
            self.assertTrue(foreign.exists(), "foreign launcher must survive")
            self.assertTrue(image.exists(), "image must survive a refusal")


class ResidualGateTests(unittest.TestCase):
    """check_apt_removal_plan(residual=True) exempts only the name rule."""

    CRITICAL = "linux-image-6.8.0-40-generic:amd64"

    def _plan(self):
        return Plan(
            action="purge",
            package_ids=[self.CRITICAL],
            removed_preview=[self.CRITICAL],
        )

    def test_residual_mode_allows_verified_rc_state_identities(self):
        ok, message = plans.check_apt_removal_plan(
            self._plan(), [self.CRITICAL], [self.CRITICAL], residual=True
        )
        self.assertTrue(ok, message)

    def test_normal_removal_still_blocks_critical_names(self):
        ok, _message = plans.check_apt_removal_plan(
            self._plan(), [self.CRITICAL], [self.CRITICAL], residual=False
        )
        self.assertFalse(ok)

    def test_residual_mode_still_refuses_growth_and_missing(self):
        ok, _message = plans.check_apt_removal_plan(
            self._plan(),
            [self.CRITICAL],
            [self.CRITICAL, "unrelated-package:amd64"],
            residual=True,
        )
        self.assertFalse(ok, "growth beyond the preview must stay blocked")

        ok, _message = plans.check_apt_removal_plan(
            self._plan(), [self.CRITICAL], [], residual=True
        )
        self.assertFalse(ok, "missing identities must stay blocked")

    def test_residual_mode_still_refuses_a_missing_plan(self):
        ok, _message = plans.check_apt_removal_plan(
            None, [self.CRITICAL], [self.CRITICAL], residual=True
        )
        self.assertFalse(ok)


class FlatpakBatchGroupingTests(unittest.TestCase):
    """Mixed system installations need one helper call each."""

    def test_mixed_installations_remove_one_helper_call_each(self):
        calls = []

        def fake_remove_flatpak_system(
            package_ids, installation=None, output_callback=None
        ):
            calls.append((tuple(package_ids), installation))
            return SimpleNamespace(
                ok=True, message="Removed.", events=[], returncode=0
            )

        apps = [
            SimpleNamespace(
                name="App One",
                manager="Flatpak",
                package_id="org.one.App",
                installation="system",
                details="",
            ),
            SimpleNamespace(
                name="App Two",
                manager="Flatpak",
                package_id="org.two.App",
                installation="kde",
                details="",
            ),
        ]
        with (
            patch("appector.actions.helper_client") as helper,
            patch(
                "shutil.which",
                side_effect=lambda name: (
                    "/usr/bin/flatpak" if name == "flatpak" else None
                ),
            ),
        ):
            helper.remove_flatpak_system.side_effect = fake_remove_flatpak_system
            results, _summary = actions.execute_batch_removal(apps)

        self.assertEqual(
            calls,
            [(("org.one.App",), "system"), (("org.two.App",), "kde")],
        )
        self.assertTrue(all(success for _app, success, _msg in results))

    def test_same_installation_is_removed_in_one_call(self):
        calls = []

        def fake_remove_flatpak_system(
            package_ids, installation=None, output_callback=None
        ):
            calls.append((tuple(package_ids), installation))
            return SimpleNamespace(
                ok=True, message="Removed.", events=[], returncode=0
            )

        apps = [
            SimpleNamespace(
                name="App One",
                manager="Flatpak",
                package_id="org.one.App",
                installation="kde",
                details="",
            ),
            SimpleNamespace(
                name="App Two",
                manager="Flatpak",
                package_id="org.two.App",
                installation="kde",
                details="",
            ),
        ]
        with (
            patch("appector.actions.helper_client") as helper,
            patch(
                "shutil.which",
                side_effect=lambda name: (
                    "/usr/bin/flatpak" if name == "flatpak" else None
                ),
            ),
        ):
            helper.remove_flatpak_system.side_effect = fake_remove_flatpak_system
            results, _summary = actions.execute_batch_removal(apps)

        self.assertEqual(calls, [(("org.one.App", "org.two.App"), "kde")])
        self.assertTrue(all(success for _app, success, _msg in results))


class BoundedLogReadTests(unittest.TestCase):
    """_read_log_lines keeps a bounded tail instead of the whole file."""

    def test_read_log_lines_keeps_only_the_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "dpkg.log"
            with open(log, "w", encoding="utf-8") as handle:
                for index in range(20000):
                    handle.write(f"line-{index:06d} padding-padding-padding\n")
            lines = scanners._read_log_lines(log, max_bytes=4096)
            self.assertLessEqual(sum(len(line) for line in lines), 8192)
            self.assertTrue(lines, "the tail must not be empty")
            self.assertTrue(
                lines[-1].startswith("line-019999"),
                "the tail must end with the file's last line",
            )
            self.assertTrue(
                lines[0] > "line-000000",
                "the head of the file must have been dropped",
            )


class DpkgGlobEscapeTests(unittest.TestCase):
    """dpkg-query -S arguments are glob patterns, not literals."""

    def test_glob_metacharacters_are_escaped(self):
        escaped = scanners._escape_dpkg_glob("/opt/game[1]/weird*name?.desktop")
        self.assertEqual(escaped, "/opt/game\\[1\\]/weird\\*name\\?.desktop")

    def test_plain_paths_are_unchanged(self):
        path = "/usr/share/applications/org.example.App.desktop"
        self.assertEqual(scanners._escape_dpkg_glob(path), path)


class DebReviewScratchDirTests(unittest.TestCase):
    """The .deb staging area honours XDG_CACHE_HOME like the rest."""

    def test_scratch_dir_honours_xdg_cache_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            xdg_cache = home / "xdg-cache"
            with patch.dict(
                os.environ,
                {"HOME": str(home), "XDG_CACHE_HOME": str(xdg_cache)},
            ):
                scratch = actions._deb_review_scratch_dir()
            self.assertEqual(scratch, xdg_cache / "appector" / "deb-review")
            self.assertTrue(scratch.is_dir())


class AppImageCopyIntegrityTests(unittest.TestCase):
    """The installed copy must match the reviewed content hash."""

    HEADER = (
        b"\x7fELF"  # ELF magic
        + b"\x00" * 4  # padding
        + b"\x02"  # AppImage type 2 marker at offset 8
        + b"\x00" * (actions.APPIMAGE_HEADER_SIZE - 9)
    )

    def test_changed_source_during_copy_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            source = home / "Downloads" / "myapp.AppImage"
            source.parent.mkdir(parents=True)
            source.write_bytes(self.HEADER + b"payload-one")

            app_dir = home / ".local/share/appector/appimages"
            swapped = []

            def appimage_dir_swapping_source():
                # install_appimage calls appimage_dir() after hashing the
                # source and before copying it: swap the file right there,
                # exactly like a concurrent writer would.
                if not swapped:
                    swapped.append(True)
                    source.write_bytes(self.HEADER + b"payload-two-changed")
                return app_dir

            with (
                patch("pathlib.Path.home", return_value=home),
                patch(
                    "appector.actions.appimage_dir",
                    side_effect=appimage_dir_swapping_source,
                ),
                patch(
                    "appector.actions.legacy_appimage_dir",
                    return_value=home / ".local/share/app-manager/appimages",
                ),
            ):
                success, message = actions.install_appimage(source)

            self.assertFalse(success)
            self.assertIn("changed while it was being installed", message)
            installed = (
                list(app_dir.glob("*.AppImage")) if app_dir.exists() else []
            )
            self.assertEqual(installed, [], "no image may survive a refusal")
            leftovers = (
                list(app_dir.glob(".*.part")) if app_dir.exists() else []
            )
            self.assertEqual(leftovers, [], "no .part file may survive")


if __name__ == "__main__":
    unittest.main()
