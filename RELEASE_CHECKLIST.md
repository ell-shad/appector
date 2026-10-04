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
| Clean-checkout build after audit commits | Pending | Re-run after the icon/package changes are committed. |
| Confirm every user-visible/build/tag version agrees | Done | CLI/About/build/tag gate use `appector/__init__.py`; release tag must be `v0.1.0` for current version. |
| Confirm runtime/build dependency versions and licences | Partial | Source inventory found standard-library and system GI imports, with no vendored application code/assets; review target archive dependency licences and vulnerabilities. |
| Application icon and GitHub branding asset | Partial | Icon is packaged and shown in the README/About dialog; preview artwork is ready, but upload `assets/github-social-preview.png` manually in Settings → General → Social preview. |

## Phase 1 — Secrets and sensitive information

| Item | Status | Notes |
|---|---|---|
| Scan working tree and locally reachable history for high-confidence secret patterns | Partial | Manual pattern scan found no matches; gitleaks/TruffleHog unavailable. |
| Scan all public remote refs/history for high-confidence secret patterns | Partial | Public mirror scan covered 14 commits and 101 blobs with no matches; dedicated scanners still unavailable. |
| Review commit author/committer metadata | Decision required | Public history and local audit commits use a Gmail-domain email; review privacy before pushing the local branch. Actual addresses are omitted from audit files. |
| Review Debian Maintainer identity | Done | Package metadata uses `Elshad Guliyev <ell-shad@users.noreply.github.com>`. |
| Inspect final `.deb` paths, caches, local paths and permissions | Done | No build paths/caches/vendor files/group-world-writable files detected; source `.py` files are included. |
| Review screenshots/icons/sample data for private information | Done for supplied branding | No application screenshot or sample user data is bundled. The chosen icon and generated social-preview image were visually reviewed; no personal information is visible. |
| Ensure logs/exports are user-private | Done | Log `0700` directory/`0600` file; exports `0600` with symlink-target regression test. |
| Identify direct network endpoints and telemetry | Partial | Direct update endpoint documented; package managers contact configured remotes; no telemetry client found by inspection, no sandbox capture. |
| Confirm copyright, GPLv3 intent, and corresponding source availability | Done | Owner confirmed original code and copyright; source repo is public and matching source/build scripts are included at each release tag. |

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
| Local `.deb` metadata/trust/architecture/dependency/transaction preview | Not done — release blocker | Concerns Appector's in-app install-local-`.deb` feature, not the generated release package; current UI runs `apt-get -y` without a complete review. |
| `.flatpakref`/AppImage hostile input and architecture tests | Not done | Requires isolated integration tests. |
| Update checker uses HTTPS, User-Agent, timeouts, safe URL/version parsing | Done | Unit tests cover Debian ordering and unsafe responses/URL. |
| Update checker cache/ETag/daily limit/disable setting | Not done | Current check is manual-only; no background checks. |
| Update checker only opens page, no auto-install | Done | Release URL is validated; app never downloads/executes updates. |
| Download integrity signature/attestation | Not done | SHA256SUMS only; GitHub provenance is recommended, or explicitly accept checksum-only integrity for a pre-release. |

## Phase 4 — Test plan and platform verification

| Item | Status | Notes |
|---|---|---|
| Unit tests for safety, update versions and response parsing | Done | See `TEST_MATRIX.md`. |
| Recorded fixtures for each source adapter, residual matching, batch queue/state | Not done | Existing suite does not cover these areas comprehensively. |
| Disposable Ubuntu 26.04 amd64 install/use/remove lifecycle | Not done — release blocker | This is the owner's initial target; no container runtime or configured VM image was available. |
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
| Desktop file validation and standard paths/modes | Done | Validated; app icon is installed in hicolor sizes and referenced by desktop entry; no unsafe write bits found. |
| AppStream, icons, MIME types, polkit, D-Bus and systemd artifacts | Partial | Hicolor application icons and desktop entry are packaged; AppStream/MIME and dedicated polkit/helper metadata are absent. Direct GitHub `.deb` distribution can proceed without AppStream, but not without resolving release blockers. |
| Lintian `--pedantic` | Done | Clean on Ubuntu 26.04. |
| Reproducible build | Done on one host | Two same-host builds had identical SHA-256; clean chroot/container and cross-architecture builds not proven. |
| Install/upgrade/remove/purge test | Not done — release blocker | Never run on the host; perform in disposable environments. |
| Package size | Partial | Previous package was about 54 KiB; icon assets add size, measure the updated package after final build. |

## Phase 7 — GitHub repository and release setup

