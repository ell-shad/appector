# Changelog

All notable changes to Appector will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions use the application's Debian-compatible version order.

## [Unreleased]

### Added

- Initial Debian package build and release workflow.
- Manual check for stable updates through GitHub Releases.
- Add the Appector application icon to the desktop app, Debian package, and GitHub branding assets.
- Show local Debian package metadata, file hash and APT transaction preview before installation.
- Generate GitHub build-provenance attestations for release Debian packages.
- Regression tests for removal protection, private logs, and update version checks.

### Fixed

- Prevent removal of Appector and the legacy `app-manager` package.
- Write activity logs with user-only permissions.
- Compare update versions using Debian's version rules and validate release URLs.

## [0.1.0] - Unreleased

### Added

- Initial GTK desktop application package.
