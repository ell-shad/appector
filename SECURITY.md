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
