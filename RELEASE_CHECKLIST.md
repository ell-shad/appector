# Appector release checklist

**Current decision: NO-GO. Do not tag, push a release tag, or publish until
every release-blocking item below is resolved and the owner explicitly
approves publication.** This checklist is not authorization to publish.

## Phase 0 — Orientation and build

| Item | Status | Notes |
|---|---|---|
| Identify language, framework, entry points and version source | Done | Python 3, PyGObject, GTK 4/libadwaita; `appector/__init__.py` is the version source (`0.1.0`). |
| Map privileged helper, polkit, D-Bus and system services | Done | `pkexec` and system package managers are used; no dedicated helper/action IDs, D-Bus service or systemd unit found. |
| Document package/runtime prerequisites | Done | Debian runtime dependencies are declared; no pip distribution is required. |
| Build from documented source commands | Done | `python3 -m unittest discover -s tests -v`; `./scripts/build-deb.sh`; package validation. |
| Clean-checkout build after audit commits | Not done | Must be recorded before audit completion. |
| Confirm every user-visible/build/tag version agrees | Done | CLI/About/build/tag gate use `appector/__init__.py`; release tag must be `v0.1.0` for current version. |
| Confirm runtime/build dependency versions and licences | Partial | Audit-host versions recorded; complete transitive licence/security review and cross-distro availability not done. |

## Phase 1 — Secrets and sensitive information

| Item | Status | Notes |
|---|---|---|
| Scan working tree and locally reachable history for high-confidence secret patterns | Partial | Manual scan found none; gitleaks/TruffleHog unavailable and remote refs inaccessible. |
| Scan all remote branches, tags and deleted history | Not done | Remote ref enumeration failed. |
| Review commit author/committer metadata | Done, decision required | Local commit metadata uses a personal email-provider domain; exact identities are intentionally omitted from public report. |
| Review Debian Maintainer identity | Partial | Uses GitHub noreply; owner must approve. |
| Inspect final `.deb` paths, caches, local paths and permissions | Done | No build paths/caches/vendor files/group-world-writable files detected; source `.py` files are included. |
| Review screenshots/icons/sample data for private information | Not applicable / incomplete | No screenshot or app icon is packaged; no screenshot-driven visual privacy review was possible. |
| Ensure logs/exports are user-private | Done | Log `0700` directory/`0600` file; exports `0600` with symlink-target regression test. |
| Identify direct network endpoints and telemetry | Partial | Direct update endpoint documented; package managers contact configured remotes; no telemetry client found by inspection, no sandbox capture. |
| Decide source-history identity and GPL corresponding-source delivery | Not done | See Needs Decision in `AUDIT_REPORT.md`. |

## Phase 2 — Code quality, cleanup and reliability

| Item | Status | Notes |
|---|---|---|
| Run tests and Python compilation | Done | 16 tests pass; `compileall` passes. |
| Run shell syntax and package linters | Done | `sh -n`, `lintian --pedantic`, desktop validation pass. |
| Run ShellCheck/Ruff/mypy/pytest | Not done | Unavailable locally; ShellCheck is included in CI. |
| Check duplicate/shadowed methods | Done | Duplicate GUI methods removed and regression test added. |
| Review dead code/imports/TODOs and broad exception handling | Partial | Duplicate methods addressed; no full static-analysis/dead-code audit. |
| Review UI-thread blocking, subprocess/network timeouts and resource cleanup | Partial | Update requests and version subprocesses have timeouts; package transactions are streamed and may run as long as the system operation. Full failure/resource review pending. |
| Force stable locale for parsed command output | Partial | APT and Snap parsed output covered; source-adapter fixtures and other command output need broader review. |
| Test optional Snap/Flatpak absence, offline, display, Wayland/X11 | Not done | No integration or GUI environment. |

## Phase 3 — Security and destructive operations