| Item | Status | Notes |
|---|---|---|
| README install/update/privacy/known limitations/recovery | Partial | Updated for public source and same-repository releases; a release link will resolve after the first approved tag. |
| SECURITY, CONTRIBUTING, CHANGELOG, issue/PR templates | Done as drafts | Verify repository security settings and public contact before publishing. |
| CI tests/build/package validation | Done as workflow configuration | Workflow YAML parses; CI itself was not run. |
| Pin actions and set least permissions | Done | Checkout is commit-pinned; release workflow grants `contents: write` only to the release job; CI is read-only. |
| Release assets `*.deb` and `SHA256SUMS` | Partial | Workflow now targets this public source repository with `GITHUB_TOKEN`; no release upload was run. |
| Detached signature/provenance | Not done | Add GitHub artifact provenance; no detached signature/attestation yet. |
| Protected `public-release` environment | Not done — release blocker | Configure required reviewer and tag restrictions; no PAT secret or second repository is needed. |
| Branch protection, secret scanning/push protection, Dependabot, 2FA | Not done | GitHub reports `main` unprotected; require PRs and CI, prevent force-push/deletion, and review security settings. |
| APT repository recommendation and direct-deb limitation | Done | README explains release `.deb` does not update via `apt upgrade`; signed APT repository deferred. |

## Phase 8 — Additional release readiness

| Item | Status | Notes |
|---|---|---|
| Licence compatibility, artwork, trademarks and name collision | Partial | Owner confirmed copyright/GPLv3; source inventory found no bundled third-party application assets. No broad trademark/name search or full dependency licence audit. |
| Unaffiliated-with-distributions notice | Done | README includes Debian/Ubuntu/Canonical/Flathub/Snap Store notice. |
| Privacy/network statement | Done with limitation | README documents local state and direct update endpoint; no sandbox network capture. |
| Man page and recovery instructions | Partial | Man page and basic dpkg recovery documentation exist; no user guide/restore UI. |
| Internationalization and locale formatting | Not done | No translation/accessibility/locale review. |
| Diagnostic export that redacts sensitive data | Not done | Installed-app export is private by default but intentionally contains inventory. |
| Data migration and upgrade path | Not done | No migration testing or versioned state format. |
| Support, rollback, yanking and downgrade policy | Not done | Must be decided before stable release. |
| Reverse-DNS app ID consistency | Partial | Desktop/application ID is `com.appector.appector`; no metainfo/polkit/D-Bus/icon ID to cross-check. |

## Owner decisions and prerequisites

1. Before pushing this local branch, review the commit email privacy finding.
   Changing GitHub email settings protects future commits only; it does not
   edit commits already created. If you do not want the address in the local
   audit branch, explicitly authorize a local-only history rewrite.
2. Protect `main` in Settings → Rules → Rulesets: require pull requests,
   require the CI `test-and-package` status check, and block force pushes and
   branch deletion. GitHub currently reports `main` as unprotected.
3. After the email decision, push `pre-release-audit` and open a PR into
   `main` for code review. This is only a source branch/PR, not a release or
   tag.
4. In Settings → Environments, create/open `public-release`. Add a required
   reviewer, restrict deployment to version tags (`v*`), and confirm GitHub
   pauses a test deployment for approval. Do not create the real release tag
   as a test.
5. Decide whether to add local `.deb` metadata/trust/transaction review before
   including that in-app feature in the first release, or disable local `.deb`
   installation for the initial release.
6. Add GitHub artifact provenance attestation (recommended) and document
   verification, or explicitly accept checksum-only integrity for a
   pre-release.
7. Review secret scanning/push protection, Dependabot and 2FA settings.
8. Complete the Ubuntu 26.04 amd64 install/launch/upgrade/remove/purge tests in
   a disposable VM.
9. Decide whether generic `pkexec` package operations are acceptable for an
   initial release or require a dedicated helper/polkit policy.
10. After the icon commit is on GitHub, open Settings → General → Social
    preview and upload `assets/github-social-preview.png`, then save.

## Click-by-click setup guide

### Protect your commit email before pushing this branch

1. Open GitHub → profile menu → **Settings → Emails**.
2. Enable **Keep my email addresses private** and copy the GitHub-provided
   `...@users.noreply.github.com` address.
3. In this repository checkout, configure future commits:

   ```sh
   git config user.name "Elshad Guliyev"
   git config user.email "PASTE-YOUR-GITHUB-NOREPLY-ADDRESS-HERE"
   ```

4. This setting does not change commits already made. The published `main`
   history already has commits with a personal Gmail-domain author address.
   The local `pre-release-audit` commits would expose their author email if
   pushed unchanged. Choose either:
   - authorize me to rewrite only the unpublished local audit branch to use
     your GitHub noreply address before it is pushed; or
   - accept that the address in those new commits will become public.

   A rewrite of the already-published `main` history is a separate, disruptive
   operation and is not recommended without a deliberate plan.

### Push and review the source changes (not a release)

After deciding the email issue, push the branch and open a pull request:

```sh
git push -u origin pre-release-audit
```

On GitHub, compare `pre-release-audit` into `main`, review the changed-file
list and CI result, then merge the PR. This does **not** create a release.
The PR includes the app icon, GitHub preview image, package/update changes,
and audit documents. The generated `.deb` in `dist/` is local/ignored and is
not committed as source.

