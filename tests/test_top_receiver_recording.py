import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from gate import rpc
from intel import __main__ as intel_main
from intel import budget

from tests.test_budget import BudgetCase, T0


class RecordingBudgetTest(BudgetCase):
    def test_cooldown_refusal_does_not_reserve_a_keyless_call(self):
        budget.key_failure(429, T0)
        with tempfile.TemporaryDirectory() as directory, rpc.caller_class("recording"), \
                mock.patch.object(rpc, "_fetch") as fetched:
            client = rpc.Client(rpc.MAINNET_RPC_URL, "record", kind="http", fixtures_dir=directory,
                                headers={"Authorization": "Bearer dummy-recording-token"}, clock=lambda: T0)
            with self.assertRaises(rpc.BudgetExhausted) as caught:
                client.get("/read")
        fetched.assert_not_called()
        self.assertEqual(self.count("keyless:recording:fastnear_rpc", budget.DAY_S), 0)
        self.assertEqual(self.count("pooled", budget.DAY_S), 0)
        self.assertEqual(caught.exception.retry_after, budget.KEY_COOLDOWN_S)

    def test_recording_without_a_database_refuses_before_fetching(self):
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as directory:
            os.environ.pop(budget.DATABASE_ENV)
            client = rpc.Client(rpc.MAINNET_RPC_URL, "record", kind="http", fixtures_dir=directory,
                                headers={"Authorization": "Bearer dummy-recording-token"})
            with rpc.caller_class("recording"), mock.patch.object(rpc, "_fetch") as fetched:
                with self.assertRaises(rpc.BudgetExhausted):
                    client.get("/read")
            fetched.assert_not_called()
            self.assertFalse(self.path.exists())

    def test_recording_shares_3000_daily_calls_across_fastnear_classes(self):
        self.assertIn("recording", budget.CALLER_CLASSES)
        self.assertEqual(budget.KEYED_LIMITS["recording"], {"*": (3000, None, None)})
        self.seed("keyed:recording:*", budget.DAY_S, 2996)
        for provider in budget.FASTNEAR_CLASSES:
            self.assertTrue(budget.reserve(provider, "recording", keyed=True, now=T0))
        for provider in budget.FASTNEAR_CLASSES:
            self.assertRefused(provider, "recording", True, T0, 48600)
        self.assertEqual(self.count("keyed:recording:*", budget.DAY_S), 3000)
        self.assertEqual(self.count("pooled", budget.DAY_S), 4)
        self.assertTrue(budget.reserve("fastnear_rpc", "recording", keyed=True, now=T0 + 48600))

    def test_record_fetches_meter_and_only_fastnear_hosts_receive_authorization(self):
        for host, provider in budget.PROVIDER_HOSTS.items():
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory:
                rpc._NEARBLOCKS_USE.clear()
                client = rpc.Client("https://" + host, "record", kind="http", fixtures_dir=directory,
                                    headers={"authorization": "Bearer dummy-recording-token"}, clock=lambda: T0)
                with rpc.caller_class("recording"), mock.patch.object(rpc, "_fetch", return_value={}) as fetched:
                    client.get("/read")
                    client.get("/read")
                expected = {"authorization": "Bearer dummy-recording-token"} if provider in budget.FASTNEAR_CLASSES else {}
                self.assertEqual(fetched.call_args.args[2], expected)
                self.assertEqual(fetched.call_count, 1)
                mode = "keyed" if provider in budget.FASTNEAR_CLASSES else "keyless"
                self.assertGreaterEqual(self.count(f"{mode}:recording:{provider}", budget.DAY_S), 1)
        rpc._NEARBLOCKS_USE.clear()
        self.assertEqual(self.count("pooled", budget.DAY_S),
                         sum(provider in budget.FASTNEAR_CLASSES for provider in budget.PROVIDER_HOSTS.values()))

    def test_other_record_callers_never_touch_the_budget(self):
        for caller in budget.CALLER_CLASSES:
            if caller == "recording":
                continue
            with self.subTest(caller=caller), tempfile.TemporaryDirectory() as directory, \
                    rpc.caller_class(caller), mock.patch.object(budget, "reserve") as reserve, \
                    mock.patch.object(budget, "key_failure") as key_failure, \
                    mock.patch.object(rpc, "_fetch", side_effect=rpc.HttpError(429, rpc.MAINNET_RPC_URL)) as fetched:
                client = rpc.Client(rpc.MAINNET_RPC_URL, "record", kind="http", fixtures_dir=directory,
                                    headers={"Authorization": "Bearer dummy-recording-token"})
                with self.assertRaises(rpc.HttpError):
                    client.get("/read")
                reserve.assert_not_called()
                key_failure.assert_not_called()
                self.assertEqual(fetched.call_args.args[2], client.headers)


class TopReceiverRecordingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.fixtures = self.directory / "rpc"
        self.state = self.directory / "feed"
        self.database = self.state / budget.DATABASE_NAME
        self.key = self.directory / "dummy-provider-key.txt"
        self.key.write_text("  dummy-recording-token\n", encoding="utf-8")
        environment = mock.patch.dict(os.environ, {"FASTNEAR_API_KEY_FILE": str(self.key),
                                                   "FEED_STATE_DIR": str(self.state)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        for target in ("urllib.request.urlopen", "gate.rpc._open_with_deadline"):
            patcher = mock.patch(target, side_effect=AssertionError("the test opened the network"))
            patcher.start()
            self.addCleanup(patcher.stop)
        directory = mock.patch.object(intel_main, "RECEIVER_RPC_DIR", self.fixtures)
        directory.start()
        self.addCleanup(directory.stop)
        self.addCleanup(rpc._NEARBLOCKS_USE.clear)
        rpc._NEARBLOCKS_USE.clear()
        self.document = {"judged_at_ms": int(T0 * 1000), "subjects": [
            {"subject": subject, "age_recorded": index == 0, "as": "receiver",
             "request": {"kind": "near", "token_id": None, "receiver_id": subject,
                         "amount": "1", "memo": None}}
            for index, subject in enumerate(("first.near", "second.near"))]}
        self.ranking = self.directory / "top.json"
        self.ranking.write_text(json.dumps(self.document), encoding="utf-8")

    def count(self, counter):
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute("SELECT count FROM counters WHERE counter=? AND window=?",
                                     (counter, int(rpc.time.time() // budget.DAY_S))).fetchone()
        return row[0] if row else 0

    def judge_reads(self, entry, store, client, nearblocks, now_ms, archival, reports_account):
        client.call("status", [entry["subject"]])
        archival.call("status", [entry["subject"]])
        if entry["age_recorded"]:
            nearblocks.get("/v1/account/" + entry["subject"] + "/keys")

    def test_record_run_loads_key_and_meters_the_outbound_headers(self):
        response = {"jsonrpc": "2.0", "id": "gate", "result": {"x": 1}}
        with mock.patch.object(intel_main, "_judge", side_effect=self.judge_reads), \
                mock.patch.object(rpc, "_fetch", return_value=response) as fetched:
            intel_main.record_top_receiver_judgements(self.document, object())
        self.assertEqual(fetched.call_count, 5)
        for call in fetched.call_args_list:
            expected = {} if call.args[0].startswith(rpc.NEARBLOCKS_URL) else {
                "Authorization": "Bearer dummy-recording-token"}
            self.assertEqual(call.args[2], expected)
        self.assertEqual(self.count("keyed:recording:*"), 4)
        self.assertEqual(self.count("pooled"), 4)
        self.assertEqual(self.count("keyless:recording:nearblocks"), 1)

    def test_resuming_old_fixtures_makes_no_fetch_and_changes_no_file(self):
        response = {"jsonrpc": "2.0", "id": "gate", "result": {"x": 1}}
        with mock.patch.object(intel_main, "_judge", side_effect=self.judge_reads), \
                mock.patch.object(rpc, "_fetch", return_value=response):
            intel_main.record_top_receiver_judgements(self.document, object())
        for path in self.fixtures.glob("*.json"):
            fixture = json.loads(path.read_text(encoding="utf-8"))
            fixture["fetched_at"] = "2000-01-01T00:00:00Z"
            path.write_text(json.dumps(fixture), encoding="utf-8")
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.fixtures.glob("*.json")}
        with mock.patch.object(intel_main, "_judge", side_effect=self.judge_reads), \
                mock.patch.object(rpc, "_fetch", side_effect=AssertionError("a recorded read fetched again")) as fetched:
            intel_main.record_top_receiver_judgements(self.document, object())
        fetched.assert_not_called()
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}, before)
        self.assertEqual(self.count("pooled"), 4)
        self.assertIsNone(budget.database())
        self.assertIsNone(rpc._CALLER_CLASS.get())

    def test_budget_refusals_stop_the_command_even_when_the_rules_catch_them(self):
        for refusal in ("daily", "pooled", "cooldown", "database"):
            with self.subTest(refusal=refusal), tempfile.TemporaryDirectory() as directory:
                self.database = Path(directory) / budget.DATABASE_NAME
                with mock.patch.dict(os.environ, {budget.DATABASE_ENV: str(self.database)}):
                    now = rpc.time.time()
                    budget.reserve("other", "operator", keyed=False, now=now)
                    if refusal in ("daily", "pooled"):
                        counter, limit = (("keyed:recording:*", 3000) if refusal == "daily"
                                          else ("pooled", budget.POOLED_DAILY))
                        with closing(sqlite3.connect(self.database)) as connection:
                            connection.execute("INSERT INTO counters VALUES(?,?,?,NULL)",
                                               (counter, int(now // budget.DAY_S), limit))
                            connection.commit()
                    elif refusal == "cooldown":
                        budget.key_failure(429, now)
                    else:
                        self.database = Path(directory)
                        os.environ[budget.DATABASE_ENV] = directory

                    def swallowed(entry, store, client, nearblocks, now_ms, archival, reports_account):
                        for reader in (client, archival):
                            try:
                                reader.call("status", [])
                            except rpc.RateLimited:
                                pass
                        try:
                            nearblocks.get("/keys")
                        except rpc.RateLimited:
                            pass

                    with mock.patch.object(intel_main, "TOP_RECEIVERS_PATH", self.ranking), \
                            mock.patch.object(intel_main, "_judge", side_effect=swallowed) as judge, \
                            mock.patch.object(intel_main, "judge_receivers", return_value=[]) as replay, \
                            mock.patch.object(rpc, "_fetch", return_value={
                                "jsonrpc": "2.0", "id": "gate", "result": {}}) as fetched, \
                            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                        result = intel_main.main(["judge-top-receivers", "--record"])
                    self.assertEqual(result, 1)
                    self.assertIn("stopped:", stderr.getvalue())
                    self.assertIn("budget", stderr.getvalue().lower())
                    self.assertNotIn("dummy-recording-token", stderr.getvalue())
                    self.assertEqual(judge.call_count, 1)
                    replay.assert_not_called()
                    fetched.assert_not_called()
        self.assertFalse((self.state / budget.DATABASE_NAME).exists())

    def test_partial_recording_stops_at_the_cap_and_resumes_only_missing_reads(self):
        now = rpc.time.time()
        with budget.use_database(self.database):
            budget.reserve("other", "operator", keyed=False, now=now)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO counters VALUES(?,?,?,NULL)",
                               ("keyed:recording:*", int(now // budget.DAY_S), 2999))
            connection.commit()
        response = {"jsonrpc": "2.0", "id": "gate", "result": {"x": 1}}
        with mock.patch.object(intel_main, "TOP_RECEIVERS_PATH", self.ranking), \
                mock.patch.object(intel_main, "_judge", side_effect=self.judge_reads), \
                mock.patch.object(intel_main, "judge_receivers") as replay, \
                mock.patch.object(rpc, "_fetch", return_value=response) as fetched, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(intel_main.main(["judge-top-receivers", "--record"]), 1)
        self.assertEqual(fetched.call_count, 1)
        replay.assert_not_called()
        self.assertEqual(self.count("keyed:recording:*"), 3000)
        [path] = self.fixtures.glob("*.json")
        fixture = json.loads(path.read_text(encoding="utf-8"))
        fixture["fetched_at"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(fixture), encoding="utf-8")
        before = path.read_bytes(), path.stat().st_mtime_ns
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE counters SET count=0 WHERE counter='keyed:recording:*'")
            connection.commit()
        with mock.patch.object(intel_main, "_judge", side_effect=self.judge_reads), \
                mock.patch.object(rpc, "_fetch", return_value=response) as fetched:
            intel_main.record_top_receiver_judgements(self.document, object())
        self.assertEqual(fetched.call_count, 4)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(self.count("pooled"), 4)
        self.assertEqual(self.count("keyed:recording:*"), 3)

    def test_invalid_configured_key_stops_before_fetching(self):
        for contents in ("", "dummy\ninvalid"):
            with self.subTest(contents=contents):
                self.key.write_text(contents, encoding="utf-8")
                with mock.patch.object(intel_main, "TOP_RECEIVERS_PATH", self.ranking), \
                        mock.patch.object(rpc, "_fetch", side_effect=AssertionError("invalid credential fetched")) as fetched, \
                        mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    self.assertEqual(intel_main.main(["judge-top-receivers", "--record"]), 1)
                fetched.assert_not_called()
                self.assertIn("FASTNEAR_API_KEY_FILE", stderr.getvalue())

    def test_rejected_key_stops_after_exactly_one_keyed_fetch(self):
        for status in (401, 402, 403, 429):
            for caught_by_rules in (False, True):
                with self.subTest(status=status, caught_by_rules=caught_by_rules), \
                        tempfile.TemporaryDirectory() as directory, \
                        mock.patch.dict(os.environ, {budget.DATABASE_ENV: str(Path(directory) / budget.DATABASE_NAME)}):

                    def judge(entry, store, client, nearblocks, now_ms, archival, reports_account):
                        for reader in (client, archival):
                            try:
                                reader.call("status", [entry["subject"]])
                            except (rpc.HttpError, rpc.RateLimited):
                                if not caught_by_rules:
                                    raise

                    with mock.patch.object(intel_main, "TOP_RECEIVERS_PATH", self.ranking), \
                            mock.patch.object(intel_main, "_judge", side_effect=judge) as judged, \
                            mock.patch.object(intel_main, "judge_receivers") as replay, \
                            mock.patch.object(rpc, "_fetch", side_effect=rpc.HttpError(status, rpc.MAINNET_RPC_URL)) as fetched, \
                            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                        self.assertEqual(intel_main.main(["judge-top-receivers", "--record"]), 1)
                    self.assertEqual(fetched.call_count, 1)
                    self.assertEqual(fetched.call_args.args[2], {"Authorization": "Bearer dummy-recording-token"})
                    self.assertEqual(judged.call_count, 1)
                    replay.assert_not_called()
                    self.assertIn(f"HTTP {status}", stderr.getvalue())
                    self.assertNotIn("dummy-recording-token", stderr.getvalue())

    def test_record_requires_a_key_file_before_starting(self):
        os.environ.pop("FASTNEAR_API_KEY_FILE")
        with mock.patch.object(intel_main, "TOP_RECEIVERS_PATH", self.ranking), \
                mock.patch.object(intel_main.top_receivers, "fastnear_headers") as headers, \
                mock.patch.object(intel_main, "_judge") as judge, \
                mock.patch.object(intel_main, "judge_receivers", return_value=[]) as replay, \
                mock.patch.object(rpc, "_fetch") as fetched, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(intel_main.main(["judge-top-receivers", "--record"]), 1)
        self.assertIn("FASTNEAR_API_KEY_FILE", stderr.getvalue())
        headers.assert_not_called()
        judge.assert_not_called()
        replay.assert_not_called()
        fetched.assert_not_called()
        self.assertFalse(self.database.exists())

    def test_record_requires_an_external_budget_location_before_starting(self):
        source_database = intel_main.ROOT / "intel" / "feed" / budget.DATABASE_NAME
        for configuration in ({}, {"FEED_STATE_DIR": str(source_database.parent)},
                              {budget.DATABASE_ENV: str(source_database)},
                              {"FEED_STATE_DIR": str(self.state), budget.DATABASE_ENV: str(source_database)}):
            with self.subTest(configuration=configuration), \
                    mock.patch.dict(os.environ, {"FASTNEAR_API_KEY_FILE": str(self.key), **configuration}, clear=True), \
                    mock.patch.object(intel_main, "TOP_RECEIVERS_PATH", self.ranking), \
                    mock.patch.object(intel_main.top_receivers, "fastnear_headers") as headers, \
                    mock.patch.object(budget, "use_database", wraps=budget.use_database) as use_database, \
                    mock.patch.object(intel_main, "_judge") as judge, \
                    mock.patch.object(intel_main, "judge_receivers", return_value=[]) as replay, \
                    mock.patch.object(rpc, "_fetch") as fetched, \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(intel_main.main(["judge-top-receivers", "--record"]), 1)
                self.assertIn("outside the repository", stderr.getvalue())
                self.assertIn("FEED_STATE_DIR", stderr.getvalue())
                self.assertIn(budget.DATABASE_ENV, stderr.getvalue())
                use_database.assert_not_called()
                headers.assert_not_called()
                judge.assert_not_called()
                replay.assert_not_called()
                fetched.assert_not_called()
        self.assertFalse(self.database.exists())


if __name__ == "__main__":
    unittest.main()
