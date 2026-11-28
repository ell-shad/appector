#!/bin/sh
# Make a failed scheduled refresh visible to a person.
#
# The weekly refresh is the only thing that keeps the published metadata from
# drifting past its Valid-Until date, so a failure that nobody notices is
# exactly how `apt update` starts failing weeks later. A failed run is silent
# otherwise: the run is red in the Actions tab, which no one watches.
#
# This opens one issue on the repository (assigned to its owner, which is what
# sends the notification) and, while that issue is still open, adds a comment
# to it instead of opening another. Failed runs therefore show up as one
# tracked problem with its own history, not a new issue every week.
#
# Usage:
#   scripts/report-refresh-failure.sh RUN_URL
#
# GITHUB_REPOSITORY must be set to owner/repo and the gh CLI must be
# authenticated (GH_TOKEN or GITHUB_TOKEN) with `issues: write`.
#
# Exit status: 0 when the failure has been recorded. The alert is best effort
# about the parts that can fail for permission reasons (assigning the issue),
# but a run that cannot be reported at all fails, so a broken alerting path is
# visible rather than silent.

set -eu

RUN_URL=${1:?usage: report-refresh-failure.sh RUN_URL}
REPO=${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set to owner/repo}
OWNER=${GITHUB_REPOSITORY_OWNER:-${REPO%%/*}}

TITLE="APT repository refresh failed"

log() { printf '%s\n' "$*" >&2; }
fail() { log "error: $*"; exit 1; }

command -v gh >/dev/null 2>&1 || fail "the gh CLI is required"

body=$(cat <<EOF
The scheduled APT repository refresh failed, so the published metadata was
**not** re-signed.

Left alone, this is how the archive expires: once \`Valid-Until\` passes,
\`apt update\` fails with "Release file … is expired" and then
"Unable to locate package appector", for every user, on every machine.

Failing run: $RUN_URL

Fix the cause, then re-run the refresh:

    gh workflow run "Publish APT repository" --ref main -f refresh=true

Close this issue once a refresh has succeeded.
EOF
)

# Reuse the open issue if there is one. Comparing the title exactly avoids the
# search syntax, where an unrelated match would swallow the alert.
number=$(gh issue list --repo "$REPO" --state open --limit 100 \
    --json number,title \
    --jq "[.[] | select(.title == \"$TITLE\")][0].number // empty") \
    || fail "could not list the issues of $REPO"

if [ -n "$number" ]; then
    gh issue comment "$number" --repo "$REPO" --body "$body" >/dev/null \
        || fail "could not comment on issue #$number"
    log "added the failure to issue #$number"
else
    # `gh issue create` prints the URL of the issue it opened; the number is
    # its last path segment.
    url=$(gh issue create --repo "$REPO" --title "$TITLE" --body "$body") \
        || fail "could not open the alert issue"
    number=${url##*/}
    case "$number" in
        ''|*[!0-9]*)
            log "notice: opened the alert issue at $url"
            number=
            ;;
        *)
            # Assigning the owner is what notifies them. It must not be the
            # reason an alert is lost, so a failure here is only a notice.
            if ! gh issue edit "$number" --repo "$REPO" \
                --add-assignee "$OWNER" >/dev/null 2>&1; then
                log "notice: could not assign issue #$number to $OWNER"
            fi
            log "opened issue #$number"
            ;;
    esac
fi

log "reported the failed refresh of $REPO; $OWNER has been notified"
