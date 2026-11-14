#!/usr/bin/env python3
"""Patch 5 (Phase 2.10): route every privileged operation through the
helper; remove pkexec sh -c and the text protocol (S1, S6)."""
from pathlib import Path


def patch(path, replacements):
    source = Path(path).read_text()
    for old, new in replacements:
        if new in source:
            continue  # idempotent
        if source.count(old) < 1:
            raise SystemExit(f"{path}: ANCHOR NOT FOUND:\n{old[:200]}")
        source = source.replace(old, new, 1)
    Path(path).write_text(source)
    print(f"{path} patched")


# ------------------------------------------------------------------
# actions.py
# ------------------------------------------------------------------
patch("appector/actions.py", [
    # import the client
    (
        """from .paths import appimage_dir, legacy_appimage_dir""",
        """from . import helper_client
from .helper_client import HelperUnavailable
from .paths import appimage_dir, legacy_appimage_dir""",
    ),
    # ----------------------------------------------------------------
    # S1: install_deb_batch runs the reviewed install through the helper
    # ----------------------------------------------------------------
    (
        '''    pkexec_command = shutil.which("pkexec")
    if os.geteuid() != 0 and not pkexec_command:
        return False, (
            "pkexec is required to authorize a system-wide Debian package "
            "installation. Appector itself must not be run as root."
        )

    cmd = [
        apt_get_command,
        "install",
        "-y",
        "--",
    ] + [str(p) for p in deb_paths]

    if pkexec_command:
        cmd = [pkexec_command] + cmd

    _log_action(f"DEB_BATCH_INSTALL_START cmd={shlex.join(cmd)}")

    success, message = _run_command_stream(cmd, output_callback)''',
        '''    # S1: the privileged install runs through the helper, which copies
    # each reviewed file into root-owned staging *while hashing it* and
    # installs only the copies — root never opens a user-writable path
    # after verification.
    helper_files = [
        {"path": str(path), "sha256": expected_hash}
        for path, expected_hash in zip(staged_paths, review.hashes)
    ]

    _log_action(
        "DEB_BATCH_INSTALL_START helper=install-deb "
        f"files={len(helper_files)}"
    )

    try:
        result = helper_client.install_deb(helper_files, output_callback)
    except HelperUnavailable as error:
        return False, f"{error}"
    except OSError as error:
        return False, f"Could not start the privileged installer: {error}"

    success = result.ok
    message = result.message or ("Installed." if success else "Installation failed.")''',
    ),
    # ----------------------------------------------------------------
    # S4/S6: execute_apt_removal through the helper
    # ----------------------------------------------------------------
    (
        '''    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"
    pkexec_command = shutil.which("pkexec")
    backup_note = ""
    if purge:
        try:
            backup_dir, backed_up_count = _backup_residual_conffiles(
                removed_packages
            )
        except OSError as error:
            _log_action(f"APT_REMOVE_BLOCKED {key} reason=purge-backup-failed")
            return False, (
                "Purge was not started because Appector could not securely back "
                f"up the affected configuration files.\\n\\n{error}"
            )
        backup_note = _purge_backup_note(backup_dir, backed_up_count)

    apt_action = "purge" if purge else "remove"

    if pkexec_command:
        cmd = [
            pkexec_command,
            apt_get_command,
            apt_action,
            "-y",
            "--",
            package_id,
        ]
    else:
        cmd = [
            apt_get_command,
            apt_action,
            "-y",
            "--",
            package_id,
        ]

    _log_action(f"APT_REMOVE_START {key} cmd={shlex.join(cmd)}")

    success, message = _run_command(cmd)''',
        '''    # S6: the removal runs through the privileged helper, which
    # re-validates the plan and the policy as root. A purge's conffile
    # backup is made there too, as root (S9).
    _log_action(f"APT_REMOVE_START {key} helper=remove-apt purge={purge}")

    try:
        result = helper_client.remove_apt(
            [package_id],
            plan=plan,
            purge=purge,
            output_callback=None,
        )
    except HelperUnavailable as error:
        return False, f"{error}"
    except OSError as error:
        return False, f"Could not start the privileged helper: {error}"

    success = result.ok
    message = result.message or ("Removed." if success else "Removal failed.")
    backup_note = ""
    for event in result.events:
        if event.get("type") == "backup":
            backup_note = _purge_backup_note(
                event.get("path"), int(event.get("files", 0) or 0)
            )''',
    ),
    # ----------------------------------------------------------------
    # S6: batch removal — helper per group, no shell script
    # ----------------------------------------------------------------
    (
        '''    # ------------------------------------------------------------
    # User Flatpak removals
    # ------------------------------------------------------------
    if user_flatpaks:
        report(f"Removing {len(user_flatpaks)} Flatpak app(s)…")

        script_results, output = _execute_user_flatpak_batch(user_flatpaks)

        for app in user_flatpaks:
            package_id = getattr(app, "package_id", "") or ""
            key = ("Flatpak", package_id)

            success, message = script_results.get(
                key,
                (False, output if output else "Flatpak removal failed."),
            )

            results_by_key[app_key(app)] = (success, message)''',
        '''    # ------------------------------------------------------------
    # User Flatpak removals (no privilege, one command per app)
    # ------------------------------------------------------------
    if user_flatpaks:
        report(f"Removing {len(user_flatpaks)} Flatpak app(s)…")

        for app in user_flatpaks:
            package_id = getattr(app, "package_id", "") or ""
            if not _is_safe_package_id(package_id):
                results_by_key[app_key(app)] = (
                    False,
                    "Unsafe or invalid Flatpak package ID.",
                )
                continue
            installation = str(getattr(app, "installation", "") or "").strip().lower()
            scope = ["--user"] if installation in {"user", ""} else [
                f"--installation={installation}"
            ]
            cmd = [
                shutil.which("flatpak") or "flatpak",
                "uninstall",
                "-y",
                *scope,
                "--",
                package_id,
            ]
            returncode, output = _run_command_raw(cmd, timeout=300)
            success = returncode == 0
            results_by_key[app_key(app)] = (
                success,
                output if success else (
                    output or "Flatpak removal failed."
                ),
            )''',
    ),
    (
        '''        script = _build_privileged_script(
            snap_exe,
            snap_ids,
            flatpak_exe,
            system_flatpak_ids,
            apt_get_exe,
            apt_ids,
            purge=purge,
        )

        if not script:
            for app in privileged_apps:
                results_by_key[app_key(app)] = (
                    False,
                    "Could not build privileged removal script.",
                )
        else:
            script_results, output = _execute_privileged_script(
                script,
                privileged_expected,
            )

            for app in privileged_apps:
                manager = getattr(app, "manager", "")
                package_id = getattr(app, "package_id", "") or ""

                key = (manager, package_id)

                success, message = script_results.get(
                    key,
                    (False, output if output else "Privileged removal failed."),
                )
                if backup_note:
                    message = f"{message}\\n\\n{backup_note}"

                results_by_key[app_key(app)] = (success, message)''',
        '''        # S6: the privileged groups run through the dedicated helper,
        # one named action per manager; the pkexec sh -c text protocol is
        # gone. Failures are isolated per group.
        def fail_group(apps, message):
            for app in apps:
                results_by_key[app_key(app)] = (False, message)

        if snap_ids:
            try:
                result = helper_client.remove_snap(
                    snap_ids, output_callback=None
                )
                snap_message = result.message or (
                    "Removed." if result.ok else "Snap removal failed."
                )
                for app in snaps:
                    if app_key(app) not in results_by_key:
                        results_by_key[app_key(app)] = (result.ok, snap_message)
            except (HelperUnavailable, OSError) as error:
                fail_group(
                    [app for app in snaps if app_key(app) not in results_by_key],
                    str(error),
                )

        if system_flatpak_ids:
            installations = {
                str(getattr(app, "installation", "") or "").strip()
                for app in system_flatpaks
            }
            installation = next(iter(installations)) if len(installations) == 1 else None
            try:
                result = helper_client.remove_flatpak_system(
                    system_flatpak_ids,
                    installation=installation,
                    output_callback=None,
                )
                flatpak_message = result.message or (
                    "Removed." if result.ok else "Flatpak removal failed."
                )
                for app in system_flatpaks:
                    if app_key(app) not in results_by_key:
                        results_by_key[app_key(app)] = (result.ok, flatpak_message)
            except (HelperUnavailable, OSError) as error:
                fail_group(
                    [app for app in system_flatpaks
                     if app_key(app) not in results_by_key],
                    str(error),
                )

        if apt_ids:
            plan_for_helper = apt_plan
            if plan_for_helper is None and purge and "removed_packages" in dir():
                plan_for_helper = None
            try:
                result = helper_client.remove_apt(
                    apt_ids,
                    plan=plan_for_helper if plan_for_helper is not None else Plan(
                        action="batch-remove",
                        package_ids=list(apt_ids),
                        removed_preview=list(removed_packages),
                    ),
                    purge=purge,
                    output_callback=None,
                )
                apt_message = result.message or (
                    "Removed." if result.ok else "APT removal failed."
                )
                events_note = ""
                for event in result.events:
                    if event.get("type") == "backup":
                        events_note = _purge_backup_note(
                            event.get("path"), int(event.get("files", 0) or 0)
                        )
                if events_note:
                    apt_message = f"{apt_message}\\n\\n{events_note}"
                if backup_note:
                    apt_message = f"{apt_message}\\n\\n{backup_note}"
                for app in apt_valid_apps:
                    if app_key(app) not in results_by_key:
                        results_by_key[app_key(app)] = (result.ok, apt_message)
            except (HelperUnavailable, OSError) as error:
                fail_group(
                    [app for app in apt_valid_apps
                     if app_key(app) not in results_by_key],
                    str(error),
                )''',
    ),
    # drop the now-dead purge-backup-block before the privileged section
    (
        '''        backup_note = ""
        if purge and apt_ids:
            try:
                backup_dir, backed_up_count = _backup_residual_conffiles(
                    removed_packages
                )
                backup_note = _purge_backup_note(backup_dir, backed_up_count)
            except OSError as error:
                # F16: a failed APT conffile backup fails only the APT
                # group; Snap and system Flatpak removals still run.
                message = (
                    "APT removal was not started because Appector could "
                    "not securely back up the APT configuration files.\\n\\n"
                    f"{error}"
                )
                for app in apt_valid_apps:
                    results_by_key[app_key(app)] = (False, message)
                apt_ids = []
                if not (snap_ids or system_flatpak_ids):
                    privileged_apps = []
                    privileged_expected = []''',
        '''        backup_note = ""
        # S9: the conffile backup for a batch purge is made inside the
        # helper (as root), together with the removal, so a root-only
        # conffile can no longer block the purge or split the transaction.''',
    ),
    # ----------------------------------------------------------------
    # S6: remove the shell-script machinery
    # ----------------------------------------------------------------
    (
        '''# ------------------------------------------------------------
# Privileged grouped script
# ------------------------------------------------------------

def _build_privileged_script(''',
        '''# ------------------------------------------------------------
# Privileged operations now run through appector-helper (ADR-0003).
# ------------------------------------------------------------

def _build_privileged_script(''',
    ),
])

