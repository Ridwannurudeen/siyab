import hashlib
import importlib.util
import io
import itertools
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from email.message import Message
from pathlib import Path
from unittest import mock

from intel import history, workspace
from intel.workspace import SQLiteWorkspaceStore
from intel.workspace_api import PREFIX, WorkspaceAPI
from tests.test_history import Reader, page, transfer

ROOT = Path(__file__).resolve().parent.parent
BUILD_COMMIT = "53288c98e252eb11a605387c4215bebe1c867b97"


class Failing:
    def __init__(self, store):
        self.store, self.fail = store, True

    def watched_accounts(self):
        return self.store.watched_accounts()

    def ingest_event(self, *args, **kwargs):
        if self.fail:
            raise OSError("delivery unavailable")
        return self.store.ingest_event(*args, **kwargs)


class WatchCapacityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 1000.0
        self.history = history.AccountHistory(self.root / "history.sqlite3", clock=lambda: self.now)
        self.store = SQLiteWorkspaceStore(self.root / "workspace.sqlite3", clock=lambda: self.now)
        self.reader = Reader()

    def stamp(self):
        return str(int(self.now * 1000) * 1000000)

    def team(self, username, account, store=None):
        store = store or self.store
        user = store.register(username, username + "-password-123")["user"]["id"]
        team = store.list_teams(user)[0]["id"]
        store.add_watch(user, team, account)
        return user, team

    def poll(self, account, items, *, archive=None, store=None, cursor=None):
        archive, store = archive or self.history, store or self.store
        self.reader.pages[account, None] = page(items, account=account, cursor=cursor)
        for _ in range(60):
            self.now += 5
            before = len(self.reader.calls)
            archive.poll_watches(store, self.reader)
            if len(self.reader.calls) > before and self.reader.calls[before][0] == account:
                return
        self.fail("the watch for " + account + " was never polled")

    def outbox(self, archive=None):
        with (archive or self.history)._connection() as connection:
            return {row["id"]: row["delivered_seq"] for row in connection.execute("SELECT id,delivered_seq FROM event_outbox")}

    def seen(self, account, archive=None):
        with (archive or self.history)._connection() as connection:
            return [row[0] for row in connection.execute("SELECT row_id FROM watch_seen WHERE account=? ORDER BY rowid", (account,))]

    def watch_error(self, account):
        with self.history._connection() as connection:
            return connection.execute("SELECT last_error FROM watch_poll WHERE account=?", (account,)).fetchone()[0]

    def assertUsageMatchesRows(self, archive=None):
        with (archive or self.history)._connection() as connection:
            usage = connection.execute("SELECT pages,records,graphs,events,seen,bytes FROM archive_usage WHERE singleton=1").fetchone()
            rows = connection.execute(
                "SELECT (SELECT COUNT(*) FROM pages),(SELECT COUNT(*) FROM history_rows),(SELECT COUNT(*) FROM graphs),"
                "(SELECT COUNT(*) FROM event_outbox),(SELECT COUNT(*) FROM watch_seen),"
                "(SELECT COALESCE(SUM(LENGTH(body)),0) FROM pages)+(SELECT COALESCE(SUM(LENGTH(body)),0) FROM history_rows)+"
                "(SELECT COALESCE(SUM(LENGTH(body)),0) FROM graphs)+(SELECT COALESCE(SUM(LENGTH(body)),0) FROM event_outbox)").fetchone()
        self.assertEqual(tuple(usage), tuple(rows))

    def request(self, api, path, *, method="GET", body=None, cookie=None):
        headers = Message()
        headers["Origin"] = "https://siyab.xyz"
        headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        return api.handle(method, PREFIX + path, "203.0.113.1", headers, json.dumps(body or {}).encode())

    def api_team(self, api, account):
        status, body, headers = self.request(api, "/auth/register", method="POST", body={"username": "alice", "password": "test-password-123"})
        self.assertEqual(status, 201, body)
        team = self.store.list_teams(body["user"]["id"])[0]["id"]
        self.store.add_watch(body["user"]["id"], team, account)
        return body["user"]["id"], team, headers["Set-Cookie"].split(";", 1)[0]

    def test_a_full_event_budget_of_delivered_events_reopens_for_another_teams_watch(self):
        alice, alice_team = self.team("alice", "alice.near")
        carol, carol_team = self.team("carol", "carol.near")
        self.poll("alice.near", [])
        self.poll("carol.near", [])
        items = []
        for number in range(1, 4):
            items.insert(0, transfer(number, timestamp=self.stamp()))
            self.poll("alice.near", items)
        held = [item["event_id"] for item in self.store.list_inbox(alice, alice_team)["items"]]
        self.assertEqual(len(held), 3)
        with mock.patch.object(history, "MAX_ARCHIVE_EVENTS", 3):
            seen = transfer(9, subject="carol.near", timestamp=self.stamp())
            self.poll("carol.near", [seen])
            self.assertEqual(len(self.store.list_inbox(carol, carol_team)["items"]), 1)
            self.assertIsNone(self.history.status()["last_error"])
            outbox = self.outbox()
            self.assertEqual(len(outbox), 3)
            self.assertNotIn(held[0], outbox)
            self.assertTrue(all(sequence is not None for sequence in outbox.values()))
            self.assertUsageMatchesRows()
            self.poll("carol.near", [transfer(10, subject="carol.near", timestamp=self.stamp()), seen])
            self.assertEqual(len(self.store.list_inbox(carol, carol_team)["items"]), 2)
            self.assertNotIn(held[1], self.outbox())
        self.assertEqual(len(self.store.list_inbox(alice, alice_team)["items"]), 3)
        self.assertUsageMatchesRows()

    def test_undelivered_events_are_never_removed_and_a_budget_of_only_undelivered_events_still_refuses(self):
        alice, team = self.team("alice", "alice.near")
        inbox = Failing(self.store)
        self.poll("alice.near", [])
        items = [transfer(1, timestamp=self.stamp())]
        inbox.fail = False
        self.poll("alice.near", items, store=inbox)
        delivered = self.store.list_inbox(alice, team)["items"][0]["event_id"]
        inbox.fail = True
        for number in (2, 3):
            items.insert(0, transfer(number, timestamp=self.stamp()))
            self.poll("alice.near", items, store=inbox)
        with mock.patch.object(history, "MAX_ARCHIVE_EVENTS", 3):
            items.insert(0, transfer(4, timestamp=self.stamp()))
            self.poll("alice.near", items, store=inbox)
            waiting = self.outbox()
            self.assertEqual(len(waiting), 3)
            self.assertNotIn(delivered, waiting)
            self.assertEqual(set(waiting.values()), {None})
            items.insert(0, transfer(5, timestamp=self.stamp()))
            self.poll("alice.near", items, store=inbox)
            self.assertEqual(self.outbox(), waiting)
            self.assertEqual(self.watch_error("alice.near"), "HistoryCapacityError")
            self.assertNotIn(items[0]["id"], self.seen("alice.near"))
            self.assertUsageMatchesRows()
            inbox.fail = False
            self.poll("alice.near", items, store=inbox)
            self.assertEqual(len(self.store.list_inbox(alice, team)["items"]), 5)
            self.assertEqual(len(self.outbox()), 3)
            self.assertIsNone(self.watch_error("alice.near"))
        self.assertUsageMatchesRows()

    def test_an_inbox_link_to_a_pruned_event_answers_the_same_bytes_and_never_other_bytes(self):
        api = WorkspaceAPI(self.store, history_store=self.history)
        user, team, cookie = self.api_team(api, "alice.near")
        self.poll("alice.near", [])
        stamp = self.stamp()
        native = transfer(1, timestamp=stamp)
        token = dict(transfer(2, timestamp=stamp), asset_id="nep141:token.near")
        no_sender = dict(transfer(3, outgoing=False, timestamp=stamp), **{"from": None})
        no_receiver = dict(transfer(4, timestamp=stamp), to=None)
        no_hash = dict(transfer(5, timestamp=stamp), transaction_hash=None)
        text = dict(transfer(6, timestamp=stamp), asset_id="nep141:café \"q\" \\   \x01 \U0001f600.near")
        self.poll("alice.near", [native, token, no_sender, no_receiver, no_hash, text])
        links = {}
        for item in self.store.list_inbox(user, team)["items"]:
            stored = self.history.get_event_bytes(item["event_id"])
            self.assertEqual(history._canonical(item["event"]), stored)
            status, served, _ = self.request(api, f"/teams/{team}/events/{item['event_id']}", cookie=cookie)
            self.assertEqual((status, served), (200, stored))
            links[item["event_id"]] = served
        self.assertEqual(len(links), 6)
        with mock.patch.object(history, "MAX_ARCHIVE_EVENTS", 1):
            self.poll("alice.near", [transfer(7, timestamp=self.stamp())])
        for identifier, body in links.items():
            with self.subTest(identifier=identifier):
                self.assertIsNone(self.history.get_event_bytes(identifier))
                status, served, _ = self.request(api, f"/teams/{team}/events/{identifier}", cookie=cookie)
                self.assertEqual((status, served), (200, body))
                self.assertEqual(hashlib.sha256(served).hexdigest(), identifier)
        unknown = self.request(api, f"/teams/{team}/events/{'b' * 64}", cookie=cookie)
        self.assertEqual(unknown[0], 404)
        changed = next(iter(links))
        with self.store._connection(write=True) as connection:
            connection.execute("UPDATE events SET body_json=REPLACE(body_json,'\"amount\":\"','\"amount\":\"9') WHERE event_id=?", (changed,))
        self.assertEqual(self.request(api, f"/teams/{team}/events/{changed}", cookie=cookie), unknown)
        feed_event = {"kind": "feed_event", "signal": "watch_account_key_added", "subject": "alice.near",
                      "detail": "A key was added.", "observed": {"block_height": 12, "timestamp_ms": int(self.now * 1000)}}
        feed_id = hashlib.sha256(json.dumps(feed_event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.store.ingest_event(feed_id, feed_event, notify=True)
        self.assertTrue(self.store.team_event(user, team, feed_id))
        self.assertEqual(self.request(api, f"/teams/{team}/events/{feed_id}", cookie=cookie), unknown)

    def test_a_full_dedup_budget_from_one_account_no_longer_blocks_a_second_account(self):
        alice, alice_team = self.team("alice", "alice.near")
        carol, carol_team = self.team("carol", "carol.near")
        self.poll("alice.near", [])
        self.poll("carol.near", [])
        with mock.patch.object(history, "MAX_WATCH_SEEN", 3):
            self.poll("alice.near", [transfer(number, timestamp=self.stamp()) for number in (3, 2, 1)])
            self.assertEqual(len(self.seen("alice.near")), 3)
            self.poll("carol.near", [transfer(9, subject="carol.near", timestamp=self.stamp())])
            self.assertEqual(len(self.store.list_inbox(carol, carol_team)["items"]), 1)
            self.assertEqual(self.seen("alice.near"), [transfer(2)["id"], transfer(1)["id"]])
            self.assertUsageMatchesRows()
        self.assertEqual(len(self.store.list_inbox(alice, alice_team)["items"]), 3)

    def test_each_account_keeps_at_most_its_own_dedup_share_oldest_rows_first(self):
        self.assertEqual(history.MAX_ACCOUNT_WATCH_SEEN, 1000)
        alice, team = self.team("alice", "alice.near")
        self.poll("alice.near", [])
        items = []
        with mock.patch.object(history, "MAX_ACCOUNT_WATCH_SEEN", 3):
            for number in range(1, 6):
                items.insert(0, transfer(number, timestamp=self.stamp()))
                self.poll("alice.near", items[:3])
            self.assertEqual(self.seen("alice.near"), [transfer(number)["id"] for number in (3, 4, 5)])
            self.assertUsageMatchesRows()
        self.assertEqual(len(self.store.list_inbox(alice, team)["items"]), 5)

    def test_a_pruned_dedup_row_does_not_realert_or_add_a_second_inbox_row(self):
        alice, team = self.team("alice", "alice.near")
        self.poll("alice.near", [])
        first = transfer(1, timestamp=self.stamp())
        self.poll("alice.near", [first])
        original = self.store.list_inbox(alice, team)["items"][0]
        body = self.history.get_event_bytes(original["event_id"])
        with mock.patch.object(history, "MAX_ACCOUNT_WATCH_SEEN", 1), mock.patch.object(history, "MAX_ARCHIVE_EVENTS", 1):
            self.poll("alice.near", [transfer(2, timestamp=self.stamp())])
            self.assertNotIn(first["id"], self.seen("alice.near"))
            self.assertIsNone(self.history.get_event_bytes(original["event_id"]))
            self.poll("alice.near", [first])
            self.assertEqual(self.seen("alice.near"), [first["id"]])
            self.assertEqual(self.outbox(), {original["event_id"]: original["event_seq"]})
            self.assertEqual(self.history.get_event_bytes(original["event_id"]), body)
            self.assertUsageMatchesRows()
        inbox = self.store.list_inbox(alice, team)["items"]
        self.assertEqual(len(inbox), 2)
        self.assertEqual([item["event_id"] for item in inbox].count(original["event_id"]), 1)
        self.assertEqual(len(self.store.events_after()["items"]), 2)

    def test_a_quiet_account_pruned_at_the_global_cap_records_its_rows_again_without_a_duplicate(self):
        alice, alice_team = self.team("alice", "alice.near")
        carol, carol_team = self.team("carol", "carol.near")
        before = transfer(1, timestamp=str(int(self.now * 1000 - 1000) * 1000000))
        self.now += 1
        after = transfer(2, timestamp=self.stamp())
        self.poll("alice.near", [after, before])
        self.poll("carol.near", [])
        self.assertEqual(self.store.list_inbox(alice, alice_team)["items"], [])
        with mock.patch.object(history, "MAX_WATCH_SEEN", 2):
            self.poll("carol.near", [transfer(9, subject="carol.near", timestamp=self.stamp())])
            self.assertEqual(self.seen("alice.near"), [before["id"]])
            self.poll("alice.near", [after, before])
            self.poll("alice.near", [after, before])
            self.assertUsageMatchesRows()
        alerts = self.store.list_inbox(alice, alice_team)["items"]
        self.assertEqual([item["event"]["receipt_id"] for item in alerts], [after["receipt_id"]])
        self.assertEqual(len(self.store.list_inbox(carol, carol_team)["items"]), 1)

    def carol_ready(self):
        alice, alice_team = self.team("alice", "alice.near")
        carol, carol_team = self.team("carol", "carol.near")
        self.poll("alice.near", [])
        self.poll("carol.near", [])
        items = []
        for number in range(1, 4):
            items.insert(0, transfer(number, timestamp=self.stamp()))
            self.poll("alice.near", items)
        held = [item["event_id"] for item in self.store.list_inbox(alice, alice_team)["items"]]
        self.assertEqual(len(held), 3)
        seen = transfer(9, subject="carol.near", timestamp=self.stamp())
        self.history.record_page("carol.near", "transfers", page([seen], account="carol.near"), reserved=True)
        with self.history._connection() as connection:
            used = connection.execute("SELECT bytes FROM archive_usage WHERE singleton=1").fetchone()[0]
            delivered = connection.execute("SELECT SUM(LENGTH(body)) FROM event_outbox WHERE delivered_seq IS NOT NULL").fetchone()[0]
        return (carol, carol_team), held, seen, used, delivered

    def test_a_byte_budget_full_of_delivered_events_reopens_for_another_teams_watch(self):
        (carol, carol_team), held, seen, used, _ = self.carol_ready()
        with mock.patch.object(history, "MAX_ARCHIVE_BYTES", used + 100), mock.patch.object(history, "EVENT_ARCHIVE_BYTES", 0):
            self.poll("carol.near", [seen])
            self.assertIsNone(self.watch_error("carol.near"))
            self.assertEqual(len(self.store.list_inbox(carol, carol_team)["items"]), 1)
            outbox = self.outbox()
            self.assertEqual(len(outbox), 3)
            self.assertNotIn(held[0], outbox)
            self.assertTrue(all(sequence is not None for sequence in outbox.values()))
            self.assertUsageMatchesRows()
            with self.history._connection() as connection:
                self.assertLessEqual(connection.execute("SELECT bytes FROM archive_usage WHERE singleton=1").fetchone()[0], used + 100)

    def test_a_byte_shortfall_that_delivered_events_cannot_cover_still_refuses_and_removes_nothing(self):
        (carol, carol_team), _, seen, used, delivered = self.carol_ready()
        before = self.outbox()
        opener = history.AccountHistory._connection
        statements = []

        @contextmanager
        def traced(archive, *, write=False, transaction=True):
            with opener(archive, write=write, transaction=transaction) as opened:
                opened.set_trace_callback(statements.append)
                yield opened

        with mock.patch.object(history, "MAX_ARCHIVE_BYTES", used - delivered), mock.patch.object(history, "EVENT_ARCHIVE_BYTES", 0), \
                mock.patch.object(history.AccountHistory, "_connection", traced):
            self.poll("carol.near", [seen])
            self.assertEqual(self.watch_error("carol.near"), "HistoryCapacityError")
            self.assertTrue(any(statement.startswith("SELECT id,LENGTH(body)") for statement in statements))
            self.assertEqual([statement for statement in statements if statement.startswith("DELETE FROM event_outbox")], [])
            self.assertEqual(self.outbox(), before)
            self.assertEqual(self.store.list_inbox(carol, carol_team)["items"], [])
            self.assertNotIn(seen["id"], self.seen("carol.near"))
            self.assertUsageMatchesRows()

    def test_the_workspace_transfer_identity_changes_exactly_with_the_six_dedup_fields(self):
        def identity(item):
            return workspace._transfer_identity(json.loads(history.AccountHistory._event(item)[1]))

        base = transfer(1)
        for item in (dict(base, transaction_hash=None), dict(base, transaction_hash="txfilled"), dict(base, block_height="99"),
                     dict(base, block_timestamp="2000000000000")):
            self.assertEqual(identity(item), identity(base))
        different = [base, dict(base, receipt_id="receipt9"), dict(base, transfer_index=9), dict(base, asset_id="nep141:token.near"),
                     dict(base, amount="2"), dict(base, **{"from": "carol.near"}), dict(base, to="carol.near"), dict(base, to=None),
                     dict(base, **{"from": None, "to": "alice.near"}), dict(base, **{"from": "bob.near", "to": "alice.near"})]
        self.assertEqual(len({identity(item) for item in different}), len(different))
        self.assertIsNone(workspace._transfer_identity({"kind": "feed_event", "signal": "watch_account_key_added", "detail": "A key was added."}))

    def test_a_transfer_recorded_again_with_a_changed_hash_or_block_after_a_dedup_prune_adds_no_second_inbox_row(self):
        alice, alice_team = self.team("alice", "alice.near")
        carol, carol_team = self.team("carol", "carol.near")
        self.poll("alice.near", [])
        self.poll("carol.near", [])
        first = dict(transfer(1, timestamp=self.stamp()), transaction_hash=None)
        self.poll("alice.near", [first])
        original = self.store.list_inbox(alice, alice_team)["items"]
        self.assertEqual(len(original), 1)
        with mock.patch.object(history, "MAX_WATCH_SEEN", 1):
            self.poll("carol.near", [transfer(9, subject="carol.near", timestamp=self.stamp())])
            self.assertEqual(self.seen("alice.near"), [])
            self.poll("alice.near", [dict(first, transaction_hash="txfilled")])
            self.assertEqual(self.seen("alice.near"), [first["id"]])
        self.assertIsNone(self.watch_error("alice.near"))
        self.assertEqual(self.store.list_inbox(alice, alice_team)["items"], original)
        second = transfer(2, timestamp=self.stamp())
        self.poll("alice.near", [second, first])
        third = transfer(3, timestamp=self.stamp())
        with mock.patch.object(history, "MAX_ACCOUNT_WATCH_SEEN", 1):
            self.poll("alice.near", [third])
            self.assertEqual(self.seen("alice.near"), [third["id"]])
            self.poll("alice.near", [dict(second, block_height="13"), dict(first, transaction_hash="txfilled")])
        alerts = self.store.list_inbox(alice, alice_team)["items"]
        self.assertEqual(sorted(item["event"]["receipt_id"] for item in alerts), ["receipt1", "receipt2", "receipt3"])
        self.assertEqual(len(self.store.events_after()["items"]), 4)
        identity = {key: second[key] for key in ("receipt_id", "transfer_index", "asset_id", "from", "to", "amount")}
        identity["transfer_index"] = 7
        sibling = dict(second, transfer_index=7, id=hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        self.poll("alice.near", [sibling, third])
        alerts = self.store.list_inbox(alice, alice_team)["items"]
        self.assertEqual(sorted((item["event"]["receipt_id"], item["event"]["transfer_index"]) for item in alerts),
                         [("receipt1", 1), ("receipt2", 2), ("receipt2", 7), ("receipt3", 3)])
        self.assertUsageMatchesRows()

    def test_an_account_already_over_its_dedup_share_is_pruned_when_a_poll_returns_only_known_transfers(self):
        alice, team = self.team("alice", "alice.near")
        self.poll("alice.near", [])
        items = [transfer(number) for number in range(1, history.MAX_ACCOUNT_WATCH_SEEN + 4)]
        with self.history._connection(write=True) as connection:
            connection.executemany("INSERT INTO watch_seen VALUES (?,?)", [("alice.near", item["id"]) for item in items])
            connection.execute("UPDATE archive_usage SET seen=seen+? WHERE singleton=1", (len(items),))
        self.assertUsageMatchesRows()
        for _ in range(2):
            self.poll("alice.near", [items[-1], items[-2]])
            self.assertEqual(self.seen("alice.near"), [item["id"] for item in items[3:]])
            self.assertEqual(len(self.seen("alice.near")), 1000)
            self.assertIsNone(self.watch_error("alice.near"))
            self.assertUsageMatchesRows()
        self.assertEqual(self.store.list_inbox(alice, team)["items"], [])

    def test_a_changed_timestamp_on_an_archived_transfer_keeps_its_evidence_and_still_alerts_a_new_transfer_once(self):
        alice, team = self.team("alice", "alice.near")
        self.poll("alice.near", [])
        first = transfer(1, timestamp=self.stamp())
        self.poll("alice.near", [first])
        archived = self.archived()
        original = self.store.list_inbox(alice, team)["items"]
        self.assertEqual(len(original), 1)
        second = transfer(2, timestamp=self.stamp())
        moved = dict(first, block_timestamp=str(int(first["block_timestamp"]) + 1))
        for _ in range(3):
            self.poll("alice.near", [second, moved])
            self.assertEqual(self.archived(), archived)
            self.assertIsNone(self.watch_error("alice.near"))
            self.assertUsageMatchesRows()
        with self.history._connection() as connection:
            self.assertEqual(connection.execute("SELECT coverage_gap FROM watch_poll WHERE account='alice.near'").fetchone()[0], 1)
        status = self.history.status()
        self.assertEqual((status["coverage_gap_accounts"], status["last_error"]), (1, None))
        alerts = self.store.list_inbox(alice, team)["items"]
        self.assertEqual(sorted(item["event"]["receipt_id"] for item in alerts), ["receipt1", "receipt2"])
        self.assertIn(original[0], alerts)
        self.assertEqual(len(self.store.events_after()["items"]), 2)
        self.assertEqual(len(self.outbox()), 2)
        self.assertEqual(self.seen("alice.near"), [first["id"], second["id"]])
        self.assertEqual(self.history.list_records("alice.near", "transfers")["items"], [first])

    def test_a_legacy_workspace_event_is_matched_by_its_transfer_after_migration(self):
        old_workspace = self.head_module("workspace")
        path = self.root / "legacy" / "workspace.sqlite3"
        store = old_workspace.SQLiteWorkspaceStore(path, clock=lambda: self.now)
        user, team = self.team("alice", "alice.near", store=store)
        self.now += 1
        first = dict(transfer(1, timestamp=self.stamp()), transaction_hash=None)
        identifier, body = history.AccountHistory._event(first)
        sequence = store.ingest_event(identifier, json.loads(body), notify=True)
        other_id, other = history.AccountHistory._event(transfer(2, timestamp=self.stamp()))
        store.ingest_event(other_id, json.loads(other), notify=True)
        self.assertEqual(len(store.list_inbox(user, team)["items"]), 2)
        self.store = SQLiteWorkspaceStore(path, clock=lambda: self.now)
        self.store = SQLiteWorkspaceStore(path, clock=lambda: self.now)
        with self.store._connection() as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='events_transfer'").fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE transfer_id IS NULL").fetchone()[0], 0)
        changed_id, changed = history.AccountHistory._event(dict(first, transaction_hash="txfilled"))
        self.assertNotEqual(changed_id, identifier)
        self.assertEqual(self.store.ingest_event(changed_id, json.loads(changed), notify=True), sequence)
        self.assertEqual(len(self.store.list_inbox(user, team)["items"]), 2)
        self.assertEqual(len(self.store.events_after()["items"]), 2)

    def head_module(self, name):
        source = subprocess.run(["git", "show", BUILD_COMMIT + ":intel/" + name + ".py"], cwd=ROOT, check=True, capture_output=True).stdout
        path = self.root / ("head_" + name + ".py")
        path.write_bytes(source)
        spec = importlib.util.spec_from_file_location("head_" + name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_databases_written_by_the_build_branch_modules_open_and_prune(self):
        old_history, old_workspace = self.head_module("history"), self.head_module("workspace")
        legacy = self.root / "legacy"
        store = old_workspace.SQLiteWorkspaceStore(legacy / "workspace.sqlite3", clock=lambda: self.now)
        archive = old_history.AccountHistory(legacy / "history.sqlite3", clock=lambda: self.now)
        user, team = self.team("alice", "alice.near", store=store)
        self.poll("alice.near", [], archive=archive, store=store)
        items = []
        for number in (1, 2):
            items.insert(0, transfer(number, timestamp=self.stamp()))
            self.poll("alice.near", items, archive=archive, store=store)
        held = {item["event_id"]: archive.get_event_bytes(item["event_id"]) for item in store.list_inbox(user, team)["items"]}
        self.assertEqual(len(held), 2)
        self.store = SQLiteWorkspaceStore(legacy / "workspace.sqlite3", clock=lambda: self.now)
        self.history = history.AccountHistory(legacy / "history.sqlite3", clock=lambda: self.now)
        with self.history._connection() as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='outbox_delivered'").fetchone())
        with mock.patch.object(history, "MAX_ARCHIVE_EVENTS", 2), mock.patch.object(history, "MAX_ACCOUNT_WATCH_SEEN", 2):
            items.insert(0, transfer(3, timestamp=self.stamp()))
            self.poll("alice.near", items[:2])
            self.assertEqual(self.seen("alice.near"), [transfer(number)["id"] for number in (2, 3)])
            self.assertEqual(len(self.outbox()), 2)
            self.assertUsageMatchesRows()
        self.assertEqual(len(self.store.list_inbox(user, team)["items"]), 3)
        status, body, headers = self.request(WorkspaceAPI(self.store), "/auth/login", method="POST", body={"username": "alice", "password": "alice-password-123"})
        self.assertEqual(status, 200, body)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        api = WorkspaceAPI(self.store, history_store=self.history)
        pruned = [identifier for identifier in held if identifier not in self.outbox()]
        self.assertEqual(len(pruned), 1)
        for identifier, body in held.items():
            with self.subTest(identifier=identifier):
                self.assertEqual(self.request(api, f"/teams/{team}/events/{identifier}", cookie=cookie)[:2], (200, body))

    def assertWithinBounds(self, archive=None):
        with (archive or self.history)._connection() as connection:
            usage = connection.execute("SELECT * FROM archive_usage WHERE singleton=1").fetchone()
            events = connection.execute("SELECT COALESCE(SUM(LENGTH(body)),0) FROM event_outbox").fetchone()[0]
        for field, maximum in (("pages", history.MAX_ARCHIVE_PAGES), ("records", history.MAX_ARCHIVE_ROWS), ("graphs", history.MAX_ARCHIVE_GRAPHS),
                               ("events", history.MAX_ARCHIVE_EVENTS), ("seen", history.MAX_WATCH_SEEN), ("bytes", history.MAX_ARCHIVE_BYTES)):
            self.assertLessEqual(usage[field], maximum, field)
        self.assertLessEqual(usage["bytes"] - events, history.MAX_ARCHIVE_BYTES - history.EVENT_ARCHIVE_BYTES)

    def archived(self, archive=None):
        with (archive or self.history)._connection() as connection:
            return ({row[0]: row[1] for row in connection.execute("SELECT id,body FROM pages")},
                    {row[0]: tuple(row[1:]) for row in connection.execute("SELECT seq,account,row_id,stamp,body,page_id FROM history_rows")})

    def usage(self, field, archive=None):
        with (archive or self.history)._connection() as connection:
            return connection.execute("SELECT " + field + " FROM archive_usage WHERE singleton=1").fetchone()[0]

    def fill(self):
        for number in range(1, 1000):
            account = "fill" + str(number) + ".near"
            try:
                self.history.record_page(account, "transfers", page([], account=account), reserved=True)
            except history.HistoryCapacityError:
                return
        self.fail("the archive never reached its byte budget")

    def assertAlertsWithoutArchiving(self, api, user, team, cookie, item):
        event_id, body = history.AccountHistory._event(item)
        pages, rows = self.archived()
        self.poll("carol.near", [item])
        with self.history._connection() as connection:
            state = connection.execute("SELECT last_error,coverage_gap FROM watch_poll WHERE account='carol.near'").fetchone()
        self.assertEqual(tuple(state), (None, 1))
        self.assertEqual(self.history.status()["coverage_gap_accounts"], 1)
        self.assertEqual([entry["event_id"] for entry in self.store.list_inbox(user, team)["items"]], [event_id])
        self.assertEqual(self.request(api, f"/teams/{team}/events/{event_id}", cookie=cookie)[:2], (200, body))
        self.assertEqual(self.archived(), (pages, rows))
        self.assertNotIn(item, self.history.list_records("carol.near", "transfers")["items"])
        self.assertIn(item["id"], self.seen("carol.near"))
        self.assertUsageMatchesRows()
        self.assertWithinBounds()

    def full_archive(self):
        api = WorkspaceAPI(self.store, history_store=self.history)
        user, team, cookie = self.api_team(api, "carol.near")
        bob = self.team("bob", "bob.near")
        self.reader.pages["carol.near", None] = page([], account="carol.near")
        self.poll("bob.near", [transfer(number, subject="bob.near", timestamp=self.stamp()) for number in (2, 1)])
        self.poll("carol.near", [])
        return api, user, team, cookie, bob

    def test_a_full_page_budget_still_raises_another_teams_alert_without_archiving_its_page(self):
        api, user, team, cookie, bob = self.full_archive()
        with mock.patch.object(history, "MAX_ARCHIVE_PAGES", self.usage("pages")):
            self.assertAlertsWithoutArchiving(api, user, team, cookie, transfer(5, subject="carol.near", other="erin.near", timestamp=self.stamp()))
        self.assertEqual(self.store.list_inbox(*bob)["items"], [])

    def test_a_full_row_budget_still_raises_another_teams_alert_without_archiving_its_page(self):
        api, user, team, cookie, bob = self.full_archive()
        with mock.patch.object(history, "MAX_ARCHIVE_ROWS", self.usage("records")):
            self.assertAlertsWithoutArchiving(api, user, team, cookie, transfer(5, subject="carol.near", other="erin.near", timestamp=self.stamp()))
        self.assertEqual(self.store.list_inbox(*bob)["items"], [])

    def test_a_byte_budget_full_of_archived_pages_still_raises_another_teams_alert(self):
        self.assertEqual((history.EVENT_ARCHIVE_BYTES, history.MAX_ARCHIVE_BYTES), (16 * 1024 * 1024, 128 * 1024 * 1024))
        api, user, team, cookie, bob = self.full_archive()
        item = transfer(5, subject="carol.near", other="erin.near", timestamp=self.stamp())
        share = len(history.AccountHistory._event(item)[1]) + 100
        with mock.patch.object(history, "EVENT_ARCHIVE_BYTES", share), mock.patch.object(history, "MAX_ARCHIVE_BYTES", self.usage("bytes") + share + 5000):
            self.fill()
            self.assertEqual(self.outbox(), {})
            self.assertGreater(self.usage("bytes") + 300, history.MAX_ARCHIVE_BYTES - share)
            with self.assertRaises(history.HistoryCapacityError):
                self.history.record_page("carol.near", "transfers", page([item], account="carol.near"), reserved=True)
            self.assertAlertsWithoutArchiving(api, user, team, cookie, item)
        self.assertEqual(self.store.list_inbox(*bob)["items"], [])

    def test_a_byte_budget_full_of_pages_and_delivered_events_still_admits_a_new_event(self):
        api = WorkspaceAPI(self.store, history_store=self.history)
        user, team, cookie = self.api_team(api, "carol.near")
        self.poll("carol.near", [])
        items = [transfer(number, subject="carol.near", timestamp=str(int(self.now * 1000 + number) * 1000000)) for number in (3, 2, 1)]
        size = len(history.AccountHistory._event(items[0])[1])
        self.assertEqual({len(history.AccountHistory._event(item)[1]) for item in items}, {size})
        with mock.patch.object(history, "EVENT_ARCHIVE_BYTES", 2 * size + 50), mock.patch.object(history, "MAX_ARCHIVE_BYTES", self.usage("bytes") + 2 * size + 5000):
            self.fill()
            for count in (1, 2):
                self.poll("carol.near", items[-count:])
            held = {entry["event_id"]: self.history.get_event_bytes(entry["event_id"]) for entry in self.store.list_inbox(user, team)["items"]}
            self.assertEqual(len(held), 2)
            self.assertTrue(all(sequence is not None for sequence in self.outbox().values()))
            self.assertWithinBounds()
            event_id, body = history.AccountHistory._event(items[0])
            self.assertGreater(self.usage("bytes") + size, history.MAX_ARCHIVE_BYTES)
            pages, rows = self.archived()
            self.poll("carol.near", items)
            self.assertIsNone(self.watch_error("carol.near"))
            inbox = [entry["event_id"] for entry in self.store.list_inbox(user, team)["items"]]
            self.assertEqual(sorted(inbox), sorted({event_id, *held}))
            outbox = self.outbox()
            self.assertIn(event_id, outbox)
            self.assertEqual(set(held) - set(outbox), {history.AccountHistory._event(items[2])[0]})
            self.assertEqual(self.archived(), (pages, rows))
            self.assertUsageMatchesRows()
            self.assertWithinBounds()
        for identifier, stored in {**held, event_id: body}.items():
            with self.subTest(identifier=identifier):
                self.assertEqual(self.request(api, f"/teams/{team}/events/{identifier}", cookie=cookie)[:2], (200, stored))

    def test_a_history_database_written_by_the_build_branch_module_alerts_when_its_page_budget_is_full(self):
        old_history, old_workspace = self.head_module("history"), self.head_module("workspace")
        legacy = self.root / "legacy"
        store = old_workspace.SQLiteWorkspaceStore(legacy / "workspace.sqlite3", clock=lambda: self.now)
        archive = old_history.AccountHistory(legacy / "history.sqlite3", clock=lambda: self.now)
        user, team = self.team("carol", "carol.near", store=store)
        self.poll("carol.near", [], archive=archive, store=store)
        first = transfer(1, subject="carol.near", timestamp=self.stamp())
        self.poll("carol.near", [first], archive=archive, store=store)
        held = store.list_inbox(user, team)["items"]
        self.assertEqual(len(held), 1)
        self.store = SQLiteWorkspaceStore(legacy / "workspace.sqlite3", clock=lambda: self.now)
        self.history = history.AccountHistory(legacy / "history.sqlite3", clock=lambda: self.now)
        status, body, headers = self.request(WorkspaceAPI(self.store), "/auth/login", method="POST", body={"username": "carol", "password": "carol-password-123"})
        self.assertEqual(status, 200, body)
        api = WorkspaceAPI(self.store, history_store=self.history)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        with mock.patch.object(history, "MAX_ARCHIVE_PAGES", self.usage("pages")):
            second = transfer(2, subject="carol.near", timestamp=self.stamp())
            event_id, stored = history.AccountHistory._event(second)
            pages, rows = self.archived()
            self.poll("carol.near", [second, first])
            self.assertIsNone(self.watch_error("carol.near"))
            self.assertEqual(self.history.status()["coverage_gap_accounts"], 1)
            inbox = self.store.list_inbox(user, team)["items"]
            self.assertEqual(len(inbox), 2)
            self.assertEqual([entry["event_id"] for entry in inbox if entry["event_id"] != held[0]["event_id"]], [event_id])
            self.assertEqual(self.request(api, f"/teams/{team}/events/{event_id}", cookie=cookie)[:2], (200, stored))
            self.assertEqual(self.archived(), (pages, rows))
            self.assertUsageMatchesRows()
            self.assertWithinBounds()

    def test_a_pruned_event_whose_workspace_copy_is_not_text_answers_404(self):
        api = WorkspaceAPI(self.store, history_store=self.history)
        user, team, cookie = self.api_team(api, "alice.near")
        self.poll("alice.near", [])
        self.poll("alice.near", [transfer(1, timestamp=self.stamp())])
        pruned = self.store.list_inbox(user, team)["items"][0]["event_id"]
        with mock.patch.object(history, "MAX_ARCHIVE_EVENTS", 1):
            self.poll("alice.near", [transfer(2, timestamp=self.stamp())])
        self.assertIsNone(self.history.get_event_bytes(pruned))
        unknown = self.request(api, f"/teams/{team}/events/{'b' * 64}", cookie=cookie)
        self.assertEqual(unknown[0], 404)
        for value in ("CAST(body_json AS BLOB)", "7"):
            with self.subTest(value=value):
                with self.store._connection(write=True) as connection:
                    connection.execute("UPDATE events SET body_json=" + value + " WHERE event_id=?", (pruned,))
                self.assertIsNotNone(self.store.team_event(user, team, pruned))
                self.assertEqual(self.request(api, f"/teams/{team}/events/{pruned}", cookie=cookie), unknown)

    def test_a_byte_full_history_database_from_the_build_branch_alerts_on_several_new_transfers(self):
        old_history, old_workspace = self.head_module("history"), self.head_module("workspace")
        legacy = self.root / "legacy"
        store = old_workspace.SQLiteWorkspaceStore(legacy / "workspace.sqlite3", clock=lambda: self.now)
        archive = old_history.AccountHistory(legacy / "history.sqlite3", clock=lambda: self.now)
        user, team = self.team("carol", "carol.near", store=store)
        self.poll("carol.near", [], archive=archive, store=store)
        limit = self.usage("bytes", archive) + 20000
        with mock.patch.object(old_history, "MAX_ARCHIVE_BYTES", limit):
            for number in range(1, 1000):
                account = "fill" + str(number) + ".near"
                try:
                    archive.record_page(account, "transfers", page([], account=account), reserved=True)
                except old_history.HistoryCapacityError:
                    break
        self.store = SQLiteWorkspaceStore(legacy / "workspace.sqlite3", clock=lambda: self.now)
        self.history = history.AccountHistory(legacy / "history.sqlite3", clock=lambda: self.now)
        items = [transfer(number, subject="carol.near", timestamp=str(int(self.now * 1000 + number) * 1000000)) for number in range(5, 0, -1)]
        size = len(history.AccountHistory._event(items[0])[1])
        self.assertEqual({len(history.AccountHistory._event(item)[1]) for item in items}, {size})
        self.assertEqual(self.outbox(), {})
        kept = self.usage("bytes")
        self.assertGreater(kept + size, limit)
        pages, rows = self.archived()
        with mock.patch.object(history, "MAX_ARCHIVE_BYTES", limit), mock.patch.object(history, "EVENT_ARCHIVE_BYTES", 3 * size + 50):
            self.poll("carol.near", items[-2:])
            self.assertIsNone(self.watch_error("carol.near"))
            self.assertEqual(len(self.store.list_inbox(user, team)["items"]), 2)
            self.assertEqual(len(self.outbox()), 2)
            self.poll("carol.near", items[-3:])
            self.assertIsNone(self.watch_error("carol.near"))
            self.assertEqual(set(self.outbox()), {history.AccountHistory._event(item)[0] for item in items[-3:]})
            self.assertUsageMatchesRows()
            self.poll("carol.near", items)
            self.assertIsNone(self.watch_error("carol.near"))
            inbox = self.store.list_inbox(user, team)["items"]
            self.assertEqual(sorted(entry["event"]["receipt_id"] for entry in inbox), sorted(item["receipt_id"] for item in items))
            self.assertEqual(set(self.outbox()), {history.AccountHistory._event(item)[0] for item in items[:3]})
            self.assertEqual(self.archived(), (pages, rows))
            self.assertEqual(self.history.status()["coverage_gap_accounts"], 1)
            self.assertUsageMatchesRows()
            with self.history._connection() as connection:
                events = connection.execute("SELECT COALESCE(SUM(LENGTH(body)),0) FROM event_outbox").fetchone()[0]
            self.assertEqual(self.usage("bytes") - events, kept)
            self.assertLessEqual(events, history.EVENT_ARCHIVE_BYTES)
            self.assertGreater(self.usage("bytes"), limit)
        for entry in inbox:
            if entry["event_id"] in self.outbox():
                self.assertEqual(self.history.get_event_bytes(entry["event_id"]), history._canonical(entry["event"]))

    def test_more_watched_accounts_than_one_statement_can_bind_still_poll_and_alert(self):
        user, team = self.team("carol", "carol.near")
        accounts = ["watch" + str(number) + ".near" for number in range(33000)]
        with self.store._connection(write=True) as connection:
            connection.executemany("INSERT INTO watchlist VALUES (?,?,?,?)", [(team, account, 0, 0) for account in accounts])
        with self.history._connection(write=True) as connection:
            connection.executemany("INSERT INTO watch_poll(account,next_due_ms) VALUES (?,?)", [(account, 2 ** 62) for account in accounts + ["gone.near"]])
            connection.execute("INSERT INTO watch_seen VALUES ('gone.near','gone')")
            connection.execute("UPDATE archive_usage SET seen=seen+1")
        self.assertGreater(len(self.store.watched_accounts()), 32766)
        self.poll("carol.near", [])
        self.poll("carol.near", [transfer(1, subject="carol.near", timestamp=self.stamp())])
        self.assertIsNone(self.history.status()["last_error"])
        self.assertIsNone(self.watch_error("carol.near"))
        self.assertEqual(len(self.store.list_inbox(user, team)["items"]), 1)
        with self.history._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM watch_poll").fetchone()[0], 33001)
            self.assertIsNone(connection.execute("SELECT 1 FROM watch_poll WHERE account='gone.near'").fetchone())
        self.assertEqual(self.seen("gone.near"), [])
        self.assertUsageMatchesRows()

    def test_a_storage_error_in_a_watch_tick_is_recorded_and_the_service_keeps_polling(self):
        user, team = self.team("carol", "carol.near")
        self.reader.pages["carol.near", None] = page([], account="carol.near")
        opener = history.AccountHistory._connection
        commits = []

        @contextmanager
        def failing_commit(archive, *, write=False, transaction=True):
            with opener(archive, write=write, transaction=transaction) as opened:
                yield opened
                if write and not commits:
                    commits.append(write)
                    raise sqlite3.OperationalError("database or disk is full")

        with mock.patch.object(history.AccountHistory, "_connection", failing_commit):
            self.now += 5
            status = self.history.poll_watches(self.store, self.reader)
        self.assertEqual(commits, [True])
        self.assertEqual(status["last_error"], "OperationalError")
        self.assertEqual(self.reader.calls, [])
        with self.history._connection() as opened:
            self.assertIsNone(opened.execute("SELECT last_tick_ms FROM history_state").fetchone()[0])
            self.assertEqual(opened.execute("SELECT COUNT(*) FROM watch_poll").fetchone()[0], 0)
        reserve = history.AccountHistory._reserve
        failures = []

        def failing(connection, **changes):
            if not failures:
                failures.append(changes)
                raise sqlite3.OperationalError("database or disk is full")
            return reserve(connection, **changes)

        with mock.patch.object(history.AccountHistory, "_reserve", staticmethod(failing)):
            self.now += 5
            status = self.history.poll_watches(self.store, self.reader)
        self.assertEqual(failures, [{"seen": 0}])
        self.assertEqual(status["last_error"], "OperationalError")
        self.assertEqual(status["heartbeat_ms"], int(self.now * 1000))
        self.assertEqual(self.reader.calls, [])
        self.poll("carol.near", [])
        self.poll("carol.near", [transfer(1, subject="carol.near", timestamp=self.stamp())])
        self.assertIsNone(self.history.status()["last_error"])
        self.assertEqual(len(self.store.list_inbox(user, team)["items"]), 1)
        commits.clear()

        @contextmanager
        def failing_twice(archive, *, write=False, transaction=True):
            with opener(archive, write=write, transaction=transaction) as opened:
                yield opened
                if write and len(commits) < 2:
                    commits.append(write)
                    raise sqlite3.OperationalError("database or disk is full")

        log = io.StringIO()
        with mock.patch.object(history.AccountHistory, "_connection", failing_twice), redirect_stderr(log):
            self.now += 5
            status = self.history.poll_watches(self.store, self.reader)
        self.assertEqual(commits, [True, True])
        self.assertIn("OperationalError", log.getvalue())
        self.assertEqual(status["heartbeat_ms"], int(self.now * 1000))
        self.poll("carol.near", [transfer(2, subject="carol.near", timestamp=self.stamp())])
        self.assertIsNone(self.history.status()["last_error"])
        self.assertEqual(len(self.store.list_inbox(user, team)["items"]), 2)
        archive_path = self.root / "service.sqlite3"
        poll_watches = history.AccountHistory.poll_watches
        calls = []

        def flaky(archive, *args):
            calls.append(args)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database or disk is full")
            return poll_watches(archive, *args)

        class Stop:
            def wait(inner, interval):
                return len(calls) == 2

        log = io.StringIO()
        with mock.patch.object(history.AccountHistory, "poll_watches", flaky), redirect_stderr(log):
            status = history.run(self.store, self.reader, archive_path, stop_event=Stop())
            self.assertEqual(len(calls), 2)
            calls.clear()
            with self.assertRaises(sqlite3.OperationalError):
                history.run(self.store, self.reader, archive_path, once=True)
        self.assertIn("OperationalError", log.getvalue())
        self.assertEqual(status["mode"], "service")
        self.assertIsNotNone(status["last_success_ms"])
        self.assertIsNone(status["last_error"])

    def test_every_documented_budget_full_with_long_accounts_fits_the_database_and_still_alerts(self):
        def name(*parts):
            return hashlib.sha256(repr(parts).encode()).hexdigest()

        def insert(statement, rows):
            rows = iter(rows)
            while chunk := list(itertools.islice(rows, 5000)):
                with self.history._connection(write=True) as connection:
                    connection.executemany(statement, chunk)

        _, filler = self.team("bob", "bob.near")
        accounts = [name("account", number) for number in range(history.MAX_WATCH_SEEN // history.MAX_ACCOUNT_WATCH_SEEN)]
        with self.store._connection(write=True) as connection:
            connection.executemany("INSERT INTO watchlist VALUES (?,?,?,?)", [(filler, account, 0, 0) for account in accounts])
        event_size = history.EVENT_ARCHIVE_BYTES // history.MAX_ARCHIVE_EVENTS
        row_size = 650
        page_size = (history.MAX_ARCHIVE_BYTES - history.MAX_ARCHIVE_ROWS * row_size) // history.MAX_ARCHIVE_PAGES
        insert("INSERT INTO watch_poll(account,initialized,next_due_ms) VALUES (?,1,?)", ((account, 2 ** 62) for account in accounts + ["bob.near"]))
        insert("INSERT INTO pages VALUES (?,?,?,?,?)", ((name("page", number), accounts[number % len(accounts)], "transfers", bytes(page_size), number) for number in range(history.MAX_ARCHIVE_PAGES)))
        insert("INSERT INTO history_rows(account,kind,row_id,stamp,body,page_id) VALUES (?,?,?,?,?,?)",
               ((accounts[number % len(accounts)], "transfers", name("row", number), str(1700000000000000000 + number), bytes(row_size), name("page", number % history.MAX_ARCHIVE_PAGES))
                for number in range(history.MAX_ARCHIVE_ROWS)))
        insert("INSERT INTO event_outbox VALUES (?,?,1,?,?)", ((name("event", number), bytes(event_size), number, number) for number in range(history.MAX_ARCHIVE_EVENTS)))
        insert("INSERT INTO watch_seen VALUES (?,?)", ((accounts[number % len(accounts)], name("seen", number)) for number in range(history.MAX_WATCH_SEEN)))
        insert("INSERT INTO graphs VALUES (?,?,?)", ((name("graph", number), b"", number) for number in range(history.MAX_ARCHIVE_GRAPHS)))
        insert("INSERT INTO backfills VALUES (?,?,?,?,?)", ((name("backfill", number), "transfers", name("cursor", number) * 64, 0, number) for number in range(10000)))
        insert("INSERT INTO backfill_control VALUES (?,?,?,?,?)", ((name("backfill", number), "transfers", number, name("lease", number)[:32], number) for number in range(10000)))
        insert("INSERT INTO backfill_control VALUES (?,?,?,?,?)", ((name("control", number), "job", number, name("lease", number)[:32], number) for number in range(20000)))
        with self.history._connection(write=True) as connection:
            connection.execute("UPDATE archive_usage SET pages=(SELECT COUNT(*) FROM pages),records=(SELECT COUNT(*) FROM history_rows),graphs=(SELECT COUNT(*) FROM graphs),"
                               "events=(SELECT COUNT(*) FROM event_outbox),seen=(SELECT COUNT(*) FROM watch_seen),"
                               "bytes=(SELECT SUM(LENGTH(body)) FROM pages)+(SELECT SUM(LENGTH(body)) FROM history_rows)+(SELECT SUM(LENGTH(body)) FROM event_outbox)")
        self.assertUsageMatchesRows()
        self.assertGreater(self.usage("bytes"), history.MAX_ARCHIVE_BYTES + history.EVENT_ARCHIVE_BYTES - history.MAX_ARCHIVE_EVENTS)
        victim = name("victim")
        user, team = self.team("erin", victim)
        self.poll(victim, [])
        self.poll(victim, [transfer(1, subject=victim, timestamp=self.stamp())])
        self.assertIsNone(self.watch_error(victim))
        self.assertIsNone(self.history.status()["last_error"])
        self.assertEqual(len(self.store.list_inbox(user, team)["items"]), 1)
        self.assertUsageMatchesRows()
        with self.history._connection() as connection:
            used, limit = connection.execute("PRAGMA page_count").fetchone()[0], connection.execute("PRAGMA max_page_count").fetchone()[0]
        self.assertEqual(limit, 131072)
        self.assertLess(used, limit * 3 // 4)


if __name__ == "__main__":
    unittest.main()
