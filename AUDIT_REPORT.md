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

The final committed tree was tested in an isolated clean worktree: all 16
tests passed there, the package passed `lintian --pedantic` and desktop-file
validation, and its `.deb` bytes matched the worktree build exactly. This
validates reproducible construction from the committed source; it does not
replace installation and lifecycle testing in a disposable Ubuntu VM.

The owner has confirmed Elshad Guliyev owns the original application code,
intends to license it under GNU GPL version 3, and selected Ubuntu 26.04 amd64
as the first target. The source repository is now public. Package and release
configuration has been aligned to publish source and binary assets from the
same repository, with a release-environment approval gate and the
automatically provided GitHub Actions token.

Release is still blocked until the release environment's required reviewer is
configured, the local `.deb` trust/metadata preview is addressed, and the
Ubuntu 26.04 amd64 package lifecycle is tested in a disposable environment.
Signed provenance is recommended but not yet configured. No known unresolved
critical finding remains; the open high-severity items below keep this a
no-go for now.

## Repository and build map

| Area | Location | Audit observation |
|---|---|---|
| Version source | [`appector/__init__.py`](./appector/__init__.py) | `0.1.0`; CLI, About dialog, builder and tag gate use this value. |
| GUI and entry point | [`appector/main.py`](./appector/main.py), [`appector/window.py`](./appector/window.py), [`run.py`](./run.py), [`appector/__main__.py`](./appector/__main__.py) | GTK 4/libadwaita GUI; rejects root launch; supports help/version. |
| Scanners and data model | [`appector/scanners.py`](./appector/scanners.py), `appector/models.py`, `appector/app_item.py` | APT, Snap, Flatpak, AppImage and detected manual apps; “leftovers” are dpkg residual-config packages only. |
| Package operations | [`appector/actions.py`](./appector/actions.py) | Package-manager and file operations; system authorisation uses `pkexec` where applicable. |
| Update checker | [`appector/updater.py`](./appector/updater.py) | HTTPS GitHub latest-release API; Debian version ordering; opens release page without downloading/installing. |
| Debian build | [`scripts/build-deb.sh`](./scripts/build-deb.sh), [`debian/copyright`](./debian/copyright), [`assets/icons/hicolor/`](./assets/icons/hicolor/) | Builds an `Architecture: all` binary package with hicolor icons; no maintainer scripts. |
| CI/release | [`.github/workflows/ci.yml`](./.github/workflows/ci.yml), [`.github/workflows/release.yml`](./.github/workflows/release.yml) | Tests and packages on CI; a `v*` tag can publish a release in the public source repository. |
| Tests | [`tests/`](./tests/) | 16 `unittest` tests; safety, updater and source-integrity checks. |
| User/release documentation | [`README.md`](./README.md), [`SECURITY.md`](./SECURITY.md), [`CONTRIBUTING.md`](./CONTRIBUTING.md), [`CHANGELOG.md`](./CHANGELOG.md) | Drafted; several public-release decisions and settings remain open. |

The package runtime dependencies declared in the `.deb` are `python3`,
`python3-gi`, GTK 4 introspection, libadwaita introspection, and GdkPixbuf
introspection. On the audit host these were Python 3.14.4, PyGObject 3.56.2,
GTK 4.22.4, libadwaita 1.9.1, and GdkPixbuf 2.44.5. The installed copyright
notices identify PyGObject, GTK and libadwaita as LGPL-2.1-or-later; GdkPixbuf
uses LGPL and other notices for its distributed components. These are system
libraries, not copied into Appector's package. The host also had
python3-apt 3.1.0, lintian 2.129.0, and desktop-file-utils 0.28. Appector does
not declare a pip runtime dependency set or require a Python package-index
distribution. No vendored third-party application code or artwork was found.
A complete transitive dependency/SBOM and vulnerability review was not
performed.

## Findings

