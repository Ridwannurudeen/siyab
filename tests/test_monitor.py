import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from intel import monitor


def event(subject="watched.near", height=10, timestamp_ms=100000):
    return {"kind": "feed_event", "version": "1", "signal": "watch_account_key_added",
            "subject": subject, "detail": "Recorded key change", "source": {"provider": "fixture"},
            "observed": {"block_height": height, "timestamp_ms": timestamp_ms}}


def write_event(directory, document):
    body = (json.dumps(document, sort_keys=True) + "\n").encode()
    path = directory / (hashlib.sha256(body).hexdigest() + ".json")
    path.write_bytes(body)
    return path, body


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.calls = []
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, id TEXT UNIQUE, notify INTEGER)")

    def ingest_event(self, event_id, document, notify=False):
        self.calls.append((event_id, document, notify))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT OR IGNORE INTO events(id,notify) VALUES (?,?)", (event_id, notify))
            return connection.execute("SELECT seq FROM events WHERE id=?", (event_id,)).fetchone()[0]

    def notifications(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            return connection.execute("SELECT id FROM events WHERE notify=1").fetchall()


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.events = self.root / "events"
        self.events.mkdir()
        self.store = Journal(self.root / "workspace.sqlite3")
        self.now = 100.0

    def observer(self, **kwargs):
        return monitor.Monitor(self.store, self.events, clock=lambda: self.now, **kwargs)

    def test_backfill_is_bounded_persistent_and_never_replays_notifications(self):
        for index in range(3):
            write_event(self.events, event(height=index))
        observer = self.observer(batch_size=1)
        first = observer.run_once()
        self.assertEqual((first["indexed"], first["backfill_pending"]), (1, 2))
        self.assertEqual(self.store.notifications(), [])
        new_path, _ = write_event(self.events, event(height=100))
        restarted = self.observer(batch_size=1)
        for _ in range(4):
            last = restarted.run_once()
        self.assertEqual((last["indexed"], last["pending"], last["backfill_pending"]), (4, 0, 0))
        self.assertEqual(self.store.notifications(), [(new_path.stem,)])
        self.assertEqual(sum(not notify for _, _, notify in self.store.calls), 3)
        self.observer().run_once()
        self.assertEqual(len(self.store.calls), 4)

    def test_new_evidence_at_an_older_height_is_not_lost(self):
        write_event(self.events, event(height=100))
        observer = self.observer()
        observer.run_once()
        path, _ = write_event(self.events, event(height=1, timestamp_ms=101000))
        observer.run_once()
        self.assertEqual(self.store.notifications(), [(path.stem,)])

    def test_partial_publication_is_retried_without_marking_it_processed(self):
        observer = self.observer()
        observer.run_once()
        path, body = write_event(self.events, event())
        path.write_bytes(body[:15])
        failed = observer.run_once()
        self.assertEqual((failed["indexed"], failed["retry_pending"]), (0, 1))
        self.assertEqual(self.store.calls, [])
        path.write_bytes(body)
        self.now += 301
        passed = observer.run_once()
        self.assertEqual((passed["indexed"], passed["retry_pending"]), (1, 0))
        self.assertEqual(self.store.notifications(), [(path.stem,)])
        self.assertEqual(path.read_bytes(), body)

    def test_invalid_names_and_unreadable_documents_do_not_starve_valid_events(self):
        observer = self.observer(batch_size=1)
        observer.run_once()
        (self.events / "legacy.json").write_text("{}")
        bad = event()
        bad["observed"]["block_height"] = True
        write_event(self.events, bad)
        good, _ = write_event(self.events, event(height=20))
        for _ in range(3):
            status = observer.run_once()
        self.assertEqual(status["ignored_files"], 1)
        self.assertEqual(status["retry_pending"], 1)
        self.assertEqual(self.store.notifications(), [(good.stem,)])

    def test_symlink_outside_evidence_directory_is_never_indexed(self):
        observer = self.observer()
        observer.run_once()
        outside, _ = write_event(self.root, event())
        try:
            (self.events / outside.name).symlink_to(outside)
        except OSError as error:
            self.skipTest("creating symlinks is unavailable: " + type(error).__name__)
        status = observer.run_once()
        self.assertEqual(status["indexed"], 0)
        self.assertEqual(status["retry_pending"], 1)
        self.assertEqual(self.store.calls, [])

    def test_missing_source_does_not_complete_bootstrap_then_replay_old_events(self):
        self.events.rmdir()
        observer = self.observer()
        unavailable = observer.run_once()
        self.assertFalse(unavailable["initialized"])
        self.assertFalse(unavailable["source_available"])
        self.events.mkdir()
        write_event(self.events, event())
        available = observer.run_once()
        self.assertTrue(available["initialized"])
        self.assertTrue(available["source_available"])
        self.assertEqual(self.store.notifications(), [])

    def test_leases_prevent_concurrent_processing_and_recover_after_commit_crash(self):
        first = self.observer()
        first.run_once()
        path, _ = write_event(self.events, event())
        entered, release = threading.Event(), threading.Event()
        original = self.store.ingest_event

        def slow_ingest(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)

        failures = []

        def work():
            try:
                first.run_once()
            except Exception as error:
                failures.append(error)

        with mock.patch.object(self.store, "ingest_event", side_effect=slow_ingest):
            thread = threading.Thread(target=work)
            thread.start()
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(self.observer().run_once()["processed_this_run"], 0)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.store.notifications(), [(path.stem,)])

        another, _ = write_event(self.events, event(height=30))
        with mock.patch.object(first, "_finish", side_effect=RuntimeError("process stopped after journal commit")):
            with self.assertRaisesRegex(RuntimeError, "process stopped"):
                first.run_once()
        self.now += monitor.LEASE_MS / 1000 + 1
        recovered = self.observer().run_once()
        self.assertEqual(recovered["pending"], 0)
        self.assertEqual(set(self.store.notifications()), {(path.stem,), (another.stem,)})
        self.assertEqual(len(self.store.calls), 3)

    def test_status_is_read_only_truthful_and_stale_service_is_not_running(self):
        missing = self.root / "missing.sqlite3"
        self.assertFalse(monitor.read_status(missing)["running"])
        self.assertFalse(missing.exists())
        observer = self.observer(mode="service")
        status = observer.run_once()
        self.assertTrue(status["running"])
        self.assertFalse(status["external_delivery_configured"])
        self.assertEqual(status["delivery_mode"], "team_inbox")
        self.assertNotIn(str(self.events), json.dumps(status))
        self.now += monitor.STALE_AFTER_MS / 1000 + 1
        self.assertFalse(observer.status()["running"])
        self.assertFalse(monitor.read_status(observer.state_path, clock=lambda: self.now)["running"])
        observer.stop()
        self.assertFalse(observer.status()["running"])

    def test_state_cannot_be_reused_for_a_different_source(self):
        self.observer().run_once()
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "different evidence directory"):
            monitor.Monitor(self.store, other)

    def test_oversized_event_is_bounded_and_does_not_open_network(self):
        observer = self.observer()
        observer.run_once()
        huge = event()
        huge["detail"] = "x" * monitor.MAX_EVENT_BYTES
        write_event(self.events, huge)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("monitor must not deliver externally")):
            status = observer.run_once()
        self.assertEqual(status["retry_pending"], 1)
        self.assertEqual(self.store.calls, [])

    def test_nonstandard_json_constants_are_not_indexed(self):
        observer = self.observer()
        observer.run_once()
        document = event()
        document["source"]["unreadable"] = float("nan")
        write_event(self.events, document)
        self.assertEqual(observer.run_once()["retry_pending"], 1)
        self.assertEqual(self.store.calls, [])

    def test_continuous_run_returns_stopped_health_after_stop_request(self):
        stop = threading.Event()
        stop.set()
        result = monitor.run(self.store, self.events, stop_event=stop)
        self.assertFalse(result["running"])


