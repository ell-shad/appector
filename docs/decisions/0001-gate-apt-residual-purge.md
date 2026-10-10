# ADR-0001: Gate every APT residual purge behind a full pre-flight

## Status

Accepted

## Date

2026-10-10

## Context

Appector offers an experimental action that purges APT packages in dpkg's
`rc` (removed, config-files-remain) state. Unlike removing an installed
package, this looks harmless from the GUI: the package is already gone, so
"purging" appears to only tidy leftovers.

That appearance was wrong in several ways:

- **A purge is irreversible.** `apt-get purge` deletes configuration without
  asking again. There is no undo, and Appector has no restore feature.
- **The `rc` check was a time-of-check/time-of-use race.** The original flow
  verified residual state, then made a backup (slow — it copies every
  conffile), then purged. A package reinstalled during that window left
  `rc` state and would then have been purged *as if it were only residual
  config* — deleting configuration of a freshly installed, working package.
- **The backup was narrower than the deletion.** The backup covered
  dpkg-registered conffiles, but `postrm` maintainer scripts may delete more.
  Users were told the operation was recoverable in a way that was not true.
- **Nothing confirmed the purge scope.** `apt-get purge` was run without a
  simulation, so a user could not see what it would actually touch.
- **Backups accumulated without bound,** filling the home directory over
  repeated use.

## Decision

Treat a residual purge as a privileged destructive operation with an explicit
pre-flight. `appector/residual.py::purge_leftover_configs` runs these gates in
order and stops at the first failure:

1. **Validate** every package name against a strict allowlist regex, so a name
   can never be parsed as an option or shell syntax.
2. **Verify** each package is still in `rc` state.
3. **Simulate** with `apt-get --simulate purge`. Require every selected
   package to appear in the plan, and refuse if the plan touches any package
   outside the selection.
4. **Back up** dpkg-registered conffiles into a private `0700` tree, with a
   per-batch cap on file count and total bytes. An incomplete backup aborts.
5. **Re-verify** `rc` state *after* the backup. This is the gate that closes
   the reinstall race.
6. **Purge** via `pkexec apt-get purge -y`.
7. **Verify** afterwards and report anything still residual.

The GUI mirrors the same ordering, so the user sees the simulation before
confirming and can narrow the selection per package first.

## Consequences

- Purging is slower and sometimes refused where it previously "worked". A
  reinstalled-during-backup package is now reported instead of silently
  purged; that is the intended behaviour.
- Only the newest 10 backups are retained; older ones are pruned after a
  successful purge. A user wanting an older backup must copy it out first.
- The simulation is documented as the authoritative preview, because it is the
  only view that includes `postrm` side effects Appector cannot model.
- Backups are a recovery aid, not a restore feature. The UI says so
  explicitly and offers a read-only manifest browser instead of pretending
  to provide one-click restore.

## Alternatives considered

- **`dpkg --purge` instead of `apt-get purge`.** Faster and avoids the APT
  resolver, but gives no simulation and no dependency awareness, which is
  exactly the visibility this decision depends on. Rejected.
- **Rely on dpkg's own prompts.** Appector runs non-interactively under
  `pkexec`; interactive confirmation is not available and would put the
  decision in the hands of a prompt nobody reads. Rejected.
- **Skip the simulation, trust `rc` state.** The original design. Rejected:
  it is the reason the simulation gate exists.
- **Keep every backup forever.** Rejected in favour of a bounded retention,
  given backups can be large and are a convenience rather than the only copy
  of a system's real state (snapshots are the real safety net).

## Related

- Destructive file removal outside the residual flow uses a separate,
  stricter rule set in `appector/actions.py::_validate_removal_file`:
  allowlisted root, minimum depth below that root, ownership evidence read
  before the operation, and regular-file-or-symlink only.