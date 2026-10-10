# Changelog

All notable changes to Appector will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions use the application's Debian-compatible version order.

A release heading may carry a suffix. A heading marked `Prerelease` is
published as a GitHub pre-release, which GitHub excludes from
`/releases/latest`; Appector's in-app update check reads that endpoint and
therefore cannot see pre-releases.

## [Unreleased]

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
- Documented that Ubuntu Software Center cannot upgrade a release `.deb`,
  because release packages are not published through an APT repository, and
  documented `sudo apt install ./<file>.deb` as the upgrade path.

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