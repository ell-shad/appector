# Appector pre-release audit

Audit date: 2026-10-04
Branch: `pre-release-audit` (local only)
Recommendation: **NO-GO for public release**

## Executive summary

Appector is a Python 3 / PyGObject desktop application using GTK 4 and
libadwaita. Its entry points are `run.py`, `python -m appector`, and the
`appector` launcher installed by the Debian package. The project uses a shell
script to build an architecture-independent `.deb`; it does not need or
provide a PyPI distribution. A manual GitHub Releases check is implemented.
The GUI delegates privileged work to system package managers, generally via
`pkexec`; there is no dedicated privileged helper, D-Bus service, or
action-specific polkit policy in the package.

The audit found and mitigated a critical privileged manual-file deletion
hazard by refusing that operation whenever administrator privileges would
have been required. The regression test exercises the refusal path without
performing a privileged or destructive operation. Export files now use
private temporary files and atomic replacement, and a test confirms a
symlink destination is replaced without modifying its target.

The package builds reproducibly on the available Ubuntu 26.04 amd64 host,
passes `lintian --pedantic`, and passes desktop-file validation. Sixteen
automated tests pass. These results do **not** establish safe package
installation/removal, GUI usability, support on the target distributions, or
release readiness.

The committed tree was cloned locally into a clean checkout: all 16 tests
passed there, the package built and passed package/desktop validation, and its
`.deb` bytes matched the working-tree build.

Release is blocked until the local `.deb` trust/metadata preview is addressed,
the public release destination and its protected credential are configured,
license/copyright and maintainer metadata are confirmed, and the target
distribution/install matrix is tested in disposable environments. There are
no known unresolved critical findings; the open high-severity items below
still make release a no-go.

## Repository and build map

| Area | Location | Audit observation |
|---|---|---|
| Version source | [`appector/__init__.py`](./appector/__init__.py) | `0.1.0`; CLI, About dialog, builder and tag gate use this value. |
| GUI and entry point | [`appector/main.py`](./appector/main.py), [`appector/window.py`](./appector/window.py), [`run.py`](./run.py), [`appector/__main__.py`](./appector/__main__.py) | GTK 4/libadwaita GUI; rejects root launch; supports help/version. |
| Scanners and data model | [`appector/scanners.py`](./appector/scanners.py), `appector/models.py`, `appector/app_item.py` | APT, Snap, Flatpak, AppImage and detected manual apps; “leftovers” are dpkg residual-config packages only. |
| Package operations | [`appector/actions.py`](./appector/actions.py) | Package-manager and file operations; system authorisation uses `pkexec` where applicable. |
| Update checker | [`appector/updater.py`](./appector/updater.py) | HTTPS GitHub latest-release API; Debian version ordering; opens release page without downloading/installing. |
| Debian build | [`scripts/build-deb.sh`](./scripts/build-deb.sh), [`debian/copyright`](./debian/copyright) | Builds an `Architecture: all` binary package; no maintainer scripts. |
| CI/release | [`.github/workflows/ci.yml`](./.github/workflows/ci.yml), [`.github/workflows/release.yml`](./.github/workflows/release.yml) | Tests and packages on CI; a `v*` tag can publish to a separate public assets repository. |
| Tests | [`tests/`](./tests/) | 16 `unittest` tests; safety, updater and source-integrity checks. |
| User/release documentation | [`README.md`](./README.md), [`SECURITY.md`](./SECURITY.md), [`CONTRIBUTING.md`](./CONTRIBUTING.md), [`CHANGELOG.md`](./CHANGELOG.md) | Drafted; several public-release decisions and settings remain open. |

The package runtime dependencies declared in the `.deb` are `python3`,
`python3-gi`, GTK 4 introspection, libadwaita introspection, and GdkPixbuf
introspection. On the audit host these were Python 3.14.4, PyGObject 3.56.2,
GTK 4.22.4, libadwaita 1.9.1, and GdkPixbuf from the Ubuntu 26.04 archive.
The host also had python3-apt 3.1.0, lintian 2.129.0, and
desktop-file-utils 0.28. Appector does not declare a pip runtime dependency
set or require a Python package-index distribution. Transitive archive
licences, compatibility across all target distributions, and a complete
dependency vulnerability scan were not independently verified.

## Findings

