import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from email.message import Message
from unittest import mock

from gate import canon
from intel import history, investigation_jobs, live
from intel.accounts import AccountReadError
from intel.workspace import SQLiteWorkspaceStore
from intel.workspace_api import WorkspaceAPI
from tests.test_history import Reader, later, page, transfer

PREVIOUS_SCHEMA = """
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,team TEXT NOT NULL,actor TEXT NOT NULL,request TEXT NOT NULL,progress TEXT NOT NULL,state TEXT NOT NULL,created_ms INTEGER NOT NULL,updated_ms INTEGER NOT NULL,not_before_ms INTEGER NOT NULL,lease_token TEXT,lease_until_ms INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT);
    CREATE INDEX IF NOT EXISTS team_jobs ON jobs(team,created_ms DESC,id);
    CREATE INDEX IF NOT EXISTS due_jobs ON jobs(state,not_before_ms,updated_ms);
    CREATE TABLE IF NOT EXISTS job_items(seq INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT NOT NULL REFERENCES jobs(id),row_id TEXT NOT NULL,body TEXT NOT NULL,page_id TEXT NOT NULL,UNIQUE(job,row_id));
    CREATE TABLE IF NOT EXISTS job_reads(seq INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT NOT NULL REFERENCES jobs(id),body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS job_usage(singleton INTEGER PRIMARY KEY CHECK(singleton=1),bytes INTEGER NOT NULL);
    INSERT OR IGNORE INTO job_usage VALUES(1,0);
    CREATE TABLE IF NOT EXISTS job_worker(singleton INTEGER PRIMARY KEY CHECK(singleton=1),mode TEXT,heartbeat_ms INTEGER,stopped_ms INTEGER);
    INSERT OR IGNORE INTO job_worker(singleton) VALUES(1);
    COMMIT;
"""

ROW_BYTES = ("SELECT SUM(LENGTH(CAST(request AS BLOB))+LENGTH(CAST(progress AS BLOB))+512+(SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))+256),0) FROM job_items WHERE job=jobs.id)"
             "+(SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))),0) FROM job_reads WHERE job=jobs.id)) FROM jobs")


class HistoricalReader(Reader):
    def history_page(self, account, kind, **kwargs):
        return self.transfers(account, cursor=kwargs.get("cursor"), limit=kwargs["limit"])


class InvestigationJobsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 2000.0
        self.archive = history.AccountHistory(self.root / "history.sqlite3", clock=lambda: self.now)
        self.jobs = investigation_jobs.InvestigationJobs(self.root / "jobs.sqlite3", self.archive, clock=lambda: self.now)
        self.reader = HistoricalReader()

    def test_multitick_history_resumes_after_restart_and_retains_source_pages(self):
        self.reader.pages["alice.near", None] = page([transfer(1)], cursor="opaque+/=")
        self.reader.pages["alice.near", "opaque+/="] = page([transfer(2)])
        job = self.jobs.create("team", "actor", "alice.near", max_pages=5, max_rows=10)
        self.jobs.tick(self.reader)
        current = self.jobs.get("team", job["id"])
        self.assertEqual(current["counts"]["pages"], 1)
        self.assertEqual(current["state"], "queued")
        self.now += 61
        restarted = investigation_jobs.InvestigationJobs(self.root / "jobs.sqlite3", self.archive, clock=lambda: self.now)
        restarted.tick(self.reader)
        result = restarted.result("team", job["id"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(len(result["observations"]), 2)
        self.assertEqual(self.reader.calls[-1][1], "opaque+/=")
        self.assertEqual(restarted.get("team", job["id"])["state"], "completed")
        self.assertFalse(result["coverage"]["history_complete"])

    def test_team_isolation_cancellation_and_retry_preserve_progress(self):
        self.reader.pages["alice.near", None] = AccountReadError("private provider text")
        job = self.jobs.create("team", "actor", "alice.near")
        with self.assertRaises(investigation_jobs.JobNotFound):
            self.jobs.get("another-team", job["id"])
        self.jobs.tick(self.reader)
        failed = self.jobs.get("team", job["id"])
        self.assertEqual(failed["state"], "retry_wait")
        self.assertNotIn("private provider text", str(failed))
        self.jobs.cancel("team", job["id"])
        self.now += 61
        self.assertEqual(self.jobs.tick(self.reader)["processed"], 0)
        self.reader.pages["alice.near", None] = page([])
        self.jobs.retry("team", job["id"])
        self.jobs.tick(self.reader)
        self.assertEqual(self.jobs.get("team", job["id"])["state"], "completed")

    def test_graph_expands_beyond_two_hops_without_publishing_private_metadata(self):
        chain = ["alice.near", "bob.near", "carol.near", "dave.near"]
        for index, account in enumerate(chain[:-1]):
            self.reader.pages[account, None] = page([transfer(index, subject=account, other=chain[index + 1])], account=account)
        job = self.jobs.create("private-team", "private-actor", chain[0], kind="graph", depth=3, max_accounts=10)
        for _ in range(3):
            self.jobs.tick(self.reader)
            self.now += 61
        result = self.jobs.result("private-team", job["id"])
        graph = result["graph"]
        self.assertEqual(graph["bounds"]["reached_depth"], 3)
        self.assertEqual([node["id"] for node in graph["nodes"]], chain)
        self.assertNotIn("private-team", str(graph))
        self.assertNotIn("private-actor", str(graph))
        self.assertTrue(graph["coverage"]["truncated"])

    def test_private_graph_result_does_not_create_an_anonymous_immutable_snapshot(self):
        self.reader.pages["alice.near", None] = page([transfer(1)])
        job = self.jobs.create("private-team", "actor", "alice.near", kind="graph", depth=1,
                               from_timestamp_ms=500, to_timestamp_ms=1500000)
        self.jobs.tick(self.reader)
        with self.archive._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM graphs").fetchone()[0], 0)
        result = self.jobs.result("private-team", job["id"])
        self.assertIsNone(result["snapshot"])
        graph = result["graph"]
        self.assertEqual(graph["root"], "alice.near")
        self.assertEqual(graph["bounds"]["window"], {"from_timestamp_ms": 500, "to_timestamp_ms": 1500000})
        digest = hashlib.sha256(history._canonical(graph)).hexdigest()
        anonymous = live.LiveAPI(history_store=self.archive)
        self.assertEqual(anonymous.handle("/live/account-data/" + digest + ".json", "203.0.113.9", {})[0], 404)
        with self.assertRaises(investigation_jobs.JobNotFound):
            self.jobs.result("other-team", job["id"])
        restarted = investigation_jobs.InvestigationJobs(self.root / "jobs.sqlite3", self.archive)
        self.assertEqual(restarted.result("private-team", job["id"])["graph"], graph)
        with restarted._connection() as connection:
            progress = json.loads(connection.execute("SELECT progress FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])
            size = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
        self.assertEqual(progress["graph"], graph)
        self.assertGreaterEqual(size, len(json.dumps(graph).encode()))

    def test_cancelled_graph_does_not_publish_or_return_a_graph(self):
        job = self.jobs.create("private-team", "actor", "alice.near", kind="graph")

        class Reader:
            def history_page(inner, account, kind, **kwargs):
                self.jobs.cancel("private-team", job["id"])
                return page([transfer(1)])

        self.jobs.tick(Reader())
        self.assertIsNone(self.jobs.result("private-team", job["id"])["graph"])
        with self.archive._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM graphs").fetchone()[0], 0)

    @unittest.skipUnless(shutil.which("node"), "Node is required for the graph download boundary test")
    def test_authorized_private_graph_download_preserves_exact_requested_bounds(self):
        self.reader.pages["alice.near", None] = page([transfer(1)])
        job = self.jobs.create("private-team", "actor", "alice.near", kind="graph", depth=1,
                               from_timestamp_ms=500, to_timestamp_ms=1500000)
        self.jobs.tick(self.reader)
        team = {"id": "private-team", "name": "Private workspace", "role": "owner"}
        fixture = {"team": team, "session": {"authenticated": True, "user": {"id": "actor"}, "teams": [team], "csrf_token": "test-csrf-token"},
                   "completed": self.jobs.get("private-team", job["id"]), "result": self.jobs.result("private-team", job["id"])}
        checked = subprocess.run([shutil.which("node"), "tests/investigation_jobs_runtime.cjs", "private-graph-download"], input=json.dumps(fixture), text=True, capture_output=True, timeout=20)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_private_graph_result_requires_current_workspace_team_membership(self):
        workspace = SQLiteWorkspaceStore(self.root / "workspace.sqlite3")
        owner = workspace.register("owner", "test-password-123")
        stranger = workspace.register("stranger", "test-password-123")
        team = workspace.list_teams(owner["user"]["id"])[0]["id"]
        job = self.jobs.create(team, owner["user"]["id"], "alice.near", kind="graph", depth=1)
        self.reader.pages["alice.near", None] = page([transfer(1)])
        self.jobs.tick(self.reader)
        api = WorkspaceAPI(workspace, investigation_jobs=self.jobs)
        path = "/workspace-api/teams/" + team + "/investigation-jobs/" + job["id"] + "/result"

        def request(session=None):
            headers = Message()
            if session:
                headers["Cookie"] = "__Host-siyab-session=" + session["session_token"]
            return api.handle("GET", path, "203.0.113.9", headers)

        self.assertEqual(request()[0], 401)
        self.assertEqual(request(stranger)[0], 403)
        status, body, _ = request(owner)
        self.assertEqual(status, 200)
        self.assertEqual(body["graph"]["root"], "alice.near")
        self.assertIsNone(body["snapshot"])
        with self.archive._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM graphs").fetchone()[0], 0)
            public = [json.loads(row[0]) for row in connection.execute("SELECT body FROM pages")]
        self.assertNotIn(team, json.dumps(public))
        self.assertNotIn(owner["user"]["id"], json.dumps(public))

    def test_date_and_row_bounds_are_explicit_and_do_not_claim_provider_completeness(self):
        self.reader.pages["alice.near", None] = page([transfer(1, timestamp="999000000000"), transfer(2, timestamp="998000000000")], cursor="more")
        job = self.jobs.create("team", "actor", "alice.near", from_timestamp_ms=998000, to_timestamp_ms=1000000, max_rows=1)
        self.jobs.tick(self.reader)
        result = self.jobs.result("team", job["id"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["coverage"]["stop_reason"], "row_limit")
        self.assertFalse(result["coverage"]["history_complete"])
        self.assertEqual(self.jobs.get("team", job["id"])["bounds"]["from_timestamp_ms"], 998000)

    def test_invalid_unbounded_requests_are_rejected(self):
        for kwargs in ({"max_pages": 101}, {"max_rows": 5001}, {"kind": "graph", "depth": 6},
                       {"kind": "graph", "max_rows": 201}, {"from_timestamp_ms": 20, "to_timestamp_ms": 10}):
            with self.subTest(kwargs=kwargs), self.assertRaises(investigation_jobs.JobInputError):
                self.jobs.create("team", "actor", "alice.near", **kwargs)

    def test_running_lease_blocks_another_worker_and_cancellation_during_fetch_wins(self):
        job = self.jobs.create("team", "actor", "alice.near")
        restarted = investigation_jobs.InvestigationJobs(self.root / "jobs.sqlite3", self.archive, clock=lambda: self.now)

        class Reader:
            def history_page(inner, account, kind, **kwargs):
                self.assertEqual(restarted.tick(inner)["processed"], 0)
                restarted.cancel("team", job["id"])
                return page([transfer(1)])

        self.jobs.tick(Reader())
        self.assertEqual(self.jobs.get("team", job["id"])["state"], "cancelled")
        self.assertEqual(self.jobs.result("team", job["id"])["items"], [])

    def test_stale_lease_can_resume_without_resetting_a_committed_cursor(self):
        self.reader.pages["alice.near", None] = page([transfer(1)], cursor="resume")
        self.reader.pages["alice.near", "resume"] = page([transfer(2)])
        job = self.jobs.create("team", "actor", "alice.near")
        self.jobs.tick(self.reader)
        with self.jobs._connection(write=True) as connection:
            connection.execute("UPDATE jobs SET state='running',lease_token='stale',lease_until_ms=? WHERE id=?", (self.jobs._now() + 45000, job["id"]))
        self.now += 61
        self.jobs.tick(self.reader)
        self.assertEqual(self.reader.calls[-1][1], "resume")
        self.assertEqual(len(self.jobs.result("team", job["id"])["items"]), 2)

    def due_watches(self, count, *, heartbeat_age_s=0):
        with self.archive._connection(write=True) as connection:
            for number in range(count):
                connection.execute("INSERT OR REPLACE INTO watch_poll(account,next_due_ms) VALUES(?,0)", ("watched" + str(number) + ".near",))
            connection.execute("UPDATE history_state SET heartbeat_ms=? WHERE singleton=1", (self.archive._now() - heartbeat_age_s * 1000,))

    def test_history_budget_waits_without_spending_failure_retries_and_yields_to_watch(self):
        self.reader.pages["alice.near", None] = page([])
        job = self.jobs.create("team", "actor", "alice.near")
        self.due_watches(1)
        self.jobs.tick(self.reader)
        current = self.jobs.get("team", job["id"])
        self.assertEqual(current["attempts"], 0)
        self.assertEqual(current["last_error"], "watch_priority")
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(current["retry_at_ms"], self.jobs._now() + 10000)
        with self.archive._connection(write=True) as connection:
            connection.execute("DELETE FROM watch_poll")
            connection.execute("UPDATE job_read_budget SET window=?,pages=8 WHERE singleton=1", ((self.archive._now() + 10000) // 60000,))
        self.now += 10
        self.jobs.tick(self.reader)
        current = self.jobs.get("team", job["id"])
        self.assertEqual((current["attempts"], current["last_error"]), (0, "history_capacity"))
        self.assertEqual(current["retry_at_ms"], self.jobs._now() + 60000)
        for _ in range(investigation_jobs.MAX_CAPACITY_WAITS):
            self.now += 60
            with self.archive._connection(write=True) as connection:
                connection.execute("UPDATE job_read_budget SET window=?,pages=8 WHERE singleton=1", (self.archive._now() // 60000,))
            self.jobs.tick(self.reader)
            current = self.jobs.get("team", job["id"])
            self.assertEqual((current["state"], current["attempts"], current["last_error"]), ("queued", 0, "history_capacity"))
        with self.jobs._connection() as connection:
            self.assertEqual(connection.execute("SELECT capacity_waits FROM jobs WHERE id=?", (job["id"],)).fetchone()[0], 0)

    def test_job_service_records_a_tampered_retained_page_and_keeps_running(self):
        self.archive.record_page("alice.near", "transfers", page([transfer(1)]))
        with self.archive._connection(write=True) as connection:
            connection.execute("UPDATE pages SET body=?", (b"null",))
        self.reader.pages["alice.near", None] = page([transfer(1)])
        job = self.jobs.create("team", "actor", "alice.near")
        waits = []

        class Stop:
            def wait(inner, interval):
                waits.append(self.jobs.get("team", job["id"]))
                return len(waits) == 2

        investigation_jobs.run(self.root / "jobs.sqlite3", self.archive, self.reader, stop_event=Stop())
        self.assertEqual(len(waits), 2)
        self.assertEqual(len(self.reader.calls), 1)
        current = self.jobs.get("team", job["id"])
        self.assertEqual((current["state"], current["attempts"], current["last_error"]), ("retry_wait", 1, "HistoryReadError"))
        with self.jobs._connection() as connection:
            self.assertEqual(tuple(connection.execute("SELECT lease_token,lease_until_ms FROM jobs").fetchone()), (None, 0))
        with self.archive._connection() as connection:
            self.assertEqual(tuple(connection.execute("SELECT lease_token,lease_until_ms FROM backfill_control WHERE kind='job'").fetchone()), (None, 0))

    def test_job_pages_use_the_archive_share_reserved_from_anonymous_reads(self):
        self.reader.pages["alice.near", None] = page([transfer(1)])
        job = self.jobs.create("team", "actor", "alice.near")
        with mock.patch.object(history, "MAX_ARCHIVE_PAGES", 4):
            for number in range(3):
                account = "public" + str(number) + ".near"
                self.archive.record_page(account, "transfers", page([], account=account))
            with self.assertRaises(history.HistoryCapacityError):
                self.archive.record_page("public3.near", "transfers", page([], account="public3.near"))
            self.jobs.tick(self.reader)
        current = self.jobs.get("team", job["id"])
        self.assertEqual(current["counts"]["pages"], 1)
        self.assertIsNone(current["last_error"])
        self.assertEqual(len(self.jobs.result("team", job["id"])["items"]), 1)

    def test_a_job_over_retained_rows_cites_the_retained_page_and_its_provenance(self):
        retained = page([transfer(2), transfer(1)])
        saved = self.archive.record_page("alice.near", "transfers", retained)
        self.archive.record_page("alice.near", "transfers", page([transfer(3), transfer(2)]))
        fresh = later(page([transfer(2), transfer(1)]), "2026-11-10T00:05:00Z")
        fresh["source"]["request_sha256"] = fresh["items"][0]["source"]["request_sha256"] = "b" * 64
        self.reader.pages["alice.near", None] = fresh
        job = self.jobs.create("team", "actor", "alice.near")
        self.jobs.tick(self.reader)
        result = self.jobs.result("team", job["id"])
        with self.archive._connection() as connection:
            bodies = {row["id"]: json.loads(row["body"]) for row in connection.execute("SELECT id,body FROM pages")}
        self.assertEqual(len(bodies), 2)
        self.assertEqual(result["observations"], [{"page_id": saved["page_id"], "account_id": "alice.near",
                                                   "source": retained["source"], "observed_at": retained["observed_at"]}])
        self.assertEqual(result["items"], retained["items"])
        with self.jobs._connection() as connection:
            cited = [(json.loads(row["body"]), row["page_id"]) for row in connection.execute("SELECT body,page_id FROM job_items ORDER BY seq")]
        for item, page_id in cited:
            self.assertIn(item, bodies[page_id]["items"])
        for observation in result["observations"]:
            self.assertEqual((observation["source"], observation["observed_at"]),
                             (bodies[observation["page_id"]]["source"], bodies[observation["page_id"]]["observed_at"]))

    def test_page_limit_date_cutoff_and_result_cursors_are_truthful(self):
        self.reader.pages["alice.near", None] = page([transfer(1, timestamp="999000000000"), transfer(2, timestamp="998000000000")], cursor="more")
        job = self.jobs.create("team", "actor", "alice.near", max_pages=1)
        self.jobs.tick(self.reader)
        result = self.jobs.result("team", job["id"], limit=1)
        self.assertEqual(result["coverage"]["stop_reason"], "page_limit")
        self.assertTrue(result["coverage"]["more_provider_pages"])
        following = self.jobs.result("team", job["id"], before=result["pagination"]["next_before"], limit=1)
        self.assertNotEqual(result["items"][0]["id"], following["items"][0]["id"])
        another = self.jobs.create("team", "actor", "bob.near")
        with self.assertRaises(investigation_jobs.JobInputError):
            self.jobs.result("team", another["id"], before=result["pagination"]["next_before"])

    def test_retention_refuses_new_jobs_without_deleting_results(self):
        job = self.jobs.create("team", "actor", "alice.near")
        with mock.patch.object(investigation_jobs, "MAX_TEAM_JOBS", 1), self.assertRaises(investigation_jobs.JobCapacityError):
            self.jobs.create("team", "actor", "bob.near")
        self.assertEqual(self.jobs.get("team", job["id"])["state"], "queued")

    def test_dated_transactions_scan_newer_pages_then_stop_at_the_lower_date_bound(self):
        self.reader.pages["alice.near", None] = page([{"transaction_hash": "new", "tx_block_timestamp": "1500000000"}], cursor="older")
        self.reader.pages["alice.near", "older"] = page([
            {"transaction_hash": "included", "tx_block_timestamp": "999999999"},
            {"transaction_hash": "old", "tx_block_timestamp": "499999999"}], cursor="still-older")
        job = self.jobs.create("team", "actor", "alice.near", kind="transactions", from_timestamp_ms=500, to_timestamp_ms=1000)
        self.jobs.tick(self.reader)
        self.assertEqual(self.jobs.result("team", job["id"])["items"], [])
        self.now += 61
        self.jobs.tick(self.reader)
        result = self.jobs.result("team", job["id"])
        self.assertEqual([item["transaction_hash"] for item in result["items"]], ["included"])
        self.assertEqual(result["coverage"]["stop_reason"], "date_window_reached")
        self.assertFalse(result["coverage"]["history_complete"])

    def test_graph_page_window_is_persistent_and_mirrored_transfers_are_deduplicated(self):
        self.reader.pages["alice.near", None] = page([transfer(1)], cursor="root-second")
        self.reader.pages["alice.near", "root-second"] = page([transfer(2, other="carol.near")], cursor="outside-window")
        self.reader.pages["bob.near", None] = page([transfer(1, subject="bob.near", other="alice.near", outgoing=False)], account="bob.near")
        self.reader.pages["carol.near", None] = page([], account="carol.near")
        job = self.jobs.create("team", "actor", "alice.near", kind="graph", pages_per_account=2)
        for _ in range(4):
            self.jobs.tick(self.reader)
            self.now += 61
        result = self.jobs.result("team", job["id"])
        graph = result["graph"]
        self.assertEqual(graph["total_records"], 2)
        self.assertEqual(graph["coverage"]["accounts_with_unread_pages"], ["alice.near"])
        self.assertNotIn(("alice.near", "outside-window", 100), self.reader.calls)
        self.assertEqual(graph["coverage"]["filters"]["pages_per_account"], 2)

    def test_worker_loop_reports_liveness_and_marks_clean_shutdown(self):
        jobs_path = self.root / "service-jobs.sqlite3"

        class Stop:
            def wait(inner, interval):
                self.assertEqual(interval, 5)
                service = investigation_jobs.InvestigationJobs(jobs_path, self.archive)
                self.assertTrue(service.status()["running"])
                self.assertNotIn("team", service.status())
                return True

        status = investigation_jobs.run(jobs_path, self.archive, self.reader, stop_event=Stop())
        self.assertFalse(status["running"])
        self.assertIsNotNone(status["heartbeat_ms"])

    def test_thirteen_due_watches_hold_a_job_for_at_most_three_short_pauses(self):
        self.reader.pages["alice.near", None] = page([transfer(1)], cursor="next")
        job = self.jobs.create("team", "actor", "alice.near")
        for _ in range(investigation_jobs.MAX_WATCH_YIELDS):
            self.due_watches(13)
            self.jobs.tick(self.reader)
            current = self.jobs.get("team", job["id"])
            self.assertEqual((current["state"], current["attempts"], current["last_error"]), ("queued", 0, "watch_priority"))
            self.assertEqual(current["retry_at_ms"], self.jobs._now() + 10000)
            self.now += 10
        self.due_watches(13)
        self.jobs.tick(self.reader)
        self.assertEqual(self.reader.calls, [("alice.near", None, 100)])
        current = self.jobs.get("team", job["id"])
        self.assertEqual((current["state"], current["counts"]["pages"], current["last_error"]), ("queued", 1, None))
        with self.jobs._connection() as connection:
            self.assertEqual(connection.execute("SELECT watch_yields FROM jobs").fetchone()[0], 0)
        self.now += 61
        self.due_watches(13)
        self.jobs.tick(self.reader)
        self.assertEqual(self.jobs.get("team", job["id"])["last_error"], "watch_priority")
        self.assertEqual(len(self.reader.calls), 1)

    def test_watch_priority_does_not_yield_between_global_watch_ticks(self):
        self.reader.pages["alice.near", None] = page([])
        job = self.jobs.create("team", "actor", "alice.near")
        self.due_watches(3)
        with self.archive._connection(write=True) as connection:
            connection.execute("UPDATE history_state SET last_tick_ms=? WHERE singleton=1", (self.archive._now(),))
        self.jobs.tick(self.reader)
        self.assertEqual(self.reader.calls, [("alice.near", None, 100)])
        self.assertEqual(self.jobs.get("team", job["id"])["state"], "completed")

    def test_a_stale_history_heartbeat_does_not_hold_jobs_behind_watch_rows(self):
        self.reader.pages["alice.near", None] = page([])
        job = self.jobs.create("team", "actor", "alice.near")
        self.due_watches(13, heartbeat_age_s=31)
        self.jobs.tick(self.reader)
        self.assertEqual(self.reader.calls, [("alice.near", None, 100)])
        self.assertEqual(self.jobs.get("team", job["id"])["state"], "completed")
        with self.archive._connection(write=True) as connection:
            connection.execute("UPDATE history_state SET heartbeat_ms=NULL WHERE singleton=1")
        self.assertIsInstance(self.archive.claim_job_read("bob.near"), str)

    def test_a_new_account_job_still_reads_when_idle_checkpoints_fill_the_table(self):
        stamp = int(self.now * 1000) - history.ACCOUNT_INTERVAL_MS
        with self.archive._connection(write=True) as connection:
            connection.executemany("INSERT INTO backfill_control VALUES (?,'job',?,NULL,0)", [("idle" + str(number) + ".near", stamp) for number in range(20000)])
        self.reader.pages["alice.near", None] = page([transfer(1)])
        job = self.jobs.create("new-team", "actor", "alice.near")
        self.jobs.tick(self.reader)
        self.assertEqual(self.reader.calls, [("alice.near", None, 100)])
        self.assertEqual(self.jobs.get("new-team", job["id"])["state"], "completed")
        with self.archive._connection() as connection:
            self.assertEqual([row[0] for row in connection.execute("SELECT account FROM backfill_control")], ["alice.near"])

    def test_anonymous_backfill_and_jobs_never_spend_each_others_page_share(self):
        for number in range(6):
            account = "public" + str(number) + ".near"
            self.reader.pages[account, None] = page([], account=account)
        for number in range(8):
            account = "job" + str(number) + ".near"
            self.archive.release_job_read(account, self.archive.claim_job_read(account))
        with self.assertRaises(history.HistoryCapacityError):
            self.archive.claim_job_read("joblast.near")
        for number in range(2):
            self.archive.backfill_account(self.reader, "public" + str(number) + ".near", "transfers", max_pages=2)
        with self.assertRaises(history.HistoryCapacityError):
            self.archive.backfill_account(self.reader, "public2.near", "transfers", max_pages=1)
        self.now += 60
        for number in range(2):
            self.archive.backfill_account(self.reader, "public" + str(number + 3) + ".near", "transfers", max_pages=2)
        with self.assertRaises(history.HistoryCapacityError):
            self.archive.backfill_account(self.reader, "public5.near", "transfers", max_pages=1)
        for number in range(8):
            account = "next" + str(number) + ".near"
            self.archive.release_job_read(account, self.archive.claim_job_read(account))
        with self.assertRaises(history.HistoryCapacityError):
            self.archive.claim_job_read("nextlast.near")
        with self.archive._connection() as connection:
            self.assertEqual((connection.execute("SELECT pages FROM backfill_budget").fetchone()[0], connection.execute("SELECT pages FROM job_read_budget").fetchone()[0]), (4, 8))

    def test_team_retention_removes_the_oldest_finished_job_and_never_an_active_one(self):
        with mock.patch.object(investigation_jobs, "MAX_TEAM_JOBS", 2):
            first = self.jobs.cancel("team", self.jobs.create("team", "actor", "alice.near")["id"])
            second = self.jobs.create("team", "actor", "bob.near")
            other = self.jobs.cancel("other-team", self.jobs.create("other-team", "actor", "carol.near")["id"])
            self.now += 1
            third = self.jobs.create("team", "actor", "carol.near")
            with self.assertRaises(investigation_jobs.JobNotFound):
                self.jobs.get("team", first["id"])
            self.assertEqual([item["id"] for item in self.jobs.list("team")["items"]], [third["id"], second["id"]])
            self.assertEqual(self.jobs.get("other-team", other["id"])["state"], "cancelled")
            with self.assertRaises(investigation_jobs.JobCapacityError):
                self.jobs.create("team", "actor", "dave.near")
            self.assertEqual([item["state"] for item in self.jobs.list("team")["items"]], ["queued", "queued"])

    def test_retry_respects_the_active_job_limit(self):
        stopped = self.jobs.cancel("team", self.jobs.create("team", "actor", "alice.near")["id"])
        active = [self.jobs.create("team", "actor", "account" + str(number) + ".near") for number in range(investigation_jobs.MAX_ACTIVE_JOBS)]
        with self.assertRaises(investigation_jobs.JobCapacityError):
            self.jobs.retry("team", stopped["id"])
        self.assertEqual(self.jobs.get("team", stopped["id"])["state"], "cancelled")
        self.jobs.cancel("team", active[0]["id"])
        self.assertEqual(self.jobs.retry("team", stopped["id"])["state"], "queued")

    def test_removed_jobs_release_exactly_the_bytes_they_reserved(self):
        foreign = transfer(1)
        foreign["method_name"] = "überweisung ✓"
        self.reader.pages["alice.near", None] = page([foreign])
        self.reader.pages["bob.near", None] = page([], account="bob.near")
        graph = self.jobs.create("team", "actor", "alice.near", kind="graph")
        for _ in range(2):
            self.jobs.tick(self.reader)
        self.assertIsNotNone(self.jobs.result("team", graph["id"])["graph"])
        self.now += 61
        listed = self.jobs.create("team", "actor", "alice.near")
        self.reader.pages["alice.near", None] = page([foreign], cursor="more")
        self.jobs.tick(self.reader)
        self.jobs.cancel("team", listed["id"])
        waiting = self.jobs.create("team", "actor", "carol.near")
        self.due_watches(1)
        self.jobs.tick(self.reader)
        self.assertEqual(self.jobs.get("team", waiting["id"])["last_error"], "watch_priority")
        self.jobs.cancel("team", waiting["id"])
        with self.jobs._connection() as connection:
            self.assertGreater(connection.execute("SELECT bytes FROM job_usage").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM job_items WHERE body LIKE ?", ("%überweisung%",)).fetchone()[0], 2)
        with self.jobs._connection(write=True) as connection:
            while investigation_jobs._remove_oldest(connection, "team"):
                pass
            self.assertEqual(connection.execute("SELECT bytes FROM job_usage").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT bytes FROM job_team_usage WHERE team='team'").fetchone())
            self.assertEqual([connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in ("jobs", "job_items", "job_reads")], [0, 0, 0])

    def test_a_byte_shortfall_removes_the_oldest_finished_jobs_of_any_team(self):
        finished = []
        for number in range(5):
            finished.append(self.jobs.cancel("team", self.jobs.create("team", "actor", "acct" + str(number) + ".near")["id"]))
            self.now += 1
        active = self.jobs.create("team", "actor", "alice.near")
        with self.jobs._connection() as connection:
            used = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
        with mock.patch.object(investigation_jobs, "MAX_JOB_BYTES", used + 100):
            created = self.jobs.create("other-team", "actor", "carol.near")
            with self.assertRaises(investigation_jobs.JobNotFound):
                self.jobs.get("team", finished[0]["id"])
            self.assertEqual([self.jobs.get("team", job["id"])["state"] for job in finished[1:]], ["cancelled"] * 4)
            later = [self.jobs.create("third-team", "actor", "dave" + str(number) + ".near") for number in range(4)]
            with self.assertRaises(investigation_jobs.JobCapacityError):
                self.jobs.create("third-team", "actor", "erin.near")
        self.assertEqual([item["state"] for item in self.jobs.list("team")["items"]], ["queued"])
        self.assertEqual([self.jobs.get(team, job["id"])["state"] for team, job in [("team", active), ("other-team", created)] + [("third-team", job) for job in later]], ["queued"] * 6)
        with self.jobs._connection() as connection:
            usage = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
            self.assertLessEqual(usage, used + 100)
            self.assertEqual(usage, connection.execute("SELECT SUM(bytes) FROM job_team_usage").fetchone()[0])

    def test_four_full_team_shares_do_not_lock_out_a_fifth_team(self):
        teams = ["team" + str(number) for number in range(4)]
        active, finished = {}, {}
        for team in teams:
            active[team] = self.jobs.create(team, "actor", "keep.near")
            finished[team] = [self.jobs.create(team, "actor", "acct" + str(number) + ".near") for number in range(3)]
        for team in reversed(teams):
            for job in finished[team]:
                self.now += 1
                self.jobs.cancel(team, job["id"])
        with self.jobs._connection(write=True) as connection:
            for team, state, lease in zip(teams, ("queued", "running", "retry_wait", "queued"), (0, 1, 0, 0)):
                connection.execute("UPDATE jobs SET state=?,lease_token=?,lease_until_ms=?,not_before_ms=? WHERE id=?",
                                   (state, "lease" if lease else None, self.jobs._now() + 10 ** 9 if lease else 0, self.jobs._now() + 10 ** 9, active[team]["id"]))
            used = connection.execute("SELECT bytes FROM job_usage").fetchone()[0] + 5 * investigation_jobs.CAPACITY_STOP_BYTES
            share = connection.execute("SELECT MAX(bytes) FROM job_team_usage").fetchone()[0]
        self.reader.pages["erin.near", None] = page([transfer(1, subject="erin.near")], account="erin.near")
        with mock.patch.object(investigation_jobs, "MAX_JOB_BYTES", used), mock.patch.object(investigation_jobs, "MAX_TEAM_JOB_BYTES", share):
            fifth = self.jobs.create("team4", "actor", "erin.near")
            remaining = {team: [job["id"] for job in finished[team] if job["id"] in {item["id"] for item in self.jobs.list(team)["items"]}] for team in teams}
            self.assertEqual(remaining["team3"], [job["id"] for job in finished["team3"][1:]])
            self.assertEqual([len(remaining[team]) for team in teams[:3]], [3, 3, 3])
            self.jobs.tick(self.reader)
        current = self.jobs.get("team4", fifth["id"])
        self.assertEqual((current["state"], current["counts"]["pages"], current["attempts"], current["last_error"]), ("completed", 1, 0, None))
        self.assertEqual(len(self.jobs.result("team4", fifth["id"])["items"]), 1)
        self.assertEqual([self.jobs.get(team, active[team]["id"])["state"] for team in teams], ["queued", "running", "retry_wait", "queued"])
        self.assertEqual(len(self.jobs.list("team0")["items"]), 4)
        self.assertLess(len(self.jobs.list("team3")["items"]), 3)
        with self.jobs._connection() as connection:
            usage = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
            self.assertLessEqual(usage, used)
            self.assertEqual(usage, connection.execute("SELECT SUM(bytes) FROM job_team_usage").fetchone()[0])

    def test_a_page_that_does_not_fit_waits_without_spending_attempts(self):
        self.reader.pages["erin.near", None] = page([transfer(number, subject="erin.near") for number in range(1, 4)], account="erin.near")
        holders = [self.jobs.create("team", "actor", account) for account in ("alice.near", "bob.near", "carol.near")]
        with self.jobs._connection(write=True) as connection:
            connection.execute("UPDATE jobs SET not_before_ms=?", (self.jobs._now() + 10 ** 9,))
        waiting = self.jobs.create("other-team", "actor", "erin.near")
        with self.jobs._connection() as connection:
            used = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
        with mock.patch.object(investigation_jobs, "MAX_JOB_BYTES", used + investigation_jobs.MIN_JOB_ROOM + 4 * investigation_jobs.CAPACITY_STOP_BYTES):
            for _ in range(investigation_jobs.MAX_CAPACITY_WAITS - 1):
                self.jobs.tick(self.reader)
                current = self.jobs.get("other-team", waiting["id"])
                self.assertEqual((current["state"], current["attempts"], current["last_error"], current["counts"]["pages"]), ("queued", 0, "history_capacity", 0))
                self.assertEqual(current["retry_at_ms"], self.jobs._now() + 60000)
                self.assertEqual(len(self.reader.calls), 1)
                self.now += 61
            self.assertEqual([item["state"] for item in self.jobs.list("team")["items"]], ["queued"] * 3)
            for holder in holders:
                self.jobs.cancel("team", holder["id"])
                self.now += 1
            self.jobs.tick(self.reader)
        current = self.jobs.get("other-team", waiting["id"])
        self.assertEqual((current["state"], current["attempts"], current["last_error"], current["counts"]["pages"]), ("completed", 0, None, 1))
        self.assertEqual(len(self.reader.calls), 2)
        with self.assertRaises(investigation_jobs.JobNotFound):
            self.jobs.get("team", holders[0]["id"])

    def test_active_jobs_holding_the_byte_bound_end_after_bounded_waits(self):
        holders = []
        for account in ("alice.near", "bob.near"):
            self.reader.pages[account, None] = page([], cursor="next", account=account)
            self.reader.pages[account, "next"] = page([transfer(number, subject=account) for number in range(2, 6)], account=account)
            holders.append(self.jobs.create("owner", "actor", account))
        self.jobs.tick(self.reader)
        self.jobs.tick(self.reader)
        self.assertEqual([self.jobs.get("owner", job["id"])["counts"]["pages"] for job in holders], [1, 1])
        with self.jobs._connection() as connection:
            used = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
        self.reader.pages["erin.near", None] = page([transfer(1, subject="erin.near")], account="erin.near")
        with mock.patch.object(investigation_jobs, "MAX_JOB_BYTES", used + 100):
            for _ in range(investigation_jobs.MAX_CAPACITY_WAITS - 1):
                with self.assertRaises(investigation_jobs.JobCapacityError):
                    self.jobs.create("victim", "actor", "erin.near")
                self.now += 61
                self.jobs.tick(self.reader)
                self.jobs.tick(self.reader)
                for job in holders:
                    current = self.jobs.get("owner", job["id"])
                    self.assertEqual((current["state"], current["attempts"], current["last_error"], current["counts"]["pages"]), ("queued", 0, "history_capacity", 1))
                self.assertEqual(len(self.reader.calls), 2)
            with self.assertRaises(investigation_jobs.JobCapacityError):
                self.jobs.create("victim", "actor", "erin.near")
            self.now += 61
            self.jobs.tick(self.reader)
            self.jobs.tick(self.reader)
            self.assertEqual(len(self.reader.calls), 2)
            for job in holders:
                current = self.jobs.get("owner", job["id"])
                self.assertEqual((current["state"], current["attempts"], current["last_error"], current["counts"]["pages"], current["coverage"]["stop_reason"]),
                                 ("completed", 0, None, 1, "job_capacity"))
                self.assertEqual(len(self.jobs.result("owner", job["id"])["observations"]), 1)
            victim = self.jobs.create("victim", "actor", "erin.near")
        self.assertEqual(self.jobs.get("victim", victim["id"])["state"], "queued")
        self.assertLess(len(self.jobs.list("owner")["items"]), 2)
        with self.jobs._connection() as connection:
            usage = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
            self.assertLessEqual(usage, used + 100)
            self.assertEqual(usage, connection.execute("SELECT SUM(bytes) FROM job_team_usage").fetchone()[0])

    def test_a_full_archive_does_not_keep_active_jobs_holding_the_byte_bound(self):
        holders = []
        for account in ("alice.near", "bob.near"):
            self.reader.pages[account, None] = page([transfer(1, subject=account)], cursor="next", account=account)
            self.reader.pages[account, "next"] = page([transfer(2, subject=account)], account=account)
            holders.append(self.jobs.create("owner", "actor", account))
        self.jobs.tick(self.reader)
        self.jobs.tick(self.reader)
        self.assertEqual([self.jobs.get("owner", job["id"])["counts"]["pages"] for job in holders], [1, 1])
        with self.jobs._connection() as connection:
            used = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
        with self.archive._connection() as connection:
            archived = connection.execute("SELECT pages FROM archive_usage").fetchone()[0]
        with mock.patch.object(history, "MAX_ARCHIVE_PAGES", archived), mock.patch.object(investigation_jobs, "MAX_JOB_BYTES", used + 100):
            for wait in range(investigation_jobs.MAX_CAPACITY_WAITS):
                with self.assertRaises(investigation_jobs.JobCapacityError):
                    self.jobs.create("victim", "actor", "erin.near")
                self.now += 61
                self.jobs.tick(self.reader)
                if wait < investigation_jobs.MAX_CAPACITY_WAITS - 1:
                    self.jobs.tick(self.reader)
            ended = [job for job in holders if self.jobs.get("owner", job["id"])["state"] == "completed"]
            self.assertEqual(len(ended), 1)
            current = self.jobs.get("owner", ended[0]["id"])
            self.assertEqual((current["attempts"], current["last_error"], current["counts"]["pages"], current["coverage"]["stop_reason"]), (0, None, 1, "job_capacity"))
            self.assertEqual(len(self.jobs.result("owner", ended[0]["id"])["items"]), 1)
            self.jobs.tick(self.reader)
            self.assertEqual(len(self.reader.calls), 2)
            with self.assertRaises(investigation_jobs.JobNotFound):
                self.jobs.get("owner", ended[0]["id"])
            kept = self.jobs.get("owner", next(job["id"] for job in holders if job is not ended[0]))
            self.assertEqual((kept["state"], kept["attempts"], kept["last_error"], kept["counts"]["pages"]), ("queued", 0, "history_capacity", 1))
            victim = self.jobs.create("victim", "actor", "erin.near")
        self.assertEqual(self.jobs.get("victim", victim["id"])["state"], "queued")
        with self.jobs._connection() as connection:
            usage = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
            self.assertLessEqual(usage, used + 100)
            self.assertEqual(usage, connection.execute("SELECT SUM(bytes) FROM job_team_usage").fetchone()[0])
            self.assertEqual(usage, connection.execute(ROW_BYTES).fetchone()[0])

    def test_a_full_archive_alone_never_ends_a_job_or_reads_the_provider(self):
        self.reader.pages["alice.near", None] = page([transfer(1)])
        job = self.jobs.create("team", "actor", "alice.near")
        self.archive.record_page("bob.near", "transfers", page([transfer(1, subject="bob.near")], account="bob.near"))
        with self.archive._connection() as connection:
            archived = connection.execute("SELECT pages FROM archive_usage").fetchone()[0]
        with mock.patch.object(history, "MAX_ARCHIVE_PAGES", archived):
            for _ in range(investigation_jobs.MAX_CAPACITY_WAITS + 2):
                self.jobs.tick(self.reader)
                current = self.jobs.get("team", job["id"])
                self.assertEqual((current["state"], current["attempts"], current["last_error"], current["counts"]["pages"]), ("queued", 0, "history_capacity", 0))
                self.now += 61
        self.assertEqual(self.reader.calls, [])
        with self.jobs._connection() as connection:
            self.assertEqual(connection.execute("SELECT capacity_waits FROM jobs WHERE id=?", (job["id"],)).fetchone()[0], 0)
        self.jobs.tick(self.reader)
        current = self.jobs.get("team", job["id"])
        self.assertEqual((current["state"], current["attempts"], current["last_error"], current["counts"]["pages"]), ("completed", 0, None, 1))

    def test_archive_rows_or_bytes_within_one_page_of_the_limit_stop_job_reads(self):
        self.reader.pages["alice.near", None] = page([transfer(number) for number in range(1, 4)])
        self.archive.record_page("bob.near", "transfers", page([transfer(1, subject="bob.near")], account="bob.near"))
        for reads, (limit, field, extra) in enumerate((("MAX_ARCHIVE_ROWS", "records", 0), ("MAX_ARCHIVE_BYTES", "bytes", 50))):
            with self.subTest(limit=limit):
                job = self.jobs.create("team", "actor", "alice.near")
                with self.archive._connection() as connection:
                    used = connection.execute("SELECT records,bytes FROM archive_usage").fetchone()[field]
                with mock.patch.object(history, limit, used + extra):
                    for step in range(24):
                        self.jobs.tick(self.reader)
                        current = self.jobs.get("team", job["id"])
                        self.assertEqual((current["state"], current["attempts"], current["last_error"], current["counts"]["pages"]), ("queued", 0, "history_capacity", 0))
                        self.now += 31 * 86400 if step == 11 else 61
                    self.assertEqual(len(self.reader.calls), reads)
                self.jobs.tick(self.reader)
                current = self.jobs.get("team", job["id"])
                self.assertEqual((current["state"], current["attempts"], current["last_error"], current["counts"]["pages"]), ("completed", 0, None, 1))
                self.assertEqual(len(self.reader.calls), reads + 1)

    def test_a_full_archive_holds_every_job_before_another_teams_page_reaches_the_job_store(self):
        holders = []
        for account in ("alice.near", "bob.near"):
            self.reader.pages[account, None] = page([transfer(1, subject=account)], cursor="next", account=account)
            self.reader.pages[account, "next"] = page([transfer(2, subject=account)], account=account)
            holders.append(self.jobs.create("owner", "actor", account))
        self.jobs.tick(self.reader)
        self.jobs.tick(self.reader)
        self.assertEqual([self.jobs.get("owner", job["id"])["counts"]["pages"] for job in holders], [1, 1])
        held = page([transfer(number, subject="erin.near") for number in range(1, 21)], account="erin.near")
        self.archive.record_page("erin.near", "transfers", held)
        self.reader.pages["erin.near", None] = held
        with self.jobs._connection() as connection:
            used = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
        with self.archive._connection() as connection:
            records = connection.execute("SELECT records FROM archive_usage").fetchone()[0]
        with mock.patch.object(history, "MAX_ARCHIVE_ROWS", records), mock.patch.object(investigation_jobs, "MAX_JOB_BYTES", used + 1100 + investigation_jobs.MIN_JOB_ROOM + 200):
            victim = self.jobs.create("victim", "actor", "erin.near")
            for days in (0, 31):
                self.now += days * 86400
                for _ in range(12):
                    self.now += 61
                    self.jobs.tick(self.reader)
                    self.assertNotEqual(self.jobs.get("victim", victim["id"])["state"], "completed")
                current = self.jobs.get("victim", victim["id"])
                self.assertEqual((current["state"], current["attempts"], current["last_error"], current["counts"]["pages"]), ("queued", 0, "history_capacity", 0))
                for job in holders:
                    current = self.jobs.get("owner", job["id"])
                    self.assertEqual((current["state"], current["attempts"], current["last_error"], current["counts"]["pages"]), ("queued", 0, "history_capacity", 1))
                self.assertEqual(len(self.reader.calls), 2)

    def test_the_room_a_job_needs_before_reading_fits_the_largest_new_job(self):
        self.now = canon.U64_MAX // 1000000 / 1000
        jobs = investigation_jobs.InvestigationJobs(self.root / "largest.sqlite3", self.archive, clock=lambda: self.now)
        jobs.create("team", "actor", "a" * 64, kind="transactions", from_timestamp_ms=jobs._now() - 1, max_pages=100, max_rows=5000, depth=5, max_accounts=50, pages_per_account=5)
        with jobs._connection() as connection:
            size = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
        self.assertLessEqual(size + investigation_jobs.CAPACITY_STOP_BYTES, investigation_jobs.MIN_JOB_ROOM)

    def job_sizes(self, accounts):
        jobs = investigation_jobs.InvestigationJobs(self.root / "sizes.sqlite3", self.archive, clock=lambda: self.now)
        sizes = []
        for account in accounts:
            jobs.create("sizes", "actor", account)
            with jobs._connection() as connection:
                sizes.append(connection.execute("SELECT bytes FROM job_usage").fetchone()[0] - sum(sizes))
        return sizes

    def test_ending_jobs_at_zero_headroom_keeps_usage_within_the_bound(self):
        accounts = ("alice.near", "bob.near")
        for account in accounts:
            self.reader.pages[account, None] = page([transfer(1, subject=account)], account=account)
        total = sum(self.job_sizes(accounts))
        for bound in ("MAX_JOB_BYTES", "MAX_TEAM_JOB_BYTES"):
            for extra in range(2 * investigation_jobs.CAPACITY_STOP_BYTES + 1):
                with self.subTest(bound=bound, extra=extra), mock.patch.object(investigation_jobs, bound, total + extra):
                    jobs = investigation_jobs.InvestigationJobs(self.root / (bound + str(extra) + ".sqlite3"), self.archive, clock=lambda: self.now)
                    created = []
                    for account in accounts:
                        try:
                            created.append(jobs.create("team", "actor", account)["id"])
                        except investigation_jobs.JobCapacityError:
                            pass
                    for _ in range(investigation_jobs.MAX_CAPACITY_WAITS):
                        jobs.tick(self.reader)
                        jobs.tick(self.reader)
                        self.now += 61
                    self.assertEqual([(jobs.get("team", identifier)["state"], jobs.get("team", identifier)["coverage"]["stop_reason"]) for identifier in created],
                                     [("completed", "job_capacity")] * len(created))
                    with jobs._connection() as connection:
                        usage = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
                        self.assertLessEqual(usage, total + extra)
                        self.assertEqual(usage, connection.execute("SELECT SUM(bytes) FROM job_team_usage").fetchone()[0])
                        self.assertEqual(usage, connection.execute(ROW_BYTES).fetchone()[0])
                    self.assertEqual(len(created), 2 if extra == 2 * investigation_jobs.CAPACITY_STOP_BYTES else 1)

    def test_retrying_a_stopped_job_keeps_room_for_every_active_stop_reason(self):
        first_size, second_size = self.job_sizes(("alice.near", "bob.near"))
        with mock.patch.object(investigation_jobs, "MAX_JOB_BYTES", first_size + second_size + 15):
            first = self.jobs.create("team", "actor", "alice.near")
            self.jobs.cancel("team", first["id"])
            second = self.jobs.create("team", "actor", "bob.near")
            with self.assertRaises(investigation_jobs.JobCapacityError):
                self.jobs.retry("team", first["id"])
            self.assertEqual(self.jobs.get("team", first["id"])["state"], "cancelled")
            self.jobs.cancel("team", second["id"])
            self.assertEqual(self.jobs.retry("team", first["id"])["state"], "queued")
        self.assertEqual(self.jobs.get("team", second["id"])["state"], "cancelled")

    def test_a_job_that_cannot_store_its_final_graph_ends_without_it(self):
        self.reader.pages["alice.near", None] = page([transfer(1)])
        job = self.jobs.create("team", "actor", "alice.near", kind="graph", depth=1)
        with self.jobs._connection() as connection:
            used = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
        with mock.patch.object(investigation_jobs, "MAX_JOB_BYTES", used + investigation_jobs.MIN_JOB_ROOM + investigation_jobs.CAPACITY_STOP_BYTES):
            for _ in range(investigation_jobs.MAX_CAPACITY_WAITS):
                self.jobs.tick(self.reader)
                self.now += 61
        current = self.jobs.get("team", job["id"])
        self.assertEqual((current["state"], current["attempts"], current["last_error"], current["counts"]["pages"], current["coverage"]["stop_reason"]),
                         ("completed", 0, None, 1, "job_capacity"))
        retained = self.jobs.result("team", job["id"])
        self.assertIsNone(retained["graph"])
        self.assertEqual(len(retained["items"]), 1)
        with self.jobs._connection() as connection:
            usage = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
            self.assertEqual(usage, connection.execute("SELECT SUM(bytes) FROM job_team_usage").fetchone()[0])
            self.assertEqual(usage, connection.execute("SELECT SUM(LENGTH(CAST(request AS BLOB))+LENGTH(CAST(progress AS BLOB))+512+(SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))+256),0) FROM job_items WHERE job=jobs.id)+(SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))),0) FROM job_reads WHERE job=jobs.id)) FROM jobs").fetchone()[0])

    def test_finished_job_removal_reads_the_finished_jobs_index(self):
        for number in range(3):
            self.jobs.cancel("team", self.jobs.create("team", "actor", "acct" + str(number) + ".near")["id"])
        self.jobs.create("team", "actor", "alice.near")
        path = self.root / "previous-jobs.sqlite3"
        previous = sqlite3.connect(path, isolation_level=None)
        previous.executescript(PREVIOUS_SCHEMA)
        previous.execute("ALTER TABLE jobs ADD COLUMN provider_retry_ms INTEGER NOT NULL DEFAULT 0")
        previous.execute("PRAGMA user_version=2")
        previous.close()
        reopened = investigation_jobs.InvestigationJobs(path, self.archive, clock=lambda: self.now)
        for store in (self.jobs, reopened):
            with store._connection(write=True) as connection:
                statements = []
                connection.set_trace_callback(statements.append)
                investigation_jobs._remove_finished(connection, store._now())
                connection.set_trace_callback(None)
                plan = " ".join(row["detail"] for row in connection.execute("EXPLAIN QUERY PLAN " + statements[0]))
            self.assertIn("USING INDEX finished_jobs", plan)
            self.assertNotIn("TEMP B-TREE", plan)

    def test_finished_jobs_expire_thirty_days_after_they_finish(self):
        kept = self.jobs.create("team", "actor", "bob.near")
        with self.jobs._connection(write=True) as connection:
            connection.execute("UPDATE jobs SET not_before_ms=? WHERE id=?", (self.jobs._now() + 10 ** 12, kept["id"]))
        finished = self.jobs.cancel("team", self.jobs.create("team", "actor", "alice.near")["id"])
        self.now += 10
        other = self.jobs.cancel("other-team", self.jobs.create("other-team", "actor", "carol.near")["id"])
        self.now += 20 * 86400
        self.jobs.cancel("team", finished["id"])
        self.now += 10 * 86400 - 10 - 0.001
        self.assertEqual(self.jobs.tick(self.reader), {"processed": 0})
        self.assertEqual(self.jobs.get("team", finished["id"])["state"], "cancelled")
        self.now += 0.001
        self.assertEqual(self.jobs.tick(self.reader), {"processed": 0})
        with self.assertRaises(investigation_jobs.JobNotFound):
            self.jobs.get("team", finished["id"])
        self.assertEqual(self.jobs.get("other-team", other["id"])["state"], "cancelled")
        self.now += 10
        self.jobs.create("third-team", "actor", "dave.near")
        with self.assertRaises(investigation_jobs.JobNotFound):
            self.jobs.get("other-team", other["id"])
        self.assertEqual(self.jobs.get("team", kept["id"])["state"], "queued")
        with self.jobs._connection() as connection:
            usage = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
            self.assertEqual(usage, connection.execute("SELECT SUM(bytes) FROM job_team_usage").fetchone()[0])
            self.assertEqual(usage, connection.execute("SELECT SUM(LENGTH(CAST(request AS BLOB))+LENGTH(CAST(progress AS BLOB))+512) FROM jobs").fetchone()[0])

    def test_expiry_removes_a_bounded_number_of_jobs_per_write(self):
        finished = []
        for number in range(3):
            finished.append(self.jobs.cancel("team", self.jobs.create("team", "actor", "acct" + str(number) + ".near")["id"]))
            self.now += 1
        self.now += 31 * 86400
        with mock.patch.object(investigation_jobs, "MAX_EXPIRED_PER_WRITE", 2):
            self.assertEqual(self.jobs.tick(self.reader), {"processed": 0})
            self.assertEqual([item["id"] for item in self.jobs.list("team")["items"]], [finished[2]["id"]])
            self.assertEqual(self.jobs.tick(self.reader), {"processed": 0})
        self.assertEqual(self.jobs.list("team")["items"], [])
        with self.jobs._connection() as connection:
            self.assertEqual(connection.execute("SELECT bytes FROM job_usage").fetchone()[0], 0)

    def test_a_team_at_its_byte_share_leaves_room_for_other_teams(self):
        self.reader.pages["dave.near", None] = page([transfer(1)], account="dave.near")
        finished = self.jobs.cancel("team", self.jobs.create("team", "actor", "alice.near")["id"])
        kept = [self.jobs.create("team", "actor", account) for account in ("bob.near", "carol.near")]
        with self.jobs._connection() as connection:
            share = connection.execute("SELECT bytes FROM job_usage").fetchone()[0] + 3 * investigation_jobs.CAPACITY_STOP_BYTES
        with mock.patch.object(investigation_jobs, "MAX_TEAM_JOB_BYTES", share, create=True):
            replacement = self.jobs.create("team", "actor", "erin.near")
            with self.assertRaises(investigation_jobs.JobNotFound):
                self.jobs.get("team", finished["id"])
            with self.assertRaises(investigation_jobs.JobCapacityError):
                self.jobs.create("team", "actor", "francesca.near")
            for job in kept + [replacement]:
                self.jobs.cancel("team", job["id"])
            other = self.jobs.create("other-team", "actor", "dave.near")
        self.jobs.tick(self.reader)
        current = self.jobs.get("other-team", other["id"])
        self.assertEqual((current["state"], current["counts"]["pages"], current["last_error"]), ("completed", 1, None))
        self.assertEqual([item["state"] for item in self.jobs.list("team")["items"]], ["cancelled"] * 3)
        with self.jobs._connection() as connection:
            usage = dict(connection.execute("SELECT team,bytes FROM job_team_usage").fetchall())
            self.assertLessEqual(usage["team"], share)
            self.assertEqual(sum(usage.values()), connection.execute("SELECT bytes FROM job_usage").fetchone()[0])

    def test_a_job_removed_during_its_fetch_does_not_stop_the_worker(self):
        job = self.jobs.create("team", "actor", "alice.near")

        class Reader:
            def history_page(inner, account, kind, **kwargs):
                self.jobs.cancel("team", job["id"])
                with mock.patch.object(investigation_jobs, "MAX_TEAM_JOBS", 1):
                    self.jobs.create("team", "actor", "bob.near")
                return page([transfer(1)])

        self.assertEqual(self.jobs.tick(Reader()), {"processed": 1})
        with self.assertRaises(investigation_jobs.JobNotFound):
            self.jobs.get("team", job["id"])
        self.assertEqual([item["state"] for item in self.jobs.list("team")["items"]], ["queued"])
        with self.archive._connection() as connection:
            self.assertEqual(tuple(connection.execute("SELECT lease_token,lease_until_ms FROM backfill_control WHERE kind='job'").fetchone()), (None, 0))

    def test_a_database_from_the_previous_schema_opens_in_place(self):
        path = self.root / "previous-jobs.sqlite3"
        request = {"account_id": "alice.near", "kind": "transfers", "from_timestamp_ms": None, "to_timestamp_ms": 2000000,
                   "max_pages": 20, "max_rows": 1000, "depth": 3, "max_accounts": 20, "pages_per_account": 2}
        progress = {"pages": 1, "scanned_rows": 1, "matched_rows": 1, "more_provider_pages": False, "unknown_counterparties": 0,
                    "account_limit_rows": 0, "stop_reason": "provider_pages_exhausted", "snapshot": None, "graph": None,
                    "frontier": {"alice.near": {"depth": 0, "cursor": None, "pages": 1, "done": True}}}
        item = investigation_jobs._json({"id": "row1", "method_name": "überweisung"})
        read = investigation_jobs._json({"page_id": "previous-page", "account_id": "alice.near", "source": {"provider": "ü"}, "observed_at": None})
        encoded, state = investigation_jobs._json(request), investigation_jobs._json(progress)
        reserved = len((encoded + state).encode("utf-8")) + 512 + len(item.encode("utf-8")) + 256 + len(read.encode("utf-8"))
        previous = sqlite3.connect(path, isolation_level=None)
        previous.executescript(PREVIOUS_SCHEMA)
        previous.execute("ALTER TABLE jobs ADD COLUMN provider_retry_ms INTEGER NOT NULL DEFAULT 0")
        previous.execute("PRAGMA user_version=2")
        previous.execute("INSERT INTO jobs(id,team,actor,request,progress,state,created_ms,updated_ms,not_before_ms) VALUES('old','team','actor',?,?,'completed',1,1,1)", (encoded, state))
        previous.execute("INSERT INTO job_items(job,row_id,body,page_id) VALUES('old','row1',?,'previous-page')", (item,))
        previous.execute("INSERT INTO job_reads(job,body) VALUES('old',?)", (read,))
        previous.execute("UPDATE job_usage SET bytes=?", (reserved,))
        previous.close()
        reopened = investigation_jobs.InvestigationJobs(path, self.archive, clock=lambda: self.now)
        self.assertEqual(reopened.get("team", "old")["state"], "completed")
        self.assertEqual(reopened.result("team", "old")["items"], [json.loads(item)])
        with reopened._connection(write=True) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(tuple(connection.execute("SELECT watch_yields,capacity_waits,capacity_need FROM jobs WHERE id='old'").fetchone()), (0, 0, 0))
            self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='job_reads_job'").fetchone())
            self.assertEqual(connection.execute("SELECT bytes FROM job_team_usage WHERE team='team'").fetchone()[0], reserved)
            self.assertTrue(investigation_jobs._remove_oldest(connection, "team"))
            self.assertEqual(connection.execute("SELECT bytes FROM job_usage").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT bytes FROM job_team_usage WHERE team='team'").fetchone())
        earlier = sqlite3.connect(path, isolation_level=None)
        self.assertIn(earlier.execute("PRAGMA user_version").fetchone()[0], (0, 1, 2))
        earlier.executescript(PREVIOUS_SCHEMA)
        earlier.execute("INSERT INTO jobs(id,team,actor,request,progress,state,created_ms,updated_ms,not_before_ms) VALUES('again','team','actor',?,?,'queued',2,2,2)", (encoded, state))
        earlier.close()
        self.assertEqual(reopened.get("team", "again")["state"], "queued")
        investigation_jobs.InvestigationJobs(path, self.archive, clock=lambda: self.now)
        with reopened._connection() as connection:
            self.assertEqual(connection.execute("SELECT bytes FROM job_team_usage WHERE team='team'").fetchone()[0], len((encoded + state).encode("utf-8")) + 512)
            self.assertEqual(connection.execute("SELECT bytes FROM job_usage").fetchone()[0], len((encoded + state).encode("utf-8")) + 512)

    def test_a_previous_release_database_reclaims_finished_jobs_for_other_teams(self):
        path = self.root / "previous-jobs.sqlite3"

        def job(account, pages, stop_reason):
            request = {"account_id": account, "kind": "transfers", "from_timestamp_ms": None, "to_timestamp_ms": 2000000,
                       "max_pages": 20, "max_rows": 1000, "depth": 3, "max_accounts": 20, "pages_per_account": 2}
            progress = {"pages": pages, "scanned_rows": 0, "matched_rows": 0, "more_provider_pages": False, "unknown_counterparties": 0,
                        "account_limit_rows": 0, "stop_reason": stop_reason, "snapshot": None, "graph": None,
                        "frontier": {account: {"depth": 0, "cursor": None, "pages": pages, "done": pages > 0}}}
            return investigation_jobs._json(request), investigation_jobs._json(progress)

        previous = sqlite3.connect(path, isolation_level=None)
        previous.executescript(PREVIOUS_SCHEMA)
        previous.execute("ALTER TABLE jobs ADD COLUMN provider_retry_ms INTEGER NOT NULL DEFAULT 0")
        previous.execute("PRAGMA user_version=2")
        for number in range(5):
            encoded, state = job("acct" + str(number) + ".near", 1, "provider_pages_exhausted")
            previous.execute("INSERT INTO jobs(id,team,actor,request,progress,state,created_ms,updated_ms,not_before_ms) VALUES(?,'team-a','actor',?,?,'cancelled',1,?,1)", ("old" + str(number), encoded, state, 10 - number))
            previous.execute("INSERT INTO job_items(job,row_id,body,page_id) VALUES(?,'row',?,'previous-page')", ("old" + str(number), investigation_jobs._json({"id": "row", "n": number})))
        encoded, state = job("erin.near", 0, None)
        previous.execute("INSERT INTO jobs(id,team,actor,request,progress,state,created_ms,updated_ms,not_before_ms,lease_token,lease_until_ms) VALUES('busy','team-b','actor',?,?,'running',20,20,20,'stale',1)", (encoded, state))
        total = previous.execute("SELECT SUM(LENGTH(CAST(request AS BLOB))+LENGTH(CAST(progress AS BLOB))+512+(SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))+256),0) FROM job_items WHERE job=jobs.id)) FROM jobs").fetchone()[0]
        previous.execute("UPDATE job_usage SET bytes=?", (total,))
        previous.close()
        reopened = investigation_jobs.InvestigationJobs(path, self.archive, clock=lambda: self.now)
        with reopened._connection() as connection:
            self.assertEqual(connection.execute("SELECT bytes FROM job_usage").fetchone()[0], total)
            self.assertEqual(connection.execute("SELECT SUM(bytes) FROM job_team_usage").fetchone()[0], total)
        self.reader.pages["erin.near", None] = page([transfer(1, subject="erin.near")], account="erin.near")
        with mock.patch.object(investigation_jobs, "MAX_JOB_BYTES", total + 100):
            reopened.tick(self.reader)
            current = reopened.get("team-b", "busy")
            self.assertEqual((current["state"], current["counts"]["pages"], current["attempts"], current["last_error"]), ("completed", 1, 0, None))
            with self.assertRaises(investigation_jobs.JobNotFound):
                reopened.get("team-a", "old4")
            created = reopened.create("team-c", "actor", "carol.near")
        self.assertEqual(reopened.get("team-c", created["id"])["state"], "queued")
        self.assertEqual(reopened.get("team-a", "old0")["state"], "cancelled")
        with reopened._connection() as connection:
            usage = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
            self.assertLessEqual(usage, total + 100)
            self.assertEqual(usage, connection.execute("SELECT SUM(bytes) FROM job_team_usage").fetchone()[0])

    def test_removing_a_teams_last_job_drops_its_usage_row(self):
        original, limit = investigation_jobs.InvestigationJobs._connection, []

        @contextmanager
        def limited(store, **options):
            with original(store, **options) as connection:
                if limit:
                    self.assertEqual(connection.execute("PRAGMA max_page_count=" + str(limit[0])).fetchone()[0], limit[0])
                yield connection

        def held():
            with original(self.jobs) as connection:
                usage = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM job_team_usage").fetchone()[0], connection.execute("SELECT COUNT(DISTINCT team) FROM jobs").fetchone()[0])
                self.assertEqual(usage, connection.execute("SELECT COALESCE(SUM(bytes),0) FROM job_team_usage").fetchone()[0])
                self.assertEqual(usage, connection.execute("SELECT COALESCE((" + ROW_BYTES + "),0)").fetchone()[0])

        size = self.job_sizes(("alice.near",))[0]
        with mock.patch.object(investigation_jobs.InvestigationJobs, "_connection", limited), mock.patch.object(investigation_jobs, "MAX_JOB_BYTES", 4 * size):
            for number in range(400):
                if number == 20:
                    with self.jobs._connection() as connection:
                        limit.append(connection.execute("PRAGMA page_count").fetchone()[0] + 2)
                    held()
                team = "team" + str(number).zfill(28)
                self.jobs.cancel(team, self.jobs.create(team, "actor", "alice.near")["id"])
                self.now += 1
                if number >= 200:
                    self.now += 31 * 86400
                    self.assertEqual(self.jobs.tick(self.reader), {"processed": 0})
                if number == 199:
                    held()
            self.assertEqual(self.jobs.list("team" + "0" * 28)["items"], [])
            last = self.jobs.create("last-team", "actor", "alice.near")
        self.assertEqual(self.jobs.get("last-team", last["id"])["state"], "queued")
        held()
        with self.jobs._connection() as connection:
            self.assertEqual(connection.execute("SELECT team FROM job_team_usage").fetchall()[0][0], "last-team")

    def test_a_job_removed_before_its_room_check_leaves_no_usage_row(self):
        self.reader.pages["alice.near", None] = page([transfer(1)])
        job = self.jobs.create("team", "actor", "alice.near")
        original, calls = investigation_jobs.InvestigationJobs._connection, []

        def removing(store, **options):
            calls.append(options)
            if len(calls) == 2:
                with original(store, write=True) as connection:
                    connection.execute("UPDATE jobs SET state='cancelled',lease_token=NULL,lease_until_ms=0 WHERE id=?", (job["id"],))
                    self.assertTrue(investigation_jobs._remove_oldest(connection, "team"))
            return original(store, **options)

        with mock.patch.object(investigation_jobs.InvestigationJobs, "_connection", removing):
            self.assertEqual(self.jobs.tick(self.reader), {"processed": 1})
        self.assertEqual(self.reader.calls, [])
        with self.jobs._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM job_team_usage").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT bytes FROM job_usage").fetchone()[0], 0)
        with self.archive._connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM backfill_control WHERE kind='job'").fetchone())

    def test_a_previous_release_active_job_ends_within_the_bound_at_zero_headroom(self):
        def job(account, stop_reason):
            request = {"account_id": account, "kind": "transfers", "from_timestamp_ms": None, "to_timestamp_ms": 2000000,
                       "max_pages": 20, "max_rows": 1000, "depth": 3, "max_accounts": 20, "pages_per_account": 2}
            progress = {"pages": 0, "scanned_rows": 0, "matched_rows": 0, "more_provider_pages": False, "unknown_counterparties": 0,
                        "account_limit_rows": 0, "stop_reason": stop_reason, "snapshot": None, "graph": None,
                        "frontier": {account: {"depth": 0, "cursor": None, "pages": 0, "done": stop_reason is not None}}}
            return investigation_jobs._json(request), investigation_jobs._json(progress)

        for bound in ("MAX_JOB_BYTES", "MAX_TEAM_JOB_BYTES"):
            for finished in (True, False):
                with self.subTest(bound=bound, finished=finished):
                    path = self.root / ("previous-" + bound + str(finished) + ".sqlite3")
                    previous = sqlite3.connect(path, isolation_level=None)
                    previous.executescript(PREVIOUS_SCHEMA)
                    previous.execute("ALTER TABLE jobs ADD COLUMN provider_retry_ms INTEGER NOT NULL DEFAULT 0")
                    previous.execute("PRAGMA user_version=2")
                    encoded, state = job("erin.near", None)
                    previous.execute("INSERT INTO jobs(id,team,actor,request,progress,state,created_ms,updated_ms,not_before_ms,lease_token,lease_until_ms) VALUES('busy','team','actor',?,?,'running',20,20,20,'stale',1)", (encoded, state))
                    if finished:
                        encoded, state = job("carol.near", "provider_pages_exhausted")
                        previous.execute("INSERT INTO jobs(id,team,actor,request,progress,state,created_ms,updated_ms,not_before_ms) VALUES('old','team','actor',?,?,'cancelled',1,1,1)", (encoded, state))
                    total = previous.execute(ROW_BYTES).fetchone()[0]
                    previous.execute("UPDATE job_usage SET bytes=?", (total,))
                    previous.close()
                    reopened = investigation_jobs.InvestigationJobs(path, self.archive, clock=lambda: self.now)
                    with mock.patch.object(investigation_jobs, bound, total):
                        for _ in range(investigation_jobs.MAX_CAPACITY_WAITS):
                            self.assertEqual(reopened.tick(self.reader), {"processed": 1})
                            self.now += 61
                    current = reopened.get("team", "busy")
                    self.assertEqual((current["state"], current["coverage"]["stop_reason"], current["last_error"], current["attempts"]),
                                     ("completed", "job_capacity", None, 0) if finished else ("failed", None, "job_capacity", 0))
                    self.assertEqual([item["id"] for item in reopened.list("team")["items"]], ["busy"])
                    self.assertEqual(self.reader.calls, [])
                    with reopened._connection() as connection:
                        usage = connection.execute("SELECT bytes FROM job_usage").fetchone()[0]
                        self.assertLessEqual(usage, total)
                        self.assertEqual(usage, connection.execute("SELECT SUM(bytes) FROM job_team_usage").fetchone()[0])
                        self.assertEqual(usage, connection.execute(ROW_BYTES).fetchone()[0])
                    with mock.patch.object(investigation_jobs, bound, total):
                        if finished:
                            with self.assertRaises(investigation_jobs.JobInputError):
                                reopened.retry("team", "busy")
                        else:
                            with self.assertRaises(investigation_jobs.JobCapacityError):
                                reopened.retry("team", "busy")
                            self.assertEqual(reopened.get("team", "busy")["state"], "failed")

    def test_a_storage_error_in_a_tick_does_not_stop_the_service_worker(self):
        jobs_path = self.root / "service-jobs.sqlite3"
        investigation_jobs.InvestigationJobs(jobs_path, self.archive)
        original, writes, waits = investigation_jobs.InvestigationJobs._connection, [], []

        def full(store, **options):
            if options.get("write"):
                writes.append(options)
                if len(writes) == 2:
                    raise sqlite3.OperationalError("database or disk is full")
            return original(store, **options)

        class Stop:
            def wait(inner, interval):
                waits.append(interval)
                return len(waits) == 2

        errors = io.StringIO()
        with mock.patch.object(investigation_jobs.InvestigationJobs, "_connection", full), redirect_stderr(errors):
            status = investigation_jobs.run(jobs_path, self.archive, self.reader, stop_event=Stop())
        self.assertEqual(waits, [5, 5])
        self.assertIn("OperationalError", errors.getvalue())
        self.assertFalse(status["running"])
        self.assertIsNotNone(status["heartbeat_ms"])
        writes.clear()
        with mock.patch.object(investigation_jobs.InvestigationJobs, "_connection", full), self.assertRaises(sqlite3.OperationalError):
            investigation_jobs.run(jobs_path, self.archive, self.reader, once=True)


if __name__ == "__main__":
    unittest.main()