| Item | Status | Notes |
|---|---|---|
| GUI refuses root launch | Done | Unit-tested. |
| Protect Appector and legacy package from self-removal | Done | Unit-tested. |
| Review APT protected package list | Partial | Critical package/prefix unit tests pass; distro-specific meta/kernel/network package coverage needs review. |
| Dedicated polkit action per privileged action | Not done | Generic `pkexec`; a constrained helper/policy needs a design decision. |
| Strict package/app ID validation and argument handling | Partial | Package/app IDs validated; batched privileged script quotes package IDs; no comprehensive hostile-input fuzz suite. |
| TOCTOU/path symlink protections for manual removal | Mitigated | Privileged manual-app file deletion is blocked; no dedicated race-resistant helper exists. |
| Preview equals executed transaction for all managers | Not done | APT simulations exist; Snap/Flatpak/file-install behavior is not fully previewed/reverified. |
| APT autoremove/purge and Flatpak cleanup confirmation | Partial | Preview/confirmation exists; execution was not run. |
| Residual scanner uses safe allow-list/quarantine/restore index | Not applicable to implemented scope | Only dpkg `rc` conffiles are surfaced; backups precede purge, but no restore UI. No general filesystem residual deletion engine. |
| Local `.deb` metadata/trust/architecture/dependency/transaction preview | Not done — release blocker | Current path installs the selected file via `apt-get -y` without a metadata/signature/origin confirmation. |
| `.flatpakref`/AppImage hostile input and architecture tests | Not done | Requires isolated integration tests. |
| Update checker uses HTTPS, User-Agent, timeouts, safe URL/version parsing | Done | Unit tests cover Debian ordering and unsafe responses/URL. |
| Update checker cache/ETag/daily limit/disable setting | Not done | Current check is manual-only; no background checks. |
| Update checker only opens page, no auto-install | Done | Release URL is validated; app never downloads/executes updates. |
| Download integrity signature/attestation | Not done | SHA256SUMS only; no signature or provenance. |

## Phase 4 — Test plan and platform verification

| Item | Status | Notes |
|---|---|---|
| Unit tests for safety, update versions and response parsing | Done | See `TEST_MATRIX.md`. |
| Recorded fixtures for each source adapter, residual matching, batch queue/state | Not done | Existing suite does not cover these areas comprehensively. |
| Disposable-distro install/use/remove lifecycle matrix | Not done — release blocker | No container runtime or configured VM image. |
| Failure injection, package locks, disk-full and recovery | Not done | Must only run in disposable VMs. |
| Residual dataset and zero user-data safe-tier criterion | Not done | General residual feature absent; no precision dataset. |
| UI, accessibility, performance and memory tests | Not done | No visual/UI test execution. |
| Test coverage report | Not done | No coverage tooling installed. |

## Phase 5 — UI/UX review

| Recommendation | Status | Notes |
|---|---|---|
| Package name as well as launcher name; details/context menu | Partial | App details exist; verify package/launcher naming across all source adapters in GUI testing. |
| Tag/hide runtimes and components by default | Partial | Advanced filtering exists; UI behavior not visually verified. |
| Visible clearable filters, meaningful Installed/Size columns | Not done | No Size column; filter-state/accessibility review pending. |
| Bottom action bar; safe Mark All; actionable duplicates | Partial | Selection and duplicate filtering exist; layout and protected-item behavior need UI validation. |
| Scan/empty/error states | Partial | Empty states exist; no GUI failure-state test. |
| Trust cues for local/third-party packages | Not done — release blocker | `.deb` metadata/trust review is missing. |
| Confirmation on every destructive maintenance operation | Partial | APT leftovers and Flatpak runtime cleanup have preview/confirmation; all paths not integration-tested. |
| Batch per-file metadata/warnings and failure policy | Not done | Mixed queue exists; per-file trust review and end-to-end policy need implementation/testing. |
| AppImage location, safe delete-source, Flatpak scope/remote | Partial | AppImage warns it is executable and not sandboxed; full UX not tested. |
| Tooltips, plain errors, accessibility, translations | Partial | Some tooltips/errors exist; screen-reader/localization/overflow review pending. |

## Phase 6 — Debian package

