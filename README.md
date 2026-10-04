# Appector

Appector is a GTK 4/libadwaita desktop app for inspecting installed software
and performing package-management tasks on Linux. It can list APT, Snap,
Flatpak, AppImage, and detected manual installs; install local `.deb`,
`.flatpakref`, and AppImage files; and preview selected cleanup operations.

Appector is not affiliated with or endorsed by Debian, Ubuntu, Canonical,
Flathub, or the Snap Store.

## Download and install

The source repository is private. User-facing binary releases are intended to
be published in the separate public
[`ell-shad/appector-releases`](https://github.com/ell-shad/appector-releases)
repository. That repository currently returns 404 and must be created before
publishing; until then, there is no public release to download.

The `.deb` includes Appector's Python source files. Keeping the Git repository
private does not keep the application's implementation private, and the
license's corresponding-source requirements must be met for distributed
packages.

After a release is available, download `appector_<version>_all.deb` and
`SHA256SUMS` from its GitHub Releases page. Verify the checksum in the same
directory, then install:

```bash
sha256sum --check SHA256SUMS
sudo apt install ./appector_<version>_all.deb
```

Launch **Appector** from the applications menu or run `appector`. The package
declares GTK 4, libadwaita, PyGObject, and GdkPixbuf introspection packages as
system dependencies; `apt` installs these. Optional package managers such as
Snap and Flatpak are not required. `Architecture: all` means the Python files
are architecture-independent; it does not mean every target architecture has
been tested. Do not run the GUI as root; privileged operations use system
authorization prompts when available. Removal of manual app files that require
administrator privileges is intentionally blocked until a race-resistant
privileged helper is implemented.

`appector --help` shows the available launcher options and `appector --version`
prints the installed version.

To remove Appector:

```bash
sudo apt remove appector
```

To remove its system package configuration as well:

```bash
sudo apt purge appector
```

These commands do not remove Appector's per-user state or AppImages.

## Supported systems

The package is architecture-independent Python (`Architecture: all`), but it
has not yet been integration-tested on Debian stable, Ubuntu LTS, Linux Mint,
Pop!_OS, Raspberry Pi OS, or both amd64 and arm64. Package availability and
GTK/libadwaita versions must be checked on each supported distribution before
claiming support. The current audit host is Ubuntu 26.04.

## Usage and destructive actions

Refresh the installed-app list, search and filter it, inspect an app's details,
and use the main menu for installation, exports, update checks, and maintenance.
Removal and cleanup can delete packages, package configuration, or files.
Review each preview and confirmation carefully; package-manager transactions
can affect dependencies beyond the selected app.

When Appector purges APT residual configurations it first backs up dpkg-listed
conffiles under:

```text
~/.local/state/app-manager/purge-backups/
```

The backup includes a `manifest.json` and copies under `files/`. There is no
in-app restore action yet. To restore a file, inspect its manifest record,
confirm the target is safe, and restore the saved copy to the recorded absolute
path with appropriate ownership and mode; system paths require administrator
privileges. Do not blindly copy an entire backup tree over the filesystem.
If the backup is incomplete, Appector refuses to start the purge.

App data and activity state are stored per user:

- `~/.local/share/app-manager/appimages/` — managed AppImage copies.
- `~/.local/share/applications/` — Appector-created AppImage launchers.
- `~/.local/state/app-manager/actions.log` — activity log.
- `~/.local/state/app-manager/purge-backups/` — pre-purge conffile backups.

The activity log and newly generated CSV/JSON exports are written with
user-only permissions. The activity log can contain package names, operation
results, and local paths; exports can reveal software installed on the machine.
Review both before sharing.

## Appector updates and network use

**Check for Appector updates…** is a manual menu action. It requests the latest
stable release metadata from
`https://api.github.com/repos/ell-shad/appector-releases/releases/latest`.
GitHub's `latest` endpoint excludes drafts and pre-releases. Beta-channel
updates are not currently offered. When a newer release is found, Appector
opens that release page; it never downloads or installs the update itself.
Install a downloaded package with `apt install ./file.deb` after reviewing the
release and package.

There is no background update check and no telemetry or analytics. Package
installation and update actions use the locally configured APT, Snap, or
Flatpak services and their configured software sources; installing from
Flathub may contact `https://dl.flathub.org/repo/flathub.flatpakrepo`.
The GitHub Releases API is the only direct Appector update-check endpoint.

A `.deb` installed from a GitHub Release does **not** configure an APT
repository and will not receive upgrades through `apt upgrade`. Release
publication requires creating a public `ell-shad/appector-releases`
repository, configuring its write-scoped `APPECTOR_RELEASES_TOKEN` as a secret
in a GitHub Actions `public-release` environment, and protecting that
environment with a required human reviewer. The source repository stays
private. A tag matching the version in `appector/__init__.py` then builds the
package, creates `SHA256SUMS`, and publishes a release to the public assets
repository. The first `0.x` release is marked as a pre-release.

Checksums are not signed yet. A signed APT
repository (for example, GitHub Pages with aptly/reprepro, or a hosted package
repository) is a possible later improvement; it is not configured here.

## Build from source

On Debian or Ubuntu, install the runtime/build prerequisites:

```bash
sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
  gir1.2-gdkpixbuf-2.0 dpkg
```

Then, from a checkout:

```bash
python3 run.py
./scripts/build-deb.sh
```

The package builder creates `dist/appector_<version>_all.deb`. It does not
install the package or publish a release. Automated tests can be run with:

```bash
python3 -m unittest discover -s tests -v
```

## Troubleshooting and recovery

- If an operation fails, review its details in **Activity Log** and inspect
  APT's state with `sudo dpkg --audit`.
- To complete interrupted package configuration, review the proposed changes
  and run `sudo dpkg --configure -a` if appropriate.
- For package-manager lock errors, wait for the other APT/dpkg operation to
  finish; do not remove lock files manually.
- If the desktop app cannot start, check that GTK 4, libadwaita, and PyGObject
  are installed and launch `appector` from a terminal to see diagnostic
  output.
- Before sharing logs or exports, remove personal paths and installed-software
  details that you do not wish to disclose.

Report bugs at
<https://github.com/ell-shad/appector/issues> with the distribution/release,
architecture, Appector version, and a redacted error or log excerpt. The source
repository is private, so public issue reporting still needs a maintainer-
approved public support channel.

## License and security

The repository contains the text of the GNU General Public License version 3
in [`LICENSE`](./LICENSE). The project's explicit copyright-holder/license
grant should be confirmed before a public release; Debian metadata currently
assumes GPL-3-only provisionally.

For security issues, follow [`SECURITY.md`](./SECURITY.md). Do not include
credentials or unredacted private logs in public reports.
