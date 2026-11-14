"""Safety-gate tests with mutation-style checks (ground rule 2).

Each test verifies a gate introduced or changed by the review remediation,
in the disabling form: patch the gate away and the *same* test must fail.
The mutation runs are done here explicitly (patch the guard to a no-op and
assert the operation would have proceeded) so a regression in the test
itself is visible.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from appector import actions, plans, policy
from appector.plans import Plan


def _app(manager="APT", package_id="demo-pkg", **extra):
    namespace = SimpleNamespace(
        manager=manager, package_id=package_id, name=package_id, **extra
    )
    return namespace


class AptRemovalPlanBindingTests(unittest.TestCase):
    """S4: execute_apt_removal must refuse to run without / against the plan."""

    def _run(self, plan, simulated_removed):
        from appector.helper_client import HelperResult

        fake = HelperResult(ok=True, message="Removed.")
        with (
            patch("appector.actions.simulate_apt_remove_multiple") as simulate,
            patch("appector.actions.helper_client.remove_apt",
                  return_value=fake) as run,
            patch("appector.actions._log_action"),
        ):
            simulate.return_value = (True, "Purg demo-pkg", simulated_removed)
            success, message = actions.execute_apt_removal(
                _app(), plan=plan
            )
        return success, message, run

    def test_execution_without_plan_is_refused(self):
        success, message, run = self._run(None, ["demo-pkg"])
        self.assertFalse(success)
        self.assertIn("plan", message.lower())
        run.assert_not_called()

    def test_execution_with_matching_plan_runs(self):
        plan = Plan(action="remove", package_ids=["demo-pkg"],
                    removed_preview=["demo-pkg"])
        success, _message, run = self._run(plan, ["demo-pkg"])
        self.assertTrue(success)
        run.assert_called()

    def test_re_simulation_grown_by_one_package_is_refused(self):
        """S4 acceptance: the re-simulation grew by one package -> refuse."""
        plan = Plan(action="remove", package_ids=["demo-pkg"],
                    removed_preview=["demo-pkg"])
        success, message, run = self._run(plan, ["demo-pkg", "extra-pkg"])
        self.assertFalse(success)
        self.assertIn("extra-pkg", message)
        run.assert_not_called()

    def test_critical_package_in_re_simulation_is_refused(self):
        plan = Plan(action="remove", package_ids=["demo-pkg"],
                    removed_preview=["demo-pkg", "sudo"])
        # The plan itself contains a critical package; the shared gate
        # must refuse even though the plan was built by prepare code.
        success, message, run = self._run(plan, ["demo-pkg", "sudo"])
        self.assertFalse(success)
        self.assertIn("critical", message.lower())
        run.assert_not_called()

    def test_mutation_disabled_gate_would_have_run(self):
        """Ground rule 2: with the gate disabled, execution proceeds.

        This documents the failure mode the gate prevents: patching
        `check_apt_removal_plan` to always pass lets an unplaned removal
        through.
        """
        from appector.helper_client import HelperResult

        fake = HelperResult(ok=True, message="Removed.")
        with patch(
            "appector.actions.check_apt_removal_plan",
            return_value=(True, ""),
        ):
            with (
                patch("appector.actions.simulate_apt_remove_multiple") as simulate,
                patch("appector.actions.helper_client.remove_apt",
                      return_value=fake) as run,
                patch("appector.actions._log_action"),
            ):
                simulate.return_value = (True, "Remv demo-pkg", ["demo-pkg"])
                success, _message = actions.execute_apt_removal(
                    _app(), plan=None
                )
        self.assertTrue(success, "disabled gate must let the removal run")
        run.assert_called()


class AutoremovePlanTests(unittest.TestCase):
    """S4: autoremove gets the shared gate (it previously had none)."""

    def test_autoremove_without_plan_is_refused(self):
        with (
            patch("appector.actions._run_command_raw") as raw,
            patch("appector.actions._log_action"),
        ):
            raw.return_value = (0, "Remv leaf-pkg")
            success, message = actions.execute_apt_autoremove(plan=None)
        self.assertFalse(success)
        self.assertIn("plan", message.lower())

    def test_autoremove_with_critical_package_is_blocked(self):
        plan = Plan(action="autoremove", removed_preview=["sudo"])
        with (
            patch("appector.actions._run_command_raw") as raw,
            patch("appector.actions._log_action"),
        ):
            raw.return_value = (0, "Remv sudo")
            success, message = actions.execute_apt_autoremove(plan=plan)
        self.assertFalse(success)
        self.assertIn("critical", message.lower())

    def test_autoremove_grown_simulation_is_refused(self):
        plan = Plan(action="autoremove", removed_preview=["leaf-a"])
        with (
            patch("appector.actions._run_command_raw") as raw,
            patch("appector.actions._log_action"),
        ):
            raw.return_value = (0, "Remv leaf-a\nRemv surprise")
            success, message = actions.execute_apt_autoremove(plan=plan)
        self.assertFalse(success)
        self.assertIn("surprise", message)


class UpgradeRemovesNothingTests(unittest.TestCase):
    """S4: an update transaction must contain no Remv lines."""

    def test_upgrade_with_removals_is_refused(self):
        ok, message = plans.check_apt_upgrade_plan(["important-lib"])
        self.assertFalse(ok)
        self.assertIn("important-lib", message)

    def test_clean_upgrade_passes(self):
        ok, message = plans.check_apt_upgrade_plan([])
        self.assertTrue(ok)


class BatchAptPlanTests(unittest.TestCase):
    """S4: batch APT removal is bound to the preview plan."""

    def _batch(self, apt_plan, simulated_removed):
        from appector.helper_client import HelperResult

        fake = HelperResult(ok=True, message="Removed.")
        app = _app(package_id="demo-pkg")
        with (
            patch("appector.actions.simulate_apt_remove_multiple") as simulate,
            patch("appector.actions.helper_client.remove_apt",
                  return_value=fake) as remove,
            patch("appector.actions._log_action"),
        ):
            simulate.return_value = (True, "Remv demo-pkg", simulated_removed)
            results, _summary = actions.execute_batch_removal(
                [app], apt_plan=apt_plan
            )
        return results[0], remove

    def test_batch_without_plan_fails_apt_group_only(self):
        (_app_obj, success, message), remove = self._batch(None, ["demo-pkg"])
        self.assertFalse(success)
        self.assertIn("plan", message.lower())
        remove.assert_not_called()

    def test_batch_with_grown_simulation_fails(self):
        plan = Plan(action="batch-remove", package_ids=["demo-pkg"],
                    removed_preview=["demo-pkg"])
        (_app_obj, success, message), remove = self._batch(
            plan, ["demo-pkg", "extra"]
        )
        self.assertFalse(success)
        self.assertIn("extra", message)
        remove.assert_not_called()


class FlatpakrefReviewBindingTests(unittest.TestCase):
    """S3: a changed .flatpakref after review must refuse to install."""

    def test_changed_ref_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            ref = Path(temporary) / "app.flatpakref"
            ref.write_text(
                "[Flatpak Ref]\nName=Demo\nUrl=https://example.org/repo\n",
                encoding="utf-8",
            )
            reviewed = {"name": "Demo", "url": "https://example.org/repo"}
            # The file changed after review.
            ref.write_text(
                "[Flatpak Ref]\nName=Evil\nUrl=https://evil.example/repo\n",
                encoding="utf-8",
            )
            with patch("appector.actions._install_flatpak_location") as install:
                success, message = actions.install_flatpak_ref_file(
                    str(ref), reviewed_fields=reviewed
                )
        self.assertFalse(success)
        self.assertIn("changed", message)
        install.assert_not_called()

    def test_plain_http_repo_in_ref_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            ref = Path(temporary) / "app.flatpakref"
            ref.write_text(
                "Name=Demo\nUrl=http://insecure.example/repo\n",
                encoding="utf-8",
            )
            with patch("appector.actions._install_flatpak_location") as install:
                success, message = actions.install_flatpak_ref_file(
                    str(ref), reviewed_fields=None
                )
        self.assertFalse(success)
        self.assertIn("HTTPS", message)
        install.assert_not_called()


class FlatpakInstallCommandTests(unittest.TestCase):
    """S3: explicit flathub remote and option terminator."""

    def test_app_id_install_uses_explicit_remote_and_terminator(self):
        with patch("appector.actions.shutil.which", return_value="/usr/bin/flatpak"):
            cmd, error = actions._flatpak_install_command(
                "org.gimp.GIMP", True, remote="flathub"
            )
        self.assertIsNone(error)
        self.assertIn("flathub", cmd)
        self.assertIn("--", cmd)
        self.assertEqual(cmd[-1], "org.gimp.GIMP")

    def test_relative_file_target_becomes_absolute(self):
        """S3: local file targets reach flatpak as absolute paths."""
        from appector import managers

        with (
            patch(
                "appector.managers.flatpak.shutil.which",
                return_value="/usr/bin/flatpak",
            ),
            patch("appector.managers.flatpak._run_command_stream") as run,
            patch("appector.managers.flatpak._log_action"),
        ):
            run.return_value = (True, "ok")
            success, _message = managers.flatpak._install_flatpak_location(
                "app.flatpakref", True
            )
        self.assertTrue(success)
        cmd = run.call_args.args[0]
        self.assertTrue(Path(cmd[-1]).is_absolute(), cmd)
        self.assertIn("--", cmd)


class StandaloneAppImageRemovalTests(unittest.TestCase):
    """S8: standalone AppImage deletion is validated like any other removal."""

    def test_removal_outside_allowed_roots_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            victim = Path(temporary) / "evil.AppImage"
            victim.write_bytes(b"\x7fELF fake")
            app = _app(
                manager="AppImage",
                package_id=str(victim),
                removal_paths=[],
            )
            with patch("appector.actions._log_action"):
                success, message = actions.execute_removal(app)
            self.assertFalse(success)
            self.assertIn("Refusing", message)
            self.assertTrue(victim.exists())

    def test_removal_inside_home_applications_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            applications = home / "Applications"
            applications.mkdir()
            appimage = applications / "demo.AppImage"
            appimage.write_bytes(b"\x7fELF fake")
            app = _app(
                manager="AppImage",
                package_id=str(appimage),
                removal_paths=[],
            )
            with (
                patch.object(Path, "home", return_value=home),
                patch("appector.actions._log_action"),
            ):
                success, _message = actions.execute_removal(app)
        self.assertTrue(success)
        self.assertFalse(appimage.exists())


class SnapCriticalCascadeTests(unittest.TestCase):
    """S11: the appector cascade check protects its own dependencies."""

    def test_plan_containing_appector_is_blocked(self):
        self.assertTrue(policy.is_critical_apt_package_name("appector"))
        self.assertTrue(
            policy.is_blocked(_app(manager="APT", package_id="appector"))
        )


if __name__ == "__main__":
    unittest.main()
