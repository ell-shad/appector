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
| Clean-checkout build after audit commits | Pending | Repeat after the package-review and provenance changes are committed. |
| Confirm every user-visible/build/tag version agrees | Done | CLI/About/build/tag gate use `appector/__init__.py`; release tag must be `v0.1.0` for current version. |
| Confirm runtime/build dependency versions and licences | Partial | Source inventory found standard-library and system GI imports, with no vendored application code/assets; review target archive dependency licences and vulnerabilities. |
| Application icon and GitHub branding asset | Partial | Icon is packaged and shown in the README/About dialog; preview artwork is ready, but upload `assets/github-social-preview.png` manually in Settings → General → Social preview. |

## Phase 1 — Secrets and sensitive information

| Item | Status | Notes |
|---|---|---|
| Scan working tree and locally reachable history for high-confidence secret patterns | Partial | Manual pattern scan found no matches; gitleaks/TruffleHog unavailable. |
| Scan all public remote refs/history for high-confidence secret patterns | Partial | Public mirror scan covered 14 commits and 101 blobs with no matches; dedicated scanners still unavailable. |
| Review commit author/committer metadata | In progress | Owner enabled GitHub private email and authorized rewriting unpublished audit-branch commits before push. |
| Review Debian Maintainer identity | Done | Package metadata uses `Elshad Guliyev <ell-shad@users.noreply.github.com>`. |
| Inspect final `.deb` paths, caches, local paths and permissions | Done | No build paths/caches/vendor files/group-world-writable files detected; source `.py` files are included. |
| Review screenshots/icons/sample data for private information | Done for supplied branding | No application screenshot or sample user data is bundled. The chosen icon and generated social-preview image were visually reviewed; no personal information is visible. |
| Ensure logs/exports are user-private | Done | Log `0700` directory/`0600` file; exports `0600` with symlink-target regression test. |
| Identify direct network endpoints and telemetry | Partial | Direct update endpoint documented; package managers contact configured remotes; no telemetry client found by inspection, no sandbox capture. |
| Confirm copyright, GPLv3 intent, and corresponding source availability | Done | Owner confirmed original code and copyright; source repo is public and matching source/build scripts are included at each release tag. |

## Phase 2 — Code quality, cleanup and reliability

| Item | Status | Notes |
|---|---|---|
| Run tests and Python compilation | Done | 25 tests pass; `compileall` passes. |
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
| Dedicated polkit action per privileged action | Deferred by owner | Generic per-operation `pkexec` prompts are accepted for the initial beta; the GUI remains unprivileged. |
| Strict package/app ID validation and argument handling | Partial | Package/app IDs validated; batched privileged script quotes package IDs; no comprehensive hostile-input fuzz suite. |
| TOCTOU/path symlink protections for manual removal | Mitigated | Privileged manual-app file deletion is blocked; no dedicated race-resistant helper exists. |
| Preview equals executed transaction for all managers | Not done | APT simulations exist; Snap/Flatpak/file-install behavior is not fully previewed/reverified. |
| APT autoremove/purge and Flatpak cleanup confirmation | Partial | Preview/confirmation exists; execution was not run. |
| Residual scanner uses safe allow-list/quarantine/restore index | Not applicable to implemented scope | Only dpkg `rc` conffiles are surfaced; backups precede purge, but no restore UI. No general filesystem residual deletion engine. |
| Local `.deb` metadata/trust/architecture/dependency/transaction preview | Implemented | In-app local-package installation stages an immutable-to-normal-writes copy, displays metadata/hash and APT simulation, blocks if the preview fails or changes, warns that publisher identity is not verified and maintainer scripts run with administrator privileges. Manual install integration tests remain pending. |
| `.flatpakref`/AppImage hostile input and architecture tests | Not done | Requires isolated integration tests. |
| Update checker uses HTTPS, User-Agent, timeouts, safe URL/version parsing | Done | Unit tests cover Debian ordering and unsafe responses/URL. |
| Update checker cache/ETag/daily limit/disable setting | Not done | Current check is manual-only; no background checks. |
| Update checker only opens page, no auto-install | Done | Release URL is validated; app never downloads/executes updates. |
| Download integrity signature/attestation | Workflow configured | Release workflow requests GitHub build-provenance attestation and documents `gh attestation verify`; no attestation exists until a tagged release run succeeds. SHA256SUMS is not separately signed. |

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
| Trust cues for local/third-party packages | Implemented | Review dialog displays local `.deb` metadata, file hash and simulated APT transaction; explicitly states that publisher signatures are not verified and maintainer scripts may run as root. |
| Confirmation on every destructive maintenance operation | Partial | APT leftovers and Flatpak runtime cleanup have preview/confirmation; all paths not integration-tested. |
| Batch per-file metadata/warnings and failure policy | Not done | Mixed queue exists; per-file trust review and end-to-end policy need implementation/testing. |
| AppImage location, safe delete-source, Flatpak scope/remote | Partial | AppImage warns it is executable and not sandboxed; full UX not tested. |
| Tooltips, plain errors, accessibility, translations | Partial | Some tooltips/errors exist; screen-reader/localization/overflow review pending. |

