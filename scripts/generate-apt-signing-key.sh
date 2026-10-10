#!/bin/sh
# Generate the GPG keypair used to sign the Appector APT repository.
#
# Run this ONCE, on a machine you control. It produces:
#
#   apt/keys/appector-archive-keyring.asc   the public key (safe to commit)
#   $SECRET_FILE                            the private key (NEVER commit)
#
# The private key must be stored as the GitHub Actions secret
# APPECTOR_GPG_KEY. Delete the local copy once the secret is set, or keep it
# somewhere encrypted and offline.
#
# Usage: scripts/generate-apt-signing-key.sh [--passphrase]

set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)

KEY_NAME=${APPECTOR_KEY_NAME:-"Appector Archive Signing Key <appector@ell-shad.github.com>"}
PUBLIC_KEY="$ROOT/apt/keys/appector-archive-keyring.asc"
SECRET_FILE=${APPECTOR_SECRET_KEY_FILE:-"$HOME/.secrets/appector-archive-secret-key.asc"}

log() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Values are deliberately not "read" or other shell command names: shellcheck
# reads an unquoted assignment of a command name as a command substitution.
PASSPHRASE_MODE=none
for argument in "$@"; do
    case "$argument" in
        --passphrase) PASSPHRASE_MODE=prompt ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) fail "unknown argument: $argument" ;;
    esac
done

command -v gpg >/dev/null 2>&1 || fail "gpg is required"

if [ -e "$PUBLIC_KEY" ]; then
    fail "public key already exists: $PUBLIC_KEY
Remove it first if you really mean to replace the archive signing key."
fi

mkdir -p "$(dirname "$PUBLIC_KEY")"
mkdir -p "$(dirname "$SECRET_FILE")"
chmod 700 "$(dirname "$SECRET_FILE")"

# A throwaway keyring so the archive key is not mixed into the user's own
# keyring, and so the script cannot touch existing personal keys.
GNUPGHOME=$(mktemp -d)
chmod 700 "$GNUPGHOME"
export GNUPGHOME
trap 'rm -rf "$GNUPGHOME"' EXIT INT TERM

log "Generating an ed25519 archive signing key in a throwaway keyring..."
if [ "$PASSPHRASE_MODE" = prompt ]; then
    log "You will be prompted for a passphrase. Leave it empty only if you"
    log "understand the consequences described below."
    # --batch suppresses interaction, so loopback pinentry must be pointed at
    # an explicit source of the passphrase. It cannot read standard input
    # twice (key creation and the agent both want it), so the passphrase is
    # read once here and kept inside the throwaway keyring, which the exit
    # trap removes. It is never passed on a command line, where any local
    # user could read it from the process list.
    printf 'Enter passphrase: ' >&2
    stty -echo 2>/dev/null || true
    IFS= read -r PASSPHRASE
    stty echo 2>/dev/null || true
    printf '\n' >&2
    printf '%s' "$PASSPHRASE" > "$GNUPGHOME/passphrase"
    chmod 600 "$GNUPGHOME/passphrase"
    unset PASSPHRASE
    gpg --batch --pinentry-mode loopback \
        --passphrase-file "$GNUPGHOME/passphrase" \
        --quick-generate-key "$KEY_NAME" ed25519 sign never
else
    gpg --batch --quiet --passphrase '' --quick-generate-key \
        "$KEY_NAME" ed25519 sign never
fi

FINGERPRINT=$(gpg --batch --list-secret-keys --with-colons \
    | awk -F: '/^fpr:/ {print $10; exit}')
[ -n "$FINGERPRINT" ] || fail "could not determine the new key fingerprint"

gpg --batch --yes --armor --export "$FINGERPRINT" > "$PUBLIC_KEY"

# Exporting the secret key also needs the passphrase: gpg asks the agent for
# it to unwrap the key. Without the passphrase file still in place this fails
# with "error receiving key from agent: No passphrase given - skipped" and
# writes an empty file.
if [ "$PASSPHRASE_MODE" = prompt ]; then
    gpg --batch --yes --pinentry-mode loopback \
        --passphrase-file "$GNUPGHOME/passphrase" --armor \
        --export-secret-keys "$FINGERPRINT" > "$SECRET_FILE"
else
    gpg --batch --yes --passphrase '' --armor \
        --export-secret-keys "$FINGERPRINT" > "$SECRET_FILE"
fi

rm -f "$GNUPGHOME/passphrase"

# An armored private-key block is the only acceptable result here. An empty or
# truncated file would be stored as the release signing secret and fail every
# later release.
grep -q "BEGIN PGP PRIVATE KEY BLOCK" "$SECRET_FILE" \
    || fail "the secret key was not exported correctly"
chmod 600 "$SECRET_FILE"

log ""
log "Public key  : $PUBLIC_KEY"
log "Secret key  : $SECRET_FILE (mode 600)"
log "Fingerprint : $FINGERPRINT"
log ""
log "Next steps:"
log ""
log "1. Commit the PUBLIC key only:"
log "     git add apt/keys/appector-archive-keyring.asc"
log ""
log "2. Store the PRIVATE key as a GitHub Actions secret (it is piped, so it"
log "   is never written to your shell history):"
log "     gh secret set APPECTOR_GPG_KEY --repo ell-shad/appector < $SECRET_FILE"
log ""
log "3. Store the fingerprint so the workflow signs with the right key:"
log "     gh secret set APPECTOR_GPG_KEY_ID --repo ell-shad/appector <<< '$FINGERPRINT'"
log ""
if [ "$PASSPHRASE_MODE" = prompt ]; then
    log "4. Store the passphrase, since CI cannot prompt:"
    log "     gh secret set APPECTOR_GPG_PASSPHRASE --repo ell-shad/appector"
    log ""
    log "   The signing key is passphrase-protected. Losing the passphrase means"
    log "   the archive key must be rotated and every user must re-import it."
else
    log "4. No passphrase was set. The private key is protected only by your"
    log "   filesystem and by GitHub's encrypted secret storage. Anyone with"
    log "   access to the repository secrets can sign releases for this project."
    log "   Re-run this script with --passphrase if you want stronger protection."
fi
log ""
log "5. Delete the local copy of the private key once the secret is stored:"
log "     shred -u $SECRET_FILE"
log ""
log "Do not commit $SECRET_FILE, and do not paste the private key into a"
log "chat, issue, or pull request."