| ID | Severity | Area | Location | Evidence and verification | Fix/status |
|---|---|---|---|---|---|
| A-01 | Critical (historical) | Privileged file deletion | [`appector/actions.py:1626`](./appector/actions.py)–1665; [`tests/test_safety.py:142`](./tests/test_safety.py) | The original path allowed a validated manual-app path to reach `pkexec rm`; lexical containment did not make a parent-directory symlink check race-resistant. A temporary-directory regression test verifies that the privileged path is refused, no `pkexec` command is invoked, and an outside sentinel remains unchanged. No real deletion was attempted. | **Mitigated.** Privileged manual-app removal is intentionally disabled until a race-resistant helper is implemented. |
| A-02 | High | Local `.deb` trust and install preview | [`appector/actions.py:755`](./appector/actions.py)–791 | The install path invokes `apt-get install -y` on a selected local package. There is no pre-install display of package metadata, signer/origin, maintainer-script warning, architecture/dependency summary, or transaction preview. This was verified by code-path inspection; installation was not run. | **Open; release blocker.** Add a clear metadata/trust review and transaction confirmation, or defer local `.deb` installation from the first public release. |
| A-03 | High | Public release destination | [`.github/workflows/release.yml:14`](./.github/workflows/release.yml), `:84`–87; [`README.md:121`](./README.md)–127 | The workflow requires the `public-release` environment and `APPECTOR_RELEASES_TOKEN` and targets `ell-shad/appector-releases`. That repository returned HTTP 404 during the audit; the token/environment/reviewer settings are not configured or verified. | **Open; release blocker.** Create/configure the destination and protect the credential/environment before any approved tag. |
| A-04 | Medium | Privileged-operation policy | [`appector/actions.py:3125`](./appector/actions.py)–3215 | System package operations use generic `pkexec` invocation (including a constructed shell script for batched operations); no dedicated helper or action-specific polkit policy is present in the tracked/package inventory. Package IDs are validated/quoted, but there is no narrowly named authorization policy per action. | **Open.** Design a constrained helper and action IDs before expanding privileged operations; do not add a rushed policy as a release shortcut. |
| A-05 | Medium | Release integrity | [`.github/workflows/release.yml:52`](./.github/workflows/release.yml)–55; [`SECURITY.md:25`](./SECURITY.md)–28 | The workflow publishes `SHA256SUMS` but no detached signature, Sigstore bundle, or provenance attestation. A checksum downloaded from the same release channel does not independently authenticate that channel. | **Open and documented.** Add signing/attestation and publish verification instructions, or explicitly accept this residual risk before distribution. |
| A-06 | Medium | Copyright, licence grant and maintainer identity | [`debian/copyright:4`](./debian/copyright)–12; [`scripts/build-deb.sh:60`](./scripts/build-deb.sh)–62; [`README.md:179`](./README.md)–181 | The repository contains GPL-3 text and the package metadata provisionally attributes copyright to “Appector contributors”; the project owner has not confirmed the copyright holder/grant or that GPL-3-only is the intended project licence. The package maintainer uses a GitHub noreply address. | **Needs owner decision before public distribution.** Confirm the rights holder, grant, year, maintainer identity, and dependency/artwork licence compatibility. |
| A-07 | Medium | Platform/install validation | [`scripts/build-deb.sh`](./scripts/build-deb.sh); [`README.md:55`](./README.md)–66 | The package was built/inspected but not installed. Only an Ubuntu 26.04 amd64 host was available; Debian stable, Ubuntu LTS, Mint, Pop!_OS, Raspberry Pi OS, arm64, package lifecycle, and GUI integration were not tested. Docker/Podman/LXD were unavailable; no disposable VM image was configured. | **Open; release blocker for support claims.** Run the matrix in `TEST_MATRIX.md` in disposable systems before advertising support. |
| A-08 | Medium | Residual cleanup and recovery | [`appector/scanners.py:165`](./appector/scanners.py)–205; [`appector/window.py:4174`](./appector/window.py)–4235 | “Leftovers” cover dpkg `rc` residual configuration only, not a general allow-listed filesystem residual engine. Backups are made before purge, but there is no in-app restore/index browser. No labelled precision dataset was run. | **Limited scope documented.** Keep the feature restricted to dpkg-listed conffiles; add restore UX and a labelled dataset before claiming general cleanup. |
| A-09 | Medium | Update-check throttling/cache | [`appector/updater.py:54`](./appector/updater.py)–65; [`appector/window.py:2328`](./appector/window.py)–2355 | The checker is user-initiated and uses HTTPS, a User-Agent and a timeout, but does not use ETag/conditional requests, a cache, or a configurable disable option. GitHub API behavior was tested with mocks; no live stable release currently exists at the configured destination. | **Open, non-blocking for a strictly manual checker.** Decide whether to retain manual-only behavior or add opt-out, cache and conditional requests before automatic checks are considered. |
| A-10 | Medium | Debian desktop integration | [`scripts/build-deb.sh`](./scripts/build-deb.sh) | The built package has a desktop entry and man page and passed `desktop-file-validate`, but has no AppStream metainfo, packaged application icon, MIME association, localization catalog, or AppStream validation result. | **Open.** Add metadata/assets before seeking software-center distribution; otherwise keep distribution explicitly limited to direct `.deb` releases. |
| A-11 | Medium | Automated quality and source-adapter coverage | [`tests/test_safety.py:1`](./tests/test_safety.py), [`tests/test_updater.py:1`](./tests/test_updater.py) | Sixteen unit tests pass, but tests do not comprehensively cover recorded source-adapter fixtures, residual matching, batch state transitions, failure injection, the GUI, or the distribution matrix. Coverage was not measured. Ruff, mypy, pytest, ShellCheck, gitleaks and TruffleHog were unavailable. | **Open.** Expand tests and run the missing tooling in CI or a disposable test environment. |
| A-12 | Low | Release process / package lifecycle | [`.github/workflows/release.yml`](./.github/workflows/release.yml), [`README.md`](./README.md) | The package is a direct `.deb`, so it will not update via `apt upgrade`; there is no signed APT repository, rollback/yank drill, supported-version policy, or public support channel. | **Documented/deferred.** Use the release page for the initial manually approved release; decide on an APT repository and support policy separately. |

