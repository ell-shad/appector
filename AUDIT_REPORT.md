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
passes `lintian --pedantic`, and passes desktop-file validation. Twenty-five
automated tests pass. These results do **not** establish safe package
installation/removal, GUI usability, support on the target distributions, or
release readiness.

The final committed tree still needs a clean-worktree rerun after the current
local-package review and provenance changes. A built `.deb` passed local
metadata inspection, APT simulation, `lintian --pedantic`, and desktop-file
validation; no install was run by the audit.

The owner has confirmed Elshad Guliyev owns the original application code,
intends to license it under GNU GPL version 3, and selected Ubuntu 26.04 amd64
as the first target. The source repository is now public. Package and release
configuration has been aligned to publish source and binary assets from the
same repository, with a release-environment approval gate and the
automatically provided GitHub Actions token.

The release workflow now creates GitHub build-provenance attestations for the
release `.deb`, and the in-app local `.deb` installer now displays package
metadata and an APT transaction simulation before proceeding. Attestations
will only exist after a successful release run. Release is still blocked
until the required reviewer is configured for the release environment and
the Ubuntu 26.04 amd64 package lifecycle is tested in a disposable
environment. No known unresolved critical finding remains.

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
| Tests | [`tests/`](./tests/) | 25 `unittest` tests; safety, local-package review, updater and source-integrity checks. |
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
| A-02 | High (mitigated) | Local `.deb` trust and install preview | [`appector/actions.py`](./appector/actions.py), [`appector/window.py`](./appector/window.py), [`tests/test_deb_install.py`](./tests/test_deb_install.py) | The app's local-file installation feature now copies selected files into a private temporary staging directory, displays package/version/architecture/maintainer/dependency metadata, file SHA-256 and an APT simulation, and blocks install if inspection/simulation fails or architecture is incompatible. Maintainer scripts can run as root; publisher identity/signatures are not verified. The app does not elevate the GUI; it uses `pkexec` for the APT operation. Regression tests cover architecture mismatch, failed simulation, symlink refusal, review hash, and altered-source handling. No actual package install was run. | **Mitigated for initial release, with residual trust risk.** Users must trust the package source and review the simulation; signatures are not verified. |
| A-03 | High | Release approval gate | [`.github/workflows/release.yml`](./.github/workflows/release.yml) | The release workflow targets this repository using `GITHUB_TOKEN`; `contents: write` is confined to the release job. The `public-release` environment exists with a verified `v*` tag policy, but its public API response has no required reviewer and permits administrator bypass. | **Open; release blocker.** Add a required reviewer and disable administrator bypass before pushing a version tag. No second repository or PAT secret is needed. |
| A-04 | Medium | Privileged-operation policy | [`appector/actions.py`](./appector/actions.py) | System package operations use generic `pkexec` invocation; no dedicated helper or action-specific polkit policy is present. The GUI refuses to run as root; only individual system package operations request authorization. | **Owner accepted for initial beta.** Keep the GUI unprivileged; consider a constrained helper/action-specific policy before broader release adoption. |
| A-05 | Medium (mitigated) | Release integrity | [`.github/workflows/release.yml`](./.github/workflows/release.yml); [`SECURITY.md`](./SECURITY.md) | Releases include `SHA256SUMS` and the workflow is configured to create a GitHub artifact provenance attestation for the `.deb`. Neither detached signing nor a successful tagged release attestation has yet been verified. | **Workflow configured.** Verify the first successful release attestation with `gh attestation verify`; checksums remain unsigned. |
| A-06 | Resolved | Copyright, licence grant and maintainer identity | [`debian/copyright`](./debian/copyright), [`scripts/build-deb.sh`](./scripts/build-deb.sh), [`README.md`](./README.md) | The owner confirmed copyright ownership by Elshad Guliyev (2026), original application code, and intended GNU GPL version 3. Source/import inventory found standard-library and GTK/PyGObject GI imports, but no vendored third-party application code or artwork. The GPL license document is separately attributed to the Free Software Foundation. Package metadata now reflects these statements. | **Owner-confirmed and updated locally.** Runtime dependencies are system packages, not bundled; their package-specific licensing still needs review on the target distro. |
| A-07 | Medium | Platform/install validation | [`scripts/build-deb.sh`](./scripts/build-deb.sh); [`README.md`](./README.md) | Ubuntu 26.04 amd64 is the owner's first target and the available build host, but the package has not been installed or lifecycle-tested. Other distributions and arm64 are not in the initial support claim. Docker/Podman/LXD were unavailable and no disposable VM image was configured. | **Open; release blocker.** Test install, launch, upgrade, removal and purge in a disposable Ubuntu 26.04 amd64 environment before claiming support. |
| A-08 | Medium | Residual cleanup and recovery | [`appector/scanners.py:165`](./appector/scanners.py)–205; [`appector/window.py:4174`](./appector/window.py)–4235 | “Leftovers” cover dpkg `rc` residual configuration only, not a general allow-listed filesystem residual engine. Backups are made before purge, but there is no in-app restore/index browser. No labelled precision dataset was run. | **Limited scope documented.** Keep the feature restricted to dpkg-listed conffiles; add restore UX and a labelled dataset before claiming general cleanup. |
| A-09 | Medium | Update-check throttling/cache | [`appector/updater.py:54`](./appector/updater.py)–65; [`appector/window.py:2328`](./appector/window.py)–2355 | The checker is user-initiated and uses HTTPS, a User-Agent and a timeout, but does not use ETag/conditional requests, a cache, or a configurable disable option. GitHub API behavior was tested with mocks; no live stable release currently exists at the configured destination. | **Open, non-blocking for a strictly manual checker.** Decide whether to retain manual-only behavior or add opt-out, cache and conditional requests before automatic checks are considered. |
| A-10 | Low | Debian desktop integration | [`scripts/build-deb.sh`](./scripts/build-deb.sh), [`assets/icons/hicolor/`](./assets/icons/hicolor/) | The package now includes the desktop entry and icon in eight hicolor sizes and passes desktop-file validation. AppStream metainfo, MIME association, and localization catalog are absent. | **Partially complete.** Good for direct `.deb` release; add AppStream/MIME/localization before software-center distribution. |
| A-11 | Medium | Automated quality and source-adapter coverage | [`tests/`](./tests/) | Twenty-five unit tests pass, but tests do not comprehensively cover recorded source-adapter fixtures, residual matching, batch state transitions, failure injection, the GUI, or the distribution matrix. Coverage was not measured. Ruff, mypy, pytest, ShellCheck, gitleaks and TruffleHog were unavailable. | **Open.** Expand tests and run the missing tooling in CI or a disposable test environment. |
| A-12 | Low | Release process / package lifecycle | [`.github/workflows/release.yml`](./.github/workflows/release.yml), [`README.md`](./README.md) | The package is a direct `.deb`, so it will not update via `apt upgrade`; there is no signed APT repository or rollback/yank drill. Public issue tracking is available, but supported-version policy is not yet published. | **Documented/deferred.** Use the release page for a manually approved pre-release; decide on an APT repository and support policy separately. |
| A-13 | Medium | Public branch protection | Public GitHub repository settings (`main`) | GitHub reports no repository rulesets and `main` as unprotected. A direct push or compromised account could bypass CI/review requirements. | **Open.** Create an active ruleset for `main`, require pull requests and the CI check, and prevent force-push/deletion. |
| A-14 | Medium | Public commit identity privacy | Public `main` history and local `pre-release-audit` history | Some already-published commits use the author identity recorded at the time; the unpublished audit branch still needs identity cleanup before it is pushed. | **Local branch rewrite authorized and pending.** Rewrite the unpublished branch to use the owner's GitHub noreply identity. This does not alter any history already published on `main`. |
| A-15 | Low | App and repository icon | [`assets/icons/hicolor/`](./assets/icons/hicolor/), [`assets/github-social-preview.png`](./assets/github-social-preview.png) | Owner-supplied artwork has been cropped to a square app icon, included in the README/About/window, and packaged in standard hicolor sizes. A 1200x630 GitHub social-preview image is ready. | **App/package integration done.** Upload the social preview manually in GitHub Settings after the asset is merged; repository owner avatar is separate and remains unchanged. |

