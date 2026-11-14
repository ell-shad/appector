# ADR-0003: A dedicated privileged helper replaces `pkexec sh -c`

## Status

Accepted

## Date

2026-11-14

## Context

Every privileged operation in Appector used to run through one of two
mechanisms:

1. `pkexec apt-get …` — a single-purpose command, but with a polkit prompt
   that shows the user a bare `apt-get` command line, and with the
   environment pkexec sanitizes: `DEBIAN_FRONTEND`, `NEEDRESTART_MODE` and
   friends set in the parent never reach the root process, so conffile
   prompts, needrestart or debconf could hang a run whose stdin is
   `/dev/null` (S5).
2. `pkexec sh -c <generated script>` — the batch removal path. The script
   is a text protocol (`echo APP_MANAGER_START APT pkg`) whose markers any
   package maintainer script can spoof, and the user authorizes a generic
   root shell rather than a named operation (S6). When pkexec is missing,
   both paths silently fall back to running unprivileged and fail late —
   after the purge backup was already made.

The .deb install path had a third problem no amount of client-side hashing
can fix: after Appector verified the staged file, it passed the
user-writable *path* to root (`pkexec apt-get install -- <path>`). The
polkit prompt can sit between the final hash check and root opening the
file, and the owning user can swap the file in that window — running
maintainer scripts of a package the user never reviewed (S1, TOCTOU).
Likewise, the conffile backup ran unprivileged, so root-only configuration
files permanently blocked purges (S9).

## Decision

Appector ships a dedicated privileged helper, `appector-helper`, installed
root-owned at `/usr/lib/appector/appector-helper`, with a polkit policy
(`com.appector.appector.policy`) that defines one action id per operation
so prompts are specific and reviewable:

| Action id | Operation |
|---|---|
| `com.appector.appector.install-deb` | install reviewed local .deb files |
| `com.appector.appector.remove-apt` | apt-get remove/purge |
| `com.appector.appector.purge-residual` | purge residual configurations (incl. the root-side conffile backup) |
| `com.appector.appector.remove-snap` | snap remove |
| `com.appector.appector.remove-flatpak-system` | flatpak uninstall (system scope) |
| `com.appector.appector.apt-autoremove` | apt-get autoremove |
| `com.appector.appector.apt-upgrade` | apt-get install --only-upgrade |
| `com.appector.appector.flatpak-update-system` | flatpak update (system scope) |

Protocol:

- The client (`appector/helper_client.py`) spawns
  `pkexec /usr/lib/appector/appector-helper <action>` and writes **one JSON
  request** to its stdin: action, a random 32-hex nonce, the package
  identities (`name:arch` for APT), the confirmed `Plan` (package ids *and*
  the previewed removal set) and its hash, plus action-specific fields.
- The helper emits **JSON-lines events** on stdout: `start`, `output`
  (streamed process output), action-specific events (e.g. `backup`), and a
  final mandatory `result`. Every event echoes the nonce, so spoofed
  `output` lines from maintainer scripts cannot inject a fake result —
  the client drops any event whose nonce mismatches.
- Fail fast: if the helper or pkexec is unavailable, the operation is
  refused with an explicit message *before* any backup or partial work; the
  old "run unprivileged and fail late" fallback is gone.

The helper re-validates everything as root, because everything the client
checked is attacker-controllable up to the moment root acts:

- schema: unknown top-level keys, wrong types, over-long or empty values
  and more than the per-action package cap are rejected;
- names: every package identity must match `policy.SAFE_PACKAGE_ID_RE`;
  snap and Flatpak ids their own grammar;
- policy: `policy.is_critical_apt_package_name` and the critical metadata
  rules for every APT identity; the cascade cap (`APT_MAX_REMOVALS`);
- plan binding: the request carries the confirmed plan; the helper
  recomputes its hash, re-runs the APT simulation itself, and refuses when
  the re-simulation grew beyond the preview (S4 enforced at root);
- for `install-deb`, the helper never installs a user-writable path: it
  opens each source once with `O_NOFOLLOW` + a regular-file check, copies
  into a root-owned 0700 staging directory while hashing, compares the
  digest to the reviewed SHA-256, and runs `apt-get install` on the
  root-owned copies only (S1);
- for `purge-residual`, the conffile backup runs as root into the invoking
  user's state directory (located via `PKEXEC_UID`/`SUDO_UID`), then the
  copy is `chown`ed to that user; the rc state and the multiarch rule are
  re-verified as root immediately before the purge (S2/S9);
- the environment is set by the helper itself: `DEBIAN_FRONTEND=
  noninteractive`, `NEEDRESTART_MODE=a`, `APT_LISTCHANGES_FRONTEND=none`,
  `LC_ALL=C`, plus `-o Dpkg::Options::=--force-confold -o Dpkg::Options::=
  --force-confdef` (S5) — pkexec's sanitized environment no longer matters;
- no shell is ever involved: commands are argv lists (`shell=False`), the
  APT lock is taken by the helper, and all subprocesses run with
  `start_new_session=True` so a timeout kills the whole process group.

The helper runs only as root (`os.geteuid() != 0` → refuse), reads the
request once from stdin, and exits after one action.

## Consequences

- Polkit prompts now name the exact operation ("Install reviewed Debian
  packages", "Remove APT packages …"), not a shell command.
- The .deb install TOCTOU is closed at the only place it can be closed:
  inside the root process.
- Root-only conffiles no longer block residual purges.
- Running from a source checkout without the packaged helper now refuses
  privileged operations with a clear message instead of silently
  degrading — a deliberate behaviour change; install the .deb (or set
  `APECTOR_HELPER` to the helper path for development).
- The helper shares the policy/plan code with the client (`appector.policy`,
  `appector.plans`, `appector._proc`), so the root-side rules cannot drift
  from the client-side ones; it is unit-tested with injected hostile inputs
  (`tests/test_helper.py`).
- The old `_build_privileged_script` / `_execute_privileged_script` text
  protocol and every `pkexec sh -c` call are removed (S6).

## Verification

- Unit tests feed the helper hostile requests (option-like names, newlines,
  oversized lists, wrong nonce, non-root, unknown keys) and assert refusal
  without executing anything.
- The `install-deb` handler is tested with a swapped source file between
  "review" and install: the hash comparison refuses it.
- The polkit policy is validated in CI against the DTD (`xmllint`) when
  available.
