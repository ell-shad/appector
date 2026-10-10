import hashlib
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appector import actions


class DebInstallReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def _make_package(self, name="example.deb", contents=b"not-a-real-deb"):
        package = self.root / name
        package.write_bytes(contents)
        return package

    def _mock_tools(
        self,
        host_architecture="amd64",
        package_architecture="all",
        simulation=(0, "Inst example [1.0]"),
    ):
        def run_command(command, **kwargs):
            if command[1:] == ["--print-architecture"]:
                return 0, host_architecture
            if command[1:2] == ["--field"]:
                return (
                    0,
                    "Package: example\n"
                    "Version: 1.0\n"
                    f"Architecture: {package_architecture}\n"
                    "Maintainer: Example Publisher\n"
                    "Depends: libc6\n"
                    "Description: Example package\n",
                )
            if "--simulate" in command:
                return simulation
            self.fail(f"Unexpected command: {command!r}")

        return (
            patch("appector.actions.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("appector.actions._run_command_raw", side_effect=run_command),
        )

    def test_review_stages_package_and_includes_metadata_hash_and_simulation(self):
        package = self._make_package(contents=b"package bytes")
        patches = self._mock_tools()
        with patches[0], patches[1]:
            review, error = actions.review_deb_batch([str(package)])

        self.assertEqual(error, "")
        self.assertIsNotNone(review)
        self.addCleanup(review.close)
        staged_path = review.staged_paths[0]
        self.assertNotEqual(staged_path, package)
        self.assertEqual(staged_path.read_bytes(), b"package bytes")
        self.assertEqual(stat.S_IMODE(staged_path.stat().st_mode), 0o400)
        self.assertIn("Package: example", review.summary)
        self.assertIn("Maintainer: Example Publisher", review.summary)
        self.assertIn(hashlib.sha256(b"package bytes").hexdigest(), review.summary)
        self.assertIn("Inst example [1.0]", review.summary)

    def test_review_refuses_an_incompatible_architecture(self):
        package = self._make_package()
        patches = self._mock_tools(package_architecture="arm64")
        with patches[0], patches[1]:
            review, error = actions.review_deb_batch([str(package)])

        self.assertIsNone(review)
        self.assertIn("system is amd64", error)

    def test_review_blocks_when_apt_cannot_simulate_transaction(self):
        package = self._make_package()
        patches = self._mock_tools(
            simulation=(100, "Unable to locate package dependency")
        )
        with patches[0], patches[1]:
            review, error = actions.review_deb_batch([str(package)])

        self.assertIsNone(review)
        self.assertIn("Installation is blocked", error)
        self.assertIn("Unable to locate package dependency", error)

    def test_review_refuses_symlink_selected_package(self):
        target = self._make_package("real.deb")
        link = self.root / "linked.deb"
        link.symlink_to(target)
        patches = self._mock_tools()
        with patches[0], patches[1]:
            review, error = actions.review_deb_batch([str(link)])

        self.assertIsNone(review)
        self.assertIn("Could not safely copy", error)

    def test_changed_original_is_not_moved_to_trash_after_review(self):
        package = self._make_package(contents=b"reviewed")
        patches = self._mock_tools()
        with patches[0], patches[1]:
            review, error = actions.review_deb_batch([str(package)])

        self.assertEqual(error, "")
        self.addCleanup(review.close)
        package.write_bytes(b"changed after review")
        with patch("appector.actions._maybe_delete_source_file") as trash:
            result = review.trash_unchanged_sources()

        trash.assert_not_called()
        self.assertIn("changed after review", result)

    def test_unchanged_original_is_moved_to_trash_only_after_install(self):
        package = self._make_package(contents=b"reviewed")
        patches = self._mock_tools()
        with patches[0], patches[1]:
            review, error = actions.review_deb_batch([str(package)])

        self.assertEqual(error, "")
        self.addCleanup(review.close)
        with patch(
            "appector.actions._maybe_delete_source_file",
            return_value="Moved installation file to Trash after successful installation.",
        ) as trash:
            result = review.trash_unchanged_sources()

        trash.assert_called_once_with(str(package), True, "")
        self.assertIn("Moved to Trash: example.deb", result)

    def test_review_rejects_non_deb_path(self):
        package = self._make_package("package.rpm")
        review, error = actions.review_deb_batch([str(package)])
        self.assertIsNone(review)
        self.assertIn("not a .deb package", error)

    def test_install_cannot_bypass_review(self):
        package = self._make_package()
        with patch("appector.actions._run_command_stream") as install:
            success, message = actions.install_deb_batch([str(package)])

        self.assertFalse(success)
        self.assertIn("review is required", message)
        install.assert_not_called()

    def test_install_is_blocked_if_apt_transaction_changed_after_review(self):
        package = self._make_package()
        patches = self._mock_tools()
        with patches[0], patches[1]:
            review, error = actions.review_deb_batch([str(package)])

        self.assertEqual(error, "")
        self.addCleanup(review.close)
        with (
            patch("appector.actions.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "appector.actions._run_command_raw",
                return_value=(0, "Inst different-package [1.0]"),
            ),
            patch("appector.actions._run_command_stream") as install,
        ):
            success, message = actions.install_deb_batch(
                review.staged_paths,
                review=review,
            )

        self.assertFalse(success)
        self.assertIn("transaction changed", message)
        install.assert_not_called()


class DebReviewResilienceTests(unittest.TestCase):
    """A swept-away staging directory must not fail an otherwise valid review.

    Staging used to live in /tmp, where a system-wide tmpfiles cleaner could
    remove it between the review dialog and the install. That surfaced as
    "Could not verify reviewed package ...: No such file or directory".
    """

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def _make_package(self, name="appector_0.2.0_all.deb", contents=b"package bytes"):
        package = self.root / name
        package.write_bytes(contents)
        return package

    def _which(self):
        return patch(
            "appector.actions.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        )

    def _field(self):
        return patch(
            "appector.actions._run_command_raw",
            side_effect=lambda command, **kwargs: (
                (0, "amd64")
                if command[1:] == ["--print-architecture"]
                else (
                    0,
                    "Package: appector\nVersion: 0.2.0\nArchitecture: all\n"
                    "Maintainer: Example Publisher\nDescription: Example\n",
                )
                if command[1:2] == ["--field"]
                else (0, "Inst appector [0.2.0]")
            ),
        )

    def _review(self, contents=b"package bytes"):
        package = self._make_package(contents=contents)
        with patch.object(Path, "home", return_value=self.root), self._which(), self._field():
            review, error = actions.review_deb_batch([str(package)])
        self.assertEqual(error, "")
        self.assertIsNotNone(review)
        self.addCleanup(review.close)
        return review

    def test_staging_is_not_in_system_tmp(self):
        review = self._review()
        scratch = Path(review.staged_paths[0]).parent
        self.assertTrue(
            str(scratch).startswith(str(self.root)),
            f"staging should live under the per-user cache, got {scratch}",
        )

    def test_vanished_staging_is_restored_and_install_proceeds(self):
        review = self._review()
        staged = Path(review.staged_paths[0])

        # Simulate the staging area being swept away after the review.
        shutil.rmtree(staged.parent)
        self.assertFalse(staged.exists())

        with self._which(), self._field(), patch(
            "appector.actions._run_command_stream", return_value=(True, "ok")
        ) as install:
            success, message = actions.install_deb_batch(
                review.staged_paths, review=review
            )
        self.assertTrue(success, message)
        install.assert_called_once()

    def test_changed_original_refuses_instead_of_installing(self):
        review = self._review(contents=b"original bytes")
        shutil.rmtree(Path(review.staged_paths[0]).parent)

        # The review no longer describes these bytes, so refuse rather than
        # silently install different content.
        Path(review.original_paths[0]).write_bytes(b"tampered bytes")

        with self._which(), patch("appector.actions._run_command_stream") as install:
            success, message = actions.install_deb_batch(
                review.staged_paths, review=review
            )
        self.assertFalse(success)
        self.assertIn("Review the file again", message)
        self.assertIn("No package was installed", message)
        install.assert_not_called()

    def test_missing_original_refuses_clearly(self):
        review = self._review()
        shutil.rmtree(Path(review.staged_paths[0]).parent)
        Path(review.original_paths[0]).unlink()

        with self._which(), patch("appector.actions._run_command_stream") as install:
            success, message = actions.install_deb_batch(
                review.staged_paths, review=review
            )
        self.assertFalse(success)
        self.assertIn("Review the file again", message)
        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
