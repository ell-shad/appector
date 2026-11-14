"""Helper protocol and validation tests (ADR-0003, S6 acceptance).

The helper's validation runs unprivileged here: hostile requests must be
rejected before any subprocess executes. Execution paths are exercised with
fakes; the real privileged run happens in the VM integration job.
"""

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appector import helper, helper_client
from appector.helper import RequestError, validate_request
from appector.plans import Plan


def _nonce():
    return "a" * 32


def _plan(action="remove", package_ids=("demo-pkg",), removed=("demo-pkg",)):
    return Plan(
        action=action,
        package_ids=list(package_ids),
        removed_preview=list(removed),
    )


def _request(**overrides):
    plan = _plan()
    request = {
        "version": 1,
        "action": "remove-apt",
        "nonce": _nonce(),
        "packages": ["demo-pkg"],
        "purge": False,
        "plan": {
            "action": plan.action,
            "package_ids": plan.package_ids,
            "removed_preview": plan.removed_preview,
            "text": "",
            "plan_hash": plan.plan_hash,
        },
        "plan_hash": plan.plan_hash,
    }
    request.update(overrides)
    return request


class ValidationTests(unittest.TestCase):
    def test_valid_remove_apt_request_passes(self):
        normalized = validate_request(_request())
        self.assertEqual(normalized["action"], "remove-apt")
        self.assertEqual(normalized["packages"], ["demo-pkg"])

    def test_unknown_top_level_keys_are_rejected(self):
        with self.assertRaises(RequestError):
            validate_request(_request(surprise="--exec-me"))

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(RequestError):
            validate_request(_request(action="run-anything"))

    def test_wrong_protocol_version_is_rejected(self):
        with self.assertRaises(RequestError):
            validate_request(_request(version=2))

    def test_bad_nonce_is_rejected(self):
        for nonce in (None, "", "xyz", "a" * 31, "a" * 33, "A" * 32, 5):
            with self.subTest(nonce=nonce):
                with self.assertRaises(RequestError):
                    validate_request(_request(nonce=nonce))

    def test_option_like_package_names_are_rejected(self):
        for packages in (["-rf"], ["--reinstall"], ["a;b"], ["a b"], ["$(x)"],
                         ["a\nb"], [""], [".."]):
            with self.subTest(packages=packages):
                with self.assertRaises(RequestError):
                    validate_request(_request(packages=packages))

    def test_oversized_package_list_is_rejected(self):
        with self.assertRaises(RequestError):
            validate_request(_request(packages=[f"pkg{i}" for i in range(200)]))

    def test_missing_plan_is_rejected_for_apt_actions(self):
        request = _request()
        del request["plan"]
        del request["plan_hash"]
        with self.assertRaises(RequestError):
            validate_request(request)

    def test_plan_hash_mismatch_is_rejected(self):
        request = _request(plan_hash="b" * 64)
        with self.assertRaises(RequestError):
            validate_request(request)

    def test_tampered_plan_body_is_rejected(self):
        request = _request()
        request["plan"]["removed_preview"] = ["demo-pkg", "innocent-bystander"]
        with self.assertRaises(RequestError):
            validate_request(request)

    def test_purge_flag_must_be_bool(self):
        normalized = validate_request(_request(purge=True))
        self.assertTrue(normalized["purge"])

    def test_snap_names_use_their_own_grammar(self):
        request = _request(action="remove-snap")
        request["packages"] = ["firefox"]
        del request["plan"]
        del request["plan_hash"]
        normalized = validate_request(request)
        self.assertEqual(normalized["packages"], ["firefox"])
        request["packages"] = ["--dangerous"]
        with self.assertRaises(RequestError):
            validate_request(request)

    def test_flatpak_refs_use_their_own_grammar(self):
        request = _request(action="remove-flatpak-system")
        request["packages"] = ["org.gimp.GIMP"]
        del request["plan"]
        del request["plan_hash"]
        normalized = validate_request(request)
        self.assertEqual(normalized["packages"], ["org.gimp.GIMP"])
        request["packages"] = ["-x"]
        with self.assertRaises(RequestError):
            validate_request(request)

    def test_install_deb_file_entries_are_strict(self):
        def deb_request(files):
            request = _request(action="install-deb")
            for key in ("packages", "plan", "plan_hash", "purge"):
                request.pop(key, None)
            request["files"] = files
            return request

        good = [{"path": "/home/u/x.deb", "sha256": "c" * 64}]
        self.assertEqual(validate_request(deb_request(good))["files"], good)
        for bad in (
            [{"path": "relative.deb", "sha256": "c" * 64}],
            [{"path": "/tmp/x.txt", "sha256": "c" * 64}],
            [{"path": "/tmp/x.deb", "sha256": "short"}],
            [{"path": "/tmp/x.deb", "sha256": "c" * 64, "extra": 1}],
            [{"path": "/tmp/x.deb"}],
            [],
            "not-a-list",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(RequestError):
                    validate_request(deb_request(bad))

    def test_newlines_anywhere_are_rejected(self):
        with self.assertRaises(RequestError):
            validate_request(_request(packages=["ok\n--evil"]))

    def test_apt_upgrade_plan_must_not_preview_removals(self):
        plan = _plan(action="apt-upgrade", package_ids=["demo-pkg"], removed=[])
        request = _request(action="apt-upgrade", packages=["demo-pkg"])
        request["plan"].update(
            action="apt-upgrade", removed_preview=[], plan_hash=plan.plan_hash
        )
        request["plan_hash"] = plan.plan_hash
        validate_request(request)  # ok

        sneaky = _plan(action="apt-upgrade", package_ids=["demo-pkg"],
                       removed=["sudo"])
        request["plan"].update(
            action="apt-upgrade", removed_preview=["sudo"], plan_hash=sneaky.plan_hash
        )
        request["plan_hash"] = sneaky.plan_hash
        with self.assertRaises(RequestError):
            validate_request(request)


class ProcessRequestTests(unittest.TestCase):
    def _run(self, request, authorized_action=None):
        stream = io.StringIO()
        result = helper.process_request(
            request, stream=stream, authorized_action=authorized_action
        )
        return result, [json.loads(line) for line in stream.getvalue().splitlines()]

    def test_rejected_request_emits_no_start_event(self):
        request = _request(action="nonexistent-action")
        result, events = self._run(request)
        self.assertFalse(result["ok"])
        self.assertTrue(result["message"].startswith("Rejected"))
        self.assertEqual([event["type"] for event in events], ["result"])

    def test_action_mismatch_with_authorization_is_rejected(self):
        request = _request(action="remove-apt")
        result, events = self._run(request, authorized_action="install-deb")
        self.assertFalse(result["ok"])
        self.assertIn("does not match", result["message"])
        self.assertEqual(events[0]["type"], "result")

    def test_missing_tool_reports_failure(self):
        request = _request()
        with patch("appector.helper.shutil.which", return_value=None):
            result, events = self._run(request)
        self.assertFalse(result["ok"])
        self.assertIn("apt-get", result["message"])
        types = [event["type"] for event in events]
        self.assertEqual(types, ["start", "result"])

    def test_remove_apt_gates_fail_before_execution(self):
        request = _request()
        with (
            patch("appector.helper.shutil.which", return_value="/usr/bin/apt-get"),
            patch("appector.helper._simulate_apt") as simulate,
            patch("appector.helper._run_streaming") as run,
        ):
            simulate.return_value = (0, "Remv demo-pkg\nRemv surprise-pkg",
                                     ["demo-pkg", "surprise-pkg"])
            result, events = self._run(request)
        run.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("surprise-pkg", result["message"])


class InstallDebSwapTests(unittest.TestCase):
    """S1 acceptance: a swapped staged file results in refusal."""

    def test_swapped_staged_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / "demo.deb"
            original.write_bytes(b"reviewed-bytes")
            reviewed_hash = hashlib.sha256(b"reviewed-bytes").hexdigest()

            # Between review and install, the file is replaced.
            original.write_bytes(b"evil-maintainer-scripts")

            with (
                patch("appector.helper.shutil.which", return_value="/usr/bin/apt-get"),
                patch("appector.helper.HELPER_STAGING_ROOT",
                      Path(temporary) / "staging"),
            ):
                stream = io.StringIO()
                result = helper.process_request(
                    {
                        "version": 1,
                        "action": "install-deb",
                        "nonce": _nonce(),
                        "files": [
                            {"path": str(original), "sha256": reviewed_hash}
                        ],
                    },
                    stream=stream,
                )
            self.assertFalse(result["ok"])
            self.assertIn("changed before installation", result["message"])

    def test_matching_file_installs_root_copy_not_user_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "demo.deb"
            source.write_bytes(b"reviewed-bytes")
            reviewed_hash = hashlib.sha256(b"reviewed-bytes").hexdigest()

            with (
                patch("appector.helper.shutil.which", return_value="/usr/bin/apt-get"),
                patch("appector.helper.HELPER_STAGING_ROOT",
                      Path(temporary) / "staging"),
                patch("appector.helper._run_streaming") as run,
            ):
                run_result = type("R", (), {
                    "success": True, "output": "installed", "returncode": 0,
                })()
                run.return_value = run_result
                stream = io.StringIO()
                result = helper.process_request(
                    {
                        "version": 1,
                        "action": "install-deb",
                        "nonce": _nonce(),
                        "files": [{"path": str(source), "sha256": reviewed_hash}],
                    },
                    stream=stream,
                )
            self.assertTrue(result["ok"])
            installed_paths = run.call_args.args[1]
            # The install must run on the root-owned copy, never the
            # user-writable source path (S1).
            self.assertNotIn(str(source), installed_paths)
            self.assertTrue(
                str(installed_paths[-1]).startswith(
                    str(Path(temporary) / "staging")
                )
            )


class HelperClientTests(unittest.TestCase):
    def test_helper_unavailable_raises_with_instructions(self):
        with patch.object(
            helper_client, "helper_path", return_value=None
        ):
            with self.assertRaises(helper_client.HelperUnavailable) as caught:
                helper_client.require_helper()
        self.assertIn("appector-helper", str(caught.exception))

    def test_nonce_mismatching_events_are_dropped(self):
        """S6: spoofed events cannot forge a result."""
        fake = self._fake_helper_process(
            output_lines=[
                json.dumps({"type": "output", "nonce": "f" * 32, "line": "noise"}),
                json.dumps({"type": "result", "nonce": "f" * 32, "ok": True,
                            "message": "spoofed"}),
                json.dumps({"type": "result", "nonce": None, "ok": False,
                            "message": "real"}),
            ]
        )
        with patch.object(helper_client, "require_helper", return_value=Path("/x")):
            with patch("appector.helper_client.subprocess.Popen", return_value=fake):
                with patch("appector.helper_client.shutil.which",
                           return_value="/usr/bin/pkexec"):
                    result = helper_client.run_helper("remove-snap",
                                                      packages=["firefox"])
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "real")

    @staticmethod
    def _fake_helper_process(output_lines):
        import io as _io

        class FakeProcess:
            def __init__(self):
                self.stdin = _io.StringIO()
                self.stdout = _io.StringIO("\n".join(output_lines) + "\n")
                self.stderr = _io.StringIO("")
                self.returncode = 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        return FakeProcess()

    def test_pkexec_argv_carries_env_for_older_runner(self):
        """S5 acceptance: the client passes the helper path through pkexec."""
        captured = {}

        class FakeProcess:
            def __init__(self):
                import io as _io

                self.stdin = _io.StringIO()
                self.stdout = _io.StringIO(
                    json.dumps({"type": "result", "nonce": None, "ok": True,
                                "message": "done"}) + "\n"
                )
                self.stderr = _io.StringIO("")
                self.returncode = 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        def fake_popen(command, **kwargs):
            captured["command"] = command
            return FakeProcess()

        with patch.object(helper_client, "require_helper", return_value=Path("/usr/lib/appector/appector-helper")):
            with patch("appector.helper_client.subprocess.Popen",
                       side_effect=fake_popen):
                with patch("appector.helper_client.shutil.which",
                           return_value="/usr/bin/pkexec"):
                    result = helper_client.run_helper(
                        "remove-snap", packages=["firefox"]
                    )
        self.assertTrue(result.ok)
        self.assertEqual(
            captured["command"],
            ["/usr/bin/pkexec", "/usr/lib/appector/appector-helper", "remove-snap"],
        )


if __name__ == "__main__":
    unittest.main()
