#!/bin/sh
# Build a signed APT repository from the Debian packages in dist/.
#
# Layout produced (served from the repository root):
#
#   pool/main/a/appector/appector_<version>_all.deb
#   dists/stable/main/binary-all/Packages
#   dists/stable/main/binary-all/Packages.gz
#   dists/stable/Release
#   dists/stable/InRelease            (only when a signing key is supplied)
#   dists/stable/Release.gpg          (only when a signing key is supplied)
#
# Usage:
#   scripts/build-apt-repo.sh [DIST_DIR] [OUTPUT_ROOT]
#
# Signing: set APPECTOR_GPG_KEY to the ASCII-armoured private key, and
# optionally APPECTOR_GPG_KEY_ID to select a key from the keyring. When no key
# is supplied the repository is still built, but unsigned, which is only
# useful for local verification - apt refuses unsigned repositories by default.

set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)

DIST_DIR=${1:-"$ROOT/dist"}
OUTPUT_ROOT=${2:-"$ROOT/apt-repo"}

SUITE=stable
COMPONENT=main
ARCHITECTURE=all
SECTION=utils

log() { printf '%s\n' "$*" >&2; }
fail() { log "error: $*"; exit 1; }

[ -d "$DIST_DIR" ] || fail "package directory not found: $DIST_DIR"

command -v dpkg-scanpackages >/dev/null 2>&1 \
    || fail "dpkg-scanpackages is required (package: dpkg-dev)"

# ---------------------------------------------------------------------------
# Collect packages
# ---------------------------------------------------------------------------
PACKAGE_LIST=$(mktemp)
CLEANUP_FILES="$PACKAGE_LIST"

cleanup() {
    for path in $CLEANUP_FILES; do
        rm -rf "$path"
    done
}
trap cleanup EXIT INT TERM

# Newline-delimited, which is sufficient here: dist/ is produced by
# scripts/build-deb.sh, which already restricts the version to characters
# that cannot include a newline.
find "$DIST_DIR" -maxdepth 1 -name '*.deb' -type f -print \
    | sort > "$PACKAGE_LIST"
[ -s "$PACKAGE_LIST" ] || fail "no .deb packages found in $DIST_DIR"

POOL_DIR="$OUTPUT_ROOT/pool/$COMPONENT/a/appector"
DISTS_DIR="$OUTPUT_ROOT/dists/$SUITE"
BINARY_DIR="$DISTS_DIR/$COMPONENT/binary-$ARCHITECTURE"

rm -rf "$OUTPUT_ROOT"
mkdir -p "$POOL_DIR" "$BINARY_DIR"

while IFS= read -r package; do
    [ -n "$package" ] || continue
    name=$(basename "$package")
    cp "$package" "$POOL_DIR/$name"
    log "pooled $name"
done < "$PACKAGE_LIST"

# ---------------------------------------------------------------------------
# Packages index
# ---------------------------------------------------------------------------
# Run from the repository root so Filename entries are repo-root relative,
# which is what apt resolves against the configured base URL.
(
    cd "$OUTPUT_ROOT"
    # Scan pool/main, not "main": the scan path determines the Filename
    # entries, which apt resolves against the repository base URL, so they
    # have to be pool-root relative.
    dpkg-scanpackages --arch "$ARCHITECTURE" "pool/$COMPONENT" \
        > "$BINARY_DIR/Packages"
)

[ -s "$BINARY_DIR/Packages" ] || fail "generated Packages index is empty"

# -n keeps the source file so the Release checksum can cover it too.
gzip -9 -n -c "$BINARY_DIR/Packages" > "$BINARY_DIR/Packages.gz"

# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------
python3 - "$OUTPUT_ROOT" "$SUITE" "$COMPONENT" "$ARCHITECTURE" "$SECTION" \
    <<'PY'
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
suite, component, architecture, section = sys.argv[2:6]
dists = root / "dists" / suite