| ID | Severity | Area | Location | Evidence and verification | Fix/status |
|---|---|---|---|---|---|
| A-01 | Critical (historical) | Privileged file deletion | [`appector/actions.py:1626`](./appector/actions.py)–1665; [`tests/test_safety.py:142`](./tests/test_safety.py) | The original path allowed a validated manual-app path to reach `pkexec rm`; lexical containment did not make a parent-directory symlink check race-resistant. A temporary-directory regression test verifies that the privileged path is refused, no `pkexec` command is invoked, and an outside sentinel remains unchanged. No real deletion was attempted. | **Mitigated.** Privileged manual-app removal is intentionally disabled until a race-resistant helper is implemented. |
| A-02 | High | Local `.deb` trust and install preview | [`appector/actions.py:755`](./appector/actions.py)–791 | The app's local-file installation feature invokes `apt-get install -y` on a user-selected `.deb`. This is separate from the release `.deb` artifact. There is no pre-install display of package metadata, signer/origin, maintainer-script warning, architecture/dependency summary, or transaction preview. Verified by code-path inspection; no package install was run. | **Open; release blocker.** Add metadata/trust and transaction review or disable local `.deb` installation in the first release. |
| A-03 | High | Release approval gate | [`.github/workflows/release.yml`](./.github/workflows/release.yml) | The repository is confirmed public. The workflow now targets the same repository using `GITHUB_TOKEN` and grants `contents: write` only to the release job; it references a `public-release` environment. GitHub environments without configured protection rules do not pause for approval, and settings cannot be verified from code. | **Open; release blocker.** Configure the `public-release` environment with a required reviewer and verify it before pushing a version tag. No second repository or PAT secret is needed. |
| A-04 | Medium | Privileged-operation policy | [`appector/actions.py:3125`](./appector/actions.py)–3215 | System package operations use generic `pkexec` invocation (including a constructed shell script for batched operations); no dedicated helper or action-specific polkit policy is present in the tracked/package inventory. Package IDs are validated/quoted, but there is no narrowly named authorization policy per action. | **Open.** Design a constrained helper and action IDs before expanding privileged operations; do not add a rushed policy as a release shortcut. |
| A-05 | Medium | Release integrity | [`.github/workflows/release.yml`](./.github/workflows/release.yml); [`SECURITY.md`](./SECURITY.md) | Releases include `SHA256SUMS` but no detached signature or GitHub artifact provenance attestation. A checksum fetched from the same release channel does not independently authenticate that channel. | **Open, recommended before first public binary.** Add provenance and verification instructions, or explicitly accept checksum-only integrity for a clearly marked pre-release. |
| A-06 | Resolved | Copyright, licence grant and maintainer identity | [`debian/copyright`](./debian/copyright), [`scripts/build-deb.sh`](./scripts/build-deb.sh), [`README.md`](./README.md) | The owner confirmed copyright ownership by Elshad Guliyev (2026), original application code, and intended GNU GPL version 3. Source/import inventory found standard-library and GTK/PyGObject GI imports, but no vendored third-party application code or artwork. The GPL license document is separately attributed to the Free Software Foundation. Package metadata now reflects these statements. | **Owner-confirmed and updated locally.** Runtime dependencies are system packages, not bundled; their package-specific licensing still needs review on the target distro. |
| A-07 | Medium | Platform/install validation | [`scripts/build-deb.sh`](./scripts/build-deb.sh); [`README.md`](./README.md) | Ubuntu 26.04 amd64 is the owner's first target and the available build host, but the package has not been installed or lifecycle-tested. Other distributions and arm64 are not in the initial support claim. Docker/Podman/LXD were unavailable and no disposable VM image was configured. | **Open; release blocker.** Test install, launch, upgrade, removal and purge in a disposable Ubuntu 26.04 amd64 environment before claiming support. |
| A-08 | Medium | Residual cleanup and recovery | [`appector/scanners.py:165`](./appector/scanners.py)–205; [`appector/window.py:4174`](./appector/window.py)–4235 | “Leftovers” cover dpkg `rc` residual configuration only, not a general allow-listed filesystem residual engine. Backups are made before purge, but there is no in-app restore/index browser. No labelled precision dataset was run. | **Limited scope documented.** Keep the feature restricted to dpkg-listed conffiles; add restore UX and a labelled dataset before claiming general cleanup. |
| A-09 | Medium | Update-check throttling/cache | [`appector/updater.py:54`](./appector/updater.py)–65; [`appector/window.py:2328`](./appector/window.py)–2355 | The checker is user-initiated and uses HTTPS, a User-Agent and a timeout, but does not use ETag/conditional requests, a cache, or a configurable disable option. GitHub API behavior was tested with mocks; no live stable release currently exists at the configured destination. | **Open, non-blocking for a strictly manual checker.** Decide whether to retain manual-only behavior or add opt-out, cache and conditional requests before automatic checks are considered. |
| A-10 | Low | Debian desktop integration | [`scripts/build-deb.sh`](./scripts/build-deb.sh), [`assets/icons/hicolor/`](./assets/icons/hicolor/) | The package now includes the desktop entry and icon in eight hicolor sizes and passes desktop-file validation. AppStream metainfo, MIME association, and localization catalog are absent. | **Partially complete.** Good for direct `.deb` release; add AppStream/MIME/localization before software-center distribution. |
| A-11 | Medium | Automated quality and source-adapter coverage | [`tests/test_safety.py:1`](./tests/test_safety.py), [`tests/test_updater.py:1`](./tests/test_updater.py) | Sixteen unit tests pass, but tests do not comprehensively cover recorded source-adapter fixtures, residual matching, batch state transitions, failure injection, the GUI, or the distribution matrix. Coverage was not measured. Ruff, mypy, pytest, ShellCheck, gitleaks and TruffleHog were unavailable. | **Open.** Expand tests and run the missing tooling in CI or a disposable test environment. |
| A-12 | Low | Release process / package lifecycle | [`.github/workflows/release.yml`](./.github/workflows/release.yml), [`README.md`](./README.md) | The package is a direct `.deb`, so it will not update via `apt upgrade`; there is no signed APT repository or rollback/yank drill. Public issue tracking is available, but supported-version policy is not yet published. | **Documented/deferred.** Use the release page for a manually approved pre-release; decide on an APT repository and support policy separately. |
| A-13 | Medium | Public branch protection | Public GitHub repository settings (`main`) | GitHub reports only `main` as a branch and `protected: false`. A direct push or compromised account could bypass CI/review requirements. | **Open.** Protect `main`, require pull requests and the CI `test-and-package` check, and prevent force-push/deletion. |
| A-14 | Medium | Public commit email privacy | Public `main` history and local `pre-release-audit` history | Commit metadata uses a Gmail-domain address in the published `main` history and in local audit-branch commits. The actual address is intentionally omitted; this is personal contact information, not a detected credential. | **Owner decision before pushing the audit branch.** Enable GitHub's private commit email for future commits. If you do not want the address in the unpublished audit commits, explicitly authorize rewriting that local-only branch before it is pushed. No published history was rewritten. |
| A-15 | Low | App and repository icon | [`assets/icons/hicolor/`](./assets/icons/hicolor/), [`assets/github-social-preview.png`](./assets/github-social-preview.png) | Owner-supplied artwork has been cropped to a square app icon, included in the README/About/window, and packaged in standard hicolor sizes. A 1200x630 GitHub social-preview image is ready. | **App/package integration done.** Upload the social preview manually in GitHub Settings after the asset is merged; repository owner avatar is separate and remains unchanged. |

