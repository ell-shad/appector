import inspect
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appector import actions, policy, residual


class ResidualValidationTests(unittest.TestCase):
    def test_purge_simulation_passes_packages_after_terminator(self):
        """The purge simulation must terminate options before package names."""
        with patch("appector.residual._run_command_raw") as run:
            run.return_value = (0, "Purg demo-pkg [1.0]")
            success, _output, purged = residual.simulate_leftover_purge(
                ["demo-pkg"]
            )
        self.assertTrue(success)
        self.assertEqual(purged, ["demo-pkg"])
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[-2:], ["--", "demo-pkg"])
        self.assertEqual(run.call_args.kwargs["env_overrides"], {"LC_ALL": "C"})
    def test_split_safe_package_ids(self):
        valid, invalid = policy._split_safe_package_ids(
            ["vim", "vim:amd64", "-bad", "foo;bar", "vim", "", "a+b:c.d_e-f"]
        )
        self.assertEqual(valid, ["vim", "vim:amd64", "a+b:c.d_e-f"])
        self.assertEqual(invalid, ["-bad", "foo;bar"])

    def test_deduplicate_preserves_order(self):
        self.assertEqual(
            policy._deduplicate_package_ids(["b", "a", "b", "", "a", "c"]),
            ["b", "a", "c"],
        )

    def test_conffile_line_regex_accepts_obsolete(self):
        self.assertTrue(residual.CONFFILE_LINE_RE.match("/etc/foo.conf abcdef0123456789abcdef0123456789"))
        self.assertTrue(
            residual.CONFFILE_LINE_RE.match(
                "/etc/foo.conf abcdef0123456789abcdef0123456789 obsolete"
            )
        )
        self.assertFalse(residual.CONFFILE_LINE_RE.match("not-a-path xyz"))

    def test_conffile_line_regex_finds_every_line_in_multiline_output(self):
        # finditer over full dpkg-query output must match every record, not
        # just the first line, and must not swallow subsequent lines.
        output = (
            "/etc/a.conf 0123456789abcdef0123456789abcdef\n"
            "/etc/b.conf 0123456789abcdef0123456789abcdef obsolete\n"
            "/etc/c.conf 0123456789abcdef0123456789abcdef\n"
        )
        self.assertEqual(
            [m.group(1) for m in residual.CONFFILE_LINE_RE.finditer(output)],
            ["/etc/a.conf", "/etc/b.conf", "/etc/c.conf"],
        )

    def test_conffile_line_regex_ignores_indented_noise(self):
        output = "  not-a-conffile-line\n/etc/ok.conf 0123456789abcdef0123456789abcdef\n"
        self.assertEqual(
            [m.group(1) for m in residual.CONFFILE_LINE_RE.finditer(output)],
            ["/etc/ok.conf"],
        )

    def test_purge_blocks_invalid_names_without_touching_system(self):
        with (
            patch("appector.residual._dpkg_residual_package_set") as rc,
            patch("appector.residual._backup_residual_conffiles") as backup,
        ):
            success, message = residual.purge_leftover_configs(["good-pkg", "-bad"])
        self.assertFalse(success)
        self.assertIn("Blocked unsafe", message)
        rc.assert_not_called()
        backup.assert_not_called()

    def test_purge_blocks_oversized_batch(self):
        many = [f"pkg{i}" for i in range(residual.RESIDUAL_MAX_PACKAGES + 1)]
        with (
            patch("appector.residual._dpkg_residual_package_set") as rc,
            patch("appector.residual._backup_residual_conffiles") as backup,
        ):
            success, message = residual.purge_leftover_configs(many)
        self.assertFalse(success)
        self.assertIn("limit", message.lower())
        rc.assert_not_called()
        backup.assert_not_called()

    def test_purge_requires_rc_state(self):
        with patch(
            "appector.residual._dpkg_residual_package_set", return_value={"other-pkg"}
        ):
            success, message = residual.purge_leftover_configs(["my-pkg"])
        self.assertFalse(success)
        self.assertIn("residual-configuration", message)

    def test_purge_simulation_failure_blocks(self):
        with (
            patch(
                "appector.residual._dpkg_residual_package_set",
                return_value={"my-pkg"},
            ),
            patch(
                "appector.residual.simulate_leftover_purge",
                return_value=(False, "sim boom", []),
            ),
            patch("appector.residual._backup_residual_conffiles") as backup,
        ):
            success, message = residual.purge_leftover_configs(["my-pkg"])
        self.assertFalse(success)
        self.assertIn("simulation failed", message.lower())
        backup.assert_not_called()

    def test_purge_blocks_extra_simulated_removals(self):
        with (
            patch(
                "appector.residual._dpkg_residual_package_set",
                return_value={"my-pkg"},
            ),
            patch(
                "appector.residual.simulate_leftover_purge",
                return_value=(True, "Purg my-pkg\nPurg unrelated", ["my-pkg", "unrelated"]),
            ),
            patch("appector.residual._backup_residual_conffiles") as backup,
        ):
            success, message = residual.purge_leftover_configs(["my-pkg"])
        self.assertFalse(success)
        self.assertIn("beyond", message.lower())
        backup.assert_not_called()

    def test_purge_reverifies_after_backup(self):
        calls = {"rc": 0}

        def fake_rc():
            calls["rc"] += 1
            if calls["rc"] == 1:
                return {"my-pkg"}
            return set()  # reinstalled / left rc state after backup

        with (
            patch("appector.residual._dpkg_residual_package_set", side_effect=fake_rc),
            patch(
                "appector.residual.simulate_leftover_purge",
                return_value=(True, "Purg my-pkg", ["my-pkg"]),
            ),
            patch(
                "appector.residual._backup_residual_conffiles",
                return_value=(Path("/tmp/fake-backup"), 1),
            ),
            patch("appector.actions._run_command_stream") as run,
        ):
            success, message = residual.purge_leftover_configs(["my-pkg"])
        self.assertFalse(success)
        self.assertIn("after the backup", message)
        run.assert_not_called()

    def test_size_estimate_uses_shared_conffile_pattern(self):
        fake_output = "/etc/kept.conf abcdef0123456789abcdef0123456789 obsolete\n"
        fake_app = type("A", (), {"manager": "Leftover", "package_id": "demo"})()
        with (
            patch(
                "appector.actions.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=fake_output, stderr=""
                ),
            ),
            patch(
                "appector.actions._regular_file_sizes",
                return_value=1234,
            ) as sizes,
        ):
            text = actions.get_removal_size_estimate([fake_app])
        self.assertIn("approximately", text)
        # Obsolete entries must be counted (old regex missed them).
        self.assertTrue(sizes.called)
        counted = sizes.call_args.args[0]
        self.assertIn("/etc/kept.conf", counted)

    def test_dpkg_conffile_query_uses_option_terminator(self):
        with patch("appector.actions.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            residual._dpkg_conffile_entries("demo-pkg")
        cmd = run.call_args.args[0]
        self.assertIn("--", cmd)
        self.assertEqual(cmd[-2:], ["--", "demo-pkg"])
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")

    def test_dpkg_conffile_query_rejects_unsafe_name(self):
        with patch("appector.actions.subprocess.run") as run:
            with self.assertRaises(OSError):
                residual._dpkg_conffile_entries("-evil")
        run.assert_not_called()


    def test_size_estimate_dpkg_queries_use_option_terminator(self):
        """A package name must never be able to parse as a dpkg option."""
        leftover = type("A", (), {"manager": "Leftover", "package_id": "demo"})()
        with patch("appector.actions.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            actions.get_removal_size_estimate(
                [leftover], apt_package_ids=["vim"]
            )
        self.assertTrue(run.called)
        for call in run.call_args_list:
            cmd = call.args[0]
            self.assertIn("--", cmd, cmd)
            self.assertEqual(call.kwargs["env"]["LC_ALL"], "C")

    def test_leftover_size_query_passes_package_after_terminator(self):
        fake_app = type("A", (), {"manager": "Leftover", "package_id": "demo"})()
        with patch("appector.actions.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            actions.get_removal_size_estimate([fake_app])
        first = run.call_args_list[0].args[0]
        self.assertEqual(first[-2:], ["--", "demo"])

    def test_apt_size_query_passes_ids_after_terminator(self):
        with patch("appector.actions.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            actions.get_removal_size_estimate([], apt_package_ids=["vim", "nano"])
        cmd = run.call_args_list[0].args[0]
        self.assertEqual(cmd[-3:], ["--", "vim", "nano"])

class SignalSignatureTests(unittest.TestCase):
    """GTK callbacks must accept the arguments their signal delivers.

    A handler bound to a GObject signal receives the emitting object, so a
    one-argument signature raises TypeError when the signal fires. GTK prints
    the traceback and continues, which makes the connected feature silently do
    nothing - exactly how a saved window size went unapplied.
    """

    def test_map_handler_accepts_the_emitting_object(self):
        from appector.window import MainWindow

        stub = _SignalStub("_on_map_once")
        scheduled = []

        with patch("appector.window.GLib.idle_add", side_effect=scheduled.append):
            MainWindow._on_map_once(stub, stub)
        self.assertEqual(len(scheduled), 1, "map should schedule the size apply")

        # A second map (window re-shown) must not schedule again.
        with patch("appector.window.GLib.idle_add", side_effect=scheduled.append):
            MainWindow._on_map_once(stub, stub)
        self.assertEqual(len(scheduled), 1, "size apply must happen only once")

    def test_handlers_tolerate_signal_arguments(self):
        """Every handler connected to a widget signal must survive delivery.

        Called with the object GTK passes (plus one for notify-style signals);
        a signature mismatch would raise TypeError here.
        """
        from appector.window import MainWindow

        handlers = {
            # name: (arguments GTK appends after `self`)
            "_on_map_once": ("widget",),
            "on_close_request": ("widget",),
            "on_selection_changed": ("widget",),
            "on_filter_items_changed": ("widget",),
            "on_sorter_changed": ("widget", "pspec"),
            "on_activate": ("widget", "position"),
            "on_reload_clicked": ("button",),
            "on_check_updates_clicked": ("button",),
            "on_view_mode_toggled": ("button", "mode"),
            "on_marked_only_toggled": ("button",),
            "on_duplicates_only_toggled": ("button",),
            "on_scope_toggled": ("button", "scope"),
            "on_search_changed": ("entry",),
            "on_flatpak_batch_text_changed": ("buffer", "dialog"),
        }
        values = {
            "widget": object(),
            "button": object(),
            "pspec": object(),
            "position": 0,
            "mode": "table",
            "scope": "all",
            "entry": object(),
            "buffer": object(),
            "dialog": object(),
        }
        for name, extras in handlers.items():
            with self.subTest(handler=name):
                handler = getattr(MainWindow, name)
                # Bind only - proves the signature accepts what GTK delivers
                # without running the body against fake widgets.
                inspect.signature(handler).bind(
                    object(), *(values[kind] for kind in extras)
                )


class _SignalStub:
    """Minimal stand-in for MainWindow used to call bound handlers."""

    def __init__(self, name):
        self.name = name
        self._map_size_applied = False
        self.scope = "all"
        self.show_leftovers = True
        self.show_advanced_apps = True
        self.hide_basic_apps = False

    def __getattr__(self, item):
        # Every attribute access used by the handlers under test.
        def _noop(*args, **kwargs):
            return False

        return _noop

    def _sync_legacy_scope_flags(self):
        return None

    def _unmark_hidden_advanced_items(self):
        return 0

    def get_active(self):
        return True


class PurgeBackupInspectionTests(unittest.TestCase):
    def test_read_purge_backup_parses_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "20240101T000000-a"
            backup.mkdir()
            (backup / "manifest.json").write_text(
                '{"created_at": "2024-01-01T00:00:00+00:00",'
                ' "status": "complete",'
                ' "packages": ["demo", "other"],'
                ' "files": ['
                '   {"path": "/etc/demo.conf", "package": "demo",'
                '    "type": "file", "size": 12, "sha256": "abc"},'
                '   {"path": "/etc/gone.conf", "package": "demo",'
                '    "status": "missing"}'
                ']}',
                encoding="utf-8",
            )
            data = residual.read_purge_backup(backup)
        self.assertEqual(data["status"], "complete")
        self.assertEqual(data["packages"], ["demo", "other"])
        self.assertEqual(data["backed_up"], 1)
        self.assertEqual(data["missing"], 1)
        self.assertEqual(data["files"][0]["path"], "/etc/demo.conf")

    def test_read_purge_backup_rejects_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "b"
            backup.mkdir()
            (backup / "manifest.json").write_text(
                '{"files": [{"path": "etc/relative.conf"}]}', encoding="utf-8"
            )
            data = residual.read_purge_backup(backup)
        self.assertEqual(data["files"], [])

    def test_read_purge_backup_handles_corrupt_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "b"
            backup.mkdir()
            (backup / "manifest.json").write_text("{not json", encoding="utf-8")
            self.assertIsNone(residual.read_purge_backup(backup))
            self.assertIsNone(residual.read_purge_backup(Path(tmp) / "missing"))


class FlatpakPreviewDetectionTests(unittest.TestCase):
    def test_prompt_regex_matches_variants(self):
        for text in (
            "Proceed with these changes? [Y/n]",
            "proceed with these changes? [y/N]",
            "Abort? [n/Y]",
            "[N/y]",
        ):
            self.assertTrue(actions.FLATPAK_PROMPT_RE.search(text), text)

    def test_prompt_regex_ignores_ordinary_output(self):
        for text in ("Removing runtime", "Nothing unused to uninstall", ""):
            self.assertIsNone(actions.FLATPAK_PROMPT_RE.search(text), text)


class MarkPersistenceTests(unittest.TestCase):
    """Staged removal marks are stored privately and parsed defensively."""

    def _stub(self):
        from types import SimpleNamespace

        from appector.window import MainWindow

        stub = SimpleNamespace()
        stub.marked_keys = {"APT::vim", "Snap::firefox"}
        stub._state_dir = lambda: MainWindow._state_dir(stub)
        stub.load_marked_keys = lambda: MainWindow.load_marked_keys(stub)
        stub.save_marked_keys = lambda: MainWindow.save_marked_keys(stub)
        return stub

    def _mark_file(self, home):
        return home / ".local" / "state" / "app-manager" / "marked.json"

    def test_marks_round_trip_with_user_only_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            stub = self._stub()
            with patch.object(Path, "home", return_value=home):
                stub.save_marked_keys()
                keys = stub.load_marked_keys()
            self.assertEqual(keys, {"APT::vim", "Snap::firefox"})
            self.assertEqual(
                stat.S_IMODE(self._mark_file(home).stat().st_mode),
                0o600,
            )

    def test_malformed_mark_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            mark_file = self._mark_file(home)
            mark_file.parent.mkdir(parents=True, exist_ok=True)
            stub = self._stub()

            for payload in (
                "{not json",
                "[]",
                '{"marked": "nope"}',
                '{"marked": [1, 2]}',
            ):
                with self.subTest(payload=payload):
                    mark_file.write_text(payload, encoding="utf-8")
                    with patch.object(Path, "home", return_value=home):
                        self.assertEqual(stub.load_marked_keys(), set())

    def test_symlinked_mark_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            mark_file = self._mark_file(home)
            mark_file.parent.mkdir(parents=True, exist_ok=True)
            outside = home / "outside.json"
            outside.write_text('{"marked": ["APT::evil"]}', encoding="utf-8")
            mark_file.symlink_to(outside)
            stub = self._stub()
            with patch.object(Path, "home", return_value=home):
                self.assertEqual(stub.load_marked_keys(), set())


class ScopeFilterTests(unittest.TestCase):
    """`filter_func` decides visibility from the unified scope selector."""

    def _window_stub(self):
        from types import SimpleNamespace

        from appector import window as window_module

        stub = SimpleNamespace()
        stub.search_text = ""
        stub.marked_only = False
        stub.duplicates_only = False
        stub.scope = "all"
        stub.show_advanced_apps = True
        stub.hide_basic_apps = False
        stub.show_leftovers = True
        stub.filter_func = window_module.MainWindow.filter_func.__get__(stub)
        stub._is_advanced_item = (
            window_module.MainWindow._is_advanced_item.__get__(stub)
        )
        stub._entry_hidden_by_advanced_options = (
            window_module.MainWindow._entry_hidden_by_advanced_options.__get__(stub)
        )
        stub._sync_legacy_scope_flags = (
            window_module.MainWindow._sync_legacy_scope_flags.__get__(stub)
        )
        return stub

    def _entry(self, manager="APT", is_gui_app=True):
        # `filter_func` requires real AppItem instances, so build one.
        from appector.app_item import AppItem
        from appector.models import AppEntry

        return AppItem(
            AppEntry(
                name="x",
                manager=manager,
                source="s",
                package_id="p",
                is_gui_app=is_gui_app,
            )
        )

    def test_all_scope_shows_apps_and_system_items(self):
        stub = self._window_stub()
        self.assertTrue(stub.filter_func(self._entry("APT", True)))
        self.assertTrue(stub.filter_func(self._entry("Leftover", False)))
        self.assertTrue(stub.filter_func(self._entry("Snap", False)))

    def test_apps_scope_hides_system_items(self):
        stub = self._window_stub()
        stub.scope = "apps"
        self.assertTrue(stub.filter_func(self._entry("APT", True)))
        self.assertFalse(stub.filter_func(self._entry("Snap", False)))
        self.assertFalse(stub.filter_func(self._entry("Leftover", False)))

    def test_system_scope_shows_only_system_items(self):
        stub = self._window_stub()
        stub.scope = "system"
        self.assertFalse(stub.filter_func(self._entry("APT", True)))
        self.assertTrue(stub.filter_func(self._entry("Snap", False)))
        self.assertTrue(stub.filter_func(self._entry("Leftover", False)))

    def test_sync_flags_agree_with_scope(self):
        stub = self._window_stub()
        for scope, advanced, hidden_basic in (
            ("all", True, False),
            ("apps", False, False),
            ("system", True, True),
        ):
            stub.scope = scope
            stub._sync_legacy_scope_flags()
            self.assertEqual(stub.show_advanced_apps, advanced, scope)
            self.assertEqual(stub.hide_basic_apps, hidden_basic, scope)

    def test_hidden_entry_detection_matches_filter(self):
        stub = self._window_stub()
        for scope in ("all", "apps", "system"):
            stub.scope = scope
            stub._sync_legacy_scope_flags()
            app_entry = self._entry("APT", True)
            sys_entry = self._entry("Snap", False)
            self.assertEqual(
                stub._entry_hidden_by_advanced_options(app_entry),
                not stub.filter_func(app_entry),
                scope,
            )
            self.assertEqual(
                stub._entry_hidden_by_advanced_options(sys_entry),
                not stub.filter_func(sys_entry),
                scope,
            )


class ResidualBackupLimitTests(unittest.TestCase):
    """Backup size/count caps must abort the purge before any deletion."""

    def _fake_conffiles(self, entries):
        return lambda package_id, timeout=30: [
            (path, "0" * 32) for path in entries
        ]

    def _patched_home(self, home):
        return patch.object(Path, "home", return_value=home)

    def test_backup_file_count_cap_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source_dir = home / "conffiles"
            source_dir.mkdir()
            entries = []
            for index in range(4):
                target = source_dir / f"fake{index}.conf"
                target.write_bytes(b"x" * 10)
                entries.append(str(target))
            with (
                self._patched_home(home),
                patch.object(residual, "RESIDUAL_MAX_BACKUP_FILES", 2),
                patch.object(
                    residual,
                    "_dpkg_conffile_entries",
                    self._fake_conffiles(entries),
                ),
                patch("appector.residual._log_action"),
            ):
                with self.assertRaises(OSError) as caught:
                    residual._backup_residual_conffiles(["demo-pkg"])
            self.assertIn("Refusing to back up more than", str(caught.exception))

    def test_backup_byte_cap_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source_dir = home / "conffiles"
            source_dir.mkdir()
            entries = []
            for index in range(2):
                target = source_dir / f"fake{index}.conf"
                target.write_bytes(b"y" * 4096)
                entries.append(str(target))
            with (
                self._patched_home(home),
                patch.object(residual, "RESIDUAL_MAX_BACKUP_BYTES", 1024),
                patch.object(
                    residual,
                    "_dpkg_conffile_entries",
                    self._fake_conffiles(entries),
                ),
                patch("appector.residual._log_action"),
            ):
                with self.assertRaises(OSError) as caught:
                    residual._backup_residual_conffiles(["demo-pkg"])
            self.assertIn("configuration files at once", str(caught.exception))

    def test_backup_itself_refuses_oversized_batch(self):
        """`_backup_residual_conffiles` enforces the batch cap on its own."""
        many = [f"pkg{index}" for index in range(residual.RESIDUAL_MAX_PACKAGES + 1)]
        with (
            patch.object(Path, "home", return_value=Path("/nonexistent-home")),
            patch.object(residual, "_dpkg_conffile_entries") as entries,
        ):
            with self.assertRaises(OSError) as caught:
                residual._backup_residual_conffiles(many)
        self.assertIn("Refusing to back up", str(caught.exception))
        self.assertIn("limit is", str(caught.exception))
        entries.assert_not_called()

    def test_purge_batch_cap_uses_purge_level_check(self):
        """`purge_leftover_configs` enforces its own batch cap before backup."""
        many = [f"pkg{i}" for i in range(residual.RESIDUAL_MAX_PACKAGES + 1)]
        with (
            patch("appector.residual._dpkg_residual_package_set") as rc,
            patch("appector.residual._backup_residual_conffiles") as backup,
        ):
            success, message = residual.purge_leftover_configs(many)
        self.assertFalse(success)
        self.assertIn("limit is", message)
        rc.assert_not_called()
        backup.assert_not_called()


class ManualRemovalSymlinkTests(unittest.TestCase):
    def test_direct_outside_path_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            outside = root / "outside"
            home.mkdir()
            outside.mkdir()
            (outside / "victim").write_text("do not delete", encoding="utf-8")
            victim = outside / "victim"
            app = type(
                "A",
                (),
                {
                    "manager": "Manual",
                    "package_id": str(victim),
                    "removal_paths": [str(victim)],
                },
            )()
            with (
                patch.object(Path, "home", return_value=home),
                patch("appector.residual._log_action"),
            ):
                success, message = actions.execute_removal(app)
            self.assertFalse(success)
            self.assertIn("outside known manual-install", message)
            self.assertEqual(
                (outside / "victim").read_text(encoding="utf-8"), "do not delete"
            )

    def test_parent_symlink_to_privileged_location_is_blocked(self):
        # A symlinked ~/Applications pointing at a privileged location must
        # still be refused via the administrator-privilege guard, even though
        # the lexical path looks allowed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            outside = root / "outside"
            home.mkdir()
            outside.mkdir()
            (outside / "victim").write_text("do not delete", encoding="utf-8")
            apps_dir = home / "Applications"
            apps_dir.symlink_to(outside, target_is_directory=True)
            victim = apps_dir / "victim"
            app = type(
                "A",
                (),
                {
                    "manager": "Manual",
                    "package_id": str(victim),
                    "removal_paths": [str(victim)],
                },
            )()
            with (
                patch.object(Path, "home", return_value=home),
                patch("appector.actions.os.access", return_value=False),
                patch("appector.residual._log_action"),
            ):
                success, message = actions.execute_removal(app)
            self.assertFalse(success)
            self.assertIn("administrator privileges", message)
            self.assertEqual(
                (outside / "victim").read_text(encoding="utf-8"), "do not delete"
            )

    def test_file_not_owned_by_current_user_is_refused(self):
        """Ownership evidence is required before any unlink."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            bindir = home / ".local" / "bin"
            bindir.mkdir(parents=True)
            victim = bindir / "root-owned"
            victim.write_text("system file", encoding="utf-8")
            app = type(
                "A",
                (),
                {
                    "manager": "Manual",
                    "package_id": str(victim),
                    "removal_paths": [str(victim)],
                },
            )()
            with (
                patch.object(Path, "home", return_value=home),
                patch("appector.actions.os.getuid", return_value=os.getuid() + 4242),
                patch("appector.residual._log_action"),
            ):
                success, message = actions.execute_removal(app)
            self.assertFalse(success)
            self.assertIn("not owned by the current user", message)
            self.assertTrue(victim.exists())

    def test_lexical_root_guard_holds_independently_of_resolved_guard(self):
        """The lexical allowlist must reject even if resolved roots are bogus."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            (home / ".local" / "bin").mkdir(parents=True)
            victim = home / ".local" / "bin" / "tool"
            victim.write_text("x", encoding="utf-8")
            allowed = [home / ".local" / "bin"]
            # Resolved roots point somewhere unrelated, isolating the lexical
            # check from the resolved check.
            with patch("appector.actions._resolve_allowed_roots", return_value=[root / "nowhere"]):
                with self.assertRaises(OSError) as caught:
                    actions._validate_removal_file(
                        str(victim), allowed, [root / "nowhere"]
                    )
            self.assertIn("outside known manual-install", str(caught.exception))
            self.assertTrue(victim.exists())

    def test_symlink_outside_allowed_root_is_refused(self):
        """Symlinks return early, so the lexical guard is their only guard.

        A symlink is deleted by unlinking the link without resolving it, so the
        resolved-root check never runs for it. The lexical allowlist must
        therefore reject an out-of-scope symlink on its own.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            (home / ".local" / "bin").mkdir(parents=True)
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            victim = elsewhere / "target"
            victim.write_text("keep", encoding="utf-8")
            link = root / "outside-link"
            link.symlink_to(victim)

            allowed = [home / ".local" / "bin"]
            with patch.object(Path, "home", return_value=home):
                with self.assertRaises(OSError) as caught:
                    actions._validate_removal_file(
                        str(link), allowed, allowed
                    )
            self.assertIn("outside known manual-install", str(caught.exception))
            self.assertTrue(link.is_symlink())
            self.assertTrue(victim.exists())

    def test_directory_at_allowed_path_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            target = home / ".local" / "bin" / "somedir"
            target.mkdir(parents=True)
            with patch.object(Path, "home", return_value=home):
                with self.assertRaises(OSError) as caught:
                    actions._validate_removal_file(
                        str(target),
                        [home / ".local" / "bin"],
                        [home / ".local" / "bin"],
                    )
            self.assertIn("not a regular file", str(caught.exception))
            self.assertTrue(target.is_dir())

    def test_fifo_at_allowed_path_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            bindir = home / ".local" / "bin"
            bindir.mkdir(parents=True)
            fifo = bindir / "pipe"
            os.mkfifo(fifo)
            with patch.object(Path, "home", return_value=home):
                with self.assertRaises(OSError) as caught:
                    actions._validate_removal_file(
                        str(fifo), [bindir], [bindir]
                    )
            self.assertIn("not a regular file", str(caught.exception))
            self.assertTrue(fifo.exists())

    def test_allowed_root_itself_is_refused(self):
        """Minimum-depth rule: the root can never be the deletion target."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            bindir = home / ".local" / "bin"
            bindir.mkdir(parents=True)
            with patch.object(Path, "home", return_value=home):
                with self.assertRaises(OSError) as caught:
                    actions._validate_removal_file(
                        str(bindir), [bindir], [bindir]
                    )
            self.assertIn("not a regular file", str(caught.exception))
            self.assertTrue(bindir.is_dir())

    def test_symlink_file_itself_can_be_removed_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            (home / "Applications").mkdir(parents=True)
            (home / ".local/share/applications").mkdir(parents=True)
            target = root / "real-target"
            target.write_text("keep me", encoding="utf-8")
            link = home / "Applications" / "my-link"
            link.symlink_to(target)
            app = type(
                "A",
                (),
                {
                    "manager": "Manual",
                    "package_id": str(link),
                    "removal_paths": [str(link)],
                },
            )()
            with (
                patch.object(Path, "home", return_value=home),
                patch("appector.residual._log_action"),
            ):
                success, _message = actions.execute_removal(app)
            self.assertTrue(success)
            self.assertFalse(link.exists() or link.is_symlink())
            # Only the link is removed; the target survives.
            self.assertEqual(target.read_text(encoding="utf-8"), "keep me")


class PurgeBackupPruningTests(unittest.TestCase):
    def test_prune_keeps_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            backup_root = home / ".local/state/app-manager/purge-backups"
            backup_root.mkdir(parents=True)
            for name in ("20240101T000000-a", "20240102T000000-b", "20240103T000000-c"):
                (backup_root / name).mkdir()
            with patch.object(Path, "home", return_value=home):
                removed, kept = residual.prune_old_purge_backups(keep=2)
            self.assertEqual((removed, kept), (1, 2))
            remaining = sorted(p.name for p in backup_root.iterdir())
            self.assertEqual(
                remaining, ["20240102T000000-b", "20240103T000000-c"]
            )


if __name__ == "__main__":
    unittest.main()
