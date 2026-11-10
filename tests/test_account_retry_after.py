import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from email.message import Message
from email.utils import format_datetime
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from gate import rpc
from intel import accounts, history, investigation_jobs, labels
from intel.workspace import SQLiteWorkspaceStore


class AccountRetryAfterTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1794325863.25
        self.archive = history.AccountHistory(self.root / "history.sqlite3", clock=lambda: self.now)
        self.jobs = investigation_jobs.InvestigationJobs(self.root / "jobs.sqlite3", self.archive, clock=lambda: self.now)
        environment = mock.patch.dict("os.environ", {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.reader = accounts.AccountData(labels.Store([], "a" * 64), clock=lambda: self.now)

    def upstream(self, status, retry_after):
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        return mock.patch.object(rpc, "_open_with_deadline", side_effect=HTTPError("https://private.invalid/?token=never-retain", status, "private provider description", headers, io.BytesIO(b"private body")))

    def test_real_transaction_and_transfer_adapters_keep_only_sanitized_cooldown_metadata(self):
        for kind in ("transactions", "transfers"):
            for status in (429, 500):
                with self.subTest(kind=kind, status=status), self.upstream(status, "3600"), self.assertRaises(accounts.AccountReadError) as caught:
                    self.reader.history_page("alice.near", kind)
                self.assertEqual(getattr(caught.exception, "status", None), status)
                self.assertEqual(getattr(caught.exception, "retry_at_ms", None), int(self.now * 1000) + 3600000)
                self.assertNotIn("private", str(caught.exception) + json.dumps(vars(caught.exception)))

    def test_http_date_and_invalid_headers_are_safe(self):
        until = int(self.now) + 7200
        cases = [(format_datetime(datetime.fromtimestamp(until, UTC), usegmt=True), until * 1000),
                 ("0", int(self.now * 1000)), ("-1", None), ("1.5", None), ("NaN", None),
                 ("3600, 120", None), ("not a date", None), (None, None)]
        for value, expected in cases:
            with self.subTest(value=value), self.upstream(429, value), self.assertRaises(accounts.AccountReadError) as caught:
                self.reader.transfers("alice.near")
            self.assertEqual(getattr(caught.exception, "retry_at_ms", None), expected)

    def test_job_cooldown_survives_restart_and_manual_retry_cannot_shorten_it(self):
        job = self.jobs.create("team", "actor", "alice.near")
        with self.upstream(429, "3600") as opened:
            self.jobs.tick(self.reader)
            expected = int(self.now * 1000) + 3600000
            self.assertEqual(self.jobs.get("team", job["id"])["retry_at_ms"], expected)
            restarted = investigation_jobs.InvestigationJobs(self.root / "jobs.sqlite3", self.archive, clock=lambda: self.now)
            restarted.retry("team", job["id"])
            self.now += 3599
            self.assertEqual(restarted.tick(self.reader)["processed"], 0)
            self.assertEqual(opened.call_count, 1)
            self.now += 1
            restarted.tick(self.reader)
            self.assertEqual(opened.call_count, 2)

    def test_another_job_and_public_backfill_cannot_evade_account_source_cooldown(self):
        first = self.jobs.create("team", "actor", "alice.near")
        with self.upstream(429, "3600") as opened:
            self.jobs.tick(self.reader)
            self.jobs.cancel("team", first["id"])
            self.now += 61
            self.jobs.create("another-team", "another-actor", "alice.near")
            self.jobs.tick(self.reader)
            with self.assertRaises(history.HistoryCapacityError):
                self.archive.backfill_account(self.reader, "alice.near", "transfers", max_pages=1)
            self.assertEqual(opened.call_count, 1)

    def test_cooldowns_do_not_spread_to_other_accounts_or_other_history_sources(self):
        until = int(self.now * 1000) + 3600000
        self.archive.defer_account("alice.near", "transfers", until)
        with self.assertRaises(history.HistoryCapacityError) as caught:
            self.archive.claim_job_read("alice.near", "transfers")
        self.assertEqual(caught.exception.retry_at_ms, until)
        token = self.archive.claim_job_read("alice.near", "transactions")
        self.archive.release_job_read("alice.near", token)
        token = self.archive.claim_job_read("bob.near", "transfers")
        self.archive.release_job_read("bob.near", token)

    def test_public_backfill_cooldown_blocks_new_jobs_without_another_provider_call(self):
        with self.upstream(500, "600") as opened:
            until = int(self.now * 1000) + 600000
            with self.assertRaises(accounts.AccountReadError):
                self.archive.backfill_account(self.reader, "alice.near", "transfers", max_pages=1)
            self.now += 61
            job = self.jobs.create("team", "actor", "alice.near")
            self.jobs.tick(self.reader)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(self.jobs.get("team", job["id"])["retry_at_ms"], until)

    def test_both_job_kinds_persist_http_date_cooldowns_and_sanitized_status(self):
        until = int(self.now) + 7200
        header = format_datetime(datetime.fromtimestamp(until, UTC), usegmt=True)
        for index, (kind, status) in enumerate((("transfers", 429), ("transfers", 500), ("transactions", 429), ("transactions", 500))):
            with self.subTest(kind=kind, status=status):
                job = self.jobs.create("team", "actor", "account" + str(index) + ".near", kind=kind)
                with self.upstream(status, header):
                    self.jobs.tick(self.reader)
                result = self.jobs.get("team", job["id"])
                self.assertEqual(result["retry_at_ms"], until * 1000)
                self.assertEqual(result["last_error"], "provider_http_" + str(status))

    def test_history_v2_optional_cooldown_extension_keeps_base_schema_and_observations(self):
        from tests.test_history import page, transfer

        self.archive.record_page("alice.near", "transfers", page([transfer(1)]))
        with closing(sqlite3.connect(self.root / "history.sqlite3")) as connection, connection:
            connection.execute("DROP TABLE provider_cooldowns")
            connection.execute("PRAGMA user_version=2")
        upgraded = history.AccountHistory(self.root / "history.sqlite3", clock=lambda: self.now)
        self.assertEqual(len(upgraded.list_records("alice.near", "transfers")["items"]), 1)
        with upgraded._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM provider_cooldowns").fetchone()[0], 0)

    def test_provider_backoff_failures_remain_bounded_and_release_leases(self):
        job = self.jobs.create("team", "actor", "alice.near", kind="transactions")
        with self.upstream(500, "invalid") as opened:
            for attempt, delay in ((1, 60), (2, 120), (3, 240)):
                start = int(self.now * 1000)
                self.jobs.tick(self.reader)
                current = self.jobs.get("team", job["id"])
                self.assertEqual(current["attempts"], attempt)
                self.assertEqual(current["retry_at_ms"], start + delay * 1000)
                self.now += delay
            self.assertEqual(self.jobs.get("team", job["id"])["state"], "failed")
            self.assertEqual(self.jobs.tick(self.reader)["processed"], 0)
            self.assertEqual(opened.call_count, 3)
        with self.archive._connection() as connection:
            self.assertEqual(connection.execute("SELECT lease_until_ms FROM backfill_control WHERE account='alice.near'").fetchone()[0], 0)

    def test_watch_poll_cooldown_survives_restart_and_rewatch(self):
        workspace = SQLiteWorkspaceStore(self.root / "workspace.sqlite3", clock=lambda: self.now)
        owner = workspace.register("owner", "test-password-123")["user"]["id"]
        team = workspace.list_teams(owner)[0]["id"]
        workspace.add_watch(owner, team, "alice.near")
        with self.upstream(429, "3600") as opened:
            self.archive.poll_watches(workspace, self.reader)
            workspace.remove_watch(owner, team, "alice.near")
            self.now += 6
            self.archive.poll_watches(workspace, self.reader)
            workspace.add_watch(owner, team, "alice.near")
            restarted = history.AccountHistory(self.root / "history.sqlite3", clock=lambda: self.now)
            self.now += 120
            restarted.poll_watches(workspace, self.reader)
            self.assertEqual(opened.call_count, 1)

    def test_jobs_v1_migration_preserves_existing_records_and_adds_cooldown(self):
        job = self.jobs.create("team", "actor", "alice.near")
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as connection, connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "provider_retry_ms" in columns:
                connection.execute("ALTER TABLE jobs DROP COLUMN provider_retry_ms")
            connection.execute("PRAGMA user_version=1")
        restarted = investigation_jobs.InvestigationJobs(self.root / "jobs.sqlite3", self.archive)
        self.assertEqual(restarted.get("team", job["id"])["account_id"], "alice.near")
        with restarted._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT provider_retry_ms FROM jobs").fetchone()[0], 0)
