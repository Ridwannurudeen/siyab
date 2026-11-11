import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from email.message import Message
from unittest import mock

from intel import history, investigation_jobs, live
from intel.accounts import AccountReadError
from intel.workspace import SQLiteWorkspaceStore
from intel.workspace_api import WorkspaceAPI
from tests.test_history import Reader, later, page, transfer


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

    def test_history_budget_waits_without_spending_failure_retries_and_yields_to_watch(self):
        self.reader.pages["alice.near", None] = page([])
        job = self.jobs.create("team", "actor", "alice.near")
        with self.archive._connection(write=True) as connection:
            connection.execute("INSERT INTO watch_poll(account,next_due_ms) VALUES('bob.near',?)", (self.archive._now(),))
        self.jobs.tick(self.reader)
        current = self.jobs.get("team", job["id"])
        self.assertEqual(current["attempts"], 0)
        self.assertEqual(current["last_error"], "history_capacity")
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(current["retry_at_ms"], self.jobs._now() + 60000)

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
        with mock.patch.object(investigation_jobs, "MAX_JOBS", 1), self.assertRaises(investigation_jobs.JobCapacityError):
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

    def test_job_and_public_backfill_share_the_same_persistent_provider_budget(self):
        for number in range(12):
            account = "account" + str(number) + ".near"
            token = self.archive.claim_job_read(account)
            self.archive.release_job_read(account, token)
        with self.assertRaises(history.HistoryCapacityError):
            self.archive.backfill_account(self.reader, "alice.near", "transfers", max_pages=1)
        self.assertEqual(self.reader.calls, [])

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


if __name__ == "__main__":
    unittest.main()
