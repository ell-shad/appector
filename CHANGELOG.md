# Changelog

All notable changes to Appector will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions use the application's Debian-compatible version order.

A release heading may carry a suffix. A heading marked `Prerelease` is
published as a GitHub pre-release, which GitHub excludes from
`/releases/latest`; Appector's in-app update check reads that endpoint and
therefore cannot see pre-releases.

## [Unreleased]

### Added

- The table can show an optional Installed column, toggled from the View
  menu and remembered in `ui.json`.
- The primary menu has a Quit item backed by an application action.
- A spinner in the header runs for the whole scan, from the refresh request
  until results are shown or the scan fails.

### Changed

- The Version column sorts with Debian version rules instead of plain text,
  so `1.10` follows `1.9` and epochs, revisions, and `~` pre-releases order
  correctly.

### Fixed

- The APT repository no longer expires between releases. Its metadata now
  carries a 30-day `Valid-Until` and a scheduled workflow re-signs and
  republishes it weekly, so `apt update` stops failing with "Release file …
  is expired" (and the "Unable to locate package appector" that follows it)
  whenever releases are more than a week apart.
- The APT pool now really carries the previous releases forward, keeping the
  newest five `.deb`s published, instead of being rebuilt from an empty pool
  because the workflow could not read the previously published archive.
- Versions that were only ever published as GitHub Release assets (0.2.0,
  0.2.1 and 0.3.0; 0.1.0 is a pre-release and stays out of the stable
  channel) are backfilled into the APT pool and listed in the
  index, so a version pin or an old package list no longer 404s. The backfill
  runs both when a release is published and on the weekly refresh; when a
  release is being published, an old release whose assets fail verification
  is skipped with a warning instead of blocking the new one.

## [0.3.1] - 2026-11-15

### Fixed

- The "Install apps" dialog opens again (Ctrl+O, the main menu item, and
  every file-open route). The dialog builder extracted in 0.3.0 never
  returned the dialog it built, and its presenter then used an argument
  form that a plain `Gtk.Dialog` does not accept, so the whole install
  flow crashed before anything appeared.
- "Remove marked" works again (button, `Ctrl+Delete`, and the context
  menu entry). The batch confirmation dialog crashed while being
  constructed: `set_show_title_buttons()` does not exist on
  `Adw.HeaderBar`, and the asynchronous disk-estimate row started before
  the confirm button existed. The app details window had the same header
  crash and opens again.
- A new UI smoke test constructs the real dialogs, and CI plus the
  release checks run it under Xvfb with the system Python, so broken
  dialog construction now fails the build instead of silently
  disabling features.

## [0.2.1] - 2026-10-10

### Added

- Appector is now published to a signed APT repository, so `apt upgrade` and
  Ubuntu Software Center can offer Appector updates. Adding the repository is
  a one-time setup; see "Installing from the APT repository" in the README.
  The repository is signed with a dedicated OpenPGP archive key whose public
  half is committed to the repository and can be reviewed in a pull request.
  Releases are published from GitHub Pages by a workflow that refuses to
  publish an unsigned repository, refuses to publish a pre-release into the
  stable channel, and verifies the signature against the committed public key
  before deploying.
- `scripts/generate-apt-signing-key.sh` creates the archive keypair. It runs
  in a throwaway keyring, so it cannot disturb existing personal keys, and
  it never writes the private key into the working tree.
- `scripts/build-apt-repo.sh` builds the repository layout, the `Packages`
  index, the `Release` metadata, and both the clearsigned `InRelease` and the
  detached `Release.gpg` signature.
- `scripts/release-notes.py` renders release notes and decides pre-release
  status from the changelog heading. The GitHub Release and APT repository
  workflows share it so they cannot disagree about whether a tag is a
  pre-release, which is what keeps a pre-release out of the stable channel.

### Fixed

- Installing a reviewed `.deb` no longer fails with a bare "no such file"
  when the staging area disappears between the review and the install.
  Staging moved from `/tmp` to a per-user cache directory that system-wide
  tmpfiles cleaners do not touch, and a vanished staged copy is rebuilt from
  the original file only when that file still hashes to the reviewed value.
  A changed or missing original is still refused, so the review continues to
  guarantee that only reviewed bytes are installed.