### Protect `main`

In **Repository Settings → Rules → Rulesets**, create an active branch
ruleset targeting the default branch `main`:

- require a pull request before merging;
- require at least one approval if you have a second trusted reviewer (if you
  are the sole maintainer, keep the PR requirement and CI check, but do not
  create an approval rule that nobody can satisfy);
- require the CI job/status `test-and-package` once it appears after the first
  PR run;
- block force pushes and branch deletion.

GitHub reported `main` unprotected during this audit. Do not require a status
check by an incorrect display name: open the first PR, wait for CI, and select
the exact successful check name shown by GitHub.

### Add the GitHub social preview

After the image file has merged into `main`:

1. Open the repository → **Settings → General**.
2. Find **Social preview**, choose **Edit**, and upload
   `assets/github-social-preview.png` (1200 × 630).
3. Save and check the repository page/link preview.

The desktop and `.deb` already use the square icon. GitHub's social-preview
image is a separate repository page setting; it cannot be activated merely by
committing the file. This does not change the account/profile avatar.

### Require a human approval for releases

In **Repository Settings → Environments → New environment**, create
`public-release`:

- add a required reviewer (you can approve manually; for independent
  separation, choose another trusted GitHub account);
- configure deployment branches/tags to allow version tags matching `v*`;
- save and review the environment protection settings.

The release workflow uses GitHub's built-in `GITHUB_TOKEN`, with
`contents: write` only on the release job. **Do not create a PAT, do not add
`APPECTOR_RELEASES_TOKEN`, and do not paste any token into chat.** A tag push
starts a real release run; do not push a dummy tag as a gate test. The workflow
must pause at the environment approval before it uploads the release.

### Test the `.deb` safely on the selected system

The target is the same release series as the audit host: **Ubuntu 26.04 LTS,
amd64**. This is the first target, not a verified support claim yet. Use a
fresh disposable VM (GNOME Boxes, VirtualBox or virt-manager), not your
everyday installation. Give it at least 2 CPUs, 4 GB RAM and 25 GB disk, take
a VM snapshot before testing, and copy the locally built `.deb` into it.

Inside the VM:

```sh
dpkg-deb --info ./appector_0.1.0_all.deb
sudo apt install ./appector_0.1.0_all.deb
appector --version
dpkg -L appector | grep -E 'applications/com.appector.appector.desktop|icons/hicolor/.*/com.appector.appector.png'
```

Launch Appector from the Applications grid and confirm the icon appears in
the launcher, window/About dialog and app list. Use the app only for
non-destructive browsing during this basic package test. Then exercise package
removal/purge in the disposable VM:

```sh
sudo apt remove appector
sudo apt install ./appector_0.1.0_all.deb
sudo apt purge appector
sudo dpkg --audit
```

Record any error and the exact distro/version/architecture; remove or revert
the VM after the test. Do not test package-manager cleanup/removal of other
software on your host. A version-to-version upgrade test requires a later
version; GitHub-release `.deb` installs do not update through `apt upgrade`.

### Final decisions to send me

No passwords or tokens are needed. Before I prepare the final release PR,
please tell me:

1. **Local `.deb` install feature**: implement the metadata/trust/transaction
   review first (recommended), or disable/defer that feature in the initial
   release? This is Appector's feature for installing other `.deb` files,
   not the Appector package being released.
2. **Commit email**: authorize rewriting the unpublished local branch to use
   your GitHub noreply address, or accept exposing its current author email?
3. **Release integrity**: add GitHub provenance attestation before the first
   binary (recommended; no signing key/token to provide), or accept
   checksum-only integrity for an explicitly marked pre-release?
4. **Privileged operations**: accept the current system `pkexec`/polkit
   prompts for the initial beta, or require a dedicated restricted helper
   before publication?
5. Once the VM test is done, send the pass/fail result and any exact error
   text. Do not send personal logs with usernames or package inventories.

## Exact commands to tag and publish (do not run without explicit approval)

The following are **instructions only**. They will create and push a release
tag, triggering publication to this public source repository. First merge the
reviewed changes into `main`, switch to that updated branch, and replace
`v0.1.0` if the approved version changes, ensure all blocking items above are
closed, and obtain explicit owner approval.

```sh
git switch main
git pull --ff-only origin main
git status --short
git diff --check
python3 -m unittest discover -s tests -v
./scripts/build-deb.sh
lintian --pedantic dist/appector_0.1.0_all.deb
desktop-file-validate build/appector_0.1.0_all/usr/share/applications/com.appector.appector.desktop
git tag -a v0.1.0 -m "Appector 0.1.0"
git push origin v0.1.0
```

The first `0.x` tag is created as a pre-release. After the tag push, inspect
the Actions run and approve the `public-release` environment only after
reviewing the build/artifacts and release notes. No tag, push, or release was
created during this audit.