## Security, privacy and history results

- A final targeted scan of the working tree and locally reachable Git blobs
  found no high-confidence secret-pattern matches. A separate mirror of the
  public repository scanned all refs visible at audit time: 14 commits and 101
  unique blobs, with zero high-confidence matches. These were manual patterns,
  **not** equivalent to gitleaks/TruffleHog; dedicated scanners were
  unavailable. No live credential was identified.
- The source repository is public. Some published commits retain author
  metadata that predates the privacy settings. The owner authorized rewriting
  only the unpublished audit branch to use the GitHub noreply identity before
  push; published `main` will not be rewritten. No actual personal address is
  included in this report.
- The final `.deb` includes Appector's Python source files and eight hicolor
  icon assets by design. Package paths, caches, embedded build paths and file
  modes should be rechecked after the final build. It contains no vendored
  runtime libraries by design.
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

| Category | Summary |
|---|---|
| Cleanup | Remove shadowed duplicate GUI methods; add a source-integrity regression test. |
| Security | Harden privileged/removal/log/export/update behavior. |
| Tests | Add safety, updater and local-package-review regression tests. |
| Packaging | Build reproducible `.deb`; add CI, same-repository release workflow and provenance attestation. |
| Documentation/audit | Add audit report, test matrix and release guidance; record clean-worktree verification. |
| Owner follow-up | Align metadata, updater and publishing workflow with the public source repository and confirmed copyright; record the owner's intentional `window.txt` deletion. |
| App icon | Package hicolor icon sizes; apply icon to app windows, About dialog, launcher, README and GitHub preview art. |
| Local package review | Require package metadata and APT transaction review before any in-app local `.deb` installation. |
| Release instructions | [`RELEASE_CHECKLIST.md`](./RELEASE_CHECKLIST.md) includes GitHub setup paths, isolated VM test steps, and the remaining owner actions. |

## Owner actions remaining

1. Add a required reviewer to `public-release` and disable administrator
   bypass. The `v*` tag policy is already set.
2. Create a `main` branch ruleset that requires PRs/CI and blocks force-push
   and deletion; GitHub currently reports no rulesets.
3. Complete/confirm install, launch, remove and purge tests in a disposable
   Ubuntu 26.04 amd64 VM.
4. Push the local audit branch only after the authorized GitHub noreply
   rewrite and final clean-worktree validation.
5. The owner accepted generic `pkexec` authorization for the initial beta;
   the GUI remains unprivileged and system actions receive individual
   authorization prompts.

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
- No detached checksum signature, successful release attestation, SBOM, complete
  transitive dependency licence/vulnerability audit, AppStream metadata
  validation, name/trademark collision search, or release rollback drill was
  completed.
- Ruff, mypy, pytest, ShellCheck, gitleaks and TruffleHog were not installed.
  Shell syntax was checked with `sh -n`; CI is configured to run ShellCheck.
- A manual high-confidence secret-pattern scan covered the public mirror
  history and local audit commits with no matches. Dedicated scanners were
  unavailable; repeat a dedicated scan on the final branch before merge.
