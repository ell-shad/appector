#!/bin/sh
# Verify a freshly built APT repository before it is deployed to GitHub Pages.
#
# The release publish and the scheduled metadata refresh deploy the same
# layout, so this is the single place that decides whether what is about to be
# served is signed by the key users were told to trust, and whether the
# checksums in Release match the indexes on disk.
#
# Usage:
#   scripts/verify-apt-repo.sh REPO_ROOT [COMMITTED_KEY]
#
# COMMITTED_KEY is the public key committed to the source tree
# (apt/keys/appector-archive-keyring.asc). When it is given and present it
# must match the signing key. APPECTOR_GPG_KEY must hold the signing secret,
# and APPECTOR_GPG_PASSPHRASE must hold its passphrase when it has one.

set -eu

REPO_ROOT=${1:?usage: verify-apt-repo.sh REPO_ROOT [COMMITTED_KEY]}
COMMITTED_KEY=${2:-}
DISTS="$REPO_ROOT/dists/stable"

log() { printf '%s\n' "$*" >&2; }
fail() { log "error: $*"; exit 1; }

# Never deploy unsigned metadata. apt rejects such a repository, so a silent
# unsigned deployment would look successful while breaking every user's
# apt update.
[ -f "$DISTS/Release" ]    || fail "Release was not produced"
[ -f "$DISTS/InRelease" ]  || fail "InRelease was not produced"
[ -f "$DISTS/Release.gpg" ] || fail "Release.gpg was not produced"

[ -n "${APPECTOR_GPG_KEY:-}" ] || fail "APPECTOR_GPG_KEY is not set"
command -v gpg >/dev/null 2>&1 || fail "gpg is required"

secret_home=$(mktemp -d)
keyring_home=$(mktemp -d)
derived=$(mktemp)
expected=$(mktemp)
cleanup() { rm -rf "$secret_home" "$keyring_home" "$derived" "$expected"; }
trap cleanup EXIT INT TERM
chmod 700 "$secret_home" "$keyring_home"

# Recompute the public half of the signing secret in an isolated keyring. The
# property that has to hold is that the keyring users import verifies the
# metadata being served.
GNUPGHOME=$secret_home
export GNUPGHOME
printf '%s\n' "$APPECTOR_GPG_KEY" | gpg --batch --quiet --import \
    || fail "could not import APPECTOR_GPG_KEY"
gpg --batch --yes --pinentry-mode loopback \
    --passphrase "${APPECTOR_GPG_PASSPHRASE-}" --armor --export \
    > "$derived" || fail "could not export the archive public key"
[ -s "$derived" ] || fail "the exported public key is empty"

PUBLISHED_KEY="$REPO_ROOT/appector-archive-keyring.asc"
if [ -n "$COMMITTED_KEY" ] && [ -f "$COMMITTED_KEY" ]; then
    # A committed key is reviewable in a pull request, which is preferable.
    # It must nevertheless be the same key, or users who trusted it would
    # fail to verify this repository.
    if ! diff -q "$derived" "$COMMITTED_KEY" >/dev/null; then
        fail "the committed $COMMITTED_KEY does not match the key in APPECTOR_GPG_KEY; users who trusted the committed key would fail to verify this repository"
    fi
    log "committed public key matches the signing key"
    cp "$COMMITTED_KEY" "$PUBLISHED_KEY"
else
    # Not fatal: the archive is still correct, it simply was not reviewed
    # before publication. Commit the key so the next release is reviewable.
    log "warning: no committed archive public key at ${COMMITTED_KEY:-<unset>}; publishing the key derived from the signing secret"
    cp "$derived" "$PUBLISHED_KEY"
fi

# Verify with only the key that is about to be published.
GNUPGHOME=$keyring_home
export GNUPGHOME
gpg --batch --quiet --import "$PUBLISHED_KEY" \
    || fail "could not import $PUBLISHED_KEY"
gpg --batch --verify "$DISTS/InRelease" >/dev/null 2>&1 \
    || fail "InRelease does not verify against $PUBLISHED_KEY"
gpg --batch --verify "$DISTS/Release.gpg" "$DISTS/Release" >/dev/null 2>&1 \
    || fail "Release.gpg does not verify against $PUBLISHED_KEY"
log "release metadata verifies against the published keyring"

# The checksums in Release must cover the indexes actually shipped.
awk '/^SHA256:$/ {inblock=1; next}
     inblock && /^[[:space:]]/ {print $1"  "$3}' "$DISTS/Release" > "$expected"
[ -s "$expected" ] || fail "Release lists no SHA-256 checksums"
( cd "$DISTS" && sha256sum --check "$expected" >/dev/null ) \
    || fail "Release checksums do not match the indexes on disk"
log "Release checksums match the shipped indexes"
