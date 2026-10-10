# ADR-0002: Split safety policy, process execution, and the residual subsystem

## Status

Accepted

## Date

2026-10-10

## Context

`appector/actions.py` had grown past 4500 lines and was still growing. It
contained unrelated concerns side by side:

- the safety policy (which packages may never be removed),
- process execution and the activity log,
- APT, Snap, Flatpak, AppImage and manual-install operations,
- the entire APT residual-configuration purge subsystem.

Three problems followed from that:

1. **Untestable rules.** Policy questions ("may this package be removed?",
   "is this name safe?") were interleaved with subprocess calls, so verifying
   them required mocking execution.
2. **Duplication waiting to happen.** The purge subsystem needs the same
   package-name validation, apt-simulation parsing, and package-list
   formatting as the removal path. Sharing them meant importing across a
   cycle unless they were lifted out.
3. **Review risk.** A change to one subsystem forced a reviewer to reason
   about four unrelated ones in the same file.

## Decision

Split by *concern*, not by size:

| Module | Owns |
|---|---|
| `appector/policy.py` | Pure decisions: blocked/critical package lists, name validation, `is_blocked`, `can_remove*`, `get_removal_risk`. No I/O. |
| `appector/_proc.py` | Process and logging primitives: apt lock serialisation, ANSI stripping, `_run_command*`, `_log_action`. |
| `appector/residual.py` | The residual purge subsystem end to end: previews, gates, backup, purge. |
| `appector/actions.py` | Remaining package-manager operations (install, update, remove, Flatpak/AppImage handling). |

Dependency direction is one-way and acyclic:
`window` → `actions` → `residual` → `policy` / `_proc`.

The single exception is `CONFFILE_LINE_RE`, a dpkg output-format constant
shared by the residual preview and the size estimator in `actions`. It lives
with the code that owns dpkg conffile parsing (`residual`) rather than being
duplicated or moved into a grab-bag module.

## Consequences

- Policy rules can be read and unit-tested without touching the filesystem.
- The residual subsystem — the highest-risk code in the project — is one
  file with one docstring describing its safety model.
- Adding a new package manager no longer grows `residual` or `policy`.
- `actions.py` remains the largest module (~3300 lines). It is still above a
  comfortable size; the next split candidate is the Flatpak surface
  (`install_flatpak_*`, `add_flathub_remote`, `get_flatpak_*`), which is
  cohesive enough to move wholesale.
- `_proc.py` is a private module by the leading underscore: its helpers are
  shared internally but are not a supported external API.

## Verification

The split was validated with the existing suite plus a mutation check that
disables each safety gate in turn (ownership check, resolved-root check,
non-regular-file guard, all three residual caps, the post-backup re-verify,
the option terminators, and the scope filters) and asserts the suite fails.
Every mutation is caught, so the tests genuinely pin the safety behaviour
rather than merely exercising it.