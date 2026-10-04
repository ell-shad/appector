# Appector pre-release test matrix

Audit host: Ubuntu 26.04.1 LTS, amd64, Python 3.14.4.
Package version: `0.1.0`; package architecture: `all`.
No package was installed or removed on the host. Destructive/integration
scenarios are marked **Not run** because no disposable VM/container runtime
was available.

## Reproduction commands

From the repository root:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q appector
python3 run.py --help
python3 run.py --version
sh -n scripts/build-deb.sh
./scripts/build-deb.sh
lintian --pedantic dist/appector_0.1.0_all.deb
desktop-file-validate build/appector_0.1.0_all/usr/share/applications/com.appector.appector.desktop
dpkg-deb --info dist/appector_0.1.0_all.deb
dpkg-deb --contents dist/appector_0.1.0_all.deb
git diff --check
```

The CI workflow additionally runs ShellCheck; it was unavailable on the audit
host. The release workflow uses Ubuntu 24.04, but was not executed during this
audit.

## Executed checks

| Scenario | Environment | Result | Evidence/notes |
|---|---|---|---|
| Safety, package-review and updater unit suite | Ubuntu 26.04, Python 3.14.4 | **Pass: 25 tests** | `python3 -m unittest discover -s tests -v`. |
| Application module compilation | Same | **Pass** | `python3 -m compileall -q appector`. |
| GTK module import / CLI help | Same | **Pass** | `python3 run.py --help` imports the GUI modules and exits with help. |
| CLI version/source consistency | Same | **Pass: `appector 0.1.0`** | `python3 run.py --version`; code version is the source of truth. |
| Root GUI guard | Unit test, mocked application boundary | **Pass** | Test asserts root is refused before GUI startup; no GUI was run as root. |
| Self-removal and protected APT package guard | Unit tests | **Pass** | Tests cover Appector/legacy package and critical package/prefix refusal. |
| Privileged manual-path refusal | Unit test with temporary paths | **Pass** | Does not call `pkexec`; outside sentinel remains unchanged; no privileged deletion. |
| Private activity log and symlink directory handling | Temporary home/state paths | **Pass** | Tests assert private mode and refusal of a symlinked state directory. |
| Private export and symlink target safety | Temporary destination/target | **Pass** | Export is mode `0600`; destination symlink is replaced; target contents remain unchanged. |
| APT removal simulation locale | Mocked command invocation | **Pass** | Test verifies `LC_ALL=C` for parsed APT output. |
| Debian version ordering | `dpkg --compare-versions` via tests | **Pass** | Covers epoch, `+` suffix, tilde prerelease, Debian revision ordering and equal version. |
| Update API invalid data / URL / 404 | Mocked API responses | **Pass** | Malformed response, unsafe URL and no stable release handled without crash. |
| Local `.deb` review/staging | Mocked tests and built Appector package | **Pass: 9 tests + smoke check** | Metadata/hash display, private staging, incompatible architecture, simulation failure, symlink rejection, changed source, Trash safety, mandatory review and transaction-change block. The generated Appector `.deb` passed real metadata inspection and APT simulation; no package was installed or changed. |
| Duplicate GUI method check | Source-integrity test | **Pass** | No duplicate methods remain in inspected GUI classes. |
| Workflow YAML syntax | Local PyYAML parser | **Pass** | Both `.github/workflows/*.yml` parse. |
| High-confidence secret patterns in public Git history | Public mirror of `ell-shad/appector` | **Pass: 0 matches** | Scanned all refs visible at audit time: 14 commits and 101 unique blobs. Manual patterns only; gitleaks/TruffleHog were unavailable. |
| Public default-branch protection | GitHub public API | **Fail / not configured** | Repository has zero rulesets and `main` is reported as unprotected. |
| Release environment tag restriction | GitHub public API | **Pass: `v*`** | `public-release` exists and permits tags matching `v*`; required reviewer is absent and administrators can bypass protection. |
| GitHub artifact provenance workflow | Local workflow configuration | **Configured; not executed** | Release job requests OIDC/attestations permission and pins `actions/attest-build-provenance`; verify an attestation after the first approved release. |
| Commit author email privacy | Published history / unpublished branch | **Pass for unpublished branch** | All 15 unpublished commits now use GitHub noreply; previously published `main` history was left untouched. |
| Shell script syntax | POSIX shell parser | **Pass** | `sh -n scripts/build-deb.sh`. |
| First deterministic `.deb` build | Ubuntu 26.04 amd64 | **Pass** | Produces `dist/appector_0.1.0_all.deb`. |
| Repeated deterministic `.deb` build | Same host/checkout | **Pass** | Repeated builds had identical SHA-256; the clean-worktree package was byte-identical. Final package size: 375,874 bytes. |
| Clean-checkout tests/build | Isolated worktree at rewritten branch head | **Pass** | All 25 tests passed; package built, passed `lintian --pedantic` and desktop validation, and matched the worktree package byte-for-byte. |
| Debian package metadata / file list | `dpkg-deb` | **Pass** | Architecture `all`, dependency metadata, launcher, GTK desktop entry, man page and copyright present. |
| Runtime dependency/source inventory | Source imports and Ubuntu package copyright notices | **Partial** | App imports use standard library and system GI bindings; no vendored app code/assets. PyGObject/GTK/libadwaita copyright notices list LGPL terms; full transitive SBOM/licence review remains. |
| Lintian | lintian 2.129.0, `--pedantic` | **Pass: no diagnostics** | `lintian --pedantic dist/appector_0.1.0_all.deb`. |
| Desktop entry validation | desktop-file-utils 0.28 | **Pass** | `desktop-file-validate` on staged desktop file. |
| App icon package entries | Debian package contents and image dimensions | **Pass** | Desktop file references `com.appector.appector`; hicolor PNGs are packaged at 16, 24, 32, 48, 64, 128, 256 and 512 px; README/About/window use same icon ID. |
| GitHub social-preview image | Generated local PNG | **Ready to upload** | `assets/github-social-preview.png` is 1200x630 and visually reviewed; upload manually in repository Settings → General → Social preview after merging the asset commit. |
| Package contents/privacy/modes | Extracted Debian package | **Pass: 53 paths / 24 regular files** | Includes 8 hicolor icons; no `.git`, Python caches, embedded local paths or group/world-writable files. |
| Git whitespace check | Local branch | **Pass** | `git diff --check`. |

## Platform matrix

| Distribution / architecture | Build | Install/launch | Package lifecycle | Result |
|---|---|---|---|---|
| Ubuntu 26.04 amd64 (initial target/audit host) | `.deb` built twice | Not run | Not run | Build/static validation only; disposable install/lifecycle test remains. |
| Debian stable amd64 | Not claimed/tested | Not run | Not run | Future support requires a separate disposable test. |
| Other Ubuntu releases, Mint, Pop!_OS | Not claimed/tested | Not run | Not run | Future support requires per-release compatibility/install testing. |
| Raspberry Pi OS / arm64 | Not claimed/tested | Not run | Not run | `Architecture: all` is not compatibility evidence. |

## Required integration and failure scenarios (not run)

These scenarios require a disposable, snapshot/recreate-capable VM or
container. Do not run them against a user's host.

| Scenario | Result | Required environment / notes |
|---|---|---|
| Fresh `.deb` install, launch, initial scan, refresh, search/filter/sort | **Not run** | Per supported distro; package was not installed on host. |
| APT, Snap, Flatpak, AppImage and manual removal | **Not run** | Disposable machine with labelled test apps and snapshots. |
| APT purge, autoremove preview/execution and residual-config purge | **Not run** | Snapshot before every destructive operation; verify backup and recovery. |
| Flatpak cleanup / install / remove with and without data | **Not run** | User and system scopes, configured test remote. |
| Snap install/remove | **Not run** | Snap-enabled disposable distro; unavailable host coverage. |
| AppImage integrate/remove/delete-source | **Not run** | Safe sample file and disposable home; ensure delete-source uses Trash only after success. |
| Batch mixed install (valid/invalid), failure policy, cancel/retry | **Not run** | Isolated test packages and remotes; inspect each per-file warning. |
| `.deb` missing dependencies, downgrade, upgrade, already-installed, duplicate | **Not run** | Review UI implemented; exercise representative scenarios in the disposable target VM before release. |
| Hostile/malformed `.deb`, `.flatpakref`, AppImage and Unicode/newline paths | **Not run** | Fuzzed samples in a disposable environment; no AppImage execution for metadata. |
| Dpkg lock contention / unattended-upgrades-like activity | **Not run** | Disposable VM only; verify actionable error and retry behavior. |
| Kill application/helper, fill disk, disconnect network, interrupt package install | **Not run** | Disposable VM with snapshot and recovery plan. |
| `dpkg --audit` / `dpkg --configure -a` interrupted-install recovery | **Not run** | Disposable VM; no host package database modifications. |
| Conffiles, services, systemd, alternatives, DKMS, third-party postinst, metapackages, multiarch, held packages | **Not run** | Representative packages in snapshots; review transaction previews. |
| Same app installed via APT, Snap and Flatpak | **Not run** | Validate duplicate classification and removal scope in disposable machine. |
| Residual cleanup labelled precision dataset | **Not run** | General filesystem residual cleanup is not implemented; current scanner only detects dpkg `rc` configs. |
| Appector install/upgrade/remove/purge/reinstall lifecycle | **Not run** | Verify no orphaned service, file, state, or desktop entry; self-removal refusal is unit-tested only. |
| Keyboard, Orca, contrast, large text, HiDPI, themes, narrow UI, Wayland/X11 | **Not run** | GUI visual/accessibility audit pending. |
| 1,500+ package performance, scan responsiveness and memory | **Not run** | Record first-scan time and peak memory on supported target systems. |
| Offline/rate-limited GitHub update API and live release page | **Not run** | Mock tests cover malformed data and 404; the public source repository currently has no published application release. |
| APT install with missing dependency and maintainer-script prompts | **Not run** | Must first decide/implement local package metadata and transaction confirmation. |
| Public release upload / rollback / yank | **Not run** | Explicitly not run; requires a protected environment approval and explicit publication approval. |

## Tool coverage and limitations

ShellCheck, Ruff, mypy, pytest, gitleaks and TruffleHog were unavailable on the
host and were not installed into the host environment. The public history was
scanned with manual high-confidence patterns, not a dedicated secret scanner.
CI is configured to install ShellCheck. Docker,
Podman and LXD were unavailable and no QEMU guest image was configured, so no
disposable integration environment could be created. AppStream metainfo is
absent, so no AppStream validation result is available. Test coverage
percentage was not measured.
