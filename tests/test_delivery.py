import io
import json
import shutil
import subprocess
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError, URLError

from intel.delivery import MAX_ATTEMPT_ROWS, MAX_ATTEMPTS, TelegramClient, TelegramDelivery, TelegramError, configured_client, run
from intel.workspace import SQLiteWorkspaceStore, WorkspaceError


class FakeClient:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.send_results = []
        self.username = "siyabonnearbot"
        self.webhook = ""
        self.bot_id = 123456
        self.filter_updates = True

    def configuration(self):
        return {"configured": True, "reason": None}

    def call(self, method, payload):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"id": self.bot_id, "is_bot": True, "username": self.username}
        if method == "getWebhookInfo":
            return {"url": self.webhook}
        if method == "getUpdates":
            updates, self.updates = self.updates, []
            allowed = payload.get("allowed_updates")
            return [update for update in updates if not self.filter_updates or allowed is None or set(update) - {"update_id"} <= set(allowed)]
        if method == "sendMessage":
            result = self.send_results.pop(0) if self.send_results else None
            if isinstance(result, BaseException):
                raise result
            return result if result is not None else {"message_id": 42, "chat": {"id": payload["chat_id"]}}
        raise AssertionError(method)


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1700000000.0
        self.store = SQLiteWorkspaceStore(Path(self.temp.name) / "workspace.sqlite3", clock=lambda: self.now)
        self.owner = self.store.register("owner", "owner-password-123")["user"]["id"]
        self.other = self.store.register("other", "other-password-123")["user"]["id"]
        self.team = self.store.list_teams(self.owner)[0]["id"]
        self.client = FakeClient()
        self.delivery = TelegramDelivery(self.store, self.client, bot_username="siyabonnearbot", clock=lambda: self.now)
        self.store.add_watch(self.owner, self.team, "wrap.near")

    def error(self, status, method, *args):
        with self.assertRaises(WorkspaceError) as caught:
            method(*args)
        self.assertEqual(caught.exception.status, status)

    def start_update(self, nonce, update_id=1, chat_id=555, chat_type="private"):
        return {"update_id": update_id, "message": {"text": "/start " + nonce,
                "date": int(self.now), "chat": {"id": chat_id, "type": chat_type, "first_name": "Owner"},
                "from": {"id": chat_id, "is_bot": False, "username": "ownertg"}}}

    def activate(self):
        link = self.delivery.begin_link(self.owner, self.team)
        nonce = link["link"].split("start=")[1]
        self.client.updates = [self.start_update(nonce)]
        self.delivery.run_once()
        self.delivery.confirm_link(self.owner, self.team, link["generation"])
        return link

    def event(self, identifier="a", candidate=False):
        self.now += 1
        event = {"signal": "full_access_key_then_outflow" if candidate else "account_transfer",
                 "subject": "wrap.near", "detail": "PRIVATE NOTES MUST NEVER BE SENT",
                 "notes": "ANOTHER PRIVATE FIELD", "observed": {"timestamp_ms": int(self.now * 1000), "block_height": 4}}
        self.store.ingest_event(identifier * 64, event, notify=True)

    def sends(self):
        return [payload for method, payload in self.client.calls if method == "sendMessage"]

    def test_unconfigured_is_honest_and_no_network_or_binding(self):
        delivery = TelegramDelivery(self.store, bot_username="siyabonnearbot", clock=lambda: self.now)
        self.assertFalse(delivery.status(self.owner, self.team)["configured"])
        self.assertFalse(delivery.run_once()["configured"])
        self.error(503, delivery.begin_link, self.owner, self.team)

    def test_nonce_private_observation_then_explicit_owner_confirmation(self):
        link = self.delivery.begin_link(self.owner, self.team)
        nonce = link["link"].split("start=")[1]
        self.assertRegex(nonce, r"^[a-f0-9]{64}$")
        with self.store._connection() as connection:
            self.assertNotIn(nonce, "\n".join(connection.iterdump()))
        self.error(409, self.delivery.confirm_link, self.owner, self.team, link["generation"])
        self.client.updates = [self.start_update(nonce, chat_type="group")]
        self.delivery.run_once()
        self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "expired")
        link = self.delivery.begin_link(self.owner, self.team)
        nonce = link["link"].split("start=")[1]
        self.client.updates = [self.start_update(nonce, update_id=2)]
        self.delivery.run_once()
        observed = self.delivery.status(self.owner, self.team)
        self.assertEqual(observed["state"], "observed")
        self.assertIn("ownertg", observed["observed_chat_label"])
        self.assertEqual(self.sends(), [])
        self.error(403, self.delivery.confirm_link, self.other, self.team, link["generation"])
        self.delivery.confirm_link(self.owner, self.team, link["generation"])
        self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "active")

    def test_nonce_expiry_replay_stale_generation_and_no_arbitrary_chat(self):
        first = self.delivery.begin_link(self.owner, self.team)
        old_nonce = first["link"].split("start=")[1]
        self.now += 901
        self.client.updates = [self.start_update(old_nonce)]
        self.delivery.run_once()
        self.error(409, self.delivery.confirm_link, self.owner, self.team, first["generation"])
        second = self.delivery.begin_link(self.owner, self.team)
        self.client.updates = [self.start_update(second["link"].split("start=")[1], update_id=2)]
        self.delivery.run_once()
        self.error(409, self.delivery.confirm_link, self.owner, self.team, first["generation"])
        self.delivery.confirm_link(self.owner, self.team, second["generation"])
        self.client.updates = [self.start_update(old_nonce, update_id=3, chat_id=999)]
        self.delivery.run_once()
        self.event()
        self.delivery.run_once()
        self.assertEqual(self.sends()[0]["chat_id"], 555)

    def test_activation_baseline_dedup_candidate_exclusion_and_public_only_payload(self):
        self.event("b")
        self.activate()
        self.delivery.run_once()
        self.assertEqual(self.sends(), [])
        self.event("c", candidate=True)
        self.event("d")
        self.delivery.run_once()
        self.delivery.run_once()
        self.assertEqual(len(self.sends()), 1)
        message = self.sends()[0]["text"]
        self.assertIn("wrap.near", message)
        self.assertIn("d" * 64, message)
        self.assertNotIn("PRIVATE", message)
        self.assertNotIn("parse_mode", self.sends()[0])
        rows = self.delivery.list_deliveries(self.owner, self.team)["items"]
        self.assertEqual(rows[0]["state"], "sent")

    def test_retries_are_durable_bounded_and_rate_delay_is_respected(self):
        self.activate()
        self.event()
        self.client.send_results = [TelegramError("provider_429", retryable=True, retry_after=30)]
        self.delivery.run_once()
        self.assertEqual(len(self.sends()), 1)
        restarted = TelegramDelivery(self.store, self.client, bot_username="siyabonnearbot", clock=lambda: self.now)
        restarted.run_once()
        self.assertEqual(len(self.sends()), 1)
        self.now += 31
        restarted.run_once()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(restarted.list_deliveries(self.owner, self.team)["items"][0]["state"], "sent")

    def test_permanent_failure_deadletter_manual_retry_and_revoke(self):
        self.activate()
        self.event()
        self.client.send_results = [TelegramError("provider_403", retryable=False)]
        self.delivery.run_once()
        row = self.delivery.list_deliveries(self.owner, self.team)["items"][0]
        self.assertEqual(row["state"], "dead")
        self.error(403, self.delivery.list_deliveries, self.other, self.team)
        self.error(403, self.delivery.retry_dead, self.other, self.team, row["id"])
        self.delivery.retry_dead(self.owner, self.team, row["id"])
        self.delivery.revoke(self.owner, self.team)
        self.now += 60
        self.delivery.run_once()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.delivery.list_deliveries(self.owner, self.team)["items"][0]["state"], "cancelled")

    def test_bot_identity_and_existing_webhook_fail_closed(self):
        self.activate()
        self.event()
        self.client.username = "unrelatedbot"
        result = self.delivery.run_once()
        self.assertEqual(result["last_error"], "bot_identity_mismatch")
        self.assertEqual(self.sends(), [])
        self.client.username = "siyabonnearbot"
        self.client.webhook = "https://other.example.test/private-hook"
        self.now += 6
        self.assertEqual(self.delivery.run_once()["last_error"], "bot_webhook_configured")
        self.assertEqual(self.sends(), [])

    def test_worker_once_and_shutdown_use_the_same_durable_service(self):
        self.assertFalse(run(self.store, TelegramClient(), once=True)["configured"])
        stop = threading.Event()
        stop.set()
        self.assertFalse(run(self.store, TelegramClient(), stop=stop)["configured"])
        for interval in (True, 0, 61, float("nan")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                run(self.store, TelegramClient(), once=True, interval=interval)

    def test_crash_claim_is_recovered_and_uncertain_ack_is_visible(self):
        self.activate()
        self.event()
        self.delivery._enqueue()
        self.delivery._claim()
        self.now += 46
        restarted = TelegramDelivery(self.store, self.client, clock=lambda: self.now)
        restarted.run_once()
        row = restarted.list_deliveries(self.owner, self.team)["items"][0]
        self.assertEqual((row["state"], row["attempts"], row["uncertain_ack"]), ("sent", 2, 1))

    def test_five_transient_failures_deadletter_and_never_loop_forever(self):
        self.activate()
        self.event()
        self.client.send_results = [TelegramError("transport_unavailable", retryable=True, uncertain=True) for _ in range(5)]
        for _ in range(6):
            self.delivery.run_once()
            self.now += 301
        row = self.delivery.list_deliveries(self.owner, self.team)["items"][0]
        self.assertEqual((row["state"], row["attempts"]), ("dead", 5))
        self.assertEqual(len(self.sends()), 5)

    def test_persisted_bot_id_cannot_change_even_with_same_username(self):
        self.activate()
        self.event()
        self.client.bot_id += 1
        self.assertEqual(self.delivery.run_once()["last_error"], "bot_identity_mismatch")
        self.assertEqual(self.sends(), [])

    def test_repeated_crash_claims_also_stop_at_maximum_attempts(self):
        self.activate()
        self.event()
        self.delivery._enqueue()
        for _ in range(5):
            self.assertIsNotNone(self.delivery._claim())
            self.now += 46
        self.assertIsNone(self.delivery._claim())
        row = self.delivery.list_deliveries(self.owner, self.team)["items"][0]
        self.assertEqual((row["state"], row["attempts"], row["uncertain_ack"]), ("dead", 5, 1))

    def test_old_update_cannot_rewind_offset_or_bind_a_new_nonce(self):
        self.activate()
        link = self.delivery.begin_link(self.owner, self.team)
        self.client.updates = [self.start_update(link["link"].split("start=")[1], update_id=0, chat_id=999)]
        self.delivery.run_once()
        self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "pending")
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT update_offset FROM delivery_state").fetchone()[0], 2)

    def test_queue_budget_preserves_first_unqueued_cursor_and_resumes_after_pruning_a_delivered_row(self):
        self.activate()
        self.event("a", candidate=True)
        for identifier in "bcd":
            self.event(identifier)
        with patch("intel.delivery.MAX_QUEUE_ROWS", 2, create=True):
            self.assertEqual(self.delivery._enqueue(), 2)
            restarted = TelegramDelivery(self.store, self.client, clock=lambda: self.now)
            self.assertEqual(restarted._enqueue(), 0)
            self.assertTrue(restarted.status(self.owner, self.team)["capacity"]["queue_paused"])
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT cursor_id FROM delivery_bindings").fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0], 2)
            claimed = restarted._claim()
            restarted._send(claimed)
            archived = restarted.list_deliveries(self.owner, self.team)["items"]
            self.assertFalse(restarted.status(self.owner, self.team)["capacity"]["queue_paused"])
            self.assertEqual(restarted._enqueue(), 1)
            current = restarted.list_deliveries(self.owner, self.team)["items"]
            self.assertEqual({row["event_id"] for row in archived + current}, {char * 64 for char in "bcd"})
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT cursor_id FROM delivery_bindings").fetchone()[0], 4)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 4)

    def test_queue_budget_serializes_concurrent_workers_and_skips_only_ineligible_rows(self):
        self.activate()
        self.event("a", candidate=True)
        for identifier in "bcde":
            self.event(identifier)
        workers = [TelegramDelivery(self.store, self.client, clock=lambda: self.now) for _ in range(2)]
        with patch("intel.delivery.MAX_QUEUE_ROWS", 2, create=True), ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(lambda worker: worker._enqueue(), workers)), 2)
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT cursor_id FROM delivery_bindings").fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0], 2)

    def test_401_events_cannot_grow_retention_past_budget_across_restart(self):
        self.activate()
        self.now += 1
        for index in range(401):
            self.store.ingest_event(f"{index:064x}", {"signal": "account_transfer", "subject": "wrap.near", "detail": "Observed transfer",
                                    "observed": {"timestamp_ms": int(self.now * 1000)}}, notify=True)
        with patch("intel.delivery.MAX_QUEUE_ROWS", 250):
            self.assertEqual(self.delivery._enqueue(), 200)
            restarted = TelegramDelivery(self.store, self.client, clock=lambda: self.now)
            self.assertEqual(restarted._enqueue(), 50)
            self.assertEqual(restarted._enqueue(), 0)
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0], 250)
                self.assertEqual(connection.execute("SELECT cursor_id FROM delivery_bindings").fetchone()[0], 250)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 401)
            self.assertTrue(restarted.status(self.owner, self.team)["capacity"]["queue_paused"])
            self.assertFalse(restarted.run_once()["capacity"]["queue_paused"])
            self.assertEqual(restarted._enqueue(), 1)
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0], 250)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_queue WHERE state='sent'").fetchone()[0], 0)
            other_team = self.store.list_teams(self.other)[0]["id"]
            status = restarted.status(self.other, other_team)
            self.assertEqual((status["counts"], status["capacity"]["paused"]), ({}, False))
            self.assertEqual(set(status["capacity"]), {"paused", "queue_paused", "attempts_paused", "queue_rows_limit", "attempt_rows_limit"})

    def test_full_queue_can_advance_late_historical_and_candidate_rows_without_losing_eligible_alert(self):
        self.now += 10
        activated_at = self.now
        self.activate()
        self.event("a")
        with patch("intel.delivery.MAX_QUEUE_ROWS", 1):
            self.assertEqual(self.delivery._enqueue(), 1)
            self.store.ingest_event("b" * 64, {"signal": "account_transfer", "subject": "wrap.near", "detail": "Observed transfer",
                                    "observed": {"timestamp_ms": int((activated_at - 5) * 1000)}}, notify=True)
            self.event("c", candidate=True)
            self.event("d")
            self.assertEqual(self.delivery._enqueue(), 0)
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT cursor_id FROM delivery_bindings").fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 4)

    def test_attempt_budget_held_by_undelivered_rows_pauses_claims_and_dead_letters_give_way(self):
        self.activate()
        for identifier in "ab":
            self.event(identifier)
        self.delivery._enqueue()
        with patch("intel.delivery.MAX_ATTEMPT_ROWS", 1, create=True):
            claimed = self.delivery._claim()
            self.client.send_results = [TelegramError("transport_unavailable", retryable=True)]
            self.delivery._send(claimed)
            before = self.delivery.list_deliveries(self.owner, self.team)["items"]
            restarted = TelegramDelivery(self.store, self.client, clock=lambda: self.now)
            self.now += 2
            self.assertIsNone(restarted._claim())
            self.assertEqual(before, restarted.list_deliveries(self.owner, self.team)["items"])
            self.assertTrue(restarted.run_once()["capacity"]["attempts_paused"])
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 1)
        with patch("intel.delivery.MAX_ATTEMPT_ROWS", 2, create=True):
            again = self.delivery._claim()
            self.assertEqual(again["id"], claimed["id"])
            self.client.send_results = [TelegramError("provider_403")]
            self.delivery._send(again)
            self.now += 2
            self.assertEqual(self.delivery._claim()["event_id"], "b" * 64)
            self.assertEqual([row["event_id"] for row in self.delivery.list_deliveries(self.owner, self.team)["items"]], ["b" * 64])
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 1)

    def test_attempt_budget_is_atomic_across_concurrent_claims(self):
        self.activate()
        for identifier in "ab":
            self.event(identifier)
        self.delivery._enqueue()
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0], 2)
        workers = [TelegramDelivery(self.store, self.client, clock=lambda: self.now) for _ in range(2)]
        with patch("intel.delivery.MAX_ATTEMPT_ROWS", 1, create=True), ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda worker: worker._claim(), workers))
            self.assertEqual(sum(row is not None for row in claimed), 1)
            with self.store._connection(write=True) as connection:
                connection.execute("UPDATE delivery_bindings SET send_after_ms=0")
            self.assertIsNone(self.delivery._claim())
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 1)
        with patch("intel.delivery.MAX_ATTEMPT_ROWS", 2, create=True):
            self.assertIsNotNone(self.delivery._claim())

    def join(self, user, username, role="editor"):
        self.store.join_team(user, self.store.invite_member(self.owner, self.team, username, role)["token"])

    def link(self, user, team, update_id, chat_id):
        link = self.delivery.begin_link(user, team)
        self.client.updates = [self.start_update(link["link"].split("start=")[1], update_id=update_id, chat_id=chat_id)]
        self.delivery.run_once()
        self.delivery.confirm_link(user, team, link["generation"])

    def actions(self, team):
        with self.store._connection() as connection:
            return [row[0] for row in connection.execute("SELECT action FROM audit WHERE team_id=? ORDER BY id", (team,))]

    def test_removing_the_linking_member_disconnects_only_their_destination(self):
        other_team = self.store.list_teams(self.other)[0]["id"]
        self.store.add_watch(self.other, other_team, "wrap.near")
        self.link(self.other, other_team, 1, 777)
        self.join(self.other, "other")
        self.store.set_role(self.owner, self.team, self.other, "owner")
        self.link(self.other, self.team, 2, 888)
        third = self.store.register("third", "third-password-123")["user"]["id"]
        self.join(third, "third")
        self.store.remove_member(self.owner, self.team, third)
        self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "active")
        self.event("a")
        self.assertEqual(self.delivery._enqueue() + self.delivery._enqueue(), 2)
        self.store.remove_member(self.owner, self.team, self.other)
        status = self.delivery.status(self.owner, self.team)
        self.assertEqual((status["state"], status["counts"]), ("disconnected", {"cancelled": 1}))
        self.assertEqual(self.actions(self.team)[-2:], ["telegram.disconnected", "member.removed"])
        self.assertEqual(self.delivery.status(self.other, other_team)["state"], "active")
        self.now += 2
        self.delivery.run_once()
        self.assertEqual([payload["chat_id"] for payload in self.sends()], [777])

    def test_demoting_the_linking_owner_disconnects_and_other_role_changes_do_not(self):
        third = self.store.register("third", "third-password-123")["user"]["id"]
        self.join(self.other, "other")
        self.join(third, "third")
        self.store.set_role(self.owner, self.team, self.other, "owner")
        self.link(self.other, self.team, 1, 888)
        self.store.set_role(self.owner, self.team, third, "viewer")
        self.store.set_role(self.owner, self.team, self.other, "owner")
        self.store.set_role(self.other, self.team, self.owner, "editor")
        self.store.set_role(self.other, self.team, self.owner, "owner")
        self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "active")
        self.store.set_role(self.owner, self.team, self.other, "editor")
        self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "disconnected")
        self.assertIn("telegram.disconnected", self.actions(self.team))
        self.store.set_role(self.owner, self.team, self.other, "owner")
        pending = self.delivery.begin_link(self.other, self.team)
        self.store.set_role(self.owner, self.team, self.other, "viewer")
        self.client.updates = [self.start_update(pending["link"].split("start=")[1], update_id=3, chat_id=888)]
        self.delivery.run_once()
        self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "disconnected")
        self.error(409, self.delivery.confirm_link, self.owner, self.team, pending["generation"])
        self.event("a")
        self.delivery.run_once()
        self.assertEqual(self.sends(), [])

    def test_one_team_at_capacity_does_not_pause_another_team(self):
        self.activate()
        other_team = self.store.list_teams(self.other)[0]["id"]
        self.store.add_watch(self.other, other_team, "usdt.tether-token.near")
        self.link(self.other, other_team, 2, 777)
        for identifier in "abc":
            self.event(identifier)
        with patch("intel.delivery.MAX_QUEUE_ROWS", 2), patch("intel.delivery.MAX_ATTEMPT_ROWS", 1):
            self.assertEqual(self.delivery._enqueue() + self.delivery._enqueue(), 2)
            self.assertTrue(self.delivery.status(self.owner, self.team)["capacity"]["queue_paused"])
            self.now += 1
            self.store.ingest_event("f" * 64, {"signal": "account_transfer", "subject": "usdt.tether-token.near", "detail": "Observed transfer",
                                    "observed": {"timestamp_ms": int(self.now * 1000)}}, notify=True)
            self.assertEqual(self.delivery._enqueue(), 1)
            self.assertFalse(self.delivery.status(self.other, other_team)["capacity"]["paused"])
            first = self.delivery._claim()
            self.assertEqual(first["team_id"], self.team)
            self.client.send_results = [TelegramError("transport_unavailable", retryable=True)]
            self.delivery._send(first)
            self.now += 2
            self.assertTrue(self.delivery.status(self.owner, self.team)["capacity"]["attempts_paused"])
            second = self.delivery._claim()
            self.assertEqual(second["team_id"], other_team)
            self.assertIsNone(self.delivery._claim())
            self.assertFalse(self.delivery.status(self.other, other_team)["capacity"]["queue_paused"])

    def test_age_pruning_removes_terminal_rows_and_never_pending_or_sending(self):
        self.activate()
        for identifier in "abcde":
            self.event(identifier)
        self.delivery._enqueue()
        with self.store._connection(write=True) as connection:
            ids = [row[0] for row in connection.execute("SELECT id FROM delivery_queue ORDER BY id")]
            for row_id, state in zip(ids, ("sent", "dead", "cancelled", "pending", "sending")):
                connection.execute("UPDATE delivery_queue SET state=?,lease_until_ms=? WHERE id=?", (state, int(self.now * 1000) + 10 ** 12, row_id))
                connection.execute("INSERT INTO delivery_attempts(queue_id,attempt,started_ms) VALUES(?,1,?)", (row_id, int(self.now * 1000)))
        self.now += 29 * 86400
        self.delivery._enqueue()
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0], 5)
        self.now += 2 * 86400
        self.delivery._enqueue()
        with self.store._connection() as connection:
            self.assertEqual([row[0] for row in connection.execute("SELECT state FROM delivery_queue ORDER BY id")], ["pending", "sending"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 5)

    def test_team_at_capacity_prunes_delivered_rows_before_dead_letters_and_never_pending(self):
        self.activate()
        for identifier in "abcd":
            self.event(identifier)
        self.delivery._enqueue()
        with self.store._connection(write=True) as connection:
            ids = [row[0] for row in connection.execute("SELECT id FROM delivery_queue ORDER BY id")]
            for row_id, state in zip(ids, ("dead", "sent", "cancelled", "pending")):
                connection.execute("UPDATE delivery_queue SET state=? WHERE id=?", (state, row_id))
        with patch("intel.delivery.MAX_QUEUE_ROWS", 4):
            self.event("e")
            self.assertEqual(self.delivery._enqueue(), 1)
            self.event("f")
            self.assertEqual(self.delivery._enqueue(), 1)
            with self.store._connection() as connection:
                self.assertEqual([row[0] for row in connection.execute("SELECT state FROM delivery_queue ORDER BY id")],
                                 ["dead", "pending", "pending", "pending"])
            self.event("9")
            self.assertEqual(self.delivery._enqueue(), 1)
            self.event("8")
            self.assertEqual(self.delivery._enqueue(), 0)
            status = self.delivery.status(self.owner, self.team)
            self.assertEqual((status["counts"], status["capacity"]["queue_paused"]), ({"pending": 4}, True))
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT cursor_id FROM delivery_bindings").fetchone()[0], 7)

    def held_dead_letter(self, held, delivered):
        self.activate()
        if delivered:
            self.event("0")
            self.delivery._enqueue()
            self.delivery._send(self.delivery._claim())
            self.now += 2
        self.event("a")
        self.delivery._enqueue()
        self.client.send_results = [TelegramError("provider_403")]
        row = self.delivery._claim()
        self.delivery._send(row)
        with self.store._connection(write=True) as connection:
            connection.executemany("INSERT INTO delivery_attempts(queue_id,attempt,started_ms,finished_ms,error) VALUES(?,1,0,0,'provider_403')",
                                   [(row["id"],)] * (held - 1))
        self.now += 2
        return row["id"]

    def retained(self):
        with self.store._connection() as connection:
            return ([tuple(row) for row in connection.execute("SELECT * FROM delivery_queue ORDER BY id")],
                    [tuple(row) for row in connection.execute("SELECT queue_id,COUNT(*),MAX(id) FROM delivery_attempts GROUP BY queue_id ORDER BY queue_id")],
                    self.actions(self.team))

    def assert_team_keeps_sending(self, identifiers="bc"):
        for identifier, wait in zip(identifiers, (0, 86400)):
            self.now += wait
            self.event(identifier)
            self.delivery._enqueue()
            row = self.delivery._claim()
            self.assertEqual(row["event_id"], identifier * 64)
            self.assertTrue(self.delivery._send(row))
            self.now += 2

    def retry_detail(self):
        with self.store._connection() as connection:
            return json.loads(connection.execute("SELECT detail_json FROM audit WHERE action='telegram.delivery_retried'").fetchone()[0])

    def test_retry_of_the_only_dead_letter_at_the_attempt_cap_drops_its_old_attempts_and_the_team_keeps_sending(self):
        target = self.held_dead_letter(MAX_ATTEMPT_ROWS, False)
        self.assertEqual(self.delivery.retry_dead(self.owner, self.team, target), {"retried": True})
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 0)
        self.assertEqual(self.retry_detail(), {"pruned": 0, "dropped_attempts": MAX_ATTEMPT_ROWS})
        row = self.delivery._claim()
        self.assertEqual(row["id"], target)
        self.assertTrue(self.delivery._send(row))
        self.now += 2
        self.assert_team_keeps_sending()

    def test_retry_without_room_beside_other_undelivered_rows_is_refused_and_rolls_back(self):
        target = self.held_dead_letter(1, True)
        self.event("b")
        self.delivery._enqueue()
        with self.store._connection(write=True) as connection:
            pending = connection.execute("SELECT id FROM delivery_queue WHERE state='pending'").fetchone()[0]
            connection.executemany("INSERT INTO delivery_attempts(queue_id,attempt,started_ms,finished_ms,error) VALUES(?,1,0,0,'transport_unavailable')",
                                   [(pending,)] * (MAX_ATTEMPT_ROWS - MAX_ATTEMPTS + 1))
        before = self.retained()
        with self.assertRaises(WorkspaceError) as caught:
            self.delivery.retry_dead(self.owner, self.team, target)
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "delivery_capacity"))
        self.assertEqual(self.retained(), before)

    def test_boundary_retry_with_other_queued_alerts_and_retryable_failures_never_stalls_the_team(self):
        target = self.held_dead_letter(MAX_ATTEMPT_ROWS - MAX_ATTEMPTS, True)
        self.assertEqual(self.delivery.retry_dead(self.owner, self.team, target), {"retried": True})
        for identifier in "def123":
            self.event(identifier)
        self.assertEqual(self.delivery._enqueue(), 6)
        claims = 0
        while self.delivery.status(self.owner, self.team)["counts"].get("pending"):
            self.assertLess(claims, 200)
            self.now += 1.1
            with self.store._connection() as connection:
                due = connection.execute("SELECT COUNT(*) FROM delivery_queue WHERE state='pending' AND next_attempt_ms<=?",
                                         (int(self.now * 1000),)).fetchone()[0]
            row = self.delivery._claim()
            self.assertEqual(row is None, due == 0)
            if row is not None:
                claims += 1
                self.client.send_results = [TelegramError("transport_unavailable", retryable=True)]
                self.delivery._send(row)
            self.assertFalse(self.delivery.status(self.owner, self.team)["capacity"]["attempts_paused"])
        self.assertEqual(claims, 7 * MAX_ATTEMPTS)
        self.assertEqual(self.delivery.status(self.owner, self.team)["counts"], {"dead": 7, "sent": 1})
        self.assert_team_keeps_sending()

    def legacy_store(self, name):
        path = Path(self.temp.name) / name
        store = SQLiteWorkspaceStore(path, clock=lambda: self.now)
        owner = store.register("legacy", "legacy-password-123")["user"]["id"]
        member = store.register("member", "member-password-123")["user"]["id"]
        team = store.list_teams(owner)[0]["id"]
        store.add_watch(owner, team, "wrap.near")
        store.join_team(member, store.invite_member(owner, team, "member", "viewer")["token"])
        now = int(self.now * 1000)
        with store._connection(write=True) as connection:
            connection.execute("CREATE TABLE delivery_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL,"
                               "bot_id INTEGER,update_offset INTEGER NOT NULL DEFAULT 0,last_update_ms INTEGER,last_poll_ms INTEGER,last_send_ms INTEGER,"
                               "last_error TEXT,next_poll_ms INTEGER NOT NULL DEFAULT 0,next_send_ms INTEGER NOT NULL DEFAULT 0,"
                               "lease_token TEXT,lease_until_ms INTEGER NOT NULL DEFAULT 0,last_binding INTEGER NOT NULL DEFAULT 0)")
            connection.execute("INSERT INTO delivery_state(singleton,version,bot_id,update_offset) VALUES(1,1,?,5)", (self.client.bot_id,))
            connection.execute("CREATE TABLE delivery_bindings (id INTEGER PRIMARY KEY AUTOINCREMENT,team_id TEXT NOT NULL UNIQUE REFERENCES teams(id) ON DELETE CASCADE,"
                               "generation INTEGER NOT NULL,nonce_hash TEXT UNIQUE,nonce_expires_ms INTEGER,observed_chat_id INTEGER,observed_label TEXT,"
                               "observed_ms INTEGER,active INTEGER NOT NULL DEFAULT 0,active_ms INTEGER,baseline_id INTEGER NOT NULL DEFAULT 0,"
                               "cursor_id INTEGER NOT NULL DEFAULT 0,send_after_ms INTEGER NOT NULL DEFAULT 0,created_ms INTEGER NOT NULL,updated_ms INTEGER NOT NULL)")
            connection.execute("CREATE TABLE delivery_queue (id INTEGER PRIMARY KEY AUTOINCREMENT,binding_id INTEGER NOT NULL REFERENCES delivery_bindings(id) ON DELETE CASCADE,"
                               "team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,generation INTEGER NOT NULL,"
                               "inbox_id INTEGER NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,event_id TEXT NOT NULL,message TEXT NOT NULL,"
                               "state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','dead','cancelled')),attempts INTEGER NOT NULL DEFAULT 0,"
                               "next_attempt_ms INTEGER NOT NULL,lease_token TEXT,lease_until_ms INTEGER NOT NULL DEFAULT 0,last_error TEXT,message_id INTEGER,"
                               "uncertain_ack INTEGER NOT NULL DEFAULT 0,created_ms INTEGER NOT NULL,updated_ms INTEGER NOT NULL,UNIQUE(binding_id,generation,inbox_id))")
            connection.execute("CREATE INDEX delivery_pending ON delivery_queue(state,next_attempt_ms,lease_until_ms)")
            connection.execute("CREATE TABLE delivery_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT,queue_id INTEGER NOT NULL REFERENCES delivery_queue(id) ON DELETE CASCADE,"
                               "attempt INTEGER NOT NULL,started_ms INTEGER NOT NULL,finished_ms INTEGER,error TEXT)")
            connection.execute("INSERT INTO delivery_bindings(team_id,generation,observed_chat_id,observed_label,observed_ms,active,active_ms,created_ms,updated_ms) "
                               "VALUES(?,1,555,'legacy',?,1,?,?,?)", (team, now, now, now, now))
        return store, owner, member, team

    def test_old_schema_database_opens_migrates_and_keeps_rows(self):
        store, owner, member, team = self.legacy_store("old.sqlite3")
        now = int(self.now * 1000)
        for index, state in enumerate(("sent", "dead", "cancelled", "pending")):
            self.now += 1
            store.ingest_event(str(index) * 64, {"signal": "account_transfer", "subject": "wrap.near", "detail": "Observed transfer",
                               "observed": {"timestamp_ms": int(self.now * 1000)}}, notify=True)
            with store._connection(write=True) as connection:
                inbox = connection.execute("SELECT MAX(id) FROM inbox").fetchone()[0]
                connection.execute("INSERT INTO delivery_queue(binding_id,team_id,generation,inbox_id,event_id,message,state,next_attempt_ms,created_ms,updated_ms) "
                                   "VALUES(1,?,1,?,?,'Siyab alert',?,0,?,?)", (team, inbox, str(index) * 64, state, now, now))
                connection.execute("UPDATE delivery_bindings SET cursor_id=?", (inbox,))
        with store._connection() as connection:
            before = [tuple(row) for row in connection.execute("SELECT * FROM delivery_queue ORDER BY id")]
        delivery = TelegramDelivery(store, self.client, clock=lambda: self.now)
        TelegramDelivery(store, self.client, clock=lambda: self.now)
        with store._connection() as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(delivery_bindings)")]
            self.assertEqual(columns.count("user_id"), 1)
            self.assertEqual(connection.execute("SELECT version FROM delivery_state").fetchone()[0], 1)
            self.assertIsNone(connection.execute("SELECT user_id FROM delivery_bindings").fetchone()[0])
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM delivery_queue ORDER BY id")], before)
        self.assertEqual(delivery.status(owner, team)["state"], "active")
        self.now += 2
        self.assertEqual(delivery.run_once()["sent"], 1)
        self.assertEqual(self.sends()[0]["chat_id"], 555)
        store.set_role(owner, team, member, "editor")
        self.assertEqual(delivery.status(owner, team)["state"], "active")
        store.remove_member(owner, team, member)
        self.assertEqual(delivery.status(owner, team)["state"], "disconnected")

    def test_member_removal_before_migration_treats_the_binding_as_legacy(self):
        store, owner, member, team = self.legacy_store("unmigrated.sqlite3")
        store.remove_member(owner, team, member)
        with store._connection() as connection:
            binding = connection.execute("SELECT active,generation,observed_chat_id FROM delivery_bindings").fetchone()
            self.assertEqual(tuple(binding), (0, 2, None))
            self.assertNotIn("user_id", [row[1] for row in connection.execute("PRAGMA table_info(delivery_bindings)")])
            self.assertEqual(connection.execute("SELECT action FROM audit WHERE team_id=? ORDER BY id DESC LIMIT 1 OFFSET 1", (team,)).fetchone()[0], "telegram.disconnected")

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_browser_consumes_actual_backend_status_and_every_delivery_state(self):
        self.activate()
        fixtures = []
        def capture(phase):
            fixtures.append({"phase": phase, "status": self.delivery.status(self.owner, self.team),
                             "log": self.delivery.list_deliveries(self.owner, self.team)})
        self.event("a")
        self.delivery._enqueue()
        capture("pending")
        row = self.delivery._claim()
        capture("sending")
        self.delivery._send(row)
        capture("sent")
        self.event("b")
        self.delivery._enqueue()
        self.client.send_results = [TelegramError("provider_403")]
        self.delivery._send(self.delivery._claim())
        capture("dead")
        with patch.object(TelegramDelivery, "_room", return_value=False):
            capture("capacity")
        self.event("c")
        self.delivery._enqueue()
        self.delivery.revoke(self.owner, self.team)
        capture("cancelled")
        path = Path(self.temp.name) / "backend-responses.json"
        path.write_text(json.dumps(fixtures), encoding="utf-8")
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([shutil.which("node"), "tests/delivery_runtime.cjs", str(path)], cwd=root,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class FakeReply(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())

    def read1(self, size=-1):
        return self.read(size)


class FakeOpener:
    def __init__(self, result):
        self.result, self.requests = result, []

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.result, BaseException):
            raise self.result
        return FakeReply(self.result)


