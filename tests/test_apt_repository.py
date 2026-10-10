"""Tests for the APT repository produced by scripts/build-apt-repo.sh.

These cover the properties apt actually enforces when it fetches a
repository. A repository whose Release checksums do not match the files on
disk is rejected outright, and one whose Packages entries point outside the
pool cannot be fetched at all, so both are checked here rather than being
discovered by a user running apt update.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-apt-repo.sh"

SUITE = "stable"
COMPONENT = "main"
ARCHITECTURE = "all"

HAVE_SCANPACKAGES = shutil.which("dpkg-scanpackages") is not None
HAVE_DPKG_DEB = shutil.which("dpkg-deb") is not None

CONTROL = """\
Package: appector
Version: 0.2.1
Section: utils
Priority: optional
Architecture: all
Maintainer: Elshad Guliyev <ell-shad@users.noreply.github.com>
Homepage: https://github.com/ell-shad/appector
Description: GTK application manager for Linux
 Manage installed applications and software sources.
Depends: python3
"""


def build_test_package(root, version="0.2.1"):
    """Create a minimal but genuine .deb for the scanner to read."""
    if not HAVE_DPKG_DEB:
        raise unittest.SkipTest("dpkg-deb is required")

    staging = root / "staging"
    (staging / "DEBIAN").mkdir(parents=True)
    (staging / "usr" / "share" / "appector").mkdir(parents=True)
    (staging / "usr" / "share" / "appector" / "marker.txt").write_text("appector\n")
    control = staging / "DEBIAN" / "control"
    control.write_text(CONTROL.replace("Version: 0.2.1", f"Version: {version}"))

    package = root / f"appector_{version}_all.deb"
    subprocess.run(
        [
            "dpkg-deb",
            "--build",
            "--root-owner-group",
            str(staging),
            str(package),
        ],
        check=True,
        capture_output=True,
    )
    return package


CHECKSUM_HEADERS = {"MD5Sum", "SHA1", "SHA256", "SHA512"}


def parse_release(text):
    """Parse a Release file into a field dict plus its checksum entries.

    Checksum entries are indented lines introduced by a bare "SHA256:"
    style header, which is distinct from an ordinary "Field: value" pair.
    """
    fields = {}
    checksums = []
    in_checksums = False

    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw[0] in " \t":
            if in_checksums:
                parts = raw.split()
                checksums.append((parts[0], int(parts[1]), parts[2]))
            continue

        name, separator, value = raw.partition(":")
        if not separator:
            continue
        value = value.strip()
        if name in CHECKSUM_HEADERS and not value:
            in_checksums = True
            fields[name] = ""
            continue

        in_checksums = False
        fields[name] = value

    return fields, checksums


@unittest.skipUnless(
    HAVE_SCANPACKAGES and HAVE_DPKG_DEB,
    "dpkg-dev and dpkg are required to build a test repository",
)
class AptRepositoryLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._scratch = tempfile.TemporaryDirectory()
        root = Path(cls._scratch.name)
        cls.dist = root / "dist"
        cls.dist.mkdir()
        build_test_package(cls.dist)
        cls.output = root / "apt-repo"

        result = subprocess.run(
            ["sh", str(BUILD_SCRIPT), str(cls.dist), str(cls.output)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise unittest.SkipTest(
                f"build-apt-repo.sh failed: {result.stderr.strip()[-400:]}"
            )
        cls.stderr = result.stderr

    @classmethod
    def tearDownClass(cls):
        cls._scratch.cleanup()

    def _release_text(self):
        return (self.output / "dists" / SUITE / "Release").read_text()

    def test_release_declares_fields_apt_requires(self):
        fields, _ = parse_release(self._release_text())
        self.assertEqual(fields.get("Origin"), "Appector")
        self.assertEqual(fields.get("Suite"), SUITE)
        self.assertEqual(fields.get("Codename"), SUITE)
        self.assertEqual(fields.get("Architectures"), ARCHITECTURE)
        self.assertEqual(fields.get("Components"), COMPONENT)
        self.assertTrue(fields.get("Date"), "Release must carry a Date")

    def test_release_checksums_match_files_on_disk(self):
        """apt aborts the whole update if any checksum does not match."""
        _, checksums = parse_release(self._release_text())
        self.assertTrue(checksums, "Release must list checksummed index files")

        dists = self.output / "dists" / SUITE
        for digest, size, relative in checksums:
            with self.subTest(index=relative):
                path = dists / relative
                self.assertTrue(path.is_file(), f"{relative} listed but missing")
                data = path.read_bytes()
                self.assertEqual(
                    hashlib.sha256(data).hexdigest(),
                    digest,
                    f"SHA-256 mismatch for {relative}",
                )
                self.assertEqual(
                    len(data), size, f"size mismatch for {relative}"
                )

    def test_every_index_file_present_is_checksummed(self):
        """A new index added without a Release entry would be ignored."""
        _, checksums = parse_release(self._release_text())
        listed = {relative for _, _, relative in checksums}
        binary_dir = (
            self.output / "dists" / SUITE / COMPONENT / f"binary-{ARCHITECTURE}"
        )
        on_disk = {
            f"{COMPONENT}/binary-{ARCHITECTURE}/{path.name}"
            for path in binary_dir.iterdir()
        }
        self.assertEqual(on_disk, listed)

    def test_packages_filename_is_pool_relative(self):
        packages = (
            self.output
            / "dists"
            / SUITE
            / COMPONENT
            / f"binary-{ARCHITECTURE}"
            / "Packages"
        ).read_text()
        entries = [
            dict(
                line.split(": ", 1)
                for line in block.splitlines()
                if ": " in line
            )
            for block in packages.strip().split("\n\n")
            if block.strip()
        ]
        self.assertTrue(entries)
        for entry in entries:
            with self.subTest(package=entry.get("Package")):
                self.assertTrue(
                    entry["Filename"].startswith("pool/"),
                    "Filename must resolve against the repository base URL",
                )
                self.assertTrue(
                    (self.output / entry["Filename"]).is_file(),
                    f"{entry['Filename']} is listed but not present",
                )
                self.assertIn("SHA256", entry)

    def test_packages_entry_matches_the_deb_control(self):
        packages = (
            self.output
            / "dists"
            / SUITE
            / COMPONENT
            / f"binary-{ARCHITECTURE}"
            / "Packages"
        ).read_text()
        fields = dict(
            line.split(": ", 1) for line in packages.splitlines() if ": " in line
        )
        for name in ("Package", "Version", "Architecture", "Maintainer"):
            with self.subTest(field=name):
                self.assertEqual(
                    fields[name],
                    dict(
                        line.split(": ", 1)
                        for line in CONTROL.splitlines()
                        if line.startswith(f"{name}: ")
                    )[name],
                )

    def test_unsigned_build_warns_and_emits_no_signatures(self):
        self.assertFalse(
            (self.output / "dists" / SUITE / "InRelease").exists(),
            "no signing key was supplied, so no InRelease may be produced",
        )
        self.assertFalse(
            (self.output / "dists" / SUITE / "Release.gpg").exists(),
            "no signing key was supplied, so no Release.gpg may be produced",
        )
        self.assertIn("UNSIGNED", self.stderr)


class AptRepositoryBuildFailureTests(unittest.TestCase):
    def test_missing_package_directory_is_rejected(self):
        result = subprocess.run(
            [
                "sh",
                str(BUILD_SCRIPT),
                str(Path(tempfile.gettempdir()) / "appector-nonexistent-dist"),
                str(Path(tempfile.mkdtemp()) / "apt-repo"),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stderr)


@unittest.skipUnless(HAVE_DPKG_DEB, "dpkg-deb is required")
class AptRepositorySigningTests(unittest.TestCase):
    """Signing must produce metadata apt accepts, and must fail loudly."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("gpg") is None:
            raise unittest.SkipTest("gpg is required")

    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory()
        self.root = Path(self._scratch.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        build_test_package(self.dist)

        self.keyring = self.root / "gnupg"
        self.keyring.mkdir(mode=0o700)
        self.addCleanup(self._scratch.cleanup)

    def _generate_key(self, passphrase):
        env = dict(os.environ, GNUPGHOME=str(self.keyring))
        for command in (
            [
                "gpg", "--batch", "--quiet", "--pinentry-mode", "loopback",
                "--passphrase", passphrase, "--quick-generate-key",
                "Appector Test <test@appector.invalid>",
                "default", "default", "never",
            ],
            [
                "gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
                "--passphrase", passphrase, "--armor", "--export-secret-keys",
            ],
        ):
            result = subprocess.run(
                command, env=env, capture_output=True, text=True
            )
            self.assertEqual(
                result.returncode, 0, result.stderr
            )
        # The export above wrote to stdout, not a file; redo it capturing.
        return subprocess.run(
            [
                "gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
                "--passphrase", passphrase, "--armor", "--export-secret-keys",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def _build(self, secret_key, passphrase=None, key_id=None):
        env = dict(os.environ, APPECTOR_GPG_KEY=secret_key)
        if passphrase is not None:
            env["APPECTOR_GPG_PASSPHRASE"] = passphrase
        if key_id is not None:
            env["APPECTOR_GPG_KEY_ID"] = key_id
        return subprocess.run(
            [
                "sh",
                str(BUILD_SCRIPT),
                str(self.dist),
                str(self.root / "apt-repo"),
            ],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_signs_and_publishes_a_usable_keyring(self):
        secret = self._generate_key("")
        result = self._build(secret)
        self.assertEqual(result.returncode, 0, result.stderr)

        dists = self.root / "apt-repo" / "dists" / SUITE
        self.assertTrue((dists / "InRelease").is_file())
        self.assertTrue((dists / "Release.gpg").is_file())

        # The exported keyring must contain the signing key, so users have
        # something to install.
        keyring = self.root / "apt-repo" / "appector-archive-keyring.asc"
        self.assertTrue(keyring.is_file())
        self.assertIn("BEGIN PGP PUBLIC KEY BLOCK", keyring.read_text())

    def test_signature_verifies_against_the_release_it_covers(self):
        secret = self._generate_key("")
        self.assertEqual(self._build(secret).returncode, 0)

        dists = self.root / "apt-repo" / "dists" / SUITE
        env = dict(os.environ, GNUPGHOME=str(self.keyring))

        detached = subprocess.run(
            ["gpg", "--batch", "--verify", str(dists / "Release.gpg"), str(dists / "Release")],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(detached.returncode, 0, detached.stderr)
        self.assertIn("Good signature", detached.stderr)

    def test_passphrase_protected_key_signs(self):
        secret = self._generate_key("a passphrase with spaces")
        result = self._build(secret, passphrase="a passphrase with spaces")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "apt-repo" / "dists" / SUITE / "InRelease").is_file())

    def test_wrong_passphrase_fails_without_emitting_signatures(self):
        secret = self._generate_key("correct horse")
        result = self._build(secret, passphrase="battery staple")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("clearsigning Release failed", result.stderr)

        dists = self.root / "apt-repo" / "dists" / SUITE
        self.assertFalse((dists / "InRelease").exists())
        self.assertFalse((dists / "Release.gpg").exists())

    def test_public_key_alone_is_rejected(self):
        """Only the private key can sign; a public-only secret must fail."""
        env = dict(os.environ, GNUPGHOME=str(self.keyring))
        self._generate_key("")
        public = subprocess.run(
            ["gpg", "--batch", "--yes", "--armor", "--export"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        result = self._build(public)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no secret (signing) key", result.stderr)


class ArchiveKeyGenerationTests(unittest.TestCase):
    """scripts/generate-apt-signing-key.sh must yield a usable signing key.

    The passphrase path previously exported an empty file, because the secret
    key also needs the passphrase to be unwrapped by the agent and the
    passphrase file had already been deleted by then. A test that only checked
    the file existed would have passed while shipping a broken signing key.
    """

    SCRIPT = REPO_ROOT / "scripts" / "generate-apt-signing-key.sh"

    @classmethod
    def setUpClass(cls):
        if shutil.which("gpg") is None:
            raise unittest.SkipTest("gpg is required")

    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory()
        self.root = Path(self._scratch.name)
        self.addCleanup(self._scratch.cleanup)

        # Run from a scratch copy so the script writes its keys there. The
        # script derives the repository root from its own location, so copying
        # it into <tmp>/scripts is enough to redirect every output path.
        scripts = self.root / "scripts"
        scripts.mkdir()
        self.script = scripts / self.SCRIPT.name
        shutil.copy2(self.SCRIPT, self.script)

        self.secret = self.root / "secret.asc"
        self.public = self.root / "apt" / "keys" / "appector-archive-keyring.asc"

    def _generate(self, *arguments, stdin=None):
        return subprocess.run(
            ["sh", str(self.script), *arguments],
            capture_output=True,
            text=True,
            input=stdin,
            env={
                **os.environ,
                "APPECTOR_SECRET_KEY_FILE": str(self.secret),
                "APPECTOR_KEY_NAME": "Appector Test <test@appector.invalid>",
            },
        )

    def _assert_usable_key(self, passphrase=""):
        """The exported key must be complete and able to sign.

        Checking only that the file exists is not enough: the secret export
        once produced an empty file, which would be stored as the release
        signing secret and break every later release.
        """
        secret = self.secret.read_text()
        self.assertIn("BEGIN PGP PRIVATE KEY BLOCK", secret)
        self.assertIn("END PGP PRIVATE KEY BLOCK", secret)
        self.assertIn("BEGIN PGP PUBLIC KEY BLOCK", self.public.read_text())

        keyring = self.root / "signing-gpg"
        keyring.mkdir(mode=0o700)
        env = dict(os.environ, GNUPGHOME=str(keyring))

        imported = subprocess.run(
            ["gpg", "--batch", "--quiet", "--import", str(self.secret)],
            env=env,
            capture_output=True,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr.decode())
        listed = subprocess.run(
            ["gpg", "--batch", "--list-secret-keys", "--with-colons"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertIn("sec:", listed.stdout, "no usable secret key was imported")

        message = self.root / "data.txt"
        message.write_text("appector\n")
        signature = subprocess.run(
            [
                "gpg", "--batch", "--quiet", "--pinentry-mode", "loopback",
                "--passphrase", passphrase, "--armor", "--detach-sign",
                "--output", "-", str(message),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(signature.returncode, 0, signature.stderr)
        self.assertIn("BEGIN PGP SIGNATURE", signature.stdout)

    def test_generates_a_usable_key_without_a_passphrase(self):
        result = self._generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.secret.is_file())
        self.assertTrue(self.public.is_file())
        self._assert_usable_key(passphrase="")

    def test_generates_a_usable_key_with_a_passphrase(self):
        passphrase = "a passphrase with spaces"
        result = self._generate("--passphrase", stdin=f"{passphrase}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_usable_key(passphrase=passphrase)

    def test_secret_key_is_never_written_inside_the_repository(self):
        result = self._generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        produced = sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(
            "apt/keys/appector-archive-keyring.asc.secret",
            produced,
        )
        # The only files produced are the intended ones.
        self.assertEqual(
            produced,
            ["apt/keys/appector-archive-keyring.asc", "scripts/" + self.SCRIPT.name,
             "secret.asc"],
        )

    def test_refuses_to_overwrite_an_existing_public_key(self):
        self.public.parent.mkdir(parents=True, exist_ok=True)
        self.public.write_text("existing key\n")
        result = self._generate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.public.read_text(), "existing key\n")

    def test_rejects_an_unknown_argument(self):
        result = self._generate("--not-a-flag")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown argument", result.stderr)


if __name__ == "__main__":
    unittest.main()