## Security, privacy and history results

- The working tree and 80 locally available commits (354 unique blobs in the
  earlier audit pass) had no high-confidence secret-pattern matches. This was
  a manual pattern scan, **not** equivalent to gitleaks/TruffleHog; those
  scanners were unavailable. Remote refs could not be enumerated, so this is
  not a claim about unseen remote branches or tags. No live credential was
  identified.
- Commit author/committer metadata in the local history uses a personal
  email-provider domain. Individual addresses are intentionally omitted from
  this report. The separate public assets repository would not expose this
  private source history, but making the source public later requires an
  explicit identity/privacy decision. No history rewrite was performed.
- The final `.deb` contains 16 regular files. Inspection found no `.git`,
  `__pycache__`, `node_modules`, editor backups, embedded `/home/<user>`,
  `/tmp/`, or `/root/` paths, and no files with group/world write bits. It
  contains the Python source files by design. The package is about 54 KiB.
- Activity logs are created under the user state directory with mode `0700`
  and file mode `0600`, with symlink checks. New installed-app exports use a
  mode-`0600` temporary file followed by atomic replacement; the test verifies
  a symlink destination does not overwrite the target. Both can contain
  software inventory and paths; documentation warns users to review before
  sharing.
- The direct Appector update endpoint is
  `https://api.github.com/repos/ell-shad/appector-releases/releases/latest`.
  Package-manager operations may contact the user's configured APT, Snap or
  Flatpak remotes; Flathub is documented as
  `https://dl.flathub.org/repo/flathub.flatpakrepo`. No telemetry/analytics
  client was found by source inspection. Network behavior was not monitored
  in a sandbox.
- The updater treats API data as untrusted, validates the HTTPS GitHub release
  URL and Debian version, uses timeouts and opens the release page only. It
  never downloads or executes an update. GitHub latest semantics intentionally
  exclude drafts and pre-releases; beta-channel updates are unsupported.

## Fixes and commits

Fixes include the manual-removal privilege refusal, root GUI refusal and
self-removal blocks, private action logging/exports, locale-stable parsed
output, Debian-version-aware update checks, URL/API response validation,
release/package scaffolding, improved purge warnings, and removal of
shadowed duplicate GUI methods. The latest export fix and all tests passed
before commits were made.

| Category | Commit | Summary |
|---|---|---|
| Cleanup | `eeb315e` | Remove shadowed duplicate GUI methods; add a source-integrity regression test. |
| Security | `d11201a` | Harden privileged/removal/log/export/update behavior. |
| Tests | `1675ba4` | Add safety and updater regression tests. |
| Packaging | `ba2eb39` | Build reproducible `.deb`; add package and release workflows. |
| Documentation/audit | `671d13e`, `74bc51f` | Add audit report, test matrix and release guidance; record clean-checkout verification. |

## Needs decision

1. Confirm that the project owner intends to distribute under GPL-3-only and
   has authority to grant that licence; confirm the copyright holder/year and
   whether the maintainer noreply identity is acceptable.
2. The source repository is private, while the `.deb` contains the Python
   source. Decide how every binary recipient will receive the corresponding
   source and build/install scripts under the applicable GPL terms.
3. Decide whether the first public release should include local `.deb`
   installation. Recommendation: defer it until metadata, origin/trust and
   transaction preview are implemented and tested.
4. Decide whether generic `pkexec` package operations are acceptable for the
   first release or should be replaced by a narrowly scoped helper/polkit
   policy.
5. Create/configure the separate public release repository, scoped token and
   protected environment with required reviewers. Confirm the private-source /
   public-binary model and the manual-release trust limitations.
6. Decide on signing/attestation, the supported distribution/architecture
   matrix, public support/security contact, and initial `0.1.0` pre-release
   wording.
7. Review whether local Git commit identity metadata needs changing before
   any future source-publication decision. No history rewrite is recommended
   or performed without explicit approval.

## Not verified / skipped

- No package install, upgrade, remove or purge was performed on the host.
- No disposable VM/container integration testing, distro/architecture matrix,
  root/helper failure injection, lock contention, disk-full, crash recovery,
  or system-damaging test was run.
- No visual GTK session, accessibility/Orca, keyboard-only, Wayland/X11,
  HiDPI, theme, translation, narrow-window, performance or memory test was
  performed.
- No live update API success path, offline network capture, Snap/Flatpak/APT
  source operation, or release upload was performed.
- No detached checksum signature, Sigstore/provenance, SBOM, complete
  transitive dependency licence/vulnerability audit, AppStream metadata
  validation, name/trademark collision search, or release rollback drill was
  completed.
- Ruff, mypy, pytest, ShellCheck, gitleaks and TruffleHog were not installed.
  Shell syntax was checked with `sh -n`; CI is configured to run ShellCheck.
- The remote branch/tag set was not available for the history scan.
