import io
import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from appector.updater import _is_newer, check_for_update


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class DebianVersionTests(unittest.TestCase):
    def test_debian_version_ordering(self):
        self.assertTrue(_is_newer("v1:1.0", "2.0"))
        self.assertTrue(_is_newer("1.0+git1", "1.0"))
        self.assertFalse(_is_newer("1.0~beta1", "1.0"))
        self.assertFalse(_is_newer("1.0-1", "1.0-2"))
        self.assertFalse(_is_newer("v0.1.0", "0.1.0"))

    def test_rejects_invalid_versions(self):
        with self.assertRaises(ValueError):
            _is_newer("1.0; touch /tmp/not-run", "0.9")


class ReleaseCheckTests(unittest.TestCase):
    def _response(self, payload):
        return _Response(json.dumps(payload).encode("utf-8"))

    def test_newer_release_is_reported(self):
        payload = {
            "tag_name": "v0.2.0",
            "html_url": (
                "https://github.com/ell-shad/appector/"
                "releases/tag/v0.2.0"
            ),
        }
        with patch("appector.updater.urlopen", return_value=self._response(payload)):
            result = check_for_update("0.1.0")
        self.assertTrue(result["available"])
        self.assertEqual(result["version"], "0.2.0")

    def test_untrusted_release_url_is_rejected(self):
        payload = {
            "tag_name": "v0.2.0",
            "html_url": "file:///tmp/not-a-release",
        }
        with patch("appector.updater.urlopen", return_value=self._response(payload)):
            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                check_for_update("0.1.0")

    def test_non_object_api_response_is_rejected(self):
        with patch("appector.updater.urlopen", return_value=self._response([])):
            with self.assertRaisesRegex(RuntimeError, "invalid release response"):
                check_for_update("0.1.0")

    def test_missing_stable_release_is_reported_without_crashing(self):
        error = HTTPError(
            "https://api.github.com/releases/latest",
            404,
            "Not Found",
            hdrs=None,
            fp=None,
        )
        with patch("appector.updater.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "no published stable release"):
                check_for_update("0.1.0")


if __name__ == "__main__":
    unittest.main()