## Phase 6 — Debian package

| Item | Status | Notes |
|---|---|---|
| Accurate control metadata and dependencies | Done | Lintian clean; package maintainer metadata matches the owner's confirmed identity. |
| DEP-5 copyright coverage and licence match | Done | Owner confirmed copyright and GNU GPL version 3 for original application code. |
| Maintainer scripts safe/idempotent | Not applicable | No postinst/prerm/postrm scripts are packaged. |
| Desktop file validation and standard paths/modes | Done | Validated; app icon is installed in hicolor sizes and referenced by desktop entry; no unsafe write bits found. |
| AppStream, icons, MIME types, polkit, D-Bus and systemd artifacts | Partial | Hicolor application icons and desktop entry are packaged; AppStream/MIME and dedicated polkit/helper metadata are absent. Direct GitHub `.deb` distribution can proceed without AppStream, but not without resolving release blockers. |
| Lintian `--pedantic` | Done | Clean on Ubuntu 26.04. |
| Reproducible build | Done on one host | Two same-host builds had identical SHA-256; clean chroot/container and cross-architecture builds not proven. |
| Install/upgrade/remove/purge test | Needs result confirmation | Owner reports successful app testing without issues; confirm whether package install, remove and purge were tested in a disposable Ubuntu VM. |
| Package size | Pending final build | Measure after local-package review changes; previous icon-enabled package was about 365 KiB. |

## Phase 7 — GitHub repository and release setup

| Item | Status | Notes |
|---|---|---|
| README install/update/privacy/known limitations/recovery | Partial | Updated for public source and same-repository releases; a release link will resolve after the first approved tag. |
| SECURITY, CONTRIBUTING, CHANGELOG, issue/PR templates | Done as drafts | Verify repository security settings and public contact before publishing. |
| CI tests/build/package validation | Done as workflow configuration | Workflow YAML parses; CI itself was not run. |
| Pin actions and set least permissions | Done | Actions are commit-pinned; build has read/attestation permissions, while only the environment-gated publish job has `contents: write`. CI is read-only. |
| Release assets `*.deb` and `SHA256SUMS` | Partial | The workflow builds and validates release assets in a separate job, then publishes the reviewed run's artifact through `GITHUB_TOKEN`; no release upload was run. |
| Detached signature/provenance | Workflow configured | Build job creates a GitHub provenance attestation and publishes testable assets before the environment-gated publish job; verify after first release. Checksums are not separately signed. |
| Protected `public-release` environment | Partial — release blocker | Environment exists with verified `v*` tag restriction, but no required reviewer is configured and administrator bypass is enabled. |
| Branch protection, secret scanning/push protection, Dependabot, 2FA | Not done | GitHub reports zero rulesets and `main` unprotected; create the ruleset described below and review account/repository security settings. |
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

1. The owner enabled GitHub email privacy and authorized rewriting the
   unpublished local branch to noreply before push. That rewrite is being
   completed; published `main` history is not being rewritten.
2. Protect `main` in Settings → Rules → Rulesets. GitHub's public API currently
   shows no rulesets and reports `main` as unprotected. The click-by-click
   setup and timing for selecting CI are below.
3. Once the local history rewrite and final checks are complete, push
   `pre-release-audit` and open a PR into `main` for code review. This is only
   a source branch/PR, not a release or tag; because the repository is public,
   pushed branch contents and PR changes are visible to everyone.
