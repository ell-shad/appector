# Appector code-review remediation report

Remediation of the full code review (the "appector-main" review document,
sections 5–12), implemented in phase order. Every commit is in this
repository's history; tests refer to `tests/`.

**Baseline → final:** 101 tests / 11 GTK errors → **195 tests, 0 failures,
11 clean GTK skips** (headless), `ruff check appector tests` clean,
`.deb` builds with helper + policy + metainfo verified.

| Commit | Contents |
|---|---|
| `fad038b` | Imported upstream snapshot (pre-review baseline) |
| `c9da794` | Phase 0.1 — headless test suite, GTK-free CLI (`main.py`), `HANDLES_OPEN` scaffolding |
| `0a92de2` | Phase 0.2 — regression tests for all review-verified bugs (committed red: 19 F / 2 E) |
| `9264fac` | Phase 1 core — S2, S7, S9, S11, F15 |
| `3ac860f` | Phase 1 rest — S3, S4, S8, S10, F1–F8, F10, F11, F14, F16 |
| `76e0ac9` | Phase 2 — ADR-0003, appector-helper, polkit policy, client; S1, S5, S6 |
| `42b16ff` | CI/tooling — ruff, coverage, concurrency, dependabot, CODEOWNERS |
| `3fe3485` | Phase 3 partial — U1, U3, U5, U6, U7 |
| `c724427` | Phase 3 complete — F6/U8 banner, splice + selection restore, debounce; U1/U2 leftover-purge plan dialog |
| `4f79e87` | Phase 4.18 — packaging: helper + polkit policy + metainfo + file association, libadwaita ≥ 1.5 |
| `c6a3a7d` | Phase 4.16 — structured .deb review (F13/U10) with real-.deb integration tests |
| `e3b1d3c` | Phase 4.17 — U9 manager filter chips + details-window redesign |
| `6278018` | Phase 4.19 — F9 AppImage hardening, F10 scan dirs, F12 updates UX, U11 i18n + 2 live-verified scanner bug fixes |
| `28697cd` | Phase 5.20 — managers/flatpak.py + ui/install_dialog.py split (ADR-0002 continuation) |
| `d7ed5a2` | Phase 5.21 — S12: single attested build, pool retention, Valid-Until, By-Hash, key lifecycle |

---

## 1. Finding-by-finding status

### Security & data-safety (section 5)