- The Appector update dialog now names the exact package file and shows the
  `sudo apt install ./appector_<version>_all.deb` command, so upgrading no
  longer depends on the user knowing the procedure.

### Changed

- Updated screenshots for the new sidebar scope selector, backup browser, and
  menu layout.

## [0.2.0] - 2026-10-10

### Added

- Architecture decision records in `docs/decisions/` covering the residual
  purge gates and the module split.
- Keyboard shortcuts for previously unbound actions: Cleanup and residuals
  (`Ctrl+Shift+K`), Appector updates (`Ctrl+Shift+U`), export JSON
  (`Ctrl+Shift+J`), and the shortcuts window (`Ctrl+?`).
- Scope selector shortcuts: `Ctrl+1` all items, `Ctrl+2` applications only,
  `Ctrl+3` system items only.
- Unified sidebar scope selector (All / Applications / System items) replacing
  two conflicting visibility toggles, plus tooltips and clearer selection
  buttons.
- Reorganized primary menu into Install, Maintenance, Updates, Export, Tools
  and About.
- Header count label showing visible vs total items, and saved window size is
  re-applied when the window is first shown.
- Per-package deselection step before purging residual APT configurations.
- Read-only purge-backup browser showing manifests, recorded paths, and
  hashes; Appector still performs no automatic restore.
- `--` option terminator on APT/DPKG invocations that take package lists.
- Staged removal marks now persist between sessions in a private
  `marked.json`; removal still requires a fresh preview and confirmation.
- Sidebar section spacing, tooltips, and icon-labelled selection buttons.
- Residual-purge preview now shows conffile counts, sample file paths, APT
  purge simulation output, critical-package warnings, and existing backup
  count.
- Cleanup dialog shows purge-backup retention state, file counts, and
  offers prune-old-backups.
- Grid view now shows version and duplicate badges to match the table view.
- New `tests/test_residuals.py` covering residual validation, simulation
  gating, post-backup re-verification, conffile parsing, destructive-path
  guards, mark persistence, and backup handling.

### Changed

- `appector/actions.py` split into focused modules: `policy.py` (safety
  decisions), `_proc.py` (command execution and logging), and `residual.py`
  (APT residual purge). See `docs/decisions/0002`.
- Manual and AppImage removal now requires, before any unlink: an
  allowlisted root, at least one level below that root, ownership by the
  current user, and a regular file or symlink.
- Icon-only header controls and the search entry carry explicit accessible
  names; the empty state offers a "Clear filters" action when a filter is
  active.
- Release status (pre-release or full release) is now taken from the
  changelog heading instead of the version number, so a release intended for
  the in-app update check is not silently hidden as a pre-release.

### Fixed

- Fixed a `TypeError` on the window's `map` handler that silently prevented
  the saved window size from ever being applied.
- Every action with an accelerator is now listed in the shortcuts window,
  generated from a single shortcut table so the help cannot drift from the
  real bindings.
- Backup wording no longer implies missing data: the cleanup dialog reports
  backup count and copied-file count separately, and a backup with no files
  explains that dpkg registered no conffiles for those packages.
- Removed a duplicated app scan at window startup that launched two
  concurrent scan threads.
- Restored `install_flatpak`, which had been merged into the neighbouring
  `simulate_leftover_purge` body and left unreachable after its return.
- Fixed the shared conffile pattern so size estimation matches every record in
  multi-line dpkg output instead of none.
- Scope filtering and "hidden by filter" mark cleanup now agree, so marks are
  no longer dropped for entries that are still visible.
- Flatpak unused-runtime preview fails instead of showing a possibly wrong
  preview when the confirmation prompt is not recognised.
- Removed unreachable leftover-purge and sidebar helpers that were never
  wired to a widget, plus superseded confirm-response handlers.
- APT package lists are passed after `--` so a package name can never be
  parsed as an option.