# Delete the dead script functions entirely.
source = Path("appector/actions.py").read_text()
for function_name in (
    "_build_privileged_script",
    "_execute_privileged_script",
    "_append_loop_commands",
    "_parse_script_results",
    "_execute_user_flatpak_batch",
):
    marker = f"def {function_name}("
    start = source.find(marker)
    if start < 0:
        raise SystemExit(f"function not found: {function_name}")
    # Walk back to the start of the def line's indentation (module level).
    previous_newline = source.rfind("\n", 0, start) + 1
    # Find the end: next line at column 0 starting with "def " or "# ----"
    # or EOF.
    rest = source[start:]
    end_match = None
    for candidate in (
        "\n# ------------------------------------------------------------",
        "\ndef ",
    ):
        index = rest.find(candidate, 1)
        if index != -1 and (end_match is None or index < end_match):
            end_match = index
    end = start + (end_match if end_match is not None else len(rest))
    source = source[:previous_newline] + source[end + 1:]
    print(f"removed {function_name}")

# Fix autoremove + updates to use the helper.
patch("appector/actions.py", [
    (
        '''    cmd = [apt_get_command, "autoremove", "-y"]

    if pkexec_command:
        cmd = [pkexec_command] + cmd

    _log_action(f"APT_AUTOREMOVE_START cmd={shlex.join(cmd)}")
    success, message = _run_command_stream(
        cmd,
        output_callback=output_callback,
        timeout=900,
        uses_apt=True,
    )''',
        '''    _log_action("APT_AUTOREMOVE_START helper=apt-autoremove")
    try:
        result = helper_client.apt_autoremove(
            plan, output_callback=output_callback
        )
    except HelperUnavailable as error:
        return False, f"{error}"
    except OSError as error:
        return False, f"Could not start the privileged helper: {error}"
    success = result.ok
    message = result.message or ("Autoremove finished." if success else "Autoremove failed.")''',
    ),
    (
        '''        if (manager == "APT" or manager == "Snap" or scope == "system") and os.geteuid() != 0:
            pkexec_command = shutil.which("pkexec")
            if not pkexec_command:
                failures.append(
                    f"{manager}: pkexec is required to update system packages."
                )
                continue
            cmd.insert(0, pkexec_command)

        title = f"{manager}{f' ({scope})' if scope else ''}"
        if output_callback:
            output_callback(f"Updating {title}: {', '.join(package_ids)}")
        success, output = _run_command_stream(cmd, output_callback)''',
        '''        title = f"{manager}{f' ({scope})' if scope else ''}"
        if output_callback:
            output_callback(f"Updating {title}: {', '.join(package_ids)}")
        if manager == "APT" or (manager == "Flatpak" and scope == "system"):
            # S5/S6: privileged updates go through the named helper action.
            try:
                if manager == "APT":
                    result = helper_client.apt_upgrade(
                        package_ids,
                        plan=Plan(
                            action="apt-upgrade",
                            package_ids=package_ids,
                            removed_preview=[],
                        ),
                        output_callback=output_callback,
                    )
                else:
                    result = helper_client.flatpak_update_system(
                        package_ids, output_callback=output_callback
                    )
            except (HelperUnavailable, OSError) as error:
                failures.append(f"{title}: {error}")
                continue
            success, output = result.ok, result.message
        else:
            success, output = _run_command_stream(cmd, output_callback)
            output = output if output else ""''',
    ),
])