## Security, privacy and history results

- A final targeted scan of the working tree and locally reachable Git blobs
  found no high-confidence secret-pattern matches. A separate mirror of the
  public repository scanned all refs visible at audit time: 14 commits and 101
  unique blobs, with zero high-confidence matches. These were manual patterns,
  **not** equivalent to gitleaks/TruffleHog; dedicated scanners were
  unavailable. No live credential was identified.
- The source repository is public. Published commit author/committer metadata
  uses a Gmail-domain address in the 14 commits checked; all 24 unpublished
  commits on the local audit branch use that domain too. Actual addresses are
  omitted. The package Maintainer field uses the owner's GitHub noreply
  address. Decide whether the personal email should remain public before
  pushing the audit branch; no history rewrite was performed.
- The final `.deb` contains 16 regular files. Inspection found no `.git`,
  `__pycache__`, `node_modules`, editor backups, embedded `/home/<user>`,
  `/tmp/`, or `/root/` paths, and no files with group/world write bits. It
  contains the Python source files by design. The package is about 54 KiB
  (55,000 bytes for this build).
- Activity logs are created under the user state directory with mode `0700`
  and file mode `0600`, with symlink checks. New installed-app exports use a
  mode-`0600` temporary file followed by atomic replacement; the test verifies
  a symlink destination does not overwrite the target. Both can contain
  software inventory and paths; documentation warns users to review before
  sharing.