- Residual purge is gated by an APT `--simulate purge` check, aborts when
  the simulation reports packages beyond the selection, and re-verifies dpkg
  rc state after backup to close the reinstall-then-purge TOCTOU window.
- Residual backup and size estimation share one conffile parser (including
  `obsolete` entries), validate package names, use `--` plus `LC_ALL=C` for
  dpkg queries, and enforce per-batch file-count (2000) and size (100 MiB)
  caps with automatic pruning to the newest 10 backups.
- Manual/AppImage removal now resolves parent symlinks, refuses non-regular
  files, and treats symlink links as link-only deletions.
- Debian package install re-hashes staged files after the second APT
  simulation to close the swap-between-simulation-and-install window.
- Leftover scanner uses a timeout, `LC_ALL=C`, and package-name validation.
- Prevent removal of Appector and the legacy `app-manager` package.
- Activity logs and staged marks are written with user-only permissions.
- Update versions are compared using Debian's version rules and release URLs
  are validated.
- Stopped tracking `appector/__pycache__/` bytecode and third-party agent
  tooling in git.

## [0.1.0]

### Added

- Initial GTK desktop application package.
- Manual check for stable updates through GitHub Releases.
- Show local Debian package metadata, file hash and APT transaction preview
  before installation.
- Generate GitHub build-provenance attestations for release Debian packages.
## [0.3.0] - 2026-11-14 — security and data-safety remediation (review round 1)

### Security
- **S2**: residual APT identities are now `name:arch`; purges are refused
  when another architecture instance of the same package is installed
  (multiarch protection), before and after the conffile backup.
- **S3**: plain-HTTP `.flatpakref` URLs and plain-HTTP repositories declared
  inside ref files are refused; `.flatpakref` files are reviewed before
  installation and re-checked for changes; app-ID installs pass the Flathub
  remote explicitly; the `Url=` fallback install was removed.
- **S4**: every removal/autoremove/upgrade execution is bound to the plan
  the user confirmed — re-simulations that grew, critical packages, or
  update transactions containing removals are refused. Autoremove and
  updates previously ran with no gate at all.
- **S7**: unified process runner — non-UTF-8 output no longer fails
  exit-0 commands, timeouts kill whole process groups (with a root-side
  `timeout` under pkexec), output is bounded, cancellation is supported,
  `LC_ALL=C` is forced, pkexec argv carries the non-interactive settings.
  The activity log escapes newlines (no forged log lines), uses timezone
  aware timestamps, and rotates at 1 MiB × 3.
- **S8**: standalone AppImage removal goes through the same
  allowlist/ownership/regular-file validation as managed removals;
  launcher ownership uses exact desktop-key values; symlink-parent escapes
  are resolved and refused.
- **S9**: conffile backup parents are created without following symlinks;
  backups for purges that never start are discarded; pruning deletes
  incomplete backups first and keeps the newest 10 *complete* ones;
  root-only conffiles can be skipped behind an explicit, logged choice.
- **S10**: CSV export escapes formula-injection cells; exports are atomic,
  0600, and include exec/details/duplicate columns.

### Fixed
- **F1** Flatpak install dates are used instead of discarded; **F2** real
  Flatpak display names and ID-based duplicate identity (no more
  "Client"/"client" false duplicates); **F3** `normalize_exec` handles
  quoted paths and wrapper commands; **F4** APT installed-state uses
  `${db:Status-Abbrev}` `ii` and `name:arch` keys; **F5** history.log
  parsing handles `(version, automatic)` and falls back to
  `/var/lib/dpkg/info/<pkg>.list` mtimes; **F7** the desktop parser honours
  `[Desktop Entry]` group boundaries, TryExec, ShowIn and Type;
  **F15/F16** shared helpers have single definitions (enforced by test);
  batch-removal progress reaches 100% and an APT backup failure no longer
  aborts Snap/Flatpak removals.
- **F6**: all scanner subprocesses carry timeouts, dpkg ownership lookups
  are batched, scans run in parallel, and each source reports status
  (ok / tool-missing / failed); marks are no longer pruned for sources
  that failed to scan.