# ------------------------------------------------------------------
# residual.py: the privileged purge tail runs through the helper
# ------------------------------------------------------------------
patch("appector/residual.py", [
    (
        '''from ._proc import _log_action, _run_command_raw, _run_command_stream''',
        '''from . import helper_client
from .helper_client import HelperUnavailable
from ._proc import _log_action, _run_command_raw''',
    ),
    (
        '''    apt_get_command = shutil.which("apt-get") or "/usr/bin/apt-get"
    pkexec_command = shutil.which("pkexec")

    try:
        backup_dir, backed_up_count = _backup_residual_conffiles(
            packages_to_purge,
            skip_unreadable=skip_unreadable_backup,
        )
    except OSError as error:
        _log_action(f"LEFTOVER_PURGE_BLOCKED backup_error={error}")
        return False, (
            "Purge was not started because Appector could not securely back up "
            f"the remaining configuration files.\\n\\n{error}"
        )

    def blocked_before_execution(reason, message):
        # S9: the purge never runs, so the source conffiles are untouched and
        # the just-made backup is redundant — remove it instead of letting
        # blocked attempts accumulate.
        _log_action(f"LEFTOVER_PURGE_BLOCKED reason={reason} backup={backup_dir}")
        _discard_backup(backup_dir)
        return False, message

    # Re-verify after the (potentially slow) backup: if a package left rc
    # state meanwhile (e.g. reinstalled), purging it now would remove an
    # installed package, not just residual config.
    try:
        status_after_backup = _dpkg_status_map()
    except (OSError, subprocess.TimeoutExpired) as error:
        return blocked_before_execution(
            "reverify-failed",
            "Purge was not started because residual state could not be "
            f"re-verified after backup.\\n\\n{error}",
        )
    residual_after_backup = {
        package
        for package, status in status_after_backup.items()
        if status[:2] == "rc"
    }
    changed = [p for p in packages_to_purge if p not in residual_after_backup]
    if changed:
        return blocked_before_execution(
            "state-changed",
            "Purge was not started because these packages left "
            "residual-configuration state after the backup (they may have been "
            "reinstalled):\\n\\n" + "\\n".join(changed),
        )
    # S2 re-check: another arch instance may have been (re)installed while
    # the backup ran.
    late_conflicts = _multiarch_conflicts(packages_to_purge, status_after_backup)
    if late_conflicts:
        return blocked_before_execution(
            "multiarch-after-backup",
            "Purge was not started because another architecture instance of "
            "these packages was installed while the backup was running:\\n\\n"
            + "\\n".join(
                f"• {package}: {', '.join(others)}"
                for package, others in late_conflicts.items()
            ),
        )

    cmd = [
        apt_get_command,
        "purge",
        "-y",
        "--",
    ] + packages_to_purge

    if pkexec_command:
        cmd = [pkexec_command] + cmd

    _log_action(f"LEFTOVER_PURGE_START cmd={shlex.join(cmd)}")

    success, message = _run_command_stream(
        cmd,
        output_callback,
        uses_apt=True,
    )

    notes = [f"{_purge_backup_note(backup_dir, backed_up_count)}"]
    notes.append(
        "This backup covers dpkg-registered conffiles only; maintainer purge "
        "scripts may have removed additional files. There is no automatic "
        "restore: inspect the manifest before copying anything back."
    )
    if critical_residuals:
        notes.append(
            "Note: critical/system packages were among the purged residuals:\\n"
            + _format_package_list(critical_residuals)
            + "\\nReinstalling them will recreate default configuration; your "
            "previous customizations remain only in the backup above."
        )
    # Prune old backups so repeated purges do not fill the home directory.
    try:
        removed, _kept = prune_old_purge_backups()
        if removed:
            notes.append(
                f"Pruned {removed} older purge backup(s); newest "
                f"{RESIDUAL_PURGE_BACKUP_KEEP} retained."
            )
    except Exception:
        pass

    if success:
        _log_action("LEFTOVER_PURGE_SUCCESS")
        # Post-verify: residuals should be gone now.
        try:
            remaining = _dpkg_residual_package_set()
            lingering = [p for p in packages_to_purge if p in remaining]
        except (OSError, subprocess.TimeoutExpired):
            lingering = []
        if lingering:
            message += (
                "\\n\\nWarning: these packages are still in residual state:\\n"
                + "\\n".join(lingering)
            )
        message += "\\n\\n" + "\\n\\n".join(notes)
        if skipped_packages:
            message += (
                "\\n\\nSkipped packages no longer in residual-configuration state:\\n"
                + "\\n".join(skipped_packages)
            )
    else:
        _log_action(f"LEFTOVER_PURGE_FAILED backup={backup_dir} error={message}")
        message += "\\n\\n" + "\\n\\n".join(notes)

    return success, message''',
        '''    # The privileged tail — conffile backup as root (S9), rc/multiarch
    # re-verification, plan binding, and the purge itself — runs inside the
    # helper (ADR-0003). The plan binds the purge to the simulated set.
    plan = Plan(
        action="purge",
        package_ids=list(packages_to_purge),
        removed_preview=list(sim_purged),
        text="",
    )

    _log_action(
        f"LEFTOVER_PURGE_START helper=purge-residual packages={len(packages_to_purge)}"
    )

    try:
        result = helper_client.purge_residual(
            packages_to_purge,
            plan=plan,
            skip_unreadable_backup=skip_unreadable_backup,
            output_callback=output_callback,
        )
    except HelperUnavailable as error:
        _log_action("LEFTOVER_PURGE_BLOCKED reason=helper-unavailable")
        return False, f"{error}"
    except OSError as error:
        _log_action("LEFTOVER_PURGE_BLOCKED reason=helper-start-failed")
        return False, f"Could not start the privileged helper: {error}"

    success = result.ok
    message = result.message

    notes = []
    for event in result.events:
        if event.get("type") == "backup":
            notes.append(
                _purge_backup_note(
                    event.get("path"), int(event.get("files", 0) or 0)
                )
            )
    notes.append(
        "This backup covers dpkg-registered conffiles only; maintainer purge "
        "scripts may have removed additional files. There is no automatic "
        "restore: inspect the manifest before copying anything back."
    )
    if critical_residuals:
        notes.append(
            "Note: critical/system packages were among the purged residuals:\\n"
            + _format_package_list(critical_residuals)
            + "\\nReinstalling them will recreate default configuration; your "
            "previous customizations remain only in the backup above."
        )
    # Prune old backups so repeated purges do not fill the home directory.
    try:
        removed, _kept = prune_old_purge_backups()
        if removed:
            notes.append(
                f"Pruned {removed} older purge backup(s); newest "
                f"{RESIDUAL_PURGE_BACKUP_KEEP} retained."
            )
    except Exception:
        pass

    if success:
        _log_action("LEFTOVER_PURGE_SUCCESS")
        # Post-verify: residuals should be gone now.
        try:
            remaining = _dpkg_residual_package_set()
            lingering = [p for p in packages_to_purge if p in remaining]
        except (OSError, subprocess.TimeoutExpired):
            lingering = []
        if lingering:
            message += (
                "\\n\\nWarning: these packages are still in residual state:\\n"
                + "\\n".join(lingering)
            )
        if notes:
            message += "\\n\\n" + "\\n\\n".join(notes)
        if skipped_packages:
            message += (
                "\\n\\nSkipped packages no longer in residual-configuration state:\\n"
                + "\\n".join(skipped_packages)
            )
    else:
        _log_action(f"LEFTOVER_PURGE_FAILED error={message[:200]}")
        if notes:
            message += "\\n\\n" + "\\n\\n".join(notes)

    return success, message''',
    ),
])

# residual.py needs the Plan import
patch("appector/residual.py", [
    (
        """from .paths import is_acceptable_private_dir, state_dir""",
        """from .paths import is_acceptable_private_dir, state_dir
from .plans import Plan""",
    ),
])
