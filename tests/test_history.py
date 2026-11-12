import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

from gate import rpc
from intel import accounts, budget, history, investigation_jobs
from intel.accounts import AccountReadError
from intel.workspace import SQLiteWorkspaceStore


def transfer(index, *, subject="alice.near", other="bob.near", timestamp="1000000000000", outgoing=True):
    sender, receiver = (subject, other) if outgoing else (other, subject)
    identity = {"receipt_id": "receipt" + str(index), "transfer_index": index, "asset_id": "native:near",
                "from": sender, "to": receiver, "amount": "1000000000000000000000001"}
    item_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**identity, "id": item_id, "account_id": subject, "other_account_id": other,
            "signed_amount": ("-" if outgoing else "") + identity["amount"], "block_timestamp": timestamp,
            "timestamp_ms": int(timestamp) // 1000000, "block_height": "12", "transaction_hash": "tx" + str(index),
            "human_amount": None, "usd_amount": None, "method_name": None,
            "source": {"provider": "transfers.main.fastnear.com", "request_sha256": "a" * 64,
                       "fetched_at": "2026-11-10T00:00:00Z"}}


def page(items, *, cursor=None, account="alice.near"):
    return {"account_id": account, "network": "mainnet", "items": items,
            "pagination": {"limit": 100, "next_cursor": cursor},
            "source": {"provider": "transfers.main.fastnear.com", "fetched_at": "2026-11-10T00:00:00Z"},
            "observed_at": "2026-11-10T00:00:00Z", "coverage": {"scope": "One provider page"}}


def later(payload, stamp):
    moved = json.loads(json.dumps(payload))
    moved["observed_at"] = moved["source"]["fetched_at"] = stamp
    for item in moved["items"]:
        item["source"]["fetched_at"] = stamp
    return moved