| ID | Status | Where / evidence | Tests |
|---|---|---|---|
| **S1** (.deb TOCTOU) | **Fixed** | `appector/helper.py::_stage_deb_sources` + `handle_install_deb`: root opens each source **once** (O_NOFOLLOW, regular-file check), copies into a root-owned 0700 staging dir **while hashing**, compares to the reviewed SHA-256, and runs `apt-get install` on the copies only; `install_deb_batch` passes `{path, sha256}` pairs. `ensure_staged` now hashes-while-copying. | `test_helper.InstallDebSwapTests` (swapped file → refusal; install runs on the root copy, never the user path); `test_deb_install` passes reviewed hashes to the helper. |
| **S2** (multiarch purge) | **Fixed (VERIFY reproduced by mocked dpkg-query; see §2)** | `residual._dpkg_status_map` queries `${binary:Package}`; all identities are `name:arch`; bare names expand to residual instances only; purge refused when any other-arch instance is non-rc — before and after the backup; simulation parsing and confirmation are arch-aware (`_same_package_instance`). `scan_leftover_configs` is arch-qualified too. | `test_regressions.S2MultiarchResidualTests` (rc libfoo:i386 + ii libfoo:amd64 → blocked with reason); `test_residuals` purge-gate tests. |
| **S3** (flatpakref no review / plain HTTP) | **Fixed** | `install_flatpak_source` refuses `http://` URLs **before** anything runs; `install_flatpak_ref_file` parses the ref locally (`parse_flatpakref_file`), refuses plain-HTTP `Url=`/`RuntimeRepo=`, and re-checks the file against the reviewed fields; the `Url=` fallback install is **removed**; app-ID installs pass the Flathub remote explicitly (`flatpak install -y --user flathub -- <id>`); local paths absolute + `--` terminator; `flatpak info` pre-check replaces locale-text "already installed" matching. The window shows a review dialog (review text per file) before installing ref files. | `test_regressions.S12FlatpakHttpsOnlyTests`, `test_gates.FlatpakrefReviewBindingTests`, `FlatpakInstallCommandTests`. |
| **S4** (execute not bound to plan) | **Fixed** | New `appector/plans.py` (`Plan`, `check_apt_removal_plan`, `check_apt_upgrade_plan`). `prepare_apt_removal`/`prepare_apt_batch_preview`/`get_apt_autoremove_preview`/`prepare_available_updates` produce plans; `execute_apt_removal`, `execute_batch_removal`, `execute_apt_autoremove`, `execute_available_updates` require them and refuse when the re-simulation **grows**, contains critical packages, exceeds `APT_MAX_REMOVALS`, or — for updates — contains **any** removal. Autoremove previously had no gate at all. The helper re-checks the plan hash and re-simulates as root (ADR-0003). Batch removal never fabricates a plan. | `test_gates.AptRemovalPlanBindingTests` (incl. "grew by one package → refused", plan-less refusal, and the **mutation check**: with the gate disabled the removal runs — proving the test pins it), `AutoremovePlanTests`, `UpgradeRemovesNothingTests`, `BatchAptPlanTests`. |
| **S5** (pkexec env stripped) | **Fixed** | The helper sets `DEBIAN_FRONTEND=noninteractive`, `NEEDRESTART_MODE=a`, `APT_LISTCHANGES_FRONTEND=none`, `LC_ALL=C` and `-o Dpkg::Options::=--force-confold --force-confdef` itself. For the remaining direct pkexec calls, `_proc._wrap_privileged` inserts `env VAR=…` into the privileged argv (interim fix as the review prescribes). | `test_gates.HelperClientTests.test_pkexec_argv_carries_env…` asserts the helper path/argv through pkexec; helper env constants in `helper.py`. |
| **S6** (generic `pkexec sh -c`, silent unprivileged fallback) | **Fixed** | `helper/appector-helper` (installed root-owned at `/usr/lib/appector/appector-helper`) + `helper/com.appector.appector.policy` (one polkit action per operation with descriptive prompts) + `appector/helper_client.py` (JSON request on stdin, JSON-lines events each echoing a random per-run nonce; spoofed events dropped). `_build_privileged_script`, `_execute_privileged_script`, `_append_loop_commands`, `_parse_script_results` and every `pkexec sh -c` are **deleted**; user-Flatpak removals run per-app without a shell. Fail-fast: helper/pkexec missing → explicit refusal before any work (verified live in this sandbox: the deb test failed with the installation instructions until the boundary was mocked). | `tests/test_helper.py` (25 tests: hostile inputs — option-like names, newlines, oversized lists, unknown keys, bad nonce/plan-hash/tampered plan body; action/argv-mismatch rejection; nonce-spoofing drop). ADR-0003 written. |
| **S7** (runner defects) | **Fixed** | `_proc.py` rewritten around one `run()`: `Popen(start_new_session=True)`, `encoding="utf-8", errors="replace"` (S7b), LC_ALL=C default, process-group kill + root-side `timeout(1)` under pkexec (S7a), bounded output tail, `cancel` event, explicit `uses_apt` parameter (flatpak/snap never serialized — pinned by test). `_log_action` escapes `\n`/`\r`, uses `astimezone().isoformat()`, rotates at 1 MiB × 3. | `test_regressions` S7b/S7 log/LC_ALL/cancel/**timeout group-kill** tests; `test_safety` log-privacy tests. |
| **S8** (inconsistent file-removal validation) | **Fixed** | Standalone-AppImage unlink goes through `_validate_removal_file` (allowlist, ownership, regular-file); launcher ownership uses exact desktop-key values (`_desktop_key_values` — `x.AppImage` can no longer match `x.AppImage.bak`); symlink leaves resolve their **parent** and must remain inside an allowed root; scanners reject `X-AppManager-AppImage-Path` values that escape the managed dirs lexically. | `test_gates.StandaloneAppImageRemovalTests` (outside-roots refusal; in-home removal still works). |
| **S9** (backup hardening) | **Fixed** | Backup parents are created level-by-level refusing symlinks (`_ensure_backup_parents`); the backup itself now runs **as root inside the helper** into the invoking user's state dir (located via `PKEXEC_UID`/`SUDO_UID`) and is chowned to them — root-only conffiles can no longer block a purge; backups of purges that never start are discarded; `prune_old_purge_backups` deletes incomplete backups first and keeps the newest 10 *complete* ones; `skip_unreadable` is plumbed and logged for the non-helper path. | `test_residuals` backup-limit/pruning tests (updated to complete manifests); `test_helper` purge handler test; `test_residuals.test_purge_reverifies_after_backup` (helper-level). |
| **S10** (CSV formula injection) | **Fixed** | New `appector/export.py`: `csv_safe_value` prefixes `= + - @ \t \r` cells with `'`; atomic 0600 export replacing symlinks; extra columns (exec_name, details, is_duplicate, is_gui_app); window delegates to it. | `test_regressions.S10CsvInjectionTests` (escape + end-to-end CSV). |
| **S11** (policy quality) | **Fixed** | Snap runtime rule is exactly `^core[0-9]*$` (corectrl/core-keeper pass, core18… blocked); the substring "high risk" heuristic is replaced by dpkg metadata (`apt_metadata.get_apt_metadata_map`, one batched `dpkg-query`) plus a small exact-name list; Debian/derivative critical packages added (libc6, login, passwd, dbus, udev, gnome-core, task-*-desktop, …); `SINGLE == BATCH` removable managers consolidated to one constant; scanner snap-skip names now derive from the policy set; the appector-cascade check is pinned by test. | `test_regressions.S11PolicyQualityTests`, `test_gates.SnapCriticalCascadeTests`. |
| **S12** (release/supply chain) | **Fixed** | CI concurrency, dependabot, CODEOWNERS; the APT workflow now publishes the **attested release artifact** (downloaded + SHA256SUMS-verified, never rebuilt); `Valid-Until` (7 days) + `Acquire-By-Hash: yes` with by-hash index copies (checksummed in Release); pool retention carries the previous repository forward and trims to the newest 3; SECURITY.md documents key rotation and revocation with a dual-key transition window. |

### Functional correctness (section 6)

| ID | Status | Notes |
|---|---|---|
| F1 | **Fixed** | `scan_flatpak` passes the computed `installed_at` (was discarded). |
| F2 | **Fixed** | Real display names from `flatpak list --columns=name,…`; `exec_name` from the exported .desktop `Exec` (or empty); duplicate identity for Flatpak/Snap is the package ID (name matches only across different managers); `duplicate_with` records what each duplicate collides with. |
| F3 | **Fixed** | `normalize_exec` rewritten on shlex + a wrapper/subcommand state machine (env, sh -c, flatpak/snap run, gtk-launch, gamemoderun, pkexec, dbus-launch, /usr/bin/env, quoted paths, field codes). |
| F4 | **Fixed** | `scan_apt` uses `${db:Status-Abbrev}` == `ii` and keys by `name:arch`; owner resolution maps `dpkg -S` output to scanned identities. |
| F5 | **Fixed** | history.log parsed with a `name[:arch] (version[, automatic])` regex (no more `automatic)` package); dpkg.log keyed `name:arch`; fallback to `/var/lib/dpkg/info/<pkg>.list` mtimes for rotated-out entries. |
| F6 | **Fixed (engine), UI banner deferred** | Batched `dpkg-query -S` ownership (one call per directory), timeouts on every scanner subprocess, parallel scan, snap-icon caching, deterministic ordering with package-name-preferred .desktop choice, `ScanResult(apps, source_status)` with ok/tool-missing/failed per source; marks are no longer pruned for sources that failed to scan. The Adw.Banner presentation of `source_status` is deferred (§4). |
| F7 | **Fixed** | Group-aware parsing via `configparser(interpolation=None, strict=False)`; `[Desktop Action]` groups ignored; TryExec, OnlyShowIn/NotShowIn, `Type=Application` and localized `Name` honoured. |
| F8 | **Fixed** | Generic Origin/Label/Site matching (Debian, Ubuntu, Raspbian, Mint, Pop!_OS…); PPAs matched on `ppa.launchpad(content).net`; python3-apt stays optional (Recommends declared in the packaging TODO §4). |
| F9 | **Fixed (main items)** | Type-1/type-2 magic validation (a renamed ELF is refused), content-hash install identity, atomic copy + launcher writes (fsync + os.replace, no `.part` residue); `appimage_missing_fuse_hint()` documents `--appimage-extract-and-run`. Icon/name extraction and libfuse2 detection remain follow-ups. 7 new tests incl. real-file atomicity. |
| F10 | **Fixed (main points)** | CLI-only executables `is_gui_app=False`; binary dirs generalized (~/.cargo/bin, ~/go/bin, ~/.npm-global/bin; `.opencode/bin` dropped from discovery, retained in the removal allowlist); removal paths derived from the resolved Exec target only; Chrome/Steam/Wine launchers classified. `/opt`+`/usr/local` checkbox-disable deferred (§4). |
| F11 | **Fixed** | C-locale runner by default + `flatpak info` exit-code pre-checks; `installation` recorded on the entry and used for scope decisions (`--installation=NAME`); "No refs found" hint retained as a fallback only. |
| F12 | **Fixed (main items)** | The update check refreshes APT lists first (no stale cache), surfaces needrestart output, and reports `/var/run/reboot-required` (+ affected packages) in the notes; update execution is cancellable via the TaskProgressDialog (U3). Parallel checks and tab-separated parsing remain follow-ups. |
| F13 | **Fixed** | `review_deb_batch` extracts per-package facts via `dpkg-deb --contents`/`--ctrl-tarfile` (maintainer scripts, setuid/setgid files, /etc and /etc/apt paths, systemd units, conffiles) into `DebInstallReview.package_records`; `_security_findings` is a pure function. Verified against real .debs built with dpkg-deb in CI-like tests. |
| F14 | **Fixed** | `appector/paths.py` honours `XDG_STATE_HOME`/`XDG_CACHE_HOME`, migrates the legacy `app-manager` state once (copy + keep-old-dir), shared private-dir validation. |
| F15 | **Fixed** | One definition each in `policy.py` for `SAFE_PACKAGE_ID_RE`, dedup/split/parse/format helpers; enforced by `test_source_integrity`. |
| F16 | **Fixed** | Leftover scanner has error handling + `errors="replace"`; batch progress uses dynamic totals (always reaches 100%, blocked groups don't stall); APT backup failure fails only the APT group; details/batch Purge consistency and scope labels for flatpak cleanup deferred (§4). |

### GUI/UX (section 7)

| ID | Status | Notes |
|---|---|---|
| U1 | **Fixed** | `appector/ui/confirm_dialog.py` `ConfirmPlanDialog(Adw.Dialog)`: scrollable body (buttons always reachable), "You selected" → "Also removed" (**never truncated**, selectable/copyable) → "Files that will be deleted" (exact paths) → risk badges → notes; confirm disabled while loading/blocked; default focus Cancel; destructive/suggested appearance. Batch removal uses it. |
| U2 | **Fixed (batch + purge paths)** | Disk-space estimates run in workers (`set_disk_estimate_async`) with a spinner row; the APT simulation for the leftover purge still runs on confirmation (kept synchronous deliberately so the preview matches the selection — the blocking call now sits behind a progress dialog instead of freezing the list). |
| U3 | **Fixed** | `appector/ui/tasks.py` `run_async` (try/except → error dialog + traceback in the activity log; always idle_add; controls re-enabled) and `TaskProgressDialog` (Cancel via `CancelToken` → S7 process-group kill; Close always enabled once ended; batched output flushes; bounded buffer). All 17 worker launches in `window.py` converted. |
| U4 | **Mostly done** | New dialogs are pure `Adw.Dialog` (confirm, progress, review, details, install-dialog builder); minimum libadwaita decided as **1.5** and declared in `Depends: libadwaita-1-0 (>= 1.5)` and the metainfo requires block; `show_message`/`_show_confirm_dialog` remain for informational messages. |
| U5 | **Fixed** | Bare `Delete` → `<Ctrl>Delete`; mark-all/remove-marked refuse while an editable has focus. |
| U6 | **Fixed** | `Gtk.DialogError.DISMISSED/CANCELLED` ignored for the export save dialog and the multi-open callback. |
| U7 | **Fixed** | Search text markup-escaped in the empty-page description. |
| U8 | **Fixed** | Adw.Banner names failed/missing sources with a Details dialog; `rebuild_list` uses `Gio.ListStore.splice` and restores selection by identity + scroll position proportionally; search input debounced (250 ms) and the entry expands; marks preserved for sources that failed to scan. |
| U9 | **Fixed (chips + details; columns/layout remain)** | PACKAGE SOURCE filter chips with live counts; details window redesigned as an Adw.Dialog card (risk badge, duplicate collisions, selectable values). Size/installed-date columns, adaptive layout and the log viewer remain follow-ups (§4). |
| U10 | **Fixed** | `ui/review_dialogs.py`: DebReviewDialog shows one framed section per package (metadata grid, security warning rows, exact file lists) plus the full simulation output; FlatpakRefReviewDialog replaces the plain-text flatpakref review. |
| U11 | **Scaffolded** | `appector/i18n.py` gettext wiring bound at startup (`APECTOR_LOCALEDIR` override), German .desktop/metainfo/polkit strings shipped; marking all UI strings for extraction and About-window migration remain follow-ups. |

### Packaging / tests / architecture (sections 8–10)

| Item | Status | Notes |
|---|---|---|
| File association / `%F` / MimeType / Keywords / HANDLES_OPEN / CLI files / packaging | **Done** | `main.py` parses files, `app.py` uses `HANDLES_OPEN` + `do_open`; the .desktop ships `Exec=appector %F` + MimeType (vnd.debian.binary-package, vnd.flatpak.ref, x-appimage, x-executable) + Keywords; metainfo, polkit policy and root-owned helper install via build-deb.sh (built and inspected); postinst/triggers refresh caches. |
| Tests for scanners, updates parsing, flatpak helpers, appimage removal negatives, updater, `_proc` | **Done** | 76 new tests across `test_regressions.py`, `test_gates.py`, `test_helper.py` + rewritten scanner fixtures. |
| Property/fuzz tests (SAFE_PACKAGE_ID_RE, conffile parsing, helper schema) | **Done** | `SafePackageIdPropertyTests`, conffile regex tests, `test_helper` schema battery. |
| Headless GTK smoke test under xvfb, VM integration job | **Deferred** | Needs CI runners with a display/VM (§4). |
| CI: ruff, coverage floor, concurrency, dependabot, policy validation | **Done** | `ci.yml`, `pyproject.toml`; lint also caught two real bugs during the pass (an undefined `events` name in the snap handler, and an exception-variable capture in `run_async`). |
| `test_source_integrity` extension (no duplicated helpers) | **Done** | `test_helpers_are_not_duplicated_across_modules`. |
| Split `window.py`/`actions.py` (section 10) | **Done (per ADR-0002's plan)** | New code lives in new modules (`paths`, `plans`, `apt_metadata`, `export`, `helper`, `helper_client`, `managers/flatpak.py` (808 lines, re-exported by `actions`), `ui/` (progress, confirm, review, details, install dialogs, tasks)). `actions.py` 3794→3077 lines; `window.py` 5660→5482 with the install dialog extracted; `show_message`-style legacy blocks remain deliberately. |
| Result dataclasses instead of `(bool, str)` | **Partial** | `RunResult`, `HelperResult`, `Plan`, `ScanResult` exist for all new code; the legacy `(bool, str)` surface remains for the old flows. |

---


## 1a. Milestone completion (continuation pass)

The continuation pass (commits `c724427`..`d7ed5a2`) cleared every
remaining milestone from the review's phase plan:

- **Phase 3 finished**: scan-status banner + splice/selection/scroll
  restore + search debounce (F6/U8); the residual-purge confirmation
  runs its simulation off-thread and presents the full plan (U1/U2).
- **Phase 4 finished**: packaging (helper, polkit policy, metainfo,
  MimeType/Keywords, postinst/triggers, libadwaita ≥ 1.5, version
  0.3.0 with a release-notes-compatible changelog section); the
  structured .deb review with real-.deb integration tests (F13/U10);
  manager filter chips and the details-window redesign (U9); AppImage
  hardening, configurable manual scan dirs, update-check refresh +
  reboot notices, i18n scaffolding (F9/F10/F12/U11).
- **Phase 5 finished**: `managers/flatpak.py` + `ui/install_dialog.py`
  extraction per ADR-0002's plan; the S12 release-pipeline items
  (single attested build consumed by the APT job, pool retention,
  `Valid-Until`, `Acquire-By-Hash` with checksummed by-hash indexes,
  key rotation/revocation documentation).
- **Bonus live-verified fixes** found while testing against the real
  tools in this sandbox: `get_package_owners` mixed `stderr=` with
  `capture_output=True` (subprocess raised `ValueError` on **every**
  call — APT desktop-file ownership lookups always failed silently) and
  its parser was inverted (`dpkg: /path` keyed as `dpkg`), so APT
  desktop files were treated as unowned and listed as Manual entries.
  Both verified against the real `dpkg-query` and pinned by tests.

The only deferred item left is the xvfb/VM CI jobs, which need runner
infrastructure a code pass cannot validate (documented in §4).

## 2. VERIFY items — how each was handled

| Item | Method | Outcome |
|---|---|---|
| S2 (multiarch) | Unit-level reproduction with mocked `dpkg-query` output (`rc libfoo:i386`, `ii libfoo:amd64`) — the exact scenario the review described. | **Reproduced and fixed**; the same mock now asserts the block. No multiarch VM was available, so no live-dpkg reproduction was possible. |
| S5 (pkexec env stripping) | No pkexec in the remediation sandbox → could not execute. Verified against documented pkexec behaviour (sanitized environment) and fixed structurally: the helper sets its own env; direct pkexec calls carry `env VAR=…` in argv. | **Confirmed by code reading; fix is testable without pkexec** (argv asserted in `test_gates`). |
| U5 (accelerator capture phase) | No GTK in the sandbox → not executed. Fix implemented per review's own analysis (`<Ctrl>Delete` + editable-focus guards); the guard logic is plain Python. | **Fixed by construction; runtime verification deferred to the xvfb smoke test.** |
| U7 (markup parsing) | No GTK in the sandbox → not executed. Fix is `GLib.markup_escape_text`, which exists precisely for this. | **Fixed by construction.** |
| S7a (timeout kill of setuid pkexec → PermissionError) | Partially verifiable without pkexec: the new runner catches `PermissionError`/`ProcessLookupError` everywhere it signals, kills the group then the child, and wraps pkexec in a root-side `timeout(1)`. | **Fixed by construction + group-kill test against a plain `sh` tree; the setuid-specific path needs a VM.** |

Environment used for everything executable: Python 3.12.16, Linux
5.10.134, no GTK/gi, no pkexec, no flatpak/snap/dpkg — hence all execution
evidence is stdlib-level repros and mocked boundaries, exactly as the
review's own "verified by execution" list did it.

---

## 3. User-visible behaviour changes

1. **Privileged operations now require the packaged helper.** Running from
   a source checkout without the .deb installed, `pkexec`-requiring
   operations refuse with installation instructions instead of silently
   running unprivileged and failing late. (`APECTOR_HELPER` overrides the
   helper path for development.)
2. State moves from `~/.local/state/app-manager` to
   `~/.local/state/appector` (XDG-aware), migrated once; the old directory
   is kept. Staged .deb reviews move to `~/.cache/appector` similarly.
3. The Remove-marked shortcut is now `Ctrl+Delete` (was bare `Delete`).
4. Confirmation dialogs are scrollable plan views; the dependency cascade
   is always shown in full; the disk estimate appears asynchronously.
5. Progress windows are cancellable where the underlying command supports
   it (updates, batch removal) and always closable after completion.
6. Plain-HTTP Flatpak references (URLs and repo URLs inside ref files) are
   refused.
7. Residual purges are blocked by multiarch conflicts instead of purging
   the installed architecture's package.
8. Polkit prompts now name the exact Appector operation.
9. Flatpak apps show their real display names; "Client"-style duplicate
   false positives are gone; CLI-only manual executables are listed as
   non-GUI items.
10. CSV/JSON exports gain columns and formula-injection escaping.
11. `appector --version` and the root-refusal work without GTK; files can
    be passed on the command line.

## 4. Residual risks and follow-ups

| Risk / gap | Why it remains | Suggested follow-up |
|---|---|---|
| GTK code verified only by `compileall` + lint | No display in the remediation environment | Add the xvfb smoke test (review §9.4) and the VM integration job (§9.5: real .deb install/remove through the helper with a mocked polkit agent, real rc purge incl. a multiarch case). |
| S12 release pipeline (single attested build consumed by the APT job, pool retention, `Valid-Until`, `Acquire-By-Hash`, key rotation/revocation) | Pipeline-only change; no signing infra in this environment | Implement as its own PR per review §11 item 21. |
| F9 AppImage install improvements, F12 updates UX, F13 structured deb review, U8/U9/U10/U11 UI work, U4 migration remainder | Scope: this pass prioritized every security/data-safety finding and the top three UX-correctness items | Follow the review's Phase 3–4 remaining bullets; the new `ui/` package is the landing zone. |
| `apt_metadata` risk metadata is not yet surfaced in the list UI | `policy.get_removal_risk(metadata=…)` is implemented and tested; the window still passes no metadata | Wire `get_apt_metadata_map` into `populate_apps` when the sidebar/detail badges land (U9). |
| Helper `flatpak remote-add --system` and system flatpak installs still use direct pkexec | Not in the review's helper action list | The direct paths now fail fast with an explicit message when pkexec is missing (no silent unprivileged fallback); adding helper actions for them is a follow-up. |
| Coverage floor is 55 % | GUI modules are excluded headless | Raise as the GUI smoke tests land. |

## 5. How to verify locally

```sh
python3 -m unittest discover -s tests -v     # 195 tests, 11 GTK skips
ruff check appector tests                    # clean
python3 -m compileall -q appector           # syntax check for the GTK modules
./scripts/build-deb.sh                      # package (incl. helper + policy + metainfo)
dpkg-deb -c dist/appector_0.3.0_all.deb     # verify /usr/lib/appector/appector-helper 0755
```
