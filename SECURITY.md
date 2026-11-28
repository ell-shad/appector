# Security policy

## Reporting a vulnerability

Please do not report an exploitable vulnerability in a public issue.

The Appector source repository is public. If GitHub private vulnerability
reporting is enabled for `ell-shad/appector`, use the repository's **Report a
vulnerability** flow. If it is not enabled, contact the repository owner
privately through GitHub and ask for a secure reporting channel. Do not send
credentials, personal logs, or exploit details through a public issue.

Include the affected Appector version, distribution and architecture, impact,
and minimal reproduction steps. Redact usernames, home paths, tokens, and
installed-application lists.

## Supported versions

No public release has been published yet. Once releases begin, security fixes
will target the latest supported release. The maintainer must publish the
supported-version policy before declaring the first stable release.

## Release integrity

The release workflow attaches Debian packages and SHA-256 checksums to GitHub
Releases and generates GitHub build-provenance attestations for the package.
Attestations are not available until a release workflow succeeds. The
checksums are not separately signed. Users can verify package provenance with
`gh attestation verify ./appector_<version>_all.deb --repo ell-shad/appector`.
The in-app checker only opens a release page; it does not download or install
package files.

## APT repository signing key

The APT repository metadata is signed with a dedicated archive key held as
the `APPECTOR_GPG_KEY` repository secret (see ADR-0003's release notes and
`scripts/generate-apt-signing-key.sh`). The matching public key is committed
at `apt/keys/appector-archive-keyring.asc` so it is reviewable in pull
requests; the release workflow refuses a mismatch between the committed key
and the signing secret.

### Key rotation

1. Generate a new key: `./scripts/generate-apt-signing-key.sh` (never reuse
   the old key material).
2. Commit the new public key to `apt/keys/appector-archive-keyring.asc`
   **alongside** the old one, and ship *both* in the published
   `appector-archive-keyring.asc` for at least one release cycle, so
   existing users can roll forward without manual steps.
3. Update the `APPECTOR_GPG_KEY`, `APPECTOR_GPG_KEY_ID` and
   `APPECTOR_GPG_PASSPHRASE` secrets.
4. Publish the next release; verify the published metadata verifies under
   both keys.
5. Remove the old public half only after the minimum supported version has
   shipped with the new key (one release cycle).

### Key revocation / compromise

1. Revoke the compromised key and commit the revocation certificate to
   `apt/keys/`.
2. Immediately stop publishing: disable the `public-release` environment so
   the APT workflow cannot sign with the compromised key.
3. Announce the compromise with the revocation fingerprint in the release
   notes of the next release and in the repository README.
4. Rotate per "Key rotation" above; the first release signed with the new
   key must instruct users to re-import the keyring (the announcement
   covers this).
5. A `workflow_dispatch` run of the APT workflow prints the archive public
   key; use it to confirm which key is currently configured.

### Repository metadata hygiene

Release metadata carries `Valid-Until` (30 days) so a stale mirror cannot be
served indefinitely, and `Acquire-By-Hash: yes` indexes so mirror updates
are atomic for clients. The window is wider than the release cadence and a
scheduled workflow re-signs and republishes the metadata weekly, so the
archive only expires if that automation has been broken for a month. The
pool retains the newest five releases and the index lists every retained
version, so in-flight updates from a previous release and explicit version
pins still resolve; older packages are removed only after the retention
window. Releases that were only ever attached to GitHub Releases are
backfilled into the pool from their assets — on every publish and on the
weekly refresh — and each asset is checked against that release's
`SHA256SUMS` before it is served.
