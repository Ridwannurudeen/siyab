from contextlib import closing, contextmanager
import hashlib
import json
import multiprocessing
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from intel import workspace
from intel.workspace import SQLiteWorkspaceStore, WorkspaceError


def _open_legacy_store(path, barrier, results):
    try:
        barrier.wait(timeout=10)
        connect = sqlite3.connect

        class ShortTimeoutConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == "PRAGMA busy_timeout=5000":
                    sql = "PRAGMA busy_timeout=20"
                return super().execute(sql, parameters)

        identity = workspace._transfer_identity
        def slow_identity(event):
            time.sleep(0.0001)
            return identity(event)

        with patch.object(workspace.sqlite3, "connect", side_effect=lambda *args, **kwargs: connect(*args, **kwargs, factory=ShortTimeoutConnection)), patch.object(workspace, "_transfer_identity", slow_identity):
            store = SQLiteWorkspaceStore(path)
        with store._connection() as connection:
            remaining = connection.execute("SELECT COUNT(*) FROM events WHERE transfer_id IS NULL").fetchone()[0]
        results.put(("opened", remaining))
    except Exception as error:
        results.put((type(error).__name__, str(error)))


class WorkspaceMigrationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "workspace.sqlite3"

    def legacy(self, count=6501):
        events = []
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.executescript("""
                CREATE TABLE events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                    body_json TEXT NOT NULL, observed_ms INTEGER, ingested_ms INTEGER NOT NULL
                );
                CREATE TABLE migration_probe (id INTEGER PRIMARY KEY);
                PRAGMA user_version=1;
            """)
            for index in range(count):
                event = {"signal": "account_transfer_observed", "detail": "Observed transfer",
                         "receipt_id": f"receipt-{index}", "transfer_index": 0, "amount": str(index + 1),
                         "account_id": "alice.near", "other_account_id": "bob.near", "tx_hash": None,
                         "observed": {"timestamp_ms": index}, "block_timestamp": str(index)}
                events.append(event)
            connection.executemany("INSERT INTO events(event_id,body_json,observed_ms,ingested_ms) VALUES (?,?,?,?)",
                                   [(hashlib.sha256(str(index).encode()).hexdigest(), json.dumps(event), index, index)
                                    for index, event in enumerate(events)])
            connection.commit()
        return events

    def test_all_legacy_transfers_are_migrated_before_lookups(self):
        events = self.legacy()
        store = SQLiteWorkspaceStore(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE transfer_id IS NULL").fetchone()[0], 0)
            self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='events_transfer'").fetchone())
            for seq, event in enumerate(events, 1):
                stable = {key: value for key, value in event.items() if key not in ("tx_hash", "block_timestamp", "observed")}
                identity = hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                self.assertEqual(connection.execute("SELECT seq FROM events WHERE transfer_id=?", (identity,)).fetchone()[0], seq)
        for seq, event in enumerate(events, 1):
            changed = dict(event, tx_hash="filled", block_timestamp="changed", observed={"timestamp_ms": 9999})
            identifier = hashlib.sha256(json.dumps(changed, sort_keys=True).encode()).hexdigest()
            self.assertEqual(store.ingest_event(identifier, changed), seq)

    def test_migration_commits_bounded_batches_and_releases_the_write_lock(self):
        events = self.legacy()
        original = SQLiteWorkspaceStore._connection
        batches, durations, waits = [], [], []

        @contextmanager
        def observe(store, **kwargs):
            updates = 0
            def trace(statement):
                nonlocal updates
                if statement.startswith("UPDATE events SET transfer_id="):
                    updates += 1
            with original(store, **kwargs) as connection:
                connection.set_trace_callback(trace)
                started = time.monotonic()
                yield connection
            if kwargs.get("write"):
                durations.append(time.monotonic() - started)
            if updates:
                batches.append(updates)
                started = time.monotonic()
                with closing(sqlite3.connect(self.path, timeout=1)) as writer:
                    writer.execute("INSERT INTO migration_probe DEFAULT VALUES")
                    writer.commit()
                waits.append(time.monotonic() - started)

        with patch.object(SQLiteWorkspaceStore, "_connection", observe):
            SQLiteWorkspaceStore(self.path)
        self.assertEqual(sum(batches), len(events))
        self.assertGreaterEqual(len(batches), 4)
        self.assertLessEqual(max(batches), 2000)
        self.assertLess(max(durations), 1, durations)
        self.assertLess(max(waits), 1, waits)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM migration_probe").fetchone()[0], len(batches))

    def test_two_processes_open_the_same_legacy_file(self):
        self.legacy()
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(2), context.Queue()
        processes = [context.Process(target=_open_legacy_store, args=(self.path, barrier, results)) for _ in range(2)]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual([results.get(timeout=2) for _ in processes], [("opened", 0), ("opened", 0)])
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                process.close()
            results.close()
            results.join_thread()

    def test_invalid_json_is_preserved_with_a_nonmatching_identity(self):
        self.legacy(3)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE events SET body_json=? WHERE seq=2", ("{invalid json",))
            connection.commit()
        SQLiteWorkspaceStore(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            body, identity = connection.execute("SELECT body_json,transfer_id FROM events WHERE seq=2").fetchone()
            self.assertEqual(body, "{invalid json")
            self.assertEqual(identity, "invalid-json:2")
            self.assertIsNone(workspace._HEX.fullmatch(identity))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE transfer_id IS NULL").fetchone()[0], 0)

    def test_non_object_json_is_preserved_with_a_nonmatching_identity(self):
        for index, body in enumerate(("[]", "null", "true", "3", '"event"')):
            with self.subTest(body=body):
                self.path = Path(self.directory.name) / f"body-{index}.sqlite3"
                self.legacy(1)
                with closing(sqlite3.connect(self.path)) as connection:
                    connection.execute("UPDATE events SET body_json=? WHERE seq=1", (body,))
                    connection.commit()
                SQLiteWorkspaceStore(self.path)
                with closing(sqlite3.connect(self.path)) as connection:
                    stored = connection.execute("SELECT body_json,transfer_id FROM events WHERE seq=1").fetchone()
                self.assertEqual(stored, (body, "invalid-json:1"))
                self.assertIsNone(workspace._HEX.fullmatch(stored[1]))
                with patch.object(workspace, "_transfer_identity", side_effect=AssertionError("migration repeated")):
                    SQLiteWorkspaceStore(self.path)

    def test_non_transfer_ingested_after_migration_is_not_reprocessed(self):
        self.legacy(1)
        store = SQLiteWorkspaceStore(self.path)
        event = {"signal": "watch_account_key_added", "detail": "Observed key", "observed": {"timestamp_ms": 1}}
        identifier = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        seq = store.ingest_event(identifier, event)
        with closing(sqlite3.connect(self.path)) as connection:
            identity = connection.execute("SELECT transfer_id FROM events WHERE seq=?", (seq,)).fetchone()[0]
        self.assertEqual(identity, f"non-transfer:{seq}")
        with patch.object(workspace, "_transfer_identity", side_effect=AssertionError("migration repeated")):
            SQLiteWorkspaceStore(self.path)

    def test_interrupted_migration_resumes_and_completed_rows_are_not_reprocessed(self):
        events = self.legacy()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE events SET body_json=? WHERE seq=1", (json.dumps({"signal": "watch_account_key_added"}),))
            connection.commit()
        original = SQLiteWorkspaceStore._connection

        @contextmanager
        def interrupt(store, **kwargs):
            with original(store, **kwargs) as connection:
                before = connection.total_changes
                yield connection
                updates = connection.total_changes - before
            if updates:
                raise OSError("interrupted migration")

        with patch.object(SQLiteWorkspaceStore, "_connection", interrupt):
            with self.assertRaisesRegex(OSError, "interrupted migration"):
                SQLiteWorkspaceStore(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            migrated = connection.execute("SELECT seq,transfer_id FROM events WHERE transfer_id IS NOT NULL ORDER BY seq").fetchall()
        self.assertGreater(len(migrated), 0)
        self.assertLess(len(migrated), len(events))
        with patch.object(workspace, "_transfer_identity", wraps=workspace._transfer_identity) as identity:
            SQLiteWorkspaceStore(self.path)
        self.assertEqual(identity.call_count, len(events) - len(migrated))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT seq,transfer_id FROM events ORDER BY seq LIMIT ?", (len(migrated),)).fetchall(), migrated)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE transfer_id IS NULL").fetchone()[0], 0)
        with patch.object(workspace, "_transfer_identity", side_effect=AssertionError("migration repeated")):
            SQLiteWorkspaceStore(self.path)

    def test_startup_retries_a_real_write_lock(self):
        self.legacy(20)
        connect = sqlite3.connect
        failures = []
        release = threading.Event()

        class ShortTimeoutConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == "PRAGMA busy_timeout=5000":
                    sql = "PRAGMA busy_timeout=20"
                try:
                    return super().execute(sql, parameters)
                except sqlite3.OperationalError as error:
                    failures.append(str(error))
                    release.set()
                    raise

        with closing(connect(self.path, check_same_thread=False, isolation_level=None)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            def unlock():
                release.wait(timeout=2)
                blocker.commit()
            thread = threading.Thread(target=unlock)
            thread.start()
            try:
                with patch.object(workspace.sqlite3, "connect", side_effect=lambda *args, **kwargs: connect(*args, **kwargs, factory=ShortTimeoutConnection)):
                    SQLiteWorkspaceStore(self.path)
            finally:
                release.set()
                thread.join(timeout=2)
        self.assertTrue(failures)
        self.assertTrue(all("locked" in error for error in failures), failures)
        with closing(connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE transfer_id IS NULL").fetchone()[0], 0)

    def test_locked_batch_rolls_back_and_is_retried(self):
        self.legacy(4001)
        original = SQLiteWorkspaceStore._connection
        failed_batches = []

        @contextmanager
        def lock_batch(store, **kwargs):
            with original(store, **kwargs) as connection:
                before = connection.total_changes
                yield connection
                updates = connection.total_changes - before
                if updates and not failed_batches:
                    failed_batches.append(updates)
                    raise sqlite3.OperationalError("database is locked")

        with patch.object(SQLiteWorkspaceStore, "_connection", lock_batch), patch.object(workspace.time, "sleep") as sleep:
            SQLiteWorkspaceStore(self.path)
        self.assertEqual(failed_batches, [2000])
        sleep.assert_called_once()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE transfer_id IS NOT NULL").fetchone()[0], 4001)

    def test_startup_retries_are_bounded_and_other_errors_are_not_retried(self):
        self.legacy(1)
        original = SQLiteWorkspaceStore._connection
        attempts = []
        message = "database is locked"

        @contextmanager
        def fail(store, **kwargs):
            attempts.append(kwargs)
            with original(store, **kwargs) as connection:
                yield connection
                raise sqlite3.OperationalError(message)

        with patch.object(SQLiteWorkspaceStore, "_connection", fail), patch.object(workspace.time, "sleep") as sleep:
            with self.assertRaises(WorkspaceError) as caught:
                SQLiteWorkspaceStore(self.path)
            self.assertEqual(caught.exception.code, "workspace_busy")
            self.assertEqual(str(caught.exception), "Workspace storage is busy while upgrading stored events")
            self.assertEqual(len(attempts), 4)
            self.assertEqual(sleep.call_count, 3)
            self.assertTrue(all(0 < call.args[0] <= 0.1 for call in sleep.call_args_list))
            attempts.clear()
            sleep.reset_mock()
            message = "disk full"
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk full"):
                SQLiteWorkspaceStore(self.path)
            self.assertEqual(len(attempts), 1)
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