| Item | Status | Notes |
|---|---|---|
| Accurate control metadata and dependencies | Partial | Lintian clean; maintainer/copyright assumptions provisional. |
| DEP-5 copyright coverage and licence match | Partial | Provisional GPL-3 metadata; owner must confirm grant and copyright. |
| Maintainer scripts safe/idempotent | Not applicable | No postinst/prerm/postrm scripts are packaged. |
| Desktop file validation and standard paths/modes | Done | Validated; launcher and files use standard paths; no unsafe write bits found. |
| AppStream, icons, MIME types, polkit, D-Bus and systemd artifacts | Not done / not applicable | AppStream/icons/MIME/polkit/D-Bus/systemd metadata not packaged; dedicated helper/policy remains a security gap. |
| Lintian `--pedantic` | Done | Clean on Ubuntu 26.04. |
| Reproducible build | Done on one host | Two same-host builds had identical SHA-256; clean chroot/container and cross-architecture builds not proven. |
| Install/upgrade/remove/purge test | Not done — release blocker | Never run on the host; perform in disposable environments. |
| Package size | Done | About 54 KiB compressed; Python source included. |

## Phase 7 — GitHub repository and release setup

| Item | Status | Notes |
|---|---|---|
| README install/update/privacy/known limitations/recovery | Partial | Drafted; no release link currently resolves and support channel/license decisions remain open. |
| SECURITY, CONTRIBUTING, CHANGELOG, issue/PR templates | Done as drafts | Verify repository security settings and public contact before publishing. |
| CI tests/build/package validation | Done as workflow configuration | Workflow YAML parses; CI itself was not run. |
| Pin actions and set least permissions | Done | Checkout is commit-pinned; workflows use `contents: read`. |
| Release assets `*.deb` and `SHA256SUMS` | Partial | Workflow configured, not run; destination repository is currently 404. |
| Detached signature/provenance | Not done | No signing secret/key or attestation. |
| Protected public-release environment and scoped token | Not done — release blocker | Configure reviewers and a token scoped only to the assets repository. |
| Branch protection, secret scanning/push protection, Dependabot, 2FA | Not verified | Repository settings need owner review. |
| APT repository recommendation and direct-deb limitation | Done | README explains release `.deb` does not update via `apt upgrade`; signed APT repository deferred. |

## Phase 8 — Additional release readiness

| Item | Status | Notes |
|---|---|---|
| Licence compatibility, artwork, trademarks and name collision | Partial | GPL-3 metadata provisional; no artwork/license or broad name/trademark collision search completed. |
| Unaffiliated-with-distributions notice | Done | README includes Debian/Ubuntu/Canonical/Flathub/Snap Store notice. |
| Privacy/network statement | Done with limitation | README documents local state and direct update endpoint; no sandbox network capture. |
| Man page and recovery instructions | Partial | Man page and basic dpkg recovery documentation exist; no user guide/restore UI. |
| Internationalization and locale formatting | Not done | No translation/accessibility/locale review. |
| Diagnostic export that redacts sensitive data | Not done | Installed-app export is private by default but intentionally contains inventory. |
| Data migration and upgrade path | Not done | No migration testing or versioned state format. |
| Support, rollback, yanking and downgrade policy | Not done | Must be decided before stable release. |
| Reverse-DNS app ID consistency | Partial | Desktop/application ID is `com.appector.appector`; no metainfo/polkit/D-Bus/icon ID to cross-check. |

## Owner decisions and prerequisites

1. Confirm project copyright/licence grant, GPL source-delivery approach and
   package maintainer identity.
2. Decide whether to defer local `.deb` installation until a trustworthy
   metadata/origin/transaction preview exists.
3. Decide on the privileged helper/polkit architecture and checksum signing.
4. Create the public assets repository; configure a narrowly scoped token,
   protected environment, required human reviewer and tag restrictions.
5. Approve the supported distro/architecture matrix, support/security contact,
   initial pre-release scope, rollback policy and any identity/history changes.
6. Complete the disposable integration matrix in `TEST_MATRIX.md`.

## Exact commands to tag and publish (do not run without explicit approval)

The following are **instructions only**. They will create and push a release
tag, triggering publication to the separate public repository. First replace
`v0.1.0` if the approved version changes, ensure all blocking items above are
closed, and obtain explicit owner approval.

```sh
git switch pre-release-audit
git status --short
git diff --check
python3 -m unittest discover -s tests -v
./scripts/build-deb.sh
lintian --pedantic dist/appector_0.1.0_all.deb
desktop-file-validate build/appector_0.1.0_all/usr/share/applications/com.appector.appector.desktop
git tag -a v0.1.0 -m "Appector 0.1.0"
git push origin v0.1.0
```

No tag, push, or release was created during this audit.
