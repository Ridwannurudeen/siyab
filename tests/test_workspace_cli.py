import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from intel import __main__ as cli
from intel.workspace import SQLiteWorkspaceStore


class WorkspaceCLITest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "repository"
        self.root.mkdir()
        self.state = self.base / "private-state"

    def invoke(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = cli.main(list(args))
        return result, output.getvalue(), errors.getvalue()

    def test_access_code_create_list_revoke_and_grant(self):
        store = SQLiteWorkspaceStore(self.state / "workspace.sqlite3")
        result, output, errors = self.invoke("workspace-access-code", "--state-dir", str(self.state), "--uses", "2", "--days", "3")
        self.assertEqual((result, errors), (0, ""))
        created = json.loads(output)
        self.assertEqual(created["uses_left"], 2)
        self.assertEqual(created["expires_ms"] - created["created_ms"], 3 * 86400000)
        result, output, errors = self.invoke("workspace-access-code", "--state-dir", str(self.state), "--list")
        self.assertEqual((result, errors), (0, ""))
        self.assertNotIn(created["code"], output)
        self.assertEqual(json.loads(output)[0]["uses_left"], 2)
        result, _, errors = self.invoke("workspace-access-code", "--state-dir", str(self.state), "--revoke")
        self.assertEqual((result, errors), (0, ""))
        self.assertEqual(store.list_access_codes()[0]["uses_left"], 0)
        user = store.register("alice", "alice-password-123")["user"]["id"]
        team = store.list_teams(user)[0]["id"]
        store.add_watch(user, team, "alice.near")
        result, _, errors = self.invoke("workspace-access-code", "--state-dir", str(self.state), "--grant", "alice")
        self.assertEqual((result, errors), (0, ""))
        self.assertEqual(store.watch_schedule()[0]["reserved"], 1)

    def test_access_code_expiry_includes_utc_on_create_and_list(self):
        SQLiteWorkspaceStore(self.state / "workspace.sqlite3")
        result, output, errors = self.invoke("workspace-access-code", "--state-dir", str(self.state))
        self.assertEqual((result, errors), (0, ""))
        created = json.loads(output)
        expected = datetime.fromtimestamp(created["expires_ms"] // 1000, UTC).isoformat().replace("+00:00", "Z")
        self.assertEqual(created["expires_utc"], expected)
        result, output, errors = self.invoke("workspace-access-code", "--state-dir", str(self.state), "--list")
        self.assertEqual((result, errors), (0, ""))
        listed = json.loads(output)[0]
        self.assertEqual(listed["expires_ms"], created["expires_ms"])
        self.assertEqual(listed["expires_utc"], expected)

    def test_access_code_defaults_and_invalid_options(self):
        store = SQLiteWorkspaceStore(self.state / "workspace.sqlite3")
        result, output, errors = self.invoke("workspace-access-code", "--state-dir", str(self.state))
        self.assertEqual((result, errors), (0, ""))
        created = json.loads(output)
        self.assertEqual(created["uses_left"], 20)
        self.assertEqual(created["expires_ms"] - created["created_ms"], 30 * 86400000)
        for options in (("--uses", "0"), ("--days", "61"), ("--grant", "missing")):
            result, output, errors = self.invoke("workspace-access-code", "--state-dir", str(self.state), *options)
            self.assertEqual(result, 2)
            self.assertEqual(output, "")
            self.assertIn("stopped:", errors)
        self.assertEqual(len(store.list_access_codes()), 1)

    def test_access_code_refuses_internal_or_missing_state(self):
        for state in (self.root, self.root / "state", self.state):
            with patch.object(cli, "ROOT", self.root):
                result, output, errors = self.invoke("workspace-access-code", "--state-dir", str(state))
            self.assertEqual(result, 2)
            self.assertEqual(output, "")
            self.assertIn("external", errors)
        self.assertFalse((self.root / "state").exists())
        self.assertFalse(self.state.exists())

    def test_backup_refuses_destination_in_source_repository(self):
        store = SQLiteWorkspaceStore(self.state / "workspace.sqlite3")
        destination = self.root / "private.sqlite3"
        with patch.object(cli, "ROOT", self.root), patch.object(SQLiteWorkspaceStore, "backup") as backup:
            result, _, errors = self.invoke("workspace-backup", "--state-dir", str(self.state), "--out", str(destination))
        self.assertEqual(result, 2)
        self.assertIn("external", errors)
        backup.assert_not_called()
        self.assertTrue(store.path.exists())

    def test_backup_copies_database_without_overwriting(self):
        store = SQLiteWorkspaceStore(self.state / "workspace.sqlite3")
        registered = store.register("alice", "alice-password-123")
        team = store.list_teams(registered["user"]["id"])[0]["id"]
        store.create_case(registered["user"]["id"], team, "Investigation", "Private notes", "/accounts/alice.near.html")
        destination = self.base / "backup.sqlite3"
        result, output, _ = self.invoke("workspace-backup", "--state-dir", str(self.state), "--out", str(destination))
        self.assertEqual(result, 0)
        self.assertIn("completed", output)
        with contextlib.closing(sqlite3.connect(destination)) as connection:
            self.assertEqual(connection.execute("SELECT title,notes FROM cases").fetchone(), ("Investigation", "Private notes"))
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        original = destination.read_bytes()
        self.assertEqual(self.invoke("workspace-backup", "--state-dir", str(self.state), "--out", str(destination))[0], 2)
        self.assertEqual(destination.read_bytes(), original)

    def test_serve_passes_state_and_origin(self):
        with patch.object(cli.api, "serve", return_value=0) as serve:
            result, _, _ = self.invoke("serve", "--host", "127.0.0.1", "--port", "8792", "--site", "demo/site", "--live", "--state-dir", str(self.state), "--workspace-origin", "http://localhost:8792")
        self.assertEqual(result, 0)
        serve.assert_called_once_with("127.0.0.1", 8792, "demo/site", live=True, state_dir=str(self.state), workspace_origin="http://localhost:8792")

    def test_monitor_once_uses_external_feed(self):
        events = self.base / "feed"
        with patch.dict("os.environ", {"FEED_STATE_DIR": str(events)}), patch("intel.monitor.run", return_value={"running": False}) as run:
            result, output, _ = self.invoke("monitor", "--state-dir", str(self.state), "--once")
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(output)["running"])
        self.assertEqual(run.call_args.args[0].path, self.state / "workspace.sqlite3")
        self.assertEqual(run.call_args.args[1], events / "events")
        self.assertEqual(run.call_args.kwargs, {"once": True})

    def test_monitor_refuses_internal_state(self):
        with patch.object(cli, "ROOT", self.root), patch("intel.monitor.run") as run:
            result, _, _ = self.invoke("monitor", "--state-dir", str(self.root / "state"), "--once")
        self.assertEqual(result, 2)
        run.assert_not_called()
        self.assertFalse((self.root / "state").exists())

    def test_history_once_runs_without_network_when_no_watches(self):
        from intel import history

        with patch("intel.accounts.AccountData") as reader, patch.object(cli.labels, "load", return_value="labels") as load, patch.object(history, "run", wraps=history.run) as run:
            result, output, _ = self.invoke("history-follow", "--state-dir", str(self.state), "--once")
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(output)["running"])
        self.assertTrue((self.state / "history.sqlite3").is_file())
        self.assertEqual(run.call_args.args[0].path, self.state / "workspace.sqlite3")
        self.assertIs(run.call_args.args[1], reader.return_value)
        self.assertEqual(run.call_args.args[2], self.state / "history.sqlite3")
        self.assertEqual(run.call_args.kwargs, {"once": True})
        load.assert_called_once_with()
        reader.assert_called_once_with("labels")
        self.assertEqual(reader.return_value.method_calls, [])

    def test_history_refuses_internal_state(self):
        with patch.object(cli, "ROOT", self.root), patch("intel.history.run") as run:
            result, _, _ = self.invoke("history-follow", "--state-dir", str(self.root / "state"), "--once")
        self.assertEqual(result, 2)
        run.assert_not_called()
        self.assertFalse((self.root / "state").exists())

    def test_investigation_once_runs_without_network_when_no_jobs(self):
        with patch("intel.accounts.AccountData") as reader:
            result, output, _ = self.invoke("investigation-follow", "--state-dir", str(self.state), "--once")
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(output)["running"])
        self.assertTrue((self.state / "investigation-jobs.sqlite3").is_file())
        self.assertTrue((self.state / "history.sqlite3").is_file())
        self.assertEqual(reader.return_value.method_calls, [])

    def test_investigation_refuses_internal_state(self):
        with patch.object(cli, "ROOT", self.root), patch("intel.investigation_jobs.run") as run:
            result, _, _ = self.invoke("investigation-follow", "--state-dir", str(self.root / "state"), "--once")
        self.assertEqual(result, 2)
        run.assert_not_called()
        self.assertFalse((self.root / "state").exists())

    def test_delivery_once_without_credentials_makes_no_network_calls(self):
        with patch.dict("os.environ", {"SIYAB_TELEGRAM_TOKEN_FILE": ""}), patch("intel.delivery.TelegramClient.call") as call:
            result, output, _ = self.invoke("delivery-follow", "--state-dir", str(self.state), "--once")
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(output)["configured"])
        self.assertEqual(json.loads(output)["reason"], "credentials_missing")
        call.assert_not_called()

    def test_delivery_refuses_internal_state(self):
        with patch.object(cli, "ROOT", self.root), patch("intel.delivery.run") as run:
            result, _, _ = self.invoke("delivery-follow", "--state-dir", str(self.root / "state"), "--once")
        self.assertEqual(result, 2)
        run.assert_not_called()
        self.assertFalse((self.root / "state").exists())

    def test_backup_reserves_destination_exclusively(self):
        SQLiteWorkspaceStore(self.state / "workspace.sqlite3")
        destination = self.base / "race.sqlite3"

        def competing_writer(path, flags, mode):
            destination.write_bytes(b"other backup")
            raise FileExistsError("another process created the destination")

        with patch.object(cli.os, "open", side_effect=competing_writer), patch.object(SQLiteWorkspaceStore, "backup") as backup:
            result, _, _ = self.invoke("workspace-backup", "--state-dir", str(self.state), "--out", str(destination))
        self.assertEqual(result, 2)
        self.assertEqual(destination.read_bytes(), b"other backup")
        backup.assert_not_called()

    def test_failed_backup_removes_partial_destination(self):
        SQLiteWorkspaceStore(self.state / "workspace.sqlite3")
        destination = self.base / "failed.sqlite3"
        with patch.object(SQLiteWorkspaceStore, "backup", side_effect=OSError("disk full")), self.assertRaisesRegex(OSError, "disk full"):
            self.invoke("workspace-backup", "--state-dir", str(self.state), "--out", str(destination))
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