- **F8**: APT source labels work on Debian/Mint/Pop!_OS/Raspbian; PPAs are
  matched on their launchpad host.
- **F10**: manual scanner marks CLI-only executables as non-GUI, uses
  common per-user tool directories, derives removal paths from the
  resolved Exec target only, and classifies Chrome/Steam/Wine launchers.
- **F11**: Flatpak installation scope comes from the recorded installation
  name; "already installed" detection uses `flatpak info` exit codes.
- **F14**: XDG_STATE_HOME/XDG_CACHE_HOME are honoured with one-time
  migration from the legacy `app-manager` directories (old data kept).

### Removed
- Duplicate `_parse_apt_simulation_removed_packages`,
  `_format_package_list`, `_deduplicate_package_ids`,
  `_split_safe_package_ids` and `SAFE_PACKAGE_ID_RE` definitions; the
  `Url=` flatpakref fallback; the substring APT "high risk" heuristic.

### Changed
- `appector --version` and the root-refusal check no longer require GTK;
  files can be passed on the command line (`appector foo.deb`) and are
  routed into the install flow (HANDLES_OPEN).

### Privileged helper and UI correctness (phases 2–3)

### Added
- **ADR-0003 and the privileged helper**: `appector-helper` (installed
  root-owned at `/usr/lib/appector/appector-helper`) with a per-operation
  polkit policy; strict JSON request schema, root-side re-validation,
  nonce-echoing JSON-lines events; `appector/helper_client.py` fails fast
  when the helper or pkexec is missing. .deb installs, APT
  remove/purge/autoremove/upgrade, Snap and system-Flatpak removals, and
  the residual purge (including its conffile backup) all run through it.
- `appector/ui/` package: `run_async` worker plumbing (errors surfaced,
  tracebacks logged, controls always re-enabled), the cancellable
  `TaskProgressDialog`, and the scrollable `ConfirmPlanDialog` whose
  "also removed" list is never truncated.

### Changed
- Batch confirmation shows the full dependency cascade and exact file
  paths, computes the disk estimate in a worker, and disables Remove while
  the plan is loading or blocked; Cancel has the default focus.
- `Ctrl+Delete` replaces the bare `Delete` shortcut; mark-all/remove-marked
  ignore activation while a text entry has focus.
- Cancelling an export file chooser no longer reports an error; search
  text is markup-escaped.

### Removed
- Every `pkexec sh -c` call, the `APP_MANAGER_*` text protocol, and the
  silent unprivileged fallbacks when pkexec is unavailable.

## Unreleased — review milestones completed (phases 3–5 continuation)

### Added
- Scan-status banner naming failed or missing sources, with a per-source
  details dialog (F6/U8); PACKAGE SOURCE filter chips with live counts (U9);
  a redesigned details card with risk badge and duplicate collisions (U9).
- Structured .deb review: maintainer scripts, setuid files, /etc/apt paths
  and systemd units extracted per package and shown in a structured dialog
  (F13/U10); .flatpakref review dialog.
- AppImage install hardening: type-1/type-2 magic validation, content-hash
  identity, atomic copy and launcher writes (F9).
- i18n scaffolding (gettext, APECTOR_LOCALEDIR); German desktop strings.

### Changed
- The update check refreshes APT package lists first and reports
  /var/run/reboot-required and needrestart output (F12).
- The residual-purge confirmation simulates off-thread and shows the full
  plan on the confirm dialog (U1/U2).
- libadwaita 1.5 is the declared minimum (Depends + metainfo) (U4).
- The APT repository publishes the attested release artifact (never
  rebuilt), retains the newest three pool releases, and signs metadata
  with Valid-Until and Acquire-By-Hash (S12).

### Fixed
- dpkg ownership lookups crashed every call (stderr mixed with
  capture_output) and parsed `dpkg -S` output inverted — APT desktop
  files were listed as Manual entries; verified against the real
  dpkg-query.
- Search input no longer refilters on every keystroke (debounced) and the
  list keeps selection and scroll position across rescans (U8).
