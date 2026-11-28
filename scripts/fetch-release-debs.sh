#!/bin/sh
# Backfill the APT pool with the .deb assets attached to GitHub Releases.
#
# The pool is carried forward from the published repository, so a version is
# only in it if some publish run put it there. Releases published before that
# carry-forward worked exist solely as GitHub Release assets, which left a
# user pinned to one of them with a 404 from the APT repository. This script
# reads those assets back into the pool, so the served repository contains
# every version that was ever released.
#
# Usage:
#   scripts/fetch-release-debs.sh OUTPUT_DIR
#
# GITHUB_REPOSITORY must be set to owner/repo and the gh CLI must be
# authenticated (GH_TOKEN or GITHUB_TOKEN). Pre-releases and drafts are
# skipped: they never belong on the stable channel.
#
# BACKFILL_LENIENT=1 turns a release that cannot be backfilled (no manifest,
# unreadable assets, checksum mismatch) into a warning that skips that release,
# so a defective *old* release cannot block a new release, nor stop the weekly
# refresh from re-signing the archive every week until the metadata expires.
# Both jobs set it. The newest release is never skippable: if it cannot be
# produced the run fails, because a pool without the current version is not a
# repository anyone can install from.

set -eu

OUTPUT_DIR=${1:?usage: fetch-release-debs.sh OUTPUT_DIR}
REPO=${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set to owner/repo}

log() { printf '%s\n' "$*" >&2; }
fail() { log "error: $*"; exit 1; }

LENIENT=${BACKFILL_LENIENT:-0}
skipped=0
newest_tag=

# skip_release TAG REASON: refuse one release. The newest release always fails
# the run, whatever the mode: the served pool must contain the current version.
# An older release fails under strict (the default) and is warned about and
# counted under lenient, so the caller moves on to the next one.
skip_release() {
    if [ "$1" = "$newest_tag" ]; then
        fail "release $1 is the newest release and must be published: $2"
    fi
    if [ "$LENIENT" = "1" ]; then
        log "warning: skipping release $1: $2"
        skipped=$((skipped + 1))
        return 0
    fi
    fail "release $1: $2"
}

command -v gh >/dev/null 2>&1 || fail "the gh CLI is required"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required"

mkdir -p "$OUTPUT_DIR"

work=$(mktemp -d)
tags=$(mktemp)
cleanup() { rm -rf "$work" "$tags"; }
trap cleanup EXIT INT TERM

gh release list --repo "$REPO" --limit 200 \
    --json tagName,isDraft,isPrerelease \
    --jq '.[] | select(.isDraft | not) | select(.isPrerelease | not) | .tagName' \
    > "$tags" || fail "could not list the releases of $REPO"

[ -s "$tags" ] || {
    log "notice: $REPO has no published releases; nothing to backfill"
    exit 0
}

# `gh release list` is ordered newest first, so the first tag is the release
# the repository must serve right now: exactly the one GitHub reports as
# "latest" (drafts and pre-releases are already filtered out above).
newest_tag=$(head -n 1 "$tags")
[ -n "$newest_tag" ] ||
    fail "could not determine the newest release of $REPO"

# listed NAME: is NAME named in SHA256SUMS? `sha256sum --check` only proves
# the files its manifest names, so a package the manifest does not mention
# would otherwise be served with no provenance at all.
listed() {
    awk -v want="$1" '
        { file = $2; sub(/^\*/, "", file); if (file == want) found = 1 }
        END { exit(found ? 0 : 1) }
    ' "${download_dir:?}/SHA256SUMS"
}

fetched=0
while IFS= read -r tag; do
    [ -n "$tag" ] || continue

    assets=$(gh release view "$tag" --repo "$REPO" --json assets \
        --jq '.assets[].name') \
        || {
            skip_release "$tag" "its assets could not be read"
            continue
        }

    case "$assets" in
        *.deb*)
            ;;
        *)
            if [ "$tag" = "$newest_tag" ]; then
                fail "release $tag is the newest release but has no .deb asset"
            fi
            log "notice: $tag has no .deb asset; skipping"
            continue
            ;;
    esac

    # Packages about to be served again must be verified first: an asset
    # without a manifest cannot be proven to be the artifact that was
    # released, so it is not published.
    if ! printf '%s\n' "$assets" | grep -qx 'SHA256SUMS'; then
        skip_release "$tag" \
            "it has no SHA256SUMS asset, so its packages cannot be verified"
        continue
    fi

    download_dir="$work/assets"
    rm -rf "${download_dir:?}"
    mkdir -p "$download_dir"
    if ! gh release download "$tag" --repo "$REPO" \
        --pattern '*.deb' --pattern 'SHA256SUMS' --dir "$download_dir" \
        >/dev/null 2>&1; then
        skip_release "$tag" "its assets could not be downloaded"
        continue
    fi

    if ! ( cd "$download_dir" && sha256sum --check --strict SHA256SUMS >/dev/null ); then
        skip_release "$tag" "its assets do not match its SHA256SUMS"
        continue
    fi

    unlisted=
    for package in "$download_dir"/*.deb; do
        [ -f "$package" ] || continue
        if ! listed "$(basename "$package")"; then
            unlisted=$(basename "$package")
            break
        fi
    done
    if [ -n "$unlisted" ]; then
        skip_release "$tag" \
            "its SHA256SUMS does not list $unlisted, so it cannot be verified"
        continue
    fi

    copied_here=0
    for package in "$download_dir"/appector_*.deb; do
        [ -f "$package" ] || continue
        cp "$package" "$OUTPUT_DIR/$(basename "$package")"
        log "backfilled $(basename "$package") from $tag"
        copied_here=$((copied_here + 1))
    done
    if [ "$copied_here" -eq 0 ] && [ "$tag" = "$newest_tag" ]; then
        fail "release $tag is the newest release but produced no package for the pool"
    fi
    fetched=$((fetched + copied_here))
done < "$tags"

log "backfilled $fetched release package(s) into $OUTPUT_DIR"
if [ "$skipped" -gt 0 ]; then
    log "warning: skipped $skipped release(s) that could not be backfilled"
fi