class TelegramClientTest(unittest.TestCase):
    def test_fixed_origin_success_and_missing_credentials(self):
        token = "123456:" + "a" * 35
        opener = FakeOpener({"ok": True, "result": {"id": 1}})
        client = TelegramClient(lambda: token, opener=opener)
        self.assertEqual(client.call("getMe", {}), {"id": 1})
        self.assertEqual(opener.requests[0].full_url, "https://api.telegram.org/bot" + token + "/getMe")
        self.assertFalse(TelegramClient().configuration()["configured"])
        with self.assertRaises(ValueError):
            client.call("https://evil.test/", {})

    def test_errors_do_not_expose_token_or_provider_description(self):
        token = "123456:" + "a" * 35
        for response in ({"ok": False, "error_code": 429, "description": token, "parameters": {"retry_after": 999999}},
                         URLError("transport failed " + token),
                         HTTPError("https://api.telegram.org/bot" + token, 302, token, {}, io.BytesIO())):
            with self.subTest(kind=type(response).__name__):
                client = TelegramClient(lambda: token, opener=FakeOpener(response))
                with self.assertRaises(TelegramError) as caught:
                    client.call("sendMessage", {"chat_id": 1, "text": "test"})
                self.assertNotIn(token, str(caught.exception))
                self.assertLessEqual(caught.exception.retry_after or 0, 3600)

    def test_invalid_encoding_in_external_file_is_redacted_unconfigured(self):
        def unreadable():
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        self.assertEqual(TelegramClient(unreadable).configuration(), {"configured": False, "reason": "credentials_unavailable"})

    def test_operator_factory_never_exposes_missing_file_path(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(configured_client().configuration()["reason"], "credentials_missing")
        with patch.dict("os.environ", {"SIYAB_TELEGRAM_TOKEN_FILE": "operator-owned-path"}), patch("pathlib.Path.open", side_effect=FileNotFoundError("private path")):
            self.assertEqual(configured_client().configuration(), {"configured": False, "reason": "credentials_unavailable"})

    def test_valid_token_is_pinned_until_worker_restart(self):
        values = iter(["123456:" + "a" * 35, "123456:" + "b" * 35])
        opener = FakeOpener({"ok": True, "result": {"id": 1}})
        client = TelegramClient(lambda: next(values), opener=opener)
        client.call("getMe", {})
        client.call("getMe", {})
        self.assertEqual(opener.requests[0].full_url, opener.requests[1].full_url)


if __name__ == "__main__":
    unittest.main()
