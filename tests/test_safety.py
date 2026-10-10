import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from appector import __version__
from appector import _proc, actions
from appector import main as main_module


class RemovalSafetyTests(unittest.TestCase):
    def test_gui_refuses_to_start_as_root(self):
        stderr = io.StringIO()
        with (
            patch("appector.main.os.geteuid", return_value=0),
            patch("appector.main.AppectorApp") as application,
            patch("sys.stderr", stderr),
        ):
            self.assertEqual(main_module.main([]), 1)
        application.assert_not_called()
        self.assertEqual(
            stderr.getvalue(),
            "Appector is a desktop application and must not be run as root.\n",
        )

    def test_version_option_exits_without_starting_gui(self):
        with (
            patch("appector.main.AppectorApp") as application,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            with self.assertRaises(SystemExit) as exit_info:
                main_module.main(["--version"])
        self.assertEqual(exit_info.exception.code, 0)
        self.assertEqual(
                stdout.getvalue(),
                f"appector {__version__}\n",
            )
        application.assert_not_called()

    def test_appector_and_legacy_package_cannot_be_removed(self):
        for package_id in ("appector", "app-manager"):
            with self.subTest(package_id=package_id):
                app = SimpleNamespace(manager="APT", package_id=package_id)
                self.assertTrue(actions.is_blocked(app))
                self.assertFalse(actions.can_remove(app))
                self.assertTrue(actions.is_critical_apt_package_name(package_id))

    def test_critical_apt_package_and_prefix_are_blocked(self):
        for package_id in ("sudo", "linux-image-custom", "ubuntu-desktop"):
            with self.subTest(package_id=package_id):
                app = SimpleNamespace(manager="APT", package_id=package_id)
                self.assertTrue(actions.is_blocked(app))

    def test_apt_removal_simulation_uses_c_locale(self):
        with (
            patch("appector.actions._log_action"),
            patch(
                "appector.actions._run_command_raw",
                return_value=(0, "Remv sample-package [1.0]"),
            ) as run_command,
        ):
            success, _, removed = actions.simulate_apt_remove_multiple(
                ["sample-package"]
            )
        self.assertTrue(success)
        self.assertEqual(removed, ["sample-package"])
        self.assertEqual(
            run_command.call_args.kwargs["env_overrides"],
            {"LC_ALL": "C"},
        )

    def test_action_log_is_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.object(Path, "home", return_value=home):
                _proc._log_action("TEST event")

            log_dir = home / ".local/state/app-manager"
            log_file = log_dir / "actions.log"
            self.assertEqual(os.stat(log_dir).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(log_file).st_mode & 0o777, 0o600)
            self.assertIn("TEST event", log_file.read_text(encoding="utf-8"))

    def test_app_list_export_is_private_and_replaces_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "existing-data"
            target.write_text("leave unchanged", encoding="utf-8")
            destination = root / "installed-apps.csv"
            destination.symlink_to(target)
            exporter = SimpleNamespace(
                current_apps=[
                    SimpleNamespace(
                        name="Example",
                        manager="APT",
                        package_id="example",
                        version="1.0",
                        source="local",
                        installed_at="",
                        category="Apps",
                    )
                ],
                show_message=Mock(),
            )

            main_module.MainWindow._export_installed_apps(
                exporter,
                str(destination),
                "csv",
            )

            self.assertFalse(destination.is_symlink())
            self.assertEqual(
                stat.S_IMODE(destination.stat().st_mode),
                0o600,
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "leave unchanged")
            self.assertEqual(
                exporter.show_message.call_args.args[0],
                "Export complete",
            )

    def test_action_log_refuses_symlinked_state_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            outside = root / "outside"
            home.mkdir()
            outside.mkdir()
            (home / ".local").mkdir()
            (home / ".local/state").mkdir()
            (home / ".local/state/app-manager").symlink_to(outside)

            with (
                patch.object(Path, "home", return_value=home),
                self.assertLogs("appector._proc", level="WARNING"),
            ):
                _proc._log_action("TEST event")

            self.assertFalse((outside / "actions.log").exists())

    def test_manual_removal_refuses_privileged_symlinked_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            outside = root / "outside"
            home.mkdir()
            outside.mkdir()
            (outside / "victim").write_text("test file", encoding="utf-8")
            (home / "Applications").symlink_to(outside, target_is_directory=True)
            app = SimpleNamespace(
                manager="Manual",
                package_id=str(home / "Applications" / "victim"),
                removal_paths=[str(home / "Applications" / "victim")],
            )

            with (
                patch.object(Path, "home", return_value=home),
                patch("appector.actions.os.access", return_value=False),
                patch("appector.actions._log_action"),
                patch("appector.actions._run_command_stream") as privileged_command,
            ):
                success, message = actions.execute_removal(app)

            self.assertFalse(success)
            self.assertIn("administrator privileges", message)
            privileged_command.assert_not_called()
            self.assertEqual(
                (outside / "victim").read_text(encoding="utf-8"),
                "test file",
            )


if __name__ == "__main__":
    unittest.main()