- The direct Appector update endpoint is
  `https://api.github.com/repos/ell-shad/appector/releases/latest`.
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
release/package scaffolding, owner-confirmed copyright and maintainer
metadata, same-repository release publishing, improved purge warnings,
removal of shadowed duplicate GUI methods, and the confirmed obsolete-file
deletion. The regression suite and package validation pass for the changes
made so far.

| Category | Commit | Summary |
|---|---|---|
| Cleanup | `eeb315e` | Remove shadowed duplicate GUI methods; add a source-integrity regression test. |
| Security | `d11201a` | Harden privileged/removal/log/export/update behavior. |
| Tests | `1675ba4` | Add safety and updater regression tests. |
| Packaging | `ba2eb39` | Build reproducible `.deb`; add package and release workflows. |
| Documentation/audit | `671d13e`, `74bc51f` | Add audit report, test matrix and release guidance; record clean-checkout verification. |
| Owner follow-up | `6626370`, `0c53f10` | Align metadata, updater and publishing workflow with the public source repository and confirmed copyright; record the owner's intentional `window.txt` deletion. |
| App icon | `814b042` | Package hicolor icon sizes; apply icon to app windows, About dialog, launcher, README and GitHub preview art. |
| Release instructions | Complete | [`RELEASE_CHECKLIST.md`](./RELEASE_CHECKLIST.md) includes GitHub setup paths, isolated VM test steps, and the remaining owner decisions. |

## Owner actions remaining

1. Configure the `public-release` Actions environment with a required
   reviewer; verify tag deployments are restricted to `v*` and that an
   environment approval pauses publication.
2. Decide whether to add a local `.deb` metadata/trust/transaction preview
   before including that in-app feature in the first release, or disable local
   `.deb` installation for the initial release.
3. Add and document GitHub artifact provenance attestation (recommended), or
   explicitly accept checksum-only integrity for a clearly marked pre-release.
4. Review public commit author email visibility and update GitHub email
   privacy settings. The public history and local audit branch currently
   contain Gmail-domain author metadata; if the unpublished audit branch
   should not expose that, authorize a local-only rewrite before pushing.
5. Complete install, launch, upgrade, removal and purge tests in a disposable
   Ubuntu 26.04 amd64 environment.
6. Decide whether generic `pkexec` package operations are acceptable for an
   initial release or require a dedicated helper/polkit policy first.
7. Protect `main` with pull-request review and required CI checks; GitHub
   currently reports the default branch as unprotected.

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
- A manual high-confidence secret-pattern scan covered the public mirror
  history and local audit commits with no matches. Dedicated scanners were
  unavailable; repeat a dedicated scan on the final branch before merge.