entries = []
for relative in sorted([
    f"{component}/binary-{architecture}/Packages",
    f"{component}/binary-{architecture}/Packages.gz",
]):
    path = dists / relative
    data = path.read_bytes()
    entries.append((relative, hashlib.sha256(data).hexdigest(), len(data)))

date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

lines = [
    "Origin: Appector",
    "Label: Appector",
    f"Suite: {suite}",
    f"Codename: {suite}",
    f"Architectures: {architecture}",
    f"Components: {component}",
    f"Section: {section}",
    "Description: Appector Debian package repository",
    f"Date: {date}",
    "Acquire-By-Hash: no",
    "SHA256:",
]
for relative, digest, size in entries:
    lines.append(f" {digest} {size:>16} {relative}")

(dists / "Release").write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

log "wrote $DISTS_DIR/Release"

# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------
if [ -n "${APPECTOR_GPG_KEY:-}" ]; then
    command -v gpg >/dev/null 2>&1 || fail "gpg is required to sign the repository"

    GNUPGHOME=$(mktemp -d)
    export GNUPGHOME
    chmod 700 "$GNUPGHOME"
    CLEANUP_FILES="$CLEANUP_FILES $GNUPGHOME"

    printf '%s\n' "$APPECTOR_GPG_KEY" | gpg --batch --quiet --import \
        || fail "could not import APPECTOR_GPG_KEY"

    if [ -z "$(gpg --batch --list-secret-keys --with-colons 2>/dev/null)" ]; then
        fail "APPECTOR_GPG_KEY contains no secret (signing) key"
    fi

    # Batch signing never prompts, so loopback pinentry is required and any
    # passphrase must be passed on the command line. Arguments are passed
    # individually rather than through one string, so a passphrase containing
    # spaces survives intact.
    sign_release() {
        if [ -n "${APPECTOR_GPG_PASSPHRASE+x}" ]; then
            gpg --batch --yes --pinentry-mode loopback \
                --passphrase "$APPECTOR_GPG_PASSPHRASE" "$@"
        else
            gpg --batch --yes --pinentry-mode loopback "$@"
        fi
    }

    KEY_ARGS=""
    if [ -n "${APPECTOR_GPG_KEY_ID:-}" ]; then
        KEY_ARGS="--local-user $APPECTOR_GPG_KEY_ID"
    fi

    # InRelease: the clearsigned Release. apt prefers this when present.
    # shellcheck disable=SC2086
    sign_release $KEY_ARGS --clearsign \
        --output "$DISTS_DIR/InRelease" "$DISTS_DIR/Release" \
        || fail "clearsigning Release failed"

    # Release.gpg: detached signature, required for clients that cannot use
    # clearsigned metadata.
    # shellcheck disable=SC2086
    sign_release $KEY_ARGS --detach-sign --armor \
        --output "$DISTS_DIR/Release.gpg" "$DISTS_DIR/Release" \
        || fail "detached-signing Release failed"

    log "signed with $(gpg --batch --list-secret-keys --with-colons \
        | awk -F: '/^fpr:/ {print $10; exit}')"
else
    log "APPECTOR_GPG_KEY not set: repository left UNSIGNED"
    log "apt will refuse this repository unless AllowInsecureRepositories is enabled"
fi

# ---------------------------------------------------------------------------
# Export the public key alongside the repository so users can fetch it easily
# ---------------------------------------------------------------------------
if [ -n "${APPECTOR_GPG_KEY:-}" ] && command -v gpg >/dev/null 2>&1; then
    KEY_ID=${APPECTOR_GPG_KEY_ID:-}
    if [ -n "$KEY_ID" ]; then
        gpg --batch --yes --armor --export "$KEY_ID" \
            > "$OUTPUT_ROOT/appector-archive-keyring.asc"
    else
        gpg --batch --yes --armor --export \
            > "$OUTPUT_ROOT/appector-archive-keyring.asc"
    fi
fi

log "repository built at $OUTPUT_ROOT"