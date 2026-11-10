import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from intel import history
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
        self.now += 5
        store.add_watch(user, team, "bob.near")
        self.now += 1
        stamp = str(int(self.now * 1000000000))
        reader.pages["bob.near", None] = page([
            transfer(2, subject="bob.near", other="alice.near", outgoing=False, timestamp=stamp)
        ], account="bob.near")
        self.history.poll_watches(store, reader)
        self.assertEqual(store.list_inbox(user, team)["items"], [])
        self.now += 60
        reader.pages["alice.near", None] = page([transfer(2, timestamp=stamp)])
        self.history.poll_watches(store, reader)
        self.assertEqual(len(store.list_inbox(user, team)["items"]), 1)
        self.assertEqual(len(store.events_after()["items"]), 1)
        self.now += 5
        self.history.poll_watches(store, reader)
        self.assertEqual(len(store.list_inbox(user, team)["items"]), 1)

    def test_mirrored_multiwatch_transfer_creates_one_team_inbox_event(self):
        store, user, team = self.workspace()
        store.add_watch(user, team, "bob.near")
        reader = Reader()
        reader.pages["alice.near", None] = page([])
        reader.pages["bob.near", None] = page([], account="bob.near")
        self.history.poll_watches(store, reader)
        self.now += 5
        self.history.poll_watches(store, reader)
        self.now += 61
        stamp = str(int(self.now * 1000000000))
        reader.pages["alice.near", None] = page([transfer(2, timestamp=stamp)])
        reader.pages["bob.near", None] = page([transfer(2, subject="bob.near", other="alice.near", outgoing=False, timestamp=stamp)], account="bob.near")
        self.history.poll_watches(store, reader)
        self.now += 5
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
        self.now += 5
        self.history.poll_watches(store, reader)
        self.assertEqual(reader.calls[-1][0], "bob.near")
        self.assertEqual(self.history.status()["initialized_accounts"], 1)

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

            def watched_accounts(self):
                return store.watched_accounts()

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

    def test_new_watches_do_not_jump_ahead_of_an_overdue_existing_account(self):
        store, user, team = self.workspace()
        reader = Reader()
        reader.pages["alice.near", None] = page([])
        reader.pages["bob.near", None] = page([], account="bob.near")
        self.history.poll_watches(store, reader)
        self.now += 70
        store.add_watch(user, team, "bob.near")
        self.history.poll_watches(store, reader)
        self.assertEqual(reader.calls[-1][0], "alice.near")

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

    def test_two_page_watch_cap_sets_a_persistent_coverage_gap_without_claiming_complete_history(self):
        store, _, _ = self.workspace()
        reader = Reader()
        reader.pages["alice.near", None] = page([transfer(1)])
        self.history.poll_watches(store, reader)
        self.now += 61
        reader.pages["alice.near", None] = page([transfer(2)], cursor="next")
        reader.pages["alice.near", "next"] = page([transfer(3)], cursor="older")
        result = self.history.poll_watches(store, reader)
        self.assertEqual(reader.calls[-2:], [("alice.near", None, 100), ("alice.near", "next", 100)])
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
        for number in range(6):
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
                self.history.record_page("alice.near", "transfers", dict(original, observed_at="later"))
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


if __name__ == "__main__":
    unittest.main()