class WorkspaceMonitorIntegrationTest(unittest.TestCase):
    def test_matching_is_durable_team_scoped_and_candidates_are_not_external_delivery(self):
        from intel.workspace import SQLiteWorkspaceStore, WorkspaceError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "events"
            source.mkdir()
            now = [100.0]
            store = SQLiteWorkspaceStore(root / "workspace.sqlite3", clock=lambda: now[0])
            alice = store.register("alice-monitor", "test password with enough length")["user"]["id"]
            bob = store.register("bob-monitor", "another password with enough length")["user"]["id"]
            team = store.list_teams(alice)[0]["id"]
            other = store.list_teams(bob)[0]["id"]
            store.add_watch(alice, team, "watched.near")
            store.add_watch(alice, team, "second.near")
            store.add_watch(bob, other, "different.near")
            write_event(source, event())
            observer = monitor.Monitor(store, source, clock=lambda: now[0])
            observer.run_once()
            self.assertEqual(store.list_inbox(alice, team)["items"], [])
            now[0] = 101
            current = event(height=1, timestamp_ms=101000)
            current["other_account_id"] = "second.near"
            current["signal"] = "full_access_key_then_outflow"
            write_event(source, current)
            observer.run_once()
            persisted = SQLiteWorkspaceStore(store.path, clock=lambda: now[0])
            monitor.Monitor(persisted, source, clock=lambda: now[0]).run_once()
            rows = persisted.list_inbox(alice, team)["items"]
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["candidate"])
            self.assertFalse(rows[0]["external_delivery_eligible"])
            self.assertEqual(persisted.list_inbox(bob, other)["items"], [])
            with self.assertRaises(WorkspaceError):
                persisted.list_inbox(bob, team)
            self.assertEqual(len(persisted.events_after()["items"]), 2)


if __name__ == "__main__":
    unittest.main()