class Reader:
    def __init__(self):
        self.pages = {}
        self.calls = []

    def transfers(self, account, cursor=None, limit=25):
        self.calls.append((account, cursor, limit))
        answer = self.pages[(account, cursor)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    transactions = transfers


class AccountHistoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 1000.0
        self.path = self.root / "history.sqlite3"
        self.history = history.AccountHistory(self.path, clock=lambda: self.now)

    def test_rows_survive_restart_dedupe_and_sort_exact_nanoseconds(self):
        first = transfer(1, timestamp="1794308552765806506")
        second = transfer(2, timestamp="1794308552765806507")
        self.history.record_page("alice.near", "transfers", page([second, first]))
        self.history.record_page("alice.near", "transfers", page([second, first]))
        restarted = history.AccountHistory(self.path, clock=lambda: self.now)
        initial = restarted.list_records("alice.near", "transfers", limit=1)
        self.assertEqual(initial["items"][0]["id"], second["id"])
        following = restarted.list_records("alice.near", "transfers", before=initial["pagination"]["next_before"], limit=1)
        self.assertEqual(following["items"][0]["block_timestamp"], first["block_timestamp"])
        self.assertEqual(restarted.list_records("bob.near", "transfers")["items"], [])
        self.assertEqual(restarted.list_records("alice.near", "transfers")["coverage"]["stored_rows"], 2)

    def test_late_record_uses_timestamp_keyset_instead_of_sequence_only(self):
        newest = transfer(1, timestamp="1000000002")
        oldest = transfer(2, timestamp="1000000000")
        self.history.record_page("alice.near", "transfers", page([newest, oldest]))
        first = self.history.list_records("alice.near", "transfers", limit=1)
        late = transfer(3, timestamp="1000000001")
        self.history.record_page("alice.near", "transfers", page([late]))
        later = self.history.list_records("alice.near", "transfers", before=first["pagination"]["next_before"])
        self.assertEqual([row["id"] for row in later["items"]], [late["id"], oldest["id"]])

    def test_graph_snapshot_exact_bytes_match_hash_and_corruption_is_rejected(self):
        graph = {"kind": "flow", "root": "alice.near", "nodes": [{"id": "alice.near", "label": "Alice", "depth": 0}],
                 "edges": [], "records": [], "total_nodes": 1, "total_edges": 0, "total_records": 0,
                 "source": {"provider": "FastNEAR Transfers API", "network": "mainnet", "recorded": False},
                 "coverage": {"truncated": True}, "bounds": {"requested_depth": 1, "reached_depth": 0}}
        saved = self.history.save_graph(graph)
        body = self.history.get_graph_bytes(saved["id"])
        self.assertEqual(hashlib.sha256(body).hexdigest(), saved["id"])
        self.assertEqual(json.loads(body), graph)
        self.assertEqual(self.history.get_graph(saved["id"]), graph)
        self.assertEqual(saved["url"], "/live/account-data/" + saved["id"] + ".json")
        self.assertIsNone(self.history.get_graph("a" * 64))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE graphs SET body=? WHERE id=?", (b"{}\n", saved["id"]))
        with self.assertRaises(history.HistoryReadError):
            self.history.get_graph_bytes(saved["id"])

    def test_private_workspace_data_cannot_be_published_as_a_snapshot(self):
        graph = {"kind": "flow", "root": "alice.near", "source": {"network": "mainnet"}, "notes": "private"}
        with self.assertRaises(history.HistoryInputError):
            self.history.save_graph(graph)
        with self.assertRaises(history.HistoryInputError):
            self.history.record_page("alice.near", "transfers", dict(page([]), team_id="private"))

    def test_opaque_backfill_cursor_persists_and_two_page_budget_is_enforced(self):
        reader = Reader()
        reader.pages["alice.near", None] = page([transfer(1)], cursor="opaque+/=")
        reader.pages["alice.near", "opaque+/="] = page([transfer(2)], cursor="next-token")
        reader.pages["alice.near", "next-token"] = page([transfer(3)])
        first = self.history.backfill_account(reader, "alice.near", "transfers", max_pages=2)
        self.assertEqual((first["pages"], first["next_cursor"]), (2, "next-token"))
        restarted = history.AccountHistory(self.path, clock=lambda: self.now)
        self.now += 61
        last = restarted.backfill_account(reader, "alice.near", "transfers", max_pages=2)
        self.assertEqual((last["pages"], last["next_cursor"]), (1, None))
        self.assertTrue(last["coverage"]["provider_pages_exhausted"])
        self.assertFalse(last["coverage"]["history_complete"])
        self.assertEqual(reader.calls[-1][1], "next-token")
        self.assertEqual(self.history.backfill_account(reader, "alice.near", "transfers")["pages"], 0)

    def test_backfill_failure_keeps_successful_page_and_resumes_without_replaying_it(self):
        reader = Reader()
        reader.pages["alice.near", None] = page([transfer(1)], cursor="retry")
        reader.pages["alice.near", "retry"] = AccountReadError("unavailable")
        with self.assertRaises(AccountReadError):
            self.history.backfill_account(reader, "alice.near", "transfers")
        self.assertEqual(len(self.history.list_records("alice.near", "transfers")["items"]), 1)
        reader.pages["alice.near", "retry"] = page([transfer(2)])
        self.now += 61
        self.history.backfill_account(reader, "alice.near", "transfers")
        self.assertEqual(reader.calls[-1][1], "retry")

    def workspace(self):
        store = SQLiteWorkspaceStore(self.root / "workspace.sqlite3", clock=lambda: self.now)
        user = store.register("alice", "alice-password-123")["user"]["id"]
        team = store.list_teams(user)[0]["id"]
        store.add_watch(user, team, "alice.near")
        return store, user, team

    def test_previous_watch_state_migrates_in_place_without_becoming_fresh(self):
        path = self.root / "previous-history.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE watch_poll (account TEXT PRIMARY KEY,initialized INTEGER NOT NULL DEFAULT 0,next_due_ms INTEGER NOT NULL DEFAULT 0,last_success_ms INTEGER,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,coverage_gap INTEGER NOT NULL DEFAULT 0,lease_token TEXT,lease_until_ms INTEGER NOT NULL DEFAULT 0)")
            connection.execute("CREATE TABLE history_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1),mode TEXT,heartbeat_ms INTEGER,last_success_ms INTEGER,last_tick_ms INTEGER,stopped_ms INTEGER,last_error TEXT)")
            connection.execute("INSERT INTO watch_poll(account,initialized,next_due_ms) VALUES('alice.near',1,5)")
            connection.execute("INSERT INTO history_state(singleton,last_tick_ms) VALUES(1,10)")
        archive = history.AccountHistory(path, clock=lambda: self.now)
        history.AccountHistory(path, clock=lambda: self.now)
        with archive._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            row = connection.execute("SELECT initialized,next_due_ms,reserved,added_ms FROM watch_poll").fetchone()
            self.assertEqual(tuple(row), (1, 5, 0, 0))
            self.assertEqual(tuple(connection.execute("SELECT last_tick_ms,tick_count FROM history_state").fetchone()), (10, 0))

    def test_concurrent_history_starts_serialize_guarded_column_migrations(self):
        path = self.root / "concurrent-history.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE watch_poll (account TEXT PRIMARY KEY,initialized INTEGER NOT NULL DEFAULT 0,next_due_ms INTEGER NOT NULL DEFAULT 0,last_success_ms INTEGER,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,coverage_gap INTEGER NOT NULL DEFAULT 0,lease_token TEXT,lease_until_ms INTEGER NOT NULL DEFAULT 0)")
            connection.execute("CREATE TABLE history_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1),mode TEXT,heartbeat_ms INTEGER,last_success_ms INTEGER,last_tick_ms INTEGER,stopped_ms INTEGER,last_error TEXT)")
            connection.execute("INSERT INTO history_state(singleton) VALUES(1)")
        read_ready, second_done = threading.Event(), threading.Event()
        local = threading.local()
        original_connect = sqlite3.connect

        class MigratingConnection(sqlite3.Connection):
            def execute(inner, sql, *args):
                result = super().execute(sql, *args)
                if sql == "PRAGMA table_info(watch_poll)" and getattr(local, "first", False):
                    result = result.fetchall()
                    read_ready.set()
                    if not inner.in_transaction:
                        self.assertTrue(second_done.wait(5))
                return result

        def connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=MigratingConnection)

        def first():
            local.first = True
            return history.AccountHistory(path, clock=lambda: self.now)

        def second():
            self.assertTrue(read_ready.wait(5))
            try:
                return history.AccountHistory(path, clock=lambda: self.now)
            finally:
                second_done.set()

        with mock.patch.object(history.sqlite3, "connect", connect), ThreadPoolExecutor(max_workers=2) as pool:
            starts = [pool.submit(first), pool.submit(second)]
            archives = [start.result(timeout=10) for start in starts]
        for archive in archives:
            with archive._connection() as connection:
                self.assertEqual(tuple(connection.execute("SELECT tick_count FROM history_state").fetchone()), (0,))
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(watch_poll)")}
                self.assertTrue({"reserved", "added_ms"}.issubset(columns))

    def test_watch_tier_refreshes_without_spending_a_closed_or_idle_tick(self):
        store = mock.Mock()
        row = {"account": "alice.near", "created_ms": int(self.now * 1000), "reserved": 0}
        store.watch_schedule.return_value = [row]
        reader = Reader()
        reader.pages["alice.near", None] = page([])
        self.history.poll_watches(store, reader)
        row["reserved"] = 1
        self.now += 1
        self.history.poll_watches(store, reader)
        with self.history._connection() as connection:
            self.assertEqual(tuple(connection.execute("SELECT reserved,added_ms FROM watch_poll").fetchone()), (1, 1000000))
            self.assertEqual(connection.execute("SELECT tick_count FROM history_state").fetchone()[0], 1)
        self.now += 30
        self.history.poll_watches(store, reader)
        with self.history._connection() as connection:
            self.assertEqual(connection.execute("SELECT tick_count FROM history_state").fetchone()[0], 1)
        self.assertEqual(reader.calls, [("alice.near", None, 100)])

    def test_baseline_routes_only_transfers_at_or_after_watch_creation(self):
        store, user, team = self.workspace()
        other_user = store.register("carol", "carol-password-123")["user"]["id"]
        other_team = store.list_teams(other_user)[0]["id"]
        added_ns = int(self.now * 1000) * 1000000
        items = [transfer(number, timestamp=str(added_ns + offset)) for number, offset in ((1, -1), (2, 0), (3, 1))]
        self.now += 30
        reader = Reader()
        reader.pages["alice.near", None] = page(items)
        self.history.poll_watches(store, reader)
        inbox = store.list_inbox(user, team)["items"]
        self.assertEqual({item["event"]["receipt_id"] for item in inbox}, {"receipt2", "receipt3"})
        self.assertEqual(store.list_inbox(other_user, other_team)["items"], [])
        self.assertEqual(self.history.list_records("alice.near", "transfers")["coverage"]["stored_rows"], 3)
        self.now += 61
        self.history.poll_watches(store, reader)
        self.assertEqual(len(store.list_inbox(user, team)["items"]), 2)

    def test_uninitialized_legacy_watch_seeds_seen_without_events(self):
        store = mock.Mock()
        store.ingest_event.return_value = 1
        store.watch_schedule.return_value = [{"account": "alice.near", "created_ms": int(self.now * 1000), "reserved": 0}]
        with self.history._connection(write=True) as connection:
            connection.execute("INSERT INTO watch_poll(account) VALUES('alice.near')")
        reader = Reader()
        items = [transfer(number) for number in range(100)]
        reader.pages["alice.near", None] = page(items)
        self.history.poll_watches(store, reader)
        with self.history._connection() as connection:
            self.assertEqual({row[0] for row in connection.execute("SELECT row_id FROM watch_seen WHERE account='alice.near'")}, {item["id"] for item in items})
            self.assertEqual(tuple(connection.execute("SELECT initialized,added_ms FROM watch_poll WHERE account='alice.near'").fetchone()), (1, 0))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0], 0)
        store.ingest_event.assert_not_called()

    def test_watch_lanes_follow_each_slot_and_idle_shares_fall_through(self):
        orders = (("reserved", "fresh", "rest"), ("fresh", "reserved", "rest"),
                  ("reserved", "fresh", "rest"), ("rest", "reserved", "fresh"))
        for slot, order in enumerate(orders):
            for absent in ((), (order[0],), order[:2]):
                with self.subTest(slot=slot, absent=absent):
                    archive = history.AccountHistory(self.root / ("lanes" + str(slot) + str(len(absent)) + ".sqlite3"), clock=lambda: self.now)
                    schedule = [{"account": "reserved.near", "created_ms": 1, "reserved": 1},
                                {"account": "fresh.near", "created_ms": int(self.now * 1000), "reserved": 0},
                                {"account": "rest.near", "created_ms": 1, "reserved": 0}]
                    schedule = [row for row in schedule if row["account"].split(".")[0] not in absent]
                    store = mock.Mock()
                    store.watch_schedule.return_value = schedule
                    reader = Reader()
                    for row in schedule:
                        reader.pages[row["account"], None] = page([], account=row["account"])
                    with archive._connection(write=True) as connection:
                        connection.execute("UPDATE history_state SET tick_count=? WHERE singleton=1", (slot,))
                    archive.poll_watches(store, reader)
                    self.assertEqual(reader.calls, [(order[len(absent)] + ".near", None, 100)])
                    with archive._connection() as connection:
                        self.assertEqual(connection.execute("SELECT tick_count FROM history_state").fetchone()[0], slot + 1)

    def test_churned_public_watches_cannot_take_reserved_slots(self):
        reader = Reader()
        store = mock.Mock()
        store.ingest_event.return_value = 1
        for slot in range(4):
            fresh = "churn" + str(slot) + ".near"
            schedule = [{"account": "reserved.near", "created_ms": 1, "reserved": 1},
                        {"account": fresh, "created_ms": int(self.now * 1000), "reserved": 0}]
            store.watch_schedule.return_value = schedule
            for row in schedule:
                reader.pages[row["account"], None] = page([], account=row["account"])
            self.history.poll_watches(store, reader)
            self.now += 30
        self.assertEqual([call[0] for call in reader.calls], ["reserved.near", "churn1.near", "reserved.near", "churn3.near"])

    def test_first_poll_baselines_then_new_ordinary_transfers_reach_team_inbox(self):
        store, user, team = self.workspace()
        reader = Reader()
        old = transfer(1, timestamp="999000000000")
        reader.pages["alice.near", None] = page([old])
        self.history.poll_watches(store, reader)
        self.assertEqual(store.list_inbox(user, team)["items"], [])
        self.now += 61
        fresh = transfer(2, timestamp=str(int(self.now * 1000000000)))
        reader.pages["alice.near", None] = page([fresh, old])
        self.history.poll_watches(store, reader)
        inbox = store.list_inbox(user, team)["items"]
        self.assertEqual(len(inbox), 1)
        document = inbox[0]["event"]
        self.assertEqual(document["signal"], "account_transfer_observed")
        self.assertEqual(document["amount_yocto"], fresh["amount"])
        self.assertEqual(document["accounts"], ["alice.near", "bob.near"])
        self.assertNotIn("alice.near", json.dumps(self.history.status()))
        self.now += 61
        self.history.poll_watches(store, reader)
        self.assertEqual(len(store.list_inbox(user, team)["items"]), 1)

    def test_new_counterparty_baseline_cannot_suppress_existing_watch_alert(self):
        store, user, team = self.workspace()
        reader = Reader()
        reader.pages["alice.near", None] = page([])
        self.history.poll_watches(store, reader)
        self.now += 30
        store.add_watch(user, team, "bob.near")
        self.now += 1
        stamp = str(int(self.now * 1000000000))
        reader.pages["bob.near", None] = page([
            transfer(2, subject="bob.near", other="alice.near", outgoing=False, timestamp=stamp)
        ], account="bob.near")
        self.history.poll_watches(store, reader)
        self.assertEqual(len(store.list_inbox(user, team)["items"]), 1)
        self.now += 60
        reader.pages["alice.near", None] = page([transfer(2, timestamp=stamp)])
        self.history.poll_watches(store, reader)
        self.assertEqual(len(store.list_inbox(user, team)["items"]), 1)
        self.assertEqual(len(store.events_after()["items"]), 1)
        self.now += 30
        self.history.poll_watches(store, reader)
        self.assertEqual(len(store.list_inbox(user, team)["items"]), 1)

    def test_mirrored_multiwatch_transfer_creates_one_team_inbox_event(self):
        store, user, team = self.workspace()
        store.add_watch(user, team, "bob.near")
        reader = Reader()
        reader.pages["alice.near", None] = page([])
        reader.pages["bob.near", None] = page([], account="bob.near")
        self.history.poll_watches(store, reader)
        self.now += 30
        self.history.poll_watches(store, reader)
        self.now += 61
        stamp = str(int(self.now * 1000000000))
        reader.pages["alice.near", None] = page([transfer(2, timestamp=stamp)])
        reader.pages["bob.near", None] = page([transfer(2, subject="bob.near", other="alice.near", outgoing=False, timestamp=stamp)], account="bob.near")
        self.history.poll_watches(store, reader)
        self.now += 30
        self.history.poll_watches(store, reader)
        self.assertEqual(len(store.list_inbox(user, team)["items"]), 1)
        self.assertEqual(len(store.events_after()["items"]), 1)

    def test_provider_retry_does_not_mark_failed_account_successful_or_starve_another_watch(self):
        store, user, team = self.workspace()
        store.add_watch(user, team, "bob.near")
        reader = Reader()
        reader.pages["alice.near", None] = AccountReadError("secret URL must not leak")
        reader.pages["bob.near", None] = page([], account="bob.near")
        result = self.history.poll_watches(store, reader)
        self.assertEqual(result["retry_pending"], 1)
        self.assertNotIn("secret URL", json.dumps(result))
        self.now += 30
        self.history.poll_watches(store, reader)
        self.assertEqual(reader.calls[-1][0], "bob.near")
        self.assertEqual(self.history.status()["initialized_accounts"], 1)

    def test_global_watch_claims_wait_thirty_seconds_across_restart(self):
        store, user, team = self.workspace()
        store.add_watch(user, team, "bob.near")
        reader = Reader()
        reader.pages["alice.near", None] = page([])
        reader.pages["bob.near", None] = page([], account="bob.near")
        self.history.poll_watches(store, reader)
        self.now += 29.999
        restarted = history.AccountHistory(self.path, clock=lambda: self.now)
        restarted.poll_watches(store, reader)
        self.assertEqual(reader.calls, [("alice.near", None, 100)])
        self.now += 0.001
        restarted.poll_watches(store, reader)
        self.assertEqual(reader.calls, [("alice.near", None, 100), ("bob.near", None, 100)])
        self.assertEqual(history.GLOBAL_INTERVAL_MS, 30000)

    def test_every_watch_poll_reads_one_page_and_reports_a_missing_anchor(self):
        store, user, team = self.workspace()
        reader = Reader()
        old = transfer(1, timestamp="999000000000")
        reader.pages["alice.near", None] = page([old], cursor="older")
        reader.pages["alice.near", "older"] = page([transfer(0)])
        self.history.poll_watches(store, reader)
        self.assertEqual(reader.calls, [("alice.near", None, 100)])
        self.assertEqual(store.list_inbox(user, team)["items"], [])
        self.now += 61
        fresh = transfer(2, timestamp=str(int(self.now * 1000000000)))
        reader.pages["alice.near", None] = page([fresh], cursor="anchor")
        reader.pages["alice.near", "anchor"] = page([old], cursor="older")
        self.history.poll_watches(store, reader)
        self.assertEqual(reader.calls, [("alice.near", None, 100), ("alice.near", None, 100)])
        self.assertEqual(len(store.list_inbox(user, team)["items"]), 1)
        self.assertEqual(self.history.status()["coverage_gap_accounts"], 1)
        self.now += 61
        self.history.poll_watches(store, reader)
        self.assertEqual(reader.calls[-1], ("alice.near", None, 100))
        self.assertEqual(len(reader.calls), 3)

    def test_global_poll_budget_and_cursor_boundaries(self):
        store, _, _ = self.workspace()
        reader = Reader()
        reader.pages["alice.near", None] = page([])
        self.history.poll_watches(store, reader)
        self.history.poll_watches(store, reader)
        self.assertEqual(len(reader.calls), 1)
        with self.assertRaises(history.HistoryInputError):
            self.history.backfill_account(reader, "alice.near", "transfers", max_pages=3)
        with self.assertRaises(history.HistoryInputError):
            self.history.list_records("alice.near", "transfers", limit=101)
        with self.assertRaises(history.HistoryInputError):
            self.history.get_graph_bytes("../outside")
        self.assertFalse(history.read_status(self.root / "missing.sqlite3")["running"])
        self.assertFalse((self.root / "missing.sqlite3").exists())

    def test_outbox_survives_workspace_failure_and_restart_without_duplicate_delivery(self):
        store, user, team = self.workspace()
        reader = Reader()
        reader.pages["alice.near", None] = page([])
        self.history.poll_watches(store, reader)
        self.now += 61
        reader.pages["alice.near", None] = page([transfer(5, timestamp=str(int(self.now * 1000000000)))])

        class Inbox:
            fail = True

            def watch_schedule(self):
                return store.watch_schedule()

            def ingest_event(self, *args, **kwargs):
                if self.fail:
                    raise OSError("private delivery detail")
                return store.ingest_event(*args, **kwargs)

        inbox = Inbox()
        failed = self.history.poll_watches(inbox, reader)
        self.assertEqual(failed["pending_events"], 1)
        self.assertEqual(store.list_inbox(user, team)["items"], [])
        inbox.fail = False
        self.now += 5
        restarted = history.AccountHistory(self.path, clock=lambda: self.now)
        restarted.poll_watches(inbox, reader)
        restarted.poll_watches(inbox, reader)
        self.assertEqual(len(store.list_inbox(user, team)["items"]), 1)
        document = store.list_inbox(user, team)["items"][0]
        body = restarted.get_event_bytes(document["event_id"])
        self.assertEqual(hashlib.sha256(body).hexdigest(), document["event_id"])
        self.assertEqual(restarted.status()["pending_events"], 0)

    def test_late_lower_block_event_is_archived_and_team_isolation_is_preserved(self):
        store, user, team = self.workspace()
        other_user = store.register("carol", "carol-password-123")["user"]["id"]
        other_team = store.list_teams(other_user)[0]["id"]
        reader = Reader()
        reader.pages["alice.near", None] = page([transfer(1, timestamp="999000000000")])
        self.history.poll_watches(store, reader)
        self.now += 61
        late = transfer(2, timestamp=str(int(self.now * 1000000000)))
        late["block_height"] = "1"
        reader.pages["alice.near", None] = page([late])
        self.history.poll_watches(store, reader)
        self.assertEqual(len(store.list_inbox(user, team)["items"]), 1)
        self.assertEqual(store.list_inbox(other_user, other_team)["items"], [])
        self.assertEqual(self.history.list_records("alice.near", "transfers")["items"][0]["block_height"], "1")

    def test_ft_event_does_not_label_its_base_units_as_yocto_near(self):
        item = transfer(1)
        item["asset_id"] = "nep141:token.near"
        event_id, encoded = self.history._event(item)
        document = json.loads(encoded)
        self.assertNotIn("amount_yocto", document)
        self.assertEqual(document["amount"], item["amount"])
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), event_id)

    def test_fresh_lane_gives_an_uninitialized_watch_its_baseline_first(self):
        store, user, team = self.workspace()
        reader = Reader()
        reader.pages["alice.near", None] = page([])
        reader.pages["bob.near", None] = page([], account="bob.near")
        self.history.poll_watches(store, reader)
        self.now += 70
        store.add_watch(user, team, "bob.near")
        self.history.poll_watches(store, reader)
        self.assertEqual(reader.calls[-1][0], "bob.near")

    def test_archive_pages_expose_retained_source_and_observation_time_for_every_kind(self):
        cases = {
            "transactions": dict(page([]), items=[{"transaction_hash": "tx1", "tx_block_timestamp": "1794308552765806506"}]),
            "holdings": dict(page([]), items=[{"contract_id": "wrap.near", "balance": "1234567890123456789012345"}]),
            "profile": {"account_id": "alice.near", "network": "mainnet", "native": {"amount_yocto": "1234567890123456789012345"},
                        "source": {"provider": "rpc.mainnet.near.org"}, "observed_at": "2026-11-10T00:00:00Z"},
        }
        for kind, payload in cases.items():
            with self.subTest(kind=kind):
                saved = self.history.record_page("alice.near", kind, payload)
                archived = self.history.list_records("alice.near", kind)
                self.assertEqual(archived["account_id"], "alice.near")
                self.assertEqual(archived["network"], "mainnet")
                self.assertEqual(archived["kind"], kind)
                self.assertEqual(archived["observations"], [{"page_id": saved["page_id"], "source": payload["source"],
                                                            "observed_at": payload["observed_at"]}])
                self.assertEqual(archived["items"], [payload] if kind == "profile" else payload["items"])

    def test_one_page_watch_cap_sets_a_persistent_coverage_gap_without_claiming_complete_history(self):
        store, _, _ = self.workspace()
        reader = Reader()
        reader.pages["alice.near", None] = page([transfer(1)])
        self.history.poll_watches(store, reader)
        self.now += 61
        reader.pages["alice.near", None] = page([transfer(2)], cursor="next")
        reader.pages["alice.near", "next"] = page([transfer(3)], cursor="older")
        result = self.history.poll_watches(store, reader)
        self.assertEqual(reader.calls, [("alice.near", None, 100), ("alice.near", None, 100)])
        self.assertEqual(result["coverage_gap_accounts"], 1)
        self.now += 61
        reader.pages["alice.near", None] = page([transfer(3)])
        self.history.poll_watches(store, reader)
        self.assertEqual(history.AccountHistory(self.path).status()["coverage_gap_accounts"], 1)
        self.assertFalse(self.history.list_records("alice.near", "transfers")["coverage"]["history_complete"])

    def test_service_loop_persists_heartbeat_and_marks_a_clean_stop(self):
        store, _, _ = self.workspace()
        reader = Reader()
        reader.pages["alice.near", None] = page([])
        archive_path = self.root / "service.sqlite3"

        class Stop:
            def wait(inner, interval):
                self.assertEqual(interval, 5)
                self.assertTrue(history.read_status(archive_path)["running"])
                return True

        status = history.run(store, reader, archive_path, stop_event=Stop())
        self.assertEqual(status["mode"], "service")
        self.assertFalse(status["running"])
        self.assertIsNotNone(status["last_success_ms"])

    def test_backfill_rejects_uncommitted_cursor_and_serializes_across_instances(self):
        reader = Reader()
        reader.pages["alice.near", None] = page([], cursor="committed")
        with self.assertRaises(history.HistoryInputError):
            self.history.backfill_account(reader, "alice.near", "transfers", cursor="forged")
        self.assertEqual(reader.calls, [])
        self.history.backfill_account(reader, "alice.near", "transfers", max_pages=1)
        restarted = history.AccountHistory(self.path, clock=lambda: self.now)
        with self.assertRaises(history.HistoryReadError):
            restarted.backfill_account(reader, "alice.near", "transfers")
        self.now += 61
        with self.assertRaises(history.HistoryInputError):
            restarted.backfill_account(reader, "alice.near", "transfers", cursor="forged")
        reader.pages["alice.near", "committed"] = page([])
        restarted.backfill_account(reader, "alice.near", "transfers", cursor="committed")
        with self.assertRaises(history.HistoryInputError):
            restarted.backfill_account(reader, "alice.near", "transfers", cursor="committed")

    def test_global_backfill_budget_is_persistent_and_lease_prevents_concurrent_claim(self):
        reader = Reader()
        for number in range(history.PUBLIC_BACKFILL_PAGES // 2):
            account = "account" + str(number) + ".near"
            reader.pages[account, None] = page([], account=account)
            self.history.backfill_account(reader, account, "transfers")
        restarted = history.AccountHistory(self.path, clock=lambda: self.now)
        with self.assertRaises(history.HistoryReadError):
            restarted.backfill_account(reader, "next.near", "transfers")
        self.now += 61
        reader.pages["next.near", None] = page([], account="next.near")
        original = reader.transfers

        def simultaneous(account, **kwargs):
            with self.assertRaises(history.HistoryReadError):
                restarted.backfill_account(reader, account, "transfers")
            return original(account, **kwargs)

        reader.transfers = simultaneous
        self.history.backfill_account(reader, "next.near", "transfers")

    def test_retention_capacity_refuses_new_documents_without_deleting_evidence(self):
        original = page([transfer(1)])
        self.history.record_page("alice.near", "transfers", original)
        with mock.patch.object(history, "MAX_ARCHIVE_PAGES", 1):
            self.history.record_page("alice.near", "transfers", original)
            with self.assertRaises(history.HistoryCapacityError):
                self.history.record_page("alice.near", "transfers", page([transfer(2), transfer(1)]))
        self.assertEqual(self.history.list_records("alice.near", "transfers")["items"], original["items"])

    def test_graph_and_byte_capacity_keep_existing_hash_addressed_evidence(self):
        graph = {"kind": "flow", "root": "alice.near", "nodes": [], "edges": [], "records": [],
                 "source": {"network": "mainnet"}}
        saved = self.history.save_graph(graph)
        original = self.history.get_graph_bytes(saved["id"])
        with mock.patch.object(history, "MAX_ARCHIVE_GRAPHS", 1):
            self.assertEqual(self.history.save_graph(graph), saved)
            with self.assertRaises(history.HistoryCapacityError):
                self.history.save_graph(dict(graph, root="bob.near"))
        with mock.patch.object(history, "MAX_ARCHIVE_BYTES", len(original)):
            with self.assertRaises(history.HistoryCapacityError):
                self.history.record_page("alice.near", "transfers", page([]))
        self.assertEqual(self.history.get_graph_bytes(saved["id"]), original)

    def test_a_job_read_waits_unless_the_archive_has_room_for_one_full_page(self):
        items = [dict(transfer(number), method_name="m" * 20000) for number in range(100)]
        body = history._canonical(page(items))
        self.assertGreater(len(body), history.MAX_DOCUMENT_BYTES - 100000)
        self.history.record_page("alice.near", "transfers", page(items), reserved=True)
        with self.history._connection() as connection:
            usage = connection.execute("SELECT pages,records,bytes FROM archive_usage").fetchone()
        self.assertEqual((usage["pages"], usage["records"]), (1, 100))
        self.assertLessEqual(usage["bytes"], history.JOB_PAGE_BYTES)
        for limit, used, room in (("MAX_ARCHIVE_PAGES", usage["pages"], 1), ("MAX_ARCHIVE_ROWS", usage["records"], 100), ("MAX_ARCHIVE_BYTES", usage["bytes"], history.JOB_PAGE_BYTES + history.EVENT_ARCHIVE_BYTES)):
            with self.subTest(limit=limit):
                self.now += 61
                with mock.patch.object(history, limit, used + room - 1):
                    with self.assertRaises(history.HistoryCapacityError):
                        self.history.claim_job_read("bob.near")
                with mock.patch.object(history, limit, used + room):
                    self.history.release_job_read("bob.near", self.history.claim_job_read("bob.near"))

    def test_migration_reconstructs_retained_usage_without_modifying_evidence(self):
        self.history.record_page("alice.near", "transfers", page([transfer(1)]))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executescript("DROP TABLE archive_usage;DROP TABLE backfill_control;DROP TABLE backfill_budget;PRAGMA user_version=1;")
        restarted = history.AccountHistory(self.path, clock=lambda: self.now)
        with restarted._connection() as connection:
            usage = connection.execute("SELECT * FROM archive_usage").fetchone()
            self.assertEqual((usage["pages"], usage["records"]), (1, 1))
            self.assertGreater(usage["bytes"], 0)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(len(restarted.list_records("alice.near", "transfers")["items"]), 1)

    def test_a_previous_history_database_gains_the_job_page_share_in_place(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TABLE job_read_budget")
            connection.execute("UPDATE backfill_budget SET window=?,pages=4 WHERE singleton=1", (int(self.now * 1000) // 60000,))
        restarted = history.AccountHistory(self.path, clock=lambda: self.now)
        for number in range(8):
            account = "job" + str(number) + ".near"
            restarted.release_job_read(account, restarted.claim_job_read(account))
        with self.assertRaises(history.HistoryCapacityError):
            restarted.claim_job_read("joblast.near")
        with restarted._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(tuple(connection.execute("SELECT window,pages FROM backfill_budget").fetchone()), (int(self.now * 1000) // 60000, 4))

    def test_idle_read_checkpoints_never_hold_the_checkpoint_table_shut(self):
        stamp = int(self.now * 1000)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany("INSERT INTO backfill_control VALUES (?,?,?,NULL,0)", [
                ("idle" + str(number) + ".near", ("job", "transfers", "transactions")[number % 3], stamp - history.ACCOUNT_INTERVAL_MS) for number in range(19998)])
            connection.execute("INSERT INTO backfill_control VALUES ('fresh.near','job',?,NULL,0)", (stamp - history.ACCOUNT_INTERVAL_MS + 1,))
            connection.execute("INSERT INTO backfill_control VALUES ('leased.near','transfers',?,'token',?)", (stamp - history.ACCOUNT_INTERVAL_MS, stamp + 1))
        self.history.release_job_read("new.near", self.history.claim_job_read("new.near"))
        with self.history._connection() as connection:
            self.assertEqual(sorted(row[0] for row in connection.execute("SELECT account FROM backfill_control")), ["fresh.near", "leased.near", "new.near"])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany("INSERT INTO backfill_control VALUES (?,'job',?,NULL,0)", [("busy" + str(number) + ".near", stamp) for number in range(19997)])
        with self.assertRaisesRegex(history.HistoryCapacityError, "checkpoint capacity"):
            self.history.claim_job_read("other.near")
        with self.history._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM backfill_control").fetchone()[0], 20000)

    def test_a_backfill_whose_checkpoint_was_pruned_mid_read_fails_closed(self):
        reader = Reader()
        reader.pages["alice.near", None] = page([], account="alice.near")
        original = reader.transfers

        def pruned(account, **kwargs):
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.executemany("INSERT INTO backfill_control VALUES (?,'job',?,NULL,0)", [("idle" + str(number) + ".near", int(self.now * 1000)) for number in range(19999)])
            self.now += history.ACCOUNT_INTERVAL_MS / 1000
            self.history.release_job_read("bob.near", self.history.claim_job_read("bob.near"))
            return original(account, **kwargs)

        reader.transfers = pruned
        with self.assertRaisesRegex(history.HistoryReadError, "lease expired"):
            self.history.backfill_account(reader, "alice.near", "transfers")
        self.assertEqual(reader.calls, [("alice.near", None, 100)])
        self.assertEqual(self.page_count(), (0, 0))
        with self.history._connection() as connection:
            self.assertEqual(tuple(connection.execute("SELECT cursor,exhausted FROM backfills WHERE account='alice.near'").fetchone()), (None, 0))
            self.assertEqual([row[0] for row in connection.execute("SELECT account FROM backfill_control")], ["bob.near"])

    def test_a_new_backfill_reclaims_one_idle_checkpoint_when_the_backfill_table_is_full(self):
        stamp = int(self.now * 1000)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany("INSERT INTO backfills VALUES (?,'transfers',?,?,?)", [
                ("leased.near", None, 1, 0), ("recent.near", None, 1, 0), ("done.near", None, 1, 5), ("old.near", "c", 0, 1)] + [
                ("idle" + str(number) + ".near", None, 0, stamp) for number in range(9996)])
            connection.executemany("INSERT INTO backfill_control VALUES (?,'transfers',?,?,?)", [
                ("leased.near", stamp - history.ACCOUNT_INTERVAL_MS, "token", stamp + 1),
                ("recent.near", stamp - history.ACCOUNT_INTERVAL_MS + 1, None, 0),
                ("done.near", stamp - history.ACCOUNT_INTERVAL_MS, None, 0)])
        reader = Reader()
        for account in ("new.near", "next.near"):
            reader.pages[account, None] = page([], account=account)
        self.history.backfill_account(reader, "new.near", "transfers")
        with self.history._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM backfills").fetchone()[0], 10000)
            self.assertIsNone(connection.execute("SELECT 1 FROM backfills WHERE account='done.near'").fetchone())
            self.assertEqual(sorted(row[0] for row in connection.execute("SELECT account FROM backfill_control")), ["leased.near", "new.near", "recent.near"])
        self.history.backfill_account(reader, "next.near", "transfers")
        with self.history._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM backfills").fetchone()[0], 10000)
            self.assertEqual(sorted(row[0] for row in connection.execute("SELECT account FROM backfills WHERE account IN ('leased.near','recent.near','old.near','new.near','next.near')")), ["leased.near", "new.near", "next.near", "recent.near"])
        with self.assertRaisesRegex(history.HistoryInputError, "committed continuation"):
            self.history.backfill_account(reader, "old.near", "transfers", cursor="c")
        self.assertEqual(reader.calls, [("new.near", None, 100), ("next.near", None, 100)])

    def test_a_new_backfill_waits_while_every_backfill_checkpoint_is_in_use(self):
        stamp = int(self.now * 1000)
        accounts = ["held" + str(number) + ".near" for number in range(10000)]
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany("INSERT INTO backfills VALUES (?,'transfers',NULL,1,0)", [(account,) for account in accounts])
            connection.executemany("INSERT INTO backfill_control VALUES (?,'transfers',?,?,?)", [
                (account, stamp - history.ACCOUNT_INTERVAL_MS + 1, None, 0) if number % 2 else (account, stamp - history.ACCOUNT_INTERVAL_MS, "token", stamp + 1)
                for number, account in enumerate(accounts)])
        reader = Reader()
        reader.pages["new.near", None] = page([], account="new.near")
        with self.assertRaisesRegex(history.HistoryCapacityError, "backfill checkpoint capacity"):
            self.history.backfill_account(reader, "new.near", "transfers")
        self.assertEqual(reader.calls, [])
        with self.history._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM backfills").fetchone()[0], 10000)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM backfill_control").fetchone()[0], 10000)

    def test_a_backfill_in_flight_or_inside_its_minute_keeps_its_checkpoint_and_commits(self):
        stamp = int(self.now * 1000)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany("INSERT INTO backfills VALUES (?,'transfers',NULL,0,?)", [("idle" + str(number) + ".near", stamp + 1) for number in range(9999)])
        reader = Reader()
        reader.pages["alice.near", None] = page([transfer(1)], cursor="next")
        reader.pages["alice.near", "next"] = page([transfer(2)])
        for account in ("bob.near", "carol.near"):
            reader.pages[account, None] = page([], account=account)
        original = reader.transfers

        def contended(account, **kwargs):
            reader.transfers = original
            self.history.backfill_account(reader, "bob.near", "transfers", max_pages=1)
            return original(account, **kwargs)

        reader.transfers = contended
        self.assertEqual(self.history.backfill_account(reader, "alice.near", "transfers", max_pages=1)["next_cursor"], "next")
        self.history.backfill_account(reader, "carol.near", "transfers", max_pages=1)
        with self.history._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM backfills").fetchone()[0], 10000)
            self.assertEqual(tuple(connection.execute("SELECT cursor,exhausted FROM backfills WHERE account='alice.near'").fetchone()), ("next", 0))
        self.now += 61
        self.assertIsNone(self.history.backfill_account(reader, "alice.near", "transfers", cursor="next", max_pages=1)["next_cursor"])
        self.assertEqual(reader.calls, [("bob.near", None, 100), ("alice.near", None, 100), ("carol.near", None, 100), ("alice.near", "next", 100)])
        self.assertEqual(len(self.history.list_records("alice.near", "transfers")["items"]), 2)

    def test_a_backfill_whose_checkpoint_was_reclaimed_mid_read_fails_closed(self):
        stamp = int(self.now * 1000)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany("INSERT INTO backfills VALUES (?,'transfers',NULL,0,?)", [("idle" + str(number) + ".near", stamp + 1) for number in range(9999)])
        reader = Reader()
        reader.pages["alice.near", None] = page([transfer(1)], cursor="next")
        reader.pages["bob.near", None] = page([], account="bob.near")
        original = reader.transfers

        def stalled(account, **kwargs):
            reader.transfers = original
            self.now += 61
            self.history.backfill_account(reader, "bob.near", "transfers", max_pages=1)
            return original(account, **kwargs)

        reader.transfers = stalled
        with self.assertRaisesRegex(history.HistoryReadError, "lease expired"):
            self.history.backfill_account(reader, "alice.near", "transfers", max_pages=1)
        self.assertEqual(self.history.list_records("alice.near", "transfers")["items"], [])
        with self.history._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM backfills").fetchone()[0], 10000)
            self.assertIsNone(connection.execute("SELECT 1 FROM backfills WHERE account='alice.near'").fetchone())
            self.assertIsNone(connection.execute("SELECT 1 FROM backfill_control WHERE account='alice.near'").fetchone())

    def test_a_reclaimed_backfill_starts_again_without_a_cursor_and_keeps_its_archived_pages(self):
        reader = Reader()
        reader.pages["alice.near", None] = page([transfer(1)], cursor="next")
        reader.pages["bob.near", None] = page([], account="bob.near")
        self.history.backfill_account(reader, "alice.near", "transfers", max_pages=1)
        archived = self.history.list_records("alice.near", "transfers")
        stamp = int(self.now * 1000)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            pages = connection.execute("SELECT id FROM pages WHERE account='alice.near'").fetchall()
            connection.executemany("INSERT INTO backfills VALUES (?,'transfers',NULL,0,?)", [("idle" + str(number) + ".near", stamp + 1) for number in range(9999)])
        self.now += 61
        self.history.backfill_account(reader, "bob.near", "transfers", max_pages=1)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM backfills WHERE account='alice.near'").fetchone())
            self.assertIsNone(connection.execute("SELECT 1 FROM backfill_control WHERE account='alice.near'").fetchone())
            self.assertEqual(connection.execute("SELECT id FROM pages WHERE account='alice.near'").fetchall(), pages)
        self.assertEqual(self.history.list_records("alice.near", "transfers"), archived)
        with self.assertRaisesRegex(history.HistoryInputError, "committed continuation"):
            self.history.backfill_account(reader, "alice.near", "transfers", cursor="next")
        self.assertEqual(self.history.backfill_account(reader, "alice.near", "transfers", max_pages=1)["next_cursor"], "next")
        self.assertEqual(reader.calls, [("alice.near", None, 100), ("bob.near", None, 100), ("alice.near", None, 100)])
        self.assertEqual(self.history.list_records("alice.near", "transfers")["items"], archived["items"])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT id FROM pages WHERE account='alice.near'").fetchall(), pages)
            self.assertEqual(connection.execute("SELECT cursor FROM backfills WHERE account='alice.near'").fetchone()[0], "next")

    def test_a_full_backfill_table_from_the_previous_release_still_reclaims(self):
        stamp = int(self.now * 1000)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TABLE job_read_budget")
            connection.executemany("INSERT INTO backfills VALUES (?,'transfers','c',0,?)", [("old" + str(number) + ".near", stamp - number) for number in range(10000)])
            connection.executemany("INSERT INTO backfill_control VALUES (?,'transfers',?,NULL,0)", [("old" + str(number) + ".near", stamp - history.ACCOUNT_INTERVAL_MS) for number in range(10000)])
        restarted = history.AccountHistory(self.path, clock=lambda: self.now)
        reader = Reader()
        reader.pages["new.near", None] = page([], account="new.near")
        restarted.backfill_account(reader, "new.near", "transfers", max_pages=1)
        self.assertEqual(reader.calls, [("new.near", None, 100)])
        with restarted._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM backfills").fetchone()[0], 10000)
            self.assertIsNone(connection.execute("SELECT 1 FROM backfills WHERE account='old9999.near'").fetchone())
            self.assertIsNone(connection.execute("SELECT 1 FROM backfill_control WHERE account='old9999.near'").fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM backfill_control").fetchone()[0], 10000)

    def page_count(self):
        with self.history._connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0], connection.execute("SELECT pages FROM archive_usage").fetchone()[0]

    def test_repeated_identical_reads_reuse_one_retained_page_until_a_row_is_new_or_changed(self):
        original = page([transfer(2), transfer(1)])
        first = self.history.record_page("alice.near", "transfers", original)
        again = self.history.record_page("alice.near", "transfers", later(original, "2026-11-10T00:01:00Z"))
        self.assertEqual((again["page_id"], again["inserted"], again["total_rows"]), (first["page_id"], 0, 2))
        narrower = later(page([transfer(2)]), "2026-11-10T00:02:00Z")
        narrower["items"][0]["source"]["request_sha256"] = "b" * 64
        self.assertEqual(self.history.record_page("alice.near", "transfers", narrower)["page_id"], first["page_id"])
        quiet = self.history.record_page("bob.near", "transfers", page([], account="bob.near"))
        repeated = self.history.record_page("bob.near", "transfers", later(page([], account="bob.near"), "2026-11-10T00:03:00Z"))
        self.assertEqual(repeated["page_id"], quiet["page_id"])
        self.assertEqual(self.page_count(), (2, 2))
        archived = self.history.list_records("alice.near", "transfers")
        self.assertCountEqual(archived["items"], original["items"])
        self.assertEqual(archived["observations"], [{"page_id": first["page_id"], "source": original["source"], "observed_at": original["observed_at"]}])
        changed = later(original, "2026-11-10T00:04:00Z")
        changed["items"][0]["usd_amount"] = "5"
        revised = self.history.record_page("alice.near", "transfers", changed)
        self.assertNotEqual(revised["page_id"], first["page_id"])
        fresh = self.history.record_page("alice.near", "transfers", later(page([transfer(3), transfer(2)]), "2026-11-10T00:05:00Z"))
        self.assertEqual((fresh["inserted"], fresh["total_rows"]), (1, 3))
        self.assertEqual(self.page_count(), (4, 4))
        with self.history._connection() as connection:
            for identifier, body in connection.execute("SELECT id,body FROM pages"):
                self.assertEqual(hashlib.sha256(body).hexdigest(), identifier)

    def test_profile_and_holdings_reads_at_a_later_block_reuse_one_page_until_a_balance_changes(self):
        def profile(height, stamp, amount="1234567890123456789012345"):
            return {"account_id": "alice.near", "network": "mainnet", "exists": True,
                    "native": {"amount_yocto": amount, "locked_yocto": "0", "storage_usage": 182, "code_hash": "11111111111111111111111111111111",
                               "block_height": height, "block_hash": "hash" + str(height)},
                    "labels": [], "source": {"provider": "rpc.mainnet.near.org", "request_sha256": "a" * 64, "fetched_at": stamp},
                    "observed_at": stamp, "coverage": {"scope": "Finalized account state"}}

        def holdings(height, stamp, balance="1234567890123456789012345"):
            return dict(page([], cursor=None), observed_at=stamp, items=[
                {"contract_id": "wrap.near", "balance": balance, "balance_status": "indexed", "last_update_block_height": 100,
                 "metadata": {"name": "Wrapped NEAR", "symbol": "wNEAR", "decimals": 24},
                 "metadata_source": {"provider": "rpc.mainnet.near.org", "request_sha256": "a" * 64, "fetched_at": stamp,
                                     "read_kind": "final_rpc", "recorded": False, "network": "mainnet", "block_height": height}}])

        first = self.history.record_page("alice.near", "profile", profile(200, "2026-11-10T00:00:00Z"))
        again = self.history.record_page("alice.near", "profile", profile(260, "2026-11-10T00:01:00Z"))
        self.assertEqual((again["page_id"], again["inserted"], again["total_rows"]), (first["page_id"], 0, 1))
        held = self.history.record_page("alice.near", "holdings", holdings(200, "2026-11-10T00:00:00Z"))
        self.assertEqual(self.history.record_page("alice.near", "holdings", holdings(260, "2026-11-10T00:01:00Z"))["page_id"], held["page_id"])
        self.assertEqual(self.page_count(), (2, 2))
        self.assertEqual(self.history.list_records("alice.near", "profile")["items"], [profile(200, "2026-11-10T00:00:00Z")])
        changed = self.history.record_page("alice.near", "profile", profile(300, "2026-11-10T00:02:00Z", amount="1234567890123456789012346"))
        self.assertNotEqual(changed["page_id"], first["page_id"])
        moved = self.history.record_page("alice.near", "holdings", holdings(300, "2026-11-10T00:02:00Z", balance="1"))
        self.assertNotEqual(moved["page_id"], held["page_id"])
        self.assertEqual(self.page_count(), (4, 4))
        with self.history._connection() as connection:
            for identifier, body in connection.execute("SELECT id,body FROM pages"):
                self.assertEqual(hashlib.sha256(body).hexdigest(), identifier)

    def test_quiet_watch_polls_do_not_add_archive_pages(self):
        store, _, _ = self.workspace()
        reader = Reader()
        reader.pages["alice.near", None] = page([transfer(1)])
        self.history.poll_watches(store, reader)
        for minute in range(1, 4):
            self.now += 61
            reader.pages["alice.near", None] = later(page([transfer(1)]), "2026-11-10T00:0" + str(minute) + ":00Z")
            status = self.history.poll_watches(store, reader)
        self.assertEqual(len(reader.calls), 4)
        self.assertIsNone(status["last_error"])
        self.assertEqual(self.page_count(), (1, 1))

    def test_public_reads_cannot_use_the_share_reserved_for_watches_and_jobs(self):
        with mock.patch.object(history, "MAX_ARCHIVE_PAGES", 4):
            for number in range(3):
                account = "public" + str(number) + ".near"
                self.history.record_page(account, "transfers", page([], account=account))
            with self.assertRaises(history.HistoryCapacityError):
                self.history.record_page("public3.near", "transfers", page([], account="public3.near"))
            self.history.record_page("watched.near", "transfers", page([], account="watched.near"), reserved=True)
            with self.assertRaises(history.HistoryCapacityError):
                self.history.record_page("watched2.near", "transfers", page([], account="watched2.near"), reserved=True)
        graph = {"kind": "flow", "root": "alice.near", "nodes": [], "edges": [], "records": [], "source": {"network": "mainnet"}}
        with mock.patch.object(history, "MAX_ARCHIVE_GRAPHS", 1), self.assertRaises(history.HistoryCapacityError):
            self.history.save_graph(graph)
        self.assertEqual(self.page_count(), (4, 4))

    def test_watch_alerts_continue_after_public_reads_exhaust_their_share(self):
        store, user, team = self.workspace()
        reader = Reader()
        old = transfer(1, timestamp="999000000000")
        reader.pages["alice.near", None] = page([old])
        with mock.patch.object(history, "MAX_ARCHIVE_PAGES", 4):
            self.history.poll_watches(store, reader)
            for number in range(2):
                account = "public" + str(number) + ".near"
                self.history.record_page(account, "transfers", page([], account=account))
            with self.assertRaises(history.HistoryCapacityError):
                self.history.record_page("public2.near", "transfers", page([], account="public2.near"))
            self.now += 61
            fresh = transfer(2, timestamp=str(int(self.now * 1000000000)))
            reader.pages["alice.near", None] = later(page([fresh, old]), "2026-11-10T00:01:00Z")
            status = self.history.poll_watches(store, reader)
        self.assertIsNone(status["last_error"])
        self.assertEqual(status["retry_pending"], 0)
        inbox = store.list_inbox(user, team)["items"]
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["event"]["receipt_id"], fresh["receipt_id"])

    def assert_rows_held(self, saved, payload):
        with self.history._connection() as connection:
            bodies = {row["id"]: json.loads(row["body"]) for row in connection.execute("SELECT id,body FROM pages")}
        self.assertEqual([row["item"]["id"] for row in saved["rows"]], [item["id"] for item in payload["items"]])
        for row in saved["rows"]:
            self.assertIn(row["item"], bodies[row["page_id"]]["items"])
        cited = list(dict.fromkeys(row["page_id"] for row in saved["rows"]))
        self.assertEqual(saved["observations"], [{"page_id": page_id, "source": bodies[page_id]["source"],
                                                  "observed_at": bodies[page_id]["observed_at"]} for page_id in cited])
        self.assertEqual(saved["page_id"], cited[0] if len(cited) == 1 else None)

    def test_overlapping_unchanged_reads_reuse_the_pages_that_hold_their_rows(self):
        first = self.history.record_page("alice.near", "transfers", page([transfer(2), transfer(1)]))
        second = self.history.record_page("alice.near", "transfers", later(page([transfer(3), transfer(2)]), "2026-11-10T00:01:00Z"))
        for minute in range(2, 6):
            stamp = "2026-11-10T00:0" + str(minute) + ":00Z"
            for payload, expected in ((later(page([transfer(2), transfer(1)]), stamp), first),
                                      (later(page([transfer(3), transfer(2)]), stamp), second)):
                saved = self.history.record_page("alice.near", "transfers", payload)
                self.assertEqual((saved["page_id"], saved["inserted"], saved["total_rows"]), (expected["page_id"], 0, 3))
                self.assert_rows_held(saved, payload)
        self.assertEqual(self.page_count(), (2, 2))
        carol = [transfer(index, subject="carol.near") for index in range(4)]
        self.history.record_page("carol.near", "transfers", page([carol[2], carol[1]], account="carol.near"))
        self.history.record_page("carol.near", "transfers", later(page([carol[1], carol[0]], account="carol.near"), "2026-11-10T00:06:00Z"))
        self.history.record_page("carol.near", "transfers", later(page([carol[3], carol[2]], account="carol.near"), "2026-11-10T00:07:00Z"))
        spread = later(page([carol[2], carol[1]], account="carol.near"), "2026-11-10T00:08:00Z")
        for _ in range(3):
            saved = self.history.record_page("carol.near", "transfers", spread)
            self.assertIsNone(saved["page_id"])
            self.assertEqual(len(saved["observations"]), 2)
            self.assert_rows_held(saved, spread)
        self.assertEqual(self.page_count(), (5, 5))
        with self.history._connection() as connection:
            for identifier, body in connection.execute("SELECT id,body FROM pages"):
                self.assertEqual(hashlib.sha256(body).hexdigest(), identifier)

    def test_alternating_public_and_watch_page_sizes_reuse_retained_pages(self):
        rows = [transfer(index) for index in range(100, 0, -1)]
        public = dict(page(rows[:25], cursor="next"), pagination={"limit": 25, "next_cursor": "next"})
        quiet = dict(page([], account="bob.near"), pagination={"limit": 25, "next_cursor": None})
        for minute in range(4):
            stamp = "2026-11-10T00:0" + str(minute) + ":00Z"
            for account, payload in (("alice.near", public), ("alice.near", page(rows)), ("bob.near", quiet), ("bob.near", page([], account="bob.near"))):
                saved = self.history.record_page(account, "transfers", later(payload, stamp))
                self.assertIsNotNone(saved["page_id"])
        self.assertEqual(self.page_count(), (3, 3))

    def test_holdings_reads_reuse_only_a_retained_page_holding_exactly_the_same_tokens(self):
        def inventory(contracts, stamp, *, account="alice.near", offset=0, limit=20):
            items = [{"contract_id": contract, "balance": "1", "balance_status": "indexed", "last_update_block_height": 100,
                      "metadata": None, "metadata_source": None} for contract in contracts]
            return {"account_id": account, "network": "mainnet", "items": items,
                    "pagination": {"offset": offset, "limit": limit, "total": len(contracts), "next_offset": None},
                    "source": {"provider": "test-provider", "fetched_at": stamp}, "observed_at": stamp,
                    "coverage": {"scope": "Indexed FT inventory"}}

        def newest(account):
            with self.history._connection() as connection:
                body = connection.execute("SELECT body FROM pages WHERE account=? AND kind='holdings' ORDER BY created_ms DESC,rowid DESC LIMIT 1", (account,)).fetchone()[0]
            return [item["contract_id"] for item in json.loads(body)["items"]]

        both = self.history.record_page("alice.near", "holdings", inventory(["a.near", "b.near"], "2026-11-10T00:00:00Z"))
        self.assertEqual(self.history.record_page("alice.near", "holdings", inventory(["a.near", "b.near"], "2026-11-10T00:01:00Z"))["page_id"], both["page_id"])
        self.assertEqual(self.page_count(), (1, 1))
        steps = (["a.near"], ["a.near", "b.near"], [], ["a.near", "b.near"])
        for count, contracts in enumerate(steps, start=2):
            saved = self.history.record_page("alice.near", "holdings", inventory(contracts, "2026-11-10T00:0" + str(count) + ":00Z"))
            self.assertNotEqual(saved["page_id"], both["page_id"])
            self.assertEqual([row["item"]["contract_id"] for row in saved["rows"]], contracts)
            self.assertEqual(newest("alice.near"), contracts)
            self.assertEqual(self.page_count(), (count, count))
        single = self.history.record_page("bob.near", "holdings", inventory(["a.near"], "2026-11-10T00:00:00Z", account="bob.near"))
        grown = self.history.record_page("bob.near", "holdings", inventory(["a.near", "b.near"], "2026-11-10T00:01:00Z", account="bob.near"))
        self.assertNotEqual(grown["page_id"], single["page_id"])
        self.assertEqual(newest("bob.near"), ["a.near", "b.near"])
        self.assertEqual(self.page_count(), (7, 7))
        for minute in range(4):
            stamp = "2026-11-10T00:0" + str(minute) + ":00Z"
            for offset, contracts in ((0, ["a.near"]), (1, ["b.near"])):
                self.history.record_page("carol.near", "holdings", inventory(contracts, stamp, account="carol.near", offset=offset, limit=1))
        self.assertEqual(self.page_count(), (9, 9))
        window = inventory(["a.near", "b.near"], "2026-11-10T00:00:00Z", account="dave.near", limit=2)
        window["pagination"].update(total=3, next_offset=2)
        wide = self.history.record_page("dave.near", "holdings", window)
        window = inventory(["a.near", "b.near"], "2026-11-10T00:01:00Z", account="dave.near", limit=2)
        window["pagination"].update(total=3, next_offset=2)
        self.assertEqual(self.history.record_page("dave.near", "holdings", window)["page_id"], wide["page_id"])
        narrowed = self.history.record_page("dave.near", "holdings", inventory(["a.near", "b.near"], "2026-11-10T00:02:00Z", account="dave.near", limit=2))
        self.assertNotEqual(narrowed["page_id"], wide["page_id"])
        self.assertEqual(self.page_count(), (11, 11))

    def corrupt(self, table, body):
        with self.history._connection(write=True) as connection:
            connection.execute("UPDATE " + table + " SET body=?", (body,))

    def test_malformed_retained_rows_and_pages_are_never_reused_or_cited(self):
        original = page([transfer(1)])
        rewritten = dict(original, observed_at="2026-11-09T00:00:00Z", source=dict(original["source"], provider="rewritten.example"))
        for table, body in (("history_rows", b"null"), ("history_rows", b"[1]"), ("history_rows", b"\xff"),
                            ("history_rows", json.dumps(transfer(1)).encode()), ("history_rows", history._canonical(transfer(2))),
                            ("pages", b"null"), ("pages", b"{}"), ("pages", b'{"source":{},"items":[1]}'),
                            ("pages", b'{"source":{},"items":[{"id":7}]}'), ("pages", history._canonical(page([]))),
                            ("pages", history._canonical(rewritten))):
            with self.subTest(table=table, body=body):
                self.history.record_page("alice.near", "transfers", original)
                self.corrupt(table, body)
                with self.assertRaises(history.HistoryReadError):
                    self.history.list_records("alice.near", "transfers")
                fresh = later(original, "2026-11-10T00:01:00Z")
                saved = self.history.record_page("alice.near", "transfers", fresh)
                self.assertEqual(saved["page_id"], hashlib.sha256(history._canonical(fresh)).hexdigest())
                self.assert_rows_held(saved, fresh)
                self.assertEqual(self.history.record_page("alice.near", "transfers", later(original, "2026-11-10T00:02:00Z"))["page_id"], saved["page_id"])
                archived = self.history.list_records("alice.near", "transfers")
                self.assertEqual((archived["items"], archived["observations"]), (fresh["items"], saved["observations"]))
                with self.history._connection(write=True) as connection:
                    connection.execute("DELETE FROM history_rows")
                    connection.execute("DELETE FROM pages")
        with self.subTest(probe="a row cites a retained page that does not hold it"):
            first = self.history.record_page("alice.near", "transfers", page([transfer(1)]))
            second = self.history.record_page("alice.near", "transfers", page([transfer(2)]))
            with self.history._connection(write=True) as connection:
                connection.execute("UPDATE history_rows SET page_id=? WHERE page_id=?", (second["page_id"], first["page_id"]))
            with self.assertRaises(history.HistoryReadError):
                self.history.list_records("alice.near", "transfers")
            fresh = later(page([transfer(1), transfer(2)]), "2026-11-10T00:01:00Z")
            saved = self.history.record_page("alice.near", "transfers", fresh)
            self.assertEqual(saved["page_id"], hashlib.sha256(history._canonical(fresh)).hexdigest())
            self.assert_rows_held(saved, fresh)
            self.assertEqual(self.history.list_records("alice.near", "transfers")["observations"], saved["observations"])
        quiet = page([], account="bob.near")
        for body in (b"[]", history._canonical(dict(quiet, observed_at="2026-11-09T00:00:00Z"))):
            with self.subTest(account="bob.near", body=body):
                self.history.record_page("bob.near", "transfers", quiet)
                with self.history._connection(write=True) as connection:
                    connection.execute("UPDATE pages SET body=? WHERE account='bob.near'", (body,))
                fresh = later(quiet, "2026-11-10T00:01:00Z")
                saved = self.history.record_page("bob.near", "transfers", fresh)
                self.assertEqual(saved["page_id"], hashlib.sha256(history._canonical(fresh)).hexdigest())
                self.assertEqual(self.history.record_page("bob.near", "transfers", later(quiet, "2026-11-10T00:02:00Z"))["page_id"], saved["page_id"])
                with self.history._connection(write=True) as connection:
                    connection.execute("DELETE FROM pages WHERE account='bob.near'")
        with self.subTest(probe="an identical read cannot cite a tampered page under its own id"):
            same = page([transfer(1)], account="carol.near")
            self.history.record_page("carol.near", "transfers", same)
            with self.history._connection(write=True) as connection:
                connection.execute("UPDATE pages SET body=? WHERE account='carol.near'", (b"null",))
            with self.assertRaises(history.HistoryReadError):
                self.history.record_page("carol.near", "transfers", same)
            with self.assertRaises(history.HistoryReadError):
                self.history.list_records("carol.near", "transfers")
        with self.subTest(probe="a retained row whose timestamp column no longer matches its body"):
            self.history.record_page("dave.near", "transfers", page([transfer(1, subject="dave.near")], account="dave.near"))
            with self.history._connection(write=True) as connection:
                connection.execute("UPDATE history_rows SET stamp=? WHERE account='dave.near'", ("5".zfill(20),))
            with self.assertRaises(history.HistoryReadError):
                self.history.list_records("dave.near", "transfers")
            fresh = later(page([transfer(2, subject="dave.near"), transfer(1, subject="dave.near")], account="dave.near"), "2026-11-10T00:01:00Z")
            saved = self.history.record_page("dave.near", "transfers", fresh)
            self.assertEqual(saved["page_id"], hashlib.sha256(history._canonical(fresh)).hexdigest())
            again = later(page([transfer(2, subject="dave.near"), transfer(1, subject="dave.near")], account="dave.near"), "2026-11-10T00:02:00Z")
            self.assertEqual(self.history.record_page("dave.near", "transfers", again)["page_id"], saved["page_id"])
            self.assertEqual(self.history.list_records("dave.near", "transfers")["items"], fresh["items"])

    def test_a_sound_retained_transfer_whose_timestamp_changed_is_rejected_without_writing(self):
        original = page([transfer(1)])
        self.history.record_page("alice.near", "transfers", original)
        moved = later(page([transfer(2), transfer(1, timestamp="1000000000001")]), "2026-11-10T00:01:00Z")
        with self.assertRaises(history.HistoryInputError):
            self.history.record_page("alice.near", "transfers", moved)
        self.assertEqual(self.page_count(), (1, 1))
        self.assertEqual(self.history.list_records("alice.near", "transfers")["items"], original["items"])

    def test_one_page_naming_a_transfer_at_two_timestamps_is_rejected_without_writing(self):
        with self.assertRaises(history.HistoryInputError):
            self.history.record_page("alice.near", "transfers", page([transfer(1), transfer(1, timestamp="1000000000001")]))
        self.assertEqual(self.page_count(), (0, 0))

    def test_a_retained_row_whose_timestamp_disagrees_with_its_page_does_not_block_a_fresh_read(self):
        original = page([transfer(1)])
        self.history.record_page("alice.near", "transfers", original)
        with self.history._connection(write=True) as connection:
            connection.execute("UPDATE history_rows SET body=?,stamp=?", (history._canonical(transfer(1, timestamp="1000000000001")), "1000000000001".zfill(20)))
        with self.assertRaises(history.HistoryReadError):
            self.history.list_records("alice.near", "transfers")
        fresh = later(original, "2026-11-10T00:01:00Z")
        saved = self.history.record_page("alice.near", "transfers", fresh)
        self.assertEqual(saved["page_id"], hashlib.sha256(history._canonical(fresh)).hexdigest())
        self.assert_rows_held(saved, fresh)
        self.assertEqual(self.history.record_page("alice.near", "transfers", later(original, "2026-11-10T00:02:00Z"))["page_id"], saved["page_id"])
        archived = self.history.list_records("alice.near", "transfers")
        self.assertEqual((archived["items"], archived["observations"]), (fresh["items"], saved["observations"]))

    def test_a_damaged_pagination_anchor_is_refused_instead_of_skipping_rows(self):
        items = [transfer(index, timestamp="100000000000" + str(index)) for index in (3, 2, 1)]
        self.history.record_page("alice.near", "transfers", page(items))
        cursor = self.history.list_records("alice.near", "transfers", limit=1)["pagination"]["next_before"]
        self.assertEqual(self.history.list_records("alice.near", "transfers", before=cursor)["items"], items[1:])
        with self.history._connection(write=True) as connection:
            connection.execute("UPDATE history_rows SET body=?,stamp=? WHERE seq=?", (b"null", "0".zfill(20), cursor))
        with self.assertRaises(history.HistoryReadError):
            self.history.list_records("alice.near", "transfers", before=cursor)

    def test_a_retained_page_for_another_network_is_never_reused_or_cited(self):
        token = {"contract_id": "wrap.near", "balance": "1", "balance_status": "indexed", "last_update_block_height": 100,
                 "metadata": None, "metadata_source": None}
        for account, kind, original in (("alice.near", "transfers", page([transfer(1)])),
                                        ("bob.near", "transfers", page([], account="bob.near")),
                                        ("carol.near", "holdings", dict(page([], account="carol.near"), items=[token]))):
            with self.subTest(account=account, kind=kind):
                first = self.history.record_page(account, kind, original)
                body = history._canonical(dict(original, network="testnet"))
                identifier = hashlib.sha256(body).hexdigest()
                with self.history._connection(write=True) as connection:
                    connection.execute("INSERT INTO pages VALUES (?,?,?,?,?)", (identifier, account, kind, body, 0))
                    connection.execute("UPDATE history_rows SET page_id=? WHERE page_id=?", (identifier, first["page_id"]))
                    connection.execute("DELETE FROM pages WHERE id=?", (first["page_id"],))
                if original["items"]:
                    with self.assertRaises(history.HistoryReadError):
                        self.history.list_records(account, kind)
                fresh = later(original, "2026-11-10T00:01:00Z") if kind == "transfers" else dict(original, observed_at="2026-11-10T00:01:00Z")
                saved = self.history.record_page(account, kind, fresh)
                self.assertEqual(saved["page_id"], hashlib.sha256(history._canonical(fresh)).hexdigest())
                self.assertEqual(saved["observations"], [{"page_id": saved["page_id"], "source": fresh["source"], "observed_at": "2026-11-10T00:01:00Z"}])
                if original["items"]:
                    self.assertEqual(self.history.list_records(account, kind)["observations"], saved["observations"])

    def test_a_retained_page_whose_bytes_no_longer_match_its_id_is_never_reused_or_cited(self):

        token = {"contract_id": "wrap.near", "balance": "1", "balance_status": "indexed", "last_update_block_height": 100,
                 "metadata": None, "metadata_source": None}
        for account, kind, original in (("alice.near", "transfers", page([transfer(1)])),
                                        ("bob.near", "transfers", page([], account="bob.near")),
                                        ("carol.near", "holdings", dict(page([], account="carol.near"), items=[token]))):
            with self.subTest(account=account, kind=kind):
                first = self.history.record_page(account, kind, original)
                rewritten = dict(original, observed_at="2026-11-09T00:00:00Z", source=dict(original["source"], fetched_at="2026-11-09T00:00:00Z"))
                with self.history._connection(write=True) as connection:
                    connection.execute("UPDATE pages SET body=? WHERE id=?", (history._canonical(rewritten), first["page_id"]))
                if original["items"]:
                    with self.assertRaises(history.HistoryReadError):
                        self.history.list_records(account, kind)
                fresh = dict(original, observed_at="2026-11-10T00:01:00Z")
                saved = self.history.record_page(account, kind, fresh)
                self.assertNotEqual(saved["page_id"], first["page_id"])
                self.assertEqual(saved["page_id"], hashlib.sha256(history._canonical(fresh)).hexdigest())
                self.assertEqual(saved["observations"], [{"page_id": saved["page_id"], "source": original["source"], "observed_at": "2026-11-10T00:01:00Z"}])
                self.assertEqual(self.history.record_page(account, kind, dict(original, observed_at="2026-11-10T00:02:00Z"))["page_id"], saved["page_id"])
                if original["items"]:
                    self.assertEqual(self.history.list_records(account, kind)["observations"], saved["observations"])

    def test_watch_service_records_a_tampered_retained_page_and_keeps_running(self):
        store, _, _ = self.workspace()
        reader = Reader()
        reader.pages["alice.near", None] = page([transfer(1)])
        archive_path = self.root / "service.sqlite3"
        history.AccountHistory(archive_path).record_page("alice.near", "transfers", page([transfer(1)]))
        with closing(sqlite3.connect(archive_path)) as connection, connection:
            connection.execute("UPDATE pages SET body=?", (b"null",))
        waits = []

        class Stop:
            def wait(inner, interval):
                waits.append(history.read_status(archive_path))
                return len(waits) == 2

        status = history.run(store, reader, archive_path, stop_event=Stop())
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(len(waits), 2)
        self.assertEqual(waits[0]["last_error"], "HistoryReadError")
        self.assertEqual(status["retry_pending"], 1)
        with closing(sqlite3.connect(archive_path)) as connection:
            self.assertEqual(connection.execute("SELECT attempts,last_error,lease_token,lease_until_ms FROM watch_poll").fetchone(), (1, "HistoryReadError", None, 0))


class HistoryCooldownTest(unittest.TestCase):
    def setUp(self):
        for name in ("urllib.request.urlopen", "gate.rpc._open_with_deadline", "gate.rpc._fetch"):
            patcher = mock.patch(name, side_effect=AssertionError("network forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = mock.patch.dict("os.environ", {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 2000.0
        self.history = history.AccountHistory(self.root / "history.sqlite3", clock=lambda: self.now)
        self.reader = accounts.AccountData(clock=lambda: self.now)
        self.store = mock.Mock()
        self.store.watch_schedule.return_value = [{"account": "alice.near", "created_ms": 1, "reserved": 0}]

    def test_backfill_budget_refusal_leaves_provider_cooldowns_unchanged(self):
        self.history.defer_account("other.near", "transfers", self.history._now() + 300000)
        with self.history._connection() as connection:
            before = [tuple(row) for row in connection.execute("SELECT * FROM provider_cooldowns")]
        for kind in ("transfers", "transactions"):
            self.now += 61
            with self.subTest(kind=kind), mock.patch.object(rpc.Client, "post", side_effect=rpc.BudgetExhausted("Provider budget spent.", 3600)):
                with self.assertRaises(accounts.AccountBudgetError) as caught:
                    self.history.backfill_account(self.reader, "alice.near", kind)
                self.assertEqual(caught.exception.retry_at_ms, self.history._now() + 3600000)
                with self.history._connection() as connection:
                    self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM provider_cooldowns")], before)

    def test_watch_budget_refusal_keeps_its_retry_without_a_provider_cooldown(self):
        self.history.defer_account("other.near", "transfers", self.history._now() + 300000)
        with self.history._connection() as connection:
            before = [tuple(row) for row in connection.execute("SELECT * FROM provider_cooldowns")]
        retry_at = self.history._now() + 3600000
        with mock.patch.object(rpc.Client, "post", autospec=True, side_effect=rpc.BudgetExhausted("Provider budget spent.", 3600)) as fetch:
            self.history.poll_watches(self.store, self.reader)
            with self.history._connection() as connection:
                self.assertEqual(tuple(connection.execute("SELECT initialized,next_due_ms,last_success_ms FROM watch_poll").fetchone()), (0, retry_at, None))
                self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM provider_cooldowns")], before)
            self.now += 3599
            self.history.poll_watches(self.store, self.reader)
            self.assertEqual(fetch.call_count, 1)
            self.now += 1
            def completed(client, *args, **kwargs):
                client.last_source = page([])["source"]
                return {"transfers": [], "resume_token": None}

            fetch.side_effect = completed
            self.history.poll_watches(self.store, self.reader)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(self.history.status()["initialized_accounts"], 1)

    def test_backfill_provider_retry_after_still_writes_a_cooldown(self):
        for kind in ("transfers", "transactions"):
            with self.subTest(kind=kind), mock.patch.object(rpc.Client, "post", side_effect=rpc.HttpError(429, "https://source.invalid", retry_after="3600")) as fetch:
                retry_at = self.history._now() + 3600000
                with self.assertRaises(AccountReadError) as caught:
                    self.history.backfill_account(self.reader, "alice.near", kind)
                self.assertEqual((caught.exception.status, caught.exception.retry_at_ms), (429, retry_at))
                with self.history._connection() as connection:
                    self.assertEqual(connection.execute("SELECT not_before_ms FROM provider_cooldowns WHERE account=? AND kind=?", ("alice.near", kind)).fetchone()[0], retry_at)
                self.now += 61
                with self.assertRaises(history.HistoryCapacityError) as cooldown:
                    self.history.backfill_account(self.reader, "alice.near", kind)
                self.assertEqual(cooldown.exception.retry_at_ms, retry_at)
                self.assertEqual(fetch.call_count, 1)

    def test_watch_provider_retry_after_still_writes_a_cooldown(self):
        retry_at = self.history._now() + 3600000
        with mock.patch.object(rpc.Client, "post", side_effect=rpc.HttpError(429, "https://source.invalid", retry_after="3600")) as fetch:
            self.history.poll_watches(self.store, self.reader)
            with self.history._connection() as connection:
                self.assertEqual(tuple(connection.execute("SELECT account,kind,not_before_ms FROM provider_cooldowns").fetchone()), ("alice.near", "transfers", retry_at))
                self.assertEqual(tuple(connection.execute("SELECT initialized,next_due_ms,last_success_ms,last_error FROM watch_poll").fetchone()), (0, retry_at, None, "provider_http_429"))
            self.now += 61
            self.history.poll_watches(self.store, self.reader)
            self.assertEqual(fetch.call_count, 1)
            with self.assertRaises(history.HistoryCapacityError) as cooldown:
                self.history.claim_job_read("alice.near")
            self.assertEqual(cooldown.exception.retry_at_ms, retry_at)

    def test_spent_public_backfill_budget_does_not_block_a_team_job_or_watch(self):
        database = self.root / "budget.sqlite3"
        jobs = investigation_jobs.InvestigationJobs(self.root / "jobs.sqlite3", self.history, clock=lambda: self.now)
        client = rpc.Client(accounts.TRANSFERS_URL, "off", kind="http", clock=lambda: self.now,
                            headers={"Authorization": "Bearer unit-only-token"})
        self.reader.client_factory = mock.Mock(return_value=client)

        def response(client, *args, **kwargs):
            client._metered("transfers.main.fastnear.com")
            client.last_source = page([])["source"]
            return {"transfers": [], "resume_token": None}

        with budget.use_database(database), mock.patch.object(rpc.Client, "post", autospec=True, side_effect=response) as fetch:
            budget.reserve("other", "operator", keyed=False, now=self.now)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("INSERT INTO counters VALUES(?,?,?,NULL)", ("keyed:backfill:fastnear_index", int(self.now // budget.DAY_S), 500))
            with rpc.caller_class("backfill"), self.assertRaises(accounts.AccountBudgetError) as caught:
                self.history.backfill_account(self.reader, "alice.near", "transfers")
            retry_at = caught.exception.retry_at_ms
            self.now += 61
            self.assertLess(self.history._now(), retry_at)
            job = jobs.create("team", "actor", "alice.near")
            with rpc.caller_class("investigation"):
                self.assertEqual(jobs.tick(self.reader), {"processed": 1})
            current = jobs.get("team", job["id"])
            self.assertEqual((current["state"], current["counts"]["pages"]), ("completed", 1))
            with rpc.caller_class("watch"):
                self.history.poll_watches(self.store, self.reader)
            self.assertEqual(self.history.status()["initialized_accounts"], 1)
            self.assertEqual(fetch.call_count, 3)
            with self.history._connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM provider_cooldowns").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
