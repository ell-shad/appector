#!/bin/sh
# Fetch the .deb pool of the currently published APT repository.
#
# A GitHub Pages deployment is not a git ref: the served site is the only
# published copy of the archive. Both the release publish and the scheduled
# metadata refresh therefore read the previous pool back over HTTP, otherwise
# every run starts from an empty pool and users still tracking an older
# release lose the file their apt lists reference.
#
# Usage:
#   scripts/fetch-published-pool.sh OUTPUT_DIR BASE_URL
#
# The exit status is 0 both when packages were fetched and when nothing is
# published yet; the latter prints a notice and leaves OUTPUT_DIR empty.

set -eu

OUTPUT_DIR=${1:?usage: fetch-published-pool.sh OUTPUT_DIR BASE_URL}
BASE_URL=${2:?usage: fetch-published-pool.sh OUTPUT_DIR BASE_URL}
BASE_URL=${BASE_URL%/}

SUITE=stable
COMPONENT=main
ARCHITECTURE=all
INDEX="$BASE_URL/dists/$SUITE/$COMPONENT/binary-$ARCHITECTURE/Packages"

log() { printf '%s\n' "$*" >&2; }
fail() { log "error: $*"; exit 1; }

command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required"

mkdir -p "$OUTPUT_DIR"

index_file=$(mktemp)
list_file=$(mktemp)
checksum_file=$(mktemp)
cleanup() { rm -f "$index_file" "$list_file" "$checksum_file"; }
trap cleanup EXIT INT TERM

# A repository that was never published is not an error: the first release
# legitimately has nothing to carry forward.
if ! curl -fsS --location --retry 3 --retry-delay 2 --max-time 120 \
        --output "$index_file" "$INDEX"; then
    log "notice: no published package index at $INDEX"
    log "notice: nothing to carry forward"
    exit 0
fi

# One "<sha256>  <filename>" line per package paragraph, the layout that
# sha256sum --check reads. Paragraph mode (RS = "") keeps multi-line
# Description fields from splitting an entry.
awk '
    BEGIN { RS = "" }
    {
        package = ""; filename = ""; sha256 = ""
        count = split($0, lines, "\n")
        for (i = 1; i <= count; i++) {
            line = lines[i]
            if (line ~ /^Package:/)       package  = substr(line, 10)
            else if (line ~ /^Filename:/) filename = substr(line, 11)
            else if (line ~ /^SHA256:/)   sha256   = substr(line, 9)
        }
        gsub(/^[ \t]+|[ \t]+$/, "", package)
        gsub(/^[ \t]+|[ \t]+$/, "", filename)
        gsub(/^[ \t]+|[ \t]+$/, "", sha256)
        if (package == "appector" && filename != "")
            print sha256 "  " filename
    }
' "$index_file" > "$list_file"

[ -s "$list_file" ] || fail "$INDEX lists no appector packages"

while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    expected=${entry%%  *}
    filename=${entry#*  }
    [ -n "$expected" ] || fail "no SHA256 recorded for $filename"

    destination="$OUTPUT_DIR/$(basename "$filename")"
    log "fetching $(basename "$filename")"
    curl -fsS --location --retry 3 --retry-delay 2 --max-time 300 \
        --output "$destination" "$BASE_URL/$filename"
    printf '%s  %s\n' "$expected" "$destination" >> "$checksum_file"
done < "$list_file"

# These files are about to be re-signed and served again, so a truncated or
# corrupted download must never reach the archive.
sha256sum --check "$checksum_file" >/dev/null \
    || fail "the downloaded pool does not match the published checksums"

log "carried forward $(wc -l < "$checksum_file") package(s) from $BASE_URL"