4. In Settings → Environments → `public-release`, add a required reviewer and
   disable administrator bypass. The `v*` tag policy is already confirmed;
   the release build will finish and create reviewable assets before the
   publish job waits for approval.
5. Review secret scanning/push protection, Dependabot and 2FA settings.
6. Complete/confirm the Ubuntu 26.04 amd64 install/launch/remove/purge tests
    in a disposable VM. The owner reports testing the app without issues; the
    exact package lifecycle steps/results still need confirmation.
7. The owner accepted the existing authorization model for an initial beta:
    Appector's GUI remains unprivileged and uses the system `pkexec` prompt for
    each privileged operation. A dedicated helper is deferred.
8. After the icon commit is on GitHub, open Settings → General → Social
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

4. The owner has authorized a rewrite of the unpublished `pre-release-audit`
   branch. I will rewrite only that local branch to use the account's
   GitHub-provided noreply identity before pushing it. The already-published
   `main` history is untouched; changing it would be a separate and disruptive
   operation.

### Push and review the source changes (not a release)

After the local rewrite and final checks, push the branch and open a pull
request:

```sh
git push -u origin pre-release-audit
```

On GitHub, compare `pre-release-audit` into `main`, review the changed-file
list and CI result, then merge the PR. This does **not** create a release.
The PR includes the app icon, GitHub preview image, package/update changes,
and audit documents. The generated `.deb` in `dist/` is local/ignored and is
not committed as source.

### Protect `main`

GitHub currently reports zero repository rulesets and `main` as unprotected.
Create the ruleset in **Repository → Settings → Rules → Rulesets → New branch
ruleset**:

1. Name it `Protect main`, leave enforcement as **Active**, and target only
   `main` (choose the default branch or add `refs/heads/main`).
2. Do not add bypass actors.
3. Under **Branch rules**, enable **Restrict deletions**, **Block force
   pushes**, **Require a pull request before merging**, and **Require status
   checks to pass**.
4. Do not require approvals if you are the sole maintainer; the pull request
   requirement still prevents direct pushes. If you have a second trusted
   reviewer, require one approval.
5. Save the ruleset. After the branch is pushed and the first PR CI run
   completes, edit the ruleset and add the exact successful required-check
   name displayed by GitHub (the workflow job is `test-and-package`). Require
   the branch to be up to date before merging if GitHub offers that option.

Do not guess the check's display name or merge before adding it; GitHub may
display it with a workflow prefix.

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

The `public-release` environment already exists, and its tag policy was
verified through GitHub's public API as `v*`. The API currently shows **no
required reviewer**, and administrators can bypass protection. Open
**Repository Settings → Environments → public-release → Edit protection
rules**:

1. Add yourself as a required reviewer (or another trusted account).
2. Disable administrator bypass if GitHub presents **Allow administrators to
   bypass configured protection rules**.
3. Keep the deployment policy limited to tags matching `v*`.
4. Save. The environment should show both the reviewer and the `v*` tag
   restriction before release publication is considered protected.

The build job validates, attests and uploads assets first. The publish job
then waits for environment approval and uses GitHub's built-in `GITHUB_TOKEN`,
with `contents: write` only on that job. **Do not create a PAT, do not add
`APPECTOR_RELEASES_TOKEN`, and do not paste any token into chat.** A tag push
starts a real release run; do not push a dummy tag as a gate test. Review the
build artifact and notes before approving the publish job.

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

### Remaining owner tasks

No design choices remain for the local `.deb` review, provenance workflow, or
initial beta authorization model. The owner selected the review and
attestation options and accepted per-operation `pkexec` authorization; the
GUI remains unprivileged.

1. Create the `main` ruleset as described above. The first CI result does not
   exist on GitHub yet; after the branch PR triggers CI, select the exact
   passing check before merging.
2. Add a required reviewer to `public-release` and disable administrator
   bypass. Its `v*` tag restriction is already verified.
3. Confirm whether your successful app test also covered installation of the
   built `.deb` in a disposable Ubuntu 26.04 amd64 VM, launch/icon display,
   removal and purge. If any step was not covered, run only the missing steps
   in a disposable VM and send me pass/fail plus exact errors. No personal logs,
   passwords or tokens are needed.
4. The social-preview image's public setting cannot be read through the GitHub
   API. If it is not already visible on the repository page, after merging
   upload `assets/github-social-preview.png` under Settings → General →
   Social preview.

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
